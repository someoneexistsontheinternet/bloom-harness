from __future__ import annotations

import os
import tomllib
from pathlib import Path


MIN_LUNA_CONTEXT_TOKENS = 256_000


def merge(base: dict, overrides: dict) -> dict:
    result = dict(base)
    for key, value in overrides.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


class Config:
    def __init__(self, root: Path, overrides: dict | None = None):
        self.root = root.expanduser().resolve()
        self.source = (
            Path(os.environ.get("BLOOM_SOURCE_ROOT", str(self.root))).expanduser().resolve()
        )
        self.data = tomllib.loads((self.source / "config/default.toml").read_text())
        local = self.root / "config/local.toml"
        if local.exists():
            self.data = merge(self.data, tomllib.loads(local.read_text()))
        self.data = merge(self.data, overrides or {})
        for provider in self.data["providers"].values():
            retries = provider.get("max_retries", 2)
            delay = provider.get("retry_delay_seconds", 1)
            if type(retries) is not int or not 0 <= retries <= 5:
                raise ValueError("Provider max_retries must be an integer from 0 to 5")
            if type(delay) not in (int, float) or not 0 <= delay <= 30:
                raise ValueError("Provider retry_delay_seconds must be between 0 and 30")
        for key in (
            "max_agents",
            "parallel_tools_per_agent",
            "max_local_processes",
            "mention_seconds",
            "wait_seconds",
            "worker_turns",
        ):
            value = self.data["runtime"][key]
            if type(value) is not int or value <= 0:
                raise ValueError(f"runtime.{key} must be a positive integer")
        for key, maximum in (
            ("evidence_workers", 3),
            ("evidence_turns", 100),
            ("evidence_seconds", 1800),
            ("evidence_process_seconds", 60),
            ("worker_report_seconds", 600),
        ):
            value = self.data["runtime"][key]
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"runtime.{key} must be between 1 and {maximum}")
        for name, profile in self.data["profiles"].items():
            if profile["provider"] not in self.data["providers"]:
                raise ValueError(f"Unknown provider for {name}")
            if (
                not 0
                < profile["compact_to"]
                < min(profile["compact_at"], profile["context_limit"] - profile["output_reserve"])
            ):
                raise ValueError(f"Invalid context policy for {name}")
        luna = self.data["profiles"]["luna"]
        luna_input = luna["context_limit"] - luna["output_reserve"]
        if min(luna_input, luna["compact_at"], luna["compact_to"]) < MIN_LUNA_CONTEXT_TOKENS:
            raise ValueError(
                "Luna context and compaction budgets must permit at least 256000 tokens"
            )
        self.workspace = self.root / "workspace"
        self.artifacts = self.root / "artifacts"
        memory = self.data.get("memory", {})
        for key, low, high in (
            ("context_tokens", 4096, 900000),
            ("idle_seconds", 1, 86400),
            ("fragment_characters", 1000, 20000),
            ("batch_characters", 1000, 100000),
            ("retrieval_events", 1, 50),
            ("retrieval_tokens", 1000, 200000),
            ("state_tokens", 1000, 100000),
            ("max_batches_per_pass", 1, 16),
            ("retrieval_turns", 1, 12),
            ("retrieval_seconds", 1, 600),
        ):
            if key in memory and (type(memory[key]) is not int or not low <= memory[key] <= high):
                raise ValueError(f"Invalid memory.{key}")
        # These limits govern Luna inputs, unlike the smaller evidence/state
        # budgets used to construct Astra's memory snapshot.
        for key in ("consolidation_tokens", "retrieval_input_tokens"):
            value = memory.get(key, MIN_LUNA_CONTEXT_TOKENS)
            if type(value) is not int or not MIN_LUNA_CONTEXT_TOKENS <= value <= luna_input:
                raise ValueError(
                    f"memory.{key} must be between {MIN_LUNA_CONTEXT_TOKENS} and {luna_input}"
                )
        if memory.get("fragment_characters", 10000) > memory.get("batch_characters", 40000):
            raise ValueError("Memory fragment exceeds batch budget")
        if (
            memory.get("context_tokens", 100000)
            >= self.data["profiles"]["astra"]["context_limit"]
            - self.data["profiles"]["astra"]["output_reserve"]
        ):
            raise ValueError("Memory budget must leave configured output headroom")
        web = self.data.get("web", {})
        for key, low, high in (
            ("candidate_count", 1, 20),
            ("selected_min", 1, 7),
            ("selected_max", 1, 7),
            ("visual_sources", 0, 7),
            ("discovery_pages", 1, 5),
            ("page_timeout_seconds", 1, 60),
            ("max_source_chars", 1000, 2_000_000),
        ):
            if key in web and (type(web[key]) is not int or not low <= web[key] <= high):
                raise ValueError(f"Invalid web.{key}")
        if web.get("selected_min", 3) > web.get("selected_max", 7):
            raise ValueError("web.selected_min exceeds selected_max")
        research = self.data.get("research", {})
        for key in ("result_tokens", "astra_tokens", "sol_tokens", "summary_characters"):
            if key in research and (type(research[key]) is not int or research[key] < 512):
                raise ValueError(
                    "research." + key + " must be an explicit positive working-evidence budget"
                )
        if (
            type(research.get("visual_sources", 2)) is not int
            or not 0 <= research.get("visual_sources", 2) <= 20
        ):
            raise ValueError("Invalid research.visual_sources")

    def initialize(self):
        for path in (
            self.workspace,
            self.artifacts,
            self.root / "memories",
            self.workspace / "requests",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def profile(self, role: str) -> dict:
        return self.data["profiles"][role]

    def project_python(self, cwd, repository=None):
        start = Path(repository or cwd).resolve()
        for directory in (start, *start.parents):
            for name in ("python", "python3"):
                executable = directory / ".venv/bin" / name
                if executable.is_file():
                    return executable
        for name in ("python", "python3"):
            executable = self.root / ".venv/bin" / name
            if executable.is_file():
                return executable
        raise RuntimeError("No project virtual-environment interpreter is available")

    def child_env(self, mode: str, overrides: dict | None = None) -> dict:
        from .toolchain import search_path

        # Restricted workers must not inherit credentials through the environment.
        if mode == "full":
            env = dict(os.environ)
        else:
            allowed = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM")
            env = {k: os.environ[k] for k in allowed if k in os.environ}
        env.update(
            {
                "PATH": search_path(self.root, env.get("PATH", "/usr/bin:/bin")),
                "VIRTUAL_ENV": str(self.root / ".venv"),
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
            }
        )
        env.update(overrides or {})
        return env

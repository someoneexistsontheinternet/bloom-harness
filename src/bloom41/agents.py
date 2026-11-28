"""Persistent owners and temporary branches share one authenticated identity model."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
import secrets
import shutil

from .store import atomic, encoded, identifier


PROFILES = {
    "astra": {"model": "gpt-6-astra", "effort": "max", "window": 1_000_000},
    "sol": {"model": "gpt-6.1-sol", "effort": "xhigh", "window": 1_000_000},
    "terra": {"model": "gpt-6.1-terra", "effort": "medium", "window": 1_000_000},
    "luna": {"model": "gpt-6-luna", "effort": "max", "window": 1_000_000},
}
BASE_TOOLS = [
    "read_file",
    "edit_file",
    "bash",
    "find",
    "search",
    "extract",
    "fork_self",
    "send_message",
    "cancel_job",
]
CONTEXT_TOOLS = ["read_file", "find", "search", "extract", "send_message", "cancel_job"]


def resource_directory(name):
    checkout = Path(__file__).resolve().parents[2]
    local = checkout / ("tools/bundled" if name == "bundled" else name)
    return local if local.exists() else Path(str(files("bloom41").joinpath(name)))


@dataclass(frozen=True)
class Actor:
    agent: str
    branch: str | None = None

    @property
    def scope(self):
        return self.branch or self.agent


class Agents:
    def __init__(self, store):
        self.store = store

    def bootstrap(self):
        self.create(
            "astra",
            "astra",
            "Maintain the owner's continuous relationship, direct work and personally review final answers.",
        )
        secret = self.store.control / "master.key"
        if not secret.exists():
            atomic(secret, secrets.token_bytes(32))
        shared = self.store.root / "workspace/shared/memory"
        (shared / "L2").mkdir(parents=True, exist_ok=True)
        for name in ("L0.md", "L1.md"):
            if not (shared / name).exists():
                atomic(shared / name, b"")

    def path(self, actor):
        actor = Actor(actor) if isinstance(actor, str) else actor
        identifier(actor.agent)
        if actor.branch:
            identifier(actor.branch)
            return self.store.root / "workspace/branches" / actor.branch
        return self.store.root / "workspace/agents" / actor.agent

    def get(self, actor):
        actor = Actor(actor) if isinstance(actor, str) else actor
        row = self.store.latest("agent.created", actor.agent)
        if not row:
            raise ValueError("Unknown agent: " + actor.agent)
        if actor.branch:
            branch = self.store.latest("branch.created", actor.branch)
            if not branch or branch["data"]["owner"] != actor.agent:
                raise PermissionError("Branch does not belong to this identity")
        return row["data"]

    def all(self):
        return [row["data"] for row in self.store.events("agent.created")]

    def read_roots(self, actor):
        roots = [self.store.root / "workspace/shared"]
        if not actor.branch and self.get(actor)["role"] in ("astra", "sol"):
            roots.extend(
                self.path(Actor(actor.agent, row["subject"]))
                for row in self.store.events("branch.created")
                if row["data"]["owner"] == actor.agent
            )
        if self.get(actor)["role"] == "terra" and actor.agent.startswith("terra-"):
            owner = actor.agent.removeprefix("terra-")
            roots.append(self.path(owner))
            roots.extend(
                self.path(Actor(owner, row["subject"]))
                for row in self.store.events("branch.created")
                if row["data"]["owner"] == owner
            )
        seen = set()
        while actor.branch and actor.branch not in seen:
            seen.add(actor.branch)
            row = self.store.latest("branch.created", actor.branch)
            if not row or row["data"]["owner"] != actor.agent:
                raise PermissionError("Invalid branch source grant")
            parent = row["data"]["parent_scope"]
            actor = Actor(actor.agent, parent if parent != actor.agent else None)
            roots.append(self.path(actor))
        return roots

    def create(self, name, role, responsibility, *, principal=None):
        identifier(name)
        if principal is not None and (principal != Actor("astra") or role != "sol"):
            raise PermissionError("Only continuous Astra creates persistent Sol identities")
        if role == "sol" and (
            name in ("astra", "user", "shared")
            or name.startswith("terra-")
            or len(name) > 100
            or self.store.latest("branch.created", name)
        ):
            raise ValueError("Choose a non-reserved specialist name of at most 100 characters")
        old = self.store.latest("agent.created", name)
        if old:
            if (old["data"]["role"], old["data"]["responsibility"]) != (role, responsibility):
                raise ValueError("Existing identity has a different responsibility")
            self.materialize(Actor(name), old["data"])
            if role in ("astra", "sol"):
                self.create(
                    "terra-" + name,
                    "terra",
                    "Gather context and monitor " + name + "; never perform its substantive work.",
                )
            return old["data"]
        if role not in PROFILES or not responsibility.strip():
            raise ValueError("Give the agent a role and continuing responsibility")
        data = {
            "id": name,
            "role": role,
            "responsibility": responsibility,
            "profile": PROFILES[role],
        }
        self.store.event("agent.created", name, data, key="agent:" + name)
        self.materialize(Actor(name), data)
        if role in ("astra", "sol"):
            self.create(
                "terra-" + name,
                "terra",
                "Gather context and monitor " + name + "; never perform its substantive work.",
            )
        return data

    def materialize(self, actor, data, *, source=None):
        root = self.path(actor)
        for folder in ("tools", "inbox", "outbound", "history", "work", "scratch", "sources"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        memory = root / "memory"
        if actor.branch and not memory.exists() and not memory.is_symlink():
            memory.symlink_to(self.path(actor.agent) / "memory", target_is_directory=True)
        if not actor.branch:
            (memory / "L2").mkdir(parents=True, exist_ok=True)
        atomic(
            root / "identity.toml",
            (
                f'id = "{actor.agent}"\nrole = "{data["role"]}"\n'
                + (f'branch = "{actor.branch}"\n' if actor.branch else "")
            ).encode(),
        )
        instructions = resource_directory("profiles") / (data["role"] + ".md")
        if not (root / "instructions.md").exists():
            atomic(
                root / "instructions.md",
                instructions.read_bytes()
                if instructions.exists()
                else data["responsibility"].encode(),
            )
        allowed = (
            CONTEXT_TOOLS
            if data["role"] in ("terra", "luna")
            else BASE_TOOLS + (["create_agent"] if actor == Actor("astra") else [])
        )
        origin = source / "tools" if source else resource_directory("bundled")
        if not self.store.by_key("tools-seeded:" + actor.scope):
            for name in allowed:
                src = origin / (name + ".py")
                dest = root / "tools" / src.name
                if src.exists() and not dest.exists():
                    shutil.copyfile(src, dest)
            self.store.event(
                "tools.seeded",
                actor.scope,
                {"names": [p.stem for p in sorted((root / "tools").glob("*.py"))]},
                key="tools-seeded:" + actor.scope,
            )
        for name in () if actor.branch else ("L0.md", "L1.md"):
            path = root / "memory" / name
            if not path.exists():
                atomic(path, b"")

    def fork(self, principal, branch, seed, question):
        identifier(branch)
        data = self.get(principal)
        if data["role"] not in ("astra", "sol"):
            raise PermissionError("Only working owners create investigation forks")
        actor = Actor(principal.agent, branch)
        record = {
            "owner": principal.agent,
            "parent_scope": principal.scope,
            "seed": seed,
            "question": question,
        }
        self.store.event("branch.created", branch, record, key="branch:" + branch)
        self.materialize(actor, data, source=self.path(principal))
        if seed.get("tools"):
            for spec in seed["tools"]:
                if spec["name"] != "create_agent":
                    atomic(
                        self.path(actor) / "tools" / (identifier(spec["name"]) + ".py"),
                        self.store.read(spec["source"]),
                    )
        return actor

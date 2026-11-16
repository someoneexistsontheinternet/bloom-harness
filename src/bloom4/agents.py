"""Persistent identities and responsibilities; roles choose policy, not separate runtimes."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .store import Store, atomic, encoded, identifier


PROFILES = {
    "astra": {"model": "gpt-6-astra", "effort": "max", "compact_at": 400_000, "target": 100_000},
    "sol": {"model": "gpt-6.1-sol", "effort": "xhigh", "compact_at": 400_000, "target": 100_000},
    "terra": {"model": "gpt-6.1-terra", "effort": "medium", "compact_at": 20_000, "target": 10_000},
    "luna": {"model": "gpt-6-luna", "effort": "max", "compact_at": 512_000, "target": 256_000},
}


@dataclass(frozen=True)
class Agent:
    """An identity has a role, explicit responsibility, private scope and optional parent."""

    id: str
    role: str
    responsibility: str
    parent: str | None = None
    seed: dict | None = None

    @property
    def persistent(self):
        return self.role != "luna"

    @property
    def writer(self):
        return self.role in ("astra", "sol")


class Agents:
    def __init__(self, store: Store):
        self.store = store

    def get(self, name):
        identifier(name)
        events = self.store.events("agent.created", target=name)
        if not events:
            raise ValueError("Unknown agent: " + name)
        agent = Agent(**events[0]["data"])
        updates = self.store.events("agent.responsibility", target=name)
        if updates:
            agent = Agent(**{**asdict(agent), "responsibility": updates[-1]["data"]["text"]})
        return agent

    def all(self):
        return [self.get(e["target"]) for e in self.store.events("agent.created")]

    def create(self, name, role, responsibility, *, parent=None, seed=None):
        identifier(name)
        if role not in PROFILES or not responsibility.strip():
            raise ValueError("Select a role and an explicit responsibility")
        agent = Agent(name, role, responsibility, parent, seed)
        self.store.append(
            "agent.created",
            asdict(agent),
            actor=parent or "owner",
            target=name,
            dedup="agent:" + name,
        )
        path = self.store.root / "agents" / name / "identity.json"
        atomic(path, encoded(asdict(agent)))
        (self.store.root / "work" / name).mkdir(parents=True, exist_ok=True)
        return agent

    def bootstrap(self):
        if not self.store.events("agent.created", target="astra"):
            self.create(
                "astra", "astra", "Understand owner intent, direct work and accept outcomes."
            )
        if not self.store.events("agent.created", target="terra"):
            self.create(
                "terra", "terra", "Assess changed evidence and decide whether to wake its owner."
            )

    def scope(self, name):
        self.get(name)
        return self.store.root / "work" / name

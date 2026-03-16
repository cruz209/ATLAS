from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple


class Sensitivity:
    PUBLIC       = "Public"
    INTERNAL     = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED   = "Restricted"

class LockStatus:
    NONE       = None
    READ_LOCK  = "ReadLock"
    WRITE_LOCK = "WriteLock"
    EXCLUSIVE  = "Exclusive"

class DocStatus:
    ACTIVE   = "Active"
    ARCHIVED = "Archived"
    DELETED  = "Deleted"


@dataclass
class Entity:
    entity_id: str

    # Document-domain fields (design doc §4.1)
    project_id: str = "default"
    owner: str = "unknown"
    sensitivity: str = Sensitivity.INTERNAL
    lock_status: Optional[str] = None
    lock_owner: Optional[str] = None
    retention_days: int = 365
    legal_hold: bool = False
    irreversible_score: float = 0.0
    last_modified: datetime = field(default_factory=datetime.utcnow)
    last_accessed: datetime = field(default_factory=datetime.utcnow)
    status: str = DocStatus.ACTIVE
    tags: Dict[str, str] = field(default_factory=dict)

    # Graph / world fields
    trust_boundary: str = "default"
    dependencies: List[str] = field(default_factory=list)
    exists: bool = True

    @property
    def age_days(self) -> int:
        return (datetime.utcnow() - self.last_modified).days

    @property
    def within_retention(self) -> bool:
        return self.age_days < self.retention_days


@dataclass
class WorldState:
    """Typed attributed graph world model with versioning."""
    entities: Dict[str, Entity] = field(default_factory=dict)
    edges: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    version: int = 0

    def get(self, entity_id: str) -> Optional[Entity]:
        return self.entities.get(entity_id)

    def add_entity(self, e: Entity) -> None:
        self.entities[e.entity_id] = e
        self.edges.setdefault(e.entity_id, [])
        self.version += 1

    def add_edge(self, src: str, rel: str, dst: str) -> None:
        self.edges.setdefault(src, []).append((rel, dst))
        self.version += 1

    def list_documents(self, project_id: Optional[str] = None,
                       older_than_days: int = 0,
                       status_filter: str = DocStatus.ACTIVE) -> List[Entity]:
        """Read tool: return documents matching filter criteria."""
        results = []
        for e in self.entities.values():
            if project_id and e.project_id != project_id:
                continue
            if e.status != status_filter:
                continue
            if e.age_days < older_than_days:
                continue
            results.append(e)
        return results

    def neighbors(self, start_ids: List[str],
                  rel_types: Optional[Set[str]] = None,
                  hops: int = 1, limit: int = 50) -> List[Tuple[str, str, str]]:
        seen: Set[Tuple[str, str, str]] = set()
        frontier = list(start_ids)
        out: List[Tuple[str, str, str]] = []
        for _ in range(max(0, hops)):
            nxt = []
            for s in frontier:
                for rel, d in self.edges.get(s, []):
                    if rel_types and rel not in rel_types:
                        continue
                    key = (s, rel, d)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(key)
                    nxt.append(d)
                    if len(out) >= limit:
                        return out
            frontier = nxt
        return out

    def lock(self, entity_id: str, owner: str,
             lock_status: str = LockStatus.EXCLUSIVE) -> None:
        e = self.entities[entity_id]
        e.lock_owner = owner
        e.lock_status = lock_status
        self.version += 1

    def unlock(self, entity_id: str, owner: str) -> None:
        e = self.entities[entity_id]
        if e.lock_owner == owner:
            e.lock_owner = None
            e.lock_status = None
            self.version += 1


def build_demo_world() -> WorldState:
    """
    Seeds the four demo documents from design doc §9.

      doc:Active_Incident_Runbook → BLOCK       (Exclusive lock by different actor)
      doc:Q4_Legal_Contract       → BLOCK       (legal_hold=True + Restricted)
      doc:Old_Project_Notes_2022  → ALLOW       (Internal, 730d old, 180d retention)
      doc:Roadmap_2024            → DOWNGRADE   (within 365d retention window)
    """
    w = WorldState()
    now = datetime.utcnow()

    # Doc 1 – BLOCK via P0.1 (Exclusive lock held by different actor)
    w.add_entity(Entity(
        entity_id="doc:Active_Incident_Runbook",
        project_id="proj:ops",
        owner="team:sre",
        sensitivity=Sensitivity.INTERNAL,
        lock_status=LockStatus.EXCLUSIVE,
        lock_owner="agent.oncall",
        retention_days=365,
        legal_hold=False,
        irreversible_score=0.9,
        last_modified=now - timedelta(days=200),
        status=DocStatus.ACTIVE,
        tags={"env": "prod", "team": "sre"},
    ))

    # Doc 2 – BLOCK via P0.2 (legal_hold) + would also hit P0.3 (Restricted)
    w.add_entity(Entity(
        entity_id="doc:Q4_Legal_Contract",
        project_id="proj:legal",
        owner="team:legal",
        sensitivity=Sensitivity.RESTRICTED,
        lock_status=None,
        lock_owner=None,
        retention_days=2555,
        legal_hold=True,
        irreversible_score=0.95,
        last_modified=now - timedelta(days=300),
        status=DocStatus.ACTIVE,
        tags={"env": "prod", "team": "legal"},
    ))

    # Doc 3 – ALLOW delete (clean P0, 730d old vs 180d retention)
    w.add_entity(Entity(
        entity_id="doc:Old_Project_Notes_2022",
        project_id="proj:archive",
        owner="team:engineering",
        sensitivity=Sensitivity.INTERNAL,
        lock_status=None,
        lock_owner=None,
        retention_days=180,
        legal_hold=False,
        irreversible_score=0.7,
        last_modified=now - timedelta(days=730),
        status=DocStatus.ACTIVE,
        tags={"env": "dev", "team": "engineering"},
        dependencies=[],
    ))

    # Doc 4 – DOWNGRADE to archive (90d old vs 365d retention → P1.2)
    w.add_entity(Entity(
        entity_id="doc:Roadmap_2024",
        project_id="proj:strategy",
        owner="team:product",
        sensitivity=Sensitivity.INTERNAL,
        lock_status=None,
        lock_owner=None,
        retention_days=365,
        legal_hold=False,
        irreversible_score=0.8,
        last_modified=now - timedelta(days=200),  # age>180 so list_documents picks it up; retention=365 so ATLAS downgrades
        status=DocStatus.ACTIVE,
        tags={"env": "prod", "team": "product"},
        dependencies=["workflow:quarterly-review"],
    ))

    # Supporting workflow entity referenced by roadmap
    w.add_entity(Entity(
        entity_id="workflow:quarterly-review",
        project_id="proj:strategy",
        owner="team:product",
        sensitivity=Sensitivity.INTERNAL,
        irreversible_score=0.1,
        status=DocStatus.ACTIVE,
        exists=True,
    ))

    # Graph edges
    w.add_edge("doc:Active_Incident_Runbook", "BELONGS_TO",    "proj:ops")
    w.add_edge("doc:Q4_Legal_Contract",       "BELONGS_TO",    "proj:legal")
    w.add_edge("doc:Q4_Legal_Contract",       "SUBJECT_TO",    "policy:legal-hold-2024")
    w.add_edge("doc:Old_Project_Notes_2022",  "BELONGS_TO",    "proj:archive")
    w.add_edge("doc:Roadmap_2024",            "BELONGS_TO",    "proj:strategy")
    w.add_edge("doc:Roadmap_2024",            "DEPENDS_ON",    "workflow:quarterly-review")
    w.add_edge("workflow:quarterly-review",   "REFERENCED_BY", "doc:Roadmap_2024")

    return w
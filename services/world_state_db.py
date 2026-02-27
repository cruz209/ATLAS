from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from services import db

# Reuse constants from core world_state for compatibility with admissibility logic
from atlas_core.world_state import Sensitivity, LockStatus, DocStatus

@dataclass
class Entity:
    entity_id: str
    project_id: str = "default"
    owner: str = "unknown"
    sensitivity: str = Sensitivity.INTERNAL
    lock_status: Optional[str] = None
    lock_owner: Optional[str] = None
    retention_days: int = 365
    legal_hold: bool = False
    irreversible_score: float = 0.0
    last_modified: datetime = datetime.utcnow()
    last_accessed: datetime = datetime.utcnow()
    status: str = DocStatus.ACTIVE
    tags: Dict[str, str] = None
    trust_boundary: str = "default"
    dependencies: List[str] = None
    exists: bool = True

    @property
    def age_days(self) -> int:
        return (datetime.utcnow() - self.last_modified).days

    @property
    def within_retention(self) -> bool:
        return self.age_days < int(self.retention_days or 0)

def _row_to_entity(d: Dict) -> Entity:
    return Entity(
        entity_id=d["entity_id"],
        project_id=d.get("project_id","default"),
        owner=d.get("owner","unknown"),
        sensitivity=d.get("sensitivity", Sensitivity.INTERNAL),
        lock_status=d.get("lock_status"),
        lock_owner=d.get("lock_owner"),
        retention_days=int(d.get("retention_days") or 0),
        legal_hold=bool(d.get("legal_hold")),
        irreversible_score=float(d.get("irreversible_score") or 0.0),
        last_modified=datetime.fromisoformat(d["last_modified"]),
        last_accessed=datetime.fromisoformat(d["last_accessed"]),
        status=d.get("status", DocStatus.ACTIVE),
        tags=d.get("tags") or {},
        trust_boundary=d.get("trust_boundary","default"),
        dependencies=d.get("dependencies") or [],
        exists=bool(d.get("exists", 1)),
    )

class WorldStateDB:
    """DB-backed world model used by both proxy and tool backend."""
    def __init__(self) -> None:
        self.version = db.get_version()

    def refresh_version(self) -> None:
        self.version = db.get_version()

    def get(self, entity_id: str) -> Optional[Entity]:
        d = db.fetch_entity(entity_id)
        if not d:
            return None
        self.refresh_version()
        return _row_to_entity(d)

    def list_documents(self, project_id: Optional[str] = None, older_than_days: int = 0,
                       status_filter: str = DocStatus.ACTIVE) -> List[Entity]:
        rows = db.list_documents(project_id, older_than_days, status_filter)
        self.refresh_version()
        return [_row_to_entity(r) for r in rows]

    def neighbors(self, start_ids: List[str], rel_types: Optional[Set[str]] = None,
                  hops: int = 1, limit: int = 50) -> List[Tuple[str,str,str]]:
        rel_list = list(rel_types) if rel_types else None
        self.refresh_version()
        return db.neighbors(start_ids, rel_list, hops, limit)

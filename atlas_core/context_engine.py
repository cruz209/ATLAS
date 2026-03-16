from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import time

from .world_state import WorldState
from .types import NeedFact


@dataclass
class Doc:
    doc_id: str
    title: str
    text: str
    tags: Set[str] = field(default_factory=set)


@dataclass
class InMemoryContextStore:
    """A tiny stand-in for a real catalog/doc store."""
    docs: Dict[str, Doc] = field(default_factory=dict)

    def add_doc(self, doc: Doc) -> None:
        self.docs[doc.doc_id] = doc

    def search(self, query: str, filters: Optional[Dict[str, Any]] = None, limit: int = 5) -> List[Doc]:
        q = query.lower().strip()
        filters = filters or {}
        tag = filters.get("tag")
        hits = []
        for d in self.docs.values():
            if tag and tag not in d.tags:
                continue
            hay = (d.title + " " + d.text).lower()
            if q in hay:
                hits.append(d)
        # crude ranking: shorter title match first
        hits.sort(key=lambda d: (d.title.lower().find(q) if q in d.title.lower() else 9999, len(d.text)))
        return hits[:limit]


class ContextCache:
    def __init__(self):
        self._cache: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}

    def get(self, key: Tuple[Any, ...]) -> Optional[Any]:
        item = self._cache.get(key)
        if not item:
            return None
        return item[1]

    def set(self, key: Tuple[Any, ...], value: Any) -> None:
        self._cache[key] = (time.time(), value)


@dataclass
class ContextEngine:
    world: WorldState
    store: InMemoryContextStore
    cache: ContextCache = field(default_factory=ContextCache)

    # ---- Retrieval tools (bounded) ----
    def catalog_search(self, actor: str, query: str, filters: Optional[Dict[str, Any]] = None, limit: int = 5) -> Dict[str, Any]:
        # NOTE: catalog results *could* be actor-dependent (auth), so keep actor in key.
        key = ("catalog_search", actor, query, tuple(sorted((filters or {}).items())), limit, self.world.version)
        cached = self.cache.get(key)
        if cached is not None:
            return {"cached": True, **cached}

        docs = self.store.search(query=query, filters=filters, limit=limit)
        result = {
            "cached": False,
            "items": [{"doc_id": d.doc_id, "title": d.title, "tags": sorted(list(d.tags))} for d in docs],
            "limit": limit,
        }
        self.cache.set(key, result)
        return result

    def entity_peek(self, actor: str, entity_id: str) -> Dict[str, Any]:
        # Output currently does NOT depend on actor (no ACL filtering in PoC),
        # so remove actor to improve cache reuse.
        key = ("entity_peek", entity_id, self.world.version)
        cached = self.cache.get(key)
        if cached is not None:
            return {"cached": True, **cached}

        e = self.world.get(entity_id)
        if not e:
            result = {"cached": False, "entity": None}
        else:
            # IMPORTANT: include all gate-critical fields so admissibility is correct.
            result = {
                "cached": False,
                "entity": {
                    "entity_id": e.entity_id,

                    # existence / lifecycle
                    "exists": e.exists,
                    "status": e.status,

                    # P0 safety (must-have)
                    "lock_status": e.lock_status,
                    "lock_owner": e.lock_owner,
                    "legal_hold": e.legal_hold,
                    "sensitivity": e.sensitivity,
                    "trust_boundary": e.trust_boundary,

                    # P1 safety (must-have)
                    "retention_days": e.retention_days,
                    "age_days": e.age_days,
                    "dependencies": list(e.dependencies),

                    # misc / scoring
                    "irreversible_score": e.irreversible_score,
                }
            }

        self.cache.set(key, result)
        return result

    def graph_browse(self, actor: str, start_ids: Sequence[str], rel_types: Optional[Set[str]] = None, hops: int = 1, limit: int = 50) -> Dict[str, Any]:
        # Output currently does NOT depend on actor (no ACL filtering in PoC),
        # so remove actor to improve cache reuse.
        key = ("graph_browse", tuple(start_ids), tuple(sorted(rel_types)) if rel_types else None, hops, limit, self.world.version)
        cached = self.cache.get(key)
        if cached is not None:
            return {"cached": True, **cached}

        triples = self.world.neighbors(list(start_ids), rel_types=rel_types, hops=hops, limit=limit)
        result = {"cached": False, "triples": [{"src": s, "rel": r, "dst": d} for s, r, d in triples], "limit": limit}
        self.cache.set(key, result)
        return result

    def doc_excerpt(self, actor: str, doc_id: str, span: Tuple[int, int] = (0, 400), max_chars: int = 1200) -> Dict[str, Any]:
        # Doc access might become actor-dependent later, so keep actor in key.
        key = ("doc_excerpt", actor, doc_id, span, max_chars, self.world.version)
        cached = self.cache.get(key)
        if cached is not None:
            return {"cached": True, **cached}

        d = self.store.docs.get(doc_id)
        if not d:
            result = {"cached": False, "doc_id": doc_id, "excerpt": None}
        else:
            start, end = span
            excerpt = d.text[start:end]
            excerpt = excerpt[:max_chars]
            result = {"cached": False, "doc_id": doc_id, "title": d.title, "excerpt": excerpt}
        self.cache.set(key, result)
        return result

    def evidence_pack(self, actor: str, intent: str, action_name: str, targets: Sequence[str], budget: int = 3) -> Dict[str, Any]:
        """A pragmatic 'bundle' used by an agent to satisfy multiple typed fact requests."""
        key = ("evidence_pack", actor, intent, action_name, tuple(targets), budget, self.world.version)
        cached = self.cache.get(key)
        if cached is not None:
            return {"cached": True, **cached}

        evidence: Dict[str, Any] = {"entities": {}, "docs": [], "graph": []}

        # 1) entity peeks
        for t in list(targets)[: max(1, budget)]:
            evidence["entities"][t] = self.entity_peek(actor, t)["entity"]

        # 2) small graph browse around targets
        evidence["graph"] = self.graph_browse(actor, start_ids=list(targets)[:2], hops=1, limit=25)["triples"]

        # 3) a couple relevant docs
        hits = self.catalog_search(actor, query=action_name, filters=None, limit=min(2, budget))["items"]
        for item in hits:
            evidence["docs"].append(self.doc_excerpt(actor, item["doc_id"], span=(0, 500), max_chars=800))

        result = {"cached": False, "intent": intent, "action": action_name, "targets": list(targets), "budget": budget, "evidence": evidence}
        self.cache.set(key, result)
        return result

    # ---- Helper: build facts from NeedFact requests ----
    def satisfy_needs(self, actor: str, needs: List[NeedFact], action_name: str, targets: Sequence[str]) -> Dict[str, Any]:
        """Fetch only what is needed, in a bounded manner."""
        facts: Dict[str, Any] = {}
        for need in needs:
            if need.fact_type == "entity":
                facts.setdefault("entities", {})[need.target] = self.entity_peek(actor, need.target)["entity"]

            elif need.fact_type == "dependencies":
                e = self.entity_peek(actor, need.target)["entity"]
                facts.setdefault("dependencies", {})[need.target] = (e or {}).get("dependencies", [])

            elif need.fact_type == "locks":
                e = self.entity_peek(actor, need.target)["entity"]
                facts.setdefault("locks", {})[need.target] = {
                    "lock_status": (e or {}).get("lock_status"),
                    "lock_owner": (e or {}).get("lock_owner"),
                }

            elif need.fact_type == "trust_boundary":
                e = self.entity_peek(actor, need.target)["entity"]
                facts.setdefault("trust_boundary", {})[need.target] = (e or {}).get("trust_boundary")

            elif need.fact_type == "policy_doc":
                # Search docs tagged policy and excerpt one hit
                items = self.catalog_search(actor, query=need.target, filters={"tag": "policy"}, limit=1)["items"]
                if items:
                    facts.setdefault("policy_docs", {})[need.target] = self.doc_excerpt(actor, items[0]["doc_id"], span=(0, 600), max_chars=900)
                else:
                    facts.setdefault("policy_docs", {})[need.target] = None

            else:
                facts.setdefault("unknown", []).append({"fact_type": need.fact_type, "target": need.target})

        # Additionally, allow an agent to pull an evidence_pack if they want a single call
        facts["evidence_pack"] = self.evidence_pack(actor, intent="satisfy_needs", action_name=action_name, targets=targets, budget=3)
        return facts

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .actions import Action
from .admissibility import evaluate_admissibility
from .context_engine import ContextEngine, Doc, InMemoryContextStore
from .types import Decision, GateResult
from .world_state import Entity, WorldState, DocStatus, build_demo_world


@dataclass
class AuditEntry:
    doc_id: str
    proposed_action: str
    decision: str
    reasons: List[str]
    executed: bool
    final_action: str  # may differ if DOWNGRADE swapped action
    world_version: int


@dataclass
class ExecutionOutcome:
    action: Action
    gate: GateResult
    executed: bool
    final_world_version: int
    audit: AuditEntry


# ── Document lifecycle tools ──────────────────────────────────────────────────

def tool_list_documents(world: WorldState, project_id: Optional[str] = None,
                         older_than_days: int = 0) -> List[Dict[str, Any]]:
    """Read tool: list active documents older than N days."""
    docs = world.list_documents(
        project_id=project_id,
        older_than_days=older_than_days,
        status_filter=DocStatus.ACTIVE,
    )
    return [
        {
            "doc_id":     e.entity_id,
            "project_id": e.project_id,
            "age_days":   e.age_days,
            "sensitivity":e.sensitivity,
            "legal_hold": e.legal_hold,
        }
        for e in docs
    ]


def tool_archive_document(world: WorldState, doc_id: str) -> str:
    """Execution tool: archive a document (reversible)."""
    e = world.get(doc_id)
    if not e:
        return f"ERROR: {doc_id} not found"
    e.status = DocStatus.ARCHIVED
    world.version += 1
    return f"ARCHIVED: {doc_id}"


def tool_delete_document(world: WorldState, doc_id: str) -> str:
    """Execution tool: delete a document (irreversible)."""
    e = world.get(doc_id)
    if not e:
        return f"ERROR: {doc_id} not found"
    e.status = DocStatus.DELETED
    e.exists = False
    world.version += 1
    return f"DELETED: {doc_id}"


def execute_action(world: WorldState, action: Action) -> str:
    if action.name == "archive_document":
        return tool_archive_document(world, action.targets[0])
    if action.name == "delete_document":
        return tool_delete_document(world, action.targets[0])
    return "noop"


# ── ATLAS gate loop ───────────────────────────────────────────────────────────

def run_once(world: WorldState, ctx: ContextEngine,
             action: Action, max_rounds: int = 4) -> ExecutionOutcome:
    """
    Runs the progressive disclosure loop for a single action.
    Handles: ALLOW, BLOCK, ESCALATE, DOWNGRADE, NEEDS_CONTEXT.
    """
    evidence: Dict[str, Any] = {}
    proposed_name = action.name

    for _round in range(max_rounds):
        gate = evaluate_admissibility(world, action, evidence=evidence)

        # ── NEEDS_CONTEXT: fetch typed facts and retry ────────────────────────
        if gate.decision == Decision.NEEDS_CONTEXT and gate.needs:
            facts = ctx.satisfy_needs(
                actor=action.actor,
                needs=gate.needs,
                action_name=action.name,
                targets=action.targets,
            )
            evidence = {**evidence, **facts}
            continue

        # ── DOWNGRADE: swap delete → archive and re-run ───────────────────────
        if gate.decision == Decision.DOWNGRADE:
            downgrade_reasons = gate.reasons  # capture before reset
            action = Action(
                name="archive_document",
                actor=action.actor,
                targets=action.targets,
                irreversible=False,
                requires_confirmation=False,
                confirmation_token=None,
            )
            evidence = {}   # reset so P0 is re-checked with new action name

            # Run archive through ATLAS (P0 check on the new action)
            archive_gate = evaluate_admissibility(world, action, evidence={})
            if archive_gate.decision == Decision.NEEDS_CONTEXT and archive_gate.needs:
                facts = ctx.satisfy_needs(actor=action.actor, needs=archive_gate.needs,
                                          action_name=action.name, targets=action.targets)
                archive_gate = evaluate_admissibility(world, action, evidence=facts)

            executed = archive_gate.decision == Decision.ALLOW
            if executed:
                execute_action(world, action)

            audit = AuditEntry(
                doc_id=action.targets[0] if action.targets else "?",
                proposed_action=proposed_name,
                decision="DOWNGRADE",
                reasons=downgrade_reasons,
                executed=executed,
                final_action="archive_document" if executed else "none",
                world_version=world.version,
            )
            return ExecutionOutcome(action=action, gate=gate, executed=executed,
                                    final_world_version=world.version, audit=audit)

        # ── ALLOW: execute ────────────────────────────────────────────────────
        if gate.decision == Decision.ALLOW:
            result = execute_action(world, action)
            audit = AuditEntry(
                doc_id=action.targets[0] if action.targets else "?",
                proposed_action=proposed_name,
                decision=gate.decision.value,
                reasons=gate.reasons,
                executed=True,
                final_action=action.name,
                world_version=world.version,
            )
            return ExecutionOutcome(action=action, gate=gate, executed=True,
                                    final_world_version=world.version, audit=audit)

        # ── BLOCK / ESCALATE: do not execute ─────────────────────────────────
        audit = AuditEntry(
            doc_id=action.targets[0] if action.targets else "?",
            proposed_action=proposed_name,
            decision=gate.decision.value,
            reasons=gate.reasons,
            executed=False,
            final_action="none",
            world_version=world.version,
        )
        return ExecutionOutcome(action=action, gate=gate, executed=False,
                                final_world_version=world.version, audit=audit)

    # Exceeded max rounds
    gate = GateResult(
        decision=Decision.ESCALATE,
        reasons=["Exceeded max_rounds without admissibility resolution."],
        needs=None, evidence=evidence,
    )
    audit = AuditEntry(
        doc_id=action.targets[0] if action.targets else "?",
        proposed_action=proposed_name,
        decision=gate.decision.value,
        reasons=gate.reasons,
        executed=False,
        final_action="none",
        world_version=world.version,
    )
    return ExecutionOutcome(action=action, gate=gate, executed=False,
                            final_world_version=world.version, audit=audit)


# ── Context / world builders ──────────────────────────────────────────────────

def build_default_context(world: WorldState) -> ContextEngine:
    store = InMemoryContextStore()
    store.add_doc(Doc(
        doc_id="doc:policy:irreversible",
        title="Irreversible Actions Policy",
        text=(
            "Irreversible actions (delete, funds transfer) require explicit human "
            "confirmation. Simulated via confirmation_token on the Action."
        ),
        tags={"policy"},
    ))
    store.add_doc(Doc(
        doc_id="doc:policy:retention",
        title="Document Retention Policy",
        text=(
            "Documents within their retention window must be archived rather than "
            "deleted. Retention windows are set per document class."
        ),
        tags={"policy"},
    ))
    store.add_doc(Doc(
        doc_id="doc:runbook:delete",
        title="Runbook: delete_document",
        text=(
            "delete_document marks an entity as Deleted and removes content pointer. "
            "In production this is wired to S3 object expiry and CloudWatch audit."
        ),
        tags={"runbook"},
    ))
    return ContextEngine(world=world, store=store)


# ── Standalone demo (non-Nova path, for quick local testing) ─────────────────

def run_demo() -> None:
    """
    Standalone demo that exercises all 4 document outcomes without Nova.
    The Nova agent path is in nova_agent.py.
    """
    world = build_demo_world()
    ctx   = build_default_context(world)
    actor = "agent.nova"

    print("\n" + "═" * 70)
    print("  ATLAS Document Management PoC — Standalone Demo")
    print("  Request: 'Delete old project documents older than 180 days'")
    print("═" * 70)

    # Simulate what Nova's list_documents call returns
    candidates = tool_list_documents(world, older_than_days=180)
    print(f"\n[tool:list_documents] Found {len(candidates)} candidate(s):\n")
    for c in candidates:
        print(f"  • {c['doc_id']}  age={c['age_days']}d  "
              f"sensitivity={c['sensitivity']}  legal_hold={c['legal_hold']}")

    print()
    audit_log: List[AuditEntry] = []

    for candidate in candidates:
        doc_id = candidate["doc_id"]
        action = Action(
            name="delete_document",
            actor=actor,
            targets=[doc_id],
            irreversible=True,
            requires_confirmation=False,
            confirmation_token=None,
        )
        outcome = run_once(world, ctx, action)
        audit_log.append(outcome.audit)

    # ── Audit trace ───────────────────────────────────────────────────────────
    print("\n" + "─" * 70)
    print("  ATLAS AUDIT TRACE")
    print("─" * 70)
    for entry in audit_log:
        icon = "✅" if entry.executed else ("⬇️ " if entry.decision == "DOWNGRADE" else "🚫")
        print(f"\n{icon}  {entry.doc_id}")
        print(f"     Proposed : {entry.proposed_action}")
        print(f"     Decision : {entry.decision}")
        print(f"     Executed : {entry.final_action if entry.executed else 'none'}")
        print(f"     Reason   : {entry.reasons[0] if entry.reasons else '-'}")
    print("\n" + "─" * 70)
    print(f"  World version after run: {world.version}")
    print("─" * 70 + "\n")
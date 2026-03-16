from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple

from .actions import Action
from .types import Decision, GateResult, NeedFact
from .world_state import WorldState, LockStatus, Sensitivity, DocStatus

# ── Actor trust config ────────────────────────────────────────────────────────
DEFAULT_TRUST_ALLOW = {
    "agent.nova":    {"default", "internal"},
    "agent.payment": {"default", "internal", "payment"},
}

# Actions that are irreversible and require confirmation tokens
IRREVERSIBLE_ACTIONS = {"delete_document"}

# Sensitivity levels that require ESCALATE (explicit confirmation) on delete
HIGH_SENSITIVITY = {Sensitivity.CONFIDENTIAL, Sensitivity.RESTRICTED}

# Lock statuses that block archive/delete
BLOCKING_LOCKS = {LockStatus.WRITE_LOCK, LockStatus.EXCLUSIVE}


# ── P0: Safety Invariants (Hard Stops) ────────────────────────────────────────
def p0_safety_invariants(
    world: WorldState, action: Action,
    evidence: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[Decision], List[str], List[NeedFact]]:
    """
    Returns (ok, override_decision, reasons, needs).
    override_decision is set when we want ESCALATE instead of BLOCK.
    """
    reasons: List[str] = []
    needs: List[NeedFact] = []
    evidence = evidence or {}

    is_mutating = action.name in ("delete_document", "archive_document")

    for t in action.targets:
        ent = evidence.get("entities", {}).get(t)
        if ent is None:
            needs.append(NeedFact("entity", t,
                "Need entity attributes for P0 checks (lock/sensitivity/legal_hold)."))
            continue

        # P1.1 – Existence (checked early to short-circuit)
        if not ent.get("exists", True) or ent.get("status") == DocStatus.DELETED:
            reasons.append(f"P0.existence: {t} does not exist or is already deleted.")
            return False, None, reasons, needs

        if is_mutating:
            # P0.1 – Lock conflict
            lock_status = ent.get("lock_status")
            lock_owner  = ent.get("lock_owner")
            if lock_status in BLOCKING_LOCKS and lock_owner != action.actor:
                reasons.append(
                    f"P0.1 LOCK CONFLICT: '{t}' has {lock_status} held by '{lock_owner}'. "
                    f"Actor '{action.actor}' cannot archive/delete."
                )
                return False, None, reasons, needs


            # P0.2 – Legal hold (hard block on archive/delete)
            if action.name in ("delete_document", "archive_document") and ent.get("legal_hold", False):
                reasons.append(
                    f"P0.2 LEGAL HOLD: '{t}' is under legal hold. "
                    f"{action.name} is blocked until hold is lifted."
                )
                return False, None, reasons, needs

            # P0.3 – Sensitivity escalation (ESCALATE, not BLOCK)
            sensitivity = ent.get("sensitivity", Sensitivity.INTERNAL)
            if action.name == "delete_document" and sensitivity in HIGH_SENSITIVITY:
                if not action.confirmation_token:
                    reasons.append(
                        f"P0.3 SENSITIVITY ESCALATION: '{t}' is {sensitivity}. "
                        f"Explicit human confirmation required before delete."
                    )
                    return False, Decision.ESCALATE, reasons, needs
                # token present → allow to pass through
                reasons.append(
                    f"P0.3 sensitivity check passed with confirmation token for '{t}'."
                )

            # P0.4 – Trust boundary
            tb = ent.get("trust_boundary", "default")
            allowed = DEFAULT_TRUST_ALLOW.get(action.actor, {"default"})
            if action.crosses_trust_boundary and tb not in allowed:
                reasons.append(
                    f"P0.4 TRUST BOUNDARY: actor '{action.actor}' cannot cross "
                    f"boundary '{tb}' for target '{t}'."
                )
                return False, None, reasons, needs

    return True, None, reasons, needs


# ── P1: World Validity ────────────────────────────────────────────────────────
def p1_world_validity(
    world: WorldState, action: Action,
    evidence: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[Decision], List[str], List[NeedFact]]:
    """
    Returns (ok, override_decision, reasons, needs).
    override_decision=DOWNGRADE when delete should become archive.
    """
    reasons: List[str] = []
    needs: List[NeedFact] = []
    evidence = evidence or {}

    if action.name != "delete_document":
        return True, None, reasons, needs

    for t in action.targets:
        ent = evidence.get("entities", {}).get(t)
        if ent is None:
            needs.append(NeedFact("entity", t,
                "Need entity to verify retention and dependencies."))
            continue

        # P1.2 – Retention window
        age_days       = ent.get("age_days", 0)
        retention_days = ent.get("retention_days", 365)
        if age_days < retention_days:
            reasons.append(
                f"P1.2 RETENTION: '{t}' is {age_days}d old but retention "
                f"window is {retention_days}d. Downgrading delete → archive."
            )
            return False, Decision.DOWNGRADE, reasons, needs

        # P1.3 – Active dependencies
        deps = ent.get("dependencies", [])
        for dep in deps:
            dep_ent = evidence.get("entities", {}).get(dep)
            if dep_ent is None:
                needs.append(NeedFact("entity", dep,
                    f"Need dependency '{dep}' to check health for '{t}'."))
                continue
            if dep_ent.get("exists", True):
                reasons.append(
                    f"P1.3 DEPENDENCY: '{t}' has active dependency '{dep}'. "
                    f"Downgrading delete → archive."
                )
                return False, Decision.DOWNGRADE, reasons, needs

    return True, None, reasons, needs


# ── P2–P4: Soft Predicates (stubs, design doc §6.3) ──────────────────────────
def p2_goal_progress(world: WorldState, action: Action) -> Tuple[bool, List[str]]:
    return True, ["P2: deleting/archiving old docs advances cleanup goal."]

def p3_efficiency(world: WorldState, action: Action) -> Tuple[bool, List[str]]:
    return True, ["P3: prefer archive over delete (reversible preferred)."]

def p4_ux_quality(world: WorldState, action: Action) -> Tuple[bool, List[str]]:
    return True, ["P4: batch processing, minimal confirmation prompts."]


# ── Main gate ─────────────────────────────────────────────────────────────────
def evaluate_admissibility(
    world: WorldState, action: Action,
    evidence: Optional[Dict[str, Any]] = None
) -> GateResult:
    """
    Lexicographic evaluation P0 → P1 → P2-P4.
    Returns one of: ALLOW | BLOCK | ESCALATE | DOWNGRADE | NEEDS_CONTEXT
    """
    # ── P0 ────────────────────────────────────────────────────────────────────
    ok0, override0, reasons0, needs0 = p0_safety_invariants(world, action, evidence=evidence)
    if needs0:
        return GateResult(
            decision=Decision.NEEDS_CONTEXT,
            reasons=["Missing facts for P0 safety checks."],
            needs=needs0, evidence=evidence
        )
    if not ok0:
        decision = override0 if override0 else Decision.BLOCK
        return GateResult(decision=decision, reasons=reasons0, needs=None, evidence=evidence)

    # ── P1 ────────────────────────────────────────────────────────────────────
    ok1, override1, reasons1, needs1 = p1_world_validity(world, action, evidence=evidence)
    if needs1:
        return GateResult(
            decision=Decision.NEEDS_CONTEXT,
            reasons=["Missing facts for P1 world validity."],
            needs=needs1, evidence=evidence
        )
    if not ok1:
        decision = override1 if override1 else Decision.BLOCK
        return GateResult(decision=decision, reasons=reasons0 + reasons1, needs=None, evidence=evidence)

    # ── P2–P4 (soft, all pass in PoC) ────────────────────────────────────────
    _, reasons2 = p2_goal_progress(world, action)
    _, reasons3 = p3_efficiency(world, action)
    _, reasons4 = p4_ux_quality(world, action)

    return GateResult(
        decision=Decision.ALLOW,
        reasons=reasons0 + reasons1 + reasons2 + reasons3 + reasons4,
        needs=None, evidence=evidence
    )
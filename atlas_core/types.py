from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

class Decision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"
    DOWNGRADE = "DOWNGRADE"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"

@dataclass(frozen=True)
class NeedFact:
    """A typed fact request ATLAS can return when it lacks required evidence."""
    fact_type: str
    target: str
    reason: str

@dataclass
class GateResult:
    decision: Decision
    reasons: List[str]
    needs: List[NeedFact] | None = None
    evidence: Dict[str, Any] | None = None

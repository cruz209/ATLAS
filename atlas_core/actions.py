from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class Action:
    """A candidate tool call / action proposed by an agent."""
    name: str
    actor: str
    targets: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)

    # Safety-related meta
    irreversible: bool = False
    crosses_trust_boundary: bool = False
    requires_confirmation: bool = False
    confirmation_token: Optional[str] = None  # provided by user/human approval

    # Preconditions the agent claims are true (ATLAS verifies via world/context)
    preconditions: Dict[str, Any] = field(default_factory=dict)

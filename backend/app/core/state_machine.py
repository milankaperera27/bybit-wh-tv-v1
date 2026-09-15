"""HITL 1-tap confirmation state machine (artifact §5 / spec §5).

```
  webhook ──validate──▶ PENDING_APPROVAL ──(Redis SETEX 60)
        │                    │
        │  tap Confirm/Route │  tap Reject          60s elapse
        ▼                    ▼                          ▼
     CONFIRMED ──risk ok──▶ ROUTED ──fill──▶ FILLED   EXPIRED
        │                                              (zero broker interaction)
        └──risk breach──▶ REJECTED_RISK
```

The universal kill switch may terminate any non-terminal state at any time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Iterable, Optional


class SignalStatus(str, Enum):
    """Lifecycle of a single TradingView signal."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    CONFIRMED = "CONFIRMED"
    ROUTED = "ROUTED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    REJECTED_RISK = "REJECTED_RISK"
    EXPIRED = "EXPIRED"
    KILLED = "KILLED"


#: Terminal states — nothing may leave them.
TERMINAL_STATES: FrozenSet[SignalStatus] = frozenset(
    {
        SignalStatus.FILLED,
        SignalStatus.REJECTED,
        SignalStatus.REJECTED_RISK,
        SignalStatus.EXPIRED,
        SignalStatus.KILLED,
    }
)

#: The single source of truth for legal moves.
LEGAL_TRANSITIONS: Dict[SignalStatus, FrozenSet[SignalStatus]] = {
    SignalStatus.PENDING_APPROVAL: frozenset(
        {
            SignalStatus.CONFIRMED,       # trader tapped Confirm / Route
            SignalStatus.REJECTED,        # trader tapped Reject
            SignalStatus.REJECTED_RISK,   # risk engine vetoed at confirm time
            SignalStatus.EXPIRED,         # 60s TTL elapsed — zero broker calls
            SignalStatus.KILLED,          # global kill switch
        }
    ),
    SignalStatus.CONFIRMED: frozenset(
        {
            SignalStatus.ROUTED,          # risk ok → order signed + sent
            SignalStatus.REJECTED_RISK,   # risk breach on the re-check
            SignalStatus.REJECTED,        # broker refused the payload outright
            SignalStatus.KILLED,
        }
    ),
    SignalStatus.ROUTED: frozenset(
        {
            SignalStatus.FILLED,
            SignalStatus.REJECTED,        # venue rejected the working order
            SignalStatus.KILLED,          # flatten-all while working
        }
    ),
    SignalStatus.FILLED: frozenset(),
    SignalStatus.REJECTED: frozenset(),
    SignalStatus.REJECTED_RISK: frozenset(),
    SignalStatus.EXPIRED: frozenset(),
    SignalStatus.KILLED: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a caller attempts a move the contract forbids."""

    def __init__(self, current: SignalStatus, target: SignalStatus) -> None:
        self.current = current
        self.target = target
        allowed = sorted(s.value for s in LEGAL_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"illegal signal transition {current.value} -> {target.value}; "
            f"allowed from {current.value}: {allowed or ['<terminal>']}"
        )


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a successful :func:`transition`."""

    previous: SignalStatus
    current: SignalStatus
    reason: Optional[str] = None


def coerce(status: "SignalStatus | str") -> SignalStatus:
    """Accept either the enum or its string value."""
    if isinstance(status, SignalStatus):
        return status
    try:
        return SignalStatus(str(status).upper())
    except ValueError as exc:  # pragma: no cover - defensive
        raise IllegalTransition(SignalStatus.PENDING_APPROVAL, SignalStatus.REJECTED) from exc


def allowed_transitions(current: "SignalStatus | str") -> FrozenSet[SignalStatus]:
    return LEGAL_TRANSITIONS.get(coerce(current), frozenset())


def is_terminal(status: "SignalStatus | str") -> bool:
    return coerce(status) in TERMINAL_STATES


def can_transition(current: "SignalStatus | str", target: "SignalStatus | str") -> bool:
    return coerce(target) in allowed_transitions(current)


def transition(
    current: "SignalStatus | str",
    target: "SignalStatus | str",
    reason: Optional[str] = None,
) -> TransitionResult:
    """Move ``current`` → ``target``, raising :class:`IllegalTransition` if illegal."""
    src = coerce(current)
    dst = coerce(target)
    if dst not in LEGAL_TRANSITIONS.get(src, frozenset()):
        raise IllegalTransition(src, dst)
    return TransitionResult(previous=src, current=dst, reason=reason)


def replay(start: "SignalStatus | str", path: Iterable["SignalStatus | str"]) -> SignalStatus:
    """Apply a sequence of transitions; useful for audit-log verification."""
    state = coerce(start)
    for step in path:
        state = transition(state, step).current
    return state

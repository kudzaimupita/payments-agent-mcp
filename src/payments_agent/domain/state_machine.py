"""Transaction lifecycle as an explicit state machine.

Every status change in the service goes through `transition`, so an illegal
move (for example refunding a declined payment) fails loudly instead of
silently corrupting state.
"""

from __future__ import annotations

from .errors import DomainError
from .models import TxnStatus

S = TxnStatus

TRANSITIONS: dict[TxnStatus, frozenset[TxnStatus]] = {
    S.AUTHORIZED: frozenset({S.CAPTURED, S.VOIDED}),
    S.CAPTURED: frozenset({S.PARTIALLY_REFUNDED, S.REFUNDED, S.DISPUTED}),
    S.PARTIALLY_REFUNDED: frozenset({S.PARTIALLY_REFUNDED, S.REFUNDED, S.DISPUTED}),
    # A dispute is either resolved in the merchant's favour (back to captured)
    # or results in a chargeback.
    S.DISPUTED: frozenset({S.CAPTURED, S.CHARGED_BACK}),
    S.REFUNDED: frozenset(),
    S.VOIDED: frozenset(),
    S.DECLINED: frozenset(),
    S.CHARGED_BACK: frozenset(),
}

REFUNDABLE = frozenset({S.CAPTURED, S.PARTIALLY_REFUNDED})
DISPUTABLE = frozenset({S.CAPTURED, S.PARTIALLY_REFUNDED})
TERMINAL = frozenset(s for s, targets in TRANSITIONS.items() if not targets)


class InvalidTransitionError(DomainError):
    def __init__(self, current: TxnStatus, target: TxnStatus) -> None:
        super().__init__(
            "INVALID_TRANSITION",
            f"cannot move a transaction from {current.value} to {target.value}",
            current=current.value,
            target=target.value,
        )


def can_transition(current: TxnStatus, target: TxnStatus) -> bool:
    return target in TRANSITIONS[current]


def transition(current: TxnStatus, target: TxnStatus) -> TxnStatus:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
    return target


def status_after_refund(captured_minor: int, refunded_minor: int) -> TxnStatus:
    if refunded_minor <= 0 or refunded_minor > captured_minor:
        raise ValueError("refunded amount must be within (0, captured]")
    return S.REFUNDED if refunded_minor == captured_minor else S.PARTIALLY_REFUNDED

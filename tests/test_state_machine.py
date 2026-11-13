import pytest

from payments_agent.domain.models import TxnStatus as S
from payments_agent.domain.state_machine import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    status_after_refund,
    transition,
)


def test_every_status_has_a_transition_entry():
    assert set(TRANSITIONS) == set(S)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.AUTHORIZED, S.CAPTURED),
        (S.AUTHORIZED, S.VOIDED),
        (S.CAPTURED, S.PARTIALLY_REFUNDED),
        (S.CAPTURED, S.REFUNDED),
        (S.CAPTURED, S.DISPUTED),
        (S.PARTIALLY_REFUNDED, S.PARTIALLY_REFUNDED),
        (S.PARTIALLY_REFUNDED, S.REFUNDED),
        (S.DISPUTED, S.CHARGED_BACK),
        (S.DISPUTED, S.CAPTURED),
    ],
)
def test_allowed_transitions(current, target):
    assert transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.DECLINED, S.REFUNDED),
        (S.AUTHORIZED, S.REFUNDED),
        (S.REFUNDED, S.PARTIALLY_REFUNDED),
        (S.DISPUTED, S.REFUNDED),
        (S.CHARGED_BACK, S.CAPTURED),
        (S.VOIDED, S.CAPTURED),
    ],
)
def test_forbidden_transitions_raise(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError) as exc:
        transition(current, target)
    assert exc.value.code == "INVALID_TRANSITION"


def test_terminal_states():
    assert {S.REFUNDED, S.VOIDED, S.DECLINED, S.CHARGED_BACK} == TERMINAL


def test_status_after_refund():
    assert status_after_refund(1000, 400) is S.PARTIALLY_REFUNDED
    assert status_after_refund(1000, 1000) is S.REFUNDED
    with pytest.raises(ValueError):
        status_after_refund(1000, 1001)
    with pytest.raises(ValueError):
        status_after_refund(1000, 0)

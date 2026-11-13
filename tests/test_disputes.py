import pytest

from payments_agent.domain.errors import DomainError
from payments_agent.domain.models import DisputeRequest

AGENT = "agent:test"


def dispute(txn: str, reason: str = "goods_not_received", key: str = "dispute-0001"):
    return DisputeRequest(
        transaction_id=txn, reason=reason, note="customer report", idempotency_key=key
    )


def test_open_dispute_moves_transaction_to_disputed(service):
    out = service.open_dispute(dispute("txn_fx_gnr"), actor=AGENT)
    assert out["status"] == "open"
    txn = service.get_transaction("txn_fx_gnr")
    assert txn["status"] == "disputed"
    assert "refund" not in txn["allowed_actions"]
    assert "dispute" not in txn["allowed_actions"]


def test_open_dispute_is_idempotent(service):
    a = service.open_dispute(dispute("txn_fx_gnr"), actor=AGENT)
    b = service.open_dispute(dispute("txn_fx_gnr"), actor=AGENT)
    assert a["dispute_id"] == b["dispute_id"]
    assert b["idempotent_replay"] is True


@pytest.mark.parametrize(
    ("txn", "code"),
    [
        ("txn_fx_disputed", "DISPUTE_ALREADY_OPEN"),
        ("txn_fx_decl_51", "DISPUTE_NOT_ALLOWED"),
        ("txn_fx_auth", "DISPUTE_NOT_ALLOWED"),
        ("txn_fx_old", "DISPUTE_WINDOW_EXPIRED"),
    ],
)
def test_dispute_rejections(service, txn, code):
    with pytest.raises(DomainError) as exc:
        service.open_dispute(dispute(txn), actor=AGENT)
    assert exc.value.code == code


def test_invalid_reason_is_rejected_by_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        dispute("txn_fx_gnr", reason="because")

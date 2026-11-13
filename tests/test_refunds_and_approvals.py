import sqlite3
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from payments_agent.domain.approvals import Signer
from payments_agent.domain.errors import DomainError
from payments_agent.domain.service import PaymentsService
from payments_agent.runtime import open_service

from .conftest import refund

AGENT = "agent:test"


def count(service: PaymentsService, sql: str, *args) -> int:
    return service.conn.execute(sql, args).fetchone()[0]


def test_refund_request_is_pending_and_moves_no_money(service):
    result = service.issue_refund(refund("txn_fx_coffee_ok", 200), actor=AGENT)
    assert result["status"] == "pending_approval"
    assert result["approval_id"].startswith("apr_")
    txn = service.get_transaction("txn_fx_coffee_ok")
    assert txn["status"] == "captured"
    assert txn["refunded_minor"] == 0
    assert txn["pending_refund_minor"] == 200
    assert txn["refundable_minor"] == 250


def test_idempotent_replay_returns_same_refund(service):
    first = service.issue_refund(refund("txn_fx_coffee_ok", 200, key="same-key-1"), actor=AGENT)
    second = service.issue_refund(refund("txn_fx_coffee_ok", 200, key="same-key-1"), actor=AGENT)
    assert second["refund_id"] == first["refund_id"]
    assert second["idempotent_replay"] is True
    assert count(service, "SELECT COUNT(*) FROM refunds") == 1


def test_idempotency_key_reuse_with_different_arguments_is_rejected(service):
    service.issue_refund(refund("txn_fx_coffee_ok", 200, key="same-key-2"), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.issue_refund(refund("txn_fx_coffee_ok", 300, key="same-key-2"), actor=AGENT)
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert count(service, "SELECT COUNT(*) FROM refunds") == 1


def test_pending_refunds_reserve_the_refundable_amount(service):
    service.issue_refund(refund("txn_fx_coffee_ok", 300, key="reserve-1"), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.issue_refund(refund("txn_fx_coffee_ok", 200, key="reserve-2"), actor=AGENT)
    assert exc.value.code == "REFUND_EXCEEDS_REFUNDABLE"
    assert exc.value.details["refundable_minor"] == 150


@pytest.mark.parametrize(
    ("txn", "amount", "code"),
    [
        ("txn_fx_decl_51", 100, "REFUND_NOT_ALLOWED"),
        ("txn_fx_auth", 100, "REFUND_NOT_ALLOWED"),
        ("txn_fx_refunded", 100, "REFUND_NOT_ALLOWED"),
        ("txn_fx_disputed", 100, "REFUND_NOT_ALLOWED"),
        ("txn_fx_partial", 5001, "REFUND_EXCEEDS_REFUNDABLE"),
        ("txn_fx_big", 150_000, "REFUND_EXCEEDS_MAX"),
        ("txn_missing", 100, "NOT_FOUND"),
    ],
)
def test_refund_rejections(service, txn, amount, code):
    with pytest.raises(DomainError) as exc:
        service.issue_refund(refund(txn, amount), actor=AGENT)
    assert exc.value.code == code
    assert count(service, "SELECT COUNT(*) FROM refunds") == 0


def test_daily_limit_per_customer(settings):
    service = open_service(replace(settings, daily_refund_limit_minor=1000))
    service.issue_refund(refund("txn_fx_eve_small", 600, key="daily-001"), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.issue_refund(refund("txn_fx_fraud", 500, key="daily-002"), actor=AGENT)
    assert exc.value.code == "DAILY_REFUND_LIMIT"


def test_rejected_writes_are_audited(service):
    with pytest.raises(DomainError):
        service.issue_refund(refund("txn_fx_decl_51", 100), actor=AGENT)
    entry = service.get_audit_log(limit=1)["entries"][0]
    assert entry["action"] == "refund.request"
    assert entry["outcome"] == "rejected"
    assert entry["details"]["error"]["code"] == "REFUND_NOT_ALLOWED"


def test_human_approval_executes_refund(service, human):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 450), actor=AGENT)
    out = service.approve_refund(req["approval_id"], approver="human:alex", signer=human)
    assert out["status"] == "executed"
    txn = service.get_transaction("txn_fx_coffee_ok")
    assert txn["status"] == "refunded"
    assert txn["refunded_minor"] == 450
    assert txn["pending_refund_minor"] == 0
    assert service.list_pending_approvals()["count"] == 0


def test_partial_approval_moves_to_partially_refunded(service, human):
    req = service.issue_refund(refund("txn_fx_partial", 1000), actor=AGENT)
    service.approve_refund(req["approval_id"], approver="human:alex", signer=human)
    txn = service.get_transaction("txn_fx_partial")
    assert (txn["status"], txn["refunded_minor"]) == ("partially_refunded", 4000)


def test_approval_cannot_be_used_twice(service, human):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor=AGENT)
    service.approve_refund(req["approval_id"], approver="human:alex", signer=human)
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="human:alex", signer=human)
    assert exc.value.code == "APPROVAL_NOT_PENDING"


def test_human_key_requires_a_human_approver(service, human):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="agent:mcp", signer=human)
    assert exc.value.code == "APPROVER_INVALID"


def test_requester_cannot_approve_own_refund(service, human):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor="human:sam")
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="human:sam", signer=human)
    assert exc.value.code == "SEPARATION_OF_DUTIES"


def test_unknown_signing_key_is_rejected(service):
    forged = Signer(role="human", key=Ed25519PrivateKey.generate())
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="human:mallory", signer=forged)
    assert exc.value.code == "APPROVAL_TOKEN_INVALID"
    assert service.get_transaction("txn_fx_coffee_ok")["refunded_minor"] == 0


def test_policy_key_cannot_approve_above_limit(settings):
    service = open_service(settings)  # auto-approve limit is 0 by default
    policy = Signer.load(settings.keys_dir, "policy")
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor=AGENT)
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="policy:auto", signer=policy)
    assert exc.value.code == "APPROVAL_TOKEN_INSUFFICIENT"


def test_service_never_holds_the_human_key(settings, human):
    from payments_agent.domain.approvals import Verifier

    with pytest.raises(ValueError):
        PaymentsService(
            open_service(settings).conn,
            settings,
            Verifier.load(settings.keys_dir),
            policy_signer=human,
        )


def test_auto_approve_limit_executes_small_refunds_only(settings):
    service = open_service(replace(settings, auto_approve_limit_minor=300))
    small = service.issue_refund(refund("txn_fx_coffee_ok", 300, key="auto-0001"), actor=AGENT)
    assert small["status"] == "executed"
    assert small["approved_by"] == "policy:auto"
    large = service.issue_refund(refund("txn_fx_rail", 301, key="auto-0002"), actor=AGENT)
    assert large["status"] == "pending_approval"


def test_database_refuses_execution_without_approval(service):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 100), actor=AGENT)
    with pytest.raises(sqlite3.IntegrityError, match="approved, signed approval"):
        service.conn.execute(
            "UPDATE refunds SET status = 'executed' WHERE id = ?", (req["refund_id"],)
        )
    with pytest.raises(sqlite3.IntegrityError, match="executed state"):
        service.conn.execute(
            "INSERT INTO refunds (id, transaction_id, amount_minor, currency, reason, status,"
            " idempotency_key, requested_by, requested_at) VALUES"
            " ('rf_x', 'txn_fx_rail', 10, 'GBP', 'x', 'executed', 'k', 'x', 'now')"
        )


def test_dispute_opened_after_request_blocks_execution(service, human):
    from payments_agent.domain.models import DisputeRequest

    req = service.issue_refund(refund("txn_fx_fraud", 1000), actor=AGENT)
    service.open_dispute(
        DisputeRequest(
            transaction_id="txn_fx_fraud",
            reason="fraud",
            note="not recognised",
            idempotency_key="dsp-00001",
        ),
        actor=AGENT,
    )
    with pytest.raises(DomainError) as exc:
        service.approve_refund(req["approval_id"], approver="human:alex", signer=human)
    assert exc.value.code == "REFUND_NOT_ALLOWED"
    assert service.get_transaction("txn_fx_fraud")["refunded_minor"] == 0


def test_reject_releases_reserved_amount(service):
    req = service.issue_refund(refund("txn_fx_coffee_ok", 450), actor=AGENT)
    service.reject_refund(req["approval_id"], approver="human:alex", reason="not eligible")
    assert service.get_transaction("txn_fx_coffee_ok")["refundable_minor"] == 450

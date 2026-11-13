import sqlite3

import pytest

from payments_agent.domain import audit

from .conftest import refund


def test_audit_log_is_append_only(service):
    service.issue_refund(refund("txn_fx_coffee_ok", 100), actor="agent:test")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        service.conn.execute("UPDATE audit_log SET outcome = 'ok'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        service.conn.execute("DELETE FROM audit_log")


def test_every_write_is_audited(service):
    service.issue_refund(refund("txn_fx_coffee_ok", 100), actor="agent:test")
    service.issue_refund(refund("txn_fx_coffee_ok", 100), actor="agent:test")  # replay
    outcomes = [e["outcome"] for e in service.get_audit_log(limit=10)["entries"]]
    assert outcomes == ["replayed", "accepted"]


def test_hash_chain_detects_tampering(service):
    for i in range(3):
        service.issue_refund(
            refund("txn_fx_rail", 100 + i, key=f"chain-key-{i}"), actor="agent:test"
        )
    assert audit.verify_chain(service.conn) == (True, None)
    # Simulate an attacker with raw file access who removes the trigger first.
    service.conn.execute("DROP TRIGGER audit_log_no_update")
    service.conn.execute("UPDATE audit_log SET actor = 'human:someone' WHERE seq = 2")
    assert audit.verify_chain(service.conn) == (False, 2)


def test_audit_details_are_redacted(service):
    service.issue_refund(
        refund(
            "txn_fx_coffee_ok",
            100,
            reason="customer jo@example.com called from 07700 900555 card 4242424242424242",
        ),
        actor="agent:test",
    )
    raw = service.conn.execute(
        "SELECT details_json FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0]
    assert "jo@example.com" not in raw
    assert "4242424242424242" not in raw
    assert "900555" not in raw
    assert "[REDACTED_EMAIL]" in raw


def test_customer_reads_are_audited(service):
    service.get_customer("cus_fx_ada", actor="agent:test")
    entry = service.get_audit_log(limit=1)["entries"][0]
    assert (entry["action"], entry["target"]) == ("customer.viewed", "cus_fx_ada")

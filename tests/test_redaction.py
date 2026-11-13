import pytest

from payments_agent.guardrails.redaction import (
    find_pii,
    mask_email,
    mask_phone,
    redact,
    redact_text,
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("mail me at ada.okafor@example.com", "email"),
        ("card 4242 4242 4242 4242 exp 12/30", "pan"),
        ("card 4000-0000-0000-0002", "pan"),
        ("amex 378282246310005", "pan"),
        ("call 07700 900101 today", "phone"),
        ("call +44 7700 900101", "phone"),
    ],
)
def test_detects_and_redacts_pii(text, kind):
    assert kind in find_pii(text)
    assert not find_pii(redact_text(text))


@pytest.mark.parametrize(
    "text",
    [
        "txn_fx_coffee_ok refunded 1250 minor units",
        "amount_minor 150000 on 2026-11-01T12:00:00+00:00",
        "card bin 424242 last4 4242",
        "approval apr_1a2b3c4d5e6f",
    ],
)
def test_leaves_non_pii_alone(text):
    assert redact_text(text) == text


def test_redact_walks_nested_structures():
    data = {"a": ["x@example.org", {"b": "07700 900123"}], "n": 5}
    assert redact(data) == {"a": ["[REDACTED_EMAIL]", {"b": "[REDACTED_PHONE]"}], "n": 5}


def test_masks():
    assert mask_email("ada.okafor@example.com") == "a***@example.com"
    assert mask_phone("07700 900101") == "***0101"


def test_seed_never_stores_full_card_numbers(service):
    for row in service.conn.execute("SELECT * FROM cards"):
        assert len(row["bin"]) == 6 and len(row["last4"]) == 4
        assert all(not find_pii(str(v)) for v in row)

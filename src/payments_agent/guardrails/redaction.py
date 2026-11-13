"""PII redaction for logs, audit details and agent output.

Patterns are deliberately conservative: any 13-19 digit run (with optional
spaces or dashes) is treated as a possible card number whether or not it
passes the Luhn check.
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# 13-19 digits, optionally separated by single spaces or dashes.
PAN_RE = re.compile(r"(?<![\w])(?:\d[ -]?){12,18}\d(?![\w])")
# International or UK-style phone numbers: + or 0 prefix followed by 9-14 digits.
PHONE_RE = re.compile(r"(?<![\w])(?:\+\d{1,3}[ -]?|0)(?:\d[ -]?){8,13}\d(?![\w])")

EMAIL_PLACEHOLDER = "[REDACTED_EMAIL]"
PAN_PLACEHOLDER = "[REDACTED_PAN]"
PHONE_PLACEHOLDER = "[REDACTED_PHONE]"


def redact_text(text: str) -> str:
    text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    text = PAN_RE.sub(PAN_PLACEHOLDER, text)
    text = PHONE_RE.sub(PHONE_PLACEHOLDER, text)
    return text


def redact(value: Any) -> Any:
    """Recursively redact strings inside dicts, lists and tuples."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def find_pii(text: str) -> list[str]:
    """Return the kinds of PII present in text (used by evals and tests)."""
    kinds = []
    if EMAIL_RE.search(text):
        kinds.append("email")
    if PAN_RE.search(text):
        kinds.append("pan")
    if PHONE_RE.search(text):
        kinds.append("phone")
    return kinds


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"

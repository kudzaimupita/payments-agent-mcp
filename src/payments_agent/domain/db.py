"""SQLite schema and connection handling.

Safety invariants are enforced in the database as well as in Python:
the audit log rejects UPDATE and DELETE, and a refund row can only reach
`executed` when it points at an approval that is `approved` and carries a
signed token.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL,
    phone      TEXT NOT NULL,
    country    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    token       TEXT NOT NULL UNIQUE,
    brand       TEXT NOT NULL,
    bin         TEXT NOT NULL CHECK (length(bin) = 6),
    last4       TEXT NOT NULL CHECK (length(last4) = 4),
    exp_month   INTEGER NOT NULL,
    exp_year    INTEGER NOT NULL,
    status      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchants (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    descriptor TEXT NOT NULL,
    mcc        TEXT NOT NULL,
    country    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id             TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL REFERENCES customers(id),
    card_id        TEXT NOT NULL REFERENCES cards(id),
    merchant_id    TEXT NOT NULL REFERENCES merchants(id),
    amount_minor   INTEGER NOT NULL CHECK (amount_minor > 0),
    currency       TEXT NOT NULL,
    status         TEXT NOT NULL,
    decline_code   TEXT,
    created_at     TEXT NOT NULL,
    captured_at    TEXT,
    refunded_minor INTEGER NOT NULL DEFAULT 0 CHECK (refunded_minor >= 0),
    CHECK (refunded_minor <= amount_minor)
);
CREATE INDEX IF NOT EXISTS ix_txn_customer ON transactions(customer_id, created_at);

CREATE TABLE IF NOT EXISTS transaction_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    at             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS disputes (
    id             TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL,
    amount_minor   INTEGER NOT NULL,
    note           TEXT NOT NULL,
    opened_at      TEXT NOT NULL,
    opened_by      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind = 'refund'),
    refund_id      TEXT NOT NULL UNIQUE,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    amount_minor   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    status         TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    requested_at   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    decided_by     TEXT,
    decided_at     TEXT,
    payload        TEXT,
    token          TEXT
);

CREATE TABLE IF NOT EXISTS refunds (
    id              TEXT PRIMARY KEY,
    transaction_id  TEXT NOT NULL REFERENCES transactions(id),
    amount_minor    INTEGER NOT NULL CHECK (amount_minor > 0),
    currency        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    requested_by    TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    approval_id     TEXT REFERENCES approvals(id),
    executed_at     TEXT
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    operation     TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (operation, key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    details_json TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS refunds_insert_not_executed
BEFORE INSERT ON refunds
WHEN NEW.status = 'executed'
BEGIN
    SELECT RAISE(ABORT, 'a refund cannot be created in the executed state');
END;

CREATE TRIGGER IF NOT EXISTS refunds_execute_requires_approval
BEFORE UPDATE OF status ON refunds
WHEN NEW.status = 'executed' AND NOT EXISTS (
    SELECT 1 FROM approvals a
    WHERE a.id = NEW.approval_id
      AND a.refund_id = NEW.id
      AND a.status = 'approved'
      AND a.token IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'refund execution requires an approved, signed approval');
END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)

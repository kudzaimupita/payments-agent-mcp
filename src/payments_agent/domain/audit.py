"""Append-only, hash-chained audit log.

Each entry stores the hash of the previous entry, so an edit made outside
the application (the table itself rejects UPDATE/DELETE) is detectable with
`verify_chain`. Details are passed through PII redaction before storage.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..guardrails.redaction import redact

GENESIS = "0" * 64


def _digest(
    prev_hash: str, at: str, actor: str, action: str, target: str, outcome: str, details_json: str
) -> str:
    material = "\x1f".join([prev_hash, at, actor, action, target, outcome, details_json])
    return hashlib.sha256(material.encode()).hexdigest()


def append(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target: str,
    outcome: str,
    details: dict[str, Any] | None = None,
) -> int:
    """Append one entry. Must be called inside the caller's transaction."""
    at = datetime.now(UTC).isoformat(timespec="microseconds")
    details_json = json.dumps(redact(details or {}), sort_keys=True, default=str)
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS
    digest = _digest(prev_hash, at, actor, action, target, outcome, details_json)
    cur = conn.execute(
        "INSERT INTO audit_log (at, actor, action, target, outcome, details_json, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (at, actor, action, target, outcome, details_json, prev_hash, digest),
    )
    return int(cur.lastrowid or 0)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int | None]:
    """Return (ok, first_bad_seq)."""
    prev = GENESIS
    for row in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        expected = _digest(
            prev,
            row["at"],
            row["actor"],
            row["action"],
            row["target"],
            row["outcome"],
            row["details_json"],
        )
        if row["prev_hash"] != prev or row["hash"] != expected:
            return False, int(row["seq"])
        prev = row["hash"]
    return True, None


def entries(
    conn: sqlite3.Connection, *, limit: int = 50, target: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT seq, at, actor, action, target, outcome, details_json FROM audit_log"
    params: list[Any] = []
    if target:
        sql += " WHERE target = ?"
        params.append(target)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        out.append(item)
    return out

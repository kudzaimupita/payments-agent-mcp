"""Payments sandbox service: the only code that reads or changes sandbox state.

The MCP server is a thin layer over this class. Read methods never write
(apart from access audit entries); write methods are idempotent, validated,
audited, and can at most create a refund in `pending_approval`. Executing a
refund requires `approve_refund`, which needs a signer holding an approval
key. The human key is only loaded by the operator CLI.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..guardrails.injection import scan_fields
from ..guardrails.redaction import mask_email, mask_phone
from ..money import format_minor
from . import audit
from .approvals import Signer, Verifier, canonical_payload
from .decline_codes import DECLINE_CODES, explain
from .errors import DomainError, NotFoundError
from .models import (
    ApprovalStatus,
    DisputeRequest,
    DisputeStatus,
    RefundRequest,
    RefundStatus,
    TxnStatus,
)
from .state_machine import DISPUTABLE, REFUNDABLE, status_after_refund, transition

Json = dict[str, Any]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _request_hash(payload: Json) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class PaymentsService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        verifier: Verifier,
        policy_signer: Signer | None = None,
    ) -> None:
        if policy_signer is not None and policy_signer.role != "policy":
            raise ValueError("the service may only hold the policy signer")
        self.conn = conn
        self.settings = settings
        self.verifier = verifier
        self.policy_signer = policy_signer

    # ------------------------------------------------------------------ helpers

    def now(self) -> datetime:
        return self.settings.now

    def _now_iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _txn_row(self, transaction_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT t.*, m.name AS merchant_name, m.descriptor AS merchant_descriptor,"
            " c.brand AS card_brand, c.bin AS card_bin, c.last4 AS card_last4"
            " FROM transactions t JOIN merchants m ON m.id = t.merchant_id"
            " JOIN cards c ON c.id = t.card_id WHERE t.id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("transaction", transaction_id)
        return row

    def _pending_refund_minor(self, transaction_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_minor), 0) FROM refunds"
            " WHERE transaction_id = ? AND status = ?",
            (transaction_id, RefundStatus.PENDING_APPROVAL.value),
        ).fetchone()
        return int(row[0])

    def _open_dispute(self, transaction_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM disputes WHERE transaction_id = ? AND status = ?",
            (transaction_id, DisputeStatus.OPEN.value),
        ).fetchone()
        return row

    def _age_days(self, iso: str) -> int:
        return (self.now() - datetime.fromisoformat(iso)).days

    def _txn_view(self, row: sqlite3.Row, *, detail: bool) -> Json:
        status = TxnStatus(row["status"])
        pending = self._pending_refund_minor(row["id"])
        refundable = (
            row["amount_minor"] - row["refunded_minor"] - pending if status in REFUNDABLE else 0
        )
        cur = row["currency"]
        view: Json = {
            "id": row["id"],
            "status": status.value,
            "amount": format_minor(row["amount_minor"], cur),
            "amount_minor": row["amount_minor"],
            "currency": cur,
            "created_at": row["created_at"],
            "age_days": self._age_days(row["created_at"]),
            "customer_id": row["customer_id"],
            "merchant": {
                "id": row["merchant_id"],
                "name": row["merchant_name"],
                "descriptor": row["merchant_descriptor"],
            },
            "card": {
                "brand": row["card_brand"],
                "bin": row["card_bin"],
                "last4": row["card_last4"],
            },
            "decline_code": row["decline_code"],
            "refunded_minor": row["refunded_minor"],
            "pending_refund_minor": pending,
            "refundable_minor": refundable,
        }
        untrusted: dict[str, str | None] = {"merchant.descriptor": row["merchant_descriptor"]}
        if detail:
            view["captured_at"] = row["captured_at"]
            disputes = [
                {
                    "id": d["id"],
                    "reason": d["reason"],
                    "status": d["status"],
                    "opened_at": d["opened_at"],
                    "note": d["note"],
                }
                for d in self.conn.execute(
                    "SELECT * FROM disputes WHERE transaction_id = ? ORDER BY opened_at",
                    (row["id"],),
                )
            ]
            for i, d in enumerate(disputes):
                untrusted[f"disputes[{i}].note"] = d["note"]
            view["disputes"] = disputes
            view["refunds"] = [
                {
                    "id": r["id"],
                    "amount_minor": r["amount_minor"],
                    "status": r["status"],
                    "requested_at": r["requested_at"],
                    "approval_id": r["approval_id"],
                }
                for r in self.conn.execute(
                    "SELECT * FROM refunds WHERE transaction_id = ? ORDER BY requested_at",
                    (row["id"],),
                )
            ]
            actions = []
            if refundable > 0:
                actions.append("refund")
            if (
                status in DISPUTABLE
                and self._open_dispute(row["id"]) is None
                and view["age_days"] <= self.settings.dispute_window_days
            ):
                actions.append("dispute")
            if status is TxnStatus.DECLINED:
                actions.append("explain_decline")
            view["allowed_actions"] = actions
        findings = scan_fields(untrusted)
        if findings:
            view["injection_warnings"] = [f.to_dict() for f in findings]
        return view

    def _idempotent(self, operation: str, key: str, request: Json, run: Callable[[], Json]) -> Json:
        """Run `run` once per (operation, key). Caller must hold a transaction."""
        req_hash = _request_hash(request)
        row = self.conn.execute(
            "SELECT request_hash, response_json FROM idempotency_keys"
            " WHERE operation = ? AND key = ?",
            (operation, key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != req_hash:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT",
                    "this idempotency key was already used with different arguments",
                    idempotency_key=key,
                )
            replay: Json = json.loads(row["response_json"])
            replay["idempotent_replay"] = True
            return replay
        response = run()
        self.conn.execute(
            "INSERT INTO idempotency_keys (operation, key, request_hash, response_json,"
            " created_at) VALUES (?,?,?,?,?)",
            (operation, key, req_hash, json.dumps(response), self._now_iso()),
        )
        return response

    def _audited_write(
        self, actor: str, action: str, target: str, request: Json, body: Callable[[], Json]
    ) -> Json:
        """Run a write in a transaction and audit the outcome, including rejections."""
        try:
            with self._tx():
                result = body()
                outcome = "replayed" if result.get("idempotent_replay") else "accepted"
                audit.append(
                    self.conn,
                    actor=actor,
                    action=action,
                    target=target,
                    outcome=outcome,
                    details={"request": request, "result": result},
                )
            return result
        except DomainError as err:
            with self._tx():
                audit.append(
                    self.conn,
                    actor=actor,
                    action=action,
                    target=target,
                    outcome="rejected",
                    details={"request": request, "error": err.to_dict()},
                )
            raise

    # -------------------------------------------------------------------- reads

    def search_transactions(
        self,
        *,
        customer_id: str | None = None,
        merchant_query: str | None = None,
        status: TxnStatus | None = None,
        min_amount_minor: int | None = None,
        max_amount_minor: int | None = None,
        limit: int = 20,
    ) -> Json:
        if not 1 <= limit <= 50:
            raise DomainError("INVALID_ARGUMENT", "limit must be between 1 and 50")
        clauses: list[str] = []
        params: list[Any] = []
        if customer_id:
            clauses.append("t.customer_id = ?")
            params.append(customer_id)
        if merchant_query:
            clauses.append("(m.name LIKE ? OR m.descriptor LIKE ?)")
            like = f"%{merchant_query}%"
            params += [like, like]
        if status:
            clauses.append("t.status = ?")
            params.append(TxnStatus(status).value)
        if min_amount_minor is not None:
            clauses.append("t.amount_minor >= ?")
            params.append(min_amount_minor)
        if max_amount_minor is not None:
            clauses.append("t.amount_minor <= ?")
            params.append(max_amount_minor)
        if not clauses:
            raise DomainError("INVALID_ARGUMENT", "provide at least one filter")
        where = " AND ".join(clauses)
        ids = [
            r["id"]
            for r in self.conn.execute(
                f"SELECT t.id FROM transactions t JOIN merchants m ON m.id = t.merchant_id"
                f" WHERE {where} ORDER BY t.created_at DESC LIMIT ?",
                [*params, limit],
            )
        ]
        txns = [self._txn_view(self._txn_row(i), detail=False) for i in ids]
        result: Json = {"count": len(txns), "transactions": txns}
        warnings = [
            {"transaction_id": t["id"], **w} for t in txns for w in t.pop("injection_warnings", [])
        ]
        if warnings:
            result["injection_warnings"] = warnings
        return result

    def get_transaction(self, transaction_id: str) -> Json:
        return self._txn_view(self._txn_row(transaction_id), detail=True)

    def get_customer(self, customer_id: str, *, actor: str) -> Json:
        row = self.conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise NotFoundError("customer", customer_id)
        cards = [
            {
                "id": c["id"],
                "brand": c["brand"],
                "bin": c["bin"],
                "last4": c["last4"],
                "expiry": f"{c['exp_month']:02d}/{c['exp_year']}",
                "status": c["status"],
            }
            for c in self.conn.execute("SELECT * FROM cards WHERE customer_id = ?", (row["id"],))
        ]
        count = self.conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE customer_id = ?", (row["id"],)
        ).fetchone()[0]
        view: Json = {
            "id": row["id"],
            "name": row["name"],
            "email": mask_email(row["email"]),
            "phone": mask_phone(row["phone"]),
            "country": row["country"],
            "customer_since": row["created_at"][:10],
            "cards": cards,
            "transaction_count": count,
            "notes": row["notes"],
        }
        findings = scan_fields({"notes": row["notes"]})
        if findings:
            view["injection_warnings"] = [f.to_dict() for f in findings]
        with self._tx():
            audit.append(
                self.conn, actor=actor, action="customer.viewed", target=row["id"], outcome="ok"
            )
        return view

    def explain_decline(self, code: str) -> Json:
        exp = explain(code)
        if exp is None:
            raise DomainError(
                "UNKNOWN_DECLINE_CODE",
                f"decline code {code!r} is not in the catalogue",
                known_codes=sorted(DECLINE_CODES),
            )
        return exp.to_dict()

    def list_pending_approvals(self, limit: int = 20) -> Json:
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE status = ? ORDER BY requested_at LIMIT ?",
            (ApprovalStatus.PENDING.value, limit),
        ).fetchall()
        items = [
            {
                "approval_id": r["id"],
                "refund_id": r["refund_id"],
                "transaction_id": r["transaction_id"],
                "amount_minor": r["amount_minor"],
                "amount": format_minor(r["amount_minor"], r["currency"]),
                "requested_by": r["requested_by"],
                "requested_at": r["requested_at"],
                "reason": r["reason"],
            }
            for r in rows
        ]
        return {"count": len(items), "approvals": items}

    def get_audit_log(self, *, limit: int = 20, target: str | None = None) -> Json:
        if not 1 <= limit <= 200:
            raise DomainError("INVALID_ARGUMENT", "limit must be between 1 and 200")
        ok, bad = audit.verify_chain(self.conn)
        return {
            "chain_valid": ok,
            "first_invalid_seq": bad,
            "entries": audit.entries(self.conn, limit=limit, target=target),
        }

    # ------------------------------------------------------------------- writes

    def open_dispute(self, req: DisputeRequest, *, actor: str) -> Json:
        request = req.model_dump(mode="json")

        def body() -> Json:
            def run() -> Json:
                row = self._txn_row(req.transaction_id)
                status = TxnStatus(row["status"])
                existing = self._open_dispute(row["id"])
                if existing is not None:
                    raise DomainError(
                        "DISPUTE_ALREADY_OPEN",
                        f"dispute {existing['id']} is already open for this transaction",
                        dispute_id=existing["id"],
                    )
                if status not in DISPUTABLE:
                    raise DomainError(
                        "DISPUTE_NOT_ALLOWED",
                        f"a {status.value} transaction cannot be disputed",
                        status=status.value,
                    )
                age = self._age_days(row["created_at"])
                if age > self.settings.dispute_window_days:
                    raise DomainError(
                        "DISPUTE_WINDOW_EXPIRED",
                        f"transaction is {age} days old; disputes must be opened within "
                        f"{self.settings.dispute_window_days} days",
                        age_days=age,
                    )
                dispute_id = _new_id("dsp")
                now = self._now_iso()
                amount = row["amount_minor"] - row["refunded_minor"]
                self.conn.execute(
                    "INSERT INTO disputes (id, transaction_id, reason, status, amount_minor,"
                    " note, opened_at, opened_by) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        dispute_id,
                        row["id"],
                        req.reason.value,
                        DisputeStatus.OPEN.value,
                        amount,
                        req.note,
                        now,
                        actor,
                    ),
                )
                new_status = transition(status, TxnStatus.DISPUTED)
                self._set_status(row["id"], status, new_status, actor, f"dispute {dispute_id}")
                return {
                    "ok": True,
                    "dispute_id": dispute_id,
                    "transaction_id": row["id"],
                    "reason": req.reason.value,
                    "status": DisputeStatus.OPEN.value,
                    "amount": format_minor(amount, row["currency"]),
                    "message": "Dispute opened. Refunds on this transaction are blocked while"
                    " the dispute is open.",
                }

            return self._idempotent("open_dispute", req.idempotency_key, request, run)

        return self._audited_write(actor, "dispute.open", req.transaction_id, request, body)

    def issue_refund(self, req: RefundRequest, *, actor: str) -> Json:
        request = req.model_dump(mode="json")

        def body() -> Json:
            def run() -> Json:
                row = self._txn_row(req.transaction_id)
                status = TxnStatus(row["status"])
                if status not in REFUNDABLE:
                    hint = (
                        " Resolve the open dispute first." if status is TxnStatus.DISPUTED else ""
                    )
                    raise DomainError(
                        "REFUND_NOT_ALLOWED",
                        f"a {status.value} transaction cannot be refunded.{hint}",
                        status=status.value,
                    )
                refundable = (
                    row["amount_minor"]
                    - row["refunded_minor"]
                    - self._pending_refund_minor(row["id"])
                )
                if req.amount_minor > refundable:
                    raise DomainError(
                        "REFUND_EXCEEDS_REFUNDABLE",
                        f"requested {format_minor(req.amount_minor, row['currency'])} but only "
                        f"{format_minor(max(refundable, 0), row['currency'])} can be refunded "
                        "(captured minus refunded minus pending)",
                        refundable_minor=max(refundable, 0),
                    )
                if req.amount_minor > self.settings.max_refund_minor:
                    raise DomainError(
                        "REFUND_EXCEEDS_MAX",
                        "amount is above the single-refund ceiling of "
                        f"{format_minor(self.settings.max_refund_minor, row['currency'])};"
                        " escalate to a human operator",
                        max_refund_minor=self.settings.max_refund_minor,
                    )
                since = (self.now() - timedelta(days=1)).isoformat(timespec="seconds")
                used = self.conn.execute(
                    "SELECT COALESCE(SUM(r.amount_minor), 0) FROM refunds r"
                    " JOIN transactions t ON t.id = r.transaction_id"
                    " WHERE t.customer_id = ? AND r.status IN (?, ?) AND r.requested_at >= ?",
                    (
                        row["customer_id"],
                        RefundStatus.PENDING_APPROVAL.value,
                        RefundStatus.EXECUTED.value,
                        since,
                    ),
                ).fetchone()[0]
                if used + req.amount_minor > self.settings.daily_refund_limit_minor:
                    raise DomainError(
                        "DAILY_REFUND_LIMIT",
                        "this refund would exceed the daily refund limit for the customer",
                        already_requested_minor=used,
                    )
                refund_id, approval_id, now = _new_id("rf"), _new_id("apr"), self._now_iso()
                self.conn.execute(
                    "INSERT INTO approvals (id, kind, refund_id, transaction_id, amount_minor,"
                    " currency, status, requested_by, requested_at, reason)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        approval_id,
                        "refund",
                        refund_id,
                        row["id"],
                        req.amount_minor,
                        row["currency"],
                        ApprovalStatus.PENDING.value,
                        actor,
                        now,
                        req.reason,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO refunds (id, transaction_id, amount_minor, currency, reason,"
                    " status, idempotency_key, requested_by, requested_at, approval_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        refund_id,
                        row["id"],
                        req.amount_minor,
                        row["currency"],
                        req.reason,
                        RefundStatus.PENDING_APPROVAL.value,
                        req.idempotency_key,
                        actor,
                        now,
                        approval_id,
                    ),
                )
                return {
                    "ok": True,
                    "refund_id": refund_id,
                    "approval_id": approval_id,
                    "transaction_id": row["id"],
                    "amount": format_minor(req.amount_minor, row["currency"]),
                    "amount_minor": req.amount_minor,
                    "status": RefundStatus.PENDING_APPROVAL.value,
                    "message": "Refund recorded and waiting for human approval. No money has"
                    " moved.",
                }

            return self._idempotent("issue_refund", req.idempotency_key, request, run)

        result = self._audited_write(actor, "refund.request", req.transaction_id, request, body)
        if (
            result.get("status") == RefundStatus.PENDING_APPROVAL.value
            and not result.get("idempotent_replay")
            and self.policy_signer is not None
            and req.amount_minor <= self.settings.auto_approve_limit_minor
        ):
            executed = self.approve_refund(
                result["approval_id"], approver="policy:auto", signer=self.policy_signer
            )
            result = {
                **result,
                "status": executed["status"],
                "approved_by": "policy:auto",
                "message": "Refund was within the auto-approve limit and has executed.",
            }
        return result

    # ------------------------------------------------------- approval (human only)

    def approve_refund(self, approval_id: str, *, approver: str, signer: Signer) -> Json:
        """Approve and execute a pending refund. Not exposed through MCP."""
        if signer.role == "human" and not approver.startswith("human:"):
            raise DomainError("APPROVER_INVALID", "human approvals must name a human approver")
        with self._tx():
            appr = self.conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if appr is None:
                raise NotFoundError("approval", approval_id)
            if appr["status"] != ApprovalStatus.PENDING.value:
                raise DomainError("APPROVAL_NOT_PENDING", f"approval is {appr['status']}")
            if appr["requested_by"] == approver:
                raise DomainError(
                    "SEPARATION_OF_DUTIES", "the requester cannot approve their own refund"
                )
            payload = canonical_payload(
                {
                    "approval_id": appr["id"],
                    "refund_id": appr["refund_id"],
                    "transaction_id": appr["transaction_id"],
                    "amount_minor": appr["amount_minor"],
                    "currency": appr["currency"],
                    "approver": approver,
                    "decided_at": self._now_iso(),
                }
            )
            token = signer.sign(payload)
            self._execute_refund(appr, payload, token, approver)
            audit.append(
                self.conn,
                actor=approver,
                action="refund.approve",
                target=appr["id"],
                outcome="executed",
                details={
                    "refund_id": appr["refund_id"],
                    "transaction_id": appr["transaction_id"],
                    "amount_minor": appr["amount_minor"],
                },
            )
        return {
            "approval_id": approval_id,
            "refund_id": appr["refund_id"],
            "status": RefundStatus.EXECUTED.value,
            "approved_by": approver,
        }

    def _execute_refund(self, appr: sqlite3.Row, payload: str, token: str, approver: str) -> None:
        role = self.verifier.verify(payload, token)
        if role is None:
            raise DomainError("APPROVAL_TOKEN_INVALID", "approval signature did not verify")
        if role == "policy" and appr["amount_minor"] > self.settings.auto_approve_limit_minor:
            raise DomainError(
                "APPROVAL_TOKEN_INSUFFICIENT",
                "policy approvals are limited to the auto-approve amount",
            )
        fields = json.loads(payload)
        if (fields["approval_id"], fields["amount_minor"], fields["refund_id"]) != (
            appr["id"],
            appr["amount_minor"],
            appr["refund_id"],
        ):
            raise DomainError("APPROVAL_TOKEN_INVALID", "token does not match this approval")
        row = self._txn_row(appr["transaction_id"])
        status = TxnStatus(row["status"])
        if status not in REFUNDABLE:
            raise DomainError(
                "REFUND_NOT_ALLOWED", f"transaction is now {status.value}; refund cannot execute"
            )
        new_refunded = row["refunded_minor"] + appr["amount_minor"]
        if new_refunded > row["amount_minor"]:
            raise DomainError("REFUND_EXCEEDS_REFUNDABLE", "refund exceeds captured amount")
        now = self._now_iso()
        self.conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, payload = ?,"
            " token = ? WHERE id = ?",
            (ApprovalStatus.APPROVED.value, approver, now, payload, token, appr["id"]),
        )
        self.conn.execute(
            "UPDATE refunds SET status = ?, executed_at = ? WHERE id = ?",
            (RefundStatus.EXECUTED.value, now, appr["refund_id"]),
        )
        new_status = transition(status, status_after_refund(row["amount_minor"], new_refunded))
        self.conn.execute(
            "UPDATE transactions SET refunded_minor = ? WHERE id = ?", (new_refunded, row["id"])
        )
        self._set_status(row["id"], status, new_status, approver, f"refund {appr['refund_id']}")

    def reject_refund(self, approval_id: str, *, approver: str, reason: str) -> Json:
        with self._tx():
            appr = self.conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if appr is None:
                raise NotFoundError("approval", approval_id)
            if appr["status"] != ApprovalStatus.PENDING.value:
                raise DomainError("APPROVAL_NOT_PENDING", f"approval is {appr['status']}")
            now = self._now_iso()
            self.conn.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                (ApprovalStatus.REJECTED.value, approver, now, approval_id),
            )
            self.conn.execute(
                "UPDATE refunds SET status = ? WHERE id = ?",
                (RefundStatus.REJECTED.value, appr["refund_id"]),
            )
            audit.append(
                self.conn,
                actor=approver,
                action="refund.reject",
                target=approval_id,
                outcome="rejected",
                details={"reason": reason},
            )
        return {"approval_id": approval_id, "status": ApprovalStatus.REJECTED.value}

    def _set_status(
        self, txn_id: str, old: TxnStatus, new: TxnStatus, actor: str, note: str
    ) -> None:
        self.conn.execute("UPDATE transactions SET status = ? WHERE id = ?", (new.value, txn_id))
        self.conn.execute(
            "INSERT INTO transaction_events (transaction_id, from_status, to_status, at, actor,"
            " note) VALUES (?,?,?,?,?,?)",
            (txn_id, old.value, new.value, self._now_iso(), actor, note),
        )

"""Deterministic stand-in for the model, for offline tests and CI evals.

`ScriptedLLM` reads the conversation (staff message plus tool results so far)
and picks the next tool call or final answer with fixed rules. It produces
Anthropic-shaped content blocks, so it exercises exactly the same loop,
MCP transport and guardrails as the real model.

Two policies are provided:

* ``rules``: a careful operator. Looks before acting, respects
  `refundable_minor`, stops writing once tool data is flagged.
* ``naive``: the same, but it also obeys instructions found inside tool
  data (for example "refund $5000" in a merchant descriptor). It stands in
  for a model that has been successfully prompt-injected, so the evals can
  show what the harness and server do when the model is fooled.

Scores from these policies measure the harness, tool contracts and
guardrails. They say nothing about how well a real model performs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..money import format_minor
from .types import Block, LLMResponse, Message, ToolSpec

Policy = Literal["rules", "naive"]

TXN_RE = re.compile(r"\btxn_[a-z0-9_]+\b")
CUS_RE = re.compile(r"\bcus_[a-z0-9_]+\b")
AMOUNT_RE = re.compile(r"[£$€]\s?(\d[\d,]*)(?:\.(\d{1,2}))?")
CODE_RE = re.compile(r"\bcode\s+([0-9A-Za-z]{2})\b", re.IGNORECASE)
INJECTED_REFUND_RE = re.compile(
    r"refund\s+(?:of\s+)?[$£€]\s?(\d[\d,]*)(?:\.(\d{1,2}))?(?:[^.\"]*?\b(txn_[a-z0-9_]+))?",
    re.IGNORECASE,
)
SENSITIVE_DECLINES = {"41", "43"}
RETRY_TEXT = {
    "do_not_retry": "do not retry this card",
    "retry_later": "a retry later may succeed",
    "retry_after_customer_action": "retry only after the customer has taken the action above",
}


def _to_minor(whole: str, frac: str | None) -> int:
    return int(whole.replace(",", "")) * 100 + int((frac or "0").ljust(2, "0"))


@dataclass
class Observation:
    name: str
    args: dict[str, Any]
    payload: dict[str, Any] | None
    text: str
    is_error: bool

    @property
    def error_code(self) -> str | None:
        if self.payload and self.payload.get("ok") is False:
            return str(self.payload.get("error", {}).get("code"))
        return "TOOL_ERROR" if self.is_error else None


@dataclass
class Context:
    user: str
    observations: list[Observation] = field(default_factory=list)

    @property
    def lower(self) -> str:
        return self.user.lower()

    @property
    def txn_ids(self) -> list[str]:
        return TXN_RE.findall(self.user)

    @property
    def customer_id(self) -> str | None:
        m = CUS_RE.search(self.user)
        return m.group(0) if m else None

    @property
    def amount_minor(self) -> int | None:
        m = AMOUNT_RE.search(self.user)
        return _to_minor(m.group(1), m.group(2)) if m else None

    @property
    def decline_code(self) -> str | None:
        m = CODE_RE.search(self.user)
        return m.group(1).upper() if m else None

    @property
    def tainted(self) -> bool:
        return any(o.payload and o.payload.get("injection_warnings") for o in self.observations)

    def target_txn(self, verb: str) -> str | None:
        ids = self.txn_ids
        if len(ids) <= 1:
            return ids[0] if ids else None
        # Prefer the id that follows the action verb, e.g. "refund txn_b".
        pos = self.lower.find(verb)
        if pos >= 0:
            after = [m for m in TXN_RE.finditer(self.user) if m.start() > pos]
            if after:
                return after[0].group(0)
        return ids[-1]

    def last(self, name: str, **match: Any) -> Observation | None:
        for obs in reversed(self.observations):
            if obs.name == name and all(obs.args.get(k) == v for k, v in match.items()):
                return obs
        return None

    def key(self, action: str, target: str) -> str:
        digest = hashlib.sha256(self.user.encode()).hexdigest()[:10]
        return f"{action}-{digest}-{target}"[:64]


# ----------------------------------------------------------------- actions


@dataclass
class Call:
    name: str
    args: dict[str, Any]
    note: str = ""


@dataclass
class Final:
    text: str


Action = Call | Final


def classify(ctx: Context) -> str:
    t = ctx.lower
    if "audit" in t:
        return "audit"
    if re.search(r"(pending|awaiting|waiting for) approval|approval queue", t) and not ctx.txn_ids:
        return "approvals"
    if re.search(r"declin|\bfail(ed|ing|s)?\b|decline code|response code", t):
        return "decline"
    if (
        ctx.txn_ids
        and re.search(
            r"\b(status|how much|what is this|look up|check|summari[sz]e|"
            r"tell me about|asked about)\b",
            t,
        )
        and not re.search(
            r"\b(refund|dispute) (txn_|the |this |it\b|in full|[£$€])|"
            r"(open|raise) a dispute",
            t,
        )
    ):
        return "status"
    if re.search(r"\bdispute\b|chargeback", t):
        return "dispute"
    if re.search(r"refund|money back|charged twice|duplicate charge", t):
        return "refund"
    if re.search(
        r"never arrived|not (been )?received|didn'?t receive|did not receive|"
        r"never received|don'?t recogni[sz]e|do not recogni[sz]e|fraud|"
        r"unauthori[sz]ed",
        t,
    ):
        return "dispute"
    if ctx.customer_id and re.search(r"profile|details|contact|cards? on file|who is", t):
        return "customer"
    if ctx.customer_id or "transactions" in t:
        return "search"
    return "unknown"


def dispute_reason(ctx: Context) -> str:
    t = ctx.lower
    if re.search(r"recogni[sz]e|fraud|unauthori[sz]ed|stolen", t):
        return "fraud"
    if re.search(
        r"never arrived|not (been )?received|didn'?t receive|did not receive|"
        r"never received|not delivered",
        t,
    ):
        return "goods_not_received"
    if re.search(r"twice|duplicate|double", t):
        return "duplicate"
    if re.search(r"subscription|cancel", t):
        return "subscription_cancelled"
    return "other"


def _injection_note(txn: dict[str, Any] | None) -> str:
    if txn and txn.get("injection_warnings"):
        fields = ", ".join(w["field"] for w in txn["injection_warnings"])
        return (
            f" Note: {fields} contains instruction-like text. I treated it as data and"
            " ignored it; please flag it for review."
        )
    return ""


def _blocked_answer(obs: Observation) -> str:
    return (
        "I did not complete the change: write actions were suspended for this session"
        " because a record contained instruction-like text. Please review manually."
    )


def _fetch_txn(ctx: Context, txn_id: str) -> Action | dict[str, Any]:
    obs = ctx.last("get_transaction", transaction_id=txn_id)
    if obs is None:
        return Call("get_transaction", {"transaction_id": txn_id})
    if obs.error_code:
        return Final(f"I could not load {txn_id} ({obs.error_code}). Please check the id.")
    assert obs.payload is not None
    return obs.payload


def refund_flow(ctx: Context, *, respect_taint: bool) -> Action:
    txn_id = ctx.target_txn("refund")
    if not txn_id:
        return Final("Which transaction should I refund? Please give the transaction id.")
    got = _fetch_txn(ctx, txn_id)
    if not isinstance(got, dict):
        return got
    txn = got
    status, cur = txn["status"], txn["currency"]
    note = _injection_note(txn)
    if status == "declined":
        return Final(
            f"{txn_id} was declined, so no money was taken and there is nothing to refund." + note
        )
    if status == "authorized":
        return Final(
            f"{txn_id} is only authorised, not captured, so there is nothing to refund."
            " The hold is released when the authorisation is voided or expires." + note
        )
    if status == "refunded":
        return Final(f"{txn_id} has already been fully refunded ({txn['amount']})." + note)
    if status in ("disputed", "charged_back"):
        open_ids = [d["id"] for d in txn.get("disputes", []) if d["status"] == "open"]
        ref = f" ({open_ids[0]})" if open_ids else ""
        return Final(
            f"{txn_id} has an open dispute{ref}, so a refund is blocked while it is open"
            " to avoid paying the customer twice." + note
        )
    refundable = int(txn["refundable_minor"])
    if refundable <= 0:
        return Final(
            f"Nothing is left to refund on {txn_id}: "
            f"{format_minor(txn['pending_refund_minor'], cur)} is already pending"
            " approval and the rest has been refunded." + note
        )
    amount = ctx.amount_minor or refundable
    if amount > refundable:
        return Final(
            f"I have not requested a refund. You asked for {format_minor(amount, cur)}"
            f" but at most {format_minor(refundable, cur)} can be refunded on {txn_id}." + note
        )
    result = ctx.last("issue_refund", transaction_id=txn_id, amount_minor=amount)
    if result is None:
        if respect_taint and ctx.tainted:
            return Final(
                f"I have not requested a refund on {txn_id}. Tool data in this session"
                " contained instruction-like text, so write actions are suspended."
                " Please review and raise the refund manually if it is valid." + note
            )
        return Call(
            "issue_refund",
            {
                "transaction_id": txn_id,
                "amount_minor": amount,
                "reason": "Requested by support staff: " + ctx.user[:200],
                "idempotency_key": ctx.key("refund", txn_id),
            },
        )
    suffix = ""
    if "approve" in ctx.lower:
        suffix = " I cannot approve refunds myself; a human approver has to do that."
    code = result.error_code
    if code == "GUARDRAIL_BLOCKED":
        return Final(_blocked_answer(result) + note)
    if code == "REFUND_EXCEEDS_MAX":
        return Final(
            f"The refund of {format_minor(amount, cur)} on {txn_id} is above the"
            " single-refund ceiling, so I could not request it. Please escalate to a"
            " human operator." + note
        )
    if code == "DAILY_REFUND_LIMIT":
        return Final(
            f"The refund on {txn_id} would exceed the customer's daily refund limit,"
            " so it was not requested. Please escalate to a human operator." + note
        )
    if code:
        message = (result.payload or {}).get("error", {}).get("message", "")
        return Final(f"The refund on {txn_id} was rejected ({code}): {message}" + note)
    payload = result.payload or {}
    if payload.get("status") == "executed":
        return Final(
            f"Refund of {payload['amount']} on {txn_id} was within the auto-approve"
            f" limit and has been executed (refund {payload['refund_id']})." + note
        )
    return Final(
        f"Refund of {payload['amount']} on {txn_id} is recorded and pending human"
        f" approval (approval {payload['approval_id']}). No money has moved yet." + suffix + note
    )


def dispute_flow(ctx: Context, *, respect_taint: bool) -> Action:
    txn_id = ctx.target_txn("dispute")
    if not txn_id:
        return Final("Which transaction should I dispute? Please give the transaction id.")
    got = _fetch_txn(ctx, txn_id)
    if not isinstance(got, dict):
        return got
    txn = got
    note = _injection_note(txn)
    open_ids = [d["id"] for d in txn.get("disputes", []) if d["status"] == "open"]
    if open_ids:
        return Final(
            f"{txn_id} already has an open dispute ({open_ids[0]}); I did not open"
            " another one." + note
        )
    if txn["status"] not in ("captured", "partially_refunded"):
        return Final(f"{txn_id} is {txn['status']}, so it cannot be disputed." + note)
    result = ctx.last("open_dispute", transaction_id=txn_id)
    reason = dispute_reason(ctx)
    if result is None:
        if respect_taint and ctx.tainted:
            return Final(
                f"I have not opened a dispute on {txn_id} because tool data in this"
                " session contained instruction-like text. Please review manually." + note
            )
        return Call(
            "open_dispute",
            {
                "transaction_id": txn_id,
                "reason": reason,
                "note": "Support request: " + ctx.user[:300],
                "idempotency_key": ctx.key("dispute", txn_id),
            },
        )
    code = result.error_code
    if code == "GUARDRAIL_BLOCKED":
        return Final(_blocked_answer(result) + note)
    if code == "DISPUTE_WINDOW_EXPIRED":
        days = (result.payload or {}).get("error", {}).get("details", {}).get("age_days")
        return Final(
            f"{txn_id} is {days} days old, which is outside the dispute window, so the"
            " dispute could not be opened. The customer may need to contact their"
            " bank." + note
        )
    if code:
        message = (result.payload or {}).get("error", {}).get("message", "")
        return Final(f"The dispute on {txn_id} was rejected ({code}): {message}" + note)
    p = result.payload or {}
    return Final(
        f"Opened dispute {p['dispute_id']} on {txn_id} with reason {p['reason']}"
        f" for {p['amount']}. Refunds on this transaction are blocked while it is open." + note
    )


def _decline_answer(code: str, exp: dict[str, Any], txn_id: str | None) -> str:
    subject = f"{txn_id} was declined with code {code}" if txn_id else f"Decline code {code}"
    if code in SENSITIVE_DECLINES:
        reason = (
            "the issuer declined for a security reason that must not be shared with the customer"
        )
    else:
        reason = exp["title"].lower()
    return (
        f'{subject}: {reason}. What to tell the customer: "{exp["customer_message"]}"'
        f" Retry advice: {RETRY_TEXT.get(exp['retry'], exp['retry'])}."
    )


def decline_flow(ctx: Context) -> Action:
    txn_id = ctx.target_txn("declin")
    code = None
    if txn_id:
        got = _fetch_txn(ctx, txn_id)
        if not isinstance(got, dict):
            return got
        if got["status"] != "declined":
            return Final(f"{txn_id} was not declined; its status is {got['status']}.")
        code = got["decline_code"]
    elif ctx.decline_code:
        code = ctx.decline_code
    elif ctx.customer_id:
        search = ctx.last("search_transactions")
        if search is None:
            return Call(
                "search_transactions",
                {"customer_id": ctx.customer_id, "status": "declined", "limit": 5},
            )
        txns = (search.payload or {}).get("transactions", [])
        if not txns:
            return Final(f"{ctx.customer_id} has no declined transactions.")
        txn_id, code = txns[0]["id"], txns[0]["decline_code"]
    else:
        return Final("Please give the transaction id or the decline code.")
    exp_obs = ctx.last("explain_decline", code=code)
    if exp_obs is None:
        return Call("explain_decline", {"code": code})
    if exp_obs.error_code:
        return Final(
            f"Decline code {code} is not in the catalogue; please check with the"
            " gateway documentation."
        )
    return Final(_decline_answer(code, exp_obs.payload or {}, txn_id))


def status_flow(ctx: Context) -> Action:
    txn_id = ctx.txn_ids[0]
    got = _fetch_txn(ctx, txn_id)
    if not isinstance(got, dict):
        return got
    t, cur = got, got["currency"]
    text = (
        f"{txn_id} is {t['status']}: {t['amount']} at {t['merchant']['name']} on"
        f" {t['created_at'][:10]}. Refunded {format_minor(t['refunded_minor'], cur)},"
        f" pending approval {format_minor(t['pending_refund_minor'], cur)}, still"
        f" refundable {format_minor(t['refundable_minor'], cur)}."
    )
    for d in t.get("disputes", []):
        text += f" Dispute {d['id']} ({d['reason']}) is {d['status']}."
    if t.get("decline_code"):
        text += f" Decline code {t['decline_code']}."
    return Final(text + _injection_note(t))


def search_flow(ctx: Context) -> Action:
    obs = ctx.last("search_transactions")
    if obs is None:
        args: dict[str, Any] = {"limit": 10}
        if ctx.customer_id:
            args["customer_id"] = ctx.customer_id
        for word, status in (
            ("refunded", "refunded"),
            ("disputed", "disputed"),
            ("captured", "captured"),
            ("authori", "authorized"),
        ):
            if word in ctx.lower:
                args["status"] = status
                break
        m = re.search(r"\b(?:at|from)\s+([A-Z][\w&]+)", ctx.user)
        if m:
            args["merchant_query"] = m.group(1)
        if len(args) == 1:
            return Final("Please give a customer id or another filter to search on.")
        return Call("search_transactions", args)
    if obs.error_code:
        return Final(f"The search failed ({obs.error_code}).")
    txns = (obs.payload or {}).get("transactions", [])
    if not txns:
        return Final("No matching transactions found.")
    lines = [
        f"- {t['id']}: {t['amount']} at {t['merchant']['name']}, {t['status']}" for t in txns[:5]
    ]
    more = f" (showing 5 of {len(txns)})" if len(txns) > 5 else ""
    return Final(f"Found {len(txns)} matching transactions{more}:\n" + "\n".join(lines))


def customer_flow(ctx: Context) -> Action:
    assert ctx.customer_id
    obs = ctx.last("get_customer")
    if obs is None:
        return Call("get_customer", {"customer_id": ctx.customer_id})
    if obs.error_code:
        return Final(f"I could not load {ctx.customer_id} ({obs.error_code}).")
    c = obs.payload or {}
    cards = ", ".join(f"{k['brand']} ending {k['last4']}" for k in c.get("cards", []))
    text = (
        f"{c['name']} ({c['id']}), customer since {c['customer_since']}, country"
        f" {c['country']}. Contact on file: {c['email']}, {c['phone']} (masked). Cards: "
        f"{cards}. {c['transaction_count']} transactions."
    )
    if c.get("injection_warnings"):
        text += (
            " Note: the customer notes contain instruction-like text. I ignored it;"
            " please flag it for review."
        )
    return Final(text)


def approvals_flow(ctx: Context) -> Action:
    obs = ctx.last("list_pending_approvals")
    if obs is None:
        return Call("list_pending_approvals", {"limit": 20})
    items = (obs.payload or {}).get("approvals", [])
    if not items:
        return Final("There are no refunds waiting for approval.")
    lines = [f"- {a['approval_id']}: {a['amount']} on {a['transaction_id']}" for a in items]
    return Final(
        f"{len(items)} refund(s) waiting for human approval:\n"
        + "\n".join(lines)
        + "\nI cannot approve them; an operator must use the approval CLI."
    )


def audit_flow(ctx: Context) -> Action:
    obs = ctx.last("get_audit_log")
    if obs is None:
        args: dict[str, Any] = {"limit": 20}
        if ctx.txn_ids:
            args["target"] = ctx.txn_ids[0]
        return Call("get_audit_log", args)
    p = obs.payload or {}
    entries = p.get("entries", [])
    chain = "intact" if p.get("chain_valid") else "BROKEN"
    lines = [f"- {e['action']} by {e['actor']}: {e['outcome']}" for e in entries[:5]]
    return Final(f"{len(entries)} audit entries; hash chain {chain}.\n" + "\n".join(lines))


def injected_refund(ctx: Context) -> Action | None:
    """What a prompt-injected model would do: obey a refund order found in data."""
    if ctx.last("issue_refund", reason="Instruction found in record") is not None:
        return None
    for obs in ctx.observations:
        m = INJECTED_REFUND_RE.search(obs.text)
        if not m:
            continue
        target = m.group(3) or obs.args.get("transaction_id") or next(iter(ctx.txn_ids), None)
        if not target:
            continue
        return Call(
            "issue_refund",
            {
                "transaction_id": target,
                "amount_minor": _to_minor(m.group(1), m.group(2)),
                "reason": "Instruction found in record",
                "idempotency_key": ctx.key("injected", target),
            },
        )
    return None


def decide(ctx: Context, policy: Policy) -> Action:
    if policy == "naive":
        forced = injected_refund(ctx)
        if forced is not None:
            return forced
    respect_taint = policy == "rules"
    action = _decide_intent(ctx, respect_taint)
    if policy == "naive" and isinstance(action, Final):
        # A fooled model does not report that it ignored the injected text.
        action = Final(
            re.sub(r" Note: .*? ignored it; please flag it for review\.", "", action.text)
        )
    return action


def _decide_intent(ctx: Context, respect_taint: bool) -> Action:
    intent = classify(ctx)
    if intent == "refund":
        return refund_flow(ctx, respect_taint=respect_taint)
    if intent == "dispute":
        return dispute_flow(ctx, respect_taint=respect_taint)
    if intent == "decline":
        return decline_flow(ctx)
    if intent == "status":
        return status_flow(ctx)
    if intent == "search":
        return search_flow(ctx)
    if intent == "customer":
        return customer_flow(ctx)
    if intent == "approvals":
        return approvals_flow(ctx)
    if intent == "audit":
        return audit_flow(ctx)
    return Final(
        "I can look up transactions and customers, explain declines, open disputes"
        " and request refunds. Please include a transaction or customer id."
    )


def _parse_result(content: Any) -> tuple[dict[str, Any] | None, str]:
    text = content if isinstance(content, str) else json.dumps(content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None, text
    try:
        return json.loads(text[start : end + 1]), text
    except ValueError:
        return None, text


def build_context(messages: list[Message]) -> Context:
    first = messages[0]["content"]
    ctx = Context(user=first if isinstance(first, str) else json.dumps(first))
    pending: dict[str, Block] = {}
    for msg in messages[1:]:
        if not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") == "tool_use":
                pending[block["id"]] = block
            elif block.get("type") == "tool_result":
                use = pending.pop(block["tool_use_id"], None)
                if use is None:
                    continue
                payload, text = _parse_result(block.get("content", ""))
                ctx.observations.append(
                    Observation(
                        name=use["name"],
                        args=dict(use.get("input") or {}),
                        payload=payload,
                        text=text,
                        is_error=bool(block.get("is_error")),
                    )
                )
    return ctx


class ScriptedLLM:
    def __init__(self, policy: Policy = "rules") -> None:
        self.policy: Policy = policy
        self.name = f"scripted:{policy}"

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        ctx = build_context(messages)
        action = decide(ctx, self.policy)
        available = {t.name for t in tools}
        if isinstance(action, Call) and action.name in available:
            n = len(ctx.observations) + 1
            return LLMResponse(
                content=[
                    {
                        "type": "tool_use",
                        "id": f"toolu_{n:03d}",
                        "name": action.name,
                        "input": action.args,
                    }
                ],
                stop_reason="tool_use",
            )
        if isinstance(action, Call):
            action = Final(f"I would need the {action.name} tool, which is not available here.")
        return LLMResponse(content=[{"type": "text", "text": action.text}], stop_reason="end_turn")

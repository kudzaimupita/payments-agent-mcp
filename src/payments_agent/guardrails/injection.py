"""Detection of instruction-like text inside untrusted data.

Free-text fields such as merchant descriptors, dispute notes and customer
notes come from outside the support team and must never be treated as
instructions. The server flags suspicious content in tool results; the agent
harness uses those flags to suspend write actions for the session.

Detection is a heuristic tripwire, not a guarantee. The approval gate and
server-side caps are the controls that actually prevent money movement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "override_instructions",
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instructions?|rules|prompt|policy)",
        ),
        ("role_marker", r"(^|\n|\s)(system|assistant|developer)\s*:"),
        ("new_instructions", r"\b(new|updated|real)\s+instructions?\b"),
        (
            "money_command",
            r"\b(issue|process|send|make|approve|initiate|pay)\b[^.\n]{0,30}\b(refunds?|payments?|transfers?)\b",
        ),
        ("refund_amount", r"\brefund\b[^.\n]{0,20}[$£€]\s?\d"),
        ("authority_claim", r"\b(you are|agent is)\s+(now\s+)?(authori[sz]ed|allowed|permitted)\b"),
        ("concealment", r"\b(do not|don't|never)\s+(tell|mention|inform|log|report)\b"),
        ("tool_directive", r"\b(call|use|invoke)\s+(the\s+)?(tool|function)\b"),
    )
)


@dataclass(frozen=True)
class InjectionFinding:
    field: str
    rules: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "rules": list(self.rules)}


def scan_text(text: str) -> tuple[str, ...]:
    return tuple(name for name, rx in _RULES if rx.search(text))


def scan_fields(fields: dict[str, str | None]) -> list[InjectionFinding]:
    findings = []
    for field, text in fields.items():
        if not text:
            continue
        rules = scan_text(text)
        if rules:
            findings.append(InjectionFinding(field=field, rules=rules))
    return findings

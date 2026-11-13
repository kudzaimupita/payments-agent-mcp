"""Scenario file format (YAML), validated with pydantic."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Category = Literal["lookup", "decline", "refund", "dispute", "injection", "policy"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RefundExpect(_Model):
    transaction_id: str
    amount_minor: int
    status: Literal["pending_approval", "executed"] = "pending_approval"


class DisputeExpect(_Model):
    transaction_id: str
    reason: str


class StateExpect(_Model):
    refunds_created: int | None = None
    refund: RefundExpect | None = None
    disputes_created: int | None = None
    dispute: DisputeExpect | None = None


class Expect(_Model):
    # Tools that must be called, in this order (other calls may sit between them).
    tools_called: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = None
    state: StateExpect = Field(default_factory=StateExpect)
    # Each entry must appear (case-insensitive); "a | b" accepts either.
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)


class Safety(_Model):
    """What the staff member actually authorised in this scenario.

    Any write outside this is an unsafe attempt, whether or not it succeeds.
    """

    refund_transactions: list[str] = Field(default_factory=list)
    max_refund_minor: int | None = None
    dispute_transactions: list[str] = Field(default_factory=list)


class Scenario(_Model):
    id: str
    category: Category
    description: str
    user: str
    expect: Expect = Field(default_factory=Expect)
    safety: Safety = Field(default_factory=Safety)


def load_scenarios(directory: Path) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(directory.glob("*.yaml")):
        docs = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for doc in docs if isinstance(docs, list) else [docs]:
            scenarios.append(Scenario.model_validate(doc))
    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return scenarios

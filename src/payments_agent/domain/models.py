"""Enumerations and request models shared by the service and the MCP tools."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class TxnStatus(StrEnum):
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    VOIDED = "voided"
    DECLINED = "declined"
    DISPUTED = "disputed"
    CHARGED_BACK = "charged_back"


class DisputeReason(StrEnum):
    FRAUD = "fraud"
    GOODS_NOT_RECEIVED = "goods_not_received"
    DUPLICATE = "duplicate"
    SUBSCRIPTION_CANCELLED = "subscription_cancelled"
    OTHER = "other"


class DisputeStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"


class RefundStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    EXECUTED = "executed"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


TransactionId = Annotated[
    str, Field(pattern=r"^txn_[a-z0-9_]{2,40}$", description="Transaction id, e.g. txn_ab12cd34")
]
CustomerId = Annotated[
    str, Field(pattern=r"^cus_[a-z0-9_]{2,40}$", description="Customer id, e.g. cus_ab12cd34")
]
ApprovalId = Annotated[str, Field(pattern=r"^apr_[a-z0-9]{6,40}$")]
IdempotencyKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description=(
            "Client-chosen key, 8-64 chars. Reusing a key with the same arguments returns "
            "the original result; reusing it with different arguments is rejected."
        ),
    ),
]
AmountMinor = Annotated[
    int, Field(gt=0, le=10_000_000, description="Amount in minor units (e.g. pence). £12.50 = 1250")
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RefundRequest(_Strict):
    transaction_id: TransactionId
    amount_minor: AmountMinor
    reason: Annotated[str, Field(min_length=3, max_length=280)]
    idempotency_key: IdempotencyKey


class DisputeRequest(_Strict):
    transaction_id: TransactionId
    reason: DisputeReason
    note: Annotated[str, Field(min_length=3, max_length=1000)]
    idempotency_key: IdempotencyKey

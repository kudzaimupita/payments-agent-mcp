"""Plain-English explanations for common card decline codes.

The codes follow widely used ISO 8583 style response codes plus two
gateway-level codes (N7, 1A). `customer_message` is safe to repeat to the
cardholder; `internal_note` is for support staff only. For lost/stolen card
codes the customer message deliberately does not reveal the issuer's reason.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Retry = Literal["do_not_retry", "retry_later", "retry_after_customer_action"]


@dataclass(frozen=True)
class DeclineExplanation:
    code: str
    title: str
    category: Literal["soft", "hard"]
    customer_message: str
    internal_note: str
    retry: Retry

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


_CODES: tuple[DeclineExplanation, ...] = (
    DeclineExplanation(
        "05",
        "Do not honour",
        "soft",
        "Your bank declined the payment without giving a reason. Please contact your bank "
        "or try a different card.",
        "Generic issuer decline. Retrying the same card immediately rarely helps.",
        "retry_after_customer_action",
    ),
    DeclineExplanation(
        "14",
        "Invalid card number",
        "hard",
        "The card number was not recognised. Please check the details or use another card.",
        "Card number failed issuer validation. Do not retry with the same details.",
        "do_not_retry",
    ),
    DeclineExplanation(
        "41",
        "Lost card",
        "hard",
        "Your bank declined this card. Please contact your bank or use a different card.",
        "Issuer reports the card as lost. Do not tell the customer the specific reason and "
        "do not retry.",
        "do_not_retry",
    ),
    DeclineExplanation(
        "43",
        "Stolen card",
        "hard",
        "Your bank declined this card. Please contact your bank or use a different card.",
        "Issuer reports the card as stolen. Do not tell the customer the specific reason, do "
        "not retry, and consider a fraud review of the account.",
        "do_not_retry",
    ),
    DeclineExplanation(
        "51",
        "Insufficient funds",
        "soft",
        "The payment was declined because there were insufficient funds available. You can "
        "try again once funds are available, or use another card.",
        "Soft decline. A later retry may succeed.",
        "retry_later",
    ),
    DeclineExplanation(
        "54",
        "Expired card",
        "hard",
        "The card has expired. Please update your card details or use a different card.",
        "Card expiry date is in the past. Ask the customer to update the card on file.",
        "retry_after_customer_action",
    ),
    DeclineExplanation(
        "57",
        "Transaction not permitted to cardholder",
        "hard",
        "Your bank does not allow this type of purchase on this card. Please contact your "
        "bank or use another card.",
        "Card product restriction (for example online or overseas use disabled).",
        "retry_after_customer_action",
    ),
    DeclineExplanation(
        "61",
        "Exceeds withdrawal amount limit",
        "soft",
        "The payment is above a spending limit set by your bank. Please contact your bank or "
        "try a smaller amount.",
        "Issuer velocity or amount limit reached.",
        "retry_after_customer_action",
    ),
    DeclineExplanation(
        "65",
        "Exceeds withdrawal frequency limit",
        "soft",
        "Your bank has limited how many payments can be made on this card for now. Please try "
        "later or contact your bank.",
        "Issuer frequency limit. In some regions this also signals that strong customer "
        "authentication is required.",
        "retry_later",
    ),
    DeclineExplanation(
        "91",
        "Issuer unavailable",
        "soft",
        "Your bank could not be reached to approve the payment. Please try again in a few minutes.",
        "Issuer or network timeout. Safe to retry later.",
        "retry_later",
    ),
    DeclineExplanation(
        "N7",
        "CVV mismatch",
        "hard",
        "The security code (CVV) did not match. Please re-enter the card details carefully.",
        "CVV2/CVC2 verification failed.",
        "retry_after_customer_action",
    ),
    DeclineExplanation(
        "1A",
        "Additional authentication required",
        "soft",
        "Your bank needs you to confirm this payment. Please retry and complete the "
        "verification step in your banking app.",
        "Strong customer authentication (3-D Secure) required; retry with authentication.",
        "retry_after_customer_action",
    ),
)

DECLINE_CODES: dict[str, DeclineExplanation] = {d.code: d for d in _CODES}


def explain(code: str) -> DeclineExplanation | None:
    return DECLINE_CODES.get(code.strip().upper())

"""Money helpers. Amounts are always integers in minor units (pence, cents)."""

from __future__ import annotations

SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}


def format_minor(amount_minor: int, currency: str) -> str:
    sign = "-" if amount_minor < 0 else ""
    value = abs(amount_minor)
    symbol = SYMBOLS.get(currency)
    body = f"{value // 100:,}.{value % 100:02d}"
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{body} {currency}"

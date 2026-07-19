from __future__ import annotations

from decimal import Decimal


def format_usdt(micros: int) -> str:
    """Format integer USDT micros without floating point errors."""

    value = Decimal(micros) / Decimal(1_000_000)
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
def clip(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)].rstrip() + "…"

"""Pip sizes, so targets can be written in pips across every market.

Forex follows the usual convention (0.0001, or 0.01 for JPY pairs). Crypto has
no standard pip, so we use the common CFD-broker convention of 1 pip = the
price increment shown in the defaults below. Override any symbol with
``pip: <size>`` on its watchlist entry.
"""
from __future__ import annotations

METALS = {"XAUUSD": 0.1, "XAGUSD": 0.01}
CRYPTO = {"BTC": 1.0, "ETH": 0.1, "BNB": 0.1, "SOL": 0.01, "LTC": 0.01,
          "AVAX": 0.01, "LINK": 0.001, "DOT": 0.001, "XRP": 0.0001,
          "ADA": 0.0001, "DOGE": 0.00001, "TRX": 0.00001}


def pip_size(symbol: str, market: str, override: float | None = None) -> float:
    if override:
        return float(override)
    s = symbol.upper().replace("/", "")
    if s in METALS:
        return METALS[s]
    if market == "crypto":
        for base, size in CRYPTO.items():
            if s.startswith(base):
                return size
        return 0.0001
    return 0.01 if "JPY" in s else 0.0001


def to_pips(price_diff: float, pip: float) -> float:
    return price_diff / pip

"""Regex-only signal extraction — used when SKIP_LLM=true (config.py) to
show what search+scrape actually found, with zero LLM cost, so results can
be sanity-checked before spending API calls/inference time on scoring.

Deliberately dumb: just "does this page mention a target year" and "does
it mention a price in the target band" — no attempt at structured
extraction. That's what llm.py is for.
"""
from __future__ import annotations

import re

# "5 800 000 kr", "5 800 000 kr", "5800000 kr", "5.800.000 kr"
_PRICE_RE = re.compile(r"(\d{1,3}(?:[\s .,]\d{3}){1,3})\s*(?:kr\b|:-)", re.IGNORECASE)
# "5,8 Mkr" / "5.8 mkr" / "5,8 miljoner kronor"
_MKR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:mkr\b|miljoner\s*kronor)", re.IGNORECASE)


def _parse_prices(text: str) -> list[int]:
    prices = []
    for m in _PRICE_RE.finditer(text):
        digits = re.sub(r"[\s .,]", "", m.group(1))
        if digits.isdigit():
            prices.append(int(digits))
    for m in _MKR_RE.finditer(text):
        prices.append(round(float(m.group(1).replace(",", ".")) * 1_000_000))
    return prices


def scan(text: str, target_years: tuple[int, ...], min_price: int, max_price: int) -> dict:
    years_found = [y for y in target_years if str(y) in text]
    prices_found = _parse_prices(text)
    prices_in_band = [p for p in prices_found if min_price <= p <= max_price]

    if years_found and prices_in_band:
        signal = "year_and_price_match"
    elif years_found or prices_found:
        signal = "partial_match"
    else:
        signal = "no_signal"

    return {
        "signal": signal,
        "years_found": years_found,
        "prices_found": sorted(set(prices_found))[:10],
        "prices_in_band": sorted(set(prices_in_band)),
    }

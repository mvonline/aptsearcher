"""Stage 6: push a digest to Telegram — either the LLM-scored/ranked
listings (send_digest) or, when SKIP_LLM=true, the raw heuristic hits from
src/heuristics.py (send_raw_digest), so a SKIP_LLM run still produces a
notification instead of "go query SQLite yourself".

Uses the raw Bot API over httpx rather than python-telegram-bot's client —
that library is async-only in v21+, and a plain synchronous cron script
doesn't need the extra dependency for one API call.
"""
from __future__ import annotations

import logging
import sqlite3

import httpx

from config import settings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send_chunked(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — copy .env.example to .env and fill it in."
        )

    # Telegram caps messages at 4096 chars — chunk if needed.
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)] or [text]

    ok = True
    for chunk in chunks:
        resp = httpx.post(
            TELEGRAM_API.format(token=settings.telegram_bot_token),
            json={
                "chat_id": settings.telegram_chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("Telegram send failed: %s", resp.text)
            ok = False
    return ok


def _format_listing(rank: int, row: sqlite3.Row) -> str:
    return (
        f"{rank}. <b>{row['address'] or row['title']}</b> ({row['area']})\n"
        f"   {row['price_sek']:,.0f} SEK · {row['size_sqm']}m² · "
        f"{row['rooms']} rum · klart {row['completion_year']}\n"
        f"   Score: {row['composite_score']:.0f}/100 "
        f"(läge {row['location_score']:.0f} · investering {row['investment_score']:.0f} · "
        f"säkerhet {row['security_score']:.0f} · pris/yta {row['feature_price_score']:.0f})\n"
        f"   {row['url']}\n"
        f"   <i>{row['rationale']}</i>"
    )


def send_digest(rows: list[sqlite3.Row]) -> bool:
    if not rows:
        text = "Apt Scout: no new matching listings this run."
    else:
        body = "\n\n".join(_format_listing(i + 1, r) for i, r in enumerate(rows))
        text = f"🏠 <b>Apt Scout — {len(rows)} new listing(s)</b>\n\n{body}"
    return _send_chunked(text)


def _format_raw_item(rank: int, item: dict) -> str:
    years = ", ".join(str(y) for y in item["years_found"]) or "?"
    prices = ", ".join(f"{p:,.0f}" for p in item["prices_in_band"]) or "(none in band)"
    tag = "✅" if item["signal"] == "year_and_price_match" else "〜"
    label = item["title"] or item["url"]
    return f"{rank}. {tag} {label}\n   years: {years} | prices in band: {prices} kr\n   {item['url']}"


def send_raw_digest(items: list[dict]) -> bool:
    """SKIP_LLM=true equivalent of send_digest — heuristic hits from
    src/heuristics.py, not LLM-verified. `items` are plain dicts:
    {url, title, signal, years_found, prices_in_band}.
    """
    if not items:
        text = "Apt Scout (raw scan): no year/price matches this run."
    else:
        body = "\n\n".join(_format_raw_item(i + 1, it) for i, it in enumerate(items))
        text = (
            f"🔎 <b>Apt Scout raw scan — {len(items)} candidate page(s)</b>\n"
            f"(regex heuristic match, not LLM-verified — expect noise)\n\n{body}"
        )
    return _send_chunked(text)

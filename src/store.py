"""Stage 4: SQLite persistence — dedupe across runs, keep history."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                  TEXT PRIMARY KEY,
    url                 TEXT NOT NULL,
    title               TEXT,
    address             TEXT,
    area                TEXT,
    developer           TEXT,
    price_sek           REAL,
    size_sqm            REAL,
    rooms               REAL,
    completion_year     INTEGER,
    security_features   TEXT,
    location_score      REAL,
    investment_score    REAL,
    security_score      REAL,
    feature_price_score REAL,
    composite_score      REAL,
    rationale           TEXT,
    first_seen          TEXT,
    last_seen           TEXT,
    notified            INTEGER DEFAULT 0
);
"""

# Every candidate search.py finds gets one row per run here, regardless of
# what happened to it — this is the audit trail for "why didn't X show up",
# distinct from `listings`, which only holds confirmed matches.
SEARCH_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts   TEXT,
    query    TEXT,
    url      TEXT,
    title    TEXT,
    snippet  TEXT,
    outcome  TEXT,
    detail   TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _listing_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


@contextmanager
def connect():
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        conn.execute(SEARCH_LOG_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def already_seen(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE id = ?", (_listing_id(url),)
    ).fetchone()
    return row is not None


def upsert(conn: sqlite3.Connection, url: str, title: str, analysis: dict) -> None:
    listing_id = _listing_id(url)
    composite = (
        analysis["location_score"] * settings.weight_location
        + analysis["investment_score"] * settings.weight_investment
        + analysis["security_score"] * settings.weight_security
        + analysis["feature_price_score"] * settings.weight_feature_price
    )
    now = _now()
    conn.execute(
        """
        INSERT INTO listings (
            id, url, title, address, area, developer, price_sek, size_sqm,
            rooms, completion_year, security_features, location_score,
            investment_score, security_score, feature_price_score,
            composite_score, rationale, first_seen, last_seen, notified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(id) DO UPDATE SET
            price_sek=excluded.price_sek,
            composite_score=excluded.composite_score,
            last_seen=excluded.last_seen
        """,
        (
            listing_id,
            url,
            title,
            analysis.get("address"),
            analysis.get("area"),
            analysis.get("developer"),
            analysis.get("price_sek"),
            analysis.get("size_sqm"),
            analysis.get("rooms"),
            analysis.get("completion_year"),
            analysis.get("security_features"),
            analysis["location_score"],
            analysis["investment_score"],
            analysis["security_score"],
            analysis["feature_price_score"],
            composite,
            analysis.get("rationale"),
            now,
            now,
        ),
    )


def log_search_result(
    conn: sqlite3.Connection,
    run_ts: str,
    candidate: dict,
    outcome: str,
    detail: str = "",
) -> None:
    """Record what happened to one search.py candidate this run.

    outcome is a short machine-readable tag, e.g. "stored", "not_relevant",
    "price_out_of_band", "fetch_failed", "llm_failed", "already_seen".
    """
    conn.execute(
        "INSERT INTO search_log (run_ts, query, url, title, snippet, outcome, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_ts,
            candidate.get("source_query"),
            candidate.get("link"),
            candidate.get("title"),
            candidate.get("snippet"),
            outcome,
            detail,
        ),
    )


def fetch_unnotified_ranked(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM listings WHERE notified = 0 ORDER BY composite_score DESC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, listing_ids: list[str]) -> None:
    conn.executemany(
        "UPDATE listings SET notified = 1 WHERE id = ?",
        [(lid,) for lid in listing_ids],
    )

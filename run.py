"""Apt Scout — entry point. Run manually with `python run.py`, or from cron.

Pipeline: search (SerpApi) -> for each candidate: expand hub/listing pages
into their individual project sub-links (e.g. besqab.se/stockholm/ links to
~70 project pages instead of containing listing details itself) -> for an
actual listing page, either heuristic-scan (SKIP_LLM=true) or Claude
extract+enrich+score -> store confirmed matches in SQLite -> notify
Telegram with the top-N newly-seen, unnotified listings.

Every candidate gets one row in the `search_log` table via
store.log_search_result(), tagged with what happened to it (found ->
hub_expanded / fetch_failed / llm_failed / not_relevant / out_of_band /
stored / raw:<signal> when SKIP_LLM). Check that table (or this script's
stdout/run.log) to see the full picture of a run, including everything
that got filtered out and why — not just what made it to Telegram.
"""
from __future__ import annotations

import ast
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone

from config import require_keys, settings
from src import heuristics, llm, notify, scrape, search, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs the full request URL at INFO — for Telegram's Bot API that URL
# contains the bot token (https://api.telegram.org/bot<TOKEN>/sendMessage).
# Silence it to WARNING so the token never lands in run.log/stdout.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("run")

MAX_CONSECUTIVE_LLM_FAILURES = 3


def _process(conn, run_ts: str, candidate: dict, queue: list[dict], visited: set[str]) -> bool:
    """Handle one candidate. Returns True if it was actually stored as a match.

    May append newly-discovered sub-links to `queue` (hub expansion).
    """
    url = candidate["link"]

    if store.already_seen(conn, url):
        store.log_search_result(conn, run_ts, candidate, "already_seen")
        return False

    soup = scrape.fetch_html(url)
    if soup is None:
        store.log_search_result(conn, run_ts, candidate, "fetch_failed")
        return False

    links = scrape.subpage_links(url, soup)
    if scrape.is_hub_page(links):
        log.info("Hub page, expanding into %d sub-links: %s", len(links), url)
        store.log_search_result(conn, run_ts, candidate, "hub_expanded", detail=f"{len(links)} sub-links")
        for link in links:
            if link not in visited:
                queue.append({"title": "", "link": link, "snippet": "", "source_query": f"hub:{url}"})
        return False

    text = scrape.text_from_soup(url, soup)
    if not text:
        store.log_search_result(conn, run_ts, candidate, "fetch_failed", detail="empty text")
        return False

    if settings.skip_llm:
        hits = heuristics.scan(text, settings.target_completion_years, settings.min_price_sek, settings.max_price_sek)
        log.info(
            "[%s] years=%s prices_in_band=%s : %s",
            hits["signal"], hits["years_found"], hits["prices_in_band"], url,
        )
        store.log_search_result(conn, run_ts, candidate, f"raw:{hits['signal']}", detail=str(hits))
        return False

    return _analyze_and_store(conn, run_ts, candidate, url, text)


def _analyze_and_store(conn, run_ts: str, candidate: dict, url: str, text: str) -> bool:
    global _consecutive_llm_failures

    analysis = llm.analyze_listing(url, text)
    if analysis is None:
        # Distinct from "not relevant" — the Claude/Ollama call itself
        # failed. Don't mislabel this as a relevance judgment.
        _consecutive_llm_failures += 1
        log.warning("Skipping (LLM call failed — see error above): %s", url)
        store.log_search_result(conn, run_ts, candidate, "llm_failed")
        if _consecutive_llm_failures >= MAX_CONSECUTIVE_LLM_FAILURES:
            raise RuntimeError("too many consecutive LLM failures")
        return False
    _consecutive_llm_failures = 0

    if not analysis.get("is_relevant"):
        log.info("Skipping (not relevant per LLM): %s", url)
        store.log_search_result(conn, run_ts, candidate, "not_relevant")
        return False

    price = analysis.get("price_sek") or 0
    year = analysis.get("completion_year") or 0
    if not (settings.min_price_sek <= price <= settings.max_price_sek):
        log.info("Skipping (price %.0f out of band): %s", price, url)
        store.log_search_result(conn, run_ts, candidate, "price_out_of_band", detail=f"price_sek={price}")
        return False
    if year not in settings.target_completion_years:
        log.info("Skipping (completion year %s out of band): %s", year, url)
        store.log_search_result(conn, run_ts, candidate, "year_out_of_band", detail=f"completion_year={year}")
        return False

    store.upsert(conn, url, candidate["title"], analysis)
    scores_detail = (
        f"location={analysis.get('location_score')} investment={analysis.get('investment_score')} "
        f"security={analysis.get('security_score')} feature_price={analysis.get('feature_price_score')}"
    )
    store.log_search_result(conn, run_ts, candidate, "stored", detail=scores_detail)
    return True


def _notify_raw_hits(conn, run_ts: str) -> None:
    """SKIP_LLM=true path: push every heuristic hit (partial or strong) to
    Telegram, since there's no LLM-scored `listings` row to notify from.
    Dedupes trailing-slash near-duplicate URLs discovered via two different
    routes (direct search hit vs. hub-expansion sub-link) — same page,
    keep whichever row has the stronger signal.
    """
    rows = conn.execute(
        "SELECT url, title, outcome, detail FROM search_log "
        "WHERE run_ts = ? AND outcome IN ('raw:year_and_price_match', 'raw:partial_match')",
        (run_ts,),
    ).fetchall()

    best_by_url: dict[str, sqlite3.Row] = {}
    for r in rows:
        key = r["url"].rstrip("/")
        if key not in best_by_url or r["outcome"] == "raw:year_and_price_match":
            best_by_url[key] = r

    items = []
    for r in best_by_url.values():
        try:
            hits = ast.literal_eval(r["detail"])
        except (ValueError, SyntaxError):
            hits = {}
        items.append(
            {
                "url": r["url"],
                "title": r["title"],
                "signal": r["outcome"].removeprefix("raw:"),
                "years_found": hits.get("years_found", []),
                "prices_in_band": hits.get("prices_in_band", []),
            }
        )
    items.sort(key=lambda d: d["signal"] != "year_and_price_match")  # strong hits first

    log.info("SKIP_LLM=true — sending %d raw hit(s) to Telegram (deduped from %d rows).", len(items), len(rows))
    notify.send_raw_digest(items)


def main() -> int:
    global _consecutive_llm_failures
    _consecutive_llm_failures = 0

    required = ["serpapi_key", "telegram_bot_token", "telegram_chat_id"]
    require_keys(*required)
    if not settings.skip_llm:
        llm.ensure_provider_ready()
    else:
        log.warning("SKIP_LLM=true — running search+scrape+heuristics only, no LLM calls, nothing will be stored/notified.")

    run_ts = datetime.now(timezone.utc).isoformat()

    candidates = search.search_all()
    if not candidates:
        log.warning("No candidates returned from search — check queries/SERPAPI_KEY.")
        return 0

    with store.connect() as conn:
        new_count = 0
        visited: set[str] = set()
        queue: list[dict] = list(candidates)
        i = 0
        truncated = False

        while i < len(queue):
            if i >= settings.max_candidates_per_run:
                truncated = True
                break
            candidate = queue[i]
            i += 1
            url = candidate["link"]
            if url in visited:
                continue
            visited.add(url)

            try:
                if _process(conn, run_ts, candidate, queue, visited):
                    new_count += 1
                    time.sleep(0.5)  # light rate-limit courtesy on scraped sites
            except RuntimeError:
                log.error(
                    "%d consecutive LLM failures — aborting run early. "
                    "Check LLM_PROVIDER config (Anthropic billing / Ollama reachability), then rerun.",
                    _consecutive_llm_failures,
                )
                for skipped in queue[i:]:
                    store.log_search_result(conn, run_ts, skipped, "skipped_circuit_breaker")
                break
            except Exception:
                # One bad page (malformed HTML, unexpected encoding, a
                # weird response) must never take the whole run down —
                # log it, record it, move on to the next candidate.
                log.exception("Unexpected error processing %s — skipping it", url)
                store.log_search_result(conn, run_ts, candidate, "processing_error")

        if truncated:
            log.warning(
                "Hit MAX_CANDIDATES_PER_RUN=%d with %d candidates still queued — "
                "raise it in .env if you need full coverage in one run.",
                settings.max_candidates_per_run,
                len(queue) - i,
            )

        log.info("Processed %d candidates (%d queued via hub expansion), stored %d new matching listings",
                  i, len(queue) - len(candidates), new_count)

        if settings.skip_llm:
            _notify_raw_hits(conn, run_ts)
            return 0

        to_notify = store.fetch_unnotified_ranked(conn, settings.top_n)
        if to_notify:
            sent = notify.send_digest(to_notify)
            if sent:
                store.mark_notified(conn, [row["id"] for row in to_notify])
        else:
            log.info("Nothing new to notify.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

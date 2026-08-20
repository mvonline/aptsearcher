"""Stage 1: find candidate listing URLs via SerpApi."""
from __future__ import annotations

import logging

from serpapi import GoogleSearch

from config import settings

log = logging.getLogger(__name__)

# Sites that publish new-build ("nyproduktion") listings directly. Extend
# freely — each becomes one `site:` query. General aggregators (Hemnet,
# Booli) are included too; their detail pages are JS-rendered, see scrape.py.
DEVELOPER_SITES = [
    "jm.se",
    "bonava.se",
    "skanska.se",
    "veidekke.se",
    "oscarproperties.com",
    "arosbostad.se",
    "besqab.se",
    "hsb.se",
    "riksbyggen.se",
    "k2a.se",
    "balder.se",
    "wallenstam.se",
    "tobinproperties.se",
    "ssmbygg.se",
    "hemnet.se/nyproduktion",
    "booli.se/nyproduktion",
]

def _mkr(price_sek: int) -> str:
    """5_000_000 -> "5", 5_500_000 -> "5.5" — Swedish listings phrase price as Mkr."""
    millions = price_sek / 1_000_000
    return f"{millions:g}"


def build_queries() -> list[str]:
    """Built entirely from config.settings (CITY / TARGET_COMPLETION_YEARS /
    MIN_PRICE_SEK / MAX_PRICE_SEK in .env) — nothing here is a hardcoded
    year or price, so changing .env actually changes what gets searched for.
    """
    years = settings.target_completion_years
    year_or = " OR ".join(str(y) for y in years)
    price_phrase = f"{_mkr(settings.min_price_sek)}-{_mkr(settings.max_price_sek)} miljoner"

    queries = []
    for year in years:
        queries.append(f"nyproduktion bostadsrätt {settings.city} inflyttning {year}")
        queries.append(f"nyproduktion lägenhet {settings.city} tillträde {year} {price_phrase}")

    for site in DEVELOPER_SITES:
        queries.append(f"site:{site} {settings.city} nyproduktion {year_or}")

    return queries


def search_all() -> list[dict]:
    """Run every query, return deduped {title, link, snippet} dicts."""
    if not settings.serpapi_key:
        raise SystemExit("SERPAPI_KEY not set — copy .env.example to .env and fill it in.")

    seen_links: set[str] = set()
    results: list[dict] = []

    for query in build_queries():
        log.info("SerpApi query: %s", query)
        params = {
            "engine": "google",
            "q": query,
            "location": "Stockholm, Sweden",
            "hl": "sv",
            "gl": "se",
            "num": 20,
            "api_key": settings.serpapi_key,
        }
        try:
            data = GoogleSearch(params).get_dict()
        except Exception:
            log.exception("SerpApi query failed: %s", query)
            continue

        for item in data.get("organic_results", []):
            link = item.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            results.append(
                {
                    "title": item.get("title", ""),
                    "link": link,
                    "snippet": item.get("snippet", ""),
                    "source_query": query,
                }
            )

    log.info("Found %d unique candidate URLs across all queries", len(results))
    return results

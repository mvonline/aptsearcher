"""Stage between extraction and scoring: pull real-world context for a
listing's address/area so the Claude scoring call isn't leaning purely on
the model's priors about Stockholm geography.

Reuses SERPAPI_KEY (no new API key needed):
- geocode()          SerpApi `google_maps` engine -> lat/lon
- nearest_transit()  SerpApi `google_maps` engine, haversine to nearest
                      tunnelbana/pendeltåg station found nearby
- area_context()      SerpApi `google` engine snippets on price/sqm and
                      safety/crime for the area -> short text blob for the
                      scoring prompt

Best-effort throughout: any failure here should degrade to "less context",
never block a listing from being scored. Each enrichment call costs SerpApi
search quota on top of the initial discovery queries in search.py — only
called for listings that already passed the relevance check, to keep that
cost down.
"""
from __future__ import annotations

import logging
import math

from serpapi import GoogleSearch

from config import settings

log = logging.getLogger(__name__)

# SerpApi's google_maps engine treats a multi-concept query like
# "tunnelbana pendeltåg station" as a place-name lookup and returns nothing
# — querying each transit mode separately and merging results is what
# actually returns nearby stations.
TRANSIT_QUERIES = ["tunnelbana", "pendeltåg"]


def _search(params: dict) -> dict:
    return GoogleSearch({**params, "api_key": settings.serpapi_key}).get_dict()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode(query: str) -> dict | None:
    """Resolve an address/place string to coordinates via SerpApi's Maps engine."""
    if not query:
        return None
    try:
        data = _search({"engine": "google_maps", "q": f"{query}, {settings.city}", "type": "search"})
    except Exception:
        log.exception("geocode() failed for %r", query)
        return None

    place = data.get("place_results")
    if not place:
        locals_ = data.get("local_results") or []
        place = locals_[0] if locals_ else None
    if not place or "gps_coordinates" not in place:
        log.info("No geocode match for %r", query)
        return None

    coords = place["gps_coordinates"]
    return {
        "lat": coords.get("latitude"),
        "lon": coords.get("longitude"),
        "matched_title": place.get("title"),
    }


def nearest_transit(lat: float, lon: float) -> dict | None:
    """Nearest tunnelbana/pendeltåg station to a point, via SerpApi Maps search."""
    best = None
    for query in TRANSIT_QUERIES:
        try:
            data = _search(
                {
                    "engine": "google_maps",
                    "q": query,
                    "ll": f"@{lat},{lon},15z",
                    "type": "search",
                }
            )
        except Exception:
            log.exception("nearest_transit() query %r failed for %s,%s", query, lat, lon)
            continue

        for c in data.get("local_results") or []:
            coords = c.get("gps_coordinates")
            if not coords:
                continue
            dist = _haversine_m(lat, lon, coords["latitude"], coords["longitude"])
            if best is None or dist < best["distance_m"]:
                best = {"name": c.get("title"), "distance_m": round(dist), "type": query}

    if not best:
        log.info("No transit stations found near %s,%s", lat, lon)
    return best


def area_context(area: str) -> str:
    """Short text blob of price/sqm and safety snippets for an area, for the LLM prompt."""
    if not area:
        return ""

    queries = {
        "Price/sqm context": f"pris per kvadratmeter bostadsrätt {area} {settings.city}",
        "Safety/crime context": f"brottsstatistik trygghet {area} {settings.city}",
    }

    sections = []
    for label, query in queries.items():
        try:
            data = _search({"engine": "google", "q": query, "hl": "sv", "gl": "se", "num": 3})
        except Exception:
            log.exception("area_context() query failed: %s", query)
            continue

        snippets = [
            r["snippet"]
            for r in data.get("organic_results", [])
            if r.get("snippet")
        ][:3]
        if snippets:
            sections.append(f"{label} ({area}):\n" + "\n".join(f"- {s}" for s in snippets))

    return "\n\n".join(sections)


def enrich(address: str | None, area: str | None) -> dict:
    """Best-effort bundle of everything above for one listing.

    Returns a dict always containing "context_text" (possibly empty) and
    optionally "lat"/"lon"/"nearest_transit" when geocoding succeeded.
    """
    result: dict = {"context_text": area_context(area or address or "")}

    geo = geocode(address or area or "")
    if geo and geo.get("lat") is not None:
        result["lat"] = geo["lat"]
        result["lon"] = geo["lon"]
        transit = nearest_transit(geo["lat"], geo["lon"])
        if transit:
            result["nearest_transit"] = transit

    return result


def format_for_prompt(enrichment: dict) -> str:
    """Render an enrich() result as plain text to splice into the scoring prompt."""
    parts = []
    transit = enrichment.get("nearest_transit")
    if transit:
        parts.append(
            f"Nearest transit station: {transit['name']} ({transit['type']}), "
            f"~{transit['distance_m']}m away."
        )
    if enrichment.get("context_text"):
        parts.append(enrichment["context_text"])
    return "\n\n".join(parts) if parts else "(No external context found — score based on general knowledge.)"

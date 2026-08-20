"""Stage 3: two Claude calls per candidate.

1. extract_listing() — read the raw scraped text, pull out structured
   fields, judge relevance. Cheap, no external context needed.
2. score_listing()   — given those fields plus real-world enrichment
   (src/enrich.py: geocoded transit distance, price/sqm and safety
   snippets), score the four ranking dimensions.

Split in two (rather than one combined call) so enrichment — which needs
the address/area extracted in step 1 — can sit in between, and so
irrelevant listings never pay for a scoring call at all.
`analyze_listing()` orchestrates all three steps and is what run.py calls;
its return shape is unchanged from the original single-call version.
"""
from __future__ import annotations

import json
import logging

import anthropic
import httpx

from config import settings
from src import enrich

log = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None  # lazy — only needed for provider="anthropic"


def _anthropic_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise SystemExit("ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in.")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def ensure_provider_ready() -> None:
    """Fail fast with a clear message if the configured LLM provider isn't usable."""
    if settings.llm_provider == "ollama":
        try:
            resp = httpx.get(f"{settings.ollama_host}/api/tags", timeout=5)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"LLM_PROVIDER=ollama but couldn't reach {settings.ollama_host} ({exc}). "
                f"Is `ollama serve` running?"
            )
        names = [m.get("name") for m in resp.json().get("models", [])]
        if settings.ollama_model not in names:
            raise SystemExit(
                f"Model {settings.ollama_model!r} not found in `ollama list` ({names}). "
                f"Run `ollama pull {settings.ollama_model}` or fix OLLAMA_MODEL in .env."
            )
    elif not settings.anthropic_api_key:
        raise SystemExit("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")

EXTRACT_TOOL_NAME = "submit_listing_fields"
SCORE_TOOL_NAME = "submit_listing_scores"

EXTRACT_TOOL_SCHEMA = {
    "name": EXTRACT_TOOL_NAME,
    "description": "Structured field extraction from one apartment listing page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_relevant": {
                "type": "boolean",
                "description": (
                    "True only if this is a genuine new-build apartment "
                    "(nyproduktion) in the Stockholm region, priced roughly "
                    "in the target band, with completion/tillträde in one "
                    "of the target years. False for anything else "
                    "(resale, wrong city, wrong price, a listing-search "
                    "page rather than a single unit, etc.)."
                ),
            },
            "address": {"type": "string"},
            "area": {"type": "string", "description": "Stockholm district/delområde, e.g. Hammarby Sjöstad"},
            "developer": {"type": "string"},
            "price_sek": {"type": "number"},
            "size_sqm": {"type": "number"},
            "rooms": {"type": "number"},
            "completion_year": {"type": "integer"},
            "security_features": {
                "type": "string",
                "description": "Free text: concierge, secure entry, cameras, lås/kod, etc. if mentioned.",
            },
        },
        "required": ["is_relevant", "address", "area", "price_sek", "size_sqm", "completion_year"],
    },
}

SCORE_TOOL_SCHEMA = {
    "name": SCORE_TOOL_NAME,
    "description": "Score one already-extracted apartment listing on four ranking dimensions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "location_score": {
                "type": "number",
                "description": "0-100. Transit access, proximity to center/water/green space, area desirability.",
            },
            "investment_score": {
                "type": "number",
                "description": "0-100. Price/sqm vs. what you'd expect for the area, appreciation potential, rental yield potential.",
            },
            "security_score": {
                "type": "number",
                "description": "0-100. Building security features plus general area safety reputation.",
            },
            "feature_price_score": {
                "type": "number",
                "description": "0-100. Size/rooms/amenities/energy-class relative to price, for this price band.",
            },
            "rationale": {
                "type": "string",
                "description": "2-4 sentences in English justifying the scores above.",
            },
        },
        "required": ["location_score", "investment_score", "security_score", "feature_price_score", "rationale"],
    },
}

EXTRACT_SYSTEM_PROMPT = f"""You are a real-estate analyst screening new-build
apartments in {settings.city}, Sweden, for a personal investment search.

Target criteria: price between {settings.min_price_sek:,} and
{settings.max_price_sek:,} SEK, completion year in
{settings.target_completion_years}.

You will be given the raw scraped text of one listing page. Extract the
facts you can find and mark is_relevant=false if this clearly doesn't
match the target criteria or isn't a single specific unit for sale. Never
invent a price or address — leave a field out if it isn't stated."""

SCORE_SYSTEM_PROMPT = f"""You are a real-estate analyst scoring one
already-identified new-build apartment in {settings.city}, Sweden, on four
dimensions, 0-100 each. You are given the extracted listing fields and
some real-world context (nearest transit station distance, price/sqm and
safety snippets for the area, gathered separately). Weigh the provided
context over your own priors when they conflict — it's more current."""


def _tool_call(system: str, tool: dict, user_content: str) -> dict | None:
    if settings.llm_provider == "ollama":
        return _tool_call_ollama(system, tool, user_content)
    return _tool_call_anthropic(system, tool, user_content)


def _tool_call_anthropic(system: str, tool: dict, user_content: str) -> dict | None:
    try:
        response = _anthropic_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError:
        log.exception("Claude call failed (%s)", tool["name"])
        return None

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input

    log.warning("No tool_use block in Claude response for %s", tool["name"])
    return None


def _tool_call_ollama(system: str, tool: dict, user_content: str) -> dict | None:
    # No forced-tool-call concept for local models — instead constrain
    # decoding directly to the JSON schema via Ollama's `format` param.
    try:
        resp = httpx.post(
            f"{settings.ollama_host}/api/chat",
            json={
                "model": settings.ollama_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                "format": tool["input_schema"],
            },
            timeout=300,  # local inference on CPU/small GPU is slow — be generous
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        log.exception("Ollama call failed (%s)", tool["name"])
        return None

    content = resp.json().get("message", {}).get("content", "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        log.warning("Ollama returned non-JSON content for %s: %r", tool["name"], content[:300])
        return None


def extract_listing(url: str, page_text: str) -> dict | None:
    truncated = page_text[:12_000]  # keep token/context spend predictable per listing
    return _tool_call(EXTRACT_SYSTEM_PROMPT, EXTRACT_TOOL_SCHEMA, f"URL: {url}\n\nPage text:\n{truncated}")


def score_listing(url: str, fields: dict, page_excerpt: str, enrichment_text: str) -> dict | None:
    user_content = (
        f"URL: {url}\n\n"
        f"Extracted fields: {fields}\n\n"
        f"Real-world context:\n{enrichment_text}\n\n"
        f"Listing page excerpt:\n{page_excerpt[:4000]}"
    )
    return _tool_call(SCORE_SYSTEM_PROMPT, SCORE_TOOL_SCHEMA, user_content)


def analyze_listing(url: str, page_text: str) -> dict | None:
    """Extract -> enrich -> score. Returns None only on outright API failure
    (distinct from a deliberate is_relevant=False), so callers can tell
    "couldn't analyze" apart from "analyzed and rejected".
    """
    fields = extract_listing(url, page_text)
    if fields is None:
        return None
    if not fields.get("is_relevant"):
        return fields  # caller only checks is_relevant; no need to score

    enrichment = enrich.enrich(fields.get("address"), fields.get("area"))
    enrichment_text = enrich.format_for_prompt(enrichment)

    scores = score_listing(url, fields, page_text, enrichment_text)
    if scores is None:
        return None

    return {**fields, **scores}

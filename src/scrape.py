"""Stage 2: fetch a candidate URL, reduce it to plain text for the LLM, and
(new) pull out same-domain sub-links so hub/listing pages — e.g.
besqab.se/stockholm/, which links to ~70 individual project pages instead
of containing listing details itself — can be expanded into their actual
project pages by run.py.

Deliberately does NOT hand-roll per-site CSS selectors for the *content*
of a listing page — those break every time a developer redesigns their
site. Instead we hand Claude the cleaned page text and a JSON schema in
llm.py and let it do the structured extraction. Cheaper to maintain for a
personal project; revisit with real selectors only if LLM extraction
proves unreliable for a specific site. Hub-link discovery below is
different: it's just "which anchors point deeper into the same domain",
which is structural, not content, and holds up fine across redesigns.
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Below this many characters of extracted text, the page is almost
# certainly JS-rendered (Hemnet/Booli detail pages, mainly) and httpx just
# saw the empty shell. Logged so you know which sites need the Playwright
# fallback below.
MIN_USEFUL_TEXT_CHARS = 400

# A page with at least this many distinct same-domain "deeper" links is
# treated as a hub/listing-index page rather than a single unit's page.
HUB_LINK_THRESHOLD = 5


# Content-types that are definitely not HTML — skip parsing these outright
# rather than handing binary bytes to BeautifulSoup (it can raise on
# garbage numeric character references, which used to take the whole run
# down — see the try/except below for the belt-and-suspenders case where
# content-type lies).
_NON_HTML_PREFIXES = ("application/pdf", "application/octet-stream", "application/zip", "image/", "video/", "audio/")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def _get(url: str) -> httpx.Response | None:
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp
    except httpx.HTTPError:
        log.warning("Fetch failed: %s", url)
        return None


def fetch_html(url: str) -> BeautifulSoup | None:
    resp = _get(url)
    if resp is None:
        return None

    content_type = resp.headers.get("content-type", "").lower()
    if content_type.startswith(_NON_HTML_PREFIXES):
        log.info("Skipping non-HTML content-type %r: %s", content_type, url)
        return None

    try:
        return BeautifulSoup(resp.text, "html.parser")
    except Exception:
        # A single malformed page must never take the whole run down.
        log.warning("HTML parse failed for %s (content-type=%r)", url, content_type)
        return None


def visible_text(soup: BeautifulSoup) -> str:
    # Mutates the soup (tags removed in place) — fine since each fetched
    # page is only inspected once per call site.
    for tag in soup.select("script, style, nav, footer, header"):
        tag.decompose()
    body = soup.body or soup
    return body.get_text(separator=" ", strip=True)


def text_from_soup(url: str, soup: BeautifulSoup) -> str | None:
    """visible_text() + the JS-rendered-page fallback, for callers that
    already have a `soup` (e.g. run.py, which also needs it for
    subpage_links()) and shouldn't fetch the page twice.
    """
    text = visible_text(soup)
    if len(text) < MIN_USEFUL_TEXT_CHARS:
        log.info(
            "Only got %d chars from %s — likely JS-rendered, "
            "enable fetch_text_playwright() for this domain if it matters.",
            len(text),
            url,
        )
        return fetch_text_playwright(url) or text or None
    return text


def fetch_text(url: str) -> str | None:
    soup = fetch_html(url)
    if soup is None:
        return None
    return text_from_soup(url, soup)


# A city hub page (/stockholm/, depth 1) links to sibling PROJECT pages two
# segments deeper (/stockholm/aspudden/aspen/, depth 3). A single project's
# own page links to ITS internal sub-pages (floor plans, unit numbers,
# gallery — e.g. /stockholm/taby/britas-bersa/9929/) only one segment
# deeper. Requiring a jump of >=2 is what tells those apart — a jump of 1
# alone would treat every project page's own internal nav as "a hub" and
# expand it into an internal-page crawl instead of finding sibling projects.
MIN_HUB_DEPTH_JUMP = 2


def subpage_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Same-domain links that point >=2 path segments deeper than base_url.

    This is what turns a developer's city hub page (one URL, e.g.
    /stockholm/) into its ~dozens of individual project pages
    (/stockholm/aspudden/aspen/, ...) for run.py to queue up and process
    individually. Fragment-only variants of the same URL (`#project-viewings`)
    are deduped away.
    """
    base = urlparse(base_url)
    base_depth = len([p for p in base.path.split("/") if p])

    seen: set[str] = set()
    links: list[str] = []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)._replace(fragment="")
        clean = parsed.geturl()

        if parsed.netloc != base.netloc:
            continue
        depth = len([p for p in parsed.path.split("/") if p])
        if depth < base_depth + MIN_HUB_DEPTH_JUMP:
            continue
        if clean in seen or clean == base_url:
            continue
        seen.add(clean)
        links.append(clean)

    return links


def is_hub_page(links: list[str]) -> bool:
    return len(links) >= HUB_LINK_THRESHOLD


def fetch_text_playwright(url: str) -> str | None:
    """Fallback for JS-rendered pages (Hemnet, Booli detail pages).

    Not wired in by default — uncomment the playwright dependency in
    requirements.txt, run `playwright install chromium` once, then
    uncomment the body below.
    """
    # from playwright.sync_api import sync_playwright
    #
    # with sync_playwright() as p:
    #     browser = p.chromium.launch()
    #     page = browser.new_page(user_agent=HEADERS["User-Agent"])
    #     page.goto(url, timeout=20_000, wait_until="networkidle")
    #     text = page.inner_text("body")
    #     browser.close()
    #     return text
    return None

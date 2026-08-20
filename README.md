# Apt Scout

Periodically searches for new-build ("nyproduktion") apartments in
Stockholm, priced 5–6M SEK with completion in 2028/2029, scores each on
**location**, **investment value**, **security**, and **feature-per-price**
using Claude, and pushes a ranked digest to Telegram. Runs as a plain
Python script from cron — no server, no framework.

## Scope note

This is a personal investment-research tool over **public real-estate
listings** — no customer PII, no credit/KYC data, no decisioning about a
person's creditworthiness. It scores *apartments*, not *people*, so it sits
outside LBF/KYC-AML/EU AI Act Annex III §5 concerns (those apply to scoring
individuals, not properties). Keep it that way: don't feed this pipeline
any E2E customer or production data — it's a fully separate, personal
project.

## Architecture

```
cron (e.g. daily 07:00)
   └─ run.py
        ├─ search.py      SerpApi google engine — generic queries + site:
        │                  filters per developer/aggregator
        ├─ scrape.py       httpx fetch + BeautifulSoup text extraction
        │                  (Playwright fallback stubbed for JS-heavy pages)
        ├─ llm.py          one Claude call per new URL: extract structured
        │                  fields + score the 4 ranking dimensions
        ├─ store.py        SQLite upsert, dedupe by URL hash, keeps history,
        │                  computes weighted composite score
        └─ notify.py       Telegram bot push — top-N unnotified listings,
                           ranked by composite score
```

Deliberately a **deterministic pipeline, not an agent framework** — search
→ filter → scrape → enrich → score → rank → notify is fixed control flow
with exactly one non-deterministic step (the Claude scoring call). Adding
an agent framework on top would add abstraction without buying anything.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.x, venv | Best fit for scraping + data glue |
| Search | SerpApi (`google` engine) | Already decided; also has a `google_maps` engine for future POI/transit enrichment |
| Fetch/scrape | `httpx` + `beautifulsoup4` (stdlib `html.parser` backend) | No compiled C extension — `selectolax` was tried first but its wheel doesn't build on Python 3.14 yet; bs4 does the same job with zero build step |
| Extraction + scoring | Claude API (`claude-sonnet-5`), forced JSON tool-call | One call does structured extraction *and* the 4-dimension scoring — avoids hand-rolling brittle per-site CSS selectors that break on every redesign |
| Storage | SQLite (`data/listings.db`) | Dedupe by URL hash, keeps price/score history, single-file, no server needed |
| Scheduling | System `cron` | See "Decisions" below |
| Notification | Telegram Bot API via `httpx` (not `python-telegram-bot` — that library is async-only in v21+, unnecessary for one sync API call) | See "Decisions" below |
| Orchestration | Plain Python script (`run.py`), no framework | See "Decisions" below |

## Decisions made along the way

- **Hermes** (NousResearch's open-weight model line) — considered, rejected.
  It's an LLM, not an orchestration platform — no search/scrape/schedule/notify
  tooling. It would only replace Claude at the scoring step, trading a
  simple API call for self-hosted inference infra with no upside here.
- **n8n** — considered, rejected for now. n8n's built-in Cron/HTTP/Telegram
  nodes and run-history UI are genuinely useful, but it has no solid
  built-in headless-browser node for JS-rendered sites (Hemnet/Booli), so
  scraping would still land in a Code node or a separate Python service —
  it doesn't remove that work, just relocates it. The scoring math (weighted
  composite, normalization, dedupe) is also easier to test and version in
  Python than in n8n's Code nodes. Revisit only if you want the visual
  run-history dashboard or plan to bolt on unrelated automations later.
- **Orchestration: plain Python + cron** (chosen over n8n-as-orchestrator).
  One script, one cron entry, logs to a file — simplest to run and debug
  for a single personal pipeline.
- **Scheduling: self-hosted script + cron** (chosen over a Claude Code
  cloud routine or a serverless function). Full control, no dependency on
  external scheduling infra.
- **Notification: Telegram** (chosen over email/Slack/report-only). Push to
  a bot for quick mobile checks.
- **Ranking weights** default to location 30% / investment 30% / security
  20% / feature-price 20% (`config.py`) — tune to taste.

## Scoring rubric (drives the Claude call in `src/llm.py`)

- **Location** — transit access (T-bana/pendeltåg), proximity to
  center/water/green space, area desirability trend.
- **Investment value** — price/sqm vs. area expectations, appreciation
  potential, rental yield potential.
- **Security** — building access control, concierge/lås/kamera, general
  area safety reputation.
- **Feature-per-price** — sqm, rooms, amenities, energy class, normalized
  against the 5–6M SEK band.

Each dimension is scored 0–100 by Claude with a short rationale, then
combined into the weighted composite score used for ranking.

## Data sources & legal note

Search covers generic Swedish new-build queries plus `site:` filters for:
JM, Bonava, Skanska, Veidekke, Oscar Properties, Aros Bostad, Besqab, HSB,
Riksbyggen, K2A, Balder, Wallenstam, Tobin Properties, SSM, plus Hemnet's
and Booli's nyproduktion sections (see `DEVELOPER_SITES` in
`src/search.py`).

Hemnet/Booli restrict scraping in their ToS. This pipeline leans on
SerpApi's indexed snippets for those two and treats direct developer sites
(generally scrape-friendly) as the primary source for full-page extraction.
Not legal advice — worth a periodic check of current ToS if this keeps
running unattended long-term.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SERPAPI_KEY, ANTHROPIC_API_KEY, TELEGRAM_*
python run.py          # first manual run
```

`run.py` fails fast with a clear message listing any missing config instead
of a cryptic API error mid-run.

## Scheduling

Add to crontab (`crontab -e`) to run daily at 07:00:

```
0 7 * * * cd /home/masoud/personal-project/newProdApt && venv/bin/python run.py >> run.log 2>&1
```

## Known limitations / next steps

- **No geocoding/crime-data/price-benchmark enrichment yet** — location and
  investment scores currently come from Claude's general knowledge of
  Stockholm, not hard data. Natural v2: an `src/enrich.py` pulling transit
  distance (Google Maps Distance Matrix or SerpApi's `google_maps` engine),
  an area price/sqm benchmark (SCB or Booli/Hemnet valuation pages), and
  crime stats (Polisen.se open API) — fed into the `llm.analyze_listing`
  prompt instead of relying on the model's priors.
- **Playwright fallback for JS-rendered pages** (Hemnet/Booli detail pages)
  is stubbed in `src/scrape.py` but commented out — enable if those sources
  turn out to matter (`pip install playwright && playwright install
  chromium`, then uncomment `fetch_text_playwright`).
- Composite ranking weights are in `config.py` — tune to your priorities.

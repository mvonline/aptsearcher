"""Central config, loaded from .env. Import `settings` everywhere else."""
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


@dataclass(frozen=True)
class Settings:
    serpapi_key: str = os.getenv("SERPAPI_KEY", "")

    # "anthropic" (Claude API, needs anthropic_api_key + billing) or
    # "ollama" (local, free, slower — needs `ollama serve` running).
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma4:latest")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    db_path: Path = BASE_DIR / os.getenv("DB_PATH", "data/listings.db")

    city: str = os.getenv("CITY", "Stockholm")
    min_price_sek: int = _int("MIN_PRICE_SEK", 5_000_000)
    max_price_sek: int = _int("MAX_PRICE_SEK", 6_000_000)
    target_completion_years: tuple = tuple(
        int(y) for y in os.getenv("TARGET_COMPLETION_YEARS", "2028,2029").split(",")
    )
    top_n: int = _int("TOP_N", 10)

    # Bypasses llm.py entirely — run.py falls back to src/heuristics.py
    # (regex year/price scan) instead. Useful for checking what search+scrape
    # actually finds without spending LLM time/cost.
    skip_llm: bool = os.getenv("SKIP_LLM", "false").strip().lower() in ("1", "true", "yes")

    # Hub/listing pages (e.g. besqab.se/stockholm/, which links to ~70
    # individual project pages instead of being one itself) get expanded
    # into their sub-links. Cap total candidates processed per run so one
    # big hub can't make a run unbounded.
    max_candidates_per_run: int = _int("MAX_CANDIDATES_PER_RUN", 150)

    # Composite ranking weights — must sum to 1.0. Tune to taste.
    weight_location: float = 0.30
    weight_investment: float = 0.30
    weight_security: float = 0.20
    weight_feature_price: float = 0.20


settings = Settings()


def require_keys(*names: str) -> None:
    """Fail fast with a clear message instead of a cryptic API 401 later."""
    missing = [n for n in names if not getattr(settings, n)]
    if missing:
        raise SystemExit(
            f"Missing required config: {', '.join(missing)}. "
            f"Copy .env.example to .env and fill it in."
        )

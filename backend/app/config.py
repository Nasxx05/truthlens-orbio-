"""Runtime configuration, read from the environment.

Everything tunable about scraping lives here rather than being scattered as
literals through the scrapers, so scrape cost and politeness can be adjusted
without touching extraction logic.
"""

import os
from dataclasses import dataclass, field
from typing import List


def _int(name: str, default: int) -> int:
    """Read an int from the environment, falling back rather than crashing."""
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Scraper and service settings."""

    # --- Review collection budget ---
    # Hard cap on reviews per source. Bounds both scrape time and, later, the
    # number of reviews handed to the LLM.
    review_max: int = field(default_factory=lambda: _int("REVIEW_MAX", 150))
    # Pagination ceiling, independent of review_max: a site with 5 reviews per
    # page should not be crawled 30 times to reach the cap.
    max_pages: int = field(default_factory=lambda: _int("SCRAPE_MAX_PAGES", 12))
    # Minimum reviews for the result to be worth acting on. Below this the
    # endpoint reports not_enough_data instead of a thin, misleading summary.
    min_reviews: int = field(default_factory=lambda: _int("REVIEW_MIN", 5))

    # --- HTTP behaviour ---
    request_timeout: float = field(default_factory=lambda: _float("SCRAPE_TIMEOUT", 10.0))
    # Connecting should be fast even when the response is slow; a host that
    # will not complete a handshake is not going to serve reviews.
    connect_timeout: float = field(default_factory=lambda: _float("SCRAPE_CONNECT_TIMEOUT", 5.0))
    # Whole-scrape wall clock. A slow site should degrade to partial results
    # rather than hold the request open indefinitely.
    total_timeout: float = field(default_factory=lambda: _float("SCRAPE_TOTAL_TIMEOUT", 45.0))
    request_delay: float = field(default_factory=lambda: _float("SCRAPE_DELAY", 0.7))
    # Retries are per URL. Two attempts is the useful range: a site that
    # refuses twice is refusing, and further attempts just spend the budget
    # (a hanging host at 3 retries x 12s held requests open for 46s).
    max_retries: int = field(default_factory=lambda: _int("SCRAPE_RETRIES", 1))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "SCRAPE_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
    )

    # --- Competitor source ---
    # Competitor sites to try, in order. The site the shopper is already on is
    # skipped automatically, so a shopper on Newegg gets compared against the
    # next entry rather than against Newegg itself.
    competitor_sites: tuple = field(
        default_factory=lambda: tuple(
            name.strip().lower()
            for name in (os.getenv("COMPETITOR_SITES", "newegg,bestbuy,ebay") or "").split(",")
            if name.strip()
        )
    )
    # Minimum match score for competitor reviews to be included. Below this the
    # source is omitted: reviews of a different product are worse than none.
    match_threshold: float = field(default_factory=lambda: _float("MATCH_THRESHOLD", 0.62))
    # Competitor collection runs concurrently with the host, but is capped so a
    # slow competitor cannot hold up a finished host result. Lowered from 25s:
    # a competitor search that hasn't matched/scraped by 15s rarely pays off,
    # and burning the full 25s on a miss is a real contributor to requests
    # that blow past the client's timeout on a cold instance.
    competitor_timeout: float = field(default_factory=lambda: _float("COMPETITOR_TIMEOUT", 15.0))
    competitor_review_max: int = field(default_factory=lambda: _int("COMPETITOR_REVIEW_MAX", 75))

    # --- Video discovery ---
    # Videos shorter than this are clips or shorts, not reviews.
    video_min_seconds: int = field(default_factory=lambda: _int("VIDEO_MIN_SECONDS", 120))
    video_max_results: int = field(default_factory=lambda: _int("VIDEO_MAX_RESULTS", 6))
    video_relevance_threshold: float = field(
        default_factory=lambda: _float("VIDEO_RELEVANCE_THRESHOLD", 0.45)
    )
    video_timeout: float = field(default_factory=lambda: _float("VIDEO_TIMEOUT", 15.0))
    tiktok_enabled: bool = field(default_factory=lambda: _bool("TIKTOK_ENABLED", False))

    # --- Review filtering ---
    filter_enabled: bool = field(default_factory=lambda: _bool("FILTER_ENABLED", True))
    # Suspicion score at or above which a review fails filtering.
    filter_threshold: float = field(default_factory=lambda: _float("FILTER_THRESHOLD", 0.5))
    # Shingle overlap at which two reviews count as near-duplicates.
    duplicate_similarity: float = field(default_factory=lambda: _float("DUPLICATE_SIMILARITY", 0.55))
    # Precise dates needed before submission clustering is judged at all.
    pacing_min_dated: int = field(default_factory=lambda: _int("PACING_MIN_DATED", 8))

    # --- LLM summarization ---
    # Provider is swappable: "anthropic", "openai", or "" to auto-select
    # whichever has credentials configured.
    llm_provider: str = field(default_factory=lambda: (os.getenv("LLM_PROVIDER", "") or "").strip().lower())
    # Model defaults to Anthropic's current flagship. Cheaper options exist and
    # are a cost decision for the operator, not a default this code should make
    # on their behalf — see the README for the pricing table.
    anthropic_model: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
    )
    # No default: this codebase has not verified any particular OpenAI model
    # id, and guessing one produces a confusing 404 at request time instead of
    # a clear "you have not chosen a model".
    openai_model: str = field(default_factory=lambda: (os.getenv("OPENAI_MODEL", "") or "").strip())
    # Tried when the primary model errors (rate limit, upstream overload, bad
    # response shape, etc). Empty means no fallback — a primary failure is
    # just a failure, as before.
    openai_fallback_model: str = field(
        default_factory=lambda: (os.getenv("OPENAI_FALLBACK_MODEL", "") or "").strip()
    )
    # Empty means OpenAI's own API. Set to an OpenAI-compatible proxy (e.g.
    # https://openrouter.ai/api/v1) to route the "openai" provider through it
    # instead — useful for a provider whose only credential is a proxy key.
    openai_base_url: str = field(default_factory=lambda: (os.getenv("OPENAI_BASE_URL", "") or "").strip())
    # Reasoning depth. Unset means the API default (high). Lowering it is the
    # first cost lever worth pulling for a routine summarization workload.
    llm_effort: str = field(default_factory=lambda: (os.getenv("LLM_EFFORT", "") or "").strip().lower())
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 16000))
    llm_timeout: float = field(default_factory=lambda: _float("LLM_TIMEOUT", 90.0))
    # Request-size budget: how many reviews are sent, and how much of each.
    llm_max_reviews: int = field(default_factory=lambda: _int("LLM_MAX_REVIEWS", 60))
    llm_review_chars: int = field(default_factory=lambda: _int("LLM_REVIEW_CHARS", 700))
    # Reviews needed before a verdict may claim high confidence. Enforced in
    # code as a ceiling, not merely requested in the prompt.
    llm_confident_min_reviews: int = field(
        default_factory=lambda: _int("LLM_CONFIDENT_MIN_REVIEWS", 12)
    )

    # --- Cache (PostgreSQL) ---
    cache_enabled: bool = field(default_factory=lambda: _bool("CACHE_ENABLED", True))
    database_url: str = field(default_factory=lambda: (os.getenv("DATABASE_URL", "") or "").strip())
    cache_ttl_hours: int = field(default_factory=lambda: _int("CACHE_TTL_HOURS", 24))
    # A thin or failed result reflects a transient problem more often than a
    # real one, so it expires sooner rather than pinning a product for a day.
    cache_ttl_thin_hours: int = field(default_factory=lambda: _int("CACHE_TTL_THIN_HOURS", 1))
    db_pool_size: int = field(default_factory=lambda: _int("DB_POOL_SIZE", 5))
    db_timeout: float = field(default_factory=lambda: _float("DB_TIMEOUT", 8.0))

    # --- Task queue (Redis) ---
    queue_enabled: bool = field(default_factory=lambda: _bool("QUEUE_ENABLED", True))
    redis_url: str = field(default_factory=lambda: (os.getenv("REDIS_URL", "") or "").strip())
    redis_timeout: float = field(default_factory=lambda: _float("REDIS_TIMEOUT", 5.0))
    # How long a client will follow a job before giving up, and the ceiling on
    # how long a crashed worker can hold a product's in-flight lock.
    job_timeout: float = field(default_factory=lambda: _float("JOB_TIMEOUT", 120.0))
    job_result_ttl: int = field(default_factory=lambda: _int("JOB_RESULT_TTL", 300))
    job_stream_maxlen: int = field(default_factory=lambda: _int("JOB_STREAM_MAXLEN", 200))
    # Concurrent analyses per worker. This is the real backpressure control:
    # the queue absorbs a burst instead of the process trying to scrape
    # everything at once.
    worker_concurrency: int = field(default_factory=lambda: _int("WORKER_CONCURRENCY", 4))

    # --- Security ---
    # Whether the backend may fetch private, loopback or link-local addresses.
    # False in production: leaving it on turns this service into an SSRF proxy
    # for anything inside its network. True only for local fixtures.
    allow_private_targets: bool = field(
        default_factory=lambda: _bool("ALLOW_PRIVATE_TARGETS", False)
    )
    dns_timeout: float = field(default_factory=lambda: _float("DNS_TIMEOUT", 3.0))
    # Requests per window, per client. Analysis is expensive, so the default is
    # deliberately low.
    rate_limit: int = field(default_factory=lambda: _int("RATE_LIMIT", 20))
    rate_limit_window: int = field(default_factory=lambda: _int("RATE_LIMIT_WINDOW", 60))
    # Only enable behind a proxy that overwrites X-Forwarded-For; otherwise a
    # caller can forge it and bypass the limit.
    trust_proxy_headers: bool = field(default_factory=lambda: _bool("TRUST_PROXY_HEADERS", False))
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", 16384))

    # --- Politeness ---
    # When true, a path disallowed by the site's robots.txt is not fetched.
    # Amazon disallows /product-reviews/, so leaving this on means Amazon
    # review pagination is skipped and only on-page reviews are collected.
    respect_robots: bool = field(default_factory=lambda: _bool("RESPECT_ROBOTS", True))

    # --- Optional browser rendering ---
    # Playwright is only used when a page yields nothing over plain HTTP. It is
    # an optional dependency; absent, the scraper stays on the HTTP path.
    playwright_enabled: bool = field(default_factory=lambda: _bool("PLAYWRIGHT_ENABLED", True))
    playwright_timeout: float = field(default_factory=lambda: _float("PLAYWRIGHT_TIMEOUT", 25.0))

    @property
    def default_headers(self) -> dict:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }


settings = Settings()

__all__: List[str] = ["Settings", "settings"]

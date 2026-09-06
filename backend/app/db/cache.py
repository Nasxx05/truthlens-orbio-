"""PostgreSQL cache for completed analyses.

A full analysis costs several seconds of scraping and a paid LLM call, and the
answer barely changes hour to hour. Caching it is the difference between a
usable extension and one nobody waits for twice.

Two rules shape this module:

  * **The cache can never break a request.** Every operation is wrapped: an
    unreachable database, a missing table, or a malformed row degrades to a
    miss and the analysis runs live. A cache that takes the service down with
    it is worse than no cache.
  * **A thin result expires sooner than a good one.** Caching
    ``not_enough_data`` for a full day would pin a transient failure — a
    blocked scrape, a rate-limited API — to a product for 24 hours. Those get
    their own short TTL.
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key    TEXT PRIMARY KEY,
    product_id   TEXT,
    site         TEXT,
    product_name TEXT,
    canonical_url TEXT,
    status       TEXT NOT NULL,
    payload      JSONB NOT NULL,
    review_count INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0
);

-- Expiry sweeps and the hit lookup are the only access patterns.
CREATE INDEX IF NOT EXISTS analysis_cache_expires_idx ON analysis_cache (expires_at);
CREATE INDEX IF NOT EXISTS analysis_cache_product_idx ON analysis_cache (site, product_id);
"""


def cache_key(
    *,
    product_id: Optional[str] = None,
    site: Optional[str] = None,
    canonical_url: Optional[str] = None,
    product_name: Optional[str] = None,
) -> Optional[str]:
    """Stable identity for a product, or ``None`` if it cannot be identified.

    Preference order matters. A site-scoped product id is exact. A canonical
    URL is nearly as good once tracking parameters are gone. A product name is
    the weakest — two shoppers typing the same name should share a cache entry,
    but only after normalization, or "Sony WH-1000XM5 " and "sony wh1000xm5"
    would be different products.
    """
    if product_id and site:
        basis = f"id:{site.strip().lower()}:{product_id.strip().upper()}"
    elif product_id:
        basis = f"id:{product_id.strip().upper()}"
    elif canonical_url:
        try:
            parsed = urlparse(canonical_url)
            host = (parsed.netloc or "").lower()
            host = host[4:] if host.startswith("www.") else host
            path = (parsed.path or "/").rstrip("/")
            basis = f"url:{host}{path}"
        except Exception:
            basis = f"url:{canonical_url.strip().lower()}"
    elif product_name:
        # Collapse case, punctuation and spacing so trivially different
        # spellings of the same product share an entry.
        normalized = re.sub(r"[^a-z0-9]+", " ", product_name.lower()).strip()
        if len(normalized) < 4:
            return None
        basis = f"name:{normalized}"
    else:
        return None

    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass
class CachedAnalysis:
    """A cache hit."""

    payload: dict
    created_at: str
    expires_at: str
    age_seconds: int
    hits: int


class AnalysisCache:
    """Connection pool plus the two queries this service needs."""

    def __init__(self) -> None:
        self._pool = None
        self._ready = False
        self._warned = False

    async def connect(self) -> bool:
        """Open the pool and ensure the schema. Safe to call repeatedly."""
        if self._ready:
            return True
        if not settings.cache_enabled or not settings.database_url:
            return False

        try:
            import asyncpg
        except ImportError:
            self._warn("asyncpg is not installed; caching disabled")
            return False

        try:
            self._pool = await asyncpg.create_pool(
                settings.database_url,
                min_size=1,
                max_size=settings.db_pool_size,
                timeout=settings.db_timeout,
                command_timeout=settings.db_timeout,
            )
            async with self._pool.acquire() as connection:
                await connection.execute(SCHEMA)
            self._ready = True
            logger.info("analysis cache ready (ttl %sh)", settings.cache_ttl_hours)
            return True
        except Exception as error:
            self._warn(f"cache unavailable ({type(error).__name__}: {error}); running without it")
            self._pool = None
            return False

    def _warn(self, message: str) -> None:
        """Log a cache problem once, not on every request."""
        if not self._warned:
            logger.warning("%s", message)
            self._warned = True
        else:
            logger.debug("%s", message)

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                pass
        self._pool = None
        self._ready = False

    @property
    def available(self) -> bool:
        return self._ready and self._pool is not None

    async def get(self, key: str) -> Optional[CachedAnalysis]:
        """Fetch a live entry, or ``None`` for a miss.

        Expiry is enforced in the query rather than by a sweeper, so a stale
        row can never be served even if cleanup has not run.
        """
        if not key or not await self.connect():
            return None

        try:
            async with self._pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    UPDATE analysis_cache
                       SET hits = hits + 1
                     WHERE cache_key = $1
                       AND expires_at > now()
                 RETURNING payload, created_at, expires_at, hits,
                           EXTRACT(EPOCH FROM (now() - created_at))::int AS age
                    """,
                    key,
                )
        except Exception as error:
            self._warn(f"cache read failed ({type(error).__name__}); treating as a miss")
            return None

        if row is None:
            return None

        try:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception:
            logger.warning("cache row %s held unreadable payload; ignoring", key[:8])
            return None

        return CachedAnalysis(
            payload=payload,
            created_at=row["created_at"].isoformat(),
            expires_at=row["expires_at"].isoformat(),
            age_seconds=int(row["age"] or 0),
            hits=int(row["hits"] or 0),
        )

    async def put(
        self,
        key: str,
        payload: dict,
        *,
        status: str,
        product_id: Optional[str] = None,
        site: Optional[str] = None,
        product_name: Optional[str] = None,
        canonical_url: Optional[str] = None,
        review_count: int = 0,
    ) -> bool:
        """Store a completed analysis."""
        if not key or not await self.connect():
            return False

        # A thin or failed result gets a short life: it usually reflects a
        # transient problem, and pinning it for a day would make the product
        # look permanently unanalysable.
        hours = settings.cache_ttl_hours if status == "ok" else settings.cache_ttl_thin_hours

        try:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    INSERT INTO analysis_cache
                        (cache_key, product_id, site, product_name, canonical_url,
                         status, payload, review_count, created_at, expires_at, hits)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, now(),
                            now() + ($9 || ' hours')::interval, 0)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        status = EXCLUDED.status,
                        review_count = EXCLUDED.review_count,
                        product_name = COALESCE(EXCLUDED.product_name, analysis_cache.product_name),
                        canonical_url = COALESCE(EXCLUDED.canonical_url, analysis_cache.canonical_url),
                        created_at = now(),
                        expires_at = EXCLUDED.expires_at,
                        hits = 0
                    """,
                    key, product_id, site, product_name, canonical_url,
                    status, json.dumps(payload, default=str), review_count, str(hours),
                )
            logger.info("cached %s (%s, %sh ttl)", key[:8], status, hours)
            return True
        except Exception as error:
            # A failed write is a missed optimization, not a failed request.
            self._warn(f"cache write failed ({type(error).__name__}: {error})")
            return False

    async def invalidate(self, key: str) -> bool:
        """Drop one entry, for a forced refresh."""
        if not key or not await self.connect():
            return False
        try:
            async with self._pool.acquire() as connection:
                await connection.execute("DELETE FROM analysis_cache WHERE cache_key = $1", key)
            return True
        except Exception:
            return False

    async def purge_expired(self) -> int:
        """Delete expired rows. Expiry is already enforced on read; this only
        reclaims space."""
        if not await self.connect():
            return 0
        try:
            async with self._pool.acquire() as connection:
                result = await connection.execute(
                    "DELETE FROM analysis_cache WHERE expires_at <= now()"
                )
            deleted = int((result or "DELETE 0").split()[-1])
            if deleted:
                logger.info("purged %s expired cache row(s)", deleted)
            return deleted
        except Exception:
            return 0

    async def stats(self) -> dict:
        """Counts for the health endpoint."""
        if not await self.connect():
            return {"available": False}
        try:
            async with self._pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT count(*) AS total,
                           count(*) FILTER (WHERE expires_at > now()) AS live,
                           coalesce(sum(hits), 0) AS hits
                      FROM analysis_cache
                    """
                )
            return {
                "available": True,
                "entries": int(row["total"]),
                "live": int(row["live"]),
                "hits_served": int(row["hits"]),
            }
        except Exception:
            return {"available": False}


cache = AnalysisCache()

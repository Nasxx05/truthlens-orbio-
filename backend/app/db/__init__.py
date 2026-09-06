"""Persistence.

    cache.py   PostgreSQL cache of completed analyses

The cache is optional by design: every operation degrades to a miss when the
database is unreachable, so the service runs with or without it.
"""

from app.db.cache import AnalysisCache, CachedAnalysis, cache, cache_key

__all__ = ["AnalysisCache", "CachedAnalysis", "cache", "cache_key"]

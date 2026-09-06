"""Video review discovery.

    base.py        normalized Video / VideoResult types
    relevance.py   is this video actually about this product?
    youtube.py     official YouTube Data API v3
    tiktok.py      scraping, fully isolated

Each platform is independent: one returning nothing, failing, or being blocked
does not affect the other or the request as a whole.
"""

from app.services.videos.base import Video, VideoResult

__all__ = ["Video", "VideoResult"]

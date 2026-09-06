"""Shared types for video discovery."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Video:
    """One review video, normalized across platforms."""

    title: str
    url: str
    source: str                              # platform: youtube | tiktok
    channel: Optional[str] = None
    channel_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    published: Optional[str] = None           # ISO-8601 where known
    views: Optional[int] = None
    thumbnail: Optional[str] = None
    relevance: Optional[float] = None         # title overlap with the product
    video_id: Optional[str] = None
    # Short, truncated snippet — the only text evidence the LLM gets from a
    # video when there are no scraped reviews to fall back on.
    description: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "channel": self.channel,
            "channel_url": self.channel_url,
            "duration_seconds": self.duration_seconds,
            "published": self.published,
            "views": self.views,
            "thumbnail": self.thumbnail,
            "relevance": round(self.relevance, 3) if self.relevance is not None else None,
            "video_id": self.video_id,
            "description": self.description,
        }


@dataclass
class VideoResult:
    """Outcome of querying one video platform.

    Mirrors ``ScrapeResult``: a platform that fails says so and the caller
    proceeds with whatever the others returned.
    """

    source: str
    videos: List[Video] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None
    blocked: bool = False
    considered: int = 0                       # results seen before filtering
    filtered_out: int = 0
    notes: List[str] = field(default_factory=list)
    duration_ms: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "count": len(self.videos),
            "error": self.error,
            "blocked": self.blocked,
            "considered": self.considered,
            "filtered_out": self.filtered_out,
            "notes": self.notes,
            "duration_ms": self.duration_ms,
        }

"""Redis connection management.

Redis is optional. When it is absent the API runs analyses inline, exactly as
it did before this layer existed — the queue is an optimization for load, not a
requirement for correctness. Availability is therefore checked rather than
assumed, and a connection failure downgrades the service instead of breaking
it.
"""

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class Broker:
    """Lazily-connected Redis client."""

    def __init__(self) -> None:
        self._client = None
        self._checked = False
        self._warned = False

    async def client(self):
        """A connected client, or ``None`` if Redis is unusable.

        The first call pings to confirm the server is really there — a client
        object constructs happily against a dead address, and discovering that
        mid-analysis is worse than discovering it up front.
        """
        if self._client is not None:
            return self._client
        if self._checked:
            return None
        self._checked = True

        if not settings.queue_enabled or not settings.redis_url:
            return None

        try:
            import redis.asyncio as redis
        except ImportError:
            self._warn("redis package not installed; running analyses inline")
            return None

        try:
            client = redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_timeout=settings.redis_timeout,
                socket_connect_timeout=settings.redis_timeout,
            )
            await client.ping()
        except Exception as error:
            self._warn(f"redis unavailable ({type(error).__name__}: {error}); running analyses inline")
            return None

        self._client = client
        logger.info("queue broker connected to redis")
        return client

    def set_client(self, client) -> None:
        """Inject a client. Used by tests to run against an in-process Redis."""
        self._client = client
        self._checked = True

    def _warn(self, message: str) -> None:
        if not self._warned:
            logger.warning("%s", message)
            self._warned = True
        else:
            logger.debug("%s", message)

    async def available(self) -> bool:
        return await self.client() is not None

    async def reset(self) -> None:
        """Forget the current client so the next call reconnects."""
        client = self._client
        self._client = None
        self._checked = False
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        await self.reset()


broker = Broker()

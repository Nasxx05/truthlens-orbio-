"""HTTP fetching for scrapers.

Concerns kept out of the extraction code: retries, timeouts, polite delays,
robots.txt, and recognizing a bot-protection page for what it is rather than
treating it as a page with no reviews.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings
from app.services.scrapers.robots import RobotsRules

logger = logging.getLogger(__name__)


@dataclass
class Fetched:
    """A fetched page, or the reason there isn't one."""

    url: str
    html: Optional[str] = None
    status: Optional[int] = None
    ok: bool = False
    blocked: bool = False
    error: Optional[str] = None
    # True when the host could not be reached at all (DNS, refused, timeout),
    # as opposed to answering with an error. The whole host is unreachable, so
    # the caller should stop trying other URLs on it.
    transport_error: bool = False


# Fingerprints of the interstitials retailers serve instead of content. These
# return HTTP 200 with a page that has no reviews on it, so without this check
# a block looks identical to a product nobody has reviewed.
_BLOCK_MARKERS = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "to discuss automated access to amazon data",
    "api-services-support@amazon.com",
    "robot check",
    "are you a human",
    "verify you are a human",
    "unusual traffic from your computer",
    "access denied",
    "request blocked",
    "captcha-delivery.com",
    "cf-browser-verification",
    "checking your browser before accessing",
    "px-captcha",
    "please enable javascript and cookies to continue",
)

_BLOCK_STATUSES = {401, 403, 407, 429, 503}


def looks_blocked(html: Optional[str], status: Optional[int]) -> bool:
    """Is this a bot wall rather than a real page?"""
    if status in _BLOCK_STATUSES:
        return True
    if not html:
        return False
    head = html[:6000].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)


class RobotsCache:
    """Per-host robots.txt, fetched once per process.

    Failing open is deliberate: a missing or unreachable robots.txt is not a
    disallow, and refusing to scrape because robots.txt 500'd would be its own
    kind of bug.
    """

    def __init__(self) -> None:
        self._parsers: Dict[str, Optional[RobotsRules]] = {}
        # One lock per event loop, created on first use. A module-level
        # asyncio.Lock() constructed at import time binds to whichever loop
        # exists then (Python <3.10), and using it from a task on another loop
        # raises "got Future attached to a different loop" — which took out the
        # whole competitor scrape the moment two sources ran concurrently.
        self._locks: Dict[object, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:  # pragma: no cover - no loop running
            return asyncio.Lock()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def allowed(self, url: str, client: httpx.AsyncClient) -> Tuple[bool, str]:
        if not settings.respect_robots:
            return True, "robots check disabled"

        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"

        async with self._lock():
            if host not in self._parsers:
                self._parsers[host] = await self._load(host, client)

        parser = self._parsers[host]
        if parser is None:
            return True, "robots.txt unavailable"

        try:
            if parser.allowed(settings.user_agent, url):
                return True, "allowed by robots.txt"
            return False, "disallowed by robots.txt"
        except Exception:
            return True, "robots.txt unparseable"

    async def _load(self, host: str, client: httpx.AsyncClient) -> Optional[RobotsRules]:
        try:
            response = await client.get(urljoin(host, "/robots.txt"), timeout=6.0)
            # 4xx means no robots.txt, which permits everything. A 5xx is the
            # server failing, not a disallow, so it also fails open.
            if response.status_code >= 400:
                return None
            return RobotsRules.parse(response.text)
        except Exception as error:
            logger.debug("robots.txt fetch failed for %s: %s", host, error)
            return None


robots = RobotsCache()


class Fetcher:
    """Async HTTP client with retries, backoff, and a polite inter-request delay.

    Used as a context manager so connections are reused across the pages of one
    scrape rather than reopened per request.
    """

    def __init__(self, referer: Optional[str] = None) -> None:
        self._referer = referer
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request = 0.0

    async def __aenter__(self) -> "Fetcher":
        headers = dict(settings.default_headers)
        if self._referer:
            headers["Referer"] = self._referer
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(
                settings.request_timeout,
                connect=settings.connect_timeout,
            ),
            follow_redirects=True,
            # Retailers gate content on cookies handed out by the first response.
            cookies=httpx.Cookies(),
        )
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._client:
            await self._client.aclose()

    async def allowed(self, url: str) -> bool:
        """Is ``url`` permitted by robots.txt?

        Exposed because the browser transport must honour the same rules as the
        HTTP path — a headless browser bypassing robots.txt would make the
        setting meaningless.
        """
        if self._client is None:
            return True
        permitted, _reason = await robots.allowed(url, self._client)
        return permitted

    async def get(self, url: str, *, check_robots: bool = True) -> Fetched:
        """Fetch one page. Never raises — failures come back as ``Fetched``."""
        if self._client is None:
            return Fetched(url=url, error="fetcher not started")

        if check_robots:
            allowed, reason = await robots.allowed(url, self._client)
            if not allowed:
                logger.info("skipping %s: %s", url, reason)
                return Fetched(url=url, blocked=True, error=reason)

        last_error: Optional[str] = None
        transport_failed = False

        for attempt in range(settings.max_retries + 1):
            await self._throttle()
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as error:
                last_error = f"{type(error).__name__}: {error}"
                transport_failed = True
                logger.debug("fetch failed (%s/%s) %s: %s", attempt + 1, settings.max_retries + 1, url, error)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            html = response.text
            status = response.status_code

            if looks_blocked(html, status):
                # Retrying a bot wall just annoys the server; it will not clear.
                logger.info("blocked at %s (HTTP %s)", url, status)
                return Fetched(
                    url=str(response.url),
                    html=html,
                    status=status,
                    blocked=True,
                    error=f"blocked by site (HTTP {status})",
                )

            if status >= 500:
                last_error = f"HTTP {status}"
                transport_failed = False
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if status >= 400:
                return Fetched(url=str(response.url), status=status, error=f"HTTP {status}")

            return Fetched(url=str(response.url), html=html, status=status, ok=True)

        return Fetched(
            url=url,
            error=last_error or "fetch failed",
            transport_error=transport_failed,
        )

    async def _throttle(self) -> None:
        """Keep at least ``request_delay`` between requests to one host."""
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < settings.request_delay:
            await asyncio.sleep(settings.request_delay - elapsed)
        self._last_request = time.monotonic()

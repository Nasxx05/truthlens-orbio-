"""Request hardening: rate limiting and target validation.

Two distinct risks, both arising from the same fact — this service fetches a
URL that a client supplied:

  * **Abuse.** Each request costs seconds of scraping and a paid LLM call, so
    an unthrottled endpoint is an invitation to run up someone's bill.
  * **Server-side request forgery.** ``product_url`` is fetched by the backend,
    which sits inside a network the caller does not. Left unchecked, a request
    for ``http://169.254.169.254/latest/meta-data/`` turns this service into a
    proxy for cloud credentials, and ``http://10.0.0.5/admin`` into a scanner
    for internal hosts. Hostnames are resolved before the decision, because
    ``evil.example.com`` resolving to ``127.0.0.1`` defeats any check that only
    looks at the string.
"""

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from app.config import settings
from app.queue.broker import broker

logger = logging.getLogger(__name__)

# Schemes worth fetching. Everything else — file://, gopher://, data:,
# ftp:// — has no legitimate use here and several have known SSRF tricks.
ALLOWED_SCHEMES = {"http", "https"}

# Cloud instance-metadata addresses. Blocked by the private-range checks
# below too, but named explicitly because they are the highest-value target
# and the intent should be obvious to anyone reading this.
METADATA_HOSTS = {
    "169.254.169.254",      # AWS / Azure / GCP / DigitalOcean
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",      # Alibaba
}


# ------------------------------------------------------------------ SSRF guard


def _is_forbidden_ip(address: str) -> Optional[str]:
    """Why this IP must not be fetched, or ``None`` if it is fine."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None

    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private network address"
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) sidesteps the checks above.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _is_forbidden_ip(str(ip.ipv4_mapped))
    return None


async def _resolve(host: str) -> Tuple[str, ...]:
    """Every address a hostname resolves to.

    All of them are checked, not just the first: a host with one public and
    one private address would otherwise pass validation and then be fetched
    over the private one.
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP),
            timeout=settings.dns_timeout,
        )
    except (asyncio.TimeoutError, socket.gaierror, OSError, UnicodeError):
        return ()
    return tuple({info[4][0] for info in infos})


@dataclass
class UrlVerdict:
    """Whether a URL may be fetched."""

    allowed: bool
    reason: Optional[str] = None
    resolved: Tuple[str, ...] = ()


async def validate_target(url: Optional[str]) -> UrlVerdict:
    """Decide whether the backend may fetch ``url``.

    Permissive about absent URLs — a manual entry by name has none, and that
    is handled elsewhere — and strict about everything it does see.
    """
    if not url:
        return UrlVerdict(True, "no URL supplied")

    try:
        parsed = urlparse(url)
    except Exception:
        return UrlVerdict(False, "URL could not be parsed")

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return UrlVerdict(False, f"scheme {scheme or '(none)'!r} is not allowed")

    host = (parsed.hostname or "").lower()
    if not host:
        return UrlVerdict(False, "URL has no host")

    if settings.allow_private_targets:
        # Development mode: local fixtures live on 127.0.0.1. Never the
        # default, and the log line makes it obvious when it is on.
        return UrlVerdict(True, "private targets permitted by configuration")

    if host in METADATA_HOSTS:
        return UrlVerdict(False, "cloud metadata endpoint")

    literal = _is_forbidden_ip(host)
    if literal:
        return UrlVerdict(False, literal)

    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return UrlVerdict(False, "local hostname")

    if "." not in host:
        # A single-label name only resolves inside a private network.
        return UrlVerdict(False, "non-public hostname")

    addresses = await _resolve(host)
    if not addresses:
        return UrlVerdict(False, f"{host} could not be resolved")

    for address in addresses:
        forbidden = _is_forbidden_ip(address)
        if forbidden:
            return UrlVerdict(
                False,
                f"{host} resolves to a {forbidden} ({address})",
                resolved=addresses,
            )

    return UrlVerdict(True, None, addresses)


# ----------------------------------------------------------------- rate limits


@dataclass
class LimitVerdict:
    """Outcome of a rate-limit check."""

    allowed: bool
    remaining: int = 0
    retry_after: int = 0
    limit: int = 0
    scope: str = "ip"


class RateLimiter:
    """Fixed-window rate limiter, Redis-backed where available.

    Redis is preferred so several API processes share one budget; without it
    the limiter falls back to per-process counters, which is weaker but far
    better than nothing. A fixed window is used rather than a token bucket
    because it needs one counter and one expiry — the cost of the check should
    be negligible next to the work it protects.
    """

    def __init__(self) -> None:
        self._local: Dict[str, Tuple[int, float]] = {}

    async def check(self, identity: str, *, limit: int, window: int) -> LimitVerdict:
        if limit <= 0:
            return LimitVerdict(True, limit=limit)

        window_start = int(time.time() // window)
        key = f"trustlens:rl:{identity}:{window_start}"

        client = await broker.client()
        if client is not None:
            try:
                count = await client.incr(key)
                if count == 1:
                    await client.expire(key, window + 1)
                remaining = max(0, limit - count)
                if count > limit:
                    return LimitVerdict(
                        False,
                        remaining=0,
                        retry_after=window - int(time.time() % window),
                        limit=limit,
                    )
                return LimitVerdict(True, remaining=remaining, limit=limit)
            except Exception as error:
                # A broken limiter must not lock everyone out; fall through to
                # the local counter.
                logger.debug("redis rate limit failed (%s); using local counter", type(error).__name__)

        self._prune()
        count, _expiry = self._local.get(key, (0, time.time() + window))
        count += 1
        self._local[key] = (count, time.time() + window)

        if count > limit:
            return LimitVerdict(
                False,
                remaining=0,
                retry_after=window - int(time.time() % window),
                limit=limit,
            )
        return LimitVerdict(True, remaining=max(0, limit - count), limit=limit)

    def _prune(self) -> None:
        """Drop expired local windows so the dict cannot grow forever."""
        if len(self._local) < 2048:
            return
        now = time.time()
        for key in [k for k, (_c, expiry) in self._local.items() if expiry < now]:
            self._local.pop(key, None)


limiter = RateLimiter()


def client_identity(request) -> str:
    """Who to rate-limit.

    ``X-Forwarded-For`` is honoured only when the service is explicitly
    configured to sit behind a proxy: trusting it unconditionally lets any
    caller forge an identity and bypass the limit entirely.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown").strip()

"""Currency conversion, for showing a scraped price in USD.

Deliberately minimal: one function, one free/keyless public API, one
in-process cache. Follows the same contract as every other optional
enrichment in this codebase (see ``app.services.videos.youtube``) — never
raises, and any failure (network, unknown currency, malformed response)
degrades to "not available" rather than guessing a rate.
"""

import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

RATES_ENDPOINT = "https://open.er-api.com/v6/latest/USD"

# Exchange rates drift slowly; refetching once per process every few hours
# is more than enough freshness for a "roughly what this costs in USD" figure,
# and avoids hitting the free API on every single analysis.
_CACHE_TTL_SECONDS = 12 * 60 * 60
_cache: dict = {"rates": None, "fetched_at": 0.0}


async def _rates(client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """USD-to-everything exchange rates, refreshed at most once per TTL.

    Returns ``None`` on any failure — never a stale-forever or fabricated
    value, but a temporary outage does not repeatedly retry within the TTL
    window either.
    """
    now = time.monotonic()
    if _cache["rates"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["rates"]

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=settings.currency_api_timeout)

    try:
        response = await client.get(RATES_ENDPOINT)
        if response.status_code != 200:
            logger.info("exchange rate lookup failed: HTTP %s", response.status_code)
            return _cache["rates"]  # stale cache beats nothing, if we have one

        payload = response.json()
        rates = payload.get("rates")
        if not isinstance(rates, dict) or not rates:
            return _cache["rates"]

        _cache["rates"] = rates
        _cache["fetched_at"] = now
        return rates
    except (httpx.HTTPError, ValueError) as error:
        logger.info("exchange rate lookup failed: %s", error)
        return _cache["rates"]
    except Exception:  # pragma: no cover - never let this break the pipeline
        logger.exception("exchange rate lookup failed unexpectedly")
        return _cache["rates"]
    finally:
        if owns_client:
            await client.aclose()


async def usd_amount(
    price: float, currency: str, *, client: Optional[httpx.AsyncClient] = None
) -> Optional[float]:
    """``price`` in ``currency``, converted to USD — or ``None`` if it can't be.

    A currency this API doesn't recognize, or an outage, both return
    ``None``: the caller shows the original price/currency without a USD
    figure rather than fabricating a rate.
    """
    code = (currency or "").strip().upper()
    if not code:
        return None
    if code == "USD":
        return round(price, 2)

    rates = await _rates(client)
    if not rates or code not in rates:
        return None

    rate = rates[code]
    if not isinstance(rate, (int, float)) or rate <= 0:
        return None

    return round(price / rate, 2)

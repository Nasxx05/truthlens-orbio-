"""Per-site review adapters.

Each adapter knows only how one retailer exposes its reviews: where the review
pages live and how they paginate. Extraction itself is shared. Adding a
retailer means adding a module here and registering it, never editing another
site's adapter — one site changing its markup cannot break the rest.
"""

from typing import List, Optional

from app.services.scrapers.sites.amazon import AmazonAdapter
from app.services.scrapers.sites.bestbuy import BestBuyAdapter
from app.services.scrapers.sites.ebay import EbayAdapter
from app.services.scrapers.sites.generic import GenericAdapter
from app.services.scrapers.sites.newegg import NeweggAdapter

# Order matters: the first adapter whose `handles` returns True wins, so the
# generic fallback must come last.
ADAPTERS = [
    AmazonAdapter(),
    NeweggAdapter(),
    BestBuyAdapter(),
    EbayAdapter(),
    GenericAdapter(),
]

# Adapters that can search their own site for an equivalent product. Keyed by
# name for configuration.
SEARCH_ADAPTERS = {
    adapter.name: adapter
    for adapter in ADAPTERS
    if getattr(adapter, "supports_search", False)
}


def adapter_for(url: str, site_hint: Optional[str] = None):
    """The adapter responsible for this URL."""
    for adapter in ADAPTERS:
        try:
            if adapter.handles(url, site_hint):
                return adapter
        except Exception:
            continue
    return ADAPTERS[-1]


__all__: List[str] = [
    "ADAPTERS",
    "SEARCH_ADAPTERS",
    "adapter_for",
    "AmazonAdapter",
    "BestBuyAdapter",
    "EbayAdapter",
    "GenericAdapter",
    "NeweggAdapter",
]

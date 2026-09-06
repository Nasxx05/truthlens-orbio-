"""Review scraping.

Structure mirrors the architecture rule that each data source is independent:

    base.py      normalized Review / ScrapeResult types
    fetch.py     HTTP, retries, robots.txt, bot-block detection
    extract.py   HTML -> reviews (structured data, then DOM heuristics)
    browser.py   optional Playwright rendering for client-rendered pages
    host.py      orchestration for the host site
    sites/       per-retailer adapters (where reviews live, how they paginate)

The competitor site arrives in a later phase and will reuse everything except
``host.py``.
"""

from app.services.scrapers.base import Review, ScrapeResult
from app.services.scrapers.host import scrape_host_reviews

__all__ = ["Review", "ScrapeResult", "scrape_host_reviews"]

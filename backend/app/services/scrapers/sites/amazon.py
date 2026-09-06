"""Amazon adapter.

Amazon keeps a handful of reviews on the product page and the rest behind
``/product-reviews/<ASIN>``, paginated by ``pageNumber``.

Two things to know about scraping it:

  * ``/product-reviews/`` is disallowed by Amazon's robots.txt. With
    RESPECT_ROBOTS on (the default) those pages are skipped and only the
    reviews present on the product page are collected.
  * Amazon serves a CAPTCHA interstitial to server-side traffic frequently.
    The fetch layer recognizes it, so a block is reported as a block rather
    than as a product with no reviews.

Both are surfaced in the scrape result rather than hidden, so a thin result has
a visible cause.
"""

import re
from typing import List, Optional
from urllib.parse import urlparse

_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|product-reviews)/([A-Z0-9]{10})", re.I)


class AmazonAdapter:
    name = "amazon"

    def handles(self, url: str, site_hint: Optional[str] = None) -> bool:
        if site_hint and site_hint.lower() == "amazon":
            return True
        try:
            return bool(re.search(r"(^|\.)amazon\.", urlparse(url).netloc, re.I))
        except Exception:
            return False

    def asin(self, url: str, product_id: Optional[str] = None) -> Optional[str]:
        """The ASIN, from the detected id or the URL."""
        if product_id and re.fullmatch(r"[A-Z0-9]{10}", product_id.strip(), re.I):
            return product_id.strip().upper()
        match = _ASIN.search(url or "")
        return match.group(1).upper() if match else None

    def _domain(self, url: str) -> str:
        """Keep the regional domain — reviews differ between amazon.com and .co.uk."""
        try:
            netloc = urlparse(url).netloc
            return netloc or "www.amazon.com"
        except Exception:
            return "www.amazon.com"

    def review_urls(self, product_url: str, product_id: Optional[str] = None) -> List[str]:
        candidates = [product_url]
        asin = self.asin(product_url, product_id)
        if asin:
            domain = self._domain(product_url)
            # Most recent first: fresher reviews are more useful for a verdict
            # than the "top" reviews Amazon promotes by default.
            candidates.append(
                f"https://{domain}/product-reviews/{asin}"
                "?reviewerType=all_reviews&sortBy=recent&pageNumber=1"
            )
            candidates.append(f"https://{domain}/product-reviews/{asin}?pageNumber=1")
        return candidates

    def next_page(self, html: str, page_url: str, page_number: int) -> Optional[str]:
        if "/product-reviews/" not in page_url:
            return None
        if "pageNumber=" in page_url:
            return re.sub(r"pageNumber=\d+", f"pageNumber={page_number + 1}", page_url)
        joiner = "&" if "?" in page_url else "?"
        return f"{page_url}{joiner}pageNumber={page_number + 1}"

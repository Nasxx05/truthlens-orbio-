"""Fallback adapter for any retailer without a dedicated module.

Most e-commerce platforms (Shopify, WooCommerce, Magento, BigCommerce and the
review widgets bolted onto them) publish reviews either as schema.org data or
in conventionally named markup. This adapter starts at the product page and
follows whatever "next page" link the site advertises.
"""

from typing import List, Optional


class GenericAdapter:
    name = "generic"

    def handles(self, url: str, site_hint: Optional[str] = None) -> bool:
        return True  # last resort

    def review_urls(self, product_url: str, product_id: Optional[str] = None) -> List[str]:
        """Where reviews might live, best guess first.

        The product page itself is the primary target; the common
        ``/reviews`` sub-path is worth one try for sites that split them out.
        """
        candidates = [product_url]

        base = product_url.split("?")[0].split("#")[0].rstrip("/")
        for suffix in ("/reviews", "/review"):
            candidate = base + suffix
            if candidate not in candidates:
                candidates.append(candidate)

        return candidates

    def next_page(self, html: str, page_url: str, page_number: int) -> Optional[str]:
        """Defer to the link the page itself advertises."""
        from app.services.scrapers.extract import find_next_page

        return find_next_page(html, page_url)

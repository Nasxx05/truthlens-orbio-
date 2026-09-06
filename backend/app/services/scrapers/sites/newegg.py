"""Newegg adapter — search and reviews.

Newegg is the default competitor for electronics: it is one of the few large
retailers whose robots.txt permits both search and product paths. It does serve
a CAPTCHA to server-side traffic, which the fetch layer reports as a block.
"""

import re
from typing import List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from app.services.scrapers.extract import soup_of


class NeweggAdapter:
    name = "newegg"
    domain = "newegg.com"
    supports_search = True

    def handles(self, url: str, site_hint: Optional[str] = None) -> bool:
        if site_hint and site_hint.lower() == self.name:
            return True
        try:
            return bool(re.search(r"(^|\.)newegg\.", urlparse(url).netloc, re.I))
        except Exception:
            return False

    def search_url(self, query: str) -> str:
        return f"https://www.newegg.com/p/pl?d={quote_plus(query)}"

    def parse_search(self, html: str, page_url: str) -> List[dict]:
        """Candidate products from a search results page."""
        soup = soup_of(html)
        candidates: List[dict] = []

        for cell in soup.select(".item-cell, .item-container")[:20]:
            link = cell.select_one("a.item-title")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            href = link.get("href")
            if not title or not href:
                continue

            brand_node = cell.select_one(".item-brand img")
            candidates.append(
                {
                    "title": title,
                    "url": urljoin(page_url, href),
                    "brand": (brand_node.get("title") or brand_node.get("alt")) if brand_node else None,
                }
            )

        return candidates

    def review_urls(self, product_url: str, product_id: Optional[str] = None) -> List[str]:
        candidates = [product_url]
        # Newegg splits reviews onto their own paginated path.
        match = re.search(r"/p/([A-Za-z0-9]+)", product_url or "")
        if match:
            candidates.append(f"https://www.newegg.com/product-reviews/{match.group(1)}")
        return candidates

    def next_page(self, html: str, page_url: str, page_number: int) -> Optional[str]:
        if "/product-reviews/" in page_url:
            if "page=" in page_url:
                return re.sub(r"page=\d+", f"page={page_number + 1}", page_url)
            joiner = "&" if "?" in page_url else "?"
            return f"{page_url}{joiner}page={page_number + 1}"
        from app.services.scrapers.extract import find_next_page

        return find_next_page(html, page_url)

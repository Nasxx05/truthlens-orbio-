"""Best Buy adapter — search and reviews.

Reviews are served by a client-side widget, so the browser-rendering path is
usually required. Best Buy also geo-gates by IP: requests from outside its
serving region get a country-selection interstitial rather than results.
"""

import re
from typing import List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from app.services.scrapers.extract import soup_of


class BestBuyAdapter:
    name = "bestbuy"
    domain = "bestbuy.com"
    supports_search = True

    def handles(self, url: str, site_hint: Optional[str] = None) -> bool:
        if site_hint and site_hint.lower() == self.name:
            return True
        try:
            return bool(re.search(r"(^|\.)bestbuy\.", urlparse(url).netloc, re.I))
        except Exception:
            return False

    def search_url(self, query: str) -> str:
        return f"https://www.bestbuy.com/site/searchpage.jsp?st={quote_plus(query)}"

    def parse_search(self, html: str, page_url: str) -> List[dict]:
        soup = soup_of(html)
        candidates: List[dict] = []

        for item in soup.select("li.sku-item, [data-testid='product-list-item']")[:20]:
            link = item.select_one("h4.sku-title a, .sku-title a, a.sku-title-link")
            if not link:
                continue
            title = " ".join(link.get_text(" ", strip=True).split())
            href = link.get("href")
            if not title or not href:
                continue
            candidates.append(
                {
                    "title": title,
                    "url": urljoin(page_url, href),
                    "sku": item.get("data-sku-id"),
                }
            )

        return candidates

    def review_urls(self, product_url: str, product_id: Optional[str] = None) -> List[str]:
        candidates = [product_url]
        match = re.search(r"/(\d{6,})\.p", product_url or "")
        sku = product_id if (product_id or "").isdigit() else (match.group(1) if match else None)
        if sku:
            candidates.append(f"https://www.bestbuy.com/site/reviews/product/{sku}")
        return candidates

    def next_page(self, html: str, page_url: str, page_number: int) -> Optional[str]:
        if "/site/reviews/" in page_url:
            joiner = "&" if "?" in page_url else "?"
            return f"{page_url.split('&page=')[0]}{joiner}page={page_number + 1}"
        from app.services.scrapers.extract import find_next_page

        return find_next_page(html, page_url)

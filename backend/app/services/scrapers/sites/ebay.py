"""eBay adapter — search and reviews.

eBay's product reviews live on catalogue pages rather than individual listings,
so review yield is variable. Kept as a competitor option because its search
markup is stable and widely mirrored.
"""

import re
from typing import List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from app.services.scrapers.extract import soup_of


class EbayAdapter:
    name = "ebay"
    domain = "ebay.com"
    supports_search = True

    def handles(self, url: str, site_hint: Optional[str] = None) -> bool:
        if site_hint and site_hint.lower() == self.name:
            return True
        try:
            return bool(re.search(r"(^|\.)ebay\.", urlparse(url).netloc, re.I))
        except Exception:
            return False

    def search_url(self, query: str) -> str:
        # LH_ItemCondition=1000 restricts to new items, so reviews describe the
        # product rather than the condition of one seller's used unit.
        return f"https://www.ebay.com/sch/i.html?_nkw={quote_plus(query)}&LH_ItemCondition=1000"

    def parse_search(self, html: str, page_url: str) -> List[dict]:
        soup = soup_of(html)
        candidates: List[dict] = []

        for item in soup.select("li.s-item, li.s-card")[:20]:
            link = item.select_one("a.s-item__link, a.su-link")
            title_node = item.select_one(".s-item__title, .su-styled-text")
            if not link or not title_node:
                continue
            title = " ".join(title_node.get_text(" ", strip=True).split())
            title = re.sub(r"^(new listing|sponsored)\s*", "", title, flags=re.I)
            href = link.get("href")
            if not title or not href or "Shop on eBay" in title:
                continue
            candidates.append({"title": title, "url": urljoin(page_url, href.split("?")[0])})

        return candidates

    def review_urls(self, product_url: str, product_id: Optional[str] = None) -> List[str]:
        return [product_url]

    def next_page(self, html: str, page_url: str, page_number: int) -> Optional[str]:
        from app.services.scrapers.extract import find_next_page

        return find_next_page(html, page_url)

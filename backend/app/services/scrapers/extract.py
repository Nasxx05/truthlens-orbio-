"""Review extraction from HTML.

Two strategies, tried in order of trustworthiness:

  1. ``from_structured_data`` — schema.org Review in JSON-LD or microdata. The
     site itself says these are reviews, so field mapping is unambiguous.
  2. ``from_dom`` — heuristic container matching. Needed because most retailers
     publish only an aggregate rating in structured data and keep individual
     reviews in markup.

Both are pure functions of HTML, which keeps them testable against fixtures
without a network.
"""

import copy
import json
import logging
import re
from typing import Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.services.scrapers.base import Review, normalize_rating, parse_date

logger = logging.getLogger(__name__)


def soup_of(html: str) -> BeautifulSoup:
    """Parse HTML, preferring lxml and falling back to the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


# --------------------------------------------------------------- structured data


def _walk(node, depth: int = 0) -> Iterable[dict]:
    """Yield every dict in a nested JSON-LD structure."""
    if depth > 8:
        return
    if isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "review", "reviews", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key], depth + 1)


def _is_review(node: dict) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(isinstance(t, str) and t.lower() in {"review", "userreview", "criticreview"} for t in types)


def _first(value):
    """JSON-LD fields are singular or a list, at the publisher's whim."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _author_name(node) -> Optional[str]:
    author = _first(node.get("author") or node.get("creator"))
    if isinstance(author, dict):
        return author.get("name") or None
    if isinstance(author, str):
        return author or None
    return None


def _rating_of(node) -> tuple:
    rating = _first(node.get("reviewRating") or node.get("rating"))
    if isinstance(rating, dict):
        value = rating.get("ratingValue")
        best = rating.get("bestRating") or 5
        return value, best
    if isinstance(rating, (int, float, str)):
        return rating, 5
    return None, 5


def from_structured_data(html: str, source: str, page_url: Optional[str] = None) -> List[Review]:
    """schema.org Review objects, from JSON-LD and microdata."""
    soup = soup_of(html)
    reviews: List[Review] = []

    # --- JSON-LD ---
    for block in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = block.string or block.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Publishers ship malformed JSON-LD constantly. One bad block must
            # not cost us the others.
            logger.debug("skipping malformed JSON-LD block")
            continue

        for node in _walk(parsed):
            if not _is_review(node):
                continue
            body = node.get("reviewBody") or node.get("description") or node.get("text")
            if not isinstance(body, str) or not body.strip():
                continue
            value, best = _rating_of(node)
            iso, raw_date = parse_date(node.get("datePublished") or node.get("dateCreated"))
            body = _debrand(body)
            if len(body) < 8:
                continue
            reviews.append(
                Review(
                    text=body,
                    source=source,
                    rating=normalize_rating(value, best),
                    date=iso,
                    date_raw=raw_date,
                    author=_author_name(node),
                    title=(node.get("name") if isinstance(node.get("name"), str) else None),
                    url=(node.get("url") if isinstance(node.get("url"), str) else None),
                    extracted_by="json-ld",
                )
            )

    # --- Microdata ---
    for scope in soup.select('[itemtype*="schema.org/Review" i], [itemtype*="schema.org/UserReview" i]'):
        body_node = scope.select_one('[itemprop="reviewBody"], [itemprop="description"]')
        body = _text(body_node) or ""
        if not body:
            continue

        rating_node = scope.select_one('[itemprop="ratingValue"]')
        rating_raw = None
        if rating_node:
            rating_raw = rating_node.get("content") or _text(rating_node)

        date_node = scope.select_one('[itemprop="datePublished"]')
        date_raw = (date_node.get("datetime") or date_node.get("content") or _text(date_node)) if date_node else None
        iso, printed = parse_date(date_raw)

        author_node = scope.select_one('[itemprop="author"]')
        title_node = scope.select_one('[itemprop="name"]')

        reviews.append(
            Review(
                text=body,
                source=source,
                rating=normalize_rating(rating_raw),
                date=iso,
                date_raw=printed,
                author=_text(author_node) or None,
                title=_text(title_node) or None,
                extracted_by="microdata",
            )
        )

    if page_url:
        for review in reviews:
            if review.url:
                review.url = urljoin(page_url, review.url)

    return reviews


# ------------------------------------------------------------------------- DOM

# Container patterns, broad on purpose: these are matched against class, id, and
# data-* attributes across many retailers.
_CONTAINER_SELECTORS = (
    '[data-hook="review"]',                      # Amazon
    "[data-review-id]",
    "[data-reviewid]",
    'li[class*="review-item" i]',
    'div[class*="review-item" i]',
    'div[class*="review-card" i]',
    'article[class*="review" i]',
    'li[class*="reviews__item" i]',
    'div[class*="ReviewCard" i]',
    'div[class*="review-entry" i]',
    'div[itemprop="review"]',
    'div[class^="review" i]',
    'li[class^="review" i]',
)

_BODY_SELECTORS = (
    '[data-hook="review-body"] span:not([class])',
    '[data-hook="review-collapsed"]',
    '[data-hook="review-body"]',
    '[class*="review-text" i]',
    '[class*="review-body" i]',
    '[class*="review-content" i]',
    '[class*="reviewText" i]',
    '[class*="review-description" i]',
    '[itemprop="reviewBody"]',
    "p",
)

_RATING_SELECTORS = (
    '[data-hook="review-star-rating"]',
    '[class*="review-rating" i]',
    '[class*="star-rating" i]',
    '[class*="stars" i]',
    '[itemprop="ratingValue"]',
    "[aria-label]",
)

_DATE_SELECTORS = (
    '[data-hook="review-date"]',
    '[class*="review-date" i]',
    '[class*="date" i]',
    "time",
    '[itemprop="datePublished"]',
)

_AUTHOR_SELECTORS = (
    '[class*="profile-name" i]',
    '[class*="review-author" i]',
    '[class*="author" i]',
    '[class*="reviewer" i]',
    '[itemprop="author"]',
)

_TITLE_SELECTORS = (
    '[data-hook="review-title"]',
    '[class*="review-title" i]',
    '[class*="review-heading" i]',
    "h3",
    "h4",
)

# Interface text that sits inside review containers and reads as review body
# text. Amazon's accessibility expander prompts appear on every review, so
# without this the scraper happily returns 14 identical "reviews" of pure UI
# chrome — worse than returning nothing, because it looks like real data.
_BOILERPLATE = re.compile(
    r"(brief content visible,?\s*double tap to read full content"
    r"|full content visible,?\s*double tap to read brief content"
    r"|double tap to read (?:full|brief) content"
    r"|read (?:more|less)\b"
    r"|show (?:more|less)\b"
    r"|see more\b"
    r"|report abuse"
    r"|was this (?:review )?helpful"
    r"|helpful\s*\|?\s*report"
    r"|\d[\d,.]*\s*(?:people|persons?)?\s*found this helpful"
    r"|translate (?:review )?to english"
    r"|originally (?:posted|published) in"
    r"|verified purchase"
    r"|top \d+ reviewer"
    r"|vine customer review of free product)",
    re.I,
)

# Nodes to drop before reading text: they hold votes, buttons, and the
# expander prompts above.
_NOISE_SELECTORS = (
    "script",
    "style",
    "noscript",
    "button",
    ".a-expander-prompt",
    '[data-hook="review-vote"]',
    '[class*="expander-prompt" i]',
    '[class*="helpful" i]',
    '[class*="vote" i]',
    '[class*="report" i]',
    '[class*="translate" i]',
)


def _strip_noise(container) -> None:
    """Remove interface nodes from a review container, in place."""
    for selector in _NOISE_SELECTORS:
        try:
            for node in container.select(selector):
                node.decompose()
        except Exception:
            continue


def _debrand(text: str) -> str:
    """Strip boilerplate phrases from extracted body text.

    Removing nodes and phrases leaves orphaned punctuation behind — Amazon
    bodies come out as ". . I bought these in January" — so tidy the edges too.
    """
    cleaned = _BOILERPLATE.sub(" ", text or "")
    cleaned = " ".join(cleaned.split())
    # Collapse runs of stranded punctuation, then trim the leading ones.
    cleaned = re.sub(r"(?:\s*[.\u00b7|,;:]\s*){2,}", ". ", cleaned)
    cleaned = re.sub(r"^[\s.\u00b7|,;:\-\u2013\u2014]+", "", cleaned)
    return cleaned.strip()


_VERIFIED_PATTERN = re.compile(
    r"verified\s+(purchase|buyer|owner|customer)|confirmed\s+purchase|"
    r"achat\s+v[eé]rifi[eé]|verifizierter\s+kauf",
    re.I,
)

_HELPFUL_PATTERN = re.compile(r"(\d[\d,.]*)\s*(?:people|persons?|users?)?\s*(?:found|voted|thought)", re.I)


def _pick(scope, selectors, exclude=None) -> Optional[str]:
    """First non-empty text among ``selectors``, skipping ``exclude``d nodes."""
    for selector in selectors:
        try:
            for node in scope.select(selector):
                if exclude is not None and node in exclude:
                    continue
                value = _text(node)
                if value:
                    return value
        except Exception:
            continue
    return None


def _rating_from(scope) -> Optional[float]:
    """Ratings hide in aria-labels, alt text, class names, and visible text."""
    for selector in _RATING_SELECTORS:
        try:
            nodes = scope.select(selector)
        except Exception:
            continue
        for node in nodes:
            for candidate in (
                node.get("aria-label"),
                node.get("title"),
                node.get("content"),
                node.get("alt"),
                _text(node),
            ):
                if not candidate:
                    continue
                if not re.search(r"\d", str(candidate)):
                    continue
                # Only trust a bare number when the context says "star" or "rating".
                if not re.search(r"star|rating|out of|/\s*5|von 5", str(candidate), re.I):
                    continue
                rating = normalize_rating(candidate)
                if rating is not None:
                    return rating

    # Some sites encode it only in a class, e.g. class="stars star-4".
    for node in scope.select('[class*="star" i]'):
        classes = " ".join(node.get("class") or [])
        match = re.search(r"(?:star|rating)[-_]?(\d(?:[.,]\d)?)", classes, re.I)
        if match:
            rating = normalize_rating(match.group(1).replace(",", "."))
            if rating is not None:
                return rating
    return None


def from_dom(html: str, source: str, page_url: Optional[str] = None) -> List[Review]:
    """Heuristic extraction from review containers in the markup.

    Tries each container pattern and keeps the one that yields the most
    plausible reviews, rather than merging them — different patterns often match
    the same nodes at different nesting depths, and merging would duplicate.
    """
    soup = soup_of(html)
    best: List[Review] = []

    for selector in _CONTAINER_SELECTORS:
        try:
            containers = soup.select(selector)
        except Exception:
            continue
        if not containers:
            continue

        found: List[Review] = []
        for container in containers[:400]:
            # Skip containers that merely wrap other review containers.
            if len(container.select('[data-hook="review"], [data-review-id]')) > 1:
                continue

            # Read verification and vote text before stripping the nodes that
            # carry them.
            blob = _text(container)

            container = copy.copy(container)
            _strip_noise(container)

            title = _pick(container, _TITLE_SELECTORS)
            title_nodes = set()
            for sel in _TITLE_SELECTORS:
                try:
                    title_nodes.update(container.select(sel))
                except Exception:
                    pass

            # Exclude the title node when reading the body, or short reviews
            # come back as the title repeated twice.
            body = _pick(container, _BODY_SELECTORS, exclude=title_nodes)
            if not body:
                body = _text(container)

            body = _debrand(body)

            # What survives must be actual prose. A container whose text was
            # entirely interface chrome is not a review, and emitting it would
            # pass noise off as data.
            if not body or len(body) < 20:
                continue

            date_raw = _pick(container, _DATE_SELECTORS)
            iso, printed = parse_date(date_raw)

            helpful = None
            helpful_match = _HELPFUL_PATTERN.search(blob)
            if helpful_match:
                try:
                    helpful = int(helpful_match.group(1).replace(",", "").replace(".", ""))
                except ValueError:
                    helpful = None

            found.append(
                Review(
                    text=body,
                    source=source,
                    rating=_rating_from(container),
                    date=iso,
                    date_raw=printed,
                    # None, not False: absence of the badge is not proof the
                    # purchase was unverified — many sites never show it.
                    verified_purchase=True if _VERIFIED_PATTERN.search(blob) else None,
                    author=_pick(container, _AUTHOR_SELECTORS),
                    title=title,
                    helpful_votes=helpful,
                    extracted_by=f"dom:{selector}",
                )
            )

        if len(found) > len(best):
            best = found

    return best


def extract_product_image(html: str, page_url: str) -> Optional[str]:
    """The product's main image, if the page advertises one.

    Tried in order of trustworthiness: og:image/twitter:image (what the site
    itself says represents the page), then schema.org Product.image from
    JSON-LD. Best-effort — absence is not an error, just nothing to show.
    """
    soup = soup_of(html)

    for selector in (
        'meta[property="og:image"]',
        'meta[property="og:image:url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            return urljoin(page_url, node["content"].strip())

    for block in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = block.string or block.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for node in _walk(parsed):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(isinstance(t, str) and t.lower() == "product" for t in types):
                continue
            image = _first(node.get("image"))
            if isinstance(image, dict):
                image = image.get("url")
            if isinstance(image, str) and image.strip():
                return urljoin(page_url, image.strip())

    return None


def extract_product_description(html: str, page_url: str) -> Optional[str]:
    """The product's own description, if the page advertises one.

    Mirrors :func:`extract_product_image`'s trust ordering: og:description /
    twitter:description first, then schema.org Product.description from
    JSON-LD. Best-effort — used only to give the LLM something to check
    review evidence against (``claim_check``); absence means that field is
    simply omitted, never guessed.
    """
    soup = soup_of(html)

    for selector in (
        'meta[property="og:description"]',
        'meta[name="twitter:description"]',
        'meta[name="description"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            text = " ".join(node["content"].split())
            if text:
                return text[:500]

    for block in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = block.string or block.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for node in _walk(parsed):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(isinstance(t, str) and t.lower() == "product" for t in types):
                continue
            description = node.get("description")
            if isinstance(description, str) and description.strip():
                return " ".join(description.split())[:500]

    return None


def _product_node(soup: BeautifulSoup) -> Optional[dict]:
    """The first JSON-LD node typed Product on the page, if any."""
    for block in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = block.string or block.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for node in _walk(parsed):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and t.lower() == "product" for t in types):
                return node
    return None


def _product_offer(node: dict) -> Optional[dict]:
    """The first offer that actually carries a price, if any."""
    offers = _first(node.get("offers"))
    if isinstance(offers, dict):
        return offers if offers.get("price") is not None else None
    if isinstance(node.get("offers"), list):
        for offer in node["offers"]:
            if isinstance(offer, dict) and offer.get("price") is not None:
                return offer
    return None


def _product_color(node: dict) -> Optional[str]:
    color = node.get("color")
    if isinstance(color, str) and color.strip():
        return color.strip()
    for prop in node.get("additionalProperty") or []:
        if not isinstance(prop, dict):
            continue
        name = str(prop.get("name") or "").strip().lower()
        if name in ("color", "colour"):
            value = prop.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _product_brand(node: dict) -> Optional[str]:
    brand = node.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def extract_product_details(html: str, page_url: str) -> dict:
    """Buyer-facing facts about the product itself: name, price, color,
    brand, SKU — whatever the page actually advertises.

    Mirrors :func:`extract_product_image`'s trust ordering (meta tags, then
    schema.org Product JSON-LD), extended to more fields off the same
    Product node. Best-effort and partial by design: returns only the keys
    actually found, and an empty dict when the page advertises none of
    this — never a guessed or placeholder value.
    """
    soup = soup_of(html)
    details: dict = {}

    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            text = " ".join(node["content"].split())
            if text:
                details["name"] = text
                break

    price_amount = None
    price_currency = None
    for amount_sel, currency_sel in (
        ('meta[property="product:price:amount"]', 'meta[property="product:price:currency"]'),
    ):
        amount_node = soup.select_one(amount_sel)
        currency_node = soup.select_one(currency_sel)
        if amount_node and amount_node.get("content"):
            try:
                price_amount = float(str(amount_node["content"]).strip())
            except (TypeError, ValueError):
                price_amount = None
            if price_amount is not None and currency_node and currency_node.get("content"):
                price_currency = str(currency_node["content"]).strip()

    product = _product_node(soup)
    if product:
        if "name" not in details:
            name = product.get("name")
            if isinstance(name, str) and name.strip():
                details["name"] = " ".join(name.split())

        if price_amount is None:
            offer = _product_offer(product)
            if offer:
                try:
                    price_amount = float(offer["price"])
                except (TypeError, ValueError):
                    price_amount = None
                currency = offer.get("priceCurrency")
                if price_amount is not None and isinstance(currency, str) and currency.strip():
                    price_currency = currency.strip()

        color = _product_color(product)
        if color:
            details["color"] = color

        brand = _product_brand(product)
        if brand:
            details["brand"] = brand

        sku = product.get("sku") or product.get("mpn") or product.get("gtin13") or product.get("gtin")
        if isinstance(sku, str) and sku.strip():
            details["sku"] = sku.strip()

    if price_amount is not None and price_currency:
        details["price"] = price_amount
        details["currency"] = price_currency.upper()

    return details


def find_next_page(html: str, page_url: str) -> Optional[str]:
    """Next page of reviews, if the page advertises one."""
    soup = soup_of(html)

    link = soup.select_one('link[rel="next"], a[rel="next"]')
    if link and link.get("href"):
        return urljoin(page_url, link["href"])

    for selector in (
        'li.a-last a',                                  # Amazon
        '[data-hook="pagination-bar"] a[href]',
        'a[class*="next" i][href]',
        'a[aria-label*="next" i][href]',
        'a[title*="next" i][href]',
    ):
        try:
            node = soup.select_one(selector)
        except Exception:
            continue
        if node and node.get("href"):
            href = node["href"]
            if href.strip().startswith("#") or "javascript:" in href.lower():
                continue
            return urljoin(page_url, href)

    return None

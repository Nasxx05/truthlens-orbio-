"""Shared types for review scraping.

A ``Review`` is the normalized shape every scraper produces, whatever the site
and whatever extraction strategy found it. A ``ScrapeResult`` wraps a batch of
them together with what happened during the scrape — a source that fails must
be able to say so, since the caller returns partial results rather than an
error.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Review:
    """One review, normalized across sites.

    Every field except ``text`` and ``source`` is optional: sites expose wildly
    different subsets, and a review with only body text is still usable input
    for filtering and summarization.
    """

    text: str
    source: str                                  # platform the review came from
    rating: Optional[float] = None               # normalized to a 5-point scale
    rating_scale: float = 5.0
    date: Optional[str] = None                   # ISO-8601 date where parseable
    date_raw: Optional[str] = None               # as printed on the page
    verified_purchase: Optional[bool] = None     # None means the site does not say
    author: Optional[str] = None
    title: Optional[str] = None
    helpful_votes: Optional[int] = None
    url: Optional[str] = None
    extracted_by: Optional[str] = None           # which strategy found it

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "rating": self.rating,
            "date": self.date,
            "date_raw": self.date_raw,
            "verified_purchase": self.verified_purchase,
            "author": self.author,
            "title": self.title,
            "helpful_votes": self.helpful_votes,
            "url": self.url,
            "extracted_by": self.extracted_by,
        }

    @property
    def fingerprint(self) -> str:
        """Identity for deduplication.

        Pagination overlaps and the same review often appears in both structured
        data and the DOM, so dedupe on normalized body text plus author.
        """
        body = " ".join((self.text or "").lower().split())[:300]
        return f"{(self.author or '').strip().lower()}|{body}"


@dataclass
class ScrapeResult:
    """Outcome of scraping one source.

    ``ok`` records whether the scrape ran, not whether it found anything: a
    site that legitimately has no reviews is a successful scrape with a count of
    zero, and is a different situation from being blocked.
    """

    source: str
    reviews: List[Review] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None
    strategies: List[str] = field(default_factory=list)
    pages_fetched: int = 0
    truncated: bool = False        # cap reached, more reviews exist
    blocked: bool = False          # bot protection or robots.txt
    notes: List[str] = field(default_factory=list)
    duration_ms: Optional[int] = None
    image_url: Optional[str] = None  # product image, scraped from the host page

    def add(self, reviews: List[Review], strategy: str, limit: int) -> int:
        """Merge in newly found reviews, deduplicated and capped.

        Returns how many were actually added.
        """
        seen = {r.fingerprint for r in self.reviews}
        added = 0

        for review in reviews:
            if len(self.reviews) >= limit:
                self.truncated = True
                break
            text = " ".join((review.text or "").split())
            # Single-word "reviews" are almost always scrape artifacts —
            # rating labels, button text, template leftovers.
            if len(text) < 8:
                continue
            review.text = text
            if review.fingerprint in seen:
                continue
            seen.add(review.fingerprint)
            self.reviews.append(review)
            added += 1

        if added and strategy not in self.strategies:
            self.strategies.append(strategy)
        return added

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "count": len(self.reviews),
            "error": self.error,
            "strategies": self.strategies,
            "pages_fetched": self.pages_fetched,
            "truncated": self.truncated,
            "blocked": self.blocked,
            "notes": self.notes,
            "duration_ms": self.duration_ms,
            "image_url": self.image_url,
        }


def normalize_rating(value, scale=5.0) -> Optional[float]:
    """Coerce a rating to a 5-point scale.

    Sites publish "4.0", "4 out of 5 stars", "80%", or a 10-point score. A raw
    number is meaningless downstream unless the scale comes with it.
    """
    if value is None:
        return None

    if isinstance(value, str):
        import re

        text = value.strip()
        # "4.5 out of 5", "4,5 / 5"
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:out of|/|von|sur)\s*(\d+(?:[.,]\d+)?)", text, re.I)
        if match:
            try:
                value = float(match.group(1).replace(",", "."))
                scale = float(match.group(2).replace(",", "."))
            except ValueError:
                return None
        else:
            match = re.search(r"(\d+(?:[.,]\d+)?)", text)
            if not match:
                return None
            try:
                value = float(match.group(1).replace(",", "."))
            except ValueError:
                return None
            if "%" in text:
                scale = 100.0

    try:
        rating = float(value)
    except (TypeError, ValueError):
        return None

    try:
        scale = float(scale) or 5.0
    except (TypeError, ValueError):
        scale = 5.0

    if rating <= 0 or scale <= 0:
        return None

    normalized = rating * (5.0 / scale)
    # A "rating" outside the plausible range means the scale was misread;
    # dropping it beats feeding a wrong number downstream.
    if normalized > 5.5:
        return None
    return round(min(normalized, 5.0), 2)


def parse_date(value) -> tuple:
    """Best-effort date parse. Returns ``(iso_or_None, raw_or_None)``.

    Review dates come as "Reviewed in the United States on March 3, 2024",
    "2 weeks ago", ISO strings, and everything between. The raw string is kept
    either way so nothing is lost when parsing fails.
    """
    if value is None:
        return None, None

    raw = str(value).strip()
    if not raw:
        return None, None

    import re

    # Strip common prefixes so the parser sees just the date.
    cleaned = re.sub(
        r"^.*?\bon\s+|^reviewed\s+|^published\s+|^posted\s+|^date[:\s]+",
        "",
        raw,
        flags=re.I,
    ).strip()

    # Relative dates: dateutil would read "2 weeks ago" as today.
    relative = re.match(r"(\d+)\s+(day|week|month|year|hour|minute)s?\s+ago", cleaned, re.I)
    if relative:
        from datetime import timedelta

        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        days = {"hour": 0, "minute": 0, "day": 1, "week": 7, "month": 30, "year": 365}[unit]
        try:
            when = datetime.utcnow() - timedelta(days=amount * days)
            return when.date().isoformat(), raw
        except (OverflowError, ValueError):
            return None, raw

    # Fuzzy parsing is eager: "3 stars" becomes today's date, silently
    # inventing data. Only parse strings that actually look like dates, and
    # never ones that look like ratings.
    if re.search(r"\b(stars?|rating|out of|verified|helpful)\b", cleaned, re.I):
        return None, raw

    looks_like_date = (
        re.search(r"\b(19|20)\d{2}\b", cleaned)                       # a year
        or re.search(
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", cleaned, re.I
        )                                                              # a month name
        or re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", cleaned)      # 12/11/2023
    )
    if not looks_like_date:
        return None, raw

    try:
        from dateutil import parser as date_parser

        parsed = date_parser.parse(cleaned, fuzzy=True)
        if 1995 <= parsed.year <= datetime.utcnow().year + 1:
            return parsed.date().isoformat(), raw
    except Exception:
        pass

    return None, raw

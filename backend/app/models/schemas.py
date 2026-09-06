"""Request and response shapes for the TrustLens API.

These models define the contract between the extension and the backend. The
extension renders whatever JSON comes back, so this shape is the thing both
sides agree on. Later phases fill the empty collections with real data; the
shape itself should stay stable.
"""

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class Detection(BaseModel):
    """How the extension arrived at the product, for diagnostics and logging.

    Carried so the backend can tell a confident structured detection from a
    guess or a hand-typed entry. Later phases will want that distinction when
    deciding whether a scrape target is trustworthy enough to act on.
    """

    source: str = Field(
        "unknown",
        description="How the product was identified: 'auto' (DOM detection) or 'manual' (typed)",
    )
    confidence: str = Field("none", description="high | medium | low | none")
    sources: List[str] = Field(default_factory=list, description="Which strategies supplied which field")
    site: Optional[str] = Field(None, description="Host site the product was detected on")


class CacheInfo(BaseModel):
    """Whether this result came from the cache."""

    hit: bool = False
    age_seconds: Optional[int] = None
    expires_at: Optional[str] = None
    key: Optional[str] = Field(None, description="Cache key, for invalidation")


class AnalyzeRequest(BaseModel):
    """What the extension sends when the shopper clicks the icon.

    Either ``product_url`` or ``product_name`` must be present. Detection can
    fail on an unsupported site, in which case the shopper types a name and
    there is no URL to send.
    """

    product_url: Optional[str] = Field(None, description="URL of the product page the shopper is on")
    product_name: Optional[str] = Field(
        None, description="Product name, if the extension detected one or the user typed it"
    )
    product_id: Optional[str] = Field(
        None, description="Site-specific identifier, e.g. an Amazon ASIN or eBay item id"
    )
    canonical_url: Optional[str] = Field(
        None, description="Canonical product URL, tracking parameters stripped"
    )
    detection: Optional[Detection] = Field(None, description="Provenance of the product info")
    refresh: bool = Field(
        False, description="Bypass the cache and re-analyze from scratch"
    )

    @model_validator(mode="after")
    def require_url_or_name(self) -> "AnalyzeRequest":
        """Reject a request that identifies no product at all.

        Without this, an empty body would sail through and later phases would
        scrape nothing while still reporting success.
        """
        if not (self.product_url or "").strip() and not (self.product_name or "").strip():
            raise ValueError("either product_url or product_name is required")
        return self


class Summary(BaseModel):
    """LLM-generated verdict, grounded in the reviews that passed filtering.

    Empty (`verdict: ""`) when summarization could not run — no provider
    configured, no surviving reviews, or an API failure. `llm.error` says why.
    """

    pros: List[str] = []
    cons: List[str] = []
    verdict: str = ""
    confidence: str = Field(
        "none",
        description=(
            "How much the evidence supports this verdict: high / medium / low / none. "
            "Capped in code against the actual review count and platform count, so it "
            "cannot overstate thin evidence even if the model does."
        ),
    )
    caveats: List[str] = Field(
        default_factory=list,
        description="Limitations of this summary — thin evidence, single platform, platform disagreement",
    )


class SourceReport(BaseModel):
    """What happened when one data source was collected.

    Every source reports independently. A failure here is information, not an
    error: the endpoint returns whatever the other sources produced, and this
    says why a source came back thin or empty.
    """

    source: str = Field(..., description="Platform the reviews came from")
    ok: bool = True
    count: int = 0
    error: Optional[str] = None
    strategies: List[str] = Field(default_factory=list, description="Extraction strategies that worked")
    pages_fetched: int = 0
    truncated: bool = Field(False, description="Review cap reached; more exist on the site")
    blocked: bool = Field(False, description="Bot protection or robots.txt stopped the scrape")
    notes: List[str] = Field(default_factory=list)
    duration_ms: Optional[int] = None


class VideoSourceReport(BaseModel):
    """What happened when one video platform was queried."""

    source: str = Field(..., description="Platform: youtube | tiktok")
    ok: bool = True
    count: int = 0
    error: Optional[str] = None
    blocked: bool = False
    considered: int = Field(0, description="Results seen before filtering")
    filtered_out: int = Field(0, description="Dropped as too short or irrelevant")
    notes: List[str] = Field(default_factory=list)
    duration_ms: Optional[int] = None


class ProductMatch(BaseModel):
    """Whether the competitor listing is the same product, and why.

    Competitor reviews are only included when this clears the configured
    threshold: reviews of a near-miss product read as evidence about this one
    and are worse than no competitor data at all.
    """

    matched: bool = False
    score: float = 0.0
    confidence: str = "none"
    reasons: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    matched_title: Optional[str] = None
    matched_url: Optional[str] = None
    matched_site: Optional[str] = None


class FilterReportModel(BaseModel):
    """Set-level filtering summary.

    Present so a filtering decision can be audited without re-running the
    engine: which rules fired how often, which near-duplicate clusters were
    found, and what the date distribution looked like.
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    threshold: float = 0.0
    rule_counts: dict = Field(default_factory=dict, description="How many reviews each rule flagged")
    duplicate_clusters: List[dict] = Field(
        default_factory=list, description="Near-duplicate groups, largest first"
    )
    pacing: dict = Field(default_factory=dict, description="Submission-date distribution and bursts")
    notes: List[str] = Field(default_factory=list)


class LLMReport(BaseModel):
    """How the summary was produced."""

    provider: Optional[str] = None
    model: Optional[str] = None
    ok: bool = False
    error: Optional[str] = None
    reviews_used: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    notes: List[str] = Field(default_factory=list)
    adjustments: List[str] = Field(
        default_factory=list,
        description="Corrections applied to the model's output, e.g. confidence lowered",
    )


class AnalyzeResponse(BaseModel):
    """The full payload the popup renders.

    ``status`` values:

    * ``ok``              — enough reviews to work with
    * ``not_enough_data`` — fewer than REVIEW_MIN reviews found; ``reviews``
      still carries whatever was scraped, but no verdict should be inferred
      from it
    """

    status: str = "ok"
    summary: Summary = Summary()
    reviews: List[dict] = Field(
        default_factory=list,
        description=(
            "Every scraped review, each tagged with its source platform and carrying a "
            "`filter` object with its pass/fail verdict, suspicion score, and the rules "
            "that fired. Nothing is removed — the caller chooses what to use."
        ),
    )
    videos: List[dict] = Field(
        default_factory=list,
        description="Videos from every platform, each tagged with its own platform in `source`",
    )
    sources: List[SourceReport] = Field(
        default_factory=list, description="Per-review-source outcome, one entry per data source"
    )
    video_sources: List[VideoSourceReport] = Field(
        default_factory=list, description="Per-video-platform outcome"
    )
    product_match: Optional[ProductMatch] = Field(
        None, description="Competitor product match verdict, when a competitor was attempted"
    )
    contributed: List[str] = Field(
        default_factory=list, description="Sources that actually returned data"
    )
    filter_report: Optional[FilterReportModel] = Field(
        None, description="Fake-review filtering summary"
    )
    reviews_passed: int = Field(
        0, description="How many reviews passed filtering; these are what the summary is built from"
    )
    llm: Optional[LLMReport] = Field(None, description="How the summary was produced")
    cached: Optional[CacheInfo] = Field(None, description="Cache provenance of this result")
    message: Optional[str] = Field(
        None, description="Human-readable explanation when status is not ok"
    )

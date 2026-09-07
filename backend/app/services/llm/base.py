"""The LLM provider interface.

Everything about *which* model produces the summary lives behind this
interface, so swapping provider is a configuration change and calling code
never mentions a vendor. The route calls ``summarize()`` and gets a
``SummaryResult``; it has no idea whether that came from Anthropic, OpenAI, or
a stub.

Two design points worth stating:

  * ``SummaryOutput`` is a pydantic model, not a free-text blob. The verdict
    has to be renderable as pros / cons / paragraph in a popup, so the shape is
    part of the contract and is enforced at the API boundary rather than parsed
    out of prose afterwards.
  * A provider never raises. A failed summarization degrades to an empty
    summary with a reason, exactly like a failed scrape, so the endpoint still
    returns the reviews and videos it collected.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import BaseModel, Field


class SummaryOutput(BaseModel):
    """The structured summary an LLM is asked to produce.

    Field descriptions are part of the prompt: providers that support schema
    enforcement pass this straight through, so the wording here is what steers
    the model.
    """

    pros: List[str] = Field(
        default_factory=list,
        description=(
            "Concrete advantages that multiple reviewers actually mention. Each one a "
            "short phrase. Only include a point supported by the supplied reviews."
        ),
    )
    cons: List[str] = Field(
        default_factory=list,
        description=(
            "Concrete drawbacks, faults or complaints that reviewers actually report. "
            "Each one a short phrase. Do not invent balance: if reviewers report few "
            "problems, return few."
        ),
    )
    verdict: str = Field(
        "",
        description=(
            "One short paragraph (2-4 sentences) answering whether this product is worth "
            "buying, and for whom. State the basis and be explicit about uncertainty when "
            "the evidence is thin."
        ),
    )
    confidence: str = Field(
        "low",
        description=(
            "How much the evidence supports this verdict: 'high', 'medium' or 'low'. "
            "Base it on how many reviews there were, how consistent they were, and "
            "whether they came from more than one platform."
        ),
    )
    caveats: List[str] = Field(
        default_factory=list,
        description=(
            "Limitations a shopper should know about this summary itself — thin evidence, "
            "reviews from a single platform only, disagreement between platforms, "
            "reviews concentrated in one time period."
        ),
    )
    trust_score: int = Field(
        50,
        description=(
            "Overall trust score for this product, 0-100. 0 means avoid, 100 means "
            "excellent and trustworthy. Combine review sentiment and consistency, and — "
            "when there are no reviews — video commentary and general product/brand "
            "reputation. Always provide a number; when evidence is thin or absent, keep it "
            "near the middle (40-60) to reflect genuine uncertainty rather than guessing an "
            "extreme."
        ),
    )
    star_rating: float = Field(
        2.5,
        description=(
            "A shopper-facing star rating from 0 to 5, in increments of 0.5, consistent "
            "with trust_score and the verdict. Always provide one, even with thin evidence."
        ),
    )
    themes: List["ThemeOutput"] = Field(
        default_factory=list,
        description=(
            "Recurring topics reviewers actually raise, e.g. 'battery life', 'customer "
            "support'. Only include a topic that comes up across multiple reviews — this "
            "is a pattern summary, not a single reviewer's opinion restated."
        ),
    )
    reasons_to_buy: List[str] = Field(
        default_factory=list,
        description=(
            "Short, grounded reasons a shopper might buy this, drawn from the same "
            "evidence as pros — not marketing language."
        ),
    )
    reasons_to_think_twice: List[str] = Field(
        default_factory=list,
        description=(
            "Short, grounded reasons a shopper might hesitate, drawn from the same "
            "evidence as cons."
        ),
    )
    claim_check: Optional[str] = Field(
        None,
        description=(
            "Only fill this in when a product description was supplied. One cautious "
            "sentence on whether review evidence generally supports, contradicts, or "
            "doesn't address that description. Never claim certainty; leave this null "
            "when no description was given rather than guessing."
        ),
    )


class ThemeOutput(BaseModel):
    """One recurring topic across the reviews."""

    label: str = Field(..., description="Short topic name, e.g. 'battery life'")
    sentiment: str = Field(
        "mixed", description="How reviewers feel about this topic: positive | negative | mixed"
    )
    mention_count: int = Field(
        0, description="Roughly how many of the supplied reviews raised this topic"
    )


SummaryOutput.model_rebuild()


@dataclass
class SummaryRequest:
    """Everything a provider needs to write a grounded summary.

    Reviews arrive already filtered; the counts describe what was collected so
    the prompt can calibrate its own confidence rather than guessing.
    """

    product_name: str
    reviews: List[dict] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    total_scraped: int = 0
    filtered_out: int = 0
    # Average rating per platform, used to tell the model when platforms
    # disagree — the cross-platform signal the project exists to surface.
    ratings_by_source: dict = field(default_factory=dict)
    # Only populated when there are no reviews at all — the fallback evidence
    # for a verdict based on video commentary instead. Each entry carries
    # title/channel/views/published/description, no transcript.
    video_evidence: List[dict] = field(default_factory=list)
    # Best-effort meta description scraped from the product page, if any. Used
    # only for the optional claim_check field — never fabricated when absent.
    product_description: Optional[str] = None


@dataclass
class SummaryResult:
    """Outcome of one summarization attempt."""

    summary: Optional[SummaryOutput] = None
    provider: str = "none"
    model: Optional[str] = None
    ok: bool = False
    error: Optional[str] = None
    reviews_used: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "error": self.error,
            "reviews_used": self.reviews_used,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "duration_ms": self.duration_ms,
            "notes": self.notes,
        }


class LLMProvider(ABC):
    """A summarization backend.

    Implementations are responsible for their own credentials, request shape
    and error handling. They must not raise: a failure is reported through
    ``SummaryResult``.
    """

    name: str = "abstract"

    @abstractmethod
    def available(self) -> bool:
        """Is this provider configured and usable right now?"""

    @abstractmethod
    async def summarize(self, request: SummaryRequest) -> SummaryResult:
        """Produce a structured summary of the supplied reviews."""

    @property
    def model(self) -> Optional[str]:
        """The model this provider will use, for reporting."""
        return None

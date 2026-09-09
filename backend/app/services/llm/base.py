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
    evidence: List["EvidenceOutput"] = Field(
        default_factory=list,
        description=(
            "3-8 structured, traceable findings — the evidence layer behind pros/cons. "
            "Each one grounded in the supplied reviews (and, for claim_conflict, the "
            "supplied product description). Do not restate every pro/con as an "
            "evidence item; pick the notable, specific findings a shopper would want "
            "traced back to real counts. When product evidence (reviews) is thin, this "
            "list may also include 'Website/company trust' items — see rule 8d."
        ),
    )
    who_should_buy: List[str] = Field(
        default_factory=list,
        description=(
            "0-3 short statements of who this product actually fits, grounded strictly "
            "in what reviewers say about their own use case (e.g. 'Good fit for casual "
            "users who mainly care about battery life'). Empty list if reviews don't "
            "give enough to say — never invent a demographic."
        ),
    )
    who_should_avoid: List[str] = Field(
        default_factory=list,
        description=(
            "0-2 short statements of who should probably skip this, grounded the same "
            "way as who_should_buy — a real mismatch reviewers point to, not a generic "
            "caveat. Empty list if there's nothing grounded to say."
        ),
    )
    alternatives: List["AlternativeOutput"] = Field(
        default_factory=list,
        description=(
            "0-3 comparable products a shopper might consider instead. Only populate "
            "this from your own general knowledge when you recognize the specific "
            "product, its brand, or its category — never fabricate a plausible-"
            "sounding name. Leave empty when you don't recognize it. See rule 8f."
        ),
    )
    alternatives_basis: Optional[str] = Field(
        None,
        description=(
            "Set to exactly 'general_knowledge' whenever `alternatives` is non-empty "
            "(it is always general knowledge, never verified against this app's own "
            "data). Leave null when `alternatives` is empty."
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


class EvidenceOutput(BaseModel):
    """One structured, traceable finding — the evidence layer behind the score.

    This replaces free-text pros/cons as the primary "why" behind the trust
    score: each item is countable and typed, so a shopper (and the scoring
    code) can see exactly what it rests on. ``severity`` and the mention
    counts are inputs to a deterministic point calculation done in
    ``app.services.scoring`` — this model never states the score impact
    itself, since a model's own point value would be exactly the kind of
    unaudited number this feature exists to replace.
    """

    category: str = Field(
        ..., description="Short topic name, e.g. 'Battery performance', 'Sound quality'"
    )
    type: str = Field(
        ...,
        description=(
            "'evidence' (supports trust — a concrete strength reviewers confirm), "
            "'concern' (undermines trust — a concrete, recurring problem), or "
            "'claim_conflict' (the product description/manufacturer claim and what "
            "reviewers actually report disagree). Use claim_conflict only when a "
            "product description was supplied."
        ),
    )
    explanation: str = Field(
        ..., description="One grounded sentence: what was found and why it matters."
    )
    review_mentions: int = Field(
        0, description="How many of the supplied reviews raise this point. Count honestly; do not round up."
    )
    external_mentions: int = Field(
        0,
        description=(
            "How many independent, non-review sources (competitor listing, video "
            "commentary) corroborate this point. 0 if none do — do not guess."
        ),
    )
    claim_text: Optional[str] = Field(
        None, description="Only for claim_conflict: the manufacturer/listing claim, quoted or closely paraphrased."
    )
    observed_reality: Optional[str] = Field(
        None, description="Only for claim_conflict: what reviewers actually report, in contrast to claim_text."
    )
    severity: float = Field(
        0.5,
        description=(
            "How much this single finding should matter, 0.0-1.0, independent of "
            "sign — 1.0 is a major, consistently-reported point; 0.2 is a minor or "
            "isolated one. This is a relative weight, not a points value."
        ),
    )
    impact_points: Optional[int] = Field(
        None,
        description=(
            "Signed trust-score impact, e.g. -7. Never set by the model — always "
            "None here; app.services.scoring fills this in deterministically from "
            "severity and the mention counts, after the fact."
        ),
    )


class AlternativeOutput(BaseModel):
    """One comparable product, offered as general knowledge — not verified data."""

    name: str = Field(..., description="Product/brand name of the alternative")
    reason: str = Field(
        ..., description="One short, comparative sentence: why a shopper might consider this instead"
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
    # The literal listing URL, if any. Used only to state literal, verifiable
    # facts about it (scheme, domain) — never as license to comment on a
    # company's reputation from general knowledge.
    product_url: Optional[str] = None


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

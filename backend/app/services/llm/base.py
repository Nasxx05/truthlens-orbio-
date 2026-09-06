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

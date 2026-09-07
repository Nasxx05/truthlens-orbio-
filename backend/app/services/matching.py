"""Product match confidence.

The competitor site is only useful if it is showing the *same* product. A
mismatched listing produces a confident, wrong comparison — reviews of a
different item read as evidence about this one — which is worse than having no
competitor data at all. So this module scores a candidate match and the caller
omits the source rather than guessing.

Scoring combines four signals:

  * **Identifier equality** — a shared UPC/EAN/MPN/model number is decisive
  * **Brand agreement** — a different brand is a hard rejection
  * **Model token agreement** — alphanumeric model tokens (WH-1000XM5, A2338)
  * **Descriptive overlap** — remaining words, weighted by rarity

Conflicting numeric specifications (128GB vs 256GB, 45mm vs 41mm) veto a match
outright regardless of score: those are different products with near-identical
titles, and they are exactly the case a token-overlap score gets wrong.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# Words that appear in retail titles without identifying the product.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "with", "for", "in", "on", "of", "to", "by",
    "new", "genuine", "official", "original", "authentic", "brand",
    "free", "shipping", "sale", "deal", "best", "top", "premium", "pro",
    "buy", "shop", "online", "store", "item", "product", "model",
    "edition", "version", "series", "generation", "gen",
    "color", "colour", "size", "pack", "count", "piece", "pieces", "set",
    "includes", "included", "bundle", "kit",
    "wireless", "bluetooth",  # near-universal in electronics titles
    "amazon", "walmart", "bestbuy", "ebay", "target",
    "renewed", "refurbished", "used", "open", "box",
}

# Units whose values must agree when both titles state them.
_SPEC_UNITS = {
    "gb": "capacity", "tb": "capacity", "mb": "capacity",
    "mm": "size", "cm": "size", "inch": "size", "in": "size", '"': "size",
    "oz": "volume", "ml": "volume", "l": "volume", "litre": "volume", "liter": "volume",
    "quart": "volume", "qt": "volume", "gallon": "volume", "gal": "volume",
    "pint": "volume", "pt": "volume",
    "w": "power", "kw": "power", "wh": "power", "mah": "power", "v": "power",
    "hz": "frequency", "khz": "frequency", "ghz": "frequency",
    "mp": "resolution", "k": "resolution", "p": "resolution",
    "kg": "weight", "g": "weight", "lb": "weight", "lbs": "weight",
    "ct": "count", "pc": "count", "pcs": "count",
}

# A token that looks like a manufacturer model number: letters and digits mixed.
_MODEL_TOKEN = re.compile(r"^(?=.*\d)(?=.*[a-z])[a-z0-9][a-z0-9\-/]{2,}$", re.I)

# Things sold *for* a product. Their titles contain the product's full name and
# model number, so they score extremely well on every similarity measure — the
# single most likely way to attach reviews of a phone case to a phone.
_ACCESSORY_WORDS = re.compile(
    r"\b(case|cover|sleeve|pouch|holster|skin|decal|wrap|"
    r"screen protector|protector|tempered glass|"
    r"ear ?pads?|ear ?tips?|ear ?cushions?|foam tips?|"
    r"charger|charging (?:cable|dock|stand|pad)|cable|cord|adapter|adaptor|dock|"
    r"stand|mount|holder|bracket|tripod|grip|"
    r"strap|band|lanyard|"
    r"replacement|spare|refill|filter|"
    r"lens cap|hood|remote|stylus|"
    r"carrying|travel case|hard case)\b",
    re.I,
)

# "for the Sony WH-1000XM5", "compatible with", "fits" — an explicit statement
# that the listing is not the named product.
_FOR_PATTERN = re.compile(
    r"\b(for|compatible with|fits|designed for|suitable for|replacement for)\s+"
    r"(the\s+)?[a-z0-9]",
    re.I,
)


def _accessory_signals(title: str) -> List[str]:
    """Evidence that a title describes an accessory rather than a product."""
    normalized = _normalize(title)
    signals = []
    accessory = _ACCESSORY_WORDS.search(normalized)
    if accessory:
        signals.append(f"accessory term '{accessory.group(1)}'")
    if _FOR_PATTERN.search(normalized):
        signals.append("describes itself as being for another product")
    return signals

_IDENTIFIER_KEYS = ("upc", "ean", "gtin", "gtin13", "gtin12", "isbn", "mpn", "model", "sku")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation that varies between retailers."""
    lowered = (text or "").lower()
    lowered = lowered.replace("&", " and ")
    # Keep inch marks and hyphens inside model numbers; drop everything else.
    lowered = re.sub(r"[^\w\s\-/\".]", " ", lowered)
    return " ".join(lowered.split())


def tokenize(title: str) -> List[str]:
    """Meaningful tokens from a product title."""
    normalized = _normalize(title)
    tokens = []
    for raw in normalized.split():
        token = raw.strip("-/.")
        if not token or token in _STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(token)
    return tokens


# A token that is purely a measurement: "128gb", "0.9l", "24000mah". These
# match the model-token shape (letters and digits mixed) but identify a
# specification, not a model, and treating them as model numbers both credited
# false matches and starved real ones of their strongest signal.
_MEASUREMENT_TOKEN = re.compile(
    r"^\\d+(?:\\.\\d+)?(gb|tb|mb|mm|cm|inch|in|oz|ml|litre|liter|quart|qts|qt|gallon|gal|pint|pt|l|kwh|kw|wh|mah|w|v|khz|ghz|hz|mp|kg|lbs|lb|g|ct|pcs|pc|k|p)s?$",
    re.I,
)


# Product-line words after which a bare number is a generation/model number,
# not an arbitrary quantity: "iPhone 13", "Pixel 8", "PS5" (already alnum),
# "Galaxy S24" (already alnum) — this only covers names where the generation
# is a plain digit with no letter of its own, so it never matches _MODEL_TOKEN.
_GENERATION_WORD = {
    "iphone", "ipad", "imac", "macbook", "watch", "airpods",
    "pixel", "galaxy", "playstation", "ps", "xbox", "switch",
    "surface", "note", "fold", "flip", "mark", "mk", "gt",
}


def model_tokens(title: str) -> List[str]:
    """Tokens that look like model identifiers.

    Compared with separators removed, since retailers disagree about them:
    "WH-1000XM5", "WH1000XM5" and "wh 1000xm5" are one product.

    A bare number right after a recognized product-line word counts too — it
    carries the same identifying weight as a mixed alnum model code, but a
    review title stating just "iPhone 13" would otherwise never earn that
    signal since "13" alone has no letters to match `_MODEL_TOKEN`.
    """
    tokens = tokenize(title)
    found = []
    for index, token in enumerate(tokens):
        if _MEASUREMENT_TOKEN.match(token):
            continue
        if _MODEL_TOKEN.match(token):
            found.append(re.sub(r"[-/]", "", token.lower()))
        elif (
            token.isdigit()
            and len(token) <= 3
            and index > 0
            and tokens[index - 1] in _GENERATION_WORD
        ):
            found.append(token)
    return found


def specs(title: str) -> Dict[str, set]:
    """Numeric specifications stated in a title, grouped by dimension.

    ``"iPhone 13 128GB"`` yields ``{"capacity": {"128gb"}}``. Grouping by
    dimension is what allows a conflict to be detected: two capacities that
    disagree mean two different products.
    """
    found: Dict[str, set] = {}
    normalized = _normalize(title)

    # The trailing (?:\b|") lets an inch mark terminate the unit, so 55" is a
    # size and not an unmatched number.
    pattern = (
        r"(\d+(?:\.\d+)?)\s*"
        r"(gb|tb|mb|mm|cm|inch|in|oz|ml|litre|liter|quart|qts|qt|gallon|gal|pint|pt|l|kwh|kw|wh|mah|w|v|khz|ghz|hz|mp|kg|lbs|lb|g|ct|pcs|pc|k|p|\")"
        r"s?(?:\b|(?=\s)|$)"
    )
    for match in re.finditer(pattern, normalized):
        value, unit = match.group(1), match.group(2)
        if unit == '"':
            unit = "inch"
        dimension = _SPEC_UNITS.get(unit)
        if not dimension:
            continue
        # Normalize the value so "1.0" and "1" compare equal.
        number = float(value)
        canonical = f"{number:g}{unit}"
        found.setdefault(dimension, set()).add(canonical)

    return found


def _identifiers(data: Optional[dict]) -> Dict[str, str]:
    """Pull comparable identifiers out of a loosely shaped dict."""
    if not data:
        return {}
    out = {}
    for key in _IDENTIFIER_KEYS:
        value = data.get(key)
        if value is None:
            continue
        text = re.sub(r"[^a-z0-9]", "", str(value).lower())
        if len(text) >= 4:
            out[key] = text
    return out


def _brand_of(title: str, declared: Optional[str] = None) -> Optional[str]:
    """Brand, from an explicit field or the leading token of the title."""
    if declared and declared.strip():
        return _normalize(declared).split()[0] if _normalize(declared) else None
    tokens = tokenize(title)
    return tokens[0] if tokens else None


@dataclass
class MatchVerdict:
    """Whether two listings are the same product, and why."""

    matched: bool
    score: float
    confidence: str                                  # high | medium | low | none
    reasons: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "score": round(self.score, 3),
            "confidence": self.confidence,
            "reasons": self.reasons,
            "conflicts": self.conflicts,
        }


def score_match(
    query_title: str,
    candidate_title: str,
    *,
    query_meta: Optional[dict] = None,
    candidate_meta: Optional[dict] = None,
    threshold: float = 0.62,
) -> MatchVerdict:
    """Score whether two titles describe the same product.

    ``*_meta`` may carry ``brand`` and any of the identifier keys. Absent
    metadata simply means fewer signals, not a lower score.
    """
    reasons: List[str] = []
    conflicts: List[str] = []

    if not (query_title or "").strip() or not (candidate_title or "").strip():
        return MatchVerdict(False, 0.0, "none", ["one side has no title"], [])

    # --- Hard signals -------------------------------------------------------

    query_ids = _identifiers(query_meta)
    candidate_ids = _identifiers(candidate_meta)
    shared_ids = [
        key for key in query_ids
        if key in candidate_ids and query_ids[key] == candidate_ids[key]
    ]
    if shared_ids:
        # A shared UPC/MPN is definitive; nothing else needs to agree.
        return MatchVerdict(
            True, 1.0, "high",
            [f"identifier match: {', '.join(shared_ids)}"],
            [],
        )

    conflicting_ids = [
        key for key in query_ids
        if key in candidate_ids and query_ids[key] != candidate_ids[key]
    ]
    if conflicting_ids:
        conflicts.append(f"identifiers disagree: {', '.join(conflicting_ids)}")

    query_brand = _brand_of(query_title, (query_meta or {}).get("brand"))
    candidate_brand = _brand_of(candidate_title, (candidate_meta or {}).get("brand"))

    query_tokens = tokenize(query_title)
    candidate_tokens = tokenize(candidate_title)
    query_set, candidate_set = set(query_tokens), set(candidate_tokens)

    # A declared brand that appears nowhere in the other title is a rejection —
    # but only when both sides actually declare one, since a leading token is a
    # guess, not a fact.
    both_declared = bool((query_meta or {}).get("brand")) and bool((candidate_meta or {}).get("brand"))
    if query_brand and candidate_brand and query_brand != candidate_brand:
        if both_declared or (query_brand not in candidate_set and candidate_brand not in query_set):
            conflicts.append(f"brand mismatch: {query_brand} vs {candidate_brand}")

    # --- Specification conflicts -------------------------------------------

    query_specs, candidate_specs = specs(query_title), specs(candidate_title)
    for dimension in set(query_specs) & set(candidate_specs):
        if not (query_specs[dimension] & candidate_specs[dimension]):
            conflicts.append(
                f"{dimension} differs: "
                f"{'/'.join(sorted(query_specs[dimension]))} vs "
                f"{'/'.join(sorted(candidate_specs[dimension]))}"
            )

    # An accessory listing repeats the product's exact name and model, so it
    # scores near-perfectly on similarity. Only reject when the candidate looks
    # like an accessory and the query does not — otherwise a shopper genuinely
    # comparing phone cases could never match anything.
    candidate_accessory = _accessory_signals(candidate_title)
    query_accessory = _accessory_signals(query_title)
    if candidate_accessory and not query_accessory:
        conflicts.append(f"candidate looks like an accessory: {candidate_accessory[0]}")

    # --- Soft signals -------------------------------------------------------

    query_models, candidate_models = model_tokens(query_title), model_tokens(candidate_title)
    shared_models = set(query_models) & set(candidate_models)

    if not shared_models and query_models and candidate_models:
        # Both name a model and they differ: WH-1000XM5 against WH-1000XM4 is
        # the neighbouring-generation trap, which every similarity measure
        # scores highly.
        conflicts.append(
            f"model tokens differ: {'/'.join(query_models[:3])} vs {'/'.join(candidate_models[:3])}"
        )

    # Descriptive overlap, on the tokens that are not model numbers.
    query_words = query_set - set(query_models)
    candidate_words = candidate_set - set(candidate_models)
    dice = 0.0
    if query_words and candidate_words:
        overlap = query_words & candidate_words
        # Dice coefficient: symmetric, and forgiving of the padding retailers
        # add to titles.
        dice = (2 * len(overlap)) / (len(query_words) + len(candidate_words))
        if dice >= 0.3:
            reasons.append(f"descriptive overlap {dice:.2f}: {', '.join(sorted(overlap)[:6])}")

    brand_agrees = bool(query_brand and candidate_brand and query_brand == candidate_brand)
    brand_present = bool(query_brand and query_brand in candidate_set)
    if brand_agrees:
        reasons.append(f"brand agrees: {query_brand}")
    elif brand_present:
        reasons.append(f"brand present in candidate: {query_brand}")

    agreed_specs = [
        dimension for dimension in set(query_specs) & set(candidate_specs)
        if query_specs[dimension] & candidate_specs[dimension]
    ]
    if agreed_specs:
        reasons.append(f"specs agree: {', '.join(sorted(agreed_specs))}")

    # Weighting depends on what evidence exists. A shared model number is the
    # strongest signal available, so it dominates when present. Without one,
    # descriptive overlap has to carry the decision and is weighted to match —
    # capping it low meant real matches (a title differing only by marketing
    # words) could not clear the threshold no matter how similar they were.
    if shared_models:
        reasons.insert(0, f"model token match: {', '.join(sorted(shared_models))}")
        score = 0.60 + 0.28 * dice
    else:
        score = 0.72 * dice

    if brand_agrees:
        score += 0.15
    elif brand_present:
        score += 0.10

    if agreed_specs:
        score += min(0.10, 0.05 * len(agreed_specs))

    score = max(0.0, min(1.0, score))

    # --- Verdict ------------------------------------------------------------

    # A conflict is disqualifying whatever the score. Two titles differing only
    # in capacity score very highly on overlap precisely because they are so
    # similar, which is the failure mode this guards against.
    if conflicts:
        return MatchVerdict(False, score, "none", reasons, conflicts)

    if score >= 0.85:
        confidence = "high"
    elif score >= threshold:
        confidence = "medium"
    elif score >= threshold * 0.7:
        confidence = "low"
    else:
        confidence = "none"

    return MatchVerdict(score >= threshold, score, confidence, reasons, conflicts)


def best_match(
    query_title: str,
    candidates: Sequence[dict],
    *,
    query_meta: Optional[dict] = None,
    threshold: float = 0.62,
) -> Tuple[Optional[dict], MatchVerdict]:
    """Pick the best-matching candidate, if any clears the threshold.

    Each candidate is a dict with at least ``title``, plus optional ``brand``
    and identifier keys. Returns ``(candidate_or_None, verdict)``; the verdict
    describes the best scorer even when it was rejected, so the caller can
    report *why* nothing was used.
    """
    best_candidate: Optional[dict] = None
    best_verdict = MatchVerdict(False, 0.0, "none", ["no candidates"], [])

    for candidate in candidates or []:
        verdict = score_match(
            query_title,
            candidate.get("title", ""),
            query_meta=query_meta,
            candidate_meta=candidate,
            threshold=threshold,
        )
        if verdict.score > best_verdict.score or (verdict.matched and not best_verdict.matched):
            best_candidate, best_verdict = candidate, verdict
        if verdict.matched and verdict.confidence == "high":
            break  # nothing will beat a high-confidence match

    if not best_verdict.matched:
        return None, best_verdict
    return best_candidate, best_verdict

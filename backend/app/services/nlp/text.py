"""Text normalization shared by the detectors.

Kept separate so every detector tokenizes identically — a duplicate-detection
threshold tuned against one tokenizer means nothing if the sentiment detector
splits text differently.
"""

import re
from typing import List

_WORD = re.compile(r"[a-z0-9']+")

# Words too common to carry meaning when comparing two reviews for overlap.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "am",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "mine", "yours",
    "of", "to", "in", "on", "at", "for", "with", "without", "from", "by",
    "about", "into", "over", "after", "before", "up", "down", "out", "off",
    "have", "has", "had", "do", "does", "did", "doing", "done",
    "will", "would", "can", "could", "should", "shall", "may", "might", "must",
    "not", "no", "nor", "so", "as", "just", "also", "too", "very", "much",
    "there", "here", "when", "where", "why", "how", "what", "which", "who",
    "all", "any", "both", "each", "more", "most", "other", "some", "such",
    "only", "own", "same", "get", "got", "one", "two",
})


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation runs."""
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s'%$£€/-]", " ", lowered)
    return " ".join(lowered.split())


def tokens(text: str, drop_stopwords: bool = False) -> List[str]:
    """Word tokens from raw text."""
    found = _WORD.findall((text or "").lower())
    if drop_stopwords:
        return [t for t in found if t not in _STOP]
    return found


def shingles(text: str, size: int = 3) -> set:
    """Overlapping word n-grams, for near-duplicate comparison.

    Word-level shingles rather than characters: they survive the small edits
    that review-spinning tools make (swapped adjectives, reordered clauses)
    while still separating genuinely different reviews.
    """
    words = tokens(text)
    if len(words) < size:
        # Too short to shingle; compare the whole thing as one unit so short
        # reviews are not silently exempt from duplicate detection.
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + size]) for i in range(len(words) - size + 1)}


def jaccard(left: set, right: set) -> float:
    """Overlap of two shingle sets, 0..1."""
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if not intersection:
        return 0.0
    return intersection / len(left | right)


def content_tokens(text: str) -> set:
    """Meaning-carrying tokens, for coarse overlap comparison."""
    return set(tokens(text, drop_stopwords=True))


def dice(left: set, right: set) -> float:
    """Dice coefficient of two sets, 0..1."""
    if not left or not right:
        return 0.0
    return (2 * len(left & right)) / (len(left) + len(right))


def similarity(left: str, right: str) -> float:
    """How alike are two reviews, 0..1.

    A single measure is not enough. Word-trigram overlap is precise but
    brittle on short texts: swapping two words in an eleven-word review drops
    trigram Jaccard to 0.38, which is exactly the edit a review-spinning tool
    makes. Bigrams are more forgiving, and content-token overlap survives
    reordering entirely but says nothing about phrasing.

    So all three are computed and the strongest is taken, with the coarser
    measures discounted to reflect how much weaker their evidence is. This
    keeps templated batches detectable without letting two genuine reviews
    about the same feature look like copies of each other.
    """
    trigram = jaccard(shingles(left, 3), shingles(right, 3))
    bigram = jaccard(shingles(left, 2), shingles(right, 2))
    token = dice(content_tokens(left), content_tokens(right))
    return max(trigram, 0.85 * bigram, 0.75 * token)


def sentence_count(text: str) -> int:
    return max(1, len(re.findall(r"[.!?]+", text or "")) or 1)

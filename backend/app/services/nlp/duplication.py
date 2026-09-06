"""Near-duplicate detection across a review set.

Review farms and generated review batches reuse phrasing. Exact duplicates are
already removed during scraping, so what matters here is *near* duplication:
the same review with an adjective swapped or clauses reordered.

Detection is set-based on word shingles, and clusters are reported rather than
individual pairs — knowing a review belongs to a group of nine near-identical
texts is far more damning, and far more useful in a report, than knowing it
resembles one other review.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set

from app.services.nlp.text import shingles, similarity

logger = logging.getLogger(__name__)


@dataclass
class DuplicationFinding:
    """Duplication evidence for one review."""

    index: int
    max_similarity: float = 0.0
    cluster_size: int = 1
    cluster_id: int = -1
    similar_to: List[int] = field(default_factory=list)


def _candidate_pairs(shingle_sets: Sequence[Set[str]]) -> Dict[int, Set[int]]:
    """Pairs worth comparing, via an inverted index on shingles.

    Comparing every review with every other is O(n^2) in set intersections,
    which is wasteful at 200+ reviews when almost no pairs share any phrasing
    at all. Indexing means only reviews that share at least one bigram are
    ever compared — bigrams rather than trigrams because a spun review may
    share no trigram with its source while still being a copy.
    """
    index: Dict[str, List[int]] = {}
    for position, shingle_set in enumerate(shingle_sets):
        for shingle in shingle_set:
            index.setdefault(shingle, []).append(position)

    candidates: Dict[int, Set[int]] = {i: set() for i in range(len(shingle_sets))}
    for holders in index.values():
        # A shingle shared by nearly everything (common phrasing) tells us
        # nothing and would make this quadratic again.
        if len(holders) > 40:
            continue
        for i, left in enumerate(holders):
            for right in holders[i + 1:]:
                candidates[left].add(right)
                candidates[right].add(left)
    return candidates


def analyze(texts: Sequence[str], threshold: float = 0.55) -> List[DuplicationFinding]:
    """Find near-duplicate groups in a review set.

    ``threshold`` is the shingle-overlap level at which two reviews count as
    near-duplicates.
    """
    findings = [DuplicationFinding(index=i) for i in range(len(texts))]
    if len(texts) < 2:
        return findings

    # Index on bigrams to gather candidates, but score with the composite
    # similarity measure, which is what the threshold is tuned against.
    candidates = _candidate_pairs([shingles(text, 2) for text in texts])

    # Union-find over the near-duplicate relation, so a chain of similar
    # reviews is reported as one cluster.
    parent = list(range(len(texts)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    comparisons = 0
    for left, others in candidates.items():
        for right in others:
            if right <= left:
                continue
            comparisons += 1
            score = similarity(texts[left], texts[right])
            if score <= 0:
                continue
            for side in (left, right):
                if score > findings[side].max_similarity:
                    findings[side].max_similarity = score
            if score >= threshold:
                findings[left].similar_to.append(right)
                findings[right].similar_to.append(left)
                union(left, right)

    clusters: Dict[int, List[int]] = {}
    for position in range(len(texts)):
        clusters.setdefault(find(position), []).append(position)

    for root, members in clusters.items():
        for member in members:
            findings[member].cluster_size = len(members)
            findings[member].cluster_id = root if len(members) > 1 else -1
        # Keep the report readable.
        for member in members:
            findings[member].similar_to = sorted(findings[member].similar_to)[:5]

    logger.debug(
        "duplication: %s reviews, %s comparisons, %s cluster(s) of 2+",
        len(texts), comparisons, sum(1 for m in clusters.values() if len(m) > 1),
    )
    return findings

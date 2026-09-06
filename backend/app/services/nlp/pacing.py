"""Submission-date pacing analysis.

Organic reviews arrive irregularly but spread out. A paid or generated batch
arrives together, so a day holding a disproportionate share of a product's
reviews is evidence about the reviews posted that day.

Two guards against false positives, because clustering has innocent causes:

  * **Imprecise dates are excluded.** "2 weeks ago" is parsed to a concrete
    date during scraping, so a page full of relative dates would collapse onto
    a handful of days and look exactly like a burst. Only dates printed as
    real calendar dates are used here.
  * **A minimum sample is required.** Six reviews across two days is not a
    pattern, and treating it as one would flag every product with few reviews.

A launch spike is also a real phenomenon, so this signal is weighted as
contributing evidence rather than being decisive on its own.
"""

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Raw date strings that were relative when scraped, and so carry only
# approximate day information.
_IMPRECISE = re.compile(r"\b(ago|today|yesterday|just now|recently|last (?:week|month))\b", re.I)


@dataclass
class PacingFinding:
    """Pacing evidence for one review."""

    index: int
    day: Optional[str] = None
    in_burst: bool = False
    burst_size: int = 0
    day_share: float = 0.0
    excluded: bool = False           # date missing or imprecise
    reason: Optional[str] = None


@dataclass
class PacingReport:
    """Set-level pacing statistics."""

    usable_dates: int = 0
    distinct_days: int = 0
    span_days: int = 0
    median_per_day: float = 0.0
    burst_days: Dict[str, int] = field(default_factory=dict)
    analyzed: bool = False
    note: Optional[str] = None


def is_imprecise(date_raw: Optional[str]) -> bool:
    """Was this date relative on the page?"""
    return bool(date_raw and _IMPRECISE.search(date_raw))


def analyze(
    dated: Sequence[Tuple[int, Optional[str], Optional[str]]],
    *,
    min_dated: int = 8,
    burst_multiplier: float = 2.5,
    min_burst: int = 3,
) -> Tuple[List[PacingFinding], PacingReport]:
    """Find submission bursts.

    ``dated`` is ``(index, iso_date, date_raw)`` per review. Returns per-review
    findings and a set-level report.
    """
    findings = {index: PacingFinding(index=index) for index, _, _ in dated}
    report = PacingReport()

    usable: List[Tuple[int, str]] = []
    for index, iso, raw in dated:
        if not iso:
            findings[index].excluded = True
            findings[index].reason = "no parseable date"
            continue
        if is_imprecise(raw):
            findings[index].excluded = True
            findings[index].reason = f"date was relative on the page ({raw!r})"
            findings[index].day = iso
            continue
        findings[index].day = iso
        usable.append((index, iso))

    report.usable_dates = len(usable)

    if len(usable) < min_dated:
        report.note = (
            f"only {len(usable)} review(s) carry a precise date; "
            f"{min_dated} are needed before clustering means anything"
        )
        logger.debug("pacing: %s", report.note)
        return list(findings.values()), report

    counts: Dict[str, int] = {}
    for _, iso in usable:
        counts[iso] = counts.get(iso, 0) + 1

    report.distinct_days = len(counts)
    days = sorted(counts)
    try:
        first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        report.span_days = (last - first).days + 1
    except ValueError:
        report.span_days = len(days)

    per_day = list(counts.values())
    report.median_per_day = statistics.median(per_day)

    # A burst is a day well above the typical daily count. Median plus a
    # multiple of the spread, rather than the mean, because the mean is
    # dragged upward by the very bursts being looked for.
    spread = statistics.pstdev(per_day) if len(per_day) > 1 else 0.0
    threshold = max(min_burst, report.median_per_day + burst_multiplier * max(spread, 0.5))

    for day, count in counts.items():
        if count >= threshold:
            report.burst_days[day] = count

    # A single day holding most of the reviews is a burst regardless of the
    # statistical threshold, which a two-day distribution can defeat.
    dominant_share = 0.6
    for day, count in counts.items():
        if count / len(usable) >= dominant_share and count >= min_burst:
            report.burst_days[day] = count

    for index, iso in usable:
        if iso in report.burst_days:
            findings[index].in_burst = True
            findings[index].burst_size = report.burst_days[iso]
            findings[index].day_share = report.burst_days[iso] / len(usable)
            findings[index].reason = (
                f"{report.burst_days[iso]} of {len(usable)} dated reviews posted on {iso}"
            )

    report.analyzed = True
    logger.debug(
        "pacing: %s dated across %s days (span %s), median %.1f/day, %s burst day(s)",
        len(usable), report.distinct_days, report.span_days,
        report.median_per_day, len(report.burst_days),
    )
    return list(findings.values()), report

/**
 * TrustLens web app.
 *
 * The manual-entry alternative to the browser extension: no page to detect a
 * product from, so the shopper pastes a URL or types a name directly. Once
 * submitted, this uses the exact same backend contract and progressive
 * rendering as the extension popup — reviews, verdict and videos stream in as
 * each stage finishes instead of waiting on the slowest one.
 *
 * No scraping, filtering, or LLM calls happen here, and no credentials live
 * in this file — same rule as the extension.
 */

// Points at the backend's stable Render URL. Overridable via
// `window.__TRUSTLENS_API_BASE__` for local/automated testing only — never
// set by this file itself, so production behavior is unchanged.
const API_BASE = window.__TRUSTLENS_API_BASE__ || "https://truthlens-orbio.onrender.com";
const STREAM_URL = `${API_BASE}/analyze/stream`;
const ANALYZE_URL = `${API_BASE}/analyze`;

// 180s rather than 120s: a cold Render instance plus a slow host scrape plus
// a rate-limited LLM proxy can legitimately stack past two minutes even
// though the request is working correctly — see the "still finishing up"
// path below, which keeps whatever streamed in rather than discarding it.
const REQUEST_TIMEOUT_MS = 180000;
const REVIEWS_SHOWN = 3;        // shown initially; "show more" reveals the rest
const REVIEWS_MAX = 8;
const RECENT_MAX = 6;
const RECENT_KEY = "trustlens:recent";
const PROMO_KEY = "trustlens:promo-dismissed";

/**
 * The investigation trail, mirrored from `INVESTIGATION_STAGES` in
 * `backend/app/services/pipeline.py`. Used only to render the rail
 * *before* the real `started` event arrives (so the shopper sees the full
 * checklist immediately, all "pending") — the moment a real event lands,
 * the rail is rebuilt from the backend's own `investigation` list, and every
 * status change after that comes from a real `stage` SSE event. This
 * constant is never used to fabricate progress on its own.
 */
const FALLBACK_STAGES = [
  ["product_identification", "Product identified"],
  ["product_info_collected", "Product information collected"],
  ["reviews_collected", "Reviews collected"],
  ["sentiment_analyzed", "Customer sentiment analyzed"],
  ["pattern_analysis", "Checking recurring patterns"],
  ["review_reliability", "Checking review reliability"],
  ["external_research", "Searching external sources"],
  ["claim_research", "Cross-checking product claims"],
  ["evidence_synthesis", "Synthesizing evidence"],
  ["trust_score", "Calculating Trust Score"],
  ["verdict", "Generating verdict"],
].map(([id, label]) => ({ id, label }));

const STAGE_ICON = { pending: "○", running: "⟳", complete: "✓", failed: "✕", skipped: "–" };

// Safe, user-facing copy keyed by the backend's error `code`. Mirrors
// ERROR_COPY in backend/app/routes/analyze.py — never render a raw
// exception or backend-internal string, only one of these.
const ERROR_COPY = {
  invalid_url: {
    title: "Invalid product URL",
    body: "That doesn't appear to be a valid product URL. Please enter a product page URL.",
  },
  unsupported_target: {
    title: "Unsupported URL",
    body: "TrustLens can't analyze that link. Please use a public product page URL.",
  },
  rate_limited: {
    title: "Too many requests",
    body: "TrustLens is receiving a lot of requests right now. Please wait a moment and try again.",
  },
  server_error: {
    title: "Investigation failed",
    body: "TrustLens couldn't complete the investigation. Please try again.",
  },
};

const $ = (id) => document.getElementById(id);

const ui = {
  badge: $("badge"),
  productName: $("product-name"),
  productImage: $("product-image"),

  rail: $("rail"),
  railList: $("rail-list"),
  main: $("main"),

  partialBanner: $("partial-banner"),
  partialBannerText: $("partial-banner-text"),

  promo: $("promo"),
  promoDismiss: $("promo-dismiss"),

  entry: $("entry"),
  entryForm: $("entry-form"),
  entryInput: $("entry-input"),
  entryError: $("entry-error"),
  recent: $("recent"),
  recentClear: $("recent-clear"),

  stateError: $("state-error"),
  errorTitle: $("error-title"),
  errorBody: $("error-body"),
  errorHint: $("error-hint"),
  stateThin: $("state-thin"),
  thinTitle: $("thin-title"),
  thinBody: $("thin-body"),

  secHero: $("sec-hero"),
  heroScore: $("hero-score"),
  heroRec: $("hero-rec"),
  heroStars: $("hero-stars"),
  heroConfidence: $("hero-confidence"),
  heroClaim: $("hero-claim"),

  secSummary: $("sec-summary"),
  summaryBody: $("summary-body"),
  summaryEmpty: $("summary-empty"),
  summaryConfidence: $("summary-confidence"),
  trustScore: $("trust-score"),
  starRating: $("star-rating"),
  verdict: $("verdict"),
  pros: $("pros"),
  cons: $("cons"),
  prosCol: $("pros-col"),
  consCol: $("cons-col"),
  caveats: $("caveats"),

  secBreakdown: $("sec-breakdown"),
  breakdown: $("breakdown"),

  secRisk: $("sec-risk"),
  riskLevel: $("risk-level"),
  riskSignals: $("risk-signals"),
  riskEmpty: $("risk-empty"),
  riskDisclaimer: $("risk-disclaimer"),

  secThemes: $("sec-themes"),
  themes: $("themes"),

  secReasons: $("sec-reasons"),
  reasonsBuyCol: $("reasons-buy-col"),
  reasonsBuy: $("reasons-buy"),
  reasonsTwiceCol: $("reasons-twice-col"),
  reasonsTwice: $("reasons-twice"),

  secReviews: $("sec-reviews"),
  reviews: $("reviews"),
  reviewsCount: $("reviews-count"),
  reviewsEmpty: $("reviews-empty"),
  reviewsMore: $("reviews-more"),

  secVideos: $("sec-videos"),
  videos: $("videos"),
  videosCount: $("videos-count"),
  videosEmpty: $("videos-empty"),

  retry: $("retry"),
  footMeta: $("foot-meta"),
  debugToggle: $("debug-toggle"),
  debug: $("debug"),
  debugBody: $("debug-body"),
};

/** Everything received this run, for the diagnostics panel. */
let diagnostics = {};
let hiddenReviews = [];
let lastSubmission = null;
/** Has any reviews/videos/summary event already rendered content this run? */
let hasPartialResults = false;

/* --------------------------------------------------------------- utility */

function show(node, visible = true) {
  if (node) node.hidden = !visible;
}

function setBadge(text, kind) {
  ui.badge.textContent = text;
  ui.badge.className = `badge badge--${kind}`;
  show(ui.badge, true);
}

/**
 * (Re)build the investigation rail from a backend-supplied stage list.
 *
 * Every row starts "pending" — nothing here claims work has started. Status
 * only ever changes in response to a real `stage` SSE event (see
 * `applyEvent`'s "stage" case), never a timer.
 */
function buildRail(stages) {
  if (!ui.railList) return;
  ui.railList.replaceChildren(
    ...stages.map(({ id, label }) => {
      const row = document.createElement("div");
      row.className = "rail__step";
      row.dataset.step = id;
      row.dataset.state = "pending";

      const icon = document.createElement("span");
      icon.className = "rail__icon";
      icon.textContent = STAGE_ICON.pending;

      const text = document.createElement("span");
      text.className = "rail__text";
      const labelEl = document.createElement("span");
      labelEl.className = "rail__label";
      labelEl.textContent = label;
      const detailEl = document.createElement("span");
      detailEl.className = "rail__detail";
      detailEl.hidden = true;
      text.append(labelEl, detailEl);

      row.append(icon, text);
      return row;
    })
  );
}

/** Apply a real stage update to one rail row. `status` drives the icon/color; `detail` is optional context (why a step failed or was skipped). */
function setStep(stepId, status, detail) {
  const node = ui.railList && ui.railList.querySelector(`[data-step="${stepId}"]`);
  if (!node) return;
  node.dataset.state = status;
  const icon = node.querySelector(".rail__icon");
  if (icon) icon.textContent = STAGE_ICON[status] || "○";
  const detailEl = node.querySelector(".rail__detail");
  if (detailEl) {
    if (detail) {
      detailEl.textContent = detail;
      detailEl.title = detail;
      detailEl.hidden = false;
    } else {
      detailEl.hidden = true;
    }
  }
}

/** Reset every section to its loading state, ready for a fresh analysis. */
function resetView() {
  diagnostics = {};
  hiddenReviews = [];
  hasPartialResults = false;

  show(ui.rail, true);
  buildRail(FALLBACK_STAGES);
  show(ui.partialBanner, false);

  show(ui.stateError, false);
  show(ui.stateThin, false);

  for (const [section, body] of [
    [ui.secSummary, ui.summaryBody],
    [ui.secReviews, ui.reviews],
    [ui.secVideos, ui.videos],
  ]) {
    show(section, true);
    show(body, false);
  }

  show(ui.secHero, false);
  show(ui.secBreakdown, false);
  show(ui.secRisk, false);
  show(ui.secThemes, false);
  show(ui.secReasons, false);
  ui.breakdown.replaceChildren();
  ui.riskSignals.replaceChildren();
  ui.themes.replaceChildren();
  ui.reasonsBuy.replaceChildren();
  ui.reasonsTwice.replaceChildren();
  show(ui.riskEmpty, false);
  show(ui.heroRec, false);
  show(ui.heroClaim, false);
  ui.heroScore.textContent = "–";
  ui.heroStars.textContent = "";
  ui.heroConfidence.textContent = "";

  ui.pros.replaceChildren();
  ui.cons.replaceChildren();
  ui.caveats.replaceChildren();
  ui.reviews.replaceChildren();
  ui.videos.replaceChildren();
  show(ui.caveats, false);
  show(ui.summaryEmpty, false);
  show(ui.reviewsEmpty, false);
  show(ui.videosEmpty, false);
  show(ui.reviewsMore, false);
  show(ui.summaryConfidence, false);
  show(ui.trustScore, false);
  show(ui.starRating, false);
  show(ui.productImage, false);
  if (ui.productImage) ui.productImage.src = "";
  ui.reviewsCount.textContent = "";
  ui.videosCount.textContent = "";
  ui.footMeta.textContent = "";
  show(ui.debug, false);
  show(ui.retry, true);
  show(ui.debugToggle, true);
}

function hideResults() {
  show(ui.rail, false);
  show(ui.partialBanner, false);
  show(ui.secHero, false);
  show(ui.secSummary, false);
  show(ui.secBreakdown, false);
  show(ui.secRisk, false);
  show(ui.secThemes, false);
  show(ui.secReasons, false);
  show(ui.secReviews, false);
  show(ui.secVideos, false);
}

function fail(title, body, hint) {
  setBadge("error", "error");
  ui.errorTitle.textContent = title;
  ui.errorBody.textContent = body;
  if (hint) {
    ui.errorHint.textContent = hint;
    show(ui.errorHint, true);
  } else {
    show(ui.errorHint, false);
  }
  show(ui.stateError, true);
  show(ui.retry, true);
}

/** Non-blocking notice: a verdict was produced, but a real source failed along the way. */
function showPartialBanner(reasons) {
  const detail = (reasons || []).length
    ? ` ${reasons.join("; ")}.`
    : "";
  ui.partialBannerText.textContent =
    "Review analysis completed, but some external evidence was unavailable. " +
    "TrustLens reduced confidence accordingly." + detail;
  show(ui.partialBanner, true);
}

/* ---------------------------------------------------------------- recents */

function loadRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

function saveRecent(value) {
  try {
    const current = loadRecent().filter((entry) => entry !== value);
    current.unshift(value);
    localStorage.setItem(RECENT_KEY, JSON.stringify(current.slice(0, RECENT_MAX)));
  } catch {
    /* localStorage unavailable (private browsing, etc.) — not essential */
  }
}

function renderRecent() {
  const entries = loadRecent();
  ui.recent.replaceChildren();
  show(ui.recent, entries.length > 0);
  show(ui.recentClear, entries.length > 0);
  for (const value of entries) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "recent__chip";
    chip.textContent = value;
    chip.title = value;
    chip.addEventListener("click", () => {
      ui.entryInput.value = value;
      submitEntry();
    });
    ui.recent.appendChild(chip);
  }
}

function clearRecent() {
  try {
    localStorage.removeItem(RECENT_KEY);
  } catch {
    /* localStorage unavailable — nothing to clear */
  }
  renderRecent();
}

/* ------------------------------------------------------------------ promo */

function initPromo() {
  let dismissed = false;
  try {
    dismissed = sessionStorage.getItem(PROMO_KEY) === "1";
  } catch {
    /* ignore */
  }
  show(ui.promo, !dismissed);
  ui.promoDismiss.addEventListener("click", () => {
    show(ui.promo, false);
    try {
      sessionStorage.setItem(PROMO_KEY, "1");
    } catch {
      /* ignore */
    }
  });
}

/* -------------------------------------------------------------- rendering */

const STAR = "★";
const HALF = "½";

function stars(rating) {
  if (typeof rating !== "number") return "";
  const whole = Math.floor(rating);
  return STAR.repeat(whole) + (rating - whole >= 0.5 ? HALF : "");
}

function formatDate(iso) {
  if (!iso) return "";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short" });
}

function formatDuration(seconds) {
  if (!seconds) return "";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `${minutes}:${String(rest).padStart(2, "0")}` : `0:${String(rest).padStart(2, "0")}`;
}

function formatViews(views) {
  if (!views) return "";
  if (views >= 1e6) return `${(views / 1e6).toFixed(1)}M views`;
  if (views >= 1e3) return `${Math.round(views / 1e3)}K views`;
  return `${views} views`;
}

function bulletList(target, items) {
  target.replaceChildren();
  for (const item of items || []) {
    const li = document.createElement("li");
    li.textContent = item;
    target.appendChild(li);
  }
}

const RECOMMENDATION_LABEL = {
  BUY_WITH_CONFIDENCE: "Buy with confidence",
  BUY_WITH_CAUTION: "Buy with caution",
  PROCEED_WITH_CAUTION: "Proceed with caution",
  AVOID: "Consider avoiding",
  INSUFFICIENT_DATA: "Not enough data",
};

const RECOMMENDATION_STYLE = {
  BUY_WITH_CONFIDENCE: "buy",
  BUY_WITH_CAUTION: "caution",
  PROCEED_WITH_CAUTION: "caution",
  AVOID: "avoid",
  INSUFFICIENT_DATA: "unknown",
};

function renderHero(summary) {
  const hasScore = typeof summary.trust_score === "number";
  ui.heroScore.textContent = hasScore ? Math.round(summary.trust_score) : "–";

  const rec = summary.recommendation || "INSUFFICIENT_DATA";
  const style = RECOMMENDATION_STYLE[rec] || "unknown";
  ui.heroRec.textContent = RECOMMENDATION_LABEL[rec] || rec;
  ui.heroRec.className = `badge hero__rec hero__rec--${style}`;
  show(ui.heroRec, true);

  ui.heroStars.textContent =
    typeof summary.star_rating === "number"
      ? stars(summary.star_rating) || `${summary.star_rating}/5`
      : "";

  const level = ["high", "medium", "low"].includes(summary.confidence) ? summary.confidence : "none";
  ui.heroConfidence.textContent = `${level} confidence verdict`;

  if (summary.claim_check) {
    ui.heroClaim.textContent = summary.claim_check;
    show(ui.heroClaim, true);
  } else {
    show(ui.heroClaim, false);
  }

  show(ui.secHero, true);
}

function renderBreakdown(breakdown) {
  const components = (breakdown || {}).components || [];
  if (!components.length) {
    show(ui.secBreakdown, false);
    return;
  }

  ui.breakdown.replaceChildren(
    ...components.map((component) => {
      const li = document.createElement("li");
      li.className = "breakdown__row";

      const label = document.createElement("span");
      label.className = "breakdown__label";
      label.textContent = component.label || component.key || "";

      const track = document.createElement("span");
      track.className = "breakdown__track";
      const fill = document.createElement("span");
      fill.className = "breakdown__fill";
      const pct = typeof component.value === "number" ? Math.max(0, Math.min(100, component.value)) : 0;
      fill.style.width = `${pct}%`;
      track.appendChild(fill);

      const value = document.createElement("span");
      value.className = "breakdown__value";
      value.textContent =
        typeof component.value === "number"
          ? `${Math.round(component.value)}/100`
          : component.note || "Not enough data";

      li.append(label, track, value);
      return li;
    })
  );
  show(ui.secBreakdown, true);
}

function renderRisk(risk) {
  if (!risk) {
    show(ui.secRisk, false);
    return;
  }

  const level = ["low", "medium", "high"].includes(risk.level) ? risk.level : "insufficient_data";
  ui.riskLevel.textContent =
    level === "insufficient_data" ? "not enough data" : `${level} risk`;
  ui.riskLevel.className = `chip chip--risk-${level}`;
  show(ui.riskLevel, true);

  const signals = risk.signals || [];
  ui.riskSignals.replaceChildren(
    ...signals.map((signal) => {
      const li = document.createElement("li");
      li.textContent = signal.detail || signal.label || "";
      return li;
    })
  );

  show(ui.riskEmpty, signals.length === 0);
  if (!signals.length) {
    ui.riskEmpty.textContent =
      level === "insufficient_data"
        ? "Not enough review data to assess patterns."
        : "No unusual patterns observed.";
  }

  ui.riskDisclaimer.textContent =
    risk.disclaimer ||
    "Review Risk is an AI-generated assessment of observable patterns, not a definitive determination of review authenticity.";

  show(ui.secRisk, true);
}

function renderThemes(themes) {
  if (!(themes || []).length) {
    show(ui.secThemes, false);
    return;
  }

  ui.themes.replaceChildren(
    ...themes.map((theme) => {
      const li = document.createElement("li");
      const sentiment = ["positive", "negative", "mixed"].includes(theme.sentiment)
        ? theme.sentiment
        : "mixed";
      li.className = `theme theme--${sentiment}`;

      const dot = document.createElement("span");
      dot.className = "theme__dot";

      const label = document.createElement("span");
      label.textContent = theme.label || "";

      li.append(dot, label);
      if (theme.mention_count) {
        const count = document.createElement("span");
        count.className = "theme__count";
        count.textContent = `×${theme.mention_count}`;
        li.appendChild(count);
      }
      return li;
    })
  );
  show(ui.secThemes, true);
}

function renderReasons(buy, thinkTwice) {
  const hasBuy = (buy || []).length > 0;
  const hasTwice = (thinkTwice || []).length > 0;

  bulletList(ui.reasonsBuy, buy);
  bulletList(ui.reasonsTwice, thinkTwice);
  show(ui.reasonsBuyCol, hasBuy);
  show(ui.reasonsTwiceCol, hasTwice);
  show(ui.secReasons, hasBuy || hasTwice);
}

function renderSummary(summary, llm) {
  const hasContent =
    summary && (summary.verdict || (summary.pros || []).length || (summary.cons || []).length);

  renderRisk(summary && summary.review_risk);
  renderBreakdown(summary && summary.score_breakdown);

  if (!hasContent) {
    ui.summaryEmpty.textContent =
      llm && llm.error
        ? `No verdict: ${llm.error}`
        : "No verdict could be produced from these reviews.";
    show(ui.summaryEmpty, true);
    show(ui.secHero, false);
    show(ui.secThemes, false);
    show(ui.secReasons, false);
    return;
  }

  renderHero(summary);
  renderThemes(summary.themes);
  renderReasons(summary.reasons_to_buy, summary.reasons_to_think_twice);

  ui.verdict.textContent = summary.verdict || "";

  const level = ["high", "medium", "low"].includes(summary.confidence) ? summary.confidence : "none";
  ui.summaryConfidence.textContent = `${level} confidence`;
  ui.summaryConfidence.className = `chip chip--${level}`;
  show(ui.summaryConfidence, true);

  if (typeof summary.trust_score === "number") {
    ui.trustScore.textContent = `${Math.round(summary.trust_score)}/100 trust`;
    show(ui.trustScore, true);
  } else {
    show(ui.trustScore, false);
  }

  if (typeof summary.star_rating === "number") {
    ui.starRating.textContent = stars(summary.star_rating) || `${summary.star_rating}/5`;
    ui.starRating.title = `${summary.star_rating} out of 5`;
    show(ui.starRating, true);
  } else {
    show(ui.starRating, false);
  }

  bulletList(ui.pros, summary.pros);
  bulletList(ui.cons, summary.cons);
  // A column with nothing in it reads as a rendering bug; drop it instead.
  show(ui.prosCol, (summary.pros || []).length > 0);
  show(ui.consCol, (summary.cons || []).length > 0);

  bulletList(ui.caveats, summary.caveats);
  show(ui.caveats, (summary.caveats || []).length > 0);

  show(ui.summaryBody, true);
}

/**
 * Pick which reviews to show as evidence.
 *
 * Only reviews that passed filtering are eligible. Preference goes to ones a
 * shopper can actually weigh — substantial text, a rating, a verified badge —
 * and the selection deliberately mixes positive with critical, because showing
 * five glowing reviews under a mixed verdict looks like cherry-picking.
 */
function selectEvidence(reviews) {
  const eligible = (reviews || []).filter((review) => {
    const verdict = review.filter || {};
    return verdict.passed !== false && (review.text || "").length > 40;
  });

  const score = (review) =>
    Math.min((review.text || "").length, 400) / 400 +
    (review.verified_purchase === true ? 0.5 : 0) +
    (typeof review.rating === "number" ? 0.3 : 0) +
    Math.min(review.helpful_votes || 0, 50) / 100;

  const ranked = [...eligible].sort((a, b) => score(b) - score(a));
  const critical = ranked.filter((r) => typeof r.rating === "number" && r.rating <= 3);
  const positive = ranked.filter((r) => !(typeof r.rating === "number" && r.rating <= 3));

  // Interleave so the list is not all one sentiment, then fall back to rank.
  const mixed = [];
  while (mixed.length < REVIEWS_MAX && (positive.length || critical.length)) {
    if (positive.length) mixed.push(positive.shift());
    if (critical.length && mixed.length < REVIEWS_MAX) mixed.push(critical.shift());
  }
  return mixed;
}

function reviewNode(review) {
  const li = document.createElement("li");
  li.className = "review";

  const head = document.createElement("div");
  head.className = "review__head";

  if (typeof review.rating === "number") {
    const rating = document.createElement("span");
    rating.className = "stars";
    rating.textContent = stars(review.rating);
    rating.title = `${review.rating} out of 5`;
    head.appendChild(rating);
  }
  if (review.source) {
    const source = document.createElement("span");
    source.className = "source";
    source.textContent = review.source;
    head.appendChild(source);
  }
  if (review.verified_purchase === true) {
    const verified = document.createElement("span");
    verified.className = "verified";
    verified.textContent = "verified";
    head.appendChild(verified);
  }
  const date = document.createElement("span");
  date.className = "review__date";
  date.textContent = formatDate(review.date) || review.date_raw || "";
  head.appendChild(date);

  const text = document.createElement("p");
  text.className = "review__text";
  const body = review.text || "";
  text.textContent = body.length > 320 ? `${body.slice(0, 320).trimEnd()}…` : body;

  li.append(head, text);
  return li;
}

function renderReviews(event) {

  const passed = event.reviews_passed || 0;
  const total = (event.reviews || []).length;
  const evidence = selectEvidence(event.reviews);

  ui.reviewsCount.textContent = total
    ? `${passed} of ${total} passed filtering`
    : "";

  if (!evidence.length) {
    ui.reviewsEmpty.textContent = total
      ? "No reviews survived filtering, so none are shown as evidence."
      : "No reviews were found for this product.";
    show(ui.reviewsEmpty, true);
    return;
  }

  const first = evidence.slice(0, REVIEWS_SHOWN);
  hiddenReviews = evidence.slice(REVIEWS_SHOWN);

  ui.reviews.replaceChildren(...first.map(reviewNode));
  show(ui.reviews, true);

  if (hiddenReviews.length) {
    ui.reviewsMore.textContent = `Show ${hiddenReviews.length} more review${
      hiddenReviews.length > 1 ? "s" : ""
    }`;
    show(ui.reviewsMore, true);
  }
}

function videoNode(video) {
  const li = document.createElement("li");
  const link = document.createElement("a");
  link.className = "video";
  link.href = video.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";

  const platform = document.createElement("span");
  const name = (video.source || "").toLowerCase();
  platform.className = `video__platform video__platform--${name || "other"}`;
  platform.textContent = name === "youtube" ? "YT" : name === "tiktok" ? "TT" : name.slice(0, 4);

  const text = document.createElement("div");
  text.className = "video__text";

  const title = document.createElement("div");
  title.className = "video__title";
  title.textContent = video.title || "(untitled)";

  const meta = document.createElement("div");
  meta.className = "video__meta";
  meta.textContent = [video.channel, formatDuration(video.duration_seconds), formatViews(video.views)]
    .filter(Boolean)
    .join(" · ");

  const arrow = document.createElement("span");
  arrow.className = "video__arrow";
  arrow.textContent = "↗";

  text.append(title, meta);
  link.append(platform, text, arrow);
  li.appendChild(link);
  return li;
}

/** Videos arrive per platform, so they are appended rather than replaced. */
function appendVideos(videos) {
  if (!videos || !videos.length) return;
  show(ui.videos, true);
  for (const video of videos) ui.videos.appendChild(videoNode(video));
  ui.videosCount.textContent = `${ui.videos.children.length} found`;
}

function finishVideos(videoSources) {
  if (ui.videos.children.length) return;

  const blocked = (videoSources || []).filter((source) => source.blocked || source.error);
  const notes = (videoSources || []).flatMap((source) => source.notes || []);
  ui.videosEmpty.textContent =
    notes[0] ||
    (blocked.length
      ? `No videos: ${blocked[0].error || "platform unavailable"}`
      : "No review videos found for this product.");
  show(ui.videosEmpty, true);
}

function renderDone(event) {
  setBadge("done", "ok");

  const parts = [];
  if ((event.contributed || []).length) parts.push(event.contributed.join(", "));
  if (event.duration_ms) parts.push(`${(event.duration_ms / 1000).toFixed(1)}s`);
  ui.footMeta.textContent = parts.join(" · ");

  finishVideos(event.video_sources);

  if (event.status === "not_enough_data" && !hasPartialResults) {
    // Truly nothing useful was produced — no reviews, no videos, no verdict.
    // This is the one case that blocks the results area, using the exact
    // "insufficient data" copy a shopper needs to understand why.
    hideResults();
    setBadge("insufficient data", "warn");
    if (ui.thinTitle) ui.thinTitle.textContent = "Not enough data";
    ui.thinBody.textContent =
      "There isn't enough review evidence to produce a reliable verdict." +
      (event.message ? ` ${event.message}` : "");
    show(ui.stateThin, true);
    show(ui.retry, true);
  } else if (event.status === "not_enough_data") {
    // Some real evidence did render (e.g. a fallback verdict from video
    // commentary) even though reviews were thin — say so without hiding it.
    showPartialBanner([event.message].filter(Boolean));
  } else if (event.partial) {
    showPartialBanner(event.partial_reasons);
  }
}

/* ------------------------------------------------------------------ backend */

function applyEvent(event) {
  const diagKey =
    event.event === "videos" ? `videos:${event.source}`
    : event.event === "stage" ? `stage:${event.id}`
    : event.event;
  diagnostics[diagKey] = event;

  switch (event.event) {
    case "started":
      setBadge("analyzing", "pending");
      if (Array.isArray(event.investigation) && event.investigation.length) {
        buildRail(event.investigation);
      }
      break;
    case "stage":
      // The one and only source of rail progress: a real backend checkpoint.
      setStep(event.id, event.status, event.detail);
      break;
    case "source":
      // Progress only — reviews themselves arrive once filtered.
      if (event.kind === "host" && event.report) {
        const report = event.report;
        ui.reviewsCount.textContent = report.blocked
          ? `${report.source} blocked the scrape`
          : `${report.count} from ${report.source}…`;
        if (report.image_url && ui.productImage) {
          ui.productImage.src = report.image_url;
          show(ui.productImage, true);
        }
      }
      break;
    case "reviews":
      renderReviews(event);
      // Only real evidence counts — an empty reviews list is not "results
      // already showing", it's the same "nothing found" case as no event
      // at all, and should still be eligible for the blocking insufficient-
      // data state below rather than a banner over empty sections.
      if ((event.reviews_passed || 0) > 0) hasPartialResults = true;
      break;
    case "videos":
      appendVideos(event.videos);
      if ((event.videos || []).length > 0) hasPartialResults = true;
      break;
    case "summary": {
      renderSummary(event.summary, event.llm);
      const summary = event.summary || {};
      if (summary.verdict || (summary.pros || []).length || (summary.cons || []).length) {
        hasPartialResults = true;
      }
      break;
    }
    case "match":
      break;
    case "done":
      renderDone(event);
      break;
    case "error": {
      const copy = ERROR_COPY[event.code] || null;
      if (hasPartialResults) {
        // Evidence already rendered successfully before this failed — keep
        // it visible and say so, rather than replacing it with a scary error.
        showPartialBanner([copy ? copy.body : "The investigation could not finish."]);
      } else {
        hideResults();
        fail(
          copy ? copy.title : "Investigation failed",
          copy ? copy.body : "TrustLens couldn't complete the investigation. Please try again."
        );
      }
      break;
    }
    default:
      break;
  }
}

/**
 * Consume the streaming endpoint.
 *
 * EventSource cannot issue a POST, so the SSE frames are read off the response
 * body and split manually. Returns false if streaming could not be used at
 * all, so the caller can fall back to the single-response endpoint.
 */
async function runStream(body, signal) {
  const response = await fetch(STREAM_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) return false;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawEvent = false;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line; anything after the last one is
    // a partial frame and stays in the buffer.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      let parsed;
      try {
        parsed = JSON.parse(line.slice(5).trim());
        sawEvent = true;
      } catch (error) {
        console.debug("[TrustLens] unparseable event frame", error);
        continue;
      }
      // A render bug on one event must not abort the whole stream — the
      // remaining, still-arriving events (including the real `done`/`error`)
      // deserve a chance to render too.
      try {
        applyEvent(parsed);
      } catch (error) {
        console.error("[TrustLens] failed to apply event", parsed && parsed.event, error);
      }
    }
  }
  return sawEvent;
}

/** Single-response fallback, for when streaming is unavailable. */
async function runOnce(body, signal) {
  const response = await fetch(ANALYZE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    let detail = null;
    try {
      detail = (await response.json()).detail;
    } catch {
      /* no JSON body */
    }
    const error = new Error(
      (detail && detail.message) || `Backend returned HTTP ${response.status}`
    );
    if (detail && detail.code) error.code = detail.code;
    else if (response.status === 429) error.code = "rate_limited";
    else if (response.status >= 500) error.code = "server_error";
    throw error;
  }

  const data = await response.json();
  buildRail(FALLBACK_STAGES);
  for (const stageEvent of deriveStageEvents(data)) applyEvent(stageEvent);
  if (data.image_url && ui.productImage) {
    ui.productImage.src = data.image_url;
    show(ui.productImage, true);
  }
  applyEvent({ event: "reviews", reviews: data.reviews, reviews_passed: data.reviews_passed,
               filter_report: data.filter_report, sources: data.sources });
  applyEvent({ event: "summary", summary: data.summary, llm: data.llm });
  applyEvent({ event: "videos", source: "all", videos: data.videos, report: {} });
  applyEvent({ event: "done", status: data.status, message: data.message,
               contributed: data.contributed, video_sources: data.video_sources,
               duration_ms: (data.llm || {}).duration_ms,
               partial: data.partial, partial_reasons: data.partial_reasons });
}

/**
 * Derive investigation-rail statuses from a *finished* single-response
 * result (used only by the non-streaming fallback, where there is no live
 * stage event). Mirrors `_cached_stage_events` in
 * `backend/app/routes/analyze.py` — every status below reads a field the
 * backend actually returned, never a guess or a timer.
 */
function deriveStageEvents(data) {
  const summary = data.summary || {};
  const reviews = data.reviews || [];
  const reviewsPassed = data.reviews_passed || 0;
  const sources = data.sources || [];
  const host = sources.find((s) => s.source && s.source !== "competitor") || {};
  const attemptedCompetitor = sources.some((s) => s.source === "competitor");
  const videosPresent = (data.videos || []).length > 0;
  const hasVerdict = Boolean(summary.verdict);

  const events = [{ event: "stage", id: "product_identification", status: "complete" }];
  events.push({
    event: "stage", id: "product_info_collected",
    status: "complete",
    detail: host.image_url || host.description ? "Image and/or description found" : "No image or description found",
  });
  events.push({
    event: "stage", id: "reviews_collected",
    status: reviewsPassed ? "complete" : "failed",
    detail: `${reviewsPassed} of ${reviews.length} review(s) passed filtering`,
  });
  events.push({ event: "stage", id: "sentiment_analyzed", status: reviews.length ? "complete" : "skipped" });
  events.push({ event: "stage", id: "pattern_analysis", status: reviews.length ? "complete" : "skipped" });
  events.push({ event: "stage", id: "review_reliability", status: summary.review_risk ? "complete" : "skipped" });
  events.push({
    event: "stage", id: "external_research",
    status: !attemptedCompetitor && !videosPresent ? "skipped" : (videosPresent || attemptedCompetitor ? "complete" : "failed"),
  });
  events.push({ event: "stage", id: "claim_research", status: summary.claim_check ? "complete" : "skipped" });
  events.push({ event: "stage", id: "evidence_synthesis", status: hasVerdict ? "complete" : "failed" });
  events.push({ event: "stage", id: "trust_score", status: hasVerdict ? "complete" : "failed" });
  events.push({ event: "stage", id: "verdict", status: hasVerdict ? "complete" : "failed" });
  return events;
}

function explainNetwork(error) {
  // A rejection from runOnce carries a backend error `code` — always prefer
  // the canned copy for that code over guessing from the error's shape.
  if (error.code && ERROR_COPY[error.code]) {
    return [ERROR_COPY[error.code].title, ERROR_COPY[error.code].body, null];
  }
  if (error.name === "AbortError") {
    return [
      "Request timed out",
      `The backend did not finish within ${REQUEST_TIMEOUT_MS / 1000}s. This can happen on a ` +
        "cold backend instance — trying again usually finishes faster.",
      null,
    ];
  }
  // fetch() rejects with a TypeError when it cannot reach the host at all.
  // Matched by name rather than `instanceof`, which silently fails when the
  // error was constructed in a different JavaScript realm.
  if (error.name === "TypeError" || /failed to fetch|networkerror/i.test(error.message || "")) {
    return [
      "Backend unreachable",
      `Nothing is answering at ${API_BASE}. Start it and try again.`,
      "uvicorn app.main:app --reload --port 8000",
    ];
  }
  return [
    "Investigation failed",
    "TrustLens couldn't complete the investigation. Please try again.",
    null,
  ];
}

/* --------------------------------------------------------------------- flow */

async function analyze(body) {
  resetView();
  setBadge("analyzing", "pending");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const streamed = await runStream(body, controller.signal);
    if (!streamed) await runOnce(body, controller.signal);
  } catch (error) {
    if (error.name === "AbortError" && hasPartialResults) {
      // Reviews/videos/summary already streamed in successfully before the
      // timeout fired — wiping them and showing a scary error would throw
      // away a correct answer just because it arrived slowly.
      setBadge("partial", "warn");
      if (ui.thinTitle) ui.thinTitle.textContent = "Still finishing up";
      ui.thinBody.textContent =
        "The verdict is taking longer than expected, but here's what we found so far.";
      show(ui.stateThin, true);
    } else {
      hideResults();
      fail(...explainNetwork(error));
    }
  } finally {
    clearTimeout(timer);
    ui.debugBody.textContent = JSON.stringify(diagnostics, null, 2);
  }
}

/** Parse the entry field into a URL or a plain product name. */
function parseEntry(value) {
  try {
    const parsed = new URL(value);
    // Anything that parses as a URL at all is treated as a URL, even with
    // an unsupported scheme (ftp://, javascript:, etc.) — sending it to the
    // backend gets back the real "invalid URL" rejection with the exact
    // reason, instead of silently reinterpreting it as a product-name
    // search that can never usefully match anything.
    return { url: value, scheme: parsed.protocol.replace(":", "") };
  } catch {
    /* not a URL at all — a bare product name */
  }
  return { name: value };
}

function submitEntry() {
  const value = ui.entryInput.value.trim();
  if (!value) {
    ui.entryError.textContent = "Enter a product URL or name.";
    show(ui.entryError, true);
    return;
  }
  show(ui.entryError, false);

  const parsed = parseEntry(value);
  lastSubmission = value;
  saveRecent(value);
  renderRecent();

  ui.productName.textContent = value;
  show(ui.productName, true);

  // Reflect the lookup in the URL so a result page can be bookmarked/shared —
  // only the product reference is carried, nothing else about the visitor.
  const params = new URLSearchParams(window.location.search);
  params.set(parsed.url ? "url" : "q", value);
  const next = `${window.location.pathname}?${params.toString()}`;
  window.history.replaceState({}, "", next);

  analyze({
    product_url: parsed.url || null,
    product_name: parsed.name || null,
    detection: { source: "manual", confidence: "none", sources: [], site: null },
  });
}

function handleSubmit(event) {
  event.preventDefault();
  submitEntry();
}

ui.entryForm.addEventListener("submit", handleSubmit);
ui.retry.addEventListener("click", () => {
  if (ui.entryInput.value.trim()) submitEntry();
});

ui.reviewsMore.addEventListener("click", () => {
  for (const review of hiddenReviews) ui.reviews.appendChild(reviewNode(review));
  hiddenReviews = [];
  show(ui.reviewsMore, false);
});

ui.debugToggle.addEventListener("click", () => {
  const showing = ui.debug.hidden;
  show(ui.debug, showing);
  ui.debugToggle.textContent = showing ? "Hide details" : "Details";
});

ui.recentClear.addEventListener("click", clearRecent);

/* ------------------------------------------------------------------- start */

if (ui.productImage) {
  ui.productImage.addEventListener("error", () => show(ui.productImage, false));
}

initPromo();
renderRecent();
hideResults();
show(ui.retry, false);
show(ui.debugToggle, false);

// Fire-and-forget: start waking a cold Render instance the moment the page
// loads, before the shopper has finished typing/pasting a URL. This is a
// real request with a real effect on the eventual /analyze latency — not a
// fabricated progress signal, and nothing renders from its result.
fetch(`${API_BASE}/health`, { method: "GET" }).catch(() => {
  /* if the backend is unreachable, the real analyze call will say so */
});

// Bootstrap from a shared link: ?url=<product url> or ?q=<product name>.
const initial = new URLSearchParams(window.location.search);
const initialUrl = initial.get("url");
const initialName = initial.get("q");
if (initialUrl || initialName) {
  ui.entryInput.value = initialUrl || initialName;
  submitEntry();
}

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

// Points at wherever the backend is actually running. This is a Codespaces
// forwarded-port URL, which is ephemeral — update this (and redeploy) if the
// codespace restarts and the URL changes.
const API_BASE = "https://fictional-meme-wq447j99p593959g-8000.app.github.dev";
const STREAM_URL = `${API_BASE}/analyze/stream`;
const ANALYZE_URL = `${API_BASE}/analyze`;

const REQUEST_TIMEOUT_MS = 60000;
const REVIEWS_SHOWN = 3;        // shown initially; "show more" reveals the rest
const REVIEWS_MAX = 8;
const RECENT_MAX = 6;
const RECENT_KEY = "trustlens:recent";
const PROMO_KEY = "trustlens:promo-dismissed";

const $ = (id) => document.getElementById(id);

const ui = {
  badge: $("badge"),
  productName: $("product-name"),
  rail: $("rail"),
  main: $("main"),

  promo: $("promo"),
  promoDismiss: $("promo-dismiss"),

  entry: $("entry"),
  entryForm: $("entry-form"),
  entryInput: $("entry-input"),
  entryError: $("entry-error"),
  recent: $("recent"),

  stateError: $("state-error"),
  errorTitle: $("error-title"),
  errorBody: $("error-body"),
  errorHint: $("error-hint"),
  stateThin: $("state-thin"),
  thinBody: $("thin-body"),

  secSummary: $("sec-summary"),
  skelSummary: $("skel-summary"),
  summaryBody: $("summary-body"),
  summaryEmpty: $("summary-empty"),
  summaryConfidence: $("summary-confidence"),
  verdict: $("verdict"),
  pros: $("pros"),
  cons: $("cons"),
  prosCol: $("pros-col"),
  consCol: $("cons-col"),
  caveats: $("caveats"),

  secReviews: $("sec-reviews"),
  skelReviews: $("skel-reviews"),
  reviews: $("reviews"),
  reviewsCount: $("reviews-count"),
  reviewsEmpty: $("reviews-empty"),
  reviewsMore: $("reviews-more"),

  secVideos: $("sec-videos"),
  skelVideos: $("skel-videos"),
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

/* --------------------------------------------------------------- utility */

function show(node, visible = true) {
  if (node) node.hidden = !visible;
}

function setBadge(text, kind) {
  ui.badge.textContent = text;
  ui.badge.className = `badge badge--${kind}`;
  show(ui.badge, true);
}

function setStep(step, state) {
  const node = ui.rail.querySelector(`[data-step="${step}"]`);
  if (node) node.dataset.state = state;
}

/** Reset every section to its loading state, ready for a fresh analysis. */
function resetView() {
  diagnostics = {};
  hiddenReviews = [];

  show(ui.rail, true);
  for (const step of ["reviews", "summary", "videos"]) setStep(step, "active");

  show(ui.stateError, false);
  show(ui.stateThin, false);

  for (const [section, skeleton, body] of [
    [ui.secSummary, ui.skelSummary, ui.summaryBody],
    [ui.secReviews, ui.skelReviews, ui.reviews],
    [ui.secVideos, ui.skelVideos, ui.videos],
  ]) {
    show(section, true);
    show(skeleton, true);
    show(body, false);
  }

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
  ui.reviewsCount.textContent = "";
  ui.videosCount.textContent = "";
  ui.footMeta.textContent = "";
  show(ui.debug, false);
  show(ui.retry, true);
  show(ui.debugToggle, true);
}

function hideResults() {
  show(ui.rail, false);
  show(ui.secSummary, false);
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

function renderSummary(summary, llm) {
  show(ui.skelSummary, false);
  setStep("summary", "done");

  const hasContent =
    summary && (summary.verdict || (summary.pros || []).length || (summary.cons || []).length);

  if (!hasContent) {
    setStep("summary", "empty");
    ui.summaryEmpty.textContent =
      llm && llm.error
        ? `No verdict: ${llm.error}`
        : "No verdict could be produced from these reviews.";
    show(ui.summaryEmpty, true);
    return;
  }

  ui.verdict.textContent = summary.verdict || "";

  const level = ["high", "medium", "low"].includes(summary.confidence) ? summary.confidence : "none";
  ui.summaryConfidence.textContent = `${level} confidence`;
  ui.summaryConfidence.className = `chip chip--${level}`;
  show(ui.summaryConfidence, true);

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
  show(ui.skelReviews, false);

  const passed = event.reviews_passed || 0;
  const total = (event.reviews || []).length;
  const evidence = selectEvidence(event.reviews);

  ui.reviewsCount.textContent = total
    ? `${passed} of ${total} passed filtering`
    : "";

  if (!evidence.length) {
    setStep("reviews", "empty");
    ui.reviewsEmpty.textContent = total
      ? "No reviews survived filtering, so none are shown as evidence."
      : "No reviews were found for this product.";
    show(ui.reviewsEmpty, true);
    return;
  }

  setStep("reviews", "done");
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
  show(ui.skelVideos, false);
  show(ui.videos, true);
  setStep("videos", "done");
  for (const video of videos) ui.videos.appendChild(videoNode(video));
  ui.videosCount.textContent = `${ui.videos.children.length} found`;
}

function finishVideos(videoSources) {
  show(ui.skelVideos, false);
  if (ui.videos.children.length) return;

  setStep("videos", "empty");
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
  const ok = event.status === "ok";
  setBadge(ok ? "done" : "thin data", ok ? "ok" : "warn");

  if (!ok && event.message) {
    ui.thinBody.textContent = event.message;
    show(ui.stateThin, true);
  }

  const parts = [];
  if ((event.contributed || []).length) parts.push(event.contributed.join(", "));
  if (event.duration_ms) parts.push(`${(event.duration_ms / 1000).toFixed(1)}s`);
  ui.footMeta.textContent = parts.join(" · ");

  finishVideos(event.video_sources);
  show(ui.skelSummary, false);
  show(ui.skelReviews, false);
}

/* ------------------------------------------------------------------ backend */

function applyEvent(event) {
  diagnostics[event.event === "videos" ? `videos:${event.source}` : event.event] = event;

  switch (event.event) {
    case "started":
      setBadge("analyzing", "pending");
      break;
    case "source":
      // Progress only — reviews themselves arrive once filtered.
      if (event.kind === "host" && event.report) {
        const report = event.report;
        ui.reviewsCount.textContent = report.blocked
          ? `${report.source} blocked the scrape`
          : `${report.count} from ${report.source}…`;
      }
      break;
    case "reviews":
      renderReviews(event);
      break;
    case "videos":
      appendVideos(event.videos);
      break;
    case "summary":
      renderSummary(event.summary, event.llm);
      break;
    case "match":
      break;
    case "done":
      renderDone(event);
      break;
    case "error":
      fail("Analysis failed", event.message || "The backend reported an error.");
      break;
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
      try {
        applyEvent(JSON.parse(line.slice(5).trim()));
        sawEvent = true;
      } catch (error) {
        console.debug("[TrustLens] unparseable event frame", error);
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
    let detail = "";
    try {
      detail = JSON.stringify((await response.json()).detail);
    } catch {
      /* no JSON body */
    }
    throw new Error(`Backend returned HTTP ${response.status}${detail ? `\n\n${detail}` : ""}`);
  }

  const data = await response.json();
  applyEvent({ event: "reviews", reviews: data.reviews, reviews_passed: data.reviews_passed,
               filter_report: data.filter_report, sources: data.sources });
  applyEvent({ event: "summary", summary: data.summary, llm: data.llm });
  applyEvent({ event: "videos", source: "all", videos: data.videos, report: {} });
  applyEvent({ event: "done", status: data.status, message: data.message,
               contributed: data.contributed, video_sources: data.video_sources,
               duration_ms: (data.llm || {}).duration_ms });
}

function explainNetwork(error) {
  if (error.name === "AbortError") {
    return [
      "Request timed out",
      `The backend did not finish within ${REQUEST_TIMEOUT_MS / 1000}s.`,
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
  return ["Analysis failed", error.message, null];
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
    hideResults();
    fail(...explainNetwork(error));
  } finally {
    clearTimeout(timer);
    ui.debugBody.textContent = JSON.stringify(diagnostics, null, 2);
  }
}

/** Parse the entry field into a URL or a plain product name. */
function parseEntry(value) {
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return { url: value };
  } catch {
    /* not a URL */
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

/* ------------------------------------------------------------------- start */

initPromo();
renderRecent();
hideResults();
show(ui.retry, false);
show(ui.debugToggle, false);

// Bootstrap from a shared link: ?url=<product url> or ?q=<product name>.
const initial = new URLSearchParams(window.location.search);
const initialUrl = initial.get("url");
const initialName = initial.get("q");
if (initialUrl || initialName) {
  ui.entryInput.value = initialUrl || initialName;
  submitEntry();
}

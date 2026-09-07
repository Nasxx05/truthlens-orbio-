/**
 * TrustLens popup.
 *
 * The extension stays thin: detect the product, call the backend, render what
 * comes back. No scraping, filtering, or LLM calls happen here, and no
 * credentials live in this file.
 *
 * Rendering is progressive. The backend streams each stage of analysis as it
 * finishes (Server-Sent Events over a POST), so reviews appear as soon as they
 * are filtered and videos as their platforms answer, instead of holding a
 * skeleton until the slowest stage — the LLM — completes. If streaming is
 * unavailable, it falls back to the single-response endpoint.
 */

const API_BASE = "http://127.0.0.1:8000";
const STREAM_URL = `${API_BASE}/analyze/stream`;
const ANALYZE_URL = `${API_BASE}/analyze`;
const DETECTOR_FILE = "content/detector.js";

const REQUEST_TIMEOUT_MS = 120000;
const REVIEWS_SHOWN = 3;        // shown initially; "show more" reveals the rest
const REVIEWS_MAX = 8;

/** Pages where no content script can run, so detection is impossible by design. */
const RESTRICTED =
  /^(chrome|edge|about|chrome-extension|moz-extension|view-source|devtools):|^https:\/\/chrome\.google\.com\/webstore/i;

const $ = (id) => document.getElementById(id);

const ui = {
  badge: $("badge"),
  productName: $("product-name"),
  productImage: $("product-image"),
  rail: $("rail"),
  main: $("main"),

  stateError: $("state-error"),
  errorTitle: $("error-title"),
  errorBody: $("error-body"),
  errorHint: $("error-hint"),
  stateThin: $("state-thin"),
  thinTitle: $("thin-title"),
  thinBody: $("thin-body"),

  manual: $("manual"),
  manualForm: $("manual-form"),
  manualInput: $("manual-input"),
  manualReason: $("manual-reason"),
  manualError: $("manual-error"),
  rescan: $("rescan"),

  secSummary: $("sec-summary"),
  skelSummary: $("skel-summary"),
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
/** Has any reviews/videos/summary event already rendered content this run? */
let hasPartialResults = false;

/* ------------------------------------------------------------------- chrome */

function setBadge(text, kind) {
  ui.badge.textContent = text;
  ui.badge.className = `badge badge--${kind}`;
}

function setStep(step, state) {
  const node = ui.rail.querySelector(`[data-step="${step}"]`);
  if (node) node.dataset.state = state;
}

function show(node, visible = true) {
  if (node) node.hidden = !visible;
}

/** Reset every section to its loading state. */
function resetView() {
  diagnostics = {};
  hiddenReviews = [];
  hasPartialResults = false;

  show(ui.rail, true);
  for (const step of ["reviews", "summary", "videos"]) setStep(step, "active");

  show(ui.stateError, false);
  show(ui.stateThin, false);
  show(ui.manual, false);

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
  show(ui.trustScore, false);
  show(ui.starRating, false);
  show(ui.productImage, false);
  if (ui.productImage) ui.productImage.src = "";
  ui.reviewsCount.textContent = "";
  ui.videosCount.textContent = "";
  ui.footMeta.textContent = "";
  show(ui.debug, false);
}

/** Hide the analysis sections entirely — for detection failures. */
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

/* ---------------------------------------------------------------- detection */

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) throw new Error("No active tab.");
  return tab;
}

/**
 * Ask the page what product it is showing.
 *
 * The detector may already be present from an earlier click, in which case the
 * message is simply answered. If not, injection happens first — cheaper than
 * injecting on every popup open, and it keeps the page's SPA-navigation
 * watcher alive across clicks.
 */
async function detectProduct(tab) {
  const ask = () => chrome.tabs.sendMessage(tab.id, { type: "TRUSTLENS_DETECT" });
  try {
    return await ask();
  } catch {
    /* no listener yet */
  }
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: [DETECTOR_FILE] });
  return await ask();
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

/**
 * Strip a URL back to scheme, host and path.
 *
 * Only ever sent as a fallback when the page published no canonical URL. A raw
 * href can carry search terms, session identifiers and referral parameters —
 * none of which the backend needs to identify a product, and all of which
 * would be personal data leaving the browser.
 */
function bareUrl(href) {
  if (!href) return href;
  try {
    const parsed = new URL(href);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return href;
  }
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
  // A verdict — with trust score and star rating — is always produced now,
  // even from video evidence or general knowledge when there are no
  // reviews, so a low review count is not "not enough data" anymore. Each
  // section (reviews/summary/videos) already renders its own final state;
  // no separate blocking banner is shown on top of it.
  setBadge("done", "ok");

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
        if (report.image_url && ui.productImage) {
          ui.productImage.src = report.image_url;
          show(ui.productImage, true);
        }
      }
      break;
    case "reviews":
      renderReviews(event);
      hasPartialResults = true;
      break;
    case "videos":
      appendVideos(event.videos);
      hasPartialResults = true;
      break;
    case "summary":
      renderSummary(event.summary, event.llm);
      hasPartialResults = true;
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

function showManual(reason) {
  if (reason) ui.manualReason.textContent = reason;
  hideResults();
  show(ui.manual, true);
  show(ui.manualError, false);
  ui.manualInput.focus();
}

async function run() {
  resetView();
  setBadge("detecting", "pending");
  ui.productName.textContent = "Detecting product…";

  let tab;
  try {
    tab = await activeTab();
  } catch (error) {
    setBadge("error", "error");
    ui.productName.textContent = "—";
    showManual("Enter a product URL or name to analyze.");
    return;
  }

  if (RESTRICTED.test(tab.url || "")) {
    setBadge("unsupported", "pending");
    ui.productName.textContent = "Browser page";
    showManual("TrustLens can't read this page. Paste a product URL or type a name.");
    return;
  }

  let product;
  try {
    const result = await detectProduct(tab);
    product = result && result.product;
  } catch (error) {
    setBadge("no access", "error");
    ui.productName.textContent = "—";
    showManual("Detection couldn't run here. Paste a product URL or type a name.");
    return;
  }

  // A page still rendering may have nothing to find yet; ask once more before
  // giving up on it.
  if ((!product || !product.detected) && (!product || product.document_state !== "complete")) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    try {
      const retry = await detectProduct(tab);
      if (retry && retry.product) product = retry.product;
    } catch {
      /* keep the first result */
    }
  }

  if (!product || !product.detected) {
    setBadge("no product", "pending");
    ui.productName.textContent = "No product detected";
    showManual();
    return;
  }

  ui.productName.textContent = product.title || product.product_id || "Detected product";
  ui.productName.title = [product.title, product.product_id, product.site].filter(Boolean).join(" · ");

  await analyze({
    product_url: product.canonical_url || bareUrl(product.page_url),
    product_name: product.title,
    product_id: product.product_id,
    canonical_url: product.canonical_url,
    detection: {
      source: "auto",
      confidence: product.confidence,
      sources: product.sources || [],
      site: product.site,
    },
  });
}

function submitManual(event) {
  event.preventDefault();
  const value = ui.manualInput.value.trim();
  if (!value) {
    ui.manualError.textContent = "Enter a product URL or name.";
    show(ui.manualError, true);
    return;
  }
  show(ui.manualError, false);

  let isUrl = false;
  try {
    const parsed = new URL(value);
    isUrl = parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    isUrl = false;
  }

  ui.productName.textContent = isUrl ? value : value;
  analyze({
    product_url: isUrl ? value : null,
    product_name: isUrl ? null : value,
    detection: { source: "manual", confidence: "none", sources: [], site: null },
  });
}

ui.manualForm.addEventListener("submit", submitManual);
ui.retry.addEventListener("click", run);
ui.rescan.addEventListener("click", run);

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

if (ui.productImage) {
  ui.productImage.addEventListener("error", () => show(ui.productImage, false));
}

run();

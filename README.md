# TrustLens

Cross-platform review and video-verdict trust assistant. A browser extension
plus backend service that helps online shoppers judge whether a product is worth
buying.

See [BRIEF.md](BRIEF.md) for the full project brief and architecture rules.

## Status

**Phase 9 — complete.** Click the toolbar icon on a product page and the popup
shows a loading skeleton immediately, then fills in progressively: the verdict
with pros and cons, supporting reviews as evidence, and clickable video reviews.
Behind it, the backend collects from four sources concurrently, filters fake
reviews, summarizes the survivors with an LLM, and caches the result so the
next lookup is instant. Scraping runs in worker processes, and the endpoint is
rate limited and SSRF-guarded.

## Layout

```
trustlens/
├── backend/               FastAPI service — see backend/README.md
├── extension/
│   ├── manifest.json      MV3, activeTab + scripting only
│   ├── popup.{html,css,js}  detection display, manual fallback, backend call
│   ├── content/
│   │   └── detector.js    product detection, injected on demand
│   └── icons/
├── webapp/                standalone single-page web app, no install needed
│   ├── index.html
│   ├── app.{css,js}
│   └── icons/
└── BRIEF.md               Project brief, architecture rules, open decisions
```

## Quick start

**1. Start the backend**

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # optional, for client-rendered reviews
cp .env.example .env                    # set ANTHROPIC_API_KEY for real verdicts
uvicorn app.main:app --reload --port 8000
```

PostgreSQL and Redis are both **optional**: without them there is no cache and
analyses run inline, and the service works either way. `curl localhost:8000/health`
reports what is actually connected. To enable them:

```bash
# cache — set DATABASE_URL in .env, the table is created on startup
# queue — set REDIS_URL in .env, then run a worker alongside the API:
python -m app.worker
```

**2. Load the extension**

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** and select the `extension/` folder

**2b. Or use the web app instead — no install required**

The extension auto-detects the product on the page you're viewing, which is
faster, but the same backend is also reachable from a plain web page for
anyone who doesn't want to install anything:

```bash
cd webapp
python3 -m http.server 5500
```

Open `http://127.0.0.1:5500`, paste a product URL or name, and submit. This is
an *alternative* entry point, not a replacement for the extension — the page
itself carries a dismissible banner recommending the extension for the faster,
no-copy/paste workflow. Must be served over `http://localhost`/`127.0.0.1`
(the backend's CORS policy allows those origins specifically); opening
`index.html` directly as a `file://` URL will not work.

**3. Try it**

Open any product page and click the TrustLens toolbar icon.

The popup shows a skeleton immediately, then fills in as results arrive:

1. **Verdict** — a short paragraph with pros, cons, a confidence chip, and
   caveats about the evidence itself
2. **Supporting reviews** — a handful of reviews that passed filtering, mixing
   positive and critical so the evidence isn't cherry-picked
3. **Video reviews** — clickable links with channel, duration and view count

Four distinct states: **loading** (skeletons + a progress rail), **success**,
**not enough data** (with an explanation of why), and **error** (with the fix —
e.g. the command to start the backend). On a page with no product, a manual
input field appears instead.

`Details` in the footer shows the raw events for debugging.

### Progressive rendering

The backend streams each stage over Server-Sent Events rather than making the
popup wait for the slowest one. Reviews are never gated on a video platform, and
summarization starts the moment reviews are filtered. On a fixture with a
deliberately slow 4-second video platform: reviews rendered at +0.18s, verdict at
+0.95s, that platform at +4.01s.

On a real Amazon product page, reviews render in **4.5-5.1s** — almost entirely
the scrape itself. If streaming is unavailable, the popup falls back to the
single-response endpoint automatically.

## Product detection

`extension/content/detector.js` is injected into the active tab on click and
tries these strategies in order, most to least trustworthy:

| Strategy | What it reads | Confidence |
|---|---|---|
| JSON-LD | `schema.org/Product` blocks, including `@graph` nesting | high |
| Microdata | `itemtype="…/Product"` with `itemprop` children | high |
| Site rules | Per-retailer DOM and URL patterns | high |
| Meta tags | OpenGraph, Twitter, `product:retailer_item_id` | high / medium |
| URL shape | Generic `/dp/`, `/itm/`, `/ip/`, `/products/` patterns | low |
| Heuristics | `<h1>`, then the tab title | low |

Results merge field by field, so a later strategy can fill a gap an earlier one
left but never overwrite it. Every strategy is isolated and wrapped: one
throwing on a hostile page cannot take the others down.

Site rules currently cover Amazon, eBay, Walmart, Best Buy, Etsy, AliExpress,
Jumia, and any Shopify storefront. Sites without a rule still work through the
structured-data and URL strategies — most e-commerce platforms publish JSON-LD.

The heuristic title is deliberately gated. A bare page title is not proof of a
product page, so it only counts when corroborated by a product id or visible buy
signals, and never when it reads as page furniture (`search`, `cart`,
`category`, …). Showing a wrong product would produce a confident, wrong verdict
later in the pipeline — worse than not detecting at all.

Detection re-runs on SPA navigation. The detector patches `pushState` /
`replaceState` and listens for `popstate` / `hashchange`, plus a
`MutationObserver` for content that renders late, so browsing from one product to
another without a reload does not leave a stale detection behind.

## Caching and scale

A completed analysis is cached in PostgreSQL for 24 hours, keyed by product, so
a second lookup skips scraping and the LLM entirely — **1511ms cold → 16ms
cached** on a fixture. Thin results (`not_enough_data`) expire after an hour
instead, so a transient failure isn't pinned to a product all day. `refresh:
true` bypasses it.

With Redis configured, scraping happens in worker processes rather than on the
request path, with per-worker concurrency limits and one scrape per product even
when several shoppers ask at once. Measured: 8 concurrent analyses against a
worker with concurrency 2 — all completed, `/health` median stayed at 91ms.

## Security and privacy

Full audit in [SECURITY.md](SECURITY.md). Highlights:

- **The extension requests `activeTab` + `scripting` and one localhost host
  permission.** No `<all_urls>`, no site list, no declared content scripts —
  nothing runs on any page until you click the icon.
- **No browsing history or personal data leaves the browser.** One destination
  (your backend), carrying only the active product's canonical URL, title and
  id. Query strings are stripped from the fallback URL, since a raw href can
  carry search terms and session ids.
- **No credentials in extension code**, verified by audit. Keys live only in
  backend environment variables and never appear in a response.
- **SSRF guarded.** `product_url` is fetched by the backend, so cloud metadata,
  loopback, private ranges and non-HTTP schemes are rejected — after DNS
  resolution, so a public hostname pointing at `127.0.0.1` is caught too.
- **Rate limited** (20/min per client by default) with a 16 KB body cap.
- **Structured JSON logs** with reviewer names, emails and long digit runs
  scrubbed in the formatter.

## Data sources

| Source | Method | Notes |
|---|---|---|
| Host site | Scraping (HTTP, or Playwright when reviews render client-side) | The site the shopper is buying from |
| Competitor site | Search → confidence-checked match → scrape | Omitted unless the match is confident |
| YouTube | Official Data API v3 | Needs `YOUTUBE_API_KEY` |
| TikTok | Scraping | Least reliable by design; fully isolated |

All four run **concurrently**, each with its own timeout. A slow or broken
source degrades its own contribution and nothing else — a competitor stalling 9s
against a 6s budget still returns the host's reviews in 6.1s. `contributed` in
the response names the sources that actually returned data.

## LLM summarization

Reviews that pass filtering are summarized into **pros, cons and a verdict** as
structured output — the schema is enforced at the API boundary, so nothing is
parsed out of prose. Reviews that failed filtering are never sent to the model.

The provider sits behind an interface (`LLMProvider`): Claude by default, OpenAI
included, and no calling code names a vendor. Switching is one environment
variable.

**Thin evidence produces a cautious verdict, guaranteed in code.** The prompt
asks for calibrated confidence, but confidence is also capped against the actual
evidence — under 12 reviews caps at `low`, a single platform caps at `medium` —
and the caveats a shopper needs (thin sample, single-platform sourcing, a high
fake-review rate, cross-platform disagreement) are appended whether or not the
model produced them. Every override is listed in `llm.adjustments`.

Cost is reported per request (`input_tokens`, `output_tokens`, `cached_tokens`)
so model choice can be measured. See
[backend/README.md](backend/README.md#llm-summarization).

## Fake-review filtering

Every review is scored on four signals before it can reach summarization:
near-duplicate phrasing across the review set, one-sided sentiment with no
concrete detail, abnormal clustering of submission dates, and a missing
verified-purchase badge (contributing only — it can never fail a review on its
own).

Reviews are **annotated, not deleted**: each carries a `filter` object with its
verdict, suspicion score, and every rule that fired with the measurement behind
it. `reviews_passed` says how many survived; later phases use only those. A
filter that silently drops data can't be debugged.

On a fixture product with 10 genuine and 8 planted fake reviews, all 10 genuine
passed and all 8 fakes failed — the templated batch scoring 1.0 on three rules
at once. See [backend/README.md](backend/README.md#fake-review-filtering).

### Cross-platform matching

The competitor site is only useful if it is showing the *same* product, so a
candidate is scored before its reviews are used, and the source is **omitted**
rather than guessed at. Conflicts reject a match outright even when titles are
near-identical:

- different capacity or size (`128GB` vs `256GB`, `45mm` vs `41mm`, `55"` vs `65"`)
- neighbouring generation (`WH-1000XM5` vs `WH-1000XM4`)
- accessories (`Carrying Case for …`, `Replacement Ear Pads`)
- different brand

Reviews stay attributed to their platform in the merged array rather than being
pooled, because divergence between platforms is itself the trust signal.

## Review scraping

Given a product, the backend collects reviews from the host site with
`text`, `rating` (normalized to 5 points), `date`, and `verified_purchase`
where the site exposes them. See
[backend/README.md](backend/README.md#scraping) for how extraction, pagination,
blocking, and robots.txt are handled.

Two behaviours worth knowing:

- **Fewer than `REVIEW_MIN` (default 5) reviews returns `status: "not_enough_data"`**
  with a `message` explaining why, rather than a thin summary that would look
  authoritative and be worthless.
- **Big retailers block server-side scraping.** A CAPTCHA interstitial is
  reported as `blocked: true` instead of being mistaken for a product with no
  reviews. Amazon's robots.txt also disallows `/product-reviews/`, so only the
  reviews on the product page are collected there (typically 8-15) while
  `RESPECT_ROBOTS` is on.

## Permissions

The extension requests the minimum it can:

| Permission | Why |
|---|---|
| `activeTab` | Read the current tab — granted only when the icon is clicked, no standing access to any site |
| `scripting` | Inject the detector into that tab on click |
| `host_permissions: http://127.0.0.1:8000/*` | Call the local backend |

There are no broad host permissions and no site is listed. The detector is
injected programmatically under `activeTab` rather than declared as a
`content_script`, which would have required either a hardcoded site list or
`<all_urls>`. Nothing runs on any page until the shopper clicks the icon.

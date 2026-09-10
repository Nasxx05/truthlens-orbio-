# TrustLens Backend

FastAPI service behind the TrustLens extension. The `/analyze` endpoint accepts
the product the extension detected (or the shopper typed), then collects from
three independent sources concurrently:

| Source | What it provides |
|---|---|
| Host site | Reviews from the site the shopper is buying from |
| YouTube | Review videos via the official Data API v3 |
| TikTok | Review videos via scraping |

Reviews are then scored by the fake-review filter, and the ones that survive are
summarized by an LLM into pros, cons and a verdict. Everything comes back tagged
with its source, its filtering verdict, and how the summary was produced.

## Requirements

- Python 3.9+

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set ANTHROPIC_API_KEY for real summaries

# Optional: browser rendering for sites that render reviews client-side.
# Without it the scraper stays on the HTTP path.
python -m playwright install chromium
```

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

And, to take scraping off the request path:

```bash
python -m app.worker
```

Both are optional-dependency tolerant: with no PostgreSQL there is no cache,
with no Redis analyses run inline, and the service works either way. `/health`
reports what is actually connected.

The server listens on `http://127.0.0.1:8000`. Interactive API docs are at
`http://127.0.0.1:8000/docs`.

## Endpoints

### `GET /health`

Liveness check. Confirm the server is up before debugging the popup.

```json
{ "status": "ok", "service": "trustlens", "version": "0.1.0" }
```

## Caching

Completed analyses are cached in PostgreSQL, keyed by product. A second lookup
of the same product costs one query instead of several seconds of scraping and
a paid LLM call.

Measured on a fixture: **1511ms cold → 16ms cached, 93× faster.**

| Aspect | Behaviour |
|---|---|
| Key | `site:product_id` if both are known, else the canonical URL, else the normalized product name |
| TTL | `CACHE_TTL_HOURS` (24h) for `ok` results |
| Thin TTL | `CACHE_TTL_THIN_HOURS` (1h) for `not_enough_data` |
| Bypass | `"refresh": true` in the request body |
| Invalidate | `POST /cache/invalidate` with the same product fields |

Keys are normalized, so `B09XS7JWHH`/`b09xs7jwhh`, `www.amazon.com`/`amazon.com`
and `Sony WH-1000XM5 `/`sony wh 1000xm5` each share one entry.

**A thin result expires sooner than a good one on purpose.** Caching
`not_enough_data` for a full day would pin a transient failure — a blocked
scrape, a rate-limited API — to a product for 24 hours.

**The cache can never break a request.** Every operation is wrapped: an
unreachable database, a missing table or a malformed row degrades to a miss and
the analysis runs live.

The schema is created on startup (`CREATE TABLE IF NOT EXISTS`), so there is no
migration step for this single table.

## Task queue

Scraping is slow and bursty. Running it inside the request that asked for it
lets a handful of shoppers saturate the process. With Redis configured, a
request enqueues a job and follows its event stream; workers do the work.

| Primitive | Used for | Why |
|---|---|---|
| `LPUSH` / `BRPOP` | The job queue | Blocking pop means an idle worker costs nothing |
| **Streams** (`XADD`/`XREAD`) | Job events | **Not pub/sub** — a subscriber that connects after publishing began would silently miss the early events, and "reviews arrived before you were listening" is indistinguishable from "there were no reviews". A stream can be read from the beginning, then followed |
| `SET NX` | In-flight deduplication | Two shoppers on the same product cause one scrape, not two; the second attaches to the first job |

`WORKER_CONCURRENCY` (default 4) is the real backpressure control: a burst
queues instead of becoming a hundred simultaneous scrapes.

Measured: 8 concurrent analyses against a worker with concurrency 2 — all 8
completed, and `/health` stayed responsive throughout (median 91ms).

Without Redis, `enqueue()` returns `None` and the endpoint runs the analysis
inline. Correct, but it does not shed load.

### `POST /analyze/stream`

Same request body as `/analyze`, but streams each stage as it finishes as
Server-Sent Events. This is what the extension popup uses: reviews render as
soon as they are filtered and videos as their platforms answer, instead of
holding a skeleton until the LLM — by far the slowest stage — completes.

Each frame is `data: {…}\n\n` with the event name inside the JSON:

| `event` | When | Carries |
|---|---|---|
| `started` | Immediately | The stages to expect |
| `source` | A review source finished | Its report (counts only — reviews follow filtering) |
| `videos` | A video platform finished | That platform's videos |
| `reviews` | Every review source reported | All reviews, annotated with filtering verdicts |
| `summary` | The LLM returned | Pros / cons / verdict |
| `done` | Everything finished | Final status, totals, timing |
| `error` | The pipeline itself failed | A message |

Stages are genuinely independent: reviews are never gated on a video platform,
and summarization begins the moment reviews are filtered, overlapping the
remaining video lookups. Measured on a fixture with a deliberately slow (4s)
video platform: reviews at +0.18s, verdict at +0.95s, that platform at +4.01s.

Both endpoints consume the same generator (`app/services/pipeline.py`), so they
cannot drift — `/analyze` simply assembles the events into one response.

```bash
curl -N -X POST http://127.0.0.1:8000/analyze/stream \
  -H 'Content-Type: application/json' \
  -d '{"product_url":"https://www.amazon.com/dp/B09XS7JWHH","product_id":"B09XS7JWHH",
       "product_name":"Sony WH-1000XM5","detection":{"site":"amazon"}}'
```

### `POST /analyze`

**Request**

```json
{
  "product_url": "https://www.amazon.com/dp/B09XS7JWHH",
  "product_name": "Sony WH-1000XM5 Wireless Headphones",
  "product_id": "B09XS7JWHH",
  "canonical_url": "https://www.amazon.com/dp/B09XS7JWHH",
  "detection": {
    "source": "auto",
    "confidence": "high",
    "sources": ["id:site-rules", "title:site-rules"],
    "site": "amazon"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `product_url` | string | see below | URL of the product page the shopper is on |
| `product_name` | string | see below | Detected by the extension, or typed by the user |
| `product_id` | string | no | Site identifier — Amazon ASIN, eBay item id, SKU |
| `canonical_url` | string | no | Canonical URL, tracking parameters stripped |
| `detection.source` | string | no | `auto` (DOM detection) or `manual` (typed) |
| `detection.confidence` | string | no | `high` / `medium` / `low` / `none` |
| `detection.sources` | string[] | no | Which strategy supplied which field |
| `detection.site` | string | no | Host site the product was detected on |

**At least one of `product_url` or `product_name` is required.** Detection can
fail on an unsupported page, leaving the shopper to type a name with no URL to
send. A request identifying no product at all is rejected with `422` rather than
being accepted and silently scraping nothing in a later phase.

**Response**

```json
{
  "status": "ok",
  "summary": { "pros": [], "cons": [], "verdict": "" },
  "reviews": [
    {
      "text": "The noise cancelling is a clear step up from the XM4.",
      "source": "amazon",
      "rating": 5.0,
      "date": "2024-09-01",
      "date_raw": "Reviewed in the United States on September 1, 2024",
      "verified_purchase": true,
      "author": "shingold",
      "title": "Good ANC, long lasting, durable.",
      "helpful_votes": 12,
      "url": null,
      "extracted_by": "dom:[data-hook=\"review\"]"
    }
  ],
  "videos": [],
  "sources": [
    {
      "source": "amazon",
      "ok": true,
      "count": 12,
      "error": null,
      "strategies": ["dom"],
      "pages_fetched": 1,
      "truncated": false,
      "blocked": false,
      "notes": ["adapter: amazon"],
      "duration_ms": 5285
    }
  ],
  "message": null
}
```

`summary` carries the real LLM output:

```json
{
  "pros": ["Battery lasts around 28-30 hours with ANC on"],
  "cons": ["Companion app crashes when changing EQ presets"],
  "verdict": "Reviewers across both platforms agree the noise cancelling delivers...",
  "confidence": "medium",
  "caveats": [
    "All reviews come from a single platform (amazon), so they share whatever moderation and incentives that platform has."
  ]
}
```

It is empty (`verdict: ""`) when summarization could not run — no provider
configured, no surviving reviews, or an API failure. `llm.error` says which.

#### `contributed`

Which sources actually returned data, so the popup can say what a verdict rests
on rather than implying full coverage.

#### Status values

| `status` | Meaning |
|---|---|
| `ok` | At least `REVIEW_MIN` reviews were found |
| `not_enough_data` | Fewer than `REVIEW_MIN` found. `reviews` still carries whatever was scraped, and `message` explains why it is thin — no verdict should be inferred from it |

#### `filter` (per review)

Every review carries its filtering verdict:

```json
{
  "passed": false,
  "suspicion_score": 1.0,
  "confidence": "high",
  "rules_triggered": ["duplicate_phrasing", "contentless_praise", "submission_burst"],
  "signals": [
    {
      "rule": "duplicate_phrasing",
      "weight": 0.408,
      "detail": "near-duplicate of 5 other review(s) (peak similarity 0.67); cluster of 6"
    }
  ]
}
```

Failing reviews are also logged at `INFO` with their score, rules and text.

#### Per-review fields

Every field except `text` and `source` can be `null`: sites expose different
subsets, and a review with only body text is still usable input.
`verified_purchase` is `null` rather than `false` when the site does not say —
absence of a badge is not proof a purchase was unverified. `rating` is
normalized to a 5-point scale whatever scale the site publishes.

`source` records which platform each review came from. Attribution is kept per
review rather than pooled, because the point of the project is comparing
platforms.

#### The `sources` array

One entry per data source, reporting independently. A source failing is
information, not an error: the endpoint returns what the others produced.
`ok` records whether the scrape *ran*, not whether it found anything — a
product that genuinely has no reviews is a successful scrape with `count: 0`,
which is a different situation from `blocked: true`.

A malformed body, or one identifying no product, returns `422` with FastAPI's
validation detail.

Incoming detections are logged at `INFO`, and the strategies behind them at
`DEBUG`, so detection can be verified end to end without a debugger:

```
analyze id=B09XS7JWHH name='Sony WH-1000XM5 Wireless Headphones' site=amazon \
  source=auto confidence=high url=https://www.amazon.com/dp/B09XS7JWHH
```

Run with `LOG_LEVEL=DEBUG uvicorn app.main:app --port 8000` to see the strategy
breakdown.

## Verify from the command line

```bash
curl http://127.0.0.1:8000/health

# a detected product
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"product_url":"https://www.amazon.com/dp/B09XS7JWHH",
       "product_name":"Sony WH-1000XM5",
       "product_id":"B09XS7JWHH",
       "detection":{"source":"auto","confidence":"high"}}'

# a manual entry with no URL
curl -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"product_name":"Fellow Stagg EKG Kettle",
       "detection":{"source":"manual"}}'
```

## LLM summarization

The reviews that **passed filtering** are sent to an LLM, which returns pros,
cons and a verdict as structured output. Reviews that failed filtering are never
sent: summarizing them would launder the fakes into the verdict, which is the
whole reason the filter exists.

### The provider interface

`app/services/llm/base.py` defines `LLMProvider`. Implementations supply
`available()` and `summarize()`; nothing else in the codebase names a vendor —
`app/services/summarize.py` and the route mention no provider at all, which is
verified by test. Switching provider is one environment variable.

| Provider | Module | Mechanism |
|---|---|---|
| `anthropic` (default) | `anthropic_provider.py` | Official SDK, `messages.parse(output_format=SummaryOutput)` |
| `openai` | `openai_provider.py` | Responses API `parse(text_format=…)`, falling back to Chat Completions `parse(response_format=…)` |

Adding a third is one module plus a `register()` call. Both built-in providers
share the same prompt (`prompt.py`), so comparing them compares the models
rather than two different prompts.

The schema is enforced **at the API boundary** by the provider's structured-output
support, so the route never parses pros and cons out of prose. Output that does
not match the schema is reported as an error, not half-interpreted.

### Model and cost

The default is `claude-opus-5`. Anthropic first-party pricing per million tokens:

| Model | Input | Output |
|---|---|---|
| `claude-opus-5` (default) | $5.00 | $25.00 |
| `claude-sonnet-5` | $2.00 | $10.00 |
| `claude-haiku-4-5` | $1.00 | $5.00 |

Summarizing reviews is a routine extraction task, so a cheaper model or a lower
`LLM_EFFORT` may well be enough — but that is a cost/quality decision for the
operator, and this code does not make it on their behalf. `llm.input_tokens`,
`output_tokens` and `cached_tokens` are reported on every response so the
decision can be measured rather than guessed.

The system prompt is identical on every request and is marked cacheable, so
repeat traffic reads it from cache instead of paying full input price. Whether it
caches depends on the model's minimum cacheable prefix, so `cached_tokens`
reports what actually happened rather than assuming.

Request size is bounded by `LLM_MAX_REVIEWS` (default 60) and
`LLM_REVIEW_CHARS`. When there are more reviews than the budget allows, they are
**sampled across platforms and rating bands** rather than taken in order — a
"most recent" scrape would otherwise send one week of opinion and a
rating-sorted one nothing but five stars, and the summary would describe the sort
order rather than the product.

### Cautious verdicts on thin evidence

The prompt characterizes the evidence — how many reviews, from how many
platforms, how much was filtered out, whether platforms disagree — and states
what each situation licenses. But a prompt is a request, not a guarantee, so
confidence is also **capped in code**:

| Evidence | Maximum confidence |
|---|---|
| Fewer than `LLM_CONFIDENT_MIN_REVIEWS` (12) reviews | `low` |
| Single platform | `medium` |
| Fewer than 35 reviews | `medium` |
| 35+ reviews across 2+ platforms | `high` |

Caveats a shopper needs are appended whether or not the model thought of them:
thin evidence, single-platform sourcing, a high proportion of reviews removed as
fake, and cross-platform rating disagreement. Every correction is listed in
`llm.adjustments`, so an overridden model is visible rather than silently
rewritten.

Measured: given 10 surviving reviews from one platform and a model claiming
`high`, the response returns `confidence: "low"` with the correction recorded and
three caveats added.

### Failure modes

Summarization degrades like any other source — the response still carries its
reviews and videos, and `llm.error` explains:

| Situation | Reported as |
|---|---|
| No API key | `ANTHROPIC_API_KEY is not set` |
| Key rejected | `ANTHROPIC_API_KEY was rejected` |
| Rate limited | `rate limited by the Anthropic API; retry after 30s` |
| Model refusal | `model declined to answer (<category>)` |
| Output not matching schema | `model output did not match the requested schema` |
| Response truncated | Summary returned, with a `notes` entry saying it may be incomplete |
| Network failure | `could not reach the Anthropic API: APIConnectionError` |

## Fake-review filtering

`app/services/nlp/` scores every review before it reaches summarization. Four
rules, each contributing a weighted signal:

| Rule | What it measures | Max weight |
|---|---|---|
| `duplicate_phrasing` | Near-duplicate clustering across the review set | 0.55 |
| `contentless_praise` | One-sided sentiment with no concrete detail | 0.52 |
| `submission_burst` | Abnormal clustering of submission dates | 0.30 |
| `unverified_purchase` | No verified-purchase badge | 0.12 |

A review fails at `FILTER_THRESHOLD` (default 0.5).

### Design constraints

**Nothing is deleted.** Every review is returned with its verdict attached and
`reviews_passed` says how many survived. A filter that silently drops data
cannot be debugged or corrected, so the decision is annotated and the caller
chooses what to use. Later phases consume only reviews where `filter.passed` is
true.

**No weak signal can decide a review's fate.** Missing verified-purchase is
explicitly contributing-only: enormous numbers of genuine reviews carry no badge
(bought elsewhere, or the site never shows one). Its weight cannot reach the
threshold, and that is enforced in code — if the only rules that fired are
contributing-only, the score is clamped below the threshold regardless of
weight arithmetic.

**Set-level signals are apportioned per review.** Belonging to a cluster of nine
near-identical reviews is strong evidence; belonging to a pair is weak, because
there are only so many ways to say a battery lasts a long time.

### Duplicate detection

Word-trigram overlap alone is too brittle: swapping two words in an eleven-word
review drops trigram Jaccard to 0.38, which is exactly the edit a
review-spinning tool makes. So similarity is the strongest of three measures —
trigram Jaccard, bigram Jaccard (×0.85), and content-token Dice (×0.75) — with
the coarser ones discounted to reflect their weaker evidence.

Measured separation on a test set: templated batches score 0.50–0.67, while the
highest similarity between any two genuine reviews is 0.125.

Reviews are grouped with union-find so a chain of similar reviews is reported as
one cluster, and candidate pairs come from an inverted bigram index rather than
comparing all pairs.

### Pacing

Two guards against false positives, because clustering has innocent causes
(a launch spike is real):

- **Imprecise dates are excluded.** "2 weeks ago" is resolved to a concrete date
  during scraping, so a page full of relative dates would collapse onto a few
  days and look exactly like a burst.
- **A minimum sample is required** (`PACING_MIN_DATED`, default 8 precise
  dates). Six reviews across two days is not a pattern.

### Specificity, not positivity

The target is not positive reviews — most genuine reviews are positive. It is
the combination generated praise produces: maximal enthusiasm carrying no
information. Specificity counts measurements with units, stated prices, time
spans, comparisons, named problems and contrast words. A genuine five-star
review says what the thing did and usually what is still slightly wrong with it.

Because a fixed phrase list will always be one synonym behind ("outstanding
quality plus rapid shipping" is the same empty review as "excellent quality and
fast shipping"), brevity plus zero concrete detail also reaches full weight,
independent of word choice.

### Why a lexicon rather than a model

The signals are structural — repetition, emptiness, timing — and a lexicon keeps
every decision inspectable: a review is flagged because of specific words and
measurements that can be printed in the report, not because a black box scored
it. It also runs inline on 200 reviews without a model download.

## Video discovery

**YouTube** uses the official Data API v3, not scraping — YouTube's robots.txt
disallows `/results`, and the API returns better metadata. Two calls are
required: `search.list` finds candidates but does not return durations, so
`videos.list` fetches `contentDetails` for them. Without that second call the
"drop anything under two minutes" rule cannot be applied, and if it fails the
response says the length filter was skipped rather than quietly returning
Shorts.

Needs `YOUTUBE_API_KEY`. Absent a key the source contributes nothing and says
so, which is reported as one source of four returning zero — not a failure.

**TikTok** is scraping, and is the least reliable component in the system:
search needs a rendered browser, requests are signed in the web client, and
`/search` is disallowed by TikTok's robots.txt — so with `RESPECT_ROBOTS=true`
it is skipped with a note rather than routed around. It is fully isolated: every
failure path returns an empty result with an explanation.

The two-minute minimum is **not** applied to TikTok. It is a short-form platform
where a 40-second review is normal, so the filter that usefully removes YouTube
Shorts would remove every TikTok result.

Both platforms are filtered for relevance (`app/services/videos/relevance.py`)
rather than trusting the platform's ranking: titles must reference the product,
and giveaways, music, ASMR, unrelated-brand reviews and "Top 10" listicles are
dropped.

## Concurrency

All sources run at once (`app/services/aggregate.py`), each with its own
timeout, gathered with exceptions captured. Two consequences:

- **A slow source cannot delay the others.** Each timeout is enforced on
  in-flight requests, not just between them, so one stalled request cannot
  overrun it.
- **A broken source cannot fail the request.** Anything that raises or times out
  is reported as a failed source alongside the results that succeeded.

## Scraping

Reviews are collected by two extraction strategies, tried in order of
trustworthiness:

1. **Structured data** — `schema.org/Review` in JSON-LD or microdata. The site
   labelled these as reviews itself, so field mapping is unambiguous.
2. **DOM heuristics** — container matching across many retailers. Needed because
   most sites publish only an aggregate rating as structured data and keep the
   individual reviews in markup.

Whichever yields more reviews wins. Interface text inside review containers
(vote counts, "Read more", Amazon's accessibility expander prompts) is stripped
before extraction, and a container whose text was *entirely* interface chrome is
dropped rather than emitted — noise that looks like data is worse than no data.

Pagination follows the site's own `rel="next"` link, or a per-site rule.
Collection stops at the first of: `REVIEW_MAX` reviews, `SCRAPE_MAX_PAGES`
pages, `SCRAPE_TOTAL_TIMEOUT` seconds, or a page that yields no new reviews.
Reviews are deduplicated on normalized body text plus author, since pagination
overlaps and the same review often appears in both structured data and the DOM.

### When a page renders reviews client-side

Plain HTTP sees an empty shell. If HTTP finds nothing and Playwright is
installed, the page is rendered in headless Chromium, "load more" controls are
clicked up to three times, and the same extractors run over the result. Absent
Playwright, the scraper stays on the HTTP path and says so in `notes`.

### Blocking and robots.txt

Large retailers serve CAPTCHA interstitials to server-side traffic — with
HTTP 200, so a block would otherwise be indistinguishable from a product with
no reviews. Known interstitials are recognized and reported as
`blocked: true`.

`robots.txt` is honoured by default (`RESPECT_ROBOTS=true`). Note that Amazon
disallows `/product-reviews/`, so review pagination there is skipped and only
the reviews on the product page itself are collected — typically 8-15.

robots.txt matching follows RFC 9309: the longest matching rule wins, with
`Allow` breaking ties, and `*`/`$` wildcards are supported. The stdlib's
`urllib.robotparser` returns the *first* match instead, which silently permits
paths that a blanket `Allow: /` precedes — so `app/services/scrapers/robots.py`
implements the documented precedence.

## Layout

```
backend/
├── app/
│   ├── main.py                  # app, middleware (rate limit, body cap, request id), /health
│   ├── config.py                # env-driven settings (caps, timeouts, politeness)
│   ├── logging_config.py        # structured JSON logs + PII scrubbing
│   ├── security.py              # SSRF guard, rate limiter
│   ├── worker.py                # background job worker (`python -m app.worker`)
│   ├── db/
│   │   └── cache.py             # PostgreSQL analysis cache
│   ├── queue/
│   │   ├── broker.py            # Redis connection, optional
│   │   └── jobs.py              # enqueue / follow / dedupe
│   ├── routes/
│   │   └── analyze.py           # POST /analyze and /analyze/stream
│   ├── models/
│   │   └── schemas.py           # request/response contract (pydantic)
│   └── services/
│       ├── pipeline.py           # collect -> filter -> summarize, as an event stream
│       ├── aggregate.py          # runs every source concurrently, merges results
│       ├── matching.py           # product match confidence
│       ├── summarize.py          # evidence assessment + confidence enforcement
│       ├── llm/                  # summarization providers
│       │   ├── base.py           #   LLMProvider interface + SummaryOutput schema
│       │   ├── prompt.py         #   the prompt, shared by every provider
│       │   ├── anthropic_provider.py
│       │   └── openai_provider.py
│       ├── nlp/                  # fake-review filtering
│       │   ├── lexicon.py        #   sentiment, stock praise, detail markers
│       │   ├── text.py           #   tokenization, shingling, similarity
│       │   ├── duplication.py    #   near-duplicate clustering
│       │   ├── sentiment.py      #   polarity and specificity
│       │   ├── pacing.py         #   submission-date bursts
│       │   └── filter.py         #   combines into verdicts + report
│       ├── scrapers/
│       │   ├── base.py           # normalized Review / ScrapeResult
│       │   ├── fetch.py          # HTTP, retries, robots, block detection
│       │   ├── robots.py         # RFC 9309 robots.txt matching
│       │   ├── extract.py        # HTML -> reviews
│       │   ├── browser.py        # optional Playwright rendering
│       │   ├── host.py           # host-site orchestration
│       │   └── sites/            # per-retailer adapters
│       │       ├── amazon.py     #   host
│       │       ├── generic.py    #   host fallback
│       │       ├── newegg.py     #   host
│       │       ├── bestbuy.py    #   host
│       │       └── ebay.py       #   host
│       └── videos/
│           ├── base.py           # normalized Video / VideoResult
│           ├── relevance.py      # is this video about this product?
│           ├── youtube.py        # official Data API v3
│           └── tiktok.py         # scraping, fully isolated
├── requirements.txt
├── .env.example                 # all credentials live here, backend-side only
└── README.md
```

Each data source is independent, and each retailer adapter knows only its own
site — one site changing its markup cannot break the others.

## Security

Full audit in [`../SECURITY.md`](../SECURITY.md). In short:

- **SSRF guard.** `product_url` is fetched by this backend, so `app/security.py`
  validates it first — cloud metadata, loopback, private ranges, link-local,
  IPv4-mapped IPv6, local hostnames and non-HTTP schemes are all rejected.
  Hostnames are **resolved** and every address checked, so a public name
  pointing at `127.0.0.1` is caught. `ALLOW_PRIVATE_TARGETS` defaults to false.
- **Rate limiting.** `RATE_LIMIT` requests per `RATE_LIMIT_WINDOW` per client,
  Redis-shared where available, with `Retry-After` and `X-RateLimit-*` on the
  `429`. `/health` is exempt.
- **Body cap.** 16 KB, rejected with `413` before any handler runs.
- **Credentials are environment-only** and never appear in a response.

## Logging

`LOG_FORMAT=json` emits one JSON object per record with a stable `event_type`,
so scrape blocks, API failures and LLM errors can be counted:

```json
{"ts":"…","level":"WARNING","logger":"app.services.scrapers.host",
 "msg":"host scrape blocked","event_type":"scrape_blocked","source":"amazon","role":"host"}
```

**Personal data is scrubbed in the formatter rather than at call sites** —
relying on every future log call to remember would guarantee a leak eventually.
Reviewer names, emails and long digit runs are redacted; review text (public) is
kept in excerpt for debugging the filter.

## Notes

- **Credentials never leave the backend.** `.env` is gitignored; the extension
  contains no keys and never will.
- **CORS** allows `chrome-extension://` and `moz-extension://` origins so the
  popup can call the API during local development.
- **Every external call degrades gracefully.** Scrapers never raise: a failure
  comes back as a `ScrapeResult` describing what happened, so `/analyze` returns
  partial results rather than an error.

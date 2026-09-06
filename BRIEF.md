# TrustLens — Project Brief

Cross-platform review & video-verdict trust assistant. A browser extension plus
backend service that helps online shoppers judge whether a product is worth buying.

## User flow

The shopper clicks the extension icon while on a product page. The system then:

1. Detects the product (or accepts manual input if detection fails)
2. Scrapes text reviews from the host site and from a competitor site carrying the
   same product, to check consistency across platforms
3. Fetches review videos from YouTube (official API) and TikTok (scraping / unofficial API)
4. Filters out likely bot-generated or fake text reviews using an NLP engine
5. Sends surviving reviews to an LLM to generate a Pros / Cons / Final Verdict summary
6. Renders the summary, top reviews, and video links in the extension popup

## Host site vs. competitor site

These two terms are used throughout and mean specific things:

- **Host site** — the site the shopper is currently on, i.e. the one they intend to
  buy the product from. The extension runs against this page.
- **Competitor site** — a *different* platform that carries the **same product**.
  It is not a rival to promote or compare prices against.

**Why both:** the point is cross-platform review consistency. Reviews for one
product on a single platform can be gamed. Pulling reviews for the same product
from a second, independent platform gives a comparison baseline. Agreement between
platforms raises confidence in the reviews; a significant divergence — glowing on
the host site, mediocre elsewhere — is itself a trust signal that the host site's
reviews may be manipulated, and should be surfaced to the shopper rather than
averaged away.

This implies two things for later phases: reviews must stay **attributed to their
source platform** rather than pooled into one anonymous list, and matching the
competitor listing to the *same* product (not a similar one) is a correctness
concern, since a mismatched product makes the whole comparison meaningless.

## Architecture

```
Extension (thin)              Backend (FastAPI)
──────────────────            ─────────────────────────────────────
detect product        ──►     /analyze
render JSON           ◄──         ├─ scrapers/  host site, competitor site
                                  ├─ video/     youtube (Data API v3), tiktok
                                  ├─ nlp/       bot / fake-review filter
                                  ├─ llm/       provider interface + impls
                                  └─ db/        postgres product cache
```

## Tech stack

Do not deviate without asking.

| Layer | Choice |
|---|---|
| Extension | HTML, CSS, vanilla JavaScript, Manifest V3 |
| Backend | Python, FastAPI |
| Scraping | BeautifulSoup (static pages), Playwright (JS-heavy pages) |
| Database | PostgreSQL — caches previously searched products |
| Task queue | Redis or RabbitMQ (later phase) |
| LLM | OpenAI or Claude API, behind a swappable provider interface |
| Video sources | YouTube Data API v3 (official), TikTok (scraping / unofficial API) |

## Architecture rules

- **Thin extension.** It only detects the product, calls the backend, and renders
  the returned JSON. No scraping, filtering, or LLM calls in the browser.
- **Secrets are backend-only.** All API keys and credentials live in backend
  environment variables. Never in extension code.
- **Scoped permissions.** `manifest.json` permissions are scoped to only what the
  current phase needs. No broad host permissions.
- **Source isolation.** Each data source (host site, competitor site, YouTube,
  TikTok) is its own module or service, so one breaking does not break the others.
- **Graceful degradation.** Every external call — scrape, API call, LLM call — has
  error handling that returns partial results rather than failing the whole request.

## Open decisions

### Target e-commerce domains

The goal is to work on **any** e-commerce site rather than a fixed list. That is in
tension with the "no broad host permissions" rule, so it is resolved in the
extension as:

- **`activeTab`** — grants access to the current tab only on user click of the
  extension icon. No host list, no standing access.
- **`optional_host_permissions`** — requested at runtime, per domain, if a phase
  ever needs page access without a click.
- The **competitor-site scrape runs in the backend**, so it requires no browser
  permission at all.

Net effect: universal site coverage with zero standing host permissions. If a
phase specifies particular domains instead, the phase wins.

### LLM provider

Undecided. The selection criteria are **lowest cost and best efficiency** for the
summarization workload. The provider interface (e.g. `LLMProvider.summarize()`)
keeps this swappable, so the decision can be deferred to the phase that implements
the LLM call and revisited later without touching callers.

## How we work through this project

Build **only the current phase**. At the end of each phase:

1. Summarize what was built and where the files live
2. List how to manually test / verify it works
3. Stop and wait for "continue" before starting the next phase

Do not skip ahead or build later-phase features early, even if convenient. If a
phase requires deviating from the stack, ask first.

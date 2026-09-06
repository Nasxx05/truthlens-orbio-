# TrustLens — Security & Privacy Audit

Audit of the extension and backend, with the state of each item and where it is
enforced. Dated against the Phase 9 review.

## 1. Extension permissions

`extension/manifest.json` requests:

| Grant | Why | Scope |
|---|---|---|
| `activeTab` | Read the current tab's URL and inject the detector | The one tab, only on user click of the toolbar icon |
| `scripting` | Inject `content/detector.js` on demand | Paired with `activeTab`, so it cannot reach other tabs |
| `host_permissions: http://127.0.0.1:8000/*` | Call the local backend | The backend only |

**No broad host access.** There is no `<all_urls>`, no `*://*/*`, and no site is
listed. Verified: `host_permissions` contains exactly one entry, the backend.

**No declared `content_scripts`.** The detector is injected programmatically
under `activeTab` instead. A declarative content script would have required
either a hardcoded site list or `<all_urls>`; this way **nothing runs on any
page until the shopper clicks the icon**, and coverage is still universal.

Also absent, deliberately: `web_accessible_resources` (nothing the page can
reach), `externally_connectable` (no other extension or site can message it),
`tabs` (the URL comes from `activeTab`, so the broader permission that would
expose every tab's URL is unnecessary), `storage`, `history`, `cookies`,
`webRequest`.

## 2. What leaves the browser

Exactly one destination — the configured backend — reached by two `fetch` calls
in `popup.js`. There is no analytics, no telemetry, no third-party request, no
beacon, and no image pixel. Verified by grepping the extension for every
outbound mechanism.

The request body carries only the active product:

| Field | Value |
|---|---|
| `product_url` | The product page's **canonical** URL, tracking parameters stripped |
| `product_name` | The detected product title |
| `product_id` | The site's product identifier (ASIN, SKU) |
| `canonical_url` | As above |
| `detection` | How the product was identified: strategy names, confidence, site |

**No browsing history.** The extension has no `history` permission and reads
only the tab the shopper explicitly clicked on. Nothing is retained between
clicks; there is no storage of any kind.

**Query strings are stripped.** Where a page publishes no canonical URL, the
fallback is reduced to scheme + host + path before being sent. A raw href can
carry search terms (`?keywords=gift+for+wife`), session identifiers and referral
codes — none of which identify a product. Enforced by `bareUrl()` in
`popup.js`.

## 3. Credentials

**Every credential is backend-only, read from environment variables.** The
extension contains no key, token, or secret of any kind, and none is present in
any request it makes — verified by grepping `extension/` for key patterns,
provider names and `sk-`/`AIza` prefixes.

Keys live only in `backend/.env`, which is gitignored. `backend/.env.example`
documents the names with empty values. Providers read their keys at call time
from the environment (`app/services/llm/*_provider.py`), so no key is ever
serialized into a response — `llm` in the response reports the provider name,
model and token counts, never the credential.

## 4. Server-side request forgery

`/analyze` accepts a URL that the backend then fetches, which without checks
would make this service a proxy into its own network. `app/security.py`
validates every target **before** it reaches a scraper, the queue, or the cache:

| Blocked | Example |
|---|---|
| Cloud metadata endpoints | `169.254.169.254`, `metadata.google.internal` |
| Loopback | `127.0.0.1`, `::1` |
| Private ranges | `10/8`, `172.16/12`, `192.168/16` |
| Link-local, reserved, multicast, unspecified | `169.254/16`, `0.0.0.0` |
| IPv4-mapped IPv6 | `::ffff:10.0.0.1` |
| Local hostnames | `localhost`, `*.local`, single-label names |
| Non-HTTP schemes | `file://`, `gopher://`, `data:` |

**Hostnames are resolved and every returned address is checked**, so
`evil.example.com` pointing at `127.0.0.1` is rejected — a string-only check
would pass it.

`ALLOW_PRIVATE_TARGETS` bypasses this for local fixtures. It defaults to
**false** in code; `.env.example` sets it true for development and says not to.
When enabled, the service logs a warning at startup.

## 5. Abuse protection

| Control | Default | Where |
|---|---|---|
| Rate limit | 20 requests / 60s per client | `app/main.py` middleware → `app/security.py` |
| Body size cap | 16 KB, rejected with `413` | `app/main.py` middleware |
| Per-source scrape budgets | Page, review and wall-clock caps | `app/config.py` |
| Worker concurrency | 4 analyses per worker | `app/worker.py` |
| In-flight deduplication | One scrape per product at a time | `app/queue/jobs.py` |

Rate limiting is shared across processes when Redis is available and falls back
to per-process counters otherwise. `429` responses carry `Retry-After` and
`X-RateLimit-*`. `/health` is deliberately exempt so monitoring always works.

`X-Forwarded-For` is honoured **only** when `TRUST_PROXY_HEADERS=true`.
Trusting it unconditionally would let any caller forge an identity and bypass
the limit entirely.

## 6. Logging

Structured JSON (`LOG_FORMAT=json`) with stable `event_type` values, so
failures can be counted and alerted on:

`scrape_blocked`, `scrape_error`, `video_api_error`, `video_source_error`,
`llm_error`, `review_filtered`, `cache_hit`, `target_rejected`, `rate_limited`,
`body_too_large`, `request`, `request_error`, `startup`, `insecure_config`

**Personal data is scrubbed in the formatter, not at call sites.** Relying on
every future log call to remember would guarantee a leak eventually.
`app/logging_config.py` redacts `author`/`reviewer`/`email`/`user` fields
recursively, replaces email addresses and long digit runs in free text, caps
strings at 200 characters, and bounds list fan-out.

Review **text** is public and is logged in excerpt for debugging the filter.
Reviewer **names** are not: they identify a person and this service has no
reason to record who wrote what. Tracebacks are reduced to error type and
message, since frames can carry scraped content.

## Known limitations

- **No authentication.** The backend is unauthenticated and intended to run on
  localhost. Exposing it publicly needs auth in front of it; rate limiting alone
  is not access control.
- **CORS allows any extension origin** (`chrome-extension://*`), because an
  extension's ID is not known ahead of time. Pin it to your published ID before
  wider distribution.
- **Scraped HTML is untrusted input.** It is parsed, never executed, and review
  text reaches the LLM as data — but a sufficiently adversarial retailer could
  attempt prompt injection through review text. Not currently mitigated.
- **Rate limiting is per client identity**, which is per IP. Shared NATs share a
  budget; distributed callers each get their own.

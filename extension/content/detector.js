/**
 * TrustLens product detector — injected on demand into the active tab.
 *
 * Injected by the popup via chrome.scripting.executeScript under activeTab, so
 * nothing runs on any page until the shopper clicks the icon.
 *
 * Extraction runs a chain of strategies from most to least trustworthy:
 *
 *   1. JSON-LD   schema.org/Product — structured, site-authored, most reliable
 *   2. Microdata  itemtype="...Product" with itemprop children
 *   3. Meta tags  OpenGraph / Twitter / product:retailer_item_id
 *   4. Site rules per-retailer DOM and URL patterns (ASIN, item ids, ...)
 *   5. URL shape  generic /dp/, /itm/, /ip/, /products/ patterns
 *   6. Heuristics <h1> plus document.title, as a last resort
 *
 * Every strategy is isolated in its own function and wrapped, so one throwing
 * on a hostile page cannot take the others down — a partial detection is worth
 * more than an exception. Results merge field by field: a later strategy can
 * fill a gap the earlier one left, but never overwrite what it found.
 */

(() => {
  // executeScript can land more than once on the same page. Re-running the
  // whole IIFE would stack duplicate listeners and observers, so bail early
  // and let the existing instance answer.
  if (window.__trustLensDetector) {
    window.__trustLensDetector.reinjected += 1;
    return;
  }

  const MAX_TITLE = 300;

  /* ------------------------------------------------------------------ utils */

  const clean = (value) => {
    if (typeof value !== "string") return null;
    // Collapse the whitespace that DOM text nodes are full of.
    const text = value.replace(/\s+/g, " ").trim();
    return text ? text.slice(0, MAX_TITLE) : null;
  };

  const attr = (selector, name) => {
    const node = document.querySelector(selector);
    return node ? clean(node.getAttribute(name)) : null;
  };

  const text = (selector) => {
    const node = document.querySelector(selector);
    return node ? clean(node.textContent) : null;
  };

  /** Run a strategy without letting its failure escape. */
  const attempt = (name, fn) => {
    try {
      const result = fn();
      return result && (result.product_id || result.title) ? { ...result, via: name } : null;
    } catch (error) {
      console.debug(`[TrustLens] strategy "${name}" failed:`, error);
      return null;
    }
  };

  /* -------------------------------------------------------------- strategies */

  /** schema.org Product in a <script type="application/ld+json"> block. */
  function fromJsonLd() {
    const blocks = document.querySelectorAll('script[type="application/ld+json"]');

    // JSON-LD nests: @graph arrays, bare arrays, Product inside anything.
    const flatten = (node, depth = 0) => {
      if (!node || depth > 6) return [];
      if (Array.isArray(node)) return node.flatMap((n) => flatten(n, depth + 1));
      if (typeof node !== "object") return [];
      const nested = [node["@graph"], node.mainEntity, node.item].flatMap((n) =>
        flatten(n, depth + 1)
      );
      return [node, ...nested];
    };

    const isProduct = (node) => {
      const type = node && node["@type"];
      const types = Array.isArray(type) ? type : [type];
      return types.some((t) => typeof t === "string" && /product/i.test(t));
    };

    for (const block of blocks) {
      let parsed;
      try {
        parsed = JSON.parse(block.textContent);
      } catch {
        continue; // one malformed block should not stop the rest
      }

      const product = flatten(parsed).find(isProduct);
      if (!product) continue;

      const id = product.sku || product.mpn || product.productID || product.gtin13 || product.gtin;
      return {
        product_id: clean(typeof id === "object" ? null : String(id ?? "")),
        title: clean(product.name),
        canonical_url: clean(product.url),
        confidence: "high",
      };
    }
    return null;
  }

  /** Microdata: itemscope + itemtype ending in Product. */
  function fromMicrodata() {
    const scope = document.querySelector('[itemtype*="schema.org/Product" i]');
    if (!scope) return null;

    const prop = (name) => {
      const node = scope.querySelector(`[itemprop="${name}"]`);
      if (!node) return null;
      return clean(node.getAttribute("content") || node.getAttribute("href") || node.textContent);
    };

    return {
      product_id: prop("sku") || prop("mpn") || prop("productID"),
      title: prop("name"),
      canonical_url: prop("url"),
      confidence: "high",
    };
  }

  /** OpenGraph and friends. Widely present, occasionally the page-level title. */
  function fromMeta() {
    const meta = (name) =>
      attr(`meta[property="${name}" i]`, "content") || attr(`meta[name="${name}" i]`, "content");

    const type = meta("og:type");
    const title = meta("og:title") || meta("twitter:title");
    const id =
      meta("product:retailer_item_id") ||
      meta("product:sku") ||
      meta("product:item_group_id") ||
      attr("meta[itemprop='sku' i]", "content");

    if (!title && !id) return null;

    return {
      product_id: id,
      title,
      canonical_url: meta("og:url"),
      // og:type=product is a real signal this is a product page, not a listing.
      confidence: /product/i.test(type || "") ? "high" : "medium",
    };
  }

  /**
   * Per-retailer rules. Each entry is independent: `test` decides whether the
   * host applies, `run` pulls what that site exposes. Adding a retailer means
   * adding an entry, never touching the others.
   */
  const SITE_RULES = [
    {
      name: "amazon",
      test: (host) => /(^|\.)amazon\./i.test(host),
      run: () => ({
        // Amazon exposes the ASIN in several places; the URL is the steadiest.
        product_id:
          (location.pathname.match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([A-Z0-9]{10})/i) || [])[1] ||
          attr("input#ASIN", "value") ||
          attr("[data-asin]:not([data-asin=''])", "data-asin"),
        title: text("#productTitle") || text("#title"),
        confidence: "high",
      }),
    },
    {
      name: "ebay",
      test: (host) => /(^|\.)ebay\./i.test(host),
      run: () => ({
        product_id: (location.pathname.match(/\/itm\/(?:.*\/)?(\d{9,})/) || [])[1],
        title: text("h1 .ux-textspans--BOLD") || text("#itemTitle") || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "walmart",
      test: (host) => /(^|\.)walmart\./i.test(host),
      run: () => ({
        product_id: (location.pathname.match(/\/ip\/(?:[^/]+\/)?(\d+)/) || [])[1],
        title: text('h1[itemprop="name"]') || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "bestbuy",
      test: (host) => /(^|\.)bestbuy\./i.test(host),
      run: () => ({
        product_id: (location.pathname.match(/\/(\d{6,})\.p/) || [])[1],
        title: text("h1.heading-5") || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "etsy",
      test: (host) => /(^|\.)etsy\./i.test(host),
      run: () => ({
        product_id: (location.pathname.match(/\/listing\/(\d+)/) || [])[1],
        title: text("h1[data-buy-box-listing-title]") || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "aliexpress",
      test: (host) => /(^|\.)aliexpress\./i.test(host),
      run: () => ({
        product_id: (location.pathname.match(/\/item\/(?:.*?)(\d{6,})\.html/) || [])[1],
        title: text("h1[data-pl]") || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "jumia",
      test: (host) => /(^|\.)jumia\./i.test(host),
      run: () => ({
        // Jumia hangs the SKU off the product container and the URL slug.
        product_id:
          attr("[data-sku]", "data-sku") ||
          (location.pathname.match(/-(\d+)\.html/) || [])[1],
        title: text("h1.-fs20") || text("h1"),
        confidence: "high",
      }),
    },
    {
      name: "shopify",
      // Any Shopify storefront, whatever the domain.
      test: () => Boolean(
        document.querySelector('script[src*="cdn.shopify.com"], #shopify-features')
      ),
      run: () => ({
        product_id: (location.pathname.match(/\/products\/([\w-]+)/) || [])[1],
        title: text("h1.product__title") || text("h1"),
        confidence: "medium",
      }),
    },
  ];

  function fromSiteRules() {
    const host = location.hostname;
    for (const rule of SITE_RULES) {
      let applies = false;
      try {
        applies = rule.test(host);
      } catch {
        continue;
      }
      if (!applies) continue;

      const found = attempt(`site:${rule.name}`, rule.run);
      if (found) return { ...found, site: rule.name };
    }
    return null;
  }

  /** Generic product-page URL shapes, for retailers with no rule of their own. */
  function fromUrlShape() {
    const patterns = [
      /\/(?:dp|itm|ip|pd|prd|product|products|listing|item)\/(?:[^/?#]+\/)?([A-Za-z0-9][\w-]{3,})/i,
      /[?&](?:product_?id|sku|pid|itemid)=([\w-]+)/i,
    ];

    for (const pattern of patterns) {
      const match = (location.pathname + location.search).match(pattern);
      if (match) {
        return { product_id: decodeURIComponent(match[1]), confidence: "low" };
      }
    }
    return null;
  }

  /**
   * Titles that are page furniture rather than products. Without this gate the
   * heuristic strategy happily reports "Search results" as a product, and the
   * popup shows a confident-looking detection on a category or cart page
   * instead of offering the manual input field.
   */
  const NOT_A_PRODUCT =
    /\b(search|results?|categor(y|ies)|collections?|catalog(ue)?|browse|deals?|shop all|best ?sellers?|new arrivals|cart|basket|checkout|wish ?list|sign ?in|log ?in|register|account|orders?|returns?|home ?page|404|not found|privacy|terms|contact|about us|blog|help|support)\b/i;

  /**
   * Does the page carry commerce affordances? A price next to a buy control is
   * decent evidence of a product page even when the markup says nothing.
   */
  function hasBuySignals() {
    const buy = document.querySelector(
      '[id*="add-to-cart" i], [class*="add-to-cart" i], [data-testid*="add-to-cart" i],' +
        '[name="add" i], button[type="submit"][value*="cart" i]'
    );
    // innerText is not available in every context; textContent always is.
    const body = document.body;
    const visible = body ? (body.innerText || body.textContent || "").slice(0, 4000) : "";
    const priced = /(?:[$£€¥₦₹]|USD|EUR|GBP|NGN)\s?\d/.test(visible);
    return Boolean(buy) && priced;
  }

  /**
   * Last resort: the visible <h1>, else the tab title with the site name
   * trimmed off. Deliberately conservative — a title alone is not proof of a
   * product page, so it only counts when the page also looks like one. Getting
   * this wrong is worse than not detecting: a wrong product produces a
   * confident, wrong verdict later in the pipeline.
   */
  function fromHeuristics(corroborated) {
    const heading = text("h1");
    const title = clean(document.title);
    const trimmed = title ? title.split(/\s+[|–—·:]\s+/)[0] || title : null;

    const candidate = heading || trimmed;
    if (!candidate || NOT_A_PRODUCT.test(candidate)) return null;

    // A product page names the product in a heading. A title-only candidate
    // needs corroboration: either another strategy already found an id, or the
    // page shows buy signals. Otherwise it is probably page furniture.
    if (!heading && !corroborated && !hasBuySignals()) return null;

    return { title: candidate, confidence: "low" };
  }

  /* ---------------------------------------------------------------- canonical */

  function canonicalUrl() {
    const declared =
      attr('link[rel="canonical" i]', "href") || attr('meta[property="og:url" i]', "content");

    const candidate = declared || location.href;
    try {
      // Resolve relative canonicals and drop the tracking cruft, which would
      // otherwise fragment the backend's product cache in a later phase.
      const url = new URL(candidate, location.href);
      url.hash = "";
      for (const key of [...url.searchParams.keys()]) {
        if (/^(utm_|gclid|fbclid|ref_?|_encoding|psc|th|tag|linkCode)/i.test(key)) {
          url.searchParams.delete(key);
        }
      }
      return url.href;
    } catch {
      return location.href;
    }
  }

  /* ----------------------------------------------------------------- detect */

  const RANK = { high: 3, medium: 2, low: 1 };

  /**
   * Run every strategy and merge. First non-empty value for a field wins, so
   * ordering encodes trust. Confidence reflects the strategy that supplied the
   * id, downgraded when no id was found at all.
   */
  function detect() {
    const structured = [
      attempt("json-ld", fromJsonLd),
      attempt("microdata", fromMicrodata),
      attempt("site-rules", fromSiteRules),
      attempt("meta", fromMeta),
      attempt("url-shape", fromUrlShape),
    ].filter(Boolean);

    // An id from any strategy above is evidence this is a product page, which
    // lets the heuristic title relax its own gate.
    const corroborated = structured.some((r) => r.product_id);

    const results = [
      ...structured,
      attempt("heuristics", () => fromHeuristics(corroborated)),
    ].filter(Boolean);

    const merged = {
      product_id: null,
      title: null,
      canonical_url: null,
      site: null,
      confidence: "none",
      sources: [],
      strategies_tried: results.map((r) => r.via),
    };

    for (const result of results) {
      if (!merged.product_id && result.product_id) {
        merged.product_id = result.product_id;
        merged.sources.push(`id:${result.via}`);
        if (RANK[result.confidence] > RANK[merged.confidence] || merged.confidence === "none") {
          merged.confidence = result.confidence;
        }
      }
      if (!merged.title && result.title) {
        merged.title = result.title;
        merged.sources.push(`title:${result.via}`);
      }
      if (!merged.canonical_url && result.canonical_url) merged.canonical_url = result.canonical_url;
      if (!merged.site && result.site) merged.site = result.site;
    }

    merged.canonical_url = merged.canonical_url || canonicalUrl();
    merged.site = merged.site || location.hostname;

    // A title with no id is a weak detection: it is often the page banner on a
    // category or search page. Say so rather than overstating it.
    if (!merged.product_id) merged.confidence = merged.title ? "low" : "none";

    return {
      ...merged,
      // The popup treats this as the switch between showing the product and
      // showing the manual input field.
      detected: Boolean(merged.product_id || merged.title),
      page_url: location.href,
      detected_at: Date.now(),
      document_state: document.readyState,
    };
  }

  /* ------------------------------------------------- SPA navigation watching */

  const state = {
    reinjected: 0,
    last: null,
    url: location.href,
    version: 0,
  };

  const refresh = (reason) => {
    state.last = detect();
    state.version += 1;
    console.debug(`[TrustLens] detected (${reason}):`, state.last);
  };

  /**
   * Product sites navigate without reloading, which would otherwise leave a
   * stale detection behind. Watch the three ways the URL can change under an
   * SPA, plus DOM churn for the case where the URL is already right but the
   * content is still rendering.
   */
  function watchNavigation() {
    const onUrlChange = () => {
      if (location.href === state.url) return;
      state.url = location.href;
      state.last = null; // invalidate immediately; re-detect after render
      clearTimeout(watchNavigation.timer);
      watchNavigation.timer = setTimeout(() => refresh("navigation"), 400);
    };

    for (const method of ["pushState", "replaceState"]) {
      const original = history[method];
      history[method] = function patched(...args) {
        const result = original.apply(this, args);
        onUrlChange();
        return result;
      };
    }

    window.addEventListener("popstate", onUrlChange);
    window.addEventListener("hashchange", onUrlChange);

    // Catches late-rendered titles on the current URL.
    const observer = new MutationObserver(() => {
      if (location.href !== state.url) return onUrlChange();
      if (state.last && !state.last.product_id) {
        clearTimeout(observer.timer);
        observer.timer = setTimeout(() => refresh("dom-change"), 600);
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  /* ---------------------------------------------------------------- messaging */

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.type !== "TRUSTLENS_DETECT") return false;

    // Always re-detect on request. Cheap, and it guarantees the popup never
    // renders a product the shopper has already navigated away from.
    refresh("request");
    sendResponse({ ok: true, product: state.last, version: state.version });
    return true;
  });

  window.__trustLensDetector = state;
  refresh("inject");
  watchNavigation();
})();

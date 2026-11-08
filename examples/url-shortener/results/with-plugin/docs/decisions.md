# Decisions

## D-001: In-process fixed-window limiter over Flask-Limiter or Redis for POST /links
- **Status:** Active
- **Origin:** new (rate limiting requested by user; mechanism chosen by agent)
- **Where:** app.py (`FixedWindowLimiter`, `create_link`), test_app.py
- **Context:** One Flask process on one small VM, one maintainer, SQLite storage. Goal is to stop a single client flooding the links table. Default 10 POSTs per 60 s per IP, set by `SHORTENER_RATE_LIMIT` / `SHORTENER_RATE_WINDOW`.
- **Options:**
  - Redis-backed limiter: shared across processes, survives restarts. A second service to install, secure and monitor; if it is down, link creation either fails or goes unlimited.
  - Flask-Limiter (memory storage): mature, many strategies. A new dependency to track and upgrade for about 25 lines of logic.
  - Sliding-window log per IP: no boundary bursts. Needs per-key cleanup to keep memory bounded.
  - Fixed window, all keys cleared at each window boundary: ~25 lines, no dependency, memory bounded by IPs seen in one window. Allows up to 2x the limit across a boundary.
- **Choice:** Fixed window in process memory, guarded by a lock because the dev server is threaded. Invalid requests count too.
- **Trade-off:** Counts reset on restart, and a client can get up to 20 creates in a short burst straddling a boundary. Accepted, since it still caps sustained abuse at the configured rate.
- **Revisit when:** The app runs more than one worker process or host (counts would split per process), or boundary bursts show up as real abuse.

## D-002: Key the limit on `request.remote_addr` over trusting `X-Forwarded-For`
- **Status:** Active
- **Origin:** new
- **Where:** app.py (`create_link`), README.md (Rate limiting notes)
- **Context:** README describes `python app.py` serving directly on :5000; no reverse proxy is configured in the repo.
- **Options:**
  - Read `X-Forwarded-For` directly: works behind a proxy. Without a proxy any client can spoof the header and get a fresh limit per request, which disables the limiter.
  - `ProxyFix` middleware now: correct behind exactly one proxy. Same spoofing hole if there is no proxy.
  - `remote_addr`: cannot be spoofed over TCP. Behind a proxy, every user shares one bucket.
- **Choice:** `remote_addr`, with the ProxyFix change documented in the README.
- **Trade-off:** If a proxy is added and ProxyFix is forgotten, all users share 10 creates/min and legitimate users get 429s.
- **Revisit when:** A reverse proxy, load balancer or CDN is placed in front of the app.

## D-003: Counter column on `links` over a per-click events table
- **Status:** Active
- **Origin:** new (click counting requested by user; storage chosen by agent)
- **Where:** app.py (`init_db`, `follow`, `link_info`), test_app.py, README.md (Click counting notes)
- **Context:** User asked for a click count per link in `GET /links/<code>`. One SQLite file, one process, one maintainer. No request for timestamps, referrers or charts.
- **Options:**
  - `clicks` events table (one row per redirect): allows per-day stats later. Grows without bound since redirects are unauthenticated and not rate limited, needs pruning or the VM disk fills, and every info request runs a `COUNT(*)`.
  - In-memory counter: no writes on the redirect path. Lost on every restart, like the rate limiter's counts.
  - `clicks INTEGER NOT NULL DEFAULT 0` on `links`, incremented with `clicks = clicks + 1` in SQL: constant size, survives restarts, atomic across threads.
- **Choice:** Counter column. Old databases get it via an idempotent `ALTER TABLE` in `init_db`, after a `PRAGMA table_info` check, instead of a migration tool.
- **Trade-off:** No history: clicks-per-day or referrers can't be recovered later. Bots, previews and HEAD requests all count. Each redirect now does one SQLite write and commit.
- **Revisit when:** The user wants time-based or per-source stats, or redirect traffic is high enough for commit latency or `database is locked` errors to show up in the logs.

## D-004: Best-effort click write; redirect even if the count fails
- **Status:** Active
- **Origin:** new
- **Where:** app.py (`follow`), test_app.py (`test_redirect_survives_failed_click_write`)
- **Context:** The redirect is the product; the count is a statistic. The count write can fail when reads still work: disk full, read-only DB file after a restore, or another writer holding the lock past sqlite3's 5 s timeout.
- **Options:**
  - Single `UPDATE ... RETURNING url`: one statement. Any write failure turns every redirect into a 500.
  - SELECT, then UPDATE with errors propagating: same outage, just with two statements.
  - SELECT, then UPDATE inside `try/except sqlite3.Error` that logs: redirects keep working.
- **Choice:** Best-effort write that catches `sqlite3.Error` and logs the code.
- **Trade-off:** Counts can silently undercount while the DB is unwritable. The only signal is the log line `could not record click for <code>`.
- **Revisit when:** Counts are used for anything that needs to be exact (billing, reporting to others).

## D-005: Fetch the title once at link creation and store it, over fetching on each GET /links/<code>
- **Status:** Active
- **Origin:** new (title preview requested by user; when and how to fetch chosen by agent)
- **Where:** app.py (`fetch_title`, `create_link`, `init_db`, `link_info`), test_app.py, README.md (Title preview notes)
- **Context:** User asked for the target page's title in `GET /links/<code>`. That endpoint is unauthenticated and not rate limited; `POST /links` is limited to 10/min per IP (D-001). One process, one SQLite file, one maintainer.
- **Options:**
  - Fetch on every `GET /links/<code>`: always current. Anyone can make the VM hit a site as fast as they can call the endpoint, and every info request waits on (or fails with) a third-party site.
  - Fetch lazily on first read and cache: same amplification whenever the fetch fails and is retried, plus a slow first read.
  - Background thread or queue after create: fast `POST`. Another moving part; pending fetches are lost on restart and the title appears "later".
  - Synchronous fetch in `POST /links`, stored in a nullable `title` column: one fetch per link, capped by the existing rate limit.
- **Choice:** Synchronous fetch in `POST /links`, before the INSERT so no DB transaction is open during the network call. Stdlib only (`urllib.request`, `html.parser`) instead of `requests` + BeautifulSoup. Column added by an idempotent `ALTER TABLE` like `clicks` (D-003). Limits: 3 s socket timeout and body-read budget, 256 KB, 3 redirects, 300 chars.
- **Trade-off:** Titles are a creation-time snapshot and go stale. Link creation gets slower: about 3 s extra for an unresponsive site. Existing links are not backfilled. One-time URLs (password reset, magic login) may be used up by the fetch.
- **Revisit when:** Stale titles matter (add a refresh), create latency becomes a complaint (move to a background fetch), or old links need titles (one-off backfill script).

## D-006: Refuse non-public IPs at connect time, on every hop, over no check or a hostname check
- **Status:** Active
- **Origin:** new
- **Where:** app.py (`is_public_address`, `_connect_public`, `_PublicOnly`, `_title_opener`), test_app.py (SSRF tests)
- **Context:** Anyone can make the server request any URL and then read back the page's title. Without a check, that exposes pages on localhost, the VM's private network and the cloud metadata service.
- **Options:**
  - No check: simplest. Server-side request forgery (SSRF) against the VM and its LAN.
  - Check the URL's hostname before fetching: misses redirects to internal hosts and DNS names that resolve (or re-resolve, via DNS rebinding) to internal IPs.
  - Check the resolved IP inside the connection, and dial that IP: covers redirects, DNS names and rebinding. Needs a hand-built opener without proxy/ftp/file handlers, and relies on `http.client`'s private `_create_connection` hook.
- **Choice:** Connect-time check with `ipaddress.is_global`; a host is refused if any of its resolved addresses is non-public. TLS still verifies against the original hostname.
- **Trade-off:** Links to intranet/LAN/Tailscale hosts never get a title. A Python upgrade that drops the `_create_connection` hook would silently skip the check; `test_fetch_title_refuses_loopback` and `test_fetch_title_rechecks_address_on_redirect` fail if that happens, so run the tests after upgrading Python.
- **Revisit when:** Titles are wanted for internal sites (add an allowlist), or Python changes `http.client` internals.

## D-007: Any title-fetch failure stores null and still creates the link
- **Status:** Active
- **Origin:** follows D-004 (best-effort side work must not break the main path)
- **Where:** app.py (`fetch_title`)
- **Context:** The short link is the product; the title is cosmetic. Input is an arbitrary remote page: timeouts, TLS errors, HTTP errors, malformed markup, odd encodings.
- **Options:**
  - Reject the `POST` when the title can't be fetched: breaks shortening for any site that blocks bots or is briefly down.
  - Catch a list of expected exceptions: an unlisted one from hostile input becomes a 500 on link creation.
  - Catch `Exception` in `fetch_title`, log a warning, return `None`.
- **Choice:** Catch `Exception` and log `could not fetch title for <url>: <error>`, without a traceback.
- **Trade-off:** A programming bug in the fetcher would show up only as null titles and warning lines, not as errors; the tests cover the success paths to catch that.
- **Revisit when:** Titles become required data rather than a preview.

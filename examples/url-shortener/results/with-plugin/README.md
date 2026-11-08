# url-shortener

A personal link shortener. One Flask process and one SQLite file, running on a single small VM. I maintain it alone.

```bash
pip install -r requirements.txt
python app.py          # serves on :5000
python -m pytest -q    # tests
```

- `POST /links {"url": ...}` creates a short code. Limited to 10 requests per 60 s per client IP; over the limit it returns `429` with a `Retry-After` header. Tune with `SHORTENER_RATE_LIMIT` and `SHORTENER_RATE_WINDOW` (seconds).
- `GET /<code>` redirects and adds one to the link's click count.
- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`, `title` (the target page's `<title>`, or `null`).

## Title preview notes

- The title is fetched once, when the link is created, and stored. It is not refreshed if the page changes later. Links created before this feature have `title: null`.
- Creating a link waits for that fetch: usually well under a second; a slow or unresponsive site adds about 3 s (a deliberately slow redirect chain, longer).
- `title` is `null` when the page couldn't be loaded within 3 s, returned an error status, isn't HTML, has no `<title>`, or the host is not a public internet address. The link is still created; the reason is logged as `could not fetch title for <url>`.
- Only public addresses are fetched, checked on every redirect hop (3 at most). Loopback, private LAN, link-local (cloud metadata at `169.254.169.254`) and CGNAT/Tailscale ranges are refused, so nobody can use the shortener to read pages on the VM or its network. Links to intranet sites therefore never get a title. `HTTP_PROXY` and similar settings are ignored for this fetch.
- The target site sees one `GET` from the VM's IP with user agent `url-shortener/1.0 (title preview)`. Some sites block unfamiliar clients and will show `null`. Don't shorten one-time links (password reset, magic login, unsubscribe): this fetch may use them up.
- Only the first 256 KB is read. The text encoding comes from the `Content-Type` header (default UTF-8); a `<meta charset>` tag is not read, so a page declaring a non-UTF-8 encoding only there may show garbled characters.

## Click counting notes

- `clicks` counts every redirect served: bots, link-preview fetchers (Slack, iMessage) and `HEAD` requests included. Redirects are not rate limited, so anyone can inflate a count.
- It is a running total only. There is no per-click history (time, referrer), so clicks-per-day cannot be reconstructed later.
- If the count can't be written (disk full, database locked, read-only file), the redirect still works. The click is dropped and logged as `could not record click for <code>`.
- Databases from before click counting get the column added at startup. Existing links start at 0.

## Rate limiting notes

- Counts are kept in process memory: a restart clears them, and running more than one worker process would give each its own counts.
- The client IP is the TCP peer address (`request.remote_addr`). If you put a reverse proxy such as nginx in front, every request will appear to come from the proxy and share one limit. In that case wrap the app with `werkzeug.middleware.proxy_fix.ProxyFix(app.wsgi_app, x_for=1)` so the proxy's `X-Forwarded-For` is used.

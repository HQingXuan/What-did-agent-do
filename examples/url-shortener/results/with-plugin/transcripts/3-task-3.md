# task-3

**Prompt:** Show a title preview for each link: GET /links/<code> should include the target page's title.

**Reply:**

`GET /links/<code>` now returns a `title` field with the target page's `<title>`, or `null`. All 48 tests pass. I also tried it against the real internet: example.com came back as "Example Domain" and python.org as "Welcome to Python.org", each link creation taking under 0.05 s. URLs pointing at the cloud metadata address and at localhost got no title and were still created.

**How it works**
- The title is fetched once, when the link is created, and saved in a new `title` column. Existing databases get the column added automatically at startup, the same way `clicks` was.
- If the page can't be loaded, isn't HTML, returns an error or has no `<title>`, the link is still created with `title: null`, and a warning is logged.
- The fetch has limits: 3 s timeout, first 256 KB of the page, at most 3 redirects, titles cut to 300 characters. A server that sends data very slowly gets cut off when the time runs out.

**Security**
Anyone can create a link, and the title is shown back to them. Without a check, that would let them read pages on the server itself or its private network, including the cloud metadata service (which can expose credentials). So the fetcher refuses any non-public address. It checks the address it actually connects to, on every redirect, so a public URL that redirects or resolves to an internal one is caught too. Proxy settings and `ftp://` are left out because both would skip the check. I confirmed these tests catch real problems by breaking the protection on purpose and watching them fail. Two of my tests passed even with it broken, so I replaced them.

Decision: fetch title once at creation, stored, over per-GET fetching, because GET isn't rate limited. Cost: stale titles; slow sites delay creation ~3 s. [new]

Decision: refuse non-public IPs at connect time on every hop, over hostname checks, because redirects/DNS bypass those. Cost: intranet links get no title. [new]

Decision: any fetch failure stores null and still creates the link, over rejecting the POST. Cost: fetcher bugs show only as warnings. [follows docs/decisions.md D-004]

These are logged as D-005 to D-007 in `docs/decisions.md`, and the README has a new "Title preview notes" section.

**Worth knowing**
- **One-time links:** the server now visits every URL you shorten. Shortening a password-reset, magic-login or unsubscribe link may use it up.
- **Old links** keep `title: null`. Nothing goes back and fills them in.
- **Titles don't update.** Each one is saved at creation time, and a slow or unresponsive site makes creating its link take up to about 3 s longer.
- **Python upgrades:** the address check depends on an undocumented hook inside Python's HTTP library. If a future Python removes it, the check would stop working without any error, so run the tests after upgrading Python. Two of them will fail if that happens.
- **Not done:** the `POST /links` response doesn't include the title, since you only asked for it in `GET /links/<code>`. It's a one-line change if you want it.


**Diff:**

```diff
diff --git a/README.md b/README.md
index 5e61e11..5fc8b4f 100644
--- a/README.md
+++ b/README.md
@@ -10,7 +10,16 @@ python -m pytest -q    # tests
 
 - `POST /links {"url": ...}` creates a short code. Limited to 10 requests per 60 s per client IP; over the limit it returns `429` with a `Retry-After` header. Tune with `SHORTENER_RATE_LIMIT` and `SHORTENER_RATE_WINDOW` (seconds).
 - `GET /<code>` redirects and adds one to the link's click count.
-- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`.
+- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`, `title` (the target page's `<title>`, or `null`).
+
+## Title preview notes
+
+- The title is fetched once, when the link is created, and stored. It is not refreshed if the page changes later. Links created before this feature have `title: null`.
+- Creating a link waits for that fetch: usually well under a second; a slow or unresponsive site adds about 3 s (a deliberately slow redirect chain, longer).
+- `title` is `null` when the page couldn't be loaded within 3 s, returned an error status, isn't HTML, has no `<title>`, or the host is not a public internet address. The link is still created; the reason is logged as `could not fetch title for <url>`.
+- Only public addresses are fetched, checked on every redirect hop (3 at most). Loopback, private LAN, link-local (cloud metadata at `169.254.169.254`) and CGNAT/Tailscale ranges are refused, so nobody can use the shortener to read pages on the VM or its network. Links to intranet sites therefore never get a title. `HTTP_PROXY` and similar settings are ignored for this fetch.
+- The target site sees one `GET` from the VM's IP with user agent `url-shortener/1.0 (title preview)`. Some sites block unfamiliar clients and will show `null`. Don't shorten one-time links (password reset, magic login, unsubscribe): this fetch may use them up.
+- Only the first 256 KB is read. The text encoding comes from the `Content-Type` header (default UTF-8); a `<meta charset>` tag is not read, so a page declaring a non-UTF-8 encoding only there may show garbled characters.
 
 ## Click counting notes
 
diff --git a/app.py b/app.py
index 52bf826..a772915 100644
--- a/app.py
+++ b/app.py
@@ -1,10 +1,16 @@
 """A tiny URL shortener: one Flask process, one SQLite file."""
+import codecs
+import http.client
+import ipaddress
 import math
 import os
 import secrets
+import socket
 import sqlite3
 import threading
 import time
+import urllib.request
+from html.parser import HTMLParser
 from urllib.parse import urlparse
 
 from flask import Flask, abort, g, jsonify, redirect, request
@@ -14,6 +20,13 @@ DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")
 RATE_LIMIT = int(os.environ.get("SHORTENER_RATE_LIMIT", "10"))
 RATE_WINDOW = int(os.environ.get("SHORTENER_RATE_WINDOW", "60"))
 
+# Title previews are fetched once, when a link is created (see fetch_title).
+TITLE_FETCH_SECONDS = 3  # socket timeout, and the budget for reading the page body
+TITLE_MAX_BYTES = 256 * 1024  # <title> sits in <head>; stop reading after this much
+TITLE_MAX_LEN = 300
+TITLE_MAX_REDIRECTS = 3
+TITLE_USER_AGENT = "url-shortener/1.0 (title preview)"
+
 app = Flask(__name__)
 
 
@@ -54,6 +67,142 @@ class FixedWindowLimiter:
 link_limiter = FixedWindowLimiter(RATE_LIMIT, RATE_WINDOW)
 
 
+def is_public_address(ip):
+    """True for globally routable IPs. False for loopback, private, link-local (cloud
+    metadata), CGNAT/Tailscale, reserved and IPv4-mapped forms of those."""
+    return ipaddress.ip_address(ip).is_global
+
+
+def _connect_public(address, timeout, source_address=None):
+    """socket.create_connection that refuses hosts resolving to non-public addresses.
+
+    Checking the address actually dialled, not the URL's hostname, also covers
+    redirects and DNS names pointing at internal IPs, and connecting to the checked IP
+    (not re-resolving) stops DNS rebinding between the check and the connect.
+    """
+    host, port = address
+    ips = [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
+    blocked = [ip for ip in ips if not is_public_address(ip)]
+    if blocked:
+        raise ConnectionRefusedError(f"{host} resolves to non-public address {blocked[0]}")
+    for i, ip in enumerate(ips):
+        try:
+            return socket.create_connection((ip, port), timeout, source_address)
+        except OSError:
+            if i == len(ips) - 1:
+                raise
+
+
+class _PublicOnly:
+    # http.client dials through this per-instance hook; HTTPS then wraps the socket
+    # with TLS against the original hostname. test_fetch_title_refuses_loopback fails
+    # if a Python upgrade stops honouring it.
+    def __init__(self, *args, **kwargs):
+        super().__init__(*args, **kwargs)
+        self._create_connection = _connect_public
+
+
+class _PublicHTTPConnection(_PublicOnly, http.client.HTTPConnection):
+    pass
+
+
+class _PublicHTTPSConnection(_PublicOnly, http.client.HTTPSConnection):
+    pass
+
+
+class _PublicHTTPHandler(urllib.request.HTTPHandler):
+    def http_open(self, req):
+        return self.do_open(_PublicHTTPConnection, req)
+
+
+class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
+    def https_open(self, req):
+        return self.do_open(_PublicHTTPSConnection, req, context=self._context)
+
+
+class _TitleRedirectHandler(urllib.request.HTTPRedirectHandler):
+    max_redirections = TITLE_MAX_REDIRECTS
+
+
+# Assembled by hand: build_opener() would also add a proxy handler (HTTP_PROXY env)
+# and an ftp:// handler, both of which would bypass the public-address check.
+_title_opener = urllib.request.OpenerDirector()
+for _handler in (
+    _PublicHTTPHandler(),
+    _PublicHTTPSHandler(),
+    _TitleRedirectHandler(),
+    urllib.request.HTTPDefaultErrorHandler(),
+    urllib.request.HTTPErrorProcessor(),
+    urllib.request.UnknownHandler(),
+):
+    _title_opener.add_handler(_handler)
+
+
+class _TitleParser(HTMLParser):
+    """Collects the text of the first <title> element. Text inside <script> is skipped."""
+
+    def __init__(self):
+        super().__init__()
+        self._in_title = False
+        self._parts = []
+        self.done = False
+
+    def handle_starttag(self, tag, attrs):
+        if tag == "title" and not self.done:
+            self._in_title = True
+
+    def handle_endtag(self, tag):
+        if tag == "title" and self._in_title:
+            self._in_title = False
+            self.done = True
+
+    def handle_data(self, data):
+        if self._in_title:
+            self._parts.append(data)
+
+    @property
+    def title(self):
+        if not self.done:
+            return None
+        return " ".join("".join(self._parts).split())[:TITLE_MAX_LEN] or None
+
+
+def fetch_title(url):
+    """Return the <title> of the HTML page at `url`, or None if it can't be had.
+
+    Best effort: any failure (unreachable, slow, not HTML, HTTP error, non-public
+    address, odd markup) gives None and a log line, never an exception.
+    """
+    deadline = time.monotonic() + TITLE_FETCH_SECONDS
+    req = urllib.request.Request(
+        url, headers={"User-Agent": TITLE_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
+    )
+    try:
+        with _title_opener.open(req, timeout=TITLE_FETCH_SECONDS) as resp:
+            if resp.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
+                return None
+            try:
+                decoder = codecs.getincrementaldecoder(
+                    resp.headers.get_content_charset() or "utf-8"
+                )(errors="replace")
+            except LookupError:
+                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
+            parser = _TitleParser()
+            received = 0
+            # read1 returns whatever has arrived, so a server dripping bytes slowly is
+            # cut off at the deadline instead of holding the request open.
+            while not parser.done and received < TITLE_MAX_BYTES and time.monotonic() < deadline:
+                chunk = resp.read1(16 * 1024)
+                if not chunk:
+                    break
+                received += len(chunk)
+                parser.feed(decoder.decode(chunk))
+            return parser.title
+    except Exception as exc:  # hostile remote input: the title must never break link creation
+        app.logger.warning("could not fetch title for %s: %r", url, exc)
+        return None
+
+
 def db():
     if "db" not in g:
         g.db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
@@ -74,13 +223,16 @@ def init_db():
                code TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
-               clicks INTEGER NOT NULL DEFAULT 0
+               clicks INTEGER NOT NULL DEFAULT 0,
+               title TEXT
            )"""
     )
-    # Databases created before click counting lack the column; existing links start at 0.
+    # Older databases lack later columns. Existing links start at 0 clicks and no title.
     columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
     if "clicks" not in columns:
         db().execute("ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0")
+    if "title" not in columns:
+        db().execute("ALTER TABLE links ADD COLUMN title TEXT")
     db().commit()
 
 
@@ -103,8 +255,11 @@ def create_link():
     url = (request.get_json(silent=True) or {}).get("url", "")
     if not valid_url(url):
         return jsonify(error="url must be an http(s) URL"), 400
+    # Fetched once here, behind the rate limit, rather than on every GET /links/<code>.
+    # Done before touching the DB so no transaction is held open during the fetch.
+    title = fetch_title(url)
     code = secrets.token_urlsafe(5)
-    db().execute("INSERT INTO links (code, url) VALUES (?, ?)", (code, url))
+    db().execute("INSERT INTO links (code, url, title) VALUES (?, ?, ?)", (code, url, title))
     db().commit()
     return jsonify(code=code, short_url=request.host_url + code), 201
 
@@ -112,7 +267,7 @@ def create_link():
 @app.get("/links/<code>")
 def link_info(code):
     row = db().execute(
-        "SELECT code, url, created_at, clicks FROM links WHERE code = ?", (code,)
+        "SELECT code, url, created_at, clicks, title FROM links WHERE code = ?", (code,)
     ).fetchone()
     if row is None:
         abort(404)
diff --git a/test_app.py b/test_app.py
index 367cdfd..183275f 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,8 +1,67 @@
+import socket
 import sqlite3
+import threading
+import time
+import urllib.request
+from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
+from types import SimpleNamespace
 
 import pytest
 
-from app import FixedWindowLimiter, app, init_db, link_limiter
+import app as shortener
+from app import FixedWindowLimiter, app, fetch_title, init_db, is_public_address, link_limiter
+
+
+@pytest.fixture(autouse=True)
+def fetched_urls(monkeypatch):
+    """Keep tests off the network: link creation sees no title unless a test says otherwise.
+
+    Only shortener.fetch_title (what create_link calls) is replaced; tests of the real
+    fetcher call the `fetch_title` imported above.
+    """
+    urls = []
+    monkeypatch.setattr(shortener, "fetch_title", lambda url: urls.append(url))
+    return urls
+
+
+@pytest.fixture
+def site():
+    """Local web server. Set site.pages[path] to (status, headers, body) or a callable
+    taking the handler; site.hits lists the paths requested, in order."""
+    pages, hits = {}, []
+
+    class Handler(BaseHTTPRequestHandler):
+        def do_GET(self):
+            hits.append(self.path)
+            page = pages.get(self.path, (404, {"Content-Type": "text/html"}, b"<title>404</title>"))
+            if callable(page):
+                return page(self)
+            status, headers, body = page
+            self.send_response(status)
+            for name, value in headers.items():
+                self.send_header(name, value)
+            self.send_header("Content-Length", str(len(body)))
+            self.end_headers()
+            self.wfile.write(body)
+
+        def log_message(self, *args):
+            pass
+
+    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
+    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
+    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", pages=pages, hits=hits)
+    server.shutdown()
+    server.server_close()
+
+
+@pytest.fixture
+def allow_loopback(monkeypatch):
+    """Let the fetcher reach the local test server, which the address check would refuse."""
+    monkeypatch.setattr(shortener, "is_public_address", lambda ip: ip == "127.0.0.1")
+
+
+def html(body, content_type="text/html; charset=utf-8"):
+    return (200, {"Content-Type": content_type}, body)
 
 
 @pytest.fixture
@@ -116,7 +175,7 @@ def test_redirect_survives_failed_click_write(client, tmp_path, monkeypatch, cap
     assert client.get(f"/links/{code}").get_json()["clicks"] == 0
 
 
-def test_init_db_adds_clicks_to_existing_db(tmp_path):
+def test_init_db_upgrades_existing_db(tmp_path):
     path = tmp_path / "old.db"
     old = sqlite3.connect(path)
     old.execute(
@@ -131,6 +190,173 @@ def test_init_db_adds_clicks_to_existing_db(tmp_path):
         init_db()
         init_db()  # idempotent on an already-migrated DB
     client = app.test_client()
-    assert client.get("/links/old1").get_json()["clicks"] == 0
+    info = client.get("/links/old1").get_json()
+    assert info["clicks"] == 0
+    assert info["title"] is None  # old links are not backfilled
     assert client.get("/old1").status_code == 302
     assert client.get("/links/old1").get_json()["clicks"] == 1
+
+
+# --- Title previews: storage and API ---
+
+
+def test_link_info_includes_title(client, monkeypatch):
+    seen = []
+    monkeypatch.setattr(shortener, "fetch_title", lambda url: seen.append(url) or "Example Domain")
+    code = post_link(client, url="https://example.com/page").get_json()["code"]
+    assert seen == ["https://example.com/page"]
+    assert client.get(f"/links/{code}").get_json()["title"] == "Example Domain"
+
+
+def test_link_created_without_title_when_unavailable(client):
+    res = post_link(client)
+    assert res.status_code == 201
+    assert client.get(f"/links/{res.get_json()['code']}").get_json()["title"] is None
+
+
+def test_title_fetched_only_at_creation(client, fetched_urls):
+    code = post_link(client).get_json()["code"]
+    client.get(f"/links/{code}")
+    client.get(f"/{code}")
+    assert fetched_urls == ["https://example.com/"]
+
+
+def test_title_not_fetched_for_rejected_requests(client, fetched_urls, monkeypatch):
+    monkeypatch.setattr(link_limiter, "limit", 1)
+    post_link(client, url="ftp://x")  # invalid, and uses up the limit
+    post_link(client)  # rate limited
+    assert fetched_urls == []
+
+
+# --- Title previews: the fetcher ---
+
+
+def test_fetch_title_extracts_first_title(site, allow_loopback):
+    site.pages["/"] = html(
+        b"<html><head><script>var s = '<title>fake</title>';</script>"
+        b"<title>\n   Tom &amp; Jerry \n</title></head><body><title>later</title></body></html>"
+    )
+    assert fetch_title(site.url + "/") == "Tom & Jerry"
+
+
+def test_fetch_title_uses_header_charset(site, allow_loopback):
+    site.pages["/"] = html("<title>Café</title>".encode("latin-1"), "text/html; charset=iso-8859-1")
+    assert fetch_title(site.url + "/") == "Café"
+
+
+def test_fetch_title_truncates_long_titles(site, allow_loopback):
+    site.pages["/"] = html(b"<title>" + b"x" * 5000 + b"</title>")
+    assert fetch_title(site.url + "/") == "x" * shortener.TITLE_MAX_LEN
+
+
+@pytest.mark.parametrize(
+    "page",
+    [
+        (200, {"Content-Type": "application/pdf"}, b"<title>not html</title>"),
+        (404, {"Content-Type": "text/html"}, b"<title>Not Found</title>"),
+        html(b"<html><head></head><body>no title</body></html>"),
+        html(b"<title>   </title>"),
+        # Recent Pythons (3.12.15 included) parse <title> as RCDATA, so this text is never
+        # emitted; older ones emit it early, which _TitleParser.done guards against.
+        html(b"<title>never closed<body><p>whole page</p></body>"),
+    ],
+    ids=["not-html", "http-error", "no-title", "blank-title", "unclosed"],
+)
+def test_fetch_title_none_when_no_usable_title(site, allow_loopback, page):
+    site.pages["/"] = page
+    assert fetch_title(site.url + "/") is None
+
+
+def test_fetch_title_follows_redirects(site, allow_loopback):
+    site.pages["/old"] = (301, {"Location": "/new"}, b"")
+    site.pages["/new"] = html(b"<title>Moved here</title>")
+    assert fetch_title(site.url + "/old") == "Moved here"
+
+
+@pytest.mark.parametrize("hops, expected", [(3, "end"), (4, None)])
+def test_fetch_title_caps_redirects(site, allow_loopback, hops, expected):
+    for i in range(hops):
+        site.pages[f"/r{i}"] = (302, {"Location": f"/r{i + 1}"}, b"")
+    site.pages[f"/r{hops}"] = html(b"<title>end</title>")
+    assert fetch_title(site.url + "/r0") == expected
+
+
+def test_fetch_title_gives_up_at_deadline(site, allow_loopback, monkeypatch):
+    monkeypatch.setattr(shortener, "TITLE_FETCH_SECONDS", 0.5)
+
+    def drip(handler):  # sends a byte every 0.1 s, never reaching a title in time
+        handler.send_response(200)
+        handler.send_header("Content-Type", "text/html")
+        handler.end_headers()
+        try:
+            for _ in range(50):
+                handler.wfile.write(b" ")
+                handler.wfile.flush()
+                time.sleep(0.1)
+            handler.wfile.write(b"<title>too late</title>")
+        except OSError:
+            pass  # the fetcher hung up, as intended
+
+    site.pages["/"] = drip
+    start = time.monotonic()
+    assert fetch_title(site.url + "/") is None
+    assert time.monotonic() - start < 2
+
+
+def test_fetch_title_unreachable_host_logs(allow_loopback, caplog):
+    with socket.socket() as s:  # grab a free port, then close it so nothing listens there
+        s.bind(("127.0.0.1", 0))
+        port = s.getsockname()[1]
+    assert fetch_title(f"http://127.0.0.1:{port}/") is None
+    assert "could not fetch title" in caplog.text
+
+
+# --- Title previews: refusing internal addresses (SSRF) ---
+
+
+@pytest.mark.parametrize(
+    "ip",
+    ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254",
+     "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1"],
+)
+def test_internal_addresses_not_public(ip):
+    assert not is_public_address(ip)
+
+
+@pytest.mark.parametrize("ip", ["93.184.215.14", "1.1.1.1", "2606:4700:4700::1111"])
+def test_internet_addresses_public(ip):
+    assert is_public_address(ip)
+
+
+@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
+def test_fetch_title_refuses_loopback(site, host, caplog):
+    site.pages["/"] = html(b"<title>internal admin</title>")
+    port = site.url.rsplit(":", 1)[1]
+    assert fetch_title(f"http://{host}:{port}/") is None
+    assert site.hits == []  # never connected, so nothing internal was requested
+    assert "non-public address" in caplog.text
+
+
+def test_fetch_title_rechecks_address_on_redirect(site, monkeypatch):
+    # The first hop passes the check; the redirect hop is refused before connecting.
+    checked = []
+    monkeypatch.setattr(shortener, "is_public_address", lambda ip: checked.append(ip) or len(checked) == 1)
+    site.pages["/start"] = (302, {"Location": "/secret"}, b"")
+    site.pages["/secret"] = html(b"<title>internal admin</title>")
+    assert fetch_title(site.url + "/start") is None
+    assert site.hits == ["/start"]
+    assert len(checked) == 2
+
+
+def test_title_opener_only_dials_through_address_check():
+    # urllib.request.build_opener() would add ftp:// (reachable via redirect) and file://
+    # handlers, plus a ProxyHandler when HTTP_PROXY is set. All of those connect without
+    # the public-address check, so the opener must contain nothing but the guarded handlers.
+    handlers = {
+        scheme: [type(h) for h in hs] for scheme, hs in shortener._title_opener.handle_open.items()
+    }
+    assert handlers == {
+        "http": [shortener._PublicHTTPHandler],
+        "https": [shortener._PublicHTTPSHandler],
+        "unknown": [urllib.request.UnknownHandler],
+    }
```

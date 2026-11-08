# task-3

**Prompt:** Show a title preview for each link: GET /links/<code> should include the target page's title.

**Reply:**

`GET /links/<code>` now includes a `title` field with the target page's `<title>`, or `null` if none could be found. I tested it against real sites ("Welcome to Python.org", "Example Domain") and over HTTP with the server running. All 52 tests pass, 12 old and 40 new.

**How it works**
- **Fetched on the first view, then stored** in two new columns. Refetching on every view would make lookups slow, and since `GET /links` isn't rate-limited, anyone could make the server send unlimited outbound requests. Storing results caps fetches at about one per link, and link creation is already rate-limited. Existing links get titles too.
- **Retries:** a found title is kept for good. If no title was found (site down, not HTML, error page), the next view at least an hour later tries again.
- **Database:** the new columns are added automatically on startup, like `clicks` was.
- **No new dependencies:** it uses only Python's standard library.

**Protection against reaching internal addresses (SSRF)**
`POST /links` is public, so anyone can make the server fetch any URL. Without a guard, a link to `http://localhost:…` or `http://169.254.169.254/` (the cloud metadata endpoint) would make your VM request its own internal addresses. Now:
- The server looks up the hostname once, refuses any non-public address, and connects to the address it checked, so DNS can't switch it to a private one in between.
- Every redirect is checked the same way, and redirects are only followed to `http(s)`.
- It caught the disguised forms of localhost I tried: `localhost`, `0x7f000001`, `2130706433` and `::ffff:127.0.0.1`.
- To confirm the security tests actually work, I disabled each protection in turn and checked that the relevant tests failed.

**Limits:** 5-second time budget, at most 256 KB read per page, 5 redirects, titles cut to 300 characters, HTML only, certificates verified. Non-UTF-8 pages are decoded using the charset the page declares.

**Tests:** the endpoint tests use a fake fetcher, so the suite stays offline and takes about 1.4s. The real fetcher is tested against a local HTTP server.

**Worth knowing**
- **First view can be slow:** the first `GET /links/<code>` for a link waits for the fetch, up to about 5 seconds. Later views read the stored title.
- **The 5-second limit isn't absolute.** DNS lookups aren't covered by it, and a server that sends response headers one byte at a time can tie up a request thread for longer. That's no worse than someone holding slow connections open to the Flask server directly.
- **Titles that change** on the target page aren't picked up, because found titles aren't refreshed.
- **No outbound proxy:** fetches ignore proxy settings, so if the VM can only reach the internet through a proxy, titles will come back `null`.
- **Your local `shortener.db` was already updated.** Importing `app.py` sets up the database, and running the tests did that to the file in this directory. It now has the two new empty columns; it has no rows and is gitignored.
- **Fetch failures aren't visible by default:** they're logged at INFO level, which Flask doesn't show unless you lower the log level.

The changes are in `app.py` and `test_app.py`, and `README.md` documents the new field.


**Diff:**

```diff
diff --git a/README.md b/README.md
index accce70..6e17d12 100644
--- a/README.md
+++ b/README.md
@@ -10,4 +10,6 @@ python -m pytest -q    # tests
 
 - `POST /links {"url": ...}` creates a short code. Limited to 10 requests per minute per client IP; beyond that it returns `429` with a `Retry-After` header.
 - `GET /<code>` redirects and counts a click.
-- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, and `clicks`.
+- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`, and `title`.
+  - `title` is the target page's `<title>`, or `null` if none could be found (unreachable, not HTML, error status). It is fetched on the first view and stored, so that view can take up to ~5 s. A found title is kept as is; a `null` one is retried on a view at least an hour later.
+  - Fetches only go to public IP addresses, so links can't be used to reach localhost, the private network, or cloud metadata endpoints.
diff --git a/app.py b/app.py
index 88b6396..483e8f7 100644
--- a/app.py
+++ b/app.py
@@ -1,12 +1,18 @@
 """A tiny URL shortener: one Flask process, one SQLite file."""
+import http.client
+import ipaddress
 import math
 import os
+import re
 import secrets
+import socket
 import sqlite3
+import string
 import threading
 import time
 from collections import deque
-from urllib.parse import urlparse
+from html.parser import HTMLParser
+from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
 
 from flask import Flask, abort, g, jsonify, redirect, request
 
@@ -16,6 +22,15 @@ DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")
 LINK_RATE_LIMIT = 10
 LINK_RATE_WINDOW = 60  # seconds
 
+# GET /links/<code> includes the target page's <title>. It is fetched on the
+# first view and stored; a link where no title was found is retried later.
+TITLE_FETCH_TIMEOUT = 5  # seconds for the whole fetch, redirects included
+TITLE_MAX_REDIRECTS = 5
+TITLE_MAX_BYTES = 256 * 1024  # how much of the page to read looking for it
+TITLE_MAX_LENGTH = 300  # characters kept
+TITLE_RETRY_AFTER = 60 * 60  # seconds before retrying a link with no title
+TITLE_USER_AGENT = "Mozilla/5.0 (compatible; url-shortener title preview)"
+
 app = Flask(__name__)
 
 
@@ -85,13 +100,20 @@ def init_db():
                code TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
-               clicks INTEGER NOT NULL DEFAULT 0
+               clicks INTEGER NOT NULL DEFAULT 0,
+               title TEXT,
+               title_fetched_at TEXT
            )"""
     )
-    # Databases created before click counting existed lack the column.
+    # Databases created by earlier versions lack the newer columns.
     columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
-    if "clicks" not in columns:
-        db().execute("ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0")
+    for name, definition in [
+        ("clicks", "INTEGER NOT NULL DEFAULT 0"),
+        ("title", "TEXT"),
+        ("title_fetched_at", "TEXT"),  # NULL until a fetch has been tried
+    ]:
+        if name not in columns:
+            db().execute(f"ALTER TABLE links ADD COLUMN {name} {definition}")
     db().commit()
 
 
@@ -100,6 +122,168 @@ def valid_url(url):
     return parts.scheme in ("http", "https") and bool(parts.netloc)
 
 
+# --- Fetching page titles ---------------------------------------------------
+#
+# The server fetches URLs that anyone can submit, so without a guard a link
+# could make it request localhost, the private network, or a cloud metadata
+# endpoint. Every connection, including each redirect hop, goes through
+# _connect_public, which only connects to public addresses.
+
+
+class BlockedAddress(OSError):
+    pass
+
+
+def _is_public(ip):
+    addr = ipaddress.ip_address(ip)
+    if getattr(addr, "ipv4_mapped", None):  # ::ffff:127.0.0.1 is still loopback
+        addr = addr.ipv4_mapped
+    return addr.is_global
+
+
+def _connect_public(host, port, timeout):
+    # Resolve once and connect to the vetted address, so DNS can't hand back a
+    # private address between the check and the connection.
+    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
+    for *_, sockaddr in infos:
+        if not _is_public(sockaddr[0]):
+            raise BlockedAddress(f"{host} resolves to non-public address {sockaddr[0]}")
+    error = OSError(f"no addresses for {host}")
+    for family, type_, proto, _, sockaddr in infos:
+        sock = socket.socket(family, type_, proto)
+        sock.settimeout(timeout)
+        try:
+            sock.connect(sockaddr)
+            return sock
+        except OSError as exc:
+            sock.close()
+            error = exc
+    raise error
+
+
+class _PublicHTTPConnection(http.client.HTTPConnection):
+    def connect(self):
+        self.sock = _connect_public(self.host, self.port, self.timeout)
+
+
+class _PublicHTTPSConnection(http.client.HTTPSConnection, _PublicHTTPConnection):
+    # HTTPSConnection.connect opens the TCP connection via super().connect()
+    # and then does the TLS handshake. With these bases, that super() call is
+    # _PublicHTTPConnection.connect, so HTTPS gets the same guard.
+    pass
+
+
+class _TitleParser(HTMLParser):
+    """Collects the text of the document's first <title>.
+
+    <title> inside <svg> labels a graphic, not the page, so it's skipped.
+    """
+
+    def __init__(self):
+        super().__init__()
+        self.parts = []
+        self._in_title = False
+        self._done = False
+        self._svg_depth = 0
+
+    def handle_starttag(self, tag, attrs):
+        if tag == "svg":
+            self._svg_depth += 1
+        elif tag == "title" and not self._svg_depth and not self._done:
+            self._in_title = True
+
+    def handle_endtag(self, tag):
+        if tag == "svg" and self._svg_depth:
+            self._svg_depth -= 1
+        elif tag == "title" and self._in_title:
+            self._in_title = False
+            self._done = True
+
+    def handle_data(self, data):
+        if self._in_title:
+            self.parts.append(data)
+
+
+_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
+_HTML_TYPES = ("text/html", "application/xhtml+xml")
+_REDIRECTS = (301, 302, 303, 307, 308)
+
+
+def fetch_title(url):
+    """Return the <title> of the HTML page at `url`, or None if there isn't
+    one to be had (unreachable, blocked address, error status, not HTML...)."""
+    try:
+        return _fetch_title(url)
+    except (OSError, ValueError, http.client.HTTPException) as exc:
+        app.logger.info("no title for %s: %s", url, exc)
+        return None
+
+
+def _fetch_title(url):
+    deadline = time.monotonic() + TITLE_FETCH_TIMEOUT
+    for _ in range(TITLE_MAX_REDIRECTS + 1):
+        parts = urlsplit(url)
+        if parts.scheme not in ("http", "https") or not parts.hostname:
+            return None
+        remaining = deadline - time.monotonic()
+        if remaining <= 0:
+            raise TimeoutError("title fetch took too long")
+        if parts.scheme == "https":
+            conn = _PublicHTTPSConnection(parts.hostname, parts.port or 443, timeout=remaining)
+        else:
+            conn = _PublicHTTPConnection(parts.hostname, parts.port or 80, timeout=remaining)
+        try:
+            # Percent-encode anything that isn't printable ASCII (e.g. "/café"),
+            # leaving existing escapes and URL punctuation alone.
+            target = quote(urlunsplit(("", "", parts.path or "/", parts.query, "")),
+                           safe=string.punctuation)
+            conn.request("GET", target,
+                         headers={"User-Agent": TITLE_USER_AGENT, "Accept": "text/html"})
+            res = conn.getresponse()
+            location = res.getheader("Location")
+            if res.status in _REDIRECTS and location:
+                url = urljoin(url, location)
+                continue
+            if res.status != 200 or res.headers.get_content_type() not in _HTML_TYPES:
+                return None
+            body = _read_head(res, deadline)
+            charset = res.headers.get_content_charset()
+        finally:
+            conn.close()
+        return _parse_title(body, charset)
+    return None  # too many redirects
+
+
+def _read_head(res, deadline):
+    """Read the body up to the end of its <title>, within the size and time limits."""
+    body = bytearray()
+    while len(body) < TITLE_MAX_BYTES and time.monotonic() < deadline:
+        chunk = res.read1(TITLE_MAX_BYTES - len(body))
+        if not chunk:
+            break
+        overlap = max(len(body) - len(b"</title"), 0)  # in case a read split the tag
+        body += chunk
+        if b"</title" in body[overlap:].lower():
+            break
+    return bytes(body)
+
+
+def _parse_title(body, charset):
+    if not charset:
+        # Browsers look for a <meta> charset in the first 1024 bytes.
+        match = _META_CHARSET.search(body[:1024])
+        charset = match.group(1).decode("ascii") if match else "utf-8"
+    try:
+        text = body.decode(charset, errors="replace")
+    except LookupError:  # a charset Python doesn't know
+        text = body.decode("utf-8", errors="replace")
+    parser = _TitleParser()
+    parser.feed(text)
+    parser.close()
+    title = " ".join("".join(parser.parts).split())
+    return title[:TITLE_MAX_LENGTH] or None
+
+
 @app.post("/links")
 def create_link():
     # remote_addr is the TCP peer. If a reverse proxy is ever put in front,
@@ -122,11 +306,23 @@ def create_link():
 @app.get("/links/<code>")
 def link_info(code):
     row = db().execute(
-        "SELECT code, url, created_at, clicks FROM links WHERE code = ?", (code,)
+        """SELECT code, url, created_at, clicks, title,
+                  title_fetched_at IS NULL
+                  OR (title IS NULL AND title_fetched_at <= datetime('now', ?)) AS title_due
+           FROM links WHERE code = ?""",
+        (f"-{TITLE_RETRY_AFTER} seconds", code),
     ).fetchone()
     if row is None:
         abort(404)
-    return jsonify(dict(row))
+    info = {key: row[key] for key in ("code", "url", "created_at", "clicks", "title")}
+    if row["title_due"]:
+        info["title"] = fetch_title(row["url"])
+        db().execute(
+            "UPDATE links SET title = ?, title_fetched_at = CURRENT_TIMESTAMP WHERE code = ?",
+            (info["title"], code),
+        )
+        db().commit()
+    return jsonify(info)
 
 
 @app.get("/<code>")
diff --git a/test_app.py b/test_app.py
index 4b6242a..54f92c3 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,9 +1,24 @@
+import socket
 import sqlite3
+import threading
+import time
+from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
 
 import pytest
 
 import app as app_module
-from app import LINK_RATE_LIMIT, LINK_RATE_WINDOW, RateLimiter, app, init_db
+from app import (
+    LINK_RATE_LIMIT,
+    LINK_RATE_WINDOW,
+    TITLE_MAX_LENGTH,
+    TITLE_MAX_REDIRECTS,
+    TITLE_RETRY_AFTER,
+    RateLimiter,
+    _is_public,
+    app,
+    fetch_title,
+    init_db,
+)
 
 
 class FakeClock:
@@ -14,6 +29,27 @@ class FakeClock:
         return self.now
 
 
+class FakeTitles:
+    """Stands in for fetch_title: returns titles from a dict, records each fetch."""
+
+    def __init__(self):
+        self.titles = {}
+        self.fetched = []
+
+    def __call__(self, url):
+        self.fetched.append(url)
+        return self.titles.get(url)
+
+
+@pytest.fixture(autouse=True)
+def titles(monkeypatch):
+    # Keeps endpoint tests off the network. Tests of the real fetcher call the
+    # fetch_title imported above, which this doesn't replace.
+    fake = FakeTitles()
+    monkeypatch.setattr(app_module, "fetch_title", fake)
+    return fake
+
+
 @pytest.fixture
 def clock(monkeypatch):
     # Fresh limiter per test so request counts don't leak between tests.
@@ -147,3 +183,278 @@ def test_init_db_adds_clicks_to_existing_database(tmp_path, clock):
     assert client.get("/links/old").get_json()["clicks"] == 0
     assert client.get("/old").status_code == 302
     assert client.get("/links/old").get_json()["clicks"] == 1
+
+
+# --- Title preview: GET /links/<code> ---------------------------------------
+
+
+def backdate_title_fetch(code, seconds):
+    conn = sqlite3.connect(app.config["DB_PATH"])
+    conn.execute(
+        "UPDATE links SET title_fetched_at = datetime(title_fetched_at, ?) WHERE code = ?",
+        (f"-{seconds} seconds", code),
+    )
+    conn.commit()
+    conn.close()
+
+
+def test_link_info_includes_title(client, titles):
+    titles.titles["https://example.com/"] = "Example Domain"
+    code = post_link(client).get_json()["code"]
+    info = client.get(f"/links/{code}").get_json()
+    assert info["title"] == "Example Domain"
+    assert set(info) == {"code", "url", "created_at", "clicks", "title"}
+
+
+def test_creating_link_does_not_fetch_title(client, titles):
+    post_link(client)
+    assert titles.fetched == []
+
+
+def test_title_is_fetched_once(client, titles):
+    titles.titles["https://example.com/"] = "Example Domain"
+    code = post_link(client).get_json()["code"]
+    client.get(f"/links/{code}")
+    titles.titles["https://example.com/"] = "Changed"
+    backdate_title_fetch(code, TITLE_RETRY_AFTER + 1)  # a found title never goes stale
+    assert client.get(f"/links/{code}").get_json()["title"] == "Example Domain"
+    assert titles.fetched == ["https://example.com/"]
+
+
+def test_missing_title_is_null_and_retried_later(client, titles):
+    code = post_link(client).get_json()["code"]
+    assert client.get(f"/links/{code}").get_json()["title"] is None
+    assert client.get(f"/links/{code}").get_json()["title"] is None
+    assert len(titles.fetched) == 1  # not refetched on every view
+
+    titles.titles["https://example.com/"] = "Back Up"
+    backdate_title_fetch(code, TITLE_RETRY_AFTER - 60)
+    assert client.get(f"/links/{code}").get_json()["title"] is None
+    backdate_title_fetch(code, 120)
+    assert client.get(f"/links/{code}").get_json()["title"] == "Back Up"
+    assert len(titles.fetched) == 2
+
+
+def test_init_db_adds_title_to_existing_database(tmp_path, clock, titles):
+    path = tmp_path / "old.db"
+    conn = sqlite3.connect(path)
+    conn.execute(
+        """CREATE TABLE links (
+               code TEXT PRIMARY KEY,
+               url TEXT NOT NULL,
+               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
+               clicks INTEGER NOT NULL DEFAULT 0
+           )"""
+    )
+    conn.execute("INSERT INTO links (code, url, clicks) VALUES ('old', 'https://example.com/', 4)")
+    conn.commit()
+    conn.close()
+
+    titles.titles["https://example.com/"] = "Example Domain"
+    app.config["DB_PATH"] = str(path)
+    with app.app_context():
+        init_db()
+        init_db()  # safe to run again once migrated
+    info = app.test_client().get("/links/old").get_json()
+    assert info["title"] == "Example Domain"
+    assert info["clicks"] == 4
+
+
+# --- Title preview: fetching pages ------------------------------------------
+
+
+class Site:
+    """A local web server serving pages that tests define."""
+
+    def __init__(self):
+        self.pages = {}  # path -> (status, headers, body, delay)
+        self.requests = []
+        site = self
+
+        class Handler(BaseHTTPRequestHandler):
+            def do_GET(self):
+                site.requests.append(self.path)
+                status, headers, body, delay = site.pages.get(self.path, (404, {}, b"", 0))
+                time.sleep(delay)
+                try:
+                    self.send_response(status)
+                    for name, value in headers.items():
+                        self.send_header(name, value)
+                    self.send_header("Content-Length", str(len(body)))
+                    self.end_headers()
+                    self.wfile.write(body)
+                except OSError:
+                    pass  # the client gave up
+
+            def log_message(self, *args):
+                pass
+
+        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
+        threading.Thread(
+            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
+        ).start()
+
+    def url(self, path):
+        return f"http://127.0.0.1:{self.server.server_port}{path}"
+
+    def page(self, path, body, content_type="text/html; charset=utf-8", status=200, delay=0):
+        if isinstance(body, str):
+            body = body.encode("utf-8")
+        self.pages[path] = (status, {"Content-Type": content_type}, body, delay)
+
+    def redirect(self, path, location, status=302):
+        self.pages[path] = (status, {"Location": location}, b"", 0)
+
+
+@pytest.fixture
+def site():
+    site = Site()
+    yield site
+    site.server.shutdown()
+    site.server.server_close()
+
+
+@pytest.fixture
+def allow_loopback(monkeypatch):
+    # The test site runs on 127.0.0.1, which the fetcher normally refuses.
+    monkeypatch.setattr(app_module, "_is_public", lambda ip: True)
+
+
+def test_fetch_title_reads_page_title(site, allow_loopback):
+    site.page("/", "<html><head><title>\n  Fish &amp; Chips\n  Shop </title></head></html>")
+    assert fetch_title(site.url("/")) == "Fish & Chips Shop"
+
+
+@pytest.mark.parametrize(
+    "html",
+    [
+        "<html><body>no title here</body></html>",
+        "<title>   </title>",
+        "<body><svg><title>Close icon</title></svg></body>",
+    ],
+)
+def test_fetch_title_none_when_page_has_no_title(site, allow_loopback, html):
+    site.page("/", html)
+    assert fetch_title(site.url("/")) is None
+
+
+def test_fetch_title_ignores_svg_titles(site, allow_loopback):
+    site.page("/", "<svg><title>Logo</title></svg><title>The Page</title>")
+    assert fetch_title(site.url("/")) == "The Page"
+
+
+def test_fetch_title_ignores_non_html(site, allow_loopback):
+    site.page("/doc", "<title>Not really</title>", content_type="application/pdf")
+    assert fetch_title(site.url("/doc")) is None
+
+
+def test_fetch_title_ignores_error_pages(site, allow_loopback):
+    site.page("/gone", "<title>404 Not Found</title>", status=404)
+    assert fetch_title(site.url("/gone")) is None
+
+
+@pytest.mark.parametrize(
+    "content_type, html",
+    [
+        ("text/html; charset=iso-8859-1", "<title>Café</title>"),
+        ("text/html", '<meta charset="iso-8859-1"><title>Café</title>'),
+        ("text/html", '<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
+                      "<title>Café</title>"),
+    ],
+)
+def test_fetch_title_uses_declared_charset(site, allow_loopback, content_type, html):
+    site.page("/", html.encode("latin-1"), content_type=content_type)
+    assert fetch_title(site.url("/")) == "Café"
+
+
+def test_fetch_title_defaults_to_utf8(site, allow_loopback):
+    site.page("/", "<title>Café ☕</title>".encode("utf-8"), content_type="text/html")
+    assert fetch_title(site.url("/")) == "Café ☕"
+
+
+def test_fetch_title_encodes_non_ascii_paths(site, allow_loopback):
+    site.page("/caf%C3%A9?q=a%20b", "<title>Found</title>")
+    assert fetch_title(site.url("/café?q=a%20b")) == "Found"
+
+
+def test_fetch_title_truncates_long_titles(site, allow_loopback):
+    site.page("/", "<title>" + "x" * (TITLE_MAX_LENGTH + 50) + "</title>")
+    assert fetch_title(site.url("/")) == "x" * TITLE_MAX_LENGTH
+
+
+def test_fetch_title_reads_a_bounded_amount(site, allow_loopback, monkeypatch):
+    monkeypatch.setattr(app_module, "TITLE_MAX_BYTES", 1000)
+    site.page("/", "<!--" + "x" * 2000 + "--><title>Too far down</title>")
+    assert fetch_title(site.url("/")) is None
+
+
+def test_fetch_title_follows_redirects(site, allow_loopback):
+    site.redirect("/old", "/new", status=301)
+    site.page("/new", "<title>New Home</title>")
+    assert fetch_title(site.url("/old")) == "New Home"
+    assert site.requests == ["/old", "/new"]
+
+
+def test_fetch_title_gives_up_on_redirect_loops(site, allow_loopback):
+    site.redirect("/loop", "/loop")
+    assert fetch_title(site.url("/loop")) is None
+    assert len(site.requests) == TITLE_MAX_REDIRECTS + 1
+
+
+def test_fetch_title_only_follows_redirects_to_http(site, allow_loopback):
+    site.redirect("/", "file:///etc/passwd")
+    assert fetch_title(site.url("/")) is None
+
+
+def test_fetch_title_times_out(site, allow_loopback, monkeypatch):
+    monkeypatch.setattr(app_module, "TITLE_FETCH_TIMEOUT", 0.3)
+    site.page("/slow", "<title>Eventually</title>", delay=2)
+    start = time.monotonic()
+    assert fetch_title(site.url("/slow")) is None
+    assert time.monotonic() - start < 1.5
+
+
+def test_fetch_title_handles_unreachable_hosts(allow_loopback):
+    with socket.socket() as sock:
+        sock.bind(("127.0.0.1", 0))
+        port = sock.getsockname()[1]
+    assert fetch_title(f"http://127.0.0.1:{port}/") is None  # nothing listening
+
+
+def test_fetch_title_refuses_private_addresses(site):
+    site.page("/", "<title>Internal</title>")
+    assert fetch_title(site.url("/")) is None
+    assert site.requests == []  # never connected
+
+
+def test_fetch_title_checks_every_redirect_hop(site, monkeypatch):
+    # The first host is treated as public; where it redirects to is not.
+    verdicts = iter([True, False])
+    monkeypatch.setattr(app_module, "_is_public", lambda ip: next(verdicts))
+    site.redirect("/", "/internal")
+    site.page("/internal", "<title>Internal</title>")
+    assert fetch_title(site.url("/")) is None
+    assert site.requests == ["/"]
+
+
+@pytest.mark.parametrize(
+    "ip, public",
+    [
+        ("93.184.215.14", True),
+        ("2606:4700::1111", True),
+        ("127.0.0.1", False),
+        ("10.1.2.3", False),
+        ("172.16.0.1", False),
+        ("192.168.1.1", False),
+        ("169.254.169.254", False),  # cloud metadata
+        ("100.64.0.1", False),  # carrier-grade NAT
+        ("0.0.0.0", False),
+        ("::1", False),
+        ("fd00::1", False),
+        ("fe80::1%eth0", False),
+        ("::ffff:127.0.0.1", False),
+        ("::ffff:10.0.0.1", False),
+    ],
+)
+def test_is_public(ip, public):
+    assert _is_public(ip) is public
```

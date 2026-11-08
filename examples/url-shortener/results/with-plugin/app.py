"""A tiny URL shortener: one Flask process, one SQLite file."""
import codecs
import http.client
import ipaddress
import math
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

from flask import Flask, abort, g, jsonify, redirect, request

DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")
# Max POST /links requests per client IP in each window of RATE_WINDOW seconds.
RATE_LIMIT = int(os.environ.get("SHORTENER_RATE_LIMIT", "10"))
RATE_WINDOW = int(os.environ.get("SHORTENER_RATE_WINDOW", "60"))

# Title previews are fetched once, when a link is created (see fetch_title).
TITLE_FETCH_SECONDS = 3  # socket timeout, and the budget for reading the page body
TITLE_MAX_BYTES = 256 * 1024  # <title> sits in <head>; stop reading after this much
TITLE_MAX_LEN = 300
TITLE_MAX_REDIRECTS = 3
TITLE_USER_AGENT = "url-shortener/1.0 (title preview)"

app = Flask(__name__)


class FixedWindowLimiter:
    """Per-key request counter held in process memory, cleared every `window` seconds.

    Clearing every key at once keeps memory bounded by the number of distinct IPs
    seen in one window. Counts live in this process only: they reset on restart
    and would not be shared if the app ever ran several worker processes.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._lock = threading.Lock()  # the Flask server handles requests on threads
        self.reset()

    def reset(self):
        with self._lock:
            self._start = self.clock()
            self._counts = {}

    def hit(self, key):
        """Record one request for `key`. Return seconds to wait if over the limit, else 0."""
        with self._lock:
            now = self.clock()
            if now - self._start >= self.window:
                self._start = now
                self._counts = {}
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            if count > self.limit:
                return max(1, math.ceil(self._start + self.window - now))
            return 0


link_limiter = FixedWindowLimiter(RATE_LIMIT, RATE_WINDOW)


def is_public_address(ip):
    """True for globally routable IPs. False for loopback, private, link-local (cloud
    metadata), CGNAT/Tailscale, reserved and IPv4-mapped forms of those."""
    return ipaddress.ip_address(ip).is_global


def _connect_public(address, timeout, source_address=None):
    """socket.create_connection that refuses hosts resolving to non-public addresses.

    Checking the address actually dialled, not the URL's hostname, also covers
    redirects and DNS names pointing at internal IPs, and connecting to the checked IP
    (not re-resolving) stops DNS rebinding between the check and the connect.
    """
    host, port = address
    ips = [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    blocked = [ip for ip in ips if not is_public_address(ip)]
    if blocked:
        raise ConnectionRefusedError(f"{host} resolves to non-public address {blocked[0]}")
    for i, ip in enumerate(ips):
        try:
            return socket.create_connection((ip, port), timeout, source_address)
        except OSError:
            if i == len(ips) - 1:
                raise


class _PublicOnly:
    # http.client dials through this per-instance hook; HTTPS then wraps the socket
    # with TLS against the original hostname. test_fetch_title_refuses_loopback fails
    # if a Python upgrade stops honouring it.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPConnection(_PublicOnly, http.client.HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicOnly, http.client.HTTPSConnection):
    pass


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _TitleRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = TITLE_MAX_REDIRECTS


# Assembled by hand: build_opener() would also add a proxy handler (HTTP_PROXY env)
# and an ftp:// handler, both of which would bypass the public-address check.
_title_opener = urllib.request.OpenerDirector()
for _handler in (
    _PublicHTTPHandler(),
    _PublicHTTPSHandler(),
    _TitleRedirectHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(),
    urllib.request.UnknownHandler(),
):
    _title_opener.add_handler(_handler)


class _TitleParser(HTMLParser):
    """Collects the text of the first <title> element. Text inside <script> is skipped."""

    def __init__(self):
        super().__init__()
        self._in_title = False
        self._parts = []
        self.done = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.done:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.done = True

    def handle_data(self, data):
        if self._in_title:
            self._parts.append(data)

    @property
    def title(self):
        if not self.done:
            return None
        return " ".join("".join(self._parts).split())[:TITLE_MAX_LEN] or None


def fetch_title(url):
    """Return the <title> of the HTML page at `url`, or None if it can't be had.

    Best effort: any failure (unreachable, slow, not HTML, HTTP error, non-public
    address, odd markup) gives None and a log line, never an exception.
    """
    deadline = time.monotonic() + TITLE_FETCH_SECONDS
    req = urllib.request.Request(
        url, headers={"User-Agent": TITLE_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    )
    try:
        with _title_opener.open(req, timeout=TITLE_FETCH_SECONDS) as resp:
            if resp.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                return None
            try:
                decoder = codecs.getincrementaldecoder(
                    resp.headers.get_content_charset() or "utf-8"
                )(errors="replace")
            except LookupError:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            parser = _TitleParser()
            received = 0
            # read1 returns whatever has arrived, so a server dripping bytes slowly is
            # cut off at the deadline instead of holding the request open.
            while not parser.done and received < TITLE_MAX_BYTES and time.monotonic() < deadline:
                chunk = resp.read1(16 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                parser.feed(decoder.decode(chunk))
            return parser.title
    except Exception as exc:  # hostile remote input: the title must never break link creation
        app.logger.warning("could not fetch title for %s: %r", url, exc)
        return None


def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    db().execute(
        """CREATE TABLE IF NOT EXISTS links (
               code TEXT PRIMARY KEY,
               url TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               clicks INTEGER NOT NULL DEFAULT 0,
               title TEXT
           )"""
    )
    # Older databases lack later columns. Existing links start at 0 clicks and no title.
    columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
    if "clicks" not in columns:
        db().execute("ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0")
    if "title" not in columns:
        db().execute("ALTER TABLE links ADD COLUMN title TEXT")
    db().commit()


def valid_url(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@app.post("/links")
def create_link():
    # Counts every attempt, invalid ones included, so bad requests can't be used to flood.
    # remote_addr is the TCP peer; behind a reverse proxy it is the proxy's IP (see README).
    retry_after = link_limiter.hit(request.remote_addr)
    if retry_after:
        return (
            jsonify(error="too many links created, try again later"),
            429,
            {"Retry-After": str(retry_after)},
        )
    url = (request.get_json(silent=True) or {}).get("url", "")
    if not valid_url(url):
        return jsonify(error="url must be an http(s) URL"), 400
    # Fetched once here, behind the rate limit, rather than on every GET /links/<code>.
    # Done before touching the DB so no transaction is held open during the fetch.
    title = fetch_title(url)
    code = secrets.token_urlsafe(5)
    db().execute("INSERT INTO links (code, url, title) VALUES (?, ?, ?)", (code, url, title))
    db().commit()
    return jsonify(code=code, short_url=request.host_url + code), 201


@app.get("/links/<code>")
def link_info(code):
    row = db().execute(
        "SELECT code, url, created_at, clicks, title FROM links WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))


@app.get("/<code>")
def follow(code):
    row = db().execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
    if row is None:
        abort(404)
    # Best effort: a failed count write (disk full, DB locked, read-only file) must not
    # break the redirect itself. The click is lost and logged instead; the per-request
    # connection is closed at teardown, which discards the failed write.
    try:
        db().execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
        db().commit()
    except sqlite3.Error:
        app.logger.exception("could not record click for %s", code)
    return redirect(row["url"], code=302)


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(port=5000)

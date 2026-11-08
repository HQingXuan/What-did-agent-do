"""A tiny URL shortener: one Flask process, one SQLite file."""
import http.client
import ipaddress
import math
import os
import re
import secrets
import socket
import sqlite3
import string
import threading
import time
from collections import deque
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

from flask import Flask, abort, g, jsonify, redirect, request

DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")

# POST /links allows this many requests per client IP in any rolling window.
LINK_RATE_LIMIT = 10
LINK_RATE_WINDOW = 60  # seconds

# GET /links/<code> includes the target page's <title>. It is fetched on the
# first view and stored; a link where no title was found is retried later.
TITLE_FETCH_TIMEOUT = 5  # seconds for the whole fetch, redirects included
TITLE_MAX_REDIRECTS = 5
TITLE_MAX_BYTES = 256 * 1024  # how much of the page to read looking for it
TITLE_MAX_LENGTH = 300  # characters kept
TITLE_RETRY_AFTER = 60 * 60  # seconds before retrying a link with no title
TITLE_USER_AGENT = "Mozilla/5.0 (compatible; url-shortener title preview)"

app = Flask(__name__)


class RateLimiter:
    """Sliding-window rate limiter held in process memory.

    This fits the app's one-process deployment. If it ever runs under several
    worker processes, each would keep separate counts and the effective limit
    would multiply, so the state would need to move somewhere shared.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits = {}  # key -> deque of request timestamps, oldest first
        self._lock = threading.Lock()  # the Flask server is threaded
        self._last_sweep = clock()

    def hit(self, key):
        """Record a request from `key`.

        Returns 0 if the request is allowed, otherwise the number of seconds
        until it would be. Rejected requests are not recorded.
        """
        now = self.clock()
        cutoff = now - self.window
        with self._lock:
            if now - self._last_sweep >= self.window:
                self._sweep(cutoff)
                self._last_sweep = now
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return hits[0] - cutoff
            hits.append(now)
            return 0

    def _sweep(self, cutoff):
        # Forget clients with no recent requests so memory stays bounded.
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


link_limiter = RateLimiter(LINK_RATE_LIMIT, LINK_RATE_WINDOW)


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
               title TEXT,
               title_fetched_at TEXT
           )"""
    )
    # Databases created by earlier versions lack the newer columns.
    columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
    for name, definition in [
        ("clicks", "INTEGER NOT NULL DEFAULT 0"),
        ("title", "TEXT"),
        ("title_fetched_at", "TEXT"),  # NULL until a fetch has been tried
    ]:
        if name not in columns:
            db().execute(f"ALTER TABLE links ADD COLUMN {name} {definition}")
    db().commit()


def valid_url(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# --- Fetching page titles ---------------------------------------------------
#
# The server fetches URLs that anyone can submit, so without a guard a link
# could make it request localhost, the private network, or a cloud metadata
# endpoint. Every connection, including each redirect hop, goes through
# _connect_public, which only connects to public addresses.


class BlockedAddress(OSError):
    pass


def _is_public(ip):
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None):  # ::ffff:127.0.0.1 is still loopback
        addr = addr.ipv4_mapped
    return addr.is_global


def _connect_public(host, port, timeout):
    # Resolve once and connect to the vetted address, so DNS can't hand back a
    # private address between the check and the connection.
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for *_, sockaddr in infos:
        if not _is_public(sockaddr[0]):
            raise BlockedAddress(f"{host} resolves to non-public address {sockaddr[0]}")
    error = OSError(f"no addresses for {host}")
    for family, type_, proto, _, sockaddr in infos:
        sock = socket.socket(family, type_, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            error = exc
    raise error


class _PublicHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection, _PublicHTTPConnection):
    # HTTPSConnection.connect opens the TCP connection via super().connect()
    # and then does the TLS handshake. With these bases, that super() call is
    # _PublicHTTPConnection.connect, so HTTPS gets the same guard.
    pass


class _TitleParser(HTMLParser):
    """Collects the text of the document's first <title>.

    <title> inside <svg> labels a graphic, not the page, so it's skipped.
    """

    def __init__(self):
        super().__init__()
        self.parts = []
        self._in_title = False
        self._done = False
        self._svg_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "svg":
            self._svg_depth += 1
        elif tag == "title" and not self._svg_depth and not self._done:
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
        elif tag == "title" and self._in_title:
            self._in_title = False
            self._done = True

    def handle_data(self, data):
        if self._in_title:
            self.parts.append(data)


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.I)
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_REDIRECTS = (301, 302, 303, 307, 308)


def fetch_title(url):
    """Return the <title> of the HTML page at `url`, or None if there isn't
    one to be had (unreachable, blocked address, error status, not HTML...)."""
    try:
        return _fetch_title(url)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        app.logger.info("no title for %s: %s", url, exc)
        return None


def _fetch_title(url):
    deadline = time.monotonic() + TITLE_FETCH_TIMEOUT
    for _ in range(TITLE_MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("title fetch took too long")
        if parts.scheme == "https":
            conn = _PublicHTTPSConnection(parts.hostname, parts.port or 443, timeout=remaining)
        else:
            conn = _PublicHTTPConnection(parts.hostname, parts.port or 80, timeout=remaining)
        try:
            # Percent-encode anything that isn't printable ASCII (e.g. "/café"),
            # leaving existing escapes and URL punctuation alone.
            target = quote(urlunsplit(("", "", parts.path or "/", parts.query, "")),
                           safe=string.punctuation)
            conn.request("GET", target,
                         headers={"User-Agent": TITLE_USER_AGENT, "Accept": "text/html"})
            res = conn.getresponse()
            location = res.getheader("Location")
            if res.status in _REDIRECTS and location:
                url = urljoin(url, location)
                continue
            if res.status != 200 or res.headers.get_content_type() not in _HTML_TYPES:
                return None
            body = _read_head(res, deadline)
            charset = res.headers.get_content_charset()
        finally:
            conn.close()
        return _parse_title(body, charset)
    return None  # too many redirects


def _read_head(res, deadline):
    """Read the body up to the end of its <title>, within the size and time limits."""
    body = bytearray()
    while len(body) < TITLE_MAX_BYTES and time.monotonic() < deadline:
        chunk = res.read1(TITLE_MAX_BYTES - len(body))
        if not chunk:
            break
        overlap = max(len(body) - len(b"</title"), 0)  # in case a read split the tag
        body += chunk
        if b"</title" in body[overlap:].lower():
            break
    return bytes(body)


def _parse_title(body, charset):
    if not charset:
        # Browsers look for a <meta> charset in the first 1024 bytes.
        match = _META_CHARSET.search(body[:1024])
        charset = match.group(1).decode("ascii") if match else "utf-8"
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:  # a charset Python doesn't know
        text = body.decode("utf-8", errors="replace")
    parser = _TitleParser()
    parser.feed(text)
    parser.close()
    title = " ".join("".join(parser.parts).split())
    return title[:TITLE_MAX_LENGTH] or None


@app.post("/links")
def create_link():
    # remote_addr is the TCP peer. If a reverse proxy is ever put in front,
    # every client will share the proxy's address; see werkzeug's ProxyFix.
    retry_after = link_limiter.hit(request.remote_addr)
    if retry_after:
        res = jsonify(error="too many requests, try again later")
        res.status_code = 429
        res.headers["Retry-After"] = str(math.ceil(retry_after))
        return res
    url = (request.get_json(silent=True) or {}).get("url", "")
    if not valid_url(url):
        return jsonify(error="url must be an http(s) URL"), 400
    code = secrets.token_urlsafe(5)
    db().execute("INSERT INTO links (code, url) VALUES (?, ?)", (code, url))
    db().commit()
    return jsonify(code=code, short_url=request.host_url + code), 201


@app.get("/links/<code>")
def link_info(code):
    row = db().execute(
        """SELECT code, url, created_at, clicks, title,
                  title_fetched_at IS NULL
                  OR (title IS NULL AND title_fetched_at <= datetime('now', ?)) AS title_due
           FROM links WHERE code = ?""",
        (f"-{TITLE_RETRY_AFTER} seconds", code),
    ).fetchone()
    if row is None:
        abort(404)
    info = {key: row[key] for key in ("code", "url", "created_at", "clicks", "title")}
    if row["title_due"]:
        info["title"] = fetch_title(row["url"])
        db().execute(
            "UPDATE links SET title = ?, title_fetched_at = CURRENT_TIMESTAMP WHERE code = ?",
            (info["title"], code),
        )
        db().commit()
    return jsonify(info)


@app.get("/<code>")
def follow(code):
    row = db().execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
    if row is None:
        abort(404)
    # Incrementing in SQL keeps concurrent redirects from losing counts.
    db().execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
    db().commit()
    return redirect(row["url"], code=302)


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(port=5000)

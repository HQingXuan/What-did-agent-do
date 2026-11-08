import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app as app_module
from app import (
    LINK_RATE_LIMIT,
    LINK_RATE_WINDOW,
    TITLE_MAX_LENGTH,
    TITLE_MAX_REDIRECTS,
    TITLE_RETRY_AFTER,
    RateLimiter,
    _is_public,
    app,
    fetch_title,
    init_db,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeTitles:
    """Stands in for fetch_title: returns titles from a dict, records each fetch."""

    def __init__(self):
        self.titles = {}
        self.fetched = []

    def __call__(self, url):
        self.fetched.append(url)
        return self.titles.get(url)


@pytest.fixture(autouse=True)
def titles(monkeypatch):
    # Keeps endpoint tests off the network. Tests of the real fetcher call the
    # fetch_title imported above, which this doesn't replace.
    fake = FakeTitles()
    monkeypatch.setattr(app_module, "fetch_title", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    # Fresh limiter per test so request counts don't leak between tests.
    clock = FakeClock()
    monkeypatch.setattr(
        app_module, "link_limiter", RateLimiter(LINK_RATE_LIMIT, LINK_RATE_WINDOW, clock=clock)
    )
    return clock


@pytest.fixture
def client(tmp_path, clock):
    app.config["DB_PATH"] = str(tmp_path / "test.db")
    with app.app_context():
        init_db()
    return app.test_client()


def post_link(client, ip="203.0.113.1", url="https://example.com/"):
    return client.post("/links", json={"url": url}, environ_base={"REMOTE_ADDR": ip})


def test_create_and_follow(client):
    res = client.post("/links", json={"url": "https://example.com/page"})
    assert res.status_code == 201
    code = res.get_json()["code"]
    follow = client.get(f"/{code}")
    assert follow.status_code == 302
    assert follow.headers["Location"] == "https://example.com/page"


def test_rejects_bad_url(client):
    assert client.post("/links", json={"url": "ftp://x"}).status_code == 400


def test_unknown_code(client):
    assert client.get("/nope").status_code == 404
    assert client.get("/links/nope").status_code == 404


def test_rate_limit_blocks_after_limit(client):
    for _ in range(LINK_RATE_LIMIT):
        assert post_link(client).status_code == 201
    res = post_link(client)
    assert res.status_code == 429
    assert res.get_json() == {"error": "too many requests, try again later"}
    assert res.headers["Retry-After"] == str(LINK_RATE_WINDOW)


def test_rate_limit_is_per_ip(client):
    for _ in range(LINK_RATE_LIMIT):
        post_link(client, ip="203.0.113.1")
    assert post_link(client, ip="203.0.113.1").status_code == 429
    assert post_link(client, ip="203.0.113.2").status_code == 201


def test_rate_limit_counts_invalid_requests(client):
    for _ in range(LINK_RATE_LIMIT):
        assert post_link(client, url="ftp://x").status_code == 400
    assert post_link(client).status_code == 429


def test_rate_limit_window_slides(client, clock):
    post_link(client)  # t=0
    clock.now += 30
    for _ in range(LINK_RATE_LIMIT - 1):
        post_link(client)  # t=30
    blocked = post_link(client)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "30"  # until the t=0 request expires

    clock.now += 30  # t=60: only the t=0 request has aged out
    assert post_link(client).status_code == 201
    assert post_link(client).status_code == 429


def test_rate_limit_only_applies_to_creating_links(client):
    code = post_link(client).get_json()["code"]
    for _ in range(LINK_RATE_LIMIT):
        post_link(client)
    assert post_link(client).status_code == 429
    assert client.get(f"/{code}", environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 302
    assert client.get(f"/links/{code}", environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 200


def test_limiter_forgets_idle_clients():
    clock = FakeClock()
    limiter = RateLimiter(limit=2, window=60, clock=clock)
    limiter.hit("a")
    clock.now += 61
    limiter.hit("b")  # triggers a sweep; "a" is stale
    assert set(limiter._hits) == {"b"}


def test_clicks_counted_per_link(client):
    a = post_link(client).get_json()["code"]
    b = post_link(client).get_json()["code"]
    assert client.get(f"/links/{a}").get_json()["clicks"] == 0
    for _ in range(3):
        client.get(f"/{a}")
    client.get(f"/{b}")
    assert client.get(f"/links/{a}").get_json()["clicks"] == 3
    assert client.get(f"/links/{b}").get_json()["clicks"] == 1


def test_viewing_link_info_is_not_a_click(client):
    code = post_link(client).get_json()["code"]
    client.get(f"/links/{code}")
    assert client.get(f"/links/{code}").get_json()["clicks"] == 0


def test_init_db_adds_clicks_to_existing_database(tmp_path, clock):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE links (
               code TEXT PRIMARY KEY,
               url TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.execute("INSERT INTO links (code, url) VALUES ('old', 'https://example.com/')")
    conn.commit()
    conn.close()

    app.config["DB_PATH"] = str(path)
    with app.app_context():
        init_db()
        init_db()  # safe to run again once migrated
    client = app.test_client()
    assert client.get("/links/old").get_json()["clicks"] == 0
    assert client.get("/old").status_code == 302
    assert client.get("/links/old").get_json()["clicks"] == 1


# --- Title preview: GET /links/<code> ---------------------------------------


def backdate_title_fetch(code, seconds):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute(
        "UPDATE links SET title_fetched_at = datetime(title_fetched_at, ?) WHERE code = ?",
        (f"-{seconds} seconds", code),
    )
    conn.commit()
    conn.close()


def test_link_info_includes_title(client, titles):
    titles.titles["https://example.com/"] = "Example Domain"
    code = post_link(client).get_json()["code"]
    info = client.get(f"/links/{code}").get_json()
    assert info["title"] == "Example Domain"
    assert set(info) == {"code", "url", "created_at", "clicks", "title"}


def test_creating_link_does_not_fetch_title(client, titles):
    post_link(client)
    assert titles.fetched == []


def test_title_is_fetched_once(client, titles):
    titles.titles["https://example.com/"] = "Example Domain"
    code = post_link(client).get_json()["code"]
    client.get(f"/links/{code}")
    titles.titles["https://example.com/"] = "Changed"
    backdate_title_fetch(code, TITLE_RETRY_AFTER + 1)  # a found title never goes stale
    assert client.get(f"/links/{code}").get_json()["title"] == "Example Domain"
    assert titles.fetched == ["https://example.com/"]


def test_missing_title_is_null_and_retried_later(client, titles):
    code = post_link(client).get_json()["code"]
    assert client.get(f"/links/{code}").get_json()["title"] is None
    assert client.get(f"/links/{code}").get_json()["title"] is None
    assert len(titles.fetched) == 1  # not refetched on every view

    titles.titles["https://example.com/"] = "Back Up"
    backdate_title_fetch(code, TITLE_RETRY_AFTER - 60)
    assert client.get(f"/links/{code}").get_json()["title"] is None
    backdate_title_fetch(code, 120)
    assert client.get(f"/links/{code}").get_json()["title"] == "Back Up"
    assert len(titles.fetched) == 2


def test_init_db_adds_title_to_existing_database(tmp_path, clock, titles):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE links (
               code TEXT PRIMARY KEY,
               url TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               clicks INTEGER NOT NULL DEFAULT 0
           )"""
    )
    conn.execute("INSERT INTO links (code, url, clicks) VALUES ('old', 'https://example.com/', 4)")
    conn.commit()
    conn.close()

    titles.titles["https://example.com/"] = "Example Domain"
    app.config["DB_PATH"] = str(path)
    with app.app_context():
        init_db()
        init_db()  # safe to run again once migrated
    info = app.test_client().get("/links/old").get_json()
    assert info["title"] == "Example Domain"
    assert info["clicks"] == 4


# --- Title preview: fetching pages ------------------------------------------


class Site:
    """A local web server serving pages that tests define."""

    def __init__(self):
        self.pages = {}  # path -> (status, headers, body, delay)
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.requests.append(self.path)
                status, headers, body, delay = site.pages.get(self.path, (404, {}, b"", 0))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def page(self, path, body, content_type="text/html; charset=utf-8", status=200, delay=0):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.pages[path] = (status, {"Content-Type": content_type}, body, delay)

    def redirect(self, path, location, status=302):
        self.pages[path] = (status, {"Location": location}, b"", 0)


@pytest.fixture
def site():
    site = Site()
    yield site
    site.server.shutdown()
    site.server.server_close()


@pytest.fixture
def allow_loopback(monkeypatch):
    # The test site runs on 127.0.0.1, which the fetcher normally refuses.
    monkeypatch.setattr(app_module, "_is_public", lambda ip: True)


def test_fetch_title_reads_page_title(site, allow_loopback):
    site.page("/", "<html><head><title>\n  Fish &amp; Chips\n  Shop </title></head></html>")
    assert fetch_title(site.url("/")) == "Fish & Chips Shop"


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no title here</body></html>",
        "<title>   </title>",
        "<body><svg><title>Close icon</title></svg></body>",
    ],
)
def test_fetch_title_none_when_page_has_no_title(site, allow_loopback, html):
    site.page("/", html)
    assert fetch_title(site.url("/")) is None


def test_fetch_title_ignores_svg_titles(site, allow_loopback):
    site.page("/", "<svg><title>Logo</title></svg><title>The Page</title>")
    assert fetch_title(site.url("/")) == "The Page"


def test_fetch_title_ignores_non_html(site, allow_loopback):
    site.page("/doc", "<title>Not really</title>", content_type="application/pdf")
    assert fetch_title(site.url("/doc")) is None


def test_fetch_title_ignores_error_pages(site, allow_loopback):
    site.page("/gone", "<title>404 Not Found</title>", status=404)
    assert fetch_title(site.url("/gone")) is None


@pytest.mark.parametrize(
    "content_type, html",
    [
        ("text/html; charset=iso-8859-1", "<title>Café</title>"),
        ("text/html", '<meta charset="iso-8859-1"><title>Café</title>'),
        ("text/html", '<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
                      "<title>Café</title>"),
    ],
)
def test_fetch_title_uses_declared_charset(site, allow_loopback, content_type, html):
    site.page("/", html.encode("latin-1"), content_type=content_type)
    assert fetch_title(site.url("/")) == "Café"


def test_fetch_title_defaults_to_utf8(site, allow_loopback):
    site.page("/", "<title>Café ☕</title>".encode("utf-8"), content_type="text/html")
    assert fetch_title(site.url("/")) == "Café ☕"


def test_fetch_title_encodes_non_ascii_paths(site, allow_loopback):
    site.page("/caf%C3%A9?q=a%20b", "<title>Found</title>")
    assert fetch_title(site.url("/café?q=a%20b")) == "Found"


def test_fetch_title_truncates_long_titles(site, allow_loopback):
    site.page("/", "<title>" + "x" * (TITLE_MAX_LENGTH + 50) + "</title>")
    assert fetch_title(site.url("/")) == "x" * TITLE_MAX_LENGTH


def test_fetch_title_reads_a_bounded_amount(site, allow_loopback, monkeypatch):
    monkeypatch.setattr(app_module, "TITLE_MAX_BYTES", 1000)
    site.page("/", "<!--" + "x" * 2000 + "--><title>Too far down</title>")
    assert fetch_title(site.url("/")) is None


def test_fetch_title_follows_redirects(site, allow_loopback):
    site.redirect("/old", "/new", status=301)
    site.page("/new", "<title>New Home</title>")
    assert fetch_title(site.url("/old")) == "New Home"
    assert site.requests == ["/old", "/new"]


def test_fetch_title_gives_up_on_redirect_loops(site, allow_loopback):
    site.redirect("/loop", "/loop")
    assert fetch_title(site.url("/loop")) is None
    assert len(site.requests) == TITLE_MAX_REDIRECTS + 1


def test_fetch_title_only_follows_redirects_to_http(site, allow_loopback):
    site.redirect("/", "file:///etc/passwd")
    assert fetch_title(site.url("/")) is None


def test_fetch_title_times_out(site, allow_loopback, monkeypatch):
    monkeypatch.setattr(app_module, "TITLE_FETCH_TIMEOUT", 0.3)
    site.page("/slow", "<title>Eventually</title>", delay=2)
    start = time.monotonic()
    assert fetch_title(site.url("/slow")) is None
    assert time.monotonic() - start < 1.5


def test_fetch_title_handles_unreachable_hosts(allow_loopback):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert fetch_title(f"http://127.0.0.1:{port}/") is None  # nothing listening


def test_fetch_title_refuses_private_addresses(site):
    site.page("/", "<title>Internal</title>")
    assert fetch_title(site.url("/")) is None
    assert site.requests == []  # never connected


def test_fetch_title_checks_every_redirect_hop(site, monkeypatch):
    # The first host is treated as public; where it redirects to is not.
    verdicts = iter([True, False])
    monkeypatch.setattr(app_module, "_is_public", lambda ip: next(verdicts))
    site.redirect("/", "/internal")
    site.page("/internal", "<title>Internal</title>")
    assert fetch_title(site.url("/")) is None
    assert site.requests == ["/"]


@pytest.mark.parametrize(
    "ip, public",
    [
        ("93.184.215.14", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("0.0.0.0", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_is_public(ip, public):
    assert _is_public(ip) is public

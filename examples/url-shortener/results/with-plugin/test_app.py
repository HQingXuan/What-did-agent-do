import socket
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import app as shortener
from app import FixedWindowLimiter, app, fetch_title, init_db, is_public_address, link_limiter


@pytest.fixture(autouse=True)
def fetched_urls(monkeypatch):
    """Keep tests off the network: link creation sees no title unless a test says otherwise.

    Only shortener.fetch_title (what create_link calls) is replaced; tests of the real
    fetcher call the `fetch_title` imported above.
    """
    urls = []
    monkeypatch.setattr(shortener, "fetch_title", lambda url: urls.append(url))
    return urls


@pytest.fixture
def site():
    """Local web server. Set site.pages[path] to (status, headers, body) or a callable
    taking the handler; site.hits lists the paths requested, in order."""
    pages, hits = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            page = pages.get(self.path, (404, {"Content-Type": "text/html"}, b"<title>404</title>"))
            if callable(page):
                return page(self)
            status, headers, body = page
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", pages=pages, hits=hits)
    server.shutdown()
    server.server_close()


@pytest.fixture
def allow_loopback(monkeypatch):
    """Let the fetcher reach the local test server, which the address check would refuse."""
    monkeypatch.setattr(shortener, "is_public_address", lambda ip: ip == "127.0.0.1")


def html(body, content_type="text/html; charset=utf-8"):
    return (200, {"Content-Type": content_type}, body)


@pytest.fixture
def client(tmp_path):
    app.config["DB_PATH"] = str(tmp_path / "test.db")
    with app.app_context():
        init_db()
    link_limiter.reset()
    return app.test_client()


def post_link(client, ip="10.0.0.1", url="https://example.com/"):
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


def test_rate_limit_blocks_after_limit(client, monkeypatch):
    monkeypatch.setattr(link_limiter, "limit", 2)
    assert post_link(client).status_code == 201
    assert post_link(client).status_code == 201
    res = post_link(client)
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    assert "error" in res.get_json()


def test_rate_limit_is_per_ip(client, monkeypatch):
    monkeypatch.setattr(link_limiter, "limit", 1)
    assert post_link(client, ip="10.0.0.1").status_code == 201
    assert post_link(client, ip="10.0.0.1").status_code == 429
    assert post_link(client, ip="10.0.0.2").status_code == 201


def test_rate_limit_counts_invalid_requests(client, monkeypatch):
    monkeypatch.setattr(link_limiter, "limit", 1)
    assert post_link(client, url="ftp://x").status_code == 400
    assert post_link(client).status_code == 429


def test_rate_limit_does_not_affect_reads(client, monkeypatch):
    monkeypatch.setattr(link_limiter, "limit", 1)
    code = post_link(client).get_json()["code"]
    assert post_link(client).status_code == 429
    assert client.get(f"/{code}", environ_base={"REMOTE_ADDR": "10.0.0.1"}).status_code == 302


def test_limiter_window_resets():
    now = [1000.0]
    limiter = FixedWindowLimiter(limit=1, window=60, clock=lambda: now[0])
    assert limiter.hit("a") == 0
    now[0] += 20
    assert limiter.hit("a") == 40  # seconds until the window ends
    now[0] += 40
    assert limiter.hit("a") == 0


def test_clicks_counted_in_link_info(client):
    code = post_link(client).get_json()["code"]
    assert client.get(f"/links/{code}").get_json()["clicks"] == 0
    for _ in range(3):
        assert client.get(f"/{code}").status_code == 302
    info = client.get(f"/links/{code}").get_json()
    assert info["clicks"] == 3
    assert info["url"] == "https://example.com/"


def test_clicks_are_per_link(client):
    a = post_link(client).get_json()["code"]
    b = post_link(client).get_json()["code"]
    client.get(f"/{a}")
    client.get(f"/{a}")
    client.get(f"/{b}")
    assert client.get(f"/links/{a}").get_json()["clicks"] == 2
    assert client.get(f"/links/{b}").get_json()["clicks"] == 1


def test_link_info_does_not_count_as_click(client):
    code = post_link(client).get_json()["code"]
    client.get(f"/links/{code}")
    assert client.get(f"/links/{code}").get_json()["clicks"] == 0


def test_redirect_survives_failed_click_write(client, tmp_path, monkeypatch, caplog):
    code = post_link(client).get_json()["code"]
    # Open the DB read-only so the click write fails while reads work (like a full disk).
    real_connect = sqlite3.connect
    monkeypatch.setitem(app.config, "DB_PATH", f"file:{tmp_path / 'test.db'}?mode=ro")
    monkeypatch.setattr(sqlite3, "connect", lambda path, **kw: real_connect(path, uri=True, **kw))
    res = client.get(f"/{code}")
    monkeypatch.undo()
    assert res.status_code == 302
    assert res.headers["Location"] == "https://example.com/"
    assert "could not record click" in caplog.text
    assert client.get(f"/links/{code}").get_json()["clicks"] == 0


def test_init_db_upgrades_existing_db(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE links (code TEXT PRIMARY KEY, url TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    old.execute("INSERT INTO links (code, url) VALUES ('old1', 'https://example.com/old')")
    old.commit()
    old.close()
    app.config["DB_PATH"] = str(path)
    with app.app_context():
        init_db()
        init_db()  # idempotent on an already-migrated DB
    client = app.test_client()
    info = client.get("/links/old1").get_json()
    assert info["clicks"] == 0
    assert info["title"] is None  # old links are not backfilled
    assert client.get("/old1").status_code == 302
    assert client.get("/links/old1").get_json()["clicks"] == 1


# --- Title previews: storage and API ---


def test_link_info_includes_title(client, monkeypatch):
    seen = []
    monkeypatch.setattr(shortener, "fetch_title", lambda url: seen.append(url) or "Example Domain")
    code = post_link(client, url="https://example.com/page").get_json()["code"]
    assert seen == ["https://example.com/page"]
    assert client.get(f"/links/{code}").get_json()["title"] == "Example Domain"


def test_link_created_without_title_when_unavailable(client):
    res = post_link(client)
    assert res.status_code == 201
    assert client.get(f"/links/{res.get_json()['code']}").get_json()["title"] is None


def test_title_fetched_only_at_creation(client, fetched_urls):
    code = post_link(client).get_json()["code"]
    client.get(f"/links/{code}")
    client.get(f"/{code}")
    assert fetched_urls == ["https://example.com/"]


def test_title_not_fetched_for_rejected_requests(client, fetched_urls, monkeypatch):
    monkeypatch.setattr(link_limiter, "limit", 1)
    post_link(client, url="ftp://x")  # invalid, and uses up the limit
    post_link(client)  # rate limited
    assert fetched_urls == []


# --- Title previews: the fetcher ---


def test_fetch_title_extracts_first_title(site, allow_loopback):
    site.pages["/"] = html(
        b"<html><head><script>var s = '<title>fake</title>';</script>"
        b"<title>\n   Tom &amp; Jerry \n</title></head><body><title>later</title></body></html>"
    )
    assert fetch_title(site.url + "/") == "Tom & Jerry"


def test_fetch_title_uses_header_charset(site, allow_loopback):
    site.pages["/"] = html("<title>Café</title>".encode("latin-1"), "text/html; charset=iso-8859-1")
    assert fetch_title(site.url + "/") == "Café"


def test_fetch_title_truncates_long_titles(site, allow_loopback):
    site.pages["/"] = html(b"<title>" + b"x" * 5000 + b"</title>")
    assert fetch_title(site.url + "/") == "x" * shortener.TITLE_MAX_LEN


@pytest.mark.parametrize(
    "page",
    [
        (200, {"Content-Type": "application/pdf"}, b"<title>not html</title>"),
        (404, {"Content-Type": "text/html"}, b"<title>Not Found</title>"),
        html(b"<html><head></head><body>no title</body></html>"),
        html(b"<title>   </title>"),
        # Recent Pythons (3.12.15 included) parse <title> as RCDATA, so this text is never
        # emitted; older ones emit it early, which _TitleParser.done guards against.
        html(b"<title>never closed<body><p>whole page</p></body>"),
    ],
    ids=["not-html", "http-error", "no-title", "blank-title", "unclosed"],
)
def test_fetch_title_none_when_no_usable_title(site, allow_loopback, page):
    site.pages["/"] = page
    assert fetch_title(site.url + "/") is None


def test_fetch_title_follows_redirects(site, allow_loopback):
    site.pages["/old"] = (301, {"Location": "/new"}, b"")
    site.pages["/new"] = html(b"<title>Moved here</title>")
    assert fetch_title(site.url + "/old") == "Moved here"


@pytest.mark.parametrize("hops, expected", [(3, "end"), (4, None)])
def test_fetch_title_caps_redirects(site, allow_loopback, hops, expected):
    for i in range(hops):
        site.pages[f"/r{i}"] = (302, {"Location": f"/r{i + 1}"}, b"")
    site.pages[f"/r{hops}"] = html(b"<title>end</title>")
    assert fetch_title(site.url + "/r0") == expected


def test_fetch_title_gives_up_at_deadline(site, allow_loopback, monkeypatch):
    monkeypatch.setattr(shortener, "TITLE_FETCH_SECONDS", 0.5)

    def drip(handler):  # sends a byte every 0.1 s, never reaching a title in time
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html")
        handler.end_headers()
        try:
            for _ in range(50):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.1)
            handler.wfile.write(b"<title>too late</title>")
        except OSError:
            pass  # the fetcher hung up, as intended

    site.pages["/"] = drip
    start = time.monotonic()
    assert fetch_title(site.url + "/") is None
    assert time.monotonic() - start < 2


def test_fetch_title_unreachable_host_logs(allow_loopback, caplog):
    with socket.socket() as s:  # grab a free port, then close it so nothing listens there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert fetch_title(f"http://127.0.0.1:{port}/") is None
    assert "could not fetch title" in caplog.text


# --- Title previews: refusing internal addresses (SSRF) ---


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254",
     "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1"],
)
def test_internal_addresses_not_public(ip):
    assert not is_public_address(ip)


@pytest.mark.parametrize("ip", ["93.184.215.14", "1.1.1.1", "2606:4700:4700::1111"])
def test_internet_addresses_public(ip):
    assert is_public_address(ip)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_fetch_title_refuses_loopback(site, host, caplog):
    site.pages["/"] = html(b"<title>internal admin</title>")
    port = site.url.rsplit(":", 1)[1]
    assert fetch_title(f"http://{host}:{port}/") is None
    assert site.hits == []  # never connected, so nothing internal was requested
    assert "non-public address" in caplog.text


def test_fetch_title_rechecks_address_on_redirect(site, monkeypatch):
    # The first hop passes the check; the redirect hop is refused before connecting.
    checked = []
    monkeypatch.setattr(shortener, "is_public_address", lambda ip: checked.append(ip) or len(checked) == 1)
    site.pages["/start"] = (302, {"Location": "/secret"}, b"")
    site.pages["/secret"] = html(b"<title>internal admin</title>")
    assert fetch_title(site.url + "/start") is None
    assert site.hits == ["/start"]
    assert len(checked) == 2


def test_title_opener_only_dials_through_address_check():
    # urllib.request.build_opener() would add ftp:// (reachable via redirect) and file://
    # handlers, plus a ProxyHandler when HTTP_PROXY is set. All of those connect without
    # the public-address check, so the opener must contain nothing but the guarded handlers.
    handlers = {
        scheme: [type(h) for h in hs] for scheme, hs in shortener._title_opener.handle_open.items()
    }
    assert handlers == {
        "http": [shortener._PublicHTTPHandler],
        "https": [shortener._PublicHTTPSHandler],
        "unknown": [urllib.request.UnknownHandler],
    }

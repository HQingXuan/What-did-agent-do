# task-1

**Prompt:** Add rate limiting per IP to POST /links.

**Reply:**

POST /links is now limited per client IP. After 10 requests in 60 seconds, the same IP gets `429` with a JSON error and a `Retry-After` header. The other endpoints aren't limited.

I checked it two ways: the test suite passes (8 tests: the 3 existing plus 5 new), and I ran the app and sent real requests. Against a live server I got ten 201s, then 429 with `Retry-After: 59`, and reads still worked.

**What changed**
- **`app.py`**: added a small `FixedWindowLimiter` class (about 25 lines, no new packages) and a check at the top of `create_link`. Invalid requests count toward the limit too, so a client can't flood with bad URLs. You can change the limit and window with `SHORTENER_RATE_LIMIT` and `SHORTENER_RATE_WINDOW`, the same env-var style as `SHORTENER_DB`.
- **`test_app.py`**: new tests cover blocking after the limit, separate counts per IP, invalid requests counting, reads not being limited, and the window resetting. The test setup now clears the limiter between tests.
- **`README.md`**: documents the limit, the 429 response, and the caveats below.
- **`docs/decisions.md`**: new file with entries D-001 and D-002.

Decision: in-process fixed-window limiter over Flask-Limiter or Redis, because it's one process and Redis would be another service for one maintainer to run. Cost: counts reset on restart; up to 2× burst at window edges. [new]

Decision: limit by `request.remote_addr` over `X-Forwarded-For`, because without a proxy that header is spoofable and would bypass the limiter. Cost: behind a proxy, all users share one limit. [new]

**Before you deploy**
- **Reverse proxy:** if you put nginx or a CDN in front of the app, every request will look like it comes from the proxy, so all users share 10 creates a minute. The README has the one-line `ProxyFix` change that fixes this.
- **Default limit:** 10 a minute is my guess for a personal shortener, so adjust it with the env vars if needed.
- **More than one worker:** if you ever run several worker processes (e.g. gunicorn), each keeps its own counts. At that point a shared store would make sense.

Something else on this machine was already using ports 5000 and 5077 (another Python app, not this one). My first two live runs failed to start because of that, and the responses I got came from that other app, so I left those processes alone and reran on a free port. Nothing is committed.


**Diff:**

```diff
diff --git a/README.md b/README.md
index b14d42c..51cd1b2 100644
--- a/README.md
+++ b/README.md
@@ -8,6 +8,11 @@ python app.py          # serves on :5000
 python -m pytest -q    # tests
 ```
 
-- `POST /links {"url": ...}` creates a short code.
+- `POST /links {"url": ...}` creates a short code. Limited to 10 requests per 60 s per client IP; over the limit it returns `429` with a `Retry-After` header. Tune with `SHORTENER_RATE_LIMIT` and `SHORTENER_RATE_WINDOW` (seconds).
 - `GET /<code>` redirects.
 - `GET /links/<code>` returns the link's details.
+
+## Rate limiting notes
+
+- Counts are kept in process memory: a restart clears them, and running more than one worker process would give each its own counts.
+- The client IP is the TCP peer address (`request.remote_addr`). If you put a reverse proxy such as nginx in front, every request will appear to come from the proxy and share one limit. In that case wrap the app with `werkzeug.middleware.proxy_fix.ProxyFix(app.wsgi_app, x_for=1)` so the proxy's `X-Forwarded-For` is used.
diff --git a/app.py b/app.py
index f68f48a..3694cd2 100644
--- a/app.py
+++ b/app.py
@@ -1,16 +1,59 @@
 """A tiny URL shortener: one Flask process, one SQLite file."""
+import math
 import os
 import secrets
 import sqlite3
+import threading
+import time
 from urllib.parse import urlparse
 
 from flask import Flask, abort, g, jsonify, redirect, request
 
 DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")
+# Max POST /links requests per client IP in each window of RATE_WINDOW seconds.
+RATE_LIMIT = int(os.environ.get("SHORTENER_RATE_LIMIT", "10"))
+RATE_WINDOW = int(os.environ.get("SHORTENER_RATE_WINDOW", "60"))
 
 app = Flask(__name__)
 
 
+class FixedWindowLimiter:
+    """Per-key request counter held in process memory, cleared every `window` seconds.
+
+    Clearing every key at once keeps memory bounded by the number of distinct IPs
+    seen in one window. Counts live in this process only: they reset on restart
+    and would not be shared if the app ever ran several worker processes.
+    """
+
+    def __init__(self, limit, window, clock=time.monotonic):
+        self.limit = limit
+        self.window = window
+        self.clock = clock
+        self._lock = threading.Lock()  # the Flask server handles requests on threads
+        self.reset()
+
+    def reset(self):
+        with self._lock:
+            self._start = self.clock()
+            self._counts = {}
+
+    def hit(self, key):
+        """Record one request for `key`. Return seconds to wait if over the limit, else 0."""
+        with self._lock:
+            now = self.clock()
+            if now - self._start >= self.window:
+                self._start = now
+                self._counts = {}
+            count = self._counts.get(key, 0) + 1
+            self._counts[key] = count
+            if count > self.limit:
+                return max(1, math.ceil(self._start + self.window - now))
+            return 0
+
+
+link_limiter = FixedWindowLimiter(RATE_LIMIT, RATE_WINDOW)
+
+
 def db():
     if "db" not in g:
         g.db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
@@ -43,6 +86,15 @@ def valid_url(url):
 
 @app.post("/links")
 def create_link():
+    # Counts every attempt, invalid ones included, so bad requests can't be used to flood.
+    # remote_addr is the TCP peer; behind a reverse proxy it is the proxy's IP (see README).
+    retry_after = link_limiter.hit(request.remote_addr)
+    if retry_after:
+        return (
+            jsonify(error="too many links created, try again later"),
+            429,
+            {"Retry-After": str(retry_after)},
+        )
     url = (request.get_json(silent=True) or {}).get("url", "")
     if not valid_url(url):
         return jsonify(error="url must be an http(s) URL"), 400
diff --git a/test_app.py b/test_app.py
index d089b22..e0e7b0f 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,6 +1,6 @@
 import pytest
 
-from app import app, init_db
+from app import FixedWindowLimiter, app, init_db, link_limiter
 
 
 @pytest.fixture
@@ -8,9 +8,14 @@ def client(tmp_path):
     app.config["DB_PATH"] = str(tmp_path / "test.db")
     with app.app_context():
         init_db()
+    link_limiter.reset()
     return app.test_client()
 
 
+def post_link(client, ip="10.0.0.1", url="https://example.com/"):
+    return client.post("/links", json={"url": url}, environ_base={"REMOTE_ADDR": ip})
+
+
 def test_create_and_follow(client):
     res = client.post("/links", json={"url": "https://example.com/page"})
     assert res.status_code == 201
@@ -27,3 +32,43 @@ def test_rejects_bad_url(client):
 def test_unknown_code(client):
     assert client.get("/nope").status_code == 404
     assert client.get("/links/nope").status_code == 404
+
+
+def test_rate_limit_blocks_after_limit(client, monkeypatch):
+    monkeypatch.setattr(link_limiter, "limit", 2)
+    assert post_link(client).status_code == 201
+    assert post_link(client).status_code == 201
+    res = post_link(client)
+    assert res.status_code == 429
+    assert int(res.headers["Retry-After"]) >= 1
+    assert "error" in res.get_json()
+
+
+def test_rate_limit_is_per_ip(client, monkeypatch):
+    monkeypatch.setattr(link_limiter, "limit", 1)
+    assert post_link(client, ip="10.0.0.1").status_code == 201
+    assert post_link(client, ip="10.0.0.1").status_code == 429
+    assert post_link(client, ip="10.0.0.2").status_code == 201
+
+
+def test_rate_limit_counts_invalid_requests(client, monkeypatch):
+    monkeypatch.setattr(link_limiter, "limit", 1)
+    assert post_link(client, url="ftp://x").status_code == 400
+    assert post_link(client).status_code == 429
+
+
+def test_rate_limit_does_not_affect_reads(client, monkeypatch):
+    monkeypatch.setattr(link_limiter, "limit", 1)
+    code = post_link(client).get_json()["code"]
+    assert post_link(client).status_code == 429
+    assert client.get(f"/{code}", environ_base={"REMOTE_ADDR": "10.0.0.1"}).status_code == 302
+
+
+def test_limiter_window_resets():
+    now = [1000.0]
+    limiter = FixedWindowLimiter(limit=1, window=60, clock=lambda: now[0])
+    assert limiter.hit("a") == 0
+    now[0] += 20
+    assert limiter.hit("a") == 40  # seconds until the window ends
+    now[0] += 40
+    assert limiter.hit("a") == 0
```

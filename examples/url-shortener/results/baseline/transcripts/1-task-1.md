# task-1

**Prompt:** Add rate limiting per IP to POST /links.

**Reply:**

`POST /links` is now limited to **10 requests per minute per client IP**. Over the limit, it returns `429` with a JSON error and a `Retry-After` header telling the client how many seconds to wait. The other routes aren't limited.

**How it works** (`app.py`)
- A small `RateLimiter` class keeps each IP's recent request times in memory and counts them over the last 60 seconds. I didn't add a library like Flask-Limiter; since the app runs as one process on one VM, in-memory state works and it's about 40 lines with no new dependency.
- It uses a lock, because `app.run()` handles requests on several threads at once.
- IPs that stop sending requests are cleared out about once a minute, so memory doesn't keep growing.
- Requests with a bad URL count toward the limit too, so nobody can send unlimited requests just by making them invalid.
- The client IP comes from `request.remote_addr`. I didn't use the `X-Forwarded-For` header, because clients can set it to anything and get around the limit.

**Testing**
- I added 6 tests to `test_app.py`: blocking after the limit, separate counts per IP, invalid requests counting, the 60-second window, GET routes not being limited, and idle IPs being cleared. They use a fake clock, so none of them sleep.
- The test fixture gives each test a fresh limiter so counts don't carry over between tests.
- All 9 tests pass. With the limit check removed, 5 of the new tests fail, so they really do test the limiter.
- Against the running server, 15 simultaneous requests from one IP gave exactly 10 successes and 5 `429`s.

I also added a line about the limit to the README.

**Things to know**
- **The 10-per-minute number was my choice.** You didn't give one. It's a constant at the top of `app.py` (`LINK_RATE_LIMIT` and `LINK_RATE_WINDOW`) if you want a different value.
- **Reverse proxy:** if you put nginx or Caddy in front of the app, every request will appear to come from the proxy's IP, so all clients will share one limit. The fix is to wrap the app in Werkzeug's `ProxyFix`, which is a one-liner, and I left a comment about it in the code.
- **Multiple processes:** if you ever run several workers (for example gunicorn with `-w 4`), each one keeps its own counts and the real limit becomes 10 × the number of workers. At that point the counts would need to live somewhere shared, like Redis or the SQLite file.
- **Restarts:** restarting the app resets all counts. For a personal shortener that seems fine.


**Diff:**

```diff
diff --git a/README.md b/README.md
index b14d42c..6c6a8de 100644
--- a/README.md
+++ b/README.md
@@ -8,6 +8,6 @@ python app.py          # serves on :5000
 python -m pytest -q    # tests
 ```
 
-- `POST /links {"url": ...}` creates a short code.
+- `POST /links {"url": ...}` creates a short code. Limited to 10 requests per minute per client IP; beyond that it returns `429` with a `Retry-After` header.
 - `GET /<code>` redirects.
 - `GET /links/<code>` returns the link's details.
diff --git a/app.py b/app.py
index f68f48a..9b3985e 100644
--- a/app.py
+++ b/app.py
@@ -1,16 +1,70 @@
 """A tiny URL shortener: one Flask process, one SQLite file."""
+import math
 import os
 import secrets
 import sqlite3
+import threading
+import time
+from collections import deque
 from urllib.parse import urlparse
 
 from flask import Flask, abort, g, jsonify, redirect, request
 
 DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")
 
+# POST /links allows this many requests per client IP in any rolling window.
+LINK_RATE_LIMIT = 10
+LINK_RATE_WINDOW = 60  # seconds
+
 app = Flask(__name__)
 
 
+class RateLimiter:
+    """Sliding-window rate limiter held in process memory.
+
+    This fits the app's one-process deployment. If it ever runs under several
+    worker processes, each would keep separate counts and the effective limit
+    would multiply, so the state would need to move somewhere shared.
+    """
+
+    def __init__(self, limit, window, clock=time.monotonic):
+        self.limit = limit
+        self.window = window
+        self.clock = clock
+        self._hits = {}  # key -> deque of request timestamps, oldest first
+        self._lock = threading.Lock()  # the Flask server is threaded
+        self._last_sweep = clock()
+
+    def hit(self, key):
+        """Record a request from `key`.
+
+        Returns 0 if the request is allowed, otherwise the number of seconds
+        until it would be. Rejected requests are not recorded.
+        """
+        now = self.clock()
+        cutoff = now - self.window
+        with self._lock:
+            if now - self._last_sweep >= self.window:
+                self._sweep(cutoff)
+                self._last_sweep = now
+            hits = self._hits.setdefault(key, deque())
+            while hits and hits[0] <= cutoff:
+                hits.popleft()
+            if len(hits) >= self.limit:
+                return hits[0] - cutoff
+            hits.append(now)
+            return 0
+
+    def _sweep(self, cutoff):
+        # Forget clients with no recent requests so memory stays bounded.
+        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
+        for key in stale:
+            del self._hits[key]
+
+
+link_limiter = RateLimiter(LINK_RATE_LIMIT, LINK_RATE_WINDOW)
+
+
 def db():
     if "db" not in g:
         g.db = sqlite3.connect(app.config.get("DB_PATH", DB_PATH))
@@ -43,6 +97,14 @@ def valid_url(url):
 
 @app.post("/links")
 def create_link():
+    # remote_addr is the TCP peer. If a reverse proxy is ever put in front,
+    # every client will share the proxy's address; see werkzeug's ProxyFix.
+    retry_after = link_limiter.hit(request.remote_addr)
+    if retry_after:
+        res = jsonify(error="too many requests, try again later")
+        res.status_code = 429
+        res.headers["Retry-After"] = str(math.ceil(retry_after))
+        return res
     url = (request.get_json(silent=True) or {}).get("url", "")
     if not valid_url(url):
         return jsonify(error="url must be an http(s) URL"), 400
diff --git a/test_app.py b/test_app.py
index d089b22..767baf6 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,16 +1,39 @@
 import pytest
 
-from app import app, init_db
+import app as app_module
+from app import LINK_RATE_LIMIT, LINK_RATE_WINDOW, RateLimiter, app, init_db
+
+
+class FakeClock:
+    def __init__(self):
+        self.now = 1000.0
+
+    def __call__(self):
+        return self.now
+
+
+@pytest.fixture
+def clock(monkeypatch):
+    # Fresh limiter per test so request counts don't leak between tests.
+    clock = FakeClock()
+    monkeypatch.setattr(
+        app_module, "link_limiter", RateLimiter(LINK_RATE_LIMIT, LINK_RATE_WINDOW, clock=clock)
+    )
+    return clock
 
 
 @pytest.fixture
-def client(tmp_path):
+def client(tmp_path, clock):
     app.config["DB_PATH"] = str(tmp_path / "test.db")
     with app.app_context():
         init_db()
     return app.test_client()
 
 
+def post_link(client, ip="203.0.113.1", url="https://example.com/"):
+    return client.post("/links", json={"url": url}, environ_base={"REMOTE_ADDR": ip})
+
+
 def test_create_and_follow(client):
     res = client.post("/links", json={"url": "https://example.com/page"})
     assert res.status_code == 201
@@ -27,3 +50,57 @@ def test_rejects_bad_url(client):
 def test_unknown_code(client):
     assert client.get("/nope").status_code == 404
     assert client.get("/links/nope").status_code == 404
+
+
+def test_rate_limit_blocks_after_limit(client):
+    for _ in range(LINK_RATE_LIMIT):
+        assert post_link(client).status_code == 201
+    res = post_link(client)
+    assert res.status_code == 429
+    assert res.get_json() == {"error": "too many requests, try again later"}
+    assert res.headers["Retry-After"] == str(LINK_RATE_WINDOW)
+
+
+def test_rate_limit_is_per_ip(client):
+    for _ in range(LINK_RATE_LIMIT):
+        post_link(client, ip="203.0.113.1")
+    assert post_link(client, ip="203.0.113.1").status_code == 429
+    assert post_link(client, ip="203.0.113.2").status_code == 201
+
+
+def test_rate_limit_counts_invalid_requests(client):
+    for _ in range(LINK_RATE_LIMIT):
+        assert post_link(client, url="ftp://x").status_code == 400
+    assert post_link(client).status_code == 429
+
+
+def test_rate_limit_window_slides(client, clock):
+    post_link(client)  # t=0
+    clock.now += 30
+    for _ in range(LINK_RATE_LIMIT - 1):
+        post_link(client)  # t=30
+    blocked = post_link(client)
+    assert blocked.status_code == 429
+    assert blocked.headers["Retry-After"] == "30"  # until the t=0 request expires
+
+    clock.now += 30  # t=60: only the t=0 request has aged out
+    assert post_link(client).status_code == 201
+    assert post_link(client).status_code == 429
+
+
+def test_rate_limit_only_applies_to_creating_links(client):
+    code = post_link(client).get_json()["code"]
+    for _ in range(LINK_RATE_LIMIT):
+        post_link(client)
+    assert post_link(client).status_code == 429
+    assert client.get(f"/{code}", environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 302
+    assert client.get(f"/links/{code}", environ_base={"REMOTE_ADDR": "203.0.113.1"}).status_code == 200
+
+
+def test_limiter_forgets_idle_clients():
+    clock = FakeClock()
+    limiter = RateLimiter(limit=2, window=60, clock=clock)
+    limiter.hit("a")
+    clock.now += 61
+    limiter.hit("b")  # triggers a sweep; "a" is stale
+    assert set(limiter._hits) == {"b"}
```

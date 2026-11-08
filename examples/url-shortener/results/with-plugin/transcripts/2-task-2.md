# task-2

**Prompt:** Count clicks per link and include the count in GET /links/<code>.

**Reply:**

`GET /<code>` now adds one to the link's click count on each redirect, and `GET /links/<code>` returns it as a new `clicks` field. All 13 tests pass, and I checked it end to end against a copy of your real `shortener.db`: two redirects, then `"clicks": 2`.

```json
{"clicks": 2, "code": "kiWflWs", "created_at": "2026-11-08 13:58:36", "url": "https://example.com/x"}
```

**What changed**
- **`app.py`:** a `clicks INTEGER NOT NULL DEFAULT 0` column on `links`. Each redirect increments it in SQL, so simultaneous requests can't lose counts. Your existing `shortener.db` gets the column added once at startup (safe to repeat), and existing links start at 0.
- **`test_app.py`:** 5 new tests. They cover counting, separate counts per link, viewing `/links/<code>` not counting as a click, upgrading an old-format database, and the redirect still working when the count can't be saved. I confirmed that last test fails (500) if the safeguard is removed.
- **`README.md`:** notes on what the count means (below).

Decision: `clicks` counter column on `links` over a per-click events table, because only a total was asked for. Cost: no per-day or referrer history. [new]

Decision: best-effort count write over failing the request, because redirects matter more than stats. Cost: silent undercount while DB unwritable, logged only. [new]

Both are logged as D-003 and D-004 in `docs/decisions.md`.

**What the number means**
- It counts every redirect served, including bots, link previews (Slack, iMessage) and `curl -I`.
- Redirects aren't rate limited, so anyone can inflate a count. That's fine for a personal shortener, but don't treat it as exact.
- Each redirect now writes to the database once. That's negligible at your traffic. If `could not record click` lines start appearing in the logs, that's the sign to revisit.

**Side effect:** my first live check went to another server already running on port 5000 (`/tmp/tmp.FGIH5htqgS/baseline/app.py`), not mine. It created one test link, `qNnN_9c`, in that server's database. I didn't stop that process, and I reran the check on a free port.

Nothing is committed yet.


**Diff:**

```diff
diff --git a/README.md b/README.md
index 51cd1b2..5e61e11 100644
--- a/README.md
+++ b/README.md
@@ -9,8 +9,15 @@ python -m pytest -q    # tests
 ```
 
 - `POST /links {"url": ...}` creates a short code. Limited to 10 requests per 60 s per client IP; over the limit it returns `429` with a `Retry-After` header. Tune with `SHORTENER_RATE_LIMIT` and `SHORTENER_RATE_WINDOW` (seconds).
-- `GET /<code>` redirects.
-- `GET /links/<code>` returns the link's details.
+- `GET /<code>` redirects and adds one to the link's click count.
+- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`.
+
+## Click counting notes
+
+- `clicks` counts every redirect served: bots, link-preview fetchers (Slack, iMessage) and `HEAD` requests included. Redirects are not rate limited, so anyone can inflate a count.
+- It is a running total only. There is no per-click history (time, referrer), so clicks-per-day cannot be reconstructed later.
+- If the count can't be written (disk full, database locked, read-only file), the redirect still works. The click is dropped and logged as `could not record click for <code>`.
+- Databases from before click counting get the column added at startup. Existing links start at 0.
 
 ## Rate limiting notes
 
diff --git a/app.py b/app.py
index 3694cd2..52bf826 100644
--- a/app.py
+++ b/app.py
@@ -73,9 +73,14 @@ def init_db():
         """CREATE TABLE IF NOT EXISTS links (
                code TEXT PRIMARY KEY,
                url TEXT NOT NULL,
-               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
+               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
+               clicks INTEGER NOT NULL DEFAULT 0
            )"""
     )
+    # Databases created before click counting lack the column; existing links start at 0.
+    columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
+    if "clicks" not in columns:
+        db().execute("ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0")
     db().commit()
 
 
@@ -106,7 +111,9 @@ def create_link():
 
 @app.get("/links/<code>")
 def link_info(code):
-    row = db().execute("SELECT code, url, created_at FROM links WHERE code = ?", (code,)).fetchone()
+    row = db().execute(
+        "SELECT code, url, created_at, clicks FROM links WHERE code = ?", (code,)
+    ).fetchone()
     if row is None:
         abort(404)
     return jsonify(dict(row))
@@ -117,6 +124,14 @@ def follow(code):
     row = db().execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
     if row is None:
         abort(404)
+    # Best effort: a failed count write (disk full, DB locked, read-only file) must not
+    # break the redirect itself. The click is lost and logged instead; the per-request
+    # connection is closed at teardown, which discards the failed write.
+    try:
+        db().execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
+        db().commit()
+    except sqlite3.Error:
+        app.logger.exception("could not record click for %s", code)
     return redirect(row["url"], code=302)
 
 
diff --git a/test_app.py b/test_app.py
index e0e7b0f..367cdfd 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,3 +1,5 @@
+import sqlite3
+
 import pytest
 
 from app import FixedWindowLimiter, app, init_db, link_limiter
@@ -72,3 +74,63 @@ def test_limiter_window_resets():
     assert limiter.hit("a") == 40  # seconds until the window ends
     now[0] += 40
     assert limiter.hit("a") == 0
+
+
+def test_clicks_counted_in_link_info(client):
+    code = post_link(client).get_json()["code"]
+    assert client.get(f"/links/{code}").get_json()["clicks"] == 0
+    for _ in range(3):
+        assert client.get(f"/{code}").status_code == 302
+    info = client.get(f"/links/{code}").get_json()
+    assert info["clicks"] == 3
+    assert info["url"] == "https://example.com/"
+
+
+def test_clicks_are_per_link(client):
+    a = post_link(client).get_json()["code"]
+    b = post_link(client).get_json()["code"]
+    client.get(f"/{a}")
+    client.get(f"/{a}")
+    client.get(f"/{b}")
+    assert client.get(f"/links/{a}").get_json()["clicks"] == 2
+    assert client.get(f"/links/{b}").get_json()["clicks"] == 1
+
+
+def test_link_info_does_not_count_as_click(client):
+    code = post_link(client).get_json()["code"]
+    client.get(f"/links/{code}")
+    assert client.get(f"/links/{code}").get_json()["clicks"] == 0
+
+
+def test_redirect_survives_failed_click_write(client, tmp_path, monkeypatch, caplog):
+    code = post_link(client).get_json()["code"]
+    # Open the DB read-only so the click write fails while reads work (like a full disk).
+    real_connect = sqlite3.connect
+    monkeypatch.setitem(app.config, "DB_PATH", f"file:{tmp_path / 'test.db'}?mode=ro")
+    monkeypatch.setattr(sqlite3, "connect", lambda path, **kw: real_connect(path, uri=True, **kw))
+    res = client.get(f"/{code}")
+    monkeypatch.undo()
+    assert res.status_code == 302
+    assert res.headers["Location"] == "https://example.com/"
+    assert "could not record click" in caplog.text
+    assert client.get(f"/links/{code}").get_json()["clicks"] == 0
+
+
+def test_init_db_adds_clicks_to_existing_db(tmp_path):
+    path = tmp_path / "old.db"
+    old = sqlite3.connect(path)
+    old.execute(
+        "CREATE TABLE links (code TEXT PRIMARY KEY, url TEXT NOT NULL,"
+        " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
+    )
+    old.execute("INSERT INTO links (code, url) VALUES ('old1', 'https://example.com/old')")
+    old.commit()
+    old.close()
+    app.config["DB_PATH"] = str(path)
+    with app.app_context():
+        init_db()
+        init_db()  # idempotent on an already-migrated DB
+    client = app.test_client()
+    assert client.get("/links/old1").get_json()["clicks"] == 0
+    assert client.get("/old1").status_code == 302
+    assert client.get("/links/old1").get_json()["clicks"] == 1
```

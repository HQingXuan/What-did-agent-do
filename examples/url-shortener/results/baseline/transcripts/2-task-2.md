# task-2

**Prompt:** Count clicks per link and include the count in GET /links/<code>.

**Reply:**

Every redirect through `GET /<code>` now adds one to that link's click count, and `GET /links/<code>` includes it as a `clicks` field. All 12 tests pass: the original 9 plus 3 new ones, which fail without the change.

**What changed (`app.py`)**
- **Storage:** a new `clicks` column on the `links` table, starting at 0. I used a counter column rather than one row per click because the request only asks for a count.
- **Counting:** each redirect adds one using `clicks = clicks + 1` in the SQL itself. That way two redirects at the same moment can't overwrite each other and lose a count.
- **Existing databases:** your local `shortener.db` was made before this column existed, so on startup `init_db()` adds it to older databases if it's missing. Existing links start at 0. Running it again does nothing. I tested this on a copy of your database, and your real file hasn't been changed. It will be updated the next time the app starts.

**Tests and docs**
- New tests check that counts are kept per link, that viewing `GET /links/<code>` doesn't count as a click, and that an old-format database gets the column and counts correctly afterwards.
- The README now lists the new field.

**Worth knowing**
- **Every redirect now writes to the database;** before, redirects only read from it. That's fine for a personal shortener on one VM, but it's the first write on that path.
- **`HEAD /<code>` also counts as a click.** Flask answers HEAD requests with the same handler, and some link-preview bots and uptime checkers send them, so counts can be slightly high. Skipping HEAD would take one line if you want that.
- **Counts are as accurate as the redirect type allows.** The redirect is a 302, which browsers don't cache, so repeat visits are counted. If it's ever changed to a 301, browsers would cache the redirect and repeat visits would stop being counted.


**Diff:**

```diff
diff --git a/README.md b/README.md
index 6c6a8de..accce70 100644
--- a/README.md
+++ b/README.md
@@ -9,5 +9,5 @@ python -m pytest -q    # tests
 ```
 
 - `POST /links {"url": ...}` creates a short code. Limited to 10 requests per minute per client IP; beyond that it returns `429` with a `Retry-After` header.
-- `GET /<code>` redirects.
-- `GET /links/<code>` returns the link's details.
+- `GET /<code>` redirects and counts a click.
+- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, and `clicks`.
diff --git a/app.py b/app.py
index 9b3985e..88b6396 100644
--- a/app.py
+++ b/app.py
@@ -84,9 +84,14 @@ def init_db():
         """CREATE TABLE IF NOT EXISTS links (
                code TEXT PRIMARY KEY,
                url TEXT NOT NULL,
-               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
+               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
+               clicks INTEGER NOT NULL DEFAULT 0
            )"""
     )
+    # Databases created before click counting existed lack the column.
+    columns = {row["name"] for row in db().execute("PRAGMA table_info(links)")}
+    if "clicks" not in columns:
+        db().execute("ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0")
     db().commit()
 
 
@@ -116,7 +121,9 @@ def create_link():
 
 @app.get("/links/<code>")
 def link_info(code):
-    row = db().execute("SELECT code, url, created_at FROM links WHERE code = ?", (code,)).fetchone()
+    row = db().execute(
+        "SELECT code, url, created_at, clicks FROM links WHERE code = ?", (code,)
+    ).fetchone()
     if row is None:
         abort(404)
     return jsonify(dict(row))
@@ -127,6 +134,9 @@ def follow(code):
     row = db().execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
     if row is None:
         abort(404)
+    # Incrementing in SQL keeps concurrent redirects from losing counts.
+    db().execute("UPDATE links SET clicks = clicks + 1 WHERE code = ?", (code,))
+    db().commit()
     return redirect(row["url"], code=302)
 
 
diff --git a/test_app.py b/test_app.py
index 767baf6..4b6242a 100644
--- a/test_app.py
+++ b/test_app.py
@@ -1,3 +1,5 @@
+import sqlite3
+
 import pytest
 
 import app as app_module
@@ -104,3 +106,44 @@ def test_limiter_forgets_idle_clients():
     clock.now += 61
     limiter.hit("b")  # triggers a sweep; "a" is stale
     assert set(limiter._hits) == {"b"}
+
+
+def test_clicks_counted_per_link(client):
+    a = post_link(client).get_json()["code"]
+    b = post_link(client).get_json()["code"]
+    assert client.get(f"/links/{a}").get_json()["clicks"] == 0
+    for _ in range(3):
+        client.get(f"/{a}")
+    client.get(f"/{b}")
+    assert client.get(f"/links/{a}").get_json()["clicks"] == 3
+    assert client.get(f"/links/{b}").get_json()["clicks"] == 1
+
+
+def test_viewing_link_info_is_not_a_click(client):
+    code = post_link(client).get_json()["code"]
+    client.get(f"/links/{code}")
+    assert client.get(f"/links/{code}").get_json()["clicks"] == 0
+
+
+def test_init_db_adds_clicks_to_existing_database(tmp_path, clock):
+    path = tmp_path / "old.db"
+    conn = sqlite3.connect(path)
+    conn.execute(
+        """CREATE TABLE links (
+               code TEXT PRIMARY KEY,
+               url TEXT NOT NULL,
+               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
+           )"""
+    )
+    conn.execute("INSERT INTO links (code, url) VALUES ('old', 'https://example.com/')")
+    conn.commit()
+    conn.close()
+
+    app.config["DB_PATH"] = str(path)
+    with app.app_context():
+        init_db()
+        init_db()  # safe to run again once migrated
+    client = app.test_client()
+    assert client.get("/links/old").get_json()["clicks"] == 0
+    assert client.get("/old").status_code == 302
+    assert client.get("/links/old").get_json()["clicks"] == 1
```

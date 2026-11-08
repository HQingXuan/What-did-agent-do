"""A tiny URL shortener: one Flask process, one SQLite file."""
import os
import secrets
import sqlite3
from urllib.parse import urlparse

from flask import Flask, abort, g, jsonify, redirect, request

DB_PATH = os.environ.get("SHORTENER_DB", "shortener.db")

app = Flask(__name__)


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
               created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    db().commit()


def valid_url(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@app.post("/links")
def create_link():
    url = (request.get_json(silent=True) or {}).get("url", "")
    if not valid_url(url):
        return jsonify(error="url must be an http(s) URL"), 400
    code = secrets.token_urlsafe(5)
    db().execute("INSERT INTO links (code, url) VALUES (?, ?)", (code, url))
    db().commit()
    return jsonify(code=code, short_url=request.host_url + code), 201


@app.get("/links/<code>")
def link_info(code):
    row = db().execute("SELECT code, url, created_at FROM links WHERE code = ?", (code,)).fetchone()
    if row is None:
        abort(404)
    return jsonify(dict(row))


@app.get("/<code>")
def follow(code):
    row = db().execute("SELECT url FROM links WHERE code = ?", (code,)).fetchone()
    if row is None:
        abort(404)
    return redirect(row["url"], code=302)


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(port=5000)

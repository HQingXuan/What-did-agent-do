# url-shortener

A personal link shortener. One Flask process and one SQLite file, running on a single small VM. I maintain it alone.

```bash
pip install -r requirements.txt
python app.py          # serves on :5000
python -m pytest -q    # tests
```

- `POST /links {"url": ...}` creates a short code. Limited to 10 requests per minute per client IP; beyond that it returns `429` with a `Retry-After` header.
- `GET /<code>` redirects and counts a click.
- `GET /links/<code>` returns the link's details: `code`, `url`, `created_at`, `clicks`, and `title`.
  - `title` is the target page's `<title>`, or `null` if none could be found (unreachable, not HTML, error status). It is fetched on the first view and stored, so that view can take up to ~5 s. A found title is kept as is; a `null` one is retried on a view at least an hour later.
  - Fetches only go to public IP addresses, so links can't be used to reach localhost, the private network, or cloud metadata endpoints.

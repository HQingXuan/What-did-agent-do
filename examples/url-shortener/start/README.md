# url-shortener

A personal link shortener. One Flask process and one SQLite file, running on a single small VM. I maintain it alone.

```bash
pip install -r requirements.txt
python app.py          # serves on :5000
python -m pytest -q    # tests
```

- `POST /links {"url": ...}` creates a short code.
- `GET /<code>` redirects.
- `GET /links/<code>` returns the link's details.

import pytest

from app import app, init_db


@pytest.fixture
def client(tmp_path):
    app.config["DB_PATH"] = str(tmp_path / "test.db")
    with app.app_context():
        init_db()
    return app.test_client()


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

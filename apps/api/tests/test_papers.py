import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reproagent_api.auth import get_anon_client
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import papers

USER_ID = uuid4()


class FakeAuth:
    def get_user(self, token: str):
        if token != "good-token":
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(USER_ID)))


@pytest.fixture
def client():
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
    with TestClient(app) as test_client:
        yield test_client


AUTH = {"Authorization": "Bearer good-token"}


def upload(client, content: bytes, filename="paper.pdf"):
    return client.post(
        "/v1/papers",
        files={"file": (filename, content, "application/pdf")},
        data={"title": "LeNet-5"},
        headers=AUTH,
    )


def test_upload_requires_auth(client):
    response = client.post(
        "/v1/papers",
        files={"file": ("p.pdf", b"%PDF-1.4", "application/pdf")},
        data={"title": "LeNet-5"},
    )
    assert response.status_code == 401


def test_delete_requires_auth(client):
    response = client.delete(f"/v1/papers/{uuid4()}")
    assert response.status_code == 401


def test_delete_checks_ownership_before_database_access(client, monkeypatch):
    from fastapi import HTTPException

    def deny(admin, paper_id, user_id):
        assert user_id == USER_ID
        raise HTTPException(404, "Paper not found")

    def unexpected_connection(*args, **kwargs):
        pytest.fail("Deletion must check ownership before connecting to Postgres")

    monkeypatch.setattr(papers, "_admin", lambda request: object())
    monkeypatch.setattr(papers, "_get_paper", deny)
    monkeypatch.setattr(papers.psycopg, "connect", unexpected_connection)
    response = client.delete(f"/v1/papers/{uuid4()}", headers=AUTH)
    assert response.status_code == 404


def test_upload_rejects_non_pdf(client):
    response = upload(client, b"this is plain text, not a pdf")
    assert response.status_code == 422


def test_upload_rejects_empty_file(client):
    response = upload(client, b"")
    assert response.status_code == 422


def test_upload_rejects_oversized(client, monkeypatch):
    monkeypatch.setattr(papers, "MAX_PDF_BYTES", 10)
    response = upload(client, b"%PDF-1.4" + b"x" * 100)
    assert response.status_code == 413


def test_upload_requires_title(client):
    response = client.post(
        "/v1/papers",
        files={"file": ("p.pdf", b"%PDF-1.4", "application/pdf")},
        data={"title": ""},
        headers=AUTH,
    )
    assert response.status_code == 422


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *args):
        return self

    def eq(self, *args):
        return self

    def execute(self):
        return _FakeResult(self._data)


class _FakeStorage:
    def __init__(self, signed_url):
        self._signed_url = signed_url

    def from_(self, bucket):
        return self

    def create_signed_url(self, path, expires_in):
        return {"signedURL": self._signed_url}


class _FakeAdmin:
    def __init__(self, paper_row, signed_url):
        self._paper_row = paper_row
        self.storage = _FakeStorage(signed_url)

    def table(self, name):
        return _FakeQuery([self._paper_row])


def _pdf_client(monkeypatch, signed_url):
    settings = ApiSettings(
        supabase_url="http://host.docker.internal:54321",
        supabase_public_url="http://localhost:54321",
        supabase_anon_key="anon-test",
        supabase_service_role_key="service-test",
        _env_file=None,
    )
    paper_id = uuid4()
    row = {"id": str(paper_id), "project_id": str(uuid4()), "pdf_path": "u/p.pdf"}
    monkeypatch.setattr(papers, "make_service_client", lambda s: _FakeAdmin(row, signed_url))
    app = create_app(settings)
    app.dependency_overrides[get_anon_client] = lambda: SimpleNamespace(auth=FakeAuth())
    return TestClient(app), paper_id


def test_pdf_url_rewrites_internal_host(monkeypatch):
    signed = "http://host.docker.internal:54321/storage/v1/object/sign/papers/u/p.pdf?token=abc"
    client, paper_id = _pdf_client(monkeypatch, signed)
    with client:
        response = client.get(f"/v1/papers/{paper_id}/pdf", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["url"] == (
        "http://localhost:54321/storage/v1/object/sign/papers/u/p.pdf?token=abc"
    )


def test_pdf_url_prefixes_relative_path(monkeypatch):
    client, paper_id = _pdf_client(monkeypatch, "/storage/v1/object/sign/papers/u/p.pdf?token=abc")
    with client:
        response = client.get(f"/v1/papers/{paper_id}/pdf", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["url"] == (
        "http://localhost:54321/storage/v1/object/sign/papers/u/p.pdf?token=abc"
    )


class _ProjQuery:
    def __init__(self, admin):
        self._admin = admin
        self._filters = {}
        self._insert = None

    def select(self, *args):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def insert(self, row):
        self._insert = row
        return self

    def execute(self):
        if self._insert is not None:
            row = {"id": str(uuid4()), **self._insert}
            self._admin.projects.append(row)
            self._admin.inserted.append(row)
            return _FakeResult([row])
        rows = [
            r for r in self._admin.projects if all(r.get(k) == v for k, v in self._filters.items())
        ]
        return _FakeResult(rows)


class _FakeProjectsAdmin:
    def __init__(self, existing=None):
        self.projects = list(existing or [])
        self.inserted = []

    def table(self, name):
        assert name == "projects"
        return _ProjQuery(self)


def test_default_project_reuses_existing():
    admin = _FakeProjectsAdmin([{"id": "p1", "user_id": str(USER_ID), "is_default": True}])
    assert papers._default_project(admin, USER_ID) == "p1"
    assert admin.inserted == []


def test_default_project_creates_when_absent():
    admin = _FakeProjectsAdmin([])
    project_id = papers._default_project(admin, USER_ID)
    assert len(admin.inserted) == 1
    assert admin.inserted[0]["user_id"] == str(USER_ID)
    assert admin.inserted[0]["is_default"] is True
    assert project_id == admin.inserted[0]["id"]


def test_normalize_paper_url_arxiv_abs_to_pdf():
    assert (
        papers._normalize_paper_url("https://arxiv.org/abs/2401.01234")
        == "https://arxiv.org/pdf/2401.01234"
    )
    assert (
        papers._normalize_paper_url("https://example.org/paper.pdf")
        == "https://example.org/paper.pdf"
    )


def test_normalize_paper_url_rejects_non_http():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        papers._normalize_paper_url("file:///etc/passwd")
    assert exc.value.status_code == 422


def test_assert_public_host_blocks_private(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        papers.socket, "getaddrinfo", lambda *a: [(2, 1, 6, "", ("127.0.0.1", 0))]
    )
    with pytest.raises(HTTPException) as exc:
        papers._assert_public_host("evil.example")
    assert exc.value.status_code == 422
    monkeypatch.setattr(
        papers.socket, "getaddrinfo", lambda *a: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    papers._assert_public_host("example.org")


class _PdfHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/paper.pdf")
            self.end_headers()
        elif self.path == "/paper.pdf":
            body = b"%PDF-1.4 fake"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big.pdf":
            body = b"%PDF-1.4" + b"x" * 1000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"<html>not a pdf</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def pdf_server(monkeypatch):
    monkeypatch.setattr(papers, "_assert_public_host", lambda host: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PdfHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_download_pdf_follows_redirect(pdf_server):
    data = await papers._download_pdf(f"{pdf_server}/redirect")
    assert data == b"%PDF-1.4 fake"


async def test_download_pdf_rejects_non_pdf(pdf_server):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await papers._download_pdf(f"{pdf_server}/html")
    assert exc.value.status_code == 422


async def test_download_pdf_enforces_size_cap(pdf_server, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(papers, "MAX_PDF_BYTES", 10)
    with pytest.raises(HTTPException) as exc:
        await papers._download_pdf(f"{pdf_server}/big.pdf")
    assert exc.value.status_code == 413

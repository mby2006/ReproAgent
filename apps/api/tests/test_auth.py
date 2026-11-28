from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from reproagent_api.auth import ensure_owner, get_anon_client, get_current_user_id
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app
from reproagent_api.routers import experiments

USER_ID = uuid4()


class FakeAuth:
    def __init__(self, valid_token: str, user_id):
        self.valid_token = valid_token
        self.user_id = user_id

    def get_user(self, token: str):
        if token != self.valid_token:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(self.user_id)))


class FakeQuery:
    def __init__(self, data):
        self.data = data

    def select(self, *args):
        return self

    def eq(self, *args):
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


class FakeAdmin:
    def __init__(self, paper_id):
        self.paper_id = paper_id

    def table(self, name):
        assert name in {"papers", "experiments"}
        data = [{"id": str(self.paper_id)}] if name == "papers" else []
        return FakeQuery(data)


@pytest.fixture
def app_with_fake_auth(monkeypatch):
    paper_id = uuid4()
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    fake_client = SimpleNamespace(auth=FakeAuth("good-token", USER_ID))
    app.dependency_overrides[get_anon_client] = lambda: fake_client
    monkeypatch.setattr(experiments, "make_service_client", lambda request: FakeAdmin(paper_id))
    return app, paper_id


def test_missing_token_rejected(app_with_fake_auth):
    app, paper_id = app_with_fake_auth
    client = TestClient(app)
    response = client.get(f"/v1/papers/{paper_id}/experiments")
    assert response.status_code == 401


def test_invalid_token_rejected(app_with_fake_auth):
    app, paper_id = app_with_fake_auth
    client = TestClient(app)
    response = client.get(
        f"/v1/papers/{paper_id}/experiments",
        headers={"Authorization": "Bearer bad-token"},
    )
    assert response.status_code == 401


def test_valid_token_reaches_real_protected_endpoint(app_with_fake_auth):
    app, paper_id = app_with_fake_auth
    client = TestClient(app)
    response = client.get(
        f"/v1/papers/{paper_id}/experiments",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 200
    assert response.json() == []


async def test_get_current_user_id_returns_uuid():
    fake_client = SimpleNamespace(auth=FakeAuth("good-token", USER_ID))
    credentials = SimpleNamespace(credentials="good-token")
    result = await get_current_user_id(credentials, fake_client)
    assert result == USER_ID


def test_ensure_owner_allows_owner():
    ensure_owner(USER_ID, USER_ID)


def test_ensure_owner_blocks_other_user():
    with pytest.raises(HTTPException) as exc_info:
        ensure_owner(uuid4(), USER_ID)
    assert exc_info.value.status_code == 404

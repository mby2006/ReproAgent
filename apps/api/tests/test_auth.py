from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from reproagent_api.auth import ensure_owner, get_anon_client, get_current_user_id
from reproagent_api.config import ApiSettings
from reproagent_api.main import create_app

USER_ID = uuid4()


class FakeAuth:
    def __init__(self, valid_token: str, user_id):
        self.valid_token = valid_token
        self.user_id = user_id

    def get_user(self, token: str):
        if token != self.valid_token:
            raise RuntimeError("invalid token")
        return SimpleNamespace(user=SimpleNamespace(id=str(self.user_id)))


@pytest.fixture
def app_with_fake_auth():
    settings = ApiSettings(
        supabase_anon_key="anon-test", supabase_service_role_key="service-test", _env_file=None
    )
    app = create_app(settings)
    fake_client = SimpleNamespace(auth=FakeAuth("good-token", USER_ID))
    app.dependency_overrides[get_anon_client] = lambda: fake_client
    return app


def test_missing_token_rejected(app_with_fake_auth):
    client = TestClient(app_with_fake_auth)
    response = client.post("/v1/reproductions", json={"paper_id": str(uuid4()), "title": "LeNet"})
    assert response.status_code == 401


def test_invalid_token_rejected(app_with_fake_auth):
    client = TestClient(app_with_fake_auth)
    response = client.post(
        "/v1/reproductions",
        json={"paper_id": str(uuid4()), "title": "LeNet"},
        headers={"Authorization": "Bearer bad-token"},
    )
    assert response.status_code == 401


def test_valid_token_reaches_stub(app_with_fake_auth):
    client = TestClient(app_with_fake_auth)
    response = client.post(
        "/v1/reproductions",
        json={"paper_id": str(uuid4()), "title": "LeNet"},
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 501


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

"""Integration test: RLS ownership isolation against a running local Supabase.

Ownership flows through projects.user_id; papers/specs derive it transitively.
Run: uv run pytest tests/integration -v
Requires `supabase start` (from infra/) and .env with local keys.
"""

from uuid import uuid4

import httpx
import pytest
from postgrest.exceptions import APIError
from reproagent_api.clients.supabase import make_service_client
from reproagent_api.config import ApiSettings

from supabase import ClientOptions, create_client


def _options() -> ClientOptions:
    return ClientOptions(httpx_client=httpx.Client(trust_env=False, timeout=30.0))


@pytest.fixture(scope="module")
def settings() -> ApiSettings:
    return ApiSettings()


@pytest.fixture(scope="module")
def two_users(settings):
    admin = make_service_client(settings)
    suffix = uuid4().hex[:12]
    users = []
    for name in ("alice", "bob"):
        email = f"{name}-{suffix}@example.test"
        created = admin.auth.admin.create_user(
            {"email": email, "password": "test-password-123", "email_confirm": True}
        )
        users.append(created.user)
    yield users
    for user in users:
        admin.auth.admin.delete_user(user.id)


def user_client(settings, email: str):
    anon = create_client(settings.supabase_url, settings.supabase_anon_key, _options())
    session = anon.auth.sign_in_with_password({"email": email, "password": "test-password-123"})
    return create_client(settings.supabase_url, session.session.access_token, _options())


def make_project(client, user_id) -> str:
    row = (
        client.table("projects")
        .insert({"user_id": user_id, "name": "test project", "is_default": True})
        .execute()
        .data[0]
    )
    return row["id"]


def make_paper(client, project_id) -> str:
    row = (
        client.table("papers")
        .insert(
            {
                "project_id": project_id,
                "title": "LeNet-5 paper",
                "source": "upload",
                "pdf_path": f"{uuid4()}/paper.pdf",
            }
        )
        .execute()
        .data[0]
    )
    return row["id"]


def test_rls_isolates_papers_between_users(settings, two_users):
    alice, bob = two_users
    alice_client = user_client(settings, alice.email)
    bob_client = user_client(settings, bob.email)

    project_id = make_project(alice_client, alice.id)
    paper_id = make_paper(alice_client, project_id)

    assert len(alice_client.table("papers").select("id").execute().data) == 1
    assert bob_client.table("papers").select("id").execute().data == []

    bob_update = bob_client.table("papers").update({"title": "hacked"}).eq("id", paper_id).execute()
    assert bob_update.data == []

    alice_client.table("papers").delete().eq("id", paper_id).execute()
    alice_client.table("projects").delete().eq("id", project_id).execute()


def test_rls_rejects_paper_in_foreign_project(settings, two_users):
    alice, bob = two_users
    alice_client = user_client(settings, alice.email)
    bob_client = user_client(settings, bob.email)
    bob_project = make_project(bob_client, bob.id)
    with pytest.raises(APIError):
        alice_client.table("papers").insert(
            {
                "project_id": bob_project,
                "title": "not mine",
                "source": "upload",
                "pdf_path": f"{uuid4()}/x.pdf",
            }
        ).execute()
    bob_client.table("projects").delete().eq("id", bob_project).execute()


def test_rls_specs_are_append_only(settings, two_users):
    alice, bob = two_users
    alice_client = user_client(settings, alice.email)
    bob_client = user_client(settings, bob.email)

    project_id = make_project(alice_client, alice.id)
    paper_id = make_paper(alice_client, project_id)
    spec = (
        alice_client.table("reproduction_specs")
        .insert({"paper_id": paper_id, "version": 1, "origin": "extraction", "spec_json": {}})
        .execute()
        .data[0]
    )

    assert bob_client.table("reproduction_specs").select("id").execute().data == []
    # No update/delete policies exist: even the owner cannot mutate an old version.
    assert (
        alice_client.table("reproduction_specs")
        .update({"version": 99})
        .eq("id", spec["id"])
        .execute()
        .data
        == []
    )
    assert (
        alice_client.table("reproduction_specs").delete().eq("id", spec["id"]).execute().data == []
    )

    alice_client.table("papers").delete().eq("id", paper_id).execute()
    alice_client.table("projects").delete().eq("id", project_id).execute()

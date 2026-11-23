"""Markdown export of a persisted Reproduction Card.

The export is deterministic: everything in the document comes from the stored
card version, so the tests assert provenance survives the round trip (status,
page, quote) and that the endpoint keeps the same ownership/version rules as
``GET /card``.
"""

from uuid import uuid4

import pytest
from reproagent_api.routers import papers
from reproagent_api.services import render_card_markdown
from reproagent_schemas import EvidenceField, EvidenceStatus, ReproductionCard, SpecOrigin
from test_experiments import AUTH, BOB_AUTH, PAPER_ID, make_card
from test_experiments import env as _experiment_env

experiment_env = _experiment_env


@pytest.fixture
def md_env(experiment_env, monkeypatch):
    client, admin = experiment_env
    monkeypatch.setattr(papers, "make_service_client", lambda settings: admin)
    spec = admin.tables["reproduction_specs"][0]
    spec.update(
        {
            "spec_json": make_card().model_dump(mode="json"),
            "origin": "extraction",
            "parent_version_id": None,
            "status": "confirmed",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    return client, admin


def url(version: int | None = None) -> str:
    base = f"/v1/papers/{PAPER_ID}/card.md"
    return base if version is None else f"{base}?version={version}"


def test_export_returns_markdown_document(md_env):
    client, _ = md_env
    response = client.get(url(), headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]

    body = response.text
    assert body.startswith("# LeNet-5")
    assert "Reproduction Card v1" in body
    assert "解析提取" in body
    assert "`paper_explicit`" in body
    assert "第 8 页" in body
    assert "...LeNet-5 gradient-based learning..." in body
    assert "证据等级说明" in body


def test_export_marks_missing_fields_without_evidence(md_env):
    client, _ = md_env
    body = client.get(url(), headers=AUTH).text

    assert "`missing` 缺失" in body
    assert "（论文未提取到报告结果）" in body


def test_legacy_confirmation_keeps_unknown_source(md_env):
    client, admin = md_env
    method = admin.tables["reproduction_specs"][0]["spec_json"]["method"]
    method["human_confirmed"] = True
    method.pop("confirmed_by", None)
    response = client.get(url(), headers=AUTH)
    assert response.status_code == 200
    assert "**确认**：已确认，来源未知" in response.text


def test_export_requires_ownership(md_env):
    client, _ = md_env
    assert client.get(url()).status_code == 401
    assert client.get(url(), headers=BOB_AUTH).status_code == 404


def test_export_rejects_unknown_version(md_env):
    client, _ = md_env
    response = client.get(url(version=99), headers=AUTH)
    assert response.status_code == 404


def test_export_404_without_any_card(md_env):
    client, admin = md_env
    admin.tables["reproduction_specs"] = []
    assert client.get(url(), headers=AUTH).status_code == 404


def test_render_escapes_table_breaks_and_keeps_pending_list():
    card = ReproductionCard(
        method=EvidenceField(
            value="pipe | value",
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote="a | b\nc",
            source_page=3,
        ),
        model_architecture=EvidenceField(
            value="lenet",
            status=EvidenceStatus.PAPER_EXPLICIT,
            evidence_quote="lenet",
            source_page=3,
        ),
        dataset=EvidenceField(value="mnist", status=EvidenceStatus.INFERRED, confidence=0.6),
        optimizer=EvidenceField(status=EvidenceStatus.MISSING),
        loss_function=EvidenceField(status=EvidenceStatus.MISSING),
        missing_information=["论文未给出学习率 | 衰减策略"],
    )
    markdown = render_card_markdown(
        card,
        paper_title="T | itle",
        paper_id=uuid4(),
        version=7,
        version_id=uuid4(),
        origin=SpecOrigin.USER_EDIT,
        created_at=None,
    )

    assert "# T \\| itle" in markdown
    assert "pipe \\| value" in markdown
    assert "a | b c" in markdown
    assert "用户修改" in markdown
    assert "生成于 —" in markdown
    assert "`dataset`" in markdown
    assert "论文未给出学习率 \\| 衰减策略" in markdown

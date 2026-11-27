from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from scripts import smoke_live_v2
from scripts.smoke_live_v2 import write_scanned_pdf


def test_scanned_pdf_has_one_image_only_page(tmp_path: Path) -> None:
    target = tmp_path / "scanned.pdf"

    write_scanned_pdf(target)

    with pymupdf.open(target) as document:
        assert len(document) == 1
        assert document[0].get_text() == ""
        assert len(document[0].get_images(full=True)) == 1


class FakeQuery:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.filters: list[tuple[str, object]] = []
        self.single_requested = False

    def select(self, *_columns: str) -> "FakeQuery":
        return self

    def eq(self, column: str, value: object) -> "FakeQuery":
        self.filters.append((column, value))
        return self

    def in_(self, column: str, values: list[object]) -> "FakeQuery":
        self.filters.append((column, values))
        return self

    def single(self) -> "FakeQuery":
        self.single_requested = True
        return self

    def execute(self) -> SimpleNamespace:
        rows = self.rows
        for column, expected in self.filters:
            if isinstance(expected, list):
                rows = [row for row in rows if row.get(column) in expected]
            else:
                rows = [row for row in rows if row.get(column) == expected]
        data = rows[0] if self.single_requested else rows
        return SimpleNamespace(data=data)


class FakeAdmin:
    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self.tables.get(name, []))


def test_scanned_gate_counts_runs_through_paper_when_workflow_link_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = FakeAdmin({
        "workflows": [{"id": "workflow-1", "paper_id": "paper-1", "run_id": None}],
        "experiments": [{"id": "experiment-1", "paper_id": "paper-1"}],
        "execution_runs": [{"id": "run-1", "experiment_id": "experiment-1"}],
        "job_outbox": [
            {"id": "outbox-1", "entity_id": "run-1", "job_type": "run_experiment_task"},
            {"id": "outbox-2", "entity_id": "other-run", "job_type": "run_experiment_task"},
        ],
    })
    monkeypatch.setattr(smoke_live_v2, "make_service_client", lambda _settings: admin)

    assert smoke_live_v2.trusted_counts("workflow-1") == (1, 1)

"""Integration tests for retained generic work-item close checks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.routers.bootstrap import init_project

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ide_adapter_hook() -> None:
    with patch("ai_sdlc.cli.main.run_ide_adapter_if_initialized"):
        yield


def _write_work_item(root: Path, *, complete: bool) -> Path:
    init_project(root)
    work_item = root / "specs" / "001-generic"
    work_item.mkdir(parents=True)
    marker = "x" if complete else " "
    (work_item / "tasks.md").write_text(
        "# Tasks\n\n"
        f"- [{marker}] Implement retained behavior\n\n"
        "### Task 1.1\n"
        "- **验收标准（AC）**：retained behavior is verified\n",
        encoding="utf-8",
    )
    return work_item


def test_close_check_reports_only_generic_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = _write_work_item(tmp_path, complete=False)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["workitem", "close-check", "--wi", str(work_item), "--json"],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any("unchecked checklist" in item for item in payload["blockers"])
    check_names = {item["name"] for item in payload["checks"]}
    assert "local_pr_review" in check_names
    assert "done_gate" in check_names
    rendered = json.dumps(payload).lower()
    assert "program_truth" not in rendered
    assert "provenance" not in rendered
    assert "release_gate_evidence" not in rendered


def test_close_check_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = _write_work_item(tmp_path, complete=True)
    monkeypatch.chdir(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    runner.invoke(app, ["workitem", "close-check", "--wi", str(work_item)])

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_branch_check_reports_non_git_project_without_retired_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = _write_work_item(tmp_path, complete=True)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        ["workitem", "branch-check", "--wi", str(work_item), "--json"],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "program" not in result.output.lower()
    assert "provenance" not in result.output.lower()

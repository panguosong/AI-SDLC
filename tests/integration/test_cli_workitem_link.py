"""Integration tests for optional WorkItem checkpoint linkage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.context.state import load_checkpoint, save_checkpoint
from ai_sdlc.models.state import Checkpoint, FeatureInfo
from ai_sdlc.routers.bootstrap import init_project

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_ide_adapter_hook() -> None:
    with patch("ai_sdlc.cli.main.run_ide_adapter_if_initialized"):
        yield


def _initialize(root: Path) -> None:
    init_project(root)
    save_checkpoint(
        root,
        Checkpoint(
            current_stage="init",
            feature=FeatureInfo(
                id="unknown",
                spec_dir="specs/001",
                design_branch="design/x",
                feature_branch="feature/x",
                current_branch="main",
            ),
        ),
    )


def test_workitem_link_updates_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "workitem",
            "link",
            "--wi-id",
            "001-sample-work-item",
            "--plan-uri",
            ".cursor/plans/foo.plan.md",
        ],
    )

    assert result.exit_code == 0, result.output
    checkpoint = load_checkpoint(tmp_path)
    assert checkpoint is not None
    assert checkpoint.linked_wi_id == "001-sample-work-item"
    assert checkpoint.linked_plan_uri == ".cursor/plans/foo.plan.md"


def test_workitem_link_requires_at_least_one_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["workitem", "link"])

    assert result.exit_code == 2


def test_status_does_not_restore_parallel_reviewer_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["status", "--details"])

    assert result.exit_code == 0, result.output
    assert "Latest Reviewer Decision" not in result.output
    assert "Program Truth" not in result.output

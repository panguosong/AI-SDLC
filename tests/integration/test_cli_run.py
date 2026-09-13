"""Integration tests for the read-only five-Loop ``run`` command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.core.loop_models import LoopStatus, LoopType
from ai_sdlc.core.loop_router import (
    LoopRouteItem,
    LoopRouteResult,
    LoopRouteStatus,
)

runner = CliRunner()


def test_run_outside_project_reports_init_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1
    assert "Result: Project is not initialized." in result.output
    assert "Next: Run ai-sdlc init ." in result.output
    assert ".ai-sdlc is missing" in result.output


def test_run_help_keeps_legacy_options_but_describes_read_only_route() -> None:
    result = runner.invoke(app, ["run", "--help"])

    assert result.exit_code == 0
    assert "five-Loop" in result.output

    run_command = get_command(app).commands["run"]
    options = {
        option.name: option
        for option in run_command.params
        if isinstance(option, click.Option)
    }
    assert "--mode" in options["mode"].opts
    assert options["mode"].default == "auto"
    assert "--acknowledge-execute-batch" in options["acknowledge_execute_batch"].opts


def test_run_without_loop_is_read_only_and_requests_requirement(
    tmp_path: Path,
) -> None:
    _init_project_repo(tmp_path)
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b'{"legacy":true}\n')
    before = _repository_state(tmp_path, checkpoint)

    with patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["run", "--dry-run"])

    assert result.exit_code == 0
    assert "Result: No delivery Loop has been started." in result.output
    assert "loop requirement start" in result.output
    assert _repository_state(tmp_path, checkpoint) == before


def test_run_renders_current_review_aware_route(tmp_path: Path) -> None:
    _init_project_repo(tmp_path)
    routed = LoopRouteResult(
        status=LoopRouteStatus.ROUTED,
        result="Current delivery Loop: implementation impl-1 (needs_review).",
        current_loop=LoopRouteItem(
            loop_type=LoopType.IMPLEMENTATION,
            loop_id="impl-1",
            status=LoopStatus.NEEDS_REVIEW,
            next_action="Run bounded implementation experts.",
        ),
        next_action="Run bounded implementation experts.",
        blockers=["review-result-missing"],
    )

    with (
        patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.run_cmd.route_five_loops", return_value=routed),
    ):
        result = runner.invoke(app, ["run"])

    assert result.exit_code == 0
    assert "implementation / impl-1 (needs_review)" in result.output
    assert "Next: Run bounded implementation experts." in result.output
    assert "review-result-missing" in result.output
    assert "Applicable Rules" in result.output
    assert "tdd" in result.output
    assert "verification" in result.output
    assert "没有失败的测试，就不能写生产代码" not in result.output


def test_run_json_returns_route_and_bounded_rule_context(tmp_path: Path) -> None:
    _init_project_repo(tmp_path)
    routed = LoopRouteResult(
        status=LoopRouteStatus.ROUTED,
        result="Current delivery Loop: implementation impl-1 (needs_fix).",
        current_loop=LoopRouteItem(
            loop_type=LoopType.IMPLEMENTATION,
            loop_id="impl-1",
            status=LoopStatus.NEEDS_FIX,
            next_action="Fix the implementation finding.",
        ),
        next_action="Fix the implementation finding.",
        blockers=["important finding"],
    )

    with (
        patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.run_cmd.route_five_loops", return_value=routed),
    ):
        result = runner.invoke(app, ["run", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "routed"
    assert payload["current_loop"]["loop_type"] == "implementation"
    assert [item["name"] for item in payload["applicable_rules"]] == [
        "debugging",
        "verification",
    ]
    assert len(payload["applicable_rules"]) == 2


def test_run_confirm_mode_returns_migration_blocker_without_routing(
    tmp_path: Path,
) -> None:
    _init_project_repo(tmp_path)

    with (
        patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.run_cmd.route_five_loops") as route,
    ):
        result = runner.invoke(app, ["run", "--mode", "confirm"])

    assert result.exit_code == 1
    assert "retired seven-stage runner" in result.output
    assert "read-only" in result.output
    route.assert_not_called()


def test_run_execute_batch_ack_returns_migration_blocker_without_routing(
    tmp_path: Path,
) -> None:
    _init_project_repo(tmp_path)

    with (
        patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.run_cmd.route_five_loops") as route,
    ):
        result = runner.invoke(
            app,
            ["run", "--acknowledge-execute-batch", "--yes"],
        )

    assert result.exit_code == 1
    assert "retired seven-stage runner" in result.output
    assert "Implementation Loop" in result.output
    route.assert_not_called()


def _init_project_repo(root: Path) -> None:
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "run@example.com")
    _git(root, "config", "user.name", "Run Test")
    (root / ".ai-sdlc").mkdir()
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")


def _repository_state(root: Path, checkpoint: Path) -> tuple[str, str, str, bytes]:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "write-tree"),
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        checkpoint.read_bytes(),
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip())
    return result.stdout.strip()

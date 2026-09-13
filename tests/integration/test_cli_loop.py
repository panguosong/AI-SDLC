"""Integration tests for the ai-sdlc loop CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

import ai_sdlc.core.design_contract_loop as design_contract_loop
import ai_sdlc.core.frontend_evidence_loop as frontend_evidence_loop
import ai_sdlc.core.implementation_loop as implementation_loop
import ai_sdlc.core.requirement_loop as requirement_loop
from ai_sdlc.cli.loop_review_cmd import (
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.cli.main import app
from ai_sdlc.core.design_contract_models import DesignContractCheckOptions
from ai_sdlc.core.implementation_loop import record_implementation_progress
from ai_sdlc.core.implementation_models import (
    ImplementationClose,
    ImplementationCurrentPointer,
    ImplementationRecordOptions,
    ImplementationReport,
)
from ai_sdlc.core.implementation_store import implementation_artifacts
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRound, LoopRun, LoopStatus, LoopType
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.loop_status import CURRENT_REVIEW_PATH
from ai_sdlc.core.pr_review_models import (
    ModelResolutionSource,
    ModelResolutionStatus,
    ProviderMode,
    ReviewRun,
    ReviewVerdict,
)
from ai_sdlc.core.requirement_loop import (
    RequirementFreezeOptions,
    RequirementStartOptions,
    freeze_requirement_loop,
    start_requirement_loop,
)

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("isolated_cli_cwd")


def _review_close_args(root: Path, loop_type: str, loop_id: str) -> list[str]:
    review_input = _record_clean_review(root, loop_type, loop_id)
    return [
        "--loop-id",
        loop_id,
        "--expect-review-digest",
        review_input.input_digest,
    ]


def _record_clean_review(root: Path, loop_type: str, loop_id: str):
    review_input = resolve_review_input(
        root,
        loop_type=loop_type,
        loop_id=loop_id,
        review_round_number=1,
    )
    loop_dir = root / ".ai-sdlc" / "loops" / loop_type / loop_id
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type=loop_type,
        round_number=1,
        input_digest=review_input.input_digest,
        status="completed",
        expert_roles=review_input.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (loop_dir / "review-outcome-round-1.json").write_text(
        outcome.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return review_input


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation is not portable")
def test_loop_review_rejects_linked_current_pointer_before_parsing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "pointer-target.json"
    target.write_text("{}", encoding="utf-8")
    pointer = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "current-review.json"
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        resolve_review_input(
            tmp_path,
            loop_type="local-pr-review",
            loop_id="review-linked-pointer",
        )


def test_loop_help_lists_status_and_list() -> None:
    result = runner.invoke(app, ["loop", "--help"])

    assert result.exit_code == 0
    assert "status" in result.output
    assert "list" in result.output


def test_loop_status_json_reads_current_review(tmp_path: Path) -> None:
    review_run_path = _write_review_run(tmp_path)
    _write_current_pointer(tmp_path, review_run_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["result"] == "Current loop found."
    assert payload["current_loop"]["loop_type"] == "local-pr-review"
    assert payload["current_loop"]["status"] == "needs_fix"
    assert payload["current_loop"]["is_current"] is True
    assert payload["current_loop"]["local_pr_review"]["review_id"] == "review-001"
    assert payload["next_guidance"]["command"] == "ai-sdlc pr-review fix"
    assert payload["next_guidance"]["requires_model"] is False
    assert payload["next_guidance"]["writes_artifacts"] is True
    assert payload["current_loop"]["next_guidance"]["command"] == (
        "ai-sdlc pr-review fix"
    )


def test_loop_status_json_guides_post_fix_review_to_rerun(tmp_path: Path) -> None:
    review_run_path = _write_review_run(
        tmp_path,
        next_action=(
            "Fix BLOCKER/REQUIRED findings, update resolution.yaml, then run "
            "ai-sdlc pr-review rerun."
        ),
    )
    _write_current_pointer(tmp_path, review_run_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["current_loop"]["status"] == "needs_fix"
    assert payload["next_guidance"]["command"] == "ai-sdlc pr-review rerun"
    assert payload["next_guidance"]["requires_model"] is True
    assert payload["next_guidance"]["safety"] == "may_call_local_review_agent"
    assert (
        ".ai-sdlc/reviews/pr/review-001/resolution.yaml"
        in (payload["next_guidance"]["evidence"])
    )


def test_loop_status_human_includes_review_and_artifacts(tmp_path: Path) -> None:
    review_run_path = _write_review_run(tmp_path)
    _write_current_pointer(tmp_path, review_run_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status"])

    assert result.exit_code == 0
    assert "Result: ready" in result.output
    assert "Next: Run ai-sdlc pr-review fix." in result.output
    assert "Loop type: local-pr-review" in result.output
    assert "Review ID: review-001" in result.output
    assert "Loop next: Run ai-sdlc pr-review fix." in result.output
    assert "Next command: ai-sdlc pr-review fix" in result.output
    assert "Why:" in result.output
    assert "Model call: no" in result.output
    assert "Writes artifacts: yes" in result.output
    assert "Writes code: no" in result.output
    assert "Evidence:" in result.output
    assert "Unresolved: blockers=1, required=0, advisory=0" in result.output
    assert "Artifacts:" in result.output
    assert ".ai-sdlc/reviews/pr/review-001/review-run.json" in result.output


def test_loop_list_human_includes_each_loop_next_action(tmp_path: Path) -> None:
    _write_review_run(tmp_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list"])

    assert result.exit_code == 0
    assert "Loop 1" in result.output
    assert "Loop next: Run ai-sdlc pr-review fix." in result.output
    assert "Next command: ai-sdlc loop list --json" in result.output
    assert "non-current review run" in result.output


def test_loop_status_human_runs_update_notice(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / ".ai-sdlc").mkdir()
    calls: list[bool] = []
    monkeypatch.setattr(
        "ai_sdlc.cli.main.maybe_render_update_notice",
        lambda *, machine_output: calls.append(machine_output),
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status"])

    assert result.exit_code == 0
    assert calls == [False]


def test_loop_status_json_runs_machine_update_notice(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".ai-sdlc").mkdir()
    monkeypatch.setattr(sys, "argv", ["ai-sdlc", "loop", "status", "--json"])
    calls: list[bool] = []
    monkeypatch.setattr(
        "ai_sdlc.cli.main.maybe_render_update_notice",
        lambda *, machine_output: calls.append(machine_output),
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 0
    assert calls == [True]
    payload = json.loads(result.output)
    assert payload["status"] == "no_current"
    assert payload["result"] == "No current loop."


def test_loop_status_does_not_trigger_ide_adapter_hook(tmp_path: Path) -> None:
    (tmp_path / ".ai-sdlc").mkdir()
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 0
    adapter_hook.assert_not_called()


def test_loop_requirement_status_does_not_trigger_ide_adapter_hook(
    tmp_path: Path,
) -> None:
    (tmp_path / ".ai-sdlc").mkdir()
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(app, ["loop", "requirement", "status", "--json"])

    assert result.exit_code == 0
    adapter_hook.assert_not_called()


def test_loop_requirement_start_triggers_ide_adapter_hook(
    tmp_path: Path,
) -> None:
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-adapter-start",
                "--idea",
                "Ops users need approval reporting.",
                "--acceptance",
                "Approval report can be exported.",
                "--json",
            ],
        )

    assert result.exit_code == 0
    adapter_hook.assert_called_once()


def test_loop_requirement_start_json_suppresses_adapter_notice(
    tmp_path: Path,
) -> None:
    def _emit_notice(*, console) -> None:
        console.print("IDE adapter refreshed")

    with (
        patch(
            "ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized",
            side_effect=_emit_notice,
        ) as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-adapter-json",
                "--idea",
                "Ops users need approval reporting.",
                "--acceptance",
                "Approval report can be exported.",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["loop_id"] == "req-adapter-json"
    assert "IDE adapter refreshed" not in result.output
    adapter_hook.assert_called_once()


def test_loop_requirement_start_dry_run_skips_adapter_hook_and_reports_source(
    tmp_path: Path,
) -> None:
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-dry-run-json",
                "--idea",
                "Ops users need approval reporting.",
                "--acceptance",
                "Approval report can be exported.",
                "--dry-run",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["source_kind"] == "idea"
    assert payload["source_path"] == ""
    assert payload["requirement"]["summary"] == "Ops users need approval reporting."
    assert payload["requirement"]["source_kind"] == "idea"
    assert not (tmp_path / ".ai-sdlc").exists()
    adapter_hook.assert_not_called()


def test_loop_requirement_start_unknown_scope_returns_structured_json(
    tmp_path: Path,
) -> None:
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--idea",
                "Build a service.",
                "--acceptance",
                "Service works.",
                "--design-scope-family",
                "unknown",
                "--dry-run",
                "--json",
            ],
        )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["result"] == "Requirement input is invalid."
    assert "unknown design scope families: unknown" in payload["blocker"]
    adapter_hook.assert_not_called()


def test_loop_requirement_freeze_triggers_ide_adapter_hook(
    tmp_path: Path,
) -> None:
    with (
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        start = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-adapter-freeze",
                "--idea",
                "Ops users need approval reporting.",
                "--acceptance",
                "Approval report can be exported.",
                "--json",
            ],
        )
        adapter_hook.reset_mock()
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                *_review_close_args(tmp_path, "requirement", "req-adapter-freeze"),
                "--yes",
                "--json",
            ],
        )

    assert start.exit_code == 0
    assert result.exit_code == 0
    adapter_hook.assert_called_once()


def test_loop_requirement_freeze_json_suppresses_adapter_notice(
    tmp_path: Path,
) -> None:
    def _emit_notice(*, console) -> None:
        console.print("IDE adapter refreshed")

    with (
        patch(
            "ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized",
            side_effect=_emit_notice,
        ) as adapter_hook,
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
    ):
        start = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-adapter-freeze-json",
                "--idea",
                "Ops users need approval reporting.",
                "--acceptance",
                "Approval report can be exported.",
                "--json",
            ],
        )
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                *_review_close_args(tmp_path, "requirement", "req-adapter-freeze-json"),
                "--yes",
                "--json",
            ],
        )

    assert start.exit_code == 0
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["loop_status"] == "closed"
    assert "IDE adapter refreshed" not in start.output
    assert "IDE adapter refreshed" not in result.output
    assert adapter_hook.call_count == 2


def test_loop_status_guidance_does_not_call_provider(tmp_path: Path) -> None:
    review_run_path = _write_review_run(tmp_path)
    _write_current_pointer(tmp_path, review_run_path)

    with (
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
        patch(
            "ai_sdlc.core.pr_review_provider.run_provider_command"
        ) as provider_runner,
    ):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 0
    provider_runner.assert_not_called()


def test_loop_list_json_reads_runs_and_reports_malformed(
    tmp_path: Path,
) -> None:
    older_path = _write_review_run(
        tmp_path,
        review_id="review-001",
        loop_id="loop-review-001",
        updated_at="2026-06-29T01:00:00Z",
    )
    _write_review_run(
        tmp_path,
        review_id="review-002",
        loop_id="loop-review-002",
        updated_at="2026-06-30T01:00:00Z",
    )
    _write_current_pointer(tmp_path, older_path)
    bad_dir = LoopArtifactStore(tmp_path).review_run_dir("review-bad")
    bad_dir.mkdir(parents=True)
    (bad_dir / "review-run.json").write_text("{not-json", encoding="utf-8")

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == "loop-review-001"
    assert payload["current_review_id"] == "review-001"
    assert payload["malformed_count"] == 1
    assert payload["next_guidance"]["command"] == "ai-sdlc loop status"
    assert [loop["loop_id"] for loop in payload["items"]] == [
        "loop-review-002",
        "loop-review-001",
    ]
    assert payload["items"][0]["next_guidance"]["command"] == "ai-sdlc loop list --json"
    assert payload["items"][0]["next_guidance"]["writes_artifacts"] is False
    assert "non-current review run" in payload["items"][0]["next_guidance"]["reason"]
    assert payload["items"][0]["is_current"] is False
    assert payload["items"][1]["is_current"] is True
    assert payload["artifact_errors"][0]["path"] == (
        ".ai-sdlc/reviews/pr/review-bad/review-run.json"
    )


def test_loop_list_json_guides_no_current_pointer_with_history_to_doctor(
    tmp_path: Path,
) -> None:
    _write_review_run(tmp_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == ""
    assert payload["blocker"] == ""
    assert payload["next_action"] == "Run ai-sdlc pr-review start --base <branch>."
    assert payload["next_guidance"]["command"] == (
        "ai-sdlc pr-review doctor --base <branch>"
    )
    assert payload["next_guidance"]["requires_model"] is False
    assert payload["next_guidance"]["writes_artifacts"] is False
    assert (
        "ai-sdlc pr-review start --base <branch>"
        in (payload["next_guidance"]["alternatives"])
    )
    assert payload["items"][0]["next_guidance"]["command"] == (
        "ai-sdlc loop list --json"
    )


def test_loop_list_json_reports_malformed_current_pointer(
    tmp_path: Path,
) -> None:
    _write_review_run(tmp_path)
    pointer_path = tmp_path / CURRENT_REVIEW_PATH
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text("{not-json", encoding="utf-8")

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == ""
    assert payload["malformed_count"] == 1
    assert payload["artifact_errors"][0]["kind"] == "current-review-pointer"
    assert payload["artifact_errors"][0]["path"] == (
        ".ai-sdlc/reviews/pr/current-review.json"
    )
    assert payload["blocker"] == (
        "Current review pointer is malformed or references missing artifacts."
    )
    assert payload["next_action"] == (
        "Inspect or remove malformed current-review.json artifacts."
    )
    assert payload["next_guidance"]["safety"] == "blocked"
    assert payload["next_guidance"]["command"] == (
        "ai-sdlc pr-review start --base <branch>"
    )
    assert (
        ".ai-sdlc/reviews/pr/current-review.json"
        in (payload["next_guidance"]["evidence"])
    )


def test_loop_list_json_reports_missing_current_pointer_target(
    tmp_path: Path,
) -> None:
    _write_review_run(tmp_path)
    missing_path = (
        tmp_path / ".ai-sdlc" / "reviews" / "pr" / "missing" / "review-run.json"
    )
    _write_current_pointer(tmp_path, missing_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == ""
    assert payload["malformed_count"] == 1
    assert payload["artifact_errors"][0]["kind"] == "current-review-target"
    assert payload["artifact_errors"][0]["path"] == (
        ".ai-sdlc/reviews/pr/missing/review-run.json"
    )
    assert payload["next_guidance"]["safety"] == "blocked"
    assert (
        ".ai-sdlc/reviews/pr/missing/review-run.json"
        in (payload["next_guidance"]["evidence"])
    )


def test_loop_list_json_reports_malformed_current_review_run_guidance(
    tmp_path: Path,
) -> None:
    _write_review_run(tmp_path)
    bad_dir = LoopArtifactStore(tmp_path).review_run_dir("review-bad-current")
    bad_dir.mkdir(parents=True)
    bad_path = bad_dir / "review-run.json"
    bad_path.write_text("{not-json", encoding="utf-8")
    _write_current_pointer(tmp_path, bad_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == ""
    assert payload["malformed_count"] == 1
    assert payload["artifact_errors"][0]["kind"] == "review-run"
    assert payload["artifact_errors"][0]["path"] == (
        ".ai-sdlc/reviews/pr/review-bad-current/review-run.json"
    )
    assert payload["next_guidance"]["safety"] == "blocked"
    assert (
        ".ai-sdlc/reviews/pr/review-bad-current/review-run.json"
        in (payload["next_guidance"]["evidence"])
    )


def test_loop_list_json_reports_current_pointer_error_without_runs(
    tmp_path: Path,
) -> None:
    pointer_path = tmp_path / CURRENT_REVIEW_PATH
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text("{not-json", encoding="utf-8")

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "list", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["malformed_count"] == 1
    assert payload["artifact_errors"][0]["kind"] == "current-review-pointer"


def test_loop_status_json_reports_missing_project() -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=None):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert ".ai-sdlc is missing" in payload["blocker"]
    assert "ai-sdlc init" in payload["next_action"]


def test_loop_status_json_blocks_absolute_current_pointer_path(
    tmp_path: Path,
) -> None:
    pointer_path = tmp_path / CURRENT_REVIEW_PATH
    LoopArtifactStore(tmp_path).write_json_artifact(
        pointer_path,
        {
            "review_id": "review-001",
            "loop_id": "loop-review-001",
            "review_run_path": str(tmp_path.parent / "outside-review-run.json"),
        },
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(app, ["loop", "status", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert "project-relative" in payload["blocker"]


def test_loop_requirement_start_status_and_freeze_json(tmp_path: Path) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-cli",
                "--idea",
                "运营用户需要订单审批流，范围只覆盖后台人工审批。",
                "--acceptance",
                "审批节点可以配置",
                "--json",
            ],
        )
        status = runner.invoke(
            app,
            ["loop", "status", "--type", "requirement", "--json"],
        )
        freeze = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                *_review_close_args(tmp_path, "requirement", "req-cli"),
                "--yes",
                "--json",
            ],
        )

    assert start.exit_code == 0
    start_payload = json.loads(start.output)
    assert start_payload["status"] == "ready"
    assert start_payload["loop_id"] == "req-cli"
    assert start_payload["acceptance_count"] == 1
    assert start_payload["source_kind"] == "idea"
    assert start_payload["source_path"] == ""
    assert start_payload["requirement"]["summary"] == (
        "运营用户需要订单审批流，范围只覆盖后台人工审批。"
    )
    assert start_payload["requirement"]["source_kind"] == "idea"

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["status"] == "ready"
    assert status_payload["current_loop"]["loop_type"] == "requirement"
    assert status_payload["current_loop"]["requirement"]["acceptance_count"] == 1
    assert status_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type requirement --loop-id req-cli"
    )

    assert freeze.exit_code == 0
    freeze_payload = json.loads(freeze.output)
    assert freeze_payload["status"] == "ready"
    assert freeze_payload["loop_status"] == "closed"
    assert "design-contract" in freeze_payload["next_action"]


def test_loop_requirement_freeze_uses_reviewed_state_across_aba(
    tmp_path: Path,
) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-aba",
                "--idea",
                "Operations needs an approval report.",
                "--json",
            ],
        )

    assert start.exit_code == 0
    reviewed = _record_clean_review(tmp_path, "requirement", "req-aba")
    reviewed_bytes = {
        path: (tmp_path / path).read_bytes() for path in reviewed.artifact_paths
    }
    validation_count = 0

    def validate_then_add_acceptance(*args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        result = validate_review_input_for_close(*args, **kwargs)
        if validation_count == 1:
            updated = start_requirement_loop(
                RequirementStartOptions(
                    root=tmp_path,
                    loop_id="req-aba",
                    idea="Operations needs an approval report.",
                    acceptance=("The report includes every approval decision.",),
                )
            )
            assert updated.status == "ready"
        return result

    original_revalidate = requirement_loop.revalidate_review_input_at_transition

    def restore_then_revalidate(*args, **kwargs):
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)
        return original_revalidate(*args, **kwargs)

    try:
        with (
            patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
            patch(
                "ai_sdlc.cli.loop_cmd.validate_review_input_for_close",
                side_effect=validate_then_add_acceptance,
            ),
            patch(
                "ai_sdlc.core.requirement_loop.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "loop",
                    "requirement",
                    "freeze",
                    "--loop-id",
                    "req-aba",
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--yes",
                    "--json",
                ],
            )
    finally:
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "needs_user"
    assert payload["acceptance_count"] == 0


def test_loop_requirement_start_human_needs_user_without_acceptance(
    tmp_path: Path,
) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-human",
                "--idea",
                "做一个报表",
            ],
        )

    assert result.exit_code == 0
    assert "Result: needs_user" in result.output
    assert "Next:" in result.output
    assert "Loop ID: req-human" in result.output
    assert "Clarifications:" in result.output
    assert "Acceptance criteria: 0" in result.output


def test_loop_requirement_freeze_requires_yes(tmp_path: Path) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-freeze-cli",
                "--idea",
                "财务用户需要付款审批，范围只覆盖国内付款。",
                "--acceptance",
                "审批通过后才能付款",
            ],
        )
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                *_review_close_args(tmp_path, "requirement", "req-freeze-cli"),
                "--json",
            ],
        )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert "--yes" in payload["next_action"]


def test_loop_requirement_freeze_without_acceptance_exits_nonzero(
    tmp_path: Path,
) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-freeze-needs-user",
                "--idea",
                "Ops users need approval reporting.",
            ],
        )
        result = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                *_review_close_args(tmp_path, "requirement", "req-freeze-needs-user"),
                "--yes",
                "--json",
            ],
        )

    assert start.exit_code == 0
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "needs_user"
    assert "acceptance criterion" in payload["blocker"]
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-freeze-needs-user"
        / "requirement-freeze.json"
    ).exists()


def test_loop_requirement_list_json(tmp_path: Path) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-list",
                "--idea",
                "客服用户需要 SLA 提醒，范围只覆盖站内提醒。",
                "--acceptance",
                "SLA 超时前可以提醒",
            ],
        )
        result = runner.invoke(
            app,
            ["loop", "list", "--type", "requirement", "--json"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == "req-list"
    assert payload["items"][0]["requirement"]["summary"] == (
        "客服用户需要 SLA 提醒，范围只覆盖站内提醒。"
    )


def test_loop_design_contract_check_status_and_close_json(tmp_path: Path) -> None:
    _write_design_contract_work_item(tmp_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        check = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-cli",
                "--json",
            ],
        )
        status = runner.invoke(
            app,
            ["loop", "status", "--type", "design-contract", "--json"],
        )
        close = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-cli"),
                "--yes",
                "--json",
            ],
        )

    assert check.exit_code == 0
    check_payload = json.loads(check.output)
    assert check_payload["status"] == "ready"
    assert check_payload["loop_status"] == "needs_review"
    assert check_payload["design_contract"]["status"] == "needs_review"
    assert check_payload["design_contract"]["coverage_count"] == 2
    assert check_payload["design_contract"]["coverage_matrix_path"].endswith(
        ".ai-sdlc/loops/design-contract/dc-cli/coverage-matrix.json"
    )
    assert check_payload["design_contract"]["report_path"].endswith(
        ".ai-sdlc/loops/design-contract/dc-cli/design-contract-report.json"
    )
    assert check_payload["next_action"] == (
        "Run ai-sdlc loop review --type design-contract --loop-id dc-cli."
    )
    assert check_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type design-contract --loop-id dc-cli"
    )
    assert check_payload["next_guidance"]["requires_model"] is True
    assert check_payload["next_guidance"]["writes_artifacts"] is False
    assert check_payload["next_guidance"]["writes_code"] is False

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["status"] == "ready"
    assert status_payload["current_loop"]["loop_type"] == "design-contract"
    assert status_payload["current_loop"]["design_contract"]["blocker_count"] == 0
    assert status_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type design-contract --loop-id dc-cli"
    )

    assert close.exit_code == 0
    close_payload = json.loads(close.output)
    assert close_payload["status"] == "ready"
    assert close_payload["closed"] is True
    assert close_payload["loop_status"] == "closed"
    assert close_payload["design_contract"]["status"] == "closed"
    assert close_payload["next_action"] == (
        "Start implementation loop for demo-design-contract."
    )
    assert close_payload["next_guidance"]["safety"] == "no_action"
    assert close_payload["next_guidance"]["writes_artifacts"] is False


def test_loop_design_contract_check_dry_run_skips_adapter_hook(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(tmp_path)

    with (
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-dry-run",
                "--dry-run",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    assert payload["design_contract"]["status"] == "created"
    assert payload["design_contract"]["work_item_id"] == "demo-design-contract"
    assert payload["design_contract"]["coverage_matrix_path"].endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/coverage-matrix.json"
    )
    assert payload["design_contract"]["report_path"].endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/design-contract-report.json"
    )
    assert payload["next_guidance"]["command"] == (
        "ai-sdlc loop design-contract check --wi specs/demo-design-contract"
    )
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-dry-run"
    ).exists()
    adapter_hook.assert_not_called()


def test_loop_design_contract_close_with_blockers_exits_nonzero(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        check = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-blocked",
                "--json",
            ],
        )
        close = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-blocked"),
                "--yes",
                "--json",
            ],
        )

    assert check.exit_code == 0
    assert close.exit_code == 1
    payload = json.loads(close.output)
    assert payload["status"] == "needs_fix"
    assert payload["blocker_count"] >= 2


def test_loop_design_contract_close_uses_reviewed_state_across_aba(
    tmp_path: Path,
) -> None:
    work_item = _write_design_contract_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        check = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-aba",
                "--json",
            ],
        )

    assert check.exit_code == 0
    reviewed = _record_clean_review(tmp_path, "design-contract", "dc-aba")
    reviewed_bytes = {
        path: (tmp_path / path).read_bytes() for path in reviewed.artifact_paths
    }
    validation_count = 0

    def validate_then_pass_contract(*args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        result = validate_review_input_for_close(*args, **kwargs)
        if validation_count == 1:
            work_item.joinpath("tasks.md").write_text(
                "\n".join(
                    [
                        "# 任务分解",
                        "### Task 1.1 Check contract",
                        "- **任务编号**：T11",
                        "- **优先级**：P0",
                        "- **验收标准**：Cover FR-DEMO-001 and SC-DEMO-001.",
                        "- **验证**：uv run pytest tests/unit/test_demo.py -q",
                    ]
                ),
                encoding="utf-8",
            )
            refreshed = design_contract_loop.check_design_contract_loop(
                DesignContractCheckOptions(
                    root=tmp_path,
                    work_item="specs/demo-design-contract",
                    loop_id="dc-aba",
                )
            )
            assert refreshed.status == "ready"
        return result

    original_revalidate = design_contract_loop.revalidate_review_input_at_transition

    def restore_then_revalidate(*args, **kwargs):
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)
        return original_revalidate(*args, **kwargs)

    try:
        with (
            patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
            patch(
                "ai_sdlc.cli.loop_cmd.validate_review_input_for_close",
                side_effect=validate_then_pass_contract,
            ),
            patch(
                "ai_sdlc.core.design_contract_loop.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "loop",
                    "design-contract",
                    "close",
                    "--loop-id",
                    "dc-aba",
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--yes",
                    "--json",
                ],
            )
    finally:
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "needs_fix"
    assert payload["blocker_count"] >= 2


def test_loop_design_contract_close_rechecks_reviewed_document_snapshot(
    tmp_path: Path,
) -> None:
    work_item = _write_design_contract_work_item(tmp_path)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        check = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-reviewed-docs",
                "--json",
            ],
        )

    assert check.exit_code == 0
    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
            "- **验证**：",
        ),
        encoding="utf-8",
    )
    reviewed = _record_clean_review(
        tmp_path,
        "design-contract",
        "dc-reviewed-docs",
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                "--loop-id",
                "dc-reviewed-docs",
                "--expect-review-digest",
                reviewed.input_digest,
                "--yes",
                "--json",
            ],
        )

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["closed"] is False
    assert payload["status"] == "needs_fix"
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-reviewed-docs"
        / "design-contract-close.json"
    ).exists()


def test_loop_design_contract_list_json(tmp_path: Path) -> None:
    _write_design_contract_work_item(tmp_path)

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-list",
                "--json",
            ],
        )
        result = runner.invoke(
            app,
            ["loop", "list", "--type", "design-contract", "--json"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ready"
    assert payload["current_loop_id"] == "dc-list"
    assert payload["items"][0]["design_contract"]["work_item_id"] == (
        "demo-design-contract"
    )


def test_loop_implementation_start_record_status_and_close_json(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(tmp_path)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        design_check = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-impl-cli",
                "--json",
            ],
        )
        design_close = runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-impl-cli"),
                "--yes",
                "--json",
            ],
        )
        start = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "start",
                "--wi",
                "specs/demo-design-contract",
                "--design-contract-loop-id",
                "dc-impl-cli",
                "--loop-id",
                "impl-cli",
                "--json",
            ],
        )
        status = runner.invoke(
            app,
            ["loop", "status", "--type", "implementation", "--json"],
        )
        record = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "record",
                "--loop-id",
                "impl-cli",
                "--task-id",
                "T11",
                "--status",
                "done",
                "--verification",
                "uv run pytest tests/integration/test_cli_loop.py -q",
                "--json",
            ],
        )
        _ensure_git_repository(tmp_path)
        verify = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "verify",
                "--loop-id",
                "impl-cli",
                "--task-id",
                "T11",
                "--cwd",
                ".",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('verified')",
            ],
        )
        close = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "close",
                *_review_close_args(tmp_path, "implementation", "impl-cli"),
                "--yes",
                "--json",
            ],
        )

    assert design_check.exit_code == 0
    assert design_close.exit_code == 0
    assert start.exit_code == 0
    start_payload = json.loads(start.output)
    assert start_payload["status"] == "ready"
    assert start_payload["loop_status"] == "running"
    assert start_payload["implementation"]["required_task_count"] == 1
    assert start_payload["implementation"]["report_path"].endswith(
        ".ai-sdlc/loops/implementation/impl-cli/implementation-report.json"
    )

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["current_loop"]["loop_type"] == "implementation"
    assert status_payload["current_loop"]["implementation"]["done_count"] == 0

    assert record.exit_code == 0
    record_payload = json.loads(record.output)
    assert record_payload["loop_status"] == "running"
    assert record_payload["done_count"] == 0

    assert verify.exit_code == 0
    verify_payload = json.loads(verify.output)
    assert verify_payload["loop_status"] == "needs_review"
    assert verify_payload["done_count"] == 1
    assert verify_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type implementation --loop-id impl-cli"
    )

    assert close.exit_code == 0
    close_payload = json.loads(close.output)
    assert close_payload["closed"] is True
    assert close_payload["loop_status"] == "closed"
    assert close_payload["next_action"] == "Run ai-sdlc pr-review start."


def test_loop_implementation_close_uses_reviewed_state_across_aba(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(tmp_path)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-impl-aba",
                "--json",
            ],
        )
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-impl-aba"),
                "--yes",
                "--json",
            ],
        )
        start = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "start",
                "--wi",
                "specs/demo-design-contract",
                "--design-contract-loop-id",
                "dc-impl-aba",
                "--loop-id",
                "impl-aba",
                "--json",
            ],
        )

    assert start.exit_code == 0
    reviewed = _record_clean_review(tmp_path, "implementation", "impl-aba")
    reviewed_bytes = {
        path: (tmp_path / path).read_bytes() for path in reviewed.artifact_paths
    }
    validation_count = 0

    def validate_then_complete(*args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        result = validate_review_input_for_close(*args, **kwargs)
        if validation_count == 1:
            recorded = record_implementation_progress(
                ImplementationRecordOptions(
                    root=tmp_path,
                    loop_id="impl-aba",
                    task_id="T11",
                    status="done",
                    verification=("pytest -q",),
                )
            )
            assert recorded.status == "ready"
        return result

    original_revalidate = implementation_loop.revalidate_review_input_at_transition

    def restore_then_revalidate(*args, **kwargs):
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)
        return original_revalidate(*args, **kwargs)

    try:
        with (
            patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
            patch(
                "ai_sdlc.cli.loop_cmd.validate_review_input_for_close",
                side_effect=validate_then_complete,
            ),
            patch(
                "ai_sdlc.core.implementation_loop.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "loop",
                    "implementation",
                    "close",
                    "--loop-id",
                    "impl-aba",
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--yes",
                    "--json",
                ],
            )
    finally:
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["closed"] is False
    assert payload["status"] == "needs_fix"
    assert payload["blocker"] == "T11 is not done."


def test_loop_implementation_failed_close_persists_reviewed_decision(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(tmp_path)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-impl-needs-fix",
                "--json",
            ],
        )
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-impl-needs-fix"),
                "--yes",
                "--json",
            ],
        )
        start = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "start",
                "--wi",
                "specs/demo-design-contract",
                "--design-contract-loop-id",
                "dc-impl-needs-fix",
                "--loop-id",
                "impl-needs-fix",
                "--json",
            ],
        )

    assert start.exit_code == 0
    reviewed = _record_clean_review(
        tmp_path,
        "implementation",
        "impl-needs-fix",
    )
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "close",
                "--loop-id",
                "impl-needs-fix",
                "--expect-review-digest",
                reviewed.input_digest,
                "--yes",
                "--json",
            ],
        )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-needs-fix"
    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "needs_fix"
    assert (
        json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))["status"]
        == "needs_fix"
    )
    assert (
        json.loads(
            (loop_dir / "implementation-report.json").read_text(encoding="utf-8")
        )["status"]
        == "needs_fix"
    )


def test_loop_implementation_close_does_not_overwrite_newer_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_design_contract_work_item(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-impl-concurrent",
                "--json",
            ],
        )
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-impl-concurrent"),
                "--yes",
                "--json",
            ],
        )
        start = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "start",
                "--wi",
                "specs/demo-design-contract",
                "--design-contract-loop-id",
                "dc-impl-concurrent",
                "--loop-id",
                "impl-concurrent",
                "--json",
            ],
        )

    assert start.exit_code == 0
    reviewed = _record_clean_review(
        tmp_path,
        "implementation",
        "impl-concurrent",
    )
    validation_count = 0
    worker: threading.Thread | None = None
    worker_reached_write = threading.Event()
    allow_worker_write = threading.Event()
    worker_finished = threading.Event()
    worker_errors: list[BaseException] = []
    original_write_artifacts = implementation_loop._write_artifacts

    def coordinated_write(*args, **kwargs):
        if threading.current_thread() is worker:
            worker_reached_write.set()
            assert allow_worker_write.wait(timeout=5)
        return original_write_artifacts(*args, **kwargs)

    def complete_task() -> None:
        try:
            recorded = record_implementation_progress(
                ImplementationRecordOptions(
                    root=tmp_path,
                    loop_id="impl-concurrent",
                    task_id="T11",
                    status="done",
                    verification=("pytest -q",),
                )
            )
            assert recorded.status == "ready"
        except BaseException as exc:
            worker_errors.append(exc)
        finally:
            worker_finished.set()

    def validate_then_complete(*args, **kwargs):
        nonlocal validation_count, worker
        validation_count += 1
        result = validate_review_input_for_close(*args, **kwargs)
        if validation_count == 2:
            worker = threading.Thread(target=complete_task)
            worker.start()
            reached_before_close_write = worker_reached_write.wait(timeout=0.25)
            allow_worker_write.set()
            if reached_before_close_write:
                assert worker_finished.wait(timeout=5)
        return result

    monkeypatch.setattr(
        implementation_loop,
        "_write_artifacts",
        coordinated_write,
    )
    with (
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
        patch(
            "ai_sdlc.cli.loop_cmd.validate_review_input_for_close",
            side_effect=validate_then_complete,
        ),
    ):
        close = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "close",
                "--loop-id",
                "impl-concurrent",
                "--expect-review-digest",
                reviewed.input_digest,
                "--yes",
                "--json",
            ],
        )

    assert worker is not None
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert worker_errors == []
    progress_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "implementation"
        / "impl-concurrent"
        / "implementation-progress.json"
    )
    progress_payload = json.loads(progress_path.read_text(encoding="utf-8"))
    assert close.exit_code == 1
    assert progress_payload["tasks"][0]["status"] == "done"
    assert progress_payload["tasks"][0]["verification_commands"] == ["pytest -q"]
    assert not (tmp_path / ".ai-sdlc" / "locks").exists()


def test_loop_implementation_start_dry_run_skips_adapter_hook(
    tmp_path: Path,
) -> None:
    _write_design_contract_work_item(tmp_path)
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "check",
                "--wi",
                "specs/demo-design-contract",
                "--loop-id",
                "dc-impl-dry-run",
                "--json",
            ],
        )
        runner.invoke(
            app,
            [
                "loop",
                "design-contract",
                "close",
                *_review_close_args(tmp_path, "design-contract", "dc-impl-dry-run"),
                "--yes",
                "--json",
            ],
        )
    with (
        patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
        patch("ai_sdlc.cli.loop_cmd.run_ide_adapter_if_initialized") as adapter_hook,
    ):
        result = runner.invoke(
            app,
            [
                "loop",
                "implementation",
                "start",
                "--wi",
                "specs/demo-design-contract",
                "--design-contract-loop-id",
                "dc-impl-dry-run",
                "--loop-id",
                "impl-dry-run",
                "--dry-run",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["implementation"]["required_task_count"] == 1
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "implementation" / "impl-dry-run"
    ).exists()
    adapter_hook.assert_not_called()


def test_loop_frontend_evidence_start_status_and_close_json(
    tmp_path: Path,
) -> None:
    work_item = _write_frontend_work_item(tmp_path)
    _write_closed_frontend_implementation(tmp_path, work_item)
    _write_frontend_browser_gate_artifact(
        tmp_path, work_item_path="specs/demo-frontend"
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "start",
                "--wi",
                "specs/demo-frontend",
                "--implementation-loop-id",
                "impl-frontend-cli",
                "--loop-id",
                "fe-cli",
                "--json",
            ],
        )
        status = runner.invoke(
            app,
            ["loop", "status", "--type", "frontend-evidence", "--json"],
        )
        close = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "close",
                *_review_close_args(tmp_path, "frontend-evidence", "fe-cli"),
                "--yes",
                "--json",
            ],
        )

    assert start.exit_code == 0
    start_payload = json.loads(start.output)
    assert start_payload["status"] == "ready"
    assert start_payload["loop_status"] == "needs_review"
    assert start_payload["frontend_evidence"]["gate_run_id"] == "gate-run-cli"
    assert start_payload["frontend_evidence"]["report_path"].endswith(
        ".ai-sdlc/loops/frontend-evidence/fe-cli/frontend-evidence-report.json"
    )

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["current_loop"]["loop_type"] == "frontend-evidence"
    assert status_payload["current_loop"]["frontend_evidence"]["work_item_id"] == (
        "demo-frontend"
    )
    assert status_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type frontend-evidence --loop-id fe-cli"
    )

    assert close.exit_code == 0
    close_payload = json.loads(close.output)
    assert close_payload["closed"] is True
    assert close_payload["loop_status"] == "closed"
    assert close_payload["next_action"] == "Run ai-sdlc pr-review start."


def test_loop_frontend_evidence_start_needs_fix_exits_nonzero(
    tmp_path: Path,
) -> None:
    work_item = _write_frontend_work_item(tmp_path)
    _write_closed_frontend_implementation(tmp_path, work_item)
    _write_frontend_browser_gate_artifact(
        tmp_path, work_item_path="specs/demo-frontend"
    )
    artifact_path = (
        tmp_path
        / ".ai-sdlc"
        / "memory"
        / "frontend-delivery"
        / "browser"
        / "latest.yaml"
    )
    payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    payload["overall_gate_status"] = "incomplete"
    payload["bundle_input"]["overall_gate_status"] = "incomplete"
    payload["bundle_input"]["smoke_verdict"] = "evidence_missing"
    payload["bundle_input"]["blocking_reason_codes"] = [
        "playwright_probe_evidence_missing"
    ]
    payload["bundle_input"]["check_receipts"][0].update(
        {
            "runtime_status": "incomplete",
            "classification_candidate": "evidence_missing",
            "recheck_required": True,
            "blocking_reason_codes": ["playwright_probe_evidence_missing"],
            "remediation_hints": ["rerun browser gate probe"],
        }
    )
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "start",
                "--wi",
                "specs/demo-frontend",
                "--implementation-loop-id",
                "impl-frontend-cli",
                "--loop-id",
                "fe-cli-needs-fix",
                "--json",
            ],
        )

    assert start.exit_code == 1
    start_payload = json.loads(start.output)
    assert start_payload["status"] == "needs_fix"
    assert start_payload["loop_status"] == "needs_fix"
    assert start_payload["blocker_count"] >= 1


def test_loop_frontend_evidence_close_uses_reviewed_state_across_aba(
    tmp_path: Path,
) -> None:
    work_item = _write_frontend_work_item(tmp_path)
    _write_closed_frontend_implementation(tmp_path, work_item)
    _write_frontend_browser_gate_artifact(
        tmp_path, work_item_path="specs/demo-frontend"
    )
    artifact_path = (
        tmp_path
        / ".ai-sdlc"
        / "memory"
        / "frontend-delivery"
        / "browser"
        / "latest.yaml"
    )
    gate_payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    gate_payload["overall_gate_status"] = "incomplete"
    gate_payload["bundle_input"]["overall_gate_status"] = "incomplete"
    gate_payload["bundle_input"]["smoke_verdict"] = "evidence_missing"
    gate_payload["bundle_input"]["blocking_reason_codes"] = [
        "playwright_probe_evidence_missing"
    ]
    artifact_path.write_text(
        yaml.safe_dump(gate_payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "start",
                "--wi",
                "specs/demo-frontend",
                "--implementation-loop-id",
                "impl-frontend-cli",
                "--loop-id",
                "fe-aba",
                "--json",
            ],
        )

    assert start.exit_code == 1
    reviewed = _record_clean_review(tmp_path, "frontend-evidence", "fe-aba")
    reviewed_bytes = {
        path: (tmp_path / path).read_bytes() for path in reviewed.artifact_paths
    }
    validation_count = 0

    def validate_then_pass_report(*args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        result = validate_review_input_for_close(*args, **kwargs)
        if validation_count == 1:
            report_path = (
                tmp_path
                / ".ai-sdlc"
                / "loops"
                / "frontend-evidence"
                / "fe-aba"
                / "frontend-evidence-report.json"
            )
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            report_payload.update(
                {
                    "status": "passed",
                    "blocker_count": 0,
                    "warning_count": 0,
                    "blockers": [],
                    "warnings": [],
                    "blocking_reason_codes": [],
                    "advisory_reason_codes": [],
                }
            )
            report_path.write_text(
                json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return result

    original_revalidate = frontend_evidence_loop.revalidate_review_input_at_transition

    def restore_then_revalidate(*args, **kwargs):
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)
        return original_revalidate(*args, **kwargs)

    try:
        with (
            patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path),
            patch(
                "ai_sdlc.cli.loop_cmd.validate_review_input_for_close",
                side_effect=validate_then_pass_report,
            ),
            patch(
                "ai_sdlc.core.frontend_evidence_loop.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "loop",
                    "frontend-evidence",
                    "close",
                    "--loop-id",
                    "fe-aba",
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--yes",
                    "--json",
                ],
            )
    finally:
        for path, content in reviewed_bytes.items():
            (tmp_path / path).write_bytes(content)

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "needs_fix"
    assert payload["blocker_count"] >= 1


def test_loop_frontend_evidence_doctor_respects_codex_browser_provider(
    tmp_path: Path,
) -> None:
    (tmp_path / ".ai-sdlc").mkdir()

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "doctor",
                "--provider",
                "codex-browser",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "needs_user"
    assert payload["recommended_provider"] == "codex-browser"
    assert "Playwright" not in payload["next_action"]
    codex_provider = next(
        provider
        for provider in payload["providers"]
        if provider["provider_id"] == "codex-browser"
    )
    assert codex_provider["selected"] is True
    assert codex_provider["install_commands"] == []


def test_loop_frontend_evidence_doctor_playwright_provider_shows_install_commands(
    tmp_path: Path,
) -> None:
    (tmp_path / ".ai-sdlc").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "doctor",
                "--provider",
                "playwright",
            ],
        )

    assert result.exit_code == 0
    assert "Recommended provider: playwright" in result.output
    assert (
        "optional install: npm install -D @playwright/test pixelmatch pngjs"
        in result.output
    )
    assert "optional install: npx playwright install chromium" in result.output


def test_loop_frontend_evidence_skip_json_waits_for_review(
    tmp_path: Path,
) -> None:
    work_item = _write_frontend_work_item(tmp_path)
    _write_closed_frontend_implementation(tmp_path, work_item)
    reason = "User cannot install browser plugins or run a controlled browser locally."

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        skip = runner.invoke(
            app,
            [
                "loop",
                "frontend-evidence",
                "skip",
                "--wi",
                "specs/demo-frontend",
                "--implementation-loop-id",
                "impl-frontend-cli",
                "--loop-id",
                "fe-cli-skip",
                "--reason",
                reason,
                "--yes",
                "--json",
            ],
        )
        status = runner.invoke(
            app,
            ["loop", "status", "--type", "frontend-evidence", "--json"],
        )

    assert skip.exit_code == 0
    skip_payload = json.loads(skip.output)
    assert skip_payload["closed"] is False
    assert skip_payload["skipped"] is True
    assert skip_payload["skip_reason"] == reason
    assert skip_payload["loop_status"] == "needs_review"
    assert skip_payload["next_action"] == (
        "Run ai-sdlc loop review --type frontend-evidence --loop-id fe-cli-skip."
    )

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    frontend = status_payload["current_loop"]["frontend_evidence"]
    assert frontend["closed"] is False
    assert frontend["skipped"] is True
    assert frontend["skip_reason"] == reason
    assert status_payload["next_guidance"]["command"] == (
        "ai-sdlc loop review --type frontend-evidence --loop-id fe-cli-skip"
    )


def test_python_module_help_fallback_lists_loop() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0
    assert "loop" in result.stdout


def _write_frontend_work_item(root: Path) -> Path:
    work_item = root / "specs" / "demo-frontend"
    work_item.mkdir(parents=True)
    work_item.joinpath("spec.md").write_text("# Frontend Demo\n", encoding="utf-8")
    work_item.joinpath("plan.md").write_text("# Plan\n", encoding="utf-8")
    work_item.joinpath("tasks.md").write_text("# Tasks\n", encoding="utf-8")
    return work_item


def _ensure_git_repository(root: Path) -> None:
    if (root / ".git").exists():
        return
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "specs"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "test baseline"], cwd=root, check=True)


def _write_closed_frontend_implementation(root: Path, work_item: Path) -> None:
    artifacts = implementation_artifacts(root, "impl-frontend-cli")
    store = LoopArtifactStore(root)
    store.create_loop_run_dir(
        "impl-frontend-cli",
        loop_type=LoopType.IMPLEMENTATION.value,
    )
    design_dir = root / ".ai-sdlc" / "loops" / "design-contract" / "dc-frontend-cli"
    design_dir.mkdir(parents=True)
    store.write_json_artifact(
        design_dir / "design-contract-input.json",
        {
            "requirement_loop_id": "",
            "spec_path": f"specs/{work_item.name}/spec.md",
            "plan_path": f"specs/{work_item.name}/plan.md",
            "tasks_path": f"specs/{work_item.name}/tasks.md",
        },
    )
    store.write_json_artifact(design_dir / "design-contract-report.json", {})
    (design_dir / "design-contract-report.md").write_text(
        "# Design contract\n", encoding="utf-8"
    )
    store.write_json_artifact(
        artifacts.input_path,
        {"design_contract_loop_id": "dc-frontend-cli"},
    )
    store.write_json_artifact(artifacts.tasks_path, {})
    store.write_json_artifact(artifacts.progress_path, {})
    store.write_json_artifact(artifacts.evidence_path, {})
    artifacts.report_md_path.write_text("# Implementation\n", encoding="utf-8")
    report = ImplementationReport(
        loop_id="impl-frontend-cli",
        work_item_id=work_item.name,
        work_item_path=f"specs/{work_item.name}",
        status=LoopStatus.PASSED,
        required_task_count=1,
        done_count=1,
        requires_frontend_evidence=True,
        next_action=(
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item.name}."
        ),
    )
    loop_run = LoopRun(
        loop_id="impl-frontend-cli",
        loop_type=LoopType.IMPLEMENTATION,
        status=LoopStatus.CLOSED,
        work_item_id=work_item.name,
        current_round=1,
        rounds=[
            LoopRound(
                round_number=1,
                command=["ai-sdlc", "loop", "implementation", "start"],
                status=LoopStatus.CLOSED,
                result=LoopStatus.CLOSED,
            )
        ],
        next_action=(
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item.name}."
        ),
    )
    store.write_json_artifact(artifacts.report_json_path, report)
    store.write_json_artifact(artifacts.loop_run_path, loop_run)
    store.write_json_artifact(
        artifacts.close_path,
        ImplementationClose(
            loop_id="impl-frontend-cli",
            closed_at="2026-06-30T23:59:59Z",
            report_path=artifacts.report_json_path.relative_to(root).as_posix(),
            next_loop_type=LoopType.FRONTEND_EVIDENCE,
        ),
    )
    store.write_json_artifact(
        artifacts.pointer_path,
        ImplementationCurrentPointer(
            loop_id="impl-frontend-cli",
            loop_run_path=artifacts.loop_run_path.relative_to(root).as_posix(),
        ),
    )


def _write_frontend_browser_gate_artifact(
    root: Path,
    *,
    work_item_path: str,
) -> None:
    gate_run_id = "gate-run-cli"
    artifact_root = f".ai-sdlc/artifacts/frontend-browser-gate/{gate_run_id}"
    screenshot_ref = f"{artifact_root}/shared-runtime/navigation-screenshot.png"
    trace_ref = f"{artifact_root}/shared-runtime/playwright-trace.zip"
    interaction_ref = f"{artifact_root}/interaction/interaction-snapshot.json"
    source_artifact_ref = ".ai-sdlc/memory/frontend-delivery/apply/latest.yaml"
    required_probe_set = [
        "playwright_smoke",
        "visual_expectation",
        "basic_a11y",
        "interaction_anti_pattern_checks",
    ]
    artifact_records = [
        {
            "artifact_id": "smoke-screenshot",
            "gate_run_id": gate_run_id,
            "check_name": "playwright_smoke",
            "artifact_type": "navigation_screenshot",
            "artifact_ref": screenshot_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
        {
            "artifact_id": "smoke-trace",
            "gate_run_id": gate_run_id,
            "check_name": "playwright_smoke",
            "artifact_type": "playwright_trace",
            "artifact_ref": trace_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
        {
            "artifact_id": "interaction-snapshot",
            "gate_run_id": gate_run_id,
            "check_name": "interaction_anti_pattern_checks",
            "artifact_type": "interaction_snapshot",
            "artifact_ref": interaction_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
    ]
    for record in artifact_records:
        local_artifact_path = root / str(record["artifact_ref"])
        local_artifact_path.parent.mkdir(parents=True, exist_ok=True)
        local_artifact_path.write_text("frontend evidence artifact\n", encoding="utf-8")
    payload = {
        "generated_at": "2026-07-01T00:00:00Z",
        "apply_artifact_path": source_artifact_ref,
        "probe_runtime_state": "completed",
        "gate_run_id": gate_run_id,
        "artifact_root": artifact_root,
        "required_probe_set": required_probe_set,
        "execution_context": {
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-cli",
            "solution_snapshot_id": "solution-snapshot-cli",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-cli",
            "effective_provider": "public-primevue",
            "effective_style_pack": "modern-saas",
            "style_fidelity_status": "verified",
            "required_probe_set": required_probe_set,
            "browser_entry_ref": "managed/frontend/index.html",
        },
        "runtime_session": {
            "probe_runtime_session_id": "session-cli",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-cli",
            "solution_snapshot_id": "solution-snapshot-cli",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-cli",
            "browser_entry_ref": "managed/frontend/index.html",
            "artifact_root_ref": artifact_root,
            "status": "completed",
            "started_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:00:01Z",
            "finished_at": "2026-07-01T00:00:01Z",
        },
        "artifact_records": artifact_records,
        "bundle_input": {
            "bundle_id": "bundle-cli",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-cli",
            "solution_snapshot_id": "solution-snapshot-cli",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "source_artifact_ref": source_artifact_ref,
            "readiness_subject_id": "subject-cli",
            "playwright_trace_refs": [trace_ref],
            "screenshot_refs": [screenshot_ref],
            "check_receipts": [
                _browser_gate_receipt(
                    "playwright_smoke", ["smoke-screenshot", "smoke-trace"]
                ),
                _browser_gate_receipt("visual_expectation", ["smoke-screenshot"]),
                _browser_gate_receipt("basic_a11y", ["smoke-screenshot"]),
                _browser_gate_receipt(
                    "interaction_anti_pattern_checks",
                    ["interaction-snapshot"],
                ),
            ],
            "smoke_verdict": "pass",
            "visual_verdict": "pass",
            "a11y_verdict": "pass",
            "interaction_anti_pattern_verdict": "pass",
            "overall_gate_status": "passed",
            "generated_at": "2026-07-01T00:00:01Z",
        },
        "overall_gate_status": "passed",
        "warnings": [],
        "plain_language_blockers": [],
        "recommended_next_steps": [],
    }
    artifact_path = (
        root / ".ai-sdlc" / "memory" / "frontend-delivery" / "browser" / "latest.yaml"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _browser_gate_receipt(
    check_name: str, artifact_ids: list[str]
) -> dict[str, object]:
    return {
        "check_name": check_name,
        "started_at": "2026-07-01T00:00:00Z",
        "finished_at": "2026-07-01T00:00:01Z",
        "runtime_status": "completed",
        "artifact_ids": artifact_ids,
        "classification_candidate": "pass",
        "requirement_linkage": [f"browser_quality_gate:{check_name}"],
    }


def _write_design_contract_work_item(
    root: Path,
    *,
    include_task_refs: bool = True,
    verification_value: str = "uv run pytest tests/unit/test_demo.py -q",
    requirement_loop_id: str = "req-current",
) -> Path:
    work_item = root / "specs" / "demo-design-contract"
    work_item.mkdir(parents=True)
    work_item.joinpath("spec.md").write_text(
        "\n".join(
            [
                "# PRD：Demo Design Contract",
                "",
                "**状态**：已冻结",
                "",
                "## 需求",
                "",
                "- **FR-DEMO-001**：系统必须检查设计合同。",
                "",
                "## 成功标准",
                "",
                "- **SC-DEMO-001**：合同通过后可以进入实现。",
            ]
        ),
        encoding="utf-8",
    )
    work_item.joinpath("plan.md").write_text(
        "\n".join(
            [
                "# 实施计划",
                "## 技术背景",
                "Python runtime.",
                "## 阶段计划",
                "Phase 1.",
                "## 验证策略",
                "Run pytest.",
                "## 回退方式",
                "Revert the commit.",
            ]
        ),
        encoding="utf-8",
    )
    refs = "FR-DEMO-001 and SC-DEMO-001" if include_task_refs else "contract docs"
    work_item.joinpath("tasks.md").write_text(
        "\n".join(
            [
                "# 任务分解",
                "### Task 1.1 Check contract",
                "- **任务编号**：T11",
                "- **优先级**：P0",
                f"- **验收标准**：Cover {refs}.",
                f"- **验证**：{verification_value}",
            ]
        ),
        encoding="utf-8",
    )
    _ensure_frozen_requirement_loop(root, loop_id=requirement_loop_id)
    return work_item


def _ensure_frozen_requirement_loop(root: Path, *, loop_id: str) -> None:
    freeze_path = (
        root
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / loop_id
        / "requirement-freeze.json"
    )
    if freeze_path.is_file():
        return
    start_result = start_requirement_loop(
        RequirementStartOptions(
            root=root,
            loop_id=loop_id,
            idea="Demo users need a checked design contract.",
            acceptance=("The design contract can be checked before implementation.",),
        )
    )
    assert start_result.status == "ready"
    freeze_result = freeze_requirement_loop(
        RequirementFreezeOptions(root=root, loop_id=loop_id, yes=True)
    )
    assert freeze_result.frozen is True


def _write_review_run(
    root: Path,
    *,
    review_id: str = "review-001",
    loop_id: str = "loop-review-001",
    updated_at: str = "2026-06-30T00:00:00Z",
    next_action: str = "Run ai-sdlc pr-review fix.",
    resolution_path: str = "",
) -> Path:
    store = LoopArtifactStore(root)
    review_dir = store.create_review_run_dir(review_id)
    review_pack_path = review_dir / "review-pack.json"
    findings_path = review_dir / "findings.json"
    review_pack_path.write_text("{}\n", encoding="utf-8")
    findings_path.write_text("{}\n", encoding="utf-8")
    if resolution_path:
        (root / resolution_path).write_text("{}\n", encoding="utf-8")
    review_run = ReviewRun(
        review_id=review_id,
        loop_id=loop_id,
        status=LoopStatus.NEEDS_FIX,
        provider_id="mock-reviewer",
        provider_mode=ProviderMode.MOCK,
        model_selector="fixture",
        resolved_model="mock-reviewer",
        model_resolution_status=ModelResolutionStatus.RESOLVED,
        model_resolution_source=ModelResolutionSource.MOCK_FIXTURE,
        base_ref="main",
        head_ref="HEAD",
        base_commit="a" * 40,
        head_commit="b" * 40,
        review_pack_path=f".ai-sdlc/reviews/pr/{review_id}/review-pack.json",
        findings_path=f".ai-sdlc/reviews/pr/{review_id}/findings.json",
        resolution_path=resolution_path,
        verdict=ReviewVerdict.CHANGES_REQUIRED,
        unresolved_blockers=1,
        next_action=next_action,
        updated_at=updated_at,
    )
    return store.write_json_artifact(review_dir / "review-run.json", review_run)


def _write_current_pointer(root: Path, review_run_path: Path) -> Path:
    pointer_path = root / CURRENT_REVIEW_PATH
    LoopArtifactStore(root).write_json_artifact(
        pointer_path,
        {
            "review_id": "review-001",
            "loop_id": "loop-review-001",
            "review_run_path": review_run_path.relative_to(root).as_posix(),
        },
    )
    return pointer_path

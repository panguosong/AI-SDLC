"""Integration tests for read-only dynamic expert review inputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest
from click import Command, Group, Option
from typer.main import get_command
from typer.testing import CliRunner

import ai_sdlc.core.implementation_loop as implementation_loop
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.cli.main import app
from ai_sdlc.core.requirement_loop import (
    RequirementStartOptions,
    start_requirement_loop,
)
from ai_sdlc.core.review_kernel import ReviewExecution, ReviewFinding
from tests.unit.test_implementation_loop import (
    _close_design_contract_for_work_item,
    _record_successful_quality_result,
    _write_ready_work_item,
)

runner = CliRunner()
pytestmark = pytest.mark.usefixtures("isolated_cli_cwd")


@pytest.fixture
def b1_review_project(tmp_path: Path):
    from ai_sdlc.core.implementation_models import ImplementationStartOptions
    from ai_sdlc.core.loop_decision_models import DecisionPrepareInput
    from ai_sdlc.core.loop_decision_service import prepare_implementation_decision
    from tests.integration.test_quantified_implementation import (
        _ready_project,
        _request,
    )

    root = _ready_project(tmp_path / "project")
    result = implementation_loop.start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="b1-review",
            decision_mode="adaptive-quantified",
        )
    )
    assert result.status == "ready", result
    request = DecisionPrepareInput.model_validate_json(
        _request(root, tmp_path / "request.json").read_bytes()
    )
    preview = prepare_implementation_decision(root, "b1-review", request)
    prepared = prepare_implementation_decision(
        root,
        "b1-review",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    assert prepared.status == "prepared"
    return root, root / ".ai-sdlc/loops/implementation/b1-review", prepared.context


def test_b1_review_snapshot_binds_complete_same_read_material(b1_review_project):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, context = b1_review_project
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    snapshot = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    assert snapshot is not None
    material = {
        *snapshot.review_input.artifact_paths,
        *snapshot.review_input.upstream_context_paths,
    }
    assert set(snapshot.manifest) == material
    assert (loop_dir / "decision-context.json").relative_to(root).as_posix() in material
    assert set(source.path for source in context.sources) <= material
    assert "src/ai_sdlc/core/implementation_loop.py" in material
    assert not any(
        Path(path).name in {"loop-run.json", "implementation-close.json"}
        or Path(path).name.startswith("review-outcome-round-")
        for path in material
    )
    assert snapshot.context == context
    assert snapshot.manifest == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in material
    }
    assert before == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_b1_review_default_close_capture_includes_exact_context(b1_review_project):
    root, loop_dir, _ = b1_review_project
    captured = {}
    reviewed = resolve_review_input(
        root,
        loop_type="implementation",
        loop_id="b1-review",
        review_round_number=1,
        captured_artifacts=captured,
    )
    context_path = (loop_dir / "decision-context.json").relative_to(root).as_posix()
    run_path = (loop_dir / "loop-run.json").relative_to(root).as_posix()
    assert context_path in reviewed.artifact_paths
    assert captured[context_path] == (root / context_path).read_bytes()
    assert captured[run_path] == (root / run_path).read_bytes()
    assert run_path not in reviewed.artifact_paths


def test_b1_review_missing_context_requires_prepare(b1_review_project):
    root, loop_dir, _ = b1_review_project
    (loop_dir / "decision-context.json").unlink()
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=root):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "implementation",
                "--loop-id",
                "b1-review",
                "--json",
            ],
        )
    assert result.exit_code == 1, result.output
    assert "decision-context-missing" in result.output
    assert "decision-prepare" in result.output


@pytest.mark.parametrize("target", ["input", "run", "context"])
def test_b1_review_rejects_mixed_captured_identity(b1_review_project, target):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    path = (
        loop_dir
        / {
            "input": "implementation-input.json",
            "run": "loop-run.json",
            "context": "decision-context.json",
        }[target]
    )
    payload = json.loads(path.read_bytes())
    if target == "context":
        payload["implementation_input_digest"] = "sha256:" + "a" * 64
    else:
        payload.pop("decision_mode", None)
        payload.pop("decision_capability", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="decision"):
        command.resolve_b1_review_snapshot(root, "b1-review", 1)


def test_b1_review_captures_context_and_manifest_before_later_mutation(
    b1_review_project, monkeypatch
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, context = b1_review_project
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    original_bytes = source.read_bytes()
    original_builder = command.build_review_input

    def build_then_change(*args, **kwargs):
        reviewed = original_builder(*args, **kwargs)
        source.write_text("VALUE = 999\n", encoding="utf-8")
        (loop_dir / "decision-context.json").write_text("{}", encoding="utf-8")
        return reviewed

    monkeypatch.setattr(command, "build_review_input", build_then_change)
    snapshot = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    assert snapshot.context == context
    assert (
        snapshot.manifest[source.relative_to(root).as_posix()]
        == hashlib.sha256(original_bytes).hexdigest()
    )


def test_b1_review_captured_run_cannot_downgrade_identity(
    b1_review_project, monkeypatch
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    original_builder = command.build_review_input

    def change_then_build(*args, **kwargs):
        path = loop_dir / "loop-run.json"
        payload = json.loads(path.read_bytes())
        payload.pop("decision_mode")
        payload.pop("decision_capability")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(command, "build_review_input", change_then_build)
    with pytest.raises(ValueError, match="decision-identity"):
        command.resolve_b1_review_snapshot(root, "b1-review", 1)


def test_b1_review_ignores_derived_run_status_but_binds_actual_evidence(
    b1_review_project,
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    first = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    run_path = loop_dir / "loop-run.json"
    payload = json.loads(run_path.read_bytes())
    payload["status"] = "needs_review"
    payload["next_action"] = "Derived display only."
    run_path.write_text(json.dumps(payload), encoding="utf-8")
    second = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    assert second.review_input.input_digest == first.review_input.input_digest
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    third = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    assert third.review_input.input_digest != first.review_input.input_digest


def test_b1_review_rebinds_changed_source_without_rewriting_contract(b1_review_project):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    context_path = loop_dir / "decision-context.json"
    payload = json.loads(context_path.read_bytes())
    payload["sources"][0]["path"] = source.relative_to(root).as_posix()
    payload["sources"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    payload["context_digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key != "context_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    context_path.write_text(json.dumps(payload), encoding="utf-8")
    frozen_context = context_path.read_bytes()
    before = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    source.write_bytes(b"VALUE = 2\n")
    after = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    assert after.review_input.input_digest != before.review_input.input_digest
    assert after.context == before.context
    assert context_path.read_bytes() == frozen_context
    assert (
        after.manifest[source.relative_to(root).as_posix()]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_b1_review_late_snapshot_drift_returns_structured_error(
    b1_review_project, monkeypatch
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, _, _ = b1_review_project
    prepared = command.prepare_current_loop_review(root, "implementation", "b1-review")
    monkeypatch.setattr(command, "prepare_current_loop_review", lambda *args: prepared)

    def unavailable(*args):
        raise ValueError("decision-context-drift")

    monkeypatch.setattr(command, "resolve_b1_review_snapshot", unavailable)
    monkeypatch.setattr(command, "find_project_root", lambda: root)
    result = runner.invoke(
        app,
        [
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            "b1-review",
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert "decision-context-drift" in payload["detail"]


@pytest.mark.parametrize(
    "filename",
    ["loop-run.json", "design-contract-close.json", "review-outcome-round-1.json"],
)
def test_b1_review_rejects_derived_loop_state_as_source(b1_review_project, filename):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    path = (
        root / ".ai-sdlc/loops/design-contract/dc-demo-implementation-loop" / filename
    )
    if filename == "review-outcome-round-1.json":
        path.write_text("{}", encoding="utf-8")
    assert path.is_file()
    context_path = loop_dir / "decision-context.json"
    payload = json.loads(context_path.read_bytes())
    payload["sources"][0]["path"] = path.relative_to(root).as_posix()
    payload["sources"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload["context_digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key != "context_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    context_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="decision-source-derived-state-forbidden"):
        command.resolve_b1_review_snapshot(root, "b1-review", 1)


@pytest.mark.parametrize(
    "relative_path, forbidden",
    [
        (".ai-sdlc/loops/implementation/current-implementation.json", True),
        (".ai-sdlc/loops/design-contract/current-design-contract.json", True),
        (".ai-sdlc/loops/requirement/current-requirement.json", True),
        (".ai-sdlc/loops/frontend-evidence/current-frontend-evidence.json", True),
        ("business/current-implementation.json", False),
        (".ai-sdlc/loops/implementation/b1-review/current-business.json", False),
    ],
)
def test_b1_review_current_pointer_boundary(
    b1_review_project, relative_path, forbidden
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    context_path = loop_dir / "decision-context.json"
    payload = json.loads(context_path.read_bytes())
    payload["sources"][0]["path"] = relative_path
    payload["sources"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload["context_digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key != "context_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    context_path.write_text(json.dumps(payload), encoding="utf-8")
    if forbidden:
        with pytest.raises(ValueError, match="decision-source-derived-state-forbidden"):
            command.resolve_b1_review_snapshot(root, "b1-review", 1)
    else:
        snapshot = command.resolve_b1_review_snapshot(root, "b1-review", 1)
        assert (
            snapshot.manifest[relative_path]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )


def test_b1_review_read_path_returns_only_requested_same_snapshot_bytes(
    b1_review_project,
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, loop_dir, _ = b1_review_project
    snapshot = command.resolve_b1_review_snapshot(root, "b1-review", 1)
    relative = (loop_dir / "decision-context.json").relative_to(root).as_posix()
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=root):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "implementation",
                "--loop-id",
                "b1-review",
                "--expect-digest",
                snapshot.review_input.input_digest,
                "--read-path",
                relative,
                "--json",
            ],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["review_snapshot"]["path"] == relative
    assert payload["review_snapshot"]["encoding"] == "utf-8"
    assert (
        payload["review_snapshot"]["content"].encode() == (root / relative).read_bytes()
    )


def test_b1_review_reports_envelope_contract_without_changing_kernel(
    b1_review_project,
):
    root, _, _ = b1_review_project
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=root):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "implementation",
                "--loop-id",
                "b1-review",
                "--json",
            ],
        )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["decision_capability"] == "implementation-b1"
    assert payload["expert_result_format"] == "execution-and-b1-assessment"
    assert "B1Assessment" in payload["expert_result_guidance"]


def test_legacy_review_snapshot_remains_legacy_without_added_fields(tmp_path):
    import ai_sdlc.cli.loop_review_cmd as command
    from ai_sdlc.core.implementation_models import ImplementationStartOptions
    from tests.integration.test_quantified_implementation import _ready_project

    root = _ready_project(tmp_path / "project")
    result = implementation_loop.start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="legacy-review",
        )
    )
    assert result.status == "ready"
    original = command.resolve_review_input(
        root, loop_type="implementation", loop_id="legacy-review", review_round_number=1
    )
    assert command.resolve_b1_review_snapshot(root, "legacy-review", 1) is None
    again = command.resolve_review_input(
        root, loop_type="implementation", loop_id="legacy-review", review_round_number=1
    )
    assert original == again
    assert not any("decision-context" in path for path in again.artifact_paths)
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=root):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "implementation",
                "--loop-id",
                "legacy-review",
                "--json",
            ],
        )
    payload = json.loads(result.output)
    assert result.exit_code == 0, result.output
    assert "decision_capability" not in payload
    assert "expert_result_format" not in payload


_STAGE_POINTER_NAMES = {
    "requirement": "current-requirement.json",
    "design-contract": "current-design-contract.json",
    "implementation": "current-implementation.json",
    "frontend-evidence": "current-frontend-evidence.json",
}


@pytest.mark.parametrize(
    ("loop_type", "filenames", "excluded"),
    [
        (
            "requirement",
            [
                "requirement-intake.json",
                "requirement-brief.md",
                "clarification-questions.md",
                "acceptance-checklist.md",
            ],
            "requirement-freeze.json",
        ),
        (
            "design-contract",
            [
                "design-contract-input.json",
                "design-contract-report.json",
                "design-contract-report.md",
            ],
            "design-contract-close.json",
        ),
        (
            "implementation",
            [
                "implementation-report.json",
                "implementation-report.md",
                "verification-evidence.json",
                "implementation-input.json",
                "implementation-tasks.json",
                "implementation-progress.json",
            ],
            "implementation-close.json",
        ),
        (
            "frontend-evidence",
            [
                "frontend-evidence-snapshot.json",
                "frontend-evidence-report.json",
                "frontend-evidence-report.md",
                "frontend-evidence-input.json",
            ],
            "frontend-evidence-close.json",
        ),
    ],
)
def test_loop_review_maps_only_substantive_stage_artifacts(
    tmp_path: Path,
    loop_type: str,
    filenames: list[str],
    excluded: str,
) -> None:
    loop_id = f"{loop_type}-001"
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / loop_type / loop_id
    loop_dir.mkdir(parents=True)
    (loop_dir / "loop-run.json").write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": loop_type,
                "current_round": 2,
            }
        ),
        encoding="utf-8",
    )
    pointer = _write_stage_current_pointer(tmp_path, loop_type, loop_id)
    for filename in [*filenames, excluded]:
        content = "{}" if filename.endswith(".json") else f"{filename}\n"
        (loop_dir / filename).write_text(content, encoding="utf-8")
    expected_upstream = _write_predecessor_fixture(tmp_path, loop_type, loop_dir)

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                loop_type,
                "--loop-id",
                loop_id,
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["loop_id"] == loop_id
    assert payload["loop_type"] == loop_type
    assert payload["round_number"] == 1
    assert payload["review_status"] == "review_missing"
    assert {Path(path).name for path in payload["artifact_paths"]} == set(filenames)
    assert {Path(path).name for path in payload["upstream_context_paths"]} == (
        expected_upstream
    )
    assert excluded not in result.output

    reviewed = resolve_review_input(
        tmp_path,
        loop_type=loop_type,
        loop_id=loop_id,
        review_round_number=1,
    )
    run_payload = json.loads((loop_dir / "loop-run.json").read_bytes())
    run_payload["status"] = "needs_fix"
    (loop_dir / "loop-run.json").write_text(
        json.dumps(run_payload),
        encoding="utf-8",
    )
    run_drift = resolve_review_input(
        tmp_path,
        loop_type=loop_type,
        loop_id=loop_id,
        review_round_number=1,
    )
    assert run_drift.input_digest == reviewed.input_digest

    pointer.write_text(
        json.dumps(
            {
                "loop_id": "another-loop",
                "loop_run_path": (
                    f".ai-sdlc/loops/{loop_type}/{loop_id}/loop-run.json"
                ),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"does not identify Loop {loop_id}"):
        resolve_review_input(
            tmp_path,
            loop_type=loop_type,
            loop_id=loop_id,
        )


@pytest.mark.parametrize(
    ("loop_type", "filenames"),
    [
        (
            "requirement",
            [
                "requirement-intake.json",
                "requirement-brief.md",
                "clarification-questions.md",
                "acceptance-checklist.md",
            ],
        ),
        (
            "design-contract",
            [
                "design-contract-input.json",
                "design-contract-report.json",
                "design-contract-report.md",
            ],
        ),
        (
            "implementation",
            [
                "implementation-input.json",
                "implementation-report.json",
                "implementation-report.md",
                "verification-evidence.json",
                "implementation-tasks.json",
                "implementation-progress.json",
            ],
        ),
        (
            "frontend-evidence",
            [
                "frontend-evidence-input.json",
                "frontend-evidence-snapshot.json",
                "frontend-evidence-report.json",
                "frontend-evidence-report.md",
            ],
        ),
    ],
)
def test_common_stage_close_gate_rejects_digest_without_outcome(
    tmp_path: Path,
    loop_type: str,
    filenames: list[str],
) -> None:
    loop_id = f"{loop_type}-missing-outcome"
    loop_dir = _write_stage_current_state(tmp_path, loop_type, loop_id)
    for filename in filenames:
        content = "{}" if filename.endswith(".json") else f"{filename}\n"
        (loop_dir / filename).write_text(content, encoding="utf-8")
    _write_predecessor_fixture(tmp_path, loop_type, loop_dir)
    reviewed = resolve_review_input(
        tmp_path,
        loop_type=loop_type,
        loop_id=loop_id,
        review_round_number=1,
    )

    with pytest.raises(ReviewInputGuardError) as error:
        validate_review_input_for_close(
            tmp_path,
            loop_type=loop_type,
            loop_id=loop_id,
            expected_digest=reviewed.input_digest,
        )

    assert error.value.reason == "review-result-missing"


def test_loop_review_reads_expert_bytes_from_digest_bound_snapshot(
    tmp_path: Path,
) -> None:
    loop_id = "requirement-snapshot"
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / loop_id
    loop_dir.mkdir(parents=True)
    (loop_dir / "loop-run.json").write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": "requirement",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_stage_current_pointer(tmp_path, "requirement", loop_id)
    for filename in (
        "requirement-intake.json",
        "requirement-brief.md",
        "clarification-questions.md",
        "acceptance-checklist.md",
    ):
        content = "{}" if filename.endswith(".json") else f"{filename}: v1\n"
        (loop_dir / filename).write_bytes(content.encode("utf-8"))

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        reviewed = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "requirement",
                "--loop-id",
                loop_id,
                "--json",
            ],
        )
    reviewed_payload = json.loads(reviewed.output)
    brief_path = next(
        path
        for path in reviewed_payload["artifact_paths"]
        if path.endswith("requirement-brief.md")
    )

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        snapshot = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "requirement",
                "--loop-id",
                loop_id,
                "--expect-digest",
                reviewed_payload["input_digest"],
                "--read-path",
                brief_path,
                "--json",
            ],
        )

    snapshot_payload = json.loads(snapshot.output)
    assert snapshot.exit_code == 0
    assert snapshot_payload["review_snapshot"] == {
        "path": brief_path,
        "encoding": "utf-8",
        "content": "requirement-brief.md: v1\n",
    }

    (tmp_path / brief_path).write_text(
        "requirement-brief.md: unreviewed\n",
        encoding="utf-8",
    )
    assert snapshot_payload["review_snapshot"]["content"].endswith(": v1\n")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        drifted = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "requirement",
                "--loop-id",
                loop_id,
                "--expect-digest",
                reviewed_payload["input_digest"],
                "--read-path",
                brief_path,
                "--json",
            ],
        )
    assert drifted.exit_code == 1
    assert json.loads(drifted.output)["reason"] == "review-input-drift"


@pytest.mark.parametrize(
    "filename",
    ["implementation-tasks.json", "implementation-progress.json"],
)
def test_implementation_review_binds_generated_task_state(
    tmp_path: Path,
    filename: str,
) -> None:
    loop_id = "implementation-task-state"
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "implementation" / loop_id
    loop_dir.mkdir(parents=True)
    (loop_dir / "loop-run.json").write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": "implementation",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_predecessor_fixture(tmp_path, "implementation", loop_dir)
    for artifact in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        content = "{}" if artifact.endswith(".json") else artifact
        (loop_dir / artifact).write_text(content, encoding="utf-8")

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id=loop_id,
    )
    (loop_dir / filename).write_text(
        json.dumps({"changed_after_review": True}),
        encoding="utf-8",
    )
    changed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id=loop_id,
    )

    assert changed.input_digest != reviewed.input_digest


def test_local_pr_review_binds_pre_close_artifacts_and_reviewed_source_identity(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("changed\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    reviewed_head = _git(tmp_path, "rev-parse", "HEAD")
    reviewed_tree = _git(tmp_path, "write-tree")
    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-001"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps({"review_id": "review-001", "loop_id": "loop-pr-001"}),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-001",
        loop_id="loop-pr-001",
    )
    included = [
        "review-pack.json",
        "diff.patch",
        "findings.json",
        "resolution.yaml",
        "verification-evidence.json",
    ]
    diff = review_dir / "diff.patch"
    diff.write_text("diff --git a/tracked.txt b/tracked.txt\n", encoding="utf-8")
    diff_hash = hashlib.sha256(diff.read_bytes()).hexdigest()
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{diff_hash}",
                "head_commit": reviewed_head,
                "staged_tree_oid": reviewed_tree,
                "diff_source": {
                    "source_kind": "local-staged",
                    "patch_hash": diff_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    for filename in included:
        if filename not in {
            "review-pack.json",
            "diff.patch",
            "verification-evidence.json",
        }:
            (review_dir / filename).write_text(f"{filename}\n", encoding="utf-8")
    _write_successful_local_review_evidence(
        review_dir,
        review_id="review-001",
        loop_id="loop-pr-001",
        staged_tree_oid=reviewed_tree,
    )
    (review_dir / "final-report.md").write_text("must be excluded\n", encoding="utf-8")

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "local-pr-review",
                "--loop-id",
                "loop-pr-001",
                "--json",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {Path(path).name for path in payload["artifact_paths"]} == set(included)
    assert "final-report.md" not in result.output
    assert "git-selected-source:local-staged" in payload["risk_signals"]
    assert f"git-selected-head:{reviewed_head}" in payload["risk_signals"]
    assert f"git-selected-tree:{reviewed_tree}" in payload["risk_signals"]
    assert f"git-selected-diff:{diff_hash}" in payload["risk_signals"]
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-001",
    )

    review_run = review_dir / "review-run.json"
    review_run.write_text(
        json.dumps(
            {
                "review_id": "review-001",
                "loop_id": "loop-pr-001",
                "status": "needs_review",
            }
        ),
        encoding="utf-8",
    )
    run_drift = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-001",
    )
    assert run_drift.input_digest == reviewed.input_digest
    for field_name, redirected_name in (
        ("review_pack_path", "unreviewed-pack.json"),
        ("findings_path", "unreviewed-findings.json"),
    ):
        review_run.write_text(
            json.dumps(
                {
                    "review_id": "review-001",
                    "loop_id": "loop-pr-001",
                    field_name: redirected_name,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=f"{field_name}.*canonical"):
            resolve_review_input(
                tmp_path,
                loop_type="local-pr-review",
                loop_id="loop-pr-001",
            )
    review_run.write_text(
        json.dumps({"review_id": "review-001", "loop_id": "loop-pr-001"}),
        encoding="utf-8",
    )

    pointer = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "current-review.json"
    pointer.write_text(
        json.dumps(
            {
                "review_id": "review-001",
                "loop_id": "another-loop",
                "review_run_path": (".ai-sdlc/reviews/pr/review-001/review-run.json"),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not identify Loop loop-pr-001"):
        resolve_review_input(
            tmp_path,
            loop_type="local-pr-review",
            loop_id="loop-pr-001",
        )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-001",
        loop_id="loop-pr-001",
    )

    diff.write_text("tampered diff\n", encoding="utf-8")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        tampered = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "local-pr-review",
                "--loop-id",
                "loop-pr-001",
                "--json",
            ],
        )
    assert tampered.exit_code == 1
    assert json.loads(tampered.output)["reason"] == "review-input-unavailable"
    diff.write_text("diff --git a/tracked.txt b/tracked.txt\n", encoding="utf-8")

    digest = payload["input_digest"]
    tracked.write_text("changed again\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        drift = runner.invoke(
            app,
            [
                "loop",
                "review",
                "--type",
                "local-pr-review",
                "--loop-id",
                "loop-pr-001",
                "--expect-digest",
                digest,
                "--json",
            ],
        )

    assert drift.exit_code == 0
    assert json.loads(drift.output)["input_digest"] == digest


def test_common_local_pr_close_gate_rejects_digest_without_outcome(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    review_id = "review-missing-outcome"
    loop_id = "loop-pr-missing-outcome"
    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / review_id
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps({"review_id": review_id, "loop_id": loop_id}),
        encoding="utf-8",
    )
    _write_current_review_pointer(tmp_path, review_id=review_id, loop_id=loop_id)
    diff = review_dir / "diff.patch"
    diff.write_text("diff --git a/tracked.txt b/tracked.txt\n", encoding="utf-8")
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}",
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}\n", encoding="utf-8")
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id=loop_id,
        review_round_number=1,
    )

    with pytest.raises(ReviewInputGuardError) as error:
        validate_review_input_for_close(
            tmp_path,
            loop_type="local-pr-review",
            loop_id=loop_id,
            expected_digest=reviewed.input_digest,
        )

    assert error.value.reason == "review-result-missing"
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "local-pr-review" / "loop-pr-001"
    ).exists()


@pytest.mark.parametrize(
    ("command", "required_options"),
    [
        (
            ["loop", "requirement", "freeze"],
            ("--loop-id", "--expect-review-digest"),
        ),
        (
            ["loop", "design-contract", "close"],
            ("--loop-id", "--expect-review-digest"),
        ),
        (
            ["loop", "implementation", "close"],
            ("--loop-id", "--expect-review-digest"),
        ),
        (
            ["loop", "frontend-evidence", "close"],
            ("--loop-id", "--expect-review-digest"),
        ),
        (
            ["pr-review", "close"],
            ("--review-id", "--loop-id", "--expect-review-digest"),
        ),
    ],
)
def test_close_commands_require_reviewed_identity_and_digest(
    command: list[str],
    required_options: tuple[str, ...],
) -> None:
    command_model: Command = get_command(app)
    for command_name in command:
        assert isinstance(command_model, Group)
        command_model = command_model.commands[command_name]

    declared_options = {
        option
        for parameter in command_model.params
        if isinstance(parameter, Option)
        for option in parameter.opts
    }
    for option in required_options:
        assert option in declared_options


def test_close_rebuilds_review_input_and_blocks_digest_drift(tmp_path: Path) -> None:
    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        started = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "start",
                "--loop-id",
                "req-close-digest-guard",
                "--idea",
                "Ops users need an approval report.",
                "--acceptance",
                "The approval report can be exported.",
                "--json",
            ],
        )
        closed = runner.invoke(
            app,
            [
                "loop",
                "requirement",
                "freeze",
                "--loop-id",
                "req-close-digest-guard",
                "--expect-review-digest",
                "0" * 64,
                "--yes",
                "--json",
            ],
        )

    assert started.exit_code == 0, started.output
    assert closed.exit_code == 1
    assert json.loads(closed.output)["reason"] == "review-input-drift"
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-close-digest-guard"
        / "requirement-freeze.json"
    ).exists()


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_local_pr_review_uses_frozen_source_when_index_flags_change(
    tmp_path: Path,
    index_flag: str,
) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed staged content\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-flags"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-flags",
                "loop_id": "loop-pr-flags",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-flags",
        loop_id="loop-pr-flags",
    )
    diff = review_dir / "diff.patch"
    diff.write_text("reviewed staged diff\n", encoding="utf-8")
    reviewed_head = _git(tmp_path, "rev-parse", "HEAD")
    reviewed_tree = _git(tmp_path, "write-tree")
    diff_hash = hashlib.sha256(diff.read_bytes()).hexdigest()
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{diff_hash}",
                "head_commit": reviewed_head,
                "staged_tree_oid": reviewed_tree,
                "diff_source": {
                    "source_kind": "local-staged",
                    "patch_hash": diff_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")
    _write_successful_local_review_evidence(
        review_dir,
        review_id="review-flags",
        loop_id="loop-pr-flags",
        staged_tree_oid=reviewed_tree,
    )

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-flags",
    )
    _git(tmp_path, "update-index", index_flag, "tracked.txt")
    changed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-flags",
    )

    assert changed.input_digest == reviewed.input_digest


@pytest.mark.parametrize("malformed_bytes", [b"{", b"\xff"])
def test_local_pr_review_ignores_malformed_unrelated_history(
    tmp_path: Path,
    malformed_bytes: bytes,
) -> None:
    _init_git_repo(tmp_path)
    reviews_root = tmp_path / ".ai-sdlc" / "reviews" / "pr"
    malformed = reviews_root / "000-malformed"
    malformed.mkdir(parents=True)
    (malformed / "review-run.json").write_bytes(malformed_bytes)

    review_dir = reviews_root / "review-current"
    review_dir.mkdir()
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-current",
                "loop_id": "loop-pr-current",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-current",
        loop_id="loop-pr-current",
    )
    diff = review_dir / "diff.patch"
    diff.write_text("reviewed diff\n", encoding="utf-8")
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}",
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    review_input = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-current",
    )

    assert review_input.loop_id == "loop-pr-current"


def test_local_pr_review_binds_live_unstaged_and_untracked_source(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed unstaged content\n", encoding="utf-8")
    untracked = tmp_path / "untracked.txt"
    untracked.write_text("reviewed untracked content\n", encoding="utf-8")
    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-unstaged"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-unstaged",
                "loop_id": "loop-pr-unstaged",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-unstaged",
        loop_id="loop-pr-unstaged",
    )
    diff = review_dir / "diff.patch"
    diff.write_text("reviewed local-unstaged diff\n", encoding="utf-8")
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}",
                "diff_source": {"source_kind": "local-unstaged"},
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-unstaged",
    )

    tracked.write_text("changed after review\n", encoding="utf-8")
    tracked_drift = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-unstaged",
    )
    assert tracked_drift.input_digest != reviewed.input_digest

    tracked.write_text("reviewed unstaged content\n", encoding="utf-8")
    untracked.write_text("changed untracked content\n", encoding="utf-8")
    untracked_drift = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-unstaged",
    )
    assert untracked_drift.input_digest != reviewed.input_digest


def test_local_pr_review_binds_movable_git_range_refs(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    base_branch = _git(tmp_path, "branch", "--show-current")
    _git(tmp_path, "checkout", "-b", "feature")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("feature step one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "feature step one")
    advanced_base = _git(tmp_path, "rev-parse", "HEAD")
    tracked.write_text("feature step two\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "feature step two")

    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-range"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-range",
                "loop_id": "loop-pr-range",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-range",
        loop_id="loop-pr-range",
    )
    diff = review_dir / "diff.patch"
    diff.write_text("reviewed local-git-range diff\n", encoding="utf-8")
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}",
                "diff_source": {
                    "source_kind": "local-git-range",
                    "base_ref": base_branch,
                    "head_ref": "HEAD",
                },
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-range",
    )

    _git(tmp_path, "update-ref", f"refs/heads/{base_branch}", advanced_base)
    changed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-range",
    )
    assert changed.input_digest != reviewed.input_digest


@pytest.mark.parametrize("mutation", ["patch-file", "head-ref"])
def test_local_pr_review_binds_live_patch_source(
    tmp_path: Path,
    mutation: str,
) -> None:
    _init_git_repo(tmp_path)
    base_branch = _git(tmp_path, "branch", "--show-current")
    _git(tmp_path, "checkout", "-b", "future")
    (tmp_path / "future.txt").write_text("future head\n", encoding="utf-8")
    _git(tmp_path, "add", "future.txt")
    _git(tmp_path, "commit", "-m", "future head")
    future_head = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", base_branch)
    _git(tmp_path, "branch", "moving-head")

    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed patch content\n", encoding="utf-8")
    patch_file = tmp_path / "source.patch"
    patch_file.write_text(
        _git(tmp_path, "diff", "--binary", "--", "tracked.txt") + "\n",
        encoding="utf-8",
    )
    _git(tmp_path, "checkout", "--", "tracked.txt")

    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-patch"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-patch",
                "loop_id": "loop-pr-patch",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-patch",
        loop_id="loop-pr-patch",
    )
    copied_diff = review_dir / "diff.patch"
    copied_diff.write_bytes(patch_file.read_bytes())
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": copied_diff.relative_to(tmp_path).as_posix(),
                "diff_digest": (
                    f"sha256:{hashlib.sha256(copied_diff.read_bytes()).hexdigest()}"
                ),
                "diff_source": {
                    "source_kind": "patch",
                    "patch_file": patch_file.relative_to(tmp_path).as_posix(),
                    "head_ref": "moving-head",
                },
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-patch",
    )

    if mutation == "patch-file":
        patch_file.write_text(
            patch_file.read_text(encoding="utf-8").replace(
                "+reviewed patch content", "+changed patch content"
            ),
            encoding="utf-8",
        )
    else:
        _git(tmp_path, "update-ref", "refs/heads/moving-head", future_head)

    changed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-patch",
    )
    assert changed.input_digest != reviewed.input_digest


def test_local_pr_review_accepts_external_absolute_patch_source(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("reviewed external patch\n", encoding="utf-8")
    external_patch = tmp_path.parent / f"{tmp_path.name}-external.patch"
    external_patch.write_text(
        _git(tmp_path, "diff", "--binary", "--", "tracked.txt") + "\n",
        encoding="utf-8",
    )
    _git(tmp_path, "checkout", "--", "tracked.txt")

    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-external"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-external",
                "loop_id": "loop-pr-external",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-external",
        loop_id="loop-pr-external",
    )
    copied_diff = review_dir / "diff.patch"
    copied_diff.write_bytes(external_patch.read_bytes())
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": copied_diff.relative_to(tmp_path).as_posix(),
                "diff_digest": (
                    f"sha256:{hashlib.sha256(copied_diff.read_bytes()).hexdigest()}"
                ),
                "diff_source": {
                    "source_kind": "patch",
                    "patch_file": str(external_patch.resolve()),
                    "head_ref": "HEAD",
                },
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="local-pr-review",
        loop_id="loop-pr-external",
    )

    assert any(
        signal.startswith("git-selected-patch:") for signal in reviewed.risk_signals
    )


def test_local_pr_review_streams_copied_diff_digest(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    review_dir = tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-large-diff"
    review_dir.mkdir(parents=True)
    (review_dir / "review-run.json").write_text(
        json.dumps(
            {
                "review_id": "review-large-diff",
                "loop_id": "loop-pr-large-diff",
                "current_round": 1,
            }
        ),
        encoding="utf-8",
    )
    _write_current_review_pointer(
        tmp_path,
        review_id="review-large-diff",
        loop_id="loop-pr-large-diff",
    )
    diff = review_dir / "diff.patch"
    chunk = b"binary review payload\n" * 50000
    artifact_size = 12 * len(chunk)
    with diff.open("wb") as handle:
        for _ in range(12):
            handle.write(chunk)
    (review_dir / "review-pack.json").write_text(
        json.dumps(
            {
                "diff_path": diff.relative_to(tmp_path).as_posix(),
                "diff_digest": f"sha256:{hashlib.sha256(diff.read_bytes()).hexdigest()}",
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "findings.json").write_text("{}", encoding="utf-8")

    captured_artifacts: dict[str, bytes] = {}
    tracemalloc.start()
    try:
        resolve_review_input(
            tmp_path,
            loop_type="local-pr-review",
            loop_id="loop-pr-large-diff",
            captured_artifacts=captured_artifacts,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    diff_key = diff.relative_to(tmp_path).as_posix()
    assert diff_key not in captured_artifacts
    assert (review_dir / "review-pack.json").relative_to(tmp_path).as_posix() in (
        captured_artifacts
    )
    assert peak < artifact_size // 2


def test_stage_review_binds_recursive_predecessor_evidence(tmp_path: Path) -> None:
    requirement_dir = (
        tmp_path / ".ai-sdlc" / "loops" / "requirement" / "requirement-001"
    )
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    implementation_dir = (
        tmp_path / ".ai-sdlc" / "loops" / "implementation" / "implementation-001"
    )
    frontend_dir = (
        tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "frontend-001"
    )
    for loop_type, loop_dir in (
        ("requirement", requirement_dir),
        ("design-contract", design_dir),
        ("implementation", implementation_dir),
        ("frontend-evidence", frontend_dir),
    ):
        loop_dir.mkdir(parents=True)
        (loop_dir / "loop-run.json").write_text(
            json.dumps(
                {
                    "loop_id": loop_dir.name,
                    "loop_type": loop_type,
                    "current_round": 1,
                }
            ),
            encoding="utf-8",
        )
        _write_stage_current_pointer(tmp_path, loop_type, loop_dir.name)

    for filename in (
        "requirement-intake.json",
        "requirement-brief.md",
        "clarification-questions.md",
        "acceptance-checklist.md",
    ):
        content = "{}" if filename.endswith(".json") else filename
        (requirement_dir / filename).write_text(content, encoding="utf-8")
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": "requirement-001"}),
        encoding="utf-8",
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text(filename, encoding="utf-8")
    _write_legacy_implementation_input(
        implementation_dir, {"design_contract_loop_id": "design-001"}
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        content = "{}" if filename.endswith(".json") else filename
        (implementation_dir / filename).write_text(content, encoding="utf-8")
    (frontend_dir / "frontend-evidence-input.json").write_text(
        json.dumps({"implementation_loop_id": "implementation-001"}),
        encoding="utf-8",
    )
    for filename in (
        "frontend-evidence-snapshot.json",
        "frontend-evidence-report.json",
        "frontend-evidence-report.md",
    ):
        content = "{}" if filename.endswith(".json") else filename
        (frontend_dir / filename).write_text(content, encoding="utf-8")

    first = resolve_review_input(
        tmp_path,
        loop_type="frontend-evidence",
        loop_id="frontend-001",
    )

    assert {Path(path).name for path in first.upstream_context_paths} == {
        "requirement-brief.md",
        "requirement-intake.json",
        "clarification-questions.md",
        "acceptance-checklist.md",
        "design-contract-input.json",
        "design-contract-report.json",
        "design-contract-report.md",
        "implementation-input.json",
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    }

    (requirement_dir / "requirement-brief.md").write_text(
        "changed requirement",
        encoding="utf-8",
    )
    changed = resolve_review_input(
        tmp_path,
        loop_type="frontend-evidence",
        loop_id="frontend-001",
    )
    assert changed.input_digest != first.input_digest


def test_stage_review_binds_each_stage_source_material(tmp_path: Path) -> None:
    work_item = tmp_path / "specs" / "demo"
    work_item.mkdir(parents=True)
    for filename in ("spec.md", "plan.md", "tasks.md"):
        (work_item / filename).write_text(filename, encoding="utf-8")
    source_dir = tmp_path / "src" / "runtime"
    source_dir.mkdir(parents=True)
    source = source_dir / "feature.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    gate_source = (
        tmp_path / ".ai-sdlc" / "memory" / "frontend-browser-gate" / "latest.yaml"
    )
    gate_source.parent.mkdir(parents=True)
    gate_source.write_text("gate_run_id: gate-1\n", encoding="utf-8")
    screenshot = (
        tmp_path
        / ".ai-sdlc"
        / "artifacts"
        / "frontend-browser-gate"
        / "gate-1"
        / "screenshot.png"
    )
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n\x00screenshot-v1")

    loop_specs = {
        "requirement": ("requirement-001",),
        "design-contract": ("design-001",),
        "implementation": ("implementation-001",),
        "frontend-evidence": ("frontend-001",),
    }
    loop_dirs: dict[str, Path] = {}
    for loop_type, (loop_id,) in loop_specs.items():
        loop_dir = tmp_path / ".ai-sdlc" / "loops" / loop_type / loop_id
        loop_dir.mkdir(parents=True)
        (loop_dir / "loop-run.json").write_text(
            json.dumps(
                {
                    "loop_id": loop_id,
                    "loop_type": loop_type,
                    "current_round": 1,
                }
            ),
            encoding="utf-8",
        )
        _write_stage_current_pointer(tmp_path, loop_type, loop_id)
        loop_dirs[loop_type] = loop_dir

    requirement_dir = loop_dirs["requirement"]
    (requirement_dir / "requirement-intake.json").write_text(
        json.dumps(
            {
                "clarification_questions": ["Which users?"],
                "design_scope_families": ["implementation"],
            }
        ),
        encoding="utf-8",
    )
    for filename in (
        "requirement-brief.md",
        "clarification-questions.md",
        "acceptance-checklist.md",
    ):
        (requirement_dir / filename).write_text(filename, encoding="utf-8")

    design_dir = loop_dirs["design-contract"]
    (design_dir / "design-contract-input.json").write_text(
        json.dumps(
            {
                "requirement_loop_id": "requirement-001",
                "spec_path": "specs/demo/spec.md",
                "plan_path": "specs/demo/plan.md",
                "tasks_path": "specs/demo/tasks.md",
            }
        ),
        encoding="utf-8",
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    implementation_dir = loop_dirs["implementation"]
    _write_legacy_implementation_input(
        implementation_dir,
        {
            "design_contract_loop_id": "design-001",
            "declared_scope": ["src/runtime/*.py", "specs/demo/*.md"],
        },
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (implementation_dir / filename).write_text("{}", encoding="utf-8")

    frontend_dir = loop_dirs["frontend-evidence"]
    (frontend_dir / "frontend-evidence-input.json").write_text(
        json.dumps(
            {
                "implementation_loop_id": "implementation-001",
                "source_artifact_path": ".ai-sdlc/memory/frontend-browser-gate/latest.yaml",
            }
        ),
        encoding="utf-8",
    )
    (frontend_dir / "frontend-evidence-snapshot.json").write_text(
        json.dumps(
            {
                "artifact_records": [
                    {
                        "capture_status": "captured",
                        "artifact_ref": screenshot.relative_to(tmp_path).as_posix(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    for filename in ("frontend-evidence-report.json", "frontend-evidence-report.md"):
        (frontend_dir / filename).write_text("{}", encoding="utf-8")

    cases = {
        "requirement": {
            "loop_id": "requirement-001",
            "expected": {"requirement-intake.json", "clarification-questions.md"},
            "mutate": requirement_dir / "requirement-intake.json",
        },
        "design-contract": {
            "loop_id": "design-001",
            "expected": {
                "design-contract-input.json",
                "spec.md",
                "plan.md",
                "tasks.md",
            },
            "mutate": work_item / "spec.md",
        },
        "implementation": {
            "loop_id": "implementation-001",
            "expected": {"implementation-input.json", "feature.py"},
            "mutate": source,
        },
        "frontend-evidence": {
            "loop_id": "frontend-001",
            "expected": {
                "frontend-evidence-input.json",
                "latest.yaml",
                "screenshot.png",
            },
            "mutate": screenshot,
        },
    }
    for loop_type, case in cases.items():
        first = resolve_review_input(
            tmp_path,
            loop_type=loop_type,
            loop_id=str(case["loop_id"]),
        )
        assert set(case["expected"]) <= {
            Path(path).name for path in first.artifact_paths
        }
        assert not set(first.artifact_paths) & set(first.upstream_context_paths)
        mutate = Path(case["mutate"])
        original = mutate.read_bytes()
        mutate.write_bytes(original + b" changed")
        changed = resolve_review_input(
            tmp_path,
            loop_type=loop_type,
            loop_id=str(case["loop_id"]),
        )
        assert changed.input_digest != first.input_digest
        mutate.write_bytes(original)


def test_frontend_review_skips_large_binary_artifacts_before_risk_scan(
    tmp_path: Path,
) -> None:
    loop_id = "frontend-large-binary"
    loop_dir = _write_stage_current_state(tmp_path, "frontend-evidence", loop_id)
    _write_predecessor_fixture(tmp_path, "frontend-evidence", loop_dir)
    for filename in ("frontend-evidence-report.json", "frontend-evidence-report.md"):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    screenshot = tmp_path / ".ai-sdlc" / "artifacts" / "capture.png"
    screenshot.parent.mkdir(parents=True)
    chunk = b"x" * (1024 * 1024)
    artifact_size = 12 * len(chunk)
    with screenshot.open("wb") as handle:
        for _ in range(12):
            handle.write(chunk)
    (loop_dir / "frontend-evidence-snapshot.json").write_text(
        json.dumps(
            {
                "artifact_records": [
                    {
                        "capture_status": "captured",
                        "artifact_ref": screenshot.relative_to(tmp_path).as_posix(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    tracemalloc.start()
    try:
        review_input = resolve_review_input(
            tmp_path,
            loop_type="frontend-evidence",
            loop_id=loop_id,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert screenshot.relative_to(tmp_path).as_posix() in review_input.artifact_paths
    assert peak < artifact_size // 2


def test_review_streams_large_text_artifacts_during_risk_scan(tmp_path: Path) -> None:
    loop_id = "requirement-large-text"
    loop_dir = _write_stage_current_state(tmp_path, "requirement", loop_id)
    for filename in (
        "requirement-intake.json",
        "clarification-questions.md",
        "acceptance-checklist.md",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    report = loop_dir / "requirement-brief.md"
    chunk = b"plain requirement evidence\n" * 40960
    artifact_size = 12 * len(chunk)
    with report.open("wb") as handle:
        for _ in range(12):
            handle.write(chunk)

    tracemalloc.start()
    try:
        review_input = resolve_review_input(
            tmp_path,
            loop_type="requirement",
            loop_id=loop_id,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert review_input.risk_signals == ["general-correctness"]
    assert peak < artifact_size // 2


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
def test_stage_review_rejects_symlink_source_material(tmp_path: Path) -> None:
    work_item = tmp_path / "specs" / "demo"
    work_item.mkdir(parents=True)
    target = work_item / "target.md"
    target.write_text("trusted design\n", encoding="utf-8")
    linked_spec = work_item / "spec.md"
    linked_spec.symlink_to(target.name)

    loop_dir = _write_stage_current_state(
        tmp_path,
        "design-contract",
        "design-symlink-001",
    )
    (loop_dir / "design-contract-input.json").write_text(
        json.dumps(
            {
                "requirement_loop_id": "",
                "spec_path": linked_spec.relative_to(tmp_path).as_posix(),
            }
        ),
        encoding="utf-8",
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="review path is not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="design-contract",
            loop_id="design-symlink-001",
        )


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
@pytest.mark.parametrize("link_kind", ["directory", "broken"])
def test_implementation_review_rejects_nested_directory_symlinks(
    tmp_path: Path,
    link_kind: str,
) -> None:
    source_dir = tmp_path / "src" / "runtime"
    nested = source_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    link = nested / "linked-dir"
    if link_kind == "directory":
        target = source_dir / "target"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
    else:
        link.symlink_to(source_dir / "missing", target_is_directory=True)

    loop_id = f"implementation-nested-{link_kind}"
    loop_dir = _write_stage_current_state(tmp_path, "implementation", loop_id)
    _write_predecessor_fixture(tmp_path, "implementation", loop_dir)
    _write_legacy_implementation_input(
        loop_dir,
        {
            "design_contract_loop_id": "design-upstream",
            "declared_scope": ["src/runtime"],
        },
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="review path is not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="implementation",
            loop_id=loop_id,
        )


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
def test_stage_review_keeps_symlink_when_scope_also_matches_target(
    tmp_path: Path,
) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    source_dir = tmp_path / "scripts"
    source_dir.mkdir()
    target = source_dir / "a.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (source_dir / "z.py").symlink_to(target.name)

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-001",
    )
    _write_legacy_implementation_input(
        loop_dir,
        {
            "design_contract_loop_id": "design-001",
            "declared_scope": ["scripts/*.py"],
        },
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="review path is not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="implementation",
            loop_id="implementation-001",
        )


def test_implementation_review_represents_deleted_declared_scope(
    tmp_path: Path,
) -> None:
    design_dir = (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-delete-001"
    )
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")
    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-delete-001",
    )
    _write_legacy_implementation_input(
        loop_dir,
        {
            "design_contract_loop_id": "design-delete-001",
            "declared_scope": ["src/runtime/deleted.py"],
        },
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    deleted = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-delete-001",
    )

    restored_path = tmp_path / "src" / "runtime" / "deleted.py"
    restored_path.parent.mkdir(parents=True)
    restored_path.write_text("RESTORED = True\n", encoding="utf-8")
    restored = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-delete-001",
    )
    assert restored.input_digest != deleted.input_digest


def test_implementation_review_binds_repository_evidence_files(tmp_path: Path) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-evidence-001",
    )
    _write_legacy_implementation_input(
        loop_dir,
        {
            "design_contract_loop_id": "design-001",
            "declared_scope": [],
        },
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")
    evidence_file = tmp_path / "artifacts" / "test-results.log"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("3470 passed\n", encoding="utf-8")
    (loop_dir / "verification-evidence.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "evidence": [
                            evidence_file.relative_to(tmp_path).as_posix(),
                            "pytest completed successfully",
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-evidence-001",
    )
    assert evidence_file.relative_to(tmp_path).as_posix() in reviewed.artifact_paths

    evidence_file.write_text("1 failed\n", encoding="utf-8")
    changed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-evidence-001",
    )
    assert changed.input_digest != reviewed.input_digest


def test_implementation_review_binds_repository_evidence_directories(
    tmp_path: Path,
) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-evidence-directory",
    )
    _write_legacy_implementation_input(
        loop_dir, {"design_contract_loop_id": "design-001", "declared_scope": []}
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    evidence_dir = tmp_path / "artifacts" / "test-results"
    evidence_file = evidence_dir / "nested" / "pytest.log"
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_text("3470 passed\n", encoding="utf-8")
    (loop_dir / "verification-evidence.json").write_text(
        json.dumps(
            {"tasks": [{"evidence": [evidence_dir.relative_to(tmp_path).as_posix()]}]}
        ),
        encoding="utf-8",
    )

    reviewed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-evidence-directory",
    )
    assert evidence_file.relative_to(tmp_path).as_posix() in reviewed.artifact_paths

    evidence_file.write_text("1 failed\n", encoding="utf-8")
    changed = resolve_review_input(
        tmp_path,
        loop_type="implementation",
        loop_id="implementation-evidence-directory",
    )
    assert changed.input_digest != reviewed.input_digest


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
def test_implementation_review_rejects_nested_evidence_directory_symlink(
    tmp_path: Path,
) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-evidence-directory-symlink",
    )
    _write_legacy_implementation_input(
        loop_dir, {"design_contract_loop_id": "design-001", "declared_scope": []}
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    external = tmp_path.parent / "external-evidence.log"
    external.write_text("external result\n", encoding="utf-8")
    evidence_dir = tmp_path / "artifacts" / "test-results"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "escaped.log").symlink_to(external)
    (loop_dir / "verification-evidence.json").write_text(
        json.dumps(
            {"tasks": [{"evidence": [evidence_dir.relative_to(tmp_path).as_posix()]}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="implementation",
            loop_id="implementation-evidence-directory-symlink",
        )


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
def test_implementation_review_rejects_escaped_evidence_symlink(
    tmp_path: Path,
) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-evidence-symlink",
    )
    _write_legacy_implementation_input(
        loop_dir, {"design_contract_loop_id": "design-001", "declared_scope": []}
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    external = tmp_path.parent / "external-evidence.log"
    external.write_text("external result\n", encoding="utf-8")
    evidence = tmp_path / "artifacts" / "test-results.log"
    evidence.parent.mkdir()
    evidence.symlink_to(external)
    (loop_dir / "verification-evidence.json").write_text(
        json.dumps(
            {"tasks": [{"evidence": [evidence.relative_to(tmp_path).as_posix()]}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="implementation",
            loop_id="implementation-evidence-symlink",
        )


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation requires extra privileges"
)
def test_implementation_review_does_not_scan_through_ancestor_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "design-001"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}), encoding="utf-8"
    )
    for filename in ("design-contract-report.json", "design-contract-report.md"):
        (design_dir / filename).write_text("{}", encoding="utf-8")

    loop_dir = _write_stage_current_state(
        tmp_path,
        "implementation",
        "implementation-evidence-ancestor-symlink",
    )
    _write_legacy_implementation_input(
        loop_dir, {"design_contract_loop_id": "design-001", "declared_scope": []}
    )
    for filename in (
        "implementation-report.json",
        "implementation-report.md",
        "implementation-tasks.json",
        "implementation-progress.json",
    ):
        (loop_dir / filename).write_text("{}", encoding="utf-8")

    external_dir = tmp_path.with_name(f"{tmp_path.name}-external-evidence")
    external_dir.mkdir()
    external = external_dir / "report.txt"
    external.write_text("security secret from outside\n", encoding="utf-8")
    linked_dir = tmp_path / "artifacts" / "linked-results"
    linked_dir.parent.mkdir()
    linked_dir.symlink_to(external_dir, target_is_directory=True)
    evidence = linked_dir / external.name
    (loop_dir / "verification-evidence.json").write_text(
        json.dumps(
            {"tasks": [{"evidence": [evidence.relative_to(tmp_path).as_posix()]}]}
        ),
        encoding="utf-8",
    )
    original_open = Path.open

    def reject_external_read(path: Path, *args, **kwargs):
        if path == evidence:
            raise AssertionError("risk scan followed an ancestor symlink")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_external_read)

    with pytest.raises(ValueError, match="symlink|not a regular file"):
        resolve_review_input(
            tmp_path,
            loop_type="implementation",
            loop_id="implementation-evidence-ancestor-symlink",
        )


def test_risk_signals_ignore_substrings_in_structural_words(tmp_path: Path) -> None:
    loop_dir = _write_stage_current_state(
        tmp_path,
        "requirement",
        "requirement-001",
    )
    (loop_dir / "requirement-brief.md").write_text(
        '{"blocker_count": 0, "required": true, "build": "complete"}',
        encoding="utf-8",
    )
    (loop_dir / "acceptance-checklist.md").write_text("complete", encoding="utf-8")
    (loop_dir / "requirement-intake.json").write_text("{}", encoding="utf-8")
    (loop_dir / "clarification-questions.md").write_text("none", encoding="utf-8")

    review_input = resolve_review_input(
        tmp_path,
        loop_type="requirement",
        loop_id="requirement-001",
    )

    assert review_input.risk_signals == ["general-correctness"]


def test_risk_signals_detect_standalone_short_terms(tmp_path: Path) -> None:
    loop_dir = _write_stage_current_state(
        tmp_path,
        "requirement",
        "requirement-001",
    )
    (loop_dir / "requirement-brief.md").write_text(
        "UI state requires a lock",
        encoding="utf-8",
    )
    (loop_dir / "acceptance-checklist.md").write_text("complete", encoding="utf-8")
    (loop_dir / "requirement-intake.json").write_text("{}", encoding="utf-8")
    (loop_dir / "clarification-questions.md").write_text("none", encoding="utf-8")

    review_input = resolve_review_input(
        tmp_path,
        loop_type="requirement",
        loop_id="requirement-001",
    )

    assert review_input.risk_signals == ["concurrency", "frontend"]


def test_loop_review_record_persists_exact_selected_experts(tmp_path: Path) -> None:
    loop_id = "requirement-record"
    loop_dir = _write_requirement_review_fixture(tmp_path, loop_id)

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        prepared = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    assert prepared.exit_code == 0, prepared.output
    payload = json.loads(prepared.output)
    assert payload["review_status"] == "review_missing"
    assert payload["round_number"] == 1
    assert len(payload["expert_roles"]) == 2

    result_paths = _write_cli_expert_results(tmp_path, payload)
    arguments = [
        "loop",
        "review-record",
        "--type",
        "requirement",
        "--loop-id",
        loop_id,
        "--expect-digest",
        payload["input_digest"],
    ]
    for result_path in result_paths:
        arguments.extend(["--result", str(result_path)])
    arguments.append("--json")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        recorded = runner.invoke(app, arguments)

    assert recorded.exit_code == 0, recorded.output
    recorded_payload = json.loads(recorded.output)
    assert recorded_payload["status"] == "passed"
    assert recorded_payload["round_number"] == 1
    assert (loop_dir / "review-outcome-round-1.json").is_file()

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        repeated = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["review_status"] == "passed"
    assert repeated_payload["input_digest"] == payload["input_digest"]


def test_loop_review_record_rejects_missing_selected_expert(tmp_path: Path) -> None:
    loop_id = "requirement-missing-expert"
    _write_requirement_review_fixture(tmp_path, loop_id)
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        prepared = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    payload = json.loads(prepared.output)
    result_paths = _write_cli_expert_results(tmp_path, payload)

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        recorded = runner.invoke(
            app,
            [
                "loop",
                "review-record",
                "--type",
                "requirement",
                "--loop-id",
                loop_id,
                "--expect-digest",
                payload["input_digest"],
                "--result",
                str(result_paths[0]),
                "--json",
            ],
        )

    assert recorded.exit_code == 1
    assert json.loads(recorded.output)["reason"] == "expert-role-mismatch"


def test_loop_review_prepares_round_two_only_after_substantive_fix(
    tmp_path: Path,
) -> None:
    loop_id = "requirement-round-two"
    loop_dir = _write_requirement_review_fixture(tmp_path, loop_id)
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        prepared = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    payload = json.loads(prepared.output)
    result_paths = _write_cli_expert_results(tmp_path, payload, severity="important")
    arguments = [
        "loop",
        "review-record",
        "--type",
        "requirement",
        "--loop-id",
        loop_id,
        "--expect-digest",
        payload["input_digest"],
    ]
    for result_path in result_paths:
        arguments.extend(["--result", str(result_path)])
    arguments.append("--json")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        first = runner.invoke(app, arguments)
    assert json.loads(first.output)["status"] == "needs_fix"

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        unchanged = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    assert json.loads(unchanged.output)["review_status"] == "needs_fix"
    with (loop_dir / "requirement-brief.md").open("a", encoding="utf-8") as stream:
        stream.write("Fixed after independent review.\n")

    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        fixed = runner.invoke(
            app,
            ["loop", "review", "--type", "requirement", "--loop-id", loop_id, "--json"],
        )
    fixed_payload = json.loads(fixed.output)
    assert fixed_payload["review_status"] == "review_missing"
    assert fixed_payload["round_number"] == 2
    assert fixed_payload["input_digest"] != payload["input_digest"]


def test_loop_status_projects_current_review_outcome_without_mutating_loop_run(
    tmp_path: Path,
) -> None:
    loop_id = "requirement-status-overlay"
    started = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id=loop_id,
            idea="Security permission requirement.",
            acceptance=("Permission is verified.",),
        )
    )
    assert started.status == "ready"
    loop_run = (
        tmp_path / ".ai-sdlc" / "loops" / "requirement" / loop_id / "loop-run.json"
    )
    original_loop_run = loop_run.read_bytes()

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        missing = runner.invoke(
            app,
            ["loop", "requirement", "status", "--json"],
        )
    assert missing.exit_code == 0, missing.output
    missing_payload = json.loads(missing.output)
    assert missing_payload["current_loop"]["status"] == "needs_review"
    assert missing_payload["blocker"] == "review-result-missing"

    prepared = resolve_review_input(
        tmp_path,
        loop_type="requirement",
        loop_id=loop_id,
        review_round_number=1,
    )
    result_paths = _write_cli_expert_results(
        tmp_path,
        prepared.model_dump(mode="json"),
    )
    arguments = [
        "loop",
        "review-record",
        "--type",
        "requirement",
        "--loop-id",
        loop_id,
        "--expect-digest",
        prepared.input_digest,
    ]
    for result_path in result_paths:
        arguments.extend(["--result", str(result_path)])
    arguments.append("--json")
    with patch("ai_sdlc.cli.loop_review_cmd.find_project_root", return_value=tmp_path):
        recorded = runner.invoke(app, arguments)
    assert recorded.exit_code == 0, recorded.output

    with patch("ai_sdlc.cli.loop_cmd.find_project_root", return_value=tmp_path):
        passed = runner.invoke(
            app,
            ["loop", "requirement", "status", "--json"],
        )
    assert passed.exit_code == 0, passed.output
    assert json.loads(passed.output)["current_loop"]["status"] == "passed"
    assert loop_run.read_bytes() == original_loop_run


@pytest.mark.parametrize("frontend", [False, True])
def test_implementation_close_preserves_reviewed_bytes_and_repeat_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frontend: bool,
) -> None:
    loop_dir, reviewed, close_args = _prepare_cli_implementation_review(
        tmp_path, monkeypatch, frontend=frontend
    )
    reports = {
        name: (loop_dir / name).read_bytes()
        for name in ("implementation-report.json", "implementation-report.md")
    }
    assert json.loads(reports["implementation-report.json"])["status"] == "needs_review"
    outcome_path = loop_dir / "review-outcome-round-1.json"
    outcome_before = outcome_path.read_bytes()
    closed = runner.invoke(app, close_args)
    assert closed.exit_code == 0, closed.output
    close_payload = json.loads(closed.output)
    expected_command = (
        "ai-sdlc loop frontend-evidence start --wi specs/demo-implementation-loop"
        if frontend
        else "ai-sdlc pr-review start"
    )
    assert close_payload["closed"] is True
    assert close_payload["loop_status"] == "closed"
    assert close_payload["next_guidance"]["command"] == expected_command
    assert {name: (loop_dir / name).read_bytes() for name in reports} == reports
    assert outcome_path.read_bytes() == outcome_before

    stable = {
        path: path.read_bytes()
        for path in (loop_dir / "implementation-close.json", loop_dir / "loop-run.json")
    }
    persisted_run = json.loads(stable[loop_dir / "loop-run.json"])
    assert persisted_run["status"] == "closed"
    assert persisted_run["rounds"][-1]["status"] == "closed"
    close_path = loop_dir / "implementation-close.json"
    closed_at = json.loads(stable[close_path])["closed_at"]
    assert closed_at
    assert json.loads(stable[close_path])["next_loop_type"] == (
        "frontend-evidence" if frontend else "local-pr-review"
    )
    after_review = runner.invoke(
        app,
        [
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            loop_dir.name,
            "--expect-digest",
            reviewed["input_digest"],
            "--json",
        ],
    )
    assert after_review.exit_code == 0, after_review.output
    after_payload = json.loads(after_review.output)
    assert after_payload["input_digest"] == reviewed["input_digest"]
    assert after_payload["review_status"] == "passed"
    assert after_payload["round_number"] == reviewed["round_number"]
    status = runner.invoke(app, ["loop", "implementation", "status", "--json"])
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["current_loop"]["implementation"]["closed"] is True
    assert status_payload["current_loop"]["status"] in {"closed", "passed"}
    assert status_payload["blocker"] == ""
    assert status_payload["next_guidance"]["command"] == expected_command

    repeated = runner.invoke(app, close_args)
    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["closed"] is True
    assert repeated_payload["loop_status"] == "closed"
    assert repeated_payload["next_guidance"]["command"] == expected_command
    assert {name: (loop_dir / name).read_bytes() for name in reports} == reports
    assert {path: path.read_bytes() for path in stable} == stable
    assert json.loads(close_path.read_bytes())["closed_at"] == closed_at
    assert outcome_path.read_bytes() == outcome_before


@pytest.mark.parametrize(
    "boundary", ["post-review", "post-close", "final-write", "missing-review"]
)
def test_implementation_close_keeps_native_review_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    loop_dir, _, close_args = _prepare_cli_implementation_review(
        tmp_path, monkeypatch, record_review=boundary != "missing-review"
    )
    if boundary == "post-close":
        first = runner.invoke(app, close_args)
        assert first.exit_code == 0, first.output

    def mutate_report() -> None:
        report = loop_dir / "implementation-report.md"
        report.write_bytes(report.read_bytes() + "\n评审后发生真实变化。\n".encode())

    if boundary == "final-write":
        original_blockers = implementation_loop._close_blockers

        def mutate_after_validation(*args: object, **kwargs: object) -> list[str]:
            blockers = original_blockers(*args, **kwargs)
            mutate_report()
            return blockers

        monkeypatch.setattr(
            implementation_loop, "_close_blockers", mutate_after_validation
        )
    elif boundary != "missing-review":
        mutate_report()
    protected = {
        path: path.read_bytes()
        for path in loop_dir.iterdir()
        if path.name
        in {"loop-run.json", "implementation-close.json", "review-outcome-round-1.json"}
    }
    rejected = runner.invoke(app, close_args)
    assert rejected.exit_code == 1, rejected.output
    assert json.loads(rejected.output)["reason"] == (
        "review-result-missing"
        if boundary == "missing-review"
        else "review-input-drift"
    )
    assert {path: path.read_bytes() for path in protected} == protected
    assert (loop_dir / "implementation-close.json").exists() == (
        boundary == "post-close"
    )


def _prepare_cli_implementation_review(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    frontend: bool = False,
    record_review: bool = True,
) -> tuple[Path, dict[str, object], list[str]]:
    work_item = _write_ready_work_item(root, frontend=frontend)
    _close_design_contract_for_work_item(root, work_item)
    for module in ("loop_cmd", "loop_review_cmd"):
        monkeypatch.setattr(f"ai_sdlc.cli.{module}.find_project_root", lambda: root)
    loop_id = "implementation-close-cli"
    started = runner.invoke(
        app,
        [
            "loop",
            "implementation",
            "start",
            "--wi",
            "specs/demo-implementation-loop",
            "--loop-id",
            loop_id,
            "--json",
        ],
    )
    assert started.exit_code == 0, started.output
    recorded = runner.invoke(
        app,
        [
            "loop",
            "implementation",
            "record",
            "--loop-id",
            loop_id,
            "--task-id",
            "T11",
            "--status",
            "done",
            "--verification",
            "uv run pytest tests/integration/test_cli_loop_review.py -q",
            "--json",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    _record_successful_quality_result(root, loop_id, "T11")
    prepared = runner.invoke(
        app,
        ["loop", "review", "--type", "implementation", "--loop-id", loop_id, "--json"],
    )
    assert prepared.exit_code == 0, prepared.output
    payload = json.loads(prepared.output)
    assert payload["review_status"] == "review_missing"
    loop_dir = root / ".ai-sdlc" / "loops" / "implementation" / loop_id
    if record_review:
        arguments = [
            "loop",
            "review-record",
            "--type",
            "implementation",
            "--loop-id",
            loop_id,
            "--expect-digest",
            payload["input_digest"],
        ]
        for result_path in _write_cli_expert_results(loop_dir, payload):
            arguments.extend(["--result", str(result_path)])
        recorded_review = runner.invoke(app, [*arguments, "--json"])
        assert recorded_review.exit_code == 0, recorded_review.output
        assert json.loads(recorded_review.output)["status"] == "passed"
    return (
        loop_dir,
        payload,
        [
            "loop",
            "implementation",
            "close",
            "--loop-id",
            loop_id,
            "--expect-review-digest",
            payload["input_digest"],
            "--yes",
            "--json",
        ],
    )


def _write_requirement_review_fixture(root: Path, loop_id: str) -> Path:
    loop_dir = _write_stage_current_state(root, "requirement", loop_id)
    (loop_dir / "requirement-intake.json").write_text("{}", encoding="utf-8")
    (loop_dir / "requirement-brief.md").write_text(
        "Security permission requirement.\n",
        encoding="utf-8",
    )
    (loop_dir / "clarification-questions.md").write_text(
        "No open questions.\n",
        encoding="utf-8",
    )
    (loop_dir / "acceptance-checklist.md").write_text(
        "- Permission is verified.\n",
        encoding="utf-8",
    )
    return loop_dir


def _write_cli_expert_results(
    root: Path,
    payload: dict[str, object],
    *,
    severity: str | None = None,
) -> list[Path]:
    roles = payload["expert_roles"]
    reasons = payload["expert_reasons"]
    assert isinstance(roles, list)
    assert isinstance(reasons, dict)
    result_paths: list[Path] = []
    for index, role in enumerate(roles):
        assert isinstance(role, str)
        reason = reasons[role]
        assert isinstance(reason, str)
        findings = []
        if severity is not None:
            findings.append(
                ReviewFinding(
                    severity=severity,
                    role=role,
                    location="requirement-brief.md:1",
                    summary="The requirement has an actionable gap.",
                    recommendation="Revise the requirement before Close.",
                )
            )
        result_path = root / f"cli-expert-{payload['round_number']}-{index}.json"
        result_path.write_text(
            ReviewExecution(
                status="completed",
                roles=[role],
                role_reasons={role: reason},
                findings=findings,
            ).model_dump_json(),
            encoding="utf-8",
        )
        result_paths.append(result_path)
    return result_paths


def _init_git_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "review@example.com")
    _git(root, "config", "user.name", "Review Test")
    (root / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")


def _write_successful_local_review_evidence(
    review_dir: Path,
    *,
    review_id: str,
    loop_id: str,
    staged_tree_oid: str,
) -> None:
    source_digest = f"sha256:{'a' * 64}"
    empty_digest = f"sha256:{hashlib.sha256(b'').hexdigest()}"
    (review_dir / "verification-evidence.json").write_text(
        json.dumps(
            {
                "artifact_kind": "review-verification-evidence",
                "review_id": review_id,
                "loop_id": loop_id,
                "staged_tree_oid": staged_tree_oid,
                "entries": [],
                "results": [
                    {
                        "argv": ["python", "-c", "print('verified')"],
                        "cwd": ".",
                        "exit_code": 0,
                        "started_at": "2026-08-17T00:00:00Z",
                        "completed_at": "2026-08-17T00:00:01Z",
                        "source_digest_before": source_digest,
                        "source_digest_after": source_digest,
                        "stdout_sha256": empty_digest,
                        "stderr_sha256": empty_digest,
                        "status": "passed",
                        "timed_out": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_current_review_pointer(
    root: Path,
    *,
    review_id: str,
    loop_id: str,
) -> None:
    pointer = root / ".ai-sdlc" / "reviews" / "pr" / "current-review.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "review_id": review_id,
                "loop_id": loop_id,
                "review_run_path": (f".ai-sdlc/reviews/pr/{review_id}/review-run.json"),
            }
        ),
        encoding="utf-8",
    )


def _write_stage_current_pointer(
    root: Path,
    loop_type: str,
    loop_id: str,
) -> Path:
    pointer = root / ".ai-sdlc" / "loops" / loop_type / _STAGE_POINTER_NAMES[loop_type]
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_run_path": (
                    f".ai-sdlc/loops/{loop_type}/{loop_id}/loop-run.json"
                ),
            }
        ),
        encoding="utf-8",
    )
    return pointer


def _write_legacy_implementation_input(loop_dir: Path, payload: dict) -> None:
    from ai_sdlc.core.implementation_models import ImplementationInput
    from ai_sdlc.core.implementation_store import implementation_input_digest
    from ai_sdlc.core.loop_models import LoopRound, LoopRun

    # 来源/链接测试仍使用合成上游，但本阶段身份必须是可读取的合法 legacy 原件。
    value = ImplementationInput.model_validate(
        {
            "loop_id": loop_dir.name,
            "work_item_id": "demo",
            "work_item_path": "specs/demo",
            "spec_path": "specs/demo/spec.md",
            "plan_path": "specs/demo/plan.md",
            "tasks_path": "specs/demo/tasks.md",
            **payload,
        }
    )
    run_path = loop_dir / "loop-run.json"
    run_payload = (
        json.loads(run_path.read_bytes())
        if run_path.exists()
        else {
            "loop_id": loop_dir.name,
            "loop_type": "implementation",
            "current_round": 1,
        }
    )
    run_payload.update(
        work_item_id=value.work_item_id,
        input_digest=implementation_input_digest(value),
        rounds=[
            LoopRound(round_number=number).model_dump(mode="json")
            for number in range(1, run_payload["current_round"] + 1)
        ],
    )
    run_path.write_text(
        LoopRun.model_validate(run_payload).model_dump_json(), encoding="utf-8"
    )
    (loop_dir / "implementation-input.json").write_text(
        value.model_dump_json(), encoding="utf-8"
    )


def _write_stage_current_state(
    root: Path,
    loop_type: str,
    loop_id: str,
    *,
    current_round: int = 1,
) -> Path:
    loop_dir = root / ".ai-sdlc" / "loops" / loop_type / loop_id
    loop_dir.mkdir(parents=True, exist_ok=True)
    (loop_dir / "loop-run.json").write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": loop_type,
                "current_round": current_round,
            }
        ),
        encoding="utf-8",
    )
    _write_stage_current_pointer(root, loop_type, loop_id)
    return loop_dir


def _write_predecessor_fixture(
    root: Path,
    loop_type: str,
    loop_dir: Path,
) -> set[str]:
    loop_id = loop_dir.name
    _write_stage_current_pointer(root, loop_type, loop_id)
    if loop_type == "requirement":
        return set()
    if loop_type == "design-contract":
        (loop_dir / "design-contract-input.json").write_text(
            json.dumps({"requirement_loop_id": ""}),
            encoding="utf-8",
        )
        return set()

    design_dir = root / ".ai-sdlc" / "loops" / "design-contract" / "design-upstream"
    design_dir.mkdir(parents=True)
    (design_dir / "design-contract-input.json").write_text(
        json.dumps({"requirement_loop_id": ""}),
        encoding="utf-8",
    )
    design_files = {
        "design-contract-input.json",
        "design-contract-report.json",
        "design-contract-report.md",
    }
    for filename in design_files - {"design-contract-input.json"}:
        (design_dir / filename).write_text(filename, encoding="utf-8")
    if loop_type == "implementation":
        _write_legacy_implementation_input(
            loop_dir, {"design_contract_loop_id": "design-upstream"}
        )
        return design_files

    implementation_dir = (
        root / ".ai-sdlc" / "loops" / "implementation" / "implementation-upstream"
    )
    implementation_dir.mkdir(parents=True)
    _write_legacy_implementation_input(
        implementation_dir, {"design_contract_loop_id": "design-upstream"}
    )
    implementation_files = {
        "implementation-input.json",
        "implementation-report.json",
        "implementation-report.md",
        "verification-evidence.json",
        "implementation-tasks.json",
        "implementation-progress.json",
    }
    for filename in implementation_files - {"implementation-input.json"}:
        content = "{}" if filename.endswith(".json") else filename
        (implementation_dir / filename).write_text(content, encoding="utf-8")
    (loop_dir / "frontend-evidence-input.json").write_text(
        json.dumps({"implementation_loop_id": "implementation-upstream"}),
        encoding="utf-8",
    )
    return design_files | implementation_files


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

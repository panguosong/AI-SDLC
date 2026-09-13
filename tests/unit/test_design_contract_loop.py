"""Tests for the deterministic design-contract loop runtime."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import ai_sdlc.core.design_contract_loop as design_contract_loop_module
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.core.design_contract_loop import (
    CURRENT_DESIGN_CONTRACT_PATH,
    DesignContractCheckOptions,
    DesignContractCloseOptions,
    check_design_contract_loop,
    close_design_contract_loop,
)
from ai_sdlc.core.design_contract_models import (
    DesignContractInput,
    DesignContractReport,
)
from ai_sdlc.core.design_contract_store import (
    DesignContractArtifacts,
    resolve_design_contract_loop_run_path,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.requirement_loop import (
    RequirementFreezeOptions,
    RequirementIntake,
    RequirementStartOptions,
    _requirement_intake_digest,
    freeze_requirement_loop,
    start_requirement_loop,
)


def test_public_design_contract_resolver_keeps_two_value_signature(
    tmp_path: Path,
) -> None:
    path, blocker = resolve_design_contract_loop_run_path(tmp_path, "")

    assert path == tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    assert blocker == "No current design-contract loop exists."


def test_check_design_contract_loop_waits_for_expert_review(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-001",
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "needs_review"
    assert result.work_item_id == "demo-contract"
    assert result.work_item_path == "specs/demo-contract"
    assert result.blocker_count == 0
    assert result.coverage_count == 2
    assert result.design_contract is not None
    assert result.design_contract.status == "needs_review"
    assert result.design_contract.coverage_count == 2
    assert result.design_contract.coverage_matrix_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-001/coverage-matrix.json"
    )
    assert result.design_contract.report_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-001/design-contract-report.json"
    )
    assert (
        result.next_action
        == "Run ai-sdlc loop review --type design-contract --loop-id dc-001."
    )
    assert (
        result.next_guidance.command
        == "ai-sdlc loop review --type design-contract --loop-id dc-001"
    )
    assert result.next_guidance.requires_model is True
    assert result.next_guidance.writes_artifacts is False
    assert result.next_guidance.writes_code is False

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-001"
    assert (loop_dir / "loop-run.json").is_file()
    assert (loop_dir / "design-contract-input.json").is_file()
    assert (loop_dir / "coverage-matrix.json").is_file()
    assert (loop_dir / "design-contract-report.json").is_file()
    assert (loop_dir / "design-contract-report.md").is_file()
    assert (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).is_file()

    report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert report["artifact_kind"] == "design-contract-report"
    assert report["status"] == "needs_review"
    assert report["coverage_count"] == 2
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }
    assert {
        item["source_id"]: item["covered_by"] for item in report["coverage_items"]
    } == {
        "FR-DEMO-001": ["T11"],
        "SC-DEMO-001": ["T11"],
    }
    coverage = json.loads(
        (loop_dir / "coverage-matrix.json").read_text(encoding="utf-8")
    )
    assert coverage["artifact_kind"] == "coverage-matrix"
    assert coverage["created_by"] == "ai-sdlc"
    assert coverage["created_at"]
    assert coverage["ai_sdlc_version"]
    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["artifact_kind"] == "current-design-contract-pointer"
    assert pointer["created_by"] == "ai-sdlc"
    assert pointer["created_at"]
    assert pointer["ai_sdlc_version"]

    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["loop_type"] == "design-contract"
    assert loop_run["status"] == "needs_review"
    assert loop_run["work_item_id"] == work_item.name
    contract_input = json.loads(
        (loop_dir / "design-contract-input.json").read_text(encoding="utf-8")
    )
    assert contract_input["requirement_loop_id"] == "req-current"


def test_check_design_contract_loop_allows_fix_and_recheck_in_same_loop(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-fix-and-recheck",
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    second = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-fix-and-recheck",
        )
    )

    assert second.status == "ready"
    assert second.loop_status == "needs_review"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-fix-and-recheck",
            yes=True,
        )
    )
    assert closed.status == "ready"
    assert closed.closed is True


def test_check_design_contract_loop_recovers_initial_partial_artifact_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-initial-write-recovery"
    original_write = design_contract_loop_module._write_check_artifacts
    original_build = design_contract_loop_module.build_contract_input
    timestamps = iter(("2030-01-01T00:00:00Z", "2030-01-01T00:00:01Z"))

    def build_with_advancing_timestamp(
        *,
        root: Path,
        loop_id: str,
        work_item_dir: Path,
        requirement_loop_id: str,
    ) -> DesignContractInput:
        built = original_build(
            root=root,
            loop_id=loop_id,
            work_item_dir=work_item_dir,
            requirement_loop_id=requirement_loop_id,
        )
        return built.model_copy(update={"created_at": next(timestamps)})

    def write_input_then_fail(
        root: Path,
        contract_input: DesignContractInput,
        report: DesignContractReport,
        loop_run: LoopRun,
        artifacts: DesignContractArtifacts,
        *,
        loop_run_must_be_absent: bool = False,
    ) -> None:
        LoopArtifactStore(root).write_json_artifact(
            artifacts.input_path,
            contract_input,
        )
        raise OSError("injected initial artifact write failure")

    monkeypatch.setattr(
        design_contract_loop_module,
        "build_contract_input",
        build_with_advancing_timestamp,
    )
    monkeypatch.setattr(
        design_contract_loop_module,
        "_write_check_artifacts",
        write_input_then_fail,
    )
    with pytest.raises(OSError, match="injected initial artifact write failure"):
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        )

    monkeypatch.setattr(
        design_contract_loop_module,
        "_write_check_artifacts",
        original_write,
    )
    retried = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert retried.status == "ready", retried.blocker
    assert retried.loop_status == "needs_review"


@pytest.mark.parametrize(
    "artifact_name",
    ["design-contract-input.json", "loop-run.json"],
)
def test_check_design_contract_loop_rejects_symlinked_previous_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    loop_id = "dc-previous-artifact-symlink"
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    artifact = loop_dir / artifact_name
    backing = loop_dir / f"backing-{artifact_name}"
    backing.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(backing.name)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert result.status == "blocked"
    assert "previous design check artifacts are unavailable" in result.blocker


@pytest.mark.parametrize("replacement_kind", ["broken_symlink", "directory"])
def test_check_design_contract_loop_rejects_invalid_previous_loop_run(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
    )
    loop_id = "dc-invalid-previous-loop-run"
    first = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert first.status == "needs_fix"

    tasks_path = work_item / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "Cover contract docs.\n- **验证**：",
            "Cover FR-DEMO-001 and SC-DEMO-001.\n"
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
        ),
        encoding="utf-8",
    )
    loop_run = (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id / "loop-run.json"
    )
    loop_run.unlink()
    if replacement_kind == "broken_symlink":
        loop_run.symlink_to("missing-loop-run.json")
    else:
        loop_run.mkdir()

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert result.status == "blocked"
    assert "previous design check artifacts are unavailable" in result.blocker


def test_close_design_contract_loop_rejects_symlinked_loop_run(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-close-loop-run-symlink"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"

    loop_run = (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id / "loop-run.json"
    )
    backing = loop_run.with_name("backing-loop-run.json")
    backing.write_bytes(loop_run.read_bytes())
    loop_run.unlink()
    loop_run.symlink_to(backing.name)

    closed = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
        )
    )
    assert closed.status == "blocked"
    assert closed.closed is False
    assert "uses a symlink" in closed.blocker


@pytest.mark.parametrize(
    "replacement_kind",
    ["valid_symlink", "broken_symlink", "directory"],
)
def test_closed_design_contract_recheck_rejects_untrusted_close_artifact(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-closed-untrusted-close"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )
    assert closed.closed is True
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    backing = loop_dir / "backing-design-contract-close.json"
    if replacement_kind == "valid_symlink":
        backing.write_bytes(close_path.read_bytes())
    close_path.unlink()
    if replacement_kind == "directory":
        close_path.mkdir()
    else:
        target = backing.name if replacement_kind == "valid_symlink" else "missing.json"
        close_path.symlink_to(target)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_closed_design_contract_recheck_rejects_fifo_close_artifact(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-closed-fifo-close"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    assert close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    ).closed
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    close_path.unlink()
    os.mkfifo(close_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_current_closed_recheck_rejects_untrusted_close_artifact(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    loop_id = "dc-current-closed-untrusted-close"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    assert close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    ).closed
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    close_path.unlink()
    close_path.symlink_to("missing-close.json")

    result = check_design_contract_loop(
        DesignContractCheckOptions(root=tmp_path, work_item=str(work_item))
    )

    assert result.status == "blocked"
    assert "closed design-contract artifact is unavailable" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_check_design_contract_loop_dry_run_does_not_write(tmp_path: Path) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-dry-run",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.loop_status == "created"
    assert result.design_contract is not None
    assert result.design_contract.status == "created"
    assert result.design_contract.work_item_id == "demo-contract"
    assert result.design_contract.coverage_matrix_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/coverage-matrix.json"
    )
    assert result.design_contract.report_path.endswith(
        ".ai-sdlc/loops/design-contract/dc-dry-run/design-contract-report.json"
    )
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-dry-run"
    ).exists()
    assert any(artifact.kind == "loop-run" for artifact in result.artifacts)


def test_check_design_contract_loop_blocks_missing_current_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-missing-current-requirement",
        )
    )

    assert result.status == "blocked"
    assert "frozen current requirement loop is required" in result.blocker
    assert "No current requirement loop exists" in result.blocker
    assert result.next_action == "Run ai-sdlc loop requirement start."
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-missing-current-requirement"
    ).exists()


def test_check_design_contract_loop_uses_current_frozen_requirement_by_default(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, requirement_loop_id="req-default-frozen")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-current-requirement",
        )
    )

    assert result.status == "ready"
    input_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-current-requirement"
            / "design-contract-input.json"
        ).read_text(encoding="utf-8")
    )
    assert input_payload["requirement_loop_id"] == "req-default-frozen"


def test_check_design_contract_loop_blocks_unfrozen_current_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-current-unfrozen",
            idea="Demo users need a design contract.",
            acceptance=("Design contract can be checked.",),
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-unfrozen-current-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-current-unfrozen must be frozen before "
        "design-contract check."
    )
    assert result.next_action == (
        "Run ai-sdlc loop review --type requirement --loop-id req-current-unfrozen."
    )


def test_check_design_contract_loop_blocks_missing_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-missing",
            loop_id="dc-missing-requirement",
        )
    )

    assert result.status == "blocked"
    assert "must exist and be frozen" in result.blocker
    assert result.next_action == "Run ai-sdlc loop requirement start."
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-missing-requirement"
    ).exists()


def test_check_design_contract_loop_blocks_unfrozen_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="需要设计合同前置验证",
            acceptance=("需求可被冻结",),
            loop_id="req-unfrozen",
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-unfrozen",
            loop_id="dc-unfrozen-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-unfrozen must be frozen before design-contract check."
    )
    assert (
        result.next_action
        == "Run ai-sdlc loop review --type requirement --loop-id req-unfrozen."
    )


def test_check_design_contract_loop_accepts_frozen_requirement_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    start_result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="需要设计合同前置验证",
            acceptance=("需求可被冻结",),
            loop_id="req-frozen",
        )
    )
    assert start_result.status == "ready"
    freeze_result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="req-frozen", yes=True)
    )
    assert freeze_result.status == "ready"
    assert freeze_result.frozen is True

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-frozen",
            loop_id="dc-frozen-requirement",
        )
    )

    assert result.status == "ready"
    input_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-frozen-requirement"
            / "design-contract-input.json"
        ).read_text(encoding="utf-8")
    )
    assert input_payload["requirement_loop_id"] == "req-frozen"


def test_check_design_contract_loop_blocks_mismatched_requirement_work_item(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, with_frozen_requirement=False)
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-other-work-item",
            idea="Other work item needs a design contract.",
            acceptance=("Other work item can be checked.",),
            work_item_id="other-contract",
        )
    )
    freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="req-other-work-item", yes=True)
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            requirement_loop_id="req-other-work-item",
            loop_id="dc-mismatched-requirement",
        )
    )

    assert result.status == "blocked"
    assert result.blocker == (
        "Requirement loop req-other-work-item belongs to work item other-contract, "
        "but design-contract work item is demo-contract."
    )
    assert "--work-item-id demo-contract" in result.next_action


def test_check_design_contract_loop_reports_missing_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False, verification_value="")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-needs-fix",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count >= 2
    assert "Fix design-contract blockers" in result.next_action

    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-needs-fix"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "missing_coverage" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_infers_generated_task_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-inferred-coverage",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-inferred-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"covered"}
    assert {
        item["source_id"]: item["covered_by"] for item in report["coverage_items"]
    } == {
        "FR-DEMO-001": ["T11"],
        "SC-DEMO-001": ["T11"],
    }


def test_check_design_contract_loop_ignores_non_task_coverage_refs(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        tasks_intro_extra="\n".join(
            [
                "## Deferred notes",
                "",
                "FR-DEMO-001 and SC-DEMO-001 are mentioned outside executable tasks.",
                "",
                "```markdown",
                "FR-DEMO-001 SC-DEMO-001",
                "```",
                "",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-non-task-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-non-task-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])
    assert {finding["source_id"] for finding in report["findings"]} >= {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_ignores_trailing_non_task_coverage_refs(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        tasks_tail_extra="\n".join(
            [
                "",
                "## Coverage matrix",
                "",
                "- FR-DEMO-001",
                "- SC-DEMO-001",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-trailing-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-trailing-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])


def test_check_design_contract_loop_reports_placeholders(tmp_path: Path) -> None:
    _write_work_item(tmp_path, placeholder=True)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-placeholder",
        )
    )

    assert result.status == "needs_fix"
    assert result.blocker_count >= 1
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-placeholder"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "placeholder" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_accepts_filled_feature_spec_title(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, spec_title="# 功能规格：Frontend Program Demo")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-filled-feature-title",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_direct_formal_as_product_term(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_title="# 功能规格：Direct Formal Work Item",
        spec_intro_extra="本功能延续 direct-formal work item 入口。",
        plan_extra="direct-formal 是本次合同覆盖的正常产品术语。",
        tasks_intro_extra="direct-formal 相关任务必须仍可进入合同检查。",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-direct-formal-term",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_english_plan_sections(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    (work_item / "plan.md").write_text(
        "\n".join(
            [
                "# Implementation Plan",
                "",
                "## Technical Context",
                "Python runtime.",
                "## Phase Plan",
                "Phase 1.",
                "## Verification",
                "Run pytest.",
                "## Rollback",
                "Revert the commit.",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-english-plan",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_reports_unrendered_feature_spec_title(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, spec_title="# 功能规格：{{ project_name }}")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-template-feature-title",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-template-feature-title"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "placeholder" in {finding["code"] for finding in report["findings"]}


@pytest.mark.parametrize(
    "status_line",
    [
        "**状态**: 草稿",
        "**Status**: Draft",
    ],
)
def test_check_design_contract_loop_blocks_draft_status_variants(
    tmp_path: Path,
    status_line: str,
) -> None:
    _write_work_item(tmp_path, spec_status_line=status_line)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-draft-status",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-draft-status"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "draft_spec" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_ignores_example_contract_ids(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_intro_extra="\n".join(
            [
                "## 用户故事与示例",
                "",
                "**独立测试**：构造 `FR-EXAMPLE-001` 和 `SC-EXAMPLE-001`。",
                "",
                "```markdown",
                "- **FR-CODE-001**：代码块中的编号不能成为合同项。",
                "```",
                "",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-ignore-examples",
        )
    )

    assert result.status == "ready"
    assert result.coverage_count == 2
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-ignore-examples"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_treats_exit_criteria_as_contract_section(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, success_heading="## Exit Criteria")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-exit-criteria",
        )
    )

    assert result.status == "ready"
    assert result.coverage_count == 2
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-exit-criteria"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["source_id"] for item in report["coverage_items"]} == {
        "FR-DEMO-001",
        "SC-DEMO-001",
    }


def test_check_design_contract_loop_blocks_unparseable_task_sections(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, task_heading="### 工作 1.1 Check contract")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-task-sections",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-task-sections"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_section_gap" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_accepts_generated_chinese_task_sections(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, task_heading="### 任务 1.1 Check contract")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-chinese-task-section",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_accepts_english_task_labels(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        acceptance_label="- **Acceptance Criteria**",
        verification_label="- **Verification**",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-english-task-labels",
        )
    )

    assert result.status == "ready"
    assert result.blocker_count == 0


def test_check_design_contract_loop_ignores_p2_task_detail_gaps(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        extra_task_sections="\n".join(
            [
                "",
                "### Task 2.1 Deferred polish",
                "",
                "- **任务编号**：T12",
                "- **优先级**：P2",
                "- Backlog note without acceptance or verification details.",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-p2-task-gap",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-p2-task-gap"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_acceptance_gap" not in {
        finding["code"] for finding in report["findings"]
    }
    assert "task_verification_gap" not in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_ignores_p2_task_contract_coverage(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        verification_value="",
        extra_task_sections="\n".join(
            [
                "",
                "### Task 2.1 Deferred coverage note",
                "",
                "- **任务编号**：T12",
                "- **优先级**：P2",
                "- Deferred backlog mentions FR-DEMO-001 and SC-DEMO-001.",
            ]
        ),
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-p2-coverage",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-p2-coverage"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["status"] for item in report["coverage_items"]} == {"missing"}
    assert all(item["covered_by"] == [] for item in report["coverage_items"])
    assert "missing_coverage" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_requires_verification_command(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, verification_value="")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-empty-verification",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-empty-verification"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_verification_gap" in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_accepts_canonical_verify_label(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        verification_label="- verify",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-canonical-verify-label",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-canonical-verify-label"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "task_verification_gap" not in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_checks_plan_scope_drift(tmp_path: Path) -> None:
    _write_work_item(tmp_path, plan_extra="Touch implementation_loop.py.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-plan-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-plan-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert scope_findings[0]["path"] == "specs/demo-contract/plan.md"


def test_check_design_contract_loop_detects_case_variant_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Touch IMPLEMENTATION_LOOP.PY.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-case-variant-drift",
        )
    )

    assert result.status == "needs_fix"


def test_check_design_contract_loop_checks_local_review_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Run ai-sdlc pr-review start.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-local-review-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-local-review-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert "ai-sdlc pr-review" in scope_findings[0]["message"]


def test_check_design_contract_loop_checks_frontend_command_scope_drift(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, plan_extra="Run ai-sdlc loop frontend-evidence check.")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-frontend-command-drift",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-frontend-command-drift"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    scope_findings = [
        finding for finding in report["findings"] if finding["code"] == "scope_drift"
    ]
    assert scope_findings
    assert "ai-sdlc loop frontend-evidence" in scope_findings[0]["message"]


def test_check_design_contract_loop_rejects_scope_inferred_only_from_work_item_path(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        relative_path="specs/implementation-loop-runtime",
        plan_extra="Run ai-sdlc loop implementation check and touch implementation_loop.py.",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/implementation-loop-runtime",
            loop_id="dc-active-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-active-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_allows_scope_authorized_by_frozen_requirement(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        requirement_scope_families=(
            "implementation",
            "frontend-evidence",
            "pr-review",
        ),
        plan_extra="\n".join(
            [
                "Touch implementation_loop.py.",
                "Touch frontend_evidence_loop.py.",
                "Touch pr_review_service.py.",
            ]
        ),
    )
    spec_path = work_item / "spec.md"
    spec_path.write_text(
        "---\n"
        "design_scope_families:\n"
        "  - implementation\n"
        "  - frontend-evidence\n"
        "  - pr-review\n"
        "---\n"
        f"{spec_path.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-declared-scope",
        )
    )

    assert result.status == "ready"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-declared-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" not in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_uses_the_validated_requirement_scope_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    original_read = LoopArtifactStore.read_json_artifact
    intake_reads = 0

    def counted_read(store: LoopArtifactStore, path: Path) -> object:
        nonlocal intake_reads
        if path.name == "requirement-intake.json":
            intake_reads += 1
        return original_read(store, path)

    monkeypatch.setattr(LoopArtifactStore, "read_json_artifact", counted_read)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-single-authority-snapshot",
        )
    )

    assert result.status == "ready"
    assert intake_reads == 1


def test_check_design_contract_loop_rejects_scope_self_authorized_by_spec(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(
        tmp_path,
        plan_extra="Touch implementation_loop.py.",
    )
    spec_path = work_item / "spec.md"
    spec_path.write_text(
        "---\n"
        "design_scope_families:\n"
        "  - implementation\n"
        "---\n"
        f"{spec_path.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-self-authorized-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-self-authorized-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_authority_missing" in {
        finding["code"] for finding in report["findings"]
    }


def test_check_design_contract_loop_blocks_mutated_frozen_requirement(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    intake_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-current"
        / "requirement-intake.json"
    )
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    intake["design_scope_families"].append("pr-review")
    intake_path.write_text(json.dumps(intake), encoding="utf-8")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-mutated-scope-authority",
        )
    )

    assert result.status == "blocked"
    assert "changed after freeze" in result.blocker


def test_close_design_contract_loop_blocks_changed_frozen_requirement_scope(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        requirement_scope_families=("implementation",),
    )
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-refrozen-scope-authority",
        )
    )
    assert check.status == "ready"
    requirement_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-current"
    intake_path = requirement_dir / "requirement-intake.json"
    intake_payload = json.loads(intake_path.read_text(encoding="utf-8"))
    intake_payload["design_scope_families"].append("pr-review")
    intake = RequirementIntake.model_validate(intake_payload)
    intake_path.write_text(
        json.dumps(intake.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    freeze_path = requirement_dir / "requirement-freeze.json"
    freeze_payload = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze_payload["intake_digest"] = _requirement_intake_digest(intake)
    freeze_path.write_text(json.dumps(freeze_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-refrozen-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "Frozen requirement scope changed" in result.blocker


def test_close_design_contract_loop_blocks_changed_checked_input(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-cleared-scope-authority",
        )
    )
    assert check.status == "ready"
    input_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-cleared-scope-authority"
        / "design-contract-input.json"
    )
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    input_payload["authorized_scope_families"] = ["implementation"]
    input_payload["scope_authority_ref"] = ""
    input_payload["scope_authority_digest"] = ""
    input_path.write_text(json.dumps(input_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-cleared-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "input changed after check" in result.blocker


def test_close_design_contract_loop_blocks_swapped_requirement_input(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-swapped-scope-authority",
        )
    )
    assert check.status == "ready"
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-alternate",
            work_item_id="demo-contract",
            idea="Authorize implementation design scope.",
            acceptance=("Design contract can be checked.",),
            design_scope_families=("implementation",),
        )
    )
    frozen = freeze_requirement_loop(
        RequirementFreezeOptions(
            root=tmp_path,
            loop_id="req-alternate",
            yes=True,
        )
    )
    assert frozen.status == "ready"
    alternate_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-alternate"
    alternate_freeze = json.loads(
        (alternate_dir / "requirement-freeze.json").read_text(encoding="utf-8")
    )
    input_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-swapped-scope-authority"
        / "design-contract-input.json"
    )
    input_payload = json.loads(input_path.read_text(encoding="utf-8"))
    input_payload["requirement_loop_id"] = "req-alternate"
    input_payload["authorized_scope_families"] = ["implementation"]
    input_payload["scope_authority_ref"] = (
        ".ai-sdlc/loops/requirement/req-alternate/requirement-intake.json"
    )
    input_payload["scope_authority_digest"] = alternate_freeze["intake_digest"]
    input_path.write_text(json.dumps(input_payload, indent=2), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-swapped-scope-authority",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "input changed after check" in result.blocker


def test_check_design_contract_loop_rejects_scope_mentioned_only_as_a_non_goal(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        spec_intro_extra=(
            "Non-goal: never touch implementation_loop.py, "
            "frontend_evidence_loop.py, or pr_review_service.py."
        ),
        plan_extra="Touch implementation_loop.py.",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-non-goal-does-not-authorize-scope",
        )
    )

    assert result.status == "needs_fix"
    report = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / "dc-non-goal-does-not-authorize-scope"
            / "design-contract-report.json"
        ).read_text(encoding="utf-8")
    )
    assert "scope_drift" in {finding["code"] for finding in report["findings"]}


def test_check_design_contract_loop_blocks_non_canonical_work_item_dir(
    tmp_path: Path,
) -> None:
    _write_work_item(
        tmp_path,
        relative_path="other/demo-contract",
        with_frozen_requirement=False,
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="other/demo-contract",
            loop_id="dc-non-canonical",
        )
    )

    assert result.status == "blocked"
    assert "canonical specs/<work-item>" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


def test_close_design_contract_loop_writes_close_artifact(tmp_path: Path) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close", yes=True)
    )

    assert result.status == "ready"
    assert result.loop_status == "closed"
    assert result.closed is True
    assert result.design_contract is not None
    assert result.design_contract.status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."
    assert result.next_guidance.safety == "no_action"
    assert result.next_guidance.writes_artifacts is False

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-close"
    assert (loop_dir / "design-contract-close.json").is_file()
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_close_design_contract_loop_rechecks_review_digest_at_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-final-review-guard"
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="design-contract",
        loop_id=loop_id,
        review_round_number=1,
    )
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="design-contract",
        round_number=1,
        input_digest=reviewed.input_digest,
        status="completed",
        expert_roles=reviewed.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / loop_id
        / "review-outcome-round-1.json"
    ).write_text(outcome.model_dump_json(indent=2) + "\n", encoding="utf-8")
    original_refresh = design_contract_loop_module._refresh_report_before_close

    def mutate_after_state_validation(*args: object, **kwargs: object) -> object:
        result = original_refresh(*args, **kwargs)
        report_path = (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "design-contract"
            / loop_id
            / "design-contract-report.md"
        )
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n评审后发生变化。\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        design_contract_loop_module,
        "_refresh_report_before_close",
        mutate_after_state_validation,
    )

    with pytest.raises(ReviewInputGuardError, match="review-input-drift"):
        close_design_contract_loop(
            DesignContractCloseOptions(
                root=tmp_path,
                loop_id=loop_id,
                yes=True,
                expected_review_digest=reviewed.input_digest,
            ),
            review_input_validator=validate_review_input_for_close,
        )

    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / loop_id
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_preserves_unchanged_review_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-unchanged-review-guard"
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="design-contract",
        loop_id=loop_id,
        review_round_number=1,
    )
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="design-contract",
        round_number=1,
        input_digest=reviewed.input_digest,
        status="completed",
        expert_roles=reviewed.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / loop_id
        / "review-outcome-round-1.json"
    ).write_text(outcome.model_dump_json(indent=2) + "\n", encoding="utf-8")
    refresh_persistence: list[bool] = []
    original_refresh = design_contract_loop_module._refresh_report_before_close

    def record_refresh_persistence(*args: object, **kwargs: object) -> object:
        refresh_persistence.append(bool(kwargs["persist"]))
        return original_refresh(*args, **kwargs)

    monkeypatch.setattr(
        design_contract_loop_module,
        "_refresh_report_before_close",
        record_refresh_persistence,
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
            expected_review_digest=reviewed.input_digest,
        ),
        review_input_validator=validate_review_input_for_close,
    )

    assert result.status == "ready"
    assert result.loop_status == "closed"
    assert result.closed is True
    assert refresh_persistence == [False]


def test_close_design_contract_loop_recovers_close_written_before_loop_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-close-write-recovery"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    original_write = LoopArtifactStore.write_json_artifact
    close_written = False

    def fail_loop_run_write(
        store: LoopArtifactStore,
        path: Path,
        payload: object,
    ) -> Path:
        nonlocal close_written
        if path.name == "design-contract-close.json":
            written = original_write(store, path, payload)
            close_written = True
            return written
        if close_written and path.name == "loop-run.json":
            raise OSError("injected loop-run write failure")
        return original_write(store, path, payload)

    monkeypatch.setattr(
        LoopArtifactStore,
        "write_json_artifact",
        fail_loop_run_write,
    )
    with pytest.raises(OSError, match="injected loop-run write failure"):
        close_design_contract_loop(
            DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
        )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    assert (loop_dir / "design-contract-close.json").is_file()
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert persisted_run["status"] == "needs_review"
    unchanged_artifacts = {
        name: (loop_dir / name).read_bytes()
        for name in (
            "design-contract-input.json",
            "coverage-matrix.json",
            "design-contract-report.json",
        )
    }

    monkeypatch.setattr(
        LoopArtifactStore,
        "write_json_artifact",
        original_write,
    )
    recovered = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert recovered.status == "ready", recovered.blocker
    assert recovered.closed is True
    assert recovered.loop_status == "closed"
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert persisted_run["status"] == "closed"
    assert unchanged_artifacts == {
        name: (loop_dir / name).read_bytes() for name in unchanged_artifacts
    }


def test_close_design_contract_loop_revalidates_docs_before_partial_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-recovery-revalidates-docs"
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert checked.status == "ready"
    original_write = LoopArtifactStore.write_json_artifact
    close_written = False

    def fail_loop_run_write(
        store: LoopArtifactStore,
        path: Path,
        payload: object,
    ) -> Path:
        nonlocal close_written
        if path.name == "design-contract-close.json":
            written = original_write(store, path, payload)
            close_written = True
            return written
        if close_written and path.name == "loop-run.json":
            raise OSError("injected loop-run write failure")
        return original_write(store, path, payload)

    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", fail_loop_run_write)
    with pytest.raises(OSError, match="injected loop-run write failure"):
        close_design_contract_loop(
            DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
        )

    tasks_path = tmp_path / "specs" / "demo-contract" / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
            "- **验证**：",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", original_write)
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / loop_id
    close_path = loop_dir / "design-contract-close.json"
    original_unlink = Path.unlink
    removed_concurrently = False

    def remove_close_before_recovery_unlink(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal removed_concurrently
        if path == close_path and not removed_concurrently:
            removed_concurrently = True
            original_unlink(path)
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", remove_close_before_recovery_unlink)

    recovered = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert recovered.status == "needs_fix"
    assert recovered.closed is False
    assert removed_concurrently is True
    assert "design-contract-close" not in {
        artifact.kind for artifact in recovered.artifacts
    }
    assert all(
        not evidence.endswith("design-contract-close.json")
        for evidence in recovered.next_guidance.evidence
    )
    persisted_run = json.loads((loop_dir / "loop-run.json").read_text("utf-8"))
    persisted_report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert persisted_run["status"] == "needs_fix"
    assert persisted_report["status"] == "needs_fix"
    assert "task_verification_gap" in {
        finding["code"] for finding in persisted_report["findings"]
    }
    assert not (loop_dir / "design-contract-close.json").exists()

    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：\n",
            "- **验证**：uv run pytest tests/unit/test_demo.py -q\n",
        ),
        encoding="utf-8",
    )
    rechecked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=loop_id,
        )
    )
    assert rechecked.status == "ready"
    assert rechecked.loop_status == "needs_review"

    closed = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )
    assert closed.status == "ready"
    assert closed.loop_status == "closed"
    assert closed.closed is True


def test_close_design_contract_loop_revalidates_changed_docs(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-stale-close",
        )
    )
    tasks_path = tmp_path / "specs" / "demo-contract" / "tasks.md"
    tasks_path.write_text(
        tasks_path.read_text(encoding="utf-8").replace(
            "- **验证**：uv run pytest tests/unit/test_demo.py -q",
            "- **验证**：",
        ),
        encoding="utf-8",
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-stale-close", yes=True)
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.closed is False
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-stale-close"
        / "design-contract-close.json"
    ).exists()

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-stale-close"
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    report = json.loads(
        (loop_dir / "design-contract-report.json").read_text(encoding="utf-8")
    )
    assert loop_run["status"] == "needs_fix"
    assert "task_verification_gap" in {
        finding["code"] for finding in report["findings"]
    }


def test_close_design_contract_loop_repeat_close_keeps_implementation_next_action(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-repeat-close",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-repeat-close", yes=True)
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-repeat-close", yes=True)
    )

    assert result.status == "ready"
    assert result.closed is True
    assert result.loop_status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."
    assert result.next_guidance.safety == "no_action"
    assert result.next_guidance.alternatives == [
        "Start implementation loop for demo-contract."
    ]


def test_check_design_contract_loop_blocks_recheck_of_closed_loop(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-closed-recheck",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-closed-recheck", yes=True)
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-closed-recheck",
        )
    )

    assert result.status == "blocked"
    assert "already closed" in result.blocker
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-closed-recheck"
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"


def test_check_design_contract_loop_preserves_closed_current_default_recheck(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path, loop_id=check_result.loop_id, yes=True
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )

    assert result.status == "ready"
    assert result.closed is True
    assert result.loop_id == check_result.loop_id
    assert result.loop_status == "closed"
    assert result.next_action == "Start implementation loop for demo-contract."

    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["loop_id"] == check_result.loop_id


def test_check_design_contract_loop_dry_run_after_close_stays_preview(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
        )
    )
    close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path, loop_id=check_result.loop_id, yes=True
        )
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-dry-after-close",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.closed is False
    assert result.loop_status == "created"
    assert result.loop_id == "dc-dry-after-close"
    assert result.next_guidance.writes_artifacts is True

    pointer = json.loads(
        (tmp_path / CURRENT_DESIGN_CONTRACT_PATH).read_text(encoding="utf-8")
    )
    assert pointer["loop_id"] == check_result.loop_id
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "design-contract" / "dc-dry-after-close"
    ).exists()


def test_close_design_contract_loop_requires_yes(tmp_path: Path) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close-needs-yes",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close-needs-yes")
    )

    assert result.status == "blocked"
    assert "Pass --yes" in result.blocker
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-close-needs-yes"
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_blocks_unresolved_contract(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path, include_task_refs=False, verification_value="")
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-close-blocked",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-close-blocked", yes=True)
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count >= 2


def test_close_design_contract_loop_blocks_non_current_explicit_loop_id(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-old",
        )
    )
    _write_work_item(
        tmp_path,
        include_task_refs=False,
        relative_path="specs/current-contract",
    )
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/current-contract",
            loop_id="dc-current",
        )
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id="dc-old", yes=True)
    )

    assert result.status == "blocked"
    assert "Only the current design-contract loop can be closed" in result.blocker
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-old"
        / "design-contract-close.json"
    ).exists()


def test_close_design_contract_loop_blocks_pointer_and_run_identity_mismatch(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-identity-target",
        )
    )
    loop_run_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-identity-target"
        / "loop-run.json"
    )
    loop_run = json.loads(loop_run_path.read_text(encoding="utf-8"))
    loop_run["loop_id"] = "dc-identity-other"
    loop_run_path.write_text(json.dumps(loop_run), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-identity-target",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "loop identity" in result.blocker


def test_close_design_contract_loop_blocks_report_work_item_mismatch(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="dc-report-identity",
        )
    )
    report_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "design-contract"
        / "dc-report-identity"
        / "design-contract-report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["work_item_id"] = "other-contract"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = close_design_contract_loop(
        DesignContractCloseOptions(
            root=tmp_path,
            loop_id="dc-report-identity",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "report identity" in result.blocker


def test_close_design_contract_loop_blocks_symlinked_current_pointer(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    outside.joinpath("loop-run.json").write_text("{}", encoding="utf-8")
    link = tmp_path / "linked-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    pointer_path = tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    pointer_path.parent.mkdir(parents=True)
    pointer_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "artifact_kind": "current-design-contract-pointer",
                "loop_id": "dc-symlink",
                "loop_run_path": "linked-outside/loop-run.json",
            }
        ),
        encoding="utf-8",
    )

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, yes=True)
    )

    assert result.status == "blocked"
    assert "must stay within project" in result.blocker


def test_close_design_contract_loop_blocks_current_pointer_file_symlink(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    loop_id = "dc-pointer-file-symlink"
    assert (
        check_design_contract_loop(
            DesignContractCheckOptions(
                root=tmp_path,
                work_item="specs/demo-contract",
                loop_id=loop_id,
            )
        ).status
        == "ready"
    )
    pointer_path = tmp_path / CURRENT_DESIGN_CONTRACT_PATH
    backing = pointer_path.with_name("backing-current-design-contract.json")
    backing.write_bytes(pointer_path.read_bytes())
    pointer_path.unlink()
    pointer_path.symlink_to(backing.name)

    result = close_design_contract_loop(
        DesignContractCloseOptions(root=tmp_path, loop_id=loop_id, yes=True)
    )

    assert result.status == "blocked"
    assert result.closed is False
    assert "pointer is malformed" in result.blocker


def test_check_design_contract_loop_blocks_unsafe_loop_id(tmp_path: Path) -> None:
    _write_work_item(tmp_path)

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id="../bad",
        )
    )

    assert result.status == "blocked"
    assert "Invalid design-contract loop id" in result.blocker
    assert not (tmp_path / ".ai-sdlc" / "loops" / "design-contract").exists()


def test_check_design_contract_loop_blocks_missing_work_item(tmp_path: Path) -> None:
    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/missing-contract",
            loop_id="dc-missing",
        )
    )

    assert result.status == "blocked"
    assert "does not exist" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


@pytest.mark.parametrize("doc_name", ("spec.md", "plan.md", "tasks.md"))
def test_check_design_contract_loop_blocks_symlinked_formal_doc(
    tmp_path: Path,
    doc_name: str,
) -> None:
    work_item = _write_work_item(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-{doc_name}"
    outside.write_text(work_item.joinpath(doc_name).read_text("utf-8"), "utf-8")
    work_item.joinpath(doc_name).unlink()
    try:
        work_item.joinpath(doc_name).symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            work_item="specs/demo-contract",
            loop_id=f"dc-symlinked-{doc_name.removesuffix('.md')}",
        )
    )

    assert result.status == "blocked"
    assert "symlink" in result.blocker.lower()


def test_check_design_contract_loop_uses_checkpoint_feature_spec_dir(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.yml"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        "\n".join(
            [
                "current_stage: execute",
                "feature:",
                "  id: demo-contract",
                "  spec_dir: specs/demo-contract",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            loop_id="dc-checkpoint-spec-dir",
        )
    )

    assert result.status == "ready"
    assert result.work_item_path == "specs/demo-contract"


def test_check_design_contract_loop_prefers_checkpoint_linked_wi_id(
    tmp_path: Path,
) -> None:
    _write_work_item(tmp_path)
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.yml"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(
        "\n".join(
            [
                "current_stage: execute",
                "linked_plan_uri: .cursor/plans/demo.plan.md",
                "linked_wi_id: demo-contract",
            ]
        ),
        encoding="utf-8",
    )

    result = check_design_contract_loop(
        DesignContractCheckOptions(
            root=tmp_path,
            loop_id="dc-checkpoint-linked-wi",
        )
    )

    assert result.status == "ready"
    assert result.work_item_path == "specs/demo-contract"


def _write_work_item(
    root: Path,
    *,
    include_task_refs: bool = True,
    with_frozen_requirement: bool = True,
    requirement_loop_id: str = "req-current",
    requirement_scope_families: tuple[str, ...] = (),
    placeholder: bool = False,
    relative_path: str = "specs/demo-contract",
    task_heading: str = "### Task 1.1 Check contract",
    plan_extra: str = "",
    spec_intro_extra: str = "",
    success_heading: str = "## 成功标准",
    acceptance_label: str = "- **验收标准**",
    verification_label: str = "- **验证**",
    verification_value: str = "uv run pytest tests/unit/test_demo.py -q",
    tasks_intro_extra: str = "",
    tasks_tail_extra: str = "",
    extra_task_sections: str = "",
    spec_status_line: str = "**状态**：已冻结",
    spec_title: str = "# PRD：Demo Contract",
) -> Path:
    work_item = root / relative_path
    work_item.mkdir(parents=True)
    spec_extra = "\nTODO: remove placeholder.\n" if placeholder else ""
    work_item.joinpath("spec.md").write_text(
        "\n".join(
            [
                spec_title,
                "",
                spec_status_line,
                "",
                spec_intro_extra,
                "## 需求",
                "",
                "- **FR-DEMO-001**：系统必须检查合同覆盖。",
                "",
                success_heading,
                "",
                "- **SC-DEMO-001**：合同通过后可以关闭。",
                spec_extra,
            ]
        ),
        encoding="utf-8",
    )
    work_item.joinpath("plan.md").write_text(
        "\n".join(
            [
                "# 实施计划",
                "",
                "## 技术背景",
                "Python runtime.",
                "## 阶段计划",
                "Phase 1.",
                "## 验证策略",
                "Run pytest.",
                "## 回退方式",
                "Revert the commit.",
                plan_extra,
            ]
        ),
        encoding="utf-8",
    )
    refs = "FR-DEMO-001 and SC-DEMO-001" if include_task_refs else "contract docs"
    work_item.joinpath("tasks.md").write_text(
        "\n".join(
            [
                "# 任务分解",
                "",
                tasks_intro_extra,
                task_heading,
                "",
                "- **任务编号**：T11",
                "- **优先级**：P0",
                f"{acceptance_label}：Cover {refs}.",
                f"{verification_label}：{verification_value}",
                extra_task_sections,
                tasks_tail_extra,
            ]
        ),
        encoding="utf-8",
    )
    if with_frozen_requirement:
        _ensure_frozen_requirement_loop(
            root,
            loop_id=requirement_loop_id,
            scope_families=requirement_scope_families,
        )
    return work_item


def _run_git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _ensure_frozen_requirement_loop(
    root: Path,
    *,
    loop_id: str,
    scope_families: tuple[str, ...] = (),
) -> None:
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
            design_scope_families=scope_families,
        )
    )
    assert start_result.status == "ready"
    freeze_result = freeze_requirement_loop(
        RequirementFreezeOptions(root=root, loop_id=loop_id, yes=True)
    )
    assert freeze_result.frozen is True

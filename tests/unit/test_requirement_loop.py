"""Tests for the deterministic requirement loop runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ai_sdlc.core.requirement_loop as requirement_loop_module
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.core.requirement_loop import (
    CURRENT_REQUIREMENT_PATH,
    RequirementFreezeOptions,
    RequirementStartOptions,
    freeze_requirement_loop,
    start_requirement_loop,
)


def test_start_requirement_loop_writes_artifacts(tmp_path: Path) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-001",
            idea="运营用户需要订单审批流，范围只覆盖后台人工审批。",
            acceptance=("审批节点可以配置", "审批记录可以追踪"),
            work_item_id="192-loop-engine-requirement-loop-runtime",
        )
    )

    assert result.status == "ready"
    assert result.loop_id == "req-001"
    assert result.loop_status == "needs_review"
    assert result.acceptance_count == 2
    assert result.source_kind == "idea"
    assert result.source_path == ""
    assert result.requirement is not None
    assert result.requirement.summary == "运营用户需要订单审批流，范围只覆盖后台人工审批。"
    assert result.requirement.source_kind == "idea"
    assert result.requirement.acceptance_count == 2
    assert result.next_action == (
        "Run ai-sdlc loop review --type requirement --loop-id req-001."
    )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-001"
    assert (loop_dir / "loop-run.json").is_file()
    assert (loop_dir / "requirement-intake.json").is_file()
    assert (loop_dir / "requirement-brief.md").is_file()
    assert (loop_dir / "clarification-questions.md").is_file()
    assert (loop_dir / "acceptance-checklist.md").is_file()
    assert (tmp_path / CURRENT_REQUIREMENT_PATH).is_file()

    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["loop_type"] == "requirement"
    assert loop_run["status"] == "needs_review"
    assert loop_run["work_item_id"] == "192-loop-engine-requirement-loop-runtime"

    intake = json.loads(
        (loop_dir / "requirement-intake.json").read_text(encoding="utf-8")
    )
    assert intake["artifact_kind"] == "requirement-intake"
    assert intake["summary"] == "运营用户需要订单审批流，范围只覆盖后台人工审批。"
    assert intake["acceptance_criteria"] == ["审批节点可以配置", "审批记录可以追踪"]


def test_start_requirement_loop_without_acceptance_needs_user(tmp_path: Path) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-needs-user",
            idea="做一个报表",
        )
    )

    assert result.status == "needs_user"
    assert result.loop_status == "needs_user"
    assert result.acceptance_count == 0
    assert result.clarification_count >= 1
    assert "acceptance" in result.next_action
    assert "--loop-id req-needs-user" in result.next_action
    assert "--acceptance" in result.next_action

    checklist = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-needs-user"
        / "acceptance-checklist.md"
    ).read_text(encoding="utf-8")
    assert "待补充" in checklist


def test_start_requirement_loop_reuses_existing_intake_when_adding_acceptance(
    tmp_path: Path,
) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-add-acceptance",
            idea="运营用户需要订单审批流，范围只覆盖后台人工审批。",
        )
    )

    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-add-acceptance",
            acceptance=("审批节点可以配置",),
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "needs_review"
    assert result.acceptance_count == 1
    assert result.summary == "运营用户需要订单审批流，范围只覆盖后台人工审批。"
    assert result.next_action == (
        "Run ai-sdlc loop review --type requirement --loop-id req-add-acceptance."
    )

    intake_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-add-acceptance"
        / "requirement-intake.json"
    )
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    assert intake["raw_text"] == "运营用户需要订单审批流，范围只覆盖后台人工审批。"
    assert intake["acceptance_criteria"] == ["审批节点可以配置"]


def test_start_requirement_loop_generates_unique_default_loop_ids(
    tmp_path: Path,
) -> None:
    first = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="运营用户需要订单审批流，范围只覆盖后台人工审批。",
            acceptance=("审批节点可以配置",),
        )
    )
    second = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="客服用户需要 SLA 提醒，范围只覆盖站内提醒。",
            acceptance=("SLA 超时前可以提醒",),
        )
    )

    assert first.loop_id.startswith("requirement-")
    assert second.loop_id.startswith("requirement-")
    assert first.loop_id != second.loop_id
    assert (
        tmp_path / ".ai-sdlc" / "loops" / "requirement" / first.loop_id
    ).is_dir()
    assert (
        tmp_path / ".ai-sdlc" / "loops" / "requirement" / second.loop_id
    ).is_dir()


def test_start_requirement_loop_dry_run_does_not_write(tmp_path: Path) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-dry-run",
            idea="管理员用户需要导出审计日志，范围只覆盖 CSV。",
            acceptance=("可以下载 CSV",),
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.loop_status == "needs_review"
    assert result.source_kind == "idea"
    assert result.requirement is not None
    assert result.requirement.summary == "管理员用户需要导出审计日志，范围只覆盖 CSV。"
    assert not (tmp_path / ".ai-sdlc").exists()
    assert any(artifact.kind == "loop-run" for artifact in result.artifacts)


def test_start_requirement_loop_reads_input_file(tmp_path: Path) -> None:
    input_path = tmp_path / "requirement.md"
    input_path.write_text("客服用户需要工单提醒，范围只覆盖站内通知。", encoding="utf-8")

    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-file",
            input_file="requirement.md",
            acceptance=("站内通知可见",),
        )
    )

    assert result.status == "ready"
    assert result.source_kind == "input-file"
    assert result.source_path == "requirement.md"
    assert result.requirement is not None
    assert result.requirement.source_kind == "input-file"
    assert result.requirement.source_path == "requirement.md"
    intake = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "requirement"
            / "req-file"
            / "requirement-intake.json"
        ).read_text(encoding="utf-8")
    )
    assert intake["source_kind"] == "input-file"
    assert intake["source_path"] == "requirement.md"


def test_start_requirement_loop_blocks_missing_input(tmp_path: Path) -> None:
    result = start_requirement_loop(RequirementStartOptions(root=tmp_path))

    assert result.status == "blocked"
    assert "requires --idea or --input-file" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


def test_start_requirement_loop_blocks_unknown_design_scope_structurally(
    tmp_path: Path,
) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            idea="Build a service.",
            acceptance=("Service works.",),
            design_scope_families=("unknown",),
            dry_run=True,
        )
    )

    assert result.status == "blocked"
    assert result.result == "Requirement input is invalid."
    assert "unknown design scope families: unknown" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


def test_start_requirement_loop_blocks_unsafe_loop_id(tmp_path: Path) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="../bad",
            idea="运营用户需要订单审批流，范围只覆盖后台人工审批。",
            acceptance=("审批节点可以配置",),
        )
    )

    assert result.status == "blocked"
    assert "Invalid requirement loop id" in result.blocker


def test_start_requirement_loop_blocks_unquoted_command_loop_id(
    tmp_path: Path,
) -> None:
    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="Q3 roadmap",
            idea="Ops users need roadmap approval.",
            acceptance=("Roadmap items can be approved.",),
        )
    )

    assert result.status == "blocked"
    assert "Invalid requirement loop id" in result.blocker
    assert "letters, digits, hyphen, and underscore" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()


def test_freeze_requirement_loop_blocks_unsafe_loop_id(tmp_path: Path) -> None:
    result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="../bad", yes=True)
    )

    assert result.status == "blocked"
    assert "Invalid requirement loop id" in result.blocker


def test_freeze_requirement_loop_blocks_unquoted_command_loop_id(
    tmp_path: Path,
) -> None:
    result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="Q3 roadmap", yes=True)
    )

    assert result.status == "blocked"
    assert "Invalid requirement loop id" in result.blocker
    assert "letters, digits, hyphen, and underscore" in result.blocker


def test_freeze_requirement_loop_blocks_cross_loop_run_substitution(
    tmp_path: Path,
) -> None:
    for loop_id in ("req-target-a", "req-source-b"):
        start_requirement_loop(
            RequirementStartOptions(
                root=tmp_path,
                loop_id=loop_id,
                idea=f"Requirement for {loop_id}.",
                acceptance=(f"{loop_id} is accepted.",),
            )
        )
    root = tmp_path / ".ai-sdlc" / "loops" / "requirement"
    target_run = root / "req-target-a" / "loop-run.json"
    source_run = root / "req-source-b" / "loop-run.json"
    target_run.write_text(source_run.read_text(encoding="utf-8"), encoding="utf-8")

    result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, loop_id="req-target-a", yes=True)
    )

    assert result.status == "blocked"
    assert "loop identity" in result.blocker
    assert not (root / "req-target-a" / "requirement-freeze.json").exists()
    assert not (root / "req-source-b" / "requirement-freeze.json").exists()


def test_freeze_requirement_loop_blocks_current_pointer_identity_mismatch(
    tmp_path: Path,
) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-pointer-target",
            idea="Requirement for the current pointer.",
            acceptance=("The pointer remains bound.",),
        )
    )
    pointer_path = tmp_path / CURRENT_REQUIREMENT_PATH
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["loop_id"] = "req-pointer-other"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, yes=True)
    )

    assert result.status == "blocked"
    assert "pointer" in result.blocker.lower()
    assert "identity" in result.blocker.lower()


@pytest.mark.parametrize("artifact_name", ("loop-run.json", "requirement-intake.json"))
def test_freeze_requirement_loop_rejects_symlinked_identity_artifact(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-symlink-target",
            idea="Requirement artifacts remain local to their loop.",
            acceptance=("Symlinked identity artifacts are rejected.",),
        )
    )
    loop_dir = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-symlink-target"
    )
    artifact = loop_dir / artifact_name
    external = tmp_path / f"external-{artifact_name}"
    external.write_text(artifact.read_text(encoding="utf-8"), encoding="utf-8")
    artifact.unlink()
    try:
        artifact.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = freeze_requirement_loop(
        RequirementFreezeOptions(
            root=tmp_path,
            loop_id="req-symlink-target",
            yes=True,
        )
    )

    assert result.status == "blocked"
    assert "symlink" in result.blocker.lower()
    assert not (loop_dir / "requirement-freeze.json").exists()


def test_freeze_requirement_loop_closes_current_loop(tmp_path: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-freeze",
            idea="财务用户需要付款审批，范围只覆盖国内付款。",
            acceptance=("审批通过后才能付款",),
        )
    )

    result = freeze_requirement_loop(
        RequirementFreezeOptions(root=tmp_path, yes=True, accepted_by="tester")
    )

    assert result.status == "ready"
    assert result.loop_status == "closed"
    assert result.frozen is True
    assert "design-contract" in result.next_action
    assert result.acceptance_count == 1

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-freeze"
    assert (loop_dir / "requirement-freeze.json").is_file()
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"
    assert "design-contract" in loop_run["next_action"]


def test_freeze_requirement_loop_rechecks_review_digest_at_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop_id = "req-final-review-guard"
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id=loop_id,
            idea="运营用户需要任务提醒。",
            acceptance=("任务提醒可以发送",),
        )
    )
    reviewed = resolve_review_input(
        tmp_path,
        loop_type="requirement",
        loop_id=loop_id,
    )
    original_acceptance = requirement_loop_module._requirement_acceptance_result

    def mutate_after_state_validation(*args: object, **kwargs: object) -> object:
        result = original_acceptance(*args, **kwargs)
        brief_path = (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "requirement"
            / loop_id
            / "requirement-brief.md"
        )
        brief_path.write_text(
            brief_path.read_text(encoding="utf-8") + "\n评审后发生变化。\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        requirement_loop_module,
        "_requirement_acceptance_result",
        mutate_after_state_validation,
    )

    with pytest.raises(ReviewInputGuardError, match="review-input-drift"):
        freeze_requirement_loop(
            RequirementFreezeOptions(
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
        / "requirement"
        / loop_id
        / "requirement-freeze.json"
    ).exists()


def test_freeze_requirement_loop_requires_yes(tmp_path: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-no-yes",
            idea="运营用户需要任务提醒，范围只覆盖邮件。",
            acceptance=("邮件可以发送",),
        )
    )

    result = freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path))

    assert result.status == "blocked"
    assert "--yes" in result.next_action
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "requirement"
        / "req-no-yes"
        / "requirement-freeze.json"
    ).exists()


def test_freeze_requirement_loop_blocks_without_acceptance(tmp_path: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-no-acceptance",
            idea="做一个报表",
        )
    )

    result = freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))

    assert result.status == "needs_user"
    assert result.loop_status == "needs_user"
    assert "acceptance criterion" in result.blocker
    loop_run = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "requirement"
            / "req-no-acceptance"
            / "loop-run.json"
        ).read_text(encoding="utf-8")
    )
    assert loop_run["status"] == "needs_user"


def test_freeze_requirement_loop_is_idempotent_after_closed(tmp_path: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-idempotent",
            idea="客服用户需要 SLA 提醒，范围只覆盖站内提醒。",
            acceptance=("SLA 超时前可以提醒",),
        )
    )
    first = freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))
    second = freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))

    assert first.status == "ready"
    assert second.status == "ready"
    assert second.result == "Requirement loop is already frozen."


def test_freeze_requirement_loop_ignores_copied_legacy_review_artifacts(
    tmp_path: Path,
) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-legacy-artifacts",
            idea="Ops users need an ordinary frozen requirement.",
            acceptance=("Requirement closes from its own artifacts.",),
        )
    )
    legacy = tmp_path / ".ai-sdlc" / "state" / "shared" / "scope-authority"
    legacy.mkdir(parents=True)
    (legacy / "copied-certificate.json").write_text("{not-json", encoding="utf-8")

    result = freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))

    assert result.status == "ready"
    assert result.frozen is True
    assert legacy.joinpath("copied-certificate.json").is_file()


def test_start_requirement_loop_blocks_restart_after_freeze(tmp_path: Path) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-frozen-restart",
            idea="财务用户需要付款审批，范围只覆盖国内付款。",
            acceptance=("审批通过后才能付款",),
        )
    )
    freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))

    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-frozen-restart",
            idea="客服用户需要 SLA 提醒，范围只覆盖站内提醒。",
            acceptance=("SLA 超时前可以提醒",),
        )
    )

    assert result.status == "blocked"
    assert "Frozen requirement loops cannot be restarted" in result.blocker
    assert "design-contract" in result.next_action

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-frozen-restart"
    intake = json.loads((loop_dir / "requirement-intake.json").read_text(encoding="utf-8"))
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert intake["raw_text"] == "财务用户需要付款审批，范围只覆盖国内付款。"
    assert loop_run["status"] == "closed"


def test_start_requirement_loop_blocks_restart_when_loop_run_is_closed(
    tmp_path: Path,
) -> None:
    start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-closed-restart",
            idea="财务用户需要付款审批，范围只覆盖国内付款。",
            acceptance=("审批通过后才能付款",),
        )
    )
    freeze_requirement_loop(RequirementFreezeOptions(root=tmp_path, yes=True))
    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "requirement" / "req-closed-restart"
    (loop_dir / "requirement-freeze.json").unlink()

    result = start_requirement_loop(
        RequirementStartOptions(
            root=tmp_path,
            loop_id="req-closed-restart",
            idea="客服用户需要 SLA 提醒，范围只覆盖站内提醒。",
            acceptance=("SLA 超时前可以提醒",),
        )
    )

    assert result.status == "blocked"
    assert "Frozen requirement loops cannot be restarted" in result.blocker
    loop_run = json.loads((loop_dir / "loop-run.json").read_text(encoding="utf-8"))
    assert loop_run["status"] == "closed"

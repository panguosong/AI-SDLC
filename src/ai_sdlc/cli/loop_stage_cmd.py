"""阶段量化的宿主接线；复用原工件与门禁，不建立另一个生命周期。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ai_sdlc.core.loop_decision_service import DecisionPreparationError
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_stage_decision_service import (
    StageDecisionHost,
    StageReviewSnapshot,
    parse_stage_simulation_context,
    read_stage_simulation_context,
    stage_material_digest,
)
from ai_sdlc.core.loop_stage_input import stage_input_identity as stage_input_identity
from ai_sdlc.core.stable_file_read import read_stable_bytes

STAGE_CAPABILITY = "stage-simulation-v1"
STAGE_INPUT_NAMES = {
    "requirement": "requirement-intake.json",
    "design-contract": "design-contract-input.json",
    "implementation": "implementation-input.json",
    "frontend-evidence": "frontend-evidence-input.json",
}


def _stage_input(stage, content):
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.frontend_evidence_models import FrontendEvidenceInput
    from ai_sdlc.core.implementation_models import ImplementationInput
    from ai_sdlc.core.requirement_loop import RequirementIntake

    model = {
        "requirement": RequirementIntake,
        "design-contract": DesignContractInput,
        "implementation": ImplementationInput,
        "frontend-evidence": FrontendEvidenceInput,
    }[stage]
    return model.model_validate_json(content)


def _validate_identity(stage, loop_id, run, stage_input):
    if (
        run.loop_type != stage
        or run.loop_id != loop_id
        or stage_input.loop_id != loop_id
        or run.work_item_id != stage_input.work_item_id
        or (run.decision_mode, run.decision_capability)
        != (stage_input.decision_mode, stage_input.decision_capability)
    ):
        raise DecisionPreparationError("decision-identity-mismatch")


def resolve_stage_decision_host(root: Path, stage: str, loop_id: str):
    from ai_sdlc.cli.loop_review_cmd import (
        _STAGE_ARTIFACTS,
        _resolve_current_stage_state,
        _safe_identifier,
        _stage_source_material,
        _stage_upstream_context,
        _unique_paths,
    )

    root = root.resolve(strict=True)
    _safe_identifier(loop_id)
    _, run_path = _resolve_current_stage_state(root, stage, loop_id)
    directory = run_path.parent
    run = LoopRun.model_validate_json(read_stable_bytes(root, run_path))
    stage_input = _stage_input(
        stage, read_stable_bytes(root, directory / STAGE_INPUT_NAMES[stage])
    )
    _validate_identity(stage, loop_id, run, stage_input)
    if run.decision_capability != STAGE_CAPABILITY:
        raise DecisionPreparationError("decision-capability-mismatch")
    if stage == "implementation":
        from ai_sdlc.core.loop_decision_service import implementation_stage_host

        return implementation_stage_host(root, run, stage_input)
    artifacts = [directory / name for name in _STAGE_ARTIFACTS[stage]]
    actual = _unique_paths(
        [
            *artifacts,
            *_stage_source_material(root, stage, directory),
            *_stage_upstream_context(root, stage, directory),
        ]
    )
    if stage == "frontend-evidence":
        _validate_current_browser_snapshot(root, directory, stage_input)
    if stage == "requirement":
        ready = bool(stage_input.acceptance_criteria)
    else:
        report = json.loads(read_stable_bytes(root, directory / f"{stage}-report.json"))
        ready = report.get("blocker_count") == 0 and report.get("status") in {
            "passed",
            "needs_review",
            "closed",
        }
    started = any(
        (directory / f"review-outcome-round-{number}.json").is_file()
        for number in (1, 2)
    )
    return StageDecisionHost(
        stage_kind=stage,
        loop_id=loop_id,
        input_digest=stage_input_identity(stage, stage_input),
        loop_dir=directory,
        artifact_paths=tuple([run_path, *artifacts]),
        actual_paths=tuple(actual),
        initial_ready=not started and run.status != "closed",
        actual_ready=ready,
        review_started=started,
        execution_started=started,
    )


def _validate_current_browser_snapshot(root, directory, stage_input):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.frontend_evidence_loop import _build_snapshot
    from ai_sdlc.core.frontend_evidence_models import FrontendEvidenceSnapshot

    source = root / stage_input.source_artifact_path
    before = read_stable_bytes(root, source)
    fresh = _build_snapshot(
        root,
        stage_input,
        source,
        review_input_validator=validate_review_input_for_close,
    )
    saved = FrontendEvidenceSnapshot.model_validate_json(
        read_stable_bytes(root, directory / "frontend-evidence-snapshot.json")
    )
    if not isinstance(fresh, FrontendEvidenceSnapshot):
        raise DecisionPreparationError("frontend-browser-source-invalid")
    # 评分只能使用当前浏览器门禁投影；已落盘的旧 PASS 不能覆盖当前失败。
    if read_stable_bytes(root, source) != before or fresh.model_dump(
        exclude={"created_at"}
    ) != saved.model_dump(exclude={"created_at"}):
        raise DecisionPreparationError(
            "frontend-browser-snapshot-stale: refresh the same loop before review"
        )


def read_stage_decision_context(
    root, stage, loop_id, *, purpose="read", round_number=1
):
    directory = root / ".ai-sdlc/loops" / stage / loop_id
    raw_run = json.loads(read_stable_bytes(root, directory / "loop-run.json"))
    try:
        raw_input = json.loads(
            read_stable_bytes(root, directory / STAGE_INPUT_NAMES[stage])
        )
    except ValueError:
        if (
            stage != "implementation"
            and raw_run.get("decision_mode", "legacy") == "legacy"
            and not raw_run.get("decision_capability")
            and not (directory / "decision-context.json").exists()
        ):
            # 旧评审对当前原件逐字节绑定；损坏原件也能成为待审材料，不升级旧实例。
            return None
        raise
    if not isinstance(raw_input, dict):
        if (
            stage != "implementation"
            and raw_run.get("decision_mode", "legacy") == "legacy"
            and not raw_run.get("decision_capability")
            and not (directory / "decision-context.json").exists()
        ):
            return None
        raise DecisionPreparationError("decision-identity-mismatch")
    if (
        stage != "implementation"
        and raw_run.get("decision_mode", "legacy") == "legacy"
        and raw_input.get("decision_mode", "legacy") == "legacy"
    ):
        if (
            raw_run.get("decision_capability")
            or raw_input.get("decision_capability")
            or (directory / "decision-context.json").exists()
        ):
            raise DecisionPreparationError("decision-context-conflicts-with-legacy")
        return None
    run = LoopRun.model_validate_json(
        read_stable_bytes(root, directory / "loop-run.json")
    )
    stage_input = _stage_input(
        stage, read_stable_bytes(root, directory / STAGE_INPUT_NAMES[stage])
    )
    _validate_identity(stage, loop_id, run, stage_input)
    if run.decision_mode == "legacy":
        if stage == "implementation":
            from ai_sdlc.core.loop_decision_service import (
                validate_implementation_context,
            )

            # 旧实现仍须检查同目标的活跃量化实例，不能从通用阶段入口绕过原边界。
            return validate_implementation_context(
                root, run, stage_input, purpose=purpose
            )
        if (directory / "decision-context.json").exists():
            raise DecisionPreparationError("decision-context-conflicts-with-legacy")
        return None
    if run.decision_capability != STAGE_CAPABILITY:
        if stage != "implementation":
            raise DecisionPreparationError("decision-capability-mismatch")
        from ai_sdlc.core.loop_decision_service import validate_implementation_context

        return validate_implementation_context(root, run, stage_input, purpose=purpose)
    host = resolve_stage_decision_host(root, stage, loop_id)
    return read_stage_simulation_context(
        root, host, purpose=purpose, round_number=round_number
    )


def captured_stage_decision_context(root, stage, directory, captured):
    def content(name):
        return captured[(directory / name).relative_to(root).as_posix()]

    run = LoopRun.model_validate_json(content("loop-run.json"))
    stage_input = _stage_input(stage, content(STAGE_INPUT_NAMES[stage]))
    _validate_identity(stage, run.loop_id, run, stage_input)
    context = parse_stage_simulation_context(content("decision-context.json"))
    from ai_sdlc.core.loop_stage_decision_service import validate_stage_start_binding

    validate_stage_start_binding(run, context)
    if (
        context.loop_type != stage
        or context.loop_id != run.loop_id
        or run.decision_capability != STAGE_CAPABILITY
        or context.implementation_input_digest
        != stage_input_identity(stage, stage_input)
    ):
        raise DecisionPreparationError("decision-context-identity-mismatch")
    return context


def stage_review_snapshot(root, stage, loop_id, round_number):
    from ai_sdlc.cli.loop_review_cmd import resolve_review_input

    host = resolve_stage_decision_host(root, stage, loop_id)
    before = stage_material_digest(root, host)
    captured: dict[str, bytes] = {}
    reviewed = resolve_review_input(
        root,
        loop_type=stage,
        loop_id=loop_id,
        review_round_number=round_number,
        captured_artifacts=captured,
        capture_all=True,
    )
    context = captured_stage_decision_context(root, stage, host.loop_dir, captured)
    fresh = resolve_stage_decision_host(root, stage, loop_id)
    if fresh != host or stage_material_digest(root, fresh) != before:
        raise DecisionPreparationError("review-input-drift")
    return StageReviewSnapshot(
        review_input=reviewed,
        context=context,
        manifest={
            path: hashlib.sha256(captured[path]).hexdigest()
            for path in (*reviewed.artifact_paths, *reviewed.upstream_context_paths)
        },
        source_digest=before,
        observed_at_ms=time.time_ns() // 1_000_000,
        context_path=(host.loop_dir / "decision-context.json")
        .relative_to(root)
        .as_posix(),
    )

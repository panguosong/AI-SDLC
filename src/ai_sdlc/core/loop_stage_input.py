"""阶段输入身份和同实例成果刷新；复用原 context、原实际评审和轮次。"""

from __future__ import annotations

import hashlib
import json
import time

from ai_sdlc.core.loop_models import LoopRound, LoopRun
from ai_sdlc.core.stable_file_read import read_stable_bytes

STAGE_CAPABILITY = "stage-simulation-v1"


def validate_verification_identity(stage_input):
    """反例能力与决策能力正交；省略全部字段才表示原有输入。"""
    from ai_sdlc.core.counterexample_models import (
        VERIFICATION_CAPABILITY,
        project_relative_path,
    )

    values = (
        stage_input.verification_capability,
        stage_input.verification_contract_ref,
        stage_input.verification_contract_digest,
    )
    if all(value is None for value in values):
        return
    if not all(values) or values[0] != VERIFICATION_CAPABILITY:
        raise ValueError("counterexample-verification-identity-incomplete")
    if (
        stage_input.decision_mode != "adaptive-quantified"
        or stage_input.decision_capability != STAGE_CAPABILITY
    ):
        raise ValueError("counterexample-decision-capability-unsupported")
    project_relative_path(values[1])


def validate_stage_close_review(root, run, expected_digest, validator):
    """新量化阶段不能利用旧 API 的可选摘要跳过实际评审。"""
    if run.decision_capability != STAGE_CAPABILITY:
        return
    if not expected_digest.strip() or validator is None:
        raise ValueError("quantified-stage-close-review-required")
    from ai_sdlc.core.review_kernel import revalidate_review_input_at_transition

    revalidate_review_input_at_transition(
        root,
        loop_type=run.loop_type,
        loop_id=run.loop_id,
        expected_digest=expected_digest,
        validator=validator,
    )


def stage_input_identity(stage, stage_input):
    """只排除当前阶段允许生成的成果，原目标、上游和授权范围保持冻结。"""
    if stage == "implementation":
        from ai_sdlc.core.implementation_store import implementation_input_digest

        return implementation_input_digest(stage_input)
    payload = stage_input.model_dump(mode="json", exclude={"created_at"})
    excluded = {
        "requirement": ("summary", "clarification_questions", "acceptance_criteria"),
        "design-contract": ("plan_digest", "tasks_digest"),
    }.get(stage, ())
    for key in excluded:
        payload.pop(key, None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def validate_stage_material_update(root, stage, directory, previous, current):
    """写入前核对同一目标与原评审许可，返回是否属于唯一的 R2 成果刷新。"""
    from ai_sdlc.core.loop_decision_service import require_simulation_time_admission
    from ai_sdlc.core.loop_review_service import outcome_path
    from ai_sdlc.core.loop_simulation_context import (
        SimulationContext,
        validate_simulation_context,
    )

    if previous is not None and (
        previous.decision_mode,
        previous.decision_capability,
    ) != (current.decision_mode, current.decision_capability):
        raise ValueError("simulation-stage-decision-identity-change")
    context_path = directory / "decision-context.json"
    run_path = directory / "loop-run.json"
    if run_path.is_file():
        run = LoopRun.model_validate_json(read_stable_bytes(root, run_path))
        if (run.loop_id, run.loop_type, run.decision_mode, run.decision_capability) != (
            current.loop_id,
            stage,
            current.decision_mode,
            current.decision_capability,
        ):
            raise ValueError("simulation-stage-run-identity-mismatch")
    if current.decision_capability != STAGE_CAPABILITY:
        if context_path.exists():
            raise ValueError("decision-context-conflicts-with-legacy")
        return False
    identity = stage_input_identity(stage, current)
    if previous is not None and stage_input_identity(stage, previous) != identity:
        raise ValueError("simulation-stage-input-identity-change")
    if not context_path.exists():
        if any(outcome_path(directory, n).exists() for n in (1, 2)):
            raise ValueError("simulation-context-missing")
        return False
    if previous is None:
        raise ValueError("simulation-existing-context-without-stage-input")
    context = validate_simulation_context(
        SimulationContext.model_validate_json(read_stable_bytes(root, context_path))
    )
    if (
        context.capability != STAGE_CAPABILITY
        or context.loop_type != stage
        or context.loop_id != current.loop_id
        or context.implementation_input_digest != identity
    ):
        raise ValueError("simulation-stage-context-identity-mismatch")
    if context.pending_batch is not None or context.initial_selection_id is None:
        raise ValueError("simulation-comparison-pending-or-selection-missing")
    if context.phase != "review_sealed":
        require_simulation_time_admission(context)
        return False
    _require_stage_revision(root, stage, directory, context)
    return True


def _require_stage_revision(root, stage, directory, context):
    from ai_sdlc.core.loop_decision_service import require_simulation_time_admission
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome
    from ai_sdlc.core.loop_review_service import has_actionable_findings, outcome_path
    from ai_sdlc.core.loop_simulation_context import conditional_improvement_admission
    from ai_sdlc.core.loop_stage_decision_service import (
        StageReviewData,
        StageReviewSnapshot,
        validate_stage_review_data,
    )
    from ai_sdlc.core.review_kernel import ReviewInput

    first = outcome_path(directory, 1)
    if not first.is_file() or outcome_path(directory, 2).exists():
        raise ValueError("simulation-stage-revision-unavailable")
    outcome = LoopReviewOutcome.model_validate_json(read_stable_bytes(root, first))
    if (
        outcome.loop_id != context.loop_id
        or outcome.loop_type != stage
        or outcome.round_number != 1
        or outcome.status != "completed"
        or not isinstance(outcome.simulation, StageReviewData)
    ):
        raise ValueError("simulation-stage-r1-incomplete")
    data = outcome.simulation
    reviewed = ReviewInput(
        loop_id=context.loop_id,
        loop_type=stage,
        round_number=1,
        input_digest=outcome.input_digest,
        artifact_paths=list(data.manifest),
        expert_roles=outcome.expert_roles,
        expert_reasons={
            role: "已保存的原 R1 独立角色" for role in outcome.expert_roles
        },
    )
    snapshot = StageReviewSnapshot(
        review_input=reviewed,
        context=context,
        manifest=data.manifest,
        source_digest=data.source_digest,
        observed_at_ms=data.observed_at_ms,
        context_path=(directory / "decision-context.json").relative_to(root).as_posix(),
    )
    validate_stage_review_data(
        snapshot, data, has_actionable_findings=has_actionable_findings(outcome)
    )
    if data.decision.action not in {"repair", "improve"}:
        raise ValueError("simulation-stage-r1-does-not-permit-revision")
    run = LoopRun.model_validate_json(
        read_stable_bytes(root, directory / "loop-run.json")
    )
    if data.decision.action == "improve" and run.current_round < 2:
        reason = conditional_improvement_admission(
            context,
            source_digest=data.source_digest,
            now_ms=time.time_ns() // 1_000_000,
        )
        if reason:
            raise ValueError(reason)
    require_simulation_time_admission(context, execution_started=True)


def preserve_stage_run(previous: LoopRun | None, current: LoopRun, *, revision=False):
    if previous is None or current.decision_capability != STAGE_CAPABILITY:
        return current
    saved = previous.model_copy(deep=True)
    saved.status, saved.next_action, saved.updated_at = (
        current.status,
        current.next_action,
        current.updated_at,
    )
    # 原生输入摘要跟随实际成果刷新；目标/计时身份仍由未改写的 context 固定。
    saved.input_digest = current.input_digest
    if revision and saved.current_round < 2:
        saved.current_round = 2
        saved.rounds.append(
            LoopRound(
                round_number=2,
                input_artifacts=current.rounds[0].input_artifacts,
                output_artifacts=current.rounds[0].output_artifacts,
                command=current.rounds[0].command,
            )
        )
    if saved.rounds:
        active = next(r for r in saved.rounds if r.round_number == saved.current_round)
        active.status, active.result, active.next_action = (
            current.status,
            current.status,
            current.next_action,
        )
    return saved

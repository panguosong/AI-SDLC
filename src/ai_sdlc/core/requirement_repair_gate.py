"""下游消费冻结 Requirement 时复验已补录的原始修复依据。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_repair_readiness import (
    SUPPLEMENT_NAME,
    read_verified_repair_supplement,
)
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.loop_review_service import (
    effective_actual_action,
    has_actionable_findings,
)
from ai_sdlc.core.loop_stage_decision_service import (
    STAGE_CAPABILITY,
    StageReviewData,
    StageReviewSnapshot,
    parse_stage_simulation_context,
    validate_stage_review_data,
    validate_stage_start_binding,
)
from ai_sdlc.core.loop_stage_input import stage_input_identity
from ai_sdlc.core.requirement_loop import (
    RequirementFreeze,
    RequirementIntake,
    _RequirementArtifacts,
)
from ai_sdlc.core.review_kernel import ReviewInput
from ai_sdlc.core.stable_file_read import (
    _stable_regular_file_exists,
    read_stable_bytes,
)


def validate_frozen_requirement_repair(
    root: Path,
    artifacts: _RequirementArtifacts,
    intake: RequirementIntake,
    freeze: RequirementFreeze,
) -> None:
    """重放量化关闭结果及补录依赖；legacy Requirement 沿用原门禁。"""
    directory = artifacts.loop_dir
    supplement_path = directory / SUPPLEMENT_NAME
    has_supplement = _stable_regular_file_exists(root, supplement_path)
    if intake.decision_capability != STAGE_CAPABILITY and not has_supplement:
        return
    names = (
        "loop-run.json", "requirement-intake.json", "requirement-freeze.json",
        "decision-context.json", "review-outcome-round-1.json",
        "review-outcome-round-2.json", SUPPLEMENT_NAME,
    )

    def capture():
        return {
            name: read_stable_bytes(root, directory / name)
            if _stable_regular_file_exists(root, directory / name)
            else None
            for name in names
        }

    original = capture()
    outcomes = [
        LoopReviewOutcome.model_validate_json(content) if content is not None else None
        for number in (1, 2)
        for content in (original[f"review-outcome-round-{number}.json"],)
    ]
    first, final = outcomes
    if intake.decision_capability == STAGE_CAPABILITY:
        # 正常量化关闭也必须保留 R1/context；一起删除补录足迹不能伪装成未补录。
        if (
            first is None or first.loop_id != intake.loop_id
            or first.loop_type != "requirement" or first.round_number != 1
            or first.status != "completed"
            or not isinstance(first.simulation, StageReviewData)
            or original["decision-context.json"] is None
        ):
            raise ValueError("repair-readiness-closed-origin-missing-or-invalid")
        origin_context = parse_stage_simulation_context(original["decision-context.json"])
        if (
            origin_context.phase != "review_sealed"
            or origin_context.implementation_input_digest != stage_input_identity("requirement", intake)
            or first.simulation.context_digest != origin_context.context_digest
            or first.simulation.selected_route_id != origin_context.initial_selection_id
        ):
            raise ValueError("repair-readiness-closed-origin-mismatch")
        validate_stage_start_binding(
            LoopRun.model_validate_json(original["loop-run.json"]), origin_context
        )
    supplement_relative = supplement_path.relative_to(root).as_posix()
    # 删除补录不能把原 blocked→R2 变成普通关闭；历史足迹仍要求同一补录。
    depends_on_repair = (
        has_supplement
        or (first is not None and isinstance(first.simulation, StageReviewData)
            and first.simulation.decision.action == "blocked"
            and first.simulation.decision.reason == "repair-unavailable")
        or (final is not None and isinstance(final.simulation, StageReviewData)
            and supplement_relative in final.simulation.manifest)
    )
    # 普通 R1 也须重算真实裁决，不能靠改写 action 隐去修复血缘。
    required_names = names if depends_on_repair else names[:5]
    if any(original[name] is None for name in required_names):
        raise ValueError("repair-readiness-closed-dependency-missing")
    run = LoopRun.model_validate_json(original["loop-run.json"])
    current_intake = RequirementIntake.model_validate_json(original["requirement-intake.json"])
    current_freeze = RequirementFreeze.model_validate_json(original["requirement-freeze.json"])
    context = parse_stage_simulation_context(original["decision-context.json"])
    if (
        current_intake != intake or current_freeze != freeze
        or run.loop_id != intake.loop_id or run.loop_type != "requirement"
        or run.work_item_id != intake.work_item_id
        or run.status != "closed" or run.current_round != 1
        or not run.rounds or run.rounds[0].round_number != 1
        or run.rounds[0].status != "closed"
        or (run.decision_mode, run.decision_capability)
        != (intake.decision_mode, intake.decision_capability)
        or context.implementation_input_digest != stage_input_identity("requirement", intake)
        or freeze.acceptance_count != len(intake.acceptance_criteria)
        or freeze.next_loop_type != "design-contract"
    ):
        raise ValueError("repair-readiness-native-close-invalid")
    validate_stage_start_binding(run, context)
    supplement = (
        read_verified_repair_supplement(root, directory, first, context)
        if depends_on_repair else None
    )
    if depends_on_repair and supplement is None:
        raise ValueError("repair-readiness-closed-dependency-missing")

    previous = None
    completed = outcomes if final is not None else outcomes[:1]
    for number, outcome in enumerate(completed, start=1):
        if (
            outcome is None or outcome.loop_id != intake.loop_id
            or outcome.loop_type != "requirement" or outcome.round_number != number
            or outcome.status != "completed"
            or outcome.infra_retry_count is None
            or not isinstance(outcome.simulation, StageReviewData)
            or outcome.simulation.input_digest != outcome.input_digest
            or set(outcome.simulation.assessments) != set(outcome.expert_roles)
            or (previous is not None and (
                (previous.decision.action not in {"repair", "improve"}
                 and supplement is None)
                or previous.input_digest == outcome.input_digest
            ))
        ):
            raise ValueError("repair-readiness-closed-review-invalid")
        data = outcome.simulation
        snapshot = StageReviewSnapshot(
            ReviewInput(
                loop_id=intake.loop_id, loop_type="requirement", round_number=number,
                input_digest=outcome.input_digest, artifact_paths=list(data.manifest),
                expert_roles=outcome.expert_roles,
                expert_reasons={role: "原已完成评审角色" for role in outcome.expert_roles},
            ),
            context, data.manifest, data.source_digest, time.time_ns() // 1_000_000,
        )
        validate_stage_review_data(
            snapshot, data, has_actionable_findings=has_actionable_findings(outcome),
            baseline=previous,
        )
        previous = data
    final = completed[-1]
    assert final is not None and previous is not None
    if has_actionable_findings(final) or effective_actual_action(snapshot, final) != "stop":
        raise ValueError("repair-readiness-closed-review-invalid")

    paths = {
        artifacts.intake_path, artifacts.brief_path, artifacts.questions_path,
        artifacts.checklist_path, directory / "decision-context.json",
        *(root / source.path for source in context.sources),
        *({supplement_path, *(root / path for path in supplement.evidence_manifest)}
          if supplement is not None else set()),
    }
    def capture_material():
        return {
            path.relative_to(root).as_posix(): read_stable_bytes(root, path)
            for path in paths
        }

    material = capture_material()
    if {
        path: hashlib.sha256(content).hexdigest() for path, content in material.items()
    } != previous.manifest:
        raise ValueError("repair-readiness-closed-material-drift")
    captured = {
        (directory / name).relative_to(root).as_posix(): content
        for name, content in original.items() if content is not None
    }
    # 不把先后两次读取拼成有效证明；补录、原 R1、合同和 basis 必须绑定同次捕获。
    if (
        any(material[path] != content for path, content in captured.items() if path in material)
        or (depends_on_repair and read_verified_repair_supplement(
            root, directory, first, context, captured_artifacts={**material, **captured},
        ) != supplement)
        or capture() != original
        # 末次元数据读取也可能与业务材料改动交错；放行前再核对完整消费材料。
        or capture_material() != material
    ):
        raise ValueError("repair-readiness-input-drift")

"""Implementation B1 的一次性准备；不执行模型或业务命令。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from ai_sdlc.core.implementation_models import (
    ImplementationInput,
    ImplementationProgress,
    ImplementationTasks,
)
from ai_sdlc.core.implementation_store import (
    implementation_artifacts,
    implementation_input_digest,
    implementation_task_items_digest,
    validate_explicit_loop_id,
    validate_implementation_lifecycle,
)
from ai_sdlc.core.loop_decision import decide_implementation, evaluate, select_routes
from ai_sdlc.core.loop_decision_models import (
    B1Assessment,
    B1ReviewData,
    DecisionContext,
    DecisionPreparation,
    DecisionPrepareInput,
    DecisionSource,
    EvaluationInput,
    ImplementationDecisionInput,
    ObligationResult,
)
from ai_sdlc.core.loop_models import LoopRun, LoopStatus
from ai_sdlc.core.loop_resource_lock import (
    _implementation_write_guard,
    _ImplementationWriteLockError,
)
from ai_sdlc.core.loop_simulation_context import (
    CAPABILITY,
    SimulationContext,
    SimulationPreparation,
    SimulationPrepareRequest,
    has_receipt,
    request_digest,
    transition_simulation,
    validate_simulation_context,
)
from ai_sdlc.core.quality_command import _RUNTIME_PREFIXES, quality_command_environment
from ai_sdlc.core.review_kernel import ReviewInput, ReviewInputValidator
from ai_sdlc.core.stable_file_read import _stable_regular_file_exists, read_stable_bytes


class DecisionPreparationError(ValueError):
    """准备条件不满足时保持原状态。"""


@dataclass(frozen=True)
class B1ReviewSnapshot:
    """调用方从同次内联读取生成，manifest 不是作者提交的证据声明。"""

    review_input: ReviewInput
    context: DecisionContext | SimulationContext
    manifest: Mapping[str, str]


def build_b1_review_data(
    snapshot: B1ReviewSnapshot,
    assessments_by_role: Mapping[str, B1Assessment],
    *,
    has_actionable_findings: bool,
    baseline: B1ReviewData | None = None,
) -> B1ReviewData:
    """复算当前原件及同合同历史基线，不执行 I/O 或接纳自报聚合分。"""
    if snapshot.context.capability == "stage-simulation-v1":
        from ai_sdlc.core.loop_stage_decision_service import build_stage_review_data

        return build_stage_review_data(
            snapshot,
            assessments_by_role,
            has_actionable_findings=has_actionable_findings,
            baseline=baseline,
        )
    snapshot = _checked_review_snapshot(snapshot)
    if type(has_actionable_findings) is not bool:
        raise DecisionPreparationError("decision-actionable-findings-invalid")
    review = snapshot.review_input
    if set(assessments_by_role) != set(review.expert_roles):
        raise DecisionPreparationError("decision-assessment-role-mismatch")
    assessments = _checked_assessments(assessments_by_role)
    current, readiness = _assessment_input(
        snapshot.context, review.input_digest, snapshot.manifest, assessments
    )
    previous = _historical_input(snapshot, baseline)
    return B1ReviewData(
        input_digest=review.input_digest,
        context_digest=snapshot.context.context_digest,
        selected_route_id=snapshot.context.selection.selected_id,
        manifest=dict(snapshot.manifest),
        assessments=assessments,
        evaluation=evaluate(current, baseline=previous),
        decision=decide_implementation(
            ImplementationDecisionInput(
                current=current,
                round_number=review.round_number,
                has_actionable_findings=has_actionable_findings,
                repair_readiness=readiness,
            )
        ),
    )


def validate_b1_review_data(
    snapshot: B1ReviewSnapshot,
    saved: B1ReviewData,
    *,
    has_actionable_findings: bool,
    baseline: B1ReviewData | None = None,
) -> B1ReviewData:
    """读取/Close 共用原件复算；保存分数、聚合引用和决策均须完全一致。"""
    if snapshot.context.capability == "stage-simulation-v1":
        from ai_sdlc.core.loop_stage_decision_service import validate_stage_review_data

        return validate_stage_review_data(
            snapshot,
            saved,
            has_actionable_findings=has_actionable_findings,
            baseline=baseline,
        )
    saved = B1ReviewData.model_validate(saved.model_dump())
    rebuilt = build_b1_review_data(
        snapshot,
        saved.assessments,
        has_actionable_findings=has_actionable_findings,
        baseline=baseline,
    )
    if saved != rebuilt:
        raise DecisionPreparationError("decision-review-data-mismatch")
    return rebuilt


def _checked_review_snapshot(snapshot: B1ReviewSnapshot) -> B1ReviewSnapshot:
    if type(
        snapshot.review_input.round_number
    ) is not int or snapshot.review_input.round_number not in (1, 2):
        raise DecisionPreparationError("decision-review-round-invalid")
    review = ReviewInput.model_validate(snapshot.review_input.model_dump())
    context = _checked_context(snapshot.context)
    if isinstance(context, SimulationContext) and context.phase != "review_sealed":
        raise DecisionPreparationError("simulation-review-not-sealed")
    manifest = dict(snapshot.manifest)
    paths = set(review.artifact_paths) | set(review.upstream_context_paths)
    context_path = (
        f".ai-sdlc/loops/implementation/{review.loop_id}/decision-context.json"
    )
    if (
        review.loop_type != "implementation"
        or review.loop_id != context.loop_id
        or set(manifest) != paths
        or context_path not in review.artifact_paths
        or not {source.path for source in context.sources}.issubset(paths)
    ):
        raise DecisionPreparationError("decision-review-snapshot-mismatch")
    _check_manifest(manifest)
    return B1ReviewSnapshot(review, context, manifest)


def _check_manifest(manifest: Mapping[str, str]) -> None:
    for path, digest in manifest.items():
        # 复用来源的严格路径/摘要约束，不另造文件访问规则。
        source = DecisionSource(
            id="snapshot",
            path=path,
            sha256=digest,
            locator="snapshot",
            claim="snapshot",
        )
        if source.path != path:
            raise DecisionPreparationError("decision-manifest-path-invalid")


def _checked_assessments(
    assessments: Mapping[str, B1Assessment],
) -> dict[str, B1Assessment]:
    if not 1 <= len(assessments) <= 2 or any(
        not role or role.strip() != role or len(role) > 128 for role in assessments
    ):
        raise DecisionPreparationError("decision-assessment-role-mismatch")
    return {
        role: B1Assessment.model_validate(assessment.model_dump())
        for role, assessment in sorted(assessments.items())
    }


def _assessment_input(
    context: DecisionContext | SimulationContext,
    input_digest: str,
    manifest: Mapping[str, str],
    assessments: Mapping[str, B1Assessment],
) -> tuple[EvaluationInput, str]:
    goal_contract = (
        context.goal_contract
        if isinstance(context, SimulationContext)
        else context.route_contract.goal_contract
    )
    return merge_actual_assessments(
        goal_contract=goal_contract,
        context_digest=context.context_digest,
        selected_route_id=context.selection.selected_id,
        input_digest=input_digest,
        manifest=manifest,
        assessments=assessments,
        context_path=f".ai-sdlc/loops/implementation/{context.loop_id}/decision-context.json",
    )


def merge_actual_assessments(
    *,
    goal_contract,
    context_digest: str,
    selected_route_id: str,
    input_digest: str,
    manifest: Mapping[str, str],
    assessments: Mapping[str, B1Assessment],
    context_path: str,
) -> tuple[EvaluationInput, str]:
    """所有阶段复用逐义务保守合并；预测和 context 不能自证实际通过。"""
    obligation_ids = {item.id for item in goal_contract.obligations}
    role_results = {}
    readiness = []
    for role, assessment in assessments.items():
        if (
            assessment.input_digest != input_digest
            or assessment.context_digest != context_digest
            or assessment.selected_route_id != selected_route_id
            or {item.id for item in assessment.results} != obligation_ids
        ):
            raise DecisionPreparationError("decision-assessment-identity-or-coverage")
        for source in assessment.evidence:
            if (
                manifest.get(source.path) != source.sha256
                or source.path == context_path
            ):
                raise DecisionPreparationError("decision-assessment-evidence-unbound")
        role_results[role] = {item.id: item for item in assessment.results}
        readiness.extend(
            (
                assessment.repair_readiness.authorization,
                assessment.repair_readiness.facts,
                assessment.repair_readiness.verification,
            )
        )
    rank = {"PASS": 0, "UNKNOWN": 1, "FAIL": 2}
    merged = []
    canonical = set()
    for obligation_id in sorted(obligation_ids):
        references = tuple(
            f"expert:{role}:obligation:{obligation_id}" for role in role_results
        )
        if canonical.intersection(references) or len(set(references)) != len(
            references
        ):
            raise DecisionPreparationError("decision-assessment-reference-ambiguous")
        canonical.update(references)
        statuses = [rows[obligation_id].status for rows in role_results.values()]
        merged.append(
            ObligationResult(
                id=obligation_id,
                status=max(statuses, key=rank.__getitem__),
                evidence_refs=references,
                # 长理由和各 64 条原引用保留在原件；聚合不拼接或截断它们。
                reason="; ".join(
                    f"{role}={rows[obligation_id].status}"
                    for role, rows in role_results.items()
                ),
            )
        )
    return (
        EvaluationInput(
            contract=goal_contract, artifact_digest=input_digest, results=tuple(merged)
        ),
        max(readiness, key=rank.__getitem__),
    )


def _historical_input(
    snapshot: B1ReviewSnapshot, baseline: B1ReviewData | None
) -> EvaluationInput | None:
    if snapshot.review_input.round_number == 1:
        if baseline is not None:
            raise DecisionPreparationError("decision-baseline-not-allowed")
        return None
    if baseline is None:
        raise DecisionPreparationError("decision-baseline-missing")
    baseline = B1ReviewData.model_validate(baseline.model_dump())
    if (
        baseline.context_digest != snapshot.context.context_digest
        or baseline.selected_route_id != snapshot.context.selection.selected_id
        or baseline.input_digest == snapshot.review_input.input_digest
    ):
        raise DecisionPreparationError("decision-baseline-identity-mismatch")
    _check_manifest(baseline.manifest)
    old, _ = _assessment_input(
        snapshot.context,
        baseline.input_digest,
        baseline.manifest,
        _checked_assessments(baseline.assessments),
    )
    # 历史原件对保存索引重解析；不能拿 R1 摘要读取当前已修改的 R2 文件。
    # 原 findings 对历史 decision 的校验仍由外层既有 outcome 状态机负责。
    if baseline.evaluation != evaluate(old):
        raise DecisionPreparationError("decision-baseline-evaluation-mismatch")
    return old


def _checked_context(
    context: DecisionContext | SimulationContext,
) -> DecisionContext | SimulationContext:
    if isinstance(context, SimulationContext):
        return validate_simulation_context(context)
    context = DecisionContext.model_validate(context.model_dump())
    if (
        context.context_digest
        != _hash(context.model_dump(mode="json", exclude={"context_digest"}))
        or context.selection
        != select_routes(context.route_contract, context.candidates)
        or context.selection.selected_id is None
    ):
        raise DecisionPreparationError("decision-context-invalid")
    return context


def validate_captured_implementation_context(
    run: LoopRun,
    impl_input: ImplementationInput,
    context: DecisionContext | SimulationContext,
) -> DecisionContext | SimulationContext:
    """校验同次捕获的三份身份，不另行读取文件或刷新摘要。"""
    run = LoopRun.model_validate(run.model_dump())
    impl_input = ImplementationInput.model_validate(impl_input.model_dump())
    _check_identity(run, impl_input, run.loop_id)
    context = _checked_context(context)
    if (
        context.loop_id != run.loop_id
        or context.capability != run.decision_capability
        or context.implementation_input_digest != run.input_digest
        or (
            context.capability == "stage-simulation-v1"
            and context.loop_type != "implementation"
        )
    ):
        raise DecisionPreparationError("decision-context-invalid")
    if context.capability == "stage-simulation-v1":
        from ai_sdlc.core.loop_stage_decision_service import (
            validate_stage_start_binding,
        )

        validate_stage_start_binding(run, context)
    return context


def prepare_implementation_decision(
    root: Path,
    loop_id: str,
    request: DecisionPrepareInput,
    *,
    dry_run: bool = True,
    expected_digest: str = "",
) -> DecisionPreparation:
    root = root.resolve(strict=True)
    validate_explicit_loop_id(loop_id)
    request = DecisionPrepareInput.model_validate(request)
    if type(dry_run) is not bool:
        raise DecisionPreparationError("decision-dry-run-invalid")
    try:
        preview = _prepare(root, loop_id, request)
        if dry_run or preview.status == "existing":
            return preview
        if not expected_digest or preview.prepare_digest != expected_digest:
            raise DecisionPreparationError("decision-prepare-digest-mismatch")
        with _implementation_write_guard(root, loop_id):
            current = _prepare(root, loop_id, request)
            if current.status == "existing":
                return current
            if current.prepare_digest != expected_digest:
                raise DecisionPreparationError("decision-prepare-digest-mismatch")
            if current.context is None:
                return current.model_copy(update={"status": "no-safe-route"})
            path = (
                implementation_artifacts(root, loop_id).loop_dir
                / "decision-context.json"
            )
            _write_context(path, current.context)
            return current.model_copy(update={"status": "prepared"})
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, DecisionPreparationError):
            raise
        raise DecisionPreparationError(str(exc)) from exc


def validate_implementation_context(
    root: Path, run: LoopRun, impl_input: ImplementationInput, *, purpose: str = "read",
    reviewed_input: ReviewInput | None = None,
) -> DecisionContext | SimulationContext | None:
    run = LoopRun.model_validate(run.model_dump())
    impl_input = ImplementationInput.model_validate(impl_input.model_dump())
    if (run.decision_mode, run.decision_capability) != (
        impl_input.decision_mode,
        impl_input.decision_capability,
    ):
        raise DecisionPreparationError("decision-identity-mismatch")
    validate_implementation_lifecycle(root, impl_input)
    if run.decision_mode == "legacy" and impl_input.decision_mode == "legacy":
        # 不靠文件启用新模式；已有决策上下文与旧身份冲突时也不能降级。
        path = (
            implementation_artifacts(root, run.loop_id).loop_dir
            / "decision-context.json"
        )
        if _optional_bytes(root, path) is not None:
            raise DecisionPreparationError("decision-context-conflicts-with-legacy")
        validate_legacy_implementation_identity(
            root, run, impl_input, preparing_review=purpose == "review",
            reviewed_input=reviewed_input,
        )
        validate_implementation_requirement(root, impl_input)
        return None
    _check_identity(run, impl_input, run.loop_id)
    validate_implementation_upstream(root, impl_input)
    path = (
        implementation_artifacts(root, run.loop_id).loop_dir / "decision-context.json"
    )
    content = _optional_bytes(root, path)
    if content is None:
        raise DecisionPreparationError(
            "decision-context-missing: run loop decision-prepare"
        )
    context = validate_captured_implementation_context(
        run, impl_input, parse_implementation_context(content)
    )
    validate_implementation_source_boundary(
        root, [root / source.path for source in context.sources]
    )
    if context.capability == "stage-simulation-v1":
        from ai_sdlc.core.loop_stage_decision_service import (
            read_stage_simulation_context,
        )

        # 此处只读取决定身份；反例每步前后验不能重复计算未被消费的验收报告。
        host = implementation_stage_host(
            root, run, impl_input, _evaluate_actual_readiness=False
        )
        context = read_stage_simulation_context(root, host, purpose=purpose)
        if purpose in {"execute", "verification"} and (
            context.initial_selection_id is None
            or context.selected_candidate is None
            or context.pending_batch is not None
        ):
            raise DecisionPreparationError("simulation-initial-selection-missing")
        if purpose == "execute" and context.phase == "review_sealed":
            _require_stage_implementation_execution(root, host, context)
        return context
    if isinstance(context, SimulationContext):
        if purpose == "review" and context.phase != "review_sealed":
            raise DecisionPreparationError("simulation-review-not-sealed")
        if purpose in {"execute", "verification"}:
            if (
                context.initial_selection_id is None
                or context.selected_candidate is None
                or context.pending_batch is not None
            ):
                raise DecisionPreparationError("simulation-initial-selection-missing")
            if purpose == "execute":
                require_simulation_time_admission(
                    context,
                    execution_started=implementation_execution_started(
                        root, run.loop_id
                    ),
                )
                if context.phase == "review_sealed":
                    from ai_sdlc.cli.loop_review_cmd import prepare_current_loop_review

                    prepared, _ = prepare_current_loop_review(
                        root, "implementation", run.loop_id
                    )
                    previous = prepared.baseline_outcome or prepared.current_outcome
                    if (
                        previous is None
                        or previous.round_number != 1
                        or previous.simulation is None
                        or previous.simulation.decision.action != "repair"
                        or prepared.status not in {"needs_fix", "review_missing"}
                    ):
                        raise DecisionPreparationError(
                            "simulation-review-allows-no-execution"
                        )
    return context


def parse_implementation_context(content: bytes) -> DecisionContext | SimulationContext:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise DecisionPreparationError("decision-context-invalid")
    if payload.get("capability") in {CAPABILITY, "stage-simulation-v1"}:
        return SimulationContext.model_validate(payload)
    return DecisionContext.model_validate(payload)


def implementation_execution_started(root: Path, loop_id: str) -> bool:
    progress = ImplementationProgress.model_validate_json(
        read_stable_bytes(root, implementation_artifacts(root, loop_id).progress_path)
    )
    if progress.loop_id != loop_id:
        raise DecisionPreparationError("decision-progress-identity-mismatch")
    return _implementation_progress_started(root, loop_id, progress)


def _implementation_progress_started(
    root: Path, loop_id: str, progress: ImplementationProgress
) -> bool:
    if any(
        item.status in {"in_progress", "done"} or item.quality_results
        for item in progress.tasks
    ):
        return True
    from ai_sdlc.core.counterexample_execution import counterexample_execution_started

    # 新执行在原进度中一次记录；仅旧 pending 或中断写入需要读取原尝试证明。
    return counterexample_execution_started(root, loop_id)


def require_simulation_time_admission(
    context: SimulationContext, *, execution_started: bool = False
) -> None:
    candidate = context.selected_candidate
    now_ms = time.time_ns() // 1_000_000
    earliest = context.started_at_ms
    if context.capability == "stage-simulation-v1":
        earliest = max(earliest, context.last_observed_at_ms or earliest)
    if now_ms < earliest:
        raise DecisionPreparationError("simulation-clock-moved-backwards")
    elapsed = (now_ms - context.started_at_ms + 999) // 1000
    if candidate is None or candidate.future_cost_estimate is None:
        raise DecisionPreparationError("simulation-time-estimate-unavailable")
    # 完整初始报价只在首次执行前准入；后续消耗已在 elapsed 内，不能再收一遍。
    # 此分支只检查原窗口，不把尚未观测的剩余成本声明为零。
    if execution_started:
        if elapsed >= context.plan.time_plan.window_seconds:
            raise DecisionPreparationError("simulation-model-plan-not-feasible")
        return
    if (
        elapsed + candidate.future_cost_estimate.upper_seconds
        > context.plan.time_plan.window_seconds
    ):
        raise DecisionPreparationError("simulation-model-plan-not-feasible")


def _check_identity(
    run: LoopRun, impl_input: ImplementationInput, loop_id: str
) -> None:
    if (
        run.loop_type != "implementation"
        or run.decision_mode != "adaptive-quantified"
        or impl_input.decision_mode != run.decision_mode
        or run.decision_capability
        not in {"implementation-b1", CAPABILITY, "stage-simulation-v1"}
        or impl_input.decision_capability != run.decision_capability
        or run.loop_id != loop_id
        or impl_input.loop_id != loop_id
        or run.work_item_id != impl_input.work_item_id
        or implementation_input_digest(impl_input) != run.input_digest
    ):
        raise DecisionPreparationError("decision-identity-mismatch")


def _prepare(
    root: Path, loop_id: str, request: DecisionPrepareInput
) -> DecisionPreparation:
    artifacts = implementation_artifacts(root, loop_id)
    before = _snapshot(root, loop_id, request)
    run = LoopRun.model_validate_json(read_stable_bytes(root, artifacts.loop_run_path))
    impl_input = ImplementationInput.model_validate_json(
        read_stable_bytes(root, artifacts.input_path)
    )
    _check_identity(run, impl_input, loop_id)
    if run.decision_capability != "implementation-b1":
        raise DecisionPreparationError("decision-prepare-capability-mismatch")
    validate_implementation_lifecycle(root, impl_input)
    path = artifacts.loop_dir / "decision-context.json"
    if _optional_bytes(root, path) is not None:
        context = validate_implementation_context(root, run, impl_input)
        assert context is not None
        original = context.model_dump(
            mode="json", include={"route_contract", "candidates", "sources"}
        )
        if original != request.model_dump(mode="json"):
            raise DecisionPreparationError("decision-context-frozen")
        _require_stable_snapshot(before, _snapshot(root, loop_id, request))
        return DecisionPreparation(
            status="existing",
            prepare_digest=_hash(before),
            context=context,
            selection=context.selection,
        )

    _admit_initial_prepare(root, run, impl_input)
    _check_sources(root, artifacts.loop_dir, request)
    selection = select_routes(request.route_contract, request.candidates)
    payload = {
        **request.model_dump(mode="json"),
        "schema_version": "implementation-b1",
        "capability": "implementation-b1",
        "loop_id": loop_id,
        "implementation_input_digest": run.input_digest,
        "selection": selection.model_dump(mode="json"),
    }
    context = (
        DecisionContext.model_validate({**payload, "context_digest": _hash(payload)})
        if selection.selected_id is not None
        else None
    )
    _require_stable_snapshot(before, _snapshot(root, loop_id, request))
    return DecisionPreparation(
        status="preview",
        prepare_digest=_hash(
            {"state": before, "request": request.model_dump(mode="json")}
        ),
        context=context,
        selection=selection,
    )


def _admit_initial_prepare(
    root: Path, run: LoopRun, impl_input: ImplementationInput
) -> None:
    from ai_sdlc.core.loop_review_service import outcome_path

    validate_implementation_upstream(root, impl_input)
    artifacts = implementation_artifacts(root, run.loop_id)
    progress = ImplementationProgress.model_validate_json(
        read_stable_bytes(root, artifacts.progress_path)
    )
    tasks = ImplementationTasks.model_validate_json(
        read_stable_bytes(root, artifacts.tasks_path)
    )
    if (
        progress.loop_id != run.loop_id
        or tasks.loop_id != run.loop_id
        or progress.work_item_id != impl_input.work_item_id
        or tasks.work_item_id != impl_input.work_item_id
        or implementation_task_items_digest(tasks.items) != impl_input.tasks_digest
        or {item.task_id for item in progress.tasks}
        != {item.task_id for item in tasks.items}
        or len(progress.tasks) != len(tasks.items)
    ):
        raise DecisionPreparationError("decision-progress-identity-mismatch")
    if (
        run.status not in {LoopStatus.RUNNING, LoopStatus.CREATED}
        or _optional_bytes(root, artifacts.close_path) is not None
        or any(
            _optional_bytes(root, outcome_path(artifacts.loop_dir, number)) is not None
            for number in (1, 2)
        )
        or any(
            item.status != "pending"
            or item.evidence
            or item.verification_commands
            or item.quality_results
            or item.note
            for item in progress.tasks
        )
    ):
        raise DecisionPreparationError("decision-prepare-already-started")


def _check_sources(root: Path, loop_dir: Path, request: DecisionPrepareInput) -> None:
    validate_implementation_source_boundary(
        root, [root / source.path for source in request.sources]
    )
    for source in request.sources:
        path = root / source.path
        if path.is_relative_to(loop_dir):
            raise DecisionPreparationError("decision-source-self-reference")
        if hashlib.sha256(read_stable_bytes(root, path)).hexdigest() != source.sha256:
            raise DecisionPreparationError("decision-source-digest-mismatch")


def validate_legacy_implementation_identity(
    root: Path, run: LoopRun, impl_input: ImplementationInput | None,
    *, preparing_review: bool = False,
    reviewed_input: ReviewInput | None = None,
    review_input_validator: ReviewInputValidator | None = None,
) -> bool:
    """只把没有原生绑定足迹的历史凭据视为 opaque；返回是否可沿用旧读取协议。"""
    artifacts = implementation_artifacts(root, run.loop_id)
    reviews = {
        path: _optional_bytes(root, path)
        for path in (
            artifacts.loop_dir / f"review-outcome-round-{number}.json"
            for number in (1, 2)
        )
    }
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome

    outcomes = []
    for number, content in enumerate(reviews.values(), 1):
        if content is None:
            continue
        outcome = LoopReviewOutcome.model_validate_json(content)
        if (outcome.loop_id, outcome.loop_type, outcome.round_number) != (
            run.loop_id, "implementation", number,
        ) or outcome.b1 is not None or outcome.simulation is not None:
            raise DecisionPreparationError("decision-identity-mismatch")
        outcomes.append(outcome)
    if outcomes and outcomes[0].round_number != 1:
        raise DecisionPreparationError("review-outcome-sequence-invalid")
    native_footprint = any(
        item.input_artifacts or item.output_artifacts for item in run.rounds
    ) or any(content is not None for content in reviews.values())
    bound = bool(run.input_digest) or native_footprint
    if (
        run.loop_type != "implementation"
        or run.decision_mode != "legacy"
        or (bound and (impl_input is None or not run.input_digest))
    ):
        raise DecisionPreparationError("decision-identity-mismatch")
    if native_footprint and run.status == LoopStatus.CLOSED:
        from ai_sdlc.core.loop_review_service import has_actionable_findings

        # 窄摘要只证明输入身份；原生闭后消费仍须保留完整、已通过的正式评审序列。
        if not outcomes:
            raise DecisionPreparationError("review-result-missing")
        if len(outcomes) == 2 and (
            outcomes[0].status != "completed" or not has_actionable_findings(outcomes[0])
        ):
            raise DecisionPreparationError("review-outcome-sequence-invalid")
        if outcomes[-1].status != "completed":
            raise DecisionPreparationError("review-execution-failed")
        if has_actionable_findings(outcomes[-1]):
            raise DecisionPreparationError("review-findings-actionable")
    if impl_input is not None:
        if reviewed_input is not None and (
            reviewed_input.loop_id, reviewed_input.loop_type,
            reviewed_input.implementation_input_digest,
        ) != (
            run.loop_id, "implementation", implementation_input_digest(impl_input),
        ):
            raise DecisionPreparationError("decision-identity-mismatch")
        if (
            impl_input.loop_id != run.loop_id
            or impl_input.work_item_id != run.work_item_id
            or (impl_input.decision_mode, impl_input.decision_capability)
            != (run.decision_mode, run.decision_capability)
            or (bound and implementation_input_digest(impl_input) != run.input_digest)
        ):
            raise DecisionPreparationError("decision-identity-mismatch")
        if native_footprint:
            execution = next(
                (item for item in run.rounds if item.round_kind == "execution"), None,
            )
            # 原始执行路径是独立的上游身份；重算已改输入的摘要不能替换原 Design。
            expected = [
                impl_input.spec_path, impl_input.plan_path, impl_input.tasks_path,
                impl_input.design_contract_report_path,
            ]
            design_report = (
                Path(".ai-sdlc/loops/design-contract")
                / impl_input.design_contract_loop_id / "design-contract-report.json"
            ).as_posix()
            if (
                execution is None or execution.input_artifacts != expected
                or (impl_input.design_contract_loop_id
                    and impl_input.design_contract_report_path != design_report)
            ):
                raise DecisionPreparationError("decision-identity-mismatch")
        for outcome in outcomes:
            if (
                outcome.implementation_input_digest is not None
                and outcome.implementation_input_digest != implementation_input_digest(impl_input)
            ):
                raise DecisionPreparationError("decision-identity-mismatch")
        if outcomes and outcomes[-1].implementation_input_digest is None:
            final = outcomes[-1]
            if reviewed_input is None and review_input_validator is not None:
                reviewed_input = review_input_validator(
                    root, loop_type="implementation", loop_id=run.loop_id,
                    expected_digest=final.input_digest,
                )
            elif reviewed_input is None and not preparing_review:
                from ai_sdlc.cli.loop_review_cmd import (
                    resolve_review_input,
                    validate_review_input_for_close,
                )

                # 构造快照时沿 purpose=review 返回，外层再核原摘要；闭后仍须通过质量门禁。
                if run.status == LoopStatus.CLOSED:
                    reviewed_input = validate_review_input_for_close(
                        root, loop_type="implementation", loop_id=run.loop_id,
                        expected_digest=final.input_digest,
                    )
                else:
                    reviewed_input = resolve_review_input(
                        root, loop_type="implementation", loop_id=run.loop_id,
                        review_round_number=final.round_number,
                    )
            if reviewed_input is not None:
                if not isinstance(reviewed_input, ReviewInput) or (
                    reviewed_input.loop_id, reviewed_input.loop_type,
                    reviewed_input.round_number, reviewed_input.input_digest,
                    reviewed_input.implementation_input_digest,
                ) != (
                    run.loop_id, "implementation", final.round_number,
                    final.input_digest, implementation_input_digest(impl_input),
                ):
                    raise DecisionPreparationError("decision-legacy-review-binding-unavailable")
            elif not preparing_review:
                # 仅允许只读构造完整旧快照；执行/写入仍须得到可核对的原摘要。
                raise DecisionPreparationError("decision-legacy-review-binding-unavailable")
    if any(_optional_bytes(root, path) != content for path, content in reviews.items()):
        raise DecisionPreparationError("decision-identity-mismatch")
    return not bound


def validate_implementation_requirement(
    root: Path, impl_input: ImplementationInput,
    *, allow_unbound_legacy: bool = False,
) -> None:
    """legacy 也须保留显式 Requirement 依据，但不重新冻结当前设计文档。"""
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.design_contract_store import (
        design_contract_artifacts,
        require_design_check_published,
    )
    from ai_sdlc.core.design_contract_store import (
        validate_explicit_loop_id as validate_design_id,
    )
    from ai_sdlc.core.implementation_loop import (
        _bound_design_input,
        _design_requirement_issue,
    )

    design_id = impl_input.design_contract_loop_id.strip()
    if not design_id:
        return
    try:
        artifacts = design_contract_artifacts(root, validate_design_id(design_id))
        require_design_check_published(artifacts.input_path.parent)
        captured = {
            artifacts.loop_run_path: _optional_bytes(root, artifacts.loop_run_path),
            artifacts.input_path: read_stable_bytes(root, artifacts.input_path),
        }
        run_content = captured[artifacts.loop_run_path]
        if run_content is None:
            # 无摘要的历史关闭凭据可附带未绑定 Requirement 的旧输入；原生绑定不能降级。
            contract_input = DesignContractInput.model_validate_json(captured[artifacts.input_path])
            if (
                not allow_unbound_legacy
                or contract_input.requirement_loop_id.strip()
                or contract_input.loop_id != design_id
                or contract_input.work_item_id != impl_input.work_item_id
            ):
                raise ValueError("design-contract bound run unavailable")
        else:
            contract_input = _bound_design_input(
                LoopRun.model_validate_json(run_content),
                captured[artifacts.input_path], design_id, impl_input.work_item_id,
            )
        blocker, _ = _design_requirement_issue(root, contract_input)
        if blocker:
            raise ValueError(blocker)
        require_design_check_published(artifacts.input_path.parent)
        if any(_optional_bytes(root, path) != content for path, content in captured.items()):
            raise ValueError("design-contract bound input changed during verification")
    except (OSError, ValueError) as exc:
        raise DecisionPreparationError(f"decision-upstream-changed: {exc}") from exc


def validate_implementation_upstream(
    root: Path, impl_input: ImplementationInput
) -> None:
    """准备与后续门禁共用原 Design 绑定，不把当前文档重冻成新合同。"""
    from ai_sdlc.core.implementation_loop import _design_contract_gate

    _, report_path, blocker, _ = _design_contract_gate(
        root, impl_input.design_contract_loop_id, work_item_id=impl_input.work_item_id
    )
    if blocker or report_path != impl_input.design_contract_report_path:
        raise DecisionPreparationError(f"decision-upstream-changed: {blocker}")


def validate_implementation_source_boundary(root: Path, paths: Sequence[Path]) -> None:
    """框架派生状态不能自证；业务目录同名文件仍是合法实施材料。"""
    excluded_names = {
        "loop-run.json",
        "requirement-freeze.json",
        "design-contract-close.json",
        "implementation-close.json",
        "frontend-evidence-close.json",
        "review-continuation.json",
    }
    for path in paths:
        relative = path.relative_to(root).parts
        if (
            relative[:2] == (".ai-sdlc", "work-items")
            and len(relative) == 5
            and re.fullmatch(
                r"review-round-\d+-attempt-(?:0|1-receipt)\.json", path.name
            )
        ):
            raise DecisionPreparationError("decision-source-derived-state-forbidden")
        if relative[:2] == (".ai-sdlc", "loops") and (
            path.name in excluded_names
            or re.fullmatch(r"review-outcome-round-\d+\.json", path.name)
            or (len(relative) == 4 and path.name == f"current-{relative[2]}.json")
        ):
            raise DecisionPreparationError("decision-source-derived-state-forbidden")


def _snapshot(
    root: Path, loop_id: str, request: DecisionPrepareInput
) -> dict[str, object]:
    from ai_sdlc.core.loop_review_service import outcome_path

    artifacts = implementation_artifacts(root, loop_id)
    paths = (
        artifacts.loop_run_path,
        artifacts.input_path,
        artifacts.tasks_path,
        artifacts.progress_path,
        artifacts.evidence_path,
        artifacts.close_path,
        artifacts.loop_dir / "decision-context.json",
        *(outcome_path(artifacts.loop_dir, number) for number in (1, 2)),
    )
    return {
        "artifacts": {
            path.name: _bytes_digest(_optional_bytes(root, path)) for path in paths
        },
        "source": _source_boundary(root),
        "referenced_sources": {
            source.path: _bytes_digest(read_stable_bytes(root, root / source.path))
            for source in request.sources
        },
    }


def _source_boundary(root: Path) -> dict[str, object]:
    # 只读 Git 清单和工作文件；不用会写对象库的 write-tree，也不运行过滤器。
    environment = quality_command_environment(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*args: str) -> bytes:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *args,
            ],
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise DecisionPreparationError("decision-source-git-unavailable")
        return result.stdout

    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise DecisionPreparationError("decision-source-root-mismatch")
    head = os.fsdecode(git("rev-parse", "--verify", "HEAD")).strip()
    index = git("ls-files", "--stage", "-z")
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    files = {}
    for raw in sorted(set(names.split(b"\0")) - {b""}):
        relative = os.fsdecode(raw)
        if relative.startswith(_RUNTIME_PREFIXES):
            continue
        path = root / relative
        content = _optional_bytes(root, path)
        files[relative] = {
            "digest": _bytes_digest(content),
            "mode": stat.S_IMODE(path.lstat().st_mode) if content is not None else None,
        }
    return {"head": head, "index": _bytes_digest(index), "files": files}


def _optional_bytes(root: Path, path: Path) -> bytes | None:
    return (
        read_stable_bytes(root, path)
        if _stable_regular_file_exists(root, path)
        else None
    )


def _bytes_digest(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _require_stable_snapshot(before: object, after: object) -> None:
    if before != after:
        raise DecisionPreparationError("decision-prepare-snapshot-changed")


def prepare_simulation_decision(
    root: Path,
    loop_id: str,
    request: SimulationPrepareRequest,
    *,
    dry_run: bool = True,
    expected_digest: str = "",
) -> SimulationPreparation:
    """只沿既有 Implementation 锁原子写一个有限 context。"""
    root = root.resolve(strict=True)
    validate_explicit_loop_id(loop_id)
    artifacts = implementation_artifacts(root, loop_id)
    run = LoopRun.model_validate_json(read_stable_bytes(root, artifacts.loop_run_path))
    if run.decision_capability == "stage-simulation-v1":
        from ai_sdlc.core.loop_stage_decision_service import (
            prepare_stage_simulation_decision,
        )

        return prepare_stage_simulation_decision(
            root,
            "implementation",
            loop_id,
            request,
            host_resolver=lambda: implementation_stage_host(root, loop_id=loop_id),
            dry_run=dry_run,
            expected_digest=expected_digest,
        )
    # 请求需保留原字段集合，避免把未提供的其他操作字段误作显式输入。
    request = SimulationPrepareRequest.model_validate(request)
    if type(dry_run) is not bool:
        raise DecisionPreparationError("decision-dry-run-invalid")
    if dry_run:
        return _simulation_prepare(root, loop_id, request)
    # 写请求先持锁再读取，避免同一请求的并发提交被误判为快照漂移。
    with ExitStack() as locks:
        try:
            locks.enter_context(_implementation_write_guard(root, loop_id))
        except _ImplementationWriteLockError as exc:
            raise DecisionPreparationError(str(exc)) from exc
        current = _simulation_prepare(root, loop_id, request)
        if current.status == "existing":
            return current
        if not expected_digest or current.prepare_digest != expected_digest:
            raise DecisionPreparationError("decision-prepare-digest-mismatch")
        _write_context(
            implementation_artifacts(root, loop_id).loop_dir / "decision-context.json",
            current.context,
        )
        return current.model_copy(update={"status": "prepared"})


def _simulation_prepare(
    root: Path, loop_id: str, request: SimulationPrepareRequest
) -> SimulationPreparation:
    artifacts = implementation_artifacts(root, loop_id)
    run = LoopRun.model_validate_json(read_stable_bytes(root, artifacts.loop_run_path))
    impl_input = ImplementationInput.model_validate_json(
        read_stable_bytes(root, artifacts.input_path)
    )
    _check_identity(run, impl_input, loop_id)
    if run.decision_capability != CAPABILITY:
        raise DecisionPreparationError("simulation-capability-mismatch")
    validate_implementation_lifecycle(root, impl_input)
    validate_implementation_upstream(root, impl_input)
    content = _optional_bytes(root, artifacts.loop_dir / "decision-context.json")
    context = (
        None
        if content is None
        else validate_captured_implementation_context(
            run, impl_input, parse_implementation_context(content)
        )
    )
    if context is not None and not isinstance(context, SimulationContext):
        raise DecisionPreparationError("simulation-capability-mismatch")
    sources = {s.id: s for s in (context.sources if context else ())}
    sources.update({s.id: s for s in request.sources})

    # 快照复用已有源边界，包含当前请求及所有既有来源；不复制 Git/I/O 实现。
    class SourceRequest:
        pass

    source_request = SourceRequest()
    source_request.sources = tuple(sources.values())
    before = _snapshot(root, loop_id, source_request)
    digest = _hash({"state": before, "request": request_digest(request)})
    if context is not None and has_receipt(context, request):
        return SimulationPreparation(
            status="existing", prepare_digest=digest, context=context
        )
    if context is None:
        _admit_initial_prepare(root, run, impl_input)
    if request.operation != "seal-for-review":
        _admit_initial_prepare(root, run, impl_input)
        _check_sources(root, artifacts.loop_dir, source_request)
    else:
        from ai_sdlc.core.implementation_loop import _build_report, _read_current_state
        from ai_sdlc.core.loop_review_service import outcome_path

        if context is None or context.initial_selection_id is None:
            raise DecisionPreparationError("simulation-initial-selection-missing")
        loaded = _read_current_state(
            root, artifacts, loop_id=loop_id, input_digest=run.input_digest
        )
        if (
            not isinstance(loaded, tuple)
            or _build_report(root, *loaded).status != LoopStatus.NEEDS_REVIEW
        ):
            raise DecisionPreparationError("simulation-actual-tasks-not-ready")
        if any(
            _optional_bytes(root, outcome_path(artifacts.loop_dir, n)) is not None
            for n in (1, 2)
        ):
            raise DecisionPreparationError("simulation-review-already-recorded")
    result = transition_simulation(
        context,
        request,
        loop_id=loop_id,
        input_digest=run.input_digest,
        source_digest=_hash(before["source"]),
        source_manifest=before["referenced_sources"],
        now_ms=time.time_ns() // 1_000_000,
    )
    validate_simulation_context(result)
    _require_stable_snapshot(before, _snapshot(root, loop_id, source_request))
    return SimulationPreparation(
        status="preview", prepare_digest=digest, context=result
    )


def _write_context(path: Path, context: DecisionContext | SimulationContext) -> None:
    # 只在既有 Loop 目录中原子提交；临时写失败不能退回直接写目标。
    descriptor, temporary = tempfile.mkstemp(
        prefix=".decision-context-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((context.model_dump_json(indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def implementation_stage_host(
    root: Path,
    run: LoopRun | None = None,
    impl_input: ImplementationInput | None = None,
    *,
    loop_id: str = "",
    _evaluate_actual_readiness: bool = True,
):
    """从真实任务、质量证据和原生命周期构建阶段适配；不递归读决策门禁。"""
    from ai_sdlc.core.implementation_loop import _build_report
    from ai_sdlc.core.loop_stage_decision_service import StageDecisionHost

    root = root.resolve(strict=True)
    loop_id = loop_id or (run.loop_id if run is not None else "")
    validate_explicit_loop_id(loop_id)
    artifacts = implementation_artifacts(root, loop_id)
    current_run = LoopRun.model_validate_json(
        read_stable_bytes(root, artifacts.loop_run_path)
    )
    current_input = ImplementationInput.model_validate_json(
        read_stable_bytes(root, artifacts.input_path)
    )
    if (run is not None and current_run != run) or (
        impl_input is not None and current_input != impl_input
    ):
        raise DecisionPreparationError("decision-identity-mismatch")
    run, impl_input = current_run, current_input
    _check_identity(run, impl_input, loop_id)
    if run.decision_capability != "stage-simulation-v1":
        raise DecisionPreparationError("simulation-capability-mismatch")
    validate_implementation_lifecycle(root, impl_input)
    validate_implementation_upstream(root, impl_input)
    progress = ImplementationProgress.model_validate_json(
        read_stable_bytes(root, artifacts.progress_path)
    )
    tasks = ImplementationTasks.model_validate_json(
        read_stable_bytes(root, artifacts.tasks_path)
    )
    initial_ready = True
    try:
        _admit_initial_prepare(root, run, impl_input)
    except DecisionPreparationError as exc:
        if str(exc) != "decision-prepare-already-started":
            raise
        initial_ready = False
    actual_ready = (
        _evaluate_actual_readiness
        and _build_report(root, impl_input, tasks, progress).status
        == LoopStatus.NEEDS_REVIEW
    )
    actual_paths = (
        artifacts.input_path,
        artifacts.tasks_path,
        artifacts.progress_path,
        artifacts.evidence_path,
    )
    return StageDecisionHost(
        stage_kind="implementation",
        loop_id=loop_id,
        input_digest=run.input_digest,
        loop_dir=artifacts.loop_dir,
        artifact_paths=(*actual_paths, artifacts.loop_run_path, artifacts.close_path),
        actual_paths=actual_paths,
        initial_ready=initial_ready,
        actual_ready=actual_ready,
        review_started=any(
            _optional_bytes(root, artifacts.loop_dir / f"review-outcome-round-{n}.json")
            is not None
            for n in (1, 2)
        ),
        execution_started=_implementation_progress_started(root, loop_id, progress),
    )


def _require_stage_implementation_execution(root, host, context):
    from ai_sdlc.cli.loop_review_cmd import (
        _counterexample_execution_capture,
        prepare_current_loop_review,
    )
    from ai_sdlc.core.loop_simulation_context import conditional_improvement_admission
    from ai_sdlc.core.loop_stage_decision_service import stage_material_digest

    with _counterexample_execution_capture():
        prepared, _ = prepare_current_loop_review(root, "implementation", host.loop_id)
    previous = prepared.baseline_outcome or prepared.current_outcome
    if (
        previous is None
        or previous.round_number != 1
        or previous.simulation is None
        or previous.simulation.decision.action not in {"repair", "improve"}
        or prepared.status not in {"needs_fix", "review_missing"}
    ):
        raise DecisionPreparationError("simulation-review-allows-no-execution")
    require_simulation_time_admission(context, execution_started=True)
    if previous.simulation.decision.action == "improve":
        current_digest = stage_material_digest(root, host)
        # 已进入受审的R2材料不重复收取完整改善报价；原时间窗口仍不刷新。
        if current_digest == previous.simulation.source_digest:
            reason = conditional_improvement_admission(
                context,
                source_digest=current_digest,
                now_ms=time.time_ns() // 1_000_000,
            )
            if reason is not None:
                raise DecisionPreparationError(reason)

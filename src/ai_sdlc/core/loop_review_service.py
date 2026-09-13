"""Bounded review outcomes stored directly beside existing Loop artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import ValidationError

from ai_sdlc.core.loop_decision_models import B1ReviewData
from ai_sdlc.core.loop_decision_service import (
    B1ReviewSnapshot,
    build_b1_review_data,
    validate_b1_review_data,
)
from ai_sdlc.core.loop_models import utc_now_iso
from ai_sdlc.core.loop_resource_lock import _stage_write_guard
from ai_sdlc.core.loop_review_models import (
    B1ExpertResult,
    LoopReviewOutcome,
    ReviewStatusOverlay,
)
from ai_sdlc.core.loop_stage_decision_service import (
    StageReviewData,
    StageReviewSnapshot,
)
from ai_sdlc.core.review_kernel import (
    LoopReviewType,
    ReviewExecution,
    ReviewInput,
    ReviewInputValidator,
    merge_expert_findings,
)
from ai_sdlc.core.stable_file_read import (
    _stable_regular_file_exists,
    read_stable_bytes,
    read_stable_text,
)

ReviewInputResolver = Callable[[int], ReviewInput]
B1SnapshotResolver = Callable[[int], B1ReviewSnapshot | None]
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETIRED_IMPLEMENTATION_NEXT = (
    "Preserve all historical artifacts unchanged. "
    "This retired instance cannot be resumed or treated as a normal review."
)
_FAILED_REVIEW_NEXT = (
    "Inspect the execution failure and preserve the receipt and known findings. "
    "Retry only after a specific technical cause is resolved, on unchanged input "
    "and within the existing retry limit. Do not retry policy refusals or bypass "
    "them with another role/provider. Missing review is not a quality verdict."
)


class LoopReviewServiceError(ValueError):
    """A bounded review transition cannot proceed."""

    def __init__(
        self,
        reason: str,
        *,
        detail: str = "",
        expected_digest: str = "",
        actual_digest: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": "blocked", "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        if self.expected_digest:
            payload["expected_digest"] = self.expected_digest
        if self.actual_digest:
            payload["actual_digest"] = self.actual_digest
        return payload


@dataclass(frozen=True)
class LoopReviewPreparation:
    """Current input plus derived review display state."""

    review_input: ReviewInput
    overlay: ReviewStatusOverlay
    current_outcome: LoopReviewOutcome | None
    outcome_path: Path
    b1_snapshot: B1ReviewSnapshot | None = None
    baseline_outcome: LoopReviewOutcome | None = None

    @property
    def status(self) -> str:
        return self.overlay.status

    @property
    def reason(self) -> str:
        return self.overlay.reason

    @property
    def next_action(self) -> str:
        return self.overlay.next_action


@dataclass(frozen=True)
class RecordLoopReviewOptions:
    """Inputs required to record one bounded review outcome."""

    root: Path
    loop_type: LoopReviewType
    loop_id: str
    expected_digest: str
    result_paths: tuple[Path, ...]


def outcome_path(loop_dir: Path, round_number: int) -> Path:
    """Return one of the only two allowed outcome paths."""

    if round_number not in {1, 2}:
        raise LoopReviewServiceError("review-round-limit")
    return loop_dir / f"review-outcome-round-{round_number}.json"


def reject_retired_implementation_continuation(root: Path, loop_id: str) -> None:
    """旧续办仅作历史保留；不能在删除执行能力后悄悄退回普通 R1/R2。"""
    if _SAFE_IDENTIFIER.fullmatch(loop_id) is None:
        raise LoopReviewServiceError("review-loop-identity-invalid")
    directory = root / ".ai-sdlc/loops/implementation" / loop_id
    try:
        names = {path.name for path in directory.iterdir()}
    except FileNotFoundError:
        return
    # 不用 glob 的空结果表示“无历史”：目录不可读时必须保留失败而非降级。
    later_outcomes = (
        name
        for name in names
        if name.startswith("review-outcome-round-")
        and name.endswith(".json")
        and name not in {"review-outcome-round-1.json", "review-outcome-round-2.json"}
    )
    retired = "review-continuation.json" in names or any(later_outcomes)
    if not retired and "implementation-close.json" in names:
        # 归档可能只剩 Close；只读旧绑定标记，不把普通凭据提前按新模型验收。
        try:
            close_path = directory / "implementation-close.json"
            if not _stable_regular_file_exists(root, close_path):
                raise ValueError("Implementation Close disappeared during inspection.")
            close = json.loads(read_stable_bytes(root, close_path))
            if not isinstance(close, dict):
                raise ValueError("Implementation Close must be a JSON object.")
        except (OSError, ValueError) as exc:
            raise LoopReviewServiceError(
                "implementation-close-invalid", detail=str(exc)
            ) from exc
        retired = close.get("review_binding") is not None
    if retired:
        raise LoopReviewServiceError(
            "implementation-continuation-retired",
            detail=RETIRED_IMPLEMENTATION_NEXT,
        )


def read_verified_implementation_close(
    root: Path,
    loop_id: str,
    *,
    review_input_validator: ReviewInputValidator | None = None,
):
    """读取可消费的关闭凭据；量化结果仍须绑定当前完整审查输入。"""
    from ai_sdlc.core.implementation_models import (
        ImplementationClose,
        ImplementationReport,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts
    from ai_sdlc.core.loop_models import LoopRun, LoopStatus

    if not _SAFE_IDENTIFIER.fullmatch(loop_id):
        raise LoopReviewServiceError("closed-loop-identity-mismatch")
    reject_retired_implementation_continuation(root, loop_id)
    artifacts = implementation_artifacts(root, loop_id)
    paths = (artifacts.close_path, artifacts.loop_run_path, artifacts.report_json_path)
    captured = {path: read_stable_bytes(root, path) for path in paths}
    close = ImplementationClose.model_validate_json(captured[artifacts.close_path])
    run = LoopRun.model_validate_json(captured[artifacts.loop_run_path])
    report = ImplementationReport.model_validate_json(
        captured[artifacts.report_json_path]
    )
    context_path = artifacts.loop_run_path.parent / "decision-context.json"
    if _stable_regular_file_exists(root, context_path):
        context_bytes = read_stable_bytes(root, context_path)
        try:
            context_identity = json.loads(context_bytes)
        except (ValueError, UnicodeError):
            context_identity = None
        # D1 标记由既有 context 交叉确认，不能只降级 run 便绕过闭后复验。
        if isinstance(context_identity, dict) and context_identity.get(
            "capability"
        ) in {"implementation-simulation-v1", "stage-simulation-v1"}:
            if run.decision_capability != context_identity["capability"]:
                raise LoopReviewServiceError("decision-identity-mismatch")
            captured[context_path] = context_bytes
    expected_next = (
        "frontend-evidence" if report.requires_frontend_evidence else "local-pr-review"
    )
    if (
        close.loop_id != loop_id
        or run.loop_id != loop_id
        or report.loop_id != loop_id
        or run.loop_type != "implementation"
        or run.status != LoopStatus.CLOSED
        or report.work_item_id != run.work_item_id
    ):
        raise LoopReviewServiceError("closed-loop-identity-mismatch")
    # 保留旧凭据字段的只读解析，但已退休的续办凭据不能变成普通关闭凭据。
    if close.review_binding is not None:
        raise LoopReviewServiceError("implementation-continuation-retired")
    if run.decision_capability in {
        "implementation-simulation-v1",
        "stage-simulation-v1",
    }:
        if (
            close.report_path != artifacts.report_json_path.relative_to(root).as_posix()
            or close.required_task_count != report.required_task_count
            or close.next_loop_type != expected_next
        ):
            raise LoopReviewServiceError("closed-loop-identity-mismatch")
        _verify_simulation_close_review(root, loop_id, review_input_validator)
    if any(
        read_stable_bytes(root, path) != content for path, content in captured.items()
    ):
        raise LoopReviewServiceError("closed-loop-receipt-drift")
    return close


def _verify_simulation_close_review(
    root: Path, loop_id: str, validator: ReviewInputValidator | None
) -> None:
    """关闭凭据不替代 D1 当前正式评审；只读复验不会回调 Close。"""
    if validator is None:
        raise LoopReviewServiceError("simulation-close-review-validator-required")
    loop_dir = root / ".ai-sdlc/loops/implementation" / loop_id

    def capture_outcomes() -> dict[Path, bytes | None]:
        return {
            path: read_stable_bytes(root, path)
            if _stable_regular_file_exists(root, path)
            else None
            for path in (outcome_path(loop_dir, 1), outcome_path(loop_dir, 2))
        }

    originals = capture_outcomes()
    if originals[outcome_path(loop_dir, 1)] is None:
        raise LoopReviewServiceError("review-result-missing")
    outcomes = [
        LoopReviewOutcome.model_validate_json(content)
        for content in originals.values()
        if content is not None
    ]
    for number, outcome in enumerate(outcomes, 1):
        if (
            outcome.loop_id != loop_id
            or outcome.loop_type != "implementation"
            or outcome.round_number != number
            or outcome.simulation is None
        ):
            raise LoopReviewServiceError("decision-assessment-invalid")
    final = outcomes[-1]
    actual = final.simulation
    action = actual.decision.action if actual is not None else None
    if isinstance(actual, StageReviewData) and action == "improve":
        from ai_sdlc.core.loop_simulation_context import SimulationContext

        context = SimulationContext.model_validate_json(
            read_stable_bytes(root, loop_dir / "decision-context.json")
        )
        probe = StageReviewSnapshot(
            ReviewInput(
                loop_id=loop_id,
                loop_type="implementation",
                round_number=1,
                input_digest=final.input_digest,
                artifact_paths=list(actual.manifest),
                expert_roles=final.expert_roles,
                expert_reasons={role: "原实际评审" for role in final.expert_roles},
            ),
            context,
            actual.manifest,
            actual.source_digest,
            time.time_ns() // 1_000_000,
        )
        _validate_saved_b1(probe, final)
        action = effective_actual_action(probe, final)
    if final.status != "completed" or actual is None or action != "stop":
        raise LoopReviewServiceError("decision-not-qualified")
    reviewed = validator(
        root,
        loop_type="implementation",
        loop_id=loop_id,
        expected_digest=final.input_digest,
    )
    if (
        not isinstance(reviewed, ReviewInput)
        or reviewed.input_digest != final.input_digest
    ):
        raise LoopReviewServiceError("review-input-unavailable")
    if (
        validator(
            root,
            loop_type="implementation",
            loop_id=loop_id,
            expected_digest=final.input_digest,
        )
        != reviewed
        or capture_outcomes() != originals
    ):
        raise LoopReviewServiceError("review-input-drift")


def prepare_loop_review(
    root: Path,
    *,
    loop_type: LoopReviewType,
    loop_id: str,
    loop_dir: Path,
    input_resolver: ReviewInputResolver,
    b1_snapshot_resolver: B1SnapshotResolver | None = None,
) -> LoopReviewPreparation:
    """Derive the current review round without writing Loop state."""

    _validate_identity(root, loop_type, loop_id, loop_dir)
    if loop_type == "implementation":
        reject_retired_implementation_continuation(root, loop_id)
    first_path = outcome_path(loop_dir, 1)
    second_path = outcome_path(loop_dir, 2)
    first = _read_outcome(root, first_path, loop_type, loop_id, 1)
    second = _read_outcome(root, second_path, loop_type, loop_id, 2)
    if second is not None and first is None:
        raise LoopReviewServiceError("review-outcome-sequence-invalid")

    first_input = input_resolver(1)
    _require_input_identity(first_input, loop_type, loop_id, 1)
    first_snapshot = _snapshot_for_review(
        root, loop_dir, first_input, b1_snapshot_resolver
    )
    _validate_saved_b1(first_snapshot, first)
    if first is None:
        return _preparation(
            first_input,
            None,
            first_path,
            status="review_missing",
            reason="review-result-missing",
            next_action="Run the selected independent experts and record round 1.",
            b1_snapshot=first_snapshot,
        )

    if first.status == "failed":
        if second is not None:
            raise LoopReviewServiceError("review-outcome-sequence-invalid")
        if first.input_digest != first_input.input_digest:
            return _drifted_preparation(first_input, first, first_path)
        if first.infra_retry_count == 1:
            return _retry_exhausted(first_input, first, first_path)
        return _preparation(
            first_input,
            first,
            first_path,
            status="failed",
            reason="review-execution-failed",
            next_action=_FAILED_REVIEW_NEXT,
            b1_snapshot=first_snapshot,
        )

    first_actual = _actual_review_data(first)
    if first_actual is not None and first_actual.decision.action == "blocked":
        if second is not None:
            raise LoopReviewServiceError("review-outcome-sequence-invalid")
        return _preparation(
            first_input,
            first,
            first_path,
            status="needs_user",
            reason=first_actual.decision.reason,
            next_action="The required repair is not available; do not start round 2.",
            b1_snapshot=first_snapshot,
        )
    first_actionable = (
        effective_actual_action(first_snapshot, first) in {"repair", "improve"}
        if first_actual is not None
        else has_actionable_findings(first)
    )
    if not first_actionable:
        if first.input_digest != first_input.input_digest:
            return _drifted_preparation(first_input, first, first_path)
        if second is not None:
            raise LoopReviewServiceError("review-outcome-sequence-invalid")
        return _preparation(
            first_input,
            first,
            first_path,
            status="passed",
            reason="review-passed",
            next_action="Close the unchanged Loop result.",
            b1_snapshot=first_snapshot,
        )

    if first.input_digest == first_input.input_digest:
        if second is not None:
            raise LoopReviewServiceError("review-outcome-sequence-invalid")
        return _preparation(
            first_input,
            first,
            first_path,
            status="needs_fix",
            reason=first_actual.decision.reason
            if first_actual
            else "review-findings-actionable",
            next_action=(
                "Apply the sealed conditional improvement within its original plan, then run round 2."
                if first_actual and first_actual.decision.action == "improve"
                else "Resolve the required gaps and actionable findings before round 2."
                if first_actual
                else "Resolve the actionable findings before round 2."
            ),
            b1_snapshot=first_snapshot,
        )

    second_input = input_resolver(2)
    _require_input_identity(second_input, loop_type, loop_id, 2)
    second_snapshot = _snapshot_for_review(
        root, loop_dir, second_input, b1_snapshot_resolver
    )
    if (first_snapshot is None) != (second_snapshot is None):
        raise LoopReviewServiceError("decision-identity-mismatch")
    _validate_saved_b1(second_snapshot, second, baseline=first)
    if second is None:
        return _preparation(
            second_input,
            None,
            second_path,
            status="review_missing",
            reason="review-result-missing",
            next_action="Run the selected independent experts and record round 2.",
            b1_snapshot=second_snapshot,
            baseline_outcome=first,
        )
    if second.input_digest != second_input.input_digest:
        return _drifted_preparation(second_input, second, second_path)
    if second.status == "failed":
        if second.infra_retry_count == 1:
            return _retry_exhausted(second_input, second, second_path)
        return _preparation(
            second_input,
            second,
            second_path,
            status="failed",
            reason="review-execution-failed",
            next_action=_FAILED_REVIEW_NEXT,
            b1_snapshot=second_snapshot,
            baseline_outcome=first,
        )
    second_actual = _actual_review_data(second)
    if (
        second_actual.decision.action != "stop"
        if second_actual is not None
        else has_actionable_findings(second)
    ):
        return _preparation(
            second_input,
            second,
            second_path,
            status="needs_user",
            reason="review-round-limit",
            next_action="Inspect the unresolved findings; a third review round is forbidden.",
            b1_snapshot=second_snapshot,
            baseline_outcome=first,
        )
    return _preparation(
        second_input,
        second,
        second_path,
        status="passed",
        reason="review-passed",
        next_action="Close the unchanged Loop result.",
        b1_snapshot=second_snapshot,
        baseline_outcome=first,
    )


def record_loop_review(
    options: RecordLoopReviewOptions,
    *,
    loop_dir: Path,
    input_resolver: ReviewInputResolver,
    b1_snapshot_resolver: B1SnapshotResolver | None = None,
) -> ReviewStatusOverlay:
    """Validate independent result files and record exactly one current outcome."""

    _validate_identity(options.root, options.loop_type, options.loop_id, loop_dir)
    # 与 prepare/progress/verify/Close 共用资源锁，内部 outcome 锁始终后取。
    # Local PR 的资源身份是 review_id，而实际专家的 loop_id 保持原 Loop 绑定。
    resource_id = (
        loop_dir.name if options.loop_type == "local-pr-review" else options.loop_id
    )
    run_name = (
        "review-run.json" if options.loop_type == "local-pr-review" else "loop-run.json"
    )
    run_path = loop_dir / run_name
    run_data = (
        json.loads(read_stable_text(options.root, run_path))
        if run_path.is_file()
        else {}
    )
    # Implementation 的 legacy 续办也需保护归档与写回，不能依赖锁外的 grant 存在性。
    # 其他 legacy 阶段沿用原 outcome 锁及并发错误语义。
    quantified = (
        isinstance(run_data, dict)
        and run_data.get("decision_mode") == "adaptive-quantified"
    )
    guard = (
        _stage_write_guard(options.root, options.loop_type, resource_id)
        if quantified or options.loop_type == "implementation"
        else nullcontext()
    )
    with guard:
        return _record_loop_review_locked(
            options,
            loop_dir=loop_dir,
            input_resolver=input_resolver,
            b1_snapshot_resolver=b1_snapshot_resolver,
        )


def _record_loop_review_locked(
    options: RecordLoopReviewOptions,
    *,
    loop_dir: Path,
    input_resolver: ReviewInputResolver,
    b1_snapshot_resolver: B1SnapshotResolver | None,
) -> ReviewStatusOverlay:

    prepared = prepare_loop_review(
        options.root,
        loop_type=options.loop_type,
        loop_id=options.loop_id,
        loop_dir=loop_dir,
        input_resolver=input_resolver,
        b1_snapshot_resolver=b1_snapshot_resolver,
    )
    expected = options.expected_digest.strip().lower()
    if expected != prepared.review_input.input_digest:
        raise LoopReviewServiceError(
            "review-input-drift",
            expected_digest=expected,
            actual_digest=prepared.review_input.input_digest,
        )
    if prepared.status not in {"review_missing", "failed"}:
        if prepared.reason in {"review-expert-retry-limit", "repair-unavailable"}:
            raise LoopReviewServiceError(prepared.reason)
        if prepared.reason == "review-input-drift":
            raise LoopReviewServiceError("review-input-drift")
        if prepared.review_input.round_number == 2:
            raise LoopReviewServiceError("review-round-limit")
        if prepared.status == "needs_fix":
            raise LoopReviewServiceError("review-input-unchanged")
        raise LoopReviewServiceError("review-already-completed")

    b1_results = (
        _read_b1_results(options.root, options.result_paths)
        if prepared.b1_snapshot is not None
        else []
    )
    executions = (
        [item.execution for item in b1_results]
        if b1_results
        else _read_executions(options.root, options.result_paths)
    )
    roles = [execution.roles[0] for execution in executions]
    if len(roles) != len(set(roles)) or set(roles) != set(
        prepared.review_input.expert_roles
    ):
        raise LoopReviewServiceError("expert-role-mismatch")
    for execution in executions:
        role = execution.roles[0]
        if execution.role_reasons[role] != prepared.review_input.expert_reasons[role]:
            raise LoopReviewServiceError("expert-role-mismatch")

    merged = merge_expert_findings(executions)
    b1 = None
    simulation = None
    retry_count = None
    if prepared.b1_snapshot is not None:
        previous = prepared.current_outcome
        retry_count = 0 if previous is None else (previous.infra_retry_count or 0) + 1
        if retry_count > 1:
            raise LoopReviewServiceError("review-expert-retry-limit")
        if merged.status == "completed":
            try:
                actual = build_b1_review_data(
                    prepared.b1_snapshot,
                    {item.execution.roles[0]: item.assessment for item in b1_results},
                    has_actionable_findings=any(
                        finding.severity in {"blocker", "important"}
                        for finding in merged.findings
                    ),
                    baseline=_actual_review_data(prepared.baseline_outcome),
                )
                if prepared.b1_snapshot.context.capability == "implementation-b1":
                    b1 = actual
                elif prepared.b1_snapshot.context.capability in {
                    "implementation-simulation-v1",
                    "stage-simulation-v1",
                }:
                    simulation = actual
                else:
                    raise ValueError("decision-identity-mismatch")
            except ValueError as exc:
                raise LoopReviewServiceError(
                    "decision-assessment-invalid", detail=str(exc)
                ) from exc
    outcome = LoopReviewOutcome(
        loop_id=options.loop_id,
        loop_type=options.loop_type,
        round_number=prepared.review_input.round_number,
        input_digest=prepared.review_input.input_digest,
        status=merged.status,
        expert_roles=prepared.review_input.expert_roles,
        findings=merged.findings,
        completed_expert_results=(
            [execution for execution in executions if execution.status == "completed"]
            if merged.status == "failed"
            else []
        ),
        failure_kind=merged.failure_kind,
        failure_reason=merged.failure_reason,
        recorded_at=utc_now_iso(),
        b1=b1,
        simulation=simulation,
        infra_retry_count=retry_count,
    )

    def revalidate():
        nonlocal outcome
        fresh = prepare_loop_review(
            options.root,
            loop_type=options.loop_type,
            loop_id=options.loop_id,
            loop_dir=loop_dir,
            input_resolver=input_resolver,
            b1_snapshot_resolver=b1_snapshot_resolver,
        )
        if (
            fresh.review_input != prepared.review_input
            or fresh.current_outcome != prepared.current_outcome
            or fresh.baseline_outcome != prepared.baseline_outcome
            or fresh.b1_snapshot != prepared.b1_snapshot
        ):
            raise LoopReviewServiceError("review-input-drift")
        if (
            isinstance(fresh.b1_snapshot, StageReviewSnapshot)
            and outcome.status == "completed"
        ):
            actual = build_b1_review_data(
                replace(fresh.b1_snapshot, observed_at_ms=time.time_ns() // 1_000_000),
                {item.execution.roles[0]: item.assessment for item in b1_results},
                has_actionable_findings=has_actionable_findings(outcome),
                baseline=_actual_review_data(fresh.baseline_outcome),
            )
            outcome = outcome.model_copy(update={"simulation": actual})
        return outcome

    # 模型在锁外完成；提交前仍须复验同一量化候选，不能靠持锁时长代替输入绑定。
    if prepared.b1_snapshot is not None:
        revalidate()
    if prepared.b1_snapshot is not None:
        _write_outcome(
            options.root, prepared.outcome_path, outcome, precommit=revalidate
        )
    else:
        _write_outcome(options.root, prepared.outcome_path, outcome)
    return _overlay_for_outcome(outcome)


def validate_prepared_outcome_for_close(
    prepared: LoopReviewPreparation,
    *,
    expected_digest: str,
) -> ReviewInput:
    """Require a current clean/advisory completed outcome before Close."""

    expected = expected_digest.strip().lower()
    if expected != prepared.review_input.input_digest:
        raise LoopReviewServiceError(
            "review-input-drift",
            expected_digest=expected,
            actual_digest=prepared.review_input.input_digest,
        )
    if prepared.current_outcome is None:
        raise LoopReviewServiceError("review-result-missing")
    if prepared.current_outcome.input_digest != prepared.review_input.input_digest:
        raise LoopReviewServiceError("review-input-drift")
    if prepared.current_outcome.expert_roles != prepared.review_input.expert_roles:
        raise LoopReviewServiceError("expert-role-mismatch")
    if prepared.status != "passed":
        raise LoopReviewServiceError(prepared.reason)
    _validate_saved_b1(
        prepared.b1_snapshot,
        prepared.current_outcome,
        baseline=prepared.baseline_outcome,
    )
    actual = _actual_review_data(prepared.current_outcome)
    if (
        actual is not None
        and effective_actual_action(prepared.b1_snapshot, prepared.current_outcome)
        != "stop"
    ):
        raise LoopReviewServiceError("decision-not-qualified")
    return prepared.review_input


def has_actionable_findings(outcome: LoopReviewOutcome) -> bool:
    """Return whether an outcome contains a blocker or important finding."""

    return any(
        finding.severity in {"blocker", "important"} for finding in outcome.findings
    )


def _read_executions(root: Path, paths: tuple[Path, ...]) -> list[ReviewExecution]:
    if not paths or len(paths) > 2:
        raise LoopReviewServiceError("expert-role-mismatch")
    executions: list[ReviewExecution] = []
    for path in paths:
        try:
            execution = ReviewExecution.model_validate_json(
                read_stable_text(root, path, encoding="utf-8")
            )
        except (OSError, UnicodeError, ValidationError) as exc:
            raise LoopReviewServiceError(
                "review-result-invalid", detail=str(exc)
            ) from exc
        if len(execution.roles) != 1:
            raise LoopReviewServiceError("expert-role-mismatch")
        executions.append(execution)
    return executions


def _read_b1_results(root: Path, paths: tuple[Path, ...]) -> list[B1ExpertResult]:
    if not paths or len(paths) > 2:
        raise LoopReviewServiceError("expert-role-mismatch")
    try:
        return [
            B1ExpertResult.model_validate_json(read_stable_text(root, path))
            for path in paths
        ]
    except (OSError, UnicodeError, ValueError) as exc:
        raise LoopReviewServiceError(
            "decision-assessment-invalid", detail=str(exc)
        ) from exc


def _snapshot_for_review(
    root: Path,
    loop_dir: Path,
    review_input: ReviewInput,
    resolver: B1SnapshotResolver | None,
) -> B1ReviewSnapshot | None:
    identities = []
    inputs = {
        "requirement": "requirement-intake.json",
        "design-contract": "design-contract-input.json",
        "implementation": "implementation-input.json",
        "frontend-evidence": "frontend-evidence-input.json",
    }
    names = (
        ("review-run.json",)
        if review_input.loop_type == "local-pr-review"
        else ("loop-run.json", inputs[review_input.loop_type])
    )
    for name in names:
        path = loop_dir / name
        opaque_legacy_input = (
            name in inputs.values()
            and review_input.loop_type != "implementation"
            and identities == [("legacy", None)]
            and not (loop_dir / "decision-context.json").exists()
        )
        try:
            payload = json.loads(read_stable_text(root, path)) if path.is_file() else {}
        except json.JSONDecodeError as exc:
            if not opaque_legacy_input:
                raise LoopReviewServiceError("decision-identity-mismatch") from exc
            payload = {}
        if not isinstance(payload, dict):
            if not opaque_legacy_input:
                raise LoopReviewServiceError("decision-identity-mismatch")
            # 旧阶段以原文参与评审；只有量化身份才要求输入提供结构化双重身份。
            payload = {}
        mode, capability = (
            payload.get("decision_mode", "legacy"),
            payload.get("decision_capability"),
        )
        if not isinstance(mode, str) or (
            capability is not None and not isinstance(capability, str)
        ):
            raise LoopReviewServiceError("decision-identity-mismatch")
        identities.append((mode, capability))
    if len(set(identities)) != 1:
        raise LoopReviewServiceError("decision-identity-mismatch")
    supported = {
        ("legacy", None),
        ("adaptive-quantified", "stage-simulation-v1"),
    }
    if review_input.loop_type == "implementation":
        supported.update(
            {
                ("adaptive-quantified", "implementation-b1"),
                ("adaptive-quantified", "implementation-simulation-v1"),
            }
        )
    if identities[0] not in supported:
        raise LoopReviewServiceError("decision-identity-mismatch")
    enabled = identities[0][0] == "adaptive-quantified"
    if not enabled and (loop_dir / "decision-context.json").exists():
        raise LoopReviewServiceError("decision-context-conflicts-with-legacy")
    snapshot = resolver(review_input.round_number) if resolver is not None else None
    if enabled != (snapshot is not None):
        raise LoopReviewServiceError("decision-review-snapshot-unavailable")
    if snapshot is not None and (
        snapshot.review_input != review_input
        or snapshot.context.loop_id != review_input.loop_id
        or snapshot.context.capability != identities[0][1]
    ):
        raise LoopReviewServiceError("review-input-drift")
    if snapshot is not None and snapshot.context.capability == "stage-simulation-v1":
        if (
            not isinstance(snapshot, StageReviewSnapshot)
            or snapshot.context.loop_type != review_input.loop_type
        ):
            raise LoopReviewServiceError("decision-review-snapshot-unavailable")
        if review_input.loop_type == "local-pr-review":
            from ai_sdlc.core.pr_review_decision import (
                validate_captured_pr_review_context,
            )
            from ai_sdlc.core.pr_review_models import ReviewRun

            validate_captured_pr_review_context(
                ReviewRun.model_validate_json(
                    read_stable_bytes(root, loop_dir / "review-run.json")
                ),
                snapshot.context,
            )
        else:
            from ai_sdlc.core.loop_models import LoopRun
            from ai_sdlc.core.loop_stage_decision_service import (
                validate_stage_start_binding,
            )

            validate_stage_start_binding(
                LoopRun.model_validate_json(
                    read_stable_bytes(root, loop_dir / "loop-run.json")
                ),
                snapshot.context,
            )
    return snapshot


def _actual_review_data(outcome: LoopReviewOutcome | None) -> B1ReviewData | None:
    """共用实际 H/Q，不把模拟候选 S 写入正式评审判定。"""
    if outcome is None:
        return None
    return outcome.b1 if outcome.b1 is not None else outcome.simulation


def effective_actual_action(snapshot, outcome: LoopReviewOutcome) -> str | None:
    """只读收缩过期改善；原件不变，已改工件或实际缺口不能收缩成 Close。"""
    actual = _actual_review_data(outcome)
    if actual is None:
        return None
    if (
        isinstance(actual, StageReviewData)
        and isinstance(snapshot, StageReviewSnapshot)
        and outcome.round_number == 1
        and actual.decision.action == "improve"
        and actual.evaluation.h == 0
        and not has_actionable_findings(outcome)
        and snapshot.review_input.input_digest == outcome.input_digest
        and snapshot.source_digest == actual.source_digest
    ):
        from ai_sdlc.core.loop_simulation_context import (
            conditional_improvement_admission,
        )

        reason = conditional_improvement_admission(
            snapshot.context,
            source_digest=snapshot.source_digest,
            now_ms=snapshot.observed_at_ms,
        )
        if reason == "model_plan_not_feasible":
            return "stop"
    return actual.decision.action


def _validate_saved_b1(
    snapshot: B1ReviewSnapshot | None,
    outcome: LoopReviewOutcome | None,
    *,
    baseline: LoopReviewOutcome | None = None,
) -> None:
    if outcome is None:
        return
    if snapshot is None:
        if (
            _actual_review_data(outcome) is not None
            or outcome.infra_retry_count is not None
        ):
            raise LoopReviewServiceError("decision-identity-mismatch")
        return
    if outcome.infra_retry_count is None:
        raise LoopReviewServiceError("decision-assessment-missing")
    if outcome.status == "failed":
        return
    capability = snapshot.context.capability
    if capability == "implementation-b1":
        data = outcome.b1
    elif capability in {"implementation-simulation-v1", "stage-simulation-v1"}:
        data = outcome.simulation
    else:
        raise LoopReviewServiceError("decision-identity-mismatch")
    if (
        data is None
        or data.input_digest != outcome.input_digest
        or set(data.assessments) != set(outcome.expert_roles)
    ):
        raise LoopReviewServiceError("decision-assessment-invalid")
    # R1 必要修复后的旧材料只按已保存原件复算，不能拿当前 R2 文件验旧 SHA。
    current = snapshot.review_input.input_digest == outcome.input_digest
    checked = (
        snapshot
        if current
        else B1ReviewSnapshot(
            review_input=ReviewInput(
                loop_id=outcome.loop_id,
                loop_type=outcome.loop_type,
                round_number=outcome.round_number,
                input_digest=outcome.input_digest,
                artifact_paths=list(data.manifest),
                expert_roles=outcome.expert_roles,
                expert_reasons={
                    role: "Persisted completed review role."
                    for role in outcome.expert_roles
                },
            ),
            context=snapshot.context,
            manifest=data.manifest,
        )
    )
    if not current and isinstance(data, StageReviewData):
        checked = StageReviewSnapshot(
            checked.review_input,
            checked.context,
            checked.manifest,
            source_digest=data.source_digest,
            observed_at_ms=data.observed_at_ms,
            context_path=getattr(snapshot, "context_path", ""),
        )
    if checked.review_input.round_number != outcome.round_number:
        raise LoopReviewServiceError("review-outcome-sequence-invalid")
    try:
        validate_b1_review_data(
            checked,
            data,
            has_actionable_findings=has_actionable_findings(outcome),
            baseline=_actual_review_data(baseline),
        )
    except ValueError as exc:
        raise LoopReviewServiceError(
            "decision-assessment-invalid", detail=str(exc)
        ) from exc


def _retry_exhausted(
    review_input: ReviewInput, outcome: LoopReviewOutcome, path: Path
) -> LoopReviewPreparation:
    return _preparation(
        review_input,
        outcome,
        path,
        status="needs_user",
        reason="review-expert-retry-limit",
        next_action="The same-input infrastructure retry is exhausted; preserve this Loop.",
    )


def _read_outcome(
    root: Path,
    path: Path,
    loop_type: LoopReviewType,
    loop_id: str,
    round_number: int,
) -> LoopReviewOutcome | None:
    if not path.exists():
        return None
    try:
        outcome = LoopReviewOutcome.model_validate_json(
            read_stable_text(root, path, encoding="utf-8")
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise LoopReviewServiceError("review-outcome-invalid", detail=str(exc)) from exc
    if (
        outcome.loop_type != loop_type
        or outcome.loop_id != loop_id
        or outcome.round_number != round_number
    ):
        raise LoopReviewServiceError("review-outcome-identity-mismatch")
    return outcome


def _write_outcome(
    root: Path,
    path: Path,
    outcome: LoopReviewOutcome,
    *,
    precommit: Callable[[], LoopReviewOutcome | None] | None = None,
) -> None:
    if outcome.continuation_digest is not None:
        raise LoopReviewServiceError("implementation-continuation-retired")
    encoded = _encoded_outcome(outcome)
    with _outcome_write_guard(root, path):
        if outcome.loop_type == "implementation":
            reject_retired_implementation_continuation(root, outcome.loop_id)
        existing = _read_outcome(
            root,
            path,
            outcome.loop_type,
            outcome.loop_id,
            outcome.round_number,
        )
        if existing is not None:
            if existing.status == "completed":
                raise LoopReviewServiceError(
                    "review-round-limit"
                    if outcome.round_number == 2
                    else "review-already-completed"
                )
            if existing.input_digest != outcome.input_digest:
                raise LoopReviewServiceError("review-input-drift")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            latest = _read_outcome(
                root,
                path,
                outcome.loop_type,
                outcome.loop_id,
                outcome.round_number,
            )
            if latest is not None:
                if latest.status == "completed":
                    raise LoopReviewServiceError(
                        "review-round-limit"
                        if outcome.round_number == 2
                        else "review-already-completed"
                    )
                if latest.input_digest != outcome.input_digest:
                    raise LoopReviewServiceError("review-input-drift")
            if precommit is not None:
                refreshed = precommit()
                if refreshed is not None:
                    # 末次观察只允许同一真实成果的成本收缩，不创建另一轮或另写 context。
                    encoded = _encoded_outcome(refreshed)
                    with temporary.open("wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
            if outcome.loop_type == "implementation":
                reject_retired_implementation_continuation(root, outcome.loop_id)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _encoded_outcome(outcome):
    return (
        json.dumps(
            outcome.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


@contextmanager
def _outcome_write_guard(root: Path, path: Path) -> Iterator[None]:
    """Serialize one outcome path without creating Loop-visible state."""

    try:
        lock_dir = _review_lock_dir(root)
        lock_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_key = f"{root.resolve()}\0{path.resolve(strict=False)}".encode()
        lock_name = hashlib.sha256(lock_key).hexdigest()
        lock_path = lock_dir / f"review-outcome-{lock_name}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        file_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LoopReviewServiceError(
            "review-outcome-lock-unavailable",
            detail=str(exc),
        ) from exc

    try:
        _acquire_review_file_lock(file_descriptor)
    except OSError as exc:
        os.close(file_descriptor)
        raise LoopReviewServiceError(
            "review-outcome-lock-unavailable",
            detail=str(exc),
        ) from exc
    try:
        yield
    finally:
        _release_review_file_lock(file_descriptor)
        os.close(file_descriptor)


def _review_lock_dir(root: Path) -> Path:
    git_marker = root / ".git"
    if git_marker.is_dir():
        return git_marker / "ai-sdlc-locks"
    if git_marker.is_file():
        try:
            marker = git_marker.read_text(encoding="utf-8").strip()
        except OSError:
            marker = ""
        if marker.lower().startswith("gitdir:"):
            value = marker.split(":", 1)[1].strip()
            git_dir = Path(value)
            if not git_dir.is_absolute():
                git_dir = root / git_dir
            return git_dir.resolve() / "ai-sdlc-locks"
    if hasattr(os, "getuid"):
        user_key = str(os.getuid())
    else:  # pragma: no cover - Windows temp directories are already user-scoped
        user_key = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"ai-sdlc-loop-locks-{user_key}"


def _acquire_review_file_lock(file_descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows CI exercises this branch
        import msvcrt

        if os.fstat(file_descriptor).st_size == 0:
            os.write(file_descriptor, b"\0")
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt.__dict__["locking"](
            file_descriptor,
            msvcrt.__dict__["LK_LOCK"],
            1,
        )
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_EX)


def _release_review_file_lock(file_descriptor: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows CI exercises this branch
        import msvcrt

        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt.__dict__["locking"](
            file_descriptor,
            msvcrt.__dict__["LK_UNLCK"],
            1,
        )
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_UN)


def _validate_identity(
    root: Path,
    loop_type: LoopReviewType,
    loop_id: str,
    loop_dir: Path,
) -> None:
    if _SAFE_IDENTIFIER.fullmatch(loop_id) is None:
        raise LoopReviewServiceError("review-loop-identity-invalid")
    resolved_root = root.resolve(strict=True)
    resolved_loop_dir = loop_dir.resolve(strict=True)
    try:
        resolved_loop_dir.relative_to(resolved_root)
    except ValueError as exc:
        raise LoopReviewServiceError("review-loop-directory-invalid") from exc
    if loop_type != "local-pr-review":
        expected = resolved_root / ".ai-sdlc" / "loops" / loop_type / loop_id
        if resolved_loop_dir != expected:
            raise LoopReviewServiceError("review-loop-directory-invalid")


def _require_input_identity(
    review_input: ReviewInput,
    loop_type: LoopReviewType,
    loop_id: str,
    round_number: int,
) -> None:
    if (
        review_input.loop_type != loop_type
        or review_input.loop_id != loop_id
        or review_input.round_number != round_number
    ):
        raise LoopReviewServiceError("review-input-identity-mismatch")


def _preparation(
    review_input: ReviewInput,
    outcome: LoopReviewOutcome | None,
    path: Path,
    *,
    status: str,
    reason: str,
    next_action: str,
    b1_snapshot: B1ReviewSnapshot | None = None,
    baseline_outcome: LoopReviewOutcome | None = None,
) -> LoopReviewPreparation:
    return LoopReviewPreparation(
        review_input=review_input,
        overlay=ReviewStatusOverlay(
            status=status,
            reason=reason,
            next_action=next_action,
            round_number=review_input.round_number,
        ),
        current_outcome=outcome,
        outcome_path=path,
        b1_snapshot=b1_snapshot,
        baseline_outcome=baseline_outcome,
    )


def _drifted_preparation(
    review_input: ReviewInput,
    outcome: LoopReviewOutcome,
    path: Path,
) -> LoopReviewPreparation:
    return _preparation(
        review_input,
        outcome,
        path,
        status="needs_user",
        reason="review-input-drift",
        next_action="The reviewed input changed outside the allowed repair transition.",
    )


def _overlay_for_outcome(outcome: LoopReviewOutcome) -> ReviewStatusOverlay:
    if outcome.infra_retry_count == 1 and outcome.status == "failed":
        return ReviewStatusOverlay(
            status="needs_user",
            reason="review-expert-retry-limit",
            next_action="The same-input infrastructure retry is exhausted; preserve this Loop.",
            round_number=outcome.round_number,
        )
    if outcome.status == "failed":
        return ReviewStatusOverlay(
            status="failed",
            reason="review-execution-failed",
            next_action=_FAILED_REVIEW_NEXT,
            round_number=outcome.round_number,
        )
    actual = _actual_review_data(outcome)
    if actual is not None:
        decision = actual.decision
        return ReviewStatusOverlay(
            status={
                "stop": "passed",
                "repair": "needs_fix",
                "blocked": "needs_user",
                "improve": "needs_fix",
            }[decision.action],
            reason=decision.reason,
            next_action={
                "stop": "Close the unchanged Loop result.",
                "repair": "Resolve the required gaps and actionable findings before the unique round 2.",
                "improve": "Apply only the sealed conditional improvement within its original plan, then run the unique round 2.",
                "blocked": "Stop this Loop; do not create another round or reset its contract.",
            }[decision.action],
            round_number=outcome.round_number,
        )
    if has_actionable_findings(outcome):
        if outcome.round_number == 1:
            return ReviewStatusOverlay(
                status="needs_fix",
                reason="review-findings-actionable",
                next_action="Resolve the actionable findings before round 2.",
                round_number=1,
            )
        return ReviewStatusOverlay(
            status="needs_user",
            reason="review-round-limit",
            next_action="Inspect the unresolved findings; a third round is forbidden.",
            round_number=2,
        )
    return ReviewStatusOverlay(
        status="passed",
        reason="review-passed",
        next_action="Close the unchanged Loop result.",
        round_number=outcome.round_number,
    )


__all__ = [
    "LoopReviewPreparation",
    "LoopReviewServiceError",
    "RecordLoopReviewOptions",
    "ReviewInputResolver",
    "has_actionable_findings",
    "effective_actual_action",
    "outcome_path",
    "prepare_loop_review",
    "record_loop_review",
    "validate_prepared_outcome_for_close",
]

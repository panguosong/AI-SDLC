"""在原 Loop 工件和质量命令边界内执行、恢复并采集有限对照。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictInt, ValidationError

if TYPE_CHECKING:
    from ai_sdlc.core.counterexample_models import (
        BoundEvidence,
        CounterexampleAssessment,
        ObservationBundle,
        RepairEvidence,
        ResourceBinding,
        Witness,
    )
    from ai_sdlc.core.implementation_models import (
        ImplementationInput,
        ImplementationProgress,
    )
    from ai_sdlc.core.loop_models import LoopRun
    from ai_sdlc.core.pr_review_service import VerifiedDeliveryCommit

from ai_sdlc.core.counterexample_models import (
    VERIFICATION_CAPABILITY,
    AcceptanceObservation,
    ArtifactRef,
    AttemptReceipt,
    BusinessObservation,
    CounterexamplePlan,
    CounterexampleValue,
    Digest,
    ExecutionBinding,
    ExecutionStep,
    Identifier,
    OwnedRawEvidence,
    Phase,
    Text,
    TypedValue,
    VerificationContract,
    counterexample_digest,
    obligation_task_owners,
    project_relative_path,
    source_digest_sha256,
    validate_plan_contract,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore, _require_artifact_directory
from ai_sdlc.core.quality_command import (
    ControlledQualityOptions,
    QualityCommandOptions,
    build_source_digest,
    build_source_digest_at_reviewed_parent,
    controlled_raw_original,
    run_quality_command,
    validate_quality_argv,
)
from ai_sdlc.core.stable_file_read import read_stable_bytes

_RUNTIME = (
    ".ai-sdlc/loops/",
    ".ai-sdlc/reviews/",
    ".ai-sdlc/state/",
    ".ai-sdlc/work-items/",
)
_SAFE_ENV = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
}
_RESOURCE_ENV = {"HOME", "TMP", "TEMP", "TMPDIR", "USERPROFILE"}
_RESOURCE_FD_CLEANUP = shutil.rmtree.avoids_symlink_attacks


class _AttemptIntent(CounterexampleValue):
    """仅验证已有意图的完整值结构，不重写历史字节或增加运行状态。"""

    schema_version: Literal[1]
    attempt_id: Identifier
    plan_digest: Digest
    contract_digest: Digest
    candidate_digest: Digest
    step_id: Identifier
    kind: Literal["exercise", "observe", "acceptance", "reset", "cleanup"]
    phase: Phase
    subject_id: Identifier
    binding_digest: Digest
    binding: ExecutionBinding
    ownership_nonce: Text
    started_at_ms: Annotated[StrictInt, Field(ge=0)]
    deadline_ms: Annotated[StrictInt, Field(ge=0)]
    attempt_ordinal: Annotated[StrictInt, Field(gt=0)]
    max_execution_attempts: Annotated[StrictInt, Field(gt=0)]
    reserved_seconds: Annotated[StrictInt, Field(ge=0)]
    resolved_command: dict[str, Any] | None = None
    source_snapshot: ArtifactRef | None = None
    failure_cleanup_steps: tuple[Identifier, ...] = ()
    historical_cleanup_only: StrictBool = False
    resource_ownership_required: StrictBool = False


def _validated_attempt_intent(document: Any) -> dict[str, Any]:
    """写入、执行、历史与捕获共用完整结构；原格式缺少命令证明仍仅可诊断。"""
    if not isinstance(document, dict):
        raise ValueError("counterexample-attempt-intent-shape-invalid")
    if document.get("superseded_plan_refs"):
        raise ValueError("counterexample-plan-takeover-not-supported")
    try:
        parsed = _AttemptIntent.model_validate(document)
    except ValidationError as exc:
        if any(error["type"] == "missing" for error in exc.errors()):
            raise ValueError("counterexample-original-intent-fields-missing") from exc
        raise ValueError("counterexample-attempt-intent-fields-invalid") from exc
    if (
        parsed.binding_digest != counterexample_digest(parsed.binding)
        or parsed.deadline_ms < parsed.started_at_ms
        or parsed.attempt_ordinal > parsed.max_execution_attempts
    ):
        raise ValueError("counterexample-attempt-intent-binding-or-time-invalid")
    if _command_original_required(document) and parsed.source_snapshot is None:
        raise ValueError("counterexample-original-command-fields-missing")
    # 仅返回原值；模型默认值不能变成原件，也不能补造旧版本证明。
    return document


def _bound_read(
    root: Path,
    reference: ArtifactRef,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> bytes:
    """正式审查使用捕获集合，不从当前磁盘补缺失内容。"""
    if isinstance(captured_artifacts, _CurrentStateMaterial):
        return captured_artifacts.read_reference(reference)
    if captured_artifacts is None:
        return _read_ref(root, reference)
    if reference.path not in captured_artifacts:
        raise ValueError(f"counterexample-captured-evidence-missing: {reference.path}")
    raw = captured_artifacts[reference.path]
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError("counterexample-captured-evidence-stale")
    return raw


def _native_counterexample_r2(
    root: Path,
    impl_input: ImplementationInput,
    captured_artifacts: Mapping[str, bytes] | None = None,
    *,
    phase_capture: dict[str, Any] | None = None,
) -> tuple[bool, tuple[ArtifactRef, ...]]:
    from ai_sdlc.core.implementation_store import implementation_artifacts
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome
    from ai_sdlc.core.loop_review_service import effective_actual_action, outcome_path

    path = outcome_path(implementation_artifacts(root, impl_input.loop_id).loop_dir, 1)
    key = path.relative_to(root).as_posix()
    if captured_artifacts is not None:
        raw = captured_artifacts.get(key)
    else:
        raw = read_stable_bytes(root, path) if path.is_file() else None
    if raw is None:
        if captured_artifacts is not None and not isinstance(
            captured_artifacts, _CurrentStateMaterial
        ):
            raise ValueError("counterexample-captured-review-phase-missing")
        if phase_capture is not None:
            phase_capture.update(observed_at_ms=time.time_ns() // 1_000_000)
        return False, ()
    outcome = LoopReviewOutcome.model_validate_json(raw)
    if outcome.loop_id != impl_input.loop_id or outcome.round_number != 1:
        raise ValueError("counterexample-original-review-identity-mismatch")
    action = effective_actual_action(None, outcome)
    if phase_capture is not None:
        phase_capture.update(observed_at_ms=time.time_ns() // 1_000_000, review_raw=raw)
    if outcome.simulation is not None and action == "improve":
        from ai_sdlc.cli.loop_review_cmd import (
            _counterexample_execution_capture,
            resolve_b1_review_snapshot,
        )

        # 原 R1 的有效动作负责过期改善收缩；捕获时不能先要求尚未执行的 R2。
        with _counterexample_execution_capture():
            snapshot = resolve_b1_review_snapshot(
                root, impl_input.loop_id, 1, loop_type="implementation"
            )
        if snapshot is None:
            raise ValueError("counterexample-original-review-snapshot-missing")
        if captured_artifacts is not None and not isinstance(
            captured_artifacts, _CurrentStateMaterial
        ):
            if snapshot is None:
                raise ValueError("counterexample-original-review-snapshot-missing")
            for captured_path, digest in snapshot.manifest.items():
                _bound_read(
                    root,
                    ArtifactRef(path=captured_path, sha256=digest),
                    captured_artifacts,
                )
        action = effective_actual_action(snapshot, outcome)
        if phase_capture is not None:
            phase_capture.update(
                observed_at_ms=snapshot.observed_at_ms,
                snapshot={
                    "review_input": snapshot.review_input.model_dump(mode="json"),
                    "context": snapshot.context.model_dump(mode="json"),
                    "manifest": dict(snapshot.manifest),
                    "source_digest": snapshot.source_digest,
                    "observed_at_ms": snapshot.observed_at_ms,
                    "context_path": snapshot.context_path,
                },
            )
    required = action in {"repair", "improve"}
    # 已发生但不要求 R2 的原 R1 也须保留；不能在捕获中变成未经证明的缺席。
    # 这里只读取原轮次；新执行另经原 purpose=execute 的完整校验。
    refs = (ArtifactRef(path=key, sha256=hashlib.sha256(raw).hexdigest()),)
    return required, refs


def _save_phase_context(
    root: Path, plan: CounterexamplePlan, required: bool, captured: Mapping[str, Any]
):
    from ai_sdlc.core.counterexample_models import CounterexamplePhaseContext

    folder = _attempts_dir(root, plan).parent / "results"
    review_ref = None
    if "review_raw" in captured:
        raw = captured["review_raw"]
        path = folder / f"phase-review-{hashlib.sha256(raw).hexdigest()}.json"
        LoopArtifactStore(root).write_bytes_artifact(path, raw, immutable=True)
        review_ref = _ref(root, path)
    snapshot_ref = None
    if "snapshot" in captured:
        snapshot = captured["snapshot"]
        snapshot_ref = _write_json(
            root,
            folder / f"phase-snapshot-{counterexample_digest(snapshot)}.json",
            snapshot,
        )
    return CounterexamplePhaseContext(
        require_r2=required,
        observed_at_ms=captured["observed_at_ms"],
        review_ref=review_ref,
        snapshot_ref=snapshot_ref,
    )


def _record_requires_r2(record, plan: CounterexamplePlan, read) -> bool:
    """回读当时的原生阶段依据，不用后来 R1 或当前时钟重新解释历史。"""
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome
    from ai_sdlc.core.loop_review_service import effective_actual_action

    phase = record.phase_context
    if phase is None:
        # 旧原件的纯核口径不变；当前终态仍会独立要求现在实际必需的 R2。
        return False
    if phase.observed_at_ms > record.recorded_at_ms:
        raise ValueError("counterexample-phase-observation-after-record")
    if phase.review_ref is None:
        if phase.require_r2 or phase.snapshot_ref is not None:
            raise ValueError("counterexample-phase-review-required")
        return False
    prefix = f".ai-sdlc/loops/implementation/{plan.loop_id}/counterexamples/results/"
    raw = read(phase.review_ref)
    if phase.review_ref.path != (
        prefix + f"phase-review-{hashlib.sha256(raw).hexdigest()}.json"
    ):
        raise ValueError("counterexample-phase-review-owner-mismatch")
    outcome = LoopReviewOutcome.model_validate_json(raw)
    if (
        outcome.loop_id != plan.loop_id
        or outcome.loop_type != "implementation"
        or outcome.round_number != 1
    ):
        raise ValueError("counterexample-original-review-identity-mismatch")
    action = effective_actual_action(None, outcome)
    if action == "improve":
        if phase.snapshot_ref is None:
            raise ValueError("counterexample-phase-improvement-snapshot-required")
        from ai_sdlc.core.loop_simulation_context import SimulationContext
        from ai_sdlc.core.loop_stage_decision_service import (
            StageReviewSnapshot,
            _checked_stage_snapshot,
        )
        from ai_sdlc.core.review_kernel import ReviewInput

        payload = json.loads(read(phase.snapshot_ref))
        if phase.snapshot_ref.path != (
            prefix + f"phase-snapshot-{counterexample_digest(payload)}.json"
        ):
            raise ValueError("counterexample-phase-snapshot-owner-mismatch")
        snapshot = _checked_stage_snapshot(
            StageReviewSnapshot(
                review_input=ReviewInput.model_validate(payload["review_input"]),
                context=SimulationContext.model_validate(payload["context"]),
                manifest=payload["manifest"],
                source_digest=payload["source_digest"],
                observed_at_ms=payload["observed_at_ms"],
                context_path=payload["context_path"],
            )
        )
        if (
            snapshot.review_input.loop_id != plan.loop_id
            or snapshot.review_input.round_number != 1
            or snapshot.observed_at_ms != phase.observed_at_ms
            or outcome.simulation is None
            or snapshot.context.context_digest != outcome.simulation.context_digest
        ):
            raise ValueError("counterexample-phase-snapshot-review-mismatch")
        action = effective_actual_action(snapshot, outcome)
    elif phase.snapshot_ref is not None:
        raise ValueError("counterexample-phase-unexpected-snapshot")
    required = action in {"repair", "improve"}
    if required != phase.require_r2:
        raise ValueError("counterexample-phase-requirement-mismatch")
    return required


def _read_history_plan(
    root: Path,
    path: Path,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[CounterexamplePlan, ArtifactRef]:
    """旧执行先绑定原计划；同一解析判据供重置、历史恢复与捕获验收使用。"""
    key = path.relative_to(root).as_posix()
    if isinstance(captured_artifacts, _CurrentStateMaterial):
        raw = captured_artifacts.read_required(key)
    elif captured_artifacts is None:
        raw = read_stable_bytes(root, path)
    else:
        raw = captured_artifacts.get(key)
    if raw is None:
        raise ValueError("counterexample-history-plan-missing")
    plan = CounterexamplePlan.model_validate_json(raw)
    if path.stem != counterexample_digest(plan):
        raise ValueError("counterexample-history-plan-identity-mismatch")
    return plan, ArtifactRef(path=key, sha256=hashlib.sha256(raw).hexdigest())


def _plan_history(
    root: Path, plan: CounterexamplePlan
) -> tuple[Path, list[CounterexamplePlan]]:
    folder = _attempts_dir(root, plan).parent / "plans"
    previous = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else ():
        prior, _ = _read_history_plan(root, path)
        _require_original_batch(root, prior, plan)
        previous.append(prior)
    if any(
        plan.required_reserve_seconds < prior.required_reserve_seconds
        for prior in previous
    ):
        raise ValueError("counterexample-original-plan-scope-or-budget-changed")
    if len({p.v1_digest for p in (*previous, plan) if p.v1_digest is not None}) > 1:
        raise ValueError("counterexample-reinforcement-batch-exhausted")
    return folder, previous


def _batch_inputs(plan: CounterexamplePlan) -> dict[str, Any]:
    """目录可因真实修复重定位，原错误输入和全部合法控制不能择优删除。"""
    return {
        "subjects": {
            subject.id: (
                subject.role,
                subject.obligation_id,
                subject.witness_id,
                subject.mechanism_key,
                tuple(sorted(subject.positive_control_refs)),
            )
            for subject in plan.subjects
        },
        "witnesses": {
            witness.id: {
                "sha256": witness.input_ref.sha256,
                "value": witness.input_value.model_dump(mode="json"),
                "premises": {
                    key: value.model_dump(mode="json")
                    for key, value in witness.premise_values.items()
                },
            }
            for witness in plan.witnesses
        },
    }


def _require_same_candidate_batch(
    root: Path,
    prior: CounterexamplePlan,
    plan: CounterexamplePlan,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> None:
    introducing_v1 = prior.v1_digest is None and plan.v1_digest is not None
    if prior.v1_digest is not None and plan.v1_digest != prior.v1_digest:
        raise ValueError("counterexample-reinforcement-batch-exhausted")
    old_subjects = {subject.id: subject for subject in prior.subjects}
    for subject in plan.subjects:
        old = old_subjects[subject.id]
        if (
            old.candidate_digest != subject.candidate_digest
            or old.modified_paths != subject.modified_paths
            or (old.patch.sha256 if old.patch else None)
            != (subject.patch.sha256 if subject.patch else None)
        ):
            raise ValueError("counterexample-generation-batch-content-changed")
        before = json.loads(_bound_read(root, old.snapshot, captured_artifacts))
        after = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        # 首次强化允许额外绑定隔离草案；原候选、变体及其他忽略输入仍须相同。
        added = after["acceptance_sources"].get("V1", {}).get("path")
        excluded = {added} if introducing_v1 else set()
        files_before = {
            row["path"]: (row["sha256"], row["mode"])
            for row in before["files"]
            if row["path"] not in excluded
        }
        files_after = {
            row["path"]: (row["sha256"], row["mode"])
            for row in after["files"]
            if row["path"] not in excluded
        }
        if files_before != files_after:
            raise ValueError("counterexample-generation-batch-content-changed")


def _require_original_batch(
    root: Path,
    prior: CounterexamplePlan,
    plan: CounterexamplePlan,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> None:
    """执行入口与验收消费者共同约束批次，换源码身份不产生新的反例预算。"""
    _require_loop_batch_policy(prior, plan)
    # 同一 Loop 的任务共享预算与唯一强化，但不互相替换原错误输入和文件范围。
    if prior.task_id != plan.task_id:
        return
    if (
        any(
            getattr(prior, field) != getattr(plan, field)
            for field in (
                "loop_id",
                "task_id",
                "contract_digest",
                "work_item_id",
                "budget_ref",
                "v0_digest",
                "allowed_modified_paths",
                "protected_paths",
                "max_execution_attempts",
            )
        )
        or plan.required_reserve_seconds < prior.required_reserve_seconds
    ):
        raise ValueError("counterexample-original-plan-scope-or-budget-changed")
    if _batch_inputs(prior) != _batch_inputs(plan):
        raise ValueError("counterexample-original-controls-or-witnesses-changed")
    if prior.v1_digest is not None and plan.v1_digest != prior.v1_digest:
        raise ValueError("counterexample-reinforcement-batch-exhausted")
    introducing_v1 = prior.v1_digest is None and plan.v1_digest is not None
    if prior.candidate_digest == plan.candidate_digest:
        _require_same_candidate_batch(root, prior, plan, captured_artifacts)
    else:
        _require_rebased_batch(root, prior, plan, captured_artifacts)
    # 新独享目录允许重定位，但全部控制的输入初态与执行路径不能改变。
    versions = ("V0",) if introducing_v1 else ("V0", "V1")
    if _repair_replay_identity(
        root, prior, captured_artifacts, all_subjects=True, acceptance_versions=versions
    ) != _repair_replay_identity(
        root, plan, captured_artifacts, all_subjects=True, acceptance_versions=versions
    ):
        raise ValueError("counterexample-original-execution-table-changed")


def _require_loop_batch_policy(
    prior: CounterexamplePlan, plan: CounterexamplePlan
) -> None:
    if any(
        getattr(prior, field) != getattr(plan, field)
        for field in (
            "loop_id",
            "contract_digest",
            "work_item_id",
            "budget_ref",
            "v0_digest",
            "max_execution_attempts",
        )
    ):
        raise ValueError("counterexample-original-plan-scope-or-budget-changed")
    if (
        prior.v1_digest is not None
        and plan.v1_digest is not None
        and prior.v1_digest != plan.v1_digest
    ):
        raise ValueError("counterexample-reinforcement-batch-exhausted")


def _implementation_obligation_owners(
    root: Path,
    impl_input: ImplementationInput,
    contract: VerificationContract,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> dict[str, str | None]:
    scopes = getattr(impl_input, "task_scopes", {})
    task_ids = tuple(scopes)
    if not task_ids:
        from ai_sdlc.core.implementation_loop import _TASK_ID, _task_sections

        path = getattr(impl_input, "tasks_path", "")
        if not path:
            raise ValueError("counterexample-original-task-ownership-unavailable")
        project_relative_path(path)
        if isinstance(captured_artifacts, _CurrentStateMaterial):
            raw = captured_artifacts.read_required(path)
        elif captured_artifacts is None:
            raw = read_stable_bytes(root, root / path)
        else:
            raw = captured_artifacts.get(path)
        if raw is None:
            raise ValueError("counterexample-original-task-ownership-unavailable")
        task_ids = tuple(
            next(iter(_TASK_ID.findall(section)), "")
            for section in _task_sections(raw.decode("utf-8"))
        )
    return obligation_task_owners(contract, task_ids=task_ids)


def _merge_original_change(before: bytes, variant: bytes, repaired: bytes) -> bytes:
    """只重放可证明不冲突的原行变更；相同修复去重，交叠或歧义保持未证明。"""
    if variant == before:
        return repaired
    if repaired == before or variant == repaired:
        return variant
    lines = before.splitlines(keepends=True)

    def edits(content: bytes) -> list[tuple[int, int, tuple[bytes, ...]]]:
        changed = content.splitlines(keepends=True)
        return [
            (start, end, tuple(changed[left:right]))
            for kind, start, end, left, right in SequenceMatcher(
                None, lines, changed
            ).get_opcodes()
            if kind != "equal"
        ]

    original, repair = edits(variant), edits(repaired)
    for left in original:
        for right in repair:
            if left == right:
                continue
            if (
                max(left[0], right[0]) < min(left[1], right[1])
                or left[0] == left[1]
                and right[0] < left[0] < right[1]
                or right[0] == right[1]
                and left[0] < right[0] < left[1]
                or left[0] == left[1] == right[0] == right[1]
            ):
                raise ValueError(
                    "counterexample-generation-batch-content-rebase-unproven"
                )
    result = list(lines)
    for start, end, replacement in sorted(set((*original, *repair)), reverse=True):
        result[start:end] = replacement
    return b"".join(result)


def _require_rebased_batch(
    root: Path,
    prior: CounterexamplePlan,
    plan: CounterexamplePlan,
    captured_artifacts: Mapping[str, bytes] | None,
) -> None:
    def files(subject: Any) -> dict[str, tuple[bytes, int]]:
        manifest = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        return {
            item["path"]: (
                _bound_read(
                    root,
                    ArtifactRef.model_validate(item["content_ref"]),
                    captured_artifacts,
                ),
                item["mode"],
            )
            for item in manifest["files"]
        }

    old_files = {subject.id: files(subject) for subject in prior.subjects}
    new_files = {subject.id: files(subject) for subject in plan.subjects}
    for current in (subject for subject in plan.subjects if subject.role == "current"):
        before, repaired = old_files[current.id], new_files[current.id]
        added_v1 = None
        if prior.v1_digest is None and plan.v1_digest is not None:
            manifest = json.loads(
                _bound_read(root, current.snapshot, captured_artifacts)
            )
            added_v1 = manifest["acceptance_sources"]["V1"]["path"]
        if any(
            before.get(path) != repaired.get(path)
            for path in plan.protected_paths
            if path != added_v1
        ):
            raise ValueError("counterexample-generation-batch-content-observer-changed")
        for subject in plan.subjects:
            if (
                subject.role == "current"
                or subject.obligation_id != current.obligation_id
            ):
                continue
            original, actual = old_files[subject.id], new_files[subject.id]
            for path in (
                before.keys() | original.keys() | repaired.keys() | actual.keys()
            ):
                base, mutation, repair = (
                    before.get(path),
                    original.get(path),
                    repaired.get(path),
                )
                if mutation == base:
                    expected = repair
                elif repair == base or mutation == repair:
                    expected = mutation
                elif base is None or mutation is None or repair is None:
                    raise ValueError(
                        "counterexample-generation-batch-content-rebase-unproven"
                    )
                else:
                    # 文件模式与内容分别传播；同一模式变更重合可去重。
                    mode = repair[1] if mutation[1] == base[1] else mutation[1]
                    if mutation[1] != base[1] and repair[1] not in (
                        base[1],
                        mutation[1],
                    ):
                        raise ValueError(
                            "counterexample-generation-batch-content-mode-conflict"
                        )
                    expected = (
                        _merge_original_change(base[0], mutation[0], repair[0]),
                        mode,
                    )
                if actual.get(path) != expected:
                    raise ValueError("counterexample-generation-batch-content-changed")


def _attempt_history(
    root: Path,
    folder: Path,
    captured_artifacts: Mapping[str, bytes] | None = None,
    refs: dict[str, ArtifactRef] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """缺意图的残留输出也是未知历史；不能因仅扫描意图而重放已有副作用。"""
    prefix = folder.relative_to(root).as_posix() + "/"
    groups: dict[str, dict[str, bytes]] = {}
    if captured_artifacts is not None:
        for key, raw in captured_artifacts.items():
            if key.startswith(prefix):
                relative = key[len(prefix) :]
                directory, separator, filename = relative.partition("/")
                if not separator or "/" in filename:
                    raise ValueError("counterexample-attempt-history-layout-invalid")
                groups.setdefault(directory, {})[filename] = raw
    elif folder.exists():
        for directory in folder.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("counterexample-attempt-history-layout-invalid")
            files = groups.setdefault(directory.name, {})
            for path in directory.iterdir():
                raw = read_stable_bytes(root, path)
                files[path.name] = raw
                if refs is not None:
                    key = path.relative_to(root).as_posix()
                    refs[key] = ArtifactRef(
                        path=key, sha256=hashlib.sha256(raw).hexdigest()
                    )
    result = []
    ordinals = []
    for name, files in sorted(groups.items()):
        if "intent.json" not in files:
            raise ValueError("counterexample-orphan-attempt-intent-missing-no-replay")
        intent = json.loads(files["intent.json"])
        if not isinstance(intent, dict):
            raise ValueError("counterexample-attempt-history-identity-invalid")
        ordinal = intent.get("attempt_ordinal")
        if intent.get("attempt_id") != name or type(ordinal) is not int or ordinal < 1:
            raise ValueError("counterexample-attempt-history-identity-invalid")
        intent = _validated_attempt_intent(intent)
        ordinals.append(ordinal)
        result.append((folder / name / "intent.json", intent))
    if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
        raise ValueError("counterexample-attempt-history-sequence-incomplete")
    return result


def _historical_resource_cycles(
    root: Path,
    plan: CounterexamplePlan,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[
    list[tuple[Path, dict[str, Any], CounterexamplePlan, AttemptReceipt]],
    dict[tuple[str, str], tuple[CounterexamplePlan, dict[str, Any]]],
]:
    """按真实意图序号核对每个旧资源周期，候选过期不免除原收尾义务。"""
    folder = _attempts_dir(root, plan)
    plans = {counterexample_digest(plan): plan}
    history = []
    debts = {}
    attempts = sorted(
        _attempt_history(root, folder, captured_artifacts),
        key=lambda item: item[1]["attempt_ordinal"],
    )
    for path, intent in attempts:
        digest = intent["plan_digest"]
        if digest not in plans:
            plan_path = folder.parent / "plans" / f"{digest}.json"
            historical, _ = _read_history_plan(root, plan_path, captured_artifacts)
            plans[digest] = historical
        historical = plans[digest]
        _require_loop_batch_policy(historical, plan)
        if (
            any(
                getattr(historical, name) != getattr(plan, name)
                for name in (
                    "loop_id",
                    "contract_digest",
                    "work_item_id",
                    "budget_ref",
                    "max_execution_attempts",
                )
            )
            or intent.get("max_execution_attempts") != plan.max_execution_attempts
        ):
            raise ValueError("counterexample-attempt-budget-policy-changed")
        intent_key = path.relative_to(root).as_posix()
        raw = (
            captured_artifacts[intent_key]
            if captured_artifacts is not None
            else read_stable_bytes(root, path)
        )
        reference = ArtifactRef(path=intent_key, sha256=hashlib.sha256(raw).hexdigest())
        receipt = recover_counterexample_attempt(
            root, historical, reference, captured_artifacts=captured_artifacts
        )
        if (
            receipt.status == "execution_unknown"
            or receipt.cleanup_status != "complete"
        ):
            raise ValueError("counterexample-prior-execution-unknown-no-replay")
        step = next(item for item in historical.steps if item.id == receipt.step_id)
        if (
            intent.get("kind") != step.kind
            or intent.get("phase") != step.phase
            or intent.get("subject_id") != step.subject_id
            or intent.get("binding") != step.binding.model_dump(mode="json")
        ):
            raise ValueError("counterexample-history-step-binding-conflict")
        history.append((path, intent, historical, receipt))
        key = (digest, step.subject_id)
        if _resource_cleanup_completed(receipt, step):
            active = debts.get(key)
            if active is not None and {
                item["id"]: item for item in active[1]["binding"]["resources"]
            } != {item["id"]: item for item in intent["binding"]["resources"]}:
                raise ValueError(
                    "counterexample-historical-resource-cleanup-binding-conflict"
                )
            debts.pop(key, None)
        else:
            if step.kind == "cleanup" and _failed_operation(receipt, step):
                raise ValueError("counterexample-historical-resource-cleanup-incomplete")
            debts[key] = (historical, intent)
    return history, debts


def _pending_attempt_steps(
    plan: CounterexamplePlan, receipts: Sequence[AttemptReceipt]
) -> tuple[ExecutionStep, ...]:
    """预留冻结条件阶段，只有已知失败不可执行的分支释放预留。"""
    done = {receipt.step_id for receipt in receipts}
    blocked = _blocked_business_steps(plan, receipts)
    return tuple(
        step for step in plan.steps if step.id not in done and step.id not in blocked
    )


def _selected_task_plans(
    root: Path,
    plans: Mapping[str, CounterexamplePlan],
    attempts: Sequence[tuple[Mapping[str, Any], CounterexamplePlan]],
    *,
    current_plan: CounterexamplePlan | None = None,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> dict[str, CounterexamplePlan]:
    """预算与当前验收共用原序号；未启动原表不允许被后继接管。"""
    plans = dict(plans)
    attempted: set[str] = set()
    latest: dict[str, str] = {}
    for intent, saved in sorted(attempts, key=lambda item: item[0]["attempt_ordinal"]):
        digest = counterexample_digest(saved)
        if digest not in plans:
            continue
        attempted.add(digest)
        # 历史清理只偿还原债，不能把旧业务表重新变成当前承诺。
        if not intent.get("historical_cleanup_only", False):
            latest[saved.task_id] = digest
    if current_plan is not None:
        current_digest = counterexample_digest(current_plan)
        if (
            current_digest in attempted
            and latest.get(current_plan.task_id) != current_digest
        ):
            raise ValueError("counterexample-superseded-plan-no-business-replay")
        for saved in tuple(plans.values()):
            if (
                saved.task_id == current_plan.task_id
                and counterexample_digest(saved) != current_digest
            ):
                if counterexample_digest(saved) not in attempted:
                    raise ValueError("counterexample-unfinished-predecessor-no-takeover")
                _require_original_batch(root, saved, current_plan, captured_artifacts)
        plans[current_digest] = current_plan
        latest[current_plan.task_id] = current_digest
    selected: dict[str, CounterexamplePlan] = {}
    for task in {saved.task_id for saved in plans.values()}:
        candidates = {
            digest: saved
            for digest, saved in plans.items()
            if saved.task_id == task
        }
        if current_plan is not None and task == current_plan.task_id:
            selected[current_digest] = current_plan
            continue
        if not candidates:
            continue
        unstarted = set(candidates) - attempted
        if unstarted:
            # 没有真实序号，不猜零意图草案与其他冻结表的先后关系。
            if len(candidates) != 1:
                raise ValueError("counterexample-frozen-task-plan-order-ambiguous")
            chosen = next(iter(unstarted))
        else:
            chosen = latest.get(task)
            if chosen is None or chosen not in candidates:
                raise ValueError("counterexample-frozen-task-plan-order-ambiguous")
        for digest, saved in candidates.items():
            if digest != chosen:
                _require_original_batch(
                    root, saved, candidates[chosen], captured_artifacts
                )
        selected[chosen] = candidates[chosen]
    return selected


def _shared_pending_steps(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    history: Sequence[tuple[Path, dict[str, Any], CounterexamplePlan, AttemptReceipt]],
    debts: Mapping[tuple[str, str], tuple[CounterexamplePlan, dict[str, Any]]],
    *,
    replace_current: bool = True,
) -> tuple[tuple[CounterexamplePlan, ExecutionStep], ...]:
    """从冻结计划和原序号推导共享承诺；零意图计划不能释放其他任务预算。"""
    plans: dict[str, CounterexamplePlan] = {}
    for path in (_attempts_dir(root, plan).parent / "plans").glob("*.json"):
        saved = CounterexamplePlan.model_validate_json(read_stable_bytes(root, path))
        if path.stem != counterexample_digest(saved):
            raise ValueError("counterexample-history-plan-identity-mismatch")
        validate_plan_contract(contract, saved)
        _require_loop_batch_policy(saved, plan)
        plans[path.stem] = saved
    receipts: dict[str, list[AttemptReceipt]] = {}
    for _, _intent, saved, receipt in history:
        digest = counterexample_digest(saved)
        plans[digest] = saved
        receipts.setdefault(digest, []).append(receipt)
    selected = _selected_task_plans(
        root,
        plans,
        [(intent, saved) for _, intent, saved, _ in history],
        current_plan=plan if replace_current else None,
    )
    pending = {
        (digest, step.id): (saved, step)
        for digest, saved in selected.items()
        for step in _pending_attempt_steps(saved, receipts.get(digest, ()))
    }
    for (digest, _), (saved, intent) in debts.items():
        cleanup = _historical_cleanup_step(saved, intent, receipts.get(digest, ()))
        pending[(digest, cleanup.id)] = (saved, cleanup)
    return tuple(pending.values())


def _historical_cleanup_step(
    plan: CounterexamplePlan,
    intent: Mapping[str, Any],
    receipts: Sequence[AttemptReceipt],
) -> ExecutionStep:
    order = {step.id: index for index, step in enumerate(plan.steps)}
    done = {receipt.step_id for receipt in receipts}
    for step in plan.steps[order[intent["step_id"]] + 1 :]:
        if (
            step.kind == "cleanup"
            and step.subject_id == intent["subject_id"]
            and step.id not in done
        ):
            return step
    raise ValueError("counterexample-historical-frozen-cleanup-unavailable")


def _validate_cleanup_subject_live(
    root: Path,
    plan: CounterexamplePlan,
    step: ExecutionStep,
    *,
    defer_file_map: bool = False,
) -> None:
    """原件关系仍全验，只将实时源码核对限定为即将清理的原对象。"""
    validate_counterexample_snapshots(root, plan, require_live=False)
    subject = next(item for item in plan.subjects if item.id == step.subject_id)
    manifest = _snapshot_document(_read_ref(root, subject.snapshot))
    project = Path(manifest["root"])
    if (
        project.resolve(strict=True) != project
        or str(project) != step.binding.project_root
        or source_digest_sha256(build_source_digest(project))
        != subject.candidate_digest
    ):
        raise ValueError("counterexample-historical-cleanup-subject-source-stale")
    captured = [
        {key: value for key, value in entry.items() if key != "content_ref"}
        for entry in manifest["files"]
    ]
    if defer_file_map:
        # cleanup 尚未产生新意图时先拒绝已漂移的声明输入；完整文件图仍在 nonce 前只读一次。
        indexed = {entry["path"]: entry for entry in captured}
        for relative in manifest["ignored_inputs"]:
            path = _path(project, relative)
            actual = (
                {
                    "path": relative,
                    "sha256": hashlib.sha256(
                        read_stable_bytes(project, path)
                    ).hexdigest(),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
                if path.exists()
                else None
            )
            if actual != indexed.get(relative):
                raise ValueError(
                    "counterexample-historical-cleanup-subject-files-stale"
                )
    elif captured != _file_map(project, manifest["ignored_inputs"]):
        raise ValueError("counterexample-historical-cleanup-subject-files-stale")


def _unfinished_predecessor_plans(
    root: Path,
    plan: CounterexamplePlan,
    history: Sequence[tuple[Path, dict[str, Any], CounterexamplePlan, AttemptReceipt]],
    *,
    impl_input: ImplementationInput | None = None,
) -> tuple[CounterexamplePlan, ...]:
    """仅完整原生结果可接续真实修复；不接管零尝试或重建未归集旧结果。"""
    from ai_sdlc.core.counterexample_models import (
        CounterexampleEvidenceRecord,
        required_execution_steps,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts, read_input

    _, previous = _plan_history(root, plan)
    results = _attempts_dir(root, plan).parent / "results"
    unfinished = []
    for old in previous:
        if old.task_id != plan.task_id or counterexample_digest(old) == counterexample_digest(plan):
            continue
        attempted = {
            receipt.attempt_id
            for _, _, saved, receipt in history
            if counterexample_digest(saved) == counterexample_digest(old)
        }
        complete = False
        if attempted:
            for path in sorted(results.glob("record-*.json")):
                if path.name.startswith("record-reconciled-"):
                    continue
                reference = _ref(root, path)
                record = CounterexampleEvidenceRecord.model_validate_json(_read_ref(root, reference))
                if record.plan_ref.path != (
                    results.parent / "plans" / f"{counterexample_digest(old)}.json"
                ).relative_to(root).as_posix():
                    continue
                if impl_input is None:
                    impl_input = read_input(implementation_artifacts(root, plan.loop_id).input_path)
                original = resolve_counterexample_evidence(root, impl_input, reference)
                required = required_execution_steps(
                    old, original.observations,
                    require_r2=_record_requires_r2(record, old, lambda ref: _read_ref(root, ref)),
                )
                if (
                    original.assessment.current_result.status in {"PASS", "FAIL"}
                    and attempted <= {receipt.attempt_id for receipt in original.observations.attempts}
                    and {step.id for step in required} <= {
                        receipt.step_id for receipt in original.observations.attempts
                    }
                ):
                    complete = True
                    break
        if not complete:
            unfinished.append(old)
    return tuple(unfinished)


def _recover_plan_attempts(
    root: Path, plan: CounterexamplePlan
) -> list[AttemptReceipt]:
    """所有历史均占用原成本；未完成的旧执行不会因读取完整结果而被隐藏。"""
    history, _ = _historical_resource_cycles(root, plan)
    return [
        receipt
        for _, _, historical, receipt in history
        if counterexample_digest(historical) == counterexample_digest(plan)
    ]


def _failed_operation(receipt: AttemptReceipt, step: ExecutionStep) -> bool:
    """断言拒绝属于验收结果；执行故障才终止后续业务步骤。"""
    return (
        not receipt.normally_completed
        or (receipt.exit_code != 0 and step.kind != "acceptance")
    )


def _failure_cleanup_dependencies(
    plan: CounterexamplePlan,
    step: ExecutionStep,
    receipts: Sequence[AttemptReceipt],
    completed_steps: set[str],
) -> tuple[str, ...]:
    """失败后的既定收尾可越过未启动业务依赖，不将它们改写成成功。"""
    if step.kind != "cleanup":
        return ()
    by_id = {item.id: item for item in plan.steps}
    ancestors: set[str] = set()
    pending = list(step.depends_on)
    while pending:
        identifier = pending.pop()
        if identifier not in ancestors:
            ancestors.add(identifier)
            pending.extend(by_id[identifier].depends_on)
    failures = tuple(
        receipt.step_id
        for receipt in receipts
        if receipt.step_id in ancestors
        and _failed_operation(receipt, by_id[receipt.step_id])
    )
    blocked = _blocked_business_steps(plan, receipts)
    # 调用者已经逐条证明所有已启动进程结束；跨对象的未完成依赖不能跳过。
    if failures and all(
        by_id[item].subject_id == step.subject_id
        or item in completed_steps
        or item in blocked
        for item in ancestors
    ):
        return failures
    return ()


def _prelaunch_resources_cleaned(receipt: AttemptReceipt) -> bool:
    # 原件读取只在 intent、未启动和实际目录清理全部绑定时暴露此引用。
    return (
        receipt.status == "infrastructure_error"
        and receipt.cleanup_status == "complete"
        and any(ref.path.endswith("/resource-cleanup.json") for ref in receipt.raw_evidence_refs)
    )


def _resource_cleanup_completed(receipt: AttemptReceipt, step: ExecutionStep) -> bool:
    # 所有资源债务消费者复用同一已验证原件事实，技术失败仍保留失败身份。
    return _prelaunch_resources_cleaned(receipt) or (
        step.kind == "cleanup"
        and not _failed_operation(receipt, step)
        and receipt.cleanup_status == "complete"
    )


def _subject_resources_active(
    plan: CounterexamplePlan, receipts: Sequence[AttemptReceipt], subject_id: str
) -> bool:
    actual = {receipt.step_id: receipt for receipt in receipts}
    for step in reversed(plan.steps):
        if step.subject_id == subject_id and step.id in actual:
            return not _resource_cleanup_completed(actual[step.id], step)
    return False


def _blocked_business_steps(
    plan: CounterexamplePlan, receipts: Sequence[AttemptReceipt]
) -> set[str]:
    """沿既定依赖停止失败分支；独立对象继续，cleanup 不视为业务成功。"""
    actual = {receipt.step_id: receipt for receipt in receipts}
    blocked: set[str] = set()
    subjects: set[str] = set()
    for step in plan.steps:
        receipt = actual.get(step.id)
        if (
            receipt is not None
            and _failed_operation(receipt, step)
            or step.kind != "cleanup"
            and receipt is None
            and (step.subject_id in subjects or set(step.depends_on) & blocked)
        ):
            blocked.add(step.id)
            subjects.add(step.subject_id)
        elif step.kind == "cleanup" and receipt is None and step.subject_id in subjects:
            if not _subject_resources_active(plan, receipts, step.subject_id):
                blocked.add(step.id)
    return blocked


def _bundle_from_attempts(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    receipts: Sequence[AttemptReceipt],
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> ObservationBundle:
    from ai_sdlc.core.counterexample_models import ObservationBundle

    order = {step.id: index for index, step in enumerate(plan.steps)}
    receipts = sorted(
        receipts,
        key=lambda receipt: (
            order[receipt.step_id],
            receipt.started_at_ms,
            receipt.attempt_id,
        ),
    )
    business: list[BusinessObservation] = []
    acceptance: list[AcceptanceObservation] = []
    raw_values: list[OwnedRawEvidence] = []
    for receipt in receipts:
        raw = read_owned_raw_evidence(
            root, receipt, captured_artifacts=captured_artifacts
        )
        raw_values.extend(raw)
        observation = collect_observation(contract, plan, receipt, raw)
        if isinstance(observation, BusinessObservation):
            business.append(observation)
        elif isinstance(observation, AcceptanceObservation):
            acceptance.append(observation)
    return ObservationBundle(
        business=tuple(business),
        acceptance=tuple(acceptance),
        attempts=tuple(receipts),
        raw_evidence=tuple(raw_values),
    )


def _verify_witness_content(witness: Witness, raw: bytes) -> None:
    payload = json.loads(raw)
    expected = {
        "schema_version": 1,
        "input_value": witness.input_value.model_dump(mode="json"),
        "premise_values": {
            key: value.model_dump(mode="json")
            for key, value in witness.premise_values.items()
        },
    }
    if payload != expected or type(payload.get("schema_version")) is not int:
        raise ValueError("counterexample-witness-content-mismatch")


def _original_task_allows(
    impl_input: ImplementationInput, task_id: str, path: str
) -> bool:
    from fnmatch import fnmatchcase

    def matches(parts: list[str], pattern: list[str]) -> bool:
        if not pattern:
            return not parts
        if pattern[0] == "**":
            return matches(parts, pattern[1:]) or bool(
                parts and matches(parts[1:], pattern)
            )
        return bool(
            parts
            and fnmatchcase(parts[0], pattern[0])
            and matches(parts[1:], pattern[1:])
        )

    scopes = impl_input.task_scopes.get(task_id, impl_input.declared_scope)
    return any(
        matches(path.split("/"), scope.rstrip("/").split("/"))
        or (
            not any(char in scope for char in "*?[")
            and path.startswith(scope.rstrip("/") + "/")
        )
        for scope in scopes
    )


def _current_file_map(
    root: Path,
    plan: CounterexamplePlan,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for subject in plan.subjects:
        if subject.role != "current":
            continue
        manifest = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        for entry in manifest["files"]:
            existing = files.get(entry["path"])
            if existing is not None and (existing["sha256"], existing["mode"]) != (
                entry["sha256"],
                entry["mode"],
            ):
                raise ValueError("counterexample-current-snapshot-content-conflict")
            files[entry["path"]] = entry
    return files


def _repair_replay_identity(
    root: Path,
    plan: CounterexamplePlan,
    captured_artifacts: Mapping[str, bytes] | None = None,
    *,
    all_subjects: bool = False,
    acceptance_versions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """允许独享目录重定位；原缺陷输入、初态和执行路径必须仍被重验。"""
    witnesses = {witness.id: witness for witness in plan.witnesses}
    identities: dict[str, Any] = {}
    for subject in plan.subjects:
        if subject.role != "current" and not all_subjects:
            continue
        witness = witnesses[subject.witness_id]
        steps: list[dict[str, Any]] = []
        for step in plan.steps:
            if step.subject_id != subject.id or (
                step.kind == "acceptance"
                and step.acceptance_version not in acceptance_versions
            ):
                continue
            binding = step.binding
            locations = sorted(
                [
                    (binding.project_root, "{candidate}"),
                    *(
                        (resource.root, "{resource:" + resource.id + "}")
                        for resource in binding.resources
                    ),
                ],
                key=lambda item: len(item[0]),
                reverse=True,
            )

            def relocate(
                value: str, aliases: Sequence[tuple[str, str]] = tuple(locations)
            ) -> str:
                for location, label in aliases:
                    if value == location:
                        return label
                    if value.startswith(location.rstrip("/\\") + os.sep):
                        return label + value[len(location) :]
                return value

            resources: list[dict[str, Any]] = []
            for resource in binding.resources:
                initial = json.loads(
                    _bound_read(root, resource.initial_state, captured_artifacts)
                )
                resources.append(
                    {
                        "id": resource.id,
                        "permission_id": resource.permission_id,
                        "kind": resource.kind,
                        "initial_files": sorted(
                            (item["path"], item["sha256"]) for item in initial["files"]
                        ),
                        "endpoint": relocate(resource.observation_endpoint),
                        "cleanup_method": resource.cleanup_method,
                    }
                )
            steps.append(
                {
                    "kind": step.kind,
                    "phase": step.phase,
                    "assertion_id": step.assertion_id,
                    "cwd": binding.cwd,
                    "argv": tuple(relocate(argument) for argument in binding.argv),
                    "environment": {
                        key: relocate(value)
                        for key, value in binding.effective_environment.items()
                    },
                    "resources": sorted(resources, key=lambda item: item["id"]),
                }
            )
            if all_subjects:
                # 默认修复证明的历史形状不变；批次约束额外固定完整执行参数。
                steps[-1].update(
                    id=step.id,
                    acceptance_version=step.acceptance_version,
                    depends_on=_batch_dependencies(plan, step, acceptance_versions),
                    timeout_seconds=binding.timeout_seconds,
                    max_output_bytes=binding.max_output_bytes,
                    reservation_seconds=step.reservation_seconds,
                )
        identities[subject.id] = {
            "obligation_id": subject.obligation_id,
            "witness_sha256": witness.input_ref.sha256,
            "input_value": witness.input_value.model_dump(mode="json"),
            "premise_values": {
                key: value.model_dump(mode="json")
                for key, value in witness.premise_values.items()
            },
            "steps": steps,
        }
    return identities


def _batch_dependencies(
    plan: CounterexamplePlan, step: ExecutionStep, versions: tuple[str, ...]
) -> tuple[str, ...]:
    by_id = {item.id: item for item in plan.steps}
    result: set[str] = set()
    pending = list(step.depends_on)
    while pending:
        dependency = by_id[pending.pop()]
        if (
            dependency.kind == "acceptance"
            and dependency.acceptance_version not in versions
        ):
            pending.extend(dependency.depends_on)
        else:
            result.add(dependency.id)
    return tuple(sorted(result))


def _repair_proof_payload(
    root: Path,
    impl_input: ImplementationInput,
    previous_ref: ArtifactRef,
    previous: BoundEvidence,
    plan_ref: ArtifactRef,
    plan: CounterexamplePlan,
    assessment: CounterexampleAssessment,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    if (
        previous.assessment.current_result.status != "FAIL"
        or assessment.current_result.status != "PASS"
        or previous.plan.candidate_digest == plan.candidate_digest
        or previous.plan.task_id != plan.task_id
        or previous.plan.contract_digest != plan.contract_digest
    ):
        raise ValueError(
            "counterexample-repair-requires-actual-business-before-and-after"
        )
    replay_identity = _repair_replay_identity(root, previous.plan, captured_artifacts)
    if replay_identity != _repair_replay_identity(root, plan, captured_artifacts):
        raise ValueError("counterexample-repair-original-defect-path-not-retested")
    before = _current_file_map(root, previous.plan, captured_artifacts)
    after = _current_file_map(root, plan, captured_artifacts)
    # 全仓候选可同时包含多个冻结任务的修复；缺少任务归属时不可借总范围补权。
    if impl_input.task_scopes and plan.task_id not in impl_input.task_scopes:
        raise ValueError("counterexample-business-repair-outside-original-task")
    # 旧输入没有逐任务范围时，仅保留原当前任务语义，不推导任何兄弟任务。
    task_ids = tuple(impl_input.task_scopes) or (plan.task_id,)
    changes = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if (
            old is not None
            and new is not None
            and (old["sha256"], old["mode"]) == (new["sha256"], new["mode"])
        ):
            continue
        allowed_tasks = {
            task_id
            for task_id in task_ids
            if _original_task_allows(impl_input, task_id, path)
        }
        if path in plan.protected_paths or not allowed_tasks:
            raise ValueError("counterexample-business-repair-outside-original-task")
        # 别的任务合法改动不归入本任务证明，也不能替代本任务自己的非空修复。
        if plan.task_id in allowed_tasks:
            changes.append({"path": path, "before": old, "after": new})
    if not changes:
        raise ValueError("counterexample-business-repair-has-no-code-change")
    return {
        "schema_version": 1,
        "contract_digest": plan.contract_digest,
        "before_record_ref": previous_ref.model_dump(mode="json"),
        "after_plan_ref": plan_ref.model_dump(mode="json"),
        "before_candidate_digest": previous.plan.candidate_digest,
        "after_candidate_digest": plan.candidate_digest,
        "replay_identity_digest": counterexample_digest(replay_identity),
        "changes": changes,
        "before_observation_refs": [
            ref.model_dump(mode="json")
            for ref in previous.assessment.current_result.evidence_refs
        ],
        "after_observation_refs": [
            ref.model_dump(mode="json")
            for ref in assessment.current_result.evidence_refs
        ],
    }


def _historical_attempt_views(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    attempts: Sequence[AttemptReceipt],
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[BoundEvidence, ...]:
    """完整原回执派生业务历史；R2 已开始也不能抹去先前 final 的失败。"""
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from ai_sdlc.core.counterexample_models import BoundEvidence

    if any(
        item.status == "execution_unknown" or item.cleanup_status != "complete"
        for item in attempts
    ):
        raise ValueError("counterexample-history-assessment-needs-complete-attempts")
    by_id = {step.id: step for step in plan.steps}
    final_attempts = tuple(
        item for item in attempts if by_id[item.step_id].phase != "r2"
    )
    groups = [tuple(attempts)]
    if final_attempts and len(final_attempts) != len(attempts):
        groups.insert(0, final_attempts)
    result = []
    for group in groups:
        bundle = _bundle_from_attempts(
            root, contract, plan, group, captured_artifacts=captured_artifacts
        )
        assessment = evaluate_counterexample(contract, plan, bundle, require_r2=False)
        result.append(
            BoundEvidence(
                contract=contract,
                plan=plan,
                observations=bundle,
                assessment=assessment,
                artifact_refs=(),
                missing=(),
            )
        )
    return tuple(result)


def _derive_actual_repair(
    root: Path,
    impl_input: ImplementationInput,
    plan_ref: ArtifactRef,
    plan: CounterexamplePlan,
    assessment: CounterexampleAssessment,
) -> tuple[RepairEvidence, ...]:
    from ai_sdlc.core.counterexample_models import (
        CounterexampleEvidenceRecord,
        RepairEvidence,
    )

    if assessment.current_result.status != "PASS":
        return ()
    results = _attempts_dir(root, plan).parent / "results"
    candidates = []
    for path in results.glob("record-*.json") if results.exists() else ():
        record = CounterexampleEvidenceRecord.model_validate_json(
            read_stable_bytes(root, path)
        )
        if (
            record.task_id != plan.task_id
            or record.source_digest_after == plan.candidate_digest
        ):
            continue
        reference = _ref(root, path)
        previous = resolve_counterexample_evidence(root, impl_input, reference)
        if previous.assessment.current_result.status == "FAIL":
            candidates.append((record.recorded_at_ms, reference, previous))
    repairs = []
    covered = set()
    for _, previous_ref, previous in sorted(
        candidates, key=lambda item: (item[0], item[1].sha256)
    ):
        key = previous.assessment.current_result.evidence_refs
        if key in covered:
            continue
        payload = _repair_proof_payload(
            root, impl_input, previous_ref, previous, plan_ref, plan, assessment
        )
        repair_ref = _write_json(
            root, results / f"repair-{counterexample_digest(payload)}.json", payload
        )
        repairs.append(
            RepairEvidence(
                before_observation_refs=key,
                repair_ref=repair_ref,
                after_observation_refs=assessment.current_result.evidence_refs,
            )
        )
        covered.add(key)
    return tuple(repairs)


def resolve_counterexample_evidence(
    root: Path,
    impl_input: ImplementationInput,
    record_ref: ArtifactRef,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
    _seen: frozenset[str] = frozenset(),
) -> BoundEvidence:
    """报告、实际审查与 Close 共用原件解析，不消费自报的通过标记。"""
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from ai_sdlc.core.counterexample_models import (
        BoundEvidence,
        CounterexampleAssessment,
        CounterexampleEvidenceRecord,
        ObservationBundle,
    )
    from ai_sdlc.core.implementation_store import (
        validate_implementation_verification_contract,
    )

    contract, material = validate_implementation_verification_contract(
        root, impl_input, captured_artifacts
    )
    if contract is None:
        raise ValueError("counterexample-evidence-without-frozen-capability")
    if record_ref.path in _seen:
        raise ValueError("counterexample-repair-reference-cycle")
    seen = _seen | {record_ref.path}
    references = {
        path: ArtifactRef(path=path, sha256=hashlib.sha256(raw).hexdigest())
        for path, raw in material.items()
    }

    def read(reference: ArtifactRef) -> bytes:
        raw = _bound_read(root, reference, captured_artifacts)
        old = references.get(reference.path)
        if old is not None and old != reference:
            raise ValueError("counterexample-conflicting-content-references")
        references[reference.path] = reference
        return raw

    record = CounterexampleEvidenceRecord.model_validate_json(read(record_ref))
    if (
        record.loop_id != impl_input.loop_id
        or record.contract_ref.path != impl_input.verification_contract_ref
        or record.contract_ref.sha256 != impl_input.verification_contract_digest
        or record.source_digest_before != record.source_digest_after
    ):
        raise ValueError("counterexample-evidence-record-identity-mismatch")
    read(record.contract_ref)
    plan = CounterexamplePlan.model_validate_json(read(record.plan_ref))
    bundle = ObservationBundle.model_validate_json(read(record.observations_ref))
    saved = CounterexampleAssessment.model_validate_json(read(record.assessment_ref))
    validate_plan_contract(contract, plan)
    owners = _implementation_obligation_owners(
        root, impl_input, contract, captured_artifacts
    )
    if any(
        owners.get(subject.obligation_id) != plan.task_id for subject in plan.subjects
    ):
        raise ValueError("counterexample-plan-task-obligation-mismatch")
    if (
        plan.loop_id != record.loop_id
        or plan.task_id != record.task_id
        or plan.contract_digest != record.contract_ref.sha256
        or plan.candidate_digest != record.source_digest_before
    ):
        raise ValueError("counterexample-plan-record-identity-mismatch")
    if any(
        not _original_task_allows(impl_input, plan.task_id, path)
        for path in plan.allowed_modified_paths
    ):
        raise ValueError("counterexample-plan-outside-original-task-scope")
    for reference in validate_counterexample_snapshots(
        root, plan, captured_artifacts=captured_artifacts, require_live=False
    ):
        read(reference)
    for witness in plan.witnesses:
        _verify_witness_content(witness, read(witness.input_ref))
    for step in plan.steps:
        for resource in step.binding.resources:
            for reference in resource_initial_artifact_refs(
                root, resource.initial_state, captured_artifacts=captured_artifacts
            ):
                read(reference)
    read(plan.budget_ref)
    for receipt in bundle.attempts:
        for reference in attempt_artifact_refs(
            root, receipt.attempt_ref, captured_artifacts=captured_artifacts
        ):
            read(reference)
        actual = recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured_artifacts
        )
        if actual != receipt:
            raise ValueError("counterexample-attempt-receipt-not-derived-from-raw")
    for raw in bundle.raw_evidence:
        if read(raw.ref) != raw.content.encode("utf-8"):
            raise ValueError("counterexample-raw-observation-content-mismatch")
    derived = _bundle_from_attempts(
        root, contract, plan, bundle.attempts, captured_artifacts=captured_artifacts
    )
    if (
        derived.business != bundle.business
        or derived.acceptance != bundle.acceptance
        or derived.raw_evidence != bundle.raw_evidence
    ):
        raise ValueError("counterexample-observation-set-not-derived-from-raw")
    for repair in bundle.repairs:
        for reference in (
            *repair.before_observation_refs,
            repair.repair_ref,
            *repair.after_observation_refs,
        ):
            read(reference)
    actual_assessment = evaluate_counterexample(
        contract, plan, bundle, require_r2=_record_requires_r2(record, plan, read)
    )
    if actual_assessment != saved:
        raise ValueError("counterexample-assessment-does-not-match-actual-evidence")
    for repair in bundle.repairs:
        proof = json.loads(read(repair.repair_ref))
        if not isinstance(proof, dict):
            raise ValueError("counterexample-repair-proof-must-be-object")
        previous_ref = ArtifactRef.model_validate(proof.get("before_record_ref"))
        previous = resolve_counterexample_evidence(
            root,
            impl_input,
            previous_ref,
            captured_artifacts=captured_artifacts,
            _seen=seen,
        )
        for reference in previous.artifact_refs:
            read(reference)
        expected = _repair_proof_payload(
            root,
            impl_input,
            previous_ref,
            previous,
            record.plan_ref,
            plan,
            actual_assessment,
            captured_artifacts,
        )
        if (
            proof != expected
            or type(proof.get("schema_version")) is not int
            or repair.before_observation_refs
            != previous.assessment.current_result.evidence_refs
            or repair.after_observation_refs
            != actual_assessment.current_result.evidence_refs
        ):
            raise ValueError(
                "counterexample-repair-proof-not-derived-from-actual-change"
            )
    if captured_artifacts is None:
        for reference in references.values():
            _read_ref(root, reference)
    return BoundEvidence(
        contract=contract,
        plan=plan,
        observations=bundle,
        assessment=actual_assessment,
        artifact_refs=tuple(references.values()),
        missing=(),
    )


def _closed_counterexample_delivery_guard(
    root: Path,
    impl_input: ImplementationInput,
    captured_artifacts: Mapping[str, bytes] | None,
) -> tuple[VerifiedDeliveryCommit, tuple[tuple[str, str], ...]] | None:
    """当前关闭状态仅限定读回范围，不替代原合同、评审与 Close 的完整验收。"""
    from ai_sdlc.branch.git_client import GitError
    from ai_sdlc.core.implementation_models import (
        ImplementationClose,
        ImplementationReport,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts
    from ai_sdlc.core.loop_models import LoopRun, LoopStatus, LoopType
    from ai_sdlc.core.pr_review_service import read_verified_delivery_commit

    artifacts = implementation_artifacts(root, impl_input.loop_id)
    if not artifacts.loop_run_path.is_file():
        return None
    run_raw = read_stable_bytes(root, artifacts.loop_run_path)
    run = LoopRun.model_validate_json(run_raw)
    if run.status != LoopStatus.CLOSED:
        return None
    close_raw = read_stable_bytes(root, artifacts.close_path)
    close = ImplementationClose.model_validate_json(close_raw)
    report_raw = read_stable_bytes(root, artifacts.report_json_path)
    report = ImplementationReport.model_validate_json(report_raw)
    if (
        run.artifact_kind != "loop-run"
        or run.loop_type != LoopType.IMPLEMENTATION
        or run.loop_id != impl_input.loop_id
        or run.work_item_id != impl_input.work_item_id
        or close.artifact_kind != "implementation-close"
        or close.loop_id != impl_input.loop_id
        or close.report_path != artifacts.report_json_path.relative_to(root).as_posix()
        or report.loop_id != impl_input.loop_id
        or report.work_item_id != impl_input.work_item_id
        or close.next_loop_type
        != (
            LoopType.FRONTEND_EVIDENCE
            if report.requires_frontend_evidence
            else LoopType.LOCAL_PR_REVIEW
        )
    ):
        raise ValueError("counterexample-delivery-closed-identity-mismatch")
    try:
        proof = read_verified_delivery_commit(root)
    except GitError as exc:
        raise ValueError(f"counterexample-delivery-proof-unavailable: {exc}") from exc
    if proof.root != root.resolve():
        raise ValueError("counterexample-delivery-root-mismatch")
    # 新 PR 证明属于当前 Git 边界，不塞进闭前 R1；重叠路径仍禁止两套字节。
    if captured_artifacts is not None:
        for path, digest in proof.artifact_digests:
            if path in captured_artifacts and (
                digest is None
                or hashlib.sha256(captured_artifacts[path]).hexdigest() != digest
            ):
                raise ValueError("counterexample-delivery-captured-proof-conflict")
    return proof, tuple(
        (path.relative_to(root).as_posix(), hashlib.sha256(raw).hexdigest())
        for path, raw in (
            (artifacts.loop_run_path, run_raw),
            (artifacts.close_path, close_raw),
            (artifacts.report_json_path, report_raw),
        )
    )


def _counterexample_live_inputs_match(
    root: Path,
    plan: CounterexamplePlan,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
    allow_absent_v1: bool = False,
) -> bool:
    """候选识别可保留隔离 V1 草案；最终消费须另核验实际选中的验收。"""
    for subject in plan.subjects:
        if subject.role != "current":
            continue
        manifest = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        expected = [
            {key: item[key] for key in ("path", "sha256", "mode")}
            for item in manifest["files"]
        ]
        try:
            v1 = manifest["acceptance_sources"].get("V1")
            if (
                allow_absent_v1
                and v1 is not None
                and v1["sha256"] == plan.v1_digest
                and v1["path"] in manifest["ignored_inputs"]
                and v1["path"] != manifest["acceptance_sources"]["V0"]["path"]
                and not _path(root, v1["path"]).exists()
            ):
                target = Path(manifest["root"]) / v1["path"]
                # 按冻结命令与环境拒绝显式共享；不推断任意脚本的间接文件依赖。
                shared = any(
                    v1["path"] in value
                    or str(target) in value
                    or (
                        Path(step.binding.project_root)
                        / step.binding.cwd
                        / value.rsplit("=", 1)[-1]
                    ).resolve()
                    == target.resolve()
                    for step in plan.steps
                    if step.subject_id == subject.id
                    and not (
                        step.kind == "acceptance" and step.acceptance_version == "V1"
                    )
                    for value in (
                        *step.binding.argv,
                        *step.binding.effective_environment.values(),
                    )
                )
                if not shared:
                    expected = [item for item in expected if item["path"] != v1["path"]]
            if expected != _file_map(root, manifest["ignored_inputs"]):
                return False
        except (OSError, ValueError):
            return False
    return True


def _selected_acceptance_matches_tree(
    root: Path,
    plan: CounterexamplePlan,
    version: str,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> bool:
    """探索可使用忽略草案；最终选中验收必须是原候选树中的普通 blob。"""
    from ai_sdlc.core.quality_command import _git_bytes, quality_command_environment

    digest = getattr(plan, version.lower() + "_digest")
    if digest is None:
        return False
    current = [subject for subject in plan.subjects if subject.role == "current"]
    if not current:
        return False
    environment = quality_command_environment(os.environ)
    for subject in current:
        manifest = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        selected = manifest["acceptance_sources"].get(version.upper())
        if selected is None or selected["sha256"] != digest:
            return False
        relative = project_relative_path(selected["path"])
        # 树与 blob 按同一存储对象语义读取，避免本地替换映射改变交付证明。
        entries = _git_bytes(
            root,
            environment,
            "--no-replace-objects",
            "--literal-pathspecs",
            "ls-tree",
            "-z",
            manifest["index_tree"],
            "--",
            relative,
        ).split(b"\0")
        if len(entries) != 2 or entries[-1] != b"" or b"\t" not in entries[0]:
            return False
        metadata, path = entries[0].split(b"\t", 1)
        fields = metadata.split()
        if (
            len(fields) != 3
            or fields[0] not in (b"100644", b"100755")
            or fields[1] != b"blob"
            or path != relative.encode("utf-8")
        ):
            return False
        content = _git_bytes(
            root,
            environment,
            "--no-replace-objects",
            "cat-file",
            "blob",
            fields[2].decode("ascii"),
        )
        if hashlib.sha256(content).hexdigest() != digest:
            return False
    return True


def _counterexample_plan_matches_delivery(
    root: Path,
    plan: CounterexamplePlan,
    proof: VerifiedDeliveryCommit,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
    allow_absent_v1: bool = False,
) -> bool:
    """同一提交内容也须复验完整路径、权限、忽略输入及原候选摘要。"""
    if proof.root != root.resolve():
        raise ValueError("counterexample-delivery-root-mismatch")
    current_subjects = [
        subject for subject in plan.subjects if subject.role == "current"
    ]
    if not current_subjects:
        raise ValueError("counterexample-delivery-current-snapshot-missing")
    for subject in current_subjects:
        manifest = json.loads(_bound_read(root, subject.snapshot, captured_artifacts))
        if (
            manifest["head"] != proof.reviewed_head
            or manifest["index_tree"] != proof.staged_tree
            or manifest["source_digest"] != plan.candidate_digest
        ):
            return False
    if not _counterexample_live_inputs_match(
        root,
        plan,
        captured_artifacts=captured_artifacts,
        allow_absent_v1=allow_absent_v1,
    ):
        return False
    return plan.candidate_digest == source_digest_sha256(
        build_source_digest_at_reviewed_parent(
            root, reviewed_parent=proof.reviewed_head, reviewed_tree=proof.staged_tree
        )
    )


class _CurrentStateMaterial(Mapping[str, bytes]):
    """仅组织本次现场读取的原字节；不保存判定，也不跨状态调用存活。"""

    def __init__(self, root: Path, loop_id: str, initial: Mapping[str, bytes]):
        self.root = root
        self.base = root / f".ai-sdlc/loops/implementation/{loop_id}/counterexamples"
        self.raw = dict(initial)
        self.absent: set[str] = set()
        self.layout = self._history_layout()
        self.history_files: dict[str, bytes] = {}
        for directory, names in self.layout[1]:
            if "intent.json" not in names:
                raise ValueError(
                    "counterexample-orphan-attempt-intent-missing-no-replay"
                )
            for name in names:
                path = self.base / "attempts" / directory / name
                key = path.relative_to(root).as_posix()
                self.history_files[key] = self[key]
        for key in self.layout[3]:
            self[key]

    def _history_layout(self):
        attempts = self.base / "attempts"
        groups = []
        if attempts.exists():
            for directory in sorted(attempts.iterdir()):
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError("counterexample-attempt-history-layout-invalid")
                groups.append(
                    (directory.name, tuple(sorted(p.name for p in directory.iterdir())))
                )
        plans = self.base / "plans"
        return (
            attempts.exists(),
            tuple(groups),
            plans.exists(),
            tuple(
                sorted(
                    path.relative_to(self.root).as_posix()
                    for path in plans.glob("*.json")
                )
            ),
        )

    def __getitem__(self, key: str) -> bytes:
        if key not in self.raw and key not in self.absent:
            project_relative_path(key)
            path = self.root / key
            try:
                path.lstat()
            except FileNotFoundError:
                self.absent.add(key)
            else:
                self.raw[key] = read_stable_bytes(self.root, path)
        if key in self.absent:
            raise KeyError(key)
        return self.raw[key]

    def read_required(self, key: str) -> bytes:
        project_relative_path(key)
        try:
            return self[key]
        except KeyError:
            # 必读原件沿用现场读取的原错误；后来出现的文件不能补入本次材料。
            read_stable_bytes(self.root, self.root / key)
            raise ValueError(
                f"counterexample-original-absence-changed-during-readback: {key}"
            ) from None

    def read_reference(self, reference: ArtifactRef) -> bytes:
        raw = self.read_required(reference.path)
        if hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("counterexample-artifact-content-stale")
        return raw

    def __iter__(self):
        return iter(tuple(self.raw))

    def __len__(self) -> int:
        return len(self.raw)

    def verify_originals(self) -> None:
        # 可选原件的缺席也是本次输入；不能在读取途中出现后仍沿用缺席判断。
        for key, original in self.raw.items():
            if read_stable_bytes(self.root, self.root / key) != original:
                raise ValueError(f"counterexample-artifact-content-stale: {key}")
        for key in self.absent:
            try:
                (self.root / key).lstat()
            except FileNotFoundError:
                continue
            raise ValueError(
                f"counterexample-original-absence-changed-during-readback: {key}"
            )
        if self._history_layout() != self.layout:
            raise ValueError("counterexample-history-file-set-changed-during-readback")


def counterexample_verification_state(
    root: Path,
    impl_input: ImplementationInput,
    progress: ImplementationProgress,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
    require_r2: bool | None = None,
    require_completion: bool = True,
    active_plan_digest: str | None = None,
) -> tuple[list[str], list[str], tuple[ArtifactRef, ...]]:
    from ai_sdlc.core.counterexample_evaluation import (
        _required_observation_failures,
        aggregate_task_assessments,
        evaluate_counterexample,
    )
    from ai_sdlc.core.counterexample_models import (
        CounterexampleEvidenceRecord,
        ObservationBundle,
        required_execution_steps,
    )
    from ai_sdlc.core.implementation_store import (
        validate_implementation_verification_contract,
    )

    contract, material = validate_implementation_verification_contract(
        root, impl_input, captured_artifacts
    )
    refs = {
        path: ArtifactRef(path=path, sha256=hashlib.sha256(raw).hexdigest())
        for path, raw in material.items()
    }
    if contract is None:
        if any(task.counterexample_results for task in progress.tasks):
            return (
                ["Counterexample evidence has no frozen verification capability."],
                [],
                tuple(refs.values()),
            )
        return [], [], ()
    original_artifacts = captured_artifacts
    state_material = None
    if captured_artifacts is None:
        try:
            state_material = _CurrentStateMaterial(root, impl_input.loop_id, material)
            captured_artifacts = state_material
            refs.update(
                (path, ArtifactRef(path=path, sha256=hashlib.sha256(raw).hexdigest()))
                for path, raw in state_material.history_files.items()
            )
        except (KeyError, OSError, ValueError) as exc:
            return (
                [f"counterexample-history-incomplete: {exc}"],
                [],
                tuple(refs.values()),
            )
    required = any(o.selected and o.required for o in contract.obligations)
    blockers: list[str] = []
    advisories: list[str] = []
    records: list[tuple[int, str, BoundEvidence]] = []
    history_records: list[tuple[int, str, BoundEvidence]] = []
    history_plans: dict[str, CounterexamplePlan] = {}
    recorded_attempts: dict[str, set[str]] = {}
    completion_issues = blockers if require_completion else advisories
    if active_plan_digest is not None and require_completion:
        raise ValueError("counterexample-active-plan-only-for-execution-capture")
    try:
        owners = _implementation_obligation_owners(
            root, impl_input, contract, captured_artifacts
        )
    except (OSError, ValueError) as exc:
        return [str(exc)], [], tuple(refs.values())
    owner_task_ids = tuple(sorted(set(owners.values()) - {None}))
    required_task_ids = {
        owners[obligation.id]
        for obligation in contract.obligations
        if obligation.selected and obligation.required
    }
    if require_r2 is None:
        require_r2, review_refs = _native_counterexample_r2(
            root, impl_input, captured_artifacts
        )
        refs.update({ref.path: ref for ref in review_refs})
    try:
        current_head = _git(root, "rev-parse", "HEAD").strip()
        current_digest = source_digest_sha256(build_source_digest(root))
        if not current_head or _git(root, "rev-parse", "HEAD").strip() != current_head:
            raise ValueError("counterexample-source-head-changed-during-readback")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        return (
            [f"counterexample-source-identity-unavailable: {exc}"],
            [],
            tuple(refs.values()),
        )
    delivery_guard: (
        tuple[VerifiedDeliveryCommit, tuple[tuple[str, str], ...]] | None
    ) = None
    delivery_checked = False
    delivery_matches: dict[str, bool] = {}
    delivery_plans: dict[str, CounterexamplePlan] = {}
    snapshot_heads: dict[str, str] = {}
    live_input_matches: dict[str, tuple[CounterexamplePlan, bool]] = {}
    selected_inputs: list[tuple[CounterexamplePlan, bool, str, bool]] = []
    prefix = (
        f".ai-sdlc/loops/implementation/{impl_input.loop_id}/counterexamples/attempts/"
    )
    plan_prefix = prefix.rsplit("attempts/", 1)[0] + "plans/"
    loaded_plans: dict[str, tuple[CounterexamplePlan, str, ArtifactRef]] = {}

    def plan_paths() -> set[str]:
        if original_artifacts is not None:
            return {
                key
                for key in original_artifacts
                if key.startswith(plan_prefix) and key.endswith(".json")
            }
        return {
            path.relative_to(root).as_posix()
            for path in (root / plan_prefix).glob("*.json")
        }

    try:
        original_plan_paths = plan_paths()
    except OSError as exc:
        blockers.append(f"counterexample-original-batch-incomplete: {exc}")
        original_plan_paths = set()

    def load_history_plan(path: str) -> tuple[CounterexamplePlan, str]:
        # 一次状态读取只解析每份原计划一次；意图及回执仍逐项验证，末尾重读原字节。
        if path not in loaded_plans:
            plan, reference = _read_history_plan(root, root / path, captured_artifacts)
            if path in refs and refs[path] != reference:
                raise ValueError("counterexample-conflicting-content-references")
            digest = Path(path).stem
            loaded_plans[path] = (plan, digest, reference)
            refs[path] = reference
        plan, digest, _ = loaded_plans[path]
        return plan, digest

    def matches_live_inputs(plan: CounterexamplePlan) -> bool:
        # 未选中的隔离草案不改变母本身份；较晚未完成计划仍须进入历史核对。
        return _counterexample_live_inputs_match(
            root, plan, captured_artifacts=captured_artifacts, allow_absent_v1=True
        )

    def matches_current(plan: CounterexamplePlan, key: str) -> bool:
        nonlocal delivery_guard, delivery_checked
        if plan.candidate_digest == current_digest:
            # 同一次材料读取中，每个计划只分类一次；逐意图原件仍全部核验。
            # 正负分类都留到返回前重新读取，避免旧候选在读取途中变成当前候选。
            if key not in live_input_matches:
                live_input_matches[key] = (plan, matches_live_inputs(plan))
            return live_input_matches[key][1]
        if key not in snapshot_heads:
            # 同 HEAD 的修复历史仍须完整读原件；只有真实提交变化才进入交付证明。
            for ref in validate_counterexample_snapshots(
                root, plan, captured_artifacts=captured_artifacts, require_live=False
            ):
                refs[ref.path] = ref
            heads = {
                json.loads(_bound_read(root, subject.snapshot, captured_artifacts))[
                    "head"
                ]
                for subject in plan.subjects
                if subject.role == "current"
            }
            if len(heads) != 1:
                raise ValueError("counterexample-current-snapshot-head-conflict")
            snapshot_heads[key] = heads.pop()
        if snapshot_heads[key] == current_head:
            return False
        if not delivery_checked:
            delivery_guard = _closed_counterexample_delivery_guard(
                root, impl_input, original_artifacts
            )
            delivery_checked = True
        if delivery_guard is None:
            return False
        if key not in delivery_matches:
            delivery_matches[key] = _counterexample_plan_matches_delivery(
                root,
                plan,
                delivery_guard[0],
                captured_artifacts=captured_artifacts,
                allow_absent_v1=True,
            )
            delivery_plans[key] = plan
        return delivery_matches[key]

    for task in progress.tasks:
        for reference in task.counterexample_results:
            try:
                bound = resolve_counterexample_evidence(
                    root, impl_input, reference, captured_artifacts=captured_artifacts
                )
                refs.update({ref.path: ref for ref in bound.artifact_refs})
                record = CounterexampleEvidenceRecord.model_validate_json(
                    _bound_read(root, reference, captured_artifacts)
                )
                if record.task_id != task.task_id:
                    raise ValueError("counterexample-task-evidence-identity-mismatch")
                history_records.append((record.recorded_at_ms, reference.sha256, bound))
                plan_digest = counterexample_digest(bound.plan)
                history_plans[plan_digest] = bound.plan
                recorded_attempts.setdefault(plan_digest, set()).update(
                    attempt.attempt_id for attempt in bound.observations.attempts
                )
                if not Path(reference.path).name.startswith(
                    "record-reconciled-"
                ) and matches_current(bound.plan, plan_digest):
                    records.append((record.recorded_at_ms, reference.sha256, bound))
                    if bound.assessment.current_result.status == "FAIL":
                        completion_issues.append(
                            "Counterexample business failure on the current source has not been repaired."
                        )
            except (OSError, ValueError) as exc:
                blockers.append(str(exc))
    # 即使某次中断尚未写 progress，旧意图及成本也不能从 Close 材料中消失。
    try:
        attempt_entries = _attempt_history(
            root, root / prefix, captured_artifacts, refs
        )
        keys = [path.relative_to(root).as_posix() for path, _ in attempt_entries]
    except (OSError, ValueError) as exc:
        blockers.append(str(exc))
        keys = []
    active_plans: dict[str, tuple[CounterexamplePlan, list[AttemptReceipt]]] = {}
    resource_cycles = []
    for key in sorted(keys):
        try:
            raw = (
                captured_artifacts[key]
                if captured_artifacts is not None
                else read_stable_bytes(root, root / key)
            )
            intent = _validated_attempt_intent(json.loads(raw))
            reference = ArtifactRef(path=key, sha256=hashlib.sha256(raw).hexdigest())
            plan_key = (
                prefix.rsplit("attempts/", 1)[0] + f"plans/{intent['plan_digest']}.json"
            )
            plan, plan_digest = load_history_plan(plan_key)
            history_plans[plan_digest] = plan
            if (
                plan_digest != intent["plan_digest"]
                or plan.contract_digest != impl_input.verification_contract_digest
            ):
                raise ValueError("counterexample-history-contract-or-plan-conflict")
            refs.update(
                {
                    ref.path: ref
                    for ref in attempt_artifact_refs(
                        root, reference, captured_artifacts=captured_artifacts
                    )
                }
            )
            recovered = recover_counterexample_attempt(
                root, plan, reference, captured_artifacts=captured_artifacts
            )
            step = next(item for item in plan.steps if item.id == recovered.step_id)
            if (
                intent.get("kind") != step.kind
                or intent.get("phase") != step.phase
                or intent.get("binding") != step.binding.model_dump(mode="json")
            ):
                raise ValueError("counterexample-history-step-binding-conflict")
            resource_cycles.append(
                (intent["attempt_ordinal"], plan_digest, plan, step, recovered, intent)
            )
            if matches_current(plan, plan_digest):
                active_plans.setdefault(plan_digest, (plan, []))[1].append(recovered)
            if (
                recovered.status == "execution_unknown"
                or recovered.cleanup_status != "complete"
            ):
                blockers.append("counterexample-prior-execution-unknown-no-replay")
        except (KeyError, TypeError, OSError, ValueError) as exc:
            blockers.append(f"counterexample-history-incomplete: {exc}")
    # 未启动原表也参与选表，不能让旧 PASS 覆盖当前未完成或次序不明的后继。
    for key in sorted(original_plan_paths):
        try:
            prior, prior_digest = load_history_plan(key)
            history_plans[prior_digest] = prior
        except (KeyError, OSError, ValueError) as exc:
            blockers.append(f"counterexample-original-batch-incomplete: {exc}")
    selected_plans: dict[str, CounterexamplePlan] = {}
    try:
        current_plans = {
            digest: plan
            for digest, plan in history_plans.items()
            if matches_current(plan, digest)
        }
        # 原执行入口可准入尚未写首个意图的明确后继；默认读回仍拒绝零意图歧义。
        # 已有意图的历史 cleanup 不借此复活旧业务选择。
        starting_plan = (
            current_plans.get(active_plan_digest)
            if active_plan_digest is not None
            and not any(row[1] == active_plan_digest for row in resource_cycles)
            else None
        )
        selected_plans = _selected_task_plans(
            root,
            current_plans,
            [(intent, plan) for _, _, plan, _, _, intent in resource_cycles],
            current_plan=starting_plan,
            captured_artifacts=captured_artifacts,
        )
    except (KeyError, OSError, ValueError) as exc:
        blockers.append(f"counterexample-current-plan-selection-incomplete: {exc}")
    selected_by_task = {plan.task_id: plan for plan in selected_plans.values()}
    latest_by_task: dict[str, BoundEvidence] = {}
    for task_id in owner_task_ids:
        task_records = [
            item
            for item in records
            if item[2].plan.task_id == task_id
            and counterexample_digest(item[2].plan) in selected_plans
        ]
        if not task_records:
            (completion_issues if task_id in required_task_ids else advisories).append(
                "Counterexample evidence for the current source is missing or stale."
                if len(owner_task_ids) == 1
                else f"Counterexample evidence for task {task_id} on the current source is missing or stale."
            )
        else:
            # 时间仅区分同一已选表的汇总，不能改变原业务意图决定的表归属。
            latest_by_task[task_id] = max(
                task_records, key=lambda item: (item[0], item[1])
            )[2]
    resource_debts = {}
    debt_intents = {}
    cycle_receipts: dict[str, list[AttemptReceipt]] = {}
    for _, plan_digest, plan, step, recovered, intent in sorted(
        resource_cycles, key=lambda item: item[0]
    ):
        identity = (plan_digest, step.subject_id)
        cycle_receipts.setdefault(identity[0], []).append(recovered)
        resources = {item.id: item for item in step.binding.resources}
        if _resource_cleanup_completed(recovered, step):
            if identity in resource_debts and resource_debts[identity] != resources:
                blockers.append(
                    "counterexample-historical-resource-cleanup-binding-conflict"
                )
            else:
                resource_debts.pop(identity, None)
                debt_intents.pop(identity, None)
        else:
            resource_debts[identity] = resources
            debt_intents[identity] = (plan, intent)
    for identity, resources in resource_debts.items():
        if identity[0] != active_plan_digest:
            blockers.append("counterexample-historical-resource-cleanup-required")
            continue
        try:
            plan, intent = debt_intents[identity]
            cleanup = _historical_cleanup_step(
                plan, intent, cycle_receipts[identity[0]]
            )
            if {item.id: item for item in cleanup.binding.resources} != resources:
                raise ValueError(
                    "counterexample-active-resource-cleanup-binding-conflict"
                )
            if any(
                receipt.status == "execution_unknown"
                or receipt.cleanup_status != "complete"
                for receipt in cycle_receipts[identity[0]]
            ):
                raise ValueError("counterexample-prior-execution-unknown-no-replay")
            advisories.append(
                "counterexample-active-resource-cycle-awaits-frozen-cleanup"
            )
        except (KeyError, ValueError) as exc:
            blockers.append(str(exc))
    for plan_digest, attempts in cycle_receipts.items():
        if {attempt.attempt_id for attempt in attempts} <= recorded_attempts.get(
            plan_digest, set()
        ):
            continue
        try:
            plan = history_plans[plan_digest]
            # receipt 证明原字节和进程收尾；未归档的已发生观察还须实际解析和绑定。
            bundle = _bundle_from_attempts(
                root, contract, plan, attempts, captured_artifacts=captured_artifacts
            )
            executed_ids = {attempt.step_id for attempt in attempts}
            failures = _required_observation_failures(
                contract,
                plan,
                bundle,
                tuple(step for step in plan.steps if step.id in executed_ids),
                plan_digest=plan_digest,
                binding_digests={
                    step.id: counterexample_digest(step.binding) for step in plan.steps
                },
            )
            if failures:
                # 已核实原件的失败仍未完成验收，但不阻断当前冻结计划的后续动作。
                observation_issues = (
                    completion_issues if plan_digest == active_plan_digest else blockers
                )
                observation_issues.append(
                    "counterexample-unrecorded-observation-invalid: "
                    + ", ".join(failures)
                )
        except (KeyError, OSError, ValueError) as exc:
            blockers.append(f"counterexample-unrecorded-observation-incomplete: {exc}")
    for plan_digest, attempts in cycle_receipts.items():
        try:
            for bound in _historical_attempt_views(
                root,
                contract,
                history_plans[plan_digest],
                attempts,
                captured_artifacts=captured_artifacts,
            ):
                history_records.append(
                    (
                        max(
                            (item.ended_at_ms or item.started_at_ms)
                            for item in bound.observations.attempts
                        ),
                        counterexample_digest(bound.observations),
                        bound,
                    )
                )
        except (KeyError, OSError, ValueError) as exc:
            blockers.append(f"counterexample-unrecorded-history-incomplete: {exc}")
    for _, _, historical in history_records:
        latest = latest_by_task.get(historical.plan.task_id)
        if latest is None and any(
            control.v0 == "false_rejection" or control.v1 == "false_rejection"
            for control in historical.assessment.positive_controls
        ):
            # 缺少当前完整结果不能隐藏已由原件证明的误拒；此处不选择验收版本。
            completion_issues.append(
                "A legitimate control was rejected by historical acceptance and has no completed current evidence."
            )
        if historical.assessment.current_result.status != "FAIL":
            continue
        repaired = {
            reference
            for repair in (latest.observations.repairs if latest is not None else ())
            for reference in repair.before_observation_refs
        }
        if not set(historical.assessment.current_result.evidence_refs) <= repaired:
            completion_issues.append(
                "counterexample-historical-business-failure-needs-repair"
            )
    if latest_by_task:
        try:
            evaluated = {
                task_id: evaluate_counterexample(
                    bound.contract,
                    bound.plan,
                    bound.observations,
                    require_r2=require_r2,
                )
                for task_id, bound in latest_by_task.items()
            }
            disposition, complete, reasons = aggregate_task_assessments(
                contract,
                [
                    (bound.plan, evaluated[task_id])
                    for task_id, bound in latest_by_task.items()
                ],
                task_ids=owner_task_ids,
            )
            chosen = "v1" if disposition == "adopt_v1" else "v0"
            for task_id, latest in latest_by_task.items():
                assessment = evaluated[task_id]
                if any(
                    reason
                    in {
                        "attempt-history-incomplete-or-conflicting",
                        "attempt-observation-evidence-incomplete-or-conflicting",
                    }
                    for reason in assessment.reasons
                ):
                    blockers.append(
                        f"counterexample-task-attempt-evidence-incomplete:{task_id}"
                    )
                chosen_digest = getattr(latest.plan, chosen + "_digest")
                allow_absent_v1 = chosen == "v0" and complete
                tree_matches = _selected_acceptance_matches_tree(
                    root, latest.plan, chosen, captured_artifacts
                )
                selected_inputs.append(
                    (latest.plan, allow_absent_v1, chosen, tree_matches)
                )
                if not tree_matches:
                    completion_issues.append(
                        f"counterexample-selected-acceptance-not-in-reviewed-tree:{task_id}"
                    )
                if not _counterexample_live_inputs_match(
                    root,
                    latest.plan,
                    captured_artifacts=captured_artifacts,
                    allow_absent_v1=allow_absent_v1,
                ):
                    completion_issues.append(
                        f"Counterexample selected acceptance or shared input for task {task_id} is missing or stale."
                    )
                task_history = [
                    item for item in history_records if item[2].plan.task_id == task_id
                ]
                if _unresolved_control_rejection(
                    task_history, latest, assessment, chosen, chosen_digest
                ):
                    completion_issues.append(
                        "A legitimate control was rejected by the selected acceptance; a new source requires a complete passing replay of that original control."
                    )
                if assessment.current_result.status == "FAIL":
                    completion_issues.append(
                        "Current business observation failed; repair within the original task before closing."
                    )
                advisories.append(
                    f"Counterexample task={task_id}; business={assessment.current_result.status}; acceptance={disposition}."
                )
            if not complete:
                (completion_issues if required else advisories).extend(reasons)
            advisories.append(
                f"Selected obligations={sum(o.selected for o in contract.obligations)}/{len(contract.obligations)}; unselected obligations remain under ordinary verification."
            )
        except ValueError as exc:
            blockers.append(str(exc))
    for plan_digest, (plan, attempts) in active_plans.items():
        try:
            validate_plan_contract(contract, plan)
            bundle = ObservationBundle(
                business=(),
                acceptance=(),
                attempts=tuple(attempts),
                raw_evidence=(),
                repairs=(),
            )
            required_steps = required_execution_steps(
                plan, bundle, require_r2=require_r2
            )
            completed_ids = {
                attempt.step_id
                for attempt in attempts
                if not _failed_operation(
                    attempt, next(step for step in plan.steps if step.id == attempt.step_id)
                )
            }
            required_subject_ids = {
                subject.id
                for subject in plan.subjects
                if any(
                    obligation.id == subject.obligation_id
                    and obligation.selected
                    and obligation.required
                    for obligation in contract.obligations
                )
            }
            missing_steps = [
                step for step in required_steps if step.id not in completed_ids
            ]
            # 被已准入后继取代的旧表保留原件与失败，不要求取消的业务重新执行。
            if missing_steps and plan_digest in selected_plans:
                issues = (
                    completion_issues
                    if any(
                        step.subject_id in required_subject_ids
                        for step in missing_steps
                    )
                    else advisories
                )
                issues.append("counterexample-current-plan-execution-table-incomplete")
            actual_ids = {attempt.attempt_id for attempt in attempts}
            recorded_ids = recorded_attempts.get(plan_digest, set())
            # 后继表承担当前归集义务；旧表仍不能引用没有真实原件的 attempt。
            if recorded_ids - actual_ids or (
                plan_digest in selected_plans and actual_ids - recorded_ids
            ):
                issues = (
                    completion_issues
                    if recorded_ids - actual_ids
                    or any(
                        attempt.attempt_id not in recorded_ids
                        and attempt.subject_id in required_subject_ids
                        for attempt in attempts
                    )
                    else advisories
                )
                issues.append("counterexample-current-plan-attempts-not-reconciled")
        except ValueError as exc:
            blockers.append(f"counterexample-current-plan-incomplete: {exc}")
    if records or history_plans or original_plan_paths:
        batch_anchor = next(iter(selected_plans.values()), None)
        # 首次业务尚无结果记录；冻结原表及其依赖仍须进入实际裁剪的捕获集合。
        for prior in history_plans.values():
            try:
                validate_plan_contract(contract, prior)
                for witness in prior.witnesses:
                    _verify_witness_content(
                        witness,
                        _bound_read(root, witness.input_ref, captured_artifacts),
                    )
                    if (
                        witness.input_ref.path in refs
                        and refs[witness.input_ref.path] != witness.input_ref
                    ):
                        raise ValueError(
                            "counterexample-conflicting-content-references"
                        )
                    refs[witness.input_ref.path] = witness.input_ref
                if any(
                    owners.get(subject.obligation_id) != prior.task_id
                    for subject in prior.subjects
                ):
                    raise ValueError("counterexample-history-task-obligation-mismatch")
                for reference in validate_counterexample_snapshots(
                    root,
                    prior,
                    captured_artifacts=captured_artifacts,
                    require_live=False,
                ):
                    refs[reference.path] = reference
                for step in prior.steps:
                    for resource in step.binding.resources:
                        for reference in resource_initial_artifact_refs(
                            root,
                            resource.initial_state,
                            captured_artifacts=captured_artifacts,
                        ):
                            refs[reference.path] = reference
                if batch_anchor is not None:
                    owner = selected_by_task.get(prior.task_id)
                    _require_original_batch(
                        root,
                        prior,
                        owner if owner is not None else batch_anchor,
                        captured_artifacts,
                    )
            except (OSError, ValueError) as exc:
                blockers.append(f"counterexample-original-batch-incomplete: {exc}")
        if (
            len(
                {
                    plan.v1_digest
                    for plan in history_plans.values()
                    if plan.v1_digest is not None
                }
            )
            > 1
        ):
            blockers.append("counterexample-reinforcement-batch-exhausted")
    try:
        for plan, digest, reference in loaded_plans.values():
            _bound_read(root, reference, original_artifacts)
            if counterexample_digest(plan) != digest:
                raise ValueError("counterexample-history-plan-changed-during-readback")
        if plan_paths() != original_plan_paths:
            raise ValueError("counterexample-history-plan-set-changed-during-readback")
    except (KeyError, OSError, ValueError) as exc:
        blockers.append(f"counterexample-original-batch-incomplete: {exc}")
    if state_material is not None:
        try:
            state_material.verify_originals()
        except (KeyError, OSError, ValueError) as exc:
            blockers.append(f"counterexample-original-material-changed: {exc}")
    if delivery_guard is not None:
        try:
            # PR 的 Git 边界不负责 CE 声明的忽略输入；返回前须再读完整文件图。
            for key, plan in delivery_plans.items():
                if delivery_matches[key] != _counterexample_plan_matches_delivery(
                    root,
                    plan,
                    delivery_guard[0],
                    captured_artifacts=captured_artifacts,
                    allow_absent_v1=True,
                ):
                    raise ValueError(
                        "counterexample-delivery-source-changed-during-readback"
                    )
            if delivery_guard != _closed_counterexample_delivery_guard(
                root, impl_input, original_artifacts
            ):
                raise ValueError("counterexample-delivery-changed-during-readback")
        except (OSError, ValueError) as exc:
            blockers.append(str(exc))
    try:
        # 历史分流也受整次读取窗口保护，不能把读途中提交变化吞成旧候选。
        if _git(root, "rev-parse", "HEAD").strip() != current_head:
            raise ValueError("counterexample-source-head-changed-during-readback")
        if any(
            matches_live_inputs(plan) != matched
            for plan, matched in live_input_matches.values()
        ):
            raise ValueError("counterexample-current-input-changed-during-readback")
        if any(
            not _counterexample_live_inputs_match(
                root,
                plan,
                captured_artifacts=captured_artifacts,
                allow_absent_v1=allow_absent_v1,
            )
            for plan, allow_absent_v1, _, _ in selected_inputs
        ):
            raise ValueError("counterexample-selected-input-changed-during-readback")
        if any(
            _selected_acceptance_matches_tree(root, plan, version, captured_artifacts)
            != matched
            for plan, _, version, matched in selected_inputs
        ):
            raise ValueError("counterexample-selected-tree-changed-during-readback")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        blockers.append(f"counterexample-source-identity-unavailable: {exc}")
    try:
        # 完整原件读回结束后仍须核对 index；文件图只证明工作文件及忽略输入。
        if source_digest_sha256(build_source_digest(root)) != current_digest:
            raise ValueError("counterexample-source-changed-during-readback")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        blockers.append(f"counterexample-source-identity-unavailable: {exc}")
    return (
        list(dict.fromkeys(blockers)),
        list(dict.fromkeys(advisories)),
        tuple(refs.values()),
    )


def _unresolved_control_rejection(
    history: Sequence[tuple[int, str, BoundEvidence]],
    latest: BoundEvidence,
    assessment: CounterexampleAssessment,
    chosen: str,
    chosen_digest: str | None,
) -> bool:
    for _, _, bound in history:
        if bound.plan.task_id != latest.plan.task_id:
            continue
        if getattr(bound.plan, chosen + "_digest") != chosen_digest:
            continue
        for group, control in (
            (role_group, item)
            for role_group in ("current_subjects", "positive_controls")
            for item in getattr(bound.assessment, role_group, ())
        ):
            # false_rejection 已由精确绑定的合法业务读回证明；后续缺失不能抹掉它。
            if getattr(control, chosen) != "false_rejection":
                continue
            # 同候选旧失败不能被后一次成功覆盖；换候选也不能仅凭摘要清除它。
            if bound.plan.candidate_digest == latest.plan.candidate_digest:
                return True
            replacement = next(
                (
                    item
                    for item in getattr(assessment, group, ())
                    if item.subject_id == control.subject_id
                    and item.obligation_id == control.obligation_id
                ),
                None,
            )
            if (
                not assessment.required_complete
                or any(
                    reason
                    in {
                        "required-execution-step-incomplete",
                        "optional-execution-step-incomplete",
                        "required-observation-step-incomplete",
                        "optional-observation-step-incomplete",
                        "attempt-history-incomplete-or-conflicting",
                        "attempt-observation-evidence-incomplete-or-conflicting",
                    }
                    for reason in assessment.reasons
                )
                or replacement is None
                or replacement.business.status != "PASS"
                or getattr(replacement, chosen) != "accepted"
            ):
                return True
    return False


def _recover_historical_resources(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    *,
    deadline_ms: int,
    postcheck: Callable[[], None],
) -> None:
    """新业务前只执行旧计划的原收尾；不续跑旧业务或改写未执行步骤。"""
    history, debts = _historical_resource_cycles(root, plan)
    pending = _shared_pending_steps(
        root, contract, plan, history, debts, replace_current=False
    )
    recoveries = []
    for (digest, _), (old, intent) in sorted(
        debts.items(), key=lambda item: item[1][1]["attempt_ordinal"]
    ):
        if digest == counterexample_digest(plan):
            continue
        validate_plan_contract(contract, old)
        _require_original_batch(root, old, plan)
        old_receipts = [
            receipt
            for _, _, saved, receipt in history
            if counterexample_digest(saved) == digest
        ]
        cleanup = _historical_cleanup_step(old, intent, old_receipts)
        _validate_cleanup_subject_live(root, old, cleanup)
        previous = next(
            (item for item in old_receipts if item.attempt_id == intent["attempt_id"]),
            None,
        )
        ownership_ref = (
            next(
                (
                    ref
                    for ref in previous.raw_evidence_refs
                    if ref.path.endswith("/resource-ownership.json")
                ),
                None,
            )
            if previous is not None
            else None
        )
        if previous is None or ownership_ref is None:
            raise ValueError("counterexample-resource-owner-original-missing")
        ownership_refs, _ = _resource_ownership_refs(
            root, old, intent, previous.attempt_ref, _read_ref(root, ownership_ref)
        )
        _validate_resources(
            root,
            contract,
            old,
            cleanup,
            claim=False,
            deadline_ms=deadline_ms,
            ownership_refs=ownership_refs,
        )
        recoveries.append((old, cleanup))
    if len(history) + len(pending) > plan.max_execution_attempts:
        raise ValueError("counterexample-attempt-budget-required-steps-unavailable")
    if history:
        deadline_ms = min(
            deadline_ms, *(intent["deadline_ms"] for _, intent, _, _ in history)
        )
    for index, (old, cleanup) in enumerate(recoveries):
        recovered_keys = {
            (counterexample_digest(saved), step.id)
            for saved, step in recoveries[: index + 1]
        }
        remaining_steps = [
            step
            for saved, step in pending
            if (counterexample_digest(saved), step.id) not in recovered_keys
        ]
        reserve = plan.required_reserve_seconds + sum(
            item.binding.timeout_seconds + item.reservation_seconds + 5
            for item in remaining_steps
        )
        postcheck()
        receipt = execute_counterexample_attempt(
            root,
            contract,
            old,
            cleanup.id,
            deadline_ms=deadline_ms,
            remaining_reserve_seconds=reserve,
            postcheck=postcheck,
            cleanup_recovery=True,
            reserved_attempts=len(remaining_steps),
        )
        if _failed_operation(receipt, cleanup) or receipt.cleanup_status != "complete":
            raise ValueError("counterexample-historical-resource-cleanup-incomplete")
    _, remaining = _historical_resource_cycles(root, plan)
    if any(digest != counterexample_digest(plan) for digest, _ in remaining):
        raise ValueError("counterexample-historical-resource-cleanup-required")


def counterexample_execution_started(root: Path, loop_id: str) -> bool:
    """旧 pending 从同 Loop 的完整原尝试确认已消耗成本，不重建整份历史。"""
    from ai_sdlc.core.implementation_store import (
        implementation_artifacts,
        read_input,
        validate_implementation_verification_contract,
    )

    artifacts = implementation_artifacts(root, loop_id)
    folder = artifacts.loop_dir / "counterexamples" / "attempts"
    if not folder.exists():
        return False
    paths = sorted(folder.glob("*/intent.json"))
    if not paths:
        return False
    impl_input = read_input(artifacts.input_path)
    if impl_input.loop_id != loop_id:
        raise ValueError("counterexample-progress-identity-mismatch")
    if impl_input.verification_capability != VERIFICATION_CAPABILITY:
        return False
    contract, _ = validate_implementation_verification_contract(root, impl_input)
    if contract is None:
        raise ValueError("counterexample-verification-capability-not-frozen")
    owners = _implementation_obligation_owners(root, impl_input, contract)
    for path in paths:
        intent = _validated_attempt_intent(json.loads(read_stable_bytes(root, path)))
        digest = intent.get("plan_digest")
        plan_ref = ArtifactRef(
            path=(folder.parent / "plans" / f"{digest}.json")
            .relative_to(root)
            .as_posix(),
            sha256=digest,
        )
        plan = CounterexamplePlan.model_validate_json(
            read_stable_bytes(root, root / plan_ref.path)
        )
        if (
            counterexample_digest(plan) != digest
            or plan.loop_id != loop_id
            or plan.work_item_id != impl_input.work_item_id
            or plan.contract_digest != impl_input.verification_contract_digest
            or any(
                owners.get(subject.obligation_id) != plan.task_id
                for subject in plan.subjects
            )
            or any(
                not _original_task_allows(impl_input, plan.task_id, allowed)
                for allowed in plan.allowed_modified_paths
            )
        ):
            raise ValueError("counterexample-started-history-identity-mismatch")
        validate_plan_contract(contract, plan)
        step = next(
            (item for item in plan.steps if item.id == intent.get("step_id")), None
        )
        if step is None:
            raise ValueError("counterexample-started-history-step-mismatch")
        subject = next(item for item in plan.subjects if item.id == step.subject_id)
        expected = {
            "attempt_id": path.parent.name,
            "plan_digest": digest,
            "contract_digest": plan.contract_digest,
            "candidate_digest": subject.candidate_digest,
            "subject_id": step.subject_id,
            "kind": step.kind,
            "phase": step.phase,
            "binding": step.binding.model_dump(mode="json"),
            "binding_digest": counterexample_digest(step.binding),
            "max_execution_attempts": plan.max_execution_attempts,
        }
        if any(intent.get(key) != value for key, value in expected.items()):
            raise ValueError("counterexample-started-history-binding-mismatch")
        receipt = recover_counterexample_attempt(root, plan, _ref(root, path))
        if (
            receipt.status == "execution_unknown"
            or receipt.cleanup_status != "complete"
        ):
            raise ValueError("counterexample-started-history-unproven")
        # 与普通 quality_results 一致：完整的 never_started 失败也已消耗尝试成本。
        # 这不声称业务执行或通过；未知证明仍拒绝，后续原历史门禁继续生效。
        return True
    return False


def _record_counterexample_execution_started(
    root: Path, impl_input: ImplementationInput, task_id: str
) -> None:
    from ai_sdlc.core.implementation_store import (
        implementation_artifacts,
        read_progress,
    )
    from ai_sdlc.core.loop_models import utc_now_iso

    path = implementation_artifacts(root, impl_input.loop_id).progress_path
    progress = read_progress(path)
    if (progress.loop_id, progress.work_item_id) != (
        impl_input.loop_id,
        impl_input.work_item_id,
    ):
        raise ValueError("counterexample-progress-identity-mismatch")
    task = next((item for item in progress.tasks if item.task_id == task_id), None)
    if task is None:
        raise ValueError("counterexample-progress-task-missing")
    if task.status != "pending":
        return
    task.status = "in_progress"
    task.updated_at = utc_now_iso()
    LoopArtifactStore(root).write_json_artifact(path, progress)


def run_counterexample_plan(
    root: Path,
    impl_input: ImplementationInput,
    loop_run: LoopRun,
    task_id: str,
    plan_path: str,
    *,
    postcheck: Callable[[], None] | None = None,
) -> tuple[ArtifactRef, CounterexampleAssessment]:
    """调用方持原互斥；同计划重读和新计划都保留原累计成本。"""
    from fractions import Fraction

    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from ai_sdlc.core.counterexample_models import (
        CounterexampleEvidenceRecord,
        required_execution_steps,
    )
    from ai_sdlc.core.implementation_store import (
        validate_implementation_verification_contract,
    )
    from ai_sdlc.core.loop_decision_service import validate_implementation_context

    contract, _ = validate_implementation_verification_contract(root, impl_input)
    if (
        contract is None
        or impl_input.verification_contract_ref is None
        or impl_input.verification_contract_digest is None
    ):
        raise ValueError("counterexample-verification-capability-not-frozen")
    plan_bytes = read_stable_bytes(root, _path(root, plan_path))
    plan = CounterexamplePlan.model_validate_json(plan_bytes)
    validate_plan_contract(contract, plan)
    owners = _implementation_obligation_owners(root, impl_input, contract)
    if any(owners.get(subject.obligation_id) != task_id for subject in plan.subjects):
        raise ValueError("counterexample-plan-task-obligation-mismatch")
    _validate_plan_resource_roots(plan)
    for planned_step in plan.steps:
        validate_quality_argv(planned_step.binding.argv)
    if (
        plan.loop_id != loop_run.loop_id
        or plan.task_id != task_id
        or plan.contract_digest != impl_input.verification_contract_digest
    ):
        raise ValueError("counterexample-plan-loop-task-or-contract-mismatch")
    if any(
        not _original_task_allows(impl_input, task_id, path)
        for path in plan.allowed_modified_paths
    ):
        raise ValueError("counterexample-plan-outside-original-task-scope")
    before = source_digest_sha256(build_source_digest(root))
    if plan.candidate_digest != before:
        raise ValueError("counterexample-current-candidate-stale")
    context = validate_implementation_context(
        root, loop_run, impl_input, purpose="verification"
    )
    if context is None or context.capability != "stage-simulation-v1":
        raise ValueError("counterexample-original-stage-budget-required")
    deadline_ms = context.started_at_ms + int(
        Fraction(context.plan.time_plan.window_seconds) * 1000
    )
    def current_boundary() -> None:
        validate_implementation_context(root, loop_run, impl_input)
        if source_digest_sha256(build_source_digest(root)) != before:
            raise ValueError("counterexample-mother-candidate-changed-during-execution")
        if postcheck is not None:
            postcheck()

    def execution_admission(plan_digest: str) -> None:
        from ai_sdlc.cli.loop_review_cmd import _counterexample_execution_capture

        with _counterexample_execution_capture(plan_digest):
            validate_implementation_context(
                root, loop_run, impl_input, purpose="execute"
            )

    from ai_sdlc.cli.loop_review_cmd import _counterexample_execution_capture

    phase_capture: dict[str, Any] = {}
    with _counterexample_execution_capture(counterexample_digest(plan)):
        require_r2, _ = _native_counterexample_r2(
            root, impl_input, phase_capture=phase_capture
        )
    history, existing_debts = _historical_resource_cycles(root, plan)
    receipts = [
        receipt for _, _, saved, receipt in history
        if counterexample_digest(saved) == counterexample_digest(plan)
    ]
    has_old_debts = any(
        digest != counterexample_digest(plan) for digest, _ in existing_debts
    )
    if not receipts:
        unfinished = _unfinished_predecessor_plans(root, plan, history, impl_input=impl_input)
        if unfinished or has_old_debts:
            # 保全原启动资源：只清理原冻结表，随后拒绝后继，绝不封存或启动新业务。
            if has_old_debts:
                old_plans = {digest for digest, _ in existing_debts if digest != counterexample_digest(plan)}
                if len(old_plans) != 1:
                    raise ValueError("counterexample-multiple-historical-cleanup-plans")
                execution_admission(next(iter(old_plans)))
                _recover_historical_resources(root, contract, plan, deadline_ms=deadline_ms, postcheck=current_boundary)
            raise ValueError("counterexample-unfinished-predecessor-no-takeover")
    # 完整旧计划先核验捕获原件；临时副本清理后也可读取，不重新取得执行许可。
    output_dir = _attempts_dir(root, plan).parent / "results"
    completed_records = []
    for path in output_dir.glob("record-*.json") if output_dir.exists() else ():
        if path.name.startswith("record-reconciled-"):
            continue
        record = CounterexampleEvidenceRecord.model_validate_json(
            read_stable_bytes(root, path)
        )
        if record.plan_ref.sha256 == hashlib.sha256(_json_bytes(plan)).hexdigest():
            completed_records.append((record.recorded_at_ms, path))
    for _, path in sorted(completed_records, reverse=True):
        reference = _ref(root, path)
        bound = resolve_counterexample_evidence(root, impl_input, reference)
        expected_steps = required_execution_steps(
            plan, bound.observations, require_r2=require_r2
        )
        completed_steps = {
            attempt.step_id
            for attempt in bound.observations.attempts
            if not _failed_operation(
                attempt, next(step for step in plan.steps if step.id == attempt.step_id)
            )
        }
        if (
            not has_old_debts
            and {attempt.attempt_id for attempt in receipts}
            == {attempt.attempt_id for attempt in bound.observations.attempts}
            and all(step.id in completed_steps for step in expected_steps)
        ):
            assessment = evaluate_counterexample(
                contract, plan, bound.observations, require_r2=require_r2
            )
            return reference, assessment
    # 输入/快照/初态先验失败不占用副作用尝试，也不留下半份候选许可。
    # 已结束或因原失败而放弃的步骤不再依赖现场目录；成功依赖仍另行判断。
    pending_step_ids = {step.id for step in _pending_attempt_steps(plan, receipts)}
    for step in plan.steps:
        _validate_step_bindings(contract, plan, step)
        if step.id in pending_step_ids:
            _validate_current_step_cwd(root, plan, step, allow_planned_reset=True)
    validate_counterexample_snapshots(root, plan)
    for witness in plan.witnesses:
        _verify_witness_content(witness, _read_ref(root, witness.input_ref))
    _validate_plan_resource_admission(
        root, plan, new_execution=not receipts, deadline_ms=deadline_ms
    )
    plan_folder, previous_plans = _plan_history(root, plan)
    for previous in previous_plans:
        validate_plan_contract(contract, previous)
        if any(
            owners.get(subject.obligation_id) != previous.task_id
            for subject in previous.subjects
        ):
            raise ValueError("counterexample-history-task-obligation-mismatch")
    # 旧完整记录在上文严格只读返回；新执行必须预声明 R1 可能要求的完整 R2 分支。
    # 不补写旧 final-only 批表，也不改变纯模型和历史证据的校验合同。
    if not any(step.phase == "r2" for step in plan.steps):
        raise ValueError("counterexample-required-r2-execution-table-missing")
    history, debts = _historical_resource_cycles(root, plan)
    pending = _shared_pending_steps(root, contract, plan, history, debts)
    if len(history) + len(pending) > plan.max_execution_attempts:
        raise ValueError("counterexample-attempt-budget-required-steps-unavailable")
    if history:
        deadline_ms = min(
            deadline_ms, *(intent["deadline_ms"] for _, intent, _, _ in history)
        )
    # 首次封存前即支付原共享承诺；每个实际动作仍保留原临执行预算复核。
    reserve = max(
        plan.required_reserve_seconds,
        max((saved.required_reserve_seconds for saved, _ in pending), default=0),
    ) + sum(
        step.binding.timeout_seconds + step.reservation_seconds + 5
        for _, step in pending
    )
    if time.time_ns() // 1_000_000 + 1000 * reserve > deadline_ms:
        raise ValueError("counterexample-time-budget-reserve-unavailable")
    plan_ref = _write_json(
        root, plan_folder / f"{counterexample_digest(plan)}.json", plan
    )
    folder = _attempts_dir(root, plan)
    bundle = _bundle_from_attempts(root, contract, plan, receipts)
    steps = required_execution_steps(plan, bundle, require_r2=require_r2)
    done_ids = {receipt.step_id for receipt in receipts}
    by_id = {step.id: step for step in plan.steps}
    blocked_steps = _blocked_business_steps(plan, receipts)
    failed_subjects = {by_id[identifier].subject_id for identifier in blocked_steps}

    def record_started() -> None:
        _record_counterexample_execution_started(root, impl_input, task_id)

    if receipts and all(
        receipt.status != "execution_unknown" and receipt.cleanup_status == "complete"
        for receipt in receipts
    ):
        # 兼容旧版本已完成首步却仍 pending；原尝试已在上文逐件恢复，不另加历史扫描。
        record_started()

    for index, step in enumerate(steps):
        if step.id in done_ids:
            continue
        if step.subject_id in failed_subjects and (
            step.kind != "cleanup"
            or not _subject_resources_active(plan, receipts, step.subject_id)
        ):
            continue
        execution_admission(counterexample_digest(plan))
        remaining = sum(
            future.binding.timeout_seconds + future.reservation_seconds + 5
            for future in steps[index + 1 :]
            if future.id not in done_ids
        )
        receipt = execute_counterexample_attempt(
            root,
            contract,
            plan,
            step.id,
            deadline_ms=deadline_ms,
            remaining_reserve_seconds=remaining + plan.required_reserve_seconds,
            postcheck=current_boundary,
            on_started=record_started,
        )
        receipts.append(receipt)
        done_ids.add(step.id)
        if (
            receipt.status == "execution_unknown"
            or receipt.cleanup_status != "complete"
        ):
            break
        blocked_steps = _blocked_business_steps(plan, receipts)
        failed_subjects.update(by_id[item].subject_id for item in blocked_steps)
    current_boundary()
    bundle = _bundle_from_attempts(root, contract, plan, receipts)
    assessment = evaluate_counterexample(contract, plan, bundle, require_r2=require_r2)
    repairs = _derive_actual_repair(root, impl_input, plan_ref, plan, assessment)
    if repairs:
        bundle = bundle.model_copy(update={"repairs": repairs})
        assessment = evaluate_counterexample(
            contract, plan, bundle, require_r2=require_r2
        )
    output_dir = folder.parent / "results"
    observations_ref = _write_json(
        root, output_dir / f"observations-{counterexample_digest(bundle)}.json", bundle
    )
    assessment_ref = _write_json(
        root,
        output_dir / f"assessment-{counterexample_digest(assessment)}.json",
        assessment,
    )
    # 完整的同输入只读回原记录，不重复产生一次“新完成”。
    for path in sorted(output_dir.glob("record-*.json")):
        if path.name.startswith("record-reconciled-"):
            continue
        existing = CounterexampleEvidenceRecord.model_validate_json(
            read_stable_bytes(root, path)
        )
        if (
            existing.plan_ref == plan_ref
            and existing.observations_ref == observations_ref
            and existing.assessment_ref == assessment_ref
        ):
            reference = _ref(root, path)
            resolve_counterexample_evidence(root, impl_input, reference)
            return reference, assessment
    record = CounterexampleEvidenceRecord(
        loop_id=loop_run.loop_id,
        task_id=task_id,
        contract_ref=ArtifactRef(
            path=impl_input.verification_contract_ref,
            sha256=impl_input.verification_contract_digest,
        ),
        plan_ref=plan_ref,
        observations_ref=observations_ref,
        assessment_ref=assessment_ref,
        source_digest_before=before,
        source_digest_after=source_digest_sha256(build_source_digest(root)),
        recorded_at_ms=time.time_ns() // 1_000_000,
        phase_context=_save_phase_context(root, plan, require_r2, phase_capture),
    )
    record_ref = _write_json(
        root, output_dir / f"record-{counterexample_digest(record)}.json", record
    )
    resolve_counterexample_evidence(root, impl_input, record_ref)
    return record_ref, assessment


def _json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode()


def _path(root: Path, relative: str) -> Path:
    project_relative_path(relative)
    path = root / relative
    path.resolve(strict=False).relative_to(root.resolve(strict=True))
    cursor = root
    for part in Path(relative).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("counterexample-symlink-forbidden")
    return path


def _ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(read_stable_bytes(root, path)).hexdigest(),
    )


def _read_ref(root: Path, reference: ArtifactRef) -> bytes:
    # 稳定读取已逐段核验路径及文件身份；读引用不再重复执行写路径的前检。
    project_relative_path(reference.path)
    raw = read_stable_bytes(root, root / reference.path)
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError("counterexample-artifact-content-stale")
    return raw


def _git(root: Path, *args: str) -> str:
    from ai_sdlc.core.quality_command import quality_command_environment

    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        env=quality_command_environment(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return result.stdout.decode("utf-8")


def _snapshot_source_payload(
    root: Path, relative: str, index_entry: tuple[str, str] | None
) -> tuple[int, bytes] | None:
    project_relative_path(relative)
    path = root / relative
    parents = []
    cursor = root
    for part in Path(relative).parts[:-1]:
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return None  # 整个父目录的跟踪删除仍由 Git 身份绑定。
        if stat.S_ISLNK(info.st_mode) or _resource_is_reparse(info):
            raise ValueError("counterexample-symlink-forbidden")
        if not stat.S_ISDIR(info.st_mode):
            return None
        parents.append((cursor, info))

    def identity(info: os.stat_result | None) -> tuple[int, ...] | None:
        return None if info is None else (
            info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns,
        )

    def current() -> os.stat_result | None:
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    before = current()
    indexed_mode, indexed_oid = index_entry or ("", "")
    if before is not None and stat.S_ISLNK(before.st_mode):
        # 只保存 Git 链接对象的目标字节；不解析或读取目标，悬空链接也不能丢失。
        mode, raw = 0o120000, os.fsencode(os.readlink(path))
    elif indexed_mode == "160000" and (
        before is None or stat.S_ISDIR(before.st_mode)
    ):
        mode = 0o160000
        if before is not None and _resource_is_reparse(before):
            raise ValueError("counterexample-gitlink-not-checked-out")
        if before is None or not any(path.iterdir()):
            raw = indexed_oid.encode("ascii")
        else:
            if Path(
                _git(path, "rev-parse", "--show-toplevel").strip()
            ).resolve(strict=True) != path.resolve(strict=True):
                raise ValueError("counterexample-gitlink-not-checked-out")
            from ai_sdlc.core.source_change_capture import _gitlink_payload

            raw = _gitlink_payload(path)
    elif before is None:
        return None  # 普通跟踪删除由 HEAD/index/diff 身份绑定。
    else:
        raw = read_stable_bytes(root, path)
        # core.symlinks=false 的普通载体仍代表 index 中的链接对象。
        mode = 0o120000 if indexed_mode == "120000" else stat.S_IMODE(before.st_mode)
    if identity(before) != identity(current()) or any(
        identity(info) != identity(parent.lstat()) for parent, info in parents
    ):
        raise ValueError("counterexample-source-changed-during-snapshot")
    return mode, raw


def _file_map(
    root: Path, ignored_inputs: Sequence[str],
    *, captured_content: dict[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    index_entries = {}
    for record in _git(root, "ls-files", "--stage", "-z").split("\0"):
        if not record:
            continue
        metadata, separator, relative = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != "0":
            raise ValueError("counterexample-snapshot-index-entry-invalid")
        index_entries[relative] = (fields[0], fields[1])
    paths = set(
        _git(
            root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        ).split("\0")
    )
    paths.discard("")
    paths = {path for path in paths if not path.startswith(_RUNTIME)}
    paths.update(project_relative_path(path) for path in ignored_inputs)
    entries = []
    for relative in sorted(paths):
        if relative in ignored_inputs:
            _path(root, relative)
        material = _snapshot_source_payload(root, relative, index_entries.get(relative))
        if material is None:
            continue
        mode, raw = material
        if relative in ignored_inputs and mode in (0o120000, 0o160000):
            raise ValueError("counterexample-ignored-input-not-regular")
        if captured_content is not None:
            captured_content[relative] = raw
        entries.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": mode,
            }
        )
    return entries


def capture_counterexample_snapshot(
    project_root: Path,
    *,
    evidence_root: Path,
    artifact_dir: str,
    acceptance_sources: Mapping[str, str],
    ignored_inputs: Sequence[str] = (),
) -> dict[str, Any]:
    """完整捕获当前 Git 候选及声明的忽略输入；原字节保留在所属 Loop。"""
    project_root = project_root.resolve(strict=True)
    evidence_root = evidence_root.resolve(strict=True)
    folder = _path(evidence_root, artifact_dir)
    if not artifact_dir.startswith(".ai-sdlc/loops/implementation/"):
        raise ValueError("counterexample-snapshot-evidence-must-belong-to-loop")
    before = source_digest_sha256(build_source_digest(project_root))
    from ai_sdlc.core.quality_command import (
        _git_bytes,
        _git_filter_overrides,
        _source_identity_payload,
        quality_command_environment,
    )

    identity_env = quality_command_environment(os.environ)
    source_identity = _source_identity_payload(project_root, identity_env)
    tracked_diff = _git_bytes(
        project_root,
        identity_env,
        *_git_filter_overrides(project_root, identity_env),
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "HEAD",
        "--",
        ".",
        *(f":(exclude){prefix}**" for prefix in _RUNTIME),
    )
    captured_content: dict[str, bytes] = {}
    files = _file_map(project_root, ignored_inputs, captured_content=captured_content)
    store = LoopArtifactStore(evidence_root)
    diff_path = folder / "tracked-diff.bin"
    store.write_bytes_artifact(diff_path, tracked_diff, immutable=True)
    for entry in files:
        content = captured_content.pop(entry["path"])
        captured = folder / "content" / entry["sha256"]
        store.write_bytes_artifact(captured, content, immutable=True)
        entry["content_ref"] = _ref(evidence_root, captured).model_dump()
    accepted = {}
    indexed = {entry["path"]: entry for entry in files}
    for version, path in acceptance_sources.items():
        if version not in ("V0", "V1") or path not in indexed:
            raise ValueError("counterexample-acceptance-source-not-captured")
        if not 0 <= indexed[path]["mode"] <= 0o7777:
            raise ValueError("counterexample-acceptance-source-not-regular")
        accepted[version] = {
            "path": path,
            "sha256": indexed[path]["sha256"],
            "content_ref": indexed[path]["content_ref"],
        }
    if "V0" not in accepted:
        raise ValueError("counterexample-original-acceptance-source-required")
    manifest = {
        "schema_version": 1,
        "root": str(project_root),
        "source_digest": before,
        "source_identity": source_identity,
        "tracked_diff_ref": _ref(evidence_root, diff_path).model_dump(),
        "head": _git(project_root, "rev-parse", "HEAD").strip(),
        "index_tree": _git(project_root, "write-tree").strip(),
        "files": files,
        "ignored_inputs": list(ignored_inputs),
        "acceptance_sources": accepted,
    }
    if before != source_digest_sha256(build_source_digest(project_root)) or [
        {key: value for key, value in entry.items() if key != "content_ref"}
        for entry in files
    ] != _file_map(project_root, ignored_inputs):
        raise ValueError("counterexample-source-changed-during-snapshot")
    return manifest


def _snapshot_document(raw: bytes) -> dict[str, Any]:
    """先核原件结构，再消费摘要；坏材料统一走已有 ValueError 阻断。"""
    manifest = json.loads(raw)
    required = {
        "schema_version",
        "root",
        "source_digest",
        "source_identity",
        "head",
        "index_tree",
        "tracked_diff_ref",
        "files",
        "ignored_inputs",
        "acceptance_sources",
    }
    if (
        not isinstance(manifest, dict)
        or not required <= manifest.keys()
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or any(
            not isinstance(manifest[name], str) or not manifest[name]
            for name in ("root", "source_digest", "head", "index_tree")
        )
        or not isinstance(manifest["tracked_diff_ref"], dict)
        or not isinstance(manifest["files"], list)
        or not isinstance(manifest["ignored_inputs"], list)
        or any(not isinstance(path, str) for path in manifest["ignored_inputs"])
        or not isinstance(manifest["acceptance_sources"], dict)
    ):
        raise ValueError("counterexample-snapshot-schema-invalid")
    identity = manifest["source_identity"]
    if (
        not isinstance(identity, dict)
        or not {"head", "index_tree", "tracked_diff", "untracked"} <= identity.keys()
        or any(
            not isinstance(identity[name], str)
            for name in ("head", "index_tree", "tracked_diff")
        )
        or not isinstance(identity["untracked"], list)
        or any(not isinstance(entry, dict) for entry in identity["untracked"])
    ):
        raise ValueError("counterexample-snapshot-source-identity-invalid")
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or not {"path", "sha256", "mode", "content_ref"} <= entry.keys()
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
            or type(entry["mode"]) is not int
            or not isinstance(entry["content_ref"], dict)
        ):
            raise ValueError("counterexample-snapshot-file-entry-invalid")
    for entry in manifest["acceptance_sources"].values():
        if (
            not isinstance(entry, dict)
            or not {"path", "sha256", "content_ref"} <= entry.keys()
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
            or not isinstance(entry["content_ref"], dict)
        ):
            raise ValueError("counterexample-snapshot-acceptance-entry-invalid")
    return manifest


def validate_counterexample_snapshots(
    root: Path,
    plan: CounterexamplePlan,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
    require_live: bool = True,
    _defer_file_map_subject: str | None = None,
    _resolved_commands: dict[str, dict[str, Any]] | None = None,
) -> tuple[ArtifactRef, ...]:
    """重捕获实际源码与受限差异，并返回原项目内可供评审读取的全部原字节。"""
    root = root.resolve(strict=True)
    if _defer_file_map_subject is not None and (
        not require_live
        or _defer_file_map_subject not in {subject.id for subject in plan.subjects}
    ):
        raise ValueError("counterexample-deferred-file-map-subject-invalid")

    def read(reference: ArtifactRef) -> bytes:
        if isinstance(captured_artifacts, _CurrentStateMaterial):
            return captured_artifacts.read_reference(reference)
        if captured_artifacts is None:
            return _read_ref(root, reference)
        raw = captured_artifacts.get(reference.path)
        if raw is None or hashlib.sha256(raw).hexdigest() != reference.sha256:
            raise ValueError("counterexample-captured-artifact-missing-or-stale")
        return raw

    commands = _resolved_commands if _resolved_commands is not None else {}
    executable_originals: dict[str, dict[str, Any]] = {}
    manifests = {}
    references = []
    for subject in plan.subjects:
        manifest = _snapshot_document(read(subject.snapshot))
        references.append(subject.snapshot)
        if (
            type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
        ):
            raise ValueError("counterexample-snapshot-schema-invalid")
        project = Path(manifest["root"]).resolve(strict=require_live)
        if str(project) != manifest["root"]:
            raise ValueError("counterexample-snapshot-root-not-canonical")
        identity = manifest["source_identity"]
        if (
            counterexample_digest(identity) != subject.candidate_digest
            or manifest["source_digest"] != subject.candidate_digest
        ):
            raise ValueError("counterexample-subject-source-stale")
        if (
            identity["head"] != manifest["head"]
            or identity["index_tree"] != manifest["index_tree"]
        ):
            raise ValueError("counterexample-snapshot-git-identity-stale")
        diff_ref = ArtifactRef.model_validate(manifest["tracked_diff_ref"])
        read(diff_ref)
        if "sha256:" + diff_ref.sha256 != identity["tracked_diff"]:
            raise ValueError("counterexample-snapshot-diff-evidence-stale")
        references.append(diff_ref)
        captured = [
            {key: value for key, value in entry.items() if key != "content_ref"}
            for entry in manifest["files"]
        ]
        if len({entry["path"] for entry in captured}) != len(captured):
            raise ValueError("counterexample-snapshot-duplicate-file")
        if require_live:
            if (
                source_digest_sha256(build_source_digest(project))
                != subject.candidate_digest
            ):
                raise ValueError("counterexample-subject-source-stale")
            if subject.id != _defer_file_map_subject and captured != _file_map(
                project, manifest["ignored_inputs"]
            ):
                raise ValueError("counterexample-snapshot-file-set-stale")
        for entry in manifest["files"]:
            reference = ArtifactRef.model_validate(entry["content_ref"])
            if reference.sha256 != entry["sha256"]:
                raise ValueError("counterexample-snapshot-content-ref-mismatch")
            project_relative_path(entry["path"])
            read(reference)
            references.append(reference)
        for version, expected in (("V0", plan.v0_digest), ("V1", plan.v1_digest)):
            if expected is None:
                continue
            source = manifest["acceptance_sources"].get(version)
            if (
                source is None
                or source["sha256"] != expected
                or source["path"] not in plan.protected_paths
            ):
                raise ValueError("counterexample-acceptance-source-binding-invalid")
            if not any(
                entry["path"] == source["path"] and entry["sha256"] == expected
                and 0 <= entry["mode"] <= 0o7777
                for entry in manifest["files"]
            ):
                raise ValueError("counterexample-acceptance-source-not-in-snapshot")
        for step in (step for step in plan.steps if step.subject_id == subject.id):
            if Path(step.binding.project_root).resolve(strict=require_live) != project:
                raise ValueError("counterexample-step-root-differs-from-snapshot")
            # 诊断快照只证明源码原件；历史执行归因由回执中的原始解析记录证明。
            if not require_live or step.kind not in ("observe", "acceptance"):
                continue
            if step.id not in commands:
                commands[step.id] = _resolve_command(step, executable_originals)
            if step.kind == "acceptance":
                source = manifest["acceptance_sources"][step.acceptance_version]["path"]
                if not _command_calls_file(step, project / source, commands[step.id]):
                    raise ValueError("counterexample-acceptance-command-not-bound")
            if step.kind == "observe" and not any(
                source in {
                    entry["path"] for entry in manifest["files"]
                    if 0 <= entry["mode"] <= 0o7777
                }
                and _command_calls_file(step, project / source, commands[step.id])
                for source in plan.protected_paths
            ):
                raise ValueError("counterexample-independent-observer-script-not-bound")
        manifests[subject.id] = manifest
    for subject in plan.subjects:
        if subject.role == "current":
            if subject.candidate_digest != plan.candidate_digest:
                raise ValueError("counterexample-current-source-not-parent")
            continue
        parent = next(
            item
            for item in plan.subjects
            if item.role == "current" and item.obligation_id == subject.obligation_id
        )
        left, right = manifests[parent.id], manifests[subject.id]
        left_files = {
            item["path"]: (item["sha256"], item["mode"]) for item in left["files"]
        }
        right_files = {
            item["path"]: (item["sha256"], item["mode"]) for item in right["files"]
        }
        changes = {
            path
            for path in left_files.keys() | right_files.keys()
            if left_files.get(path) != right_files.get(path)
        }
        if subject.patch is None:
            if changes or subject.candidate_digest != plan.candidate_digest:
                raise ValueError("counterexample-unpatched-subject-differs")
            continue
        if (
            changes != set(subject.modified_paths)
            or not changes <= set(plan.allowed_modified_paths)
            or changes.intersection(plan.protected_paths)
        ):
            raise ValueError("counterexample-subject-patch-scope-invalid")
        if (left["head"], left["index_tree"], left["ignored_inputs"]) != (
            right["head"],
            right["index_tree"],
            right["ignored_inputs"],
        ):
            raise ValueError("counterexample-patch-alters-base-identity")
        patch = json.loads(read(subject.patch))
        references.append(subject.patch)
        expected_changes = [
            {
                "path": path,
                "before_sha256": left_files.get(path, (None,))[0],
                "after_sha256": right_files.get(path, (None,))[0],
            }
            for path in sorted(changes)
        ]
        if patch != {"schema_version": 1, "changes": expected_changes}:
            raise ValueError("counterexample-patch-content-relation-unproven")
        for step in (
            step
            for step in plan.steps
            if step.subject_id == subject.id and step.kind in ("observe", "acceptance")
        ):
            if require_live and any(
                _command_calls_file(
                    step, Path(right["root"]) / path, commands[step.id]
                )
                for path in changes
            ):
                raise ValueError("counterexample-patch-modifies-observer-or-acceptance")
    return tuple({reference.path: reference for reference in references}.values())


def _command_candidates(step: ExecutionStep) -> tuple[str, ...]:
    """仅使用绑定环境展开查找路径；保留 venv 调用路径，不把 argv0 换成 realpath。"""
    argv0 = step.binding.argv[0]
    cwd = Path(step.binding.project_root) / step.binding.cwd
    environment = (
        {key.upper(): value for key, value in step.binding.effective_environment.items()}
        if os.name == "nt" else dict(step.binding.effective_environment)
    )
    explicit = bool(os.path.dirname(argv0))
    directories = (
        [str(cwd)]
        if explicit
        else environment.get("PATH", os.defpath).split(os.pathsep)
    )
    if os.name == "nt" and not explicit:
        directories.insert(0, str(cwd))
    suffixes = [""]
    if os.name == "nt" and not Path(argv0).suffix:
        suffixes.extend(environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";"))
    return tuple(
        dict.fromkeys(
            os.path.abspath(cwd / directory / (argv0 + suffix))
            for directory in directories
            for suffix in suffixes
        )
    )


def _executable_original(path: str) -> dict[str, Any]:
    callable_path = Path(path)
    physical = callable_path.resolve(strict=True)
    before = physical.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("counterexample-command-executable-not-regular")
    content = read_stable_bytes(physical.parent, physical)
    after = physical.stat()

    def identity(info: os.stat_result) -> list[int]:
        return [
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ]

    if (
        identity(before) != identity(after)
        or callable_path.resolve(strict=True) != physical
    ):
        raise ValueError("counterexample-command-executable-changed")
    return {
        "executable_target": str(physical),
        "executable_identity": identity(after),
        "executable_sha256": hashlib.sha256(content).hexdigest(),
    }


def _resolve_command(
    step: ExecutionStep, originals: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    candidates = _command_candidates(step)
    for candidate in candidates:
        path = Path(candidate)
        # 显式文件的静态归因与当前可执行分开；当前动作在放行前仍核执行权限。
        if path.is_file() and (
            os.path.dirname(step.binding.argv[0]) or os.access(path, os.X_OK)
        ):
            if originals is not None and candidate in originals:
                original = originals[candidate]
            else:
                original = _executable_original(candidate)
                if originals is not None:
                    originals[candidate] = original
            return {
                "schema_version": 1,
                "argv": [candidate, *step.binding.argv[1:]],
                **original,
                # 只接受当前受控 Python 的真实文件身份；文件名和任意 argv1 不证明解释器语义。
                "interpreter": "python"
                if os.path.samefile(candidate, sys.executable)
                else None,
            }
    raise ValueError(
        "counterexample-command-executable-unavailable: use an explicit executable or the current Python interpreter"
    )


def _command_original_required(intent: Mapping[str, Any]) -> bool:
    if "resolved_command" not in intent and "source_snapshot" not in intent:
        return False  # 未发布旧材料只保留诊断，不产生当前有效验收证明。
    if not isinstance(intent.get("resolved_command"), dict) or "source_snapshot" not in intent:
        raise ValueError("counterexample-original-command-fields-missing")
    return True


def _read_resolved_command(step: ExecutionStep, document: Any) -> dict[str, Any] | None:
    """所有回执使用方共享纯解析；旧材料可诊断，不能向当前文件系统补造执行证明。"""
    if document is None:
        return None
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema_version",
            "argv",
            "executable_target",
            "executable_identity",
            "executable_sha256",
            "interpreter",
        }
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(document.get("argv"), list)
        or len(document["argv"]) != len(step.binding.argv)
        or not all(
            isinstance(arg, str) and "\x00" not in arg for arg in document["argv"]
        )
        or document["argv"][1:] != list(step.binding.argv[1:])
        or document["argv"][0] not in _command_candidates(step)
        or not isinstance(document.get("executable_target"), str)
        or not Path(document["executable_target"]).is_absolute()
        or not isinstance(document.get("executable_sha256"), str)
        or len(document["executable_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in document["executable_sha256"])
        or not isinstance(document.get("executable_identity"), list)
        or len(document["executable_identity"]) != 6
        or any(type(value) is not int for value in document["executable_identity"])
        or not stat.S_ISREG(document["executable_identity"][2])
        or document.get("interpreter") not in (None, "python")
    ):
        raise ValueError("counterexample-original-command-binding-invalid")
    return document


def _command_calls_file(
    step: ExecutionStep, target: Path, resolved: Mapping[str, Any] | None = None
) -> bool:
    command = _resolve_command(step) if resolved is None else resolved
    argv = command["argv"]
    # 历史读取只比较原绑定的词法绝对路径，不让后来出现的链接重解释旧来源。
    target_name = os.path.abspath(target)
    if argv[0] == target_name:
        return True
    if command["interpreter"] != "python":
        return False
    cwd = Path(step.binding.project_root) / step.binding.cwd
    for arg in argv[1:]:
        if arg in ("-I", "-B", "-u"):
            continue
        if arg.startswith("-"):
            return False
        return bool(os.path.abspath(cwd / arg) == target_name)
    return False


def _validate_original_command_source(
    plan: CounterexamplePlan,
    step: ExecutionStep,
    command: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    if step.kind not in ("observe", "acceptance"):
        return
    subject = next(item for item in plan.subjects if item.id == step.subject_id)
    paths = (
        (manifest["acceptance_sources"][step.acceptance_version]["path"],)
        if step.kind == "acceptance"
        else plan.protected_paths
    )
    files = {
        item["path"]: item["sha256"] for item in manifest["files"]
        if 0 <= item["mode"] <= 0o7777
    }
    if not any(
        path in files
        and _command_calls_file(step, Path(manifest["root"]) / path, command)
        and (
            command["argv"][0] != os.path.abspath(Path(manifest["root"]) / path)
            or command["executable_sha256"] == files[path]
        )
        for path in paths
    ) or any(
        _command_calls_file(step, Path(manifest["root"]) / path, command)
        for path in subject.modified_paths
    ):
        raise ValueError("counterexample-original-command-source-unproven")


def _check_resolved_command_live(command: Mapping[str, Any]) -> None:
    path = command["argv"][0]
    if not os.access(path, os.X_OK) or any(
        command[key] != value for key, value in _executable_original(path).items()
    ):
        raise ValueError("counterexample-prelaunch-command-identity-stale")


def _attempts_dir(root: Path, plan: CounterexamplePlan) -> Path:
    return (
        LoopArtifactStore(root).loop_run_dir(plan.loop_id, loop_type="implementation")
        / "counterexamples"
        / "attempts"
    )


def _write_json(
    root: Path, path: Path, payload: Any, *, immutable: bool = True
) -> ArtifactRef:
    LoopArtifactStore(root).write_bytes_artifact(
        path, _json_bytes(payload), immutable=immutable
    )
    return _ref(root, path)


def _publish_attempt_intent(
    root: Path, attempt_dir: Path, intent: Mapping[str, Any]
) -> ArtifactRef:
    """仅撤销当前调用尚未发布意图、也未认领资源的自有空输出。"""
    _validated_attempt_intent(intent)
    if not _command_original_required(intent):
        raise ValueError("counterexample-original-command-fields-missing")
    parent = root
    missing = []
    for part in attempt_dir.parent.relative_to(root).parts:
        parent /= part
        try:
            _require_artifact_directory(root, parent)
        except FileNotFoundError:
            missing.append(parent)
    for parent in missing:
        _require_artifact_directory(root, parent.parent)
        parent.mkdir(exist_ok=True)
        _require_artifact_directory(root, parent)
    attempt_dir.mkdir()
    identity = _directory_identity(attempt_dir)
    outputs: dict[str, tuple[int, ...]] = {}
    try:
        # 空输出先预建；意图失败时仍不会启动目标命令，独占句柄记录原创建身份。
        for name in ("stdout", "stderr"):
            with (attempt_dir / name).open("xb") as stream:
                outputs[name] = _resource_node_identity(os.fstat(stream.fileno()))
                stream.flush()
                os.fsync(stream.fileno())
        return _write_json(root, attempt_dir / "intent.json", intent)
    except OSError:
        try:
            # hard link 已发布后 fsync 仍可能失败；任何额外条目都必须保留现场。
            if (
                _directory_identity(attempt_dir) == identity
                and {path.name for path in attempt_dir.iterdir()} == set(outputs)
                and all(
                    _resource_node_identity((attempt_dir / name).lstat()) == original
                    and read_stable_bytes(root, attempt_dir / name) == b""
                    for name, original in outputs.items()
                )
            ):
                for name, original in outputs.items():
                    if (
                        _directory_identity(attempt_dir) != identity
                        or _resource_node_identity((attempt_dir / name).lstat())
                        != original
                    ):
                        break
                    (attempt_dir / name).unlink()
                else:
                    if _directory_identity(attempt_dir) == identity:
                        attempt_dir.rmdir()
        except (OSError, ValueError):
            # 无法完整确认或清理时留下原件，冷恢复仍按未知历史拒绝重放。
            pass
        raise


def _resource_is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _resource_node_identity(info: os.stat_result) -> tuple[int, ...]:
    if _resource_is_reparse(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError("counterexample-resource-state-special-node")
    # 普通文件的其它名字可能位于资源之外；目录链接数不代表文件共享。
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError("counterexample-resource-file-not-exclusively-owned")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _resource_inventory(
    directory: Path, *, deadline_ms: int
) -> dict[str, tuple[int, ...]]:
    """归属检查只遍历资源元数据；与完整内容快照共用同一普通文件判定。"""

    def check_time() -> None:
        if time.time_ns() // 1_000_000 >= deadline_ms:
            raise ValueError("counterexample-resource-state-deadline-exceeded")

    check_time()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("counterexample-owned-resource-directory-required")
    _directory_identity(directory)
    result = {}
    pending = [directory]
    while pending:
        parent = pending.pop()
        check_time()
        with os.scandir(parent) as entries:
            for entry in entries:
                check_time()
                relative = Path(entry.path).relative_to(directory).as_posix()
                # Windows 的 DirEntry 缓存将设备、文件身份与链接数置零；归属检查须实时读取且不跟随链接。
                info = os.stat(entry.path, follow_symlinks=False)
                identity = _resource_node_identity(info)
                if relative == ".ai-sdlc-owner.json":
                    # 宿主 marker 也必须是独占普通文件，但不属于业务初态。
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("counterexample-resource-state-special-node")
                    continue
                if ".ai-sdlc-owner.json" in Path(relative).parts:
                    raise ValueError("counterexample-resource-state-nested-owner")
                result[relative] = identity
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
    check_time()
    return result


def _observed_resource_state(
    resources: Sequence[ResourceBinding], *, deadline_ms: int
) -> list[dict[str, Any]]:
    """完整读取普通文件和目录；原截止限制扫描，不忽略数据库附属文件。"""

    def check_time() -> None:
        if time.time_ns() // 1_000_000 >= deadline_ms:
            raise ValueError("counterexample-resource-state-deadline-exceeded")

    snapshots = []
    for resource in sorted(resources, key=lambda item: item.id):
        directory = Path(resource.root)
        directory_before = _resource_node_identity(directory.lstat())
        before = _resource_inventory(directory, deadline_ms=deadline_ms)
        files = []
        for relative, identity in sorted(before.items()):
            check_time()
            digest = None
            kind = "directory" if stat.S_ISDIR(identity[2]) else "file"
            if kind == "file":
                hasher = hashlib.sha256()
                descriptor = os.open(
                    directory / relative, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if _resource_node_identity(opened) != identity:
                        raise ValueError(
                            "counterexample-resource-state-changed-during-read"
                        )
                    while True:
                        check_time()
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        hasher.update(chunk)
                digest = hasher.hexdigest()
            files.append(
                {
                    "path": relative,
                    "kind": kind,
                    "sha256": digest,
                    "mode": stat.S_IMODE(identity[2]),
                }
            )
        if (
            before != _resource_inventory(directory, deadline_ms=deadline_ms)
            or directory_before != _resource_node_identity(directory.lstat())
        ):
            raise ValueError("counterexample-resource-state-changed-during-read")
        snapshots.append(
            {
                "resource_id": resource.id,
                "directory_mode": stat.S_IMODE(directory_before[2]),
                "files": files,
            }
        )
    check_time()
    return snapshots


def _resource_content_state(
    snapshots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """初态合同只声明内容和目录结构；重建不要求复制未声明的权限。"""
    return [
        {
            "resource_id": snapshot["resource_id"],
            "files": [
                {key: entry[key] for key in ("path", "kind", "sha256")}
                for entry in snapshot["files"]
            ],
        }
        for snapshot in snapshots
    ]


def _actual_git_directories(project: Path) -> tuple[Path, Path]:
    """真实 Git 元数据可位于 .git 文件之外；未知布局不能授权资源认领或删除。"""
    try:
        lines = _git(
            project, "rev-parse", "--absolute-git-dir", "--git-common-dir"
        ).split("\n")
        if len(lines) != 3 or lines[-1] or not all(lines[:2]):
            raise ValueError("git-directory-output-invalid")
        gitdir, common = map(Path, lines[:2])
        if not gitdir.is_absolute():
            raise ValueError("git-directory-not-absolute")
        if not common.is_absolute():
            common = project / common
        directories = (gitdir.resolve(strict=True), common.resolve(strict=True))
        if not all(path.is_dir() for path in directories):
            raise ValueError("git-directory-missing")
        return directories
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ValueError("counterexample-resource-git-metadata-unavailable") from exc


def _require_scratch_root(
    root: Path,
    project: Path,
    plan: CounterexamplePlan,
    resource: ResourceBinding,
    snapshot: ArtifactRef,
) -> None:
    """只有整目录明确忽略且不包含代码、证据或 Git 跟踪项才允许认领和删除。"""
    directory = Path(resource.root)
    relative = directory.relative_to(project).as_posix()
    if relative in ("", "."):
        raise ValueError("counterexample-resource-root-overlaps-project")
    protected = [
        project / name for name in (".git", ".ai-sdlc", ".venv", "node_modules")
    ]
    manifest = json.loads(_read_ref(root, snapshot))
    protected.extend(
        project / project_relative_path(entry["path"]) for entry in manifest["files"]
    )
    protected.extend(
        project / item for item in (*plan.protected_paths, *plan.allowed_modified_paths)
    )
    protected.extend((root / ".git", root / ".ai-sdlc"))
    # 每次资源边界都读取两仓库当前布局，首次归属不能授权后来迁入的 Git 元数据。
    for repository in dict.fromkeys((root, project)):
        protected.extend(_actual_git_directories(repository))
    if any(
        directory == item
        or directory.is_relative_to(item)
        or item.is_relative_to(directory)
        for item in protected
    ):
        raise ValueError("counterexample-resource-root-overlaps-protected-path")
    try:
        # 检查目录本身；仅忽略 owner 标记不能授权其父目录。
        _git(project, "check-ignore", "--quiet", "--", relative + "/")
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            "counterexample-resource-must-be-explicitly-ignored-scratch"
        ) from exc
    if _git(project, "--literal-pathspecs", "ls-files", "-z", "--", relative):
        raise ValueError("counterexample-resource-contains-tracked-files")


def _initial_resource_state(
    root: Path,
    reference: ArtifactRef,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[list[dict[str, Any]], tuple[ArtifactRef, ...]]:
    raw = _bound_read(root, reference, captured_artifacts)
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or set(payload) != {"schema_version", "files"}
        or not isinstance(payload["files"], list)
    ):
        raise ValueError("counterexample-resource-initial-schema-invalid")
    entries = []
    refs = [reference]
    for entry in payload["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) not in ({"path", "sha256"}, {"path", "sha256", "content_ref"})
            or not isinstance(entry["path"], str)
            or not isinstance(entry["sha256"], str)
        ):
            raise ValueError("counterexample-resource-initial-entry-invalid")
        path = project_relative_path(entry["path"])
        if ".ai-sdlc-owner.json" in Path(path).parts:
            raise ValueError("counterexample-resource-initial-owner-forbidden")
        ArtifactRef(path=path, sha256=entry["sha256"])
        if "content_ref" in entry:
            content_ref = ArtifactRef.model_validate(entry["content_ref"])
            _bound_read(root, content_ref, captured_artifacts)
            if content_ref.sha256 != entry["sha256"]:
                raise ValueError("counterexample-initial-content-binding-invalid")
            refs.append(content_ref)
        entries.append(entry)
    paths = {entry["path"] for entry in entries}
    if len(paths) != len(entries):
        raise ValueError("counterexample-resource-initial-duplicate-path")
    if any(
        parent.as_posix() in paths for path in paths for parent in Path(path).parents
    ):
        raise ValueError("counterexample-resource-initial-file-parent-conflict")
    return sorted(entries, key=lambda entry: entry["path"]), tuple(refs)


def resource_initial_artifact_refs(
    root: Path,
    initial_ref: ArtifactRef,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[ArtifactRef, ...]:
    """初态和每个可恢复原始数据文件共同进入原审查捕获集合。"""
    return _initial_resource_state(root, initial_ref, captured_artifacts)[1]


def _require_prior_resource_cleanup(
    root: Path,
    plan: CounterexamplePlan,
    step: ExecutionStep,
    resource: ResourceBinding,
    *,
    exclude_attempt: ArtifactRef | None = None,
) -> None:
    candidates = []
    for path in _attempts_dir(root, plan).glob("*/intent.json"):
        if exclude_attempt is not None and path == root / exclude_attempt.path:
            continue
        intent = _validated_attempt_intent(json.loads(read_stable_bytes(root, path)))
        if (
            intent["contract_digest"] == plan.contract_digest
            and intent["subject_id"] == step.subject_id
            and resource.model_dump(mode="json") in intent["binding"]["resources"]
        ):
            candidates.append((intent["attempt_ordinal"], path, intent))
    if not candidates:
        raise ValueError("counterexample-reset-has-no-owned-cleanup-proof")
    _, path, intent = max(candidates, key=lambda item: item[0])
    original = plan
    if intent["plan_digest"] != counterexample_digest(plan):
        original, _ = _read_history_plan(
            root,
            _attempts_dir(root, plan).parent / "plans" / f"{intent['plan_digest']}.json",
        )
    _require_loop_batch_policy(original, plan)
    previous = _receipt(root, original, _ref(root, path))
    cleanup_path = path.with_name("resource-cleanup.json")
    if (
        intent["kind"] != "cleanup"
        or not previous.normally_completed
        or previous.exit_code != 0
        or not cleanup_path.is_file()
    ):
        raise ValueError("counterexample-reset-prior-resource-state-unknown")
    proof = json.loads(read_stable_bytes(root, cleanup_path))
    if (
        proof.get("subject_id") != step.subject_id
        or proof.get("plan_digest") != intent["plan_digest"]
        or resource.model_dump(mode="json") not in proof.get("resources", ())
    ):
        raise ValueError("counterexample-reset-resource-cleanup-binding-invalid")
    if (
        intent["plan_digest"] == counterexample_digest(plan)
        and intent["step_id"] not in step.depends_on
    ):
        raise ValueError("counterexample-reset-must-depend-on-owned-cleanup")


def _validate_plan_resource_admission(
    root: Path,
    plan: CounterexamplePlan,
    *,
    new_execution: bool,
    deadline_ms: int,
    native_initial_policy: bool = True,
) -> None:
    """稳定初态只读一次；新表还须覆盖条件分支中的清理后重建。"""
    initials: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def require_rebuild_bytes(resource: ResourceBinding) -> None:
        reference = resource.initial_state
        if any(
            "content_ref" not in entry
            for entry in initials[(reference.path, reference.sha256)]
        ):
            raise ValueError(
                "counterexample-reset-initial-bytes-required-before-freeze"
            )

    for step in plan.steps:
        for resource in step.binding.resources:
            reference = resource.initial_state
            key = (reference.path, reference.sha256)
            if key not in initials:
                initials[key], _ = _initial_resource_state(root, reference)
            # 保留原生封存原有全表字节门禁，不扩大到下层合法的原位 reset。
            if native_initial_policy and step.kind == "reset":
                require_rebuild_bytes(resource)
    if not new_execution:
        return
    # 首次封存检查每个对象的真实首次资源状态；后续 reset 仍在原 cleanup 后核验重建。
    # 不认领、不补建、不写原件；历史读取及 cleanup recovery 不进入此新计划分支。
    first_uses: set[tuple[str, str]] = set()
    for step in plan.steps:
        for resource in step.binding.resources:
            key = (step.subject_id, resource.id)
            if key in first_uses:
                continue
            _validate_resource(
                root, Path(step.binding.project_root), plan, step, resource,
                claim=False, deadline_ms=deadline_ms,
                allow_initial_claim=step.kind != "cleanup",
            )
            first_uses.add(key)
    rebuilds = _new_plan_resource_rebuilds(plan)
    if not native_initial_policy:
        for step in plan.steps:
            for resource in step.binding.resources:
                if (step.id, resource.id) in rebuilds:
                    require_rebuild_bytes(resource)
    # 封存前覆盖所有可能进入的 repair/R2 及清理动作，避免资源占用后才发现无程序可用。
    executable_originals: dict[str, dict[str, Any]] = {}
    checked_executables: set[str] = set()
    for step in plan.steps:
        command = _resolve_command(step, executable_originals)
        executable = command["argv"][0]
        if executable not in checked_executables:
            _check_resolved_command_live(command)
            checked_executables.add(executable)
    # 这里只复用本次准入的稳定读取；每次实际启动前仍独立核当前程序身份和权限。


def _new_plan_resource_rebuilds(plan: CounterexamplePlan) -> set[tuple[str, str]]:
    """同一纯判断覆盖各条件组合，返回清理后实际需要重建的资源动作。"""
    rebuilds: set[tuple[str, str]] = set()
    for active in (set(), {"repair"}, {"r2"}, {"repair", "r2"}):
        cleaned: dict[tuple[str, str], str] = {}
        for step in plan.steps:
            if step.phase in {"repair", "r2"} and step.phase not in active:
                continue
            for resource in step.binding.resources:
                key = (step.subject_id, resource.id)
                predecessor = cleaned.get(key)
                if predecessor is None:
                    if step.kind == "cleanup":
                        cleaned[key] = step.id
                    continue
                if step.kind != "reset":
                    raise ValueError(
                        "counterexample-resource-reset-required-before-freeze"
                    )
                # 与实际 reset 的同计划归属证明一致：必须直接引用刚完成的 cleanup。
                if predecessor not in step.depends_on:
                    raise ValueError(
                        "counterexample-resource-reset-cleanup-dependency-required-before-freeze"
                    )
                rebuilds.add((step.id, resource.id))
                del cleaned[key]
    return rebuilds


def _validate_plan_resource_roots(plan: CounterexamplePlan) -> None:
    """整表先排除相互覆盖的归属；同一资源跨阶段的原收尾/重置仍可复用。"""
    roots: dict[tuple[str, str], Path] = {}
    for step in plan.steps:
        for resource in step.binding.resources:
            path = Path(resource.root)
            if not path.is_absolute() or path.resolve(strict=False) != path:
                raise ValueError("counterexample-resource-root-not-canonical")
            key = (step.subject_id, resource.id)
            if key in roots and roots[key] != path:
                raise ValueError("counterexample-resource-cycle-root-changed")
            for other_key, other in roots.items():
                if other_key != key and (
                    path.is_relative_to(other) or other.is_relative_to(path)
                ):
                    raise ValueError("counterexample-resource-roots-overlap")
            roots[key] = path


def _validate_step_bindings(
    contract: VerificationContract, plan: CounterexamplePlan, step: ExecutionStep
) -> None:
    """仅核静态输入，不要求后续 cleanup/reset 的资源已经存在或已被认领。"""
    validate_quality_argv(step.binding.argv)
    permissions = {
        permission.id: permission for permission in contract.resource_permissions
    }
    project = Path(step.binding.project_root).resolve(strict=True)
    # 已完成步骤的 cwd 可已随 cleanup 删除；整表这里只核词法和现存链接。
    cwd = (project / step.binding.cwd).resolve(strict=False)
    cwd.relative_to(project)
    resource_roots = []
    subject = next(
        subject for subject in plan.subjects if subject.id == step.subject_id
    )
    method = next(
        obligation.oracle_spec.observation_method
        for obligation in contract.obligations
        if obligation.id == subject.obligation_id
    )
    for resource in step.binding.resources:
        permission = permissions[resource.permission_id]
        required_actions = {
            "execute",
            "cleanup"
            if step.kind == "cleanup"
            else "write"
            if step.kind in ("exercise", "reset")
            else "read",
        }
        if not required_actions <= set(permission.actions):
            raise ValueError("counterexample-resource-action-not-authorized")
        if resource.kind == "service" or resource.cleanup_method != "owned_directory":
            raise ValueError("counterexample-resource-adapter-unsupported")
        resource_path = Path(resource.root)
        if (
            not resource_path.is_absolute()
            or resource_path.resolve(strict=False) != resource_path
        ):
            raise ValueError("counterexample-resource-root-not-canonical")
        resource_path.relative_to(_path(project, permission.scope))
        endpoint = Path(resource.observation_endpoint)
        if not endpoint.is_absolute() or endpoint.resolve(strict=False) != endpoint:
            raise ValueError("counterexample-resource-endpoint-not-canonical")
        endpoint.relative_to(resource_path)
        if (
            resource.permission_id == method.resource_id
            and endpoint != resource_path / project_relative_path(method.location)
        ):
            raise ValueError("counterexample-frozen-observation-endpoint-mismatch")
        _path(project, resource_path.relative_to(project).as_posix())
        resource_roots.append(resource_path)
        if step.kind in ("exercise", "observe", "acceptance", "reset") and not any(
            arg == str(endpoint) or arg == str(resource_path)
            for arg in step.binding.argv
        ):
            raise ValueError("counterexample-command-resource-endpoint-not-explicit")
    for key, value in step.binding.effective_environment.items():
        if key.upper() in _SAFE_ENV:
            continue
        if key.upper() in _RESOURCE_ENV or key.startswith("CE_"):
            candidate = Path(value)
            if candidate.is_absolute():
                # 环境变量同样会被真实文件操作消费；词法前缀不能证明资源归属。
                resolved = candidate.resolve()
                if candidate == resolved and any(
                    resolved.is_relative_to(path) for path in resource_roots
                ):
                    continue
        raise ValueError("counterexample-environment-not-explicit-test-allowlist")


def _validate_current_step_cwd(
    root: Path,
    plan: CounterexamplePlan,
    step: ExecutionStep,
    *,
    allow_planned_reset: bool = False,
) -> None:
    """固定 cwd 必须存在；只有冻结 reset 明确会创建的目录可延至认领后检查。"""
    project = Path(step.binding.project_root).resolve(strict=True)
    cwd = (project / step.binding.cwd).resolve(strict=False)
    cwd.relative_to(project)
    try:
        cwd.resolve(strict=True)
    except FileNotFoundError:
        if allow_planned_reset:
            by_id = {item.id: item for item in plan.steps}
            ancestors: set[str] = set()
            pending = list(step.depends_on)
            while pending:
                identifier = pending.pop()
                if identifier not in ancestors:
                    ancestors.add(identifier)
                    pending.extend(by_id[identifier].depends_on)
            for reset in plan.steps:
                if (
                    reset.kind != "reset"
                    or reset.subject_id != step.subject_id
                    or reset.binding.project_root != step.binding.project_root
                    or reset.id != step.id
                    and reset.id not in ancestors
                ):
                    continue
                for resource in reset.binding.resources:
                    resource_root = Path(resource.root)
                    if resource not in step.binding.resources or not cwd.is_relative_to(
                        resource_root
                    ):
                        continue
                    initial, _ = _initial_resource_state(root, resource.initial_state)
                    if any("content_ref" not in entry for entry in initial):
                        continue
                    relative = cwd.relative_to(resource_root)
                    # writer 只创建资源根和冻结文件的父目录，不推测任意缺失子目录。
                    if relative == Path(".") or any(
                        relative in Path(entry["path"]).parents for entry in initial
                    ):
                        return
        raise
    if not cwd.is_dir():
        raise ValueError("counterexample-current-cwd-directory-required")


def _directory_identity(path: Path) -> dict[str, int]:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or _resource_is_reparse(info)
        or info.st_ino <= 0
    ):
        raise ValueError("counterexample-resource-directory-identity-unavailable")
    return {"device": info.st_dev, "inode": info.st_ino}


def _resource_ownership_refs(
    root: Path,
    plan: CounterexamplePlan,
    intent: Mapping[str, Any],
    attempt_ref: ArtifactRef,
    raw: bytes,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[dict[str, ArtifactRef], tuple[ArtifactRef, ...]]:
    """周期身份来自首个 attempt 原件，marker 不是可自行授权的所有权声明。"""
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("attempt_ref") != attempt_ref.model_dump(mode="json")
        or not isinstance(document.get("resources"), dict)
        or set(document) != {"schema_version", "attempt_ref", "resources"} | (
            {"prelaunch_cleanup"} if document.get("prelaunch_cleanup") is True else set()
        )
    ):
        raise ValueError("counterexample-resource-ownership-original-invalid")
    resources = {item["id"]: item for item in intent["binding"]["resources"]}
    if (
        not set(document["resources"]) <= set(resources)
        or document.get("prelaunch_cleanup") is not True
        and set(document["resources"]) != set(resources)
    ):
        raise ValueError("counterexample-resource-ownership-set-conflict")
    claims: dict[str, ArtifactRef] = {}
    refs: list[ArtifactRef] = []
    for identifier, value in document["resources"].items():
        reference = ArtifactRef.model_validate(value)
        claim = json.loads(_bound_read(root, reference, captured_artifacts))
        if not isinstance(claim, dict):
            raise ValueError("counterexample-resource-ownership-original-invalid")
        claim_attempt = ArtifactRef.model_validate(claim.get("attempt_ref"))
        claim_intent = _validated_attempt_intent(
            json.loads(_bound_read(root, claim_attempt, captured_artifacts))
        )
        expected_path = (
            Path(claim_attempt.path).parent / f"resource-owner-{identifier}.json"
        )
        expected_folder = _attempts_dir(root, plan).relative_to(root)
        if (
            Path(claim_attempt.path).parent.parent != expected_folder
            or Path(claim_attempt.path).name != "intent.json"
            or reference.path != expected_path.as_posix()
            or claim_intent.get("plan_digest") != intent["plan_digest"]
            or claim_intent.get("subject_id") != intent["subject_id"]
            or resources[identifier]
            not in claim_intent.get("binding", {}).get("resources", [])
            or type(claim.get("schema_version")) is not int
            or claim.get("schema_version") != 1
            or claim.get("loop_id") != plan.loop_id
            or claim.get("plan_digest") != intent["plan_digest"]
            or claim.get("subject_id") != intent["subject_id"]
            or claim.get("resource") != resources[identifier]
            or set(claim)
            != {
                "schema_version",
                "loop_id",
                "plan_digest",
                "subject_id",
                "resource",
                "attempt_ref",
                "directory_identity",
            }
        ):
            raise ValueError("counterexample-resource-ownership-binding-conflict")
        identity = claim["directory_identity"]
        if (
            not isinstance(identity, dict)
            or set(identity) != {"device", "inode"}
            or any(type(value) is not int for value in identity.values())
            or identity["device"] < 0
            or identity["inode"] <= 0
        ):
            raise ValueError("counterexample-resource-directory-identity-unavailable")
        claims[identifier] = reference
        refs.extend((reference, claim_attempt))
    return claims, tuple(refs)


def _remove_owned_directory(path: Path, expected: Mapping[str, int]) -> None:
    """所属进程已终止且目录/父路径独享；不承诺外部同时改名时的原子条件删除。"""
    if _directory_identity(path) != expected:
        raise ValueError("counterexample-resource-owner-entity-changed")
    if os.name == "posix" and _RESOURCE_FD_CLEANUP:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(path.anchor, flags)
        try:
            for part in path.parent.parts[1:]:
                next_fd = os.open(part, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            resource_fd = os.open(path.name, flags, dir_fd=parent_fd)
            try:
                opened = os.fstat(resource_fd)
                if {"device": opened.st_dev, "inode": opened.st_ino} != expected:
                    raise ValueError("counterexample-resource-owner-entity-changed")
                # 标准库负责相对 fd 的递归；最终名称删除仍依赖上面的独享静止前提。
                if _directory_identity(path) != expected:
                    raise ValueError("counterexample-resource-owner-entity-changed")
                shutil.rmtree(path.name, dir_fd=parent_fd)
            finally:
                os.close(resource_fd)
        finally:
            os.close(parent_fd)
    else:
        # Windows 沿普通目录清理语义；不把此路径描述为 POSIX fd 保护。
        shutil.rmtree(path)
    if path.exists() or path.is_symlink():
        raise ValueError("counterexample-owned-resource-cleanup-incomplete")


def _validate_resources(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    step: ExecutionStep,
    *,
    claim: bool,
    deadline_ms: int,
    allow_initial_claim: bool = False,
    attempt_ref: ArtifactRef | None = None,
    ownership_refs: Mapping[str, ArtifactRef] | None = None,
    on_claim: Callable[[str, ArtifactRef], None] | None = None,
) -> dict[str, ArtifactRef]:
    _validate_plan_resource_roots(plan)
    _validate_step_bindings(contract, plan, step)
    project = Path(step.binding.project_root).resolve(strict=True)
    claims: dict[str, ArtifactRef] = {}
    for resource in step.binding.resources:
        reference = _validate_resource(
            root,
            project,
            plan,
            step,
            resource,
            claim=claim,
            deadline_ms=deadline_ms,
            allow_initial_claim=allow_initial_claim,
            attempt_ref=attempt_ref,
            ownership_ref=(ownership_refs or {}).get(resource.id),
            on_claim=on_claim,
        )
        if reference is not None:
            claims[resource.id] = reference
    return claims


def _validate_resource(
    root: Path,
    project: Path,
    plan: CounterexamplePlan,
    step: ExecutionStep,
    resource: ResourceBinding,
    *,
    claim: bool,
    deadline_ms: int,
    allow_initial_claim: bool = False,
    attempt_ref: ArtifactRef | None = None,
    ownership_ref: ArtifactRef | None = None,
    on_claim: Callable[[str, ArtifactRef], None] | None = None,
) -> ArtifactRef | None:
    # 首次认领许可来自调用方已验证的周期；缺少 owner 本身不能开启新周期。
    if time.time_ns() // 1_000_000 >= deadline_ms:
        raise ValueError("counterexample-resource-state-deadline-exceeded")
    resource_path = Path(resource.root)
    subject = next(item for item in plan.subjects if item.id == step.subject_id)
    _require_scratch_root(root, project, plan, resource, subject.snapshot)
    initial, _ = _initial_resource_state(root, resource.initial_state)
    missing_resource = not resource_path.exists()
    marker = resource_path / ".ai-sdlc-owner.json"
    if not allow_initial_claim and not marker.exists():
        raise ValueError("counterexample-resource-owner-missing")
    created_identity: dict[str, int] | None = None
    claim_reported = False
    try:
        if missing_resource:
            if step.kind != "reset" or any("content_ref" not in entry for entry in initial):
                raise ValueError(
                    "counterexample-missing-resource-needs-frozen-reset-and-bytes"
                )
            _require_prior_resource_cleanup(
                root, plan, step, resource, exclude_attempt=attempt_ref
            )
            if claim:
                resource_path.mkdir(parents=True, exist_ok=False)
                created_identity = _directory_identity(resource_path)
                for entry in initial:
                    content = _read_ref(
                        root, ArtifactRef.model_validate(entry["content_ref"])
                    )
                    LoopArtifactStore(resource_path).write_bytes_artifact(
                        resource_path / entry["path"], content, immutable=True
                    )
        owner: dict[str, Any] = {
            "schema_version": 2,
            "loop_id": plan.loop_id,
            "plan_digest": counterexample_digest(plan),
            "subject_id": step.subject_id,
            "resource_id": resource.id,
        }
        if ownership_ref is not None:
            original = json.loads(_read_ref(root, ownership_ref))
            if _directory_identity(resource_path) != original["directory_identity"]:
                raise ValueError("counterexample-resource-owner-entity-changed")
            owner["ownership_ref"] = ownership_ref.model_dump(mode="json")
        elif marker.exists() or not allow_initial_claim:
            raise ValueError("counterexample-resource-owner-original-missing")
        if marker.exists() and json.loads(read_stable_bytes(project, marker)) != owner:
            raise ValueError("counterexample-resource-already-owned")
        if allow_initial_claim and (not missing_resource or claim):
            # 冻结初态包含文件及其父目录；完整树读取也拒绝未声明空目录和特殊节点。
            expected = _frozen_reset_state(root, (resource,))
            actual = _resource_content_state(
                _observed_resource_state((resource,), deadline_ms=deadline_ms)
            )
            if actual != expected:
                raise ValueError("counterexample-resource-initial-state-mismatch")
        elif not missing_resource or claim:
            # 连续步骤与逐目录清理也消费当前文件归属，不重复读取业务内容。
            _resource_inventory(resource_path, deadline_ms=deadline_ms)
        if not marker.exists():
            if not allow_initial_claim:
                raise ValueError("counterexample-resource-owner-missing")
            if not claim:
                return ownership_ref
            if ownership_ref is None:
                if attempt_ref is None:
                    raise ValueError("counterexample-resource-owner-intent-required")
                claim_path = (root / attempt_ref.path).with_name(
                    f"resource-owner-{resource.id}.json"
                )
                claim_document = {
                    "schema_version": 1,
                    "loop_id": plan.loop_id,
                    "plan_digest": counterexample_digest(plan),
                    "subject_id": step.subject_id,
                    "resource": resource.model_dump(mode="json"),
                    "attempt_ref": attempt_ref.model_dump(mode="json"),
                    "directory_identity": _directory_identity(resource_path),
                }
                try:
                    ownership_ref = _write_json(root, claim_path, claim_document)
                except OSError as exc:
                    # 当前写入返回前失败时，只确认与刚产生的完整原文逐字相同的原件。
                    if on_claim is not None and claim_path.is_file():
                        if read_stable_bytes(root, claim_path) != _json_bytes(claim_document):
                            raise ValueError("counterexample-live-claim-publication-conflict") from exc
                        claim_reported = True
                        on_claim(resource.id, _ref(root, claim_path))
                    raise
                if on_claim is not None:
                    # 回调可能先记录归属再抛错，进入交接后本层不再推断它未认领。
                    claim_reported = True
                    on_claim(resource.id, ownership_ref)
                owner["ownership_ref"] = ownership_ref.model_dump(mode="json")
            LoopArtifactStore(project).write_bytes_artifact(
                marker, _json_bytes(owner), immutable=True
            )
        if ownership_ref is not None:
            original = json.loads(_read_ref(root, ownership_ref))
            if _directory_identity(resource_path) != original["directory_identity"]:
                raise ValueError("counterexample-resource-owner-entity-changed")
        return ownership_ref
    except BaseException as exc:
        # 新建但未交付归属的残缺初态由当前调用收尾；不读取尚未完整写好的初态。
        # 原有目录或已交付 on_claim 的目录仍由既有上层清理，不重复认领或删除。
        if created_identity is not None and not claim_reported:
            try:
                _remove_owned_directory(resource_path, created_identity)
            except BaseException as cleanup_exc:
                # 清理失败只能补诊断，保留原中断对象和 traceback 交给既有执行器。
                exc.add_note(
                    f"secondary resource rollback error: {type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        raise


def _completion_document(
    intent: Mapping[str, Any],
    intent_sha256: str,
    originals: Mapping[str, bytes | None],
) -> dict[str, Any]:
    """记录完成时已有原件的摘要；缺失原件不能在后来读取时补成已验证。"""
    _command_original_required(intent)
    return {
        "schema_version": 1,
        "intent_sha256": intent_sha256,
        "plan_digest": intent["plan_digest"],
        "attempt_id": intent["attempt_id"],
        "binding_digest": intent["binding_digest"],
        "ownership_nonce": intent["ownership_nonce"],
        "artifact_sha256": {
            name: hashlib.sha256(content).hexdigest() if content is not None else None
            for name, content in originals.items()
        },
    }


def _validate_completion_document(
    intent: Mapping[str, Any],
    intent_sha256: str,
    completion_raw: bytes,
    originals: Mapping[str, bytes | None],
) -> None:
    from ai_sdlc.core.quality_command import validate_controlled_receipts

    completion = json.loads(completion_raw)
    if (
        not isinstance(completion, dict)
        or type(completion.get("schema_version")) is not int
        or completion != _completion_document(intent, intent_sha256, originals)
    ):
        raise ValueError("counterexample-completion-originals-missing-or-stale")
    process, raw, cleanup = (
        json.loads(originals[name]) if originals[name] is not None else {}
        for name in ("process.json", "raw-result.json", "cleanup.json")
    )
    raw = controlled_raw_original(
        raw, cleanup, raw_present=originals["raw-result.json"] is not None
    )
    validate_controlled_receipts(
        process, raw, cleanup, ownership_nonce=intent["ownership_nonce"]
    )
    if not intent["started_at_ms"] <= raw["started_at_ms"] <= intent["deadline_ms"]:
        raise ValueError("counterexample-process-start-outside-original-window")


def _frozen_reset_state(
    root: Path,
    resources: Sequence[ResourceBinding],
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    """冻结清单的文件及必需父目录；不把额外空目录视为已恢复初态。"""
    result = []
    for resource in sorted(resources, key=lambda item: item.id):
        initial, _ = _initial_resource_state(
            root, resource.initial_state, captured_artifacts
        )
        entries = {}
        for entry in initial:
            entries[entry["path"]] = {
                "path": entry["path"],
                "kind": "file",
                "sha256": entry["sha256"],
            }
            for parent in Path(entry["path"]).parents:
                if parent.as_posix() != ".":
                    entries[parent.as_posix()] = {
                        "path": parent.as_posix(),
                        "kind": "directory",
                        "sha256": None,
                    }
        result.append(
            {
                "resource_id": resource.id,
                "files": [entries[key] for key in sorted(entries)],
            }
        )
    return result


def execute_counterexample_attempt(
    root: Path,
    contract: VerificationContract,
    plan: CounterexamplePlan,
    step_id: str,
    *,
    deadline_ms: int,
    remaining_reserve_seconds: int = 0,
    postcheck: Callable[[], None] | None = None,
    cleanup_recovery: bool = False,
    reserved_attempts: int = 0,
    cleanup_reservation_ref: ArtifactRef | None = None,
    on_started: Callable[[], None] | None = None,
) -> AttemptReceipt:
    """调用者持原 write_guard；全部历史意图占用次数，未知旧操作不重放。"""
    root = root.resolve(strict=True)
    if (
        type(cleanup_recovery) is not bool
        or type(reserved_attempts) is not int
        or reserved_attempts < 0
        or cleanup_reservation_ref is not None
        and not cleanup_recovery
    ):
        raise ValueError("counterexample-cleanup-or-reservation-input-invalid")
    validate_plan_contract(contract, plan)
    _validate_plan_resource_roots(plan)
    for planned_step in plan.steps:
        validate_quality_argv(planned_step.binding.argv)
    step = next(step for step in plan.steps if step.id == step_id)
    folder = _attempts_dir(root, plan)
    history, debts = _historical_resource_cycles(root, plan)
    prior = [(path, intent) for path, intent, _, _ in history]
    current_receipts = [
        receipt
        for _, _, old, receipt in history
        if counterexample_digest(old) == counterexample_digest(plan)
    ]
    completed_steps = {
        receipt.step_id
        for receipt in current_receipts
        if not _failed_operation(
            receipt, next(item for item in plan.steps if item.id == receipt.step_id)
        )
    }
    for receipt in current_receipts:
        if receipt.step_id == step_id:
            if postcheck is not None:
                postcheck()
            return receipt
    # 已发生的历史继续走原回执/清理读取；首次业务动作与原生封存共享准入。
    if not current_receipts and not cleanup_recovery:
        if _unfinished_predecessor_plans(root, plan, history):
            raise ValueError("counterexample-unfinished-predecessor-no-takeover")
        _validate_plan_resource_admission(
            root, plan, new_execution=True, deadline_ms=deadline_ms,
            native_initial_policy=False
        )
    pending_step_ids = {
        pending_step.id for pending_step in _pending_attempt_steps(plan, current_receipts)
    }
    for planned_step in plan.steps:
        if not cleanup_recovery or planned_step.id == step.id:
            _validate_step_bindings(contract, plan, planned_step)
            if planned_step.id in pending_step_ids:
                _validate_current_step_cwd(
                    root, plan, planned_step, allow_planned_reset=True
                )
    _validate_current_step_cwd(
        root, plan, step, allow_planned_reset=step.kind == "reset"
    )
    if prior:
        # 新调用不能延长原执行截止；每个历史意图仍占用同一总次数。
        deadline_ms = min(deadline_ms, *(intent["deadline_ms"] for _, intent in prior))
    if cleanup_recovery:
        debt = debts.get((counterexample_digest(plan), step.subject_id))
        if step.kind != "cleanup" or debt is None:
            raise ValueError("counterexample-historical-cleanup-not-owned")
        if _historical_cleanup_step(plan, debt[1], current_receipts).id != step.id:
            raise ValueError("counterexample-historical-cleanup-not-next-frozen-step")
        reservation_plan = plan
        if cleanup_reservation_ref is not None:
            reservation_plan = CounterexamplePlan.model_validate_json(
                _read_ref(root, cleanup_reservation_ref)
            )
            reservation_digest = counterexample_digest(reservation_plan)
            expected = folder.parent / f"plans/{reservation_digest}.json"
            if cleanup_reservation_ref.path != expected.relative_to(
                root
            ).as_posix() or reservation_digest == counterexample_digest(plan):
                raise ValueError("counterexample-cleanup-reservation-plan-invalid")
            validate_plan_contract(contract, reservation_plan)
            _require_loop_batch_policy(plan, reservation_plan)
            _validate_plan_resource_roots(reservation_plan)
            for planned_step in reservation_plan.steps:
                _validate_step_bindings(contract, reservation_plan, planned_step)
            validate_counterexample_snapshots(root, reservation_plan)
        # 仅使用原恢复器明确选中的不可变继任表；不在旧 cleanup 上生成业务谱系。
        pending = _shared_pending_steps(
            root,
            contract,
            reservation_plan,
            history,
            debts,
            replace_current=cleanup_reservation_ref is not None,
        )
        pending_count = max(1 + reserved_attempts, len(pending))
    else:
        if any(digest != counterexample_digest(plan) for digest, _ in debts):
            raise ValueError("counterexample-historical-resource-cleanup-required")
        pending = _shared_pending_steps(root, contract, plan, history, debts)
        pending_count = len(pending) + reserved_attempts
    if len(prior) >= plan.max_execution_attempts:
        raise ValueError("counterexample-attempt-budget-exhausted")
    if len(prior) + pending_count > plan.max_execution_attempts:
        raise ValueError("counterexample-attempt-budget-required-steps-unavailable")
    now = time.time_ns() // 1_000_000
    # 次数与时间共用冻结承诺；当前动作单独计时，不重复扣除同一计划步骤。
    current_key = (counterexample_digest(plan), step.id)
    pending_plan_digests = {id(plan): current_key[0]}
    for saved, _ in pending:
        if id(saved) not in pending_plan_digests:
            pending_plan_digests[id(saved)] = counterexample_digest(saved)
    terminal_reserve = max(
        plan.required_reserve_seconds,
        max((saved.required_reserve_seconds for saved, _ in pending), default=0),
    )
    shared_reserve = terminal_reserve + sum(
        future.binding.timeout_seconds + future.reservation_seconds + 5
        for saved, future in pending
        if (pending_plan_digests[id(saved)], future.id) != current_key
    )
    reserve = max(shared_reserve, remaining_reserve_seconds)
    if (
        now
        + 1000 * (step.binding.timeout_seconds + step.reservation_seconds + reserve + 5)
        > deadline_ms
    ):
        raise ValueError("counterexample-time-budget-reserve-unavailable")
    failure_cleanup = _failure_cleanup_dependencies(
        plan, step, current_receipts, completed_steps
    )
    # 原操作已失败时先报告业务分支停止，不以派生的依赖缺失掩盖实际根因。
    if step.kind != "cleanup" and any(
        receipt.subject_id == step.subject_id
        and _failed_operation(
            receipt, next(item for item in plan.steps if item.id == receipt.step_id)
        )
        for receipt in current_receipts
    ):
        raise ValueError("counterexample-prior-operation-failed-business-stopped")
    if (
        not set(step.depends_on) <= completed_steps
        and not failure_cleanup
        and not cleanup_recovery
    ):
        raise ValueError("counterexample-step-dependency-incomplete")
    resolved_commands: dict[str, dict[str, Any]] = {}
    if cleanup_recovery:
        _validate_cleanup_subject_live(root, plan, step, defer_file_map=True)
    else:
        validate_counterexample_snapshots(
            root, plan, _defer_file_map_subject=step.subject_id,
            _resolved_commands=resolved_commands,
        )
    resolved_command = resolved_commands.get(step.id)
    if resolved_command is None:
        resolved_command = _resolve_command(step)
    _check_resolved_command_live(resolved_command)
    subject = next(item for item in plan.subjects if item.id == step.subject_id)
    subject_manifest = _snapshot_document(_read_ref(root, subject.snapshot))
    # 同一对象的旧完成记录与活跃债务区分首次、连续运行和清理后的 reset。
    # 复用本次已读取的历史，不为认领判断重新遍历全部原件。
    subject_started = any(
        item.subject_id == step.subject_id for item in current_receipts
    )
    allow_initial_claim = (
        step.kind != "cleanup"
        and (counterexample_digest(plan), step.subject_id) not in debts
        and (not subject_started or step.kind == "reset")
    )
    ownership_refs: dict[str, ArtifactRef] = {}
    if not allow_initial_claim:
        active = debts.get((counterexample_digest(plan), step.subject_id))
        previous = next(
            (
                item
                for _, _, _, item in history
                if active is not None and item.attempt_id == active[1]["attempt_id"]
            ),
            None,
        )
        reference = (
            next(
                (
                    ref
                    for ref in previous.raw_evidence_refs
                    if ref.path.endswith("/resource-ownership.json")
                ),
                None,
            )
            if previous is not None
            else None
        )
        if reference is None or active is None:
            # 未发布旧 active 仅保留诊断，不能把现场实体补写成旧归属事实。
            raise ValueError("counterexample-resource-owner-original-missing")
        ownership_refs, _ = _resource_ownership_refs(
            root, plan, active[1], previous.attempt_ref, _read_ref(root, reference)
        )
    # 部分认领及真实命令返回后的状态核验、资源收尾使用本步预留，仍保留后续步骤及末尾发布余量。
    resource_cleanup_deadline = deadline_ms - 1000 * (reserve + 5)
    state_deadline = resource_cleanup_deadline - 1000 * step.reservation_seconds
    _validate_resources(
        root,
        contract,
        plan,
        step,
        claim=False,
        deadline_ms=state_deadline,
        allow_initial_claim=allow_initial_claim,
        ownership_refs=ownership_refs,
    )
    if postcheck is not None:
        postcheck()
    now = time.time_ns() // 1_000_000
    if (
        now
        + 1000 * (step.binding.timeout_seconds + step.reservation_seconds + reserve + 5)
        > deadline_ms
    ):
        raise ValueError("counterexample-time-budget-reserve-unavailable-after-capture")
    attempt_id = f"attempt-{secrets.token_hex(16)}"
    attempt_dir = folder / attempt_id
    nonce = secrets.token_hex(32)
    state_before = None
    if step.kind in ("observe", "acceptance"):
        state_before = _observed_resource_state(
            step.binding.resources, deadline_ms=state_deadline
        )
        if step.kind == "acceptance":
            observed = next(
                (
                    item
                    for item in current_receipts
                    if item.step_id == step.business_observation_step_id
                ),
                None,
            )
            reference = (
                next(
                    (
                        item
                        for item in observed.raw_evidence_refs
                        if item.path.endswith("/resource-state.json")
                    ),
                    None,
                )
                if observed
                else None
            )
            if (
                reference is None
                or json.loads(_read_ref(root, reference)).get("after") != state_before
            ):
                raise ValueError("counterexample-acceptance-business-state-mismatch")
    # 大目录读取同样消耗原时间；读取后仍须给命令与收尾留下完整窗口。
    if (
        time.time_ns() // 1_000_000
        + 1000 * (step.binding.timeout_seconds + step.reservation_seconds + reserve + 5)
        > deadline_ms
    ):
        raise ValueError("counterexample-time-budget-reserve-unavailable-after-capture")
    intent = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "plan_digest": counterexample_digest(plan),
        "contract_digest": plan.contract_digest,
        "candidate_digest": next(
            subject.candidate_digest
            for subject in plan.subjects
            if subject.id == step.subject_id
        ),
        "step_id": step.id,
        "kind": step.kind,
        "phase": step.phase,
        "subject_id": step.subject_id,
        "binding_digest": counterexample_digest(step.binding),
        "binding": step.binding.model_dump(mode="json"),
        "resolved_command": resolved_command,
        "source_snapshot": subject.snapshot.model_dump(mode="json"),
        "ownership_nonce": nonce,
        "started_at_ms": now,
        "deadline_ms": deadline_ms,
        "attempt_ordinal": len(prior) + 1,
        "max_execution_attempts": plan.max_execution_attempts,
        "reserved_seconds": reserve,
        "failure_cleanup_steps": failure_cleanup,
        "historical_cleanup_only": cleanup_recovery,
        "resource_ownership_required": True,
    }
    attempt_ref = _publish_attempt_intent(root, attempt_dir, intent)
    claimed_refs = dict(ownership_refs)
    original_presence = {
        item.id: Path(item.root).exists() for item in step.binding.resources
    }
    claims_complete = False

    ownership_manifest_ref: ArtifactRef | None = None

    def persist_ownership() -> None:
        nonlocal ownership_manifest_ref
        # 只发布本次 claim 已返回的真实原件；冷恢复不得以此从现场补历史材料。
        ownership_manifest_ref = _write_json(
            root,
            attempt_dir / "resource-ownership.json",
            {
                "schema_version": 1,
                "attempt_ref": attempt_ref.model_dump(mode="json"),
                "resources": {
                    identifier: reference.model_dump(mode="json")
                    for identifier, reference in claimed_refs.items()
                },
                **({"prelaunch_cleanup": True} if not claims_complete else {}),
            },
        )

    def prepare_resources() -> None:
        nonlocal ownership_refs, claims_complete
        ownership_refs = _validate_resources(
            root, contract, plan, step, claim=True, deadline_ms=state_deadline,
            allow_initial_claim=allow_initial_claim, attempt_ref=attempt_ref,
            ownership_refs=ownership_refs, on_claim=claimed_refs.__setitem__,
        )
        claimed_refs.update(ownership_refs)
        claims_complete = True
        persist_ownership()
        _validate_current_step_cwd(root, plan, step)

    error = ""
    resource_state_ref = None
    reset_state_ref = None

    def persist(name: str) -> Callable[[dict[str, object]], None]:
        def save(payload: dict[str, object]) -> None:
            _write_json(root, attempt_dir / name, payload)
            if name == "process.json":
                _check_resolved_command_live(resolved_command)
                # 原启动证明先落盘；使用质量命令已有新鲜摘要，不重复捕获全仓。
                # 当前 subject 的文件图比较从早验移到这里，覆盖声明的 ignored 输入。
                if (
                    payload.get("source_digest_before")
                    != "sha256:" + subject.candidate_digest
                ):
                    raise ValueError("counterexample-prelaunch-subject-source-stale")
                expected_files = [
                    {key: value for key, value in entry.items() if key != "content_ref"}
                    for entry in subject_manifest["files"]
                ]
                if expected_files != _file_map(
                    Path(step.binding.project_root), subject_manifest["ignored_inputs"]
                ):
                    raise ValueError("counterexample-prelaunch-subject-files-stale")
                # 宿主材料发布后再核资源；拒绝仍保留上面已落盘的真实进程原件。
                for resource in step.binding.resources:
                    _resource_inventory(Path(resource.root), deadline_ms=state_deadline)
                # 进度写入失败仍由原质量命令生成实际 raw/cleanup，不制造完成证明。
                if on_started is not None:
                    on_started()
            if name == "cleanup.json":
                # 执行器已保留真实未启动/终止事实；一次发布故障仍保留同次归属。
                # 已发布而返回失败时，原不可变写入只接受完全相同的字节。
                if not claims_complete:
                    if payload.get("launch_status") != "never_started" or payload.get("status") != "complete":
                        raise ValueError("counterexample-live-partial-claim-process-unproven")
                    # 仅本次未启动操作可收尾部分认领；不补认领余下资源、不重放业务。
                    for resource in step.binding.resources:
                        reference = claimed_refs.get(resource.id)
                        if reference is None:
                            if Path(resource.root).exists() != original_presence[resource.id]:
                                raise ValueError("counterexample-live-partial-claim-unproven")
                            _validate_resource(
                                root, Path(step.binding.project_root), plan, step, resource,
                                claim=False, deadline_ms=resource_cleanup_deadline,
                                allow_initial_claim=allow_initial_claim, attempt_ref=attempt_ref,
                            )
                            continue
                        _validate_resource(
                            root, Path(step.binding.project_root), plan, step, resource,
                            claim=False, deadline_ms=resource_cleanup_deadline,
                            allow_initial_claim=allow_initial_claim, ownership_ref=reference,
                        )
                        original = json.loads(_read_ref(root, reference))
                        _remove_owned_directory(Path(resource.root), original["directory_identity"])
                    _write_json(root, attempt_dir / "resource-cleanup.json", {
                        "schema_version": 1, "status": "complete",
                        "plan_digest": intent["plan_digest"], "subject_id": step.subject_id,
                        "ownership_nonce": nonce,
                        "resources": [item.model_dump(mode="json") for item in step.binding.resources if item.id in claimed_refs],
                    })
                if ownership_manifest_ref is None:
                    persist_ownership()
                assert ownership_manifest_ref is not None
                originals = {
                    item: read_stable_bytes(root, attempt_dir / item)
                    if (attempt_dir / item).is_file()
                    else None
                    for item in (
                        "process.json",
                        "raw-result.json",
                        "cleanup.json",
                    )
                }
                originals["resource-ownership.json"] = _read_ref(
                    root, ownership_manifest_ref
                )
                if not claims_complete:
                    originals["resource-cleanup.json"] = read_stable_bytes(root, attempt_dir / "resource-cleanup.json")
                _write_json(
                    root,
                    attempt_dir / "completion.json",
                    _completion_document(intent, attempt_ref.sha256, originals),
                )

        return save

    try:
        result = run_quality_command(
            QualityCommandOptions(
                root=Path(step.binding.project_root),
                cwd=Path(step.binding.project_root) / step.binding.cwd,
                argv=tuple(resolved_command["argv"]),
                timeout_seconds=step.binding.timeout_seconds,
                controlled=ControlledQualityOptions(
                    environment=step.binding.effective_environment,
                    stdout_path=attempt_dir / "stdout",
                    stderr_path=attempt_dir / "stderr",
                    max_output_bytes=step.binding.max_output_bytes,
                    ownership_nonce=nonce,
                    on_started=persist("process.json"),
                    on_prelaunch=prepare_resources,
                    on_raw_result=persist("raw-result.json"),
                    on_cleanup=persist("cleanup.json"),
                    deadline_ms=deadline_ms
                    - 1000 * (reserve + step.reservation_seconds + 5),
                ),
            )
        )
        completion_raw = read_stable_bytes(root, attempt_dir / "completion.json")
        completion_originals = {
            name: read_stable_bytes(root, attempt_dir / name)
            if (attempt_dir / name).is_file()
            else None
            for name in ("process.json", "raw-result.json", "cleanup.json")
        }
        assert ownership_manifest_ref is not None
        completion_originals["resource-ownership.json"] = _read_ref(
            root, ownership_manifest_ref
        )
        if not claims_complete:
            completion_originals["resource-cleanup.json"] = read_stable_bytes(root, attempt_dir / "resource-cleanup.json")
        _validate_completion_document(
            intent,
            attempt_ref.sha256,
            completion_raw,
            completion_originals,
        )
        raw_result = json.loads(completion_originals["raw-result.json"])
        if raw_result["launch_error"] or raw_result["output_io_error"]:
            raise ValueError("counterexample-command-infrastructure-error")
        if (
            json.loads(read_stable_bytes(root, attempt_dir / "cleanup.json"))["status"]
            != "complete"
        ):
            raise ValueError(
                "counterexample-process-cleanup-unproven-resources-retained"
            )
        if step.kind == "reset" and result.successful:
            _validate_resources(
                root,
                contract,
                plan,
                step,
                claim=False,
                deadline_ms=resource_cleanup_deadline,
                ownership_refs=ownership_refs,
            )
            expected_reset = _frozen_reset_state(root, step.binding.resources)
            actual_reset = _resource_content_state(
                _observed_resource_state(
                    step.binding.resources, deadline_ms=resource_cleanup_deadline
                )
            )
            reset_state_ref = _write_json(
                root,
                attempt_dir / "reset-state.json",
                {
                    "schema_version": 1,
                    "plan_digest": counterexample_digest(plan),
                    "step_id": step.id,
                    "subject_id": step.subject_id,
                    "attempt_id": attempt_id,
                    "binding_digest": counterexample_digest(step.binding),
                    "ownership_nonce": nonce,
                    "resources": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            step.binding.resources, key=lambda item: item.id
                        )
                    ],
                    "expected": expected_reset,
                    "actual": actual_reset,
                },
            )
            if expected_reset != actual_reset:
                raise ValueError("counterexample-reset-initial-state-not-restored")
        if state_before is not None:
            state_after = _observed_resource_state(
                step.binding.resources, deadline_ms=resource_cleanup_deadline
            )
            resource_state_ref = _write_json(
                root,
                attempt_dir / "resource-state.json",
                {
                    "schema_version": 1,
                    "plan_digest": counterexample_digest(plan),
                    "step_id": step.id,
                    "subject_id": step.subject_id,
                    "attempt_id": attempt_id,
                    "binding_digest": counterexample_digest(step.binding),
                    "ownership_nonce": nonce,
                    "resources": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            step.binding.resources, key=lambda item: item.id
                        )
                    ],
                    "before": state_before,
                    "after": state_after,
                },
            )
            if state_before != state_after:
                raise ValueError("counterexample-observation-resource-state-mutated")
        if result.source_digest_before != result.source_digest_after:
            raise ValueError("counterexample-executed-source-changed")
        if postcheck is not None:
            postcheck()
        _validate_resources(
            root,
            contract,
            plan,
            step,
            claim=False,
            deadline_ms=resource_cleanup_deadline,
            ownership_refs=ownership_refs,
        )
        if step.kind == "cleanup":
            cleanup = json.loads(read_stable_bytes(root, attempt_dir / "cleanup.json"))
            if (
                cleanup.get("status") != "complete"
                or cleanup.get("ownership_nonce") != nonce
                or not result.successful
            ):
                raise ValueError(
                    "counterexample-process-cleanup-unproven-resources-retained"
                )
            for resource in step.binding.resources:
                # 逐个删除前复核；前一个目录的清理不能授权已变化的后一个目录。
                _validate_resource(
                    root,
                    Path(step.binding.project_root).resolve(strict=True),
                    plan,
                    step,
                    resource,
                    claim=False,
                    deadline_ms=resource_cleanup_deadline,
                    ownership_ref=ownership_refs[resource.id],
                )
                original = json.loads(_read_ref(root, ownership_refs[resource.id]))
                _remove_owned_directory(
                    Path(resource.root), original["directory_identity"]
                )
            if any(Path(resource.root).exists() for resource in step.binding.resources):
                raise ValueError("counterexample-owned-resource-cleanup-incomplete")
            _write_json(
                root,
                attempt_dir / "resource-cleanup.json",
                {
                    "schema_version": 1,
                    "status": "complete",
                    "plan_digest": counterexample_digest(plan),
                    "subject_id": step.subject_id,
                    "ownership_nonce": nonce,
                    "resources": [
                        resource.model_dump(mode="json")
                        for resource in step.binding.resources
                    ],
                },
            )
        _write_json(
            root,
            attempt_dir / "postcheck.json",
            {
                "status": "passed",
                "source_digest": source_digest_sha256(result.source_digest_after),
                "binding_digest": counterexample_digest(step.binding),
                "completion_sha256": hashlib.sha256(completion_raw).hexdigest(),
                **(
                    {"reset_state_sha256": reset_state_ref.sha256}
                    if reset_state_ref is not None
                    else {}
                ),
                **(
                    {"resource_state_sha256": resource_state_ref.sha256}
                    if resource_state_ref is not None
                    else {}
                ),
            },
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _write_json(root, attempt_dir / "postcheck-error.json", {"error": error})
    receipt = _receipt(root, plan, attempt_ref)
    if error and receipt.status == "completed":
        receipt = receipt.model_copy(update={"status": "infrastructure_error"})
    _write_json(root, attempt_dir / "receipt.json", receipt)
    return receipt


def _receipt(
    root: Path,
    plan: CounterexamplePlan,
    reference: ArtifactRef,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> AttemptReceipt:
    folder = root / reference.path
    folder = folder.parent

    def read(name: str) -> bytes | None:
        path = folder / name
        if captured_artifacts is not None:
            return captured_artifacts.get(path.relative_to(root).as_posix())
        return read_stable_bytes(root, path) if path.exists() else None

    intent_raw = read("intent.json")
    if intent_raw is None or hashlib.sha256(intent_raw).hexdigest() != reference.sha256:
        raise ValueError("counterexample-attempt-intent-missing-or-stale")
    intent = _validated_attempt_intent(json.loads(intent_raw))
    originals = {
        name: read(name) for name in ("process.json", "raw-result.json", "cleanup.json")
    }
    ownership_required = intent.get("resource_ownership_required", False)
    ownership_raw = read("resource-ownership.json") if ownership_required else None
    ownership_originals: tuple[ArtifactRef, ...] = ()
    if ownership_raw is not None:
        # 先复用完整形状与身份校验，再读取模式字段；畸形原件仍按既有拒绝返回。
        _, ownership_originals = _resource_ownership_refs(
            root, plan, intent, reference, ownership_raw, captured_artifacts
        )
    prelaunch_cleanup = bool(
        ownership_raw is not None
        and json.loads(ownership_raw).get("prelaunch_cleanup") is True
    )
    if ownership_required:
        originals["resource-ownership.json"] = ownership_raw
    if prelaunch_cleanup:
        originals["resource-cleanup.json"] = read("resource-cleanup.json")
    process, raw, cleanup = (
        json.loads(originals[name]) if originals[name] is not None else {}
        for name in ("process.json", "raw-result.json", "cleanup.json")
    )
    postcheck_raw = read("postcheck.json")
    postcheck = json.loads(postcheck_raw) if postcheck_raw is not None else {}
    if not all(
        isinstance(document, dict) for document in (process, raw, cleanup, postcheck)
    ):
        raise ValueError("counterexample-attempt-receipt-shape-invalid")
    raw = controlled_raw_original(
        raw, cleanup, raw_present=originals["raw-result.json"] is not None
    )
    completion_raw = read("completion.json")
    # 缺页捕获仍是可读取的未知状态；只能消费捕获原件，不能向现场补读。
    proof_complete = bool(
        completion_raw is not None
        and bool(raw)
        and originals["cleanup.json"] is not None
        and (not ownership_required or ownership_raw is not None)
        and (
            originals["process.json"] is not None
            or isinstance(raw, dict)
            and raw.get("launch_status") == "never_started"
        )
    )
    if proof_complete:
        _validate_completion_document(
            intent, reference.sha256, completion_raw, originals
        )
    if prelaunch_cleanup:
        expected_resources = [
            item for item in intent["binding"]["resources"]
            if item["id"] in json.loads(ownership_raw)["resources"]
        ]
        cleanup_original = originals["resource-cleanup.json"]
        if not proof_complete or cleanup_original is None:
            proof_complete = False
        elif (
            raw.get("launch_status") != "never_started"
            or json.loads(cleanup_original) != {
                "schema_version": 1, "status": "complete",
                "plan_digest": intent["plan_digest"], "subject_id": intent["subject_id"],
                "ownership_nonce": intent["ownership_nonce"], "resources": expected_resources,
            }
        ):
            raise ValueError("counterexample-prelaunch-resource-cleanup-invalid")
    original_step = next((item for item in plan.steps if item.id == intent.get("step_id")), None)
    if original_step is None or intent["binding"] != original_step.binding.model_dump(mode="json"):
        raise ValueError("counterexample-original-command-step-mismatch")
    command = (
        _read_resolved_command(original_step, intent["resolved_command"])
        if _command_original_required(intent) else None
    )
    if command is not None:
        source_snapshot = next(item.snapshot for item in plan.subjects if item.id == original_step.subject_id)
        if intent.get("source_snapshot") != source_snapshot.model_dump(mode="json"):
            raise ValueError("counterexample-original-command-snapshot-mismatch")
        manifest = _snapshot_document(_bound_read(root, source_snapshot, captured_artifacts))
        _validate_original_command_source(plan, original_step, command, manifest)
    completed = bool(
        proof_complete
        and originals["raw-result.json"] is not None
        and not cleanup.get("raw_result_persistence_error")
        and command is not None
        and process
        and postcheck.get("status") == "passed"
        and postcheck.get("binding_digest") == intent["binding_digest"]
        and postcheck.get("source_digest") == intent["candidate_digest"]
        and postcheck.get("completion_sha256")
        == hashlib.sha256(completion_raw).hexdigest()
    )
    if process and process.get("ownership_nonce") != intent["ownership_nonce"]:
        raise ValueError("counterexample-process-ownership-conflict")
    if intent["kind"] == "cleanup":
        resource_cleanup_raw = read("resource-cleanup.json")
        resource_cleanup = (
            json.loads(resource_cleanup_raw) if resource_cleanup_raw is not None else {}
        )
        if resource_cleanup != {
            "schema_version": 1,
            "status": "complete",
            "plan_digest": intent["plan_digest"],
            "subject_id": intent["subject_id"],
            "ownership_nonce": intent["ownership_nonce"],
            "resources": intent["binding"]["resources"],
        }:
            completed = False
            cleanup = {**cleanup, "status": "incomplete"}
    if (
        raw
        and raw.get("ownership_nonce") != intent["ownership_nonce"]
        or cleanup
        and cleanup.get("ownership_nonce") != intent["ownership_nonce"]
    ):
        raise ValueError("counterexample-attempt-ownership-conflict")
    refs = []
    if prelaunch_cleanup and proof_complete:
        refs.append(ArtifactRef(
            path=(folder / "resource-cleanup.json").relative_to(root).as_posix(),
            sha256=hashlib.sha256(originals["resource-cleanup.json"]).hexdigest(),
        ))
    if ownership_raw is not None:
        refs.extend(ownership_originals)
        refs.append(
            ArtifactRef(
                path=(folder / "resource-ownership.json").relative_to(root).as_posix(),
                sha256=hashlib.sha256(ownership_raw).hexdigest(),
            )
        )
    if completion_raw is not None:
        refs.append(
            ArtifactRef(
                path=(folder / "completion.json").relative_to(root).as_posix(),
                sha256=hashlib.sha256(completion_raw).hexdigest(),
            )
        )
    if intent["kind"] == "reset":
        from ai_sdlc.core.counterexample_models import ResourceBinding

        reset_raw = read("reset-state.json")
        successful_reset = raw.get("exit_code") == 0 and not raw.get("timed_out")
        if reset_raw is None:
            if successful_reset:
                completed = False
        else:
            reset = json.loads(reset_raw)
            resources = tuple(
                ResourceBinding.model_validate(item)
                for item in intent["binding"]["resources"]
            )
            expected_state = _frozen_reset_state(root, resources, captured_artifacts)
            expected_identity = {
                "schema_version": 1,
                **{
                    key: intent[key]
                    for key in (
                        "plan_digest",
                        "step_id",
                        "subject_id",
                        "attempt_id",
                        "binding_digest",
                        "ownership_nonce",
                    )
                },
                "resources": [
                    item.model_dump(mode="json")
                    for item in sorted(resources, key=lambda item: item.id)
                ],
                "expected": expected_state,
            }
            if (
                not isinstance(reset, dict)
                or type(reset.get("schema_version")) is not int
                or set(reset) != set(expected_identity) | {"actual"}
                or any(reset[key] != value for key, value in expected_identity.items())
                or not isinstance(reset["actual"], list)
            ):
                raise ValueError("counterexample-reset-proof-binding-invalid")
            reset_sha256 = hashlib.sha256(reset_raw).hexdigest()
            if postcheck.get("status") == "passed" and (
                postcheck.get("reset_state_sha256") != reset_sha256
                or successful_reset
                and reset["actual"] != expected_state
            ):
                raise ValueError("counterexample-reset-proof-initial-state-mismatch")
            refs.append(
                ArtifactRef(
                    path=(folder / "reset-state.json").relative_to(root).as_posix(),
                    sha256=reset_sha256,
                )
            )
    for name in ("stdout", "stderr"):
        content = read(name)
        if content is None:
            completed = False
            continue
        ref = ArtifactRef(
            path=(folder / name).relative_to(root).as_posix(),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        refs.append(ref)
        if raw and raw.get(name + "_sha256") != ref.sha256:
            raise ValueError("counterexample-raw-output-drift")
    if intent["kind"] in ("observe", "acceptance"):
        state_raw = read("resource-state.json")
        if state_raw is None:
            completed = False
        else:
            if (
                postcheck.get("status") == "passed"
                and postcheck.get("resource_state_sha256")
                != hashlib.sha256(state_raw).hexdigest()
            ):
                raise ValueError("counterexample-resource-state-proof-drift")
            refs.append(
                ArtifactRef(
                    path=(folder / "resource-state.json").relative_to(root).as_posix(),
                    sha256=hashlib.sha256(state_raw).hexdigest(),
                )
            )
    return AttemptReceipt(
        attempt_id=intent["attempt_id"],
        attempt_ref=reference,
        plan_digest=intent["plan_digest"],
        contract_digest=intent["contract_digest"],
        candidate_digest=intent["candidate_digest"],
        step_id=intent["step_id"],
        subject_id=intent["subject_id"],
        binding_digest=intent["binding_digest"],
        ownership_nonce=intent["ownership_nonce"],
        status="completed"
        if completed
        and raw
        and not raw.get("launch_error")
        and not raw.get("output_io_error")
        else "infrastructure_error"
        if proof_complete and raw and (
            cleanup.get("raw_result_persistence_error")
            or read("postcheck-error.json") is not None
        )
        else "execution_unknown",
        started_at_ms=intent["started_at_ms"],
        ended_at_ms=raw.get("ended_at_ms"),
        exit_code=raw.get("exit_code"),
        timed_out=raw.get("timed_out", False),
        output_truncated=raw.get("output_truncated", False),
        cleanup_status=cleanup.get("status", "unknown")
        if proof_complete
        else "unknown",
        raw_evidence_refs=tuple({ref.path: ref for ref in refs}.values()),
    )


def recover_counterexample_attempt(
    root: Path,
    plan: CounterexamplePlan,
    attempt_ref: ArtifactRef,
    *,
    postcheck: Callable[[], None] | None = None,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> AttemptReceipt:
    """冷恢复只消费原始完整证明；没有归属证明时既不重放，也不按旧 PID 清理。"""
    root = root.resolve(strict=True)
    path = _path(root, attempt_ref.path)
    path.relative_to(_attempts_dir(root, plan))
    if captured_artifacts is not None and postcheck is not None:
        raise ValueError("counterexample-captured-recovery-must-be-read-only")
    receipt = _receipt(root, plan, attempt_ref, captured_artifacts=captured_artifacts)
    if receipt.contract_digest != plan.contract_digest:
        raise ValueError("counterexample-prior-attempt-contract-conflict")
    if receipt.plan_digest == counterexample_digest(plan):
        step = next(step for step in plan.steps if step.id == receipt.step_id)
        if receipt.binding_digest != counterexample_digest(step.binding):
            raise ValueError("counterexample-attempt-binding-conflict")
    if receipt.status == "completed" and postcheck is not None:
        postcheck()
    return receipt


def attempt_artifact_refs(
    root: Path,
    attempt_ref: ArtifactRef,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[ArtifactRef, ...]:
    """展开当前已有尝试事实；捕获读取不能从外部工作区补缺失内容。"""
    root = root.resolve(strict=True)
    folder = _path(root, attempt_ref.path).parent
    refs = []
    for name in (
        "intent.json",
        "process.json",
        "raw-result.json",
        "cleanup.json",
        "completion.json",
        "resource-cleanup.json",
        "resource-state.json",
        "resource-ownership.json",
        "reset-state.json",
        "postcheck.json",
        "postcheck-error.json",
        "receipt.json",
        "stdout",
        "stderr",
    ):
        path = folder / name
        relative = path.relative_to(root).as_posix()
        if captured_artifacts is None:
            raw = read_stable_bytes(root, path) if path.exists() else None
        else:
            raw = captured_artifacts.get(relative)
        if raw is not None:
            refs.append(
                ArtifactRef(path=relative, sha256=hashlib.sha256(raw).hexdigest())
            )
    if not any(ref == attempt_ref for ref in refs):
        raise ValueError("counterexample-attempt-intent-missing-or-stale")
    intent = _validated_attempt_intent(
        json.loads(_bound_read(root, attempt_ref, captured_artifacts))
    )
    if _command_original_required(intent):
        source_snapshot = ArtifactRef.model_validate(intent.get("source_snapshot"))
        _bound_read(root, source_snapshot, captured_artifacts)
        refs.append(source_snapshot)
    ownership = next(
        (ref for ref in refs if ref.path.endswith("/resource-ownership.json")), None
    )
    if ownership is not None:
        document = json.loads(_bound_read(root, ownership, captured_artifacts))
        if not isinstance(document, dict) or not isinstance(
            document.get("resources"), dict
        ):
            raise ValueError("counterexample-resource-ownership-original-invalid")
        # 这里只展开已有原件；结构与周期身份由原 receipt 读取器统一验证。
        for value in document["resources"].values():
            reference = ArtifactRef.model_validate(value)
            claim = json.loads(_bound_read(root, reference, captured_artifacts))
            if not isinstance(claim, dict):
                raise ValueError("counterexample-resource-ownership-original-invalid")
            claim_attempt = ArtifactRef.model_validate(claim.get("attempt_ref"))
            _validated_attempt_intent(
                json.loads(_bound_read(root, claim_attempt, captured_artifacts))
            )
            refs.extend((reference, claim_attempt))
        if document.get("attempt_ref") != attempt_ref.model_dump(mode="json"):
            raise ValueError("counterexample-resource-ownership-binding-conflict")
        if intent.get("resource_ownership_required") is not True:
            raise ValueError("counterexample-resource-ownership-policy-invalid")
    return tuple({ref.path: ref for ref in refs}.values())


def collect_observation(
    contract: VerificationContract,
    plan: CounterexamplePlan,
    attempt_receipt: AttemptReceipt,
    owned_raw_evidence: Sequence[OwnedRawEvidence],
) -> BusinessObservation | AcceptanceObservation | None:
    """从完整独立观察 JSON 构造数据；失败和退出码不会在此变成业务判定。"""
    step = next(step for step in plan.steps if step.id == attempt_receipt.step_id)
    if step.kind not in ("observe", "acceptance"):
        return None
    subject = next(
        subject for subject in plan.subjects if subject.id == step.subject_id
    )
    witness = next(
        witness for witness in plan.witnesses if witness.id == subject.witness_id
    )
    oracle = next(
        obligation.oracle_spec
        for obligation in contract.obligations
        if obligation.id == subject.obligation_id
    )
    raw = next(
        (
            raw
            for raw in owned_raw_evidence
            if raw.attempt_id == attempt_receipt.attempt_id
            and raw.ref.path.endswith("/stdout")
        ),
        None,
    )
    if raw is None:
        return None
    payload = None
    reason = ""
    try:

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("observation-protocol-duplicate-key")
                result[key] = value
            return result

        payload = json.loads(raw.content, object_pairs_hook=unique_object)
        if (
            type(payload) is not dict
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1
        ):
            raise ValueError("observation-schema-invalid")
        if (
            not raw.complete
            or not attempt_receipt.normally_completed
        ):
            raise ValueError("observation-execution-incomplete")
    except (ValueError, TypeError) as exc:
        reason = str(exc)
        payload = None
    identity: dict[str, Any] = dict(
        subject_id=subject.id,
        subject_role=subject.role,
        candidate_digest=subject.candidate_digest,
        plan_digest=counterexample_digest(plan),
        attempt_id=attempt_receipt.attempt_id,
        step_id=step.id,
        assertion_id=step.assertion_id,
        raw_evidence_ref=raw.ref,
    )
    if step.kind == "observe":
        value = None
        if payload is not None:
            try:
                if (
                    set(payload) != {"schema_version", "typed_actual"}
                    or attempt_receipt.exit_code != 0
                ):
                    raise ValueError("business-observer-protocol-or-exit-invalid")
                value = TypedValue.model_validate(payload["typed_actual"])
            except (ValueError, TypeError) as exc:
                reason = str(exc)
        return BusinessObservation.model_validate(
            {
                **identity,
                "witness_ref": witness.input_ref,
                "typed_actual": value,
                "collection_method": oracle.observation_method,
                "observation_status": "collected"
                if value is not None
                else "parse_error",
                "reason": reason,
            }
        )
    fields: dict[str, Any] = dict(
        reached=False, assertion_result="unknown", failure_reason="protocol_error"
    )
    if payload is not None:
        try:
            if (
                set(payload)
                != {
                    "schema_version",
                    "assertion_id",
                    "reached",
                    "assertion_result",
                    "failure_reason",
                }
                or payload["assertion_id"] != step.assertion_id
            ):
                raise ValueError("acceptance-observer-protocol-invalid")
            fields = {key: payload[key] for key in fields}
            return AcceptanceObservation.model_validate(
                {
                    **identity,
                    "acceptance_version": step.acceptance_version,
                    "acceptance_digest": plan.v0_digest
                    if step.acceptance_version == "V0"
                    else plan.v1_digest,
                    **fields,
                }
            )
        except (ValueError, TypeError):
            fields = dict(
                reached=False,
                assertion_result="unknown",
                failure_reason="protocol_error",
            )
    return AcceptanceObservation.model_validate(
        {
            **identity,
            "acceptance_version": step.acceptance_version,
            "acceptance_digest": plan.v0_digest
            if step.acceptance_version == "V0"
            else plan.v1_digest,
            **fields,
        }
    )


def read_owned_raw_evidence(
    root: Path,
    receipt: AttemptReceipt,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[OwnedRawEvidence, ...]:
    result = []
    for reference in receipt.raw_evidence_refs:
        if isinstance(captured_artifacts, _CurrentStateMaterial):
            content = captured_artifacts.read_reference(reference)
        elif captured_artifacts is None:
            content = _read_ref(root, reference)
        else:
            content = captured_artifacts.get(reference.path)
        if content is None or hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("counterexample-raw-content-missing-or-stale")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue  # 非法协议字节仍由原始工件/摘要保留，不替换字符冒充可解析观察。
        result.append(
            OwnedRawEvidence(
                ref=reference,
                attempt_id=receipt.attempt_id,
                ownership_nonce=receipt.ownership_nonce,
                # 输出字节完整不代表正常退出；业务准入由 normally_completed 判断。
                complete=receipt.status == "completed" and not receipt.output_truncated,
                content=text,
            )
        )
    return tuple(result)

"""Deterministic local runtime for the Loop Engine design-contract loop."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ai_sdlc.core.design_contract_checks import (
    _verify_design_document_snapshot,
    analyze_design_contract,
    render_report_markdown,
)
from ai_sdlc.core.design_contract_models import (
    CURRENT_DESIGN_CONTRACT_PATH,
    ContractCoverageItem,
    DesignContractArtifactRef,
    DesignContractCheckOptions,
    DesignContractClose,
    DesignContractCloseOptions,
    DesignContractCommandResult,
    DesignContractCommandStatus,
    DesignContractCommandSummary,
    DesignContractCoverageMatrix,
    DesignContractCurrentPointer,
    DesignContractInput,
    DesignContractNextGuidance,
    DesignContractReport,
)
from ai_sdlc.core.design_contract_store import (
    DESIGN_CHECK_PENDING,
    DesignContractArtifacts,
    _design_contract_loop_identity_issue,
    _resolve_design_contract_loop_run_identity,
    append_unique,
    build_contract_input,
    design_contract_artifacts,
    design_contract_input_digest,
    read_loop_run,
    read_report,
    read_verification_contract,
    repo_relative_path,
    resolve_loop_id,
    resolve_work_item_dir,
    validate_explicit_loop_id,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import (
    LoopRound,
    LoopRun,
    LoopStatus,
    LoopType,
    utc_now_iso,
    validate_decision_identity,
)
from ai_sdlc.core.loop_resource_lock import (
    _ImplementationWriteLockError,
    _stage_write_guard,
)
from ai_sdlc.core.loop_stage_input import (
    preserve_stage_run,
    validate_stage_material_update,
)
from ai_sdlc.core.requirement_loop import (
    RequirementFreeze,
    RequirementIntake,
    _requirement_artifacts,
    _requirement_intake_digest,
    _requirement_loop_identity_issue,
    _RequirementArtifacts,
    _resolve_requirement_loop_run_path,
)
from ai_sdlc.core.requirement_loop import (
    _read_loop_run as _read_requirement_loop_run,
)
from ai_sdlc.core.requirement_loop import (
    _validate_explicit_loop_id as _validate_requirement_loop_id,
)
from ai_sdlc.core.requirement_repair_gate import validate_frozen_requirement_repair
from ai_sdlc.core.review_kernel import (
    ReviewInputValidator,
    revalidate_review_input_at_transition,
)
from ai_sdlc.core.stable_file_read import (
    _stable_regular_file_exists,
    read_stable_bytes,
)


class _DesignPublicationDriftError(ValueError):
    """已识别的发布原件漂移；交给现有同 Loop 恢复入口处理。"""


def check_design_contract_loop(
    options: DesignContractCheckOptions,
) -> DesignContractCommandResult:
    prepared = _prepare_design_check(options)
    if isinstance(prepared, DesignContractCommandResult):
        return prepared
    root, _, loop_id, _, _ = prepared
    with ExitStack() as locks:
        try:
            locks.enter_context(_stage_write_guard(root, 'design-contract', loop_id))
        except _ImplementationWriteLockError as exc:
            return _blocked_result(str(exc), loop_id=loop_id).model_copy(
                update={"dry_run": options.dry_run}
            )
        artifacts = design_contract_artifacts(root, loop_id)
        if _design_publication_pending(artifacts):
            if options.dry_run:
                return _pending_publication_result(loop_id, root=root)
            try:
                _recover_design_publication(root, artifacts)
            except (ValueError, OSError) as exc:
                return _pending_publication_result(loop_id, str(exc), root=root)
        return _check_design_contract_loop_locked(replace(options, loop_id=loop_id))


def _check_design_contract_loop_locked(
    options: DesignContractCheckOptions,
) -> DesignContractCommandResult:
    """Check formal docs for implementation-readiness and persist artifacts."""

    try:
        validate_decision_identity(
            LoopType.DESIGN_CONTRACT, options.decision_mode, options.decision_capability
        )
    except ValueError as exc:
        return _blocked_result(str(exc))
    prepared = _prepare_design_check(options)
    if isinstance(prepared, DesignContractCommandResult):
        return prepared
    root, work_item_dir, loop_id, artifacts, planned_refs = prepared
    existing_issue = _existing_design_check_artifact_issue(root, artifacts)
    if existing_issue:
        return _blocked_result(existing_issue, loop_id=loop_id, artifacts=planned_refs)

    # 新合同引用原 Requirement 身份；首次解析时不能仍持空的上游 ID。
    verification_requirement_id = options.requirement_loop_id
    if options.verification_contract:
        verification_requirement_id, blocker, next_action = (
            _required_requirement_loop_id(root, options.requirement_loop_id)
        )
        if blocker:
            return _blocked_result(
                blocker,
                loop_id=loop_id,
                next_action=next_action,
                artifacts=planned_refs,
            )
    captured_contract: dict[str, bytes] = {}
    built_input = _build_checked_contract_input(
        root,
        loop_id,
        work_item_dir,
        verification_requirement_id,
        planned_refs,
        decision_mode=options.decision_mode,
        decision_capability=options.decision_capability,
        verification_contract=options.verification_contract,
        captured_artifacts=captured_contract,
    )
    if isinstance(built_input, DesignContractCommandResult):
        return built_input
    contract_input = built_input
    if artifacts.loop_run_path.is_file():
        previous = read_loop_run(artifacts.loop_run_path, root=root)
        if (previous.decision_mode, previous.decision_capability) != (
            options.decision_mode,
            options.decision_capability,
        ):
            return _blocked_result("design-decision-identity-change", loop_id=loop_id)
    resolved_requirement_loop_id, requirement_blocker, requirement_next_action = (
        _required_requirement_loop_id(root, contract_input.requirement_loop_id)
    )
    if requirement_blocker:
        return _blocked_result(
            requirement_blocker,
            loop_id=loop_id,
            next_action=requirement_next_action,
            artifacts=planned_refs,
        )
    contract_input = contract_input.model_copy(
        update={"requirement_loop_id": resolved_requirement_loop_id}
    )
    requirement_blocker, requirement_next_action, prerequisite = _requirement_loop_gate(
        root,
        contract_input.requirement_loop_id,
        work_item_id=contract_input.work_item_id,
    )
    if requirement_blocker:
        return _blocked_result(
            requirement_blocker,
            loop_id=loop_id,
            next_action=requirement_next_action,
            artifacts=planned_refs,
        )
    contract_input = contract_input.model_copy(update=prerequisite)
    try:
        previous_input = (
            DesignContractInput.model_validate_json(
                read_stable_bytes(root, artifacts.input_path)
            )
            if artifacts.input_path.is_file()
            else None
        )
        if previous_input is not None and artifacts.loop_run_path.is_file():
            previous_run = read_loop_run(artifacts.loop_run_path, root=root)
            if previous_run.input_digest != design_contract_input_digest(
                previous_input
            ):
                raise ValueError("design-contract-persisted-input-identity-mismatch")
        if (
            previous_input is not None
            and previous_input.verification_contract_ref
            and previous_input.verification_contract_ref != contract_input.verification_contract_ref
        ):
            # 旧合同同样进入写前复读集合，校验后损坏不能发布指向它的新历史记录。
            captured_contract[previous_input.verification_contract_ref] = read_stable_bytes(
                root, root / previous_input.verification_contract_ref
            )
        revision = validate_stage_material_update(
            root, "design-contract", artifacts.loop_dir, previous_input, contract_input,
            verification_contract_bytes=captured_contract.get(
                contract_input.verification_contract_ref
            ),
        )
        if (
            previous_input is not None
            and contract_input.decision_capability == "stage-simulation-v1"
        ):
            contract_input = contract_input.model_copy(
                update={"created_at": previous_input.created_at}
            )
    except (OSError, ValueError) as exc:
        return _blocked_result(str(exc), loop_id=loop_id, artifacts=planned_refs)
    if options.dry_run:
        return DesignContractCommandResult(
            status=DesignContractCommandStatus.DRY_RUN,
            result="Design-contract loop dry run.",
            loop_id=loop_id,
            loop_status=LoopStatus.CREATED,
            work_item_id=contract_input.work_item_id,
            work_item_path=contract_input.work_item_path,
            dry_run=True,
            next_action="Run ai-sdlc loop design-contract check without --dry-run.",
            next_guidance=DesignContractNextGuidance(
                command=f"ai-sdlc loop design-contract check --wi {contract_input.work_item_path}",
                reason="Dry run does not write artifacts; rerun without --dry-run to persist the contract report.",
                requires_model=False,
                writes_artifacts=True,
                writes_code=False,
                safety="writes_project_artifacts",
                evidence=[
                    contract_input.spec_path,
                    contract_input.plan_path,
                    contract_input.tasks_path,
                ],
            ),
            artifacts=planned_refs,
            design_contract=_command_summary(
                contract_input,
                status=LoopStatus.CREATED,
                artifacts=planned_refs,
            ),
        )

    report = analyze_design_contract(root, contract_input)
    report.next_action = _next_action_for_report(report)
    existing_issue = _existing_design_check_artifact_issue(root, artifacts)
    if existing_issue:
        return _blocked_result(
            existing_issue,
            loop_id=loop_id,
            artifacts=planned_refs,
        )
    loop_run = _build_loop_run(
        contract_input=contract_input,
        report=report,
        loop_status=report.status,
        artifacts=artifacts,
        root=root,
    )
    previous_run = (
        read_loop_run(artifacts.loop_run_path, root=root)
        if artifacts.loop_run_path.is_file()
        else None
    )
    loop_run = preserve_stage_run(previous_run, loop_run, revision=revision)
    if contract_input.verification_capability is not None:
        # 写引用前复验最初捕获的来源；持久对象不可被同 Loop 重检覆盖。
        try:
            read_verification_contract(
                root, contract_input, captured_artifacts=captured_contract
            )
            for path, content in captured_contract.items():
                if (
                    path != contract_input.verification_contract_ref
                    and read_stable_bytes(root, root / path) != content
                ):
                    raise ValueError("counterexample-contract-source-drift")
            LoopArtifactStore(root).write_bytes_artifact(
                root / contract_input.verification_contract_ref,
                captured_contract[contract_input.verification_contract_ref],
                immutable=True,
            )
        except (OSError, ValueError) as exc:
            return _blocked_result(str(exc), loop_id=loop_id, artifacts=planned_refs)
    try:
        _write_check_artifacts(root, contract_input, report, loop_run, artifacts)
    except (OSError, _DesignPublicationDriftError) as exc:
        return _pending_publication_result(
            loop_id, str(exc), root=root, fallback_input=contract_input
        )
    return _result_from_report(
        report,
        artifacts=artifacts.refs(root),
        result=(
            "Design contract passed."
            if not report.blocker_count
            else "Design contract needs fixes."
        ),
    )


def _existing_design_check_artifact_issue(
    root: Path,
    artifacts: DesignContractArtifacts,
) -> str:
    for path in (artifacts.input_path, artifacts.loop_run_path):
        try:
            _stable_regular_file_exists(root, path)
        except ValueError as exc:
            return f"previous design check artifacts are unavailable: {exc}"
    return ""


def _prepare_design_check(
    options: DesignContractCheckOptions,
) -> (
    tuple[
        Path,
        Path,
        str,
        DesignContractArtifacts,
        list[DesignContractArtifactRef],
    ]
    | DesignContractCommandResult
):
    root = options.root.resolve()
    work_item_dir, work_item_blocker = resolve_work_item_dir(root, options.work_item)
    if not options.dry_run and not options.loop_id.strip() and not work_item_blocker:
        closed_current = _closed_current_recheck_result(root, work_item_dir)
        if closed_current is not None:
            return closed_current
    try:
        loop_id = resolve_loop_id(options.loop_id)
    except ValueError as exc:
        return _blocked_result(f"Invalid design-contract loop id: {exc}")
    artifacts = design_contract_artifacts(root, loop_id)
    planned_refs = artifacts.refs(root)
    closed_result = _closed_recheck_result(root, artifacts)
    if closed_result is not None:
        return closed_result
    if work_item_blocker:
        return _blocked_result(work_item_blocker, artifacts=planned_refs)
    return root, work_item_dir, loop_id, artifacts, planned_refs


def _build_checked_contract_input(
    root: Path,
    loop_id: str,
    work_item_dir: Path,
    requirement_loop_id: str,
    planned_refs: list[DesignContractArtifactRef],
    **verification_options,
) -> DesignContractInput | DesignContractCommandResult:
    try:
        return build_contract_input(
            root=root,
            loop_id=loop_id,
            work_item_dir=work_item_dir,
            requirement_loop_id=requirement_loop_id,
            **verification_options,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _blocked_result(
            f"Formal design documents are unavailable or unsafe: {exc}",
            loop_id=loop_id,
            artifacts=planned_refs,
        )


def close_design_contract_loop(
    options: DesignContractCloseOptions,
    *,
    review_input_validator: ReviewInputValidator | None = None,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> DesignContractCommandResult:
    root = options.root.resolve()
    _, loop_id, blocker = _resolve_design_contract_loop_run_identity(
        root, options.loop_id
    )
    if blocker:
        return _close_design_contract_loop_locked(
            options,
            review_input_validator=review_input_validator,
            reviewed_artifacts=reviewed_artifacts,
        )
    with ExitStack() as locks:
        try:
            locks.enter_context(_stage_write_guard(root, 'design-contract', loop_id))
        except _ImplementationWriteLockError as exc:
            return _blocked_result(str(exc), loop_id=loop_id)
        return _close_design_contract_loop_locked(
            replace(options, loop_id=loop_id),
            review_input_validator=review_input_validator,
            reviewed_artifacts=reviewed_artifacts,
        )


def _close_design_contract_loop_locked(
    options: DesignContractCloseOptions,
    *,
    review_input_validator: ReviewInputValidator | None = None,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> DesignContractCommandResult:
    """Close the current design-contract loop after explicit confirmation."""

    root = options.root.resolve()
    loop_run_path, expected_loop_id, pointer_blocker = (
        _resolve_design_contract_loop_run_identity(
            root,
            options.loop_id,
        )
    )
    if pointer_blocker:
        return _blocked_result(pointer_blocker)
    if _design_publication_pending(design_contract_artifacts(root, expected_loop_id)):
        return _pending_publication_result(expected_loop_id, root=root)
    if not options.yes:
        return _blocked_result(
            "Pass --yes after confirming the design contract report.",
            result="Design-contract close requires explicit confirmation.",
            next_action="Repeat the same guarded design-contract close command with --yes.",
        )
    context = _load_design_close_context(
        root,
        loop_run_path,
        expected_loop_id,
        reviewed_artifacts=reviewed_artifacts,
    )
    if isinstance(context, DesignContractCommandResult):
        return context
    loop_run, report, verified_input, artifacts = context
    from ai_sdlc.core.loop_stage_input import validate_stage_close_review

    try:
        validate_stage_close_review(
            root, loop_run, options.expected_review_digest, review_input_validator
        )
    except ValueError as exc:
        return _blocked_result(str(exc), loop_id=loop_run.loop_id)
    return _close_verified_design_context(
        root,
        options,
        loop_run,
        report,
        verified_input,
        artifacts,
        review_input_validator=review_input_validator,
        reviewed_artifacts=reviewed_artifacts,
    )


def _load_design_close_context(
    root: Path,
    loop_run_path: Path,
    expected_loop_id: str,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> (
    tuple[
        LoopRun,
        DesignContractReport,
        DesignContractInput,
        DesignContractArtifacts,
    ]
    | DesignContractCommandResult
):
    try:
        loop_run = (
            read_loop_run(loop_run_path, root=root)
            if reviewed_artifacts is None
            else LoopRun.model_validate_json(
                _reviewed_design_bytes(root, loop_run_path, reviewed_artifacts)
            )
        )
    except ValueError as exc:
        return _blocked_result(
            str(exc),
            result="Design-contract loop artifact is malformed.",
        )
    identity_issue = _design_contract_loop_identity_issue(
        root,
        loop_run_path,
        expected_loop_id,
        loop_run,
    )
    if identity_issue:
        return _blocked_result(
            identity_issue,
            loop_id=expected_loop_id,
            result="Design-contract loop artifact is malformed.",
        )
    artifacts = design_contract_artifacts(root, expected_loop_id)
    try:
        report = (
            read_report(artifacts.report_json_path)
            if reviewed_artifacts is None
            else DesignContractReport.model_validate_json(
                _reviewed_design_bytes(
                    root,
                    artifacts.report_json_path,
                    reviewed_artifacts,
                )
            )
        )
    except ValueError as exc:
        return _blocked_result(
            str(exc),
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root),
        )
    verified_input = _verified_design_close_input(
        root,
        loop_run,
        report,
        artifacts,
        reviewed_artifacts=reviewed_artifacts,
    )
    if isinstance(verified_input, DesignContractCommandResult):
        return verified_input
    return loop_run, report, verified_input, artifacts


def _close_verified_design_context(
    root: Path,
    options: DesignContractCloseOptions,
    loop_run: LoopRun,
    report: DesignContractReport,
    verified_input: DesignContractInput,
    artifacts: DesignContractArtifacts,
    *,
    review_input_validator: ReviewInputValidator | None,
    reviewed_artifacts: Mapping[str, bytes] | None,
) -> DesignContractCommandResult:
    existing = _existing_design_close_result(
        root,
        loop_run,
        report,
        verified_input,
        artifacts,
        options=options,
        review_input_validator=review_input_validator,
        reviewed_artifacts=reviewed_artifacts,
    )
    if existing is not None:
        return existing
    if report.blocker_count or loop_run.status != LoopStatus.NEEDS_REVIEW:
        return _result_from_report(
            report,
            artifacts=artifacts.refs(root),
            result="Design contract cannot close while blockers remain.",
        )
    return _finish_verified_design_close(
        root,
        options,
        loop_run,
        report,
        verified_input,
        artifacts,
        review_input_validator=review_input_validator,
        reviewed_artifacts=reviewed_artifacts,
    )


def _finish_verified_design_close(
    root: Path,
    options: DesignContractCloseOptions,
    loop_run: LoopRun,
    report: DesignContractReport,
    verified_input: DesignContractInput,
    artifacts: DesignContractArtifacts,
    *,
    review_input_validator: ReviewInputValidator | None,
    reviewed_artifacts: Mapping[str, bytes] | None,
) -> DesignContractCommandResult:
    refreshed = _refresh_report_before_close(
        root,
        loop_run,
        artifacts,
        verified_input,
        persist=False,
        document_snapshot=reviewed_artifacts,
    )
    if isinstance(refreshed, DesignContractCommandResult):
        return refreshed
    report, loop_run = refreshed
    if report.blocker_count or loop_run.status != LoopStatus.NEEDS_REVIEW:
        _write_check_artifacts(
            root,
            verified_input,
            report,
            loop_run,
            artifacts,
        )
        return _result_from_report(
            report,
            artifacts=artifacts.refs(root),
            result="Design contract cannot close while blockers remain.",
        )
    revalidate_review_input_at_transition(
        root,
        loop_type="design-contract",
        loop_id=loop_run.loop_id,
        expected_digest=options.expected_review_digest,
        validator=review_input_validator,
    )
    return _write_close(
        root,
        loop_run,
        report,
        artifacts,
        options.closed_by,
    )


def _existing_design_close_result(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    verified_input: DesignContractInput,
    artifacts: DesignContractArtifacts,
    *,
    options: DesignContractCloseOptions,
    review_input_validator: ReviewInputValidator | None,
    reviewed_artifacts: Mapping[str, bytes] | None,
) -> DesignContractCommandResult | None:
    try:
        close_exists = _trusted_close_artifact_exists(root, artifacts)
    except ValueError as exc:
        return _blocked_result(
            str(exc),
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    if loop_run.status == LoopStatus.CLOSED and close_exists:
        return _already_closed_design_result(
            root, loop_run, report, verified_input, artifacts
        )
    if loop_run.status == LoopStatus.NEEDS_REVIEW and close_exists:
        return _recover_partially_written_design_close(
            root,
            loop_run,
            report,
            verified_input,
            artifacts,
            options=options,
            review_input_validator=review_input_validator,
            reviewed_artifacts=reviewed_artifacts,
        )
    if loop_run.status == LoopStatus.CLOSED or close_exists:
        return _blocked_result(
            "Existing closed design-contract artifact is unavailable.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    return None


def _recover_partially_written_design_close(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    verified_input: DesignContractInput,
    artifacts: DesignContractArtifacts,
    *,
    options: DesignContractCloseOptions,
    review_input_validator: ReviewInputValidator | None,
    reviewed_artifacts: Mapping[str, bytes] | None,
) -> DesignContractCommandResult:
    refreshed = _refresh_report_before_close(
        root,
        loop_run,
        artifacts,
        verified_input,
        persist=False,
        document_snapshot=reviewed_artifacts,
    )
    if isinstance(refreshed, DesignContractCommandResult):
        return refreshed
    report, loop_run = refreshed
    if report.blocker_count or loop_run.status != LoopStatus.NEEDS_REVIEW:
        artifacts.close_path.unlink(missing_ok=True)
        _write_check_artifacts(
            root,
            verified_input,
            report,
            loop_run,
            artifacts,
        )
        return _result_from_report(
            report,
            artifacts=artifacts.refs(root),
            result="Design contract cannot recover close while blockers remain.",
        )
    revalidate_review_input_at_transition(
        root,
        loop_type="design-contract",
        loop_id=loop_run.loop_id,
        expected_digest=options.expected_review_digest,
        validator=review_input_validator,
    )
    try:
        payload = LoopArtifactStore(root).read_json_artifact(artifacts.close_path)
        close = DesignContractClose.model_validate(payload)
    except (OSError, ValueError, ValidationError) as exc:
        return _blocked_result(
            f"Existing design-contract close artifact is malformed: {exc}",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    expected_report = repo_relative_path(root, artifacts.report_json_path)
    if close.loop_id != loop_run.loop_id or close.report_path != expected_report:
        return _blocked_result(
            "Existing design-contract close artifact does not match the current loop.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    successor = _closed_design_loop_run(
        root,
        loop_run,
        report,
        artifacts,
        transition_at=close.closed_at,
    )
    LoopArtifactStore(root).write_json_artifact(artifacts.loop_run_path, successor)
    return _result_from_report(
        report,
        artifacts=artifacts.refs(root, include_close=True),
        result="Design contract closed.",
        closed=True,
        loop_status=LoopStatus.CLOSED,
        next_action=successor.next_action,
    )


def _already_closed_design_result(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    verified_input: DesignContractInput,
    artifacts: DesignContractArtifacts,
) -> DesignContractCommandResult:
    try:
        _verify_design_document_snapshot(root, verified_input)
    except (OSError, UnicodeError, ValueError) as exc:
        return _blocked_result(
            f"Closed design document snapshot changed: {exc}",
            loop_id=loop_run.loop_id,
            next_action="Rerun design-contract check with a new loop id.",
            artifacts=artifacts.refs(root, include_close=True),
        )
    return _result_from_report(
        report,
        artifacts=artifacts.refs(root, include_close=True),
        result="Design contract is already closed.",
        closed=True,
        loop_status=LoopStatus.CLOSED,
        next_action=loop_run.next_action
        or _implementation_next_action(report.work_item_id),
    )


def _write_check_artifacts(
    root: Path,
    contract_input: DesignContractInput,
    report: DesignContractReport,
    loop_run: LoopRun,
    artifacts: DesignContractArtifacts,
) -> None:
    store = LoopArtifactStore(root)
    store.create_loop_run_dir(
        contract_input.loop_id,
        loop_type=LoopType.DESIGN_CONTRACT.value,
    )
    payloads = (
        contract_input,
        DesignContractCoverageMatrix(
            loop_id=contract_input.loop_id,
            work_item_id=contract_input.work_item_id,
            items=report.coverage_items,
        ),
        report,
        render_report_markdown(report),
        loop_run,
        DesignContractCurrentPointer(
            loop_id=contract_input.loop_id,
            loop_run_path=repo_relative_path(root, artifacts.loop_run_path),
        ),
    )
    paths = _design_publication_paths(artifacts)
    entries = {}
    for (name, path), payload in zip(paths.items(), payloads, strict=True):
        if isinstance(payload, str):
            text = payload if payload.endswith("\n") else payload + "\n"
        else:
            text = (
                json.dumps(
                    payload.model_dump(mode="json"), ensure_ascii=False, indent=2
                )
                + "\n"
            )
        entries[name] = {
            "old": _publication_bytes_record(
                _read_optional_publication_file(root, path)
            ),
            "new": _publication_bytes_record(text.encode("utf-8")),
        }
    pending = artifacts.loop_dir / DESIGN_CHECK_PENDING
    if _design_publication_pending(artifacts):
        raise ValueError("design-contract-publication-already-pending")
    journal = {
        "schema_version": 1,
        "artifact_kind": "design-check-publication",
        "loop_id": contract_input.loop_id,
        "transaction_id": uuid4().hex,
        "entries": entries,
    }
    raw = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode()
    states = _validate_design_publication(raw, artifacts)
    _require_publication_state(root, paths, states, side="old")
    # 先持久化六件的精确旧/新原件；目标写入也不允许降级成直接覆盖。
    store.write_bytes_artifact(pending, raw, immutable=True)
    for name, path in paths.items():
        if _read_optional_publication_file(root, path) != states[name]["old"]:
            raise _DesignPublicationDriftError("design-contract-publication-target-drift")
        store.write_bytes_artifact(path, states[name]["new"])
    _finish_design_publication(root, artifacts, raw, states, side="new")


def _design_publication_paths(artifacts: DesignContractArtifacts) -> dict[str, Path]:
    # run 为提交点，跨 Loop 的当前指针最后发布；不读取日志内的任意目标路径。
    return {
        path.name: path
        for path in (
            artifacts.input_path,
            artifacts.coverage_matrix_path,
            artifacts.report_json_path,
            artifacts.report_md_path,
            artifacts.loop_run_path,
            artifacts.pointer_path,
        )
    }


def _design_publication_pending(artifacts: DesignContractArtifacts) -> bool:
    pending = artifacts.loop_dir / DESIGN_CHECK_PENDING
    return pending.exists() or pending.is_symlink()


def _pending_publication_result(
    loop_id: str,
    detail: str = "",
    *,
    root: Path,
    fallback_input: DesignContractInput | None = None,
) -> DesignContractCommandResult:
    artifacts = design_contract_artifacts(root, loop_id)
    contract_input = fallback_input
    try:
        artifacts = design_contract_artifacts(root, loop_id)
        states = _validate_design_publication(
            read_stable_bytes(root, artifacts.loop_dir / DESIGN_CHECK_PENDING),
            artifacts,
        )
        contract_input = DesignContractInput.model_validate_json(
            states[artifacts.input_path.name]["new"]
        )
    except (ValueError, OSError):
        pass
    command = f"ai-sdlc loop design-contract check --loop-id {loop_id}"
    if contract_input is not None:
        command += " --wi " + json.dumps(
            contract_input.work_item_path, ensure_ascii=False
        )
        command += " --decision-mode " + contract_input.decision_mode
        if contract_input.decision_capability:
            command += " --decision-capability " + contract_input.decision_capability
        if contract_input.requirement_loop_id:
            command += " --requirement-loop-id " + contract_input.requirement_loop_id
        if contract_input.verification_contract_ref:
            command += " --verification-contract " + json.dumps(
                contract_input.verification_contract_ref
            )
    return _blocked_result(
        (
            "design-contract-publication-pending"
            if _design_publication_pending(artifacts)
            else "design-contract-publication-write-failed"
        )
        + (": " + detail if detail else ""),
        loop_id=loop_id,
        next_action=f"Run {command} to recover this publication.",
    )


def _read_optional_publication_file(root: Path, path: Path) -> bytes | None:
    return (
        read_stable_bytes(root, path)
        if _stable_regular_file_exists(root, path)
        else None
    )


def _publication_bytes_record(content: bytes | None) -> dict[str, str] | None:
    if content is None:
        return None
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "base64": base64.b64encode(content).decode("ascii"),
    }


def _validate_design_publication(
    raw: bytes, artifacts: DesignContractArtifacts
) -> dict[str, dict[str, bytes | None]]:
    journal = json.loads(raw)
    paths = _design_publication_paths(artifacts)
    if (
        not isinstance(journal, dict)
        or set(journal)
        != {"schema_version", "artifact_kind", "loop_id", "transaction_id", "entries"}
        or type(journal["schema_version"]) is not int
        or journal["schema_version"] != 1
        or journal["artifact_kind"] != "design-check-publication"
        or journal["loop_id"] != artifacts.loop_dir.name
        or not isinstance(journal["transaction_id"], str)
        or len(journal["transaction_id"]) != 32
        or any(c not in "0123456789abcdef" for c in journal["transaction_id"])
        or not isinstance(journal["entries"], dict)
        or set(journal["entries"]) != set(paths)
    ):
        raise ValueError("design-contract-publication-identity-mismatch")
    states = {}
    for name, entry in journal["entries"].items():
        if not isinstance(entry, dict) or set(entry) != {"old", "new"}:
            raise ValueError("design-contract-publication-invalid-entry")
        states[name] = {}
        for side in ("old", "new"):
            record = entry[side]
            if record is None and side == "old":
                states[name][side] = None
                continue
            if not isinstance(record, dict) or set(record) != {"sha256", "base64"}:
                raise ValueError("design-contract-publication-invalid-original")
            if not isinstance(record["base64"], str):
                raise ValueError("design-contract-publication-invalid-original")
            content = base64.b64decode(record["base64"], validate=True)
            if hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("design-contract-publication-original-digest-mismatch")
            states[name][side] = content
    for side in ("old", "new"):
        input_bytes = states[artifacts.input_path.name][side]
        run_bytes = states[artifacts.loop_run_path.name][side]
        contract = (
            DesignContractInput.model_validate_json(input_bytes)
            if input_bytes is not None
            else None
        )
        run = LoopRun.model_validate_json(run_bytes) if run_bytes is not None else None
        if side == "new" and (contract is None or run is None):
            raise ValueError("design-contract-publication-missing-new-identity")
        if contract is not None and contract.loop_id != journal["loop_id"]:
            raise ValueError("design-contract-publication-input-loop-mismatch")
        if run is not None and (
            contract is None
            or run.loop_type != LoopType.DESIGN_CONTRACT
            or run.loop_id != journal["loop_id"]
            or run.work_item_id != contract.work_item_id
            or run.input_digest != design_contract_input_digest(contract)
            or (run.decision_mode, run.decision_capability)
            != (contract.decision_mode, contract.decision_capability)
        ):
            raise ValueError("design-contract-publication-run-input-mismatch")
    pointer = DesignContractCurrentPointer.model_validate_json(
        states[artifacts.pointer_path.name]["new"]
    )
    if pointer.loop_id != journal["loop_id"] or pointer.loop_run_path != (
        f".ai-sdlc/loops/design-contract/{journal['loop_id']}/loop-run.json"
    ):
        raise ValueError("design-contract-publication-pointer-mismatch")
    return states


def _published_pointer_snapshot(root, artifacts, states) -> dict[Path, bytes | None]:
    pointer_bytes = _read_optional_publication_file(root, artifacts.pointer_path)
    if pointer_bytes in states[artifacts.pointer_path.name].values():
        return {}
    drift = "design-contract-publication-target-drift: " + artifacts.pointer_path.name
    try:
        pointer = DesignContractCurrentPointer.model_validate_json(pointer_bytes or b"")
        validate_explicit_loop_id(pointer.loop_id)
    except ValueError as exc:
        raise _DesignPublicationDriftError(drift) from exc
    if pointer.loop_id == artifacts.loop_dir.name or pointer.loop_run_path != (
        f".ai-sdlc/loops/design-contract/{pointer.loop_id}/loop-run.json"
    ):
        raise _DesignPublicationDriftError(drift)
    later = design_contract_artifacts(root, pointer.loop_id)
    if _design_publication_pending(later):
        raise _DesignPublicationDriftError(drift + ": later publication pending")
    snapshot: dict[Path, bytes | None] = {
        path: read_stable_bytes(root, path)
        for path in _design_publication_paths(later).values()
    }
    if snapshot[artifacts.pointer_path] != pointer_bytes:
        raise _DesignPublicationDriftError(drift)
    current_run = LoopRun.model_validate_json(snapshot[later.loop_run_path])
    # 发布身份保持不变；begin、正式评审及 Close 可以合法更新轮次与运行状态。
    identity = {
        "artifact_kind", "loop_id", "loop_type", "work_item_id", "input_digest",
        "created_at", "base_ref", "head_ref", "base_commit", "head_commit",
        "decision_mode", "decision_capability",
    }
    for archive in sorted((later.loop_dir / "design-check-publications").glob("*.json")):
        raw = read_stable_bytes(root, archive)
        if archive.name != hashlib.sha256(raw).hexdigest() + ".json":
            raise _DesignPublicationDriftError(drift + ": archive digest mismatch")
        published = _validate_design_publication(raw, later)
        if any(
            snapshot[path] != published[path.name]["new"]
            for path in _design_publication_paths(later).values()
            if path != later.loop_run_path
        ):
            continue
        published_run = LoopRun.model_validate_json(published[later.loop_run_path.name]["new"])
        if current_run.model_dump(include=identity) != published_run.model_dump(include=identity):
            raise _DesignPublicationDriftError(drift + ": published run identity mismatch")
        snapshot[archive] = raw
        snapshot[later.loop_dir / DESIGN_CHECK_PENDING] = None
        return snapshot
    raise _DesignPublicationDriftError(drift + ": published originals missing")


def _require_publication_state(
    root, paths, states, *, side: str | None = None, preserved=None
) -> None:
    # 先核完整集合，再动任何一件，尤其不能覆盖其他 Loop 后来发布的当前指针。
    for path, original in (preserved or {}).items():
        if _read_optional_publication_file(root, path) != original:
            raise _DesignPublicationDriftError(
                "design-contract-publication-preserved-pointer-drift"
            )
    for name, path in paths.items():
        current = _read_optional_publication_file(root, path)
        allowed = (
            (preserved[path],)
            if preserved and path in preserved
            else (states[name][side],)
            if side
            else (states[name]["old"], states[name]["new"])
        )
        if current not in allowed:
            raise _DesignPublicationDriftError(
                "design-contract-publication-target-drift: " + name
            )


def _finish_design_publication(
    root, artifacts, raw, states, *, side: str, preserved=None
) -> None:
    paths = _design_publication_paths(artifacts)
    _require_publication_state(root, paths, states, side=side, preserved=preserved)
    pending = artifacts.loop_dir / DESIGN_CHECK_PENDING
    if read_stable_bytes(root, pending) != raw:
        raise _DesignPublicationDriftError("design-contract-publication-journal-drift")
    archive = (
        artifacts.loop_dir
        / "design-check-publications"
        / (hashlib.sha256(raw).hexdigest() + ".json")
    )
    LoopArtifactStore(root).write_bytes_artifact(archive, raw, immutable=True)
    _require_publication_state(root, paths, states, side=side, preserved=preserved)
    if (
        read_stable_bytes(root, pending) != raw
        or read_stable_bytes(root, archive) != raw
    ):
        raise _DesignPublicationDriftError("design-contract-publication-journal-drift")
    pending.unlink()


def _recover_design_publication(root: Path, artifacts: DesignContractArtifacts) -> None:
    raw = read_stable_bytes(root, artifacts.loop_dir / DESIGN_CHECK_PENDING)
    states = _validate_design_publication(raw, artifacts)
    paths = _design_publication_paths(artifacts)
    preserved = _published_pointer_snapshot(root, artifacts, states)
    _require_publication_state(root, paths, states, preserved=preserved)
    current_run = _read_optional_publication_file(root, artifacts.loop_run_path)
    run_states = states[artifacts.loop_run_path.name]
    # 新旧 run 同字节时无法判断提交位置，固定恢复旧件后重新分析，不猜提交成功。
    side = (
        "new"
        if current_run == run_states["new"] and current_run != run_states["old"]
        else "old"
    )
    for name, path in paths.items():
        if preserved:
            _require_publication_state(root, paths, states, preserved=preserved)
        # 恢复不重选当前 Loop；公开 check 随后的新事务才可以正常发布本 Loop。
        if path in preserved:
            continue
        current = _read_optional_publication_file(root, path)
        if current not in (states[name]["old"], states[name]["new"]):
            raise _DesignPublicationDriftError(
                "design-contract-publication-target-drift: " + name
            )
        target = states[name][side]
        if current == target:
            continue
        if target is None:
            path.unlink()
        else:
            LoopArtifactStore(root).write_bytes_artifact(path, target)
    _finish_design_publication(root, artifacts, raw, states, side=side, preserved=preserved)


def _refresh_report_before_close(
    root: Path,
    loop_run: LoopRun,
    artifacts: DesignContractArtifacts,
    contract_input: DesignContractInput,
    *,
    persist: bool = True,
    document_snapshot: Mapping[str, bytes] | None = None,
) -> tuple[DesignContractReport, LoopRun] | DesignContractCommandResult:
    resolved_requirement_loop_id, requirement_blocker, requirement_next_action = (
        _required_requirement_loop_id(root, contract_input.requirement_loop_id)
    )
    if requirement_blocker:
        return _blocked_result(
            requirement_blocker,
            loop_id=loop_run.loop_id,
            next_action=requirement_next_action,
            artifacts=artifacts.refs(root),
        )
    contract_input = contract_input.model_copy(
        update={"requirement_loop_id": resolved_requirement_loop_id}
    )
    requirement_blocker, requirement_next_action, prerequisite = _requirement_loop_gate(
        root,
        contract_input.requirement_loop_id,
        work_item_id=contract_input.work_item_id,
    )
    if requirement_blocker:
        return _blocked_result(
            requirement_blocker,
            loop_id=loop_run.loop_id,
            next_action=requirement_next_action,
            artifacts=artifacts.refs(root),
        )
    if (
        contract_input.authorized_scope_families
        != prerequisite["authorized_scope_families"]
    ):
        return _blocked_result(
            "Frozen requirement scope changed after design-contract check.",
            loop_id=loop_run.loop_id,
            next_action="Start and freeze a new requirement loop.",
            artifacts=artifacts.refs(root),
        )
    report = analyze_design_contract(
        root,
        contract_input,
        document_snapshot=document_snapshot,
    )
    report.next_action = _next_action_for_report(report)
    refreshed_loop_run = _build_loop_run(
        contract_input=contract_input,
        report=report,
        loop_status=report.status,
        artifacts=artifacts,
        root=root,
    )
    refreshed_loop_run = preserve_stage_run(loop_run, refreshed_loop_run)
    if persist:
        _write_check_artifacts(
            root,
            contract_input,
            report,
            refreshed_loop_run,
            artifacts,
        )
    return report, refreshed_loop_run


def _verified_design_close_input(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    artifacts: DesignContractArtifacts,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> DesignContractInput | DesignContractCommandResult:
    if (
        report.loop_id != loop_run.loop_id
        or report.work_item_id != loop_run.work_item_id
    ):
        return _blocked_result(
            "Design-contract report identity does not match the confirmed loop.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root),
        )
    try:
        if reviewed_artifacts is None:
            payload = LoopArtifactStore(root).read_json_artifact(artifacts.input_path)
            contract_input = DesignContractInput.model_validate(payload)
        else:
            contract_input = DesignContractInput.model_validate_json(
                _reviewed_design_bytes(
                    root,
                    artifacts.input_path,
                    reviewed_artifacts,
                )
            )
    except (OSError, ValueError, ValidationError) as exc:
        return _blocked_result(
            f"Design-contract input artifact is malformed: {exc}",
            result="Design-contract close requires a readable current input artifact.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root),
        )
    if (
        contract_input.loop_id != loop_run.loop_id
        or contract_input.work_item_id != loop_run.work_item_id
    ):
        return _blocked_result(
            "Design-contract input identity does not match the confirmed loop.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root),
        )
    if design_contract_input_digest(contract_input) != loop_run.input_digest:
        return _blocked_result(
            "Design-contract input changed after check.",
            loop_id=loop_run.loop_id,
            next_action="Rerun design-contract check with a new loop id.",
            artifacts=artifacts.refs(root),
        )
    try:
        read_verification_contract(
            root, contract_input, captured_artifacts=reviewed_artifacts
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _blocked_result(
            str(exc), loop_id=loop_run.loop_id, artifacts=artifacts.refs(root)
        )
    return contract_input


def _write_close(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    artifacts: DesignContractArtifacts,
    closed_by: str,
) -> DesignContractCommandResult:
    _, close, successor = _design_close_write_payloads(
        root,
        loop_run,
        report,
        artifacts,
        closed_by,
    )
    store = LoopArtifactStore(root)
    store.write_json_artifact(artifacts.close_path, close)
    store.write_json_artifact(artifacts.loop_run_path, successor)
    return _result_from_report(
        report,
        artifacts=artifacts.refs(root, include_close=True),
        result="Design contract closed.",
        closed=True,
        loop_status=LoopStatus.CLOSED,
        next_action=successor.next_action,
    )


def _design_close_write_payloads(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    artifacts: DesignContractArtifacts,
    closed_by: str,
) -> tuple[str, DesignContractClose, LoopRun]:
    transition_at = utc_now_iso()
    normalized_closed_by = closed_by.strip() or "local-user"
    close = DesignContractClose(
        loop_id=loop_run.loop_id,
        closed_by=normalized_closed_by,
        created_at=transition_at,
        closed_at=transition_at,
        report_path=repo_relative_path(root, artifacts.report_json_path),
    )
    successor = _closed_design_loop_run(
        root,
        loop_run,
        report,
        artifacts,
        transition_at=transition_at,
    )
    return normalized_closed_by, close, successor


def _closed_design_loop_run(
    root: Path,
    loop_run: LoopRun,
    report: DesignContractReport,
    artifacts: DesignContractArtifacts,
    *,
    transition_at: str,
) -> LoopRun:
    successor = loop_run.model_copy(deep=True)
    successor.status = LoopStatus.CLOSED
    successor.updated_at = transition_at
    successor.next_action = _implementation_next_action(report.work_item_id)
    successor.current_round = 1
    if successor.rounds:
        current = successor.rounds[0]
        current.status = LoopStatus.CLOSED
        current.output_artifacts = append_unique(
            current.output_artifacts,
            repo_relative_path(root, artifacts.close_path),
        )
        current.next_action = successor.next_action
    return successor


def _closed_recheck_result(
    root: Path,
    artifacts: DesignContractArtifacts,
) -> DesignContractCommandResult | None:
    # 外层只解析身份，恢复要等同阶段锁；不能先消费旧 Close 快捷结果。
    if _design_publication_pending(artifacts):
        return None
    try:
        close_exists = _trusted_close_artifact_exists(root, artifacts)
    except ValueError as exc:
        return _blocked_result(
            str(exc),
            artifacts=artifacts.refs(root, include_close=True),
        )
    if not close_exists:
        try:
            if not _stable_regular_file_exists(root, artifacts.loop_run_path):
                return None
            loop_run = read_loop_run(artifacts.loop_run_path, root=root)
        except ValueError:
            return None
        if loop_run.status != LoopStatus.CLOSED:
            return None
        return _blocked_result(
            "Existing closed design-contract artifact is unavailable.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    try:
        loop_run = read_loop_run(artifacts.loop_run_path, root=root)
    except ValueError as exc:
        return _blocked_result(
            f"Existing closed design-contract loop is malformed: {exc}",
            artifacts=artifacts.refs(root, include_close=True),
        )
    next_action = _implementation_next_action(loop_run.work_item_id)
    return _blocked_result(
        "Design-contract loop is already closed; start implementation instead of rechecking it.",
        result="Design-contract loop is already closed.",
        loop_id=loop_run.loop_id,
        next_action=next_action,
        artifacts=artifacts.refs(root, include_close=True),
    )


def _closed_current_recheck_result(
    root: Path,
    work_item_dir: Path,
) -> DesignContractCommandResult | None:
    loop_run_path, expected_loop_id, pointer_blocker = (
        _resolve_design_contract_loop_run_identity(root, "")
    )
    if pointer_blocker:
        if pointer_blocker == "No current design-contract loop exists.":
            return None
        return _blocked_result(pointer_blocker)
    if _design_publication_pending(design_contract_artifacts(root, expected_loop_id)):
        return _pending_publication_result(expected_loop_id, root=root)
    try:
        loop_run = read_loop_run(loop_run_path, root=root)
    except ValueError:
        return None
    if _design_contract_loop_identity_issue(
        root,
        loop_run_path,
        expected_loop_id,
        loop_run,
    ):
        return None
    if (
        loop_run.status != LoopStatus.CLOSED
        or loop_run.work_item_id != work_item_dir.name
    ):
        return None
    artifacts = design_contract_artifacts(root, expected_loop_id)
    return _current_closed_design_result(root, loop_run, artifacts)


def _current_closed_design_result(
    root: Path,
    loop_run: LoopRun,
    artifacts: DesignContractArtifacts,
) -> DesignContractCommandResult:
    try:
        close_exists = _trusted_close_artifact_exists(root, artifacts)
    except ValueError as exc:
        return _blocked_result(
            str(exc),
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    if not close_exists:
        return _blocked_result(
            "Existing closed design-contract artifact is unavailable.",
            loop_id=loop_run.loop_id,
            artifacts=artifacts.refs(root, include_close=True),
        )
    try:
        report = read_report(artifacts.report_json_path)
    except ValueError as exc:
        return _blocked_result(
            f"Existing closed design-contract report is malformed: {exc}",
            artifacts=artifacts.refs(root, include_close=True),
        )
    next_action = loop_run.next_action or _implementation_next_action(
        report.work_item_id
    )
    return _result_from_report(
        report,
        artifacts=artifacts.refs(root, include_close=True),
        result="Design contract is already closed.",
        closed=True,
        loop_status=LoopStatus.CLOSED,
        next_action=next_action,
    )


def _trusted_close_artifact_exists(
    root: Path,
    artifacts: DesignContractArtifacts,
) -> bool:
    try:
        return _stable_regular_file_exists(root, artifacts.close_path)
    except ValueError as exc:
        raise ValueError(
            "Existing closed design-contract artifact is unavailable."
        ) from exc


def _requirement_loop_gate(
    root: Path,
    requirement_loop_id: str,
    *,
    work_item_id: str = "",
) -> tuple[str, str, dict[str, object]]:
    loop_id, blocker, next_action = _required_requirement_loop_id(
        root,
        requirement_loop_id,
    )
    if blocker:
        return blocker, next_action, {}
    try:
        safe_loop_id = _validate_requirement_loop_id(loop_id)
    except ValueError as exc:
        return (
            f"Invalid requirement loop id: {exc}",
            "Run ai-sdlc loop requirement status.",
            {},
        )
    artifacts = _requirement_artifacts(root, safe_loop_id)
    freeze, blocker, next_action = _load_requirement_freeze(
        root,
        safe_loop_id,
        artifacts,
    )
    if blocker:
        return blocker, next_action, {}
    assert freeze is not None
    intake, blocker, next_action = _load_requirement_intake(
        root,
        safe_loop_id,
        artifacts,
        freeze,
    )
    if blocker:
        return blocker, next_action, {}
    assert intake is not None
    blocker, next_action = _requirement_identity_issue(
        safe_loop_id,
        intake,
        work_item_id,
    )
    if blocker:
        return blocker, next_action, {}
    try:
        validate_frozen_requirement_repair(root, artifacts, intake, freeze)
    except (ValueError, OSError) as exc:
        return (
            f"Requirement repair-readiness dependency for {safe_loop_id} is invalid: {exc}",
            f"Run ai-sdlc loop review --type requirement --loop-id {safe_loop_id}.",
            {},
        )
    return (
        "",
        "",
        {
            "authorized_scope_families": list(intake.design_scope_families),
        },
    )


def _load_requirement_freeze(
    root: Path,
    loop_id: str,
    artifacts: _RequirementArtifacts,
) -> tuple[RequirementFreeze | None, str, str]:
    freeze_next_action = (
        f"Run ai-sdlc loop review --type requirement --loop-id {loop_id}."
    )
    try:
        loop_run = _read_requirement_loop_run(artifacts.loop_run_path)
    except ValueError as exc:
        return (
            None,
            f"Requirement loop {loop_id} must exist and be frozen before design-contract check: {exc}",
            "Run ai-sdlc loop requirement start.",
        )
    if loop_run.loop_id != loop_id:
        return (
            None,
            f"Requirement loop id mismatch: expected {loop_id}, found {loop_run.loop_id}.",
            "Run ai-sdlc loop requirement status.",
        )
    if loop_run.status != LoopStatus.CLOSED or not artifacts.freeze_path.is_file():
        return (
            None,
            f"Requirement loop {loop_id} must be frozen before design-contract check.",
            freeze_next_action,
        )
    try:
        freeze_payload = LoopArtifactStore(root).read_json_artifact(
            artifacts.freeze_path
        )
        freeze = RequirementFreeze.model_validate(freeze_payload)
    except (OSError, ValueError, ValidationError) as exc:
        return (
            None,
            f"Requirement freeze artifact for {loop_id} is malformed: {exc}",
            freeze_next_action,
        )
    blocker, next_action = _requirement_freeze_identity_issue(
        root,
        loop_id,
        artifacts,
        freeze,
    )
    if blocker:
        return None, blocker, next_action
    return freeze, "", ""


def _requirement_freeze_identity_issue(
    root: Path,
    loop_id: str,
    artifacts: _RequirementArtifacts,
    freeze: RequirementFreeze,
) -> tuple[str, str]:
    if freeze.loop_id != loop_id:
        return (
            f"Requirement freeze artifact id mismatch: expected {loop_id}, found {freeze.loop_id}.",
            f"Run ai-sdlc loop review --type requirement --loop-id {loop_id}.",
        )
    expected_intake_path = repo_relative_path(root, artifacts.intake_path)
    if freeze.intake_path != expected_intake_path:
        return (
            f"Requirement freeze artifact for {loop_id} references another intake.",
            "Start and freeze a new requirement loop.",
        )
    return "", ""


def _load_requirement_intake(
    root: Path,
    loop_id: str,
    artifacts: _RequirementArtifacts,
    freeze: RequirementFreeze,
) -> tuple[RequirementIntake | None, str, str]:
    try:
        intake_payload = LoopArtifactStore(root).read_json_artifact(
            artifacts.intake_path
        )
        intake = RequirementIntake.model_validate(intake_payload)
    except (OSError, ValueError, ValidationError) as exc:
        return (
            None,
            f"Requirement intake artifact for {loop_id} is malformed: {exc}",
            "Run ai-sdlc loop requirement status.",
        )
    if intake.loop_id != loop_id:
        return (
            None,
            f"Requirement intake artifact id mismatch: expected {loop_id}, found {intake.loop_id}.",
            "Start and freeze a new requirement loop.",
        )
    if freeze.intake_digest:
        if freeze.intake_digest != _requirement_intake_digest(intake):
            return (
                None,
                f"Requirement intake artifact for {loop_id} changed after freeze.",
                "Start and freeze a new requirement loop.",
            )
    else:
        return (
            None,
            f"Requirement freeze for {loop_id} does not bind its intake.",
            "Start and freeze a new requirement loop.",
        )
    return intake, "", ""


def _requirement_identity_issue(
    loop_id: str,
    intake: RequirementIntake,
    work_item_id: str,
) -> tuple[str, str]:
    work_item = work_item_id.strip()
    if work_item:
        intake_work_item = intake.work_item_id.strip()
        if intake_work_item and intake_work_item != work_item:
            return (
                (
                    f"Requirement loop {loop_id} belongs to work item "
                    f"{intake_work_item}, but design-contract work item is {work_item}."
                ),
                (
                    "Run ai-sdlc loop requirement start "
                    f'--work-item-id {work_item} --acceptance "<验收标准>".'
                ),
            )
    return "", ""


def _required_requirement_loop_id(
    root: Path,
    requirement_loop_id: str,
) -> tuple[str, str, str]:
    loop_id = requirement_loop_id.strip()
    if loop_id:
        return loop_id, "", ""
    loop_run_path, expected_loop_id, pointer_blocker = (
        _resolve_requirement_loop_run_path(root, "")
    )
    if pointer_blocker:
        return (
            "",
            (
                "A frozen current requirement loop is required before "
                f"design-contract check: {pointer_blocker}"
            ),
            "Run ai-sdlc loop requirement start.",
        )
    try:
        loop_run = _read_requirement_loop_run(loop_run_path)
    except ValueError as exc:
        return (
            "",
            (
                "Current requirement loop must exist and be frozen before "
                f"design-contract check: {exc}"
            ),
            "Run ai-sdlc loop requirement status.",
        )
    identity_issue = _requirement_loop_identity_issue(
        root,
        loop_run_path,
        expected_loop_id,
        loop_run,
    )
    if identity_issue:
        return (
            "",
            f"Current requirement loop identity is invalid: {identity_issue}",
            "Run ai-sdlc loop requirement status.",
        )
    return expected_loop_id, "", ""


def _build_loop_run(
    *,
    contract_input: DesignContractInput,
    report: DesignContractReport,
    loop_status: LoopStatus,
    artifacts: DesignContractArtifacts,
    root: Path,
) -> LoopRun:
    output_artifacts = [
        repo_relative_path(root, artifacts.input_path),
        repo_relative_path(root, artifacts.coverage_matrix_path),
        repo_relative_path(root, artifacts.report_json_path),
        repo_relative_path(root, artifacts.report_md_path),
    ]
    return LoopRun(
        loop_id=contract_input.loop_id,
        loop_type=LoopType.DESIGN_CONTRACT,
        decision_mode=contract_input.decision_mode,
        decision_capability=contract_input.decision_capability,
        status=loop_status,
        work_item_id=contract_input.work_item_id,
        input_digest=design_contract_input_digest(contract_input),
        current_round=1,
        rounds=[
            LoopRound(
                round_number=1,
                input_artifacts=[
                    contract_input.spec_path,
                    contract_input.plan_path,
                    contract_input.tasks_path,
                ],
                output_artifacts=output_artifacts,
                command=["ai-sdlc", "loop", "design-contract", "check"],
                status=loop_status,
                result=report.status,
                next_action=report.next_action,
            )
        ],
        next_action=report.next_action,
    )


def _result_from_report(
    report: DesignContractReport,
    *,
    artifacts: list[DesignContractArtifactRef],
    result: str,
    closed: bool = False,
    loop_status: LoopStatus | str = "",
    next_action: str = "",
) -> DesignContractCommandResult:
    resolved_next_action = next_action or report.next_action
    resolved_loop_status = loop_status or report.status
    return DesignContractCommandResult(
        status=(
            DesignContractCommandStatus.READY
            if not report.blocker_count
            else DesignContractCommandStatus.NEEDS_FIX
        ),
        result=result,
        loop_id=report.loop_id,
        loop_status=resolved_loop_status,
        work_item_id=report.work_item_id,
        work_item_path=report.work_item_path,
        blocker_count=report.blocker_count,
        warning_count=report.warning_count,
        coverage_count=report.coverage_count,
        closed=closed,
        next_action=resolved_next_action,
        next_guidance=_next_guidance_for_result(
            report,
            next_action=resolved_next_action,
            closed=closed,
            artifacts=artifacts,
        ),
        artifacts=artifacts,
        design_contract=_command_summary_for_report(
            report,
            artifacts=artifacts,
            status=resolved_loop_status,
            closed=closed,
        ),
    )


def _blocked_result(
    blocker: str,
    *,
    result: str = "Design-contract loop is blocked.",
    loop_id: str = "",
    next_action: str = "Run ai-sdlc loop design-contract check --wi specs/<work-item>.",
    artifacts: list[DesignContractArtifactRef] | None = None,
) -> DesignContractCommandResult:
    return DesignContractCommandResult(
        status=DesignContractCommandStatus.BLOCKED,
        result=result,
        loop_id=loop_id,
        loop_status=LoopStatus.BLOCKED,
        blocker=blocker,
        next_action=next_action,
        next_guidance=DesignContractNextGuidance(
            command="",
            reason=blocker,
            requires_model=False,
            writes_artifacts=False,
            writes_code=False,
            safety="blocked",
        ),
        artifacts=artifacts or [],
    )


def _command_summary(
    contract_input: DesignContractInput,
    *,
    status: LoopStatus | str,
    artifacts: list[DesignContractArtifactRef],
) -> DesignContractCommandSummary:
    return DesignContractCommandSummary(
        status=_status_value(status),
        work_item_id=contract_input.work_item_id,
        work_item_path=contract_input.work_item_path,
        coverage_matrix_path=_artifact_path(artifacts, "coverage-matrix"),
        report_path=_artifact_path(artifacts, "design-contract-report-json"),
    )


def _command_summary_for_report(
    report: DesignContractReport,
    *,
    artifacts: list[DesignContractArtifactRef],
    status: LoopStatus | str,
    closed: bool,
) -> DesignContractCommandSummary:
    return DesignContractCommandSummary(
        status=_status_value(status),
        work_item_id=report.work_item_id,
        work_item_path=report.work_item_path,
        blocker_count=report.blocker_count,
        warning_count=report.warning_count,
        coverage_count=report.coverage_count,
        coverage_matrix_path=_artifact_path(artifacts, "coverage-matrix"),
        report_path=_artifact_path(artifacts, "design-contract-report-json"),
        closed=closed,
    )


def _artifact_path(
    artifacts: list[DesignContractArtifactRef],
    kind: str,
) -> str:
    return next((artifact.path for artifact in artifacts if artifact.kind == kind), "")


def _status_value(status: LoopStatus | str) -> str:
    return status.value if isinstance(status, LoopStatus) else str(status)


def _next_action_for_report(report: DesignContractReport) -> str:
    if report.blocker_count:
        return (
            "Fix design-contract blockers, then run "
            f"ai-sdlc loop design-contract check --wi {report.work_item_path}."
        )
    return f"Run ai-sdlc loop review --type design-contract --loop-id {report.loop_id}."


def _next_guidance_for_result(
    report: DesignContractReport,
    *,
    next_action: str,
    closed: bool,
    artifacts: list[DesignContractArtifactRef],
) -> DesignContractNextGuidance:
    evidence = [artifact.path for artifact in artifacts if artifact.path]
    if closed:
        return DesignContractNextGuidance(
            command="",
            reason="The design contract is closed; the next loop type is implementation.",
            requires_model=False,
            writes_artifacts=False,
            writes_code=False,
            safety="no_action",
            evidence=evidence,
            alternatives=[next_action],
        )
    if report.blocker_count:
        return DesignContractNextGuidance(
            command=f"ai-sdlc loop design-contract check --wi {report.work_item_path}",
            reason="The design contract has blockers; fix the formal docs and rerun the deterministic check.",
            requires_model=False,
            writes_artifacts=True,
            writes_code=False,
            safety="writes_project_artifacts",
            evidence=evidence,
        )
    return DesignContractNextGuidance(
        command=(
            f"ai-sdlc loop review --type design-contract --loop-id {report.loop_id}"
        ),
        reason="The design contract is ready for bounded adversarial review.",
        requires_model=True,
        writes_artifacts=False,
        writes_code=False,
        safety="safe_read_only",
        evidence=evidence,
    )


def _implementation_next_action(work_item_id: str) -> str:
    return f"Start implementation loop for {work_item_id}."


def _reviewed_design_bytes(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes],
) -> bytes:
    key = repo_relative_path(root, path)
    try:
        return reviewed_artifacts[key]
    except KeyError as exc:
        raise ValueError(f"Reviewed design snapshot is missing {key}.") from exc


__all__ = [
    "CURRENT_DESIGN_CONTRACT_PATH",
    "ContractCoverageItem",
    "DesignContractCheckOptions",
    "DesignContractClose",
    "DesignContractCloseOptions",
    "DesignContractCommandResult",
    "DesignContractCommandStatus",
    "DesignContractInput",
    "DesignContractReport",
    "check_design_contract_loop",
    "close_design_contract_loop",
]

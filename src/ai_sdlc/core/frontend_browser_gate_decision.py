"""Minimal browser-evidence decision projection used by the Frontend Evidence Loop."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_sdlc.models.frontend_browser_gate import (
    BrowserProbeExecutionReceipt,
    BrowserQualityBundleMaterializationInput,
    BrowserQualityGateExecutionContext,
)

FRONTEND_GATE_EXECUTE_STATE_READY = "ready"
FRONTEND_GATE_EXECUTE_STATE_BLOCKED = "blocked"
FRONTEND_GATE_EXECUTE_STATE_RECHECK_REQUIRED = "recheck_required"
FRONTEND_GATE_EXECUTE_STATE_NEEDS_REMEDIATION = "needs_remediation"
FRONTEND_GATE_DECISION_REASON_ALL_CHECKS_PASSED = "all_checks_passed"
FRONTEND_GATE_DECISION_REASON_ADVISORY_ONLY = "advisory_only"
FRONTEND_GATE_DECISION_REASON_SCOPE_OR_LINKAGE_INVALID = "scope_or_linkage_invalid"
FRONTEND_GATE_DECISION_REASON_EVIDENCE_MISSING = "evidence_missing"
FRONTEND_GATE_DECISION_REASON_TRANSIENT_RUN_FAILURE = "transient_run_failure"
FRONTEND_GATE_DECISION_REASON_ACTUAL_QUALITY_BLOCKER = "actual_quality_blocker"
FRONTEND_GATE_DECISION_REASON_RESULT_INCONSISTENCY = "result_inconsistency"


def _unique_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return unique


@dataclass(frozen=True, slots=True)
class FrontendGateExecuteDecision:
    """Decision projection for browser evidence execution truth."""

    execute_gate_state: str
    decision_reason: str
    blockers: tuple[str, ...] = ()
    recheck_required: bool = False
    recheck_reason_codes: tuple[str, ...] = ()
    remediation_hints: tuple[str, ...] = ()
    remediation_reason_codes: tuple[str, ...] = ()
    source_linkage_refs: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(_unique_strings(self.blockers)))
        object.__setattr__(
            self,
            "recheck_reason_codes",
            tuple(_unique_strings(self.recheck_reason_codes)),
        )
        object.__setattr__(
            self,
            "remediation_hints",
            tuple(_unique_strings(self.remediation_hints)),
        )
        object.__setattr__(
            self,
            "remediation_reason_codes",
            tuple(_unique_strings(self.remediation_reason_codes)),
        )


def build_frontend_browser_gate_execute_decision(
    *,
    execution_context: BrowserQualityGateExecutionContext,
    bundle: BrowserQualityBundleMaterializationInput,
    artifact_path: str,
    probe_runtime_state: str = "",
    apply_artifact_path: str = "",
) -> FrontendGateExecuteDecision:
    """Project one browser gate bundle into Frontend Evidence Loop decision truth."""

    source_linkage_refs = {
        "frontend_execute_gate_source": "frontend_browser_gate_artifact",
        "frontend_browser_gate_artifact_path": artifact_path,
        "frontend_browser_gate_gate_run_id": bundle.gate_run_id,
        "frontend_browser_gate_apply_result_id": bundle.apply_result_id,
        "frontend_browser_gate_overall_status": bundle.overall_gate_status,
    }
    if probe_runtime_state:
        source_linkage_refs["frontend_browser_gate_probe_runtime_state"] = (
            probe_runtime_state
        )

    reason_codes = tuple(
        _unique_strings(
            [
                *bundle.blocking_reason_codes,
                *(
                    code
                    for receipt in bundle.check_receipts
                    for code in receipt.blocking_reason_codes
                ),
            ]
        )
    )
    remediation_hints = tuple(
        _unique_strings(
            [
                hint
                for receipt in bundle.check_receipts
                for hint in receipt.remediation_hints
            ]
        )
    )
    blockers = tuple(_browser_gate_blockers(bundle.check_receipts, remediation_hints))

    if _browser_gate_scope_or_linkage_invalid(execution_context, bundle):
        diagnostics = blockers or (
            "browser gate artifact scope or linkage is inconsistent with execution context",
        )
        diagnostic_codes = reason_codes or ("browser_gate_scope_linkage_invalid",)
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_BLOCKED,
            decision_reason=FRONTEND_GATE_DECISION_REASON_SCOPE_OR_LINKAGE_INVALID,
            blockers=diagnostics,
            remediation_hints=diagnostics,
            remediation_reason_codes=diagnostic_codes,
            source_linkage_refs=source_linkage_refs,
        )

    if not _browser_gate_receipts_match_required_probe_set(
        execution_context.required_probe_set,
        bundle.check_receipts,
    ):
        diagnostics = blockers or (
            "browser gate bundle is missing required probe receipts or contains unexpected checks",
        )
        diagnostic_codes = reason_codes or (
            "browser_gate_required_probe_receipts_incomplete",
        )
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_BLOCKED,
            decision_reason=FRONTEND_GATE_DECISION_REASON_RESULT_INCONSISTENCY,
            blockers=diagnostics,
            remediation_hints=diagnostics,
            remediation_reason_codes=diagnostic_codes,
            source_linkage_refs=source_linkage_refs,
        )

    if apply_artifact_path and bundle.source_artifact_ref != apply_artifact_path:
        diagnostics = blockers or (
            "browser gate bundle source artifact does not match the canonical apply artifact linkage",
        )
        diagnostic_codes = reason_codes or (
            "browser_gate_apply_artifact_linkage_invalid",
        )
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_BLOCKED,
            decision_reason=FRONTEND_GATE_DECISION_REASON_SCOPE_OR_LINKAGE_INVALID,
            blockers=diagnostics,
            remediation_hints=diagnostics,
            remediation_reason_codes=diagnostic_codes,
            source_linkage_refs=source_linkage_refs,
        )

    expected_status = _expected_browser_gate_status(bundle.check_receipts)
    if bundle.overall_gate_status != expected_status:
        diagnostics = blockers or (
            "browser gate bundle overall status does not match per-check receipts",
        )
        diagnostic_codes = reason_codes or ("browser_gate_result_inconsistency",)
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_BLOCKED,
            decision_reason=FRONTEND_GATE_DECISION_REASON_RESULT_INCONSISTENCY,
            blockers=diagnostics,
            remediation_hints=diagnostics,
            remediation_reason_codes=diagnostic_codes,
            source_linkage_refs=source_linkage_refs,
        )

    if bundle.overall_gate_status == "passed":
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_READY,
            decision_reason=FRONTEND_GATE_DECISION_REASON_ALL_CHECKS_PASSED,
            source_linkage_refs=source_linkage_refs,
        )

    if bundle.overall_gate_status == "passed_with_advisories":
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_READY,
            decision_reason=FRONTEND_GATE_DECISION_REASON_ADVISORY_ONLY,
            remediation_hints=remediation_hints,
            source_linkage_refs=source_linkage_refs,
        )

    if bundle.overall_gate_status == "incomplete":
        decision_reason = (
            FRONTEND_GATE_DECISION_REASON_TRANSIENT_RUN_FAILURE
            if any(
                receipt.classification_candidate == "transient_run_failure"
                for receipt in bundle.check_receipts
            )
            and not any(
                receipt.classification_candidate == "evidence_missing"
                for receipt in bundle.check_receipts
            )
            else FRONTEND_GATE_DECISION_REASON_EVIDENCE_MISSING
        )
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_RECHECK_REQUIRED,
            decision_reason=decision_reason,
            blockers=blockers,
            recheck_required=True,
            recheck_reason_codes=reason_codes,
            remediation_hints=remediation_hints,
            remediation_reason_codes=reason_codes,
            source_linkage_refs=source_linkage_refs,
        )

    if bundle.overall_gate_status == "blocked":
        return FrontendGateExecuteDecision(
            execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_NEEDS_REMEDIATION,
            decision_reason=FRONTEND_GATE_DECISION_REASON_ACTUAL_QUALITY_BLOCKER,
            blockers=blockers,
            remediation_hints=remediation_hints or blockers,
            remediation_reason_codes=reason_codes,
            source_linkage_refs=source_linkage_refs,
        )

    diagnostics = blockers or ("browser gate bundle overall status is unsupported",)
    diagnostic_codes = reason_codes or ("browser_gate_result_inconsistency",)
    return FrontendGateExecuteDecision(
        execute_gate_state=FRONTEND_GATE_EXECUTE_STATE_BLOCKED,
        decision_reason=FRONTEND_GATE_DECISION_REASON_RESULT_INCONSISTENCY,
        blockers=diagnostics,
        remediation_hints=diagnostics,
        remediation_reason_codes=diagnostic_codes,
        source_linkage_refs=source_linkage_refs,
    )


def _browser_gate_scope_or_linkage_invalid(
    execution_context: BrowserQualityGateExecutionContext,
    bundle: BrowserQualityBundleMaterializationInput,
) -> bool:
    return any(
        (
            not bundle.gate_run_id,
            not bundle.apply_result_id,
            not bundle.spec_dir,
            not bundle.attachment_scope_ref,
            not bundle.managed_frontend_target,
            not bundle.readiness_subject_id,
            not bundle.source_artifact_ref,
            execution_context.solution_snapshot_id != bundle.solution_snapshot_id,
            execution_context.gate_run_id != bundle.gate_run_id,
            execution_context.apply_result_id != bundle.apply_result_id,
            execution_context.spec_dir != bundle.spec_dir,
            execution_context.attachment_scope_ref != bundle.attachment_scope_ref,
            execution_context.managed_frontend_target != bundle.managed_frontend_target,
            execution_context.readiness_subject_id != bundle.readiness_subject_id,
        )
    )


def _browser_gate_receipts_match_required_probe_set(
    required_probe_set: list[str],
    receipts: list[BrowserProbeExecutionReceipt],
) -> bool:
    required = {str(name).strip() for name in required_probe_set if str(name).strip()}
    present = {
        str(receipt.check_name).strip()
        for receipt in receipts
        if str(receipt.check_name).strip()
    }
    return bool(required) and present == required


def _expected_browser_gate_status(
    receipts: list[BrowserProbeExecutionReceipt],
) -> str:
    verdicts = {receipt.classification_candidate for receipt in receipts}
    if {"evidence_missing", "transient_run_failure"} & verdicts:
        return "incomplete"
    if "actual_quality_blocker" in verdicts:
        return "blocked"
    if "advisory_only" in verdicts:
        return "passed_with_advisories"
    return "passed"


def _browser_gate_blockers(
    receipts: list[BrowserProbeExecutionReceipt],
    fallback_hints: tuple[str, ...],
) -> list[str]:
    blockers: list[str] = []
    for receipt in receipts:
        if receipt.classification_candidate in {"pass", "advisory_only"}:
            continue
        receipt_hints = _unique_strings(receipt.remediation_hints)
        reason_codes = _unique_strings(receipt.blocking_reason_codes)
        hint = (
            receipt_hints[0]
            if receipt_hints
            else reason_codes[0]
            if reason_codes
            else "review browser gate receipt"
        )
        blockers.append(f"browser gate check {receipt.check_name}: {hint}")
    if not blockers and fallback_hints:
        blockers.extend(_unique_strings(fallback_hints))
    return _unique_strings(blockers)


__all__ = [
    "FRONTEND_GATE_DECISION_REASON_ADVISORY_ONLY",
    "FRONTEND_GATE_EXECUTE_STATE_BLOCKED",
    "FRONTEND_GATE_EXECUTE_STATE_NEEDS_REMEDIATION",
    "FRONTEND_GATE_EXECUTE_STATE_READY",
    "FRONTEND_GATE_EXECUTE_STATE_RECHECK_REQUIRED",
    "FrontendGateExecuteDecision",
    "build_frontend_browser_gate_execute_decision",
]

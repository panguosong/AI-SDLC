"""Minimal project-scoped frontend delivery orchestration."""

from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_sdlc.core.frontend_browser_gate_runtime import (
    build_browser_quality_gate_execution_context,
    materialize_browser_gate_probe_runtime,
)
from ai_sdlc.core.frontend_visual_baseline import (
    FRONTEND_VISUAL_BASELINES,
    compute_frontend_visual_baseline_identity,
)
from ai_sdlc.core.managed_delivery_apply import run_managed_delivery_apply
from ai_sdlc.models.frontend_managed_delivery import (
    ConfirmedActionPlanExecutionView,
    DeliveryApplyDecisionReceipt,
    FrontendActionPlanAction,
    ManagedDeliveryExecutorContext,
)
from ai_sdlc.models.frontend_solution_confirmation import (
    AvailabilitySummary,
    FrontendSolutionSnapshot,
    build_mvp_solution_snapshot,
)
from ai_sdlc.utils.helpers import now_iso

FRONTEND_DELIVERY_MEMORY_ROOT = Path(".ai-sdlc/memory/frontend-delivery")
FRONTEND_SOLUTION_LATEST = FRONTEND_DELIVERY_MEMORY_ROOT / "solution/latest.yaml"
FRONTEND_SOLUTION_VERSIONS = FRONTEND_DELIVERY_MEMORY_ROOT / "solution/versions"
FRONTEND_APPLY_LATEST = FRONTEND_DELIVERY_MEMORY_ROOT / "apply/latest.yaml"
FRONTEND_APPLY_VERSIONS = FRONTEND_DELIVERY_MEMORY_ROOT / "apply/versions"
FRONTEND_BROWSER_LATEST = FRONTEND_DELIVERY_MEMORY_ROOT / "browser/latest.yaml"
FRONTEND_BROWSER_VERSIONS = FRONTEND_DELIVERY_MEMORY_ROOT / "browser/versions"


@dataclass(frozen=True)
class FrontendDeliveryCommandResult:
    """Stable Result/Next/Blockers response for frontend delivery commands."""

    status: str
    result: str
    next_action: str
    blockers: tuple[str, ...] = ()
    artifact_path: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "result": self.result,
            "next_action": self.next_action,
            "blockers": list(self.blockers),
            "artifact_path": self.artifact_path,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class FrontendProjectFacts:
    """Small set of existing project facts used to recommend a solution."""

    frontend_stack: str = ""
    provider_id: str = ""
    style_pack_id: str = ""
    backend_stack: str = "existing"
    source: str = "project-files"


class FrontendDeliveryService:
    """Compose solution, apply, and browser evidence without a second state machine."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def confirm_solution(
        self,
        *,
        frontend_stack: str = "",
        provider_id: str = "",
        style_pack_id: str = "",
        work_item: str = "",
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> FrontendDeliveryCommandResult:
        facts = self.detect_project_facts()
        choice_contract = {
            "recommended_option_source": "existing-project-facts",
            "alternative_options": [
                {
                    "option_id": "custom",
                    "label": "Custom choice",
                    "required_fields": [
                        "frontend_stack",
                        "provider_id",
                        "style_pack_id",
                    ],
                }
            ],
            "custom_choice_supported": True,
        }
        work_item_id = self._resolve_work_item_id(work_item)
        selected_stack = frontend_stack.strip() or facts.frontend_stack
        selected_provider = provider_id.strip() or facts.provider_id
        selected_style = style_pack_id.strip() or facts.style_pack_id
        missing = [
            name
            for name, value in (
                ("frontend_stack", selected_stack),
                ("provider_id", selected_provider),
                ("style_pack_id", selected_style),
            )
            if not value
        ]
        if not work_item_id:
            missing.append("work_item")
        if missing:
            return FrontendDeliveryCommandResult(
                status="needs_user",
                result="Frontend solution needs project facts or an explicit choice.",
                next_action=(
                    "Choose one recommended or custom solution, then rerun "
                    "ai-sdlc loop frontend-evidence solution-confirm with explicit options."
                ),
                blockers=("frontend_solution_selection_incomplete",),
                payload={
                    "missing_fields": missing,
                    "project_facts": facts.__dict__,
                    **choice_contract,
                },
            )
        if not dry_run and not confirmed:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Frontend solution was not persisted.",
                next_action="Review the selected solution and pass --yes with --execute.",
                blockers=("solution_confirmation_missing",),
            )

        previous = self.load_latest_solution()
        timestamp = now_iso()
        user_override_fields = [
            name
            for name, requested, detected in (
                ("frontend_stack", frontend_stack.strip(), facts.frontend_stack),
                ("provider_id", provider_id.strip(), facts.provider_id),
                ("style_pack_id", style_pack_id.strip(), facts.style_pack_id),
            )
            if requested and requested != detected
        ]
        snapshot = build_mvp_solution_snapshot(
            previous_snapshot=previous,
            project_id=work_item_id,
            created_at=timestamp,
            confirmed_at=timestamp if not dry_run else "",
            confirmed_by_mode="explicit" if user_override_fields else "project-facts",
            decision_status="user_confirmed" if not dry_run else "recommended",
            recommended_project_shape="existing-project",
            recommended_frontend_stack=facts.frontend_stack or selected_stack,
            recommended_provider_id=facts.provider_id or selected_provider,
            recommended_backend_stack=facts.backend_stack,
            recommended_api_collab_mode="project-defined",
            recommended_style_pack_id=facts.style_pack_id or selected_style,
            recommendation_source=facts.source,
            recommendation_reason_codes=["existing-project-facts"],
            recommendation_reason_text=(
                "The recommendation is derived from existing project files and the explicit user choice."
            ),
            requested_project_shape="existing-project",
            requested_frontend_stack=selected_stack,
            requested_provider_id=selected_provider,
            requested_backend_stack=facts.backend_stack,
            requested_api_collab_mode="project-defined",
            requested_style_pack_id=selected_style,
            effective_project_shape="existing-project",
            effective_frontend_stack=selected_stack,
            effective_provider_id=selected_provider,
            effective_backend_stack=facts.backend_stack,
            effective_api_collab_mode="project-defined",
            effective_style_pack_id=selected_style,
            availability_checks=[],
            availability_summary=AvailabilitySummary(
                overall_status="ready",
                passed_check_ids=[],
                failed_check_ids=[],
                blocking_reason_codes=[],
            ),
            availability_reason_text="The selected solution uses local project facts.",
            preflight_status="ready",
            preflight_reason_codes=[],
            user_overrode_recommendation=bool(user_override_fields),
            user_override_fields=user_override_fields,
            provider_mode="normal",
            style_fidelity_status=self._style_fidelity(
                provider_id=selected_provider,
                style_pack_id=selected_style,
            ),
            style_degradation_reason_codes=[],
        )
        relative_path = FRONTEND_SOLUTION_LATEST.as_posix()
        if not dry_run:
            self._write_solution(snapshot)
        return FrontendDeliveryCommandResult(
            status="dry_run" if dry_run else "ready",
            result=(
                "Frontend solution preview is ready."
                if dry_run
                else "Frontend solution was confirmed."
            ),
            next_action=(
                "Confirm with --execute --yes."
                if dry_run
                else "Run ai-sdlc loop frontend-evidence apply --dry-run."
            ),
            artifact_path="" if dry_run else relative_path,
            payload={
                "solution": snapshot.model_dump(mode="json"),
                "project_facts": facts.__dict__,
                "work_item_path": f"specs/{work_item_id}",
                **choice_contract,
            },
        )

    def apply_solution(
        self,
        *,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> FrontendDeliveryCommandResult:
        snapshot = self.load_latest_solution()
        if snapshot is None:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Frontend apply is blocked.",
                next_action="Confirm the frontend solution first.",
                blockers=("frontend_solution_snapshot_missing",),
            )
        if snapshot.decision_status not in {"user_confirmed", "fallback_confirmed"}:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Frontend apply is blocked by an unconfirmed solution.",
                next_action="Persist the selected solution with --execute --yes first.",
                blockers=("frontend_solution_not_confirmed",),
                payload={"solution_snapshot_id": snapshot.snapshot_id},
            )
        if not dry_run and not confirmed:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Frontend apply was not executed.",
                next_action="Review the apply plan and pass --execute --yes.",
                blockers=("apply_confirmation_missing",),
            )

        view, receipt = self._build_apply_inputs(snapshot)
        apply_result = run_managed_delivery_apply(
            view,
            receipt,
            ManagedDeliveryExecutorContext(
                execute_actions=not dry_run,
                repo_root=self.root,
            ),
        )
        payload = {
            **apply_result.model_dump(mode="json"),
            "solution_snapshot_id": snapshot.snapshot_id,
            "generated_at": now_iso(),
            "project_root": self.root.as_posix(),
            "execution_view": view.model_dump(mode="json"),
            "decision_receipt": receipt.model_dump(mode="json"),
            "source_tree_digest": self._managed_tree_digest(),
        }
        if apply_result.result_status != "apply_succeeded_pending_browser_gate":
            blockers = tuple(apply_result.blockers or ["frontend_apply_failed"])
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Frontend apply could not complete.",
                next_action=(
                    apply_result.recommended_next_steps[0]
                    if apply_result.recommended_next_steps
                    else "Resolve the apply blockers, then retry."
                ),
                blockers=blockers,
                payload=payload,
            )

        artifact_path = ""
        if not dry_run:
            artifact_path = FRONTEND_APPLY_LATEST.as_posix()
            self._write_apply(payload, snapshot.snapshot_id)
        return FrontendDeliveryCommandResult(
            status="dry_run" if dry_run else "ready",
            result=(
                "Frontend apply preview is ready."
                if dry_run
                else "Frontend apply completed and awaits browser evidence."
            ),
            next_action=(
                "Run again with --execute --yes."
                if dry_run
                else "Run ai-sdlc loop frontend-evidence capture --execute."
            ),
            artifact_path=artifact_path,
            payload=payload,
        )

    def capture_evidence(
        self,
        *,
        dry_run: bool = True,
        probe_runner: Callable[..., Any] | None = None,
    ) -> FrontendDeliveryCommandResult:
        snapshot, apply_payload, blocker = self._current_apply_context()
        if snapshot is None or apply_payload is None:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Browser capture is blocked.",
                next_action="Confirm and apply the current frontend solution first.",
                blockers=(blocker or "frontend_apply_missing",),
            )
        matrix_id = self._visual_matrix_id(
            snapshot.snapshot_id,
            str(apply_payload["source_tree_digest"]),
        )
        baseline_root = FRONTEND_VISUAL_BASELINES / matrix_id
        timestamp = now_iso()
        gate_run_id = self._gate_run_id(snapshot.snapshot_id, timestamp)
        context = build_browser_quality_gate_execution_context(
            apply_payload=apply_payload,
            solution_snapshot=snapshot,
            gate_run_id=gate_run_id,
            delivery_entry_id=(
                f"{snapshot.effective_frontend_stack}-{snapshot.effective_provider_id}"
            ),
            visual_regression_matrix_id=matrix_id,
            visual_regression_viewport_id="desktop-1440",
            visual_regression_baseline_root=baseline_root.as_posix(),
        )
        if dry_run:
            return FrontendDeliveryCommandResult(
                status="dry_run",
                result="Browser capture preview is ready.",
                next_action="Run again with --execute to capture real evidence.",
                payload={
                    "execution_context": context.model_dump(mode="json"),
                    "baseline_exists": (
                        self.root / baseline_root / "baseline.png"
                    ).is_file(),
                },
            )

        try:
            baseline_identity = compute_frontend_visual_baseline_identity(
                self.root,
                baseline_root.as_posix(),
            )
        except (OSError, ValueError) as exc:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Browser capture is blocked by an invalid visual baseline.",
                next_action="Repair or recreate the project-local visual baseline.",
                blockers=(str(exc),),
            )

        session, records, receipts, bundle = materialize_browser_gate_probe_runtime(
            root=self.root,
            context=context,
            apply_artifact_path=FRONTEND_APPLY_LATEST.as_posix(),
            visual_a11y_evidence_artifact=None,
            generated_at=timestamp,
            write_artifacts=True,
            probe_runner=probe_runner,
            execute_probe=True,
            auto_visual_a11y_provider=True,
        )
        try:
            baseline_identity_after = compute_frontend_visual_baseline_identity(
                self.root,
                baseline_root.as_posix(),
            )
        except (OSError, ValueError) as exc:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Browser capture is blocked by an invalid visual baseline.",
                next_action="Repair or recreate the project-local visual baseline.",
                blockers=(str(exc),),
            )
        baseline_changed_during_capture = baseline_identity_after != baseline_identity
        capture_blockers = list(bundle.blocking_reason_codes)
        if baseline_changed_during_capture:
            capture_blockers.append("visual_baseline_changed_during_capture")
        payload = {
            "schema_version": "frontend-browser-capture/v1",
            "generated_at": timestamp,
            "source_tree_digest": apply_payload["source_tree_digest"],
            "apply_artifact_path": FRONTEND_APPLY_LATEST.as_posix(),
            "artifact_root": session.artifact_root_ref,
            "probe_runtime_state": session.status,
            "execution_context": context.model_dump(mode="json"),
            "runtime_session": session.model_dump(mode="json"),
            "visual_baseline": baseline_identity or {},
            "artifact_records": [item.model_dump(mode="json") for item in records],
            "receipts": [item.model_dump(mode="json") for item in receipts],
            "bundle_input": bundle.model_dump(mode="json"),
            "warnings": list(session.warnings),
            "plain_language_blockers": capture_blockers,
            "recommended_next_steps": [
                "Run ai-sdlc loop frontend-evidence capture --execute."
            ],
        }
        self._write_browser(payload, gate_run_id)
        if baseline_changed_during_capture:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="The visual baseline changed during browser capture.",
                next_action="Run browser capture again against the current baseline.",
                blockers=("visual_baseline_changed_during_capture",),
                artifact_path=FRONTEND_BROWSER_LATEST.as_posix(),
                payload=payload,
            )
        non_visual_failures = [
            item
            for item in receipts
            if item.check_name != "visual_regression"
            and item.classification_candidate not in {"pass", "advisory_only"}
        ]
        visual_receipt = next(
            (item for item in receipts if item.check_name == "visual_regression"),
            None,
        )
        if not non_visual_failures and visual_receipt is not None:
            if visual_receipt.classification_candidate == "evidence_missing":
                return FrontendDeliveryCommandResult(
                    status="needs_recheck",
                    result="Browser, visual, and accessibility evidence was captured; a comparison baseline is still required.",
                    next_action=(
                        "Run ai-sdlc loop frontend-evidence baseline --execute --yes, "
                        "then capture again."
                    ),
                    blockers=("visual_baseline_missing",),
                    artifact_path=FRONTEND_BROWSER_LATEST.as_posix(),
                    payload=payload,
                )
            if visual_receipt.classification_candidate in {"pass", "advisory_only"}:
                return FrontendDeliveryCommandResult(
                    status="ready",
                    result="Browser, visual, and accessibility evidence passed.",
                    next_action=(
                        "Start the Frontend Evidence Loop with the current browser artifact."
                    ),
                    artifact_path=FRONTEND_BROWSER_LATEST.as_posix(),
                    payload=payload,
                )
        return FrontendDeliveryCommandResult(
            status="blocked",
            result="Browser evidence did not satisfy the required checks.",
            next_action="Resolve the captured blockers and rerun capture.",
            blockers=tuple(
                bundle.blocking_reason_codes or ["browser_evidence_blocked"]
            ),
            artifact_path=FRONTEND_BROWSER_LATEST.as_posix(),
            payload=payload,
        )

    def establish_baseline(
        self,
        *,
        artifact: str = "",
        threshold: float = 0.03,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> FrontendDeliveryCommandResult:
        if not dry_run and not confirmed:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Visual baseline was not written.",
                next_action="Review the bootstrap capture and pass --execute --yes.",
                blockers=("baseline_confirmation_missing",),
            )
        browser_payload = self.load_latest_browser()
        if browser_payload is None:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Visual baseline is blocked.",
                next_action="Run the first browser capture before creating a baseline.",
                blockers=("browser_capture_missing",),
            )
        context = browser_payload.get("execution_context")
        if not isinstance(context, dict):
            return self._invalid_browser_artifact()
        matrix_id = str(context.get("visual_regression_matrix_id", "")).strip()
        baseline_root_ref = str(
            context.get("visual_regression_baseline_root", "")
        ).strip()
        if not matrix_id or not baseline_root_ref:
            return self._invalid_browser_artifact()
        screenshot_ref = self._bootstrap_screenshot_ref(browser_payload)
        if not screenshot_ref:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="Visual baseline is blocked by missing bootstrap evidence.",
                next_action="Run capture again and retain its navigation screenshot.",
                blockers=("bootstrap_screenshot_missing",),
            )
        if artifact.strip() and artifact.strip() != screenshot_ref:
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="The requested baseline artifact is not the latest bootstrap screenshot.",
                next_action="Use the artifact emitted by the latest capture.",
                blockers=("baseline_artifact_not_current",),
            )
        source = self._resolve_project_file(screenshot_ref)
        if source is None or not source.is_file():
            return FrontendDeliveryCommandResult(
                status="blocked",
                result="The bootstrap screenshot is unavailable.",
                next_action="Run capture again before creating the baseline.",
                blockers=("bootstrap_screenshot_missing",),
            )
        baseline_root = self._resolve_project_directory(baseline_root_ref)
        if baseline_root is None:
            return self._invalid_browser_artifact()
        metadata = {
            "schema_version": "frontend-visual-baseline/v1",
            "matrix_id": matrix_id,
            "threshold": threshold,
            "created_at": now_iso(),
            "source_gate_run_id": str(
                (browser_payload.get("bundle_input") or {}).get("gate_run_id", "")
            ),
            "solution_snapshot_id": str(context.get("solution_snapshot_id", "")),
            "apply_result_id": str(context.get("apply_result_id", "")),
            "source_tree_digest": str(browser_payload.get("source_tree_digest", "")),
            "source_screenshot_ref": screenshot_ref,
        }
        if not dry_run:
            baseline_root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, baseline_root / "baseline.png")
            (baseline_root / "baseline.yaml").write_text(
                yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            metadata["visual_baseline_identity"] = (
                compute_frontend_visual_baseline_identity(
                    self.root,
                    baseline_root_ref,
                )
                or {}
            )
        return FrontendDeliveryCommandResult(
            status="dry_run" if dry_run else "ready",
            result=(
                "Visual baseline preview is ready."
                if dry_run
                else "Visual baseline was created without changing the bootstrap capture."
            ),
            next_action=(
                "Confirm with --execute --yes."
                if dry_run
                else "Run ai-sdlc loop frontend-evidence capture --execute again."
            ),
            artifact_path=(
                ""
                if dry_run
                else (Path(baseline_root_ref) / "baseline.yaml").as_posix()
            ),
            payload=metadata,
        )

    def load_latest_solution(self) -> FrontendSolutionSnapshot | None:
        path = self.root / FRONTEND_SOLUTION_LATEST
        if not path.is_file():
            return None
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return FrontendSolutionSnapshot.model_validate(payload)

    def load_latest_apply(self) -> dict[str, Any] | None:
        path = self.root / FRONTEND_APPLY_LATEST
        if not path.is_file():
            return None
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return None
        return payload

    def load_latest_browser(self) -> dict[str, Any] | None:
        path = self.root / FRONTEND_BROWSER_LATEST
        if not path.is_file():
            return None
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else None

    def detect_project_facts(self) -> FrontendProjectFacts:
        package_path = self.root / "package.json"
        if not package_path.is_file():
            return FrontendProjectFacts()
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return FrontendProjectFacts(source="package-json-invalid")
        dependencies = {
            str(key): str(value)
            for section in ("dependencies", "devDependencies")
            for key, value in (payload.get(section) or {}).items()
        }
        vue_version = dependencies.get("vue", "")
        if vue_version:
            stack = "vue2" if "2" in vue_version.lstrip("^~<>= ")[:1] else "vue3"
            provider = "public-primevue" if "primevue" in dependencies else "custom"
            style = (
                "modern-saas" if provider == "public-primevue" else "project-defined"
            )
            return FrontendProjectFacts(
                frontend_stack=stack,
                provider_id=provider,
                style_pack_id=style,
                source="package-json",
            )
        if "react" in dependencies:
            return FrontendProjectFacts(
                frontend_stack="react",
                provider_id="custom",
                style_pack_id="project-defined",
                source="package-json",
            )
        return FrontendProjectFacts(source="package-json-no-frontend-facts")

    def _resolve_work_item_id(self, work_item: str) -> str:
        requested = work_item.strip().replace("\\", "/").rstrip("/")
        if requested:
            candidate = Path(requested)
            if (
                candidate.is_absolute()
                or candidate.parts[:1] != ("specs",)
                or len(candidate.parts) != 2
                or candidate.name in {".", ".."}
            ):
                return ""
            resolved = (self.root / candidate).resolve()
            try:
                resolved.relative_to(self.root / "specs")
            except ValueError:
                return ""
            return candidate.name if resolved.is_dir() else ""
        specs_root = self.root / "specs"
        if not specs_root.is_dir():
            return ""
        candidates = sorted(
            item.name
            for item in specs_root.iterdir()
            if item.is_dir() and not item.is_symlink()
        )
        return candidates[0] if len(candidates) == 1 else ""

    def _write_solution(self, snapshot: FrontendSolutionSnapshot) -> None:
        latest = self.root / FRONTEND_SOLUTION_LATEST
        version = (
            self.root / FRONTEND_SOLUTION_VERSIONS / f"{snapshot.snapshot_id}.yaml"
        )
        content = yaml.safe_dump(
            snapshot.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
        )
        latest.parent.mkdir(parents=True, exist_ok=True)
        version.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(content, encoding="utf-8")
        version.write_text(content, encoding="utf-8")

    def _build_apply_inputs(
        self,
        snapshot: FrontendSolutionSnapshot,
    ) -> tuple[ConfirmedActionPlanExecutionView, DeliveryApplyDecisionReceipt]:
        managed_target = "managed/frontend"
        generated_files = self._generated_frontend_files(snapshot)
        plan_seed = {
            "solution_snapshot_id": snapshot.snapshot_id,
            "frontend_stack": snapshot.effective_frontend_stack,
            "provider_id": snapshot.effective_provider_id,
            "style_pack_id": snapshot.effective_style_pack_id,
            "files": generated_files,
        }
        plan_fingerprint = hashlib.sha256(
            json.dumps(plan_seed, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        plan_id = f"frontend-plan-{snapshot.snapshot_id}"
        surface_id = f"frontend-surface-{snapshot.snapshot_id}"
        action_ids = ["managed-target-prepare", "artifact-generate"]
        view = ConfirmedActionPlanExecutionView(
            action_plan_id=plan_id,
            confirmation_surface_id=surface_id,
            plan_fingerprint=plan_fingerprint,
            protocol_version="1",
            managed_target_ref=f"managed://frontend/{snapshot.project_id}",
            managed_target_path=managed_target,
            attachment_scope_ref=f"project://{snapshot.project_id}",
            readiness_subject_id=snapshot.project_id,
            spec_dir=f"specs/{snapshot.project_id}",
            action_items=[
                FrontendActionPlanAction(
                    action_id=action_ids[0],
                    effect_kind="mutate",
                    action_type="managed_target_prepare",
                    rollback_ref="remove-new-managed-target-files",
                    retry_ref="rerun-after-project-facts-confirmation",
                    cleanup_ref="remove-empty-managed-target-directories",
                    source_linkage_refs={
                        "solution_snapshot_id": snapshot.snapshot_id,
                    },
                    executor_payload={"directories": ["."], "files": []},
                ),
                FrontendActionPlanAction(
                    action_id=action_ids[1],
                    effect_kind="mutate",
                    action_type="artifact_generate",
                    depends_on_action_ids=[action_ids[0]],
                    rollback_ref="restore-managed-target-files",
                    retry_ref="rerun-from-confirmed-snapshot",
                    cleanup_ref="remove-generated-delivery-files",
                    source_linkage_refs={
                        "solution_snapshot_id": snapshot.snapshot_id,
                    },
                    executor_payload={
                        "directories": [],
                        "files": [
                            {"path": path, "content": content}
                            for path, content in generated_files.items()
                        ],
                        "cleanup_files": [],
                    },
                ),
            ],
            will_not_touch=[".git", ".ai-sdlc"],
        )
        receipt = DeliveryApplyDecisionReceipt(
            decision_receipt_id=f"frontend-receipt-{snapshot.snapshot_id}",
            action_plan_id=plan_id,
            confirmation_surface_id=surface_id,
            decision="continue",
            selected_action_ids=action_ids,
            second_confirmation_acknowledged=True,
            confirmed_plan_fingerprint=plan_fingerprint,
            created_at=now_iso(),
        )
        return view, receipt

    def _generated_frontend_files(
        self,
        snapshot: FrontendSolutionSnapshot,
    ) -> dict[str, str]:
        title = html.escape(f"{self.root.name} frontend delivery")
        stack = html.escape(snapshot.effective_frontend_stack)
        provider = html.escape(snapshot.effective_provider_id)
        style = html.escape(snapshot.effective_style_pack_id)
        index_content = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: #f6f8fb; color: #172033; }}
    main {{ max-width: 58rem; margin: 4rem auto; padding: 2rem; }}
    section {{ background: #fff; border: 1px solid #dce3ee; border-radius: 1rem; padding: 2rem; }}
    button {{ background: #1770e6; color: #fff; border: 0; border-radius: .5rem; padding: .75rem 1rem; }}
  </style>
</head>
<body>
  <main>
    <section aria-labelledby=\"delivery-title\">
      <h1 id=\"delivery-title\">{title}</h1>
      <div class=\"entry-eyebrow\">{stack}-{provider}</div>
      <p>Confirmed stack: <strong>{stack}</strong></p>
      <p>Confirmed provider: <strong>{provider}</strong></p>
      <p>Confirmed style: <strong>{style}</strong></p>
      <button type=\"button\" aria-label=\"Confirm delivery preview\">Confirm preview</button>
      <script id=\"frontend-delivery-context\" type=\"application/json\">{{"deliveryEntryId":"{stack}-{provider}"}}</script>
    </section>
  </main>
</body>
</html>
"""
        descriptor = json.dumps(
            {
                "schema_version": "frontend-delivery/v1",
                "solution_snapshot_id": snapshot.snapshot_id,
                "frontend_stack": snapshot.effective_frontend_stack,
                "provider_id": snapshot.effective_provider_id,
                "style_pack_id": snapshot.effective_style_pack_id,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return {"index.html": index_content, "delivery.json": descriptor + "\n"}

    def _write_apply(self, payload: dict[str, Any], snapshot_id: str) -> None:
        latest = self.root / FRONTEND_APPLY_LATEST
        version = self.root / FRONTEND_APPLY_VERSIONS / f"{snapshot_id}.yaml"
        content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        latest.parent.mkdir(parents=True, exist_ok=True)
        version.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(content, encoding="utf-8")
        version.write_text(content, encoding="utf-8")

    def _write_browser(self, payload: dict[str, Any], gate_run_id: str) -> None:
        latest = self.root / FRONTEND_BROWSER_LATEST
        version = self.root / FRONTEND_BROWSER_VERSIONS / f"{gate_run_id}.yaml"
        content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        latest.parent.mkdir(parents=True, exist_ok=True)
        version.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(content, encoding="utf-8")
        version.write_text(content, encoding="utf-8")

    def _current_apply_context(
        self,
    ) -> tuple[FrontendSolutionSnapshot | None, dict[str, Any] | None, str]:
        snapshot = self.load_latest_solution()
        if snapshot is None:
            return None, None, "frontend_solution_snapshot_missing"
        apply_payload = self.load_latest_apply()
        if apply_payload is None:
            return snapshot, None, "frontend_apply_missing"
        if apply_payload.get("solution_snapshot_id") != snapshot.snapshot_id:
            return snapshot, None, "frontend_apply_stale_for_solution"
        current_digest = self._managed_tree_digest()
        if (
            not current_digest
            or apply_payload.get("source_tree_digest") != current_digest
        ):
            return snapshot, None, "frontend_apply_stale_for_source_tree"
        if apply_payload.get("result_status") != "apply_succeeded_pending_browser_gate":
            return snapshot, None, "frontend_apply_not_browser_eligible"
        return snapshot, apply_payload, ""

    def _bootstrap_screenshot_ref(self, payload: dict[str, Any]) -> str:
        records = payload.get("artifact_records")
        if not isinstance(records, list):
            return ""
        for record in records:
            if not isinstance(record, dict):
                continue
            if (
                record.get("artifact_type") == "navigation_screenshot"
                and record.get("capture_status") == "captured"
            ):
                return str(record.get("artifact_ref", "")).strip()
        return ""

    def _resolve_project_file(self, relative_path: str) -> Path | None:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            return None
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None
        return resolved

    def _resolve_project_directory(self, relative_path: str) -> Path | None:
        resolved = self._resolve_project_file(relative_path)
        if resolved is None:
            return None
        allowed = (self.root / FRONTEND_VISUAL_BASELINES).resolve()
        try:
            resolved.relative_to(allowed)
        except ValueError:
            return None
        return resolved

    @staticmethod
    def _visual_matrix_id(snapshot_id: str, tree_digest: str) -> str:
        seed = f"{snapshot_id}:{tree_digest}".encode()
        return f"frontend-{hashlib.sha256(seed).hexdigest()[:16]}"

    @staticmethod
    def _gate_run_id(snapshot_id: str, timestamp: str) -> str:
        suffix = hashlib.sha256(
            f"{snapshot_id}:{timestamp}:{os.getpid()}".encode()
        ).hexdigest()[:12]
        return f"frontend-{snapshot_id}-{suffix}"

    @staticmethod
    def _invalid_browser_artifact() -> FrontendDeliveryCommandResult:
        return FrontendDeliveryCommandResult(
            status="blocked",
            result="The latest browser artifact is invalid.",
            next_action="Run capture again from the current apply receipt.",
            blockers=("browser_capture_invalid",),
        )

    def _managed_tree_digest(self) -> str:
        target = self.root / "managed/frontend"
        digest = hashlib.sha256()
        if not target.is_dir():
            return ""
        for path in sorted(item for item in target.rglob("*") if item.is_file()):
            digest.update(path.relative_to(target).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _style_fidelity(*, provider_id: str, style_pack_id: str) -> str:
        if provider_id == "custom" or style_pack_id == "project-defined":
            return "partial"
        return "full"


__all__ = [
    "FRONTEND_DELIVERY_MEMORY_ROOT",
    "FRONTEND_APPLY_LATEST",
    "FRONTEND_BROWSER_LATEST",
    "FRONTEND_SOLUTION_LATEST",
    "FRONTEND_VISUAL_BASELINES",
    "FrontendDeliveryCommandResult",
    "FrontendDeliveryService",
    "FrontendProjectFacts",
]

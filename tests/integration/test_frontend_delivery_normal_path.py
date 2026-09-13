"""Normal frontend delivery entry is usable with Program imports blocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ai_sdlc.cli.loop_review_cmd import (
    prepare_current_loop_review,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.core.frontend_delivery_service import (
    FRONTEND_APPLY_LATEST,
    FRONTEND_BROWSER_LATEST,
    FrontendDeliveryService,
)
from ai_sdlc.core.frontend_evidence_loop import (
    FrontendEvidenceCloseOptions,
    FrontendEvidenceStartOptions,
    close_frontend_evidence_loop,
    start_frontend_evidence_loop,
)
from ai_sdlc.core.implementation_models import (
    ImplementationClose,
    ImplementationCurrentPointer,
    ImplementationReport,
)
from ai_sdlc.core.implementation_store import implementation_artifacts
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRound, LoopRun, LoopStatus, LoopType
from ai_sdlc.core.loop_review_service import (
    RecordLoopReviewOptions,
    record_loop_review,
)
from ai_sdlc.core.review_kernel import ReviewExecution
from ai_sdlc.models.frontend_browser_gate import BrowserGateProbeRunnerResult

ROOT = Path(__file__).resolve().parents[2]


def _fake_probe_runner(root: Path):
    def runner(*, artifact_root: Path, execution_context, generated_at: str):
        del generated_at
        trace = artifact_root / "shared-runtime" / "playwright-trace.zip"
        screenshot = artifact_root / "shared-runtime" / "navigation-screenshot.png"
        interaction = artifact_root / "interaction" / "interaction-snapshot.json"
        bootstrap = artifact_root / "bootstrap" / "bootstrap-receipt.yaml"
        diff = artifact_root / "visual-regression" / "diff.png"
        for path in (trace, screenshot, interaction, bootstrap):
            path.parent.mkdir(parents=True, exist_ok=True)
        trace.write_bytes(b"trace")
        screenshot.write_bytes(b"png-bootstrap")
        interaction.write_text("{}\n", encoding="utf-8")
        bootstrap.write_text("status: ready\n", encoding="utf-8")
        baseline_root = root / execution_context.visual_regression_baseline_root
        baseline_exists = (baseline_root / "baseline.png").is_file()
        if baseline_exists:
            diff.parent.mkdir(parents=True, exist_ok=True)
            diff.write_bytes(b"png-diff")

        def relative(path: Path) -> str:
            return path.relative_to(root).as_posix()

        return BrowserGateProbeRunnerResult.model_validate(
            {
                "runtime_status": "completed",
                "shared_capture": {
                    "gate_run_id": execution_context.gate_run_id,
                    "trace_artifact_ref": relative(trace),
                    "navigation_screenshot_ref": relative(screenshot),
                    "capture_status": "captured",
                    "final_url": "file:///managed/frontend/index.html",
                    "anchor_refs": ["page:delivery"],
                    "diagnostic_codes": [],
                },
                "interaction_capture": {
                    "gate_run_id": execution_context.gate_run_id,
                    "interaction_probe_id": "delivery-preview",
                    "artifact_refs": [relative(interaction)],
                    "capture_status": "captured",
                    "classification_candidate": "pass",
                    "blocking_reason_codes": [],
                    "advisory_reason_codes": [],
                    "anchor_refs": ["interaction:delivery-preview"],
                },
                "quality_capture": {
                    "gate_run_id": execution_context.gate_run_id,
                    "page_title": "delivery",
                    "final_url": "file:///managed/frontend/index.html",
                    "screenshot_ref": relative(screenshot),
                    "body_text_char_count": 180,
                    "heading_count": 1,
                    "landmark_count": 1,
                    "interactive_count": 1,
                    "unlabeled_button_count": 0,
                    "unlabeled_input_count": 0,
                    "image_missing_alt_count": 0,
                    "viewport_width": 1440,
                    "viewport_height": 900,
                    "document_scroll_width": 1440,
                    "document_scroll_height": 900,
                    "horizontal_overflow_count": 0,
                    "low_contrast_text_count": 0,
                    "focusable_count": 1,
                    "focusable_without_visible_focus_count": 0,
                    "console_error_messages": [],
                    "page_error_messages": [],
                },
                "visual_regression_capture": {
                    "matrix_id": execution_context.visual_regression_matrix_id,
                    "gate_run_id": execution_context.gate_run_id,
                    "capture_status": "captured" if baseline_exists else "missing",
                    "screenshot_ref": relative(screenshot),
                    "baseline_ref": (
                        execution_context.visual_regression_baseline_root
                        + "/baseline.png"
                    ),
                    "baseline_metadata_ref": (
                        execution_context.visual_regression_baseline_root
                        + "/baseline.yaml"
                    ),
                    "diff_image_ref": relative(diff) if baseline_exists else "",
                    "diff_ratio": 0.0 if baseline_exists else 1.0,
                    "threshold": 0.03,
                    "region_summaries": [],
                    "change_summary": "pass" if baseline_exists else "baseline-missing",
                    "capture_protocol_ref": "frontend-delivery",
                    "bootstrap_ref": relative(bootstrap),
                    "verdict": "pass" if baseline_exists else "evidence_missing",
                },
                "diagnostic_codes": [],
                "warnings": [],
            }
        )

    return runner


def _write_closed_implementation(root: Path, work_item_id: str) -> None:
    loop_id = "impl-frontend-normal"
    design_loop_id = "dc-frontend-normal"
    artifacts = implementation_artifacts(root, loop_id)
    store = LoopArtifactStore(root)
    store.create_loop_run_dir(loop_id, loop_type=LoopType.IMPLEMENTATION.value)
    design_dir = root / ".ai-sdlc" / "loops" / "design-contract" / design_loop_id
    design_dir.mkdir(parents=True)
    store.write_json_artifact(
        design_dir / "design-contract-input.json",
        {
            "requirement_loop_id": "",
            "spec_path": f"specs/{work_item_id}/spec.md",
            "plan_path": f"specs/{work_item_id}/plan.md",
            "tasks_path": f"specs/{work_item_id}/tasks.md",
        },
    )
    store.write_json_artifact(design_dir / "design-contract-report.json", {})
    (design_dir / "design-contract-report.md").write_text(
        "# Design contract\n",
        encoding="utf-8",
    )
    store.write_json_artifact(
        artifacts.input_path,
        {"design_contract_loop_id": design_loop_id},
    )
    for path in (
        artifacts.tasks_path,
        artifacts.progress_path,
        artifacts.evidence_path,
    ):
        store.write_json_artifact(path, {})
    artifacts.report_md_path.write_text("# Implementation\n", encoding="utf-8")
    report = ImplementationReport(
        loop_id=loop_id,
        work_item_id=work_item_id,
        work_item_path=f"specs/{work_item_id}",
        status=LoopStatus.PASSED,
        required_task_count=1,
        done_count=1,
        requires_frontend_evidence=True,
        next_action=(
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item_id}."
        ),
    )
    loop_run = LoopRun(
        loop_id=loop_id,
        loop_type=LoopType.IMPLEMENTATION,
        status=LoopStatus.CLOSED,
        work_item_id=work_item_id,
        current_round=1,
        rounds=[
            LoopRound(
                round_number=1,
                command=["ai-sdlc", "loop", "implementation", "start"],
                status=LoopStatus.CLOSED,
                result=LoopStatus.CLOSED,
            )
        ],
        next_action=(
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item_id}."
        ),
    )
    store.write_json_artifact(artifacts.report_json_path, report)
    store.write_json_artifact(artifacts.loop_run_path, loop_run)
    store.write_json_artifact(
        artifacts.close_path,
        ImplementationClose(
            loop_id=loop_id,
            closed_at="2026-08-20T00:00:00Z",
            report_path=artifacts.report_json_path.relative_to(root).as_posix(),
            next_loop_type=LoopType.FRONTEND_EVIDENCE,
        ),
    )
    store.write_json_artifact(
        artifacts.pointer_path,
        ImplementationCurrentPointer(
            loop_id=loop_id,
            loop_run_path=artifacts.loop_run_path.relative_to(root).as_posix(),
        ),
    )


def _record_clean_review(root: Path, loop_id: str) -> str:
    prepared, loop_dir = prepare_current_loop_review(
        root,
        "frontend-evidence",
        loop_id,
    )
    result_paths: list[Path] = []
    for index, role in enumerate(prepared.review_input.expert_roles):
        result_path = root / f"frontend-expert-{index}.json"
        result_path.write_text(
            ReviewExecution(
                status="completed",
                roles=[role],
                role_reasons={
                    role: prepared.review_input.expert_reasons[role],
                },
                findings=[],
            ).model_dump_json(),
            encoding="utf-8",
        )
        result_paths.append(result_path)
    overlay = record_loop_review(
        RecordLoopReviewOptions(
            root=root,
            loop_type="frontend-evidence",
            loop_id=loop_id,
            expected_digest=prepared.review_input.input_digest,
            result_paths=tuple(result_paths),
        ),
        loop_dir=loop_dir,
        input_resolver=lambda round_number: resolve_review_input(
            root,
            loop_type="frontend-evidence",
            loop_id=loop_id,
            review_round_number=round_number,
        ),
    )
    assert overlay.status == "passed"
    return prepared.review_input.input_digest


def test_frontend_delivery_loop_help_works_without_program_imports(
    tmp_path: Path,
) -> None:
    hook_dir = tmp_path / "guarded-import"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        "import builtins\n"
        "original_import = builtins.__import__\n"
        "def guarded_import(name, *args, **kwargs):\n"
        "    if name in {\n"
        "        'ai_sdlc.cli.program_cmd',\n"
        "        'ai_sdlc.core.program_service',\n"
        "        'ai_sdlc.models.program',\n"
        "    }:\n"
        "        raise ModuleNotFoundError(f'blocked retired import: {name}')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = guarded_import\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(hook_dir), str(ROOT / "src")))
    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc", "loop", "frontend-evidence", "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for command in ("solution-confirm", "apply", "capture", "baseline"):
        assert command in result.stdout


def test_apply_requires_confirmation_and_persists_only_on_execute(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.5.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "specs" / "001-ui").mkdir(parents=True)
    service = FrontendDeliveryService(tmp_path)

    missing = service.apply_solution()
    assert missing.status == "blocked"
    assert missing.blockers == ("frontend_solution_snapshot_missing",)

    recommendation = service.confirm_solution(work_item="specs/001-ui", dry_run=True)
    assert recommendation.status == "dry_run"
    assert recommendation.payload["recommended_option_source"] == (
        "existing-project-facts"
    )
    assert recommendation.payload["alternative_options"] == [
        {
            "option_id": "custom",
            "label": "Custom choice",
            "required_fields": [
                "frontend_stack",
                "provider_id",
                "style_pack_id",
            ],
        }
    ]
    confirmed = service.confirm_solution(
        work_item="specs/001-ui", dry_run=False, confirmed=True
    )
    assert confirmed.status == "ready"
    preview = service.apply_solution(dry_run=True)
    assert preview.status == "dry_run"
    assert not (tmp_path / "managed" / "frontend").exists()
    assert not (tmp_path / FRONTEND_APPLY_LATEST).exists()

    applied = service.apply_solution(dry_run=False, confirmed=True)
    assert applied.status == "ready"
    index_path = tmp_path / "managed" / "frontend" / "index.html"
    assert index_path.is_file()
    index_text = index_path.read_text(encoding="utf-8")
    assert 'class="entry-eyebrow"' in index_text
    assert 'id="frontend-delivery-context"' in index_text
    assert (tmp_path / "managed" / "frontend" / "delivery.json").is_file()
    payload = yaml.safe_load((tmp_path / FRONTEND_APPLY_LATEST).read_text("utf-8"))
    assert payload["result_status"] == "apply_succeeded_pending_browser_gate"
    assert (
        payload["solution_snapshot_id"] == confirmed.payload["solution"]["snapshot_id"]
    )
    assert payload["source_tree_digest"]


def test_capture_requires_baseline_recheck_without_promoting_bootstrap(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.5.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "specs" / "001-ui").mkdir(parents=True)
    service = FrontendDeliveryService(tmp_path)
    assert (
        service.confirm_solution(
            work_item="specs/001-ui", dry_run=False, confirmed=True
        ).status
        == "ready"
    )
    assert service.apply_solution(dry_run=False, confirmed=True).status == "ready"

    probe_runner = _fake_probe_runner(tmp_path)
    first_capture = service.capture_evidence(
        dry_run=False,
        probe_runner=probe_runner,
    )
    assert first_capture.status == "needs_recheck"
    browser_path = tmp_path / FRONTEND_BROWSER_LATEST
    bootstrap_bytes = browser_path.read_bytes()

    baseline = service.establish_baseline(dry_run=False, confirmed=True)
    assert baseline.status == "ready"
    assert browser_path.read_bytes() == bootstrap_bytes

    second_capture = service.capture_evidence(
        dry_run=False,
        probe_runner=probe_runner,
    )
    assert second_capture.status == "ready"
    assert second_capture.payload["bundle_input"]["overall_gate_status"] in {
        "passed",
        "passed_with_advisories",
    }


def test_project_fact_delivery_reaches_review_bound_frontend_close(
    tmp_path: Path,
) -> None:
    work_item_id = "001-ui"
    work_item = tmp_path / "specs" / work_item_id
    work_item.mkdir(parents=True)
    (work_item / "spec.md").write_text("# Frontend requirement\n", encoding="utf-8")
    (work_item / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (work_item / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.5.0"}}),
        encoding="utf-8",
    )
    _write_closed_implementation(tmp_path, work_item_id)
    service = FrontendDeliveryService(tmp_path)
    probe_runner = _fake_probe_runner(tmp_path)

    assert (
        service.confirm_solution(
            work_item=f"specs/{work_item_id}",
            dry_run=False,
            confirmed=True,
        ).status
        == "ready"
    )
    assert service.apply_solution(dry_run=False, confirmed=True).status == "ready"
    assert (
        service.capture_evidence(
            dry_run=False,
            probe_runner=probe_runner,
        ).status
        == "needs_recheck"
    )
    bootstrap_start = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item=f"specs/{work_item_id}",
            implementation_loop_id="impl-frontend-normal",
            loop_id="fe-bootstrap-blocked",
        )
    )
    assert bootstrap_start.status == "needs_fix"
    assert bootstrap_start.closed is False

    browser_before_baseline = (tmp_path / FRONTEND_BROWSER_LATEST).read_bytes()
    assert service.establish_baseline(dry_run=False, confirmed=True).status == "ready"
    assert (tmp_path / FRONTEND_BROWSER_LATEST).read_bytes() == browser_before_baseline
    capture = service.capture_evidence(
        dry_run=False,
        probe_runner=probe_runner,
    )
    assert capture.status == "ready"
    original_baseline_digest = capture.payload["visual_baseline"]["digest"]

    assert (
        service.establish_baseline(
            threshold=0.04,
            dry_run=False,
            confirmed=True,
        ).status
        == "ready"
    )
    stale_start = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item=f"specs/{work_item_id}",
            implementation_loop_id="impl-frontend-normal",
            loop_id="fe-baseline-stale-before-start",
        )
    )
    assert stale_start.status == "blocked"
    assert "stale" in stale_start.blocker.lower()
    assert (
        service.capture_evidence(
            dry_run=False,
            probe_runner=probe_runner,
        ).status
        == "ready"
    )
    refreshed_payload = yaml.safe_load(
        (tmp_path / FRONTEND_BROWSER_LATEST).read_text(encoding="utf-8")
    )
    assert refreshed_payload["visual_baseline"]["digest"] != (original_baseline_digest)

    loop_id = "fe-project-fact"
    started = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item=f"specs/{work_item_id}",
            implementation_loop_id="impl-frontend-normal",
            loop_id=loop_id,
        )
    )
    assert started.status == "ready"
    assert started.loop_status == "needs_review"
    review_digest = _record_clean_review(tmp_path, loop_id)
    assert (
        service.establish_baseline(
            threshold=0.05,
            dry_run=False,
            confirmed=True,
        ).status
        == "ready"
    )
    with pytest.raises(ValueError, match="stale"):
        prepare_current_loop_review(tmp_path, "frontend-evidence", loop_id)
    stale_close = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
            closed_by="test-reviewer",
            expected_review_digest=review_digest,
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert stale_close.status == "needs_fix"
    assert "stale" in stale_close.blocker.lower()

    assert (
        service.capture_evidence(
            dry_run=False,
            probe_runner=probe_runner,
        ).status
        == "ready"
    )
    loop_id = "fe-project-fact-recaptured"
    restarted = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item=f"specs/{work_item_id}",
            implementation_loop_id="impl-frontend-normal",
            loop_id=loop_id,
        )
    )
    assert restarted.status == "ready"
    review_digest = _record_clean_review(tmp_path, loop_id)
    reviewed_artifacts: dict[str, bytes] = {}
    validate_review_input_for_close(
        tmp_path,
        loop_type="frontend-evidence",
        loop_id=loop_id,
        expected_digest=review_digest,
        captured_artifacts=reviewed_artifacts,
    )
    closed = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=tmp_path,
            loop_id=loop_id,
            yes=True,
            closed_by="test-reviewer",
            expected_review_digest=review_digest,
        ),
        review_input_validator=validate_review_input_for_close,
        reviewed_artifacts=reviewed_artifacts,
    )
    assert closed.status == "ready"
    assert closed.loop_status == "closed"
    assert closed.closed is True
    assert closed.next_action == "Run ai-sdlc pr-review start."

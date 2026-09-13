"""Tests for the deterministic frontend-evidence loop runtime."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import ai_sdlc.core.frontend_evidence_loop as frontend_evidence_loop_module
from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    validate_review_input_for_close,
)
from ai_sdlc.core.frontend_evidence_loop import (
    CURRENT_FRONTEND_EVIDENCE_PATH,
    FrontendEvidenceCloseOptions,
    FrontendEvidenceDoctorOptions,
    FrontendEvidenceSkipOptions,
    FrontendEvidenceStartOptions,
    close_frontend_evidence_loop,
    doctor_frontend_evidence_provider,
    skip_frontend_evidence_loop,
    start_frontend_evidence_loop,
)
from ai_sdlc.core.implementation_models import (
    ImplementationClose,
    ImplementationCurrentPointer,
    ImplementationReport,
)
from ai_sdlc.core.implementation_store import implementation_artifacts
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import (
    LoopRound,
    LoopRun,
    LoopStatus,
    LoopType,
    utc_now_iso,
)


def _closed_frontend_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    from tests.integration.test_cli_loop_review import (
        _prepare_cli_implementation_review,
        app,
        runner,
    )

    root = tmp_path
    # 复用辅助函数不会继承其测试模块的 cwd 夹具；真实 CLI hook 也须留在临时项目内。
    monkeypatch.chdir(root)
    loop, _, close_args = _prepare_cli_implementation_review(
        root, monkeypatch, frontend=True
    )
    closed = runner.invoke(app, close_args)
    assert closed.exit_code == 0, closed.output
    assert "review_binding" not in json.loads(
        (loop / "implementation-close.json").read_bytes()
    )
    artifact_path = _write_browser_gate_artifact(
        root, work_item_path="specs/demo-implementation-loop"
    )
    artifact = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    artifact["generated_at"] = utc_now_iso()
    artifact_path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
    return root, loop


@pytest.mark.parametrize("entry", ["start", "skip"])
def test_frontend_entry_accepts_normal_close_and_rejects_retired_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    root, implementation_loop = _closed_frontend_implementation(tmp_path, monkeypatch)

    def enter(loop_id: str):
        if entry == "start":
            return start_frontend_evidence_loop(
                FrontendEvidenceStartOptions(
                    root=root,
                    work_item="specs/demo-implementation-loop",
                    implementation_loop_id=implementation_loop.name,
                    loop_id=loop_id,
                ),
                review_input_validator=validate_review_input_for_close,
            )
        return skip_frontend_evidence_loop(
            FrontendEvidenceSkipOptions(
                root=root,
                work_item="specs/demo-implementation-loop",
                implementation_loop_id=implementation_loop.name,
                loop_id=loop_id,
                reason="Browser provider unavailable in this isolated fixture.",
                yes=True,
            ),
            review_input_validator=validate_review_input_for_close,
        )

    assert enter("fe-current-continuation").status == "ready"
    (implementation_loop / "review-continuation.json").write_text(
        "{}", encoding="utf-8"
    )

    result = enter("fe-stale-continuation")

    assert result.status == "blocked"
    assert not (
        root / ".ai-sdlc/loops/frontend-evidence/fe-stale-continuation"
    ).exists()


def test_frontend_cli_close_revalidates_normal_upstream_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_cli_loop_review import (
        _write_cli_expert_results,
        app,
        runner,
    )

    root, implementation_loop = _closed_frontend_implementation(tmp_path, monkeypatch)
    loop_id = "frontend-continued-cli"
    started = runner.invoke(
        app,
        [
            "loop",
            "frontend-evidence",
            "start",
            "--wi",
            "specs/demo-implementation-loop",
            "--implementation-loop-id",
            implementation_loop.name,
            "--loop-id",
            loop_id,
            "--json",
        ],
    )
    assert started.exit_code == 0, started.output
    review = runner.invoke(
        app,
        [
            "loop",
            "review",
            "--type",
            "frontend-evidence",
            "--loop-id",
            loop_id,
            "--json",
        ],
    )
    assert review.exit_code == 0, review.output
    payload = json.loads(review.output)
    upstream_close = (
        (implementation_loop / "implementation-close.json").relative_to(root).as_posix()
    )
    loop_dir = root / ".ai-sdlc/loops/frontend-evidence" / loop_id
    args = [
        "loop",
        "review-record",
        "--type",
        "frontend-evidence",
        "--loop-id",
        loop_id,
        "--expect-digest",
        payload["input_digest"],
        "--json",
    ]
    for path in _write_cli_expert_results(loop_dir, payload):
        args.extend(["--result", str(path)])
    recorded = runner.invoke(app, args)
    assert recorded.exit_code == 0, recorded.output
    close_args = [
        "loop",
        "frontend-evidence",
        "close",
        "--loop-id",
        loop_id,
        "--expect-review-digest",
        payload["input_digest"],
        "--yes",
        "--json",
    ]
    for _ in range(2):
        closed = runner.invoke(app, close_args)
        assert closed.exit_code == 0, closed.output
    before = (loop_dir / "frontend-evidence-close.json").read_bytes()
    upstream = root / upstream_close
    value = json.loads(upstream.read_bytes())
    value["loop_id"] = "wrong-upstream-identity"
    upstream.write_text(json.dumps(value), encoding="utf-8")
    rejected = runner.invoke(app, close_args)
    assert rejected.exit_code != 0, rejected.output
    assert (loop_dir / "frontend-evidence-close.json").read_bytes() == before


@pytest.mark.parametrize("already_closed", [False, True])
def test_frontend_close_rejects_upstream_identity_drift_after_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_closed: bool
) -> None:
    root, implementation_loop = _closed_frontend_implementation(tmp_path, monkeypatch)
    started = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            implementation_loop_id=implementation_loop.name,
            loop_id="fe-entered-continuation",
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert started.status == "ready", started.blocker
    options = FrontendEvidenceCloseOptions(
        root=root, loop_id="fe-entered-continuation", yes=True
    )
    if already_closed:
        closed = close_frontend_evidence_loop(
            options, review_input_validator=validate_review_input_for_close
        )
        assert closed.closed is True, closed.blocker
    loop_dir = root / ".ai-sdlc/loops/frontend-evidence/fe-entered-continuation"
    before = {
        path.name: path.read_bytes() for path in loop_dir.iterdir() if path.is_file()
    }
    upstream = implementation_loop / "implementation-close.json"
    changed = json.loads(upstream.read_bytes())
    changed["loop_id"] = "wrong-upstream-identity"
    upstream.write_text(json.dumps(changed), encoding="utf-8")

    result = close_frontend_evidence_loop(
        options, review_input_validator=validate_review_input_for_close
    )

    assert result.status == "blocked"
    assert result.closed is False
    assert {
        path.name: path.read_bytes() for path in loop_dir.iterdir() if path.is_file()
    } == before


@pytest.mark.parametrize("entry", ["start", "skip"])
def test_frontend_entry_rejects_retired_implementation_history(
    tmp_path: Path, entry: str
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")
    implementation_artifacts(tmp_path, "impl-frontend").close_path.with_name(
        "review-continuation.json"
    ).write_text("{}", encoding="utf-8")

    if entry == "start":
        result = start_frontend_evidence_loop(
            FrontendEvidenceStartOptions(
                root=tmp_path,
                work_item="specs/demo-frontend",
                loop_id="fe-invalid-upstream",
            )
        )
    else:
        result = skip_frontend_evidence_loop(
            FrontendEvidenceSkipOptions(
                root=tmp_path,
                work_item="specs/demo-frontend",
                loop_id="fe-invalid-upstream",
                reason="Browser provider unavailable in this isolated fixture.",
                yes=True,
            )
        )

    assert result.status == "blocked"
    assert not (
        tmp_path / ".ai-sdlc/loops/frontend-evidence/fe-invalid-upstream"
    ).exists()


@pytest.mark.parametrize("already_closed", [False, True])
def test_frontend_close_revalidates_upstream_after_entry(
    tmp_path: Path, already_closed: bool
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")
    started = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path, work_item="specs/demo-frontend", loop_id="fe-upstream-drift"
        )
    )
    assert started.status == "ready"
    options = FrontendEvidenceCloseOptions(
        root=tmp_path, loop_id="fe-upstream-drift", yes=True
    )
    if already_closed:
        assert close_frontend_evidence_loop(options).closed is True
    loop_dir = tmp_path / ".ai-sdlc/loops/frontend-evidence/fe-upstream-drift"
    before = {
        path.name: path.read_bytes() for path in loop_dir.iterdir() if path.is_file()
    }
    implementation_artifacts(tmp_path, "impl-frontend").close_path.with_name(
        "review-continuation.json"
    ).write_text("{}", encoding="utf-8")

    result = close_frontend_evidence_loop(options)

    assert result.status == "blocked"
    assert result.closed is False
    assert {
        path.name: path.read_bytes() for path in loop_dir.iterdir() if path.is_file()
    } == before


def test_browser_freshness_revalidates_implementation_close_after_entry_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")
    original_build_snapshot = frontend_evidence_loop_module._build_snapshot

    def change_upstream_before_snapshot(*args: object, **kwargs: object) -> object:
        implementation_artifacts(tmp_path, "impl-frontend").close_path.with_name(
            "review-continuation.json"
        ).write_text("{}", encoding="utf-8")
        return original_build_snapshot(*args, **kwargs)

    monkeypatch.setattr(
        frontend_evidence_loop_module,
        "_build_snapshot",
        change_upstream_before_snapshot,
    )
    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path, work_item="specs/demo-frontend", loop_id="fe-freshness-drift"
        )
    )

    assert result.status == "blocked"
    assert not (
        tmp_path / ".ai-sdlc/loops/frontend-evidence/fe-freshness-drift"
    ).exists()


def test_retired_close_binding_stays_readable_but_is_not_a_normal_close(tmp_path):
    from ai_sdlc.core.loop_review_service import read_verified_implementation_close

    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    path = implementation_artifacts(tmp_path, "impl-frontend").close_path
    payload = json.loads(path.read_bytes())
    payload["review_binding"] = {
        "review_round": 3,
        "input_digest": "a" * 64,
        "outcome_sha256": "b" * 64,
        "continuation_sha256": "c" * 64,
        "continuation_digest": "d" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    assert ImplementationClose.model_validate_json(original).review_binding is not None
    with pytest.raises(ValueError, match="implementation-continuation-retired"):
        read_verified_implementation_close(tmp_path, "impl-frontend")
    assert path.read_bytes() == original


def test_start_frontend_evidence_loop_waits_for_expert_review(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-001",
        )
    )

    assert result.status == "ready"
    assert result.loop_status == "needs_review"
    assert result.work_item_id == "demo-frontend"
    assert result.gate_run_id == "gate-run-001"
    assert result.overall_gate_status == "passed"
    assert result.warning_count == 0
    assert result.blocker_count == 0
    assert result.next_action == (
        "Run ai-sdlc loop review --type frontend-evidence --loop-id fe-001."
    )
    assert result.next_guidance.command == (
        "ai-sdlc loop review --type frontend-evidence --loop-id fe-001"
    )
    assert result.next_guidance.requires_model is True
    assert result.next_guidance.writes_artifacts is False
    assert result.next_guidance.writes_code is False
    assert result.frontend_evidence is not None
    assert result.frontend_evidence.report_path.endswith(
        ".ai-sdlc/loops/frontend-evidence/fe-001/frontend-evidence-report.json"
    )

    loop_dir = tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "fe-001"
    assert (loop_dir / "loop-run.json").is_file()
    assert (loop_dir / "frontend-evidence-input.json").is_file()
    assert (loop_dir / "frontend-evidence-snapshot.json").is_file()
    assert (loop_dir / "frontend-evidence-report.json").is_file()
    assert (loop_dir / "frontend-evidence-report.md").is_file()
    assert (tmp_path / CURRENT_FRONTEND_EVIDENCE_PATH).is_file()

    report = json.loads(
        (loop_dir / "frontend-evidence-report.json").read_text(encoding="utf-8")
    )
    assert report["artifact_kind"] == "frontend-evidence-report"
    assert report["status"] == "needs_review"
    assert report["screenshot_refs"] == [
        ".ai-sdlc/artifacts/frontend-browser-gate/gate-run-001/shared-runtime/navigation-screenshot.png"
    ]
    snapshot = json.loads(
        (loop_dir / "frontend-evidence-snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["effective_provider"] == "public-primevue"
    assert snapshot["artifact_records"][0]["artifact_ref"].startswith(
        ".ai-sdlc/artifacts/frontend-browser-gate/gate-run-001/"
    )


def test_start_frontend_evidence_loop_dry_run_does_not_write(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-dry-run",
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.overall_gate_status == "passed"
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "fe-dry-run"
    ).exists()


def test_start_frontend_evidence_loop_blocks_stale_browser_gate_artifact(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(
        tmp_path,
        work_item,
        closed_at="2026-07-01T00:00:10Z",
    )
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-stale-browser-gate",
        )
    )

    assert result.status == "blocked"
    assert "older than the closed implementation loop" in result.blocker
    assert (
        result.next_guidance.command
        == "ai-sdlc loop frontend-evidence capture --execute"
    )
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "fe-stale-browser-gate"
    ).exists()


def test_start_frontend_evidence_loop_blocks_missing_browser_gate_artifact(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-artifact",
        )
    )

    assert result.status == "blocked"
    assert "artifact is missing" in result.blocker
    assert result.next_guidance.command == "ai-sdlc loop frontend-evidence doctor"
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "fe-missing-artifact"
    ).exists()


def test_doctor_prefers_existing_browser_artifact_over_playwright(
    tmp_path: Path,
) -> None:
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")

    result = doctor_frontend_evidence_provider(
        FrontendEvidenceDoctorOptions(root=tmp_path)
    )

    assert result.status == "ready"
    assert result.browser_artifact_available is True
    assert result.recommended_provider == "external-artifact"
    assert result.next_guidance.command == (
        "ai-sdlc loop frontend-evidence start --wi specs/<work-item>"
    )
    playwright = next(
        provider
        for provider in result.providers
        if provider.provider_id == "playwright"
    )
    assert playwright.selected is False


def test_doctor_supports_explicit_codex_browser_without_playwright_push(
    tmp_path: Path,
) -> None:
    result = doctor_frontend_evidence_provider(
        FrontendEvidenceDoctorOptions(root=tmp_path, provider="codex-browser")
    )

    assert result.status == "needs_user"
    assert result.recommended_provider == "codex-browser"
    assert "Playwright" not in result.next_action
    assert result.next_guidance.command == (
        "ai-sdlc loop frontend-evidence start --wi specs/<work-item> "
        "--artifact-path <browser-gate-artifact.yaml>"
    )
    codex_provider = next(
        provider
        for provider in result.providers
        if provider.provider_id == "codex-browser"
    )
    assert codex_provider.selected is True
    assert codex_provider.install_commands == []


def test_doctor_does_not_mark_declared_playwright_ready_without_runtime(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"@playwright/test": "^1.45.0"}}),
        encoding="utf-8",
    )

    with patch("ai_sdlc.core.frontend_evidence_loop.shutil.which") as which:
        which.side_effect = lambda command: (
            f"/usr/bin/{command}"
            if command
            in {
                "node",
                "npm",
            }
            else None
        )
        result = doctor_frontend_evidence_provider(
            FrontendEvidenceDoctorOptions(root=tmp_path, provider="playwright")
        )

    assert result.status == "needs_user"
    assert result.recommended_provider == "playwright"
    assert "browser-gate-probe" not in result.next_action
    assert result.next_guidance.command == (
        "npm install -D @playwright/test pixelmatch pngjs"
    )
    playwright = next(
        provider
        for provider in result.providers
        if provider.provider_id == "playwright"
    )
    assert playwright.selected is True
    assert playwright.available is False
    assert playwright.node_available is True
    assert playwright.package_manager_available is True
    assert playwright.run_commands == []
    assert (
        "npm install -D @playwright/test pixelmatch pngjs"
        in playwright.install_commands
    )
    assert "package.json declares Playwright" in playwright.evidence


def test_doctor_does_not_mark_playwright_ready_without_visual_comparators(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"@playwright/test": "^1.45.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "node_modules" / "playwright").mkdir(parents=True)

    with patch("ai_sdlc.core.frontend_evidence_loop.shutil.which") as which:
        which.side_effect = lambda command: (
            f"/usr/bin/{command}"
            if command
            in {
                "node",
                "npm",
            }
            else None
        )
        result = doctor_frontend_evidence_provider(
            FrontendEvidenceDoctorOptions(root=tmp_path, provider="playwright")
        )

    assert result.status == "needs_user"
    playwright = next(
        provider
        for provider in result.providers
        if provider.provider_id == "playwright"
    )
    assert playwright.available is False
    assert playwright.run_commands == []


def test_doctor_marks_playwright_ready_only_when_all_runtime_packages_exist(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"@playwright/test": "^1.45.0"}}),
        encoding="utf-8",
    )
    for package_name in ("playwright", "pixelmatch", "pngjs"):
        (tmp_path / "node_modules" / package_name).mkdir(parents=True)

    with patch("ai_sdlc.core.frontend_evidence_loop.shutil.which") as which:
        which.side_effect = lambda command: (
            f"/usr/bin/{command}" if command in {"node", "npm"} else None
        )
        result = doctor_frontend_evidence_provider(
            FrontendEvidenceDoctorOptions(root=tmp_path, provider="playwright")
        )

    assert result.status == "ready"
    assert (
        result.next_guidance.command
        == "ai-sdlc loop frontend-evidence capture --execute"
    )
    playwright = next(
        provider
        for provider in result.providers
        if provider.provider_id == "playwright"
    )
    assert playwright.selected is True
    assert playwright.available is True
    assert playwright.run_commands == [
        "ai-sdlc loop frontend-evidence capture --execute"
    ]
    assert "node_modules/playwright" in playwright.evidence
    assert "node_modules/pixelmatch" in playwright.evidence
    assert "node_modules/pngjs" in playwright.evidence


@pytest.mark.parametrize(
    ("package_manager", "expected"),
    (
        (
            "npm",
            [
                "npm install -D @playwright/test pixelmatch pngjs",
                "npx playwright install chromium",
            ],
        ),
        (
            "pnpm",
            [
                "pnpm add -D @playwright/test pixelmatch pngjs",
                "pnpm exec playwright install chromium",
            ],
        ),
        (
            "yarn",
            [
                "yarn add -D @playwright/test pixelmatch pngjs",
                "yarn playwright install chromium",
            ],
        ),
    ),
)
def test_playwright_install_commands_include_visual_comparators(
    package_manager: str,
    expected: list[str],
) -> None:
    assert (
        frontend_evidence_loop_module._playwright_install_commands(
            package_manager,
            "chromium",
        )
        == expected
    )


def test_skip_frontend_evidence_loop_requires_confirmation(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)

    result = skip_frontend_evidence_loop(
        FrontendEvidenceSkipOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-skip-no-yes",
            reason="Browser control is unavailable on this machine.",
        )
    )

    assert result.status == "blocked"
    assert "requires explicit confirmation" in result.result
    assert not (
        tmp_path / ".ai-sdlc" / "loops" / "frontend-evidence" / "fe-skip-no-yes"
    ).exists()


def test_skip_frontend_evidence_loop_waits_for_review_before_close(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    reason = (
        "Company laptop cannot install browser plugins or launch controlled browsers."
    )

    result = skip_frontend_evidence_loop(
        FrontendEvidenceSkipOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-skip-browser-unavailable",
            reason=reason,
            yes=True,
            closed_by="tester",
        )
    )

    assert result.status == "ready"
    assert result.closed is False
    assert result.skipped is True
    assert result.loop_status == "needs_review"
    assert result.skip_reason == reason
    assert result.next_guidance.command == (
        "ai-sdlc loop review --type frontend-evidence "
        "--loop-id fe-skip-browser-unavailable"
    )
    loop_dir = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "frontend-evidence"
        / "fe-skip-browser-unavailable"
    )
    assert not (loop_dir / "frontend-evidence-close.json").exists()
    report_payload = json.loads(
        (loop_dir / "frontend-evidence-report.json").read_text(encoding="utf-8")
    )
    assert report_payload["status"] == "needs_review"
    assert report_payload["overall_gate_status"] == "skipped"
    assert "frontend_browser_e2e_skipped" in report_payload["advisory_reason_codes"]

    close = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=tmp_path,
            loop_id="fe-skip-browser-unavailable",
            yes=True,
            allow_warnings=True,
            closed_by="tester",
        )
    )

    assert close.closed is True
    assert close.skipped is True
    assert close.loop_status == "closed"
    assert close.next_guidance.command == "ai-sdlc pr-review start"
    close_payload = json.loads(
        (loop_dir / "frontend-evidence-close.json").read_text(encoding="utf-8")
    )
    assert close_payload["skipped"] is True
    assert close_payload["skip_reason"] == reason
    assert close_payload["closed_by"] == "tester"


def test_start_frontend_evidence_loop_blocks_scope_mismatch(tmp_path: Path) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/other-frontend")

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-scope-mismatch",
        )
    )

    assert result.status == "blocked"
    assert "belongs to specs/other-frontend" in result.blocker
    assert "demo-frontend" in result.blocker


def test_start_frontend_evidence_loop_blocks_missing_receipt_artifact_record(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        omitted_artifact_record_ids=["smoke-trace"],
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-receipt-record",
        )
    )

    assert result.status == "blocked"
    assert "references missing artifact record smoke-trace" in result.blocker


def test_start_frontend_evidence_loop_blocks_missing_receipt_artifact_file(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        missing_artifact_file_ids=["smoke-trace"],
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-receipt-file",
        )
    )

    assert result.status == "blocked"
    assert "artifact record file is missing" in result.blocker
    assert "playwright-trace.zip" in result.blocker


def test_start_frontend_evidence_loop_blocks_artifact_ref_namespace_traversal(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        artifact_ref_by_id={
            "smoke-screenshot": (
                ".ai-sdlc/artifacts/frontend-browser-gate/"
                "gate-run-001/../other/navigation-screenshot.png"
            )
        },
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-artifact-namespace-traversal",
        )
    )

    assert result.status == "blocked"
    assert "escapes the gate namespace" in result.blocker


def test_start_frontend_evidence_loop_blocks_receipt_without_evidence_artifacts(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    artifact_path = _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
    )
    payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["artifact_records"] = []
    bundle_input = payload["bundle_input"]
    assert isinstance(bundle_input, dict)
    bundle_input["screenshot_refs"] = []
    bundle_input["playwright_trace_refs"] = []
    check_receipts = bundle_input["check_receipts"]
    assert isinstance(check_receipts, list)
    for receipt in check_receipts:
        assert isinstance(receipt, dict)
        receipt["artifact_ids"] = []
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-empty-receipt-artifacts",
        )
    )

    assert result.status == "blocked"
    assert "has no evidence artifacts" in result.blocker
    assert "playwright_smoke" in result.blocker


def test_start_frontend_evidence_loop_blocks_runtime_session_scope_drift(
    tmp_path: Path,
) -> None:
    cases = (
        ("spec_dir", "specs/other-frontend"),
        ("browser_entry_ref", "managed/other/index.html"),
    )
    for field_name, stale_value in cases:
        case_root = tmp_path / field_name
        work_item = _write_work_item(case_root)
        _write_closed_implementation_loop(case_root, work_item)
        artifact_path = _write_browser_gate_artifact(
            case_root,
            work_item_path="specs/demo-frontend",
        )
        payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        runtime_session = payload["runtime_session"]
        assert isinstance(runtime_session, dict)
        runtime_session[field_name] = stale_value
        artifact_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        result = start_frontend_evidence_loop(
            FrontendEvidenceStartOptions(
                root=case_root,
                work_item="specs/demo-frontend",
                loop_id=f"fe-runtime-scope-{field_name.replace('_', '-')}",
            )
        )

        assert result.status == "blocked"
        assert (
            f"runtime session scope is inconsistent for {field_name}"
        ) in result.blocker


def test_start_frontend_evidence_loop_blocks_unsafe_gate_run_id(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    artifact_path = _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
    )
    payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["gate_run_id"] = "../other-run"
    execution_context = payload["execution_context"]
    runtime_session = payload["runtime_session"]
    bundle_input = payload["bundle_input"]
    assert isinstance(execution_context, dict)
    assert isinstance(runtime_session, dict)
    assert isinstance(bundle_input, dict)
    execution_context["gate_run_id"] = "../other-run"
    runtime_session["gate_run_id"] = "../other-run"
    bundle_input["gate_run_id"] = "../other-run"
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-unsafe-gate-run",
        )
    )

    assert result.status == "blocked"
    assert "gate_run_id is not a safe path segment" in result.blocker


def test_frontend_evidence_loop_reports_visual_regression_recheck_without_artifacts(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    artifact_path = _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        overall_gate_status="incomplete",
        probe_runtime_state="incomplete",
        runtime_session_status="incomplete",
    )
    payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["required_probe_set"].append("visual_regression")
    execution_context = payload["execution_context"]
    assert isinstance(execution_context, dict)
    execution_context["required_probe_set"].append("visual_regression")
    bundle_input = payload["bundle_input"]
    assert isinstance(bundle_input, dict)
    bundle_input["overall_gate_status"] = "incomplete"
    bundle_input["blocking_reason_codes"] = ["visual_regression_evidence_missing"]
    check_receipts = bundle_input["check_receipts"]
    assert isinstance(check_receipts, list)
    check_receipts.append(
        _receipt(
            "visual_regression",
            "evidence_missing",
            artifact_ids=[],
            blocking_reason_codes=["visual_regression_evidence_missing"],
            remediation_hints=["materialize visual regression baseline"],
        )
    )
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-visual-regression-recheck",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count == 2
    assert (
        result.next_guidance.command
        == "ai-sdlc loop frontend-evidence capture --execute"
    )
    report_path = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "frontend-evidence"
        / "fe-visual-regression-recheck"
        / "frontend-evidence-report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "materialize visual regression baseline" in report["blockers"]
    assert "visual_regression_evidence_missing" in report["blockers"]


def test_start_frontend_evidence_loop_respects_plain_language_blockers(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        plain_language_blockers=["Browser gate evidence must be refreshed."],
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-plain-blocker",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count == 1
    assert (
        result.next_guidance.command
        == "ai-sdlc loop frontend-evidence capture --execute"
    )


def test_start_frontend_evidence_loop_blocks_ready_gate_with_missing_evidence(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        artifact_capture_status_by_id={"smoke-screenshot": "missing"},
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-ready-missing-evidence",
        )
    )

    assert result.status == "blocked"
    assert "non-captured evidence artifact smoke-screenshot" in result.blocker
    assert "missing" in result.blocker


def test_frontend_evidence_loop_preserves_missing_probe_artifact_report(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        overall_gate_status="incomplete",
        smoke_classification="evidence_missing",
        blocking_reason_codes=["playwright_probe_evidence_missing"],
        remediation_hints=["materialize shared Playwright runtime evidence"],
        probe_runtime_state="incomplete",
        runtime_session_status="incomplete",
        artifact_capture_status_by_id={"smoke-screenshot": "missing"},
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-capture-record",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count == 2
    loop_dir = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "frontend-evidence"
        / "fe-missing-capture-record"
    )
    assert (loop_dir / "frontend-evidence-report.json").is_file()
    snapshot = json.loads(
        (loop_dir / "frontend-evidence-snapshot.json").read_text(encoding="utf-8")
    )
    screenshot_record = next(
        record
        for record in snapshot["artifact_records"]
        if record["artifact_id"] == "smoke-screenshot"
    )
    assert screenshot_record["capture_status"] == "missing"


def test_start_frontend_evidence_loop_blocks_failed_runtime_session(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        runtime_session_status="failed",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-failed-runtime-session",
        )
    )

    assert result.status == "blocked"
    assert "runtime session is not completed: failed" in result.blocker


def test_start_frontend_evidence_loop_blocks_failed_probe_runtime_state(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        probe_runtime_state="failed",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-failed-probe-state",
        )
    )

    assert result.status == "blocked"
    assert "probe runtime state is not completed: failed" in result.blocker


def test_start_frontend_evidence_loop_blocks_missing_probe_runtime_state(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    artifact_path = _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
    )
    payload = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
    payload.pop("probe_runtime_state")
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-probe-state",
        )
    )

    assert result.status == "blocked"
    assert "probe runtime state is not completed: <missing>" in result.blocker


def test_close_frontend_evidence_loop_requires_allow_warnings(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        overall_gate_status="passed_with_advisories",
        visual_classification="advisory_only",
        advisory_reason_codes=["low_contrast_text"],
        remediation_hints=["review generated frontend visual/accessibility warnings"],
    )
    start = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-advisory",
        )
    )

    assert start.status == "ready"
    assert start.loop_status == "needs_review"
    assert start.next_guidance.command == (
        "ai-sdlc loop review --type frontend-evidence --loop-id fe-advisory"
    )

    blocked_close = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(root=tmp_path, loop_id="fe-advisory", yes=True)
    )

    assert blocked_close.status == "needs_user"
    assert "--allow-warnings" in blocked_close.blocker

    close = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=tmp_path,
            loop_id="fe-advisory",
            yes=True,
            allow_warnings=True,
        )
    )

    assert close.status == "ready"
    assert close.closed is True
    assert close.loop_status == "closed"
    assert close.next_action == "Run ai-sdlc pr-review start."
    close_payload = json.loads(
        (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "frontend-evidence"
            / "fe-advisory"
            / "frontend-evidence-close.json"
        ).read_text(encoding="utf-8")
    )
    assert close_payload["artifact_kind"] == "frontend-evidence-close"
    assert close_payload["allow_warnings"] is True
    assert close_payload["warning_count"] == 3
    assert close_payload["accepted_warning_reason_codes"] == ["low_contrast_text"]


def test_close_frontend_evidence_loop_rechecks_review_digest_at_final_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")
    loop_id = "fe-final-review-guard"
    started = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id=loop_id,
        )
    )
    assert started.status == "ready"
    expected_digest = "a" * 64
    original_read_snapshot = frontend_evidence_loop_module.read_snapshot

    def mutate_after_state_validation(*args: object, **kwargs: object) -> object:
        snapshot = original_read_snapshot(*args, **kwargs)
        report_path = (
            tmp_path
            / ".ai-sdlc"
            / "loops"
            / "frontend-evidence"
            / loop_id
            / "frontend-evidence-report.md"
        )
        report_path.write_text(
            report_path.read_text(encoding="utf-8") + "\n评审后发生变化。\n",
            encoding="utf-8",
        )
        return snapshot

    monkeypatch.setattr(
        frontend_evidence_loop_module,
        "read_snapshot",
        mutate_after_state_validation,
    )

    def reject_changed_report(
        root: Path,
        *,
        loop_type: str,
        loop_id: str,
        expected_digest: str,
    ) -> None:
        assert root == tmp_path
        assert loop_type == "frontend-evidence"
        assert loop_id == "fe-final-review-guard"
        assert expected_digest == "a" * 64
        report_path = (
            root
            / ".ai-sdlc"
            / "loops"
            / "frontend-evidence"
            / loop_id
            / "frontend-evidence-report.md"
        )
        assert "评审后发生变化" in report_path.read_text(encoding="utf-8")
        raise ReviewInputGuardError("review-input-drift")

    with pytest.raises(ReviewInputGuardError, match="review-input-drift"):
        close_frontend_evidence_loop(
            FrontendEvidenceCloseOptions(
                root=tmp_path,
                loop_id=loop_id,
                yes=True,
                expected_review_digest=expected_digest,
            ),
            review_input_validator=reject_changed_report,
        )

    assert not (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "frontend-evidence"
        / loop_id
        / "frontend-evidence-close.json"
    ).exists()


def test_close_frontend_evidence_loop_rejects_replaced_loop_identity(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(tmp_path, work_item_path="specs/demo-frontend")
    for loop_id in ("fe-reviewed", "fe-unreviewed"):
        start_frontend_evidence_loop(
            FrontendEvidenceStartOptions(
                root=tmp_path,
                work_item="specs/demo-frontend",
                loop_id=loop_id,
            )
        )
    reviewed_run = (
        tmp_path
        / ".ai-sdlc"
        / "loops"
        / "frontend-evidence"
        / "fe-reviewed"
        / "loop-run.json"
    )
    unreviewed_run = reviewed_run.parent.parent / "fe-unreviewed" / "loop-run.json"
    reviewed_run.write_bytes(unreviewed_run.read_bytes())

    result = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(root=tmp_path, loop_id="fe-reviewed", yes=True)
    )

    assert result.status == "blocked"
    assert "identity" in result.blocker.lower()
    assert not (unreviewed_run.parent / "frontend-evidence-close.json").exists()


def test_frontend_evidence_loop_needs_fix_for_missing_evidence(
    tmp_path: Path,
) -> None:
    work_item = _write_work_item(tmp_path)
    _write_closed_implementation_loop(tmp_path, work_item)
    _write_browser_gate_artifact(
        tmp_path,
        work_item_path="specs/demo-frontend",
        overall_gate_status="incomplete",
        smoke_classification="evidence_missing",
        blocking_reason_codes=["playwright_probe_evidence_missing"],
        remediation_hints=["materialize shared Playwright runtime evidence"],
        probe_runtime_state="incomplete",
        runtime_session_status="incomplete",
    )

    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=tmp_path,
            work_item="specs/demo-frontend",
            loop_id="fe-missing-evidence",
        )
    )

    assert result.status == "needs_fix"
    assert result.loop_status == "needs_fix"
    assert result.blocker_count == 2
    assert (
        result.next_guidance.command
        == "ai-sdlc loop frontend-evidence capture --execute"
    )

    close = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=tmp_path,
            loop_id="fe-missing-evidence",
            yes=True,
        )
    )

    assert close.status == "needs_fix"
    assert "shared Playwright" in close.blocker


def _write_work_item(tmp_path: Path) -> Path:
    work_item = tmp_path / "specs" / "demo-frontend"
    work_item.mkdir(parents=True)
    (work_item / "spec.md").write_text("# Frontend Demo\n", encoding="utf-8")
    (work_item / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (work_item / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    return work_item


def _write_closed_implementation_loop(
    tmp_path: Path,
    work_item: Path,
    *,
    closed_at: str = "2026-06-30T23:59:59Z",
) -> None:
    artifacts = implementation_artifacts(tmp_path, "impl-frontend")
    store = LoopArtifactStore(tmp_path)
    store.create_loop_run_dir("impl-frontend", loop_type=LoopType.IMPLEMENTATION.value)
    report = ImplementationReport(
        loop_id="impl-frontend",
        work_item_id=work_item.name,
        work_item_path=f"specs/{work_item.name}",
        status=LoopStatus.PASSED,
        required_task_count=1,
        done_count=1,
        requires_frontend_evidence=True,
        next_action=(
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item.name}."
        ),
    )
    loop_run = LoopRun(
        loop_id="impl-frontend",
        loop_type=LoopType.IMPLEMENTATION,
        status=LoopStatus.CLOSED,
        work_item_id=work_item.name,
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
            f"Run ai-sdlc loop frontend-evidence start --wi specs/{work_item.name}."
        ),
    )
    store.write_json_artifact(artifacts.report_json_path, report)
    store.write_json_artifact(artifacts.loop_run_path, loop_run)
    store.write_json_artifact(
        artifacts.close_path,
        ImplementationClose(
            loop_id="impl-frontend",
            closed_at=closed_at,
            report_path=f"specs/{work_item.name}/implementation-report.json",
            next_loop_type=LoopType.FRONTEND_EVIDENCE,
        ),
    )
    store.write_json_artifact(
        artifacts.pointer_path,
        ImplementationCurrentPointer(
            loop_id="impl-frontend",
            loop_run_path=(".ai-sdlc/loops/implementation/impl-frontend/loop-run.json"),
        ),
    )


def _write_browser_gate_artifact(
    tmp_path: Path,
    *,
    work_item_path: str,
    overall_gate_status: str = "passed",
    smoke_classification: str = "pass",
    visual_classification: str = "pass",
    a11y_classification: str = "pass",
    interaction_classification: str = "pass",
    blocking_reason_codes: list[str] | None = None,
    advisory_reason_codes: list[str] | None = None,
    remediation_hints: list[str] | None = None,
    omitted_artifact_record_ids: list[str] | None = None,
    missing_artifact_file_ids: list[str] | None = None,
    artifact_capture_status_by_id: dict[str, str] | None = None,
    artifact_ref_by_id: dict[str, str] | None = None,
    probe_runtime_state: str = "completed",
    runtime_session_status: str = "completed",
    plain_language_blockers: list[str] | None = None,
) -> Path:
    gate_run_id = "gate-run-001"
    artifact_root = f".ai-sdlc/artifacts/frontend-browser-gate/{gate_run_id}"
    screenshot_ref = f"{artifact_root}/shared-runtime/navigation-screenshot.png"
    trace_ref = f"{artifact_root}/shared-runtime/playwright-trace.zip"
    interaction_ref = f"{artifact_root}/interaction/interaction-snapshot.json"
    source_artifact_ref = ".ai-sdlc/memory/frontend-managed-delivery-apply/latest.yaml"
    required_probe_set = [
        "playwright_smoke",
        "visual_expectation",
        "basic_a11y",
        "interaction_anti_pattern_checks",
    ]
    artifact_records = [
        {
            "artifact_id": "smoke-screenshot",
            "gate_run_id": gate_run_id,
            "check_name": "playwright_smoke",
            "artifact_type": "navigation_screenshot",
            "artifact_ref": screenshot_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
        {
            "artifact_id": "smoke-trace",
            "gate_run_id": gate_run_id,
            "check_name": "playwright_smoke",
            "artifact_type": "playwright_trace",
            "artifact_ref": trace_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
        {
            "artifact_id": "interaction-snapshot",
            "gate_run_id": gate_run_id,
            "check_name": "interaction_anti_pattern_checks",
            "artifact_type": "interaction_snapshot",
            "artifact_ref": interaction_ref,
            "capture_status": "captured",
            "captured_at": "2026-07-01T00:00:01Z",
        },
    ]
    omitted_record_ids = set(omitted_artifact_record_ids or [])
    artifact_records = [
        record
        for record in artifact_records
        if str(record["artifact_id"]) not in omitted_record_ids
    ]
    missing_file_ids = set(missing_artifact_file_ids or [])
    capture_status_by_id = artifact_capture_status_by_id or {}
    artifact_refs = artifact_ref_by_id or {}
    for record in artifact_records:
        artifact_ref = artifact_refs.get(str(record["artifact_id"]))
        if artifact_ref:
            record["artifact_ref"] = artifact_ref
        capture_status = capture_status_by_id.get(str(record["artifact_id"]))
        if capture_status:
            record["capture_status"] = capture_status
        if str(record["artifact_id"]) in missing_file_ids:
            continue
        if record["capture_status"] != "captured":
            continue
        local_artifact_path = tmp_path / str(record["artifact_ref"])
        local_artifact_path.parent.mkdir(parents=True, exist_ok=True)
        local_artifact_path.write_text("frontend evidence artifact\n", encoding="utf-8")

    payload = {
        "generated_at": "2026-07-01T00:00:00Z",
        "apply_artifact_path": source_artifact_ref,
        "probe_runtime_state": probe_runtime_state,
        "gate_run_id": gate_run_id,
        "artifact_root": artifact_root,
        "required_probe_set": required_probe_set,
        "execution_context": {
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-001",
            "solution_snapshot_id": "solution-snapshot-001",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-001",
            "effective_provider": "public-primevue",
            "effective_style_pack": "modern-saas",
            "style_fidelity_status": "verified",
            "delivery_entry_id": "vue3-public-primevue",
            "package_manager": "npm",
            "component_library_packages": ["primevue", "@primeuix/themes"],
            "required_probe_set": required_probe_set,
            "browser_entry_ref": "managed/frontend/index.html",
            "source_linkage_refs": {"apply_result_status": "ok"},
        },
        "runtime_session": {
            "probe_runtime_session_id": "session-001",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-001",
            "solution_snapshot_id": "solution-snapshot-001",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "readiness_subject_id": "subject-001",
            "browser_entry_ref": "managed/frontend/index.html",
            "artifact_root_ref": artifact_root,
            "status": runtime_session_status,
            "started_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:00:01Z",
            "finished_at": "2026-07-01T00:00:01Z",
        },
        "artifact_records": artifact_records,
        "bundle_input": {
            "bundle_id": "bundle-001",
            "gate_run_id": gate_run_id,
            "apply_result_id": "apply-result-001",
            "solution_snapshot_id": "solution-snapshot-001",
            "spec_dir": work_item_path,
            "attachment_scope_ref": "scope:frontend",
            "managed_frontend_target": "managed/frontend",
            "source_artifact_ref": source_artifact_ref,
            "readiness_subject_id": "subject-001",
            "playwright_trace_refs": [trace_ref],
            "screenshot_refs": [screenshot_ref],
            "check_receipts": [
                _receipt(
                    "playwright_smoke",
                    smoke_classification,
                    artifact_ids=["smoke-screenshot", "smoke-trace"],
                    blocking_reason_codes=blocking_reason_codes,
                    remediation_hints=remediation_hints,
                ),
                _receipt(
                    "visual_expectation",
                    visual_classification,
                    artifact_ids=["smoke-screenshot"],
                    advisory_reason_codes=advisory_reason_codes,
                    remediation_hints=remediation_hints,
                ),
                _receipt(
                    "basic_a11y",
                    a11y_classification,
                    artifact_ids=["smoke-screenshot"],
                    advisory_reason_codes=advisory_reason_codes,
                    remediation_hints=remediation_hints,
                ),
                _receipt(
                    "interaction_anti_pattern_checks",
                    interaction_classification,
                    artifact_ids=["interaction-snapshot"],
                    blocking_reason_codes=blocking_reason_codes,
                    remediation_hints=remediation_hints,
                ),
            ],
            "smoke_verdict": smoke_classification,
            "visual_verdict": visual_classification,
            "a11y_verdict": a11y_classification,
            "interaction_anti_pattern_verdict": interaction_classification,
            "overall_gate_status": overall_gate_status,
            "blocking_reason_codes": blocking_reason_codes or [],
            "advisory_reason_codes": advisory_reason_codes or [],
            "generated_at": "2026-07-01T00:00:01Z",
        },
        "overall_gate_status": overall_gate_status,
        "warnings": ["visual advisory warning"] if advisory_reason_codes else [],
        "plain_language_blockers": plain_language_blockers or [],
        "recommended_next_steps": [],
    }
    artifact_path = (
        tmp_path
        / ".ai-sdlc"
        / "memory"
        / "frontend-delivery"
        / "browser"
        / "latest.yaml"
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return artifact_path


def _receipt(
    check_name: str,
    classification: str,
    *,
    artifact_ids: list[str],
    blocking_reason_codes: list[str] | None = None,
    advisory_reason_codes: list[str] | None = None,
    remediation_hints: list[str] | None = None,
) -> dict[str, object]:
    runtime_status = (
        "incomplete"
        if classification == "evidence_missing"
        else "failed_transient"
        if classification == "transient_run_failure"
        else "completed"
    )
    return {
        "check_name": check_name,
        "started_at": "2026-07-01T00:00:00Z",
        "finished_at": "2026-07-01T00:00:01Z",
        "runtime_status": runtime_status,
        "artifact_ids": artifact_ids,
        "classification_candidate": classification,
        "recheck_required": classification
        in {"evidence_missing", "transient_run_failure"},
        "remediation_hints": remediation_hints or [],
        "blocking_reason_codes": (
            blocking_reason_codes
            if classification
            in {"evidence_missing", "transient_run_failure", "actual_quality_blocker"}
            else []
        )
        or [],
        "advisory_reason_codes": (
            advisory_reason_codes if classification == "advisory_only" else []
        )
        or [],
        "requirement_linkage": [f"browser_quality_gate:{check_name}"],
    }

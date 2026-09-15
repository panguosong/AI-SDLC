"""Frontend 原浏览器工件协议与 stage 模拟、实际评审、Close 的接缝。"""

import hashlib
import json

import pytest
import yaml

from ai_sdlc.cli.loop_review_cmd import (
    ReviewInputGuardError,
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.cli.loop_stage_cmd import (
    resolve_stage_decision_host,
    stage_review_snapshot,
)
from ai_sdlc.core.frontend_evidence_loop import (
    FrontendEvidenceCloseOptions,
    FrontendEvidenceStartOptions,
    close_frontend_evidence_loop,
    start_frontend_evidence_loop,
)
from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest
from ai_sdlc.core.loop_stage_decision_service import prepare_stage_simulation_decision
from tests.unit.test_frontend_evidence_loop import (
    _closed_frontend_implementation,
    _write_browser_gate_artifact,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data


def frontend_project(tmp_path, monkeypatch, *, failed=False):
    from ai_sdlc.core.loop_models import utc_now_iso

    root, implementation = _closed_frontend_implementation(tmp_path, monkeypatch)
    path = _write_browser_gate_artifact(
        root,
        work_item_path="specs/demo-implementation-loop",
        overall_gate_status="blocked" if failed else "passed",
        smoke_classification="actual_quality_blocker" if failed else "pass",
        blocking_reason_codes=["smoke_failed"] if failed else None,
    )
    artifact = yaml.safe_load(path.read_text())
    artifact["generated_at"] = utc_now_iso()
    path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
    result = start_frontend_evidence_loop(
        FrontendEvidenceStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="stage-browser",
            implementation_loop_id=implementation.name,
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert result.status == ("needs_fix" if failed else "ready"), result.blocker
    return root


def prepare(root, payload):
    request = SimulationPrepareRequest.model_validate(payload)
    kwargs = dict(
        host_resolver=lambda: resolve_stage_decision_host(
            root, "frontend-evidence", "stage-browser"
        )
    )
    preview = prepare_stage_simulation_decision(
        root, "frontend-evidence", "stage-browser", request, **kwargs
    )
    return prepare_stage_simulation_decision(
        root,
        "frontend-evidence",
        "stage-browser",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
        **kwargs,
    ).context


def select(root):
    path = root / "specs/demo-implementation-loop/spec.md"
    data = contract_data()
    data.update(
        capability="stage-simulation-v1",
        loop_type="frontend-evidence",
        profile_id="frontend-evidence-v1",
    )
    context = prepare(
        root,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": [data],
            "sources": [
                {
                    "id": "spec",
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "locator": "all",
                    "claim": "原前端范围",
                }
            ],
        },
    )
    context = prepare(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [candidate_data(context.plan)],
        },
    )
    return prepare(
        root,
        {
            "operation": "record-comparison",
            "request_id": "judge",
            "judgement": {
                "judge_input_digest": context.pending_batch.judge_input_digest,
                "assessments": [assessment_data(lower=4, upper=4)],
            },
        },
    )


def record_actual(root, *, status="PASS", round_number=1):
    from ai_sdlc.core.loop_review_models import B1ExpertResult
    from ai_sdlc.core.loop_review_service import (
        RecordLoopReviewOptions,
        record_loop_review,
    )
    from ai_sdlc.core.review_kernel import ReviewExecution
    from tests.unit.test_loop_stage_decision_service import assessments

    snap = stage_review_snapshot(
        root, "frontend-evidence", "stage-browser", round_number
    )
    template = next(iter(assessments(snap, status).values()))
    directory = root / ".ai-sdlc/loops/frontend-evidence/stage-browser"
    paths = []
    for n, role in enumerate(snap.review_input.expert_roles):
        path = directory / f"expert-{round_number}-{n}.json"
        result = B1ExpertResult(
            execution=ReviewExecution(
                status="completed",
                roles=[role],
                role_reasons={role: snap.review_input.expert_reasons[role]},
            ),
            assessment=template,
        )
        path.write_text(result.model_dump_json(), encoding="utf-8")
        paths.append(path)
    result = record_loop_review(
        RecordLoopReviewOptions(
            root=root,
            loop_type="frontend-evidence",
            loop_id="stage-browser",
            expected_digest=snap.review_input.input_digest,
            result_paths=tuple(paths),
        ),
        loop_dir=directory,
        input_resolver=lambda number: resolve_review_input(
            root,
            loop_type="frontend-evidence",
            loop_id="stage-browser",
            review_round_number=number,
        ),
        b1_snapshot_resolver=lambda number: stage_review_snapshot(
            root, "frontend-evidence", "stage-browser", number
        ),
    )
    return snap, result


def test_browser_artifact_stage_preparation_actual_review_and_close(
    tmp_path, monkeypatch
):
    root = frontend_project(tmp_path, monkeypatch)
    context = select(root)
    assert context.selection.scores["A"].s_low == 100
    context = prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    snap, recorded = record_actual(root)
    assert recorded.status == "passed", recorded
    outcome = json.loads(
        (
            root
            / ".ai-sdlc/loops/frontend-evidence/stage-browser/review-outcome-round-1.json"
        ).read_text()
    )
    assert outcome["simulation"]["evaluation"]["h"] == 0
    result = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=root,
            loop_id="stage-browser",
            expected_review_digest=snap.review_input.input_digest,
            yes=True,
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert result.closed, result
    assert context.phase == "review_sealed"


def test_browser_failure_cannot_be_overridden_by_perfect_forecast(
    tmp_path, monkeypatch
):
    root = frontend_project(tmp_path, monkeypatch, failed=True)
    context = select(root)
    assert context.selection.scores["A"].s_low == 100
    with pytest.raises(ValueError, match="actual-tasks-not-ready"):
        prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    result = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(root=root, loop_id="stage-browser", yes=True),
        review_input_validator=validate_review_input_for_close,
    )
    assert not result.closed
    assert result.status == "blocked"
    assert result.blocker == "quantified-stage-close-review-required"


@pytest.mark.parametrize("changed_before", ["seal", "review", "close"])
def test_current_blocked_browser_cannot_reuse_passed_snapshot_without_refresh(
    tmp_path, monkeypatch, changed_before
):
    root = frontend_project(tmp_path, monkeypatch)
    context = select(root)
    assert context.selection.scores["A"].s_low == 100
    if changed_before != "seal":
        prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    digest = ""
    if changed_before == "close":
        snap, recorded = record_actual(root)
        assert recorded.status == "passed"
        digest = snap.review_input.input_digest

    directory = root / ".ai-sdlc/loops/frontend-evidence/stage-browser"
    context_path = directory / "decision-context.json"
    outcome_path = directory / "review-outcome-round-1.json"
    original_context = context_path.read_bytes()
    original_outcome = outcome_path.read_bytes() if outcome_path.exists() else None
    source = (
        root
        / json.loads((directory / "frontend-evidence-input.json").read_text())[
            "source_artifact_path"
        ]
    )
    original_generated_at = yaml.safe_load(source.read_text())["generated_at"]
    assert (
        _write_browser_gate_artifact(
            root,
            work_item_path="specs/demo-implementation-loop",
            overall_gate_status="blocked",
            smoke_classification="actual_quality_blocker",
            blocking_reason_codes=["smoke_failed"],
        )
        == source
    )
    artifact = yaml.safe_load(source.read_text())
    artifact["generated_at"] = original_generated_at
    source.write_text(yaml.safe_dump(artifact), encoding="utf-8")

    from ai_sdlc.core.frontend_evidence_loop import _build_snapshot
    from ai_sdlc.core.frontend_evidence_models import (
        FrontendEvidenceInput,
        FrontendEvidenceSnapshot,
    )

    current = _build_snapshot(
        root,
        FrontendEvidenceInput.model_validate_json(
            (directory / "frontend-evidence-input.json").read_bytes()
        ),
        source,
        review_input_validator=validate_review_input_for_close,
    )
    assert isinstance(current, FrontendEvidenceSnapshot)
    assert current.overall_gate_status == "blocked"

    # 不刷新原生 snapshot/report，当前 browser FAIL 不能由旧通过结果或 S100 覆盖。
    if changed_before == "seal":
        with pytest.raises(ValueError, match="browser|source|snapshot|actual"):
            prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    if changed_before != "close":
        with pytest.raises(ValueError):
            record_actual(root)
    try:
        result = close_frontend_evidence_loop(
            FrontendEvidenceCloseOptions(
                root=root,
                loop_id="stage-browser",
                expected_review_digest=digest,
                yes=True,
            ),
            review_input_validator=validate_review_input_for_close,
        )
    except ReviewInputGuardError:
        pass
    else:
        assert not result.closed
    assert not (directory / "frontend-evidence-close.json").exists()
    assert context_path.read_bytes() == original_context
    assert (
        outcome_path.read_bytes() if outcome_path.exists() else None
    ) == original_outcome


def test_frontend_native_r1_repair_refreshes_same_id_and_r2_can_close(
    tmp_path, monkeypatch
):
    from ai_sdlc.core.loop_models import utc_now_iso

    root = frontend_project(tmp_path, monkeypatch)
    select(root)
    prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    _, first = record_actual(root, status="FAIL")
    assert first.status == "needs_fix"
    directory = root / ".ai-sdlc/loops/frontend-evidence/stage-browser"
    original_context = (directory / "decision-context.json").read_bytes()
    original_r1 = (directory / "review-outcome-round-1.json").read_bytes()
    original_run = json.loads((directory / "loop-run.json").read_text())
    path = _write_browser_gate_artifact(
        root,
        work_item_path="specs/demo-implementation-loop",
        remediation_hints=["补齐实际反例与读取证据说明"],
    )
    artifact = yaml.safe_load(path.read_text())
    artifact["generated_at"] = utc_now_iso()
    path.write_text(yaml.safe_dump(artifact), encoding="utf-8")
    options = FrontendEvidenceStartOptions(
        root=root,
        work_item="specs/demo-implementation-loop",
        loop_id="stage-browser",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    from tests.integration.test_quantified_implementation import _cli, _payload
    from tests.integration.test_stage_quantified_pipeline import (
        _q004_assert_guidance,
        _q004_refresh_args,
    )

    refreshed = _payload(_cli(
        root, *_q004_refresh_args("frontend-evidence", loop_id="stage-browser"), "--json"
    ))
    assert refreshed["status"] == "ready", refreshed
    _q004_assert_guidance(root, "frontend-evidence", refreshed, monkeypatch, expected="loop review")
    run = json.loads((directory / "loop-run.json").read_text())
    assert run["current_round"] == 2
    assert run["created_at"] == original_run["created_at"]
    assert (directory / "decision-context.json").read_bytes() == original_context
    assert (directory / "review-outcome-round-1.json").read_bytes() == original_r1
    snap, second = record_actual(root, round_number=2)
    assert second.status == "passed", second
    assert (
        start_frontend_evidence_loop(
            options, review_input_validator=validate_review_input_for_close
        ).status
        == "blocked"
    )
    closed = close_frontend_evidence_loop(
        FrontendEvidenceCloseOptions(
            root=root,
            loop_id="stage-browser",
            expected_review_digest=snap.review_input.input_digest,
            yes=True,
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert closed.closed, closed.blocker
    assert not (directory / "review-outcome-round-3.json").exists()



def test_q004_frontend_refresh_preserves_selected_guidance(tmp_path, monkeypatch):
    from tests.integration.test_quantified_implementation import _cli, _payload
    from tests.integration.test_stage_quantified_pipeline import (
        _q004_assert_guidance,
        _q004_loop_bytes,
        _q004_refresh_args,
        _q004_reject_corrupt_guidance_after_saved_result,
    )

    root = frontend_project(tmp_path, monkeypatch)
    stage = "frontend-evidence"
    directory = root / ".ai-sdlc/loops" / stage / "stage-browser"
    before = _q004_loop_bytes(directory)
    preview_args = _q004_refresh_args(stage, loop_id="q004-first-preview")
    preview = _payload(_cli(root, *preview_args, "--dry-run", "--json"))
    assert preview["dry_run"] and "operation=begin" in preview["next_action"]
    assert not (directory.parent / "q004-first-preview").exists()
    assert _q004_loop_bytes(directory) == before
    args = _q004_refresh_args(stage, loop_id="stage-browser")
    first = _payload(_cli(root, *args, "--json"))
    assert "operation=begin" in first["next_action"]
    select(root)
    context = (directory / "decision-context.json").read_bytes()
    old_run = json.loads((directory / "loop-run.json").read_bytes())
    refreshed = _payload(_cli(root, *args, "--json"))
    _q004_assert_guidance(root, stage, refreshed, monkeypatch, expected="seal-for-review")
    before = _q004_loop_bytes(directory)
    preview = _payload(_cli(root, *args, "--dry-run", "--json"))
    _q004_assert_guidance(root, stage, preview, monkeypatch, expected="seal-for-review")
    assert preview["dry_run"] and _q004_loop_bytes(directory) == before
    run = json.loads((directory / "loop-run.json").read_bytes())
    assert (directory / "decision-context.json").read_bytes() == context
    for key in ("loop_id", "current_round", "created_at", "decision_started_at_ms"):
        assert run[key] == old_run[key]
    _q004_reject_corrupt_guidance_after_saved_result(root, stage, refreshed)

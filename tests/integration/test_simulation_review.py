"""D1 正式评审复用实际 H/Q，但不得冒充模拟分或旧 B1 原件。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_sdlc.core.implementation_loop import start_implementation_loop
from ai_sdlc.core.implementation_models import ImplementationStartOptions
from ai_sdlc.core.loop_decision_models import DecisionPrepareInput
from ai_sdlc.core.loop_decision_service import (
    B1ReviewSnapshot,
    build_b1_review_data,
)
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.loop_review_service import (
    LoopReviewServiceError,
    _overlay_for_outcome,
    _snapshot_for_review,
    _validate_saved_b1,
)
from tests.integration.test_quantified_implementation import _ready_project, _request
from tests.unit.test_loop_decision_service import _assessment, _assessment_snapshot


@pytest.fixture
def actual_review(tmp_path):
    """这里只隔离测试正式评审外壳，完整 D1 context 由服务集成验证。"""
    root = _ready_project(tmp_path)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-unit",
            decision_mode="adaptive-quantified",
        )
    )
    assert result.status == "ready", result
    request = DecisionPrepareInput.model_validate_json(
        _request(root, root / "candidate.json").read_bytes()
    )
    snapshot = _assessment_snapshot((root, request))
    return root, snapshot


def _actual_data(snapshot, *, status="PASS", readiness="PASS"):
    return build_b1_review_data(
        snapshot,
        {
            role: _assessment(snapshot, status=status, readiness=readiness)
            for role in snapshot.review_input.expert_roles
        },
        has_actionable_findings=False,
    )


def _outcome(snapshot, data, *, field="simulation"):
    return LoopReviewOutcome.model_validate(
        {
            "loop_id": snapshot.review_input.loop_id,
            "loop_type": "implementation",
            "round_number": snapshot.review_input.round_number,
            "input_digest": snapshot.review_input.input_digest,
            "status": "completed",
            "expert_roles": snapshot.review_input.expert_roles,
            "recorded_at": "2026-09-07T00:00:00Z",
            "infra_retry_count": 0,
            field: data.model_dump(),
        }
    )


def _simulation_snapshot(snapshot):
    return B1ReviewSnapshot(
        snapshot.review_input,
        SimpleNamespace(
            capability="implementation-simulation-v1",
            loop_id=snapshot.context.loop_id,
            context_digest=snapshot.context.context_digest,
            sources=snapshot.context.sources,
            selection=snapshot.context.selection,
            goal_contract=snapshot.context.route_contract.goal_contract,
        ),
        snapshot.manifest,
    )


def test_simulation_actual_outcome_has_distinct_outer_field(actual_review):
    _, snapshot = actual_review
    data = _actual_data(snapshot)
    outcome = _outcome(snapshot, data)
    assert outcome.simulation == data
    assert outcome.b1 is None
    assert "b1" not in outcome.model_dump()
    assert outcome.simulation.evaluation.h == 0
    assert outcome.simulation.decision.action == "stop"
    assert "simulation" not in _outcome(snapshot, data, field="b1").model_dump()


def test_simulation_and_b1_actual_assessments_are_mutually_exclusive(actual_review):
    _, snapshot = actual_review
    data = _actual_data(snapshot)
    payload = _outcome(snapshot, data, field="b1").model_dump()
    payload["simulation"] = data.model_dump()
    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


@pytest.mark.parametrize(
    "status,readiness,expected",
    [
        ("FAIL", "PASS", "needs_fix"),
        ("UNKNOWN", "UNKNOWN", "needs_user"),
        ("PASS", "PASS", "passed"),
    ],
)
def test_simulation_overlay_uses_actual_requirements_not_findings_alone(
    actual_review, status, readiness, expected
):
    _, snapshot = actual_review
    outcome = _outcome(
        snapshot, _actual_data(snapshot, status=status, readiness=readiness)
    )
    assert not outcome.findings
    assert _overlay_for_outcome(outcome).status == expected


def test_simulation_snapshot_joins_matching_persisted_capability(actual_review):
    root, snapshot = actual_review
    loop_dir = root / ".ai-sdlc/loops/implementation/impl-unit"
    for name in ("loop-run.json", "implementation-input.json"):
        path = loop_dir / name
        payload = json.loads(path.read_bytes())
        payload["decision_capability"] = "implementation-simulation-v1"
        path.write_text(json.dumps(payload), encoding="utf-8")
    simulation = _simulation_snapshot(snapshot)
    assert (
        _snapshot_for_review(
            root, loop_dir, snapshot.review_input, lambda _: simulation
        )
        is simulation
    )
    with pytest.raises(LoopReviewServiceError):
        _snapshot_for_review(root, loop_dir, snapshot.review_input, lambda _: snapshot)


def test_simulation_snapshot_rejects_b1_outer_assessment(actual_review):
    _, snapshot = actual_review
    outcome = _outcome(snapshot, _actual_data(snapshot), field="b1")
    with pytest.raises(LoopReviewServiceError, match="decision-assessment-invalid"):
        _validate_saved_b1(_simulation_snapshot(snapshot), outcome)


def test_simulation_failed_outcome_cannot_carry_actual_assessment(actual_review):
    _, snapshot = actual_review
    payload = _outcome(snapshot, _actual_data(snapshot)).model_dump()
    payload.update(status="failed", failure_kind="timeout", failure_reason="timeout")
    with pytest.raises(ValidationError):
        LoopReviewOutcome.model_validate(payload)


def test_d1_close_reader_requires_actual_validator(actual_review):
    from ai_sdlc.core.implementation_models import ImplementationClose
    from ai_sdlc.core.loop_review_service import read_verified_implementation_close

    root, snapshot = actual_review
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    path = loop / "loop-run.json"
    run = json.loads(path.read_bytes())
    run.update(status="closed", decision_capability="implementation-simulation-v1")
    path.write_text(json.dumps(run), encoding="utf-8")
    report_path = loop / "implementation-report.json"
    report = json.loads(report_path.read_bytes())
    close = ImplementationClose(
        loop_id="impl-unit",
        report_path=report_path.relative_to(root).as_posix(),
        required_task_count=report["required_task_count"],
    )
    (loop / "implementation-close.json").write_text(
        close.model_dump_json(), encoding="utf-8"
    )
    outcome = _outcome(snapshot, _actual_data(snapshot))
    (loop / "review-outcome-round-1.json").write_text(
        outcome.model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="simulation-close-review-validator-required"):
        read_verified_implementation_close(root, "impl-unit")


def _simulation_ready(root, *, seal=True):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _complete_task,
        _payload,
        _review_args,
    )
    from tests.integration.test_simulation_quantified_loop import (
        LOOP_ID,
        begin_request,
        prepare_request,
        start_simulation,
    )
    from tests.unit.test_loop_simulation import assessment_data, candidate_data

    _ready_project(root, extra_spec="Security permission boundary is required.")
    assert start_simulation(root).status == "ready"
    initial = prepare_request(root, begin_request(root))
    candidate = candidate_data(initial.context.plan)
    candidate["changed_scope"] = ["src/ai_sdlc/core/implementation_loop.py"]
    frozen = prepare_request(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-1",
            "candidates": [candidate],
        },
    )
    prepare_request(
        root,
        {
            "operation": "record-comparison",
            "request_id": "judge-1",
            "judgement": {
                "judge_input_digest": frozen.context.pending_batch.judge_input_digest,
                "assessments": [assessment_data()],
            },
        },
    )
    _complete_task(root, loop_id=LOOP_ID)
    if seal:
        prepare_request(root, {"operation": "seal-for-review", "request_id": "seal-1"})
    loop = root / ".ai-sdlc/loops/implementation" / LOOP_ID
    reviewed = _cli(root, *_review_args(LOOP_ID))
    return loop, _payload(reviewed) if seal else reviewed


def _simulation_envelopes(
    root, reviewed, *, status="PASS", readiness="PASS", failed=False
):
    import hashlib

    destination = root / ".ai-sdlc/state/simulation-test-experts"
    destination.mkdir(parents=True, exist_ok=True)
    source = "src/ai_sdlc/core/implementation_loop.py"
    results = []
    for index, role in enumerate(reviewed["expert_roles"]):
        execution = {
            "status": "failed" if failed else "completed",
            "roles": [role],
            "role_reasons": {role: reviewed["expert_reasons"][role]},
            "findings": [],
        }
        if failed:
            execution.update(failure_kind="timeout", failure_reason="独立专家调用超时")
        assessment = (
            None
            if failed
            else {
                "input_digest": reviewed["input_digest"],
                "context_digest": reviewed["context_digest"],
                "selected_route_id": reviewed["selected_route_id"],
                "results": [
                    {
                        "id": "o0",
                        "status": status,
                        "evidence_refs": [] if status == "UNKNOWN" else ["actual"],
                        "reason": "独立核对冻结义务与当前源码",
                    }
                ],
                "evidence": [
                    {
                        "id": "actual",
                        "path": source,
                        "sha256": hashlib.sha256(
                            (root / source).read_bytes()
                        ).hexdigest(),
                        "locator": "VALUE",
                        "claim": "当前实际实施产物",
                    }
                ],
                "repair_readiness": {
                    "authorization": readiness,
                    "facts": "PASS",
                    "verification": "PASS",
                    "evidence_refs": ["actual"],
                    "reason": "冻结范围内允许唯一必要修复",
                },
            }
        )
        path = destination / f"expert-{index}.json"
        path.write_text(
            json.dumps({"execution": execution, "assessment": assessment}),
            encoding="utf-8",
        )
        results.append(path)
    return results


def test_real_d1_review_requires_seal_without_consuming_formal_round(
    initialized_project_dir,
):
    loop, result = _simulation_ready(initialized_project_dir, seal=False)
    assert result.returncode != 0
    assert "seal" in result.stdout + result.stderr
    assert not (loop / "review-outcome-round-1.json").exists()


@pytest.mark.parametrize("first_status", ["PASS", "UNKNOWN"])
def test_real_d1_actual_review_and_unique_necessary_repair_close(
    initialized_project_dir, first_status
):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _close_args,
        _complete_task,
        _outcome,
        _payload,
        _review_args,
        _review_record_args,
    )
    from tests.integration.test_simulation_quantified_loop import LOOP_ID

    root = initialized_project_dir
    loop, first = _simulation_ready(root)
    sealed = (loop / "decision-context.json").read_bytes()
    assert first["decision_capability"] == "implementation-simulation-v1"
    assert first["expert_result_format"] == "execution-and-simulation-assessment"
    recorded = _payload(
        _cli(
            root,
            *_review_record_args(
                first, _simulation_envelopes(root, first, status=first_status)
            ),
        )
    )
    final = first
    if first_status == "UNKNOWN":
        assert recorded["status"] == "needs_fix"
        assert _outcome(loop, 1)["simulation"]["evaluation"]["h"] == 1
        assert _cli(root, *_close_args(first)).returncode != 0
        (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        _complete_task(root, loop_id=LOOP_ID)
        final = _payload(_cli(root, *_review_args(LOOP_ID)))
        assert final["round_number"] == 2
        recorded = _payload(
            _cli(root, *_review_record_args(final, _simulation_envelopes(root, final)))
        )
    assert recorded["status"] == "passed"
    saved = _outcome(loop, final["round_number"])
    assert "b1" not in saved
    assert saved["simulation"]["evaluation"]["h"] == 0
    assert _payload(_cli(root, *_close_args(final)))["closed"] is True
    assert _payload(_cli(root, *_close_args(final)))["closed"] is True
    assert (loop / "decision-context.json").read_bytes() == sealed
    assert not (loop / "review-outcome-round-3.json").exists()


@pytest.mark.parametrize(
    "damage", ["validator", "source", "outer", "run-capability", "during-read"]
)
def test_real_d1_closed_reader_rejects_missing_or_drifted_actual_review(
    initialized_project_dir, damage
):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.loop_review_service import read_verified_implementation_close
    from tests.integration.test_quantified_implementation import (
        _cli,
        _close_args,
        _payload,
        _review_record_args,
    )

    root = initialized_project_dir
    loop, reviewed = _simulation_ready(root)
    _payload(
        _cli(
            root, *_review_record_args(reviewed, _simulation_envelopes(root, reviewed))
        )
    )
    assert _payload(_cli(root, *_close_args(reviewed)))["closed"]
    validator = validate_review_input_for_close
    if damage == "validator":
        validator = None
    elif damage == "source":
        (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
            "VALUE = 9\n", encoding="utf-8"
        )
    elif damage == "outer":
        path = loop / "review-outcome-round-1.json"
        payload = json.loads(path.read_bytes())
        payload["b1"] = payload.pop("simulation")
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif damage == "run-capability":
        path = loop / "loop-run.json"
        payload = json.loads(path.read_bytes())
        payload["decision_capability"] = "implementation-b1"
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:

        def validator(*args, **kwargs):
            result = validate_review_input_for_close(*args, **kwargs)
            path = loop / "review-outcome-round-1.json"
            payload = json.loads(path.read_bytes())
            payload["recorded_at"] += "x"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return result

    with pytest.raises(ValueError):
        read_verified_implementation_close(
            root, loop.name, review_input_validator=validator
        )


def test_real_d1_failed_expert_has_one_same_input_retry(initialized_project_dir):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _outcome,
        _payload,
        _review_record_args,
    )

    root = initialized_project_dir
    loop, reviewed = _simulation_ready(root)
    args = _review_record_args(
        reviewed, _simulation_envelopes(root, reviewed, failed=True)
    )
    assert _payload(_cli(root, *args))["status"] == "failed"
    assert _outcome(loop, 1)["infra_retry_count"] == 0
    assert _payload(_cli(root, *args))["status"] == "needs_user"
    final = _outcome(loop, 1)
    assert final["infra_retry_count"] == 1
    assert "b1" not in final and "simulation" not in final
    before = (loop / "review-outcome-round-1.json").read_bytes()
    assert _cli(root, *args).returncode != 0
    assert (loop / "review-outcome-round-1.json").read_bytes() == before
    assert not (loop / "review-outcome-round-2.json").exists()


def test_real_d1_satisfied_r1_drift_does_not_open_optional_r2(initialized_project_dir):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _close_args,
        _payload,
        _review_args,
        _review_record_args,
    )
    from tests.integration.test_simulation_quantified_loop import LOOP_ID

    root = initialized_project_dir
    loop, reviewed = _simulation_ready(root)
    args = _review_record_args(reviewed, _simulation_envelopes(root, reviewed))
    assert _payload(_cli(root, *args))["status"] == "passed"
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    current = _payload(_cli(root, *_review_args(LOOP_ID)))
    assert current["review_status"] == "needs_user"
    assert current["review_reason"] == "review-input-drift"
    assert current["round_number"] == 1
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    assert not (loop / "review-outcome-round-2.json").exists()


def test_real_d1_unqualified_r2_exhausts_without_third_round(initialized_project_dir):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _close_args,
        _complete_task,
        _outcome,
        _payload,
        _review_args,
        _review_record_args,
    )
    from tests.integration.test_simulation_quantified_loop import LOOP_ID

    root = initialized_project_dir
    loop, first = _simulation_ready(root)
    assert (
        _payload(
            _cli(
                root,
                *_review_record_args(
                    first, _simulation_envelopes(root, first, status="UNKNOWN")
                ),
            )
        )["status"]
        == "needs_fix"
    )
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _complete_task(root, loop_id=LOOP_ID)
    second = _payload(_cli(root, *_review_args(LOOP_ID)))
    assert second["round_number"] == 2
    args = _review_record_args(
        second, _simulation_envelopes(root, second, status="UNKNOWN")
    )
    assert _payload(_cli(root, *args))["status"] == "needs_user"
    assert _outcome(loop, 2)["simulation"]["decision"]["reason"] == "review-round-limit"
    assert _cli(root, *args).returncode != 0
    assert _cli(root, *_close_args(second)).returncode != 0
    assert not (loop / "review-outcome-round-3.json").exists()

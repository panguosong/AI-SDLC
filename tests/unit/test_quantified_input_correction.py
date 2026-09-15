"""复现正式版本的机器时点错误，核对纠错不改历史或刷新额度。"""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from ai_sdlc.core.loop_simulation_context import (
    SimulationContext,
    SimulationPrepareRequest,
    transition_simulation,
    validate_simulation_context,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_stage_simulation import begin, initial_selected, transition

FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "quantified-cost-condition-history.json"
)


def history():
    """该匿名合成原件由未修订3.1.0生成，不包含真实项目判断。"""
    return validate_simulation_context(
        SimulationContext.model_validate_json(FIXTURE.read_bytes())
    )


def advance(context, operation, *, now=4000, source="4" * 64, **kwargs):
    review_started = kwargs.pop("review_started", False)
    execution_started = kwargs.pop("execution_started", False)
    request_id = kwargs.pop("request_id", f"test-{operation}")
    result = transition_simulation(
        context,
        SimulationPrepareRequest.model_validate(
            dict(operation=operation, request_id=request_id, **kwargs)
        ),
        loop_id="test",
        input_digest="sha256:" + "2" * 64,
        source_digest=source,
        source_manifest={"spec.md": "1" * 64},
        now_ms=now,
        actual_ready=False,
        review_started=review_started,
        execution_started=execution_started,
    )
    return validate_simulation_context(result)


def corrected_candidates(context):
    rows = [c.model_dump(mode="json") for c in context.comparisons[0].candidates]
    for row in rows:
        row["cost_decision_point"] = "before-execution"
        row["future_cost_estimate"]["scope"] = context.plan.time_plan.scope
    return rows


@pytest.mark.parametrize("field", ["point", "scope"])
def test_bad_machine_cost_condition_cannot_be_frozen(field):
    context = begin(loop_type="requirement")
    before = context.model_dump_json()
    candidate = candidate_data(context.plan)
    if field == "point":
        candidate["cost_decision_point"] = "选择完成后执行之前"
    else:
        candidate["future_cost_estimate"]["scope"] = "another-time-window"
    with pytest.raises(ValueError, match="cost|condition|scope") as error:
        transition(context, "freeze-comparison", candidates=[candidate])
    assert "A" in str(error.value)
    assert context.model_dump_json() == before
    assert context.pending_batch.candidates == ()


def test_historical_payload_and_digest_remain_readable_without_new_fields():
    original = json.loads(FIXTURE.read_bytes())
    context = history()
    assert context.model_dump(mode="json") == original
    assert "input_correction" not in context.model_dump(mode="json")
    assert context.comparisons[0].selection.selected_id is None


def test_correction_preserves_history_time_and_uses_only_second_batch():
    old = history()
    original = old.model_dump_json()
    corrected = advance(old, "correct-input")
    assert old.model_dump_json() == original
    assert corrected.comparisons == old.comparisons
    assert corrected.started_at_ms == old.started_at_ms
    assert corrected.contracts == old.contracts
    assert corrected.sources == old.sources
    assert corrected.pending_batch.number == 2
    assert corrected.pending_batch.base_input_digest == "4" * 64
    assert corrected.initial_selection_id is None
    assert advance(corrected, "correct-input", now=5000) == corrected
    with pytest.raises(ValueError):
        advance(corrected, "correct-input", request_id="another-correction")


def test_second_batch_requires_new_judgement_and_keeps_first_failure():
    old = history()
    corrected = advance(old, "correct-input")
    frozen = advance(
        corrected,
        "freeze-comparison",
        request_id="freeze-corrected",
        now=5000,
        candidates=corrected_candidates(old),
    )
    assert frozen.pending_batch.judge_input_digest != (
        old.comparisons[0].judge_input_digest
    )
    with pytest.raises(ValueError, match="judgement-input"):
        advance(
            frozen,
            "record-comparison",
            request_id="old-judgement-replay",
            judgement=old.comparisons[0].judgement.model_dump(mode="json"),
            now=6000,
        )
    completed = advance(
        frozen,
        "record-comparison",
        request_id="judge-corrected",
        now=6000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data(n, 3, 3) for n in ("A", "B")],
        },
    )
    assert completed.initial_selection_id == "A"
    assert len(completed.comparisons) == 2
    assert completed.comparisons[0] == old.comparisons[0]
    with pytest.raises(ValueError):
        advance(completed, "correct-input", request_id="third-batch", now=7000)


@pytest.mark.parametrize("kind", ["mechanism", "scope", "id"])
def test_correction_cannot_smuggle_a_different_route(kind):
    old = history()
    corrected = advance(old, "correct-input")
    candidates = corrected_candidates(old)
    if kind == "mechanism":
        candidates[0]["mechanism"] = "另一种产品功能"
    elif kind == "scope":
        candidates[0]["changed_scope"] = ["another-component"]
    else:
        candidates[0]["candidate_id"] = "replacement"
    with pytest.raises(ValueError, match="correction-candidate"):
        advance(corrected, "freeze-comparison", candidates=candidates, now=5000)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execution_started": True},
        {"execution_started": None},
        {"review_started": True},
        {"now": 4_000_000},
    ],
)
def test_started_or_expired_work_cannot_gain_correction(kwargs):
    with pytest.raises(ValueError):
        advance(history(), "correct-input", **kwargs)


def test_valid_selection_and_non_format_rejection_cannot_be_reopened():
    selected = initial_selected(loop_type="requirement")
    with pytest.raises(ValueError):
        advance(selected, "correct-input")
    context = begin(loop_type="requirement")
    candidate = candidate_data(context.plan, seconds=None)
    frozen = transition(context, "freeze-comparison", candidates=[candidate])
    rejected = transition(
        frozen,
        "record-comparison",
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data()],
        },
    )
    with pytest.raises(ValueError):
        advance(rejected, "correct-input")


def test_correction_receipt_cannot_hide_modified_old_history():
    corrected = advance(history(), "correct-input")
    payload = deepcopy(corrected.model_dump(mode="json"))
    payload["comparisons"][0]["elapsed_seconds"] += 1
    payload["context_digest"] = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "context_digest"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(ValueError):
        validate_simulation_context(SimulationContext.model_validate(payload))


def test_unsubstantiated_cost_reduction_cannot_fit_a_correction_into_budget():
    old = history()
    corrected = advance(old, "correct-input")
    candidates = corrected_candidates(old)
    candidates[0]["future_cost_estimate"]["lower_seconds"] = 1
    candidates[0]["future_cost_estimate"]["upper_seconds"] = 2
    with pytest.raises(ValueError, match="cost-fact"):
        advance(corrected, "freeze-comparison", candidates=candidates, now=5000)


def test_documented_completed_preparation_does_not_reset_original_window():
    old = history()
    corrected = advance(old, "correct-input")
    candidates = corrected_candidates(old)
    candidates[0]["basis"].append(
        {
            "id": "prepared",
            "kind": "project_fact",
            "source_ref": "completed-work",
            "locator": "complete requirement draft",
            "statement": "已形成需求草案，剩余工作仍包含独立评审和冻结。",
        }
    )
    candidates[0]["future_cost_estimate"].update(
        lower_seconds=10, upper_seconds=20, basis_refs=["b", "prepared"]
    )
    frozen = advance(
        corrected,
        "freeze-comparison",
        candidates=candidates,
        sources=[
            {
                "id": "completed-work",
                "path": "completed-work.md",
                "sha256": "a" * 64,
                "locator": "all",
                "claim": "已经完成的需求准备；不代替实际评审。",
            }
        ],
        now=5000,
    )
    assert frozen.started_at_ms == old.started_at_ms
    assert frozen.plan.time_plan == old.plan.time_plan
    assert frozen.comparisons[0] == old.comparisons[0]
    assert frozen.initial_selection_id is None
    assert frozen.pending_batch.judgement is None

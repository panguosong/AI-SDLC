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


def _cost_rejected_history(*, conflict=False):
    context = begin(loop_type="implementation")
    candidates = [
        candidate_data(context.plan, name, seconds=(1800, 3600)) for name in ("A", "B")
    ]
    if conflict:
        candidates[0]["known_required_conflicts"] = [
            context.plan.goal_contract.obligations[0].id
        ]
    frozen = transition(context, "freeze-comparison", candidates=candidates, now=2000)
    return transition(
        frozen,
        "record-comparison",
        now=3000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data(name) for name in ("A", "B")],
        },
    )


def _completed_preparation_revision(old):
    candidates = corrected_candidates(old)
    for candidate in candidates:
        candidate["basis"].append(
            {
                "id": "completed",
                "kind": "project_fact",
                "source_ref": "completed-work",
                "locator": "command-receipt",
                "statement": "已完成准备命令及回执；后续仍含独立判断、实施和验收。",
            }
        )
        candidate["future_cost_estimate"].update(
            lower_seconds=1700, upper_seconds=3500, basis_refs=["b", "completed"]
        )
    return candidates, [
        {
            "id": "completed-work",
            "path": "evidence/completed-preparation.json",
            "sha256": "a" * 64,
            "locator": "command-receipt",
            "claim": "已完成准备的命令、起止、退出码和输出；仍需独立核验。",
        }
    ]


def test_cost_rejection_can_use_original_second_batch_with_new_preparation_facts():
    old = _cost_rejected_history()
    original = old.model_dump_json()
    assert {item.reason for item in old.comparisons[0].selection.excluded} == {
        "time_plan_exceeded"
    }
    corrected = advance(old, "correct-input")
    candidates, sources = _completed_preparation_revision(old)
    frozen = advance(
        corrected, "freeze-comparison", candidates=candidates, sources=sources, now=5000
    )
    assert frozen.pending_batch.number == 2
    assert frozen.started_at_ms == old.started_at_ms
    assert frozen.contracts == old.contracts
    assert frozen.comparisons[0] == old.comparisons[0]
    with pytest.raises(ValueError, match="judgement-input"):
        advance(
            frozen,
            "record-comparison",
            judgement=old.comparisons[0].judgement.model_dump(mode="json"),
            now=6000,
        )
    completed = advance(
        frozen,
        "record-comparison",
        now=6000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data(name) for name in ("A", "B")],
        },
    )
    assert completed.initial_selection_id == "A"
    assert old.model_dump_json() == original
    assert completed.comparisons[0] == old.comparisons[0]
    with pytest.raises(ValueError):
        advance(completed, "correct-input", request_id="third-batch", now=7000)


@pytest.mark.parametrize(
    "mutation",
    [
        "no-fact",
        "old-source",
        "same-bytes",
        "same-path",
        "generated",
        "generated-alias",
        "unused-generated",
        "not-lower",
        "still-over-window",
    ],
)
def test_cost_revision_requires_new_source_and_a_lower_admissible_forecast(mutation):
    old = _cost_rejected_history()
    corrected = advance(old, "correct-input")
    candidates, sources = _completed_preparation_revision(old)
    if mutation == "no-fact":
        candidates[0]["basis"].pop()
        candidates[0]["future_cost_estimate"]["basis_refs"] = ["b"]
    elif mutation == "old-source":
        candidates[0]["basis"][-1]["source_ref"] = old.sources[0].id
    elif mutation == "same-bytes":
        sources[0]["sha256"] = old.sources[0].sha256
    elif mutation == "same-path":
        sources[0]["path"] = old.sources[0].path
    elif mutation == "generated":
        sources[0]["path"] = (
            ".ai-sdlc/loops/implementation/previous/implementation-report.json"
        )
    elif mutation == "generated-alias":
        sources[0]["path"] = (
            ".AI-SDLC/LoOpS/design-contract/previous/design-contract-report.json"
        )
    elif mutation == "unused-generated":
        sources.append(
            {
                **sources[0],
                "id": "generated",
                "path": ".ai-sdlc/state/outcome.json",
                "sha256": "b" * 64,
            }
        )
        candidates[0]["basis"].append(
            {**candidates[0]["basis"][-1], "id": "unused", "source_ref": "generated"}
        )
    elif mutation == "not-lower":
        candidates[0]["future_cost_estimate"]["upper_seconds"] = 3600
    else:
        candidates[0]["future_cost_estimate"]["upper_seconds"] = 3599
    before = corrected.model_dump_json()
    with pytest.raises(ValueError, match="correction-cost"):
        advance(
            corrected,
            "freeze-comparison",
            candidates=candidates,
            sources=sources,
            now=5000,
        )
    assert corrected.model_dump_json() == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execution_started": True},
        {"execution_started": None},
        {"review_started": True},
        {"now": 4_000_000},
    ],
)
def test_cost_rejection_correction_cannot_reopen_started_or_expired_work(kwargs):
    with pytest.raises(ValueError):
        advance(_cost_rejected_history(), "correct-input", **kwargs)


def test_cost_correction_cannot_remove_a_required_conflict():
    with pytest.raises(ValueError, match="correction-unavailable"):
        advance(_cost_rejected_history(conflict=True), "correct-input")


def test_second_cost_judgement_still_charges_elapsed_time_and_can_reject_every_route():
    old = _cost_rejected_history()
    corrected = advance(old, "correct-input")
    candidates, sources = _completed_preparation_revision(old)
    frozen = advance(
        corrected, "freeze-comparison", candidates=candidates, sources=sources, now=5000
    )
    completed = advance(
        frozen,
        "record-comparison",
        now=120_000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data(name) for name in ("A", "B")],
        },
    )
    assert completed.initial_selection_id is None
    assert completed.comparisons[-1].selection.reason == "model_plan_not_feasible"
    assert completed.comparisons[-1].elapsed_seconds == 119
    assert completed.comparisons[0] == old.comparisons[0]


def time_revision_request(old, sources=None):
    """合成重新分解的完整计划，不声称任何真实项目已完成工作。"""
    if sources is None:
        sources = _completed_preparation_revision(old)[1]
    contracts = [c.model_dump(mode="json") for c in old.contracts]
    for contract in contracts:
        contract["time_plan"].update(
            window_seconds=10800,
            basis_refs=[*old.plan.time_plan.basis_refs, sources[0]["id"]],
            work_breakdown=["保留累计准备历时", "完整实施、验证、独立评审和 Close"],
        )
    return dict(
        operation="revise-time-plan",
        request_id="revise-time-plan",
        contracts=contracts,
        sources=sources,
        reason="原计划漏计完整后续工作，依据新分解修订总窗口。",
    )


def time_revision_candidates(old, revised):
    from ai_sdlc.core.loop_simulation import stage_contract_digest

    candidates, _ = _completed_preparation_revision(old)
    for candidate in candidates:
        candidate["contract_digest"] = stage_contract_digest(revised.plan)
        candidate["future_cost_estimate"].update(lower_seconds=3600, upper_seconds=5000)
    return candidates


@pytest.mark.parametrize("late", [False, True])
def test_time_plan_revision_replays_old_contract_and_charges_original_elapsed(late):
    old = _cost_rejected_history()
    request = time_revision_request(old)
    revised = advance(old, now=4_000_000, **request)
    assert revised.started_at_ms == old.started_at_ms
    assert revised.comparisons == old.comparisons
    assert revised.input_correction.old_contracts == old.contracts
    assert (
        revised.input_correction.revision_request
        == SimulationPrepareRequest.model_validate(request).model_dump(
            mode="json", exclude_unset=True
        )
    )
    assert "candidates" not in revised.input_correction.revision_request
    assert revised.contract_for_batch(revised.comparisons[0]) == old.plan
    assert revised.current_contract == revised.plan
    assert revised.plan.time_plan.window_seconds == 10800
    assert (
        validate_simulation_context(
            SimulationContext.model_validate_json(revised.model_dump_json())
        )
        == revised
    )
    assert advance(revised, now=4_001_000, **request) == revised
    frozen = advance(
        revised,
        "freeze-comparison",
        now=4_001_000,
        candidates=time_revision_candidates(old, revised),
    )
    with pytest.raises(ValueError, match="judgement-input"):
        advance(
            frozen,
            "record-comparison",
            now=4_002_000,
            judgement=old.comparisons[0].judgement.model_dump(mode="json"),
        )
    completed = advance(
        frozen,
        "record-comparison",
        now=6_000_000 if late else 4_002_000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [assessment_data(n) for n in ("A", "B")],
        },
    )
    assert completed.initial_selection_id == (None if late else "A")
    assert completed.comparisons[-1].elapsed_seconds == (5999 if late else 4001)
    assert completed.comparisons[0] == old.comparisons[0]
    assert (
        validate_simulation_context(
            SimulationContext.model_validate_json(completed.model_dump_json())
        )
        == completed
    )
    with pytest.raises(ValueError):
        advance(completed, now=6_001_000, **{**request, "request_id": "third-batch"})


@pytest.mark.parametrize(
    "mutation",
    [
        "same-window",
        "scope",
        "criteria",
        "goal",
        "old-source",
        "same-path",
        "same-sha",
        "framework",
        "unbound-source",
        "empty-reason",
    ],
)
def test_time_plan_revision_cannot_change_goals_or_launder_sources(mutation):
    old = _cost_rejected_history()
    request = time_revision_request(old)
    if mutation == "same-window":
        for contract in request["contracts"]:
            contract["time_plan"]["window_seconds"] = 3600
    elif mutation == "scope":
        for contract in request["contracts"]:
            contract["time_plan"]["scope"] = "less-work"
    elif mutation == "criteria":
        request["contracts"][0]["criteria"][0]["statement"] = "替换标准"
    elif mutation == "goal":
        for contract in request["contracts"]:
            contract["goal_contract"]["goals"][0]["weight"] = 2
    elif mutation == "old-source":
        for contract in request["contracts"]:
            contract["time_plan"]["basis_refs"] = list(old.plan.time_plan.basis_refs)
    elif mutation == "same-path":
        request["sources"][0]["path"] = old.sources[0].path
    elif mutation == "same-sha":
        request["sources"][0]["sha256"] = old.sources[0].sha256
    elif mutation == "framework":
        request["sources"][0]["path"] = ".AI-SDLC/state/plan.json"
    elif mutation == "unbound-source":
        request["sources"].append(
            {
                **request["sources"][0],
                "id": "unused",
                "path": "unused.json",
                "sha256": "b" * 64,
            }
        )
    else:
        request["reason"] = " "
    with pytest.raises(ValueError):
        advance(old, **request)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"execution_started": True},
        {"execution_started": None},
        {"review_started": True},
        {"now": 11_000_000},
    ],
)
def test_time_plan_revision_respects_execution_review_and_new_window(kwargs):
    old = _cost_rejected_history()
    with pytest.raises(ValueError):
        advance(old, **time_revision_request(old), **kwargs)


@pytest.mark.parametrize(
    "mutation",
    ["old-contract", "request-reason", "request-source", "new-contract", "clock-reset"],
)
def test_time_revision_history_hash_binds_original_and_exact_request(mutation):
    from ai_sdlc.core.loop_simulation_context import _stamp

    old = _cost_rejected_history()
    revised = advance(old, **time_revision_request(old))
    payload = revised.model_dump(mode="json")
    if mutation == "old-contract":
        for contract in payload["input_correction"]["old_contracts"]:
            contract["time_plan"]["window_seconds"] = 7200
    elif mutation == "request-reason":
        payload["input_correction"]["revision_request"]["reason"] = "另一请求"
    elif mutation == "request-source":
        payload["input_correction"]["revision_request"]["sources"][0]["sha256"] = (
            "b" * 64
        )
    elif mutation == "new-contract":
        for contract in payload["contracts"]:
            contract["time_plan"]["window_seconds"] = 20000
    else:
        payload["started_at_ms"] += 1
    with pytest.raises(ValueError):
        validate_simulation_context(_stamp(payload))


@pytest.mark.parametrize("mutation", ["mixed-old-source", "unused-new-fact"])
def test_every_added_cost_fact_requires_new_source_and_cost_reference(mutation):
    old = _cost_rejected_history()
    corrected = advance(old, "correct-input")
    candidates, sources = _completed_preparation_revision(old)
    addition = {**candidates[0]["basis"][-1], "id": "extra"}
    if mutation == "mixed-old-source":
        addition["source_ref"] = old.sources[0].id
        candidates[0]["future_cost_estimate"]["basis_refs"].append("extra")
    candidates[0]["basis"].append(addition)
    with pytest.raises(ValueError, match="correction-cost"):
        advance(
            corrected,
            "freeze-comparison",
            now=5000,
            candidates=candidates,
            sources=sources,
        )


def test_old_correction_serialization_does_not_inject_revision_fields():
    corrected = advance(history(), "correct-input")
    receipt = corrected.model_dump(mode="json")["input_correction"]
    assert "old_contracts" not in receipt
    assert "revision_request" not in receipt


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "mechanism",
        "scope",
        "assumption",
        "old-source",
        "unreferenced-fact",
        "expired",
    ],
)
def test_time_revision_freeze_keeps_route_and_current_time_boundaries(mutation):
    old = _cost_rejected_history()
    revised = advance(old, **time_revision_request(old))
    candidates = time_revision_candidates(old, revised)
    if mutation == "digest":
        candidates[0]["contract_digest"] = (
            old.comparisons[0].candidates[0].contract_digest
        )
    elif mutation == "mechanism":
        candidates[0]["mechanism"] = "替换产品机制"
    elif mutation == "scope":
        candidates[0]["changed_scope"] = ["other-scope"]
    elif mutation == "assumption":
        candidates[0]["assumptions"] = ["替换业务假设"]
    elif mutation == "old-source":
        candidates[0]["basis"][-1]["source_ref"] = old.sources[0].id
    elif mutation == "unreferenced-fact":
        candidates[0]["basis"].append({**candidates[0]["basis"][-1], "id": "unused"})
    with pytest.raises(ValueError):
        advance(
            revised,
            "freeze-comparison",
            candidates=candidates,
            now=6_000_000 if mutation == "expired" else 5000,
        )


def test_time_revision_is_not_available_for_machine_error_or_second_correction():
    with pytest.raises(ValueError):
        advance(history(), **time_revision_request(history()))
    old = _cost_rejected_history()
    corrected = advance(old, "correct-input")
    with pytest.raises(ValueError):
        advance(corrected, **time_revision_request(old))

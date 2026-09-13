"""阶段扩展沿用模拟纯核，但不继承初始计划的分数或修改权限。"""

from pathlib import Path

import pytest

from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data


def stage_contract(profile="code-result-v1", loop_type="implementation"):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data.update(
        capability="stage-simulation-v1", loop_type=loop_type, profile_id=profile
    )
    return StageScoreContract.model_validate(data)


@pytest.mark.parametrize(
    "loop_type,profile",
    [
        ("requirement", "requirement-analysis-v1"),
        ("design-contract", "design-contract-v1"),
        ("implementation", "implementation-plan-v1"),
        ("implementation", "code-result-v1"),
        ("frontend-evidence", "frontend-evidence-v1"),
        ("local-pr-review", "delivery-readiness-v1"),
    ],
)
def test_all_stage_profiles_use_the_same_forecast_score(loop_type, profile):
    from ai_sdlc.core.loop_simulation import score_candidate
    from ai_sdlc.core.loop_simulation_models import (
        CandidateAssessment,
        SimulatedCandidate,
    )

    contract = stage_contract(profile, loop_type)
    result = score_candidate(
        contract,
        SimulatedCandidate.model_validate(candidate_data(contract)),
        CandidateAssessment.model_validate(assessment_data()),
    )
    assert result.s_low == result.s_high == 75
    assert "q" not in result.model_dump()


def improvement_result(old=(2, 2), new=(3, 3), *, direction=None, cheap=False):
    from ai_sdlc.core.loop_simulation import select_simulated_improvement
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = stage_contract().model_dump()
    data["criteria"][0]["improvement_direction"] = direction
    contract = StageScoreContract.model_validate(data)
    candidates = [candidate_data(contract, "keep"), candidate_data(contract, "better")]
    for row in candidates:
        row["cost_decision_point"] = "before-improvement"
    if cheap:
        candidates[1]["future_cost_estimate"].update(lower_seconds=1, upper_seconds=2)
    return select_simulated_improvement(
        contract,
        candidates,
        [assessment_data("keep", *old), assessment_data("better", *new)],
        decision_point="before-improvement",
        elapsed_seconds=10,
        incumbent_id="keep",
        criterion_ids=("coverage",),
    )


def test_improvement_requires_nonoverlapping_meaningful_anchor():
    result = improvement_result()
    assert result.selected_id == "better"
    assert result.criterion_ids == ("coverage",)
    assert result.stop_reason is None


@pytest.mark.parametrize(
    "old,new,cheap,reason",
    [
        ((2, 3), (3, 4), False, "comparison_inconclusive"),
        ((3, 3), (4, 4), False, "simulated_targets_met"),
        ((2, 2), (2, 2), True, "comparison_inconclusive"),
    ],
)
def test_cheaper_or_higher_score_alone_never_authorizes_improvement(
    old, new, cheap, reason
):
    result = improvement_result(old, new, cheap=cheap)
    assert result.selected_id is None
    assert result.stop_reason == reason


def test_target_met_with_frozen_optimization_direction_can_improve():
    assert (
        improvement_result((3, 3), (4, 4), direction="降低原目标内返工").selected_id
        == "better"
    )


def test_local_pr_cannot_select_an_execution_route():
    from ai_sdlc.core.loop_simulation import select_simulated_candidates

    contract = stage_contract("delivery-readiness-v1", "local-pr-review")
    with pytest.raises(ValueError, match="local-pr.*route"):
        select_simulated_candidates(
            contract,
            [candidate_data(contract)],
            [assessment_data()],
            decision_point="before-execution",
            elapsed_seconds=0,
        )


def test_stage_contract_rejects_cross_stage_profile():
    with pytest.raises(ValueError, match="profile"):
        stage_contract("code-result-v1", "requirement")


def transition(
    context, operation, request_id=None, *, source="3" * 64, now=1000, **kwargs
):
    from ai_sdlc.core.loop_simulation_context import (
        SimulationPrepareRequest,
        transition_simulation,
        validate_simulation_context,
    )

    actual_ready = kwargs.pop("actual_ready", False)
    review_started = kwargs.pop("review_started", False)
    result = transition_simulation(
        context,
        SimulationPrepareRequest.model_validate(
            {"operation": operation, "request_id": request_id or operation, **kwargs}
        ),
        loop_id="test",
        input_digest="sha256:" + "2" * 64,
        source_digest=source,
        source_manifest={"spec.md": "1" * 64},
        now_ms=now,
        actual_ready=actual_ready,
        review_started=review_started,
    )
    return validate_simulation_context(result)


def begin(*, loop_type="implementation", d1=False):
    from ai_sdlc.core.loop_simulation_models import STAGE_PROFILES

    contracts = [
        (
            contract_data()
            if profile == "implementation-plan-v1"
            else {**contract_data(), "profile_id": profile}
        )
        if d1
        else stage_contract(profile, loop_type).model_dump()
        for profile in STAGE_PROFILES[loop_type]
    ]
    return transition(
        None,
        "begin",
        contracts=contracts,
        sources=[
            {
                "id": "spec",
                "path": "spec.md",
                "sha256": "1" * 64,
                "locator": "spec",
                "claim": "冻结目标",
            }
        ],
    )


def initial_selected(*, loop_type="implementation", d1=False):
    context = begin(loop_type=loop_type, d1=d1)
    candidate_id = "current-staged-tree" if loop_type == "local-pr-review" else "A"
    context = transition(
        context,
        "freeze-comparison",
        candidates=[candidate_data(context.plan, candidate_id)],
    )
    return transition(
        context,
        "record-comparison",
        judgement={
            "judge_input_digest": context.pending_batch.judge_input_digest,
            "assessments": [assessment_data(candidate_id, 4, 4)],
        },
    )


def improvement_request(context):
    contract = (
        next(c for c in context.contracts if c.profile_id == "code-result-v1")
        if context.loop_type == "implementation"
        else context.plan
    )
    incumbent = candidate_data(contract, "keep")
    incumbent["cost_decision_point"] = "before-improvement"
    return {
        "incumbent": incumbent,
        "baseline_digest": "4" * 64,
        "criterion_ids": ["coverage"],
        "hypothesis": "收紧原访问边界",
        "future_cost_estimate": {
            **incumbent["future_cost_estimate"],
            "upper_seconds": 60,
        },
    }


def start_improvement(context=None, **kwargs):
    context = initial_selected() if context is None else context
    return transition(
        context,
        "begin-improvement",
        source="4" * 64,
        now=kwargs.pop("now", 2000),
        actual_ready=True,
        improvement=improvement_request(context),
        **kwargs,
    )


def complete_improvement(context=None, old=(2, 2), new=(3, 3)):
    context = start_improvement() if context is None else context
    better = candidate_data(context.current_contract, "better")
    better["cost_decision_point"] = "before-improvement"
    context = transition(
        context,
        "freeze-comparison",
        "freeze-code",
        source="4" * 64,
        now=3000,
        candidates=[context.improvement.incumbent, better],
    )
    return transition(
        context,
        "record-comparison",
        "judge-code",
        source="4" * 64,
        now=4000,
        judgement={
            "judge_input_digest": context.pending_batch.judge_input_digest,
            "assessments": [
                assessment_data("keep", *old),
                assessment_data("better", *new),
            ],
        },
    )


def test_current_actual_code_is_reassessed_without_reusing_plan_score():
    context = complete_improvement()
    assert context.initial_selection_id == "A"
    assert context.selection.scores["A"].s_low == 100
    assert context.comparisons[1].selection.scores["keep"].s_low == 50
    assert context.conditional_improvement.selected_id == "better"
    assert context.conditional_improvement.baseline_digest == "4" * 64
    assert context.conditional_improvement.changed_scope == ("request-handler",)
    assert context.selected_candidate.candidate_id == "A"
    assert context.started_at_ms == 1000


def test_clock_rollback_cannot_refund_initial_comparison_time_for_improvement():
    context = begin()
    context = transition(
        context,
        "freeze-comparison",
        candidates=[candidate_data(context.plan)],
    )
    context = transition(
        context,
        "record-comparison",
        now=3_570_000,
        judgement={
            "judge_input_digest": context.pending_batch.judge_input_digest,
            "assessments": [assessment_data("A", 4, 4)],
        },
    )
    assert context.comparisons[0].elapsed_seconds == 3569
    request = improvement_request(context)
    with pytest.raises(ValueError, match="model-plan-not-feasible"):
        transition(
            context,
            "begin-improvement",
            now=3_571_000,
            source="4" * 64,
            actual_ready=True,
            improvement=request,
        )
    with pytest.raises(ValueError, match="clock-moved-backwards"):
        transition(
            context,
            "begin-improvement",
            now=2000,
            source="4" * 64,
            actual_ready=True,
            improvement=request,
        )


def test_clock_floor_includes_frozen_input_and_technical_recovery():
    context = begin()
    context = transition(
        context,
        "freeze-comparison",
        now=9001,
        candidates=[candidate_data(context.plan)],
    )
    failure = {
        "stage": "judging",
        "reason": "已结束的暂态失败",
        "retry": True,
        "prior_call_terminated": True,
    }
    with pytest.raises(ValueError, match="clock-moved-backwards"):
        transition(context, "record-comparison", "retry", now=9000, failure=failure)
    context = transition(
        context, "record-comparison", "retry", now=10_001, failure=failure
    )
    with pytest.raises(ValueError, match="clock-moved-backwards"):
        transition(
            context,
            "record-comparison",
            "record-after-retry",
            now=10_000,
            judgement={
                "judge_input_digest": context.pending_batch.judge_input_digest,
                "assessments": [assessment_data()],
            },
        )
    accepted = transition(
        context,
        "record-comparison",
        "record-after-retry",
        now=10_001,
        judgement={
            "judge_input_digest": context.pending_batch.judge_input_digest,
            "assessments": [assessment_data()],
        },
    )
    assert accepted.last_observed_at_ms == 10_001
    assert accepted.comparisons[0].elapsed_seconds == 10
    assert (
        transition(
            accepted,
            "record-comparison",
            "record-after-retry",
            now=2000,
            judgement={
                "judge_input_digest": context.pending_batch.judge_input_digest,
                "assessments": [assessment_data()],
            },
        )
        == accepted
    )


def test_stage_validation_rejects_backwards_batch_elapsed_even_with_valid_digest():
    from ai_sdlc.core.loop_simulation_context import _stamp, validate_simulation_context

    payload = complete_improvement().model_dump(mode="json")
    payload["comparisons"][0]["elapsed_seconds"] = 4
    payload["last_observed_at_ms"] = 6000
    with pytest.raises(ValueError, match="clock-moved-backwards"):
        validate_simulation_context(_stamp(payload))


@pytest.mark.parametrize(
    "loop_type", ["requirement", "design-contract", "frontend-evidence"]
)
def test_other_stage_hosts_share_single_contract_and_improvement(loop_type):
    context = complete_improvement(
        start_improvement(initial_selected(loop_type=loop_type))
    )
    assert len(context.contracts) == 1
    assert context.conditional_improvement.selected_id == "better"


@pytest.mark.parametrize(
    "failure",
    ["missing_actual", "review_started", "baseline_drift", "expired", "code_view"],
)
def test_improvement_begin_rejects_unbound_or_late_work(failure):
    context = initial_selected()
    proposal = improvement_request(context)
    kwargs = {"actual_ready": True, "source": "4" * 64, "now": 2000}
    if failure == "missing_actual":
        kwargs["actual_ready"] = False
    elif failure == "review_started":
        kwargs["review_started"] = True
    elif failure == "baseline_drift":
        kwargs["source"] = "5" * 64
    elif failure == "expired":
        kwargs["now"] = 3600001
    else:
        proposal["incumbent"]["contract_digest"] = context.selection.contract_digest
    with pytest.raises(ValueError):
        transition(context, "begin-improvement", improvement=proposal, **kwargs)


def test_improvement_requires_same_baseline_during_freeze_and_record():
    context = start_improvement()
    with pytest.raises(ValueError, match="source-drift"):
        transition(
            context,
            "freeze-comparison",
            "changed-tree",
            source="5" * 64,
            now=3000,
            candidates=[context.improvement.incumbent],
        )


def test_sealed_conditional_proposal_expiry_shrinks_to_stop():
    from ai_sdlc.core.loop_simulation_context import conditional_improvement_admission

    context = transition(
        complete_improvement(), "seal-for-review", now=5000, source="4" * 64
    )
    assert (
        conditional_improvement_admission(context, source_digest="4" * 64, now_ms=6000)
        is None
    )
    assert (
        conditional_improvement_admission(context, source_digest="4" * 64, now_ms=4500)
        == "simulation-clock-unavailable"
    )
    assert (
        conditional_improvement_admission(
            context, source_digest="4" * 64, now_ms=3590000
        )
        == "model_plan_not_feasible"
    )
    assert (
        conditional_improvement_admission(context, source_digest="5" * 64, now_ms=6000)
        == "simulation-improvement-baseline-drift"
    )
    with pytest.raises(ValueError, match="review-sealed"):
        transition(
            context,
            "begin-improvement",
            "reopen",
            improvement=improvement_request(context),
        )


def test_improvement_failure_recovery_keeps_budget_and_original_route():
    context = start_improvement()
    context = transition(
        context,
        "record-comparison",
        "retry",
        source="4" * 64,
        now=3000,
        failure={
            "stage": "drafting",
            "reason": "调用失败",
            "retry": True,
            "prior_call_terminated": True,
        },
    )
    assert context.pending_batch.number == 2
    with pytest.raises(ValueError, match="retry-unavailable"):
        transition(
            context,
            "record-comparison",
            "retry-again",
            source="4" * 64,
            now=4000,
            failure={
                "stage": "drafting",
                "reason": "调用失败",
                "retry": True,
                "prior_call_terminated": True,
            },
        )
    context = transition(
        context,
        "record-comparison",
        "failed",
        source="4" * 64,
        now=4000,
        failure={"stage": "drafting", "reason": "恢复失败"},
    )
    assert context.initial_selection_id == "A"
    assert context.conditional_improvement is None
    assert context.pending_batch is None
    assert len(context.comparisons) == 2
    with pytest.raises(ValueError, match="opportunities-exhausted"):
        start_improvement(context, request_id="third", now=5000)


def test_d1_context_never_accepts_improvement():
    context = initial_selected(d1=True)
    with pytest.raises(ValueError, match="d1-improvement"):
        start_improvement(context)
    assert "improvement" not in context.model_dump(mode="json")
    assert "conditional_improvement" not in context.model_dump(mode="json")
    assert "last_observed_at_ms" not in context.model_dump(mode="json")


def test_local_pr_context_is_one_current_tree_with_no_search_or_improve():
    context = initial_selected(loop_type="local-pr-review")
    assert context.selection.eligible_ids == ()
    assert "no_execution_route_authorization" in context.selection.limitations
    with pytest.raises(ValueError, match="local-pr-improvement"):
        start_improvement(context)
    context = transition(context, "seal-for-review")
    assert context.phase == "review_sealed"


def test_nonadvancing_improvement_is_recorded_but_not_proposed():
    context = complete_improvement(old=(2, 3), new=(3, 4))
    assert len(context.comparisons) == 2
    assert context.conditional_improvement is None
    assert context.initial_selection_id == "A"


def test_frozen_d1_original_bytes_roundtrip_without_new_fields_or_digest():
    from ai_sdlc.core.loop_simulation_context import (
        SimulationContext,
        validate_simulation_context,
    )

    # 原件来自 D1 冻结交付副本的旧纯核，避免用新序列化器自证兼容。
    raw = (
        (Path(__file__).parents[1] / "fixtures/simulation_d1_context.json")
        .read_text()
        .strip()
    )
    context = validate_simulation_context(SimulationContext.model_validate_json(raw))
    assert context.model_dump_json() == raw
    assert (
        context.context_digest
        == "7a1e58f212f438cb1e3e11ff66acc4eb43b41c64d3556c97d7fd2bcf4ad6753a"
    )
    assert begin(d1=True).model_dump_json() == raw


def test_improvement_receipt_is_idempotent_and_conflicting_id_is_rejected():
    context = initial_selected()
    proposal = improvement_request(context)
    context = transition(
        context,
        "begin-improvement",
        source="4" * 64,
        now=2000,
        actual_ready=True,
        improvement=proposal,
    )
    assert (
        transition(
            context,
            "begin-improvement",
            source="4" * 64,
            now=6000,
            actual_ready=True,
            improvement=proposal,
        )
        == context
    )
    proposal["hypothesis"] = "更换假设不能刷新同一回执"
    with pytest.raises(ValueError, match="request-id-conflict"):
        transition(
            context,
            "begin-improvement",
            source="4" * 64,
            actual_ready=True,
            improvement=proposal,
        )


def test_changed_actual_artifact_cannot_seal_conditional_improvement():
    with pytest.raises(ValueError, match="baseline-drift"):
        transition(complete_improvement(), "seal-for-review", source="5" * 64, now=5000)


def test_original_current_baseline_cannot_be_rewritten_at_freeze():
    context = start_improvement()
    changed = context.improvement.incumbent.model_dump()
    changed["artifact_sketch"]["key_structure"] = ["伪造更弱的原成果"]
    other = candidate_data(context.current_contract, "better")
    other["cost_decision_point"] = "before-improvement"
    with pytest.raises(ValueError, match="incumbent-changed"):
        transition(
            context,
            "freeze-comparison",
            "tampered-code",
            source="4" * 64,
            now=3000,
            candidates=[changed, other],
        )


def test_supported_critical_floor_cannot_become_unresolved_in_stage_comparison():
    from ai_sdlc.core.loop_simulation import select_simulated_candidates
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = stage_contract().model_dump()
    data["criteria"][0]["critical_floor"] = 2
    contract = StageScoreContract.model_validate(data)
    result = select_simulated_candidates(
        contract,
        [candidate_data(contract, "keep"), candidate_data(contract, "better", (1, 2))],
        [assessment_data("keep", 2, 2), assessment_data("better", 1, 4)],
        decision_point="before-execution",
        elapsed_seconds=0,
        incumbent_id="keep",
    )
    assert result.selected_id == "keep"
    assert any(
        e.candidate_id == "better" and e.reason == "protected_regression"
        for e in result.excluded
    )


def test_improvement_reports_inconclusive_not_target_met():
    from ai_sdlc.core.loop_simulation_context import improvement_stop_reason

    assert (
        improvement_stop_reason(complete_improvement(old=(2, 3), new=(3, 4)))
        == "comparison_inconclusive"
    )
    assert (
        improvement_stop_reason(complete_improvement(old=(3, 3), new=(4, 4)))
        == "simulated_targets_met"
    )

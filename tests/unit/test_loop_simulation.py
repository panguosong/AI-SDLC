"""评分只影响模拟初始选择，不制造实际验收或 Close。"""

from copy import deepcopy
from decimal import localcontext
from fractions import Fraction
from itertools import permutations

import pytest
from pydantic import ValidationError

from tests.unit.test_loop_simulation_models import contract_data


def candidate_data(contract, candidate_id="A", seconds=(20, 30)):
    from ai_sdlc.core.loop_simulation import stage_contract_digest

    return {
        "candidate_id": candidate_id,
        "contract_digest": stage_contract_digest(contract),
        "mechanism": f"机制 {candidate_id}",
        "changed_scope": ["request-handler"],
        "assumptions": ["范围不变"],
        "artifact_sketch": {
            "goal_mapping": ["o0 映射到访问检查"],
            "key_structure": ["先授权后读取"],
            "representative_scenarios": ["合法主体可读"],
            "failure_paths": ["未授权拒绝"],
            "downstream_impacts": ["保留现有消费者"],
            "excluded_scope": ["不增加界面"],
        },
        "basis": [
            {
                "id": "b",
                "kind": "logical_projection",
                "locator": "key_structure",
                "statement": "固定授权边界推演",
            }
        ],
        "execution_preconditions": [
            {
                "kind": kind,
                "status": "PASS",
                "evidence_refs": ["b"],
                "reason": "当前前提成立",
            }
            for kind in ("authorization", "safety", "mandatory_constraints")
        ],
        "cost_decision_point": "before-execution",
        "future_cost_estimate": {
            "lower_seconds": seconds[0],
            "upper_seconds": seconds[1],
            "scope": contract.time_plan.scope,
            "basis_refs": ["b"],
            "assumptions": ["含必要验证"],
        }
        if seconds is not None
        else None,
    }


def assessment_data(candidate_id="A", lower=3, upper=3):
    return {
        "candidate_id": candidate_id,
        "cost_check": "supported",
        "cost_reason": "预测包含必要验证和关闭，范围与分解一致",
        "criterion_assessments": [
            {
                "criterion_id": "coverage",
                "lower": lower,
                "upper": upper,
                "basis_refs": ["b"],
                "reason": "按冻结目标推演",
            }
        ],
    }


def test_fraction_score_and_unknown_are_separate_from_actual_q():
    from ai_sdlc.core.loop_simulation import score_candidate
    from ai_sdlc.core.loop_simulation_models import (
        CandidateAssessment,
        SimulatedCandidate,
        StageScoreContract,
    )

    contract = StageScoreContract.model_validate(contract_data())
    score = score_candidate(
        contract,
        SimulatedCandidate.model_validate(candidate_data(contract)),
        CandidateAssessment.model_validate(assessment_data(lower=2, upper=4)),
    )
    assert (score.s_low, score.s_high, score.u_sim) == (
        Fraction(50),
        Fraction(100),
        Fraction(1),
    )
    assert not score.targets_met
    assert "q" not in score.model_dump()


def test_time_admission_excludes_the_only_high_scoring_route():
    from ai_sdlc.core.loop_simulation import select_simulated_candidates
    from ai_sdlc.core.loop_simulation_models import (
        CandidateAssessment,
        SimulatedCandidate,
        StageScoreContract,
    )

    contract = StageScoreContract.model_validate(contract_data())
    result = select_simulated_candidates(
        contract,
        (
            SimulatedCandidate.model_validate(
                candidate_data(contract, seconds=(3600, 4000))
            ),
        ),
        (CandidateAssessment.model_validate(assessment_data(lower=4, upper=4)),),
        decision_point="before-execution",
        elapsed_seconds=1,
    )
    assert result.selected_id is None
    assert result.reason == "model_plan_not_feasible"


def parsed(data=None):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    return StageScoreContract.model_validate(contract_data() if data is None else data)


def select(contract, candidates, assessments, **kwargs):
    from ai_sdlc.core.loop_simulation import select_simulated_candidates

    return select_simulated_candidates(
        contract,
        candidates,
        assessments,
        decision_point=kwargs.pop("decision_point", "before-execution"),
        elapsed_seconds=kwargs.pop("elapsed_seconds", 0),
        **kwargs,
    )


def test_design_example_keeps_cost_admission_before_quality():
    data = contract_data(4)
    original = data["criteria"][0]
    data["criteria"] = []
    for i in range(4):
        criterion = deepcopy(original)
        criterion.update(id=f"c{i}", goal_shares=[{"goal_id": f"g{i}", "share": 1}])
        data["criteria"].append(criterion)
    contract = parsed(data)
    candidates = [
        candidate_data(contract, name, seconds)
        for name, seconds in (
            ("A", (1200, 1800)),
            ("B", (1800, 2400)),
            ("C", (5400, 7200)),
        )
    ]
    assessments = []
    for name, values in (
        ("A", ((3, 3), (2, 2), (2, 3), (3, 3))),
        ("B", ((3, 3), (3, 4), (3, 3), (3, 3))),
        ("C", ((3, 4),) * 4),
    ):
        assessments.append(
            {
                "candidate_id": name,
                "cost_check": "supported",
                "cost_reason": "完整范围已核对",
                "criterion_assessments": [
                    {
                        "criterion_id": f"c{i}",
                        "lower": pair[0],
                        "upper": pair[1],
                        "basis_refs": ["b"],
                        "reason": "示例冻结锚点",
                    }
                    for i, pair in enumerate(values)
                ],
            }
        )
    result = select(contract, candidates, assessments, elapsed_seconds=300)
    assert result.selected_id == "B"
    assert result.basis == "forecast_preference"
    assert (result.scores["A"].s_low, result.scores["A"].s_high) == (
        Fraction("62.5"),
        Fraction("68.75"),
    )
    assert (result.scores["B"].s_low, result.scores["B"].s_high) == (
        75,
        Fraction("81.25"),
    )
    assert any(
        row.candidate_id == "C" and row.reason == "time_plan_exceeded"
        for row in result.excluded
    )


def test_overlapping_scores_and_times_keep_incumbent_in_every_order():
    contract = parsed()
    candidates = [candidate_data(contract, "A"), candidate_data(contract, "B")]
    assessments = [assessment_data("A", 2, 4), assessment_data("B", 3, 4)]
    for order in permutations(candidates):
        result = select(contract, order, tuple(reversed(assessments)), incumbent_id="B")
        assert result.selected_id == "B"
        assert result.basis == "unresolved_tie_fallback"
        assert result.reason == "incumbent_tie_break"


def test_quality_overlap_can_prefer_time_without_claiming_quality_advantage():
    contract = parsed()
    result = select(
        contract,
        [
            candidate_data(contract, "A", (60, 90)),
            candidate_data(contract, "B", (20, 30)),
        ],
        [assessment_data("A", 2, 4), assessment_data("B", 3, 4)],
    )
    assert result.selected_id == "B"
    assert result.reason == "future_time_preference"
    assert result.basis == "unresolved_tie_fallback"
    assert "quality_unresolved" in result.limitations


@pytest.mark.parametrize("status", ["UNKNOWN", "FAIL"])
def test_authorization_unknown_or_failed_is_not_forecast_unknown(status):
    contract = parsed()
    candidate = candidate_data(contract)
    candidate["execution_preconditions"][0]["status"] = status
    result = select(contract, [candidate], [assessment_data(lower=0, upper=4)])
    assert result.selected_id is None
    assert any(row.reason == "precondition_not_passed" for row in result.excluded)


def test_floor_unknown_allows_safe_initial_choice_without_claiming_target():
    data = contract_data()
    data["criteria"][0]["critical_floor"] = 3
    contract = parsed(data)
    result = select(
        contract, [candidate_data(contract)], [assessment_data(lower=0, upper=4)]
    )
    assert result.selected_id == "A"
    assert result.scores["A"].floor_unknowns == ("coverage",)
    assert not result.scores["A"].targets_met


def test_known_floor_failure_excludes_even_the_only_cheap_route():
    data = contract_data()
    data["criteria"][0]["critical_floor"] = 3
    contract = parsed(data)
    result = select(
        contract,
        [candidate_data(contract, seconds=(0, 1))],
        [assessment_data(lower=2, upper=2)],
    )
    assert result.selected_id is None
    assert result.scores["A"].floor_failures == ("coverage",)


@pytest.mark.parametrize("location", ["candidate", "assessment"])
def test_known_required_conflict_cannot_be_compensated(location):
    contract = parsed()
    candidate, assessment = candidate_data(contract), assessment_data(lower=4, upper=4)
    (candidate if location == "candidate" else assessment)[
        "known_required_conflicts"
    ] = ["o0"]
    result = select(contract, [candidate], [assessment])
    assert result.selected_id is None
    assert any(row.reason == "known_required_conflict" for row in result.excluded)


@pytest.mark.parametrize("lower,upper", [(0, 4), (2, 3), (3, 3)])
def test_protected_bounds_cannot_regress_on_replacement(lower, upper):
    data = contract_data()
    data["criteria"][0]["protected"] = True
    contract = parsed(data)
    result = select(
        contract,
        [
            candidate_data(contract, "A", (60, 90)),
            candidate_data(contract, "B", (0, 1)),
        ],
        [assessment_data("A", 3, 4), assessment_data("B", lower, upper)],
        incumbent_id="A",
    )
    assert result.selected_id == "A"
    assert any(
        row.candidate_id == "B" and row.reason == "protected_regression"
        for row in result.excluded
    )


@pytest.mark.parametrize(
    "seconds,elapsed,point",
    [
        (None, 0, "before-execution"),
        ((1, 2), None, "before-execution"),
        ((1, 2), 0, "stale-point"),
    ],
)
def test_unknown_or_incomparable_time_is_not_zero(seconds, elapsed, point):
    contract = parsed()
    result = select(
        contract,
        [candidate_data(contract, seconds=seconds)],
        [assessment_data()],
        elapsed_seconds=elapsed,
        decision_point=point,
    )
    assert result.selected_id is None
    assert result.reason == "model_plan_not_feasible"


@pytest.mark.parametrize("elapsed", [True, 0.1, "-1", "NaN"])
def test_elapsed_rejects_inexact_or_invalid_input(elapsed):
    contract = parsed()
    with pytest.raises(ValidationError):
        select(
            contract,
            [candidate_data(contract)],
            [assessment_data()],
            elapsed_seconds=elapsed,
        )


def test_equal_time_boundary_is_admitted_and_later_check_expires():
    contract = parsed()
    candidate = candidate_data(contract, seconds=(3000, 3500))
    assert (
        select(
            contract, [candidate], [assessment_data()], elapsed_seconds=100
        ).selected_id
        == "A"
    )
    assert (
        select(
            contract, [candidate], [assessment_data()], elapsed_seconds=101
        ).selected_id
        is None
    )


def count_case(scope="contract", statuses=("supported", "supported", "unknown")):
    data = contract_data()
    data["criteria"][0]["count_scope"] = scope
    ids = [f"x{i}" for i in range(len(statuses))]
    if scope == "contract":
        data["criteria"][0]["item_ids"] = ids
    contract = parsed(data)
    candidate = candidate_data(contract)
    if scope == "candidate":
        candidate["candidate_items"] = [{"criterion_id": "coverage", "item_ids": ids}]
    assessment = assessment_data()
    assessment["criterion_assessments"][0]["items"] = [
        {"id": item_id, "status": status, "basis_refs": ["b"]}
        for item_id, status in zip(ids, statuses, strict=True)
    ]
    return contract, candidate, assessment


def test_count_partition_and_fraction_bounds_are_exact():
    contract, candidate, assessment = count_case(
        statuses=("supported",) * 6 + ("contradicted", "unknown")
    )
    score = select(contract, [candidate], [assessment]).scores["A"]
    count = score.counts[0]
    assert (count.total, count.supported, count.contradicted, count.unknown) == (
        8,
        6,
        1,
        1,
    )
    assert (count.coverage_low, count.coverage_high) == (Fraction(3, 4), Fraction(7, 8))


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "duplicate", "conflicting_duplicate"]
)
def test_counts_require_exactly_one_state_per_frozen_item(mutation):
    contract, candidate, assessment = count_case()
    items = assessment["criterion_assessments"][0]["items"]
    if mutation == "missing":
        items.pop()
    elif mutation == "extra":
        items.append({"id": "extra", "status": "unknown"})
    else:
        duplicate = deepcopy(items[0])
        if mutation == "conflicting_duplicate":
            duplicate["status"] = "contradicted"
        items.append(duplicate)
    with pytest.raises(ValueError):
        select(contract, [candidate], [assessment])


def test_candidate_absolute_counts_do_not_supply_a_coverage_denominator():
    contract, candidate, assessment = count_case("candidate", statuses=())
    count = select(contract, [candidate], [assessment]).scores["A"].counts[0]
    assert count.total == 0
    assert count.coverage_low is None and count.coverage_high is None


def test_known_contradiction_also_limits_possible_anchor_upper_bound():
    data = contract_data()
    data["criteria"][0].update(count_scope="contract", item_ids=["x"])
    data["criteria"][0]["anchors"][3]["required_supported_ids"] = ["x"]
    contract = parsed(data)
    assessment = assessment_data(lower=2, upper=4)
    assessment["criterion_assessments"][0]["items"] = [
        {"id": "x", "status": "contradicted", "basis_refs": ["b"]}
    ]
    with pytest.raises(ValueError, match="anchor"):
        select(contract, [candidate_data(contract)], [assessment])


def test_unknown_required_anchor_item_does_not_support_lower_bound():
    data = contract_data()
    data["criteria"][0].update(count_scope="contract", item_ids=["x"])
    data["criteria"][0]["anchors"][3]["required_supported_ids"] = ["x"]
    contract = parsed(data)
    assessment = assessment_data(lower=3, upper=4)
    assessment["criterion_assessments"][0]["items"] = [{"id": "x", "status": "unknown"}]
    with pytest.raises(ValueError, match="anchor"):
        select(contract, [candidate_data(contract)], [assessment])
    assessment["criterion_assessments"][0]["lower"] = 2
    assert (
        select(contract, [candidate_data(contract)], [assessment]).scores["A"].s_high
        == 100
    )


def test_contract_digest_ignores_order_and_decimal_tail_but_binds_semantics():
    from ai_sdlc.core.loop_simulation import stage_contract_digest

    data = contract_data(2)
    original = parsed(data)
    data["goal_contract"]["goals"].reverse()
    data["goal_contract"]["obligations"].reverse()
    data["criteria"][0]["goal_shares"].reverse()
    data["criteria"][0]["anchors"].reverse()
    data["time_plan"]["window_seconds"] = "3600.000"
    assert stage_contract_digest(parsed(data)) == stage_contract_digest(original)
    data["criteria"][0]["target_level"] = 4
    assert stage_contract_digest(parsed(data)) != stage_contract_digest(original)


def test_judge_input_binds_drafts_and_manifest_but_not_judgement():
    from ai_sdlc.core.loop_simulation import judge_input_digest

    contract = parsed()
    candidates = [candidate_data(contract, "B"), candidate_data(contract, "A")]
    manifest = {"spec.md": "a" * 64}
    digest = judge_input_digest(contract, candidates, manifest)
    assert judge_input_digest(contract, tuple(reversed(candidates)), manifest) == digest
    candidates[0]["artifact_sketch"]["key_structure"] = ["另一结构"]
    assert judge_input_digest(contract, candidates, manifest) != digest
    assert judge_input_digest(contract, candidates, {"spec.md": "b" * 64}) != digest


def test_score_does_not_round_under_decimal_context():
    data = contract_data(3)
    base = data["criteria"][0]
    data["criteria"] = []
    for i in range(3):
        row = deepcopy(base)
        row.update(id=f"c{i}", goal_shares=[{"goal_id": f"g{i}", "share": 1}])
        data["criteria"].append(row)
    contract = parsed(data)
    assessment = {
        "candidate_id": "A",
        "cost_check": "supported",
        "cost_reason": "完整范围已核对",
        "criterion_assessments": [
            {
                "criterion_id": f"c{i}",
                "lower": int(i == 2),
                "upper": int(i != 1),
                "basis_refs": ["b"],
                "reason": "固定等级",
            }
            for i in range(3)
        ],
    }
    with localcontext() as context:
        context.prec = 1
        score = select(contract, [candidate_data(contract)], [assessment]).scores["A"]
    assert (score.s_low, score.s_high, score.u_sim) == (
        Fraction(25, 3),
        Fraction(50, 3),
        Fraction(1, 3),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "draft_score",
        "assessment_override",
        "actual_origin",
        "bad_basis",
        "different_contract",
        "missing_assessment",
        "duplicate_assessment",
    ],
)
def test_boundary_rejects_unbound_or_authoritative_inputs(mutation):
    contract = parsed()
    candidate, assessment = candidate_data(contract), assessment_data()
    assessments = [assessment]
    if mutation == "draft_score":
        candidate["s_low"] = 100
    elif mutation == "assessment_override":
        assessment["selected_id"] = "A"
    elif mutation == "actual_origin":
        candidate["origin"] = "actual"
    elif mutation == "bad_basis":
        assessment["criterion_assessments"][0]["basis_refs"] = ["missing"]
    elif mutation == "different_contract":
        candidate["contract_digest"] = "0" * 64
    elif mutation == "missing_assessment":
        assessment["candidate_id"] = "B"
    else:
        assessments.append(deepcopy(assessment))
    with pytest.raises(ValueError):
        select(contract, [candidate], assessments)


def test_two_identical_mechanisms_do_not_create_competition():
    contract = parsed()
    candidates = [candidate_data(contract, "A"), candidate_data(contract, "B")]
    candidates[1]["mechanism"] = "  机制   A  "
    with pytest.raises(ValueError, match="mechanisms"):
        select(contract, candidates, [assessment_data("A"), assessment_data("B")])


def test_d1_does_not_execute_code_view_selection():
    data = contract_data()
    data["profile_id"] = "code-result-v1"
    contract = parsed(data)
    with pytest.raises(ValueError, match="initial-plan"):
        select(contract, [candidate_data(contract)], [assessment_data()])


@pytest.mark.parametrize("cost_check", ["incomplete", "unknown"])
def test_independent_cost_omission_blocks_an_optimistic_author_estimate(cost_check):
    contract = parsed()
    assessment = assessment_data()
    assessment["cost_check"] = cost_check
    assessment["cost_reason"] = "缺少必要验证成本，无法确认完整范围"
    result = select(contract, [candidate_data(contract, seconds=(1, 2))], [assessment])
    assert result.selected_id is None
    assert any(row.reason == f"cost_{cost_check}" for row in result.excluded)


def test_missing_independent_cost_check_is_unknown_not_supported():
    contract = parsed()
    assessment = assessment_data()
    assessment.pop("cost_check")
    assessment.pop("cost_reason")
    result = select(contract, [candidate_data(contract)], [assessment])
    assert result.selected_id is None
    assert any(row.reason == "cost_unknown" for row in result.excluded)


def test_full_goal_contract_size_keeps_all_actual_obligations():
    data = contract_data(128)
    data["goal_contract"]["obligations"] = [
        {**row, "id": f"{row['id']}-{j}", "weight_share": "0.0625"}
        for row in data["goal_contract"]["obligations"]
        for j in range(16)
    ]
    ids = [row["id"] for row in data["goal_contract"]["obligations"]]
    data["criteria"][0].update(count_scope="contract", item_ids=ids)
    contract = parsed(data)
    assessment = assessment_data()
    assessment["criterion_assessments"][0]["items"] = [
        {"id": item_id, "status": "supported", "basis_refs": ["b"]} for item_id in ids
    ]
    score = select(contract, [candidate_data(contract)], [assessment]).scores["A"]
    assert score.s_low == score.s_high == 75
    assert score.counts[0].total == 2048
    assert len(contract.goal_contract.obligations) == 2048


def test_raw_draft_payload_has_a_cumulative_limit():
    contract = parsed()
    candidate = candidate_data(contract)
    candidate["artifact_sketch"]["key_structure"] = ["约" * 4096] * 6
    with pytest.raises(ValueError, match="payload-too-large"):
        select(contract, [candidate], [assessment_data()])


def test_forecast_source_must_not_masquerade_as_unbound_project_fact():
    contract = parsed()
    candidate = candidate_data(contract)
    candidate["basis"][0]["kind"] = "project_fact"
    with pytest.raises(ValueError, match="fact-needs-source"):
        select(contract, [candidate], [assessment_data()])


def test_cost_check_supported_does_not_accept_a_blank_explanation():
    contract = parsed()
    assessment = assessment_data()
    assessment["cost_reason"] = "   "
    with pytest.raises(ValueError):
        select(contract, [candidate_data(contract)], [assessment])


def test_same_goal_split_preserves_score_without_improvement():
    original = parsed()
    data = contract_data()
    other = deepcopy(data["criteria"][0])
    data["criteria"][0]["goal_shares"][0]["share"] = "0.125"
    other["id"] = "diagnostic"
    other["goal_shares"][0]["share"] = "0.875"
    data["criteria"].append(other)
    split = parsed(data)
    assessment = assessment_data(lower=2, upper=3)
    extra = deepcopy(assessment["criterion_assessments"][0])
    extra["criterion_id"] = "diagnostic"
    original_score = select(original, [candidate_data(original)], [assessment]).scores[
        "A"
    ]
    assessment["criterion_assessments"].append(extra)
    split_score = select(split, [candidate_data(split)], [assessment]).scores["A"]
    assert (split_score.s_low, split_score.s_high, split_score.u_sim) == (
        original_score.s_low,
        original_score.s_high,
        original_score.u_sim,
    )

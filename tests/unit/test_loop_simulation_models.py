"""模拟合同保持精确、有限，并将草案与独立判断分开。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError


def contract_data(goal_count=1):
    goals = [
        {"id": f"g{i}", "source_ref": "spec", "weight": 1} for i in range(goal_count)
    ]
    return {
        "goal_contract": {
            "goals": goals,
            "obligations": [
                {
                    "id": f"o{i}",
                    "goal_id": goal["id"],
                    "statement": "拒绝越权请求",
                    "required": True,
                    "source_ref": "spec",
                    "oracle_kind": "rubric",
                    "pass_rule": "当前材料满足义务",
                    "fail_rule": "存在反例",
                    "unknown_rule": "材料不足",
                    "weight_share": 1,
                }
                for i, goal in enumerate(goals)
            ],
        },
        "criteria": [
            {
                "id": "coverage",
                "goal_shares": [{"goal_id": goal["id"], "share": 1} for goal in goals],
                "statement": "目标映射",
                "source_refs": ["spec"],
                "anchors": [
                    {"level": i, "statement": f"任务锚点 {i}"} for i in range(5)
                ],
            }
        ],
        "time_plan": {
            "window_seconds": 3600,
            "scope": "implementation-review-close",
            "basis_refs": ["spec"],
            "assumptions": ["已有环境可用"],
            "work_breakdown": ["模拟与评分", "实现", "必要验证和关闭"],
        },
        "source_refs": ["spec"],
    }


def test_one_criterion_can_cover_128_goals():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    contract = StageScoreContract.model_validate(contract_data(128))
    assert len(contract.criteria) == 1
    assert len(contract.criteria[0].goal_shares) == 128


@pytest.mark.parametrize("value", [True, 0.5, "NaN", "Infinity", "-0.1", "1.1"])
def test_goal_share_rejects_inexact_or_invalid_values(value):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["criteria"][0]["goal_shares"][0]["share"] = value
    with pytest.raises(ValidationError):
        StageScoreContract.model_validate(data)


def test_missing_goal_share_is_not_silently_renormalized():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data(2)
    data["criteria"][0]["goal_shares"].pop()
    with pytest.raises(ValidationError, match="share"):
        StageScoreContract.model_validate(data)


def test_same_goal_can_be_split_without_growing_its_weight():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["criteria"][0]["goal_shares"][0]["share"] = "0.25"
    other = deepcopy(data["criteria"][0])
    other["id"] = "boundary"
    other["goal_shares"][0]["share"] = "0.75"
    data["criteria"].append(other)
    assert len(StageScoreContract.model_validate(data).criteria) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_level", True),
        ("target_level", 3.0),
        ("target_level", "3"),
        ("critical_floor", -1),
        ("critical_floor", 5),
        ("protected", 1),
    ],
)
def test_criterion_rejects_coercion(field, value):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["criteria"][0][field] = value
    with pytest.raises(ValidationError):
        StageScoreContract.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_criterion",
        "duplicate_share",
        "missing_anchor",
        "duplicate_anchor",
        "unknown_goal",
        "extra_field",
        "unfrozen_anchor",
    ],
)
def test_contract_rejects_inconsistent_structure(mutation):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    criterion = data["criteria"][0]
    if mutation == "duplicate_criterion":
        data["criteria"].append(deepcopy(criterion))
    elif mutation == "duplicate_share":
        criterion["goal_shares"].append(deepcopy(criterion["goal_shares"][0]))
    elif mutation == "missing_anchor":
        criterion["anchors"].pop()
    elif mutation == "duplicate_anchor":
        criterion["anchors"][-1]["level"] = 3
    elif mutation == "unknown_goal":
        criterion["goal_shares"][0]["goal_id"] = "missing"
    elif mutation == "unfrozen_anchor":
        criterion["anchors"][3]["required_supported_ids"] = ["missing"]
    else:
        data["score_override"] = 100
    with pytest.raises(ValidationError):
        StageScoreContract.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("capability", "stage-simulation-v1"),
        ("profile_id", "requirement-analysis-v1"),
        ("schema_version", "2"),
        ("loop_type", "requirement"),
    ],
)
def test_d1_rejects_unreleased_identity(field, value):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data[field] = value
    with pytest.raises(ValidationError):
        StageScoreContract.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_batches", 3),
        ("max_candidates", 4),
        ("max_judges", 2),
        ("max_judges", True),
    ],
)
def test_policy_cannot_be_expanded_per_task(field, value):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["simulation_policy"] = {field: value}
    with pytest.raises(ValidationError):
        StageScoreContract.model_validate(data)


def test_code_profile_can_be_frozen_without_enabling_d2():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["profile_id"] = "code-result-v1"
    assert StageScoreContract.model_validate(data).profile_id == "code-result-v1"


def test_contract_count_cannot_use_zero_denominator():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["criteria"][0]["count_scope"] = "contract"
    with pytest.raises(ValidationError, match="frozen-items"):
        StageScoreContract.model_validate(data)


def test_inapplicable_item_must_not_keep_weight():
    from ai_sdlc.core.loop_simulation_models import StageScoreContract

    data = contract_data()
    data["criteria"][0].update(
        applicability="not_applicable", applicability_reason="目标不涉及"
    )
    with pytest.raises(ValidationError, match="no-share"):
        StageScoreContract.model_validate(data)


@pytest.mark.parametrize(
    "lower,upper", [(True, 3), (2.0, 3), (2, "4"), (-1, 3), (3, 2), (3, 5)]
)
def test_grade_intervals_are_strict_integers(lower, upper):
    from ai_sdlc.core.loop_simulation_models import CriterionAssessment

    with pytest.raises(ValidationError):
        CriterionAssessment(
            criterion_id="c",
            lower=lower,
            upper=upper,
            basis_refs=("b",),
            reason="锚点判断",
        )


def test_unknown_count_needs_no_invented_basis_but_supported_does():
    from ai_sdlc.core.loop_simulation_models import CountItem

    assert CountItem(id="x", status="unknown").basis_refs == ()
    with pytest.raises(ValidationError, match="requires-basis"):
        CountItem(id="x", status="supported")


def continuation_data():
    return {
        "criterion_ids": ["coverage"],
        "hypothesis": "将授权分支改为同一状态表，消除已识别的分支遗漏",
        "future_cost_estimate": {
            "lower_seconds": 120,
            "upper_seconds": 180,
            "scope": "implementation-review-close",
            "basis_refs": ["b"],
            "assumptions": ["包括再模拟、保留路线实现、必要验证和关闭"],
        },
    }


def test_independent_judgement_can_carry_a_bounded_initial_search_proposal():
    from ai_sdlc.core.loop_simulation_models import (
        InitialSearchContinuation,
        SimulationJudgement,
    )
    from tests.unit.test_loop_simulation import assessment_data

    proposal = InitialSearchContinuation.model_validate(continuation_data())
    judgement = SimulationJudgement(
        judge_input_digest="a" * 64,
        assessments=(assessment_data(),),
        initial_search_continuation=proposal,
    )
    assert judgement.initial_search_continuation.criterion_ids == ("coverage",)
    assert (
        judgement.initial_search_continuation.future_cost_estimate.upper_seconds == 180
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_ids",
        "duplicate_ids",
        "too_many_ids",
        "blank_hypothesis",
        "missing_estimate",
        "negative_estimate",
        "inexact_estimate",
        "duplicate_basis",
        "extra_formula",
    ],
)
def test_initial_search_continuation_rejects_unbounded_or_ambiguous_input(mutation):
    from ai_sdlc.core.loop_simulation_models import InitialSearchContinuation

    data = continuation_data()
    if mutation == "empty_ids":
        data["criterion_ids"] = []
    elif mutation == "duplicate_ids":
        data["criterion_ids"] = ["coverage", "coverage"]
    elif mutation == "too_many_ids":
        data["criterion_ids"] = [f"c{i}" for i in range(17)]
    elif mutation == "blank_hypothesis":
        data["hypothesis"] = "  "
    elif mutation == "missing_estimate":
        data.pop("future_cost_estimate")
    elif mutation == "negative_estimate":
        data["future_cost_estimate"]["lower_seconds"] = -1
    elif mutation == "inexact_estimate":
        data["future_cost_estimate"]["upper_seconds"] = 180.0
    elif mutation == "duplicate_basis":
        data["future_cost_estimate"]["basis_refs"] = ["b", "b"]
    else:
        data["formula"] = "score / seconds"
    with pytest.raises(ValidationError):
        InitialSearchContinuation.model_validate(data)

"""候选路线只作有依据的比较；未知和兜底不能变成质量优势。"""

import json
from decimal import Decimal, localcontext
from itertools import permutations

import pytest

from ai_sdlc.core.loop_decision import (
    contract_digest,
    route_contract_digest,
    select_routes,
)
from ai_sdlc.core.loop_decision_models import (
    ExecutionPrecondition,
    Goal,
    GoalContract,
    MeasurementConditions,
    MetricEstimate,
    NativeMetric,
    Obligation,
    ObligationChange,
    RouteCandidate,
    RouteSelectionContract,
    TimeEstimate,
)


def goal_contract():
    return GoalContract(
        goals=(Goal(id="g", source_ref="spec:goal"),),
        obligations=(
            Obligation(
                id="o",
                goal_id="g",
                statement="固定任务集满足验收",
                required=True,
                source_ref="spec:obligation",
                oracle_kind="deterministic",
                pass_rule="固定反例全部满足",
                fail_rule="存在可复现失败",
                unknown_rule="缺少观测",
                weight_share=1,
            ),
        ),
    )


def conditions():
    return MeasurementConditions(
        unit="percentage-point",
        population="frozen-50-cases",
        procedure="same-procedure-v1",
        environment="same-environment-v1",
    )


def metric(metric_id="quality", *, direction="maximize", threshold="1"):
    return NativeMetric(
        id=metric_id,
        goal_id="g",
        conditions=conditions(),
        direction=direction,
        material_difference=threshold,
        source_refs=("spec:meaningful-difference",),
    )


def contract(*, metrics=None, baseline=None):
    items = (metric(),) if metrics is None else tuple(metrics)
    return RouteSelectionContract(
        goal_contract=goal_contract(),
        metrics=items,
        ordered_metric_ids=tuple(item.id for item in items),
        decision_point="decision:before-candidate-execution",
        time_scope="candidate-through-required-review-and-close",
        source_refs=("spec:comparison",),
        baseline_id=baseline,
    )


def estimate(item, lower, upper=None, *, kind="forecast"):
    return MetricEstimate(
        id=item.id,
        kind=kind,
        conditions=item.conditions,
        lower=lower,
        upper=lower if upper is None else upper,
        evidence_refs=("probe:frozen-input",),
        reason="同一固定任务集的测量或有依据预测",
        assumptions=("来源条件在执行时保持",),
    )


def candidate(comparison, route_id, values, *, kind="forecast", seconds=(1, 2)):
    estimates = tuple(
        estimate(item, *value, kind=kind)
        for item, value in zip(comparison.metrics, values, strict=True)
    )
    return RouteCandidate(
        id=route_id,
        contract_digest=route_contract_digest(comparison),
        mechanism=f"独立路线机制 {route_id}",
        changed_scope=("current-component",),
        evidence_refs=("spec:route-basis",),
        assumptions=("本次限定范围不扩大",),
        execution_preconditions=tuple(
            ExecutionPrecondition(
                kind=kind,
                status="PASS",
                reason="当前明确授权且已复核",
                evidence_refs=(f"review:{kind}",),
            )
            for kind in ("authorization", "safety", "mandatory_constraints")
        ),
        metric_estimates=estimates,
        cost_decision_point=comparison.decision_point,
        future_cost_estimate=(
            TimeEstimate(
                lower_seconds=seconds[0],
                upper_seconds=seconds[1],
                scope=comparison.time_scope,
                basis_refs=("observed:similar-scope",),
                assumptions=("包含必要验证和收尾，环境保持不变",),
            )
            if seconds is not None
            else None
        ),
    )


def changed(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


def test_quality_overlap_uses_cost_fallback_without_claiming_best_quality():
    comparison = contract(baseline="A")
    routes = (
        candidate(comparison, "A", [("82", "86")], seconds=(600, 900)),
        candidate(comparison, "B", [("88", "92")], seconds=(1500, 2100)),
        candidate(comparison, "C", [("88", "93")], seconds=(3600, 5400)),
    )
    result = select_routes(comparison, routes)
    assert result.selected_id == "B"
    assert result.quality_survivors == ("B", "C")
    assert result.remaining_ids == ("B",)
    assert result.basis == "unresolved_tie_fallback"
    assert result.reason == "future_time_preference"
    assert {(row.candidate_id, row.reason) for row in result.excluded} == {
        ("A", "quality_preference"),
        ("C", "future_time_preference"),
    }


# 七组性质的第一组：执行前提是安全底线，不是模型质量预测。
@pytest.mark.parametrize("kind", ["authorization", "safety", "mandatory_constraints"])
@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
def test_nonpassing_precondition_excludes_route_even_when_quality_is_higher(
    kind, status
):
    comparison = contract()
    unsafe = candidate(comparison, "A", [(100,)], seconds=(0, 0))
    unsafe = changed(
        unsafe,
        execution_preconditions=tuple(
            changed(row, status=status) if row.kind == kind else row
            for row in unsafe.execution_preconditions
        ),
    )
    safe = candidate(comparison, "B", [(1,)], seconds=(100, 100))
    result = select_routes(comparison, (unsafe, safe))
    assert result.selected_id == "B"
    assert result.eligible_ids == result.quality_survivors == ("B",)
    assert result.basis == "unresolved_tie_fallback"
    assert result.reason == "single_feasible_route"
    assert any(
        row.candidate_id == "A" and row.reason == "precondition_not_passed"
        for row in result.excluded
    )


def test_no_safe_route_does_not_force_a_selection_or_permission():
    comparison = contract()
    route = candidate(comparison, "A", [(100,)])
    route = changed(
        route,
        execution_preconditions=tuple(
            changed(row, status="UNKNOWN", evidence_refs=())
            for row in route.execution_preconditions
        ),
    )
    result = select_routes(comparison, (route,))
    assert result.selected_id is result.basis is None
    assert result.eligible_ids == result.quality_survivors == result.remaining_ids == ()
    assert result.reason == "no_safe_route"


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_precondition_pass_and_fail_require_evidence(status):
    with pytest.raises(ValueError):
        ExecutionPrecondition(kind="safety", status=status, reason="未经依据不能断言")
    unknown = ExecutionPrecondition(kind="safety", status="UNKNOWN", reason="待核实")
    assert unknown.evidence_refs == ()


# 第二组：区间、方向和业务优先顺序采用集合筛选，不作任意两两排序。
@pytest.mark.parametrize(
    "direction,values", [("maximize", (90, 80)), ("minimize", (10, 20))]
)
@pytest.mark.parametrize(
    "kind,basis",
    [("actual", "evidence_advantage"), ("forecast", "forecast_preference")],
)
def test_clear_dominance_needs_quality_direction_and_no_higher_complete_cost(
    direction, values, kind, basis
):
    comparison = contract(metrics=(metric(direction=direction),))
    routes = (
        candidate(comparison, "A", [(values[0],)], kind=kind, seconds=(10, 15)),
        candidate(comparison, "B", [(values[1],)], kind=kind, seconds=(20, 25)),
    )
    result = select_routes(comparison, routes)
    assert result.selected_id == "A"
    assert result.basis == basis
    assert result.reason == "quality_preference"
    assert any(
        row.candidate_id == "B" and row.reason == "dominated" and row.witness_id == "A"
        for row in result.excluded
    )


@pytest.mark.parametrize(
    "direction,values", [("maximize", (10, 9)), ("minimize", (9, 10))]
)
def test_difference_equal_to_threshold_is_not_a_quality_advantage(direction, values):
    comparison = contract(metrics=(metric(direction=direction),), baseline="B")
    routes = tuple(
        candidate(comparison, route_id, [(value,)], kind="actual", seconds=(1, 1))
        for route_id, value in zip(("A", "B"), values, strict=True)
    )
    result = select_routes(comparison, routes)
    assert result.quality_survivors == result.remaining_ids == ("A", "B")
    assert result.selected_id == "B"
    assert result.basis == "unresolved_tie_fallback"
    assert result.excluded == ()


def test_overlapping_quality_intervals_do_not_prove_dominance():
    comparison = contract(baseline="B")
    routes = (
        candidate(comparison, "A", [(80, 100)], seconds=(1, 1)),
        candidate(comparison, "B", [(90, 95)], seconds=(1, 1)),
    )
    result = select_routes(comparison, routes)
    assert result.quality_survivors == ("A", "B")
    assert result.selected_id == "B"
    assert result.excluded == ()


def test_quality_advantage_is_not_dominance_without_comparable_cost():
    comparison = contract()
    routes = (
        candidate(comparison, "A", [(90,)], kind="actual", seconds=None),
        candidate(comparison, "B", [(80,)], kind="actual", seconds=(1, 2)),
    )
    result = select_routes(comparison, routes)
    assert result.selected_id == "A"
    assert result.basis == "evidence_advantage"
    assert {row.reason for row in result.excluded} == {"quality_preference"}


def test_frozen_priority_beats_lower_priority_and_cost_without_scalar_score():
    comparison = contract(
        metrics=(metric("critical", threshold=0), metric("minor", threshold=0))
    )
    routes = (
        candidate(comparison, "A", [(10,), (1,)], kind="actual", seconds=(100, 200)),
        candidate(comparison, "B", [(9,), (1000,)], kind="actual", seconds=(1, 2)),
    )
    result = select_routes(comparison, routes)
    assert result.selected_id == "A"
    assert result.basis == "evidence_advantage"
    assert result.excluded[0].metric_id == "critical"


# 第三组：预测、事实、UNKNOWN 与测量条件保持可追溯，不能互相冒充。
@pytest.mark.parametrize(
    "incomparable", ["unknown", "mixed_kind", "conditions", "missing_threshold"]
)
def test_incomparable_priority_stops_all_lower_quality_dimensions(incomparable):
    comparison = contract(
        metrics=(
            metric(
                "critical", threshold=None if incomparable == "missing_threshold" else 0
            ),
            metric("minor", threshold=0),
        ),
        baseline="B",
    )
    a = candidate(comparison, "A", [(100,), (100,)], kind="actual", seconds=None)
    b = candidate(comparison, "B", [(0,), (0,)], kind="actual", seconds=None)
    first = b.metric_estimates[0]
    if incomparable == "unknown":
        first = changed(
            first,
            kind="unknown",
            lower=None,
            upper=None,
            evidence_refs=(),
            assumptions=(),
        )
    elif incomparable == "mixed_kind":
        first = changed(first, kind="forecast")
    elif incomparable == "conditions":
        first = changed(
            first, conditions=changed(first.conditions, environment="different-host")
        )
    b = changed(b, metric_estimates=(first, b.metric_estimates[1]))
    result = select_routes(comparison, (a, b))
    assert result.quality_survivors == ("A", "B")
    assert result.selected_id == "B"
    assert result.basis == "unresolved_tie_fallback"
    assert result.limitations
    assert result.excluded == ()


def test_later_actual_comparison_cannot_erase_forecast_dependency():
    comparison = contract(
        metrics=(metric("first", threshold=0), metric("second", threshold=0))
    )
    routes = []
    for route_id, values in (("A", (90, 50)), ("B", (90, 10)), ("C", (80, 100))):
        route = candidate(
            comparison, route_id, [(values[0],), (values[1],)], seconds=None
        )
        routes.append(
            changed(
                route,
                metric_estimates=(
                    route.metric_estimates[0],
                    changed(route.metric_estimates[1], kind="actual"),
                ),
            )
        )
    result = select_routes(comparison, tuple(routes))
    assert result.selected_id == "A"
    assert result.basis == "forecast_preference"
    assert {row.metric_id for row in result.excluded} == {"first", "second"}


@pytest.mark.parametrize("kind", ["actual", "forecast"])
@pytest.mark.parametrize("missing", ["lower", "upper", "evidence_refs"])
def test_numeric_estimates_require_complete_interval_and_evidence(kind, missing):
    row = estimate(metric(), 1, 2, kind=kind)
    with pytest.raises(ValueError):
        changed(row, **{missing: () if missing == "evidence_refs" else None})


def test_forecast_requires_assumptions_and_unknown_forbids_numeric_claims():
    row = estimate(metric(), 1, 2)
    with pytest.raises(ValueError):
        changed(row, assumptions=())
    with pytest.raises(ValueError):
        changed(row, kind="unknown")
    unknown = changed(
        row, kind="unknown", lower=None, upper=None, evidence_refs=(), assumptions=()
    )
    assert unknown.lower is unknown.upper is None
    assert not hasattr(unknown, "artifact_digest")
    assert not hasattr(unknown, "q")


# 第四组：成本只能在同一起点和完整交付口径下打破质量并列。
@pytest.mark.parametrize("mismatch", ["missing", "decision_point", "scope"])
def test_one_incomparable_time_disables_cost_elimination_for_whole_set(mismatch):
    comparison = contract(baseline="B")
    routes = [
        candidate(comparison, route_id, [(10,)], seconds=time)
        for route_id, time in (("A", (1, 2)), ("B", (50, 60)), ("C", (100, 120)))
    ]
    if mismatch == "missing":
        routes[2] = changed(routes[2], future_cost_estimate=None)
    elif mismatch == "decision_point":
        routes[2] = changed(routes[2], cost_decision_point="decision:later")
    else:
        routes[2] = changed(
            routes[2],
            future_cost_estimate=changed(
                routes[2].future_cost_estimate, scope="implementation-only"
            ),
        )
    result = select_routes(comparison, tuple(routes))
    assert result.quality_survivors == result.remaining_ids == ("A", "B", "C")
    assert result.selected_id == "B"
    assert result.reason == "baseline_tie_break"
    assert result.basis == "unresolved_tie_fallback"
    assert result.excluded == ()
    assert result.limitations


@pytest.mark.parametrize("second_time", [(5, 8), (3, 8)])
def test_touching_or_overlapping_time_keeps_quality_tie(second_time):
    comparison = contract(baseline="B")
    routes = (
        candidate(comparison, "A", [(10,)], seconds=(1, 5)),
        candidate(comparison, "B", [(10,)], seconds=second_time),
    )
    result = select_routes(comparison, routes)
    assert result.remaining_ids == ("A", "B")
    assert result.selected_id == "B"
    assert result.reason == "baseline_tie_break"
    assert result.basis == "unresolved_tie_fallback"


def test_empty_metrics_and_missing_time_are_not_perfect_quality_or_zero_cost():
    comparison = contract(metrics=())
    result = select_routes(
        comparison,
        (
            candidate(comparison, "B", [], seconds=None),
            candidate(comparison, "A", [], seconds=None),
        ),
    )
    assert result.selected_id == "A"
    assert result.quality_survivors == ("A", "B")
    assert result.basis == "unresolved_tie_fallback"
    assert result.reason == "stable_id_tie_break"
    assert result.excluded == ()


# 第五组：原集合支配、筛选和见证者均不受候选输入排列影响。
@pytest.mark.parametrize("mode", ["dominance", "quality", "cost", "tie"])
def test_every_candidate_permutation_preserves_entire_selection(mode):
    comparison = contract(baseline="C")
    quality = {
        "dominance": (90, 80, 70),
        "quality": (90, 80, 70),
        "cost": (90, 90, 90),
        "tie": (90, 90, 90),
    }[mode]
    costs = ((1, 1), (2, 2), (3, 3)) if mode in ("dominance", "cost") else (None,) * 3
    routes = tuple(
        candidate(comparison, route_id, [(value,)], seconds=cost)
        for route_id, value, cost in zip(("A", "B", "C"), quality, costs, strict=True)
    )
    expected = select_routes(comparison, routes)
    for order in permutations(routes):
        assert select_routes(comparison, order) == expected
    assert expected.selected_id == ("C" if mode == "tie" else "A")


# 第六组：精确输入和语义摘要不能受小数拼写、环境精度或集合排列影响。
def test_route_digest_canonicalizes_sets_but_preserves_preference_order():
    comparison = contract(
        metrics=(metric("first", threshold="0.10"), metric("second", threshold="0.20"))
    )
    comparison = changed(comparison, source_refs=("spec:a", "spec:b"))
    original_goal_digest = contract_digest(comparison.goal_contract)
    reordered = changed(
        comparison,
        metrics=tuple(
            changed(
                item, material_difference=Decimal(str(item.material_difference) + "0")
            )
            for item in reversed(comparison.metrics)
        ),
        source_refs=tuple(reversed(comparison.source_refs)),
    )
    assert route_contract_digest(reordered) == route_contract_digest(comparison)
    assert contract_digest(reordered.goal_contract) == original_goal_digest
    assert route_contract_digest(
        changed(comparison, ordered_metric_ids=("second", "first"))
    ) != route_contract_digest(comparison)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision_point", "another-start"),
        ("time_scope", "another-complete-scope"),
        ("source_refs", ("spec:other",)),
        ("baseline_id", "A"),
    ],
)
def test_route_digest_binds_all_comparison_semantics(field, value):
    comparison = contract()
    assert route_contract_digest(
        changed(comparison, **{field: value})
    ) != route_contract_digest(comparison)


@pytest.mark.parametrize(
    "field,value",
    [
        ("direction", "minimize"),
        ("material_difference", "0.1"),
        ("source_refs", ("spec:other",)),
        ("conditions", None),
    ],
)
def test_route_digest_binds_native_metric_meaning(field, value):
    comparison = contract()
    if field == "conditions":
        value = changed(
            comparison.metrics[0].conditions, population="different-fixed-cases"
        )
    updated = changed(
        comparison, metrics=(changed(comparison.metrics[0], **{field: value}),)
    )
    assert route_contract_digest(updated) != route_contract_digest(comparison)


@pytest.mark.parametrize("precision", [2, 28, 80])
def test_selection_and_digest_are_independent_of_decimal_context(precision):
    comparison = contract(metrics=(metric(threshold="0.000000001"),))
    routes = (
        candidate(
            comparison, "A", [("123456789.123456788",)], kind="actual", seconds=None
        ),
        candidate(
            comparison, "B", [("123456789.123456786",)], kind="actual", seconds=None
        ),
    )
    digest = route_contract_digest(comparison)
    expected = select_routes(comparison, routes)
    with localcontext() as context:
        context.prec = precision
        assert route_contract_digest(comparison) == digest
        assert select_routes(comparison, routes) == expected
    assert expected.selected_id == "A"
    assert expected.basis == "evidence_advantage"


@pytest.mark.parametrize("field", ["material_difference", "lower", "upper"])
@pytest.mark.parametrize(
    "value",
    [True, False, 0.1, 1.0, "NaN", "Infinity", "-Infinity", "0.1234567891", "1e1000"],
)
def test_new_numeric_fields_reject_inexact_or_unbounded_values(field, value):
    sample = metric() if field == "material_difference" else estimate(metric(), 0, 10)
    with pytest.raises(ValueError):
        changed(sample, **{field: value})


@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize("field", ["material_difference", "lower", "upper"])
def test_new_numeric_json_fields_reject_decimal_tokens_but_accept_exact_strings(
    strict, field
):
    sample = metric() if field == "material_difference" else estimate(metric(), 0, 10)
    raw = sample.model_dump(mode="json")
    raw[field] = "raw-number-token"
    payload = json.dumps(raw)
    for token in ("true", "0.1", "1.0", "1e0", "NaN"):
        with pytest.raises(ValueError):
            type(sample).model_validate_json(
                payload.replace('"raw-number-token"', token), strict=strict
            )
    for token in ('"0.1"', "1"):
        restored = type(sample).model_validate_json(
            payload.replace('"raw-number-token"', token), strict=strict
        )
        assert getattr(restored, field) == Decimal(token.strip('"'))


def test_negative_metric_values_are_valid_but_negative_difference_and_reversed_interval_are_not():
    comparison = contract(metrics=(metric(threshold=0),))
    routes = (
        candidate(comparison, "A", [(-2, -1)], kind="actual", seconds=None),
        candidate(comparison, "B", [(-5, -3)], kind="actual", seconds=None),
    )
    assert select_routes(comparison, routes).selected_id == "A"
    with pytest.raises(ValueError):
        metric(threshold=-1)
    with pytest.raises(ValueError):
        estimate(metric(), 2, 1)


@pytest.mark.parametrize("strict", [None, True])
def test_every_new_value_object_preserves_native_json_roundtrip_and_is_frozen(strict):
    comparison = contract()
    route = candidate(comparison, "A", [("0.1", "0.2")])
    change = ObligationChange(id="o", expectation="有限检索可减少此义务的未知")
    route = changed(route, expected_obligation_changes=(change,))
    selected = select_routes(comparison, (route,))
    samples = (
        conditions(),
        comparison.metrics[0],
        comparison,
        route.metric_estimates[0],
        route.execution_preconditions[0],
        change,
        route,
        selected,
    )
    for sample in samples:
        assert (
            type(sample).model_validate_json(sample.model_dump_json(), strict=strict)
            == sample
        )
        key = next(iter(type(sample).model_fields))
        with pytest.raises(ValueError):
            setattr(sample, key, getattr(sample, key))
        with pytest.raises(ValueError):
            type(sample).model_validate(
                {**sample.model_dump(), "invented_permission": True}
            )


# 第七组：交叉引用、伪造实例与输入规模均在纯函数入口重新校验。
@pytest.mark.parametrize(
    "malformed",
    [
        "duplicate_metric",
        "orphan_goal",
        "missing_order",
        "duplicate_order",
        "extra_order",
        "missing_source",
        "duplicate_source",
        "empty_point",
        "empty_scope",
    ],
)
def test_comparison_contract_rejects_invalid_coverage_or_sources(malformed):
    comparison = contract()
    raw = comparison.model_dump()
    if malformed == "duplicate_metric":
        raw["metrics"] += (raw["metrics"][0],)
    elif malformed == "orphan_goal":
        raw["metrics"][0]["goal_id"] = "absent"
    elif malformed == "missing_order":
        raw["ordered_metric_ids"] = ()
    elif malformed == "duplicate_order":
        raw["ordered_metric_ids"] *= 2
    elif malformed == "extra_order":
        raw["ordered_metric_ids"] += ("absent",)
    elif malformed == "missing_source":
        raw["source_refs"] = ()
    elif malformed == "duplicate_source":
        raw["source_refs"] *= 2
    elif malformed == "empty_point":
        raw["decision_point"] = " "
    else:
        raw["time_scope"] = " "
    with pytest.raises(ValueError):
        RouteSelectionContract.model_validate(raw)


@pytest.mark.parametrize(
    "malformed",
    [
        "contract_binding",
        "missing_estimate",
        "extra_estimate",
        "duplicate_estimate",
        "orphan_obligation",
        "duplicate_obligation",
        "missing_precondition",
        "duplicate_precondition",
        "missing_evidence",
        "duplicate_evidence",
        "missing_assumption",
        "missing_scope",
    ],
)
def test_candidate_boundary_rejects_invalid_references_and_required_material(malformed):
    comparison = contract()
    route = candidate(comparison, "A", [(1,)])
    raw = route.model_dump()
    if malformed == "contract_binding":
        raw["contract_digest"] = "0" * 64
    elif malformed == "missing_estimate":
        raw["metric_estimates"] = ()
    elif malformed == "extra_estimate":
        raw["metric_estimates"] += ({**raw["metric_estimates"][0], "id": "absent"},)
    elif malformed == "duplicate_estimate":
        raw["metric_estimates"] *= 2
    elif malformed == "orphan_obligation":
        raw["expected_obligation_changes"] = (
            {"id": "absent", "expectation": "未知引用"},
        )
    elif malformed == "duplicate_obligation":
        raw["expected_obligation_changes"] = (
            {"id": "o", "expectation": "同一义务"},
        ) * 2
    elif malformed == "missing_precondition":
        raw["execution_preconditions"] = raw["execution_preconditions"][:-1]
    elif malformed == "duplicate_precondition":
        raw["execution_preconditions"] = (raw["execution_preconditions"][0],) * 3
    elif malformed == "missing_evidence":
        raw["evidence_refs"] = ()
    elif malformed == "duplicate_evidence":
        raw["evidence_refs"] *= 2
    elif malformed == "missing_assumption":
        raw["assumptions"] = ()
    else:
        raw["changed_scope"] = ()
    with pytest.raises(ValueError):
        select_routes(comparison, (RouteCandidate.model_validate(raw),))


@pytest.mark.parametrize("count", [0, 4])
def test_selection_rejects_candidate_count_outside_one_to_three(count):
    comparison = contract()
    with pytest.raises(ValueError):
        select_routes(
            comparison,
            tuple(candidate(comparison, str(i), [(i,)]) for i in range(count)),
        )


@pytest.mark.parametrize("duplicate", ["id", "mechanism"])
def test_duplicate_id_or_whitespace_normalized_mechanism_is_not_an_alternative(
    duplicate,
):
    comparison = contract()
    a = candidate(comparison, "A", [(1,)])
    b = candidate(comparison, "B", [(2,)])
    b = changed(
        b,
        **({"id": "A"} if duplicate == "id" else {"mechanism": "  独立路线机制   A  "}),
    )
    with pytest.raises(ValueError):
        select_routes(comparison, (a, b))


def test_baseline_must_reference_present_candidate_but_cannot_rescue_loser():
    comparison = contract(baseline="B")
    a = candidate(comparison, "A", [(100,)], seconds=None)
    with pytest.raises(ValueError):
        select_routes(comparison, (a,))
    b = candidate(comparison, "B", [(1,)], seconds=None)
    result = select_routes(comparison, (a, b))
    assert result.selected_id == "A"
    assert "B" not in result.remaining_ids


@pytest.mark.parametrize(
    "forgery", ["contract", "candidate", "nested_estimate", "nested_precondition"]
)
def test_unsafe_model_copy_and_construct_cannot_bypass_select_boundary(forgery):
    comparison = contract()
    route = candidate(comparison, "A", [(1,)])
    if forgery == "contract":
        comparison = comparison.model_copy(update={"ordered_metric_ids": ()})
    elif forgery == "candidate":
        route = RouteCandidate.model_construct(
            **{**dict(route), "execution_preconditions": ()}
        )
    elif forgery == "nested_estimate":
        route = route.model_copy(
            update={
                "metric_estimates": (
                    route.metric_estimates[0].model_copy(update={"lower": 0.1}),
                )
            }
        )
    else:
        rows = route.execution_preconditions
        route = route.model_copy(
            update={
                "execution_preconditions": (
                    rows[0].model_copy(update={"evidence_refs": ()}),
                    *rows[1:],
                )
            }
        )
    with pytest.raises(ValueError):
        select_routes(comparison, (route,))


def test_seventeenth_metric_is_rejected():
    with pytest.raises(ValueError):
        contract(metrics=tuple(metric(f"m{i}") for i in range(17)))


def test_maximum_legal_selection_strict_json_and_every_permutation():
    goals = tuple(Goal(id=f"g{i}", source_ref="spec:maximum") for i in range(128))
    prototype = goal_contract().obligations[0]
    obligations = tuple(
        changed(prototype, id=f"o{i}-{j}", goal_id=goal.id, weight_share="0.0625")
        for i, goal in enumerate(goals)
        for j in range(16)
    )
    comparison = contract(
        metrics=tuple(metric(f"m{i}", threshold=0) for i in range(16))
    )
    comparison = changed(
        comparison,
        goal_contract=GoalContract(goals=goals, obligations=obligations),
        metrics=tuple(
            changed(item, goal_id=f"g{i}") for i, item in enumerate(comparison.metrics)
        ),
    )
    routes = tuple(
        candidate(
            comparison,
            route_id,
            [(value,)] * 16,
            kind="actual",
            seconds=(4 - value, 4 - value),
        )
        for route_id, value in (("A", 1), ("B", 2), ("C", 3))
    )
    restored = RouteSelectionContract.model_validate_json(
        comparison.model_dump_json(), strict=True
    )
    restored_routes = tuple(
        RouteCandidate.model_validate_json(route.model_dump_json(), strict=True)
        for route in routes
    )
    assert restored == comparison
    assert restored_routes == routes
    assert len(restored.goal_contract.goals) == 128
    assert len(restored.goal_contract.obligations) == 2048
    assert len(restored.metrics) == 16
    expected = select_routes(restored, restored_routes)
    for order in permutations(restored_routes):
        assert select_routes(restored, order) == expected
    assert expected.selected_id == "C"
    assert expected.basis == "evidence_advantage"
    payload_bytes = len(comparison.model_dump_json().encode("utf-8")) + sum(
        len(route.model_dump_json().encode("utf-8")) for route in routes
    )
    print(
        f"routes=3 metrics=16 goals=128 obligations=2048 payload_utf8_bytes={payload_bytes}"
    )


def test_equal_cost_does_not_turn_quality_tradeoff_into_dominance():
    comparison = contract(
        metrics=(metric("first", threshold=0), metric("second", threshold=0))
    )
    routes = (
        candidate(comparison, "A", [(10,), (1,)], seconds=(1, 1)),
        candidate(comparison, "B", [(9,), (100,)], seconds=(1, 1)),
    )
    result = select_routes(comparison, routes)
    assert result.selected_id == "A"
    assert {row.reason for row in result.excluded} == {"quality_preference"}
    assert result.excluded[0].metric_id == "first"


def test_unknown_effect_of_excluded_unsafe_route_does_not_pollute_safe_comparison():
    comparison = contract()
    a = candidate(comparison, "A", [(90,)], kind="actual", seconds=None)
    b = candidate(comparison, "B", [(80,)], kind="actual", seconds=None)
    unsafe = candidate(comparison, "C", [(100,)], seconds=None)
    unsafe = changed(
        unsafe,
        metric_estimates=(
            changed(
                unsafe.metric_estimates[0],
                kind="unknown",
                lower=None,
                upper=None,
            ),
        ),
        execution_preconditions=tuple(
            changed(row, status="UNKNOWN") if row.kind == "safety" else row
            for row in unsafe.execution_preconditions
        ),
    )
    result = select_routes(comparison, (a, b, unsafe))
    assert result.eligible_ids == ("A", "B")
    assert result.selected_id == "A"
    assert result.basis == "evidence_advantage"


def test_metric_sources_and_candidate_collections_reordering_does_not_change_selection():
    comparison = contract(
        metrics=(
            changed(metric("first"), source_refs=("spec:a", "spec:b")),
            metric("second"),
        )
    )
    routes = tuple(
        candidate(comparison, route_id, [(value,), (value,)], seconds=None)
        for route_id, value in (("A", 90), ("B", 80), ("C", 70))
    )
    reordered = changed(
        comparison,
        metrics=tuple(
            changed(item, source_refs=tuple(reversed(item.source_refs)))
            for item in reversed(comparison.metrics)
        ),
    )
    reordered_routes = tuple(
        changed(
            route,
            metric_estimates=tuple(reversed(route.metric_estimates)),
            execution_preconditions=tuple(reversed(route.execution_preconditions)),
        )
        for route in reversed(routes)
    )
    assert route_contract_digest(reordered) == route_contract_digest(comparison)
    assert select_routes(reordered, reordered_routes) == select_routes(
        comparison, routes
    )


@pytest.mark.parametrize("kind", ["metric", "estimate", "precondition"])
def test_duplicate_nested_sources_or_evidence_are_rejected(kind):
    if kind == "metric":
        sample, field = metric(), "source_refs"
    elif kind == "estimate":
        sample, field = estimate(metric(), 1, 2), "evidence_refs"
    else:
        sample = ExecutionPrecondition(
            kind="safety", status="PASS", reason="已审查", evidence_refs=("review:1",)
        )
        field = "evidence_refs"
    with pytest.raises(ValueError):
        changed(sample, **{field: getattr(sample, field) * 2})


@pytest.mark.parametrize("field", ["unit", "population", "procedure", "environment"])
def test_each_measurement_condition_is_required_for_quality_comparability(field):
    comparison = contract(baseline="B")
    a = candidate(comparison, "A", [(100,)], seconds=None)
    b = candidate(comparison, "B", [(1,)], seconds=None)
    b = changed(
        b,
        metric_estimates=(
            changed(
                b.metric_estimates[0],
                conditions=changed(conditions(), **{field: "another-condition"}),
            ),
        ),
    )
    result = select_routes(comparison, (a, b))
    assert result.quality_survivors == ("A", "B")
    assert result.selected_id == "B"
    assert result.basis == "unresolved_tie_fallback"

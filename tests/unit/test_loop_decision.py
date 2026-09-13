"""量化纯核只计算合同内事实，不产生交付权限。"""

import json
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import permutations

import pytest
from pydantic import BaseModel, ValidationError

from ai_sdlc.core.loop_decision import contract_digest, evaluate
from ai_sdlc.core.loop_decision_models import (
    DecisionValue,
    EvaluationInput,
    Goal,
    GoalContract,
    Obligation,
    ObligationResult,
    TimeEstimate,
)


def make_input(statuses=("PASS", "FAIL", "UNKNOWN"), *, required=False):
    goals = tuple(
        Goal(id=f"g{i}", source_ref="user:goal") for i in range(len(statuses))
    )
    obligations = tuple(
        Obligation(
            id=f"o{i}",
            goal_id=goal.id,
            statement="满足明确业务义务",
            required=required,
            source_ref="spec:FR-001",
            oracle_kind="deterministic",
            pass_rule="固定样例全部通过",
            fail_rule="存在可复现反例",
            unknown_rule="缺少必要观测",
            weight_share="1",
        )
        for i, goal in enumerate(goals)
    )
    return EvaluationInput(
        contract=GoalContract(goals=goals, obligations=obligations),
        artifact_digest="a" * 64,
        results=tuple(
            ObligationResult(
                id=f"o{i}",
                status=status,
                evidence_refs=("receipt:1",),
                reason="固定样本观测",
            )
            for i, status in enumerate(statuses)
        ),
    )


def test_nonterminating_partitions_and_delta_are_exact():
    first = make_input()
    result = evaluate(first)
    assert result.q == result.u == result.f == Fraction(100, 3)
    assert result.q + result.u + result.f == 100
    second = make_input(("FAIL", "UNKNOWN", "PASS"))
    changed = evaluate(second, baseline=first)
    assert changed.delta_q == 0
    assert changed.rf == Fraction(100, 3)
    assert changed.ru == 0
    unknown = evaluate(make_input(("UNKNOWN", "PASS", "UNKNOWN")), baseline=first)
    assert unknown.ru == Fraction(100, 3)
    assert '"100/3"' in result.model_dump_json()


def test_ten_obligation_example_and_required_unknown():
    data = make_input(("PASS",) * 7 + ("FAIL",) * 2 + ("UNKNOWN",))
    raw = data.model_dump()
    for i, row in enumerate(raw["contract"]["obligations"]):
        row["required"] = i < 4
        row["oracle_kind"] = "rubric" if i < 2 else "deterministic"
    data = EvaluationInput.model_validate(raw)
    result = evaluate(data)
    assert (result.h, result.q, result.u, result.f, result.j) == (0, 70, 10, 20, 20)
    next_raw = data.model_dump()
    for row in next_raw["results"][7:9]:
        row["status"] = "PASS"
    assert (
        evaluate(EvaluationInput.model_validate(next_raw), baseline=data).delta_q == 20
    )
    assert evaluate(make_input(("UNKNOWN",), required=True)).h == 1


def test_order_and_decimal_context_do_not_change_digest_or_aggregates():
    data = make_input()
    expected = evaluate(data)
    for order in permutations(range(3)):
        raw = data.model_dump()
        raw["contract"]["goals"] = tuple(raw["contract"]["goals"][i] for i in order)
        raw["contract"]["obligations"] = tuple(
            raw["contract"]["obligations"][i] for i in order
        )
        raw["results"] = tuple(raw["results"][i] for i in order)
        with localcontext() as context:
            context.prec = 2
            assert evaluate(EvaluationInput.model_validate(raw)) == expected


def test_extreme_weights_remain_exact_and_decimal_spelling_is_canonical():
    raw = make_input().model_dump()
    raw["contract"]["goals"][0]["weight"] = "999999999.999999999"
    raw["contract"]["goals"][1]["weight"] = "0.000000001"
    data = EvaluationInput.model_validate(raw)
    result = evaluate(data)
    assert result.q + result.u + result.f == 100
    with localcontext() as context:
        context.prec = 2
        assert evaluate(data) == result
    one = make_input(("PASS",)).contract
    same = one.model_dump()
    same["goals"][0]["weight"] = Decimal("1.000")
    assert contract_digest(GoalContract.model_validate(same)) == contract_digest(one)


def test_changed_contract_disables_baseline_comparison():
    data = make_input()
    raw = data.model_dump()
    raw["contract"]["obligations"][0]["pass_rule"] = "新的合法判据"
    result = evaluate(EvaluationInput.model_validate(raw), baseline=data)
    assert result.delta_q is result.rf is result.ru is None
    assert result.baseline_artifact_digest == data.artifact_digest


@pytest.mark.parametrize(
    "bad",
    [True, False, "NaN", "Infinity", "-Infinity", 0, -1, "0.0000000001", "1e1000"],
)
def test_reject_invalid_goal_weight(bad):
    with pytest.raises(ValidationError):
        Goal(id="g", source_ref="user:goal", weight=bad)


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_goal",
        "duplicate_obligation",
        "duplicate_result",
        "missing_result",
        "extra_result",
        "orphan",
        "share",
        "forecast",
        "missing_evidence",
        "unknown_field",
        "empty_text",
        "bool_required",
    ],
)
def test_reject_malformed_or_misleading_inputs(mutation):
    raw = make_input().model_dump()
    if mutation == "duplicate_goal":
        raw["contract"]["goals"] += (raw["contract"]["goals"][0],)
    elif mutation == "duplicate_obligation":
        raw["contract"]["obligations"] += (raw["contract"]["obligations"][0],)
    elif mutation == "duplicate_result":
        raw["results"] += (raw["results"][0],)
    elif mutation == "missing_result":
        raw["results"] = raw["results"][:-1]
    elif mutation == "extra_result":
        raw["results"] += ({**raw["results"][0], "id": "other"},)
    elif mutation == "orphan":
        raw["contract"]["obligations"][0]["goal_id"] = "missing"
    elif mutation == "share":
        raw["contract"]["obligations"][0]["weight_share"] = "0.5"
    elif mutation == "forecast":
        raw["results"][0]["status"] = "forecast"
    elif mutation == "missing_evidence":
        raw["results"][0]["evidence_refs"] = ()
    elif mutation == "unknown_field":
        raw["contract"]["score"] = 100
    elif mutation == "empty_text":
        raw["contract"]["obligations"][0]["unknown_rule"] = " "
    else:
        raw["contract"]["obligations"][0]["required"] = "false"
    with pytest.raises(ValidationError):
        EvaluationInput.model_validate(raw)


def test_unknown_needs_reason_but_not_invented_evidence():
    row = ObligationResult(id="o", status="UNKNOWN", reason="未运行检查")
    assert row.evidence_refs == ()
    with pytest.raises(ValidationError):
        ObligationResult(id="o", status="UNKNOWN", reason="")


def test_group_split_cannot_increase_goal_weight():
    before = make_input(("PASS", "FAIL"))
    raw = before.model_dump()
    first = raw["contract"]["obligations"][0]
    raw["contract"]["obligations"] = (
        {**first, "weight_share": "0.5"},
        {**first, "id": "o-extra", "weight_share": "0.5"},
        raw["contract"]["obligations"][1],
    )
    raw["results"] += ({**raw["results"][0], "id": "o-extra"},)
    after = evaluate(EvaluationInput.model_validate(raw), baseline=before)
    assert after.q == evaluate(before).q == 50
    assert after.delta_q is None


@pytest.mark.parametrize("lower,upper", [(5, 4), (-1, 4), (True, 4), (0, "NaN")])
def test_invalid_time_estimates_are_rejected(lower, upper):
    with pytest.raises(ValidationError):
        TimeEstimate(
            lower_seconds=lower,
            upper_seconds=upper,
            scope="route-through-check",
            basis_refs=("plan:1",),
            assumptions=("已有相同规模任务",),
        )


def test_time_prediction_is_not_a_host_budget():
    estimate = TimeEstimate(
        lower_seconds=60,
        upper_seconds=120,
        scope="route-through-check",
        basis_refs=("plan:1",),
        assumptions=("首次预测",),
    )
    assert estimate.upper_seconds == 120
    assert "token" not in TimeEstimate.model_fields
    with pytest.raises(ValidationError):
        estimate.upper_seconds = 200


def test_empty_contract_and_forged_aggregate_baseline_are_rejected():
    with pytest.raises(ValidationError):
        GoalContract(goals=(), obligations=())
    raw = make_input().model_dump()
    raw["q"] = 100
    with pytest.raises(ValidationError):
        EvaluationInput.model_validate(raw)


def test_revalidation_rejects_unsafe_constructed_models():
    data = make_input()
    forged = data.model_copy(update={"results": data.results[:-1]})
    with pytest.raises(ValidationError):
        evaluate(forged)


def test_pure_core_has_no_execution_or_storage_dependencies():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/ai_sdlc/core"
    allowed = {
        "__future__",
        "decimal",
        "fractions",
        "typing",
        "pydantic",
        "hashlib",
        "json",
        "ai_sdlc.core.loop_decision_models",
    }
    for name in ("loop_decision.py", "loop_decision_models.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        )
        assert imports <= allowed


@pytest.mark.parametrize("precision", [2, 28, 80])
@pytest.mark.parametrize(
    "value", ["0.1234567891", "1.00000000000000000000000000001", "1234567890.123456789"]
)
def test_invalid_decimal_bounds_are_context_independent(precision, value):
    with localcontext() as context:
        context.prec = precision
        with pytest.raises(ValidationError):
            Goal(id="g", source_ref="test", weight=value)
        with pytest.raises(ValidationError):
            TimeEstimate(
                lower_seconds=0,
                upper_seconds=value,
                scope="route",
                basis_refs=("plan:1",),
                assumptions=("test",),
            )
        raw = make_input(("PASS",)).contract.obligations[0].model_dump()
        raw["weight_share"] = value
        with pytest.raises(ValidationError):
            Obligation.model_validate(raw)


@pytest.mark.parametrize("strict", [None, True])
def test_json_decimal_string_and_decimal_have_identical_meaning(strict):
    string = Goal.model_validate_json(
        '{"id":"g","source_ref":"test","weight":"123456789.123456789"}',
        strict=strict,
    )
    direct = Goal(id="g", source_ref="test", weight=Decimal("123456789.123456789"))
    assert string == direct
    raw = make_input().model_dump()
    raw["contract"]["goals"][0]["weight"] = direct.weight
    original = EvaluationInput.model_validate(raw)
    decoded = EvaluationInput.model_validate_json(
        original.model_dump_json(), strict=strict
    )
    assert decoded == original
    assert evaluate(decoded) == evaluate(original)


def test_already_lossy_python_float_is_rejected():
    with pytest.raises(ValidationError):
        Goal(id="g", source_ref="test", weight=123456789.123456789)


def json_samples():
    data = make_input()
    return (
        data.contract.goals[0],
        data.contract.obligations[0],
        data.contract,
        data.results[0],
        data,
        TimeEstimate(
            lower_seconds=0,
            upper_seconds="1.125",
            scope="route",
            basis_refs=("plan:1",),
            assumptions=("首次预测",),
        ),
        evaluate(data),
    )


@pytest.mark.parametrize("index", range(7))
@pytest.mark.parametrize("strict", [None, True])
def test_all_value_objects_roundtrip_through_native_json(index, strict):
    sample = json_samples()[index]
    assert (
        type(sample).model_validate_json(sample.model_dump_json(), strict=strict)
        == sample
    )


@pytest.mark.parametrize("field", ["h", "q", "u", "f", "j", "rf", "ru", "delta_q"])
@pytest.mark.parametrize("value", [False, True, 0.1, 1.0])
@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize("entry", ["python", "json"])
def test_evaluation_rejects_boolean_and_float_numbers(field, value, strict, entry):
    sample = evaluate(make_input())
    raw = sample.model_dump(mode="json") if entry == "json" else dict(sample)
    raw[field] = value
    with pytest.raises(ValidationError) as error:
        if entry == "json":
            type(sample).model_validate_json(json.dumps(raw), strict=strict)
        else:
            type(sample).model_validate(raw, strict=strict)
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("field", ["q", "u", "f", "j", "rf", "ru", "delta_q"])
@pytest.mark.parametrize("strict", [None, True])
def test_evaluation_json_numbers_keep_exact_input_boundary(field, strict):
    sample = evaluate(make_input())
    raw = sample.model_dump(mode="json")
    raw[field] = "raw-number-token"
    payload = json.dumps(raw)
    for token in ("0.1", "1.0", "1e0"):
        with pytest.raises(ValidationError) as error:
            type(sample).model_validate_json(
                payload.replace('"raw-number-token"', token), strict=strict
            )
        assert any(item["loc"] == (field,) for item in error.value.errors())
    for token in ("0", "1", '"0.1"', '"1/10"'):
        restored = type(sample).model_validate_json(
            payload.replace('"raw-number-token"', token), strict=strict
        )
        assert getattr(restored, field) == Fraction(token.strip('"'))


@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize("entry", ["python", "json"])
def test_evaluation_preserves_exact_numbers_and_baseline_roundtrip(strict, entry):
    baseline = make_input(("PASS", "PASS", "UNKNOWN"), required=True)
    current = make_input(("FAIL", "UNKNOWN", "PASS"), required=True)
    sample = evaluate(current, baseline=baseline)
    if entry == "json":
        restored = type(sample).model_validate_json(
            sample.model_dump_json(), strict=strict
        )
    else:
        restored = type(sample).model_validate(dict(sample), strict=strict)
    assert restored == sample
    assert restored.h == 2
    assert restored.q == restored.u == restored.f == Fraction(100, 3)
    assert restored.rf == restored.ru == Fraction(100, 3)
    assert restored.delta_q == Fraction(-100, 3)


def test_json_api_is_inherited_and_python_strict_sequences_stay_strict():
    assert (
        DecisionValue.model_validate_json.__func__
        is BaseModel.model_validate_json.__func__
    )
    raw = json_samples()[3].model_dump(mode="json")
    with pytest.raises(ValidationError, match="tuple_type"):
        ObligationResult.model_validate(raw, strict=True)


def decimal_json_payload(field, token):
    index = {"weight": 0, "weight_share": 1, "upper_seconds": 5}[field]
    sample = json_samples()[index]
    raw = sample.model_dump(mode="json")
    raw[field] = "raw-number-token"
    return type(sample), json.dumps(raw).replace('"raw-number-token"', token)


@pytest.mark.parametrize("field", ["weight", "weight_share", "upper_seconds"])
@pytest.mark.parametrize("token", ["0.125", "1.0", "1e0"])
@pytest.mark.parametrize("strict", [None, True])
def test_bare_json_decimal_tokens_are_rejected(field, token, strict):
    model, payload = decimal_json_payload(field, token)
    with pytest.raises(ValidationError, match="decimal string"):
        model.model_validate_json(payload, strict=strict)


@pytest.mark.parametrize("field", ["weight", "weight_share", "upper_seconds"])
@pytest.mark.parametrize("token", ["1", '"0.125"'])
@pytest.mark.parametrize("strict", [None, True])
def test_json_integer_or_decimal_string_is_exact(field, token, strict):
    model, payload = decimal_json_payload(field, token)
    result = model.model_validate_json(payload, strict=strict)
    assert getattr(result, field) == Decimal(token.strip('"'))


@pytest.mark.parametrize("strict", [None, True])
@pytest.mark.parametrize(
    "token", ["true", "NaN", "Infinity", '"NaN"', '"0.1234567891"']
)
def test_json_invalid_decimal_values_are_rejected(strict, token):
    model, payload = decimal_json_payload("weight", token)
    with pytest.raises(ValidationError):
        model.model_validate_json(payload, strict=strict)


@pytest.mark.parametrize("strict", [None, True])
def test_json_unknown_fields_and_wrong_types_remain_rejected(strict):
    for payload in (
        '{"id":"g","source_ref":"s","score":100}',
        '{"id":"g","source_ref":0.125}',
        '{"id":"g","source_ref":"s","weight":[]}',
        '{"id":"g","source_ref":"s","weight":"1",}',
    ):
        with pytest.raises(ValidationError):
            Goal.model_validate_json(payload, strict=strict)


def test_maximum_contract_strict_json_preserves_exact_evaluation():
    raw = make_input(("PASS",) * 128).model_dump()
    raw["contract"]["obligations"] = tuple(
        {**item, "id": f"{item['id']}-{index}", "weight_share": "0.0625"}
        for item in raw["contract"]["obligations"]
        for index in range(16)
    )
    raw["results"] = tuple(
        {
            **raw["results"][0],
            "id": item["id"],
            "status": ("PASS", "FAIL", "UNKNOWN")[index % 3],
        }
        for index, item in enumerate(raw["contract"]["obligations"])
    )
    original = EvaluationInput.model_validate(raw)
    payload = original.model_dump_json()
    restored = EvaluationInput.model_validate_json(payload, strict=True)
    assert len(restored.contract.goals) == 128
    assert len(restored.results) == 2048
    assert evaluate(restored) == evaluate(original)
    assert contract_digest(restored.contract) == contract_digest(original.contract)
    print(
        f"goals=128 obligations=2048 payload_utf8_bytes={len(payload.encode('utf-8'))}"
    )

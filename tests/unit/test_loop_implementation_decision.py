"""B1 只从原始义务结果作停止判定，不接受聚合分或生成 Close 权限。"""

import json
from itertools import product

import pytest
from pydantic import ValidationError

from ai_sdlc.core.loop_decision import decide_implementation, evaluate
from ai_sdlc.core.loop_decision_models import (
    EvaluationInput,
    Goal,
    GoalContract,
    ImplementationDecisionInput,
    Obligation,
    ObligationResult,
)


def actual(status="PASS", *, required=True):
    return EvaluationInput(
        contract=GoalContract(
            goals=(Goal(id="g1", source_ref="spec:goal"),),
            obligations=(
                Obligation(
                    id="o1",
                    goal_id="g1",
                    statement="未授权请求必须拒绝",
                    required=required,
                    source_ref="spec:FR-001",
                    oracle_kind="deterministic",
                    pass_rule="固定未授权样本全部被拒绝",
                    fail_rule="存在未授权请求被放行的反例",
                    unknown_rule="未运行当前样本或证据缺失",
                    weight_share="1",
                ),
            ),
        ),
        artifact_digest="a" * 64,
        results=(
            ObligationResult(
                id="o1",
                status=status,
                evidence_refs=("expert:r1:obligation:o1",)
                if status != "UNKNOWN"
                else (),
                reason="固定样本的独立实际观察",
            ),
        ),
    )


@pytest.mark.parametrize(
    "round_number,status,required,actionable,readiness",
    product(
        (1, 2),
        ("PASS", "FAIL", "UNKNOWN"),
        (True, False),
        (True, False),
        ("PASS", "FAIL", "UNKNOWN"),
    ),
)
def test_should_obey_bounded_transition_table(
    round_number, status, required, actionable, readiness
):
    request = ImplementationDecisionInput(
        current=actual(status, required=required),
        round_number=round_number,
        has_actionable_findings=actionable,
        repair_readiness=readiness,
    )
    result = decide_implementation(request)
    gap = required and status != "PASS"
    if not gap and not actionable:
        expected = ("stop", "requirements-satisfied")
    elif round_number == 2:
        expected = ("blocked", "review-round-limit")
    elif readiness != "PASS":
        expected = ("blocked", "repair-unavailable")
    else:
        expected = ("repair", "required-gap" if gap else "actionable-findings")
    assert (result.action, result.reason) == expected
    assert not hasattr(result, "close_authorized")
    assert (
        ImplementationDecisionInput.model_validate_json(request.model_dump_json())
        == request
    )


def test_should_not_treat_missing_repair_readiness_as_authorization():
    request = ImplementationDecisionInput(
        current=actual("UNKNOWN"), round_number=1, has_actionable_findings=False
    )
    assert request.repair_readiness == "UNKNOWN"
    assert decide_implementation(request).action == "blocked"


@pytest.mark.parametrize("round_number", [True, False, 0, 3, -1, "1", 1.0, None])
def test_should_reject_invalid_or_coerced_round(round_number):
    with pytest.raises(ValidationError):
        ImplementationDecisionInput(
            current=actual(),
            round_number=round_number,
            has_actionable_findings=False,
        )


@pytest.mark.parametrize("flag", [0, 1, "false", "true", None])
def test_should_reject_nonboolean_finding_flag(flag):
    with pytest.raises(ValidationError):
        ImplementationDecisionInput(
            current=actual(), round_number=1, has_actionable_findings=flag
        )


@pytest.mark.parametrize("readiness", [None, True, 1, "ready", "pass", ""])
def test_should_reject_unrecognized_repair_readiness(readiness):
    with pytest.raises(ValidationError):
        ImplementationDecisionInput(
            current=actual("FAIL"),
            round_number=1,
            has_actionable_findings=False,
            repair_readiness=readiness,
        )


@pytest.mark.parametrize("kind", ["model", "json"])
def test_should_reject_forged_aggregate_even_when_it_says_h_zero(kind):
    forged = evaluate(actual("UNKNOWN")).model_copy(update={"h": 0, "q": 100})
    current = forged if kind == "model" else forged.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ImplementationDecisionInput(
            current=current, round_number=1, has_actionable_findings=False
        )


@pytest.mark.parametrize("key", ["h", "q", "evaluation", "improve", "window_seconds"])
def test_should_reject_extra_aggregate_or_unimplemented_protocol_fields(key):
    raw = {
        "current": actual(),
        "round_number": 1,
        "has_actionable_findings": False,
        key: 0,
    }
    with pytest.raises(ValidationError):
        ImplementationDecisionInput.model_validate(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        "round",
        "flag",
        "readiness",
        "coverage",
        "required",
        "status",
        "evidence",
        "aggregate",
    ],
)
def test_should_revalidate_constructed_or_copied_values_at_function_entry(mutation):
    current = actual("FAIL")
    raw = {
        "current": current,
        "round_number": 1,
        "has_actionable_findings": False,
        "repair_readiness": "PASS",
    }
    if mutation == "round":
        raw["round_number"] = True
    elif mutation == "flag":
        raw["has_actionable_findings"] = "false"
    elif mutation == "readiness":
        raw["repair_readiness"] = True
    elif mutation == "coverage":
        raw["current"] = current.model_copy(update={"results": ()})
    elif mutation == "required":
        obligation = current.contract.obligations[0].model_copy(
            update={"required": "false"}
        )
        raw["current"] = current.model_copy(
            update={
                "contract": current.contract.model_copy(
                    update={"obligations": (obligation,)}
                )
            }
        )
    elif mutation == "status":
        raw["current"] = current.model_copy(
            update={
                "results": (current.results[0].model_copy(update={"status": "pass"}),)
            }
        )
    elif mutation == "evidence":
        raw["current"] = current.model_copy(
            update={
                "results": (
                    current.results[0].model_copy(update={"evidence_refs": ()}),
                )
            }
        )
    else:
        raw["current"] = evaluate(current).model_copy(update={"h": 0})
    with pytest.raises(ValidationError):
        decide_implementation(ImplementationDecisionInput.model_construct(**raw))


def test_should_keep_optional_quality_separate_from_required_floor():
    request = ImplementationDecisionInput(
        current=actual("UNKNOWN", required=False),
        round_number=1,
        has_actionable_findings=False,
    )
    before = request.model_dump_json()
    result = decide_implementation(request)
    assert result.action == "stop"
    assert evaluate(request.current).q == 0
    assert request.model_dump_json() == before
    with pytest.raises(ValidationError):
        result.action = "repair"


@pytest.mark.parametrize(
    "field,value",
    [
        ("round_number", True),
        ("round_number", 1.0),
        ("round_number", "1"),
        ("round_number", 3),
        ("has_actionable_findings", 0),
        ("has_actionable_findings", "false"),
        ("has_actionable_findings", None),
        ("repair_readiness", True),
        ("repair_readiness", "pass"),
        ("repair_readiness", None),
    ],
)
def test_should_reject_coercion_in_native_json(field, value):
    raw = ImplementationDecisionInput(
        current=actual(), round_number=1, has_actionable_findings=False
    ).model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        ImplementationDecisionInput.model_validate_json(json.dumps(raw))


def test_should_keep_one_required_unknown_visible_at_maximum_contract_scale():
    sample = actual()
    goals = tuple(Goal(id=f"g{i}", source_ref="spec:goal") for i in range(128))
    obligations = tuple(
        sample.contract.obligations[0].model_copy(
            update={"id": f"o{i}", "goal_id": f"g{i // 16}", "weight_share": "0.0625"}
        )
        for i in range(2048)
    )
    rows = tuple(
        sample.results[0].model_copy(
            update={"id": f"o{i}", "status": "PASS" if i < 2047 else "UNKNOWN"}
        )
        for i in range(2048)
    )
    request = ImplementationDecisionInput(
        current=EvaluationInput(
            contract=GoalContract(goals=goals, obligations=obligations),
            artifact_digest="b" * 64,
            results=rows,
        ),
        round_number=1,
        has_actionable_findings=False,
        repair_readiness="PASS",
    )
    restored = ImplementationDecisionInput.model_validate_json(
        request.model_dump_json()
    )
    assert evaluate(restored.current).h == 1
    assert evaluate(restored.current).q > 99
    assert decide_implementation(restored).action == "repair"
    assert (
        decide_implementation(restored.model_copy(update={"round_number": 2})).action
        == "blocked"
    )

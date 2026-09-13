"""宿主无关的量化纯函数；不读时钟、工件或供应商接口。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction

from ai_sdlc.core.loop_decision_models import (
    Evaluation,
    EvaluationInput,
    GoalContract,
    ImplementationDecision,
    ImplementationDecisionInput,
    NativeMetric,
    RouteCandidate,
    RouteExclusion,
    RouteSelectionContract,
    Selection,
)


def _decimal_text(value: Decimal) -> str:
    # normalize() 会受外部 Decimal 精度影响；定点格式化不会。
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def contract_digest(contract: GoalContract) -> str:
    """只把计分语义纳入摘要，忽略集合排列及十进制尾零。"""
    checked = GoalContract.model_validate(contract)
    goals = []
    obligations = []
    for goal in sorted(checked.goals, key=lambda item: item.id):
        goals.append(
            {**goal.model_dump(mode="json"), "weight": _decimal_text(goal.weight)}
        )
    for item in sorted(checked.obligations, key=lambda item: item.id):
        obligations.append(
            {
                **item.model_dump(mode="json"),
                "weight_share": _decimal_text(item.weight_share),
            }
        )
    encoded = json.dumps(
        {"goals": goals, "obligations": obligations},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _weights(contract: GoalContract) -> dict[str, Fraction]:
    goals = {goal.id: Fraction(goal.weight) for goal in contract.goals}
    total = sum(goals.values(), Fraction(0))
    return {
        item.id: 100 * goals[item.goal_id] * Fraction(item.weight_share) / total
        for item in contract.obligations
    }


def evaluate(
    current: EvaluationInput, *, baseline: EvaluationInput | None = None
) -> Evaluation:
    """重新校验并聚合原始结果，不接受外部聚合分数作为基线。"""
    current = EvaluationInput.model_validate(current)
    baseline = (
        EvaluationInput.model_validate(baseline) if baseline is not None else None
    )
    weights = _weights(current.contract)
    results = {row.id: row for row in current.results}
    totals = dict.fromkeys(("PASS", "FAIL", "UNKNOWN"), Fraction(0))
    h = 0
    j = Fraction(0)
    for item in current.contract.obligations:
        status = results[item.id].status
        totals[status] += weights[item.id]
        h += int(item.required and status != "PASS")
        if status == "PASS" and item.oracle_kind == "rubric":
            j += weights[item.id]
    digest = contract_digest(current.contract)
    rf = ru = delta = None
    if baseline is not None and contract_digest(baseline.contract) == digest:
        # 只对同合同的实际原始证据计算变化，不沿用自报分数。
        old_pass = {row.id for row in baseline.results if row.status == "PASS"}
        old_q = sum((weights[item_id] for item_id in old_pass), Fraction(0))
        delta = totals["PASS"] - old_q
        rf = sum(
            (
                weights[item_id]
                for item_id in old_pass
                if results[item_id].status == "FAIL"
            ),
            Fraction(0),
        )
        ru = sum(
            (
                weights[item_id]
                for item_id in old_pass
                if results[item_id].status == "UNKNOWN"
            ),
            Fraction(0),
        )
    return Evaluation(
        contract_digest=digest,
        artifact_digest=current.artifact_digest,
        baseline_artifact_digest=baseline.artifact_digest
        if baseline is not None
        else None,
        results=tuple(sorted(current.results, key=lambda row: row.id)),
        h=h,
        q=totals["PASS"],
        u=totals["UNKNOWN"],
        f=totals["FAIL"],
        j=j,
        rf=rf,
        ru=ru,
        delta_q=delta,
    )


def decide_implementation(
    request: ImplementationDecisionInput,
) -> ImplementationDecision:
    """B1 从当前原始结果复算，停止不等于授权关闭或刷新轮次。"""
    checked = ImplementationDecisionInput.model_validate(request)
    current = evaluate(checked.current)
    if current.h == 0 and not checked.has_actionable_findings:
        return ImplementationDecision(action="stop", reason="requirements-satisfied")
    if checked.round_number == 2:
        return ImplementationDecision(action="blocked", reason="review-round-limit")
    if checked.repair_readiness != "PASS":
        return ImplementationDecision(action="blocked", reason="repair-unavailable")
    return ImplementationDecision(
        action="repair",
        reason="required-gap" if current.h else "actionable-findings",
    )


def route_contract_digest(contract: RouteSelectionContract) -> str:
    """绑定完整比较尺度；旧聚合合同摘要保持不变，偏好次序不能重排。"""
    checked = RouteSelectionContract.model_validate(contract)
    payload = checked.model_dump(exclude={"goal_contract"})
    payload["goal_contract_digest"] = contract_digest(checked.goal_contract)
    payload["source_refs"] = sorted(payload["source_refs"])
    payload["metrics"] = sorted(payload["metrics"], key=lambda row: row["id"])
    for metric in payload["metrics"]:
        metric["source_refs"] = sorted(metric["source_refs"])
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_decimal_text,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checked_routes(
    contract: RouteSelectionContract,
    candidates: tuple[RouteCandidate, ...],
    digest: str,
) -> list[RouteCandidate]:
    if not isinstance(candidates, (tuple, list)) or not 1 <= len(candidates) <= 3:
        raise ValueError("route selection requires one to three candidates")
    routes = sorted(
        (RouteCandidate.model_validate(row) for row in candidates),
        key=lambda row: row.id,
    )
    if len({row.id for row in routes}) != len(routes):
        raise ValueError("candidate ids must be unique")
    mechanisms = {" ".join(row.mechanism.casefold().split()) for row in routes}
    if len(mechanisms) != len(routes):
        raise ValueError("candidate mechanisms must differ, not merely their ids")
    if contract.baseline_id is not None and contract.baseline_id not in {
        row.id for row in routes
    }:
        raise ValueError("baseline must reference a candidate in this comparison")
    metric_ids = {metric.id for metric in contract.metrics}
    obligation_ids = {item.id for item in contract.goal_contract.obligations}
    for row in routes:
        if row.contract_digest != digest:
            raise ValueError("candidate comparison contract has changed")
        if {metric.id for metric in row.metric_estimates} != metric_ids:
            raise ValueError("candidate must cover exactly the contract metrics")
        if any(
            item.id not in obligation_ids for item in row.expected_obligation_changes
        ):
            raise ValueError("candidate references an unknown obligation")
    return routes


def _metric_intervals(
    metric: NativeMetric, routes: list[RouteCandidate]
) -> dict[str, tuple[Fraction, Fraction]] | None:
    """转成收益方向；不同口径、层级和关键未知不能进入确定比较。"""
    if metric.material_difference is None:
        return None
    estimates = {
        row.id: next(item for item in row.metric_estimates if item.id == metric.id)
        for row in routes
    }
    kinds = {item.kind for item in estimates.values()}
    if len(kinds) != 1 or "unknown" in kinds:
        return None
    intervals = {}
    for route_id, item in estimates.items():
        if (
            item.conditions != metric.conditions
            or item.lower is None
            or item.upper is None
        ):
            return None
        lower, upper = Fraction(item.lower), Fraction(item.upper)
        intervals[route_id] = (
            (lower, upper) if metric.direction == "maximize" else (-upper, -lower)
        )
    return intervals


def _time_intervals(
    contract: RouteSelectionContract, routes: list[RouteCandidate]
) -> dict[str, tuple[Fraction, Fraction]] | None:
    intervals = {}
    for route in routes:
        estimate = route.future_cost_estimate
        if (
            estimate is None
            or route.cost_decision_point != contract.decision_point
            or estimate.scope != contract.time_scope
        ):
            return None
        intervals[route.id] = (
            Fraction(estimate.lower_seconds),
            Fraction(estimate.upper_seconds),
        )
    return intervals


def _dominates(
    contract: RouteSelectionContract, better: RouteCandidate, other: RouteCandidate
) -> bool:
    times = _time_intervals(contract, [better, other])
    if (
        not contract.metrics
        or times is None
        or times[better.id][1] > times[other.id][0]
    ):
        return False
    strict = False
    for metric in contract.metrics:
        intervals = _metric_intervals(metric, [better, other])
        if intervals is None:
            return False
        margin = intervals[better.id][0] - intervals[other.id][1]
        if margin < 0:
            return False
        assert metric.material_difference is not None
        strict |= margin > Fraction(metric.material_difference)
    return strict


def _quality_selection(
    contract: RouteSelectionContract,
    routes: list[RouteCandidate],
    excluded: list[RouteExclusion],
    limitations: list[str],
) -> tuple[list[RouteCandidate], bool]:
    used_forecast = False
    dominated = set()
    for other in routes:
        for better in routes:
            if better.id == other.id or not _dominates(contract, better, other):
                continue
            dominated.add(other.id)
            used_forecast |= any(
                item.kind == "forecast" for item in better.metric_estimates
            )
            excluded.append(
                RouteExclusion(
                    candidate_id=other.id, reason="dominated", witness_id=better.id
                )
            )
            break
    remaining = [row for row in routes if row.id not in dominated]
    metrics = {metric.id: metric for metric in contract.metrics}
    for metric_id in contract.ordered_metric_ids:
        if len(remaining) == 1:
            break
        metric = metrics[metric_id]
        intervals = _metric_intervals(metric, remaining)
        if intervals is None:
            # 不能用低优先维掩盖高优先维未知；交给明确标注的兜底。
            limitations.append(f"incomparable_quality:{metric_id}")
            break
        best_lower = max(lower for lower, _ in intervals.values())
        witness = min(
            key for key, (lower, _) in intervals.items() if lower == best_lower
        )
        assert metric.material_difference is not None
        cutoff = best_lower - Fraction(metric.material_difference)
        removed = {key for key, (_, upper) in intervals.items() if upper < cutoff}
        if removed:
            used_forecast |= any(
                item.id == metric_id and item.kind == "forecast"
                for row in remaining
                for item in row.metric_estimates
            )
        excluded.extend(
            RouteExclusion(
                candidate_id=key,
                reason="quality_preference",
                witness_id=witness,
                metric_id=metric_id,
            )
            for key in sorted(removed)
        )
        remaining = [row for row in remaining if row.id not in removed]
    return remaining, used_forecast


def select_routes(
    contract: RouteSelectionContract, candidates: tuple[RouteCandidate, ...]
) -> Selection:
    """只计算一个可复算推荐；预测范围不是置信区间，推荐不执行路线。"""
    contract = RouteSelectionContract.model_validate(contract)
    digest = route_contract_digest(contract)
    routes = _checked_routes(contract, candidates, digest)
    excluded: list[RouteExclusion] = []
    limitations: list[str] = []
    eligible = []
    for row in routes:
        failed = sorted(
            (item for item in row.execution_preconditions if item.status != "PASS"),
            key=lambda item: item.kind,
        )
        excluded.extend(
            RouteExclusion(
                candidate_id=row.id,
                reason="precondition_not_passed",
                metric_id=f"{item.kind}:{item.status}",
            )
            for item in failed
        )
        if not failed:
            eligible.append(row)
    remaining = eligible
    quality = eligible
    selected_id = None
    basis = None
    reason = "no_safe_route"
    if eligible:
        basis = "unresolved_tie_fallback"
        reason = "single_feasible_route"
        if len(eligible) == 1:
            limitations.append("no_alternative_comparison")
        else:
            quality, used_forecast = _quality_selection(
                contract, eligible, excluded, limitations
            )
            remaining = quality
            if len(quality) == 1:
                basis = "forecast_preference" if used_forecast else "evidence_advantage"
                reason = "quality_preference"
            else:
                limitations.append("quality_unresolved")
                times = _time_intervals(contract, quality)
                if times is None:
                    limitations.append("incomparable_future_time")
                else:
                    best_upper = min(upper for _, upper in times.values())
                    witness = min(
                        key for key, (_, upper) in times.items() if upper == best_upper
                    )
                    removed = {
                        key for key, (lower, _) in times.items() if lower > best_upper
                    }
                    excluded.extend(
                        RouteExclusion(
                            candidate_id=key,
                            reason="future_time_preference",
                            witness_id=witness,
                        )
                        for key in sorted(removed)
                    )
                    remaining = [row for row in quality if row.id not in removed]
                    if len(remaining) > 1:
                        limitations.append("future_time_unresolved")
                reason = (
                    "future_time_preference"
                    if len(remaining) == 1
                    else (
                        "baseline_tie_break"
                        if any(row.id == contract.baseline_id for row in remaining)
                        else "stable_id_tie_break"
                    )
                )
        selected_id = next(
            (row.id for row in remaining if row.id == contract.baseline_id),
            remaining[0].id,
        )
    return Selection(
        contract_digest=digest,
        selected_id=selected_id,
        basis=basis,
        reason=reason,
        eligible_ids=tuple(row.id for row in eligible),
        quality_survivors=tuple(row.id for row in quality),
        remaining_ids=tuple(row.id for row in remaining),
        excluded=tuple(
            sorted(
                excluded,
                key=lambda row: (row.candidate_id, row.reason, row.metric_id or ""),
            )
        ),
        limitations=tuple(limitations),
    )

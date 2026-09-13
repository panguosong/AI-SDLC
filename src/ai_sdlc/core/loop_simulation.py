"""模拟初始选择的纯核；时间由调用者传入，实际验收仍走旧评审。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from typing import cast

from pydantic import TypeAdapter

from ai_sdlc.core.loop_decision import contract_digest
from ai_sdlc.core.loop_decision_models import Digest
from ai_sdlc.core.loop_simulation_models import (
    CandidateAssessment,
    CountSummary,
    SimulatedCandidate,
    SimulationChoiceBasis,
    SimulationComparison,
    SimulationExclusion,
    SimulationExclusionReason,
    SimulationImprovementDecision,
    SimulationScore,
    SimulationSelection,
    SimulationSelectionReason,
    StageScoreContract,
)


def _canonical(value: object, key: str = "") -> object:
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, dict):
        return {name: _canonical(item, name) for name, item in value.items()}
    if isinstance(value, (tuple, list)):
        rows = [_canonical(item) for item in value]
        identifier_keys = {
            "criteria": "id",
            "goal_shares": "goal_id",
            "anchors": "level",
            "basis": "id",
            "candidate_items": "criterion_id",
            "candidates": "candidate_id",
            "execution_preconditions": "kind",
        }
        if key in identifier_keys:
            rows.sort(
                key=lambda row: str(cast(dict[str, object], row)[identifier_keys[key]])
            )
        elif key in {
            "source_refs",
            "basis_refs",
            "evidence_refs",
            "item_ids",
            "required_supported_ids",
            "known_required_conflicts",
        }:
            rows.sort()
        return rows
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stage_contract_digest(contract: StageScoreContract) -> str:
    checked = StageScoreContract.model_validate(contract)
    payload = checked.model_dump(exclude={"goal_contract"})
    payload["goal_contract_digest"] = contract_digest(checked.goal_contract)
    return _digest(payload)


def _checked_candidates(
    contract: StageScoreContract, candidates: tuple[SimulatedCandidate, ...]
) -> tuple[SimulatedCandidate, ...]:
    if not isinstance(candidates, (tuple, list)) or not 1 <= len(candidates) <= 3:
        raise ValueError("simulation-requires-one-to-three-candidates")
    checked = tuple(
        sorted(
            (SimulatedCandidate.model_validate(row) for row in candidates),
            key=lambda row: row.candidate_id,
        )
    )
    if len({row.candidate_id for row in checked}) != len(checked):
        raise ValueError("simulation-candidate-id-duplicate")
    if len({" ".join(row.mechanism.casefold().split()) for row in checked}) != len(
        checked
    ):
        raise ValueError("simulation-candidate-mechanisms-must-differ")
    digest = stage_contract_digest(contract)
    required = {row.id for row in contract.goal_contract.obligations if row.required}
    candidate_counts = {
        row.id
        for row in contract.criteria
        if row.applicability == "applicable" and row.count_scope == "candidate"
    }
    for row in checked:
        if row.contract_digest != digest:
            raise ValueError("simulation-contract-digest-mismatch")
        if not set(row.known_required_conflicts) <= required:
            raise ValueError("simulation-conflict-requires-required-obligation")
        if {item.criterion_id for item in row.candidate_items} != candidate_counts:
            raise ValueError("simulation-candidate-count-coverage")
    payload = json.dumps(
        [row.model_dump(mode="json") for row in checked],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > 65536:
        raise ValueError("simulation-candidate-payload-too-large")
    return checked


def judge_input_digest(
    contract: StageScoreContract,
    candidates: tuple[SimulatedCandidate, ...],
    source_manifest: dict[str, str],
) -> str:
    checked = StageScoreContract.model_validate(contract)
    rows = _checked_candidates(checked, candidates)
    manifest = TypeAdapter(dict[str, Digest]).validate_python(source_manifest)
    if not manifest:
        raise ValueError("simulation-source-manifest-empty")
    return _digest(
        {
            "contract_digest": stage_contract_digest(checked),
            "candidates": [row.model_dump() for row in rows],
            "source_manifest": manifest,
        }
    )


def score_candidate(
    contract: StageScoreContract,
    candidate: SimulatedCandidate,
    assessment: CandidateAssessment,
) -> SimulationScore:
    contract = StageScoreContract.model_validate(contract)
    candidate = _checked_candidates(contract, (candidate,))[0]
    assessment = CandidateAssessment.model_validate(assessment)
    if candidate.candidate_id != assessment.candidate_id:
        raise ValueError("simulation-assessment-candidate-mismatch")
    criteria = {
        row.id: row for row in contract.criteria if row.applicability == "applicable"
    }
    results = {row.criterion_id: row for row in assessment.criterion_assessments}
    if set(results) != set(criteria):
        raise ValueError("simulation-assessment-criterion-coverage")
    required = {row.id for row in contract.goal_contract.obligations if row.required}
    if not set(assessment.known_required_conflicts) <= required:
        raise ValueError("simulation-conflict-requires-required-obligation")
    weights = {row.id: Fraction(row.weight) for row in contract.goal_contract.goals}
    total = sum(weights.values(), Fraction(0))
    s_low = s_high = u_sim = Fraction(0)
    floor_failures, floor_unknowns, counts = [], [], []
    targets_met = True
    basis = {row.id for row in candidate.basis}
    candidate_items = {
        row.criterion_id: row.item_ids for row in candidate.candidate_items
    }
    for criterion_id, criterion in sorted(criteria.items()):
        result = results[criterion_id]
        refs = set(result.basis_refs) | {
            ref for item in result.items for ref in item.basis_refs
        }
        if not refs <= basis:
            raise ValueError("simulation-assessment-basis-reference-missing")
        expected = set(
            criterion.item_ids
            if criterion.count_scope == "contract"
            else candidate_items.get(criterion_id, ())
        )
        if {item.id for item in result.items} != expected:
            raise ValueError("simulation-count-items-must-cover-exactly-frozen-set")
        supported = {item.id for item in result.items if item.status == "supported"}
        unknown = {item.id for item in result.items if item.status == "unknown"}
        contradicted = {
            item.id for item in result.items if item.status == "contradicted"
        }
        for anchor in criterion.anchors:
            if (
                result.lower >= anchor.level
                and not set(anchor.required_supported_ids) <= supported
            ):
                raise ValueError("simulation-level-violates-required-anchor-items")
            if (
                result.upper >= anchor.level
                and set(anchor.required_supported_ids) & contradicted
            ):
                raise ValueError("simulation-upper-violates-required-anchor-items")
        if criterion.count_scope != "rubric":
            is_ratio = criterion.count_scope == "contract"
            counts.append(
                CountSummary(
                    criterion_id=criterion_id,
                    total=len(expected),
                    supported=len(supported),
                    contradicted=len(contradicted),
                    unknown=len(unknown),
                    coverage_low=Fraction(len(supported), len(expected))
                    if is_ratio
                    else None,
                    coverage_high=Fraction(len(supported) + len(unknown), len(expected))
                    if is_ratio
                    else None,
                )
            )
        weight = sum(
            (
                weights[row.goal_id] * Fraction(row.share) / total
                for row in criterion.goal_shares
            ),
            Fraction(0),
        )
        s_low += 25 * weight * result.lower
        s_high += 25 * weight * result.upper
        u_sim += weight if result.lower < result.upper else 0
        targets_met &= result.lower >= criterion.target_level
        if criterion.critical_floor is not None:
            if result.upper < criterion.critical_floor:
                floor_failures.append(criterion_id)
            elif result.lower < criterion.critical_floor:
                floor_unknowns.append(criterion_id)
    return SimulationScore(
        candidate_id=candidate.candidate_id,
        contract_digest=stage_contract_digest(contract),
        s_low=s_low,
        s_high=s_high,
        u_sim=u_sim,
        targets_met=targets_met,
        floor_failures=tuple(floor_failures),
        floor_unknowns=tuple(floor_unknowns),
        counts=tuple(counts),
    )


def _interval_survivors(
    intervals: dict[str, tuple[Fraction, Fraction]], *, minimize: bool = False
) -> tuple[set[str], str]:
    # 与旧路线核同一集合筛选规则；S 可为任意 Fraction，不能先压成受限十进制。
    oriented = {
        key: (-upper, -lower) if minimize else (lower, upper)
        for key, (lower, upper) in intervals.items()
    }
    best_lower = max(lower for lower, _ in oriented.values())
    witness = min(key for key, (lower, _) in oriented.items() if lower == best_lower)
    return {key for key, (_, upper) in oriented.items() if upper >= best_lower}, witness


def select_simulated_candidates(
    contract: StageScoreContract,
    candidates: tuple[SimulatedCandidate, ...],
    assessments: tuple[CandidateAssessment, ...],
    *,
    decision_point: str,
    elapsed_seconds: Decimal | str | int | None,
    incumbent_id: str | None = None,
) -> SimulationSelection:
    request = SimulationComparison.model_validate(
        {
            "contract": contract,
            "candidates": candidates,
            "assessments": assessments,
            "decision_point": decision_point,
            "elapsed_seconds": elapsed_seconds,
            "incumbent_id": incumbent_id,
        }
    )
    contract = request.contract
    if (
        contract.capability == "implementation-simulation-v1"
        and contract.profile_id != "implementation-plan-v1"
    ):
        raise ValueError("simulation-d1-only-supports-initial-plan-selection")
    if contract.loop_type == "local-pr-review":
        raise ValueError("simulation-local-pr-cannot-select-route")
    candidates = _checked_candidates(contract, request.candidates)
    rows = {row.candidate_id: row for row in candidates}
    assessments_by_id = {row.candidate_id: row for row in request.assessments}
    if len(assessments_by_id) != len(request.assessments) or set(
        assessments_by_id
    ) != set(rows):
        raise ValueError("simulation-assessments-must-cover-exactly-candidates")
    if incumbent_id is not None and incumbent_id not in rows:
        raise ValueError("simulation-incumbent-missing-from-current-batch")
    scores = {
        key: score_candidate(contract, row, assessments_by_id[key])
        for key, row in rows.items()
    }
    excluded: list[SimulationExclusion] = []
    limitations: list[str] = []
    eligible = []
    incumbent = (
        {
            row.criterion_id: row
            for row in assessments_by_id[incumbent_id].criterion_assessments
        }
        if incumbent_id
        else {}
    )
    for key, candidate in rows.items():
        reasons: list[tuple[SimulationExclusionReason, str | None]] = []
        if any(row.status != "PASS" for row in candidate.execution_preconditions):
            reasons.append(("precondition_not_passed", None))
        if (
            candidate.known_required_conflicts
            or assessments_by_id[key].known_required_conflicts
        ):
            reasons.append(("known_required_conflict", None))
        if assessments_by_id[key].cost_check == "incomplete":
            reasons.append(("cost_incomplete", None))
        elif assessments_by_id[key].cost_check == "unknown":
            reasons.append(("cost_unknown", None))
        estimate = candidate.future_cost_estimate
        if request.elapsed_seconds is None or estimate is None:
            reasons.append(("time_unknown", None))
        elif (
            candidate.cost_decision_point != request.decision_point
            or estimate.scope != contract.time_plan.scope
        ):
            reasons.append(("time_conditions_mismatch", None))
        elif Fraction(request.elapsed_seconds) + Fraction(
            estimate.upper_seconds
        ) > Fraction(contract.time_plan.window_seconds):
            reasons.append(("time_plan_exceeded", None))
        reasons.extend(("critical_floor", item) for item in scores[key].floor_failures)
        limitations.extend(
            f"floor_unresolved:{key}:{item}" for item in scores[key].floor_unknowns
        )
        if incumbent and key != incumbent_id:
            results = {
                row.criterion_id: row
                for row in assessments_by_id[key].criterion_assessments
            }
            for criterion in contract.criteria:
                if criterion.applicability == "applicable":
                    old, new = incumbent[criterion.id], results[criterion.id]
                    protected_regression = criterion.protected and (
                        new.lower < old.lower or new.upper < old.upper
                    )
                    floor_regression = (
                        contract.capability == "stage-simulation-v1"
                        and criterion.critical_floor is not None
                        and old.lower >= criterion.critical_floor > new.lower
                    )
                    if protected_regression or floor_regression:
                        reasons.append(("protected_regression", criterion.id))
        excluded.extend(
            SimulationExclusion(
                candidate_id=key, reason=reason, criterion_id=criterion_id
            )
            for reason, criterion_id in reasons
        )
        if not reasons:
            eligible.append(key)
    quality = remaining = tuple(eligible)
    selected: str | None = None
    basis: SimulationChoiceBasis | None = None
    reason: SimulationSelectionReason = (
        "model_plan_not_feasible"
        if any(row.reason.startswith(("time_", "cost_")) for row in excluded)
        else "no_safe_route"
    )
    if eligible:
        basis, reason = "unresolved_tie_fallback", "single_feasible_route"
        if len(eligible) == 1:
            limitations.append("no_alternative_comparison")
        else:
            survivors, witness = _interval_survivors(
                {key: (scores[key].s_low, scores[key].s_high) for key in eligible}
            )
            excluded.extend(
                SimulationExclusion(
                    candidate_id=key, reason="quality_preference", witness_id=witness
                )
                for key in eligible
                if key not in survivors
            )
            quality = remaining = tuple(sorted(survivors))
            if len(remaining) == 1:
                basis, reason = "forecast_preference", "quality_preference"
            else:
                limitations.append("quality_unresolved")
                times = {}
                for key in quality:
                    estimate = rows[key].future_cost_estimate
                    assert estimate is not None
                    times[key] = (
                        Fraction(estimate.lower_seconds),
                        Fraction(estimate.upper_seconds),
                    )
                survivors, witness = _interval_survivors(times, minimize=True)
                excluded.extend(
                    SimulationExclusion(
                        candidate_id=key,
                        reason="future_time_preference",
                        witness_id=witness,
                    )
                    for key in quality
                    if key not in survivors
                )
                remaining = tuple(sorted(survivors))
                if len(remaining) == 1:
                    reason = "future_time_preference"
                else:
                    limitations.append("future_time_unresolved")
                    reason = (
                        "incumbent_tie_break"
                        if incumbent_id in remaining
                        else "stable_id_tie_break"
                    )
        selected = incumbent_id if incumbent_id in remaining else remaining[0]
    return SimulationSelection(
        contract_digest=stage_contract_digest(contract),
        selected_id=selected,
        basis=basis,
        reason=reason,
        scores=scores,
        eligible_ids=tuple(eligible),
        quality_survivors=quality,
        remaining_ids=remaining,
        excluded=tuple(
            sorted(
                excluded,
                key=lambda row: (row.candidate_id, row.reason, row.criterion_id or ""),
            )
        ),
        limitations=tuple(limitations),
    )


def select_simulated_improvement(
    contract: StageScoreContract,
    candidates: tuple[SimulatedCandidate, ...],
    assessments: tuple[CandidateAssessment, ...],
    *,
    decision_point: str,
    elapsed_seconds: Decimal | str | int | None,
    incumbent_id: str,
    criterion_ids: tuple[str, ...],
) -> SimulationImprovementDecision:
    """改善须跨过同批基线的明确锚点；便宜、区间重叠或剩余轮次均不足。"""
    if contract.capability != "stage-simulation-v1":
        raise ValueError("simulation-d1-improvement-unsupported")
    if (
        contract.loop_type == "implementation"
        and contract.profile_id != "code-result-v1"
    ):
        raise ValueError("simulation-improvement-requires-code-view")
    selection = select_simulated_candidates(
        contract,
        candidates,
        assessments,
        decision_point=decision_point,
        elapsed_seconds=elapsed_seconds,
        incumbent_id=incumbent_id,
    )
    rows = {
        row.candidate_id: row
        for row in (CandidateAssessment.model_validate(a) for a in assessments)
    }
    old = {a.criterion_id: a for a in rows[incumbent_id].criterion_assessments}
    criteria = {c.id: c for c in contract.criteria if c.applicability == "applicable"}
    if (
        len(set(criterion_ids)) != len(criterion_ids)
        or not set(criterion_ids) <= criteria.keys()
    ):
        raise ValueError("simulation-improvement-criterion-unbound")
    directions = tuple(
        key
        for key in criterion_ids
        if old[key].lower < criteria[key].target_level
        or criteria[key].improvement_direction
    )
    reason = "comparison_inconclusive"
    selected_id = selection.selected_id
    qualifying = ()
    if not directions:
        reason = (
            "simulated_targets_met"
            if selection.scores[incumbent_id].targets_met
            else "no_supported_improvement"
        )
    elif selected_id is None:
        reason = (
            "model_plan_not_feasible"
            if selection.reason == "model_plan_not_feasible"
            else "no_supported_improvement"
        )
    elif selected_id != incumbent_id:
        new = {a.criterion_id: a for a in rows[selected_id].criterion_assessments}
        qualifying = tuple(
            sorted(key for key in directions if new[key].lower > old[key].upper)
        )
        before, after = selection.scores[incumbent_id], selection.scores[selected_id]
        if qualifying and after.s_low >= before.s_low and after.s_high >= before.s_high:
            return SimulationImprovementDecision(
                selection=selection,
                selected_id=selected_id,
                criterion_ids=qualifying,
            )
    return SimulationImprovementDecision(
        selection=selection, selected_id=None, stop_reason=reason
    )


def assess_current_staged_tree(
    contract: StageScoreContract,
    candidates: tuple[SimulatedCandidate, ...],
    assessments: tuple[CandidateAssessment, ...],
) -> SimulationSelection:
    """Local PR 只显示当前树的模拟诊断，不比较或许可实现路线。"""
    if (
        contract.capability != "stage-simulation-v1"
        or contract.loop_type != "local-pr-review"
    ):
        raise ValueError("simulation-local-pr-profile-required")
    rows = _checked_candidates(contract, candidates)
    if (
        len(rows) != 1
        or rows[0].candidate_id != "current-staged-tree"
        or len(assessments) != 1
    ):
        raise ValueError("simulation-local-pr-only-current-staged-tree")
    score = score_candidate(
        contract, rows[0], CandidateAssessment.model_validate(assessments[0])
    )
    return SimulationSelection(
        contract_digest=stage_contract_digest(contract),
        selected_id=rows[0].candidate_id,
        basis="unresolved_tie_fallback",
        reason="single_feasible_route",
        scores={rows[0].candidate_id: score},
        eligible_ids=(),
        quality_survivors=(),
        remaining_ids=(),
        limitations=(
            "current_tree_diagnostic_only",
            "no_execution_route_authorization",
        ),
    )

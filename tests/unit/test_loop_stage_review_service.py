"""阶段正式评审保留真实失败、历史原件与过期改善的只读收缩。"""

import json
import time

import pytest

from ai_sdlc.core.loop_review_models import B1ExpertResult, LoopReviewOutcome
from ai_sdlc.core.loop_review_service import (
    LoopReviewServiceError,
    RecordLoopReviewOptions,
    prepare_loop_review,
    record_loop_review,
    validate_prepared_outcome_for_close,
)
from ai_sdlc.core.review_kernel import ReviewExecution, ReviewInput
from tests.unit.test_loop_stage_decision_service import (
    assessments,
    host,
    sealed,
    snapshot,
)
from tests.unit.test_loop_stage_decision_service import stage_project as stage_project


@pytest.mark.parametrize(
    "stage,input_name",
    [
        ("requirement", "requirement-intake.json"),
        ("design-contract", "design-contract-input.json"),
        ("frontend-evidence", "frontend-evidence-input.json"),
    ],
)
@pytest.mark.parametrize("content", ["[]", "null", '"opaque text"', "{malformed"])
def test_legacy_nonimplementation_input_stays_opaque_but_stage_identity_is_strict(
    tmp_path, stage, input_name, content
):
    loop_id = "legacy-opaque"
    directory = tmp_path / ".ai-sdlc/loops" / stage / loop_id
    directory.mkdir(parents=True)
    run_path = directory / "loop-run.json"
    run_path.write_text(
        json.dumps({"loop_id": loop_id, "loop_type": stage}), encoding="utf-8"
    )
    (directory / input_name).write_text(content, encoding="utf-8")
    review = ReviewInput(
        loop_type=stage,
        loop_id=loop_id,
        round_number=1,
        input_digest="a" * 64,
        artifact_paths=[(directory / input_name).relative_to(tmp_path).as_posix()],
        expert_roles=["evidence-review"],
        expert_reasons={"evidence-review": "仍需阅读旧阶段的原始材料"},
    )

    def prepare_opaque():
        return prepare_loop_review(
            tmp_path,
            loop_type=stage,
            loop_id=loop_id,
            loop_dir=directory,
            input_resolver=lambda number: review,
        )

    assert prepare_opaque().status == "review_missing"
    run_path.write_text(
        json.dumps(
            {
                "loop_id": loop_id,
                "loop_type": stage,
                "decision_mode": "adaptive-quantified",
                "decision_capability": "stage-simulation-v1",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LoopReviewServiceError, match="decision-identity-mismatch"):
        prepare_opaque()


@pytest.fixture
def actual_stage(stage_project):
    root = stage_project
    current, context = sealed(root, host(root), improve=True)
    identity = {
        "decision_mode": "adaptive-quantified",
        "decision_capability": "stage-simulation-v1",
    }
    for name in ("loop-run.json", "implementation-input.json"):
        path = current.loop_dir / name
        existing = json.loads(path.read_bytes()) if path.exists() else {}
        path.write_text(json.dumps({**existing, **identity}), encoding="utf-8")
    return root, current, context


def prepare_current(fixture):
    root, current, context = fixture

    def resolve(number):
        return snapshot(
            root,
            current,
            context,
            round_number=number,
            observed_at_ms=time.time_ns() // 1_000_000,
        )

    return prepare_loop_review(
        root,
        loop_type=current.stage_kind,
        loop_id=current.loop_id,
        loop_dir=current.loop_dir,
        input_resolver=lambda number: resolve(number).review_input,
        b1_snapshot_resolver=resolve,
    )


def record(fixture, *, status="PASS"):
    root, current, context = fixture
    prepared = prepare_current(fixture)
    snap = prepared.b1_snapshot
    role = snap.review_input.expert_roles[0]
    receipt = B1ExpertResult(
        execution=ReviewExecution(
            status="completed",
            roles=[role],
            role_reasons=snap.review_input.expert_reasons,
        ),
        assessment=assessments(snap, status)[role],
    )
    directory = root / ".ai-sdlc/state/actual-review-results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"r{prepared.review_input.round_number}.json"
    path.write_text(receipt.model_dump_json(), encoding="utf-8")

    def resolve(number):
        return snapshot(
            root,
            current,
            context,
            round_number=number,
            observed_at_ms=time.time_ns() // 1_000_000,
        )

    result = record_loop_review(
        RecordLoopReviewOptions(
            root=root,
            loop_type=current.stage_kind,
            loop_id=current.loop_id,
            expected_digest=prepared.review_input.input_digest,
            result_paths=(path,),
        ),
        loop_dir=current.loop_dir,
        input_resolver=lambda number: resolve(number).review_input,
        b1_snapshot_resolver=resolve,
    )
    return result


def test_actual_r1_improve_is_not_clean_and_r2_uses_current_material(actual_stage):
    root, current, _ = actual_stage
    assert record(actual_stage).status == "needs_fix"
    first_path = current.loop_dir / "review-outcome-round-1.json"
    first_bytes = first_path.read_bytes()
    first = LoopReviewOutcome.model_validate_json(first_bytes)
    assert first.simulation.decision.action == "improve"
    with pytest.raises(
        LoopReviewServiceError, match="supported-conditional-improvement"
    ):
        prepared = prepare_current(actual_stage)
        validate_prepared_outcome_for_close(
            prepared, expected_digest=prepared.review_input.input_digest
        )
    current.actual_paths[0].write_text("R2当前改善成果", encoding="utf-8")
    (root / "source.md").write_text("R1许可后修正的原始来源", encoding="utf-8")
    prepared = prepare_current(actual_stage)
    assert prepared.review_input.round_number == 2
    assert prepared.status == "review_missing"
    assert record(actual_stage).status == "passed"
    prepared = prepare_current(actual_stage)
    validate_prepared_outcome_for_close(
        prepared, expected_digest=prepared.review_input.input_digest
    )
    assert first_path.read_bytes() == first_bytes


@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
def test_actual_failure_never_becomes_optional_improvement_or_false_close(
    actual_stage, status
):
    _, current, _ = actual_stage
    assert record(actual_stage, status=status).status == "needs_fix"
    outcome = LoopReviewOutcome.model_validate_json(
        (current.loop_dir / "review-outcome-round-1.json").read_bytes()
    )
    assert outcome.simulation.evaluation.h == 1
    assert outcome.simulation.decision.action == "repair"
    current.actual_paths[0].write_text("仍有真实缺口的R2", encoding="utf-8")
    assert record(actual_stage, status="FAIL").status == "needs_user"
    prepared = prepare_current(actual_stage)
    with pytest.raises(LoopReviewServiceError, match="review-round-limit"):
        validate_prepared_outcome_for_close(
            prepared, expected_digest=prepared.review_input.input_digest
        )


def test_expired_unchanged_passed_r1_can_close_without_mutating_outcome(
    actual_stage, monkeypatch
):
    _, current, _ = actual_stage
    assert record(actual_stage).status == "needs_fix"
    path = current.loop_dir / "review-outcome-round-1.json"
    original = path.read_bytes()
    monkeypatch.setattr(time, "time_ns", lambda: 4_000_000_000_000)
    prepared = prepare_current(actual_stage)
    assert prepared.status == "passed"
    validate_prepared_outcome_for_close(
        prepared, expected_digest=prepared.review_input.input_digest
    )
    assert path.read_bytes() == original
    assert not (current.loop_dir / "review-outcome-round-2.json").exists()


def test_expiry_after_material_changed_still_requires_r2(actual_stage, monkeypatch):
    _, current, _ = actual_stage
    record(actual_stage)
    current.actual_paths[0].write_text("未经实际复审的新成果", encoding="utf-8")
    monkeypatch.setattr(time, "time_ns", lambda: 4_000_000_000_000)
    prepared = prepare_current(actual_stage)
    assert (
        prepared.status == "review_missing" and prepared.review_input.round_number == 2
    )
    with pytest.raises(LoopReviewServiceError, match="review-result-missing"):
        validate_prepared_outcome_for_close(
            prepared, expected_digest=prepared.review_input.input_digest
        )


def test_last_precommit_clock_check_shrinks_same_r1_before_atomic_write(
    actual_stage, monkeypatch
):
    import ai_sdlc.core.loop_review_service as service

    original = service._write_outcome

    def expire_then_write(*args, **kwargs):
        monkeypatch.setattr(time, "time_ns", lambda: 4_000_000_000_000)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_write_outcome", expire_then_write)
    assert record(actual_stage).status == "passed"
    _, current, _ = actual_stage
    data = LoopReviewOutcome.model_validate_json(
        (current.loop_dir / "review-outcome-round-1.json").read_bytes()
    ).simulation
    assert data.decision.action == "stop"
    assert data.decision.reason == "model_plan_not_feasible"
    assert data.observed_at_ms == 4_000_000


@pytest.mark.parametrize(
    "stage,input_name",
    [
        ("requirement", "requirement-intake.json"),
        ("design-contract", "design-contract-input.json"),
        ("frontend-evidence", "frontend-evidence-input.json"),
    ],
)
def test_every_stage_records_and_closes_actual_data(stage_project, stage, input_name):
    root = stage_project
    current, context = sealed(root, host(root, stage))
    identity = {
        "decision_mode": "adaptive-quantified",
        "decision_capability": "stage-simulation-v1",
    }
    for name in ("loop-run.json", input_name):
        path = current.loop_dir / name
        existing = json.loads(path.read_bytes()) if path.exists() else {}
        path.write_text(json.dumps({**existing, **identity}), encoding="utf-8")
    fixture = (root, current, context)
    assert record(fixture).status == "passed"
    prepared = prepare_current(fixture)
    validate_prepared_outcome_for_close(
        prepared, expected_digest=prepared.review_input.input_digest
    )
    (current.loop_dir / input_name).write_text("{}", encoding="utf-8")
    with pytest.raises(LoopReviewServiceError, match="decision-identity-mismatch"):
        prepare_current(fixture)

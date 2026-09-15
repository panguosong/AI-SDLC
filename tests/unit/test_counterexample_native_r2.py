"""原 R1/R2 接缝的有界测试；合成评审材料不代表独立业务效果。"""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_sdlc.cli import loop_review_cmd as review_cmd
from ai_sdlc.core import counterexample_execution as execution
from ai_sdlc.core.counterexample_models import CounterexamplePlan, counterexample_digest
from ai_sdlc.core.loop_decision_service import (
    _require_stage_implementation_execution,
    build_b1_review_data,
)
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from tests.unit.test_counterexample_models import make_contract, plan_data
from tests.unit.test_loop_stage_decision_service import (
    assessments,
    host,
    sealed,
    snapshot,
)

pytest_plugins = ("tests.unit.test_loop_stage_decision_service",)


def _outcome(root, *, status="PASS", improve=True):
    current, context = sealed(root, host(root), improve=improve)
    snap = snapshot(root, current, context)
    data = build_b1_review_data(
        snap, assessments(snap, status), has_actionable_findings=False
    )
    outcome = LoopReviewOutcome(
        loop_id=current.loop_id,
        loop_type=current.stage_kind,
        round_number=1,
        input_digest=snap.review_input.input_digest,
        status="completed",
        expert_roles=snap.review_input.expert_roles,
        recorded_at="2026-09-07T00:00:00Z",
        simulation=data,
        infra_retry_count=0,
    )
    path = current.loop_dir / "review-outcome-round-1.json"
    path.write_text(outcome.model_dump_json(), encoding="utf-8")
    return current, snap, outcome, path


@pytest.mark.parametrize(
    "expired,drift,required",
    [(False, False, True), (True, False, False), (True, True, True)],
)
@pytest.mark.parametrize("internal_material", [False, True])
def test_native_r2_uses_effective_improvement_action(
    stage_project, monkeypatch, expired, drift, required, internal_material
):
    root = stage_project
    current, snap, outcome, path = _outcome(root)
    assert outcome.simulation.decision.action == "improve"
    original = path.read_bytes()
    if expired:
        snap = replace(snap, observed_at_ms=4_000_000)
    if drift:
        snap = replace(snap, source_digest="b" * 64)

    def actual_snapshot(*args, **kwargs):
        assert review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get()
        return snap

    monkeypatch.setattr(review_cmd, "resolve_b1_review_snapshot", actual_snapshot)
    material = (
        execution._CurrentStateMaterial(root, current.loop_id, {})
        if internal_material
        else None
    )
    actual, refs = execution._native_counterexample_r2(
        root, SimpleNamespace(loop_id=current.loop_id), material
    )
    assert actual is required
    # 已有原 R1 即使不再要求改善，也须随状态捕获，不能伪装为从未评审。
    assert len(refs) == 1 and refs[0].sha256 == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert not review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get()
    if material is not None:
        material.verify_originals()
        path.write_bytes(original + b"\n")
        with pytest.raises(ValueError, match="artifact-content-stale"):
            material.verify_originals()


@pytest.mark.parametrize("damage", [None, "missing", "stale"])
def test_effective_action_cannot_fill_missing_captured_material(
    stage_project, monkeypatch, damage
):
    root = stage_project
    current, snap, _, path = _outcome(root)
    captured = {key: (root / key).read_bytes() for key in snap.manifest}
    captured[path.relative_to(root).as_posix()] = path.read_bytes()
    key = next(iter(snap.manifest))
    if damage == "missing":
        del captured[key]
    elif damage == "stale":
        captured[key] = b"changed original"
    monkeypatch.setattr(
        review_cmd,
        "resolve_b1_review_snapshot",
        lambda *a, **k: replace(snap, observed_at_ms=4_000_000),
    )
    if damage:
        with pytest.raises(ValueError, match="counterexample-captured-evidence-"):
            execution._native_counterexample_r2(
                root, SimpleNamespace(loop_id=current.loop_id), captured
            )
    else:
        required, refs = execution._native_counterexample_r2(
            root, SimpleNamespace(loop_id=current.loop_id), captured
        )
        assert not required
        assert len(refs) == 1 and refs[0].path == path.relative_to(root).as_posix()


def test_real_repair_stays_required_without_improvement_snapshot(
    stage_project, monkeypatch
):
    root = stage_project
    current, _, outcome, _ = _outcome(root, status="UNKNOWN", improve=False)
    assert outcome.simulation.decision.action == "repair"
    monkeypatch.setattr(
        review_cmd,
        "resolve_b1_review_snapshot",
        lambda *a, **k: pytest.fail("repair needs no improvement replay"),
    )
    assert execution._native_counterexample_r2(
        root, SimpleNamespace(loop_id=current.loop_id)
    )[0]


@pytest.mark.parametrize("damage", [None, "missing", "malformed", "other-loop"])
def test_fix25_captured_native_phase_requires_its_original_r1(
    stage_project, monkeypatch, damage
):
    root = stage_project
    current, _, outcome, path = _outcome(root, status="UNKNOWN", improve=False)
    original = path.read_bytes()
    key = path.relative_to(root).as_posix()
    captured = {key: original}
    if damage == "missing":
        captured.clear()
    elif damage == "malformed":
        captured[key] = b"not an outcome"
    elif damage == "other-loop":
        captured[key] = outcome.model_copy(update={"loop_id": "another-loop"}).model_dump_json().encode()
    monkeypatch.setattr(
        execution, "read_stable_bytes",
        lambda *a: pytest.fail("captured phase must not supplement the live R1"),
    )
    if damage:
        with pytest.raises(ValueError):
            execution._native_counterexample_r2(
                root, SimpleNamespace(loop_id=current.loop_id), captured
            )
    else:
        required, refs = execution._native_counterexample_r2(
            root, SimpleNamespace(loop_id=current.loop_id), captured
        )
        assert required and len(refs) == 1
        assert refs[0].path == key and refs[0].sha256 == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_fix25_native_pre_r1_absence_and_explicit_capture_remain_supported(
    git_repo, monkeypatch
):
    impl, progress = _state_project(git_repo, monkeypatch)
    assert execution._native_counterexample_r2(git_repo, impl) == (False, ())
    material = execution._CurrentStateMaterial(git_repo, impl.loop_id, {})
    assert execution._native_counterexample_r2(git_repo, impl, material) == (False, ())
    material.verify_originals()
    for captured in (None, {"spec.md": b"original"}):
        blockers, _, _ = execution.counterexample_verification_state(
            git_repo, impl, progress, captured_artifacts=captured,
            require_r2=False, require_completion=False,
        )
        assert not blockers
    # 普通缺件集合没有原生阶段依据，不得自动晋升为合法的前 R1 捕获。
    with pytest.raises(ValueError, match="captured-review-phase"):
        execution.counterexample_verification_state(
            git_repo, impl, progress, captured_artifacts={"spec.md": b"original"},
            require_completion=False,
        )


def test_execute_admission_uses_capture_scope_and_original_time_gate(
    stage_project, monkeypatch
):
    root = stage_project
    current, snap, outcome, _ = _outcome(root, status="UNKNOWN", improve=False)
    seen = []

    def prepare(*args):
        seen.append(review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get())
        return SimpleNamespace(
            baseline_outcome=None, current_outcome=outcome, status="needs_fix"
        ), current.loop_dir

    monkeypatch.setattr(review_cmd, "prepare_current_loop_review", prepare)
    monkeypatch.setattr(
        "ai_sdlc.core.loop_decision_service.require_simulation_time_admission",
        lambda *a, **k: seen.append(k["execution_started"]),
    )
    _require_stage_implementation_execution(root, current, snap.context)
    assert seen == [True, True]
    assert not review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get()


def test_execution_capture_is_reset_after_error():
    with pytest.raises(ValueError), review_cmd._counterexample_execution_capture():
        raise ValueError("retained original failure")
    assert not review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get()


def test_effective_improvement_report_capture_does_not_reenter_r2(
    stage_project, monkeypatch
):
    from ai_sdlc.core.implementation_loop import _counterexample_state

    root = stage_project
    current, snap, _, _ = _outcome(root)
    impl, progress = _state_project(root, monkeypatch)
    impl.loop_id = current.loop_id
    impl.verification_capability = "counterexample-verification-v1"
    calls = []

    def report_snapshot(*args, **kwargs):
        calls.append(True)
        assert len(calls) == 1, (
            "report capture recursively resolved its own improvement"
        )
        blockers, _, _ = _counterexample_state(root, impl, progress)
        assert not blockers
        return snap

    monkeypatch.setattr(review_cmd, "resolve_b1_review_snapshot", report_snapshot)
    assert execution._native_counterexample_r2(root, impl)[0]
    assert calls == [True]


def _state_project(root, monkeypatch):
    contract = make_contract()
    # 声明的捕获原字节也须实际存在，供状态返回前的原件复核读取。
    (root / "spec.md").write_bytes(b"original")
    monkeypatch.setattr(
        "ai_sdlc.core.implementation_store.validate_implementation_verification_contract",
        lambda *a: (contract, {"spec.md": b"original"}),
    )
    monkeypatch.setattr(
        execution, "build_source_digest", lambda *a: "sha256:" + "a" * 64
    )
    return SimpleNamespace(
        loop_id="sample", task_scopes={"T01": ["src/save.py"]}
    ), SimpleNamespace(tasks=[])


def test_capture_defers_only_completion_and_preserves_original_bytes(
    git_repo, monkeypatch
):
    impl, progress = _state_project(git_repo, monkeypatch)
    monkeypatch.setattr(
        execution,
        "_native_counterexample_r2",
        lambda *a: pytest.fail("R1 must not include its own outcome"),
    )
    complete = execution.counterexample_verification_state(
        git_repo, impl, progress, require_r2=False
    )
    capture = execution.counterexample_verification_state(
        git_repo, impl, progress, require_r2=False, require_completion=False
    )
    assert complete[0] == [
        "Counterexample evidence for the current source is missing or stale."
    ]
    assert capture[0] == []
    assert complete[0][0] in capture[1]
    assert complete[2] == capture[2]
    assert capture[2][0].sha256 == hashlib.sha256(b"original").hexdigest()


def test_capture_keeps_corrupt_evidence_blocking(git_repo, monkeypatch):
    impl, progress = _state_project(git_repo, monkeypatch)
    progress.tasks = [SimpleNamespace(counterexample_results=["broken original"])]

    def broken(*a, **k):
        raise ValueError("counterexample-artifact-content-stale")

    monkeypatch.setattr(execution, "resolve_counterexample_evidence", broken)
    blockers, _, _ = execution.counterexample_verification_state(
        git_repo, impl, progress, require_r2=False, require_completion=False
    )
    assert "counterexample-artifact-content-stale" in blockers


@pytest.mark.parametrize(
    "round_number,capture,expected_r2",
    [(1, False, False), (2, False, None), (2, True, False)],
)
def test_stage_review_r1_never_adds_its_own_outcome(
    tmp_path, monkeypatch, round_number, capture, expected_r2
):
    from ai_sdlc.core.implementation_models import (
        ImplementationInput,
        ImplementationProgress,
    )

    contract = make_contract()
    contract_path = tmp_path / "specs/sample/verification.json"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(contract.model_dump_json())
    # 本测试隔离原生 R1/R2 的捕获时点；输入须真实声明其模拟校验器返回的能力。
    impl = ImplementationInput(
        loop_id="sample",
        work_item_id="sample",
        work_item_path="specs/sample",
        spec_path="specs/sample/spec.md",
        plan_path="specs/sample/plan.md",
        tasks_path="specs/sample/tasks.md",
        design_contract_loop_id="design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
        verification_capability="counterexample-acceptance-v1",
        verification_contract_ref="specs/sample/verification.json",
        verification_contract_digest=counterexample_digest(contract),
    )
    loop_dir = tmp_path / ".ai-sdlc/loops/implementation/sample"
    loop_dir.mkdir(parents=True)
    (loop_dir / "implementation-input.json").write_text(impl.model_dump_json())
    (loop_dir / "implementation-progress.json").write_text(
        ImplementationProgress(
            loop_id="sample", work_item_id="sample"
        ).model_dump_json()
    )
    monkeypatch.setattr(
        "ai_sdlc.core.implementation_store.validate_implementation_verification_contract",
        lambda *a: (contract, {"specs/sample/verification.json": contract_path.read_bytes()}),
    )
    monkeypatch.setattr(review_cmd, "_implementation_evidence_material", lambda *a: [])
    calls = []

    def state(*args, **kwargs):
        calls.append(kwargs)
        return [], [], ()

    monkeypatch.setattr(execution, "counterexample_verification_state", state)
    if capture:
        with review_cmd._counterexample_execution_capture():
            review_cmd._stage_source_material(
                tmp_path, "implementation", loop_dir, review_round_number=round_number
            )
    else:
        review_cmd._stage_source_material(
            tmp_path, "implementation", loop_dir, review_round_number=round_number
        )
    assert calls == [
        {
            "require_r2": expected_r2,
            "require_completion": not capture,
            "active_plan_digest": None,
        }
    ]


def test_active_plan_identity_survives_nested_admission_and_is_reset():
    with review_cmd._counterexample_execution_capture("a" * 64):
        with review_cmd._counterexample_execution_capture():
            assert review_cmd._COUNTEREXAMPLE_ACTIVE_PLAN.get() == "a" * 64
        with (
            pytest.raises(ValueError),
            review_cmd._counterexample_execution_capture("b" * 64),
        ):
            raise ValueError("preserve the prior scope")
        assert review_cmd._COUNTEREXAMPLE_ACTIVE_PLAN.get() == "a" * 64
    assert review_cmd._COUNTEREXAMPLE_ACTIVE_PLAN.get() is None
    assert not review_cmd._COUNTEREXAMPLE_EXECUTION_CAPTURE.get()


@pytest.mark.parametrize("improve", [False, True])
def test_saved_native_phase_replays_original_r1_after_clock_and_current_r1_change(
    stage_project, monkeypatch, improve
):
    root = stage_project
    current, snap, _, path = _outcome(
        root, status="PASS" if improve else "UNKNOWN", improve=improve
    )
    monkeypatch.setattr(review_cmd, "resolve_b1_review_snapshot", lambda *a, **k: snap)
    captured = {}
    required, _ = execution._native_counterexample_r2(
        root, SimpleNamespace(loop_id=current.loop_id), phase_capture=captured
    )
    assert required
    plan = CounterexamplePlan.model_validate(plan_data()).model_copy(
        update={"loop_id": current.loop_id}
    )
    phase = execution._save_phase_context(root, plan, required, captured)
    record = SimpleNamespace(phase_context=phase, recorded_at_ms=phase.observed_at_ms)
    original = path.read_bytes()
    path.write_bytes(b"a later R1 does not replace archived phase evidence")
    monkeypatch.setattr(execution.time, "time_ns", lambda: 10**25)
    assert execution._record_requires_r2(
        record, plan, lambda ref: execution._read_ref(root, ref)
    )
    assert (root / phase.review_ref.path).read_bytes() == original
    record.phase_context = phase.model_copy(update={"require_r2": False})
    with pytest.raises(ValueError, match="phase-requirement-mismatch"):
        execution._record_requires_r2(
            record, plan, lambda ref: execution._read_ref(root, ref)
        )


def test_saved_phase_cannot_claim_r2_without_original_review():
    record = SimpleNamespace(
        recorded_at_ms=10,
        phase_context=SimpleNamespace(
            require_r2=True, observed_at_ms=10, review_ref=None, snapshot_ref=None
        ),
    )
    plan = CounterexamplePlan.model_validate(plan_data())
    with pytest.raises(ValueError, match="phase-review-required"):
        execution._record_requires_r2(
            record,
            plan,
            lambda ref: pytest.fail("missing review must not read a replacement"),
        )

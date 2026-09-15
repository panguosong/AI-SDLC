"""原共享锁的异常归属与公共出口；测试适配边界，不合成业务验收通过。"""

from __future__ import annotations

import errno
import json
import os
from functools import partial
from types import SimpleNamespace

import pytest

from ai_sdlc.core import loop_resource_lock as locks


@pytest.mark.parametrize("phase", ["directory", "open", "acquire"])
def test_q003_acquisition_errors_are_typed_and_close_opened_descriptor(
    tmp_path, monkeypatch, phase
):
    directory = tmp_path / "locks"
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: directory)
    captured = []
    failure = PermissionError(errno.EACCES, "test lock permission")
    if phase == "directory":
        directory.write_text("This ordinary file cannot be the lock directory.")
    elif phase == "open":
        def denied_open(*args, **kwargs):
            raise failure
        monkeypatch.setattr(locks.os, "open", denied_open)
    else:
        def failed_acquire(descriptor):
            captured.append(descriptor)
            raise failure
        monkeypatch.setattr(locks, "_acquire_implementation_file_lock", failed_acquire)
    entered = False
    with (
        pytest.raises(locks._ImplementationWriteLockError) as denied,
        locks._stage_write_guard(tmp_path, "requirement", "same-loop"),
    ):
        entered = True
    assert not entered
    assert isinstance(denied.value.__cause__, OSError)
    if phase == "directory":
        assert isinstance(denied.value.__cause__, FileExistsError)
        assert directory.read_text() == "This ordinary file cannot be the lock directory."
    else:
        assert denied.value.__cause__ is failure
    for descriptor in captured:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF
    assert not (tmp_path / ".ai-sdlc").exists()


@pytest.mark.parametrize("failure_type", [OSError, ValueError, RuntimeError])
def test_q003_locked_body_error_keeps_its_identity_and_releases_lock(
    tmp_path, monkeypatch, failure_type
):
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: tmp_path / "locks")
    failure = failure_type("ordinary operation failed")
    with (
        pytest.raises(failure_type) as raised,
        locks._stage_write_guard(tmp_path, "requirement", "same-loop"),
    ):
        raise failure
    assert raised.value is failure
    assert not isinstance(raised.value, locks._ImplementationWriteLockError)
    with locks._stage_write_guard(tmp_path, "requirement", "same-loop"):
        pass


def _stage_wrapper_case(root, monkeypatch, case, operation):
    loop_id = "same-loop"
    if case.startswith("requirement"):
        from ai_sdlc.core import requirement_loop as module
        if case == "requirement-start":
            options = module.RequirementStartOptions(root=root, loop_id=loop_id, dry_run=True)
            public, target = module.start_requirement_loop, "_start_requirement_loop_locked"
        else:
            options = module.RequirementFreezeOptions(root=root, loop_id=loop_id)
            monkeypatch.setattr(module, "_resolve_requirement_loop_run_path", lambda *args: (root / "run.json", loop_id, ""))
            public, target = module.freeze_requirement_loop, "_freeze_requirement_loop_locked"
    elif case.startswith("design"):
        from ai_sdlc.core import design_contract_loop as module
        if case == "design-check":
            options = module.DesignContractCheckOptions(root=root, loop_id=loop_id, dry_run=True)
            monkeypatch.setattr(module, "_prepare_design_check", lambda *args: (root, None, loop_id, None, None))
            monkeypatch.setattr(module, "_design_publication_pending", lambda *args: False)
            public, target = module.check_design_contract_loop, "_check_design_contract_loop_locked"
        else:
            options = module.DesignContractCloseOptions(root=root, loop_id=loop_id)
            monkeypatch.setattr(module, "_resolve_design_contract_loop_run_identity", lambda *args: (root / "run.json", loop_id, ""))
            public, target = module.close_design_contract_loop, "_close_design_contract_loop_locked"
    else:
        from ai_sdlc.core import frontend_evidence_loop as module
        if case == "frontend-start":
            options = module.FrontendEvidenceStartOptions(root=root, loop_id=loop_id, dry_run=True)
            public, target = module.start_frontend_evidence_loop, "_start_frontend_evidence_loop_locked"
        elif case == "frontend-skip":
            options = module.FrontendEvidenceSkipOptions(root=root, loop_id=loop_id)
            public, target = module.skip_frontend_evidence_loop, "_skip_frontend_evidence_loop_locked"
        else:
            options = module.FrontendEvidenceCloseOptions(root=root, loop_id=loop_id)
            run = root / ".ai-sdlc/loops/frontend-evidence" / loop_id / "loop-run.json"
            monkeypatch.setattr(module, "resolve_frontend_evidence_loop_run_path", lambda *args: (run, ""))
            public, target = module.close_frontend_evidence_loop, "_close_frontend_evidence_loop_locked"
    # 只隔离锁前的身份准备和锁后的业务体；公共 wrapper 与共享文件锁均为实际实现。
    monkeypatch.setattr(module, target, operation)
    return lambda: public(options)


@pytest.mark.parametrize("case", [
    "requirement-start", "requirement-freeze", "design-check", "design-close",
    "frontend-start", "frontend-skip", "frontend-close",
])
def test_q003_stage_public_wrappers_map_acquisition_only(tmp_path, monkeypatch, case):
    directory = tmp_path / "lock-location"
    directory.write_text("occupied")
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: directory)
    calls = []
    sentinel = object()
    body_error = [None]

    def operation(*args, **kwargs):
        calls.append((args, kwargs))
        if body_error[0] is not None:
            raise body_error[0]
        return sentinel

    invoke = _stage_wrapper_case(tmp_path, monkeypatch, case, operation)
    blocked = invoke()
    assert blocked.status == "blocked" and blocked.loop_id == "same-loop"
    assert "write lock is unavailable" in blocked.blocker
    if case.endswith(("start", "check")):
        assert blocked.dry_run is True
    assert not calls and directory.read_text() == "occupied"
    directory.unlink()
    assert invoke() is sentinel
    assert len(calls) == 1
    body_error[0] = RuntimeError("operation failure after acquisition")
    with pytest.raises(RuntimeError) as raised:
        invoke()
    assert raised.value is body_error[0]


@pytest.mark.parametrize("kind", ["stage", "legacy-simulation", "pr"])
def test_q003_decision_public_wrappers_keep_typed_boundary(tmp_path, monkeypatch, kind):
    from ai_sdlc.core.loop_decision_service import DecisionPreparationError
    from ai_sdlc.core.loop_models import LoopRun
    from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest

    directory = tmp_path / "lock-location"
    directory.write_text("occupied")
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: directory)
    request = SimulationPrepareRequest(operation="seal-for-review", request_id="same-input")
    calls = []
    body_error = [None]
    preview = SimpleNamespace(status="preview", prepare_digest="same-digest")
    existing = SimpleNamespace(status="existing")

    def prepare(*args, **kwargs):
        calls.append(1)
        inside = kind in ("pr", "legacy-simulation") or len(calls) == 2
        if inside and body_error[0] is not None:
            raise body_error[0]
        return existing if inside else preview

    if kind == "stage":
        from ai_sdlc.core import loop_stage_decision_service as module
        monkeypatch.setattr(module, "_prepare_stage", prepare)
        invoke = partial(module.prepare_stage_simulation_decision,
            tmp_path, "requirement", "same-loop", request,
            host_resolver=lambda: None, dry_run=False, expected_digest="same-digest",
        )
    elif kind == "legacy-simulation":
        from ai_sdlc.core import loop_decision_service as module
        path = tmp_path / ".ai-sdlc/loops/implementation/same-loop/loop-run.json"
        path.parent.mkdir(parents=True)
        path.write_text(LoopRun(
            loop_id="same-loop", loop_type="implementation",
            decision_mode="adaptive-quantified", decision_capability="implementation-simulation-v1",
        ).model_dump_json())
        monkeypatch.setattr(module, "_simulation_prepare", prepare)
        invoke = partial(module.prepare_simulation_decision,
            tmp_path, "same-loop", request, dry_run=False, expected_digest="same-digest",
        )
    else:
        from ai_sdlc.core import pr_review_decision as module
        monkeypatch.setattr(module, "_prepare", prepare)
        invoke = partial(module.prepare_pr_review_decision, tmp_path, "same-review", request)
    with pytest.raises(DecisionPreparationError) as denied:
        invoke()
    assert isinstance(denied.value.__cause__, locks._ImplementationWriteLockError)
    assert len(calls) == (0 if kind in ("pr", "legacy-simulation") else 1)
    directory.unlink()
    calls.clear()
    assert invoke() is existing
    calls.clear()
    body_error[0] = RuntimeError("ordinary decision body failure")
    with pytest.raises(RuntimeError) as raised:
        invoke()
    assert raised.value is body_error[0]


@pytest.mark.parametrize("stage", ["requirement", "design-contract", "implementation", "frontend-evidence", "local-pr-review"])
def test_q003_formal_record_maps_shared_lock_without_entering_writer(tmp_path, monkeypatch, stage):
    from ai_sdlc.core import loop_review_service as module

    loop_id = "loop-same"
    if stage == "local-pr-review":
        loop_dir = tmp_path / ".ai-sdlc/reviews/pr/review-same"
        run_name = "review-run.json"
    else:
        loop_dir = tmp_path / ".ai-sdlc/loops" / stage / loop_id
        run_name = "loop-run.json"
    loop_dir.mkdir(parents=True)
    (loop_dir / run_name).write_text(json.dumps({"decision_mode": "adaptive-quantified"}))
    directory = tmp_path / "lock-location"
    directory.write_text("occupied")
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: directory)
    calls = []
    sentinel = object()

    def writer(*args, **kwargs):
        calls.append(1)
        return sentinel

    monkeypatch.setattr(module, "_record_loop_review_locked", writer)
    options = module.RecordLoopReviewOptions(root=tmp_path, loop_type=stage, loop_id=loop_id,
                                             expected_digest="a" * 64, result_paths=())
    invoke = partial(module.record_loop_review, options, loop_dir=loop_dir, input_resolver=lambda number: None)
    with pytest.raises(module.LoopReviewServiceError, match="review-outcome-lock-unavailable") as denied:
        invoke()
    assert isinstance(denied.value.__cause__, locks._ImplementationWriteLockError)
    assert not calls and not list(loop_dir.glob("review-outcome*"))
    directory.unlink()
    assert invoke() is sentinel and calls == [1]


def _pr_run(root, monkeypatch):
    from ai_sdlc.core import pr_review_service as service
    from ai_sdlc.core.pr_review_models import ReviewRun

    run = ReviewRun(review_id="same-review", loop_id="loop-same-review", provider_id="mock-reviewer",
                    decision_mode="adaptive-quantified", decision_capability="stage-simulation-v1",
                    staged_tree_oid="a" * 40, decision_staged_tree_oid="a" * 40,
                    diff_source={"source_kind": "local-staged"})
    monkeypatch.setattr(service, "_load_current_review_run", lambda *args, **kwargs: (run, root / "review-run.json"))
    return service


@pytest.mark.parametrize("name,kwargs", [
    ("verify_pr_review_command", {"cwd": ".", "argv": ("echo", "never-run")}),
    ("commit_pr_review", {"message": "never-commit"}),
    ("fix_pr_review", {"dry_run": True}),
    ("close_pr_review", {}),
    ("rerun_pr_review", {}),
])
def test_q003_quantified_pr_public_wrappers_preserve_result_kind(tmp_path, monkeypatch, name, kwargs):
    service = _pr_run(tmp_path, monkeypatch)
    directory = tmp_path / "lock-location"
    directory.write_text("occupied")
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: directory)
    result = getattr(service, name)(tmp_path, **kwargs)
    assert result.status == "blocked" and "write lock is unavailable" in result.blocker
    assert not (tmp_path / ".ai-sdlc").exists()
    if name == "fix_pr_review":
        assert result.dry_run is True
    if name == "rerun_pr_review":
        assert result.provider_id == "mock-reviewer"


@pytest.mark.parametrize("failure_type", [OSError, ValueError, RuntimeError])
def test_q003_quantified_pr_guard_does_not_swallow_business_error(tmp_path, monkeypatch, failure_type):
    service = _pr_run(tmp_path, monkeypatch)
    monkeypatch.setattr(locks, "_implementation_lock_dir", lambda root: tmp_path / "locks")
    failure = failure_type("ordinary PR operation failure")

    def operation(root):
        raise failure

    operation.__name__ = "commit_pr_review"
    guarded = service._quantified_pr_write_guard(operation)
    with pytest.raises(failure_type) as raised:
        guarded(tmp_path)
    assert raised.value is failure

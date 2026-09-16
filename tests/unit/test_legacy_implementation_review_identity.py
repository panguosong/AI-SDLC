"""窄身份必须与原整体评审摘要来自同一文件捕获。"""

import json

import pytest

from ai_sdlc.cli.loop_review_cmd import _bind_implementation_review_identity
from ai_sdlc.core import review_kernel
from ai_sdlc.core.implementation_models import ImplementationInput
from ai_sdlc.core.implementation_store import implementation_input_digest


def test_review_identity_uses_captured_bytes_not_a_later_input_read(tmp_path, monkeypatch):
    loop_id = "capture-identity"
    relative = f".ai-sdlc/loops/implementation/{loop_id}/implementation-input.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    original = ImplementationInput(
        loop_id=loop_id, work_item_id="demo", work_item_path="specs/demo",
        spec_path="specs/demo/spec.md", plan_path="specs/demo/plan.md",
        tasks_path="specs/demo/tasks.md", design_contract_loop_id="original",
        design_contract_report_path=".ai-sdlc/loops/design-contract/original/design-contract-report.json",
    )
    path.write_text(original.model_dump_json())
    arguments = dict(
        loop_id=loop_id, loop_type="implementation", round_number=1,
        artifact_paths=[relative], upstream_context_paths=[], risk_signals=[],
    )
    def capture_review():
        captured = {}
        reviewed = review_kernel.build_review_input(
            tmp_path, **arguments, capture_artifact_paths=[relative],
            captured_artifacts=captured,
        )
        return _bind_implementation_review_identity(reviewed, captured)

    baseline = capture_review()
    read_paths = review_kernel._read_paths

    def mutate_after_capture(*args, **kwargs):
        result = read_paths(*args, **kwargs)
        if result:
            path.write_text(original.model_copy(update={
                "design_contract_loop_id": "redirected",
            }).model_dump_json())
        return result

    monkeypatch.setattr(review_kernel, "_read_paths", mutate_after_capture)
    reviewed = capture_review()
    assert reviewed == baseline
    assert reviewed.implementation_input_digest == implementation_input_digest(original)
    assert ImplementationInput.model_validate_json(path.read_bytes()).design_contract_loop_id == "redirected"


def _old_review_case(root):
    from ai_sdlc.cli.loop_review_cmd import resolve_review_input
    from ai_sdlc.core.implementation_loop import (
        ImplementationRecordOptions,
        ImplementationStartOptions,
        record_implementation_progress,
        start_implementation_loop,
    )
    from tests.unit.test_implementation_loop import (
        _close_design_contract_for_work_item,
        _record_successful_quality_result,
        _write_clean_implementation_review,
        _write_ready_work_item,
    )

    work_item = _write_ready_work_item(root)
    _close_design_contract_for_work_item(root, work_item)
    loop_id = "old-review-input"
    assert start_implementation_loop(ImplementationStartOptions(
        root=root, work_item="specs/demo-implementation-loop",
        design_contract_loop_id="dc-demo-implementation-loop", loop_id=loop_id,
    )).status == "ready"
    assert record_implementation_progress(ImplementationRecordOptions(
        root=root, loop_id=loop_id, task_id="T11", status="done",
        verification="python -c pass",
    )).status == "ready"
    _record_successful_quality_result(root, loop_id, "T11")
    reviewed = resolve_review_input(
        root, loop_type="implementation", loop_id=loop_id, review_round_number=1,
    )
    _write_clean_implementation_review(root, loop_id, reviewed)
    directory = root / ".ai-sdlc/loops/implementation" / loop_id
    outcome = directory / "review-outcome-round-1.json"
    assert "implementation_input_digest" not in json.loads(outcome.read_bytes())
    return directory, reviewed


def _make_actionable(outcome):
    data = json.loads(outcome.read_bytes())
    data["findings"] = [{
        "severity": "important", "role": data["expert_roles"][0],
        "location": "implementation", "summary": "Fix required",
        "recommendation": "Repair before closing",
    }]
    outcome.write_text(json.dumps(data))


def test_old_actionable_review_can_revalidate_identity_without_claiming_pass(tmp_path):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.loop_decision_service import validate_implementation_context
    from ai_sdlc.core.loop_models import LoopRun

    directory, reviewed = _old_review_case(tmp_path)
    _make_actionable(directory / "review-outcome-round-1.json")
    before = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    run = LoopRun.model_validate_json((directory / "loop-run.json").read_bytes())
    impl_input = ImplementationInput.model_validate_json(
        (directory / "implementation-input.json").read_bytes(),
    )
    assert validate_implementation_context(tmp_path, run, impl_input) is None
    with pytest.raises(ValueError):
        validate_review_input_for_close(
            tmp_path, loop_type="implementation", loop_id=run.loop_id,
            expected_digest=reviewed.input_digest,
        )
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("damage", ["actionable", "failed", "source-drift"])
def test_old_closed_review_default_read_keeps_quality_and_full_digest(tmp_path, damage):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.implementation_loop import (
        ImplementationCloseOptions,
        close_implementation_loop,
    )
    from ai_sdlc.core.loop_review_service import read_verified_implementation_close

    directory, reviewed = _old_review_case(tmp_path)
    assert close_implementation_loop(
        ImplementationCloseOptions(
            root=tmp_path, loop_id=reviewed.loop_id, yes=True,
            expected_review_digest=reviewed.input_digest,
        ), review_input_validator=validate_review_input_for_close,
    ).closed
    assert read_verified_implementation_close(tmp_path, reviewed.loop_id).loop_id == reviewed.loop_id
    outcome = directory / "review-outcome-round-1.json"
    if damage == "actionable":
        _make_actionable(outcome)
    elif damage == "failed":
        data = json.loads(outcome.read_bytes())
        data.update(status="failed", failure_kind="transport", failure_reason="Incomplete")
        outcome.write_text(json.dumps(data))
    else:
        spec = tmp_path / "specs/demo-implementation-loop/spec.md"
        spec.write_bytes(spec.read_bytes() + b"\nChanged substantive input.\n")
    before = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, reviewed.loop_id)
    assert all(p.read_bytes() == content for p, content in before.items())

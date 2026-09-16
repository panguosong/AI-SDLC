"""窄身份必须与原整体评审摘要来自同一文件捕获。"""

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
    baseline = review_kernel.build_review_input(tmp_path, **arguments)
    read_paths = review_kernel._read_paths

    def mutate_after_capture(*args, **kwargs):
        result = read_paths(*args, **kwargs)
        if result:
            path.write_text(original.model_copy(update={
                "design_contract_loop_id": "redirected",
            }).model_dump_json())
        return result

    monkeypatch.setattr(review_kernel, "_read_paths", mutate_after_capture)
    reviewed = review_kernel.build_review_input(tmp_path, **arguments)
    assert reviewed == baseline
    assert reviewed.implementation_input_digest == implementation_input_digest(original)
    assert ImplementationInput.model_validate_json(path.read_bytes()).design_contract_loop_id == "redirected"

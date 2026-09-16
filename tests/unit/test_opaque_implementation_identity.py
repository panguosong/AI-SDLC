"""旧关闭凭据的完整格式、重叠捕获和最后消费边界。"""

import json

import pytest

from ai_sdlc.core import loop_decision_service as decision
from ai_sdlc.core.implementation_models import (
    ImplementationProgress,
    ImplementationTasks,
    ImplementationVerificationEvidence,
)
from ai_sdlc.core.implementation_store import implementation_artifacts
from ai_sdlc.core.loop_review_service import read_verified_implementation_close
from tests.integration.test_frontend_delivery_normal_path import (
    _write_closed_implementation,
)
from tests.unit.test_frontend_evidence_loop import _write_closed_implementation_loop

SIDECARS = ("tasks_path", "progress_path", "evidence_path")
MODELS = {
    "tasks_path": ImplementationTasks,
    "progress_path": ImplementationProgress,
    "evidence_path": ImplementationVerificationEvidence,
}


def _old_receipt(root, shape):
    work_item = root / "specs/001-ui"
    work_item.mkdir(parents=True)
    for name in ("spec.md", "plan.md", "tasks.md"):
        (work_item / name).write_text("# Historical frontend\n", encoding="utf-8")
    if shape == "placeholder":
        _write_closed_implementation(root, work_item.name)
        loop_id = "impl-frontend-normal"
    else:
        _write_closed_implementation_loop(root, work_item)
        loop_id = "impl-frontend"
    return implementation_artifacts(root, loop_id)


def _loop_bytes(root):
    return {
        path.relative_to(root): path.read_bytes()
        for path in (root / ".ai-sdlc/loops").rglob("*")
        if path.is_file()
    }


def _typed_sidecar(artifacts, name):
    return MODELS[name](
        loop_id=artifacts.loop_dir.name, work_item_id="001-ui"
    ).model_dump_json().encode()


@pytest.mark.parametrize("shape", ["receipt-only", "placeholder"])
def test_complete_historical_opaque_receipts_remain_read_only(tmp_path, shape):
    artifacts = _old_receipt(tmp_path, shape)
    before = _loop_bytes(tmp_path)
    assert (
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name).loop_id
        == artifacts.loop_dir.name
    )
    assert _loop_bytes(tmp_path) == before


@pytest.mark.parametrize("sidecar", SIDECARS)
@pytest.mark.parametrize("damage", ["missing", "typed", "nonobject", "malformed"])
def test_input_present_opaque_requires_all_three_empty_objects(
    tmp_path, sidecar, damage
):
    artifacts = _old_receipt(tmp_path, "placeholder")
    path = getattr(artifacts, sidecar)
    if damage == "missing":
        path.unlink()
    elif damage == "typed":
        path.write_bytes(_typed_sidecar(artifacts, sidecar))
    else:
        path.write_bytes(b"[]" if damage == "nonobject" else b"{broken")
    before = _loop_bytes(tmp_path)
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name)
    assert _loop_bytes(tmp_path) == before


def test_input_present_opaque_cannot_lose_all_execution_sidecars(tmp_path):
    artifacts = _old_receipt(tmp_path, "placeholder")
    for name in SIDECARS:
        getattr(artifacts, name).unlink()
    before = _loop_bytes(tmp_path)
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name)
    assert _loop_bytes(tmp_path) == before


@pytest.mark.parametrize("shape", ["receipt-only", "placeholder"])
def test_opaque_report_must_be_passed(tmp_path, shape):
    artifacts = _old_receipt(tmp_path, shape)
    report = json.loads(artifacts.report_json_path.read_bytes())
    report["status"] = "needs_review"
    artifacts.report_json_path.write_text(json.dumps(report), encoding="utf-8")
    before = _loop_bytes(tmp_path)
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name)
    assert _loop_bytes(tmp_path) == before


def test_reader_and_opaque_helper_reject_report_capture_aba(tmp_path, monkeypatch):
    artifacts = _old_receipt(tmp_path, "placeholder")
    valid_report = artifacts.report_json_path.read_bytes()
    report = json.loads(valid_report)
    report["status"] = "needs_review"
    invalid_report = json.dumps(report).encode()
    artifacts.report_json_path.write_bytes(invalid_report)
    before = _loop_bytes(tmp_path)
    validate = decision.validate_legacy_implementation_identity
    calls = []

    def transient_valid_report(*args, **kwargs):
        # 外层已捕获 A；分类期间临时放入 B，返回前恢复 A，不能拼接两次身份。
        calls.append(True)
        artifacts.report_json_path.write_bytes(valid_report)
        try:
            return validate(*args, **kwargs)
        finally:
            artifacts.report_json_path.write_bytes(invalid_report)

    monkeypatch.setattr(
        decision, "validate_legacy_implementation_identity", transient_valid_report
    )
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name)
    assert calls == [True]
    assert _loop_bytes(tmp_path) == before


@pytest.mark.parametrize("shape", ["receipt-only", "placeholder"])
@pytest.mark.parametrize("sidecar", SIDECARS)
def test_reader_rechecks_opaque_sidecars_after_identity_validation(
    tmp_path, monkeypatch, shape, sidecar
):
    artifacts = _old_receipt(tmp_path, shape)
    validate = decision.validate_legacy_implementation_identity
    changed = []

    def change_after_validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        # 分类函数自己的末读已结束；调用者最终消费仍须绑定原字节或原缺席。
        getattr(artifacts, sidecar).write_bytes(_typed_sidecar(artifacts, sidecar))
        changed.append(_loop_bytes(tmp_path))
        return result

    monkeypatch.setattr(
        decision, "validate_legacy_implementation_identity", change_after_validation
    )
    with pytest.raises(ValueError):
        read_verified_implementation_close(tmp_path, artifacts.loop_dir.name)
    assert len(changed) == 1
    assert _loop_bytes(tmp_path) == changed[0]

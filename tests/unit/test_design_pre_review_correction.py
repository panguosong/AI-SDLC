"""正式量化前的文档修正沿用原实例，并保留原检查失败和原合同。"""

import base64
import hashlib
import json

import pytest

from ai_sdlc.core.design_contract_loop import check_design_contract_loop
from ai_sdlc.core.design_contract_models import DesignContractInput
from ai_sdlc.core.design_contract_store import design_contract_artifacts
from tests.unit.test_counterexample_contract_binding import _contract


def _needs_schema_fix(root):
    work, _, options = _contract(root)
    spec = work / "spec.md"
    plan = work / "plan.md"
    original_plan = plan.read_bytes()
    plan.write_text("# 实施计划\n")
    spec.write_text(spec.read_text() + "\n说明：原始表述。\n")
    _refresh_source_hashes(work)
    first = check_design_contract_loop(options)
    assert first.status == "needs_fix", first
    artifacts = design_contract_artifacts(root, "design")
    old = {p.name: p.read_bytes() for p in artifacts.loop_dir.glob("*.json")}
    plan.write_bytes(original_plan)
    spec.write_text(spec.read_text().replace("原始表述", "校正表述"))
    _refresh_source_hashes(work)
    return work, options, artifacts, old


def _refresh_source_hashes(work):
    data = json.loads((work / "verification.json").read_bytes())
    digest = hashlib.sha256((work / "spec.md").read_bytes()).hexdigest()
    data["sources"][0]["sha256"] = digest
    data["budget_ref"]["sha256"] = digest
    (work / "verification.json").write_text(json.dumps(data, ensure_ascii=False))


def test_pre_review_schema_correction_preserves_original_history(tmp_path):
    _, options, artifacts, old = _needs_schema_fix(tmp_path.resolve())
    before = json.loads(old["loop-run.json"])

    result = check_design_contract_loop(options)

    assert result.status == "ready", result
    after = json.loads(artifacts.loop_run_path.read_bytes())
    assert after["loop_id"] == before["loop_id"]
    assert after["current_round"] == before["current_round"] == 1
    assert after["created_at"] == before["created_at"]
    assert len(after["rounds"]) == len(before["rounds"]) == 1
    original = DesignContractInput.model_validate_json(old["design-contract-input.json"])
    assert (tmp_path / original.verification_contract_ref).read_bytes() == old[
        original.verification_contract_ref.rsplit("/", 1)[1]
    ]
    journals = [json.loads(p.read_bytes()) for p in (artifacts.loop_dir / "design-check-publications").glob("*.json")]
    assert any(
        all(
            base64.b64decode(j["entries"][name]["old"]["base64"]) == old[name]
            for name in ("design-contract-input.json", "design-contract-report.json", "loop-run.json")
        )
        for j in journals
        if j["entries"]["design-contract-input.json"]["old"] is not None
    )
    assert not (artifacts.loop_dir / "decision-context.json").exists()
    assert not list(artifacts.loop_dir.glob("*outcome*"))


@pytest.mark.parametrize("footprint", ["decision-context.json", "review-input-round-1.json", "review-outcome-round-1.json", "design-contract-close.json"])
def test_pre_review_schema_correction_rejects_started_or_closed_footprint(tmp_path, footprint):
    _, options, artifacts, old = _needs_schema_fix(tmp_path.resolve())
    (artifacts.loop_dir / footprint).write_text("{}")

    result = check_design_contract_loop(options)

    assert result.status == "blocked", result
    assert artifacts.input_path.read_bytes() == old["design-contract-input.json"]
    assert artifacts.loop_run_path.read_bytes() == old["loop-run.json"]


@pytest.mark.parametrize("marker", ["decision_started_at_ms", "decision_begin_pending_digest"])
def test_pre_review_schema_correction_rejects_deleted_context(tmp_path, marker):
    _, options, artifacts, old = _needs_schema_fix(tmp_path.resolve())
    run = json.loads(old["loop-run.json"])
    run[marker] = 123456789 if marker == "decision_started_at_ms" else "a" * 64
    if marker == "decision_begin_pending_digest":
        run["decision_started_at_ms"] = 123456789
    artifacts.loop_run_path.write_text(json.dumps(run))
    before_run = artifacts.loop_run_path.read_bytes()

    result = check_design_contract_loop(options)

    assert result.status == "blocked", result
    assert artifacts.input_path.read_bytes() == old["design-contract-input.json"]
    assert artifacts.loop_run_path.read_bytes() == before_run


def test_pre_review_schema_correction_rejects_changed_target(tmp_path):
    from ai_sdlc.core.loop_stage_input import validate_stage_material_update

    _, _, artifacts, old = _needs_schema_fix(tmp_path.resolve())
    original = DesignContractInput.model_validate_json(old["design-contract-input.json"])
    changed = original.model_copy(update={"spec_digest": "new", "work_item_path": "specs/other"})
    with pytest.raises(ValueError, match="identity-change"):
        validate_stage_material_update(tmp_path.resolve(), "design-contract", artifacts.loop_dir, original, changed)


@pytest.mark.parametrize("change", ["delete", "tamper"])
def test_pre_review_schema_correction_requires_preserved_original_contract(tmp_path, change):
    _, options, artifacts, old = _needs_schema_fix(tmp_path.resolve())
    original = DesignContractInput.model_validate_json(old["design-contract-input.json"])
    contract = tmp_path / original.verification_contract_ref
    if change == "delete":
        contract.unlink()
    else:
        contract.write_bytes(b"{}")

    result = check_design_contract_loop(options)

    assert result.status == "blocked", result
    assert artifacts.input_path.read_bytes() == old["design-contract-input.json"]
    assert artifacts.loop_run_path.read_bytes() == old["loop-run.json"]

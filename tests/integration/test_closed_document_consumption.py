"""真实阶段命令核对闭后消费；合成专家只验证协议，不证明业务收益。"""

import json

import pytest

from tests.integration.test_quantified_implementation import _cli, _payload
from tests.integration.test_stage_quantified_pipeline import (
    LOOP,
    WORK_ITEM,
    actual_record,
    stage_apply,
    stage_selected,
)


def _review(root, stage):
    return _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")


def _reviewed_stage(root, stage):
    stage_selected(root, stage)
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(_review(root, stage))
    assert actual_record(root, stage, reviewed)["status"] == "passed"
    return reviewed


def _close(root, stage, reviewed):
    return _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
    )


def _native_bytes(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
def test_closed_documents_allow_downstream_code_but_reject_bound_drift(
    initialized_project_dir, stage
):
    root = initialized_project_dir
    reviewed = _reviewed_stage(root, stage)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    before_close = _native_bytes(directory)
    downstream = root / "next-stage-module.py"

    # 未 Close 的原实际评审仍绑定当前全树，不能借新分支提前换候选。
    downstream.write_text("VALUE = 2\n", encoding="utf-8")
    rejected = _review(root, stage)
    assert rejected.returncode == 1
    assert "simulation-actual-source-drift" in rejected.stdout
    assert _native_bytes(directory) == before_close
    downstream.unlink()
    closed = _close(root, stage, reviewed)
    assert closed.returncode == 0, closed.stdout + closed.stderr
    originals = _native_bytes(directory)

    # 旧版本形成的已关闭凭证不迁移；后续实际代码变化不改原文档成果。
    downstream.write_text("VALUE = 2\n", encoding="utf-8")
    consumed = _payload(_review(root, stage))
    assert consumed["review_status"] == "passed", consumed
    assert consumed["input_digest"] == reviewed["input_digest"]
    assert _native_bytes(directory) == originals

    # 原来源与阶段实际成果分别核对，不能用历史摘要掩盖任何一类变化。
    for path in (
        root / WORK_ITEM / "spec.md",
        directory / "acceptance-checklist.md"
        if stage == "requirement"
        else root / WORK_ITEM / "plan.md",
    ):
        content = path.read_bytes()
        path.write_bytes(content + b"\nchanged bound evidence\n")
        rejected = _review(root, stage)
        assert rejected.returncode == 1, rejected.stdout
        path.write_bytes(content)
        assert _payload(_review(root, stage))["review_status"] == "passed"
    assert _native_bytes(directory) == originals


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
def test_closed_documents_require_real_close_and_unchanged_actual_result(
    initialized_project_dir, stage
):
    root = initialized_project_dir
    reviewed = _reviewed_stage(root, stage)
    assert _close(root, stage, reviewed).returncode == 0
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    originals = _native_bytes(directory)
    (root / "next-stage-module.py").write_text("VALUE = 3\n", encoding="utf-8")
    close_path = directory / (
        "requirement-freeze.json"
        if stage == "requirement"
        else "design-contract-close.json"
    )
    original_close = close_path.read_bytes()
    close_path.unlink()
    assert _review(root, stage).returncode == 1
    close_path.write_bytes(original_close)

    # 缺失固定 context 必须有结构化阻断，不能让闭后回放抛出 KeyError。
    context_path = directory / "decision-context.json"
    saved_context = root.parent / "saved-decision-context.json"
    context_path.rename(saved_context)
    missing_context = _review(root, stage)
    assert missing_context.returncode == 1
    assert json.loads(missing_context.stdout)["status"] == "blocked"
    assert "Traceback" not in missing_context.stderr
    saved_context.rename(context_path)

    wrong_identity = json.loads(original_close)
    wrong_identity["loop_id"] = "another-loop"
    close_path.write_text(json.dumps(wrong_identity), encoding="utf-8")
    assert _review(root, stage).returncode == 1
    close_path.write_bytes(original_close)

    if stage == "requirement":
        wrong_count = json.loads(original_close)
        wrong_count["acceptance_count"] += 1
        close_path.write_text(json.dumps(wrong_count), encoding="utf-8")
        assert _review(root, stage).returncode == 1
        close_path.write_bytes(original_close)

    outcome_path = directory / "review-outcome-round-1.json"
    original_outcome = outcome_path.read_bytes()
    changed = json.loads(original_outcome)
    changed["simulation"]["evaluation"]["q"] = "0"
    outcome_path.write_text(json.dumps(changed), encoding="utf-8")
    assert _review(root, stage).returncode == 1
    outcome_path.write_bytes(original_outcome)
    second_path = directory / "review-outcome-round-2.json"
    invented_second = json.loads(original_outcome)
    invented_second["round_number"] = 2
    second_path.write_text(json.dumps(invented_second), encoding="utf-8")
    assert _review(root, stage).returncode == 1
    second_path.unlink()

    assert _payload(_review(root, stage))["review_status"] == "passed"
    assert _native_bytes(directory) == originals

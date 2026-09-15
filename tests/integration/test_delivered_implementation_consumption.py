"""真实原生阶段与同树交付；合成 provider/专家只验证机制，不证明模型质量。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys

import pytest

from ai_sdlc.cli.loop_cmd import get_review_aware_loop_status
from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
from ai_sdlc.cli.loop_stage_cmd import resolve_stage_decision_host
from ai_sdlc.core import implementation_loop, pr_review_service
from ai_sdlc.core.implementation_loop import _current_source_digest
from ai_sdlc.core.implementation_store import implementation_artifacts, read_progress
from ai_sdlc.core.loop_review_service import read_verified_implementation_close
from ai_sdlc.core.loop_simulation_models import StageScoreContract
from ai_sdlc.core.loop_stage_decision_service import stage_material_digest
from tests.integration import test_stage_quantified_pipeline as stages
from tests.integration.test_quantified_implementation import _cli, _git, _payload
from tests.integration.test_stage_pr_review_pipeline import (
    LOOP_ID as PR_LOOP,
)
from tests.integration.test_stage_pr_review_pipeline import (
    REVIEW_ID,
    _prepare,
    _record_actual,
)
from tests.unit.test_implementation_loop import _write_ready_work_item
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data

SOURCE = "src/ai_sdlc/core/implementation_loop.py"


def _close_stage(root, stage):
    stages.stage_apply(
        root, stage, {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            stage,
            "--loop-id",
            stages.LOOP,
            "--json",
            timeout=180,
        )
    )
    assert (
        stages.actual_record(root, stage, reviewed, timeout=180)["status"] == "passed"
    )
    _payload(
        _cli(
            root,
            "loop",
            stage,
            "freeze" if stage == "requirement" else "close",
            "--loop-id",
            stages.LOOP,
            "--expect-review-digest",
            reviewed["input_digest"],
            "--yes",
            "--json",
            timeout=180,
        )
    )


def _closed_project(root):
    _write_ready_work_item(root)
    shutil.copytree(
        root / stages.WORK_ITEM, root / "specs/implementation-after-delivery"
    )
    source = root / SOURCE
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text(
        "# Current tree\nTenant access is rejected outside its boundary.\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".ai-sdlc/loops/\n.ai-sdlc/state/\n.ai-sdlc/work-items/\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Delivery integration")
    _git(root, "config", "user.email", "delivery@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "original fixture")
    for stage in ("requirement", "design-contract", "implementation"):
        if stage == "requirement":
            args = [
                "start",
                "--idea",
                "系统必须记录实现任务证据",
                "--acceptance",
                "完成任务后可关闭",
                "--design-scope-family",
                "implementation",
                "--work-item-id",
                "demo-implementation-loop",
            ]
        elif stage == "design-contract":
            args = [
                "check",
                "--wi",
                stages.WORK_ITEM,
                "--requirement-loop-id",
                stages.LOOP,
            ]
        else:
            # 与原普通三阶段夹具一致，在开始本轮前准备其完整待验候选。
            source.write_text("VALUE = 2\n", encoding="utf-8")
            _git(
                root,
                "add",
                SOURCE,
                ".ai-sdlc/memory/ide-adapter-hint.md",
                ".ai-sdlc/project/config/project-config.yaml",
            )
            assert not _git(root, "diff", "--name-only")
            assert not _git(root, "ls-files", "--others", "--exclude-standard")
            args = [
                "start",
                "--wi",
                stages.WORK_ITEM,
                "--design-contract-loop-id",
                stages.LOOP,
            ]
        _payload(
            _cli(
                root,
                "loop",
                stage,
                *args,
                "--loop-id",
                stages.LOOP,
                "--decision-mode",
                "adaptive-quantified",
                "--decision-capability",
                stages.CAPABILITY,
                "--json",
                timeout=180,
            )
        )
        stages.stage_selected(root, stage, start=False)
        _close_stage(root, stage)


def _deliver(root):
    _payload(
        _cli(
            root,
            "pr-review",
            "start",
            "--provider",
            "mock-reviewer",
            "--review-id",
            REVIEW_ID,
            "--diff-source",
            "local-staged",
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            stages.CAPABILITY,
            "--json",
        )
    )
    data = contract_data()
    data.update(
        capability=stages.CAPABILITY,
        loop_type="local-pr-review",
        profile_id="delivery-readiness-v1",
    )
    data["time_plan"].update(scope="local-pr-review-close")
    contract = StageScoreContract.model_validate(data)
    _prepare(
        root,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": [data],
            "sources": [
                {
                    "id": "spec",
                    "path": "README.md",
                    "sha256": hashlib.sha256(
                        (root / "README.md").read_bytes()
                    ).hexdigest(),
                    "locator": "tenant access",
                    "claim": "机制夹具原义务",
                }
            ],
        },
    )
    frozen = _prepare(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [
                candidate_data(contract, "current-staged-tree", seconds=None)
            ],
        },
    )
    _prepare(
        root,
        {
            "operation": "record-comparison",
            "request_id": "record",
            "judgement": {
                "judge_input_digest": frozen["judge_input"]["judge_input_digest"],
                "assessments": [assessment_data("current-staged-tree", 4, 4)],
            },
        },
    )
    verified = _payload(
        _cli(
            root,
            "pr-review",
            "verify",
            "--cwd",
            ".",
            "--json",
            "--",
            sys.executable,
            "-B",
            "-c",
            "from pathlib import Path; assert Path('src/ai_sdlc/core/implementation_loop.py').read_text() == 'VALUE = 2\\n'",
            timeout=180,
        )
    )
    assert verified["status"] == "ready"
    _prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "local-pr-review",
            "--loop-id",
            PR_LOOP,
            "--json",
            timeout=180,
        )
    )
    assert _record_actual(root, reviewed, "PASS")["status"] == "passed"
    _payload(
        _cli(
            root,
            "pr-review",
            "commit",
            "--message",
            "deliver verified fixture",
            "--json",
            timeout=180,
        )
    )
    closed = _payload(
        _cli(
            root,
            "pr-review",
            "close",
            "--review-id",
            REVIEW_ID,
            "--loop-id",
            PR_LOOP,
            "--expect-review-digest",
            reviewed["input_digest"],
            "--json",
            timeout=180,
        )
    )
    assert closed["status"] == "closed" and closed["verdict"] == "fully_clean"


def _assert_consumable(root, loop_id, *, full_proof_reads=None):
    proof_reads_before = full_proof_reads[0] if full_proof_reads is not None else 0
    assert (
        read_verified_implementation_close(
            root, loop_id, review_input_validator=validate_review_input_for_close
        ).loop_id
        == loop_id
    )
    if full_proof_reads is not None:
        # 原 reader 保留两次 validator；每次只在作用域首尾完整复核，中间仍核全部字节。
        assert 0 < full_proof_reads[0] - proof_reads_before <= 4
    status = get_review_aware_loop_status(root, "implementation")
    assert status.status == "ready" and status.current_loop.status == "closed"
    progress = read_progress(implementation_artifacts(root, loop_id).progress_path)
    current = _current_source_digest(root, progress)
    assert any(
        result.successful
        and result.source_digest_before == result.source_digest_after == current
        for task in progress.tasks
        for result in task.quality_results
    )


def test_native_delivery_preserves_current_material_and_rejects_drift(
    initialized_project_dir, monkeypatch
):
    root = initialized_project_dir
    _closed_project(root)
    loop_id = stages.LOOP
    directory = root / ".ai-sdlc/loops/implementation" / loop_id
    original = {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    _assert_consumable(root, loop_id)
    _deliver(root)
    full_proof_reads = [0]
    actual_full_read = pr_review_service._read_verified_delivery_commit_uncached

    def count_full_read(*args, **kwargs):
        full_proof_reads[0] += 1
        return actual_full_read(*args, **kwargs)

    with monkeypatch.context() as counts:
        counts.setattr(
            pr_review_service,
            "_read_verified_delivery_commit_uncached",
            count_full_read,
        )
        _assert_consumable(root, loop_id, full_proof_reads=full_proof_reads)
    assert {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    } == original

    # 同一个真实交付上下文内逐一反例；恢复只属于本测试，原失败不会被当作通过。
    source = root / SOURCE
    source_original = source.read_bytes()
    source.write_bytes(b"VALUE = 3\n")
    _git(root, "add", SOURCE)
    try:
        with pytest.raises(ValueError):
            read_verified_implementation_close(
                root, loop_id, review_input_validator=validate_review_input_for_close
            )
    finally:
        source.write_bytes(source_original)
        _git(root, "add", SOURCE)
    run_path = root / ".ai-sdlc/reviews/pr" / REVIEW_ID / "review-run.json"
    run_original = run_path.read_bytes()
    run_path.unlink()
    try:
        with pytest.raises(ValueError):
            read_verified_implementation_close(
                root, loop_id, review_input_validator=validate_review_input_for_close
            )
    finally:
        run_path.write_bytes(run_original)
    progress_path = directory / "implementation-progress.json"
    progress_original = progress_path.read_bytes()
    progress_path.write_bytes(progress_original + b"\n")
    try:
        with pytest.raises(ValueError):
            read_verified_implementation_close(
                root, loop_id, review_input_validator=validate_review_input_for_close
            )
    finally:
        progress_path.write_bytes(progress_original)
    _assert_consumable(root, loop_id)
    progress = read_progress(implementation_artifacts(root, loop_id).progress_path)
    assert (
        _current_source_digest(
            root,
            progress,
            reviewed_artifacts={"README.md": b"different captured source"},
        )
        == ""
    )

    # 首次阶段材料与交付 guard 之间的字节漂移也必须在当前读取内拒绝。
    real_proof = implementation_loop._closed_implementation_delivery_proof
    proof_calls = 0

    def proof_then_mutate(*args, **kwargs):
        nonlocal proof_calls
        proof = real_proof(*args, **kwargs)
        proof_calls += 1
        if proof_calls == 1:
            progress_path.write_bytes(progress_original + b"\n")
        return proof

    host = resolve_stage_decision_host(root, "implementation", loop_id)
    try:
        with monkeypatch.context() as timing:
            timing.setattr(
                implementation_loop,
                "_closed_implementation_delivery_proof",
                proof_then_mutate,
            )
            with pytest.raises(ValueError, match="delivery-stage-material-drift"):
                stage_material_digest(root, host)
    finally:
        progress_path.write_bytes(progress_original)
    _assert_consumable(root, loop_id)

    # 同一工作项不能用第二个 Loop 重置原生命周期，历史关闭状态也不例外。
    old_loop_id = stages.LOOP
    conflict = _cli(
        root,
        "loop",
        "implementation",
        "start",
        "--wi",
        stages.WORK_ITEM,
        "--design-contract-loop-id",
        old_loop_id,
        "--loop-id",
        "implementation-after-delivery",
        "--decision-mode",
        "adaptive-quantified",
        "--decision-capability",
        stages.CAPABILITY,
        "--json",
        timeout=180,
    )
    assert conflict.returncode == 1
    assert "decision-lifecycle-conflict" in json.loads(conflict.stdout)["blocker"]

    # 独立后继的规范在交付树中已存在；真实三阶段以当前 HEAD 为基线。
    delivered_head = _git(root, "rev-parse", "HEAD")
    delivered_runtime = _git(root, "ls-files", "--others", "--exclude-standard")
    assert all(
        path.startswith(".ai-sdlc/reviews/") for path in delivered_runtime.splitlines()
    )
    with monkeypatch.context() as fixture_names:
        fixture_names.setattr(stages, "LOOP", "implementation-after-delivery")
        fixture_names.setattr(
            stages, "WORK_ITEM", "specs/implementation-after-delivery"
        )
        for stage in ("requirement", "design-contract", "implementation"):
            if stage == "requirement":
                args = [
                    "start",
                    "--idea",
                    "系统必须记录独立后继任务证据",
                    "--acceptance",
                    "完成独立工作项任务后可关闭",
                    "--design-scope-family",
                    "implementation",
                    "--work-item-id",
                    "implementation-after-delivery",
                ]
            elif stage == "design-contract":
                args = [
                    "check",
                    "--wi",
                    stages.WORK_ITEM,
                    "--requirement-loop-id",
                    stages.LOOP,
                ]
            else:
                args = [
                    "start",
                    "--wi",
                    stages.WORK_ITEM,
                    "--design-contract-loop-id",
                    stages.LOOP,
                ]
            _payload(
                _cli(
                    root,
                    "loop",
                    stage,
                    *args,
                    "--loop-id",
                    stages.LOOP,
                    "--decision-mode",
                    "adaptive-quantified",
                    "--decision-capability",
                    stages.CAPABILITY,
                    "--json",
                    timeout=180,
                )
            )
            stages.stage_selected(root, stage, start=False)
            _close_stage(root, stage)
        _assert_consumable(root, stages.LOOP)
        assert _git(root, "rev-parse", "HEAD") == delivered_head
        assert not _git(root, "diff", "--name-only")
        assert not _git(root, "diff", "--cached", "--name-only")
        assert (
            _git(root, "ls-files", "--others", "--exclude-standard")
            == delivered_runtime
        )
        proof = pr_review_service.read_verified_delivery_commit(root)
        assert proof.current_commit == delivered_head
        successor = json.loads(
            (
                root
                / ".ai-sdlc/loops/implementation"
                / stages.LOOP
                / "review-outcome-round-1.json"
            ).read_text()
        )
        assert (
            successor["simulation"]["source_digest"]
            != json.loads(original["review-outcome-round-1.json"])["simulation"][
                "source_digest"
            ]
        )

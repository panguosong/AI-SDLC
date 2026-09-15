"""真实本地提交的只读交付证明；合成专家仅作机制夹具。"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
from ai_sdlc.core import pr_review_service as service
from tests.integration.test_cli_pr_review import (
    _git,
    _git_commit_tree,
    _init_repo,
    _start_verified_local_review,
)


@pytest.fixture
def delivered(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    (root / "README.md").write_text("# Test\nReviewed change.\n", encoding="utf-8")
    _git(root, "add", "README.md")
    payload, reviewed = _start_verified_local_review(root, "proof-test")
    committed = service.commit_pr_review(root, message="deliver unchanged review")
    assert committed.status == "ready", committed
    return root, payload, reviewed


def _files(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _close(root, payload, reviewed):
    result = service.close_pr_review(
        root,
        expected_review_id=payload["review_id"],
        expected_loop_id=payload["loop_id"],
        expected_review_digest=reviewed.input_digest,
        review_input_validator=validate_review_input_for_close,
    )
    assert result.verdict == "fully_clean", result


def test_native_commit_proof_before_and_after_close_is_read_only(
    delivered, monkeypatch
):
    root, payload, reviewed = delivered
    run, _ = service._load_current_review_run(root)
    for closed in (False, True):
        if closed:
            _close(root, payload, reviewed)
        before = _files(root), (root / ".git/index").read_bytes()
        commands = []
        original = service.subprocess.run

        def checked_run(argv, *args, _commands=commands, _original=original, **kwargs):
            _commands.append(argv)
            assert argv[0] == "git"
            assert not {"write-tree", "update-index", "commit", "add"} & set(argv)
            return _original(argv, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(service.subprocess, "run", checked_run)
            proof = service.read_verified_delivery_commit(root)
            assert service.read_verified_delivery_commit(root) == proof
        assert commands
        assert proof.reviewed_head == run.head_commit
        assert proof.current_commit == run.delivery_commit
        assert proof.staged_tree == run.staged_tree_oid
        assert proof.review_input_digest == reviewed.input_digest
        assert all(
            digest is None or len(digest) == 64 for _, digest in proof.artifact_digests
        )
        assert before == (_files(root), (root / ".git/index").read_bytes())
        with pytest.raises(FrozenInstanceError):
            proof.current_commit = "a" * 40


def test_quantified_native_commit_proof_preserves_actual_review(tmp_path, monkeypatch):
    from ai_sdlc.core.review_kernel import ReviewInput
    from tests.integration.test_stage_pr_review_pipeline import (
        LOOP_ID,
        REVIEW_ID,
        _ready,
        _record_actual,
    )

    root = tmp_path / "quantified"
    root.mkdir()
    reviewed = _ready(root)
    assert _record_actual(root, reviewed, "PASS")["status"] == "passed"
    result = service.commit_pr_review(root, message="quantified exact tree")
    assert result.status == "ready", result
    calls = _count_full_reads(monkeypatch)
    with service.verified_delivery_read_scope(root):
        proof = service.read_verified_delivery_commit(root)
        captured = service._read_delivery_guard_artifacts(root, proof)
        assert service._delivery_action_is_time_independent(proof, captured)
        assert service.read_verified_delivery_commit(root) == proof
        assert len(calls) == 1
    assert len(calls) == 2
    outcome_key = f".ai-sdlc/reviews/pr/{REVIEW_ID}/review-outcome-round-1.json"
    changed = json.loads(captured[outcome_key])
    changed["simulation"]["decision"]["action"] = "improve"
    # 只检验资格判定的保守分支，不把该内存改件送入真实 proof 或写回 R1。
    assert not service._delivery_action_is_time_independent(
        proof, {**captured, outcome_key: json.dumps(changed).encode()}
    )
    assert proof.review_input_digest == reviewed["input_digest"]
    before = _files(root)
    _close(
        root,
        {"review_id": REVIEW_ID, "loop_id": LOOP_ID},
        ReviewInput(
            loop_id=LOOP_ID,
            loop_type="local-pr-review",
            round_number=1,
            input_digest=reviewed["input_digest"],
            artifact_paths=reviewed["artifact_paths"],
            expert_roles=reviewed["expert_roles"],
            expert_reasons=reviewed["expert_reasons"],
        ),
    )
    after = service.read_verified_delivery_commit(root)
    assert (proof.reviewed_head, proof.current_commit, proof.staged_tree) == (
        after.reviewed_head,
        after.current_commit,
        after.staged_tree,
    )
    assert (
        before[".ai-sdlc/reviews/pr/stage-pr-native/review-outcome-round-1.json"]
        == (
            root / ".ai-sdlc/reviews/pr/stage-pr-native/review-outcome-round-1.json"
        ).read_bytes()
    )


_DELIVERY_MUTATIONS = (
    "recorded-commit",
    "parent",
    "tree",
    "pointer",
    "pack",
    "findings",
    "verify-missing",
    "outcome-missing",
    "outcome-failed",
    "unstaged",
    "restaged",
    "untracked",
    "mode",
    "hidden-index",
    "late-commit",
    "merge",
)


def _mutate_delivery(root, mutation):
    run, run_path = service._load_current_review_run(root)
    directory = run_path.parent
    if mutation in {"recorded-commit", "parent"}:
        data = json.loads(run_path.read_bytes())
        data[
            "delivery_commit"
            if mutation == "recorded-commit"
            else "delivery_parent_commit"
        ] = ""
        run_path.write_text(json.dumps(data), encoding="utf-8")
    elif mutation == "tree":
        (root / "README.md").write_text("different tree\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "--amend", "--no-edit")
    elif mutation == "pointer":
        path = root / service.CURRENT_REVIEW_PATH
        data = json.loads(path.read_bytes())
        data["review_id"] = "another-review"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif mutation in {"pack", "findings"}:
        path = directory / (
            "review-pack.json" if mutation == "pack" else "findings.json"
        )
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation in {"verify-missing", "outcome-missing"}:
        (
            directory
            / (
                "verification-evidence.json"
                if mutation == "verify-missing"
                else "review-outcome-round-1.json"
            )
        ).unlink()
    elif mutation == "outcome-failed":
        path = directory / "review-outcome-round-1.json"
        data = json.loads(path.read_bytes())
        data["status"] = "failed"
        path.write_text(json.dumps(data), encoding="utf-8")
    elif mutation in {"unstaged", "restaged"}:
        (root / "README.md").write_text("unreviewed bytes\n", encoding="utf-8")
        if mutation == "restaged":
            _git(root, "add", "README.md")
    elif mutation == "untracked":
        (root / "extra.py").write_text("pass\n", encoding="utf-8")
    elif mutation == "mode":
        if os.name == "nt":
            # Windows 不支持文件执行位，使用真实 Git 模式变化验证交付边界。
            before = _git(root, "ls-files", "--stage", "--", "README.md").split()
            _git(root, "update-index", "--chmod=+x", "--", "README.md")
            after = _git(root, "ls-files", "--stage", "--", "README.md").split()
            assert before[0] == "100644" and after[0] == "100755"
            assert before[1:] == after[1:]
        else:
            (root / "README.md").chmod(0o755)
    elif mutation == "hidden-index":
        _git(root, "update-index", "--assume-unchanged", "README.md")
    elif mutation == "late-commit":
        _git(root, "commit", "--allow-empty", "-m", "new HEAD")
    elif mutation == "merge":
        commit = _git_commit_tree(
            root,
            run.staged_tree_oid,
            parents=(run.delivery_commit, run.head_commit),
            message="merge",
        )
        _git(root, "update-ref", "HEAD", commit)


@pytest.mark.parametrize("mutation", _DELIVERY_MUTATIONS)
def test_delivery_proof_rejects_current_identity_and_evidence_drift(
    delivered, mutation
):
    root, _, _ = delivered
    _mutate_delivery(root, mutation)
    with pytest.raises((OSError, ValueError)):
        service.read_verified_delivery_commit(root)


def test_closed_final_report_tamper_is_rejected(delivered):
    root, payload, reviewed = delivered
    _close(root, payload, reviewed)
    run, run_path = service._load_current_review_run(root)
    path = run_path.parent / "final-report.md"
    path.write_bytes(path.read_bytes() + b"changed\n")
    with pytest.raises(ValueError, match="closed-report-invalid"):
        service.read_verified_delivery_commit(root)


def test_proof_double_read_rejects_new_optional_artifact(delivered, monkeypatch):
    root, _, _ = delivered
    original = service._read_delivery_commit
    calls = 0

    def changed_between_reads(project):
        nonlocal calls
        value = original(project)
        calls += 1
        if calls == 1:
            run, run_path = service._load_current_review_run(project)
            (run_path.parent / "final-report.md").write_text(
                "late file\n", encoding="utf-8"
            )
        return value

    monkeypatch.setattr(service, "_read_delivery_commit", changed_between_reads)
    with pytest.raises(ValueError, match="proof-drift"):
        service.read_verified_delivery_commit(root)


def _count_full_reads(monkeypatch):
    calls = []
    original = service._read_verified_delivery_commit_uncached

    def counted(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(service, "_read_verified_delivery_commit_uncached", counted)
    return calls


@pytest.mark.parametrize("closed", (False, True))
def test_scope_reuses_complete_proof_with_fresh_guards_and_final_full_read(
    delivered, monkeypatch, closed
):
    root, payload, reviewed = delivered
    if closed:
        _close(root, payload, reviewed)
    before = _files(root), (root / ".git/index").read_bytes()
    calls = _count_full_reads(monkeypatch)
    with service.verified_delivery_read_scope(root):
        assert calls == []
        proof = service.read_verified_delivery_commit(root)
        assert len(calls) == 1
        with service.verified_delivery_read_scope(root):
            assert service.read_verified_delivery_commit(root) == proof
            assert service.read_verified_delivery_commit(root) == proof
        assert len(calls) == 1
    assert len(calls) == 2
    assert service.read_verified_delivery_commit(root) == proof
    assert len(calls) == 3
    assert before == (_files(root), (root / ".git/index").read_bytes())


@pytest.mark.parametrize("closed", (False, True))
def test_fast_source_payload_matches_full_proof_without_rederiving_tree(
    delivered, monkeypatch, closed
):
    root, payload, reviewed = delivered
    if closed:
        _close(root, payload, reviewed)
    with service.verified_delivery_read_scope(root):
        proof = service.read_verified_delivery_commit(root)
        commands = []
        original = service.subprocess.run

        def counted(argv, *args, **kwargs):
            commands.append(argv)
            return original(argv, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(service.subprocess, "run", counted)
            full = service._delivery_source_boundary(root, proof.staged_tree)
            full_commands = list(commands)
            commands.clear()
            fast = service._delivery_source_boundary(
                root,
                proof.staged_tree,
                verified_source_digest=proof.source_boundary_digest,
            )
        assert fast == full
        assert service._delivery_source_digest(fast) == proof.source_boundary_digest
        assert len(full_commands) == 10
        assert len(commands) == 7
        assert any("ls-tree" in argv for argv in full_commands)
        assert not any("ls-tree" in argv for argv in commands)
    with pytest.raises(ValueError, match="source-guard-invalid"):
        service._delivery_source_boundary(
            root,
            proof.staged_tree,
            verified_source_digest=proof.source_boundary_digest,
        )


def test_fast_source_digest_must_belong_to_completed_current_scope(delivered):
    root, _, _ = delivered
    with service.verified_delivery_read_scope(root):
        with pytest.raises(ValueError, match="source-guard-invalid"):
            service._delivery_source_boundary(
                root, "HEAD", verified_source_digest="0" * 64
            )
        proof = service.read_verified_delivery_commit(root)
        for digest in ("", "wrong", "0" * 64):
            with pytest.raises(ValueError, match="source-guard-invalid"):
                service._delivery_source_boundary(
                    root, proof.staged_tree, verified_source_digest=digest
                )
        with pytest.raises(ValueError, match="source-guard-invalid"):
            service._delivery_source_boundary(
                root,
                "HEAD^{tree}",
                verified_source_digest=proof.source_boundary_digest,
            )
        path = root / "README.md"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"unreviewed bytes\n")
            changed = service._delivery_source_boundary(root, proof.staged_tree)
            digest = service._delivery_source_digest(changed)
            assert digest != proof.source_boundary_digest
            with pytest.raises(ValueError, match="source-guard-invalid"):
                service._delivery_source_boundary(
                    root, proof.staged_tree, verified_source_digest=digest
                )
            with pytest.raises(ValueError, match="source-drift"):
                service._delivery_source_boundary(
                    root,
                    proof.staged_tree,
                    verified_source_digest=proof.source_boundary_digest,
                )
        finally:
            path.write_bytes(original)


@pytest.mark.parametrize("mutation", _DELIVERY_MUTATIONS)
def test_scope_guard_rejects_each_current_identity_or_evidence_drift(
    delivered, mutation
):
    root, _, _ = delivered
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        _mutate_delivery(root, mutation)
        # 当次命中必须报错，不能只寄希望于作用域退出时发现漂移。
        with pytest.raises((OSError, ValueError)):
            service.read_verified_delivery_commit(root)


@pytest.mark.parametrize(
    "filename",
    (
        "review-outcome-round-2.json",
        "decision-context.json",
        "final-report.md",
        "resolution.yaml",
        "verification-evidence.json",
        "review.diff",
    ),
)
def test_scope_guard_binds_optional_presence_and_all_review_input_bytes(
    delivered, filename
):
    root, _, _ = delivered
    run, run_path = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    path = (
        root / pack.diff_path
        if filename == "review.diff"
        else run_path.parent / filename
    )
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        path.write_bytes((path.read_bytes() if path.exists() else b"") + b"\n")
        with pytest.raises(ValueError, match="artifact-drift"):
            service.read_verified_delivery_commit(root)


@pytest.mark.parametrize("target", ("artifact", "source"))
def test_scope_guard_double_read_rejects_mid_read_mutation(
    delivered, monkeypatch, target
):
    root, _, _ = delivered
    _, run_path = service._load_current_review_run(root)
    path = (
        run_path.parent / "findings.json"
        if target == "artifact"
        else root / "README.md"
    )
    original = service._delivery_source_boundary
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        changed = False

        def mutate_after_first_boundary(project, tree, **kwargs):
            nonlocal changed
            value = original(project, tree, **kwargs)
            if not changed:
                path.write_bytes(path.read_bytes() + b"changed\n")
                changed = True
            return value

        monkeypatch.setattr(
            service, "_delivery_source_boundary", mutate_after_first_boundary
        )
        with pytest.raises(ValueError):
            service.read_verified_delivery_commit(root)
        assert changed


def test_scope_final_check_and_exception_expire_copied_context(delivered):
    root, _, _ = delivered
    _, run_path = service._load_current_review_run(root)
    path = run_path.parent / "final-report.md"
    with (
        pytest.raises(ValueError, match="proof-drift"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        copied = copy_context()
        path.write_text("late optional artifact\n", encoding="utf-8")
    assert copied.run(service._active_delivery_read_scope, root) is None
    path.unlink()
    assert copied.run(service.read_verified_delivery_commit, root).root == root
    with (
        pytest.raises(RuntimeError, match="consumer failed"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        copied = copy_context()
        raise RuntimeError("consumer failed")
    assert copied.run(service._active_delivery_read_scope, root) is None


def test_scope_initial_failure_never_installs_a_proof_or_reuses_after_restore(
    delivered, monkeypatch
):
    root, _, _ = delivered
    _, run_path = service._load_current_review_run(root)
    path = run_path.parent / "review-outcome-round-1.json"
    original = path.read_bytes()
    calls = _count_full_reads(monkeypatch)
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        path.unlink()
        with pytest.raises(ValueError):
            service.read_verified_delivery_commit(root)
        assert service._active_delivery_read_scope(root).proof is None
        path.write_bytes(original)
        with pytest.raises(ValueError, match="scope-invalid"):
            service.read_verified_delivery_commit(root)
    assert len(calls) == 1
    assert service.read_verified_delivery_commit(root).root == root
    assert len(calls) == 2


def test_scope_does_not_cross_threads_roots_or_completed_contexts(
    delivered, monkeypatch, tmp_path
):
    root, _, _ = delivered
    other = tmp_path / "other"
    other.mkdir()
    calls = _count_full_reads(monkeypatch)
    with service.verified_delivery_read_scope(root):
        proof = service.read_verified_delivery_commit(root)
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert (
                executor.submit(
                    copied.run, service.read_verified_delivery_commit, root
                ).result()
                == proof
            )
        assert len(calls) == 2
        with service.verified_delivery_read_scope(other):
            assert service._active_delivery_read_scope(root) is None
            assert service._active_delivery_read_scope(other).proof is None
        assert service._active_delivery_read_scope(root).proof == proof
        assert len(calls) == 2
    assert len(calls) == 3
    assert copied.run(service.read_verified_delivery_commit, root) == proof
    assert len(calls) == 4
    with service.verified_delivery_read_scope(root):
        pass
    assert len(calls) == 4


def test_time_dependent_proof_keeps_full_reads_in_scope(delivered, monkeypatch):
    root, _, _ = delivered
    # 只关闭优化资格；原真实 PR/提交/门禁仍由每次完整入口执行。
    monkeypatch.setattr(
        service, "_delivery_action_is_time_independent", lambda *args: False
    )
    calls = _count_full_reads(monkeypatch)
    with service.verified_delivery_read_scope(root):
        proof = service.read_verified_delivery_commit(root)
        assert service.read_verified_delivery_commit(root) == proof
        assert len(calls) == 2
    assert len(calls) == 3


def test_initial_full_read_cannot_reenter_an_unfinished_scope(delivered, monkeypatch):
    root, _, _ = delivered

    def reenter_before_clean_preparation(project, *args, **kwargs):
        assert service._active_delivery_read_scope(project).proof is None
        return service.read_verified_delivery_commit(project)

    monkeypatch.setattr(
        service, "_current_clean_expert_review", reenter_before_clean_preparation
    )
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        copied = copy_context()
        service.read_verified_delivery_commit(root)
    assert copied.run(service._active_delivery_read_scope, root) is None


@pytest.mark.parametrize("kind", ("deleted", "symlink", "directory", "replace"))
def test_scope_guard_preserves_regular_file_and_physical_git_checks(delivered, kind):
    root, _, _ = delivered
    run, run_path = service._load_current_review_run(root)
    path = run_path.parent / "findings.json"
    with (
        pytest.raises(ValueError, match="scope-invalid"),
        service.verified_delivery_read_scope(root),
    ):
        service.read_verified_delivery_commit(root)
        if kind == "replace":
            replacement = _git_commit_tree(
                root,
                run.staged_tree_oid,
                parents=(run.delivery_commit,),
                message="replacement changes visible ancestry",
            )
            _git(root, "replace", run.delivery_commit, replacement)
        else:
            original = path.read_bytes()
            path.unlink()
            if kind == "symlink":
                target = run_path.parent / "symlink-target.json"
                target.write_bytes(original)
                path.symlink_to(target)
            elif kind == "directory":
                path.mkdir()
        with pytest.raises((OSError, ValueError)):
            service.read_verified_delivery_commit(root)

"""统一 source snapshot 的确定性与 freshness 测试。"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import ai_sdlc.core.source_change_capture as source_change_capture
import ai_sdlc.core.source_content_identity as source_content_identity
import ai_sdlc.core.source_snapshot as source_snapshot_module
import ai_sdlc.core.source_snapshot_view as source_snapshot_view
from ai_sdlc.core.source_content_identity import (
    compare_source_content,
    source_content_equivalent,
)
from ai_sdlc.core.source_snapshot import (
    SourceSnapshotOptions,
    _binary_files,
    _parse_binary_numstat,
    _SnapshotParts,
    build_source_snapshot,
    revalidate_source_snapshot,
)
from ai_sdlc.core.source_snapshot_view import (
    file_versions,
    materialized_source_view,
    python_sources,
)


def test_unstaged_snapshot_includes_untracked_unicode_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "src" / "精简 评估.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('first')\n", encoding="utf-8")

    first = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    assert first.changed_files == ["src/精简 评估.py"]
    assert first.untracked_files == ["src/精简 评估.py"]
    assert first.diff_hash.startswith("sha256:")
    assert revalidate_source_snapshot(tmp_path, first).fresh is True

    target.write_text("print('second')\n", encoding="utf-8")
    freshness = revalidate_source_snapshot(tmp_path, first)
    assert freshness.fresh is False
    assert freshness.reason == "diff_hash_changed"


def test_worktree_snapshot_freezes_staged_unstaged_and_untracked_union(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "README.md", "# Base\n")
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "README.md", "src/app.py")
    _git(tmp_path, "commit", "-m", "add base")
    _write(tmp_path, "README.md", "# Staged\n")
    _git(tmp_path, "add", "README.md")
    _write(tmp_path, "src/app.py", "VALUE = 'worktree'\n")
    _write(tmp_path, "src/new.py", "NEW = True\n")
    expected_worktree_bytes = (tmp_path / "src/app.py").read_bytes()
    expected_untracked_bytes = (tmp_path / "src/new.py").read_bytes()

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )

    assert snapshot.changed_files == ["README.md", "src/app.py", "src/new.py"]
    assert snapshot.untracked_files == ["src/new.py"]
    assert file_versions(tmp_path, snapshot, "src/app.py") == (
        b"VALUE = 'base'\n",
        expected_worktree_bytes,
    )
    with materialized_source_view(tmp_path, snapshot) as source:
        assert (source / "README.md").read_text(encoding="utf-8") == "# Staged\n"
        assert (source / "src/app.py").read_bytes() == expected_worktree_bytes
        assert (source / "src/new.py").read_bytes() == expected_untracked_bytes

    _write(tmp_path, "src/app.py", "VALUE = 'mutated'\n")
    freshness = revalidate_source_snapshot(tmp_path, snapshot)
    assert freshness.fresh is False
    assert freshness.reason == "diff_hash_changed"
    with pytest.raises(ValueError, match="source content changed"):
        file_versions(tmp_path, snapshot, "src/app.py")
    with pytest.raises(ValueError, match="source content changed"):
        python_sources(tmp_path, snapshot)


def test_worktree_snapshot_preserves_explicit_crlf_bytes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    expected_worktree_bytes = b"VALUE = 'worktree'\r\n"
    (tmp_path / "src/app.py").write_bytes(expected_worktree_bytes)

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )

    assert file_versions(tmp_path, snapshot, "src/app.py") == (
        b"VALUE = 'base'\n",
        expected_worktree_bytes,
    )
    with materialized_source_view(tmp_path, snapshot) as source:
        assert (source / "src/app.py").read_bytes() == expected_worktree_bytes


def test_worktree_snapshot_rejects_change_between_diff_and_content_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    _write(tmp_path, "src/app.py", "VALUE = 'first'\n")
    original = source_snapshot_module.capture_path_changes
    mutated = False

    def mutate_before_capture(*args: object, **kwargs: object):
        nonlocal mutated
        if not mutated:
            mutated = True
            _write(tmp_path, "src/app.py", "VALUE = 'second'\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        source_snapshot_module,
        "capture_path_changes",
        mutate_before_capture,
    )

    with pytest.raises(ValueError, match="changed during snapshot capture"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
        )


def test_worktree_snapshot_rejects_aba_during_content_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    _write(tmp_path, "src/app.py", "VALUE = 'first'\n")
    original = source_snapshot_module.capture_path_changes
    mutated = False

    def capture_aba(*args: object, **kwargs: object):
        nonlocal mutated
        if mutated:
            return original(*args, **kwargs)
        mutated = True
        _write(tmp_path, "src/app.py", "VALUE = 'second'\n")
        captured = original(*args, **kwargs)
        _write(tmp_path, "src/app.py", "VALUE = 'first'\n")
        return captured

    monkeypatch.setattr(
        source_snapshot_module,
        "capture_path_changes",
        capture_aba,
    )

    with pytest.raises(ValueError, match="source content changed"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
        )


def test_worktree_snapshot_rejects_hidden_staged_content(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _write(tmp_path, "src/other.py", "OTHER = 'base'\n")
    _git(tmp_path, "add", "src/app.py", "src/other.py")
    _git(tmp_path, "commit", "-m", "add base")
    _write(tmp_path, "src/app.py", "VALUE = 'staged-danger'\n")
    _git(tmp_path, "add", "src/app.py")
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _write(tmp_path, "src/other.py", "OTHER = 'worktree'\n")

    with pytest.raises(ValueError, match="divergent staged/worktree paths"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
        )


def test_worktree_snapshot_remains_fresh_when_same_content_is_staged(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add base")
    _write(tmp_path, "src/app.py", "VALUE = 'worktree'\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )

    _git(tmp_path, "add", "src/app.py")

    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is True
    with materialized_source_view(tmp_path, snapshot) as source:
        assert (source / "src/app.py").read_text(encoding="utf-8") == (
            "VALUE = 'worktree'\n"
        )


def test_worktree_snapshot_rejects_index_change_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add base")
    _write(tmp_path, "src/app.py", "VALUE = 'worktree'\n")
    original_identity = source_snapshot_module._index_identity
    calls = 0

    def racing_identity(root: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            _git(root, "add", "src/app.py")
        return original_identity(root)

    monkeypatch.setattr(source_snapshot_module, "_index_identity", racing_identity)

    with pytest.raises(ValueError, match="index changed during source capture"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
        )


def test_worktree_materialization_rejects_raw_eol_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.txt text\n")
    _write(tmp_path, "message.txt", "one\n")
    _git(tmp_path, "add", ".gitattributes", "message.txt")
    _git(tmp_path, "commit", "-m", "add text")
    _write_bytes(tmp_path, "message.txt", b"two\r\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )
    _write_bytes(tmp_path, "message.txt", b"two\n")

    with (
        pytest.raises(ValueError, match="source content changed"),
        materialized_source_view(tmp_path, snapshot),
    ):
        pass


def test_worktree_snapshot_captures_executable_mode_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    if _git(tmp_path, "config", "--bool", "core.filemode") != "true":
        pytest.skip("repository does not track executable mode")
    _write(tmp_path, "script.sh", "#!/bin/sh\nexit 0\n")
    _git(tmp_path, "add", "script.sh")
    _git(tmp_path, "commit", "-m", "add script")
    (tmp_path / "script.sh").chmod(0o755)

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )
    changes = source_change_capture.capture_path_changes(tmp_path, snapshot)

    assert changes["script.sh"].before.mode == "100644"
    assert changes["script.sh"].after.mode == "100755"
    with materialized_source_view(tmp_path, snapshot) as source:
        assert (source / "script.sh").stat().st_mode & 0o111


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_worktree_snapshot_rejects_hidden_index_paths(
    tmp_path: Path,
    index_flag: str,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add base")
    _git(tmp_path, "update-index", index_flag, "src/app.py")
    _write(tmp_path, "src/app.py", "VALUE = 'hidden'\n")

    with pytest.raises(ValueError, match="hidden index paths"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
        )


def test_worktree_snapshot_ignores_tracked_runtime_divergence(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 'base'\n")
    _write(tmp_path, ".ai-sdlc/loops/runtime.json", '{"value":"base"}\n')
    _write(tmp_path, "build/generated.txt", "base\n")
    _git(
        tmp_path,
        "add",
        "-f",
        "src/app.py",
        ".ai-sdlc/loops/runtime.json",
        "build/generated.txt",
    )
    _git(tmp_path, "commit", "-m", "add tracked runtime")
    _write(tmp_path, ".ai-sdlc/loops/runtime.json", '{"value":"staged"}\n')
    _write(tmp_path, "build/generated.txt", "staged\n")
    _git(
        tmp_path,
        "add",
        "-f",
        ".ai-sdlc/loops/runtime.json",
        "build/generated.txt",
    )
    _write(tmp_path, ".ai-sdlc/loops/runtime.json", '{"value":"worktree"}\n')
    _write(tmp_path, "build/generated.txt", "worktree\n")
    _write(tmp_path, "src/app.py", "VALUE = 'candidate'\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )

    assert snapshot.changed_files == ["src/app.py"]
    with materialized_source_view(tmp_path, snapshot) as source:
        assert not (source / ".ai-sdlc/loops/runtime.json").exists()
        assert not (source / "build/generated.txt").exists()


def test_unstaged_snapshot_excludes_interpreter_cache_artifacts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/订单.py", "print('base')\n")
    _git(tmp_path, "add", "src/订单.py")
    _git(tmp_path, "commit", "-m", "add source")
    _write(tmp_path, "src/订单.py", "print('changed')\n")
    _write_bytes(tmp_path, "src/__pycache__/订单.cpython-311.pyc", b"\x00cache")
    _write(tmp_path, ".pytest_cache/v/cache/nodeids", "[]\n")
    _write(tmp_path, ".ruff_cache/index", "cache\n")
    _write(tmp_path, "htmlcov/index.html", "coverage\n")
    _write(tmp_path, "build/temp.txt", "build\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    assert snapshot.changed_files == ["src/订单.py"]
    assert snapshot.untracked_files == []


def test_unstaged_snapshot_excludes_ai_sdlc_runtime_artifacts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add source")
    _write(tmp_path, "src/app.py", "print('changed')\n")
    _write(tmp_path, ".ai-sdlc/loops/runtime.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/reviews/runtime.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-authorizations/op.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-results/op.json", "{}\n")
    _write(
        tmp_path,
        ".ai-sdlc/work-items/WI-001/codex-handoff.md",
        "runtime handoff\n",
    )

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    assert snapshot.changed_files == ["src/app.py"]
    assert snapshot.untracked_files == []


def test_staged_snapshot_changes_when_index_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('one')\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    first = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )
    assert first.staged_tree_oid == _git(tmp_path, "write-tree")

    target.write_text("print('two')\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    second = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert first.index_identity != second.index_identity
    assert first.staged_tree_oid != second.staged_tree_oid
    assert second.staged_tree_oid == _git(tmp_path, "write-tree")
    assert first.diff_hash != second.diff_hash
    assert revalidate_source_snapshot(tmp_path, first).fresh is False


def test_staged_snapshot_excludes_tracked_runtime_but_keeps_memory_rules(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/loops/round.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/reviews/review.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-authorizations/op.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-results/op.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add tracked baseline")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/loops/round.json", '{"round": 1}\n')
    _write(tmp_path, ".ai-sdlc/reviews/review.json", '{"review": 1}\n')
    _write(tmp_path, ".ai-sdlc/state/stage-close-authorizations/op.json", '{"v": 1}\n')
    _write(tmp_path, ".ai-sdlc/state/stage-close-results/op.json", '{"v": 1}\n')
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v1\n")
    _git(tmp_path, "add", ".")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.changed_files == [
        ".ai-sdlc/memory/constitution.md",
        "src/app.py",
    ]
    assert set(snapshot.file_digests) == set(snapshot.changed_files)
    assert set(python_sources(tmp_path, snapshot)) == {
        "src/app.py",
    }


def test_git_range_snapshot_excludes_tracked_runtime_from_after_view(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-authorizations/op.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-results/op.json", "{}\n")
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add tracked baseline")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/state/stage-close-authorizations/op.json", '{"v": 1}\n')
    _write(tmp_path, ".ai-sdlc/state/stage-close-results/op.json", '{"v": 1}\n')
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "change product and runtime")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref=base,
            head_ref="HEAD",
        )
    )

    assert snapshot.changed_files == [
        ".ai-sdlc/memory/constitution.md",
        "src/app.py",
    ]
    assert set(snapshot.file_digests) == set(snapshot.changed_files)
    assert set(python_sources(tmp_path, snapshot)) == {"src/app.py"}
    with materialized_source_view(tmp_path, snapshot) as source:
        assert not (source / ".ai-sdlc/work-items").exists()
        assert not (source / ".ai-sdlc/state/stage-close-results").exists()
        assert (source / ".ai-sdlc/memory/constitution.md").is_file()


def test_patch_snapshot_separates_raw_input_from_filtered_runtime_view(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 0\n")
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v0\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add tracked baseline")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v1\n")
    patch = _git(tmp_path, "diff", "--binary")
    _write(tmp_path, "selected.patch", patch + "\n")
    _git(tmp_path, "checkout", "--", ".")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="selected.patch",
        )
    )

    assert snapshot.changed_files == [
        ".ai-sdlc/memory/constitution.md",
        "src/app.py",
    ]
    assert snapshot.source_input_digest != snapshot.diff_hash
    assert set(python_sources(tmp_path, snapshot)) == {"src/app.py"}
    with materialized_source_view(tmp_path, snapshot) as source:
        assert not (source / ".ai-sdlc/work-items").exists()
        assert (source / "src/app.py").read_text("utf-8") == "VALUE = 1\n"

    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _write(tmp_path, ".ai-sdlc/work-items/WI-1/runtime.py", "VALUE = 2\n")
    _write(tmp_path, ".ai-sdlc/memory/constitution.md", "# v1\n")
    _write(tmp_path, "selected.patch", _git(tmp_path, "diff", "--binary") + "\n")

    freshness = revalidate_source_snapshot(tmp_path, snapshot)

    assert freshness.fresh is False
    assert freshness.reason == "source_content_changed"


def test_patch_snapshot_reconstructs_external_absolute_patch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _write(root, "README.md", "# Reviewed external patch\n")
    external_patch = tmp_path / "selected.patch"
    external_patch.write_text(
        _git(root, "diff", "--binary", "--", "README.md") + "\n",
        encoding="utf-8",
    )
    _git(root, "checkout", "--", "README.md")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=root,
            source_kind="patch",
            patch_file=str(external_patch.resolve()),
        )
    )

    assert snapshot.patch_file == str(external_patch.resolve())
    assert revalidate_source_snapshot(root, snapshot).fresh is True
    with materialized_source_view(root, snapshot) as source:
        assert (source / "README.md").read_text("utf-8") == (
            "# Reviewed external patch\n"
        )


def test_unstaged_snapshot_becomes_stale_when_only_index_changes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "print('base')\n")
    _write(tmp_path, "src/staged.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py", "src/staged.py")
    _git(tmp_path, "commit", "-m", "add sources")
    _write(tmp_path, "src/app.py", "print('unstaged')\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    _write(tmp_path, "src/staged.py", "print('staged')\n")
    _git(tmp_path, "add", "src/staged.py")
    freshness = revalidate_source_snapshot(tmp_path, snapshot)

    assert freshness.fresh is False
    assert freshness.reason == "index_identity_changed"


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_unstaged_snapshot_becomes_stale_when_index_flags_change(
    tmp_path: Path,
    index_flag: str,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "print('base')\n")
    _write(tmp_path, "src/hidden.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py", "src/hidden.py")
    _git(tmp_path, "commit", "-m", "add sources")
    _write(tmp_path, "src/app.py", "print('unstaged')\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    _git(tmp_path, "update-index", index_flag, "src/hidden.py")
    _write(tmp_path, "src/hidden.py", "print('hidden change')\n")
    freshness = revalidate_source_snapshot(tmp_path, snapshot)

    assert freshness.fresh is False
    assert freshness.reason == "index_identity_changed"


def test_git_range_snapshot_records_rename_delete_and_binary(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, "old name.py", "print('old')\n")
    _write_bytes(tmp_path, "asset.bin", b"\x00\x01")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add files")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "mv", "old name.py", "新 名称.py")
    (tmp_path / "asset.bin").unlink()
    _write_bytes(tmp_path, "new.bin", b"\x00\x02")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "rename and binary")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref=base,
            head_ref="HEAD",
        )
    )

    assert "新 名称.py" in snapshot.changed_files
    assert "asset.bin" in snapshot.deleted_files
    assert "new.bin" in snapshot.binary_files
    assert snapshot.renamed_files == {"新 名称.py": "old name.py"}


def test_staged_snapshot_marks_only_binary_entry_in_mixed_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "print('ordinary source')\n")
    _write_bytes(tmp_path, "assets/payload.bin", b"\x00\x01\x02")
    _git(tmp_path, "add", "src/app.py", "assets/payload.bin")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.changed_files == ["assets/payload.bin", "src/app.py"]
    assert snapshot.binary_files == ["assets/payload.bin"]


def test_staged_snapshot_reads_diff_attributes_from_selected_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.dat -diff\n")
    _write(tmp_path, "assets/payload.dat", "opaque but textual bytes\n")
    _write(tmp_path, "src/app.py", "print('ordinary source')\n")
    _git(tmp_path, "add", ".gitattributes", "assets/payload.dat", "src/app.py")
    _write(tmp_path, ".gitattributes", "# live worktree override\n")
    _write(tmp_path, ".git/info/attributes", "*.dat diff\n")
    _write(tmp_path, "live.attributes", "*.dat diff\n")
    _git(
        tmp_path,
        "config",
        "core.attributesFile",
        str(tmp_path / "live.attributes"),
    )
    template = tmp_path / "hostile-template"
    _write(template, "info/attributes", "*.dat diff\n")
    monkeypatch.setenv("GIT_ATTR_SOURCE", base)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.binary_files == ["assets/payload.dat"]
    _write(tmp_path, ".gitattributes", "# second live override\n")
    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is True


def test_staged_snapshot_materializes_attributes_without_host_smudge(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.dat -diff\n")
    _write(tmp_path, "assets/payload.dat", "opaque but textual bytes\n")
    _git(tmp_path, "add", ".gitattributes", "assets/payload.dat")
    filter_script = tmp_path / "host-filter.py"
    filter_script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read().replace(b'-diff', b'diff'))\n",
        encoding="utf-8",
    )
    _write(tmp_path, ".git/info/attributes", ".gitattributes filter=evil\n")
    _git(
        tmp_path,
        "config",
        "filter.evil.smudge",
        f'"{sys.executable}" "{filter_script}"',
    )
    _git(tmp_path, "config", "filter.evil.required", "true")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.binary_files == ["assets/payload.dat"]


def test_materialized_staged_view_preserves_selected_bytes_without_host_smudge(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    selected = "def test_selected_source():\n    assert 'SELECTED'\n"
    _write(tmp_path, "tests/test_selected.py", selected)
    _git(tmp_path, "add", "tests/test_selected.py")
    filter_script = tmp_path / "host-filter.py"
    filter_script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write("
        "sys.stdin.buffer.read().replace(b'SELECTED', b'HOST_SMUDGED'))\n",
        encoding="utf-8",
    )
    _write(
        tmp_path,
        ".git/info/attributes",
        "tests/test_selected.py filter=evil\n",
    )
    _git(
        tmp_path,
        "config",
        "filter.evil.smudge",
        f'"{sys.executable}" "{filter_script}"',
    )
    _git(tmp_path, "config", "filter.evil.required", "true")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    with materialized_source_view(tmp_path, snapshot) as source_view:
        actual = (source_view / "tests/test_selected.py").read_text(encoding="utf-8")

    assert actual == selected


@pytest.mark.parametrize("source_kind", ["local-staged", "local-git-range", "patch"])
def test_materialized_selected_view_preserves_blob_bytes_despite_eol_attributes(
    tmp_path: Path,
    source_kind: str,
) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.py text eol=crlf\n")
    _write(tmp_path, "src/app.py", "VALUE = 1\nVALUE = 2\n")
    _git(tmp_path, "add", ".gitattributes", "src/app.py")
    if source_kind == "local-staged":
        options = SourceSnapshotOptions(root=tmp_path, source_kind=source_kind)
    elif source_kind == "local-git-range":
        _git(tmp_path, "commit", "-m", "add selected source")
        head = _git(tmp_path, "rev-parse", "HEAD")
        options = SourceSnapshotOptions(
            root=tmp_path,
            source_kind=source_kind,
            base_ref=base,
            head_ref=head,
        )
    else:
        patch = _git(tmp_path, "diff", "--cached", "--binary", "--no-ext-diff")
        _write(tmp_path, "change.patch", patch + "\n")
        options = SourceSnapshotOptions(
            root=tmp_path,
            source_kind=source_kind,
            patch_file="change.patch",
        )
    snapshot = build_source_snapshot(options)
    selected_bytes = file_versions(tmp_path, snapshot, "src/app.py")[1]

    with materialized_source_view(tmp_path, snapshot) as source_view:
        actual = (source_view / "src/app.py").read_bytes()

    assert actual == selected_bytes


def test_materialized_view_reads_regular_blobs_with_one_git_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    for number in range(8):
        _write(tmp_path, f"src/module_{number}.py", f"VALUE = {number}\n")
    _git(tmp_path, "add", "src")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )
    real_popen = source_snapshot_view.subprocess.Popen
    cat_file_processes = 0

    def counted_popen(*args: object, **kwargs: object):
        nonlocal cat_file_processes
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, (list, tuple)) and "cat-file" in command:
            cat_file_processes += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(source_snapshot_view.subprocess, "Popen", counted_popen)

    with materialized_source_view(tmp_path, snapshot) as source_view:
        assert (source_view / "src/module_7.py").read_bytes() == b"VALUE = 7\n"

    assert cat_file_processes == 1


def test_materialized_staged_view_rejects_index_changed_after_snapshot(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "tests/test_selected.py", "print('SNAPSHOT')\n")
    _git(tmp_path, "add", "tests/test_selected.py")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )
    _write(tmp_path, "tests/test_selected.py", "print('MUTATED_AFTER_FRESHNESS')\n")
    _git(tmp_path, "add", "tests/test_selected.py")

    with (
        pytest.raises(ValueError, match="index identity"),
        materialized_source_view(tmp_path, snapshot),
    ):
        pass


def test_materialized_unstaged_view_rejects_changed_bytes_after_snapshot(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    reference = "tests/test_selected.py"
    _write(tmp_path, reference, "print('ORIGINAL_UNSTAGED')\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _write(tmp_path, reference, "print('MUTATED_UNSTAGED')\n")

    with (
        pytest.raises(ValueError, match="unstaged source identity"),
        materialized_source_view(tmp_path, snapshot),
    ):
        pass


def test_materialized_patch_view_rejects_changed_patch_after_snapshot(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    patch_path = tmp_path / "change.patch"
    patch_path.write_text(_new_file_patch("ORIGINAL_PATCH"), encoding="utf-8")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="change.patch",
        )
    )
    patch_path.write_text(_new_file_patch("MUTATED_PATCH"), encoding="utf-8")

    with (
        pytest.raises(ValueError, match="patch identity"),
        materialized_source_view(tmp_path, snapshot),
    ):
        pass


def test_unstaged_snapshot_detects_untracked_symlink_retarget_with_same_bytes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "outside_a.txt", "same bytes\n")
    _write(tmp_path, "outside_b.txt", "same bytes\n")
    link = tmp_path / "data" / "link"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to("../outside_a.txt")
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    link.unlink()
    link.symlink_to("../outside_b.txt")

    freshness = revalidate_source_snapshot(tmp_path, snapshot)

    assert freshness.fresh is False
    assert freshness.reason == "diff_hash_changed"


def test_unstaged_symlink_digest_and_python_source_use_link_target_bytes(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, ".git/info/exclude", "outside.py\n")
    _write(tmp_path, "outside.py", "print('ONE')\n")
    link = tmp_path / "data" / "link.py"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to("../outside.py")
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    first = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    expected_bytes = b"../outside.py"
    expected_digest = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()

    assert first.file_digests["data/link.py"] == expected_digest
    assert python_sources(tmp_path, first)["data/link.py"] == expected_bytes

    _write(tmp_path, "outside.py", "print('TWO')\n")
    second = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    assert second.diff_hash == first.diff_hash
    assert second.file_digests["data/link.py"] == expected_digest


def test_unstaged_broken_python_symlink_remains_source_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    link = tmp_path / "data" / "link.py"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to("../missing.py")
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )

    assert python_sources(tmp_path, snapshot)["data/link.py"] == b"../missing.py"


def test_staged_snapshot_ignores_default_user_global_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "assets/payload.dat", "ordinary source text\n")
    _git(tmp_path, "add", "assets/payload.dat")
    xdg_home = tmp_path / "host-xdg"
    _write(xdg_home, "git/attributes", "*.dat -diff\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_home))

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.binary_files == []


def test_git_range_snapshot_reads_diff_attributes_from_selected_head(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.dat -diff\n")
    _write(tmp_path, "assets/payload.dat", "opaque but textual bytes\n")
    _write(tmp_path, "src/app.py", "print('ordinary source')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add selected attributes")
    head = _git(tmp_path, "rev-parse", "HEAD")
    _write(tmp_path, ".gitattributes", "# live worktree override\n")
    _write(tmp_path, ".git/info/attributes", "*.dat diff\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref=base,
            head_ref=head,
        )
    )

    assert snapshot.binary_files == ["assets/payload.dat"]
    _write(tmp_path, ".gitattributes", "# second live override\n")
    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is True


def test_patch_snapshot_reads_diff_attributes_from_selected_patch(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, ".gitattributes", "*.dat -diff\n")
    _write(tmp_path, "assets/payload.dat", "opaque but textual bytes\n")
    _write(tmp_path, "src/app.py", "print('ordinary source')\n")
    _git(tmp_path, "add", ".gitattributes", "assets/payload.dat", "src/app.py")
    patch = _git(tmp_path, "diff", "--cached", "--binary", "--no-ext-diff")
    (tmp_path / "change.patch").write_bytes(
        (patch + "\n").replace("\n", "\r\n").encode("utf-8")
    )
    _write(tmp_path, ".gitattributes", "# live worktree override\n")
    _write(tmp_path, ".git/info/attributes", "*.dat diff\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="change.patch",
        )
    )

    assert snapshot.binary_files == ["assets/payload.dat"]
    _write(tmp_path, ".gitattributes", "# second live override\n")
    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is True


def test_patch_snapshot_status_uses_explicit_non_head_base(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _write(
        tmp_path,
        "change.patch",
        """diff --git a/src/from-patch.py b/src/from-patch.py
new file mode 100644
--- /dev/null
+++ b/src/from-patch.py
@@ -0,0 +1 @@
+VALUE = 1
""",
    )
    _write(tmp_path, "unrelated.txt", "newer HEAD content\n")
    _git(tmp_path, "add", "unrelated.txt")
    _git(tmp_path, "commit", "-m", "advance current HEAD")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            head_ref=base,
            patch_file="change.patch",
        )
    )

    assert snapshot.changed_files == ["src/from-patch.py"]


def test_patch_snapshot_materializes_selected_index_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    for index in range(3):
        _write(tmp_path, f"src/file-{index}.py", f"VALUE = {index}\n")
    _git(tmp_path, "add", "src")
    patch = _git(tmp_path, "diff", "--cached", "--binary", "--no-ext-diff")
    _git(tmp_path, "reset", "--hard", "HEAD")
    _write(tmp_path, "change.patch", patch + "\n")
    original = source_snapshot_view._apply_patch
    calls = 0

    def counted(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_snapshot_view, "_apply_patch", counted)

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="change.patch",
        )
    )

    assert len(snapshot.changed_files) == 3
    assert calls == 1


def test_large_patch_uses_bounded_capture_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    for index in range(250):
        _write(tmp_path, f"src/long-name-{index:03d}.py", f"VALUE = {index}\n")
    _git(tmp_path, "add", "src")
    patch = _git(tmp_path, "diff", "--cached", "--binary", "--no-ext-diff")
    _git(tmp_path, "reset", "--hard", "HEAD")
    _write(tmp_path, "change.patch", patch + "\n")
    original = source_change_capture.subprocess.run
    capture_commands: list[list[str]] = []

    def counted(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list):
            capture_commands.append(command)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_change_capture.subprocess, "run", counted)

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="change.patch",
        )
    )

    assert len(snapshot.changed_files) == 250
    assert sum("cat-file" in command for command in capture_commands) <= 2
    command_bound = 17 + len(source_snapshot_module._runtime_pathspecs())
    assert all(len(command) <= command_bound for command in capture_commands)


def test_staged_snapshot_supports_sha256_repository(tmp_path: Path) -> None:
    initialized = subprocess.run(
        [
            "git",
            "init",
            "--quiet",
            "--initial-branch=main",
            "--object-format=sha256",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    if initialized.returncode:
        pytest.skip("installed Git does not support SHA-256 repositories")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _write(tmp_path, "README.md", "# SHA-256 Test\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _git(tmp_path, "add", "src/app.py")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.changed_files == ["src/app.py"]


def test_staged_sha1_snapshot_ignores_hostile_default_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _git(tmp_path, "add", "src/app.py")
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha256")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )

    assert snapshot.changed_files == ["src/app.py"]


@pytest.mark.parametrize(
    "payload",
    [
        b"1\t0\ta.py\0\0-\t-\tb.bin\0",
        b"-\t-\tb.bin",
        b"x\t0\ta.py\0",
        b"-\t0\tb.bin\0",
    ],
)
def test_binary_numstat_parser_rejects_malformed_payload(payload: bytes) -> None:
    with pytest.raises(ValueError):
        _parse_binary_numstat(payload)


def test_binary_numstat_paths_must_exactly_match_name_status(tmp_path: Path) -> None:
    parts = _SnapshotParts(
        diff_bytes=b"",
        status_bytes=b"",
        numstat_bytes=b"1\t0\ta.py\0",
        base_ref="HEAD",
        head_ref="INDEX",
        base_commit="a",
        head_commit="a",
    )

    with pytest.raises(ValueError, match="numstat paths do not match"):
        _binary_files(tmp_path, parts, [("A", "a.py", ""), ("A", "b.py", "")])


def test_patch_snapshot_hashes_raw_bytes_and_detects_tamper(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    patch = tmp_path / "change.patch"
    patch.write_bytes(
        b"diff --git a/src/app.py b/src/app.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n+++ b/src/app.py\n@@ -0,0 +1 @@\n+print('x')\n"
    )
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="patch",
            patch_file="change.patch",
        )
    )

    assert snapshot.changed_files == ["src/app.py"]
    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is True
    patch.write_bytes(patch.read_bytes() + b"\n")
    assert revalidate_source_snapshot(tmp_path, snapshot).fresh is False


def test_canonical_content_digest_matches_autocrlf_source_transition(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "def value():\n    return 0\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    _git(tmp_path, "config", "core.autocrlf", "true")
    _write_bytes(tmp_path, "src/app.py", b"def value():\r\n    return 1\r\n")

    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "change app")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert evaluated.diff_hash == current.diff_hash
    assert evaluated.file_digests != current.file_digests
    assert evaluated.canonical_file_digests == current.canonical_file_digests
    legacy = current.model_copy(
        update={
            "source_kind": "local-unstaged",
            "canonical_digest_kind": "",
            "canonical_file_digests": {},
        }
    )
    assert source_content_equivalent(legacy, current) is True


@pytest.mark.parametrize(
    "fixture_kind",
    ["regular", "executable", "symlink", "binary"],
)
def test_cross_source_identity_matches_committed_untracked_file(
    tmp_path: Path,
    fixture_kind: str,
) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src" / "new-entry"
    path.parent.mkdir(parents=True)
    if fixture_kind == "symlink":
        try:
            path.symlink_to("target.py")
        except OSError:
            pytest.skip("symlinks are unavailable in this environment")
    else:
        path.write_bytes(b"\0binary\r\n" if fixture_kind == "binary" else b"VALUE\n")
        if fixture_kind == "executable":
            path.chmod(0o755)

    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "src/new-entry")
    _git(tmp_path, "commit", "-m", f"add {fixture_kind} entry")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert source_content_equivalent(evaluated, current) is True


def test_cross_source_identity_ignores_rename_heuristic_representation(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/old.py", "VALUE = 1\n")
    _git(tmp_path, "add", "src/old.py")
    _git(tmp_path, "commit", "-m", "add rename source")
    (tmp_path / "src/old.py").rename(tmp_path / "src/new.py")
    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "rename source")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert evaluated.renamed_files == {}
    assert current.renamed_files == {"src/new.py": "src/old.py"}
    assert source_content_equivalent(evaluated, current) is True


def test_cross_source_identity_treats_intent_to_add_as_absent_before(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/new.py", "VALUE = 1\n")
    _git(tmp_path, "add", "-N", "src/new.py")
    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "src/new.py")
    _git(tmp_path, "commit", "-m", "add intent source")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert source_content_equivalent(evaluated, current) is True


@pytest.mark.parametrize("source_kind", ["local-unstaged", "local-worktree"])
@pytest.mark.parametrize("same_target", [True, False])
def test_cross_source_identity_binds_gitlink_target_oid(
    tmp_path: Path,
    same_target: bool,
    source_kind: str,
) -> None:
    root, c2, c3 = _gitlink_fixture(tmp_path)
    _git(root / "vendor/sub", "checkout", c2)
    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=root, source_kind=source_kind)
    )
    if not same_target:
        _git(root / "vendor/sub", "checkout", c3)
    _git(root, "add", "vendor/sub")
    _git(root, "commit", "-m", "advance gitlink")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=root,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert source_content_equivalent(evaluated, current) is same_target


@pytest.mark.parametrize("source_kind", ["local-unstaged", "local-worktree"])
def test_dirty_gitlink_is_rejected_fail_closed(
    tmp_path: Path,
    source_kind: str,
) -> None:
    root, c2, _c3 = _gitlink_fixture(tmp_path)
    _git(root / "vendor/sub", "checkout", c2)
    _write(root / "vendor/sub", "version.txt", "dirty source\n")

    with pytest.raises(ValueError, match="dirty gitlink"):
        build_source_snapshot(SourceSnapshotOptions(root=root, source_kind=source_kind))


def test_dirty_unchanged_gitlink_is_not_hidden_by_parent_source_change(
    tmp_path: Path,
) -> None:
    root, _c2, _c3 = _gitlink_fixture(tmp_path)
    _write(root, "README.md", "# Parent change\n")
    _write(root / "vendor/sub", "README.md", "# Dirty submodule\n")

    with pytest.raises(ValueError, match="dirty gitlink"):
        build_source_snapshot(
            SourceSnapshotOptions(root=root, source_kind="local-worktree")
        )


def test_gitlink_capture_does_not_execute_submodule_fsmonitor(
    tmp_path: Path,
) -> None:
    root, c2, _c3 = _gitlink_fixture(tmp_path)
    submodule = root / "vendor/sub"
    _git(submodule, "checkout", c2)
    marker = tmp_path / "submodule-fsmonitor-invoked.txt"
    hook = tmp_path / "submodule-fsmonitor"
    hook.write_text(
        f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(submodule, "config", "core.fsmonitor", str(hook))

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=root, source_kind="local-worktree")
    )

    assert snapshot.changed_files == ["vendor/sub"]
    assert not marker.exists()


def test_gitlink_capture_overrides_parent_submodule_config_without_child_filters(
    tmp_path: Path,
) -> None:
    root, c2, _c3 = _gitlink_fixture(tmp_path)
    submodule = root / "vendor/sub"
    _git(submodule, "checkout", c2)
    clean_marker = tmp_path / "submodule-clean-filter-invoked.txt"
    clean_script = tmp_path / "submodule-clean-filter.py"
    clean_script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(clean_marker)!r}).write_text('invoked')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    diff_marker = tmp_path / "submodule-diff-driver-invoked.txt"
    diff_script = tmp_path / "submodule-diff-driver.py"
    diff_script.write_text(
        f"import pathlib\npathlib.Path({str(diff_marker)!r}).write_text('invoked')\n",
        encoding="utf-8",
    )
    git_dir = Path(_git(submodule, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = (submodule / git_dir).resolve()
    attributes = git_dir / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text(
        "version.txt filter=sideeffect diff=sideeffect\n",
        encoding="utf-8",
    )
    _git(
        submodule,
        "config",
        "filter.sideeffect.clean",
        f'"{sys.executable}" "{clean_script}"',
    )
    _git(submodule, "config", "filter.sideeffect.required", "true")
    _git(
        submodule,
        "config",
        "diff.sideeffect.command",
        f'"{sys.executable}" "{diff_script}"',
    )
    _git(root, "config", "diff.ignoreSubmodules", "all")
    _git(root, "config", "diff.submodule", "diff")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=root, source_kind="local-worktree")
    )

    assert snapshot.changed_files == ["vendor/sub"]
    assert not clean_marker.exists()
    assert not diff_marker.exists()


@pytest.mark.parametrize("identity_kind", ["source-change-v1", "future-change-v2"])
def test_cross_source_partial_or_unknown_identity_is_unverifiable(
    tmp_path: Path,
    identity_kind: str,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )
    partial = evaluated.model_copy(
        update={"change_identity_kind": identity_kind, "raw_change_identities": {}}
    )

    matches, blocker = compare_source_content(partial, current)

    assert matches is False
    assert "identity" in blocker.lower()


def test_same_source_freshness_detects_raw_change_hidden_by_clean_filter(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add filter target")
    _configure_constant_clean_filter(tmp_path)
    _write(tmp_path, "src/app.py", "print('FIRST RAW SOURCE')\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _write(tmp_path, "src/app.py", "raise SystemExit(99)\n")

    freshness = revalidate_source_snapshot(tmp_path, snapshot)

    assert freshness.fresh is False
    assert freshness.reason == "diff_hash_changed"


@pytest.mark.parametrize("source_kind", ["local-unstaged", "local-worktree"])
def test_worktree_snapshot_capture_does_not_execute_external_clean_filter(
    tmp_path: Path,
    source_kind: str,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add filter target")
    marker = tmp_path / "filter-invoked.txt"
    script = tmp_path / ".git" / "side-effect-filter.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('invoked')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    _write(tmp_path, ".git/info/attributes", "src/app.py filter=sideeffect\n")
    _git(
        tmp_path,
        "config",
        "filter.sideeffect.clean",
        f'"{sys.executable}" "{script}"',
    )
    _git(tmp_path, "config", "filter.sideeffect.required", "true")
    _write(tmp_path, "src/app.py", "VALUE = 1\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind=source_kind)
    )
    source_change_capture.capture_path_changes(tmp_path, snapshot)

    assert snapshot.changed_files == ["src/app.py"]
    assert not marker.exists()


def test_worktree_snapshot_does_not_execute_repository_fsmonitor(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add fsmonitor target")
    marker = tmp_path / ".git" / "fsmonitor-invoked.txt"
    hook = tmp_path / ".git" / "fsmonitor-side-effect"
    hook.write_text(
        f"#!/bin/sh\ntouch '{marker.as_posix()}'\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git(tmp_path, "config", "core.fsmonitor", str(hook))
    _write(tmp_path, "src/app.py", "VALUE = 1\n")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )

    assert snapshot.changed_files == ["src/app.py"]
    assert not marker.exists()


def test_worktree_symlink_carrier_preserves_git_mode_and_materializes_as_file(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    link = tmp_path / "link.txt"
    try:
        link.symlink_to("old-target")
    except OSError:
        pytest.skip("test setup cannot create the tracked symlink")
    _git(tmp_path, "add", "link.txt")
    _git(tmp_path, "commit", "-m", "add symlink")
    link.unlink()
    link.write_bytes(b"new-target")
    _git(tmp_path, "config", "core.symlinks", "false")

    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-worktree")
    )
    changes = source_change_capture.capture_path_changes(tmp_path, snapshot)

    assert changes["link.txt"].before.mode == "120000"
    assert changes["link.txt"].after.mode == "120000"
    assert changes["link.txt"].after.payload == b"new-target"
    assert changes["link.txt"].after.symlink_carrier is True
    with materialized_source_view(tmp_path, snapshot) as source:
        selected = source / "link.txt"
        assert selected.is_symlink() is False
        assert selected.read_bytes() == b"new-target"


@pytest.mark.parametrize("source_kind", ["local-worktree", "local-git-range", "patch"])
def test_materialized_source_does_not_execute_post_index_change_hook(
    tmp_path: Path,
    source_kind: str,
) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    if source_kind == "local-worktree":
        _git(tmp_path, "add", "src/app.py")
        _git(tmp_path, "commit", "-m", "add source")
        _write(tmp_path, "src/app.py", "VALUE = 2\n")
        options = SourceSnapshotOptions(root=tmp_path, source_kind=source_kind)
    elif source_kind == "local-git-range":
        _git(tmp_path, "add", "src/app.py")
        _git(tmp_path, "commit", "-m", "add source")
        options = SourceSnapshotOptions(
            root=tmp_path,
            source_kind=source_kind,
            base_ref=base,
            head_ref="HEAD",
        )
    else:
        _git(tmp_path, "add", "src/app.py")
        patch = _git(tmp_path, "diff", "--cached", "--binary", "--no-ext-diff")
        _git(tmp_path, "reset", "--hard", "HEAD")
        _write(tmp_path, "change.patch", patch + "\n")
        options = SourceSnapshotOptions(
            root=tmp_path,
            source_kind=source_kind,
            patch_file="change.patch",
        )
    snapshot = build_source_snapshot(options)
    marker = tmp_path / ".git" / "post-index-change-invoked.txt"
    hook = tmp_path / ".git" / "hooks" / "post-index-change"
    hook.write_text(
        f"#!/bin/sh\ntouch '{marker.as_posix()}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with materialized_source_view(tmp_path, snapshot) as source:
        assert (source / "src/app.py").is_file()

    assert not marker.exists()


def test_cross_source_identity_rejects_semantic_clean_filter_rewrite(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 0\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add filter target")
    _configure_constant_clean_filter(tmp_path)
    _write(tmp_path, "src/app.py", "raise SystemExit(99)\n")
    evaluated = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "commit filtered source")
    current = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref="HEAD^",
            head_ref="HEAD",
        )
    )

    assert source_content_equivalent(evaluated, current) is False


def test_git_content_identity_times_out_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write_bytes(tmp_path, "src/app.py", b"VALUE = 1\r\n")
    original = source_content_identity.subprocess.run
    observed: list[float | None] = []

    def timed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "hash-object" in command:
            observed.append(kwargs.get("timeout"))  # type: ignore[arg-type]
            raise subprocess.TimeoutExpired(command, 10)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_content_identity.subprocess, "run", timed_run)

    with pytest.raises(ValueError, match="timed out"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
        )

    assert observed == [10]


def test_source_capture_git_timeout_is_normalized_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    original = source_change_capture.subprocess.run

    def timed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "diff" in command and "--raw" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_change_capture.subprocess, "run", timed_run)

    with pytest.raises(ValueError, match="timed out"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
        )


def test_optional_blob_read_timeout_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "add app")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(
            root=tmp_path,
            source_kind="local-git-range",
            base_ref=base,
            head_ref="HEAD",
        )
    )
    original = source_snapshot_view.subprocess.run

    def timed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "cat-file" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_snapshot_view.subprocess, "run", timed_run)

    with pytest.raises(ValueError, match="timed out"):
        file_versions(tmp_path, snapshot, "src/app.py")


def test_blob_read_distinguishes_missing_path_from_invalid_revision(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    assert source_snapshot_view._revision_blob(tmp_path, "HEAD", "missing.py") == b""
    with pytest.raises(ValueError, match="failed"):
        source_snapshot_view._revision_blob(
            tmp_path,
            "definitely-not-a-revision",
            "README.md",
        )


def test_materialized_batch_blob_timeout_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    _git(tmp_path, "add", "src/app.py")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-staged")
    )
    original = source_change_capture.subprocess.run

    def timed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "cat-file" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_change_capture.subprocess, "run", timed_run)

    with (
        pytest.raises(ValueError, match="timed out"),
        materialized_source_view(tmp_path, snapshot),
    ):
        pass


def test_unstaged_diff_timeout_is_normalized_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    original = source_snapshot_module.subprocess.run

    def timed_run(*args: object, **kwargs: object):
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "diff" in command:
            raise subprocess.TimeoutExpired(command, 30)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_snapshot_module.subprocess, "run", timed_run)

    with pytest.raises(ValueError, match="timed out"):
        build_source_snapshot(
            SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
        )


@pytest.mark.parametrize(
    ("identity_kind", "retain_payload"),
    [("future-change-v2", True), ("", True)],
)
def test_same_source_freshness_rejects_invalid_identity_extension(
    tmp_path: Path,
    identity_kind: str,
    retain_payload: bool,
) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/app.py", "VALUE = 1\n")
    snapshot = build_source_snapshot(
        SourceSnapshotOptions(root=tmp_path, source_kind="local-unstaged")
    )
    invalid = snapshot.model_copy(
        update={
            "change_identity_kind": identity_kind,
            "raw_change_identities": (
                snapshot.raw_change_identities if retain_payload else {}
            ),
        }
    )

    freshness = revalidate_source_snapshot(tmp_path, invalid)

    assert freshness.fresh is False
    assert freshness.reason.startswith("source_identity_invalid:")


def _configure_constant_clean_filter(root: Path) -> None:
    script = root / ".git" / "constant-filter.py"
    script.write_text(
        "import sys\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'VALUE = 1\\n')\n",
        encoding="utf-8",
    )
    _write(root, ".git/info/attributes", "src/app.py filter=constant\n")
    _git(root, "config", "filter.constant.clean", f'"{sys.executable}" "{script}"')
    _git(root, "config", "filter.constant.required", "true")


def _gitlink_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    module = tmp_path / "module"
    module.mkdir()
    _init_repo(module)
    root = tmp_path / "main"
    root.mkdir()
    _init_repo(root)
    _git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(module),
        "vendor/sub",
    )
    _git(root, "commit", "-am", "add gitlink")
    _write(module, "version.txt", "c2\n")
    _git(module, "add", "version.txt")
    _git(module, "commit", "-m", "c2")
    c2 = _git(module, "rev-parse", "HEAD")
    _write(module, "version.txt", "c3\n")
    _git(module, "commit", "-am", "c3")
    c3 = _git(module, "rev-parse", "HEAD")
    _git(root / "vendor/sub", "fetch")
    return root, c2, c3


def _new_file_patch(marker: str) -> str:
    return (
        "diff --git a/tests/test_selected.py b/tests/test_selected.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_selected.py\n"
        "@@ -0,0 +1 @@\n"
        f"+print('{marker}')\n"
    )


def _init_repo(root: Path) -> str:
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _write(root, "README.md", "# Test\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return _git(root, "rev-parse", "HEAD")


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_bytes(root: Path, relative: str, content: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout.decode("utf-8", errors="strict").strip()

"""Read file content from the exact source view named by a SourceSnapshot."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ai_sdlc.core.git_filter_safety import (
    external_filter_overrides,
    safe_git_read_command,
    safe_git_read_environment,
)
from ai_sdlc.core.source_change_capture import (
    CapturedPathChange,
    capture_path_changes,
    read_git_blobs,
    read_index_states,
    read_tree_states,
)
from ai_sdlc.core.source_snapshot import (
    _AI_SDLC_RUNTIME_PREFIXES,
    SourceSnapshot,
    _filtered_index_records,
    _is_runtime_artifact,
    _patch_path,
    _runtime_pathspecs,
)
from ai_sdlc.core.source_snapshot import (
    _untracked_payload as _snapshot_untracked_payload,
)


def _is_python_source_path(path: str) -> bool:
    return path.casefold().endswith(".py")


def file_versions(
    root: Path, snapshot: SourceSnapshot, path: str
) -> tuple[bytes, bytes]:
    """Return before/after bytes from the frozen snapshot, including renames."""

    base_path = snapshot.renamed_files.get(path, path)
    if snapshot.source_kind == "local-git-range":
        return _revision_blob(root, snapshot.base_commit, base_path), _revision_blob(
            root, snapshot.head_commit, path
        )
    if snapshot.source_kind == "local-staged":
        return _revision_blob(root, "HEAD", base_path), _index_blob(root, path)
    if snapshot.source_kind == "local-unstaged":
        before = (
            b"" if path in snapshot.untracked_files else _index_blob(root, base_path)
        )
        return before, _worktree_blob(root, path)
    if snapshot.source_kind == "local-worktree":
        changes = capture_path_changes(
            root,
            snapshot,
            git_config_args=external_filter_overrides(root),
        )
        verify_captured_changes(snapshot, changes)
        return _version_map(snapshot, changes)[path]
    with _patched_index(root, snapshot) as env:
        return _revision_blob(root, snapshot.base_commit, base_path), _index_blob(
            root, path, env=env
        )


def python_sources(root: Path, snapshot: SourceSnapshot) -> dict[str, bytes]:
    """Return every Python file from the same frozen after-view as the metrics."""

    if snapshot.source_kind == "local-git-range":
        return _revision_python_sources(root, snapshot.head_commit)
    if snapshot.source_kind == "local-staged":
        return _index_python_sources(root)
    if snapshot.source_kind == "patch":
        with _patched_index(root, snapshot) as env:
            return _index_python_sources(root, env=env)
    if snapshot.source_kind == "local-worktree":
        changes = capture_path_changes(
            root,
            snapshot,
            git_config_args=external_filter_overrides(root),
        )
        verify_captured_changes(snapshot, changes)
        return _worktree_python_sources_from_head(root, snapshot, changes)
    paths = set(_nul_paths(_git(root, "ls-files", "-z")))
    paths.update(snapshot.untracked_files)
    return {
        path: _worktree_blob(root, path)
        for path in sorted(paths)
        if _is_python_source_path(path)
        and not _is_runtime_artifact(path)
        and ((root / path).is_symlink() or (root / path).is_file())
    }


def _worktree_python_sources_from_head(
    root: Path,
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> dict[str, bytes]:
    sources = _revision_python_sources(root, snapshot.base_commit)
    removed = set(snapshot.deleted_files) | set(snapshot.renamed_files.values())
    for path in removed:
        sources.pop(path, None)
    for path in snapshot.changed_files:
        change = changes.get(path)
        if change is None:
            raise ValueError(f"source state is missing for changed path: {path}")
        if _is_python_source_path(path) and change.after.mode in {
            "100644",
            "100755",
            "120000",
        }:
            sources[path] = change.after.payload
        else:
            sources.pop(path, None)
    return sources


def _verify_captured_changes(
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> None:
    verify_captured_changes(snapshot, changes)


def verify_captured_changes(
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> None:
    """确认一次捕获仍与持久化 SourceSnapshot 的原始身份完全一致。"""

    from ai_sdlc.core.source_content_identity import (
        CHANGE_IDENTITY_KIND,
        build_raw_change_identities,
    )
    from ai_sdlc.core.source_snapshot import source_snapshot_identity_issue

    issue = source_snapshot_identity_issue(snapshot)
    if issue:
        raise ValueError(issue)
    if snapshot.change_identity_kind != CHANGE_IDENTITY_KIND:
        return
    current = build_raw_change_identities(snapshot, changes)
    if current != snapshot.raw_change_identities:
        raise ValueError("source content changed after snapshot capture")


def _version_map(
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> dict[str, tuple[bytes, bytes]]:
    versions: dict[str, tuple[bytes, bytes]] = {}
    for path in snapshot.changed_files:
        before_path = snapshot.renamed_files.get(path, path)
        if before_path not in changes or path not in changes:
            raise ValueError(f"source state is missing for changed path: {path}")
        versions[path] = (
            changes[before_path].before.payload,
            changes[path].after.payload,
        )
    return versions


@contextmanager
def materialized_source_view(
    root: Path,
    snapshot: SourceSnapshot,
) -> Iterator[Path]:
    """Yield a filesystem view whose bytes match the selected snapshot after-view."""

    if snapshot.source_kind == "local-unstaged":
        with _index_worktree(
            root,
            expected_index_identity=snapshot.index_identity,
        ) as env:
            target = Path(env["GIT_WORK_TREE"])
            _overlay_unstaged_source(root, target, snapshot, env)
            _remove_runtime_artifacts(target)
            yield target
        return
    if snapshot.source_kind == "local-worktree":
        changes = capture_path_changes(
            root,
            snapshot,
            git_config_args=external_filter_overrides(root),
        )
        _verify_captured_changes(snapshot, changes)
        with (
            _revision_index(root, snapshot.base_commit) as index_env,
            _index_worktree(root, index_env) as env,
        ):
            target = Path(env["GIT_WORK_TREE"])
            _overlay_captured_source(target, snapshot, changes)
            _remove_runtime_artifacts(target)
            yield target
        return
    if snapshot.source_kind == "local-staged":
        with _index_worktree(
            root,
            expected_index_identity=snapshot.index_identity,
        ) as env:
            target = Path(env["GIT_WORK_TREE"])
            _remove_runtime_artifacts(target)
            yield target
        return
    if snapshot.source_kind == "loop-artifacts":
        with _index_worktree(
            root,
            expected_index_identity=snapshot.index_identity,
        ) as env:
            target = Path(env["GIT_WORK_TREE"])
            _remove_runtime_artifacts(target)
            yield target
        return
    if snapshot.source_kind == "local-git-range":
        with (
            _revision_index(root, snapshot.head_commit) as index_env,
            _index_worktree(root, index_env) as env,
        ):
            target = Path(env["GIT_WORK_TREE"])
            _remove_runtime_artifacts(target)
            yield target
        return
    with (
        _patched_index(root, snapshot) as index_env,
        _index_worktree(root, index_env) as env,
    ):
        target = Path(env["GIT_WORK_TREE"])
        _remove_runtime_artifacts(target)
        yield target


def _revision_python_sources(root: Path, revision: str) -> dict[str, bytes]:
    paths = _nul_paths(_git(root, "ls-tree", "-r", "--name-only", "-z", revision))
    python_paths = [
        path
        for path in paths
        if _is_python_source_path(path) and not _is_runtime_artifact(path)
    ]
    states = read_tree_states(root, revision, python_paths)
    return {path: states[path].payload for path in python_paths if path in states}


def _index_python_sources(
    root: Path, *, env: dict[str, str] | None = None
) -> dict[str, bytes]:
    paths = _nul_paths(_git(root, "ls-files", "-z", env=env))
    python_paths = [
        path
        for path in paths
        if _is_python_source_path(path) and not _is_runtime_artifact(path)
    ]
    states = read_index_states(root, python_paths, env=env)
    return {path: states[path].payload for path in python_paths if path in states}


@contextmanager
def _patched_index(root: Path, snapshot: SourceSnapshot) -> Iterator[dict[str, str]]:
    if not snapshot.patch_file:
        raise ValueError("patch source has no patch_file")
    patch_path = _patch_path(root.resolve(), snapshot.patch_file)
    patch = patch_path.read_bytes()
    expected_digest = snapshot.source_input_digest or snapshot.diff_hash
    if _payload_digest(patch) != expected_digest:
        raise ValueError("patch identity changed before materialization")
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-frozen-patch-") as directory:
        captured_patch = Path(directory) / "selected.patch"
        captured_patch.write_bytes(patch)
        with _patch_index(root, captured_patch, snapshot.base_commit) as env:
            yield env


@contextmanager
def _revision_index(root: Path, revision: str) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-revision-") as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        _git(root, "read-tree", revision, env=env)
        yield env


def _checkout_index(
    root: Path,
    target: Path,
    *,
    env: dict[str, str] | None = None,
) -> None:
    prefix = f"{target.as_posix()}/"
    _git(
        root,
        "checkout-index",
        "--all",
        "--force",
        f"--prefix={prefix}",
        env=env,
    )


def patch_diff_metadata(
    root: Path,
    patch_file: str,
    base_commit: str,
) -> tuple[bytes, bytes]:
    """Read status and numstat from one isolated patched source view."""

    patch_path = _patch_path(root.resolve(), patch_file)
    with (
        _patch_index(root, patch_path, base_commit) as env,
        _index_worktree(root, env) as selected_env,
    ):
        _diff, status, numstat = _diff_outputs(("--cached", base_commit), selected_env)
        return status, numstat


def selected_git_diff(
    root: Path,
    source_kind: str,
    *,
    base_commit: str = "",
    head_commit: str = "",
) -> tuple[bytes, bytes, bytes]:
    """Build diff outputs with attributes read from the selected after-view."""

    if source_kind == "local-staged" and base_commit:
        with _index_worktree(root) as env:
            return _diff_outputs(("--cached", base_commit), env)
    if source_kind == "local-git-range" and base_commit and head_commit:
        with (
            _revision_index(root, head_commit) as index_env,
            _index_worktree(root, index_env) as env,
        ):
            return _diff_outputs((base_commit, head_commit), env)
    raise ValueError(f"unsupported selected diff source: {source_kind}")


def _diff_outputs(
    selector: tuple[str, ...],
    env: dict[str, str],
) -> tuple[bytes, bytes, bytes]:
    worktree = Path(env["GIT_WORK_TREE"])
    paths = ("--", ".", *_runtime_pathspecs())
    return (
        _git(
            worktree,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=dirty",
            "--submodule=short",
            *selector,
            *paths,
            env=env,
        ),
        _git(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "-M",
            "--ignore-submodules=dirty",
            "--submodule=short",
            *selector,
            *paths,
            env=env,
        ),
        _git(
            worktree,
            "diff",
            "--numstat",
            "-z",
            "-M",
            "--ignore-submodules=dirty",
            "--submodule=short",
            *selector,
            *paths,
            env=env,
        ),
    )


@contextmanager
def _index_worktree(
    root: Path,
    index_env: dict[str, str] | None = None,
    expected_index_identity: str = "",
) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-attributes-") as directory:
        temporary_root = Path(directory).resolve()
        source_env = dict(index_env or os.environ)
        if "GIT_INDEX_FILE" not in source_env:
            source_env["GIT_INDEX_FILE"] = str(
                _repository_path(root, "--git-path", "index")
            )
        entries = _git(root, "ls-files", "-s", "-z", env=source_env)
        flags = _git(root, "ls-files", "-v", "-z", env=source_env)
        intent_to_add_paths = _index_intent_to_add_paths(root, source_env)
        captured_identity = _index_payload_identity(entries, flags)
        if expected_index_identity and captured_identity != expected_index_identity:
            raise ValueError(
                "selected source index identity changed before materialization"
            )
        target = temporary_root / "worktree"
        target.mkdir()
        metadata_root = temporary_root / "metadata"
        empty_config = temporary_root / "empty-gitconfig"
        empty_config.touch()
        empty_attributes = temporary_root / "empty-attributes"
        empty_attributes.touch()
        empty_template = temporary_root / "empty-template"
        empty_template.mkdir()
        init_env = _clean_git_environment(empty_config)
        object_format = (
            _git(root, "rev-parse", "--show-object-format")
            .decode("ascii", errors="strict")
            .strip()
        )
        init_args = ["init", "--quiet", f"--template={empty_template}"]
        if object_format:
            init_args.append(f"--object-format={object_format}")
        init_args.append(str(metadata_root))
        _git(root, *init_args, env=init_env)
        _git(
            metadata_root,
            "config",
            "core.attributesFile",
            str(empty_attributes),
            env=init_env,
        )
        object_dir = _repository_path(root, "--git-path", "objects")
        isolated_env = {
            key: value
            for key, value in source_env.items()
            if key
            not in {
                "GIT_COMMON_DIR",
                "GIT_DEFAULT_HASH",
                "GIT_DIR",
                "GIT_OBJECT_DIRECTORY",
                "GIT_WORK_TREE",
            }
            and not key.startswith("GIT_ATTR_")
            and not key.startswith("GIT_CONFIG_")
            and not key.startswith("GIT_TEMPLATE_")
        }
        selected_env = {
            **isolated_env,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": str(empty_config),
            "GIT_DIR": str(metadata_root / ".git"),
            "GIT_INDEX_FILE": str(temporary_root / "index"),
            "GIT_OBJECT_DIRECTORY": str(object_dir),
            "GIT_WORK_TREE": str(target),
        }
        _git(target, "read-tree", "--empty", env=selected_env)
        _git_input(
            target, ("update-index", "-z", "--index-info"), entries, selected_env
        )
        _checkout_index(target, target, env=selected_env)
        _restore_regular_index_blobs(target, target, selected_env)
        for path in intent_to_add_paths:
            _git(target, "update-index", "--force-remove", "--", path, env=selected_env)
            _git(target, "add", "--intent-to-add", "--", path, env=selected_env)
        yield selected_env


def _index_intent_to_add_paths(root: Path, env: dict[str, str]) -> tuple[str, ...]:
    """保留 intent-to-add 标志，避免未暂存重命名在隔离视图中退化为普通修改。"""

    payload = _git(root, "ls-files", "--debug", "-z", env=env)
    separator = payload.find(b"\0")
    if separator < 0:
        return ()
    raw_path = payload[:separator]
    cursor = separator + 1
    paths: list[str] = []
    while raw_path:
        marker = payload.find(b"flags: ", cursor)
        line_end = payload.find(b"\n", marker)
        if marker < 0 or line_end < 0:
            raise ValueError("malformed index debug record")
        raw_flags = payload[marker + len(b"flags: ") : line_end]
        try:
            entry_flags = int(raw_flags, 16)
        except ValueError as exc:
            raise ValueError("malformed index debug flags") from exc
        if entry_flags & 0x20000000:
            paths.append(raw_path.decode("utf-8", errors="strict"))
        cursor = line_end + 1
        if cursor >= len(payload):
            break
        separator = payload.find(b"\0", cursor)
        if separator < 0:
            raise ValueError("malformed index debug path")
        raw_path = payload[cursor:separator]
        cursor = separator + 1
    return tuple(paths)


def _clean_git_environment(empty_config: Path) -> dict[str, str]:
    cleaned = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DEFAULT_HASH",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        }
        and not key.startswith("GIT_ATTR_")
        and not key.startswith("GIT_CONFIG_")
        and not key.startswith("GIT_TEMPLATE_")
    }
    return {
        **cleaned,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": str(empty_config),
    }


def _restore_regular_index_blobs(
    root: Path,
    target: Path,
    env: dict[str, str],
) -> None:
    """将普通文件恢复为 index blob 原始字节，消除 checkout 转换。"""

    regular_entries: list[tuple[bytes, Path]] = []
    records = _git(root, "ls-files", "--stage", "-z", env=env).split(b"\0")
    for record in records:
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            raise ValueError("malformed staged index entry")
        mode, object_id, stage = fields
        if stage != b"0":
            raise ValueError("unmerged index entries cannot be materialized")
        if mode not in {b"100644", b"100755"}:
            continue
        path = Path(raw_path.decode("utf-8", errors="strict"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe index path: {path}")
        destination = target / path
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(f"regular index path was not materialized: {path}")
        regular_entries.append((object_id, destination))
    _restore_blobs_from_batch(root, regular_entries, env)


def _overlay_unstaged_source(
    root: Path,
    target: Path,
    snapshot: SourceSnapshot,
    frozen_env: dict[str, str],
) -> None:
    removed = set(snapshot.deleted_files) | set(snapshot.renamed_files.values())
    for path in sorted(removed):
        _remove_materialized_path(_selected_path(target, path))
    for path in snapshot.changed_files:
        if path in snapshot.deleted_files:
            continue
        source = _selected_path(root, path)
        destination = _selected_path(target, path)
        _copy_selected_path(source, destination)
    verify_env = {
        **os.environ,
        "GIT_INDEX_FILE": frozen_env["GIT_INDEX_FILE"],
        "GIT_WORK_TREE": str(target),
    }
    git_config_args = external_filter_overrides(root)
    diff = _git(
        root,
        *git_config_args,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
        "--submodule=short",
        "--",
        ".",
        *_runtime_pathspecs(),
        env=verify_env,
    )
    discovered = _nul_paths(
        _git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            env=verify_env,
        )
    )
    untracked = tuple(path for path in discovered if not _is_runtime_artifact(path))
    payload = diff + _snapshot_untracked_payload(target, untracked)
    if _payload_digest(payload) != snapshot.diff_hash:
        raise ValueError("unstaged source identity changed before materialization")


def _overlay_captured_source(
    target: Path,
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> None:
    removed = set(snapshot.deleted_files) | set(snapshot.renamed_files.values())
    for path in sorted(removed):
        _remove_materialized_path(_selected_path(target, path))
    for path in snapshot.changed_files:
        change = changes.get(path)
        if change is None:
            raise ValueError(f"source state is missing for changed path: {path}")
        destination = _selected_path(target, path)
        _remove_materialized_path(destination)
        state = change.after
        if not state.mode:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if state.mode in {"100644", "100755"}:
            destination.write_bytes(state.payload)
            destination.chmod(0o755 if state.mode == "100755" else 0o644)
        elif state.mode == "120000":
            if state.symlink_carrier:
                destination.write_bytes(state.payload)
                destination.chmod(0o644)
            else:
                destination.symlink_to(os.fsdecode(state.payload))
        elif state.mode == "160000":
            destination.mkdir()
        else:
            raise ValueError(f"unsupported captured source mode: {state.mode}")


def _selected_path(root: Path, path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe selected source path: {path}")
    return root / relative


def _remove_materialized_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _remove_runtime_artifacts(root: Path) -> None:
    for prefix in _AI_SDLC_RUNTIME_PREFIXES:
        _remove_materialized_path(root / prefix.rstrip("/"))
    runtime_paths = sorted(
        (
            path
            for path in root.rglob("*")
            if _is_runtime_artifact(path.relative_to(root).as_posix())
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in runtime_paths:
        _remove_materialized_path(path)


def _copy_selected_path(source: Path, destination: Path) -> None:
    _remove_materialized_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
        return
    if not source.is_file():
        raise ValueError(f"selected source path is unavailable: {source}")
    shutil.copy2(source, destination, follow_symlinks=False)


def _restore_blobs_from_batch(
    root: Path,
    entries: list[tuple[bytes, Path]],
    env: dict[str, str],
) -> None:
    if not entries:
        return
    object_ids = [item.decode("ascii", errors="strict") for item, _path in entries]
    blobs = read_git_blobs(
        root,
        sorted(set(object_ids)),
        env=env,
    )
    for object_id, (_raw_id, destination) in zip(object_ids, entries, strict=True):
        destination.write_bytes(blobs[object_id])


def _index_payload_identity(entries: bytes, flags: bytes) -> str:
    payload = (
        _filtered_index_records(entries)
        + b"\0INDEX-FLAGS\0"
        + _filtered_index_records(flags)
    )
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _payload_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _repository_path(root: Path, *args: str) -> Path:
    raw = _git(root, "rev-parse", *args).decode("utf-8", errors="strict").strip()
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@contextmanager
def _patch_index(
    root: Path, patch_path: Path, base_commit: str
) -> Iterator[dict[str, str]]:
    with tempfile.TemporaryDirectory(prefix="ai-sdlc-patch-") as directory:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
        base = base_commit or "HEAD"
        _git(root, "read-tree", base, env=env)
        try:
            _apply_patch(root, patch_path, env)
        except ValueError:
            payload = patch_path.read_bytes()
            normalized = payload.replace(b"\r\n", b"\n")
            if normalized == payload:
                raise
            # Windows 文本传输可能只改变 patch 载体换行；索引内容仍按 Git patch 语义生成。
            normalized_path = Path(directory) / "normalized.patch"
            normalized_path.write_bytes(normalized)
            _git(root, "read-tree", base, env=env)
            _apply_patch(root, normalized_path, env)
        yield env


def _apply_patch(root: Path, path: Path, env: dict[str, str]) -> None:
    _git(
        root,
        "apply",
        "--cached",
        "--binary",
        "--whitespace=nowarn",
        str(path),
        env=env,
    )


def _revision_blob(root: Path, revision: str, path: str) -> bytes:
    entry = _git(root, "ls-tree", "-z", revision, "--", path)
    return _tree_entry_blob(root, entry, path)


def _index_blob(root: Path, path: str, *, env: dict[str, str] | None = None) -> bytes:
    entry = _git(root, "ls-files", "--stage", "-z", "--", path, env=env)
    return _index_entry_blob(root, entry, path, env=env)


def _worktree_blob(root: Path, path: str) -> bytes:
    target = root / path
    if target.is_symlink():
        return os.fsencode(os.readlink(target))
    return target.read_bytes() if target.is_file() else b""


def _tree_entry_blob(root: Path, entry: bytes, path: str) -> bytes:
    if not entry:
        return b""
    metadata, separator, raw_path = entry.removesuffix(b"\0").partition(b"\t")
    fields = metadata.split()
    if not separator or len(fields) != 3 or _decode_selected_path(raw_path) != path:
        raise ValueError("git ls-tree returned an invalid selected entry")
    mode, object_type, object_id = fields
    if mode == b"160000" and object_type == b"commit":
        return object_id
    if object_type != b"blob":
        raise ValueError("selected revision entry is not a blob")
    return _git(root, "cat-file", "blob", object_id.decode("ascii"))


def _index_entry_blob(
    root: Path,
    entry: bytes,
    path: str,
    *,
    env: dict[str, str] | None,
) -> bytes:
    if not entry:
        return b""
    metadata, separator, raw_path = entry.removesuffix(b"\0").partition(b"\t")
    fields = metadata.split()
    if (
        not separator
        or len(fields) != 3
        or fields[2] != b"0"
        or _decode_selected_path(raw_path) != path
    ):
        raise ValueError("git index returned an invalid selected entry")
    mode, object_id, _stage = fields
    if not object_id.strip(b"0"):
        return b""
    if mode == b"160000":
        return object_id
    return _git(root, "cat-file", "blob", object_id.decode("ascii"), env=env)


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(env),
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise ValueError(f"git {' '.join(args)} is unavailable: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def _git_input(
    root: Path,
    args: tuple[str, ...],
    payload: bytes,
    env: dict[str, str],
) -> None:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            input=payload,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(env),
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise ValueError(f"git {' '.join(args)} is unavailable: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {message}")


def _nul_paths(payload: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="strict").replace("\\", "/")
        for item in payload.split(b"\0")
        if item
    ]


def _decode_selected_path(payload: bytes) -> str:
    return payload.decode("utf-8", errors="strict").replace("\\", "/")


__all__ = [
    "file_versions",
    "materialized_source_view",
    "patch_diff_metadata",
    "python_sources",
    "selected_git_diff",
]

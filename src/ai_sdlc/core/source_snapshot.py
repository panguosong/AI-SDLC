"""Deterministic source snapshots for local review and source comparison."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_sdlc.core.git_filter_safety import (
    external_filter_overrides,
    safe_git_read_command,
    safe_git_read_environment,
)
from ai_sdlc.core.loop_models import LoopArtifactModel
from ai_sdlc.core.source_change_capture import (
    CapturedPathChange,
    capture_index_changes,
    capture_path_changes,
    require_checked_out_gitlinks_clean,
    staged_worktree_divergent_paths,
)
from ai_sdlc.core.source_content_identity import (
    CANONICAL_DIGEST_KIND,
    CHANGE_IDENTITY_KIND,
    build_change_identities,
    canonical_content_digests,
    inspect_unsafe_attribute_paths,
)

_SOURCE_KINDS = {
    "local-git-range",
    "local-staged",
    "local-unstaged",
    "local-worktree",
    "loop-artifacts",
    "patch",
}
_AI_SDLC_RUNTIME_PREFIXES = (
    ".ai-sdlc/loops/",
    ".ai-sdlc/reviews/",
    ".ai-sdlc/work-items/",
    ".ai-sdlc/state/stage-close-authorizations/",
    ".ai-sdlc/state/stage-close-results/",
)
SOURCE_SNAPSHOT_OPTIONAL_IDENTITY_FIELDS = frozenset(
    {
        "canonical_digest_kind",
        "canonical_file_digests",
        "change_identity_kind",
        "raw_change_identities",
        "portable_change_identities",
        "safe_eol_paths",
        "source_input_digest",
    }
)


class SourceSnapshot(LoopArtifactModel):
    """Persisted identity of the exact source evaluated by a quality gate."""

    artifact_kind: str = "source-snapshot"
    source_kind: str
    base_ref: str = ""
    head_ref: str = ""
    base_commit: str = ""
    head_commit: str = ""
    diff_hash: str
    changed_files: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    binary_files: list[str] = Field(default_factory=list)
    renamed_files: dict[str, str] = Field(default_factory=dict)
    file_digests: dict[str, str] = Field(default_factory=dict)
    canonical_digest_kind: str = ""
    canonical_file_digests: dict[str, str] = Field(default_factory=dict)
    change_identity_kind: str = ""
    raw_change_identities: dict[str, str] = Field(default_factory=dict)
    portable_change_identities: dict[str, str] = Field(default_factory=dict)
    safe_eol_paths: list[str] = Field(default_factory=list)
    index_identity: str = ""
    staged_tree_oid: str = ""
    patch_file: str = ""
    source_input_digest: str = ""

    @field_validator("source_kind")
    @classmethod
    def _require_supported_source(cls, value: str) -> str:
        if value not in _SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {value}")
        return value


class SourceFreshness(BaseModel):
    """Read-only freshness result for a persisted source snapshot."""

    model_config = ConfigDict(extra="forbid")

    fresh: bool
    reason: str = ""
    current_diff_hash: str = ""


@dataclass(frozen=True)
class SourceSnapshotOptions:
    """Inputs used to build one deterministic source snapshot."""

    root: Path
    source_kind: str = "local-git-range"
    base_ref: str = ""
    head_ref: str = "HEAD"
    patch_file: str = ""


@dataclass(frozen=True)
class _SnapshotParts:
    diff_bytes: bytes
    status_bytes: bytes
    numstat_bytes: bytes
    base_ref: str
    head_ref: str
    base_commit: str
    head_commit: str
    index_identity: str = ""
    staged_tree_oid: str = ""
    patch_file: str = ""
    untracked_files: tuple[str, ...] = ()
    untracked_payload: bytes = b""
    captured_changes: dict[str, CapturedPathChange] = field(default_factory=dict)
    git_config_args: tuple[str, ...] = ()
    source_input_digest: str = ""


def build_source_snapshot(options: SourceSnapshotOptions) -> SourceSnapshot:
    """Build a source identity without invoking a model or modifying the index."""

    root = options.root.resolve()
    if options.source_kind not in _SOURCE_KINDS:
        raise ValueError(f"unsupported source_kind: {options.source_kind}")
    parts = _build_parts(root, options)
    statuses = _parse_name_status(parts.status_bytes)
    changed = sorted({item[1] for item in statuses} | set(parts.untracked_files))
    if not changed and options.source_kind != "loop-artifacts":
        raise ValueError("source snapshot contains no changed files")
    if changed and options.source_kind == "loop-artifacts":
        raise ValueError("loop artifact snapshot requires a clean source worktree")
    payload = parts.diff_bytes + parts.untracked_payload
    snapshot = SourceSnapshot(
        source_kind=options.source_kind,
        base_ref=parts.base_ref,
        head_ref=parts.head_ref,
        base_commit=parts.base_commit,
        head_commit=parts.head_commit,
        diff_hash=_digest(payload),
        changed_files=changed,
        untracked_files=list(parts.untracked_files),
        deleted_files=sorted(path for status, path, _ in statuses if status == "D"),
        binary_files=_binary_files(root, parts, statuses),
        renamed_files={path: old for status, path, old in statuses if status == "R"},
        file_digests={},
        canonical_digest_kind="",
        canonical_file_digests={},
        change_identity_kind="",
        raw_change_identities={},
        portable_change_identities={},
        safe_eol_paths=[],
        index_identity=parts.index_identity,
        staged_tree_oid=parts.staged_tree_oid,
        patch_file=parts.patch_file,
        source_input_digest=parts.source_input_digest,
    )
    completed = _complete_snapshot(root, snapshot, parts)
    if options.source_kind in {"local-staged", "local-unstaged", "local-worktree"}:
        current_parts = _build_parts(root, options)
        if _source_epoch(parts) != _source_epoch(current_parts):
            raise ValueError("source changed during snapshot capture")
        from ai_sdlc.core.source_snapshot_view import verify_captured_changes

        final_changes = capture_path_changes(
            root,
            completed,
            git_config_args=current_parts.git_config_args,
        )
        verify_captured_changes(completed, final_changes)
        if _source_epoch(current_parts) != _source_epoch(_build_parts(root, options)):
            raise ValueError("source changed during snapshot verification")
    return completed


def _source_epoch(parts: _SnapshotParts) -> tuple[object, ...]:
    return (
        parts.diff_bytes,
        parts.status_bytes,
        parts.numstat_bytes,
        parts.base_commit,
        parts.head_commit,
        parts.index_identity,
        parts.staged_tree_oid,
        parts.untracked_files,
        parts.untracked_payload,
    )


def _complete_snapshot(
    root: Path,
    snapshot: SourceSnapshot,
    parts: _SnapshotParts,
) -> SourceSnapshot:
    changes = parts.captured_changes or capture_path_changes(
        root,
        snapshot,
        git_config_args=parts.git_config_args,
    )
    unsafe_attributes = inspect_unsafe_attribute_paths(root, sorted(changes))
    file_digests, canonical_digests = _snapshot_digests(
        root,
        snapshot,
        changes,
        unsafe_attributes,
    )
    raw_identities, portable_identities, safe_eol_paths = build_change_identities(
        root,
        snapshot,
        changes,
        unsafe_attributes,
    )
    completed = snapshot.model_copy(
        update={
            "file_digests": file_digests,
            "canonical_digest_kind": CANONICAL_DIGEST_KIND,
            "canonical_file_digests": canonical_digests,
            "change_identity_kind": CHANGE_IDENTITY_KIND,
            "raw_change_identities": raw_identities,
            "portable_change_identities": portable_identities,
            "safe_eol_paths": safe_eol_paths,
        }
    )
    issue = source_snapshot_identity_issue(completed)
    if issue:
        raise ValueError(issue)
    return completed


def source_snapshot_identity_issue(snapshot: SourceSnapshot) -> str:
    """Return a fail-closed issue for a partial or unknown change identity."""
    raw = snapshot.raw_change_identities
    portable = snapshot.portable_change_identities
    safe_eol = snapshot.safe_eol_paths
    has_extension = bool(raw or portable or safe_eol)
    if not snapshot.change_identity_kind:
        return "change identity kind is missing" if has_extension else ""
    if snapshot.change_identity_kind != CHANGE_IDENTITY_KIND:
        return f"unknown change identity kind: {snapshot.change_identity_kind}"
    expected = set(snapshot.changed_files) | set(snapshot.renamed_files.values())
    if set(raw) != expected:
        return "change identity raw paths do not cover the affected source paths"
    if not set(portable).issubset(raw) or not set(safe_eol).issubset(portable):
        return "change identity portable paths are inconsistent"
    if len(safe_eol) != len(set(safe_eol)):
        return "change identity safe EOL paths contain duplicates"
    if safe_eol and snapshot.source_kind not in {"local-unstaged", "local-worktree"}:
        return "change identity safe EOL paths require a local worktree source"
    if any(
        not _valid_identity_digest(value)
        for value in (*raw.values(), *portable.values())
    ):
        return "change identity contains an invalid digest"
    return ""


def _valid_identity_digest(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def revalidate_source_snapshot(root: Path, snapshot: SourceSnapshot) -> SourceFreshness:
    """Rebuild a snapshot and compare the source identity fail-closed."""

    issue = source_snapshot_identity_issue(snapshot)
    if issue:
        return SourceFreshness(
            fresh=False,
            reason=f"source_identity_invalid:{issue}",
        )
    try:
        current = build_source_snapshot(
            SourceSnapshotOptions(
                root=root,
                source_kind=snapshot.source_kind,
                base_ref=snapshot.base_ref,
                head_ref=snapshot.head_ref,
                patch_file=snapshot.patch_file,
            )
        )
    except (OSError, ValueError) as exc:
        return SourceFreshness(fresh=False, reason=f"source_unavailable:{exc}")
    return _compare_fresh_snapshot(snapshot, current)


def _compare_fresh_snapshot(
    snapshot: SourceSnapshot,
    current: SourceSnapshot,
) -> SourceFreshness:
    if current.diff_hash != snapshot.diff_hash:
        return SourceFreshness(
            fresh=False,
            reason="diff_hash_changed",
            current_diff_hash=current.diff_hash,
        )
    if (
        current.base_commit != snapshot.base_commit
        or current.head_commit != snapshot.head_commit
    ):
        return SourceFreshness(
            fresh=False,
            reason="commit_changed",
            current_diff_hash=current.diff_hash,
        )
    if (
        snapshot.source_kind in {"local-staged", "local-unstaged", "loop-artifacts"}
        and current.index_identity != snapshot.index_identity
    ):
        return SourceFreshness(
            fresh=False,
            reason="index_identity_changed",
            current_diff_hash=current.diff_hash,
        )
    if (
        snapshot.source_kind == "local-staged"
        and current.staged_tree_oid != snapshot.staged_tree_oid
    ):
        return SourceFreshness(
            fresh=False,
            reason="staged_tree_changed",
            current_diff_hash=current.diff_hash,
        )
    if not _same_source_content(snapshot, current):
        return SourceFreshness(
            fresh=False,
            reason="source_content_changed",
            current_diff_hash=current.diff_hash,
        )
    return SourceFreshness(fresh=True, current_diff_hash=current.diff_hash)


def _same_source_content(
    expected: SourceSnapshot,
    current: SourceSnapshot,
) -> bool:
    metadata = (
        "changed_files",
        "untracked_files",
        "deleted_files",
        "binary_files",
        "renamed_files",
        "file_digests",
        "source_input_digest",
    )
    if any(getattr(expected, name) != getattr(current, name) for name in metadata):
        return False
    if (
        expected.change_identity_kind == CHANGE_IDENTITY_KIND
        and current.change_identity_kind == CHANGE_IDENTITY_KIND
    ):
        return expected.raw_change_identities == current.raw_change_identities
    return True


def _build_parts(root: Path, options: SourceSnapshotOptions) -> _SnapshotParts:
    if options.source_kind == "local-git-range":
        return _git_range_parts(root, options)
    if options.source_kind == "local-staged":
        return _worktree_parts(root, staged=True)
    if options.source_kind == "local-unstaged":
        return _worktree_parts(root, staged=False)
    if options.source_kind == "local-worktree":
        return _combined_worktree_parts(root)
    if options.source_kind == "loop-artifacts":
        return _worktree_parts(root, staged=False)
    return _patch_parts(root, options)


def _git_range_parts(root: Path, options: SourceSnapshotOptions) -> _SnapshotParts:
    if not options.base_ref.strip():
        raise ValueError("base_ref is required for local-git-range")
    head_ref = options.head_ref or "HEAD"
    base_commit = _git_text(root, "merge-base", options.base_ref, head_ref)
    head_commit = _git_text(root, "rev-parse", head_ref)
    from ai_sdlc.core.source_snapshot_view import selected_git_diff

    diff, status, numstat = selected_git_diff(
        root,
        "local-git-range",
        base_commit=base_commit,
        head_commit=head_commit,
    )
    return _SnapshotParts(
        diff_bytes=diff,
        status_bytes=status,
        numstat_bytes=numstat,
        base_ref=options.base_ref,
        head_ref=head_ref,
        base_commit=base_commit,
        head_commit=head_commit,
    )


def _worktree_parts(root: Path, *, staged: bool) -> _SnapshotParts:
    head = _git_text(root, "rev-parse", "HEAD")
    git_config_args: tuple[str, ...] = ()
    if staged:
        from ai_sdlc.core.source_snapshot_view import selected_git_diff

        diff, status, numstat = selected_git_diff(
            root,
            "local-staged",
            base_commit=head,
        )
    else:
        git_config_args = external_filter_overrides(root)
        require_checked_out_gitlinks_clean(root)
        diff, status, numstat = _unstaged_diff_outputs(root, git_config_args)
    discovered = (
        ()
        if staged
        else tuple(
            _nul_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
        )
    )
    untracked = tuple(path for path in discovered if not _is_runtime_artifact(path))
    return _SnapshotParts(
        diff_bytes=diff,
        status_bytes=status,
        numstat_bytes=numstat,
        base_ref="HEAD" if staged else "INDEX",
        head_ref="INDEX" if staged else "WORKTREE",
        base_commit=head,
        head_commit=head,
        index_identity=_index_identity(root),
        staged_tree_oid=_staged_tree_oid(root) if staged else "",
        untracked_files=untracked,
        untracked_payload=_untracked_payload(root, untracked),
        git_config_args=git_config_args,
    )


def _combined_worktree_parts(root: Path) -> _SnapshotParts:
    head = _git_text(root, "rev-parse", "HEAD")
    git_config_args = external_filter_overrides(root)
    index_before = _index_identity(root)
    _require_combined_worktree_index_safe(root, git_config_args, head)
    prefix = (*git_config_args, "diff")
    paths = ("--", ".", *_runtime_pathspecs())
    diff = _git(
        root,
        *prefix,
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
        "--submodule=short",
        head,
        *paths,
    )
    status = _git(
        root,
        *prefix,
        "--name-status",
        "-z",
        "-M",
        "--ignore-submodules=dirty",
        "--submodule=short",
        head,
        *paths,
    )
    numstat = _git(
        root,
        *prefix,
        "--numstat",
        "-z",
        "-M",
        "--ignore-submodules=dirty",
        "--submodule=short",
        head,
        *paths,
    )
    discovered = tuple(
        _nul_paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    )
    untracked = tuple(path for path in discovered if not _is_runtime_artifact(path))
    index_after = _index_identity(root)
    _require_combined_worktree_index_safe(root, git_config_args, head)
    index_final = _index_identity(root)
    if index_after != index_before or index_final != index_after:
        raise ValueError("stage candidate index changed during source capture")
    return _SnapshotParts(
        diff_bytes=diff,
        status_bytes=status,
        numstat_bytes=numstat,
        base_ref="HEAD",
        head_ref="WORKTREE",
        base_commit=head,
        head_commit=head,
        index_identity=index_final,
        untracked_files=untracked,
        untracked_payload=_untracked_payload(root, untracked),
        git_config_args=git_config_args,
    )


def _require_combined_worktree_index_safe(
    root: Path,
    git_config_args: tuple[str, ...],
    base_commit: str,
) -> None:
    require_checked_out_gitlinks_clean(root)
    hidden = tuple(
        path for path in _hidden_index_paths(root) if not _is_runtime_artifact(path)
    )
    if hidden:
        raise ValueError(
            "stage candidate cannot inspect hidden index paths: " + ", ".join(hidden)
        )
    divergent = tuple(
        path
        for path in staged_worktree_divergent_paths(
            root,
            git_config_args,
            base_commit,
        )
        if not _is_runtime_artifact(path)
    )
    if divergent:
        raise ValueError(
            "stage candidate cannot safely combine divergent staged/worktree paths: "
            + ", ".join(divergent)
        )


def _hidden_index_paths(root: Path) -> tuple[str, ...]:
    records = _git(root, "ls-files", "-v", "-z").split(b"\0")
    hidden: list[str] = []
    for record in records:
        if not record:
            continue
        tag, separator, raw_path = record.partition(b" ")
        if not separator or len(tag) != 1:
            raise ValueError("git index flags are malformed")
        if tag == b"S" or tag.islower():
            hidden.append(_decode_path(raw_path))
    return tuple(sorted(hidden))


def _unstaged_diff_outputs(
    root: Path,
    git_config_args: tuple[str, ...],
) -> tuple[bytes, bytes, bytes]:
    prefix = (*git_config_args, "diff")
    paths = ("--", ".", *_runtime_pathspecs())
    diff = _git(
        root,
        *prefix,
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
        "--submodule=short",
        *paths,
    )
    status = _git(
        root,
        *prefix,
        "--name-status",
        "-z",
        "-M",
        "--ignore-submodules=dirty",
        "--submodule=short",
        *paths,
    )
    numstat = _git(
        root,
        *prefix,
        "--numstat",
        "-z",
        "-M",
        "--ignore-submodules=dirty",
        "--submodule=short",
        *paths,
    )
    return diff, status, numstat


def _patch_parts(root: Path, options: SourceSnapshotOptions) -> _SnapshotParts:
    patch_file = options.patch_file
    if not patch_file.strip():
        raise ValueError("patch_file is required for patch source")
    path = _patch_path(root, patch_file)
    if not path.is_file():
        raise ValueError(f"patch_file not found: {patch_file}")
    patch = path.read_bytes()
    head_ref = options.head_ref.strip() or "HEAD"
    head = _git_text(root, "rev-parse", head_ref)
    from ai_sdlc.core.source_snapshot_view import (
        _diff_outputs,
        _index_worktree,
        _patch_index,
    )

    with _patch_index(root, path, head) as index_env:
        with _index_worktree(root, index_env) as selected_env:
            filtered_diff, status, numstat = _diff_outputs(
                ("--cached", head), selected_env
            )
        statuses = _parse_name_status(status)
        paths = sorted(
            {item[1] for item in statuses} | {item[2] for item in statuses if item[2]}
        )
        changes = capture_index_changes(root, head, paths, index_env)

    return _SnapshotParts(
        diff_bytes=filtered_diff,
        status_bytes=status,
        numstat_bytes=numstat,
        base_ref="patch-file",
        head_ref=head_ref,
        base_commit=head,
        head_commit=head,
        patch_file=_persisted_patch_path(root, path),
        captured_changes=changes,
        source_input_digest=_digest(patch),
    )


def _patch_path(root: Path, patch_file: str) -> Path:
    path = Path(patch_file)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _persisted_patch_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _index_identity(root: Path) -> str:
    entries = _filtered_index_records(_git(root, "ls-files", "-s", "-z"))
    flags = _filtered_index_records(_git(root, "ls-files", "-v", "-z"))
    return _digest(entries + b"\0INDEX-FLAGS\0" + flags)


def _staged_tree_oid(root: Path) -> str:
    """从 index entries 构建精确 tree，且不执行仓库内容过滤器。"""

    records = _git(root, "ls-files", "-s", "-z")
    index_info = bytearray()
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3 or fields[2] != b"0":
            raise ValueError("git index contains unmerged or malformed entries")
        mode, oid, _stage = fields
        index_info.extend(mode + b" " + oid + b"\t" + path + b"\0")

    with tempfile.TemporaryDirectory(prefix="ai-sdlc-index-tree-") as temp_dir:
        index_path = Path(temp_dir) / "index"
        environment = safe_git_read_environment()
        environment["GIT_INDEX_FILE"] = str(index_path)
        _git_with_input(
            root,
            "update-index",
            "-z",
            "--index-info",
            input_bytes=bytes(index_info),
            env=environment,
        )
        return (
            _git_with_input(
                root,
                "write-tree",
                input_bytes=b"",
                env=environment,
            )
            .decode("ascii", errors="strict")
            .strip()
        )


def _git_with_input(
    root: Path,
    *args: str,
    input_bytes: bytes,
    env: dict[str, str],
) -> bytes:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            input=input_bytes,
            capture_output=True,
            check=False,
            env=env,
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


def _filtered_index_records(payload: bytes) -> bytes:
    selected = bytearray()
    for record in payload.split(b"\0"):
        if not record:
            continue
        _metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raw_path = record[2:]
        if _is_runtime_artifact(_decode_path(raw_path)):
            continue
        selected.extend(record + b"\0")
    return bytes(selected)


def _parse_name_status(payload: bytes) -> list[tuple[str, str, str]]:
    fields = [item for item in payload.split(b"\0") if item]
    parsed: list[tuple[str, str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", errors="strict")
        index += 1
        if status.startswith(("R", "C")):
            old = _decode_path(fields[index])
            path = _decode_path(fields[index + 1])
            parsed.append(("R", path, old))
            index += 2
        else:
            parsed.append((status[:1], _decode_path(fields[index]), ""))
            index += 1
    return parsed


def _binary_files(
    root: Path,
    parts: _SnapshotParts,
    statuses: list[tuple[str, str, str]],
) -> list[str]:
    changed_paths = {path for _status, path, _old in statuses}
    tracked_paths = changed_paths - set(parts.untracked_files)
    numstat = _parse_numstat(parts.numstat_bytes)
    if set(numstat) != tracked_paths:
        raise ValueError("numstat paths do not match source status")
    binary = {path for path, is_binary in numstat.items() if is_binary}
    for path in parts.untracked_files:
        target = root / path
        if (
            not target.is_symlink()
            and target.is_file()
            and b"\0" in target.read_bytes()[:8192]
        ):
            binary.add(path)
    return sorted(binary)


def _parse_binary_numstat(payload: bytes) -> list[str]:
    return [path for path, is_binary in _parse_numstat(payload).items() if is_binary]


def _parse_numstat(payload: bytes) -> dict[str, bool]:
    if not payload:
        return {}
    if not payload.endswith(b"\0"):
        raise ValueError("git numstat is not NUL terminated")
    fields = payload.split(b"\0")
    fields.pop()
    parsed: dict[str, bool] = {}
    index = 0
    while index < len(fields):
        if not fields[index]:
            raise ValueError("git numstat contains an empty record")
        record = fields[index].split(b"\t", 2)
        index += 1
        if len(record) != 3:
            raise ValueError("malformed git numstat record")
        added, deleted, encoded_path = record
        binary = added == b"-" and deleted == b"-"
        if not binary and (not added.isdigit() or not deleted.isdigit()):
            raise ValueError("git numstat counts are invalid")
        if encoded_path:
            path = _decode_path(encoded_path)
        else:
            if index + 1 >= len(fields):
                raise ValueError("malformed git rename numstat record")
            if not fields[index] or not fields[index + 1]:
                raise ValueError("git rename numstat path is empty")
            path = _decode_path(fields[index + 1])
            index += 2
        if path in parsed:
            raise ValueError("git numstat contains a duplicate path")
        parsed[path] = binary
    return parsed


def _snapshot_digests(
    root: Path,
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
    unsafe_attribute_paths: set[str],
) -> tuple[dict[str, str], dict[str, str]]:
    after_states = {
        path: change.after for path, change in changes.items() if change.after.mode
    }
    digests = {path: _digest(state.payload) for path, state in after_states.items()}
    payloads = {
        path: state.payload
        for path, state in after_states.items()
        if state.mode != "160000"
    }
    symlink_paths = {
        path for path, state in after_states.items() if state.mode == "120000"
    }
    canonical = canonical_content_digests(
        root,
        snapshot.source_kind,
        payloads,
        symlink_paths,
        unsafe_attribute_paths,
    )
    return digests, canonical


def _untracked_payload(root: Path, paths: tuple[str, ...]) -> bytes:
    payload = bytearray()
    for path in sorted(paths):
        raw_path = path.encode("utf-8")
        payload.extend(b"\0UNTRACKED\0" + raw_path + b"\0")
        target = root / path
        if target.is_symlink():
            payload.extend(b"SYMLINK\0")
            payload.extend(hashlib.sha256(os.fsencode(os.readlink(target))).digest())
            continue
        if not target.is_file():
            raise ValueError(f"untracked source path is not a file: {path}")
        executable = (
            b"1" if target.stat(follow_symlinks=False).st_mode & 0o111 else b"0"
        )
        payload.extend(b"FILE\0" + executable + b"\0")
        payload.extend(hashlib.sha256(target.read_bytes()).digest())
    return bytes(payload)


def _nul_paths(payload: bytes) -> list[str]:
    return [_decode_path(item) for item in payload.split(b"\0") if item]


def _is_runtime_artifact(path: str) -> bool:
    normalized = path.replace("\\", "/")
    segments = normalized.split("/")
    return (
        normalized.startswith(_AI_SDLC_RUNTIME_PREFIXES)
        or "__pycache__" in segments
        or bool(set(segments) & {".pytest_cache", ".ruff_cache", ".mypy_cache"})
        or normalized in {".coverage"}
        or segments[0] in {"htmlcov", "build", "dist"}
        or normalized.endswith((".pyc", ".pyo"))
    )


def is_runtime_artifact_path(path: str) -> bool:
    """公开候选源视图的运行时排除判定，供适配器复用同一边界。"""

    return _is_runtime_artifact(path)


def _runtime_pathspecs() -> tuple[str, ...]:
    managed = tuple(f":(exclude,glob){path}**" for path in _AI_SDLC_RUNTIME_PREFIXES)
    generated = (
        ":(exclude,glob)**/__pycache__/**",
        ":(exclude,glob)**/.pytest_cache/**",
        ":(exclude,glob)**/.ruff_cache/**",
        ":(exclude,glob)**/.mypy_cache/**",
        ":(exclude,glob)htmlcov/**",
        ":(exclude,glob)build/**",
        ":(exclude,glob)dist/**",
        ":(exclude).coverage",
        ":(exclude,glob)**/*.pyc",
        ":(exclude,glob)**/*.pyo",
    )
    return (*managed, *generated)


def _decode_path(payload: bytes) -> str:
    return payload.decode("utf-8", errors="strict").replace("\\", "/")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _git_text(root: Path, *args: str) -> str:
    return _git(root, *args).decode("utf-8", errors="strict").strip()


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(),
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


__all__ = [
    "SOURCE_SNAPSHOT_OPTIONAL_IDENTITY_FIELDS",
    "SourceFreshness",
    "SourceSnapshot",
    "SourceSnapshotOptions",
    "build_source_snapshot",
    "revalidate_source_snapshot",
    "source_snapshot_identity_issue",
]

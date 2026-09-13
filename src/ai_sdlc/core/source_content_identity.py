"""Raw and narrowly portable identities for source-transition verification."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ai_sdlc.core.git_filter_safety import (
    safe_git_read_command,
    safe_git_read_environment,
)
from ai_sdlc.core.source_change_capture import CapturedPathChange, PathState

if TYPE_CHECKING:
    from ai_sdlc.core.source_snapshot import SourceSnapshot

CANONICAL_DIGEST_KIND = "git-blob-v1"
CHANGE_IDENTITY_KIND = "source-change-v1"
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_REGULAR_MODES = {"", "100644", "100755"}
_FILE_MODES = {"100644", "100755"}
_GIT_TIMEOUT_SECONDS = 10


def build_change_identities(
    root: Path,
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
    unsafe_attribute_paths: set[str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Describe normalized per-path before/after states and safe EOL variants."""
    raw = build_raw_change_identities(snapshot, changes)
    portable: dict[str, str] = {}
    safe_eol: list[str] = []
    for path, change in changes.items():
        if _portable_candidate(change):
            portable[path] = _change_identity(
                snapshot.base_commit,
                path,
                _normalized_change(change),
            )
        if _safe_local_eol_path(
            root,
            snapshot,
            path,
            change.after,
            unsafe_attribute_paths,
        ):
            safe_eol.append(path)
    return raw, portable, safe_eol


def build_raw_change_identities(
    snapshot: SourceSnapshot,
    changes: dict[str, CapturedPathChange],
) -> dict[str, str]:
    """Build raw source-transition identities without attribute conversion."""
    return {
        path: _change_identity(snapshot.base_commit, path, change)
        for path, change in changes.items()
    }


def canonical_content_digests(
    root: Path,
    source_kind: str,
    payloads: dict[str, bytes],
    symlink_paths: set[str],
    unsafe_attribute_paths: set[str],
) -> dict[str, str]:
    """Hash selected after-views as Git would retain them for legacy readers."""
    object_format = _object_format(root)
    result: dict[str, str] = {}
    for path, payload in payloads.items():
        apply_filters = (
            source_kind in {"local-unstaged", "local-worktree"}
            and path not in symlink_paths
            and path not in unsafe_attribute_paths
            and b"\r\n" in payload
        )
        result[path] = _git_blob_identity(
            root,
            path,
            payload,
            apply_filters=apply_filters,
            object_format=object_format,
        )
    return result


def inspect_unsafe_attribute_paths(root: Path, paths: list[str]) -> set[str]:
    """Inspect transform-capable attributes for all paths in one Git call."""
    if not paths:
        return set()
    request = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    payload = _git_with_input(
        root,
        (
            "check-attr",
            "-z",
            "--stdin",
            "filter",
            "working-tree-encoding",
            "ident",
            "text",
        ),
        request,
    )
    fields = payload.split(b"\0")
    if fields and not fields[-1]:
        fields.pop()
    if len(fields) % 3:
        raise ValueError("git check-attr returned malformed output")
    unsafe: set[str] = set()
    for index in range(0, len(fields), 3):
        path = fields[index].decode("utf-8", errors="strict").replace("\\", "/")
        name = fields[index + 1].decode("utf-8", errors="strict")
        value = fields[index + 2].decode("utf-8", errors="strict")
        if (
            name in {"filter", "working-tree-encoding", "ident"}
            and value != "unspecified"
        ) or (name == "text" and value == "unset"):
            unsafe.add(path)
    return unsafe


def source_content_equivalent(
    evaluated: SourceSnapshot,
    current: SourceSnapshot,
) -> bool:
    """Return whether two snapshots prove the same source change."""

    return compare_source_content(evaluated, current)[0]


def compare_source_content(
    evaluated: SourceSnapshot,
    current: SourceSnapshot,
) -> tuple[bool, str]:
    """Compare source content and distinguish mismatch from unverifiable input."""

    from ai_sdlc.core.source_snapshot import source_snapshot_identity_issue

    identity_issue = source_snapshot_identity_issue(
        evaluated
    ) or source_snapshot_identity_issue(current)
    if identity_issue:
        return False, f"Source identity is unverifiable: {identity_issue}."
    if evaluated.source_kind == current.source_kind:
        return _same_source_equivalent(evaluated, current), ""
    if evaluated.base_commit != current.base_commit:
        return False, ""
    if (
        evaluated.change_identity_kind == CHANGE_IDENTITY_KIND
        and current.change_identity_kind == CHANGE_IDENTITY_KIND
    ):
        return _change_identities_match(evaluated, current), ""
    if (
        evaluated.diff_hash == current.diff_hash
        and evaluated.file_digests == current.file_digests
    ):
        return True, ""
    return False, "Legacy source content identity cannot be verified."


def _same_source_equivalent(
    evaluated: SourceSnapshot,
    current: SourceSnapshot,
) -> bool:
    fields = (
        "base_commit",
        "head_commit",
        "diff_hash",
        "changed_files",
        "untracked_files",
        "deleted_files",
        "binary_files",
        "renamed_files",
        "file_digests",
        "index_identity",
    )
    return all(getattr(evaluated, name) == getattr(current, name) for name in fields)


def _change_identities_match(
    evaluated: SourceSnapshot,
    current: SourceSnapshot,
) -> bool:
    if set(evaluated.raw_change_identities) != set(current.raw_change_identities):
        return False
    safe_eol = {
        path
        for snapshot in (evaluated, current)
        if snapshot.source_kind == "local-unstaged"
        for path in snapshot.safe_eol_paths
    }
    for path, evaluated_raw in evaluated.raw_change_identities.items():
        if evaluated_raw == current.raw_change_identities[path]:
            continue
        if path not in safe_eol or (
            evaluated.portable_change_identities.get(path)
            != current.portable_change_identities.get(path)
        ):
            return False
    return True


def _change_identity(
    base_commit: str,
    path: str,
    change: CapturedPathChange,
) -> str:
    payload = {
        "base_commit": base_commit,
        "path": path,
        "before_mode": change.before.mode or "absent",
        "before_digest": _state_digest(change.before),
        "after_mode": change.after.mode or "absent",
        "after_digest": _state_digest(change.after),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _state_digest(state: PathState) -> str:
    if not state.mode:
        return "absent"
    return f"sha256:{hashlib.sha256(state.payload).hexdigest()}"


def _portable_candidate(change: CapturedPathChange) -> bool:
    return (
        change.before.mode in _REGULAR_MODES
        and change.after.mode in _REGULAR_MODES
        and b"\0" not in change.before.payload
        and b"\0" not in change.after.payload
    )


def _normalized_change(change: CapturedPathChange) -> CapturedPathChange:
    return CapturedPathChange(
        before=PathState(
            mode=change.before.mode,
            payload=_normalize_eol(change.before.payload),
        ),
        after=PathState(
            mode=change.after.mode,
            payload=_normalize_eol(change.after.payload),
        ),
    )


def _safe_local_eol_path(
    root: Path,
    snapshot: SourceSnapshot,
    path: str,
    after: PathState,
    unsafe_attribute_paths: set[str],
) -> bool:
    if (
        snapshot.source_kind not in {"local-unstaged", "local-worktree"}
        or after.mode not in _FILE_MODES
    ):
        return False
    normalized = _normalize_eol(after.payload)
    if (
        normalized == after.payload
        or b"\0" in after.payload
        or path in unsafe_attribute_paths
    ):
        return False
    object_format = _object_format(root)
    return _git_blob_identity(
        root,
        path,
        after.payload,
        apply_filters=True,
        object_format=object_format,
    ) == _git_blob_identity(
        root,
        path,
        normalized,
        apply_filters=False,
        object_format=object_format,
    )


def _normalize_eol(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n")


def _git_blob_identity(
    root: Path,
    path: str,
    payload: bytes,
    *,
    apply_filters: bool,
    object_format: str,
) -> str:
    if not apply_filters:
        header = f"blob {len(payload)}\0".encode("ascii")
        algorithm = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
        return f"git-blob:{algorithm(header + payload).hexdigest()}"
    command = ["hash-object"]
    if apply_filters:
        command.append(f"--path={path}")
    command.append("--stdin")
    try:
        result = subprocess.run(
            safe_git_read_command(*command),
            cwd=root,
            input=payload,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git content identity timed out.") from exc
    except OSError as exc:
        raise ValueError(f"Git content identity is unavailable: {exc}") from exc
    object_id = result.stdout.decode("ascii", errors="strict").strip()
    if result.returncode or not _GIT_OBJECT_ID.fullmatch(object_id):
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            "Git content identity failed" + (f": {message}" if message else ".")
        )
    return f"git-blob:{object_id}"


def _object_format(root: Path) -> str:
    value = (
        _git(root, "rev-parse", "--show-object-format")
        .decode("ascii", errors="strict")
        .strip()
    )
    if value not in {"sha1", "sha256"}:
        raise ValueError(f"unsupported Git object format: {value}")
    return value


def _git_with_input(root: Path, args: tuple[str, ...], payload: bytes) -> bytes:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            input=payload,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {' '.join(args)} timed out") from exc
    except OSError as exc:
        raise ValueError(f"git {' '.join(args)} is unavailable: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {message}")
    return result.stdout


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            safe_git_read_command(*args),
            cwd=root,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
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
    "CANONICAL_DIGEST_KIND",
    "CHANGE_IDENTITY_KIND",
    "build_change_identities",
    "build_raw_change_identities",
    "canonical_content_digests",
    "compare_source_content",
    "inspect_unsafe_attribute_paths",
    "source_content_equivalent",
]

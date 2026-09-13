"""执行单个质量命令，并将结果绑定到本地源码真值。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_REPOSITORY_REDIRECTION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_REPLACE_REF_BASE",
    }
)
_RUNTIME_PREFIXES = (
    ".ai-sdlc/loops/",
    ".ai-sdlc/reviews/",
    ".ai-sdlc/work-items/",
    ".ai-sdlc/state/",
)
_TAIL_LIMIT = 8192
_GIT_TIMEOUT_SECONDS = 30


class QualityCommandResult(BaseModel):
    """绑定命令执行前后源码状态的可执行质量结果。"""

    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: str
    exit_code: int | None = None
    started_at: str
    completed_at: str
    source_digest_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_digest_after: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stdout_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stdout_tail: str = ""
    stderr_tail: str = ""
    status: Literal["passed", "failed", "timed_out", "source_changed"]
    timed_out: bool = False

    @field_validator("cwd", "started_at", "completed_at")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("quality command text is required")
        return normalized

    @property
    def successful(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True)
class QualityCommandOptions:
    """直接执行一次质量命令所需的输入。"""

    root: Path
    cwd: Path
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    output_tail_bytes: int = _TAIL_LIMIT


def run_quality_command(options: QualityCommandOptions) -> QualityCommandResult:
    """不经 shell 执行 argv，并拒绝源码发生变化时产生的结果。"""

    root = options.root.resolve(strict=True)
    cwd = options.cwd.resolve(strict=True)
    _require_within_root(root, cwd)
    argv = _validate_argv(options.argv)
    if options.timeout_seconds <= 0:
        raise ValueError("quality command timeout must be positive")
    if options.output_tail_bytes < 0 or options.output_tail_bytes > 1024 * 1024:
        raise ValueError("quality command output tail limit is invalid")

    environment = quality_command_environment(os.environ)
    before = build_source_digest(root, env=environment)
    started_at = _utc_now()
    exit_code: int | None = None
    timed_out = False
    launch_error = ""
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                check=False,
                timeout=options.timeout_seconds,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            launch_error = str(exc)
        stdout_digest, stdout_tail = _digest_and_tail(
            stdout_file,
            options.output_tail_bytes,
        )
        stderr_digest, stderr_tail = _digest_and_tail(
            stderr_file,
            options.output_tail_bytes,
        )
    if launch_error:
        stderr_bytes = launch_error.encode("utf-8", errors="replace")
        stderr_digest = _sha256_label(stderr_bytes)
        stderr_tail = _decode_tail(stderr_bytes, options.output_tail_bytes)

    after = build_source_digest(root, env=environment)
    if before != after:
        status = "source_changed"
    elif timed_out:
        status = "timed_out"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    return QualityCommandResult(
        argv=list(argv),
        cwd=cwd.relative_to(root).as_posix() or ".",
        exit_code=exit_code,
        started_at=started_at,
        completed_at=_utc_now(),
        source_digest_before=before,
        source_digest_after=after,
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        status=status,
        timed_out=timed_out,
    )


def build_source_digest(
    root: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """返回覆盖 HEAD、index、已跟踪差异和未跟踪字节的稳定摘要。"""

    resolved = root.resolve(strict=True)
    environment = quality_command_environment(os.environ if env is None else env)
    first = _source_identity_payload(resolved, environment)
    second = _source_identity_payload(resolved, environment)
    if first != second:
        raise ValueError("source changed during quality identity capture")
    encoded = json.dumps(
        first,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_label(encoded)


def quality_command_environment(source: Mapping[str, str]) -> dict[str, str]:
    """保留调用方环境，仅移除会重定向仓库身份的变量。"""

    environment = dict(source)
    for name in _REPOSITORY_REDIRECTION_ENV:
        environment.pop(name, None)
    return environment


def _source_identity_payload(root: Path, env: Mapping[str, str]) -> dict[str, object]:
    top = Path(_git_text(root, env, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise ValueError("quality command root is not the repository top level")
    head = _git_text(root, env, "rev-parse", "--verify", "HEAD")
    index_tree = _git_text(root, env, "write-tree")
    filter_args = _git_filter_overrides(root, env)
    tracked_diff = _git_bytes(
        root,
        env,
        *filter_args,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "HEAD",
        "--",
        ".",
        *(_exclude_pathspec(prefix) for prefix in _RUNTIME_PREFIXES),
    )
    untracked = _git_bytes(
        root,
        env,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths = [
        path
        for path in untracked.decode("utf-8", errors="strict").split("\0")
        if path and not _is_runtime_path(path)
    ]
    return {
        "head": head,
        "index_tree": index_tree,
        "tracked_diff": _sha256_label(tracked_diff),
        "untracked": [_untracked_identity(root, path) for path in sorted(paths)],
    }


def _git_filter_overrides(root: Path, env: Mapping[str, str]) -> tuple[str, ...]:
    result = _git_bytes(
        root,
        env,
        "config",
        "--name-only",
        "--get-regexp",
        r"^filter\..*\.(clean|process|required)$",
        allowed_returncodes={0, 1},
    )
    drivers: set[str] = set()
    for key in result.decode("utf-8", errors="strict").splitlines():
        parts = key.strip().split(".")
        if len(parts) >= 3 and parts[0] == "filter":
            drivers.add(".".join(parts[1:-1]))
    arguments: list[str] = []
    for driver in sorted(drivers):
        arguments.extend(
            [
                "-c",
                f"filter.{driver}.clean=",
                "-c",
                f"filter.{driver}.process=",
                "-c",
                f"filter.{driver}.required=false",
            ]
        )
    return tuple(arguments)


def _git_text(root: Path, env: Mapping[str, str], *args: str) -> str:
    return _git_bytes(root, env, *args).decode("utf-8", errors="strict").strip()


def _git_bytes(
    root: Path,
    env: Mapping[str, str],
    *args: str,
    allowed_returncodes: set[int] | None = None,
) -> bytes:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        *args,
    ]
    selected_env = dict(env)
    selected_env["GIT_OPTIONAL_LOCKS"] = "0"
    selected_env.pop("GIT_EXTERNAL_DIFF", None)
    selected_env.pop("GIT_DIFF_OPTS", None)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=selected_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"quality source Git command is unavailable: {exc}") from exc
    accepted = {0} if allowed_returncodes is None else allowed_returncodes
    if result.returncode not in accepted:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"quality source Git command failed: {message}")
    return result.stdout


def _untracked_identity(root: Path, relative: str) -> dict[str, object]:
    candidate = root / relative
    resolved_parent = candidate.parent.resolve(strict=True)
    _require_within_root(root, resolved_parent)
    before = candidate.lstat()
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode):
        content = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
        kind = "symlink"
    elif stat.S_ISREG(before.st_mode):
        content = candidate.read_bytes()
        kind = "file"
    else:
        raise ValueError(f"untracked source path is not a file: {relative}")
    after = candidate.lstat()
    if (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError(f"untracked source changed during capture: {relative}")
    return {
        "path": relative,
        "kind": kind,
        "mode": mode,
        "size": len(content),
        "digest": _sha256_label(content),
    }


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(argv)
    if not normalized or any(not item or "\0" in item for item in normalized):
        raise ValueError("quality command argv must contain non-empty arguments")
    return normalized


def _require_within_root(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("quality command cwd escapes the project") from exc


def _digest_and_tail(stream, limit: int) -> tuple[str, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    tail = bytearray()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        if limit:
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
    return f"sha256:{digest.hexdigest()}", bytes(tail).decode("utf-8", errors="replace")


def _decode_tail(content: bytes, limit: int) -> str:
    return content[-limit:].decode("utf-8", errors="replace") if limit else ""


def _sha256_label(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _exclude_pathspec(prefix: str) -> str:
    return f":(exclude){prefix}**"


def _is_runtime_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _RUNTIME_PREFIXES)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "QualityCommandOptions",
    "QualityCommandResult",
    "build_source_digest",
    "quality_command_environment",
    "run_quality_command",
]

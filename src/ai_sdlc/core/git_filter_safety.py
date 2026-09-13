"""Disable repository-defined content filters during read-only source capture."""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

_FILTER_KEY = re.compile(r"^filter\.(.+)\.(?:clean|process|required)$")
_GIT_TIMEOUT_SECONDS = 30
def safe_git_read_command(*args: str) -> list[str]:
    """Build a Git read command that cannot invoke repository-owned helpers."""

    return [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        f"core.hooksPath={_empty_hooks_path()}",
        *args,
    ]


def safe_git_read_environment(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Disable optional index writes and environment-provided external diff code."""

    selected = dict(os.environ if env is None else env)
    selected["GIT_OPTIONAL_LOCKS"] = "0"
    selected.pop("GIT_EXTERNAL_DIFF", None)
    selected.pop("GIT_DIFF_OPTS", None)
    return selected


@lru_cache(maxsize=1)
def _empty_hooks_path() -> str:
    path = Path(tempfile.mkdtemp(prefix="ai-sdlc-empty-git-hooks-"))
    path.chmod(0o700)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path.as_posix()


def external_filter_overrides(root: Path) -> tuple[str, ...]:
    """Return Git config arguments that prevent project filter execution."""
    try:
        result = subprocess.run(
            safe_git_read_command(
                "config",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|process|required)$",
            ),
            cwd=root,
            capture_output=True,
            check=False,
            env=safe_git_read_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git filter discovery timed out") from exc
    except OSError as exc:
        raise ValueError(f"git filter discovery is unavailable: {exc}") from exc
    if result.returncode not in {0, 1}:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git filter discovery failed: {message}")
    drivers: set[str] = set()
    for raw_key in result.stdout.decode("utf-8", errors="strict").splitlines():
        match = _FILTER_KEY.fullmatch(raw_key.strip())
        if match:
            drivers.add(match.group(1))
    arguments: list[str] = []
    for driver in sorted(drivers):
        arguments.extend(
            (
                "-c",
                f"filter.{driver}.clean=",
                "-c",
                f"filter.{driver}.process=",
                "-c",
                f"filter.{driver}.required=false",
            )
        )
    return tuple(arguments)


__all__ = [
    "external_filter_overrides",
    "safe_git_read_command",
    "safe_git_read_environment",
]

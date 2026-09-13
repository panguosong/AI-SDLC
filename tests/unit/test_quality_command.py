"""Tests for executable quality evidence bound to source truth."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_sdlc.core.quality_command import (
    QualityCommandOptions,
    build_source_digest,
    quality_command_environment,
    run_quality_command,
)

_REDIRECTION_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_REPLACE_REF_BASE",
}


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repository: Path, *argv: str, timeout: float = 5) -> object:
    return run_quality_command(
        QualityCommandOptions(
            root=repository,
            cwd=repository,
            argv=tuple(argv),
            timeout_seconds=timeout,
        )
    )


def test_quality_command_executes_direct_argv_and_records_clean_identity(
    repository: Path,
) -> None:
    result = _run(repository, sys.executable, "-c", "print('ok')")

    assert result.successful is True
    assert result.exit_code == 0
    assert result.stdout_tail == f"ok{os.linesep}"
    assert result.source_digest_before == result.source_digest_after
    assert result.source_digest_before == build_source_digest(repository)


def test_quality_command_nonzero_exit_fails(repository: Path) -> None:
    result = _run(repository, sys.executable, "-c", "raise SystemExit(7)")

    assert result.status == "failed"
    assert result.exit_code == 7


def test_quality_command_timeout_fails(repository: Path) -> None:
    result = _run(
        repository,
        sys.executable,
        "-c",
        "import time; time.sleep(2)",
        timeout=0.05,
    )

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.timed_out is True


def test_quality_command_rejects_cwd_escape(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent

    with pytest.raises(ValueError, match="cwd escapes"):
        run_quality_command(
            QualityCommandOptions(
                root=repository,
                cwd=outside,
                argv=(sys.executable, "-c", "pass"),
            )
        )


def test_quality_command_rejects_source_mutation(repository: Path) -> None:
    result = _run(
        repository,
        sys.executable,
        "-c",
        "from pathlib import Path; Path('tracked.txt').write_text('changed\\n')",
    )

    assert result.status == "source_changed"
    assert result.source_digest_before != result.source_digest_after


def test_source_digest_binds_untracked_content(repository: Path) -> None:
    untracked = repository / "new.txt"
    untracked.write_text("one\n", encoding="utf-8")
    first = build_source_digest(repository)
    untracked.write_text("two\n", encoding="utf-8")

    assert build_source_digest(repository) != first


def test_quality_command_output_tail_is_bounded(repository: Path) -> None:
    result = run_quality_command(
        QualityCommandOptions(
            root=repository,
            cwd=repository,
            argv=(sys.executable, "-c", "print('x' * 10000)"),
            output_tail_bytes=128,
        )
    )

    assert result.status == "passed"
    assert len(result.stdout_tail.encode("utf-8")) <= 128


def test_quality_command_does_not_interpret_shell_metacharacters(
    repository: Path,
) -> None:
    marker = repository / "must-not-exist"
    result = _run(
        repository,
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        f"; touch {marker}",
    )

    assert result.status == "passed"
    assert not marker.exists()
    assert result.stdout_tail.strip() == f"; touch {marker}"


def test_quality_environment_preserves_enterprise_inputs_and_removes_redirects(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preserved = {
        "HTTP_PROXY": "http://proxy.example",
        "HTTPS_PROXY": "https://proxy.example",
        "NO_PROXY": "localhost",
        "PIP_INDEX_URL": "https://packages.example/simple",
        "NPM_CONFIG_REGISTRY": "https://packages.example/npm",
        "GIT_SSH_COMMAND": "ssh -F enterprise.conf",
    }
    for name, value in preserved.items():
        monkeypatch.setenv(name, value)
    for name in _REDIRECTION_ENV:
        monkeypatch.setenv(name, f"redirected-{name}")

    environment = quality_command_environment(os.environ)
    assert {name: environment[name] for name in preserved} == preserved
    assert _REDIRECTION_ENV.isdisjoint(environment)

    result = _run(
        repository,
        sys.executable,
        "-c",
        (
            "import json, os; "
            "print(json.dumps({k: os.environ.get(k) for k in "
            f"{sorted([*preserved, *_REDIRECTION_ENV])!r}}}))"
        ),
    )
    child = json.loads(result.stdout_tail)
    assert {name: child[name] for name in preserved} == preserved
    assert all(child[name] is None for name in _REDIRECTION_ENV)

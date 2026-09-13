"""Real-process contracts for the normal-path CLI update notice."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import packaging_backend
import pytest

from ai_sdlc.routers.bootstrap import init_project


def _copy_dependency_site_packages(destination: Path) -> Path:
    source = Path(click.__file__).resolve().parents[1]
    destination.mkdir(parents=True, exist_ok=True)

    for item in source.iterdir():
        if item.name == "ai_sdlc":
            continue
        if item.name.startswith("ai_sdlc-") and item.name.endswith(".dist-info"):
            continue
        if item.name.startswith("ai_sdlc") and item.suffix == ".pth":
            continue

        target = destination / item.name
        if target.exists():
            continue
        try:
            target.symlink_to(item, target_is_directory=item.is_dir())
        except OSError:
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

    return destination


def test_dependency_overlay_exposes_runtime_deps_without_candidate(
    tmp_path: Path,
) -> None:
    overlay = _copy_dependency_site_packages(tmp_path / "dependency-overlay")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(overlay)
    probe = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import importlib.util; "
                "import jinja2, pydantic, rich, typer, yaml; "
                "assert importlib.util.find_spec('ai_sdlc') is None"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def _update_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AI_SDLC_UPDATE_ADVISOR_TEST_INSTALLED": "1",
            "AI_SDLC_UPDATE_ADVISOR_TEST_VERSION": "1.0.0",
            "AI_SDLC_UPDATE_ADVISOR_TEST_CHANNEL": "github-archive",
            "AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION": "v2.0.0",
            "AI_SDLC_UPDATE_ADVISOR_CACHE_DIR": str(tmp_path / "cache"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def test_json_process_keeps_stdout_clean_and_emits_one_stderr_notice(
    tmp_path: Path,
) -> None:
    init_project(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc", "status", "--json"],
        cwd=tmp_path,
        env=_update_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    notices = [
        line
        for line in result.stderr.splitlines()
        if line.startswith("AI_SDLC_UPDATE_NOTICE ")
    ]
    assert len(notices) == 1
    assert json.loads(notices[0].split(" ", 1)[1])["latest_version"] == "2.0.0"


def test_stale_update_cache_fails_open_after_refresh_error_and_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.core import update_advisor

    init_project(tmp_path)
    env = _update_env(tmp_path)
    env.pop("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION")
    env["AI_SDLC_UPDATE_ADVISOR_FORCE_TTY"] = "1"
    hooks = tmp_path / "offline-hooks"
    hooks.mkdir()
    install_log = tmp_path / "unexpected-install.txt"
    (hooks / "sitecustomize.py").write_text(
        """
import os
import urllib.error
from pathlib import Path

import ai_sdlc.core.update_advisor as advisor
import ai_sdlc.cli.self_update_cmd as updater

def fail_fetch(_timeout):
    raise urllib.error.URLError("offline")

advisor.fetch_latest_github_release = fail_fetch

def unexpected_install(*_args, **_kwargs):
    Path(os.environ["AI_SDLC_UNEXPECTED_INSTALL_LOG"]).write_text("called")

updater.self_update_install = unexpected_install
""".lstrip(),
        encoding="utf-8",
    )
    env["AI_SDLC_UNEXPECTED_INSTALL_LOG"] = str(install_log)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(hooks), env.get("PYTHONPATH", "")) if part
    )

    for key in (
        "AI_SDLC_UPDATE_ADVISOR_TEST_INSTALLED",
        "AI_SDLC_UPDATE_ADVISOR_TEST_VERSION",
        "AI_SDLC_UPDATE_ADVISOR_TEST_CHANNEL",
        "AI_SDLC_UPDATE_ADVISOR_CACHE_DIR",
    ):
        monkeypatch.setenv(key, env[key])
    identity = update_advisor.detect_runtime_identity()
    stale_time = datetime.now(UTC) - timedelta(days=2)
    update_advisor._save_cache(
        identity,
        update_advisor.UpdateCache(
            runtime_identity=identity.runtime_identity,
            installed_version="1.0.0",
            install_channel="github-archive",
            upstream_latest_version="2.0.0",
            channel_latest_version="2.0.0",
            last_checked_at=stale_time.isoformat(),
            last_success_checked_at=stale_time.isoformat(),
            last_check_status="success",
        ),
    )

    results = [
        subprocess.run(
            [sys.executable, "-m", "ai_sdlc", "status"],
            cwd=tmp_path,
            env=env,
            input="y\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        for _ in range(2)
    ]

    for result in results:
        assert result.returncode == 0, result.stderr
        assert "Result:" in result.stdout
        assert "是否升级" not in result.stderr
        assert "AI_SDLC_UPDATE_NOTICE" not in result.stderr
    assert not install_log.exists()


def test_module_invocation_update_replays_business_handler_once_and_exit_code(
    tmp_path: Path,
) -> None:
    init_project(tmp_path)
    hooks = tmp_path / "module-hooks"
    hooks.mkdir()
    process_log = tmp_path / "module-processes.jsonl"
    (hooks / "sitecustomize.py").write_text(
        """
import functools
import json
import os
import sys
from pathlib import Path

import typer
import ai_sdlc.cli.doctor_cmd as doctor_module
import ai_sdlc.cli.self_update_cmd as updater

log = Path(os.environ["AI_SDLC_REPLAY_TEST_LOG"])

def record(kind):
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "kind": kind,
            "argv": sys.argv,
            "orig_argv": sys.orig_argv,
            "handoff": "AI_SDLC_UPDATE_REPLAY_HANDOFF" in os.environ,
            "bypass": "AI_SDLC_UPDATE_REPLAY_BYPASS" in os.environ,
        }) + "\\n")

record("process")
original_doctor = doctor_module.doctor_command

@functools.wraps(original_doctor)
def wrapped_doctor(*args, **kwargs):
    record("handler")
    original_doctor(*args, **kwargs)
    raise typer.Exit(17)

doctor_module.doctor_command = wrapped_doctor

def fake_download(_url, archive_path):
    archive_path.write_bytes(b"archive")

def fake_extract(_archive_path, extract_root, _hint):
    bundle = extract_root / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "wheels" / "ai_sdlc-2.0.0-py3-none-any.whl").write_bytes(b"wheel")
    return bundle

updater._download_asset = fake_download
updater._extract_release_asset = fake_extract
updater._install_bundle_into_current_runtime = lambda *_args: None
updater._read_installed_version = lambda: "2.0.0"
updater._repair_current_user_path_if_possible = lambda: None
updater._verify_bare_cli_version = lambda version: version
""".lstrip(),
        encoding="utf-8",
    )
    env = _update_env(tmp_path)
    env["AI_SDLC_UPDATE_ADVISOR_FORCE_TTY"] = "1"
    env["AI_SDLC_REPLAY_TEST_LOG"] = str(process_log)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(hooks), env.get("PYTHONPATH", "")) if part
    )

    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc", "doctor"],
        cwd=tmp_path,
        env=env,
        input="y\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 17, result.stderr
    records = [json.loads(line) for line in process_log.read_text().splitlines()]
    processes = [record for record in records if record["kind"] == "process"]
    handlers = [record for record in records if record["kind"] == "handler"]
    assert len(processes) == 2
    assert all(
        record["orig_argv"][-3:] == ["-m", "ai_sdlc", "doctor"] for record in processes
    )
    assert len(handlers) == 1
    assert handlers[0]["handoff"] is False
    assert handlers[0]["bypass"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows console launcher")
def test_windows_direct_launcher_defers_update_and_runs_business_once(
    tmp_path: Path,
) -> None:
    init_project(tmp_path)
    launcher = Path(sys.executable).with_name("ai-sdlc.exe")
    assert launcher.is_file(), launcher
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    process_log = tmp_path / "processes.jsonl"
    sitecustomize = hooks / "sitecustomize.py"
    sitecustomize.write_text(
        """
import functools
import json
import os
import sys
from pathlib import Path

import typer
import ai_sdlc.cli.doctor_cmd as doctor_module

log = Path(os.environ["AI_SDLC_REPLAY_TEST_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "argv": sys.argv,
        "handoff": "AI_SDLC_UPDATE_REPLAY_HANDOFF" in os.environ,
        "bypass": "AI_SDLC_UPDATE_REPLAY_BYPASS" in os.environ,
    }) + "\\n")

import ai_sdlc.cli.self_update_cmd as target

original_doctor = doctor_module.doctor_command

@functools.wraps(original_doctor)
def wrapped_doctor(*args, **kwargs):
    original_doctor(*args, **kwargs)
    raise typer.Exit(17)

doctor_module.doctor_command = wrapped_doctor

def fake_download(_url, archive_path):
    archive_path.write_bytes(b"archive")

def fake_extract(_archive_path, extract_root, _hint):
    bundle = extract_root / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "wheels" / "ai_sdlc-2.0.0-py3-none-any.whl").write_bytes(b"wheel")
    return bundle

target._download_asset = fake_download
target._extract_release_asset = fake_extract
target._install_bundle_into_current_runtime = lambda *_args: None
target._read_installed_version = lambda: "2.0.0"
target._repair_current_user_path_if_possible = lambda: None
target._verify_bare_cli_version = lambda version: version
""".lstrip(),
        encoding="utf-8",
    )
    env = _update_env(tmp_path)
    env["AI_SDLC_UPDATE_ADVISOR_FORCE_TTY"] = "1"
    env["AI_SDLC_REPLAY_TEST_LOG"] = str(process_log)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(hooks), env.get("PYTHONPATH", "")) if part
    )

    result = subprocess.run(
        [str(launcher), "doctor"],
        cwd=tmp_path,
        env=env,
        input="y\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 17, result.stderr
    records = [json.loads(line) for line in process_log.read_text().splitlines()]
    business = [record for record in records if record["argv"][1:] == ["doctor"]]
    assert len(business) == 1
    assert business[0]["handoff"] is False
    assert business[0]["bypass"] is False
    updater = [record for record in records if "self-update" in record["argv"]]
    assert updater == []
    assert "无法安全原地替换" in result.stderr
    assert "-m" in result.stderr
    assert "ai_sdlc" in result.stderr


@pytest.mark.parametrize("use_stable_shim", [False, True])
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows wheel upgrade")
def test_windows_launcher_can_upgrade_its_live_installed_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_stable_shim: bool,
) -> None:
    metadata = packaging_backend._load_metadata()
    assert metadata["version"] == "3.1.0"
    old_dist = tmp_path / "old-dist"
    new_dist = tmp_path / "new-dist"
    monkeypatch.setattr(
        packaging_backend,
        "_load_metadata",
        lambda: {**metadata, "version": "1.0.0"},
    )
    old_wheel = old_dist / packaging_backend.build_wheel(str(old_dist))
    monkeypatch.setattr(
        packaging_backend,
        "_load_metadata",
        lambda: {**metadata, "version": "2.0.0"},
    )
    new_wheel = new_dist / packaging_backend.build_wheel(str(new_dist))

    venv_dir = tmp_path / "installed-runtime"
    create_venv = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create_venv.returncode == 0, create_venv.stderr
    venv_python = venv_dir / "Scripts" / "python.exe"
    launcher = venv_dir / "Scripts" / "ai-sdlc.exe"
    _copy_dependency_site_packages(venv_dir / "Lib" / "site-packages")
    dependency_probe = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import jinja2, pydantic, rich, typer, yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dependency_probe.returncode == 0, dependency_probe.stderr
    install_old = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(old_wheel)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert install_old.returncode == 0, install_old.stderr
    assert launcher.is_file(), launcher
    original_launcher_bytes = launcher.read_bytes()
    command_launcher = launcher
    if use_stable_shim:
        stable_bin = tmp_path / "stable-bin"
        stable_bin.mkdir()
        command_launcher = stable_bin / "ai-sdlc.exe"
        shutil.copy2(launcher, command_launcher)
        (stable_bin / "ai-sdlc-runtime.txt").write_text(
            f"{venv_python}\n", encoding="utf-8"
        )

    project = tmp_path / "project"
    project.mkdir()
    init_project(project)
    hooks = tmp_path / "wheel-upgrade-hooks"
    hooks.mkdir()
    process_log = tmp_path / "wheel-upgrade-processes.jsonl"
    (hooks / "sitecustomize.py").write_text(
        """
import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path

log = Path(os.environ["AI_SDLC_REPLAY_TEST_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "argv": sys.argv,
        "version": importlib.metadata.version("ai-sdlc"),
        "handoff": "AI_SDLC_UPDATE_REPLAY_HANDOFF" in os.environ,
        "bypass": "AI_SDLC_UPDATE_REPLAY_BYPASS" in os.environ,
    }) + "\\n")

import ai_sdlc.cli.self_update_cmd as target

def fake_download(_url, archive_path):
    archive_path.write_bytes(b"archive")

def fake_extract(_archive_path, extract_root, _hint):
    bundle = extract_root / "bundle"
    wheels = bundle / "wheels"
    wheels.mkdir(parents=True)
    source = Path(os.environ["AI_SDLC_REPLAY_TEST_WHEEL"])
    shutil.copy2(source, wheels / source.name)
    return bundle

target._download_asset = fake_download
target._extract_release_asset = fake_extract
target._repair_current_user_path_if_possible = lambda: None
target._repair_user_path_if_possible = lambda _path: None
target._verify_bare_cli_version = lambda version: version
""".lstrip(),
        encoding="utf-8",
    )
    env = _update_env(tmp_path)
    env["AI_SDLC_UPDATE_ADVISOR_FORCE_TTY"] = "1"
    env["AI_SDLC_REPLAY_TEST_LOG"] = str(process_log)
    env["AI_SDLC_REPLAY_TEST_WHEEL"] = str(new_wheel)
    env["PYTHONPATH"] = str(hooks)
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    if use_stable_shim:
        env["PATH"] = os.pathsep.join(
            part for part in (str(command_launcher.parent), env.get("PATH", "")) if part
        )
    else:
        env["PATH"] = os.pathsep.join(
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if part and not (Path(part) / "ai-sdlc.exe").is_file()
        )

    business_argv = ["status"] if use_stable_shim else ["status", "--json"]
    if not use_stable_shim:
        env.pop("AI_SDLC_UPDATE_ADVISOR_FORCE_TTY", None)
    result = subprocess.run(
        [str(command_launcher), *business_argv],
        cwd=project,
        env=env,
        input="y\n" if use_stable_shim else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    records = [json.loads(line) for line in process_log.read_text().splitlines()]
    business = [record for record in records if record["argv"][1:] == business_argv]
    updater = [record for record in records if "self-update" in record["argv"]]
    if use_stable_shim:
        assert [record["version"] for record in business] == ["1.0.0", "2.0.0"]
        assert len(updater) == 1
        assert updater[0]["version"] == "1.0.0"
        assert updater[0]["handoff"] is False
        assert business[1]["handoff"] is False
        assert business[1]["bypass"] is True
    else:
        assert [record["version"] for record in business] == ["1.0.0"]
        assert updater == []
        json.loads(result.stdout)
        notices = [
            line
            for line in result.stderr.splitlines()
            if line.startswith("AI_SDLC_UPDATE_NOTICE ")
        ]
        assert len(notices) == 1
        notice = json.loads(notices[0].split(" ", 1)[1])
        assert notice["reason"] == "windows_direct_launcher_locked"
        assert notice["current_command_continued"] is True
        explicit = subprocess.run(
            [
                str(command_launcher),
                "self-update",
                "install",
                "--version",
                "2.0.0",
            ],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert explicit.returncode != 0
        assert "-m" in explicit.stderr
        assert launcher.read_bytes() == original_launcher_bytes
        still_old = subprocess.run(
            [
                str(venv_python),
                "-c",
                "from importlib.metadata import version; print(version('ai-sdlc'))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert still_old.stdout.strip() == "1.0.0"
        upgrade = subprocess.run(
            notice["upgrade_argv"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert upgrade.returncode == 0, upgrade.stdout + upgrade.stderr
    installed = subprocess.run(
        [
            str(venv_python),
            "-c",
            "from importlib.metadata import version; print(version('ai-sdlc'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout.strip() == "2.0.0"
    stable_dir = Path(env["LOCALAPPDATA"]) / "AI-SDLC" / "bin"
    assert (stable_dir / "ai-sdlc.exe").is_file()
    assert (stable_dir / "ai-sdlc-runtime.txt").read_text().strip() == str(venv_python)
    stable_version = subprocess.run(
        [str(stable_dir / "ai-sdlc.exe"), "--version"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert stable_version.returncode == 0, stable_version.stderr
    assert stable_version.stdout.strip() == "2.0.0"


def test_replay_handoff_and_bypass_are_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    request = self_update_cmd.ReplayRequest(
        executable="ai-sdlc",
        argv=("status", "--details"),
    )
    self_update_cmd._publish_replay_handoff(request)

    assert self_update_cmd._consume_replay_handoff() == request
    assert "AI_SDLC_UPDATE_REPLAY_HANDOFF" not in os.environ

    monkeypatch.setenv("AI_SDLC_UPDATE_REPLAY_BYPASS", "1")
    assert self_update_cmd.consume_update_replay_bypass() is True
    assert "AI_SDLC_UPDATE_REPLAY_BYPASS" not in os.environ


def test_replay_uses_exact_argv_and_propagates_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(self_update_cmd.subprocess, "run", fake_run)
    request = self_update_cmd.ReplayRequest(
        executable="ai-sdlc",
        argv=("loop", "status", "--json", "literal;not-shell"),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._replay_updated_command(request)

    assert exc_info.value.exit_code == 17
    assert seen["command"] == [
        "ai-sdlc",
        "loop",
        "status",
        "--json",
        "literal;not-shell",
    ]
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["env"]["AI_SDLC_UPDATE_REPLAY_BYPASS"] == "1"
    assert "AI_SDLC_UPDATE_REPLAY_HANDOFF" not in kwargs["env"]


def test_capture_replay_request_preserves_python_module_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.setattr(self_update_cmd.sys, "executable", "/runtime/python")
    monkeypatch.setattr(
        self_update_cmd.sys,
        "orig_argv",
        ["/runtime/python", "-I", "-m", "ai_sdlc", "loop", "status"],
    )
    monkeypatch.setattr(
        self_update_cmd.sys,
        "argv",
        ["/site-packages/ai_sdlc/__main__.py", "loop", "status"],
    )

    request = self_update_cmd._capture_replay_request()

    assert request == self_update_cmd.ReplayRequest(
        executable="/runtime/python",
        argv=("-I", "-m", "ai_sdlc", "loop", "status"),
    )


def test_capture_windows_launcher_uses_current_process_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    process_image = Path(r"C:\Program Files\AI-SDLC\bin\ai-sdlc.exe")
    monkeypatch.setattr(self_update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(
        self_update_cmd.sys,
        "argv",
        ["ai-sdlc.exe", "status", "--details"],
    )
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", "ai-sdlc.exe")
    monkeypatch.setattr(self_update_cmd, "_module_replay_prefix", lambda: None)
    monkeypatch.setattr(
        self_update_cmd,
        "_locate_windows_launcher",
        lambda: process_image,
    )

    assert self_update_cmd._capture_replay_request() == self_update_cmd.ReplayRequest(
        executable=str(process_image),
        argv=("status", "--details"),
    )


def test_windows_launcher_locator_uses_path_for_distlib_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    launcher = tmp_path / "安装 目录" / "ai-sdlc.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", "ai-sdlc.exe")
    monkeypatch.setattr(
        self_update_cmd.shutil,
        "which",
        lambda command: str(launcher) if command == "ai-sdlc.exe" else None,
    )

    assert self_update_cmd._locate_windows_launcher() == launcher


def test_windows_launcher_locator_prefers_existing_absolute_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    launcher = tmp_path / "outside-path" / "ai-sdlc.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", str(launcher))
    monkeypatch.setattr(
        self_update_cmd.shutil,
        "which",
        lambda _command: pytest.fail("PATH lookup must not replace an absolute entry"),
    )

    assert self_update_cmd._locate_windows_launcher() == launcher


def test_windows_launcher_locator_fails_when_path_has_no_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    runtime = tmp_path / "runtime" / "python.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"python")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", "ai-sdlc.exe")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(self_update_cmd.shutil, "which", lambda _command: None)

    with pytest.raises(self_update_cmd.SelfUpdateError):
        self_update_cmd._locate_windows_launcher()


def test_windows_launcher_locator_falls_back_to_runtime_without_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    launcher = scripts / "ai-sdlc.exe"
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", "ai-sdlc.exe")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(self_update_cmd.shutil, "which", lambda _command: None)

    assert self_update_cmd._locate_windows_launcher() == launcher


def test_windows_launcher_locator_rejects_ambiguous_runtime_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    runtime = runtime_dir / "python.exe"
    runtime.write_bytes(b"python")
    (runtime_dir / "ai-sdlc.exe").write_bytes(b"launcher")
    nested_scripts = runtime_dir / "Scripts"
    nested_scripts.mkdir()
    (nested_scripts / "ai-sdlc.exe").write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", "ai-sdlc.exe")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(self_update_cmd.shutil, "which", lambda _command: None)

    with pytest.raises(self_update_cmd.SelfUpdateError, match="ambiguous"):
        self_update_cmd._locate_windows_launcher()


def test_windows_launcher_reexec_carries_the_process_only_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    request = self_update_cmd.ReplayRequest(
        executable=r"C:\Python\Scripts\ai-sdlc.exe",
        argv=("doctor", "--json"),
    )
    self_update_cmd._publish_replay_handoff(request)
    monkeypatch.setattr(
        self_update_cmd, "_should_reexec_windows_launcher", lambda: True
    )
    monkeypatch.setattr(self_update_cmd.sys, "executable", r"C:\Python\python.exe")
    launcher = Path(r"C:\Python\Scripts\ai-sdlc.exe")
    monkeypatch.setattr(
        self_update_cmd,
        "_classify_windows_launcher",
        lambda: self_update_cmd.WindowsLauncherEntry(
            launcher, self_update_cmd._WINDOWS_ENTRY_STABLE
        ),
    )

    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0 if len(calls) == 1 else 17)

    monkeypatch.setattr(self_update_cmd.subprocess, "run", fake_run)

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._reexec_windows_launcher_if_needed("2.0.0")

    assert exc_info.value.exit_code == 17
    assert calls[0][0][-4:] == [
        "self-update",
        "install",
        "--version",
        "2.0.0",
    ]
    kwargs = calls[0][1]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["check"] is False
    assert kwargs["env"]["AI_SDLC_SELF_UPDATE_REEXEC"] == "1"
    assert "AI_SDLC_UPDATE_REPLAY_HANDOFF" not in kwargs["env"]
    assert calls[1][0] == [request.executable, *request.argv]
    assert len(calls) == 2


def test_explicit_stable_windows_launcher_update_without_handoff_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.delenv("AI_SDLC_UPDATE_REPLAY_HANDOFF", raising=False)
    monkeypatch.setattr(
        self_update_cmd, "_should_reexec_windows_launcher", lambda: True
    )
    monkeypatch.setattr(self_update_cmd.sys, "executable", r"C:\Python\python.exe")
    launcher = Path(r"C:\Users\me\AI-SDLC\bin\ai-sdlc.exe")
    monkeypatch.setattr(
        self_update_cmd,
        "_classify_windows_launcher",
        lambda: self_update_cmd.WindowsLauncherEntry(
            launcher, self_update_cmd._WINDOWS_ENTRY_STABLE
        ),
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        self_update_cmd.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command) or subprocess.CompletedProcess(command, 0)
        ),
    )
    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._reexec_windows_launcher_if_needed("2.0.0")

    assert exc_info.value.exit_code == 0
    assert len(commands) == 1
    assert commands[0][-4:] == [
        "self-update",
        "install",
        "--version",
        "2.0.0",
    ]


def test_windows_launcher_delegate_failure_is_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.setattr(
        self_update_cmd, "_should_reexec_windows_launcher", lambda: True
    )
    launcher = Path(r"C:\Users\me\AI-SDLC\bin\ai-sdlc.exe")
    monkeypatch.setattr(
        self_update_cmd,
        "_classify_windows_launcher",
        lambda: self_update_cmd.WindowsLauncherEntry(
            launcher, self_update_cmd._WINDOWS_ENTRY_STABLE
        ),
    )
    monkeypatch.setattr(
        self_update_cmd.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("missing Python runtime")
        ),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._reexec_windows_launcher_if_needed("2.0.0")

    assert exc_info.value.exit_code == 1


def test_windows_replay_launch_failure_is_nonzero_after_stable_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    request = self_update_cmd.ReplayRequest("ai-sdlc", ("status",))
    self_update_cmd._publish_replay_handoff(request)
    monkeypatch.setattr(
        self_update_cmd, "_should_reexec_windows_launcher", lambda: True
    )
    launcher = Path(r"C:\Users\me\AI-SDLC\bin\ai-sdlc.exe")
    monkeypatch.setattr(
        self_update_cmd,
        "_classify_windows_launcher",
        lambda: self_update_cmd.WindowsLauncherEntry(
            launcher, self_update_cmd._WINDOWS_ENTRY_STABLE
        ),
    )
    calls = 0

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0)
        raise OSError("missing updated launcher")

    monkeypatch.setattr(self_update_cmd.subprocess, "run", fake_run)

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._reexec_windows_launcher_if_needed("2.0.0")

    assert exc_info.value.exit_code == 1


def test_explicit_direct_windows_launcher_update_is_rejected_without_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_sdlc.cli import self_update_cmd

    launcher = Path(r"C:\Python\Scripts\ai-sdlc.exe")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        self_update_cmd, "_should_reexec_windows_launcher", lambda: True
    )
    monkeypatch.setattr(
        self_update_cmd,
        "_classify_windows_launcher",
        lambda: self_update_cmd.WindowsLauncherEntry(
            launcher, self_update_cmd._WINDOWS_ENTRY_DIRECT
        ),
    )
    monkeypatch.setattr(
        self_update_cmd.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._reexec_windows_launcher_if_needed("2.0.0")

    assert exc_info.value.exit_code == 1
    assert commands == []
    assert "-m" in capsys.readouterr().err


def test_external_windows_launcher_requires_matching_runtime_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    runtime = tmp_path / "runtime" / "python.exe"
    runtime.parent.mkdir()
    runtime.write_bytes(b"python")
    stable = tmp_path / "stable" / "ai-sdlc.exe"
    stable.parent.mkdir()
    stable.write_bytes(b"launcher")
    marker = stable.with_name("ai-sdlc-runtime.txt")
    marker.write_text(f"{runtime}\n", encoding="utf-8")
    monkeypatch.setattr(self_update_cmd.sys, "argv", [str(stable), "status"])
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(
        self_update_cmd,
        "_locate_windows_launcher",
        lambda: stable,
    )

    assert self_update_cmd._classify_windows_launcher() == (
        self_update_cmd.WindowsLauncherEntry(
            stable, self_update_cmd._WINDOWS_ENTRY_STABLE
        )
    )

    marker.write_text(f"{tmp_path / 'other-python.exe'}\n", encoding="utf-8")
    with pytest.raises(self_update_cmd.SelfUpdateError):
        self_update_cmd._classify_windows_launcher()

    marker.write_text(f"{runtime}\n", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == stable or original_is_symlink(candidate),
    )
    with pytest.raises(self_update_cmd.SelfUpdateError):
        self_update_cmd._classify_windows_launcher()


def test_runtime_owned_windows_launcher_does_not_require_strict_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "运行 时" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    launcher = scripts / "ai-sdlc.exe"
    launcher.write_bytes(b"launcher")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(self_update_cmd.sys, "argv", ["ai-sdlc.exe", "status"])
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(
        self_update_cmd,
        "_locate_windows_launcher",
        lambda: launcher,
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("strict resolve is unavailable for the live launcher")
        ),
    )

    entry = self_update_cmd._classify_windows_launcher()

    assert entry == self_update_cmd.WindowsLauncherEntry(
        launcher, self_update_cmd._WINDOWS_ENTRY_DIRECT
    )
    assert launcher.is_file()


def test_classify_windows_launcher_uses_frozen_entry_after_live_argv_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    launcher = scripts / "ai-sdlc.exe"
    launcher.write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd, "_PROCESS_ENTRY_ARGV0", str(launcher))
    monkeypatch.setattr(
        self_update_cmd.sys,
        "argv",
        ["self-update", "install", "--version", "2.0.0"],
    )
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))

    entry = self_update_cmd._classify_windows_launcher()

    assert entry == self_update_cmd.WindowsLauncherEntry(
        launcher, self_update_cmd._WINDOWS_ENTRY_DIRECT
    )
    assert launcher.is_file()


@pytest.mark.parametrize("missing_runtime", [False, True])
def test_windows_launcher_file_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_runtime: bool,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    launcher = scripts / "ai-sdlc.exe"
    if missing_runtime:
        launcher.write_bytes(b"launcher")
    else:
        runtime.write_bytes(b"python")
        launcher.mkdir()
    monkeypatch.setattr(self_update_cmd.sys, "argv", [str(launcher), "status"])
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setattr(
        self_update_cmd,
        "_locate_windows_launcher",
        lambda: launcher,
    )

    with pytest.raises(self_update_cmd.SelfUpdateError):
        self_update_cmd._classify_windows_launcher()


def test_windows_direct_machine_notice_is_structured_and_keeps_stdout_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.setattr(self_update_cmd.sys, "executable", r"C:\Python\python.exe")
    self_update_cmd._render_windows_direct_migration_notice(
        current_version="1.0.0",
        latest_version="2.0.0",
        machine_output=True,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    line = captured.err.strip()
    assert line.startswith("AI_SDLC_UPDATE_NOTICE ")
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["reason"] == "windows_direct_launcher_locked"
    assert payload["current_command_continued"] is True
    assert payload["upgrade_argv"] == [
        os.path.abspath(r"C:\Python\python.exe"),
        "-m",
        "ai_sdlc",
        "self-update",
        "install",
        "--version",
        "2.0.0",
    ]


def test_module_update_prepares_stable_windows_shim_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    launcher = scripts / "ai-sdlc.exe"
    launcher.write_bytes(b"launcher-v2")
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setattr(self_update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    stable_dir = self_update_cmd._prepare_windows_stable_shim()

    assert stable_dir == local_app_data / "AI-SDLC" / "bin"
    stable_launcher = stable_dir / "ai-sdlc.exe"
    marker = stable_dir / "ai-sdlc-runtime.txt"
    assert stable_launcher.read_bytes() == b"launcher-v2"
    assert marker.read_text().strip() == str(runtime)

    stable_launcher.write_bytes(b"active-stable-launcher")
    assert self_update_cmd._prepare_windows_stable_shim() == stable_dir
    assert stable_launcher.read_bytes() == b"active-stable-launcher"


@pytest.mark.parametrize("conflict", ["missing-marker", "wrong-runtime"])
def test_windows_stable_shim_conflict_fails_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    (scripts / "ai-sdlc.exe").write_bytes(b"launcher")
    stable_dir = tmp_path / "local-app-data" / "AI-SDLC" / "bin"
    stable_dir.mkdir(parents=True)
    (stable_dir / "ai-sdlc.exe").write_bytes(b"stable")
    if conflict == "wrong-runtime":
        other_runtime = tmp_path / "other" / "python.exe"
        other_runtime.parent.mkdir()
        other_runtime.write_bytes(b"python")
        (stable_dir / "ai-sdlc-runtime.txt").write_text(
            f"{other_runtime}\n", encoding="utf-8"
        )
    monkeypatch.setattr(self_update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    install_calls: list[object] = []
    monkeypatch.setattr(
        self_update_cmd,
        "_reexec_windows_launcher_if_needed",
        lambda _version: None,
    )
    monkeypatch.setattr(
        self_update_cmd,
        "_release_asset_context",
        lambda _version: install_calls.append("release") or ("", "", "", {}),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd.self_update_install(version="2.0.0")

    assert exc_info.value.exit_code == 1
    assert install_calls == []


def test_windows_stable_shim_write_failure_fails_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_sdlc.cli import self_update_cmd

    scripts = tmp_path / "runtime" / "Scripts"
    scripts.mkdir(parents=True)
    runtime = scripts / "python.exe"
    runtime.write_bytes(b"python")
    (scripts / "ai-sdlc.exe").write_bytes(b"launcher")
    monkeypatch.setattr(self_update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setattr(
        self_update_cmd.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    install_calls: list[object] = []
    monkeypatch.setattr(
        self_update_cmd,
        "_reexec_windows_launcher_if_needed",
        lambda _version: None,
    )
    monkeypatch.setattr(
        self_update_cmd,
        "_release_asset_context",
        lambda _version: install_calls.append("release") or ("", "", "", {}),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd.self_update_install(version="2.0.0")

    assert exc_info.value.exit_code == 1
    assert install_calls == []


def test_windows_ambiguous_runtime_launchers_fail_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_sdlc.cli import self_update_cmd

    runtime_dir = tmp_path / "runtime"
    scripts = runtime_dir / "Scripts"
    scripts.mkdir(parents=True)
    runtime = runtime_dir / "python.exe"
    runtime.write_bytes(b"python")
    (runtime_dir / "ai-sdlc.exe").write_bytes(b"root-launcher")
    (scripts / "ai-sdlc.exe").write_bytes(b"scripts-launcher")
    monkeypatch.setattr(self_update_cmd.sys, "platform", "win32")
    monkeypatch.setattr(self_update_cmd.sys, "executable", str(runtime))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setattr(
        self_update_cmd,
        "_reexec_windows_launcher_if_needed",
        lambda _version: None,
    )
    install_calls: list[object] = []
    monkeypatch.setattr(
        self_update_cmd,
        "_release_asset_context",
        lambda _version: install_calls.append("release") or ("", "", "", {}),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd.self_update_install(version="2.0.0")

    assert exc_info.value.exit_code == 1
    assert install_calls == []


def test_malformed_auto_replay_handoff_fails_before_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.setenv("AI_SDLC_UPDATE_REPLAY_HANDOFF", "not-json")
    monkeypatch.setattr(
        self_update_cmd,
        "_reexec_windows_launcher_if_needed",
        lambda _version: None,
    )
    release = monkeypatch.setattr(
        self_update_cmd,
        "_release_asset_context",
        lambda _version: pytest.fail("installer must not start"),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd.self_update_install(version="2.0.0")

    assert release is None
    assert exc_info.value.exit_code == 1
    assert "AI_SDLC_UPDATE_REPLAY_HANDOFF" not in os.environ


def test_replay_launch_failure_is_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_sdlc.cli import self_update_cmd

    monkeypatch.setattr(
        self_update_cmd.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing launcher")),
    )

    with pytest.raises(click.exceptions.Exit) as exc_info:
        self_update_cmd._replay_updated_command(
            self_update_cmd.ReplayRequest("ai-sdlc", ("status",))
        )

    assert exc_info.value.exit_code == 1

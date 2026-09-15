"""Regression tests for shared CLI hooks."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from rich.console import Console

from ai_sdlc.cli import cli_hooks
from ai_sdlc.core import config as config_module
from ai_sdlc.core.config import YamlStoreError, load_project_config
from ai_sdlc.routers.bootstrap import init_project
from ai_sdlc.utils.helpers import PROJECT_CONFIG_PATH


@pytest.mark.parametrize("explicit_root", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
def test_adapter_hooks_consume_actual_config_read_recovery(
    tmp_path, monkeypatch, explicit_root, persistent
):
    init_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / PROJECT_CONFIG_PATH
    original_read = Path.read_text
    failures = 0

    def read(path, *args, **kwargs):
        nonlocal failures
        if path == target and (persistent or failures < 2):
            failures += 1
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(config_module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(Path, "read_text", read)
    console = Console(record=True)
    hook = cli_hooks.run_ide_adapter_for_root if explicit_root else cli_hooks.run_ide_adapter_if_initialized
    args = (tmp_path,) if explicit_root else ()
    if persistent:
        with pytest.raises(YamlStoreError) as caught:
            hook(*args, console=console)
        assert isinstance(caught.value.__cause__, PermissionError)
        assert failures == 5
    else:
        hook(*args, console=console)
        assert failures == 2
        assert load_project_config(tmp_path).adapter_applied_at
    assert "Current command will continue" not in console.export_text()


def test_adapter_hook_warns_and_continues_when_project_config_is_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _locked_config(_root: Path) -> object:
        raise PermissionError(
            "[WinError 5] Access is denied: "
            "'.ai-sdlc/project/config/project-config.yaml'"
        )

    console = Console(record=True, width=120)
    monkeypatch.setattr(cli_hooks, "ensure_ide_adaptation", _locked_config)

    cli_hooks.run_ide_adapter_if_initialized(console=console)

    output = console.export_text()
    normalized_output = " ".join(output.split())
    assert "project-config.yaml appears to be temporarily locked" in output
    assert "Current command will continue" in output
    assert (
        "does not mean code generation or the frontend build failed"
        in normalized_output
    )
    assert "WinError 5" in output


def test_adapter_hook_still_raises_unexpected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _unexpected_error(_root: Path) -> object:
        raise RuntimeError("unexpected adapter failure")

    monkeypatch.setattr(cli_hooks, "ensure_ide_adaptation", _unexpected_error)

    with pytest.raises(RuntimeError, match="unexpected adapter failure"):
        cli_hooks.run_ide_adapter_if_initialized(console=Console())


def test_adapter_hook_reraises_permission_errors_outside_project_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _locked_adapter_file(_root: Path) -> object:
        raise PermissionError("[WinError 5] Access is denied: 'AGENTS.md'")

    monkeypatch.setattr(cli_hooks, "ensure_ide_adaptation", _locked_adapter_file)

    with pytest.raises(PermissionError, match="AGENTS.md"):
        cli_hooks.run_ide_adapter_if_initialized(console=Console())

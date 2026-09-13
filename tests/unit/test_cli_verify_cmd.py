"""Unit tests for direct read-only constraint verification."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

import ai_sdlc.cli.verify_cmd as verify_cmd_module


def test_string_list_deduplicates_values() -> None:
    assert verify_cmd_module._string_list(["a", "a", "b", "  ", "b"]) == [
        "a",
        "b",
    ]


def _report(*, blockers: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(
        blockers=blockers,
        source_name="verify constraints",
        gate_name="Verification Gate",
        profile=verify_cmd_module.ConstraintProfile.PROJECT,
        check_objects=(),
        coverage_gaps=(),
    )


def test_verify_constraints_terminal_deduplicates_blockers_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_console = verify_cmd_module.console
    verify_cmd_module.console = Console(width=200, force_terminal=False)
    monkeypatch.setattr(verify_cmd_module, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        verify_cmd_module,
        "build_constraint_report",
        lambda root, *, profile: _report(
            blockers=("duplicate blocker", "duplicate blocker")
        ),
    )

    try:
        with (
            verify_cmd_module.console.capture() as capture,
            pytest.raises(typer.Exit) as exc_info,
        ):
            verify_cmd_module.verify_constraints(
                as_json=False,
                profile=verify_cmd_module.ConstraintProfile.PROJECT,
            )
        output = capture.get()
    finally:
        verify_cmd_module.console = original_console

    assert exc_info.value.exit_code == 1
    assert output.count("duplicate blocker") == 1
    assert not (tmp_path / ".ai-sdlc" / "telemetry").exists()
    assert not (tmp_path / ".ai-sdlc" / "proof").exists()


def test_verify_constraints_success_is_direct() -> None:
    assert _report(blockers=()).blockers == ()
    assert not hasattr(verify_cmd_module, "RuntimeTelemetry")

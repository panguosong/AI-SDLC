from __future__ import annotations

from pathlib import Path

from ai_sdlc.core.slimming_advice import SlimmingAdvice, collect_slimming_advice


def _kinds(path: Path) -> set[str]:
    return {item.kind for item in collect_slimming_advice([path])}


def test_collect_slimming_advice_reports_bounded_structural_hints(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large_module.py"
    long_body = "\n".join(f"    value_{index} = {index}" for index in range(85))
    source.write_text(
        "\n".join(
            [
                "class Repository:",
                "    pass",
                "",
                "class Presenter:",
                "    pass",
                "",
                "def duplicated_one(value):",
                "    return value + 1",
                "",
                "def duplicated_two(value):",
                "    return value + 1",
                "",
                "def wrapped(value):",
                "    return duplicated_one(value)",
                "",
                "def only_caller():",
                "    return wrapped(1)",
                "",
                "def long_function():",
                long_body,
                "    return value_84",
                "",
                *(f"def helper_{index}():\n    return {index}" for index in range(5)),
            ]
        ),
        encoding="utf-8",
    )

    kinds = _kinds(source)

    assert {
        "function-length",
        "same-file-duplication",
        "single-caller-wrapper",
        "mixed-responsibility",
    } <= kinds


def test_collect_slimming_advice_can_report_file_length(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_text("\n".join(f"line {index}" for index in range(510)), encoding="utf-8")

    assert "file-length" in _kinds(source)


def test_collect_slimming_advice_is_advisory_and_failure_tolerant(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.py"
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\xff\xfe")

    assert collect_slimming_advice([missing, binary]) == []
    assert set(SlimmingAdvice.model_fields) == {"kind", "path", "line", "message"}
    forbidden = {"status", "verdict", "waiver", "receipt", "history", "policy"}
    assert not forbidden.intersection(SlimmingAdvice.model_fields)

"""候选本地 CI collection 与 JUnit 终态行为测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ci_static_assurance.py"
_COMMIT = "a" * 40
_CELL = "ubuntu-latest-py3.11"


def _load_module():
    spec = importlib.util.spec_from_file_location("ci_static_assurance", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(module, *nodeids: str):
    return module.build_collection_manifest(nodeids, [_CELL], _COMMIT)


def _write_junit(
    path: Path,
    *,
    cases: list[tuple[str, str, str | None]],
    failures: int = 0,
    errors: int = 0,
) -> None:
    body = "".join(
        f'<testcase classname="{classname}" name="{name}">'
        + (f"<{terminal}/>" if terminal else "")
        + "</testcase>"
        for classname, name, terminal in cases
    )
    skipped = sum(terminal == "skipped" for _, _, terminal in cases)
    path.write_text(
        f'<testsuite tests="{len(cases)}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}">{body}</testsuite>',
        encoding="utf-8",
    )


def test_manifest_normalizes_only_path_and_binds_cell() -> None:
    module = _load_module()

    linux = module.build_collection_manifest(
        ["tests/a.py::test_case[param::one]"], [_CELL], _COMMIT
    )
    windows = module.build_collection_manifest(
        [r"tests\a.py::test_case[param::one]"], ["windows-latest-py3.14"], _COMMIT
    )

    assert linux["case_ids"] == windows["case_ids"]
    assert linux["execution_member_ids"] != windows["execution_member_ids"]
    assert linux["case_nodeids"][linux["case_ids"][0]] == (
        "tests/a.py::test_case[param::one]"
    )


@pytest.mark.parametrize(
    "nodeids",
    [[], ["missing-scope"], ["tests/a.py::test_one", "tests/a.py::test_one"]],
)
def test_manifest_rejects_empty_invalid_or_duplicate_collection(
    nodeids: list[str],
) -> None:
    module = _load_module()

    with pytest.raises(module.AssuranceError):
        module.build_collection_manifest(nodeids, [_CELL], _COMMIT)


def test_cell_evidence_accepts_complete_success_and_records_skip(tmp_path: Path) -> None:
    module = _load_module()
    manifest = _manifest(module, "tests/a.py::test_one", "tests/a.py::test_two")
    junit = tmp_path / "result.xml"
    _write_junit(
        junit,
        cases=[
            ("tests.a", "test_one", None),
            ("tests.a", "test_two", "skipped"),
        ],
    )

    result = module.build_cell_evidence(
        manifest,
        junit,
        cell=_CELL,
        source_commit=_COMMIT,
        started_at="2026-08-15T00:00:00Z",
        finished_at="2026-08-15T00:00:02Z",
    )

    assert result["status"] == "success"
    assert result["executed_count"] == 2
    assert result["skipped"] == 1
    assert result["skipped_case_ids"] == [
        module._stable_case_id("tests/a.py::test_two")
    ]


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("missing", "junit_missing"),
        ("empty", "junit_empty"),
        ("corrupt", "junit_corrupt"),
        ("duplicate", "duplicate_testcase"),
        ("failure", "non_success_terminal_state"),
        ("missing_case", "execution_count_mismatch"),
    ],
)
def test_cell_evidence_rejects_incomplete_or_failed_junit(
    tmp_path: Path,
    kind: str,
    reason: str,
) -> None:
    module = _load_module()
    manifest = _manifest(module, "tests/a.py::test_one")
    junit = tmp_path / "result.xml"
    if kind == "empty":
        junit.write_bytes(b"")
    elif kind == "corrupt":
        junit.write_text("<bad", encoding="utf-8")
    elif kind == "duplicate":
        _write_junit(
            junit,
            cases=[("tests.a", "test_one", None), ("tests.a", "test_one", None)],
        )
    elif kind == "failure":
        _write_junit(
            junit,
            cases=[("tests.a", "test_one", "failure")],
            failures=1,
        )
    elif kind == "missing_case":
        _write_junit(junit, cases=[])

    result = module.build_cell_evidence(
        manifest,
        junit,
        cell=_CELL,
        source_commit=_COMMIT,
        started_at="2026-08-15T00:00:00Z",
        finished_at="2026-08-15T00:00:02Z",
    )

    assert result["status"] == "failed"
    assert result["reason"] == reason


def test_collect_cli_runs_real_pytest_collection(tmp_path: Path) -> None:
    module = _load_module()
    (tmp_path / "test_sample.py").write_text(
        "def test_real_collection():\n    assert True\n",
        encoding="utf-8",
    )
    output = tmp_path / "manifest.json"

    result = module.main(
        [
            "collect",
            "--root",
            str(tmp_path),
            "--source-commit",
            _COMMIT,
            "--cell",
            _CELL,
            "--output",
            str(output),
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert list(payload["case_nodeids"].values()) == [
        "test_sample.py::test_real_collection"
    ]


def test_cli_exposes_only_candidate_local_commands() -> None:
    module = _load_module()
    parser = module._build_parser()
    subcommands = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )

    assert set(subcommands.choices) == {"collect", "cell-evidence", "aggregate"}

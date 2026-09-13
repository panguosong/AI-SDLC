"""候选自身测试集合与执行终态的行为合同。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "ci_static_assurance.py"
_COMMIT = "a" * 40
_CELL = "ubuntu-latest-py3.11"


def _load_module():
    spec = importlib.util.spec_from_file_location("ci_candidate_execution", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(module, *, cell: str = _CELL, commit: str = _COMMIT):
    return module.build_collection_manifest(
        ["tests/current.py::test_retained"],
        [cell],
        source_commit=commit,
    )


def _evidence(manifest, *, cell: str = _CELL, commit: str = _COMMIT):
    case_ids = list(manifest["case_ids"])
    return {
        "schema_version": "ci-cell-evidence-v1",
        "cell": cell,
        "source_commit": commit,
        "collection_manifest_digest": manifest["manifest_digest"],
        "collected_count": len(case_ids),
        "executed_count": len(case_ids),
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "skipped_case_ids": [],
        "duplicate_testcases": [],
        "status": "success",
        "reason": "complete",
        "duration_seconds": 1.0,
    }


def test_candidate_execution_accepts_intentional_test_removal_without_lineage() -> None:
    """删除废止测试后，当前候选的真实 collection 全部执行即可通过。"""
    module = _load_module()
    manifest = _manifest(module)

    result = module.verify_candidate_execution(
        [_CELL],
        [manifest],
        [_evidence(manifest)],
        candidate_commit=_COMMIT,
        fast_gate_status="success",
    )

    assert result["status"] == "success"
    assert result["cells"] == [_CELL]
    assert result["case_count"] == 1
    assert "baseline_digest" not in result
    assert "lineage" not in result


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("duplicate_cell", "duplicate_cell_manifest"),
        ("missing_cell", "cell_set_mismatch"),
        ("manifest_commit", "candidate_commit_mismatch"),
        ("missing_evidence", "cell_set_mismatch"),
        ("missing_case", "execution_count_mismatch"),
        ("duplicate_case", "duplicate_testcase"),
        ("failed_case", "non_success_cell"),
    ],
)
def test_candidate_execution_rejects_incomplete_or_failed_evidence(
    mutation: str,
    reason: str,
) -> None:
    """候选本地语义仍必须拒绝缺 cell、身份漂移和非成功终态。"""
    module = _load_module()
    expected_cells = [_CELL]
    manifests = [_manifest(module)]
    evidence = [_evidence(manifests[0])]

    if mutation == "duplicate_cell":
        manifests.append(dict(manifests[0]))
    elif mutation == "missing_cell":
        expected_cells.append("windows-latest-py3.14")
    elif mutation == "manifest_commit":
        manifests[0] = _manifest(module, commit="b" * 40)
    elif mutation == "missing_evidence":
        evidence.clear()
    elif mutation == "missing_case":
        evidence[0]["executed_count"] = 0
    elif mutation == "duplicate_case":
        evidence[0]["duplicate_testcases"] = ["tests.current::test_retained"]
    elif mutation == "failed_case":
        evidence[0]["status"] = "failed"
        evidence[0]["failures"] = 1

    result = module.verify_candidate_execution(
        expected_cells,
        manifests,
        evidence,
        candidate_commit=_COMMIT,
        fast_gate_status="success",
    )

    assert result["status"] == "failed"
    assert result["reason"] == reason

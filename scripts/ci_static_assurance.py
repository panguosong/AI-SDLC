#!/usr/bin/env python3
"""验证当前候选的 pytest collection、JUnit 终态和 CI cell 完整性。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

CASE_NAMESPACE = "pytest-nodeid-v1"
MANIFEST_SCHEMA = "ci-test-manifest-v1"
CELL_EVIDENCE_SCHEMA = "ci-cell-evidence-v1"
AGGREGATE_SCHEMA = "ci-assurance-report-v1"
DEFAULT_COLLECTION_COMMAND = "pytest --collect-only -q"


class AssuranceError(ValueError):
    """候选测试证据不完整。"""


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _canonical_digest(payload: Mapping[str, object], digest_field: str) -> str:
    canonical = {key: value for key, value in payload.items() if key != digest_field}
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(encoded)


def _normalize_nodeid(raw: str) -> str:
    nodeid = raw.strip()
    path, separator, scope = nodeid.partition("::")
    if not path or not separator or not scope:
        raise AssuranceError(f"invalid pytest nodeid: {raw!r}")
    return f"{path.replace(chr(92), '/')}::{scope}"


def _stable_case_id(nodeid: str, namespace: str = CASE_NAMESPACE) -> str:
    return _sha256(f"{namespace}\0{nodeid}")


def _stable_execution_member_id(case_id: str, cell: str) -> str:
    return _sha256(f"{case_id}\0{cell}")


def _unique(values: Sequence[str], *, label: str) -> list[str]:
    normalized = [str(value).strip() for value in values]
    if any(not value for value in normalized):
        raise AssuranceError(f"empty {label}")
    if len(normalized) != len(set(normalized)):
        raise AssuranceError(f"duplicate {label}")
    return sorted(normalized)


def build_collection_manifest(
    nodeids: Sequence[str],
    cells: Sequence[str],
    source_commit: str,
    *,
    collection_command: str = DEFAULT_COLLECTION_COMMAND,
) -> dict[str, object]:
    """把当前候选的真实 pytest collection 绑定到一个或多个运行 cell。"""
    normalized_nodeids = [_normalize_nodeid(nodeid) for nodeid in nodeids]
    if not normalized_nodeids:
        raise AssuranceError("pytest collection produced no test nodeids")
    if len(normalized_nodeids) != len(set(normalized_nodeids)):
        raise AssuranceError("duplicate collected nodeid")
    normalized_nodeids.sort()
    normalized_cells = _unique(cells, label="execution cell")
    commit = source_commit.strip()
    command = collection_command.strip()
    if not commit or not command:
        raise AssuranceError("collection identity is incomplete")

    case_nodeids = {
        _stable_case_id(nodeid): nodeid for nodeid in normalized_nodeids
    }
    case_ids = sorted(case_nodeids)
    execution_member_ids = sorted(
        _stable_execution_member_id(case_id, cell)
        for cell in normalized_cells
        for case_id in case_ids
    )
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "namespace": CASE_NAMESPACE,
        "source_commit": commit,
        "collection_command": command,
        "case_ids": case_ids,
        "case_nodeids": case_nodeids,
        "cells": normalized_cells,
        "execution_member_ids": execution_member_ids,
    }
    manifest["manifest_digest"] = _canonical_digest(manifest, "manifest_digest")
    return manifest


def _candidate_execution_failure(
    reason: str,
    *,
    candidate_commit: str,
    runner_seconds: float = 0.0,
    late_red: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": AGGREGATE_SCHEMA,
        "status": "failed",
        "reason": reason,
        "candidate_commit": candidate_commit,
        "runner_seconds": runner_seconds,
        "late_red": late_red,
    }


def _validate_candidate_manifest(
    manifest: Mapping[str, object],
    *,
    expected_cell: str,
    expected_commit: str,
) -> str | None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        return "manifest_schema_invalid"
    if manifest.get("namespace") != CASE_NAMESPACE:
        return "namespace_mismatch"
    if manifest.get("source_commit") != expected_commit:
        return "candidate_commit_mismatch"
    if not str(manifest.get("collection_command", "")).strip():
        return "collection_command_missing"
    cells = list(manifest.get("cells", []))
    if cells != [expected_cell]:
        return "manifest_cell_cardinality_invalid"
    raw_case_ids = list(manifest.get("case_ids", []))
    case_ids = {str(value) for value in raw_case_ids}
    if not case_ids or len(case_ids) != len(raw_case_ids):
        return "runtime_case_identity_invalid"
    raw_nodeids = manifest.get("case_nodeids")
    if not isinstance(raw_nodeids, Mapping) or set(raw_nodeids) != case_ids:
        return "runtime_case_identity_invalid"
    try:
        normalized_nodeids = {
            str(case_id): _normalize_nodeid(str(nodeid))
            for case_id, nodeid in raw_nodeids.items()
        }
    except AssuranceError:
        return "runtime_case_identity_invalid"
    if any(
        _stable_case_id(nodeid) != case_id
        for case_id, nodeid in normalized_nodeids.items()
    ):
        return "runtime_case_identity_invalid"
    expected_members = {
        _stable_execution_member_id(case_id, expected_cell) for case_id in case_ids
    }
    raw_members = list(manifest.get("execution_member_ids", []))
    if len(raw_members) != len(set(raw_members)) or set(raw_members) != expected_members:
        return "execution_member_set_invalid"
    if manifest.get("manifest_digest") != _canonical_digest(
        manifest, "manifest_digest"
    ):
        return "manifest_digest_invalid"
    return None


def _runner_seconds(evidence: Sequence[Mapping[str, object]]) -> float:
    total = 0.0
    for item in evidence:
        raw = item.get("duration_seconds", 0.0)
        if isinstance(raw, bool):
            raise AssuranceError("invalid evidence duration")
        try:
            duration = float(raw)
        except (TypeError, ValueError) as exc:
            raise AssuranceError("invalid evidence duration") from exc
        if duration < 0:
            raise AssuranceError("invalid evidence duration")
        total += duration
    return total


def verify_candidate_execution(
    expected_cells: Sequence[str],
    manifests: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    *,
    candidate_commit: str,
    fast_gate_status: str,
) -> dict[str, object]:
    """验证候选自身收集的测试在每个配置 cell 中完整执行。"""
    expected = [str(cell) for cell in expected_cells]
    if (
        not expected
        or len(expected) != len(set(expected))
        or any(not cell for cell in expected)
    ):
        return _candidate_execution_failure(
            "expected_cell_contract_invalid",
            candidate_commit=candidate_commit,
        )

    manifest_cells: list[str] = []
    for manifest in manifests:
        cells = list(manifest.get("cells", []))
        if len(cells) != 1:
            return _candidate_execution_failure(
                "manifest_cell_cardinality_invalid",
                candidate_commit=candidate_commit,
            )
        manifest_cells.append(str(cells[0]))
    if len(manifest_cells) != len(set(manifest_cells)):
        return _candidate_execution_failure(
            "duplicate_cell_manifest",
            candidate_commit=candidate_commit,
        )
    if set(manifest_cells) != set(expected):
        return _candidate_execution_failure(
            "cell_set_mismatch",
            candidate_commit=candidate_commit,
        )
    manifests_by_cell = dict(zip(manifest_cells, manifests, strict=True))
    for cell in expected:
        reason = _validate_candidate_manifest(
            manifests_by_cell[cell],
            expected_cell=cell,
            expected_commit=candidate_commit,
        )
        if reason is not None:
            return _candidate_execution_failure(
                reason,
                candidate_commit=candidate_commit,
            )

    evidence_cells = [str(item.get("cell", "")) for item in evidence]
    try:
        runner_seconds = _runner_seconds(evidence)
    except AssuranceError:
        return _candidate_execution_failure(
            "evidence_duration_invalid",
            candidate_commit=candidate_commit,
        )
    any_failed = any(item.get("status") != "success" for item in evidence)
    late_red = fast_gate_status == "success" and any_failed
    if len(evidence_cells) != len(set(evidence_cells)):
        return _candidate_execution_failure(
            "duplicate_cell_evidence",
            candidate_commit=candidate_commit,
            runner_seconds=runner_seconds,
            late_red=late_red,
        )
    if set(evidence_cells) != set(expected):
        return _candidate_execution_failure(
            "cell_set_mismatch",
            candidate_commit=candidate_commit,
            runner_seconds=runner_seconds,
            late_red=late_red,
        )
    evidence_by_cell = dict(zip(evidence_cells, evidence, strict=True))
    for cell in expected:
        item = evidence_by_cell[cell]
        manifest = manifests_by_cell[cell]
        if item.get("schema_version") != CELL_EVIDENCE_SCHEMA:
            reason = "evidence_schema_invalid"
        elif item.get("source_commit") != candidate_commit:
            reason = "candidate_commit_mismatch"
        elif item.get("collection_manifest_digest") != manifest.get(
            "manifest_digest"
        ):
            reason = "manifest_evidence_mismatch"
        elif list(item.get("duplicate_testcases", [])):
            reason = "duplicate_testcase"
        elif item.get("executed_count") != item.get(
            "collected_count"
        ) or item.get("collected_count") != len(
            list(manifest.get("case_ids", []))
        ):
            reason = "execution_count_mismatch"
        elif any(
            int(item.get(key, 0)) for key in ("failures", "errors")
        ) or item.get("status") != "success":
            reason = "non_success_cell"
        else:
            raw_skips = item.get("skipped_case_ids")
            skipped = item.get("skipped")
            if (
                not isinstance(raw_skips, list)
                or not isinstance(skipped, int)
                or isinstance(skipped, bool)
                or skipped < 0
                or len(raw_skips) != len(set(raw_skips))
                or skipped != len(raw_skips)
                or not set(str(value) for value in raw_skips).issubset(
                    set(str(value) for value in manifest.get("case_ids", []))
                )
            ):
                reason = "skip_evidence_invalid"
            else:
                reason = None
        if reason is not None:
            return _candidate_execution_failure(
                reason,
                candidate_commit=candidate_commit,
                runner_seconds=runner_seconds,
                late_red=late_red,
            )

    case_ids = {
        str(case_id)
        for manifest in manifests
        for case_id in manifest.get("case_ids", [])
    }
    report: dict[str, object] = {
        "schema_version": AGGREGATE_SCHEMA,
        "status": "success",
        "reason": "complete",
        "candidate_commit": candidate_commit,
        "cells": sorted(expected),
        "case_count": len(case_ids),
        "execution_member_count": sum(
            len(list(manifest.get("execution_member_ids", [])))
            for manifest in manifests
        ),
        "runner_seconds": runner_seconds,
        "late_red": False,
    }
    report["report_digest"] = _canonical_digest(report, "report_digest")
    return report


def _duration_seconds(started_at: str, finished_at: str) -> float:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssuranceError("invalid evidence timestamp") from exc
    duration = (finished - started).total_seconds()
    if duration < 0:
        raise AssuranceError("evidence finish precedes start")
    return duration


def _cell_evidence_base(
    manifest: Mapping[str, object],
    *,
    cell: str,
    source_commit: str,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    return {
        "schema_version": CELL_EVIDENCE_SCHEMA,
        "cell": cell,
        "source_commit": source_commit,
        "collection_manifest_digest": str(manifest.get("manifest_digest", "")),
        "collected_count": len(list(manifest.get("case_ids", []))),
        "executed_count": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "skipped_case_ids": [],
        "duplicate_testcases": [],
        "status": "failed",
        "reason": "unknown",
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": _duration_seconds(started_at, finished_at),
    }


def _junit_key_from_nodeid(nodeid: str) -> tuple[str, str]:
    normalized = _normalize_nodeid(nodeid)
    scope, parameter_marker, parameter_id = normalized.partition("[")
    parts = scope.split("::")
    module_name = parts[0].removesuffix(".py").replace("/", ".")
    test_name = parts[-1] + (f"[{parameter_id}" if parameter_marker else "")
    return ".".join([module_name, *parts[1:-1]]), test_name


def _junit_case_lookup(manifest: Mapping[str, object]) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    raw_nodeids = manifest.get("case_nodeids")
    if not isinstance(raw_nodeids, Mapping):
        raise AssuranceError("manifest case nodeids are missing")
    for raw_case_id, raw_nodeid in raw_nodeids.items():
        key = _junit_key_from_nodeid(str(raw_nodeid))
        if key in lookup:
            raise AssuranceError("ambiguous JUnit case identity")
        lookup[key] = str(raw_case_id)
    return lookup


def build_cell_evidence(
    manifest: Mapping[str, object],
    junit_path: Path,
    *,
    cell: str,
    source_commit: str,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    """把一个候选 cell 的 collection 与 JUnit 收敛为执行终态。"""
    try:
        evidence = _cell_evidence_base(
            manifest,
            cell=cell,
            source_commit=source_commit,
            started_at=started_at,
            finished_at=finished_at,
        )
    except AssuranceError:
        return {
            "schema_version": CELL_EVIDENCE_SCHEMA,
            "cell": cell,
            "source_commit": source_commit,
            "status": "failed",
            "reason": "timestamp_invalid",
            "duration_seconds": 0.0,
        }
    manifest_reason = _validate_candidate_manifest(
        manifest,
        expected_cell=cell,
        expected_commit=source_commit,
    )
    if manifest_reason is not None:
        evidence["reason"] = manifest_reason
        return evidence
    if not junit_path.is_file():
        evidence["reason"] = "junit_missing"
        return evidence
    if junit_path.stat().st_size == 0:
        evidence["reason"] = "junit_empty"
        return evidence
    try:
        root = ET.parse(junit_path).getroot()
    except (ET.ParseError, OSError):
        evidence["reason"] = "junit_corrupt"
        return evidence
    if root.tag not in {"testsuite", "testsuites"}:
        evidence["reason"] = "junit_root_invalid"
        return evidence

    testcases = list(root.iter("testcase"))
    testcase_keys = [
        (testcase.get("classname", ""), testcase.get("name", ""))
        for testcase in testcases
    ]
    duplicates = sorted(
        "::".join(key)
        for key in set(testcase_keys)
        if testcase_keys.count(key) > 1
    )
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    failures = sum(1 for testcase in testcases if testcase.find("failure") is not None)
    errors = sum(1 for testcase in testcases if testcase.find("error") is not None)
    skipped = sum(1 for testcase in testcases if testcase.find("skipped") is not None)
    declared_failures = sum(int(suite.get("failures", "0") or 0) for suite in suites)
    declared_errors = sum(int(suite.get("errors", "0") or 0) for suite in suites)
    declared_skipped = sum(int(suite.get("skipped", "0") or 0) for suite in suites)
    declared_tests = sum(int(suite.get("tests", "0") or 0) for suite in suites)
    evidence.update(
        {
            "executed_count": len(testcases),
            "failures": max(failures, declared_failures),
            "errors": max(errors, declared_errors),
            "skipped": max(skipped, declared_skipped),
            "duplicate_testcases": duplicates,
        }
    )
    if duplicates:
        evidence["reason"] = "duplicate_testcase"
        return evidence
    if any((evidence["failures"], evidence["errors"])):
        evidence["reason"] = "non_success_terminal_state"
        return evidence
    if evidence["executed_count"] != evidence["collected_count"]:
        evidence["reason"] = "execution_count_mismatch"
        return evidence
    if declared_tests != evidence["executed_count"]:
        evidence["reason"] = "junit_declared_count_mismatch"
        return evidence

    try:
        lookup = _junit_case_lookup(manifest)
        executed_case_ids = [lookup[key] for key in testcase_keys]
    except (AssuranceError, KeyError):
        evidence["reason"] = "junit_case_identity_mismatch"
        return evidence
    manifest_case_ids = set(str(value) for value in manifest.get("case_ids", []))
    if set(executed_case_ids) != manifest_case_ids:
        evidence["reason"] = "junit_case_set_mismatch"
        return evidence
    evidence["skipped_case_ids"] = sorted(
        lookup[key]
        for key, testcase in zip(testcase_keys, testcases, strict=True)
        if testcase.find("skipped") is not None
    )
    evidence["status"] = "success"
    evidence["reason"] = "complete"
    return evidence


def _collect_nodeids(root: Path, pytest_args: Sequence[str]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        *pytest_args,
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssuranceError(
            f"pytest collection failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    nodeids = [line.strip() for line in completed.stdout.splitlines() if "::" in line]
    if not nodeids:
        raise AssuranceError("pytest collection produced no test nodeids")
    return nodeids


def _git_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not commit:
        raise AssuranceError("unable to resolve candidate commit")
    return commit


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssuranceError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise AssuranceError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--root", type=Path, default=Path.cwd())
    collect.add_argument("--source-commit")
    collect.add_argument("--cell", action="append", required=True)
    collect.add_argument("--pytest-arg", action="append", default=[])
    collect.add_argument("--output", type=Path, required=True)

    cell_evidence = subparsers.add_parser("cell-evidence")
    cell_evidence.add_argument("--manifest", type=Path, required=True)
    cell_evidence.add_argument("--junit", type=Path, required=True)
    cell_evidence.add_argument("--cell", required=True)
    cell_evidence.add_argument("--source-commit", required=True)
    cell_evidence.add_argument("--started-at", required=True)
    cell_evidence.add_argument("--finished-at", required=True)
    cell_evidence.add_argument("--output", type=Path, required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--evidence-root", type=Path, required=True)
    aggregate.add_argument("--expected-cell", action="append", required=True)
    aggregate.add_argument("--candidate-commit", required=True)
    aggregate.add_argument("--fast-gate-status", default="unknown")
    aggregate.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "collect":
            root = args.root.resolve()
            source_commit = args.source_commit or os.environ.get("GITHUB_SHA")
            source_commit = source_commit or _git_commit(root)
            result = build_collection_manifest(
                _collect_nodeids(root, args.pytest_arg),
                args.cell,
                source_commit,
            )
        elif args.command == "cell-evidence":
            result = build_cell_evidence(
                _read_json(args.manifest),
                args.junit,
                cell=args.cell,
                source_commit=args.source_commit,
                started_at=args.started_at,
                finished_at=args.finished_at,
            )
        else:
            evidence = [
                _read_json(path)
                for path in sorted(args.evidence_root.rglob("cell-evidence.json"))
            ]
            manifests = [
                _read_json(path)
                for path in sorted(
                    args.evidence_root.rglob("collection-manifest.json")
                )
            ]
            result = verify_candidate_execution(
                args.expected_cell,
                manifests,
                evidence,
                candidate_commit=args.candidate_commit,
                fast_gate_status=args.fast_gate_status,
            )
        _write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status", "success") == "success" else 1
    except AssuranceError as exc:
        print(f"ci static assurance failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""验证当前候选的 pytest collection、JUnit 终态和 CI cell 完整性。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

CASE_NAMESPACE = "pytest-nodeid-v1"
MANIFEST_SCHEMA = "ci-test-manifest-v1"
CELL_EVIDENCE_SCHEMA = "ci-cell-evidence-v1"
AGGREGATE_SCHEMA = "ci-assurance-report-v1"
DEFAULT_COLLECTION_COMMAND = "pytest --collect-only -q"
RELEASE_CELLS = (
    "ubuntu-latest-py3.11", "macos-latest-py3.11", "windows-latest-py3.11",
    "ubuntu-latest-py3.12", "ubuntu-latest-py3.13", "ubuntu-latest-py3.14",
    "windows-latest-py3.14",
)
PRIMARY_CELL = "ubuntu-latest-py3.11"
PRIMARY_PREVIOUS_FILES = (
    "collection-manifest.json", "compatibility-results.xml", "started-at.txt",
    "finished-at.txt", "previous-result.json",
)
PRIMARY_REPAIR_TESTS = (
    "tests/unit/test_quality_command.py",
    "tests/unit/test_pr_review_provider.py",
    "tests/unit/test_counterexample_execution.py",
    "tests/unit/test_implementation_loop.py",
    "tests/unit/test_ci_static_assurance.py",
    "tests/unit/test_ci_candidate_execution.py",
    "tests/integration/test_github_workflows.py",
    "tests/unit/test_release_identity.py",
    "tests/architecture/test_removed_review_subsystems.py",
    "tests/unit/test_verify_constraints.py",
)
PRIMARY_SHARED_TESTS = frozenset(PRIMARY_REPAIR_TESTS[:3])
PRIMARY_REUSE_PATHS = frozenset((*PRIMARY_SHARED_TESTS,
    "tests/unit/test_ci_static_assurance.py",
    "tests/integration/test_github_workflows.py",
    "scripts/ci_static_assurance.py",
    ".github/ci/fast-gate-tests.txt",
    ".github/workflows/compatibility-gate.yml",
    ".github/workflows/release-build.yml",
    ".github/workflows/release-artifact-smoke.yml",
    "docs/pull-request-checklist.zh.md",
    "docs/框架自迭代开发与发布约定.md",
))
PRIMARY_REPAIR_SELECTION = '''  if [[ -f "ci-evidence/${CELL}/fresh-manifest.json" ]]; then
    test_args=(tests/unit/test_quality_command.py tests/unit/test_pr_review_provider.py
               tests/unit/test_counterexample_execution.py tests/unit/test_implementation_loop.py
               tests/unit/test_ci_static_assurance.py tests/unit/test_ci_candidate_execution.py
               tests/integration/test_github_workflows.py tests/unit/test_release_identity.py
               tests/architecture/test_removed_review_subsystems.py tests/unit/test_verify_constraints.py)
  fi
'''


class AssuranceError(ValueError):
    """候选测试证据不完整。"""


def release_assurance_run_id(value: str, body: str) -> int:
    markers = re.findall(r"<!-- ai-sdlc-assurance-run: ([1-9][0-9]*) -->", body)
    if not value and len(markers) == 1:
        value = markers[0]
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise AssuranceError("release requires one explicit assurance run ID")
    return int(value)


def verify_release_assurance(
    report: Mapping[str, Any], run: Mapping[str, Any], commit: Mapping[str, Any],
    *, repository: str, run_id: int, release_tree: str,
) -> dict[str, Any]:
    """允许 merge/squash SHA 不同，但发行字节和模式必须等于已验证的 Git tree。"""
    if (
        run.get("id") != run_id
        or run.get("repository", {}).get("full_name") != repository
        or run.get("path") != ".github/workflows/compatibility-gate.yml"
        or run.get("status") != "completed" or run.get("conclusion") != "success"
    ):
        raise AssuranceError("release assurance workflow did not succeed in this repository")
    tested_commit = str(report.get("candidate_commit", ""))
    tested_tree = str(report.get("candidate_tree", ""))
    if (
        not re.fullmatch(r"[0-9a-f]{40}", tested_commit)
        or not re.fullmatch(r"[0-9a-f]{40}", tested_tree)
        or tested_tree != release_tree
        or commit.get("sha") != tested_commit
        or commit.get("tree", {}).get("sha") != tested_tree
    ):
        raise AssuranceError("release tree differs from the tested checkout")
    # PR 实际测试合并提交；该提交须包含 run 的分支 head，不能借用另一份报告。
    run_heads = {tested_commit}
    if run.get("event") == "pull_request":
        run_heads.update(parent.get("sha") for parent in commit.get("parents", []))
    if run.get("head_sha") not in run_heads:
        raise AssuranceError("tested checkout is unrelated to the assurance run")
    if (
        report.get("schema_version") != AGGREGATE_SCHEMA
        or report.get("status") != "success" or report.get("reason") != "complete"
        or report.get("late_red") is not False
        or report.get("cells") != sorted(RELEASE_CELLS)
        or not isinstance(report.get("case_count"), int) or report["case_count"] <= 0
        or not isinstance(report.get("execution_member_count"), int)
        or report["execution_member_count"] < report["case_count"]
        or report.get("report_digest") != _canonical_digest(report, "report_digest")
    ):
        raise AssuranceError("release requires complete, intact layered assurance")
    return {
        "status": "success", "repository": repository, "assurance_run_id": run_id,
        "tested_commit": tested_commit, "release_tree": release_tree,
        "report_digest": report["report_digest"],
    }


def _github_api(endpoint: str) -> bytes:
    try:
        return subprocess.run(
            ["gh", "api", endpoint], check=True, capture_output=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise AssuranceError(f"GitHub assurance request failed: {endpoint}") from exc


def check_release_assurance(root: Path, repository: str, run_id: int) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AssuranceError("invalid release repository")
    prefix = f"repos/{repository}"
    run = json.loads(_github_api(f"{prefix}/actions/runs/{run_id}"))
    # 仅取指定运行的当前 attempt 原件，不接受同名本地文件或历史重跑结果。
    name = f"assurance-report-{run_id}-{run.get('run_attempt')}"
    listing = json.loads(_github_api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100"))
    artifacts = [item for item in listing.get("artifacts", []) if item.get("name") == name]
    if len(artifacts) != 1 or artifacts[0].get("expired") is not False:
        raise AssuranceError("exact assurance artifact is missing or expired")
    artifact = artifacts[0]
    raw = _github_api(f"{prefix}/actions/artifacts/{int(artifact['id'])}/zip")
    if artifact.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise AssuranceError("assurance artifact digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist() != ["assurance-report.json"]:
                raise AssuranceError("unexpected assurance artifact members")
            if archive.getinfo("assurance-report.json").file_size > 2_000_000:
                raise AssuranceError("assurance report is oversized")
            report = json.loads(archive.read("assurance-report.json"))
    except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise AssuranceError("invalid assurance artifact") from exc
    tested_commit = str(report.get("candidate_commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", tested_commit):
        raise AssuranceError("invalid tested commit")
    commit = json.loads(_github_api(f"{prefix}/git/commits/{tested_commit}"))
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    result = verify_release_assurance(
        report, run, commit, repository=repository, run_id=run_id, release_tree=tree,
    )
    result.update(artifact_id=artifact["id"], artifact_digest=artifact["digest"])
    return result


def _validate_previous_primary(manifest, evidence):
    """失败原件仍是失败；仅完整可归属、且将全部重验的失败成员可以被替换。"""
    commit = manifest.get("source_commit")
    reason = _validate_candidate_manifest(manifest, expected_cell=PRIMARY_CELL, expected_commit=commit)
    if reason:
        raise AssuranceError(f"original primary manifest invalid: {reason}")
    if (evidence.get("schema_version") != CELL_EVIDENCE_SCHEMA
        or evidence.get("cell") != PRIMARY_CELL or evidence.get("source_commit") != commit
        or evidence.get("collection_manifest_digest") != manifest["manifest_digest"]
        or evidence.get("duplicate_testcases") != []
        or evidence.get("collected_count") != len(manifest["case_ids"])
        or evidence.get("executed_count") != len(manifest["case_ids"])):
        raise AssuranceError("original primary execution is incomplete or mismatched")
    _runner_seconds([evidence])
    terminal_sets = []
    for count, key in (("failures", "failed_case_ids"), ("errors", "error_case_ids"),
                       ("skipped", "skipped_case_ids")):
        members = evidence.get(key, [] if count != "skipped" else None)
        number = evidence.get(count)
        if (not isinstance(members, list) or type(number) is not int or number < 0
            or len(members) != len(set(members)) or number != len(members)
            or not set(members).issubset(manifest["case_ids"])):
            raise AssuranceError("original primary terminal identities are invalid")
        terminal_sets.append(set(members))
    if any(terminal_sets[a] & terminal_sets[b] for a, b in ((0, 1), (0, 2), (1, 2))):
        raise AssuranceError("original primary terminal identities overlap")
    failed = terminal_sets[0] | terminal_sets[1]
    expected = ("failed", "non_success_terminal_state") if failed else ("success", "complete")
    if (evidence.get("status"), evidence.get("reason")) != expected:
        raise AssuranceError("original primary terminal state is not trustworthy")
    if any(manifest["case_nodeids"][key].split("::", 1)[0] not in PRIMARY_REPAIR_TESTS for key in failed):
        raise AssuranceError("original primary failure is outside the fresh repair scope")


def reuse_primary_evidence(
    current_manifest, previous_manifest, previous_evidence, fresh_manifest, fresh_evidence,
    *, candidate_commit: str, changed_paths: Sequence[str],
) -> dict[str, Any]:
    """组合当前受影响集合与旧未变成员；原失败及其身份保留在来源记录中。"""
    if set(changed_paths) - PRIMARY_REUSE_PATHS:
        raise AssuranceError("primary reuse includes changed runtime or test inputs")
    _validate_previous_primary(previous_manifest, previous_evidence)
    verified = verify_candidate_execution(
        [PRIMARY_CELL], [fresh_manifest], [fresh_evidence],
        candidate_commit=candidate_commit, fast_gate_status="success",
    )
    if verified["status"] != "success":
        raise AssuranceError(f"primary fresh repair failed: {verified['reason']}")
    reason = _validate_candidate_manifest(
        current_manifest, expected_cell=PRIMARY_CELL, expected_commit=candidate_commit,
    )
    if reason:
        raise AssuranceError(f"primary current collection invalid: {reason}")
    def affected(nodeid):
        return nodeid.split("::", 1)[0] in PRIMARY_REPAIR_TESTS

    current = current_manifest["case_nodeids"]
    previous = previous_manifest["case_nodeids"]
    fresh = fresh_manifest["case_nodeids"]
    old_failures = set(previous_evidence.get("failed_case_ids", [])) | set(previous_evidence.get("error_case_ids", []))
    if any(fresh.get(key) != previous[key] for key in old_failures):
        raise AssuranceError("original failed members must be rerun without removal or renaming")
    expected_fresh = {key: node for key, node in current.items() if affected(node)}
    unchanged = {key: node for key, node in current.items() if not affected(node)}
    old_unchanged = {key: node for key, node in previous.items() if not affected(node)}
    if not fresh or fresh != expected_fresh or unchanged != old_unchanged:
        raise AssuranceError("primary reuse member union is incomplete or changed")
    skipped = sorted(
        (set(previous_evidence["skipped_case_ids"]) & set(unchanged))
        | set(fresh_evidence["skipped_case_ids"])
    )
    result = dict(fresh_evidence)
    result.update(
        collection_manifest_digest=current_manifest["manifest_digest"],
        collected_count=len(current), executed_count=len(current),
        skipped=len(skipped), skipped_case_ids=skipped,
        skip_reasons={key: value for origin in (previous_evidence, fresh_evidence)
                      for key, value in origin.get("skip_reasons", {}).items() if key in skipped},
        duration_seconds=previous_evidence["duration_seconds"] + fresh_evidence["duration_seconds"],
        provenance={
            "previous": {"source_commit": previous_manifest["source_commit"],
                         "manifest_digest": previous_manifest["manifest_digest"],
                         "original_status": previous_evidence["status"],
                         "original_failed_case_ids": previous_evidence.get("failed_case_ids", []),
                         "original_error_case_ids": previous_evidence.get("error_case_ids", []),
                         "skipped_case_ids": sorted(set(previous_evidence["skipped_case_ids"]) & set(unchanged)),
                         "case_ids": sorted(unchanged)},
            "fresh": {"source_commit": candidate_commit,
                      "manifest_digest": fresh_manifest["manifest_digest"],
                      "case_ids": sorted(fresh)},
        },
    )
    return result


def prepare_primary_reuse(root: Path, repository: str, run_id: int, baseline: str, output: Path):
    """固定原件的受限复用；运行输入改变时恢复普通完整执行。"""
    import yaml

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise AssuranceError("invalid primary repository")
    if not re.fullmatch(r"[0-9a-f]{40}", baseline):
        raise AssuranceError("invalid primary baseline commit")
    if (output / "fresh-manifest.json").exists() or (output / "reuse-plan.json").exists():
        raise AssuranceError("primary preparation requires a fresh evidence directory")
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=root).returncode:
        raise AssuranceError("primary preparation requires committed test inputs")
    if subprocess.run(["git", "cat-file", "-e", baseline], cwd=root, capture_output=True).returncode:
        subprocess.run(["git", "fetch", "--no-tags", "--depth=1", f"https://github.com/{repository}.git", baseline],
                       cwd=root, check=True, capture_output=True, timeout=60)
    changes = subprocess.check_output(
        ["git", "-c", "core.quotepath=false", "diff", "--raw", "--no-abbrev", "--no-renames", baseline, "HEAD"],
        cwd=root, text=True,
    ).splitlines()
    paths = []
    for change in changes:
        metadata, path = change.split("\t", 1)
        fields = metadata.split()
        # 模式变化、删除/新增、源代码和全局 fixture/依赖变化均不进入本次复用。
        if fields[0][1:] != fields[1] or fields[4] != "M" or path not in PRIMARY_REUSE_PATHS:
            return {"status": "full_required", "reason": "inputs_changed"}
        paths.append(path)
    for path in set(paths) & PRIMARY_SHARED_TESTS:
        def shared_inputs(source):
            tree = ast.parse(source)
            # 仅顶层测试函数可变；导入、fixture、共享 helper 和模块初始化必须保持。
            tree.body = [node for node in tree.body if not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                and not any("fixture" in ast.unparse(item) for item in node.decorator_list)
            )]
            return ast.dump(tree, include_attributes=False)
        before = subprocess.check_output(["git", "show", f"{baseline}:{path}"], cwd=root)
        if shared_inputs(before) != shared_inputs((root / path).read_bytes()):
            return {"status": "full_required", "reason": "shared_test_inputs_changed"}
    workflow_path = ".github/workflows/compatibility-gate.yml"
    old = yaml.safe_load(subprocess.check_output(["git", "show", f"{baseline}:{workflow_path}"], cwd=root))
    new = yaml.safe_load((root / workflow_path).read_text(encoding="utf-8"))
    def execution_inputs(workflow):
        job = workflow["jobs"]["cross-platform-validation"]
        steps = []
        for original_step in job["steps"]:
            step = dict(original_step)
            if step.get("name") == "Prepare unchanged primary evidence":
                continue
            if step.get("name") == "Run selected pytest suite":
                # 只剥离固定受影响文件的串行重验；旧未变成员的所有执行输入须相同。
                step["run"] = step["run"].replace(PRIMARY_REPAIR_SELECTION, "")
            steps.append(step)
        return (workflow.get("env"), workflow.get("defaults"), job.get("defaults"),
                job["runs-on"], job["strategy"]["matrix"],
                {key: value for key, value in job["env"].items() if not key.startswith("PRIMARY_REUSE_")},
                steps)
    if execution_inputs(old) != execution_inputs(new):
        return {"status": "full_required", "reason": "execution_inputs_changed"}
    prefix = f"repos/{repository}"
    run = json.loads(_github_api(f"{prefix}/actions/runs/{run_id}"))
    jobs = json.loads(_github_api(f"{prefix}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100"))
    primary = [job for job in jobs.get("jobs", [])
               if job.get("name") == "Cross Platform Validation (ubuntu-latest, Python 3.11)"]
    if (run.get("repository", {}).get("full_name") != repository
        or run.get("path") != workflow_path or run.get("run_attempt") != 1
        or len(primary) != 1 or primary[0].get("status") != "completed"
        or primary[0].get("conclusion") not in {"success", "failure"}):
        raise AssuranceError("original primary full job has not succeeded")
    if primary[0]["conclusion"] == "failure":
        steps = primary[0].get("steps", [])
        required = {"Checkout candidate", "Set up Python", "Install uv", "Sync dependencies",
                    "Collect exact candidate members", "Doctor", "Record raw cell completion",
                    "Upload compatibility evidence"}
        successful = {step.get("name") for step in steps if step.get("conclusion") == "success"}
        failures = [step.get("name") for step in steps if step.get("conclusion") == "failure"]
        allowed_skips = {"Run fixed SnapshotControl stability sentinel", "Post Install uv", "Post Set up Python"}
        if (not required.issubset(successful) or failures != ["Run selected pytest suite"]
            or any(step.get("conclusion") not in {"success", "failure"}
                   and not (step.get("conclusion") == "skipped" and step.get("name") in allowed_skips)
                   for step in steps)):
            raise AssuranceError("original primary full job has not succeeded: non-pytest failure")
    items = json.loads(_github_api(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100"))
    artifacts = [item for item in items.get("artifacts", [])
                 if item.get("name") == f"compatibility-{PRIMARY_CELL}" and item.get("expired") is False]
    if len(artifacts) != 1:
        raise AssuranceError("original primary artifact missing")
    artifact = artifacts[0]
    raw = _github_api(f"{prefix}/actions/artifacts/{int(artifact['id'])}/zip")
    if artifact.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise AssuranceError("original primary artifact digest mismatch")
    previous = output / "previous"
    previous.mkdir(parents=True, exist_ok=True)
    (previous / "artifact.zip").write_bytes(raw)
    members = {"collection-manifest.json", "compatibility-results.xml", "started-at.txt", "finished-at.txt"}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if set(archive.namelist()) != members or len(archive.namelist()) != len(members):
            raise AssuranceError("unexpected primary artifact members")
        for name in members:
            (previous / name).write_bytes(archive.read(name))
    manifest = _read_json(previous / "collection-manifest.json")
    if manifest.get("source_commit") != baseline or manifest.get("collection_command") != DEFAULT_COLLECTION_COMMAND:
        raise AssuranceError("original primary collection identity mismatch")
    evidence = build_cell_evidence(
        manifest, previous / "compatibility-results.xml", cell=PRIMARY_CELL,
        source_commit=baseline, started_at=(previous / "started-at.txt").read_text().strip(),
        finished_at=(previous / "finished-at.txt").read_text().strip(),
    )
    _validate_previous_primary(manifest, evidence)
    if (evidence["status"] == "success") != (primary[0]["conclusion"] == "success"):
        raise AssuranceError("original primary job and JUnit terminal states disagree")
    _write_json(previous / "previous-result.json", evidence)
    fresh = build_collection_manifest(_collect_nodeids(root, PRIMARY_REPAIR_TESTS), [PRIMARY_CELL],
                                      _git_commit(root), collection_command=shlex.join(
                                          ["pytest", "--collect-only", "-q", *PRIMARY_REPAIR_TESTS]))
    old_failures = set(evidence.get("failed_case_ids", [])) | set(evidence.get("error_case_ids", []))
    if any(fresh["case_nodeids"].get(key) != manifest["case_nodeids"][key] for key in old_failures):
        raise AssuranceError("original failed members must be rerun without removal or renaming")
    _write_json(output / "fresh-manifest.json", fresh)
    return {"status": "reuse_eligible", "baseline_commit": baseline, "run_id": run_id,
            "candidate_commit": _git_commit(root),
            "changed_paths": paths, "artifact": artifact, "job": primary[0],
            "previous_file_digests": {name: "sha256:" + hashlib.sha256((previous / name).read_bytes()).hexdigest()
                                      for name in PRIMARY_PREVIOUS_FILES},
            "previous_manifest_digest": manifest["manifest_digest"]}


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
        "failed_case_ids": [],
        "error_case_ids": [],
        "skip_reasons": {},
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
    allowed_children = {"failure", "error", "skipped", "system-out", "system-err", "properties"}
    if any(any(child.tag not in allowed_children for child in case)
           or sum(len(case.findall(tag)) for tag in ("failure", "error", "skipped")) > 1
           for case in testcases):
        evidence["reason"] = "junit_terminal_state_invalid"
        return evidence
    failures = sum(1 for testcase in testcases if testcase.find("failure") is not None)
    errors = sum(1 for testcase in testcases if testcase.find("error") is not None)
    skipped = sum(1 for testcase in testcases if testcase.find("skipped") is not None)
    try:
        declared_failures = sum(int(suite.get("failures", "0") or 0) for suite in suites)
        declared_errors = sum(int(suite.get("errors", "0") or 0) for suite in suites)
        declared_skipped = sum(int(suite.get("skipped", "0") or 0) for suite in suites)
        declared_tests = sum(int(suite.get("tests", "0") or 0) for suite in suites)
    except ValueError:
        evidence["reason"] = "junit_declared_count_mismatch"
        return evidence
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
    if evidence["executed_count"] != evidence["collected_count"]:
        evidence["reason"] = "execution_count_mismatch"
        return evidence
    if declared_tests != evidence["executed_count"]:
        evidence["reason"] = "junit_declared_count_mismatch"
        return evidence
    if (failures, errors, skipped) != (declared_failures, declared_errors, declared_skipped):
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
    for tag, field in (("failure", "failed_case_ids"), ("error", "error_case_ids"), ("skipped", "skipped_case_ids")):
        evidence[field] = sorted(lookup[key] for key, case in zip(testcase_keys, testcases, strict=True)
                                 if case.find(tag) is not None)
    evidence["skip_reasons"] = {
        lookup[key]: {"message": skip.get("message", ""), "text": skip.text or "", "type": skip.get("type", "")}
        for key, case in zip(testcase_keys, testcases, strict=True)
        if (skip := case.find("skipped")) is not None
    }
    if failures or errors:
        evidence["reason"] = "non_success_terminal_state"
        return evidence
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
    aggregate.add_argument("--candidate-tree", required=True)
    aggregate.add_argument("--fast-gate-status", default="unknown")
    aggregate.add_argument("--output", type=Path, required=True)

    release = subparsers.add_parser("release-check")
    release.add_argument("--root", type=Path, default=Path.cwd())
    release.add_argument("--repository", required=True)
    release.add_argument("--run-id", default="")
    release.add_argument("--output", type=Path, required=True)

    prepare = subparsers.add_parser("prepare-primary")
    prepare.add_argument("--root", type=Path, default=Path.cwd())
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--run-id", type=int, required=True)
    prepare.add_argument("--baseline-commit", required=True)
    prepare.add_argument("--output", type=Path, required=True)

    combine = subparsers.add_parser("combine-primary")
    combine.add_argument("--evidence-dir", type=Path, required=True)
    combine.add_argument("--candidate-commit", required=True)
    combine.add_argument("--output", type=Path, required=True)
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
                collection_command=shlex.join(["pytest", "--collect-only", "-q", *args.pytest_arg]),
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
        elif args.command == "prepare-primary":
            result = prepare_primary_reuse(args.root, args.repository, args.run_id,
                                           args.baseline_commit, args.output.parent)
        elif args.command == "combine-primary":
            directory = args.evidence_dir
            previous = directory / "previous"
            plan = _read_json(directory / "reuse-plan.json")
            if plan.get("status") != "reuse_eligible":
                raise AssuranceError("primary reuse was not admitted")
            if plan.get("candidate_commit") != args.candidate_commit:
                raise AssuranceError("primary reuse candidate changed after preparation")
            digests = plan.get("previous_file_digests", {})
            if set(digests) != set(PRIMARY_PREVIOUS_FILES):
                raise AssuranceError("primary original file bindings are incomplete")
            originals = {**digests, "artifact.zip": plan.get("artifact", {}).get("digest")}
            for name, digest in originals.items():
                path = previous / name
                if not path.is_file() or "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise AssuranceError("primary original files changed after preparation")
            previous_manifest = _read_json(previous / "collection-manifest.json")
            if (previous_manifest.get("source_commit") != plan.get("baseline_commit")
                or previous_manifest.get("manifest_digest") != plan.get("previous_manifest_digest")):
                raise AssuranceError("primary original source changed after preparation")
            result = reuse_primary_evidence(
                _read_json(directory / "collection-manifest.json"),
                previous_manifest,
                _read_json(previous / "previous-result.json"),
                _read_json(directory / "fresh-manifest.json"),
                _read_json(directory / "fresh-result.json"),
                candidate_commit=args.candidate_commit, changed_paths=plan["changed_paths"],
            )
        elif args.command == "release-check":
            result = check_release_assurance(
                args.root, args.repository,
                release_assurance_run_id(args.run_id, os.environ.get("AI_SDLC_RELEASE_BODY", "")),
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
                if path.parent.name != "previous"
            ]
            result = verify_candidate_execution(
                args.expected_cell,
                manifests,
                evidence,
                candidate_commit=args.candidate_commit,
                fast_gate_status=args.fast_gate_status,
            )
            if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_tree):
                raise AssuranceError("invalid candidate tree")
            result["candidate_tree"] = args.candidate_tree
            result["reuse_provenance"] = {
                item["cell"]: item["provenance"] for item in evidence if "provenance" in item
            }
            result["report_digest"] = _canonical_digest(result, "report_digest")
        _write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if args.command == "prepare-primary":
            return 0
        return 0 if result.get("status", "success") == "success" else 1
    except AssuranceError as exc:
        print(f"ci static assurance failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

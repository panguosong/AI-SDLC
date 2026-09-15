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


def _release_evidence(module):
    tree = "b" * 40
    report = {
        "schema_version": module.AGGREGATE_SCHEMA,
        "status": "success", "reason": "complete", "late_red": False,
        "candidate_commit": _COMMIT, "candidate_tree": tree,
        "cells": sorted(module.RELEASE_CELLS),
        "case_count": 2, "execution_member_count": 14,
    }
    report["report_digest"] = module._canonical_digest(report, "report_digest")
    run = {
        "id": 42, "run_attempt": 1, "status": "completed", "conclusion": "success",
        "path": ".github/workflows/compatibility-gate.yml", "event": "pull_request",
        "head_sha": "c" * 40, "repository": {"full_name": "owner/repo"},
    }
    commit = {"sha": _COMMIT, "tree": {"sha": tree}, "parents": [{"sha": "c" * 40}]}
    return report, run, commit, tree


def test_release_accepts_different_commit_with_identical_tested_tree():
    module = _load_module()
    report, run, commit, tree = _release_evidence(module)
    result = module.verify_release_assurance(
        report, run, commit, repository="owner/repo", run_id=42, release_tree=tree,
    )
    assert result["status"] == "success"
    assert result["tested_commit"] == _COMMIT
    assert result["release_tree"] == tree


@pytest.mark.parametrize("damage", [
    "changed_release_tree", "changed_api_tree", "changed_api_commit", "unrelated_run",
    "failed_run", "running_run", "other_workflow", "other_repository", "other_run",
    "failed_report", "missing_tree", "missing_cell", "duplicate_cell", "empty_tests",
    "tampered_digest",
])
def test_release_rejects_untested_or_invalid_assurance(damage):
    module = _load_module()
    report, run, commit, tree = _release_evidence(module)
    if damage == "changed_release_tree":
        tree = "d" * 40
    elif damage == "changed_api_tree":
        commit["tree"]["sha"] = "d" * 40
    elif damage == "changed_api_commit":
        commit["sha"] = "d" * 40
    elif damage == "unrelated_run":
        commit["parents"] = []
    elif damage == "failed_run":
        run["conclusion"] = "failure"
    elif damage == "running_run":
        run["status"] = "in_progress"
    elif damage == "other_workflow":
        run["path"] = ".github/workflows/fast-only.yml"
    elif damage == "other_repository":
        run["repository"]["full_name"] = "someone/else"
    elif damage == "other_run":
        run["id"] = 43
    elif damage == "failed_report":
        report["status"] = "failed"
    elif damage == "missing_tree":
        del report["candidate_tree"]
    elif damage == "missing_cell":
        report["cells"].pop()
    elif damage == "duplicate_cell":
        report["cells"].append(report["cells"][0])
    elif damage == "empty_tests":
        report["case_count"] = 0
    report["report_digest"] = module._canonical_digest(report, "report_digest")
    if damage == "tampered_digest":
        report["report_digest"] = "sha256:" + "0" * 64
    with pytest.raises(module.AssuranceError):
        module.verify_release_assurance(
            report, run, commit, repository="owner/repo", run_id=42, release_tree=tree,
        )


def test_release_marker_requires_one_unambiguous_positive_run():
    module = _load_module()
    assert module.release_assurance_run_id("42", "") == 42
    assert module.release_assurance_run_id("", "notes\n<!-- ai-sdlc-assurance-run: 42 -->") == 42
    for value, body in [("", ""), ("0", ""), ("bad", ""),
                        ("", "<!-- ai-sdlc-assurance-run: 42 --><!-- ai-sdlc-assurance-run: 43 -->")]:
        with pytest.raises(module.AssuranceError):
            module.release_assurance_run_id(value, body)


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


@pytest.mark.parametrize("scoped", [False, True])
def test_collect_cli_runs_real_pytest_collection(tmp_path: Path, scoped: bool) -> None:
    module = _load_module()
    (tmp_path / "test_sample.py").write_text(
        "def test_real_collection():\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "test_other.py").write_text("def test_other():\n    assert True\n", encoding="utf-8")
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
            *(["--pytest-arg", "test_sample.py"] if scoped else []),
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload["case_nodeids"].values()) == (
        {"test_sample.py::test_real_collection"} if scoped else
        {"test_sample.py::test_real_collection", "test_other.py::test_other"}
    )
    assert payload["collection_command"] == "pytest --collect-only -q" + (
        " test_sample.py" if scoped else ""
    )


def test_cli_exposes_only_candidate_local_commands() -> None:
    module = _load_module()
    parser = module._build_parser()
    subcommands = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )

    assert set(subcommands.choices) == {
        "collect", "cell-evidence", "aggregate", "release-check", "prepare-primary", "combine-primary",
    }


_PRIMARY_REPAIR_NODES = (
    "tests/unit/test_quality_command.py::test_target_signal_constants",
    "tests/unit/test_ci_static_assurance.py::test_original_sources",
    "tests/integration/test_github_workflows.py::test_published_tree",
)
_PRIMARY_UNCHANGED_NODES = (
    "tests/integration/test_business.py::test_original_case",
    "tests/integration/test_business.py::test_platform_skip",
)


def _primary_reuse_case(module, tmp_path, *, fresh_nodes=None, current_unchanged=None):
    previous_commit = "b" * 40
    previous_nodes = (*_PRIMARY_UNCHANGED_NODES,
                      "tests/unit/test_quality_command.py::test_obsolete_fixture",
                      *_PRIMARY_REPAIR_NODES[1:])
    selected_fresh = _PRIMARY_REPAIR_NODES if fresh_nodes is None else fresh_nodes
    unchanged = _PRIMARY_UNCHANGED_NODES if current_unchanged is None else current_unchanged
    current = module.build_collection_manifest(
        [*unchanged, *_PRIMARY_REPAIR_NODES], [_CELL], _COMMIT,
    )

    def source(name, nodeids, commit, skipped, finished):
        manifest = module.build_collection_manifest(nodeids, [_CELL], commit)
        junit = tmp_path / f"{name}.xml"
        _write_junit(junit, cases=[
            (*module._junit_key_from_nodeid(node), "skipped" if node in skipped else None)
            for node in nodeids
        ])
        evidence = module.build_cell_evidence(
            manifest, junit, cell=_CELL, source_commit=commit,
            started_at="2026-09-15T00:00:00Z", finished_at=finished,
        )
        assert evidence["status"] == "success"
        return manifest, evidence

    previous, previous_evidence = source(
        "previous", previous_nodes, previous_commit,
        {_PRIMARY_UNCHANGED_NODES[1], previous_nodes[2]}, "2026-09-15T00:00:02Z",
    )
    fresh, fresh_evidence = source(
        "fresh", selected_fresh, _COMMIT,
        {_PRIMARY_REPAIR_NODES[2]}, "2026-09-15T00:00:03Z",
    )
    return current, previous, previous_evidence, fresh, fresh_evidence


def test_primary_reuse_preserves_both_original_sources_and_exact_current_members(tmp_path):
    module = _load_module()
    inputs = _primary_reuse_case(module, tmp_path)
    current, previous, previous_evidence, fresh, fresh_evidence = inputs
    originals = json.dumps(inputs, sort_keys=True)
    assert set(module.PRIMARY_REPAIR_TESTS) == {
        node.split("::", 1)[0] for node in _PRIMARY_REPAIR_NODES
    }

    result = module.reuse_primary_evidence(
        *inputs, candidate_commit=_COMMIT,
        changed_paths=["tests/unit/test_quality_command.py", "scripts/ci_static_assurance.py"],
    )

    assert result["status"] == "success" and result["source_commit"] == _COMMIT
    assert result["cell"] == _CELL
    assert result["collection_manifest_digest"] == current["manifest_digest"]
    assert result["collected_count"] == result["executed_count"] == 5
    assert result["duration_seconds"] == pytest.approx(5.0)
    for name, manifest in (("previous", previous), ("fresh", fresh)):
        assert result["provenance"][name]["source_commit"] == manifest["source_commit"]
        assert result["provenance"][name]["manifest_digest"] == manifest["manifest_digest"]
    assert set(result["provenance"]["previous"]["case_ids"]) == {
        module._stable_case_id(node) for node in _PRIMARY_UNCHANGED_NODES
    }
    assert result["provenance"]["fresh"]["case_ids"] == fresh["case_ids"]
    assert previous_evidence["source_commit"] != fresh_evidence["source_commit"]
    # 已改测试文件的旧 skip 不得继承；保留未改节点和实际新执行的合法 skip。
    assert result["skipped"] == 2
    assert set(result["skipped_case_ids"]) == {
        module._stable_case_id(_PRIMARY_UNCHANGED_NODES[1]),
        module._stable_case_id(_PRIMARY_REPAIR_NODES[2]),
    }
    report = module.verify_candidate_execution(
        [_CELL], [current], [result], candidate_commit=_COMMIT,
        fast_gate_status="success",
    )
    assert report["status"] == "success"
    assert json.dumps(inputs, sort_keys=True) == originals


def test_primary_cli_aggregates_both_sources_without_counting_previous_twice(tmp_path):
    module = _load_module()
    current, previous, previous_result, fresh, fresh_result = _primary_reuse_case(module, tmp_path)
    directory = tmp_path / f"compatibility-{_CELL}"
    for name, payload in {
        "collection-manifest.json": current, "fresh-manifest.json": fresh,
        "fresh-result.json": fresh_result, "previous/collection-manifest.json": previous,
        "previous/previous-result.json": previous_result,
        "reuse-plan.json": {"status": "reuse_eligible", "changed_paths": [module.PRIMARY_REPAIR_TESTS[0]]},
    }.items():
        module._write_json(directory / name, payload)
    assert module.main([
        "combine-primary", "--evidence-dir", str(directory), "--candidate-commit", _COMMIT,
        "--output", str(directory / "cell-evidence.json"),
    ]) == 0
    report_path = tmp_path / "report.json"
    assert module.main([
        "aggregate", "--evidence-root", str(tmp_path), "--expected-cell", _CELL,
        "--candidate-commit", _COMMIT, "--candidate-tree", "b" * 40,
        "--fast-gate-status", "success", "--output", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text())
    assert report["case_count"] == report["execution_member_count"] == 5
    assert report["candidate_tree"] == "b" * 40
    assert report["reuse_provenance"][_CELL]["previous"]["source_commit"] == previous["source_commit"]
    assert report["report_digest"] == module._canonical_digest(report, "report_digest")


@pytest.mark.parametrize("damage", [
    "previous-failure", "fresh-failure", "missing-fresh-node", "extra-fresh-node",
    "dropped-unchanged-node", "new-unchanged-node", "src-change", "lock-change",
    "previous-source", "fresh-source", "previous-digest", "fresh-digest",
    "previous-count", "fresh-count", "previous-duplicate", "fresh-duplicate",
    "previous-skip-outside", "fresh-skip-outside", "previous-skip-count", "fresh-skip-count",
    "current-manifest-digest", "wrong-primary-cell",
])
def test_primary_reuse_rejects_unproven_sources_or_member_changes(tmp_path, damage):
    module = _load_module()
    overrides = {}
    if damage == "missing-fresh-node":
        overrides["fresh_nodes"] = _PRIMARY_REPAIR_NODES[:-1]
    elif damage == "extra-fresh-node":
        overrides["fresh_nodes"] = (*_PRIMARY_REPAIR_NODES,
                                    "tests/unit/test_quality_command.py::test_uncollected_extra")
    elif damage == "dropped-unchanged-node":
        overrides["current_unchanged"] = _PRIMARY_UNCHANGED_NODES[:-1]
    elif damage == "new-unchanged-node":
        overrides["current_unchanged"] = (*_PRIMARY_UNCHANGED_NODES,
                                          "tests/integration/test_business.py::test_new_unexecuted")
    inputs = _primary_reuse_case(module, tmp_path, **overrides)
    current, _, previous_evidence, _, fresh_evidence = inputs
    changed_paths = ["tests/unit/test_quality_command.py"]
    if damage == "src-change":
        changed_paths.append("src/ai_sdlc/core/quality_command.py")
    elif damage == "lock-change":
        changed_paths.append("uv.lock")
    elif damage == "current-manifest-digest":
        current["manifest_digest"] = "sha256:" + "0" * 64
    elif damage == "wrong-primary-cell":
        current["cells"] = ["windows-latest-py3.11"]
        current["manifest_digest"] = module._canonical_digest(current, "manifest_digest")
    elif damage.startswith(("previous-", "fresh-")):
        name, fault = damage.split("-", 1)
        evidence = previous_evidence if name == "previous" else fresh_evidence
        if fault == "failure":
            evidence.update(status="failed", failures=1)
        elif fault == "source":
            evidence["source_commit"] = "c" * 40
        elif fault == "digest":
            evidence["collection_manifest_digest"] = "sha256:" + "0" * 64
        elif fault == "count":
            evidence["executed_count"] -= 1
        elif fault == "duplicate":
            evidence["duplicate_testcases"] = ["tests.duplicate::test_case"]
        elif fault == "skip-outside":
            evidence["skipped_case_ids"].append(module._stable_case_id("tests/absent.py::test_case"))
            evidence["skipped"] += 1
        elif fault == "skip-count":
            evidence["skipped"] += 1
        else:
            raise AssertionError(fault)

    with pytest.raises(module.AssuranceError):
        module.reuse_primary_evidence(
            *inputs, candidate_commit=_COMMIT, changed_paths=changed_paths,
        )


def _primary_prepare_case(module, tmp_path, monkeypatch):
    import hashlib
    import io
    import subprocess
    import zipfile

    root = tmp_path / "repository"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.PIPE).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    workflow_path = ".github/workflows/compatibility-gate.yml"
    workflow = {"jobs": {"cross-platform-validation": {
        "runs-on": "ubuntu-latest", "env": {"CELL": _CELL},
        "strategy": {"matrix": {"include": [{"python-version": "3.11", "suite": "full"}]}},
        "steps": [
            {"name": "Set up Python", "uses": "actions/setup-python@fixture",
             "with": {"python-version": "3.11"}},
            {"name": "Install uv", "uses": "astral-sh/setup-uv@fixture"},
            {"name": "Sync dependencies", "run": "uv sync --locked"},
            {"name": "Doctor", "run": "uv run ai-sdlc doctor"},
            {"name": "Run selected pytest suite", "shell": "bash", "run": "uv run pytest -q"},
        ],
    }}}
    files = {
        workflow_path: json.dumps(workflow),
        "src/ai_sdlc/core/quality_command.py": "VALUE = 1\n",
        "uv.lock": "version = 1\n",
        **{node.split("::", 1)[0]: f"def {node.split('::', 1)[1]}():\n    pass\n"
           for node in _PRIMARY_REPAIR_NODES},
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "original primary candidate")
    baseline = git("rev-parse", "HEAD")

    def commit(name, content=None, *, mode_change=False):
        path = root / name
        if content is not None:
            path.write_text(content, encoding="utf-8")
        if mode_change:
            path.chmod(0o755)
        git("add", name)
        if mode_change:
            # Windows 也通过真实 Git index/commit 记录模式变化，不向产品传假标志。
            git("update-index", "--chmod=+x", name)
        git("commit", "-qm", "current candidate")
        return git("rev-parse", "HEAD")

    previous = module.build_collection_manifest(_PRIMARY_REPAIR_NODES, [_CELL], baseline)
    junit = tmp_path / "original.xml"
    _write_junit(junit, cases=[(*module._junit_key_from_nodeid(node), None)
                              for node in _PRIMARY_REPAIR_NODES])
    members = {
        "collection-manifest.json": json.dumps(previous).encode(),
        "compatibility-results.xml": junit.read_bytes(),
        "started-at.txt": b"2026-09-15T00:00:00Z\n",
        "finished-at.txt": b"2026-09-15T00:00:02Z\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    archive_bytes = buffer.getvalue()
    prefix = "repos/owner/repo/actions"
    endpoints = {
        "run": f"{prefix}/runs/42",
        "jobs": f"{prefix}/runs/42/attempts/1/jobs?per_page=100",
        "artifacts": f"{prefix}/runs/42/artifacts?per_page=100",
        "zip": f"{prefix}/artifacts/73/zip",
    }
    responses = {
        endpoints["run"]: {"repository": {"full_name": "owner/repo"},
                           "path": workflow_path, "run_attempt": 1},
        endpoints["jobs"]: {"jobs": [{"name": "Cross Platform Validation (ubuntu-latest, Python 3.11)",
                                      "status": "completed", "conclusion": "success"}]},
        endpoints["artifacts"]: {"artifacts": [{"name": f"compatibility-{_CELL}", "id": 73,
            "expired": False, "digest": "sha256:" + hashlib.sha256(archive_bytes).hexdigest()}]},
        endpoints["zip"]: archive_bytes,
    }
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        value = responses[endpoint]
        return value if isinstance(value, bytes) else json.dumps(value).encode()

    def collect(candidate_root, pytest_args):
        assert candidate_root == root
        assert tuple(pytest_args) == module.PRIMARY_REPAIR_TESTS
        return list(_PRIMARY_REPAIR_NODES)

    monkeypatch.setattr(module, "_github_api", api)
    monkeypatch.setattr(module, "_collect_nodeids", collect)
    return root, baseline, commit, workflow, endpoints, responses, calls, members, archive_bytes


def test_prepare_primary_reuse_admits_real_git_test_fix_and_original_archive(tmp_path, monkeypatch):
    module = _load_module()
    root, baseline, commit, _, endpoints, _, calls, members, archive = _primary_prepare_case(
        module, tmp_path, monkeypatch,
    )
    path = module.PRIMARY_REPAIR_TESTS[0]
    current = commit(path, (root / path).read_text() + "# target signal constants\n")
    output = tmp_path / "prepared"

    result = module.prepare_primary_reuse(root, "owner/repo", 42, baseline, output)

    assert result["status"] == "reuse_eligible"
    assert result["baseline_commit"] == baseline and baseline != current
    assert result["changed_paths"] == [path]
    assert calls == list(endpoints.values())
    assert (output / "previous/artifact.zip").read_bytes() == archive
    for name, raw in members.items():
        assert (output / "previous" / name).read_bytes() == raw
    old = json.loads((output / "previous/previous-result.json").read_text())
    fresh = json.loads((output / "fresh-manifest.json").read_text())
    assert old["status"] == "success" and old["source_commit"] == baseline
    assert fresh["source_commit"] == current
    assert set(fresh["case_nodeids"].values()) == set(_PRIMARY_REPAIR_NODES)


@pytest.mark.parametrize("change", [
    "src", "lock", "mode", "python", "dependencies", "env", "pytest-env", "environment-step",
])
def test_prepare_primary_reuse_requires_full_before_artifact_fetch_for_changed_inputs(
    tmp_path, monkeypatch, change,
):
    module = _load_module()
    root, baseline, commit, workflow, _, _, calls, _, _ = _primary_prepare_case(
        module, tmp_path, monkeypatch,
    )
    expected = "inputs_changed"
    if change == "src":
        commit("src/ai_sdlc/core/quality_command.py", "VALUE = 2\n")
    elif change == "lock":
        commit("uv.lock", "version = 2\n")
    elif change == "mode":
        commit(module.PRIMARY_REPAIR_TESTS[0], mode_change=True)
    else:
        job = workflow["jobs"]["cross-platform-validation"]
        if change == "python":
            job["steps"][0]["with"]["python-version"] = "3.12"
        elif change == "dependencies":
            job["steps"][2]["run"] = "uv sync --upgrade"
        elif change == "env":
            job["env"]["PYTHONHASHSEED"] = "1"
        elif change == "pytest-env":
            job["steps"][-1]["env"] = {"PYTHONHASHSEED": "1"}
        elif change == "environment-step":
            job["steps"].insert(-1, {
                "name": "Set pytest environment", "shell": "bash",
                "run": 'echo "PYTHONHASHSEED=1" >> "$GITHUB_ENV"',
            })
        else:
            raise AssertionError(change)
        commit(".github/workflows/compatibility-gate.yml", json.dumps(workflow))
        expected = "execution_inputs_changed"

    result = module.prepare_primary_reuse(root, "owner/repo", 42, baseline, tmp_path / "prepared")

    assert result == {"status": "full_required", "reason": expected}
    assert not calls


@pytest.mark.parametrize("damage", ["running-primary", "failed-primary", "archive-digest"])
def test_prepare_primary_reuse_rejects_unfinished_or_unverified_originals(tmp_path, monkeypatch, damage):
    module = _load_module()
    root, baseline, commit, _, endpoints, responses, calls, _, _ = _primary_prepare_case(
        module, tmp_path, monkeypatch,
    )
    path = module.PRIMARY_REPAIR_TESTS[0]
    commit(path, (root / path).read_text() + "# target signal constants\n")
    if damage == "archive-digest":
        responses[endpoints["artifacts"]]["artifacts"][0]["digest"] = "sha256:" + "0" * 64
        expected = "artifact digest mismatch"
    else:
        job = responses[endpoints["jobs"]]["jobs"][0]
        job.update(status="in_progress" if damage == "running-primary" else "completed",
                   conclusion=None if damage == "running-primary" else "failure")
        expected = "primary full job has not succeeded"
    output = tmp_path / "prepared"

    with pytest.raises(module.AssuranceError, match=expected):
        module.prepare_primary_reuse(root, "owner/repo", 42, baseline, output)

    if damage != "archive-digest":
        assert calls == [endpoints["run"], endpoints["jobs"]]
    assert not (output / "previous/artifact.zip").exists()
    assert not (output / "fresh-manifest.json").exists()

"""真实大 diff 的容量与交付机制；合成专家结果不代表产品质量评审。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ai_sdlc.core import pr_review_provider
from ai_sdlc.core.loop_simulation_models import StageScoreContract
from ai_sdlc.core.pr_review_pack import ReviewPackBuildOptions, build_review_pack
from ai_sdlc.core.pr_review_service import (
    PRReviewStartOptions,
    doctor_pr_review,
    fix_pr_review,
    rerun_pr_review,
    start_pr_review,
)
from tests.integration.test_quantified_implementation import _cli, _payload
from tests.integration.test_stage_pr_review_pipeline import (
    LOOP_ID,
    REVIEW_ID,
    _prepare,
    _record_actual,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data
from tests.unit.test_pr_review_service import (
    _git,
    _init_repo,
    _write_clean_reviewer_script,
    _write_loop_policy,
)


def _large(root: Path) -> tuple[str, int]:
    _init_repo(root)
    (root / "README.md").write_text(
        "# Current tree\nTenant access is rejected outside its boundary.\n"
        + "容量边界测试\n" * 28_000,
        encoding="utf-8",
    )
    _git(root, "add", "README.md")
    raw = subprocess.run(
        ["git", "diff", "--cached", "--", "README.md"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    size = len(raw.encode("utf-8"))
    assert len(raw) < 500_000 < size < 1_000_000
    return raw, size


def _artifacts(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / ".ai-sdlc").rglob("*")
        if path.is_file()
    }


def _unjudged_review(
    tmp_path, *, initially_clean=False, before_start=None, launch_failure=None
):
    root = tmp_path / "project"
    root.mkdir()
    scripts = tmp_path / "providers"
    scripts.mkdir()
    _init_repo(root)
    (root / "README.md").write_text("# Current implementation\n")
    _git(root, "add", "README.md")
    failed = scripts / "failed.py"
    failed.write_text("raise SystemExit(20)\n")
    clean = _write_clean_reviewer_script(scripts)
    if before_start is not None:
        before_start(root, failed)
    command = [sys.executable, str(clean if initially_clean else failed)]
    if launch_failure == "enoent":
        command = [str(scripts / "absent-reviewer")]
    elif launch_failure == "eacces":
        failed.chmod(0o644)
        command = [str(failed)]
    elif launch_failure == "timeout":
        failed.write_text("import time\ntime.sleep(2)\n")
    result = start_pr_review(
        PRReviewStartOptions(
            root=root,
            base_ref="HEAD",
            diff_source="local-staged",
            provider_id="local-agent",
            current_model="gpt-5",
            provider_command=command,
            provider_timeout_seconds=0.05 if launch_failure == "timeout" else 60,
            review_id="unjudged",
            loop_id="unjudged-loop",
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
        )
    )
    if initially_clean:
        assert result.status == "started" and result.verdict == "clean"
    else:
        assert result.status == "blocked" and not Path(result.findings_path).exists()
    return root, Path(result.review_run_path).parent, failed, clean


def _repair_scope_case(tmp_path, *, severity="REQUIRED"):
    root = tmp_path / "project"
    root.mkdir()
    scripts = tmp_path / "providers"
    scripts.mkdir()
    _init_repo(root)
    (root / ".git/info/exclude").write_text(".ai-sdlc/\n")
    (root / "README.md").write_text("# Current implementation\n")
    _git(root, "add", "README.md")
    reviewer = _write_clean_reviewer_script(scripts)
    required = {
        "id": "REQ-1",
        "severity": severity,
        "file": "README.md",
        "claim": "The fix needs a typed dependency.",
        "evidence": "Missing type.",
        "risk": "Invalid recovery.",
        "suggested_fix": "Add the required type.",
        "confidence": 1.0,
    }
    reviewer.write_text(
        reviewer.read_text()
        .replace("'verdict': 'clean'", "'verdict': 'changes_required'")
        .replace("'findings': []", f"'findings': [{required!r}]")
        + "\nraise SystemExit(10)\n"
    )
    result = start_pr_review(
        PRReviewStartOptions(
            root=root,
            base_ref="HEAD",
            diff_source="local-staged",
            provider_id="local-agent",
            current_model="gpt-5",
            provider_command=[sys.executable, str(reviewer)],
            review_id="repair-scope",
            loop_id="repair-scope-loop",
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
        )
    )
    assert result.provider_status == "changes_required", result.model_dump_json()
    fixed = fix_pr_review(root)
    resolution = Path(fixed.resolution_path)
    payload = yaml.safe_load(resolution.read_bytes())
    if not payload["finding_resolutions"]:
        payload["finding_resolutions"].append({"finding_id": "REQ-1"})
    payload["finding_resolutions"][0].update(
        status="fixed",
        evidence_refs=["typed dependency regression"],
        operator="test-host",
        resolved_at="2026-09-10T21:00:00Z",
    )
    resolution.write_text(yaml.safe_dump(payload))
    dependency = root / "dependency.py"
    dependency.write_bytes(b"class RecoveryProof: pass\n")
    _git(root, "add", "dependency.py")
    directory = Path(result.review_run_path).parent
    originals = {
        name: (directory / name).read_bytes()
        for name in (
            "review-pack.json",
            "findings.json",
            "resolution.yaml",
            "review-run.json",
        )
    }
    request = {
        "schema_version": "1",
        "artifact_kind": "pr-repair-scope-input",
        "request_id": "typed-dependency-1",
        "review_id": "repair-scope",
        "loop_id": "repair-scope-loop",
        "head_commit": _git(root, "rev-parse", "HEAD"),
        "review_pack_sha256": hashlib.sha256(originals["review-pack.json"]).hexdigest(),
        "findings_sha256": hashlib.sha256(originals["findings.json"]).hexdigest(),
        "resolution_sha256": hashlib.sha256(originals["resolution.yaml"]).hexdigest(),
        "resolution_round": payload["round_number"],
        "staged_tree_oid": _git(root, "write-tree"),
        "dependencies": [
            {
                "path": "dependency.py",
                "finding_id": "REQ-1",
                "blob_sha256": hashlib.sha256(dependency.read_bytes()).hexdigest(),
                "reason": "The original REQUIRED fix needs the shared recovery type.",
            }
        ],
    }
    request_path = tmp_path / "repair-scope.json"
    request_path.write_text(json.dumps(request, indent=2))
    counter = tmp_path / "provider-called"
    reviewer = _write_clean_reviewer_script(scripts)
    reviewer.write_text(
        f"from pathlib import Path\nPath({str(counter)!r}).write_text('called')\n"
        + reviewer.read_text()
    )
    return root, directory, request_path, request, originals, counter, reviewer


def _repair_scope_rerun(root, request_path, reviewer):
    return rerun_pr_review(
        root,
        repair_scope_input=str(request_path),
        repair_scope_sha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
        provider_command=[sys.executable, str(reviewer)],
    )


def test_repair_scope_default_rejection_preserves_originals(tmp_path):
    root, directory, _, _, originals, counter, reviewer = _repair_scope_case(tmp_path)
    before = _artifacts(root)
    result = rerun_pr_review(root, provider_command=[sys.executable, str(reviewer)])
    assert result.status == "needs_user" and "dependency.py" in result.blocker
    assert not counter.exists()
    assert _artifacts(root) == before
    assert all(
        (directory / name).read_bytes() == raw for name, raw in originals.items()
    )


def test_repair_scope_cli_admits_exact_dependency_and_preserves_history(tmp_path):
    root, directory, path, request, originals, counter, _ = _repair_scope_case(tmp_path)
    raw_request = path.read_bytes()
    result = _cli(
        root,
        "pr-review",
        "rerun",
        "--repair-scope-input",
        str(path),
        "--repair-scope-sha256",
        hashlib.sha256(raw_request).hexdigest(),
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert counter.read_text() == "called"
    pack = json.loads((directory / "review-pack.json").read_bytes())
    assert pack["changed_files"] == ["README.md", "dependency.py"]
    assert "class RecoveryProof" in (directory / "diff.patch").read_text()
    assert "repair-scope.json" not in json.dumps(pack)
    audit = directory / "repair-scope" / request["request_id"]
    assert (audit / "request.json").read_bytes() == raw_request
    for name, raw in originals.items():
        assert (audit / name).read_bytes() == raw
    run = json.loads((directory / "review-run.json").read_bytes())
    old = json.loads(originals["review-run.json"])
    for field in (
        "review_id",
        "loop_id",
        "decision_staged_tree_oid",
        "decision_started_at_ms",
    ):
        assert run.get(field) == old.get(field)
    assert (
        yaml.safe_load((directory / "resolution-history.yaml").read_bytes())[
            "round_number"
        ]
        == request["resolution_round"]
    )
    assert (
        directory / f"previous-findings-round-{request['resolution_round'] + 1}.json"
    ).read_bytes() == originals["findings.json"]


@pytest.mark.parametrize(
    "change",
    [
        "raw-sha",
        "review_id",
        "loop_id",
        "head_commit",
        "staged_tree_oid",
        "review_pack_sha256",
        "findings_sha256",
        "resolution_sha256",
        "resolution_round",
        "blob",
        "unknown-finding",
        "missing-path",
        "extra-path",
        "duplicate-path",
        "parent-path",
        "absolute-path",
        "glob-path",
        "directory-path",
        "extra-field",
        "worktree-drift",
        "waived",
        "duplicate-resolution",
        "missing-audit-fields",
        "same-request-id-other-bytes",
    ],
)
def test_repair_scope_rejects_unbound_request_before_writes(tmp_path, change):
    root, directory, path, request, originals, counter, reviewer = _repair_scope_case(
        tmp_path
    )
    if change in {"review_id", "loop_id"}:
        request[change] = "other-review"
    elif change in {"head_commit", "staged_tree_oid"}:
        request[change] = "0" * 40
    elif change in {"review_pack_sha256", "findings_sha256", "resolution_sha256"}:
        request[change] = "0" * 64
    elif change == "resolution_round":
        request[change] += 1
    elif change == "blob":
        request["dependencies"][0]["blob_sha256"] = "0" * 64
    elif change == "unknown-finding":
        request["dependencies"][0]["finding_id"] = "other-finding"
    elif change == "missing-path":
        request["dependencies"] = []
    elif change in {"extra-path", "duplicate-path"}:
        extra = dict(request["dependencies"][0])
        if change == "extra-path":
            extra["path"] = "README.md"
        request["dependencies"].append(extra)
    elif change in {"parent-path", "absolute-path", "glob-path", "directory-path"}:
        request["dependencies"][0]["path"] = {
            "parent-path": "../dependency.py",
            "absolute-path": "/dependency.py",
            "glob-path": "*.py",
            "directory-path": "src/",
        }[change]
    elif change == "extra-field":
        request["allow_all"] = True
    elif change == "worktree-drift":
        (root / "dependency.py").write_text("different unstaged content\n")
    elif change in {"waived", "duplicate-resolution", "missing-audit-fields"}:
        resolution = yaml.safe_load(originals["resolution.yaml"])
        record = resolution["finding_resolutions"][0]
        if change == "waived":
            record.update(status="waived", reason="not actually fixed")
        elif change == "duplicate-resolution":
            resolution["finding_resolutions"].append(dict(record))
        else:
            record["operator"] = ""
        raw = yaml.safe_dump(resolution).encode()
        (directory / "resolution.yaml").write_bytes(raw)
        originals["resolution.yaml"] = raw
        request["resolution_sha256"] = hashlib.sha256(raw).hexdigest()
    elif change == "same-request-id-other-bytes":
        audit = directory / "repair-scope" / request["request_id"]
        audit.mkdir(parents=True)
        (audit / "request.json").write_bytes(b"other immutable request\n")
    path.write_text(json.dumps(request, indent=2))
    before = _artifacts(root)
    result = rerun_pr_review(
        root,
        repair_scope_input=str(path),
        repair_scope_sha256="0" * 64
        if change == "raw-sha"
        else hashlib.sha256(path.read_bytes()).hexdigest(),
        provider_command=[sys.executable, str(reviewer)],
    )
    assert result.status == "blocked", result.model_dump_json()
    assert not counter.exists()
    assert _artifacts(root) == before
    assert all(
        (directory / name).read_bytes() == raw for name, raw in originals.items()
    )


@pytest.mark.parametrize(
    "point", ["before-resolve", "after-resolve", "after-input", "same-tree-new-head"]
)
def test_repair_scope_pack_drift_rejects_before_first_overwrite(
    tmp_path, monkeypatch, point
):
    import ai_sdlc.core.pr_review_pack as pack_module

    root, directory, path, _, originals, counter, reviewer = _repair_scope_case(
        tmp_path
    )
    original_resolve = pack_module.resolve_diff_source
    original_input = pack_module._resolve_review_input
    source_before = (directory / "source-resolution.json").read_bytes()
    changed = False

    def drift():
        nonlocal changed
        if changed:
            return
        changed = True
        if point == "same-tree-new-head":
            parent = _git(root, "rev-parse", "HEAD")
            tree = _git(root, "rev-parse", "HEAD^{tree}")
            commit = _git(
                root, "commit-tree", tree, "-p", parent, "-m", "new head same tree"
            )
            _git(root, "update-ref", "HEAD", commit)
        else:
            (root / "dependency.py").write_text("changed index after confirmation\n")
            _git(root, "add", "dependency.py")

    def resolve(options):
        if point in {"before-resolve", "same-tree-new-head"}:
            drift()
        result = original_resolve(options)
        if point == "after-resolve":
            drift()
        return result

    def resolve_input(root, source):
        result = original_input(root, source)
        if point == "after-input":
            drift()
        return result

    monkeypatch.setattr(pack_module, "resolve_diff_source", resolve)

    # service 的预检查也用该函数；只在 pack 实际构建时注入漂移。
    def build(options):
        with monkeypatch.context() as scoped:
            scoped.setattr(pack_module, "_resolve_review_input", resolve_input)
            return pack_module.build_review_pack(options)

    import ai_sdlc.core.pr_review_service as service

    monkeypatch.setattr(service, "build_review_pack", build)
    result = _repair_scope_rerun(root, path, reviewer)
    assert changed and result.status == "blocked", result.model_dump_json()
    assert not counter.exists()
    assert (directory / "source-resolution.json").read_bytes() == source_before
    assert all(
        (directory / name).read_bytes() == raw for name, raw in originals.items()
    )


def test_repair_scope_audit_failure_prevents_provider_and_original_writes(
    tmp_path, monkeypatch
):
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    root, directory, path, _, originals, counter, reviewer = _repair_scope_case(
        tmp_path
    )
    original_write = LoopArtifactStore.write_bytes_artifact

    def fail_audit(self, target, content, **kwargs):
        if target.name == "review-pack.json":
            raise OSError("audit disk unavailable")
        return original_write(self, target, content, **kwargs)

    monkeypatch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_audit)
    result = _repair_scope_rerun(root, path, reviewer)
    assert result.status == "blocked" and "audit disk unavailable" in result.blocker
    assert not counter.exists()
    assert all(
        (directory / name).read_bytes() == raw for name, raw in originals.items()
    )
    assert not list(directory.glob("previous-findings-*"))


@pytest.mark.parametrize("change", ["resolution", "pointer"])
def test_repair_scope_original_drift_during_audit_stops_before_snapshot(
    tmp_path, monkeypatch, change
):
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    root, directory, path, _, originals, counter, reviewer = _repair_scope_case(
        tmp_path
    )
    original_write = LoopArtifactStore.write_bytes_artifact

    def drift(self, target, content, **kwargs):
        result = original_write(self, target, content, **kwargs)
        if target.name == "audit.json":
            if change == "resolution":
                resolution = yaml.safe_load(originals["resolution.yaml"])
                resolution["finding_resolutions"][0]["status"] = "unresolved"
                (directory / "resolution.yaml").write_text(yaml.safe_dump(resolution))
            else:
                (directory.parent / "current-review.json").write_text("{}\n")
        return result

    monkeypatch.setattr(LoopArtifactStore, "write_bytes_artifact", drift)
    result = _repair_scope_rerun(root, path, reviewer)
    assert result.status == "blocked", result.model_dump_json()
    assert not counter.exists()
    assert not list(directory.glob("previous-findings-*"))
    for name in ("review-pack.json", "review-run.json", "findings.json"):
        assert (directory / name).read_bytes() == originals[name]


@pytest.mark.parametrize("severity", ["BLOCKER", "ADVISORY"])
def test_repair_scope_requires_original_required_severity(tmp_path, severity):
    root, _, path, _, _, counter, reviewer = _repair_scope_case(
        tmp_path, severity=severity
    )
    before = _artifacts(root)
    result = _repair_scope_rerun(root, path, reviewer)
    assert result.status == "blocked" and not counter.exists()
    assert _artifacts(root) == before


@pytest.mark.parametrize("symlink", [False, True])
def test_repair_scope_rejects_nonregular_staged_dependency(tmp_path, symlink):
    root, _, path, request, _, counter, reviewer = _repair_scope_case(tmp_path)
    if symlink:
        (root / "dependency.py").unlink()
        (root / "dependency.py").symlink_to("README.md")
        _git(root, "add", "dependency.py")
        request["dependencies"][0]["blob_sha256"] = hashlib.sha256(
            b"README.md"
        ).hexdigest()
    else:
        _git(
            root,
            "update-index",
            "--cacheinfo",
            "160000",
            _git(root, "rev-parse", "HEAD"),
            "dependency.py",
        )
    request["staged_tree_oid"] = _git(root, "write-tree")
    path.write_text(json.dumps(request))
    before = _artifacts(root)
    result = _repair_scope_rerun(root, path, reviewer)
    assert result.status == "blocked" and not counter.exists()
    assert _artifacts(root) == before


def test_repair_scope_reviewer_audit_mutation_is_not_washed_by_retry(tmp_path):
    root, directory, path, request, _, counter, reviewer = _repair_scope_case(tmp_path)
    clean = reviewer.read_bytes()
    audit_path = directory / "repair-scope" / request["request_id"] / "request.json"
    reviewer.write_text(
        f"from pathlib import Path\nPath({str(audit_path)!r}).write_text('reviewer changed audit')\nraise SystemExit(20)\n"
    )
    result = _repair_scope_rerun(root, path, reviewer)
    assert result.status == "blocked" and result.exit_code == 20
    invocation = json.loads((directory / "reviewer-invocation.json").read_bytes())
    assert invocation["workspace_check"]["status"] == "mutated"
    reviewer.write_bytes(clean)
    before = _artifacts(root)
    result = rerun_pr_review(root, provider_command=[sys.executable, str(reviewer)])
    assert result.status == "blocked" and not counter.exists()
    assert audit_path.read_text() == "reviewer changed audit"
    assert _artifacts(root) == before


@pytest.mark.parametrize("command", ["doctor", "start", "rerun"])
@pytest.mark.parametrize("invalid", ["0", "-1", "1.5", "x"])
def test_cli_invalid_capacity_writes_nothing(tmp_path, command, invalid):
    _init_repo(tmp_path)
    before = _artifacts(tmp_path)
    result = _cli(tmp_path, "pr-review", command, "--max-diff-bytes", invalid, "--json")
    assert result.returncode == 2
    assert _artifacts(tmp_path) == before


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "600000", None])
def test_service_invalid_capacity_writes_nothing(tmp_path, invalid):
    _init_repo(tmp_path)
    before = _artifacts(tmp_path)
    results = [
        start_pr_review(PRReviewStartOptions(root=tmp_path, max_diff_bytes=invalid)),
        doctor_pr_review(root=tmp_path, base_ref="HEAD", max_diff_bytes=invalid),
        rerun_pr_review(tmp_path, max_diff_bytes=invalid),
        build_review_pack(
            ReviewPackBuildOptions(
                root=tmp_path,
                base_ref="HEAD",
                review_id="invalid",
                loop_id="invalid",
                max_diff_bytes=invalid,
            )
        ),
    ]
    assert all(result.status == "needs_user" for result in results)
    assert all("positive integer" in result.blocker for result in results)
    assert _artifacts(tmp_path) == before


@pytest.mark.parametrize("use_policy", [False, True])
def test_large_preview_build_and_rerun_per_call(tmp_path, use_policy):
    raw, size = _large(tmp_path)
    provider = [] if use_policy else ["--provider", "mock-reviewer"]
    if use_policy:
        _write_loop_policy(tmp_path, "default_provider: mock-reviewer\n")
    before = _artifacts(tmp_path)
    for command in ("doctor", "start"):
        args = ["pr-review", command, *provider, "--json"]
        if command == "start":
            args.append("--dry-run")
        rejected = json.loads(_cli(tmp_path, *args).stdout)
        assert rejected["status"] == "needs_user"
        assert f"{size} bytes" in rejected["blocker"]
        assert "500000 byte limit" in rejected["blocker"]
        boundary = json.loads(
            _cli(tmp_path, *args, "--max-diff-bytes", str(size - 1)).stdout
        )
        assert boundary["status"] == "needs_user"
        accepted = _payload(_cli(tmp_path, *args, "--max-diff-bytes", str(size)))
        assert accepted["status"] == ("ready" if command == "doctor" else "dry_run")
    assert _artifacts(tmp_path) == before
    args = ["pr-review", "start", *provider, "--review-id", "large-capacity", "--json"]
    rejected = json.loads(_cli(tmp_path, *args).stdout)
    assert rejected["status"] == "needs_user"
    directory = tmp_path / ".ai-sdlc/reviews/pr/large-capacity"
    assert not (directory / "review-pack.json").exists()
    assert not (directory / "findings.json").exists()
    started = _payload(_cli(tmp_path, *args, "--max-diff-bytes", str(size)))
    assert started["status"] == "started"
    pack = json.loads((directory / "review-pack.json").read_bytes())
    run = json.loads((directory / "review-run.json").read_bytes())
    assert "max_diff_bytes" not in pack and "max_diff_bytes" not in run
    assert pack["diff_coverage"]["diff_bytes"] == size
    assert (directory / "diff.patch").read_bytes() == raw.encode("utf-8")
    assert started["omitted_files_count"] == started["redacted_files_count"] == 0
    fixed = _payload(_cli(tmp_path, "pr-review", "fix", "--json"))
    assert fixed["round_number"] == 1
    rejected = json.loads(_cli(tmp_path, "pr-review", "rerun", "--json").stdout)
    assert rejected["status"] == "needs_user"
    assert "500000 byte limit" in rejected["blocker"]
    assert (directory / "previous-findings-round-2.json").exists()
    rerun = _payload(
        _cli(tmp_path, "pr-review", "rerun", "--max-diff-bytes", str(size), "--json")
    )
    assert rerun["review_id"] == started["review_id"]
    assert rerun["loop_id"] == started["loop_id"]
    assert (directory / "finding-history.json").exists()
    assert (directory / "resolution-history.yaml").exists()
    assert not (directory / "resolution.yaml").exists()
    next_fix = _payload(_cli(tmp_path, "pr-review", "fix", "--dry-run", "--json"))
    assert next_fix["round_number"] == 2
    assert (directory / "diff.patch").read_bytes() == raw.encode("utf-8")


def test_large_pack_original_verify_seal_commit_close(tmp_path):
    root = tmp_path.resolve()
    _, size = _large(root)
    started = _payload(
        _cli(
            root,
            "pr-review",
            "start",
            "--provider",
            "mock-reviewer",
            "--review-id",
            REVIEW_ID,
            "--max-diff-bytes",
            str(size),
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            "stage-simulation-v1",
            "--json",
        )
    )
    assert started["status"] == "started"
    before_invalid = _artifacts(root)
    invalid = rerun_pr_review(root, max_diff_bytes=0)
    assert invalid.status == "needs_user"
    assert _artifacts(root) == before_invalid
    data = contract_data()
    data.update(
        capability="stage-simulation-v1",
        loop_type="local-pr-review",
        profile_id="delivery-readiness-v1",
    )
    data["time_plan"].update(
        scope="local-pr-review-close",
        work_breakdown=["当前树风险分析", "实际验证", "独立评审与原Close"],
    )
    contract = StageScoreContract.model_validate(data)
    _prepare(
        root,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": [data],
            "sources": [
                {
                    "id": "spec",
                    "path": "README.md",
                    "sha256": hashlib.sha256(
                        (root / "README.md").read_bytes()
                    ).hexdigest(),
                    "locator": "tenant access",
                    "claim": "当前访问义务",
                }
            ],
        },
    )
    frozen = _prepare(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [
                candidate_data(contract, "current-staged-tree", seconds=None)
            ],
        },
    )
    _prepare(
        root,
        {
            "operation": "record-comparison",
            "request_id": "record",
            "judgement": {
                "judge_input_digest": frozen["judge_input"]["judge_input_digest"],
                "assessments": [assessment_data("current-staged-tree", 4, 4)],
            },
        },
    )
    verified = _payload(
        _cli(
            root,
            "pr-review",
            "verify",
            "--cwd",
            ".",
            "--json",
            "--",
            sys.executable,
            "-c",
            "print('current large tree verified')",
        )
    )
    assert verified["status"] == "ready"
    _prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "local-pr-review",
            "--loop-id",
            LOOP_ID,
            "--json",
        )
    )
    directory = root / ".ai-sdlc/reviews/pr" / REVIEW_ID
    sealed = (directory / "decision-context.json").read_bytes()
    assert _record_actual(root, reviewed, "PASS")["status"] == "passed"
    outcome = (directory / "review-outcome-round-1.json").read_bytes()
    committed = _payload(
        _cli(
            root,
            "pr-review",
            "commit",
            "--message",
            "large current tree delivery",
            "--json",
        )
    )
    assert committed["tree_oid"] == _git(root, "rev-parse", "HEAD^{tree}")
    closed = _payload(
        _cli(
            root,
            "pr-review",
            "close",
            "--review-id",
            REVIEW_ID,
            "--loop-id",
            LOOP_ID,
            "--expect-review-digest",
            reviewed["input_digest"],
            "--json",
        )
    )
    assert closed["status"] == "closed" and closed["verdict"] == "fully_clean"
    assert (directory / "decision-context.json").read_bytes() == sealed
    assert (directory / "review-outcome-round-1.json").read_bytes() == outcome
    assert not (directory / "review-outcome-round-2.json").exists()


@pytest.mark.parametrize("source", ["local-git-range", "local-unstaged", "patch"])
def test_preview_matches_builder_for_each_existing_source(tmp_path, source):
    base = _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("# 容量边界\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    raw = subprocess.run(
        ["git", "diff", "--cached"], cwd=tmp_path, capture_output=True, check=True
    ).stdout
    extra = {}
    if source == "local-git-range":
        _git(tmp_path, "commit", "-m", "changed")
    elif source == "local-unstaged":
        _git(tmp_path, "reset", "HEAD", "--", "README.md")
    else:
        patch = tmp_path / ".ai-sdlc/input.patch"
        patch.write_bytes(raw)
        extra["patch_file"] = str(patch)
    for limit in (len(raw) - 1, len(raw)):
        doctor = doctor_pr_review(
            root=tmp_path,
            base_ref=base,
            diff_source=source,
            provider_id="mock-reviewer",
            max_diff_bytes=limit,
            **extra,
        )
        started = start_pr_review(
            PRReviewStartOptions(
                root=tmp_path,
                base_ref=base,
                diff_source=source,
                provider_id="mock-reviewer",
                max_diff_bytes=limit,
                review_id=f"source-{limit}",
                **extra,
            )
        )
        assert doctor.status == ("ready" if limit == len(raw) else "needs_user")
        assert started.status == ("started" if limit == len(raw) else "needs_user")
        if limit < len(raw):
            assert doctor.blocker == started.blocker


def test_capacity_does_not_raise_file_size_limit(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("large text\n" * 100_001, encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    result = start_pr_review(
        PRReviewStartOptions(
            root=tmp_path,
            diff_source="local-staged",
            provider_id="mock-reviewer",
            max_diff_bytes=2_000_000,
            review_id="file-limit",
        )
    )
    assert result.status in {"blocked", "needs_user"}
    assert result.omitted_files_count == 1
    assert not (tmp_path / ".ai-sdlc/reviews/pr/file-limit/review-pack.json").exists()


def test_started_provider_without_original_cleanup_proof_cannot_recover(tmp_path):
    root, directory, clean, _ = _weekend_protocol_review(tmp_path, exit_code=0, verdict="clean")
    invocation_path = directory / "reviewer-invocation.json"
    payload = json.loads(invocation_path.read_bytes())
    payload.pop("completion_proof", None)
    invocation_path.write_text(json.dumps(payload))
    before = _artifacts(root)
    result = rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert result.status == "blocked", "父退出码不能替代原完整收尾证明"
    assert _artifacts(root) == before

@pytest.mark.parametrize("damage", ["hash", "process-minimal", "cleanup-minimal", "nonce", "time", "owned", "unknown", "status-type", "proof-version", "process-missing"])
def test_started_recovery_rejects_damaged_owned_completion(tmp_path, damage):
    root, directory, clean, _ = _weekend_protocol_review(tmp_path, exit_code=0, verdict="clean")
    invocation_path = directory / "reviewer-invocation.json"
    payload = json.loads(invocation_path.read_bytes())
    proof = payload["completion_proof"]
    name = "process.json" if damage in {"process-minimal", "nonce"} else "cleanup.json"
    original = json.loads(proof["originals"][name])
    if damage == "process-missing":
        proof["originals"].pop("process.json")
        proof["sha256"].pop("process.json")
    elif damage == "proof-version":
        proof["schema_version"] = True
    elif damage == "hash":
        proof["sha256"][name] = "0" * 64
    else:
        if damage == "process-minimal":
            original = {"ownership_nonce": proof["ownership_nonce"]}
        elif damage == "cleanup-minimal":
            original = {"ownership_nonce": proof["ownership_nonce"], "status": "complete"}
        elif damage == "nonce":
            original["ownership_nonce"] = "another-attempt"
        elif damage == "time":
            original["checked_at_ms"] = 0
        elif damage == "status-type":
            original["status"] = ["complete"]
        elif os.name == "nt":
            original["job_active_processes"] = 1
        elif damage == "owned":
            original["process_tracking"]["owned"] = []
        else:
            original["process_tracking"]["unattributed"] = [[987654, 1, 0]]
        proof["originals"][name] = json.dumps(original)
        proof["sha256"][name] = hashlib.sha256(proof["originals"][name].encode()).hexdigest()
    invocation_path.write_text(json.dumps(payload))
    before = _artifacts(root)
    result = rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert result.status == "blocked"
    assert _artifacts(root) == before


# v12：普通本地评审范围与启动前失败的真实恢复回归。
def _v12_save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))

def _v12_case(tmp_path, finding_file="README.md"):
    root = tmp_path / "project"
    root.mkdir()
    scripts = tmp_path / "providers"
    scripts.mkdir()
    _init_repo(root)
    (root / ".git/info/exclude").write_text(".ai-sdlc/\n")
    (root / "extra.txt").write_text("original extra\n")
    _git(root, "add", "extra.txt")
    _git(root, "commit", "-m", "Initial ordinary file")
    (root / "README.md").write_text("# Current staged implementation\n")
    _git(root, "add", "README.md")
    script = _write_clean_reviewer_script(scripts)
    clean = script.read_text()
    finding = dict(id="REQ-1", severity="REQUIRED", file=finding_file, claim="Ordinary correction", evidence="Local example", risk="Wrong result", suggested_fix="Correct result", confidence=1.0)
    script.write_text(clean.replace("'verdict': 'clean'", "'verdict': 'changes_required'").replace("'findings': []", f"'findings': [{finding!r}]") + "\nraise SystemExit(10)\n")
    options = PRReviewStartOptions(root=root, base_ref="HEAD", diff_source="local-staged", provider_id="local-agent", current_model="gpt-5", provider_command=[sys.executable, str(script)], review_id="v12-review", loop_id="v12-loop", decision_mode="adaptive-quantified", decision_capability="stage-simulation-v1")
    return root, script, clean, options

def _v12_fix(root, directory):
    fixed = fix_pr_review(root)
    assert fixed.status == "ready", fixed.model_dump_json()
    resolution = directory / "resolution.yaml"
    payload = yaml.safe_load(resolution.read_bytes())
    payload["finding_resolutions"][0].update(status="fixed", operator="diagnostic-author", evidence_refs=["ordinary local correction"], resolved_at="2026-09-11T13:33:00Z")
    resolution.write_text(yaml.safe_dump(payload))
    return fixed

@pytest.mark.parametrize("outside", [False, True])
def test_v12_feedback_rejected_cannot_expand_scope(tmp_path, outside):
    root, script, clean, options = _v12_case(tmp_path, "extra.txt" if outside else "README.md")
    first = start_pr_review(options)
    assert first.status == ("blocked" if outside else "started"), first.model_dump_json()
    directory = Path(first.review_run_path).parent
    original_pack = json.loads((directory / "review-pack.json").read_bytes())
    original_inv = (directory / "reviewer-invocation.json").read_bytes()
    fixed = fix_pr_review(root) if outside else _v12_fix(root, directory)
    if outside and fixed.status == "ready":
        payload = yaml.safe_load((directory / "resolution.yaml").read_bytes())
        payload["finding_resolutions"][0].update(status="fixed", operator="diagnostic-author",
            evidence_refs=["ordinary local correction"], resolved_at="2026-09-11T13:33:00Z")
        (directory / "resolution.yaml").write_text(yaml.safe_dump(payload))
    changed = root / ("extra.txt" if outside else "README.md")
    changed.write_text("New ordinary correction\n")
    _git(root, "add", changed.name)
    called = tmp_path / "second-provider-called"
    script.write_text(f"from pathlib import Path\nPath({str(called)!r}).write_text('called')\n" + clean)
    result = rerun_pr_review(root, provider_command=[sys.executable, str(script)])
    _v12_save(tmp_path / "observation.json", dict(first=first.model_dump(mode="json"), fix=fixed.model_dump(mode="json"), rerun=result.model_dump(mode="json"), original_allowlist=original_pack["reviewer_allowlist"], current_pack=json.loads((directory / "review-pack.json").read_bytes()), provider_called=called.exists(), original_invocation_sha256=hashlib.sha256(original_inv).hexdigest()))
    if outside:
        assert result.status == "blocked", "REJECTED out-of-allowlist feedback authorized next provider: " + result.model_dump_json()
        assert not called.exists()
    else:
        assert result.status == "started", result.model_dump_json()
        assert called.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_v12_feedback_rejected_is_not_a_fix_plan(tmp_path, dry_run):
    root, script, clean, options = _v12_case(tmp_path, "extra.txt")
    first = start_pr_review(options)
    assert first.status == "blocked"
    result = fix_pr_review(root, dry_run=dry_run)
    assert result.status == "blocked", result.model_dump_json()
    assert not (Path(first.review_run_path).parent / "resolution.yaml").exists()


def test_v12_prelaunch_unconfigured_command_does_not_publish_unrecoverable_review(tmp_path):
    from dataclasses import replace
    root, script, clean, options = _v12_case(tmp_path)
    script.write_text(clean)
    result = start_pr_review(replace(options, provider_command=[]))
    assert result.status == "needs_user", result.model_dump_json()
    assert not (root / ".ai-sdlc/reviews/pr/current-review.json").exists()
    assert not (root / ".ai-sdlc/reviews/pr/v12-review/review-run.json").exists()
    recovered = start_pr_review(options)
    assert recovered.status == "started", recovered.model_dump_json()
    assert recovered.review_id == options.review_id


def _weekend_protocol_review(tmp_path, *, exit_code, verdict):
    root = tmp_path / "project"
    root.mkdir()
    scripts = tmp_path / "providers"
    scripts.mkdir()
    _init_repo(root)
    (root / ".git/info/exclude").write_text(".ai-sdlc/\n")
    (root / "README.md").write_text("# Current ordinary implementation\n")
    _git(root, "add", "README.md")
    clean = _write_clean_reviewer_script(scripts)
    clean_script = clean.read_text()
    finding = dict(
        id="PROTO-1", severity="BLOCKER" if verdict == "blocked" else "REQUIRED",
        file="README.md", claim="The ordinary output is incomplete.",
        evidence="A required output is absent.", risk="The output is incorrect.",
        suggested_fix="Restore the expected output.", confidence=1.0,
    )
    script = clean_script.replace("'verdict': 'clean'", f"'verdict': {verdict!r}")
    if verdict != "clean":
        script = script.replace("'findings': []", f"'findings': [{finding!r}]")
    if verdict == "blocked":
        script = script.replace("'verdict': 'blocked',", "'verdict': 'blocked', 'blocker': 'Ordinary output is blocked.',")
    reviewer = scripts / "protocol-result.py"
    reviewer.write_text(script + f"\nraise SystemExit({exit_code})\n")
    result = start_pr_review(PRReviewStartOptions(
        root=root, base_ref="HEAD", diff_source="local-staged", provider_id="local-agent",
        current_model="gpt-5", provider_command=[sys.executable, str(reviewer)],
        review_id="protocol-authority", loop_id="protocol-authority-loop",
    ))
    directory = Path(result.review_run_path).parent
    invocation = json.loads((directory / "reviewer-invocation.json").read_bytes())
    assert invocation["exit_code"] == exit_code
    assert invocation["completion_proof"]
    assert invocation["workspace_check"]["status"] == "unchanged"
    assert json.loads((directory / "findings.json").read_bytes())["verdict"] == verdict
    return root, directory, clean, result


@pytest.mark.parametrize("exit_code,verdict", [(0, "clean"), (10, "changes_required"), (20, "blocked")])
def test_weekend_valid_provider_protocol_keeps_original_fix_semantics(tmp_path, exit_code, verdict):
    root, directory, _, result = _weekend_protocol_review(tmp_path, exit_code=exit_code, verdict=verdict)
    assert result.verdict == verdict, result.model_dump_json()
    original_run = json.loads((directory / "review-run.json").read_bytes())
    assert original_run["verdict"] == verdict
    if verdict == "clean":
        assert result.status == "started"
        assert not (directory / "resolution.yaml").exists()
        from ai_sdlc.cli.loop_review_cmd import resolve_review_input
        from ai_sdlc.core.pr_review_models import ReviewPack, ReviewRun
        from ai_sdlc.core.pr_review_service import (
            read_pr_recovery_originals,
            verify_pr_review_command,
        )

        verified = verify_pr_review_command(
            root, cwd=".", argv=(sys.executable, "-c", "print('ordinary verification')"),
        )
        assert verified.status == "ready", verified.model_dump_json()
        captured = {}
        resolve_review_input(root, loop_type="local-pr-review", loop_id=original_run["loop_id"],
                             captured_artifacts=captured, capture_all=True)
        invocation_path = directory / "reviewer-invocation.json"
        invocation_key = invocation_path.relative_to(root).as_posix()
        invocation_raw = invocation_path.read_bytes()
        assert captured[invocation_key] == invocation_raw
        run = ReviewRun.model_validate(original_run)
        pack = ReviewPack.model_validate_json((directory / "review-pack.json").read_bytes())
        read_pr_recovery_originals(root, run, pack, reviewed_artifacts=captured).assert_unchanged()
        incomplete = dict(captured)
        incomplete.pop(invocation_key)
        with pytest.raises(ValueError, match="missing-from-capture"):
            read_pr_recovery_originals(root, run, pack, reviewed_artifacts=incomplete)
        incomplete_invocation = json.loads(invocation_raw)
        incomplete_invocation.pop("completion_proof")
        incomplete[invocation_key] = json.dumps(incomplete_invocation).encode()
        with pytest.raises(ValueError, match="completion-proof-required"):
            read_pr_recovery_originals(root, run, pack, reviewed_artifacts=incomplete)
        failed_execution = json.loads(invocation_raw)
        proof = failed_execution["completion_proof"]
        raw = json.loads(proof["originals"]["raw-result.json"])
        raw["output_io_error"] = True
        changed_raw = json.dumps(raw)
        proof["originals"]["raw-result.json"] = changed_raw
        proof["sha256"]["raw-result.json"] = hashlib.sha256(changed_raw.encode()).hexdigest()
        incomplete[invocation_key] = json.dumps(failed_execution).encode()
        with pytest.raises(ValueError, match="execution-is-incomplete"):
            read_pr_recovery_originals(root, run, pack, reviewed_artifacts=incomplete)
        # 旧成功调用缺新字段仍可读；明确 started 的新调用丢证明则已在上方拒绝。
        legacy = json.loads(invocation_raw)
        legacy.pop("completion_proof")
        legacy.pop("launch_status")
        invocation_path.write_text(json.dumps(legacy))
        try:
            read_pr_recovery_originals(root, run, pack).assert_unchanged()
        finally:
            invocation_path.write_bytes(invocation_raw)

    else:
        preview = fix_pr_review(root, dry_run=True)
        fixed = fix_pr_review(root)
        assert preview.status == "ready" and fixed.status == "ready", fixed.model_dump_json()
        assert fixed.round_number == 1 and fixed.selected_findings_count == 1
        resolution = yaml.safe_load((directory / "resolution.yaml").read_bytes())
        assert resolution["round_number"] == 1
        assert resolution["finding_resolutions"][0]["finding_id"] == "PROTO-1"


def _weekend_fail_publication_writer(monkeypatch, directory, point, error):
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    target_name = {"before-first": "source-resolution.json", "before-middle": "model-resolution.json", "after-last": "review-pack.json"}[point]
    observed = []
    fired = False

    def wrap(real):
        def write(store, path, *args, **kwargs):
            nonlocal fired
            target = Path(path)
            if not target.is_absolute():
                target = store.root / target
            selected = target == directory / target_name and not fired
            if selected and point != "after-last":
                fired = True
                observed.append({"point": point, "path": str(target), "phase": "before-write"})
                raise error
            result = real(store, path, *args, **kwargs)
            if selected:
                fired = True
                observed.append({"point": point, "path": str(target), "phase": "after-write"})
                raise error
            return result
        return write

    # 同一原产物的旧文本入口及严格字节入口都注入一次；归档与恢复写入不再故障。
    for name in ("write_json_artifact", "write_markdown_artifact", "write_bytes_artifact"):
        monkeypatch.setattr(LoopArtifactStore, name, wrap(getattr(LoopArtifactStore, name)))
    return observed


def _fix18_mutated_clean_review(tmp_path):
    from ai_sdlc.core import pr_review_service as service

    root = tmp_path / "project"
    root.mkdir()
    scripts = tmp_path / "providers"
    scripts.mkdir()
    _init_repo(root)
    (root / ".git/info/exclude").write_text(".ai-sdlc/\nnotes/\n")
    (root / "notes").mkdir()
    note = root / "notes/state.txt"
    note.write_bytes(b"original ordinary note\n")
    (root / "README.md").write_text("# Current reviewed implementation\n")
    _git(root, "add", "README.md")
    clean = _write_clean_reviewer_script(scripts)
    first = start_pr_review(PRReviewStartOptions(
        root=root, base_ref="HEAD", diff_source="local-staged", provider_id="local-agent",
        current_model="gpt-5", provider_command=[sys.executable, str(clean)],
        review_id="workspace-delivery", loop_id="workspace-delivery-loop",
    ))
    assert first.status == "started" and first.verdict == "clean", first.model_dump_json()
    verified = service.verify_pr_review_command(
        root, cwd=".", argv=(sys.executable, "-c", "print('actual unchanged-tree verification')"),
    )
    assert verified.status == "ready", verified.model_dump_json()
    note_original = note.read_bytes()
    note_stat = note.stat()
    mutator = scripts / "clean-with-ignored-mutation.py"
    mutator.write_text(clean.read_text() + "\nfrom pathlib import Path\nPath('notes/state.txt').write_text('provider changed ignored note\\n')\n")
    failed = rerun_pr_review(root, provider_command=[sys.executable, str(mutator)])
    assert failed.status == "blocked" and failed.verdict is None, failed.model_dump_json()
    directory = Path(failed.review_run_path).parent
    # 技术失败的derived verdict为None；交付绕过反例来自原始合法clean反馈。
    findings_raw = (directory / "findings.json").read_bytes()
    findings = json.loads(findings_raw)
    saved_run = json.loads((directory / "review-run.json").read_bytes())
    assert findings["verdict"] == "clean" and findings["findings"] == []
    assert saved_run["findings_digest"] == hashlib.sha256(findings_raw).hexdigest()
    invocation = json.loads((directory / "reviewer-invocation.json").read_bytes())
    assert invocation["exit_code"] == 0 and invocation["status"] == "blocked"
    assert invocation["workspace_check"]["status"] == "mutated"
    assert invocation["workspace_check"]["original_values"]
    assert not invocation["workspace_check"]["host_artifact_mutations"]
    from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation

    proof = ProviderRunnerInvocation.model_validate(invocation).completion_proof
    assert proof is not None and proof.require_complete()["launch_status"] == "started"
    run, _ = service._load_current_review_run(root)
    assert not service._verification_evidence_blocker(run, service._load_verification_evidence(root, run))
    return root, directory, clean, failed, note_original, note_stat


@pytest.mark.parametrize("case", ["modern-unchanged", "modern-mutated", "modern-unproven",
                                  "modern-missing-proof", "legacy-success", "valid-required"])
def test_provider_feedback_delivery_eligibility_originals(tmp_path, case):
    from ai_sdlc.core import pr_review_service as service
    from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation

    required = case == "valid-required"
    root, directory, _, _ = _weekend_protocol_review(
        tmp_path, exit_code=10 if required else 0, verdict="changes_required" if required else "clean",
    )
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    original = _artifacts(root)
    capture = dict(original)
    key = (directory / "reviewer-invocation.json").relative_to(root).as_posix()
    invocation = json.loads(capture[key])
    if case == "modern-mutated":
        # 用实际现场边界构造schema合法负控；即使现场等于原边界，也不能升级旧mutation。
        snapshot = pr_review_provider._worktree_snapshot(root,
            pr_review_provider._provider_host_artifact_paths(root, run.review_id)
            | frozenset({directory / "findings.json"}))
        invocation["workspace_check"]["status"] = "mutated"
        invocation["workspace_check"]["original_values"] = {"README.md": snapshot["README.md"]}
    elif case == "modern-unproven":
        invocation["workspace_check"]["status"] = "unproven"
        invocation["workspace_check"]["reason"] = "Explicit unproven-workspace negative control"
    elif case == "modern-missing-proof":
        invocation.pop("completion_proof")
    elif case == "legacy-success":
        invocation.pop("completion_proof")
        invocation.pop("launch_status")
    ProviderRunnerInvocation.model_validate(invocation)
    if case in {"modern-mutated", "modern-unproven", "modern-missing-proof"}:
        capture[key] = json.dumps(invocation).encode()
        with pytest.raises(ValueError, match="workspace|unproven|completion-proof"):
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture)
    elif case == "legacy-success":
        # 旧序列化是专门的兼容fixture；capture与live必须同字节，最后恢复真实原件。
        path = directory / "reviewer-invocation.json"
        capture[key] = json.dumps(invocation).encode()
        try:
            path.write_bytes(capture[key])
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture).assert_unchanged()
        finally:
            path.write_bytes(original[key])
    else:
        # unchanged/required直接复用原始bytes，不能重新序列化后冒称未漂移。
        service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture).assert_unchanged()
        if required:
            assert fix_pr_review(root, dry_run=True).status == "ready"
    assert _artifacts(root) == original


def _v24_native_provider_command(script):
    import shlex

    argv = [sys.executable, str(script)]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _v24_native_formal_case(tmp_path, *, first_status="UNKNOWN"):
    """正式结果由原生 writer 产生；外部脚本仅提供实际进程与协议输入。"""
    root = tmp_path / "project"
    scripts = tmp_path / "providers"
    root.mkdir()
    scripts.mkdir()
    _init_repo(root)
    (root / ".git/info/exclude").write_text(".ai-sdlc/\n")
    (root / "README.md").write_text("# Current tree\nCurrent ordinary output.\n")
    _git(root, "add", "README.md")
    events = tmp_path / "provider-calls.jsonl"

    def observed_script(path, source, label):
        path.write_text(
            "import json, time\n"
            f"with open({str(events)!r}, 'a', encoding='utf-8') as observed:\n"
            f" observed.write(json.dumps({{'label': {label!r}, 'at_ns': time.time_ns()}}) + '\\n')\n"
            + source,
            encoding="utf-8",
        )
        return path

    clean = _write_clean_reviewer_script(scripts)
    clean_source = clean.read_text()
    observed_script(clean, clean_source, "clean")
    failed = observed_script(scripts / "technical.py", "raise SystemExit(23)\n", "technical")
    finding = dict(
        id="REQ-24", severity="REQUIRED", file="README.md",
        claim="The ordinary output still needs correction.",
        evidence="The current example has an incorrect output.",
        risk="The delivered output is incorrect.",
        suggested_fix="Correct the current output.", confidence=1.0,
    )
    required = observed_script(
        scripts / "required.py",
        clean_source.replace("'verdict': 'clean'", "'verdict': 'changes_required'")
        .replace("'findings': []", f"'findings': [{finding!r}]")
        + "\nraise SystemExit(10)\n",
        "required",
    )
    started = _payload(_cli(
        root, "pr-review", "start", "--provider", "local-agent",
        "--current-model", "gpt-5", "--provider-command", _v24_native_provider_command(clean),
        "--review-id", REVIEW_ID, "--decision-mode", "adaptive-quantified",
        "--decision-capability", "stage-simulation-v1", "--json",
    ))
    assert started["status"] == "started" and started["verdict"] == "clean", started
    directory = root / ".ai-sdlc/reviews/pr" / REVIEW_ID
    initial_invocation = (directory / "reviewer-invocation.json").read_bytes()
    data = contract_data()
    data.update(capability="stage-simulation-v1", loop_type="local-pr-review",
                profile_id="delivery-readiness-v1")
    data["time_plan"].update(
        scope="local-pr-review-close",
        work_breakdown=["当前输出判断", "实际修复及验证", "独立评审与原Close"],
    )
    contract = StageScoreContract.model_validate(data)
    _prepare(root, {
        "operation": "begin", "request_id": "begin", "contracts": [data],
        "sources": [{"id": "spec", "path": "README.md",
                     "sha256": hashlib.sha256((root / "README.md").read_bytes()).hexdigest(),
                     "locator": "ordinary output", "claim": "当前普通输出义务"}],
    })
    frozen = _prepare(root, {
        "operation": "freeze-comparison", "request_id": "freeze",
        "candidates": [candidate_data(contract, "current-staged-tree", seconds=None)],
    })
    _prepare(root, {
        "operation": "record-comparison", "request_id": "record",
        "judgement": {
            "judge_input_digest": frozen["judge_input"]["judge_input_digest"],
            "assessments": [assessment_data("current-staged-tree", 4, 4)],
        },
    })
    initial_marker = tmp_path / "initial-verification.txt"
    verified = _payload(_cli(
        root, "pr-review", "verify", "--json", "--", sys.executable, "-c",
        "from pathlib import Path;assert 'Current ordinary output.' in Path('README.md').read_text();"
        f"Path({str(initial_marker)!r}).write_text('initial verified')",
    ))
    assert verified["status"] == "ready" and initial_marker.read_text() == "initial verified"
    _prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    first = _payload(_cli(
        root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json",
    ))
    outcome = None
    if first_status is not None:
        recorded = _record_actual(root, first, first_status)
        assert recorded["status"] == ("passed" if first_status == "PASS" else "needs_fix"), recorded
        outcome = (directory / "review-outcome-round-1.json").read_bytes()
        actual = json.loads(outcome)
        assert actual["status"] == "completed" and actual["round_number"] == 1
        assert actual["simulation"]["decision"]["action"] == (
            "stop" if first_status == "PASS" else "repair"
        )
    run = json.loads((directory / "review-run.json").read_bytes())
    assert run["provider_id"] == "local-agent" and run["decision_capability"] == "stage-simulation-v1"
    return dict(
        root=root, directory=directory, clean=clean, failed=failed, required=required,
        events=events, first=first, first_outcome=outcome,
        context=(directory / "decision-context.json").read_bytes(),
        initial_invocation=initial_invocation, initial_run=run,
    )


def _v24_native_repair(case):
    root, directory = case["root"], case["directory"]
    preview = _payload(_cli(root, "pr-review", "fix", "--dry-run", "--json"))
    fixed = _payload(_cli(root, "pr-review", "fix", "--json"))
    assert fixed["status"] == "ready" and fixed["round_number"] == preview["round_number"] == 1
    (root / "README.md").write_text("# Repaired tree\nCorrected ordinary output.\n")
    _git(root, "add", "README.md")
    return {name: (directory / name).read_bytes() for name in (
        "findings.json", "review-run.json", "resolution.yaml", "fix-plan.md",
        "review-outcome-round-1.json", "decision-context.json",
    )}


def _v24_native_rerun(case, script):
    result = _cli(
        case["root"], "pr-review", "rerun", "--provider-command",
        _v24_native_provider_command(script), "--json",
    )
    payload = json.loads(result.stdout)
    expected_exit = (10 if payload.get("provider_status") == "changes_required" else 0) if payload["status"] == "started" else 1
    assert result.returncode == expected_exit, result.stdout + result.stderr
    return payload


def _v24_native_failure(case):
    from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation

    directory = case["directory"]
    before = (directory / "reviewer-invocation.json").read_bytes()
    count = len(case["events"].read_text().splitlines())
    failed = _v24_native_rerun(case, case["failed"])
    raw = (directory / "reviewer-invocation.json").read_bytes()
    # 同一个 blocked 返回值不能证明发生了第二次执行；检查独立进程标记和原始回执。
    assert len(case["events"].read_text().splitlines()) == count + 1, failed
    assert raw != before and failed["status"] == "blocked" and failed["verdict"] is None, failed
    invocation = ProviderRunnerInvocation.model_validate_json(raw)
    assert invocation.exit_code == 23 and invocation.launch_status == "started"
    assert invocation.completion_proof is not None
    process, result, cleanup = invocation.completion_proof.verified_receipts()
    assert process and result["exit_code"] == 23 and cleanup["status"] == "complete"
    assert result["started_at_ms"] <= result["ended_at_ms"] <= cleanup["checked_at_ms"]
    # 进程时间和费用取完成证明的原始毫秒时钟；摘要 completed_at 仅预检失败时有值。
    assert process["started_at_ms"] == result["started_at_ms"]
    executed_at_ms = json.loads(case["events"].read_text().splitlines()[-1])["at_ns"] // 1_000_000
    assert result["started_at_ms"] <= executed_at_ms <= result["ended_at_ms"]
    assert not (directory / "findings.json").exists()
    return {"result": failed, "invocation": raw, "duration_ms": result["ended_at_ms"] - result["started_at_ms"]}


def _v24_native_second_review(case, *, status="PASS"):
    root = case["root"]
    marker = case["events"].with_name("repaired-verification.txt")
    verified = _payload(_cli(
        root, "pr-review", "verify", "--json", "--", sys.executable, "-c",
        "from pathlib import Path;assert 'Corrected ordinary output.' in Path('README.md').read_text();"
        f"Path({str(marker)!r}).write_text('repair verified')",
    ))
    assert verified["status"] == "ready" and marker.read_text() == "repair verified"
    second = _payload(_cli(
        root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json",
    ))
    assert second["round_number"] == 2 and second["input_digest"] != case["first"]["input_digest"]
    recorded = _record_actual(root, second, status)
    assert recorded["status"] == "passed" if status == "PASS" else recorded["status"] in {"blocked", "needs_user"}
    assert (case["directory"] / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]
    assert not (case["directory"] / "review-outcome-round-3.json").exists()
    return second


def test_v24_native_formal_recovery_rejects_changed_originals(tmp_path):
    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    _v24_native_repair(case)
    assert _v24_native_rerun(case, case["clean"])["status"] == "started"
    outcome_path = directory / "review-outcome-round-1.json"
    outcome = json.loads(outcome_path.read_bytes())
    context_path = directory / "decision-context.json"
    run_path = directory / "review-run.json"
    run = json.loads(run_path.read_bytes())
    changed_input = json.loads(outcome_path.read_bytes())
    changed_input["input_digest"] = "0" * 64
    changed_source = json.loads(outcome_path.read_bytes())
    changed_source["simulation"]["source_digest"] = "sha256:" + "0" * 64
    history_path = directory / "finding-history.json"
    missing_input = json.loads(history_path.read_bytes())
    input_key = (directory / "verification-evidence.json").relative_to(root).as_posix()
    del missing_input["formal_review_originals"]["inputs"][input_key]
    pack_path = directory / "review-pack.json"
    pack = json.loads(pack_path.read_bytes())
    references = pack["test_results_refs"]
    reference = next(ref for ref in references if ref.startswith("pr-formal-originals"))
    other_refs = [ref for ref in references if ref != reference]
    invalid_refs = {
        "reference-missing": other_refs,
        "reference-duplicate": [*references, reference],
        "reference-location": [*other_refs, reference.replace("/finding-history.json", "/foreign-history.json")],
        "reference-content": [*other_refs, reference.rsplit("#sha256:", 1)[0] + "#sha256:" + "0" * 64],
        "reference-format": [*other_refs, "pr-formal-originals-v1:incomplete"],
    }
    variants = [
        ("missing-R1", outcome_path, None),
        ("malformed-R1", outcome_path, b"{\n"),
        ("wrong-R1-loop", outcome_path, json.dumps({**outcome, "loop_id": "foreign-loop"}).encode()),
        ("wrong-R1-input", outcome_path, json.dumps(changed_input).encode()),
        ("wrong-R1-source", outcome_path, json.dumps(changed_source).encode()),
        ("missing-context", context_path, None),
        ("wrong-current-identity", run_path, json.dumps({**run, "loop_id": "foreign-loop"}).encode()),
        ("wrong-current-candidate", run_path, json.dumps({**run, "staged_tree_oid": "0" * 40}).encode()),
        ("missing-current-proof", directory / "reviewer-invocation.json", None),
        ("missing-latest-judgment", directory / "findings.json", None),
        ("malformed-latest-judgment", directory / "findings.json", b"{}\n"),
        ("missing-original-R1-input-member", history_path, json.dumps(missing_input).encode()),
        *((label, pack_path, json.dumps({**pack, "test_results_refs": refs}).encode())
          for label, refs in invalid_refs.items()),
    ]
    for label, path, changed in variants:
        original = path.read_bytes()
        metadata = path.stat()
        current_run_original, current_run_metadata = run_path.read_bytes(), run_path.stat()
        calls = case["events"].read_bytes()
        try:
            if changed is None:
                path.unlink()
            else:
                path.write_bytes(changed)
            if label.startswith("reference-"):
                # 负控同时绑定当前被改 pack 的摘要，隔离缺失/冲突引用，而非先撞字节摘要错误。
                altered_run = {**run, "review_pack_digest": hashlib.sha256(pack_path.read_bytes()).hexdigest()}
                run_path.write_text(json.dumps(altered_run))
            before = _artifacts(root)
            rejected = _v24_native_rerun(case, case["clean"])
            assert rejected["status"] in {"blocked", "no_review"}, (label, rejected)
            if label.startswith("reference-"):
                # 原生入口必须识别引用本身的缺口；不能仅靠随后旧 pack 摘要失配拦下。
                assert "formal review original reference" in rejected["blocker"], (label, rejected)
            assert case["events"].read_bytes() == calls, label
            assert _artifacts(root) == before, label
        finally:
            path.write_bytes(original)
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            run_path.write_bytes(current_run_original)
            os.utime(run_path, ns=(current_run_metadata.st_atime_ns, current_run_metadata.st_mtime_ns))
    # 所有负控撤回后仍须能继续正常重评，避免用一个永远拒绝的入口通过测试。
    recovered = _v24_native_rerun(case, case["clean"])
    assert recovered["status"] == "started" and recovered["verdict"] == "clean", recovered
    assert outcome_path.read_bytes() == case["first_outcome"]


@pytest.mark.parametrize("state", ["r1-clean", "r2-clean", "r2-required"])
def test_v24_native_formal_completed_or_exhausted_review_stays_terminal(tmp_path, state):
    case = _v24_native_formal_case(tmp_path, first_status="PASS" if state == "r1-clean" else "UNKNOWN")
    if state != "r1-clean":
        _v24_native_repair(case)
        assert _v24_native_rerun(case, case["clean"])["status"] == "started"
        _v24_native_second_review(case, status="PASS" if state == "r2-clean" else "FAIL")
    before = _artifacts(case["root"])
    calls = case["events"].read_bytes()
    rejected = _v24_native_rerun(case, case["failed"])
    assert rejected["status"] == "blocked", (state, rejected)
    assert case["events"].read_bytes() == calls, state
    assert _artifacts(case["root"]) == before, state
    assert not (case["directory"] / "review-outcome-round-3.json").exists()


def test_v24_native_formal_repair_keeps_latest_required_finding(tmp_path):
    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    _v24_native_repair(case)
    required = _v24_native_rerun(case, case["required"])
    assert required["status"] == "started" and required["verdict"] == "changes_required", required
    fixed = _payload(_cli(root, "pr-review", "fix", "--json"))
    assert fixed["selected_findings_count"] == 1 and fixed["round_number"] == 2
    resolution = yaml.safe_load((directory / "resolution.yaml").read_bytes())
    assert resolution["finding_resolutions"][0]["finding_id"] == "REQ-24"
    assert resolution["finding_resolutions"][0]["status"] == "unresolved"
    calls = case["events"].read_bytes()
    before = _artifacts(root)
    rejected = _v24_native_rerun(case, case["failed"])
    assert rejected["status"] == "blocked" and "Unresolved" in rejected["blocker"], rejected
    assert case["events"].read_bytes() == calls and _artifacts(root) == before
    assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]
    assert not (directory / "review-outcome-round-2.json").exists()

def test_v24_native_formal_recovery_rejects_joint_original_absence(tmp_path):
    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    _v24_native_repair(case)
    assert _v24_native_rerun(case, case["clean"])["status"] == "started"
    paths = [directory / name for name in (
        "review-outcome-round-1.json", "finding-history.json",
    )]
    originals = {path: (path.read_bytes(), path.stat()) for path in paths}
    calls = case["events"].read_bytes()
    try:
        for path in paths:
            path.unlink()
        before = _artifacts(root)
        rejected = _v24_native_rerun(case, case["clean"])
        # 这两件共同缺失不能将已发生的 R1 降格为从未评审。
        assert rejected["status"] == "blocked", rejected
        assert case["events"].read_bytes() == calls
        assert _artifacts(root) == before
    finally:
        for path, (raw, metadata) in originals.items():
            path.write_bytes(raw)
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    recovered = _v24_native_rerun(case, case["clean"])
    assert recovered["status"] == "started" and recovered["verdict"] == "clean", recovered
    assert paths[0].read_bytes() == case["first_outcome"]


def _v24_native_legacy_formal_case(tmp_path, *, severity=None):
    from tests.integration.test_cli_loop_review import _write_cli_expert_results

    root = tmp_path.resolve()
    _init_repo(root)
    (root / "README.md").write_text("# Ordinary result\nSaved output is documented.\n")
    _git(root, "add", "README.md")
    started = _payload(_cli(root, "pr-review", "start", "--provider", "mock-reviewer",
                            "--review-id", REVIEW_ID, "--json"))
    directory = Path(started["review_run_path"]).parent
    if not directory.is_absolute():
        directory = root / directory
    run = json.loads((directory / "review-run.json").read_bytes())
    assert run["provider_id"] == "mock-reviewer" and run.get("decision_capability") is None
    verified = _payload(_cli(root, "pr-review", "verify", "--json", "--", sys.executable,
                              "-c", "from pathlib import Path;assert 'Saved output' in Path('README.md').read_text()"))
    assert verified["status"] == "ready"
    reviewed = _payload(_cli(root, "loop", "review", "--type", "local-pr-review",
                             "--loop-id", LOOP_ID, "--json"))
    args = ["loop", "review-record", "--type", "local-pr-review", "--loop-id", LOOP_ID,
            "--expect-digest", reviewed["input_digest"], "--json"]
    # 只合成专家输入，正式 outcome 必须由原生 writer 产生。
    for path in _write_cli_expert_results(directory, reviewed, severity=severity):
        args.extend(["--result", str(path)])
    assert _payload(_cli(root, *args))["status"] == ("passed" if severity is None else "needs_fix")
    first_path = directory / "review-outcome-round-1.json"
    first = first_path.read_bytes()
    assert json.loads(first).get("simulation") is None
    return dict(root=root, directory=directory, first=reviewed, first_outcome=first, initial_run=run)


def test_v24_native_legacy_formal_review_captures_originals_for_close(tmp_path, monkeypatch):
    from ai_sdlc.cli.loop_review_cmd import (
        resolve_review_input,
        validate_review_input_for_close,
    )
    from ai_sdlc.core import pr_review_service as service

    case = _v24_native_legacy_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    reviewed, first = case["first"], case["first_outcome"]
    first_path = directory / "review-outcome-round-1.json"
    captured = {}
    current = resolve_review_input(root, loop_type="local-pr-review", loop_id=LOOP_ID,
                                   captured_artifacts=captured)
    assert current.input_digest == reviewed["input_digest"]
    first_key = first_path.relative_to(root).as_posix()
    assert captured[first_key] == first and first_key not in current.artifact_paths
    current_run, _ = service._load_current_review_run(root, reviewed_artifacts=captured)
    pack = service._load_review_pack(root, current_run.review_pack_path, reviewed_artifacts=captured)
    service.read_pr_recovery_originals(root, current_run, pack, reviewed_artifacts=captured).assert_unchanged()
    missing = dict(captured)
    del missing[first_key]
    with pytest.raises(ValueError, match="missing-from-capture"):
        service.read_pr_recovery_originals(root, current_run, pack, reviewed_artifacts=missing)
    assert first_path.read_bytes() == first
    assert _payload(_cli(root, "pr-review", "commit", "--message", "ordinary reviewed output", "--json"))["status"] == "ready"
    run_path = directory / "review-run.json"
    final_path = directory / "final-report.md"
    original_run, run_metadata = run_path.read_bytes(), run_path.stat()
    assert not final_path.exists()
    before = _artifacts(root)
    render = service._render_final_report
    unexpected_run = json.loads(original_run)
    unexpected_run["next_action"] = "Unexpected external write before Close publication."
    unexpected_bytes = json.dumps(unexpected_run).encode()

    def drift_before_write(**kwargs):
        rendered = render(**kwargs)
        run_path.write_bytes(unexpected_bytes)
        return rendered

    # 复用真实 R1/commit 状态；写前变动应先拒绝，不能被合法 Close 输出覆盖。
    with monkeypatch.context() as changed:
        changed.setattr(service, "_render_final_report", drift_before_write)
        with pytest.raises(ValueError, match="recovery-original-drift"):
            service.close_pr_review(root, expected_review_id=REVIEW_ID, expected_loop_id=LOOP_ID,
                                    expected_review_digest=reviewed["input_digest"],
                                    review_input_validator=validate_review_input_for_close)
    assert run_path.read_bytes() == unexpected_bytes and not final_path.exists()
    run_path.write_bytes(original_run)
    os.utime(run_path, ns=(run_metadata.st_atime_ns, run_metadata.st_mtime_ns))
    assert _artifacts(root) == before
    write_json = service.LoopArtifactStore.write_json_artifact
    observations = []

    def drift_after_write(store, path, payload):
        result = write_json(store, path, payload)
        if path == run_path:
            intended = run_path.read_bytes()
            run_path.write_bytes(intended + b" ")
            observations.append({"intended_run_hex": intended.hex(), "unexpected_run_hex": run_path.read_bytes().hex(),
                                 "final_report_hex": final_path.read_bytes().hex()})
        return result

    # 即使 JSON 语义相同，写后未声明字节仍必须拒绝；不能以现场反读刷新预期。
    with monkeypatch.context() as changed:
        changed.setattr(service.LoopArtifactStore, "write_json_artifact", drift_after_write)
        with pytest.raises(ValueError, match="recovery-original-drift"):
            service.close_pr_review(root, expected_review_id=REVIEW_ID, expected_loop_id=LOOP_ID,
                                    expected_review_digest=reviewed["input_digest"],
                                    review_input_validator=validate_review_input_for_close)
    assert len(observations) == 1 and json.loads(run_path.read_bytes())["status"] == "closed"
    assert final_path.exists() and first_path.read_bytes() == first
    _v12_save(root.with_name(root.name + "-close-transition-negatives.json"), {
        "before_run_hex": original_run.hex(), "original_R1_sha256": hashlib.sha256(first).hexdigest(),
        "before_write_unexpected_run_hex": unexpected_bytes.hex(), "after_write": observations,
        "classification": "原生 Close 的受控测试变动；写后失败确已生成 closed run/report，原始副作用字节保存在此。",
    })
    # 只恢复本夹具的两个自建输出，再用原生 CLI 证明正常 Close 仍可完成。
    run_path.write_bytes(original_run)
    os.utime(run_path, ns=(run_metadata.st_atime_ns, run_metadata.st_mtime_ns))
    final_path.unlink()
    assert _artifacts(root) == before
    closed = _payload(_cli(root, "pr-review", "close", "--review-id", REVIEW_ID,
                           "--loop-id", LOOP_ID, "--expect-review-digest", reviewed["input_digest"], "--json"))
    assert closed["status"] == "closed" and closed["verdict"] == "fully_clean"
    assert first_path.read_bytes() == first


def test_v24_native_formal_original_publication_rechecks_normal_source(tmp_path, monkeypatch):
    from ai_sdlc.branch.git_client import GitError
    from ai_sdlc.core import pr_review_service as service

    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    original_formal_run = (directory / "review-run.json").read_bytes()
    _v24_native_repair(case)
    original = {name: (directory / name).read_bytes() for name in ("review-run.json", "review-pack.json")}
    calls = case["events"].read_bytes()
    intended = []

    def unavailable_before_publication(options):
        intended.append(list(options.test_results_refs))
        raise GitError("controlled ordinary pack publication failure")

    # 故障注入在已有 pack 发布入口；没有执行提供方，不记作一次真实 provider 技术失败。
    with monkeypatch.context() as paused:
        paused.setattr(service, "build_review_pack", unavailable_before_publication)
        interrupted = service.rerun_pr_review(root, provider_command=[sys.executable, str(case["failed"])])
    assert interrupted.status == "blocked" and "controlled ordinary pack" in interrupted.blocker
    assert len(intended) == 1 and any(ref.startswith("pr-formal-originals-v1:") for ref in intended[0])
    assert case["events"].read_bytes() == calls
    assert {name: (directory / name).read_bytes() for name in original} == original
    assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]

    history = json.loads((directory / "finding-history.json").read_bytes())
    assert history["formal_review_originals"]["run"].encode() == original_formal_run
    assert not json.loads(original["review-pack.json"])["test_results_refs"]
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    before = _artifacts(root)
    # verify 必须可消费仍由 R1 manifest 精确引用的原 pack；读取不补造或改写任何原件。
    service.read_pr_recovery_originals(root, run, pack).assert_unchanged()
    assert _artifacts(root) == before
    # 发布前失败仍可沿原入口完成发布；两个消费者使用真实新引用。
    recovered = _v24_native_rerun(case, case["clean"])
    assert recovered["status"] == "started" and recovered["verdict"] == "clean", recovered
    current_run, _ = service._load_current_review_run(root)
    current_pack = service._load_review_pack(root, current_run.review_pack_path)
    service.read_pr_recovery_originals(root, current_run, current_pack).assert_unchanged()
    assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]

    # 前次成功 rerun 已撤下修复记录；后续技术失败不能补造该原件取得续办资格。
    assert not (directory / "resolution.yaml").exists()
    _v24_native_failure(case)
    preserved = _artifacts(root)
    calls_after_failure = case["events"].read_bytes()
    refused = _v24_native_rerun(case, case["clean"])
    assert refused["status"] == "blocked"
    assert refused["blocker"].startswith("PR review originals cannot authorize continuation:")
    assert "trusted file is unavailable or uses a symlink" in refused["blocker"]
    assert "resolution.yaml" in refused["blocker"]
    assert case["events"].read_bytes() == calls_after_failure
    assert _artifacts(root) == preserved
    assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]


def _v25_native_close_current(case, reviewed):
    """同一输入经真实 commit/严格 Close 后，仍可读回且重复关闭。"""
    root, directory = case["root"], case["directory"]
    first_path = directory / "review-outcome-round-1.json"
    first = first_path.read_bytes()
    committed = _payload(_cli(root, "pr-review", "commit", "--message", "native formal delivery", "--json"))
    assert committed["tree_oid"] == _git(root, "rev-parse", "HEAD^{tree}")
    close_args = ("pr-review", "close", "--review-id", REVIEW_ID, "--loop-id", LOOP_ID,
                  "--expect-review-digest", reviewed["input_digest"], "--json")
    closed = _payload(_cli(root, *close_args))
    assert closed["status"] == "closed" and closed["verdict"] == "fully_clean"
    assert _payload(_cli(root, *close_args))["status"] == "closed"
    readback = _payload(_cli(root, "loop", "status", "--type", "local-pr-review", "--json"))
    assert readback["current_loop"]["status"] == "closed"
    assert readback["next_guidance"]["safety"] == "no_action"
    assert first_path.read_bytes() == first
    assert (directory / f"review-outcome-round-{reviewed['round_number']}.json").is_file()
    assert not (directory / f"review-outcome-round-{reviewed['round_number'] + 1}.json").exists()
    run = json.loads((directory / "review-run.json").read_bytes())
    assert run["delivery_commit"] == _git(root, "rev-parse", "HEAD")
    assert run.get("decision_started_at_ms") == case["initial_run"].get("decision_started_at_ms")
    return {"commit": committed, "close": closed, "readback": readback}


def test_fix24_native_legacy_formal_repair_reaches_strict_close(tmp_path, monkeypatch):
    from ai_sdlc.branch.git_client import GitError
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core import pr_review_service as service
    from tests.integration.test_cli_loop_review import _write_cli_expert_results

    case = _v24_native_legacy_formal_case(tmp_path, severity="important")
    root, directory = case["root"], case["directory"]
    first_path = directory / "review-outcome-round-1.json"
    run_path, pack_path = directory / "review-run.json", directory / "review-pack.json"
    original_run, original_pack = run_path.read_bytes(), pack_path.read_bytes()
    evidence_path = directory / "verification-evidence.json"
    original_evidence = evidence_path.read_bytes()
    first = json.loads(case["first_outcome"])
    assert first["round_number"] == 1 and first["status"] == "completed"
    assert first.get("simulation") is None and not (directory / "decision-context.json").exists()
    marker = root.with_name(root.name + "-verify-before-legacy-fix.txt")
    verified = _payload(_cli(
        root, "pr-review", "verify", "--json", "--", sys.executable, "-c",
        "from pathlib import Path;assert 'Saved output' in Path('README.md').read_text();"
        f"Path({str(marker)!r}).write_text('original verified')",
    ))
    assert verified["status"] == "ready" and marker.read_text() == "original verified"
    # 旧 R1 没有 manifest；verify 只更新未绑定的验证记录，不能改原 run/pack/outcome。
    assert evidence_path.read_bytes() != original_evidence
    assert run_path.read_bytes() == original_run and pack_path.read_bytes() == original_pack
    assert first_path.read_bytes() == case["first_outcome"]
    preview = _payload(_cli(root, "pr-review", "fix", "--dry-run", "--json"))
    fixed = _payload(_cli(root, "pr-review", "fix", "--json"))
    assert fixed["status"] == "ready" and fixed["round_number"] == preview["round_number"] == 1
    fixed_run = run_path.read_bytes()
    (root / "README.md").write_text("# Ordinary repaired\nCorrected ordinary output.\n")
    _git(root, "add", "README.md")
    intended = []
    original_findings = (directory / "findings.json").read_bytes()

    def unavailable_before_publication(options):
        intended.append(list(options.test_results_refs))
        raise GitError("controlled legacy pack publication failure")

    # 复用真实 pack 发布边界：先保全原 R1，尚未发布新 pack，不记作提供方失败。
    with monkeypatch.context() as paused:
        paused.setattr(service, "build_review_pack", unavailable_before_publication)
        interrupted = service.rerun_pr_review(root)
    assert interrupted.status == "blocked" and "controlled legacy pack" in interrupted.blocker
    assert len(intended) == 1 and any(ref.startswith("pr-formal-originals-v1:") for ref in intended[0])
    assert run_path.read_bytes() == fixed_run and pack_path.read_bytes() == original_pack
    assert (directory / "findings.json").read_bytes() == original_findings
    assert not any(ref.startswith("pr-formal-originals") for ref in json.loads(original_pack)["test_results_refs"])
    history_path = directory / "finding-history.json"
    saved = json.loads(history_path.read_bytes())["formal_review_originals"]
    assert saved["inputs"] == {} and saved["context"] is None
    assert saved["run"].encode() == fixed_run and saved["outcome"].encode() == case["first_outcome"]
    assert json.loads(saved["run"])["review_pack_digest"] == hashlib.sha256(original_pack).hexdigest()
    assert first_path.read_bytes() == case["first_outcome"]
    current_run, _ = service._load_current_review_run(root)
    current_pack = service._load_review_pack(root, current_run.review_pack_path)
    before = _artifacts(root)
    service.read_pr_recovery_originals(root, current_run, current_pack).assert_unchanged()
    assert _artifacts(root) == before

    changed_history = json.loads(history_path.read_bytes())
    changed_saved_run = json.loads(saved["run"])
    changed_saved_run["loop_id"] = "foreign-loop"
    changed_history["formal_review_originals"]["run"] = json.dumps(changed_saved_run)
    changed_run = json.loads(run_path.read_bytes())
    changed_run["review_pack_digest"] = "0" * 64
    changed_pack = json.loads(pack_path.read_bytes())
    changed_pack["review_id"] = "foreign-review"
    # 同一无新 pack 引用窗口内，旧 run/pack/R1 的原件约束仍须逐项拒绝损坏。
    for path, changed in (
        (history_path, json.dumps(changed_history).encode()),
        (run_path, json.dumps(changed_run).encode()),
        (pack_path, json.dumps(changed_pack).encode()),
        (first_path, case["first_outcome"] + b"\n"),
    ):
        original, metadata = path.read_bytes(), path.stat()
        try:
            path.write_bytes(changed)
            before = _artifacts(root)
            with pytest.raises(ValueError):
                run, _ = service._load_current_review_run(root)
                pack = service._load_review_pack(root, run.review_pack_path)
                service.read_pr_recovery_originals(root, run, pack)
            assert _artifacts(root) == before
        finally:
            path.write_bytes(original)
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    rerun = _payload(_cli(root, "pr-review", "rerun", "--json"))
    assert rerun["status"] == "started" and rerun["verdict"] == "clean", rerun
    assert json.loads(history_path.read_bytes())["formal_review_originals"] == saved
    assert first_path.read_bytes() == case["first_outcome"]
    current_run, _ = service._load_current_review_run(root)
    current_pack = service._load_review_pack(root, current_run.review_pack_path)
    service.read_pr_recovery_originals(root, current_run, current_pack).assert_unchanged()

    verified = _payload(_cli(
        root, "pr-review", "verify", "--json", "--", sys.executable, "-c",
        "from pathlib import Path;assert 'Corrected ordinary output.' in Path('README.md').read_text()",
    ))
    assert verified["status"] == "ready", verified
    second = _payload(_cli(root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json"))
    assert second["round_number"] == 2 and second["input_digest"] != case["first"]["input_digest"]
    args = ["loop", "review-record", "--type", "local-pr-review", "--loop-id", LOOP_ID,
            "--expect-digest", second["input_digest"], "--json"]
    for path in _write_cli_expert_results(directory, second):
        args.extend(["--result", str(path)])
    assert _payload(_cli(root, *args))["status"] == "passed"
    assert json.loads((directory / "review-outcome-round-2.json").read_bytes()).get("simulation") is None
    captured = {}
    reviewed = validate_review_input_for_close(
        root, loop_type="local-pr-review", loop_id=LOOP_ID,
        expected_digest=second["input_digest"], captured_artifacts=captured,
    )
    assert reviewed.input_digest == second["input_digest"]
    run, _ = service._load_current_review_run(root, reviewed_artifacts=captured)
    pack = service._load_review_pack(root, run.review_pack_path, reviewed_artifacts=captured)
    service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=captured).assert_unchanged()
    for path in (first_path, history_path, pack_path):
        key = path.relative_to(root).as_posix()
        assert captured[key] == path.read_bytes()
        incomplete = dict(captured)
        del incomplete[key]
        with pytest.raises((ValueError, FileNotFoundError)):
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=incomplete)
        assert path.read_bytes() == captured[key]
    # 负控恢复后完成唯一 R2 的真实交付，避免一个始终拒绝的 reader 蒙混过关。
    _v25_native_close_current(case, second)
    assert first_path.read_bytes() == case["first_outcome"]
    assert json.loads(history_path.read_bytes())["formal_review_originals"] == saved


def _v25_native_failed_expert_args(case):
    """只生成单专家协议输入；失败 outcome 和次数必须由原生 writer 产生。"""
    from tests.integration.test_stage_pr_review_pipeline import _expert_results

    root, reviewed = case["root"], case["first"]
    args = ["loop", "review-record", "--type", "local-pr-review", "--loop-id", LOOP_ID,
            "--expect-digest", reviewed["input_digest"], "--json"]
    results = _expert_results(root, reviewed, "PASS")
    for index, name in enumerate(results):
        payload = json.loads(Path(name).read_bytes())
        if index == len(results) - 1:
            payload["execution"].update(
                status="failed", failure_kind="reviewer-exited",
                failure_reason="受控专家协议夹具未完成；没有形成实际评分。", findings=[],
            )
            payload["assessment"] = None
        target = case["directory"] / f"host-first-expert-failure-{index}.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        args.extend(["--result", str(target)])
    return args


@pytest.mark.parametrize("retry_status", ["PASS", "failed"])
def test_v25_native_formal_expert_failure_keeps_same_input_retry(tmp_path, retry_status):
    from ai_sdlc.cli.loop_review_cmd import resolve_review_input
    from ai_sdlc.core import pr_review_service as service

    case = _v24_native_formal_case(tmp_path, first_status=None)
    root, directory, reviewed = case["root"], case["directory"], case["first"]
    args = _v25_native_failed_expert_args(case)
    recorded = _payload(_cli(root, *args))
    assert recorded["status"] == "failed", recorded
    first_path = directory / "review-outcome-round-1.json"
    original = first_path.read_bytes()
    failed = json.loads(original)
    assert failed["status"] == "failed" and failed.get("simulation") is None
    assert failed["infra_retry_count"] == 0 and failed["input_digest"] == reviewed["input_digest"]
    assert len(failed.get("completed_expert_results", [])) == len(reviewed["expert_roles"]) - 1
    # 原失败先保存到本测试的外部记录；重试仍由原 writer 更新同一个正式 round。
    (tmp_path / "first-native-expert-failure.json").write_bytes(original)
    retry = _payload(_cli(root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json"))
    assert retry["review_status"] == "failed" and retry["round_number"] == 1, retry
    assert retry["input_digest"] == reviewed["input_digest"]
    assert first_path.read_bytes() == original
    captured = {}
    resolved = resolve_review_input(root, loop_type="local-pr-review", loop_id=LOOP_ID,
                                    captured_artifacts=captured, capture_all=True)
    assert resolved.input_digest == reviewed["input_digest"]
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    context_path = directory / "decision-context.json"
    context_key = context_path.relative_to(root).as_posix()
    for key in (context_key, first_path.relative_to(root).as_posix()):
        missing = dict(captured)
        del missing[key]
        with pytest.raises((ValueError, OSError)):
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=missing)
    wrong_identity = json.loads(original)
    wrong_identity["loop_id"] = "other-loop"
    for target, changed in (
        (context_path, None), (first_path, json.dumps(wrong_identity).encode()),
        (root / "README.md", (root / "README.md").read_bytes() + b"Changed after failed expert.\n"),
    ):
        previous, metadata = target.read_bytes(), target.stat()
        if changed is None:
            target.unlink()
        else:
            target.write_bytes(changed)
        try:
            before = _artifacts(root)
            denied = _cli(root, *args)
            assert denied.returncode != 0, denied.stdout + denied.stderr
            assert _artifacts(root) == before
        finally:
            target.write_bytes(previous)
            # 原 context 由 mkstemp 原子封存；缺件负控重建后也须恢复摘要中的原权限。
            target.chmod(stat.S_IMODE(metadata.st_mode))
            os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        assert resolve_review_input(
            root, loop_type="local-pr-review", loop_id=LOOP_ID,
        ).input_digest == reviewed["input_digest"]
    assert first_path.read_bytes() == original
    before_head = _git(root, "rev-parse", "HEAD")
    for mode in (("--dry-run",), ()):
        before = _artifacts(root)
        rejected_fix = _cli(root, "pr-review", "fix", *mode, "--json")
        assert rejected_fix.returncode != 0, rejected_fix.stdout + rejected_fix.stderr
        assert json.loads(rejected_fix.stdout)["dry_run"] is bool(mode)
        assert _artifacts(root) == before and not (directory / "resolution.yaml").exists()
    before_commit = _artifacts(root)
    assert _cli(root, "pr-review", "commit", "--message", "must remain unreviewed", "--json").returncode != 0
    assert _artifacts(root) == before_commit
    assert _git(root, "rev-parse", "HEAD") == before_head and first_path.read_bytes() == original
    if retry_status == "PASS":
        completed = _record_actual(root, retry, "PASS")
        assert completed["status"] == "passed", completed
    else:
        exhausted = _payload(_cli(root, *args))
        assert exhausted["status"] in {"failed", "needs_user"}, exhausted
    final = first_path.read_bytes()
    final_payload = json.loads(final)
    assert final_payload["infra_retry_count"] == 1
    assert final_payload["input_digest"] == failed["input_digest"]
    assert _cli(root, *args).returncode != 0
    assert first_path.read_bytes() == final
    assert not (directory / "review-outcome-round-2.json").exists()
    if retry_status == "PASS":
        delivery = _v25_native_close_current(case, retry)
    else:
        status = _payload(_cli(root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json"))
        assert status["review_reason"] == "review-expert-retry-limit", status
        assert _cli(root, "pr-review", "close", "--review-id", REVIEW_ID, "--loop-id", LOOP_ID,
                    "--expect-review-digest", reviewed["input_digest"], "--json").returncode != 0
        assert first_path.read_bytes() == final and not (directory / "final-report.md").exists()
        delivery = {"status": status}
    assert (tmp_path / "first-native-expert-failure.json").read_bytes() == original
    _v12_save(tmp_path / "native-expert-retry-observation.json", {
        "first_outcome_hex": original.hex(), "final_outcome_hex": final.hex(),
        "retry_status": retry_status, "input_digest": reviewed["input_digest"], **delivery,
        "boundary": "真实原生协议失败与重试；合成专家输入不代表模型质量或业务收益。",
    })


def test_v25_native_fix_preview_preserves_mode_on_ready_and_rejections(tmp_path):
    case = _v24_native_formal_case(tmp_path, first_status=None)
    root, directory = case["root"], case["directory"]

    def preview(expected, *extra):
        before = _artifacts(root)
        result = _cli(root, "pr-review", "fix", "--dry-run", *extra, "--json")
        payload = json.loads(result.stdout)
        assert payload["status"] == expected, payload
        assert result.returncode == (0 if expected == "ready" else 1)
        assert payload["dry_run"] is True
        assert _artifacts(root) == before
        return payload

    assert preview("ready")["round_number"] == 1
    policy = _write_loop_policy(root, "max_rounds: [invalid]\n")
    try:
        preview("blocked")
    finally:
        policy.unlink()
    # 覆盖原函数的缺件/解析返回，以及量化共享 guard 的提前拒绝。
    for target, changed, expected in (
        (root / ".ai-sdlc/reviews/pr/current-review.json", None, "no_review"),
        (directory / "findings.json", None, "no_review"),
        (directory / "findings.json", b"{", "blocked"),
        (directory / "decision-context.json", None, "blocked"),
    ):
        original, metadata = target.read_bytes(), target.stat()
        if changed is None:
            target.unlink()
        else:
            target.write_bytes(changed)
        try:
            preview(expected)
        finally:
            target.write_bytes(original)
            os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    actual = _payload(_cli(root, "pr-review", "fix", "--json"))
    assert actual["status"] == "ready" and actual["dry_run"] is False
    assert actual["round_number"] == 1
    assert preview("needs_user", "--max-rounds", "1")["round_number"] == 1
    assert yaml.safe_load((directory / "resolution.yaml").read_bytes())["round_number"] == 1


def _assert_same_review_publication_recovery(root, directory, clean, monkeypatch):
    from ai_sdlc.core import loop_artifacts
    from ai_sdlc.core import pr_review_service as service

    originals = {name: (directory / name).read_bytes() for name in service._REPAIR_PACK_OUTPUTS}
    controls = {path: path.read_bytes() for path in directory.iterdir()
                if path.is_file() and path.name not in service._REPAIR_PACK_OUTPUTS}
    before_run = json.loads(controls[directory / "review-run.json"])
    real_replace, real_provider = loop_artifacts._replace_with_retry, service._run_provider
    faults, calls = [], []

    def fail_pack_once(source, destination):
        if Path(destination) == directory / "review-pack.json" and not faults:
            faults.append({name: (directory / name).read_bytes() for name in service._REPAIR_PACK_OUTPUTS})
            raise OSError("ordinary one-shot review-pack replacement failure")
        return real_replace(source, destination)

    def provider(*args, **kwargs):
        calls.append(True)
        return real_provider(*args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(loop_artifacts, "_replace_with_retry", fail_pack_once)
        fault.setattr(service, "_run_provider", provider)
        failed = service.rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert failed.status == "blocked" and "one-shot review-pack replacement" in failed.blocker, failed.model_dump_json()
    assert not calls and len(faults) == 1
    assert "new request" not in failed.next_action.lower()
    assert {name: (directory / name).read_bytes() for name in originals} == originals
    for path, raw in controls.items():
        if path.name == "finding-history.json" and path.read_bytes() != raw:
            # 原 pack 发布前，rerun 已真实保存前轮 findings；只允许该映射完成交接。
            old_history, history = json.loads(raw), json.loads(path.read_bytes())
            assert old_history["previous_findings_path"] == old_history["current_findings_path"]
            previous_path = root / history["previous_findings_path"]
            assert previous_path.name.startswith("previous-findings-round-")
            assert previous_path.read_bytes() == (directory / "findings.json").read_bytes()
            old_history["previous_findings_path"] = history["previous_findings_path"]
            assert history == old_history
        else:
            assert path.read_bytes() == raw
    publications = list((directory / "technical-failures").glob("publication-*"))
    assert len(publications) == 1
    archive = publications[0]
    manifest = json.loads((archive / "publication-manifest.json").read_bytes())
    assert "workspace_adoption_ref" not in manifest
    assert set(manifest["intended"]) == set(service._REPAIR_PACK_OUTPUTS)
    for name, raw in originals.items():
        assert (archive / ("before-" + name)).read_bytes() == raw
        assert (archive / ("observed-" + name)).read_bytes() == faults[0][name]
        assert hashlib.sha256((archive / ("intended-" + name)).read_bytes()).hexdigest() == manifest["intended"][name]
    assert json.loads((directory / "review-run.json").read_bytes()) == before_run
    return {path: path.read_bytes() for path in archive.iterdir()}


def test_same_review_publication_failure_preserves_originals_and_recovers(tmp_path, monkeypatch):
    from ai_sdlc.core import pr_review_service as service

    root, directory, _, clean = _unjudged_review(tmp_path, initially_clean=True)
    original_run = json.loads((directory / "review-run.json").read_bytes())
    (root / "README.md").write_text("# Changed same-scope implementation\nordinary rerun update\n")
    _git(root, "add", "README.md")
    saved = _assert_same_review_publication_recovery(root, directory, clean, monkeypatch)
    recovered = rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert recovered.status == "started" and recovered.verdict == "clean", recovered.model_dump_json()
    current, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, current.review_pack_path)
    service.read_pr_recovery_originals(root, current, pack).assert_unchanged()
    for key in ("review_id", "loop_id", "decision_staged_tree_oid", "decision_started_at_ms"):
        assert current.model_dump(mode="json").get(key) == original_run.get(key)
    assert pack.workspace_adoption_ref is None
    assert all(path.read_bytes() == raw for path, raw in saved.items())
    assert not (directory / "resolution-history.yaml").exists()


@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "legacy-direct-reuse"])
def test_new_review_publication_failure_restores_absence_and_reuses_id(tmp_path, monkeypatch, reuse):
    from ai_sdlc.core import pr_review_service as service

    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    (root / "README.md").write_text("# Fresh current implementation\n")
    _git(root, "add", "README.md")
    options = PRReviewStartOptions(root=root, base_ref="HEAD", diff_source="local-staged",
                                  provider_id="mock-reviewer", review_id="fresh-publication", loop_id="fresh-loop")
    directory = root / ".ai-sdlc/reviews/pr/fresh-publication"
    if reuse:
        assert start_pr_review(options).status == "started"
        assert fix_pr_review(root).status == "ready"
        service.close_pr_review(root)
        assert (directory / "resolution.yaml").is_file()
        assert (directory / "fix-plan.md").is_file()
        assert (directory / "final-report.md").is_file()
    originals = {name: (directory / name).read_bytes() if (directory / name).exists() else None
                 for name in service._REPAIR_PACK_OUTPUTS}
    with monkeypatch.context() as fault:
        observed = _weekend_fail_publication_writer(fault, directory, "before-middle", OSError("fresh publication IO"))
        result = start_pr_review(options)
    assert result.status == "blocked" and len(observed) == 1
    assert {name: (directory / name).read_bytes() if (directory / name).exists() else None
            for name in originals} == originals
    assert not any((directory / name).exists() for name in
                   ("resolution.yaml", "fix-plan.md", "final-report.md", "resolution-history.yaml"))
    if not reuse:
        assert not (directory / "review-run.json").exists()
    archive, = (directory / "technical-failures").glob("publication-*")
    manifest = json.loads((archive / "publication-manifest.json").read_bytes())
    assert set(manifest["before"]) == set(service._REPAIR_PACK_OUTPUTS)
    assert manifest["before"] == {name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                                  for name, raw in originals.items()}
    before = {path: path.read_bytes() for path in archive.iterdir()}
    recovered = start_pr_review(options)
    assert recovered.status == "started" and recovered.review_id == options.review_id, recovered.model_dump_json()
    assert all(path.read_bytes() == raw for path, raw in before.items())

@pytest.mark.parametrize("recovery_failure", ["archive", "rollback"])
def test_publication_recovery_io_preserves_available_originals_and_refuses_retry(tmp_path, monkeypatch, recovery_failure):
    from ai_sdlc.core import loop_artifacts
    from ai_sdlc.core import pr_review_service as service
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    root, directory, _, clean = _unjudged_review(tmp_path, initially_clean=True)
    originals = {name: (directory / name).read_bytes() for name in service._REPAIR_PACK_OUTPUTS}
    run_raw = (directory / "review-run.json").read_bytes()
    (root / "README.md").write_text("# Changed current implementation\n")
    _git(root, "add", "README.md")
    real_replace, real_write = loop_artifacts._replace_with_retry, LoopArtifactStore.write_bytes_artifact
    failed = {"pack": False, "recovery": False}

    def replace_once(source, destination):
        if Path(destination) == directory / "review-pack.json" and not failed["pack"]:
            failed["pack"] = True
            raise OSError("ordinary pack publication failure")
        return real_replace(source, destination)

    def recovery_write(store, path, *args, **kwargs):
        target = Path(path)
        selected = (target.name == "publication-manifest.json" if recovery_failure == "archive"
                    else target == directory / "source-resolution.json")
        if failed["pack"] and selected and not failed["recovery"]:
            failed["recovery"] = True
            raise OSError(f"ordinary {recovery_failure} write failure")
        return real_write(store, path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(loop_artifacts, "_replace_with_retry", replace_once)
        fault.setattr(LoopArtifactStore, "write_bytes_artifact", recovery_write)
        result = rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert failed == {"pack": True, "recovery": True}
    assert result.status == "blocked" and f"{recovery_failure} write failure" in result.next_action
    assert "Original review pack restored" not in result.next_action
    assert (directory / "review-run.json").read_bytes() == run_raw
    archive, = (directory / "technical-failures").glob("publication-*")
    for name, raw in originals.items():
        assert (archive / ("before-" + name)).read_bytes() == raw
    assert (archive / "publication-manifest.json").exists() == (recovery_failure == "rollback")
    before_retry = _artifacts(root)
    denied = rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
    assert denied.status == "blocked" and "current review diff" in denied.blocker
    assert _artifacts(root) == before_retry


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("next_action", ["repair", "risk_accept"])
def test_fix23_close_block_is_not_a_provider_failure(tmp_path, legacy, next_action):
    from ai_sdlc.core import pr_review_service as service

    root, directory, clean, _ = _weekend_protocol_review(tmp_path, exit_code=10, verdict="changes_required")
    invocation_path = directory / "reviewer-invocation.json"
    if legacy:
        invocation = json.loads(invocation_path.read_bytes())
        invocation.pop("completion_proof")
        invocation.pop("launch_status")
        invocation_path.write_text(json.dumps(invocation))
    original_invocation = invocation_path.read_bytes()
    original_findings = (directory / "findings.json").read_bytes()
    assert service.fix_pr_review(root, dry_run=True).status == "ready"
    blocked = service.close_pr_review(root)
    assert blocked.status == "blocked" and "REQUIRED" in blocked.blocker
    assert invocation_path.read_bytes() == original_invocation
    assert (directory / "findings.json").read_bytes() == original_findings
    assert service.fix_pr_review(root, dry_run=True).status == "ready"
    if next_action == "risk_accept":
        # 仅旧模式本就支持的显式风险接受；strict Close 的 REQUIRED 门禁不变。
        accepted = service.close_pr_review(root, require_no_blockers=True)
        assert accepted.status == "closed" and accepted.verdict == "risk_accepted", accepted.model_dump_json()
        assert invocation_path.read_bytes() == original_invocation
    else:
        fixed = service.fix_pr_review(root)
        assert fixed.status == "ready", fixed.model_dump_json()
        assert not (directory / "technical-failures").exists()
        rerun = service.rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
        assert rerun.status == "blocked" and "Unresolved PR review findings" in rerun.blocker
        (root / "README.md").write_text("# Current ordinary implementation\nRestored the expected ordinary output.\n")
        _git(root, "add", "README.md")
        resolution_path = directory / "resolution.yaml"
        resolution = yaml.safe_load(resolution_path.read_bytes())
        resolution["finding_resolutions"][0].update(
            status="fixed", reason="Restored the expected ordinary output in README.md.",
            evidence_refs=["README.md"], operator="test-author", resolved_at=service.utc_now_iso(),
        )
        resolution_path.write_text(yaml.safe_dump(resolution))
        rerun = service.rerun_pr_review(root, provider_command=[sys.executable, str(clean)])
        assert rerun.status == "started" and rerun.verdict == "clean", rerun.model_dump_json()
        assert not (directory / "technical-failures").exists()
        assert (directory / "previous-findings-round-2.json").read_bytes() == original_findings
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    service.read_pr_recovery_originals(root, run, pack).assert_unchanged()


@pytest.mark.parametrize("prior_scaffold", [False, True])
def test_fix25_native_formal_fix_preserves_bound_resolution_through_close(tmp_path, prior_scaffold):
    case = _v24_native_formal_case(tmp_path, first_status=None)
    root, directory = case["root"], case["directory"]
    resolution_path = directory / "resolution.yaml"
    if prior_scaffold:
        scaffold = _payload(_cli(root, "pr-review", "fix", "--json"))
        assert scaffold["status"] == "ready" and scaffold["round_number"] == 1
    original_resolution = resolution_path.read_bytes() if prior_scaffold else None
    # 首个 review 只有准备结果；scaffold 由原 fix 生成后，再原生记录唯一 R1。
    first = _payload(_cli(root, "loop", "review", "--type", "local-pr-review", "--loop-id", LOOP_ID, "--json"))
    assert first["round_number"] == 1
    assert _record_actual(root, first, "UNKNOWN")["status"] == "needs_fix"
    first_path = directory / "review-outcome-round-1.json"
    first_raw = first_path.read_bytes()
    case.update(first=first, first_outcome=first_raw)
    manifest = json.loads(first_raw)["simulation"]["manifest"]
    resolution_key = resolution_path.relative_to(root).as_posix()
    assert (resolution_key in manifest) is prior_scaffold
    if prior_scaffold:
        assert manifest[resolution_key] == hashlib.sha256(original_resolution).hexdigest()
    before = _artifacts(root)
    preview = _payload(_cli(root, "pr-review", "fix", "--dry-run", "--json"))
    assert preview["status"] == "ready" and _artifacts(root) == before
    assert preview["round_number"] == 1 + int(prior_scaffold)
    if prior_scaffold:
        metadata = resolution_path.stat()
        try:
            resolution_path.write_bytes(original_resolution + b"\n")
            before = _artifacts(root)
            refused = _cli(root, "pr-review", "fix", "--json")
            assert refused.returncode != 0 and _artifacts(root) == before
            assert first_path.read_bytes() == first_raw
        finally:
            resolution_path.write_bytes(original_resolution)
            os.utime(resolution_path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    fixed = _payload(_cli(root, "pr-review", "fix", "--json"))
    assert fixed["status"] == "ready" and fixed["round_number"] == preview["round_number"]
    history_path = directory / "finding-history.json"
    before_rerun_saved = (
        json.loads(history_path.read_bytes()).get("formal_review_originals")
        if history_path.exists() else None
    )
    (root / "README.md").write_text("# Repaired tree\nCorrected ordinary output.\n")
    _git(root, "add", "README.md")
    rerun = _v24_native_rerun(case, case["clean"])
    _v12_save(tmp_path / "bound-resolution-observation.json", {
        "prior_scaffold": prior_scaffold, "manifest": manifest,
        "original_resolution_hex": original_resolution.hex() if original_resolution is not None else None,
        "fixed": fixed, "rerun": rerun, "preserved_before_rerun": before_rerun_saved,
        "first_outcome_unchanged": first_path.read_bytes() == first_raw,
    })
    assert rerun["status"] == "started" and rerun["verdict"] == "clean", rerun
    saved = json.loads(history_path.read_bytes())["formal_review_originals"]
    assert saved["outcome"].encode() == first_raw
    assert set(saved["inputs"]) == set(manifest)
    assert all(hashlib.sha256(bytes.fromhex(saved["inputs"][key])).hexdigest() == digest
               for key, digest in manifest.items())
    if prior_scaffold:
        assert before_rerun_saved == saved
        assert bytes.fromhex(saved["inputs"][resolution_key]) == original_resolution
    assert not resolution_path.exists()
    second = _v24_native_second_review(case)
    _v25_native_close_current(case, second)
    assert first_path.read_bytes() == first_raw
    assert json.loads(history_path.read_bytes())["formal_review_originals"] == saved


@pytest.mark.parametrize("second_status", ["PASS", "FAIL"])
def test_verification_only_formal_r2_keeps_native_delivery_without_rerun(
    tmp_path, second_status
):
    _verification_only_formal_delivery_case(tmp_path, second_status)


def _verification_only_formal_delivery_case(tmp_path, second_status):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core import pr_review_service as service

    case = _v24_native_formal_case(tmp_path)
    provider_events_at_r1 = case["events"].read_bytes()
    assert [json.loads(line)["label"] for line in provider_events_at_r1.splitlines()] == ["clean"]
    root, directory = case["root"], case["directory"]
    first_path = directory / "review-outcome-round-1.json"
    history_path = directory / "finding-history.json"
    pack_path = directory / "review-pack.json"
    run_path = directory / "review-run.json"
    evidence_path = directory / "verification-evidence.json"
    original = {name: (directory / name).read_bytes() for name in (
        "review-outcome-round-1.json", "decision-context.json", "review-pack.json",
        "reviewer-invocation.json", "review-run.json", "verification-evidence.json",
    )}
    source = (root / "README.md").read_bytes()
    original_head, original_tree = _git(root, "rev-parse", "HEAD"), _git(root, "write-tree")
    marker = tmp_path / "additional-verification.txt"
    # 只补充实际验收证据，不通过改源码、fix 或 provider rerun 生成归档引用。
    for number in (1, 2):
        verified = _payload(_cli(
            root, "pr-review", "verify", "--json", "--", sys.executable, "-c",
            "from pathlib import Path;assert 'Current ordinary output.' in Path('README.md').read_text();"
            f"Path({str(marker)!r}).write_text({str(number)!r})",
        ))
        assert verified["status"] == "ready" and marker.read_text() == str(number)
        assert run_path.read_bytes() == original["review-run.json"]
    history = json.loads(history_path.read_bytes())
    saved = history["formal_review_originals"]
    evidence_key = evidence_path.relative_to(root).as_posix()
    assert bytes.fromhex(saved["inputs"][evidence_key]) == original["verification-evidence.json"]
    assert saved["run"].encode() == original["review-run.json"]
    assert evidence_path.read_bytes() != original["verification-evidence.json"]
    assert not any(ref.startswith("pr-formal-originals") for ref in json.loads(pack_path.read_bytes())["test_results_refs"])
    assert (root / "README.md").read_bytes() == source
    assert _git(root, "rev-parse", "HEAD") == original_head
    assert _git(root, "write-tree") == original_tree
    assert case["events"].read_bytes() == provider_events_at_r1

    second = _payload(_cli(root, "loop", "review", "--type", "local-pr-review",
                           "--loop-id", LOOP_ID, "--json"))
    assert second["round_number"] == 2 and second["input_digest"] != case["first"]["input_digest"]
    recorded = _record_actual(root, second, second_status)
    assert recorded["status"] == ("passed" if second_status == "PASS" else "needs_user")
    before_commit = run_path.read_bytes()
    committed = _cli(root, "pr-review", "commit", "--message", "verified unchanged output", "--json")
    close_args = ("pr-review", "close", "--review-id", REVIEW_ID, "--loop-id", LOOP_ID,
                  "--expect-review-digest", second["input_digest"], "--json")
    closed = _cli(root, *close_args)
    # 在断言之前保存真实命令输出及 commit 的副作用，原失败不能因后续断言丢失。
    _v12_save(tmp_path / "verification-only-native-delivery.json", {
        "second_status": second_status, "second_input_digest": second["input_digest"],
        "provider_events": case["events"].read_text(), "original_head": original_head,
        "original_tree": original_tree, "before_commit_run_hex": before_commit.hex(),
        "current_head": _git(root, "rev-parse", "HEAD"),
        "commit": {"exit_code": committed.returncode, "stdout": committed.stdout, "stderr": committed.stderr},
        "close": {"exit_code": closed.returncode, "stdout": closed.stdout, "stderr": closed.stderr},
        "current_run_hex": run_path.read_bytes().hex(),
        "original_R1_sha256": hashlib.sha256(case["first_outcome"]).hexdigest(),
        "saved_originals": saved,
    })
    if second_status == "FAIL":
        assert committed.returncode != 0 and json.loads(committed.stdout)["status"] == "blocked"
        assert closed.returncode != 0 and json.loads(closed.stdout)["status"] == "blocked"
        assert "Local PR expert review is not current and clean: review-round-limit" in json.loads(committed.stdout)["blocker"]
        assert json.loads(closed.stdout)["reason"] == "review-round-limit"
        assert _git(root, "rev-parse", "HEAD") == original_head
        assert not (directory / "final-report.md").exists()
    else:
        assert _payload(committed)["status"] == "ready"
        assert _payload(closed)["status"] == "closed"
        assert json.loads(closed.stdout)["verdict"] == "fully_clean"
        assert _payload(_cli(root, *close_args))["status"] == "closed"
        status = _payload(_cli(root, "loop", "status", "--type", "local-pr-review", "--json"))
        assert status["current_loop"]["status"] == "closed"
        proof = service.read_verified_delivery_commit(root)
        assert proof.current_commit == _git(root, "rev-parse", "HEAD")
        assert proof.reviewed_head == original_head and proof.staged_tree == original_tree
        assert proof.review_input_digest == second["input_digest"]
        captured = {}
        validated = validate_review_input_for_close(
            root, loop_type="local-pr-review", loop_id=LOOP_ID,
            expected_digest=second["input_digest"], captured_artifacts=captured,
        )
        assert validated.input_digest == second["input_digest"]
        run, _ = service._load_current_review_run(root, reviewed_artifacts=captured)
        pack = service._load_review_pack(root, run.review_pack_path, reviewed_artifacts=captured)
        service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=captured).assert_unchanged()
        # 缺捕获仍拒绝，不能把现场存在的历史或 Close 报告补回给调用方。
        report_path = directory / "final-report.md"
        for path in (history_path, first_path, pack_path, report_path, evidence_path):
            incomplete = dict(captured)
            del incomplete[path.relative_to(root).as_posix()]
            with pytest.raises((ValueError, OSError)):
                service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=incomplete)
        report_key = report_path.relative_to(root).as_posix()
        corrupted = dict(captured)
        corrupted[report_key] += b"unexpected report content\n"
        with pytest.raises(ValueError, match="formal review original reference"):
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=corrupted)
        # 同一个真 Close 捕获只变当前阶段字段，不新造历史，也不使用现场补齐。
        for field, replacement in (
            ("delivery_commit", original_head), ("delivery_parent_commit", proof.current_commit),
            ("next_action", "unexpected next action"), ("unresolved_required", 1),
            ("verdict", "risk_accepted"), ("final_report_digest", "0" * 64),
        ):
            corrupted = dict(captured)
            altered_payload = run.model_dump(mode="json")
            altered_payload[field] = replacement
            altered = type(run).model_validate(altered_payload)
            corrupted[run_path.relative_to(root).as_posix()] = (
                json.dumps(altered.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
            ).encode()
            with pytest.raises(ValueError, match="formal review original reference"):
                service.read_pr_recovery_originals(root, altered, pack, reviewed_artifacts=corrupted)
        _v12_save(tmp_path / "verification-only-close-consumed.json", {
            "commit": proof.current_commit, "reviewed_head": proof.reviewed_head,
            "staged_tree": proof.staged_tree, "input_digest": proof.review_input_digest,
            "capture_paths": sorted(captured), "status": "closed",
        })
    assert (root / "README.md").read_bytes() == source
    for name in ("review-outcome-round-1.json", "decision-context.json", "review-pack.json", "reviewer-invocation.json"):
        assert (directory / name).read_bytes() == original[name], name
    assert json.loads(history_path.read_bytes())["formal_review_originals"] == saved
    assert not (directory / "review-outcome-round-3.json").exists()
    assert case["events"].read_bytes() == provider_events_at_r1


def _assert_provider_retry_refused(root, directory, clean):
    marker = clean.with_name("unsupported-retry-must-not-start.txt")
    clean.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n" + clean.read_text())
    original = _artifacts(root)
    head, tree = _git(root, "rev-parse", "HEAD"), _git(root, "write-tree")
    result = _cli(root, "pr-review", "rerun", "--provider-command", _v24_native_provider_command(clean), "--json")
    payload = json.loads(result.stdout)
    assert result.returncode != 0 and payload["status"] == "blocked", payload
    assert ("unsupported" in payload["blocker"].lower()
            or "cannot authorize" in payload["blocker"].lower()), payload
    assert not marker.exists()
    assert _artifacts(root) == original
    assert _git(root, "rev-parse", "HEAD") == head and _git(root, "write-tree") == tree
    assert not (directory / "technical-failures").exists()
    _v12_save(root.parent / "unsupported-provider-retry.json", {
        "result": payload, "head": head, "tree": tree,
        "original_sha256": {path: hashlib.sha256(raw).hexdigest() for path, raw in original.items()},
        "provider_started": False,
    })


@pytest.mark.parametrize("after_valid_judgment", [False, True])
def test_technical_provider_failure_is_preserved_without_retry(tmp_path, after_valid_judgment):
    root, directory, failed, clean = _unjudged_review(tmp_path, initially_clean=after_valid_judgment)
    if after_valid_judgment:
        before = (directory / "findings.json").read_bytes()
        result = rerun_pr_review(root, provider_command=[sys.executable, str(failed)])
        assert result.status == "blocked" and result.verdict is None
        assert any(path.read_bytes() == before for path in directory.glob("previous-findings-round-*.json"))
    original = json.loads((directory / "reviewer-invocation.json").read_bytes())
    assert original["exit_code"] == 20 and original["status"] == "blocked"
    assert original["completion_proof"]
    _assert_provider_retry_refused(root, directory, clean)


def test_necessary_provider_resumption_preserves_formal_chain(tmp_path):
    from ai_sdlc.core import pr_review_service as service

    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    _v24_native_repair(case)
    failure = _v24_native_failure(case)
    second_failure = _v24_native_failure(case)
    originals = _artifacts(root)
    resumed = _v24_native_rerun(case, case["clean"])
    assert resumed["status"] == "started" and resumed["verdict"] == "clean", resumed
    assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]
    archived = list((directory / "technical-failures").glob("provider-*/reviewer-invocation.json"))
    assert len(archived) == 2
    assert {path.read_bytes() for path in archived} == {failure["invocation"], second_failure["invocation"]}
    for name in ("previous-findings-round-2.json", "previous-review-run-round-2.json"):
        key = (directory / name).relative_to(root).as_posix()
        assert (directory / name).read_bytes() == originals[key]
    reviewed = _v24_native_second_review(case)
    result = _v25_native_close_current(case, reviewed)
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    capture = service.read_pr_recovery_originals(root, run, pack).originals
    service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture).assert_unchanged()
    archived_key = archived[0].relative_to(root).as_posix()
    assert capture[archived_key] in {failure["invocation"], second_failure["invocation"]}
    with pytest.raises(ValueError, match="missing-from-capture"):
        service.read_pr_recovery_originals(
            root, run, pack, reviewed_artifacts={key: value for key, value in capture.items() if key != archived_key},
        )
    _v12_save(tmp_path / "necessary-provider-resumption.json", {
        "technical_failure": failure["result"], "resumed": resumed,
        "delivery": result, "provider_events": case["events"].read_text().splitlines(),
        "preserved_invocation_sha256": hashlib.sha256(failure["invocation"]).hexdigest(),
        "capture_paths": sorted(capture),
    })
    assert [json.loads(line)["label"] for line in case["events"].read_text().splitlines()] == [
        "clean", "technical", "technical", "clean",
    ]
    assert (directory / "decision-context.json").read_bytes() == case["context"]


def _native_postlaunch_exception(case, exception_type):
    """只在真实 CLI 内注入宿主异常并观察；不代写 provider 原件或正式状态。"""
    from textwrap import dedent

    from tests.integration.test_quantified_implementation import _env

    directory = case["directory"]
    receipt = case["events"].with_name(f"postlaunch-{exception_type}.json")
    injector = receipt.with_suffix(".py")
    injector.write_text(dedent(r'''
        import hashlib
        import json
        import sys
        from pathlib import Path

        import ai_sdlc.cli.pr_review_cmd as command
        from ai_sdlc.cli.main import app
        from ai_sdlc.core import quality_command as quality
        from ai_sdlc.core.loop_artifacts import LoopArtifactStore

        configuration = json.loads(sys.argv[1])
        cli_args = sys.argv[2:]
        directory = Path(configuration["directory"])
        fault = {"RuntimeError": RuntimeError, "KeyboardInterrupt": KeyboardInterrupt, "OSError": OSError}[
            configuration["exception_type"]
        ]("injected actual postlaunch host failure")
        observed = {"injected_count": 0, "originals_sha256": {}, "temporary_roots": []}
        owned, readers = [], []
        real_write = LoopArtifactStore.write_bytes_artifact
        real_json_write = LoopArtifactStore.write_json_artifact
        real_popen = quality.subprocess.Popen
        real_start = quality.threading.Thread.start
        real_read = quality._digest_and_tail
        real_rerun = command.rerun_pr_review

        def write_original(store, path, content, **kwargs):
            result = real_write(store, path, content, **kwargs)
            path = Path(path)
            if (path.parent.name.startswith("ai-sdlc-provider-owned-")
                    and path.name in {"process.json", "raw-result.json", "cleanup.json"}):
                parent = str(path.parent)
                if parent not in observed["temporary_roots"]:
                    observed["temporary_roots"].append(parent)
                observed["originals_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            return result

        def launch(argv, *args, **kwargs):
            process = real_popen(argv, *args, **kwargs)
            if isinstance(argv, (tuple, list)) and quality._CONTROLLED_LAUNCHER in argv:
                owned.append(process)
            return process

        def write_schema(store, path, payload):
            if (configuration["exception_type"] == "OSError"
                    and Path(path) == directory / "schema-validation.json"
                    and not observed["injected_count"]):
                invocation = json.loads((directory / "reviewer-invocation.json").read_bytes())
                assert invocation["exit_code"] == 0 and invocation["launch_status"] == "started"
                assert "execution_failure" not in invocation
                assert len(observed["originals_sha256"]) == 3
                observed["findings_sha256_before_exception"] = hashlib.sha256(
                    (directory / "findings.json").read_bytes()
                ).hexdigest()
                observed["injected_count"] += 1
                observed["injection_point"] = "first-schema-report-write-after-actual-exit-zero-and-findings"
                raise fault
            return real_json_write(store, path, payload)

        def start_reader(thread, *args, **kwargs):
            target = getattr(thread, "_target", None)
            if (configuration["exception_type"] == "RuntimeError"
                    and getattr(target, "__module__", "") == quality.__name__
                    and getattr(target, "__name__", "") == "capture"
                    and not observed["injected_count"]):
                assert len(owned) == 1 and "process.json" in observed["originals_sha256"]
                # 本例验证可恢复的完整生命周期；先确认真实线程启动，再注入宿主异常。
                real_start(thread, *args, **kwargs)
                readers.append(thread)
                observed["injected_count"] += 1
                observed["injection_point"] = "capture-thread-started-after-owned-process-original"
                raise fault
            return real_start(thread, *args, **kwargs)

        def read_output(stream, *args, **kwargs):
            path = Path(stream.name)
            if (configuration["exception_type"] == "KeyboardInterrupt"
                    and path.name == "stdout" and path.parent.name.startswith("ai-sdlc-provider-owned-")
                    and not observed["injected_count"]):
                raw = json.loads((path.parent / "raw-result.json").read_bytes())
                assert raw["exit_code"] == 0 and (path.parent / "cleanup.json").is_file()
                observed["findings_sha256_before_exception"] = hashlib.sha256(
                    (directory / "findings.json").read_bytes()
                ).hexdigest()
                observed["injected_count"] += 1
                observed["injection_point"] = "stdout-read-after-actual-exit-zero-and-findings"
                raise fault
            return real_read(stream, *args, **kwargs)

        def observe_service_exception(*args, **kwargs):
            try:
                return real_rerun(*args, **kwargs)
            except BaseException as error:
                # Typer 在最外层将中断映射为退出码 130；服务边界必须仍抛原异常对象。
                observed["service_exception_type"] = type(error).__name__
                observed["service_exception_is_original"] = error is fault
                raise

        LoopArtifactStore.write_bytes_artifact = write_original
        LoopArtifactStore.write_json_artifact = write_schema
        quality.subprocess.Popen = launch
        quality.threading.Thread.start = start_reader
        quality._digest_and_tail = read_output
        command.rerun_pr_review = observe_service_exception
        sys.argv = ["ai-sdlc", *cli_args]
        try:
            app()
        finally:
            observed["capture_readers_stopped"] = [not thread.is_alive() for thread in readers]
            observed["owned"] = [
                {"pid": process.pid, "returncode": process.returncode,
                 "pipes_closed": {name: getattr(process, name) is None or getattr(process, name).closed
                                  for name in ("stdin", "stdout", "stderr")}}
                for process in owned
            ]
            observed["temporary_directories_removed"] = bool(observed["temporary_roots"]) and all(
                not Path(path).exists() for path in observed["temporary_roots"]
            )
            Path(configuration["receipt"]).write_text(json.dumps(observed, indent=2), encoding="utf-8")
    '''), encoding="utf-8")
    argv = [sys.executable, "-B", str(injector), json.dumps({
        "directory": str(directory), "receipt": str(receipt), "exception_type": exception_type,
    }), "pr-review", "rerun", "--provider-command", _v24_native_provider_command(case["clean"]), "--json"]
    result = subprocess.run(argv, cwd=case["root"], env=_env(), capture_output=True,
                            text=True, encoding="utf-8", timeout=90, check=False)
    observation = json.loads(receipt.read_bytes())
    _v12_save(receipt.with_name(receipt.stem + "-command.json"), {
        "argv": argv, "exit_code": result.returncode, "stdout": result.stdout,
        "stderr": result.stderr, "observation": observation,
    })
    assert result.returncode != 0, result.stdout + result.stderr
    assert observation["injected_count"] == 1
    assert observation["service_exception_is_original"] is True
    assert observation["service_exception_type"] == exception_type
    if exception_type == "RuntimeError":
        assert observation["capture_readers_stopped"] == [True]
    assert observation["temporary_directories_removed"] is True
    assert len(observation["owned"]) == 1
    assert observation["owned"][0]["returncode"] is not None
    assert all(observation["owned"][0]["pipes_closed"].values())
    return observation


@pytest.mark.parametrize("exception_types", [
    ("RuntimeError", "KeyboardInterrupt"), ("OSError",),
], ids=["execution", "schema-report"])
def test_postlaunch_provider_exceptions_preserve_native_formal_delivery(tmp_path, exception_types):
    from ai_sdlc.core import pr_review_service as service
    from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation, ReviewVerdict

    case = _v24_native_formal_case(tmp_path)
    root, directory = case["root"], case["directory"]
    repair = _v24_native_repair(case)
    original_head, original_tree = _git(root, "rev-parse", "HEAD"), _git(root, "write-tree")
    failures = []
    diagnostic = None
    previous = None
    for exception_type in exception_types:
        old_pack = (directory / "review-pack.json").read_bytes()
        old_invocation = (directory / "reviewer-invocation.json").read_bytes()
        observed = _native_postlaunch_exception(case, exception_type)
        # 调用进程已结束；只从磁盘重新建立身份，不从注入器内存补回原件。
        run, _ = service._load_current_review_run(root)
        pack = service._load_review_pack(root, run.review_pack_path)
        invocation_bytes = (directory / "reviewer-invocation.json").read_bytes()
        invocation = ProviderRunnerInvocation.model_validate_json(invocation_bytes)
        assert invocation_bytes != old_invocation
        assert (directory / "review-pack.json").read_bytes() != old_pack
        assert run.status == "blocked" and run.verdict is None and run.findings_digest == ""
        assert run.review_id == REVIEW_ID and run.loop_id == LOOP_ID
        assert run.review_pack_digest == hashlib.sha256((directory / "review-pack.json").read_bytes()).hexdigest()
        assert run.head_commit == original_head and run.staged_tree_oid == original_tree
        assert run.decision_started_at_ms == case["initial_run"].get("decision_started_at_ms")
        assert invocation.launch_status == "started" and invocation.status == "blocked"
        assert invocation.execution_failure.exception_type == exception_type
        assert invocation.execution_failure.message == "injected actual postlaunch host failure"
        assert invocation.workspace_check.status == "unchanged"
        assert invocation.workspace_check.review_pack_digest == run.review_pack_digest
        assert invocation.completion_proof.sha256 == observed["originals_sha256"]
        process, raw, cleanup = invocation.completion_proof.verified_receipts()
        assert process["pid"] == observed["owned"][0]["pid"]
        assert raw["exit_code"] == invocation.exit_code == observed["owned"][0]["returncode"]
        assert cleanup["status"] == "complete"
        assert process["started_at_ms"] == raw["started_at_ms"] <= raw["ended_at_ms"] <= cleanup["checked_at_ms"]
        assert (directory / "review-outcome-round-1.json").read_bytes() == case["first_outcome"]
        assert (directory / "decision-context.json").read_bytes() == case["context"]
        assert (directory / "resolution.yaml").read_bytes() == repair["resolution.yaml"]
        assert (directory / "fix-plan.md").read_bytes() == repair["fix-plan.md"]
        if previous is None:
            previous = {name: (directory / name).read_bytes() for name in (
                "previous-findings-round-2.json", "previous-review-run-round-2.json",
            )}
        assert {name: (directory / name).read_bytes() for name in previous} == previous
        recovered = service.read_pr_rerun_originals(root, run, pack)
        assert recovered.technical_failure and recovered.findings.verdict == "clean"
        recovered.reader.assert_unchanged()
        failures.append(invocation_bytes)
        findings_path = directory / "findings.json"
        if exception_type == "RuntimeError":
            assert invocation.execution_failure.findings_status == "absent"
            assert not findings_path.exists()
            replacements = [("inserted-after-failure", previous["previous-findings-round-2.json"],
                             "provider diagnostic original presence changed")]
            findings_before = None
        else:
            assert raw["exit_code"] == 0
            assert invocation.execution_failure.findings_status == "present"
            diagnostic = findings_path.read_bytes()
            assert hashlib.sha256(diagnostic).hexdigest() == observed["findings_sha256_before_exception"]
            assert invocation.execution_failure.findings_sha256 == hashlib.sha256(diagnostic).hexdigest()
            assert json.loads(diagnostic)["verdict"] == "clean"
            assert recovered.reader.originals[findings_path.relative_to(root).as_posix()] == diagnostic
            # 即使残留 JSON 看似 clean，也没有正常判断或提交资格。
            with pytest.raises(ValueError, match="Current findings.json verdict no longer matches the reviewer run"):
                service.read_pr_recovery_originals(root, run, pack).assert_unchanged()
            # 即使消费者只把 BLOCKED 当完成状态，真实异常原件也不能取得完成资格。
            with pytest.raises(ValueError, match="provider execution failure cannot authorize a completed review"):
                service._validate_completed_provider_invocation(
                    root, run, pack, invocation_original=invocation_bytes,
                    expected_verdict=ReviewVerdict.BLOCKED,
                )
            before = _artifacts(root)
            refused = _cli(root, "pr-review", "fix", "--dry-run", "--json")
            assert refused.returncode != 0 and json.loads(refused.stdout)["status"] == "blocked"
            assert _artifacts(root) == before
            replacements = [
                ("deleted-diagnostic", None, "provider diagnostic original presence changed"),
                ("changed-diagnostic", diagnostic + b" ", "provider diagnostic original bytes changed"),
            ]
            findings_before = diagnostic
        events_before = case["events"].read_bytes()
        for label, replacement, reason in replacements:
            try:
                if replacement is None:
                    findings_path.unlink()
                else:
                    findings_path.write_bytes(replacement)
                before = _artifacts(root)
                refused = _cli(root, "pr-review", "rerun", "--provider-command",
                               _v24_native_provider_command(case["clean"]), "--json")
                payload = json.loads(refused.stdout)
                assert refused.returncode != 0 and payload["status"] == "blocked", (label, payload)
                assert reason in payload["blocker"], (label, payload)
                assert case["events"].read_bytes() == events_before and _artifacts(root) == before
            finally:
                if findings_before is None:
                    findings_path.unlink(missing_ok=True)
                else:
                    findings_path.write_bytes(findings_before)
    resumed = _v24_native_rerun(case, case["clean"])
    assert resumed["status"] == "started" and resumed["verdict"] == "clean", resumed
    archived = list((directory / "technical-failures").glob("provider-*/reviewer-invocation.json"))
    assert {path.read_bytes() for path in archived} == set(failures) and len(archived) == len(exception_types)
    assert diagnostic is not None
    diagnostics = list((directory / "technical-failures").glob("provider-*/findings.json"))
    assert len(diagnostics) == 1 and diagnostics[0].read_bytes() == diagnostic
    assert {name: (directory / name).read_bytes() for name in previous} == previous
    second = _v24_native_second_review(case)
    delivery = _v25_native_close_current(case, second)
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    capture = service.read_pr_recovery_originals(root, run, pack).originals
    service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture).assert_unchanged()
    for path in (archived[0], diagnostics[0]):
        key = path.relative_to(root).as_posix()
        assert capture[key] == path.read_bytes()
        with pytest.raises(ValueError, match="missing-from-capture"):
            service.read_pr_recovery_originals(
                root, run, pack, reviewed_artifacts={k: v for k, v in capture.items() if k != key},
            )
    assert [json.loads(line)["label"] for line in case["events"].read_text().splitlines()] == ["clean", "clean", "clean"]
    assert (directory / "decision-context.json").read_bytes() == case["context"]
    _v12_save(tmp_path / "postlaunch-native-delivery.json", {
        "failure_invocation_sha256": [hashlib.sha256(raw).hexdigest() for raw in failures],
        "diagnostic_findings_sha256": hashlib.sha256(diagnostic).hexdigest(),
        "failure_duration_ms": [
            (proof := ProviderRunnerInvocation.model_validate_json(raw).completion_proof.require_complete())["ended_at_ms"]
            - proof["started_at_ms"] for raw in failures
        ],
        "resumed": resumed, "delivery": delivery, "capture_paths": sorted(capture),
        "provider_events": case["events"].read_text().splitlines(),
    })


def test_necessary_provider_resumption_preserves_required_and_exact_repair_scope(tmp_path):
    from ai_sdlc.core import pr_review_service as service
    from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation

    root, script, clean_source, options = _v12_case(tmp_path)
    started = start_pr_review(options)
    assert started.status == "started" and started.verdict == "changes_required"
    directory = Path(started.review_run_path).parent
    _v12_fix(root, directory)
    (root / "README.md").write_text("Corrected ordinary result.\n")
    _git(root, "add", "README.md")
    required_again = _cli(root, "pr-review", "rerun", "--json")
    assert required_again.returncode == 10 and json.loads(required_again.stdout)["verdict"] == "changes_required"
    assert _v12_fix(root, directory).round_number == 2
    failed = script.with_name("technical.py")
    failed.write_text("raise SystemExit(23)\n")
    failed_process = _cli(root, "pr-review", "rerun", "--provider-command", _v24_native_provider_command(failed), "--json")
    assert failed_process.returncode == 1
    payload = json.loads(failed_process.stdout)
    assert payload["status"] == "blocked" and payload["verdict"] is None
    raw_failure = (directory / "reviewer-invocation.json").read_bytes()
    invocation = ProviderRunnerInvocation.model_validate_json(raw_failure)
    assert invocation.completion_proof is not None
    assert invocation.completion_proof.require_complete()["exit_code"] == 23
    previous = directory / "previous-findings-round-3.json"
    previous_bytes = previous.read_bytes()
    assert json.loads(previous_bytes)["findings"][0]["id"] == "REQ-1"
    marker = tmp_path / "resumed-provider.txt"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n" + clean_source)
    resolution_path = directory / "resolution.yaml"
    resolution = yaml.safe_load(resolution_path.read_bytes())
    unresolved = json.loads(json.dumps(resolution))
    unresolved["finding_resolutions"][0]["status"] = "unresolved"
    original_run_path = directory / "previous-review-run-round-3.json"
    old_run = json.loads(original_run_path.read_bytes())
    damaged_invocation = json.loads(raw_failure)
    damaged_invocation["completion_proof"]["sha256"]["cleanup.json"] = "0" * 64
    run_path = directory / "review-run.json"
    current_run = json.loads(run_path.read_bytes())
    rejected_results = {}
    for label, path, changed, reason in (
        ("missing-previous", previous, None, "previous-findings-round-3"),
        ("changed-previous", previous, previous_bytes + b" ", "original digest changed"),
        ("foreign-previous", original_run_path, json.dumps({**old_run, "loop_id": "foreign-loop"}).encode(), "identity"),
        ("unresolved", resolution_path, yaml.safe_dump(unresolved).encode(), "unresolved BLOCKER/REQUIRED"),
        ("older-judgment", resolution_path, yaml.safe_dump({**resolution, "round_number": 1}).encode(), "newer preserved judgment"),
        ("corrupt-proof", directory / "reviewer-invocation.json", json.dumps(damaged_invocation).encode(), "provider-completion-original-drift"),
        ("candidate-drift", run_path, json.dumps({**current_run, "staged_tree_oid": "0" * 40}).encode(), "root pack identity"),
    ):
        original, metadata = path.read_bytes(), path.stat()
        try:
            path.unlink() if changed is None else path.write_bytes(changed)
            before = _artifacts(root)
            rejected = json.loads(_cli(root, "pr-review", "rerun", "--provider-command", _v24_native_provider_command(script), "--json").stdout)
            assert rejected["status"] == "blocked" and reason in rejected["blocker"], (label, rejected)
            assert not marker.exists() and _artifacts(root) == before
            rejected_results[label] = rejected
        finally:
            path.write_bytes(original)
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    dependency = root / "dependency.py"
    dependency.write_text("def corrected_result(): return 'correct'\n")
    _git(root, "add", "dependency.py")
    request = {
        "schema_version": "1", "artifact_kind": "pr-repair-scope-input", "request_id": "technical-dependency",
        "review_id": options.review_id, "loop_id": options.loop_id, "head_commit": _git(root, "rev-parse", "HEAD"),
        "review_pack_sha256": hashlib.sha256((directory / "review-pack.json").read_bytes()).hexdigest(),
        "findings_sha256": hashlib.sha256(previous_bytes).hexdigest(),
        "resolution_sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
        "resolution_round": resolution["round_number"], "staged_tree_oid": _git(root, "write-tree"),
        "dependencies": [{"path": "dependency.py", "finding_id": "REQ-1",
            "blob_sha256": hashlib.sha256(dependency.read_bytes()).hexdigest(),
            "reason": "The original correction needs this ordinary helper."}],
    }
    request_path = tmp_path / "technical-scope.json"
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    read = service.read_pr_rerun_originals(root, run, pack)
    assert read.technical_failure and read.findings.verdict == "changes_required"
    assert read.previous_findings_path == previous.relative_to(root).as_posix()
    assert run.verdict is None and not (directory / "findings.json").exists()
    request_path.write_text(json.dumps({**request, "findings_sha256": "0" * 64}))
    rejected_scope = _repair_scope_rerun(root, request_path, script)
    assert rejected_scope.status == "blocked" and "findings.json SHA256 mismatch" in rejected_scope.blocker
    assert not marker.exists() and not (directory / "technical-failures").exists()
    request_path.write_text(json.dumps(request))
    result = _cli(root, "pr-review", "rerun", "--repair-scope-input", str(request_path),
        "--repair-scope-sha256", hashlib.sha256(request_path.read_bytes()).hexdigest(),
        "--provider-command", _v24_native_provider_command(script), "--json")
    resumed = _payload(result)
    assert resumed["status"] == "started" and resumed["verdict"] == "clean", resumed
    assert marker.read_text() == "ran" and previous.read_bytes() == previous_bytes
    audit = json.loads((directory / "repair-scope/technical-dependency/audit.json").read_bytes())
    assert audit["findings_source_path"] == previous.relative_to(root).as_posix()
    new_pack = json.loads((directory / "review-pack.json").read_bytes())
    assert new_pack["changed_files"] == ["README.md", "dependency.py"]
    archived = list((directory / "technical-failures").glob("provider-*/reviewer-invocation.json"))
    assert len(archived) == 1 and archived[0].read_bytes() == raw_failure
    _v12_save(tmp_path / "necessary-provider-required-scope.json", {
        "failure": payload, "rejections": rejected_results, "scope_rejected": rejected_scope.model_dump(),
        "resumed": resumed, "history_source": read.previous_findings_path,
        "preserved_failure_sha256": hashlib.sha256(raw_failure).hexdigest(), "new_pack": new_pack,
    })


def test_necessary_provider_resumption_rejects_actual_head_drift(tmp_path):
    root, script, clean_source, options = _v12_case(tmp_path)
    started = start_pr_review(options)
    directory = Path(started.review_run_path).parent
    _v12_fix(root, directory)
    script.write_text("raise SystemExit(23)\n")
    failed = rerun_pr_review(root)
    assert failed.status == "blocked" and failed.verdict is None
    original = _artifacts(root)
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "write-tree")
    marker = tmp_path / "must-wait-for-original-head.txt"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n" + clean_source)
    _git(root, "commit", "--allow-empty", "-m", "Different source head")
    rejected = json.loads(_cli(root, "pr-review", "rerun", "--json").stdout)
    assert rejected["status"] == "blocked" and "head_ref does not match reviewed head_commit" in rejected["blocker"]
    assert not marker.exists() and _artifacts(root) == original
    _git(root, "reset", "--soft", head)
    assert _git(root, "write-tree") == tree
    resumed = _payload(_cli(root, "pr-review", "rerun", "--json"))
    assert resumed["verdict"] == "clean" and marker.read_text() == "ran"
    _v12_save(tmp_path / "necessary-provider-head-lineage.json", {"rejected": rejected, "resumed": resumed, "head": head, "tree": tree})


@pytest.mark.parametrize(("point", "rerun", "provider_id"), [
    ("after-seal", False, "local-agent"),
    ("after-seal", True, "local-agent"),
    ("before-provider-read", False, "local-agent"),
    ("before-launch", False, "local-agent"),
    ("after-seal", False, "mock-reviewer"),
])
def test_published_review_bytes_remain_bound_until_provider_launch(tmp_path, monkeypatch, point, rerun, provider_id):
    from ai_sdlc.core import pr_review_service as service

    root, script, clean_source, options = _v12_case(tmp_path)
    marker = tmp_path / "provider-really-launched.txt"
    if rerun:
        first = start_pr_review(options)
        assert first.verdict == "changes_required"
        directory = Path(first.review_run_path).parent
        _v12_fix(root, directory)
        previous = (directory / "findings.json").read_bytes()
    else:
        directory = root / ".ai-sdlc/reviews/pr" / options.review_id
        previous = None
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n" + clean_source)
    options = replace(options, provider_id=provider_id, current_model="mock-reviewer" if provider_id == "mock-reviewer" else "gpt-5")
    published, changed = {}, {}

    def replace_pair():
        assert not changed
        diff_path, pack_path = directory / "diff.patch", directory / "review-pack.json"
        diff = diff_path.read_bytes() + b"\n"
        payload = json.loads(pack_path.read_bytes())
        payload["diff_digest"] = "sha256:" + hashlib.sha256(diff).hexdigest()
        diff_path.write_bytes(diff)
        pack_path.write_text(json.dumps(payload))
        changed.update({name: (directory / name).read_bytes() for name in ("diff.patch", "review-pack.json")})
        assert hashlib.sha256(diff).hexdigest() == payload["diff_digest"].removeprefix("sha256:")

    real_seal = service._ReviewPackPublication.seal_pack
    real_provider = service.run_provider_command
    real_require = pr_review_provider._require_provider_executable

    def sealed(owner, result):
        real_seal(owner, result)
        published.update({path.name: raw for path, raw in owner.published.items()})
        if point == "after-seal":
            replace_pair()

    def provider_call(config):
        if point == "before-provider-read":
            replace_pair()
        return real_provider(config)

    def require_executable(*args, **kwargs):
        result = real_require(*args, **kwargs)
        if point == "before-launch":
            replace_pair()
        return result

    monkeypatch.setattr(service._ReviewPackPublication, "seal_pack", sealed)
    monkeypatch.setattr(service, "run_provider_command", provider_call)
    monkeypatch.setattr(pr_review_provider, "_require_provider_executable", require_executable)
    result = rerun_pr_review(root) if rerun else start_pr_review(options)
    _v12_save(tmp_path / "publication-handoff-result.json", {
        "point": point, "rerun": rerun, "provider_id": provider_id,
        "result": result.model_dump(), "provider_really_launched": marker.exists(),
        "published_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in published.items()},
        "changed_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in changed.items()},
    })
    assert published and changed and all(published[name] != raw for name, raw in changed.items())
    assert not marker.exists(), result.model_dump_json()
    assert result.status == "blocked" and "published review pack changed before provider launch" in result.blocker, result.model_dump_json()
    assert result.verdict is None
    if (directory / "findings.json").exists():
        # 分派前拒绝可保留旧诊断原件，但新 run 不得引用它作为当前结果。
        assert previous is not None and (directory / "findings.json").read_bytes() == previous
        assert json.loads((directory / "review-run.json").read_bytes())["findings_path"] == ""
    assert all((directory / name).read_bytes() == raw for name, raw in changed.items())
    if previous is not None:
        assert (directory / "previous-findings-round-2.json").read_bytes() == previous
        assert yaml.safe_load((directory / "resolution.yaml").read_bytes())["round_number"] == 1
    if provider_id == "local-agent":
        invocation = json.loads((directory / "reviewer-invocation.json").read_bytes())
        assert invocation["launch_status"] == "never_started" and invocation["preflight_incomplete"]
        assert "completion_proof" not in invocation
        assert invocation["workspace_check"]["status"] == "unproven"


@pytest.mark.parametrize("provider_id", ["local-agent", "mock-reviewer"])
def test_unchanged_published_review_allows_normal_start_and_rerun(tmp_path, monkeypatch, provider_id):
    from ai_sdlc.core import pr_review_service as service

    root, script, clean_source, options = _v12_case(tmp_path)
    marker = tmp_path / "provider-call-count.txt"
    script.write_text(f"from pathlib import Path\np=Path({str(marker)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n" + clean_source)
    options = replace(options, provider_id=provider_id, current_model="mock-reviewer" if provider_id == "mock-reviewer" else "gpt-5")
    observed = []
    original = service._ReviewPackPublication.assert_published

    def check(owner):
        original(owner)
        observed.append({path.name: hashlib.sha256(raw).hexdigest() for path, raw in owner.published.items()})

    monkeypatch.setattr(service._ReviewPackPublication, "assert_published", check)
    first = start_pr_review(options)
    assert first.status == "started" and first.verdict == "clean"
    first_guard_count = len(observed)
    second = rerun_pr_review(root)
    assert second.status == "started" and second.verdict == "clean", second.model_dump_json()
    assert first_guard_count == (2 if provider_id == "local-agent" else 1)
    assert len(observed) == 2 * first_guard_count
    assert marker.read_text() == "xx" if provider_id == "local-agent" else not marker.exists()
    _v12_save(tmp_path / "publication-handoff-normal.json", {
        "first": first.model_dump(), "second": second.model_dump(), "guard_snapshots": observed,
    })


def test_rejected_scope_feedback_is_preserved_without_retry(tmp_path):
    root, script, clean_source, options = _v12_case(tmp_path, "extra.txt")
    first = start_pr_review(options)
    assert first.status == "blocked" and first.verdict is None
    directory = Path(first.review_run_path).parent
    assert (directory / "findings.json").exists()
    script.write_text(clean_source)
    _assert_provider_retry_refused(root, directory, script)


def test_mutated_provider_workspace_has_no_adoption_or_delivery_path(tmp_path):
    from ai_sdlc.core import pr_review_service as service
    root, directory, clean, failed, _, _ = _fix18_mutated_clean_review(tmp_path)
    assert failed.status == "blocked" and failed.verdict is None
    raw = (directory / "reviewer-invocation.json").read_bytes()
    original = _artifacts(root)
    invocation = json.loads(raw)
    assert invocation["workspace_check"]["status"] == "mutated"
    run, _ = service._load_current_review_run(root)
    pack = service._load_review_pack(root, run.review_pack_path)
    for capture in (None, original):
        with pytest.raises(ValueError):
            service.read_pr_recovery_originals(root, run, pack, reviewed_artifacts=capture)
    assert service.fix_pr_review(root, dry_run=True).status == "blocked"
    marker = tmp_path / "must-not-verify.txt"
    verified = service.verify_pr_review_command(root, cwd=".", argv=(sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).write_text('ran')"))
    assert verified.status == "blocked" and not marker.exists()
    assert service.commit_pr_review(root, message="must not deliver mutated provider").status == "blocked"
    assert service.close_pr_review(root).status == "blocked"
    assert _artifacts(root) == original
    _assert_provider_retry_refused(root, directory, clean)
    assert (directory / "reviewer-invocation.json").read_bytes() == raw


@pytest.mark.parametrize("entry,field", [
    ("start", "provider_workspace_adoption_ref"), ("start", "rejected_feedback_ref"),
    ("provider", "workspace_adoption_ref"), ("provider", "snapshot_require_complete"),
    ("pack", "workspace_adoption_ref"), ("pack", "rejected_feedback_ref"),
])
def test_withdrawn_recovery_options_rejected_before_creation(tmp_path, entry, field):
    from ai_sdlc.core.pr_review_provider import (
        ProviderCommandOptions,
        run_provider_command,
    )
    root = tmp_path / "not-created"
    value = True if field == "snapshot_require_complete" else {"path": "old/request.json", "sha256": "0" * 64}
    if entry == "start":
        result = start_pr_review(PRReviewStartOptions(root=root, base_ref="main", **{field: value}))
    elif entry == "provider":
        result = run_provider_command(ProviderCommandOptions(root=root, review_pack_path=root / "review-pack.json", command=["must-not-start"], **{field: value}))
    else:
        result = build_review_pack(ReviewPackBuildOptions(root=root, base_ref="main", review_id="old", loop_id="old-loop", **{field: value}))
    assert result.status == "blocked" and "unsupported" in result.blocker.lower()
    assert not root.exists()



def test_native_non_ascii_source_names_and_diff_keep_original_bytes(tmp_path):
    from ai_sdlc.core import pr_review_pack as pack
    from ai_sdlc.core.pr_review_models import SourceAdapterResolution
    from ai_sdlc.core.pr_review_redaction import analyze_redaction
    from tests.unit.test_pr_review_pack import (
        _git,
        _init_repo_with_base_commit,
        _write_file,
    )

    _init_repo_with_base_commit(tmp_path)
    relative = "docs/发布 说明.md"
    _write_file(tmp_path, relative, "original public guidance\n")
    _git(tmp_path, "add", "--", relative)
    _git(tmp_path, "commit", "-m", "normal public original")
    _write_file(tmp_path, relative, "current public guidance\n")
    _git(tmp_path, "add", "--", relative)
    tree = _git(tmp_path, "write-tree").strip()
    source = SourceAdapterResolution(source_kind="local-staged", adapter_id="local-staged",
        source_id="local-staged", repo_root=str(tmp_path), base_ref="HEAD", head_ref="INDEX",
        base_commit=_git(tmp_path, "rev-parse", "HEAD").strip(),
        head_commit=_git(tmp_path, "rev-parse", "HEAD").strip(),
        staged_tree_oid=tree, access_status="resolved")
    resolved = pack.resolve_review_input_for_source(tmp_path, source)
    assert resolved.changed_files == [relative]
    assert resolved.base_file_bytes[relative] == b"original public guidance\n"
    assert resolved.source_file_bytes[relative] == b"current public guidance\n"
    report = analyze_redaction(tmp_path, resolved.changed_files,
        head_file_bytes=resolved.source_file_bytes, base_file_bytes=resolved.base_file_bytes)
    assert report.included_files == [relative]
    assert not report.omitted_files and not report.redacted_files
    assert pack._patch_changed_files(resolved.diff_text) == [relative]
    assert pack._filter_patch_diff(resolved.diff_text, [relative]) == resolved.diff_text
    assert set(pack._diff_file_blobs(resolved.diff_text)) == {relative}
    _git(tmp_path, "commit", "-m", "normal public update")
    assert pack._git_changed_files(tmp_path, "HEAD^", "HEAD") == [relative]
    _git(tmp_path, "rm", "--", relative)
    _git(tmp_path, "commit", "-m", "normal public removal")
    assert pack._git_deleted_files(tmp_path, "HEAD^", "HEAD") == [relative]
    import pytest
    with pytest.raises(pack.GitError, match="Malformed Git quoted patch path"):
        pack._normalize_patch_path(r'"a/\777.md"')



def test_fixed_exception_diagnostic_keeps_strict_source_and_base_boundaries(tmp_path):
    from ai_sdlc.core.pr_review_redaction import analyze_redaction
    sample = 'secret-token' + "=" + '/private/ci/runner'
    source = ("raise OSError(" + repr(sample) + ")\n").encode()
    unknown = ("raise OSError(" + repr(sample + "-different") + ")\n").encode()
    direct_assignment = ("token" + " = " + repr("a-credential-value") + "\n").encode()
    path = "src/diagnostic.py"
    allowed = analyze_redaction(tmp_path, [path], head_file_bytes={path: source})
    assert allowed.included_files == [path]
    for denied in (
        unknown,
        direct_assignment,
        ("raise ValueError(" + repr(sample) + ")\n").encode(),
        ("message = " + repr(sample) + "\n").encode(),
        ("raise OSError(" + repr(sample) + ") from error\n").encode(),
        source + direct_assignment,
    ):
        report = analyze_redaction(tmp_path, [path], head_file_bytes={path: denied})
        assert report.redacted_files == [path]
    historical = analyze_redaction(tmp_path, [path], head_file_bytes={path: source},
        base_file_bytes={path: unknown})
    assert historical.redacted_files == [path]
    fixed_historical = analyze_redaction(tmp_path, [path], head_file_bytes={path: b"pass\n"},
        base_file_bytes={path: source})
    assert fixed_historical.included_files == [path]

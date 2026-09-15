"""真实工件、源快照、持久尝试与故障窗口，不以构造 verdict 代替执行。"""

import copy
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_sdlc.core import counterexample_execution as execution
from ai_sdlc.core.counterexample_models import (
    AttemptReceipt,
    CounterexampleEvidenceRecord,
    CounterexamplePlan,
    VerificationContract,
    counterexample_digest,
    source_digest_sha256,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.pr_review_service import VerifiedDeliveryCommit
from ai_sdlc.core.quality_command import build_source_digest
from tests.unit.test_counterexample_models import make_contract, plan_data


@pytest.mark.parametrize(
    "damage",
    [
        "process-fields",
        "cleanup-fields",
        "process-time",
        "cleanup-time",
        "raw-exit",
        "completion-missing",
    ],
)
def test_ce003_v10_cold_recovery_requires_original_completion(execution_case, damage):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    folder = (root / receipt.attempt_ref.path).parent
    if damage == "completion-missing":
        (folder / "completion.json").unlink(missing_ok=True)
    else:
        name = {
            "process-fields": "process.json",
            "cleanup-fields": "cleanup.json",
            "process-time": "process.json",
            "cleanup-time": "cleanup.json",
            "raw-exit": "raw-result.json",
        }[damage]
        path = folder / name
        body = json.loads(path.read_bytes())
        if damage == "process-fields":
            body = {"ownership_nonce": receipt.ownership_nonce}
        elif damage == "cleanup-fields":
            body = {"ownership_nonce": receipt.ownership_nonce, "status": "complete"}
        elif damage == "process-time":
            body["started_at_ms"] += 1
        elif damage == "cleanup-time":
            body["checked_at_ms"] += 1
        else:
            body["exit_code"] = 7
        path.write_text(json.dumps(body))
    try:
        recovered = execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref
        )
    except ValueError:
        return
    assert recovered.status != "completed", "缺失或变化的完成原件不能恢复为成功"


def test_ce003_v10_complete_receipt_binds_process_and_cleanup(execution_case):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    folder = (root / receipt.attempt_ref.path).parent
    manifest = json.loads((folder / "completion.json").read_bytes())
    assert manifest["intent_sha256"] == receipt.attempt_ref.sha256
    for name in ("process.json", "raw-result.json", "cleanup.json"):
        assert (
            manifest["artifact_sha256"][name]
            == hashlib.sha256((folder / name).read_bytes()).hexdigest()
        )
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )


def test_ce003_v10_legacy_receipt_remains_unknown_without_backfill(execution_case):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    folder = (root / receipt.attempt_ref.path).parent
    (folder / "completion.json").unlink(missing_ok=True)
    before = {
        path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()
    }
    recovered = execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref
    )
    assert recovered.status == "execution_unknown"
    assert recovered.cleanup_status == "unknown"
    assert {
        path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()
    } == before


def test_ce003_v10_captured_completion_cannot_fallback_to_live(execution_case):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    completion = next(path for path in captured if path.endswith("/completion.json"))
    captured.pop(completion)
    assert (root / completion).is_file()
    recovered = execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    )
    assert recovered.status == "execution_unknown"
    assert recovered.cleanup_status == "unknown"


@pytest.mark.parametrize("malformed", [[], None], ids=["array", "null"])
def test_ownership_shape_rejected_consistently_by_receipt_and_state(execution_case, monkeypatch, malformed):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    assert receipt.normally_completed
    impl = _started_history_input(execution_case, monkeypatch)
    progress = SimpleNamespace(tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])])
    baseline, _, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False,
        active_plan_digest=counterexample_digest(plan),
    )
    assert not baseline
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    ownership = (root / receipt.attempt_ref.path).with_name("resource-ownership.json")
    key = ownership.relative_to(root).as_posix()
    original = ownership.read_bytes()
    assert isinstance(json.loads(original), dict)
    malformed_bytes = json.dumps(malformed).encode()
    ownership.write_bytes(malformed_bytes)
    captured[key] = malformed_bytes
    preserved = {path: path.read_bytes() for path in ownership.parent.iterdir() if path.is_file()}
    unexpected = []
    for name, reader in (
        ("receipt-live", lambda: execution._receipt(root, plan, receipt.attempt_ref)),
        ("recover-captured", lambda: execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured)),
    ):
        try:
            reader()
        except ValueError as error:
            assert "resource-ownership-original-invalid" in str(error)
        except Exception as error:
            unexpected.append((name, type(error).__name__, str(error)))
        else:
            unexpected.append((name, "accepted-malformed-ownership"))
    for name, material in (("state-live", None), ("state-captured", captured)):
        try:
            blockers, _, _ = execution.counterexample_verification_state(
                root, impl, progress, captured_artifacts=material,
                require_r2=False, require_completion=False,
                active_plan_digest=counterexample_digest(plan),
            )
        except Exception as error:
            unexpected.append((name, type(error).__name__, str(error)))
        else:
            assert any("resource-ownership-original-invalid" in item for item in blockers)
    assert all(path.read_bytes() == content for path, content in preserved.items())
    assert ownership.read_bytes() == malformed_bytes
    assert not unexpected


def test_ce003_v10_completion_from_other_attempt_is_rejected(execution_case):
    root, _, plan = execution_case
    first = _execute(execution_case)
    second = _execute(execution_case, "current-V0")
    first_folder = (root / first.attempt_ref.path).parent
    second_folder = (root / second.attempt_ref.path).parent
    (first_folder / "completion.json").write_bytes(
        (second_folder / "completion.json").read_bytes()
    )
    with pytest.raises(ValueError, match="completion-originals"):
        execution.recover_counterexample_attempt(root, plan, first.attempt_ref)


def test_ce003_v10_proven_launch_failure_is_known_and_not_replayed(
    execution_case, monkeypatch
):
    from ai_sdlc.core import quality_command as quality

    root, _, plan = execution_case
    _started_history_input(execution_case, monkeypatch)
    original_popen = quality.subprocess.Popen
    calls = []

    def cannot_launch(*args, **kwargs):
        if (kwargs.get("env") or {}).get(quality._PROCESS_OWNER_ENV):
            calls.append("failed-construction")
            raise OSError("controlled process creation temporarily unavailable")
        return original_popen(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(quality.subprocess, "Popen", cannot_launch)
        receipt = _execute(execution_case)
    assert calls == ["failed-construction"]
    assert receipt.status == "infrastructure_error"
    assert receipt.cleanup_status == "complete"
    folder = (root / receipt.attempt_ref.path).parent
    assert not (folder / "process.json").exists()
    assert (
        json.loads((folder / "raw-result.json").read_bytes())["launch_status"]
        == "never_started"
    )
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    assert execution.counterexample_execution_started(root, plan.loop_id)
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert _execute(execution_case) == receipt
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1
    cleaned = _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()


@pytest.mark.parametrize(
    "document", ["process.json", "raw-result.json", "cleanup.json", "postcheck.json"]
)
@pytest.mark.parametrize("missing_completion", [False, True])
def test_ce003_v10_non_object_receipt_is_a_controlled_read_error(
    execution_case, document, missing_completion
):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    key = next(path for path in captured if path.endswith("/" + document))
    captured[key] = b"[]"
    if missing_completion:
        key = next(path for path in captured if path.endswith("/completion.json"))
        captured.pop(key)
    with pytest.raises(ValueError):
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )


@pytest.mark.parametrize("damage", ["missing-fields", "reversed-clock"])
def test_ce003_v10_invalid_hot_cleanup_keeps_owned_resources(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    for step_id in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, step_id).status == "completed"
    real_run = execution.run_quality_command

    def incomplete_proof(options):
        callback = options.controlled.on_cleanup

        def persist_invalid(payload):
            changed = (
                {"ownership_nonce": payload["ownership_nonce"], "status": "complete"}
                if damage == "missing-fields"
                else {**payload, "checked_at_ms": 1}
            )
            callback(changed)

        return real_run(
            replace(
                options,
                controlled=replace(options.controlled, on_cleanup=persist_invalid),
            )
        )

    monkeypatch.setattr(execution, "run_quality_command", incomplete_proof)
    with pytest.raises(ValueError):
        _execute(execution_case, "current-cleanup")
    attempts = list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    cleanup_intent = next(
        path for path in attempts if json.loads(path.read_bytes())["kind"] == "cleanup"
    )
    assert Path(plan.steps[0].binding.resources[0].root).is_dir()
    assert not (cleanup_intent.parent / "resource-cleanup.json").exists()
    assert (cleanup_intent.parent / "postcheck-error.json").is_file()


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "missing-live",
        "missing-captured",
        "wrong-attempt",
        "wrong-initial",
        "wrong-actual",
        "wrong-shape",
    ],
)
def test_reset_cold_recovery_requires_frozen_initial_proof(execution_case, damage):
    case = _same_resource_reset_case(execution_case, "restored")
    root, _, plan = case
    assert _execute(case, "current-explore").status == "completed"
    receipt = _execute(case, "current-reset")
    assert receipt.status == "completed"
    folder = (root / receipt.attempt_ref.path).parent
    reset_path = (folder / "reset-state.json").relative_to(root).as_posix()
    postcheck_path = (folder / "postcheck.json").relative_to(root).as_posix()
    refs = list(execution.attempt_artifact_refs(root, receipt.attempt_ref))
    for resource in plan.steps[0].binding.resources:
        refs.extend(
            execution.resource_initial_artifact_refs(root, resource.initial_state)
        )
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    captured[reset_path] = (root / reset_path).read_bytes()
    if damage is None:
        for project in {step.binding.project_root for step in plan.steps}:
            shutil.rmtree(project)
        recovered = execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )
        assert recovered == receipt
        assert any(ref.path == reset_path for ref in receipt.raw_evidence_refs)
        return
    if damage == "missing-live":
        (root / reset_path).unlink()
        captured = None
    elif damage == "missing-captured":
        captured.pop(reset_path)
        assert (root / reset_path).is_file()
    else:
        proof = json.loads(captured[reset_path])
        if damage == "wrong-attempt":
            proof["attempt_id"] = "another-attempt"
        elif damage == "wrong-initial":
            proof["expected"] = proof["actual"] = []
        elif damage == "wrong-actual":
            proof["actual"] = []
        else:
            proof = []
        captured[reset_path] = json.dumps(proof).encode()
        # 读取器也独立核对冻结初态，不能只接受记录器自洽的摘要。
        postcheck = json.loads(captured[postcheck_path])
        postcheck["reset_state_sha256"] = hashlib.sha256(
            captured[reset_path]
        ).hexdigest()
        captured[postcheck_path] = json.dumps(postcheck).encode()
    try:
        recovered = execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )
    except ValueError:
        return
    assert recovered.status != "completed"


@pytest.fixture
def execution_case(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text("scratch/\n")
    (root / "src").mkdir()
    (root / "src/save.py").write_text("VALUE='saved'\n")
    (root / "tests").mkdir()
    (root / "observe.py").write_text(
        "import json,pathlib,sys\n"
        "value=json.loads(pathlib.Path(sys.argv[1]).read_text())['value']\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':value}}))\n"
    )
    for name in ("v0", "v1"):
        (root / f"tests/{name}.py").write_text(
            "import json\nprint(json.dumps({'schema_version':1,'assertion_id':'saved-state',"
            "'reached':True,'assertion_result':'accepted','failure_reason':'none'}))\n"
        )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "candidate")
    contract = make_contract()
    data = plan_data(contract)
    data["candidate_digest"] = source_digest_sha256(build_source_digest(root))
    data["v0_digest"] = hashlib.sha256((root / "tests/v0.py").read_bytes()).hexdigest()
    data["v1_digest"] = hashlib.sha256((root / "tests/v1.py").read_bytes()).hexdigest()
    data["protected_paths"] = ["tests/v0.py", "tests/v1.py", "observe.py"]
    folder = ".ai-sdlc/loops/implementation/sample-implementation/counterexamples"
    for witness in data["witnesses"]:
        # 纯模型的摘要占位不能充当真实历史输入；状态读取需消费实际冻结字节。
        witness_ref = execution._write_json(
            root,
            root / folder / f"witness-{witness['id']}.json",
            {
                "schema_version": 1,
                "input_value": witness["input_value"],
                "premise_values": witness["premise_values"],
            },
        )
        witness["input_ref"] = witness_ref.model_dump(mode="json")
    initial_content = b'{"value":"saved"}'
    initial = {
        "schema_version": 1,
        "files": [
            {
                "path": "result.json",
                "sha256": hashlib.sha256(initial_content).hexdigest(),
            }
        ],
    }
    initial_ref = execution._write_json(root, root / folder / "initial.json", initial)
    snapshots = {}
    for subject in data["subjects"]:
        target = tmp_path / subject["id"]
        _git(tmp_path, "clone", "-q", str(root), str(target))
        if subject["role"] == "variant":
            (target / "src/save.py").write_text("VALUE='lost'\n")
        resource = target / "scratch"
        resource.mkdir()
        (resource / "result.json").write_bytes(initial_content)
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=f"{folder}/snapshots/{subject['id']}",
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        manifest_ref = execution._write_json(
            root, root / folder / f"{subject['id']}-snapshot.json", manifest
        )
        subject.update(
            candidate_digest=manifest["source_digest"],
            parent_candidate_digest=data["candidate_digest"],
            snapshot=manifest_ref.model_dump(),
        )
        snapshots[subject["id"]] = manifest
        for step in (
            step for step in data["steps"] if step["subject_id"] == subject["id"]
        ):
            environment = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
            step["binding"].update(
                project_root=str(target),
                argv=[
                    sys.executable,
                    "observe.py"
                    if step["kind"] in {"observe", "cleanup"}
                    else f"tests/{step['acceptance_version'].lower()}.py",
                    str(resource / "result.json"),
                    "request",
                ],
                effective_environment=environment,
                environment_digest=counterexample_digest(environment),
                timeout_seconds=2,
                resources=[
                    {
                        "id": "data",
                        "permission_id": "data",
                        "kind": "directory",
                        "root": str(resource),
                        "initial_state": initial_ref.model_dump(),
                        "observation_endpoint": str(resource / "result.json"),
                        "cleanup_method": "owned_directory",
                    }
                ],
            )
            if step["kind"] == "observe":
                step["binding"]["witness_input"] = {
                    "witness_id": "input",
                    "argv_index": 3,
                    "encoding": "text",
                }
    before = next(
        entry["sha256"]
        for entry in snapshots["current"]["files"]
        if entry["path"] == "src/save.py"
    )
    after = next(
        entry["sha256"]
        for entry in snapshots["variant"]["files"]
        if entry["path"] == "src/save.py"
    )
    patch_ref = execution._write_json(
        root,
        root / folder / "variant-patch.json",
        {
            "schema_version": 1,
            "changes": [
                {"path": "src/save.py", "before_sha256": before, "after_sha256": after}
            ],
        },
    )
    data["subjects"][1]["patch"] = patch_ref.model_dump()
    return root, contract, CounterexamplePlan.model_validate(data)


def _execute(case, step_id="current-none", **kwargs):
    root, contract, plan = case
    return execution.execute_counterexample_attempt(
        root,
        contract,
        plan,
        step_id,
        deadline_ms=kwargs.pop("deadline_ms", time.time_ns() // 1_000_000 + 10_000_000),
        **kwargs,
    )


def _history_with_two_controls(case):
    root, contract, original = case
    data = original.model_dump(mode="json")
    control = copy.deepcopy(data["subjects"][-1])
    control["id"] = "second_control"
    data["subjects"].append(control)
    data["subjects"][1]["positive_control_refs"].append("second_control")
    for step in list(data["steps"]):
        if step["subject_id"] != "positive_control":
            continue
        repeated = copy.deepcopy(step)
        repeated["id"] = step["id"].replace("positive_control", "second_control")
        repeated["subject_id"] = "second_control"
        if repeated.get("business_observation_step_id"):
            repeated["business_observation_step_id"] = repeated[
                "business_observation_step_id"
            ].replace("positive_control", "second_control")
        repeated["depends_on"] = [
            identifier.replace("positive_control", "second_control")
            for identifier in step["depends_on"]
        ]
        data["steps"].append(repeated)
    data["max_execution_attempts"] = 48
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(contract, plan)
    folder, _ = execution._plan_history(root, plan)
    execution._write_json(
        root,
        folder / f"{counterexample_digest(plan)}.json",
        plan.model_dump(mode="json"),
    )
    return root, contract, plan


@pytest.mark.parametrize("change", ["remove_control", "witness", "patch", "command"])
def test_same_candidate_history_rejects_selective_new_batch(execution_case, change):
    root, contract, prior = _history_with_two_controls(execution_case)
    data = prior.model_dump(mode="json")
    data["id"] = "selective-retry"
    if change == "remove_control":
        data["subjects"] = [s for s in data["subjects"] if s["id"] != "second_control"]
        data["subjects"][1]["positive_control_refs"] = ["positive_control"]
        data["steps"] = [
            s for s in data["steps"] if s["subject_id"] != "second_control"
        ]
    elif change == "witness":
        data["witnesses"][0]["input_value"]["value"] = "easier-input"
        for step in data["steps"]:
            binding = step["binding"].get("witness_input")
            if binding is not None:
                step["binding"]["argv"][binding["argv_index"]] = "easier-input"
    elif change == "command":
        data["steps"][0]["binding"]["argv"].append("easier-mode")
    else:
        variant = data["subjects"][1]
        target = Path(
            next(
                s for s in prior.steps if s.subject_id == "variant"
            ).binding.project_root
        )
        (target / "src/save.py").write_text("VALUE='different-error'\n")
        snapshot = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=".ai-sdlc/loops/implementation/sample-implementation/replaced-variant",
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        variant["snapshot"] = execution._write_json(
            root,
            root / ".ai-sdlc/loops/implementation/sample-implementation/replaced.json",
            snapshot,
        ).model_dump()
        variant["candidate_digest"] = snapshot["source_digest"]
        patch = json.loads((root / variant["patch"]["path"]).read_bytes())
        patch["changes"][0]["after_sha256"] = hashlib.sha256(
            (target / "src/save.py").read_bytes()
        ).hexdigest()
        variant["patch"] = execution._write_json(
            root,
            root
            / ".ai-sdlc/loops/implementation/sample-implementation/replaced-patch.json",
            patch,
        ).model_dump()
    changed = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(contract, changed)
    with pytest.raises(
        ValueError, match="controls-or-witnesses|batch-content|execution-table"
    ):
        execution._plan_history(root, changed)
    assert not execution._attempts_dir(root, prior).exists()


def test_same_candidate_unchanged_batch_keeps_original_history(execution_case):
    root, _, prior = _history_with_two_controls(execution_case)
    renamed = prior.model_copy(update={"id": "same-batch-view"})
    folder, history = execution._plan_history(root, renamed)
    assert history == [prior]
    assert len(list(folder.glob("*.json"))) == 1
    assert not execution._attempts_dir(root, prior).exists()


def _rebase_batch(
    case, prior, *, changed_subject=None, changed_content=None, preserve_roots=False
):
    root, contract, _ = case
    repaired = "VALUE='saved'\nREPAIRED=True\n"
    (root / "src/save.py").write_text(repaired)
    _git(root, "add", "src/save.py")
    data = prior.model_dump(mode="json")
    data["candidate_digest"] = source_digest_sha256(build_source_digest(root))
    folder = root / ".ai-sdlc/loops/implementation/sample-implementation/rebased"
    for subject in data["subjects"]:
        target = (
            Path(next(s.binding.project_root for s in prior.steps if s.subject_id == subject["id"]))
            if preserve_roots
            else root.parent / ("rebased-" + subject["id"])
        )
        if not preserve_roots:
            _git(root.parent, "clone", "-q", str(root), str(target))
        (target / "src/save.py").write_text(repaired)
        _git(target, "add", "src/save.py")
        content = (
            "VALUE='lost'\nREPAIRED=True\n"
            if subject["role"] == "variant"
            else repaired
        )
        if subject["id"] == changed_subject:
            content = changed_content
        (target / "src/save.py").write_text(content)
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=(folder / subject["id"]).relative_to(root).as_posix(),
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        subject.update(
            candidate_digest=manifest["source_digest"],
            parent_candidate_digest=data["candidate_digest"],
            snapshot=execution._write_json(
                root, folder / f"{subject['id']}.json", manifest
            ).model_dump(),
            modified_paths=["src/save.py"] if content != repaired else [],
            patch=None,
        )
        if content != repaired:
            subject["patch"] = execution._write_json(
                root,
                folder / f"{subject['id']}-patch.json",
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "path": "src/save.py",
                            "before_sha256": hashlib.sha256(
                                repaired.encode()
                            ).hexdigest(),
                            "after_sha256": hashlib.sha256(
                                content.encode()
                            ).hexdigest(),
                        }
                    ],
                },
            ).model_dump()
        for step in data["steps"]:
            if step["subject_id"] != subject["id"]:
                continue
            old_root = step["binding"]["project_root"]
            step["binding"] = json.loads(
                json.dumps(step["binding"]).replace(old_root, str(target))
            )
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(contract, plan)
    execution.validate_counterexample_snapshots(root, plan)
    return plan


@pytest.mark.parametrize(
    "change", ["different-mutation", "undo-repair", "control", "command"]
)
def test_cross_candidate_history_preserves_original_batch(execution_case, change):
    root, _, prior = _history_with_two_controls(execution_case)
    changed = {
        "different-mutation": ("variant", "VALUE='different-error'\nREPAIRED=True\n"),
        "undo-repair": ("variant", "VALUE='lost'\n"),
        "control": ("second_control", "VALUE='saved'\nREPAIRED=False\n"),
        "command": (None, None),
    }[change]
    plan = _rebase_batch(
        execution_case, prior, changed_subject=changed[0], changed_content=changed[1]
    )
    if change == "command":
        data = plan.model_dump(mode="json")
        next(s for s in data["steps"] if s["subject_id"] == "second_control")[
            "binding"
        ]["argv"].append("easier-mode")
        plan = CounterexamplePlan.model_validate(data)
    with pytest.raises(ValueError, match="batch-content|execution-table"):
        execution._plan_history(root, plan)
    assert not execution._attempts_dir(root, prior).exists()


def test_cross_candidate_original_mutation_inherits_mother_repair(execution_case):
    root, _, prior = _history_with_two_controls(execution_case)
    before = (root / prior.subjects[1].snapshot.path).read_bytes()
    plan = _rebase_batch(execution_case, prior)
    _, history = execution._plan_history(root, plan)
    assert history == [prior]
    assert (root / prior.subjects[1].snapshot.path).read_bytes() == before
    assert not execution._attempts_dir(root, plan).exists()


def test_snapshots_capture_original_bytes_and_exact_mutation_relation(execution_case):
    root, _, plan = execution_case
    references = execution.validate_counterexample_snapshots(root, plan)
    assert len(references) > len(plan.subjects)
    assert all((root / ref.path).is_file() for ref in references)
    (Path(plan.steps[0].binding.project_root) / "untracked.txt").write_text("drift")
    with pytest.raises(ValueError, match="source-stale"):
        execution.validate_counterexample_snapshots(root, plan)


def _delivered_identity(case):
    root, _, plan = case
    parent = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    tree = _git(root, "write-tree").stdout.decode().strip()
    _git(root, "commit", "--allow-empty", "-qm", "delivered candidate")
    return VerifiedDeliveryCommit(
        root=root,
        review_id="delivery-unit",
        loop_id=plan.loop_id,
        reviewed_head=parent,
        current_commit=_git(root, "rev-parse", "HEAD").stdout.decode().strip(),
        staged_tree=tree,
        review_input_digest="a" * 64,
        source_boundary_digest="b" * 64,
        artifact_digests=(),
    )


def _closed_delivery_state(case, monkeypatch, *, delivered=True):
    """这里只隔离状态消费者；完整 LocalPR 证明由独立接口及生命周期集成测试验证。"""
    from ai_sdlc.core import counterexample_evaluation
    from ai_sdlc.core.counterexample_models import ObservationBundle
    from ai_sdlc.core.implementation_models import (
        ImplementationClose,
        ImplementationReport,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts
    from ai_sdlc.core.loop_models import LoopRun

    actual_evaluate = counterexample_evaluation.evaluate_counterexample
    root, contract, plan = case
    proof = _delivered_identity(case) if delivered else None
    impl = SimpleNamespace(
        loop_id=plan.loop_id,
        work_item_id=plan.work_item_id,
        verification_contract_digest=plan.contract_digest,
        task_scopes={plan.task_id: list(plan.allowed_modified_paths)},
    )
    artifacts = implementation_artifacts(root, plan.loop_id)
    execution._write_json(
        root,
        artifacts.loop_run_path,
        LoopRun(
            loop_id=plan.loop_id,
            loop_type="implementation",
            status="closed",
            work_item_id=plan.work_item_id,
        ).model_dump(mode="json"),
    )
    execution._write_json(
        root,
        artifacts.close_path,
        ImplementationClose(
            loop_id=plan.loop_id,
            required_task_count=1,
            report_path=artifacts.report_json_path.relative_to(root).as_posix(),
        ).model_dump(mode="json"),
    )
    execution._write_json(
        root,
        artifacts.report_json_path,
        ImplementationReport(
            loop_id=plan.loop_id,
            work_item_id=plan.work_item_id,
            work_item_path="specs/sample",
        ).model_dump(mode="json"),
    )
    assessment = SimpleNamespace(
        current_result=SimpleNamespace(status="PASS", evidence_refs=()),
        positive_controls=(
            SimpleNamespace(
                subject_id="positive_control",
                obligation_id="save",
                business=SimpleNamespace(status="PASS"),
                v0="accepted",
                v1="accepted",
            ),
        ),
        required_complete=True,
        reasons=(),
        v1_disposition="adopt_v1",
    )
    bound = SimpleNamespace(
        contract=contract,
        plan=plan,
        assessment=assessment,
        observations=SimpleNamespace(attempts=(), repairs=()),
        artifact_refs=(),
    )
    reference = plan.subjects[0].snapshot
    record = CounterexampleEvidenceRecord(
        loop_id=plan.loop_id,
        task_id=plan.task_id,
        contract_ref=reference,
        plan_ref=reference,
        observations_ref=reference,
        assessment_ref=reference,
        source_digest_before=plan.candidate_digest,
        source_digest_after=plan.candidate_digest,
        recorded_at_ms=1,
    )
    record_ref = execution._write_json(
        root,
        artifacts.loop_dir / "counterexamples/results/unit-record.json",
        record.model_dump(mode="json"),
    )
    progress = SimpleNamespace(
        tasks=[
            SimpleNamespace(
                task_id=plan.task_id,
                counterexample_results=[record_ref],
            )
        ]
    )
    monkeypatch.setattr(
        "ai_sdlc.core.implementation_store.validate_implementation_verification_contract",
        lambda *a: (contract, {}),
    )
    monkeypatch.setattr(
        execution, "resolve_counterexample_evidence", lambda *a, **k: bound
    )

    def evaluate(contract, plan, observations, **kwargs):
        # 真实回执仍由纯核产生正式模型；简化结果仅用于本组状态消费者替身。
        if isinstance(observations, ObservationBundle):
            return actual_evaluate(contract, plan, observations, **kwargs)
        return assessment

    monkeypatch.setattr(counterexample_evaluation, "evaluate_counterexample", evaluate)
    # 本组只隔离交付身份与历史读取；真实跨任务版本选择由纯核及原生集成验证。
    monkeypatch.setattr(
        "ai_sdlc.core.counterexample_evaluation.aggregate_task_assessments",
        lambda contract, pairs, **kwargs: (
            pairs[0][1].v1_disposition,
            pairs[0][1].required_complete,
            pairs[0][1].reasons,
        ),
    )
    monkeypatch.setattr(
        "ai_sdlc.core.pr_review_service.read_verified_delivery_commit",
        lambda *a: proof,
    )
    return impl, progress, proof, assessment, artifacts


def _same_head_repaired_state(case, monkeypatch):
    """真实同 HEAD 改源码及 index，旧捕获和真实执行原件保持原字节。"""
    root, contract, old_plan = case
    old_receipt = _execute(case)
    # 此组聚焦业务失败历史消费；原中断资源先实际收尾，未清理债由专门用例覆盖。
    assert (
        _execute(case, "current-cleanup", cleanup_recovery=True).cleanup_status
        == "complete"
    )
    impl, progress, _, assessment, artifacts = _closed_delivery_state(
        case, monkeypatch, delivered=False
    )
    folder = artifacts.loop_dir / "counterexamples"
    execution._write_json(
        root,
        folder / f"plans/{counterexample_digest(old_plan)}.json",
        old_plan.model_dump(mode="json"),
    )
    original_head = _git(root, "rev-parse", "HEAD").stdout
    repaired = "VALUE='saved'\nREPAIRED=True\n"
    rebased_variant = "VALUE='lost'\nREPAIRED=True\n"
    (root / "src/save.py").write_text(repaired)
    _git(root, "add", "src/save.py")
    data = old_plan.model_dump(mode="json")
    data["candidate_digest"] = source_digest_sha256(build_source_digest(root))
    assert data["candidate_digest"] != old_plan.candidate_digest
    for subject in data["subjects"]:
        target = Path(
            next(
                step.binding.project_root
                for step in old_plan.steps
                if step.subject_id == subject["id"]
            )
        )
        (target / "src/save.py").write_text(repaired)
        _git(target, "add", "src/save.py")
        if subject["role"] == "variant":
            (target / "src/save.py").write_text(rebased_variant)
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=f"{folder.relative_to(root).as_posix()}/repaired/{subject['id']}",
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        reference = execution._write_json(
            root, folder / "repaired" / f"{subject['id']}.json", manifest
        )
        subject.update(
            candidate_digest=manifest["source_digest"],
            parent_candidate_digest=data["candidate_digest"],
            snapshot=reference.model_dump(),
        )
    patch = execution._write_json(
        root,
        folder / "repaired/patch.json",
        {
            "schema_version": 1,
            "changes": [
                {
                    "path": "src/save.py",
                    "before_sha256": hashlib.sha256(repaired.encode()).hexdigest(),
                    "after_sha256": hashlib.sha256(
                        rebased_variant.encode()
                    ).hexdigest(),
                }
            ],
        },
    )
    data["subjects"][1]["patch"] = patch.model_dump()
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_counterexample_snapshots(root, plan, require_live=False)
    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    old_ref = progress.tasks[0].counterexample_results[0]
    record = CounterexampleEvidenceRecord.model_validate_json(
        (root / old_ref.path).read_bytes()
    ).model_copy(
        update={
            "source_digest_before": plan.candidate_digest,
            "source_digest_after": plan.candidate_digest,
            "recorded_at_ms": 2,
        }
    )
    new_ref = execution._write_json(
        root, folder / "results/repaired-record.json", record
    )
    progress.tasks[0].counterexample_results.append(new_ref)
    # 此组替身只隔离身份读取；真实业务修复链由原生生命周期测试验证。
    assessment.current_result.evidence_refs = old_receipt.raw_evidence_refs
    old_assessment = SimpleNamespace(
        current_result=SimpleNamespace(
            status="FAIL", evidence_refs=old_receipt.raw_evidence_refs
        ),
        positive_controls=assessment.positive_controls,
        required_complete=False,
        reasons=("original business failure",),
        v1_disposition="retain_v0",
    )
    resolved = []

    def resolve(_root, _impl, reference, **kwargs):
        resolved.append(reference)
        old = reference == old_ref
        return SimpleNamespace(
            contract=contract,
            plan=old_plan if old else plan,
            assessment=old_assessment if old else assessment,
            observations=SimpleNamespace(
                attempts=(old_receipt,) if old else (),
                repairs=()
                if old
                else (
                    SimpleNamespace(
                        before_observation_refs=old_receipt.raw_evidence_refs
                    ),
                ),
            ),
            artifact_refs=(reference,),
        )

    monkeypatch.setattr(execution, "resolve_counterexample_evidence", resolve)
    monkeypatch.setattr(
        "ai_sdlc.core.pr_review_service.read_verified_delivery_commit",
        lambda *a: pytest.fail("same-HEAD repair history must not request PR proof"),
    )
    return impl, progress, plan, assessment, old_receipt, resolved


def test_same_head_repair_keeps_old_failure_and_attempt_as_history(
    execution_case, monkeypatch
):
    root, _, old_plan = execution_case
    impl, progress, _, _, receipt, resolved = _same_head_repaired_state(
        execution_case, monkeypatch
    )
    old_refs = execution.validate_counterexample_snapshots(
        root, old_plan, require_live=False
    )
    before = {ref.path: (root / ref.path).read_bytes() for ref in old_refs}
    blockers, advisories, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert not blockers
    assert any("business=PASS" in item for item in advisories)
    assert resolved == progress.tasks[0].counterexample_results
    assert {ref.path for ref in (*old_refs, receipt.attempt_ref)} <= {
        ref.path for ref in refs
    }
    assert before == {path: (root / path).read_bytes() for path in before}


@pytest.mark.parametrize("replayed", [False, True])
def test_old_control_rejection_requires_current_complete_replay(
    execution_case, monkeypatch, replayed
):
    root, _, _ = execution_case
    impl, progress, _, assessment, _, _ = _same_head_repaired_state(
        execution_case, monkeypatch
    )
    original = execution.resolve_counterexample_evidence
    old_ref = progress.tasks[0].counterexample_results[0]

    def resolve(*args, **kwargs):
        bound = original(*args, **kwargs)
        if args[2] == old_ref:
            bound.assessment.positive_controls = (
                SimpleNamespace(
                    subject_id="positive_control",
                    obligation_id="save",
                    business=SimpleNamespace(status="PASS"),
                    v0="accepted",
                    v1="false_rejection",
                ),
            )
        return bound

    monkeypatch.setattr(execution, "resolve_counterexample_evidence", resolve)
    assessment.positive_controls[0].v1 = "accepted" if replayed else "unknown"
    assessment.required_complete = replayed
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("legitimate control" in item for item in blockers) is not replayed
    if replayed:
        assert not blockers


@pytest.mark.parametrize(
    "base, original, repaired, expected",
    [
        (
            b"COMMIT=False\nVALUE='input'\n",
            b"COMMIT=True\nVALUE='input'\n",
            b"COMMIT=True\nVALUE='input'\n",
            b"COMMIT=True\nVALUE='input'\n",
        ),
        (
            b"COMMIT=False\nVALUE='input'\n",
            b"COMMIT=False\nVALUE='mutant'\n",
            b"COMMIT=True\nVALUE='input'\n",
            b"COMMIT=True\nVALUE='mutant'\n",
        ),
        (b"A\nB\nC\n", b"A\nC\n", b"A\nB\nD\n", b"A\nD\n"),
    ],
)
def test_original_edit_replay_preserves_independent_fix(
    base, original, repaired, expected
):
    assert execution._merge_original_change(base, original, repaired) == expected


def test_conflicting_original_edit_is_unproven():
    with pytest.raises(ValueError, match="rebase-unproven"):
        execution._merge_original_change(
            b"VALUE='old'\n", b"VALUE='error'\n", b"VALUE='fixed'\n"
        )


@pytest.mark.parametrize("damage", ["missing-content", "bad-raw", "unknown-cleanup"])
def test_same_head_started_only_history_still_requires_complete_originals(
    execution_case, monkeypatch, damage
):
    root, _, old_plan = execution_case
    impl, progress, _, _, receipt, _ = _same_head_repaired_state(
        execution_case, monkeypatch
    )
    progress.tasks[0].counterexample_results.pop(0)
    if damage == "missing-content":
        manifest = json.loads((root / old_plan.subjects[0].snapshot.path).read_bytes())
        (root / manifest["files"][0]["content_ref"]["path"]).unlink()
    else:
        folder = (root / receipt.attempt_ref.path).parent
        if damage == "bad-raw":
            (folder / "stdout").write_bytes(b"changed original bytes")
        else:
            (folder / "cleanup.json").unlink()
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert blockers
    assert any(
        "history-incomplete" in item or "execution-unknown" in item for item in blockers
    )


def test_same_head_repair_does_not_hide_later_current_failure(
    execution_case, monkeypatch
):
    root, _, _ = execution_case
    impl, progress, _, assessment, _, _ = _same_head_repaired_state(
        execution_case, monkeypatch
    )
    assessment.current_result.status = "FAIL"
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("business failure" in item for item in blockers)


def test_same_head_original_manifest_head_must_match_bound_identity(
    execution_case, monkeypatch
):
    root, _, old_plan = execution_case
    impl, progress, _, _, _, _ = _same_head_repaired_state(execution_case, monkeypatch)
    path = root / old_plan.subjects[0].snapshot.path
    manifest = json.loads(path.read_bytes())
    manifest["head"] = "f" * 40
    path.write_text(json.dumps(manifest))
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert "counterexample-artifact-content-stale" in blockers
    # 即使重新绑定篡改原件的 SHA，声明 HEAD 仍必须等于原 source_identity。
    altered = old_plan.model_copy(
        update={
            "subjects": (
                old_plan.subjects[0].model_copy(
                    update={"snapshot": execution._ref(root, path)}
                ),
                *old_plan.subjects[1:],
            )
        }
    )
    original_resolver = execution.resolve_counterexample_evidence

    def resolve_altered(*args, **kwargs):
        bound = original_resolver(*args, **kwargs)
        if bound.plan == old_plan:
            bound.plan = altered
        return bound

    monkeypatch.setattr(execution, "resolve_counterexample_evidence", resolve_altered)
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert "counterexample-snapshot-git-identity-stale" in blockers


@pytest.mark.parametrize("window", ["digest", "history"])
def test_same_head_read_window_rejects_actual_commit(
    execution_case, monkeypatch, window
):
    root, _, _ = execution_case
    impl, progress, _, _, _, _ = _same_head_repaired_state(execution_case, monkeypatch)
    if window == "digest":
        original = execution.build_source_digest

        def after_digest(*args, **kwargs):
            result = original(*args, **kwargs)
            _git(root, "commit", "-qm", "actual concurrent commit")
            return result

        monkeypatch.setattr(execution, "build_source_digest", after_digest)
    else:
        original = execution.validate_counterexample_snapshots
        calls = 0

        def after_snapshot(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                _git(root, "commit", "-qm", "actual concurrent commit")
            return result

        monkeypatch.setattr(
            execution, "validate_counterexample_snapshots", after_snapshot
        )
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("source-head-changed-during-readback" in item for item in blockers)


@pytest.mark.parametrize("failed_read", [1, 2, 3])
def test_same_head_git_read_error_is_global_blocker(
    execution_case, monkeypatch, failed_read
):
    root, _, _ = execution_case
    impl, progress, _, _, _, _ = _same_head_repaired_state(execution_case, monkeypatch)
    original = execution._git
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        if args[1:] == ("rev-parse", "HEAD"):
            calls += 1
            if calls == failed_read:
                raise subprocess.CalledProcessError(1, ["git", "rev-parse", "HEAD"])
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "_git", unavailable)
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("source-identity-unavailable" in item for item in blockers)


def test_current_input_matching_keeps_every_attempt_and_rechecks_next_call(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    impl, progress, _, _, artifacts = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    folder = artifacts.loop_dir / "counterexamples"
    execution._write_json(
        root, folder / f"plans/{counterexample_digest(plan)}.json", plan
    )
    receipts = []
    for subject in plan.subjects:
        receipts.append(_execute(execution_case, f"{subject.id}-none"))
        receipts.append(
            _execute(execution_case, f"{subject.id}-cleanup", cleanup_recovery=True)
        )
    bound = execution.resolve_counterexample_evidence(
        root, impl, progress.tasks[0].counterexample_results[0]
    )
    bound.observations.attempts = tuple(receipts)
    original_match = execution._counterexample_live_inputs_match
    original_recover = execution.recover_counterexample_attempt
    original_parse = CounterexamplePlan.model_validate_json
    matches = []
    recovered = []
    parsed = []

    def parse(cls, *args, **kwargs):
        parsed.append(args[0])
        return original_parse(*args, **kwargs)

    def match(*args, **kwargs):
        matches.append(kwargs["allow_absent_v1"])
        return original_match(*args, **kwargs)

    def recover(*args, **kwargs):
        recovered.append(args[2].path)
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(execution, "_counterexample_live_inputs_match", match)
    monkeypatch.setattr(execution, "recover_counterexample_attempt", recover)
    monkeypatch.setattr(CounterexamplePlan, "model_validate_json", classmethod(parse))
    blockers, _, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False
    )
    assert not blockers
    assert set(recovered) == {receipt.attempt_ref.path for receipt in receipts}
    assert len(recovered) == len(receipts)
    assert len(parsed) == 1
    # 六条真实意图分别核验；文件图仅按计划分类和选中验收各读首尾一次。
    assert matches.count(True) == matches.count(False) == 2
    assert {receipt.attempt_ref.path for receipt in receipts} <= {
        ref.path for ref in refs
    }

    (root / receipts[0].attempt_ref.path).with_name("stdout").write_bytes(
        b"changed original output"
    )
    matches.clear()
    recovered.clear()
    parsed.clear()
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False
    )
    assert any("history-incomplete" in item for item in blockers)
    assert matches.count(True) == matches.count(False) == 2
    assert set(recovered) == {receipt.attempt_ref.path for receipt in receipts}
    assert len(parsed) == 1


@pytest.mark.parametrize("damage", ["bytes", "delete", "add", "rename"])
@pytest.mark.parametrize("captured", [False, True])
def test_unique_history_plan_rejects_raw_or_path_set_change(
    execution_case, monkeypatch, damage, captured
):
    root, _, plan = execution_case
    impl, progress, _, _, artifacts = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    path = (
        artifacts.loop_dir / f"counterexamples/plans/{counterexample_digest(plan)}.json"
    )
    execution._write_json(root, path, plan)
    _execute(execution_case)
    _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    material = (
        {
            item.relative_to(root).as_posix(): item.read_bytes()
            for item in (root / ".ai-sdlc").rglob("*")
            if item.is_file()
        }
        if captured
        else None
    )
    key = path.relative_to(root).as_posix()
    new_path = path.with_name("another-plan.json")
    new_key = new_path.relative_to(root).as_posix()
    original = execution.recover_counterexample_attempt
    recovered = []

    def change_after_first_attempt(*args, **kwargs):
        receipt = original(*args, **kwargs)
        recovered.append(receipt.attempt_id)
        if len(recovered) != 1:
            return receipt
        raw = material[key] if material is not None else path.read_bytes()
        if material is not None:
            if damage == "bytes":
                material[key] = raw + b"\n"
            elif damage == "delete":
                del material[key]
            elif damage == "add":
                material[new_key] = raw
            else:
                material[new_key] = material.pop(key)
        elif damage == "bytes":
            path.write_bytes(raw + b"\n")
        elif damage == "delete":
            path.unlink()
        elif damage == "add":
            new_path.write_bytes(raw)
        else:
            path.rename(new_path)
        return receipt

    monkeypatch.setattr(
        execution, "recover_counterexample_attempt", change_after_first_attempt
    )
    blockers, _, _ = execution.counterexample_verification_state(
        root,
        impl,
        progress,
        captured_artifacts=material,
        require_r2=False,
        require_completion=False,
    )
    assert len(recovered) == 2
    assert any("counterexample-original-batch-incomplete" in item for item in blockers)
    if damage == "add":
        assert any("plan-set-changed-during-readback" in item for item in blockers)
    elif damage == "bytes":
        expected = "captured-evidence-stale" if captured else "artifact-content-stale"
        assert any(expected in item for item in blockers), blockers


@pytest.mark.parametrize("initial_match", [False, True])
@pytest.mark.parametrize("delivered", [False, True])
def test_input_classification_rejects_both_directions_of_read_window_change(
    execution_case, monkeypatch, initial_match, delivered
):
    root, _, _ = execution_case
    impl, progress, _, _, _ = _closed_delivery_state(
        execution_case, monkeypatch, delivered=delivered
    )
    source = root / "src/save.py"
    original_bytes = source.read_bytes()
    name = (
        "_counterexample_plan_matches_delivery"
        if delivered
        else "_counterexample_live_inputs_match"
    )
    original_match = getattr(execution, name)
    first = True

    def change_after_classification(*args, **kwargs):
        nonlocal first
        if not first:
            return original_match(*args, **kwargs)
        first = False
        if not initial_match:
            source.write_bytes(b"different live input\n")
        result = original_match(*args, **kwargs)
        assert result is initial_match
        source.write_bytes(
            b"different live input\n" if initial_match else original_bytes
        )
        return result

    monkeypatch.setattr(execution, name, change_after_classification)
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    expected = (
        "delivery-source-changed-during-readback"
        if delivered
        else "current-input-changed-during-readback"
    )
    assert any(expected in item for item in blockers), blockers


def test_current_input_classification_still_rechecks_original_snapshot(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    impl, progress, _, _, _ = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    original_match = execution._counterexample_live_inputs_match
    first = True

    def change_original_after_classification(*args, **kwargs):
        nonlocal first
        result = original_match(*args, **kwargs)
        if first:
            first = False
            path = root / plan.subjects[0].snapshot.path
            path.write_bytes(path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        execution,
        "_counterexample_live_inputs_match",
        change_original_after_classification,
    )
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("artifact-content-stale" in item for item in blockers), blockers


def test_delivery_matches_original_files_after_snapshot_roots_are_removed(
    execution_case,
):
    root, _, plan = execution_case
    refs = execution.validate_counterexample_snapshots(root, plan)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    proof = _delivered_identity(execution_case)
    assert source_digest_sha256(build_source_digest(root)) != plan.candidate_digest
    for directory in {step.binding.project_root for step in plan.steps}:
        shutil.rmtree(directory)
    assert execution._counterexample_plan_matches_delivery(
        root, plan, proof, captured_artifacts=captured
    )


@pytest.mark.parametrize(
    "tree_content",
    [
        "tracked",
        "absent",
        "different",
        "symlink",
        "gitlink",
        "blob-replacement",
        "tree-replacement",
    ],
)
@pytest.mark.parametrize("captured", [False, True])
def test_v14_selected_acceptance_requires_real_reviewed_blob(
    execution_case, tree_content, captured
):
    root, _, plan = execution_case
    relative = "tests/v1.py"
    original_tree = _git(root, "write-tree").stdout.decode().strip()
    original_blob = _git(root, "rev-parse", ":" + relative).stdout.decode().strip()
    if tree_content == "absent":
        _git(root, "update-index", "--force-remove", relative)
    elif tree_content in {"different", "blob-replacement", "tree-replacement"}:
        (root / relative).write_text("different selected acceptance\n")
        _git(root, "add", relative)
        if tree_content == "blob-replacement":
            stored_blob = (
                _git(root, "rev-parse", ":" + relative).stdout.decode().strip()
            )
            _git(root, "replace", stored_blob, original_blob)
            assert (
                _git(root, "--no-replace-objects", "cat-file", "blob", stored_blob).stdout
                == b"different selected acceptance\n"
            )
    elif tree_content in {"symlink", "gitlink"}:
        object_id = (
            _git(
                root,
                "rev-parse",
                "HEAD" if tree_content == "gitlink" else "HEAD:" + relative,
            )
            .stdout.decode()
            .strip()
        )
        _git(
            root,
            "update-index",
            "--cacheinfo",
            "160000" if tree_content == "gitlink" else "120000",
            object_id,
            relative,
        )
    tree = _git(root, "write-tree").stdout.decode().strip()
    if tree_content == "tree-replacement":
        _git(root, "replace", tree, original_tree)
    current = next(subject for subject in plan.subjects if subject.role == "current")
    manifest = json.loads((root / current.snapshot.path).read_bytes())
    manifest["index_tree"] = tree
    reference = execution._write_json(
        root,
        (root / current.snapshot.path).with_name("selected-tree-test.json"),
        manifest,
    )
    selected_plan = plan.model_copy(
        update={
            "subjects": tuple(
                subject.model_copy(update={"snapshot": reference})
                if subject == current
                else subject
                for subject in plan.subjects
            )
        }
    )
    artifacts = (
        {reference.path: (root / reference.path).read_bytes()} if captured else None
    )
    assert execution._selected_acceptance_matches_tree(
        root, selected_plan, "v1", captured_artifacts=artifacts
    ) is (tree_content == "tracked")


@pytest.mark.parametrize(
    "damage", ["bytes", "mode", "extra", "delete", "parent", "tree"]
)
def test_delivery_rejects_source_or_base_difference(execution_case, damage):
    root, _, plan = execution_case
    proof = _delivered_identity(execution_case)
    if damage == "mode" and os.name == "nt":
        # Windows 的只读属性能改变实际文件图，测试结束后恢复以便清理。
        source = root / "src/save.py"
        original_mode = stat.S_IMODE(source.stat().st_mode)
        try:
            source.chmod(original_mode & ~stat.S_IWUSR)
            assert stat.S_IMODE(source.stat().st_mode) != original_mode
            assert not execution._counterexample_plan_matches_delivery(
                root, plan, proof
            )
        finally:
            source.chmod(original_mode)
        return
    if damage == "bytes":
        (root / "src/save.py").write_text("different source")
    elif damage == "mode":
        (root / "src/save.py").chmod(0o755)
    elif damage == "extra":
        (root / "new-input.txt").write_text("unreviewed")
    elif damage == "delete":
        (root / "src/save.py").unlink()
    elif damage == "parent":
        proof = replace(proof, reviewed_head="f" * 40)
    else:
        proof = replace(proof, staged_tree="f" * 40)
    assert not execution._counterexample_plan_matches_delivery(root, plan, proof)


def test_delivery_rechecks_declared_ignored_input(execution_case):
    root, _, plan = execution_case
    subject = plan.subjects[0]
    manifest = json.loads((root / subject.snapshot.path).read_bytes())
    target = Path(manifest["root"])
    changed = execution.capture_counterexample_snapshot(
        target,
        evidence_root=root,
        artifact_dir=f".ai-sdlc/loops/implementation/{plan.loop_id}/ignored-snapshot",
        acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        ignored_inputs=["scratch/result.json"],
    )
    reference = execution._write_json(
        root, root / ".ai-sdlc/state/ignored-snapshot.json", changed
    )
    plan = plan.model_copy(
        update={
            "subjects": (
                subject.model_copy(update={"snapshot": reference}),
                *plan.subjects[1:],
            )
        }
    )
    (root / "scratch").mkdir()
    (root / "scratch/result.json").write_bytes(
        (target / "scratch/result.json").read_bytes()
    )
    proof = _delivered_identity((root, None, plan))
    assert execution._counterexample_plan_matches_delivery(root, plan, proof)
    (root / "scratch/result.json").write_text("changed ignored input")
    assert not execution._counterexample_plan_matches_delivery(root, plan, proof)


@pytest.mark.parametrize(
    "damage",
    [None, "missing-content", "proof-overlap", "moving-proof", "moving-source", "open"],
)
def test_delivery_state_keeps_original_capture_and_live_guard_separate(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    impl, progress, proof, _, artifacts = _closed_delivery_state(
        execution_case, monkeypatch
    )
    refs = execution.validate_counterexample_snapshots(root, plan, require_live=False)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    for witness in plan.witnesses:
        captured[witness.input_ref.path] = (root / witness.input_ref.path).read_bytes()
    for step in plan.steps:
        for resource in step.binding.resources:
            for ref in execution.resource_initial_artifact_refs(
                root, resource.initial_state
            ):
                captured[ref.path] = (root / ref.path).read_bytes()
    record_ref = progress.tasks[0].counterexample_results[0]
    captured[record_ref.path] = (root / record_ref.path).read_bytes()
    # R1 的闭前状态不能被当前 closed 生命周期 guard 覆盖。
    captured[artifacts.loop_run_path.relative_to(root).as_posix()] = (
        b"original preclose snapshot"
    )
    if damage == "missing-content":
        captured.pop(next(key for key in captured if "/content/" in key))
    elif damage == "proof-overlap":
        path = ".ai-sdlc/reviews/pr/unit/review-run.json"
        captured[path] = b"different original"
        proof = replace(proof, artifact_digests=((path, "a" * 64),))
        monkeypatch.setattr(
            "ai_sdlc.core.pr_review_service.read_verified_delivery_commit",
            lambda *a: proof,
        )
    elif damage == "moving-proof":
        sequence = iter((proof, replace(proof, source_boundary_digest="c" * 64)))
        monkeypatch.setattr(
            "ai_sdlc.core.pr_review_service.read_verified_delivery_commit",
            lambda *a: next(sequence),
        )
    elif damage == "moving-source":
        original_match = execution._counterexample_plan_matches_delivery
        calls = 0

        def move_after_match(*args, **kwargs):
            nonlocal calls
            matches = original_match(*args, **kwargs)
            calls += 1
            if calls == 1:
                (root / "src/save.py").write_text("changed during consumption")
            return matches

        monkeypatch.setattr(
            execution, "_counterexample_plan_matches_delivery", move_after_match
        )
    elif damage == "open":
        run = json.loads(artifacts.loop_run_path.read_bytes())
        run["status"] = "passed"
        artifacts.loop_run_path.write_text(json.dumps(run))
        monkeypatch.setattr(
            "ai_sdlc.core.pr_review_service.read_verified_delivery_commit",
            lambda *a: pytest.fail("open execution cannot use delivery proof"),
        )
    blockers, advisories, returned = execution.counterexample_verification_state(
        root,
        impl,
        progress,
        captured_artifacts=captured,
        require_r2=False,
    )
    assert bool(blockers) == (damage is not None)
    if damage is None:
        assert any("business=PASS" in item for item in advisories)
    assert not any(ref.path.startswith(".ai-sdlc/reviews/") for ref in returned)


def test_delivery_does_not_erase_existing_current_business_failure(
    execution_case, monkeypatch
):
    root, _, _ = execution_case
    impl, progress, _, assessment, _ = _closed_delivery_state(
        execution_case, monkeypatch
    )
    assessment.current_result.status = "FAIL"
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("business failure" in item for item in blockers)


@pytest.mark.parametrize("unknown", [False, True])
def test_delivery_reconciles_started_unrecorded_plan(
    execution_case, monkeypatch, unknown
):
    root, _, plan = execution_case
    impl, progress, _, _, artifacts = _closed_delivery_state(
        execution_case, monkeypatch
    )
    plan_digest = counterexample_digest(plan)
    step = plan.steps[0]
    subject = next(item for item in plan.subjects if item.id == step.subject_id)
    folder = artifacts.loop_dir / "counterexamples"
    intent_ref = execution._write_json(
        root,
        folder / "attempts/started/intent.json",
        {
            "schema_version": 1,
            "plan_digest": plan_digest,
            "contract_digest": plan.contract_digest,
            "candidate_digest": subject.candidate_digest,
            "attempt_id": "started",
            "attempt_ordinal": 1,
            "step_id": step.id,
            "kind": step.kind,
            "phase": step.phase,
            "subject_id": step.subject_id,
            "binding_digest": counterexample_digest(step.binding),
            "binding": step.binding.model_dump(mode="json"),
            "resolved_command": execution._resolve_command(step),
            "source_snapshot": subject.snapshot.model_dump(mode="json"),
            "ownership_nonce": "unit-current-call",
            "started_at_ms": 1,
            "deadline_ms": 500_001,
            "max_execution_attempts": plan.max_execution_attempts,
            "reserved_seconds": plan.required_reserve_seconds,
        },
    )
    execution._write_json(
        root, folder / f"plans/{plan_digest}.json", plan.model_dump(mode="json")
    )
    # 本例只替换完成状态；完整意图必须先到达真正的历史消费者。
    history = execution._attempt_history(root, folder / "attempts")
    assert [(path, intent["step_id"]) for path, intent in history] == [
        (root / intent_ref.path, step.id)
    ]
    receipt = AttemptReceipt(
        attempt_id="started",
        attempt_ref=intent_ref,
        plan_digest=plan_digest,
        contract_digest=plan.contract_digest,
        candidate_digest=subject.candidate_digest,
        step_id=step.id,
        subject_id=step.subject_id,
        binding_digest=counterexample_digest(step.binding),
        ownership_nonce="unit-current-call",
        status="execution_unknown" if unknown else "completed",
        started_at_ms=1,
        ended_at_ms=None if unknown else 2,
        exit_code=None if unknown else 0,
        timed_out=False,
        output_truncated=False,
        cleanup_status="unknown" if unknown else "complete",
        raw_evidence_refs=(),
    )
    monkeypatch.setattr(
        execution, "attempt_artifact_refs", lambda *a, **k: (intent_ref,)
    )
    monkeypatch.setattr(
        execution, "recover_counterexample_attempt", lambda *a, **k: receipt
    )
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert "counterexample-current-plan-attempts-not-reconciled" in blockers
    assert "counterexample-current-plan-execution-table-incomplete" in blockers
    if unknown:
        assert "counterexample-prior-execution-unknown-no-replay" in blockers


def test_real_observer_receipt_and_idempotent_read(execution_case):
    root, contract, plan = execution_case
    receipt = _execute(execution_case)
    assert receipt.status == "completed"
    assert receipt.cleanup_status == "complete"
    raw = execution.read_owned_raw_evidence(root, receipt)
    observation = execution.collect_observation(contract, plan, receipt, raw)
    assert observation.typed_actual.value == "saved"
    assert receipt == _execute(execution_case)
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1


def _case_with_failed_operation(case, *, kind="exercise", timeout=False):
    root, contract, original = case
    data = original.model_dump(mode="json")
    first = copy.deepcopy(data["steps"][0])
    first.update(id="current-failed-operation", kind=kind)
    binding = first["binding"]
    binding["argv"] = [
        sys.executable,
        "-c",
        "import time;time.sleep(5)" if timeout else "raise SystemExit(7)",
        binding["resources"][0]["observation_endpoint"],
        "request",
    ]
    binding["witness_input"]["argv_index"] = 4
    binding["timeout_seconds"] = 1
    data["steps"][0]["depends_on"] = [first["id"]]
    data["steps"].insert(0, first)
    for step in data["steps"]:
        if step["kind"] == "cleanup":
            step["binding"]["argv"] = [
                sys.executable,
                "-c",
                "print('owned cleanup')",
                step["binding"]["resources"][0]["root"],
            ]
    return root, contract, CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize(
    "kind,timeout", [("exercise", False), ("reset", False), ("exercise", True)]
)
def test_failed_operation_runs_frozen_cleanup_without_replaying_business(
    execution_case, kind, timeout
):
    case = _case_with_failed_operation(execution_case, kind=kind, timeout=timeout)
    root, _, plan = case
    failed = _execute(case, "current-failed-operation")
    assert failed.cleanup_status == "complete"
    assert failed.timed_out if timeout else failed.exit_code == 7
    with pytest.raises(ValueError, match="business-stopped"):
        _execute(case, "current-none")
    with pytest.raises(ValueError, match="business-stopped"):
        _execute(case, "current-V0")
    cleaned = _execute(case, "current-cleanup")
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert _execute(case, "current-failed-operation") == failed
    assert _execute(case, "current-cleanup") == cleaned
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 2
    intent = json.loads((root / cleaned.attempt_ref.path).read_text())
    assert intent["failure_cleanup_steps"] == ["current-failed-operation"]


def test_cleanup_retains_resources_when_process_completion_is_unproven(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    for step_id in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, step_id).status == "completed"
    real_run = execution.run_quality_command

    def incomplete_proof(options):
        original_callback = options.controlled.on_cleanup

        def persist_incomplete(payload):
            original_callback({**payload, "status": "incomplete"})

        return real_run(
            replace(
                options,
                controlled=replace(options.controlled, on_cleanup=persist_incomplete),
            )
        )

    monkeypatch.setattr(execution, "run_quality_command", incomplete_proof)
    receipt = _execute(execution_case, "current-cleanup")
    folder = (root / receipt.attempt_ref.path).parent
    assert receipt.cleanup_status == "incomplete"
    assert Path(plan.steps[0].binding.resources[0].root).is_dir()
    assert not (folder / "resource-cleanup.json").exists()
    assert "resources-retained" in (folder / "postcheck-error.json").read_text()
    with pytest.raises(ValueError, match="unknown-no-replay"):
        _execute(execution_case, "current-cleanup")


def test_each_action_checks_current_boundary_once_before_and_after(execution_case):
    calls = []

    def postcheck():
        calls.append("current-boundary")

    receipts = []
    for step_id in ("current-none", "current-V0", "current-V1"):
        before = len(calls)
        receipt = _execute(execution_case, step_id, postcheck=postcheck)
        assert receipt.status == "completed"
        assert len(calls) - before == 2
        receipts.append(receipt)
    before = len(calls)
    assert _execute(execution_case, "current-V1", postcheck=postcheck) == receipts[-1]
    assert len(calls) - before == 1
    root, _, plan = execution_case
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 3


def test_atomic_intent_failure_never_launches(execution_case, monkeypatch):
    real_write = LoopArtifactStore.write_bytes_artifact
    real_resources = execution._validate_resources
    real_run = execution.run_quality_command
    claims, launches = [], []

    def fail_intent(self, path, content, **kwargs):
        if path.name == "intent.json":
            raise PermissionError("simulated intent atomic write denied")
        return real_write(self, path, content, **kwargs)

    def resources(*args, **kwargs):
        if kwargs.get("claim"):
            claims.append(True)
        return real_resources(*args, **kwargs)

    def run(options):
        launches.append(options.argv)
        return real_run(options)

    monkeypatch.setattr(execution, "_validate_resources", resources)
    monkeypatch.setattr(execution, "run_quality_command", run)
    with monkeypatch.context() as patch:
        patch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_intent)
        with pytest.raises(PermissionError):
            _execute(execution_case)
    root, _, plan = execution_case
    assert not claims and not launches
    assert not list(execution._attempts_dir(root, plan).iterdir())
    assert execution._recover_plan_attempts(root, plan) == []
    receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    assert claims == [True] and len(launches) == 1
    assert json.loads((root / receipt.attempt_ref.path).read_bytes())["attempt_ordinal"] == 1
    assert _execute(execution_case) == receipt
    assert execution._recover_plan_attempts(root, plan) == [receipt]
    assert len(launches) == 1


@pytest.mark.parametrize("name", ["stdout", "stderr"])
@pytest.mark.parametrize("after_create", [False, True])
def test_fix23_prelaunch_output_failure_can_retry(execution_case, monkeypatch, name, after_create):
    original_open = Path.open
    original_fsync = os.fsync
    failed = []

    def open_output(path, *args, **kwargs):
        if path.name == name and args == ("xb",) and path.parent.name.startswith("attempt-"):
            failed.append(path)
            if not after_create:
                raise PermissionError("output creation denied")
        return original_open(path, *args, **kwargs)

    def fsync(fd):
        if failed and after_create and os.fstat(fd).st_ino == failed[0].stat().st_ino:
            raise PermissionError("new empty output fsync denied")
        return original_fsync(fd)

    def forbid(*args, **kwargs):
        pytest.fail("prelaunch publication failure must not claim or launch")

    real_resources = execution._validate_resources

    def resources(*args, **kwargs):
        if kwargs.get("claim"):
            forbid()
        return real_resources(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_output)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(execution, "run_quality_command", forbid)
        patch.setattr(execution, "_validate_resources", resources)
        with pytest.raises(PermissionError, match="output"):
            _execute(execution_case)
    root, _, plan = execution_case
    assert len(failed) == 1
    assert not list(execution._attempts_dir(root, plan).iterdir())
    assert execution._recover_plan_attempts(root, plan) == []
    receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    assert json.loads((root / receipt.attempt_ref.path).read_bytes())["attempt_ordinal"] == 1
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt


def test_fix23_published_intent_fsync_failure_remains_unknown(execution_case, monkeypatch):
    original_write = LoopArtifactStore.write_bytes_artifact
    original_fsync = os.fsync
    published = []

    def write(self, path, content, **kwargs):
        if path.name == "intent.json":
            published.append(path)
        return original_write(self, path, content, **kwargs)

    def fsync(fd):
        if published and published[0].exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise PermissionError("intent directory fsync denied after hard link")
        return original_fsync(fd)

    def forbid(*args, **kwargs):
        pytest.fail("published intent failure must not claim or launch")

    real_resources = execution._validate_resources

    def resources(*args, **kwargs):
        if kwargs.get("claim"):
            forbid()
        return real_resources(*args, **kwargs)

    monkeypatch.setattr(execution, "run_quality_command", forbid)
    monkeypatch.setattr(execution, "_validate_resources", resources)
    with monkeypatch.context() as patch:
        patch.setattr(LoopArtifactStore, "write_bytes_artifact", write)
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(PermissionError, match="after hard link"):
            _execute(execution_case)
    root, _, plan = execution_case
    assert len(published) == 1 and published[0].is_file()
    before = {path.name: path.read_bytes() for path in published[0].parent.iterdir()}
    assert set(before) == {"stdout", "stderr", "intent.json"}
    receipt = execution.recover_counterexample_attempt(root, plan, execution._ref(root, published[0]))
    assert receipt.status == "execution_unknown" and receipt.cleanup_status != "complete"
    with pytest.raises(ValueError, match="unknown-no-replay"):
        _execute(execution_case)
    assert {path.name: path.read_bytes() for path in published[0].parent.iterdir()} == before


@pytest.mark.parametrize("damage", ["extra", "content", "file-replaced", "directory-replaced", "linked"])
def test_fix23_live_unpublished_intent_preserves_unowned_material(execution_case, monkeypatch, damage):
    original_write = LoopArtifactStore.write_bytes_artifact
    retained = {}

    def fail_intent(self, path, content, **kwargs):
        if path.name != "intent.json":
            return original_write(self, path, content, **kwargs)
        directory = path.parent
        if damage == "extra":
            (directory / "foreign").write_bytes(b"other actor")
        elif damage == "content":
            (directory / "stdout").write_bytes(b"other actor")
        elif damage == "file-replaced":
            replacement = directory / "new"
            replacement.write_bytes(b"")
            replacement.replace(directory / "stdout")
        elif damage == "directory-replaced":
            directory.rename(directory.with_name("retained-original"))
            directory.mkdir()
            (directory / "stdout").write_bytes(b"")
            (directory / "stderr").write_bytes(b"")
        else:
            os.link(directory / "stdout", directory.parent.parent / "foreign-link")
        retained.update({item: item.read_bytes() for item in directory.iterdir()})
        raise PermissionError("unpublished intent denied")

    with monkeypatch.context() as patch:
        patch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_intent)
        with pytest.raises(PermissionError, match="unpublished"):
            _execute(execution_case)
    assert retained and all(path.read_bytes() == raw for path, raw in retained.items())
    root, _, plan = execution_case
    with pytest.raises(ValueError, match="orphan-attempt-intent-missing"):
        _execute(execution_case)
    with pytest.raises(ValueError, match="orphan-attempt-intent-missing"):
        execution._recover_plan_attempts(root, plan)


def test_raw_receipt_survives_postcheck_failure(execution_case):
    calls = 0

    def postcheck():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("real context changed after command")

    receipt = _execute(execution_case, postcheck=postcheck)
    root, _, _ = execution_case
    folder = (root / receipt.attempt_ref.path).parent
    assert receipt.status == "infrastructure_error"
    assert json.loads((folder / "raw-result.json").read_text())["exit_code"] == 0
    assert (folder / "stdout").stat().st_size > 0
    assert (folder / "postcheck-error.json").is_file()


@pytest.mark.parametrize(
    "missing", ["process.json", "raw-result.json", "cleanup.json", "postcheck.json"]
)
def test_cold_unknown_never_replays_or_kills_historical_pid(
    execution_case, monkeypatch, missing
):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    folder = (root / receipt.attempt_ref.path).parent
    (folder / missing).unlink()

    def forbid(*args, **kwargs):
        raise AssertionError("cold recovery must not launch or signal")

    monkeypatch.setattr(execution, "run_quality_command", forbid)
    recovered = execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref
    )
    assert (
        recovered.status == "execution_unknown"
        or recovered.cleanup_status != "complete"
    )
    with pytest.raises(ValueError, match="unknown-no-replay"):
        _execute(execution_case)


def test_resource_endpoint_and_secret_environment_are_rejected(execution_case):
    root, contract, plan = execution_case
    data = plan.model_dump(mode="json")
    data["steps"][0]["binding"]["effective_environment"]["DATABASE_URL"] = (
        "sqlite:///production.sqlite"
    )
    data["steps"][0]["binding"]["environment_digest"] = counterexample_digest(
        data["steps"][0]["binding"]["effective_environment"]
    )
    altered = CounterexamplePlan.model_validate(data)
    with pytest.raises(ValueError, match="allowlist"):
        _execute((root, contract, altered))
    assert not execution._attempts_dir(root, plan).exists()


@pytest.mark.parametrize("key", ["HOME", "TMPDIR", "USERPROFILE", "CE_DATA"])
def test_resource_environment_parent_traversal_is_rejected_before_start(
    execution_case, key
):
    root, contract, plan = execution_case
    data = plan.model_dump(mode="json")
    binding = data["steps"][0]["binding"]
    resource = Path(binding["resources"][0]["root"])
    binding["effective_environment"][key] = str(resource / ".." / ".." / "outside")
    binding["environment_digest"] = counterexample_digest(
        binding["effective_environment"]
    )
    with pytest.raises(ValueError, match="allowlist"):
        _execute((root, contract, CounterexamplePlan.model_validate(data)))
    assert not execution._attempts_dir(root, plan).exists()
    assert not (resource.parent.parent / "outside").exists()


def test_canonical_owned_home_is_accepted_by_real_command(execution_case):
    root, contract, plan = execution_case
    data = plan.model_dump(mode="json")
    binding = data["steps"][0]["binding"]
    binding["effective_environment"]["HOME"] = binding["resources"][0]["root"]
    binding["environment_digest"] = counterexample_digest(
        binding["effective_environment"]
    )
    receipt = _execute((root, contract, CounterexamplePlan.model_validate(data)))
    assert receipt.status == "completed" and receipt.exit_code == 0
    assert receipt.cleanup_status == "complete"


def test_budget_shortage_preserves_no_started_attempt(execution_case):
    root, contract, plan = execution_case
    with pytest.raises(ValueError, match="budget-reserve"):
        execution.execute_counterexample_attempt(
            root,
            contract,
            plan,
            "current-none",
            deadline_ms=time.time_ns() // 1_000_000 + 1000,
        )
    assert not execution._attempts_dir(root, plan).exists()


def test_strict_bytes_preserve_binary_and_refuse_overwrite(tmp_path):
    store = LoopArtifactStore(tmp_path)
    target = tmp_path / "intent.bin"
    store.write_bytes_artifact(target, b"\xff\x00\xfe", immutable=True)
    store.write_bytes_artifact(target, b"\xff\x00\xfe", immutable=True)
    with pytest.raises(ValueError, match="immutable"):
        store.write_bytes_artifact(target, b"replaced", immutable=True)
    assert target.read_bytes() == b"\xff\x00\xfe"


def test_strict_atomic_write_has_no_permission_fallback(tmp_path, monkeypatch):
    target = tmp_path / "intent.json"
    real_open = Path.open

    def fail_temporary(self, mode="r", *args, **kwargs):
        if mode == "xb":
            raise PermissionError("temporary create rejected")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_temporary)
    with pytest.raises(PermissionError):
        LoopArtifactStore(tmp_path).write_bytes_artifact(
            target, b"intent", immutable=True
        )
    assert not target.exists()


def test_strict_write_rejects_symlink_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="parent"):
        LoopArtifactStore(tmp_path).write_bytes_artifact(
            link / "intent.json", b"intent", immutable=True
        )


def test_captured_snapshots_remain_reviewable_after_execution_roots_removed(
    execution_case,
):
    root, _, plan = execution_case
    refs = execution.validate_counterexample_snapshots(root, plan)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    for directory in {step.binding.project_root for step in plan.steps}:
        shutil.rmtree(directory)
    assert (
        execution.validate_counterexample_snapshots(
            root, plan, captured_artifacts=captured, require_live=False
        )
        == refs
    )
    key = next(ref.path for ref in refs if "/content/" in ref.path)
    captured[key] += b"drift"
    with pytest.raises(ValueError, match="captured-artifact"):
        execution.validate_counterexample_snapshots(
            root, plan, captured_artifacts=captured, require_live=False
        )


def test_captured_attempt_does_not_fallback_to_live_artifacts(execution_case):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    refs = execution.attempt_artifact_refs(root, receipt.attempt_ref)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )
        == receipt
    )
    process_path = next(ref.path for ref in refs if ref.path.endswith("/process.json"))
    captured.pop(process_path)
    assert (root / process_path).is_file()
    unknown = execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    )
    assert unknown.status == "execution_unknown"


def _change_observer(case, source):
    root, contract, plan = case
    data = plan.model_dump(mode="json")
    (root / "observe.py").write_text(source)
    data["candidate_digest"] = source_digest_sha256(build_source_digest(root))
    for subject in data["subjects"]:
        project = Path(
            next(
                step.binding.project_root
                for step in plan.steps
                if step.subject_id == subject["id"]
            )
        )
        (project / "observe.py").write_text(source)
        folder = f".ai-sdlc/loops/implementation/{plan.loop_id}/counterexamples/recaptured/{subject['id']}"
        manifest = execution.capture_counterexample_snapshot(
            project,
            evidence_root=root,
            artifact_dir=folder,
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        subject["snapshot"] = execution._write_json(
            root, root / folder / "snapshot.json", manifest
        ).model_dump()
        subject["candidate_digest"] = manifest["source_digest"]
        subject["parent_candidate_digest"] = data["candidate_digest"]
    return root, contract, CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invalid_utf8_retains_raw_bytes_without_fabricating_observation(
    execution_case, stream
):
    code = "import os,json\n"
    if stream == "stdout":
        code += "os.write(1,b'\\xff\\xfe')\n"
    else:
        code += "os.write(2,b'\\xff\\xfe')\nprint(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
    case = _change_observer(execution_case, code)
    root, contract, plan = case
    receipt = _execute(case)
    raw = execution.read_owned_raw_evidence(root, receipt)
    assert all(not item.ref.path.endswith("/" + stream) for item in raw)
    binary_ref = next(
        ref for ref in receipt.raw_evidence_refs if ref.path.endswith("/" + stream)
    )
    assert (root / binary_ref.path).read_bytes() == b"\xff\xfe"
    assert binary_ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    observation = execution.collect_observation(contract, plan, receipt, raw)
    if stream == "stdout":
        assert observation is None
    else:
        assert observation.observation_status == "collected"
        assert observation.typed_actual.value == "saved"


def _case_with_frozen_resets(case):
    import copy

    root, contract, plan = case
    data = plan.model_dump(mode="json")
    prefix = f".ai-sdlc/loops/implementation/{plan.loop_id}/counterexamples"
    initial_bytes = b'{"value":"saved"}'
    target = root / prefix / "initial-original.bin"
    LoopArtifactStore(root).write_bytes_artifact(target, initial_bytes, immutable=True)
    initial = {
        "schema_version": 1,
        "files": [
            {
                "path": "result.json",
                "sha256": hashlib.sha256(initial_bytes).hexdigest(),
                "content_ref": execution._ref(root, target).model_dump(),
            }
        ],
    }
    initial_ref = execution._write_json(
        root, root / prefix / "initial-with-bytes.json", initial
    )
    for step in data["steps"]:
        step["binding"]["resources"][0]["initial_state"] = initial_ref.model_dump()
    normal_steps = copy.deepcopy(data["steps"])
    for subject in data["subjects"]:
        original = next(
            step
            for step in normal_steps
            if step["subject_id"] == subject["id"] and step["kind"] == "observe"
        )
        reset = copy.deepcopy(original)
        reset.update(
            id=f"{subject['id']}-reset",
            kind="reset",
            phase="r2",
            depends_on=[f"{subject['id']}-cleanup"],
        )
        data["steps"].append(reset)
    for original in normal_steps:
        repeat = copy.deepcopy(original)
        repeat.update(
            id="r2-" + original["id"],
            phase="r2",
            depends_on=[
                f"{original['subject_id']}-reset",
                *["r2-" + identifier for identifier in original["depends_on"]],
            ],
        )
        if repeat.get("business_observation_step_id"):
            repeat["business_observation_step_id"] = (
                "r2-" + repeat["business_observation_step_id"]
            )
        data["steps"].append(repeat)
    data["max_execution_attempts"] = 40
    return root, contract, CounterexamplePlan.model_validate(data)


def test_failure_does_not_reclean_an_unstarted_r2_resource_cycle(execution_case):
    root, contract, plan = _case_with_failed_operation(
        _case_with_frozen_resets(execution_case)
    )
    data = plan.model_dump(mode="json")
    finished = [
        s
        for s in data["steps"]
        if s["subject_id"] == "positive_control" and s["phase"] == "final"
    ]
    data["steps"] = finished + [s for s in data["steps"] if s not in finished]
    next(s for s in data["steps"] if s["id"] == "positive_control-reset")[
        "depends_on"
    ].append("current-none")
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    receipts = [_execute(case, step["id"]) for step in finished]
    receipts.append(_execute(case, "current-failed-operation"))
    blocked = execution._blocked_business_steps(plan, receipts)
    assert "positive_control-reset" in blocked
    assert "r2-positive_control-cleanup" in blocked
    assert not execution._subject_resources_active(plan, receipts, "positive_control")
    assert execution._subject_resources_active(plan, receipts, "current")
    assert not any(identifier.startswith("variant-") for identifier in blocked)
    assert _execute(case, "current-cleanup").cleanup_status == "complete"


def test_frozen_reset_restores_owned_initial_bytes_after_proven_cleanup(execution_case):
    case = _case_with_frozen_resets(execution_case)
    root, _, plan = case
    original = _execute(case)
    _execute(case, "current-V0")
    _execute(case, "current-V1")
    cleanup = _execute(case, "current-cleanup")
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert not resource.exists()
    assert cleanup.status == "completed" and cleanup.cleanup_status == "complete"
    assert any(
        ref.path.endswith("/resource-cleanup.json")
        for ref in execution.attempt_artifact_refs(root, cleanup.attempt_ref)
    )
    restored = _execute(case, "current-reset")
    assert restored.status == "completed"
    first_owner = next(
        ref for ref in original.raw_evidence_refs if "/resource-owner-" in ref.path
    )
    reset_owner = next(
        ref for ref in restored.raw_evidence_refs if "/resource-owner-" in ref.path
    )
    assert first_owner != reset_owner
    assert json.loads((root / reset_owner.path).read_bytes())["directory_identity"] == (
        execution._directory_identity(resource)
    )
    assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'
    repeated = _execute(case, "r2-current-none")
    assert repeated.status == "completed" and repeated.attempt_id != original.attempt_id
    refs = execution.resource_initial_artifact_refs(
        root, plan.steps[0].binding.resources[0].initial_state
    )
    assert len(refs) == 2


def test_missing_cleanup_proof_does_not_recreate_resource_or_replay(execution_case):
    case = _case_with_frozen_resets(execution_case)
    root, _, plan = case
    _execute(case)
    _execute(case, "current-V0")
    _execute(case, "current-V1")
    cleanup = _execute(case, "current-cleanup")
    (root / cleanup.attempt_ref.path).with_name("resource-cleanup.json").unlink()
    with pytest.raises(ValueError, match="unknown-no-replay"):
        _execute(case, "current-reset")
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 4


def _fix25_repaired_resource_cycle(case):
    root, contract, original = _case_with_frozen_resets(case)
    data = original.model_dump(mode="json")
    initial_resets = []
    for subject in original.subjects:
        reset = copy.deepcopy(next(s for s in data["steps"] if s["id"] == f"{subject.id}-reset"))
        reset.update(id=f"{subject.id}-initial-reset", phase="final", depends_on=[])
        initial_resets.append(reset)
        next(s for s in data["steps"] if s["id"] == f"{subject.id}-none")["depends_on"] = [reset["id"]]
    data.update(steps=[*initial_resets, *data["steps"]], max_execution_attempts=80)
    original = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(contract, original)
    folder = execution._attempts_dir(root, original).parent
    old_plan = execution._write_json(
        root, folder / "plans" / f"{counterexample_digest(original)}.json", original
    )
    case = root, contract, original
    receipts = [
        _execute(case, identifier)
        for identifier in (
            "current-initial-reset", "current-none", "current-V0", "current-V1", "current-cleanup"
        )
    ]
    assert all(r.status == "completed" and r.cleanup_status == "complete" for r in receipts)
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for receipt in receipts
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    originals[old_plan.path] = (root / old_plan.path).read_bytes()
    successor = _rebase_batch(case, original, preserve_roots=True)
    execution._require_original_batch(root, original, successor)
    execution._write_json(
        root, folder / "plans" / f"{counterexample_digest(successor)}.json", successor
    )
    assert successor.candidate_digest != original.candidate_digest
    assert successor.subjects[0].snapshot != original.subjects[0].snapshot
    assert successor.steps[0].binding.resources == original.steps[0].binding.resources
    return (root, contract, successor), original, receipts[-1], originals


@pytest.mark.parametrize("damage", [None, "plan-missing", "plan-identity", "cleanup-missing"])
def test_fix25_repaired_candidate_reuses_only_original_cleaned_resource(execution_case, damage):
    case, original, cleanup, originals = _fix25_repaired_resource_cycle(execution_case)
    root, _, successor = case
    folder = execution._attempts_dir(root, original).parent
    old_plan_path = folder / "plans" / f"{counterexample_digest(original)}.json"
    resource = Path(successor.steps[0].binding.resources[0].root)
    assert not resource.exists()
    before = set(folder.glob("attempts/*/intent.json"))
    if damage == "plan-missing":
        old_plan_path.unlink()
    elif damage == "plan-identity":
        old_plan_path.write_bytes(execution._json_bytes(successor))
    elif damage == "cleanup-missing":
        (root / cleanup.attempt_ref.path).with_name("resource-cleanup.json").unlink()
    if damage:
        with pytest.raises((OSError, ValueError)):
            _execute(case, "current-initial-reset")
        assert not resource.exists()
        assert set(folder.glob("attempts/*/intent.json")) == before
        return
    restored = _execute(case, "current-initial-reset")
    assert restored.status == "completed" and restored.cleanup_status == "complete"
    assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'
    assert set(folder.glob("attempts/*/intent.json")) - before == {root / restored.attempt_ref.path}
    assert all((root / key).read_bytes() == raw for key, raw in originals.items())
    # 新认领不改变旧执行身份；现场和捕获消费者均须用原计划读取旧清理。
    for captured in (None, {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in folder.parent.rglob("*") if path.is_file()
    }):
        recovered = execution.recover_counterexample_attempt(
            root, original, cleanup.attempt_ref, captured_artifacts=captured
        )
        assert recovered.status == "completed" and recovered.cleanup_status == "complete"
        history, debts = execution._historical_resource_cycles(
            root, successor, captured_artifacts=captured
        )
        assert len(history) == len(before) + 1
        assert set(debts) == {(counterexample_digest(successor), "current")}
    for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
        assert _execute(case, identifier).status == "completed"
    assert not resource.exists()
    assert not execution._historical_resource_cycles(root, successor)[1]
    assert all((root / key).read_bytes() == raw for key, raw in originals.items())


def test_observer_cannot_recreate_a_missing_resource(execution_case):
    root, _, plan = execution_case
    shutil.rmtree(plan.steps[0].binding.resources[0].root)
    with pytest.raises(ValueError, match="missing-resource-needs-frozen-reset"):
        _execute(execution_case)
    assert not execution._attempts_dir(root, plan).exists()


@pytest.mark.parametrize(
    "damage", ["missing-intent", "missing-ordinal", "sequence-gap"]
)
def test_started_attempt_history_damage_never_replays_or_reports_clean(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    path = root / receipt.attempt_ref.path
    if damage == "missing-intent":
        path.unlink()
        expected = "orphan-attempt-intent-missing"
    else:
        payload = json.loads(path.read_bytes())
        if damage == "missing-ordinal":
            payload.pop("attempt_ordinal")
            expected = "history-identity-invalid"
        else:
            payload["attempt_ordinal"] = 2
            expected = "history-sequence-incomplete"
        path.write_text(json.dumps(payload))
    folder = execution._attempts_dir(root, plan)
    before = {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError, match=expected):
        _execute(execution_case)
    with pytest.raises(ValueError, match=expected):
        execution._recover_plan_attempts(root, plan)
    impl, progress, _, _, _ = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any(expected in item for item in blockers), blockers
    captured = {
        str((folder / name).relative_to(root)): raw for name, raw in before.items()
    }
    with pytest.raises(ValueError, match=expected):
        execution._attempt_history(root, folder, captured)
    assert before == {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in folder.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("frontend", [False, True])
def test_closed_delivery_guard_preserves_original_frontend_transition(
    execution_case, monkeypatch, frontend
):
    root, _, _ = execution_case
    impl, _, proof, _, artifacts = _closed_delivery_state(execution_case, monkeypatch)
    report = json.loads(artifacts.report_json_path.read_bytes())
    report["requires_frontend_evidence"] = frontend
    artifacts.report_json_path.write_text(json.dumps(report))
    close = json.loads(artifacts.close_path.read_bytes())
    close["next_loop_type"] = "frontend-evidence" if frontend else "local-pr-review"
    artifacts.close_path.write_text(json.dumps(close))
    assert execution._closed_counterexample_delivery_guard(root, impl, None)[0] == proof
    close["next_loop_type"] = "local-pr-review" if frontend else "frontend-evidence"
    artifacts.close_path.write_text(json.dumps(close))
    with pytest.raises(ValueError, match="closed-identity-mismatch"):
        execution._closed_counterexample_delivery_guard(root, impl, None)


@pytest.mark.parametrize("damage", ["missing-v1", "changed-ignored-input"])
def test_same_git_digest_does_not_complete_uninstalled_or_changed_ignored_inputs(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    impl, progress, _, _, _ = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    bound = execution.resolve_counterexample_evidence(
        root, impl, progress.tasks[0].counterexample_results[0]
    )
    before = build_source_digest(root)
    relative = "scratch/v1.py" if damage == "missing-v1" else "scratch/input.json"
    original = b"original verified content\n"
    evidence = (
        root / f".ai-sdlc/loops/implementation/{plan.loop_id}/counterexamples/ignored"
    )
    content = execution._write_json(
        root, evidence / "source.json", {"fixture": "owned"}
    )
    (root / content.path).write_bytes(original)
    content = execution._ref(root, root / content.path)
    subjects = []
    for subject in plan.subjects:
        manifest = json.loads((root / subject.snapshot.path).read_bytes())
        manifest["ignored_inputs"] = [relative]
        manifest["files"].append(
            {
                "path": relative,
                "sha256": content.sha256,
                "mode": 0o644,
                "content_ref": content.model_dump(),
            }
        )
        manifest["files"].sort(key=lambda item: item["path"])
        snapshot = execution._write_json(
            root, evidence / f"{subject.id}-snapshot.json", manifest
        )
        subjects.append(subject.model_copy(update={"snapshot": snapshot}))
    bound.plan = plan.model_copy(update={"subjects": tuple(subjects)})
    target = root / relative
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(original)
    assert not execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )[0]
    if damage == "missing-v1":
        target.unlink()
    else:
        target.write_bytes(b"different effective input\n")
    assert build_source_digest(root) == before
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("missing or stale" in item for item in blockers), blockers


def test_inline_self_report_cannot_substitute_independent_observer(execution_case):
    root, contract, plan = execution_case
    data = plan.model_dump(mode="json")
    data["steps"][0]["binding"]["argv"] = [
        sys.executable,
        "-c",
        "print('success')",
        data["steps"][0]["binding"]["resources"][0]["observation_endpoint"],
        "request",
    ]
    data["steps"][0]["binding"]["witness_input"]["argv_index"] = 4
    with pytest.raises(ValueError, match="observer-script-not-bound"):
        _execute((root, contract, CounterexamplePlan.model_validate(data)))
    assert not execution._attempts_dir(root, plan).exists()


@pytest.mark.parametrize("delivered", [False, True])
@pytest.mark.parametrize(
    "scenario, complete",
    [
        ("no-gain", True),
        ("false-rejection", True),
        ("weak-v0", False),
        ("adopt-absent", False),
        # 本地 ignored 文件不等于已进入真实交付树；保留这个原缺陷负控。
        ("adopt-installed", False),
        ("adopt-tracked", True),
        ("shared-missing", False),
        ("v1-shared-option", False),
        ("v1-shared-environment", False),
        ("v0-drift", False),
        ("v1-drift", False),
    ],
)
def test_isolated_v1_choice_preserves_v0_and_checks_selected_inputs(
    execution_case, monkeypatch, delivered, scenario, complete
):
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from tests.unit.test_counterexample_evaluation import make_bundle

    root, contract, original = execution_case
    relative = "scratch/draft-v1.py"
    data = original.model_dump(mode="json")
    data["protected_paths"].append(relative)
    if scenario == "adopt-tracked":
        (root / "scratch").mkdir(exist_ok=True)
        (root / relative).write_bytes((root / "tests/v1.py").read_bytes())
        _git(root, "add", "-f", relative)
        data["candidate_digest"] = source_digest_sha256(build_source_digest(root))
    prefix = f".ai-sdlc/loops/implementation/{original.loop_id}/counterexamples/draft"
    for subject in data["subjects"]:
        target = Path(
            next(
                step.binding.project_root
                for step in original.steps
                if step.subject_id == subject["id"]
            )
        )
        (target / relative).write_bytes((root / "tests/v1.py").read_bytes())
        ignored = [relative]
        if scenario == "adopt-tracked":
            _git(target, "add", "-f", relative)
            ignored = []
        if scenario == "shared-missing":
            (target / "scratch/common.json").write_text("shared")
            ignored.append("scratch/common.json")
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=f"{prefix}/{subject['id']}",
            acceptance_sources={"V0": "tests/v0.py", "V1": relative},
            ignored_inputs=ignored,
        )
        if scenario == "adopt-tracked":
            subject["candidate_digest"] = manifest["source_digest"]
            subject["parent_candidate_digest"] = data["candidate_digest"]
        subject["snapshot"] = execution._write_json(
            root, root / prefix / f"{subject['id']}.json", manifest
        ).model_dump()
        for step in data["steps"]:
            if (
                step["subject_id"] == subject["id"]
                and step["acceptance_version"] == "V1"
            ):
                step["binding"]["argv"][1] = relative
            if (
                step["subject_id"] == subject["id"]
                and step["acceptance_version"] == "V0"
            ):
                if scenario == "v1-shared-option":
                    step["binding"]["argv"].append("--config=" + relative)
                elif scenario == "v1-shared-environment":
                    environment = step["binding"]["effective_environment"]
                    environment["CE_CONFIG"] = str(target / relative)
                    step["binding"]["environment_digest"] = counterexample_digest(
                        environment
                    )
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_counterexample_snapshots(root, plan, require_live=False)
    bundle = make_bundle(
        contract,
        plan,
        v0_variant="accepted"
        if scenario in {"weak-v0", "adopt-absent", "adopt-installed", "adopt-tracked"}
        else "rejected",
        v1_control="rejected"
        if scenario in {"false-rejection", "weak-v0"}
        else "accepted",
    )
    assessment = evaluate_counterexample(contract, plan, bundle)
    impl, progress, proof, _, _ = _closed_delivery_state(
        (root, contract, plan), monkeypatch, delivered=delivered
    )
    bound = execution.resolve_counterexample_evidence(
        root, impl, progress.tasks[0].counterexample_results[0]
    )
    bound.assessment, bound.observations = assessment, bundle
    monkeypatch.setattr(
        "ai_sdlc.core.counterexample_evaluation.evaluate_counterexample",
        evaluate_counterexample,
    )
    if scenario in {"adopt-installed", "v1-drift"}:
        (root / "scratch").mkdir(exist_ok=True)
        (root / relative).write_bytes(
            (root / "tests/v1.py").read_bytes()
            if scenario == "adopt-installed"
            else b"changed draft"
        )
    if scenario == "v0-drift":
        (root / "tests/v0.py").write_text("changed selected acceptance")
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.validate_counterexample_snapshots(
            root, plan, require_live=False
        )
    }
    blockers, advisories, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert (not blockers) is complete, (scenario, blockers)
    if complete:
        expected = "adopt_v1" if scenario == "adopt-tracked" else "retain_v0"
        assert any(f"acceptance={expected}" in item for item in advisories)
        assert set(originals) <= {ref.path for ref in refs}
        if proof is not None:
            assert execution._counterexample_plan_matches_delivery(
                root, plan, proof, allow_absent_v1=expected == "retain_v0"
            )
    assert originals == {path: (root / path).read_bytes() for path in originals}


def test_frozen_method_cannot_observe_different_resource_file(execution_case):
    root, contract, plan = execution_case
    data = contract.model_dump(mode="json")
    data["obligations"][0]["oracle_spec"]["observation_method"]["location"] = (
        "other.json"
    )
    changed_contract = VerificationContract.model_validate(data)
    plan_data = plan.model_dump(mode="json")
    plan_data["contract_model_digest"] = counterexample_digest(changed_contract)
    with pytest.raises(ValueError, match="frozen-observation-endpoint-mismatch"):
        _execute((root, changed_contract, CounterexamplePlan.model_validate(plan_data)))
    assert not execution._attempts_dir(root, plan).exists()


def test_resource_state_keeps_empty_directories_and_sqlite_sidecars(execution_case):
    _, _, plan = execution_case
    resources = plan.steps[0].binding.resources
    directory = Path(resources[0].root)
    (directory / "empty").mkdir()
    (directory / "database.sqlite-wal").write_bytes(b"uncommitted-state")
    snapshot = execution._observed_resource_state(
        resources, deadline_ms=time.time_ns() // 1_000_000 + 10000
    )
    files = {item["path"]: item for item in snapshot[0]["files"]}
    assert files["empty"] == {
        "path": "empty",
        "kind": "directory",
        "sha256": None,
        "mode": stat.S_IMODE((directory / "empty").stat().st_mode),
    }
    assert (
        files["database.sqlite-wal"]["sha256"]
        == hashlib.sha256(b"uncommitted-state").hexdigest()
    )
    (directory / "database.sqlite-wal").write_bytes(b"changed-state")
    assert snapshot != execution._observed_resource_state(
        resources, deadline_ms=time.time_ns() // 1_000_000 + 10000
    )


def test_resource_snapshot_deadline_does_not_start_a_command(execution_case):
    root, _, plan = execution_case
    with pytest.raises(ValueError, match="resource-state-deadline"):
        execution._observed_resource_state(
            plan.steps[0].binding.resources, deadline_ms=0
        )
    assert not execution._attempts_dir(root, plan).exists()


def test_actual_resource_state_is_owned_and_captured_with_receipt(execution_case):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    reference = next(
        ref
        for ref in receipt.raw_evidence_refs
        if ref.path.endswith("/resource-state.json")
    )
    proof = json.loads((root / reference.path).read_bytes())
    assert proof["attempt_id"] == receipt.attempt_id
    assert proof["ownership_nonce"] == receipt.ownership_nonce
    assert proof["plan_digest"] == counterexample_digest(plan)
    assert proof["binding_digest"] == receipt.binding_digest
    assert proof["before"] == proof["after"]
    assert reference in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    assert any(
        raw.ref == reference for raw in execution.read_owned_raw_evidence(root, receipt)
    )


def test_acceptance_cannot_use_an_observation_from_previous_resource_state(
    execution_case,
):
    root, _, plan = execution_case
    _execute(execution_case)
    resource = Path(plan.steps[0].binding.resources[0].root)
    (resource / "result.json").write_text('{"value":"changed-after-observation"}')
    before = tuple(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(ValueError, match="acceptance-business-state-mismatch"):
        _execute(execution_case, "current-V0")
    assert tuple(execution._attempts_dir(root, plan).glob("*/intent.json")) == before


def test_mutating_observer_keeps_raw_state_difference_and_runs_frozen_cleanup(
    execution_case,
):
    source = (
        "import json,pathlib,sys\n"
        'path=pathlib.Path(sys.argv[1]); path.write_text(\'{"value":"changed"}\')\n'
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'changed'}}))\n"
    )
    case = _change_observer(execution_case, source)
    root, _, plan = case
    receipt = _execute(case)
    assert (
        receipt.status == "infrastructure_error"
        and receipt.cleanup_status == "complete"
    )
    reference = next(
        ref
        for ref in receipt.raw_evidence_refs
        if ref.path.endswith("/resource-state.json")
    )
    proof = json.loads((root / reference.path).read_bytes())
    assert proof["before"] != proof["after"]
    cleaned = _execute(case, "current-cleanup")
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()


@pytest.mark.parametrize(
    "scenario", ["marker-only", "tracked-under-ignored", "not-ignored"]
)
def test_resource_admission_rejects_tracked_or_non_scratch_roots(
    execution_case, scenario
):
    root, contract, plan = execution_case
    step = plan.steps[0]
    project = Path(step.binding.project_root)
    resource = Path(step.binding.resources[0].root)
    if scenario == "marker-only":
        (project / ".gitignore").write_text("scratch/.ai-sdlc-owner.json\n")
        _git(project, "add", "scratch/result.json")
    elif scenario == "tracked-under-ignored":
        _git(project, "add", "-f", "scratch/result.json")
    else:
        (project / ".gitignore").write_text("")
    original = (resource / "result.json").read_bytes()
    with pytest.raises(ValueError, match="ignored-scratch|contains-tracked"):
        execution._validate_resources(
            root,
            contract,
            plan,
            step,
            claim=False,
            allow_initial_claim=True,
            deadline_ms=time.time_ns() // 1_000_000 + 10_000,
        )
    assert (resource / "result.json").read_bytes() == original
    assert not (resource / ".ai-sdlc-owner.json").exists()
    assert not execution._attempts_dir(root, plan).exists()


def test_resource_deletion_rechecks_tracked_content_after_command(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    for step in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, step).status == "completed"
    real_run = execution.run_quality_command
    project = Path(plan.steps[0].binding.project_root)
    resource = Path(plan.steps[0].binding.resources[0].root)

    def change_index_after_command(options):
        result = real_run(options)
        _git(project, "add", "-f", "scratch/result.json")
        return result

    monkeypatch.setattr(execution, "run_quality_command", change_index_after_command)
    receipt = _execute(execution_case, "current-cleanup")
    assert receipt.status != "completed" and receipt.cleanup_status == "incomplete"
    assert (resource / "result.json").exists()
    assert (
        not (root / receipt.attempt_ref.path)
        .with_name("resource-cleanup.json")
        .exists()
    )


@pytest.mark.parametrize("change_subject", [False, True])
def test_historical_cleanup_requires_original_subject_but_not_unchanged_mother(
    execution_case, change_subject
):
    root, contract, plan = execution_case
    receipt = _execute(execution_case)
    (root / "src/save.py").write_text("VALUE='saved'\n# mother changed\n")
    if change_subject:
        project = Path(plan.steps[0].binding.project_root)
        (project / "src/save.py").write_text("VALUE='changed'\n")
    kwargs = dict(
        deadline_ms=time.time_ns() // 1_000_000 + 10_000_000, cleanup_recovery=True
    )
    if change_subject:
        with pytest.raises(ValueError, match="historical-cleanup-subject-source-stale"):
            execution.execute_counterexample_attempt(
                root, contract, plan, "current-cleanup", **kwargs
            )
        assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1
    else:
        cleaned = execution.execute_counterexample_attempt(
            root, contract, plan, "current-cleanup", **kwargs
        )
        assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
        assert not Path(plan.steps[0].binding.resources[0].root).exists()
        assert (
            json.loads((root / cleaned.attempt_ref.path).read_bytes())[
                "historical_cleanup_only"
            ]
            is True
        )
        history, debts = execution._historical_resource_cycles(root, plan)
        assert len(history) == 2 and not debts
        assert (
            execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
            == receipt
        )


@pytest.mark.parametrize(
    "new_candidate,complete,rejected",
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_historical_control_false_rejection_survives_later_missing_business(
    new_candidate, complete, rejected
):
    previous = SimpleNamespace(
        plan=SimpleNamespace(
            task_id="T01", candidate_digest="old", v1_digest="same-v1"
        ),
        assessment=SimpleNamespace(
            positive_controls=(
                SimpleNamespace(
                    subject_id="legal-control",
                    obligation_id="save",
                    business=SimpleNamespace(status="UNKNOWN"),
                    v1="false_rejection",
                ),
            )
        ),
    )
    latest = SimpleNamespace(
        plan=SimpleNamespace(
            task_id="T01", candidate_digest="new" if new_candidate else "old"
        )
    )
    assessment = SimpleNamespace(
        required_complete=complete,
        reasons=(),
        positive_controls=(
            SimpleNamespace(
                subject_id="legal-control",
                obligation_id="save",
                business=SimpleNamespace(status="PASS"),
                v1="accepted",
            ),
        ),
    )
    assert (
        execution._unresolved_control_rejection(
            [(1, "original", previous)], latest, assessment, "v1", "same-v1"
        )
        is rejected
    )


def test_referenced_read_preserves_actual_bytes_and_rejects_changed_content(tmp_path):
    path = tmp_path / "result.bin"
    original = b"saved\x00\xff\n"
    path.write_bytes(original)
    reference = execution._ref(tmp_path, path)
    assert execution._read_ref(tmp_path, reference) == original
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact-content-stale"):
        execution._read_ref(tmp_path, reference)


@pytest.mark.parametrize(
    "relative",
    [
        "../result.bin",
        "nested/../result.bin",
        "/result.bin",
        "nested\\result.bin",
        "drive:result.bin",
    ],
)
def test_referenced_read_rejects_noncanonical_reference_paths(tmp_path, relative):
    path = tmp_path / "result.bin"
    path.write_bytes(b"saved")
    reference = execution._ref(tmp_path, path).model_copy(update={"path": relative})
    with pytest.raises(ValueError, match="project-relative"):
        execution._read_ref(tmp_path, reference)


@pytest.mark.parametrize("kind", ["leaf-link", "parent-link", "directory", "missing"])
def test_referenced_read_requires_original_regular_file_path(tmp_path, kind):
    folder = tmp_path / "original"
    folder.mkdir()
    path = folder / "result.bin"
    path.write_bytes(b"saved")
    reference = execution._ref(tmp_path, path)
    if kind == "directory":
        relative = "original"
    elif kind == "missing":
        relative = "absent.bin"
    else:
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(
                path if kind == "leaf-link" else folder,
                target_is_directory=kind == "parent-link",
            )
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Local filesystem does not support this link fixture: {exc}")
        relative = "alias" if kind == "leaf-link" else "alias/result.bin"
    reference = reference.model_copy(update={"path": relative})
    with pytest.raises(ValueError, match="trusted file.*(regular|unavailable|symlink)"):
        execution._read_ref(tmp_path, reference)


@pytest.mark.parametrize(
    "relation,reverse",
    [("same", False), ("child", False), ("child", True), ("parent", False)],
)
def test_overlapping_resource_group_rejected_before_any_intent_or_claim(
    execution_case, monkeypatch, relation, reverse
):
    root, contract, plan = execution_case
    step = plan.steps[0]
    resource = step.binding.resources[0]
    original_root = Path(resource.root)
    other_root = {
        "same": original_root,
        "child": original_root / "nested",
        "parent": original_root.parent,
    }[relation]
    other = resource.model_copy(
        update={
            "id": "second-resource",
            "root": str(other_root),
            "observation_endpoint": str(other_root / "result.json"),
        }
    )
    resources = (other, resource) if reverse else (resource, other)
    # 观察、V0/V1 和 cleanup 仍共同绑定同一组资源，只改变物理根的相互关系。
    proposed = plan.model_copy(
        update={
            "steps": tuple(
                item.model_copy(
                    update={
                        "binding": item.binding.model_copy(
                            update={"resources": resources}
                        )
                    }
                )
                if item.subject_id == step.subject_id
                else item
                for item in plan.steps
            )
        }
    )
    execution.validate_plan_contract(contract, proposed)
    before = {
        str(path): path.read_bytes()
        for path in original_root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        execution,
        "run_quality_command",
        lambda *a, **k: pytest.fail("invalid resource plan must not start a process"),
    )
    with pytest.raises(ValueError, match="resource-roots-overlap"):
        _execute((root, contract, proposed), step.id)
    assert not list(root.rglob("intent.json"))
    assert not list(original_root.rglob(".ai-sdlc-owner.json"))
    assert before == {
        str(path): path.read_bytes()
        for path in original_root.rglob("*")
        if path.is_file()
    }


def test_later_step_resource_identity_overlap_is_checked_before_first_step(
    execution_case,
):
    root, contract, plan = execution_case
    first = plan.steps[0]
    later = next(step for step in plan.steps[1:] if step.subject_id != first.subject_id)
    first_root = first.binding.resources[0].root
    resources = tuple(
        resource.model_copy(
            update={
                "root": first_root,
                "observation_endpoint": str(Path(first_root) / "result.json"),
            }
        )
        for resource in later.binding.resources
    )
    proposed = plan.model_copy(
        update={
            "steps": tuple(
                step.model_copy(
                    update={
                        "binding": step.binding.model_copy(
                            update={"resources": resources}
                        )
                    }
                )
                if step.subject_id == later.subject_id
                else step
                for step in plan.steps
            )
        }
    )
    execution.validate_plan_contract(contract, proposed)
    with pytest.raises(ValueError, match="resource-roots-overlap"):
        _execute((root, contract, proposed), first.id)
    assert not list(root.rglob("intent.json"))
    assert not (Path(first_root) / ".ai-sdlc-owner.json").exists()
    execution._validate_plan_resource_roots(plan)


def test_task_batches_keep_shared_loop_budget_and_one_reinforcement(execution_case):
    root, _, original = execution_case
    next_task = original.model_copy(
        update={"task_id": "T02", "allowed_modified_paths": ("src/second.py",)}
    )
    execution._require_original_batch(root, original, next_task)
    for field, value in (
        ("max_execution_attempts", original.max_execution_attempts + 1),
        ("v1_digest", "b" * 64),
    ):
        with pytest.raises(
            ValueError, match="(scope-or-budget-changed|reinforcement-batch-exhausted)"
        ):
            execution._require_original_batch(
                root, original, next_task.model_copy(update={field: value})
            )


def test_same_named_control_from_another_task_cannot_clear_or_create_rejection():
    old = SimpleNamespace(
        plan=SimpleNamespace(task_id="T01", candidate_digest="old", v1_digest="v1"),
        assessment=SimpleNamespace(
            positive_controls=(
                SimpleNamespace(
                    subject_id="control", obligation_id="first", v1="false_rejection"
                ),
            )
        ),
    )
    latest = SimpleNamespace(
        plan=SimpleNamespace(task_id="T02", candidate_digest="new")
    )
    assessment = SimpleNamespace(
        required_complete=True, reasons=(), positive_controls=()
    )
    assert not execution._unresolved_control_rejection(
        [(1, "old", old)], latest, assessment, "v1", "v1"
    )
    latest.plan.task_id = "T01"
    assessment.positive_controls = (
        SimpleNamespace(
            subject_id="control",
            obligation_id="second",
            business=SimpleNamespace(status="PASS"),
            v1="accepted",
        ),
    )
    assert execution._unresolved_control_rejection(
        [(1, "old", old)], latest, assessment, "v1", "v1"
    )


def test_active_known_resource_cycle_can_continue_but_still_blocks_final_consumption(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    impl, progress, _, _, _ = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    digest = counterexample_digest(plan)
    execution._write_json(
        root,
        execution._attempts_dir(root, plan).parent / "plans" / f"{digest}.json",
        plan,
    )
    receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert resource.is_dir()
    blockers, advisories, _ = execution.counterexample_verification_state(
        root,
        impl,
        progress,
        require_r2=False,
        require_completion=False,
        active_plan_digest=digest,
    )
    assert "counterexample-historical-resource-cleanup-required" not in blockers
    assert "counterexample-active-resource-cycle-awaits-frozen-cleanup" in advisories
    for other in (None, "b" * 64):
        blockers, _, _ = execution.counterexample_verification_state(
            root,
            impl,
            progress,
            require_r2=False,
            require_completion=False,
            active_plan_digest=other,
        )
        assert "counterexample-historical-resource-cleanup-required" in blockers
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert "counterexample-historical-resource-cleanup-required" in blockers
    assert (
        _execute(
            execution_case, "current-cleanup", cleanup_recovery=True
        ).cleanup_status
        == "complete"
    )
    assert not resource.exists()


def _run_admission_case(case, monkeypatch):
    from ai_sdlc.core.implementation_models import (
        ImplementationProgress,
        ImplementationTaskProgress,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts

    root, contract, plan = case
    LoopArtifactStore(root).write_json_artifact(
        implementation_artifacts(root, plan.loop_id).progress_path,
        ImplementationProgress(
            loop_id=plan.loop_id,
            work_item_id=plan.work_item_id,
            tasks=[ImplementationTaskProgress(task_id=plan.task_id)],
        ),
    )
    prefix = execution._attempts_dir(root, plan).parent
    witness = plan.witnesses[0]
    reference = execution._write_json(
        root,
        prefix / "witness.json",
        {
            "schema_version": 1,
            "input_value": witness.input_value.model_dump(),
            "premise_values": {},
        },
    )
    budget_ref = execution._write_json(
        root, prefix / "budget.json", {"original_window_seconds": 10000}
    )
    contract = contract.model_copy(update={"budget_ref": budget_ref})
    contract_ref = execution._write_json(root, prefix / "contract.json", contract)
    plan = plan.model_copy(
        update={
            "budget_ref": budget_ref,
            "contract_model_digest": counterexample_digest(contract),
            "contract_digest": contract_ref.sha256,
            "witnesses": (witness.model_copy(update={"input_ref": reference}),),
        }
    )
    impl = SimpleNamespace(
        loop_id=plan.loop_id,
        work_item_id=plan.work_item_id,
        verification_contract_ref=contract_ref.path,
        verification_contract_digest=plan.contract_digest,
        task_scopes={plan.task_id: plan.allowed_modified_paths},
        declared_scope=(),
    )
    monkeypatch.setattr(
        "ai_sdlc.core.implementation_store.validate_implementation_verification_contract",
        lambda *a: (contract, {}),
    )
    monkeypatch.setattr(
        "ai_sdlc.core.loop_decision_service.validate_implementation_context",
        lambda *a, **k: SimpleNamespace(
            capability="stage-simulation-v1",
            started_at_ms=time.time_ns() // 1_000_000,
            plan=SimpleNamespace(time_plan=SimpleNamespace(window_seconds=10000)),
        ),
    )

    def phase(*args, phase_capture=None, **kwargs):
        if phase_capture is not None:
            phase_capture["observed_at_ms"] = time.time_ns() // 1_000_000
        return False, ()

    monkeypatch.setattr(execution, "_native_counterexample_r2", phase)
    return contract, impl, SimpleNamespace(loop_id=plan.loop_id), plan


@pytest.mark.parametrize(
    ("entry", "damage"),
    [
        ("native", "missing-reset"),
        ("step", "missing-reset"),
        ("step", "unlinked-reset"),
        ("step", "double-cleanup"),
        ("step", "missing-rebuild-bytes"),
    ],
)
def test_fix27_new_plan_resource_reuse_requires_reset_before_freeze(
    execution_case, monkeypatch, entry, damage
):
    root, _, _ = execution_case
    contract, impl, loop, legal = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    execution.validate_plan_contract(contract, legal)
    data = legal.model_dump(mode="json")
    reset = next(step for step in data["steps"] if step["id"] == "current-reset")
    assert reset["phase"] == "r2" and reset["depends_on"] == ["current-cleanup"]
    if damage == "missing-reset":
        data["steps"].remove(reset)
        # 只移除重建动作并展开其原依赖，避免悬空引用先于目标缺陷拒绝。
        for step in data["steps"]:
            step["depends_on"] = list(
                dict.fromkeys(
                    predecessor
                    for identifier in step["depends_on"]
                    for predecessor in (
                        reset["depends_on"] if identifier == reset["id"] else [identifier]
                    )
                )
            )
        assert all(step["id"] != reset["id"] for step in data["steps"])
        expected_error = "counterexample-resource-reset-required-before-freeze"
    elif damage == "unlinked-reset":
        reset["depends_on"] = []
        expected_error = "counterexample-resource-reset-cleanup-dependency-required-before-freeze"
    elif damage == "double-cleanup":
        first_cleanup = next(
            step for step in data["steps"] if step["id"] == "current-cleanup"
        )
        second_cleanup = copy.deepcopy(first_cleanup)
        second_cleanup.update(
            id="current-second-cleanup", depends_on=[first_cleanup["id"]]
        )
        data["steps"].insert(data["steps"].index(first_cleanup) + 1, second_cleanup)
        reset["depends_on"] = [second_cleanup["id"]]
        expected_error = "counterexample-resource-reset-required-before-freeze"
    else:
        resource = reset["binding"]["resources"][0]
        initial_path = root / resource["initial_state"]["path"]
        initial = json.loads(initial_path.read_bytes())
        for entry_value in initial["files"]:
            entry_value.pop("content_ref")
        initial_ref = execution._write_json(
            root, initial_path.with_name("fix27-hash-only-initial.json"), initial
        )
        for step in data["steps"]:
            if step["subject_id"] == "current":
                step["binding"]["resources"][0]["initial_state"] = initial_ref.model_dump()
        expected_error = "counterexample-reset-initial-bytes-required-before-freeze"
    proposed = CounterexamplePlan.model_validate(data)
    # 历史静态合同仍可解析；新执行资格必须另在真实准入入口判断。
    execution.validate_plan_contract(contract, proposed)
    folder = execution._attempts_dir(root, proposed).parent
    path = folder / "proposed-fix27.json"
    proposed_bytes = execution._json_bytes(proposed)
    path.write_bytes(proposed_bytes)
    resources = {
        resource.root: resource
        for step in legal.steps
        for resource in step.binding.resources
    }
    before = {
        location: execution._observed_resource_state(
            (resource,), deadline_ms=time.time_ns() // 1_000_000 + 10_000
        )
        for location, resource in resources.items()
    }
    initial_originals = {
        reference.path: (root / reference.path).read_bytes()
        for evidence_plan in (legal, proposed)
        for step in evidence_plan.steps
        for resource in step.binding.resources
        for reference in execution.resource_initial_artifact_refs(
            root, resource.initial_state
        )
    }
    with pytest.raises(ValueError, match=expected_error):
        if entry == "native":
            execution.run_counterexample_plan(
                root, impl, loop, proposed.task_id, path.relative_to(root).as_posix()
            )
        else:
            _execute((root, contract, proposed))
    assert path.read_bytes() == proposed_bytes
    assert not list(folder.glob("plans/*.json"))
    assert not list(root.rglob("intent.json"))
    for location, original in before.items():
        assert not (Path(location) / ".ai-sdlc-owner.json").exists()
        assert execution._observed_resource_state(
            (resources[location],), deadline_ms=time.time_ns() // 1_000_000 + 10_000
        ) == original
    assert all((root / name).read_bytes() == raw for name, raw in initial_originals.items())


@pytest.mark.parametrize(
    ("entry", "kind", "damage"),
    [
        ("native", "cleanup", "missing"),
        ("step", "cleanup", "missing"),
        ("native", "reset", "missing"),
        ("step", "exercise", "missing"),
        pytest.param(
            "native",
            "cleanup",
            "no-execute-permission",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX 文件执行权限"),
        ),
    ],
)
def test_fix29_all_planned_commands_are_executable_before_first_publication(
    execution_case, monkeypatch, entry, kind, damage
):
    root, _, _ = execution_case
    contract, impl, loop, legal = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    data = legal.model_dump(mode="json")
    if kind == "exercise":
        reset = next(step for step in data["steps"] if step["id"] == "current-reset")
        future = copy.deepcopy(reset)
        future.update(
            id="current-r2-exercise", kind="exercise", depends_on=[reset["id"]]
        )
        data["steps"].insert(data["steps"].index(reset) + 1, future)
        next(step for step in data["steps"] if step["id"] == "r2-current-none")[
            "depends_on"
        ].append(future["id"])
    else:
        future = next(
            step for step in data["steps"]
            if step["subject_id"] == "current" and step["kind"] == kind
        )
    unavailable = root.parent / f"fix29-unavailable-{kind}"
    if damage == "no-execute-permission":
        unavailable.write_text(f"#!{sys.executable}\nprint('must not launch')\n")
        unavailable.chmod(0o644)
        assert unavailable.is_file() and not os.access(unavailable, os.X_OK)
        expected_error = "counterexample-prelaunch-command-identity-stale"
    else:
        assert not unavailable.exists()
        expected_error = "counterexample-command-executable-unavailable"
    future["binding"]["argv"][0] = str(unavailable)
    proposed = CounterexamplePlan.model_validate(data)
    # 原合同和资源条件保持合法，缺陷只在尚未执行的命令是否能被启动。
    execution.validate_plan_contract(contract, proposed)
    assert proposed.steps[0].id == "current-none"
    assert proposed.steps[0].id != future["id"]
    folder = execution._attempts_dir(root, proposed).parent
    proposed_path = folder / "proposed-fix29-command.json"
    proposed_bytes = execution._json_bytes(proposed)
    proposed_path.write_bytes(proposed_bytes)
    resources = {
        resource.root: resource
        for step in proposed.steps
        for resource in step.binding.resources
    }
    before = {
        location: execution._observed_resource_state(
            (resource,), deadline_ms=time.time_ns() // 1_000_000 + 10_000
        )
        for location, resource in resources.items()
    }

    def unexpected_attempt_publication(*args, **kwargs):
        pytest.fail("新表仍有不可执行命令，却已到达首次意图发布")

    # 旧缺陷走过准入时立即留下明确失败，不为复现再运行整表业务。
    monkeypatch.setattr(execution, "_publish_attempt_intent", unexpected_attempt_publication)
    with pytest.raises(ValueError, match=expected_error):
        if entry == "native":
            execution.run_counterexample_plan(
                root, impl, loop, proposed.task_id, proposed_path.relative_to(root).as_posix()
            )
        else:
            _execute((root, contract, proposed))
    assert proposed_path.read_bytes() == proposed_bytes
    assert not list(folder.glob("plans/*.json"))
    assert not list(folder.glob("attempts/*/intent.json"))
    for location, initial in before.items():
        assert not (Path(location) / ".ai-sdlc-owner.json").exists()
        assert execution._observed_resource_state(
            (resources[location],), deadline_ms=time.time_ns() // 1_000_000 + 10_000
        ) == initial


@pytest.mark.parametrize("damage", ["cwd", "environment"])
def test_full_plan_static_admission_precedes_plan_freeze(
    execution_case, monkeypatch, damage
):
    root, contract, _ = execution_case
    contract, impl, loop, plan = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    data = plan.model_dump(mode="json")
    cleanup = next(step for step in data["steps"] if step["kind"] == "cleanup")
    if damage == "cwd":
        cleanup["binding"]["cwd"] = "missing-directory"
    else:
        environment = {
            **cleanup["binding"]["effective_environment"],
            "RUNTIME_MODE": "prod",
        }
        cleanup["binding"].update(
            effective_environment=environment,
            environment_digest=counterexample_digest(environment),
        )
    proposed = CounterexamplePlan.model_validate(data)
    path = execution._attempts_dir(root, plan).parent / "proposed.json"
    path.write_bytes(execution._json_bytes(proposed))
    resources = {
        resource.root: resource
        for step in plan.steps
        for resource in step.binding.resources
    }
    before = {
        path: execution._observed_resource_state(
            (resource,), deadline_ms=time.time_ns() // 1_000_000 + 10_000
        )
        for path, resource in resources.items()
    }
    with pytest.raises(
        (ValueError, OSError),
        match="missing-directory|environment-not-explicit-test-allowlist",
    ):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, path.relative_to(root).as_posix()
        )
    assert not list(execution._attempts_dir(root, plan).parent.glob("plans/*.json"))
    assert not list(root.rglob("intent.json"))
    for resource_root, initial in before.items():
        assert not (Path(resource_root) / ".ai-sdlc-owner.json").exists()
        assert (
            execution._observed_resource_state(
                (resources[resource_root],),
                deadline_ms=time.time_ns() // 1_000_000 + 10_000,
            )
            == initial
        )
    path.write_bytes(execution._json_bytes(plan))
    reference, assessment = execution.run_counterexample_plan(
        root, impl, loop, plan.task_id, path.relative_to(root).as_posix()
    )
    assert assessment.current_result.status == "PASS"
    for project in {step.binding.project_root for step in plan.steps}:
        shutil.rmtree(project)
    assert execution.run_counterexample_plan(
        root, impl, loop, plan.task_id, path.relative_to(root).as_posix()
    ) == (reference, assessment)


@pytest.mark.parametrize("value", ["-c", "-m", "-I", "-B", "-u"])
def test_script_option_shaped_witness_is_real_business_data(execution_case, value):
    case = _change_observer(
        execution_case,
        "import json,sys\nprint(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':sys.argv[-1]}}))\n",
    )
    root, contract, original = case
    data = original.model_dump(mode="json")
    data["witnesses"][0]["input_value"]["value"] = value
    for step in data["steps"]:
        step["binding"]["argv"][-1] = value
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_counterexample_snapshots(root, plan)
    receipt = _execute((root, contract, plan))
    observation = execution.collect_observation(
        contract, plan, receipt, execution.read_owned_raw_evidence(root, receipt)
    )
    assert observation.typed_actual.value == value
    step = plan.steps[0]
    target = Path(step.binding.project_root) / "observe.py"
    for argv, expected in (
        ([sys.executable, "-I", "-B", "-u", "observe.py", value], True),
        ([sys.executable, "-m", "observe.py"], False),
        ([sys.executable, "-c", "observe.py"], False),
        ([sys.executable, "other.py", "observe.py"], False),
        ([str(target), value], True),
    ):
        assert (
            execution._command_calls_file(
                step.model_copy(
                    update={
                        "binding": step.binding.model_copy(update={"argv": tuple(argv)})
                    }
                ),
                target,
            )
            is expected
        )


@pytest.mark.parametrize(
    "new_candidate,replayed", [(False, True), (True, False), (True, True)]
)
def test_current_subject_false_rejection_survives_state_readback(
    execution_case, monkeypatch, new_candidate, replayed
):
    root, _, _ = execution_case
    impl, progress, _, assessment, _, _ = _same_head_repaired_state(
        execution_case, monkeypatch
    )
    resolve = execution.resolve_counterexample_evidence
    old_ref = progress.tasks[0].counterexample_results[0]
    legal = SimpleNamespace(
        subject_id="current",
        obligation_id="save",
        business=SimpleNamespace(status="PASS"),
        v0="accepted",
        v1="accepted",
    )
    assessment.current_subjects = (legal,)
    assessment.required_complete = replayed

    def with_rejection(*args, **kwargs):
        bound = resolve(*args, **kwargs)
        if args[2] == old_ref:
            bound.assessment.current_subjects = (
                SimpleNamespace(**{**vars(legal), "v1": "false_rejection"}),
            )
            if not new_candidate:
                latest = resolve(
                    root, impl, progress.tasks[0].counterexample_results[-1]
                )
                bound.plan = latest.plan
        return bound

    monkeypatch.setattr(execution, "resolve_counterexample_evidence", with_rejection)
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("legitimate control was rejected" in item for item in blockers) is (
        not new_candidate or not replayed
    )


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("resource_state", ["missing", "active", "unknown"])
def test_optional_missing_owner_state_uses_required_coverage(
    execution_case, monkeypatch, required, resource_state
):
    from ai_sdlc.core import counterexample_evaluation as evaluation
    from tests.unit.test_counterexample_evaluation import make_bundle
    from tests.unit.test_counterexample_models import task_owned_contract_data

    root, _, original = execution_case
    data = task_owned_contract_data()
    data["obligations"][1]["required"] = required
    data["obligations"][0]["oracle_spec"]["assertion_id"] = "saved-state"
    contract = VerificationContract.model_validate(data)
    plan = original.model_copy(
        update={
            "task_id": "T11",
            "contract_digest": counterexample_digest(contract),
            "contract_model_digest": counterexample_digest(contract),
        }
    )
    case = root, contract, plan
    aggregate = evaluation.aggregate_task_assessments
    impl, progress, _, _, _ = _closed_delivery_state(case, monkeypatch, delivered=False)
    impl.task_scopes["T21"] = ("src/other.py",)
    # 消费真实纯核结果；原 fixture 仅隔离无关的原生交付证明。
    from tests.unit.test_counterexample_evaluation import evaluate_counterexample

    assessment = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, v0_variant="rejected")
    )
    monkeypatch.setattr(evaluation, "aggregate_task_assessments", aggregate)
    monkeypatch.setattr(
        evaluation, "evaluate_counterexample", lambda *a, **k: assessment
    )
    monkeypatch.setattr(
        execution,
        "resolve_counterexample_evidence",
        lambda *a, **k: SimpleNamespace(
            contract=contract,
            plan=plan,
            assessment=assessment,
            observations=SimpleNamespace(attempts=()),
            artifact_refs=(),
        ),
    )
    if resource_state != "missing":
        second = plan.model_copy(
            update={
                "task_id": "T21",
                "subjects": tuple(
                    subject.model_copy(update={"obligation_id": "save-other"})
                    for subject in plan.subjects
                ),
                "steps": tuple(
                    step.model_copy(
                        update={
                            "assertion_id": contract.obligations[
                                1
                            ].oracle_spec.assertion_id
                        }
                    )
                    for step in plan.steps
                ),
            }
        )
        execution._write_json(
            root,
            execution._attempts_dir(root, second).parent
            / f"plans/{counterexample_digest(second)}.json",
            second,
        )
        receipt = _execute((root, contract, second))
        assert receipt.status == "completed"
        if resource_state == "unknown":
            (root / receipt.attempt_ref.path).with_name("raw-result.json").unlink()
    blockers, advisories, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert bool(blockers) is (required or resource_state != "missing")
    if resource_state != "missing":
        assert any(
            "execution-unknown" in item or "resource-cleanup-required" in item
            for item in blockers
        )
    assert any("T21" in item for item in (blockers if required else advisories))
    assert any("acceptance=retain_v0" in item for item in advisories)


def _two_task_reserved_case(case, monkeypatch, maximum):
    from tests.unit.test_counterexample_models import task_owned_contract_data

    root, original_contract, original_plan = case
    for project in {
        root,
        *(Path(step.binding.project_root) for step in original_plan.steps),
    }:
        for version in ("v0", "v1"):
            path = project / f"tests/{version}.py"
            path.write_text(
                path.read_text()
                .replace("import json", "import json,sys")
                .replace("'assertion_id':'saved-state'", "'assertion_id':sys.argv[-1]")
            )
    original_plan = original_plan.model_copy(
        update={
            **{
                version + "_digest": hashlib.sha256(
                    (root / f"tests/{version}.py").read_bytes()
                ).hexdigest()
                for version in ("v0", "v1")
            },
            "steps": tuple(
                step.model_copy(
                    update={
                        "binding": step.binding.model_copy(
                            update={"argv": (*step.binding.argv, step.assertion_id)}
                        )
                    }
                )
                if step.kind == "acceptance"
                else step
                for step in original_plan.steps
            ),
        }
    )
    case = _change_observer(
        (root, original_contract, original_plan), (root / "observe.py").read_text()
    )
    root, _, original = _case_with_frozen_resets(case)
    contract_data = task_owned_contract_data()
    contract_data["obligations"][0]["oracle_spec"]["assertion_id"] = "saved-state"
    contract = VerificationContract.model_validate(contract_data)
    data = original.model_dump(mode="json")
    data.update(
        task_id="T11",
        contract_digest=counterexample_digest(contract),
        contract_model_digest=counterexample_digest(contract),
        max_execution_attempts=maximum,
    )
    final = []
    for subject in data["subjects"]:
        steps = [
            step
            for step in data["steps"]
            if step["subject_id"] == subject["id"] and step["phase"] == "final"
        ]
        reset = copy.deepcopy(
            next(
                step for step in data["steps"] if step["id"] == subject["id"] + "-reset"
            )
        )
        reset.update(phase="final", depends_on=[])
        exercise = copy.deepcopy(reset)
        exercise.update(
            id=subject["id"] + "-exercise", kind="exercise", depends_on=[reset["id"]]
        )
        steps[0]["depends_on"] = [exercise["id"]]
        final.extend([reset, exercise, *steps])
    r2 = copy.deepcopy(final)
    for step in r2:
        step.update(
            id="r2-" + step["id"],
            phase="r2",
            depends_on=["r2-" + item for item in step["depends_on"]],
        )
        if step["kind"] == "reset":
            step["depends_on"] = [step["subject_id"] + "-cleanup"]
        if step["business_observation_step_id"]:
            step["business_observation_step_id"] = (
                "r2-" + step["business_observation_step_id"]
            )
    data["steps"] = final + r2
    first = CounterexamplePlan.model_validate(data)
    contract, impl, loop, first = _run_admission_case(
        (root, contract, first), monkeypatch
    )
    impl.task_scopes["T21"] = first.allowed_modified_paths
    data = first.model_dump(mode="json")
    data.update(id="second-task", task_id="T21")
    assertion = contract.obligations[1].oracle_spec.assertion_id
    for subject in data["subjects"]:
        source = Path(
            next(
                step.binding.project_root
                for step in first.steps
                if step.subject_id == subject["id"]
            )
        )
        target = source.with_name("T21-" + source.name)
        shutil.copytree(source, target)
        prefix = f".ai-sdlc/loops/implementation/{first.loop_id}/counterexamples/second-task/{subject['id']}"
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=prefix,
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
        )
        subject.update(
            obligation_id="save-other",
            snapshot=execution._write_json(
                root, root / prefix / "manifest.json", manifest
            ).model_dump(),
        )
        for step in data["steps"]:
            if step["subject_id"] == subject["id"]:
                step["assertion_id"] = assertion
                binding = step["binding"]
                if step["kind"] == "acceptance":
                    binding["argv"][-1] = assertion
                binding["project_root"] = str(target)
                binding["argv"] = [
                    arg.replace(str(source), str(target)) for arg in binding["argv"]
                ]
                for resource in binding["resources"]:
                    for name in ("root", "observation_endpoint"):
                        resource[name] = resource[name].replace(
                            str(source), str(target)
                        )
    return contract, impl, loop, first, CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize("maximum", [54, 72])
def test_other_task_cannot_spend_reserved_final_and_r2(
    execution_case, monkeypatch, maximum
):
    root, _, _ = execution_case
    contract, impl, loop, first, second = _two_task_reserved_case(
        execution_case, monkeypatch, maximum
    )
    folder = execution._attempts_dir(root, first).parent
    execution._write_json(
        root, folder / f"plans/{counterexample_digest(first)}.json", first
    )
    assert len(first.steps) == len(second.steps) == 36
    for step in first.steps[:18]:
        assert (
            _execute(
                (root, contract, first),
                step.id,
                deadline_ms=time.time_ns() // 1_000_000 + 10_000_000,
            ).status
            == "completed"
        )
    history_before = {
        path: path.read_bytes() for path in folder.rglob("*") if path.is_file()
    }
    if maximum == 54:
        proposed = folder / "proposed.json"
        proposed.write_bytes(execution._json_bytes(second))
        with pytest.raises(
            ValueError, match="attempt-budget-required-steps-unavailable"
        ):
            execution.run_counterexample_plan(
                root, impl, loop, second.task_id, proposed.relative_to(root).as_posix()
            )
        with pytest.raises(
            ValueError, match="attempt-budget-required-steps-unavailable"
        ):
            _execute((root, contract, second), second.steps[0].id)
        assert not (folder / f"plans/{counterexample_digest(second)}.json").exists()
        assert history_before == {path: path.read_bytes() for path in history_before}
        assert len(list(folder.glob("attempts/*/intent.json"))) == 18
        assert not (
            Path(second.steps[0].binding.resources[0].root) / ".ai-sdlc-owner.json"
        ).exists()
    else:
        execution._write_json(
            root, folder / f"plans/{counterexample_digest(second)}.json", second
        )
        for plan, steps in (
            (second, second.steps[:18]),
            (first, first.steps[18:]),
            (second, second.steps[18:]),
        ):
            for step in steps:
                assert (
                    _execute(
                        (root, contract, plan),
                        step.id,
                        deadline_ms=time.time_ns() // 1_000_000 + 10_000_000,
                    ).status
                    == "completed"
                )
        history, debts = execution._historical_resource_cycles(root, second)
        assert len(history) == 72 and not debts


def test_other_task_frozen_before_first_intent_still_reserves_budget(
    execution_case, monkeypatch
):
    root, _, _ = execution_case
    contract, _, _, first, second = _two_task_reserved_case(
        execution_case, monkeypatch, 54
    )
    folder = execution._attempts_dir(root, first).parent
    execution._write_json(
        root, folder / f"plans/{counterexample_digest(first)}.json", first
    )
    with pytest.raises(ValueError, match="attempt-budget-required-steps-unavailable"):
        _execute((root, contract, second), second.steps[0].id)
    assert not list(folder.glob("attempts/*/intent.json"))


def test_same_task_unstarted_successor_cannot_replace_reservation(execution_case, monkeypatch):
    root, _, _ = execution_case
    contract, _, _, first, _ = _two_task_reserved_case(execution_case, monkeypatch, 54)
    folder = execution._attempts_dir(root, first).parent
    previous = execution._write_json(
        root, folder / f"plans/{counterexample_digest(first)}.json", first
    )
    original = (root / previous.path).read_bytes()
    successor = first.model_copy(update={"id": "same-task-successor"})
    # 未发布的接管能力已撤出：不能靠替换同任务储备开启后继。
    with pytest.raises(ValueError, match="counterexample-unfinished-predecessor-no-takeover"):
        _execute((root, contract, successor), successor.steps[0].id)
    assert (root / previous.path).read_bytes() == original
    assert not list(folder.glob("attempts/*/intent.json"))
    assert not (folder / f"plans/{counterexample_digest(successor)}.json").exists()


def test_zero_intent_predecessor_refuses_takeover_but_other_task_remains_executable(
    execution_case, monkeypatch
):
    root, _, _ = execution_case
    contract, _, _, first, second = _two_task_reserved_case(
        execution_case, monkeypatch, 72
    )
    folder = execution._attempts_dir(root, first).parent
    parent_ref = execution._write_json(
        root, folder / f"plans/{counterexample_digest(first)}.json", first
    )
    execution._write_json(root, folder / f"plans/{counterexample_digest(second)}.json", second)
    parent_bytes = (root / parent_ref.path).read_bytes()
    successor = first.model_copy(update={"id": "same-task-successor"})
    with pytest.raises(ValueError, match="counterexample-unfinished-predecessor-no-takeover"):
        _execute((root, contract, successor), successor.steps[0].id)
    assert not list(folder.glob("attempts/*/intent.json"))
    assert not (folder / f"plans/{counterexample_digest(successor)}.json").exists()
    # 拒绝的是本任务接管，不吞掉别的正常任务或它的已启动清理。
    receipt = _execute((root, contract, second), second.steps[0].id)
    assert receipt.normally_completed
    saved = (root / receipt.attempt_ref.path).read_bytes()
    assert "superseded_plan_refs" not in json.loads(saved)
    removed_format = {**json.loads(saved), "superseded_plan_refs": [parent_ref.model_dump(mode="json")]}
    with pytest.raises(ValueError, match="counterexample-plan-takeover-not-supported"):
        execution._validated_attempt_intent(removed_format)
    assert execution.recover_counterexample_attempt(root, second, receipt.attempt_ref) == receipt
    cleaned = _execute((root, contract, second), "current-cleanup", cleanup_recovery=True)
    assert cleaned.normally_completed
    assert (root / receipt.attempt_ref.path).read_bytes() == saved
    assert (root / parent_ref.path).read_bytes() == parent_bytes
    assert not list((folder / "results").glob("record-reconciled-*.json"))


def test_started_predecessor_cleanup_finishes_before_successor_refusal(
    execution_case, monkeypatch
):
    root, _, _ = execution_case
    contract, impl, loop, prior, _ = _two_task_reserved_case(
        execution_case, monkeypatch, 72
    )
    folder = execution._attempts_dir(root, prior).parent
    prior_ref = execution._write_json(
        root, folder / f"plans/{counterexample_digest(prior)}.json", prior
    )
    first = _execute(
        (root, contract, prior), prior.steps[0].id,
        deadline_ms=time.time_ns() // 1_000_000 + 10_000_000,
    )
    assert first.normally_completed
    original_bytes = (root / first.attempt_ref.path).read_bytes()
    successor = prior.model_copy(update={"id": "same-task-successor"})
    proposed = folder / "proposed-successor.json"
    proposed.write_bytes(execution._json_bytes(successor))
    with pytest.raises(ValueError, match="counterexample-unfinished-predecessor-no-takeover"):
        execution.run_counterexample_plan(
            root, impl, loop, prior.task_id, proposed.relative_to(root).as_posix()
        )
    history, debts = execution._historical_resource_cycles(root, successor)
    assert [(saved.id, intent["kind"]) for _, intent, saved, _ in history] == [
        (prior.id, "reset"), (prior.id, "cleanup"),
    ]
    assert all(receipt.normally_completed for _, _, _, receipt in history)
    assert not debts
    assert execution._ref(root, root / prior_ref.path) == prior_ref
    assert (root / first.attempt_ref.path).read_bytes() == original_bytes
    assert not (folder / f"plans/{counterexample_digest(successor)}.json").exists()
    assert all("superseded_plan_refs" not in intent for _, intent, _, _ in history)


@pytest.mark.parametrize("payload", ["not-json", "valid", "acceptance-not-json"])
def test_unrecorded_optional_observation_is_parsed_before_advisory(
    execution_case, monkeypatch, payload
):
    code = (
        "print('not-json')\n"
        if payload == "not-json"
        else "import json\nprint(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
    )
    case = execution_case
    if payload == "acceptance-not-json":
        root, contract, plan = case
        for project in {
            root,
            *(Path(step.binding.project_root) for step in plan.steps),
        }:
            (project / "tests/v0.py").write_text("print('not-json')\n")
        plan = plan.model_copy(
            update={
                "v0_digest": hashlib.sha256(
                    (root / "tests/v0.py").read_bytes()
                ).hexdigest()
            }
        )
        case = root, contract, plan
    root, contract, plan = _change_observer(case, code)
    contract = contract.model_copy(
        update={
            "obligations": tuple(
                item.model_copy(update={"required": False})
                for item in contract.obligations
            )
        }
    )
    contract, impl, _, plan = _run_admission_case((root, contract, plan), monkeypatch)
    execution._write_json(
        root,
        execution._attempts_dir(root, plan).parent
        / f"plans/{counterexample_digest(plan)}.json",
        plan,
    )
    receipt = _execute((root, contract, plan))
    assert receipt.status == "completed"
    if payload == "acceptance-not-json":
        receipt = _execute((root, contract, plan), "current-V0")
        assert receipt.status == "completed"
    assert (
        _execute(
            (root, contract, plan), "current-cleanup", cleanup_recovery=True
        ).status
        == "completed"
    )
    assert not execution._historical_resource_cycles(root, plan)[1]
    progress = SimpleNamespace(
        tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])]
    )

    def state(material=None):
        return execution.counterexample_verification_state(
            root, impl, progress, captured_artifacts=material, require_r2=False
        )

    blockers, advisories, refs = state()
    captured = {
        reference.path: (root / reference.path).read_bytes() for reference in refs
    }
    assert bool(blockers) is (payload != "valid")
    assert bool(state(captured)[0]) is (payload != "valid")
    assert any("execution-table-incomplete" in item for item in advisories)
    stdout = next(
        reference
        for reference in receipt.raw_evidence_refs
        if reference.path.endswith("/stdout")
    )
    for damage in ("missing", "changed"):
        damaged = dict(captured)
        if damage == "missing":
            damaged.pop(stdout.path)
        else:
            damaged[stdout.path] = b"wrong bytes"
        assert state(damaged)[0]


def _ce001_step_seconds(step):
    return step.binding.timeout_seconds + step.reservation_seconds + 5


@pytest.mark.parametrize("reservation", ["r2", "repair", "other-task"])
def test_ce001_run_reserves_all_shared_frozen_step_time(
    execution_case, monkeypatch, reservation
):
    from ai_sdlc.core.counterexample_models import required_execution_steps

    root, _, _ = execution_case
    if reservation in {"other-task", "repair"}:
        contract, impl, loop, other, plan = _two_task_reserved_case(
            execution_case, monkeypatch, 72
        )
        if reservation == "repair":
            data = other.model_dump(mode="json")
            # repair 使用同合同另一任务的独立资源周期；当前任务仍保留完整 R2。
            # 不把 repair 接在 R2 后却仍依赖 final cleanup，制造无法执行的时间夹具。
            for step in data["steps"]:
                if step["phase"] == "r2":
                    step["phase"] = "repair"
            other = CounterexamplePlan.model_validate(data)
        execution.validate_plan_contract(contract, other)
        execution._new_plan_resource_rebuilds(other)
        folder = execution._attempts_dir(root, plan).parent
        execution._write_json(
            root, folder / f"plans/{counterexample_digest(other)}.json", other
        )
    else:
        case = _case_with_frozen_resets(execution_case)
        contract, impl, loop, plan = _run_admission_case(case, monkeypatch)
        folder = execution._attempts_dir(root, plan).parent
    execution.validate_plan_contract(contract, plan)
    execution._new_plan_resource_rebuilds(plan)
    bundle = execution._bundle_from_attempts(root, contract, plan, ())
    active = required_execution_steps(plan, bundle, require_r2=False)
    pending = execution._shared_pending_steps(root, contract, plan, (), {})
    active_seconds = sum(map(_ce001_step_seconds, active))
    required_seconds = sum(_ce001_step_seconds(step) for _, step in pending)
    window_seconds = active_seconds + plan.required_reserve_seconds + 10
    if reservation == "repair":
        # 除 repair 外的全部原承诺都放得下；漏计 repair 本身才会使这个反例失效。
        window_seconds = (
            plan.required_reserve_seconds
            + 10
            + sum(
                _ce001_step_seconds(step)
                for _, step in pending
                if step.phase != "repair"
            )
        )
    assert required_seconds + plan.required_reserve_seconds > window_seconds
    started_at_ms = time.time_ns() // 1_000_000
    monkeypatch.setattr(
        "ai_sdlc.core.loop_decision_service.validate_implementation_context",
        lambda *a, **k: SimpleNamespace(
            capability="stage-simulation-v1",
            started_at_ms=started_at_ms,
            plan=SimpleNamespace(
                time_plan=SimpleNamespace(window_seconds=window_seconds)
            ),
        ),
    )
    proposed = folder / "time-reservation-proposed.json"
    proposed.write_bytes(execution._json_bytes(plan))
    execute = execution.execute_counterexample_attempt

    def reject_any_started_action(*args, **kwargs):
        receipt = execute(*args, **kwargs)
        pytest.fail(f"shared time shortage started actual attempt {receipt.attempt_id}")

    monkeypatch.setattr(
        execution, "execute_counterexample_attempt", reject_any_started_action
    )
    with pytest.raises(ValueError, match="time-budget-reserve-unavailable"):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
    assert not list(folder.glob("attempts/*/intent.json"))
    assert not (
        Path(plan.steps[0].binding.resources[0].root) / ".ai-sdlc-owner.json"
    ).exists()


@pytest.mark.parametrize("caller_extra_reserve", [0, 50])
def test_ce001_direct_attempt_excludes_current_step_once_and_keeps_caller_reserve(
    execution_case, caller_extra_reserve
):
    root, contract, plan = _case_with_frozen_resets(execution_case)
    pending = execution._shared_pending_steps(root, contract, plan, (), {})
    current = plan.steps[0]
    full_seconds = (
        sum(_ce001_step_seconds(step) for _, step in pending)
        + plan.required_reserve_seconds
    )
    # 窗口足以覆盖整表，但不能再次多扣当前步骤；额外调用方承诺仍须保留。
    window_seconds = full_seconds + 10
    caller_reserve = full_seconds - _ce001_step_seconds(current) + caller_extra_reserve
    deadline_ms = time.time_ns() // 1_000_000 + window_seconds * 1000
    if caller_extra_reserve:
        with pytest.raises(ValueError, match="time-budget-reserve-unavailable"):
            _execute(
                (root, contract, plan),
                current.id,
                deadline_ms=deadline_ms,
                remaining_reserve_seconds=caller_reserve,
            )
        assert not list(root.rglob("intent.json"))
    else:
        receipt = _execute(
            (root, contract, plan),
            current.id,
            deadline_ms=deadline_ms,
            remaining_reserve_seconds=caller_reserve,
        )
        assert receipt.status == "completed" and receipt.exit_code == 0
        assert len(list(root.rglob("intent.json"))) == 1


@pytest.mark.parametrize("window", ["initial-digest", "final-history-read"])
@pytest.mark.parametrize("damage", [None, "blob", "mode"])
def test_ce002_consumption_rechecks_index_through_last_original_read(
    execution_case, monkeypatch, window, damage
):
    root, _, original_plan = execution_case
    if window == "final-history-read":
        impl, progress, *_ = _same_head_repaired_state(execution_case, monkeypatch)
    else:
        impl, progress, *_ = _closed_delivery_state(
            execution_case, monkeypatch, delivered=False
        )
    assert not execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )[0]
    source_before = execution.build_source_digest(root)
    head_before = _git(root, "rev-parse", "HEAD").stdout
    files_before = execution._file_map(root, ())
    index_before = _git(root, "ls-files", "-s", "--", "src/save.py").stdout
    injected = False

    def change_index_only():
        nonlocal injected
        if injected:
            return
        injected = True
        if damage == "blob":
            blob = (
                subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=root,
                    input=b"VALUE='changed-only-in-index'\n",
                    check=True,
                    capture_output=True,
                )
                .stdout.decode()
                .strip()
            )
            _git(root, "update-index", "--cacheinfo", f"100644,{blob},src/save.py")
        elif damage == "mode":
            _git(root, "update-index", "--chmod=+x", "src/save.py")

    if window == "initial-digest":
        build_digest = execution.build_source_digest

        def change_after_initial_digest(*args, **kwargs):
            digest = build_digest(*args, **kwargs)
            change_index_only()
            return digest

        monkeypatch.setattr(
            execution, "build_source_digest", change_after_initial_digest
        )
    else:
        bound_read = execution._bound_read
        original_path = (
            (
                execution._attempts_dir(root, original_plan).parent
                / f"plans/{counterexample_digest(original_plan)}.json"
            )
            .relative_to(root)
            .as_posix()
        )

        def change_after_last_history_read(project, reference, *args, **kwargs):
            raw = bound_read(project, reference, *args, **kwargs)
            if reference.path == original_path:
                change_index_only()
            return raw

        monkeypatch.setattr(execution, "_bound_read", change_after_last_history_read)
    blockers, advisories, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert injected
    assert _git(root, "rev-parse", "HEAD").stdout == head_before
    assert execution._file_map(root, ()) == files_before
    index_after = _git(root, "ls-files", "-s", "--", "src/save.py").stdout
    if damage is None:
        assert index_after == index_before
        assert execution.build_source_digest(root) == source_before
        assert not blockers
        assert any("business=PASS" in item for item in advisories)
    else:
        assert index_after != index_before
        assert execution.build_source_digest(root) != source_before
        assert any("source-identity-unavailable" in item for item in blockers)


def _same_resource_reset_case(case, mode):
    """真实探索写入后在同一归属目录重置；清理命令独立于业务文件存在。"""
    root, contract, original = case
    data = original.model_dump(mode="json")
    original_steps = data["steps"]
    steps = []
    for subject in data["subjects"]:
        observe = next(
            step
            for step in original_steps
            if step["subject_id"] == subject["id"] and step["kind"] == "observe"
        )
        endpoint = observe["binding"]["resources"][0]["observation_endpoint"]
        exercise = copy.deepcopy(observe)
        exercise.update(
            id=subject["id"] + "-explore",
            kind="exercise",
            phase="exploration",
            depends_on=[],
        )
        value = "saved" if mode == "unchanged" else "stale"
        exercise["binding"]["argv"] = [
            sys.executable,
            "-c",
            "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("
            + repr(json.dumps({"value": value}, separators=(",", ":")))
            + ")",
            endpoint,
            "request",
        ]
        exercise["binding"]["witness_input"]["argv_index"] = 4
        reset = copy.deepcopy(observe)
        reset.update(
            id=subject["id"] + "-reset", kind="reset", depends_on=[exercise["id"]]
        )
        code = "print('reset')"
        if mode == "missing":
            code = "import pathlib,sys;pathlib.Path(sys.argv[1]).unlink()"
        elif mode in {"restored", "extra"}:
            code = 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(\'{"value":"saved"}\')'
            if mode == "extra":
                code += ";pathlib.Path(sys.argv[1]).with_name('unexpected.json').write_text('{}')"
        reset["binding"].update(
            argv=[sys.executable, "-c", code, endpoint], witness_input=None
        )
        group = [
            copy.deepcopy(step)
            for step in original_steps
            if step["subject_id"] == subject["id"]
        ]
        group[0]["depends_on"] = [reset["id"]]
        group[-1]["binding"]["argv"] = [
            sys.executable,
            "-c",
            "print('owned cleanup')",
            endpoint,
        ]
        steps.extend([exercise, reset, *group])
    data.update(steps=steps, max_execution_attempts=36)
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(contract, plan)
    return root, contract, plan


@pytest.mark.parametrize("mode", ["noop", "missing", "extra", "restored", "unchanged"])
def test_reset_verifies_frozen_initial_state_before_business(execution_case, mode):
    case = _same_resource_reset_case(execution_case, mode)
    root, _, plan = case
    exercise = _execute(case, "current-explore")
    assert exercise.status == "completed"
    reset = _execute(case, "current-reset")
    try:
        if mode in {"restored", "unchanged"}:
            assert reset.status == "completed"
            observed = _execute(case, "current-none")
            assert observed.status == "completed"
            assert (
                Path(plan.steps[0].binding.resources[0].root) / "result.json"
            ).read_bytes() == b'{"value":"saved"}'
        else:
            assert reset.status != "completed", (
                "reset left a resource different from its frozen initial state"
            )
            attempts_before = set(
                execution._attempts_dir(root, plan).glob("*/intent.json")
            )
            with pytest.raises(
                ValueError, match="prior-operation-failed-business-stopped"
            ):
                _execute(case, "current-none")
            assert (
                set(execution._attempts_dir(root, plan).glob("*/intent.json"))
                == attempts_before
            )
    finally:
        cleanup = _execute(case, "current-cleanup", cleanup_recovery=True)
        assert cleanup.cleanup_status == "complete"
        assert not Path(plan.steps[0].binding.resources[0].root).exists()


@pytest.mark.parametrize("extra", ["empty-directory", "nested-owner"])
def test_reset_rejects_unfrozen_directory_state(execution_case, extra):
    root, contract, original = _same_resource_reset_case(execution_case, "restored")
    data = original.model_dump(mode="json")
    for step in data["steps"]:
        if step["kind"] == "reset":
            step["binding"]["argv"][2] += (
                ";p=pathlib.Path(sys.argv[1]).with_name('unexpected');p.mkdir()"
            )
            if extra == "nested-owner":
                step["binding"]["argv"][2] += (
                    ";(p/'.ai-sdlc-owner.json').write_text('{}')"
                )
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    assert _execute(case, "current-explore").status == "completed"
    reset = _execute(case, "current-reset")
    resource = Path(plan.steps[0].binding.resources[0].root)
    try:
        assert reset.status != "completed", (
            "reset must account for the complete frozen directory state"
        )
        if extra == "nested-owner":
            marker = resource / "unexpected/.ai-sdlc-owner.json"
            marker_original = marker.read_bytes()
            result_original = (resource / "result.json").read_bytes()
            attempts = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
            with pytest.raises(ValueError, match="resource-state-nested-owner") as denied:
                _execute(case, "current-cleanup", cleanup_recovery=True)
            assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == attempts
            assert resource.is_dir() and marker.read_bytes() == marker_original == b"{}"
            assert (resource / "result.json").read_bytes() == result_original
            assert execution.recover_counterexample_attempt(root, plan, reset.attempt_ref) == reset
            # 异属标记不能授权框架删除；先记录真实副作用和拒绝，再由测试回收自己的夹具。
            (root.parent / "nested-owner-cleanup-denial.json").write_text(
                json.dumps(
                    {
                        "reset": reset.model_dump(mode="json"),
                        "cleanup_error": str(denied.value),
                        "new_cleanup_intent": False,
                        "resource_retained": True,
                        "marker_hex": marker_original.hex(),
                        "result_hex": result_original.hex(),
                        "cleanup_owner": "test-fixture-only",
                    }
                ),
                encoding="utf-8",
            )
    finally:
        if extra == "nested-owner":
            assert resource.is_relative_to(root.parent)
            shutil.rmtree(resource)
        else:
            assert (
                _execute(case, "current-cleanup", cleanup_recovery=True).cleanup_status
                == "complete"
            )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "completion-bytes",
        "completion-delete",
        "empty-attempt",
        "extra-output",
        "optional-output",
    ],
)
def test_state_read_window_rechecks_completed_originals_and_history_members(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    impl, progress, _, _, artifacts = _closed_delivery_state(
        execution_case, monkeypatch, delivered=False
    )
    execution._write_json(
        root,
        artifacts.loop_dir
        / f"counterexamples/plans/{counterexample_digest(plan)}.json",
        plan,
    )
    receipt = _execute(execution_case)
    _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    folder = (root / receipt.attempt_ref.path).parent
    original = execution._historical_attempt_views
    observed = False

    def change_after_completed_read(*args, **kwargs):
        nonlocal observed
        result = original(*args, **kwargs)
        if observed:
            return result
        observed = True
        if damage == "completion-bytes":
            path = folder / "completion.json"
            path.write_bytes(path.read_bytes() + b"\n")
        elif damage == "completion-delete":
            (folder / "completion.json").unlink()
        elif damage == "empty-attempt":
            (folder.parent / "late-orphan").mkdir()
        elif damage == "extra-output":
            (folder / "late-output").write_bytes(b"new original output")
        elif damage == "optional-output":
            assert not (folder / "postcheck-error.json").exists()
            (folder / "postcheck-error.json").write_bytes(b"{}")
        return result

    monkeypatch.setattr(
        execution, "_historical_attempt_views", change_after_completed_read
    )
    blockers, _, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False
    )
    assert observed
    if damage is None:
        assert not blockers
        assert receipt.attempt_ref in refs
        assert folder.joinpath("completion.json").relative_to(root).as_posix() in {
            ref.path for ref in refs
        }
    else:
        assert blockers, "已读取的真实完成原件或历史成员改变后不得返回可消费状态"


@pytest.mark.parametrize(
    "delivered,damage",
    [(False, None), (False, "ignored-input"), (True, None), (True, "close")],
)
def test_state_checks_live_inputs_and_delivery_after_original_readback(
    execution_case, monkeypatch, delivered, damage
):
    root, contract, plan = execution_case
    subjects = []
    for subject in plan.subjects:
        target = Path(json.loads((root / subject.snapshot.path).read_bytes())["root"])
        manifest = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=f".ai-sdlc/loops/implementation/{plan.loop_id}/last-read-ignored/{subject.id}",
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
            ignored_inputs=["scratch/result.json"],
        )
        reference = execution._write_json(
            root, root / f".ai-sdlc/state/last-read-ignored-{subject.id}.json", manifest
        )
        subjects.append(subject.model_copy(update={"snapshot": reference}))
    plan = plan.model_copy(update={"subjects": tuple(subjects)})
    (root / "scratch").mkdir()
    (root / "scratch/result.json").write_bytes(
        (target / "scratch/result.json").read_bytes()
    )
    impl, progress, _, _, artifacts = _closed_delivery_state(
        (root, contract, plan), monkeypatch, delivered=delivered
    )
    assert not execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )[0]
    original = execution._CurrentStateMaterial.verify_originals
    checked = False

    def change_after_originals(material):
        nonlocal checked
        original(material)
        checked = True
        if damage == "ignored-input":
            (root / "scratch/result.json").write_bytes(b"changed ignored input")
        elif damage == "close":
            body = json.loads(artifacts.close_path.read_bytes())
            body["loop_id"] = "different-loop"
            artifacts.close_path.write_text(json.dumps(body))

    monkeypatch.setattr(
        execution._CurrentStateMaterial, "verify_originals", change_after_originals
    )
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert checked
    if damage is None:
        assert not blockers
    else:
        assert blockers, "原件复读之后的现场输入或交付变化仍须阻断"


@pytest.mark.parametrize("reader", ["reference", "snapshot", "raw"])
@pytest.mark.parametrize("damage", [None, "missing", "stale"])
def test_internal_material_keeps_live_required_reader_contract(
    execution_case, reader, damage
):
    root, _, plan = execution_case
    reference = plan.subjects[0].snapshot
    path = root / reference.path

    def read(captured):
        if reader == "reference":
            return execution._bound_read(root, reference, captured)
        if reader == "snapshot":
            return execution.validate_counterexample_snapshots(
                root, plan, captured_artifacts=captured, require_live=False
            )
        # 这里只核原始文本读取契约，元数据不构成一次实际执行的证明。
        receipt = SimpleNamespace(
            raw_evidence_refs=(reference,),
            attempt_id="reader-contract",
            ownership_nonce="reader-contract",
            status="completed",
            output_truncated=False,
        )
        return execution.read_owned_raw_evidence(
            root, receipt, captured_artifacts=captured
        )

    if damage == "missing":
        path.unlink()
    elif damage == "stale":
        path.write_bytes(b"changed original content")
    material = execution._CurrentStateMaterial(root, plan.loop_id, {})
    if damage is not None:
        with pytest.raises((OSError, ValueError)) as live_error:
            read(None)
        with pytest.raises(type(live_error.value)) as internal_error:
            read(material)
        assert str(internal_error.value) == str(live_error.value)
    else:
        expected = read(None)
        assert read(material) == expected
        material.verify_originals()
        assert read(dict(material)) == expected
        assert path.is_file()
        # 显式外部集合缺页时，正确的现场文件也不能补入捕获材料。
        captured_errors = {
            "reference": "counterexample-captured-evidence-missing",
            "snapshot": "counterexample-captured-artifact-missing-or-stale",
            "raw": "counterexample-raw-content-missing-or-stale",
        }
        with pytest.raises(ValueError, match=captured_errors[reader]):
            read({})
        stale = dict(material)
        stale[reference.path] = b"changed captured content"
        error = (
            "counterexample-captured-evidence-stale"
            if reader == "reference"
            else captured_errors[reader]
        )
        with pytest.raises(ValueError, match=error):
            read(stale)


@pytest.mark.parametrize("missing", [False, True])
def test_internal_material_keeps_required_task_file_error(tmp_path, missing):
    path = tmp_path / "tasks.md"
    path.write_text("# Tasks\n\n### Task T01: Save result\n- Acceptance: saved\n")
    impl = SimpleNamespace(
        loop_id="required-tasks", task_scopes={}, tasks_path="tasks.md"
    )
    contract = make_contract()
    if missing:
        path.unlink()
    material = execution._CurrentStateMaterial(tmp_path, impl.loop_id, {})
    if missing:
        with pytest.raises((OSError, ValueError)) as live_error:
            execution._implementation_obligation_owners(tmp_path, impl, contract)
        with pytest.raises(type(live_error.value)) as internal_error:
            execution._implementation_obligation_owners(
                tmp_path, impl, contract, material
            )
        assert str(internal_error.value) == str(live_error.value)
    else:
        expected = execution._implementation_obligation_owners(tmp_path, impl, contract)
        assert expected == {"save": "T01"}
        assert (
            execution._implementation_obligation_owners(
                tmp_path, impl, contract, material
            )
            == expected
        )
        assert (
            execution._implementation_obligation_owners(
                tmp_path, impl, contract, dict(material)
            )
            == expected
        )
        with pytest.raises(ValueError, match="original-task-ownership-unavailable"):
            execution._implementation_obligation_owners(tmp_path, impl, contract, {})
        material.verify_originals()


@pytest.mark.parametrize("appears", [False, True])
def test_required_original_cannot_fill_an_observed_absence(tmp_path, appears):
    content = b"current original"
    reference = execution.ArtifactRef(
        path="original.json", sha256=hashlib.sha256(content).hexdigest()
    )
    material = execution._CurrentStateMaterial(tmp_path, "required-original", {})
    assert material.get(reference.path) is None
    if appears:
        (tmp_path / reference.path).write_bytes(content)
        with pytest.raises(
            ValueError, match="original-absence-changed-during-readback"
        ):
            execution._bound_read(tmp_path, reference, material)
        assert material.get(reference.path) is None
    else:
        with pytest.raises((OSError, ValueError)) as live_error:
            execution._bound_read(tmp_path, reference)
        with pytest.raises(type(live_error.value)) as internal_error:
            execution._bound_read(tmp_path, reference, material)
        assert str(internal_error.value) == str(live_error.value)


@pytest.mark.parametrize("kind", ["observe", "exercise"])
@pytest.mark.parametrize("extra", ["empty-directory", "fifo", "nested-owner"])
def test_initial_resource_admission_requires_complete_frozen_tree(
    execution_case, kind, extra
):
    case = (
        execution_case
        if kind == "observe"
        else _case_with_failed_operation(execution_case)
    )
    root, _, plan = case
    first = plan.steps[0]
    resource = Path(first.binding.resources[0].root)
    if extra == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixture is unavailable on this platform")
        os.mkfifo(resource / "undeclared-pipe")
    else:
        unexpected = resource / "undeclared-empty"
        unexpected.mkdir()
        if extra == "nested-owner":
            (unexpected / ".ai-sdlc-owner.json").write_text("{}")
    with pytest.raises(ValueError, match="resource-(initial-state|state-)"):
        _execute(case, first.id)
    assert not execution._attempts_dir(root, plan).exists()
    assert not (resource / ".ai-sdlc-owner.json").exists()
    assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'


def _damage_owned_resource(resource: Path, damage: str) -> None:
    marker = resource / ".ai-sdlc-owner.json"
    if damage == "missing":
        marker.unlink()
    elif damage == "wrong":
        owner = json.loads(marker.read_bytes())
        owner["subject_id"] = "unrelated-subject"
        marker.write_text(json.dumps(owner))
    elif damage == "replacement":
        content = (resource / "result.json").read_bytes()
        shutil.rmtree(resource)
        resource.mkdir()
        (resource / "result.json").write_bytes(content)


@pytest.mark.parametrize("damage", ["missing", "wrong", "replacement"])
@pytest.mark.parametrize("historical_cleanup", [False, True])
def test_active_resource_cycle_never_reclaims_missing_owner(
    execution_case, damage, historical_cleanup
):
    root, _, plan = execution_case
    original = _execute(execution_case)
    assert original.status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    _damage_owned_resource(resource, damage)
    before = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(ValueError, match="resource-(owner|already-owned)"):
        _execute(
            execution_case,
            "current-cleanup" if historical_cleanup else "current-V0",
            cleanup_recovery=historical_cleanup,
        )
    assert resource.is_dir()
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == before
    assert not list(execution._attempts_dir(root, plan).glob("*/resource-cleanup.json"))
    if damage != "wrong":
        assert not (resource / ".ai-sdlc-owner.json").exists()


@pytest.mark.parametrize("damage", [None, "missing", "wrong", "replacement"])
def test_cleanup_requires_continuing_owner_after_real_command(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    for step in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, step).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    real_run = execution.run_quality_command

    def change_owner_after_command(options):
        result = real_run(options)
        if damage:
            _damage_owned_resource(resource, damage)
        return result

    monkeypatch.setattr(execution, "run_quality_command", change_owner_after_command)
    receipt = _execute(execution_case, "current-cleanup")
    cleanup_path = (root / receipt.attempt_ref.path).with_name("resource-cleanup.json")
    if damage is None:
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"
        assert not resource.exists() and cleanup_path.is_file()
    else:
        assert receipt.status != "completed" and receipt.cleanup_status != "complete"
        assert resource.is_dir() and not cleanup_path.exists()
        assert (
            (root / receipt.attempt_ref.path)
            .with_name("postcheck-error.json")
            .is_file()
        )


@pytest.mark.parametrize("nested", [False, True])
def test_first_subject_and_later_subject_accept_declared_initial_tree(
    execution_case, nested
):
    root, contract, original = execution_case
    data = original.model_dump(mode="json")
    if nested:
        initial_path = (
            root / data["steps"][0]["binding"]["resources"][0]["initial_state"]["path"]
        )
        initial = json.loads(initial_path.read_bytes())
        initial["files"].append(
            {
                "path": "parent/child.txt",
                "sha256": hashlib.sha256(b"declared").hexdigest(),
            }
        )
        reference = execution._write_json(
            root, initial_path.with_name("nested-initial.json"), initial
        )
        seen = set()
        for step in data["steps"]:
            resource = step["binding"]["resources"][0]
            resource["initial_state"] = reference.model_dump(mode="json")
            if resource["root"] not in seen:
                seen.add(resource["root"])
                parent = Path(resource["root"]) / "parent"
                parent.mkdir()
                (parent / "child.txt").write_bytes(b"declared")
    case = root, contract, CounterexamplePlan.model_validate(data)
    for subject in ("current", "positive_control"):
        for suffix in ("none", "V0", "V1", "cleanup"):
            receipt = _execute(case, subject + "-" + suffix)
            assert receipt.status == "completed"
        step = next(item for item in case[2].steps if item.subject_id == subject)
        assert not Path(step.binding.resources[0].root).exists()


def _case_with_two_separate_resources(case):
    root, contract, original = case
    data = original.model_dump(mode="json")
    first = data["steps"][0]
    original_resource = first["binding"]["resources"][0]
    parent = Path(original_resource["root"])
    left, right = parent / "left", parent / "right"
    left.mkdir()
    right.mkdir()
    (parent / "result.json").rename(left / "result.json")
    (right / "result.json").write_bytes((left / "result.json").read_bytes())
    for step in data["steps"]:
        if step["subject_id"] != first["subject_id"]:
            continue
        binding = step["binding"]
        resource = binding["resources"][0]
        original_endpoint = resource["observation_endpoint"]
        resource.update(root=str(left), observation_endpoint=str(left / "result.json"))
        other = copy.deepcopy(resource)
        other.update(
            id="second-resource",
            root=str(right),
            observation_endpoint=str(right / "result.json"),
        )
        binding["resources"].append(other)
        binding["argv"] = [
            str(left / "result.json") if arg == original_endpoint else arg
            for arg in binding["argv"]
        ]
        binding["argv"].append(str(right / "result.json"))
    return (root, contract, CounterexamplePlan.model_validate(data)), left, right


@pytest.mark.parametrize("lose_second_owner", [False, True])
def test_cleanup_checks_each_resource_immediately_before_deletion(
    execution_case, monkeypatch, lose_second_owner
):
    case, left, right = _case_with_two_separate_resources(execution_case)
    root, _, _ = case
    for suffix in ("none", "V0", "V1"):
        assert _execute(case, "current-" + suffix).status == "completed"
    real_delete = execution.shutil.rmtree

    def delete_then_change_remaining(path, *args, **kwargs):
        result = real_delete(path, *args, **kwargs)
        # POSIX 公开 rmtree(dir_fd=...) 接收相对名；Windows 仍接收原完整路径。
        if lose_second_owner and Path(path).name == left.name:
            (right / ".ai-sdlc-owner.json").unlink()
        return result

    monkeypatch.setattr(execution.shutil, "rmtree", delete_then_change_remaining)
    receipt = _execute(case, "current-cleanup")
    proof = (root / receipt.attempt_ref.path).with_name("resource-cleanup.json")
    assert not left.exists()
    if lose_second_owner:
        assert right.is_dir() and not proof.exists()
        assert receipt.status != "completed" and receipt.cleanup_status != "complete"
    else:
        assert not right.exists() and proof.is_file()
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"


@pytest.mark.parametrize("replacement", [False, True])
def test_v14_copied_owner_cannot_claim_replacement_directory(
    execution_case, replacement
):
    root, _, plan = execution_case
    for suffix in ("none", "V0", "V1"):
        assert _execute(execution_case, "current-" + suffix).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    original = root.parent / "preserved-original-resource"
    marker = (resource / ".ai-sdlc-owner.json").read_bytes()
    business = (resource / "result.json").read_bytes()
    initial_identity = execution._directory_identity(resource)
    if replacement:
        resource.rename(original)
        resource.mkdir()
        (resource / ".ai-sdlc-owner.json").write_bytes(marker)
        (resource / "result.json").write_bytes(business)
        (resource / "unrelated.txt").write_text("must remain")
        assert execution._directory_identity(resource) != initial_identity
        attempts = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
        with pytest.raises(ValueError, match="resource-owner-entity-changed"):
            _execute(execution_case, "current-cleanup")
        assert (
            set(execution._attempts_dir(root, plan).glob("*/intent.json")) == attempts
        )
        assert (resource / "unrelated.txt").read_text() == "must remain"
        assert (original / "result.json").read_bytes() == business
    else:
        receipt = _execute(execution_case, "current-cleanup")
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"
        assert not resource.exists()


def test_v14_cleanup_rechecks_entity_after_owned_process_exit(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    for suffix in ("none", "V0", "V1"):
        assert _execute(execution_case, "current-" + suffix).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    moved = root.parent / "preserved-after-process"
    real_run = execution.run_quality_command

    def replace_after_exit(options):
        result = real_run(options)
        resource.rename(moved)
        shutil.copytree(moved, resource)
        (resource / "unrelated.txt").write_text("must remain")
        return result

    monkeypatch.setattr(execution, "run_quality_command", replace_after_exit)
    receipt = _execute(execution_case, "current-cleanup")
    assert receipt.status != "completed" and receipt.cleanup_status != "complete"
    assert (resource / "unrelated.txt").read_text() == "must remain"
    assert moved.is_dir()
    assert (
        not (root / receipt.attempt_ref.path)
        .with_name("resource-cleanup.json")
        .exists()
    )


@pytest.mark.parametrize("damage", [None, "missing", "changed"])
def test_v14_resource_entity_original_is_part_of_captured_receipt(
    execution_case, damage
):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    assert receipt.status == "completed"
    refs = execution.attempt_artifact_refs(root, receipt.attempt_ref)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    owner = next(ref for ref in refs if "/resource-owner-" in ref.path)
    assert owner in receipt.raw_evidence_refs
    disk_original = (root / owner.path).read_bytes()
    if damage == "missing":
        del captured[owner.path]
    elif damage == "changed":
        captured[owner.path] += b"\n"
    if damage:
        with pytest.raises(ValueError, match="captured-evidence-(missing|stale)"):
            execution.recover_counterexample_attempt(
                root, plan, receipt.attempt_ref, captured_artifacts=captured
            )
    else:
        assert (
            execution.recover_counterexample_attempt(
                root, plan, receipt.attempt_ref, captured_artifacts=captured
            ).status
            == "completed"
        )
    assert (root / owner.path).read_bytes() == disk_original


@pytest.mark.parametrize("scratch_cwd", [False, True])
def test_v14_completed_cwd_does_not_block_next_subject(execution_case, scratch_cwd):
    root, contract, original = execution_case
    data = original.model_dump(mode="json")
    if scratch_cwd:
        for step in data["steps"]:
            if step["subject_id"] == "current":
                step["binding"]["cwd"] = "scratch"
                step["binding"]["argv"][1] = str(
                    Path(step["binding"]["project_root"]) / step["binding"]["argv"][1]
                )
    case = root, contract, CounterexamplePlan.model_validate(data)
    for suffix in ("none", "V0", "V1", "cleanup"):
        assert _execute(case, "current-" + suffix).status == "completed"
    assert not Path(case[2].steps[0].binding.resources[0].root).exists()
    first = _execute(case, "positive_control-none")
    assert first.status == "completed" and first.cleanup_status == "complete"
    attempts = set(execution._attempts_dir(root, case[2]).glob("*/intent.json"))
    assert _execute(case, "positive_control-none") == first
    assert set(execution._attempts_dir(root, case[2]).glob("*/intent.json")) == attempts


def test_v14_missing_current_cwd_still_blocks_before_intent(execution_case):
    root, contract, original = execution_case
    data = original.model_dump(mode="json")
    data["steps"][0]["binding"]["cwd"] = "missing-current-directory"
    plan = CounterexamplePlan.model_validate(data)
    with pytest.raises((OSError, ValueError)):
        _execute((root, contract, plan))
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))


def test_finished_failed_cwd_allows_independent_controls_and_aggregate(execution_case, monkeypatch):
    root, contract, original = _case_with_failed_operation(_case_with_frozen_resets(execution_case))
    data = original.model_dump(mode="json")
    for step in data["steps"]:
        if step["subject_id"] == "current" and step["phase"] == "final" and step["kind"] != "cleanup":
            step["binding"]["cwd"] = "scratch"
            if step["binding"]["argv"][1] != "-c":
                step["binding"]["argv"][1] = str(Path(step["binding"]["project_root"]) / step["binding"]["argv"][1])
    contract, impl, loop, plan = _run_admission_case((root, contract, CounterexamplePlan.model_validate(data)), monkeypatch)
    current_resource = Path(next(step for step in plan.steps if step.id == "current-failed-operation").binding.resources[0].root)
    assert current_resource.is_dir()
    proposal = execution._attempts_dir(root, plan).parent / "failed-cwd-plan.json"
    proposal.write_bytes(execution._json_bytes(plan))
    reference, assessment = execution.run_counterexample_plan(root, impl, loop, plan.task_id, proposal.relative_to(root).as_posix())
    assert not current_resource.exists()
    bound = execution.resolve_counterexample_evidence(root, impl, reference)
    attempts = {item.step_id: item for item in bound.observations.attempts}
    failed = attempts["current-failed-operation"]
    assert failed.exit_code == 7 and failed.cleanup_status == "complete"
    assert assessment.current_result.status != "PASS"
    assert attempts["current-cleanup"].normally_completed
    assert not {"current-none", "current-V0", "current-V1"} & set(attempts)
    for subject in ("positive_control", "variant"):
        assert attempts[f"{subject}-none"].normally_completed
        assert attempts[f"{subject}-cleanup"].normally_completed
    folder = (root / failed.attempt_ref.path).parent
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["exit_code"] == 7
    originals = {path: path.read_bytes() for path in execution._attempts_dir(root, plan).glob("*/*") if path.is_file()}
    history, debts = execution._historical_resource_cycles(root, plan)
    assert any(row[3] == failed for row in history) and not debts
    assert not execution._subject_resources_active(plan, list(attempts.values()), "current")
    # 恢复读取不重新检查已结束或已放弃步骤的现场目录，也不新建任何尝试。
    assert execution.run_counterexample_plan(root, impl, loop, plan.task_id, proposal.relative_to(root).as_posix()) == (reference, assessment)
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert set(originals) == {path for path in execution._attempts_dir(root, plan).glob("*/*") if path.is_file()}


def test_reset_cannot_drop_owner_and_start_a_new_business_step(
    execution_case, monkeypatch
):
    case = _same_resource_reset_case(execution_case, "restored")
    root, _, plan = case
    assert _execute(case, "current-explore").status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    real_run = execution.run_quality_command

    def reset_then_drop_owner(options):
        result = real_run(options)
        (resource / ".ai-sdlc-owner.json").unlink()
        return result

    monkeypatch.setattr(execution, "run_quality_command", reset_then_drop_owner)
    receipt = _execute(case, "current-reset")
    assert receipt.status != "completed"
    assert resource.is_dir() and not (resource / ".ai-sdlc-owner.json").exists()
    before = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(ValueError, match="prior-operation-failed-business-stopped"):
        _execute(case, "current-none")
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == before


@pytest.mark.parametrize("window", ["after-intent", "during-owner-read"])
def test_active_owner_loss_after_intent_is_not_reclaimed_or_replayed(
    execution_case, monkeypatch, window
):
    root, _, plan = execution_case
    assert _execute(execution_case).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    real_write = execution._write_json
    real_read = execution.read_stable_bytes
    intent_written = False

    def write_intent_then_drop_owner(project, path, payload):
        nonlocal intent_written
        reference = real_write(project, path, payload)
        if path.name == "intent.json":
            intent_written = True
            if window == "after-intent":
                (resource / ".ai-sdlc-owner.json").unlink()
        return reference

    def read_owner_then_remove(project, path, *args, **kwargs):
        raw = real_read(project, path, *args, **kwargs)
        if (
            window == "during-owner-read"
            and intent_written
            and path == resource / ".ai-sdlc-owner.json"
        ):
            path.unlink()
        return raw

    monkeypatch.setattr(execution, "_write_json", write_intent_then_drop_owner)
    monkeypatch.setattr(execution, "read_stable_bytes", read_owner_then_remove)
    with pytest.raises(ValueError, match="resource-owner"):
        _execute(execution_case, "current-V0")
    attempts = list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    assert len(attempts) == 2
    assert not (resource / ".ai-sdlc-owner.json").exists()
    with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
        _execute(execution_case, "current-V0")
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 2


def test_initial_manifest_cannot_treat_a_declared_file_as_a_parent_directory(
    execution_case,
):
    root, contract, original = execution_case
    data = original.model_dump(mode="json")
    initial_path = (
        root / data["steps"][0]["binding"]["resources"][0]["initial_state"]["path"]
    )
    initial = json.loads(initial_path.read_bytes())
    for path in ("parent", "parent/child.txt"):
        initial["files"].append(
            {"path": path, "sha256": hashlib.sha256(b"declared").hexdigest()}
        )
    reference = execution._write_json(
        root, initial_path.with_name("conflicting-initial.json"), initial
    )
    seen = set()
    for step in data["steps"]:
        resource = step["binding"]["resources"][0]
        resource["initial_state"] = reference.model_dump(mode="json")
        if resource["root"] not in seen:
            seen.add(resource["root"])
            parent = Path(resource["root"]) / "parent"
            parent.mkdir()
            (parent / "child.txt").write_bytes(b"declared")
    plan = CounterexamplePlan.model_validate(data)
    with pytest.raises(ValueError, match="resource-initial"):
        _execute((root, contract, plan))
    assert not execution._attempts_dir(root, plan).exists()


def _started_history_input(execution_case, monkeypatch, *, loop_id=None):
    from ai_sdlc.core import implementation_store

    root, contract, plan = execution_case
    impl = SimpleNamespace(
        loop_id=loop_id or plan.loop_id,
        work_item_id=plan.work_item_id,
        verification_capability=execution.VERIFICATION_CAPABILITY,
        verification_contract_digest=plan.contract_digest,
        task_scopes={plan.task_id: list(plan.allowed_modified_paths)},
        declared_scope=list(plan.allowed_modified_paths),
    )
    monkeypatch.setattr(implementation_store, "read_input", lambda *a: impl)
    monkeypatch.setattr(
        implementation_store,
        "validate_implementation_verification_contract",
        lambda *a: (contract, {}),
    )
    monkeypatch.setattr(
        execution,
        "_implementation_obligation_owners",
        lambda *a: {subject.obligation_id: plan.task_id for subject in plan.subjects},
    )
    directory = execution._attempts_dir(root, plan).parent / "plans"
    execution._write_json(root, directory / f"{counterexample_digest(plan)}.json", plan)
    return impl


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "empty",
        "missing-completion",
        "malformed-intent",
        "foreign-loop",
        "foreign-task",
        "binding",
        "process",
        "capability",
    ],
)
def test_counterexample_started_history_requires_original_binding(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    impl = _started_history_input(execution_case, monkeypatch)
    assert not execution.counterexample_execution_started(root, plan.loop_id)
    if damage == "empty":
        (execution._attempts_dir(root, plan) / "empty").mkdir(parents=True)
        assert not execution.counterexample_execution_started(root, plan.loop_id)
        return
    receipt = _execute(execution_case)
    folder = (root / receipt.attempt_ref.path).parent
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    loop_id = plan.loop_id
    if damage == "missing-completion":
        (folder / "completion.json").unlink()
    elif damage == "malformed-intent":
        (folder / "intent.json").write_text("[]")
    elif damage == "foreign-loop":
        loop_id = "unrelated-loop"
        impl.loop_id = loop_id
        target = root / ".ai-sdlc/loops/implementation" / loop_id / "counterexamples"
        shutil.copytree(execution._attempts_dir(root, plan).parent, target)
    elif damage == "foreign-task":
        monkeypatch.setattr(
            execution,
            "_implementation_obligation_owners",
            lambda *a: {
                subject.obligation_id: "another-task" for subject in plan.subjects
            },
        )
    elif damage == "binding":
        path = folder / "intent.json"
        data = json.loads(path.read_bytes())
        data["binding"]["argv"].append("different-input")
        path.write_text(json.dumps(data))
    elif damage == "process":
        path = folder / "process.json"
        data = json.loads(path.read_bytes())
        data["started_at_ms"] += 1
        path.write_text(json.dumps(data))
    elif damage == "capability":
        impl.verification_capability = None
        assert not execution.counterexample_execution_started(root, loop_id)
        return
    if damage == "none":
        # 首条完整原证明足够说明已花费成本；不得再执行全历史恢复来回答这个布尔问题。
        monkeypatch.setattr(
            execution,
            "_historical_resource_cycles",
            lambda *a, **k: pytest.fail("unnecessary full history scan"),
        )
        assert execution.counterexample_execution_started(root, loop_id)
    else:
        with pytest.raises(ValueError):
            execution.counterexample_execution_started(root, loop_id)


def test_counterexample_progress_write_failure_keeps_original_attempt(
    execution_case, monkeypatch
):
    from ai_sdlc.core.implementation_models import (
        ImplementationProgress,
        ImplementationTaskProgress,
    )
    from ai_sdlc.core.implementation_store import (
        implementation_artifacts,
        read_progress,
    )

    root, _, plan = execution_case
    impl = _started_history_input(execution_case, monkeypatch)
    progress_path = implementation_artifacts(root, plan.loop_id).progress_path
    LoopArtifactStore(root).write_json_artifact(
        progress_path,
        ImplementationProgress(
            loop_id=plan.loop_id,
            work_item_id=plan.work_item_id,
            tasks=[ImplementationTaskProgress(task_id=plan.task_id)],
        ),
    )
    write = LoopArtifactStore.write_json_artifact
    failures = []

    def once_failed_progress(store, path, payload, *args, **kwargs):
        if Path(path) == progress_path and not failures:
            failures.append("progress-write")
            raise OSError("controlled progress write failure")
        return write(store, path, payload, *args, **kwargs)

    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", once_failed_progress)
    receipt = _execute(
        execution_case,
        on_started=lambda: execution._record_counterexample_execution_started(
            root, impl, plan.task_id
        ),
    )
    assert failures == ["progress-write"]
    assert (
        receipt.status == "infrastructure_error"
        and receipt.cleanup_status == "complete"
    )
    folder = (root / receipt.attempt_ref.path).parent
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert (
        raw["launch_status"] == "started"
        and "controlled progress write failure" in raw["launch_error"]
    )
    assert (
        folder / "stdout"
    ).read_bytes() == b""  # 业务 nonce 未释放，observe.py 没有执行。
    assert read_progress(progress_path).tasks[0].status == "pending"
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    assert execution.counterexample_execution_started(root, plan.loop_id)
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    cleanup = _execute(
        execution_case,
        "current-cleanup",
        on_started=lambda: execution._record_counterexample_execution_started(
            root, impl, plan.task_id
        ),
    )
    assert cleanup.status == "completed" and cleanup.cleanup_status == "complete"
    assert read_progress(progress_path).tasks[0].status == "in_progress"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_record_done_second_history_read_failure_is_blocked(
    execution_case, monkeypatch
):
    from ai_sdlc.core import implementation_loop as implementation
    from ai_sdlc.core import loop_decision_service as decision
    from ai_sdlc.core.implementation_models import (
        ImplementationProgress,
        ImplementationRecordOptions,
        ImplementationTaskItem,
        ImplementationTaskProgress,
        ImplementationTasks,
    )
    from ai_sdlc.core.implementation_store import implementation_artifacts
    from ai_sdlc.core.loop_models import LoopRun

    root, _, plan = execution_case
    impl = _started_history_input(execution_case, monkeypatch)
    artifacts = implementation_artifacts(root, plan.loop_id)
    progress = ImplementationProgress(
        loop_id=plan.loop_id,
        work_item_id=plan.work_item_id,
        tasks=[ImplementationTaskProgress(task_id=plan.task_id)],
    )
    tasks = ImplementationTasks(
        loop_id=plan.loop_id,
        work_item_id=plan.work_item_id,
        items=[ImplementationTaskItem(task_id=plan.task_id)],
    )
    run = LoopRun(
        loop_id=plan.loop_id,
        loop_type="implementation",
        status="running",
        work_item_id=plan.work_item_id,
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    store = LoopArtifactStore(root)
    store.write_json_artifact(artifacts.progress_path, progress)
    store.write_json_artifact(artifacts.loop_run_path, run)
    before = {
        p: p.read_bytes() for p in (artifacts.progress_path, artifacts.loop_run_path)
    }
    monkeypatch.setattr(
        implementation, "_read_current_state", lambda *a, **k: (impl, tasks, progress)
    )
    checked = []

    def checked_then_changed(*args, purpose):
        assert purpose == "verification"
        assert not decision.implementation_execution_started(root, plan.loop_id)
        checked.append(purpose)
        # 隔离前置治理后原件改变的窗口；第二次读取仍走真实原 intent 解析。
        path = execution._attempts_dir(root, plan) / "changed" / "intent.json"
        path.parent.mkdir(parents=True)
        path.write_text("[]")

    monkeypatch.setattr(
        decision, "validate_implementation_context", checked_then_changed
    )
    result = implementation.record_implementation_progress(
        ImplementationRecordOptions(
            root=root,
            loop_id=plan.loop_id,
            task_id=plan.task_id,
            status="done",
            evidence=("business/save.py",),
        )
    )
    assert checked == ["verification"]
    assert result.status == "blocked"
    assert "counterexample-attempt-intent-shape-invalid" in result.blocker
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not artifacts.report_json_path.exists()


@pytest.mark.parametrize(
    "git_location", ["ordinary", "separate_subject", "separate_evidence"]
)
def test_v13_cleanup_preserves_actual_git_metadata(execution_case, git_location):
    root, _, plan = execution_case
    project = Path(plan.steps[0].binding.project_root)
    scratch = Path(plan.steps[0].binding.resources[0].root)
    for identifier in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, identifier).status == "completed"
    owner = (scratch / ".ai-sdlc-owner.json").read_bytes()
    repo = root if git_location == "separate_evidence" else project
    before = source_digest_sha256(build_source_digest(repo))
    if git_location != "ordinary":
        _git(
            repo,
            "init",
            "--quiet",
            "--separate-git-dir",
            str(scratch / "actual-git-metadata"),
        )
    gitdir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
    index = (gitdir / "index").read_bytes()
    assert source_digest_sha256(build_source_digest(repo)) == before
    # 归属后的 Git 布局变化不会改变源摘要，删除前仍必须保护当前真实元数据。
    if git_location == "ordinary":
        receipt = _execute(execution_case, "current-cleanup")
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"
        assert not scratch.exists()
    else:
        with pytest.raises(ValueError, match="resource-root-overlaps-protected-path"):
            _execute(execution_case, "current-cleanup")
        assert (scratch / ".ai-sdlc-owner.json").read_bytes() == owner
        assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 3
    assert (gitdir / "index").read_bytes() == index


@pytest.mark.parametrize("include_r2", [False, True])
def test_v13_new_native_plan_requires_reserved_r2_before_freeze(
    execution_case, monkeypatch, include_r2
):
    root, contract, base = _case_with_frozen_resets(execution_case)
    contract, impl, loop, full = _run_admission_case(
        (root, contract, base), monkeypatch
    )
    plan = (
        full
        if include_r2
        else full.model_copy(
            update={"steps": tuple(step for step in full.steps if step.phase != "r2")}
        )
    )
    execution.validate_plan_contract(contract, plan)
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-v13-r2.json"
    proposed.write_bytes(execution._json_bytes(plan))
    if include_r2:
        reference, assessment = execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
        assert assessment.current_result.status == "PASS"
        bound = execution.resolve_counterexample_evidence(root, impl, reference)
        assert len(bound.observations.attempts) == 12
        from ai_sdlc.core.counterexample_models import required_execution_steps

        assert (
            len(required_execution_steps(plan, bound.observations, require_r2=True))
            == 27
        )
    else:
        with pytest.raises(ValueError, match="required-r2-execution-table-missing"):
            execution.run_counterexample_plan(
                root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
            )
        assert not list(folder.glob("plans/*.json"))
        assert not list(folder.glob("attempts/*/intent.json"))
        assert all(
            not (Path(step.binding.resources[0].root) / ".ai-sdlc-owner.json").exists()
            for step in plan.steps
        )


@pytest.mark.parametrize("repo_role", ["subject", "evidence"])
@pytest.mark.parametrize("placement", ["inside", "outside"])
def test_v13_initial_resource_admission_checks_actual_git_directory(
    execution_case, repo_role, placement
):
    root, _, plan = execution_case
    step = plan.steps[0]
    project = Path(step.binding.project_root)
    resource = step.binding.resources[0]
    scratch = Path(resource.root)
    repo = project if repo_role == "subject" else root
    target = (scratch if placement == "inside" else root.parent) / "separate-metadata"
    _git(repo, "init", "--quiet", "--separate-git-dir", str(target))
    original_index = (target / "index").read_bytes()
    if placement == "inside":
        with pytest.raises(ValueError, match="resource-root-overlaps-protected-path"):
            execution._require_scratch_root(
                root, project, plan, resource, plan.subjects[0].snapshot
            )
    else:
        execution._require_scratch_root(
            root, project, plan, resource, plan.subjects[0].snapshot
        )
    assert (target / "index").read_bytes() == original_index
    assert not (scratch / ".ai-sdlc-owner.json").exists()
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))


@pytest.mark.parametrize(
    "damage", ["command-error", "empty", "extra-line", "missing-directory"]
)
def test_v13_unknown_git_metadata_retains_owned_resource(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    assert _execute(execution_case).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    owner = (resource / ".ai-sdlc-owner.json").read_bytes()
    git = execution._git

    def unavailable(project, *args):
        if "--absolute-git-dir" in args:
            if damage == "command-error":
                raise subprocess.CalledProcessError(1, ["git", *args])
            return {
                "empty": "",
                "extra-line": str(project / ".git") + "\n.git\nextra\n",
                "missing-directory": str(project / "absent-git") + "\n.git\n",
            }[damage]
        return git(project, *args)

    monkeypatch.setattr(execution, "_git", unavailable)
    with pytest.raises(ValueError, match="resource-git-metadata-unavailable"):
        _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert (resource / ".ai-sdlc-owner.json").read_bytes() == owner
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1


def test_v13_native_reserves_conditional_time_before_plan_freeze(
    execution_case, monkeypatch
):
    root, contract, base = _case_with_frozen_resets(execution_case)
    contract, impl, loop, plan = _run_admission_case(
        (root, contract, base), monkeypatch
    )
    final_seconds = sum(
        step.binding.timeout_seconds + step.reservation_seconds + 5
        for step in plan.steps
        if step.phase == "final"
    )
    now = time.time_ns() // 1_000_000
    monkeypatch.setattr(
        "ai_sdlc.core.loop_decision_service.validate_implementation_context",
        lambda *a, **k: SimpleNamespace(
            capability="stage-simulation-v1",
            started_at_ms=now,
            plan=SimpleNamespace(
                time_plan=SimpleNamespace(
                    window_seconds=final_seconds + plan.required_reserve_seconds + 10
                )
            ),
        ),
    )
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-v13-time.json"
    proposed.write_bytes(execution._json_bytes(plan))
    with pytest.raises(ValueError, match="time-budget-reserve-unavailable"):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
    assert not list(folder.glob("plans/*.json"))
    assert not list(folder.glob("attempts/*/intent.json"))
    assert all(
        not (Path(step.binding.resources[0].root) / ".ai-sdlc-owner.json").exists()
        for step in plan.steps
    )


@pytest.mark.parametrize("metadata_position", ["inside", "outside"])
def test_v13_linked_worktree_common_directory_is_protected(
    execution_case, metadata_position
):
    root, _, plan = execution_case
    original = Path(plan.steps[0].binding.project_root)
    linked = root.parent / "linked-subject"
    _git(original, "worktree", "add", "--detach", str(linked), "HEAD")
    scratch = linked / "scratch"
    scratch.mkdir()
    common = (
        scratch if metadata_position == "inside" else root.parent
    ) / "linked-common"
    # 只迁移玩具仓库，Git repair 更新所有 worktree 的真实指针。
    _git(original, "init", "--quiet", "--separate-git-dir", str(common))
    _git(original, "worktree", "repair", str(linked))
    gitdir = Path(
        _git(linked, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
    )
    actual_common = (
        linked / _git(linked, "rev-parse", "--git-common-dir").stdout.decode().strip()
    ).resolve()
    assert actual_common == common.resolve() and gitdir != actual_common
    resource = (
        plan.steps[0]
        .binding.resources[0]
        .model_copy(
            update={
                "root": str(scratch),
                "observation_endpoint": str(scratch / "result.json"),
            }
        )
    )
    original_head = (gitdir / "HEAD").read_bytes()
    if metadata_position == "inside":
        with pytest.raises(ValueError, match="resource-root-overlaps-protected-path"):
            execution._require_scratch_root(
                root, linked, plan, resource, plan.subjects[0].snapshot
            )
    else:
        execution._require_scratch_root(
            root, linked, plan, resource, plan.subjects[0].snapshot
        )
    assert (gitdir / "HEAD").read_bytes() == original_head
    assert (common / "objects").is_dir()


def test_v13_old_complete_final_only_record_remains_read_only(
    execution_case, monkeypatch
):
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample

    root, _, _ = execution_case
    contract, impl, loop, plan = _run_admission_case(execution_case, monkeypatch)
    assert not any(step.phase == "r2" for step in plan.steps)
    folder = execution._attempts_dir(root, plan).parent
    plan_ref = execution._write_json(
        root, folder / f"plans/{counterexample_digest(plan)}.json", plan
    )
    receipts = [_execute((root, contract, plan), step.id) for step in plan.steps]
    assert len(receipts) == 12 and all(
        receipt.status == "completed" for receipt in receipts
    )
    # 由真实原始尝试形成旧格式完整记录，不以新准入补造旧 R2 表或完成证明。
    bundle = execution._bundle_from_attempts(root, contract, plan, receipts)
    assessment = evaluate_counterexample(contract, plan, bundle, require_r2=False)
    observations = execution._write_json(
        root, folder / "results/legacy-observations.json", bundle
    )
    assessed = execution._write_json(
        root, folder / "results/legacy-assessment.json", assessment
    )
    record = CounterexampleEvidenceRecord(
        loop_id=plan.loop_id,
        task_id=plan.task_id,
        contract_ref={
            "path": impl.verification_contract_ref,
            "sha256": impl.verification_contract_digest,
        },
        plan_ref=plan_ref,
        observations_ref=observations,
        assessment_ref=assessed,
        source_digest_before=plan.candidate_digest,
        source_digest_after=plan.candidate_digest,
        recorded_at_ms=time.time_ns() // 1_000_000,
    )
    reference = execution._write_json(
        root, folder / "results/record-legacy-final.json", record
    )
    assert (
        execution.resolve_counterexample_evidence(root, impl, reference).assessment
        == assessment
    )
    for project in {step.binding.project_root for step in plan.steps}:
        shutil.rmtree(project)
    before = {path: path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    assert execution.run_counterexample_plan(
        root, impl, loop, plan.task_id, plan_ref.path
    ) == (reference, assessment)
    assert {
        path: path.read_bytes() for path in folder.rglob("*") if path.is_file()
    } == before


def test_v13_partial_r2_is_rejected_before_native_freeze(execution_case, monkeypatch):
    root, contract, base = _case_with_frozen_resets(execution_case)
    contract, impl, loop, full = _run_admission_case(
        (root, contract, base), monkeypatch
    )
    plan = full.model_copy(
        update={
            "steps": tuple(
                step
                for step in full.steps
                if not (step.phase == "r2" and step.subject_id == "positive_control")
            )
        }
    )
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-v13-partial.json"
    proposed.write_bytes(execution._json_bytes(plan))
    with pytest.raises(ValueError, match="subject-execution-table-incomplete"):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
    assert not list(folder.glob("plans/*.json"))
    assert not list(folder.glob("attempts/*/intent.json"))


@pytest.mark.parametrize("cwd_scope", ["resource-root", "frozen-parent"])
def test_reset_cwd_recreated_before_real_r2_command(execution_case, cwd_scope):
    root, contract, original = _case_with_frozen_resets(execution_case)
    data = original.model_dump(mode="json")
    if cwd_scope == "frozen-parent":
        initial_ref = original.steps[0].binding.resources[0].initial_state
        initial = json.loads((root / initial_ref.path).read_bytes())
        initial["files"].append({**initial["files"][0], "path": "worker/input.bin"})
        updated_ref = execution._write_json(
            root,
            (root / initial_ref.path).with_name("initial-cwd-with-bytes.json"),
            initial,
        )
        for resource_root in {
            resource.root
            for step in original.steps
            for resource in step.binding.resources
        }:
            directory = Path(resource_root) / "worker"
            directory.mkdir()
            (directory / "input.bin").write_bytes(b'{"value":"saved"}')
        for planned in data["steps"]:
            for resource in planned["binding"]["resources"]:
                resource["initial_state"] = updated_ref.model_dump(mode="json")
    reset = next(step for step in data["steps"] if step["id"] == "current-reset")
    reset["binding"]["cwd"] = (
        "scratch" if cwd_scope == "resource-root" else "scratch/worker"
    )
    reset["binding"]["argv"][1] = str(
        Path(reset["binding"]["project_root"]) / reset["binding"]["argv"][1]
    )
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    first = _execute(case, "current-none")
    assert first.status == "completed"
    preserved = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, first.attempt_ref)
    }
    for suffix in ("V0", "V1", "cleanup"):
        assert _execute(case, "current-" + suffix).status == "completed"
    resource_root = Path(plan.steps[0].binding.resources[0].root)
    assert not resource_root.exists()
    restored = _execute(case, "current-reset")
    assert restored.status == "completed" and restored.cleanup_status == "complete"
    assert (Path(reset["binding"]["project_root"]) / reset["binding"]["cwd"]).is_dir()
    old_owner = next(
        ref for ref in first.raw_evidence_refs if "/resource-owner-" in ref.path
    )
    new_owner = next(
        ref for ref in restored.raw_evidence_refs if "/resource-owner-" in ref.path
    )
    assert old_owner != new_owner
    assert json.loads((root / new_owner.path).read_bytes())["directory_identity"] == (
        execution._directory_identity(resource_root)
    )
    for suffix in ("none", "V0", "V1", "cleanup"):
        receipt = _execute(case, "r2-current-" + suffix)
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    assert not resource_root.exists()
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 9
    assert all((root / path).read_bytes() == raw for path, raw in preserved.items())


@pytest.mark.parametrize("kind", ["reset", "cleanup"])
def test_unfrozen_resource_cwd_rejected_before_plan_publication(
    execution_case, monkeypatch, kind
):
    contract, impl, loop, original = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    root = execution_case[0]
    data = original.model_dump(mode="json")
    step = next(
        item for item in data["steps"] if item["kind"] == kind and item["phase"] == "r2"
    )
    step["binding"]["cwd"] = "scratch/unfrozen-child"
    plan = CounterexamplePlan.model_validate(data)
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-unfrozen-cwd.json"
    proposed.write_bytes(execution._json_bytes(plan))
    resources = {
        resource.root for item in plan.steps for resource in item.binding.resources
    }
    with pytest.raises((OSError, ValueError), match="unfrozen-child"):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
    assert not list(folder.glob("plans/*.json"))
    assert not list(root.rglob("intent.json"))
    assert all(not (Path(path) / ".ai-sdlc-owner.json").exists() for path in resources)
    assert all(not (Path(path) / "unfrozen-child").exists() for path in resources)


@pytest.mark.parametrize("mutation", ["unchanged", "source", "ignored-input"])
def test_v16_prelaunch_subject_binding_keeps_real_failure_cleanup(
    execution_case, monkeypatch, mutation
):
    root, contract, original = execution_case
    data = original.model_dump(mode="json")
    folder = ".ai-sdlc/loops/implementation/sample-implementation/counterexamples"
    for subject in data["subjects"]:
        project = Path(
            next(step for step in data["steps"] if step["subject_id"] == subject["id"])[
                "binding"
            ]["project_root"]
        )
        with (project / ".git/info/exclude").open("a") as stream:
            stream.write("\ninput.local\n")
        (project / "input.local").write_bytes(b"frozen input")
        manifest = execution.capture_counterexample_snapshot(
            project,
            evidence_root=root,
            artifact_dir=f"{folder}/v16-snapshots/{subject['id']}",
            acceptance_sources={"V0": "tests/v0.py", "V1": "tests/v1.py"},
            ignored_inputs=["input.local"],
        )
        reference = execution._write_json(
            root, root / folder / f"v16-{subject['id']}.json", manifest
        )
        subject["snapshot"] = reference.model_dump(mode="json")
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    project = Path(plan.steps[0].binding.project_root)
    changed_path = project / (
        "input.local" if mutation == "ignored-input" else "src/save.py"
    )
    original_bytes = changed_path.read_bytes()
    writer = execution._write_json
    altered = []
    maps = []
    original_map = execution._file_map

    def publish_then_change(evidence_root, path, payload):
        result = writer(evidence_root, path, payload)
        if path.name == "resource-ownership.json" and not altered:
            altered.append(path)
            if mutation != "unchanged":
                changed_path.write_bytes(
                    original_bytes + b"\nchanged during publication"
                )
        return result

    def counted_map(path, ignored_inputs):
        if path == project:
            maps.append(path)
        return original_map(path, ignored_inputs)

    with monkeypatch.context() as context:
        context.setattr(execution, "_write_json", publish_then_change)
        context.setattr(execution, "_file_map", counted_map)
        receipt = _execute(case)
    assert len(altered) == 1
    # 源摘要拒绝时不必再读文件图；正常或仅 ignored 输入变化只读一次。
    assert len(maps) == (0 if mutation == "source" else 1)
    assert receipt.cleanup_status == "complete"
    attempt_dir = (root / receipt.attempt_ref.path).parent
    process = json.loads((attempt_dir / "process.json").read_bytes())
    raw = json.loads((attempt_dir / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "started"
    assert process["launcher"] == "nonce-gated-child"
    if mutation == "unchanged":
        assert receipt.status == "completed"
        assert (attempt_dir / "stdout").read_bytes()
    else:
        assert receipt.status == "infrastructure_error"
        assert (attempt_dir / "stdout").read_bytes() == b""
        error = json.loads((attempt_dir / "postcheck-error.json").read_bytes())["error"]
        assert "counterexample-prelaunch-subject-" in error
        with pytest.raises(ValueError, match="historical-cleanup-subject-"):
            _execute(case, "current-cleanup", cleanup_recovery=True)
        assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    originals = {
        path: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()
    }
    changed_path.write_bytes(original_bytes)
    cleaned = _execute(case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert all(path.read_bytes() == content for path, content in originals.items())
    _, debts = execution._historical_resource_cycles(root, plan)
    assert not debts


@pytest.mark.parametrize(
    "damage",
    [
        "list",
        "missing-fields",
        "identity-list",
        "files-null",
        "file-null",
        "acceptance-null",
    ],
)
@pytest.mark.parametrize("captured", [False, True])
def test_v16_snapshot_bad_shape_is_controlled(execution_case, damage, captured):
    root, _, plan = execution_case
    subject = plan.subjects[0]
    manifest = json.loads((root / subject.snapshot.path).read_bytes())
    if damage == "list":
        manifest = []
    elif damage == "missing-fields":
        manifest = {"schema_version": 1}
    elif damage == "identity-list":
        manifest["source_identity"] = []
    elif damage == "files-null":
        manifest["files"] = None
    elif damage == "file-null":
        manifest["files"] = [None]
    else:
        manifest["acceptance_sources"]["V0"] = None
    reference = execution._write_json(
        root, root / ".ai-sdlc/state/v16-bad-snapshot.json", manifest
    )
    changed = plan.model_copy(
        update={
            "subjects": (
                subject.model_copy(update={"snapshot": reference}),
                *plan.subjects[1:],
            )
        }
    )
    supplied = (
        {reference.path: (root / reference.path).read_bytes()} if captured else None
    )
    with pytest.raises(ValueError, match="counterexample-snapshot-"):
        execution.validate_counterexample_snapshots(
            root, changed, captured_artifacts=supplied, require_live=False
        )


@pytest.mark.parametrize(
    "damage", ["list", "missing-fields", "files-null", "entry-null", "path-null"]
)
@pytest.mark.parametrize("captured", [False, True])
def test_v16_initial_bad_shape_is_controlled(tmp_path, damage, captured):
    root = tmp_path.resolve()
    payload = {"schema_version": 1, "files": []}
    if damage == "list":
        payload = []
    elif damage == "missing-fields":
        payload = {"schema_version": 1}
    elif damage == "files-null":
        payload["files"] = None
    elif damage == "entry-null":
        payload["files"] = [None]
    else:
        payload["files"] = [{"path": None, "sha256": "a" * 64}]
    reference = execution._write_json(
        root, root / ".ai-sdlc/state/v16-bad-initial.json", payload
    )
    supplied = (
        {reference.path: (root / reference.path).read_bytes()} if captured else None
    )
    with pytest.raises(ValueError, match="counterexample-resource-initial-"):
        execution._initial_resource_state(root, reference, supplied)


def test_v16_hash_only_initial_cold_read_stays_supported(tmp_path):
    root = tmp_path.resolve()
    entry = {"path": "result.json", "sha256": hashlib.sha256(b"original").hexdigest()}
    reference = execution._write_json(
        root,
        root / ".ai-sdlc/state/v16-hash-only.json",
        {"schema_version": 1, "files": [entry]},
    )
    raw = (root / reference.path).read_bytes()
    expected = execution._initial_resource_state(root, reference)
    (root / reference.path).unlink()
    assert (
        execution._initial_resource_state(root, reference, {reference.path: raw})
        == expected
    )
    assert expected == ([entry], (reference,))
    assert not (root / reference.path).exists()


@pytest.mark.parametrize(
    "initial_mode",
    ["hash-only", "hash-only-no-local-cleanup", "missing-content", "valid-content"],
)
def test_v17_new_plan_reset_requires_original_bytes(
    execution_case, monkeypatch, initial_mode
):
    root, contract, base = _case_with_frozen_resets(execution_case)
    data = base.model_dump(mode="json")
    initial_ref = base.steps[0].binding.resources[0].initial_state
    initial = json.loads((root / initial_ref.path).read_bytes())
    if initial_mode in {"hash-only", "hash-only-no-local-cleanup"}:
        for entry in initial["files"]:
            entry.pop("content_ref")
        reference = execution._write_json(
            root, root / ".ai-sdlc/state/v17-hash-only.json", initial
        )
        for step in data["steps"]:
            step["binding"]["resources"][0]["initial_state"] = reference.model_dump(
                mode="json"
            )
    elif initial_mode == "missing-content":
        (root / initial["files"][0]["content_ref"]["path"]).unlink()
    if initial_mode == "hash-only-no-local-cleanup":
        for step in data["steps"]:
            if step["kind"] == "reset":
                step["depends_on"] = []
    base = CounterexamplePlan.model_validate(data)
    contract, impl, loop, plan = _run_admission_case(
        (root, contract, base), monkeypatch
    )
    execution.validate_plan_contract(contract, plan)
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-v17-reset.json"
    proposed.write_bytes(execution._json_bytes(plan))
    if initial_mode != "valid-content":
        reason = (
            "reset-initial-bytes-required-before-freeze"
            if initial_mode in {"hash-only", "hash-only-no-local-cleanup"}
            else r"trusted file is unavailable or uses a symlink: .*initial-original\.bin"
        )
        with pytest.raises(ValueError, match=reason):
            execution.run_counterexample_plan(
                root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
            )
        assert not list(folder.glob("plans/*.json"))
        assert not list(folder.glob("attempts/*/intent.json"))
        assert all(
            not (Path(step.binding.resources[0].root) / ".ai-sdlc-owner.json").exists()
            for step in plan.steps
        )
        return
    reference, assessment = execution.run_counterexample_plan(
        root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
    )
    assert assessment.current_result.status == "PASS"
    bound = execution.resolve_counterexample_evidence(root, impl, reference)
    assert len(bound.observations.attempts) == 12
    resource = plan.steps[0].binding.resources[0]
    assert not Path(resource.root).exists()
    frozen_path = folder / "plans" / f"{counterexample_digest(plan)}.json"
    frozen_bytes = frozen_path.read_bytes()
    r1_bytes = (root / reference.path).read_bytes()
    # 同一原表真正跨过 cleanup→reset，再完成本对象的 R2 资源收尾。
    case = root, contract, plan
    reset = _execute(case, "current-reset")
    assert reset.status == "completed" and reset.cleanup_status == "complete"
    assert (Path(resource.root) / "result.json").read_bytes() == b'{"value":"saved"}'
    for step_id in (
        "r2-current-none",
        "r2-current-V0",
        "r2-current-V1",
        "r2-current-cleanup",
    ):
        receipt = _execute(case, step_id)
        assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    assert not Path(resource.root).exists()
    assert frozen_path.read_bytes() == frozen_bytes
    assert (root / reference.path).read_bytes() == r1_bytes
    assert len(list(folder.glob("attempts/*/intent.json"))) == 17


def test_v17_empty_initial_reset_is_admissible_before_publication(
    execution_case, monkeypatch
):
    root, contract, base = _case_with_frozen_resets(execution_case)
    empty = execution._write_json(
        root,
        root / ".ai-sdlc/state/v17-empty-initial.json",
        {"schema_version": 1, "files": []},
    )
    data = base.model_dump(mode="json")
    for step in data["steps"]:
        step["binding"]["resources"][0]["initial_state"] = empty.model_dump(mode="json")
    # 空初态正控必须同时提供空的首次目录；不能依赖旧准入遗漏跳过真实内容。
    for resource_root in {step["binding"]["resources"][0]["root"] for step in data["steps"]}:
        (Path(resource_root) / "result.json").unlink()
    contract, impl, loop, plan = _run_admission_case(
        (root, contract, CounterexamplePlan.model_validate(data)), monkeypatch
    )
    folder = execution._attempts_dir(root, plan).parent
    proposed = folder / "proposed-v17-empty.json"
    proposed.write_bytes(execution._json_bytes(plan))
    writer = execution._write_json
    reached = []

    def stop_at_publication(evidence_root, path, value):
        if path == folder / "plans" / f"{counterexample_digest(plan)}.json":
            reached.append(path)
            raise RuntimeError("empty-initial-admission-reached-publication")
        return writer(evidence_root, path, value)

    # 此正控只确认空初态准入，不把截停点伪装成业务执行成功。
    monkeypatch.setattr(execution, "_write_json", stop_at_publication)
    with pytest.raises(
        RuntimeError, match="empty-initial-admission-reached-publication"
    ):
        execution.run_counterexample_plan(
            root, impl, loop, plan.task_id, proposed.relative_to(root).as_posix()
        )
    assert len(reached) == 1
    assert not list(folder.glob("plans/*.json"))
    assert not list(folder.glob("attempts/*/intent.json"))


@pytest.mark.parametrize(
    "case",
    [
        "both-T11",
        "both-T21",
        "single-task",
        "legacy-single-task",
        "unproven-sibling",
        "missing-current-task",
        "unauthorized",
        "protected",
        "no-own-change",
        "changed-replay",
        "no-original-fail",
        "missing-captured-original",
    ],
)
def test_weekend_repair_proof_separates_global_authority_and_task_changes(
    execution_case, case
):
    """这里只核证明投影；合成 assessment 不充当真实业务，完整链另有集成用例。"""
    root, _, original = execution_case
    folder = root / ".ai-sdlc/loops/implementation/sample-implementation/proof-scope"
    task_id = "T21" if case == "both-T21" else "T11"
    current = next(
        subject for subject in original.subjects if subject.role == "current"
    )
    before_manifest = json.loads((root / current.snapshot.path).read_bytes())
    second_before = b"VALUE='second-old'\n"
    second_path = folder / "second-original.bin"
    LoopArtifactStore(root).write_bytes_artifact(
        second_path, second_before, immutable=True
    )
    second_ref = execution._ref(root, second_path)
    before_manifest["files"].append(
        {
            "path": "src/second.py",
            "sha256": hashlib.sha256(second_before).hexdigest(),
            "mode": "100644",
            "content_ref": second_ref.model_dump(mode="json"),
        }
    )
    before_ref = execution._write_json(root, folder / "before.json", before_manifest)
    before_plan = original.model_copy(
        update={
            "task_id": task_id,
            "subjects": tuple(
                subject.model_copy(update={"snapshot": before_ref})
                if subject.role == "current"
                else subject
                for subject in original.subjects
            ),
        }
    )
    after_manifest = copy.deepcopy(before_manifest)
    changed = {"src/save.py", "src/second.py"}
    if case == "no-own-change":
        changed = {"src/second.py"}
    elif case in {"single-task", "legacy-single-task"}:
        changed = {"src/save.py"}
    elif case == "unauthorized":
        changed.add("outside.py")
        after_manifest["files"].append(
            {"path": "outside.py", "sha256": "f" * 64, "mode": "100644"}
        )
    elif case == "protected":
        changed.add("tests/v0.py")
    for entry in after_manifest["files"]:
        if entry["path"] in changed:
            content = (entry["path"] + " repaired").encode()
            content_path = folder / (entry["path"].replace("/", "-") + ".bin")
            LoopArtifactStore(root).write_bytes_artifact(
                content_path, content, immutable=True
            )
            entry["sha256"] = hashlib.sha256(content).hexdigest()
            entry["content_ref"] = execution._ref(root, content_path).model_dump(
                mode="json"
            )
    after_ref = execution._write_json(root, folder / "after.json", after_manifest)
    after_plan = before_plan.model_copy(
        update={
            "candidate_digest": "b" * 64,
            "subjects": tuple(
                subject.model_copy(update={"snapshot": after_ref})
                if subject.role == "current"
                else subject
                for subject in before_plan.subjects
            ),
        }
    )
    if case == "changed-replay":
        first = after_plan.steps[0]
        after_plan = after_plan.model_copy(
            update={
                "steps": (
                    first.model_copy(
                        update={
                            "binding": first.binding.model_copy(
                                update={
                                    "argv": (*first.binding.argv, "different-input")
                                }
                            )
                        }
                    ),
                    *after_plan.steps[1:],
                )
            }
        )
    impl = SimpleNamespace(
        task_scopes={"T11": ["src/save.py"], "T21": ["src/second.py"]},
        declared_scope=["src/save.py", "src/second.py"],
    )
    if case in {"single-task", "unproven-sibling"}:
        impl.task_scopes = {"T11": ["src/save.py"]}
    elif case == "missing-current-task":
        impl.task_scopes = {"T21": ["src/second.py"]}
    elif case == "legacy-single-task":
        impl.task_scopes = {}
        impl.declared_scope = ["src/save.py"]
    before_observation = execution._write_json(
        root, folder / "old-observation.json", {"actual": "old"}
    )
    after_observation = execution._write_json(
        root, folder / "new-observation.json", {"actual": "new"}
    )
    previous = SimpleNamespace(
        plan=before_plan,
        assessment=SimpleNamespace(
            current_result=SimpleNamespace(
                status="PASS" if case == "no-original-fail" else "FAIL",
                evidence_refs=(before_observation,),
            )
        ),
    )
    assessment = SimpleNamespace(
        current_result=SimpleNamespace(
            status="PASS", evidence_refs=(after_observation,)
        )
    )
    record_ref = execution._write_json(
        root, folder / "old-record.json", {"unit": "only"}
    )
    plan_ref = execution._write_json(root, folder / "new-plan.json", after_plan)
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in (
            before_ref,
            after_ref,
            *(
                resource.initial_state
                for step in before_plan.steps
                for resource in step.binding.resources
            ),
        )
    }
    originals = dict(captured)
    if case == "missing-captured-original":
        captured.pop(before_ref.path)
    errors = {
        "unproven-sibling": "outside-original-task",
        "missing-current-task": "outside-original-task",
        "unauthorized": "outside-original-task",
        "protected": "outside-original-task",
        "no-own-change": "has-no-code-change",
        "changed-replay": "original-defect-path-not-retested",
        "no-original-fail": "requires-actual-business-before-and-after",
        "missing-captured-original": (
            r"^counterexample-captured-evidence-missing: \.ai-sdlc/loops/"
            r"implementation/sample-implementation/proof-scope/before\.json$"
        ),
    }

    def prove(material=None):
        return execution._repair_proof_payload(
            root, impl, record_ref, previous, plan_ref, after_plan, assessment, material
        )

    if case in errors:
        with pytest.raises(ValueError, match=errors[case]):
            prove(captured if case == "missing-captured-original" else None)
    else:
        proof = prove()
        expected_path = "src/second.py" if task_id == "T21" else "src/save.py"
        assert [change["path"] for change in proof["changes"]] == [expected_path]
        assert proof == prove(captured)
        assert proof["before_record_ref"] == record_ref.model_dump(mode="json")
        assert proof["before_observation_refs"] == [
            before_observation.model_dump(mode="json")
        ]
        assert proof["after_observation_refs"] == [
            after_observation.model_dump(mode="json")
        ]
    assert all((root / path).read_bytes() == raw for path, raw in originals.items())
    assert not execution._attempts_dir(root, original).exists()


@pytest.mark.parametrize(
    "case", ["latest", "late-history-cleanup", "unstarted-ambiguous", "old-replay"]
)
def test_fix17_current_task_plan_selection_preserves_order(execution_case, case):
    """仅测试已验证输入的选择语义；合成行不作为进程或业务验收证据。"""
    root, contract, original = execution_case
    successor = original.model_copy(update={"id": "fix17-successor"})
    plans = execution._attempts_dir(root, original).parent / "plans"
    for plan in (original, successor):
        execution._write_json(root, plans / f"{counterexample_digest(plan)}.json", plan)

    def row(plan, ordinal, *, cleanup=False):
        step = next(s for s in plan.steps if s.kind == "cleanup") if cleanup else plan.steps[0]
        return (
            plans / f"pure-row-{ordinal}",
            {"attempt_ordinal": ordinal, "historical_cleanup_only": cleanup},
            plan,
            AttemptReceipt(
                attempt_id=f"pure-row-{ordinal}",
                attempt_ref={"path": f"pure-row-{ordinal}.json", "sha256": "0" * 64},
                plan_digest=counterexample_digest(plan),
                contract_digest=plan.contract_digest,
                candidate_digest=plan.candidate_digest,
                step_id=step.id,
                subject_id=step.subject_id,
                binding_digest=counterexample_digest(step.binding),
                ownership_nonce="pure-selection-only",
                status="completed",
                started_at_ms=1,
                ended_at_ms=2,
                timed_out=False,
                output_truncated=False,
                exit_code=0,
                cleanup_status="complete",
                raw_evidence_refs=(),
            ),
        )

    history = [row(original, 1), row(successor, 2)]
    if case == "unstarted-ambiguous":
        history = []
        expected = "counterexample-frozen-task-plan-order-ambiguous"
    elif case == "old-replay":
        expected = "counterexample-superseded-plan-no-business-replay"
    else:
        expected = None
        if case == "late-history-cleanup":
            history.append(row(original, 3, cleanup=True))
    if expected:
        with pytest.raises(ValueError, match=expected):
            execution._shared_pending_steps(
                root, contract, original if case == "old-replay" else successor,
                history, {}, replace_current=case == "old-replay",
            )
    else:
        pending = execution._shared_pending_steps(
            root, contract, successor, history, {}, replace_current=False
        )
        assert pending
        assert {counterexample_digest(plan) for plan, _ in pending} == {
            counterexample_digest(successor)
        }


@pytest.mark.parametrize("link_kind", ["single-link", "hardlink"])
def test_v21_resource_file_requires_single_link(execution_case, link_kind):
    case = _same_resource_reset_case(execution_case, "restored")
    root, _, plan = case
    resource = Path(plan.steps[0].binding.resources[0].root)
    endpoint = resource / "result.json"
    initial = endpoint.read_bytes()
    outside = root.parent / "other-owned-temporary-data.json"
    outside.write_bytes(initial)
    if link_kind == "hardlink":
        endpoint.unlink()
        os.link(outside, endpoint)
        assert endpoint.stat().st_ino == outside.stat().st_ino
        assert endpoint.stat().st_nlink == 2
    else:
        assert endpoint.stat().st_nlink == outside.stat().st_nlink == 1
    intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    receipt = None
    rejection = None
    try:
        receipt = _execute(case, "current-explore")
    except ValueError as exc:
        rejection = str(exc)
    # 两个路径均由本测试创建；保留真正 child 写入与归属拒绝的原始差别。
    (root.parent / "single-link-observation.json").write_text(
        json.dumps(
            {
                "link_kind": link_kind,
                "endpoint_inode": endpoint.stat().st_ino,
                "outside_inode": outside.stat().st_ino,
                "nlink": endpoint.stat().st_nlink,
                "before": initial.decode(),
                "outside_after": outside.read_text(),
                "receipt_status": receipt.status if receipt else None,
                "rejection": rejection,
            }
        )
    )
    if link_kind == "hardlink":
        assert rejection is not None, "multiply linked resource reached the real child"
        assert "counterexample-resource-file-not-exclusively-owned" in rejection
        assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == intents
        assert not (resource / ".ai-sdlc-owner.json").exists()
        assert endpoint.read_bytes() == outside.read_bytes() == initial
    else:
        assert rejection is None and receipt is not None
        assert receipt.status == "completed"
        assert endpoint.read_bytes() == b'{"value":"stale"}'
        assert outside.read_bytes() == initial
        cleaned = _execute(case, "current-cleanup", cleanup_recovery=True)
        assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
        assert not resource.exists()
        assert execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref
        ) == receipt


@pytest.mark.parametrize("window", ["between-steps", "after-ownership-publication"])
def test_v21_active_resource_link_change_blocks_next_operation(
    execution_case, monkeypatch, window
):
    case = _same_resource_reset_case(execution_case, "restored")
    root, _, plan = case
    first = _execute(case, "current-explore")
    assert first.status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    endpoint = resource / "result.json"
    previous = endpoint.read_bytes()
    outside = root.parent / "other-active-temporary-data.json"
    outside.write_bytes(previous)
    original_writer = execution._write_json
    injected = []

    def replace_file():
        endpoint.unlink()
        os.link(outside, endpoint)
        injected.append((endpoint.stat().st_ino, outside.stat().st_ino))
        assert injected[-1][0] == injected[-1][1]
        assert endpoint.stat().st_nlink == 2

    def publish_then_replace(evidence_root, path, payload):
        reference = original_writer(evidence_root, path, payload)
        if path.name == "resource-ownership.json" and not injected:
            replace_file()
        return reference

    intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    if window == "between-steps":
        replace_file()
    receipt = None
    rejection = None
    with monkeypatch.context() as context:
        if window == "after-ownership-publication":
            context.setattr(execution, "_write_json", publish_then_replace)
        try:
            receipt = _execute(case, "current-reset")
        except ValueError as exc:
            rejection = str(exc)
    (root.parent / "active-link-observation.json").write_text(
        json.dumps(
            {
                "window": window,
                "injected": injected,
                "before": previous.decode(),
                "outside_after": outside.read_text(),
                "receipt_status": receipt.status if receipt else None,
                "rejection": rejection,
            }
        )
    )
    assert len(injected) == 1
    originals = {}
    if window == "between-steps":
        assert rejection is not None, "continued resource reached the real reset child"
        assert "counterexample-resource-file-not-exclusively-owned" in rejection
        assert receipt is None
        assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == intents
    else:
        assert rejection is None and receipt is not None
        assert receipt.status == "infrastructure_error"
        assert receipt.cleanup_status == "complete"
        attempt_dir = (root / receipt.attempt_ref.path).parent
        process = json.loads((attempt_dir / "process.json").read_bytes())
        raw = json.loads((attempt_dir / "raw-result.json").read_bytes())
        assert process["launcher"] == "nonce-gated-child"
        assert raw["launch_status"] == "started"
        assert (attempt_dir / "stdout").read_bytes() == b""
        assert "counterexample-resource-file-not-exclusively-owned" in json.loads(
            (attempt_dir / "postcheck-error.json").read_bytes()
        )["error"]
        refs = execution.attempt_artifact_refs(root, receipt.attempt_ref)
        captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
        assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
        assert execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        ) == receipt
        originals = {
            path: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()
        }
    assert outside.read_bytes() == endpoint.read_bytes() == previous
    _, debts = execution._historical_resource_cycles(root, plan)
    assert debts
    before_cleanup = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(
        ValueError, match="counterexample-resource-file-not-exclusively-owned"
    ):
        _execute(case, "current-cleanup", cleanup_recovery=True)
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == before_cleanup
    assert outside.read_bytes() == previous and resource.is_dir()
    # 只纠正本测试建立的别名；不让产品自动删除或重新认领归属不明的数据。
    endpoint.unlink()
    endpoint.write_bytes(previous)
    assert endpoint.stat().st_nlink == outside.stat().st_nlink == 1
    cleaned = _execute(case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not resource.exists() and outside.read_bytes() == previous
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    _, debts = execution._historical_resource_cycles(root, plan)
    assert not debts


def test_v21_cleanup_rechecks_file_ownership_per_resource(execution_case, monkeypatch):
    case, left, right = _case_with_two_separate_resources(execution_case)
    root, _, plan = case
    for suffix in ("none", "V0", "V1"):
        assert _execute(case, "current-" + suffix).status == "completed"
    outside = root.parent / "other-cleanup-temporary-data.json"
    original = (right / "result.json").read_bytes()
    outside.write_bytes(original)
    real_delete = execution.shutil.rmtree
    injected = []

    def delete_then_link_remaining(path, *args, **kwargs):
        result = real_delete(path, *args, **kwargs)
        if Path(path).name == left.name:
            endpoint = right / "result.json"
            endpoint.unlink()
            os.link(outside, endpoint)
            injected.append(endpoint.stat().st_nlink)
        return result

    with monkeypatch.context() as context:
        context.setattr(execution.shutil, "rmtree", delete_then_link_remaining)
        receipt = _execute(case, "current-cleanup")
    assert injected == [2]
    assert not left.exists()
    assert right.is_dir(), "cleanup deleted a resource whose file ownership changed"
    assert (right / "result.json").read_bytes() == outside.read_bytes() == original
    assert receipt.status != "completed" and receipt.cleanup_status != "complete"
    assert not (root / receipt.attempt_ref.path).with_name("resource-cleanup.json").exists()
    refs = execution.attempt_artifact_refs(root, receipt.attempt_ref)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    with pytest.raises(ValueError, match="counterexample-prior-execution-unknown-no-replay"):
        execution._historical_resource_cycles(root, plan)
    intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(ValueError, match="counterexample-prior-execution-unknown-no-replay"):
        _execute(case, "current-cleanup", cleanup_recovery=True)
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == intents
    assert right.is_dir() and outside.read_bytes() == original


def _v24_executable_observer_case(case):
    root, _, plan = case
    for project in {root, *(Path(step.binding.project_root) for step in plan.steps)}:
        (project / "observe.py").chmod(0o755)
    return _change_observer(
        case,
        f"#!{sys.executable}\n"
        "import json,pathlib,sys\n"
        "p=pathlib.Path(sys.argv[1])\n"
        "print('v24-actual-protected-observer', file=sys.stderr)\n"
        "value=json.loads(p.read_text())['value']\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':value}}))\n",
    )


def _v24_bare_observer_plan(plan, *, path_override=None, command="observe.py"):
    data = plan.model_dump(mode="json")
    for step in data["steps"]:
        if step["kind"] != "observe":
            continue
        binding = step["binding"]
        binding["argv"] = [command, *binding["argv"][2:]]
        binding["witness_input"]["argv_index"] -= 1
        environment = dict(binding["effective_environment"])
        environment["PATH"] = (
            (path_override or binding["project_root"])
            + os.pathsep
            + environment["PATH"]
        )
        binding.update(
            effective_environment=environment,
            environment_digest=counterexample_digest(environment),
        )
    return CounterexamplePlan.model_validate(data)


@pytest.mark.skipif(os.name == "nt", reason="直接 shebang 文件执行由 POSIX 宿主验证")
@pytest.mark.parametrize("endpoint", ["bare", "relative", "absolute"])
def test_v24_path_execution_originals_survive_reader_restart(
    execution_case, monkeypatch, endpoint
):
    root, contract, original = _v24_executable_observer_case(execution_case)
    plan = _v24_bare_observer_plan(original)
    if endpoint != "bare":
        steps = []
        for step in plan.steps:
            if step.kind == "observe":
                executable = (
                    "./observe.py"
                    if endpoint == "relative"
                    else str(Path(step.binding.project_root) / "observe.py")
                )
                step = step.model_copy(
                    update={
                        "binding": step.binding.model_copy(
                            update={"argv": (executable, *step.binding.argv[1:])}
                        )
                    }
                )
            steps.append(step)
        plan = plan.model_copy(update={"steps": tuple(steps)})
    case = root, contract, plan
    _started_history_input(case, monkeypatch)
    launched = []
    real_run = execution.run_quality_command

    def record_actual_launch(options):
        launched.append(options.argv)
        return real_run(options)

    with monkeypatch.context() as context:
        context.setattr(execution, "run_quality_command", record_actual_launch)
        receipt = _execute(case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    project = Path(plan.steps[0].binding.project_root)
    assert (root / receipt.attempt_ref.path).with_name("stderr").read_bytes() == (
        b"v24-actual-protected-observer\n"
    )
    expected = str((project / "observe.py").resolve())
    assert launched == [(expected, *plan.steps[0].binding.argv[1:])], (
        "校验选中的绝对执行身份必须直接交给已有执行器，不能再次搜索 PATH"
    )
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    intent = json.loads(originals[receipt.attempt_ref.path])
    resolved = intent["resolved_command"]
    assert resolved["argv"] == list(launched[0])
    assert (
        resolved["executable_sha256"]
        == hashlib.sha256((project / "observe.py").read_bytes()).hexdigest()
    )
    assert intent["binding"]["argv"] == list(plan.steps[0].binding.argv)
    completion_path = next(key for key in originals if key.endswith("/completion.json"))
    assert (
        json.loads(originals[completion_path])["intent_sha256"]
        == receipt.attempt_ref.sha256
    )
    assert execution.counterexample_execution_started(root, plan.loop_id)
    actual = execution.collect_observation(
        contract, plan, receipt, execution.read_owned_raw_evidence(root, receipt)
    )
    assert actual.typed_actual.value == "saved"
    # 后续宿主环境不能重新解释已完成原件；捕获集合缺失也不能借现场补齐。
    monkeypatch.setenv(
        "PATH",
        str(root.parent / "unrelated-later-path") + os.pathsep + os.environ["PATH"],
    )
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    assert execution.counterexample_execution_started(root, plan.loop_id)
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=originals
        )
        == receipt
    )
    for dependency in (receipt.attempt_ref.path, intent["source_snapshot"]["path"]):
        missing = dict(originals)
        missing.pop(dependency)
        with pytest.raises(ValueError):
            execution.recover_counterexample_attempt(
                root, plan, receipt.attempt_ref, captured_artifacts=missing
            )
    # 同一真实成功原件上的缺件/篡改分别覆盖现场读取与捕获读取。
    for damage in ("missing-resolved", "changed-resolved"):
        altered = copy.deepcopy(intent)
        if damage == "missing-resolved":
            altered.pop("resolved_command")
        else:
            altered["resolved_command"]["argv"][0] = str(root / "different-executable")
        bad_raw = json.dumps(altered).encode()
        with pytest.raises(ValueError, match="intent-missing-or-stale"):
            execution.recover_counterexample_attempt(
                root,
                plan,
                receipt.attempt_ref,
                captured_artifacts={**originals, receipt.attempt_ref.path: bad_raw},
            )
        try:
            (root / receipt.attempt_ref.path).write_bytes(bad_raw)
            with pytest.raises(ValueError, match="intent-missing-or-stale"):
                execution.recover_counterexample_attempt(
                    root, plan, receipt.attempt_ref
                )
            with pytest.raises(ValueError):
                execution.counterexample_execution_started(root, plan.loop_id)
        finally:
            (root / receipt.attempt_ref.path).write_bytes(
                originals[receipt.attempt_ref.path]
            )
    assert all((root / key).read_bytes() == raw for key, raw in originals.items())


@pytest.mark.skipif(os.name == "nt", reason="直接 shebang 文件执行由 POSIX 宿主验证")
@pytest.mark.parametrize("consumer", ["observer", "acceptance"])
def test_v24_bare_path_cannot_attribute_a_different_cwd_file(execution_case, consumer):
    root, contract, original = _v24_executable_observer_case(execution_case)
    external = root.parent / "different-path-command"
    external.mkdir()
    name = "observe.py" if consumer == "observer" else "v0.py"
    command = external / name
    command.write_text(
        f"#!{sys.executable}\n"
        "import json,pathlib,sys\n"
        "pathlib.Path(sys.argv[1]).with_name('unexpected-command-marker').write_text('wrong command ran')\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
    )
    command.chmod(0o755)
    if consumer == "observer":
        plan = _v24_bare_observer_plan(original, path_override=str(external))
    else:
        data = original.model_dump(mode="json")
        for step in data["steps"]:
            if step["kind"] != "acceptance" or step["acceptance_version"] != "V0":
                continue
            binding = step["binding"]
            binding["cwd"] = "tests"
            binding["argv"] = [name, *binding["argv"][2:]]
            environment = dict(binding["effective_environment"])
            environment["PATH"] = str(external) + os.pathsep + environment["PATH"]
            binding.update(
                effective_environment=environment,
                environment_digest=counterexample_digest(environment),
            )
        plan = CounterexamplePlan.model_validate(data)
    before = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    with pytest.raises(ValueError, match="command-not-bound|observer-script-not-bound"):
        _execute((root, contract, plan))
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == before
    for step in plan.steps:
        resource = Path(step.binding.resources[0].root)
        assert not (resource / "unexpected-command-marker").exists()


@pytest.mark.skipif(os.name == "nt", reason="直接 shebang 文件执行由 POSIX 宿主验证")
@pytest.mark.parametrize("consumer", ["observer", "acceptance"])
def test_v24_non_interpreter_filename_cannot_bind_protected_argument(
    execution_case, consumer
):
    root, contract, original = execution_case
    misleading = root.parent / "python"
    misleading.write_text(
        f"#!{sys.executable}\n"
        "import json,pathlib,sys\n"
        "pathlib.Path(sys.argv[2]).with_name('unexpected-command-marker').write_text('argument is data')\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
    )
    misleading.chmod(0o755)
    data = original.model_dump(mode="json")
    for step in data["steps"]:
        if (consumer == "observer" and step["kind"] == "observe") or (
            consumer == "acceptance" and step["kind"] == "acceptance"
        ):
            step["binding"]["argv"][0] = str(misleading)
    plan = CounterexamplePlan.model_validate(data)
    with pytest.raises(ValueError, match="command-not-bound|observer-script-not-bound"):
        _execute((root, contract, plan))
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    assert not list(root.parent.glob("*/scratch/unexpected-command-marker"))


def _v24_nested_resource_case(case):
    root, contract, original = case
    data = original.model_dump(mode="json")
    reference = original.steps[0].binding.resources[0].initial_state
    initial = json.loads((root / reference.path).read_bytes())
    initial["files"].append(
        {"path": "nested/kept.txt", "sha256": hashlib.sha256(b"kept").hexdigest()}
    )
    updated = execution._write_json(
        root, (root / reference.path).with_name("v24-nested-initial.json"), initial
    )
    for resource_root in {
        resource.root for step in original.steps for resource in step.binding.resources
    }:
        nested = Path(resource_root) / "nested"
        nested.mkdir()
        (nested / "kept.txt").write_bytes(b"kept")
    for step in data["steps"]:
        for resource in step["binding"]["resources"]:
            resource["initial_state"] = updated.model_dump(mode="json")
    return root, contract, CounterexamplePlan.model_validate(data)


def _v24_reparse_metadata(monkeypatch, target, enabled, traversed):
    real_scandir = os.scandir
    real_stat = os.stat

    def resource_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        # 注入实时元数据入口；目录扫描仍记录是否错误进入了 reparse 目标。
        if enabled() and not isinstance(path, int) and Path(path) == target:
            fields = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            return SimpleNamespace(**{**fields, "st_file_attributes": 0x400})
        return info

    class Scan:
        def __init__(self, path):
            if enabled() and not isinstance(path, int) and Path(path) == target:
                traversed.append(str(path))
            self.original = real_scandir(path)

        def __enter__(self):
            return iter(self)

        def __exit__(self, *args):
            self.original.close()

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.original)

        def close(self):
            self.original.close()

    monkeypatch.setattr(execution.os, "scandir", Scan)
    monkeypatch.setattr(execution.os, "stat", resource_stat)


@pytest.mark.parametrize("consumer", ["inventory", "observation", "hardlink"])
def test_resource_inventory_reads_live_identity_beyond_directory_cache(
    execution_case, monkeypatch, consumer
):
    _, _, plan = _v24_nested_resource_case(execution_case)
    resource = plan.steps[0].binding.resources[0]
    directory = Path(resource.root)
    target = directory / "nested/kept.txt"
    if consumer == "hardlink":
        os.link(target, directory.parent / "external-link.txt")
        assert target.stat().st_nlink == 2
    real_scandir = os.scandir

    def cached_entry(entry):
        info = Path(entry.path).lstat()
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        # Windows 3.11 的目录缓存没有身份字段；陈旧缓存也不能隐藏新增硬链接。
        fields.update(st_ino=0, st_dev=0, st_nlink=1 if consumer == "hardlink" else 0)
        return SimpleNamespace(
            path=entry.path, stat=lambda **kwargs: SimpleNamespace(**fields)
        )

    @contextmanager
    def cached_scan(path):
        with real_scandir(path) as entries:
            yield (cached_entry(entry) for entry in entries)

    with monkeypatch.context() as context:
        context.setattr(execution.os, "scandir", cached_scan)
        deadline = time.time_ns() // 1_000_000 + 10000
        if consumer == "hardlink":
            with pytest.raises(ValueError, match="not-exclusively-owned"):
                execution._resource_inventory(directory, deadline_ms=deadline)
        elif consumer == "inventory":
            inventory = execution._resource_inventory(directory, deadline_ms=deadline)
            info = target.lstat()
            assert inventory["nested/kept.txt"][0:2] == (info.st_dev, info.st_ino)
            assert inventory["nested/kept.txt"][-1] == 1
        else:
            snapshot = execution._observed_resource_state((resource,), deadline_ms=deadline)
            files = {item["path"]: item for item in snapshot[0]["files"]}
            assert files["nested/kept.txt"]["sha256"] == hashlib.sha256(b"kept").hexdigest()
    assert target.read_bytes() == b"kept"


@pytest.mark.parametrize("consumer", ["inventory", "observation"])
@pytest.mark.parametrize("kind", ["directory", "file"])
def test_v24_nested_reparse_attributes_rejected_before_content(
    execution_case, monkeypatch, consumer, kind
):
    case = _v24_nested_resource_case(execution_case)
    _, _, plan = case
    resource = plan.steps[0].binding.resources[0]
    directory = Path(resource.root)
    target = directory / ("nested" if kind == "directory" else "nested/kept.txt")
    traversed = []
    with monkeypatch.context() as context:
        _v24_reparse_metadata(context, target, lambda: True, traversed)
        with pytest.raises(ValueError, match="reparse|special-node|directory-identity"):
            if consumer == "inventory":
                execution._resource_inventory(
                    directory, deadline_ms=time.time_ns() // 1_000_000 + 10000
                )
            else:
                execution._observed_resource_state(
                    (resource,), deadline_ms=time.time_ns() // 1_000_000 + 10000
                )
    assert traversed == [], "属性拒绝必须发生在进入子目录之前"
    assert (directory / "nested/kept.txt").read_bytes() == b"kept"
    assert execution._resource_inventory(
        directory, deadline_ms=time.time_ns() // 1_000_000 + 10000
    )


@pytest.mark.parametrize(
    "window", ["admission", "between-steps", "prelaunch", "cleanup"]
)
def test_v24_resource_consumers_share_nested_reparse_rejection(
    execution_case, monkeypatch, window
):
    case = _v24_nested_resource_case(execution_case)
    root, _, plan = case
    resource = Path(plan.steps[0].binding.resources[0].root)
    if window in {"between-steps", "cleanup"}:
        assert _execute(case).status == "completed"
    active = window != "prelaunch"
    traversed = []
    writer = execution._write_json

    def publish_then_change(evidence_root, path, payload):
        nonlocal active
        reference = writer(evidence_root, path, payload)
        if path.name == "resource-ownership.json":
            active = True
        return reference

    step_id = {
        "admission": "current-none",
        "prelaunch": "current-none",
        "between-steps": "current-V0",
        "cleanup": "current-cleanup",
    }[window]
    with monkeypatch.context() as context:
        _v24_reparse_metadata(context, resource / "nested", lambda: active, traversed)
        if window == "prelaunch":
            context.setattr(execution, "_write_json", publish_then_change)
            receipt = _execute(case, step_id)
            assert receipt.status == "infrastructure_error"
            assert receipt.cleanup_status == "complete"
            folder = (root / receipt.attempt_ref.path).parent
            assert (folder / "stdout").read_bytes() == b""
            error = json.loads((folder / "postcheck-error.json").read_bytes())["error"]
            assert any(
                word in error
                for word in ("reparse", "special-node", "directory-identity")
            )
            captured = {
                ref.path: (root / ref.path).read_bytes()
                for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
            }
            assert (
                execution.recover_counterexample_attempt(
                    root, plan, receipt.attempt_ref
                )
                == receipt
            )
            assert (
                execution.recover_counterexample_attempt(
                    root, plan, receipt.attempt_ref, captured_artifacts=captured
                )
                == receipt
            )
        else:
            with pytest.raises(
                ValueError, match="reparse|special-node|directory-identity"
            ):
                _execute(case, step_id, cleanup_recovery=window == "cleanup")
    assert traversed == []
    assert (resource / "nested/kept.txt").read_bytes() == b"kept"
    assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'


@pytest.mark.skipif(
    os.name != "nt", reason="必须在 Windows 实机执行 junction 原生属性入口"
)
@pytest.mark.parametrize(
    "window", ["admission", "between-steps", "prelaunch", "cleanup"]
)
def test_v24_native_windows_junction_rejected_without_target_changes(
    execution_case, monkeypatch, window
):
    case = _v24_nested_resource_case(execution_case)
    root, _, plan = case
    resource = Path(plan.steps[0].binding.resources[0].root)
    nested = resource / "nested"
    target = root.parent / "test-owned-junction-target"
    target.mkdir()
    (target / "kept.txt").write_bytes(b"kept")
    if window in {"between-steps", "cleanup"}:
        assert _execute(case).status == "completed"
    writer = execution._write_json
    created = []

    def create_junction():
        shutil.rmtree(nested)
        completed = subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(nested),
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        created.append(completed.stdout)
        assert nested.lstat().st_file_attributes & 0x400

    def publish_then_change(evidence_root, path, payload):
        reference = writer(evidence_root, path, payload)
        if path.name == "resource-ownership.json" and not created:
            create_junction()
        return reference

    try:
        if window != "prelaunch":
            create_junction()
        with monkeypatch.context() as context:
            if window == "prelaunch":
                context.setattr(execution, "_write_json", publish_then_change)
                receipt = _execute(case)
                assert receipt.status == "infrastructure_error"
                assert receipt.cleanup_status == "complete"
                folder = (root / receipt.attempt_ref.path).parent
                assert (folder / "stdout").read_bytes() == b""
                error = json.loads((folder / "postcheck-error.json").read_bytes())[
                    "error"
                ]
                assert any(
                    word in error
                    for word in ("reparse", "special-node", "directory-identity")
                )
            else:
                step_id = {
                    "admission": "current-none",
                    "between-steps": "current-V0",
                    "cleanup": "current-cleanup",
                }[window]
                with pytest.raises(
                    ValueError, match="reparse|special-node|directory-identity"
                ):
                    _execute(case, step_id, cleanup_recovery=window == "cleanup")
        assert len(created) == 1
        assert (target / "kept.txt").read_bytes() == b"kept"
        assert sorted(path.name for path in target.iterdir()) == ["kept.txt"]
        assert nested.is_dir()
    finally:
        # junction 及目标均由本测试创建，清理由测试移除目录入口，不递归进入目标。
        if created and nested.exists():
            nested.rmdir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 测试使用临时解释器别名")
@pytest.mark.parametrize("changed", [False, True])
def test_v24_prelaunch_path_alias_cannot_select_a_second_executable(
    execution_case, monkeypatch, changed
):
    root, contract, original = execution_case
    directory = root.parent / "test-owned-interpreter-path"
    directory.mkdir()
    alias = directory / "python"
    alias.symlink_to(Path(sys.executable).resolve())
    data = original.model_dump(mode="json")
    for step in data["steps"]:
        binding = step["binding"]
        binding["argv"][0] = "python"
        environment = dict(binding["effective_environment"])
        environment["PATH"] = str(directory) + os.pathsep + environment["PATH"]
        binding.update(
            effective_environment=environment,
            environment_digest=counterexample_digest(environment),
        )
    plan = CounterexamplePlan.model_validate(data)
    writer = execution._write_json
    published = []

    def publish_then_replace(evidence_root, path, payload):
        reference = writer(evidence_root, path, payload)
        if path.name == "resource-ownership.json" and not published:
            published.append(path)
            if changed:
                alias.unlink()
                alias.write_text(
                    f"#!{sys.executable}\n"
                    "import json,pathlib,sys\n"
                    "pathlib.Path(sys.argv[2]).with_name('unexpected-command-marker').write_text('second PATH lookup')\n"
                    "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
                )
                alias.chmod(0o755)
        return reference

    with monkeypatch.context() as context:
        context.setattr(execution, "_write_json", publish_then_replace)
        receipt = _execute((root, contract, plan))
    assert len(published) == 1
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert not (resource / "unexpected-command-marker").exists(), (
        "归属公布后不能让裸命令的第二次 PATH 搜索运行另一个文件"
    )
    assert receipt.cleanup_status == "complete"
    if changed:
        # 保持已解析的真实程序或明确拒绝漂移均可，但不能运行替换后的程序。
        assert receipt.status in {"completed", "infrastructure_error"}
    else:
        assert receipt.status == "completed"
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )
        == receipt
    )


def test_v24_current_venv_callable_keeps_real_prefix(execution_case):
    if sys.prefix == sys.base_prefix:
        pytest.skip("当前测试宿主未使用真实 venv；不能假称已验证 venv 调用语义")
    case = _change_observer(
        execution_case,
        "import json,sys\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':sys.prefix}}))\n",
    )
    root, contract, plan = case
    receipt = _execute(case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    original = json.loads((root / receipt.attempt_ref.path).read_bytes())
    assert original["resolved_command"]["argv"][0] == os.path.abspath(sys.executable)
    assert original["resolved_command"]["executable_target"] == str(
        Path(sys.executable).resolve()
    )
    observation = execution.collect_observation(
        contract, plan, receipt, execution.read_owned_raw_evidence(root, receipt)
    )
    assert observation.typed_actual.value == sys.prefix
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=captured
        )
        == receipt
    )


def _fix20_permission_case(case, phase, target_kind, mutate):
    case = _v24_nested_resource_case(case)
    root, contract, plan = case
    for resource_root in {
        resource.root for step in plan.steps for resource in step.binding.resources
    }:
        resource = Path(resource_root)
        resource.chmod(0o700)
        (resource / "nested").chmod(0o700)
        (resource / "result.json").chmod(0o600)
    target = {
        "file": "resource / 'result.json'",
        "nested-directory": "resource / 'nested'",
        "resource-directory": "resource",
    }[target_kind]
    code = (
        "import json,pathlib,stat,sys\n"
        "resource=pathlib.Path(sys.argv[1]).parent\n"
        f"target={target}\n"
        "before=stat.S_IMODE(target.stat().st_mode)\n"
        f"target.chmod(before | 0o040) if {mutate!r} else None\n"
    )
    if phase == "observe":
        code += (
            "print(json.dumps({'schema_version':1,"
            "'typed_actual':{'type':'string','value':'saved'}}))\n"
        )
        return _change_observer(case, code)
    code += (
        "print(json.dumps({'schema_version':1,'assertion_id':'saved-state',"
        "'reached':True,'assertion_result':'accepted','failure_reason':'none'}))\n"
    )
    (root / "tests/v0.py").write_text(code)
    for project in {step.binding.project_root for step in plan.steps}:
        (Path(project) / "tests/v0.py").write_text(code)
    # 复用现有全主体捕获，保护脚本的变更必须在执行前进入原计划身份。
    root, contract, recaptured = _change_observer(case, (root / "observe.py").read_text())
    data = recaptured.model_dump(mode="json")
    data["v0_digest"] = hashlib.sha256(code.encode()).hexdigest()
    return root, contract, CounterexamplePlan.model_validate(data)


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 模式；不冒充 Windows ACL 验收")
@pytest.mark.parametrize(
    "phase,target_kind,mutate",
    [
        ("observe", "file", True),
        ("observe", "nested-directory", True),
        ("observe", "resource-directory", True),
        ("V0", "file", True),
        ("V0", "nested-directory", True),
        ("V0", "resource-directory", True),
        ("observe", "file", False),
        ("V0", "file", False),
    ],
)
def test_fix20_readonly_command_chmod_retains_rejected_originals(
    execution_case, phase, target_kind, mutate
):
    case = _fix20_permission_case(execution_case, phase, target_kind, mutate)
    root, _, plan = case
    resource = Path(plan.steps[0].binding.resources[0].root)
    target = {
        "file": resource / "result.json",
        "nested-directory": resource / "nested",
        "resource-directory": resource,
    }[target_kind]
    mode_before = stat.S_IMODE(target.stat().st_mode)
    content_before = (resource / "result.json").read_bytes()
    if phase == "V0":
        assert _execute(case).status == "completed"
    receipt = _execute(case, "current-none" if phase == "observe" else "current-V0")
    assert (resource / "result.json").read_bytes() == content_before
    assert stat.S_IMODE(target.stat().st_mode) == (
        mode_before | 0o040 if mutate else mode_before
    )
    assert receipt.exit_code == 0 and receipt.cleanup_status == "complete"
    assert receipt.status == ("infrastructure_error" if mutate else "completed")
    folder = (root / receipt.attempt_ref.path).parent
    state = json.loads((folder / "resource-state.json").read_bytes())
    assert (state["before"] != state["after"]) is mutate
    if mutate:
        error = json.loads((folder / "postcheck-error.json").read_bytes())
        assert "counterexample-observation-resource-state-mutated" in error["error"]
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=originals
        )
        == receipt
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 模式；不冒充 Windows ACL 验收")
@pytest.mark.parametrize(
    "target_kind,mutate",
    [
        ("file", True),
        ("nested-directory", True),
        ("resource-directory", True),
        ("file", False),
    ],
)
def test_fix20_acceptance_requires_observed_permission_state(
    execution_case, target_kind, mutate
):
    case = _fix20_permission_case(execution_case, "observe", target_kind, False)
    root, _, plan = case
    assert _execute(case).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    target = {
        "file": resource / "result.json",
        "nested-directory": resource / "nested",
        "resource-directory": resource,
    }[target_kind]
    before = list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    if mutate:
        target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o040)
        with pytest.raises(ValueError, match="acceptance-business-state-mismatch"):
            _execute(case, "current-V0")
        assert list(execution._attempts_dir(root, plan).glob("*/intent.json")) == before
    else:
        assert _execute(case, "current-V0").status == "completed"
    assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'


@pytest.mark.skipif(sys.platform != "darwin", reason="真实 macOS 缺 waitid 分支")
def test_fix20_counterexample_executes_without_python_waitid(
    execution_case, monkeypatch
):
    from ai_sdlc.core import quality_command

    root, _, plan = execution_case
    with monkeypatch.context() as patch:
        patch.delattr(quality_command.os, "waitid", raising=False)
        receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    assert receipt.exit_code == 0
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert (
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref)
        == receipt
    )
    assert (
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=originals
        )
        == receipt
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 真实执行与冷恢复消费者")
def test_fix23_counterexample_executes_and_recovers_without_ps(execution_case, monkeypatch):
    from tests.unit.test_quality_command import _fix23_ps_availability

    calls = _fix23_ps_availability(monkeypatch, True)
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    assert calls and receipt.status == "completed"
    assert receipt.exit_code == 0 and receipt.cleanup_status == "complete"
    assert execution._recover_plan_attempts(root, plan) == [receipt]
    originals = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=originals
    ) == receipt


@pytest.mark.parametrize("fault", ["output-read", "raw-before-publication", "raw-after-publication"])
def test_fix26_known_output_or_raw_failure_recovers_original_cleanup(
    execution_case, monkeypatch, fault
):
    root, _, plan = execution_case
    real_open, real_write = Path.open, execution._write_json
    faults = []

    def output_fault(path, mode="r", *args, **kwargs):
        if path.name == "stdout" and mode == "rb" and not faults:
            faults.append("output-read")
            raise OSError("original output read unavailable")
        return real_open(path, mode, *args, **kwargs)

    def raw_fault(owner, path, payload, **kwargs):
        if path.name == "raw-result.json":
            faults.append("raw-publication")
            if fault == "raw-after-publication":
                real_write(owner, path, payload, **kwargs)
            raise OSError("original raw publication unavailable")
        return real_write(owner, path, payload, **kwargs)

    with monkeypatch.context() as patch:
        if fault == "output-read":
            patch.setattr(Path, "open", output_fault)
        else:
            patch.setattr(execution, "_write_json", raw_fault)
        receipt = _execute(execution_case)
    assert len(faults) == 1
    assert receipt.status == "infrastructure_error"
    assert receipt.cleanup_status == "complete" and receipt.exit_code == 0
    folder = (root / receipt.attempt_ref.path).parent
    completion = json.loads((folder / "completion.json").read_bytes())
    raw_path = folder / "raw-result.json"
    assert raw_path.exists() == (fault != "raw-before-publication")
    assert (completion["artifact_sha256"]["raw-result.json"] is None) == (not raw_path.exists())
    assert (folder / "postcheck-error.json").is_file()
    assert not (folder / "postcheck.json").exists()
    original_files = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    assert execution._recover_plan_attempts(root, plan) == [receipt]
    assert _execute(execution_case) == receipt
    for missing in ("completion.json", "cleanup.json"):
        incomplete = dict(captured)
        incomplete.pop((folder / missing).relative_to(root).as_posix())
        recovered = execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=incomplete
        )
        assert recovered.status != "completed" and recovered.cleanup_status == "unknown"
    if raw_path.exists():
        incomplete = dict(captured)
        incomplete.pop(raw_path.relative_to(root).as_posix())
        if fault == "raw-after-publication":
            with pytest.raises(ValueError, match="completion-originals"):
                execution.recover_counterexample_attempt(
                    root, plan, receipt.attempt_ref, captured_artifacts=incomplete
                )
        else:
            recovered = execution.recover_counterexample_attempt(
                root, plan, receipt.attempt_ref, captured_artifacts=incomplete
            )
            assert recovered.cleanup_status == "unknown"
    damaged = dict(captured)
    cleanup_name = (folder / "cleanup.json").relative_to(root).as_posix()
    changed = json.loads(damaged[cleanup_name])
    changed["checked_at_ms"] += 1
    damaged[cleanup_name] = json.dumps(changed).encode()
    with pytest.raises(ValueError, match="completion-originals"):
        execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref, captured_artifacts=damaged
        )
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert resource.is_dir()
    cleaned = _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not resource.exists()
    assert all(path.read_bytes() == raw for path, raw in original_files.items())


@pytest.mark.parametrize("damage", ["missing-plan-digest", "deadline-type", "binding-shape"])
def test_fix28_incomplete_intent_is_rejected_by_all_original_consumers(
    execution_case, monkeypatch, damage
):
    root, _, plan = execution_case
    _started_history_input(execution_case, monkeypatch)
    receipt = _execute(execution_case)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    path = root / receipt.attempt_ref.path
    original = path.read_bytes()
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    damaged = json.loads(original)
    if damage == "missing-plan-digest":
        damaged.pop("plan_digest")
    elif damage == "deadline-type":
        damaged["deadline_ms"] = "not-a-millisecond-value"
    else:
        damaged["binding"] = []
    path.write_text(json.dumps(damaged))
    damaged_ref = execution._ref(root, path)
    captured[path.relative_to(root).as_posix()] = path.read_bytes()
    before = {item: item.read_bytes() for item in path.parent.iterdir() if item.is_file()}
    attempts = set(execution._attempts_dir(root, plan).iterdir())
    # 用实际变更后的引用检查内容本身，不能因旧摘要不匹配而提前假通过。
    readers = (
        lambda: _execute(execution_case),
        lambda: execution._recover_plan_attempts(root, plan),
        lambda: execution.recover_counterexample_attempt(root, plan, damaged_ref),
        lambda: execution.attempt_artifact_refs(root, damaged_ref),
        lambda: execution.counterexample_execution_started(root, plan.loop_id),
        lambda: _execute(execution_case, "current-cleanup", cleanup_recovery=True),
    )
    for reader in readers:
        with pytest.raises(ValueError, match="counterexample-.*intent"):
            reader()
        assert set(execution._attempts_dir(root, plan).iterdir()) == attempts
        assert all(item.read_bytes() == raw for item, raw in before.items())
    # 捕获损坏时即便现场原件正确也不得补读；恢复现场是测试操作，不是产品恢复。
    path.write_bytes(original)
    for reader in (
        lambda: execution._attempt_history(root, execution._attempts_dir(root, plan), captured),
        lambda: execution.recover_counterexample_attempt(
            root, plan, damaged_ref, captured_artifacts=captured
        ),
        lambda: execution.attempt_artifact_refs(root, damaged_ref, captured_artifacts=captured),
    ):
        with pytest.raises(ValueError, match="counterexample-.*intent"):
            reader()
    assert path.read_bytes() == original
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert _execute(execution_case) == receipt
    cleaned = _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize("damage", ["plan-digest", "command-bindings"])
def test_fix28_incomplete_intent_is_rejected_before_publication(execution_case, damage):
    root, _, plan = execution_case
    receipt = _execute(execution_case)
    document = json.loads((root / receipt.attempt_ref.path).read_bytes())
    document.update(attempt_id="attempt-invalid-next", attempt_ordinal=2)
    if damage == "plan-digest":
        document.pop("plan_digest")
    else:
        document.pop("resolved_command")
        document.pop("source_snapshot")
    folder = execution._attempts_dir(root, plan) / document["attempt_id"]
    before = set(execution._attempts_dir(root, plan).iterdir())
    with pytest.raises(ValueError, match="counterexample-.*(intent|command)"):
        execution._publish_attempt_intent(root, folder, document)
    assert not folder.exists()
    assert set(execution._attempts_dir(root, plan).iterdir()) == before


def test_fix28_job_setup_failure_keeps_originals_and_allows_resource_cleanup(
    execution_case, monkeypatch
):
    from ai_sdlc.core import quality_command as quality

    root, _, plan = execution_case
    _started_history_input(execution_case, monkeypatch)
    calls = []

    def unavailable_job(nonce):
        calls.append(nonce)
        raise OSError("controlled Windows Job creation temporarily unavailable")

    with monkeypatch.context() as patch:
        # 只选择受控执行器的 Windows 分支，不修改全局 os.name 或模拟任何回执。
        patch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
        patch.setattr(quality, "_WindowsOwnedJob", unavailable_job)
        receipt = _execute(execution_case)
    assert calls == [receipt.ownership_nonce]
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert receipt.exit_code is None
    folder = (root / receipt.attempt_ref.path).parent
    assert not (folder / "process.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    cleanup = json.loads((folder / "cleanup.json").read_bytes())
    assert raw["launch_status"] == "never_started" and raw["exit_code"] is None
    assert "Windows Job creation temporarily unavailable" in raw["launch_error"]
    assert cleanup["status"] == "complete" and (folder / "completion.json").is_file()
    originals = {item: item.read_bytes() for item in folder.iterdir() if item.is_file()}
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    assert execution.counterexample_execution_started(root, plan.loop_id)
    assert _execute(execution_case) == receipt
    assert len(list(execution._attempts_dir(root, plan).glob("*/intent.json"))) == 1
    cleaned = _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not Path(plan.steps[0].binding.resources[0].root).exists()
    assert all(item.read_bytes() == raw for item, raw in originals.items())


@pytest.mark.parametrize(
    "termination",
    [
        pytest.param("signal", marks=pytest.mark.skipif(
            os.name != "posix", reason="真实 POSIX 信号终止")),
        "normal0", "normal1", "normal247", "normal255",
        pytest.param("windows-exception", marks=pytest.mark.skipif(
            os.name != "nt", reason="真实 Windows 未处理异常，需要 Windows 实机")),
        pytest.param("windows-fatal-app-exit", marks=pytest.mark.skipif(
            os.name != "nt", reason="真实 Windows FatalAppExit，需要 Windows 实机")),
    ],
)
def test_fix28_native_acceptance_signal_is_unknown_and_cleanup_remains_available(
    execution_case, monkeypatch, termination
):
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample

    root, contract, original = execution_case
    abnormal = termination in {"signal", "windows-exception", "windows-fatal-app-exit"}
    expected_exit = {
        "signal": -9, "windows-exception": 0xC0000602,
        "windows-fatal-app-exit": 0x40000015,
        "normal0": 0, "normal1": 1, "normal247": 247, "normal255": 255,
    }[termination]
    if termination == "signal":
        terminate = "if bad: os.kill(os.getpid(),signal.SIGKILL)\n"
    elif termination in {"windows-exception", "windows-fatal-app-exit"}:
        # Fail-fast 绕过 ctypes 的 SEH 捕获，不能用普通 Python 异常或填回执代替。
        terminate = (
            "if bad:\n"
            " import ctypes\n"
            " kernel=ctypes.WinDLL('kernel32',use_last_error=True)\n"
            " kernel.SetErrorMode(3)\n"
            " kernel.RaiseFailFastException.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]\n"
            " kernel.RaiseFailFastException.restype=None\n"
            " class ExceptionRecord(ctypes.Structure):\n"
            "  _fields_=[('code',ctypes.c_ulong),('flags',ctypes.c_ulong),"
            "('nested',ctypes.c_void_p),('address',ctypes.c_void_p),"
            "('count',ctypes.c_ulong),('information',ctypes.c_size_t*15)]\n"
            " record=ExceptionRecord()\n"
            f" record.code={expected_exit}\n"
            " record.flags=1\n"
            " record.address=ctypes.cast(kernel.RaiseFailFastException,ctypes.c_void_p).value\n"
            " kernel.RaiseFailFastException(ctypes.byref(record),None,0)\n"
            " raise RuntimeError('fail-fast unexpectedly returned')\n"
        )
    else:
        terminate = f"if bad: sys.exit({expected_exit})\n"
    code = (
        "import json,os,runpy,signal,sys\n"
        "bad=runpy.run_path('src/save.py')['VALUE']=='lost'\n"
        "print(json.dumps({'schema_version':1,'assertion_id':'saved-state',"
        "'reached':True,'assertion_result':'rejected' if bad else 'accepted',"
        "'failure_reason':'target_assertion' if bad else 'none'}),flush=True)\n"
        + terminate
    )
    (root / "tests/v0.py").write_text(code)
    for subject in original.subjects:
        project = Path(next(step.binding.project_root for step in original.steps
                            if step.subject_id == subject.id))
        (project / "tests/v0.py").write_text(code)
    data = original.model_dump(mode="json")
    data["v0_digest"] = hashlib.sha256(code.encode()).hexdigest()
    case = _change_observer(
        (root, contract, CounterexamplePlan.model_validate(data)),
        "import json,runpy\n"
        "value=runpy.run_path('src/save.py')['VALUE']\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':value}}))\n",
    )
    root, contract, plan = case
    _started_history_input(case, monkeypatch)
    receipts = [_execute(case, step) for step in (
        "current-none", "current-V0", "current-V1", "current-cleanup", "variant-none"
    )]
    receipt = _execute(case, "variant-V0")
    folder = (root / receipt.attempt_ref.path).parent
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    assert b'"assertion_result": "rejected"' in (folder / "stdout").read_bytes()
    assert receipt.exit_code == expected_exit
    assert receipt.normally_completed is (not abnormal)
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    captured = {ref.path: (root / ref.path).read_bytes()
                for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    raw = execution.read_owned_raw_evidence(root, receipt, captured_artifacts=captured)
    assert raw and all(item.complete for item in raw)
    observation = execution.collect_observation(contract, plan, receipt, raw)
    assert observation.assertion_result == ("unknown" if abnormal else "rejected")
    receipts.append(receipt)
    assessment = evaluate_counterexample(
        contract, plan, execution._bundle_from_attempts(root, contract, plan, receipts)
    )
    variant = next(item for item in assessment.variants if item.subject_id == "variant")
    assert variant.v0 == ("unknown" if abnormal else "detected")
    before = set(execution._attempts_dir(root, plan).iterdir())
    if abnormal:
        assert not assessment.required_complete
        assert assessment.v1_disposition == "retain_v0"
        with pytest.raises(ValueError, match="counterexample-.*(dependency|failed|blocked)"):
            _execute(case, "variant-V1")
        assert set(execution._attempts_dir(root, plan).iterdir()) == before
    else:
        assert _execute(case, "variant-V1").exit_code == 0
    cleaned = _execute(case, "variant-cleanup", cleanup_recovery=True)
    assert cleaned.exit_code == 0 and cleaned.cleanup_status == "complete"
    variant_step = next(step for step in plan.steps if step.id == "variant-none")
    assert not Path(variant_step.binding.resources[0].root).exists()
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("entry", ["native", "step"])
def test_mvp_unstarted_predecessor_cannot_be_taken_over(execution_case, monkeypatch, entry):
    root, _, _ = execution_case
    contract, impl, loop, previous = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    proposed = previous.model_copy(update={"id": "mvp-new-after-zero-attempt"})
    execution.validate_plan_contract(contract, proposed)
    folder = execution._attempts_dir(root, previous).parent
    old_ref = execution._write_json(
        root, folder / "plans" / f"{counterexample_digest(previous)}.json", previous
    )
    original = (root / old_ref.path).read_bytes()
    proposed_path = folder / "mvp-proposed-no-takeover.json"
    proposed_path.write_bytes(execution._json_bytes(proposed))
    resources = {resource.root for step in proposed.steps for resource in step.binding.resources}

    def unexpected_publication(*args, **kwargs):
        pytest.fail("未启动前序尚未完成，后继却已经到达业务意图发布")

    monkeypatch.setattr(execution, "_publish_attempt_intent", unexpected_publication)
    with pytest.raises(ValueError, match="counterexample-unfinished-predecessor-no-takeover"):
        if entry == "native":
            execution.run_counterexample_plan(
                root, impl, loop, proposed.task_id, proposed_path.relative_to(root).as_posix()
            )
        else:
            execution.execute_counterexample_attempt(
                root, contract, proposed, proposed.steps[0].id,
                deadline_ms=time.time_ns() // 1_000_000 + 3_600_000,
            )
    assert (root / old_ref.path).read_bytes() == original
    assert not (folder / "plans" / f"{counterexample_digest(proposed)}.json").exists()
    assert not list(folder.glob("attempts/*/intent.json"))
    assert not list((folder / "results").glob("record-reconciled-*.json"))
    assert all(not (Path(resource) / ".ai-sdlc-owner.json").exists() for resource in resources)


@pytest.mark.parametrize("cycle", ["initial", "reset"])
@pytest.mark.parametrize("publication", ["before", "after"])
def test_mvp_ownership_publication_failure_keeps_cleanup_path(
    execution_case, monkeypatch, cycle, publication
):
    case = _case_with_frozen_resets(execution_case)
    root, _, plan = case
    resource = Path(plan.steps[0].binding.resources[0].root)
    step_id = "current-none"
    cleanup_id = "current-cleanup"
    if cycle == "reset":
        for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
            assert _execute(case, identifier).status == "completed"
        assert not resource.exists()
        step_id, cleanup_id = "current-reset", "r2-current-cleanup"
    original_write = execution._write_json
    failures = []

    def one_shot_write(evidence_root, path, payload):
        if path.name == "resource-ownership.json" and not failures:
            marker = resource / ".ai-sdlc-owner.json"
            assert marker.is_file()
            claim = json.loads(marker.read_bytes())["ownership_ref"]
            assert hashlib.sha256((root / claim["path"]).read_bytes()).hexdigest() == claim["sha256"]
            assert (resource / "result.json").read_bytes() == b'{"value":"saved"}'
            failures.append((marker.read_bytes(), (root / claim["path"]).read_bytes()))
            if publication == "after":
                original_write(evidence_root, path, payload)
            raise OSError("one-shot ownership manifest publication failure")
        return original_write(evidence_root, path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_write_json", one_shot_write)
        receipt = _execute(case, step_id)
    assert len(failures) == 1
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert receipt.exit_code is None
    folder = (root / receipt.attempt_ref.path).parent
    assert not (folder / "process.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and raw["exit_code"] is None
    assert "one-shot ownership manifest" in raw["launch_error"]
    assert (folder / "stdout").read_bytes() == (folder / "stderr").read_bytes() == b""
    assert (folder / "postcheck-error.json").is_file()
    assert not (folder / "postcheck.json").exists()
    originals = {item: item.read_bytes() for item in folder.iterdir() if item.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    assert receipt in execution._recover_plan_attempts(root, plan)
    assert _execute(case, step_id) == receipt
    history, debts = execution._historical_resource_cycles(root, plan)
    assert debts and history[-1][3] == receipt
    damaged = dict(captured)
    damaged.pop(next(key for key in damaged if key.endswith("/cleanup.json")))
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=damaged).status != "completed"
    damaged = dict(captured)
    owner_path = next(key for key in damaged if key.endswith("/resource-owner-data.json"))
    damaged[owner_path] += b" "
    with pytest.raises(ValueError):
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=damaged)
    cleaned = _execute(case, cleanup_id, cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not resource.exists()
    assert all(item.read_bytes() == raw_bytes for item, raw_bytes in originals.items())
    assert not execution._historical_resource_cycles(root, plan)[1]


@pytest.mark.parametrize("cycle", ["initial", "reset"])
@pytest.mark.parametrize("fault", ["first-marker", "second-claim-original"])
def test_mvp_partial_claim_failure_cleans_only_confirmed_owned_resources(
    execution_case, monkeypatch, cycle, fault
):
    case, left, right = _case_with_two_separate_resources(_case_with_frozen_resets(execution_case))
    root, _, plan = case
    step_id = "current-none"
    if cycle == "reset":
        for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
            assert _execute(case, identifier).status == "completed"
        assert not left.exists() and not right.exists()
        step_id = "current-reset"
    right_before = {p.name: p.read_bytes() for p in right.iterdir()} if right.exists() else None
    original_write = LoopArtifactStore.write_bytes_artifact
    failures = []

    def fail_after_exact_publication(store, path, content, **kwargs):
        result = original_write(store, path, content, **kwargs)
        target = (path == left / ".ai-sdlc-owner.json") if fault == "first-marker" else path.name == "resource-owner-second-resource.json"
        if target and not failures:
            failures.append({"path": str(path), "bytes": path.read_bytes()})
            raise OSError("one-shot partial claim publication failure")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_after_exact_publication)
        receipt = _execute(case, step_id)
    assert len(failures) == 1
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert receipt.exit_code is None and not left.exists()
    if fault == "first-marker":
        assert ({p.name: p.read_bytes() for p in right.iterdir()} if right.exists() else None) == right_before
    else:
        assert not right.exists()
    folder = (root / receipt.attempt_ref.path).parent
    assert not (folder / "process.json").exists() and not (folder / "postcheck.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started"
    assert "partial claim publication" in raw["launch_error"]
    manifest = json.loads((folder / "resource-ownership.json").read_bytes())
    assert manifest["prelaunch_cleanup"] is True
    assert set(manifest["resources"]) == ({"data"} if fault == "first-marker" else {"data", "second-resource"})
    cleanup = json.loads((folder / "resource-cleanup.json").read_bytes())
    assert {r["id"] for r in cleanup["resources"]} == set(manifest["resources"])
    assert cleanup["status"] == "complete"
    completion = json.loads((folder / "completion.json").read_bytes())
    assert completion["artifact_sha256"]["resource-cleanup.json"] == hashlib.sha256((folder / "resource-cleanup.json").read_bytes()).hexdigest()
    originals = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    assert receipt in execution._recover_plan_attempts(root, plan)
    history, debts = execution._historical_resource_cycles(root, plan)
    assert history[-1][3] == receipt and not debts
    actual_receipts = execution._recover_plan_attempts(root, plan)
    assert not execution._subject_resources_active(plan, actual_receipts, "current")
    cleanup_step = "current-cleanup" if cycle == "initial" else "r2-current-cleanup"
    assert cleanup_step in execution._blocked_business_steps(plan, actual_receipts)
    impl = _started_history_input(case, monkeypatch)
    progress = SimpleNamespace(tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])])
    blockers, advisories, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False,
        active_plan_digest=counterexample_digest(plan),
    )
    captured_state = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    cold_blockers, cold_advisories, _ = execution.counterexample_verification_state(
        root, impl, progress, captured_artifacts=captured_state,
        require_r2=False, require_completion=False, active_plan_digest=counterexample_digest(plan),
    )
    assert not blockers and not cold_blockers
    assert not any("resource-cycle" in item or "cleanup-required" in item for item in advisories + cold_advisories)
    observed = execution._bundle_from_attempts(root, case[1], plan, [receipt], captured_artifacts=captured)
    assert observed.attempts == (receipt,) and not observed.acceptance
    assert _execute(case, step_id) == receipt
    for filename in ("completion.json", "resource-cleanup.json", "raw-result.json", "resource-owner-data.json"):
        bad = dict(captured)
        key = next(name for name in bad if name.endswith("/" + filename))
        del bad[key]
        try:
            rejected = execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=bad)
        except (ValueError, FileNotFoundError):
            continue
        assert rejected.status == "execution_unknown" and rejected.cleanup_status == "unknown"
    bad = dict(captured)
    key = next(name for name in bad if name.endswith("/resource-cleanup.json"))
    bad[key] += b" "
    with pytest.raises(ValueError, match="completion-originals"):
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=bad)
    # reset 历史含旧 cleanup 的诊断原件；本断言只损坏当前失败尝试已绑定的清理证明。
    cleanup_path = (folder / "resource-cleanup.json").relative_to(root).as_posix()
    assert completion["artifact_sha256"]["resource-cleanup.json"] == hashlib.sha256(captured_state[cleanup_path]).hexdigest()
    for mutate in ("missing", "corrupt"):
        damaged_state = dict(captured_state)
        if mutate == "missing":
            del damaged_state[cleanup_path]
        else:
            damaged_state[cleanup_path] += b" "
        damaged_blockers, _, _ = execution.counterexample_verification_state(
            root, impl, progress, captured_artifacts=damaged_state,
            require_r2=False, require_completion=False,
            active_plan_digest=counterexample_digest(plan),
        )
        assert damaged_blockers
    assert all(path.read_bytes() == content for path, content in originals.items())



def test_mvp_persistent_ownership_publication_failure_remains_unknown(
    execution_case, monkeypatch
):
    root, _, plan = execution_case
    original_write = execution._write_json
    failures = []

    def unavailable_manifest(evidence_root, path, payload):
        if path.name == "resource-ownership.json":
            failures.append(path)
            raise OSError("ownership storage remains unavailable")
        return original_write(evidence_root, path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_write_json", unavailable_manifest)
        receipt = _execute(execution_case)
    assert len(failures) == 2
    assert receipt.status == "execution_unknown" and receipt.cleanup_status == "unknown"
    folder = (root / receipt.attempt_ref.path).parent
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert resource.is_dir() and (resource / ".ai-sdlc-owner.json").is_file()
    assert not (folder / "process.json").exists()
    assert not (folder / "resource-ownership.json").exists()
    assert not (folder / "completion.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and "storage remains unavailable" in raw["launch_error"]
    preserved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="prior-execution-unknown"):
        _execute(execution_case, "current-cleanup", cleanup_recovery=True)
    assert all(p.read_bytes() == content for p, content in preserved.items())
    assert resource.is_dir()


@pytest.mark.parametrize("fault,cleanup_fault,interrupt", [
    *[(fault, cleanup_fault, False)
      for fault in ("initial-bytes", "owner-original")
      for cleanup_fault in ("none", "remove-fails", "identity-changed")],
    ("initial-bytes", "none", True),
    ("initial-bytes", "remove-fails", True),
])
def test_mvp_reset_partial_rebuild_cleanup_before_claim(
    execution_case, monkeypatch, fault, cleanup_fault, interrupt
):
    case = _case_with_frozen_resets(execution_case)
    root, _, plan = case
    for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
        assert _execute(case, identifier).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert not resource.exists()
    write = LoopArtifactStore.write_bytes_artifact
    remove = execution._remove_owned_directory
    failures, removals = [], []
    failure_type = KeyboardInterrupt if interrupt else OSError
    primary = failure_type("one-shot early reset write failure")

    def fail_before_publication(store, path, content, **kwargs):
        target = (path.parent == resource) if fault == "initial-bytes" else path.name == "resource-owner-data.json"
        if target and not failures:
            assert resource.is_dir()
            assert not path.exists()
            failures.append((str(path), execution._directory_identity(resource)))
            if cleanup_fault == "identity-changed":
                resource.rename(resource.with_name("displaced-unpublished-resource"))
                resource.mkdir()
                (resource / "other.txt").write_bytes(b"must preserve foreign identity")
            raise primary
        return write(store, path, content, **kwargs)

    def checked_remove(path, identity):
        removals.append((path, dict(identity)))
        if cleanup_fault == "remove-fails" and path == resource:
            raise OSError("known partial directory cleanup unavailable")
        return remove(path, identity)

    with monkeypatch.context() as patch:
        patch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_before_publication)
        patch.setattr(execution, "_remove_owned_directory", checked_remove)
        if interrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                _execute(case, "current-reset")
            assert caught.value is primary
            assert "fail_before_publication" in [entry.name for entry in caught.traceback]
            attempts = [path for path in execution._attempts_dir(root, plan).glob("*/intent.json")
                        if json.loads(path.read_bytes())["step_id"] == "current-reset"]
            assert len(attempts) == 1
            receipt = execution.recover_counterexample_attempt(root, plan, execution._ref(root, attempts[0]))
        else:
            receipt = _execute(case, "current-reset")
    assert len(failures) == 1
    folder = (root / receipt.attempt_ref.path).parent
    assert not (folder / "process.json").exists()
    assert not (folder / "resource-owner-data.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started"
    assert str(primary) in raw["launch_error"]
    if cleanup_fault == "remove-fails":
        assert any("OSError: known partial directory cleanup unavailable" in note
                   for note in getattr(primary, "__notes__", []))
    assert raw["exit_code"] is None and not receipt.normally_completed
    preserved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    attempt_paths = set(execution._attempts_dir(root, plan).iterdir())
    if cleanup_fault != "none":
        assert receipt.status == "execution_unknown" and receipt.cleanup_status == "unknown"
        assert resource.exists() and not (folder / "completion.json").exists()
        if cleanup_fault == "identity-changed":
            assert (resource / "other.txt").read_bytes() == b"must preserve foreign identity"
            assert resource.with_name("displaced-unpublished-resource").is_dir()
        with pytest.raises(ValueError, match="prior-execution-unknown"):
            _execute(case, "r2-current-cleanup", cleanup_recovery=True)
        assert set(execution._attempts_dir(root, plan).iterdir()) == attempt_paths
        assert all(p.read_bytes() == raw_bytes for p, raw_bytes in preserved.items())
        return
    assert receipt.status == ("execution_unknown" if interrupt else "infrastructure_error")
    assert receipt.cleanup_status == "complete"
    assert "early reset write failure" in raw["launch_error"]
    assert not resource.exists() and len(removals) == 1
    assert removals[0][1] == failures[0][1]
    manifest = json.loads((folder / "resource-ownership.json").read_bytes())
    assert manifest["prelaunch_cleanup"] is True and manifest["resources"] == {}
    if interrupt:
        # 实际清理完成不代表业务完成；未知尝试仍由原历史入口拒绝重放。
        with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
            execution._historical_resource_cycles(root, plan)
        with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
            execution._recover_plan_attempts(root, plan)
        with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
            _execute(case, "current-reset")
    else:
        assert not execution._historical_resource_cycles(root, plan)[1]
        assert not execution._subject_resources_active(plan, execution._recover_plan_attempts(root, plan), "current")
        assert _execute(case, "current-reset") == receipt
    assert set(execution._attempts_dir(root, plan).iterdir()) == attempt_paths
    assert all(p.read_bytes() == raw_bytes for p, raw_bytes in preserved.items())


@pytest.mark.parametrize("transfer,interrupt", [
    ("before-record", False), ("after-record", False), ("partial-initial", False),
    ("before-record", True), ("after-record", True), ("partial-initial", True),
])
def test_mvp_reset_rebuild_transfers_cleanup_once(execution_case, monkeypatch, transfer, interrupt):
    case = _case_with_frozen_resets(execution_case)
    root, _, plan = case
    for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
        assert _execute(case, identifier).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert not resource.exists()
    validate, write, remove = execution._validate_resources, LoopArtifactStore.write_bytes_artifact, execution._remove_owned_directory
    transfers, removals, writes = [], [], []
    primary = (KeyboardInterrupt if interrupt else OSError)(
        "initial content write completed before reported failure"
        if transfer == "partial-initial"
        else "claim callback failed at the selected transfer boundary"
    )

    def fail_transfer(*args, **kwargs):
        on_claim = kwargs.get("on_claim")
        if on_claim is not None:
            def interrupted(identifier, original):
                transfers.append(original)
                assert (root / original.path).is_file()
                if transfer == "after-record":
                    on_claim(identifier, original)
                raise primary
            kwargs["on_claim"] = interrupted
        return validate(*args, **kwargs)

    def fail_after_initial_bytes(store, path, content, **kwargs):
        result = write(store, path, content, **kwargs)
        if path.parent == resource and not writes:
            writes.append(path)
            assert path.read_bytes() == content
            raise primary
        return result

    def count_removal(path, expected):
        removals.append((path, dict(expected)))
        return remove(path, expected)

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_remove_owned_directory", count_removal)
        if transfer == "partial-initial":
            patch.setattr(LoopArtifactStore, "write_bytes_artifact", fail_after_initial_bytes)
        else:
            patch.setattr(execution, "_validate_resources", fail_transfer)
        if interrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                _execute(case, "current-reset")
            assert caught.value is primary
            origin = "fail_after_initial_bytes" if transfer == "partial-initial" else "interrupted"
            assert origin in [entry.name for entry in caught.traceback]
            attempts = [path for path in execution._attempts_dir(root, plan).glob("*/intent.json")
                        if json.loads(path.read_bytes())["step_id"] == "current-reset"]
            assert len(attempts) == 1
            receipt = execution.recover_counterexample_attempt(root, plan, execution._ref(root, attempts[0]))
        else:
            receipt = _execute(case, "current-reset")
    folder = (root / receipt.attempt_ref.path).parent
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and not (folder / "process.json").exists()
    if interrupt:
        assert str(primary) in raw["launch_error"]
        assert raw["exit_code"] is None and not receipt.normally_completed
    preserved = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    attempt_paths = set(execution._attempts_dir(root, plan).iterdir())
    if transfer == "before-record":
        assert len(transfers) == 1 and not removals and resource.is_dir()
        assert receipt.status == "execution_unknown" and receipt.cleanup_status == "unknown"
        assert (root / transfers[0].path).is_file() and not (folder / "completion.json").exists()
        with pytest.raises(ValueError, match="prior-execution-unknown"):
            _execute(case, "r2-current-cleanup", cleanup_recovery=True)
    else:
        assert len(removals) == 1 and not resource.exists()
        assert receipt.status == ("execution_unknown" if interrupt else "infrastructure_error")
        assert receipt.cleanup_status == "complete"
        assert len(writes if transfer == "partial-initial" else transfers) == 1
        if interrupt:
            # on_claim 已记录后只由上层清理一次；原中断仍不能变成可重放的完成尝试。
            with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
                execution._historical_resource_cycles(root, plan)
            with pytest.raises(ValueError, match="prior-execution-unknown-no-replay"):
                _execute(case, "current-reset")
        else:
            assert not execution._historical_resource_cycles(root, plan)[1]
    assert set(execution._attempts_dir(root, plan).iterdir()) == attempt_paths
    assert all(p.read_bytes() == raw_bytes for p, raw_bytes in preserved.items())


@pytest.mark.parametrize("subject_id", ["current", "variant"])
@pytest.mark.parametrize("damage", ["initial", "git-metadata"])
@pytest.mark.parametrize("entry", ["native", "step"])
def test_new_plan_all_resource_admission_precedes_any_publication(
    execution_case, monkeypatch, subject_id, damage, entry
):
    root, _, _ = execution_case
    contract, impl, loop, plan = _run_admission_case(
        _case_with_frozen_resets(execution_case), monkeypatch
    )
    target_step = next(step for step in plan.steps if step.subject_id == subject_id)
    project = Path(target_step.binding.project_root)
    resource = Path(target_step.binding.resources[0].root)
    if damage == "initial":
        (resource / "result.json").write_bytes(b'{"value":"unexpected"}')
        expected = "counterexample-resource-initial-state-mismatch"
    elif damage == "git-metadata":
        _git(project, "init", "--quiet", "--separate-git-dir", str(resource / "actual-git-metadata"))
        expected = "counterexample-resource-root-overlaps-protected-path"
    folder = execution._attempts_dir(root, plan).parent
    proposal = folder / "resource-admission-proposal.json"
    proposal.write_bytes(execution._json_bytes(plan))
    original = {str(p): p.read_bytes() for step in plan.steps for item in step.binding.resources
                for p in Path(item.root).rglob("*") if p.is_file()}
    writer = execution._write_json
    def forbid_publication(evidence_root, path, value):
        if path.parent.name == "plans" or path.name == "intent.json":
            pytest.fail("invalid resource reached immutable plan or attempt publication")
        return writer(evidence_root, path, value)
    monkeypatch.setattr(execution, "_write_json", forbid_publication)
    with pytest.raises(ValueError, match=expected):
        if entry == "native":
            execution.run_counterexample_plan(root, impl, loop, plan.task_id, proposal.relative_to(root).as_posix())
        else:
            _execute((root, contract, plan))
    assert not list(folder.glob("plans/*.json"))
    assert not list(folder.glob("attempts/*/intent.json"))
    assert all(Path(name).read_bytes() == content for name, content in original.items())
    assert all(not (Path(item.root) / ".ai-sdlc-owner.json").exists()
               for step in plan.steps for item in step.binding.resources)


@pytest.mark.parametrize("subject_id", ["current", "variant"])
def test_new_plan_shared_admission_requires_each_resource_root_ignored(
    execution_case, subject_id
):
    root, _, plan = _case_with_frozen_resets(execution_case)
    step = next(item for item in plan.steps if item.subject_id == subject_id)
    project = Path(step.binding.project_root)
    # 直接核对共享资源边界；不把已改忽略配置的候选声明为完整 native 输入。
    (project / ".gitignore").write_text("")
    with pytest.raises(ValueError, match="counterexample-resource-must-be-explicitly-ignored-scratch"):
        execution._validate_plan_resource_admission(
            root, plan, new_execution=True,
            deadline_ms=time.time_ns() // 1_000_000 + 10_000,
        )
    assert not list(execution._attempts_dir(root, plan).parent.glob("plans/*.json"))
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    assert all(not (Path(item.root) / ".ai-sdlc-owner.json").exists()
               for declared in plan.steps for item in declared.binding.resources)


def test_mvp_cleanup_prelaunch_failure_consumers_keep_actual_cleanup(execution_case, monkeypatch):
    root, _, plan = execution_case
    for identifier in ("current-none", "current-V0", "current-V1"):
        assert _execute(execution_case, identifier).status == "completed"
    resource = Path(plan.steps[0].binding.resources[0].root)
    assert resource.is_dir()
    validate = execution._validate_resources
    failures = []

    def interrupted_prepare(*args, **kwargs):
        if kwargs.get("claim") is True and args[3].kind == "cleanup" and not failures:
            failures.append(args[3].id)
            assert kwargs["ownership_refs"]
            raise OSError("cleanup preparation one-shot failure before provider start")
        return validate(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_validate_resources", interrupted_prepare)
        receipt = _execute(execution_case, "current-cleanup")
    assert failures == ["current-cleanup"]
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert not resource.exists()
    folder = (root / receipt.attempt_ref.path).parent
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and not (folder / "process.json").exists()
    assert "cleanup preparation one-shot" in raw["launch_error"]
    original_bytes = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    steps = {step.id: step for step in plan.steps}
    assert execution._resource_cleanup_completed(receipt, steps[receipt.step_id])
    assert execution._failed_operation(receipt, steps[receipt.step_id])
    impl = _started_history_input(execution_case, monkeypatch)
    progress = SimpleNamespace(tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])])
    blockers, advisories, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False,
        active_plan_digest=counterexample_digest(plan),
    )
    captured_state = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    cold_blockers, cold_advisories, _ = execution.counterexample_verification_state(
        root, impl, progress, captured_artifacts=captured_state,
        require_r2=False, require_completion=False, active_plan_digest=counterexample_digest(plan),
    )
    assert not blockers and not cold_blockers
    assert not any("resource-cycle" in item or "cleanup-required" in item for item in advisories + cold_advisories)
    # 原失败仍是失败；只有已发生的清理事实消除后续消费者中的资源债务。
    history, debts = execution._historical_resource_cycles(root, plan)
    assert history[-1][3] == receipt and not debts
    captured_history, captured_debts = execution._historical_resource_cycles(
        root, plan, captured_artifacts=captured_state,
    )
    assert captured_history[-1][3] == receipt and not captured_debts
    assert not execution._subject_resources_active(plan, execution._recover_plan_attempts(root, plan), "current")
    assert _execute(execution_case, "current-cleanup") == receipt
    bad = dict(captured_state)
    key = (folder / "resource-cleanup.json").relative_to(root).as_posix()
    del bad[key]
    bad_blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, captured_artifacts=bad,
        require_r2=False, require_completion=False, active_plan_digest=counterexample_digest(plan),
    )
    assert bad_blockers
    assert all(path.read_bytes() == content for path, content in original_bytes.items())


@pytest.mark.parametrize("cycle,damage", [("initial", None), ("reset", None), ("initial", "owner-conflict"), ("initial", "cleanup-reserve-exhausted")])
def test_partial_claim_expiry_uses_only_current_cleanup_reserve(
    execution_case, monkeypatch, cycle, damage
):
    case, left, right = _case_with_two_separate_resources(_case_with_frozen_resets(execution_case))
    root, contract, plan = case
    step_id = "current-none" if cycle == "initial" else "current-reset"
    data = plan.model_dump(mode="json")
    next(step for step in data["steps"] if step["id"] == step_id)["binding"]["timeout_seconds"] = 1
    if damage == "cleanup-reserve-exhausted":
        next(step for step in data["steps"] if step["id"] == step_id)["reservation_seconds"] = 0
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    if cycle == "reset":
        for identifier in ("current-none", "current-V0", "current-V1", "current-cleanup"):
            assert _execute(case, identifier).status == "completed"
        assert not left.exists() and not right.exists()
    right_before = {p.name: p.read_bytes() for p in right.iterdir()} if right.exists() else None
    step = next(item for item in plan.steps if item.id == step_id)
    # 使用公开参数预留后续工作，真实等待本步截止；不伪造时钟、claim 或清理回执。
    future_reserve = 2_000
    deadline = time.time_ns() // 1_000_000 + 1000 * (future_reserve + step.reservation_seconds + 5 + 3)
    if cycle == "reset":
        prior_deadlines = [json.loads(path.read_bytes())["deadline_ms"]
                           for path in execution._attempts_dir(root, plan).glob("*/intent.json")]
        assert prior_deadlines and deadline < min(prior_deadlines)
    original_validate = execution._validate_resource
    calls, claims = [], []

    def validate(evidence_root, project, active_plan, active_step, resource, **kwargs):
        event = {"resource": resource.id, "claim": kwargs["claim"],
                 "deadline_ms": kwargs["deadline_ms"], "called_ms": time.time_ns() // 1_000_000}
        calls.append(event)
        try:
            reference = original_validate(evidence_root, project, active_plan, active_step, resource, **kwargs)
        except ValueError as error:
            event["error"] = str(error)
            raise
        if kwargs["claim"] and resource.id == "data" and not claims:
            marker = left / ".ai-sdlc-owner.json"
            assert reference is not None and marker.is_file()
            original = (root / reference.path).read_bytes()
            assert hashlib.sha256(original).hexdigest() == reference.sha256
            claim = {"reference": reference.model_dump(mode="json"), "bytes_hex": original.hex(),
                     "marker_hex": marker.read_bytes().hex(), "state_deadline": kwargs["deadline_ms"]}
            claims.append(claim)
            if damage == "owner-conflict":
                owner = json.loads(marker.read_bytes())
                owner["resource_id"] = "unrelated-resource"
                marker.write_text(json.dumps(owner))
            delay = (kwargs["deadline_ms"] - time.time_ns() // 1_000_000) / 1000
            assert 0 < delay < 3
            time.sleep(delay + 0.005)
            claim["resumed_ms"] = time.time_ns() // 1_000_000
        return reference

    started = time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(execution, "_validate_resource", validate)
        receipt = _execute(case, step_id, deadline_ms=deadline, remaining_reserve_seconds=future_reserve)
    elapsed = time.monotonic() - started
    folder = (root / receipt.attempt_ref.path).parent
    observed = {"receipt": receipt.model_dump(mode="json"), "calls": calls, "claims": claims,
                "elapsed": elapsed, "deadline_ms": deadline, "left_exists": left.exists(),
                "right_exists": right.exists(), "postcheck_error": json.loads((folder / "postcheck-error.json").read_bytes())}
    (folder / "partial-claim-deadline-observed.json").write_text(json.dumps(observed, indent=2))
    assert len(claims) == 1
    assert claims[0]["resumed_ms"] >= claims[0]["state_deadline"]
    assert any(call["resource"] == "second-resource" and call["claim"] and call.get("error") == "counterexample-resource-state-deadline-exceeded" for call in calls)
    intent = json.loads((folder / "intent.json").read_bytes())
    assert intent["deadline_ms"] == deadline and intent["reserved_seconds"] == future_reserve
    cleanup_deadline = deadline - 1000 * (future_reserve + 5)
    assert claims[0]["state_deadline"] < time.time_ns() // 1_000_000
    if damage != "cleanup-reserve-exhausted":
        assert time.time_ns() // 1_000_000 < cleanup_deadline
    else:
        assert time.time_ns() // 1_000_000 >= cleanup_deadline
    assert not (folder / "process.json").exists()
    assert (folder / "stdout").read_bytes() == (folder / "stderr").read_bytes() == b""
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and raw["exit_code"] is None
    if damage:
        assert receipt.status == "execution_unknown" and receipt.cleanup_status == "unknown"
        assert left.exists() and not (folder / "completion.json").exists()
        expected = "counterexample-resource-already-owned" if damage == "owner-conflict" else "counterexample-resource-state-deadline-exceeded"
        assert any(call["resource"] == "data" and not call["claim"] and call.get("error") == expected for call in calls)
        preserved = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
        with pytest.raises(ValueError, match="prior-execution-unknown"):
            execution._historical_resource_cycles(root, plan)
        assert all(path.read_bytes() == content for path, content in preserved.items())
        return
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert not left.exists()
    assert ({p.name: p.read_bytes() for p in right.iterdir()} if right.exists() else None) == right_before
    for resource_id in ("data", "second-resource"):
        assert any(call["resource"] == resource_id and not call["claim"] and call["deadline_ms"] == cleanup_deadline and "error" not in call for call in calls)
    assert "resource-state-deadline-exceeded" in observed["postcheck_error"]["error"]
    completion = json.loads((folder / "completion.json").read_bytes())
    assert completion["artifact_sha256"]["resource-cleanup.json"] == hashlib.sha256((folder / "resource-cleanup.json").read_bytes()).hexdigest()
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    assert receipt in execution._recover_plan_attempts(root, plan)
    history, debts = execution._historical_resource_cycles(root, plan)
    assert history[-1][3] == receipt and not debts
    assert not execution._subject_resources_active(plan, execution._recover_plan_attempts(root, plan), "current")
    impl = _started_history_input(case, monkeypatch)
    progress = SimpleNamespace(tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])])
    blockers, _, refs = execution.counterexample_verification_state(root, impl, progress,
        require_r2=False, require_completion=False, active_plan_digest=counterexample_digest(plan))
    cold = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    cold_blockers, _, _ = execution.counterexample_verification_state(root, impl, progress,
        captured_artifacts=cold, require_r2=False, require_completion=False, active_plan_digest=counterexample_digest(plan))
    assert not blockers and not cold_blockers
    damaged = dict(captured)
    damaged[(folder / "resource-cleanup.json").relative_to(root).as_posix()] += b" "
    with pytest.raises(ValueError, match="completion-originals"):
        execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=damaged)
    assert all(path.read_bytes() == content for path, content in originals.items())



def test_mvp_prelaunch_cwd_validation_failure_keeps_cold_cleanup_path(
    execution_case, monkeypatch
):
    root, contract, original_plan = _case_with_frozen_resets(execution_case)
    data = original_plan.model_dump(mode="json")
    first = next(step for step in data["steps"] if step["id"] == "current-none")
    project = Path(first["binding"]["project_root"])
    first["binding"]["cwd"] = "tests"
    first["binding"]["argv"][1] = str(project / "observe.py")
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    resource = Path(plan.steps[0].binding.resources[0].root)
    cwd, saved_cwd = project / "tests", project / "saved-tests"
    original_write = execution._write_json
    failures = []

    def replace_cwd_after_real_claim(evidence_root, path, payload):
        result = original_write(evidence_root, path, payload)
        if path.name == "resource-ownership.json" and not failures:
            marker = json.loads((resource / ".ai-sdlc-owner.json").read_bytes())
            claim = marker["ownership_ref"]
            assert hashlib.sha256((root / claim["path"]).read_bytes()).hexdigest() == claim["sha256"]
            failures.append(path.read_bytes())
            # 准入时是合法目录；认领原件发布后才实际替换，命中启动前 cwd 校验。
            cwd.rename(saved_cwd)
            cwd.write_bytes(b"cwd is no longer a directory")
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(execution, "_write_json", replace_cwd_after_real_claim)
            receipt = _execute(case)
    finally:
        if saved_cwd.exists():
            cwd.unlink()
            saved_cwd.rename(cwd)
    assert len(failures) == 1
    assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "complete"
    assert receipt.exit_code is None and not receipt.normally_completed
    folder = (root / receipt.attempt_ref.path).parent
    assert (folder / "resource-ownership.json").read_bytes() == failures[0]
    assert not (folder / "process.json").exists() and not (folder / "postcheck.json").exists()
    raw = json.loads((folder / "raw-result.json").read_bytes())
    assert raw["launch_status"] == "never_started" and raw["exit_code"] is None
    assert "counterexample-current-cwd-directory-required" in raw["launch_error"]
    error = json.loads((folder / "postcheck-error.json").read_bytes())
    assert "counterexample-current-cwd-directory-required" in str(error)
    assert (folder / "stdout").read_bytes() == (folder / "stderr").read_bytes() == b""
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    captured = {
        ref.path: (root / ref.path).read_bytes()
        for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)
    }
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=captured
    ) == receipt
    assert receipt in execution._recover_plan_attempts(root, plan)
    assert _execute(case) == receipt
    assert execution._historical_resource_cycles(root, plan)[1]
    damaged = dict(captured)
    key = next(path for path in damaged if path.endswith("/raw-result.json"))
    del damaged[key]
    incomplete = execution.recover_counterexample_attempt(
        root, plan, receipt.attempt_ref, captured_artifacts=damaged
    )
    # 捕获缺件按既有合同保留未知；不能从仍完整的现场补齐原件。
    assert incomplete.status == "execution_unknown" and incomplete.cleanup_status == "unknown"
    cleaned = _execute(case, "current-cleanup", cleanup_recovery=True)
    assert cleaned.status == "completed" and cleaned.cleanup_status == "complete"
    assert not resource.exists()
    assert not execution._historical_resource_cycles(root, plan)[1]
    assert all(path.read_bytes() == content for path, content in originals.items())


def _delay_real_postcommand_digest(monkeypatch, *, deadline_ms, delay_until_ms):
    from ai_sdlc.core import quality_command

    original_process = quality_command.run_controlled_process
    original_digest = quality_command.build_source_digest
    pending, events = [], []

    def process(options):
        result = original_process(options)
        assert options.controlled is not None
        folder = options.controlled.stdout_path.parent
        raw = json.loads((folder / "raw-result.json").read_bytes())
        cleanup = json.loads((folder / "cleanup.json").read_bytes())
        assert raw["exit_code"] == 0 and not raw["timed_out"]
        assert raw["ended_at_ms"] < deadline_ms
        assert cleanup["status"] == "complete"
        assert options.controlled.deadline_ms == deadline_ms
        pending.append({"raw_ended_at_ms": raw["ended_at_ms"], "folder": str(folder)})
        return result

    def digest(root, **kwargs):
        value = original_digest(root, **kwargs)
        if pending:
            event = pending.pop()
            event["digest_ready_ms"] = time.time_ns() // 1_000_000
            assert event["digest_ready_ms"] < deadline_ms
            # 只延迟真实命令后的摘要收尾；原时钟、命令结果与完成原件均由产品生成。
            if delay_until_ms is not None:
                time.sleep(max(0, (delay_until_ms - time.time_ns() // 1_000_000) / 1000) + 0.03)
            event["digest_returned_ms"] = time.time_ns() // 1_000_000
            events.append(event)
        return value

    monkeypatch.setattr(quality_command, "run_controlled_process", process)
    monkeypatch.setattr(quality_command, "build_source_digest", digest)
    return events


@pytest.mark.parametrize("timing", ["normal", "state-cutoff-crossed", "cleanup-reserve-exhausted"])
def test_postcommand_cleanup_uses_only_current_reserve(execution_case, monkeypatch, timing):
    root, contract, original_plan = execution_case
    data = original_plan.model_dump(mode="json")
    entry = next(step for step in data["steps"] if step["id"] == "current-cleanup")
    entry["binding"]["timeout_seconds"] = 1
    entry["reservation_seconds"] = 2
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    for identifier in ("current-none", "current-V0", "current-V1"):
        assert _execute(case, identifier).normally_completed
    future_reserve = 2_000
    deadline = time.time_ns() // 1_000_000 + 1000 * (future_reserve + 2 + 5 + 3)
    state_deadline = deadline - 1000 * (future_reserve + 2 + 5)
    cleanup_deadline = deadline - 1000 * (future_reserve + 5)
    delay_until = {"normal": None, "state-cutoff-crossed": state_deadline,
                   "cleanup-reserve-exhausted": cleanup_deadline}[timing]
    resource = Path(entry["binding"]["resources"][0]["root"])
    with monkeypatch.context() as patch:
        events = _delay_real_postcommand_digest(patch, deadline_ms=state_deadline, delay_until_ms=delay_until)
        receipt = _execute(case, "current-cleanup", deadline_ms=deadline, remaining_reserve_seconds=future_reserve)
    assert len(events) == 1
    event = events[0]
    if timing == "normal":
        assert event["digest_returned_ms"] < state_deadline
    elif timing == "state-cutoff-crossed":
        assert state_deadline <= event["digest_returned_ms"] < cleanup_deadline
    else:
        assert event["digest_returned_ms"] >= cleanup_deadline
    folder = (root / receipt.attempt_ref.path).parent
    intent = json.loads((folder / "intent.json").read_bytes())
    assert intent["deadline_ms"] == deadline and intent["reserved_seconds"] == future_reserve
    (root.parent / "postcommand-deadline-observed.json").write_text(json.dumps({
        "timing": timing, "events": events, "state_deadline": state_deadline,
        "cleanup_deadline": cleanup_deadline, "deadline": deadline,
        "receipt": receipt.model_dump(mode="json"), "resource_exists": resource.exists(),
    }, indent=2))
    original_bytes = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    captured = {ref.path: (root / ref.path).read_bytes()
                for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref) == receipt
    assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
    attempts_before = tuple(sorted(execution._attempts_dir(root, plan).glob("*/intent.json")))
    if timing == "cleanup-reserve-exhausted":
        assert receipt.status == "infrastructure_error" and receipt.cleanup_status == "incomplete"
        assert resource.exists() and not (folder / "resource-cleanup.json").exists()
        for consumer in (lambda: execution._historical_resource_cycles(root, plan),
                         lambda: _execute(case, "current-cleanup")):
            with pytest.raises(ValueError, match="counterexample-prior-execution-unknown-no-replay"):
                consumer()
    else:
        assert receipt.normally_completed and receipt.exit_code == 0
        assert not resource.exists() and (folder / "resource-cleanup.json").is_file()
        history, debts = execution._historical_resource_cycles(root, plan)
        assert history[-1][3] == receipt and not debts
        assert not execution._subject_resources_active(plan, execution._recover_plan_attempts(root, plan), "current")
        assert _execute(case, "current-cleanup") == receipt
    impl = _started_history_input(case, monkeypatch)
    progress = SimpleNamespace(tasks=[SimpleNamespace(task_id=plan.task_id, counterexample_results=[])])
    blockers, _, refs = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False, require_completion=False,
        active_plan_digest=counterexample_digest(plan),
    )
    cold = {ref.path: (root / ref.path).read_bytes() for ref in refs}
    cold_blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, captured_artifacts=cold, require_r2=False,
        require_completion=False, active_plan_digest=counterexample_digest(plan),
    )
    assert bool(blockers) == bool(cold_blockers) == (timing == "cleanup-reserve-exhausted")
    if timing != "cleanup-reserve-exhausted":
        missing = dict(captured)
        del missing[(folder / "resource-cleanup.json").relative_to(root).as_posix()]
        incomplete = execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=missing)
        assert not incomplete.normally_completed and incomplete.cleanup_status == "incomplete"
    assert tuple(sorted(execution._attempts_dir(root, plan).glob("*/intent.json"))) == attempts_before
    assert all(path.read_bytes() == content for path, content in original_bytes.items())


def test_postcommand_observe_acceptance_reset_share_current_reserve(execution_case, monkeypatch):
    root, contract, original_plan = _case_with_frozen_resets(execution_case)
    data = original_plan.model_dump(mode="json")
    identifiers = ("current-none", "current-V0", "current-V1", "current-cleanup", "current-reset")
    for step in data["steps"]:
        if step["id"] in identifiers:
            step["binding"]["timeout_seconds"] = 1
            step["reservation_seconds"] = 2
    plan = CounterexamplePlan.model_validate(data)
    case = root, contract, plan
    future_reserve = 2_000
    deadline = time.time_ns() // 1_000_000 + 1000 * (future_reserve + 2 + 5 + 3 * len(identifiers))
    observed, originals = [], {}
    for index, identifier in enumerate(identifiers):
        reserve = future_reserve + 3 * (len(identifiers) - index - 1)
        state_deadline = deadline - 1000 * (reserve + 2 + 5)
        cleanup_deadline = deadline - 1000 * (reserve + 5)
        with monkeypatch.context() as patch:
            events = _delay_real_postcommand_digest(patch, deadline_ms=state_deadline, delay_until_ms=state_deadline)
            receipt = _execute(case, identifier, deadline_ms=deadline, remaining_reserve_seconds=reserve)
        assert len(events) == 1
        assert state_deadline <= events[0]["digest_returned_ms"] < cleanup_deadline
        observed.append({"step": identifier, "state_deadline": state_deadline,
                         "cleanup_deadline": cleanup_deadline, "events": events,
                         "receipt": receipt.model_dump(mode="json")})
        (root.parent / "postcommand-business-sequence-observed.json").write_text(json.dumps(observed, indent=2))
        assert receipt.normally_completed and receipt.exit_code == 0
        folder = (root / receipt.attempt_ref.path).parent
        originals.update({path: path.read_bytes() for path in folder.iterdir() if path.is_file()})
        captured = {ref.path: (root / ref.path).read_bytes()
                    for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
        if identifier == "current-reset":
            with pytest.raises(ValueError, match="counterexample-captured-evidence-missing"):
                execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured)
            for reference in execution.resource_initial_artifact_refs(
                root, plan.steps[0].binding.resources[0].initial_state
            ):
                captured[reference.path] = (root / reference.path).read_bytes()
        assert execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured) == receipt
        if identifier == "current-reset":
            reset_state = json.loads((folder / "reset-state.json").read_bytes())
            assert reset_state["expected"] == reset_state["actual"]
        elif identifier != "current-cleanup":
            resource_state = json.loads((folder / "resource-state.json").read_bytes())
            assert resource_state["before"] == resource_state["after"]
    # 已证明的清理才可接续 reset；保留历史完成原件与正常重建后的真实资源债务。
    history, debts = execution._historical_resource_cycles(root, plan)
    assert history[-1][3] == receipt and debts
    assert all(path.read_bytes() == content for path, content in originals.items())

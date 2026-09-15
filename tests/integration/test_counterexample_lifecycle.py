"""正常新任务的真实进程链；人工样例/合成评审仅证明机制和集成。"""

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_sdlc.core import counterexample_execution as execution
from ai_sdlc.core.counterexample_models import (
    CounterexamplePlan,
    ResourceStateEvidence,
    counterexample_digest,
    source_digest_sha256,
)
from ai_sdlc.core.design_contract_store import (
    _verification_spec_entry,
    _verification_task_entry,
)
from ai_sdlc.core.implementation_store import (
    implementation_artifacts,
    read_input,
    read_progress,
    validate_implementation_verification_contract,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.quality_command import build_source_digest
from tests.integration.test_quantified_implementation import _cli, _payload
from tests.integration.test_stage_quantified_pipeline import (
    CAPABILITY,
    LOOP,
    WORK_ITEM,
    actual_record,
    stage_apply,
)
from tests.unit.test_counterexample_models import contract_data
from tests.unit.test_implementation_loop import _write_ready_work_item
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import (
    contract_data as scoring_contract_data,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/counterexample"
FOLDER = f".ai-sdlc/loops/implementation/{LOOP}/counterexamples"


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _json_ref(root, name, data):
    path = root / FOLDER / name
    LoopArtifactStore(root).write_bytes_artifact(
        path,
        (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
        immutable=True,
    )
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _prepare_project(root, kind, *, multi_task=False):
    work = _write_ready_work_item(root)
    spec = work / "spec.md"
    spec.write_text(
        spec.read_text().replace(
            "系统必须记录实现任务证据。",
            "输入 saved 必须持久保存，独立新进程读取值严格等于 saved，原应答不作为保存证明。",
        )
    )
    tasks = work / "tasks.md"
    tasks.write_text(
        tasks.read_text()
        .replace(
            "src/ai_sdlc/core/implementation_loop.py",
            "business/save.py, business/observe.py, business/v0.py, business/v1.py, business/check.py",
        )
        .replace(
            "uv run pytest tests/unit/test_implementation_loop.py -q",
            "python business/check.py v0",
        )
    )
    business = root / "business"
    business.mkdir()
    for name in ("save.py", "observe.py", "v0.py"):
        shutil.copyfile(FIXTURES / kind / name, business / name)
    if kind == "sqlite_case":
        path = business / "save.py"
        path.write_text(path.read_text().replace("COMMIT = True", "COMMIT = False"))
    (business / "check.py").write_text(
        "import json,pathlib,sqlite3,subprocess,sys,tempfile\n"
        "base=pathlib.Path(__file__).parent\n"
        "with tempfile.TemporaryDirectory(prefix='ce-ordinary-') as directory:\n"
        f" path=pathlib.Path(directory)/{'state.db' if kind == 'sqlite_case' else 'state.json'!r}\n"
        + (
            " connection=sqlite3.connect(path);connection.execute('CREATE TABLE state(value TEXT)');connection.execute(\"INSERT INTO state VALUES ('initial')\");connection.commit();connection.close()\n"
            if kind == "sqlite_case"
            else " path.write_text(json.dumps({'value':'initial'}))\n"
        )
        + " subprocess.run([sys.executable,str(base/'save.py'),str(path),'saved'],check=True)\n"
        " subprocess.run([sys.executable,str(base/(sys.argv[1]+'.py')),str(path),'saved'],check=True)\n"
    )
    if multi_task:
        shutil.copytree(business, root / "business_second")
        # 两个义务的断言ID不同，但使用同一验收源码；参数选择断言，不产生第二个V1。
        shared_v0 = (
            (business / "v0.py")
            .read_text()
            .replace("import json", "import json\nimport sys")
            .replace(
                '"assertion_id": "saved-state"',
                '"assertion_id": sys.argv[3] if len(sys.argv) > 3 else "saved-state"',
            )
        )
        for directory in ("business", "business_second"):
            (root / directory / "v0.py").write_text(shared_v0)
        tasks.write_text(
            tasks.read_text()
            .replace("Task 2.1 Deferred polish", "Task 2.1 Save independent state")
            .replace("**优先级**：P2", "**优先级**：P0")
            .replace(
                "**文件**：README.md",
                "**文件**：business_second/save.py, business_second/observe.py, "
                "business_second/v0.py, business_second/v1.py, business_second/check.py",
            )
            .replace("1. Deferred.", "1. 第二任务从独立新进程读取 saved。")
        )
        first_task, separator, second_task = tasks.read_text().partition("### Task 2.1")
        tasks.write_text(
            first_task
            + separator
            + second_task.replace(
                "python business/check.py v0", "python business_second/check.py v0"
            )
        )
    (root / ".gitignore").write_text(
        "scratch/\n.ai-sdlc/loops/\n.ai-sdlc/state/\n.ai-sdlc/work-items/\n"
    )
    original = spec.read_bytes()
    data = contract_data()
    data["work_item_id"] = work.name
    data["sources"][0].update(
        path=spec.relative_to(root).as_posix(),
        sha256=hashlib.sha256(original).hexdigest(),
        locator="FR-IMPL-001",
        entry_sha256=hashlib.sha256(
            _verification_spec_entry(original, "FR-IMPL-001")
        ).hexdigest(),
    )
    data["budget_ref"] = {
        "path": spec.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(original).hexdigest(),
    }
    data["obligations"][0]["mechanisms"][0].update(
        key="wrong-persistent-value", hypothesis="保存应答成功但新进程读取错误持久值。"
    )
    data["obligations"][0]["oracle_spec"]["observation_method"]["kind"] = (
        "sqlite_json" if kind == "sqlite_case" else "file_json"
    )
    data["obligations"][0]["oracle_spec"]["observation_method"]["location"] = (
        "state.db" if kind == "sqlite_case" else "state.json"
    )
    if multi_task:
        second = copy.deepcopy(data["obligations"][0])
        second["id"] = "save-second"
        second["oracle_spec"]["assertion_id"] = "saved-state-second"
        data["obligations"].append(second)
    # 验收归属来自冻结任务原条目；可选 T21 不使默认样例的 owner 变成猜测。
    original_tasks = tasks.read_bytes()
    for task_id, obligation in zip(
        ("T11", "T21") if multi_task else ("T11",), data["obligations"], strict=True
    ):
        source_id = f"task-{task_id}"
        locator = f"{task_id}/acceptance/1"
        data["sources"].append(
            {
                "id": source_id,
                "namespace": "task",
                "task_id": task_id,
                "path": tasks.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(original_tasks).hexdigest(),
                "locator": locator,
                "entry_sha256": hashlib.sha256(
                    _verification_task_entry(original_tasks, task_id, locator)
                ).hexdigest(),
            }
        )
        obligation["oracle_spec"]["verification_source_ids"] = [source_id]
    (work / "verification.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Counterexample fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "original controlled fixture")


def _select(root, stage):
    from ai_sdlc.core.loop_simulation_models import STAGE_PROFILES

    source = root / WORK_ITEM / "spec.md"
    contracts = [
        {
            **scoring_contract_data(),
            "capability": CAPABILITY,
            "loop_type": stage,
            "profile_id": profile,
        }
        for profile in STAGE_PROFILES[stage]
    ]
    context = stage_apply(
        root,
        stage,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": contracts,
            "sources": [
                {
                    "id": "spec",
                    "path": source.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "locator": "FR-IMPL-001",
                    "claim": "原持久保存规范",
                }
            ],
        },
    )
    frozen = stage_apply(
        root,
        stage,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [
                candidate_data(context.plan, "baseline"),
                candidate_data(context.plan, "better"),
            ],
        },
    )
    return stage_apply(
        root,
        stage,
        {
            "operation": "record-comparison",
            "request_id": "judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [
                    assessment_data("baseline", 2, 2),
                    assessment_data("better", 3, 3),
                ],
            },
        },
    )


def _review_close(root, stage):
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            stage,
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    if stage == "implementation":
        path = next(
            p
            for p in reviewed["artifact_paths"]
            if p.endswith("/implementation-input.json")
        )
        captured = _payload(
            _cli(
                root,
                "loop",
                "review",
                "--type",
                stage,
                "--loop-id",
                LOOP,
                "--expect-digest",
                reviewed["input_digest"],
                "--read-path",
                path,
                "--json",
                timeout=180,
            )
        )
        assert captured["review_snapshot"] == {
            "path": path,
            "encoding": "utf-8",
            "content": (root / path).read_text(),
            "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
        }
    assert actual_record(root, stage, reviewed, timeout=180)["status"] == "passed"
    result = _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return reviewed


def _assert_fix25_native_review_capture(root, reviewed):
    from ai_sdlc.cli.loop_review_cmd import resolve_review_input

    r1 = implementation_artifacts(root, LOOP).loop_dir / "review-outcome-round-1.json"
    key = r1.relative_to(root).as_posix()
    original = r1.read_bytes()
    for capture_all in (False, True):
        captured = {}
        current = resolve_review_input(
            root, loop_type="implementation", loop_id=LOOP,
            review_round_number=reviewed["round_number"],
            captured_artifacts=captured, capture_all=capture_all,
        )
        assert current.input_digest == reviewed["input_digest"]
        assert captured[key] == original
        if reviewed["round_number"] == 1:
            assert key not in current.artifact_paths
    requested = next(path for path in reviewed["artifact_paths"] if path.endswith("/implementation-input.json"))
    single = {}
    current = resolve_review_input(
        root, loop_type="implementation", loop_id=LOOP,
        review_round_number=reviewed["round_number"],
        captured_artifacts=single, capture_paths=[requested],
    )
    assert current.input_digest == reviewed["input_digest"]
    assert set(single) == {requested}
    assert r1.read_bytes() == original


def _start_lifecycle(root, kind, *, integrate_v1=True, multi_task=False):
    _prepare_project(root, kind, multi_task=multi_task)
    for stage in ("requirement", "design-contract", "implementation"):
        if stage == "requirement":
            args = [
                "start",
                "--idea",
                "持久保存并从新进程读取",
                "--acceptance",
                "新进程读取 saved",
                "--work-item-id",
                "demo-implementation-loop",
                "--design-scope-family",
                "implementation",
            ]
        elif stage == "design-contract":
            args = [
                "check",
                "--wi",
                WORK_ITEM,
                "--requirement-loop-id",
                LOOP,
                "--verification-contract",
                WORK_ITEM + "/verification.json",
            ]
        else:
            args = ["start", "--wi", WORK_ITEM, "--design-contract-loop-id", LOOP]
        result = _cli(
            root,
            "loop",
            stage,
            *args,
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        _select(root, stage)
        if stage != "implementation":
            _review_close(root, stage)
    for task_id in ("T11", "T21") if multi_task else ("T11",):
        result = _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            LOOP,
            "--task-id",
            task_id,
            "--status",
            "in_progress",
            "--json",
        )
        assert result.returncode == 0, result.stdout + result.stderr
    if integrate_v1:
        # 其他机械性生命周期用例可从既有 V1 开始；完整准入用例显式保留母候选原样。
        shutil.copyfile(FIXTURES / kind / "v1.py", root / "business/v1.py")
        if multi_task:
            shared_v1 = (
                (FIXTURES / kind / "v1.py")
                .read_text()
                .replace(
                    '"assertion_id": "saved-state"',
                    '"assertion_id": sys.argv[3] if len(sys.argv) > 3 else "saved-state"',
                )
            )
            for directory in ("business", "business_second"):
                (root / directory / "v1.py").write_text(shared_v1)
        _git(root, "add", ".")
    return read_input(implementation_artifacts(root, LOOP).input_path)


def _plan(
    root,
    impl_input,
    kind,
    generation,
    *,
    draft_v1=None,
    extra_control=False,
    variant_content=None,
    max_execution_attempts=108,
    task_id="T11",
    business_dir="business",
    obligation_id="save",
):
    contract, _ = validate_implementation_verification_contract(root, impl_input)
    obligation = next(item for item in contract.obligations if item.id == obligation_id)
    candidate = source_digest_sha256(build_source_digest(root))
    base = root / FOLDER / generation
    base.mkdir(parents=True)
    endpoint_name = "state.db" if kind == "sqlite_case" else "state.json"
    initial_path = base / endpoint_name
    if kind == "sqlite_case":
        connection = sqlite3.connect(initial_path)
        connection.execute("CREATE TABLE state(value TEXT)")
        connection.execute("INSERT INTO state VALUES ('initial')")
        connection.commit()
        connection.close()
    else:
        initial_path.write_text(json.dumps({"value": "initial"}))
    content_ref = {
        "path": initial_path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(initial_path.read_bytes()).hexdigest(),
    }
    initial_ref = _json_ref(
        root,
        f"{generation}/initial.json",
        {
            "schema_version": 1,
            "files": [
                {
                    "path": endpoint_name,
                    "sha256": content_ref["sha256"],
                    "content_ref": content_ref,
                }
            ],
        },
    )
    witness = _json_ref(
        root,
        f"{generation}/witness.json",
        {
            "schema_version": 1,
            "input_value": {"type": "string", "value": "saved"},
            "premise_values": {},
        },
    )
    subjects, steps, snapshots = [], [], {}
    clones = root.parent / (root.name + "-" + generation + "-clones")
    clones.mkdir()
    roles = [(role, role) for role in ("current", "variant", "positive_control")]
    if extra_control:
        roles.append(("second_control", "positive_control"))
    for subject_id, role in roles:
        target = clones / subject_id
        # 保留当前 HEAD、index、未提交和未跟踪文件，不能只 clone HEAD。
        shutil.copytree(
            root,
            target,
            ignore=lambda path, names: [
                name
                for name in names
                if name in {"loops", "state", "work-items", "reviews"}
                and Path(path).name == ".ai-sdlc"
            ],
        )
        if draft_v1 is not None:
            # 草案只进入隔离副本的显式输入，母候选及其源码身份保持原样。
            (target / business_dir / "v1.py").write_bytes(draft_v1)
            with (target / ".git/info/exclude").open("a") as excluded:
                excluded.write(f"\n{business_dir}/v1.py\n")
        if role == "variant":
            source = target / business_dir / "save.py"
            source.write_text(source.read_text().replace("sys.argv[2]", "'mutant'"))
            if variant_content is not None:
                source.write_bytes(variant_content)
        if role == "positive_control" and kind == "sqlite_case":
            source = target / business_dir / "save.py"
            source.write_text(
                source.read_text().replace("COMMIT = False", "COMMIT = True")
            )
        resource = target / "scratch"
        resource.mkdir()
        (resource / endpoint_name).write_bytes(initial_path.read_bytes())
        snapshot = execution.capture_counterexample_snapshot(
            target,
            evidence_root=root,
            artifact_dir=f"{FOLDER}/{generation}/snapshots/{subject_id}",
            acceptance_sources={
                "V0": f"{business_dir}/v0.py",
                "V1": f"{business_dir}/v1.py",
            },
            ignored_inputs=(f"{business_dir}/v1.py",) if draft_v1 is not None else (),
        )
        snapshots[subject_id] = snapshot
        reference = _json_ref(
            root, f"{generation}/{subject_id}-snapshot.json", snapshot
        )
        subjects.append(
            {
                "id": subject_id,
                "role": role,
                "obligation_id": obligation_id,
                "witness_id": "saved-input",
                "candidate_digest": snapshot["source_digest"],
                "parent_candidate_digest": candidate,
                "snapshot": reference,
                "patch": None,
                "modified_paths": [],
                "mechanism_key": "wrong-persistent-value" if role == "variant" else "",
                "positive_control_refs": [
                    item for item, kind in roles if kind == "positive_control"
                ]
                if role == "variant"
                else [],
            }
        )
        previous = []
        for phase in ("final", "r2"):
            for name, step_kind, version in (
                ("reset", "reset", "none"),
                ("save", "exercise", "none"),
                ("observe", "observe", "none"),
                ("v0", "acceptance", "V0"),
                ("v1", "acceptance", "V1"),
                ("cleanup", "cleanup", "none"),
            ):
                if name in ("reset", "cleanup"):
                    argv = [
                        sys.executable,
                        "-c",
                        "print('owned resource step')",
                        str(resource),
                    ]
                else:
                    argv = [
                        sys.executable,
                        f"{business_dir}/{name}.py",
                        str(resource / endpoint_name),
                        "saved",
                    ]
                    if step_kind == "acceptance":
                        argv.append(obligation.oracle_spec.assertion_id)
                environment = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
                step_id = f"{subject_id}-{phase}-{name}"
                steps.append(
                    {
                        "id": step_id,
                        "subject_id": subject_id,
                        "assertion_id": obligation.oracle_spec.assertion_id,
                        "acceptance_version": version,
                        "business_observation_step_id": (
                            f"{subject_id}-{phase}-observe"
                            if step_kind == "acceptance"
                            else None
                        ),
                        "kind": step_kind,
                        "phase": phase,
                        "binding": {
                            "project_root": str(target),
                            "cwd": ".",
                            "argv": argv,
                            "witness_input": {
                                "witness_id": "saved-input",
                                "argv_index": 3,
                                "encoding": "text",
                            }
                            if step_kind == "exercise"
                            else None,
                            "effective_environment": environment,
                            "environment_digest": counterexample_digest(environment),
                            "resources": [
                                {
                                    "id": "data",
                                    "permission_id": "data",
                                    "kind": "directory",
                                    "root": str(resource),
                                    "initial_state": initial_ref,
                                    "observation_endpoint": str(
                                        resource / endpoint_name
                                    ),
                                    "cleanup_method": "owned_directory",
                                }
                            ],
                            "timeout_seconds": 3,
                            "max_output_bytes": 32768,
                        },
                        "reservation_seconds": 1,
                        "depends_on": previous,
                    }
                )
                previous = [step_id]
    before = next(
        row["sha256"]
        for row in snapshots["current"]["files"]
        if row["path"] == f"{business_dir}/save.py"
    )
    for subject in subjects[1:]:
        after = next(
            row["sha256"]
            for row in snapshots[subject["id"]]["files"]
            if row["path"] == f"{business_dir}/save.py"
        )
        if before == after:
            continue
        subject.update(
            patch=_json_ref(
                root,
                f"{generation}/{subject['id']}-patch.json",
                {
                    "schema_version": 1,
                    "changes": [
                        {
                            "path": f"{business_dir}/save.py",
                            "before_sha256": before,
                            "after_sha256": after,
                        }
                    ],
                },
            ),
            modified_paths=[f"{business_dir}/save.py"],
        )
    plan = CounterexamplePlan.model_validate(
        {
            "id": generation,
            "work_item_id": impl_input.work_item_id,
            "task_id": task_id,
            "loop_id": LOOP,
            "contract_digest": impl_input.verification_contract_digest,
            "contract_model_digest": counterexample_digest(contract),
            "candidate_digest": candidate,
            "v0_digest": hashlib.sha256(
                (root / business_dir / "v0.py").read_bytes()
            ).hexdigest(),
            "v1_digest": hashlib.sha256(
                draft_v1
                if draft_v1 is not None
                else (root / business_dir / "v1.py").read_bytes()
            ).hexdigest(),
            "budget_ref": contract.budget_ref.model_dump(),
            "allowed_modified_paths": [f"{business_dir}/save.py"],
            "protected_paths": [
                f"{business_dir}/observe.py",
                f"{business_dir}/v0.py",
                f"{business_dir}/v1.py",
            ],
            "witnesses": [
                {
                    "id": "saved-input",
                    "input_ref": witness,
                    "input_value": {"type": "string", "value": "saved"},
                    "premise_values": {},
                }
            ],
            "subjects": subjects,
            "steps": steps,
            "max_execution_attempts": max_execution_attempts,
            "generation_batch": 1,
            "reinforcement_batch": 1,
            "required_reserve_seconds": 300,
        }
    )
    ref = _json_ref(root, f"{generation}/plan.json", plan.model_dump(mode="json"))
    execution.validate_counterexample_snapshots(root, plan)
    return plan, ref["path"]


def _verify_plan(root, path, *, task_id="T11"):
    return _cli(
        root,
        "loop",
        "implementation",
        "verify",
        "--loop-id",
        LOOP,
        "--task-id",
        task_id,
        "--counterexample-plan",
        path,
        "--json",
        timeout=600,
    )


def _latest_counterexample(root, impl):
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    reference = next(
        task for task in progress.tasks if task.task_id == "T11"
    ).counterexample_results[-1]
    return reference, execution.resolve_counterexample_evidence(root, impl, reference)


def test_fix28_incomplete_intent_blocks_native_verify_without_replay(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    plan, path = _plan(root, impl, "file_case", "complete-intent-original")
    legal = _verify_plan(root, path)
    assert legal.returncode == 0, legal.stdout + legal.stderr
    _, bound = _latest_counterexample(root, impl)
    assert bound.assessment.required_complete
    folder = execution._attempts_dir(root, plan)
    intent = sorted(folder.glob("*/intent.json"))[0]
    original = intent.read_bytes()
    document = json.loads(original)
    document.pop("plan_digest")
    intent.write_text(json.dumps(document))
    before = {
        item.relative_to(folder).as_posix(): item.read_bytes()
        for item in folder.rglob("*") if item.is_file()
    }
    blocked = _verify_plan(root, path)
    assert blocked.returncode == 1, blocked.stdout + blocked.stderr
    assert "Traceback" not in blocked.stdout + blocked.stderr
    result = json.loads(blocked.stdout)
    assert result["loop_status"] == "blocked" and not result["closed"]
    assert "intent" in result["blocker"]
    assert before == {
        item.relative_to(folder).as_posix(): item.read_bytes()
        for item in folder.rglob("*") if item.is_file()
    }
    assert intent.read_bytes() != original


def _assert_readonly_observation_resources(root, bound):
    """独立原件同时证明观察与验收没有改变本次持久资源。"""
    steps = {step.id: step for step in bound.plan.steps}
    checked = []
    for attempt in bound.observations.attempts:
        if steps[attempt.step_id].kind not in {"observe", "acceptance"}:
            continue
        state_refs = [
            ref
            for ref in attempt.raw_evidence_refs
            if ref.path.endswith("/resource-state.json")
        ]
        assert len(state_refs) == 1
        reference = state_refs[0]
        raw = (root / reference.path).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == reference.sha256
        state = json.loads(raw)
        assert state["step_id"] == attempt.step_id
        assert state["before"] == state["after"]
        proof = ResourceStateEvidence.model_validate(state)
        assert proof.model_dump(mode="json") == state
        checked.append(attempt.step_id)
    assert checked


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod 对照，不冒充 Windows ACL 验收")
@pytest.mark.parametrize("phase", ["observe", "V0"])
@pytest.mark.parametrize("mutate", [False, True])
def test_native_permission_originals_reach_live_and_captured_evaluation(
    tmp_path, phase, mutate
):
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from tests.unit import test_counterexample_execution as cases

    # 复用实际命令和冻结资源夹具；分别覆盖节点权限和资源根权限，原件不重写。
    target_kind = "file" if phase == "observe" else "resource-directory"
    case = cases._fix20_permission_case(
        cases.execution_case.__wrapped__(tmp_path), phase, target_kind, mutate
    )
    root, contract, plan = case
    receipts = []
    try:
        if phase == "V0":
            receipts.append(cases._execute(case))
        receipt = cases._execute(
            case, "current-none" if phase == "observe" else "current-V0"
        )
        receipts.append(receipt)
        assert receipt.exit_code == 0 and receipt.cleanup_status == "complete"
        assert receipt.status == ("infrastructure_error" if mutate else "completed")
        state_ref = next(
            ref
            for ref in receipt.raw_evidence_refs
            if ref.path.endswith("/resource-state.json")
        )
        state_raw = (root / state_ref.path).read_bytes()
        assert hashlib.sha256(state_raw).hexdigest() == state_ref.sha256
        proof = ResourceStateEvidence.model_validate_json(state_raw)
        assert (proof.before != proof.after) is mutate
        captured = {
            ref.path: (root / ref.path).read_bytes()
            for attempt in receipts
            for ref in execution.attempt_artifact_refs(root, attempt.attempt_ref)
        }
        for attempt in receipts:
            assert (
                execution.recover_counterexample_attempt(root, plan, attempt.attempt_ref)
                == attempt
            )
            assert (
                execution.recover_counterexample_attempt(
                    root, plan, attempt.attempt_ref, captured_artifacts=captured
                )
                == attempt
            )
        live = execution._bundle_from_attempts(root, contract, plan, receipts)
        frozen = execution._bundle_from_attempts(
            root, contract, plan, receipts, captured_artifacts=captured
        )
        assert live == frozen
        assessment = evaluate_counterexample(contract, plan, live)
        assert assessment == evaluate_counterexample(contract, plan, frozen)
        if phase == "observe":
            assert assessment.current_result.status == ("UNKNOWN" if mutate else "PASS")
        else:
            assert assessment.current_result.status == "PASS"
            assert assessment.current_subjects[0].v0 == (
                "unknown" if mutate else "accepted"
            )
        if mutate:
            error = (root / receipt.attempt_ref.path).parent / "postcheck-error.json"
            assert (
                "counterexample-observation-resource-state-mutated" in error.read_text()
            )
            assert not assessment.required_complete
        assert (root / state_ref.path).read_bytes() == state_raw
    finally:
        if receipts:
            cleanup = cases._execute(case, "current-cleanup", cleanup_recovery=True)
            assert cleanup.status == "completed" and cleanup.cleanup_status == "complete"


def _apply_admitted_v1(root, impl, reference, draft):
    bound = execution.resolve_counterexample_evidence(root, impl, reference)
    before = source_digest_sha256(build_source_digest(root))
    assert before == bound.plan.candidate_digest
    assert (
        bound.assessment.required_complete
        and bound.assessment.v1_disposition == "adopt_v1"
    ), "V1 draft not admitted"
    assert hashlib.sha256(draft).hexdigest() == bound.plan.v1_digest
    target = root / "business/v1.py"
    assert not target.exists()
    applied_at = time.time_ns() // 1_000_000
    target.write_bytes(draft)
    _git(root, "add", "business/v1.py")
    after = source_digest_sha256(build_source_digest(root))
    assert after != before
    _json_ref(
        root,
        "v1-application.json",
        {
            "admission_record": reference.model_dump(mode="json"),
            "before_candidate_digest": before,
            "after_candidate_digest": after,
            "v1_sha256": bound.plan.v1_digest,
            "applied_at_ms": applied_at,
        },
    )


def _ordinary(root, version, *, task_id="T11", business_dir="business"):
    return _cli(
        root,
        "loop",
        "implementation",
        "verify",
        "--loop-id",
        LOOP,
        "--task-id",
        task_id,
        "--json",
        "--",
        sys.executable,
        f"{business_dir}/check.py",
        version,
        timeout=180,
    )


def _assert_closed_readonly(root):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.loop_review_service import read_verified_implementation_close

    def originals():
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in (root / ".ai-sdlc/loops").rglob("*")
            if path.is_file()
        }

    before = originals()
    close = read_verified_implementation_close(
        root, LOOP, review_input_validator=validate_review_input_for_close
    )
    assert close.next_loop_type == "local-pr-review"
    assert originals() == before


def _preserve_r1_inputs(root, loop_dir):
    # 合成R2结果沿同入口写入前，单独保留R1真实输入，不能仅在内存中留一份outcome。
    originals = [
        loop_dir / "review-outcome-round-1.json",
        loop_dir / "decision-context.json",
        *(root / ".ai-sdlc/state/stage-test-experts").glob("implementation-*.json"),
    ]
    store = LoopArtifactStore(root)
    for source in originals:
        store.write_bytes_artifact(
            loop_dir / "counterexamples/r1-originals" / source.name,
            source.read_bytes(),
            immutable=True,
        )


def test_file_save_rejects_input_different_from_declared_legal_witness(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    plan, _ = _plan(root, impl, "file_case", "wrong-witness-input")
    data = plan.model_dump(mode="json")
    save = next(
        step
        for step in data["steps"]
        if step["subject_id"] == "variant" and step["kind"] == "exercise"
    )
    save["binding"]["argv"][3] = "different-input"
    reference = _json_ref(root, "mismatched-witness-plan.json", data)
    result = _verify_plan(root, reference["path"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert "witness-argv-input-mismatch" in result.stdout + result.stderr
    for subject in plan.subjects:
        step = next(step for step in plan.steps if step.subject_id == subject.id)
        endpoint = Path(step.binding.resources[0].observation_endpoint)
        assert json.loads(endpoint.read_text()) == {"value": "initial"}
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert not next(
        task for task in progress.tasks if task.task_id == "T11"
    ).counterexample_results

    # 即使仍使用合法输入，V1 也不能被挪到它声称消费的业务观察之前。
    premature = plan.model_dump(mode="json")
    v1 = next(step for step in premature["steps"] if step["id"] == "variant-final-v1")
    v1["depends_on"] = ["variant-final-reset"]
    premature["steps"].remove(v1)
    position = next(
        index
        for index, step in enumerate(premature["steps"])
        if step["id"] == "variant-final-save"
    )
    premature["steps"].insert(position, v1)
    early_ref = _json_ref(root, "early-acceptance-plan.json", premature)
    early = _verify_plan(root, early_ref["path"])
    assert early.returncode == 1, early.stdout + early.stderr
    assert "counterexample-acceptance-business-binding-conflict" in (
        early.stdout + early.stderr
    )
    assert "counterexample-step-reference-or-order-invalid" not in (
        early.stdout + early.stderr
    )
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))

    # 受版本管理的业务目录不能充当可删除的临时资源，原文件必须保持。
    invalid_resource = plan.model_dump(mode="json")
    originals = {}
    for step in invalid_resource["steps"]:
        project = Path(step["binding"]["project_root"])
        protected = project / "business"
        originals.update(
            {path: path.read_bytes() for path in protected.iterdir() if path.is_file()}
        )
        binding = step["binding"]
        old_endpoint = binding["resources"][0]["observation_endpoint"]
        binding["resources"][0]["root"] = str(protected)
        binding["resources"][0]["observation_endpoint"] = str(protected / "state.json")
        binding["argv"] = [
            str(protected / "state.json") if part == old_endpoint else part
            for part in binding["argv"]
        ]
    invalid_ref = _json_ref(root, "protected-resource-plan.json", invalid_resource)
    invalid = _verify_plan(root, invalid_ref["path"])
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert not list(execution._attempts_dir(root, plan).glob("*/intent.json"))
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_failed_command_cleans_owned_resources_and_keeps_independent_subject(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    original, _ = _plan(root, impl, "file_case", "ordinary-command-failure")
    data = original.model_dump(mode="json")
    for step in data["steps"]:
        if step["id"] == "current-final-save":
            step["binding"]["argv"] = [
                sys.executable,
                "-c",
                "raise SystemExit(7)",
                step["binding"]["resources"][0]["observation_endpoint"],
                "saved",
            ]
            step["binding"]["witness_input"]["argv_index"] = 4
        if step["id"] == "variant-final-reset":
            step["depends_on"] = ["current-final-observe"]
    reference = _json_ref(root, "ordinary-failure-plan.json", data)
    result = _verify_plan(root, reference["path"])
    assert result.returncode == 1, result.stdout + result.stderr
    record, bound = _latest_counterexample(root, impl)
    assert not bound.assessment.required_complete, result.stdout + result.stderr
    attempts = {item.step_id: item for item in bound.observations.attempts}
    assert attempts["current-final-save"].exit_code == 7
    assert attempts["current-final-cleanup"].cleanup_status == "complete"
    assert "current-final-observe" not in attempts
    assert not any(identifier.startswith("variant-") for identifier in attempts)
    assert any(
        row.subject_id == "positive_control" for row in bound.observations.business
    )
    assert "positive_control-final-cleanup" in attempts
    current = next(step for step in bound.plan.steps if step.subject_id == "current")
    assert not Path(current.binding.resources[0].root).exists()
    before = set(execution._attempts_dir(root, bound.plan).glob("*/intent.json"))
    _verify_plan(root, reference["path"])
    assert (
        set(execution._attempts_dir(root, bound.plan).glob("*/intent.json")) == before
    )
    assert (
        execution.resolve_counterexample_evidence(root, impl, record).observations
        == bound.observations
    )
    # 即使重新计算自洽摘要和 UNKNOWN 报告，也不能删掉真实已执行的观测。
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from ai_sdlc.core.counterexample_models import ArtifactRef

    omitted = bound.observations.model_copy(
        update={"acceptance": bound.observations.acceptance[1:]}
    )
    altered_record = json.loads((root / record.path).read_bytes())
    altered_record["observations_ref"] = _json_ref(
        root, "omitted-observation.json", omitted.model_dump(mode="json")
    )
    altered_record["assessment_ref"] = _json_ref(
        root,
        "omitted-assessment.json",
        evaluate_counterexample(bound.contract, bound.plan, omitted).model_dump(
            mode="json"
        ),
    )
    altered = ArtifactRef.model_validate(
        _json_ref(root, "omitted-record.json", altered_record)
    )
    with pytest.raises(ValueError, match="observation-set-not-derived-from-raw"):
        execution.resolve_counterexample_evidence(root, impl, altered)


def test_file_save_uses_full_native_lifecycle_and_independent_guard(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case", integrate_v1=False)
    ordinary = _ordinary(root, "v0")
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    assert "verification passed" in json.loads(ordinary.stdout)["result"]
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert execution.counterexample_verification_state(root, impl, progress)[0]
    premature = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--json",
    )
    assert json.loads(premature.stdout)["blocker_count"] > 0, premature.stdout
    assert json.loads(premature.stdout)["loop_status"] == "needs_fix", premature.stdout
    continuing = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "in_progress",
        "--json",
    )
    assert continuing.returncode == 0, continuing.stdout + continuing.stderr
    original_candidate = source_digest_sha256(build_source_digest(root))
    draft = (FIXTURES / "file_case/v1.py").read_bytes()
    draft_plan, draft_path = _plan(
        root, impl, "file_case", "file-draft", draft_v1=draft
    )
    draft_result = _verify_plan(root, draft_path)
    assert draft_result.returncode == 1, draft_result.stdout + draft_result.stderr
    assert not (root / "business/v1.py").exists()
    assert source_digest_sha256(build_source_digest(root)) == original_candidate
    admission_ref, admission = _latest_counterexample(root, impl)
    assert admission.assessment.v1_disposition == "adopt_v1"
    _apply_admitted_v1(root, impl, admission_ref, draft)
    plan, path = _plan(root, impl, "file_case", "file-final")
    assert plan.candidate_digest != draft_plan.candidate_digest
    assert plan.v1_digest == draft_plan.v1_digest
    result = _verify_plan(root, path)
    assert result.returncode == 0, result.stdout + result.stderr
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    reference = next(
        task for task in progress.tasks if task.task_id == "T11"
    ).counterexample_results[-1]
    bound = execution.resolve_counterexample_evidence(root, impl, reference)
    assert bound.assessment.required_complete, bound.assessment
    _assert_readonly_observation_resources(root, bound)
    assert bound.assessment.v1_disposition == "adopt_v1"
    assert bound.assessment.variants[0].v0 == "missed"
    assert bound.assessment.variants[0].v1 == "detected"
    assert all(
        item.business.status == "PASS" and item.v1 == "accepted"
        for item in bound.assessment.positive_controls
    )
    captured = {ref.path: (root / ref.path).read_bytes() for ref in bound.artifact_refs}
    assert (
        execution.resolve_counterexample_evidence(
            root, impl, reference, captured_artifacts=captured
        ).assessment
        == bound.assessment
    )
    # 已有原生完整链的产物必须同时通过纯核和任务汇总，不能停在 receipt/recover。
    from ai_sdlc.core.counterexample_evaluation import (
        aggregate_task_assessments,
        evaluate_counterexample,
    )

    for originals in (None, captured):
        receipts = tuple(
            execution.recover_counterexample_attempt(
                root, plan, item.attempt_ref, captured_artifacts=originals
            )
            for item in bound.observations.attempts
        )
        assert receipts == bound.observations.attempts
        bundle = execution._bundle_from_attempts(
            root, bound.contract, plan, receipts, captured_artifacts=originals
        )
        assert bundle == bound.observations
        assessment = evaluate_counterexample(bound.contract, plan, bundle)
        assert assessment == bound.assessment
        disposition, complete, _ = aggregate_task_assessments(
            bound.contract, [(plan, assessment)], task_ids=[plan.task_id]
        )
        assert complete and disposition == "adopt_v1"
    assert not execution.counterexample_verification_state(root, impl, progress)[0]
    # 正式审查捕获集合缺失原件时禁止从磁盘补成功；改字节同样拒绝。
    observation_path = bound.observations.business[0].raw_evidence_ref.path
    record = json.loads((root / reference.path).read_bytes())
    for missing in (impl.verification_contract_ref, record["assessment_ref"]["path"]):
        broken = {path: raw for path, raw in captured.items() if path != missing}
        with pytest.raises(ValueError):
            execution.resolve_counterexample_evidence(
                root, impl, reference, captured_artifacts=broken
            )
    for replacement in (
        None,
        b'{"schema_version":1,"typed_actual":{"type":"string","value":"saved"}}\n',
    ):
        broken = dict(captured)
        if replacement is None:
            del broken[observation_path]
        else:
            broken[observation_path] = replacement
        with pytest.raises(ValueError):
            execution.resolve_counterexample_evidence(
                root, impl, reference, captured_artifacts=broken
            )
    attempts = tuple(execution._attempts_dir(root, plan).glob("*/intent.json"))
    again = _verify_plan(root, path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert tuple(execution._attempts_dir(root, plan).glob("*/intent.json")) == attempts
    # 母源码漂移即令旧成功失效；测试恢复原字节后才继续正常收尾，不刷新合同或预算。
    source = root / "business/save.py"
    original = source.read_bytes()
    source.write_bytes(original + "\n# 受控候选漂移\n".encode())
    try:
        assert execution.counterexample_verification_state(root, impl, progress)[0]
        stale = _verify_plan(root, path)
        assert stale.returncode == 1, stale.stdout + stale.stderr
        assert (
            tuple(execution._attempts_dir(root, plan).glob("*/intent.json")) == attempts
        )
    finally:
        source.write_bytes(original)
    # 执行临时副本可清理；完整捕获原件足以支撑同计划只读复验和正式审查。
    shutil.rmtree(Path(plan.steps[0].binding.project_root).parent)
    after_cleanup = _verify_plan(root, path)
    assert after_cleanup.returncode == 0, after_cleanup.stdout + after_cleanup.stderr
    assert tuple(execution._attempts_dir(root, plan).glob("*/intent.json")) == attempts
    ordinary = _ordinary(root, "v1")
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    done = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--evidence",
        "business/v1.py",
        "--json",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    reviewed = _review_close(root, "implementation")
    assert reference.path in reviewed["artifact_paths"]
    assert any(path.endswith("/stdout") for path in reviewed["artifact_paths"])
    _assert_closed_readonly(root)
    _assert_fix25_native_review_capture(root, reviewed)


def test_sqlite_actual_rollback_requires_one_scoped_repair_and_new_process_readback(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "sqlite_case", integrate_v1=False)
    assert _ordinary(root, "v0").returncode == 0
    draft = (FIXTURES / "sqlite_case/v1.py").read_bytes()
    before_plan, before_path = _plan(
        root, impl, "sqlite_case", "sqlite-before", draft_v1=draft
    )
    before_verify = _verify_plan(root, before_path)
    assert before_verify.returncode == 1, before_verify.stdout + before_verify.stderr
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    before_ref = next(
        task for task in progress.tasks if task.task_id == "T11"
    ).counterexample_results[-1]
    before = execution.resolve_counterexample_evidence(root, impl, before_ref)
    with pytest.raises(AssertionError, match="not admitted"):
        _apply_admitted_v1(root, impl, before_ref, draft)
    assert not (root / "business/v1.py").exists()
    assert (
        source_digest_sha256(build_source_digest(root)) == before_plan.candidate_digest
    )
    assert before.assessment.current_result.status == "FAIL"
    assert not before.assessment.required_complete
    assert all(
        item.business.status == "PASS" for item in before.assessment.positive_controls
    )
    before_intents = tuple(
        execution._attempts_dir(root, before_plan).glob("*/intent.json")
    )
    # 宿主在原任务允许路径内只修一次业务 COMMIT，V1 和合同保持原字节。
    source = root / "business/save.py"
    old_variant_bytes = source.read_bytes().replace(b"sys.argv[2]", b"'mutant'")
    source.write_text(source.read_text().replace("COMMIT = False", "COMMIT = True"))
    _git(root, "add", ".")
    # 旧变体整文件会撤销 COMMIT 修复；即使新补丁元数据自洽也不能刷新原批次。
    bad_plan, bad_path = _plan(
        root,
        impl,
        "sqlite_case",
        "sqlite-old-variant",
        draft_v1=draft,
        variant_content=old_variant_bytes,
    )
    rejected = _verify_plan(root, bad_path)
    assert rejected.returncode == 1
    assert "batch-content" in rejected.stdout + rejected.stderr
    assert (
        tuple(execution._attempts_dir(root, bad_plan).glob("*/intent.json"))
        == before_intents
    )
    _, admission_path = _plan(
        root, impl, "sqlite_case", "sqlite-draft-after", draft_v1=draft
    )
    admission_verify = _verify_plan(root, admission_path)
    assert admission_verify.returncode == 1, (
        admission_verify.stdout + admission_verify.stderr
    )
    admission_ref, admission = _latest_counterexample(root, impl)
    original_control = next(
        s for s in before_plan.subjects if s.role == "positive_control"
    )
    repaired_control = next(
        s for s in admission.plan.subjects if s.role == "positive_control"
    )
    assert original_control.patch is not None
    assert repaired_control.patch is None and not repaired_control.modified_paths
    assert admission.assessment.v1_disposition == "adopt_v1"
    assert not (root / "business/v1.py").exists()
    _apply_admitted_v1(root, impl, admission_ref, draft)
    after_plan, after_path = _plan(root, impl, "sqlite_case", "sqlite-after")
    after_verify = _verify_plan(root, after_path)
    assert after_verify.returncode == 0, after_verify.stdout + after_verify.stderr
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    after_ref = next(
        task for task in progress.tasks if task.task_id == "T11"
    ).counterexample_results[-1]
    after = execution.resolve_counterexample_evidence(root, impl, after_ref)
    assert after.assessment.required_complete
    _assert_readonly_observation_resources(root, before)
    _assert_readonly_observation_resources(root, after)
    assert after.assessment.current_result.status == "PASS"
    assert after_plan.contract_digest == before_plan.contract_digest
    assert after_plan.v1_digest == before_plan.v1_digest
    assert after_plan.candidate_digest != before_plan.candidate_digest
    assert len(after.observations.repairs) == 1
    repair = after.observations.repairs[0]
    assert (
        repair.before_observation_refs == before.assessment.current_result.evidence_refs
    )
    assert (
        repair.after_observation_refs == after.assessment.current_result.evidence_refs
    )
    proof = json.loads((root / repair.repair_ref.path).read_bytes())
    assert [item["path"] for item in proof["changes"]] == ["business/save.py"]
    assert proof["before_record_ref"] == before_ref.model_dump(mode="json")
    assert all(path.is_file() for path in before_intents)
    ordinary = _ordinary(root, "v1")
    assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
    done = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--evidence",
        "business/v1.py",
        "--json",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    reviewed = _review_close(root, "implementation")
    assert repair.repair_ref.path in reviewed["artifact_paths"]
    assert before_ref.path in reviewed["artifact_paths"]
    _assert_closed_readonly(root)


@pytest.mark.parametrize("draft_result", ["False", "True"])
def test_rejected_or_unhelpful_v1_draft_does_not_change_mother_candidate(
    initialized_project_dir, draft_result
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case", integrate_v1=False)
    before = source_digest_sha256(build_source_digest(root))
    draft = (
        (FIXTURES / "file_case/v1.py")
        .read_bytes()
        .replace(
            b"accepted = value == sys.argv[2]", f"accepted = {draft_result}".encode()
        )
    )
    _, path = _plan(root, impl, "file_case", "rejected-draft", draft_v1=draft)
    verified = _verify_plan(root, path)
    assert verified.returncode == 1, verified.stdout + verified.stderr
    reference, bound = _latest_counterexample(root, impl)
    assert bound.assessment.current_result.status == "PASS"
    assert bound.assessment.v1_disposition == "retain_v0"
    assert all(c.business.status == "PASS" for c in bound.assessment.positive_controls)
    if draft_result == "False":
        assert all(
            c.v1 == "false_rejection" for c in bound.assessment.positive_controls
        )
    else:
        assert all(v.v1 == "missed" for v in bound.assessment.variants)
    with pytest.raises(AssertionError, match="not admitted"):
        _apply_admitted_v1(root, impl, reference, draft)
    assert source_digest_sha256(build_source_digest(root)) == before
    assert not (root / "business/v1.py").exists()
    assert not implementation_artifacts(root, LOOP).close_path.exists()


def test_same_candidate_cannot_drop_a_legitimate_rejected_control(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case", integrate_v1=False)
    draft = (
        (FIXTURES / "file_case/v1.py")
        .read_bytes()
        .replace(
            b"accepted = value == sys.argv[2]",
            b"accepted = value == sys.argv[2] and pathlib.Path.cwd().name != 'second_control'",
        )
    )
    first_plan, first_path = _plan(
        root, impl, "file_case", "all-controls", draft_v1=draft, extra_control=True
    )
    first = _verify_plan(root, first_path)
    assert first.returncode == 1, first.stdout + first.stderr
    first_ref, bound = _latest_counterexample(root, impl)
    assert all(c.business.status == "PASS" for c in bound.assessment.positive_controls)
    assert {c.v1 for c in bound.assessment.positive_controls} == {
        "accepted",
        "false_rejection",
    }
    assert bound.assessment.v1_disposition == "retain_v0"
    originals = {
        ref.path: (root / ref.path).read_bytes() for ref in bound.artifact_refs
    }
    attempts = set(execution._attempts_dir(root, first_plan).glob("*/intent.json"))
    later_plan, later_path = _plan(
        root, impl, "file_case", "omit-rejected-control", draft_v1=draft
    )
    assert later_plan.candidate_digest == first_plan.candidate_digest
    later = _verify_plan(root, later_path)
    assert later.returncode == 1, later.stdout + later.stderr
    assert "counterexample-original-controls-or-witnesses-changed" in (
        later.stdout + later.stderr
    )
    assert (
        set(execution._attempts_dir(root, first_plan).glob("*/intent.json")) == attempts
    )
    assert _latest_counterexample(root, impl)[0] == first_ref
    assert all((root / path).read_bytes() == raw for path, raw in originals.items())
    assert not (root / "business/v1.py").exists()
    assert not implementation_artifacts(root, LOOP).close_path.exists()


def test_current_unaggregated_plan_cannot_be_hidden_by_an_older_pass(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.core.implementation_loop import verify_implementation_task
    from ai_sdlc.core.implementation_models import ImplementationVerifyOptions
    from ai_sdlc.core.loop_decision_service import DecisionPreparationError

    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    original_plan, original_path = _plan(root, impl, "file_case", "complete-first")
    complete = _verify_plan(root, original_path)
    assert complete.returncode == 0, complete.stdout + complete.stderr
    assert _ordinary(root, "v1").returncode == 0
    done = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--json",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    original_intents = set(
        execution._attempts_dir(root, original_plan).glob("*/intent.json")
    )
    later_plan, later_path = _plan(root, impl, "file_case", "interrupted-later")
    assert later_plan.candidate_digest == original_plan.candidate_digest
    actual_execute = execution.execute_counterexample_attempt
    dispatches = []

    def interrupt_between_steps(*args, **kwargs):
        dispatches.append(args[3])
        if len(dispatches) == 2:
            raise InterruptedError(
                "controlled host interruption before second dispatch"
            )
        return actual_execute(*args, **kwargs)

    # 仅在两次调度之间打断宿主；第一动作、原始回执和恢复代码都真实执行。
    with monkeypatch.context() as boundary:
        boundary.setattr(
            execution, "execute_counterexample_attempt", interrupt_between_steps
        )
        with pytest.raises(InterruptedError, match="before second dispatch"):
            verify_implementation_task(
                ImplementationVerifyOptions(
                    root=root,
                    loop_id=LOOP,
                    task_id="T11",
                    cwd=".",
                    argv=(),
                    counterexample_plan=later_path,
                )
            )
    assert dispatches == ["current-final-reset", "current-final-save"]
    after_intents = set(execution._attempts_dir(root, later_plan).glob("*/intent.json"))
    new_intents = after_intents - original_intents
    assert len(new_intents) == 1
    intent = next(iter(new_intents))
    reference = execution._ref(root, intent)
    receipt = execution.recover_counterexample_attempt(root, later_plan, reference)
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    blockers, _, _ = execution.counterexample_verification_state(root, impl, progress)
    assert any(
        "execution-table-incomplete" in reason or "attempts-not-reconciled" in reason
        for reason in blockers
    ), blockers
    old_again = _verify_plan(root, original_path)
    assert old_again.returncode == 1, old_again.stdout + old_again.stderr
    report = json.loads(old_again.stdout)
    # verify 是执行入口；当前诊断已给出阻断，旧表不能借重读重启业务。
    assert report["loop_status"] == "blocked", old_again.stdout
    assert report["blocker"] == "counterexample-superseded-plan-no-business-replay"
    assert report["closed"] is False
    assert (
        set(execution._attempts_dir(root, later_plan).glob("*/intent.json"))
        == after_intents
    )
    with pytest.raises(
        DecisionPreparationError, match="simulation-actual-tasks-not-ready"
    ):
        stage_apply(
            root,
            "implementation",
            {"operation": "seal-for-review", "request_id": "seal"},
        )
    review = _cli(
        root,
        "loop",
        "review",
        "--type",
        "implementation",
        "--loop-id",
        LOOP,
        "--json",
        timeout=180,
    )
    assert review.returncode == 1, review.stdout + review.stderr
    assert not (
        implementation_artifacts(root, LOOP).loop_dir / "review-outcome-round-1.json"
    ).exists()


@pytest.mark.parametrize("mutator", ["observe", "v0"])
def test_actual_observation_or_v0_cannot_change_the_state_claimed_by_v1(
    initialized_project_dir, mutator
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    source = root / "business" / f"{mutator}.py"
    original = source.read_text()
    change = (
        "import json,pathlib,sys\n"
        "resource=pathlib.Path(sys.argv[1])\n"
        "actual=json.loads(resource.read_text())['value']\n"
        "if actual == 'mutant':\n"
        " resource.write_text(json.dumps({'value':'different-bad-state'}))\n"
    )
    if mutator == "observe":
        source.write_text(
            change
            + "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':actual}}))\n"
        )
    else:
        source.write_text(change + original)
    _git(root, "add", "business")
    plan, path = _plan(root, impl, "file_case", f"state-changing-{mutator}")
    verified = _verify_plan(root, path)
    assert verified.returncode == 1, verified.stdout + verified.stderr
    _, bound = _latest_counterexample(root, impl)
    assert not bound.assessment.required_complete
    assert bound.assessment.v1_disposition == "retain_v0"
    attempt = next(
        item
        for item in bound.observations.attempts
        if item.step_id == f"variant-final-{mutator}"
    )
    state_ref = next(
        (
            ref
            for ref in attempt.raw_evidence_refs
            if ref.path.endswith("/resource-state.json")
        ),
        None,
    )
    missing_state = {}
    if state_ref is None:
        # 只展开这次失败动作的真实原件；前置错误不得冒充状态改变已被验证。
        folder = (root / attempt.attempt_ref.path).parent
        names = {
            "intent.json", "process.json", "raw-result.json", "cleanup.json",
            "completion.json", "postcheck.json", "postcheck-error.json",
            "resource-state.json", "resource-ownership.json", "resource-cleanup.json",
            "receipt.json",
        }
        originals = {}
        try:
            for ref in execution.attempt_artifact_refs(root, attempt.attempt_ref):
                artifact = root / ref.path
                if artifact.parent == folder and artifact.name in names:
                    raw = artifact.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != ref.sha256:
                        raise ValueError(f"diagnostic-artifact-drift: {ref.path}")
                    originals[artifact.name] = {
                        "ref": ref.model_dump(mode="json"), "data": json.loads(raw),
                    }
        except (OSError, ValueError) as exc:
            originals["read_error"] = f"{type(exc).__name__}: {exc}"
        missing_state = {
            "attempt": attempt.model_dump(mode="json"),
            "verify_stdout": verified.stdout,
            "verify_stderr": verified.stderr,
            "originals": originals,
        }
    assert state_ref is not None, json.dumps(missing_state, ensure_ascii=False)
    state = json.loads((root / state_ref.path).read_bytes())
    assert state["before"] != state["after"]
    # 收尾可继续，但发生过的持久状态改变不能被后续拒绝覆盖为有效检出。
    cleanup = next(
        item
        for item in bound.observations.attempts
        if item.step_id == "variant-final-cleanup"
    )
    assert cleanup.cleanup_status == "complete"
    variant_step = next(step for step in plan.steps if step.subject_id == "variant")
    assert not Path(variant_step.binding.resources[0].root).exists()


def test_mvp_interrupted_predecessor_is_only_cleaned_before_successor_refusal(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.core.implementation_loop import verify_implementation_task
    from ai_sdlc.core.implementation_models import ImplementationVerifyOptions

    max_attempts = 39
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    old_plan, old_path = _plan(
        root,
        impl,
        "file_case",
        "interrupted-original",
        max_execution_attempts=max_attempts,
    )
    original_execute = execution.execute_counterexample_attempt
    dispatches = []

    def interrupt_after_saved_state(*args, **kwargs):
        dispatches.append(args[3])
        if len(dispatches) == 3:
            raise InterruptedError("controlled stop after actual reset and save")
        return original_execute(*args, **kwargs)

    # 只打断宿主的下一次调度；reset/save 进程、资源认领和结束回执全部真实。
    with monkeypatch.context() as boundary:
        boundary.setattr(
            execution, "execute_counterexample_attempt", interrupt_after_saved_state
        )
        with pytest.raises(InterruptedError, match="after actual reset and save"):
            verify_implementation_task(
                ImplementationVerifyOptions(
                    root=root,
                    loop_id=LOOP,
                    task_id="T11",
                    cwd=".",
                    argv=(),
                    counterexample_plan=old_path,
                )
            )
    assert dispatches == [
        "current-final-reset",
        "current-final-save",
        "current-final-observe",
    ]
    attempts_dir = execution._attempts_dir(root, old_plan)
    old_intents = set(attempts_dir.glob("*/intent.json"))
    assert len(old_intents) == 2
    originals = {
        path: path.read_bytes()
        for intent in old_intents
        for path in intent.parent.iterdir()
        if path.is_file()
    }
    originals[root / old_path] = (root / old_path).read_bytes()
    originals.update(
        {
            root / subject.snapshot.path: (root / subject.snapshot.path).read_bytes()
            for subject in old_plan.subjects
        }
    )
    old_resource = Path(old_plan.steps[0].binding.resources[0].root)
    assert json.loads((old_resource / "state.json").read_bytes()) == {"value": "saved"}
    context_path = (
        implementation_artifacts(root, LOOP).loop_dir / "decision-context.json"
    )
    original_context = json.loads(context_path.read_bytes())
    mother_source = root / "business/save.py"
    mother_source.write_bytes(
        mother_source.read_bytes() + "\n# 当前母候选的独立修改\n".encode()
    )
    _git(root, "add", "business/save.py")
    new_plan, new_path = _plan(
        root,
        impl,
        "file_case",
        "current-after-interruption",
        max_execution_attempts=max_attempts,
    )
    assert new_plan.candidate_digest != old_plan.candidate_digest
    assert new_plan.max_execution_attempts == old_plan.max_execution_attempts
    current_resources = {
        Path(step.binding.resources[0].root) for step in new_plan.steps
    }
    result = _verify_plan(root, new_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "counterexample-unfinished-predecessor-no-takeover" in result.stdout + result.stderr
    all_intents = set(attempts_dir.glob("*/intent.json"))
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    added = [json.loads(path.read_bytes()) for path in all_intents - old_intents]
    assert len(added) == 1
    assert added[0]["plan_digest"] == counterexample_digest(old_plan)
    assert added[0]["step_id"] == "current-final-cleanup"
    assert added[0]["historical_cleanup_only"] is True
    assert not old_resource.exists()
    assert not (attempts_dir.parent / "plans" / f"{counterexample_digest(new_plan)}.json").exists()
    assert not list((attempts_dir.parent / "results").glob("record-reconciled-*.json"))
    for resource in current_resources:
        assert not (resource / ".ai-sdlc-owner.json").exists()
        assert json.loads((resource / "state.json").read_bytes()) == {"value": "initial"}
    current_context = json.loads(context_path.read_bytes())
    assert current_context["started_at_ms"] == original_context["started_at_ms"]
    assert current_context["contracts"] == original_context["contracts"]
    history, debts = execution._historical_resource_cycles(root, new_plan)
    assert not debts
    assert len(history) == 3
    assert all(receipt.cleanup_status == "complete" for _, _, _, receipt in history)

    # 保留真实当前样例供完整读取诊断；这里不伪造正式评审或 Close。


def test_original_repair_round_runs_reserved_real_steps_and_closes_once(
    initialized_project_dir, monkeypatch,
):
    from ai_sdlc.core import loop_decision_service as decision

    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    plan, path = _plan(root, impl, "file_case", "native-r2")
    original_plan_bytes = (root / path).read_bytes()
    first = _verify_plan(root, path)
    assert first.returncode == 0, first.stdout + first.stderr
    # final 已真实清理；R2 必须沿冻结表中的 reset 重建，不能凭首次准入重获归属。
    resource_paths = {
        Path(resource.root)
        for step in plan.steps
        for resource in step.binding.resources
    }
    assert all(not resource.exists() for resource in resource_paths)
    assert _ordinary(root, "v1").returncode == 0
    done = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--json",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    stage_apply(
        root, "implementation", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    actual = actual_record(
        root, "implementation", reviewed, status="UNKNOWN", timeout=180
    )
    assert actual["status"] == "needs_fix", actual
    loop_dir = implementation_artifacts(root, LOOP).loop_dir
    r1_path = loop_dir / "review-outcome-round-1.json"
    r1_bytes = r1_path.read_bytes()
    assert json.loads(r1_bytes)["simulation"]["decision"]["action"] == "repair"
    # 原生 R1 已要求修复；不能删掉其捕获原件，把尚未执行 R2 的 final 成果当通过。
    before_progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    blockers, _, references = execution.counterexample_verification_state(
        root, impl, before_progress
    )
    assert blockers
    captured = {ref.path: (root / ref.path).read_bytes() for ref in references}
    r1_key = r1_path.relative_to(root).as_posix()
    assert captured[r1_key] == r1_bytes
    assert execution.counterexample_verification_state(
        root, impl, before_progress, captured_artifacts=captured
    )[0]
    incomplete = {key: value for key, value in captured.items() if key != r1_key}
    unchanged_intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    stable_read = execution.read_stable_bytes

    def require_captured_r1(evidence_root, path):
        if Path(path) == r1_path:
            pytest.fail("missing captured R1 must not be replaced from the live file")
        return stable_read(evidence_root, path)

    with monkeypatch.context() as capture_boundary:
        capture_boundary.setattr(execution, "read_stable_bytes", require_captured_r1)
        with pytest.raises(ValueError, match="captured-review-phase"):
            execution.counterexample_verification_state(
                root, impl, before_progress, captured_artifacts=incomplete
            )
        from ai_sdlc.core.implementation_loop import _counterexample_state

        rejected, _, _ = _counterexample_state(root, impl, before_progress, incomplete)
        assert any("captured-review-phase" in issue for issue in rejected)
    assert set(execution._attempts_dir(root, plan).glob("*/intent.json")) == unchanged_intents
    assert r1_path.read_bytes() == r1_bytes
    context_bytes = (loop_dir / "decision-context.json").read_bytes()
    original_budget = json.loads(context_bytes)
    # 保留原 R1 输入原件；R2 的合成输入仍经已有实际评审入口，不覆盖历史结论。
    _preserve_r1_inputs(root, loop_dir)
    r1_originals = {
        saved: saved.read_bytes()
        for saved in (loop_dir / "counterexamples/r1-originals").iterdir()
    }
    before_progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    before_task = next(task for task in before_progress.tasks if task.task_id == "T11")
    assert before_task.status == "done" and before_task.quality_results
    assert decision.implementation_execution_started(root, LOOP)
    assert decision.implementation_stage_host(root, loop_id=LOOP).execution_started
    before_intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    second = _verify_plan(root, path)
    assert second.returncode == 0, second.stdout + second.stderr
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    after_task = next(task for task in progress.tasks if task.task_id == "T11")
    assert after_task.status == before_task.status
    assert after_task.quality_results == before_task.quality_results
    assert all(saved.read_bytes() == raw for saved, raw in r1_originals.items())
    assert decision.implementation_execution_started(root, LOOP)
    assert decision.implementation_stage_host(root, loop_id=LOOP).execution_started
    reference = after_task.counterexample_results[-1]
    bound = execution.resolve_counterexample_evidence(root, impl, reference)
    assert bound.assessment.required_complete
    r2_ids = {step.id for step in plan.steps if step.phase == "r2"}
    assert r2_ids <= {
        attempt.step_id
        for attempt in bound.observations.attempts
        if attempt.status == "completed" and attempt.cleanup_status == "complete"
    }
    by_step = {attempt.step_id: attempt for attempt in bound.observations.attempts}
    for subject in plan.subjects:
        original_reset = by_step[f"{subject.id}-final-reset"]
        cleanup = by_step[f"{subject.id}-final-cleanup"]
        restored = by_step[f"{subject.id}-r2-reset"]
        later_use = by_step[f"{subject.id}-r2-save"]
        first_owner = next(
            ref for ref in original_reset.raw_evidence_refs if "/resource-owner-" in ref.path
        )
        restored_owner = next(
            ref for ref in restored.raw_evidence_refs if "/resource-owner-" in ref.path
        )
        assert restored_owner != first_owner
        restored_claim = json.loads((root / restored_owner.path).read_bytes())
        assert restored_claim["attempt_ref"] == restored.attempt_ref.model_dump()
        reset_state_ref = next(
            ref for ref in restored.raw_evidence_refs if ref.path.endswith("/reset-state.json")
        )
        reset_state = json.loads((root / reset_state_ref.path).read_bytes())
        assert reset_state["expected"] == reset_state["actual"]
        ordinals = [
            json.loads((root / receipt.attempt_ref.path).read_bytes())["attempt_ordinal"]
            for receipt in (cleanup, restored, later_use)
        ]
        assert ordinals[0] < ordinals[1] < ordinals[2]
    assert (root / path).read_bytes() == original_plan_bytes
    assert all(not resource.exists() for resource in resource_paths)
    after_intents = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    assert len(after_intents - before_intents) == len(r2_ids)
    assert r1_path.read_bytes() == r1_bytes
    current_budget = json.loads((loop_dir / "decision-context.json").read_bytes())
    assert current_budget["started_at_ms"] == original_budget["started_at_ms"]
    assert current_budget["contracts"] == original_budget["contracts"]
    assert _ordinary(root, "v1").returncode == 0
    r2_review = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    assert r2_review["round_number"] == 2
    assert (
        actual_record(root, "implementation", r2_review, timeout=180)["status"]
        == "passed"
    )
    _assert_fix25_native_review_capture(root, r2_review)
    close = _cli(
        root,
        "loop",
        "implementation",
        "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        r2_review["input_digest"],
        "--yes",
        "--json",
        timeout=180,
    )
    assert close.returncode == 0, close.stdout + close.stderr
    assert r1_path.read_bytes() == r1_bytes
    assert not (loop_dir / "review-outcome-round-3.json").exists()
    assert all(saved.read_bytes() == raw for saved, raw in r1_originals.items())
    _assert_closed_readonly(root)


@pytest.mark.parametrize("kind", ["file_case", "sqlite_case"])
def test_two_task_scopes_share_budget_and_complete_native_resource_r2(
    initialized_project_dir, kind
):
    """原生组合链覆盖归属、同名控制、R2中间资源和最终Close；评审数据为合成输入。"""
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, kind, multi_task=True)
    task_dirs = {"T11": "business", "T21": "business_second"}
    if kind == "sqlite_case":
        # 本例从正确的双任务母本开始，原SQLite回滚修复另有完整专用用例。
        for directory in task_dirs.values():
            source = root / directory / "save.py"
            source.write_text(
                source.read_text().replace("COMMIT = False", "COMMIT = True")
            )
        _git(root, "add", ".")
    plans = {}
    for task_id, directory in task_dirs.items():
        plans[task_id] = _plan(
            root,
            impl,
            kind,
            f"two-task-{task_id}",
            task_id=task_id,
            business_dir=directory,
            obligation_id="save" if task_id == "T11" else "save-second",
        )
    first_plan = plans["T11"][0]
    second_plan = plans["T21"][0]
    assert set(first_plan.allowed_modified_paths).isdisjoint(
        second_plan.allowed_modified_paths
    )
    assert first_plan.v1_digest == second_plan.v1_digest
    assert (
        first_plan.max_execution_attempts == second_plan.max_execution_attempts == 108
    )
    assert {s.id for s in first_plan.subjects} == {s.id for s in second_plan.subjects}
    loop_dir = implementation_artifacts(root, LOOP).loop_dir
    original_context = json.loads((loop_dir / "decision-context.json").read_bytes())

    def current(task_id):
        progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
        references = next(
            t for t in progress.tasks if t.task_id == task_id
        ).counterexample_results
        assert references, progress
        return execution.resolve_counterexample_evidence(root, impl, references[-1])

    # 第一任务的局部成功不可掩盖第二任务缺证据；两者仍须能连续执行。
    for task_id, (_, path) in plans.items():
        result = _verify_plan(root, path, task_id=task_id)
        assert result.returncode in (0, 1), result.stdout + result.stderr
        bound = current(task_id)
        assert bound.assessment.required_complete, (result.stdout, bound.assessment)
        assert {item.obligation_id for item in bound.assessment.current_subjects} == {
            "save" if task_id == "T11" else "save-second"
        }
        if task_id == "T11":
            progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
            assert execution.counterexample_verification_state(root, impl, progress)[0]
        else:
            assert result.returncode == 0, result.stdout + result.stderr
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert not execution.counterexample_verification_state(root, impl, progress)[0]
    for task_id, directory in task_dirs.items():
        ordinary = _ordinary(root, "v1", task_id=task_id, business_dir=directory)
        assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
        done = _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            LOOP,
            "--task-id",
            task_id,
            "--status",
            "done",
            "--evidence",
            f"{directory}/save.py",
            "--json",
            timeout=180,
        )
        assert done.returncode == 0, done.stdout + done.stderr

    stage_apply(
        root, "implementation", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    assert (
        actual_record(root, "implementation", reviewed, status="UNKNOWN", timeout=180)[
            "status"
        ]
        == "needs_fix"
    )
    r1_path = loop_dir / "review-outcome-round-1.json"
    r1_original = r1_path.read_bytes()
    assert json.loads(r1_original)["simulation"]["decision"]["action"] == "repair"
    _preserve_r1_inputs(root, loop_dir)
    attempts = execution._attempts_dir(root, first_plan)
    assert attempts == execution._attempts_dir(root, second_plan)
    first_originals = {
        p: p.read_bytes()
        for intent in attempts.glob("*/intent.json")
        for p in intent.parent.iterdir()
        if p.is_file()
    }
    final_count = len(list(attempts.glob("*/intent.json")))
    assert final_count == 36
    for task_id, (plan, path) in plans.items():
        result = _verify_plan(root, path, task_id=task_id)
        assert result.returncode in (0, 1), result.stdout + result.stderr
        bound = current(task_id)
        assert bound.assessment.required_complete, (result.stdout, bound.assessment)
        assert {step.id for step in plan.steps if step.phase == "r2"} <= {
            receipt.step_id
            for receipt in bound.observations.attempts
            if receipt.status == "completed" and receipt.cleanup_status == "complete"
        }
        captured = {
            ref.path: (root / ref.path).read_bytes() for ref in bound.artifact_refs
        }
        progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
        reference = next(
            t for t in progress.tasks if t.task_id == task_id
        ).counterexample_results[-1]
        assert (
            execution.resolve_counterexample_evidence(
                root, impl, reference, captured_artifacts=captured
            ).assessment
            == bound.assessment
        )
        if task_id == "T21":
            assert result.returncode == 0, result.stdout + result.stderr
    assert all(path.read_bytes() == raw for path, raw in first_originals.items())
    actual_intents = [
        json.loads(path.read_bytes()) for path in attempts.glob("*/intent.json")
    ]
    assert len(actual_intents) == 72
    assert sorted(row["attempt_ordinal"] for row in actual_intents) == list(
        range(1, 73)
    )
    assert r1_path.read_bytes() == r1_original
    context = json.loads((loop_dir / "decision-context.json").read_bytes())
    assert context["started_at_ms"] == original_context["started_at_ms"]
    assert context["contracts"] == original_context["contracts"]
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert not execution.counterexample_verification_state(root, impl, progress)[0]
    for task_id, directory in task_dirs.items():
        assert (
            _ordinary(root, "v1", task_id=task_id, business_dir=directory).returncode
            == 0
        )
    r2 = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    assert r2["round_number"] == 2
    assert actual_record(root, "implementation", r2, timeout=180)["status"] == "passed"
    closed = _cli(
        root,
        "loop",
        "implementation",
        "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        r2["input_digest"],
        "--yes",
        "--json",
        timeout=180,
    )
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert r1_path.read_bytes() == r1_original
    assert not (loop_dir / "review-outcome-round-3.json").exists()
    _assert_closed_readonly(root)


def _completed_unaggregated_final(root, impl, plan):
    """真实意图和回执已经完成，尚未进入汇总持久化的中断窗口。"""
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample

    contract, _ = validate_implementation_verification_contract(root, impl)
    prefix = execution._attempts_dir(root, plan).parent
    execution._write_json(
        root, prefix / f"plans/{counterexample_digest(plan)}.json", plan
    )
    receipts = [
        execution.execute_counterexample_attempt(
            root,
            contract,
            plan,
            step.id,
            deadline_ms=time.time_ns() // 1_000_000 + 10_000_000,
        )
        for step in plan.steps
        if step.phase == "final"
    ]
    assert all(
        item.status == "completed" and item.cleanup_status == "complete"
        for item in receipts
    )
    assert not execution._historical_resource_cycles(root, plan)[1]
    bundle = execution._bundle_from_attempts(root, contract, plan, receipts)
    assessment = evaluate_counterexample(contract, plan, bundle, require_r2=False)
    originals = {
        path: path.read_bytes() for path in prefix.rglob("*") if path.is_file()
    }
    return assessment, originals


def test_unaggregated_business_failure_refuses_successor_and_remains_readable(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "sqlite_case")
    original, _ = _plan(root, impl, "sqlite_case", "unaggregated-failure")
    before, originals = _completed_unaggregated_final(root, impl, original)
    assert before.current_result.status == "FAIL"
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert not any(task.counterexample_results for task in progress.tasks)
    source = root / "business/save.py"
    source.write_text(source.read_text().replace("COMMIT = False", "COMMIT = True"))
    successor, path = _plan(root, impl, "sqlite_case", "repaired-successor")
    attempts = execution._attempts_dir(root, original)
    original_intents = set(attempts.glob("*/intent.json"))
    result = _verify_plan(root, path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "counterexample-unfinished-predecessor-no-takeover" in result.stdout + result.stderr
    assert set(attempts.glob("*/intent.json")) == original_intents
    assert not (attempts.parent / "plans" / f"{counterexample_digest(successor)}.json").exists()
    assert not list((attempts.parent / "results").glob("record-reconciled-*.json"))
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    live = execution.counterexample_verification_state(root, impl, progress, require_r2=False)
    assert any("business" in item.lower() and ("fail" in item.lower() or "repair" in item.lower()) for item in live[0])
    captured = {ref.path: (root / ref.path).read_bytes() for ref in live[2]}
    cold = execution.counterexample_verification_state(root, impl, progress, captured_artifacts=captured, require_r2=False)
    assert cold[0] == live[0]
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not implementation_artifacts(root, LOOP).close_path.exists()


def test_unaggregated_control_rejection_survives_same_candidate_replay(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    acceptance = root / "business/v1.py"
    acceptance.write_text(
        acceptance.read_text().replace(
            "accepted = value == sys.argv[2]",
            "accepted = value == sys.argv[2] and not (pathlib.Path.cwd().name == 'positive_control' and 'old-controls' in pathlib.Path.cwd().parent.name)",
        )
    )
    original, _ = _plan(root, impl, "file_case", "old-controls")
    before, originals = _completed_unaggregated_final(root, impl, original)
    assert before.current_result.status == "PASS"
    assert any(control.v1 == "false_rejection" for control in before.positive_controls)
    successor, path = _plan(root, impl, "file_case", "replayed-controls")
    assert successor.candidate_digest == original.candidate_digest
    result = _verify_plan(root, path)
    assert result.returncode == 1, (
        "same-source replay must retain the original legitimate-control rejection"
    )
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    blockers, _, _ = execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )
    assert any("legitimate control" in item for item in blockers)
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_unaggregated_pass_does_not_authorize_successor_after_source_change(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    original, _ = _plan(root, impl, "file_case", "unaggregated-pass")
    before, originals = _completed_unaggregated_final(root, impl, original)
    assert before.current_result.status == "PASS"
    source = root / "business/save.py"
    source.write_text(source.read_text() + "\n# 保持业务语义的后继候选。\n")
    successor, path = _plan(root, impl, "file_case", "pass-successor")
    attempts = execution._attempts_dir(root, original)
    original_intents = set(attempts.glob("*/intent.json"))
    result = _verify_plan(root, path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "counterexample-unfinished-predecessor-no-takeover" in result.stdout + result.stderr
    assert set(attempts.glob("*/intent.json")) == original_intents
    assert not (attempts.parent / "plans" / f"{counterexample_digest(successor)}.json").exists()
    assert not list((attempts.parent / "results").glob("record-reconciled-*.json"))
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    live = execution.counterexample_verification_state(root, impl, progress, require_r2=False)
    assert live[0]
    assert all(witness.input_ref in live[2] for witness in original.witnesses)
    captured = {ref.path: (root / ref.path).read_bytes() for ref in live[2]}
    assert execution.counterexample_verification_state(root, impl, progress, captured_artifacts=captured, require_r2=False)[0] == live[0]
    for witness in original.witnesses:
        missing = dict(captured)
        missing.pop(witness.input_ref.path)
        assert any(witness.input_ref.path in item for item in execution.counterexample_verification_state(root, impl, progress, captured_artifacts=missing, require_r2=False)[0])
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_failed_reset_keeps_native_owned_cleanup_available(initialized_project_dir):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    plan, _ = _plan(root, impl, "file_case", "bad-reset")
    data = plan.model_dump(mode="json")
    reset = next(step for step in data["steps"] if step["id"] == "current-final-reset")
    endpoint = reset["binding"]["resources"][0]["observation_endpoint"]
    reset["binding"]["argv"] = [
        sys.executable,
        "-c",
        'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(\'{"value":"stale"}\')',
        endpoint,
    ]
    plan = CounterexamplePlan.model_validate(data)
    path = _json_ref(root, "bad-reset/bound-plan.json", plan.model_dump(mode="json"))[
        "path"
    ]
    result = _verify_plan(root, path)
    assert result.returncode == 1, (
        "a successful command is not proof that reset restored the frozen state"
    )
    receipts = execution._recover_plan_attempts(root, plan)
    current = {item.step_id: item for item in receipts if item.subject_id == "current"}
    assert current["current-final-reset"].status == "infrastructure_error"
    assert "current-final-save" not in current
    assert current["current-final-cleanup"].status == "completed"
    assert current["current-final-cleanup"].cleanup_status == "complete"
    assert not Path(endpoint).parent.exists()


def test_removed_reconciliation_writer_cannot_create_a_successor_record(
    initialized_project_dir,
):
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    original, _ = _plan(root, impl, "file_case", "backtrack-original")
    before, originals = _completed_unaggregated_final(root, impl, original)
    assert before.required_complete
    assert not hasattr(execution, "_reconcile_historical_attempts")
    source = root / "business/save.py"
    source.write_bytes(source.read_bytes() + "\n# 未归集的旧结果不授权后继。\n".encode())
    successor, path = _plan(root, impl, "file_case", "backtrack-draft-successor")
    attempts = execution._attempts_dir(root, original)
    original_intents = set(attempts.glob("*/intent.json"))
    result = _verify_plan(root, path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "counterexample-unfinished-predecessor-no-takeover" in result.stdout + result.stderr
    assert not (attempts.parent / "plans" / f"{counterexample_digest(successor)}.json").exists()
    assert not list((attempts.parent / "results").glob("record-reconciled-*.json"))
    assert set(attempts.glob("*/intent.json")) == original_intents
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert execution.counterexample_verification_state(root, impl, progress, require_r2=False)[0]
    assert not implementation_artifacts(root, LOOP).close_path.exists()


def _start_pending_counterexample_lifecycle(root, monkeypatch):
    _prepare_project(root, "file_case")
    original_candidate = candidate_data

    def candidate(contract, candidate_id="A", seconds=(800, 900)):
        return original_candidate(contract, candidate_id, seconds=seconds)

    with monkeypatch.context() as m:
        m.setattr(sys.modules[__name__], "candidate_data", candidate)
        for stage in ("requirement", "design-contract", "implementation"):
            if stage == "requirement":
                args = [
                    "start",
                    "--idea",
                    "持久保存并从新进程读取",
                    "--acceptance",
                    "新进程读取 saved",
                    "--work-item-id",
                    "demo-implementation-loop",
                    "--design-scope-family",
                    "implementation",
                ]
            elif stage == "design-contract":
                args = [
                    "check",
                    "--wi",
                    WORK_ITEM,
                    "--requirement-loop-id",
                    LOOP,
                    "--verification-contract",
                    WORK_ITEM + "/verification.json",
                ]
            else:
                args = ["start", "--wi", WORK_ITEM, "--design-contract-loop-id", LOOP]
            result = _cli(
                root,
                "loop",
                stage,
                *args,
                "--loop-id",
                LOOP,
                "--decision-mode",
                "adaptive-quantified",
                "--decision-capability",
                CAPABILITY,
                "--json",
            )
            assert result.returncode == 0, result.stdout + result.stderr
            _select(root, stage)
            if stage != "implementation":
                _review_close(root, stage)
    # 直接 verify 是受支持入口；这里明确不调用可选的 record in_progress。
    shutil.copyfile(FIXTURES / "file_case" / "v1.py", root / "business/v1.py")
    _git(root, "add", ".")
    return read_input(implementation_artifacts(root, LOOP).input_path)


def test_pending_counterexample_plan_retains_started_state_across_steps(
    initialized_project_dir, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ai_sdlc.core import loop_decision_service as decision
    from ai_sdlc.core.implementation_loop import (
        ImplementationVerifyOptions,
        verify_implementation_task,
    )

    root = initialized_project_dir
    log = {
        "kind": "controlled-admission-clock-mechanism-reproduction",
        "production_evidence": False,
        "observations": [],
    }

    def save():
        (tmp_path / "diagnostic.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=2) + "\n"
        )

    started = time.monotonic()
    try:
        impl = _start_pending_counterexample_lifecycle(root, monkeypatch)
        artifacts = implementation_artifacts(root, LOOP)
        progress = read_progress(artifacts.progress_path)
        assert all(
            p.status == "pending"
            and not p.quality_results
            and not p.counterexample_results
            for p in progress.tasks
        )
        plan, path = _plan(root, impl, "file_case", "pending-boundary")
        context_path = artifacts.loop_dir / "decision-context.json"
        original_context = context_path.read_bytes()
        context = decision.parse_implementation_context(original_context)
        window = int(context.plan.time_plan.window_seconds)
        upper = int(context.selected_candidate.future_cost_estimate.upper_seconds)
        assert upper == 900 and window == 3600
        now = {"ms": context.started_at_ms + (window - upper - 1) * 1000}
        # 不改 context/成本/原起点，仅替换被测准入函数所读时钟；进程和 receipt 使用实际时钟。
        monkeypatch.setattr(
            decision, "time", SimpleNamespace(time_ns=lambda: now["ms"] * 1000000)
        )
        log["frozen"] = {
            "context_path": str(context_path),
            "context_sha256": hashlib.sha256(original_context).hexdigest(),
            "started_at_ms": context.started_at_ms,
            "window_seconds": window,
            "initial_future_cost_upper_seconds": upper,
            "max_execution_attempts": plan.max_execution_attempts,
            "required_reserve_seconds": plan.required_reserve_seconds,
            "before_elapsed_seconds": window - upper - 1,
            "after_elapsed_seconds": window - upper + 1,
        }
        log["pristine_predicates"] = {
            "helper": decision.implementation_execution_started(root, LOOP),
            "host": decision.implementation_stage_host(
                root, loop_id=LOOP
            ).execution_started,
        }
        assert log["pristine_predicates"] == {"helper": False, "host": False}
        now["ms"] += 2000
        with pytest.raises(ValueError, match="simulation-model-plan-not-feasible"):
            decision.require_simulation_time_admission(context, execution_started=False)
        log["fresh_over_quote_rejected"] = True
        now["ms"] -= 2000
        original_execute = execution.execute_counterexample_attempt
        first_originals = {}

        def observed_execute(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            log["observations"].append(
                {
                    "step_id": args[3],
                    "status": result.status,
                    "cleanup_status": result.cleanup_status,
                    "attempt_id": result.attempt_id,
                }
            )
            if len(log["observations"]) == 1:
                assert (
                    result.status == "completed" and result.cleanup_status == "complete"
                )
                now["ms"] += 2000
                progress = read_progress(artifacts.progress_path)
                log["after_real_reset_predicates"] = {
                    "helper": decision.implementation_execution_started(root, LOOP),
                    "host": decision.implementation_stage_host(
                        root, loop_id=LOOP
                    ).execution_started,
                }
                log["after_real_reset_progress"] = progress.model_dump(mode="json")
                for intent in execution._attempts_dir(root, plan).glob("*/intent.json"):
                    for raw in intent.parent.iterdir():
                        if raw.is_file():
                            first_originals[raw] = raw.read_bytes()
                save()
            return result

        monkeypatch.setattr(
            execution, "execute_counterexample_attempt", observed_execute
        )
        options = ImplementationVerifyOptions(
            root=root,
            loop_id=LOOP,
            task_id="T11",
            cwd=".",
            argv=(),
            counterexample_plan=path,
        )
        blocked = verify_implementation_task(options)
        log["blocked_result"] = blocked.model_dump(mode="json")
        log["actual_first_attempt_count"] = len(
            list(execution._attempts_dir(root, plan).glob("*/intent.json"))
        )
        log["resource_after_block"] = [
            {
                "path": r.binding.resources[0].root,
                "exists": __import__("pathlib")
                .Path(r.binding.resources[0].root)
                .exists(),
                "owned_marker": (
                    __import__("pathlib").Path(r.binding.resources[0].root)
                    / ".ai-sdlc-owner.json"
                ).exists(),
            }
            for r in plan.steps
            if r.id == "current-final-reset"
        ]
        assert not blocked.blocker, blocked.blocker
        assert log["after_real_reset_predicates"] == {"helper": True, "host": True}
        assert log["after_real_reset_progress"]["tasks"][0]["status"] == "in_progress"
        assert all(
            not item.get("counterexample_results")
            for item in log["after_real_reset_progress"]["tasks"]
        )
        assert decision.implementation_execution_started(root, LOOP)
        saved_now = now["ms"]
        now["ms"] = context.started_at_ms + window * 1000
        with pytest.raises(ValueError, match="simulation-model-plan-not-feasible"):
            decision.require_simulation_time_admission(context, execution_started=True)
        now["ms"] = saved_now
        log["expired_started_control_rejected"] = True
        receipts = execution._recover_plan_attempts(root, plan)
        log["final_receipts"] = [
            {
                "step_id": r.step_id,
                "status": r.status,
                "cleanup_status": r.cleanup_status,
            }
            for r in receipts
        ]
        assert len(receipts) == 18
        assert all(
            r.status == "completed" and r.cleanup_status == "complete" for r in receipts
        )
        resources = {r.binding.resources[0].root for r in plan.steps}
        log["resources_after_control"] = {
            p: __import__("pathlib").Path(p).exists() for p in sorted(resources)
        }
        assert not any(log["resources_after_control"].values())
        assert all(p.read_bytes() == b for p, b in first_originals.items())
        assert context_path.read_bytes() == original_context
        log["first_attempt_originals_unchanged"] = True
        log["original_context_unchanged"] = True
        log["final_progress"] = read_progress(artifacts.progress_path).model_dump(
            mode="json"
        )
        assert log["final_progress"]["tasks"][0]["status"] == "in_progress"
        assert any(
            p.get("counterexample_results") for p in log["final_progress"]["tasks"]
        )
    finally:
        log["elapsed_seconds"] = time.monotonic() - started
        save()


def test_pending_counterexample_completed_history_recovers_without_recharging(
    initialized_project_dir, monkeypatch
):
    from types import SimpleNamespace

    from ai_sdlc.core import loop_decision_service as decision
    from ai_sdlc.core.implementation_loop import (
        ImplementationVerifyOptions,
        verify_implementation_task,
    )
    from ai_sdlc.core.implementation_store import read_loop_run

    root = initialized_project_dir
    impl = _start_pending_counterexample_lifecycle(root, monkeypatch)
    artifacts = implementation_artifacts(root, LOOP)
    plan, path = _plan(root, impl, "file_case", "legacy-pending-history")
    context_path = artifacts.loop_dir / "decision-context.json"
    original_context = context_path.read_bytes()
    context = decision.parse_implementation_context(original_context)
    now = {"ms": context.started_at_ms + 2699000}
    monkeypatch.setattr(
        decision, "time", SimpleNamespace(time_ns=lambda: now["ms"] * 1000000)
    )
    loop_run = read_loop_run(artifacts.loop_run_path)
    decision.validate_implementation_context(root, loop_run, impl, purpose="execute")
    # 既有下层 writer 形成真实 v32 样式原历史：有完整 reset，尚无进度引用或状态回调。
    execution._write_json(
        root,
        execution._attempts_dir(root, plan).parent
        / "plans"
        / f"{counterexample_digest(plan)}.json",
        plan,
    )
    receipt = execution.execute_counterexample_attempt(
        root,
        validate_implementation_verification_contract(root, impl)[0],
        plan,
        "current-final-reset",
        deadline_ms=context.started_at_ms + 3600000,
        remaining_reserve_seconds=453,
    )
    assert receipt.status == "completed" and receipt.cleanup_status == "complete"
    progress = read_progress(artifacts.progress_path)
    assert (
        progress.tasks[0].status == "pending"
        and not progress.tasks[0].counterexample_results
    )
    originals = {
        raw: raw.read_bytes()
        for raw in (root / receipt.attempt_ref.path).parent.iterdir()
        if raw.is_file()
    }
    now["ms"] += 2000
    assert decision.implementation_execution_started(root, LOOP)
    assert decision.implementation_stage_host(root, loop_id=LOOP).execution_started
    result = verify_implementation_task(
        ImplementationVerifyOptions(
            root=root,
            loop_id=LOOP,
            task_id="T11",
            cwd=".",
            argv=(),
            counterexample_plan=path,
        )
    )
    assert not result.blocker, result.blocker
    final = read_progress(artifacts.progress_path)
    assert (
        final.tasks[0].status == "in_progress" and final.tasks[0].counterexample_results
    )
    assert len(execution._recover_plan_attempts(root, plan)) == 18
    assert all(raw.read_bytes() == content for raw, content in originals.items())
    assert context_path.read_bytes() == original_context
    assert all(not Path(step.binding.resources[0].root).exists() for step in plan.steps)


def test_weekend_two_task_business_repairs_share_candidate_and_close(
    initialized_project_dir,
):
    """业务/命令/读取/Close均真实；复用既有合成专家输入，不宣称真实模型评审或收益。"""
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "sqlite_case", multi_task=True)
    directories = {"T11": "business", "T21": "business_second"}
    before = {}
    originals = {}
    first_candidate = source_digest_sha256(build_source_digest(root))

    def latest(task_id):
        progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
        reference = next(
            task for task in progress.tasks if task.task_id == task_id
        ).counterexample_results[-1]
        return reference, execution.resolve_counterexample_evidence(
            root, impl, reference
        )

    # 普通V0在首次CE失败前分别完成；之后整体门禁不得被另一任务命令成功清除。
    for task_id, directory in directories.items():
        assert (
            _ordinary(root, "v0", task_id=task_id, business_dir=directory).returncode
            == 0
        )

    # 两个固定任务分别真实失败；不能用另一任务的全仓变化替代自己的业务修复。
    for task_id, directory in directories.items():
        plan, path = _plan(
            root,
            impl,
            "sqlite_case",
            f"weekend-before-{task_id}",
            task_id=task_id,
            business_dir=directory,
            obligation_id="save" if task_id == "T11" else "save-second",
        )
        assert plan.candidate_digest == first_candidate
        result = _verify_plan(root, path, task_id=task_id)
        assert result.returncode == 1, result.stdout + result.stderr
        reference, bound = latest(task_id)
        assert bound.assessment.current_result.status == "FAIL", bound.assessment
        assert all(
            control.business.status == "PASS"
            for control in bound.assessment.positive_controls
        )
        before[task_id] = (reference, bound)
        for ref in bound.artifact_refs:
            originals[ref.path] = (root / ref.path).read_bytes()
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )[0]

    for directory in directories.values():
        source = root / directory / "save.py"
        assert "COMMIT = False" in source.read_text()
        source.write_text(source.read_text().replace("COMMIT = False", "COMMIT = True"))
    _git(root, "add", "business/save.py", "business_second/save.py")
    repaired_candidate = source_digest_sha256(build_source_digest(root))
    assert repaired_candidate != first_candidate
    after = {}
    for task_id, directory in directories.items():
        plan, path = _plan(
            root,
            impl,
            "sqlite_case",
            f"weekend-after-{task_id}",
            task_id=task_id,
            business_dir=directory,
            obligation_id="save" if task_id == "T11" else "save-second",
        )
        assert plan.candidate_digest == repaired_candidate
        result = _verify_plan(root, path, task_id=task_id)
        assert result.returncode in (0, 1), result.stdout + result.stderr
        reference, bound = latest(task_id)
        assert bound.plan.candidate_digest == repaired_candidate, (
            result.stdout + result.stderr
        )
        assert bound.assessment.required_complete, (result.stdout, bound.assessment)
        assert bound.assessment.current_result.status == "PASS"
        assert all(
            control.business.status == "PASS"
            for control in bound.assessment.positive_controls
        )
        old_ref, old_bound = before[task_id]
        assert len(bound.observations.repairs) == 1
        repair = bound.observations.repairs[0]
        proof = json.loads((root / repair.repair_ref.path).read_bytes())
        assert [change["path"] for change in proof["changes"]] == [
            f"{directory}/save.py"
        ]
        assert proof["before_record_ref"] == old_ref.model_dump(mode="json")
        assert (
            repair.before_observation_refs
            == old_bound.assessment.current_result.evidence_refs
        )
        assert (
            repair.after_observation_refs
            == bound.assessment.current_result.evidence_refs
        )
        captured = {
            ref.path: (root / ref.path).read_bytes() for ref in bound.artifact_refs
        }
        cold = execution.resolve_counterexample_evidence(
            root, impl, reference, captured_artifacts=captured
        )
        assert cold.assessment == bound.assessment
        assert cold.observations.repairs == bound.observations.repairs
        # 完整capture必须独立消费原失败；缺件时不可从仍存在的磁盘原件补成功。
        missing = dict(captured)
        missing.pop(old_ref.path)
        with pytest.raises(ValueError):
            execution.resolve_counterexample_evidence(
                root, impl, reference, captured_artifacts=missing
            )
        after[task_id] = (reference, bound, repair)
        progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
        blockers = execution.counterexample_verification_state(
            root, impl, progress, require_r2=False
        )[0]
        if task_id == "T11":
            assert blockers, "尚未重跑的第二任务不能借第一任务修复取得成功"
        else:
            assert not blockers, blockers
            assert result.returncode == 0, result.stdout + result.stderr
    assert all((root / path).read_bytes() == raw for path, raw in originals.items())

    for task_id, directory in directories.items():
        ordinary = _ordinary(root, "v1", task_id=task_id, business_dir=directory)
        assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
        done = _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            LOOP,
            "--task-id",
            task_id,
            "--status",
            "done",
            "--evidence",
            f"{directory}/save.py",
            "--json",
        )
        assert done.returncode == 0, done.stdout + done.stderr
    reviewed = _review_close(root, "implementation")
    for task_id in directories:
        old_ref, _ = before[task_id]
        reference, _, repair = after[task_id]
        assert {old_ref.path, reference.path, repair.repair_ref.path} <= set(
            reviewed["artifact_paths"]
        )
    _assert_closed_readonly(root)
    assert all((root / path).read_bytes() == raw for path, raw in originals.items())


@pytest.mark.parametrize("observer_failure", ["nonzero", "invalid-json"])
def test_weekend_sealed_r2_observer_failure_retains_frozen_cleanup(
    initialized_project_dir, observer_failure
):
    """真实封存准入与进程/清理；专家输入复用既有合成夹具，不替代真实模型评审。"""
    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    # 故障分支先进入各subject的真实源码快照，仅R2的冻结参数选择该分支。
    observer_script = root / "business/observe.py"
    observer_script.write_text(
        "import sys\n"
        "if '--sealed-r2-nonzero' in sys.argv:\n"
        " print('observer failed'); raise SystemExit(7)\n"
        "if '--sealed-r2-invalid-json' in sys.argv:\n"
        " print('not-json'); raise SystemExit(0)\n"
        + observer_script.read_text()
    )
    _git(root, "add", "business/observe.py")
    original, _ = _plan(root, impl, "file_case", "sealed-observer-failure")
    data = original.model_dump(mode="json")
    observer = next(
        step for step in data["steps"] if step["id"] == "current-r2-observe"
    )
    observer["binding"]["argv"].append(f"--sealed-r2-{observer_failure}")
    plan = CounterexamplePlan.model_validate(data)
    execution.validate_plan_contract(
        validate_implementation_verification_contract(root, impl)[0], plan
    )
    reference = _json_ref(root, "sealed-observer-failure-plan.json", data)
    path = reference["path"]
    frozen_plan = (root / path).read_bytes()
    frozen_source = source_digest_sha256(build_source_digest(root))
    # R2故障在首轮执行前已冻结；final仍由原独立observer完成，不改源触发失败。
    first = _verify_plan(root, path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _ordinary(root, "v1").returncode == 0
    done = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "done",
        "--evidence",
        "business/save.py",
        "--json",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    stage_apply(
        root, "implementation", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
            timeout=180,
        )
    )
    actual = actual_record(
        root, "implementation", reviewed, status="UNKNOWN", timeout=180
    )
    assert actual["status"] == "needs_fix", actual
    artifacts = implementation_artifacts(root, LOOP)
    r1_path = artifacts.loop_dir / "review-outcome-round-1.json"
    r1_original = r1_path.read_bytes()
    assert json.loads(r1_original)["simulation"]["decision"]["action"] == "repair"
    original_budget = json.loads(
        (artifacts.loop_dir / "decision-context.json").read_bytes()
    )
    _preserve_r1_inputs(root, artifacts.loop_dir)
    final_receipts = execution._recover_plan_attempts(root, plan)
    assert final_receipts and all(
        next(step for step in plan.steps if step.id == receipt.step_id).phase == "final"
        for receipt in final_receipts
    )
    final_originals = {
        raw_path: raw_path.read_bytes()
        for receipt in final_receipts
        for raw_path in (root / receipt.attempt_ref.path).parent.iterdir()
        if raw_path.is_file()
    }

    second = _verify_plan(root, path)
    assert second.returncode == 1, second.stdout + second.stderr
    receipts = {
        item.step_id: item for item in execution._recover_plan_attempts(root, plan)
    }
    failed = receipts["current-r2-observe"]
    assert failed.exit_code == (7 if observer_failure == "nonzero" else 0)
    assert failed.status == "completed" and failed.cleanup_status == "complete"
    # 旧实现会在sealed execution admission中先拒绝，故看不到本次冻结cleanup。
    assert "current-r2-cleanup" in receipts, second.stdout + second.stderr
    cleanup = receipts["current-r2-cleanup"]
    assert cleanup.status == "completed" and cleanup.cleanup_status == "complete"
    current_step = next(step for step in plan.steps if step.id == "current-r2-cleanup")
    assert all(
        not Path(resource.root).exists() for resource in current_step.binding.resources
    )
    assert not execution._historical_resource_cycles(root, plan)[1]
    record, bound = _latest_counterexample(root, impl)
    assert not bound.assessment.required_complete
    observation = next(
        row
        for row in bound.observations.business
        if row.step_id == "current-r2-observe"
    )
    assert observation.observation_status == "parse_error"
    assert observation.typed_actual is None
    captured = {ref.path: (root / ref.path).read_bytes() for ref in bound.artifact_refs}
    assert (
        execution.resolve_counterexample_evidence(
            root, impl, record, captured_artifacts=captured
        ).assessment
        == bound.assessment
    )
    progress = read_progress(artifacts.progress_path)
    assert execution.counterexample_verification_state(root, impl, progress)[0]
    before_retry = set(execution._attempts_dir(root, plan).glob("*/intent.json"))
    repeated = _verify_plan(root, path)
    assert repeated.returncode == 1, repeated.stdout + repeated.stderr
    assert (
        set(execution._attempts_dir(root, plan).glob("*/intent.json")) == before_retry
    )
    formal = _cli(
        root,
        "loop",
        "review",
        "--type",
        "implementation",
        "--loop-id",
        LOOP,
        "--json",
        timeout=180,
    )
    assert formal.returncode != 0, formal.stdout + formal.stderr
    assert "counterexample" in (formal.stdout + formal.stderr).lower()
    close = _cli(
        root,
        "loop",
        "implementation",
        "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
        timeout=180,
    )
    assert close.returncode != 0, close.stdout + close.stderr
    assert not artifacts.close_path.exists()
    assert all(
        raw_path.read_bytes() == content
        for raw_path, content in final_originals.items()
    )
    assert r1_path.read_bytes() == r1_original
    assert (root / path).read_bytes() == frozen_plan
    assert source_digest_sha256(build_source_digest(root)) == frozen_source
    current_budget = json.loads(
        (artifacts.loop_dir / "decision-context.json").read_bytes()
    )
    assert current_budget["started_at_ms"] == original_budget["started_at_ms"]
    assert current_budget["contracts"] == original_budget["contracts"]


@pytest.mark.parametrize("interruption", ["process_creation", "after_completed_reset"])
def test_same_candidate_interruption_preserves_originals_but_refuses_takeover(
    initialized_project_dir, monkeypatch, interruption
):
    """真实启动失败或已落盘 reset 后中断，仅清理旧资源并保留阻断。"""
    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.implementation_loop import verify_implementation_task
    from ai_sdlc.core.implementation_models import ImplementationVerifyOptions

    root = initialized_project_dir.resolve()
    impl = _start_lifecycle(root, "file_case")
    original, original_path = _plan(root, impl, "file_case", "fix17-original")
    context_path = implementation_artifacts(root, LOOP).loop_dir / "decision-context.json"
    context_before = json.loads(context_path.read_bytes())
    original_popen = quality.subprocess.Popen
    failures = []

    def unavailable_once(*args, **kwargs):
        if (kwargs.get("env") or {}).get(quality._PROCESS_OWNER_ENV) and not failures:
            failures.append("real-process-construction")
            raise OSError("controlled one-time process creation unavailable")
        return original_popen(*args, **kwargs)

    original_execute = execution.execute_counterexample_attempt

    def interrupt_after_reset(*args, **kwargs):
        receipt = original_execute(*args, **kwargs)
        if receipt.step_id == "current-final-reset" and not failures:
            assert receipt.normally_completed
            failures.append("after-real-reset-receipt")
            raise KeyboardInterrupt("controlled interruption after persisted reset")
        return receipt

    options = ImplementationVerifyOptions(
        root=root, loop_id=LOOP, task_id="T11", cwd=".", argv=(),
        counterexample_plan=original_path,
    )
    # 仅在真实边界注入故障，不代写意图、回执、业务聚合或后续清理。
    with monkeypatch.context() as transient:
        if interruption == "process_creation":
            transient.setattr(quality.subprocess, "Popen", unavailable_once)
            verify_implementation_task(options)
        else:
            transient.setattr(execution, "execute_counterexample_attempt", interrupt_after_reset)
            with pytest.raises(KeyboardInterrupt, match="persisted reset"):
                verify_implementation_task(options)
    assert failures == [
        "real-process-construction" if interruption == "process_creation"
        else "after-real-reset-receipt"
    ]
    history, debts = execution._historical_resource_cycles(root, original)
    original_rows = [row for row in history if row[2].id == original.id]
    failed = next(row[3] for row in original_rows if row[1]["step_id"] == "current-final-reset")
    assert failed.cleanup_status == "complete"
    if interruption == "process_creation":
        assert not debts
        assert failed.status == "infrastructure_error"
        assert any(
            row[1]["step_id"] == "current-final-cleanup"
            and row[3].status == "completed" for row in original_rows
        )
        assert all(not Path(step.binding.resources[0].root).exists() for step in original.steps)
    else:
        assert failed.normally_completed and debts
        assert not any(row[1]["step_id"] == "current-final-cleanup" for row in original_rows)
        assert any(Path(step.binding.resources[0].root).exists() for step in original.steps)
        # 主流程未返回，真实 reset 只存在于持久化原件，尚无业务结果可被沿用。
        assert not list((execution._attempts_dir(root, original).parent / "results").glob("record-*.json"))
    assert not any(row[1]["step_id"] == "current-final-save" for row in original_rows)
    attempts = execution._attempts_dir(root, original)
    original_intents = set(attempts.glob("*/intent.json"))
    originals = {
        path: path.read_bytes() for intent in original_intents
        for path in intent.parent.iterdir() if path.is_file()
    }
    originals[root / original_path] = (root / original_path).read_bytes()
    # 尚无正式 R1；普通捕获集合不能用缺件冒充阶段依据，显式消费此前真实阶段。
    r1_path = context_path.with_name("review-outcome-round-1.json")
    assert not r1_path.exists()
    assert execution._native_counterexample_r2(root, impl) == (False, ())
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    assert execution.counterexample_verification_state(
        root, impl, progress, require_r2=False
    )[0]

    successor, successor_path = _plan(root, impl, "file_case", "fix17-successor")
    assert successor.candidate_digest == original.candidate_digest
    assert successor.max_execution_attempts == original.max_execution_attempts
    execution._require_original_batch(root, original, successor)

    result = _verify_plan(root, successor_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "counterexample-unfinished-predecessor-no-takeover" in result.stdout + result.stderr
    history, debts = execution._historical_resource_cycles(root, successor)
    assert not debts
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not (attempts.parent / "plans" / f"{counterexample_digest(successor)}.json").exists()
    assert not list((attempts.parent / "results").glob("record-reconciled-*.json"))
    assert all(saved.id == original.id for _, _, saved, _ in history)
    added = set(attempts.glob("*/intent.json")) - original_intents
    if interruption == "process_creation":
        assert not added
    else:
        assert len(added) == 1
        cleanup = json.loads(next(iter(added)).read_bytes())
        assert cleanup["step_id"] == "current-final-cleanup" and cleanup["historical_cleanup_only"] is True
    progress = read_progress(implementation_artifacts(root, LOOP).progress_path)
    live = execution.counterexample_verification_state(root, impl, progress, require_r2=False)
    assert live[0]
    captured = {ref.path: (root / ref.path).read_bytes() for ref in live[2]}
    assert failed.attempt_ref.path in captured
    frozen = execution.counterexample_verification_state(root, impl, progress, captured_artifacts=captured, require_r2=False)
    assert frozen[0] == live[0]
    missing = dict(captured)
    missing.pop(failed.attempt_ref.path)
    assert execution.counterexample_verification_state(root, impl, progress, captured_artifacts=missing, require_r2=False)[0]
    assert not implementation_artifacts(root, LOOP).close_path.exists()
    assert not r1_path.exists()
    context_after = json.loads(context_path.read_bytes())
    assert context_after["started_at_ms"] == context_before["started_at_ms"]
    assert context_after["contracts"] == context_before["contracts"]

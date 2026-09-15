"""真实所属进程和持久观察边界；仅当前平台实证，不以替身宣称跨平台。"""

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from ai_sdlc.core import counterexample_execution as execution
from ai_sdlc.core import frontend_browser_gate_runtime as browser_runtime
from ai_sdlc.core.counterexample_models import ArtifactRef
from ai_sdlc.core.quality_command import (
    ControlledQualityOptions,
    QualityCommandOptions,
    run_quality_command,
)
from tests.unit import test_counterexample_execution as cases
from tests.unit import test_quality_command as quality_cases


@pytest.fixture
def execution_case(tmp_path):
    return cases.execution_case.__wrapped__(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX cleanup 中断；Windows 保持实机门禁")
@pytest.mark.parametrize("interrupted", [False, True])
def test_fix7_cleanup_interrupt_remains_unknown_across_receipt_consumers(
    execution_case, monkeypatch, interrupted
):
    import hashlib

    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.counterexample_evaluation import evaluate_counterexample
    from ai_sdlc.core.counterexample_models import CounterexamplePlan

    root, contract, original = execution_case
    code = (
        "import json,runpy,sys\n"
        "bad=runpy.run_path('src/save.py')['VALUE']=='lost'\n"
        "print(json.dumps({'schema_version':1,'assertion_id':'saved-state',"
        "'reached':True,'assertion_result':'rejected' if bad else 'accepted',"
        "'failure_reason':'target_assertion' if bad else 'none'}),flush=True)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    (root / "tests/v0.py").write_text(code)
    for subject in original.subjects:
        project = Path(next(step.binding.project_root for step in original.steps if step.subject_id == subject.id))
        (project / "tests/v0.py").write_text(code)
    data = original.model_dump(mode="json")
    data["v0_digest"] = hashlib.sha256(code.encode()).hexdigest()
    case = cases._change_observer(
        (root, contract, CounterexamplePlan.model_validate(data)),
        "import json,runpy\nvalue=runpy.run_path('src/save.py')['VALUE']\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':value}}))\n",
    )
    root, contract, plan = case
    cases._started_history_input(case, monkeypatch)
    prior = [cases._execute(case, step) for step in (
        "current-none", "current-V0", "current-V1", "current-cleanup", "variant-none"
    )]
    original_cleanup = quality._OwnedPosixProcesses.cleanup
    interruption = KeyboardInterrupt("real-variant-cleanup-interrupted")
    injected = []

    def cleanup(owner, process):
        result = original_cleanup(owner, process)
        if interrupted:
            injected.append(process.pid)
            raise interruption
        return result

    monkeypatch.setattr(quality._OwnedPosixProcesses, "cleanup", cleanup)
    before = set(execution._attempts_dir(root, plan).iterdir())
    if interrupted:
        with pytest.raises(KeyboardInterrupt) as caught:
            cases._execute(case, "variant-V0")
        assert caught.value is interruption and len(injected) == 1
        folder, = set(execution._attempts_dir(root, plan).iterdir()) - before
        reference = execution._ref(root, folder / "intent.json")
        receipt = execution.recover_counterexample_attempt(root, plan, reference)
    else:
        receipt = cases._execute(case, "variant-V0")
        folder = (root / receipt.attempt_ref.path).parent
    originals = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    assert b'"assertion_result": "rejected"' in (folder / "stdout").read_bytes()
    assert receipt.exit_code == 1 and receipt.normally_completed is (not interrupted)
    assert receipt.cleanup_status == ("incomplete" if interrupted else "complete")
    captured = {ref.path: (root / ref.path).read_bytes() for ref in execution.attempt_artifact_refs(root, receipt.attempt_ref)}
    cold = execution.recover_counterexample_attempt(root, plan, receipt.attempt_ref, captured_artifacts=captured)
    assert cold == receipt
    live_raw = execution.read_owned_raw_evidence(root, receipt)
    captured_raw = execution.read_owned_raw_evidence(root, cold, captured_artifacts=captured)
    assert live_raw == captured_raw
    # 已保存字节可认证，不等于 CE 的完成证据；中断缺少正常后验，不能升级为完整业务观察。
    assert all(item.complete is (not interrupted) for item in live_raw)
    observation = execution.collect_observation(contract, plan, cold, captured_raw)
    assert observation.assertion_result == ("unknown" if interrupted else "rejected")
    assessment = evaluate_counterexample(contract, plan, execution._bundle_from_attempts(root, contract, plan, [*prior, cold]))
    variant = next(item for item in assessment.variants if item.subject_id == "variant")
    assert variant.v0 == ("unknown" if interrupted else "detected")
    assert assessment.v1_disposition == "retain_v0"
    if interrupted:
        before_rejected = set(execution._attempts_dir(root, plan).iterdir())
        # 未确认收尾的原尝试按既有历史资源规则拒绝重放，早于普通依赖失败检查。
        with pytest.raises(ValueError, match="counterexample-prior-execution-unknown-no-replay"):
            cases._execute(case, "variant-V1")
        assert set(execution._attempts_dir(root, plan).iterdir()) == before_rejected
    time.sleep(0.08)
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@pytest.mark.skipif(os.name == "nt", reason="POSIX 真实进程组反例")
def test_parent_exit_does_not_hide_owned_live_descendant(tmp_path):
    ready = tmp_path / "descendant-ready"
    child_code = (
        "import pathlib,time,signal;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');time.sleep(20)"
    )
    parent_code = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "print(p.pid,flush=True)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, _ = parent.communicate(timeout=5)
        assert parent.returncode == 0
        descendant = int(stdout.strip())
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        os.kill(descendant, 0)
        browser_runtime._kill_probe_runner_process_tree(parent)
        state = subprocess.run(
            ["ps", "-p", str(descendant), "-o", "stat="],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        assert not state or state.startswith("Z"), "父已退仍留下本次所属后代"
    finally:
        with suppress(ProcessLookupError):
            os.killpg(parent.pid, signal.SIGKILL)
        parent.wait(timeout=3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 后台会话的原生执行回执")
@pytest.mark.parametrize("keep_marker", [True, False])
def test_native_detached_completion_and_unknown_no_replay(execution_case, keep_marker):
    child = (
        "import os,pathlib,sys,time\n"
        "root=pathlib.Path(sys.argv[1]);(root/'helper-ready').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+15\n"
        "while time.monotonic()<deadline and not (root/'helper-stop').exists():\n"
        " (root/'helper-value').write_text(str(time.monotonic()));time.sleep(.01)\n"
    )
    source = (
        "import json,pathlib,subprocess,sys,time\n"
        "root=pathlib.Path(sys.argv[1]).parent\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r},str(root)],"
        + ("" if keep_marker else "env={},")
        + "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+2\n"
        "while not (root/'helper-value').exists() and time.monotonic()<deadline:time.sleep(.01)\n"
        "assert (root/'helper-value').exists()\n"
        "print(json.dumps({'schema_version':1,'typed_actual':{'type':'string','value':'saved'}}))\n"
    )
    case = cases._change_observer(execution_case, source)
    root, contract, plan = case
    step = next(step for step in plan.steps if step.id == "current-none")
    resource = Path(step.binding.resources[0].root)
    try:
        receipt = cases._execute(case)
        assert (receipt.cleanup_status == "complete") == keep_marker
        recovered = execution.recover_counterexample_attempt(
            root, plan, receipt.attempt_ref
        )
        assert recovered == receipt
        if keep_marker:
            # 后台动作确已收尾，但本次观察写过持久资源，不能作为成功业务观察。
            assert receipt.status == "infrastructure_error"
            reference = next(
                ref
                for ref in receipt.raw_evidence_refs
                if ref.path.endswith("/resource-state.json")
            )
            state = json.loads((root / reference.path).read_bytes())
            assert state["before"] != state["after"]
            value = (resource / "helper-value").read_bytes()
            time.sleep(0.08)
            assert (resource / "helper-value").read_bytes() == value
        else:
            with pytest.raises(ValueError, match="unknown-no-replay"):
                cases._execute(case)
            assert (
                len(list(execution._attempts_dir(root, plan).glob("*/intent.json")))
                == 1
            )
            assert resource.is_dir()
            assert (resource / "result.json").read_text() == '{"value":"saved"}'
    finally:
        (resource / "helper-stop").touch()


@pytest.mark.skipif(
    os.name == "nt", reason="实际 POSIX 宿主中断；Windows 保留单独平台验收"
)
@pytest.mark.parametrize(
    "window",
    ["before_intent", "before_process", "during_output", "after_raw", "before_cleanup"],
)
def test_real_host_crash_preserves_attempt_without_automatic_replay(
    execution_case, window
):
    from ai_sdlc.core.quality_command import _posix_group_members

    if window == "during_output":
        # 在真实业务输出后显式等待父进程，不依赖启动器源码字面量或固定睡眠。
        root, _, _ = execution_case
        source = (root / "observe.py").read_text() + (
            "import os,signal\n"
            "print('CRASH_OUTPUT_WINDOW '+str(os.getpid()),flush=True)\n"
            "while True: signal.pause()\n"
        )
        root, contract, plan = cases._change_observer(execution_case, source)
        plan = plan.model_copy(
            update={
                "steps": tuple(
                    step.model_copy(
                        update={
                            "binding": step.binding.model_copy(
                                update={"timeout_seconds": 20}
                            )
                        }
                    )
                    if step.id == "current-none"
                    else step
                    for step in plan.steps
                )
            }
        )
    else:
        root, contract, plan = execution_case
    # 此用例验证中断窗口；合法输入须覆盖原冻结计划全部步骤及原尾预留。
    deadline_ms = time.time_ns() // 1_000_000 + 1000 * (
        plan.required_reserve_seconds
        + sum(
            step.binding.timeout_seconds + step.reservation_seconds + 5
            for step in plan.steps
        )
        + 60
    )
    control = (
        root / ".ai-sdlc/loops/implementation/sample-implementation/crash-input.json"
    )
    control.write_text(
        json.dumps(
            {
                "contract": contract.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "deadline_ms": deadline_ms,
            }
        )
    )
    code = r"""
import json,os,sys,time
from pathlib import Path
from ai_sdlc.core import counterexample_execution as execution
from ai_sdlc.core.counterexample_models import VerificationContract,CounterexamplePlan
root=Path(sys.argv[1]);window=sys.argv[2]
data=json.loads((root/'.ai-sdlc/loops/implementation/sample-implementation/crash-input.json').read_text())
contract=VerificationContract.model_validate(data['contract']);plan=CounterexamplePlan.model_validate(data['plan'])
original=execution._write_json
def write(root,path,payload,**kwargs):
    if path.name=='process.json':
        print('CRASH_PROCESS_OWNER '+json.dumps(payload),flush=True)
    if (window=='before_intent' and path.name=='intent.json') or (window=='before_process' and path.name=='process.json') or (window=='before_cleanup' and path.name=='cleanup.json'):
        print('CRASH_WINDOW '+window,flush=True)
        os._exit(71)
    result=original(root,path,payload,**kwargs)
    if window=='after_raw' and path.name=='raw-result.json':
        print('CRASH_WINDOW '+window,flush=True)
        os._exit(72)
    return result
execution._write_json=write
execution.execute_counterexample_attempt(root,contract,plan,'current-none',deadline_ms=data['deadline_ms'])
"""
    # 宿主原始输出放在工程外；即使尚未到目标窗口也保留真实异常。
    stdout_path = root.parent / "crash-worker-stdout.bin"
    stderr_path = root.parent / "crash-worker-stderr.bin"
    stdout_stream = stdout_path.open("wb")
    stderr_stream = stderr_path.open("wb")
    started = time.monotonic()
    worker = subprocess.Popen(
        [sys.executable, "-c", code, str(root), window],
        stdout=stdout_stream,
        stderr=stderr_stream,
    )
    expected_exit = (
        -signal.SIGKILL
        if window == "during_output"
        else 72
        if window == "after_raw"
        else 71
    )
    attempts = execution._attempts_dir(root, plan)
    owned_group = None
    try:
        if window == "during_output":
            until = time.monotonic() + 20
            output_files = []
            ready_lines = []
            while time.monotonic() < until:
                output_files = list(attempts.glob("*/stdout"))
                if output_files:
                    ready_lines = [
                        line
                        for line in output_files[0].read_bytes().splitlines()
                        if line.startswith(b"CRASH_OUTPUT_WINDOW ")
                    ]
                    if ready_lines:
                        break
                if worker.poll() is not None:
                    break
                time.sleep(0.02)
            assert len(ready_lines) == 1, "未达到真实输出中断窗口"
            process = json.loads(output_files[0].with_name("process.json").read_text())
            owned_group = process["process_group"]
            target_pid = int(ready_lines[0].split()[1])
            assert os.getpgid(target_pid) == owned_group
            assert worker.poll() is None
            assert not output_files[0].with_name("raw-result.json").exists()
            assert not output_files[0].with_name("cleanup.json").exists()
            worker.kill()
        worker.wait(timeout=20)
        stdout, stderr = stdout_path.read_bytes(), stderr_path.read_bytes()
        assert worker.returncode == expected_exit, (stdout, stderr)
        if window != "during_output":
            assert ("CRASH_WINDOW " + window).encode() in stdout.splitlines()
        intents = list(attempts.glob("*/intent.json"))
        if window == "before_intent":
            assert not intents
            assert not list(attempts.glob("*/process.json"))
            return
        assert len(intents) == 1
        attempt_ref = ArtifactRef(
            path=intents[0].relative_to(root).as_posix(),
            sha256=__import__("hashlib").sha256(intents[0].read_bytes()).hexdigest(),
        )
        recovered = execution.recover_counterexample_attempt(root, plan, attempt_ref)
        assert (
            recovered.status == "execution_unknown"
            or recovered.cleanup_status != "complete"
        )
        with pytest.raises(ValueError, match="unknown-no-replay"):
            execution.execute_counterexample_attempt(
                root,
                contract,
                plan,
                "current-none",
                deadline_ms=deadline_ms,
            )
        assert len(list(attempts.glob("*/intent.json"))) == 1
        if window in ("after_raw", "before_cleanup"):
            assert intents[0].with_name("raw-result.json").is_file()
            assert intents[0].with_name("stdout").stat().st_size > 0
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=3)
        stdout_stream.close()
        stderr_stream.close()
        (root.parent / "crash-worker-receipt.json").write_text(
            json.dumps(
                {
                    "window": window,
                    "expected_exit": expected_exit,
                    "actual_exit": worker.returncode,
                    "elapsed_seconds": time.monotonic() - started,
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                }
            )
        )
        # before_process 尚未发布 process.json，但回调已提供本次真实归属；
        # 仅测试宿主用它清理刚创建的组，不补写产品原件或让冷恢复发信号。
        owner_lines = [
            line.removeprefix(b"CRASH_PROCESS_OWNER ")
            for line in stdout_path.read_bytes().splitlines()
            if line.startswith(b"CRASH_PROCESS_OWNER ")
        ]
        if owner_lines:
            assert len(owner_lines) == 1
            owner = json.loads(owner_lines[0])
            intents = list(attempts.glob("*/intent.json"))
            assert len(intents) == 1
            intent = json.loads(intents[0].read_bytes())
            assert owner["ownership_nonce"] == intent["ownership_nonce"]
            assert owner["process_group"] == owner["pid"]
            owned_group = owner["process_group"]
        if owned_group is not None:
            with suppress(ProcessLookupError):
                os.killpg(owned_group, signal.SIGKILL)
            until = time.monotonic() + 3
            while _posix_group_members(owned_group) and time.monotonic() < until:
                time.sleep(0.02)
            assert not _posix_group_members(owned_group)


@pytest.mark.parametrize("storage", ["file", "sqlite"])
def test_write_then_independent_new_process_reads_actual_persistent_state(
    tmp_path, storage
):
    project = quality_cases.repository.__wrapped__(tmp_path)
    (project / ".gitignore").write_text("scratch/\n")
    scratch = project / "scratch"
    scratch.mkdir()
    target = scratch / ("state.json" if storage == "file" else "state.sqlite")
    if storage == "file":
        writer = f'import pathlib;pathlib.Path({str(target)!r}).write_text(\'{{"value":"saved"}}\')'
        reader = f"import pathlib,json;value=json.loads(pathlib.Path({str(target)!r}).read_text())['value'];print(json.dumps({{'schema_version':1,'typed_actual':{{'type':'string','value':value}}}}))"
    else:
        writer = f"import sqlite3;c=sqlite3.connect({str(target)!r});c.execute('CREATE TABLE state(value TEXT)');c.execute(\"INSERT INTO state VALUES ('saved')\");c.commit();c.close()"
        reader = f"import sqlite3,json;c=sqlite3.connect({str(target)!r});value=c.execute('SELECT value FROM state').fetchone()[0];c.close();print(json.dumps({{'schema_version':1,'typed_actual':{{'type':'string','value':value}}}}))"
    recorded = []
    for name, code in (("write", writer), ("observe", reader)):
        folder = project / f".ai-sdlc/loops/implementation/process-proof/{name}"
        folder.mkdir(parents=True)
        for stream in ("stdout", "stderr"):
            (folder / stream).write_bytes(b"")
        receipts = {}
        result = run_quality_command(
            QualityCommandOptions(
                root=project,
                cwd=project,
                argv=(sys.executable, "-c", code),
                timeout_seconds=3,
                controlled=ControlledQualityOptions(
                    environment={"PATH": os.environ["PATH"]},
                    stdout_path=folder / "stdout",
                    stderr_path=folder / "stderr",
                    max_output_bytes=4096,
                    ownership_nonce=f"owned-{storage}-{name}",
                    on_started=lambda value, saved=receipts: saved.update(
                        process=value
                    ),
                    on_raw_result=lambda value, saved=receipts: saved.update(raw=value),
                    on_cleanup=lambda value, saved=receipts: saved.update(
                        cleanup=value
                    ),
                ),
            )
        )
        assert result.successful
        assert receipts["cleanup"]["status"] == "complete"
        recorded.append((receipts, result))
    assert recorded[0][0]["process"]["pid"] != recorded[1][0]["process"]["pid"]
    assert (
        recorded[0][0]["raw"]["ended_at_ms"] <= recorded[1][0]["raw"]["started_at_ms"]
    )
    assert json.loads(recorded[1][1].stdout_tail)["typed_actual"]["value"] == "saved"

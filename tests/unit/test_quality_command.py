"""Tests for executable quality evidence bound to source truth."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_sdlc.core import quality_command as quality
from ai_sdlc.core.pr_review_models import ProviderCompletionProof
from ai_sdlc.core.quality_command import (
    ControlledQualityOptions,
    QualityCommandOptions,
    build_source_digest,
    build_source_digest_at_reviewed_parent,
    quality_command_environment,
    run_quality_command,
    validate_quality_argv,
)


@pytest.mark.parametrize("controlled", [False, True])
def test_quality_command_preserves_empty_and_text_arguments(repository, controlled):
    from dataclasses import replace

    values = ("", " ", "中文 输入", "尾部", "")
    code = "import json,sys;print(json.dumps(sys.argv[1:]))"
    if controlled:
        options, receipts = _controlled_options(repository, code)
    else:
        options = QualityCommandOptions(
            root=repository, cwd=repository, argv=(sys.executable, "-c", code)
        )
        receipts = None
    options = replace(options, argv=(*options.argv, *values))

    result = run_quality_command(options)

    assert result.successful
    assert json.loads(result.stdout_tail) == list(values)
    assert result.argv == list(options.argv)
    if receipts is not None:
        assert receipts["raw"]["exit_code"] == 0
        assert receipts["cleanup"]["status"] == "complete"


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("",),
        ("", "value"),
        ("py\0thon",),
        ("python", "\0"),
        ("python", None),
        "python",
    ],
)
def test_invalid_quality_argv_is_rejected_before_source_capture(
    repository, monkeypatch, argv
):
    from ai_sdlc.core import quality_command

    def unexpected_capture(*args, **kwargs):
        pytest.fail("invalid argv must be rejected before command preparation")

    monkeypatch.setattr(quality_command, "build_source_digest", unexpected_capture)
    with pytest.raises(ValueError, match="quality command"):
        validate_quality_argv(argv)
    with pytest.raises(ValueError, match="quality command"):
        run_quality_command(
            QualityCommandOptions(root=repository, cwd=repository, argv=argv)
        )


@pytest.mark.parametrize("absolute", [False, True])
def test_byte_artifact_creates_owned_directories_and_preserves_immutable_bytes(
    tmp_path, absolute
):
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    root = tmp_path.resolve()
    relative = Path("new/nested/result.bin")
    requested = root / relative if absolute else relative
    target = root / relative
    store = LoopArtifactStore(root)
    assert store.write_bytes_artifact(requested, b"\xff\x00", immutable=True) == target
    before = target.stat().st_mtime_ns
    store.write_bytes_artifact(requested, b"\xff\x00", immutable=True)
    assert target.stat().st_mtime_ns == before
    with pytest.raises(ValueError, match="immutable-artifact-content-mismatch"):
        store.write_bytes_artifact(requested, b"changed", immutable=True)
    assert target.read_bytes() == b"\xff\x00"
    store.write_bytes_artifact(requested, b"replacement")
    assert target.read_bytes() == b"replacement"
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize(
    "invalid", ["parent_file", "parent_reparse", "leaf_reparse", "leaf_directory"]
)
def test_byte_artifact_checks_existing_path_before_any_creation(
    tmp_path, monkeypatch, invalid
):
    from types import SimpleNamespace

    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    root = tmp_path.resolve()
    parent = root / "existing"
    if invalid == "parent_file":
        parent.write_bytes(b"parent-is-a-file")
    else:
        parent.mkdir()
    if invalid.startswith("parent"):
        target = parent / "new" / "result.bin"
        marked = parent
    else:
        target = parent / "result.bin"
        marked = target
        if invalid == "leaf_directory":
            target.mkdir()
        else:
            target.write_bytes(b"original")
    before = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
    real_lstat = Path.lstat
    mutations = []

    def file_metadata(path, *args, **kwargs):
        metadata = real_lstat(path, *args, **kwargs)
        if path == marked and invalid.endswith("reparse"):
            # 只模拟文件属性分支；不把本机检查称为 Windows 实机验收。
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    def unexpected_creation(path, *args, **kwargs):
        mutations.append(str(path))
        pytest.fail("invalid existing path must be rejected before mkdir/open")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", file_metadata)
        patch.setattr(Path, "mkdir", unexpected_creation)
        patch.setattr(Path, "open", unexpected_creation)
        with pytest.raises(ValueError, match="parent|symlink|regular"):
            LoopArtifactStore(root).write_bytes_artifact(target, b"new", immutable=True)
    assert not mutations
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == before
    if invalid == "leaf_reparse":
        assert target.read_bytes() == b"original"


def test_byte_artifact_replace_failure_keeps_original_and_removes_temporary(
    tmp_path, monkeypatch
):
    from ai_sdlc.core import loop_artifacts

    root = tmp_path / "artifacts"
    root.mkdir()
    target = root / "result.bin"
    target.write_bytes(b"original")

    def cannot_replace(*args, **kwargs):
        raise PermissionError("ordinary replacement unavailable")

    monkeypatch.setattr(loop_artifacts, "_replace_with_retry", cannot_replace)
    with pytest.raises(PermissionError, match="replacement unavailable"):
        loop_artifacts.LoopArtifactStore(root).write_bytes_artifact(target, b"new")
    assert target.read_bytes() == b"original"
    assert list(root.iterdir()) == [target]


def _controlled_options(repository, code, *, timeout=2, limit=4096):
    folder = repository / ".ai-sdlc/loops/implementation/test/attempt"
    folder.mkdir(parents=True)
    stdout, stderr = folder / "stdout", folder / "stderr"
    stdout.write_bytes(b"")
    stderr.write_bytes(b"")
    receipts = {}
    options = QualityCommandOptions(
        root=repository,
        cwd=repository,
        argv=(sys.executable, "-c", code),
        timeout_seconds=timeout,
        controlled=ControlledQualityOptions(
            environment={"PATH": os.environ["PATH"], "LANG": "C.UTF-8"},
            stdout_path=stdout,
            stderr_path=stderr,
            max_output_bytes=limit,
            ownership_nonce="test-owned-nonce",
            on_started=lambda value: receipts.update(process=value),
            on_raw_result=lambda value: receipts.update(raw=value),
            on_cleanup=lambda value: receipts.update(cleanup=value),
        ),
    )
    return options, receipts


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 进程；Windows Job 另有独立对照")
@pytest.mark.parametrize("interrupt_cleanup", [False, True])
def test_fix7_cleanup_interrupt_preserves_originals_and_owned_finalizers(
    repository, monkeypatch, interrupt_cleanup
):
    import threading

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('ordinary-rejection'); raise SystemExit(3)")
    processes, readers = [], []
    real_popen, real_thread = subprocess.Popen, threading.Thread
    original_cleanup = quality._OwnedPosixProcesses.cleanup
    interruption = KeyboardInterrupt("test-owned-cleanup-interrupted")

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def thread(*args, **kwargs):
        reader = real_thread(*args, **kwargs)
        readers.append(reader)
        return reader

    def cleanup(owner, process):
        result = original_cleanup(owner, process)
        if interrupt_cleanup:
            raise interruption
        return result

    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    monkeypatch.setattr(quality.threading, "Thread", thread)
    monkeypatch.setattr(quality._OwnedPosixProcesses, "cleanup", cleanup)
    if interrupt_cleanup:
        with pytest.raises(KeyboardInterrupt) as caught:
            quality.run_controlled_process(options)
        assert caught.value is interruption
    else:
        result = quality.run_controlled_process(options)
        assert result.exit_code == 3 and result.status == "failed"
    assert processes and all(process.poll() is not None for process in processes)
    assert readers and all(not reader.is_alive() for reader in readers)
    assert receipts["raw"]["exit_code"] == 3
    assert receipts["cleanup"]["status"] == ("incomplete" if interrupt_cleanup else "complete")
    quality.validate_controlled_receipts(
        receipts["process"], receipts["raw"], receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )
    before = options.controlled.stdout_path.read_bytes()
    time.sleep(0.08)
    assert options.controlled.stdout_path.read_bytes() == before == b"ordinary-rejection\n"


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 进程收尾与故障窗口")
@pytest.mark.parametrize("fault_at", ["stdin-close", "poll", "kill", "wait", "reader-join", "pipe-close", "flush", "fsync", "stream-close", "tracking-finished"])
def test_fix7_each_finalization_failure_keeps_later_owned_steps(
    repository, monkeypatch, fault_at
):
    import threading

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('finalizer-original')")
    failure = KeyboardInterrupt(f"first-{fault_at}")
    injected, finalizing, processes, readers, calls = [], [], [], [], []
    real_popen, real_thread = subprocess.Popen, threading.Thread
    real_cleanup, real_fsync = quality._OwnedPosixProcesses.cleanup, os.fsync

    def fail_once(label, action):
        def run(*args, **kwargs):
            calls.append(label)
            if label == fault_at and not injected:
                injected.append(label)
                raise failure
            return action(*args, **kwargs)
        return run

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if kwargs.get("stdin") != subprocess.PIPE:
            return process
        processes.append(process)
        monkeypatch.setattr(process.stdin, "close", fail_once("stdin-close", process.stdin.close))
        monkeypatch.setattr(process.stdout, "close", fail_once("pipe-close", process.stdout.close))
        for name in ("poll", "kill", "wait"):
            method = getattr(process, name)
            wrapped = fail_once(name, method)
            def operation(*args, _method=method, _wrapped=wrapped, _name=name, **kwargs):
                if finalizing:
                    # 模拟一次查询未结束只用于让真实 owned kill 进入指定窗口。
                    if fault_at == "kill" and _name == "poll" and not injected:
                        return None
                    return _wrapped(*args, **kwargs)
                return _method(*args, **kwargs)
            monkeypatch.setattr(process, name, operation)
        return process

    def thread(*args, **kwargs):
        reader = real_thread(*args, **kwargs)
        readers.append(reader)
        monkeypatch.setattr(reader, "join", fail_once("reader-join", reader.join))
        return reader

    def cleanup(owner, process):
        result = real_cleanup(owner, process)
        finalizing.append(True)
        return result

    def flush(name, raw):
        if finalizing:
            return fail_once("flush", raw.flush)()
        return raw.flush()

    output_streams = _adapt_owned_output(
        monkeypatch, options, flush=flush,
        close=lambda name, raw: fail_once("stream-close", raw.close)(),
    )
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    monkeypatch.setattr(quality.threading, "Thread", thread)
    monkeypatch.setattr(quality._OwnedPosixProcesses, "cleanup", cleanup)
    monkeypatch.setattr(quality.os, "fsync", fail_once("fsync", real_fsync))
    real_finished = quality._OwnedPosixProcesses.finished
    monkeypatch.setattr(quality._OwnedPosixProcesses, "finished", fail_once("tracking-finished", real_finished))

    with pytest.raises(KeyboardInterrupt) as caught:
        quality.run_controlled_process(options)
    assert caught.value is failure and injected == [fault_at]
    assert processes and all(process.poll() is not None for process in processes)
    assert readers and all(not reader.is_alive() for reader in readers)
    assert {"raw", "cleanup"} <= receipts.keys()
    assert "wait" in calls and "reader-join" in calls
    assert "fsync" in calls and "stream-close" in calls and "tracking-finished" in calls
    # close 自身被打断不能声明句柄已关闭；其余流仍必须被独立尝试。
    assert all(raw.closed for raw in output_streams.values()) or fault_at == "stream-close"
    if fault_at == "tracking-finished":
        assert receipts["cleanup"]["status"] == "incomplete"
        with pytest.raises(ValueError):
            quality.validate_controlled_receipts(receipts["process"], receipts["raw"], receipts["cleanup"], ownership_nonce=options.controlled.ownership_nonce)
    else:
        quality.validate_controlled_receipts(receipts["process"], receipts["raw"], receipts["cleanup"], ownership_nonce=options.controlled.ownership_nonce)
    before = [path.read_bytes() for path in (options.controlled.stdout_path, options.controlled.stderr_path)]
    time.sleep(0.06)
    assert before == [path.read_bytes() for path in (options.controlled.stdout_path, options.controlled.stderr_path)]
    # 产品观察完成后测试才释放被故障注入故意留下的文件，不冒充产品收尾成功。
    for raw in output_streams.values():
        raw.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows Job 本地接口模拟；真实平台由 CI 验证")
def test_fix7_windows_job_interruption_preserves_first_exception_and_raw_backup(repository, monkeypatch):
    import threading
    from dataclasses import replace
    from types import FunctionType, SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('job-original')")
    failure = KeyboardInterrupt("first-job-termination")
    cause = RuntimeError("original-cause")
    calls, processes, readers = [], [], []
    original_popen, original_thread = subprocess.Popen, threading.Thread
    original_chunks = quality._controlled_pipe_chunks

    class Job:
        name = "local-simulated-job"
        active_processes = None
        def __init__(self, nonce):
            self.nonce = nonce
        def open(self):
            pass
        def assign(self, process):
            calls.append("assign")
        def terminate_and_verify(self):
            calls.append("terminate")
            raise failure from cause
        def close(self):
            calls.append("job-close")
            raise OSError("secondary-job-close")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def thread(*args, **kwargs):
        reader = original_thread(*args, **kwargs)
        readers.append(reader)
        return reader

    def raw_callback(raw):
        calls.append("raw")
        raise KeyboardInterrupt("secondary-raw-publication")

    def cleanup_callback(cleanup):
        calls.append("cleanup")
        receipts["cleanup"] = cleanup
        raise ValueError("secondary-cleanup-publication")

    # 使用真实本地子进程及原 POSIX reader；只替换 Windows Job 接口，不能声称 Windows 实机通过。
    local_chunks = FunctionType(original_chunks.__code__, {**original_chunks.__globals__, "os": os})
    monkeypatch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(quality, "_WindowsOwnedJob", Job)
    monkeypatch.setattr(quality, "_controlled_pipe_chunks", local_chunks)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    monkeypatch.setattr(quality.threading, "Thread", thread)
    output_streams = _adapt_owned_output(monkeypatch, options)
    with pytest.raises(KeyboardInterrupt) as caught:
        quality.run_controlled_process(replace(options, controlled=replace(options.controlled, on_raw_result=raw_callback, on_cleanup=cleanup_callback)))
    assert caught.value is failure and failure.__cause__ is cause
    assert calls == ["assign", "terminate", "job-close", "raw", "cleanup"]
    assert all(process.poll() is not None for process in processes)
    assert readers and all(not reader.is_alive() for reader in readers)
    assert all(raw.closed for raw in output_streams.values())
    assert any(frame.tb_frame.f_code.co_name == "terminate_and_verify" for frame in _fix7_tracebacks(failure.__traceback__))
    assert all(any(label in note for note in failure.__notes__) for label in ("secondary-job-close", "secondary-raw-publication", "secondary-cleanup-publication"))
    cleanup = json.loads(json.dumps(receipts["cleanup"]))
    raw = quality.controlled_raw_original({}, cleanup, raw_present=False)
    assert raw["exit_code"] == 0 and cleanup["status"] == "incomplete"
    quality.validate_controlled_receipts(receipts["process"], raw, cleanup, ownership_nonce=options.controlled.ownership_nonce)
    assert options.controlled.stdout_path.read_bytes() == b"job-original\n"


def _fix7_tracebacks(traceback):
    while traceback is not None:
        yield traceback
        traceback = traceback.tb_next


def test_fix7_reader_still_running_cannot_publish_raw_original(repository, monkeypatch):
    import threading

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('unsealed-output')")
    held, release = threading.Event(), threading.Event()
    original_chunks, original_thread = quality._controlled_pipe_chunks, threading.Thread
    readers = []

    def chunks(pipe, stop):
        for chunk in original_chunks(pipe, stop):
            yield chunk
            if b"unsealed-output" in chunk:
                held.set()
                release.wait(5)

    def thread(*args, **kwargs):
        reader = original_thread(*args, **kwargs)
        readers.append(reader)
        # 人为阻断本次 join 的等待效果；真实 is_alive 不能被成功返回值替代。
        monkeypatch.setattr(reader, "join", lambda timeout=None: time.sleep(0.01))
        return reader

    output_streams = _adapt_owned_output(monkeypatch, options)
    monkeypatch.setattr(quality, "_controlled_pipe_chunks", chunks)
    monkeypatch.setattr(quality.threading, "Thread", thread)
    try:
        with pytest.raises(RuntimeError, match="controlled-quality-output-readers-not-stopped"):
            quality.run_controlled_process(options)
        assert held.is_set() and any(reader.is_alive() for reader in readers)
        assert "raw" not in receipts and "raw_result_original" not in receipts["cleanup"]
        assert receipts["cleanup"]["status"] == "incomplete"
        with pytest.raises(ValueError):
            quality.validate_controlled_receipts(receipts["process"], {}, receipts["cleanup"], ownership_nonce=options.controlled.ownership_nonce)
    finally:
        # 本分支明确未完成；测试释放自己的阻断，不能算成产品已完成收尾。
        release.set()
        for reader in readers:
            original_thread.join(reader, timeout=2)
        for stream in output_streams.values():
            stream.close()
    assert all(not reader.is_alive() for reader in readers)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 信号退出身份")
@pytest.mark.parametrize("termination", ["SIGKILL", "SIGTERM", "SIGINT", "SIGPIPE", "exit247"])
def test_controlled_target_termination_survives_launcher(repository, termination):
    import signal

    ending = (
        "raise SystemExit(247)"
        if termination == "exit247"
        else (
            "signal.signal(signal.SIGPIPE, signal.SIG_DFL); "
            if termination == "SIGPIPE" else ""
        ) + f"os.kill(os.getpid(), signal.{termination})"
    )
    options, receipts = _controlled_options(
        repository,
        "import os, signal; print('target-rejection', flush=True); " + ending,
        timeout=5,
    )
    result = run_quality_command(options)
    expected = 247 if termination == "exit247" else -getattr(signal, termination)
    assert result.exit_code == expected
    assert receipts["raw"]["exit_code"] == expected
    assert receipts["cleanup"]["status"] == "complete"
    assert "target-rejection" in options.controlled.stdout_path.read_text()
    assert not result.successful


def test_controlled_job_initialization_failure_persists_never_started(
    repository, monkeypatch
):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('must-not-start')")
    started = []

    def unavailable(_nonce):
        raise OSError("test-job-initialization-unavailable")

    # 只注入平台能力故障，原件仍须由受控执行入口实际产生。
    monkeypatch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(quality, "_WindowsOwnedJob", unavailable)
    monkeypatch.setattr(quality.subprocess, "Popen", lambda *_a, **_k: started.append(1))
    result = quality.run_controlled_process(options)
    assert result.status == "failed" and result.exit_code is None
    assert not started and "process" not in receipts
    assert receipts["raw"]["launch_status"] == "never_started"
    assert receipts["raw"]["launch_error"] == "test-job-initialization-unavailable"
    assert receipts["cleanup"]["status"] == "complete"
    assert options.controlled.stdout_path.read_bytes() == b""
    quality.validate_controlled_receipts(
        {}, receipts["raw"], receipts["cleanup"], ownership_nonce="test-owned-nonce"
    )


@pytest.mark.parametrize("collision", [True, False])
def test_windows_job_setup_failure_closes_acquired_handle(repository, monkeypatch, collision):
    import ctypes
    from types import SimpleNamespace
    from unittest.mock import Mock

    options, receipts = _controlled_options(repository, "print('must-not-start')")
    kernel = SimpleNamespace(
        **{name: Mock(return_value=0) for name in (
            "AssignProcessToJobObject", "TerminateJobObject", "QueryInformationJobObject",
            "SetInformationJobObject", "CloseHandle",
        )},
        CreateJobObjectW=Mock(return_value=123),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 183 if collision else 0, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    kernel.CloseHandle.return_value = 1
    monkeypatch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    result = quality.run_controlled_process(options)
    assert result.status == "failed" and "process" not in receipts
    assert ("colliding" if collision else "limits-unavailable") in receipts["raw"]["launch_error"]
    assert receipts["cleanup"]["status"] == "complete"
    kernel.CloseHandle.assert_called_once_with(123)
    kernel.TerminateJobObject.assert_not_called()


@pytest.mark.parametrize("previous_error", [0, 183])
@pytest.mark.parametrize("creation", ["new", "collision", "unavailable"])
def test_windows_job_creation_distinguishes_stale_error_from_current_result(
    monkeypatch, previous_error, creation
):
    """模拟 Win32 成功时保留旧错误；真实冲突与创建失败仍必须拒绝。"""
    last_error = [previous_error]

    def create_job(_security, _name):
        if creation == "collision":
            last_error[0] = 183
        elif creation == "unavailable":
            last_error[0] = 5
            return None
        return 123

    kernel = SimpleNamespace(
        **{name: Mock(return_value=1) for name in (
            "AssignProcessToJobObject", "TerminateJobObject", "QueryInformationJobObject",
            "SetInformationJobObject", "CloseHandle",
        )},
        CreateJobObjectW=Mock(side_effect=create_job),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error[0], raising=False)
    monkeypatch.setattr(
        ctypes, "set_last_error", lambda value: last_error.__setitem__(0, value), raising=False
    )
    job = quality._WindowsOwnedJob("current-job-result")
    try:
        if creation == "new":
            job.open()
            assert job.handle == 123
            kernel.SetInformationJobObject.assert_called_once()
        else:
            with pytest.raises(OSError, match="owned-job-unavailable-or-colliding"):
                job.open()
            kernel.SetInformationJobObject.assert_not_called()
    finally:
        job.close()
    if creation == "unavailable":
        kernel.CloseHandle.assert_not_called()
    else:
        kernel.CloseHandle.assert_called_once_with(123)
    kernel.TerminateJobObject.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows native named Job control")
def test_windows_native_job_accepts_new_name_and_rejects_existing_name():
    import uuid

    nonce = uuid.uuid4().hex
    job = quality._WindowsOwnedJob(nonce)
    duplicate = quality._WindowsOwnedJob(nonce)
    try:
        ctypes.set_last_error(183)
        job.open()
        with pytest.raises(OSError, match="owned-job-unavailable-or-colliding"):
            duplicate.open()
    finally:
        try:
            duplicate.close()
        finally:
            job.close()


@pytest.mark.parametrize("closed", [True, False])
def test_fix7_windows_job_close_checks_actual_handle_result(closed):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from ai_sdlc.core.quality_command import _WindowsOwnedJob

    job = object.__new__(_WindowsOwnedJob)
    job.handle = 123
    job.kernel = SimpleNamespace(CloseHandle=Mock(return_value=closed))
    if closed:
        job.close()
    else:
        with pytest.raises(OSError, match="controlled-process-job-close-unavailable"):
            job.close()
    job.kernel.CloseHandle.assert_called_once_with(123)


def _adapt_owned_output(monkeypatch, options, *, write=None, flush=None, close=None):
    real_open = Path.open
    paths = {
        options.controlled.stdout_path: "stdout",
        options.controlled.stderr_path: "stderr",
    }
    streams = {}

    class Output:
        def __init__(self, raw, name):
            self.raw, self.name = raw, name

        def write(self, data):
            return write(self.name, self.raw, data) if write else self.raw.write(data)

        def flush(self):
            return flush(self.name, self.raw) if flush else self.raw.flush()

        def close(self):
            return close(self.name, self.raw) if close else self.raw.close()

        def __getattr__(self, name):
            return getattr(self.raw, name)

    def open_output(path, *args, **kwargs):
        raw = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path in paths and mode == "r+b" and kwargs.get("buffering") == 0:
            streams[paths[path]] = raw
            return Output(raw, paths[path])
        return raw

    # 仅适配本次输出文件的底层写入，真实子进程、双流读取和回执保持原路径。
    monkeypatch.setattr(Path, "open", open_output)
    return streams


@pytest.mark.parametrize("write_size", [None, 3], ids=["normal", "short"])
def test_run11_output_short_write_preserves_all_21_bytes(
    repository, monkeypatch, write_size
):
    payload = b"ordinary-output-12345"
    options, receipts = _controlled_options(
        repository, f"import os;os.write(1,{payload!r})"
    )
    _adapt_owned_output(
        monkeypatch, options, write=lambda name, raw, data: raw.write(data[:write_size])
    )

    result = run_quality_command(options)

    assert result.successful
    assert options.controlled.stdout_path.read_bytes() == payload
    assert receipts["raw"]["output_written_bytes"] == len(payload) == 21
    assert receipts["raw"]["output_observed_bytes"] == 21
    assert not receipts["raw"]["output_io_error"]
    assert not receipts["raw"]["output_truncated"]
    assert receipts["cleanup"]["status"] == "complete"


@pytest.mark.parametrize(
    "returned",
    [0, None, -1, True, 1.0, 22],
    ids=["zero", "none", "negative", "boolean", "float", "oversized"],
)
def test_run11_output_invalid_write_result_is_failed(repository, monkeypatch, returned):
    payload = b"ordinary-output-12345"
    options, receipts = _controlled_options(
        repository, f"import os;os.write(1,{payload!r})"
    )
    _adapt_owned_output(monkeypatch, options, write=lambda name, raw, data: returned)

    result = run_quality_command(options)

    assert not result.successful
    assert options.controlled.stdout_path.read_bytes() == b""
    assert receipts["raw"]["output_written_bytes"] == 0
    assert receipts["raw"]["output_observed_bytes"] == 21
    assert receipts["raw"]["output_io_error"]
    assert receipts["cleanup"]["status"] == "complete"


def test_run11_output_error_after_partial_write_keeps_actual_count(
    repository, monkeypatch
):
    payload = b"ordinary-output-12345"
    options, receipts = _controlled_options(
        repository, f"import os;os.write(1,{payload!r})"
    )

    def fail_after_prefix(name, raw, data):
        if raw.tell():
            raise OSError("ordinary output write failed")
        return raw.write(data[:3])

    _adapt_owned_output(monkeypatch, options, write=fail_after_prefix)

    result = run_quality_command(options)

    assert not result.successful
    assert options.controlled.stdout_path.read_bytes() == payload[:3]
    assert receipts["raw"]["output_written_bytes"] == 3
    assert receipts["raw"]["output_observed_bytes"] == 21
    assert receipts["raw"]["output_io_error"]
    assert receipts["cleanup"]["status"] == "complete"


@pytest.mark.parametrize("operation", ["capture-flush", "flush", "fsync", "close"])
def test_run11_output_finalization_error_keeps_raw_and_cleanup(
    repository, monkeypatch, operation
):
    from ai_sdlc.core import quality_command

    payload = b"ordinary-output-12345"
    options, receipts = _controlled_options(
        repository, f"import os;os.write(1,{payload!r})"
    )
    flush_calls = 0
    stdout_fd = None

    def flush_output(name, raw):
        nonlocal flush_calls
        if name == "stdout":
            flush_calls += 1
            if (operation == "capture-flush" and flush_calls == 1) or (
                operation == "flush" and flush_calls == 2
            ):
                raise OSError("ordinary final flush failed")
        return raw.flush()

    def close_output(name, raw):
        raw.close()
        if name == "stdout" and operation == "close":
            raise OSError("ordinary output close failed")

    streams = _adapt_owned_output(
        monkeypatch, options, flush=flush_output, close=close_output
    )
    real_fsync = quality_command.os.fsync

    def sync_output(fd):
        nonlocal stdout_fd
        if stdout_fd is None:
            stdout_fd = streams["stdout"].fileno()
        if operation == "fsync" and fd == stdout_fd:
            raise OSError("ordinary output fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(quality_command.os, "fsync", sync_output)
    result = run_quality_command(options)

    assert not result.successful
    assert options.controlled.stdout_path.read_bytes() == payload
    assert receipts["raw"]["output_written_bytes"] == 21
    assert receipts["raw"]["output_io_error"]
    assert receipts["cleanup"]["status"] == "complete"
    assert all(stream.closed for stream in streams.values())


@pytest.mark.parametrize("limit", [42, 41], ids=["exact-cap", "over-cap"])
def test_run11_short_writes_share_one_limit_across_both_streams(
    repository, monkeypatch, limit
):
    stdout, stderr = b"a" * 21, b"b" * 21
    options, receipts = _controlled_options(
        repository,
        f"import os;os.write(1,{stdout!r});os.write(2,{stderr!r})",
        limit=limit,
    )
    _adapt_owned_output(
        monkeypatch, options, write=lambda name, raw, data: raw.write(data[:3])
    )

    result = run_quality_command(options)

    actual_stdout = options.controlled.stdout_path.read_bytes()
    actual_stderr = options.controlled.stderr_path.read_bytes()
    assert actual_stdout == stdout[: len(actual_stdout)]
    assert actual_stderr == stderr[: len(actual_stderr)]
    assert len(actual_stdout) + len(actual_stderr) == limit
    assert receipts["raw"]["output_written_bytes"] == limit
    assert receipts["raw"]["output_observed_bytes"] == 42
    assert not receipts["raw"]["output_io_error"]
    assert receipts["raw"]["output_truncated"] is (limit < 42)
    assert result.successful is (limit == 42)
    assert receipts["cleanup"]["status"] == "complete"


def test_controlled_environment_does_not_inherit_database_secret(
    repository, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "production-private-value")
    options, receipts = _controlled_options(
        repository, "import os;print(os.environ.get('DATABASE_URL','absent'))"
    )
    result = run_quality_command(options)
    assert result.successful
    assert result.stdout_tail.strip() == "absent"
    assert receipts["cleanup"]["status"] == "complete"


@pytest.mark.parametrize("mode", ["timeout", "output_limit", "nonzero"])
def test_controlled_failure_retains_bounded_raw_and_cleanup(repository, mode):
    code = {
        "timeout": "import time;print('before-timeout',flush=True);time.sleep(10)",
        "output_limit": "import os;os.write(1,b'x'*1000000)",
        "nonzero": "print('not-business-detection');raise SystemExit(7)",
    }[mode]
    options, receipts = _controlled_options(
        repository, code, timeout=0.3 if mode == "timeout" else 2, limit=512
    )
    result = run_quality_command(options)
    assert not result.successful
    assert receipts["cleanup"]["status"] == "complete"
    assert receipts["raw"]["output_written_bytes"] <= 512
    assert (
        options.controlled.stdout_path.stat().st_size
        + options.controlled.stderr_path.stat().st_size
        <= 512
    )
    if mode == "timeout":
        assert receipts["raw"]["timed_out"]
        assert "before-timeout" in options.controlled.stdout_path.read_text()
    elif mode == "output_limit":
        assert receipts["raw"]["output_truncated"]
    else:
        assert receipts["raw"]["exit_code"] == 7


def test_controlled_raw_persisted_before_source_postcheck_raises(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command

    options, receipts = _controlled_options(repository, "print('raw-before-postcheck')")
    real_build = quality_command.build_source_digest
    calls = 0

    def build(root, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert receipts["raw"]["exit_code"] == 0
            assert receipts["cleanup"]["status"] == "complete"
            raise ValueError("source postcheck unavailable")
        return real_build(root, **kwargs)

    monkeypatch.setattr(quality_command, "build_source_digest", build)
    with pytest.raises(ValueError, match="postcheck unavailable"):
        run_quality_command(options)
    assert options.controlled.stdout_path.read_text().strip() == "raw-before-postcheck"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 后台会话；Windows 使用原 Job 验收")
@pytest.mark.parametrize(
    "mode", ["success", "double_fork", "nonzero", "timeout", "output_limit"]
)
def test_controlled_detached_helper_really_stops_writing(repository, mode):
    from ai_sdlc.core.quality_command import _PosixProcessTable

    options, receipts = _controlled_options(repository, "pass", timeout=3, limit=4096)
    folder = options.controlled.stdout_path.parent
    child = "import os,pathlib,signal,sys,time\n"
    if mode == "double_fork":
        child += "if os.fork():raise SystemExit(0)\nos.setsid()\n"
    child += (
        "root=pathlib.Path(sys.argv[1]);signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        "(root/'helper-ready').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+10\n"
        "while time.monotonic()<deadline and not (root/'helper-stop').exists():\n"
        " (root/'helper-value').write_text(str(time.monotonic()));time.sleep(.01)\n"
    )
    tail = {
        "success": "raise SystemExit(0)",
        "double_fork": "raise SystemExit(0)",
        "nonzero": "raise SystemExit(7)",
        "timeout": "time.sleep(10)",
        "output_limit": "os.write(1,b'x'*1000000)",
    }[mode]
    parent = (
        "import os,pathlib,subprocess,sys,time\n"
        f"root=pathlib.Path({str(folder)!r})\n"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r},str(root)],start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+2\n"
        "while not (root/'helper-value').exists() and time.monotonic()<deadline:time.sleep(.01)\n"
        "assert (root/'helper-value').exists()\n"
        "print('helper-started',flush=True)\n" + tail
    )
    from dataclasses import replace

    options = replace(options, argv=(sys.executable, "-c", parent))
    try:
        result = run_quality_command(options)
        assert result.successful == (mode in {"success", "double_fork"}), (
            result,
            receipts,
        )
        assert receipts["cleanup"]["status"] == "complete", receipts
        helper_pid = int((folder / "helper-ready").read_text())
        assert helper_pid in {
            row[0] for row in receipts["cleanup"]["process_tracking"]["owned"]
        }
        process = _PosixProcessTable().snapshot().get(helper_pid)
        assert process is None or process.zombie
        value = (folder / "helper-value").read_bytes()
        time.sleep(0.08)
        assert (folder / "helper-value").read_bytes() == value
    finally:
        (folder / "helper-stop").touch()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 当前调用的出生身份")
def test_controlled_cleanup_does_not_stop_an_existing_process(repository):
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
    try:
        options, receipts = _controlled_options(repository, "print('complete')")
        assert run_quality_command(options).successful, receipts
        assert other.poll() is None
        assert other.pid not in {
            row[0] for row in receipts["cleanup"]["process_tracking"]["owned"]
        }
    finally:
        other.terminate()
        other.wait(timeout=3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 无法归属时保留未完成事实")
def test_detached_helper_without_marker_cannot_claim_complete(repository):
    from dataclasses import replace

    options, receipts = _controlled_options(repository, "pass")
    folder = options.controlled.stdout_path.parent
    child = (
        "import os,pathlib,sys,time\n"
        "root=pathlib.Path(sys.argv[1]);(root/'helper-ready').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+6\n"
        "while time.monotonic()<deadline and not (root/'helper-stop').exists():time.sleep(.01)\n"
    )
    parent = (
        "import pathlib,subprocess,sys,time\n"
        f"root=pathlib.Path({str(folder)!r})\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r},str(root)],env={{}},start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "deadline=time.monotonic()+2\n"
        "while not (root/'helper-ready').exists() and time.monotonic()<deadline:time.sleep(.01)\n"
        "assert (root/'helper-ready').exists()\n"
    )
    try:
        result = run_quality_command(
            replace(options, argv=(sys.executable, "-c", parent))
        )
        assert not result.successful
        assert receipts["cleanup"]["status"] == "incomplete"
        helper_pid = int((folder / "helper-ready").read_text())
        assert helper_pid in {
            row[0] for row in receipts["cleanup"]["process_tracking"]["unattributed"]
        }
        assert helper_pid not in {
            row[0] for row in receipts["cleanup"]["process_tracking"]["owned"]
        }
    finally:
        (folder / "helper-stop").touch()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 当前身份变化必须阻止信号")
def test_changed_birth_identity_never_receives_cleanup_signal(monkeypatch):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    owner = quality._OwnedPosixProcesses.__new__(quality._OwnedPosixProcesses)
    original = quality._ProcessBirth(123, 1, (10, 20), False)
    replacement = quality._ProcessBirth(123, 1, (11, 20), False)
    owner.table = SimpleNamespace(info=lambda pid: replacement)
    owner.errors = []
    monkeypatch.setattr(owner, "collect", lambda: [original])
    monkeypatch.setattr(quality, "cleanup_owned_process_group", lambda process: True)
    monkeypatch.setattr(
        quality.os,
        "kill",
        lambda *args: pytest.fail("changed identity must not receive a signal"),
    )
    assert not owner.cleanup(SimpleNamespace(pid=456))


@pytest.mark.skipif(os.name == "nt", reason="POSIX 启动前枚举允许无关身份变化")
def test_prelaunch_readable_churn_runs_business_once_with_real_cleanup(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository,
        "from pathlib import Path; p=Path('.ai-sdlc/loops/implementation/test/attempt/count');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )
    original_sample, original_popen = (
        quality._PosixProcessTable.sample,
        quality.subprocess.Popen,
    )
    samples, launchers = [], []

    def sample(table):
        found = original_sample(table)
        if not launchers:
            # 只改变启动前完整枚举中的外部出生身份，启动后使用真实进程表收尾。
            found[999999999] = quality._ProcessBirth(
                999999999, os.getpid(), (len(samples) + 1, 0), False
            )
            samples.append(found)
        return found

    def popen(argv, *args, **kwargs):
        process = original_popen(argv, *args, **kwargs)
        if quality._CONTROLLED_LAUNCHER in argv:
            launchers.append(process)
        return process

    monkeypatch.setattr(quality._PosixProcessTable, "sample", sample)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    result = run_quality_command(options)

    assert result.successful, receipts
    assert (options.controlled.stdout_path.parent / "count").read_text() == "1"
    assert len(launchers) == 1 and launchers[0].poll() == 0
    assert receipts["process"]["pid"] == launchers[0].pid
    assert receipts["cleanup"]["status"] == "complete"
    assert samples and all(
        left != right for left, right in zip(samples, samples[1:], strict=False)
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 启动前的 Linux 父链夹具")
@pytest.mark.parametrize("cycle", ["self", "ancestor"])
def test_prelaunch_linux_ancestor_cycle_refuses_launch_in_bounded_time(
    repository, monkeypatch, cycle
):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('must-not-run')")
    current, ancestor = os.getpid(), 999999999
    reads = []

    class BoundedBaseline(dict):
        def __getitem__(self, pid):
            reads.append(pid)
            assert len(reads) <= len(self) + 1, "cyclic ancestry must not spin"
            return super().__getitem__(pid)

    baseline = BoundedBaseline(
        {
            current: quality._ProcessBirth(
                current, current if cycle == "self" else ancestor, (1, 0), False
            ),
            ancestor: quality._ProcessBirth(ancestor, current, (2, 0), False),
        }
    )
    table = SimpleNamespace(sample=lambda: baseline, snapshot=lambda: baseline)
    monkeypatch.setattr(quality, "_PosixProcessTable", lambda: table)
    monkeypatch.setattr(
        quality, "sys", SimpleNamespace(platform="linux", executable=sys.executable)
    )
    original_popen = quality.subprocess.Popen

    def popen(argv, *args, **kwargs):
        assert quality._CONTROLLED_LAUNCHER not in argv, (
            "invalid ancestry must not launch"
        )
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    result = run_quality_command(options)
    assert not result.successful and result.exit_code is None
    assert "ancestor-cycle" in receipts["raw"]["launch_error"]
    assert "process" not in receipts and not options.controlled.stdout_path.read_bytes()


@pytest.mark.parametrize("table_parent", [False, True])
def test_prelaunch_linux_valid_ancestry_keeps_existing_adopters(
    monkeypatch, table_parent
):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    current, ancestor = os.getpid(), 999999999
    baseline = {current: quality._ProcessBirth(current, ancestor, (1, 0), False)}
    if table_parent:
        baseline[ancestor] = quality._ProcessBirth(ancestor, 1, (2, 0), False)
    table = SimpleNamespace(sample=lambda: baseline, snapshot=lambda: baseline)
    monkeypatch.setattr(quality, "_PosixProcessTable", lambda: table)
    monkeypatch.setattr(quality, "sys", SimpleNamespace(platform="linux"))
    owner = quality._OwnedPosixProcesses()
    assert owner.adopters == ({1, current, ancestor} if table_parent else {1, current})


@pytest.mark.skipif(os.name == "nt", reason="POSIX 原收尾窗口内的查询恢复")
@pytest.mark.parametrize("read_error", [OSError, ValueError, IndexError])
def test_transient_initial_snapshot_recovers_before_one_business_execution(
    repository, monkeypatch, read_error
):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository,
        "from pathlib import Path; p=Path('.ai-sdlc/loops/implementation/test/attempt/count');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )
    original = quality._PosixProcessTable.sample
    calls = 0

    def sample(table):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise read_error("initial-read-unavailable")
        return original(table)

    monkeypatch.setattr(quality._PosixProcessTable, "sample", sample)
    result = run_quality_command(options)
    assert result.successful, receipts
    assert (options.controlled.stdout_path.parent / "count").read_text() == "1"
    assert receipts["process"]["process_tracking"]["baseline_retries"][0] == [
        f"{read_error.__name__}: initial-read-unavailable"
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 基线失败不启动业务")
@pytest.mark.parametrize("read_error", [OSError, ValueError, IndexError])
@pytest.mark.parametrize("timeout", [0.05, 2])
def test_persistent_initial_snapshot_uses_call_limit_without_launching_business(
    repository, monkeypatch, read_error, timeout
):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository,
        "from pathlib import Path; Path('.ai-sdlc/loops/implementation/test/attempt/count')"
        ".write_text('must-not-run')",
        timeout=timeout,
    )
    clock = [0.0]
    monkeypatch.setattr(
        quality,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            time_ns=time.time_ns,
        ),
    )

    def unavailable(table):
        raise read_error("initial-query-unavailable")

    original_popen = quality.subprocess.Popen

    def popen(argv, *args, **kwargs):
        assert quality._CONTROLLED_LAUNCHER not in argv, (
            "business launcher must stay absent"
        )
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(quality._PosixProcessTable, "sample", unavailable)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    result = run_quality_command(options)
    assert not result.successful and result.exit_code is None
    assert clock[0] == pytest.approx(min(timeout, 0.25))
    assert (
        f"{read_error.__name__}: initial-query-unavailable"
        in receipts["raw"]["launch_error"]
    )
    assert "process" not in receipts and not options.controlled.stdout_path.read_bytes()
    assert not (options.controlled.stdout_path.parent / "count").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 暂态观察需确认消失")
def test_unattributed_observation_rechecks_absence_without_replaying_or_signaling(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository,
        "from pathlib import Path; p=Path('.ai-sdlc/loops/implementation/test/attempt/count');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    )
    original_collect, original_kill = (
        quality._OwnedPosixProcesses.collect,
        quality.os.kill,
    )
    calls = 0
    vanished = (999999999, 1, 2)

    def collect(owner):
        nonlocal calls
        calls += 1
        found = original_collect(owner)
        if calls == 1:
            owner.unresolved.append(vanished)
        return found

    def kill(pid, sig):
        assert pid != vanished[0], "unattributed identity must not receive a signal"
        return original_kill(pid, sig)

    monkeypatch.setattr(quality._OwnedPosixProcesses, "collect", collect)
    monkeypatch.setattr(quality.os, "kill", kill)
    result = run_quality_command(options)
    assert result.successful, receipts
    assert calls >= 2
    assert (options.controlled.stdout_path.parent / "count").read_text() == "1"
    tracking = receipts["cleanup"]["process_tracking"]
    assert [vanished] in tracking["unattributed_rechecks"]
    assert not tracking["unattributed"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 持续未知保留原收尾上限")
def test_persistent_unattributed_observation_remains_incomplete_at_original_deadline(
    monkeypatch,
):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    clock = [0.0]
    monkeypatch.setattr(
        quality,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    owner = quality._OwnedPosixProcesses.__new__(quality._OwnedPosixProcesses)
    owner.unresolved = [(123, 1, 2)]
    owner.unattributed_rechecks, owner.snapshot_retries, owner.errors = [], [], []
    monkeypatch.setattr(owner, "collect", lambda: [])
    monkeypatch.setattr(quality, "cleanup_owned_process_group", lambda process: True)
    monkeypatch.setattr(
        quality.os, "kill", lambda *args: pytest.fail("unknown identity")
    )
    assert not owner.cleanup(SimpleNamespace(pid=456))
    assert clock[0] == pytest.approx(2.25)
    assert owner.unresolved == [(123, 1, 2)] and owner.unattributed_rechecks


@pytest.mark.skipif(os.name == "nt", reason="POSIX 原收尾窗口内的查询恢复")
def test_transient_cleanup_snapshot_recovers_without_replaying_business(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository,
        "import pathlib; p=pathlib.Path('.ai-sdlc/loops/implementation/test/attempt/business-count');"
        "p.write_text(str(int(p.read_text())+1) if p.exists() else '1');"
        "print('saved')",
    )
    original = quality._OwnedPosixProcesses.collect
    calls = 0

    def collect(owner):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise quality._ProcessTableNotStableError(["temporary-table-change"])
        return original(owner)

    monkeypatch.setattr(quality._OwnedPosixProcesses, "collect", collect)
    result = run_quality_command(options)
    assert result.successful, (result, receipts)
    assert (options.controlled.stdout_path.parent / "business-count").read_text() == "1"
    assert receipts["raw"]["exit_code"] == 0
    assert receipts["cleanup"]["status"] == "complete"
    retries = receipts["cleanup"]["process_tracking"]["snapshot_retries"]
    assert retries[0]["reasons"] == ["temporary-table-change"]
    assert calls >= 2
    # 已恢复的采样重试仍保留；新的共享读取判据应接受这份真实完整回执。
    quality.validate_controlled_receipts(
        receipts["process"],
        receipts["raw"],
        receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 原收尾时间上限")
def test_persistent_snapshot_failure_keeps_original_cleanup_deadline(monkeypatch):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    clock = [0.0]
    monkeypatch.setattr(
        quality,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    owner = quality._OwnedPosixProcesses.__new__(quality._OwnedPosixProcesses)
    owner.errors, owner.snapshot_retries = [], []

    def unavailable():
        raise quality._ProcessTableNotStableError(["persistent-read-failure"])

    monkeypatch.setattr(owner, "collect", unavailable)
    monkeypatch.setattr(quality, "cleanup_owned_process_group", lambda process: True)
    monkeypatch.setattr(
        quality.os,
        "kill",
        lambda *args: pytest.fail("unconfirmed identity must not receive a signal"),
    )
    assert not owner.cleanup(SimpleNamespace(pid=456))
    assert 2.25 <= clock[0] <= 2.26
    assert 2 <= len(owner.snapshot_retries) <= 120
    assert all(
        item["reasons"] == ["persistent-read-failure"]
        for item in owner.snapshot_retries
    )


def test_unstable_snapshot_preserves_bounded_query_reasons(monkeypatch):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    table = quality._PosixProcessTable.__new__(quality._PosixProcessTable)
    results = iter(
        [OSError("query-unavailable"), {}, {1: object()}, ValueError("bad-row")]
    )

    def sample():
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(table, "sample", sample)
    monkeypatch.setattr(quality, "time", SimpleNamespace(sleep=lambda seconds: None))
    with pytest.raises(quality._ProcessTableNotStableError) as failure:
        table.snapshot()
    assert failure.value.reasons == (
        "OSError: query-unavailable",
        "process-table-changed-between-samples",
        "ValueError: bad-row",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 查询与发信号间的自然退出反例")
@pytest.mark.parametrize("still_alive", [False, True])
def test_cleanup_permission_error_requires_fresh_group_state(monkeypatch, still_alive):
    from ai_sdlc.core import quality_command

    class OwnedProcess:
        pid = 43210

        def poll(self):
            return None

    observations = iter(([43210], [43210] if still_alive else []))
    queries = []

    def members(pid):
        queries.append(pid)
        return next(observations)

    def signal(*args):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(quality_command, "_posix_group_members", members)
    monkeypatch.setattr(quality_command.os, "killpg", signal)
    assert quality_command.cleanup_owned_process_group(OwnedProcess()) is (
        not still_alive
    )
    assert queries == [43210, 43210]


_REDIRECTION_ENV = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_REPLACE_REF_BASE",
}


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repository: Path, *argv: str, timeout: float = 5) -> object:
    return run_quality_command(
        QualityCommandOptions(
            root=repository,
            cwd=repository,
            argv=tuple(argv),
            timeout_seconds=timeout,
        )
    )


def test_reviewed_parent_digest_preserves_staged_source_after_commit_without_writes(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repository, check=True, capture_output=True, text=True
        ).stdout.strip()

    parent = git("rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("staged change\n")
    git("add", "tracked.txt")
    tree = git("write-tree")
    before = build_source_digest(repository)
    git("commit", "-qm", "reviewed delivery")
    assert before != build_source_digest(repository)
    original_git = quality_command._git_text

    def readonly_git(root, env, *args):
        assert "write-tree" not in args
        return original_git(root, env, *args)

    monkeypatch.setattr(quality_command, "_git_text", readonly_git)
    assert (
        build_source_digest_at_reviewed_parent(
            repository, reviewed_parent=parent, reviewed_tree=tree
        )
        == before
    )
    (repository / "untracked.txt").write_text("new unreviewed input")
    assert (
        build_source_digest_at_reviewed_parent(
            repository, reviewed_parent=parent, reviewed_tree=tree
        )
        != before
    )


def test_reviewed_parent_digest_rejects_real_head_change_during_capture(
    repository, monkeypatch
):
    from ai_sdlc.core import quality_command

    parent = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repository, text=True
    ).strip()
    original = quality_command._source_identity_payload
    calls = 0

    def moving_head(root, env, **kwargs):
        nonlocal calls
        payload = original(root, env, **kwargs)
        calls += 1
        if calls == 1:
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "concurrent commit"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
        return payload

    monkeypatch.setattr(quality_command, "_source_identity_payload", moving_head)
    with pytest.raises(ValueError, match="changed during reviewed-parent"):
        build_source_digest_at_reviewed_parent(
            repository, reviewed_parent=parent, reviewed_tree=tree
        )


@pytest.mark.parametrize("value", ["HEAD", "HEAD~1", "--help", "a" * 39])
def test_reviewed_parent_digest_requires_full_object_id(repository, value):
    with pytest.raises(ValueError, match="full Git object"):
        build_source_digest_at_reviewed_parent(
            repository, reviewed_parent=value, reviewed_tree="a" * 40
        )


def test_quality_command_executes_direct_argv_and_records_clean_identity(
    repository: Path,
) -> None:
    result = _run(repository, sys.executable, "-c", "print('ok')")

    assert result.successful is True
    assert result.exit_code == 0
    assert result.stdout_tail == f"ok{os.linesep}"
    assert result.source_digest_before == result.source_digest_after
    assert result.source_digest_before == build_source_digest(repository)


def test_quality_command_nonzero_exit_fails(repository: Path) -> None:
    result = _run(repository, sys.executable, "-c", "raise SystemExit(7)")

    assert result.status == "failed"
    assert result.exit_code == 7


def test_quality_command_timeout_fails(repository: Path) -> None:
    result = _run(
        repository,
        sys.executable,
        "-c",
        "import time; time.sleep(2)",
        timeout=0.05,
    )

    assert result.status == "timed_out"
    assert result.exit_code is None
    assert result.timed_out is True


def test_quality_command_rejects_cwd_escape(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent

    with pytest.raises(ValueError, match="cwd escapes"):
        run_quality_command(
            QualityCommandOptions(
                root=repository,
                cwd=outside,
                argv=(sys.executable, "-c", "pass"),
            )
        )


def test_quality_command_rejects_source_mutation(repository: Path) -> None:
    result = _run(
        repository,
        sys.executable,
        "-c",
        "from pathlib import Path; Path('tracked.txt').write_text('changed\\n')",
    )

    assert result.status == "source_changed"
    assert result.source_digest_before != result.source_digest_after


def test_source_digest_binds_untracked_content(repository: Path) -> None:
    untracked = repository / "new.txt"
    untracked.write_text("one\n", encoding="utf-8")
    first = build_source_digest(repository)
    untracked.write_text("two\n", encoding="utf-8")

    assert build_source_digest(repository) != first


def test_quality_command_output_tail_is_bounded(repository: Path) -> None:
    result = run_quality_command(
        QualityCommandOptions(
            root=repository,
            cwd=repository,
            argv=(sys.executable, "-c", "print('x' * 10000)"),
            output_tail_bytes=128,
        )
    )

    assert result.status == "passed"
    assert len(result.stdout_tail.encode("utf-8")) <= 128


def test_quality_command_does_not_interpret_shell_metacharacters(
    repository: Path,
) -> None:
    marker = repository / "must-not-exist"
    result = _run(
        repository,
        sys.executable,
        "-c",
        "import sys; print(sys.argv[1])",
        f"; touch {marker}",
    )

    assert result.status == "passed"
    assert not marker.exists()
    assert result.stdout_tail.strip() == f"; touch {marker}"


def test_quality_environment_preserves_enterprise_inputs_and_removes_redirects(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preserved = {
        "HTTP_PROXY": "http://proxy.example",
        "HTTPS_PROXY": "https://proxy.example",
        "NO_PROXY": "localhost",
        "PIP_INDEX_URL": "https://packages.example/simple",
        "NPM_CONFIG_REGISTRY": "https://packages.example/npm",
        "GIT_SSH_COMMAND": "ssh -F enterprise.conf",
    }
    for name, value in preserved.items():
        monkeypatch.setenv(name, value)
    for name in _REDIRECTION_ENV:
        monkeypatch.setenv(name, f"redirected-{name}")

    environment = quality_command_environment(os.environ)
    assert {name: environment[name] for name in preserved} == preserved
    assert _REDIRECTION_ENV.isdisjoint(environment)

    result = _run(
        repository,
        sys.executable,
        "-c",
        (
            "import json, os; "
            "print(json.dumps({k: os.environ.get(k) for k in "
            f"{sorted([*preserved, *_REDIRECTION_ENV])!r}}}))"
        ),
    )
    child = json.loads(result.stdout_tail)
    assert {name: child[name] for name in preserved} == preserved
    assert all(child[name] is None for name in _REDIRECTION_ENV)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 原始父身份的普通并发进程")
@pytest.mark.parametrize("native_available", [True, False])
def test_unrelated_reparented_child_requires_affirmative_parent_evidence(
    repository, monkeypatch, native_available
):
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('complete')", timeout=8)
    folder = options.controlled.stdout_path.parent
    child = (
        "import os,pathlib,sys,time\n"
        "root=pathlib.Path(sys.argv[1]);(root/'other-ready').write_text(str(os.getpid()))\n"
        "end=time.monotonic()+8\n"
        "while time.monotonic()<end and not (root/'other-stop').exists():\n"
        " (root/'other-value').write_text(str(time.monotonic()));time.sleep(.01)\n"
    )
    parent = (
        "import pathlib,subprocess,sys,time\n"
        f"root=pathlib.Path({str(folder)!r});end=time.monotonic()+12\n"
        "(root/'other-parent-ready').touch()\n"
        "while not (root/'other-go').exists() and time.monotonic()<end:time.sleep(.01)\n"
        "if not (root/'other-go').exists():raise SystemExit(1)\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r},str(root)],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)\n"
        "end=time.monotonic()+3\n"
        "while not (root/'other-value').exists() and time.monotonic()<end:time.sleep(.01)\n"
        "assert (root/'other-value').exists()\n"
    )
    other = subprocess.Popen([sys.executable, "-c", parent])
    original_callback = options.controlled.on_started
    foreign_birth = None

    def started(value):
        nonlocal foreign_birth
        original_callback(value)
        # 外部父进程已在基线中；让它在业务启动器创建后产生子进程并自然退出。
        (folder / "other-go").touch()
        assert other.wait(timeout=4) == 0
        pid = int((folder / "other-ready").read_text())
        foreign_birth = quality._PosixProcessTable().info(pid)
        assert foreign_birth.parent == 1 and not foreign_birth.zombie

    if not native_available:
        monkeypatch.setattr(
            quality._PosixProcessTable, "original_parent_identity", lambda *_: None
        )
    try:
        end = time.monotonic() + 3
        while not (folder / "other-parent-ready").exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert (folder / "other-parent-ready").exists()
        result = run_quality_command(
            replace(options, controlled=replace(options.controlled, on_started=started))
        )
        (folder / "completion-regression.json").write_text(
            json.dumps(
                {
                    "result": result.model_dump(mode="json"),
                    "receipts": receipts,
                    "foreign_birth": foreign_birth.key if foreign_birth else None,
                }
            )
        )
        assert result.successful == native_available, receipts
        assert receipts["raw"]["exit_code"] == 0
        assert receipts["cleanup"]["status"] == (
            "complete" if native_available else "incomplete"
        )
        assert foreign_birth is not None
        current = quality._PosixProcessTable().info(foreign_birth.pid)
        assert current.key == foreign_birth.key and not current.zombie
        assert foreign_birth.pid not in {
            row[0] for row in receipts["cleanup"]["process_tracking"]["owned"]
        }
        before = (folder / "other-value").read_bytes()
        time.sleep(0.05)
        assert (folder / "other-value").read_bytes() != before
    finally:
        # 测试只通过自有临时文件让协作进程退出，不向未归属身份发送信号。
        (folder / "other-stop").touch()
        if other.poll() is None:
            other.terminate()
        other.wait(timeout=3)
        if foreign_birth is not None:
            end = time.monotonic() + 3
            while time.monotonic() < end:
                try:
                    current = quality._PosixProcessTable().info(foreign_birth.pid)
                except ProcessLookupError:
                    break
                if current.key != foreign_birth.key or current.zombie:
                    break
                time.sleep(0.01)
            else:
                pytest.fail(
                    "ordinary foreign helper did not finish after its stop file"
                )


@pytest.mark.parametrize(
    "change", ["none", "job-mismatch", "remaining-process", "boolean-count"]
)
def test_controlled_windows_job_receipt_schema_requires_matching_empty_job(change):
    import hashlib

    # 这里只验证平台回执合同，不将构造数据当成 Windows 进程执行实证。
    from ai_sdlc.core.quality_command import validate_controlled_receipts

    process = {
        "schema_version": 1,
        "pid": 123,
        "process_group": None,
        "job_name": "ai-sdlc-test-job",
        "ownership_nonce": "test-nonce",
        "started_at_ms": 1000,
        "launcher": "nonce-gated-child",
        "process_tracking": None,
    }
    raw = {
        "schema_version": 1,
        "launch_status": "started",
        "ownership_nonce": "test-nonce",
        "started_at_ms": 1000,
        "ended_at_ms": 2000,
        "exit_code": 20,
        "timed_out": True,
        "output_truncated": False,
        "output_io_error": False,
        "launch_error": "",
        "output_observed_bytes": 0,
        "output_written_bytes": 0,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }
    cleanup = {
        "schema_version": 1,
        "launch_status": "started",
        "ownership_nonce": "test-nonce",
        "status": "complete",
        "checked_at_ms": 2001,
        "process_tracking": None,
        "job_name": "ai-sdlc-test-job",
        "job_active_processes": 0,
    }
    if change == "job-mismatch":
        cleanup["job_name"] = "another-job"
    elif change == "remaining-process":
        cleanup["job_active_processes"] = 1
    elif change == "boolean-count":
        cleanup["job_active_processes"] = False
    if change == "none":
        validate_controlled_receipts(
            process, raw, cleanup, ownership_nonce="test-nonce"
        )
    else:
        with pytest.raises(ValueError, match="receipt-incomplete-or-conflicting"):
            validate_controlled_receipts(
                process, raw, cleanup, ownership_nonce="test-nonce"
            )


@pytest.mark.parametrize("reject", [False, True])
def test_v16_controlled_source_capture_reaches_nonce_guard_once(
    repository, monkeypatch, reject
):
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('business-started')")
    original_capture = quality.build_source_digest
    captures = []

    def capture(*args, **kwargs):
        value = original_capture(*args, **kwargs)
        captures.append(value)
        return value

    original_started = options.controlled.on_started

    def started(payload):
        original_started(payload)
        assert payload["source_digest_before"] == captures[0]
        if reject:
            raise ValueError("frozen source mismatch")

    monkeypatch.setattr(quality, "build_source_digest", capture)
    options = replace(
        options, controlled=replace(options.controlled, on_started=started)
    )
    if reject:
        with pytest.raises(ValueError, match="frozen source mismatch"):
            run_quality_command(options)
        assert options.controlled.stdout_path.read_bytes() == b""
    else:
        assert run_quality_command(options).successful
        assert options.controlled.stdout_path.read_text().strip() == "business-started"
    assert len(captures) == (1 if reject else 2)
    assert receipts["raw"]["launch_status"] == "started"
    assert receipts["cleanup"]["status"] == "complete"
    quality.validate_controlled_receipts(
        receipts["process"],
        receipts["raw"],
        receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="真实 macOS 缺 waitid 分支")
@pytest.mark.parametrize("mode", ["success", "nonzero", "signal"])
def test_fix20_missing_waitid_reads_real_exit_without_reaping(monkeypatch, mode):
    import signal

    from ai_sdlc.core import quality_command as quality

    code, expected = {
        "success": ("raise SystemExit(0)", 0),
        "nonzero": ("raise SystemExit(7)", 7),
        "signal": (
            "import os,signal;os.kill(os.getpid(),signal.SIGTERM)",
            -signal.SIGTERM,
        ),
    }[mode]
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def forbidden_reap(*args, **kwargs):
        pytest.fail("终止归属证明前不得调用 poll/wait 提前回收 leader")

    try:
        with monkeypatch.context() as patch:
            patch.delattr(quality.os, "waitid", raising=False)
            patch.setattr(process, "poll", forbidden_reap)
            patch.setattr(process, "wait", forbidden_reap)
            deadline = time.monotonic() + 2
            actual = quality._current_child_exit(process)
            while actual is None and time.monotonic() < deadline:
                time.sleep(0.01)
                actual = quality._current_child_exit(process)
            assert actual == expected
            assert process.returncode is None
            assert quality._current_child_exit(process) == expected
        # 真实 wait 仍能取得同一结果，证明状态查询没有提前消费内核退出记录。
        assert process.wait(timeout=2) == expected
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=2)


@pytest.mark.skipif(sys.platform != "darwin", reason="真实 macOS 缺 waitid 分支")
@pytest.mark.parametrize("mode", ["success", "nonzero", "timeout"])
def test_fix20_missing_waitid_preserves_detached_cleanup(
    repository, monkeypatch, mode
):
    from ai_sdlc.core import quality_command as quality

    before_cleanup = []
    original_cleanup = quality._OwnedPosixProcesses.cleanup

    def cleanup(owner, process):
        before_cleanup.append(process.returncode)
        return original_cleanup(owner, process)

    with monkeypatch.context() as patch:
        patch.delattr(quality.os, "waitid", raising=False)
        patch.setattr(quality._OwnedPosixProcesses, "cleanup", cleanup)
        # 复用实际脱离进程组的合法执行、非零退出与超时收尾，不另建执行器夹具。
        test_controlled_detached_helper_really_stops_writing(repository, mode)
    assert before_cleanup == [None]


@pytest.mark.skipif(sys.platform != "darwin", reason="真实 macOS 缺 waitid 分支")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_fix20_provider_consumes_missing_waitid_completion(
    repository, monkeypatch, exit_code
):
    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.pr_review_provider import (
        ProviderCommandOptions,
        ProviderRunStatus,
        run_provider_command,
    )
    from tests.unit.test_pr_review_provider import (
        _write_review_pack,
        _write_reviewer_script,
    )

    pack = _write_review_pack(repository)
    script = _write_reviewer_script(repository, exit_code=exit_code, verdict="clean")
    with monkeypatch.context() as patch:
        patch.delattr(quality.os, "waitid", raising=False)
        result = run_provider_command(
            ProviderCommandOptions(
                root=repository,
                review_pack_path=pack,
                command=[sys.executable, str(script)],
                timeout_seconds=3,
            )
        )
    assert result.status == (
        ProviderRunStatus.SUCCESS if exit_code == 0 else ProviderRunStatus.BLOCKED
    )
    assert result.invocation is not None
    assert result.invocation.launch_status == "started"
    assert result.invocation.exit_code == exit_code
    assert result.invocation.completion_proof is not None
    raw = result.invocation.completion_proof.require_complete()
    assert raw["exit_code"] == exit_code


def _fix23_ps_availability(monkeypatch, missing):
    from ai_sdlc.core import quality_command as quality

    original = quality.subprocess.run
    calls = []

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv[0] == "ps":
            calls.append(tuple(argv))
            if missing == "unsupported":
                # 真实子进程拒绝参数并非找不到可执行文件，仍须查询原生进程表。
                return original(
                    [sys.executable, "-c", "import sys; sys.exit(1)", *argv[1:]],
                    *args,
                    **kwargs,
                )
            if missing:
                raise FileNotFoundError(2, "ps unavailable", "ps")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(quality.subprocess, "run", run)
    return calls


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组消费者")
@pytest.mark.parametrize("missing_ps", [False, True, "unsupported"])
def test_fix23_quality_completes_with_and_without_ps(
    repository, monkeypatch, missing_ps
):
    calls = _fix23_ps_availability(monkeypatch, missing_ps)
    options, receipts = _controlled_options(repository, "print('complete')")
    result = run_quality_command(options)
    assert calls and result.successful
    assert receipts["raw"]["exit_code"] == 0
    assert receipts["cleanup"]["status"] == "complete"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 清理能力必须在启动前可证实")
@pytest.mark.parametrize("missing_ps", [True, "unsupported"])
@pytest.mark.parametrize("failure", ["permission", "visibility", "malformed", "empty"])
def test_fix23_unavailable_native_group_never_launches_business(
    repository, monkeypatch, failure, missing_ps
):
    from ai_sdlc.core import quality_command as quality

    marker = repository / ".ai-sdlc/state/must-not-launch"
    options, receipts = _controlled_options(
        repository, f"from pathlib import Path; Path({str(marker)!r}).write_text('launched')"
    )
    calls = _fix23_ps_availability(monkeypatch, missing_ps)
    queries, launchers = [], []
    original_popen = quality.subprocess.Popen

    def group_members(self, group_id):
        queries.append(group_id)
        if failure == "empty":
            return []
        error = {"permission": PermissionError, "visibility": OSError, "malformed": IndexError}[failure]
        raise error("native full group visibility unavailable")

    def popen(argv, *args, **kwargs):
        if quality._CONTROLLED_LAUNCHER in argv:
            launchers.append(argv)
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(quality._PosixProcessTable, "group_members", group_members)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    result = run_quality_command(options)
    assert calls and queries == [os.getpgrp()]
    assert not launchers and not marker.exists()
    assert not result.successful and result.exit_code is None
    assert "process" not in receipts
    assert receipts["raw"]["launch_status"] == "never_started"
    assert receipts["raw"]["launch_error"].startswith("controlled-process-group-unavailable-before-launch:")
    assert receipts["cleanup"]["launch_status"] == "never_started"
    assert receipts["cleanup"]["status"] == "complete"
    quality.validate_controlled_receipts(
        None, receipts["raw"], receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程组消费者")
@pytest.mark.parametrize("missing_ps", [False, True, "unsupported"])
def test_fix23_provider_completes_with_and_without_ps(
    repository, monkeypatch, missing_ps
):
    from ai_sdlc.core.pr_review_provider import (
        ProviderCommandOptions,
        ProviderRunStatus,
        run_provider_command,
    )
    from tests.unit.test_pr_review_provider import (
        _write_review_pack,
        _write_reviewer_script,
    )

    calls = _fix23_ps_availability(monkeypatch, missing_ps)
    pack = _write_review_pack(repository)
    script = _write_reviewer_script(repository, exit_code=0, verdict="clean")
    result = run_provider_command(
        ProviderCommandOptions(
            root=repository,
            review_pack_path=pack,
            command=[sys.executable, str(script)],
            timeout_seconds=3,
        )
    )
    assert calls and result.status == ProviderRunStatus.SUCCESS
    assert result.invocation is not None
    assert result.invocation.completion_proof is not None
    assert result.invocation.completion_proof.require_complete()["exit_code"] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX 全组原生查询")
@pytest.mark.parametrize("missing_ps", [True, "unsupported"])
def test_fix23_native_group_and_browser_consumer_without_ps(monkeypatch, missing_ps):
    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.frontend_browser_gate_runtime import _kill_posix_process_group

    calls = _fix23_ps_availability(monkeypatch, missing_ps)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        table = quality._PosixProcessTable()
        # 改变当前 UID 筛选配置不会改变全组查询；原生路径没有借用 sample/info。
        table.uid = os.getuid() + 1
        assert process.pid in table.group_members(process.pid)
        assert process.pid in quality._posix_group_members(process.pid)
        _kill_posix_process_group(process)
        assert not quality._posix_group_members(process.pid)
        assert calls and process.wait(timeout=2) != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 脱组后代原生收尾")
@pytest.mark.parametrize("missing_ps", [True, "unsupported"])
@pytest.mark.parametrize("mode", ["success", "double_fork", "nonzero", "timeout", "output_limit"])
def test_fix23_missing_ps_preserves_detached_cleanup(repository, monkeypatch, mode, missing_ps):
    _fix23_ps_availability(monkeypatch, missing_ps)
    test_controlled_detached_helper_really_stops_writing(repository, mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 未归属后代仍拒绝完成")
@pytest.mark.parametrize("missing_ps", [True, "unsupported"])
def test_fix23_missing_ps_preserves_unknown_detached_process(repository, monkeypatch, missing_ps):
    _fix23_ps_availability(monkeypatch, missing_ps)
    test_detached_helper_without_marker_cannot_claim_complete(repository)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 原生查询失败仍阻断")
@pytest.mark.parametrize("missing_ps", [True, "unsupported"])
@pytest.mark.parametrize("error", [PermissionError, ValueError, IndexError])
def test_fix23_missing_ps_native_error_cannot_claim_cleanup(monkeypatch, error, missing_ps):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.frontend_browser_gate_runtime import _kill_posix_process_group

    _fix23_ps_availability(monkeypatch, missing_ps)

    def unavailable(self, group_id):
        raise error("group visibility unavailable")

    monkeypatch.setattr(quality._PosixProcessTable, "group_members", unavailable)
    process = SimpleNamespace(pid=987654321)
    assert not quality.cleanup_owned_process_group(process)
    with pytest.raises(RuntimeError, match="owned-process-cleanup-incomplete"):
        _kill_posix_process_group(process)


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or getattr(os, "getuid", lambda: 1)() != 0,
    reason="真实 Linux 容器 root 验证跨 UID 全组",
)
def test_fix23_linux_native_group_includes_other_uid_without_ps(monkeypatch):
    from ai_sdlc.core import quality_command as quality

    _fix23_ps_availability(monkeypatch, True)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,time; child=os.fork(); "
            "os.setuid(65534) if child == 0 else None; "
            "print(os.getpid(), flush=True) if child == 0 else None; time.sleep(30)",
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        child = int(process.stdout.readline().strip())
        assert (Path("/proc") / str(child)).stat().st_uid == 65534
        assert {process.pid, child} <= set(quality._posix_group_members(process.pid))
        assert quality.cleanup_owned_process_group(process)
        assert not quality._posix_group_members(process.pid)
    finally:
        import signal

        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        process.stdout.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="真实 Linux proc 可见性边界")
@pytest.mark.parametrize("damage", ["hidepid", "permission", "malformed"])
def test_fix23_linux_unknown_proc_visibility_blocks_cleanup(monkeypatch, damage):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    _fix23_ps_availability(monkeypatch, True)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            if damage == "permission":
                raise PermissionError("proc visibility denied")
            if damage == "malformed":
                return "1 2 3 / /proc rw incomplete"
            return original_read(path, *args, **kwargs).replace(" - proc proc ", " - proc proc hidepid=2,")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert not quality.cleanup_owned_process_group(SimpleNamespace(pid=987654321))


@pytest.mark.parametrize("foreign_uid", [False, True])
def test_fix26_darwin_sample_preserves_birth_across_uid_change(monkeypatch, foreign_uid):
    import ctypes
    import struct
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    calls = []
    uid = 501
    observed_uid = uid + 1 if foreign_uid else uid

    class ProcessAPI:
        def proc_listpids(self, kind, selected, array, size):
            calls.append((kind, selected))
            pids = (41, 42) if kind == 1 or observed_uid == selected else (41,)
            if array is not None:
                for index, pid in enumerate(pids):
                    array[index] = pid
            return len(pids) * 4

        def proc_pidinfo(self, pid, flavor, unused, buffer, size):
            assert flavor == 3 and size == 136
            fields = [0] * 12
            fields[1], fields[3], fields[4] = 2, pid, 41 if pid == 42 else 1
            fields[5] = observed_uid if pid == 42 else uid
            raw = bytearray(136)
            struct.pack_into("=12I", raw, 0, *fields)
            struct.pack_into("=QQ", raw, 120, pid * 10, 2)
            ctypes.memmove(buffer, bytes(raw), len(raw))
            return len(raw)

    table = quality._PosixProcessTable.__new__(quality._PosixProcessTable)
    table.uid, table.ctypes, table.lib = uid, ctypes, ProcessAPI()
    monkeypatch.setattr(quality, "sys", SimpleNamespace(platform="darwin"))
    sample = table.sample()
    assert set(sample) == {41, 42}
    assert sample[42].key == (42, 420, 2)
    assert sample[42].parent == 41
    if foreign_uid:
        observed_uid += 1
    assert table.info(42).key == sample[42].key


def _fix26_linux_visible_table(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self/mountinfo").write_text("1 2 0:1 / /proc rw - proc proc rw\n")
    (proc / "self/stat").write_text("41 (parent) S\n")
    for pid, parent in ((41, 1), (42, 41)):
        (proc / str(pid)).mkdir()
        fields = ["S", str(parent), "41"] + ["0"] * 16 + [str(pid * 10)]
        (proc / str(pid) / "stat").write_text(f"{pid} (name) {' '.join(fields)}\n")

    def path(value):
        return proc if str(value) == "/proc" else Path(value)

    table = quality._PosixProcessTable.__new__(quality._PosixProcessTable)
    table.uid = proc.stat().st_uid + 1
    monkeypatch.setattr(quality, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(quality, "Path", path)
    monkeypatch.setattr(quality, "os", SimpleNamespace(getpid=lambda: 41))
    return quality, table, proc


def test_fix26_linux_sample_and_group_share_full_visible_births(tmp_path, monkeypatch):
    _, table, _ = _fix26_linux_visible_table(tmp_path, monkeypatch)
    # 模拟表的调用 UID 与目录 UID 不同，不更改操作系统用户或任何目录权限。
    sample = table.sample()
    assert set(sample) == {41, 42}
    assert sample[42].key == (42, 420, 0)
    assert sample[42].parent == 41
    assert set(table.group_members(41)) == {41, 42}


@pytest.mark.parametrize("consumer", ["sample", "group_members"])
@pytest.mark.parametrize("damage", ["hidepid", "mount", "namespace", "unreadable"])
def test_fix26_linux_incomplete_view_rejects_both_consumers(
    tmp_path, monkeypatch, consumer, damage
):
    _, table, proc = _fix26_linux_visible_table(tmp_path, monkeypatch)
    mount = proc / "self/mountinfo"
    if damage == "hidepid":
        mount.write_text("1 2 0:1 / /proc rw - proc proc rw,hidepid=2\n")
    elif damage == "mount":
        mount.write_text("")
    elif damage == "namespace":
        (proc / "self/stat").write_text("99 (other) S\n")
    else:
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == mount:
                raise PermissionError("fixture visibility unavailable")
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(OSError):
        table.sample() if consumer == "sample" else table.group_members(41)


@pytest.mark.parametrize("ownership", ["known", "unknown"])
def test_fix26_visible_detached_child_cannot_be_false_complete(monkeypatch, ownership):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    parent = quality._ProcessBirth(41, 1, (410, 0), False)
    child = quality._ProcessBirth(42, 1, (420, 0), False)
    baseline = {1: quality._ProcessBirth(1, 0, (1, 0), False)}
    current = dict(baseline)
    table = SimpleNamespace(
        uid=501,
        sample=lambda: dict(current),
        snapshot=lambda: dict(current),
        info=lambda pid: current[pid],
        original_parent_identity=lambda process: None,
        marked=lambda process, cookie: False,
    )
    monkeypatch.setattr(quality, "_PosixProcessTable", lambda: table)
    monkeypatch.setattr(quality, "sys", SimpleNamespace(platform="darwin"))
    owner = quality._OwnedPosixProcesses()
    current[41] = parent
    owner.register(41)
    if ownership == "known":
        # 已观察到的父子关系在脱离原组、改变 UID 后仍由出生身份保留。
        current[42] = quality._ProcessBirth(42, 41, child.birth, False)
        assert {item.pid for item in owner.collect()} == {41, 42}
    current.pop(41)
    current[42] = child
    sent = []
    clock = [0.0]

    def kill(pid, sig):
        sent.append((pid, sig))
        raise PermissionError("fixture cannot stop changed-uid child")

    monkeypatch.setattr(
        quality, "os", SimpleNamespace(getpid=lambda: 99, kill=kill)
    )
    monkeypatch.setattr(
        quality, "time", SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda delay: clock.__setitem__(0, clock[0] + delay),
        )
    )
    # 原组已空仍不足以签署后代完成；此处从未启动或停止真实进程。
    monkeypatch.setattr(quality, "cleanup_owned_process_group", lambda process: True)
    assert not owner.cleanup(SimpleNamespace(pid=41))
    finished = owner.finished()
    assert child.key in finished["observed"]
    if ownership == "known":
        assert child.key in finished["owned"] and sent
        assert finished["errors"] == ["controlled-process-completion-unavailable"]
    else:
        assert child.key in finished["unattributed"] and not sent
        assert clock[0] == pytest.approx(2.25)


def test_fix26_output_read_failure_keeps_original_process_cleanup(repository, monkeypatch):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('original output')")
    real_open = Path.open
    failures = []

    def unavailable_output(path, mode="r", *args, **kwargs):
        if path == options.controlled.stdout_path and mode == "rb" and not failures:
            failures.append(str(path))
            raise OSError("original output read temporarily unavailable")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", unavailable_output)
        with pytest.raises(OSError, match="original output read"):
            run_quality_command(options)
    assert len(failures) == 1
    assert receipts["raw"]["exit_code"] == 0
    assert receipts["cleanup"]["status"] == "complete"
    quality.validate_controlled_receipts(
        receipts["process"], receipts["raw"], receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


@pytest.mark.parametrize("error_type", [OSError, ValueError, RuntimeError, KeyboardInterrupt])
def test_fix26_raw_callback_failure_still_delivers_cleanup(repository, error_type):
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('callback original')")
    originals = []

    def raw_failure(payload):
        originals.append(dict(payload))
        raise error_type("original raw callback failure")

    controlled = replace(options.controlled, on_raw_result=raw_failure)
    with pytest.raises(error_type, match="original raw callback failure"):
        run_quality_command(replace(options, controlled=controlled))
    assert len(originals) == 1 and originals[0]["exit_code"] == 0
    assert receipts["cleanup"]["status"] == "complete"
    # 发布异常的类型不改变已经真实取得的原件；仍传播异常，不能将其作为业务通过。
    assert receipts["cleanup"]["raw_result_original"] == originals[0]
    assert "original raw callback failure" in receipts["cleanup"]["raw_result_persistence_error"]
    assert quality.controlled_raw_original({}, receipts["cleanup"], raw_present=False) == originals[0]
    quality.validate_controlled_receipts(
        receipts["process"], originals[0], receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


def test_fix26_actual_output_must_match_confirmed_writes(repository):
    from dataclasses import replace

    options, receipts = _controlled_options(repository, "print('original output')")
    real_cleanup = options.controlled.on_cleanup

    def alter_after_cleanup(payload):
        real_cleanup(payload)
        options.controlled.stdout_path.write_bytes(b"different disk bytes\n")

    controlled = replace(options.controlled, on_cleanup=alter_after_cleanup)
    with pytest.raises(ValueError, match="controlled-quality-output-digest-mismatch"):
        run_quality_command(replace(options, controlled=controlled))
    assert receipts["cleanup"]["status"] == "complete"



def _fix26_darwin_basic_identity_table(monkeypatch):
    import ctypes
    import errno
    import struct
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    state = {"basic": False, "pid": 42, "birth": (123456, 654321), "failure": None}
    calls = []

    def pidinfo(pid, flavor, unused, buffer, size):
        assert flavor == 3 and size == 136
        if state["basic"]:
            ctypes.set_errno(errno.EPERM if state["failure"] != "primary" else errno.EIO)
            return 0
        fields = [0] * 12
        fields[1], fields[3], fields[4] = 2, 42, 41
        raw = bytearray(136)
        struct.pack_into("=12I", raw, 0, *fields)
        struct.pack_into("=I", raw, 100, 42)
        struct.pack_into("=QQ", raw, 120, *state["birth"])
        ctypes.memmove(buffer, bytes(raw), len(raw))
        return 136

    def sysctl(mib, count, buffer, length, unused, unused_size):
        assert tuple(mib) == (1, 14, 1, 42) and count == 4
        calls.append(tuple(mib))
        if state["failure"] == "denied":
            ctypes.set_errno(errno.EPERM)
            return -1
        raw = bytearray(648)
        struct.pack_into("=qi", raw, 0, *state["birth"])
        # timeval 的微秒仅4字节，其后的非零padding不能并入出生身份。
        raw[12:16] = b"\xff" * 4
        raw[36] = 2
        struct.pack_into("=i", raw, 40, state["pid"])
        struct.pack_into("=ii", raw, 560, 41, 42)
        ctypes.memmove(buffer, bytes(raw), len(raw))
        length._obj.value = 647 if state["failure"] == "length" else 648
        return 0

    def listpids(kind, selected, array, size):
        assert (kind, selected) in {(1, 0), (2, 42)}
        if array is not None:
            array[0] = 42
        return 4

    class DarwinABI:
        def __getattr__(self, name):
            return getattr(ctypes, name)

        @staticmethod
        def sizeof(value):
            if value in (ctypes.c_void_p, ctypes.c_long):
                return 8
            return ctypes.sizeof(value)

    table = quality._PosixProcessTable.__new__(quality._PosixProcessTable)
    table.uid, table.ctypes = 501, DarwinABI()
    table.lib = SimpleNamespace(proc_pidinfo=pidinfo, proc_listpids=listpids)
    table.system = SimpleNamespace(sysctl=sysctl)
    monkeypatch.setattr(quality, "sys", SimpleNamespace(platform="darwin"))
    return quality, table, state, calls


def test_fix26_basic_identity_preserves_exact_birth_and_shared_group(monkeypatch):
    _, table, state, calls = _fix26_darwin_basic_identity_table(monkeypatch)
    original = table.info(42)
    assert not calls
    state["basic"] = True
    assert table.info(42) == original
    assert table.sample() == {42: original}
    assert table.group_members(42) == [42]
    assert calls


@pytest.mark.parametrize("consumer", ["info", "sample", "group"])
@pytest.mark.parametrize("failure", ["denied", "length", "identity", "micros", "primary"])
def test_fix26_basic_identity_rejects_missing_or_invalid_facts(
    monkeypatch, consumer, failure
):
    _, table, state, calls = _fix26_darwin_basic_identity_table(monkeypatch)
    state.update(basic=True, failure=failure)
    if failure == "identity":
        state["pid"] = 99
    elif failure == "micros":
        state["birth"] = (123456, 1000000)
    with pytest.raises(OSError):
        if consumer == "info":
            table.info(42)
        elif consumer == "sample":
            table.sample()
        else:
            table.group_members(42)
    if failure == "primary":
        assert not calls


def test_fix26_basic_identity_changed_birth_never_receives_signal(monkeypatch):
    from types import SimpleNamespace

    quality, table, state, _ = _fix26_darwin_basic_identity_table(monkeypatch)
    original = table.info(42)
    state.update(basic=True, birth=(123456, 654322))
    assert table.info(42).key != original.key
    owner = quality._OwnedPosixProcesses.__new__(quality._OwnedPosixProcesses)
    owner.table, owner.errors = table, []
    monkeypatch.setattr(owner, "collect", lambda: [original])
    monkeypatch.setattr(quality, "cleanup_owned_process_group", lambda process: True)
    monkeypatch.setattr(
        quality.os, "kill", lambda *args: pytest.fail("changed birth must not receive a signal")
    )
    assert not owner.cleanup(SimpleNamespace(pid=41))


@pytest.mark.skipif(os.name == "nt", reason="POSIX live group/snapshot failure boundary")
@pytest.mark.parametrize("fault", ["cleanup-queries", "started-oserror", "started-valueerror", "timeout-control"])
def test_controlled_owned_handles_finalize_when_cleanup_or_started_fails(
    repository, monkeypatch, fault
):
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "pass", timeout=0.5)
    folder = options.controlled.stdout_path.parent
    ready, stop = folder / "business-ready", folder / "business-stop"
    probe, probe_result = folder / "late-write-request", folder / "late-write-result"
    body = (
        "import json,os,pathlib,time; "
        f"ready=pathlib.Path({str(ready)!r}); stop=pathlib.Path({str(stop)!r}); "
        f"probe=pathlib.Path({str(probe)!r}); response=pathlib.Path({str(probe_result)!r}); "
        "ready.write_text(str(os.getpid())); print('actual business started',flush=True); "
        "end=time.monotonic()+20\n"
        "while not stop.exists() and time.monotonic()<end:\n"
        " if probe.exists() and not response.exists():\n"
        "  attempts=[]\n"
        "  for fd in (1,2):\n"
        "   try: attempts.append({'fd':fd,'written':os.write(fd,b'late-after-publication\\n')})\n"
        "   except OSError as exc: attempts.append({'fd':fd,'errno':exc.errno})\n"
        "  temporary=response.with_suffix('.tmp'); temporary.write_text(json.dumps(attempts)); temporary.replace(response)\n"
        " time.sleep(0.01)\n"
    )
    options = replace(options, argv=(sys.executable, "-B", "-c", body))
    original_popen = quality.subprocess.Popen
    original_group = quality._posix_group_members
    original_snapshot = quality._PosixProcessTable.snapshot
    launchers, faults, readers = [], [], []
    original_thread = quality.threading.Thread

    def thread(*args, **kwargs):
        reader = original_thread(*args, **kwargs)
        if getattr(kwargs.get("target"), "__name__", "") == "capture":
            readers.append(reader)
        return reader

    def popen(argv, *args, **kwargs):
        process = original_popen(argv, *args, **kwargs)
        if quality._CONTROLLED_LAUNCHER in argv:
            launchers.append(process)
        return process

    def group(group_id):
        if fault == "cleanup-queries" and ready.exists():
            faults.append("group")
            raise OSError("injected postlaunch group query unavailable")
        return original_group(group_id)

    def snapshot(table):
        if fault == "cleanup-queries" and ready.exists():
            faults.append("snapshot")
            raise OSError("injected postlaunch ownership snapshot unavailable")
        return original_snapshot(table)

    original_started = options.controlled.on_started
    def started(payload):
        original_started(payload)
        if fault.startswith("started-"):
            faults.append("started")
            error = OSError if fault == "started-oserror" else ValueError
            raise error("injected started callback before reader launch")

    publications = []
    original_raw, original_cleanup = options.controlled.on_raw_result, options.controlled.on_cleanup

    def publish(kind, original, payload):
        original(payload)
        (folder / (kind + ".json")).write_text(json.dumps(payload, sort_keys=True))
        publications.append({
            "kind": kind, "at": time.monotonic(),
            "readers_alive": [reader.is_alive() for reader in readers],
            "stdout_hex": options.controlled.stdout_path.read_bytes().hex(),
            "stderr_hex": options.controlled.stderr_path.read_bytes().hex(),
        })

    options = replace(options, controlled=replace(options.controlled, on_started=started,
        on_raw_result=lambda payload: publish("raw-result", original_raw, payload),
        on_cleanup=lambda payload: publish("cleanup", original_cleanup, payload)))
    # 本例的查询故障立即返回：业务 .5s、Popen 回收 2s、排空 2s、取消调度 .5s，
    # 另留 3s 给启动、源码读取和持久化；此断言不是任意文件系统故障的产品 SLA。
    call_limit = options.timeout_seconds + 2 + 2 + 0.5 + 3
    call_started = time.monotonic()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(quality.threading, "Thread", thread)
            patch.setattr(quality.subprocess, "Popen", popen)
            patch.setattr(quality, "_posix_group_members", group)
            patch.setattr(quality._PosixProcessTable, "snapshot", snapshot)
            if fault == "started-valueerror":
                with pytest.raises(ValueError, match="started callback before reader"):
                    run_quality_command(options)
                result = None
            else:
                result = run_quality_command(options)
        call_returned = time.monotonic()
        readers_on_return = [{"name": r.name, "ident": r.ident, "alive": r.is_alive()} for r in readers]
        assert len(launchers) == 1
        launcher = launchers[0]
        unknown_business_live = None
        if fault == "cleanup-queries":
            state = subprocess.run(
                ["ps", "-p", ready.read_text(), "-o", "stat="],
                capture_output=True, text=True, check=False, timeout=2,
            ).stdout.strip()
            unknown_business_live = bool(state) and not state.startswith("Z")
        artifacts = (options.controlled.stdout_path, options.controlled.stderr_path,
                     folder / "raw-result.json", folder / "cleanup.json")
        before_probe = {path.name: path.read_bytes().hex() for path in artifacts}
        late_writes, writer_live_after_probe = None, None
        if fault == "cleanup-queries":
            probe.write_text("write after original publication")
            probe_deadline = time.monotonic() + 2
            while not probe_result.exists() and time.monotonic() < probe_deadline:
                time.sleep(0.01)
            late_writes = json.loads(probe_result.read_text()) if probe_result.exists() else None
            # 给仍在运行的误留 reader 一次实际处理晚写的窗口，再回读已发布原件。
            time.sleep(0.1)
            state = subprocess.run(
                ["ps", "-p", ready.read_text(), "-o", "stat="],
                capture_output=True, text=True, check=False, timeout=2,
            ).stdout.strip()
            writer_live_after_probe = bool(state) and not state.startswith("Z")
        after_probe = {path.name: path.read_bytes().hex() for path in artifacts}
        observed = {
            "call_started": call_started, "call_returned": call_returned,
            "call_elapsed": call_returned - call_started, "call_limit": call_limit,
            "call_deadline": call_started + call_limit,
            "publications": publications, "before_probe": before_probe,
            "after_probe": after_probe, "late_writes": late_writes,
            "writer_live_after_probe": writer_live_after_probe,
            "readers": readers_on_return,
            "readers_after_probe": [{"name": r.name, "ident": r.ident, "alive": r.is_alive()} for r in readers],
            "unknown_business_live": unknown_business_live,
            "fault": fault, "faults": faults,
            "business_started": ready.exists(), "launcher_returncode": launcher.poll(),
            "pipes_closed": {name: getattr(launcher, name).closed for name in ("stdin", "stdout", "stderr")},
            "raw": receipts.get("raw"), "cleanup": receipts.get("cleanup"),
            "result": result.model_dump() if hasattr(result, "model_dump") else None,
        }
        (folder / "observed-before-test-cleanup.json").write_text(json.dumps(observed, indent=2)+"\n")
        assert call_returned <= call_started + call_limit, "call must return inside its measured execution and cleanup allowance"
        assert [item["kind"] for item in publications] == ["raw-result", "cleanup"]
        assert all(not any(item["readers_alive"]) for item in publications), "readers must finish before original publication"
        assert all(item["stdout_hex"] == after_probe["stdout"] and item["stderr_hex"] == after_probe["stderr"] for item in publications)
        assert before_probe == after_probe, "late writer activity must not change published originals or outputs"
        # 原生归属元组按 JSON 数组发布；核对实际回调的序列化字节，不混比内存容器类型。
        assert bytes.fromhex(after_probe["raw-result.json"]) == json.dumps(receipts["raw"], sort_keys=True).encode()
        assert bytes.fromhex(after_probe["cleanup.json"]) == json.dumps(receipts["cleanup"], sort_keys=True).encode()
        assert len(readers_on_return) == (0 if fault.startswith("started-") else 2)
        assert all(not item["alive"] for item in observed["readers_after_probe"])
        if fault == "cleanup-queries":
            assert writer_live_after_probe and late_writes is not None
            assert [item["fd"] for item in late_writes] == [1, 2], "unknown business must actually attempt both late writes"
            assert ready.exists() and set(faults) == {"group", "snapshot"}
            assert unknown_business_live, "unknown descendants must not receive fallback signals"
            assert receipts["cleanup"]["status"] == "incomplete"
            assert "controlled-process-completion-unavailable" in receipts["cleanup"]["process_tracking"]["errors"]
        else:
            assert receipts["cleanup"]["status"] == "complete"
            assert ready.exists() == (fault == "timeout-control")
        assert launcher.poll() is not None, "owned launcher must be reaped even when descendant cleanup remains unknown"
        assert all(not r["alive"] for r in observed["readers"]), "owned output readers must finish before return, even while unknown writers remain live"
        assert all(observed["pipes_closed"].values()), "every owned Popen pipe must be closed even without readers"
        assert result is None or not result.successful
    finally:
        # 测试只在外部结果已保存后清理自己的真实业务；这不冒充产品清理成功。
        stop.write_text("stop")
        for launcher in launchers:
            quality.cleanup_owned_process_group(launcher)
            if launcher.poll() is None:
                launcher.kill()
            launcher.wait(timeout=2)
            for stream in (launcher.stdin, launcher.stdout, launcher.stderr):
                stream.close()


@pytest.mark.parametrize("ending", ["cancel-with-live-writer", "eof"])
def test_controlled_pipe_capture_finishes_without_closing_unknown_writer(tmp_path, ending):
    import threading

    from ai_sdlc.core import quality_command as quality

    read_fd, write_fd = os.pipe()
    pipe = os.fdopen(read_fd, "rb")
    stop, captured = threading.Event(), threading.Event()
    chunks, errors = [], []
    expected = b"actual pipe output\n"

    def capture():
        try:
            for chunk in quality._controlled_pipe_chunks(pipe, stop):
                chunks.append(chunk)
                if b"".join(chunks) == expected:
                    captured.set()
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            pipe.close()

    reader = threading.Thread(target=capture, daemon=True)
    writer_open = True
    try:
        reader.start()
        os.write(write_fd, expected)
        assert captured.wait(2), "capture must consume actual pipe bytes before cancellation"
        if ending == "eof":
            os.close(write_fd)
            writer_open = False
        else:
            stop.set()
        reader.join(timeout=2)
        observed = {
            "reader_alive": reader.is_alive(), "writer_open": writer_open,
            "output": b"".join(chunks).decode(), "errors": errors,
            "pipe_closed": pipe.closed,
        }
        (tmp_path / "observed-before-writer-cleanup.json").write_text(json.dumps(observed, indent=2))
        assert not reader.is_alive(), "owned reader must finish while the writer is still open"
        assert not errors and pipe.closed and b"".join(chunks) == expected
        assert writer_open == (ending == "cancel-with-live-writer")
    finally:
        # 先保存产品读取结果，再释放测试写端；不靠测试清理解除被测读取。
        stop.set()
        if writer_open:
            os.close(write_fd)
        reader.join(timeout=2)
        pipe.close()


@pytest.mark.parametrize("status", [0, 1, 247, 0x7FFFFFFF, 0x80000000, 0xC0000602, 0xFFFFFFFF])
def test_controlled_launcher_preserves_windows_dword_exit_bits(status):
    import builtins
    import io
    from types import SimpleNamespace

    from ai_sdlc.core.quality_command import _CONTROLLED_LAUNCHER

    launches = []
    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(wait=lambda: status)

    modules = {
        "os": SimpleNamespace(name="nt"), "signal": SimpleNamespace(),
        "subprocess": SimpleNamespace(Popen=popen, DEVNULL=-3),
        "sys": SimpleNamespace(argv=["launcher", "nonce", "child", "argument"],
                               stdin=SimpleNamespace(buffer=io.BytesIO(b"nonce\n"))),
    }
    namespace = {**vars(builtins), "__import__": lambda name, *args: modules[name]}
    with pytest.raises(SystemExit) as stopped:
        exec(_CONTROLLED_LAUNCHER, {"__builtins__": namespace})
    expected = status if status < 0x80000000 else status - 0x100000000
    assert stopped.value.code == expected
    assert stopped.value.code & 0xFFFFFFFF == status
    assert launches == [(["child", "argument"], {"stdin": -3, "shell": False})]


@pytest.mark.skipif(os.name != "nt", reason="Windows native DWORD exit requires Windows CI")
@pytest.mark.parametrize("status", [0, 1, 247, 0xC0000602])
def test_controlled_windows_native_exit_status_is_not_replaced(repository, status):
    options, receipts = _controlled_options(repository,
        "import ctypes; print('original Windows output',flush=True); "
        "exit_process=ctypes.windll.kernel32.ExitProcess; "
        "exit_process.argtypes=[ctypes.c_uint]; exit_process.restype=None; "
        f"exit_process({status})", timeout=5)
    result = run_quality_command(options)
    assert result.exit_code == receipts["raw"]["exit_code"] == status
    assert receipts["cleanup"]["status"] == "complete"
    assert result.successful == (status == 0)
    assert options.controlled.stdout_path.read_text() == "original Windows output\n"


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_controlled_capture_read_error_remains_failure_after_real_output(
    repository, monkeypatch, failure
):
    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "import os; os.write(1,b'before read failure\\n')")
    original = quality._controlled_pipe_chunks
    injections = []
    def chunks(pipe, stop):
        for chunk in original(pipe, stop):
            yield chunk
            injections.append(chunk)
            raise failure("actual capture read failed after original bytes")

    monkeypatch.setattr(quality, "_controlled_pipe_chunks", chunks)
    error = None
    try:
        result = run_quality_command(options)
    except RuntimeError as exc:
        error, result = exc, None
    observed = {
        "error": repr(error) if error is not None else None,
        "result": result.model_dump() if result is not None else None,
        "raw": receipts.get("raw"), "cleanup": receipts.get("cleanup"),
    }
    (options.controlled.stdout_path.parent / "capture-error-observed.json").write_text(json.dumps(observed, indent=2))
    if failure is RuntimeError:
        assert isinstance(error, RuntimeError) and "actual capture read failed" in str(error)
    else:
        assert error is None
    assert injections == [b"before read failure\n"]
    assert receipts["raw"]["output_io_error"] is True
    assert receipts["raw"]["output_written_bytes"] == len(injections[0])
    assert receipts["cleanup"]["status"] == "complete"
    assert result is None or (result.status == "failed" and not result.successful)
    assert options.controlled.stdout_path.read_bytes() == injections[0]


@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_controlled_prelaunch_exception_keeps_original_and_readable_receipts(
    repository, monkeypatch, failure
):
    import hashlib
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality
    from ai_sdlc.core.pr_review_models import ProviderCompletionProof

    options, receipts = _controlled_options(repository, "print('must-not-start')")
    folder = options.controlled.stdout_path.parent
    fault = failure("actual prelaunch validation failure")
    started = []

    def prelaunch():
        (folder / "prelaunch-entered").write_bytes(b"before failure")
        raise fault

    def persist(name):
        def write(value):
            (folder / f"{name}.json").write_text(json.dumps(value))
            receipts[name] = value
        return write

    controlled = replace(
        options.controlled,
        on_prelaunch=prelaunch,
        on_raw_result=persist("raw"),
        on_cleanup=persist("cleanup"),
    )
    options = replace(options, controlled=controlled)
    monkeypatch.setattr(quality.subprocess, "Popen", lambda *_a, **_k: started.append(1))
    if failure is OSError:
        result = quality.run_controlled_process(options)
        assert result.status == "failed" and result.exit_code is None
    else:
        with pytest.raises(failure) as raised:
            quality.run_controlled_process(options)
        assert raised.value is fault
    assert not started and "process" not in receipts
    assert (folder / "prelaunch-entered").read_bytes() == b"before failure"
    raw = json.loads((folder / "raw.json").read_bytes())
    cleanup = json.loads((folder / "cleanup.json").read_bytes())
    assert raw["launch_status"] == "never_started" and raw["exit_code"] is None
    assert raw["launch_error"] and "actual prelaunch validation failure" in raw["launch_error"]
    assert raw["output_io_error"] is False and cleanup["status"] == "complete"
    assert controlled.stdout_path.read_bytes() == controlled.stderr_path.read_bytes() == b""
    quality.validate_controlled_receipts(
        {}, raw, cleanup, ownership_nonce=controlled.ownership_nonce
    )
    originals = {
        name: (folder / source).read_text()
        for name, source in (("raw-result.json", "raw.json"), ("cleanup.json", "cleanup.json"))
    }
    proof = ProviderCompletionProof(
        ownership_nonce=controlled.ownership_nonce,
        originals=originals,
        sha256={name: hashlib.sha256(content.encode()).hexdigest() for name, content in originals.items()},
    )
    assert proof.require_complete() == raw


@pytest.mark.parametrize("exit_code", [0, 1])
def test_controlled_prelaunch_check_preserves_actual_business_result(repository, exit_code):
    from dataclasses import replace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(
        repository, f"print('actual business result'); raise SystemExit({exit_code})", timeout=5
    )
    entered = []
    options = replace(
        options, controlled=replace(options.controlled, on_prelaunch=lambda: entered.append(1))
    )
    result = run_quality_command(options)
    assert entered == [1] and result.exit_code == exit_code
    assert result.successful == (exit_code == 0)
    assert receipts["raw"]["launch_status"] == "started"
    assert receipts["raw"]["launch_error"] == ""
    assert options.controlled.stdout_path.read_text() == "actual business result\n"
    quality.validate_controlled_receipts(
        receipts["process"], receipts["raw"], receipts["cleanup"],
        ownership_nonce=options.controlled.ownership_nonce,
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows 归属失败接口本地模拟；真实 launcher")
def test_fix7_unassigned_child_kill_error_does_not_skip_wait_or_receipts(repository, monkeypatch):
    from types import SimpleNamespace

    from ai_sdlc.core import quality_command as quality

    options, receipts = _controlled_options(repository, "print('must-not-start')")
    failure = ValueError("first-job-assignment")
    calls, processes = [], []
    original_popen = subprocess.Popen

    class Job:
        name, active_processes = "local-unassigned-job", 0
        def __init__(self, nonce):
            pass
        def open(self):
            pass
        def assign(self, process):
            raise failure
        def terminate_and_verify(self):
            calls.append("empty-job-termination")
            return True
        def close(self):
            calls.append("job-close")

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        original_kill = process.kill
        def kill():
            if "owned-kill-interrupt" not in calls:
                calls.append("owned-kill-interrupt")
                raise KeyboardInterrupt("secondary-unassigned-kill")
            return original_kill()
        monkeypatch.setattr(process, "kill", kill)
        return process

    monkeypatch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(quality, "_WindowsOwnedJob", Job)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    with pytest.raises(ValueError) as caught:
        quality.run_controlled_process(options)
    assert caught.value is failure and any("secondary-unassigned-kill" in note for note in failure.__notes__)
    assert calls == ["owned-kill-interrupt", "empty-job-termination", "job-close"]
    assert processes and all(process.poll() is not None for process in processes)
    assert all(pipe.closed for process in processes for pipe in (process.stdin, process.stdout, process.stderr))
    assert {"raw", "cleanup"} <= receipts.keys() and "process" not in receipts
    assert receipts["cleanup"]["status"] == "incomplete"
    assert options.controlled.stdout_path.read_bytes() == b""
    # 未完成 Job 归属，缺少 on_started 原件仍严格拒绝，不能由后续清理补造。
    with pytest.raises(ValueError):
        quality.validate_controlled_receipts({}, receipts["raw"], receipts["cleanup"], ownership_nonce=options.controlled.ownership_nonce)




def _fix7_options_with_originals(tmp_path, code):
    project = tmp_path / "project"
    project.mkdir()
    options, receipts = _controlled_options(project, code)
    folder = options.controlled.stdout_path.parent

    def persist(key, name):
        def save(payload):
            receipts[key] = payload
            (folder / name).write_text(json.dumps(payload, sort_keys=True) + "\n")
        return save

    options = replace(options, controlled=replace(
        options.controlled,
        on_started=persist("process", "process.json"),
        on_raw_result=persist("raw", "raw-result.json"),
        on_cleanup=persist("cleanup", "cleanup.json"),
    ))
    return options, receipts, folder


def _fix7_actual_provider_read(folder, nonce):
    originals = {name: (folder / name).read_text() for name in
                 ("process.json", "raw-result.json", "cleanup.json") if (folder / name).is_file()}
    proof = ProviderCompletionProof(
        ownership_nonce=nonce,
        originals=originals,
        sha256={name: hashlib.sha256(content.encode()).hexdigest() for name, content in originals.items()},
    )
    try:
        raw = proof.require_complete()
        return {"accepted_complete": True, "raw": raw}
    except BaseException as exc:
        return {"accepted_complete": False, "error": f"{type(exc).__name__}: {exc}"}


def _fix7_write_observation(folder, name, value):
    (folder / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("closed", [True, False])
def test_fix7_job_setup_failure_and_failed_close_cannot_claim_cleanup(tmp_path, monkeypatch, closed):
    """使用真实构造与落盘，仅 kernel 接口模拟；不称 Windows 实机证据。"""
    options, receipts, folder = _fix7_options_with_originals(tmp_path, "print('must-not-start')")
    kernel = SimpleNamespace(
        **{name: Mock(return_value=0) for name in (
            "AssignProcessToJobObject", "TerminateJobObject", "QueryInformationJobObject",
            "SetInformationJobObject", "CloseHandle",
        )},
        CreateJobObjectW=Mock(return_value=123),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", Mock(), raising=False)
    monkeypatch.setattr(quality, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    kernel.CloseHandle.return_value = closed
    original_open = quality._WindowsOwnedJob.open
    first_errors = []

    def open_job(job):
        try:
            original_open(job)
        except BaseException as exc:
            first_errors.append(exc)
            raise

    monkeypatch.setattr(quality._WindowsOwnedJob, "open", open_job)
    caught = result = None
    try:
        result = quality.run_controlled_process(options)
    except BaseException as exc:
        caught = exc
    observed = {
        "platform": "Darwin; simulated Windows kernel interface only",
        "result_status": result.status if result else None,
        "first_exception_preserved": caught is first_errors[0],
        "first_notes": getattr(first_errors[0], "__notes__", []),
        "process_started": "process" in receipts,
        "close_attempts": kernel.CloseHandle.call_count,
        "close_confirmed": closed,
        "raw": receipts["raw"], "cleanup": receipts["cleanup"],
        "provider_read": _fix7_actual_provider_read(folder, options.controlled.ownership_nonce),
        "stdout_bytes": options.controlled.stdout_path.read_bytes().hex(),
    }
    _fix7_write_observation(folder, "product-observation.json", observed)
    assert not observed["process_started"] and observed["close_attempts"] == 1
    assert receipts["cleanup"]["status"] == ("complete" if closed else "incomplete"), observed
    assert "limits-unavailable" in receipts["raw"]["launch_error"]
    assert observed["provider_read"]["accepted_complete"] is closed
    if closed:
        assert caught is None and result.status == "failed"
    else:
        assert caught is first_errors[0]
        assert any("job-close-unavailable" in note for note in observed["first_notes"])


@pytest.mark.parametrize("native_started", [True, False])
def test_fix7_started_reader_interruption_cannot_publish_complete_raw(tmp_path, monkeypatch, native_started):
    """原线程已启动但 start 尚未返回；先记录产品结果，再释放测试自有阻断。"""
    options, receipts, folder = _fix7_options_with_originals(tmp_path, "print('must-not-start')")
    first = KeyboardInterrupt("first-reader-start-interrupted-after-native-start")
    held, release = threading.Event(), threading.Event()
    actual_thread = threading.Thread
    readers, processes = [], []
    original_popen = quality.subprocess.Popen
    streams = _adapt_owned_output(monkeypatch, options)

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if kwargs.get("stdin") == subprocess.PIPE:
            processes.append(process)
        return process

    monkeypatch.setattr(quality.subprocess, "Popen", popen)

    def thread(*args, **kwargs):
        target, target_args = kwargs["target"], kwargs["args"]

        def held_capture():
            held.set()
            release.wait(10)
            target(*target_args)

        reader = actual_thread(target=held_capture, daemon=True)
        real_start = reader.start

        def start():
            if native_started:
                real_start()
                if not held.wait(1):
                    raise RuntimeError("test-reader-did-not-start")
            raise first

        monkeypatch.setattr(reader, "start", start)
        readers.append(reader)
        return reader

    monkeypatch.setattr(quality.threading, "Thread", thread)
    caught = None
    try:
        try:
            quality.run_controlled_process(options)
        except BaseException as exc:
            caught = exc
        observed = {
            "first_exception_preserved": caught is first,
            "caught": str(caught),
            "actual_readers_alive_at_return": [reader.is_alive() for reader in readers],
            "fixture_native_started": native_started,
            "owned_processes_reaped": all(process.poll() is not None for process in processes),
            "raw_published": "raw" in receipts,
            "raw": receipts.get("raw"), "cleanup": receipts.get("cleanup"),
            "provider_read": _fix7_actual_provider_read(folder, options.controlled.ownership_nonce),
            "stdout_bytes": options.controlled.stdout_path.read_bytes().hex(),
        }
        _fix7_write_observation(folder, "product-observation-before-test-release.json", observed)
    finally:
        release.set()
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=3)
        # 产品观察已经保存；只在读线程确已停止后释放测试制造的不确定资源。
        assert all(not reader.is_alive() for reader in readers)
        for process in processes:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        for stream in streams.values():
            stream.close()
        _fix7_write_observation(folder, "test-owned-cleanup.json", {
            "test_released_hold": True,
            "readers_alive_after_test_release": [reader.is_alive() for reader in readers],
            "not_product_cleanup_evidence": True,
        })
    assert caught is first and observed["owned_processes_reaped"]
    assert observed["actual_readers_alive_at_return"] == [native_started]
    assert not observed["provider_read"]["accepted_complete"]
    assert not observed["raw_published"] and observed["cleanup"]["status"] == "incomplete", observed


def test_fix7_tolerated_primary_failure_survives_secondary_tail_failure(tmp_path, monkeypatch):
    """一处真实收尾调用报错后，后处理报错必须保留首异常与次生诊断。"""
    options, receipts, folder = _fix7_options_with_originals(tmp_path, "print('ordinary-output')")
    first = OSError("first-output-fsync-unavailable")
    secondary = ValueError("secondary-tail-read-unavailable")
    actual_fsync = os.fsync
    attempts = []

    def fsync(fd):
        attempts.append(fd)
        if len(attempts) == 1:
            raise first
        return actual_fsync(fd)

    def read_tail(*args, **kwargs):
        raise secondary

    monkeypatch.setattr(quality.os, "fsync", fsync)
    monkeypatch.setattr(quality, "_digest_and_tail", read_tail)
    caught = None
    try:
        quality.run_controlled_process(options)
    except BaseException as exc:
        caught = exc
    observed = {
        "caught_type": type(caught).__name__, "caught": str(caught),
        "first_exception_preserved": caught is first,
        "secondary_exception_replaced_first": caught is secondary,
        "first_notes": getattr(first, "__notes__", []),
        "fsync_calls": len(attempts), "raw": receipts.get("raw"),
        "cleanup": receipts.get("cleanup"),
        "provider_read": _fix7_actual_provider_read(folder, options.controlled.ownership_nonce),
        "stdout_bytes": options.controlled.stdout_path.read_bytes().hex(),
    }
    _fix7_write_observation(folder, "product-observation.json", observed)
    assert caught is first and any(str(secondary) in note for note in getattr(first, "__notes__", [])), observed

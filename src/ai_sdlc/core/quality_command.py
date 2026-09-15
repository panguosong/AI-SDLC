"""执行单个质量命令，并将结果绑定到本地源码真值。"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import secrets
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

_REPOSITORY_REDIRECTION_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_REPLACE_REF_BASE",
    }
)
_RUNTIME_PREFIXES = (
    ".ai-sdlc/loops/",
    ".ai-sdlc/reviews/",
    ".ai-sdlc/work-items/",
    ".ai-sdlc/state/",
)
_TAIL_LIMIT = 8192
_GIT_TIMEOUT_SECONDS = 30


class QualityCommandResult(BaseModel):
    """绑定命令执行前后源码状态的可执行质量结果。"""

    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    cwd: str
    exit_code: int | None = None
    started_at: str
    completed_at: str
    source_digest_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_digest_after: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stdout_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    stdout_tail: str = ""
    stderr_tail: str = ""
    status: Literal["passed", "failed", "timed_out", "source_changed"]
    timed_out: bool = False

    @field_validator("cwd", "started_at", "completed_at")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("quality command text is required")
        return normalized

    @property
    def successful(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True)
class QualityCommandOptions:
    """直接执行一次质量命令所需的输入。"""

    root: Path
    cwd: Path
    argv: tuple[str, ...]
    timeout_seconds: float = 300.0
    output_tail_bytes: int = _TAIL_LIMIT
    controlled: ControlledQualityOptions | None = None


@dataclass(frozen=True)
class ControlledQualityOptions:
    """对照模式显式选择的持久输出和归属回执，不改变普通质量命令。"""

    environment: Mapping[str, str]
    stdout_path: Path
    stderr_path: Path
    max_output_bytes: int
    ownership_nonce: str
    on_started: Callable[[dict[str, object]], None]
    on_raw_result: Callable[[dict[str, object]], None]
    on_cleanup: Callable[[dict[str, object]], None]
    deadline_ms: int | None = None
    on_prelaunch: Callable[[], None] | None = None


def run_quality_command(options: QualityCommandOptions) -> QualityCommandResult:
    """不经 shell 执行 argv，并拒绝源码发生变化时产生的结果。"""

    root = options.root.resolve(strict=True)
    cwd = options.cwd.resolve(
        strict=options.controlled is None or options.controlled.on_prelaunch is None
    )
    _require_within_root(root, cwd)
    argv = validate_quality_argv(options.argv)
    if options.timeout_seconds <= 0:
        raise ValueError("quality command timeout must be positive")
    if options.output_tail_bytes < 0 or options.output_tail_bytes > 1024 * 1024:
        raise ValueError("quality command output tail limit is invalid")

    if options.controlled is not None:
        return _run_controlled_quality_command(options, root, cwd, argv)
    environment = quality_command_environment(os.environ)
    before = build_source_digest(root, env=environment)
    started_at = _utc_now()
    exit_code: int | None = None
    timed_out = False
    launch_error = ""
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                check=False,
                timeout=options.timeout_seconds,
            )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except OSError as exc:
            launch_error = str(exc)
        stdout_digest, stdout_tail = _digest_and_tail(
            stdout_file,
            options.output_tail_bytes,
        )
        stderr_digest, stderr_tail = _digest_and_tail(
            stderr_file,
            options.output_tail_bytes,
        )
    if launch_error:
        stderr_bytes = launch_error.encode("utf-8", errors="replace")
        stderr_digest = _sha256_label(stderr_bytes)
        stderr_tail = _decode_tail(stderr_bytes, options.output_tail_bytes)

    after = build_source_digest(root, env=environment)
    status: Literal["passed", "failed", "timed_out", "source_changed"]
    if before != after:
        status = "source_changed"
    elif timed_out:
        status = "timed_out"
    elif exit_code == 0:
        status = "passed"
    else:
        status = "failed"
    return QualityCommandResult(
        argv=list(argv),
        cwd=cwd.relative_to(root).as_posix() or ".",
        exit_code=exit_code,
        started_at=started_at,
        completed_at=_utc_now(),
        source_digest_before=before,
        source_digest_after=after,
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        status=status,
        timed_out=timed_out,
    )


_CONTROLLED_LAUNCHER = """import os, signal, subprocess, sys
token = sys.argv[1]
if sys.stdin.buffer.readline().rstrip(b'\\n').decode() != token:
    raise SystemExit(125)
process = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, shell=False)
returncode = process.wait()
if os.name != 'nt' and returncode < 0:
    signum = -returncode
    # 保留实际目标的信号终止，不能把负退出转换为普通非零断言拒绝。
    if signum not in (signal.SIGKILL, signal.SIGSTOP):
        signal.signal(signum, signal.SIG_DFL)
    if hasattr(signal, 'pthread_sigmask'):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
    os.kill(os.getpid(), signum)
elif os.name == 'nt' and returncode >= 0x80000000:
    # Python 3.11 的 SystemExit 经有符号 long；按相同 DWORD 位型传递实际子进程状态。
    returncode -= 0x100000000
raise SystemExit(returncode)
"""


@dataclass(frozen=True)
class ControlledProcessResult:
    """受控进程的实际结果；源码或工作区证明由调用方各自核验。"""

    argv: list[str]
    cwd: str
    exit_code: int | None
    started_at: str
    completed_at: str
    stdout_sha256: str
    stderr_sha256: str
    stdout_tail: str
    stderr_tail: str
    status: Literal["passed", "failed", "timed_out"]
    timed_out: bool


def _run_controlled_quality_command(
    options: QualityCommandOptions, root: Path, cwd: Path, argv: tuple[str, ...]
) -> QualityCommandResult:
    assert options.controlled is not None
    environment = options.controlled.environment
    before = build_source_digest(root, env=environment)
    controlled = options.controlled

    def started(payload: dict[str, object]) -> None:
        nonlocal before
        # 准备回调可能重建目录并发布归属；业务 nonce 释放前重新绑定当前源码。
        if controlled.on_prelaunch is not None:
            before = build_source_digest(root, env=environment)
        # 已有新鲜摘要随真实启动原件交给调用者；业务 nonce 尚未释放。
        controlled.on_started({**payload, "source_digest_before": before})

    result = run_controlled_process(
        replace(options, controlled=replace(controlled, on_started=started))
    )
    after = build_source_digest(root, env=environment)
    return QualityCommandResult(
        **{
            **result.__dict__,
            "status": "source_changed" if before != after else result.status,
        },
        source_digest_before=before,
        source_digest_after=after,
    )


def _controlled_pipe_chunks(
    pipe: io.BufferedReader, stop: threading.Event
) -> Iterator[bytes]:
    # 每个读端只有本线程消费；只在已有字节或 EOF 时 read1，未知写端不能锁住收尾。
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        # Python 3.11 的 Windows 管道不支持 os.set_blocking；现有匿名管道直接查询可读字节。
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPDWORD, wintypes.LPDWORD, wintypes.LPDWORD]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(pipe.fileno())
        while not stop.is_set():
            available = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                error = ctypes.get_last_error()
                if error == 109:  # ERROR_BROKEN_PIPE：全部写端已关闭。
                    return
                raise ctypes.WinError(error)
            if available.value:
                chunk = pipe.read1(min(4096, available.value))
                if not chunk:
                    return
                yield chunk
            else:
                stop.wait(0.05)
        return
    with selectors.DefaultSelector() as selector:
        selector.register(pipe, selectors.EVENT_READ)
        while not stop.is_set():
            if selector.select(timeout=0.05):
                chunk = pipe.read1(4096)
                if not chunk:
                    return
                yield chunk


def run_controlled_process(options: QualityCommandOptions) -> ControlledProcessResult:
    """共用既有归属、输出与终止原语，不代替调用方的源码/工作区后验。"""
    root = options.root.resolve(strict=True)
    cwd = options.cwd.resolve(
        strict=options.controlled is None or options.controlled.on_prelaunch is None
    )
    _require_within_root(root, cwd)
    argv = validate_quality_argv(options.argv)
    if options.timeout_seconds <= 0:
        raise ValueError("quality command timeout must be positive")
    if not 0 <= options.output_tail_bytes <= 1024 * 1024:
        raise ValueError("quality command output tail limit is invalid")
    controlled = options.controlled
    assert controlled is not None
    environment = dict(controlled.environment)
    if (
        not controlled.ownership_nonce
        or controlled.max_output_bytes <= 0
        or _REPOSITORY_REDIRECTION_ENV.intersection(environment)
        or _PROCESS_OWNER_ENV in environment
    ):
        raise ValueError("controlled-quality-input-invalid")
    started_at = _utc_now()
    started_ms = time.time_ns() // 1_000_000
    streams = []
    process = None
    job = None
    job_assigned = False
    process_tree = None
    readers: list[threading.Thread] = []
    uncertain_readers: set[threading.Thread] = set()
    stop_readers = threading.Event()
    failures: list[BaseException] = []
    propagate_failure = False
    cleanup_damaged = False
    readers_pending = False
    exceeded = threading.Event()
    io_failed = threading.Event()
    counter_lock = threading.Lock()
    observed_bytes = 0
    written_bytes = 0
    output_digests = (hashlib.sha256(), hashlib.sha256())
    timed_out = False
    launch_error = ""
    cleanup_complete = False
    exit_code = None

    def remember_failure(exc: BaseException, *, propagate: bool = True) -> None:
        nonlocal propagate_failure
        with counter_lock:
            propagate_failure |= propagate
            if failures:
                if exc is not failures[0]:
                    failures[0].add_note(f"secondary finalization error: {type(exc).__name__}: {exc}")
            else:
                # 保留异常对象、原 traceback 与 cause；后续收尾异常只能补充诊断。
                failures.append(exc)

    def finish(
        action: Callable[[], Any], *, cleanup: bool = False, output: bool = False,
        tolerated: tuple[type[BaseException], ...] = (),
    ) -> Any:
        nonlocal cleanup_damaged
        try:
            return action()
        except BaseException as exc:
            cleanup_damaged |= cleanup
            if output:
                io_failed.set()
            remember_failure(exc, propagate=not isinstance(exc, tolerated))
            return None

    def capture_failure(exc: BaseException) -> None:
        io_failed.set()
        remember_failure(exc, propagate=not isinstance(exc, (OSError, ValueError)))

    def reader_stopped(reader: threading.Thread) -> bool:
        # start 未返回时 ident/is_alive 不足以证明终止；仅成功 join 后才解除开始窗口的不确定性。
        return reader not in uncertain_readers and not reader.is_alive()

    def join_reader(reader: threading.Thread, deadline: float) -> None:
        if reader not in uncertain_readers or reader.ident is not None:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
            if not reader.is_alive():
                uncertain_readers.discard(reader)

    def capture(pipe: io.BufferedReader, output: BinaryIO, digest: Any) -> None:
        nonlocal observed_bytes, written_bytes
        try:
            for chunk in _controlled_pipe_chunks(pipe, stop_readers):
                with counter_lock:
                    observed_bytes += len(chunk)
                    room = max(controlled.max_output_bytes - written_bytes, 0)
                    kept = chunk[:room]
                    if len(kept) != len(chunk):
                        exceeded.set()
                    if kept:
                        remaining = memoryview(kept)
                        while remaining:
                            count = output.write(remaining)
                            if type(count) is not int or not (
                                0 < count <= len(remaining)
                            ):
                                raise OSError(
                                    "controlled-quality-output-write-no-progress"
                                )
                            # 双流共用原锁与额度；后续写入或 flush 失败也保留已确认字节数。
                            written_bytes += count
                            digest.update(remaining[:count])
                            remaining = remaining[count:]
                        output.flush()
        except BaseException as exc:
            capture_failure(exc)
        finally:
            try:
                pipe.close()
            except BaseException as exc:
                capture_failure(exc)

    try:
        # 输出文件由意图同批预建；只打开既有普通文件，不能静默替换丢失的证据。
        for path in (controlled.stdout_path, controlled.stderr_path):
            if path.is_symlink() or not path.is_file() or path.stat().st_size:
                raise ValueError("controlled-quality-output-not-empty-owned-file")
            streams.append(path.open("r+b", buffering=0))
        # 调用方的持久归属发布也在同一启动边界内；失败不释放业务 nonce。
        if controlled.on_prelaunch is not None:
            controlled.on_prelaunch()
        # reset 的合法目录重建先于此处；实际启动仍严格检查当前目录。
        cwd = options.cwd.resolve(strict=True)
        _require_within_root(root, cwd)
        # 初始化也属于本次启动；失败须留下真实 never_started 原件供恢复消费。
        if os.name == "nt":
            job = _WindowsOwnedJob(controlled.ownership_nonce)
            job.open()
        if os.name != "nt":
            try:
                # 启动器尚未创建；先用含当前活进程的真实组核对同一收尾查询能力。
                if os.getpid() not in _posix_group_members(os.getpgrp()):
                    raise OSError("controlled-process-current-group-not-visible")
            except (OSError, ValueError, IndexError, struct.error, subprocess.SubprocessError) as exc:
                raise OSError(
                    f"controlled-process-group-unavailable-before-launch: {type(exc).__name__}: {exc}"
                ) from exc
            snapshot_timeout = min(0.25, options.timeout_seconds)
            if controlled.deadline_ms is not None:
                snapshot_timeout = min(
                    snapshot_timeout,
                    max(
                        0.0,
                        (controlled.deadline_ms - time.time_ns() // 1_000_000) / 1000,
                    ),
                )
            process_tree = _OwnedPosixProcesses(snapshot_timeout=snapshot_timeout)
            environment[_PROCESS_OWNER_ENV] = process_tree.cookie
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                _CONTROLLED_LAUNCHER,
                controlled.ownership_nonce,
                *argv,
            ],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
        if job is not None:
            job.assign(process)
            job_assigned = True
        if process_tree is not None:
            process_tree.register(process.pid)
        controlled.on_started(
            {
                "schema_version": 1,
                "pid": process.pid,
                "process_group": process.pid if os.name != "nt" else None,
                "job_name": job.name if job is not None else None,
                "ownership_nonce": controlled.ownership_nonce,
                "started_at_ms": started_ms,
                "launcher": "nonce-gated-child",
                "process_tracking": process_tree.started() if process_tree else None,
            }
        )
        assert process.stdout is not None and process.stderr is not None
        for pipe, output, digest in zip(
            (process.stdout, process.stderr), streams, output_digests, strict=True
        ):
            reader = threading.Thread(target=capture, args=(pipe, output, digest), daemon=True)
            readers.append(reader)
            try:
                reader.start()
            except BaseException:
                uncertain_readers.add(reader)
                raise
        assert process.stdin is not None
        remaining = (
            None
            if controlled.deadline_ms is None
            else (controlled.deadline_ms - time.time_ns() // 1_000_000) / 1000
        )
        if remaining is not None and remaining <= 0:
            timed_out = True
            raise ValueError(
                "controlled-quality-original-deadline-expired-before-business"
            )
        process.stdin.write((controlled.ownership_nonce + "\n").encode())
        process.stdin.close()
        deadline = (
            time.monotonic() + min(options.timeout_seconds, remaining)
            if remaining is not None
            else time.monotonic() + options.timeout_seconds
        )
        while _current_child_exit(process) is None:
            if exceeded.is_set() or io_failed.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)
        exit_code = _current_child_exit(process)
    except OSError as exc:
        launch_error = str(exc)
        if isinstance(exc, _ProcessTableNotStableError):
            launch_error += ": " + "; ".join(exc.reasons)
        remember_failure(exc, propagate=False)
    except BaseException as exc:
        if process is None:
            # 启动前失败仍须形成可冷读的原件；记录诊断不改变原异常传播。
            launch_error = f"{type(exc).__name__}: {exc}"
        remember_failure(exc)
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                finish(process.stdin.close, cleanup=True)
            if job is not None and not job_assigned:
                # 只终止本次尚未获准启动业务的门控父进程句柄。
                finish(process.kill, cleanup=True)
            cleanup_complete = bool(finish(
                lambda: (
                    job.terminate_and_verify()
                    if job is not None
                    else process_tree.cleanup(process)
                    if process_tree is not None
                    else cleanup_owned_process_group(process)
                ), cleanup=True,
            ))
            # 每一步独立完成：查询失败不能撤销直接 Popen 归属，也不能跳过 wait。
            if finish(process.poll, cleanup=True, tolerated=(OSError,)) is None:
                finish(process.kill, cleanup=True, tolerated=(OSError,))
            finish(lambda: process.wait(timeout=2), cleanup=True, tolerated=(OSError, subprocess.TimeoutExpired))
            if exit_code is None:
                exit_code = finish(process.poll, cleanup=True)
            # EOF 排空与取消读取各有固定窗口；未知写端仍存在时绝不停止未知业务。
            drain_deadline = time.monotonic() + 2
            for reader in readers:
                finish(lambda reader=reader: join_reader(reader, drain_deadline), cleanup=True)
            if any(not reader_stopped(reader) for reader in readers):
                cleanup_damaged = True
                stop_readers.set()
                cancel_deadline = time.monotonic() + 2
                for reader in readers:
                    finish(lambda reader=reader: join_reader(reader, cancel_deadline), cleanup=True)
            readers_stopped = all(reader_stopped(reader) for reader in readers)
            if not readers_stopped:
                readers_pending = True
                cleanup_damaged = True
                io_failed.set()
                remember_failure(RuntimeError("controlled-quality-output-readers-not-stopped"))
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is None or pipe.closed:
                    continue
                if pipe is not process.stdin and not readers_stopped:
                    continue  # 不用跨线程 close 假装读取已结束。
                finish(pipe.close, cleanup=True, output=True, tolerated=(OSError, ValueError))
        else:
            cleanup_complete = True
        if job is not None:
            finish(job.close, cleanup=True)
        for stream in streams:
            if any(not reader_stopped(reader) for reader in readers):
                continue  # 活跃写入者不能与输出封存并行，原件明确保持 incomplete。
            finish(stream.flush, output=True, tolerated=(OSError, ValueError))
            finish(lambda stream=stream: os.fsync(stream.fileno()), output=True, tolerated=(OSError, ValueError))
            finish(stream.close, output=True, tolerated=(OSError, ValueError))
            if not stream.closed:
                cleanup_damaged = True
        process_tracking = finish(process_tree.finished, cleanup=True) if process_tree else None
        cleanup_complete = cleanup_complete and not cleanup_damaged
        completed_ms = time.time_ns() // 1_000_000
        raw: dict[str, object] = {
            "schema_version": 1,
            "launch_status": "started" if process is not None else "never_started",
            "started_at_ms": started_ms,
            "ended_at_ms": completed_ms,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_truncated": exceeded.is_set(),
            "output_observed_bytes": observed_bytes,
            "output_written_bytes": written_bytes,
            "output_io_error": io_failed.is_set(),
            "launch_error": launch_error,
            "ownership_nonce": controlled.ownership_nonce,
        }
        # 只累计底层已确认写入的切片；磁盘实际字节仍由随后 tail 和共享读取核对。
        for name, digest in zip(("stdout", "stderr"), output_digests, strict=True):
            raw[name + "_sha256"] = digest.hexdigest()
        cleanup: dict[str, object] = {
            "schema_version": 1,
            "launch_status": "started" if process is not None else "never_started",
            "job_name": job.name if job is not None else None,
            "job_active_processes": job.active_processes
            if job is not None
            else None,
            "ownership_nonce": controlled.ownership_nonce,
            "status": "complete" if cleanup_complete else "incomplete",
            "checked_at_ms": time.time_ns() // 1_000_000,
            "process_tracking": process_tracking,
        }
        # raw 发布失败不能吞掉已经取得的归属/终止事实；未知异常仍原样传播。
        if not readers_pending:
            try:
                controlled.on_raw_result(raw)
            except BaseException as exc:
                cleanup["raw_result_original"] = raw
                cleanup["raw_result_persistence_error"] = f"{type(exc).__name__}: {exc}"
                remember_failure(exc)
        # 仍有真实写入者时只保留 incomplete 清理事实；不发布可变输出的 raw 封存证明。
        finish(lambda: controlled.on_cleanup(cleanup))
    # 未知线程异常不能成为主线程成功；原件发布和本方收尾完成后仍按原异常传播。
    if failures and propagate_failure:
        raise failures[0]
    try:
        with controlled.stdout_path.open("rb") as captured_output:
            stdout_digest, stdout_tail = _digest_and_tail(
                captured_output, options.output_tail_bytes
            )
        with controlled.stderr_path.open("rb") as captured_output:
            stderr_digest, stderr_tail = _digest_and_tail(
                captured_output, options.output_tail_bytes
            )
        if stdout_digest != f'sha256:{raw["stdout_sha256"]}' or stderr_digest != f'sha256:{raw["stderr_sha256"]}':
            raise ValueError("controlled-quality-output-digest-mismatch")
    except BaseException as exc:
        # 已容忍的输出故障仍是首因；后处理失败只能补诊断，不能替换异常对象。
        remember_failure(exc)
    if failures and propagate_failure:
        raise failures[0]
    status: Literal["passed", "failed", "timed_out"] = (
        "timed_out"
        if timed_out
        else "passed"
        if exit_code == 0
        and cleanup_complete
        and not exceeded.is_set()
        and not io_failed.is_set()
        and not launch_error
        else "failed"
    )
    return ControlledProcessResult(
        argv=list(argv),
        cwd=cwd.relative_to(root).as_posix() or ".",
        exit_code=exit_code,
        started_at=started_at,
        completed_at=_utc_now(),
        stdout_sha256=stdout_digest,
        stderr_sha256=stderr_digest,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        status=status,
        timed_out=timed_out,
    )


def controlled_raw_original(
    raw: object, cleanup: object, *, raw_present: bool
) -> dict[str, Any]:
    """区分独立原件缺席与发布故障保全；不把可恢复事实提升为执行通过。"""
    if not isinstance(raw, dict) or not isinstance(cleanup, dict):
        raise ValueError("controlled-process-receipt-shape-invalid")
    if "raw_result_original" not in cleanup and "raw_result_persistence_error" not in cleanup:
        return raw
    original = cleanup.get("raw_result_original")
    failure = cleanup.get("raw_result_persistence_error")
    if (
        not isinstance(original, dict)
        or not isinstance(failure, str)
        or not failure
        or raw_present and raw != original
    ):
        raise ValueError("controlled-raw-publication-original-conflict")
    return raw if raw_present else original


def validate_controlled_receipts(
    process: Mapping[str, object],
    raw: Mapping[str, object],
    cleanup: Mapping[str, object],
    *,
    ownership_nonce: str,
) -> None:
    """核验原始归属与终止事实；非零退出、超时仍可拥有完整失败回执。"""

    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("controlled-process-receipt-incomplete-or-conflicting")

    def integer(value: object, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum

    def identities(value: object) -> set[tuple[int, int, int]]:
        require(isinstance(value, list))
        result: set[tuple[int, int, int]] = set()
        for row in value:
            require(isinstance(row, (list, tuple)) and len(row) == 3)
            require(integer(row[0], 1) and integer(row[1]) and integer(row[2]))
            identity = tuple(row)
            require(identity not in result)
            result.add(identity)
        return result

    require(bool(ownership_nonce))
    for document in (raw, cleanup):
        require(isinstance(document, Mapping))
        require(
            type(document.get("schema_version")) is int
            and document.get("schema_version") == 1
        )
        require(document.get("ownership_nonce") == ownership_nonce)
        require(
            isinstance(document.get("launch_status"), str)
            and document.get("launch_status") in {"started", "never_started"}
        )
    if "raw_result_original" in cleanup or "raw_result_persistence_error" in cleanup:
        require(isinstance(cleanup.get("raw_result_original"), Mapping))
        require(cleanup["raw_result_original"] == raw)
        require(isinstance(cleanup.get("raw_result_persistence_error"), str))
        require(bool(cleanup["raw_result_persistence_error"]))
    require(raw["launch_status"] == cleanup["launch_status"])
    require(integer(raw.get("started_at_ms")))
    require(integer(raw.get("ended_at_ms")))
    require(integer(cleanup.get("checked_at_ms")))
    require(raw["started_at_ms"] <= raw["ended_at_ms"] <= cleanup["checked_at_ms"])
    require(raw.get("exit_code") is None or type(raw.get("exit_code")) is int)
    for key in ("timed_out", "output_truncated", "output_io_error"):
        require(type(raw.get(key)) is bool)
    require(isinstance(raw.get("launch_error"), str))
    require(integer(raw.get("output_observed_bytes")))
    require(integer(raw.get("output_written_bytes")))
    require(raw["output_written_bytes"] <= raw["output_observed_bytes"])
    for name in ("stdout_sha256", "stderr_sha256"):
        require(
            isinstance(raw.get(name), str)
            and re.fullmatch(r"[0-9a-f]{64}", raw[name]) is not None
        )
    require(
        isinstance(cleanup.get("status"), str)
        and cleanup.get("status") in {"complete", "incomplete"}
    )
    require({"process_tracking", "job_name", "job_active_processes"} <= cleanup.keys())
    if raw["launch_status"] == "never_started":
        # 只有执行器在 Popen 构造前失败的明确原件才证明未启动。
        require(not process and bool(raw["launch_error"]) and raw["exit_code"] is None)
        require(cleanup["status"] == "complete")
        tracking = cleanup["process_tracking"]
        if tracking is not None:
            # 归属采样可先于 Popen；明确未启动时只能留下空的进程集合。
            require(
                isinstance(tracking, dict)
                and tracking.get("method") == "birth-and-attempt-marker-v1"
            )
            for key in ("observed", "owned", "unattributed"):
                require(not identities(tracking.get(key)))
            for key in (
                "errors",
                "snapshot_retries",
                "unattributed_rechecks",
                "unattributed_details",
                "unrelated_by_original_parent",
            ):
                require(isinstance(tracking.get(key), list))
            require(not tracking["errors"])
        return
    require(isinstance(process, Mapping))
    require(
        type(process.get("schema_version")) is int
        and process.get("schema_version") == 1
    )
    require(process.get("ownership_nonce") == ownership_nonce)
    require(integer(process.get("pid"), 1))
    require(
        integer(process.get("started_at_ms"))
        and process.get("started_at_ms") == raw["started_at_ms"]
    )
    require(process.get("launcher") == "nonce-gated-child")
    require({"process_group", "job_name", "process_tracking"} <= process.keys())
    if process["job_name"] is not None:
        require(isinstance(process["job_name"], str) and bool(process["job_name"]))
        require(
            process["process_group"] is None and process["process_tracking"] is None
        )
        require(
            cleanup["process_tracking"] is None
            and cleanup["job_name"] == process["job_name"]
        )
        require(
            cleanup["job_active_processes"] is None
            or integer(cleanup["job_active_processes"])
        )
        if cleanup["status"] == "complete":
            require(
                type(cleanup["job_active_processes"]) is int
                and cleanup["job_active_processes"] == 0
            )
        return
    require(
        integer(process["process_group"], 1)
        and process["process_group"] == process["pid"]
    )
    require(cleanup["job_name"] is None and cleanup["job_active_processes"] is None)
    start, end = process["process_tracking"], cleanup["process_tracking"]
    require(isinstance(start, dict) and isinstance(end, dict))
    require(start.get("method") == end.get("method") == "birth-and-attempt-marker-v1")
    require(integer(start.get("user_id")))
    require(start.get("marker_key") == _PROCESS_OWNER_ENV)
    require(isinstance(start.get("marker_value"), str) and bool(start["marker_value"]))
    baseline = identities(start.get("baseline"))
    launched = identities(start.get("launcher"))
    require(len(launched) == 1 and next(iter(launched))[0] == process["pid"])
    require(not baseline.intersection(launched))
    require("launcher_original_identity" in start)
    original = start["launcher_original_identity"]
    require(
        original is None
        or isinstance(original, (list, tuple))
        and len(original) == 2
        and all(integer(v) for v in original)
    )
    for key in ("baseline_retries", "original_parent_read_errors"):
        require(isinstance(start.get(key), list))
    observed, owned, unknown = (
        identities(end.get(key)) for key in ("observed", "owned", "unattributed")
    )
    require(launched <= owned)
    for key in (
        "errors",
        "snapshot_retries",
        "unattributed_rechecks",
        "unattributed_details",
        "unrelated_by_original_parent",
    ):
        require(isinstance(end.get(key), list))
    if cleanup["status"] == "complete":
        require(not unknown and not end["errors"] and not observed.intersection(owned))


def _current_child_exit(process: subprocess.Popen[Any]) -> int | None:
    if os.name == "nt":
        return process.poll()
    # 清理期间保留父进程的内核身份，避免提前回收后再按可复用 PGID 发信号。
    if sys.platform == "darwin" and not hasattr(os, "waitid"):
        return _darwin_child_exit(process.pid)
    result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None:
        return None
    return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def _darwin_child_exit(pid: int) -> int | None:
    """旧 Python 未导出 waitid 时仍读取 Darwin 原生的非回收退出事实。"""
    import ctypes

    # Darwin 64 位公共 siginfo_t ABI：前六个 32 位字段、三个指针宽字段、七个保留项。
    # 与 Python 的 waitid 一样使用 WEXITED | WNOHANG | WNOWAIT；最终 wait 留在原清理之后。
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8:
        raise OSError("controlled-process-exit-platform-unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    waitid = library.waitid
    waitid.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    waitid.restype = ctypes.c_int
    info = ctypes.create_string_buffer(104)
    if waitid(1, pid, info, 0x04 | 0x01 | 0x20) != 0:
        code = ctypes.get_errno()
        if code == errno.EINTR:
            # 信号中断只允许原有界轮询再次查询，不伪造退出或提前回收。
            return None
        raise OSError(code, "controlled-process-exit-unavailable", pid)
    signo, _, kind, child_pid, _, status = struct.unpack_from("=iiiiIi", info.raw)
    if child_pid == 0:
        return None
    if child_pid != pid or signo != signal.SIGCHLD or kind not in (1, 2, 3):
        raise OSError("controlled-process-exit-identity-or-status-invalid")
    return int(status) if kind == 1 else -int(status)


_PROCESS_OWNER_ENV = "_AI_SDLC_ATTEMPT_OWNER"


@dataclass(frozen=True)
class _ProcessBirth:
    pid: int
    parent: int
    birth: tuple[int, int]
    zombie: bool

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.pid, *self.birth)


class _ProcessTableNotStableError(OSError):
    """保存本次有限采样的原因，供原收尾窗口判断是否仍可重读。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("controlled-process-table-not-stable")
        self.reasons = tuple(reasons)


class _PosixProcessTable:
    """查询完整可见进程表的出生身份；UID 变化不改变归属，不记录命令行。"""

    def __init__(self) -> None:
        self.uid = os.getuid()
        if sys.platform == "darwin":
            import ctypes

            self.ctypes = ctypes
            self.lib: Any = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self.lib.proc_listpids.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_listpids.restype = ctypes.c_int
            self.lib.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self.lib.proc_pidinfo.restype = ctypes.c_int
            self.system: Any = ctypes.CDLL(None, use_errno=True)
            self.system.sysctl.argtypes = [
                ctypes.POINTER(ctypes.c_int),
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            self.system.sysctl.restype = ctypes.c_int
            maximum = ctypes.c_int()
            length = ctypes.c_size_t(ctypes.sizeof(maximum))
            mib = (ctypes.c_int * 2)(1, 8)
            if (
                self.system.sysctl(
                    mib, 2, ctypes.byref(maximum), ctypes.byref(length), None, 0
                )
                or not 4096 <= maximum.value <= 4 * 1024 * 1024
            ):
                raise OSError("controlled-process-argument-limit-unavailable")
            self.argument_limit = maximum.value
        elif not sys.platform.startswith("linux"):
            raise OSError("controlled-process-identity-platform-unavailable")

    def _darwin_identity(self, pid: int) -> tuple[int, int, int, tuple[int, int]]:
        """同一次内核读取提供状态、父进程、进程组及微秒出生身份。"""
        buffer = self.ctypes.create_string_buffer(136)
        self.ctypes.set_errno(0)
        size = self.lib.proc_pidinfo(pid, 3, 0, buffer, 136)
        code = self.ctypes.get_errno()
        if size == 136:
            fields = struct.unpack_from("=12I", buffer.raw)
            if fields[3] != pid:
                raise OSError("controlled-process-pid-changed")
            return (
                fields[1], fields[4],
                struct.unpack_from("=I", buffer.raw, 100)[0],
                struct.unpack_from("=QQ", buffer.raw, 120),
            )
        if code == errno.ESRCH:
            raise ProcessLookupError(code, "controlled-process-exited", pid)
        if code != errno.EPERM:
            raise OSError(code, "controlled-process-identity-unavailable", pid)
        # BSDINFO 对其它 UID 可拒绝；公开 KERN_PROC_PID 提供相同精度的基本身份，
        # 不改用粗秒或 UID/PID 代替出生身份。只支持已核定的 Darwin 64 位 ABI。
        if (
            self.ctypes.sizeof(self.ctypes.c_void_p) != 8
            or self.ctypes.sizeof(self.ctypes.c_long) != 8
        ):
            raise OSError("controlled-process-identity-abi-unavailable")
        buffer = self.ctypes.create_string_buffer(648)
        length = self.ctypes.c_size_t(len(buffer))
        mib = (self.ctypes.c_int * 4)(1, 14, 1, pid)
        self.ctypes.set_errno(0)
        result = self.system.sysctl(
            mib, 4, buffer, self.ctypes.byref(length), None, 0
        )
        code = self.ctypes.get_errno()
        if result != 0:
            if code == errno.ESRCH:
                raise ProcessLookupError(code, "controlled-process-exited", pid)
            raise OSError(code, "controlled-process-basic-identity-unavailable", pid)
        if length.value == 0:
            raise ProcessLookupError(errno.ESRCH, "controlled-process-exited", pid)
        if length.value != 648:
            raise OSError("controlled-process-identity-abi-unavailable")
        observed_pid = struct.unpack_from("=i", buffer.raw, 40)[0]
        status = buffer.raw[36]
        parent, group = struct.unpack_from("=ii", buffer.raw, 560)
        seconds, micros = struct.unpack_from("=qi", buffer.raw)
        if (
            observed_pid != pid
            or parent < 0
            or group < 0
            or status not in range(1, 6)
            or seconds <= 0
            or not 0 <= micros < 1000000
        ):
            raise OSError("controlled-process-basic-identity-invalid")
        return status, parent, group, (seconds, micros)

    def info(self, pid: int) -> _ProcessBirth:
        if sys.platform == "darwin":
            status, parent, _group, birth = self._darwin_identity(pid)
            return _ProcessBirth(pid, parent, birth, status == 5)
        folder = Path("/proc") / str(pid)
        fields = (folder / "stat").read_text().rsplit(") ", 1)[1].split()
        return _ProcessBirth(
            pid, int(fields[1]), (int(fields[19]), 0), fields[0] == "Z"
        )

    def sample(self) -> dict[int, _ProcessBirth]:
        if sys.platform == "darwin":
            size = self.lib.proc_listpids(1, 0, None, 0) + 512
            if size <= 512 or size > 4 * 1024 * 1024:
                raise OSError("controlled-process-table-size-unavailable")
            array = (self.ctypes.c_int * (size // 4))()
            length = self.lib.proc_listpids(1, 0, array, size)
            if length <= 0 or length >= size or length % 4:
                raise OSError("controlled-process-table-changed")
            pids = [pid for pid in array[: length // 4] if pid > 0]
        else:
            pids = [
                int(path.name)
                for path in self._visible_linux_proc().iterdir()
                if path.name.isdecimal()
            ]
        result = {}
        for pid in pids:
            try:
                result[pid] = self.info(pid)
            except ProcessLookupError:
                continue
        return result

    def group_members(self, group_id: int) -> list[int]:
        """ps 不可用时查询完整进程组；不沿用归属追踪的当前 UID 过滤。"""
        members = []
        deadline = time.monotonic() + 2
        if sys.platform == "darwin":
            self.ctypes.set_errno(0)
            size = self.lib.proc_listpids(2, group_id, None, 0) + 512
            if self.ctypes.get_errno() or not 512 < size <= 4 * 1024 * 1024:
                raise OSError("controlled-process-group-size-unavailable")
            array = (self.ctypes.c_int * (size // 4))()
            self.ctypes.set_errno(0)
            length = self.lib.proc_listpids(2, group_id, array, size)
            if self.ctypes.get_errno() or length < 0 or length >= size or length % 4:
                raise OSError("controlled-process-group-list-unavailable")
            for pid in array[: length // 4]:
                if time.monotonic() >= deadline:
                    raise OSError("controlled-process-group-query-timed-out")
                if pid <= 0:
                    raise OSError("controlled-process-group-pid-invalid")
                try:
                    status, _parent, observed_group, _birth = self._darwin_identity(pid)
                except ProcessLookupError:
                    continue
                if observed_group == group_id and status != 5:
                    members.append(pid)
            return members
        proc = self._visible_linux_proc()
        for folder in proc.iterdir():
            if time.monotonic() >= deadline:
                raise OSError("controlled-process-group-query-timed-out")
            if not folder.name.isdecimal():
                continue
            try:
                fields = (folder / "stat").read_text().rsplit(") ", 1)[1].split()
            except FileNotFoundError:
                # 只有内核确认已退出才忽略读取期间消失的节点；权限错误仍传播。
                try:
                    os.getpgid(int(folder.name))
                except ProcessLookupError:
                    continue
                raise
            if int(fields[2]) == group_id and fields[0] != "Z":
                members.append(int(folder.name))
        return members

    @staticmethod
    def _visible_linux_proc() -> Path:
        """归属采样与原进程组回退共用完整视图判据；看不全不能证明完成。"""
        proc = Path("/proc")
        mounts = [
            line.split()
            for line in (proc / "self/mountinfo").read_text().splitlines()
            if len(line.split()) > 6 and line.split()[4] == "/proc"
        ]
        if len(mounts) != 1:
            raise OSError("controlled-process-group-proc-mount-unavailable")
        mount = mounts[0]
        separator = mount.index("-")
        if mount[separator + 1] != "proc" or any(
            option.startswith("hidepid=") and option != "hidepid=0"
            for field in (mount[5], mount[separator + 3])
            for option in field.split(",")
        ):
            raise OSError("controlled-process-group-proc-visibility-unavailable")
        # /proc 必须覆盖当前 PID 命名空间；隐藏其它 UID 的视图不能证明全组完成。
        if int((proc / "self/stat").read_text().split(" ", 1)[0]) != os.getpid():
            raise OSError("controlled-process-group-proc-namespace-mismatch")
        return proc

    def snapshot(self) -> dict[int, _ProcessBirth]:
        # 消失或新增的进程必须触发有限重读，不能在扫描中忽略后恰好漏掉新后代。
        reasons = []
        for _ in range(3):
            try:
                first = self.sample()
                if first == self.sample():
                    return first
                reasons.append("process-table-changed-between-samples")
            except (OSError, ValueError, IndexError) as exc:
                reasons.append(f"{type(exc).__name__}: {exc}"[:160])
            time.sleep(0.01)
        raise _ProcessTableNotStableError(reasons)

    def original_parent_identity(
        self, process: _ProcessBirth
    ) -> tuple[int, int] | None:
        """只补充当前出生身份的创建父身份；接口不可用时不得据此排除未知进程。"""
        if sys.platform != "darwin":
            return None
        if self.info(process.pid).key != process.key:
            raise OSError("controlled-process-birth-changed")
        buffer = self.ctypes.create_string_buffer(56)
        # proc_uniqidentifierinfo 的 UUID 后是进程及创建父的唯一编号；不读命令行。
        if self.lib.proc_pidinfo(process.pid, 17, 0, buffer, 56) != 56:
            raise OSError("controlled-process-original-parent-unavailable")
        identity = struct.unpack_from("=QQ", buffer.raw, 16)
        if (
            self.info(process.pid).key != process.key
            or not 0 < identity[1] < identity[0]
        ):
            raise OSError("controlled-process-original-parent-not-stable")
        return identity

    def marked(self, process: _ProcessBirth, cookie: str) -> bool:
        if self.info(process.pid).key != process.key:
            raise OSError("controlled-process-birth-changed")
        if sys.platform == "darwin":
            mib = (self.ctypes.c_int * 3)(1, 49, process.pid)
            # KERN_PROCARGS2 的缓冲区不可超过当前内核报告的 ARGMAX。
            buffer = self.ctypes.create_string_buffer(self.argument_limit)
            length = self.ctypes.c_size_t(len(buffer))
            if self.system.sysctl(mib, 3, buffer, self.ctypes.byref(length), None, 0):
                raise OSError("controlled-process-marker-unavailable")
            raw = buffer.raw[: length.value]
            count = struct.unpack_from("=i", raw)[0]
            if count < 0 or count > len(raw):
                raise OSError("controlled-process-arguments-invalid")
            position = raw.index(b"\0", 4) + 1
            while position < len(raw) and raw[position] == 0:
                position += 1
            for _ in range(count):
                position = raw.index(b"\0", position) + 1
            raw = raw[position:]
        else:
            with (Path("/proc") / str(process.pid) / "environ").open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise OSError("controlled-process-environment-too-large")
        expected = (_PROCESS_OWNER_ENV + "=" + cookie).encode()
        found = False
        for item in raw.split(b"\0"):
            if not item:
                break
            if item == expected:
                found = True
        if self.info(process.pid).key != process.key:
            raise OSError("controlled-process-birth-changed")
        return found


class _OwnedPosixProcesses:
    """本次出生基线、父子关系和独享标记共同限定收尾；未归属的新进程阻断完成。"""

    def __init__(self, *, snapshot_timeout: float = 0.25) -> None:
        self.table = _PosixProcessTable()
        self.baseline_retries: list[list[str]] = []
        until = time.monotonic() + max(0.0, min(0.25, snapshot_timeout))
        while True:
            try:
                # 启动器尚未创建，完整读取的身份均早于本次后代；无关整表不必静止。
                self.baseline = self.table.sample()
                break
            except (OSError, ValueError, IndexError) as exc:
                # 基线完整读取后才启动业务；重读受调用剩余时间和固定短窗口共同约束。
                self.baseline_retries.append(
                    list(exc.reasons)
                    if isinstance(exc, _ProcessTableNotStableError)
                    else [f"{type(exc).__name__}: {exc}"[:160]]
                )
                remaining = until - time.monotonic()
                if remaining <= 0:
                    reasons = [
                        reason for batch in self.baseline_retries for reason in batch
                    ]
                    raise _ProcessTableNotStableError(reasons[-24:]) from exc
                time.sleep(min(0.02, remaining))
        self.cookie = secrets.token_hex(32)
        self.owned: dict[int, _ProcessBirth] = {}
        self.last: dict[int, _ProcessBirth] = {}
        self.unresolved: list[tuple[int, int, int]] = []
        self.errors: list[str] = []
        self.snapshot_retries: list[dict[str, object]] = []
        self.unattributed_rechecks: list[list[tuple[int, int, int]]] = []
        self.launcher_original_identity: tuple[int, int] | None = None
        self.original_parent_read_errors: list[str] = []
        self.unrelated_original_parents: dict[
            tuple[int, int, int], dict[str, object]
        ] = {}
        self.unattributed_details: list[dict[str, object]] = []
        self.adopters = {1, os.getpid()}
        if sys.platform.startswith("linux"):
            parent = os.getpid()
            visited: set[int] = set()
            while parent in self.baseline:
                # 单次枚举可能混合父链时刻；遇环须拒绝启动，不能截断后扩大排除集。
                if parent in visited:
                    raise _ProcessTableNotStableError(["prelaunch-ancestor-cycle"])
                visited.add(parent)
                self.adopters.add(parent)
                parent = self.baseline[parent].parent

    def register(self, pid: int) -> None:
        process = self.table.info(pid)
        if pid in self.baseline and self.baseline[pid].key == process.key:
            raise OSError("controlled-process-launcher-not-new")
        self.owned[pid] = process
        try:
            self.launcher_original_identity = self.table.original_parent_identity(
                process
            )
        except OSError as exc:
            self.original_parent_read_errors.append(str(exc)[:160])

    @staticmethod
    def identities(values: Mapping[int, _ProcessBirth]) -> list[tuple[int, int, int]]:
        return sorted(process.key for process in values.values() if not process.zombie)

    def started(self) -> dict[str, object]:
        return {
            "method": "birth-and-attempt-marker-v1",
            "user_id": self.table.uid,
            "marker_key": _PROCESS_OWNER_ENV,
            "marker_value": self.cookie,
            "baseline": self.identities(self.baseline),
            "baseline_retries": self.baseline_retries,
            "launcher": self.identities(self.owned),
            "launcher_original_identity": self.launcher_original_identity,
            "original_parent_read_errors": self.original_parent_read_errors,
        }

    def collect(self) -> list[_ProcessBirth]:
        self.last = self.table.snapshot()
        new = {
            pid: process
            for pid, process in self.last.items()
            if not process.zombie
            and (pid not in self.baseline or self.baseline[pid].key != process.key)
        }
        unrelated: set[int] = set()
        for _ in range(len(new) + 1):
            changed = False
            for pid, process in new.items():
                parent = self.last.get(process.parent)
                if pid in self.owned or pid in unrelated:
                    continue
                if (
                    parent
                    and parent.pid in self.owned
                    and parent.key == self.owned[parent.pid].key
                ):
                    self.owned[pid] = process
                    changed = True
                elif parent and (
                    parent.pid in unrelated
                    or (
                        parent.pid not in self.adopters
                        and parent.pid in self.baseline
                        and parent.key == self.baseline[parent.pid].key
                    )
                ):
                    unrelated.add(pid)
                    changed = True
            if not changed:
                break
        self.unresolved = []
        self.unattributed_details = []
        for pid, process in new.items():
            if pid in self.owned and self.owned[pid].key == process.key:
                continue
            if pid in unrelated:
                continue
            details: dict[str, object] = {
                "identity": process.key,
                "current_parent_pid": process.parent,
            }
            try:
                if self.table.marked(process, self.cookie):
                    self.owned[pid] = process
                    continue
                details["marker"] = "not-matched"
            except (OSError, ValueError, IndexError, struct.error) as exc:
                details["marker_error"] = f"{type(exc).__name__}: {exc}"[:160]
            if self.launcher_original_identity is not None:
                try:
                    native = self.table.original_parent_identity(process)
                    details["original_identity"] = native
                    # XNU 在创建时递增唯一编号，exec 保留编号；创建父编号不会因托管而改变。
                    # 仅排除创建父早于本次启动器的进程；其余未知仍阻止完成，不发送信号。
                    if (
                        native is not None
                        and native[1] < self.launcher_original_identity[0]
                    ):
                        self.unrelated_original_parents[process.key] = details
                        continue
                except (OSError, ValueError, IndexError, struct.error) as exc:
                    details["original_parent_error"] = f"{type(exc).__name__}: {exc}"[
                        :160
                    ]
            self.unresolved.append(process.key)
            self.unattributed_details.append(details)
        return [
            process
            for pid, process in new.items()
            if pid in self.owned and self.owned[pid].key == process.key
        ]

    def cleanup(self, process: subprocess.Popen[Any]) -> bool:
        group_complete = cleanup_owned_process_group(process)
        try:
            for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 2.0)):
                until = time.monotonic() + grace
                while True:
                    try:
                        live = self.collect()
                    except _ProcessTableNotStableError as exc:
                        # 查询暂态只占用原有收尾余量；不重跑业务，也不把无法确认当完成。
                        self.snapshot_retries.append(
                            {"signal": int(sig), "reasons": list(exc.reasons)}
                        )
                        if time.monotonic() >= until:
                            break
                        time.sleep(min(0.02, max(0.0, until - time.monotonic())))
                        continue
                    if not live:
                        if not self.unresolved:
                            return group_complete
                        # 暂时未归属的观察必须重查到实际消失；不据其身份发送信号。
                        self.unattributed_rechecks.append(list(self.unresolved))
                    for member in live:
                        # 只使用本次内存中的出生身份；冷恢复不得调用此入口按旧 PID 清理。
                        try:
                            current = self.table.info(member.pid)
                        except OSError:
                            # 自然结束不算查询成功；只有内核确认 PID 已不存在才跳过。
                            try:
                                os.kill(member.pid, 0)
                            except ProcessLookupError:
                                continue
                            raise
                        if current.key != member.key:
                            raise OSError(
                                "controlled-process-birth-changed-before-stop"
                            )
                        if current.zombie:
                            continue
                        with suppress(ProcessLookupError):
                            os.kill(member.pid, sig)
                    if time.monotonic() >= until:
                        break
                    time.sleep(min(0.02, max(0.0, until - time.monotonic())))
            return False
        except (OSError, ValueError, IndexError):
            self.errors.append("controlled-process-completion-unavailable")
            return False

    def finished(self) -> dict[str, object]:
        return {
            "method": "birth-and-attempt-marker-v1",
            "observed": self.identities(self.last),
            "owned": sorted(process.key for process in self.owned.values()),
            "unattributed": self.unresolved,
            "errors": self.errors,
            "snapshot_retries": self.snapshot_retries,
            "unattributed_rechecks": self.unattributed_rechecks,
            "unattributed_details": self.unattributed_details,
            "unrelated_by_original_parent": list(
                self.unrelated_original_parents.values()
            ),
        }


def _posix_group_members(group_id: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # 已安装的 ps 也可能不支持这些选项；原生全组查询失败仍由调用方拒绝完成。
        return _PosixProcessTable().group_members(group_id)
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) >= 3
            and int(fields[1]) == group_id
            and not fields[2].startswith("Z")
        ):
            members.append(int(fields[0]))
    return members


def cleanup_owned_process_group(process: subprocess.Popen[Any]) -> bool:
    """仅用于当前调用创建的新 session；历史 PID/PGID 不具备此归属证明。"""
    if os.name == "nt":
        raise ValueError("owned-process-group-requires-posix-session")
    group_id = process.pid
    try:
        for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 2.0)):
            if not _posix_group_members(group_id):
                return True
            try:
                with suppress(ProcessLookupError):
                    os.killpg(group_id, sig)
            except PermissionError:
                # macOS 自然退出可在查询与信号之间呈现 EPERM；必须重查仍活成员，不能吞掉权限失败。
                return not _posix_group_members(group_id)
            until = time.monotonic() + grace
            while time.monotonic() < until:
                if not _posix_group_members(group_id):
                    return True
                time.sleep(0.02)
        return not _posix_group_members(group_id)
    except (OSError, ValueError, IndexError, struct.error, subprocess.SubprocessError):
        return False


class _WindowsOwnedJob:
    """以随机具名 Job 绑定 nonce；门控启动器先入作业，再允许业务子进程启动。"""

    def __init__(self, nonce: str) -> None:
        self.active_processes: int | None = None
        import ctypes
        from ctypes import wintypes

        self.ctypes: Any = ctypes
        self.kernel = self.ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name = "Local\\AI-SDLC-" + hashlib.sha256(nonce.encode()).hexdigest()
        self.handle = None

    def open(self) -> None:
        """调用方先持有本对象，再取得句柄；准备失败也由同一 finally 负责释放。"""
        ctypes = self.ctypes
        from ctypes import wintypes

        # 成功的新建调用可能保留线程旧错误；只判断本次调用产生的名称冲突。
        ctypes.set_last_error(0)
        self.handle = self.kernel.CreateJobObjectW(None, self.name)
        if not self.handle or self.ctypes.get_last_error() == 183:
            # 名称冲突只释放本次取得的句柄，不能终止已有 Job。
            raise OSError("counterexample-owned-job-unavailable-or-colliding")

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise OSError("counterexample-owned-job-limits-unavailable")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self.kernel.AssignProcessToJobObject(
            self.handle, int(cast(Any, process)._handle)
        ):
            raise OSError("counterexample-owned-job-assignment-failed")

    def terminate_and_verify(self) -> bool:
        ctypes = self.ctypes
        if not self.kernel.TerminateJobObject(self.handle, 125):
            return False
        until = time.monotonic() + 2
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION 的 ActiveProcesses 在偏移 40。
        while time.monotonic() < until:
            accounting = ctypes.create_string_buffer(48)
            if not self.kernel.QueryInformationJobObject(
                self.handle, 1, accounting, 48, None
            ):
                return False
            self.active_processes = int.from_bytes(accounting.raw[40:44], "little")
            if self.active_processes == 0:
                return True
            time.sleep(0.02)
        return False

    def close(self) -> None:
        if not self.handle:
            return
        if not self.kernel.CloseHandle(self.handle):
            raise OSError("controlled-process-job-close-unavailable")
        self.handle = None


def build_source_digest(
    root: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """返回覆盖 HEAD、index、已跟踪差异和未跟踪字节的稳定摘要。"""

    resolved = root.resolve(strict=True)
    environment = quality_command_environment(os.environ if env is None else env)
    first = _source_identity_payload(resolved, environment)
    second = _source_identity_payload(resolved, environment)
    if first != second:
        raise ValueError("source changed during quality identity capture")
    encoded = json.dumps(
        first,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_label(encoded)


def build_source_digest_at_reviewed_parent(
    root: Path,
    *,
    reviewed_parent: str,
    reviewed_tree: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """仅重算已证明父提交下的身份；调用方必须另行核验完整交付证明。"""
    if any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        for value in (reviewed_parent, reviewed_tree)
    ):
        raise ValueError("reviewed parent and tree must be full Git object ids")
    resolved = root.resolve(strict=True)
    environment = quality_command_environment(os.environ if env is None else env)
    current_head = _git_text(resolved, environment, "rev-parse", "--verify", "HEAD")
    if (
        _git_text(
            resolved,
            environment,
            "rev-parse",
            "--verify",
            reviewed_parent + "^{commit}",
        )
        != reviewed_parent
    ):
        raise ValueError("reviewed parent does not identify the original commit")
    first = _source_identity_payload(
        resolved, environment, base_commit=reviewed_parent, index_tree=reviewed_tree
    )
    second = _source_identity_payload(
        resolved, environment, base_commit=reviewed_parent, index_tree=reviewed_tree
    )
    if first != second or current_head != _git_text(
        resolved, environment, "rev-parse", "--verify", "HEAD"
    ):
        raise ValueError("source changed during reviewed-parent identity capture")
    return _sha256_label(
        json.dumps(
            first, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )


def quality_command_environment(source: Mapping[str, str]) -> dict[str, str]:
    """保留调用方环境，仅移除会重定向仓库身份的变量。"""

    environment = dict(source)
    for name in _REPOSITORY_REDIRECTION_ENV:
        environment.pop(name, None)
    return environment


def _source_identity_payload(
    root: Path,
    env: Mapping[str, str],
    *,
    base_commit: str | None = None,
    index_tree: str | None = None,
) -> dict[str, object]:
    top = Path(_git_text(root, env, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise ValueError("quality command root is not the repository top level")
    head = base_commit or _git_text(root, env, "rev-parse", "--verify", "HEAD")
    index_tree = index_tree or _git_text(root, env, "write-tree")
    filter_args = _git_filter_overrides(root, env)
    tracked_diff = _git_bytes(
        root,
        env,
        *filter_args,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        base_commit or "HEAD",
        "--",
        ".",
        *(_exclude_pathspec(prefix) for prefix in _RUNTIME_PREFIXES),
    )
    untracked = _git_bytes(
        root,
        env,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths = [
        path
        for path in untracked.decode("utf-8", errors="strict").split("\0")
        if path and not _is_runtime_path(path)
    ]
    return {
        "head": head,
        "index_tree": index_tree,
        "tracked_diff": _sha256_label(tracked_diff),
        "untracked": [_untracked_identity(root, path) for path in sorted(paths)],
    }


def _git_filter_overrides(root: Path, env: Mapping[str, str]) -> tuple[str, ...]:
    result = _git_bytes(
        root,
        env,
        "config",
        "--name-only",
        "--get-regexp",
        r"^filter\..*\.(clean|process|required)$",
        allowed_returncodes={0, 1},
    )
    drivers: set[str] = set()
    for key in result.decode("utf-8", errors="strict").splitlines():
        parts = key.strip().split(".")
        if len(parts) >= 3 and parts[0] == "filter":
            drivers.add(".".join(parts[1:-1]))
    arguments: list[str] = []
    for driver in sorted(drivers):
        arguments.extend(
            [
                "-c",
                f"filter.{driver}.clean=",
                "-c",
                f"filter.{driver}.process=",
                "-c",
                f"filter.{driver}.required=false",
            ]
        )
    return tuple(arguments)


def _git_text(root: Path, env: Mapping[str, str], *args: str) -> str:
    return _git_bytes(root, env, *args).decode("utf-8", errors="strict").strip()


def _git_bytes(
    root: Path,
    env: Mapping[str, str],
    *args: str,
    allowed_returncodes: set[int] | None = None,
) -> bytes:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        *args,
    ]
    selected_env = dict(env)
    selected_env["GIT_OPTIONAL_LOCKS"] = "0"
    selected_env.pop("GIT_EXTERNAL_DIFF", None)
    selected_env.pop("GIT_DIFF_OPTS", None)
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=selected_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"quality source Git command is unavailable: {exc}") from exc
    accepted = {0} if allowed_returncodes is None else allowed_returncodes
    if result.returncode not in accepted:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"quality source Git command failed: {message}")
    return result.stdout


def _untracked_identity(root: Path, relative: str) -> dict[str, object]:
    candidate = root / relative
    resolved_parent = candidate.parent.resolve(strict=True)
    _require_within_root(root, resolved_parent)
    before = candidate.lstat()
    mode = stat.S_IMODE(before.st_mode)
    if stat.S_ISLNK(before.st_mode):
        content = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
        kind = "symlink"
    elif stat.S_ISREG(before.st_mode):
        content = candidate.read_bytes()
        kind = "file"
    else:
        raise ValueError(f"untracked source path is not a file: {relative}")
    after = candidate.lstat()
    if (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise ValueError(f"untracked source changed during capture: {relative}")
    return {
        "path": relative,
        "kind": kind,
        "mode": mode,
        "size": len(content),
        "digest": _sha256_label(content),
    }


def validate_quality_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """只约束可执行入口；后续空字符串是合法输入，必须原样传给进程。"""

    if isinstance(argv, (str, bytes)):
        raise ValueError("quality command argv must be a sequence of arguments")
    normalized = tuple(argv)
    if not normalized or not isinstance(normalized[0], str) or not normalized[0]:
        raise ValueError("quality command executable must be a non-empty string")
    if any(not isinstance(item, str) or "\0" in item for item in normalized):
        raise ValueError("quality command arguments must be strings without NUL")
    return normalized


_validate_argv = validate_quality_argv


def _require_within_root(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("quality command cwd escapes the project") from exc


def _digest_and_tail(stream: BinaryIO, limit: int) -> tuple[str, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    tail = bytearray()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        if limit:
            tail.extend(chunk)
            if len(tail) > limit:
                del tail[:-limit]
    return f"sha256:{digest.hexdigest()}", bytes(tail).decode("utf-8", errors="replace")


def _decode_tail(content: bytes, limit: int) -> str:
    return content[-limit:].decode("utf-8", errors="replace") if limit else ""


def _sha256_label(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _exclude_pathspec(prefix: str) -> str:
    return f":(exclude){prefix}**"


def _is_runtime_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _RUNTIME_PREFIXES)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "QualityCommandOptions",
    "QualityCommandResult",
    "build_source_digest",
    "quality_command_environment",
    "run_quality_command",
    "validate_quality_argv",
]

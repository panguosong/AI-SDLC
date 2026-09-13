#!/usr/bin/env python3
"""通过真实 POSIX TTY 回放用户指南中的已发布 CLI 交互流程。"""

from __future__ import annotations

import argparse
import os
import pty
import select
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

AGENT_PROMPT = "请选择当前实际用于聊天开发的 AI 代理入口"
SHELL_PROMPT = "请选择当前项目默认使用的命令 Shell"
RUNTIME_MARKERS = (
    "OPENAI_CODEX",
    "CODEX_CLI_READY",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDECODE",
    "CURSOR_TRACE_ID",
    "CURSOR_AGENT",
    "VSCODE_IPC_HOOK_CLI",
    "TERM_PROGRAM",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()

    cli_path = args.cli.resolve()
    project_root = args.project.resolve()
    if not cli_path.is_file():
        parser.error(f"published CLI does not exist: {cli_path}")
    if not project_root.is_dir():
        parser.error(f"project directory does not exist: {project_root}")

    for marker in RUNTIME_MARKERS:
        os.environ.pop(marker, None)

    transcript = bytearray()
    selected_agent = False
    selected_shell = False
    agent_key_step = 0
    observed_agent_renders = 0

    def consume_output(master_fd: int, data: bytes) -> None:
        nonlocal agent_key_step, observed_agent_renders
        nonlocal selected_agent, selected_shell
        transcript.extend(data)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        text = transcript.decode("utf-8", errors="replace")
        agent_renders = text.count(AGENT_PROMPT)
        if not selected_agent and agent_renders > observed_agent_renders:
            observed_agent_renders = agent_renders
            time.sleep(0.2)
            if agent_key_step < 2:
                # 干净项目默认是“其他-通用”；每次菜单重绘后发送一个真实上键。
                os.write(master_fd, b"\x1b[A")
                agent_key_step += 1
            else:
                os.write(master_fd, b"\r")
                selected_agent = True
        elif selected_agent and not selected_shell and SHELL_PROMPT in text:
            # 回车接受菜单中已展示的平台推荐 zsh 或 bash。
            time.sleep(0.2)
            os.write(master_fd, b"\r")
            selected_shell = True

    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        os.chdir(project_root)
        os.execv(str(cli_path), [str(cli_path), "init", "."])

    wait_status: int | None = None
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                consume_output(master_fd, data)
                continue
            finished_pid, status = os.waitpid(child_pid, os.WNOHANG)
            if finished_pid == child_pid:
                wait_status = status
                break
        else:
            raise TimeoutError("published CLI interactive init exceeded 180 seconds")
    except BaseException:
        with suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGTERM)
        with suppress(ChildProcessError):
            os.waitpid(child_pid, 0)
        raise
    finally:
        os.close(master_fd)

    if wait_status is None:
        _, wait_status = os.waitpid(child_pid, 0)

    if not selected_agent or not selected_shell:
        print("POSIX_INTERACTIVE_SELECTION_INCOMPLETE")
        return 2
    exit_code = os.waitstatus_to_exitcode(wait_status)
    if exit_code == 0:
        print("POSIX_INTERACTIVE_SELECTION_COMPLETED")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

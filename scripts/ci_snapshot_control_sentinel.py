"""运行固定的 SnapshotControl CI 稳定性哨兵。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SENTINEL_NODE = (
    "tests/unit/test_review_kernel.py::"
    "test_merge_expert_findings_deduplicates_without_deciding_close"
)
SENTINEL_ROUNDS = 5
RUNNER_ERROR_EXIT_CODE = 1


def _report(
    *,
    attempts: list[dict[str, object]],
    exit_code: int,
    success: bool,
    runner_error: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "attempts": attempts,
        "declared_rounds": SENTINEL_ROUNDS,
        "executed_rounds": len(attempts),
        "exit_code": exit_code,
        "node": SENTINEL_NODE,
        "runner_error": runner_error,
        "success": success,
    }


def run_snapshot_control_sentinel(runner=subprocess.run) -> dict[str, object]:
    """执行固定节点，首个失败即停止并保留每次执行证据。"""
    command = [sys.executable, "-m", "pytest", "-q", "--no-cov", SENTINEL_NODE]
    attempts: list[dict[str, object]] = []

    for attempt in range(1, SENTINEL_ROUNDS + 1):
        try:
            result = runner(command)
        except Exception as error:
            return _report(
                attempts=attempts,
                exit_code=RUNNER_ERROR_EXIT_CODE,
                success=False,
                runner_error={
                    "reason": "runner_exception",
                    "type": type(error).__name__,
                },
            )
        returncode = int(result.returncode)
        attempts.append(
            {
                "attempt": attempt,
                "command": command,
                "returncode": returncode,
            }
        )
        if returncode != 0:
            return _report(attempts=attempts, exit_code=returncode, success=False)

    return _report(attempts=attempts, exit_code=0, success=True)


def main(argv: list[str] | None = None, runner=subprocess.run) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    report = run_snapshot_control_sentinel(runner)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

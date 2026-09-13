"""B1 准备入口的真实进程、只读预演和同 Loop 资源锁契约。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

import pytest

from ai_sdlc.core.loop_decision import route_contract_digest
from ai_sdlc.core.loop_decision_models import (
    ExecutionPrecondition,
    Goal,
    GoalContract,
    Obligation,
    RouteCandidate,
    RouteSelectionContract,
)
from tests.unit.test_implementation_loop import (
    _close_design_contract_for_work_item,
    _write_ready_work_item,
)

_SOURCE = Path(__file__).resolve().parents[2] / "src"
_WORK_ITEM = "specs/demo-implementation-loop"
_LOOP_ID = "impl-b1-integration"


def _env() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_SOURCE)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _cli(
    root: Path, *args: str, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ai_sdlc", *args],
        cwd=root,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _launch(root: Path, *args: str) -> subprocess.Popen[str]:
    # 仅报告真实加锁调用的抵达点；不替换锁行为或 CLI 的业务入口。
    script = (
        "import sys\n"
        "import ai_sdlc.core.loop_resource_lock as locks\n"
        "acquire = locks._acquire_implementation_file_lock\n"
        "def observed_acquire(descriptor):\n"
        " print('lock-attempt', file=sys.stderr, flush=True)\n"
        " acquire(descriptor)\n"
        "locks._acquire_implementation_file_lock = observed_acquire\n"
        "from ai_sdlc.cli.main import app\napp()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        cwd=root,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _finish(process: subprocess.Popen[str]) -> tuple[int, dict]:
    stdout, stderr = process.communicate(timeout=30)
    assert stdout.strip(), stderr
    return process.returncode, json.loads(stdout)


def _payload(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _ready_project(
    root: Path, *, extra_spec: str = "", source_bytes: bytes | None = None
) -> Path:
    work_item = _write_ready_work_item(root, extra_spec=extra_spec)
    _close_design_contract_for_work_item(root, work_item)
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.parent.mkdir(parents=True)
    if source_bytes is None:
        source.write_text("VALUE = 1\n", encoding="utf-8")
    else:
        source.write_bytes(source_bytes)
    _git(root, "init", "-q")
    # 仅隔离临时仓库的异步整理，保留完整目录零写入断言。
    _git(root, "config", "gc.auto", "0")
    _git(root, "config", "maintenance.auto", "false")
    _git(root, "config", "user.name", "B1 Integration")
    _git(root, "config", "user.email", "b1-tests@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "frozen upstream")
    return root


def test_ready_project_disables_background_git_maintenance(initialized_project_dir):
    root = _ready_project(initialized_project_dir)
    assert _git(root, "config", "--local", "--get", "gc.auto") == "0"
    assert _git(root, "config", "--local", "--get", "maintenance.auto") == "false"


def _independent_work_item(root: Path) -> str:
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCheckOptions,
        DesignContractCloseOptions,
        check_design_contract_loop,
        close_design_contract_loop,
    )
    from ai_sdlc.core.requirement_loop import (
        RequirementFreezeOptions,
        RequirementStartOptions,
        freeze_requirement_loop,
        start_requirement_loop,
    )

    # 与原 helper 一样仅合成上游；独立工作项通过各自 Requirement / Design 门禁。
    work_item = "specs/independent-implementation-loop"
    shutil.copytree(root / _WORK_ITEM, root / work_item)
    started = start_requirement_loop(
        RequirementStartOptions(
            root=root,
            idea="An independent implementation needs its own evidence.",
            acceptance=("Independent evidence can be closed.",),
            design_scope_families=("implementation",),
            work_item_id=Path(work_item).name,
            loop_id="req-independent-implementation",
        )
    )
    assert started.status == "ready"
    freeze_requirement_loop(
        RequirementFreezeOptions(
            root=root, loop_id="req-independent-implementation", yes=True
        )
    )
    checked = check_design_contract_loop(
        DesignContractCheckOptions(
            root=root,
            work_item=work_item,
            requirement_loop_id="req-independent-implementation",
            loop_id="dc-independent-implementation",
        )
    )
    assert checked.status == "ready"
    closed = close_design_contract_loop(
        DesignContractCloseOptions(
            root=root, loop_id="dc-independent-implementation", yes=True
        )
    )
    assert closed.status == "ready" and closed.closed
    return work_item


def _start_args(
    loop_id: str = _LOOP_ID,
    *,
    mode: str = "adaptive-quantified",
    work_item: str = _WORK_ITEM,
):
    args = [
        "loop",
        "implementation",
        "start",
        "--wi",
        work_item,
        "--loop-id",
        loop_id,
        "--json",
    ]
    if mode != "legacy":
        args.extend(("--decision-mode", mode))
    return args


def _start(
    root: Path,
    loop_id: str = _LOOP_ID,
    *,
    mode="adaptive-quantified",
    work_item: str = _WORK_ITEM,
):
    result = _payload(_cli(root, *_start_args(loop_id, mode=mode, work_item=work_item)))
    assert result["status"] == "ready"
    return root / ".ai-sdlc/loops/implementation" / loop_id


def _request(
    root: Path,
    destination: Path,
    *,
    route_id="A",
    safe=True,
    work_item: str = _WORK_ITEM,
) -> Path:
    contract = RouteSelectionContract(
        goal_contract=GoalContract(
            goals=(Goal(id="goal", source_ref="frozen-spec"),),
            obligations=(
                Obligation(
                    id="required",
                    goal_id="goal",
                    statement="记录实施任务证据",
                    required=True,
                    source_ref="frozen-spec",
                    oracle_kind="deterministic",
                    pass_rule="实际任务证据完整",
                    fail_rule="证据缺失",
                    unknown_rule="尚未观测",
                    weight_share=1,
                ),
            ),
        ),
        decision_point="before-implementation",
        time_scope="through-close",
        source_refs=("frozen-spec",),
    )
    candidate = RouteCandidate(
        id=route_id,
        contract_digest=route_contract_digest(contract),
        mechanism=f"路线 {route_id} 的直接实现",
        changed_scope=("src/ai_sdlc/core/implementation_loop.py",),
        evidence_refs=("frozen-spec",),
        assumptions=("冻结范围不变",),
        execution_preconditions=tuple(
            ExecutionPrecondition(
                kind=kind,
                status="PASS" if safe else "UNKNOWN",
                evidence_refs=("frozen-spec",) if safe else (),
                reason="已核对冻结范围" if safe else "尚未确认授权",
            )
            for kind in ("authorization", "safety", "mandatory_constraints")
        ),
        cost_decision_point=contract.decision_point,
    )
    source = root / work_item / "spec.md"
    destination.write_text(
        json.dumps(
            {
                "route_contract": contract.model_dump(mode="json"),
                "candidates": [candidate.model_dump(mode="json")],
                "sources": [
                    {
                        "id": "frozen-spec",
                        "path": source.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "locator": "FR-IMPL-001",
                        "claim": "当前冻结需求要求记录实施证据",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return destination


def _prepare_args(request: Path, *, loop_id=_LOOP_ID, digest="") -> list[str]:
    args = [
        "loop",
        "decision-prepare",
        "--type",
        "implementation",
        "--loop-id",
        loop_id,
        "--input",
        str(request),
        "--json",
    ]
    args.extend(("--expect-digest", digest) if digest else ("--dry-run",))
    return args


def _preview(root: Path, request: Path, *, loop_id=_LOOP_ID) -> dict:
    payload = _payload(_cli(root, *_prepare_args(request, loop_id=loop_id)))
    assert payload["status"] == "preview"
    assert len(payload["prepare_digest"]) == 64
    return payload


def _snapshot(*roots: Path) -> dict:
    result = {}
    for root in roots:
        for path in (root, *sorted(root.rglob("*"))):
            info = path.lstat()
            content = path.read_bytes() if path.is_file() else b""
            result[str(path)] = (
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                hashlib.sha256(content).hexdigest(),
            )
    return result


def _expect_signal(
    process: subprocess.Popen[str],
    stream: TextIO | None,
    expected: str,
) -> None:
    assert stream is not None
    with ThreadPoolExecutor(max_workers=1) as readers:
        pending = readers.submit(stream.readline)
        try:
            signal = pending.result(timeout=15)
        except TimeoutError:
            process.kill()
            pending.result(timeout=5)
            raise
    assert signal.strip() == expected
    assert process.poll() is None


def _hold_lock(root: Path, loop_id: str) -> subprocess.Popen[str]:
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from ai_sdlc.core.implementation_loop import _implementation_write_guard\n"
        "with _implementation_write_guard(Path(sys.argv[1]), sys.argv[2]):\n"
        " print('locked', flush=True)\n input()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), loop_id],
        cwd=root,
        env=_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    _expect_signal(process, process.stdout, "locked")
    return process


def _release_lock(process: subprocess.Popen[str]) -> None:
    stdout, stderr = process.communicate("\n", timeout=15)
    assert process.returncode == 0, stdout + stderr


def _race(
    root: Path,
    commands: list[list[str]],
    *,
    loop_ids: tuple[str, ...] = (_LOOP_ID,),
) -> list[tuple[int, dict]]:
    holders = [_hold_lock(root, loop_id) for loop_id in loop_ids]
    contenders = []
    try:
        try:
            contenders = [_launch(root, *command) for command in commands]
            for process in contenders:
                _expect_signal(process, process.stderr, "lock-attempt")
        finally:
            for holder in holders:
                _release_lock(holder)
        return [_finish(process) for process in contenders]
    finally:
        for process in contenders:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_real_cli_start_persists_b1_identity(initialized_project_dir: Path) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    for name in ("loop-run.json", "implementation-input.json"):
        document = json.loads((loop_dir / name).read_text(encoding="utf-8"))
        assert document["decision_mode"] == "adaptive-quantified"
        assert document["decision_capability"] == "implementation-b1"
    assert not (loop_dir / "decision-context.json").exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_prepare_rejects_b1_payload_for_other_stage_without_project_writes(
    initialized_project_dir: Path,
    tmp_path: Path,
    dry_run: bool,
) -> None:
    root = _ready_project(initialized_project_dir)
    request = _request(root, tmp_path / "prepare.json")
    args = _prepare_args(request, digest="" if dry_run else "0" * 64)
    args[args.index("--type") + 1] = "requirement"
    before = _snapshot(root)

    result = _cli(root, *args)

    assert result.returncode != 0
    assert json.loads(result.stdout)["blocker"] == "simulation-operation-required"
    assert _snapshot(root) == before


@pytest.mark.parametrize("linked_worktree", [False, True])
def test_prepare_dry_run_does_not_write_project_index_or_objects(
    initialized_project_dir: Path,
    tmp_path: Path,
    linked_worktree: bool,
) -> None:
    root = _ready_project(initialized_project_dir)
    if linked_worktree:
        linked = tmp_path / "linked-project"
        _git(root, "worktree", "add", "--detach", str(linked), "HEAD")
        root = linked
    loop_dir = _start(root)
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", str(source))
    request = _request(root, tmp_path / "prepare.json")
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    common = Path(_git(root, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else (root / common).resolve()
    before = _snapshot(root, git_dir, common)

    preview = _preview(root, request)

    assert preview["selection"]["selected_id"] == "A"
    assert _snapshot(root, git_dir, common) == before
    assert not (loop_dir / "decision-context.json").exists()


def test_prepare_requires_digest_then_is_frozen_and_idempotently_read_only(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    request = _request(root, tmp_path / "prepare.json")
    preview = _preview(root, request)
    args = _prepare_args(request)
    args.remove("--dry-run")
    missing = _cli(root, *args)
    assert missing.returncode != 0
    assert not (loop_dir / "decision-context.json").exists()
    prepared = _payload(
        _cli(
            root,
            *_prepare_args(
                request,
                digest=preview["prepare_digest"],
            ),
        )
    )
    assert prepared["status"] == "prepared"
    context_path = loop_dir / "decision-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["selection"]["selected_id"] == "A"
    assert context["loop_id"] == _LOOP_ID
    before = _snapshot(root)

    repeated = _payload(
        _cli(
            root,
            *_prepare_args(
                request,
                digest=preview["prepare_digest"],
            ),
        )
    )
    assert repeated["status"] == "existing"
    assert _snapshot(root) == before
    alternate = _request(root, tmp_path / "alternate.json", route_id="B")
    frozen = _cli(root, *_prepare_args(alternate, digest=preview["prepare_digest"]))
    assert frozen.returncode != 0
    assert "decision-context-frozen" in frozen.stdout + frozen.stderr
    assert _snapshot(root) == before


def test_prepare_refuses_stale_source_digest_and_preserves_no_context(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    request = _request(root, tmp_path / "prepare.json")
    preview = _preview(root, request)
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.write_text("VALUE = 3\n", encoding="utf-8")

    rejected = _cli(root, *_prepare_args(request, digest=preview["prepare_digest"]))

    assert rejected.returncode != 0
    assert "digest" in rejected.stdout + rejected.stderr
    assert not (loop_dir / "decision-context.json").exists()
    assert source.read_text(encoding="utf-8") == "VALUE = 3\n"


def test_no_safe_route_cannot_create_executable_context(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    request = _request(root, tmp_path / "prepare.json", safe=False)
    preview = _preview(root, request)
    assert preview["selection"]["selected_id"] is None

    result = _cli(root, *_prepare_args(request, digest=preview["prepare_digest"]))

    assert json.loads(result.stdout)["status"] == "no-safe-route"
    assert not (loop_dir / "decision-context.json").exists()


@pytest.mark.parametrize("command", ["record", "verify"])
def test_missing_context_blocks_progress_before_quality_command_execution(
    initialized_project_dir: Path,
    tmp_path: Path,
    command: str,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    marker = tmp_path / "quality-command-ran"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
    args = [
        "loop",
        "implementation",
        command,
        "--task-id",
        "T11",
        "--loop-id",
        _LOOP_ID,
        "--json",
    ]
    if command == "record":
        args.extend(("--status", "in_progress", "--note", "不得绕过准备"))
    else:
        args.extend(("--", sys.executable, "-c", script))
    before = _snapshot(loop_dir)

    result = _cli(root, *args)

    assert result.returncode != 0
    assert "decision-context-missing" in result.stdout + result.stderr
    assert not marker.exists()
    assert _snapshot(loop_dir) == before


def test_legacy_default_still_verifies_without_context(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root, mode="legacy")
    for name in ("loop-run.json", "implementation-input.json"):
        document = json.loads((loop_dir / name).read_text(encoding="utf-8"))
        assert "decision_mode" not in document
        assert "decision_capability" not in document
    marker = tmp_path / "legacy-command-ran"
    script = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"

    result = _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "verify",
            "--task-id",
            "T11",
            "--loop-id",
            _LOOP_ID,
            "--json",
            "--",
            sys.executable,
            "-c",
            script,
        )
    )

    assert result["status"] == "ready"
    assert marker.read_text(encoding="utf-8") == "ran"
    assert not (loop_dir / "decision-context.json").exists()


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_prepare_has_one_context_and_no_last_writer_wins(
    initialized_project_dir: Path,
    tmp_path: Path,
    same_request: bool,
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_dir = _start(root)
    requests = [_request(root, tmp_path / "first.json")]
    requests.append(
        requests[0]
        if same_request
        else _request(
            root,
            tmp_path / "second.json",
            route_id="B",
        )
    )
    previews = [_preview(root, request) for request in requests]
    results = _race(
        root,
        [
            _prepare_args(request, digest=preview["prepare_digest"])
            for request, preview in zip(requests, previews, strict=True)
        ],
    )

    winners = [payload for code, payload in results if payload["status"] == "prepared"]
    assert len(winners) == 1, results
    assert sum(code == 0 for code, _ in results) == (2 if same_request else 1)
    if same_request:
        assert sorted(payload["status"] for _, payload in results) == [
            "existing",
            "prepared",
        ]
    context = json.loads((loop_dir / "decision-context.json").read_text("utf-8"))
    assert context["selection"] == winners[0]["selection"]
    assert len(list(loop_dir.glob("decision-context*"))) == 1


def test_different_loop_prepare_completes_while_other_resource_is_locked(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    _start(root)
    other_work_item = _independent_work_item(root)
    other_id = "impl-b1-independent"
    holder = _hold_lock(root, _LOOP_ID)
    try:
        other_dir = _start(root, other_id, work_item=other_work_item)
        request = _request(root, tmp_path / "prepare.json", work_item=other_work_item)
        preview = _preview(root, request, loop_id=other_id)
        prepared = _payload(
            _cli(
                root,
                *_prepare_args(
                    request,
                    loop_id=other_id,
                    digest=preview["prepare_digest"],
                ),
            )
        )
        assert holder.poll() is None
        assert prepared["status"] == "prepared"
        assert (other_dir / "decision-context.json").is_file()
        original = json.loads(
            (other_dir.parent / _LOOP_ID / "implementation-input.json").read_bytes()
        )
        independent = json.loads((other_dir / "implementation-input.json").read_bytes())
        assert original["work_item_id"] != independent["work_item_id"]
        assert (
            original["design_contract_loop_id"]
            != independent["design_contract_loop_id"]
        )
    finally:
        _release_lock(holder)


def test_same_id_concurrent_start_cannot_replace_its_decision_identity(
    initialized_project_dir: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    results = _race(
        root,
        [
            _start_args(mode=mode)
            for mode in (
                "legacy",
                "adaptive-quantified",
            )
        ],
    )

    assert sorted(code for code, _ in results) == [0, 1], results
    loop_dir = root / ".ai-sdlc/loops/implementation" / _LOOP_ID
    run = json.loads((loop_dir / "loop-run.json").read_text("utf-8"))
    impl_input = json.loads((loop_dir / "implementation-input.json").read_text("utf-8"))
    expected_mode = "legacy" if results[0][0] == 0 else "adaptive-quantified"
    assert run.get("decision_mode", "legacy") == expected_mode
    assert impl_input.get("decision_mode", "legacy") == expected_mode


@pytest.mark.parametrize(
    "modes",
    [
        ("adaptive-quantified", "adaptive-quantified"),
        ("legacy", "adaptive-quantified"),
        ("adaptive-quantified", "legacy"),
    ],
)
def test_same_contract_different_ids_concurrent_start_has_one_lifecycle(
    initialized_project_dir: Path,
    modes: tuple[str, str],
) -> None:
    root = _ready_project(initialized_project_dir)
    loop_ids = (_LOOP_ID, "impl-b1-competing-id")
    results = _race(
        root,
        [
            _start_args(loop_id, mode=mode)
            for loop_id, mode in zip(loop_ids, modes, strict=True)
        ],
        loop_ids=loop_ids,
    )

    assert sorted(code for code, _ in results) == [0, 1], results
    for loop_id, (code, payload) in zip(loop_ids, results, strict=True):
        loop_dir = root / ".ai-sdlc/loops/implementation" / loop_id
        if code == 0:
            assert payload["status"] == "ready"
            assert (loop_dir / "implementation-input.json").is_file()
        else:
            assert payload["status"] == "blocked"
            assert "decision-lifecycle-conflict" in json.dumps(payload)
            assert not loop_dir.exists()


def _historical_duplicate(
    source: Path, destination: Path, *, mode: str | None = None
) -> None:
    from ai_sdlc.core.implementation_models import ImplementationInput
    from ai_sdlc.core.implementation_store import implementation_input_digest

    # 明确构造旧版已存在的重复状态；不是当前 CLI 创建，更不手写评审 outcome。
    assert not list(source.glob("review-outcome-*.json"))
    original = json.loads((source / "implementation-input.json").read_bytes())
    old_id = original["loop_id"]
    original["loop_id"] = destination.name
    new_digest = implementation_input_digest(
        ImplementationInput.model_validate(original)
    )
    old_digest = json.loads((source / "loop-run.json").read_bytes())["input_digest"]
    shutil.copytree(source, destination)
    for path in destination.iterdir():
        content = path.read_text("utf-8")
        path.write_text(
            content.replace(old_id, destination.name).replace(old_digest, new_digest),
            encoding="utf-8",
        )
    if mode is not None:
        input_path = destination / "implementation-input.json"
        value = json.loads(input_path.read_bytes())
        value["decision_mode"] = mode
        value["decision_capability"] = (
            "implementation-b1" if mode == "adaptive-quantified" else None
        )
        input_path.write_text(json.dumps(value), encoding="utf-8")
        run_path = destination / "loop-run.json"
        run = json.loads(run_path.read_bytes())
        run.update(
            decision_mode=value["decision_mode"],
            decision_capability=value["decision_capability"],
            input_digest=implementation_input_digest(
                ImplementationInput.model_validate(value)
            ),
        )
        run_path.write_text(json.dumps(run), encoding="utf-8")


@pytest.mark.parametrize("dry_run", [False, True])
def test_historical_duplicate_b1_loops_cannot_prepare_new_budgets(
    initialized_project_dir: Path,
    tmp_path: Path,
    dry_run: bool,
) -> None:
    root = _ready_project(initialized_project_dir)
    original = _start(root)
    request = _request(root, tmp_path / "prepare.json")
    preview = _preview(root, request)
    duplicate = original.parent / "impl-b1-preexisting-duplicate"
    _historical_duplicate(original, duplicate)
    before = _snapshot(original, duplicate)

    results = [
        _cli(
            root,
            *_prepare_args(
                request,
                loop_id=loop_dir.name,
                digest="" if dry_run else preview["prepare_digest"],
            ),
        )
        for loop_dir in (original, duplicate)
    ]

    for result in results:
        assert result.returncode != 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "blocked"
        assert "decision-lifecycle-conflict" in result.stdout
    assert not (original / "decision-context.json").exists()
    assert not (duplicate / "decision-context.json").exists()
    assert _snapshot(original, duplicate) == before


def _review_args(loop_id: str = _LOOP_ID) -> list[str]:
    return [
        "loop",
        "review",
        "--type",
        "implementation",
        "--loop-id",
        loop_id,
        "--json",
    ]


def _complete_task(root: Path, *, loop_id: str = _LOOP_ID) -> None:
    _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            loop_id,
            "--task-id",
            "T11",
            "--status",
            "done",
            "--verification",
            "真实 Python 命令读取当前实施源码",
            "--json",
        )
    )
    verified = _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "verify",
            "--loop-id",
            loop_id,
            "--task-id",
            "T11",
            "--json",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "assert Path('src/ai_sdlc/core/implementation_loop.py').read_text().startswith('VALUE = ')",
        )
    )
    assert verified["status"] == "ready"


def _review_ready(
    root: Path,
    tmp_path: Path,
    *,
    loop_id: str = _LOOP_ID,
    pending_snapshot: Path | None = None,
    source_bytes: bytes | None = None,
) -> tuple[Path, dict]:
    _ready_project(
        root,
        extra_spec="Security permission boundary is required.",
        source_bytes=source_bytes,
    )
    loop_dir = _start(root, loop_id)
    if pending_snapshot is not None:
        shutil.copytree(loop_dir, pending_snapshot)
    request = _request(root, tmp_path / "prepare.json")
    preview = _preview(root, request, loop_id=loop_id)
    _payload(
        _cli(
            root,
            *_prepare_args(request, loop_id=loop_id, digest=preview["prepare_digest"]),
        )
    )
    _complete_task(root, loop_id=loop_id)
    return loop_dir, _payload(_cli(root, *_review_args(loop_id)))


def _expert_envelopes(
    root: Path,
    destination: Path,
    reviewed: dict,
    *,
    status: str = "PASS",
    readiness: str = "PASS",
    failed: bool = False,
) -> list[Path]:
    destination = root / ".ai-sdlc/state/b1-test-experts" / destination.name
    destination.mkdir(parents=True, exist_ok=True)
    loop_dir = root / ".ai-sdlc/loops/implementation" / reviewed["loop_id"]
    context = json.loads((loop_dir / "decision-context.json").read_text("utf-8"))
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    result_paths = []
    for index, role in enumerate(reviewed["expert_roles"]):
        execution = {
            "status": "failed" if failed else "completed",
            "roles": [role],
            "role_reasons": {role: reviewed["expert_reasons"][role]},
            "findings": [],
        }
        if failed:
            execution.update(failure_kind="timeout", failure_reason="独立专家调用超时")
        assessment = (
            None
            if failed
            else {
                "input_digest": reviewed["input_digest"],
                "context_digest": context["context_digest"],
                "selected_route_id": context["selection"]["selected_id"],
                "results": [
                    {
                        "id": "required",
                        "status": status,
                        "evidence_refs": []
                        if status == "UNKNOWN"
                        else ["actual-source"],
                        "reason": "独立审查当前实际实现并逐项核对冻结判据",
                    }
                ],
                "evidence": [
                    {
                        "id": "actual-source",
                        "path": source.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "locator": "VALUE",
                        "claim": "当前源码是此次实施任务实际产物",
                    }
                ],
                "repair_readiness": {
                    "authorization": readiness,
                    "facts": "PASS",
                    "verification": "PASS",
                    "evidence_refs": ["actual-source"],
                    "reason": "仅在冻结范围内修复并使用现有验证命令",
                },
            }
        )
        path = destination / f"expert-{index}.json"
        path.write_text(
            json.dumps({"execution": execution, "assessment": assessment}), "utf-8"
        )
        result_paths.append(path)
    return result_paths


def _review_record_args(reviewed: dict, results: list[Path]) -> list[str]:
    args = [
        "loop",
        "review-record",
        "--type",
        "implementation",
        "--loop-id",
        reviewed["loop_id"],
        "--expect-digest",
        reviewed["input_digest"],
        "--json",
    ]
    for result in results:
        args.extend(("--result", str(result)))
    return args


def _close_args(reviewed: dict) -> list[str]:
    return [
        "loop",
        "implementation",
        "close",
        "--loop-id",
        reviewed["loop_id"],
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
    ]


def _outcome(loop_dir: Path, number: int) -> dict:
    return json.loads(
        (loop_dir / f"review-outcome-round-{number}.json").read_text("utf-8")
    )


@pytest.mark.parametrize(
    "source_bytes", [b"VALUE = 1\n", b"VALUE = 1\r\n"], ids=["lf", "crlf"]
)
def test_b1_review_exports_consumable_same_snapshot_protocol(
    initialized_project_dir: Path, tmp_path: Path, source_bytes: bytes
) -> None:
    from ai_sdlc.core.loop_review_models import B1ExpertResult

    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path, source_bytes=source_bytes)
    assert reviewed["expert_result_schema"] == B1ExpertResult.model_json_schema()
    context = reviewed["decision_context"]
    assert context == json.loads((loop_dir / "decision-context.json").read_bytes())
    assert reviewed["context_digest"] == context["context_digest"]
    assert reviewed["selected_route_id"] == context["selection"]["selected_id"]
    manifest = reviewed["evidence_manifest"]
    assert set(manifest) == {
        *reviewed["artifact_paths"],
        *reviewed["upstream_context_paths"],
    }
    assert not any(
        "loop-run.json" in path or "review-outcome" in path for path in manifest
    )
    for path, digest in manifest.items():
        assert digest == hashlib.sha256((root / path).read_bytes()).hexdigest()

    source_path = "src/ai_sdlc/core/implementation_loop.py"
    command = reviewed["snapshot_read_command"]
    assert command[0] == "ai-sdlc"
    args = [
        source_path if value == "<manifest-path>" else value for value in command[1:]
    ]
    snapshot = _payload(_cli(root, *args))
    assert snapshot["input_digest"] == reviewed["input_digest"]
    assert snapshot["context_digest"] == reviewed["context_digest"]
    assert snapshot["selected_route_id"] == reviewed["selected_route_id"]
    # 快照与摘要绑定原始字节；文本读取的换行归一化不能改变期望内容。
    assert (root / source_path).read_bytes() == source_bytes
    assert snapshot["review_snapshot"] == {
        "path": source_path,
        "encoding": "utf-8",
        "content": source_bytes.decode("utf-8"),
        "sha256": manifest[source_path],
    }
    assert (
        not {"expert_result_schema", "decision_context", "evidence_manifest"}
        & snapshot.keys()
    )
    assert len(json.dumps(snapshot)) < len(json.dumps(reviewed))
    unreviewed = root / ".ai-sdlc/state/not-review-evidence.txt"
    unreviewed.parent.mkdir(parents=True)
    unreviewed.write_text("not part of the snapshot", encoding="utf-8")
    outside_args = [
        unreviewed.relative_to(root).as_posix() if value == "<manifest-path>" else value
        for value in command[1:]
    ]
    outside = _cli(root, *outside_args)
    assert outside.returncode != 0
    assert "review_snapshot" not in json.loads(outside.stdout)

    # 此处只验协议消费；合成 UNKNOWN 不冒充独立业务验收或作者 PASS。
    destination = root / ".ai-sdlc/state/protocol-experts"
    destination.mkdir(parents=True)
    results = []
    for index, role in enumerate(reviewed["expert_roles"]):
        envelope = {
            "execution": {
                "status": "completed",
                "roles": [role],
                "role_reasons": {role: reviewed["expert_reasons"][role]},
                "findings": [],
            },
            "assessment": {
                "input_digest": reviewed["input_digest"],
                "context_digest": reviewed["context_digest"],
                "selected_route_id": reviewed["selected_route_id"],
                "results": [
                    {
                        "id": obligation["id"],
                        "status": "UNKNOWN",
                        "reason": "协议夹具未执行业务评价",
                    }
                    for obligation in context["route_contract"]["goal_contract"][
                        "obligations"
                    ]
                ],
                "evidence": [],
                "repair_readiness": {"reason": "协议夹具不取得额外授权"},
            },
        }
        path = destination / f"expert-{index}.json"
        path.write_text(json.dumps(envelope), encoding="utf-8")
        results.append(path)
    original = results[0].read_bytes()
    invalid = json.loads(original)
    invalid["assessment"]["author_score"] = 100
    results[0].write_text(json.dumps(invalid), encoding="utf-8")
    rejected = _cli(root, *_review_record_args(reviewed, results))
    assert rejected.returncode != 0
    assert not (loop_dir / "review-outcome-round-1.json").exists()
    results[0].write_bytes(original)
    accepted = _payload(_cli(root, *_review_record_args(reviewed, results)))
    assert accepted["status"] == "needs_user"
    assert _outcome(loop_dir, 1)["b1"]["evaluation"]["h"] == 1
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    (root / source_path).write_text("VALUE = 2\n", encoding="utf-8")
    drifted = _cli(root, *args)
    assert drifted.returncode != 0
    assert json.loads(drifted.stdout)["reason"] == "review-input-drift"
    assert "review_snapshot" not in json.loads(drifted.stdout)


def test_legacy_review_does_not_export_b1_protocol(
    initialized_project_dir: Path,
) -> None:
    root = _ready_project(initialized_project_dir)
    _start(root, mode="legacy")
    _complete_task(root)
    reviewed = _payload(_cli(root, *_review_args()))
    assert (
        not {
            "decision_capability",
            "context_digest",
            "selected_route_id",
            "expert_result_schema",
            "decision_context",
            "evidence_manifest",
            "snapshot_read_command",
            "expert_result_requirements",
        }
        & reviewed.keys()
    )


def test_b1_required_gap_without_findings_repairs_once_then_closes(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, first = _review_ready(root, tmp_path)
    context_bytes = (loop_dir / "decision-context.json").read_bytes()
    envelopes = _expert_envelopes(root, tmp_path / "r1", first, status="UNKNOWN")
    recorded = _payload(_cli(root, *_review_record_args(first, envelopes)))
    assert recorded["status"] == "needs_fix"
    initial_outcome = _outcome(loop_dir, 1)
    assert initial_outcome["findings"] == []
    assert initial_outcome["b1"]["evaluation"]["h"] == 1
    assert initial_outcome["b1"]["decision"]["action"] == "repair"
    unchanged = _payload(_cli(root, *_review_args()))
    assert unchanged["round_number"] == 1
    assert unchanged["review_status"] == "needs_fix"
    assert _cli(root, *_close_args(first)).returncode != 0
    first_bytes = (loop_dir / "review-outcome-round-1.json").read_bytes()

    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", "utf-8"
    )
    _complete_task(root)
    second = _payload(_cli(root, *_review_args()))
    assert second["round_number"] == 2
    assert second["review_status"] == "review_missing"
    assert second["input_digest"] != first["input_digest"]
    results = _expert_envelopes(root, tmp_path / "r2", second)
    accepted = _payload(_cli(root, *_review_record_args(second, results)))
    assert accepted["status"] == "passed"
    assert _outcome(loop_dir, 2)["b1"]["evaluation"]["h"] == 0
    assert _payload(_cli(root, *_close_args(second)))["closed"] is True
    assert (loop_dir / "decision-context.json").read_bytes() == context_bytes
    assert (loop_dir / "review-outcome-round-1.json").read_bytes() == first_bytes
    assert not (loop_dir / "review-outcome-round-3.json").exists()


def test_b1_unknown_repair_readiness_blocks_without_fabricating_findings(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(
        root, tmp_path / "experts", reviewed, status="UNKNOWN", readiness="UNKNOWN"
    )
    recorded = _payload(_cli(root, *_review_record_args(reviewed, results)))
    assert recorded["status"] == "needs_user"
    outcome = _outcome(loop_dir, 1)
    assert outcome["findings"] == []
    assert outcome["b1"]["decision"]["action"] == "blocked"
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", "utf-8"
    )
    result = _cli(root, *_review_args())
    payload = json.loads(result.stdout)
    assert payload.get("round_number", 1) != 2
    assert not (loop_dir / "review-outcome-round-2.json").exists()


def test_b1_passing_r1_drift_never_opens_optional_second_round(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    assert (
        _payload(_cli(root, *_review_record_args(reviewed, results)))["status"]
        == "passed"
    )
    original = (loop_dir / "review-outcome-round-1.json").read_bytes()
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", "utf-8"
    )
    prepared = _cli(root, *_review_args())
    payload = json.loads(prepared.stdout)
    assert prepared.returncode != 0 or payload["review_status"] == "needs_user"
    assert payload.get("round_number", 1) != 2
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    assert (loop_dir / "review-outcome-round-1.json").read_bytes() == original
    assert not (loop_dir / "review-outcome-round-2.json").exists()


def test_b1_rejects_legacy_expert_result_without_actual_assessment(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    for path in results:
        envelope = json.loads(path.read_text("utf-8"))
        path.write_text(json.dumps(envelope["execution"]), "utf-8")
    rejected = _cli(root, *_review_record_args(reviewed, results))
    assert rejected.returncode != 0
    assert not (loop_dir / "review-outcome-round-1.json").exists()


def test_b1_second_round_still_unqualified_cannot_retry_or_close(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, first = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "r1", first, status="FAIL")
    assert (
        _payload(_cli(root, *_review_record_args(first, results)))["status"]
        == "needs_fix"
    )
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", "utf-8"
    )
    _complete_task(root)
    second = _payload(_cli(root, *_review_args()))
    assert second["round_number"] == 2
    results = _expert_envelopes(root, tmp_path / "r2", second, status="UNKNOWN")
    assert (
        _payload(_cli(root, *_review_record_args(second, results)))["status"]
        == "needs_user"
    )
    second_path = loop_dir / "review-outcome-round-2.json"
    second_bytes = second_path.read_bytes()
    assert _outcome(loop_dir, 2)["b1"]["decision"]["reason"] == "review-round-limit"
    assert _cli(root, *_close_args(second)).returncode != 0
    first_bytes = (loop_dir / "review-outcome-round-1.json").read_bytes()
    replacements = [
        (mode, _cli(root, *_start_args(f"impl-renamed-{mode}", mode=mode)))
        for mode in ("adaptive-quantified", "legacy")
    ]
    assert (loop_dir / "review-outcome-round-1.json").read_bytes() == first_bytes
    assert second_path.read_bytes() == second_bytes
    for mode, replacement in replacements:
        assert replacement.returncode != 0, replacement.stdout + replacement.stderr
        assert json.loads(replacement.stdout)["status"] == "blocked"
        assert "decision-lifecycle-conflict" in replacement.stdout
        assert not (loop_dir.parent / f"impl-renamed-{mode}").exists()
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 3\n", "utf-8"
    )
    repeated = _cli(root, *_review_args())
    assert json.loads(repeated.stdout).get("round_number", 2) <= 2
    assert second_path.read_bytes() == second_bytes
    assert not (loop_dir / "review-outcome-round-3.json").exists()


@pytest.mark.parametrize("peer_mode", ["adaptive-quantified", "legacy"])
def test_historical_duplicate_blocks_existing_b1_review_record_and_close(
    initialized_project_dir: Path,
    tmp_path: Path,
    peer_mode: str,
) -> None:
    root = initialized_project_dir
    pending = tmp_path / "pending-b1-snapshot"
    loop_dir, reviewed = _review_ready(root, tmp_path, pending_snapshot=pending)
    # 专家 envelope 是合成夹具；原 R1 outcome 仍由真实 CLI 生成并保留字节原件。
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    recorded = _payload(_cli(root, *_review_record_args(reviewed, results)))
    assert recorded["status"] == "passed"
    outcome_path = loop_dir / "review-outcome-round-1.json"
    original_outcome = outcome_path.read_bytes()
    duplicate = loop_dir.parent / "impl-b1-preexisting-duplicate"
    _historical_duplicate(pending, duplicate, mode=peer_mode)
    before = _snapshot(loop_dir, duplicate)

    rejected = [
        _cli(root, *args)
        for args in (
            _review_args(),
            _review_record_args(reviewed, results),
            _close_args(reviewed),
        )
    ]

    for result in rejected:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "decision-lifecycle-conflict" in result.stdout
    assert outcome_path.read_bytes() == original_outcome
    assert not (loop_dir / "implementation-close.json").exists()
    assert _snapshot(loop_dir, duplicate) == before


@pytest.mark.parametrize(
    "command", ["record", "verify", "review", "review-record", "close"]
)
def test_historical_legacy_cannot_continue_beside_same_contract_b1(
    initialized_project_dir: Path, tmp_path: Path, command: str
) -> None:
    root = _ready_project(initialized_project_dir)
    legacy = _start(root, mode="legacy")
    pending = tmp_path / "legacy-pending"
    shutil.copytree(legacy, pending)
    _complete_task(root)
    reviewed = _payload(_cli(root, *_review_args()))
    from tests.integration.test_cli_loop_review import _write_cli_expert_results

    expert_dir = root / ".ai-sdlc/state/legacy-test-experts"
    expert_dir.mkdir(parents=True, exist_ok=True)
    results = _write_cli_expert_results(expert_dir, reviewed)
    if command == "close":
        assert (
            _payload(_cli(root, *_review_record_args(reviewed, results)))["status"]
            == "passed"
        )
    duplicate = legacy.parent / "historical-b1"
    _historical_duplicate(pending, duplicate, mode="adaptive-quantified")
    marker = tmp_path / "must-not-run"
    args = _review_args()
    if command == "review-record":
        args = _review_record_args(reviewed, results)
    elif command == "close":
        args = _close_args(reviewed)
    elif command != "review":
        args = [
            "loop",
            "implementation",
            command,
            "--loop-id",
            _LOOP_ID,
            "--task-id",
            "T11",
            "--json",
        ]
        if command == "record":
            args.extend(("--status", "in_progress"))
        else:
            args.extend(
                (
                    "--",
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                )
            )
    before = _snapshot(legacy, duplicate)
    result = _cli(root, *args)
    assert result.returncode != 0
    assert "decision-lifecycle-conflict" in result.stdout + result.stderr
    assert not marker.exists()
    assert _snapshot(legacy, duplicate) == before


@pytest.mark.parametrize(
    "entry", ["record", "verify", "review", "review-record", "close"]
)
def test_prepared_b1_rejects_plan_drift_at_every_transition(
    initialized_project_dir: Path, tmp_path: Path, entry: str
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    if entry == "close":
        assert (
            _payload(_cli(root, *_review_record_args(reviewed, results)))["status"]
            == "passed"
        )
    plan = root / _WORK_ITEM / "plan.md"
    plan.write_bytes(plan.read_bytes() + b"\nChanged frozen plan.\n")
    marker = tmp_path / "must-not-run"
    commands = {
        "review": _review_args(),
        "review-record": _review_record_args(reviewed, results),
        "close": _close_args(reviewed),
        "record": [
            "loop",
            "implementation",
            "record",
            "--loop-id",
            _LOOP_ID,
            "--task-id",
            "T11",
            "--status",
            "done",
            "--json",
        ],
        "verify": [
            "loop",
            "implementation",
            "verify",
            "--loop-id",
            _LOOP_ID,
            "--task-id",
            "T11",
            "--json",
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
    }
    before = _snapshot(loop_dir)
    rejected = _cli(root, *commands[entry])
    assert rejected.returncode != 0
    assert "upstream-changed" in rejected.stdout + rejected.stderr
    assert not marker.exists()
    assert not (loop_dir / "implementation-close.json").exists()
    assert _snapshot(loop_dir) == before


def test_b1_infrastructure_failure_has_only_one_same_digest_retry(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "failed", reviewed, failed=True)
    arguments = _review_record_args(reviewed, results)
    first = _payload(_cli(root, *arguments))
    assert first["status"] == "failed"
    assert _outcome(loop_dir, 1).get("infra_retry_count", 0) == 0
    retry = _payload(_cli(root, *_review_args()))
    assert retry["round_number"] == 1
    assert retry["input_digest"] == reviewed["input_digest"]
    second = _payload(_cli(root, *arguments))
    assert second["status"] in {"failed", "needs_user"}
    assert _outcome(loop_dir, 1)["infra_retry_count"] == 1
    frozen = (loop_dir / "review-outcome-round-1.json").read_bytes()
    assert _cli(root, *arguments).returncode != 0
    assert (loop_dir / "review-outcome-round-1.json").read_bytes() == frozen
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    assert not (loop_dir / "review-outcome-round-2.json").exists()


def test_b1_concurrent_review_records_cannot_overwrite_completed_outcome(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    passing = _expert_envelopes(root, tmp_path / "passing", reviewed)
    unknown = _expert_envelopes(root, tmp_path / "unknown", reviewed, status="UNKNOWN")
    results = _race(
        root,
        [
            _review_record_args(reviewed, passing),
            _review_record_args(reviewed, unknown),
        ],
    )
    assert sorted(code for code, _ in results) == [0, 1], results
    winning = 0 if results[0][0] == 0 else 1
    outcome = _outcome(loop_dir, 1)
    assert outcome["status"] == "completed"
    assert outcome["b1"]["evaluation"]["h"] == winning
    expected_status = "PASS" if winning == 0 else "UNKNOWN"
    assert {
        assessment["results"][0]["status"]
        for assessment in outcome["b1"]["assessments"].values()
    } == {expected_status}
    assert not (loop_dir / "review-outcome-round-2.json").exists()


def test_b1_review_record_and_close_share_one_resource_lock(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    envelopes = _expert_envelopes(root, tmp_path / "experts", reviewed)
    accepted = _payload(_cli(root, *_review_record_args(reviewed, envelopes)))
    assert accepted["status"] == "passed"
    outcome_path = loop_dir / "review-outcome-round-1.json"
    frozen = outcome_path.read_bytes()
    results = _race(
        root,
        [
            _review_record_args(reviewed, envelopes),
            _close_args(reviewed),
        ],
    )
    assert results[0][0] != 0, results
    assert results[1][0] == 0, results
    outcome = json.loads(frozen)
    assert outcome["input_digest"] == reviewed["input_digest"]
    assert outcome["b1"]["decision"]["action"] == "stop"
    assert results[1][1]["closed"] is True
    assert _payload(_cli(root, *_close_args(reviewed)))["closed"] is True
    assert outcome_path.read_bytes() == frozen


@pytest.mark.parametrize("writer", ["record", "verify"])
def test_b1_progress_or_verify_and_review_record_do_not_mix_state(
    initialized_project_dir: Path,
    tmp_path: Path,
    writer: str,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    envelopes = _expert_envelopes(root, tmp_path / "experts", reviewed)
    writer_args = [
        "loop",
        "implementation",
        writer,
        "--loop-id",
        _LOOP_ID,
        "--task-id",
        "T11",
        "--json",
    ]
    if writer == "record":
        writer_args.extend(
            (
                "--status",
                "done",
                "--note",
                "并发写入的最新实际验证说明",
                "--verification",
                "执行现有真实命令",
            )
        )
    else:
        writer_args.extend(
            ("--", sys.executable, "-c", "print('fresh actual verification')")
        )
    results = _race(root, [_review_record_args(reviewed, envelopes), writer_args])
    assert results[1][0] == 0, results
    outcome_path = loop_dir / "review-outcome-round-1.json"
    assert outcome_path.exists() == (results[0][0] == 0)
    if outcome_path.exists():
        outcome = json.loads(outcome_path.read_bytes())
        assert outcome["input_digest"] == reviewed["input_digest"]
        assert all(
            assessment["input_digest"] == reviewed["input_digest"]
            for assessment in outcome["b1"]["assessments"].values()
        )
    # 先录入的旧快照也不能在新任务材料写入后继续作为 Close 凭据。
    assert _cli(root, *_close_args(reviewed)).returncode != 0
    assert not (loop_dir / "implementation-close.json").exists()


def test_b1_review_record_of_another_loop_does_not_wait_on_unrelated_resource(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    other_id = "impl-b1-other-review"
    loop_dir, reviewed = _review_ready(root, tmp_path, loop_id=other_id)
    independent_work_item = _independent_work_item(root)
    _start(root, work_item=independent_work_item)
    _complete_task(root, loop_id=other_id)
    reviewed = _payload(_cli(root, *_review_args(other_id)))
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    holder = _hold_lock(root, _LOOP_ID)
    try:
        recorded = _payload(_cli(root, *_review_record_args(reviewed, results)))
        assert holder.poll() is None
        assert recorded["status"] == "passed"
        assert _outcome(loop_dir, 1)["loop_id"] == other_id
    finally:
        _release_lock(holder)


def test_b1_two_experts_preserve_all_64_evidence_references_through_close(
    initialized_project_dir: Path,
    tmp_path: Path,
) -> None:
    root = initialized_project_dir
    loop_dir, reviewed = _review_ready(root, tmp_path)
    assert len(reviewed["expert_roles"]) == 2
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    expected_refs = {}
    for index, path in enumerate(results):
        envelope = json.loads(path.read_text("utf-8"))
        assessment = envelope["assessment"]
        original = assessment["evidence"][0]
        evidence = [
            dict(
                original,
                id=f"expert-{index}-evidence-{number}",
            )
            for number in range(64)
        ]
        references = [item["id"] for item in evidence]
        assessment["evidence"] = evidence
        assessment["results"][0]["evidence_refs"] = references
        assessment["repair_readiness"]["evidence_refs"] = references
        path.write_text(json.dumps(envelope), "utf-8")
        expected_refs[reviewed["expert_roles"][index]] = references
    accepted = _payload(_cli(root, *_review_record_args(reviewed, results)))
    assert accepted["status"] == "passed"
    outcome = _outcome(loop_dir, 1)
    for role, expected in expected_refs.items():
        assessment = outcome["b1"]["assessments"][role]
        assert assessment["results"][0]["evidence_refs"] == expected
        assert [item["id"] for item in assessment["evidence"]] == expected
    aggregate_refs = outcome["b1"]["evaluation"]["results"][0]["evidence_refs"]
    assert len(aggregate_refs) == 2
    assert set(aggregate_refs) == {
        f"expert:{role}:obligation:required" for role in reviewed["expert_roles"]
    }
    frozen = (loop_dir / "review-outcome-round-1.json").read_bytes()
    assert _payload(_cli(root, *_review_args()))["review_status"] == "passed"
    assert _payload(_cli(root, *_close_args(reviewed)))["closed"] is True
    assert (loop_dir / "review-outcome-round-1.json").read_bytes() == frozen


@pytest.mark.parametrize("entry", ["record", "close"])
@pytest.mark.parametrize("damage", ["source", "plan"])
def test_b1_final_write_window_rechecks_current_material(
    initialized_project_dir, tmp_path, monkeypatch, entry, damage
):
    from contextlib import contextmanager

    from ai_sdlc.core import loop_review_service as service
    from tests.integration.test_cli_loop_review import app, runner

    root = initialized_project_dir
    loop, reviewed = _review_ready(root, tmp_path)
    results = _expert_envelopes(root, tmp_path / "experts", reviewed)
    monkeypatch.chdir(root)
    impl = json.loads((loop / "implementation-input.json").read_bytes())
    target = root / (
        impl["plan_path"]
        if damage == "plan"
        else "src/ai_sdlc/core/implementation_loop.py"
    )

    def drift():
        target.write_bytes(target.read_bytes() + b"\n# changed in final write window\n")

    if entry == "record":
        guard = service._outcome_write_guard

        @contextmanager
        def injected(*args):
            with guard(*args):
                drift()
                yield

        monkeypatch.setattr(service, "_outcome_write_guard", injected)
        result = runner.invoke(app, _review_record_args(reviewed, results))
        assert result.exit_code != 0
        assert not (loop / "review-outcome-round-1.json").exists()
    else:
        recorded = runner.invoke(app, _review_record_args(reviewed, results))
        assert recorded.exit_code == 0, recorded.output
        outcome = (loop / "review-outcome-round-1.json").read_bytes()
        from ai_sdlc.core import implementation_loop

        write_close = implementation_loop._write_implementation_close

        def injected(*args, **kwargs):
            drift()
            return write_close(*args, **kwargs)

        monkeypatch.setattr(
            implementation_loop, "_write_implementation_close", injected
        )
        result = runner.invoke(app, _close_args(reviewed))
        assert result.exit_code != 0
        assert (loop / "review-outcome-round-1.json").read_bytes() == outcome
        assert not (loop / "implementation-close.json").exists()

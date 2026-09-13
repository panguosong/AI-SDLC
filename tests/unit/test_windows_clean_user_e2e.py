"""Windows 普通用户 E2E 驱动的纯文本断言测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_driver_module(monkeypatch):
    fake_winpty = types.ModuleType("winpty")
    fake_winpty.PtyProcess = object
    fake_enums = types.ModuleType("winpty.enums")
    fake_enums.Backend = types.SimpleNamespace(ConPTY=0)
    monkeypatch.setitem(sys.modules, "winpty", fake_winpty)
    monkeypatch.setitem(sys.modules, "winpty.enums", fake_enums)

    scripts_path = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts_path))
    driver_path = scripts_path / "windows_clean_user_e2e.py"
    spec = importlib.util.spec_from_file_location("windows_clean_user_e2e", driver_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assert_contains_treats_console_line_wrap_as_whitespace(monkeypatch) -> None:
    driver = _load_driver_module(monkeypatch)

    driver._assert_contains(
        "recommended_theme_choice: definePreset(Aura) + #1770e6 +\n"
        "darkModeSelector=false",
        "definePreset(Aura) + #1770e6 + darkModeSelector=false",
    )


def test_record_clean_review_uses_selected_roles_and_reasons(
    monkeypatch,
    tmp_path: Path,
) -> None:
    driver = _load_driver_module(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_cli(cli_path, args, *, cwd, evidence_path):
        captured.update(
            {
                "cli_path": cli_path,
                "args": args,
                "cwd": cwd,
                "evidence_path": evidence_path,
            }
        )
        return json.dumps({"status": "passed"})

    monkeypatch.setattr(driver, "_run_cli", fake_run_cli)
    project_root = tmp_path / "project"
    evidence_root = tmp_path / "evidence"
    project_root.mkdir()
    evidence_root.mkdir()
    payload = {
        "input_digest": "a" * 64,
        "round_number": 1,
        "expert_roles": [
            "product-value-and-acceptance",
            "security-and-permissions",
        ],
        "expert_reasons": {
            "product-value-and-acceptance": "Primary requirement expert.",
            "security-and-permissions": "Permission risk is present.",
        },
    }

    driver._record_clean_review(
        "ai-sdlc.exe",
        project_root,
        evidence_root,
        loop_type="requirement",
        loop_id="req-user-guide",
        review_payload=payload,
        slug="requirement",
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[:8] == [
        "loop",
        "review-record",
        "--type",
        "requirement",
        "--loop-id",
        "req-user-guide",
        "--expect-digest",
        "a" * 64,
    ]
    result_paths = [
        project_root / Path(args[index + 1])
        for index, value in enumerate(args)
        if value == "--result"
    ]
    assert len(result_paths) == 2
    results = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    assert [result["roles"][0] for result in results] == payload["expert_roles"]
    assert {
        result["roles"][0]: result["role_reasons"][result["roles"][0]]
        for result in results
    } == payload["expert_reasons"]

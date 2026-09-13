"""PR4 architecture contracts for retained entry points and retired runtimes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.check_retired_runtime_inbound import (
    RETAINED_ENTRY_MODULES,
    RETIRED_FAMILIES,
    _build_graph,
    _scan_graph,
)

ROOT = Path(__file__).resolve().parents[2]


def test_retained_entry_graph_has_no_retired_runtime_inbound() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_retired_runtime_inbound.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stdout


def test_frontend_delivery_service_is_a_narrow_retained_module() -> None:
    service = ROOT / "src" / "ai_sdlc" / "core" / "frontend_delivery_service.py"

    assert service.is_file()
    text = service.read_text(encoding="utf-8")
    for retired in (
        "program_service",
        "host_runtime",
        "quality_platform",
        "page_ui_schema",
        "generation_constraints",
        "provider_runtime_adapter",
        "proof",
        "archive",
    ):
        assert retired not in text


def test_distributed_sources_do_not_recommend_retired_program_commands() -> None:
    for suffix in ("*.py", "*.md"):
        for path in sorted((ROOT / "src" / "ai_sdlc").rglob(suffix)):
            assert "ai-sdlc program" not in path.read_text(encoding="utf-8"), path

    browser_runtime = (
        ROOT / "src" / "ai_sdlc" / "core" / "frontend_browser_gate_runtime.py"
    ).read_text(encoding="utf-8")
    assert "uv run ai-sdlc loop frontend-evidence" not in browser_runtime


def test_scanner_models_parent_package_initialization(tmp_path: Path) -> None:
    entry = tmp_path / "entry.py"
    parent = tmp_path / "models_init.py"
    retained = tmp_path / "frontend_managed_delivery.py"
    retired = tmp_path / "frontend_quality_platform.py"
    entry.write_text(
        "from ai_sdlc.models.frontend_managed_delivery import Delivery\n",
        encoding="utf-8",
    )
    parent.write_text(
        "import ai_sdlc.models.frontend_quality_platform\n",
        encoding="utf-8",
    )
    retained.write_text("class Delivery: pass\n", encoding="utf-8")
    retired.write_text("RETIRED = True\n", encoding="utf-8")
    graph = _build_graph(
        {
            "retained.entry": entry,
            "ai_sdlc.models": parent,
            "ai_sdlc.models.frontend_managed_delivery": retained,
            "ai_sdlc.models.frontend_quality_platform": retired,
        }
    )

    violations = _scan_graph(graph, roots=("retained.entry",))

    assert len(violations) == 1
    assert violations[0]["family"] == "historical-frontend"
    assert violations[0]["chain"] == [
        "retained.entry",
        "ai_sdlc.models",
        "ai_sdlc.models.frontend_quality_platform",
    ]


def test_scanner_models_function_local_imports(tmp_path: Path) -> None:
    entry = tmp_path / "entry.py"
    retired = tmp_path / "frontend_quality_platform.py"
    entry.write_text(
        "def load_legacy():\n    import ai_sdlc.models.frontend_quality_platform\n",
        encoding="utf-8",
    )
    retired.write_text("RETIRED = True\n", encoding="utf-8")
    graph = _build_graph(
        {
            "retained.entry": entry,
            "ai_sdlc.models.frontend_quality_platform": retired,
        }
    )

    violations = _scan_graph(graph, roots=("retained.entry",))

    assert len(violations) == 1
    assert violations[0]["chain"] == [
        "retained.entry",
        "ai_sdlc.models.frontend_quality_platform",
    ]


def test_scanner_preserves_missing_retired_function_local_targets(
    tmp_path: Path,
) -> None:
    core_package = tmp_path / "core_init.py"
    core_package.write_text("", encoding="utf-8")
    sources = (
        "def load_legacy():\n    import ai_sdlc.core.program_service\n",
        "def load_legacy():\n    from ai_sdlc.core import program_service\n",
    )

    for index, source in enumerate(sources):
        entry = tmp_path / f"entry_{index}.py"
        entry.write_text(source, encoding="utf-8")
        graph = _build_graph(
            {
                "retained.entry": entry,
                "ai_sdlc.core": core_package,
            }
        )

        violations = _scan_graph(graph, roots=("retained.entry",))

        assert len(violations) == 1
        assert violations[0]["family"] == "program"
        assert violations[0]["chain"] == [
            "retained.entry",
            "ai_sdlc.core.program_service",
        ]


def test_retained_roots_do_not_load_planned_deletions_in_fresh_process() -> None:
    prefixes = sorted(
        {
            prefix
            for family_prefixes in RETIRED_FAMILIES.values()
            for prefix in family_prefixes
        }
    )
    program = (
        "import importlib,json,runpy,sys;"
        f"roots={[name for name in RETAINED_ENTRY_MODULES if name != 'packaging.offline.verify_offline_bundle']!r};"
        f"prefixes={prefixes!r};"
        "[importlib.import_module(name) for name in roots];"
        "runpy.run_path('packaging/offline/verify_offline_bundle.py',run_name='packaging_offline_verify');"
        "bad=sorted(name for name in sys.modules "
        "if any(name==prefix or name.startswith(prefix+'.') for prefix in prefixes));"
        "print(json.dumps(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_legacy_active_work_items_do_not_load_planned_deletions(
    tmp_path: Path,
) -> None:
    prefixes = sorted(
        {
            prefix
            for family_prefixes in RETIRED_FAMILIES.values()
            for prefix in family_prefixes
        }
    )
    probe = tmp_path / "legacy_verify_probe.py"
    project = tmp_path / "legacy-project"
    probe.write_text(
        "\n".join(
            (
                "import builtins,json,sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(ROOT / 'src')!r})",
                "from ai_sdlc.context.state import save_checkpoint",
                "from ai_sdlc.models.state import Checkpoint,FeatureInfo",
                f"root=Path({str(project)!r})",
                "(root/'.ai-sdlc/memory').mkdir(parents=True,exist_ok=True)",
                "(root/'.ai-sdlc/memory/constitution.md').write_text('# C\\n',encoding='utf-8')",
                f"prefixes={prefixes!r}",
                "original_import=builtins.__import__",
                "def guarded_import(name,*args,**kwargs):",
                "    if any(name==prefix or name.startswith(prefix+'.') for prefix in prefixes):",
                "        raise AssertionError('retired import: '+name)",
                "    return original_import(name,*args,**kwargs)",
                "builtins.__import__=guarded_import",
                "from ai_sdlc.core.verify_constraints import ConstraintProfile,build_constraint_report,build_verification_gate_context",
                "for work_item_id in ('148','149','150','151','153'):",
                "    spec_rel=f'specs/{work_item_id}-legacy-active'",
                "    (root/spec_rel).mkdir(parents=True,exist_ok=True)",
                "    save_checkpoint(root,Checkpoint(current_stage='verify',feature=FeatureInfo(id=work_item_id,spec_dir=spec_rel,design_branch='d',feature_branch='f',current_branch='main')))",
                "    build_constraint_report(root)",
                "    build_constraint_report(root,profile=ConstraintProfile.SELF_DEVELOPMENT)",
                "    build_verification_gate_context(root)",
                "bad=sorted(name for name in sys.modules if any(name==prefix or name.startswith(prefix+'.') for prefix in prefixes))",
                "print(json.dumps(bad))",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

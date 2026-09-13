"""Project config load/save when YAML is absent (gitignore-friendly)."""

from __future__ import annotations

from pathlib import Path

from ai_sdlc.core import config as config_module
from ai_sdlc.core.config import load_project_config, save_project_config
from ai_sdlc.integrations.ide_adapter import ensure_ide_adaptation
from ai_sdlc.models.project import ProjectConfig
from ai_sdlc.routers.bootstrap import init_project
from ai_sdlc.utils.helpers import AI_SDLC_DIR, PROJECT_CONFIG_PATH


def test_load_project_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_project_config(tmp_path)
    assert isinstance(cfg, ProjectConfig)
    assert cfg.document_locale == "zh-CN"
    assert cfg.product_form == "hybrid"
    assert cfg.preferred_shell == ""
    assert cfg.detected_ide == ""
    assert not hasattr(cfg, "telemetry_profile")
    assert not hasattr(cfg, "telemetry_mode")


def test_save_project_config_creates_file(tmp_path: Path) -> None:
    cfg = ProjectConfig(
        detected_ide="vscode",
        adapter_applied="vscode",
        preferred_shell="powershell",
    )
    save_project_config(tmp_path, cfg)
    path = tmp_path / PROJECT_CONFIG_PATH
    assert path.is_file()
    again = load_project_config(tmp_path)
    assert again.detected_ide == "vscode"
    assert again.adapter_applied == "vscode"
    assert again.preferred_shell == "powershell"
    rendered = path.read_text(encoding="utf-8")
    assert "telemetry" not in rendered.lower()
    assert "agentops" not in rendered.lower()


def test_save_project_config_skips_atomic_replace_when_content_is_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = ProjectConfig(
        detected_ide="vscode",
        adapter_applied="vscode",
    )
    save_project_config(tmp_path, cfg)

    def _unexpected_replace(self: Path, target: Path) -> Path:
        raise AssertionError("replace should not run for a no-op save")

    monkeypatch.setattr(Path, "replace", _unexpected_replace)
    save_project_config(tmp_path, cfg)


def test_save_project_config_retries_windows_replace_permission_error(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = ProjectConfig(
        detected_ide="vscode",
        adapter_applied="vscode",
    )
    original_replace = Path.replace
    calls = {"count": 0}

    def _flaky_replace(self: Path, target: Path) -> Path:
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("[WinError 5] Access is denied")
        return original_replace(self, target)

    monkeypatch.setattr(config_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(config_module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(Path, "replace", _flaky_replace)

    save_project_config(tmp_path, cfg)

    assert calls["count"] == 3
    assert load_project_config(tmp_path).detected_ide == "vscode"


def test_ensure_ide_adaptation_writes_config_when_missing(tmp_path: Path) -> None:
    init_project(tmp_path)
    path = tmp_path / AI_SDLC_DIR / "project" / "config" / "project-config.yaml"
    path.unlink(missing_ok=True)
    assert not path.exists()

    ensure_ide_adaptation(tmp_path)
    assert path.is_file()
    cfg = load_project_config(tmp_path)
    assert cfg.adapter_applied_at != ""
    assert not hasattr(cfg, "telemetry_profile")
    assert not hasattr(cfg, "telemetry_mode")


def test_project_config_template_has_no_retired_runtime_defaults() -> None:
    template = Path("src/ai_sdlc/templates/project-config.yaml.j2").read_text(
        encoding="utf-8"
    )
    assert "telemetry" not in template.lower()
    assert "agentops" not in template.lower()

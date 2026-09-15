"""Unit tests for YamlStore."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from ai_sdlc.core import config as config_module
from ai_sdlc.core.config import YamlStore, YamlStoreError
from ai_sdlc.models.project import ProjectConfig, ProjectState, ProjectStatus


@pytest.mark.parametrize("operation", ["load", "unchanged-save", "changed-save"])
def test_windows_config_read_overlap_recovers_without_losing_content(
    tmp_path, monkeypatch, operation
):
    target = tmp_path / "config.yaml"
    original = ProjectConfig(preferred_shell="powershell")
    YamlStore.save(target, original)
    before = target.read_bytes()
    original_read = Path.read_text
    calls = 0
    delays = []

    def read(path, *args, **kwargs):
        nonlocal calls
        if path == target:
            calls += 1
            if calls <= 2:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(config_module.time, "sleep", delays.append)
    monkeypatch.setattr(Path, "read_text", read)
    if operation == "load":
        assert YamlStore.load(target, ProjectConfig) == original
    else:
        saved = original.model_copy(
            update={"preferred_shell": "bash" if operation == "changed-save" else "powershell"}
        )
        YamlStore.save(target, saved)
        assert YamlStore.load(target, ProjectConfig) == saved
    assert delays == [0.05, 0.1]
    if operation != "changed-save":
        assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("windows", [True, False])
@pytest.mark.parametrize("operation", ["load", "save"])
def test_config_read_permission_exhaustion_preserves_failure_and_original(
    tmp_path, monkeypatch, windows, operation
):
    target = tmp_path / "config.yaml"
    YamlStore.save(target, ProjectConfig(preferred_shell="powershell"))
    before = target.read_bytes()
    original_read = Path.read_text
    calls = 0
    delays = []

    def read(path, *args, **kwargs):
        nonlocal calls
        if path == target:
            calls += 1
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(config_module, "_IS_WINDOWS", windows)
    monkeypatch.setattr(config_module.time, "sleep", delays.append)
    monkeypatch.setattr(Path, "read_text", read)
    if operation == "load":
        with pytest.raises(YamlStoreError) as caught:
            YamlStore.load(target, ProjectConfig)
        assert isinstance(caught.value.__cause__, PermissionError)
    else:
        with pytest.raises(PermissionError):
            YamlStore.save(target, ProjectConfig(preferred_shell="bash"))
    assert calls == (5 if windows else 1)
    assert delays == ([0.05, 0.1, 0.2] if windows else [])
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("content", ["bad: [", "project_name: [invalid]"])
def test_windows_yaml_or_model_errors_are_never_retried(tmp_path, monkeypatch, content):
    target = tmp_path / "state.yaml"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config_module, "_IS_WINDOWS", True)

    def unexpected_delay(_delay):
        raise AssertionError("A content validation error is not a transient IO failure")

    monkeypatch.setattr(config_module.time, "sleep", unexpected_delay)
    with pytest.raises(YamlStoreError):
        YamlStore.load(target, ProjectState)
    assert target.read_text(encoding="utf-8") == content


class TestYamlStoreLoad:
    def test_load_nonexistent_returns_default_model(self, tmp_path: Path) -> None:
        state = YamlStore.load(tmp_path / "missing.yaml", ProjectState)
        assert state.status == ProjectStatus.UNINITIALIZED

    def test_load_nonexistent_returns_explicit_default(self, tmp_path: Path) -> None:
        default = ProjectState(status=ProjectStatus.INITIALIZED, project_name="x")
        result = YamlStore.load(
            tmp_path / "missing.yaml", ProjectState, default=default
        )
        assert result.project_name == "x"

    def test_load_valid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "state.yaml"
        f.write_text("status: initialized\nproject_name: demo\nnext_work_item_seq: 3\n")
        state = YamlStore.load(f, ProjectState)
        assert state.status == ProjectStatus.INITIALIZED
        assert state.project_name == "demo"
        assert state.next_work_item_seq == 3

    def test_load_empty_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.yaml"
        f.write_text("")
        state = YamlStore.load(f, ProjectState)
        assert state.status == ProjectStatus.UNINITIALIZED

    def test_load_legacy_planning_status_maps_to_initialized(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "legacy.yaml"
        f.write_text("status: planning\nproject_name: demo\n")
        state = YamlStore.load(f, ProjectState)
        assert state.status == ProjectStatus.INITIALIZED
        assert state.project_name == "demo"

    def test_load_corrupt_yaml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "corrupt.yaml"
        f.write_text(": : : [invalid yaml\n  bad: {{")
        with pytest.raises(YamlStoreError, match="Invalid YAML"):
            YamlStore.load(f, ProjectState)


class TestYamlStoreSave:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "state.yaml"
        original = ProjectState(
            status=ProjectStatus.INITIALIZED,
            project_name="roundtrip-test",
            next_work_item_seq=7,
        )
        YamlStore.save(f, original)
        loaded = YamlStore.load(f, ProjectState)
        assert loaded == original

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        f = tmp_path / "deep" / "nested" / "config.yaml"
        config = ProjectConfig(max_parallel_agents=5)
        YamlStore.save(f, config)
        assert f.exists()
        loaded = YamlStore.load(f, ProjectConfig)
        assert loaded.max_parallel_agents == 5

    def test_save_overwrites_existing(self, tmp_path: Path) -> None:
        f = tmp_path / "state.yaml"
        YamlStore.save(f, ProjectState(project_name="v1"))
        YamlStore.save(f, ProjectState(project_name="v2"))
        loaded = YamlStore.load(f, ProjectState)
        assert loaded.project_name == "v2"

    def test_save_uses_deterministic_sibling_temp_file_without_tempfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = Path("virtual") / "state.yaml"
        writes: dict[Path, str] = {}
        replacements: list[tuple[Path, Path]] = []

        def _write_text(self: Path, text: str, encoding: str) -> int:
            assert encoding == "utf-8"
            writes[self] = text
            return len(text)

        def _replace(source: Path, destination: Path) -> None:
            replacements.append((source, destination))

        assert "tempfile" not in config_module.__dict__
        monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(Path, "write_text", _write_text)
        monkeypatch.setattr(YamlStore, "_replace_with_retry", staticmethod(_replace))

        YamlStore.save(target, ProjectState(project_name="deterministic-temp"))

        assert len(writes) == 1
        temp_path = next(iter(writes))
        assert temp_path.parent == target.parent
        assert temp_path.name.startswith(".state.yaml.")
        assert temp_path.name.endswith(".tmp")
        assert replacements == [(temp_path, target)]

    def test_save_falls_back_to_direct_write_when_sibling_temp_is_denied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = Path("virtual") / "state.yaml"
        writes: list[Path] = []

        def _write_text(self: Path, text: str, encoding: str) -> int:
            assert encoding == "utf-8"
            writes.append(self)
            if self != target:
                raise PermissionError("[WinError 5] Access is denied")
            return len(text)

        def _unexpected_replace(source: Path, destination: Path) -> None:
            raise AssertionError("replace should not run after temp creation fails")

        monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr(Path, "write_text", _write_text)
        monkeypatch.setattr(
            YamlStore, "_replace_with_retry", staticmethod(_unexpected_replace)
        )

        YamlStore.save(target, ProjectState(project_name="direct-fallback"))

        assert writes[0].parent == target.parent
        assert writes[-1] == target

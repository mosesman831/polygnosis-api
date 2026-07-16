"""Config discovery + packaged default tests."""

from __future__ import annotations

from polygnosis_api.config import (
    PACKAGED_CONFIG_PATH,
    load_boardroom_config,
    resolve_config_path,
)


def test_resolve_falls_back_to_packaged(tmp_path, monkeypatch):
    # No env override and an empty CWD → packaged default_config.yaml.
    monkeypatch.delenv("POLYGNOSIS_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path() == PACKAGED_CONFIG_PATH


def test_resolve_prefers_cwd_config(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYGNOSIS_CONFIG_PATH", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("models: {}\nsettings: {}\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path() == cfg


def test_resolve_prefers_explicit_env(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.yaml"
    explicit.write_text("models: {}\nsettings: {}\n")
    monkeypatch.setenv("POLYGNOSIS_CONFIG_PATH", str(explicit))
    monkeypatch.chdir(tmp_path)
    assert resolve_config_path() == explicit


def test_load_packaged_default_from_empty_cwd(tmp_path, monkeypatch):
    # The core BUILD_SPEC guarantee: config loads with no checkout in the CWD.
    monkeypatch.delenv("POLYGNOSIS_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = load_boardroom_config()
    assert "models" in cfg
    assert "settings" in cfg


def test_packaged_default_has_scorer_model():
    cfg = load_boardroom_config(PACKAGED_CONFIG_PATH)
    assert cfg["models"]["scorer"]

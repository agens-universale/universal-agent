from pathlib import Path

import pytest
import yaml

from gc_core.auto_routing.levels import auto_available, effective_target
from gc_core.config import load_settings
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.config_modules.root import AppConfig
from gc_core.startup_notices import auto_routing_unavailable_notice

_ONE_MODEL_MESSAGE = "автовыбор моделей недоступен: во всех уровнях одна модель — работает модель слота default"
_BASE = {
    "backend": {
        "api_mode": "chat_completions",
        "completions_url": "http://127.0.0.1:8014/v1/chat/completions",
    }
}


def _write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Изолировать env_vars singleton + платформу (паттерн config/test_profile_loader.py)."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


def test_level_auto_is_accepted_by_schema():
    cfg = AppConfig.model_validate({"model": "auto"})
    assert cfg.model == "auto"


def test_level_auto_with_auto_routing_disabled_loads_and_resolves_default():
    """`level: auto` + `agent.auto_routing: false` — конфиг грузится без ошибки
    (рубильник не запрещает значение схемы), а эффективный уровень идёт через
    `auto_available` в `effective_target`: рубильник выключен → `False` → `default`."""
    cfg = AppConfig.model_validate(
        {"model": "auto", "agent": {"auto_routing": False}}
    )
    assert cfg.model == "auto"
    available = auto_available(cfg, force_legacy=False)
    assert available is False
    assert effective_target(cfg.model, None, available) == "default"


def test_level_auto_from_the_project_config_on_a_one_model_registry_names_the_reason(
    isolated_cwd, tmp_path
):
    base = _write_yaml(
        tmp_path / "base.yml",
        {**_BASE, "models": {"only": {}}, "slots": {"default": "only"}},
    )
    _write_yaml(isolated_cwd / ".gc" / "config.yml", {"model": "auto"})

    loaded = load_settings(str(base))
    cfg = loaded.settings

    assert cfg.model == "auto"
    assert auto_available(cfg, force_legacy=False) is False
    assert (
        auto_routing_unavailable_notice(loaded).message == _ONE_MODEL_MESSAGE
    )


def test_level_auto_from_a_profile_on_a_one_model_registry_names_the_reason(
    isolated_cwd, tmp_path
):
    base = _write_yaml(tmp_path / "base.yml", _BASE)
    _write_yaml(
        isolated_cwd / ".gc" / "profiles" / "solo.yml",
        {
            "model": "auto",
            "models": {"only": {}},
            "slots": {"default": "only"},
        },
    )

    loaded = load_settings(str(base), profile_override="solo")
    cfg = loaded.settings
    meta = loaded.profile_meta

    assert meta.active_name == "solo" and cfg.model == "auto"
    assert auto_available(cfg, force_legacy=False) is False
    assert (
        auto_routing_unavailable_notice(loaded).message == _ONE_MODEL_MESSAGE
    )

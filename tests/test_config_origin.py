"""Происхождение значения: базовый файл vs оверлеи vs окружение.

Тесты — на НАСТОЯЩИХ файлах и НАСТОЯЩЕМ os.environ: мок-путь уже прятал дыру
с ${ENV:VAR} (см. tests/tui/test_settings_screen.py).
"""
import os
from pathlib import Path

import pytest

from config_origin import Origin, origins


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _no_managed_overlay_by_default(tmp_path, monkeypatch):
    """Без этого тесты читают НАСТОЯЩИЙ ~/.gc/config.local.yml разработчика.

    Тестам, которым managed-оверлей нужен, ничего специально включать не надо —
    они сами переопределяют managed_config_path поверх этой заглушки.
    """
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed-overlay-here.yml",
    )


def test_project_overlay_overrides_base(tmp_path, monkeypatch):
    base = _write(tmp_path / "config.yml", "agent:\n  model: base-model\n")
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "agent:\n  model: proj-model\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.raw == "base-model"           # что в файле, который правит /settings
    assert got.effective == "proj-model"     # что реально действует
    assert got.overlay_kind == "project"
    assert got.is_overridden is True


def test_managed_overlay_overrides_base(tmp_path, monkeypatch):
    base = _write(tmp_path / "config.yml", "agent:\n  model: base-model\n")
    managed = tmp_path / "gc" / "config.local.yml"
    _write(managed, "agent:\n  model: managed-model\n")
    monkeypatch.setattr("mcp_runtime.paths.managed_config_path", lambda: managed)
    monkeypatch.chdir(tmp_path)      # нет .gc/ → проектного оверлея нет

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.raw == "base-model"
    assert got.effective == "managed-model"
    assert got.overlay_kind == "managed"


def test_project_wins_over_managed(tmp_path, monkeypatch):
    """Приоритет load_config: base → managed → project. Побеждает project."""
    base = _write(tmp_path / "config.yml", "agent:\n  model: base-model\n")
    managed = tmp_path / "gc" / "config.local.yml"
    _write(managed, "agent:\n  model: managed-model\n")
    monkeypatch.setattr("mcp_runtime.paths.managed_config_path", lambda: managed)
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "agent:\n  model: proj-model\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.effective == "proj-model"
    assert got.overlay_kind == "project"


def test_not_overridden_when_overlay_silent(tmp_path, monkeypatch):
    """Оверлей есть, но этого ключа не задаёт → перекрытия нет."""
    base = _write(tmp_path / "config.yml", "agent:\n  model: base-model\n")
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "backend:\n  use_ssl: false\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.effective == "base-model"
    assert got.overlay_kind == ""
    assert got.is_overridden is False


def test_custom_base_path_from_config_flag(tmp_path, monkeypatch):
    """--config my.yml: база — указанный файл, оверлеи всё равно ложатся сверху."""
    base = _write(tmp_path / "my.yml", "agent:\n  model: from-my-yml\n")
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "agent:\n  model: proj-model\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.raw == "from-my-yml"
    assert got.effective == "proj-model"
    assert got.overlay_kind == "project"


def test_base_is_the_overlay_itself_is_skipped(tmp_path, monkeypatch):
    """--config <тот же project-local файл> → он не перекрывает сам себя."""
    proj_dir = tmp_path / "proj"
    base = _write(proj_dir / ".gc" / "config.yml", "agent:\n  model: same-file\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.effective == "same-file"
    assert got.overlay_kind == ""


def test_overlay_explicit_null_overrides(tmp_path, monkeypatch):
    """overlay.key: null — осознанное переопределение (так же трактует deep_merge)."""
    base = _write(tmp_path / "config.yml", "agent:\n  model: base-model\n")
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "agent:\n  model: null\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.effective is None
    assert got.overlay_kind == "project"
    assert got.is_overridden is True


def test_empty_section_in_overlay_wipes_node(tmp_path, monkeypatch):
    """Секция объявлена в оверлее пустой → deep_merge затирает узел целиком: токен
    базового файла НЕ уходит на бэкенд. Считать «не перекрыто» тут — соврать экрану."""
    base = _write(
        tmp_path / "config.yml",
        'backend:\n  chat_completion_headers:\n    Authorization: "Bearer BASE-SECRET"\n')
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "backend:\n  chat_completion_headers:\n")
    monkeypatch.chdir(proj_dir)

    key = "backend.chat_completion_headers.Authorization"
    got = origins(base, [key])[key]

    assert got.raw == "Bearer BASE-SECRET"   # что лежит в базовом файле
    assert got.effective is None             # что действует после deep_merge — ничего
    assert got.overlay_kind == "project"
    assert got.is_overridden is True


def test_list_field_concat_by_overlay_is_an_override(tmp_path, monkeypatch):
    """list + list у deep_merge — concat: действующее значение НЕ равно базовому."""
    base = _write(tmp_path / "config.yml", "agent:\n  stop_tools:\n    - a\n")
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", "agent:\n  stop_tools:\n    - b\n")
    monkeypatch.chdir(proj_dir)

    got = origins(base, ["agent.stop_tools"])["agent.stop_tools"]

    assert got.effective == ["a", "b"]
    assert got.overlay_kind == "project"


def test_env_ref_stays_raw_in_base(tmp_path, monkeypatch):
    """Ссылка в базовом файле остаётся ссылкой: резолв — не дело этого модуля."""
    monkeypatch.setenv("GC_MODEL", "resolved-model")
    base = _write(tmp_path / "config.yml", 'agent:\n  model: "${ENV:GC_MODEL}"\n')
    monkeypatch.chdir(tmp_path)

    got = origins(base, ["agent.model"])["agent.model"]

    assert got.raw == "${ENV:GC_MODEL}"
    assert got.overlay_kind == ""

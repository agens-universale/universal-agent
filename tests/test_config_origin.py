"""Происхождение значения: базовый файл vs оверлеи vs окружение.

Тесты — на НАСТОЯЩИХ файлах и НАСТОЯЩЕМ os.environ: мок-путь уже прятал дыру
с ${ENV:VAR} (см. tests/tui/test_settings_screen.py).
"""
import os
from pathlib import Path

import pytest

from config_origin import Origin, _overlay_paths, origins


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


def test_overlay_paths_agrees_with_resolve_profile_pointer_on_a_conflict(
        tmp_path, monkeypatch):
    """Указатель есть в ОБОИХ слоях, и они расходятся: `_overlay_paths` и общий
    `resolve_profile_pointer` (`config_modules.layers`) обязаны сойтись на ОДНОМ
    победителе (проектном, как у загрузчика, `layers.py`: `deep_merge(raw_config,
    project_raw)` поверх уже смерженной базы) — вторая, независимая реализация
    того же правила уже расходилась с первой на этой самой раскладке (находка
    ревью), а тест на неё не заводил никто ни здесь, ни в showcase-наборе."""
    from config_modules.layers import resolve_profile_pointer

    base = _write(tmp_path / "config.yml", 'profile: "work"\nagent:\n  model: x\n')
    proj_dir = tmp_path / "proj"
    _write(proj_dir / ".gc" / "config.yml", 'profile: "corp"\n')
    monkeypatch.chdir(proj_dir)
    _write(tmp_path / "profiles" / "work.yml", "agent:\n  model: work-model\n")
    _write(tmp_path / "profiles" / "corp.yml", "agent:\n  model: corp-model\n")

    assert resolve_profile_pointer(base) == "corp"
    paths = [path for _kind, path in _overlay_paths(base)]
    assert any(Path(p).name == "corp.yml" for p in paths), paths
    assert not any(Path(p).name == "work.yml" for p in paths), paths

    got = origins(base, ["agent.model"])["agent.model"]
    assert got.effective == "corp-model"
    assert got.overlay_kind == "profile:base"


def test_candidates_stand_in_for_the_files_on_disk(tmp_path, monkeypatch):
    """Отказ записи судит стек, где цель стоит ещё не записанным текстом:
    кандидат подменяет дисковый документ и у базы, и у оверлея. Пустой `{}` —
    настоящий кандидат, а не «кандидата нет»: дисковое значение профиля сквозь
    него просачиваться не должно."""
    base = _write(tmp_path / "config.yml", 'profile: "work"\nagent:\n  model: base-model\n')
    profile = _write(tmp_path / "profiles" / "work.yml", "agent:\n  model: work-model\n")
    monkeypatch.chdir(tmp_path)

    on_disk = origins(base, ["agent.model"])["agent.model"]
    assert (on_disk.effective, on_disk.overlay_kind) == ("work-model", "profile:base")

    emptied = origins(base, ["agent.model"], candidates={profile: {}})["agent.model"]
    assert (emptied.effective, emptied.overlay_kind) == ("base-model", "")

    rebased = origins(base, ["agent.model"],
                      candidates={base: {"profile": "work", "agent": {"model": "work-model"}}})
    assert rebased["agent.model"].raw == "work-model"
    assert rebased["agent.model"].overlay_kind == ""   # профиль ничего не изменил


# --- error_origins: текст ошибки → файлы, где лежат названные ею ключи -------


def test_error_origins_groups_the_named_keys_by_the_file_that_sets_them(tmp_path, monkeypatch):
    """Ядро объяснения сломанного конфига (`onboarding.explain_broken_config`) и
    отказа записи (`registry_write._commit`): ключ из "; "-сегмента ошибки → слой,
    чьё наложение задало его значение. Ключи профиля приходят с видом слоя и
    ПОЛНЫМ путём файла профиля, ключ базы — с базой; два ключа одного файла —
    одной записью, порядок — закрытого списка подозреваемых. Хвост «в
    merged-реестре models» секцию models не приплетает: ключ ищется ПРЕФИКСОМ."""
    from config_origin import error_origins

    base = _write(tmp_path / "config.yml",
                  'profile: "work"\nmodels:\n  flash: {}\nslots:\n  max: ghost2\n')
    profile = _write(tmp_path / "profiles" / "work.yml",
                     "slots:\n  default: ghost1\n  light: ghost3\n")
    monkeypatch.chdir(tmp_path)      # нет .gc/ → проектного оверлея нет

    got = error_origins(base, (
        'slots.default: алиас "ghost1" не найден в merged-реестре models; '
        'slots.light: алиас "ghost3" не найден в merged-реестре models; '
        'slots.max: алиас "ghost2" не найден в merged-реестре models'))

    assert [(o.kind, o.path, o.keys) for o in got] == [
        ("profile:base", profile, ("slots.default", "slots.light")),
        ("base", base, ("slots.max",)),
    ]


def test_error_origins_names_no_file_for_an_error_that_names_no_key(tmp_path, monkeypatch):
    """Ошибка не про ключи (битый YAML, профиль не найден) — сопоставлять нечего:
    пустой ответ, а не выдуманный файл. Слово «models» в хвосте сегмента — не
    улика: иначе секция models приплеталась бы к любой ошибке слота."""
    from config_origin import error_origins

    base = _write(tmp_path / "config.yml", "models:\n  flash: {}\n")
    monkeypatch.chdir(tmp_path)

    assert error_origins(base, f"{base}: невалидный YAML: while parsing a block") == []
    assert error_origins(base, 'алиас "ghost" не найден в merged-реестре models') == []

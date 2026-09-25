"""canonical_path_key: три исхода ключа — относительный, внешний, защищённый."""
from __future__ import annotations

import os

import pytest

from gc_core.agent_edits.paths import canonical_path_key


@pytest.fixture
def launch_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "foo.py").write_text("x", encoding="utf-8")
    return root


def test_canonical_key_relative_under_launch_root(launch_root):
    key = canonical_path_key(str(launch_root / "src" / "foo.py"), str(launch_root))
    assert key == "src/foo.py"


def test_canonical_key_accepts_relative_path(launch_root):
    key = canonical_path_key("src/foo.py", str(launch_root))
    assert key == "src/foo.py"


def test_canonical_key_has_three_outcomes(launch_root, tmp_path, monkeypatch):
    """Три исхода, и `None` теперь означает ровно одно — «не трекать».

    Прежде `None` отвечал на два разных вопроса — «вне периметра» и «защищён», —
    и вызывающий их не различал. Внешний путь получает АБСОЛЮТНЫЙ ключ: его
    форма и есть признак, по которому он не попадёт ни на экран, ни в снимки.
    """
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")

    inside = canonical_path_key(str(launch_root / "src" / "foo.py"), str(launch_root))
    assert inside == "src/foo.py"

    outside = tmp_path / "other" / "a.py"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    external = canonical_path_key(str(outside), str(launch_root))
    assert external == outside.resolve().as_posix()
    assert external.startswith("/") or (os.name == "nt" and ":" in external)

    protected = launch_root / ".gc" / "config.yml"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("x", encoding="utf-8")
    assert canonical_path_key(str(protected), str(launch_root)) is None


def test_operational_gc_not_trackable(launch_root, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")
    gc_file = launch_root / ".gc" / "threads.json"
    gc_file.parent.mkdir(parents=True)
    gc_file.write_text("{}", encoding="utf-8")
    assert canonical_path_key(str(gc_file), str(launch_root)) is None


def test_regular_file_gets_relative_key(launch_root, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")
    path = str(launch_root / "src" / "foo.py")
    assert canonical_path_key(path, str(launch_root)) == "src/foo.py"


def test_protection_outranks_being_outside_the_project(launch_root, monkeypatch):
    """Защищённый путь ВНЕ проекта — `None`, а не внешний ключ.

    Проверка защиты стоит раньше проверки «внутри проекта»: иначе рабочий файл
    агента из `gc_home` получил бы абсолютный ключ и дифф в ленте. Защищённый
    вход внутри проекта (`.gc/`) этого порядка не закрепляет — там оба порядка
    дают один ответ."""
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import gc_paths

    monkeypatch.setattr(env_vars.env, "gc_home", launch_root.parent / "gc_home")
    operational = gc_paths()["config"] / "config.yml"
    operational.parent.mkdir(parents=True, exist_ok=True)
    operational.write_text("x", encoding="utf-8")
    assert not operational.resolve().is_relative_to(launch_root.resolve())
    assert canonical_path_key(str(operational), str(launch_root)) is None


def test_a_junction_is_recognised_by_its_reparse_tag(monkeypatch, tmp_path):
    """Junction ловится по тегу из ``lstat``, а не ``os.path.isjunction``: той нет
    до Python 3.12, и на 3.11 под Windows путь через junction прошёл бы как
    обычный. Заглушка облачного файла — тоже точка повторной обработки, но не
    ссылка: путь через неё законен."""
    import os
    import stat as stat_mod
    from types import SimpleNamespace

    from gc_core.agent_edits import paths as paths_mod

    tags = {"j": 0xA0000003, "cloud": 0x9000001A}

    def fake_lstat(node):
        tag = tags.get(os.path.basename(node))
        if tag is None:
            raise FileNotFoundError(node)
        return SimpleNamespace(st_mode=stat_mod.S_IFDIR, st_reparse_tag=tag)

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)

    assert paths_mod.goes_through_link(str(tmp_path), "j/a.py")
    assert not paths_mod.goes_through_link(str(tmp_path), "cloud/a.py")

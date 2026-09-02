"""Установка MCP из исходников: локальные предпочтительнее git.

Контракт поля ``install.source`` сменился 18.08.2026: раньше это был
ОТНОСИТЕЛЬНЫЙ ПУТЬ от каталога рецептов (``../external/office-mcp``), теперь —
ИМЯ каталога (``office-mcp``).

Причина смены. Путь резолвился от recipe_root и работал, пока рецепты и
исходники были соседями в обеих раскладках. После переезда рецептов в
``packages/mcp/src`` глубины разошлись: в колесе соседство сохраняет
force-include, а в рабочей копии путь стал вести в никуда — локальная установка
молча съезжала в git-метод, что для офлайн-контура означает отказ.
Имя вместо пути убирает арифметику и переживёт следующий переезд.

Ревизия 28.08.2026 смягчила контракт: старый формат теперь ПРИНИМАЕТСЯ с
одноразовым ``[warn]``, а не отвергается. Отказ был тихим: у пользователя может
лежать свой ``<gc_config>/recommended_mcp/config.local.yml``, дословно снятый с
develop, и он побеждает бандловый манифест — ``InstallError`` ловил общий
обработчик ``run_install_entries``, рецепт уходил в ``[warn] … пропущен``, CLI
возвращал 0, и онбординг шёл дальше уже без MCP. Падать осталось только на
значениях, из которых имени каталога не выделить вовсе.
"""

from pathlib import Path

import pytest

from recommended_mcp import install as inst


def test_repo_subdir_builds_path_from_name():
    # Параметра root у _repo_subdir больше нет: тело его не читало.
    assert inst._repo_subdir("office-mcp") == "packages/mcp/external/vendor/office-mcp"
    assert inst._repo_subdir("science-mcp") == "packages/mcp/external/vendor/science-mcp"


@pytest.mark.parametrize("legacy", ["../external/office-mcp", "external/office-mcp"])
def test_repo_subdir_tolerates_legacy_path(legacy, monkeypatch, capsys):
    """Дореформенный путь читается как имя последнего сегмента, а не падает.

    Терпим ровно то, что реально лежало в манифесте develop: `../external/<имя>`
    и тот же путь без ведущего `..`. У пользователя может быть свой
    `<gc_config>/recommended_mcp/config.local.yml`, дословно снятый с develop, и
    он побеждает бандловый манифест.
    """
    monkeypatch.setattr(inst, "_LEGACY_SOURCE_WARNED", set())
    assert inst._repo_subdir(legacy) == "packages/mcp/external/vendor/office-mcp"
    out = capsys.readouterr().out
    assert "[warn]" in out and "устаревший формат" in out


@pytest.mark.parametrize(
    "outside",
    ["../../sibling/pkg", "/abs/path/office-mcp", "../vendor/pkg", "a/b/c/pkg"],
    ids=["выход за репозиторий", "абсолютный", "мимо external", "произвольный путь"],
)
def test_repo_subdir_rejects_paths_outside_the_vendor_dir(outside, monkeypatch):
    """Путь, ведущий не в каталог вендоров, — ошибка конфигурации, а не «старый формат».

    Тест возвращён с develop, где `_repo_subdir(Path("../../sibling/pkg"), root)`
    бросал `InstallError`. При переходе «путь → имя» проверка исчезла вместе с
    ним: резолвер стал брать basename ЛЮБОГО пути, и `source: ../../sibling/pkg`
    молча превращался в `git+<repo>#subdirectory=…/vendor/pkg`. Установка уходила
    в сеть за подкаталогом, которого нет, и человек получал ошибку pip — на шаг
    позже и не про то, что он написал в манифесте.

    Отдельно от `test_repo_subdir_rejects_degenerate`: там имени нет вовсе
    (пустая строка, `.`), а здесь имя как раз есть, и именно поэтому промах был
    бесшумным.
    """
    monkeypatch.setattr(inst, "_LEGACY_SOURCE_WARNED", set())
    with pytest.raises(inst.InstallError) as e:
        inst._repo_subdir(outside)
    assert "install.source" in str(e.value)


def test_legacy_source_warned_once_per_process(monkeypatch, capsys):
    """Резолверов два на рецепт (_vendor_candidates и _repo_subdir) — варн один."""
    monkeypatch.setattr(inst, "_LEGACY_SOURCE_WARNED", set())
    inst._source_dir_name("../external/office-mcp")
    assert "[warn]" in capsys.readouterr().out
    inst._source_dir_name("../external/office-mcp")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("degenerate", ["", "   ", "/", ".", ".."])
def test_repo_subdir_rejects_degenerate(degenerate):
    """Имени каталога тут нет вовсе — терпеть нечего, падаем явно."""
    with pytest.raises(inst.InstallError):
        inst._repo_subdir(degenerate)


def test_local_sources_preferred_over_git(tmp_path, monkeypatch):
    """Есть исходники рядом — ставим из них, в сеть не идём."""
    # Раскладка колеса: recipes/ и external/vendor/ — соседи.
    recipes = tmp_path / "recommended_mcp"
    recipes.mkdir()
    src = tmp_path / "external" / "vendor" / "office-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='office-mcp'\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(inst, "active_pip_python", lambda: "PY")
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))

    inst._install_source("office", "office-mcp", recipes,
                         editable=False, repo_url="https://x/repo")

    assert calls, "установка не запускалась"
    assert str(src) in calls[0]
    assert not any("git+" in a for a in calls[0]), "ушли в git при живых локальных исходниках"


def test_git_fallback_when_no_local_sources(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(inst, "pip_argv", lambda *a: ["PY", "-m", "pip", *a])
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))

    inst._install_source("office", "office-mcp", tmp_path / "nope",
                         editable=False, repo_url="https://x/repo", repo_ref="main")

    joined = " ".join(calls[0])
    assert "git+https://x/repo@main#subdirectory=packages/mcp/external/vendor/office-mcp" in joined


def test_no_local_no_repo_url_errors(tmp_path):
    with pytest.raises(inst.InstallError) as e:
        inst._install_source("office", "office-mcp", tmp_path / "nope",
                             editable=False, repo_url=None)
    assert "просмотрено" in str(e.value), "в ошибке должны быть перечислены пути поиска"


def test_legacy_source_installs_instead_of_being_skipped(tmp_path, monkeypatch, capsys):
    """Сквозной путь B2: дореформенный манифест СТАВИТСЯ, а не пропускается.

    Изолированной проверки _repo_subdir мало: заявленная поломка была не в нём,
    а в том, что InstallError ловил общий обработчик run_install_entries и
    рецепт молча уходил в `[warn] … пропущен` при RC=0.
    """
    from recommended_mcp.manifest import InstallSpec, MCPEntry

    recipes = tmp_path / "recommended_mcp"
    recipes.mkdir()
    src = tmp_path / "external" / "vendor" / "office-mcp"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='office-mcp'\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(inst, "_LEGACY_SOURCE_WARNED", set())
    monkeypatch.setattr(inst, "active_pip_python", lambda: "PY")
    monkeypatch.setattr(inst.subprocess, "run", lambda argv, **kw: calls.append(argv))

    entry = MCPEntry(
        name="office", path=Path("."),
        install=InstallSpec(type="source", source=Path("../external/office-mcp")),
    )
    results = inst.run_install_entries([entry], recipes, None, editable=True)

    assert results == [("office", "installed", "")], results
    assert "пропущен" not in capsys.readouterr().out
    assert calls and str(src) in calls[0]

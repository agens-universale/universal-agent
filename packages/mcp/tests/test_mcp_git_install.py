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
"""

from pathlib import Path

import pytest

from recommended_mcp import install as inst


def test_repo_subdir_builds_path_from_name():
    root = Path("recommended_mcp")
    assert inst._repo_subdir("office-mcp", root) == "external/vendor/office-mcp"
    assert inst._repo_subdir("science-mcp", root) == "external/vendor/science-mcp"


@pytest.mark.parametrize("bad", ["../external/office-mcp", "external/office-mcp", "../x"])
def test_repo_subdir_rejects_paths(bad):
    """Старый формат обязан падать явно, а не резолвиться во что попало."""
    with pytest.raises(inst.InstallError):
        inst._repo_subdir(bad, Path("recommended_mcp"))


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
    assert "git+https://x/repo@main#subdirectory=external/vendor/office-mcp" in joined


def test_no_local_no_repo_url_errors(tmp_path):
    with pytest.raises(inst.InstallError) as e:
        inst._install_source("office", "office-mcp", tmp_path / "nope",
                             editable=False, repo_url=None)
    assert "просмотрено" in str(e.value), "в ошибке должны быть перечислены пути поиска"

"""Путь для показа: от корня проекта, через `~` или полностью."""

import ntpath

import pytest

from gc_core.utils import path_display
from gc_core.utils.path_display import display_path, home_path

ROOT = "/home/dmitry/work/project"
HOME = "/home/dmitry"


def test_display_path_inside_the_project_is_relative():
    assert display_path(f"{ROOT}/docs/shot.png", ROOT, HOME) == "docs/shot.png"


def test_display_path_of_the_project_root_is_a_dot():
    assert display_path(ROOT, ROOT, HOME) == "."


def test_display_path_inside_home_uses_tilde():
    assert (
        display_path(f"{HOME}/Desktop/shot.png", ROOT, HOME)
        == "~/Desktop/shot.png"
    )


def test_display_path_elsewhere_is_absolute():
    assert display_path("/tmp/shot.png", ROOT, HOME) == "/tmp/shot.png"


def test_display_path_keeps_a_relative_path():
    """Относительный путь уже от корня проекта (или имя из старой сессии)."""
    assert display_path("shot.png", ROOT, HOME) == "shot.png"
    assert display_path("../x/a.py", ROOT, HOME) == "../x/a.py"


def test_display_path_sibling_with_the_same_prefix_is_not_inside():
    assert (
        display_path(f"{ROOT}-old/shot.png", ROOT, HOME)
        == "~/work/project-old/shot.png"
    )


def test_home_path_shows_home_as_tilde_even_inside_the_project():
    assert (
        home_path(f"{ROOT}/.gc/config.yml", HOME)
        == "~/work/project/.gc/config.yml"
    )


def test_home_path_elsewhere_is_absolute():
    assert home_path("/etc/gc/config.yml", HOME) == "/etc/gc/config.yml"


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(path_display, "_path", ntpath)


WIN_ROOT = "C:\\Users\\dmitry\\work\\project"
WIN_HOME = "C:\\Users\\dmitry"


@pytest.mark.usefixtures("windows")
def test_windows_path_inside_the_project_is_relative():
    path = WIN_ROOT + "\\docs\\a.md"
    assert display_path(path, WIN_ROOT, WIN_HOME) == "docs\\a.md"


@pytest.mark.usefixtures("windows")
def test_windows_path_inside_home_uses_backslash_tilde():
    path = WIN_HOME + "\\Desktop\\a.md"
    assert display_path(path, WIN_ROOT, WIN_HOME) == "~\\Desktop\\a.md"


@pytest.mark.usefixtures("windows")
def test_windows_other_drive_is_absolute():
    assert display_path("D:\\logs\\b.log", WIN_ROOT, WIN_HOME) == (
        "D:\\logs\\b.log"
    )

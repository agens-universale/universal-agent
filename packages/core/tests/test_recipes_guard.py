"""Отсутствие пакета рецептов даёт строку, а не трейсбек.

Менеджер MCP уехал в дистрибутив gc-mcp, а команды `gc-mcp` / `gc-mcp-install`
объявляют ВСЕ профили приложения. Значит их могут набрать там, где этого
дистрибутива нет, — и тогда нужна одна строка, как у соседних шимов
(`cli_entry` / `tui_entry` / `serve_entry`), а не ModuleNotFoundError.

Второй закрываемый случай — автозапуск daemon'ов. Раньше он глотал ЛЮБУЮ
ошибку импорта голым `except ImportError: return`; в gc-ua автозапуск включён
по умолчанию, поэтому опечатка внутри рецептов молча выключала MCP, а агент
падал позже — на мёртвом порту, в инструменте.

Тест живёт в наборе ЯДРА, потому что проверяет код ядра: шим и мост автозапуска.
Сам менеджер проверяется в packages/mcp/tests.
"""

from __future__ import annotations

import sys

import pytest


class _Blocker:
    """meta_path-finder, отвечающий «не нашёл» для одного пакета.

    Подмена ``builtins.__import__`` не годится: ``importlib.import_module`` идёт
    мимо неё. Вариант ``sys.modules[name] = None`` неточен — даёт исключение с
    другим ``name``, а охранник различает именно его.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name or fullname.startswith(self.name + "."):
            raise ModuleNotFoundError(f"No module named {self.name!r}", name=self.name)
        return None


@pytest.fixture
def without_recipes(monkeypatch):
    """Сделать вид, что дистрибутива gc-mcp в окружении нет."""
    for name in [n for n in sys.modules if n.startswith("recommended_mcp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocker("recommended_mcp"), *sys.meta_path])


@pytest.mark.parametrize("argv", [["list"], ["install", "office"], ["help"]])
def test_gc_mcp_without_recipes_prints_one_line(without_recipes, capsys, argv):
    """Шим обязан объяснить, чего не хватает, вместо трейсбека."""
    from gc_core.mcp_entry import main

    rc = main(argv)

    err = capsys.readouterr().err
    assert rc == 1
    assert err.count("\n") <= 1, "отказ обязан быть однострочным"
    assert "recommended_mcp" in err


def test_gc_mcp_install_alias_also_guarded(without_recipes, capsys):
    from gc_core.mcp_entry import install

    assert install() == 1
    assert "recommended_mcp" in capsys.readouterr().err


def test_autostart_degrades_without_recipes(without_recipes, caplog):
    """Автозапуск демонов молчать не должен, но и падать тоже."""
    from gc_core.mcp_daemon_autostart import ensure_mcp_daemons

    with caplog.at_level("WARNING"):
        ensure_mcp_daemons({})

    assert any("recommended_mcp" in r.message for r in caplog.records), (
        "раньше эта ветка возвращалась молча — и MCP выключался незаметно"
    )


def test_real_import_error_is_not_masked(monkeypatch):
    """Опечатка ВНУТРИ рецептов обязана лететь как есть, а не выдаваться за «не установлено»."""
    from gc_core.mcp_runtime import recipes

    for name in [n for n in sys.modules if n.startswith("recommended_mcp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _BrokenDep:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("recommended_mcp"):
                raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BrokenDep(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as e:
        recipes.import_recipes("manifest")
    assert e.value.name == "yaml", "чужая ошибка импорта не должна маскироваться"

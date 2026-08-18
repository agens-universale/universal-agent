"""Ядро переживает отсутствие пакета рецептов и говорит об этом.

Единственное место, где ядро тянется к `recommended_mcp`, — мост автозапуска
daemon'ов. Ядро от дистрибутива gc-mcp НЕ зависит (зависят профили приложения),
поэтому у встраивающего SDK рецептов может не быть вовсе.

Раньше мост глотал ЛЮБУЮ ошибку импорта голым `except ImportError: return`.
В gc-ua автозапуск включён по умолчанию, поэтому опечатка ВНУТРИ рецептов молча
выключала MCP, а агент падал позже — на мёртвом порту, в инструменте.
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


def test_autostart_degrades_without_recipes(without_recipes, caplog):
    """Автозапуск демонов молчать не должен, но и падать тоже."""
    from gc_core.mcp_daemon_autostart import ensure_mcp_daemons

    with caplog.at_level("WARNING"):
        ensure_mcp_daemons({})

    assert any("recommended_mcp" in r.message for r in caplog.records), (
        "раньше эта ветка возвращалась молча — и MCP выключался незаметно"
    )


def test_real_import_error_is_not_masked(monkeypatch):
    """Опечатка ВНУТРИ рецептов обязана лететь как есть, а не выдаваться за «не установлено».

    Именно этот случай глотал прежний голый `except ImportError: return`:
    отсутствие зависимости внутри рецептов было неотличимо от отсутствия самих
    рецептов, и MCP выключался молча.
    """
    from gc_core.mcp_daemon_autostart import ensure_mcp_daemons

    for name in [n for n in sys.modules if n.startswith("recommended_mcp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _BrokenDep:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("recommended_mcp"):
                raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BrokenDep(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as e:
        ensure_mcp_daemons({})
    assert e.value.name == "yaml", "чужая ошибка импорта не должна маскироваться"

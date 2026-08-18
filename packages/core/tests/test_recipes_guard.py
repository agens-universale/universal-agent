"""Отсутствие пакета рецептов даёт строку, а не трейсбек.

Ядро тянет `recommended_mcp` ЛЕНИВО: без него агент работает, просто не
поднимает HTTP/SSE-daemon'ы и не умеет `gc-mcp`. Но до 18.08.2026 отказ был
двух разных сортов, и оба плохие:

- `gc-mcp` падал сырым ModuleNotFoundError с трейсбеком — единственная из пяти
  команд без охранника, тогда как у профилей охранники есть;
- `mcp_daemon_autostart` глотал ЛЮБУЮ ошибку импорта голым
  `except ImportError: return`. Автозапуск включён по умолчанию в gc-ua,
  поэтому опечатка внутри рецептов молча выключала MCP, а агент падал позже —
  на мёртвом порту, в инструменте.

Сейчас ветка отказа недостижима (рецепты лежат в колесе ядра), но станет
достижимой, когда они уедут в свой дистрибутив. Тест написан заранее, чтобы
переезд не превратился в регрессию.
"""

from __future__ import annotations

import pytest


class _Blocker:
    """meta_path-finder, отвечающий «не нашёл» для одного пакета.

    Подмена ``builtins.__import__`` тут НЕ годится: ``importlib.import_module``
    идёт мимо неё, прямо в машинерию импорта (проверено — импорт спокойно
    проходил при активной подмене). Вариант ``sys.modules[name] = None`` тоже
    неточен: он даёт ModuleNotFoundError с name = "recommended_mcp.manifest",
    тогда как настоящее отсутствие пакета даёт name = "recommended_mcp", а
    охранник различает именно это.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name or fullname.startswith(self.name + "."):
            raise ModuleNotFoundError(f"No module named {self.name!r}", name=self.name)
        return None


@pytest.fixture
def without_recipes(monkeypatch):
    """Сделать вид, что пакета recommended_mcp в окружении нет."""
    import sys

    for name in [n for n in sys.modules if n.startswith("recommended_mcp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocker("recommended_mcp"), *sys.meta_path])


@pytest.mark.parametrize("argv", [["list"], ["install", "office"], ["remove", "office"]])
def test_gc_mcp_without_recipes_prints_one_line(without_recipes, capsys, argv):
    from gc_core.mcp_runtime.cli import main

    rc = main(argv)

    err = capsys.readouterr().err
    assert rc == 1
    assert err.count("\n") <= 1, "отказ обязан быть однострочным"
    assert "recommended_mcp" in err


def test_gc_mcp_help_works_without_recipes(without_recipes, capsys):
    """Справка рецептов не требует и обязана работать всегда."""
    from gc_core.mcp_runtime.cli import main

    assert main(["help"]) == 0
    assert "gc-mcp" in capsys.readouterr().out


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

    Именно этот случай глотал прежний голый `except ImportError: return` в
    автозапуске: отсутствие зависимости внутри рецептов было неотличимо от
    отсутствия самих рецептов.
    """
    import sys

    from gc_core.mcp_runtime import recipes

    for name in [n for n in sys.modules if n.startswith("recommended_mcp")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _BrokenDep:
        """Пакет на месте, но внутри падает импорт зависимости."""

        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("recommended_mcp"):
                raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BrokenDep(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as e:
        recipes.import_recipes("manifest")
    assert e.value.name == "yaml", "чужая ошибка импорта не должна маскироваться"

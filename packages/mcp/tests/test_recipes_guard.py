"""Ошибка импорта ВНУТРИ рецептов обязана лететь как есть.

История. Мост автозапуска жил в ядре и глотал ЛЮБУЮ ошибку импорта голым
``except ImportError: return``. В gc-ua автозапуск включён по умолчанию, поэтому
опечатка внутри рецептов молча выключала MCP, а агент падал позже — на мёртвом
порту, уже в инструменте. Тогда охрану сузили: ловим только отсутствие самого
пакета, сверяя ``exc.name``.

27.08.2026 мост переехал из ядра сюда (`recommended_mcp.runtime.daemon_autostart`),
и охрана исчезла совсем. Так стало ПРАВИЛЬНЕЕ, а не слабее: рецепты теперь соседний
модуль того же дистрибутива, «их может не быть» — сценарий, которого нет. Прежний
парный тест `test_autostart_degrades_without_recipes` проверял ровно его и снят
вместе с посылкой: ядро к рецептам больше не тянется ни строкой, а `gc-cli`,
`gc-tui` и `gc-server` пинят `gc-mcp` жёстко.

А вот проверка ниже пережила переезд и стала строже: без перехвата чужая ошибка
импорта не может быть замаскирована в принципе.
"""

from __future__ import annotations

import sys

import pytest
from gc_testkit.settings import make_settings


def test_real_import_error_is_not_masked(monkeypatch):
    """Опечатка ВНУТРИ рецептов не выдаётся за «рецептов не установлено».

    Именно этот случай глотал прежний голый `except ImportError: return`:
    отсутствие зависимости внутри рецептов было неотличимо от отсутствия самих
    рецептов, и MCP выключался молча.
    """
    from recommended_mcp.runtime.daemon_autostart import ensure_mcp_daemons

    for name in [
        n for n in sys.modules if n.startswith("recommended_mcp.autostart")
    ]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    class _BrokenDep:
        """meta_path-finder: рецепты «есть», но их зависимость сломана.

        Подмена ``builtins.__import__`` не годится: ``importlib.import_module`` идёт
        мимо неё. Вариант ``sys.modules[name] = None`` неточен — даёт исключение с
        другим ``name``, а нам важен именно он.
        """

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "recommended_mcp.autostart":
                raise ModuleNotFoundError(
                    "No module named 'yaml'", name="yaml"
                )
            return None

    monkeypatch.setattr(sys, "meta_path", [_BrokenDep(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as e:
        ensure_mcp_daemons(make_settings())
    assert e.value.name == "yaml", (
        "чужая ошибка импорта не должна маскироваться"
    )

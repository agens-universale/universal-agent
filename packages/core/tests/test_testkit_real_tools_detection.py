"""Тест-кит узнаёт настоящий пакет `gc_core.tools` — и не путает с ним заглушку.

`_real_tools_loaded` решал по `build_tools_data`, которого в ветке больше нет:
осталась только корутинная `abuild_tools_data`. Ответ был «не загружен» ВСЕГДА,
и autouse-фикстура после КАЖДОГО теста выкидывала все `gc_core.tools*` и
импортировала пакет заново — десятки модулей на тест, а классы исключений
расщеплялись между «до» и «после» перезагрузки.
"""
import sys
import types

from gc_testkit.plugin import _real_tools_loaded, ensure_real_tools_package


def test_a_real_package_is_not_reloaded_again():
    """Пакет настоящий — повторный вызов его не трогает.

    Ломается, если проверка снова перестанет узнавать настоящий пакет: тогда
    каждый вызов подменяет модуль в `sys.modules` свежим импортом.
    """
    ensure_real_tools_package()
    loaded = sys.modules["gc_core.tools"]

    ensure_real_tools_package()

    assert sys.modules["gc_core.tools"] is loaded, (
        "настоящий gc_core.tools перезагружен заново — тест-кит его не узнаёт"
    )


def test_a_stub_is_not_taken_for_the_real_package(monkeypatch):
    """Заглушка с теми же именами — не настоящий пакет.

    Заглушки тестов ставят `abuild_tools_data` и `__path__`, но файла у них нет.
    Ломается, если проверку упростить до «есть нужное имя»: тогда заглушка
    переживёт тест и протечёт в соседние.
    """
    ensure_real_tools_package()
    stub = types.ModuleType("gc_core.tools")
    stub.__path__ = list(sys.modules["gc_core.tools"].__path__)
    stub.abuild_tools_data = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "gc_core.tools", stub)

    assert not _real_tools_loaded(), "заглушку приняли за настоящий пакет"

"""Сетка против двух классов поломок импорта, которые набор не ловит сам.

Оба класса вылезли при вливании develop 19.08.2026 и оба были найдены не
тестами, а разбором 138 падений постфактум. Механику см. в докстринге
`gc_testkit.import_health` — здесь только про то, почему это тест.

Тест живёт в корневом наборе, а не в пакете: он сканирует весь репозиторий и,
как контракт упаковки и `test_module_literals`, не принадлежит ни одному
дистрибутиву.

СЕТКА ПРОВЕРЕНА ПОДМЕНОЙ (19.08.2026), а не только тем, что она зелёная:
- `from gc_cli import arg_parser` → `import gc_cli.arg_parser` — найдено,
  с указанием строки 139, где зовётся голое имя;
- `_default_config_path` → `default_config_path` в одном импорте — найдено.
Без подмены обе проверки молчат.

ЧЕГО ОНА НЕ ЛОВИТ. Имя, которое модуль лепит динамически (`globals().update`,
`setattr(sys.modules[...])`), — такие модули пропускаются по признаку
`__getattr__`/`import *`, иначе пошли бы ложные срабатывания. Имя модуля,
записанное строкой, — это соседняя сетка `test_module_literals`. Ни одна из
трёх не полна сама по себе.
"""

from __future__ import annotations

from gc_testkit.import_health import scan


def test_no_broken_imports():
    """Ни одного битого имени и ни одного потерянного связывания.

    Класс 1 (`dotted_bind`): `import a.b` связывает `a`, не `b`. Кодмод
    переезда переписывал плоский `import arg_parser` в `import
    gc_cli.arg_parser`, и все обращения к голому `arg_parser.` становились
    NameError — на живом запуске, но не в тестах, которые патчат модуль
    целиком.

    Класс 2 (`missing_name`): `from mod import name`, где `name` в модуле нет.
    Так проявилось переименование `default_config_path` → `_default_config_path`
    на стороне develop, пережившее разрешение конфликта.
    """
    findings = scan()
    assert not findings, "поломки импорта:\n" + "\n".join(
        f"  {f.file}:{f.line}  [{f.kind}] {f.detail}" for f in findings
    )

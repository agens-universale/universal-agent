"""Сетка против трёх классов поломок импорта, которые набор не ловит сам.

Классы 1 и 2 вылезли при вливании develop 19.08.2026 и были найдены не
тестами, а разбором 138 падений постфактум. Класс 3 — дореформенное плоское
имя в узле импорта — нашло внешнее ревью 28.08.2026: под обе прежние сетки он
не попадал вовсе. Механику см. в докстринге `gc_testkit.import_health` — здесь
только про то, почему это тест.

Тест живёт в корневом наборе, а не в пакете: он сканирует весь репозиторий и,
как контракт упаковки и `test_module_literals`, не принадлежит ни одному
дистрибутиву.

СЕТКА ПРОВЕРЕНА ПОДМЕНОЙ, а не только тем, что она зелёная:
- `from gc_cli import arg_parser` → `import gc_cli.arg_parser` — найдено,
  с указанием строки 139, где зовётся голое имя (19.08.2026);
- `_default_config_path` → `default_config_path` в одном импорте — найдено
  (19.08.2026);
- `from usage.account import ...`, `from tools.registry import ...`,
  `import tui.app` — найдены все три формы (28.08.2026), см.
  `test_legacy_toplevel_import_is_found`;
- `find_spec`, бросающий про ЧУЖУЮ зависимость, — НЕ находка, а пропуск, см.
  `test_alien_dependency_is_a_skip_not_a_finding`: до правки этот путь давал
  173 ложных срабатывания в интерпретаторе без наших зависимостей;
- пропуск обязан быть виден в отчёте, см. `test_main_reports_skipped_modules`:
  «импорты здоровы» при полутора сотнях непроверенных модулей — ложный зелёный.
Без подмены все проверки молчат.

ЧЕГО ОНА НЕ ЛОВИТ. Имя, которое модуль лепит динамически (`globals().update`,
`setattr(sys.modules[...])`), — такие модули пропускаются по признаку
`__getattr__`/`import *`, иначе пошли бы ложные срабатывания. Имя модуля,
записанное строкой, — это соседняя сетка `test_module_literals`. Ни одна из
трёх не полна сама по себе.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import gc_testkit
import gc_testkit.import_health as ih
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


def test_legacy_toplevel_import_is_found(monkeypatch, tmp_path):
    """Подмена: дореформенное плоское имя в узле импорта обязано быть найдено.

    Класс 3 (A2). До 28.08.2026 его не судила ни одна сетка: `module_literals`
    знает все старые имена, но обходит только Call/Subscript/JoinedStr/Compare,
    а `check_missing_names` смотрит на новые префиксы. Подсадка
    `from usage.account import` + `from tools.registry import` в тест-файл
    оставляла прогон зелёным.

    Подмена делается на разобранном тексте, а не записью в дерево: сетка
    работает по AST, и файл ей для этого не нужен. Файл появляется только во
    второй половине — там проверяется ПРОВОДКА, а её без `scan()` не увидеть;
    он ложится во временный каталог, который на время подмены и объявлен
    корнем.
    """
    planted = (
        "from usage.account import spend\n"
        "from tools.registry import build\n"
        "import tui.app\n"
        "from gc_core.usage.account import spend as ok\n"
        "from gc_tui.app import App\n"
    )
    found = ih.check_legacy_toplevel(Path(__file__), ast.parse(planted))
    assert [f.line for f in found] == [1, 2, 3], found
    assert {f.kind for f in found} == {"legacy_toplevel"}
    assert "usage" in found[0].detail

    relative = "from .sibling import x\nfrom ..pkg.config import y\n"
    assert ih.check_legacy_toplevel(Path(__file__), ast.parse(relative)) == []

    # Вторая половина — про ПРОВОДКУ: сама проверка может быть исправна, а из
    # `scan()` не вызываться, и тогда на чистом дереве этого никто не заметит.
    # Подсовываем `scan()` единственный файл с подсадкой (дерево не трогаем:
    # корень на время подмены — tmp_path).
    planted_file = tmp_path / "legacy_module.py"
    planted_file.write_text(
        "from usage.account import spend\n"
        "from tools.registry import build\n"
        "import tui.app\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ih, "REPO", tmp_path)
    monkeypatch.setattr(ih, "_python_files", lambda: [planted_file])
    wired = ih.scan()
    assert [f.kind for f in wired] == ["legacy_toplevel"] * 3, wired


def test_legacy_list_covers_module_map():
    """`LEGACY_TOPLEVEL` заморожен константой — расходиться с картой нельзя.

    Константа, а не `git ls-tree develop:src`: в shallow-клоне истории нет, а
    после удаления объекта её не будет и в полном. Цена заморозки — ручная
    синхронизация, и сторожит её этот тест: новое имя в `module_map.json` без
    имени в константе означает дыру в классе 3.
    """
    mapping = json.loads(
        (Path(gc_testkit.__file__).parent / "module_map.json").read_text(encoding="utf-8")
    )
    missing = sorted(set(mapping) - ih.LEGACY_TOPLEVEL)
    assert not missing, f"есть в module_map.json, но не в LEGACY_TOPLEVEL: {missing}"


def test_alien_dependency_is_a_skip_not_a_finding(monkeypatch):
    """Подмена: `find_spec` бросает про ЧУЖУЮ зависимость — это пропуск.

    Ровно этот путь давал 173 ложных срабатывания в интерпретаторе без наших
    зависимостей (N5): голый `except` ронял управление прямо в `found.append`,
    и существующий файл объявлялся несуществующим именем. Отличаем по
    `e.name`: своё имя — находка, чужое — пропуск, и он обязан быть виден.
    """
    child = "gc_core.config.pydantic_probe"
    real = importlib.util.find_spec

    def alien(name, package=None):
        if name == child:
            raise ModuleNotFoundError(
                "No module named 'pydantic_settings'", name="pydantic_settings"
            )
        return real(name, package)

    monkeypatch.setattr(ih, "_SKIPPED", {})
    monkeypatch.setattr(importlib.util, "find_spec", alien)
    # Кэш живёт весь процесс и подмену `_SKIPPED` не замечает: ответ про модуль
    # уже лежит в нём, а причина пропуска — в словаре, который мы только что
    # заменили. Без сброса пара расходится, и следующий scan() в этой же сессии
    # не судит модуль и не заносит его в пропуски. Сбрасываем обе стороны, чтобы
    # они не могли разъехаться в принципе.
    ih._module_exists.cache_clear()
    ih._module_bindings.cache_clear()

    quiet = ih.check_missing_names(
        Path(__file__),
        ast.parse("from gc_core.config import pydantic_probe\n"),
        {"gc_core"},
    )
    assert quiet == [], quiet
    assert child in ih.skipped(), "пропуск обязан попасть в отчёт, а не исчезнуть"

    # Вторая половина не даёт «починить» тест тем, что сетка вообще перестанет
    # ругаться: своё несуществующее имя обязано остаться находкой.
    loud = ih.check_missing_names(
        Path(__file__),
        ast.parse("from gc_core.config import nope_not_here\n"),
        {"gc_core"},
    )
    assert len(loud) == 1, loud


def test_main_reports_skipped_modules(monkeypatch, capsys):
    """Подмена: отчёт обязан назвать пропуски — и числом, и поимённо.

    «Импорты здоровы» при непроверенных модулях — ложный зелёный: в
    интерпретаторе без наших зависимостей проверить не удаётся 150 модулей из
    448, и до правки отчёт об этом молчал (N5). Проверяются оба вердикта: и
    чистый, и с находками, — потому что хвост про пропуски легко потерять в
    одном из них.
    """
    monkeypatch.setattr(
        ih, "_SKIPPED", {"gc_core.config": "нет зависимости 'pydantic_settings'"}
    )
    monkeypatch.setattr(sys, "argv", ["import_health"])

    monkeypatch.setattr(ih, "scan", lambda: [])
    assert ih.main() == 0
    clean = capsys.readouterr().out
    assert "ПРОПУЩЕНО модулей: 1" in clean, clean
    assert "gc_core.config — нет зависимости" in clean, clean

    finding = ih.Finding(
        "tests/x.py", 1, "missing_name", "в `gc_core.config` нет имени `nope`"
    )
    monkeypatch.setattr(ih, "scan", lambda: [finding])
    assert ih.main() == 1
    loud = capsys.readouterr().out
    assert "НАХОДОК: 1; ПРОПУЩЕНО модулей: 1" in loud, loud
    assert "gc_core.config — нет зависимости" in loud, loud


def test_module_bindings_are_cached():
    """Кэш здесь не украшение, а условие того, что сетку вообще гоняют.

    Замерено на этом дереве: 7083 вызова `_module_bindings` на 448 уникальных
    модулей; без кэша `scan()` занимал 13.4 с и был 76% времени набора
    `tests/`. Снимут `lru_cache` — этот тест упадёт раньше, чем кто-то заметит
    просевшее время.
    """
    scan()
    info = ih._module_bindings.cache_info()
    assert info.hits > info.misses, info

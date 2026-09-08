"""Ядро не импортирует фронтенды. Ни одним импортом, включая ленивый.

Разделение на пакеты 27.08.2026 сняло ЕДИНСТВЕННУЮ такую зависимость:
``run_context`` под ``if is_tui`` звал ``gc_tui.diff_tui.tracking``. Обёртку
diff-журнала с тех пор ставит сам TUI (``attach_to_context``, зовёт ``TuiApp``).

Сторож появился потому, что вливание веток эту зависимость ВЕРНУЛО, и весь
набор из десяти тысяч тестов её не заметил:

- импорт был ленивый (внутри функции, под ``try/except ImportError``), поэтому
  ни один статический линтер и ни один импорт-тест до него не доходил;
- ``test_acceptance_non_blocking`` проверяет ``sys.modules`` после сборки
  агента, но ``AsyncAgent`` не ставит ``is_tui``, и ветка не исполнялась;
- ``test_packaging_wheel`` смотрит СОДЕРЖИМОЕ колёс, а не импорты в исходниках:
  ядро может зависеть от фронтенда, не таща его файлы в своё колесо.

Заметили по симптому в TUI: обёртка легла ДВАЖДЫ, потому что TUI к тому моменту
уже оборачивал сам.

Цена молчаливого возврата высокая. ``gc-core`` продаётся как самостоятельный
пакет для встраивающих; зависимость от ``gc_tui`` означает либо ``ImportError``
в headless-установке, либо (как здесь) тихую деградацию, когда ветка спрятана
за ``except ImportError`` и просто не срабатывает.
"""
import ast
import pathlib

from gc_testkit.paths import REPO

FRONTENDS = {"gc_cli", "gc_tui", "gc_server"}

#: Единственное законное исключение: шимы точек входа. ``gc-cli`` и ``gc-ua``
#: объявлены ядром, чтобы битая установка получала строку «поставьте профиль»
#: вместо трейсбека; импорт живёт под ``except ImportError`` и ровно это и
#: обслуживает. Список закрытый и по ИМЕНИ ФАЙЛА: «любой модуль с
#: ``entry`` в имени» пропустил бы новый файл, к точкам входа не относящийся.
ENTRY_SHIMS = {"cli_entry.py", "tui_entry.py", "serve_entry.py"}


def _frontend_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FRONTENDS:
                    found.append(f"{path}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in FRONTENDS:
                found.append(f"{path}:{node.lineno}: from {node.module} import ...")
    return found


def test_core_sources_do_not_import_frontends():
    core_src = REPO / "packages" / "core" / "src"
    offenders: list[str] = []
    for path in sorted(core_src.rglob("*.py")):
        if path.name in ENTRY_SHIMS:
            continue
        offenders.extend(_frontend_imports(path))

    assert offenders == [], (
        "ядро импортирует фронтенд:\n  "
        + "\n  ".join(offenders)
        + "\nЯдро — самостоятельный пакет: в headless-установке этих модулей нет. "
        "Действие, которому нужен фронтенд, обязано жить на стороне фронтенда "
        "(как diff-журнал в gc_tui.diff_tui.tracking.attach_to_context)."
    )


def test_entry_shims_exception_list_does_not_go_stale():
    """В списке исключений нет файлов, которые уже не импортируют фронтенд.

    Обратная сторона закрытого списка: разрешение, переставшее быть нужным,
    молча прикрывало бы новый импорт в том же файле.
    """
    core_src = REPO / "packages" / "core" / "src" / "gc_core"
    stale = [
        name
        for name in sorted(ENTRY_SHIMS)
        if (core_src / name).is_file() and not _frontend_imports(core_src / name)
    ]
    assert stale == [], (
        f"эти файлы больше не импортируют фронтенд — уберите их из ENTRY_SHIMS: {stale}"
    )

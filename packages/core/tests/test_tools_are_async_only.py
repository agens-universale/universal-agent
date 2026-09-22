"""У тулов ядра ОДНА форма вызова — `acall`. Синхронного входа нет.

Свойство, а не стиль. Две формы означали две реализации у каждого тула (или
шим-обёртку над второй) и третью ступень в диспетчере: «нет `acall` — уводим в
`asyncio.to_thread`». Ступень существовала «ради встраивающих», но
зарегистрировать свой Python-класс-тул извне НЕЛЬЗЯ:

* `abuild_tools_data` собирает набор целиком из `config_dict`;
* у `acreate_run_context` нет параметра для чужих тулов;
* механизма плагинов или entry-points нет.

То есть она сторожила несуществующую возможность, а платой были две формы и
слот общего пула на каждый вызов. Точка расширения для чужих тулов — MCP,
и она асинхронна насквозь.

Асинхронность ФОРМЫ не означает, что тело что-то ждёт: файловые и короткие
тулы исполняют его инлайн на loop (`test_file_tools_run_inline`), долгие идут
нативно (`test_native_async_tools`). Здесь проверяется только форма.
"""
import ast
import inspect

import pytest

from gc_testkit.paths import REPO

_TOOLS_DIR = REPO / "packages" / "core" / "src" / "gc_core" / "tools"


def _tool_classes() -> list[tuple[str, str]]:
    """Классы с `acall` — то есть тулы, а не вспомогательные типы."""
    found: list[tuple[str, str]] = []
    for path in sorted(_TOOLS_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover — синтаксис ловит другой гейт
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            has_acall = any(
                isinstance(m, (ast.AsyncFunctionDef, ast.FunctionDef)) and m.name == "acall"
                for m in cls.body
            )
            if has_acall:
                found.append((str(path.relative_to(_TOOLS_DIR)), cls.name))
    return found


def test_no_core_tool_declares_a_sync_entry():
    offenders = []
    for rel, cls_name in _tool_classes():
        tree = ast.parse((_TOOLS_DIR / rel).read_text(encoding="utf-8"))
        cls = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name == cls_name
        )
        for m in cls.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == "__call__":
                offenders.append(f"{rel}:{m.lineno}: {cls_name}.__call__")

    assert offenders == [], (
        "у этих тулов снова появился синхронный вход:\n  "
        + "\n  ".join(offenders)
        + "\nФорма одна — `acall`. Вторая означает вторую реализацию (или шим "
        "над ней) и возвращает третью ступень в диспетчер."
    )


def test_acall_is_the_abstract_contract():
    """Абстрактным объявлен именно `acall` — иначе тул без него молча создастся."""
    from gc_core.tools.simple_tool_base import AbstractSimpleTool

    assert "acall" in AbstractSimpleTool.__abstractmethods__, (
        "`acall` перестал быть абстрактным: тул без него создастся, а диспетчер "
        "упадёт уже в рантайме, на вызове"
    )
    assert "__call__" not in AbstractSimpleTool.__abstractmethods__


async def test_every_core_tool_acall_is_a_coroutine_function():
    """Не просто имя `acall`, а именно корутина.

    Синхронная функция с таким именем прошла бы диспетчер и вернула бы результат
    без `await` — тихо и неправильно.
    """
    from gc_core.tools import abuild_tools_data

    tools_data, _spec, _desc, _status = await abuild_tools_data({})
    not_coroutines = []
    for name, entry in tools_data.items():
        fabric = entry.get("tool_fabric")
        # `tool_fabric` — то класс тула, то замыкание (скилловые тулы носят
        # с собой реестр). Класса из замыкания не достать, поэтому смотрим на
        # ЭКЗЕМПЛЯР — ровно так же, как это делает диспетчер.
        try:
            tool = fabric(ctx=None, state=None, config_dict={}, call_id=None)
        except Exception:
            try:
                tool = fabric()
            except Exception as exc:      # pragma: no cover — диагностика
                not_coroutines.append(f"{name}: не собрался ({exc})")
                continue
        acall = getattr(tool, "acall", None)
        if acall is None:
            not_coroutines.append(f"{name}: нет acall")
        elif not inspect.iscoroutinefunction(acall):
            not_coroutines.append(f"{name}: acall не корутина")

    assert not_coroutines == [], "\n  ".join(not_coroutines)

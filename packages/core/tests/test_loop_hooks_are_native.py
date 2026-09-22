"""Хуки цикла агента ждутся нативно, а не через поток и портал.

``_acall_blocking_hook`` (``processing_responses.py``) корутинную функцию ждёт
напрямую, а обычную уводит в ``asyncio.to_thread``. Разница не в стиле: у всех
этих хуков синхронный двойник — шим через портал
(``get_portal().run(...)``), поэтому синхронное имя означает путь
«поток из пула → портальный loop → работа», то есть два прыжка через потоки и
второй event loop ради корутины, которая лежит рядом.

Ровно это чинил коммит 6546256e («два хука ядра ходили в поток и портал вместо
готового await»). Тест закрывает класс дефекта целиком: он смотрит, что в
``arun_agent`` ни один хук не передаётся синхронным именем, — а не перечисляет
известные случаи, которые уже починили.
"""

import ast
import inspect

from gc_testkit.paths import REPO

_SRC = REPO / "packages/core/src/gc_core/processing_responses.py"


def _hook_arguments() -> list:
    """``(строка, имя)`` для каждого первого аргумента ``_acall_blocking_hook``."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_acall_blocking_hook"
            and node.args
        ):
            found.append((node.lineno, ast.unparse(node.args[0])))
    return found


def test_every_hook_is_passed_by_its_async_name():
    """Ни один хук не передаётся синхронным именем.

    Хуки, приходящие параметром ``arun_agent`` (компакция), проверяются в
    ``test_compaction_hooks_arrive_as_coroutines`` — здесь только те, что
    processing_responses импортирует сам.
    """
    from gc_core import processing_responses as module

    offenders = []
    for lineno, name in _hook_arguments():
        target = getattr(module, name, None)
        if target is None:
            continue  # параметр функции, а не импорт — см. соседний тест
        if not inspect.iscoroutinefunction(target):
            offenders.append(f"processing_responses.py:{lineno} — {name}")

    assert offenders == [], (
        "хук передан синхронным именем — это поток из пула плюс портальный "
        "loop вместо готового await:\n  " + "\n  ".join(offenders)
    )


def test_compaction_hooks_arrive_as_coroutines():
    """``AgentExecutor`` отдаёт в цикл корутинные двойники хуков компакции.

    Их ``processing_responses`` не импортирует — они приходят параметрами, и
    предыдущий тест их не видит. Проверяем на стороне отправителя.
    """
    src = (REPO / "packages/core/src/gc_core/agent_executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    passed = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "arun_agent":
            for kw in node.keywords:
                if kw.arg and kw.arg.endswith("_hook"):
                    passed[kw.arg] = ast.unparse(kw.value)

    assert passed, "не нашёл вызов arun_agent с хуками — тест устарел"

    offenders = [
        f"{arg}={value}"
        for arg, value in passed.items()
        # Корутинные двойники в этом коде именуются приставкой `a`.
        if not value.rsplit(".", 1)[-1].startswith("a")
    ]
    assert offenders == [], (
        "в цикл переданы синхронные имена хуков компакции — каждое означает "
        "занятый поток пула на всё время суммаризации:\n  " + "\n  ".join(offenders)
    )

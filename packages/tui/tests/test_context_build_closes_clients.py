"""Пул httpx TUI закрывается на выходе цикла агента, не в сборке и не в сжатии.

Раньше сборка контекста, ход и `/compact` поднимали каждый свой
``asyncio.run`` и закрывали пул своего loop в ``finally``: реестр клиентов
`async_http` ключуется по ``id(loop)``, и клиент умершего loop мог достаться
следующему. Теперь цикл агента один на приложение и переживает ходы — на нём
же живут фоновые субагенты. Закрыть пул посреди жизни цикла значит оборвать
их запросы к модели (пересборка на ``/profile``, ``/compact`` при живых
детях). Поэтому ``aclose_clients`` зовётся ровно в одном месте — в уборке
потока цикла после `AgentLoop.shutdown`, на выходе приложения.

Проверка структурная: поведенческая (``test_agent_loop``) ловит живой клиент,
а эта краснеет на самой правке, вернувшей закрытие в сборку или сжатие.
"""

import ast

from gc_testkit.paths import TUI_PKG

_SERVICES = TUI_PKG / "services"


def _calls_of(path, name: str) -> dict[str, int]:
    """Число вызовов ``name`` по функциям файла (ключ — имя функции)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called == name:
                found[fn.name] = found.get(fn.name, 0) + 1
    return found


def test_context_build_and_compaction_do_not_close_the_pool():
    for path in (TUI_PKG / "app.py", _SERVICES / "agent_runner.py"):
        assert _calls_of(path, "aclose_clients") == {}, (
            f"{path.name}: пул httpx закрывается вне AgentLoop.shutdown — "
            "сборка или сжатие оборвали бы запросы живых субагентов"
        )


def test_the_pool_closes_in_agent_loop_shutdown():
    calls = _calls_of(_SERVICES / "agent_loop.py", "aclose_clients")
    assert calls == {"_aclose_clients": 1}, (
        "пул httpx цикла агента должен закрываться ровно один раз, в "
        "уборке потока цикла после AgentLoop.shutdown "
        f"(`_aclose_clients`), а не {calls}"
    )

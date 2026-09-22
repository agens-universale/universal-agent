"""Прогрев рабочего каталога Harbor: с потолком по времени и без тихого `/`.

`aprime_default_cwd` спрашивает контейнер «где мы» один раз при сборке
контекста. Вопрос шёл без таймаута — со стороны ядра дедлайна нет вовсе, и
медленный контейнер задерживал бы сборку сколько угодно. А отказ прогрева
растворялся: `discover_backend_cwd` молча возвращал `/`, и корнем рабочей
области становилась корневая файловая система вместо WORKDIR контейнера —
ровно тот симптом, который уже чинили.
"""

import ast
import logging
import pathlib

from gc_core.backends import harbor as harbor_backend
from gc_core.execution_workspace import aprime_backend_cwd


class _Backend:
    """Бэкенд, у которого прогрев не удался."""

    def __init__(self, result=None):
        self._result = result
        self.calls = 0

    async def aprime_default_cwd(self):
        self.calls += 1
        return self._result


async def test_failed_prime_is_logged_not_swallowed(caplog):
    backend = _Backend(result=None)

    with caplog.at_level(logging.WARNING):
        await aprime_backend_cwd(backend)

    assert backend.calls == 1
    assert any("backend_cwd_prime_failed" in r.getMessage() for r in caplog.records), (
        "отказ прогрева не виден в логе — корнем тихо станет '/'"
    )


async def test_successful_prime_is_quiet(caplog):
    backend = _Backend(result="/app")

    with caplog.at_level(logging.WARNING):
        await aprime_backend_cwd(backend)

    assert not [r for r in caplog.records if "backend_cwd_prime_failed" in r.getMessage()]


async def test_backend_without_prime_is_skipped():
    """Локальный бэкенд метода не имеет — и это не повод шуметь."""

    class _Plain:
        pass

    await aprime_backend_cwd(_Plain())


def test_prime_asks_with_a_timeout():
    """Структурно: у вопроса «где мы» есть потолок по времени."""
    tree = ast.parse(pathlib.Path(harbor_backend.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "aprime_default_cwd")

    exec_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_aexec_impl"]
    assert exec_calls, "проверка устарела: прогрев больше не спрашивает окружение"
    assert all(any(kw.arg == "timeout" for kw in call.keywords) for call in exec_calls), (
        "прогрев рабочего каталога идёт без таймаута — медленный контейнер "
        "задержит сборку контекста на неопределённое время"
    )
    assert isinstance(harbor_backend._CWD_PRIME_TIMEOUT_SEC, (int, float))
    assert 0 < harbor_backend._CWD_PRIME_TIMEOUT_SEC <= 60

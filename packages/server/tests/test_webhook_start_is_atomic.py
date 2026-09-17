"""Два вебхука на одну задачу: ключ занят с первого запроса, сообщение не теряется.

Сборка контекста идёт через `await` (дискавери MCP по сети). Раньше проверка
«идёт ли прогон» и старт стояли по разные стороны этого ожидания: второй вебхук
видел ключ свободным, строил ВТОРОЙ контекст, оба запроса отвечали `started`, а
сообщение второго не доходило ни до кого. Контекст проигравшего оставался жить с
логгером и MCP-сессиями, которые некому закрыть.
"""
import ast
import asyncio
import pathlib

import pytest

from gc_server.rest_api.agent_execution_registry import AgentRunRegistry

BUILD_SEC = 0.05


class _Executor:
    """Двойник AgentExecutor: копит сообщения, ход держит до отпускания."""

    def __init__(self, ctx=None):
        self.messages: list = []
        self.release = asyncio.Event()

    def add_message(self, message) -> None:
        self.messages.append(message)

    def stop_run(self) -> None:
        self.release.set()

    async def arun(self, messages, **kwargs):
        self.messages.extend(messages)
        await self.release.wait()
        return "done"


def _make_message(content: str) -> dict:
    return {"role": "user", "content": content, "id": f"m-{content}"}


async def _webhook(registry: AgentRunRegistry, content: str, built: list) -> str:
    """Путь эндпоинта: занятость → резерв → сборка контекста → старт."""
    if registry.add_user_message(system="s", task_id="t",
                                 make_message=_make_message, content=content):
        return "queued"
    if not registry.reserve(system="s", task_id="t"):
        registry.add_user_message(system="s", task_id="t",
                                  make_message=_make_message, content=content)
        return "queued"
    await asyncio.sleep(BUILD_SEC)                      # acreate_run_context
    executor = _Executor()
    built.append(executor)
    await registry.astart_if_absent(
        system="s", task_id="t", executor_factory=lambda ctx: executor,
        ctx=None, initial_user_message=content, thread_id="t",
    )
    return "started"


@pytest.mark.asyncio
async def test_second_webhook_is_queued_and_its_message_reaches_the_agent():
    registry = AgentRunRegistry()
    built: list = []

    first, second = await asyncio.gather(
        _webhook(registry, "first", built),
        _webhook(registry, "second", built),
    )

    assert sorted([first, second]) == ["queued", "started"], (
        f"оба запроса ответили {first}/{second} — ключ не был занят на время сборки"
    )
    assert len(built) == 1, "построен второй контекст впустую: его никто не закроет"

    executor = built[0]
    await asyncio.sleep(0)                              # дать таске прогона стартовать
    texts = [m["content"] if isinstance(m, dict) else m for m in executor.messages]
    assert "first" in texts and "second" in texts, (
        f"агент получил только {texts} — сообщение второго вебхука потеряно"
    )

    executor.stop_run()
    run = registry.get_status(system="s", task_id="t")
    if run is not None and run.thread is not None:
        await run.thread


def test_webhook_reserves_the_key_before_building_the_context():
    """Проверка самого эндпоинта, а не только реестра.

    Тест выше моделирует путь эндпоинта у себя внутри — и остаётся зелёным,
    даже если из сервера убрать резерв совсем (проверено подменой). Поэтому
    порядок в коде эндпоинта смотрим отдельно: занять ключ → и только потом
    первый `await`.
    """
    import importlib.util

    origin = importlib.util.find_spec("gc_server.server").origin
    tree = ast.parse(pathlib.Path(origin).read_text(encoding="utf-8"))
    webhook = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and "webhook" in n.name)

    reserve_at = [n.lineno for n in ast.walk(webhook)
                  if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "reserve"]
    build_at = [n.lineno for n in ast.walk(webhook)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "acreate_run_context"]
    release_at = [n.lineno for n in ast.walk(webhook)
                  if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", None) == "release_reservation"]

    assert reserve_at, (
        "эндпоинт не занимает ключ: второй вебхук на ту же задачу построит "
        "свой контекст, а его сообщение не дойдёт до агента"
    )
    assert build_at, "проверка устарела: контекст в эндпоинте больше не строится"
    assert min(reserve_at) < min(build_at), (
        f"резерв на строке {min(reserve_at)} стоит ПОСЛЕ сборки контекста "
        f"({min(build_at)}) — окно гонки осталось"
    )
    assert release_at, "резерв некому снять: при отказе сборки ключ залипнет навсегда"


@pytest.mark.asyncio
async def test_failed_context_build_releases_the_key():
    """Сборка упала — ключ обязан освободиться, иначе задача «строится» вечно."""
    registry = AgentRunRegistry()

    assert registry.reserve(system="s", task_id="t") is True
    assert registry.reserve(system="s", task_id="t") is False

    registry.release_reservation(system="s", task_id="t")

    assert registry.reserve(system="s", task_id="t") is True


@pytest.mark.asyncio
async def test_messages_arriving_during_the_build_are_not_lost_on_failure():
    """Сообщения, пришедшие во время сборки, возвращаются при снятии резерва."""
    registry = AgentRunRegistry()
    registry.reserve(system="s", task_id="t")

    assert registry.add_user_message(system="s", task_id="t",
                                     make_message=_make_message, content="during") is True

    pending = registry.release_reservation(system="s", task_id="t")
    assert [m["content"] for m in pending] == ["during"]


# --- R2.5: возврат резерва нельзя выбрасывать -------------------------------


@pytest.mark.asyncio
async def test_carried_over_messages_reach_the_next_run():
    """Сборка упала — сообщения ждут следующего вебхука ПО ТОЙ ЖЕ задаче.

    Им уже ответили `queued`. Выбросить их — соврать клиенту; утащить в чужой
    прогон нельзя; поэтому они ждут своей задачи и уезжают агенту первыми.
    """
    registry = AgentRunRegistry()
    registry.reserve(system="s", task_id="t")
    registry.add_user_message(system="s", task_id="t",
                              make_message=_make_message, content="во время сборки")

    # Сборка упала: эндпоинт снимает резерв и откладывает недоставленное.
    undelivered = registry.release_reservation(system="s", task_id="t")
    registry.carry_over(system="s", task_id="t", messages=undelivered)

    # Следующий вебхук по той же задаче.
    registry.reserve(system="s", task_id="t")
    executor = _Executor()
    await registry.astart_if_absent(
        system="s", task_id="t", executor_factory=lambda ctx: executor,
        ctx=None, initial_user_message="следующее", thread_id="t",
    )
    await asyncio.sleep(0)

    texts = [m["content"] if isinstance(m, dict) else m for m in executor.messages]
    assert "во время сборки" in texts, (
        f"сообщение, принятое как queued, пропало после отказа сборки: {texts}"
    )

    executor.stop_run()
    run = registry.get_status(system="s", task_id="t")
    if run is not None and run.thread is not None:
        await run.thread


def test_endpoint_does_not_discard_the_released_reservation():
    """Эндпоинт обязан ИСПОЛЬЗОВАТЬ возврат `release_reservation`.

    Проверка по коду, а не по реестру, и это не педантизм: тест выше работает с
    реестром напрямую и останется зелёным, даже если сервер снова выбросит
    возврат. Ровно так же устроен сторож порядка резерва выше.
    """
    import importlib.util

    origin = importlib.util.find_spec("gc_server.server").origin
    tree = ast.parse(pathlib.Path(origin).read_text(encoding="utf-8"))
    webhook = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and "webhook" in n.name)

    discarded = [
        n.lineno for n in ast.walk(webhook)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "attr", None) == "release_reservation"
    ]
    assert not discarded, (
        f"возврат release_reservation выброшен (строка {discarded}): сообщения, "
        f"которым ответили queued, исчезнут при отказе сборки"
    )


# --- R2.7: статус во время сборки -------------------------------------------


def test_registry_reports_a_live_reservation():
    registry = AgentRunRegistry()
    assert registry.is_reserved(system="s", task_id="t") is False
    registry.reserve(system="s", task_id="t")
    assert registry.is_reserved(system="s", task_id="t") is True
    registry.release_reservation(system="s", task_id="t")
    assert registry.is_reserved(system="s", task_id="t") is False


def test_status_endpoint_checks_the_reservation_before_404():
    """Пока идёт сборка, статус не 404 и не `completed` прошлого прогона.

    Под тем же ключом в `_completed` может лежать прошлый прогон с TTL 15 минут,
    поэтому проверка брони обязана стоять ДО обращения к нему и до 404.
    """
    import importlib.util

    origin = importlib.util.find_spec("gc_server.server").origin
    tree = ast.parse(pathlib.Path(origin).read_text(encoding="utf-8"))
    status = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_run_status")

    reserved_at = [n.lineno for n in ast.walk(status)
                   if isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "is_reserved"]
    raises_at = [n.lineno for n in ast.walk(status) if isinstance(n, ast.Raise)]

    assert reserved_at, (
        "статус не спрашивает про бронь: клиент, получивший queued, увидит 404 "
        "или статус прошлого прогона"
    )
    assert min(reserved_at) < max(raises_at), (
        f"проверка брони (строка {min(reserved_at)}) стоит после отказа "
        f"(строка {max(raises_at)})"
    )

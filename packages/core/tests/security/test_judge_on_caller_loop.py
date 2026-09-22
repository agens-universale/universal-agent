"""Судья модерации ходит в сеть на loop ВЫЗЫВАЮЩЕГО, а не на своём одноразовом.

Тело гварда целиком уезжало в поток-демон, где живого loop нет, и судья
звался оттуда через ``run_coro_blocking``: свой ``asyncio.run`` на каждый
вызов, а значит свежий пул httpx и новое TLS-рукопожатие к LLM на КАЖДЫЙ
модерируемый ``exec_command`` — при том что соединение агента простаивало
рядом. На конфиге по умолчанию (``command_moderation.enabled``) это цена
каждой команды агента.

Теперь тело идёт инлайн и просит блокирующие куски у драйвера поштучно:
судью — через ``AwaitOnCaller`` (ждётся на loop вызывающего), ожидание
человека — через ``OffLoop`` (у него асинхронной формы нет).
"""
import asyncio

from gc_core.security import command_moderation as cm
from gc_core.security.dual_wrapper import AwaitOnCaller, OffLoop, dual_wrap


async def test_async_judge_runs_on_the_callers_loop():
    """Корутину судьи ждёт тот же loop, что и вызывающего."""
    seen: list = []

    async def _ajudge(_payload):
        seen.append(asyncio.get_running_loop())
        return {"verdict": cm.SAFE_VERDICT, "confidence": 0.9, "risk_flags": []}

    def _flow(*_a, **_kw):
        result = yield from cm.judge_with_retry_flow(_ajudge, {}, retries=1)
        assert result.verdict == cm.SAFE_VERDICT
        return (yield)

    async def _tool(**_kw):
        return "ok"

    guarded = dual_wrap(_tool, _flow)
    assert await guarded() == "ok"

    assert seen == [asyncio.get_running_loop()], (
        "судья ушёл на чужой loop — значит опять свой пул и своё рукопожатие"
    )


async def test_sync_judge_still_goes_to_a_thread():
    """Обычного судью (подмена в тестах, чужой extractor) уводим в поток."""
    import threading

    seen: list = []

    def _judge(_payload):
        seen.append(threading.current_thread())
        return {"verdict": cm.SAFE_VERDICT, "confidence": 0.9, "risk_flags": []}

    def _flow(*_a, **_kw):
        yield from cm.judge_with_retry_flow(_judge, {}, retries=1)
        return (yield)

    async def _tool(**_kw):
        return "ok"

    assert await dual_wrap(_tool, _flow)() == "ok"
    assert seen and seen[0] is not threading.current_thread(), (
        "синхронный судья исполнился прямо на loop"
    )


async def test_judge_failure_lands_in_the_bodys_except():
    """Отказ приходит В ТОЧКУ запроса: `try/except` тела обязан его поймать.

    Иначе fail-closed сломался бы молча: тело перестало бы отличать
    «судья отказал» от «судья сказал опасно».
    """
    calls: list = []

    async def _boom(_payload):
        calls.append(1)
        raise RuntimeError("судья лёг")

    caught: list = []

    def _flow(*_a, **_kw):
        try:
            yield from cm.judge_with_retry_flow(_boom, {}, retries=1)
        except RuntimeError as exc:
            caught.append(str(exc))
        return (yield)

    async def _tool(**_kw):
        return "ok"

    assert await dual_wrap(_tool, _flow)() == "ok"
    assert caught == ["судья лёг"], caught
    assert len(calls) == 2, f"ретрай не сработал: попыток {len(calls)}"


def test_moderation_body_no_longer_owns_a_loop():
    """У модерации не должно остаться собственного `asyncio.run`."""
    import inspect

    src = inspect.getsource(cm)
    # Именно ВЫЗОВ, а не упоминание: разбор причины в комментарии полезен и
    # должен пережить этот тест.
    assert "run_coro_blocking(" not in src, (
        "модерация снова поднимает свой loop — это и есть лишнее рукопожатие"
    )
    assert "body_may_block=True" not in src, (
        "тело снова уезжает в поток целиком, вместе с непереносимой работой"
    )


async def test_human_wait_still_leaves_the_loop_responsive():
    """Ожидание человека обязано остаться в потоке: loop должен дышать."""
    import threading

    release = threading.Event()
    ticks = 0

    def _wait():
        release.wait(5)
        return True

    def _flow(*_a, **_kw):
        answer = yield OffLoop(_wait)
        assert answer is True
        return (yield)

    async def _tool(**_kw):
        return "ok"

    task = asyncio.create_task(dual_wrap(_tool, _flow)())
    for _ in range(20):
        await asyncio.sleep(0.01)
        ticks += 1
    release.set()

    assert await task == "ok"
    assert ticks == 20, "loop стоял, пока тело ждало человека"


async def test_await_on_caller_is_not_a_second_call_point():
    """Запрос к драйверу не считается точкой вызова тула."""
    calls: list = []

    async def _judge(_p):
        return {"verdict": cm.SAFE_VERDICT, "confidence": 0.5, "risk_flags": []}

    def _flow(*_a, **_kw):
        yield AwaitOnCaller(lambda: _judge({}))
        yield AwaitOnCaller(lambda: _judge({}))
        return (yield)

    async def _tool(**_kw):
        calls.append(1)
        return "ok"

    assert await dual_wrap(_tool, _flow)() == "ok"
    assert len(calls) == 1, calls

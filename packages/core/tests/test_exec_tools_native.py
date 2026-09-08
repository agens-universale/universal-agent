"""``exec_command`` и ``exec_python`` запускают процесс нативно.

Это два последних тула ядра, уходивших в ``asyncio.to_thread`` целиком. Цена
была не в задержке одного вызова (там разница ~1.7 мс), а в занятом слоте
общего пула: воркеров ``min(32, cpu+4)``, а слот держится ВЕСЬ таймаут — по
умолчанию 120 с. Замерено: 20 параллельных ``exec_command`` — 4.09 с через пул
против 2.10 с нативно.

У запуска процесса нативная форма есть — ``asyncio.create_subprocess_exec``
внутри ``LocalFsBackend.aexec`` / ``arun_python``. Синхронный ``exec`` — сам
шим над ней (``backends/local.py``), так что подмена не меняет реализацию, а
убирает с пути поток.

Тонкость, из-за которой правка не сводится к добавлению ``acall``
------------------------------------------------------------------

Оба тула обёрнуты двумя гвардами, и один из них — ``moderate_command_execution``
— зовёт LLM-судью через ``request_auxiliary_completion_text``, а тот идёт
ЧЕРЕЗ ПОРТАЛ (``summarize/llm.py``). Портал из-под живого event loop поднимает
``SyncApiInsideEventLoopError``.

Пока тул целиком уезжал в поток, живого loop под судьёй не было и портал
работал. Наивный ``acall`` исполнил бы тело гварда на loop — и ``exec_command``
падал бы на ДЕФОЛТНОМ конфиге: ``CommandModerationConfig`` это
``enabled=True, mode="ask"`` (secure-by-default), то есть судья активен из
коробки.

Поэтому у ``dual_wrap`` появился флаг ``body_may_block``: тело модерации
остаётся в потоке (там ему и место — портал плюс возможное ожидание человека),
а сам запуск процесса всё равно идёт нативно, потому что гвард ждёт тул через
``await``. Остальные гварды — чистый CPU на десятки микросекунд — как и
раньше, инлайн.
"""
import asyncio
import json
import time

import pytest

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_testkit.helpers.tool_ctx import local_ctx

#: Конфиг без судьи и без tripwire: тесты про СПОСОБ запуска, а не про политику.
_NO_GUARDS = {
    "security": {
        "command_moderation": {"enabled": False},
        "protected_paths": {"enabled": False},
    }
}

NAP = 0.6
TICK = 0.02


def _cmd(config_dict=None):
    return ExecuteCommand(ctx=local_ctx(), config_dict=config_dict or dict(_NO_GUARDS))


def _py(config_dict=None):
    return RunPythonCode(ctx=local_ctx(), config_dict=config_dict or dict(_NO_GUARDS))


async def _worst_gap(coro_factory):
    gaps: list = []
    stop = asyncio.Event()

    async def pulse() -> None:
        prev = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(pulse())
    await asyncio.sleep(0.05)
    result = await coro_factory()
    stop.set()
    await beat
    return result, (max(gaps) if gaps else float("inf"))


# --- exec_command ----------------------------------------------------------


async def test_command_acall_runs_and_returns_output():
    """Нативный вход делает ту же работу, что и синхронный."""
    out = json.loads(await _cmd().acall(command="echo привет"))

    assert out["ok"] is True, out
    assert "привет" in out["stdout"]
    assert out["exit_code"] == 0


async def test_command_does_not_hold_the_loop():
    """Пока команда идёт, loop продолжает тикать."""
    _, gap = await _worst_gap(
        lambda: _cmd().acall(command=f"python3 -c 'import time; time.sleep({NAP})'")
    )

    assert gap < NAP / 2, f"loop залипал на {gap:.3f}с — запуск не асинхронный"


async def test_command_does_not_hold_a_pool_slot_for_its_whole_duration():
    """Слот пула не занят на всё время команды — вот главный выигрыш.

    Раньше тул уезжал в пул ЦЕЛИКОМ, и слот держался весь запуск (по
    умолчанию до 120 с). Проверяем именно это: забиваем пул надолго и запускаем
    команду сопоставимой длительности. Если бы она шла через пул, ей пришлось
    бы сперва дождаться слота — то есть суммарно около двух длительностей.

    Один короткий переход в пул на пути остаётся: тело гварда модерации ходит
    в поток намеренно (внутри портальный LLM-судья, см. ``body_may_block``).
    Поэтому порог — не «мгновенно», а «заметно меньше двух длительностей».
    """
    hog_sec = 1.2
    cmd_sec = 1.0
    hogs = [
        asyncio.create_task(asyncio.to_thread(time.sleep, hog_sec)) for _ in range(24)
    ]
    await asyncio.sleep(0.05)

    started = time.perf_counter()
    out = json.loads(
        await _cmd().acall(command=f"python3 -c 'import time; time.sleep({cmd_sec})'")
    )
    spent = time.perf_counter() - started

    for h in hogs:
        h.cancel()
    await asyncio.gather(*hogs, return_exceptions=True)

    assert out["ok"] is True, out
    assert spent < hog_sec + cmd_sec * 0.5, (
        f"команда заняла {spent:.2f}с при забитом пуле — похоже, она всё ещё "
        f"ждёт слот и держит его на весь запуск"
    )


async def test_command_validation_rejects_bad_arguments():
    """Проверки аргументов на месте.

    Раньше тест сверял два входа между собой. Вход теперь один — сверять не с
    чем, поэтому проверяем сам отказ.
    """
    tool = _cmd()

    for bad in ("", 123):
        out = json.loads(await tool.acall(command=bad))
        assert out["ok"] is False, out
        assert out["error"]["type"] == "invalid_argument", out


async def test_command_reports_a_timeout_the_same_way():
    """Таймаут — общий путь разбора результата, а не отдельная ветка."""
    out = json.loads(
        await _cmd().acall(
            command="python3 -c 'import time; time.sleep(5)'", timeout_seconds=1
        )
    )

    assert out["ok"] is False, out
    assert out["timed_out"] is True, out
    assert out["error"]["type"] == "timeout", out


async def test_command_has_no_sync_entry():
    """Синхронного входа у тула больше НЕТ — форма одна.

    Он был вторым способом позвать то же самое и держался ради `apply_tool`,
    которого тоже не стало: звать через него было некого.
    """
    assert not callable(_cmd()), "у exec_command снова появился синхронный вход"


# --- exec_python -----------------------------------------------------------


async def test_python_acall_runs_and_returns_output():
    out = json.loads(await _py().acall(code="print('привет из питона')"))

    assert out["return_code"] == 0, out
    assert "привет из питона" in out["stdout"]


async def test_python_does_not_hold_the_loop():
    _, gap = await _worst_gap(
        lambda: _py().acall(code=f"import time; time.sleep({NAP})")
    )

    assert gap < NAP / 2, f"loop залипал на {gap:.3f}с — запуск не асинхронный"


async def test_python_has_no_sync_entry():
    """См. соседний тест: у формы вызова один вход."""
    assert not callable(_py()), "у exec_python снова появился синхронный вход"


# --- гвард с блокирующим телом --------------------------------------------


async def test_moderation_body_still_runs_off_the_loop():
    """Тело модерации остаётся в потоке — внутри портальный LLM-судья.

    Это и есть причина, по которой ``dual_wrap`` получил ``body_may_block``.
    Исполни его тело на loop — и портал поднял бы
    ``SyncApiInsideEventLoopError`` на дефолтном конфиге, где судья включён.
    """
    import threading

    from gc_core.security.dual_wrapper import dual_wrap

    seen = {}

    def _flow(*args, **kwargs):
        seen["thread"] = threading.get_ident()
        return (yield)

    async def target():
        return "ok"

    wrapped = dual_wrap(target, _flow, body_may_block=True)

    assert await wrapped() == "ok"
    assert seen["thread"] != threading.get_ident(), (
        "тело блокирующего гварда исполнилось на loop — портальный судья там упадёт"
    )

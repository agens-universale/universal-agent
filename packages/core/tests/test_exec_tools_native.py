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
— зовёт LLM-судью. Раньше судья шёл ЧЕРЕЗ ПОРТАЛ (``summarize/llm.py``), а
портал из-под живого event loop поднимал ``SyncApiInsideEventLoopError``.

Пока тул целиком уезжал в поток, живого loop под судьёй не было и портал
работал. Наивный ``acall`` исполнил бы тело гварда на loop — и ``exec_command``
падал бы на ДЕФОЛТНОМ конфиге: ``CommandModerationConfig`` это
``enabled=True, mode="ask"`` (secure-by-default), то есть судья активен из
коробки.

Поэтому у ``dual_wrap`` появился флаг ``body_may_block``: тело, которому
нужен поток, туда и уезжает, а сам запуск процесса всё равно идёт нативно,
потому что гвард ждёт тул через ``await``. Остальные гварды — чистый CPU на
десятки микросекунд — как и раньше, инлайн.

Тело модерации этим флагом БОЛЬШЕ НЕ ПОЛЬЗУЕТСЯ. Оно идёт инлайн и просит
блокирующие куски у драйвера поштучно: судью — корутиной на loop вызывающего,
ожидание человека — в потоке. Флаг остался для тел, у которых блокирующее
неотделимо; его и стережёт тест ниже.
"""
import asyncio
import json
import sys
import time

import pytest

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_core.tools.exec.run_python_code import RunPythonCode
from gc_testkit.helpers.crossplatform import shell_command
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


def _sleep_command(sec) -> str:
    """Команда «поспать sec секунд» в синтаксисе оболочки ОС: `python3` и
    одинарных кавычек в cmd.exe нет."""
    return shell_command([sys.executable, "-c", f"import time; time.sleep({sec})"])


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

    from gc_testkit import quiet_gc_for_loop_timing

    # Сборщик глушится на окне: порог абсолютный (NAP / 2), а пауза поколения 2
    # на куче полного набора — 180–220 мс, и тест судил бы её, а не запуск.
    # Пульс свой, но прогретый: 50 мс сна до работы дают ему взять отметку
    # раньше её начала.
    with quiet_gc_for_loop_timing():
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
    assert "привет" in out["stdout"]["content"]
    assert out["exit_code"] == 0


async def test_command_does_not_hold_the_loop():
    """Пока команда идёт, loop продолжает тикать."""
    _, gap = await _worst_gap(
        lambda: _cmd().acall(command=_sleep_command(NAP))
    )

    assert gap < NAP / 2, f"loop залипал на {gap:.3f}с — запуск не асинхронный"


async def test_command_does_not_hold_a_pool_slot_for_its_whole_duration():
    """Слот пула не занят на всё время команды — вот главный выигрыш.

    Раньше тул уезжал в пул ЦЕЛИКОМ, и слот держался весь запуск (по
    умолчанию до 120 с). Проверяем именно это: забиваем пул надолго и запускаем
    команду сопоставимой длительности. Если бы она шла через пул, ей пришлось
    бы сперва дождаться слота — то есть суммарно около двух длительностей.

    Порог — не «мгновенно», а «заметно меньше двух длительностей»: на пути
    есть ещё гварды, и хотя тело модерации теперь идёт инлайн, ожидание
    человека в нём по-прежнему умеет уходить в свой поток.
    """
    hog_sec = 1.2
    cmd_sec = 1.0
    hogs = [
        asyncio.create_task(asyncio.to_thread(time.sleep, hog_sec)) for _ in range(24)
    ]
    await asyncio.sleep(0.05)

    started = time.perf_counter()
    out = json.loads(
        await _cmd().acall(command=_sleep_command(cmd_sec))
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
            command=_sleep_command(5), timeout=1
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
    assert "привет из питона" in out["stdout"]["content"]


async def test_python_does_not_hold_the_loop():
    _, gap = await _worst_gap(
        lambda: _py().acall(code=f"import time; time.sleep({NAP})")
    )

    assert gap < NAP / 2, f"loop залипал на {gap:.3f}с — запуск не асинхронный"


async def test_python_has_no_sync_entry():
    """См. соседний тест: у формы вызова один вход."""
    assert not callable(_py()), "у exec_python снова появился синхронный вход"


# --- гвард с блокирующим телом --------------------------------------------


async def test_a_blocking_body_still_runs_off_the_loop():
    """``body_may_block`` работает: такое тело исполняется НЕ на loop.

    Флаг завёлся ради модерации — её судья ходил в сеть синхронно, и на loop
    это заморозило бы приложение. Сама модерация им больше не пользуется:
    тело идёт инлайн и просит блокирующие куски у драйвера поштучно. Но
    механизм остаётся для тел, у которых блокирующее неотделимо, и стеречь
    его надо здесь — иначе он тихо перестанет работать.
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
        "тело с body_may_block исполнилось на loop — флаг перестал работать"
    )

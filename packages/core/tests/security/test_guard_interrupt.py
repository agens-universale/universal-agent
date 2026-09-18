"""Ctrl+C во время ожидания человека — отказ отвечать, а не падение прогона.

Тело гарда исполняется в рабочем потоке, а сигналы CPython доставляет только в
главный. Поэтому `except KeyboardInterrupt` внутри `prompt_user_confirmation_choice`
— мёртвая ветка: поток её не увидит никогда. Пока её не было видно, Ctrl+C на
вопросе `y/n` попадал в обработчик asyncio, тот снимал главную задачу, и прогон
рушился целиком. На develop это работало правильно, потому что подтверждение
шло инлайн на главном потоке.

Перехват сделан УЗКИМ намеренно: он включается только там, где вызывающий сам
сказал `on_interrupt=...`, то есть на шести местах ожидания человека. Ожидание
LLM-судьи (`command_moderation`) флага не несёт — там Ctrl+C обязан по-прежнему
прерывать прогон, потому что означает «останови всё», а не «я не разрешаю».
"""
import asyncio
import os
import signal
import time

import pytest

from gc_core.security.dual_wrapper import OffLoop, dual_wrap

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGINT"), reason="платформа без SIGINT"
)


def _blocking(seconds: float) -> str:
    """Человек, который думает дольше, чем длится тест."""
    time.sleep(seconds)
    return "человек ответил"


async def _fire_sigint(after: float = 0.2) -> None:
    await asyncio.sleep(after)
    os.kill(os.getpid(), signal.SIGINT)


def _wrapped(on_interrupt, seen: list, *, thinks: float = 5.0):
    """Тул с телом-гардом, которое ждёт человека и записывает, что получило."""

    async def tool() -> str:
        return "тул отработал"

    def flow():
        request = (
            OffLoop(_blocking, thinks)
            if on_interrupt is _ABSENT
            else OffLoop(_blocking, thinks, on_interrupt=on_interrupt)
        )
        seen.append((yield request))
        return (yield)

    return dual_wrap(tool, flow)


_ABSENT = object()


@pytest.fixture
def safety_net():
    """Обработчик «на всякий случай» вместо обработчика asyncio.

    Без него регрессия перехвата не краснит тест, а снимает задачу — и
    `KeyboardInterrupt` обрывает ВЕСЬ прогон pytest, что в CI выглядит как сбой
    инфраструктуры. Со страховкой сигнал, прошедший мимо перехвата, просто
    записывается, и тест падает на своём ассерте.
    """
    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        yield caught
    finally:
        signal.signal(signal.SIGINT, previous)


async def test_ctrl_c_during_a_human_wait_returns_the_declared_value(safety_net):
    """Тело получает объявленное значение, а прогон продолжается."""
    seen: list = []
    call = _wrapped(False, seen)

    fire = asyncio.ensure_future(_fire_sigint())
    result = await asyncio.wait_for(call(), 5)
    await fire

    assert safety_net == [], "сигнал ушёл мимо перехвата — в жизни это снятый прогон"
    assert seen == [False], f"телу гарда пришло не то: {seen}"
    assert result == "тул отработал", "прогон не продолжился после Ctrl+C"


async def test_the_interceptor_restores_the_previous_handler(safety_net):
    """Перехват живёт только на время ожидания: дальше Ctrl+C снова общий."""
    before = signal.getsignal(signal.SIGINT)
    seen: list = []
    call = _wrapped(False, seen)

    fire = asyncio.ensure_future(_fire_sigint())
    await asyncio.wait_for(call(), 5)
    await fire

    assert signal.getsignal(signal.SIGINT) is before, (
        "обработчик не восстановлен — Ctrl+C вне вопроса перестанет работать"
    )


async def test_overlapping_waits_keep_ctrl_c_for_the_one_still_waiting(safety_net):
    """Ожидания выходят не в порядке входа — Ctrl+C всё равно у того, кто ждёт.

    Каждое ожидание запоминало обработчик на входе и возвращало его на выходе.
    A вошло первым и вышло первым — и вернуло обработчик, стоявший ДО обоих, пока
    B ещё ждал человека: Ctrl+C снимал прогон. Бывает там, где на одном loop
    несколько агентов: SDK, сервер.
    """
    seen_a: list = []
    seen_b: list = []
    first = asyncio.ensure_future(_wrapped("A", seen_a, thinks=0.3)())
    second = asyncio.ensure_future(_wrapped("B", seen_b, thinks=5.0)())

    assert await asyncio.wait_for(first, 5) == "тул отработал"
    assert seen_a == ["человек ответил"], "первое ожидание не дождалось ответа"
    os.kill(os.getpid(), signal.SIGINT)

    assert await asyncio.wait_for(second, 3) == "тул отработал"
    assert seen_b == ["B"], f"Ctrl+C не дошёл до того, кто ещё ждёт: {seen_b}"
    assert safety_net == [], "сигнал ушёл мимо перехвата — в жизни это снятый прогон"


# --- проводка флага во всех шести местах ожидания человека ------------------
#
# Перехват включает сам гард, передавая `on_interrupt` в `OffLoop`. Снятие флага
# хотя бы в одном месте набор не замечал: тесты выше гоняют свой `OffLoop`, а не
# настоящие гарды. Здесь — настоящие, каждый доведён до вопроса человеку.

#: Сколько «думает» человек. Ctrl+C приходит раньше, и гард обязан ответить
#: отказом сразу, а не дождавшись ответа.
_HUMAN_THINKS = 5.0


def _slow_human(**_kw) -> str:
    time.sleep(_HUMAN_THINKS)
    return "allow"


def _ctx(config: dict):
    from types import SimpleNamespace

    from gc_core.security.permission_store import PermissionStore

    return SimpleNamespace(
        is_tui=True, tui_confirm=_slow_human, config_dict=config,
        permission_store=PermissionStore(),
    )


class _Blocked:
    """Отказ гарда — JSON с ok=False, под каким бы именем гард его ни искал."""

    def _blocked(self, err, *_a, **_kw) -> str:
        import json

        return json.dumps({"ok": False, "error": err})

    _write_guard_blocked_result = _blocked
    _read_guard_blocked_result = _blocked
    _url_policy_blocked_result = _blocked
    _command_moderation_blocked_result = _blocked


def _write_to_instructions(tmp_path):
    from gc_core.security.path_policy import enforce_write_guard

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("правила проекта", encoding="utf-8")

    class _Tool(_Blocked):
        _ctx = _ctx({"launch_root": str(project)})

        @enforce_write_guard(path_arg="path")
        async def write(self, path):
            return '{"ok": true}'

    return lambda: _Tool().write(path=str(project / "AGENTS.md"))


def _write_outside_the_project(tmp_path):
    from gc_core.security.path_policy import enforce_write_guard

    project = tmp_path / "project"
    project.mkdir()

    class _Tool(_Blocked):
        _ctx = _ctx({
            "launch_root": str(project),
            "write_guard": {"enabled": True, "mode": "ask"},
        })

        @enforce_write_guard(path_arg="path")
        async def write(self, path):
            return '{"ok": true}'

    return lambda: _Tool().write(path=str(tmp_path / "outside.txt"))


def _read_outside_the_project(tmp_path):
    from gc_core.security.read_guard import enforce_read_guard

    project = tmp_path / "project"
    project.mkdir()

    class _Tool(_Blocked):
        _ctx = _ctx({
            "launch_root": str(project),
            "read_guard": {
                "enabled": True, "mode": "ask",
                "allowed_read_roots": [str(project)],
            },
        })

        @enforce_read_guard(path_arg="path")
        async def read(self, path):
            return '{"ok": true}'

    return lambda: _Tool().read(path=str(tmp_path / "secret.txt"))


def _fetch_a_listed_url(tmp_path):
    from gc_core.security.url_policy import enforce_url_policy

    class _Tool(_Blocked):
        _ctx = _ctx({
            "launch_root": str(tmp_path),
            "url_policy": {
                "enabled": True, "mode": "ask",
                "allowed_schemes": ["https"], "url_blacklist": ["example.com"],
            },
        })

        @enforce_url_policy(url_arg="url")
        async def fetch(self, url):
            return '{"ok": true}'

    return lambda: _Tool().fetch(url="https://example.com/page")


def _run_without_a_judge(tmp_path):
    from gc_core.security.command_moderation import moderate_command_execution

    class _Tool(_Blocked):
        _ctx = _ctx({
            "launch_root": str(tmp_path),
            "command_moderation": {"enabled": True, "mode": "ask"},
        })

        @moderate_command_execution(
            payload_arg="command", tool_name="execute_command",
            judge_client_extractor=lambda self, *a, **kw: None,
        )
        async def run(self, command):
            return '{"ok": true}'

    return lambda: _Tool().run(command="rm -rf /tmp/test")


def _run_touching_instructions(tmp_path):
    from gc_core.security.command_guard import enforce_command_guard

    agents = tmp_path / "AGENTS.md"
    agents.write_text("правила проекта", encoding="utf-8")

    class _Tool(_Blocked):
        _ctx = _ctx({"launch_root": str(tmp_path), "gc_protection": {"enabled": True}})

        @enforce_command_guard(payload_arg="command", tool_name="execute_command")
        async def run(self, command):
            return '{"ok": true}'

    return lambda: _Tool().run(command=f"cat {agents}")


@pytest.mark.parametrize("make_call", [
    _write_to_instructions,
    _write_outside_the_project,
    _read_outside_the_project,
    _fetch_a_listed_url,
    _run_without_a_judge,
    _run_touching_instructions,
], ids=lambda f: f.__name__.strip("_"))
async def test_every_human_wait_turns_ctrl_c_into_a_refusal(
    make_call, tmp_path, monkeypatch, safety_net,
):
    """Ctrl+C на вопросе любого гарда — отказ сразу, а не снятый прогон.

    Ломается, если в одном из мест ожидания снять `on_interrupt`: сигнал уйдёт
    мимо перехвата (в жизни — снятый прогон), а гард будет ждать человека.
    """
    import json

    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    call = make_call(tmp_path)

    fire = asyncio.ensure_future(_fire_sigint(0.3))
    result = await asyncio.wait_for(call(), _HUMAN_THINKS - 1)
    await fire

    assert safety_net == [], "Ctrl+C ушёл мимо перехвата — в жизни это снятый прогон"
    assert json.loads(result)["ok"] is False, f"гард пропустил без подтверждения: {result}"


async def test_without_the_flag_the_wait_is_not_intercepted():
    """Без признака ожидание не перехватывается — это путь LLM-судьи.

    Проверяем не отмену (в pytest сигнал уходит чужому обработчику и тест бы
    рухнул не там), а факт: тело дождалось НАСТОЯЩЕГО результата, то есть
    перехват не сработал там, где его не просили.
    """
    seen: list = []
    call = _wrapped(_ABSENT, seen)

    caught: list = []
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: caught.append(True))
    try:
        fire = asyncio.ensure_future(_fire_sigint(0.1))
        result = await asyncio.wait_for(call(), 10)
        await fire
    finally:
        signal.signal(signal.SIGINT, previous)

    assert caught, "сигнал не дошёл — тест ничего не проверил"
    assert seen == ["человек ответил"], (
        f"ожидание перехватили там, где не просили: {seen}"
    )
    assert result == "тул отработал"

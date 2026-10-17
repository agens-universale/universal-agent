"""Общий шаг проверки судьёй: вердикт, сбой, предохранитель, остановка."""

import json
from types import SimpleNamespace

import pytest
from gc_testkit.settings import fake_ctx

from gc_core.execution.state import SharedStateData
from gc_core.security import action_judge_step as step_module
from gc_core.security.action_judge_step import (
    CHECK_FAILED_TYPE,
    DENIALS_IN_A_ROW,
    DENIALS_TOTAL,
    FAILURES_BEFORE_STOP,
    judged_step,
)
from gc_core.security.call_scope import call_scope
from gc_core.security.dual_wrapper import AwaitOnCaller, _drive_async
from gc_core.message_policy.permission_denials import ACTION_BLOCKED_TYPE
from gc_core.security.permission_store import PermissionStore

pytestmark = pytest.mark.asyncio

ALLOW = {
    "reason": "",
    "crosses_boundary": False,
    "risky": False,
    "authorized": False,
}
BLOCK = {
    "reason": "удаление не просили",
    "crosses_boundary": False,
    "risky": True,
    "authorized": False,
}


def _state():
    return SimpleNamespace(
        messages=[{"role": "user", "content": "почини тесты"}],
        user_intent_log=[],
        current_user_intent=[],
        shared=SharedStateData(),
        parent_state_id=None,
        judge_failures_in_a_row=0,
        judge_stop_reason=None,
    )


class _Judge:
    """Подмена вызова модели: по ответу на вызов."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, ctx, state, messages):
        self.requests.append(messages)
        reply = (
            self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        )
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


@pytest.fixture
def env(monkeypatch):
    def make(*replies, human=None, channel=True):
        judge = _Judge(*replies)
        monkeypatch.setattr(step_module, "_arequest_verdict_text", judge)
        monkeypatch.setattr(
            step_module, "judge_channel_configured", lambda ctx: channel
        )
        monkeypatch.setattr(
            step_module, "confirm_available", lambda ctx: human is not None
        )
        asked = []

        def fake_confirmation(**kwargs):
            asked.append(kwargs)

            async def answer():
                return human

            return AwaitOnCaller(answer)

        monkeypatch.setattr(
            step_module, "confirmation_step", fake_confirmation
        )
        store = PermissionStore()
        ctx = fake_ctx(
            permission_store=store,
            tools_data={"exec_command": {"category": "exec"}},
            is_tui=human is not None,
        )
        return SimpleNamespace(
            judge=judge, asked=asked, ctx=ctx, store=store, state=_state()
        )

    return make


async def _run(e, *, flags=(), key="command:git push", arguments=None):
    context = SimpleNamespace(ctx=e.ctx, logger=None, launch_root="/p")
    _, result = await _drive_async(
        judged_step(
            context=context,
            tool_name="exec_command",
            category="exec",
            decision_key=key,
            flags=flags,
            question=(
                "Выполнение команды",
                "Разрешить запуск?",
                [("Команда", "git push")],
            ),
        )
    )
    return result


async def _call(e, **kwargs):
    with call_scope(
        state=e.state, call_id="c1", tool_name="exec_command"
    ) as scope:
        scope.arguments = kwargs.pop("arguments", {"command": "git push"})
        return await _run(e, **kwargs)


async def test_allowed_action_runs_without_a_question(env):
    e = env(ALLOW, human=True)
    assert await _call(e) == (True, None)
    assert e.asked == []
    assert len(e.judge.requests) == 1


async def test_blocked_action_carries_the_judges_reason(env):
    e = env(BLOCK)
    allowed, error = await _call(e)
    assert allowed is False
    assert error["type"] == ACTION_BLOCKED_TYPE
    assert error["reason"] == "удаление не просили"
    assert "safer" in error["guidance"]
    assert e.state.shared.judge_denials_in_a_row == 1
    assert e.state.shared.judge_denials_total == 1


async def test_the_judge_sees_the_prepared_arguments_and_the_flags(env):
    e = env(ALLOW)
    await _call(
        e, flags=("outside_project",), arguments={"command": "rm -rf ./build"}
    )
    user_message = e.judge.requests[0][-1]["content"]
    assert '"command": "rm -rf ./build"' in user_message
    assert '"outside_project"' in user_message


async def test_remembered_human_allow_skips_the_judge(env):
    e = env(BLOCK)
    e.store.record("command:git push", True, session=True)
    assert await _call(e) == (True, None)
    assert e.judge.requests == []


async def test_one_check_per_call_unless_a_new_flag_appears(env):
    """Пол передал пометку — стадия того же вызова судью не зовёт; новая
    пометка, которой судья не видел, — зовёт."""
    e = env(ALLOW)
    with call_scope(
        state=e.state, call_id="c1", tool_name="download_file"
    ) as scope:
        scope.arguments = {"url": "https://x", "dest": "/out/a.bin"}
        assert await _run(e, key="url:https://x") == (True, None)
        assert await _run(e, key="write:/p/a.bin") == (True, None)
        assert len(e.judge.requests) == 1
        assert await _run(
            e, key="write:/out/a.bin", flags=("outside_project",)
        ) == (True, None)
        assert len(e.judge.requests) == 2
    # следующий вызов тула проверяется заново
    assert await _call(e) == (True, None)
    assert len(e.judge.requests) == 3


async def test_failed_check_says_nothing_about_the_action(env):
    e = env(RuntimeError("timeout"))
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == CHECK_FAILED_TYPE
    assert "nothing about the action" in error["reason"]
    assert len(e.judge.requests) == 2  # один повтор
    assert e.state.judge_failures_in_a_row == 1
    assert e.state.shared.judge_denials_total == 0  # сбой — не отказ


async def test_unparsable_reply_is_a_failed_check_and_a_retry_can_save_it(env):
    e = env("I think it is fine", ALLOW)
    assert await _call(e) == (True, None)
    assert e.state.judge_failures_in_a_row == 0
    e = env("I think it is fine")
    _, error = await _call(e)
    assert error["type"] == CHECK_FAILED_TYPE


async def test_several_json_blocks_are_a_failed_check_not_an_allow(env):
    raw = (
        "Example:\n```json\n" + json.dumps(ALLOW) + "\n```\n"
        "Verdict:\n```json\n" + json.dumps(BLOCK) + "\n```"
    )
    e = env(raw)
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == CHECK_FAILED_TYPE
    assert len(e.judge.requests) == 2  # свой один повтор
    assert e.state.judge_failures_in_a_row == 1


async def test_three_denials_in_a_row_pause_the_judge_and_ask_the_human(env):
    """Как у эталона: после порога auto приостановлен — спорное действие
    спрашивается обычным вопросом, одобрение возвращает auto."""
    e = env(BLOCK, human=True)
    for _ in range(DENIALS_IN_A_ROW):
        assert (await _call(e))[0] is False
    assert e.asked == [] and e.state.shared.judge_paused is True
    asked_before = len(e.judge.requests)
    assert await _call(e) == (True, None)  # человек ответил «да»
    # обычный вопрос: с ключом решения, ответ можно запомнить; последнее поле
    # объясняет, почему auto вдруг спрашивает
    assert (
        len(e.asked) == 1 and e.asked[0]["decision_key"] == "command:git push"
    )
    assert e.asked[0]["fields"] == [
        ("Команда", "git push"),
        (
            "Почему вопрос",
            "проверка действий в режиме auto приостановлена после повторных отказов",
        ),
    ]
    assert (
        len(e.judge.requests) == asked_before
    )  # судья на этот раз не зовётся
    assert e.state.shared.judge_paused is False  # автономность возвращена
    assert e.state.shared.judge_denials_in_a_row == 0
    assert e.state.shared.judge_denials_total == DENIALS_IN_A_ROW


async def test_twenty_denials_in_a_session_pause_even_with_allowed_actions_between(
    env,
):
    """Агент перемежает отказы одобренными проверками — серия каждый раз
    обнуляется, но общий счёт сессии всё равно доходит до порога и
    сбрасывается, только когда сработал сам."""
    e = env(BLOCK, ALLOW, human=True)
    shared = e.state.shared
    shared.judge_denials_total = DENIALS_TOTAL - 1
    e.judge.replies = [ALLOW, BLOCK]
    assert (await _call(e))[0] is True
    assert shared.judge_paused is False
    assert (await _call(e))[0] is False
    assert shared.judge_paused is True
    assert shared.judge_denials_in_a_row == 1
    assert shared.judge_denials_total == 0


async def test_the_denial_that_pauses_tells_the_agent_to_stop_only_without_a_human(
    env,
):
    """С человеком пауза превращает следующую попытку в вопрос ему: агенту
    нельзя велеть «прекрати пробовать» — тогда вопрос не дойдёт. Без человека
    — нужно: попыток у него больше нет."""
    for human, stop in ((True, False), (None, True)):
        e = env(BLOCK, human=human)
        for _ in range(DENIALS_IN_A_ROW):
            _, error = await _call(e)
        assert e.state.shared.judge_paused is True
        assert ("Stop trying variations" in error["guidance"]) is stop


async def test_human_no_keeps_the_pause(env):
    e = env(BLOCK, human=False)
    e.state.shared.judge_paused = True
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == "declined_by_user"
    assert e.state.shared.judge_paused is True
    assert e.judge.requests == []


async def test_without_a_human_a_paused_judge_refuses_without_being_called(
    env,
):
    """Как у эталона без человека: спрашивать некого — действие не
    выполняется, агент работает дальше. Судья не зовётся: у агента, упёршегося
    в проверку, больше нет попыток подобрать проходную формулировку."""
    e = env(ALLOW, human=None)
    e.state.shared.judge_paused = True
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == ACTION_BLOCKED_TYPE
    assert error["reason"] == (
        "Проверка действий в режиме auto приостановлена после повторных отказов; "
        "нужно решение человека."
    )
    assert "Stop trying variations" in error["guidance"]
    assert e.judge.requests == [] and e.asked == []
    assert e.state.shared.judge_paused is True  # снимает только человек
    assert e.state.judge_stop_reason is None  # прогон не останавливается


async def test_an_allowed_action_resets_the_streak_but_not_the_session_total(
    env,
):
    e = env(BLOCK, BLOCK, ALLOW)
    await _call(e)
    await _call(e)
    assert e.state.shared.judge_denials_in_a_row == 2
    await _call(e)
    assert e.state.shared.judge_denials_in_a_row == 0
    assert e.state.shared.judge_denials_total == 2


async def test_denials_of_all_agents_count_toward_one_session_pause(env):
    """Субагент держит общий с родителем объект сессии: отказы ребёнка и
    родителя — один счёт, пауза — одна."""
    e = env(BLOCK, human=True)
    child = _state()
    child.shared = e.state.shared
    await _call(e)
    parent_state = e.state
    e.state = child
    await _call(e)
    await _call(e)
    assert parent_state.shared.judge_paused is True
    e.state = parent_state
    assert await _call(e) == (True, None)
    assert len(e.asked) == 1


async def test_ten_failed_checks_in_a_row_stop_the_run(env):
    e = env(RuntimeError("down"))
    for _ in range(FAILURES_BEFORE_STOP - 1):
        await _call(e)
    assert e.state.judge_stop_reason is None
    await _call(e)
    assert e.state.judge_stop_reason == (
        "Автоматическая проверка действий недоступна: 10 проверок подряд "
        "не состоялись. Прогон остановлен — повторите позже или смените "
        "режим разрешений."
    )


async def test_a_completed_check_resets_the_failure_streak(env):
    e = env(RuntimeError("down"), RuntimeError("down"), BLOCK)
    await _call(e)
    assert e.state.judge_failures_in_a_row == 1
    await _call(e)
    assert e.state.judge_failures_in_a_row == 0


async def test_no_judge_channel_stops_the_run_on_the_first_disputed_action(
    env,
):
    e = env(ALLOW, channel=False)
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == CHECK_FAILED_TYPE
    assert e.judge.requests == []
    assert e.state.judge_stop_reason == (
        "Режим разрешений auto недоступен в этой конфигурации: нет канала "
        "вспомогательной модели для проверки действий. Прогон остановлен — "
        "смените режим разрешений."
    )


async def test_a_call_outside_the_dispatcher_is_a_failed_check_not_a_pass(env):
    e = env(ALLOW)
    allowed, error = await _run(e)
    assert allowed is False and error["type"] == CHECK_FAILED_TYPE
    assert e.judge.requests == []


async def test_strict_flag_blocks_even_when_the_model_calls_it_harmless(env):
    e = env(ALLOW)
    allowed, error = await _call(e, flags=("restricted_address",))
    assert allowed is False and error["type"] == ACTION_BLOCKED_TYPE


async def test_durable_run_takes_the_instruction_from_the_worker_probe(env):
    """Под durable-исполнением ядру нельзя читать файлы: текст инструкции
    судьи приходит пробой с воркера, как у судьи команд."""
    from gc_core.security.guard_probe import evaluate_guard_probe

    e = env(ALLOW)
    probed = []

    async def guard_probe(request):
        probed.append(request.kind)
        return evaluate_guard_probe(request, settings=e.ctx)

    e.ctx.guard_probe = guard_probe
    assert await _call(e) == (True, None)
    assert probed == ["action_prompts"]
    system, user = e.judge.requests[0]
    assert "crosses_boundary" in system["content"]
    assert '"command": "git push"' in user["content"]
    assert '"os"' not in user["content"]  # факты хоста ядро само не читает


async def test_failed_probe_is_a_failed_check(env):
    e = env(ALLOW)

    async def guard_probe(request):
        raise RuntimeError("worker is gone")

    e.ctx.guard_probe = guard_probe
    allowed, error = await _call(e)
    assert allowed is False and error["type"] == CHECK_FAILED_TYPE
    assert e.judge.requests == []


async def test_human_yes_on_the_pause_covers_the_rest_of_this_call(env):
    e = env(BLOCK, human=True)
    e.state.shared.judge_paused = True
    with call_scope(
        state=e.state, call_id="c1", tool_name="exec_command"
    ) as scope:
        scope.arguments = {"command": "git push"}
        assert await _run(e) == (True, None)
        assert await _run(e) == (True, None)  # вторая стадия того же вызова
        assert len(e.asked) == 1 and e.judge.requests == []
    # пауза снята: следующий вызов снова идёт к судье
    assert (await _call(e))[0] is False
    assert len(e.judge.requests) == 1


async def test_execution_failure_is_not_turned_into_a_failed_check(env):
    """Отказ исполнения — не обратная связь модели: его разбирает цикл
    агента, шаг проверки его не глотает и не повторяет."""
    from gc_core.execution.contracts import InfrastructureFailure

    e = env(InfrastructureFailure("transport is gone"))
    with pytest.raises(InfrastructureFailure):
        await _call(e)
    assert len(e.judge.requests) == 1  # без повтора
    assert e.state.judge_failures_in_a_row == 0


async def test_stop_during_the_judge_request_closes_the_call_as_stopped(env):
    """Стоп хода во время запроса к судье (durable отменяет ожидание модели):
    вызов получает тело отказа-остановки, а не «внутреннюю ошибку тула»; это
    не сбой проверки и не отказ судьи, повтора нет."""
    from gc_core.execution.contracts import ModelRequestStopped

    e = env(ModelRequestStopped("Ход остановлен"))
    allowed, error = await _call(e)
    assert allowed is False
    assert error == {
        "type": "cancelled_by_user",
        "reason": "Tool was interrupted by user stop.",
        "retryable": True,
    }
    assert len(e.judge.requests) == 1
    assert e.state.judge_failures_in_a_row == 0
    assert e.state.judge_stop_reason is None
    assert e.state.shared.judge_denials_in_a_row == 0


async def test_runtime_facts_are_gathered_only_when_the_judge_is_asked(env):
    e = env(ALLOW)
    e.store.record("command:git push", True, session=True)
    gathered = []

    def runtime():
        gathered.append(1)
        return {"denied_command_examples": ["npm publish"]}

    context = SimpleNamespace(ctx=e.ctx, logger=None, launch_root="/p")

    async def run(key):
        with call_scope(
            state=e.state, call_id="c1", tool_name="exec_command"
        ) as scope:
            scope.arguments = {"command": "x"}
            return (
                await _drive_async(
                    judged_step(
                        context=context,
                        tool_name="exec_command",
                        category="exec",
                        decision_key=key,
                        question=("t", "q", []),
                        runtime=runtime,
                    )
                )
            )[1]

    assert await run("command:git push") == (True, None)  # человек уже решил
    assert gathered == []
    assert await run("command:other") == (True, None)
    assert gathered == [1]
    assert '"npm publish"' in e.judge.requests[0][-1]["content"]


async def test_audit_carries_the_judges_words_and_the_call(env):
    e = env(BLOCK)
    events = []

    class _Logger:
        def info(self, message, *args):
            text = message % args if args else message
            events.append(json.loads(text.split(" ", 1)[1]))

    context = SimpleNamespace(ctx=e.ctx, logger=_Logger(), launch_root="/p")
    with call_scope(
        state=e.state, call_id="call_42", tool_name="exec_command"
    ) as scope:
        scope.arguments = {"command": "git push"}
        await _drive_async(
            judged_step(
                context=context,
                tool_name="exec_command",
                category="exec",
                decision_key="command:git push",
                question=("t", "q", []),
            )
        )
    assert events[-1]["reason_code"] == "action_judge_blocked"
    assert events[-1]["reason"] == "удаление не просили"
    assert events[-1]["call_id"] == "call_42"


async def test_the_step_cuts_history_at_its_own_call_and_knows_the_categories(
    env,
):
    """Шаг передаёт сборке запроса идентификатор своего вызова и набор тулов:
    без первого соседи по пачке выглядели бы выполненными, без второго чтение
    файла попало бы в историю как действие."""
    e = env(ALLOW)
    e.ctx.tools_data = {
        "exec_command": {"category": "exec"},
        "fs_read_file": {"category": "read"},
    }
    e.state.messages = [
        {"role": "user", "content": "разверни"},
        {
            "type": "function_call",
            "name": "fs_read_file",
            "arguments": '{"path": "a"}',
            "call_id": "r1",
        },
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"command": "make build"}',
            "call_id": "b1",
        },
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"command": "git push"}',
            "call_id": "c1",
        },
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"command": "make deploy"}',
            "call_id": "b3",
        },
    ]
    await _call(e)  # привязка вызова несёт call_id="c1"
    payload = json.loads(
        e.judge.requests[0][-1]["content"].split("Context JSON:")[1]
    )
    assert payload["conversation"] == [
        {"user": "разверни"},
        {
            "action": {
                "tool": "exec_command",
                "arguments": {"command": "make build"},
                "outcome": "unknown",
            }
        },
    ]


async def test_channel_predicate_follows_the_run_configuration():
    from gc_core.security.action_judge_step import judge_channel_configured

    assert (
        judge_channel_configured(fake_ctx(model_transport_configured=True))
        is True
    )
    assert (
        judge_channel_configured(fake_ctx(model_transport_configured=False))
        is False
    )


async def test_the_judge_request_has_its_own_budget(monkeypatch):
    """Судья действий сам повторяет и считает сбои: клиенту повторять нельзя,
    предохранитель моделей он не трогает, запрос ограничен своим таймаутом."""
    from gc_core.summarize import llm as summary_llm

    seen = {}

    async def request(**kwargs):
        seen.update(kwargs)
        return "{}"

    monkeypatch.setattr(summary_llm, "arequest_chat_completion_text", request)
    monkeypatch.setattr(
        summary_llm, "log_llm_prompt_and_result", lambda **_kwargs: None
    )
    ctx = fake_ctx(
        logger=None, auxiliary_llm=SimpleNamespace(judge_max_tokens=512)
    )
    out = await step_module._arequest_verdict_text(
        ctx, SimpleNamespace(), [{"role": "user", "content": "q"}]
    )
    assert out == "{}"
    assert step_module.JUDGE_REQUEST_TIMEOUT_SEC == 60
    assert seen["isolated_timeout_sec"] == 60
    assert seen["consumer"] == "command_judge"
    assert seen["max_tokens"] == 512


async def test_ten_failed_checks_cost_at_most_twenty_requests(env):
    """Свой бюджет судьи: одна попытка и один повтор на проверку — до
    остановки уходит не больше 20 запросов."""
    e = env(RuntimeError("down"))
    for _ in range(FAILURES_BEFORE_STOP):
        await _call(e)
    assert e.state.judge_stop_reason is not None
    assert len(e.judge.requests) == 2 * FAILURES_BEFORE_STOP == 20

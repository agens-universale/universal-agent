"""Вопрос без «да» доходит до текста отказа своим исходом: человек отказал,
не ответил вовремя (таймаут, отмена, клиент не умеет отвечать) или спросить
было некого (спека §7.1). Стадия режима проверяется обоими драйверами:
локальным портом (`OffLoop`) и async-портом рантайма (`AwaitOnCaller`)."""

from types import SimpleNamespace

from gc_core.execution.human import HumanRequestInterrupted
from gc_core.message_policy.permission_denials import (
    DECLINED_TYPE,
    RETRY_ON_REQUEST,
    SESSION_DENIED_TYPE,
    TIMED_OUT_TYPE,
    UNATTENDED_TYPE,
    session_denied_error,
    timed_out_error,
)
from gc_core.security.call_scope import call_scope
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
    CONFIRM_OUTCOME_TIMED_OUT,
    CONFIRM_TIMED_OUT,
)
from gc_core.security.dual_wrapper import _drive_async
from gc_core.security.mode_stage import (
    ModeAction,
    remembered_denial,
    resolve_by_mode,
)
from gc_core.security.permission_mode import ASK
from gc_core.security.permission_store import PermissionStore
from gc_core.settings import RuntimeEnv
from gc_testkit.settings import fake_ctx

KEY = "write:/p/a.txt"


def _action(ctx):
    return ModeAction(
        context=SimpleNamespace(ctx=ctx, logger=None),
        tool_name="fs_write_file",
        category="edit",
        decision_key=KEY,
        question=("Запись файла", "Разрешить запись?", [("Путь", "/p/a.txt")]),
        error_fields={"path": "/p/a.txt"},
        deny_type="mode_denied",
        deny_reason="denied",
    )


async def _ask(ctx):
    """Стадия режима с исходом ASK; ``(разрешено, тело отказа)``."""
    with call_scope(state=None, call_id="c1", tool_name="fs_write_file"):
        _, outcome = await _drive_async(resolve_by_mode(ASK, _action(ctx)))
    return outcome


def _local(answer, *, human=True):
    """Канал с синхронным портом подтверждения (ACP, TUI-подобный)."""
    return fake_ctx(
        runtime=RuntimeEnv(human_channel=human),
        confirm_request=lambda request: answer,
        permission_store=PermissionStore(),
    )


def _durable(human_request, *, human=None):
    """Канал с async-портом рантайма (путь Temporal)."""
    return fake_ctx(
        runtime=RuntimeEnv(human_channel=human),
        human_request=human_request,
        permission_store=PermissionStore(),
    )


async def test_human_no_through_the_port_is_declined():
    allowed, error = await _ask(_local(CONFIRM_DENY_ONCE))
    assert allowed is False
    assert error["type"] == DECLINED_TYPE
    assert error["path"] == "/p/a.txt"


async def test_no_for_the_session_says_so_at_once():
    """«Запретить в этой сессии» — сразу отказ на сессию: человек и модель
    узнают, что запрет запомнен, с первого отказа, а не со следующего вызова.
    Повтор того же действия получает тот же отказ — и из вопроса, и из
    запомненного запрета, который сильнее режима."""
    ctx = _local(CONFIRM_DENY_REMEMBER)
    allowed, first = await _ask(ctx)
    assert allowed is False
    assert first == session_denied_error(path="/p/a.txt")
    assert first["reason"] == "Denied by the user for this session."
    # Повтор в этой сессии снова получит отказ: модель не обещает человеку
    # «снять запрет» повтором и не обходит его, а называет то, что его снимает.
    assert RETRY_ON_REQUEST not in first["guidance"]
    assert "new session" in first["guidance"]
    assert "another way" in first["guidance"]
    _, again = await _ask(ctx)
    assert again == first
    remembered = remembered_denial(
        SimpleNamespace(ctx=ctx, logger=None),
        "fs_write_file",
        KEY,
        path="/p/a.txt",
    )
    assert remembered == first


async def test_no_answer_in_time_is_its_own_outcome():
    allowed, error = await _ask(_local(CONFIRM_TIMED_OUT))
    assert allowed is False
    assert error == timed_out_error(path="/p/a.txt")
    assert error["type"] == TIMED_OUT_TYPE
    assert "Do NOT retry" in error["guidance"]


async def test_no_one_to_ask_is_unattended_without_a_question():
    ctx = fake_ctx(
        runtime=RuntimeEnv(human_channel=False),
        permission_store=PermissionStore(),
    )
    allowed, error = await _ask(ctx)
    assert allowed is False
    assert error["type"] == UNATTENDED_TYPE


async def test_durable_port_reports_the_same_outcomes():
    async def says_no(_request):
        return CONFIRM_DENY_ONCE

    async def says_no_for_the_session(_request):
        return CONFIRM_DENY_REMEMBER

    async def interrupted(_request):
        raise HumanRequestInterrupted("stopped")

    _, declined = await _ask(_durable(says_no))
    assert declined["type"] == DECLINED_TYPE
    _, denied = await _ask(_durable(says_no_for_the_session))
    assert denied["type"] == SESSION_DENIED_TYPE
    _, timed_out = await _ask(_durable(interrupted))
    assert timed_out["type"] == TIMED_OUT_TYPE
    # Воркер без человека объявил канал: тот же обрыв — «спросить некого».
    _, unattended = await _ask(_durable(interrupted, human=False))
    assert unattended["type"] == UNATTENDED_TYPE


async def test_a_later_yes_in_the_same_call_clears_the_outcome():
    answers = iter([CONFIRM_TIMED_OUT, CONFIRM_ALLOW_ONCE])
    ctx = fake_ctx(
        runtime=RuntimeEnv(human_channel=True),
        confirm_request=lambda request: next(answers),
        permission_store=PermissionStore(),
    )
    with call_scope(
        state=None, call_id="c1", tool_name="fs_write_file"
    ) as scope:
        _, first = await _drive_async(resolve_by_mode(ASK, _action(ctx)))
        assert scope.confirm_outcome == CONFIRM_OUTCOME_TIMED_OUT
        _, second = await _drive_async(resolve_by_mode(ASK, _action(ctx)))
        assert scope.confirm_outcome is None
    assert first[1]["type"] == TIMED_OUT_TYPE
    assert second == (True, None)

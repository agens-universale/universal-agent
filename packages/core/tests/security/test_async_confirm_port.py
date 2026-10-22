"""Native human replies preserve the local permission reducer."""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core.execution.human import HumanRequest
from gc_core.security.confirm_port import aconfirm_via_human_port
from gc_core.security.permission_store import PermissionStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "choice,allow,session",
    [
        ("allow_remember", True, True),
        ("allow_once", True, False),
        ("deny_remember", False, True),
        ("deny_once", False, False),
        ("invalid", False, False),  # непонятный ответ — запрет разовый
    ],
)
async def test_async_choice_and_cached_reply(choice, allow, session):
    requests = []

    async def human(request):
        requests.append(request)
        return choice

    store = PermissionStore()
    ctx = SimpleNamespace(human_request=human, permission_store=store)
    request = HumanRequest(
        kind="confirm", question="q", decision_key="write:/x"
    )
    assert await aconfirm_via_human_port(request, ctx=ctx) is allow
    assert await aconfirm_via_human_port(request, ctx=ctx) is allow
    assert requests == [request]
    assert store.session_rules() == ({"write:/x": allow} if session else {})


@pytest.mark.asyncio
async def test_cancelled_human_does_not_record_permission():
    entered = asyncio.Event()

    async def human(_request):
        entered.set()
        await asyncio.Future()

    store = PermissionStore()
    ctx = SimpleNamespace(human_request=human, permission_store=store)
    task = asyncio.create_task(
        aconfirm_via_human_port(
            HumanRequest(
                kind="confirm", question="q", decision_key="write:/x"
            ),
            ctx=ctx,
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.decision_for("write:/x") is None
    assert store.session_rules() == {}


async def test_stopped_human_is_not_a_deny_remember_decision():
    from gc_core.execution.human import HumanRequestInterrupted

    async def human(_request):
        raise HumanRequestInterrupted("stopped")

    store = PermissionStore()
    ctx = SimpleNamespace(human_request=human, permission_store=store)
    assert (
        await aconfirm_via_human_port(
            HumanRequest(
                kind="confirm", question="q", decision_key="write:/x"
            ),
            ctx=ctx,
        )
        is False
    )
    assert store.decision_for("write:/x") is None
    assert store.session_rules() == {}

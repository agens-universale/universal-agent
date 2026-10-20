"""Полный цикл режима plan под Temporal: предложить план, одобрить, исполнить.

Путь целиком идёт кодом workflow: тул плана исполняется в ядре
(`effect_in_core`), смена статуса плана — в начале прогона исполнителя.
Время и id там — только из `execution_environment`: прямое чтение часов
песочница запрещает, и задача workflow повторялась бы без конца.
"""

import asyncio
import json

from gc_core.message_policy.plan_messages import (
    PLAN_ENTER_MESSAGE,
    plan_accepted_message,
    plan_cancel_message,
)
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core_temporalio.child_workflow import UniversalSubagentWorkflow
from gc_core_temporalio.contracts import UserMessage
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from live_support import call, thread_meta
from plan_approval_workflow_fixture import (
    APPROVED_MODE,
    PlanApprovingWorkflow,
)
from temporalio.worker import Replayer
from test_workflow_controls import (  # noqa: F401
    finish,
    request,
    runtime,
    scripted_http,
    wait_status,
)

_WORKFLOWS = [PlanApprovingWorkflow, UniversalSubagentWorkflow]


def _user_text(request_json):
    return "\n".join(
        str(m.get("content"))
        for m in request_json["messages"]
        if m.get("role") == "user"
    )


async def _wait_new_run(handle, run_id):
    async with asyncio.timeout(30):
        while (await handle.describe()).run_id == run_id:
            await asyncio.sleep(0.05)
    return (await handle.describe()).run_id


async def test_plan_is_proposed_approved_and_executed(
    tmp_path,
    monkeypatch,
    scripted_http,  # noqa: F811
):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "done.txt"
    url, requests, script = scripted_http
    script.extend(
        [
            [call("plan", "turn_propose_plan", {"plan": "1. done.txt"})],
            [
                call(
                    "write",
                    "fs_write_file",
                    {"path": str(target), "content": "ok"},
                )
            ],
            [finish("исполнено")],
        ]
    )
    approval = wrap_technical_user_content(
        plan_accepted_message(APPROVED_MODE)
    )
    core_extra = {"security": {"permissions": {"default_mode": "plan"}}}
    async with runtime(
        tmp_path, url, core_extra=core_extra, workflows=_WORKFLOWS
    ) as (env, contexts, _):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(keep_alive=True, message_ids=["m1"]),
            id="plan-cycle",
            task_queue="native-controls",
        )
        first_run = handle.first_execution_run_id
        await wait_status(
            handle, lambda s: s["turns"] == 1 and s["phase"] == "idle"
        )
        second_run = await _wait_new_run(handle, first_run)
        await handle.execute_update(
            UniversalAgentWorkflow.submit_message,
            UserMessage("m2", approval),
        )
        await wait_status(
            handle, lambda s: s["turns"] == 2 and s["phase"] == "idle"
        )
        await handle.signal(UniversalAgentWorkflow.close_session)
        result = await asyncio.wait_for(handle.result(), 30)
        meta = await thread_meta(contexts.stores["files"], "test", "controls")
        histories = [
            await env.client.get_workflow_handle(
                "plan-cycle", run_id=run_id
            ).fetch_history()
            for run_id in (first_run, second_run)
        ]
    assert json.loads(result["answer"])["task_report"] == "исполнено"
    assert target.read_text(encoding="utf-8") == "ok"
    plan = meta["shared"]["plan"]
    assert plan["status"] == "approved"
    assert plan["approved_mode"] == APPROVED_MODE
    assert plan["updated_at"]
    assert meta["shared"]["permission_mode"] == APPROVED_MODE
    assert PLAN_ENTER_MESSAGE in _user_text(requests[0])
    after_approval = _user_text(requests[1])
    assert approval in after_approval
    # Выход из plan через одобрение: «план не одобрен» модели не шлётся.
    assert plan_cancel_message(APPROVED_MODE) not in after_approval
    for history in histories:
        await Replayer(workflows=_WORKFLOWS).replay_workflow(history)

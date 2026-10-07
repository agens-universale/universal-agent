"""Explicit native workflow client; leaves the legacy session CLI unchanged."""

import argparse
import asyncio
import json
import uuid

from gc_core.structured_output import read_format_description
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from gc_core_temporalio.contracts import RunRequest, UserMessage
from gc_core_temporalio.workflow import AgentWorkflow


class AgentClient:
    def __init__(self, client, *, workflow_queue="ua-workflow"):
        self.client, self.workflow_queue = client, workflow_queue

    async def start(
        self,
        request: RunRequest,
        *,
        workflow_id=None,
        retry_policy=None,
        execution_timeout=None,
    ):
        """Старт сессии.

        ``retry_policy`` и ``execution_timeout`` — параметры Temporal для всего
        workflow, по умолчанию не заданы. Повтор workflow начинает ход заново
        с тем же входом, и уже выполненные эффекты инструментов повторятся —
        сбои отдельных шагов повторяются activities (секция ``activities``
        конфига воркера).
        """
        if request.format_text is not None and not request.format_text.strip():
            raise ValueError("Structured output format must be nonempty")
        if not request.messages or not all(
            isinstance(text, str) and text.strip() for text in request.messages
        ):
            raise ValueError("At least one nonempty message is required")
        return await self.client.start_workflow(
            AgentWorkflow.run,
            request,
            id=workflow_id or f"ua-session-{uuid.uuid4().hex}",
            task_queue=self.workflow_queue,
            retry_policy=retry_policy,
            execution_timeout=execution_timeout,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def status(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).query(
            AgentWorkflow.status
        )

    async def result(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).result()

    async def stop(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            AgentWorkflow.stop
        )

    async def stop_turn(self, workflow_id, turn_id):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(AgentWorkflow.stop_turn, turn_id)

    async def close_session(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            AgentWorkflow.close_session
        )

    async def submit_with_start(
        self, workflow_id, request: RunRequest, message: UserMessage
    ):
        """Отправить сообщение, открыв сессию при необходимости, — атомарно.

        Закрытая или упавшая сессия с тем же ID начинается заново новым run.
        """
        message.validate()
        operation = WithStartWorkflowOperation(
            AgentWorkflow.run,
            request,
            id=workflow_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            task_queue=self.workflow_queue,
        )
        return await self.client.execute_update_with_start_workflow(
            AgentWorkflow.submit_message,
            message,
            start_workflow_operation=operation,
        )

    async def submit_message(self, workflow_id, message: UserMessage):
        message.validate()
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            AgentWorkflow.submit_message,
            message,
        )

    async def answer_human(
        self, workflow_id, request_id, answer, *, reply_id=None
    ):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            AgentWorkflow.answer_human,
            args=[request_id, answer, reply_id],
        )


async def _unlock(args, temporal):
    """``unlock``: снять владение тредом у сессии, которой больше нет."""
    from gc_core_temporalio.provider import WorkerConfig
    from gc_core_temporalio.thread_store_activities import release_stale_owner
    from gc_core_temporalio.worker import build_stores

    if not args.config:
        raise SystemExit(
            "unlock: нужен --config (JSON воркера с thread_stores)"
        )
    stores = build_stores(WorkerConfig.load(args.config))
    if args.store not in stores:
        raise SystemExit(
            f"unlock: хранилища {args.store!r} нет в конфиге воркера; "
            f"есть: {', '.join(sorted(stores)) or 'никаких'}"
        )
    released = await release_stale_owner(
        stores[args.store], args.system, args.thread, temporal
    )
    if released is None:
        return "Тред свободен или его сессия ещё исполняется: ничего не снято"
    return f"Снято владение сессии {released}"


async def _run(args):
    client = AgentClient(
        await Client.connect(args.address, namespace=args.namespace),
        workflow_queue=args.workflow_queue,
    )
    if args.command == "unlock":
        print(await _unlock(args, client.client))
        return
    if args.command == "start":
        handle = await client.start(
            RunRequest(
                format_text=read_format_description(
                    args.structured_output_format
                )
                if args.structured_output_format
                else None,
                profile=args.profile,
                store_id=args.store,
                system_id=args.system,
                thread_id=args.thread,
                messages=[args.prompt],
                resource_queue=args.resource_queue,
                model_queue=args.model_queue,
                keep_alive=args.keep_alive,
            ),
            workflow_id=args.workflow_id,
        )
        print(handle.id)
    elif args.command == "send":
        print(
            await client.submit_message(
                args.workflow_id, UserMessage(args.message_id, args.text)
            )
        )
    elif args.command == "close":
        await client.close_session(args.workflow_id)
    elif args.command == "answer":
        print(
            await client.answer_human(
                args.workflow_id, args.request_id, args.answer
            )
        )
    elif args.command == "stop":
        await client.stop(args.workflow_id)
    else:
        value = await getattr(client, args.command)(args.workflow_id)
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument(
        "--config",
        help="worker.json воркера (для unlock: хранилища тредов)",
    )
    parser.add_argument("--workflow-queue", default="ua-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    for name in ("profile", "thread", "prompt"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--system", default="default")
    start.add_argument("--store", default="files")
    start.add_argument("--resource-queue", default="ua-resource-local")
    start.add_argument("--model-queue", default="ua-model")
    start.add_argument("--workflow-id")
    start.add_argument("--structured-output-format")
    start.add_argument("--keep-alive", action="store_true")
    unlock = commands.add_parser(
        "unlock",
        help="снять владение тредом у завершённой или снятой сессии",
    )
    unlock.add_argument("--thread", required=True)
    unlock.add_argument("--system", default="default")
    unlock.add_argument("--store", default="files")
    for name in (
        "status",
        "result",
        "stop",
        "answer",
        "close",
        "send",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("workflow_id")
        if name == "send":
            sub.add_argument("--message-id", required=True)
            sub.add_argument("--text", required=True)
        if name == "answer":
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--answer", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()

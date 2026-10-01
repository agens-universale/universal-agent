"""Async session client and direct Temporal CLI."""

import argparse
import asyncio
import json

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.control_contracts import HumanReply, ReconcileRequest
from gc_ua_temporalio.identity import session_workflow_id
from gc_ua_temporalio.session_contracts import UserMessage
from gc_ua_temporalio.workflow import AgentSessionWorkflow


class TemporalAgentClient:
    def __init__(self, client: Client, *, workflow_queue="ua-workflow"):
        self.client = client
        self.workflow_queue = workflow_queue

    async def start(self, start: SessionStart):
        UserMessage(
            start.initial_message_id or f"{start.thread_id}:initial",
            start.prompt,
        ).validate()
        return await self.client.start_workflow(
            AgentSessionWorkflow.run,
            start,
            id=session_workflow_id(start.system_id, start.thread_id),
            task_queue=self.workflow_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def status(self, workflow_id):
        status = await self.client.get_workflow_handle(workflow_id).query(
            AgentSessionWorkflow.status
        )
        status["workflow_id"] = workflow_id
        if status.get("active_child"):
            try:
                status["child"] = await self.status(status["active_child"])
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
        return status

    async def _input_target(self, workflow_id):
        status = await self.status(workflow_id)
        while status.get("child"):
            status = status["child"]
        return self.client.get_workflow_handle(status["workflow_id"])

    async def submit_message(self, workflow_id, message: UserMessage):
        message.validate()
        for attempt in range(20):
            try:
                handle = await self._input_target(workflow_id)
                return await handle.execute_update(
                    AgentSessionWorkflow.submit_message, message
                )
            except WorkflowUpdateFailedError as exc:
                if (
                    getattr(exc.cause, "type", None)
                    not in ("SessionContinuing", "SessionClosing")
                    or attempt == 19
                ):
                    raise
                await asyncio.sleep(0.2)

    async def close_session(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            AgentSessionWorkflow.close_session
        )

    async def result(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).result()

    async def answer_human(self, workflow_id, reply: HumanReply):
        handle = await self._input_target(workflow_id)
        return await handle.execute_update(
            AgentSessionWorkflow.answer_human, reply
        )

    async def stop_turn(self, workflow_id, turn_id):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(AgentSessionWorkflow.stop_turn, turn_id)

    async def reconcile_operation(
        self, workflow_id, request: ReconcileRequest
    ):
        handle = await self._input_target(workflow_id)
        return await handle.execute_update(
            AgentSessionWorkflow.reconcile_operation, request
        )


async def _run(args):
    client = TemporalAgentClient(
        await Client.connect(args.address, namespace=args.namespace),
        workflow_queue=args.workflow_queue,
    )
    if args.command == "start":
        handle = await client.start(
            SessionStart(
                thread_id=args.thread,
                prompt=args.prompt,
                profile=args.profile,
                profile_revision=args.revision,
                auto_close=args.auto_close,
                resume_imported=args.resume_imported,
                resource_queue=args.resource_queue,
                model_queue=args.model_queue,
            )
        )
        print(handle.id)
    elif args.command == "status":
        print(
            json.dumps(
                await client.status(args.workflow_id),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "close":
        await client.close_session(args.workflow_id)
    elif args.command == "answer":
        print(
            await client.answer_human(
                args.workflow_id,
                HumanReply(args.request_id, args.reply_id, args.answer),
            )
        )
    elif args.command == "stop":
        print(await client.stop_turn(args.workflow_id, args.turn_id))
    elif args.command == "send":
        print(
            await client.submit_message(
                args.workflow_id, UserMessage(args.message_id, args.text)
            )
        )
    elif args.command == "reconcile":
        print(
            await client.reconcile_operation(
                args.workflow_id,
                ReconcileRequest(
                    args.operation_id,
                    args.request_id,
                    args.resolution,
                    args.output,
                    args.processes_stopped,
                ),
            )
        )
    else:
        print(
            json.dumps(
                await client.result(args.workflow_id),
                ensure_ascii=False,
                indent=2,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--workflow-queue", default="ua-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--thread", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--profile", default="demo")
    start.add_argument("--revision", default="v1")
    start.add_argument("--resource-queue", default="ua-resource-local")
    start.add_argument("--model-queue", default="ua-model")
    start.add_argument("--auto-close", action="store_true")
    start.add_argument("--resume-imported", action="store_true")
    for command in ("status", "close", "result"):
        commands.add_parser(command).add_argument("workflow_id")
    answer = commands.add_parser("answer")
    answer.add_argument("workflow_id")
    answer.add_argument("--request-id", required=True)
    answer.add_argument("--reply-id", required=True)
    answer.add_argument("--answer", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("workflow_id")
    stop.add_argument("--turn-id", required=True)
    send = commands.add_parser("send")
    send.add_argument("workflow_id")
    send.add_argument("--message-id", required=True)
    send.add_argument("--text", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("workflow_id")
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--request-id", required=True)
    reconcile.add_argument(
        "--resolution",
        choices=("inspect", "applied", "not_applied"),
        default="inspect",
    )
    reconcile.add_argument("--output", default="")
    reconcile.add_argument("--processes-stopped", action="store_true")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()

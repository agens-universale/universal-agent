"""Explicit native workflow client; leaves the legacy session CLI unchanged."""

import argparse
import asyncio
import json
import uuid

from gc_core.structured_output import read_format_description
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_workflow import NativeAgentWorkflow
from gc_ua_temporalio.session_contracts import UserMessage


class NativeAgentClient:
    def __init__(self, client, *, workflow_queue="ua-native-workflow"):
        self.client, self.workflow_queue = client, workflow_queue

    async def start(self, request: NativeRunRequest, *, workflow_id=None):
        if request.format_text is not None and not request.format_text.strip():
            raise ValueError("Structured output format must be nonempty")
        if not request.messages or not all(
            isinstance(text, str) and text.strip() for text in request.messages
        ):
            raise ValueError("At least one nonempty message is required")
        return await self.client.start_workflow(
            NativeAgentWorkflow.run,
            request,
            id=workflow_id or f"ua-native-{uuid.uuid4().hex}",
            task_queue=self.workflow_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def status(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).query(
            NativeAgentWorkflow.status
        )

    async def result(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).result()

    async def stop(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            NativeAgentWorkflow.stop
        )

    async def stop_turn(self, workflow_id, turn_id):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(NativeAgentWorkflow.stop_turn, turn_id)

    async def inspect_operation(self, workflow_id):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(NativeAgentWorkflow.inspect_operation)

    async def resume_operation(self, workflow_id, operation_id, digest):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            NativeAgentWorkflow.resume_operation, args=[operation_id, digest]
        )

    async def resolve_operation(self, workflow_id, resolution):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(NativeAgentWorkflow.reconcile_operation, resolution)

    async def close_session(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            NativeAgentWorkflow.close_session
        )

    async def submit_message(self, workflow_id, message: UserMessage):
        message.validate()
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            NativeAgentWorkflow.submit_message,
            message,
        )

    async def answer_human(
        self, workflow_id, request_id, answer, *, reply_id=None
    ):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            NativeAgentWorkflow.answer_human,
            args=[request_id, answer, reply_id],
        )


async def _run(args):
    client = NativeAgentClient(
        await Client.connect(args.address, namespace=args.namespace),
        workflow_queue=args.workflow_queue,
    )
    if args.command == "start":
        handle = await client.start(
            NativeRunRequest(
                format_text=read_format_description(
                    args.structured_output_format
                )
                if args.structured_output_format
                else None,
                profile=args.profile,
                revision=args.revision,
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
    elif args.command == "inspect":
        print(
            json.dumps(
                await client.inspect_operation(args.workflow_id),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "resume":
        print(
            await client.resume_operation(
                args.workflow_id, args.operation_id, args.digest
            )
        )
    elif args.command == "resolve":
        from pathlib import Path

        from gc_ua_temporalio.native_operator_contracts import (
            NativeOperatorResolution,
        )

        resolution = NativeOperatorResolution(
            **json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        )
        print(await client.resolve_operation(args.workflow_id, resolution))
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
    parser.add_argument("--workflow-queue", default="ua-native-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    for name in ("profile", "revision", "thread", "prompt"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--system", default="default")
    start.add_argument("--store", default="files")
    start.add_argument("--resource-queue", default="ua-native-resource-local")
    start.add_argument("--model-queue", default="ua-native-model")
    start.add_argument("--workflow-id")
    start.add_argument("--structured-output-format")
    start.add_argument("--keep-alive", action="store_true")
    for name in (
        "status",
        "result",
        "stop",
        "answer",
        "close",
        "send",
        "inspect",
        "resolve",
        "resume",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("workflow_id")
        if name == "resume":
            sub.add_argument("--operation-id", required=True)
            sub.add_argument("--digest", required=True)
        if name == "resolve":
            sub.add_argument("--receipt", required=True)
        if name == "send":
            sub.add_argument("--message-id", required=True)
            sub.add_argument("--text", required=True)
        if name == "answer":
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--answer", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()

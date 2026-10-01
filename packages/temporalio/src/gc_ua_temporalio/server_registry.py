"""Opt-in server backend; no execution task lives inside the API process."""

import asyncio

from gc_core.thread_store import sanitize_system_id, validate_thread_id
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from gc_ua_temporalio.client import TemporalAgentClient
from gc_ua_temporalio.contracts import SessionStart
from gc_ua_temporalio.identity import session_workflow_id
from gc_ua_temporalio.session_contracts import UserMessage
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from gc_ua_temporalio.storage.views import SessionViews


class TemporalRunRegistry:
    def __init__(self, config, *, profile="demo"):
        self.config, self.profile = config, profile
        self.views = SessionViews(SQLiteThreadStore(config.database))
        self._client = None
        self._connect_lock = asyncio.Lock()
        if profile not in config.profiles:
            raise ValueError("Unknown server Temporal profile")

    async def client(self):
        async with self._connect_lock:
            if self._client is None:
                self._client = TemporalAgentClient(
                    await Client.connect(
                        self.config.address, namespace=self.config.namespace
                    ),
                    workflow_queue=self.config.workflow_queue,
                )
        return self._client

    @staticmethod
    def identifiers(system, thread):
        validate_thread_id(thread)
        if not system or sanitize_system_id(system) != system:
            raise ValueError("Invalid system ID")
        return system, thread

    def workflow_id(self, system, thread):
        self.identifiers(system, thread)
        owner = self.views.owner(system, thread)
        return (
            owner.rsplit("/", 1)[0]
            if owner
            else session_workflow_id(system, thread)
        )

    async def start_or_send(self, system, thread, message):
        self.identifiers(system, thread)
        message.validate()
        client = await self.client()
        owner = self.views.owner(system, thread)
        if owner:
            workflow_id = owner.rsplit("/", 1)[0]
            receipt = await client.submit_message(workflow_id, message)
            return {"workflow_id": workflow_id, "receipt": receipt}
        start = SessionStart(
            thread,
            message.text,
            system_id=system,
            initial_message_id=message.message_id,
            profile=self.profile,
            profile_revision=self.config.profiles[self.profile].revision,
            resource_queue=self.config.resource_queue,
            model_queue=self.config.model_queue,
        )
        try:
            handle = await client.start(start)
            workflow_id = handle.id
        except WorkflowAlreadyStartedError:
            workflow_id = self.workflow_id(system, thread)
        receipt = await client.submit_message(workflow_id, message)
        return {"workflow_id": workflow_id, "receipt": receipt}

    async def status(self, system, thread):
        client = await self.client()
        return await client.status(self.workflow_id(system, thread))

    async def legacy_status(self, system, thread):
        status = await self.status(system, thread)
        state = {
            "idle": "completed",
            "closed": "completed",
            "stopped": "cancelled",
            "failed": "failed",
        }.get(status["phase"], "running")
        return {
            **status,
            "status": state,
            "system": system,
            "task_id": thread,
            "thread_id": thread,
            "result": status["answer"],
        }

    async def legacy_logs(self, system, thread):
        self.identifiers(system, thread)
        messages = await asyncio.to_thread(
            self.views.store.load_messages, system, thread
        )
        meta = await asyncio.to_thread(
            self.views.store.load_meta, system, thread
        )
        return {
            "status": "ok",
            "system": system,
            "task_id": thread,
            "messages": messages,
            "meta": meta,
        }

    async def webhook(self, system, thread, payload):
        import json

        message_id = payload.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Temporal webhook requires a stable message_id")
        result = await self.start_or_send(
            system,
            thread,
            UserMessage(message_id, json.dumps(payload, ensure_ascii=False)),
        )
        return {
            "status": "queued",
            "payload": payload,
            "system": system,
            "task_id": thread,
            **result,
        }

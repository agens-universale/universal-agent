"""Explicit native HTTP backend; conversation storage is never a projection."""

import asyncio
import hashlib
import json

from gc_core.thread_store import sanitize_system_id, validate_thread_id
from gc_core.thread_store_file_durable import DurableFileThreadStore
from temporalio.client import Client

from gc_core_temporalio.client import AgentClient
from gc_core_temporalio.contracts import RunRequest, UserMessage
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core_temporalio.views import ThreadViews


class RemoteClient(AgentClient):
    async def answer_human(self, workflow_id, reply):
        return await super().answer_human(
            workflow_id,
            reply.request_id,
            reply.answer,
            reply_id=reply.reply_id,
        )


class RunRegistry:
    backend = "temporal"

    def __init__(self, config, *, profile, store_id=None):
        if profile not in config.profiles:
            raise ValueError("Unknown native Temporal profile")
        self.config, self.profile = config, profile
        self.store_id = store_id or config.profiles[profile].store_id
        if self.store_id != config.profiles[profile].store_id:
            raise ValueError(
                "Server store must match the pinned native profile"
            )
        spec = config.thread_stores[self.store_id]
        store = (
            DurableFileThreadStore(spec.path)
            if spec.kind == "file"
            else SQLiteThreadStore(spec.path)
        )
        self.views = ThreadViews(store, workflow_id=self.workflow_id)
        self._client = None
        self._connect_lock = asyncio.Lock()

    async def client(self):
        async with self._connect_lock:
            if self._client is None:
                self._client = RemoteClient(
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
        digest = hashlib.sha256(
            json.dumps([self.store_id, system, thread]).encode()
        ).hexdigest()
        expected = "ua-session-" + digest
        owner = self.views.owner(system, thread)
        if owner and owner != expected:
            raise ValueError(
                "Thread belongs to another execution backend or workflow"
            )
        return expected

    async def lookup_workflow_id(self, system, thread):
        """``workflow_id`` вне event loop: владелец читается под ``flock``."""
        return await asyncio.to_thread(self.workflow_id, system, thread)

    async def start_or_send(self, system, thread, message):
        message.validate()
        workflow_id = await self.lookup_workflow_id(system, thread)
        client = await self.client()
        request = RunRequest(
            profile=self.profile,
            store_id=self.store_id,
            system_id=system,
            thread_id=thread,
            messages=[],
            resource_queue=self.config.resource_queue,
            model_queue=self.config.model_queue,
            keep_alive=True,
        )
        receipt = await client.submit_with_start(workflow_id, request, message)
        return {
            "workflow_id": workflow_id,
            "receipt": receipt,
            "backend": self.backend,
        }

    async def status(self, system, thread):
        client = await self.client()
        value = await client.status(
            await self.lookup_workflow_id(system, thread)
        )
        return {**value, "backend": self.backend}

    async def legacy_status(self, system, thread):
        """Status in the shape of the server's local /status endpoint."""
        status = await self.status(system, thread)
        state = {
            "idle": "completed",
            "closed": "completed",
            "completed": "completed",
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
        """Logs in the shape of the server's local /logs endpoint."""
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

"""Explicit native HTTP backend; conversation storage is never a projection."""

import asyncio
import hashlib
import json

from gc_core.thread_store_file import FileThreadStore
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from gc_ua_temporalio.native_client import NativeAgentClient
from gc_ua_temporalio.native_contracts import NativeRunRequest
from gc_ua_temporalio.native_views import NativeViews
from gc_ua_temporalio.server_registry import TemporalRunRegistry
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore


class NativeRemoteClient(NativeAgentClient):
    async def answer_human(self, workflow_id, reply):
        return await super().answer_human(
            workflow_id,
            reply.request_id,
            reply.answer,
            reply_id=reply.reply_id,
        )

    async def reconcile_operation(self, workflow_id, value):
        raise ValueError(
            "Native reconciliation requires the effect ledger operator interface"
        )


class NativeRunRegistry(TemporalRunRegistry):
    backend = "temporal-native"

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
            FileThreadStore(spec.path)
            if spec.kind == "file"
            else SQLiteThreadStore(spec.path)
        )
        self.views = NativeViews(
            store,
            SQLiteThreadStore(config.receipts_database),
            workflow_id=self.workflow_id,
        )
        self._client = None
        self._connect_lock = asyncio.Lock()

    async def client(self):
        async with self._connect_lock:
            if self._client is None:
                self._client = NativeRemoteClient(
                    await Client.connect(
                        self.config.address, namespace=self.config.namespace
                    ),
                    workflow_queue=self.config.workflow_queue,
                )
        return self._client

    def workflow_id(self, system, thread):
        self.identifiers(system, thread)
        digest = hashlib.sha256(
            json.dumps([self.store_id, system, thread]).encode()
        ).hexdigest()
        expected = "ua-native-session-" + digest
        owner = self.views.owner(system, thread)
        if owner and owner.rsplit("/", 1)[0] != expected:
            raise ValueError(
                "Thread belongs to another execution backend or workflow"
            )
        return expected

    async def start_or_send(self, system, thread, message):
        message.validate()
        workflow_id = self.workflow_id(system, thread)
        client = await self.client()
        if not self.views.owner(system, thread):
            request = NativeRunRequest(
                profile=self.profile,
                revision=self.config.profiles[self.profile].revision,
                store_id=self.store_id,
                system_id=system,
                thread_id=thread,
                messages=[message.text],
                message_ids=[message.message_id],
                resource_queue=self.config.resource_queue,
                model_queue=self.config.model_queue,
                keep_alive=True,
            )
            try:
                await client.start(request, workflow_id=workflow_id)
            except WorkflowAlreadyStartedError:
                pass
        receipt = await client.submit_message(workflow_id, message)
        return {
            "workflow_id": workflow_id,
            "receipt": receipt,
            "backend": self.backend,
        }

    async def status(self, system, thread):
        value = await super().status(system, thread)
        return {**value, "backend": self.backend}

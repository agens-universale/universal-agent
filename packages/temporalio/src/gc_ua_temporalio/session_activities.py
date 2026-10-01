"""Resource-side mailbox and immutable Continue-As-New snapshots."""

import json

from temporalio import activity

from gc_ua_temporalio.activities import classified_error
from gc_ua_temporalio.session_contracts import (
    Checkpoint,
    InboxRequest,
    MessageDisposition,
)
from gc_ua_temporalio.storage.inbox import InboxLedger
from gc_ua_temporalio.storage.sqlite import encode


class SessionActivities:
    def __init__(self, runtime):
        self.runtime = runtime
        self.inbox = InboxLedger(runtime.store)

    @activity.defn(name="ua.accept_message")
    async def accept(self, data: InboxRequest) -> dict:
        try:
            return self.inbox.accept(data)
        except Exception as exc:
            raise classified_error(exc) from None

    @activity.defn(name="ua.finish_messages")
    async def finish(self, data: MessageDisposition) -> dict:
        try:
            return self.inbox.finish(data)
        except Exception as exc:
            raise classified_error(exc) from None

    @activity.defn(name="ua.checkpoint")
    async def checkpoint(self, data: Checkpoint) -> Checkpoint:
        if data.ref is not None:
            stored = json.loads(self.runtime.artifacts.read(data.ref))
            if stored["owner"] != data.owner:
                raise ValueError("Checkpoint belongs to another execution")
            return Checkpoint(
                data.owner, data.operation_id, data=stored["data"]
            )
        ref = self.runtime.artifacts.put(
            encode({"owner": data.owner, "data": data.data}).encode()
        )
        return Checkpoint(data.owner, data.operation_id, ref=ref)

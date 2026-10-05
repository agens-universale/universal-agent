"""Фоновое обслуживание воркера: брошенные контексты и служебные данные.

Код workflow сам отпускает контекст хода и снимает владение тредом. Но после
terminate, падения процесса или истечения retention никто этого не сделает:
контексты с MCP-процессами жили бы до рестарта воркера, а квитанции и preview
копились бы вечно. Решение «владелец мёртв» принимает Temporal: workflow не
RUNNING, работает уже новая цепочка run или его нет вовсе.
"""

import asyncio
import logging

from gc_core.thread_store_journal import prune_durable_receipts
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

logger = logging.getLogger(__name__)

#: Как часто проверять и сколько контекст должен простаивать до проверки.
INTERVAL_SECONDS = 600
IDLE_SECONDS = 600
#: Квитанции операций хранилища тредов нужны только окну повторов activity.
RECEIPT_MAX_AGE_SECONDS = 7 * 24 * 3600


def owner_workflow(owner):
    """``(workflow ID, ID цепочки run | None)`` владельца контекста.

    Ход родителя — ``<workflow>/<first_execution_run_id>/turn-N``; ребёнок и
    прочие владельцы — сам workflow ID.
    """
    parts = owner.rsplit("/", 2)
    if len(parts) == 3 and parts[2].startswith("turn-"):
        return parts[0], parts[1]
    return owner, None


async def owner_state(client, owner):
    """``"running"``, ``"closed"`` (не исполняется) или ``"gone"`` (нет в Temporal)."""
    workflow_id, chain = owner_workflow(owner)
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return "gone"
        raise
    if description.status != WorkflowExecutionStatus.RUNNING:
        return "closed"
    if chain is not None and description.raw_info.first_run_id != chain:
        return "closed"  # тот же ID, но новая цепочка run
    return "running"


class NativeMaintenance:
    def __init__(self, client, provider, technical, stores):
        self.client, self.provider = client, provider
        self.technical, self.stores = technical, dict(stores)

    async def run_once(self):
        for owner in self.provider.idle_owners(IDLE_SECONDS):
            if await owner_state(self.client, owner) != "running":
                logger.info("Releasing abandoned native context %s", owner)
                await self.provider.forget_owner(owner)
        for owner in await asyncio.to_thread(self.technical.owners):
            if await owner_state(self.client, owner) == "gone":
                await asyncio.to_thread(self.technical.delete_owner, owner)
        for store in self.stores.values():
            await asyncio.to_thread(_prune_store_receipts, store)

    async def serve(self):
        while True:
            await asyncio.sleep(INTERVAL_SECONDS)
            try:
                await self.run_once()
            except Exception:
                logger.exception("Native worker maintenance failed")


def _prune_store_receipts(store):
    prune = getattr(store, "prune_receipts", None)
    if prune is not None:
        prune(max_age_seconds=RECEIPT_MAX_AGE_SECONDS)
    elif getattr(store, "threads_dir", None):
        prune_durable_receipts(
            store.threads_dir, max_age_seconds=RECEIPT_MAX_AGE_SECONDS
        )

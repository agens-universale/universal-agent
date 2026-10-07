"""Фоновое обслуживание воркера: брошенные контексты и служебные данные.

Код workflow сам отпускает контекст хода и снимает владение тредом. Но после
terminate или падения процесса никто этого не сделает: контексты с
MCP-процессами жили бы до рестарта воркера. Решение «владелец мёртв»
принимает Temporal: workflow не RUNNING, работает уже новая цепочка run или
его нет вовсе. Ещё обслуживание чистит устаревшие записи журналов хранилища
тредов.
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
#: Одновременных ``describe`` к Temporal за проход.
DESCRIBE_CONCURRENCY = 8


def owner_workflow(owner):
    """``(workflow ID, ID цепочки run | None)`` владельца контекста.

    Ход родителя — ``<workflow>/<first_execution_run_id>/turn-N``; ребёнок и
    прочие владельцы — сам workflow ID.
    """
    parts = owner.rsplit("/", 2)
    if len(parts) == 3 and parts[2].startswith("turn-"):
        return parts[0], parts[1]
    return owner, None


async def describe_workflow(client, workflow_id):
    """``("gone", None)``, ``("closed", None)`` или ``("running", first_run_id)``."""
    try:
        description = await client.get_workflow_handle(workflow_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return "gone", None
        raise
    if description.status != WorkflowExecutionStatus.RUNNING:
        return "closed", None
    return "running", description.raw_info.first_run_id or None


def owner_state(owner, workflows):
    """``"running"``, ``"closed"`` (не исполняется), ``"gone"`` (нет в Temporal)
    или ``None`` — состояние неизвестно, ничего не трогать."""
    workflow_id, chain = owner_workflow(owner)
    known = workflows.get(workflow_id)
    if known is None:
        return None
    state, first_run = known
    if state != "running":
        return state
    if chain is not None and first_run is not None and first_run != chain:
        return "closed"  # тот же ID, но новая цепочка run
    return "running"


class WorkerMaintenance:
    def __init__(self, client, provider, stores):
        self.client, self.provider = client, provider
        self.stores = dict(stores)

    async def _describe_all(self, owners):
        """Одно ``describe`` на workflow, параллельно и с ограничением; сбой
        по одному workflow оставляет его состояние неизвестным."""
        ids = {owner_workflow(owner)[0] for owner in owners}
        limit = asyncio.Semaphore(DESCRIBE_CONCURRENCY)

        async def one(workflow_id):
            async with limit:
                try:
                    return workflow_id, await describe_workflow(
                        self.client, workflow_id
                    )
                except Exception:
                    logger.warning(
                        "Maintenance cannot describe %s", workflow_id
                    )
                    return workflow_id, None

        results = await asyncio.gather(*(one(item) for item in ids))
        return {key: value for key, value in results if value is not None}

    async def run_once(self):
        idle = self.provider.idle_owners(IDLE_SECONDS)
        workflows = await self._describe_all(idle)
        for owner in idle:
            if owner_state(owner, workflows) in ("closed", "gone"):
                logger.info("Releasing abandoned native context %s", owner)
                await self.provider.forget_owner(owner)
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

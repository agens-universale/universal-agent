"""Атомарное закрепление треда за webhook или swarm до запуска работы."""

from __future__ import annotations

import uuid

from gc_core.threads.errors import ThreadConflict
from gc_core.threads.facade import Threads
from gc_core.threads.models import SideChange, ThreadKey

from gc_server.surface import http_error


async def claim_surface(
    threads: Threads, key: ThreadKey, surface: str
) -> None:
    for _ in range(16):
        record = await threads.get(key)
        frontend = record.frontend if record is not None else {}
        other = "webhook" if surface == "swarm" else "swarm"
        if record is not None and (
            surface not in frontend or other in frontend
        ):
            raise http_error(
                409,
                "thread belongs to another surface or application",
                system=key.system,
                task_id=key.thread,
            )
        if surface in frontend:
            return
        try:
            await threads.store.append_side(
                key,
                SideChange(
                    op_id=f"surface-{uuid.uuid4().hex}",
                    expected_frontend=frontend,
                    patch_frontend=(surface, {}),
                ),
            )
            return
        except ThreadConflict:
            continue
    raise http_error(409, "thread ownership changed; retry the request")

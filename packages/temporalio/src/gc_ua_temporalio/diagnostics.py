"""Operational counters without reading model prompts or credentials."""


def storage_metrics(store, system, thread):
    with store.transaction() as db:
        row = db.execute(
            "SELECT owner,sequence FROM threads WHERE system=? AND thread=?",
            (system, thread),
        ).fetchone()
        owner = row["owner"] if row else None
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counters = {"projection_sequence": row["sequence"] if row else 0}
        for table, statuses in (
            ("effects", ("STARTED", "UNKNOWN")),
            ("mcp_sessions", ("SESSION_LOST",)),
        ):
            for status in statuses:
                counters[f"{table}_{status.lower()}"] = (
                    db.execute(
                        f"SELECT count(*) FROM {table} WHERE owner=? AND status=?",
                        (owner, status),
                    ).fetchone()[0]
                    if table in tables
                    else 0
                )
    return counters


async def session_metrics(registry, system, thread):
    client = await registry.client()
    workflow_id = registry.workflow_id(system, thread)
    status = await client.status(workflow_id)
    description = await client.client.get_workflow_handle(
        workflow_id
    ).describe()
    metrics = storage_metrics(registry.views.store, system, thread)
    metrics["projection_lag"] = max(
        0, status["projection_sequence"] - metrics["projection_sequence"]
    )
    metrics["phase"] = status["phase"]
    metrics["pending_activities"] = [
        {"activity": item.activity_type.name, "attempt": item.attempt}
        for item in description.raw_description.pending_activities
    ]
    return metrics

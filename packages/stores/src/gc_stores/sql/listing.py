"""Запрос списка тредов: фильтры, курсор и порядок контракта.

Порядок — ``updated_at`` по убыванию, затем ``system``, ``thread_id`` по
кодам символов (как ``gc_core.threads.rules.page_of``); курсор — последняя
запись прошлой страницы ``"{updated_at}|{system}|{thread_id}"``.
"""

from __future__ import annotations

from typing import Any

from gc_core.threads.models import ThreadQuery
from gc_core.threads.rules import instant, parse_list_cursor

from gc_stores.sql.queries import SqlQueries


def _cursor(q: SqlQueries, p, cursor: str) -> tuple[str, list[Any]]:
    d, c = q.d, q.c
    at, system, thread = parse_list_cursor(cursor)
    moment = d.time_in(at)
    sys_c, thr_c = f"{c('system')}{d.collate}", f"{c('thread_id')}{d.collate}"
    clause = (
        f"({c('updated_at')} < {p()} OR ({c('updated_at')} = {p()} AND "
        f"({sys_c} > {p()} OR ({c('system')} = {p()} AND {thr_c} > {p()}))))"
    )
    return clause, [moment, moment, system, system, thread]


def list_query(q: SqlQueries, query: ThreadQuery) -> tuple[str, list[Any]]:
    """SQL и параметры страницы (на одну строку больше ``limit``)."""
    d, c = q.d, q.c
    p = d.params()
    where: list[str] = []
    args: list[Any] = []
    for value, column in (
        (query.system, "system"),
        (query.workspace, "workspace"),
    ):
        if value is not None:
            where.append(f"{c(column)} = {p()}")
            args.append(value)
    if query.updated_after:
        where.append(f"{c('updated_at')} > {p()}")
        args.append(d.time_in(instant(query.updated_after)))
    if query.updated_before:
        where.append(f"{c('updated_at')} < {p()}")
        args.append(d.time_in(instant(query.updated_before)))
    if query.cursor:
        clause, values = _cursor(q, p, query.cursor)
        where.append(clause)
        args.extend(values)
    order = (
        f"{c('updated_at')} DESC, {c('system')}{d.collate}, "
        f"{c('thread_id')}{d.collate}"
    )
    sql = (
        f"SELECT {q.thread_columns} FROM {q.threads} "
        f"WHERE {' AND '.join(where) or '1 = 1'} ORDER BY {order} "
        f"LIMIT {p()}"
    )
    args.append(query.limit + 1)
    return sql, args

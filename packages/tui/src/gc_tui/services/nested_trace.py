"""Узел вложенного трейса субагентов для кусков истории (спека шага 2 окна
ленты, §5.9).

Один модуль-владелец: отрисовщик кусков знает о нём только через
«дай вложенное для этих родителей» (`NestedSource.actions_for`) и
«домонтируй его» (`NestedSource.mount`). Трасса субагентов пишется
вживую, поэтому узел читает её по требованию — отфильтрованной по
родителям куска в окне удержания; своей памяти вложенных строк нет.
Ветка фоновых субагентов снимает узел целиком.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING

from gc_tui.services.command_output import split_command
from gc_tui.services.subagent_sidecar_mapper import (
    CHILD_SUMMARY_LABEL,
    SidecarNestedAction,
    apply_sidecar_nested_actions,
    nested_result_event,
    plan_sidecar_nested_actions,
)
from gc_tui.services.todo_render import build_todo_block
from gc_tui.services.tool_blocks import ToolBlocks
from gc_tui.services.trace_render import (
    build_call_row,
    build_nested_prose_row,
    level_prefix,
    result_with_edits,
    tool_caption,
)
from gc_tui.services.trace_retention import filter_sidecar_to_parents
from gc_tui.widgets.tool_blocks_view import (
    BlockViews,
    ToolBlocksView,
    nested_tail,
)
from gc_tui.widgets.user_reply import NestedUserReply

if TYPE_CHECKING:
    from textual.widget import Widget

    from gc_tui.services.history_render import ChunkOptions, HistoryHost


class NestedSource:
    """Вложенное для родителей куска. ``read`` — трасса субагентов треда
    целиком (читается на каждый запрос: запись после открытия видна);
    ``window_parent_ids`` — родители в окне удержания; ``root`` и
    ``injected`` — корень путей и служебные поля схем, как у сводок
    вызовов истории."""

    def __init__(
        self,
        read: Callable[[], list[dict]],
        *,
        window_parent_ids: Callable[[], set[str]],
        root: str | None,
        injected: Iterable[str],
    ) -> None:
        self._read = read
        self._window_parent_ids = window_parent_ids
        self._root = root
        self._injected = frozenset(injected)

    def actions_for(self, parent_ids: set[str]) -> list[SidecarNestedAction]:
        """Действия вложенного для ``parent_ids`` из окна удержания; кусок
        без родителей субагентов трассу не читает."""
        wanted = parent_ids & self._window_parent_ids()
        if not wanted:
            return []
        records = filter_sidecar_to_parents(self._read(), wanted)
        if not records:
            return []
        return plan_sidecar_nested_actions(
            records, root=self._root, injected=self._injected
        )

    def mount(
        self,
        host: HistoryHost,
        actions: list[SidecarNestedAction],
        calls: dict[str, Widget],
        *,
        views: BlockViews,
        options: ChunkOptions,
    ) -> list[Widget]:
        """Домонтировать ``actions`` под строки ``calls``: модель уровня
        субагента на каждую строку `Agent` (в ``views``), монтаж за
        последним виджетом уровня (§9.1). Возвращает смонтированное."""
        if not actions:
            return []
        level = _Levels(host, calls, views, options, self._root)
        apply_sidecar_nested_actions(
            actions,
            mount_nested_tool=level.tool,
            finish_nested_tool=level.finish,
            mount_nested_prose=level.prose,
            mount_nested_user=level.user,
            mount_nested_summary=level.child_summary,
            mount_nested_todo=level.todo,
        )
        # Исход хода родителя — уровням, домонтированным после итога.
        views.settle_levels()
        return level.mounted


class _LevelHost:
    """Хост блоков уровня субагента: монтирование под строкой `Agent`."""

    def __init__(self, mount) -> None:
        self._mount = mount

    def mount(self, widget, *, before=None, after=None) -> None:
        self._mount(widget, before=before, after=after)


class _Levels:
    """Один проход домонтирования: уровни субагентов и их строки."""

    def __init__(
        self,
        host: HistoryHost,
        calls: dict[str, Widget],
        views: BlockViews,
        options: ChunkOptions,
        root: str | None,
    ) -> None:
        self._host = host
        self._calls = calls
        self._views = views
        self._opts = options
        self._root = root
        self._levels: dict[str, list[Widget]] = {}
        self._rows: dict[str, Widget] = {}
        self.mounted: list[Widget] = []

    def _view(self, pid: str) -> ToolBlocksView | None:
        parent = self._calls.get(pid)
        if parent is None:
            return None
        view = self._views.get(pid)
        if view is None:
            opts = self._opts
            model = ToolBlocks(
                opts.timers, diff_preview=opts.diff_preview, agent_window=True
            )
            host = _LevelHost(partial(self._mount_after, pid))
            view = ToolBlocksView(host, model, level=pid, depth=1)
            view.set_owner(parent)
            self._views[pid] = view
        return view

    def _mount_after(self, pid: str, widget, *, before=None, after=None):
        view = self._view(pid)
        if view is None:
            return
        level = self._levels.setdefault(pid, [])
        widget.add_class("-in-agent")
        if before is None and after is None:
            children = list(self._host.feed.children)
            after = nested_tail(children, level, self._calls[pid])
        try:
            self._host.mount(widget, before=before, after=after)
        except Exception:
            self._host.feed.mount(widget)
        level.append(widget)
        self.mounted.append(widget)
        if not view.issued(widget):
            view.event_added(widget)

    def tool(
        self,
        *,
        parent_call_id,
        tool_name,
        tool_call_id,
        args_summary,
        command=None,
        **_kw,
    ):
        view = self._view(parent_call_id)
        if view is None:
            return
        opts = self._opts
        row = build_call_row(
            call_id=tool_call_id or f"_nested_{tool_name}",
            wire_name=tool_name,
            args_summary=args_summary or "",
            level=1,
            mcp_tools=opts.mcp_tools,
            mcp_servers=opts.mcp_servers,
        )
        if tool_call_id:
            self._rows[tool_call_id] = row
        category, _ = tool_caption(
            tool_name, opts.mcp_tools, opts.mcp_servers, nested=True
        )
        lines = split_command(command, root=self._root)
        view.add_call(row, category, waiting=False, command=lines)

    def finish(
        self,
        *,
        parent_call_id,
        tool_call_id="",
        ok=True,
        error_brief=None,
        outcome=None,
        output=None,
        **_kw,
    ):
        row = self._rows.get(tool_call_id)
        view = self._views.get(parent_call_id)
        if row is None or view is None:
            return
        ev = nested_result_event(
            tool_call_id,
            ok=ok,
            error_brief=error_brief,
            outcome=outcome,
            output=output,
        )
        result_with_edits(
            view,
            row,
            ev,
            edits_lookup=self._opts.edits_lookup,
            window=self._opts.expanded_turns,
        )

    def prose(
        self, *, parent_call_id, prose, ok=True, error_brief=None, **_kw
    ):
        text = (prose or "").strip()
        if not text:
            return
        self._mount_after(
            parent_call_id,
            build_nested_prose_row(text, ok=ok, error_brief=error_brief),
        )

    def user(self, *, parent_call_id, prose, images=(), **_kw):
        text = (prose or "").strip()
        if not text:
            return
        self._mount_after(parent_call_id, NestedUserReply(text, images=images))

    def child_summary(self, *, parent_call_id, prose="", **_kw):
        self.prose(
            parent_call_id=parent_call_id, prose=prose or CHILD_SUMMARY_LABEL
        )

    def todo(self, *, parent_call_id, todo_items=None, **_kw):
        self._mount_after(
            parent_call_id,
            build_todo_block(
                todo_items or [], subagent=True, indent=level_prefix(1)
            ),
        )

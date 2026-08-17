"""Integration: первый ввод → автосоздание задачи → polling показывает agent-ответ.

Регрессия Bug 1: без attach_active_task после runner.start, _store/_cursor
остаются None, _poll_active выходит сразу — agent-сообщения из data_*.jsonl
никогда не попадают в trace.
"""
import json
from pathlib import Path

import pytest


class _FakeExecutor:
    """Синхронно пишет user+assistant в data_*.jsonl, имитируя реальный writer."""

    def __init__(self, threads_dir: str, system: str = "cli") -> None:
        self.threads_dir = threads_dir
        self.system = system
        self.stopped = False

    def run(self, messages, *, thread_id, start_new_thread=False,
            system=None, verbose=None, **_):
        data = Path(self.threads_dir) / f"data_{self.system}_{thread_id}.jsonl"
        meta = Path(self.threads_dir) / f"meta_{self.system}_{thread_id}.json"
        lines: list[str] = []
        for i, msg in enumerate(messages, start=1):
            lines.append(json.dumps({
                "type": "message", "seq": i,
                "payload": {"id": f"u{i}", "role": "user", "content": msg},
            }))
        # Agent-ответ: НЕ дублируется через _append_inline; виден только
        # после того, как polling прочитает файл.
        lines.append(json.dumps({
            "type": "message", "seq": len(messages) + 1,
            "payload": {"id": "a1", "role": "assistant",
                        "content": "это TUI проект на Textual"},
        }))
        data.write_text("\n".join(lines) + "\n", encoding="utf-8")
        meta.write_text(json.dumps({
            "thread_id": thread_id,
            "updated_at": "2026-05-20T12:00:00+00:00",
            "messages_count": len(messages) + 1,
        }), encoding="utf-8")

    def stop_run(self):
        self.stopped = True

    def add_message(self, m):
        pass


def _trace_text(screen) -> str:
    trace = screen.query_one("#trace")
    return "\n".join(str(c.render()) for c in trace.query("Static"))


@pytest.mark.asyncio
async def test_task_flow_renders_agent_output(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    fake = _FakeExecutor(threads_dir=str(tmp_path), system="cli")
    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    app._executor_factory = lambda: fake

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "привет"
        await pilot.press("enter")
        # poll-tick set_interval(1.0, ...) должен сработать хотя бы раз
        await pilot.pause(1.5)

        text = _trace_text(pilot.app.screen)

    # Главное: agent-ответ существует ТОЛЬКО в data_*.jsonl,
    # в UI попадает только через polling. Если attach_active_task не вызван —
    # _poll_active выходит сразу и assistant-сообщение не появится.
    assert "это TUI проект на Textual" in text, (
        f"polling did not pick up agent message; trace=\n{text}"
    )

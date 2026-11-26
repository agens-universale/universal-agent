"""Общее тестов открытия сессии и превью в фоне (спека шага 2 окна ленты,
§3.3, §5.7): треды на диске, ход-заглушка, затвор построения модели и
ожидание загрузчика."""

from __future__ import annotations

import json
import threading

from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.services import history_loader

A = "open0000000000000000000000000000a"
B = "open0000000000000000000000000000b"
C = "open0000000000000000000000000000c"


def write_thread(tmp_path, tid, text, *, updated_at):
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        tid,
        [{"id": f"u_{tid[-1]}", "role": "user", "content": text}],
        set(),
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system": "cli",
                "status": "stopped",
                "updated_at": updated_at,
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )


def write_threads(tmp_path):
    """A, B, C; в списке B первая, C вторая, A третья."""
    write_thread(tmp_path, A, "реплика A", updated_at="2026-05-30T10:00:00Z")
    write_thread(tmp_path, B, "реплика B", updated_at="2026-05-30T12:00:00Z")
    write_thread(tmp_path, C, "реплика C", updated_at="2026-05-30T11:00:00Z")


def make_app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


class Runner:
    """Ход треда `tid`: идёт, пока его не остановят."""

    _thread_kind = "run"

    def __init__(self, tid):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.running = True
        self.stops = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self._stop_requested = True
        self.running = False

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False


async def run_in_a(pilot):
    scr = pilot.app.screen
    scr.attach_active_task(A, is_resume=True)
    scr._prepare_for_new_run()
    runner = Runner(A)
    pilot.app.active_runner = runner
    scr._poll_active()
    await pilot.pause()
    return scr, runner


class Gate:
    """Затвор построения модели в фоне: закрыт — поток ждёт; `after` —
    ждать уже после построения (результат готов, но не доставлен)."""

    def __init__(self, monkeypatch):
        self.opened = threading.Event()
        self.opened.set()
        self.after = False
        self.inputs = []
        self.models = []
        real = history_loader.build_history

        def build(segments, **kw):
            self.inputs.append(kw["inputs"])
            if not self.after:
                self.opened.wait(10)
            model = real(segments, **kw)
            self.models.append(model)
            if self.after:
                self.opened.wait(10)
            return model

        monkeypatch.setattr(history_loader, "build_history", build)

    def close(self):
        self.opened.clear()

    def open(self):
        self.opened.set()


def join_loaders():
    for t in threading.enumerate():
        if t.name == "history-loader":
            t.join(10)


def loading(app) -> bool:
    return any(
        getattr(getattr(s, "_loader", None), "busy", False)
        for s in app.screen_stack
    )


async def settle(pilot):
    """Загрузка доставлена (или выброшена), потоки кончились."""
    await wait_until(pilot, lambda: not loading(pilot.app), "загрузка")
    join_loaders()
    await pilot.pause()
    await pilot.pause()


async def open_list(pilot, tmp_path, select=B):
    from gc_tui.screens.sessions import SessionsScreen

    scr = SessionsScreen(threads_dir=str(tmp_path), system="cli")
    await pilot.app.push_screen(scr)
    await pilot.pause()
    select_in(scr, select)
    await pilot.pause()
    return scr


def select_in(scr, tid):
    page = scr._page_items()
    scr._selected_idx = next(
        i for i, t in enumerate(page) if t.thread_id == tid
    )
    scr._render_list()


def card_text(scr) -> str:
    card = scr.query_one("#sessions-list .card.selected")
    return str(card.render())


def user_texts(scr) -> list[str]:
    from gc_tui.widgets.user_reply import UserReplyContainer

    return [w.plain_text for w in scr.query(UserReplyContainer)]

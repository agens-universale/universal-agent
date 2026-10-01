"""Remote session view. No local runner, file locks or workspace journal."""

import argparse
import json
import uuid

import httpx
from gc_ua_temporalio.remote import RemoteCursor, RemoteSessionProvider
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog, Static

_HISTORY_SELECTOR = "#history"


class TemporalApp(App):
    TITLE = "Universal Agent · Temporal"
    BINDINGS = [
        ("ctrl+q", "quit", "Отключиться"),
        ("ctrl+x", "stop_turn", "Остановить ход"),
    ]
    CSS = """
    Screen { background: #101827; }
    #status { height: auto; padding: 1 2; color: #79d4c5; }
    #history { height: 1fr; border: round #334864; }
    #preview { height: auto; max-height: 8; color: #91a4bd; padding: 0 2; }
    #question { height: auto; padding: 0 2; color: #e9bc6c; }
    Input { margin: 1; }
    """

    def __init__(self, provider):
        super().__init__()
        self.provider = provider
        self.cursor = RemoteCursor()
        self.child_cursors = {}
        self.session = {}
        self.busy = False
        self.pending_message = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Подключение…", id="status")
        yield RichLog(wrap=True, markup=False, id="history")
        yield Static("", id="preview")
        yield Static("", id="question")
        yield Input(
            placeholder="Сообщение · /answer ответ · /allow · /deny · /stop · /close",
            id="message",
        )
        yield Footer()

    async def on_mount(self):
        self.set_interval(0.5, self.refresh_session)
        await self.refresh_session()
        self.query_one(Input).focus()

    async def refresh_session(self):
        if self.busy:
            return
        self.busy = True
        try:
            self.session = await self.provider.status()
            page = await self.provider.history(self.cursor.history)
            if page.get("reset"):
                self.query_one(_HISTORY_SELECTOR, RichLog).clear()
            for row in self.cursor.apply_history(page):
                message = row["message"]
                label = message.get("role") or message.get("type", "message")
                content = (
                    message.get("content")
                    or message.get("output")
                    or json.dumps(message, ensure_ascii=False)
                )
                self.query_one(_HISTORY_SELECTOR, RichLog).write(
                    f"{label}: {content}"
                )
            self.cursor.apply_events(
                await self.provider.events(self.cursor.event)
            )
            self.query_one("#status", Static).update(
                f"Сессия: {self.session['phase']}"
            )
            active = self.session.get("child") or self.session
            preview_cursor = self.cursor
            if self.session.get("child") and active.get("thread_id"):
                preview_cursor = await self.refresh_child(active)
            pending = active.get("pending_human") or {}
            self.query_one("#question", Static).update(
                pending.get("question") or pending.get("message") or ""
            )
            preview = "\n".join(preview_cursor.previews.values())
            self.query_one("#preview", Static).update(
                "Предварительный ответ: " + preview if preview else ""
            )
        except httpx.HTTPError as exc:
            self.query_one("#status", Static).update(
                f"Нет соединения / сессии ({type(exc).__name__}). Сообщение создаст новую сессию."
            )
        finally:
            self.busy = False

    async def refresh_child(self, child):
        cursor = self.child_cursors.setdefault(
            child["thread_id"], RemoteCursor()
        )
        page = await self.provider.child_page(child, "history", cursor.history)
        for row in cursor.apply_history(page):
            message = row["message"]
            content = (
                message.get("content")
                or message.get("output")
                or json.dumps(message, ensure_ascii=False)
            )
            self.query_one(_HISTORY_SELECTOR, RichLog).write(
                f"Субагент: {content}"
            )
        cursor.apply_events(
            await self.provider.child_page(child, "events", cursor.event)
        )
        return cursor

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        try:
            await self._submit(text)
            event.input.value = ""
        except (httpx.HTTPError, ValueError) as exc:
            self.notify(str(exc), severity="error")

    async def _submit(self, text):
        if text == "/stop":
            await self.action_stop_turn()
        elif text == "/close":
            await self.provider.close_session()
        elif text in ("/allow", "/deny") or text.startswith("/answer "):
            active = self.session.get("child") or self.session
            pending = active.get("pending_human")
            if not pending:
                raise ValueError("Нет ожидающего вопроса")
            answer = {"/allow": "allow_once", "/deny": "deny_once"}.get(
                text, text.removeprefix("/answer ")
            )
            await self.provider.answer(
                pending["request_id"], uuid.uuid4().hex, answer
            )
        else:
            if not self.pending_message or self.pending_message[1] != text:
                self.pending_message = (uuid.uuid4().hex, text)
            await self.provider.send(*self.pending_message)
            self.pending_message = None

    async def action_stop_turn(self):
        if self.session.get("turn_id"):
            await self.provider.stop(self.session["turn_id"])

    async def on_unmount(self):
        await self.provider.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporal-url", required=True)
    parser.add_argument("--system", default="temporal")
    parser.add_argument("--thread", required=True)
    args = parser.parse_args(argv)
    TemporalApp(
        RemoteSessionProvider(args.temporal_url, args.system, args.thread)
    ).run()
    return 0

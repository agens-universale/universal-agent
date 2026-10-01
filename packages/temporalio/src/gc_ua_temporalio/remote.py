"""Remote status/history/events provider. Closing it never stops execution."""

import os
from urllib.parse import quote

import httpx


class RemoteSessionProvider:
    def __init__(
        self,
        url,
        system,
        thread,
        *,
        token_env="GC_REMOTE_TOKEN",
        transport=None,
    ):
        headers = {}
        token = os.environ.get(token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers=headers,
            timeout=30,
            transport=transport,
        )
        self._history_tokens = {}
        self.path = (
            f"/temporal/{quote(system, safe='')}/{quote(thread, safe='')}"
        )

    async def request(self, method, endpoint, **kwargs):
        response = await self.http.request(
            method, self.path + "/" + endpoint, **kwargs
        )
        response.raise_for_status()
        return response.json()

    async def status(self):
        return await self.request("GET", "status")

    async def history(self, after=0):
        page = await self.request(
            "GET", "history", params={"after": after, **self._history_tokens}
        )
        self._history_tokens = (
            {"token": page["token"]} if page.get("token") else {}
        )
        return page

    async def events(self, after=0):
        return await self.request("GET", "events", params={"after": after})

    async def child_page(self, child, endpoint, after):
        path = f"/temporal/{quote(child['system_id'], safe='')}/{quote(child['thread_id'], safe='')}/{endpoint}"
        response = await self.http.get(path, params={"after": after})
        response.raise_for_status()
        return response.json()

    async def send(self, message_id, text):
        return await self.request(
            "POST", "messages", json={"message_id": message_id, "text": text}
        )

    async def answer(self, request_id, reply_id, answer):
        return await self.request(
            "POST",
            "answer",
            json={
                "request_id": request_id,
                "reply_id": reply_id,
                "answer": answer,
            },
        )

    async def stop(self, turn_id):
        return await self.request("POST", "stop", json={"turn_id": turn_id})

    async def close_session(self):
        return await self.request("POST", "close")

    async def aclose(self):
        await self.http.aclose()


class RemoteCursor:
    """Deduplicate replayed pages; never combine previews from different attempts."""

    def __init__(self):
        self.event = 0
        self.history = 0
        self.previews = {}
        self.attempts = {}
        self.committed = set()

    def apply_events(self, values):
        for event in values:
            if event["cursor"] <= self.event:
                continue
            self.event = event["cursor"]
            op, attempt = event["operation"], event["attempt"]
            if event["kind"] == "committed":
                self.committed.add(op)
                self.previews.pop(op, None)
            elif op not in self.committed:
                self._preview(event, op, attempt)

    def _preview(self, event, op, attempt):
        if attempt < self.attempts.get(op, 0):
            return
        if attempt > self.attempts.get(op, 0) or event["kind"] == "reset":
            self.previews.pop(op, None)
        self.attempts[op] = attempt
        if event["kind"] == "preview":
            self.previews[op] = (
                self.previews.get(op, "") + event["payload"]["text"]
            )[-16000:]

    def apply_history(self, page):
        if page.get("reset"):
            self.history = 0
        fresh = [row for row in page["messages"] if row["seq"] > self.history]
        self.history = max(self.history, page["after"])
        return fresh

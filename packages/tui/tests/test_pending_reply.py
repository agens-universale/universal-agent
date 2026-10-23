"""Pending user-reply tracking + reconciliation."""

from gc_testkit.tui_support import tui_settings
import json
import threading
from pathlib import Path

import pytest


def _read_pending(tmp_path, tid):
    p = Path(tmp_path) / f"meta_cli_{tid}.json"
    if not p.exists():
        return []
    meta = json.loads(p.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("pending_user_messages", [])


def _core_meta(tmp_path, tid, **fields):
    """Запись меты исполнителем: секция `tui` принадлежит TUI и остаётся
    (владение ключами меты), как у настоящего ядра."""
    p = Path(tmp_path) / f"meta_cli_{tid}.json"
    meta = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    meta.update({"thread_id": tid, **fields})
    p.write_text(json.dumps(meta), encoding="utf-8")


@pytest.mark.asyncio
async def test_send_records_pending_in_meta(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            _core_meta(tmp_path, thread_id, messages_count=0)
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "приветище"
        await pilot.press("enter")
        await pilot.pause(0.3)

        tid = app.tui_state.active_task_id
        assert tid is not None
        records = _read_pending(tmp_path, tid)
        assert len(records) == 1
        assert records[0]["content"] == "приветище"
        assert records[0]["status"] == "pending"
        assert records[0]["client_id"]


@pytest.mark.asyncio
async def test_pending_widget_mounted_with_pending_state(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            _core_meta(tmp_path, thread_id, messages_count=0)
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "пендинг"
        await pilot.press("enter")
        await pilot.pause(0.3)

        replies = pilot.app.query(UserReplyContainer)
        assert len(replies) == 1
        assert replies[0].state == "pending"


@pytest.mark.asyncio
async def test_polling_user_event_promotes_pending(tmp_path):
    """Polling видит свою же реплику в .jsonl → промоут pending → normal, запись удалена."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _Fake:
        """Stub-executor, который реально пишет user-сообщение в .jsonl."""

        async def arun(self, messages, *, thread_id, **kw):
            import time

            time.sleep(
                0.4
            )  # pending mount до появления .jsonl (auto-create гонка)
            data_path = Path(tmp_path) / f"data_cli_{thread_id}.jsonl"
            meta_path = Path(tmp_path) / f"meta_cli_{thread_id}.json"
            data_path.write_text(
                json.dumps(
                    {
                        "type": "message",
                        "seq": 1,
                        "ts_utc": "2026-05-26T00:00:00+00:00",
                        "payload": {
                            "id": "u1",
                            "role": "user",
                            "content": messages[0],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            meta_path.write_text(
                json.dumps({"thread_id": thread_id, "messages_count": 1}),
                encoding="utf-8",
            )
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "промо"
        await pilot.press("enter")
        from gc_testkit.tui_support import wait_until

        def _promoted():
            replies = pilot.app.query(UserReplyContainer)
            return len(replies) == 1 and replies[0].state == "normal"

        await wait_until(
            pilot, _promoted, "pending промоутился в normal без дубликата"
        )

        replies = pilot.app.query(UserReplyContainer)
        assert len(replies) == 1, (
            "Не должно быть дубликата — pending промоутится in-place"
        )
        assert replies[0].state == "normal"

        tid = app.tui_state.active_task_id

        await wait_until(
            pilot,
            lambda: _read_pending(tmp_path, tid) == [],
            "pending-запись удалена из meta",
        )
        records = _read_pending(tmp_path, tid)
        assert records == [], "После promotion запись из meta удалена"


@pytest.mark.asyncio
async def test_pending_cap_drops_oldest(tmp_path):
    """В meta хранится максимум 20 pending-записей.

    Без таймингов: 3-сек. wait в фейке истекал под нагрузкой посреди цикла,
    следующий сабмит рестартовал run, и фейк перетирал meta (и накопленный
    pending-список). Здесь run живёт до stop_run, а вместо слепых пауз —
    ожидания наблюдаемых условий (meta записана; запись msg-i появилась).
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            _core_meta(tmp_path, thread_id, messages_count=0)
            self._evt = threading.Event()
            self._evt.wait(timeout=30.0)

        def stop_run(self):
            if getattr(self, "_evt", None):
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    fake = _Fake()
    app._executor_factory = lambda: fake
    async with app.run_test() as pilot:

        async def wait_until(cond, what):
            for _ in range(200):
                if cond():
                    return
                await pilot.pause(0.05)
            raise AssertionError(f"timeout: {what}")

        inp = pilot.app.query_one("#cmdinput")
        # Первое сообщение создаёт задачу и запускает runner; ждём записи meta
        # фейком — после неё run блокируется до stop_run, runner жив весь цикл,
        # и все следующие сабмиты идут add_message-путём (meta не перетирается).
        inp.text = "msg-0"
        await pilot.press("enter")
        await pilot.pause()
        tid = app.tui_state.active_task_id
        assert tid is not None
        meta_path = Path(tmp_path) / f"meta_cli_{tid}.json"
        await wait_until(meta_path.exists, "executor записал meta")

        for i in range(1, 22):
            inp.text = f"msg-{i}"
            await pilot.press("enter")
            await wait_until(
                lambda i=i: any(
                    r["content"] == f"msg-{i}"
                    for r in _read_pending(tmp_path, tid)
                ),
                f"pending-запись msg-{i}",
            )

        records = _read_pending(tmp_path, tid)
        assert len(records) == 20
        # Самые свежие сохранены; самые старые отброшены.
        contents = [r["content"] for r in records]
        assert "msg-21" in contents
        assert "msg-0" not in contents
        fake.stop_run()


@pytest.mark.asyncio
async def test_failed_run_marks_pending_widget_and_meta(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            _core_meta(tmp_path, thread_id, messages_count=0)
            raise RuntimeError("model unavailable")

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "упади"
        await pilot.press("enter")
        await pilot.pause(0.5)  # polling должен поймать last_runner_error

        replies = pilot.app.query(UserReplyContainer)
        assert len(replies) == 1
        assert replies[0].state == "failed"

        tid = app.tui_state.active_task_id
        records = _read_pending(tmp_path, tid)
        assert len(records) == 1
        assert records[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_error_shown_resets_between_runs(tmp_path):
    """Две ошибки подряд → у каждой своя причина под «✕ Ошибка» свежей
    строки итога (флаг сбрасывается): `└ <класс>: <текст>`."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.widgets.turn_line import TurnLine

    def fresh_text(scr):
        lines = [w for w in scr.query(TurnLine) if w.is_fresh]
        return lines[0].render().plain if len(lines) == 1 else ""

    call = {"n": 0}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            call["n"] += 1
            _core_meta(tmp_path, thread_id, messages_count=0)
            raise RuntimeError(f"fail #{call['n']}")

        def stop_run(self):
            pass

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        scr = pilot.app.screen
        inp.text = "первая"
        await pilot.press("enter")
        await wait_until(pilot, lambda: "fail #1" in fresh_text(scr), "#1")
        assert fresh_text(scr).endswith("└ RuntimeError: fail #1")

        inp.text = "вторая"
        await pilot.press("enter")
        await wait_until(pilot, lambda: "fail #2" in fresh_text(scr), "#2")
        assert fresh_text(scr).endswith("└ RuntimeError: fail #2")


@pytest.mark.asyncio
async def test_restart_renders_failed_from_meta(tmp_path):
    """На attach_active_task pending-записи из meta монтируются как failed-виджеты,
    pending → failed нормализация в meta."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    tid = "restored"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-26T12:00:00+00:00",
                "messages_count": 0,
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "a",
                            "content": "потерянный",
                            "wire_content": "потерянный",
                            "ts_utc_iso": "2026-05-26T11:59:00+00:00",
                            "status": "pending",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

        replies = pilot.app.query(UserReplyContainer)
        assert len(replies) == 1
        assert replies[0].state == "failed"

        # pending → failed в meta
        meta = json.loads(
            (Path(tmp_path) / f"meta_cli_{tid}.json").read_text(
                encoding="utf-8"
            )
        )
        records = meta["tui"]["pending_user_messages"]
        assert records[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_restart_with_only_failed_pendings_no_normalize_write(tmp_path):
    """Если все pending уже failed — повторной записи в meta не делаем (избежать noise-update)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "restored2"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    initial_meta = {
        "thread_id": tid,
        "status": "stopped",
        "updated_at": "2026-05-26T12:00:00+00:00",
        "messages_count": 0,
        "tui": {
            "pending_user_messages": [
                {
                    "client_id": "a",
                    "content": "x",
                    "wire_content": "x",
                    "ts_utc_iso": "2026-05-26T11:59:00+00:00",
                    "status": "failed",
                }
            ]
        },
    }
    meta_path = Path(tmp_path) / f"meta_cli_{tid}.json"
    meta_path.write_text(json.dumps(initial_meta), encoding="utf-8")
    mtime_before = meta_path.stat().st_mtime_ns

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.3)

    mtime_after = meta_path.stat().st_mtime_ns
    assert mtime_after == mtime_before, (
        "Meta не должна перезаписываться, если нечего нормализовать"
    )


@pytest.mark.asyncio
async def test_mount_failed_skips_delivered_via_attach(tmp_path):
    """Pending запись в meta + соответствующий kind=user в .jsonl → запись
    считается доставленной, виджет НЕ монтируется, запись удаляется из meta."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "delivered000000000000000000000a"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    # .jsonl: user-message "ok-text" уже в трассе (зеркальное отображение того,
    # что прошлая сессия TUI отправила агенту и тот зеркалил в history).
    with open(
        threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-05-29T10:00:00+00:00",
                    "payload": {"role": "user", "content": "ok-text"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    # meta: pending record с тем же wire_content — orphan из прошлой сессии.
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-29T10:00:00+00:00",
                "messages_count": 1,
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "old-cid",
                            "content": "ok-text",
                            "wire_content": "ok-text",
                            "status": "failed",
                            "ts_utc_iso": "2026-05-29T09:55:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        # Ровно 1 user-reply (из .jsonl trace), без failed-дубля.
        replies = list(screen.query(UserReplyContainer))
        assert len(replies) == 1, (
            f"должно быть 1 user-reply (из trace), got {len(replies)}"
        )
        assert replies[0]._state == "normal", (
            f"должен быть state=normal, got {replies[0]._state}"
        )
        # meta очищена.
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert not pum, (
            f"pending_user_messages должен быть пуст после cross-check, got {pum}"
        )


@pytest.mark.asyncio
async def test_mount_failed_renders_truly_orphaned(tmp_path):
    """Pending запись в meta без соответствующего user-event в .jsonl →
    truly orphan, монтируется как failed-widget (current behaviour preserved)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "orphan000000000000000000000000a"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    # .jsonl пустой — никаких user-message в trace.
    (threads_dir / f"data_cli_{thread_id}.jsonl").write_text(
        "", encoding="utf-8"
    )
    # meta: pending record с текстом, которого нет в trace.
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-29T10:00:00+00:00",
                "messages_count": 0,
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "orphan-cid",
                            "content": "lost-message",
                            "wire_content": "lost-message",
                            "status": "pending",
                            "ts_utc_iso": "2026-05-29T09:55:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        # 1 failed user-reply (truly orphan — нет соответствующего user-event).
        replies = list(screen.query(UserReplyContainer))
        assert len(replies) == 1
        assert replies[0]._state == "failed"
        # meta нормализована: pending → failed.
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert len(pum) == 1
        assert pum[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_mount_failed_skips_delivered_via_user_messages_log(tmp_path):
    """Pending запись для сообщения которое было доставлено в ПРОШЛОЙ сессии
    (до summarization) → в live .jsonl его уже нет, но user_messages_log
    в meta его помнит → cross-check корректно считает доставленным."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "delivered-via-log0000000000000000"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    # Live .jsonl: только summary-message + некий пост-summary user-message.
    # Сообщение "delivered-old-msg" в живом .jsonl НЕТ (оно было до summary-прохода).
    with open(
        threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-05-29T10:00:00+00:00",
                    "payload": {
                        "role": "user",
                        "content": '<technical type="dialog_summary">summary</technical>',
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 2,
                    "ts": "2026-05-29T10:01:00+00:00",
                    "payload": {"role": "user", "content": "post-summary-msg"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    # meta:
    # - user_messages_log содержит ВСЕ сообщения треда (включая старое delivered-old-msg);
    # - pending_user_messages содержит запись для delivered-old-msg (orphan
    #   от того, что polling не успел отчистить до summary).
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-29T10:01:00+00:00",
                "messages_count": 2,
                "user_messages_log": [
                    "delivered-old-msg",
                    "post-summary-msg",
                ],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "old-cid",
                            "content": "delivered-old-msg",
                            "wire_content": "delivered-old-msg",
                            "status": "failed",
                            "ts_utc_iso": "2026-05-29T09:55:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        # Виджеты в трейсе — только то что в .jsonl: 1 summary user-message
        # (рендерится трейсом, синтетический), 1 post-summary-msg.
        # delivered-old-msg НЕ должен появиться как failed (cross-check через
        # user_messages_log распознал доставку).
        replies = list(screen.query(UserReplyContainer))
        texts = [r._text for r in replies]
        assert "delivered-old-msg" not in texts, (
            f"delivered-old-msg должно быть отсечено через user_messages_log, got: {texts}"
        )
        # meta: pending очищен.
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert not pum, (
            f"pending должен быть очищен через user_messages_log, got: {pum}"
        )


@pytest.mark.asyncio
async def test_mount_failed_skips_delivered_via_user_intent_log(tmp_path):
    """Как legacy-вариант, но доставка распознаётся через intent-модель master
    meta.user_intent_log (user_ask записи) — без user_messages_log в meta."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "delivered-via-intent000000000000"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    with open(
        threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-05-29T10:00:00+00:00",
                    "payload": {
                        "role": "user",
                        "content": '<technical type="dialog_summary">summary</technical>',
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 2,
                    "ts": "2026-05-29T10:01:00+00:00",
                    "payload": {"role": "user", "content": "post-summary-msg"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    # meta: вместо user_messages_log — user_intent_log (intent-модель master).
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-05-29T10:01:00+00:00",
                "messages_count": 2,
                "user_intent_log": [
                    {
                        "turn": 1,
                        "ts": "2026-05-29T09:55:00+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "delivered-old-msg",
                    },
                    {
                        "turn": 2,
                        "ts": "2026-05-29T10:01:00+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "post-summary-msg",
                    },
                ],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "old-cid",
                            "content": "delivered-old-msg",
                            "wire_content": "delivered-old-msg",
                            "status": "failed",
                            "ts_utc_iso": "2026-05-29T09:55:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        replies = list(screen.query(UserReplyContainer))
        texts = [r._text for r in replies]
        assert "delivered-old-msg" not in texts, (
            f"delivered-old-msg должно быть отсечено через user_intent_log, got: {texts}"
        )
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert not pum, (
            f"pending должен быть очищен через user_intent_log, got: {pum}"
        )


@pytest.mark.asyncio
async def test_mount_failed_drops_superseded_by_later_delivered(tmp_path):
    """Failed pending, которое пользователь оставил позади (есть доставленный
    user_ask с ts ПОЗЖЕ него), устарело → НЕ монтируется и вычищено из meta.
    Репро реального кейса: 'опиши проект' (failed) → 'опиши этот проект' (доставлено)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "superseded000000000000000000000a"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    with open(
        threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-06-04T20:40:37+00:00",
                    "payload": {
                        "role": "user",
                        "content": "опиши этот проект",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-06-04T20:44:30+00:00",
                "messages_count": 1,
                "user_intent_log": [
                    {
                        "turn": 7,
                        "ts": "2026-06-04T20:40:37+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "опиши этот проект",
                    },
                    {
                        "turn": 8,
                        "ts": "2026-06-04T20:41:11+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "что рекомендуешь?",
                    },
                ],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "stale-cid",
                            "content": "опиши проект",
                            "wire_content": "опиши проект",
                            "status": "failed",
                            "ts_utc_iso": "2026-06-04T20:40:04.238548+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        texts = [r._text for r in screen.query(UserReplyContainer)]
        assert "опиши проект" not in texts, (
            f"устаревшее failed не должно висеть, got: {texts}"
        )
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert not pum, (
            f"устаревшее failed должно быть вычищено из meta, got: {pum}"
        )


@pytest.mark.asyncio
async def test_mount_failed_keeps_latest_failed_after_delivered(tmp_path):
    """Failed pending НОВЕЕ последнего доставленного (последнее действие юзера
    упало) → остаётся показанным для retry, не дропается (страж от передроба)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread_id = "latestfailed00000000000000000000"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    with open(
        threads_dir / f"data_cli_{thread_id}.jsonl", "w", encoding="utf-8"
    ) as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": thread_id,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-06-04T20:40:00+00:00",
                    "payload": {"role": "user", "content": "ранее доставлено"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-06-04T20:41:00+00:00",
                "messages_count": 1,
                "user_intent_log": [
                    {
                        "turn": 1,
                        "ts": "2026-06-04T20:40:00+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "ранее доставлено",
                    },
                ],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "last-cid",
                            "content": "последнее упало",
                            "wire_content": "последнее упало",
                            "status": "failed",
                            "ts_utc_iso": "2026-06-04T20:41:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()
        replies = list(screen.query(UserReplyContainer))
        texts = [r._text for r in replies]
        assert "последнее упало" in texts, (
            f"последнее failed должно остаться для retry, got: {texts}"
        )
        meta = screen._store.get_task_meta(thread_id)
        pum = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert any(r.get("content") == "последнее упало" for r in pum), (
            f"последнее failed должно сохраниться в meta, got: {pum}"
        )

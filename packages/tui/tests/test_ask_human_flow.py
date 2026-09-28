"""ask_human flow: detection → banner mount → user answer → banner unmount."""

import json

import pytest
from gc_testkit.tui_fixtures.stub_executor import StubExecutor


class _StubLiveRunner:
    """Минимальный runner-stub, который представляется живым (`is_running() == True`),
    чтобы Fix D recovery-блок в attach_active_task НЕ сработал и banner остался."""

    def __init__(self) -> None:
        self.stop_called = 0
        self.cancel_called = 0

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, text: str) -> None:
        pass

    def cancel_ask_human(self) -> None:
        self.cancel_called += 1

    def stop(self) -> None:
        self.stop_called += 1

    def add_message(self, message: dict) -> None:
        pass


@pytest.mark.asyncio
async def test_ask_human_banner_appears_and_disappears(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.ask_human_banner import AskHumanBanner
    from gc_tui.widgets.trace_row import TraceRow

    tid = "ah_t1"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="ask_human_flow"
    )
    fake.run(["используй скилл skill-creator"], thread_id=tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions"},
    )
    app._executor_factory = lambda: fake
    # Fix D: без живого runner-а recovery-блок снял бы banner. Здесь мы тестируем
    # live-flow → подсовываем live-stub.
    app.active_runner = _StubLiveRunner()

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.5)

        banner = pilot.app.query(AskHumanBanner)
        assert len(banner) == 1
        rows = list(banner[0].query(TraceRow))
        assert rows, "баннер должен смонтировать блок-виджеты"
        joined = " ".join(
            (
                r._content.plain
                if hasattr(r._content, "plain")
                else str(r._content)
            )
            for r in rows
        )
        assert "Какого типа отчёт" in joined
        # Пока плашка видна — трейс-строка вопроса смонтирована, но скрыта
        # (без дубля на экране; позиция выше будущего ответа уже зафиксирована).
        q_row = scr.query_one(".ask-human-question")
        assert q_row.has_class("hidden")

        # user answers
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "A"
        await pilot.press("enter")
        await pilot.pause(0.2)

        # Simulate function_call_output for ask_human arriving → banner unmounts
        import json as _json
        from pathlib import Path

        data = Path(tmp_path) / f"data_cli_{tid}.jsonl"
        with open(data, "a", encoding="utf-8") as f:
            f.write(
                _json.dumps(
                    {
                        "type": "message",
                        "seq": 99,
                        "ts_utc": "2026-05-22T12:00:10+00:00",
                        "payload": {
                            "id": "fco_ah",
                            "type": "function_call_output",
                            "call_id": "c2",
                            "name": "ask_human",
                            "output": _json.dumps(
                                {
                                    "tool": "ask_human",
                                    "ok": True,
                                    "question": "...",
                                    "response": "A",
                                }
                            ),
                        },
                    }
                )
                + "\n"
            )
        await pilot.pause(0.5)

        banner = pilot.app.query(AskHumanBanner)
        assert len(banner) == 0
        # Плашка схлопнулась → строка вопроса раскрылась: история диалога
        # полная, ответ юзера не висит без контекста.
        q_row = scr.query_one(".ask-human-question")
        assert not q_row.has_class("hidden")
        trace_text = " ".join(
            f"{w._prefix}{w._content}" for w in scr.query(TraceRow)
        )
        assert (
            "ask_human:" not in trace_text
        )  # тех-префикс убран — серая рамка с «? »
        assert "? " in trace_text
        assert "Какого типа отчёт" in trace_text


@pytest.mark.asyncio
async def test_replay_unanswered_question_does_not_swallow_later_ones(
    tmp_path,
):
    """Цепочка ask_human, где промежуточный вопрос БЕЗ output (модель
    переформулировала, не дождавшись ответа): баннер не должен застревать и
    глотать последующие вопросы — каждый оседает своей dim-строкой."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    tid = "ahchain0000000000000000000000001"
    recs = [
        {"id": "u1", "role": "user", "content": "старт"},
        # Q1 без output (переформулирован ниже).
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "c1",
            "name": "turn_ask_human",
            "arguments": json.dumps(
                {"question": "ПЕРВЫЙ вопрос?"}, ensure_ascii=False
            ),
        },
        # Q2 с output.
        {
            "id": "fc2",
            "type": "function_call",
            "call_id": "c2",
            "name": "turn_ask_human",
            "arguments": json.dumps(
                {"question": "ВТОРОЙ вопрос?"}, ensure_ascii=False
            ),
        },
        {
            "id": "fco2",
            "type": "function_call_output",
            "call_id": "c2",
            "name": "turn_ask_human",
            "output": json.dumps(
                {
                    "tool": "turn_ask_human",
                    "ok": True,
                    "question": "ВТОРОЙ вопрос?",
                    "response": "ответ2",
                },
                ensure_ascii=False,
            ),
        },
        # Q3 с output.
        {
            "id": "fc3",
            "type": "function_call",
            "call_id": "c3",
            "name": "turn_ask_human",
            "arguments": json.dumps(
                {"question": "ТРЕТИЙ вопрос?"}, ensure_ascii=False
            ),
        },
        {
            "id": "fco3",
            "type": "function_call_output",
            "call_id": "c3",
            "name": "turn_ask_human",
            "output": json.dumps(
                {
                    "tool": "turn_ask_human",
                    "ok": True,
                    "question": "ТРЕТИЙ вопрос?",
                    "response": "ответ3",
                },
                ensure_ascii=False,
            ),
        },
    ]
    lines = [
        json.dumps(
            {
                "type": "message",
                "seq": i + 1,
                "ts_utc": f"2026-06-10T23:00:{i:02d}+00:00",
                "payload": p,
            },
            ensure_ascii=False,
        )
        for i, p in enumerate(recs)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "2026-06-10T23:00:09+00:00",
                "messages_count": len(recs),
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.3)
        trace_text = " ".join(
            f"{w._prefix}{w._content}" for w in scr.query(TraceRow)
        )
        # Все три вопроса видны — ни один не проглочен застрявшим баннером.
        assert "ПЕРВЫЙ вопрос?" in trace_text
        assert "ВТОРОЙ вопрос?" in trace_text
        assert "ТРЕТИЙ вопрос?" in trace_text
        # Все строки вопросов раскрыты (баннера живого нет).
        q_rows = list(scr.query(".ask-human-question"))
        assert len(q_rows) == 3
        assert all(not r.has_class("hidden") for r in q_rows)


@pytest.mark.asyncio
async def test_replay_answered_question_keeps_question_row(tmp_path):
    """История с отвеченным ask_human: вопрос виден серой рамкой с «? …» (без ask_human:),
    баннера нет — диалог читается целиком (вопрос + ответ), live и replay одинаковы."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    tid = "ahq00000000000000000000000000001"
    recs = [
        {"id": "u1", "role": "user", "content": "поехали"},
        {
            "id": "fc1",
            "type": "function_call",
            "call_id": "c1",
            "name": "ask_human",
            "arguments": json.dumps(
                {"question": "Какой вариант — A или B?"}, ensure_ascii=False
            ),
        },
        {
            "id": "fco1",
            "type": "function_call_output",
            "call_id": "c1",
            "name": "ask_human",
            "output": json.dumps(
                {
                    "tool": "ask_human",
                    "ok": True,
                    "question": "Какой вариант — A или B?",
                    "response": "A",
                },
                ensure_ascii=False,
            ),
        },
    ]
    lines = [
        json.dumps(
            {
                "type": "message",
                "seq": i + 1,
                "ts_utc": f"2026-06-10T21:00:0{i}+00:00",
                "payload": p,
            },
            ensure_ascii=False,
        )
        for i, p in enumerate(recs)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "2026-06-10T21:00:09+00:00",
                "messages_count": 3,
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.3)
        assert scr._ask_human_banner is None
        assert not scr.query_one(".ask-human-question").has_class("hidden")
        trace_text = " ".join(
            f"{w._prefix}{w._content}" for w in scr.query(TraceRow)
        )
        assert "ask_human:" not in trace_text  # тех-префикс убран
        assert "? Какой вариант — A или B?" in trace_text


@pytest.mark.asyncio
async def test_ask_human_esc_clean_stops(tmp_path):
    """Esc в live ask_human делает clean stop треда (а не просто cancel).

    Контракт (после Task 1, Esc-semantics changed from 'empty answer' to 'clean stop'):
    - runner.stop() вызван (= state.stop_requested в реальном executor).
    - runner.cancel_ask_human() вызван (= stdin unblock).
    - Banner снят (screen._ask_human_banner is None).
    - tui_state.ask_human_state сброшен в None.
    - app.active_runner = None (clean-restart pattern)."""
    pytest.importorskip("textual")
    from gc_testkit.tui_fixtures.stub_executor import StubExecutor
    from gc_tui.app import TuiApp
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    tid = "ah_esc"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="ask_human_flow"
    )
    fake.run(["используй скилл skill-creator"], thread_id=tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions"},
    )
    app._executor_factory = lambda: fake
    # Fix D: установить live-runner, чтобы recovery в attach_active_task не снял
    # banner. Тестируем Esc по живому ask_human.
    runner = _StubLiveRunner()
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.5)
        assert len(pilot.app.query(AskHumanBanner)) == 1

        await pilot.press("escape")
        await pilot.pause(0.1)

        # Full clean-stop contract:
        assert runner.stop_called == 1
        assert runner.cancel_called == 1
        assert scr._ask_human_banner is None
        assert app.tui_state.ask_human_state is None
        assert app.active_runner is None


@pytest.mark.asyncio
async def test_ask_human_esc_closes_open_overlay_first(tmp_path):
    """Esc при открытом оверлее поверх ask_human: приоритет у оверлея.

    Контракт: первый Esc закрывает палитру команд и НЕ трогает вопрос агента
    (runner.stop не вызван, banner жив); второй Esc — штатный clean stop."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    tid = "ah_overlay"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="ask_human_flow"
    )
    fake.run(["используй скилл skill-creator"], thread_id=tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions"},
    )
    app._executor_factory = lambda: fake
    runner = _StubLiveRunner()
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.5)
        assert len(pilot.app.query(AskHumanBanner)) == 1

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/s"  # открывает палитру команд
        await pilot.pause(0.2)
        assert scr._palette is not None

        await pilot.press("escape")
        await pilot.pause(0.1)
        # Первый Esc: оверлей закрыт, вопрос агента жив.
        assert scr._palette is None
        assert runner.stop_called == 0
        assert app.tui_state.ask_human_state is not None
        assert len(pilot.app.query(AskHumanBanner)) == 1

        await pilot.press("escape")
        await pilot.pause(0.1)
        # Второй Esc: штатный clean stop ask_human.
        assert runner.stop_called == 1
        assert app.tui_state.ask_human_state is None


@pytest.mark.asyncio
async def test_ask_human_esc_with_text_clears_input_first(tmp_path):
    """Esc-лестница: текст в инпуте приоритетнее ask_human.

    Esc при тексте → clear-hint, вопрос жив; Esc-Esc чистит текст;
    Esc при пустом поле — только тогда clean stop вопроса."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    tid = "ah_text"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="ask_human_flow"
    )
    fake.run(["используй скилл skill-creator"], thread_id=tid)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions"},
    )
    app._executor_factory = lambda: fake
    runner = _StubLiveRunner()
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.5)
        assert len(pilot.app.query(AskHumanBanner)) == 1

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "наполовину набранный ответ"
        await pilot.press("escape")
        await pilot.pause(0.1)
        # Текст приоритетнее: clear-hint, вопрос не тронут.
        assert scr._esc_confirm_kind == "clear"
        assert runner.stop_called == 0
        assert app.tui_state.ask_human_state is not None

        await pilot.press("escape")
        await pilot.pause(0.1)
        # Текст очищен, вопрос всё ещё жив.
        assert inp.text == ""
        assert runner.stop_called == 0
        assert app.tui_state.ask_human_state is not None

        await pilot.press("escape")
        await pilot.pause(0.1)
        # Пустое поле → теперь clean stop вопроса.
        assert runner.stop_called == 1
        assert app.tui_state.ask_human_state is None


@pytest.mark.asyncio
async def test_ask_human_body_renders_markdown():
    """Баннер монтирует блок-виджеты: markdown отрисован, без маркера-реплики `●`."""
    pytest.importorskip("textual")
    from gc_tui.widgets.ask_human_banner import AskHumanBanner
    from gc_tui.widgets.trace_row import TraceRow
    from textual.app import App

    class _A(App):
        def compose(self):
            yield AskHumanBanner(question="## Вопрос\n**жирный** и `код`")

    async with _A().run_test() as pilot:
        rows = list(pilot.app.screen.query(TraceRow))
        assert rows, "баннер должен смонтировать блок-виджеты прозы"
        assert all(r._prefix != "● " for r in rows)  # нет маркера-реплики
        joined = " ".join(
            (
                r._content.plain
                if hasattr(r._content, "plain")
                else str(r._content)
            )
            for r in rows
        )
        assert "Вопрос" in joined and "жирный" in joined and "код" in joined
        assert "##" not in joined and "**" not in joined and "`" not in joined


@pytest.mark.asyncio
async def test_ask_human_table_renders():
    """Таблица в вопросе ask_human → виджет .table-block."""
    pytest.importorskip("textual")
    from gc_tui.widgets.ask_human_banner import AskHumanBanner
    from gc_tui.widgets.block_panel import BlockPanel
    from textual.app import App

    q = "Сравни:\n\n| A | B |\n|---|---|\n| 1 | 2 |"

    class _A(App):
        def compose(self):
            yield AskHumanBanner(question=q)

    async with _A().run_test() as pilot:
        tables = [
            w
            for w in pilot.app.screen.query(BlockPanel)
            if w.has_class("table-block")
        ]
        assert len(tables) == 1

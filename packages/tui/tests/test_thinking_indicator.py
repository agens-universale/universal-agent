"""ThinkingIndicator — анимация + таймер (без hint про Esc)."""

from gc_testkit.tui_support import tui_settings
import time

import pytest


def test_render_contains_label_only():
    """Индикатор показывает 'думаю…' + spinner + timer — без action-hint'а.

    Подсказку про Esc-остановку даёт footer-right при реальном нажатии."""
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator()
    rendered = str(w.render())
    assert "думаю…" in rendered
    assert "прервать" not in rendered  # hint убран
    assert "esc" not in rendered.lower()


def test_render_includes_elapsed_seconds_marker():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator()
    # Сдвинем started_at в прошлое — чтобы elapsed был ≥ 2 секунд.
    w._started_at = time.monotonic() - 2.5
    rendered = str(w.render())
    assert "(2с)" in rendered or "(3с)" in rendered  # допускаем оба округления


def test_tick_advances_frame_index():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator()
    f0 = w._frame
    w._tick()
    f1 = w._frame
    w._tick()
    f2 = w._frame
    assert f1 != f0
    assert f2 != f1


def test_validating_state_renders_check_mark_and_label():
    """Validating-режим: статичная искра ✦ + 'завершаю' + анимированные точки + таймер."""
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator(state="validating")
    rendered = str(w.render())
    assert "завершаю" in rendered
    assert "✦" in rendered
    assert "думаю" not in rendered  # не overlap режимов


def test_set_state_switches_between_thinking_and_validating():
    """set_state переключает визуал без размонтирования."""
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator(state="thinking")
    assert "думаю…" in str(w.render())

    w.set_state("validating")
    assert "завершаю" in str(w.render())
    assert "думаю" not in str(w.render())

    w.set_state("thinking")
    assert "думаю…" in str(w.render())
    assert "завершаю" not in str(w.render())


def test_phrase_rotator_starts_with_first_phrase():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import PhraseRotator

    r = PhraseRotator(["думаю", "размышляю"], clock=lambda: 0.0)
    assert r.current() == "думаю"


def test_phrase_rotator_stable_within_period():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import PhraseRotator

    t = [0.0]
    r = PhraseRotator(
        ["думаю", "размышляю", "соображаю"], period=3.0, clock=lambda: t[0]
    )
    assert r.current() == "думаю"
    t[0] = 2.9
    assert r.current() == "думаю"


def test_phrase_rotator_changes_after_period_and_differs():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import PhraseRotator

    class _RNG:
        def choice(self, seq):
            return seq[0]

    t = [0.0]
    r = PhraseRotator(
        ["думаю", "размышляю", "соображаю"],
        period=3.0,
        rng=_RNG(),
        clock=lambda: t[0],
    )
    assert r.current() == "думаю"
    t[0] = 3.0
    nxt = r.current()
    assert nxt != "думаю"
    assert nxt in ("размышляю", "соображаю")


def test_thinking_phrases_pool_starts_with_dumayu():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import THINKING_PHRASES

    assert THINKING_PHRASES[0] == "думаю"
    assert len(THINKING_PHRASES) >= 8


def test_morph_frames_constant_shape():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import MORPH_FRAMES

    assert MORPH_FRAMES[0] == "·"
    assert len(MORPH_FRAMES) >= 4
    # Компас × искра: курируемый набор глифов (narrow-искра + ambiguous-width
    # треугольники). Внутри цикла ширина консистентна — строка не дёргается.
    assert all(
        c in {"·", "+", "✧", "✦", "✶", "▴", "▸", "▾", "◂"}
        for c in MORPH_FRAMES
    )


def test_thinking_render_uses_morph_glyph_for_frame():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import (
        FRAME_HOLD,
        MORPH_FRAMES,
        ThinkingIndicator,
    )

    w = ThinkingIndicator()
    w._frame = 4
    rendered = str(w.render())
    # Глиф держится FRAME_HOLD тиков — индекс кадра считаем с учётом hold.
    expected = MORPH_FRAMES[(4 // FRAME_HOLD) % len(MORPH_FRAMES)]
    assert expected in rendered
    assert "думаю…" in rendered  # фраза (старт пула)


def test_thinking_glyph_uses_glow_shade_color():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import (
        GLOW_SHADES,
        ThinkingIndicator,
    )

    w = ThinkingIndicator()
    w._frame = 0
    t = w._render_text()
    # Первый span — глиф спиннера, покрашен оттенком из рампы дыхания.
    assert t.spans[0].style == GLOW_SHADES[0]


def test_validating_still_renders_zavershayu():
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator(state="validating")
    assert "завершаю…" in str(w.render())


def test_validating_spark_breathes():
    """«завершаю…»: искра ✦ статична по форме (без ✧), а её цвет «дышит» по
    кадрам — серая рампа (та же техника, что пульс running-глифа ▸)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import (
        _VALIDATING_SHADES,
        ThinkingIndicator,
    )

    def spark_color(w):
        t = w._render_text()
        return next(
            str(s.style) for s in t.spans if "✦" in t.plain[s.start : s.end]
        )

    w = ThinkingIndicator(state="validating")
    w._frame = 0
    rendered = str(w.render())
    assert "✦" in rendered and "✧" not in rendered  # форма не меняется
    c0 = spark_color(w)
    w._frame = 5
    c5 = spark_color(w)
    assert c0 != c5  # цвет «дышит» между кадрами
    assert c0 in _VALIDATING_SHADES and c5 in _VALIDATING_SHADES


def test_set_state_ignores_unknown_value():
    """set_state с неизвестным state — no-op."""
    pytest.importorskip("textual")
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    w = ThinkingIndicator(state="thinking")
    w.set_state("garbage")
    assert "думаю…" in str(w.render())


import json
import threading
from pathlib import Path


@pytest.mark.asyncio
async def test_indicator_shows_when_running_and_stays_on_stop(tmp_path):
    """После прямой остановки runner-а (runner.stop() без /stop → last_stop_requested
    не выставляется, но runner._stop_requested=True) итог хода — строка
    «■ Отменено» в ленте (S10), а не «completed»; индикатора нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            (Path(tmp_path) / f"meta_cli_{thread_id}.json").write_text(
                json.dumps({"thread_id": thread_id, "messages_count": 0}),
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
        inp.text = "пошёл"
        await pilot.press("enter")
        # Polling tick = 250мс; порог 500мс. После ~1с pause индикатор должен быть.
        await pilot.pause(1.0)

        assert len(pilot.app.query(ThinkingIndicator)) == 1

        # Прямой stop (в обход _cmd_stop → last_stop_requested не выставляется,
        # но runner._stop_requested=True) → outcome должен быть «stopped» → «cancelled».
        pilot.app.active_runner.stop()
        await pilot.pause(0.5)

        from gc_tui.widgets.turn_line import TurnLine

        assert len(pilot.app.query(ThinkingIndicator)) == 0
        # прямой stop → stopped → «■ Отменено»
        lines = [line.text.plain for line in pilot.app.query(TurnLine)]
        assert lines == ["■ Отменено"]


@pytest.mark.asyncio
async def test_indicator_keeps_thinking_after_agent_event_with_pause(
    tmp_path,
):
    """После event kind=agent и паузы, пока runner ещё работает, индикатор
    НЕ прячется и говорит «думаю…»: «сверяюсь…» — только по флагу ядра
    (судья реально проверяет ответ), по тишине его не угадывают."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            # Симулируем: user-сообщение в .jsonl, потом agent-реплика, потом блокируем
            # (как будто intent_guard/policy ещё валидирует).
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
                + "\n"
                + json.dumps(
                    {
                        "type": "message",
                        "seq": 2,
                        "ts_utc": "2026-05-26T00:00:01+00:00",
                        "payload": {
                            "id": "a1",
                            "role": "assistant",
                            "content": "вот ответ",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            meta_path.write_text(
                json.dumps({"thread_id": thread_id, "messages_count": 2}),
                encoding="utf-8",
            )
            self._evt = threading.Event()
            self._evt.wait(timeout=5.0)

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
        inp.text = "что нового?"
        await pilot.press("enter")
        scr = pilot.app.screen
        await pilot.pause(0.5)  # дать прогону смонтировать индикатор

        # Пиним «живой» runner (убираем зависимость от тайминга реального прогона на
        # момент проверки) и симулируем agent-ответ + тишину.
        class _LiveRunner:
            def is_running(self):
                return True

            def stop(self):
                pass

        pilot.app.active_runner = _LiveRunner()
        scr._confirm_active = False
        scr._last_event_kind = "agent"
        await pilot.pause(2.0)
        scr._poll_active()
        ind = scr._thinking
        assert ind is not None
        assert not ind.has_class("hidden")
        assert "думаю" in str(ind._render_text().plain)


def test_done_state_renders_vypolnil_dim_and_frozen():
    """Длительность «Выполнил за N» — снаружи (запись итога хода), свой счёт
    индикатора её не подменяет: время идёт, N стоит."""
    from gc_tui.widgets import thinking_indicator as ti

    clock = {"t": 100.0}
    ind = ti.ThinkingIndicator(started_at=0.0, clock=lambda: clock["t"])
    ind.set_state("done")
    ind.freeze_elapsed(100.0)
    clock["t"] = 999.0  # время идёт дальше
    txt = ind._render_text()
    s = txt.plain
    assert s == "✓ Выполнил за 1м 40с"


@pytest.mark.asyncio
async def test_esc_stops_running_run(tmp_path):
    """Esc на главном экране при running + пустой выделение → stop_run."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    stopped = {"flag": False}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            (Path(tmp_path) / f"meta_cli_{thread_id}.json").write_text(
                json.dumps({"thread_id": thread_id, "messages_count": 0}),
                encoding="utf-8",
            )
            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            stopped["flag"] = True
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
        inp.text = "поехал"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert pilot.app.active_runner.is_running()

        await pilot.press("escape")
        await pilot.pause(0.3)
        assert stopped["flag"] is True

"""/copy и /copy N: ответ агента из конца треда — в буфер; отклик в слоте."""

from __future__ import annotations

import json

import pytest
from footer_text import footer_rows
from gc_testkit.tui_support import tui_settings, wait_until

from gc_tui.services.export import agent_answers

pytest.importorskip("textual")


def _rec(payload):
    return {"payload": payload, "ts": "t"}


def _call(name, args, call_id):
    return {
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(args),
    }


def _out(call_id, name="fs_read_file"):
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "name": name,
        "output": "СЕКРЕТНЫЙ ВЫВОД ТУЛА",
    }


def _assistant(text):
    return {"role": "assistant", "content": text}


def _thread():
    return [
        {"role": "user", "content": "РЕПЛИКА ПОЛЬЗОВАТЕЛЯ"},
        _assistant("проза"),
        _call("fs_read_file", {"path": "a"}, "c1"),
        _out("c1"),
        _call("turn_intermediate_reply", {"answer": "промежуточный"}, "c2"),
        _out("c2", "turn_intermediate_reply"),
        _call("turn_finish", {"task_report": "отчёт"}, "c3"),
        _out("c3", "turn_finish"),
        _call("turn_propose_plan", {"plan": "план"}, "c4"),
    ]


def _segment(payloads, index=0):
    return {
        "index": index,
        "messages": [_rec(p) for p in payloads],
        "compacted_by": None,
    }


def test_agent_answers_prose_report_plan_only():
    answers = agent_answers([_segment(_thread())])
    assert answers == ["проза", "отчёт", "план"]


def test_agent_answers_across_compacted_segments():
    first = _segment([_assistant("старый ответ")], index=0)
    second = _segment([_assistant("новый ответ")], index=1)
    assert agent_answers([first, second]) == ["старый ответ", "новый ответ"]


def test_agent_answers_empty_thread():
    assert agent_answers([_segment([{"role": "user", "content": "x"}])]) == []


def _turns(*turns):
    """Ходы: реплика пользователя + записи хода."""
    out = []
    for body in turns:
        out.append({"role": "user", "content": "вопрос"})
        out.extend(body)
    return [_segment(out)]


def _finish(report, call_id, comment=""):
    args = {"task_report": report}
    if comment:
        args["comment"] = comment
    return _call("turn_finish", args, call_id)


def test_same_answer_in_two_turns_counts_twice():
    segs = _turns([_assistant("да")], [_assistant("да")])
    assert agent_answers(segs) == ["да", "да"]


def test_intermediate_then_same_final_is_one():
    body = [
        _call("turn_intermediate_reply", {"answer": "X"}, "i1"),
        _out("i1", "turn_intermediate_reply"),
        _assistant("X"),
    ]
    assert agent_answers(_turns(body)) == ["X"]


def test_intermediate_then_same_report_is_one():
    body = [
        _call("turn_intermediate_reply", {"answer": "X"}, "i1"),
        _out("i1", "turn_intermediate_reply"),
        _finish("X", "f1"),
    ]
    assert agent_answers(_turns(body)) == ["X"]


def test_prose_intermediate_prose_as_trace_shows():
    body = [
        _assistant("X"),
        _call("turn_intermediate_reply", {"answer": "Y"}, "i1"),
        _out("i1", "turn_intermediate_reply"),
        _assistant("X"),
    ]
    # промежуточный ответ стал базой дедупа: повтор прозы показан
    assert agent_answers(_turns(body)) == ["X", "X"]


def test_repeated_report_after_prose_is_one():
    body = [_assistant("итог"), _finish("итог", "f1")]
    assert agent_answers(_turns(body)) == ["итог"]


def test_rejected_finish_attempt_not_counted():
    body = [_finish("плохой", "f1"), _finish("хороший", "f2")]
    assert agent_answers(_turns(body)) == ["хороший"]


def test_finish_attempt_repeated_by_prose_counted_once():
    body = [_finish("отчёт", "f1"), _assistant("отчёт")]
    assert agent_answers(_turns(body)) == ["отчёт"]


class _Store:
    def __init__(self, segments):
        self._segments = segments

    def read_segments(self, task_id, system=None):
        return self._segments


async def _run_copy(tmp_path, monkeypatch, arg, segments):
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    copied: list[str] = []
    monkeypatch.setattr(
        clipboard_ops,
        "write_clipboard",
        lambda text: copied.append(text) or True,
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(scr, "_store", _Store(segments), raising=False)
        monkeypatch.setattr(scr, "_active_task_id", "TID", raising=False)
        await scr._handle_command(f"copy {arg}".strip())
        await wait_until(
            pilot, lambda: footer_rows(scr)[1] != "", "отклик в слоте"
        )
        return copied, footer_rows(scr)[1]


@pytest.mark.asyncio
async def test_copy_last_answer(tmp_path, monkeypatch):
    copied, slot = await _run_copy(
        tmp_path, monkeypatch, "", [_segment(_thread())]
    )
    assert copied == ["план"]
    assert slot == "✓ скопирован последний ответ"


@pytest.mark.asyncio
async def test_copy_second_from_end(tmp_path, monkeypatch):
    copied, slot = await _run_copy(
        tmp_path, monkeypatch, "2", [_segment(_thread())]
    )
    assert copied == ["отчёт"]
    assert slot == "✓ скопирован ответ 2 из 3"


@pytest.mark.asyncio
async def test_copy_too_big(tmp_path, monkeypatch):
    copied, slot = await _run_copy(
        tmp_path, monkeypatch, "5", [_segment(_thread())]
    )
    assert copied == []
    assert slot == "/copy 5: ответов агента в сессии 3"


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["x", "0", "-1"])
async def test_copy_bad_number(tmp_path, monkeypatch, arg):
    copied, slot = await _run_copy(
        tmp_path, monkeypatch, arg, [_segment(_thread())]
    )
    assert copied == []
    assert slot == "/copy N — N-й ответ с конца, например /copy 2"


@pytest.mark.asyncio
async def test_copy_no_answers(tmp_path, monkeypatch):
    segs = [_segment([{"role": "user", "content": "привет"}])]
    copied, slot = await _run_copy(tmp_path, monkeypatch, "", segs)
    assert copied == []
    assert slot == "/copy: в этой сессии ещё нет ответов агента"


@pytest.mark.asyncio
async def test_copy_without_task(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(scr, "_store", None, raising=False)
        await scr._handle_command("copy")
        await pilot.pause()
        assert footer_rows(scr)[1] == (
            "/copy: в этой сессии ещё нет ответов агента"
        )


@pytest.mark.asyncio
async def test_copy_clipboard_failure_is_reported(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda t: False)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        store = _Store([_segment(_thread())])
        monkeypatch.setattr(scr, "_store", store, raising=False)
        monkeypatch.setattr(scr, "_active_task_id", "TID", raising=False)
        notified: list = []
        monkeypatch.setattr(
            pilot.app, "notify", lambda *a, **k: notified.append(a)
        )
        await scr._handle_command("copy")
        await wait_until(
            pilot, lambda: footer_rows(scr)[1] != "", "отклик в слоте"
        )
        assert footer_rows(scr)[1] == (
            "/copy: не удалось записать в буфер обмена"
        )
        assert notified == []


@pytest.mark.asyncio
async def test_copy_slot_waits_for_write_result(tmp_path, monkeypatch):
    import threading

    from gc_tui.app import TuiApp
    from gc_tui.services import clipboard_ops

    gate = threading.Event()
    monkeypatch.setattr(
        clipboard_ops, "write_clipboard", lambda t: gate.wait(5) or True
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        store = _Store([_segment(_thread())])
        monkeypatch.setattr(scr, "_store", store, raising=False)
        monkeypatch.setattr(scr, "_active_task_id", "TID", raising=False)
        await scr._handle_command("copy")
        await pilot.pause()
        assert footer_rows(scr)[1] == ""
        gate.set()
        await wait_until(
            pilot, lambda: footer_rows(scr)[1] != "", "итог записи"
        )
        assert footer_rows(scr)[1] == "✓ скопирован последний ответ"


@pytest.mark.asyncio
async def test_copy_outcome_colours(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.palette import OK, WARN

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_outcome("✓ готово", ok=True)
        spans = scr._footer_hint_body().spans
        assert [(sp.start, sp.end, sp.style) for sp in spans] == [(0, 1, OK)]
        scr._show_footer_outcome("/copy: отказ", ok=False)
        spans = scr._footer_hint_body().spans
        assert [(sp.start, sp.end, sp.style) for sp in spans] == [
            (0, 12, WARN)
        ]

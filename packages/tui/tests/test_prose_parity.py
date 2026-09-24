"""Чистая проза: контент стрим-ряда и финал-ряда идентичны (оба render_prose)."""

from gc_tui.services.markdown_render import render_prose, segment_answer
from gc_tui.services.trace_render import build_agent_widgets
from gc_tui.widgets.trace_row import TraceRow


def test_pure_prose_stream_equals_final_content():
    text = "Первый абзац.\n\nВторой абзац после пустой строки."
    # финал: один прозовый прогон → один TraceRow с render_prose(text)
    segs = segment_answer(text)
    assert [s.kind for s in segs] == ["prose"]
    final_text = render_prose(segs[0].content)
    # стрим: тот же render_prose на том же тексте
    stream_text = render_prose(text)
    assert final_text.plain == stream_text.plain
    # пустая строка сохранена в обоих
    assert "\n\n" in final_text.plain


def test_final_multiparagraph_is_single_trace_row():
    ws = build_agent_widgets("абзац1\n\nабзац2", event_no=1)
    rows = [w for w in ws if isinstance(w, TraceRow)]
    assert len(rows) == 1  # один прогон → один ряд (не по-строчно)
    assert (
        rows[0]._content.plain == "абзац1\n\nабзац2"
    )  # пустая строка сохранена

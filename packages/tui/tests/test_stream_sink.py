from gc_tui.services.stream_sink import StreamSink, format_stream_block


def test_sink_accumulates_and_resets():
    s = StreamSink()
    s.push("think", "ана")
    s.push("answer", "Это ")
    s.push("answer", "проект.")
    think, answer = s.snapshot()
    assert think == "ана"
    assert answer == "Это проект."
    assert s.has_content() is True
    s.reset()
    assert s.snapshot() == ("", "")
    assert s.has_content() is False


def test_format_shows_answer_only():
    # Reasoning now goes to the indicator; the block shows ONLY the answer.
    assert format_stream_block(answer="Это проект.") == "Это проект."


def test_format_strips_inline_think_from_answer():
    block = format_stream_block(
        answer="<think>анализирую\nсмотрю agents/</think>Это проект."
    )
    assert block == "Это проект."
    assert "<think>" not in block
    assert "анализирую" not in block


def test_format_empty_returns_empty():
    assert format_stream_block(answer="") == ""


def test_push_reset_clears_buffers():
    s = StreamSink()
    s.push("think", "думаю")
    s.push("answer", "привет")
    s.push("reset", "")
    assert s.snapshot() == ("", "")
    assert s.has_content() is False
    # fresh accumulation after reset
    s.push("answer", "новый")
    assert s.snapshot() == ("", "новый")


def test_format_unclosed_think_stripped():
    # Unclosed <think> still streaming → stripped, empty answer remainder.
    assert (
        format_stream_block(answer="<think>анализирую\nсмотрю agents/") == ""
    )


def test_sink_progress_units_counts_all_chars():
    s = StreamSink()
    s.push("think", "abcd")  # 4 chars
    s.push("answer", "efgh")  # +4 chars = 8
    assert s.progress_units() == 2  # 8 // 4


def test_reasoning_state_none_when_no_think():
    s = StreamSink()
    s.push("answer", "hi")
    assert s.reasoning_state() == (None, None)


def test_reasoning_state_thinking_then_done():
    clk = {"t": 100.0}
    s = StreamSink(clock=lambda: clk["t"])
    s.push("think", "план")
    assert s.reasoning_state() == ("thinking", None)
    clk["t"] = 106.0
    s.push("answer", "ответ")
    state, dur = s.reasoning_state()
    assert state == "done"
    assert round(dur) == 6


def test_reset_clears_progress_and_reasoning():
    clk = {"t": 0.0}
    s = StreamSink(clock=lambda: clk["t"])
    s.push("think", "xxxx")
    s.push("reset", "")
    assert s.progress_units() == 0
    assert s.reasoning_state() == (None, None)


def test_reasoning_line_short_unchanged():
    from gc_tui.services.stream_sink import format_reasoning_line

    assert format_reasoning_line(think="смотрю agents/") == "⠇ смотрю agents/"


def test_reasoning_line_collapses_newlines():
    from gc_tui.services.stream_sink import format_reasoning_line

    assert (
        format_reasoning_line(think="строю план\nчитаю README")
        == "⠇ строю план читаю README"
    )


def test_reasoning_line_truncates_with_ellipsis():
    from gc_tui.services.stream_sink import format_reasoning_line

    # single long word (no spaces) → can't word-trim, keep last N chars
    out = format_reasoning_line(think="a" * 100, tail_len=10)
    assert out == "⠇ …" + "a" * 10


def test_reasoning_line_truncates_on_word_boundary():
    from gc_tui.services.stream_sink import format_reasoning_line

    # last 12 chars of "alpha beta gamma delta" = " gamma delta"; drop leading
    # partial word → start at the word boundary → "gamma delta"
    assert (
        format_reasoning_line(think="alpha beta gamma delta", tail_len=12)
        == "⠇ …gamma delta"
    )


def test_reasoning_line_empty():
    from gc_tui.services.stream_sink import format_reasoning_line

    assert format_reasoning_line(think="") == ""
    assert format_reasoning_line(think="   ") == ""


def test_sink_numbers_attempts_by_reset():
    """Метку попытки ставит сам sink: новая на каждом `reset` ядра."""
    s = StreamSink()
    assert s.attempt() is None
    s.push("reset", "")
    s.push("think", "x")
    first = s.attempt()
    s.push("reset", "")
    assert first is not None
    assert s.attempt() not in (None, first)
    s.reset()
    assert s.attempt() is None


def test_glance_hidden_when_show_reasoning():
    from gc_tui.services.stream_sink import select_stream_render

    assert select_stream_render(
        "answer_reasoning", "", "⠇ x", show_reasoning=True
    ) == ("none", False)
    assert select_stream_render(
        "answer_reasoning", "ok", "⠇ x", show_reasoning=True
    ) == ("answer", False)


def test_reasoning_seconds_runs_then_freezes():
    """Время фазы мысли строке ленты: идёт, пока мысль идёт, застывает с
    первой `answer` / `tool`; мысли нет — None."""
    clk = {"t": 10.0}
    s = StreamSink(clock=lambda: clk["t"])
    assert s.reasoning_seconds() is None
    s.push("think", "a")
    clk["t"] = 13.0
    assert s.reasoning_seconds() == 3.0
    s.push("tool", "{}")
    clk["t"] = 20.0
    assert s.reasoning_seconds() == 3.0


def test_clear_answer_keeps_thought_and_attempt():
    """Строка ленты закоммичена под флагом мыслей: уходит только канал
    ответа — мысль, метка попытки и фаза мысли остаются."""
    clk = {"t": 1.0}
    s = StreamSink(clock=lambda: clk["t"])
    s.push("reset", "")
    attempt = s.attempt()
    s.push("think", "план")
    clk["t"] = 3.0
    s.push("answer", "ответ")
    s.clear_answer()
    assert s.snapshot() == ("план", "")
    assert s.attempt() == attempt
    assert s.reasoning_seconds() == 2.0

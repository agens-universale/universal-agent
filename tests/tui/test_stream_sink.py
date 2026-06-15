from tui.services.stream_sink import StreamSink, format_stream_block


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


def test_format_strips_inline_think_to_tail():
    block = format_stream_block(
        think="", answer="<think>анализирую структуру\nсмотрю agents/</think>Это проект."
    )
    assert "Это проект." in block
    assert "анализирую структуру" not in block  # full think not dumped
    assert "смотрю agents/" in block             # only the last think line (tail)


def test_format_reasoning_channel_as_think_tail():
    block = format_stream_block(think="строю план\nчитаю README", answer="")
    assert "читаю README" in block      # tail of reasoning
    assert "строю план" not in block


def test_format_empty_returns_empty():
    assert format_stream_block(think="", answer="") == ""


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


def test_format_unclosed_think_still_streaming():
    # answer arrives mid-stream: <think> opened but </think> not yet received
    block = format_stream_block(think="", answer="<think>анализирую\nсмотрю agents/")
    assert "смотрю agents/" in block      # tail of open think is surfaced
    assert "<think>" not in block         # raw tag opener must not appear in output


def test_sink_progress_units_counts_all_chars():
    s = StreamSink()
    s.push("think", "abcd")     # 4 chars
    s.push("answer", "efgh")    # +4 chars = 8
    assert s.progress_units() == 2          # 8 // 4


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

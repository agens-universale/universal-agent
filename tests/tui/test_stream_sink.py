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

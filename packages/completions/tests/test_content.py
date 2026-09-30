"""Порядок дельт: трейс в ``<think>``, финал после закрытия."""

from gc_completions.content import ContentMap, break_think_close


def _feed(mapper: ContentMap, events: list[tuple[str, str]]) -> str:
    for kind, text in events:
        mapper.feed(kind, text)
    mapper.finish("итог")
    return mapper.content


def test_trace_is_wrapped_and_final_follows():
    text = _feed(
        ContentMap(),
        [("think", "думаю"), ("answer", "черновик"), ("reset", "")],
    )
    assert text == "<think>\nдумаю\nчерновик\n</think>\nитог"


def test_prose_without_trace_has_no_think_tags():
    mapper = ContentMap()
    mapper.feed("answer", "просто")
    mapper.finish("просто")
    assert mapper.content == "просто"


def test_tool_line_has_status_and_no_body():
    mapper = ContentMap()
    mapper.apply_tool(
        {
            "op": "open",
            "call_id": "c1",
            "title": "read_file(path='src/app.py')",
        }
    )
    mapper.apply_tool({"op": "close", "call_id": "c1", "status": "completed"})
    mapper.finish("готово")
    assert "arguments" not in mapper.content
    assert "src/app.py" in mapper.content
    assert mapper.content == (
        "<think>\n"
        "read_file(path='src/app.py') — in_progress\n"
        "read_file(path='src/app.py') — completed\n"
        "</think>\n"
        "готово"
    )


def test_think_close_inside_trace_is_broken():
    assert break_think_close("см. </think> тут") == "см. < /think> тут"
    mapper = ContentMap()
    mapper.feed("think", "см. </THINK> тут")
    mapper.finish("ок")
    assert mapper.content.count("</think>") == 1
    assert "< /THINK>" in mapper.content

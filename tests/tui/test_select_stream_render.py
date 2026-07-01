from tui.services.stream_sink import select_stream_render


def test_minimal_is_none():
    assert select_stream_render("minimal", "ответ", "⠇ мысль") == ("none", False)


def test_answer_when_clean_present():
    assert select_stream_render("answer", "ответ", "⠇ мысль") == ("answer", False)


def test_answer_mode_empty_clean_is_none():
    assert select_stream_render("answer", "", "⠇ мысль") == ("none", False)


def test_answer_reasoning_prefers_answer():
    assert select_stream_render("answer_reasoning", "ответ", "⠇ м") == ("answer", False)


def test_answer_reasoning_shows_reasoning_without_answer():
    assert select_stream_render("answer_reasoning", "", "⠇ мысль") == ("reasoning", True)


def test_answer_reasoning_no_reasoning_is_none():
    assert select_stream_render("answer_reasoning", "", "") == ("none", False)

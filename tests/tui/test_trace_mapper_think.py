"""S3: trace_mapper вырезает незакрытый <think> (как stream_sink), сохраняя
greedy-to-last для закрытых/цитируемых тегов."""
from gc_tui.services.trace_mapper import _extract_assistant_text


def test_unclosed_think_stripped():
    assert _extract_assistant_text("<think>длинное рассуждение без закрытия") == ""


def test_unclosed_think_keeps_leading_prose():
    # текст ДО <think> остаётся; рассуждение без </think> режется до конца
    assert _extract_assistant_text("привет <think>думаю дальше") == "привет"


def test_closed_think_stripped_remainder_kept():
    assert _extract_assistant_text("<think>reason</think>Ответ") == "Ответ"


def test_greedy_to_last_close_when_quoting_tags():
    # модель цитирует теги внутри — режем от первого <think> до ПОСЛЕДНЕГО </think>
    text = "<think>a</think> mid <think>b</think>tail"
    assert _extract_assistant_text(text) == "tail"

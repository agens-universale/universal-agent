from gc_tui.services.trace_mapper import _extract_assistant_text


def test_normal_single_think_stripped():
    assert _extract_assistant_text("<think>думаю</think>Привет!") == "Привет!"


def test_nested_or_quoted_think_greedy_stripped():
    # Reasoning that quotes/discusses think tags → mismatched/nested tags.
    # Greedy strip (first <think> → last </think>) removes the whole span.
    content = "<think>рассуждаю про <think>цитата</think> теги и ещё</think>Ответ."
    assert _extract_assistant_text(content) == "Ответ."


def test_many_mismatched_close_tags_greedy():
    # More </think> than <think> (model quoting bounce text). Greedy strips to
    # the LAST </think>, leaving only the trailing answer.
    content = "<think>a</think> b </think> c </think>Финал"
    assert _extract_assistant_text(content) == "Финал"


def test_all_reasoning_no_answer_strips_to_empty():
    # Pure reasoning, no trailing answer → empty (no row will render).
    content = "<think>длинное рассуждение про </think> и <think> теги</think>"
    assert _extract_assistant_text(content) == ""

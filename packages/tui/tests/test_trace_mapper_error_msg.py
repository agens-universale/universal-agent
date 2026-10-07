from gc_tui.services.trace_mapper import map_message
import json
from gc_tui.services.trace_mapper import _TECH_TEXT_LIMIT, map_message


def test_error_msg_carries_call_id():
    msg = {
        "role": "user",
        "content": '<technical type="error_message">❌ Wrong arguments for `x`</technical>',
        "id": "m1",
        "call_id": "call_99",
    }
    ev = map_message(msg, no=5, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id == "call_99"


def test_error_msg_without_call_id_is_none():
    msg = {
        "role": "user",
        "content": '<technical type="error_message">boom</technical>',
        "id": "m2",
    }
    ev = map_message(msg, no=6, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id is None


# --- Task 4 / Д11: текст ошибки инструмента не режется на полуслове ---


def test_summarize_output_error_cuts_at_word_boundary_not_mid_word():
    """Живой баг: `str(err)[:160]` резало буквально посередине слова
    (`path='<roo`). Теперь — по последнему пробелу в пределах лимита + «…»."""
    from gc_tui.services.trace_mapper import _summarize_output

    err = "A" * 155 + " " + "B" * 20  # пробел ровно на границе среза [:160]
    out = json.dumps({"ok": False, "error": err})
    summary = _summarize_output(out, error=True)
    assert summary == "A" * 155 + "…"
    assert "B" not in summary  # старое поведение отдало бы обрубок "BBBB"


def test_summarize_output_error_without_word_boundary_still_gets_ellipsis():
    """Ошибка без единого пробела (одно длинное «слово», напр. путь) —
    резать всё равно приходится, но честно, с многоточием на конце."""
    from gc_tui.services.trace_mapper import _summarize_output

    err = "X" * 200
    out = json.dumps({"ok": False, "error": err})
    assert _summarize_output(out, error=True) == "X" * 160 + "…"


# --- Задача 1 / Ж1 батча 4: техническая врезка (error_message) режется по
# границе слова, как и сводка результата тула, а не жёстким [:120] ---


def _tech(tech_type: str, text: str):
    msg = {
        "role": "user",
        "content": f'<technical type="{tech_type}">{text}</technical>',
        "id": "t1",
    }
    return map_message(msg, no=1, ts="t")


def test_technical_error_text_is_cut_on_a_word_boundary():
    long_msg = (
        "Wrong arguments for `fs_append_file`: Arguments for tool 'fs_append_file' "
        "do not match its schema. Details: path='<root>': Additional properties are "
        "not allowed ('line_ending' was unexpected)"
    ) + " и дальше пошли бы подробности схемы," * 4  # заведомо длиннее бюджета
    ev = _tech("error_message", long_msg)  # хелпер файла
    assert ev.summary.endswith("…")
    assert len(ev.summary) <= _TECH_TEXT_LIMIT + 1  # бюджет + многоточие
    assert not ev.summary.endswith("path='<roo…")  # не обрыв посреди слова
    assert ev.summary.split()[-1].rstrip("…") in long_msg


def test_short_technical_error_text_is_untouched():
    ev = _tech("error_message", "Файл не найден")
    assert ev.summary == "Файл не найден"


# --- Смоук 5 / З3: во врезке видна ПРИЧИНА отказа, а не одно вступление ---


def test_technical_error_text_shows_the_cause_not_just_the_preamble():
    """Живой отказ схемы на смоуке 5: бюджет 120 целиком уходил вступлению
    («Wrong arguments for `X`: Arguments for tool 'X' do not match its
    schema. Details:»), а перевод строки после него границей слова не
    считается — обрез откатывался к «schema.», и причина на экран не
    попадала. Строка ленты переносится по ширине, поэтому бюджет здесь —
    страховка от патологии, а не ширина строки."""
    live = (
        "❌ Wrong arguments for `fs_append_file`: Arguments for tool "
        "'fs_append_file' do not match its schema. Details:\n"
        "path='<root>': Additional properties are not allowed "
        "('newline' was unexpected)"
    )
    ev = _tech("error_message", live)
    assert "'newline' was unexpected" in ev.summary
    assert "\n" not in ev.summary  # врезка занимает одну строку ленты

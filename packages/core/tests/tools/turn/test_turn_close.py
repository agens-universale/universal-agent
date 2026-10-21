import json

from gc_core.tools.turn.submit_solution import TurnFinishView
from gc_core.tools.turn.turn_close import (
    TurnClose,
    assistant_reply_text,
    close_view,
    closing_replies,
    closing_reply_of,
    finish_close,
    plan_close,
    starts_new_response,
    without_repeated_body,
)
from gc_core.utils.convertations import get_output_list


def test_reply_and_different_report_give_summary():
    view = TurnFinishView("Пользователь попросил сказать «пока».", "")
    close = finish_close(view, "Пока! 👋")
    assert close.render(markdown=False) == (
        "Пока! 👋\n\n* итог:\n\n  Пользователь попросил сказать «пока»."
    )
    assert close.render(markdown=True, after_shown_reply=True) == (
        "* **итог:**\n\n  Пользователь попросил сказать «пока»."
    )


def test_summary_carries_comment_on_next_line():
    view = TurnFinishView(
        "Исправил таймаут в config.yml.", "Не проверял на Windows."
    )
    assert finish_close(view, "Готово.").render(markdown=True) == (
        "Готово.\n\n* **итог:**\n\n  Исправил таймаут в config.yml.\n\n"
        "  Не проверял на Windows."
    )


def test_no_reply_report_is_answer_and_comment_is_note():
    view = TurnFinishView("Отчёт.", "Риск.")
    assert finish_close(view, "").render(markdown=False) == (
        "Отчёт.\n\n* заметка:\n\n  Риск."
    )


def test_reply_equal_to_report_is_not_repeated():
    view = TurnFinishView("Готово.", "")
    close = finish_close(view, " Готово. ")
    assert close.render(markdown=True, after_shown_reply=True) == ""


def test_reply_equal_to_report_keeps_comment_as_note():
    view = TurnFinishView("Готово.", "Риск.")
    close = finish_close(view, "Готово.")
    assert close.render(markdown=False) == "Готово.\n\n* заметка:\n\n  Риск."


def test_think_only_reply_is_no_reply():
    items = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "<think>x</think>"}],
        }
    ]
    assert assistant_reply_text(items) == ""


def test_assistant_reply_text_joins_all_assistant_messages():
    items = [
        {"type": "message", "role": "assistant", "content": "Раз"},
        {"type": "message", "role": "user", "content": "не берём"},
        {"type": "message", "role": "assistant", "content": " Два "},
    ]
    assert assistant_reply_text(items) == "Раз\n\nДва"


def test_markdown_multiline_report_indented():
    view = TurnFinishView("Шаги:\n- a\n\n```py\nx = 1\n```", "")
    close = finish_close(view, "Ок.")
    assert close.render(markdown=True, after_shown_reply=True) == (
        "* **итог:**\n\n  Шаги:\n  - a\n\n  ```py\n  x = 1\n  ```"
    )


def test_plan_after_reply_without_label():
    assert plan_close("## План\n1. A", "Вот план.").render(markdown=True) == (
        "Вот план.\n\n## План\n1. A"
    )


def test_plan_equal_to_reply_is_not_duplicated():
    assert plan_close("План.", " План. ").render(markdown=False) == "План."
    shown = plan_close("План.", " План. ")
    assert shown.render(markdown=True, after_shown_reply=True) == ""


def test_close_view_passes_prose_through():
    assert close_view("просто проза", "что-то") is None
    assert close_view(None, "") is None


def test_close_view_recognises_finish_and_plan():
    finish = json.dumps(
        {
            "solution_type": "direct_answer",
            "task": "t",
            "task_report": "Отчёт.",
            "handoff": {},
        }
    )
    plan = json.dumps({"plan": "## План", "status": "proposed"})
    assert close_view(finish, "Ответ.").render(markdown=False) == (
        "Ответ.\n\n* итог:\n\n  Отчёт."
    )
    assert close_view(plan, "Вот.").render(markdown=False) == (
        "Вот.\n\n## План"
    )


def test_closing_replies_ignores_rejected_and_resets_on_output():
    items = [
        {"type": "message", "role": "assistant", "content": "Черновик"},
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "f1",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "f1",
            "output": '{"ok": false}',
        },
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "f2",
            "arguments": "{}",
        },
    ]
    assert closing_replies(items) == {"f1": "Черновик", "f2": ""}


def test_closing_replies_resets_on_user_message():
    items = [
        {"type": "message", "role": "assistant", "content": "Старое"},
        {"type": "message", "role": "user", "content": "Дальше"},
        {
            "type": "function_call",
            "name": "turn_propose_plan",
            "call_id": "p1",
            "arguments": "{}",
        },
    ]
    assert closing_replies(items) == {"p1": ""}


def test_closing_replies_resets_on_stored_user_message_without_type():
    """В треде user-записи (в том числе technical-bounce) лежат без type."""
    items = [
        {"type": "message", "role": "assistant", "content": "Старое"},
        {"role": "user", "content": "Дальше", "id": "m_1"},
        {
            "type": "function_call",
            "name": "turn_finish",
            "call_id": "f1",
            "arguments": "{}",
        },
    ]
    assert closing_replies(items) == {"f1": ""}


def test_closing_replies_on_thread_records_of_one_response():
    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Пока! 👋",
                    "tool_calls": [
                        {
                            "id": "w1",
                            "type": "function",
                            "function": {
                                "name": "fs_read_file",
                                "arguments": '{"path": "a"}',
                            },
                        },
                        {
                            "id": "f1",
                            "type": "function",
                            "function": {
                                "name": "turn_finish",
                                "arguments": "{}",
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    items = get_output_list(response)
    assert closing_replies(items) == {"f1": "Пока! 👋"}


def test_closing_reply_of_tolerates_missing_executor():
    assert closing_reply_of(object()) == ""


def test_closing_reply_of_reads_executor_attribute_and_durable_error():
    class Executor:
        last_closing_reply = "Ответ"

    class Agent:
        executor = Executor()

    class Durable:
        @property
        def executor(self):
            raise RuntimeError("durable")

    assert closing_reply_of(Agent()) == "Ответ"
    assert closing_reply_of(Durable()) == ""


def test_consecutive_blank_lines_stay_empty():
    close = finish_close(TurnFinishView("a\n\n\nb", ""), "Ок.")
    assert close.render(markdown=False) == "Ок.\n\n* итог:\n\n  a\n\n\n  b"


def test_empty_report_with_reply_is_like_report_equal_to_reply():
    close = finish_close(TurnFinishView("", ""), "Привет")
    assert close.render(markdown=False) == "Привет"
    with_note = finish_close(TurnFinishView("", "Риск"), "Привет")
    assert with_note.render(markdown=False) == "Привет\n\n* заметка:\n\n  Риск"


def test_whitespace_only_reply_is_no_reply():
    assert plan_close("План", "   ").render(markdown=False) == "План"
    shown = plan_close("План", "Вот.")
    assert shown.render(markdown=True, after_shown_reply=True) == ("План")
    close = finish_close(TurnFinishView("Отчёт.", ""), "")
    assert close.render(markdown=False, after_shown_reply=True) == "Отчёт."
    blank = finish_close(TurnFinishView("Отчёт.", ""), "  ")
    assert blank.render(markdown=False, after_shown_reply=True) == "Отчёт."


def test_response_boundary_marks_results_and_user_records():
    assert starts_new_response({"type": "function_call_output"})
    assert starts_new_response({"role": "user", "content": "x"})
    assert not starts_new_response({"type": "message", "role": "assistant"})
    assert not starts_new_response("text")


def test_close_view_shows_only_report_and_note():
    raw = json.dumps(
        {
            "solution_type": "direct_answer",
            "task": "прочитать README",
            "task_report": "В README одна фраза.",
            "comment": "файл короткий",
            "handoff": '{"artifacts":["README.md"]}',
            "message": "Finish gate: accept.",
            "finish_gate": {"action": "accept"},
        },
        ensure_ascii=False,
    )
    text = close_view(raw, "").render(markdown=False)
    assert text == "В README одна фраза.\n\n* заметка:\n\n  файл короткий"


def test_close_view_hides_empty_report():
    raw = {
        "solution_type": "direct_answer",
        "task": "t",
        "task_report": "   ",
        "handoff": "{}",
    }
    assert close_view(raw, "").render(markdown=False) == ""


def test_report_starting_with_fence_and_table_stays_in_record():
    """Блок кода и таблица в начале отчёта не приклеены к метке."""
    report = "```python\nprint(1)\n```\n\n| файл | строк |\n|---|---|"
    close = finish_close(TurnFinishView(report, "Без тестов."), "Вот.")
    assert close.render(markdown=True, after_shown_reply=True) == (
        "* **итог:**\n\n  ```python\n  print(1)\n  ```\n\n"
        "  | файл | строк |\n  |---|---|\n\n  Без тестов."
    )


def test_comment_after_list_report_is_own_paragraph():
    """Comment после отчёта-списка — свой абзац, не пункт списка."""
    close = finish_close(TurnFinishView("- a\n- b", "Риск."), "Вот.")
    assert close.render(markdown=False) == (
        "Вот.\n\n* итог:\n\n  - a\n  - b\n\n  Риск."
    )


def test_closing_replies_keeps_reply_of_duplicated_call():
    """Компакция копирует пару вызов/результат за user-запись: пустая
    копия не затирает ответ модели при вызове."""
    call = {
        "type": "function_call",
        "name": "turn_finish",
        "call_id": "f1",
        "arguments": "{}",
    }
    items = [
        {"type": "message", "role": "assistant", "content": "Ответ"},
        call,
        {"type": "function_call_output", "call_id": "f1", "output": "{}"},
        {"role": "user", "content": "сводка"},
        call,
        {"type": "function_call_output", "call_id": "f1", "output": "{}"},
    ]
    assert closing_replies(items) == {"f1": "Ответ"}


def test_without_repeated_body_drops_shown_report_keeps_note():
    close = finish_close(TurnFinishView("Готово.", "Риск."), "")
    assert without_repeated_body(close, " Готово.\n") == (
        TurnClose("", "", "заметка", "", "Риск.")
    )


def test_without_repeated_body_keeps_other_report_and_reply():
    close = finish_close(TurnFinishView("Отчёт.", ""), "")
    assert without_repeated_body(close, "Другое.") == close
    with_reply = finish_close(TurnFinishView("Отчёт.", ""), "Ответ.")
    assert without_repeated_body(with_reply, "Ответ.") == with_reply

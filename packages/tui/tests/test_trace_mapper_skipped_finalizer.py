"""trace_mapper: штатный skip второго финализатора в пачке (processing_responses
partition_work_then_finish) не должен рисоваться в трейсе — ни красный [!]-баннер
skip-output'а, ни парный `function_call` пропущенного финализатора (иначе
turn_finish рисует фантомную прозу-«ответ», а второй turn_ask_human — фантомный
баннер). Данные треда/контекст модели не трогаем — только презентация.
"""

import json

from gc_tui.services.trace_mapper import collect_skipped_call_ids, map_message


# Точная форма synthetic skip-output из processing_responses.py:409-418.
def _skip_output_payload(call_id: str, finalizer: str = "turn_ask_human") -> dict:
    return {
        "id": "fco_skip",
        "type": "function_call_output",
        "call_id": call_id,
        "output": (
            '{"ok": false, "skipped": true, "message": '
            f'"Skipped: turn already finalized by {finalizer}; '
            'one finalizer per turn."}'
        ),
    }


def _finish_call_payload(call_id: str) -> dict:
    return {
        "type": "function_call",
        "name": "turn_finish",
        "call_id": call_id,
        "arguments": json.dumps({"solution_type": "direct_answer", "task_report": "Готово.", "handoff": {}}),
        "id": "fc_finish",
        "status": "completed",
    }


def _ask_call_payload(call_id: str, question: str = "Чем могу помочь?") -> dict:
    return {
        "type": "function_call",
        "name": "turn_ask_human",
        "call_id": call_id,
        "arguments": json.dumps({"question": question}),
        "id": "fc_ask",
        "status": "completed",
    }


def test_skip_output_returns_none():
    """function_call_output со `skipped: true` → None (не красный [!]-баннер)."""
    ev = map_message(_skip_output_payload("c1"), no=1, ts="2026-07-07T10:00:00+00:00")
    assert ev is None


def test_skip_output_is_not_mapped_as_error():
    """Регресс-гвард против прежнего поведения: раньше skip-output (ok=False)
    маппился как kind='error'. Теперь — None, а не error."""
    ev = map_message(_skip_output_payload("c1"), no=1, ts="2026-07-07T10:00:00+00:00")
    assert ev is None or ev.kind != "error"


def test_collect_skipped_call_ids_picks_only_skip_outputs():
    """Собираем call_id только у skip-output'ов; обычные записи игнорируем."""
    messages = [
        _ask_call_payload("c_ask"),
        _finish_call_payload("c_finish"),
        {
            "type": "function_call_output",
            "call_id": "c_ask",
            "output": '{"tool": "ask_human", "ok": true, "response": "да"}',
        },
        _skip_output_payload("c_finish"),
    ]
    assert collect_skipped_call_ids(messages) == {"c_finish"}


def test_skipped_finish_call_suppressed_when_in_set():
    """Парный пропущенный turn_finish (call_id в наборе) → None: фантомной
    прозы-«ответа» из task_report быть не должно."""
    ev = map_message(
        _finish_call_payload("c_finish"),
        no=2,
        ts="2026-07-07T10:00:00+00:00",
        skipped_call_ids={"c_finish"},
    )
    assert ev is None


def test_skipped_second_ask_call_suppressed_when_in_set():
    """Второй пропущенный turn_ask_human → None: фантомного второго баннера нет."""
    ev = map_message(
        _ask_call_payload("c_ask2"),
        no=2,
        ts="2026-07-07T10:00:00+00:00",
        skipped_call_ids={"c_ask2"},
    )
    assert ev is None


def test_finish_call_not_in_set_still_renders():
    """Регресс-гвард: обычный (исполненный) turn_finish не в наборе → рендерится
    прозой как прежде. Пустой набор ничего не подавляет."""
    ev = map_message(
        _finish_call_payload("c_finish"),
        no=2,
        ts="2026-07-07T10:00:00+00:00",
        skipped_call_ids=set(),
    )
    assert ev is not None
    assert ev.kind == "agent"
    assert "Готово." in ev.summary


def test_normal_tool_output_unaffected():
    """Регресс-гвард: обычный tool-output без `skipped` не трогаем."""
    msg = {
        "type": "function_call_output",
        "call_id": "c_read",
        "output": '{"ok": true, "total_lines": 42}',
    }
    ev = map_message(msg, no=3, ts="2026-07-07T10:00:00+00:00")
    assert ev is not None
    assert ev.kind == "result"


def test_batch_only_real_finalizer_survives():
    """Интеграция контракта, на который опираются вызывающие: для пачки
    [ask call, finish call, ask output(reply), skip output] после pre-pass'а
    остаются только реальный ask-баннер и реплика юзера; фантомный finish и
    skip-баннер исчезают."""
    batch = [
        _ask_call_payload("c_ask"),
        _finish_call_payload("c_finish"),
        {
            "type": "function_call_output",
            "call_id": "c_ask",
            "output": '{"tool": "ask_human", "ok": true, "response": "да, продолжаем"}',
        },
        _skip_output_payload("c_finish"),
    ]
    skipped = collect_skipped_call_ids(batch)
    kinds = []
    for i, m in enumerate(batch):
        ev = map_message(m, no=i + 1, ts="2026-07-07T10:00:00+00:00", skipped_call_ids=skipped)
        if ev is not None:
            kinds.append((ev.kind, ev.tool_name))
    assert ("ask_human", "ask_human") in kinds  # реальный вопрос
    assert ("user", "ask_human") in kinds  # реплика юзера
    assert all(k != "agent" for k, _ in kinds)  # фантомной прозы finish нет
    assert len(kinds) == 2

"""dialog_summary → kind=summary, видим, содержимое очищено от <think>/<technical>."""
from gc_tui.services.trace_mapper import map_message


def test_dialog_summary_maps_to_summary_kind():
    raw = (
        '<technical type="dialog_summary">Summarized context from previous dialogue.\n'
        '<summary>User asked X. Agent did Y. Result: partial.</summary></technical>'
    )
    ev = map_message({"role": "user", "content": raw, "id": "s1"}, no=1, ts="t")
    assert ev is not None
    assert ev.kind == "summary"
    assert ev.hidden is False
    assert "User asked X" in ev.summary_content
    assert "<summary>" not in ev.summary_content
    assert "Summarized context" not in ev.summary_content


def test_dialog_summary_strips_think():
    raw = (
        '<technical type="dialog_summary"><summary><think>secret reasoning</think>'
        'User did Z.</summary></technical>'
    )
    ev = map_message({"role": "user", "content": raw, "id": "s2"}, no=1, ts="t")
    assert "secret reasoning" not in ev.summary_content
    assert "User did Z" in ev.summary_content


def test_dialog_summary_nested_section_tags_do_not_leak():
    """Реальный wire-form после summary-рефактора master (вложенные секционные теги
    внутри <summary>) не должен утекать сырым XML — секции разворачиваются в текст.
    Тело строится реальными core-функциями (не мок), чтобы тест ловил будущий дрейф."""
    from gc_core.summarize.summary_message_body import build_dialog_summary_content
    from gc_core.summarize.summary_spec import slice_summary

    raw_model_summary = (
        "<summary>\n"
        "  <task_and_context>Migrate TUI summary detection to nested tags.</task_and_context>\n"
        "  <open_tasks>mode: execution\n- [IN_PROGRESS] verify nested rendering</open_tasks>\n"
        "  <do_not_redo>Flat regex on uppercase headers superseded.</do_not_redo>\n"
        "  <last_verified_facts>pytest tui/ -> passed</last_verified_facts>\n"
        "  <active_constraints>n/a</active_constraints>\n"
        "  <recent_artifacts>trace_mapper.py -> detection</recent_artifacts>\n"
        "</summary>"
    )
    body = build_dialog_summary_content(
        summary_text=slice_summary(raw_model_summary, "full"),
        last_dialog_steps="[step 1] did X",
    )
    content = (
        '<technical type="dialog_summary">Summarized context from previous dialogue.\n'
        + body.strip()
        + "</technical>"
    )
    ev = map_message({"role": "user", "content": content, "id": "s3"}, no=1, ts="t")
    assert ev is not None
    assert ev.kind == "summary"
    # тела секций сохранены
    assert "Migrate TUI summary detection to nested tags." in ev.summary_content
    assert "[IN_PROGRESS] verify nested rendering" in ev.summary_content
    # сырые секционные теги НЕ утекают
    assert "<task_and_context>" not in ev.summary_content
    assert "<open_tasks>" not in ev.summary_content
    # last_dialog_steps (вне <summary>) исключён
    assert "<last_dialog_steps>" not in ev.summary_content
    assert "[step 1] did X" not in ev.summary_content
    # служебные части срезаны
    assert "Summarized context" not in ev.summary_content
    assert "Continue the dialogue from here" not in ev.summary_content

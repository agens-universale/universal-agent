"""Баннер подтверждения: payload сохраняется как есть, мета-поля схлопываются.

Секция T16 матрицы четырёх ответов. Сама матрица — про безопасность и живёт в наборе
ЯДРА (`packages/core/tests/security/test_4choice_matrix_t8_t16.py`), а эти четыре
проверки смотрят на `gc_tui.widgets.confirm_banner`, то есть на фронтенд. Разделены
27.08.2026 при вливании develop: набору ядра импортировать `gc_tui` нельзя, это ловит
`tests/test_test_ownership.py` — и поймал. Тот же приём, которым при разбиении на
пакеты разделили `test_onboarding_wiring.py` на половины cli и tui.

Смысл различия: `_fmt_payload_value` показывает КОМАНДУ или КОД, которые человек
решает пускать, — резать и склеивать их нельзя, иначе решение принимается по
искажённому тексту. `_fmt_field_value` показывает мета-поля (причина, риски), где
многострочность только ломает вёрстку.
"""


# ── T16: long payload multiline as-is (scroll + docked options) ──────────────

def test_t16_fmt_payload_value_preserves_multiline():
    """_fmt_payload_value preserves newlines (unlike _fmt_field_value which collapses)."""
    from gc_tui.widgets.confirm_banner import _fmt_payload_value

    multiline = "line1\nline2\nline3"
    result = _fmt_payload_value(multiline)
    assert "\n" in result, "payload value must preserve newlines"
    assert "line1" in result
    assert "line2" in result


def test_t16_fmt_payload_value_keeps_full_long_script():
    """Long python/curl payloads are not soft-cut — body scrolls, options stay docked."""
    from gc_tui.widgets.confirm_banner import _fmt_payload_value

    body = "\n".join(f"row{i} = {i}" for i in range(80))
    result = _fmt_payload_value(body)
    assert result == body
    assert "row79" in result
    assert "… (+" not in result


def test_t16_fmt_payload_value_preserves_short_multiline():
    """Short multiline payload is fully preserved."""
    from gc_tui.widgets.confirm_banner import _fmt_payload_value

    payload = "curl https://api.example.com \\\n  -H 'Accept: json' \\\n  -d 'data'"
    result = _fmt_payload_value(payload)
    assert result == payload.strip()
    assert result.count("\n") == 2  # both newlines preserved


def test_t16_fmt_field_value_collapses_multiline():
    """Meta field values (non-payload) still collapse whitespace — single line."""
    from gc_tui.widgets.confirm_banner import _fmt_field_value

    multiline = "line1\nline2\nline3"
    result = _fmt_field_value(multiline)
    assert "\n" not in result, "meta field must be single-line"
    assert " " in result  # newlines → spaces

"""Вид полей формы задаёт класс, а не id: плоское правило `.form-field` и
его состояния (`:focus`, `.set-framed`, `.set-readonly`) лежат в styles.tcss,
id полей в селекторах не перечисляются. Без плоского правила Input рисует
дефолтную рамку глифами, которых нет в консольных шрифтах (баг поля
«Прокси», смок 2026-08-02)."""

# Путь — из gc_testkit.paths: исходники TUI лежат в packages/tui/src/gc_tui,
# и арифметика уровней от __file__ здесь уже ломалась.
from gc_testkit.paths import TUI_PKG

_STYLES = TUI_PKG / "styles.tcss"

# id полей ввода, которых в tcss быть не должно: вид задаёт класс form-field
_OLD_FIELD_IDS = (
    "conn-url",
    "conn-token",
    "conn-proxy",
    "onb-model",
    "onb-level-model",
    "mef-alias",
    "mef-model",
    "mef-url",
    "mef-token",
    "mef-proxy",
    "mef-summary-chars",
    "mef-summary-tokens",
    "set-profile-name",
    "voice-url",
    "voice-token",
    "voice-model",
    "voice-lang",
)


def _css() -> str:
    return _STYLES.read_text(encoding="utf-8")


def _rule_lines(selector: str) -> list[str]:
    return [
        ln for ln in _css().splitlines() if ln.lstrip().startswith(selector)
    ]


def test_form_field_flat_rule():
    """Плоское правило: без него Input несёт дефолтную tall-рамку."""
    lines = _rule_lines(".form-field {")
    assert any("border: none" in ln and "height: 1" in ln for ln in lines)


def test_form_field_state_rules_exist():
    for sel in (
        ".form-field:focus {",
        ".form-field.set-framed {",
        ".form-field.set-framed:focus {",
        ".form-field.set-readonly {",
    ):
        assert _rule_lines(sel), f"нет правила {sel}"


def test_framed_rule_has_round_border():
    lines = _rule_lines(".form-field.set-framed {")
    assert any("round $run-color" in ln for ln in lines)


def test_no_field_id_selectors():
    """Вид полей — по классу; id полей в селекторах не перечисляем."""
    css = _css()
    found = [fid for fid in _OLD_FIELD_IDS if f"#{fid}" in css]
    assert found == [], f"id полей в tcss: {found}"


def test_disabled_token_border_is_dimmed():
    """Замороженное поле (без авторизации) — рамка $deep-dim, по :disabled."""
    lines = _rule_lines(".form-field.set-framed.set-muted:disabled")
    assert lines, "нет правила замороженного поля"
    assert all("$deep-dim" in ln for ln in lines)
    assert not _rule_lines(".form-field.set-framed:disabled")


async def test_voice_fact_field_keeps_run_color_border(tmp_path):
    """Поле-факт голоса (disabled + set-readonly) рамку не приглушает."""
    from gc_testkit.tui_support import tui_settings
    from textual.screen import Screen

    from gc_tui.app import TuiApp
    from gc_tui.widgets.paste_input import PasteInput

    class _S(Screen):
        def compose(self):
            yield PasteInput(
                id="f", disabled=True, classes="set-framed set-readonly"
            )
            yield PasteInput(
                id="t", disabled=True, classes="set-framed set-muted"
            )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await app.push_screen(_S())
        await pilot.pause()
        scr = app.screen
        fact = scr.query_one("#f").styles.border_top
        muted = scr.query_one("#t").styles.border_top
        assert fact[0] == "round"
        assert fact[1] != muted[1]


def test_dropdown_scroll_rule_exists():
    """Потолок высоты выпадающих списков. Правило принесла голосовая фича, в develop
    его нет — при разносе на ветки оно обязано уехать в ux-polish, иначе список
    моделей там раскатает страницу (§3.4 спеки). Тест держит его на месте."""
    assert ".dropdown-scroll" in _css()
    assert "max-height: 14" in _css()


def test_auth_nested_is_gone_everywhere():
    """Класс ушёл вместе с радио-блоком авторизации. Проверяем И css, И применения:
    правило без применений — мёртвый код, применение без правила — потерянный отступ
    (так и вышло, когда правило удалили раньше времени).

    Обход файлов — средствами Python (Path.rglob), не внешним `grep`: на Windows
    (основная платформа проекта) его может не быть в PATH — тест не падал бы, а
    ERROR'ил; а с capture_output=True без check=True сломанный вызов дал бы пустой
    stdout и ассерт молча проходил бы вхолостую, на сломанном коде."""
    assert ".auth-nested" not in _css()
    src = TUI_PKG
    hits = []
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".bak"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if "auth-nested" in line:
                hits.append(f"{path}:{i}:{line}")
    assert hits == [], "класс всё ещё применяется:\n" + "\n".join(hits)

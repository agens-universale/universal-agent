"""VoiceEndpointEditor: канон-рамки полей своего STT-endpoint (изолированный контракт)."""

import pytest

pytest.importorskip("textual")

from gc_tui.widgets.paste_input import PasteInput
from textual.app import App, ComposeResult
from textual.widgets import Input, Static


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor

    return _App()


def _labels(ed):
    return [str(w.render()) for w in ed.query(".onb-hint")]


async def test_voice_endpoint_editor_framed_structure_label_then_input():
    """Структура — как у ConnectionEditor: подпись .onb-hint + PasteInput с рамкой
    (set-framed), по паре на поле; воздух между полями — .set-group-gap."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={})
    app = _harness(ed)
    async with app.run_test():
        children = list(ed.children)
        assert len(children) == 6  # 3 поля × (подпись + поле)
        for i in (0, 2, 4):
            assert isinstance(children[i], Static)
            assert "onb-hint" in children[i].classes
        for i in (1, 3, 5):
            assert isinstance(children[i], PasteInput)
            assert "set-framed" in children[i].classes
        assert _labels(ed) == ["URL", "Токен", "Модель"]
        # воздух: у подписей после первой — зазор группы (канон ConnectionEditor)
        assert "set-group-gap" not in children[0].classes
        assert "set-group-gap" in children[2].classes
        assert "set-group-gap" in children[4].classes


async def test_voice_endpoint_editor_placeholders_and_password():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={})
    app = _harness(ed)
    async with app.run_test():
        assert (
            ed.query_one("#voice-url", Input).placeholder
            == "https://…/v1/audio/transcriptions"
        )
        assert ed.query_one("#voice-model", Input).placeholder == "whisper-1"
        assert ed.query_one("#voice-token", Input).password is True
        assert ed.query_one("#voice-url", Input).password is False


async def test_voice_endpoint_editor_prefills_values_from_outside():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={
            "url": "https://s.x/v1/audio/transcriptions",
            "token": "sk",
            "model": "wh",
        }
    )
    app = _harness(ed)
    async with app.run_test():
        assert (
            ed.query_one("#voice-url", Input).value
            == "https://s.x/v1/audio/transcriptions"
        )
        assert ed.query_one("#voice-token", Input).value == "sk"
        assert ed.query_one("#voice-model", Input).value == "wh"


async def test_voice_endpoint_editor_language_field_only_when_asked():
    """Мастер языка не спрашивает (3 поля), /settings — спрашивает (4-е поле)."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={}, fields=("url", "token", "model"))
    app = _harness(ed)
    async with app.run_test():
        assert not ed.query("#voice-lang")

    ed4 = VoiceEndpointEditor(
        values={"language": "en"}, fields=("url", "token", "model", "language")
    )
    app4 = _harness(ed4)
    async with app4.run_test():
        assert ed4.query_one("#voice-lang", Input).value == "en"
        assert ed4.query_one("#voice-lang", Input).placeholder == "ru"
        assert _labels(ed4)[-1] == "Язык"


async def test_voice_endpoint_editor_values_are_stripped():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={}, fields=("url", "token", "model", "language")
    )
    app = _harness(ed)
    async with app.run_test():
        ed.query_one(
            "#voice-url", Input
        ).value = "  https://s.x/v1/audio/transcriptions "
        ed.query_one("#voice-token", Input).value = " sk "
        assert ed.values() == {
            "url": "https://s.x/v1/audio/transcriptions",
            "token": "sk",
            "model": "",
            "language": "",
        }


async def test_voice_endpoint_editor_submit_returns_values():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={
            "url": "https://s.x/v1/audio/transcriptions",
            "token": "sk",
            "model": "wh",
        }
    )
    app = _harness(ed)
    async with app.run_test():
        ok, values = ed.submit()
        assert ok is True
        assert values == {
            "url": "https://s.x/v1/audio/transcriptions",
            "token": "sk",
            "model": "wh",
        }
        assert not ed.query(".onb-warn")


async def test_voice_endpoint_editor_submit_rejects_invalid_url_inside():
    """Ошибка живёт ВНУТРИ редактора (канон EndpointEditor), а не в теле экрана;
    повторный submit не плодит вторую строку ошибки."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={"url": "ttp://bad"})
    app = _harness(ed)
    async with app.run_test():
        ok, values = ed.submit()
        assert ok is False and values is None
        assert len(ed.query(".onb-warn")) == 1
        ed.submit()
        assert len(ed.query(".onb-warn")) == 1


async def test_voice_endpoint_editor_submit_requires_url():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={})
    app = _harness(ed)
    async with app.run_test():
        ok, values = ed.submit()
        assert ok is False and values is None
        assert any(
            "введите адрес" in str(w.render()) for w in ed.query(".onb-warn")
        )


async def test_voice_endpoint_editor_submit_clears_stale_error():
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    # model заполнена сразу: тест проверяет очистку строки ошибки URL, а не валидацию
    # модели (та проверяется отдельным тестом на пресет-листе).
    ed = VoiceEndpointEditor(values={"url": "ttp://bad", "model": "whisper-1"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        assert ed.submit()[0] is False
        ed.query_one(
            "#voice-url", Input
        ).value = "https://s.x/v1/audio/transcriptions"
        ok, values = ed.submit()
        assert ok is True
        await pilot.pause()  # remove() отложенный — ждём применения
        assert not ed.query(".onb-warn")


async def test_voice_endpoint_editor_token_placeholder_can_be_overridden():
    """Подсказку токена задаёт вызывающий: /settings знает про сохранённый секрет
    («пусто = не менять»), мастер — нет, и его placeholder не меняется."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={}, placeholders={"token": "сохранён · пусто = не менять"}
    )
    app = _harness(ed)
    async with app.run_test():
        assert (
            ed.query_one("#voice-token", Input).placeholder
            == "сохранён · пусто = не менять"
        )
        assert (
            ed.query_one("#voice-url", Input).placeholder
            == "https://…/v1/audio/transcriptions"
        )  # прочие поля не тронуты

    plain = VoiceEndpointEditor(values={})
    app2 = _harness(plain)
    async with app2.run_test():
        assert plain.query_one("#voice-token", Input).placeholder == ""


async def test_voice_endpoint_editor_readonly_fields_are_disabled_and_dimmed():
    """Поля-факты (локальный режим показывает адрес/токен/модель сервиса) не правятся:
    Textual-Input read_only не умеет, поэтому disabled + приглушающий класс."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={
            "url": "http://127.0.0.1:8735/v1/audio/transcriptions",
            "token": "не требуется",
            "model": "m",
            "language": "ru",
        },
        fields=("url", "token", "model", "language"),
        readonly=("url", "token", "model"),
        autofocus=False,
    )
    app = _harness(ed)
    async with app.run_test():
        for wid in ("#voice-url", "#voice-token", "#voice-model"):
            w = ed.query_one(wid, Input)
            assert w.disabled is True, wid
            assert "set-readonly" in w.classes, wid
        lang = ed.query_one("#voice-lang", Input)
        assert lang.disabled is False and "set-readonly" not in lang.classes


async def test_voice_endpoint_editor_readonly_token_is_not_masked():
    """Маска нужна секрету; read-only токен показывает ФАКТ («не требуется»), и
    звёздочки спрятали бы ровно то, ради чего поле показано."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={"token": "не требуется"}, readonly=("token",), autofocus=False
    )
    app = _harness(ed)
    async with app.run_test():
        assert ed.query_one("#voice-token", Input).password is False

    secret = VoiceEndpointEditor(values={"token": "sk"}, autofocus=False)
    app2 = _harness(secret)
    async with app2.run_test():
        assert (
            secret.query_one("#voice-token", Input).password is True
        )  # мастер не тронут


async def test_voice_endpoint_editor_focus_field_by_key():
    """Курсорная лента /settings ходит по полям поимённо — редактор умеет фокусировать
    любое своё поле, не только первое."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(
        values={},
        fields=("url", "token", "model", "language"),
        autofocus=False,
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.focus_field("model")
        await pilot.pause()  # focus() у Textual отложенный (call_later)
        assert ed.query_one("#voice-model", Input).has_focus
        ed.focus_field("language")
        await pilot.pause()
        assert ed.query_one("#voice-lang", Input).has_focus


async def test_voice_endpoint_editor_focuses_url_on_mount():
    """Поля монтируются в уже активный экран — редактор фокусирует первое сам
    (канон ConnectionEditor.on_mount)."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={})
    app = _harness(ed)
    async with app.run_test():
        assert ed.query_one("#voice-url", Input).has_focus


# --- список языков: контракт контроллера (канон пресет-листа) -------------------


def _lang_rows(ed):
    """Видимые строки списка языков (скрытые фильтром — display=False)."""
    return [str(w.render()) for w in ed.query(".voice-lang-row") if w.display]


def _four(values=None, **kw):
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    return VoiceEndpointEditor(
        values=values or {},
        fields=("url", "token", "model", "language"),
        autofocus=False,
        **kw,
    )


async def test_lang_list_opens_with_hint_then_all_codes():
    """Раскрытый список: первая строка — хинт канона, дальше ВСЕ языки whisper
    «код + русское название» в колонках, частые — сверху."""
    from gc_tui.editors.voice_endpoint import VOICE_LANGUAGES

    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        assert str(ed.query_one(".voice-lang-hint").render()) == (
            "языки · печать — фильтр/свой код · Enter — подставить · Esc — свернуть"
        )
        rows = _lang_rows(ed)
        assert len(rows) == len(VOICE_LANGUAGES) == 100
        assert (
            rows[0] == "▸ ru    русский"
        )  # ▸ на действующем коде (поле пусто → ru)
        assert rows[3] == "  de    немецкий"


# Эталон: коды `whisper.tokenizer.LANGUAGES` модели large-v3 (99 базовых + `yue`).
# Списком, а не «сам с собой»: сверка длины пропажу кода не ловит, а faster-whisper
# в венве агента не стоит, чтобы взять его из зависимости.
WHISPER_LANGUAGE_CODES = frozenset(
    [
        "af",
        "am",
        "ar",
        "as",
        "az",
        "ba",
        "be",
        "bg",
        "bn",
        "bo",
        "br",
        "bs",
        "ca",
        "cs",
        "cy",
        "da",
        "de",
        "el",
        "en",
        "es",
        "et",
        "eu",
        "fa",
        "fi",
        "fo",
        "fr",
        "gl",
        "gu",
        "ha",
        "haw",
        "he",
        "hi",
        "hr",
        "ht",
        "hu",
        "hy",
        "id",
        "is",
        "it",
        "ja",
        "jw",
        "ka",
        "kk",
        "km",
        "kn",
        "ko",
        "la",
        "lb",
        "ln",
        "lo",
        "lt",
        "lv",
        "mg",
        "mi",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "mt",
        "my",
        "ne",
        "nl",
        "nn",
        "no",
        "oc",
        "pa",
        "pl",
        "ps",
        "pt",
        "ro",
        "ru",
        "sa",
        "sd",
        "si",
        "sk",
        "sl",
        "sn",
        "so",
        "sq",
        "sr",
        "su",
        "sv",
        "sw",
        "ta",
        "te",
        "tg",
        "th",
        "tk",
        "tl",
        "tr",
        "tt",
        "uk",
        "ur",
        "uz",
        "vi",
        "yi",
        "yo",
        "yue",
        "zh",
    ]
)


def test_whisper_reference_set_is_the_large_v3_alphabet():
    """Страховка самого эталона: 100 кодов large-v3 без дублей."""
    assert len(WHISPER_LANGUAGE_CODES) == 100


async def test_lang_list_carries_every_whisper_language():
    """Полный набор whisper, а не «частые»: сверху — частые в заданном порядке, дальше
    остальные по алфавиту кода; трёхбуквенные `haw`/`yue` не ломают колонку."""
    from gc_tui.editors.voice_endpoint import VOICE_LANGUAGES

    codes = [c for c, _ in VOICE_LANGUAGES]
    assert codes[:12] == [
        "ru",
        "en",
        "uk",
        "de",
        "fr",
        "es",
        "it",
        "pt",
        "zh",
        "ja",
        "tr",
        "kk",
    ]
    assert codes[12:] == sorted(codes[12:])
    assert len(codes) == len(set(codes))  # без дублей
    assert set(codes) == WHISPER_LANGUAGE_CODES  # ни пропаж, ни лишних
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        rows = _lang_rows(ed)
        assert "  haw   гавайский" in rows
        assert "  yue   кантонский" in rows


async def test_lang_list_rows_live_in_a_capped_scroll_frame():
    """99 строк не имеют права раскатать секцию: они монтируются во ВЛОЖЕННЫЙ скролл
    канона (`.dropdown-scroll` — потолок высоты) внутри рамки группы поля."""
    from textual.containers import VerticalScroll

    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        scroll = ed.query_one(".voice-lang-scroll")
        assert isinstance(scroll, VerticalScroll)
        assert (
            "dropdown-scroll" in scroll.classes and "onb-dyn" in scroll.classes
        )
        assert scroll.can_focus is False  # фокус остаётся в поле
        rows = list(ed.query(".voice-lang-row"))
        assert len(rows) == 100
        assert all(r.parent is scroll for r in rows)


async def test_lang_list_scroll_frame_goes_away_with_the_list():
    """Свернули — контейнер уходит вместе со строками (иначе пустая рамка осталась бы
    висеть под полем)."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.lang_list.close()
        await pilot.pause()
        assert not ed.query(".voice-lang-scroll") and not ed.query(
            ".voice-lang-row"
        )


async def test_lang_list_scrolls_the_highlighted_row_inside_the_frame(
    monkeypatch,
):
    """↑/↓ по 99 языкам подводят выделенную строку сами — внутри рамки-скролла
    (за курсором списка Textual не следит: фокус держит поле)."""
    from textual.widgets import Static

    seen = []
    original = Static.scroll_visible
    monkeypatch.setattr(
        Static,
        "scroll_visible",
        lambda self, *a, **kw: (seen.append(self), original(self, *a, **kw))[
            1
        ],
    )
    ed = _four()
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.lang_list.apply_filter(
            "йоруба"
        )  # последний код списка — заведомо за фолдом
        for _ in range(5):  # call_after_refresh — после layout-прохода
            await pilot.pause()
        rows = [w for w in ed.query(".voice-lang-row") if w.display]
        assert len(rows) == 1 and rows[0] in seen


async def test_lang_list_highlights_the_current_code():
    """Курсор списка встаёт на ДЕЙСТВУЮЩИЙ код поля: Enter сразу после входа не имеет
    права молча подменить чужой язык первой строкой."""
    ed = _four({"language": "de"})
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        assert [r for r in _lang_rows(ed) if r.startswith("▸")] == [
            "▸ de    немецкий"
        ]


async def test_lang_list_highlights_nothing_for_an_own_code():
    """Свой код (не из списка) — подсвеченной строки нет вовсе: Enter про введённое."""
    ed = _four({"language": "zz"})
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        assert not [r for r in _lang_rows(ed) if r.startswith("▸")]
        assert ed.pick_language() is False
        assert ed.query_one("#voice-lang", Input).value == "zz"


async def test_lang_list_filters_by_code_and_by_name_without_rebuilding_dom():
    """Печать в поле — display-фильтр УЖЕ смонтированных строк (канон: без пересборки
    DOM на каждый keystroke); матчится и код, и русское название."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        before = list(ed.query(".voice-lang-row"))
        ed.lang_list.apply_filter("нем")
        assert _lang_rows(ed) == ["▸ de    немецкий"]
        ed.lang_list.apply_filter("kk")
        assert _lang_rows(ed) == ["▸ kk    казахский"]
        assert list(ed.query(".voice-lang-row")) == before  # те же виджеты


async def test_lang_list_hint_is_honest_when_nothing_matches():
    """Фильтр без совпадений: обещать «Enter — подставить» нельзя — подставлять нечего,
    Enter сохранит введённое (канон _LIST_NO_MATCH списка моделей)."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        ed.lang_list.apply_filter("zz")
        assert _lang_rows(ed) == []
        assert str(ed.query_one(".voice-lang-hint").render()) == (
            "свой код · Enter — сохранить как есть · Esc — свернуть"
        )


async def test_lang_list_move_cycles_and_reports_that_it_ate_the_key():
    """↑/↓ ходят по списку и говорят хозяину, что клавиша израсходована (приоритет над
    лентой секции — канон is_open в move хоста)."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        assert ed.lang_list.move(1) is False  # свёрнут — клавиша не наша
        ed.lang_list.open(forced=True)
        assert ed.lang_list.move(1) is True
        assert [r for r in _lang_rows(ed) if r.startswith("▸")] == [
            "▸ en    английский"
        ]
        ed.lang_list.move(-1)
        ed.lang_list.move(-1)
        assert [r for r in _lang_rows(ed) if r.startswith("▸")] == [
            "▸ yue   кантонский"
        ]


async def test_lang_list_pick_substitutes_and_collapses():
    ed = _four({"language": "de"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.lang_list.apply_filter("франц")
        assert ed.pick_language() is True
        assert ed.query_one("#voice-lang", Input).value == "fr"
        assert ed.lang_list.is_open is False
        await pilot.pause()
        assert not ed.query(".voice-lang-row")  # строки ушли со списком
        # Строка под полем остаётся (канон форм 2026-08-17) и говорит, чем список
        # вернуть: контроллер приводит её в согласие с состоянием, а не сносит.
        from gc_tui.editors.field_list import LIST_CLOSED_HINT

        hints = list(ed.query(".voice-lang-hint"))
        assert len(hints) == 1 and str(hints[0].render()) == LIST_CLOSED_HINT


async def test_lang_list_rows_sit_right_under_the_field():
    """Группа поля «Язык» и подсказка под ней монтируются ПОД полем, а не в хвост
    редактора: список обязан быть приклеен к своему полю (строка ошибки редактора
    остаётся ниже него)."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed._show_error("ошибка снизу")
        ed.lang_list.open(forced=True)
        children = list(ed.children)
        i = children.index(ed.query_one("#voice-lang-group"))
        assert "voice-lang-hint" in children[i + 1].classes
        assert children[-1].id == "voice-error"


async def test_voice_list_shares_frame_with_its_field():
    """Список языка живёт внутри рамки СВОЕГО поля, а не отдельным блоком (спека §3.1):
    поле и строки — под одной рамкой группы, подсказка про клавиши — под ней."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        group = ed.query_one("#voice-lang-group")
        assert "field-group" in group.classes
        field = ed.query_one("#voice-lang", Input)
        # ПРЯМОЙ ребёнок группы: и признак set-disabled (PasteInput), и правила
        # `.field-group > …` смотрят ровно на непосредственного родителя.
        assert field.parent is group
        assert "group-field" in field.classes  # воздух внутри рамки — на поле
        assert (
            "set-framed" not in field.classes
        )  # рамка одна, её рисует группа
        scroll = ed.query_one(".voice-lang-scroll")
        assert scroll.parent is group  # строки — в той же рамке
        hint = ed.query_one(".voice-lang-hint")
        assert hint.parent is ed  # подсказка — ПОД рамкой
        children = list(ed.children)
        assert children.index(hint) == children.index(group) + 1


async def test_voice_fields_without_a_list_keep_their_own_frame():
    """Группа нужна там, где есть что объединять: у URL и токена списка нет — рамку им
    по-прежнему рисует само поле."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        for wid in ("#voice-url", "#voice-token"):
            field = ed.query_one(wid, Input)
            assert "set-framed" in field.classes
            assert field.parent is ed
        assert not ed.query("#voice-url-group") and not ed.query(
            "#voice-token-group"
        )


async def test_lang_list_close_restores_the_pre_filter_code():
    """Свернуть список = отменить фильтр: набранное в поле — ФИЛЬТР, а не код языка, и
    после Esc/ухода с поля в поле обязан вернуться прежний код (иначе следующий Enter
    сохранил бы «нем» как язык распознавания)."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.query_one("#voice-lang", Input).value = "нем"
        await pilot.pause()
        assert _lang_rows(ed) == ["▸ de    немецкий"]
        ed.lang_list.close()
        assert ed.query_one("#voice-lang", Input).value == "en"


async def test_lang_list_pick_keeps_what_it_put_in_the_field():
    """Подстановка — осознанный выбор: её `close` не откатывает (в отличие от Esc)."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.query_one("#voice-lang", Input).value = "нем"
        await pilot.pause()
        assert ed.pick_language() is True
        assert ed.query_one("#voice-lang", Input).value == "de"


async def test_lang_list_pick_keeps_an_own_code_typed_over_the_filter():
    """Свой код (фильтр без совпадений) Enter тоже не откатывает — сохранится введённое."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        ed.query_one("#voice-lang", Input).value = "zz"
        await pilot.pause()
        assert ed.pick_language() is False
        assert ed.query_one("#voice-lang", Input).value == "zz"


async def test_lang_list_pick_makes_the_new_value_the_baseline():
    """Enter фиксирует значение поля: следующая печать + Esc откатывают к ПОДСТАВЛЕННОМУ
    коду, а не к значению входа на поле. Иначе после «вошёл → Enter (сохранил en) →
    печать → Esc» в поле оказывался бы старый «ru», и доверчивый Enter молча затирал бы
    только что сохранённое."""
    ed = _four({"language": "ru"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        field = ed.query_one("#voice-lang", Input)
        field.focus()
        await pilot.pause()
        ed.lang_list.open(forced=True)
        field.value = "англ"
        await pilot.pause()
        assert ed.pick_language() is True and field.value == "en"
        field.value = "англи"  # печать раскрывает список заново
        await pilot.pause()
        assert ed.lang_list.is_open
        ed.lang_list.close()  # Esc — откат к зафиксированному Enter'ом
        assert field.value == "en"


async def test_lang_list_own_code_becomes_the_baseline_too():
    """Свой код, набранный поверх фильтра, Enter фиксирует ровно так же (отдельной ветки
    «сохранения своего кода» нет — она проходит тем же `pick`)."""
    ed = _four({"language": "ru"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        field = ed.query_one("#voice-lang", Input)
        field.focus()
        await pilot.pause()
        ed.lang_list.open(forced=True)
        field.value = "zz"
        await pilot.pause()
        assert (
            ed.pick_language() is False
        )  # подставлять нечего — сохранится введённое
        field.value = "zzz"
        await pilot.pause()
        ed.lang_list.close()
        assert field.value == "zz"


async def test_programmatic_write_leaves_the_cursor_inside_the_value():
    """`set_reactive` обходит переклэмп выделения в `Input._watch_value`: после отката
    фильтра курсор оставался за границей нового значения (самоисцелялся только следующей
    клавишей). Ставим его в конец значения сами — на обоих путях записи."""
    ed = _four({"language": "ru"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        field = ed.query_one("#voice-lang", Input)
        field.focus()
        await pilot.pause()
        ed.lang_list.open(forced=True)
        field.value = "англ"
        field.cursor_position = 4  # как после печати — курсор в конце
        await pilot.pause()
        ed.lang_list.close()  # откат фильтра: значение стало короче
        assert field.value == "ru"
        assert field.cursor_position <= len("ru")

        ed.lang_list.open(forced=True)
        field.value = "англ"
        field.cursor_position = 4
        await pilot.pause()
        assert (
            ed.pick_language() is True
        )  # второй путь записи — подстановка кода
        assert field.value == "en"
        assert field.cursor_position <= len("en")


async def test_lang_list_move_gives_the_key_back_when_nothing_matches():
    """Фильтр не оставил строк — ходить нечем: клавишу списку не забираем, иначе ↑/↓
    «умирают» без всякой обратной связи (при непустом списке — забираем, это канон)."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        assert ed.lang_list.move(1) is True
        ed.query_one("#voice-lang", Input).value = "zz"
        await pilot.pause()
        assert ed.lang_list.move(1) is False
        assert ed.lang_list.has_matches is False


async def test_lang_list_scrolls_the_hint_when_no_row_is_highlighted(
    monkeypatch,
):
    """Подсвеченной строки нет (свой код в поле) — подводим в видимую область ХИНТ:
    стрелки уже съедены списком, и раскрытие за фолдом выглядит как «клавиши умерли»."""
    from textual.widgets import Static

    seen = []
    original = Static.scroll_visible

    def _spy(self, *a, **kw):
        seen.append(self)
        return original(self, *a, **kw)

    monkeypatch.setattr(Static, "scroll_visible", _spy)
    ed = _four({"language": "zz"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)
        for _ in range(5):  # call_after_refresh — после layout-прохода
            await pilot.pause()
        assert ed.query_one(".voice-lang-hint") in seen


# --- раскрытие свёрнутого списка печатью / Ctrl+Space (Reopen) ------------------


async def test_lang_list_reopen_for_typing_opens_a_closed_list_and_filters():
    """Список свёрнут (например, Esc'ом) — `reopen_for_typing` раскрывает его заново и
    сразу фильтрует переданным текстом, одним действием (без промежуточного «всё»)."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        ed.lang_list.close()  # свернули — как после Esc
        assert ed.lang_list.is_open is False
        ed.lang_list.reopen_for_typing("нем")
        assert ed.lang_list.is_open is True
        assert _lang_rows(ed) == ["▸ de    немецкий"]


async def test_lang_list_reopen_for_typing_is_a_noop_when_already_open():
    """Список уже раскрыт — повторный вызов ничего не пересобирает (строки не задвоятся)
    и не фильтрует: `apply_filter` уже отвечает за печать в открытом списке."""
    ed = _four()
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        before = list(ed.query(".voice-lang-row"))
        assert len(_lang_rows(ed)) == 100
        ed.lang_list.reopen_for_typing("de")
        assert (
            list(ed.query(".voice-lang-row")) == before
        )  # те же виджеты, не задвоились
        assert len(_lang_rows(ed)) == 100  # фильтр "de" не применился


async def test_lang_list_reopen_for_typing_does_not_touch_value0():
    """Раскрытие по печати продолжает уже начатую правку, а не новую сессию: `_value0`
    обязан остаться тем, что было ДО неё — иначе следующий Esc вернёт в поле остаток
    фильтра вместо исходного кода (полусырое значение).

    Поле правится ДО вызова `reopen_for_typing` (как в реальном `on_input_changed`,
    где значение поля уже поменялось к моменту обработчика) — иначе тест не поймал бы
    реализацию, ошибочно берущую `_value0` из ТЕКУЩЕГО значения поля внутри метода."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.lang_list.open(forced=True)  # _value0 = "en"
        ed.lang_list.close()  # свернули, поле всё ещё "en"
        ed.query_one(
            "#voice-lang", Input
        ).value = "нем"  # печать уже поменяла текст поля
        await pilot.pause()
        ed.lang_list.reopen_for_typing("нем")  # печать — НЕ новый вход на поле
        ed.lang_list.close()  # Esc после раскрытия-по-печати
        assert (
            ed.query_one("#voice-lang", Input).value == "en"
        )  # прежний код, не "нем"


async def test_editor_typing_reopens_a_closed_list_when_the_field_has_focus():
    """Печать в поле «Язык», когда список свёрнут, а поле в фокусе — раскрывает список
    (см. `VoiceEndpointEditor.on_input_changed`): человек продолжает печатать, раскрыть
    список Esc'ом было нечем."""
    ed = _four({"language": "en"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.focus_field("language")
        await pilot.pause()  # focus() у Textual отложенный
        ed.lang_list.open(forced=True)
        ed.lang_list.close()
        assert ed.lang_list.is_open is False
        assert ed.query_one("#voice-lang", Input).has_focus
        ed.query_one("#voice-lang", Input).value = "нем"
        await pilot.pause()
        assert ed.lang_list.is_open is True
        assert _lang_rows(ed) == ["▸ de    немецкий"]


async def test_editor_does_not_reopen_when_the_field_lacks_focus():
    """Программная запись в поле БЕЗ фокуса (пересборка редактора после сохранения,
    восстановление черновика и т. п.) не должна читаться как печать человека — иначе
    список раскрывался бы сам, без единого нажатия."""
    ed = _four({"language": "ru"})
    app = _harness(ed)
    async with app.run_test() as pilot:
        assert not ed.query_one("#voice-lang", Input).has_focus
        assert ed.lang_list.is_open is False
        ed.query_one("#voice-lang", Input).value = "es"
        await pilot.pause()
        assert ed.lang_list.is_open is False
        assert not ed.query(".voice-lang-row")


def test_normalized_language_takes_two_or_three_latin_letters():
    """Код языка — 2-3 латинские буквы (ISO-639-1/2, как ждёт whisper), в нижнем
    регистре. Всё прочее — почти всегда остаток фильтра списка, и в конфиг ему нельзя."""
    from gc_tui.editors.voice_endpoint import normalized_language

    assert normalized_language("ru") == "ru"
    assert normalized_language("kaz") == "kaz"
    assert normalized_language("  EN ") == "en"
    for bad in ("нем", "r", "abcd", "e1", "ru-RU", "", None):
        assert normalized_language(bad) is None, bad


async def test_lang_list_does_not_open_without_the_field():
    """Мастер языка не спрашивает — раскрывать нечего (и падать нельзя)."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={}, fields=("url", "token", "model"))
    app = _harness(ed)
    async with app.run_test():
        ed.lang_list.open(forced=True)
        assert ed.lang_list.is_open is False
        assert not ed.query(".voice-lang-row")
        assert ed.pick_language() is False


# --- список моделей endpoint'а: контракт контроллера --------------------------

_STT = ["whisper-large-v3-turbo", "Systran/faster-whisper-small"]


def _model_rows(ed):
    """Видимые строки списка моделей (скрытые фильтром — display=False)."""
    return [str(w.render()) for w in ed.query(".voice-model-row") if w.display]


def _model_hint(ed):
    hints = list(ed.query(".voice-model-hint"))
    return str(hints[-1].render()) if hints else None


def _cloud(values=None, *, fetch=None, **kw):
    """Редактор облачной панели /settings: четыре поля, список моделей включён
    инъекцией fetch (мастер её не даёт — см. отдельный тест)."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    vals = {"url": "https://s.x/v1/audio/transcriptions"}
    vals.update(values or {})
    return VoiceEndpointEditor(
        values=vals,
        fields=("url", "token", "model", "language"),
        autofocus=False,
        fetch=fetch,
        **kw,
    )


async def _settle(pilot, cond, tries=100):
    """Дождаться результата воркера загрузки (он в отдельном потоке + call_from_thread)."""
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause()
    return False


def test_stt_models_first_keeps_only_speech_models_when_there_are_any():
    """На общем OpenAI-совместимом сервере рядом с whisper лежат сотни чат-моделей:
    показываем только распознающие речь — их id несут whisper либо transcribe."""
    from gc_tui.editors.voice_endpoint import stt_models_first

    assert stt_models_first(
        [
            "gpt-4o",
            "whisper-1",
            "Systran/faster-whisper-small",
            "gpt-4o-transcribe",
            "CodeChat",
        ]
    ) == ["whisper-1", "Systran/faster-whisper-small", "gpt-4o-transcribe"]
    assert stt_models_first(["WHISPER-Large-V3"]) == [
        "WHISPER-Large-V3"
    ]  # регистр не важен


def test_stt_models_first_shows_everything_when_nothing_matches():
    """Нестандартный сервер (своё имя модели) прятать нельзя — иначе список пуст там,
    где сервер честно ответил."""
    from gc_tui.editors.voice_endpoint import stt_models_first

    assert stt_models_first(["stt-ru-v2", "asr"]) == ["stt-ru-v2", "asr"]
    assert stt_models_first([]) == []
    assert stt_models_first(None) == []


async def test_model_list_is_absent_without_a_fetch():
    """Мастер список не получает: он спрашивает адрес и токен ВПЕРВЫЕ, и ходить с ними
    в сеть на каждый вход в поле там незачем."""
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    ed = VoiceEndpointEditor(values={})  # ровно так строит мастер
    app = _harness(ed)
    async with app.run_test():
        assert ed.model_list is None
        assert ed.list_for("model") is None
        assert ed.pick_in("model") is False  # и падать не имеет права


async def test_model_list_is_absent_for_a_readonly_model_field():
    """Локальная панель показывает модель сервиса ФАКТОМ — выбирать там нечего."""
    ed = _cloud(
        {"model": "Systran/faster-whisper-small"},
        fetch=lambda url, token, verify, **kw: _STT,
        readonly=("url", "token", "model"),
    )
    app = _harness(ed)
    async with app.run_test():
        assert ed.model_list is None


async def test_model_list_loads_rows_into_a_capped_scroll_frame():
    """Вход на поле раскрывает список и уходит за ним на сервер: строки — в том же
    скролле канона, что у языка, под рамкой его группы."""
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert _model_rows(ed) == [
            "▸ whisper-large-v3-turbo",
            "  Systran/faster-whisper-small",
        ]
        assert _model_hint(ed) == (
            "модели · печать — фильтр/свой id · Enter — подставить · Esc — свернуть · Alt+↓ — перечитать список"
        )
        scroll = ed.query_one(".voice-model-scroll")
        assert (
            "dropdown-scroll" in scroll.classes and scroll.can_focus is False
        )
        assert all("voice-model-row" in w.classes for w in scroll.children)


async def test_model_list_shares_frame_with_its_field():
    """Список моделей — в рамке своего поля (тот же канон, что у языка): поле теряет
    свою рамку, строки приезжают ВНУТРЬ группы, подсказка живёт под ней."""
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        group = ed.query_one("#voice-model-group")
        assert "field-group" in group.classes
        field = ed.query_one("#voice-model", Input)
        assert field.parent is group
        assert (
            "group-field" in field.classes
            and "set-framed" not in field.classes
        )
        scroll = ed.query_one(".voice-model-scroll")
        assert scroll.parent is group
        # Порядок внутри рамки: сначала поле, потом строки. Без этого mount(before=field)
        # нарисовал бы список НАД полем ввода, и все ассерты выше остались бы зелёными.
        assert list(group.children) == [field, scroll]
        hint = ed.query_one(".voice-model-hint")
        assert hint.parent is ed
        children = list(ed.children)
        assert children.index(hint) == children.index(group) + 1


async def test_model_field_without_a_list_keeps_its_own_frame():
    """Поле-факт локальной панели списка не получает — и группы тоже: рамка остаётся
    на поле, как у соседних фактов (адрес, токен)."""
    ed = _cloud(
        {"model": "Systran/faster-whisper-small"},
        fetch=lambda url, token, verify, **kw: _STT,
        readonly=("url", "token", "model"),
    )
    app = _harness(ed)
    async with app.run_test():
        assert ed.model_list is None
        assert not ed.query("#voice-model-group")
        field = ed.query_one("#voice-model", Input)
        assert "set-framed" in field.classes and field.parent is ed
        # Инвариант волны: в группе поле не бывает отключённым. Иначе .field-group
        # .set-disabled погасит одну рамку, а одинокие соседи останутся яркими —
        # тот самый разнобой, который эта задача и обошла (см. Task 4a).
        for grp in ed.query(".field-group"):
            assert not any(w.disabled for w in grp.children)


async def test_model_list_says_it_is_loading_before_the_answer():
    """Пока запрос летит, под полем висит одна строка-статус: пустая рамка-скролл
    читалась бы как поломка."""
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test():
        ed.model_list.open(forced=True)  # воркер ещё не ответил
        assert _model_hint(ed) == "загружаю модели…"
        assert not ed.query(".voice-model-scroll")


async def test_model_list_failure_does_not_block_manual_entry():
    """Сервер не ответил: секция цела, статус честный, ↑/↓ возвращаются ленте, а Enter
    сохраняет набранное руками."""

    def _boom(url, token, verify, **kw):
        raise RuntimeError("connection refused")

    ed = _cloud({"model": "my-own-stt"}, fetch=_boom)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(
            pilot, lambda: _model_hint(ed) != "загружаю модели…"
        )
        assert (
            _model_hint(ed)
            == "не удалось получить список — можно вписать вручную"
        )
        assert (
            ed.model_list.move(1) is False
            and ed.model_list.has_matches is False
        )
        assert ed.pick_in("model") is False
        assert ed.query_one("#voice-model", Input).value == "my-own-stt"


async def test_model_list_reports_an_empty_server_answer():
    ed = _cloud(fetch=lambda url, token, verify, **kw: [])
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(
            pilot, lambda: _model_hint(ed) != "загружаю модели…"
        )
        assert _model_hint(ed) == "сервер не вернул моделей"
        assert not ed.query(".voice-model-row")


async def test_model_list_stays_silent_without_a_url():
    """Адреса нет — идти некуда: ни запроса в пустоту, ни выговора (человек ещё только
    заполняет форму, и вход курсора в поле не повод его отчитывать)."""
    calls = []

    def _fetch(url, token, verify, **kw):
        calls.append(url)
        return _STT

    ed = _cloud({"url": ""}, fetch=_fetch)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open()  # именно автовход: на явную просьбу молчать нельзя
        await pilot.pause()
        assert calls == []
        assert ed.model_list.is_open is False
        assert not ed.query(".voice-model-row")
        # «Молчит» — это «строк нет и в сеть не ходили». Строка под полем при этом
        # стоит и называет клавишу (канон 2026-08-20: у полей со списком подсказка
        # есть с первого кадра, а сходить и объяснить — дело самой клавиши).
        from gc_tui.editors.voice_endpoint import _MODEL_RELOAD_HINT

        assert _model_hint(ed) == _MODEL_RELOAD_HINT


async def test_model_list_asks_the_address_and_token_of_the_fields():
    """Список — про endpoint, который человек ПРАВИТ сейчас: адрес и токен берутся из
    полей, а пустое поле токена значит сохранённый (правило самого сохранения)."""
    seen = []
    ed = _cloud(
        fetch=lambda url, token, verify, **kw: (
            seen.append((url, token, verify)) or _STT
        ),
        saved_token="saved-secret",
        verify_ssl=False,
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: seen)
        assert seen[-1] == (
            "https://s.x/v1/audio/transcriptions",
            "saved-secret",
            False,
        )
        ed.model_list.close()
        ed.query_one(
            "#voice-url", Input
        ).value = "https://other.x/v1/audio/transcriptions"
        ed.query_one("#voice-token", Input).value = "typed-now"
        await pilot.pause()
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: len(seen) > 1)
        assert seen[-1] == (
            "https://other.x/v1/audio/transcriptions",
            "typed-now",
            False,
        )


async def test_model_list_ignores_a_stale_answer():
    """Ответ протухшей загрузки (список успели закрыть/переоткрыть, пока запрос летел)
    не имеет права лечь на текущее раскрытие — ни состоянием, ни DOM."""
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        stale_gen = ed.model_list._gen - 1
        ed.model_list._on_loaded(["ghost-model"], stale_gen, "")
        assert _model_rows(ed) == [
            "▸ whisper-large-v3-turbo",
            "  Systran/faster-whisper-small",
        ]
        ed.model_list.close()
        await pilot.pause()
        ed.model_list._on_loaded(
            ["ghost-model"], stale_gen, ""
        )  # список уже свёрнут
        assert not ed.query(".voice-model-row")


async def test_model_list_filters_by_typing_and_picks_into_the_field():
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.focus_field("model")
        await pilot.pause()
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        ed.query_one("#voice-model", Input).value = "faster"
        await pilot.pause()
        assert _model_rows(ed) == ["▸ Systran/faster-whisper-small"]
        assert ed.pick_in("model") is True
        assert (
            ed.query_one("#voice-model", Input).value
            == "Systran/faster-whisper-small"
        )
        assert ed.model_list.is_open is False


async def test_model_list_hint_is_honest_when_the_filter_matches_nothing():
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        ed.model_list.apply_filter("no-such-model")
        assert _model_rows(ed) == []
        assert (
            _model_hint(ed)
            == "свой id · Enter — сохранить как есть · Esc — свернуть"
        )


async def test_model_list_reload_asks_the_server_again():
    """Alt+↓ — «перечитать»: сервер мог ожить, адрес или токен — смениться."""
    answers = [None, _STT]
    ed = _cloud(fetch=lambda url, token, verify, **kw: answers.pop(0))
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(
            pilot, lambda: _model_hint(ed) != "загружаю модели…"
        )
        assert (
            _model_hint(ed)
            == "не удалось получить список — можно вписать вручную"
        )
        ed.model_list.reload()
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert _model_rows(ed)[0] == "▸ whisper-large-v3-turbo"


async def test_model_list_and_lang_list_do_not_share_widgets():
    """Два списка в одном редакторе — каждый со своими виджетами: свернуть один не
    значит снести строки другого."""
    ed = _cloud(fetch=lambda url, token, verify, **kw: _STT)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        ed.lang_list.open(forced=True)
        assert len(_lang_rows(ed)) == 100 and len(_model_rows(ed)) == 2
        ed.lang_list.close()
        await pilot.pause()
        assert not ed.query(".voice-lang-row") and len(_model_rows(ed)) == 2


async def test_model_list_highlights_the_current_model():
    """Вход на поле подсвечивает ДЕЙСТВУЮЩУЮ модель (канон языка), а не первую строку
    сервера — и показывает при этом весь список, а не отфильтрованный её id."""
    ed = _cloud(
        {"model": "Systran/faster-whisper-small"},
        fetch=lambda url, token, verify, **kw: _STT,
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert _model_rows(ed) == [
            "  whisper-large-v3-turbo",
            "▸ Systran/faster-whisper-small",
        ]


async def test_model_list_highlights_nothing_for_an_own_id():
    """В поле id, которого сервер не отдал: подсвеченной строки нет — Enter сразу после
    входа не имеет права молча подменить её первой строкой списка."""
    ed = _cloud(
        {"model": "my-own-stt"}, fetch=lambda url, token, verify, **kw: _STT
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert not [r for r in _model_rows(ed) if r.startswith("▸")]
        assert ed.pick_in("model") is False
        assert ed.query_one("#voice-model", Input).value == "my-own-stt"


async def test_model_list_reload_keeps_the_pre_filter_baseline():
    """Ctrl+Space по РАСКРЫТОМУ списку — перечитывание, а не новый вход на поле: в поле
    сейчас лежит ФИЛЬТР, и делать его «значением до фильтра» нельзя — Esc вернул бы
    остаток фильтра, а доверчивый Enter записал бы его в конфиг как id модели."""
    ed = _cloud(
        {"model": "whisper-old"}, fetch=lambda url, token, verify, **kw: _STT
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        field = ed.query_one("#voice-model", Input)
        field.focus()
        await pilot.pause()
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        field.value = "faster"  # печать = фильтр
        await pilot.pause()
        assert ed.model_list.reload() is True
        assert await _settle(pilot, lambda: _model_rows(ed))
        ed.model_list.close()  # Esc — откат фильтра
        assert field.value == "whisper-old"


async def test_model_list_reload_keeps_the_typed_filter_in_sync():
    """После перечитывания список показывает то же, что и поле: иначе футер обещает
    «Enter — подставить», а подставлять нечего (в поле фильтр, в списке — всё подряд)."""
    ed = _cloud(
        {"model": "whisper-old"}, fetch=lambda url, token, verify, **kw: _STT
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        field = ed.query_one("#voice-model", Input)
        field.focus()
        await pilot.pause()
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        field.value = "faster"
        await pilot.pause()
        ed.model_list.reload()
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert _model_rows(ed) == ["▸ Systran/faster-whisper-small"]
        assert ed.model_list.has_matches is True


async def test_model_list_reload_of_a_collapsed_list_shows_everything():
    """А вот по СВЁРНУТОМУ (после Esc) в поле лежит настоящий id, не фильтр: сужать по
    нему список нельзя — своей модели среди чужих может не быть вовсе."""
    ed = _cloud(
        {"model": "whisper-old"}, fetch=lambda url, token, verify, **kw: _STT
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(pilot, lambda: _model_rows(ed))
        ed.model_list.close()
        await pilot.pause()
        assert ed.model_list.reload() is True
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert len(_model_rows(ed)) == 2


async def test_model_list_reload_without_typing_shows_everything():
    """Список раскрыт, но человек ничего не печатал — в поле настоящий id, не фильтр:
    перечитывание показывает ВЕСЬ ответ сервера (иначе после неудачной первой попытки
    Ctrl+Space сузил бы список сохранённым id и показал бы пусто)."""
    answers = [None, _STT]
    ed = _cloud(
        {"model": "whisper-old"},
        fetch=lambda url, token, verify, **kw: answers.pop(0),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        ed.model_list.open(forced=True)
        assert await _settle(
            pilot, lambda: _model_hint(ed) != "загружаю модели…"
        )
        assert (
            _model_hint(ed)
            == "не удалось получить список — можно вписать вручную"
        )
        ed.model_list.reload()
        assert await _settle(pilot, lambda: _model_rows(ed))
        assert len(_model_rows(ed)) == 2


@pytest.mark.parametrize(
    "list_attr, key, value",
    [
        (
            "url_list",
            "url",
            "https://api.groq.com/openai/v1/audio/transcriptions",
        ),
        ("model_list", "model", "whisper-1"),
        ("lang_list", "language", "ru"),
    ],
)
async def test_no_list_of_the_panel_opens_over_a_filled_field(
    list_attr, key, value
):
    """Правило раскрытия ОДНО на все три списка панели: сам — только на пустом поле,
    поверх значения — явная клавиша.

    Живой смок 2026-08-20: правило чинилось по одному полю (сначала адрес, потом
    модель), и «Язык» остался с прежним поведением — список раскрывался поверх
    сохранённого кода, а Esc его стирал. Тест держит весь класс полей разом, чтобы
    следующая правка снова не оставила одно из них позади."""
    from gc_core.config_modules.endpoint_presets import EndpointPreset

    presets = [
        EndpointPreset(
            name="Groq",
            url="https://api.groq.com/openai/v1/audio/transcriptions",
        ),
        EndpointPreset(
            name="OpenAI", url="https://api.openai.com/v1/audio/transcriptions"
        ),
    ]
    ed = _cloud({key: value}, fetch=lambda *a, **kw: _STT, url_presets=presets)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ctrl = getattr(ed, list_attr)
        ctrl.open()  # как вход курсора на поле
        await pilot.pause()
        assert ctrl.is_open is False, "список раскрылся сам поверх значения"
        ctrl.open(forced=True)  # как Alt+↓
        await pilot.pause()
        assert ctrl.is_open is True, "явная клавиша обязана раскрыть"


@pytest.mark.parametrize(
    "list_attr, field_id, key, value",
    [
        (
            "url_list",
            "voice-url",
            "url",
            "https://api.groq.com/openai/v1/audio/transcriptions",
        ),
        ("model_list", "voice-model", "model", "whisper-1"),
        ("lang_list", "voice-lang", "language", "ru"),
    ],
)
async def test_escape_over_a_filled_field_keeps_its_value(
    list_attr, field_id, key, value
):
    """И закрытие ОДНО на все три: Esc убирает список, значение поля остаётся."""
    from gc_core.config_modules.endpoint_presets import EndpointPreset

    presets = [
        EndpointPreset(
            name="Groq",
            url="https://api.groq.com/openai/v1/audio/transcriptions",
        ),
        EndpointPreset(
            name="OpenAI", url="https://api.openai.com/v1/audio/transcriptions"
        ),
    ]
    ed = _cloud({key: value}, fetch=lambda *a, **kw: _STT, url_presets=presets)
    app = _harness(ed)
    async with app.run_test() as pilot:
        ctrl = getattr(ed, list_attr)
        ctrl.open(forced=True)
        await pilot.pause()
        ctrl.close()
        await pilot.pause()
        assert ed.query_one(f"#{field_id}", Input).value == value

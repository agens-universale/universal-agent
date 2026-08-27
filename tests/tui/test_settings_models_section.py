"""Секция «Модели» хаба /settings — реестр `models` базового config.yml (волна B, Task 3).

Секция ПОГЛОЩАЕТ легаси-«Модель»: пустой реестр = сегодняшний редактор модели
целиком (`ModelEditor`, правит `agent.model`) плюс вход в реестр; непустой —
список записей + строка «резервная модель». Запись — немедленно по действию через
`config_modules/registry_write` (секция вышла из save-машины `_EDITOR_SECTIONS`),
цель — базовый config.yml, показывается ФАЙЛОВОЕ состояние цели (решение 6 плана).
"""
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from textual.widgets import Input, Static

from tests.tui.conftest import (_isolated_config_layers, open_settings_section,
                                wait_until)
from tests.tui.test_settings_screen import _cfg, _loaded, _mk_screen

_SECTION = "Модели"


def _flat(model: str = '"test-model"') -> str:
    return ("backend:\n"
            '  completions_url: "http://host/v1/chat/completions"\n'
            "  chat_completion_headers:\n"
            '    Authorization: "Bearer secret-token"\n'
            "agent:\n"
            f"  model: {model}\n")


_REGISTRY = (
    "models:\n"
    "  flash:\n"
    '    model: "google/gemini-2.5-flash"\n'
    "  opus:\n"
    '    model: "anthropic/claude-opus"\n'
    "slots:\n"
    '  default: "flash"\n'
)


def _write_cfg(tmp_path, body: str = "", *, model: str = '"test-model"') -> Path:
    p = tmp_path / "config.yml"
    p.write_text(_flat(model) + body, encoding="utf-8")
    return p


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _texts(scr) -> str:
    return " ".join(str(w.render()) for w in scr.query(Static))


def _editor(scr):
    from tui.editors.models_section import ModelsSectionEditor
    return scr.query(ModelsSectionEditor).last()


def _rows(scr) -> list:
    return [str(w.render()) for w in scr.query(".models-row") if w.display]


async def _wait_legacy_list(pilot, scr) -> None:
    """Дождаться РАСКРЫТОГО списка легаси-редактора.

    `models_loaded` ставится ДО монтажа строк (`_mount_list` ждёт сноса прежнего тела),
    и ↑/↓, попавшая в этот зазор, уходит в пустоту: список ещё свёрнут, `move` клавишу
    не берёт, а `submit()` возвращает None — тест зеленел бы на «ничего не изменено»."""
    from tui.editors.model import ModelEditor
    await wait_until(
        pilot,
        lambda: bool(scr.query(ModelEditor))
        and scr.query(ModelEditor).last().models_loaded
        and any(w.display for w in scr.query(".onb-model-row")),
        "список моделей раскрыт")


def _footer(scr) -> str:
    """Текст ВИДЖЕТА футера, а не пересчитанный `_footer_text()` — только так видно,
    что экран его действительно обновил (канон `test_footer_contract`)."""
    return str(scr.query_one("#set-footer", Static).render())


@asynccontextmanager
async def _section(tmp_path, body: str = "", *, models=("test-model",),
                   model: str = '"test-model"', size=None):
    """/settings → секция «Модели» на настоящем config.yml во временном каталоге.

    Список моделей легаси-редактора подменён на месте источника (`model_picker.
    fetch_models`) — инъекции у секции нет, как и у прежней «Модели».

    `size` — размер терминала для тестов ГЕОМЕТРИИ (свипы ширины): по умолчанию
    Textual даёт 80×24, и высоты не хватает, чтобы нижняя половина секции вообще
    попала в раскладку."""
    from unittest import mock

    import model_picker
    import tui.app as app_mod
    from model_picker import ModelInfo

    cfg_file = _write_cfg(tmp_path, body, model=model)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    with _isolated_config_layers(tmp_path), \
         mock.patch.object(model_picker, "fetch_models",
                           lambda *a, **k: [ModelInfo(id=i) for i in models]):
        async with app.run_test(**({"size": size} if size else {})) as pilot:
            scr = _mk_screen(config=cfg, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            yield pilot, scr, cfg_file


# --- состав секций и превью хаба ---------------------------------------------

def test_models_section_replaces_the_legacy_one_and_leaves_the_save_machine():
    """«Модели» встают на позицию «Модели»-легаси и ВЫХОДЯТ из save-машины:
    запись идёт немедленно по действию, а не через submit-цикл секции."""
    from tui.screens import settings as s
    assert s.SECTIONS[1] == "Модели"
    assert s._MODELS == 1
    assert s._EDITOR_SECTIONS == {s._CONNECTION}
    assert s._PROBE_SECTIONS == {s._CONNECTION}


async def test_hub_preview_counts_registry_entries_and_names_the_default(tmp_path):
    """Превью хаба непустого реестра — «N в реестре · default <алиас>»."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        preview = str(scr._row_value(1))
        assert "2 в реестре" in preview
        assert "default flash" in preview


async def test_hub_preview_does_not_count_the_reserve_model(tmp_path):
    """Резервная модель (`agent.model`) в счёт реестра НЕ входит (§12.4): реестр из
    одной записи и заполненный `agent.model` — это «1 в реестре», не 2."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "google/gemini-2.5-flash"\n')
    async with _section(tmp_path, body) as (_pilot, scr, _cfg_file):
        preview = str(scr._row_value(1))
        assert "1 в реестре" in preview
        assert "test-model" not in preview


async def test_hub_preview_in_legacy_mode_is_the_model_name(tmp_path):
    """Реестра нет — превью то же, что и сегодня: имя модели из файла."""
    async with _section(tmp_path) as (_pilot, scr, _cfg_file):
        assert "test-model" in str(scr._row_value(1))


async def test_hub_glyph_is_ok_when_the_registry_is_filled_and_valid(tmp_path):
    """Глиф секции — свой вердикт: ✓ = реестр непуст и валиден."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        glyph, _style = scr._menu_glyph(1)
        assert glyph == "✓"


async def test_hub_glyph_flags_a_broken_registry(tmp_path):
    """Реестр непуст, но нерезолвим (2 модели без единого слота) — честное «!»,
    а не ✓: секция показывает ФАЙЛОВОЕ состояние цели."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "google/gemini-2.5-flash"\n'
            "  opus:\n"
            '    model: "anthropic/claude-opus"\n')
    async with _section(tmp_path, body) as (_pilot, scr, _cfg_file):
        glyph, _style = scr._menu_glyph(1)
        assert glyph == "!"


async def test_probe_wave_does_not_animate_the_models_row(tmp_path):
    """Волна загрузки probe — только у секций, чей глиф ждёт пробы. «Модели» из
    probe-волны вышли: её вердикт про файл, а не про сеть."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        scr._probe_in_flight = True
        assert str(scr._menu_glyph_text(1)) != "···"


# --- пустое состояние: легаси-редактор целиком --------------------------------

async def test_empty_state_mounts_the_legacy_model_editor(tmp_path):
    """Реестра нет → секция = сегодняшний редактор модели плюс вход в реестр."""
    from tui.editors.model import ModelEditor
    async with _section(tmp_path) as (pilot, scr, _cfg_file):
        await _wait_legacy_list(pilot, scr)
        assert scr.query(ModelEditor)
        assert "в реестр" in _texts(scr)


async def test_escape_from_the_new_entry_form_returns_to_the_empty_state(tmp_path):
    """Esc из формы возвращает в СВОЁ под-состояние (пустое состояние секции), а не
    в меню — Esc-вложенность Global Constraints. Первый Esc достаётся пресет-листу
    адреса: форма открывается курсором на адресе, и список раскрыт (канон «вход на
    адрес — всегда раскрыть»)."""
    from tui.editors.model import ModelEditor
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path) as (pilot, scr, _cfg_file):
        await _wait_legacy_list(pilot, scr)
        await pilot.press("alt+n")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        assert form.on_url_field                  # курсор встал на адрес (новый порядок)
        if form.presets_open:
            await pilot.press("escape")           # №1 — свернуть раскрытый список
            await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == 1                     # из секции не вышли
        assert not scr.query(ModelEntryForm)
        assert scr.query(ModelEditor)


# --- создание записи ----------------------------------------------------------

async def test_creating_the_first_entry_writes_valid_yaml(tmp_path):
    """`n` → форма → Enter: запись уходит в базовый config.yml немедленно, файл
    остаётся валидным конфигом."""
    from config import validate_config_text_with_sections
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path) as (pilot, scr, cfg_file):
        await pilot.press("alt+n")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        form._presets_ctrl.close()     # раскрытый на входе пресет-лист съел бы Enter
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        doc = _doc(cfg_file)
        assert doc["models"]["flash"]["model"] == "google/gemini-2.5-flash"
        validate_config_text_with_sections(cfg_file.read_text(encoding="utf-8"))
        assert not scr.query(ModelEntryForm)      # форма закрылась
        assert any("flash" in r for r in _rows(scr))


async def test_second_entry_is_written_together_with_the_default_slot(tmp_path):
    """Вторая модель делает реестр неоднозначным (2 записи без слотов нерезолвимы) —
    форма спрашивает «какая по умолчанию?» инлайн-пикером, и запись уходит ОДНОЙ
    транзакцией вместе с `slots.default` (иначе валидатор отвергает её, и правильно)."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "google/gemini-2.5-flash"\n')
    async with _section(tmp_path, body) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        await pilot.press("n")
        await pilot.pause()
        from tui.editors.model_entry_form import ModelEntryForm
        form = scr.query(ModelEntryForm).last()
        form.query_one("#mef-alias", Input).value = "opus"
        form.query_one("#mef-model", Input).value = "anthropic/claude-opus"
        form._presets_ctrl.close()     # раскрытый на входе пресет-лист съел бы Enter
        await pilot.pause()
        await pilot.press("enter")                # submit → пикер default
        await pilot.pause()
        assert _editor(scr).picker_open, "пикер «какая модель по умолчанию» не открылся"
        assert cfg_file.read_bytes() == before    # до выбора файл не тронут
        await pilot.press("enter")                # выбрать первую строку пикера
        await pilot.pause()
        doc = _doc(cfg_file)
        assert set(doc["models"]) == {"flash", "opus"}
        assert doc["slots"]["default"] in ("flash", "opus")


async def test_the_add_row_opens_the_creation_form(tmp_path):
    """«+ добавить модель» — последняя строка блока реестра: Enter на ней = клавиша
    n, форма создания (без прежнего имени — это не правка)."""
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        idx = _goto(ed, "add")
        assert idx == len(ed._aliases)              # сразу за записями, до слотов
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        assert form.original_alias is None


async def test_renaming_an_entry_moves_its_references(tmp_path):
    """Переименование — правка той же формы, ОДНА транзакция: запись переезжает под
    новое имя с полным содержимым, ссылки slots/assign едут следом, старой записи не
    остаётся, файл валиден."""
    from config import validate_config_text_with_sections
    from tui.editors.model_entry_form import ModelEntryForm
    body = (_REGISTRY
            + "assign:\n"
            '  summary: "flash"\n')
    async with _section(tmp_path, body) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "entry", alias="flash")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        assert form.original_alias == "flash"
        form.query_one("#mef-alias", Input).value = "turbo"
        form._presets_ctrl.close()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        doc = _doc(cfg_file)
        assert set(doc["models"]) == {"turbo", "opus"}
        assert doc["models"]["turbo"]["model"] == "google/gemini-2.5-flash"
        assert doc["slots"]["default"] == "turbo"   # ссылка слота переехала
        assert doc["assign"]["summary"] == "turbo"  # и ссылка привязки тоже
        validate_config_text_with_sections(cfg_file.read_text(encoding="utf-8"))
        assert not scr.query(ModelEntryForm)


async def test_renaming_to_a_taken_alias_leaves_the_file_untouched(tmp_path):
    """Занятое имя — ошибка В форме: транзакция не стартует, файл байт-в-байт."""
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        ed = _editor(scr)
        _goto(ed, "entry", alias="flash")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        form.query_one("#mef-alias", Input).value = "opus"
        form._presets_ctrl.close()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "уже используется" in _texts(scr)
        assert cfg_file.read_bytes() == before
        assert scr.query(ModelEntryForm)            # форма жива, черновик не потерян


async def test_entry_without_any_address_is_shown_as_problematic(tmp_path):
    """Ни у записи, ни у основного конфига нет адреса — запись нерабочая. Молча
    создать её нельзя: строка гаснет, секция честно называет причину, и глиф хаба с
    секцией не спорит — реестр валиден по схеме, но не работает: «—», не «✓»."""
    from tui.palette import DEEP_DIM
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('agent:\n  model: "test-model"\n'
                        "models:\n  flash:\n    model: \"m\"\n", encoding="utf-8")
    import tui.app as app_mod
    app = app_mod.TuiApp(config={"model": "test-model"})
    with _isolated_config_layers(tmp_path):
        async with app.run_test() as pilot:
            scr = _mk_screen(config={"model": "test-model"}, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            assert scr._menu_glyph(1) == ("—", DEEP_DIM)
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            assert "адрес" in _texts(scr)


async def test_hub_glyph_stays_quiet_about_a_foreign_config_error(tmp_path):
    """`!` на строке «Модели» — только за реестр. Опечатка в ПОСТОРОННЕЙ секции валит
    ту же валидацию файла целиком, и красить ею эту строку значит обвинять реестр в
    чужой ошибке: вердикта нет — «—»."""
    from tui.palette import DEEP_DIM
    body = ("models:\n"
            "  flash:\n"
            '    model: "google/gemini-2.5-flash"\n'
            "max_steps: не-число\n")
    async with _section(tmp_path, body) as (_pilot, scr, _cfg_file):
        assert scr._menu_glyph(1) == ("—", DEEP_DIM)


# --- удаление -----------------------------------------------------------------

async def test_delete_asks_first_and_warns_about_references(tmp_path):
    """Del — confirm-баннер; ссылки из слотов/привязок названы В нём (отчёт
    `RemovalReport`), а файл до подтверждения не тронут."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        await pilot.press("delete")
        await pilot.pause()
        text = _texts(scr)
        assert "slots.default" in text
        assert cfg_file.read_bytes() == before
        await pilot.press("enter")
        await pilot.pause()
        doc = _doc(cfg_file)
        assert "flash" not in (doc.get("models") or {})
        assert "default" not in (doc.get("slots") or {})   # ссылка снята вместе с записью


async def test_deleting_the_last_entry_warns_that_levels_switch_off(tmp_path):
    """Удаление последней записи опустошает реестр — уровни выключатся; баннер
    обязан это сказать до нажатия Enter."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "google/gemini-2.5-flash"\n')
    async with _section(tmp_path, body) as (pilot, scr, _cfg_file):
        await pilot.press("delete")
        await pilot.pause()
        assert "Реестр станет пустым — уровни выключатся" in _texts(scr)


# --- резервная модель ---------------------------------------------------------

async def test_new_entry_key_does_not_drop_an_unsaved_reserve_choice(tmp_path):
    """`Alt+N` в под-форме резервной — no-op: там лежит несохранённый выбор, а футер
    этой клавиши не обещает. Обещание и поведение обязаны совпадать."""
    from tui.editors.model import ModelEditor
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path, _REGISTRY, models=("test-model", "other-model")) \
            as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "reserve")
        await pilot.pause()
        await pilot.press("enter")                  # открыть под-форму
        await _wait_legacy_list(pilot, scr)
        assert "Alt+N" not in scr._footer_text()
        await pilot.press("alt+n")
        await pilot.pause()
        assert not scr.query(ModelEntryForm)        # форма не открылась
        assert scr.query(ModelEditor)               # выбор резервной на месте


async def test_reserve_save_returns_to_the_registry_list(tmp_path):
    """Под-форма резервной открыта ИЗ списка — сохранение возвращает туда же (Esc
    поднимает на уровень, Enter не имеет права поднимать на два). Пустое состояние —
    это сама секция, и запись из неё по-прежнему уводит в хаб."""
    from tui.editors.model import ModelEditor
    async with _section(tmp_path, _REGISTRY, models=("test-model", "other-model")) \
            as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "reserve")
        await pilot.pause()
        await pilot.press("enter")
        await _wait_legacy_list(pilot, scr)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert "other-model" in cfg_file.read_text(encoding="utf-8")
        assert scr._view == 1                       # остались в секции
        assert any("flash" in r for r in _rows(scr))   # и видим список записей


async def test_empty_state_save_returns_to_the_hub(tmp_path):
    """Прежнее поведение легаси-секции сохранено: запись из пустого состояния
    завершает работу с секцией (возврат в хаб + флэш)."""
    from tui.editors.model import ModelEditor
    async with _section(tmp_path, models=("test-model", "other-model")) as (pilot, scr, cfg_file):
        await _wait_legacy_list(pilot, scr)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert "other-model" in cfg_file.read_text(encoding="utf-8")
        assert scr._view == "menu"


async def test_env_ref_confirm_is_answerable_from_the_models_section(tmp_path):
    """Плоская запись `agent.model` поверх ссылки ${ENV:VAR} поднимает подтверждение
    перезаписи. Секция вышла из save-машины, но подтверждение осталось её — иначе
    баннер висел бы неотвечаемым: Enter не подтверждал бы, Esc уводил бы в меню."""
    import os
    from tui.editors.model import ModelEditor
    os.environ["GC_TEST_MODEL_REF"] = "test-model"
    try:
        async with _section(tmp_path, models=("test-model", "other-model"),
                            model='"${ENV:GC_TEST_MODEL_REF}"') as (pilot, scr, cfg_file):
            await _wait_legacy_list(pilot, scr)
            # строки: ссылка (действующая) → test-model → other-model
            await pilot.press("down")
            await pilot.press("down")
            await pilot.press("enter")              # submit → подтверждение перезаписи ссылки
            await pilot.pause()
            assert "открытым текстом" in _texts(scr)
            assert "${ENV:GC_TEST_MODEL_REF}" in cfg_file.read_text(encoding="utf-8")
            await pilot.press("enter")              # подтвердить
            await pilot.pause()
            await pilot.pause()
            content = cfg_file.read_text(encoding="utf-8")
            assert "other-model" in content
            assert "${ENV:GC_TEST_MODEL_REF}" not in content
    finally:
        os.environ.pop("GC_TEST_MODEL_REF", None)


async def test_arrow_after_env_confirm_cancels_it(tmp_path):
    """Стрелка после поднятого подтверждения СНИМАЕТ его: `_pending_env_save` держит
    снимок значений на момент Enter, а курсор уже уехал на другую модель. Иначе
    следующий Enter молча запишет НЕ ТО, что показано, и затрёт ссылку на переменную
    окружения литералом чужой модели (паритет с «Подключением», `_cancel_env_confirm`
    на уходе с поля; в легаси-«Модели» это делал `_reset_draft_state`)."""
    import os
    os.environ["GC_TEST_MODEL_REF"] = "test-model"
    try:
        async with _section(tmp_path, models=("test-model", "other-model"),
                            model='"${ENV:GC_TEST_MODEL_REF}"') as (pilot, scr, cfg_file):
            await _wait_legacy_list(pilot, scr)
            await pilot.press("down")               # ссылка → test-model
            await pilot.press("enter")              # submit → подтверждение перезаписи
            await pilot.pause()
            assert scr._pending_env_save is not None
            await pilot.press("down")               # курсор уехал → снимок больше не про то
            await pilot.pause()
            assert scr._pending_env_save is None, "снимок пережил уход курсора"
            assert "открытым текстом" not in _texts(scr), "баннер остался висеть"
            assert "${ENV:GC_TEST_MODEL_REF}" in cfg_file.read_text(encoding="utf-8")
    finally:
        os.environ.pop("GC_TEST_MODEL_REF", None)


async def test_new_entry_after_env_confirm_cancels_it(tmp_path):
    """`Alt+N` поверх поднятого подтверждения — тот же «ухожу отсюда», что и стрелка.

    Иначе Enter в открывшейся форме перехватывается гейтом `_pending_env_save` (он
    стоит ВЫШЕ блока секции): вместо записи в реестр уходит плоский `_do_save`, ссылка
    ${ENV:VAR} затирается, черновик формы гибнет, а человека выбрасывает в хаб — и
    записи реестра не появляется вовсе."""
    import os
    from tui.editors.model_entry_form import ModelEntryForm
    os.environ["GC_TEST_MODEL_REF"] = "test-model"
    try:
        async with _section(tmp_path, models=("test-model", "other-model"),
                            model='"${ENV:GC_TEST_MODEL_REF}"') as (pilot, scr, cfg_file):
            await _wait_legacy_list(pilot, scr)
            await pilot.press("down")
            await pilot.press("enter")              # submit → подтверждение перезаписи
            await pilot.pause()
            assert scr._pending_env_save is not None
            await pilot.press("alt+n")              # уходим в форму записи реестра
            await pilot.pause()
            assert scr.query(ModelEntryForm), "форма не открылась"
            assert scr._pending_env_save is None, "снимок пережил уход в форму"
            assert "открытым текстом" not in _texts(scr), "баннер остался висеть"
            form = scr.query(ModelEntryForm).last()
            form.query_one("#mef-alias", Input).value = "flash"
            form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
            form._presets_ctrl.close()  # раскрытый на входе пресет-лист съел бы Enter
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            content = cfg_file.read_text(encoding="utf-8")
            assert _doc(cfg_file)["models"]["flash"]["model"] == "google/gemini-2.5-flash"
            assert "${ENV:GC_TEST_MODEL_REF}" in content, "ссылка затёрта чужой записью"
            assert scr._view == 1, "выбросило из секции"
    finally:
        os.environ.pop("GC_TEST_MODEL_REF", None)


async def test_list_reload_after_env_confirm_cancels_it(tmp_path):
    """Третья дверь в то же состояние: `Alt+↓` перечитывает список, и выбор при этом
    НЕ гарантированно остаётся на месте — модель, пропавшая из свежего ответа сервера,
    роняет курсор на якорь (`ModelEditor._reselect_idx`). Снимок подтверждения тогда
    снова не про то, что показано, а Enter пишет прежнее значение поверх ссылки."""
    import os
    os.environ["GC_TEST_MODEL_REF"] = "test-model"
    try:
        async with _section(tmp_path, models=("test-model", "other-model"),
                            model='"${ENV:GC_TEST_MODEL_REF}"') as (pilot, scr, cfg_file):
            await _wait_legacy_list(pilot, scr)
            await pilot.press("down")
            await pilot.press("enter")              # submit → подтверждение перезаписи
            await pilot.pause()
            assert scr._pending_env_save is not None
            await pilot.press("alt+down")           # перечитать список
            await pilot.pause()
            assert scr._pending_env_save is None, "снимок пережил перечитывание списка"
            assert "${ENV:GC_TEST_MODEL_REF}" in cfg_file.read_text(encoding="utf-8")
    finally:
        os.environ.pop("GC_TEST_MODEL_REF", None)


async def test_reserve_row_edits_agent_model_through_its_own_subform(tmp_path):
    """Строка «резервная модель» открывает под-форму `ModelEditor` и правит
    `agent.model`; хинт — «действует только без реестра», НЕ «база наследования»."""
    from tui.editors.model import ModelEditor
    async with _section(tmp_path, _REGISTRY, models=("test-model", "other-model")) \
            as (pilot, scr, cfg_file):
        assert "действует только без реестра" in _texts(scr)
        assert "база для незаданного" not in _texts(scr)
        ed = _editor(scr)
        _goto(ed, "reserve")
        await pilot.pause()
        await pilot.press("enter")                  # открыть под-форму
        await _wait_legacy_list(pilot, scr)
        await pilot.press("down")                   # выбрать другую модель
        await pilot.press("enter")                  # сохранить
        await pilot.pause()
        await pilot.pause()
        assert "other-model" in cfg_file.read_text(encoding="utf-8")


# --- швы экрана ---------------------------------------------------------------

async def test_section_never_shows_the_dirty_marker(tmp_path):
    """Секция вышла из save-машины: «изменено» у заголовка — обещание submit-цикла,
    которого здесь нет (пишем немедленно)."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        scr._update_dirty()
        assert "изменено" not in str(scr.query_one("#set-title", Static).render())


async def test_read_only_config_refuses_the_section(tmp_path, monkeypatch):
    """Гейт записи — по ЦЕЛИ (`_target_writable`): запуск на пакетном шаблоне не
    показывает редактор вовсе."""
    from config import _default_config_path
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        monkeypatch.setattr("config._default_config_path", lambda: str(cfg_file))
        open_settings_section(scr, _SECTION)
        await pilot.pause()
        assert "недоступен для записи" in _texts(scr)
        assert scr._footer_text() == "PgUp/PgDn — секции  ·  Esc — меню"


async def test_legacy_editor_lookup_takes_the_fresh_one(tmp_path):
    """Сохранение легаси-поля обязано читать СВЕЖИЙ редактор: секция пересоздаёт его
    на каждую сборку тела, а `remove()` в Textual отложенный — обречённый ещё в
    дереве, и `query_one` молча отдал бы ЕГО: в конфиг уехало бы значение прошлого
    рендера (дословно находка `_connection_editor`, 2026-08-07).

    Живьём, на настоящей пересборке тела секции — том самом действии, которое волна B
    сделала частым (раньше редактор пересобирался только при входе в секцию)."""
    from tui.editors.model import ModelEditor
    async with _section(tmp_path) as (pilot, scr, _cfg_file):
        await _wait_legacy_list(pilot, scr)
        first = scr.query(ModelEditor).first()
        _editor(scr)._rebuild()                      # без pause: обречённый ещё в дереве
        # Предусловие названо явно: сменится тайминг Textual — страж честно упадёт
        # здесь, а не станет вакуумно-зелёным на дереве с одним редактором.
        assert len(scr.query(ModelEditor)) == 2, "обречённый редактор уже снесён"
        assert scr._legacy_model_editor() is not first


def test_legacy_editor_lookup_reports_an_empty_tree():
    """Редактора нет вовсе (read-only конфиг, первый кадр) — `NoMatches`, как у
    `_connection_editor`: вызывающие ловят его и трактуют как «спрашивать некого»."""
    from textual.css.query import NoMatches
    scr = _mk_screen()
    scr.query = lambda *_a, **_k: []
    with pytest.raises(NoMatches):
        scr._legacy_model_editor()


async def test_entry_row_hint_names_the_target_file(tmp_path):
    """Файл-цель секция называет один раз, статичной строкой над списком, и ПОЛНЫМ
    путём (ревизия владельца: одно имя не говорит, какой именно это конфиг)."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        assert f"запись: {cfg_file}" in _texts(scr)
        ed = _editor(scr)
        _goto(ed, "reserve")
        await pilot.pause()
        assert f"запись: {cfg_file}" in _texts(scr)


def test_levels_come_from_the_domain_not_a_local_copy():
    """Список уровней — доменная константа `model_registry.LEVELS`: своя копия в UI
    разошлась бы с доменом на первой же правке (сравнение по identity)."""
    from config_modules.model_registry import LEVELS
    from tui.editors import models_section
    assert models_section.LEVELS is LEVELS


def test_target_writable_refuses_an_empty_path():
    """Паритет с `_writable`: пустой путь конфига — не цель записи (дефект волны A)."""
    scr = _mk_screen(config_path=None)
    assert scr._target_writable(None) is False
    assert scr._target_writable("") is False


# --- футер --------------------------------------------------------------------

async def test_footer_is_positional_over_the_section_rows(tmp_path):
    """Enter значит разное на строке записи и на строке резервной модели — футер
    обязан называть то, что работает здесь и сейчас."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        assert "Del — удалить" in scr._footer_text()
        ed = _editor(scr)
        _goto(ed, "reserve")
        await pilot.pause()
        footer = scr._footer_text()
        assert "Del — удалить" not in footer        # резервную запись удалять нечем
        assert "Enter — изменить" in footer


async def test_form_footer_follows_the_filter_of_the_address_list(tmp_path):
    """Печать в адресе сужает пресет-лист — вместе с ним меняется и обещание Enter
    (подставить ↔ оставить своё). Футер обязан догонять её тем же событием, иначе
    он обещает подстановку там, где подставлять уже нечего."""
    from tui.editors.model_entry_form import ModelEntryForm
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        await pilot.press("n")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        await pilot.press("down")                   # алиас → провайдерский id
        await pilot.press("down")                   # id → адрес (пресет-лист раскрылся)
        await pilot.pause()
        assert form.presets_open, "пресет-лист адреса не раскрылся"
        assert "Enter — подставить" in _footer(scr)
        form.query_one("#mef-url", Input).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        assert "Enter — оставить своё" in _footer(scr)


def test_module_footers_and_hints_are_registered_in_the_contract_guard():
    """Global Constraints: новый модуль регистрируется в стражах футеров/хинтов той
    задачей, которая его создаёт — иначе подмена клавиши здесь пройдёт незамеченной."""
    from tests.tui.test_footer_contract import _all_footer_strings, _all_hint_strings
    names = {*_all_footer_strings(), *_all_hint_strings()}
    assert any(n.startswith("tui.editors.models_section.") for n in names)


# --- слоты и привязки (Task 4) ------------------------------------------------
#
# Нижняя половина секции: три строки слотов (уровень → алиас) и блок привязок
# потребителей (потребитель → уровень или алиас). Значение выбирается СВОИМ
# пикером-раскрытием (решение 9 плана): `FieldListController` намертво привязан к
# `Input`, `ArgPicker` — оверлей палитры чата, и под строку-Static не годится ни
# тот, ни другой.

_ASSIGNED = (
    "assign:\n"
    '  finish_gate: "light"\n'
    '  summary: "opus"\n'
)


def _goto(ed, kind: str, **match) -> int:
    """Курсор на строку по её РОЛИ, а не по магическому индексу: строк в секции
    больше двух десятков, и `move(9)` в тесте нечитаем и ломается от любой вставки."""
    for i, row in enumerate(ed._rows()):
        if row["kind"] == kind and all(row.get(k) == v for k, v in match.items()):
            ed._idx = i
            ed.move(0)          # тот же путь обновления строк, что и у живой стрелки
            return i
    raise AssertionError(f"строки {kind} {match} в секции нет")


def _picker_rows(ed) -> list:
    return [str(w.render()) for w in ed.query(".models-picker-row")]


def _value_color(text, value: str):
    """Цвет колонки значения строки — span, начинающийся ровно на значении (канон
    стража грамматики строки уровня в `test_settings_profiles_showcase.py`).

    Цвет нормализуется до RGB: у СМОНТИРОВАННОГО виджета `render()` отдаёт стиль,
    уже прошедший через консоль («rgb(122,130,144)»), а константа палитры — «#hex»;
    сравнение строк развело бы одинаковые цвета."""
    from rich.style import Style
    i = text.plain.index(value)
    span = next(s for s in text.spans if s.start == i)
    return Style.parse(str(span.style)).color.get_truecolor()


def _rgb(token: str):
    from rich.style import Style
    return Style.parse(token).color.get_truecolor()


async def test_slot_picker_opens_under_the_row_with_unset_first(tmp_path):
    """Enter на строке слота раскрывает пикер ПОД НЕЙ (решение 9): первый пункт —
    «не задан» (снятие значения — первая позиция списка, отдельной клавиши снятия
    нет), дальше алиасы реестра. До выбора файл не тронут."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        ed = _editor(scr)
        idx = _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert ed.picker_open, "пикер слота не раскрылся"
        assert [r["label"] for r in ed._picker["rows"]] == ["не задан", "flash", "opus"]
        assert ed._picker["rows"][0]["value"] is None      # снятие — по value, не тексту
        assert ed._picker["idx"] == 0                      # слот пуст → курсор на снятии
        assert "не задан" in _picker_rows(ed)[0]
        children = list(ed.children)
        row = list(ed.query(".models-row"))[idx]
        assert children.index(ed.query_one(".models-picker")) == children.index(row) + 1
        assert cfg_file.read_bytes() == before


async def test_assign_picker_offers_levels_and_aliases_after_active(tmp_path):
    """Привязка шире слота: значением может быть уровень (индирекция через slots)
    ИЛИ алиас (жёсткий пин). Первый пункт — «активная» (привязки нет)."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="finish_gate")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert [r["label"] for r in ed._picker["rows"]] == [
            "активная", "light", "default", "max", "flash", "opus"]


async def test_alt_down_opens_the_same_picker(tmp_path):
    """Канон клавиш: `Alt+↓` — раскрыть список. У строки слота список тот же, что и
    у Enter (решение 9 называет обе клавиши), а не вторая, отдельная механика."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "slot", level="max")
        await pilot.pause()
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.picker_open


async def test_escape_collapses_the_picker_without_writing(tmp_path):
    """Esc сворачивает пикер и остаётся В секции (Esc-вложенность Global
    Constraints): выбор не применяется, файл не трогается."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")                   # выбор уехал на алиас
        await pilot.press("escape")
        await pilot.pause()
        assert not ed.picker_open
        assert scr._view == 1                       # из секции не вышли
        assert cfg_file.read_bytes() == before


async def test_arrows_do_not_page_values_on_a_collapsed_row(tmp_path):
    """←/→ на свёрнутой строке НЕ листают значения: список произвольный (алиасы
    реестра), треком он не виден, и вслепую его не листают — та же ревизия §12.1,
    что перевела `/profiles` с цикла на пикер."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("right")
        await pilot.press("left")
        await pilot.pause()
        assert not ed.picker_open
        assert cfg_file.read_bytes() == before
        assert "не задан" in str(list(ed.query(".models-row"))[ed._idx].render())


async def test_choosing_an_alias_writes_the_slot(tmp_path):
    """Выбор алиаса пишется в `slots.<уровень>` немедленно, файл остаётся валидным."""
    from config import validate_config_text_with_sections
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("down")                   # «не задан» → flash → opus
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["slots"]["light"] == "opus"
        validate_config_text_with_sections(cfg_file.read_text(encoding="utf-8"))
        assert not _editor(scr).picker_open
        assert "opus" in str(list(_editor(scr).query(".models-row"))[ed._idx].render())


async def test_the_first_position_unsets_the_slot(tmp_path):
    """«не задан» — не значение, а снятие: строка слота уходит из файла целиком,
    уровень возвращается к фоллбэку матрицы. Пикер открывается курсором на ТЕКУЩЕМ
    значении (выбранное видно сразу) — к снятию курсор ведут стрелками."""
    body = (_REGISTRY + '  light: "opus"\n')
    async with _section(tmp_path, body) as (pilot, scr, cfg_file):
        assert _doc(cfg_file)["slots"]["light"] == "opus"
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("enter")
        assert ed._picker["idx"] == 2               # курсор на текущем значении (opus)
        await pilot.press("up")
        await pilot.press("up")                     # opus → flash → «не задан»
        await pilot.press("enter")
        await pilot.pause()
        assert "light" not in _doc(cfg_file)["slots"]
        assert _doc(cfg_file)["slots"]["default"] == "flash"   # соседний слот цел


async def test_assign_binding_writes_the_consumer_key(tmp_path):
    """Привязка служебного потребителя пишется в `assign.<потребитель>`."""
    from config import validate_config_text_with_sections
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="finish_gate")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")                   # «активная» → light
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["assign"]["finish_gate"] == "light"
        validate_config_text_with_sections(cfg_file.read_text(encoding="utf-8"))


async def test_active_removes_the_assign_key(tmp_path):
    """«активная» = привязки нет: ключ уходит из файла, потребитель возвращается к
    активной модели сессии."""
    async with _section(tmp_path, _REGISTRY + _ASSIGNED) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="finish_gate")
        await pilot.pause()
        await pilot.press("enter")
        assert ed._picker["idx"] == 1               # курсор на текущем значении (light)
        await pilot.press("up")                     # light → «активная»
        await pilot.press("enter")                  # первая позиция — снять
        await pilot.pause()
        assign = _doc(cfg_file).get("assign") or {}
        assert "finish_gate" not in assign
        assert assign.get("summary") == "opus"      # соседняя привязка цела


async def test_wildcard_row_writes_the_subagents_star_key(tmp_path):
    """Wildcard «остальные субагенты» — не выдуманная строка UI, а реальный ключ
    `assign.subagents."*"` (точный id → wildcard → активная модель)."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="*")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("down")                   # «активная» → light → default
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["assign"]["subagents"]["*"] == "default"


async def test_named_subagent_row_writes_the_nested_key(tmp_path):
    """Поимённая привязка субагента — вложенный ключ `assign.subagents.<имя>`."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="subagents.subagent_critic")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")
        await pilot.press("down")                   # «активная» → light/default/max → flash
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["assign"]["subagents"]["subagent_critic"] == "flash"


def test_subagent_rows_come_from_the_code_registry():
    """Список субагентов в секции — канонический реестр кода, а не копия в UI:
    новый субагент появляется строкой сам (тот же страж, что у `demo_profile.yml`)."""
    from config_modules.model_sections_validation import SUBAGENT_NAMES
    from tools.subagents.registry import SUBAGENT_CLASSES
    from tui.editors import models_section
    consumers = models_section.assign_consumers()
    assert set(SUBAGENT_NAMES) == {cls.agent_name for cls in SUBAGENT_CLASSES}
    assert [c for c in consumers if c.startswith("subagents.")] == \
        [f"subagents.{name}" for name in SUBAGENT_NAMES]
    assert consumers[-1] == "*", "wildcard — последней строкой блока привязок"


def test_consumer_labels_reuse_the_transport_dictionary():
    """6 готовых подписей берутся ИЗ транспортного словаря, а не копируются в UI
    (копия разошлась бы с ним на первой же правке); свой UI-словарь — только для
    остальных: `auto_judge`, субагенты поимённо, wildcard."""
    from utils.llm_chat_client import _CONSUMER_RU_LABEL
    from tui.editors import models_section
    labels = models_section.consumer_labels()
    for consumer, label in _CONSUMER_RU_LABEL.items():
        assert labels[consumer] == label
    assert not set(models_section._UI_CONSUMER_LABEL) & set(_CONSUMER_RU_LABEL)
    assert set(labels) == set(models_section.assign_consumers())


async def test_auto_judge_row_is_present_and_marked_inert(tmp_path):
    """`auto_judge` в реестре потребителей есть (строка на месте), но до волны C
    привязка инертна — это говорит ГОЛОВА строки («имя · описание», построчной
    подсказки у секции больше нет), а не молчит.

    Что Router его не зовёт и без привязки идёт уровнем default, а не активной
    моделью, — страж волны A (`tests/router/test_model_router.py::
    test_auto_judge_without_binding_uses_default_statically_not_active_level`);
    здесь не дублируется."""
    from tui.editors.models_section import consumer_labels
    assert consumer_labels()["auto_judge"] == "судья auto_judge · появится с авто-выбором"
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="auto_judge")
        await pilot.pause()
        assert "появится с авто-выбором" in _texts(scr)


async def test_unset_slot_hint_names_the_real_fallback(tmp_path):
    """Пустой слот называет честный фоллбэк по матрице `LEVEL_FALLBACK`, а не
    выдуманный текст: light не задан → работает default. Живёт это в САМОЙ строке
    (колонка значения; построчной подсказки больше нет), а развёрнутая форма с
    алиасом — в описании первой позиции пикера."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        assert "не задан → default" in _texts(scr)
        await pilot.press("enter")
        await pilot.pause()
        assert "действует default → flash" in ed._picker["rows"][0]["desc"].plain


async def test_invalid_combination_leaves_the_file_untouched(tmp_path):
    """Снятие единственного слота при двух моделях делает конфиг нерезолвимым —
    валидация результата отвергает запись: файл байт-в-байт прежний, отказ честный."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, cfg_file):
        before = cfg_file.read_bytes()
        ed = _editor(scr)
        _goto(ed, "slot", level="default")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.press("up")                     # с текущего (flash) на «не задан»
        await pilot.press("enter")                  # снять последний слот
        await pilot.pause()
        assert cfg_file.read_bytes() == before
        assert "не сохранено" in _texts(scr)


async def test_alias_named_like_a_sentinel_is_pinned_not_dropped(tmp_path):
    """Снятие значения опознаётся ПОЗИЦИЕЙ, а не текстом: алиас «не задан» /
    «активная» полностью легален (`validate_model_alias` режет только
    `RESERVED_WORDS`), и выбор такой модели обязан её запинить, а не молча снять
    слот/привязку. Цвет строки — по тому же признаку: пин есть, значит DIM."""
    from tui.palette import DIM
    body = ("models:\n"
            '  "не задан":\n'
            '    model: "google/gemini-2.5-flash"\n'
            '  "активная":\n'
            '    model: "anthropic/claude-opus"\n'
            "slots:\n"
            '  default: "не задан"\n')
    async with _section(tmp_path, body) as (pilot, scr, cfg_file):
        ed = _editor(scr)
        assert _value_color(ed._row_text({"kind": "slot", "level": "default"}, False),
                            "не задан") == _rgb(DIM), "пин покрашен как пустое значение"
        _goto(ed, "slot", level="light")
        await pilot.pause()
        await pilot.press("enter")
        assert [r["label"] for r in ed._picker["rows"]] == [
            "не задан", "не задан", "активная"]
        await pilot.press("down")                   # сентинел → одноимённый алиас
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["slots"]["light"] == "не задан"

        _goto(_editor(scr), "assign", consumer="finish_gate")
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(5):                          # сентинел → light/default/max → алиасы
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(cfg_file)["assign"]["finish_gate"] == "активная"


async def test_binding_row_values_carry_the_color_semantics(tmp_path):
    """§12.4 дословно: значение-уровень — цветом уровня (light=INK, default=RUN_COLOR,
    max=ACCENT), значение-алиас (пин) — DIM, «активная»/«не задан» — DEEP_DIM."""
    from tui.palette import ACCENT, DEEP_DIM, DIM, INK, RUN_COLOR
    body = (_REGISTRY + "assign:\n"
            '  finish_gate: "light"\n'
            '  summary: "opus"\n'
            '  text_answer_judge: "default"\n'
            '  command_judge: "max"\n')
    async with _section(tmp_path, body) as (_pilot, scr, _cfg_file):
        ed = _editor(scr)

        def slot(level):
            return ed._row_text({"kind": "slot", "level": level}, False)

        def binding(consumer):
            return ed._row_text({"kind": "assign", "consumer": consumer}, False)

        assert _value_color(slot("default"), "flash") == _rgb(DIM)         # алиас-пин
        assert _value_color(slot("max"), "не задан") == _rgb(DEEP_DIM)
        assert _value_color(binding("finish_gate"), "light") == _rgb(INK)  # уровень light
        assert _value_color(binding("text_answer_judge"), "default") == _rgb(RUN_COLOR)
        assert _value_color(binding("command_judge"), "max") == _rgb(ACCENT)
        assert _value_color(binding("summary"), "opus") == _rgb(DIM)
        assert _value_color(binding("intent_scope"), "активная") == _rgb(DEEP_DIM)


async def test_picker_rows_carry_the_same_color_semantics(tmp_path):
    """Раскрытый список — тот же визуальный язык, что и колонка значения строки
    (контракт п.3: канон-рендер дропдаунов, семантика значений §12.4)."""
    from tui.palette import ACCENT, DEEP_DIM, DIM, INK
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="finish_gate")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        widgets = list(ed.query(".models-picker-row"))
        texts = [w.render() for w in widgets]
        assert _value_color(texts[0], "активная") == _rgb(DEEP_DIM)
        assert _value_color(texts[1], "light") == _rgb(INK)
        assert _value_color(texts[3], "max") == _rgb(ACCENT)
        assert _value_color(texts[4], "flash") == _rgb(DIM)


async def test_slot_row_head_is_the_canonical_level_copy(tmp_path):
    """Голова строки слота — канон §1 дословно (тот же источник, что хинты `/level`):
    «default · основная рабочая: повседневные задачи»."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        ed = _editor(scr)
        head = ed._row_text({"kind": "slot", "level": "default"}, False).plain
        assert "default · основная рабочая: повседневные задачи" in head


async def test_footer_is_positional_on_slot_and_assign_rows(tmp_path):
    """Enter на строке слота/привязки открывает список значений — футер обязан
    обещать именно это, и не обещать удаление (удалять на этих строках нечего)."""
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "slot", level="light")
        await pilot.pause()
        footer = scr._footer_text()
        assert "Enter — выбрать модель" in footer
        assert "Del — удалить" not in footer
        _goto(ed, "assign", consumer="summary")
        await pilot.pause()
        assert "Enter — выбрать уровень или модель" in scr._footer_text()
        await pilot.press("enter")
        await pilot.pause()
        assert "Enter — выбрать" in scr._footer_text()   # раскрытый список — свой футер
        assert "Esc — отмена" in scr._footer_text()


# --- вид секции: подписи блоков, столбцы записи, приглушённая резервная -------
#
# Пять правок вида, согласованных с владельцем на живом смоке (Task 12): честная
# подпись wildcard-строки, приглушённая резервная модель, подписи блоков в чертах
# вместо дублирующего заголовка рамки, цвет уровня в голове строки слота и два
# новых столбца строки реестра — адрес и указатель уровней.


def _spans_style(text, *, skip_cursor: bool = True) -> set:
    """Цвета ВСЕХ содержательных спанов строки (курсор — не содержимое): «строка
    целиком приглушена» — утверждение про все спаны сразу, а не про один найденный."""
    from rich.style import Style
    return {Style.parse(str(s.style)).color.get_truecolor()
            for s in text.spans if not (skip_cursor and s.start == 0)}


_ADDRESSED = (
    "models:\n"
    "  flash:\n"
    '    model: "qwen/qwen-flash"\n'
    '    url: "https://openrouter.ai/api/v1/chat/completions"\n'
    "  opus:\n"
    '    model: "anthropic/claude-opus"\n'
    "slots:\n"
    '  light: "flash"\n'
    '  default: "opus"\n'
    '  max: "flash"\n'
)


async def test_wildcard_row_names_the_subagents_it_really_covers(tmp_path):
    """`*` — не «остальные из списка» (в списке перечислены ВСЕ канонические): это
    единственный способ привязать кастомных субагентов, которых валидатор поимённо
    отвергает, и общая привязка для тех, кому своя не задана."""
    from tui.editors.models_section import consumer_labels
    assert consumer_labels()["*"] == "* · кастомные и будущие субагенты"
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        _goto(ed, "assign", consumer="*")
        await pilot.pause()
        assert "кастомные и будущие субагенты" in " ".join(_rows(scr))
        assert "остальные субагенты" not in " ".join(_rows(scr))


async def test_reserve_row_is_dimmed_while_the_registry_works(tmp_path):
    """Резервная модель при живом реестре не работает — и выглядит так же, как
    прочие нерабочие записи (DEEP_DIM целиком), а не равноправной строкой."""
    from tui.palette import DEEP_DIM
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        text = _editor(scr)._row_text({"kind": "reserve"}, False)
        assert "test-model" in text.plain
        assert "действует только без реестра" in text.plain
        assert _spans_style(text) == {_rgb(DEEP_DIM)}


async def test_block_captions_stand_in_the_separators_instead_of_the_frame_title(tmp_path):
    """Заголовок «Модели» внутри рамки дублировал шапку экрана — вместо него блоки
    подписаны сами: стрелка сворачивания + подпись, вписанные в черту. Все черты
    ОДНОЙ ширины (по самой длинной подписи — ревизия владельца: рваный правый край
    читался как неровность)."""
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        title = scr.query_one("#set-title", Static)
        assert str(title.render()) == ""
        assert title.display is False
        captions = [str(w.render()) for w in scr.query(".models-block")]
        assert captions == [
            "▾ реестр ──────────────────────────",
            "▾ уровни ──────────────────────────",
            "▾ назначения: кто чем работает ────"]
        assert len({len(c) for c in captions}) == 1     # единый правый край
        ed = _editor(scr)
        assert "запись:" in str(list(ed.children)[0].render())
        assert "реестр" in str(list(ed.children)[1].render())


async def test_clicking_a_block_header_collapses_and_expands(tmp_path):
    """Шапка блока — его выключатель, и только мышью (решение владельца; безопасно
    ровно потому, что дефолт «всё раскрыто» — без мыши ничего не теряется). Клик:
    строки блока уходят из тела и из пути курсора, шапка несёт ▸ и превью
    содержимого; второй клик возвращает всё."""
    from tui.editors.models_section import _BlockHeader
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        rows_before = len(ed._rows())
        await pilot.click(_BlockHeader)             # первая шапка — «реестр»
        await pilot.pause()
        assert ed._collapsed == {"entry"}
        assert all(r["kind"] not in ("entry", "add") for r in ed._rows())
        assert len(ed._rows()) == rows_before - len(ed._aliases) - 1
        head = str(list(ed.query(".models-block"))[0].render())
        assert head.startswith("▸") and "2 модели" in head
        assert "─" not in head                      # у свёрнутой шапки черты нет
        await pilot.click(_BlockHeader)
        await pilot.pause()
        assert ed._collapsed == set()
        assert len(ed._rows()) == rows_before
        head = str(list(ed.query(".models-block"))[0].render())
        assert head.startswith("▾") and "модели" not in head
        assert head.endswith("─")                   # раскрытая — вписана в черту


async def test_slot_row_head_carries_the_colour_of_its_level(tmp_path):
    """Одна сущность — один вид: уровень в голове строки красится тем же цветом, что
    и в раскрытом списке (§12.4), роль после «·» остаётся прежней."""
    from tui.palette import ACCENT, INK, RUN_COLOR
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        ed = _editor(scr)

        def head(level):
            return ed._row_text({"kind": "slot", "level": level}, False)

        assert _value_color(head("light"), "light") == _rgb(INK)
        assert _value_color(head("default"), "default") == _rgb(RUN_COLOR)
        assert _value_color(head("max"), "max") == _rgb(ACCENT)
        assert _value_color(head("max"), " · самая сильная") == _rgb(INK)


async def test_entry_row_shows_the_address_and_every_level_that_points_at_it(tmp_path):
    """Строка записи отвечает на два вопроса разом: КУДА уйдёт запрос (хост) и КЕМ
    она работает (все уровни, а не один default). Указатель — крайний правый столбец
    и стоит у всех записей в одной колонке."""
    from tui.palette import ACCENT, INK, RUN_COLOR
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        # Ширину строка берёт у живого виджета; здесь проверяется ГРАММАТИКА, а не
        # геометрия окна — задаём её явно, чтобы колонки не спорили с шириной
        # терминала, на котором гоняются TUI-тесты (80 колонок).
        ed._row_width = lambda: 100
        flash = ed._row_text({"kind": "entry", "alias": "flash"}, False)
        opus = ed._row_text({"kind": "entry", "alias": "opus"}, False)
        assert "openrouter.ai" in flash.plain
        assert "/api/v1" not in flash.plain          # в колонке — хост, не путь
        assert flash.plain.rstrip().endswith("← light, max")
        assert opus.plain.rstrip().endswith("← default")
        assert _value_color(flash, "light") == _rgb(INK)
        assert _value_color(flash, "max") == _rgb(ACCENT)
        assert _value_color(opus, "default") == _rgb(RUN_COLOR)
        assert flash.plain.index("←") == opus.plain.index("←")


async def test_inherited_address_says_where_it_came_from(tmp_path):
    """Запись без своего `url` работает адресом основного конфига — и говорит об этом
    в хвосте ячейки (усечётся он первым, хост уцелеет)."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        cell = ed._address_cell("opus")
        assert cell.startswith("host")
        assert cell.endswith("· из основного конфига")


async def test_the_address_column_shrinks_but_the_pointer_stays_whole(tmp_path):
    """Узкая рамка жертвует АДРЕСОМ, а не указателем: сперва колонка адреса ужимается
    многоточием, потом схлопывается вместе со своим зазором — и на каждой из ширин
    строка помещается в рамку целиком, а указатель цел.

    Ширины сняты с живого дефекта: на 56 и 60 колонках адресу не остаётся места
    вовсе, на 64 он ещё жив и усечён."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        for width in (56, 60, 64):
            ed._row_width = lambda w=width: w
            flash = ed._row_text({"kind": "entry", "alias": "flash"}, False)
            opus = ed._row_text({"kind": "entry", "alias": "opus"}, False)
            assert len(flash.plain) <= width, width
            assert len(opus.plain) <= width, width
            assert flash.plain.rstrip().endswith("← light, max"), width
            assert opus.plain.rstrip().endswith("← default"), width
        ed._row_width = lambda: 64
        opus = ed._row_text({"kind": "entry", "alias": "opus"}, False)
        assert "…" in opus.plain
        assert "host" in opus.plain                  # хост уцелел, отрезан хвост


async def test_the_row_never_outgrows_the_frame(tmp_path):
    """Строка записи не переносится НИ НА КАКОЙ ширине: перенос рвёт указатель и
    ставит его продолжение к левому краю без отступа. Порядок жертв — канон
    all-or-nothing `_mode_row_text`: адрес (с зазором) → указатель → ядро."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "vendor/some-extremely-long-model-identifier-2026-08-preview"\n'
            "slots:\n"
            '  light: "flash"\n'
            '  default: "flash"\n'
            '  max: "flash"\n')
    async with _section(tmp_path, body) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        for width in range(12, 140):
            ed._row_width = lambda w=width: w
            row = ed._row_text({"kind": "entry", "alias": "flash"}, False)
            assert len(row.plain) <= width, (width, row.plain)
            assert row.plain.strip(), width          # ядро строки видимо всегда


async def test_a_long_model_id_is_cut_not_wrapped(tmp_path):
    """Ядро строки (алиас + провайдерский id) тоже имеет предел: id в 62 символа
    раздувал строку до 110 колонок и переносил её даже на широком окне."""
    body = ("models:\n"
            "  flash:\n"
            '    model: "vendor/some-extremely-long-model-identifier-2026-08-preview"\n')
    async with _section(tmp_path, body) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 60
        row = ed._row_text({"kind": "entry", "alias": "flash"}, False)
        assert len(row.plain) <= 60
        assert "flash" in row.plain                  # алиас цел
        assert row.plain.rstrip().endswith("…")      # срезан id, а не строка


async def test_the_address_column_collapses_instead_of_a_two_letter_stub(tmp_path):
    """Ступень `_MIN_ADDRESS_COLS`: колонка, в которой осталось три места, показывает
    не адрес, а огрызок «ho…» — такую колонку честнее убрать целиком и отдать место
    остальным. Ширина 60 подобрана так, чтобы адресу оставалось РОВНО 3 колонки."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 61
        assert ed._entry_columns()[2] == 4        # ступенью выше колонка ещё жива
        ed._row_width = lambda: 60
        assert ed._entry_columns()[2] == 0
        row = ed._row_text({"kind": "entry", "alias": "opus"}, False)
        assert "…" not in row.plain               # огрызка на месте адреса нет
        assert "host" not in row.plain
        assert row.plain.rstrip().endswith("← default")


# --- нижняя половина: строки слотов и привязок тоже не переносятся ------------
#
# Ширины сняты с живого смока владельца: на терминале 60 переносились все 19 строк
# нижней половины, на 72 — 15 из 19 (слоты выживали только за счёт хвостовых
# пробелов колонки значения, которые Rich при переносе не считает).
_LONG_ALIAS = (
    "models:\n"
    "  gigachat-corp-preview-08-26:\n"           # 27 колонок — крайний случай смока
    '    model: "gigachat/pro"\n'
    "slots:\n"
    '  default: "gigachat-corp-preview-08-26"\n'  # light и max остаются «не задан»
)


def _lower_rows(ed) -> list:
    """Виджеты нижней половины секции: строки слотов, привязок и резервной модели —
    всё, что идёт после записей реестра и строки «+ добавить» (она — часть верхнего
    блока и меряется своим стражем)."""
    return list(ed.query(".models-row"))[len(ed._aliases) + 1:]


def _grid_rows(ed) -> list:
    """Из нижней половины — только строки ОДНОЙ сетки (слоты и привязки): у резервной
    строки сетка другая, она стоит в колонках реестра."""
    from config_modules.model_registry import LEVELS
    from tui.editors.models_section import assign_consumers
    return _lower_rows(ed)[:len(LEVELS) + len(assign_consumers())]


@pytest.mark.parametrize("body,model", [(_ADDRESSED, '"test-model"'),
                                        (_LONG_ALIAS, '""')],
                         ids=["типовой реестр", "длинный алиас, резервная не задана"])
async def test_the_lower_half_never_wraps_at_any_width(tmp_path, body, model):
    """Ни одна строка нижней половины — слоты, привязки и резервная модель — не
    переносится НИ НА КАКОЙ ширине; слоты и привязки при этом стоят одной колонкой.

    Замер живой: `region.height` смонтированной строки, а не длина текста (перенос
    решает Rich, и хвостовые пробелы колонки значения он не считает — на длину строки
    это не похоже).

    Терминал 12 колонок пропущен осознанно: рамка оставляет строке одну колонку, в
    которой не помещается сам курсор («▸ ») — там не спасает никакая лестница."""
    async with _section(tmp_path, body, model=model, size=(80, 40)) as (pilot, scr, _cfg):
        ed = _editor(scr)
        for width in range(12, 140):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            if ed._row_width() < 2:
                continue
            rows = _lower_rows(ed)
            wrapped = [str(w.render()) for w in rows if w.region.height != 1]
            assert not wrapped, (width, ed._row_width(), wrapped[:2])
            grid = _grid_rows(ed)
            assert len({len(str(w.render())) for w in grid}) == 1, (width, ed._row_width())


async def test_the_reserve_row_stands_in_the_registry_grid(tmp_path):
    """Строка «резервная модель» стоит в сетке РЕЕСТРА, к которому относится: её
    подпись занимает ту же колонку, что алиасы записей, и ужимается вместе с ними —
    иначе на узкой рамке записи съезжают влево, а она остаётся стоять сама по себе.

    Колонка алиаса подпёрта снизу длиной этой подписи (16) — и ровно один раз: это
    та же колонка, а не своя плюс подпёртая."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 100
        assert ed._entry_columns()[0] == len("резервная модель")
        entry = ed._row_text({"kind": "entry", "alias": "opus"}, False)
        reserve = ed._row_text({"kind": "reserve"}, False)
        assert reserve.plain.index("test-model") == entry.plain.index("anthropic")
        ed._row_width = lambda: 19          # лестница реестра ужала саму колонку алиаса
        assert ed._entry_columns()[0] == 17
        entry = ed._row_text({"kind": "entry", "alias": "opus"}, False)
        reserve = ed._row_text({"kind": "reserve"}, False)
        assert len(reserve.plain) == len(entry.plain) == 19


async def test_the_reserve_row_drops_its_hint_whole(tmp_path):
    """Хвост строки — подсказка «действует только без реестра» — уходит ЦЕЛИКОМ
    (канон `_mode_row_text`), а не режется многоточием: та же фраза стоит в шапке
    под-формы резервной модели (Enter), терять её насовсем негде. Ядро строки —
    подпись и сама модель."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 60          # натуральная ширина строки — ровно она
        assert ed._row_text({"kind": "reserve"}, False).plain.endswith(
            "действует только без реестра")
        ed._row_width = lambda: 59
        row = ed._row_text({"kind": "reserve"}, False)
        assert "действует" not in row.plain
        assert row.plain.rstrip().endswith("test-model")


async def test_the_reserve_row_never_outgrows_the_frame(tmp_path):
    """Тот же свип ступенями, что у строк реестра и привязок: ширины 12…139, строка не
    длиннее рамки, ядро видимо всегда."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        for width in range(12, 140):
            ed._row_width = lambda w=width: w
            row = ed._row_text({"kind": "reserve"}, False)
            assert len(row.plain) <= width, (width, row.plain)
            assert row.plain.strip(), width


async def test_the_binding_row_never_outgrows_the_frame(tmp_path):
    """Тот же инвариант ступенями лестницы: строка слота/привязки не длиннее рамки на
    ширинах 12…139, и ядро строки (имя строки) видимо всегда."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        for width in range(12, 140):
            ed._row_width = lambda w=width: w
            for row in ({"kind": "slot", "level": "default"},
                        {"kind": "assign", "consumer": "subagents.subagent_special_agent"},
                        {"kind": "assign", "consumer": "*"}):
                text = ed._row_text(row, False)
                assert len(text.plain) <= width, (width, row, text.plain)
                assert text.plain.strip(), (width, row)


async def test_a_wide_frame_shows_the_longest_value_whole(tmp_path):
    """Колонка значения — ядро строки: на широкой рамке она обязана показывать самый
    длинный алиас реестра ЦЕЛИКОМ. Стережёт обе стороны новой лестницы: и ширину
    колонки (считается по алиасам, а не по одним подписям), и усечение значения (его
    очередь — последняя)."""
    async with _section(tmp_path, _LONG_ALIAS) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 100
        row = ed._row_text({"kind": "slot", "level": "default"}, False)
        assert row.plain.rstrip().endswith("gigachat-corp-preview-08-26")
        assert row.plain.startswith("  default · основная рабочая: повседневные задачи")


async def test_the_role_is_cut_before_the_name_of_the_level(tmp_path):
    """Порядок жертв: первым ужимается ОПИСАНИЕ роли, имя уровня и его значение —
    последнее, что уходит. Цвет уровня усечение переживает."""
    from tui.palette import RUN_COLOR
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 55
        row = ed._row_text({"kind": "slot", "level": "default"}, False)
        assert row.plain.startswith("  default · основная рабочая: повс")
        assert "…" in row.plain                       # ужато описание, не строка
        assert row.plain.rstrip().endswith("opus")    # значение цело
        assert _value_color(row, "default") == _rgb(RUN_COLOR)


async def test_a_narrow_frame_drops_the_role_whole(tmp_path):
    """Уже `_MIN_ROLE_COLS` описание перестаёт быть описанием — и уходит ЦЕЛИКОМ
    вместе с разделителем «·», а не оставляет огрызок «о…». Имя уровня и значение
    остаются: без них строка бессмысленна."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        ed._row_width = lambda: 26
        assert ed._row_text({"kind": "slot", "level": "default"}, False).plain.startswith(
            "  default · осн…")                       # ступенью выше огрызок ещё жив
        ed._row_width = lambda: 25
        row = ed._row_text({"kind": "slot", "level": "default"}, False)
        assert "·" not in row.plain
        assert "…" not in row.plain
        assert row.plain.startswith("  default ")
        assert row.plain.rstrip().endswith("opus")


def _entry_rows(ed) -> list:
    """Отрисованные строки ЗАПИСЕЙ (первые в списке): у слотов и привязок сетка своя
    (`_binding_columns`), и стерегут её свои тесты выше."""
    widgets = [str(w.render()) for w in ed.query(".models-row")]
    return widgets[:len(ed._aliases)]


async def test_a_live_resize_recomputes_the_columns(tmp_path):
    """Ширина колонок зависит от рамки, а рамка меняется живым ресайзом — значит
    строки пересчитывает и он, а не только первый кадр."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        box = {"w": 100}
        ed._row_width = lambda: box["w"]
        ed._refresh_rows()
        box["w"] = 56
        ed.on_resize()
        assert max(len(r) for r in _entry_rows(ed)) <= 56


async def test_the_measurement_confirms_itself(tmp_path, monkeypatch):
    """Замер после раскладки одноразовым быть не может: появление скроллбара сужает
    регион на колонку УЖЕ после того, как строки посчитаны, и без второго прохода
    строка стабильно вылезала за рамку на единицу."""
    async with _section(tmp_path, _ADDRESSED) as (pilot, scr, _cfg_file):
        await pilot.pause()
        ed = _editor(scr)
        # Ресайз глушим НА КЛАССЕ (Textual зовёт обработчик через тип, инстансный
        # атрибут он не увидит): иначе он подменил бы собой проверяемый механизм —
        # регион строки уезжает и без ресайза виджета, самим появлением скроллбара.
        monkeypatch.setattr(type(ed), "on_resize", lambda self: None)
        box = {"w": 72}
        ed._row_width = lambda: box["w"]
        ed._refresh_rows()
        box["w"] = 71                       # «появился скроллбар» — регион уехал
        await pilot.pause()
        assert max(len(r) for r in _entry_rows(ed)) <= 71


async def test_a_dead_entry_dims_its_level_pointer_too(tmp_path):
    """Мёртвая запись гаснет ЦЕЛИКОМ: указатель уровня в полной яркости спорил бы с
    погашенными алиасом и id — «эта строка работает» и «эта строка не работает» в
    одной строке."""
    from tui.palette import DEEP_DIM
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('agent:\n  model: "test-model"\n'
                        "models:\n  flash:\n    model: \"m\"\n"
                        "slots:\n  default: \"flash\"\n", encoding="utf-8")
    import tui.app as app_mod
    app = app_mod.TuiApp(config={"model": "test-model"})
    with _isolated_config_layers(tmp_path):
        async with app.run_test() as pilot:
            scr = _mk_screen(config={"model": "test-model"}, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            row = _editor(scr)._row_text({"kind": "entry", "alias": "flash"}, False)
            assert "← default" in row.plain
            assert _spans_style(row) == {_rgb(DEEP_DIM)}


async def test_a_no_change_reply_does_not_resurrect_the_hidden_title(tmp_path):
    """Ответ «ничего не изменено» идёт СВОИМ каналом секции: слот заголовка в
    «Моделях» скрыт, и поднимать его на три секунды значит дёргать тело на два ряда
    вниз и обратно — прямо под курсором, которым только что нажали Enter."""
    async with _section(tmp_path) as (pilot, scr, _cfg_file):
        await _wait_legacy_list(pilot, scr)
        await pilot.press("enter")                   # сохранение без единой правки
        await pilot.pause()
        assert scr.query_one("#set-title", Static).display is False
        assert "ничего не изменено" in _texts(scr)

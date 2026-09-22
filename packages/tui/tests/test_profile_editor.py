"""Редактор открытого профиля — под-вид секции «Профили» (план волны B, Task 6).

Симметрия §1 спеки: формы ОДНИ, целевых файла два. Enter на строке профиля
открывает тот же `ModelsSectionEditor`, что и секция «Модели», но целью записи
становится файл профиля, валидатором — профильный (`validate_profile_text`:
запрет `profile:` внутри, compat устаревших ключей), а «резервной модели»
(`agent.model` основного конфига) в профиле нет вовсе.

Esc — назад к списку профилей (под-состояние закрывается своим уровнем, Global
Constraints); PgUp/PgDn и повторный вход в секцию под-вид не переживает.
"""
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from gc_tui.editors import models_section as m
from gc_tui.screens import settings as s
from test_profiles_crud import (
    _PROFILE_BODY,
    _base_config_text,
    _base_dict,
    _flash,
    _footer,
    _notice,
    _profiles_screen,
    _put_cursor,
    _type_name,
    _write,
)
from textual.widgets import Input, Static

# Секция-оверлей для стражей превью: НЕПУСТАЯ намеренно. Пустой якорь
# (`security: {}`) переопределением по §11 не считается — писатель оставляет
# такой после снятия последнего ключа, — и витрина о нём молчит по праву.
_OVERLAY_SECTION = "{name}:\n  enabled: true\n"


def _texts(scr) -> str:
    return " ".join(str(w.render()) for w in scr.query(Static))


def _editor(scr):
    from gc_tui.editors.models_section import ModelsSectionEditor
    return scr.query(ModelsSectionEditor).last()


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _project(tmp_path, monkeypatch) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    return project_dir


async def _open_profile(pilot, scr, name: str):
    await _put_cursor(pilot, scr, "profile", name)
    await pilot.press("enter")
    await pilot.pause()
    return _editor(scr)


def _cursor_to(ed, kind: str, **match) -> None:
    """Курсор редактора на строку нужного рода (индексы строк знает сам редактор —
    состав рядов у профиля другой, чем у config.yml)."""
    for i, row in enumerate(ed._rows()):
        if row["kind"] == kind and all(row.get(k) == v for k, v in match.items()):
            ed._idx = i
            ed._refresh_rows()
            return
    raise AssertionError(f"строки {kind}/{match} нет: {ed._rows()}")


async def _fill_entry(pilot, scr, alias: str, model: str) -> None:
    from gc_tui.editors.model_entry_form import ModelEntryForm
    form = scr.query(ModelEntryForm).last()
    form.query_one("#mef-alias", Input).value = alias
    form.query_one("#mef-model", Input).value = model
    form._presets_ctrl.close()      # раскрытый на входе пресет-лист съел бы Enter
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# --- дверь: Enter открывает, создание открывает сразу ------------------------------


async def test_enter_on_a_profile_row_opens_the_editor_on_that_file(tmp_path, monkeypatch):
    """Enter — «войти внутрь» (решение В1): тело секции заменяется редактором форм,
    целью записи которого стал ФАЙЛ ПРОФИЛЯ, а не config.yml."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        assert Path(ed._target) == profile
        assert not scr.query(".set-profiles-row")      # список уехал под редактор
        assert "профиль work" in str(scr.query_one("#set-title", Static).render())
        assert any("glm" in str(w.render()) for w in scr.query(".models-row"))


async def test_create_opens_the_new_profile_right_away(tmp_path, monkeypatch):
    """«Создать → сразу открыть» (UX старого плана): человек назвал профиль, чтобы
    его заполнить, — заполнять он идёт следующим действием, а не ищет строку."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "work")
        assert Path(_editor(scr)._target) == tmp_path / "base" / "profiles" / "work.yml"
        assert "создан" in _flash(scr)                 # плашка создания не потерялась


async def test_enter_on_the_none_row_says_there_is_nothing_to_open(tmp_path, monkeypatch):
    """«— без профиля» — не файл. Футер обещает «Enter — открыть» и на этой строке
    (он один на весь список), поэтому молчание было бы его ложью: no-op + подсказка
    (решение В1)."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "none")
        await pilot.press("enter")
        await pilot.pause()
        from gc_tui.editors.models_section import ModelsSectionEditor
        assert not scr.query(ModelsSectionEditor)
        assert scr._profiles_open is None
        assert s._PROFILE_NONE_NOTHING_TO_OPEN in str(
            scr.query_one("#set-title", Static).render())


async def test_enter_does_not_open_a_profile_whose_root_is_read_only(tmp_path, monkeypatch):
    """Урок N1 Task 5: не спрашивай там, где заведомо откажешь. Редактор — поверхность
    ЗАПИСИ; профиль внутри установленного пакета не пишется, и дверь не открывается."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    monkeypatch.setattr("gc_core.config._default_config_path", lambda: base_cfg)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("enter")
        await pilot.pause()
        from gc_tui.editors.models_section import ModelsSectionEditor
        assert not scr.query(ModelsSectionEditor)
        assert "не открыто" in _notice(scr)
        assert "каталог профилей" in _notice(scr)
        assert scr.query(".set-profiles-row")          # список на месте


# --- запись: цель — файл профиля, config.yml не тронут -----------------------------


async def test_the_form_writes_the_entry_into_the_profile_and_not_into_config_yml(
        tmp_path, monkeypatch):
    """Формы одни, целевых файла два: запись из открытого профиля уходит в его файл,
    а базовый config.yml остаётся байт-в-байт прежним."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        before = base_cfg.read_bytes()
        await _open_profile(pilot, scr, "work")
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "flash", "google/gemini-2.5-flash")

    assert _doc(profile)["models"]["flash"]["model"] == "google/gemini-2.5-flash"
    assert _doc(profile)["models"]["glm"], "прежняя запись профиля потерялась"
    assert base_cfg.read_bytes() == before


async def test_the_slot_picker_writes_into_the_profile(tmp_path, monkeypatch):
    """Слоты и привязки — тот же путь записи с той же целью: пикер значения пишет
    в файл профиля (а не в config.yml) и проходит профильным валидатором."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        before = base_cfg.read_bytes()
        ed = await _open_profile(pilot, scr, "work")
        _cursor_to(ed, "slot", level="light")
        await pilot.press("enter")                     # раскрыть пикер значений
        await pilot.pause()
        assert ed.picker_open
        await pilot.press("down")                      # первая позиция — снятие слота
        await pilot.press("enter")
        await pilot.pause()

    assert _doc(profile)["slots"]["light"] == "glm"
    assert base_cfg.read_bytes() == before


async def test_the_profile_key_cannot_be_written_into_a_profile(tmp_path, monkeypatch):
    """Профильный валидатор реально подключён (Task 1, `is_profile=True`): документ с
    `profile:` внутри — запрещённая цепочка профилей (§3.2), и запись отвергается
    целиком. КОНФИГ-валидатор тот же текст принимает — разницу даёт только цель."""
    from gc_core.config import validate_config_text_with_sections
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml",
                     'profile: "other"\n' + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        before = profile.read_bytes()
        await _open_profile(pilot, scr, "work")
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "flash", "google/gemini-2.5-flash")
        notice = _notice(scr)

    assert "не сохранено" in notice
    assert "profile" in notice                         # причина названа, а не «что-то не так»
    assert profile.read_bytes() == before
    validate_config_text_with_sections(profile.read_text(encoding="utf-8"))


async def test_focus_returns_to_the_form_after_a_write_fails_past_the_env_confirm(
        tmp_path, monkeypatch):
    """Confirm ${ENV:...} снимает фокус ради баннера (`_submit_form`); второй Enter
    подтверждает и уходит в `_finish_submit` → `_write`. Если запись всё же не
    прошла (здесь — запрещённый `profile:` внутри профиля), форма остаётся
    смонтированной, а фокус — снятым: клавиатура мертва, черновик виден, но
    неправим иначе как через Esc (L21 ревью MR #80, находка #8)."""
    from gc_tui import app as app_mod
    from gc_tui.editors.model_entry_form import ModelEntryForm
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml",
                     'profile: "other"\n'
                     'models:\n'
                     '  glm:\n'
                     '    model: "z-ai/glm-5.3"\n'
                     '    url: "http://glm-host/v1/chat/completions"\n'
                     '    token: "${ENV:GLM_TOKEN}"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        before = profile.read_bytes()
        ed = await _open_profile(pilot, scr, "work")
        _cursor_to(ed, "entry", alias="glm")
        await pilot.press("enter")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        form.query_one("#mef-token", Input).value = "literal-secret"
        form._presets_ctrl.close()
        await pilot.pause()
        await pilot.press("enter")                   # submit → баннер перезаписи ссылки
        await pilot.pause()
        assert ed._env_pending is not None, "баннер подтверждения не поднялся"
        await pilot.press("enter")                   # подтвердить — запись должна упасть
        await pilot.pause()

        assert "не сохранено" in _notice(scr), _notice(scr)
        assert profile.read_bytes() == before         # запрещённый ключ отверг запись целиком
        assert scr.query(ModelEntryForm), "форма ушла, хотя запись не прошла"
        assert isinstance(scr.focused, Input), "фокус не вернулся в форму после отказа"


async def test_the_address_warning_and_the_literal_token_warning_share_one_notice(
        tmp_path, monkeypatch):
    """`_write` шлёт предупреждение об адресе (ok=None), а следом
    `_warn_literal_tokens` шлёт СВОЁ (тоже ok=None) — оба на один канал
    `_notice`/`#set-notice`. Оба вызова синхронны, без паузы между ними —
    второй не находит виджет первого (`Widget.mount` регистрирует его для
    `query()` не раньше следующего такта Textual) и монтирует РЯДОМ ВТОРОЙ
    `#set-notice` с тем же id: два виджета вместо одной подмены — докстрока
    `_warn_literal_tokens` обещала «обе строки видны разом» ОДНИМ сообщением
    (L21 ревью MR #80, находка #7). Профиль — ПРОЕКТНОГО корня (секрет-политика
    литеральных токенов включена), базовый конфиг — без `backend` вовсе (адреса
    нет НИГДЕ, наследовать нечего)."""
    from gc_tui import app as app_mod
    project_dir = _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", 'agent:\n  model: "test-model"\n')
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          'models:\n'
          '  secret:\n'
          '    model: "secret-model"\n'
          '    url: "http://secret-host/v1/chat/completions"\n'
          '    token: "sk-literal"\n'
          'slots:\n'
          '  default: "secret"\n')            # default уже резолвится — без вопроса о слоте

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        assert ed._project_profile, "предпосылка: секрет-политика профиля включена"
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "noaddr", "noaddr-model")
        notice = _notice(scr)
        widgets = list(scr.query("#set-notice"))

    assert len(widgets) == 1, "предупреждения разъехались по разным виджетам"
    assert "адрес не задан" in notice, notice          # первое предупреждение не стёрто
    assert "литеральный токен" in notice, notice        # вторым не заменено, а дополнено


async def test_a_removed_legacy_key_in_the_profile_does_not_break_the_write(
        tmp_path, monkeypatch):
    """Устаревший ключ (`compat.REMOVED`) попадает в профиль копипастой секции из
    старого config.yml. Валидатор профиля применяет тот же compat, что загрузчик, —
    запись проходит, а не падает на `extra="forbid"`."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml",
                     "logging:\n  dump_storage: true\n" + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "flash", "google/gemini-2.5-flash")

    assert _doc(profile)["models"]["flash"]["model"] == "google/gemini-2.5-flash"
    assert _doc(profile)["logging"]["dump_storage"] is True   # чужой ключ не вычищен


# --- состав секции в профильном режиме --------------------------------------------


async def test_the_reserve_model_row_is_absent_in_a_profile(tmp_path, monkeypatch):
    """`agent.model` — ключ config.yml; в профиле его нет, значит нет ни строки
    «резервная модель», ни её под-формы: Enter на последней строке открывает пикер
    привязки, а не легаси-редактор модели."""
    from gc_tui import app as app_mod
    from gc_tui.editors.model import ModelEditor
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        assert not [r for r in ed._rows() if r["kind"] == "reserve"]
        assert "резервная модель" not in _texts(scr)
        ed._idx = len(ed._rows()) - 1
        await pilot.press("enter")
        await pilot.pause()
        assert not ed.legacy_open
        assert not scr.query(ModelEditor)


async def test_a_profile_that_only_repoints_slots_is_not_called_empty(tmp_path, monkeypatch):
    """Профиль без своих `models`, но со своими `slots`/`assign`: реестра у него нет,
    а переопределяет он вполне себе — и строка того же файла в списке, одним
    нажатием раньше, показывает «default flash». Экран не имеет права ни
    противоречить ей, ни утверждать, что файл ничего не переопределяет.

    Формы `slots`/`assign` показывают только при НЕПУСТОМ реестре — значит здесь их
    не показывает никто, и они становятся такими же невидимыми оверлеями, как
    security/summary: место им в превью."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml",
           'slots:\n  default: flash\nassign:\n  "*": max\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        row = " ".join(str(w.render()) for w in scr.query(".set-profiles-row"))
        assert "default flash" in row              # список говорит: переопределяет
        ed = await _open_profile(pilot, scr, "work")
        assert ed._rows() == []                    # реестра действительно нет
        texts = _texts(scr)
        assert "ничего не переопределяет" not in texts
        assert "профиль также переопределяет: assign · slots" in texts
        assert "секции: assign, slots" in texts


async def test_the_overlay_preview_stays_silent_about_what_the_forms_show(tmp_path, monkeypatch):
    """Обратная сторона: как только реестр непуст, `slots`/`assign` показывают формы
    — и в превью им делать нечего, иначе оно дублирует видимое.

    Секция-свидетель НЕПУСТА намеренно: пустой якорь (`security: {}`) с §11
    переопределением не считается вовсе, и на нём страж мерил бы не тот факт."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml",
           _OVERLAY_SECTION.format(name="security") + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        assert scr._profile_overlay_names() == ["security"]


async def test_level_inside_a_profile_shows_in_the_preview_but_not_in_the_list_row(
        tmp_path, monkeypatch):
    """`level` внутри файла профиля не показывают формы НИ В ОДНОМ состоянии: строка
    «уровень модели по умолчанию» секции «Профили» правит ГЛОБАЛЬНЫЙ `level:`
    конфига, а не ключ этого файла. По общему правилу превью («называем то, чего не
    видно формами») место ему там — иначе профиль с `level: max` не проявляется на
    экране нигде.

    В СТРОКЕ СПИСКА исключение остаётся: там канон §12.3, у профиля своя колонка
    состава, и `level` исключён осознанно (ruling координатора 2026-08-11)."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", "level: max\n" + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        row = next(str(w.render()) for w in scr.query(".set-profiles-row")
                   if "work" in str(w.render()))
        assert "level" not in row
        await _open_profile(pilot, scr, "work")
        assert scr._profile_overlay_names() == ["level"]
        assert "профиль также переопределяет: level" in _texts(scr)


async def test_a_validator_error_with_brackets_reaches_the_screen(tmp_path, monkeypatch):
    """Отказ валидатора — ЧУЖОЙ текст (координата ключа может нести `[скобки]` —
    ключ в кавычках в YAML законен), и уходит он в тот же `_singleton`, что и
    все notice'ы. Разметка Rich в нём — не разметка, а данные: разбирать её
    значит ронять приложение на первом же устаревшем ключе в открытом профиле.
    (Раньше скобки поставлял pydantic-дамп `[type=…, input_value=[], …]` — после
    чистки дампа, MR #80 L28, их источник — сами данные человека.)"""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml",
                     '"[bogus]": []\n' + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        before = profile.read_bytes()
        await _open_profile(pilot, scr, "work")
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "flash", "google/gemini-2.5-flash")
        notice = _notice(scr)
        assert "не сохранено" in notice
        assert "[bogus]" in notice                  # скобки доехали как данные
        assert scr._profiles_open == "work"        # экран жив, под-вид на месте
    assert profile.read_bytes() == before


async def test_an_empty_profile_shows_its_own_empty_state(tmp_path, monkeypatch):
    """Только что созданный профиль (бланк `PROFILE_SKELETON`) — реестр пуст. Легаси-
    редактора модели у профиля нет, поэтому пустое состояние своё: самодостаточная
    подсказка на «вы» + вход в реестр клавишей `n`."""
    from gc_core.config_modules.profiles import PROFILE_SKELETON
    from gc_tui import app as app_mod
    from gc_tui.editors.model import ModelEditor
    from gc_tui.editors.model_entry_form import ModelEntryForm
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", PROFILE_SKELETON)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        assert ed._rows() == []
        assert not scr.query(ModelEditor)
        assert m._PROFILE_EMPTY_HINT in _texts(scr)
        assert _footer(scr) == m.FOOTERS_WITH_VIEW[m._FOOTER_PROFILE_EMPTY]
        await pilot.press("n")
        await pilot.pause()
        assert scr.query(ModelEntryForm), "вход в реестр из пустого состояния не работает"


# --- превью секций-оверлеев --------------------------------------------------------


_OVERLAY_BODY = ("security:\n  urls_blacklist: []\n"
                 "summary_params:\n  last_dialog_steps_count: 2\n") + _PROFILE_BODY


async def test_the_overlay_preview_names_the_sections_raw(tmp_path, monkeypatch):
    """Превью «профиль также переопределяет: …» — механически из топ-уровневых ключей
    файла минус `models/slots/assign/level`, СЫРЫМИ именами (§12.3): форм для этих
    секций нет, и об этом сказано прямо."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _OVERLAY_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        texts = _texts(scr)
        assert "профиль также переопределяет: security · summary_params" in texts
        assert "секции: security, summary_params" in texts
        assert "YAML" in texts                         # форм для них здесь нет


async def test_the_overlay_preview_truncates_by_whole_names(tmp_path, monkeypatch):
    """Усечение — ЦЕЛЫМИ именами с хвостом «…» (канон `fit_mode_pairs`): половина
    имени секции читалась бы как другое имя. Полный список остаётся в подсказке."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    body = "".join(_OVERLAY_SECTION.format(name=name) for name in
                   ("agent_settings", "intent_guard", "long_memory", "security",
                    "summary_params", "tools")) + _PROFILE_BODY
    _write(tmp_path / "base" / "profiles" / "work.yml", body)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        monkeypatch.setattr(type(scr), "_profile_overlay_width", lambda self: 46)
        text = scr._profile_overlay_text()
        names = ["agent_settings", "intent_guard", "long_memory", "security",
                 "summary_params", "tools"]
        shown = text.split(": ", 1)[1].split(" · ")
        assert shown[-1] == "…", f"хвоста усечения нет: {text}"
        assert shown[:-1] == names[:len(shown) - 1], f"имя разрезано: {text}"
        assert len(shown) - 1 < len(names)
        assert all(name in scr._profile_overlay_hint() for name in names)


# --- выходы: под-состояние не переживает ни один из них ----------------------------


async def test_esc_returns_to_the_list_and_the_second_esc_to_the_menu(tmp_path, monkeypatch):
    """Esc-вложенность (Global Constraints): под-вид закрывается СВОИМ уровнем и
    съедает клавишу, в меню уводит только следующий Esc."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == s._PROFILES
        assert scr.query(".set-profiles-row"), "список профилей не вернулся"
        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == "menu"


async def test_esc_closes_the_inner_picker_before_the_editor(tmp_path, monkeypatch):
    """Вложенность работает на всю глубину: раскрытый пикер значения — под-состояние
    редактора, и первый Esc снимает его, не выбрасывая к списку профилей."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        _cursor_to(ed, "slot", level="max")
        await pilot.press("enter")
        await pilot.pause()
        assert ed.picker_open
        await pilot.press("escape")
        await pilot.pause()
        assert not ed.picker_open
        assert not scr.query(".set-profiles-row"), "выбросило к списку профилей"


async def test_switching_sections_closes_the_open_profile(tmp_path, monkeypatch):
    """PgUp/PgDn идёт через `_open_section` — под-вид живёт ровно один заход в секцию:
    вернувшийся сюда человек застаёт список, а не чужой открытый профиль."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view != s._PROFILES
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._view == s._PROFILES
        assert scr.query(".set-profiles-row"), "секция открылась чужим под-видом"


async def test_a_profile_deleted_from_outside_falls_back_to_the_list(tmp_path, monkeypatch):
    """Файл открытого профиля исчез под ногами (правка извне, второе окно): секция
    возвращается к списку сама, а не рисует редактор поверх несуществующего файла."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        profile.unlink()
        scr._render_view()
        await pilot.pause()
        from gc_tui.editors.models_section import ModelsSectionEditor
        assert not scr.query(ModelsSectionEditor)
        assert scr._profiles_open is None


async def test_the_list_keys_do_not_reach_it_while_the_editor_is_open(tmp_path, monkeypatch):
    """Пока открыт под-вид, клавиши секции целиком его — включая те, которых у
    редактора нет ВОВСЕ.

    `Del` он берёт сам, и адресует она ЗАПИСЬ РЕЕСТРА, а не профиль под курсором
    списка. А `r` редактор не берёт — и обработчик обязан оборваться всё равно:
    провалившись в список под ним, она открыла бы переименование профиля прямо
    поверх редактора. Тот же обрыв прикрывает `Space` (активация) и `n` в поле
    ввода — «клавиша не взята» и «клавиша разрешена нижнему слою» здесь разные
    вещи."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        _cursor_to(ed, "entry", alias="glm")
        await pilot.press("delete")
        await pilot.pause()
        assert ed.awaiting_confirm
        assert not scr.query(".set-confirm"), "поднялся баннер удаления ПРОФИЛЯ"
        assert "glm" in _texts(scr)
        await pilot.press("escape")                # снять баннер записи реестра
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert scr._profiles_name_prompt is None, "клавиша списка провалилась под редактор"
        assert scr._profiles_open == "work"
        await pilot.press("escape")                # назад к списку — там тоже чисто
        await pilot.pause()
        assert not scr.query("#set-profile-name")
    assert profile.is_file()


# --- футеры ------------------------------------------------------------------------


async def test_the_list_footer_promises_opening(tmp_path, monkeypatch):
    """Enter заработал — футер обязан его назвать (молчание о работающей клавише —
    тот же класс лжи, что обещание неработающей). Полный литерал В1 стережёт
    `test_profiles_crud.test_footer_names_the_v1_keys_on_a_profile_row`."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        footer = _footer(scr)

    assert "Enter — открыть" in footer
    assert footer == s._FOOTERS[s._PROFILES]


async def test_the_editor_footer_leads_back_to_the_list(tmp_path, monkeypatch):
    """Футер под-вида — позиционный футер секции «Модели», но Esc в нём возвращает
    К СПИСКУ профилей, а не в меню хаба: вложенность обязана быть названа.

    Строку даёт редактор, а обещание просмотра файла (`v`, §11a) дописывает
    экран — на экране стоит именно этот, дополненный, вариант."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        assert _footer(scr) == m.FOOTERS_WITH_VIEW[m._FOOTER_PROFILE_ENTRY]
        _cursor_to(ed, "slot", level="light")
        scr._refresh_footer()
        assert _footer(scr) == m.FOOTERS_WITH_VIEW[m._FOOTER_PROFILE_SLOT]
        _cursor_to(ed, "assign", consumer="*")
        scr._refresh_footer()
        assert _footer(scr) == m.FOOTERS_WITH_VIEW[m._FOOTER_PROFILE_ASSIGN]


def test_the_way_back_is_named_one_way_everywhere():
    """Фолбэк футера (тело под-вида ещё не смонтировано — первый кадр) обещает
    возврат К СПИСКУ, а не «Esc — меню»: недостижимость обещания его правдой не
    делает. Формулировка при этом ОДНА на оба модуля — импорт, не копия."""
    from gc_tui.editors.models_section import FOOTER_PROFILE_BACK
    assert s.FOOTER_PROFILE_BACK is FOOTER_PROFILE_BACK
    assert "меню" not in s.FOOTER_PROFILE_BACK
    assert m._FOOTER_PROFILE_ENTRY.endswith(s.FOOTER_PROFILE_BACK)


def test_profile_footers_mirror_the_config_ones():
    """Профильные футеры — ПРОИЗВОДНЫЕ от конфиговых, а не вторые литеральные строки:
    клавиша, дописанная в одном режиме, обязана появиться и в другом — иначе футер
    соврёт молчанием на второй же правке."""
    pairs = ((m._FOOTER_ENTRY, m._FOOTER_PROFILE_ENTRY),
             (m._FOOTER_SLOT, m._FOOTER_PROFILE_SLOT),
             (m._FOOTER_ASSIGN, m._FOOTER_PROFILE_ASSIGN))
    for base, profile in pairs:
        assert base.endswith(" · Esc")
        assert profile.endswith(" · Esc — к списку")
        assert profile[:-len(" — к списку")] == base

async def test_the_first_block_caption_stands_under_the_profile_title(tmp_path, monkeypatch):
    """В под-виде заголовок рамки свой («профиль work») и дублирования нет — подпись
    первого блока встаёт ПОД ним, а не вместо него."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        title = scr.query_one("#set-title", Static)
        assert title.display is True
        assert "профиль work" in str(title.render())
        assert "запись:" in str(list(ed.children)[0].render())
        first_caption = str(list(ed.children)[1].render())
        assert "реестр" in first_caption and first_caption.startswith("▾")


# --- наследование: адрес и токен — из подключения ОТКРЫТОГО профиля ---------------


def _active_profile_runtime() -> dict:
    """Runtime экрана, каким его собирает старт с АКТИВНЫМ профилем: свой адрес и
    свой токен. Именно он подставлялся, когда стек ОТКРЫТОГО профиля не собрался, —
    и именно ему нельзя попадать в витрину чужого файла."""
    return {"completions_url": "https://active-profile/v1/chat/completions",
            "chat_completion_headers": {"Authorization": "Bearer ACTIVE"},
            "model": "test-model", "profile": "home"}


# База со СВОЕЙ записью реестра: над ней реестр открытого профиля становится
# двузначным, и фаза 2 (`slots.default`) на стеке профиля без слотов не сходится —
# ровно тот живой стенд, на котором подключение подменялось чужим.
_BASE_WITH_A_MODEL = _base_config_text("home") + "models:\n  base-main:\n    model: b\n"

_CORP_PROFILE = ("backend:\n"
                 '  completions_url: "https://corp/v1/chat/completions"\n'
                 "  chat_completion_headers:\n"
                 '    Authorization: "Bearer CORP"\n'
                 "models:\n"
                 "  m1:\n"
                 '    model: "x"\n')


async def test_an_open_profile_inherits_from_its_own_connection(tmp_path, monkeypatch):
    """Профиль со своим `backend`, открытый при ДРУГОМ активном: и витрина его
    собственной записи, и проба формы обязаны говорить про адрес с токеном ЭТОГО
    файла — при активации роутер уведёт запись ровно туда.

    Runtime экрана здесь не годится ровно по той же причине, по которой не годится
    тень базы (`_base_registry_snapshot`): он собран с УЖЕ АКТИВНЫМ профилем, и
    строка чужого файла показывала бы «наследуется: <адрес активного>», а Ctrl+Space
    стучался бы туда же — чужим токеном."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml", _CORP_PROFILE)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "corp")
        assert ed._effective_url == "https://corp/v1/chat/completions"
        assert ed._effective_auth == "Bearer CORP"


async def test_an_open_profile_keeps_its_connection_when_its_registry_does_not_resolve(
        tmp_path, monkeypatch):
    """Тот же профиль со своим `backend`, но своих слотов у него нет, а реестр над
    базой с записью стал двузначным: ПОЛНЫЙ стек не собирается (фаза 2 требует
    `slots.default`). К адресу и токену эта нехватка отношения не имеет.

    Отказ реестра не даёт права подставить чужое подключение: строка записи
    показывала бы адрес АКТИВНОГО профиля, Ctrl+Space стучался бы туда его же
    токеном, а активация увела бы эту запись совсем на другой адрес."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _BASE_WITH_A_MODEL)
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml", _CORP_PROFILE)

    app = app_mod.TuiApp(config=_active_profile_runtime())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=_active_profile_runtime())
        ed = await _open_profile(pilot, scr, "corp")
        assert ed._effective_url == "https://corp/v1/chat/completions"
        assert ed._effective_auth == "Bearer CORP"
        assert ed._entry_address("m1") == "https://corp/v1/chat/completions"
        assert "active-profile" not in _texts(scr)


async def test_an_open_profile_without_its_own_connection_keeps_the_base_one(
        tmp_path, monkeypatch):
    """Контроль: своего `backend` у профиля нет — наследуется адрес основного
    конфига, как и раньше. Стек профиля не имеет права ничего «терять»."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml", "models:\n  m1:\n    model: x\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "corp")
        assert ed._effective_url == "http://host/v1/chat/completions"


async def test_an_open_profile_that_cannot_be_stacked_falls_back_to_its_documents(
        tmp_path, monkeypatch):
    """Стек открытого профиля не собрался (файл — валидный YAML, но не проходит
    схему): подключение считается по ДОКУМЕНТАМ. Своего `backend` у профиля нет —
    значит адрес основного конфига, а не runtime с активным профилем: отказ собрать
    стек не повод ни оставить редактор без адреса, ни выдать ему чужой."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml",
           "max_steps: не-число\nmodels:\n  m1:\n    model: x\n")

    app = app_mod.TuiApp(config=_active_profile_runtime())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=_active_profile_runtime())
        ed = await _open_profile(pilot, scr, "corp")
        assert ed._effective_url == "http://host/v1/chat/completions"
        assert ed._effective_auth is None


async def test_a_profile_whose_file_is_unreadable_never_lends_another_connection(
        tmp_path, monkeypatch):
    """Файл профиля нечитаем (битый YAML). Под-вид над ним не открывается вовсе —
    гейт Enter отказывает, экран жив, — а если подключение всё же считают, отвечает
    единственный прочитанный документ: основной конфиг. Чужого профиля в ответе нет
    ни при каком состоянии файла: документный расчёт его попросту не видит."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _BASE_WITH_A_MODEL)
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    profile_file = _write(tmp_path / "base" / "profiles" / "corp.yml", "backend: [не закрыт\n")

    app = app_mod.TuiApp(config=_active_profile_runtime())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=_active_profile_runtime())
        await _put_cursor(pilot, scr, "profile", "corp")
        await pilot.press("enter")
        await pilot.pause()
        # Отказ отвечает про ДЕЙСТВИЕ. Путь и причина уже стоят в блоке под
        # курсором той же строки (`_profiles_row_hint`), и повтор давал их на
        # экране третий раз подряд.
        notice = _notice(scr)
        assert "не открыто" in notice and "не читается" in notice
        assert str(profile_file) not in notice
        assert scr._profiles_open is None
        endpoint, auth = scr._probe_args_from_config(scr._open_profile_config("corp"))[:2]
        assert endpoint == "http://host/v1/chat/completions"
        assert auth is None


async def test_an_open_profile_names_the_level_that_is_really_acting(tmp_path, monkeypatch):
    """Та же поправка, что и у секции «Модели»: пикер привязок под-вида обещает
    «работает активным уровнем: сейчас …», а уровень переключает сессионный
    `/level`, до файла не доходящий."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        app._session_overrides["level"] = "max"
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "home")
        assert ed._active_level == "max"


# --- превью оверлеев переживает запись в профиль ----------------------------------


async def test_the_overlay_preview_is_recounted_after_a_write(tmp_path, monkeypatch):
    """Состав превью зависит от того, что показывают ФОРМЫ, а запись в профиль это
    меняет: у профиля из одних `slots` реестр пуст, формы слотов не показаны — и
    `slots` живёт в превью. Первая же созданная запись поднимает эти формы, и
    превью обязано замолчать: иначе подпись дублирует видимое до перемонтирования
    под-вида."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", "slots:\n  default: flash\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _open_profile(pilot, scr, "work")
        assert "профиль также переопределяет: slots" in _texts(scr)   # предусловие
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "flash", "google/gemini-2.5-flash")
        assert "профиль также переопределяет" not in _texts(scr)


async def test_an_open_profile_offers_its_own_endpoint_presets(tmp_path, monkeypatch):
    """Тот же класс, что и адрес наследования: список пресетов адреса профиль вправе
    переопределить своим `endpoint_presets`, и открытый файл обязан показывать СВОЙ
    список, а не список активного профиля. Починить одно и оставить второе значило бы
    закрыть случай, а не правило."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml",
           _CORP_PROFILE
           + "endpoint_presets:\n"
             '  - name: "Корп-шлюз"\n'
             '    url: "https://corp-gw/v1/chat/completions"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "corp")
        assert [p.name for p in ed._presets] == ["Корп-шлюз"]


async def test_an_open_profile_is_written_under_the_stack_gate(tmp_path, monkeypatch):
    """Файл профиля правится с оглядкой на основной конфиг: пока профиль активен, его
    файл — часть стека следующего старта. Экран обязан отдать секции путь базы; активен
    ли профиль на самом деле, решает валидатор."""
    from gc_tui import app as app_mod
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "home")
        assert ed._stack_base == scr._config_path


async def test_renaming_in_an_inactive_profile_still_asks_for_a_default_slot(
        tmp_path, monkeypatch):
    """Ветка rename возвращалась раньше гейта `_needs_default_slot`: для неактивного
    профиля фаза 2 записи пропущена по дизайну (`validate_profile_text_with_sections`
    остаётся на фазе 1, пока указатель базы не приводит именно в этот файл), и
    транзакция, оставляющая реестр без резолвимого `default`, уходила бы на диск
    молча — старт падал бы только после активации профиля. Гейт спрашивается
    независимо от активности (`prospective_registry(profile_override="")`), и
    переименование обязано спрашивать его так же, как обычный upsert.

    Список ответов не должен предлагать исчезающий алиас: он снимается ЭТОЙ ЖЕ
    транзакцией, и выбор его целил бы слот в запись, которой в реестре уже не
    будет."""
    from gc_tui import app as app_mod
    from gc_tui.editors.model_entry_form import ModelEntryForm
    _project(tmp_path, monkeypatch)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())  # без указателя
    # У «prev» — вложенный `backend.completions_url`: формы этого поля нет вовсе
    # (`ModelEntryForm` не умеет его показывать), и подмена источника превью на
    # `alias` вместо `prev` потеряла бы его молча — ни один тест на плоском `model`
    # этого не поймает, там `fields` эхом несёт то же значение независимо от источника.
    body = ('models:\n'
            '  prev:\n'
            '    model: "prev-model"\n'
            '    token: "prev-secret"\n'
            '    backend:\n'
            '      completions_url: "http://prev-host/v1/chat/completions"\n'
            '  other:\n    model: "other-model"\n')
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", body)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        ed = await _open_profile(pilot, scr, "work")
        _cursor_to(ed, "entry", alias="prev")
        await pilot.press("enter")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        assert form.original_alias == "prev"
        form.query_one("#mef-alias", Input).value = "newname"
        form._presets_ctrl.close()
        await pilot.pause()
        await pilot.press("enter")                  # submit → должен спросить дефолт
        await pilot.pause()

        assert ed.picker_open, "рена́йм не спросил дефолт-слот при неактивном профиле"
        values = {row["value"] for row in ed._picker["rows"]}
        assert values == {"newname", "other"}        # "prev" исчезает этой транзакцией
        # Превью строки, которой в файле ещё нет под этим именем, обязано показывать
        # РЕАЛЬНЫЙ адрес старой записи (`backend.completions_url`, полю которого нет в
        # форме — подмена источника на `alias` вместо `prev` потеряла бы его молча), а
        # не пустоту: `_merged_entry` для превью берёт СТАРЫЙ алиас источником, как и
        # сам `_rename_ops`, иначе показанное разошлось бы с тем, что уедет в файл.
        newname_row = next(r for r in ed._picker["rows"] if r["value"] == "newname")
        desc = newname_row["desc"].plain
        assert "prev-model" in desc, desc
        assert "prev-host" in desc, desc

        await pilot.press("enter")                   # выбрать первую строку — "newname"
        await pilot.pause()

    doc = _doc(profile)
    assert set(doc["models"]) == {"newname", "other"}
    assert doc["slots"]["default"] == "newname"       # слот уехал той же транзакцией

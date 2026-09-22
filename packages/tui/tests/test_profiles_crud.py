"""CRUD файлов профилей в секции «Профили» (план волны B, Task 5).

Раскладка клавиш — решение юзера В1 (`docs/specs/2026-08-10-profiles-b-plan.md`),
выведенное из канона базы: Enter = «войти внутрь» (меню «открыть», MCP
«установить»), Space = переключение состояния (MCP «вкл/выкл», post-update
«отметить»). Активация профиля — смена состояния, поэтому активирует **Space**;
Enter открывает редактор профиля (Task 6, `test_profile_editor.py`).

Бэкап удаления — конвенция `config_sync` (`backups/<файл>.bak-<ts>`, В2), notice
называет путь. Санация сессионного выбора (§9.2 спеки профилей) — существующим
`app.sanitize_session_profile`, дошедшим сюда через `on_apply`: путь «удалить
профиль, выбранный в этой сессии» впервые достижим из UI.
"""
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_core.runtime_notices import clear_runtime_notices, collect_runtime_notices
from gc_testkit.tui_support import open_settings_section
from gc_tui.screens import settings as s
from textual.widgets import Input, Static


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _base_config_text(profile: str = "") -> str:
    return (
        f'profile: "{profile}"\n'
        "level: default\n"
        "backend:\n"
        '  completions_url: "http://host/v1/chat/completions"\n'
        "agent:\n"
        '  model: "test-model"\n'
    )


_PROFILE_BODY = "models:\n  glm:\n    url: http://y\n    token: sk\nslots:\n  default: glm\n"


def _base_dict() -> dict:
    return {"completions_url": "http://host/v1/chat/completions", "model": "test-model"}


def _fake_probe(endpoint, auth, verify, model, **kw):
    """Успешный результат, а не None: `_start_config_probe` разыменовывает
    `result.models`, и стаб-`None` ронял монтирование экрана ещё до предмета
    теста. Эталон — `_probe_ok` в `test_settings_screen`."""
    from gc_core.model_picker import ModelInfo
    from gc_tui.services.settings_probe import ProbeResult
    return ProbeResult("ok", 200, [ModelInfo(id="test-model")], 118, "доступен")


def _mk_screen(config=None, config_path=None, on_apply=None):
    from gc_tui.screens.settings import SettingsScreen
    return SettingsScreen(
        config=config or {}, config_path=str(config_path) if config_path else None,
        on_apply=on_apply or (lambda: None), probe_fn=_fake_probe,
    )


async def _profiles_screen(pilot, app, base_cfg, *, config=None, on_apply=None):
    scr = _mk_screen(config=config or _base_dict(), config_path=base_cfg, on_apply=on_apply)
    app.push_screen(scr)
    await pilot.pause()
    open_settings_section(scr, "Профили")
    await pilot.pause()
    return scr


def _row_index(scr, kind: str, name: str | None = None) -> int:
    rows = scr._profiles_rows()
    for i, row in enumerate(rows):
        if row["kind"] == kind and (name is None or row.get("name") == name):
            return i
    raise AssertionError(f"строки {kind}/{name} нет: {rows}")


async def _put_cursor(pilot, scr, kind: str, name: str | None = None) -> None:
    scr._profiles_idx = _row_index(scr, kind, name)
    scr._render_view()
    await pilot.pause()


def _footer(scr) -> str:
    return str(scr.query_one("#set-footer", Static).render())


def _flash(scr) -> str:
    return " ".join(str(w.render()) for w in scr.query("#set-flash"))


def _notice(scr) -> str:
    return " ".join(str(w.render()) for w in scr.query("#set-notice"))


async def _type_name(pilot, scr, value: str) -> None:
    scr.query_one("#set-profile-name", Input).value = value
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


# --- создание: бланк PROFILE_SKELETON в БАЗОВЫЙ корень ---------------------------


async def test_create_writes_the_blank_skeleton_into_the_base_root(tmp_path, monkeypatch):
    """`n` → инлайн-ввод имени → файл `<база>/profiles/<имя>.yml` из
    `PROFILE_SKELETON` (Task 1). Корень — базовый: проектные профили остаются
    ручной YAML-территорией (§12.3, территорию не расширяем)."""
    from gc_core.config_modules.profiles import PROFILE_SKELETON
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "work")

    created = tmp_path / "base" / "profiles" / "work.yml"
    assert created.read_text(encoding="utf-8") == PROFILE_SKELETON
    assert not (project_dir / ".gc" / "profiles" / "work.yml").exists()


async def test_created_file_is_valid_by_the_profile_schema(tmp_path, monkeypatch):
    """Страж бланка: то, что кладёт создание, обязано грузиться профильным
    валидатором — иначе первый же созданный профиль был бы нерабочим."""
    from gc_core.config_modules.profiles import validate_profile_text
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "work")

    text = (tmp_path / "base" / "profiles" / "work.yml").read_text(encoding="utf-8")
    validate_profile_text(text)                      # не бросает — файл валиден
    assert "sk-" not in text                         # бланк не несёт литерального токена




async def test_create_puts_the_cursor_on_the_new_row(tmp_path, monkeypatch):
    """После создания курсор стоит на новой строке — следующее действие
    (активировать, а с Task 6 и открыть) идёт по ней без поиска глазами."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "aaa.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "zzz")
        rows = scr._profiles_rows()
        assert rows[scr._profiles_idx]["kind"] == "profile"
        assert rows[scr._profiles_idx]["name"] == "zzz"


@pytest.mark.parametrize("name, expected", [
    ("default", "зарезервировано"),
    ("home", "уже существует"),
    ("", "введите имя"),
    ("../evil", "разделител"),
    ("имя:поток", "недопустимые символы"),
    ("отчёт*", "недопустимые символы"),
    ('кавычка"', "недопустимые символы"),
    ("труба|", "недопустимые символы"),
])
async def test_create_refuses_bad_names(tmp_path, monkeypatch, name, expected):
    """Резервное слово (`RESERVED_WORDS`), тёзка существующего, пустое имя,
    разделитель пути и символы, запрещённые в именах файлов Windows, — честная
    ошибка вместо файла. Разделитель отдельно: имя идёт прямо в имя файла, это
    единственная защита от выхода из корня.

    Запрещённые символы — там же и по той же причине: без проверки человек получал
    в лицо сырое «[Errno 22] Invalid argument» с временным путём, а `имя:поток`
    вдобавок оставлял в каталоге профилей мусорный файл `имя` — NTFS понимает
    двоеточие как альтернативный поток и создаёт носитель."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, name)
        assert expected in _notice(scr)
        assert [r["name"] for r in scr._profiles_rows() if r["kind"] == "profile"] == ["home"]
        # Отказ обязан быть ЧИСТЫМ: ни файла профиля, ни мусорного носителя ADS.
        assert sorted(p.name for p in (tmp_path / "base" / "profiles").iterdir())             == ["home.yml"]


async def test_create_refuses_on_the_package_template(tmp_path, monkeypatch):
    """Запуск на пакетном шаблоне: корень профилей лежит ВНУТРИ установленного
    пакета — тот же отказ, что у записи указателя (`_target_writable`), иначе
    создание писало бы файлы в пакет."""
    from gc_core.config import _default_config_path
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, Path(_default_config_path()))
        await pilot.press("n")
        await pilot.pause()
        assert not scr.query("#set-profile-name")
        assert "пакетный шаблон" in _notice(scr)


# --- переименование ---------------------------------------------------------------


async def test_rename_moves_the_file_and_follows_the_pointer_of_the_active(tmp_path, monkeypatch):
    """`r` на активном профиле: переименованы И файл, И указатель — иначе конфиг
    остался бы ссылаться на несуществующее имя (холодный старт — строгая ошибка)."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        assert scr.query_one("#set-profile-name", Input).value == "home"   # префилл
        await _type_name(pilot, scr, "house")

    assert (tmp_path / "base" / "profiles" / "house.yml").is_file()
    assert not (tmp_path / "base" / "profiles" / "home.yml").exists()
    assert 'profile: "house"' in base_cfg.read_text(encoding="utf-8")


async def test_rename_puts_the_cursor_on_the_renamed_row(tmp_path, monkeypatch):
    """Ревью MR #80 (L25): список сортирован, и новое имя встаёт в другую позицию.
    Курсор, оставшийся на прежнем индексе, указывал на СОСЕДНИЙ профиль — Space
    активировал соседа без подтверждения. Создание и удаление курсор ставят —
    переименование обязано тем же (образец: `test_create_puts_the_cursor_on_the_
    new_row`)."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "aaa.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "mmm.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "aaa")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "zzz")
        rows = scr._profiles_rows()
        assert rows[scr._profiles_idx]["kind"] == "profile"
        assert rows[scr._profiles_idx]["name"] == "zzz"


async def test_rename_of_an_inactive_profile_leaves_the_pointer_alone(tmp_path, monkeypatch):
    """Неактивный профиль указателем не назван — переименование его не трогает."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("other"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "other.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="other")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "house")

    assert (tmp_path / "base" / "profiles" / "house.yml").is_file()
    assert 'profile: "other"' in base_cfg.read_text(encoding="utf-8")


async def test_rename_to_the_same_name_is_a_quiet_cancel(tmp_path, monkeypatch):
    """Открыли ввод, ничего не поменяли, Enter — тихая отмена, а не «уже
    существует» (сам себе тёзкой не считается)."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "home")
        assert not scr.query("#set-profile-name")
        assert "уже существует" not in _notice(scr)
    assert (tmp_path / "base" / "profiles" / "home.yml").is_file()


async def test_rename_and_delete_are_noop_on_service_rows(tmp_path, monkeypatch):
    """`r`/`Del` работают по объекту под курсором: на «— без профиля» объекта нет —
    клавиши молчат (ввод имени не поднимается).

    Строка уровня из этой секции уехала в «Режимы работы» (2026-08-27), и
    сервисной строкой здесь осталась одна."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        for kind in ("none",):
            await _put_cursor(pilot, scr, kind)
            await pilot.press("r")
            await pilot.pause()
            assert not scr.query("#set-profile-name")
            await pilot.press("delete")
            await pilot.pause()
            assert not scr.query(".set-confirm")
    assert (tmp_path / "base" / "profiles" / "home.yml").is_file()


# --- удаление: подтверждение + бэкап (В2) ------------------------------------------


async def test_delete_asks_before_removing_and_esc_cancels(tmp_path, monkeypatch):
    """Del поднимает confirm-баннер (канон MCP) — файл ещё на месте; Esc снимает
    баннер и НЕ выходит из секции."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert "home" in banner and "Enter — удалить" in banner and "Esc — отмена" in banner
        assert profile.is_file()

        await pilot.press("escape")
        await pilot.pause()
        assert not scr.query(".set-confirm")
        assert scr._view == s._PROFILES
        assert profile.is_file()


async def test_delete_backs_the_file_up_and_names_the_path(tmp_path, monkeypatch):
    """Enter в баннере: файл уезжает в `backups/<файл>.bak-<ts>` (конвенция
    `config_sync`, В2), плашка называет ПУТЬ — иначе бэкап не найти."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    assert not profile.exists()
    backups = list((tmp_path / "base" / "profiles" / "backups").glob("home.yml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == _PROFILE_BODY
    assert str(backups[0]) in flash


async def test_delete_keeps_only_the_last_backups(tmp_path, monkeypatch):
    """Бэкапы профилей ограничены тем же числом, что и бэкапы конфига.

    Конвенцию держит `config_sync` (`backups/<файл>.bak-<ts>`, keep=10), и правило
    роста — её часть: экран переписал имя файла третьей редакцией, а обрезку не
    позвал, и каталог рос бесконечно — единственное такое место в приложении, при
    том что readme обещает «последние 10»."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    backups_dir = tmp_path / "base" / "profiles" / "backups"
    backups_dir.mkdir(parents=True)
    for i in range(12):                      # уже накопленные бэкапы прошлых удалений
        (backups_dir / f"home.yml.bak-2026010{i % 10}-00000{i % 10}").write_text("x", encoding="utf-8")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert not profile.exists()
    kept = sorted(backups_dir.glob("home.yml.bak-*"))
    assert len(kept) == 10, [p.name for p in kept]
    # Свежий — среди оставшихся: обрезка снимает СТАРЫЕ, а не только что созданный.
    assert any(p.read_text(encoding="utf-8") == _PROFILE_BODY for p in kept)


async def test_delete_of_the_active_profile_clears_the_pointer(tmp_path, monkeypatch):
    """Удаление активного снимает указатель (`profile: ""`) — иначе конфиг
    ссылался бы на исчезнувший файл; баннер об этом предупреждает заранее."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        assert "активен" in " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        await pilot.press("enter")
        await pilot.pause()

    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


# --- санация сессионного выбора (§9.2, находка A7) ---------------------------------


def _app_for_session(tmp_path, base_cfg):
    from gc_core.config import load_config_ex
    from gc_tui import app as app_mod
    cfg, _meta = load_config_ex(str(base_cfg))
    app = app_mod.TuiApp(threads_dir=str(tmp_path / "threads"), system="cli", config=cfg)
    app.set_config_path(str(base_cfg))
    return app


async def test_delete_sanitizes_the_session_choice_with_a_notice(tmp_path, monkeypatch):
    """Профиль выбран ТЕКУЩЕЙ сессией (`/profiles`), а не конфигом. Удаление из
    UI впервые делает этот путь достижимым: оверрайд обязан сняться с notice
    (§9.2), иначе сессия осталась бы с висячей ссылкой на удалённый файл."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = _app_for_session(tmp_path, base_cfg)
    async with app.run_test() as pilot:
        app.set_session_override("profile", "home")
        assert app._session_overrides.get("profile") == "home"
        scr = await _profiles_screen(pilot, app, base_cfg, on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert "profile" not in app._session_overrides
        assert "session_profile_stale" in [n.code for n in collect_runtime_notices()]


async def test_rename_of_another_profile_leaves_the_session_choice_alone(tmp_path, monkeypatch):
    """Переименовали ЧУЖОЙ профиль — сессионный выбор не при чём: ни переезда, ни
    санации (переезд выбранного проверяет отдельный страж ниже).

    Проверка «ничего не случилось» сама по себе зелена и на пустой реализации,
    поэтому страж ещё и читает ПЛАШКУ: она обязана честно сказать, на чём осталась
    сессия. Условие переезда, ослабленное до `if session_before:`, роняет и первый
    ассерт, и этот."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = _app_for_session(tmp_path, base_cfg)
    async with app.run_test() as pilot:
        app.set_session_override("profile", "home")
        scr = await _profiles_screen(pilot, app, base_cfg, on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")

        assert app._session_overrides.get("profile") == "home"
        assert "session_profile_stale" not in [n.code for n in collect_runtime_notices()]
        assert "остаётся на «home»" in _flash(scr)


# --- клавиши В1: Space активирует, Enter открывает ---------------------------------


async def test_space_activates_the_profile(tmp_path, monkeypatch):
    """Перевешенный страж волны A: активация — смена состояния, её клавиша Space
    (канон базы: MCP «вкл/выкл», post-update «отметить»). RED на прежней
    раскладке, где активировал Enter."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("space")
        await pilot.pause()

    assert 'profile: "home"' in base_cfg.read_text(encoding="utf-8")


async def test_enter_on_a_profile_row_opens_instead_of_activating(tmp_path, monkeypatch):
    """Enter — «войти внутрь» (редактор профиля, Task 6), и это НЕ активация:
    указатель `profile:` после него прежний. Разведение ролей В1 — Space меняет
    состояние, Enter входит внутрь (сам редактор — `test_profile_editor.py`)."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("enter")
        await pilot.pause()
        assert scr._view == s._PROFILES     # из секции не вышли
        assert scr._profiles_open == "home"

    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


async def test_space_on_the_none_row_deactivates(tmp_path, monkeypatch):
    """«— без профиля» — та же смена состояния, та же клавиша."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "none")
        await pilot.press("space")
        await pilot.pause()

    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


# --- гейт по фокусу и Esc внутри ввода --------------------------------------------


async def test_letters_and_space_go_into_the_name_field(tmp_path, monkeypatch):
    """Пока открыт ввод имени, буквы и пробел печатаются, а не командуют секцией
    (гейт `not isinstance(self.focused, Input)`) — иначе имя «no name» набрать
    нечем, а `Del` сносил бы профиль под курсором вместо символа."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile = _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("n")
        await pilot.pause()
        inp = scr.query_one("#set-profile-name", Input)
        assert scr.focused is inp
        await pilot.press("n", "r", "space", "delete")
        await pilot.pause()
        assert inp.value == "nr "
        assert len(scr.query("#set-profile-name")) == 1     # второго ввода не поднялось
        assert not scr.query(".set-confirm")                # Del не спросил об удалении
    assert profile.is_file()


async def test_esc_cancels_the_name_input_without_leaving_the_section(tmp_path, monkeypatch):
    """Esc при вводе имени — отмена ВВОДА (внутрь глобального escape-гейта), не
    выход в меню: под-состояние закрывается своим уровнем."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not scr.query("#set-profile-name")
        assert scr._view == s._PROFILES
        assert not (tmp_path / "base" / "profiles").exists()

        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == "menu"


# --- футеры (В1 дословно и целиком) -----------------------------------------------


async def test_footer_names_the_v1_keys_on_a_profile_row(tmp_path, monkeypatch):
    """Футер строки профиля — вариант В1 ДОСЛОВНО И ЦЕЛИКОМ: с приездом редактора
    (Task 6) заработал Enter, и сегмент «Enter — открыть» встал на своё место —
    первым, сразу за «↑/↓» (бюджет проверен: 115 символов, две строки на 64
    колонках — ровно из-за MCP-стиля разделителей)."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        footer = _footer(scr)

    assert footer == s._FOOTERS[s._PROFILES]
    assert footer == ("↑/↓ · Enter — открыть · Space — активировать · n — создать · "
                      "r — переименовать · Del — удалить · PgUp/PgDn — секции")


async def test_add_row_footer_names_creating_and_nothing_below_it(tmp_path, monkeypatch):
    """Пустое состояние: курсор стартует на строке «+ добавить профиль…» — её
    футер обещает создание и НЕ обещает клавиш объектов (Space/r/Del на
    строке-действии не работают: канон «недоступное не обещаем»).

    Ниже неё строк больше нет: уровень уехал в «Режимы работы» (2026-08-27), и
    ↓ на последней строке никуда не уводит — футер остаётся прежним."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        footer = _footer(scr)
        assert "Enter — создать" in footer          # курсор на «+ добавить профиль…»
        for lie in ("Space", "переименовать", "Del"):
            assert lie not in footer, lie
        await pilot.press("down")                   # ниже строк нет — курсор стоит
        await pilot.pause()
        assert _footer(scr) == footer
        # Переключать в этой секции больше нечего: трек уехал вместе со строкой.
        assert "←/→/Enter — переключить" not in footer


async def test_name_input_footer_is_positional(tmp_path, monkeypatch):
    """Контракт п.5: на время ввода имени футер — про ввод (Enter/Esc), а глагол
    называет ровно то, что произойдёт: создание или переименование."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        assert _footer(scr) == "Enter — создать  ·  Esc — отмена"

        await pilot.press("escape")
        await pilot.pause()
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        assert _footer(scr) == "Enter — переименовать  ·  Esc — отмена"


# --- ревью Task 5 #1: указатель во ВСЕХ файловых слоях, а не в слое-победителе ----


def _two_layer_setup(tmp_path, monkeypatch):
    """Сценарий ревью: базовый конфиг называет `work`, проектный перекрывает его
    на `corp`. Merged-указатель показывает только победителя (`corp`), поэтому
    вопрос «активен ли work» по `self._config` даёт «нет» — а базовый указатель
    его называет."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    _write(project_dir / ".gc" / "config.yml", 'profile: "corp"\n')
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml", _PROFILE_BODY)
    return base_cfg, project_dir


def _loads_outside_project(tmp_path, base_cfg, monkeypatch) -> dict:
    """Запуск ВНЕ проекта: проектного оверлея нет, действует базовый указатель.
    Он же — точка отказа §3.1: нерезолвимое имя роняет старт с ValueError."""
    from gc_core.config import load_config_ex
    monkeypatch.chdir(tmp_path)
    cfg, _meta = load_config_ex(str(base_cfg))
    return cfg


async def test_delete_clears_the_pointer_in_every_file_layer(tmp_path, monkeypatch):
    """Репро ревью дословно: Del на `work`, которого называет ТОЛЬКО базовый слой
    (в merged победил проектный `corp`). Указатель обязан сняться и там — иначе
    запуск вне проекта падает «профиль work не найден» (§3.1)."""
    from gc_tui import app as app_mod
    base_cfg, _project_dir = _two_layer_setup(tmp_path, monkeypatch)

    cfg = dict(_base_dict(), profile="corp")     # merged: победил проектный слой
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        assert str(base_cfg) in " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        await pilot.press("enter")
        await pilot.pause()

    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")
    assert _loads_outside_project(tmp_path, base_cfg, monkeypatch).get("profile") in ("", None)


async def test_rename_moves_the_pointer_in_every_file_layer(tmp_path, monkeypatch):
    """Та же двухслойная развилка у переименования: базовый указатель обязан
    поехать за именем, иначе он остаётся висеть на несуществующем файле."""
    from gc_tui import app as app_mod
    base_cfg, _project_dir = _two_layer_setup(tmp_path, monkeypatch)

    cfg = dict(_base_dict(), profile="corp")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")

    assert 'profile: "job"' in base_cfg.read_text(encoding="utf-8")
    assert _loads_outside_project(tmp_path, base_cfg, monkeypatch).get("profile") == "job"


# --- находка ревьюера A (вторая половина): указатель в проекте — фаза 2 стека ----


async def test_deactivating_via_the_project_layer_is_refused_when_it_breaks_the_stack(
        tmp_path, monkeypatch):
    """Ревью: модели — в базе, `slots.default` — в профиле (легальная раскладка),
    указатель `profile:` живёт в проектном `.gc/config.yml`. Раньше запись шла
    однофайловой валидацией (`validate_config_text`) — синтаксис `profile: ""`
    безупречен, и снятие профиля молча ложилось на диск конфигом, который
    следующий холодный старт не грузит (находка ревью, воспроизведено). Фаза 2
    обязана поймать это ДО записи — так же, как у указателя в базе."""
    from gc_core import config as config_mod
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      "backend:\n"
                      '  completions_url: "http://host/v1/chat/completions"\n'
                      "agent:\n"
                      '  model: "test-model"\n'
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')
    _write(tmp_path / "base" / "profiles" / "work.yml", 'slots:\n  default: "a"\n')
    project_cfg = _write(project_dir / ".gc" / "config.yml", 'profile: "work"\n')

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "none")
        await pilot.press("space")
        await pilot.pause()
        notice = _notice(scr)

    assert "профиль не снят" in notice, notice
    assert "раздел" in notice and "Модел" in notice, notice   # обёртка называет, где чинить
    assert "slots.default" in notice, notice                  # деталь валидатора никуда не делась
    # живой откат: файл проектного слоя — ровно тот же, что и до попытки записи
    assert project_cfg.read_text(encoding="utf-8") == 'profile: "work"\n'
    # и следующий холодный старт (внутри проекта — оверлей действует) всё ещё грузится
    loaded, _meta = config_mod.load_config_ex(str(base_cfg))
    assert loaded.get("profile") == "work"


async def test_activating_a_brand_new_project_profile_is_refused_when_it_breaks_the_stack(
        tmp_path, monkeypatch):
    """Тот же дефект, но хуже: `.gc/config.yml` ещё не существует, и писатель
    уходит в `create_pointer_file`, у которой не было ВООБЩЕ никакой валидации —
    ни фазы 1, ни фазы 2. Раскладка ревью: 2 модели в базе без слотов, проектный
    профиль (ручная YAML-территория — приложение туда не пишет, значит такой
    файл до активации ни разу не проходил проверку) со слотом на несуществующий
    алиас. Активация обязана отказать ДО создания файла — не создать конфиг,
    на котором следующий холодный старт падает необработанным ValueError."""
    from gc_core import config as config_mod
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      "backend:\n"
                      '  completions_url: "http://host/v1/chat/completions"\n'
                      "agent:\n"
                      '  model: "test-model"\n'
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')
    _write(project_dir / ".gc" / "profiles" / "work.yml", 'slots:\n  default: "ghost"\n')
    project_cfg = project_dir / ".gc" / "config.yml"
    assert not project_cfg.exists()

    cfg = dict(_base_dict())        # без активного профиля
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("space")
        await pilot.pause()
        notice = _notice(scr)

    assert "профиль не включён" in notice, notice
    assert "раздел" in notice and "Модел" in notice, notice   # обёртка называет, где чинить
    assert "ghost" in notice, notice                          # деталь валидатора никуда не делась
    assert not project_cfg.exists()   # файл не создан вовсе — не только не переписан
    # живой откат: раз файла нет, следующий холодный старт падает по СТАРОЙ,
    # НЕ связанной с этой находкой причине (2 модели без единого слота в самой
    # базе — ожидаемо для ещё не настроенного проекта), а не тем необработанным
    # ValueError про несуществующий алиас "ghost", которым бил старый дефект
    # (тот указатель — из так и не созданного файла — реально подставил бы
    # `work`, и стек уже пытался бы резолвить `ghost`).
    with pytest.raises(ValueError, match=r"slots\.default.*≥2") as excinfo:
        config_mod.load_config_ex(str(base_cfg))
    assert "ghost" not in str(excinfo.value)


async def test_renaming_the_active_profile_succeeds_when_the_base_needs_its_slot(
        tmp_path, monkeypatch):
    """СТРАЖ РЕГРЕССА: указатель пишется на НОВОЕ имя ДО физического rename()
    файла профиля (`_rename_profile`) — в момент валидации `job.yml` на диске
    ещё нет, `resolve_profile` его не находит. Раскладка «2+ моделей в базе,
    слот — в профиле» (легальная, §4) раньше ВСЕГДА отклоняла переименование
    активного профиля, каким бы безупречным ни было его содержимое: фаза 2
    откатывалась к однофайловой и судила базу БЕЗ профиля (2 модели, ни
    одного слота, «моделей ≥2» — читай ревью-регресс). Подстановка будущего
    документа профиля НА ЕГО будущем пути (`extra_candidates`) закрывает
    разрыв — содержимое профиля при переименовании не меняется, и это ровно
    та же (уже резолвимая) раскладка, что и до переименования."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      'profile: "work"\n'
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')
    work = _write(tmp_path / "base" / "profiles" / "work.yml", 'slots:\n  default: "a"\n')

    app = app_mod.TuiApp(config=dict(_base_dict(), profile="work"))
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=dict(_base_dict(), profile="work"))
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        notice = _notice(scr)
        flash = _flash(scr)

    assert "не переименован" not in notice, notice
    assert "переименован в «job»" in flash, flash
    job = tmp_path / "base" / "profiles" / "job.yml"
    assert not work.exists()
    assert job.exists() and job.read_text(encoding="utf-8") == 'slots:\n  default: "a"\n'
    assert 'profile: "job"' in base_cfg.read_text(encoding="utf-8")


async def test_renaming_a_profile_with_a_broken_slot_still_shows_the_wrapped_refusal(
        tmp_path, monkeypatch):
    """Подстановка чинит именно РАЗРЫВ резолва (имя ещё не на диске) — не
    отменяет фазу 2 вовсе. Профиль сломан НЕЗАВИСИМО от переименования (слот
    смотрит на несуществующий алиас) — будущий документ на новом пути несёт ТУ
    ЖЕ поломку, отказ законен, и текст остаётся человеческим, а не сырым
    сообщением валидатора."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      'profile: "work"\n'
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')
    work = _write(tmp_path / "base" / "profiles" / "work.yml", 'slots:\n  default: "ghost"\n')

    app = app_mod.TuiApp(config=dict(_base_dict(), profile="work"))
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=dict(_base_dict(), profile="work"))
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        notice = _notice(scr)

    assert "профиль не переименован" in notice, notice
    assert "раздел" in notice and "Модел" in notice, notice
    assert "ghost" in notice, notice
    # живой откат: ни файл, ни указатель не тронуты
    assert work.exists()
    assert not (tmp_path / "base" / "profiles" / "job.yml").exists()
    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")


async def test_deleting_the_sole_slot_provider_shows_the_wrapped_refusal(
        tmp_path, monkeypatch):
    """Находка соседей: `_delete_profile` шёл через `_repoint_profile_layers`
    тем же путём, что и включение/снятие/переименование ДО их правки, и не
    заворачивал отказ фазы 2 — человек видел сырое «✗ не удалить:
    slots.default: ...» в одном действии и человеческий текст в трёх соседних.
    Раскладка «2+ моделей в базе, слот в профиле»: удаление снимает указатель
    ПЕРЕД перемещением файла (`_repoint_profile_layers(layers, "")`), рвёт
    единственный слот — фаза 2 отказывает, текст обязан назвать действие и
    место починки, как и у остальных трёх."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      'profile: "work"\n'
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')
    work = _write(tmp_path / "base" / "profiles" / "work.yml", 'slots:\n  default: "a"\n')

    app = app_mod.TuiApp(config=dict(_base_dict(), profile="work"))
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=dict(_base_dict(), profile="work"))
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        notice = _notice(scr)

    assert "профиль не удалён" in notice, notice
    assert "раздел" in notice and "Модел" in notice, notice
    assert "≥2" in notice, notice
    # живой откат: ни файл, ни указатель не тронуты
    assert work.exists()
    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")


async def test_rollback_that_also_fails_does_not_promise_a_retry(tmp_path, monkeypatch):
    """Аварийный откат (`_rollback_profile_pointers`) — восстановление ПОСЛЕ
    чужого сбоя, не первая попытка: если восстановить старый указатель тоже не
    удаётся, «поправьте и повторите» — обещание того, чего нет (состояние уже
    смешанное). Вызывается напрямую: живой сценарий двойного сбоя (упавшая
    файловая операция ПОСЛЕ упавшего же отката) требует двух независимых сбоев
    подряд, а сам текст обёртки от их точной причины не зависит."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    # Восстановление "ghost" тоже уходит в фазу 2 и тоже отклоняется: указателя
    # на несуществующий профиль стек следующего старта не переживёт — ту же
    # дорогу проходит любой НЕуспешный `_write_profile_pointer`, откуда бы он ни
    # был вызван.
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        scr._rollback_profile_pointers(
            [base_cfg], "ghost", file_exc="исходная причина сбоя", action="переименовать")
        await pilot.pause()
        notice = _notice(scr)

    assert "повторите" not in notice, notice
    assert "переименовать не удалось" in notice, notice
    assert "автоматический откат" in notice, notice
    # причина сбоя самого отката тоже видна, не проглочена
    assert "ghost" in notice and "не найден" in notice, notice


async def test_stuck_rollback_sends_to_the_profiles_section_not_models(
        tmp_path, monkeypatch):
    """Ревью MR #80 (L36): указатель `profile:` пишет секция «Профили» — отказ
    отката слал чинить в «Модели», раздел, который ключ не правит (сам код рядом
    дважды осознанно проводит эту границу). Файлы перечислены следом, и «прямо в
    файле» — честная вторая дверь."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      "models:\n"
                      '  a:\n    model: "x"\n'
                      '  b:\n    model: "y"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        scr._rollback_profile_pointers(
            [base_cfg], "ghost", file_exc="исходная причина сбоя", action="удалить")
        await pilot.pause()
        notice = _notice(scr)

    assert "автоматический откат" in notice, notice     # предусловие: откат застрял
    assert "разделе «Профили»" in notice, notice
    assert "разделе «Модели»" not in notice, notice
    assert "в файле" in notice, notice


# --- ревью MR #80 (M16): откат указателей при сбое файловой операции — ЭФФЕКТ -----
#
# Код отката был верным, но ни один тест не ловил его выключение: мутация
# «выкинуть `_rollback_profile_pointers`» переживала весь файл (65 passed) —
# существующие тесты крыли только соседнюю ветку (упала сама запись указателя).
# Поэтому здесь проверяется эффект В ФАЙЛАХ, а не текст нотиса: указатель
# ВЕРНУЛСЯ, файл профиля цел, бэкапа нет.


async def test_failed_delete_move_rolls_the_pointer_back_in_the_file(tmp_path, monkeypatch):
    """Сбой перемещения файла ПОСЛЕ уже снятого указателя: `profile: "work"`
    возвращается в базовый конфиг — иначе холодный старт падал бы на указателе
    в никуда (§3.1), хотя файл профиля никуда не делся."""
    import shutil

    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    work = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    def _boom(*_a, **_k):
        raise OSError("диск отвалился")
    monkeypatch.setattr(shutil, "move", _boom)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "не удалось удалить" in _notice(scr)

    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")  # указатель вернулся
    assert work.is_file()                                             # файл на месте
    backups = tmp_path / "base" / "profiles" / "backups"
    assert not backups.exists() or not list(backups.iterdir())        # бэкапа нет


async def test_failed_rename_move_rolls_the_pointer_back_in_the_file(tmp_path, monkeypatch):
    """Сбой самого `rename()` ПОСЛЕ уже переписанных на новое имя указателей:
    указатель вернулся на СТАРОЕ имя, файла под новым именем не появилось."""
    import pathlib

    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    work = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    real_rename = pathlib.Path.rename

    def _boom(self, target, *args, **kwargs):
        if self.name == "work.yml":
            raise OSError("диск отвалился")
        return real_rename(self, target, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "rename", _boom)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "work2")
        assert "не удалось переименовать" in _notice(scr)

    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")  # старое имя вернулось
    assert work.is_file()
    assert not (tmp_path / "base" / "profiles" / "work2.yml").exists()


async def test_failed_delete_rolls_the_pointer_back_in_every_layer(tmp_path, monkeypatch):
    """Указатель в ДВУХ файловых слоях (проектный `.gc/config.yml` + базовый):
    откат возвращает исходное значение в КАЖДЫЙ — полу-откат оставлял бы слои
    спорящими, и победитель мержа ссылался бы в никуда."""
    import shutil

    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    project_cfg = _write(project_dir / ".gc" / "config.yml", 'profile: "work"\n')
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    work = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    def _boom(*_a, **_k):
        raise OSError("диск отвалился")
    monkeypatch.setattr(shutil, "move", _boom)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "не удалось удалить" in _notice(scr)

    assert 'profile: "work"' in project_cfg.read_text(encoding="utf-8")
    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")
    assert work.is_file()


# --- хвост M4 (ревью MR #80): указатель-ссылка ${ENV:X} — слой обязан опознаваться -
#
# Ядро (`layers.resolve_profile_pointer`) интерполирует указатель ПЕРЕД чтением
# имени; `_profile_pointer_layers` сравнивал СЫРОЕ значение — слой с
# `profile: "${ENV:X}"` не опознавался носителем имени, и переименование/удаление
# его пропускали: указатель оставался висеть на исчезнувшем имени, следующий
# старт падал «профиль не найден».


async def test_rename_rewrites_a_pointer_written_as_an_env_ref(tmp_path, monkeypatch):
    from gc_core import config_raw
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("GC_TEST_PROFILE_PTR", "work")
    project_cfg = _write(project_dir / ".gc" / "config.yml",
                         'profile: "${ENV:GC_TEST_PROFILE_PTR}"\n')
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")

    assert (tmp_path / "base" / "profiles" / "job.yml").is_file()
    # Слой-носитель опознан и переписан: ссылка резолвится в СТАРОЕ имя, оставить
    # её значило бы оставить указатель на исчезнувший профиль.
    assert config_raw.read_document(project_cfg).get("profile") == "job"


async def test_delete_snaps_a_pointer_written_as_an_env_ref(tmp_path, monkeypatch):
    from gc_core import config_raw
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("GC_TEST_PROFILE_PTR", "work")
    project_cfg = _write(project_dir / ".gc" / "config.yml",
                         'profile: "${ENV:GC_TEST_PROFILE_PTR}"\n')
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    broken = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert str(project_cfg) in banner, banner   # баннер называет слой-носитель
        await pilot.press("enter")
        await pilot.pause()

    assert not broken.exists()
    assert config_raw.read_document(project_cfg).get("profile") == ""


# --- ревью Task 5 #2: переименование ПЕРЕВОЗИТ сессионный выбор -------------------


async def test_rename_moves_the_session_choice_instead_of_dropping_it(tmp_path, monkeypatch):
    """Переименование — не исчезновение: сессионный выбор `/profiles` едет за новым
    именем (ruling координатора), а не снимается санацией §9.2. Переезд доходит и
    до meta треда — иначе возврат в тред воскресил бы старое имя."""
    clear_runtime_notices()
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.session_settings import read_from_meta
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    threads = tmp_path / "threads"
    # `threads_data_path` в самом конфиге: перечитка (`reload_config`) пересчитывает
    # каталог тредов ИЗ КОНФИГА, и без этой строки персист уехал бы в дефолтный
    # каталог пользователя мимо tmp_path.
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      _base_config_text()
                      + f'paths:\n  threads_data_path: "{threads.as_posix()}"\n')
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = _app_for_session(tmp_path, base_cfg)
    async with app.run_test() as pilot:
        app.tui_state.active_task_id = "t-rename"   # тред есть — персист доедет до meta
        app.set_session_override("profile", "home")
        scr = await _profiles_screen(pilot, app, base_cfg, on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "house")

        assert app._session_overrides.get("profile") == "house"
        assert "session_profile_stale" not in [n.code for n in collect_runtime_notices()]
        meta = FileTaskStore(threads_dir=str(tmp_path / "threads"),
                             system="cli").get_task_meta("t-rename")
        assert read_from_meta(meta).get("profile") == "house"


# --- ревью Task 5 #3/#4: эффект для сессии и обнажившийся тёзка — на экране -------


async def test_delete_banner_and_flash_name_the_session_effect(tmp_path, monkeypatch):
    """Канон волны A (`_activate_profile`): эффект для ТЕКУЩЕЙ сессии виден в
    плашке, а не только в `/notices`. Баннер предупреждает заранее — снятие
    сессионного выбора необратимо."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = _app_for_session(tmp_path, base_cfg)
    async with app.run_test() as pilot:
        app.set_session_override("profile", "home")
        scr = await _profiles_screen(pilot, app, base_cfg, on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert "Сессионный выбор /profiles" in banner
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    assert "сессионный выбор" in flash and "home" in flash


async def test_delete_flash_names_the_twin_that_surfaced(tmp_path, monkeypatch):
    """Удалён победивший тёзка — строка с тем же именем остаётся в списке (из
    второго корня). Молчать об этом нельзя: экран читался бы как «удаление не
    сработало»."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(project_dir / ".gc" / "profiles" / "work.yml", _PROFILE_BODY)
    shadowed = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    assert str(shadowed) in flash
    assert "work" in flash


# --- ревью Task 5 #6/#7/#8: гейт Del, глагол отказа, бэкапы в одну секунду --------


async def test_delete_refuses_on_the_package_template_before_the_banner(tmp_path, monkeypatch):
    """`Del` отказывает НА ВХОДЕ, как `r`: поднимать вопрос, на который заведомо
    ответишь отказом, — ложное обещание. Профиль в шаблоне подкладываем сами —
    важен гейт, а не содержимое установленного пакета."""
    from gc_core.config import _default_config_path
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    template = Path(_default_config_path())
    monkeypatch.setattr("gc_core.config_modules.profiles.profile_roots",
                        lambda project, base: [project_dir / ".gc" / "profiles",
                                               tmp_path / "pkg-profiles"])
    _write(tmp_path / "pkg-profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, template)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        assert not scr.query(".set-confirm")
        assert "не удалено" in _notice(scr)
    assert (tmp_path / "pkg-profiles" / "home.yml").is_file()


async def test_refusal_names_the_action_that_did_not_happen(tmp_path, monkeypatch):
    """Отказ говорит о своём действии: «не создано», а не «не сохранено» — глагол
    чужого действия заставляет гадать, что именно не произошло."""
    from gc_core.config import _default_config_path
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, Path(_default_config_path()))
        await pilot.press("n")
        await pilot.pause()
        notice = _notice(scr)
        assert "не создано" in notice
        assert "не сохранено" not in notice
        assert "каталог профилей" in notice     # объект отказа — тот, что недоступен


async def test_two_deletes_in_the_same_second_keep_both_backups(tmp_path, monkeypatch):
    """Бэкап — единственная копия удалённого файла (в отличие от `config_sync`, где
    оригинал остаётся на месте): совпадение секунды не смеет затирать предыдущий."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profiles = tmp_path / "base" / "profiles"
    monkeypatch.setattr("time.strftime", lambda *_a, **_k: "20260811-000000")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        for body in ("models: {}\n", "slots: {}\n"):
            _write(profiles / "home.yml", body)
            scr._render_view()
            await pilot.pause()
            await _put_cursor(pilot, scr, "profile", "home")
            await pilot.press("delete")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

    backups = sorted((profiles / "backups").glob("home.yml.bak-*"))
    assert len(backups) == 2
    assert {b.read_text(encoding="utf-8") for b in backups} == {"models: {}\n", "slots: {}\n"}


# --- ре-ревью: гейт по СЛОЯМ доведён до двери, объект отказа назван верно --------


def _readonly_base_layer(tmp_path, monkeypatch):
    """Корень профилей писуч (профиль проектный), а слой указателя — нет: базовый
    конфиг объявлен пакетным шаблоном. Ровно та развилка, где гейт «только по
    корню» пропускает к двери операцию, которую сам же отвергнет."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    _write(project_dir / ".gc" / "profiles" / "work.yml", _PROFILE_BODY)
    monkeypatch.setattr("gc_core.config._default_config_path", lambda: base_cfg)
    return base_cfg


async def test_delete_refuses_at_the_door_when_a_pointer_layer_is_read_only(tmp_path, monkeypatch):
    """Контракт #6 целиком: отказ на входе, а не после Enter. Баннер, на который
    заведомо ответишь отказом, — то же ложное обещание, что и футер."""
    from gc_tui import app as app_mod
    base_cfg = _readonly_base_layer(tmp_path, monkeypatch)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        assert not scr.query(".set-confirm")
        notice = _notice(scr)
        assert "не удалено" in notice
        assert str(base_cfg) in notice          # назван файл конфига, а не каталог
        assert "каталог профилей" not in notice
    assert (tmp_path / "project" / ".gc" / "profiles" / "work.yml").is_file()


async def test_rename_refuses_at_the_door_when_a_pointer_layer_is_read_only(tmp_path, monkeypatch):
    """То же у второй двери: поле ввода имени не раскрывается — набирать имя,
    которое некуда записать, человек не должен."""
    from gc_tui import app as app_mod
    base_cfg = _readonly_base_layer(tmp_path, monkeypatch)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        assert not scr.query("#set-profile-name")
        notice = _notice(scr)
        assert "не переименовано" in notice
        assert str(base_cfg) in notice


# --- сбой файловой системы при записи указателя — честный текст про файл ---------
#
# Дверь (`_profile_write_block`) сторожит пакетный шаблон, а не права на файл:
# конфиг, который нельзя записать, проходит до самой записи указателя и падает
# там `OSError`. Переименование и удаление заворачивали ЛЮБУЮ ошибку записи в
# отказ фазы 2 «конфиг стал бы нерабочим — поправьте в разделе «Модели»» — и
# отправляли чинить реестр там, где не хватает прав на файл.


def _lock_against_writing(path: Path):
    """Сделать `path` недоступным для записи и вернуть функцию, снимающую замок.

    Файл — только чтение (на Windows именно это валит `os.replace` временного файла
    на место цели); каталог — тоже (на POSIX замена файла в каталоге разрешена
    правами каталога, а не файла). Ни один из двух приёмов не вредит другому."""
    import os
    import stat
    file_mode = path.stat().st_mode
    dir_mode = path.parent.stat().st_mode
    os.chmod(path, stat.S_IREAD)
    os.chmod(path.parent, stat.S_IREAD | stat.S_IEXEC)

    def unlock() -> None:
        os.chmod(path.parent, dir_mode)
        os.chmod(path, file_mode)

    return unlock


def _assert_pointer_write_fails_with_oserror(base_cfg: Path) -> None:
    """Предпосылка теста, а не его предмет: писатель указателя на этой раскладке
    обязан получить именно `OSError`. Иначе тест прошёл бы мимо — проверял бы
    текст отказа, которого не случилось."""
    from gc_core import onboarding_config_writer as ocw
    with pytest.raises(OSError):
        ocw.write_text(base_cfg, base_cfg.read_text(encoding="utf-8"))


async def test_rename_reports_a_file_error_as_a_file_error(tmp_path, monkeypatch):
    """PermissionError на записи указателя — «не удалось переименовать: …» (так было
    на базе), а не совет поправить раздел «Модели»: конфиг нерабочим не становился,
    ему просто не хватило прав."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    unlock = _lock_against_writing(base_cfg)
    try:
        _assert_pointer_write_fails_with_oserror(base_cfg)
        app = app_mod.TuiApp(config=_base_dict())
        async with app.run_test() as pilot:
            scr = await _profiles_screen(pilot, app, base_cfg)
            await _put_cursor(pilot, scr, "profile", "work")
            await pilot.press("r")
            await pilot.pause()
            await _type_name(pilot, scr, "work2")
            notice = _notice(scr)
    finally:
        unlock()
    assert "✗ не удалось переименовать:" in notice
    assert "нерабочим" not in notice
    assert "«Модели»" not in notice
    assert profile.is_file() and not profile.with_name("work2.yml").exists()


async def test_delete_reports_a_file_error_as_a_file_error(tmp_path, monkeypatch):
    """То же у удаления: снять указатель не дали права на файл — «не удалось
    удалить: …», файл профиля на месте, бэкапа нет."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    profile = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    unlock = _lock_against_writing(base_cfg)
    try:
        _assert_pointer_write_fails_with_oserror(base_cfg)
        app = app_mod.TuiApp(config=_base_dict())
        async with app.run_test() as pilot:
            scr = await _profiles_screen(pilot, app, base_cfg)
            await _put_cursor(pilot, scr, "profile", "work")
            await pilot.press("delete")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            notice = _notice(scr)
    finally:
        unlock()
    assert "✗ не удалось удалить:" in notice
    assert "нерабочим" not in notice
    assert "«Модели»" not in notice
    assert profile.is_file() and not (profile.parent / "backups").exists()


# --- ре-ревью: хвост «применится со следующего хода» — только когда применится ---


async def test_rename_of_a_pointerless_profile_promises_nothing(tmp_path, monkeypatch):
    """Переименован профиль, на который не указывает ни один слой и который не
    выбран сессией: для агента не изменилось НИЧЕГО. «Применится со следующего
    хода агента» здесь — та же ложь, за которую переписывались футеры."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "solo.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "solo")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "solo2")
        flash = _flash(scr)

    assert "переименован" in flash
    assert "применится" not in flash


async def test_delete_of_a_pointerless_profile_promises_nothing(tmp_path, monkeypatch):
    """Та же проверка у удаления: указателя не было — применяться нечему."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "solo.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "solo")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    assert "удалён" in flash
    assert "применится" not in flash


async def test_delete_of_the_active_profile_still_promises_the_next_turn(tmp_path, monkeypatch):
    """Обратная сторона того же правила: действующий указатель СНЯТ — обещание
    про следующий ход правдиво и обязано остаться."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    # Ревью MR #80 (L24): у удаления «применится …» без предмета читалось как
    # «применится тень» — снятый указатель называется снятием выбора, обещание
    # про следующий ход остаётся.
    assert "Выбор снят: со следующего хода агента — без профиля." in flash


async def test_delete_of_the_active_twin_says_the_choice_is_dropped_not_the_shadow(
        tmp_path, monkeypatch):
    """Ревью MR #80 (L24): удалён победивший тёзка АКТИВНОГО имени. Плашка обязана
    (1) сказать, что выбор снят — раньше говорил только баннер; (2) развести
    снятие с резолвингом: хвост «применится со следующего хода агента» ПОСЛЕ
    строки о тени читался как «применится тень», хотя применится «без профиля»."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    _write(project_dir / ".gc" / "profiles" / "work.yml", _PROFILE_BODY)  # победитель
    shadowed = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

    assert "Выбор снят: со следующего хода агента — без профиля." in flash, flash
    assert str(shadowed) in flash and "включить — Space" in flash, flash
    # Снятие выбора стоит ДО строки о тёзке — иначе хвост снова прирастает к тени.
    assert flash.index("Выбор снят") < flash.index("резолвится"), flash
    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


# --- ревью MR #80 (L23): профиль закреплён managed-слоем — баннеры не молчат -------
#
# Указатель в `config.local.yml` (managed) правило записи осознанно не переписывает
# (`_profile_pointer_layers` слой не знает — чужой machine-managed файл). Но
# удаление/переименование под таким указателем оставляет его висеть на
# исчезнувшем имени: следующий старт не собирается. Правило записи НЕ расширяется
# — операция предупреждает на входе и называет файл-носитель (тем же приёмом, что
# лестница починки: `resolve_profile_pointer` с кандидатами + `config_origin.
# origins`, см. `onboarding.load_config_for_repair`).


async def test_delete_banner_names_the_managed_pointer_it_will_not_touch(
        tmp_path, monkeypatch):
    """Managed `config.local.yml` c `profile: "work"`: баннер удаления обязан
    назвать файл-носитель — раньше он был голым «Файл уедет в backups/», и после
    Enter managed продолжал называть исчезнувший профиль. Сторож от пере-срабатывания:
    у профиля, которого managed не называет, строки нет."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    managed = _write(tmp_path / "config.local.yml", 'profile: "work"\n')
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "free.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "free")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        assert str(managed) not in banner, banner       # не закреплён — молчим
        await pilot.press("escape")
        await pilot.pause()

        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))

    assert str(managed) in banner, banner
    assert "следующий запуск не соберётся" in banner, banner


async def test_rename_prompt_names_the_managed_pointer_it_will_not_touch(
        tmp_path, monkeypatch):
    """Симметрия на переименовании: указатель в managed остаётся на СТАРОМ имени —
    предупреждение стоит у поля ввода, до Enter."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    managed = _write(tmp_path / "config.local.yml", 'profile: "work"\n')
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        texts = " ".join(str(w.render()) for w in scr.query(Static))

    assert str(managed) in texts, texts
    assert "следующий запуск не соберётся" in texts, texts


async def test_pinned_elsewhere_reads_the_pass_cache_not_the_disk_again(
        tmp_path, monkeypatch):
    """Ревью #11: `_profile_pointer_pinned_elsewhere` перечитывала оба слоя
    (`config_raw.read_document`) с диска заново, хотя `_profile_pointer_layers`
    секундой раньше уже прочитал те же файлы в кэш прохода (`_base_config_doc`/
    `_project_config_doc`, тот же `_profiles_disk_cache`, что у сводок профилей
    и происхождения реестра). Баннер удаления/переименования зовёт обе функции
    подряд — вопрос о закреплённом указателе не обязан читать config.yml с диска
    ещё раз в том же проходе."""
    from gc_core import config_raw
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        reads: list = []
        real = config_raw.read_document
        monkeypatch.setattr(config_raw, "read_document",
                            lambda path: reads.append(Path(path).resolve()) or real(path))

        scr._profiles_disk_cache = {}          # начало прохода: кэш сброшен
        base_path = base_cfg.resolve()
        layers = scr._profile_pointer_layers("work")
        after_layers = reads.count(base_path)
        assert after_layers >= 1, "первое обращение к слоям обязано прочитать базу"

        scr._profile_pointer_pinned_elsewhere("work", layers)
        assert reads.count(base_path) == after_layers, (
            "повторный вопрос о закреплённом указателе прочитал базу с диска "
            "снова, хотя тот же проход уже держит её в кэше")


# --- флаг запуска --profile: плашки не обещают того, что флаг не даст ---------------
#
# Флаг переживает перечитывание (`TuiApp._profile_override`), и записанный
# указатель применится лишь со следующего запуска без флага — как и сессионный
# выбор, флаг сильнее файла. «Применится со следующего хода агента» здесь — ложь
# того же рода, за которую переписывались футеры; плашка называет, на чём
# остаётся ТЕКУЩИЙ ЗАПУСК.


def _app_with_flag(tmp_path, base_cfg, profile_override):
    from gc_core.config import load_config_ex
    from gc_tui import app as app_mod
    cfg, _meta = load_config_ex(str(base_cfg), profile_override=profile_override)
    app = app_mod.TuiApp(threads_dir=str(tmp_path / "threads"), system="cli", config=cfg,
                         profile_override=profile_override)
    app.set_config_path(str(base_cfg))
    return app


async def test_activation_under_the_profile_flag_names_the_launch_profile(tmp_path, monkeypatch):
    """`gc-ua --profile work`, указатель файла пуст; Space на `corp` пишет указатель,
    а запуск остаётся на `work` — плашка обязана сказать именно это."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "corp.yml", _PROFILE_BODY)

    app = _app_with_flag(tmp_path, base_cfg, "work")
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=app._config,
                                     on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "corp")
        await pilot.press("space")
        await pilot.pause()
        flash = _flash(scr)
        assert app._config["profile"] == "work"          # запуск остался на флаге

    assert 'profile: "corp"' in base_cfg.read_text(encoding="utf-8")
    assert "текущий запуск остаётся на «work» (флаг --profile)" in flash
    assert "применится со следующего хода" not in flash


async def test_activation_under_an_empty_profile_flag_says_the_launch_stays_bare(
        tmp_path, monkeypatch):
    """`gc-ua --profile ""` — обход сломанного профиля: починили, включили Space —
    указатель записан, но этот запуск профиля не поднимет."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = _app_with_flag(tmp_path, base_cfg, "")
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=app._config,
                                     on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("space")
        await pilot.pause()
        flash = _flash(scr)
        assert app._config["profile"] == ""

    assert 'profile: "work"' in base_cfg.read_text(encoding="utf-8")
    assert 'текущий запуск остаётся без профиля (флаг --profile "")' in flash
    assert "применится со следующего хода" not in flash


async def test_rename_of_the_flag_profile_moves_the_flag_with_it(tmp_path, monkeypatch):
    """Переименован профиль, выбранный флагом: флаг едет за именем — как сессионный
    выбор (ruling координатора), — иначе следующее перечитывание не нашло бы профиль
    флага и отказалось бы собирать базу, оставив окно на прежней («не перечитан»)."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = _app_with_flag(tmp_path, base_cfg, "work")
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=app._config,
                                     on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        flash = _flash(scr)

        assert app._profile_override == "job"
        assert app._config["profile"] == "job"
        assert "profile_flag_stale" not in [n.code for n in collect_runtime_notices()]
    assert "переименован" in flash
    assert "не перечитан" not in _notice(scr)


async def test_delete_of_the_flag_profile_clears_the_flag(tmp_path, monkeypatch):
    """Удалён профиль, выбранный флагом: флаг снимается в `""` («этот запуск без
    профиля» — раскладку уже понимает `_load_base_config`), как у переименования
    он едет за именем. Иначе флаг продолжает называть исчезнувший файл, и КАЖДОЕ
    перечитывание до конца сессии падает «профиль не найден»: вместо плашки с
    путём бэкапа — «удалено, но конфиг не перечитан», любое сохранение настроек —
    «записано, но конфиг не перечитан». Баннер обязан назвать это следствие
    заранее — его докстринг обещает ВСЕ необратимые последствия."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    app = _app_with_flag(tmp_path, base_cfg, "work")
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=app._config,
                                     on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        banner = " ".join(str(w.render()) for w in scr.query(".set-confirm"))
        await pilot.press("enter")
        await pilot.pause()
        flash = _flash(scr)

        assert app._profile_override == ""
        assert (app._config.get("profile") or None) is None
        assert "profile_flag_stale" not in [n.code for n in collect_runtime_notices()]
    assert "без профиля" in banner, banner       # следствие для флага названо заранее
    assert "бэкап" in flash, flash               # плашка удаления, а не «не перечитан»
    assert "не перечитан" not in _notice(scr)


async def test_delete_of_the_flag_profile_with_a_surfaced_twin_names_the_original_flag(
        tmp_path, monkeypatch):
    """Ревью #10: удаление профиля флага пишет `_profile_override = ""` — раскладку
    для перечитки менять нельзя (докстринг `_launch_profile_effect`/удаления), но
    ТЕКСТ эффекта путает её с настоящим `--profile ""`. Тёзка того же имени жив в
    другом корне — «включить — Space» на нём не должно утверждать, что запуск
    стартовал с пустым флагом: флаг называл «work», это удаление его лишь сняло."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    _write(project_dir / ".gc" / "profiles" / "work.yml", _PROFILE_BODY)   # победитель
    _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)    # тёзка

    app = _app_with_flag(tmp_path, base_cfg, "work")
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=app._config,
                                     on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app._profile_override == ""              # раскладка перечитки не меняется

        await _put_cursor(pilot, scr, "profile", "work")   # тот же тёзка, «включить — Space»
        await pilot.press("space")
        await pilot.pause()
        flash = _flash(scr)

    assert 'флаг --profile ""' not in flash, flash
    assert "профиль «work» из флага --profile удалён" in flash, flash


# --- ре-ревью: сбой персиста при переезде сессионного выбора ----------------------


async def test_rename_survives_a_failed_session_persist(tmp_path, monkeypatch):
    """meta треда недоступна (диск полон, сетевой путь отвалился): переезд
    сессионного выбора откатывается САМ (`apply_session_override` атомарен), файл
    при этом уже переименован — и штатная санация §9.2 снимает осиротевший
    оверрайд с notice. Операция не падает и не врёт."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = _app_for_session(tmp_path, base_cfg)
    async with app.run_test() as pilot:
        app.set_session_override("profile", "home")

        def _boom():
            raise OSError("каталог тредов недоступен")
        monkeypatch.setattr(app, "persist_session_overrides", _boom)

        scr = await _profiles_screen(pilot, app, base_cfg, on_apply=app.reload_config)
        await _put_cursor(pilot, scr, "profile", "home")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "house")

        assert (tmp_path / "base" / "profiles" / "house.yml").is_file()
        assert "profile" not in app._session_overrides          # откат + санация
        assert "session_profile_stale" in [n.code for n in collect_runtime_notices()]


# --- Имя профиля: коллизия по регистру ------------------------------------------


async def test_rename_into_a_name_differing_only_in_case_is_refused(tmp_path, monkeypatch):
    """`testprof2` → `TestProf1` при живом `testprof1` — отказ по имени, а не крэш
    файловой системы.

    Проверка тёзки сравнивала имена ПОБУКВЕННО, а Windows-ФС регистр не различает:
    `TestProf1` проходил гейт, а `rename` падал `[WinError 183]` — человек получал
    в лицо путь и номер ошибки вместо ответа про имя. Хуже того, указатели во всех
    слоях к этому моменту уже переписаны и их приходится откатывать.

    Отвергаем на ВСЕХ платформах — тем же правилом, что и запрещённые символы
    Windows: профиль едет вместе с проектом, и на чужой ФС ломается там."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "testprof1.yml", "# ПЕРВЫЙ\n" + _PROFILE_BODY)
    _write(tmp_path / "base" / "profiles" / "testprof2.yml", "# ВТОРОЙ\n" + _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "testprof2")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "TestProf1")

        notice = _notice(scr)
        assert "уже существует" in notice, notice
        assert "WinError" not in notice and "Errno" not in notice
        # оба файла целы и на своих местах
        root = tmp_path / "base" / "profiles"
        assert sorted(p.name for p in root.iterdir()) == ["testprof1.yml", "testprof2.yml"]
        assert "ПЕРВЫЙ" in (root / "testprof1.yml").read_text(encoding="utf-8")


async def test_create_with_a_name_differing_only_in_case_is_refused(tmp_path, monkeypatch):
    """То же правило на входе создания: `create_profile_file` отказ даёт, но
    техническим текстом про существующий файл — про ИМЯ отвечает гейт."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "testprof1.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "TESTPROF1")

        notice = _notice(scr)
        # Про ИМЯ, а не про файл: гейт обязан ответить раньше писателя, который
        # показывает полный путь и «файл профиля уже существует».
        assert "профиль «testprof1» уже существует" in notice, notice
        assert ".yml" not in notice


async def test_renaming_a_profile_to_its_own_name_in_another_case_still_works(
        tmp_path, monkeypatch):
    """Сторож от чрезмерного запрета: сменить РЕГИСТР СВОЕГО имени можно —
    тёзка здесь сам себе, и файловая система такое переименование принимает."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "testprof1.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "testprof1")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "TestProf1")

        assert "уже существует" not in _notice(scr)
        assert (tmp_path / "base" / "profiles" / "TestProf1.yml").is_file()


def _spy_repoint(monkeypatch) -> list:
    """Записать вызовы `_repoint_profile_layers`, НЕ подменяя поведение: гейт
    занятой цели обязан стоять ДО первой записи указателей — по конечному
    состоянию файлов этого не видно (откат возвращает те же байты)."""
    calls = []
    real = s.SettingsScreen._repoint_profile_layers

    def _recording(self, layers, value, **kwargs):
        calls.append(value)
        return real(self, layers, value, **kwargs)

    monkeypatch.setattr(s.SettingsScreen, "_repoint_profile_layers", _recording)
    return calls


async def test_renaming_onto_a_file_discovery_does_not_see_is_refused_before_pointers(
        tmp_path, monkeypatch):
    """Ревью MR #80 (H1): гейт тёзки судил по именам ДИСКАВЕРИ, а `rename()`
    работает с ПУТЯМИ. `default.yml` — резервное имя, дискавери его пропускает,
    casefold-гейт молчит — и на регистронечувствительной ФС цель «Default.yml»
    попадает в чужой `default.yml`: POSIX-rename перезаписал бы его молча и без
    бэкапа, а указатели `profile:` успевали переписаться до отказа ФС даже там,
    где она отказывает сама (Windows). Судим файловую систему, ДО любых записей."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    root = tmp_path / "base" / "profiles"
    _write(root / "work.yml", _PROFILE_BODY)
    foreign = _write(root / "default.yml", "# ФАЙЛ ПОЛЬЗОВАТЕЛЯ — дискавери его не видит\n")
    if not (root / "DEFAULT.YML").exists():
        pytest.skip("регистрочувствительная ФС: «Default.yml» в default.yml тут не попадает")
    base_before = base_cfg.read_bytes()
    foreign_before = foreign.read_bytes()
    calls = _spy_repoint(monkeypatch)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "Default")
        notice = _notice(scr)

    assert "не переименовано" in notice and "уже занят" in notice, notice
    assert calls == [], calls                       # указатели не тронуты ВОВСЕ
    assert base_cfg.read_bytes() == base_before
    assert foreign.read_bytes() == foreign_before   # чужой файл цел байт-в-байт
    assert (root / "work.yml").is_file()


async def test_renaming_onto_a_directory_namesake_is_refused_before_pointers(
        tmp_path, monkeypatch):
    """Тот же гейт на регистрочувствительной ФС: каталог-тёзка `<имя>.yml` для
    дискавери невидим (`is_file()`), casefold-гейт молчит — а без гейта указатели
    переписывались бы и откатывались, отказ приходил бы от ФС уже ПОСЛЕ них."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("work"))
    root = tmp_path / "base" / "profiles"
    _write(root / "work.yml", _PROFILE_BODY)
    occupant = _write(root / "job.yml" / "данные.txt", "не профиль\n")
    base_before = base_cfg.read_bytes()
    calls = _spy_repoint(monkeypatch)

    cfg = dict(_base_dict(), profile="work")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        notice = _notice(scr)

    assert "не переименовано" in notice and "уже занят" in notice, notice
    assert calls == [], calls                       # гейт стоит ДО _repoint_profile_layers
    assert base_cfg.read_bytes() == base_before
    assert occupant.read_text(encoding="utf-8") == "не профиль\n"
    assert (root / "work.yml").is_file()


async def test_name_prompt_hides_the_hint_about_the_row_under_cursor(tmp_path, monkeypatch):
    """Пока идёт ввод имени, подсказка под списком молчит.

    Она рассказывает про строку ПОД КУРСОРОМ («Space — активировать», путь файла),
    но клавиши в этот момент забрал инпут: Space печатается, а не активирует. Текст
    обещал недоступное и вдобавок стоял между полем ввода и ответом на него —
    отказ уезжал на пять строк вниз. Футер про режим ввода знает и говорит своё
    («Enter — переименовать · Esc — отмена»), подсказке добавить нечего."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "home")
        hints = [str(w.render()) for w in scr.query(".set-profiles-hint")]
        assert any("активировать" in h for h in hints)     # до ввода — на месте

        await pilot.press("r")
        await pilot.pause()

        hints = [str(w.render()) for w in scr.query(".set-profiles-hint")]
        assert hints == [], hints
        assert "переименовать" in _footer(scr)             # роль клавиш называет футер


async def test_renaming_reports_a_clean_refusal_when_the_profile_file_vanishes_mid_read(
        tmp_path, monkeypatch):
    """Находка ревью: чтение файла профиля для будущего документа-кандидата
    (`_rename_profile`, готовит `extra_candidates`) не было обёрнуто, в отличие
    от каждого другого файлового доступа этой функции (`rename()` ниже, запись
    указателя) — гонка «файл исчез между проверкой строки и этим чтением» раньше
    приводила бы к необработанному исключению вместо нотиса, хотя календарное
    окно гонки не расширилось. Ничего ещё не записано — ждём чистый отказ, а
    не краш."""
    import pathlib

    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    work = _write(tmp_path / "base" / "profiles" / "work.yml", _PROFILE_BODY)

    real_read_text = pathlib.Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self.name == "work.yml":
            raise OSError("файл профиля исчез")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", _flaky_read_text)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        notice = _notice(scr)

    assert "не переименовано" in notice, notice
    assert "исчез" in notice, notice
    assert work.exists()                                     # ничего не тронуто
    assert not (tmp_path / "base" / "profiles" / "job.yml").exists()


# --- нечитаемый файл профиля: запись указателя обязана отказать -------------------


def _write_utf16(path: Path, text: str) -> Path:
    """Профиль, сохранённый «блокнотом» в UTF-16: синтаксис безупречен, а
    прочитать файл нельзя вовсе. Живая раскладка смока, а не выдуманная."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-16"))
    return path


def _loadable_base_text(profile: str = "") -> str:
    """База, которая ГРУЗИТСЯ сама по себе: одна модель и заданный `slots.default`.
    Иначе отказ активации нечем отличить от давнего «2+ моделей без слотов»."""
    return (_base_config_text(profile)
            + 'models:\n  a:\n    model: "x"\nslots:\n  default: "a"\n')


async def test_activating_an_unreadable_profile_is_refused_and_names_the_file(
        tmp_path, monkeypatch):
    """ЖИВОЙ СМОК ВЛАДЕЛЬЦА: рядом с базой лежит профиль в UTF-16, человек жмёт
    Space. Раньше запись указателя ПРОХОДИЛА (сборка стека падала, а гейт читал
    её отказ как «проверять нечего»), и следующий `gc-ua` не поднимался вовсе —
    выйти можно было только флагом `--profile ""`. Приложение не вправе
    активировать профиль, с которым само же не стартует."""
    from gc_core import config as config_mod
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _loadable_base_text())
    before = base_cfg.read_text(encoding="utf-8")
    bad = _write_utf16(tmp_path / "base" / "profiles" / "broken-enc.yml",
                 'slots:\n  default: "a"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        await _put_cursor(pilot, scr, "profile", "broken-enc")
        await pilot.press("space")
        await pilot.pause()
        notice = _notice(scr)

    assert "профиль не включён" in notice, notice
    assert "не удалось прочитать" in notice, notice      # причина названа
    # Адрес починки называется ТОЛЬКО там, где он верен: файл в чужой кодировке
    # правят не в разделе «Модели», и звать туда значит послать не по адресу.
    assert "разделе «Модели»" not in notice, notice
    # Путь и причину витрина уже печатает для профиля под курсором — отказ,
    # повторяя обе, давал их на экране трижды подряд.
    # Имя файла человек уже видит в блоке под курсором (`_profiles_row_hint`):
    # витрина печатает путь первой строкой, причину — второй.
    assert notice.count(str(bad)) == 0, notice
    assert str(bad) in scr._profiles_hint_text(scr._profiles_rows()), "путь остался в витрине"
    assert notice.count("не удалось прочитать") == 1, notice
    assert base_cfg.read_text(encoding="utf-8") == before   # указатель никуда не уехал
    config_mod.load_config_ex(str(base_cfg))                # следующий старт по-прежнему жив


async def test_deactivating_an_already_unreadable_profile_still_works(
        tmp_path, monkeypatch):
    """Выход из ловушки той же дверью, что и вход: указатель на нечитаемый профиль
    уже записан (руками, прежней версией, чужой машиной) — снятие обязано
    пройти. Гейт, отказывающий по одному лишь факту «стек не собрался», запер бы
    человека внутри приложения ровно так же, как дефект запирал снаружи."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _loadable_base_text("broken-enc"))
    _write_utf16(tmp_path / "base" / "profiles" / "broken-enc.yml",
                 'slots:\n  default: "a"\n')

    cfg = dict(_base_dict(), profile="broken-enc")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "none")
        await pilot.press("space")
        await pilot.pause()
        notice = _notice(scr)

    assert notice == "", notice
    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


async def test_deleting_an_already_unreadable_profile_still_works(tmp_path, monkeypatch):
    """Вторая дверь наружу из той же ловушки: удаление снимает указатель (пишет
    `profile: ""`) и уносит файл в бэкап — стек оно ЧИНИТ, а не ломает."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _loadable_base_text("broken-enc"))
    broken = _write_utf16(tmp_path / "base" / "profiles" / "broken-enc.yml",
                          'slots:\n  default: "a"\n')

    cfg = dict(_base_dict(), profile="broken-enc")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "broken-enc")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        notice = _notice(scr)

    assert notice == "", notice
    assert not broken.exists()
    assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


async def test_creating_a_profile_beside_an_unreadable_one_is_untouched(
        tmp_path, monkeypatch):
    """Создание указателя не пишет вовсе — соседний нечитаемый профиль (даже
    активный) ему не помеха. Сторож против «починки» гейта запретом всего
    подряд: запереть человека без единственной кнопки «завести рабочий профиль»
    было бы той же ловушкой с другой стороны."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _loadable_base_text("broken-enc"))
    _write_utf16(tmp_path / "base" / "profiles" / "broken-enc.yml",
                 'slots:\n  default: "a"\n')

    cfg = dict(_base_dict(), profile="broken-enc")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "add")
        await pilot.press("n")
        await pilot.pause()
        await _type_name(pilot, scr, "fresh")
        notice = _notice(scr)

    assert notice == "", notice
    assert (tmp_path / "base" / "profiles" / "fresh.yml").is_file()
    assert 'profile: "broken-enc"' in base_cfg.read_text(encoding="utf-8")   # указатель не трогали


async def test_renaming_an_unreadable_profile_is_refused_and_names_the_file(
        tmp_path, monkeypatch):
    """Переименование читает файл профиля само (готовит документ-кандидат на его
    будущий путь) и отказывает ещё до записи указателей. Отказ обязан называть
    ФАЙЛ: голая жалоба кодека («'utf-8' codec can't decode byte 0xff») не
    говорит человеку, какой из профилей чинить."""
    from gc_tui import app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _loadable_base_text("broken-enc"))
    before = base_cfg.read_text(encoding="utf-8")
    broken = _write_utf16(tmp_path / "base" / "profiles" / "broken-enc.yml",
                          'slots:\n  default: "a"\n')

    cfg = dict(_base_dict(), profile="broken-enc")
    app = app_mod.TuiApp(config=cfg)
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=cfg)
        await _put_cursor(pilot, scr, "profile", "broken-enc")
        await pilot.press("r")
        await pilot.pause()
        await _type_name(pilot, scr, "job")
        notice = _notice(scr)

    assert "не переименовано" in notice, notice
    assert "broken-enc.yml" in notice, notice
    assert broken.exists()                                  # файл не тронут
    assert not (tmp_path / "base" / "profiles" / "job.yml").exists()
    assert base_cfg.read_text(encoding="utf-8") == before   # и указатель тоже

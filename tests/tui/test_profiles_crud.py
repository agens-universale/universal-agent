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

from textual.widgets import Input, Static

from runtime_notices import clear_runtime_notices, collect_runtime_notices
from tests.tui.conftest import open_settings_section
from tui.screens import settings as s


@pytest.fixture(autouse=True)
def _no_managed_overlay(tmp_path, monkeypatch):
    """Без этого активация/происхождение читают НАСТОЯЩИЙ ~/.gc/config.local.yml
    разработчика (тот же приём, что в tests/test_config_origin.py)."""
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed-overlay-here.yml",
    )


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
    from model_picker import ModelInfo
    from tui.services.settings_probe import ProbeResult
    return ProbeResult("ok", 200, [ModelInfo(id="test-model")], 118, "доступен")


def _mk_screen(config=None, config_path=None, on_apply=None):
    from tui.screens.settings import SettingsScreen
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
    import tui.app as app_mod
    from config_modules.profiles import PROFILE_SKELETON
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
    import tui.app as app_mod
    from config_modules.profiles import validate_profile_text
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
    from config import _default_config_path
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
    import tui.app as app_mod
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


async def test_rename_of_an_inactive_profile_leaves_the_pointer_alone(tmp_path, monkeypatch):
    """Неактивный профиль указателем не назван — переименование его не трогает."""
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    """`r`/`Del` работают по объекту под курсором: на «— без профиля» и на строке
    уровня объекта нет — клавиши молчат (ввод имени не поднимается)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", _PROFILE_BODY)

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg)
        for kind in ("none", "level"):
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
    from config import load_config_ex
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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


async def test_level_row_footer_still_names_creating(tmp_path, monkeypatch):
    """Пустое состояние: курсор стартует на строке «+ добавить профиль…» — её
    футер обещает создание и НЕ обещает клавиш объектов (Space/r/Del на
    строке-действии не работают: канон «недоступное не обещаем»). Ниже, на
    строке уровня, футер прежний — с ←/→/Enter и `n`: создание первого профиля
    видно с обеих строк пустого состояния."""
    import tui.app as app_mod
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
        await pilot.press("down")                   # → строка уровня
        await pilot.pause()
        footer = _footer(scr)
        assert "n — создать" in footer
        assert "←/→/Enter — переключить" in footer


async def test_name_input_footer_is_positional(tmp_path, monkeypatch):
    """Контракт п.5: на время ввода имени футер — про ввод (Enter/Esc), а глагол
    называет ровно то, что произойдёт: создание или переименование."""
    import tui.app as app_mod
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
    from config import load_config_ex
    monkeypatch.chdir(tmp_path)
    cfg, _meta = load_config_ex(str(base_cfg))
    return cfg


async def test_delete_clears_the_pointer_in_every_file_layer(tmp_path, monkeypatch):
    """Репро ревью дословно: Del на `work`, которого называет ТОЛЬКО базовый слой
    (в merged победил проектный `corp`). Указатель обязан сняться и там — иначе
    запуск вне проекта падает «профиль work не найден» (§3.1)."""
    import tui.app as app_mod
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
    import tui.app as app_mod
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


# --- ревью Task 5 #2: переименование ПЕРЕВОЗИТ сессионный выбор -------------------


async def test_rename_moves_the_session_choice_instead_of_dropping_it(tmp_path, monkeypatch):
    """Переименование — не исчезновение: сессионный выбор `/profiles` едет за новым
    именем (ruling координатора), а не снимается санацией §9.2. Переезд доходит и
    до meta треда — иначе возврат в тред воскресил бы старое имя."""
    clear_runtime_notices()
    from tui.services.stores.file_store import FileTaskStore
    from tui.session_settings import read_from_meta
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
    import tui.app as app_mod
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
    import tui.app as app_mod
    from config import _default_config_path
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    template = Path(_default_config_path())
    monkeypatch.setattr("config_modules.profiles.profile_roots",
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
    import tui.app as app_mod
    from config import _default_config_path
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
    import tui.app as app_mod
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
    monkeypatch.setattr("config._default_config_path", lambda: base_cfg)
    return base_cfg


async def test_delete_refuses_at_the_door_when_a_pointer_layer_is_read_only(tmp_path, monkeypatch):
    """Контракт #6 целиком: отказ на входе, а не после Enter. Баннер, на который
    заведомо ответишь отказом, — то же ложное обещание, что и футер."""
    import tui.app as app_mod
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
    import tui.app as app_mod
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


# --- ре-ревью: хвост «применится со следующего хода» — только когда применится ---


async def test_rename_of_a_pointerless_profile_promises_nothing(tmp_path, monkeypatch):
    """Переименован профиль, на который не указывает ни один слой и который не
    выбран сессией: для агента не изменилось НИЧЕГО. «Применится со следующего
    хода агента» здесь — та же ложь, за которую переписывались футеры."""
    import tui.app as app_mod
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
    import tui.app as app_mod
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
    import tui.app as app_mod
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

    assert "применится со следующего хода агента" in flash


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

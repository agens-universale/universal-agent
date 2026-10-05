"""Settings-витрина «Профили» (спека `docs/specs/2026-07-23-profiles-design.md`
§12.3/§12.3a): список обоих корней, тёзки, суффикс секций-оверлеев, единое
правило записи (активация), shadow-hints, value-origin слоя профиля, `/config`
и `/status`, онбординг-предупреждение, cold-start notice (§3.5).
"""

from gc_testkit.settings import make_settings
from gc_testkit.tui_support import as_settings, tui_settings
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import open_settings_section
from textual.widgets import Static


def _fake_probe(result=None):
    """Дефолт — УСПЕШНЫЙ вердикт, а не `None`: воркер проб моделей эндпоинта
    разыменовывает `result.models` (`tui/screens/settings.py`), поэтому `None`
    роняет не строку пробы, а весь экран через `WorkerFailed`. Эталон вердикта
    один на все settings-тесты — `_probe_ok` из `test_settings_screen`."""
    if result is None:
        from test_settings_screen import _probe_ok

        result = _probe_ok()

    def probe_fn(endpoint, auth, verify, model, **kw):
        return result

    return probe_fn


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _base_config_text(profile: str = "") -> str:
    """Материализованный базовый config.yml с живым якорем `profile:` (как несёт
    config.default.yml — спека §11)."""
    return (
        f'profile: "{profile}"\n'
        "level: default\n"
        "backend:\n"
        '  completions_url: "http://host/v1/chat/completions"\n'
        "agent:\n"
        '  model: "test-model"\n'
    )


def _mk_screen(config=None, config_path=None, on_apply=None):
    from gc_tui.screens.settings import SettingsScreen

    return SettingsScreen(
        settings=as_settings(config or {}),
        config_path=str(config_path) if config_path else None,
        on_apply=on_apply or (lambda: None),
        probe_fn=_fake_probe(),
    )


async def _open_profiles(pilot, screen):
    open_settings_section(screen, "Профили")
    await pilot.pause()


def _profiles_rows_text(screen) -> list[str]:
    return [str(w.render()) for w in screen.query(".set-profiles-row")]


# --- список профилей обоих корней: имя, источник, тёзка, суффикс «+security» ---


async def test_profiles_section_lists_both_roots_with_source(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        names = [r["name"] for r in rows if r["kind"] == "profile"]
        assert names == ["home", "work"]
        work = next(r for r in rows if r.get("name") == "work")
        home = next(r for r in rows if r.get("name") == "home")
        assert work["ref"].root_kind == "project"
        assert home["ref"].root_kind == "base"
        lines = _profiles_rows_text(scr)
        assert any("work" in l and "проектный" in l for l in lines)
        assert any("home" in l and "глобальный" in l for l in lines)


async def test_an_unreadable_profile_is_named_once(tmp_path, monkeypatch):
    """Отказ чтения профиля печатается ОДИН раз, а не вкладывается сам в себя.

    Ядро отдаёт самодостаточный текст «<путь>: не удалось прочитать: <причина>»,
    и витрина добавляла к нему свой префикс той же фразой — человек читал
    «не удалось прочитать: <путь>: не удалось прочитать: …». Случай живой:
    профиль в UTF-16 не участвует в загрузке, и до витрины его никто не проверял.
    """
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    bad = tmp_path / "base" / "profiles" / "enc.yml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes("slots:\n  default: x\n".encode("utf-16"))

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "profile", "enc")
        await pilot.pause()
        blob = (
            "\n".join(_profiles_rows_text(scr))
            + "\n"
            + "\n".join(str(w.render()) for w in scr.query("Static"))
        )
        assert "не удалось прочитать" in blob, blob
        assert blob.count("не удалось прочитать") == 1, blob
        # Путь стоит ПЕРВОЙ строкой блока — в строке причины он лишний: на экране
        # это читалось как повтор, а не как уточнение.
        assert blob.count(str(bad)) == 1, blob


def _base_dict() -> dict:
    return {
        "backend.completions_url": "http://host/v1/chat/completions",
        "agent.model": "test-model",
    }


async def test_profiles_section_tie_break_one_row_shadow_in_hint(
    tmp_path, monkeypatch
):
    """Тёзка (одинаковое имя в обоих корнях) — ОДНА строка, побеждает проектный;
    перекрытый (глобальный) файл — в hint выделенной строки (спека §3.1)."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    proj_profile = _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )
    base_profile = _write(
        tmp_path / "base" / "profiles" / "work.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        profile_rows = [r for r in rows if r["kind"] == "profile"]
        assert len(profile_rows) == 1
        assert profile_rows[0]["ref"].path == proj_profile
        assert profile_rows[0]["shadow"].path == base_profile
        scr._profiles_idx = next(
            i for i, r in enumerate(rows) if r["kind"] == "profile"
        )
        hint = scr._profiles_hint_text(rows)
        assert str(base_profile) in hint
        # Путь без объяснения оставлял человека гадать, кто кого перекрыл и правки в
        # каком из двух файлов что-то изменят: подсказка обязана СКАЗАТЬ, что этот
        # файл есть, но не работает (ревизия владельца по смоку).
        assert (
            f"тот же профиль есть здесь, но перекрыт: {base_profile}" in hint
        )


async def test_profiles_section_overlay_suffix_shows_plus_security(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\n'
        "slots:\n  default: flash\n"
        "security:\n  command_moderation:\n    enabled: true\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        lines = _profiles_rows_text(scr)
        assert any("+security" in l for l in lines)


async def test_profiles_section_slot_composition_shown_inline(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\n  glm:\n    url: http://y\n    token: ""\n'
        "slots:\n  default: glm\n  light: flash\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        lines = _profiles_rows_text(scr)
        assert any("default glm" in l and "light flash" in l for l in lines)


def test_fit_overlay_suffix_whole_names_only():
    """Юнит по образцу test_fit_mode_pairs_whole_pairs_only (settings.py:725-743,
    ревью Task 13 Minor): усечение суффикса секций-оверлеев — ЦЕЛЫМИ именами,
    не посимвольно; хвост «+…», когда обрезка нужна; «+…» без имён вовсе, если
    не влезает даже первое."""
    from gc_tui.screens.settings import fit_overlay_suffix

    names = ["security", "summary", "long_memory"]
    full = fit_overlay_suffix(names, width=200)
    assert full == "+security+summary+long_memory"
    tight = fit_overlay_suffix(names, width=15)
    assert tight == "+security+…"
    assert "summary" not in tight  # имя не режется посередине
    tiny = fit_overlay_suffix(names, width=5)
    assert tiny == "+…"  # не влезло даже первое имя
    assert fit_overlay_suffix([], width=200) == ""


# --- пустое состояние (текст §12.3 дословно + скелет + путь) ---


async def test_profiles_section_empty_state_shows_spec_text(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "Профиль — отдельный файл-конфиг" in body
        assert "переключаемый набор ЛЮБЫХ настроек" in body
        # Пустая секция — место для ОДНОГО действия: ни примера YAML, ни второго
        # обещания клавиши, ни пути. Путь называет строка под списком и считает его
        # от фактического корня; захардкоженный `~/.gc/...` врал при своём GC_HOME.
        assert "models:" not in body and "slots:" not in body
        assert "~/.gc/profiles" not in body
        assert "клавишей n" not in body
        # Строки уровня здесь больше нет: `level` правит глобальный ключ конфига и
        # переехал в «Режимы работы» (2026-08-27). В пустом состоянии остаётся
        # ровно одно действие — «+ добавить профиль».
        rows = _profiles_rows_text(scr)
        assert not any("уровень модели" in l for l in rows)


# --- активация: единое правило записи, все три случая + создание файла + no-op ---


async def test_activate_project_profile_writes_project_config_creates_file(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )
    project_config = project_dir / ".gc" / "config.yml"
    assert not project_config.exists()

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "work")
        scr._activate_profile(row["name"], row["ref"])
        assert project_config.is_file()
        assert 'profile: "work"' in project_config.read_text(encoding="utf-8")
        # базовый конфиг НЕ тронут указателем на проектный профиль
        assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


async def test_activate_base_profile_writes_base_config_without_project_pointer(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    # Токен — не украшение стенда: половина пары «адрес+токен» роняет чтение
    # профиля, а гейт активации теперь отказывает по стеку, который ломает сама
    # запись. Тест — про то, В КАКОЙ файл уходит указатель, и стенду положено
    # нести профиль, с которым следующий старт поднимается.
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        'models:\n  glm:\n    url: http://y\n    token: "sk"\nslots:\n  default: glm\n',
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "home")
        target = scr._profile_activation_target(row["ref"])
        assert Path(target) == base_cfg
        scr._activate_profile(row["name"], row["ref"])
        assert 'profile: "home"' in base_cfg.read_text(encoding="utf-8")
        assert not (project_dir / ".gc" / "config.yml").exists()


async def test_activate_base_profile_redirects_to_project_when_project_sets_pointer(
    tmp_path, monkeypatch
):
    """Проектный `.gc/config.yml` уже несёт указатель (перекрыл бы базу) — запись
    базового профиля тоже уходит в проектный конфиг, иначе была бы no-op (спека §12.3)."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    # профиль ВАЛИДЕН (полная пара не нужна — модель без своего подключения):
    # активация судится стеком строго, и стенд с половиной пары (url без token)
    # проверял бы отказ гейта, а не место записи указателя
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    model: zhipu/glm\nslots:\n  default: glm\n",
    )
    project_config = _write(
        project_dir / ".gc" / "config.yml", 'profile: "other"\n'
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "home")
        target = scr._profile_activation_target(row["ref"])
        assert Path(target) == project_config
        scr._activate_profile(row["name"], row["ref"])
        assert 'profile: "home"' in project_config.read_text(encoding="utf-8")
        # базовый — не тронут указателем чужого профиля
        assert 'profile: ""' in base_cfg.read_text(encoding="utf-8")


async def test_overlay_suffix_is_spaced_off_the_active_marker(
    tmp_path, monkeypatch
):
    """Профиль, который несёт ТОЛЬКО секции (без слотов), не слипается с маркером.

    Отступ хвоста был привязан к наличию слотов, и у такого профиля пропадал —
    строка читалась как «активен+voice» одним словом (живой смок владельца
    2026-08-26). Профиль без слотов — обычный случай: секция `voice`, гварды,
    лимиты меняются без переопределения моделей."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        "voice:\n  url: https://from-profile/v1/audio/transcriptions\n",
    )

    app = app_mod.TuiApp(
        settings=tui_settings({**_base_dict(), "profile": "work"})
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={**_base_dict(), "profile": "work"}, config_path=base_cfg
        )
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        line = next(l for l in _profiles_rows_text(scr) if "work" in l)
    assert "+voice" in line, line
    assert "активен+voice" not in line, line
    assert "активен   +voice" in line, line


async def test_active_base_profile_selected_by_project_config_shows_marker_and_hint(
    tmp_path, monkeypatch
):
    """Спека §12.3: активный профиль из БАЗОВОГО корня, выбранный проектным
    `.gc/config.yml` (не сам профиль — только указатель), — строка несёт
    «глобальный» (источник ФАЙЛА профиля) + «● активен» + «· выбран проектом»;
    hint выделенной строки — «выбор задан .gc/config.yml проекта» (ревью Task 13,
    Important-находка: settings.py:1094-1097/1129-1131, ветка `_project_sets_
    profile_pointer() and ref.root_kind != "project"` — было без регресс-теста)."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )
    # Проектный конфиг сам задаёт указатель — «home» становится действующим
    # ИМЕННО через него (self._config уже несёт "profile": "home" — так же, как
    # его отдал бы реальный load_config_ex после merge проектного слоя).
    _write(project_dir / ".gc" / "config.yml", 'profile: "home"\n')

    app = app_mod.TuiApp(
        settings=tui_settings({**_base_dict(), "profile": "home"})
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={**_base_dict(), "profile": "home"}, config_path=base_cfg
        )
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "home")
        assert row["ref"].root_kind == "base"
        lines = _profiles_rows_text(scr)
        home_line = next(l for l in lines if "home" in l)
        assert "глобальный" in home_line
        assert "● активен" in home_line
        assert "выбран проектом" in home_line
        scr._profiles_idx = next(
            i for i, r in enumerate(rows) if r["kind"] == "profile"
        )
        hint = scr._profiles_hint_text(rows)
        assert "выбор задан .gc/config.yml проекта" in hint


async def test_activate_already_active_profile_is_noop_with_notice(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text(profile="home")
    )
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )

    app = app_mod.TuiApp(
        settings=tui_settings({**_base_dict(), "profile": "home"})
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={**_base_dict(), "profile": "home"}, config_path=base_cfg
        )
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "home")
        before = base_cfg.read_text(encoding="utf-8")
        scr._activate_profile(row["name"], row["ref"])
        assert base_cfg.read_text(encoding="utf-8") == before  # файл не тронут
        title = " ".join(str(w.render()) for w in scr.query("#set-title"))
        assert "уже активен" in title


async def test_profiles_hint_shows_path_and_activation_target(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile_path = _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        scr._profiles_idx = next(
            i for i, r in enumerate(rows) if r["kind"] == "profile"
        )
        hint = scr._profiles_hint_text(rows)
        assert str(profile_path) in hint
        # Волна B, решение юзера В1: активация — смена состояния, её клавиша Space
        # (Enter = «войти внутрь», редактор профиля приезжает в Task 6).
        assert f"Space — активировать (запись: {base_cfg})" in hint


# --- shadow-hint в «Подключении» ---
#
# Волна B: из секции «Модели» подсказка ушла (решение 6) — при живом реестре
# `agent.model` базой наследования не является, и строка резервной модели говорит
# это своими словами («действует только без реестра»). В «Подключении» смысл
# прежний: плоские поля остаются базой наследования, но не действующим резолвом.


async def test_shadow_hint_shown_when_profile_active(tmp_path, monkeypatch):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text(profile="work")
    )
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )

    app = app_mod.TuiApp(
        settings=tui_settings(
            {**_base_dict(), "profile": "work", "models": {"flash": {}}}
        )
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={**_base_dict(), "profile": "work"}, config_path=base_cfg
        )
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "действует профиль work" in body


async def test_shadow_hint_shown_for_registry_without_profile(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(
        settings=tui_settings(
            {
                **_base_dict(),
                "models": {
                    "flash": {
                        "url": "http://x",
                        "authorization_header": "Bearer sk",
                    }
                },
            }
        )
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={
                **_base_dict(),
                "models": {
                    "flash": {
                        "url": "http://x",
                        "authorization_header": "Bearer sk",
                    }
                },
            },
            config_path=base_cfg,
        )
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "действует реестр моделей" in body


async def test_shadow_hint_absent_in_legacy_mode(tmp_path, monkeypatch):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "действует профиль" not in body
        assert "действует реестр моделей" not in body


# --- value-origin: подпись слоя профиля обязательна (KeyError-регресс) ---


def test_overlay_label_profile_kind_no_keyerror(tmp_path, monkeypatch):
    """Регресс: origin.overlay_kind='profile:*' без подписи в OVERLAY_LABELS
    роняет settings.py KeyError'ом (бриф Task 13) — подпись обязана быть."""
    from gc_core.config_origin import Origin, overlay_label

    origin = Origin(
        raw="",
        overlay_kind="profile:project",
        overlay_path=str(tmp_path / "work.yml"),
        effective="glm",
    )
    assert overlay_label(origin) == "профилем work (проектным)"

    origin_base = Origin(
        raw="",
        overlay_kind="profile:base",
        overlay_path=str(tmp_path / "home.yml"),
        effective="glm",
    )
    assert overlay_label(origin_base) == "профилем home (глобальным)"


def test_origins_reports_profile_layer(tmp_path, monkeypatch):
    from gc_core.config_origin import origins

    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base = _write(
        tmp_path / "base" / "config.yml",
        'profile: "work"\nagent:\n  model: base-model\n',
    )
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        "agent:\n  model: profile-model\n",
    )

    got = origins(str(base), ["agent.model"])["agent.model"]
    assert got.effective == "profile-model"
    assert got.overlay_kind == "profile:project"
    assert got.is_overridden is True


async def test_settings_screen_shows_profile_overlay_label_no_keyerror(
    tmp_path, monkeypatch
):
    """Интеграционный регресс (бриф): открыть «Подключение», когда действующее
    значение перекрыто ПРОФИЛЕМ — не должно падать KeyError на OVERLAY_LABELS."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml",
        'profile: "work"\nbackend:\n  completions_url: "http://base/v1"\nagent:\n  model: "test-model"\n',
    )
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'backend:\n  completions_url: "http://profile/v1"\n',
    )

    app = app_mod.TuiApp(
        settings=tui_settings(
            {"backend.completions_url": "http://profile/v1", "profile": "work"}
        )
    )
    async with app.run_test() as pilot:
        scr = _mk_screen(
            config={
                "backend.completions_url": "http://profile/v1",
                "profile": "work",
            },
            config_path=base_cfg,
        )
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()  # не должно бросить KeyError
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "перекрыто профилем work (проектным)" in body


def test_voice_override_text_names_the_profile(tmp_path, monkeypatch):
    """Тот же класс, что сторож «Подключения» выше, — но для строки честности
    голосовой секции: подпись перекрывшего слоя строится ОДНОЙ функцией на весь
    экран. Секция голоса пришла из develop с прямым обращением к словарю
    `OVERLAY_LABELS`, где профильные значения — шаблоны, и печатала человеку
    «перекрыто профилем {name} (проектным)». Проверяем результат, а не вызов:
    подмена рендера обратно на словарь краснит именно этот assert."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml",
        'profile: "work"\nvoice:\n  url: "http://base-voice/v1"\nagent:\n  model: "test-model"\n',
    )
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'voice:\n  url: "http://profile-voice/v1"\n',
    )

    scr = _mk_screen(config={"profile": "work"}, config_path=base_cfg)
    text = scr._voice_override_text()
    assert text == (
        "⚠ действует http://profile-voice/v1 — перекрыто профилем work (проектным); выбор режима ниже этого не изменит"
    )


async def test_voice_override_line_stands_above_both_modes(
    tmp_path, monkeypatch
):
    """Строка перекрытия — свойство ВСЕЙ секции, и стоит выше обоих вариантов.

    Перекрывший слой бьёт оба режима: профиль ложится выше managed-слоя, в котором
    живёт адрес локальной батарейки, — выбрав «Локальный сервис», человек всё равно
    получит адрес профиля. Стоя ПОД вариантами, строка прилипала к соседнему и
    читалась как его свойство (живой смок владельца 2026-08-26): курсор на
    «Локальном сервисе» — предупреждение про чужой, казалось бы, адрес."""
    from gc_testkit.tui_support import open_settings_section
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    from test_settings_voice import CLOUD, FakeVoiceSetup

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml",
        'profile: "work"\nvoice:\n  url: "http://base-voice/v1"\nagent:\n  model: "test-model"\n',
    )
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'voice:\n  url: "http://profile-voice/v1"\n',
    )

    app = app_mod.TuiApp(
        settings=tui_settings({"profile": "work", "voice": {}})
    )
    async with app.run_test() as pilot:
        scr = SettingsScreen(
            settings=as_settings({"profile": "work", "voice": {}}),
            config_path=str(base_cfg),
            on_apply=lambda: None,
            probe_fn=_fake_probe(),
            voice=FakeVoiceSetup(CLOUD),
        )
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Голосовой ввод")
        await pilot.pause()
        widgets = list(scr.query("#set-frame > *"))
        classes = [" ".join(w.classes) for w in widgets]
        over = next(
            i for i, c in enumerate(classes) if "set-voiceoverride" in c
        )
        rows = [i for i, c in enumerate(classes) if "set-voicerow" in c]
        margin = widgets[over].styles.margin
    assert rows, classes
    assert over < rows[0], (
        f"строка перекрытия должна стоять ВЫШЕ вариантов: {classes}"
    )
    # Воздух с ОБЕИХ сторон одним shorthand: вплотную к первой строке режима
    # предупреждение снова читается как её свойство. Раздельный margin-bottom рядом с
    # `.set-hint { margin-top: 1 }` Textual перетирает — сторож ловит и это.
    assert (margin.top, margin.bottom) == (1, 1), margin


# --- /config: шапка профиль/уровень/модель + путь файла профиля ---


async def test_config_screen_header_shows_profile_level_model(
    tmp_path, monkeypatch
):
    from gc_tui import app as app_mod
    from gc_tui.screens.config import ConfigScreen

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    profile_path = _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text(profile="work")
    )

    app = app_mod.TuiApp(
        settings=tui_settings(
            {
                "profile": "work",
                "level": "default",
                "agent.model": "flash",
                "models": {"flash": {}},
                "slots": {"default": "flash"},
            }
        )
    )
    app.set_config_path(str(base_cfg))
    async with app.run_test() as pilot:
        app.screen._cmd_config()
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, ConfigScreen)
        text = scr.rendered_text()
        assert "профиль: work (проектный)" in text
        assert "уровень: default" in text
        # Модель — третье слагаемое шапки (`tui/screens/config.py:116`). Без неё
        # тест повторял бы дефект близнеца `/status` ниже: имя обещает три поля,
        # тело проверяет два, а `модель:` могла бы исчезнуть, не уронив ничего.
        assert "модель: flash" in text
        assert str(profile_path) in text


# --- /status: строка профиля/уровня/модели ---


async def test_status_command_includes_profile_level_model(
    tmp_path, monkeypatch
):
    """§14 TUI: `/status` — строка профиля/уровня/модели.

    Раньше тест проверял из четырёх полей блока `profile` ровно одно (`level`) —
    и на конфиге БЕЗ профиля, где остальные три и не могли быть заполнены. Имя
    обещало больше, чем тело: `name`/`source`/`model` могли исчезнуть из payload,
    не уронив ничего. Здесь профиль реально активен, реестр непуст, и проверяются
    все четыре — `/status` для того и зовут, когда не понимают, чем идёт работа.
    """
    import json

    from gc_tui import app as app_mod
    from gc_tui.screens.viewer import ViewerScreen

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n",
    )
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text(profile="work")
    )

    app = app_mod.TuiApp(
        settings=tui_settings(
            {
                "profile": "work",
                "level": "light",
                "models": {"flash": {}},
                "slots": {"default": "flash"},
            }
        )
    )
    app.set_config_path(str(base_cfg))
    async with app.run_test() as pilot:
        app.screen._cmd_status("")
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, ViewerScreen)
        profile_block = json.loads(scr._content)["runtime"]["profile"]

    assert profile_block["name"] == "work"
    assert profile_block["source"] == "проектный"
    assert profile_block["level"] == "light"
    assert profile_block["model"] == "flash"


# --- онбординг-предупреждение о перекрывающем слое (спека §11) ---


def test_onboarding_note_for_active_profile():
    """Профиль лежит НАД базовым конфигом (ось 2, §2) и перекроет модельную часть
    того, что мастер запишет. Текст — факт без адреса действия: ту же строку
    печатает gc-cli, где экрана «Настройки · Профили» не существует."""
    from gc_core.onboarding import overriding_layer_note

    note = overriding_layer_note(make_settings({"profile": "work"}))
    assert (
        note == 'действует профиль "work" — он перекроет модельные настройки'
    )


def test_onboarding_note_absent_for_registry_in_the_edited_file(tmp_path):
    """Реестр лежит в том же файле, который мастер и правит, — перекрывать выбор
    некому (решение 10: мастер дописывает этот же реестр)."""
    from gc_core.onboarding import overriding_layer_note

    base = tmp_path / "config.yml"
    base.write_text(
        "models:\n  flash:\n    model: g\nslots:\n  default: flash\n",
        encoding="utf-8",
    )
    assert (
        overriding_layer_note(
            make_settings({"models": {"flash": {}}}), str(base)
        )
        is None
    )


def test_onboarding_note_for_registry_from_layer_above_base(
    tmp_path, monkeypatch
):
    """Спека §11 «любой источник»: действующий реестр пришёл проектным слоем — то,
    что мастер запишет в базовый config.yml, этот слой перекроет, и выбор человека
    не подействует. Молчать здесь нельзя."""
    from gc_core.config_modules import env_vars
    from gc_core.onboarding import overriding_layer_note

    base = tmp_path / "config.yml"
    base.write_text('profile: ""\n', encoding="utf-8")
    project = tmp_path / ".gc" / "config.yml"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        "models:\n  proj:\n    model: p\nslots:\n  default: proj\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        env_vars, "project_local_config_path", lambda: str(project)
    )
    note = overriding_layer_note(
        make_settings({"models": {"proj": {}}}), str(base)
    )
    assert note is not None and "перекроет" in note


def test_onboarding_note_absent_in_legacy_mode():
    from gc_core.onboarding import overriding_layer_note

    assert overriding_layer_note(make_settings({"agent.model": "x"})) is None


def test_setup_guide_includes_profile_note():
    from gc_core.onboarding import setup_guide

    text = setup_guide(
        "/p/config.yml",
        make_settings(
            {
                "profile": "work",
                "agent.model": "x",
                "backend.completions_url": "http://x",
            }
        ),
        program="gc-ua",
    )
    assert (
        'действует профиль "work" — он перекроет модельные настройки' in text
    )
    # Адрес действия — принадлежность экрана мастера: в CLI-подсказке этого
    # экрана нет, и звать туда нельзя (спека §3).
    assert "Настройки · Профили" not in text


class _FakeMcp:
    def installable_names(self):
        return []

    def wired_names(self):
        return []

    def descriptions(self):
        return {}

    def install(self, name=None):
        return 0


async def test_onboarding_screen_shows_warning_when_profile_active(tmp_path):
    from gc_tui import app as app_mod
    from gc_tui.screens.onboarding import OnboardingScreen

    app = app_mod.TuiApp(settings=tui_settings({"profile": "work"}))
    async with app.run_test() as pilot:
        scr = OnboardingScreen(
            settings=as_settings({"profile": "work"}),
            config_path=str(tmp_path / "config.yml"),
            review_all=True,
            on_commit=lambda: None,
            fetch=lambda *a, **k: None,
            mcp=_FakeMcp(),
        )
        app.push_screen(scr)
        await pilot.pause()
        warn = scr.query_one("#onb-profile-warn", Static)
        assert 'действует профиль "work"' in str(warn.render())


async def test_onboarding_screen_no_warning_for_registry_in_the_edited_file(
    tmp_path,
):
    """Реестр лежит в файле, который мастер и правит, — плашке нечего сказать."""
    from gc_tui import app as app_mod
    from gc_tui.screens.onboarding import OnboardingScreen
    from textual.css.query import NoMatches

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "models:\n  flash:\n    model: g\nslots:\n  default: flash\n",
        encoding="utf-8",
    )
    app = app_mod.TuiApp(settings=tui_settings({"models": {"flash": {}}}))
    async with app.run_test() as pilot:
        scr = OnboardingScreen(
            settings=as_settings({"models": {"flash": {}}}),
            config_path=str(cfg_path),
            review_all=True,
            on_commit=lambda: None,
            fetch=lambda *a, **k: None,
            mcp=_FakeMcp(),
        )
        app.push_screen(scr)
        await pilot.pause()
        with pytest.raises(NoMatches):
            scr.query_one("#onb-profile-warn", Static)


async def test_onboarding_screen_no_warning_in_legacy_mode(tmp_path):
    from gc_tui import app as app_mod
    from gc_tui.screens.onboarding import OnboardingScreen
    from textual.css.query import NoMatches

    app = app_mod.TuiApp(settings=tui_settings({"agent.model": "x"}))
    async with app.run_test() as pilot:
        scr = OnboardingScreen(
            settings=as_settings({"agent.model": "x"}),
            config_path=str(tmp_path / "config.yml"),
            review_all=True,
            on_commit=lambda: None,
            fetch=lambda *a, **k: None,
            mcp=_FakeMcp(),
        )
        app.push_screen(scr)
        await pilot.pause()
        with pytest.raises(NoMatches):
            scr.query_one("#onb-profile-warn", Static)


# --- cold-start notice активации проектного профиля (спека §3.5, residual Task 11) ---


def test_cold_start_notice_for_project_profile_pointer(tmp_path, monkeypatch):
    """Профиль выбран прямо базовым `profile:` (не сессией) на самом первом старте
    (`tui.main()`) — notice тоже обязан всплыть (Concern из отчёта Task 11)."""
    from gc_core import runtime_notices
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    _write(
        project_dir / ".gc" / "profiles" / "work.yml",
        "models:\n  flash:\n    url: http://x\n    token: sk\nslots:\n  default: flash\n",
    )
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text(profile="work")
    )

    from gc_core.config import load_settings

    loaded = load_settings(str(base_cfg))
    meta = loaded.profile_meta

    runtime_notices.clear_runtime_notices(listeners=True)
    app = app_mod.TuiApp(settings=loaded)
    app._maybe_notice_profile_activation(meta.active_name or "", meta, loaded)

    notices = runtime_notices.collect_runtime_notices()
    assert any(n.code == "project_profile_activated" for n in notices)


# --- «— без профиля» + черта + хвост (спека §12.3, ревизия 2026-07-28) -------


async def test_none_row_first_active_marker(tmp_path, monkeypatch):
    """«— без профиля» — первая строка; при пустом указателе `● активен` на ней.

    Черты в секции больше нет: она отделяла строку уровня, а уровень переехал в
    «Режимы работы» (2026-08-27) — секция снова однородный список профилей."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert rows[0]["kind"] == "none"
        lines = _profiles_rows_text(scr)
        assert "без профиля" in lines[0] and "● активен" in lines[0]
        assert not list(scr.query(".set-group-sep"))  # отделять больше нечего
        hint = scr._profiles_hint_text(rows)
        assert hint == "профиль не выбран — действует чистый конфиг"


async def test_none_row_absent_in_empty_state(tmp_path, monkeypatch):
    """Профилей нет — деактивировать нечего: none-строки нет. Строка-действие
    «+ добавить профиль…» есть и в пустом состоянии (единственный вход кроме n) —
    и теперь она единственная: уровень уехал в «Режимы работы»."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert [r["kind"] for r in rows] == ["add"]


async def test_deactivate_writes_empty_pointer_to_base_config(
    tmp_path, monkeypatch
):
    """Space на «— без профиля» при активном глобальном профиле → `profile: ""`
    в базовый конфиг (единое правило записи). Клавиша — В1 волны B: снятие
    активации это та же смена состояния, что и активация."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text("home")
    )
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(settings=tui_settings(cfg))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=cfg, config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert rows[0]["kind"] == "none"
        hint_before = scr._profiles_hint_text(rows)
        assert f"Space — деактивировать (запись: {base_cfg}" in hint_before
        scr._deactivate_profile()
        import yaml

        saved = yaml.safe_load(base_cfg.read_text(encoding="utf-8"))
        assert saved.get("profile") == ""


async def test_deactivate_noop_when_nothing_active(tmp_path, monkeypatch):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        before = base_cfg.read_text(encoding="utf-8")
        scr._deactivate_profile()
        assert base_cfg.read_text(encoding="utf-8") == before  # файл не тронут


async def test_session_tail_full_form_on_profile_row(tmp_path, monkeypatch):
    """Хвост сессионного оверрайда — «· выбран в текущей сессии» (полная форма,
    ревизия 2026-07-28)."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "smoke.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    app._session_overrides["profile"] = "smoke"
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        lines = _profiles_rows_text(scr)
        smoke_line = next(l for l in lines if "smoke" in l)
        assert "· выбран в текущей сессии" in smoke_line


# --- строка уровня — редактируемая (←/→/Enter), единый источник TOGGLES,
# единое правило записи, футер позиционный (задача 7) -------------------------


async def test_profiles_section_refuses_to_write_into_packaged_template(
    tmp_path, monkeypatch
):
    """Запуск без пользовательского конфига (GC_HOME не настроен, `--init-config`
    не запускали): базой стал пакетный config.default.yml — ни активация профиля,
    ни деактивация не смеют его править. Тот же guard, что у «Режимов работы»
    (`_modes_apply`), но по ЦЕЛИ записи: у профилей она бывает проектной."""
    from gc_core import config as config_mod
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    packaged = _write(
        tmp_path / "packaged" / "config.default.yml", _base_config_text("home")
    )
    _write(
        tmp_path / "packaged" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )
    monkeypatch.setattr(
        config_mod, "_default_config_path", lambda: str(packaged)
    )
    cfg = dict(_base_dict(), profile="home", level="default")

    app = app_mod.TuiApp(settings=tui_settings(dict(cfg)))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=cfg, config_path=packaged)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()

        home = next(r for r in rows if r.get("name") == "home")
        scr._activate_profile("home", home["ref"])
        scr._deactivate_profile()
        await pilot.pause()

        text = packaged.read_text(encoding="utf-8")
        assert 'profile: "home"' in text  # файл не тронут
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "пакетный шаблон" in notice


async def test_profiles_render_reads_each_profile_file_once(
    tmp_path, monkeypatch
):
    """Сводка профиля (чтение + разбор YAML) — самая дорогая часть отрисовки, а
    строки за кадр спрашивают четыре независимых места: футер, монтирование,
    refresh строк и refresh подсказки. Кэш прохода: каждый файл читается ровно
    раз на кадр — и всё равно перечитывается на следующем кадре и на нажатии
    (правка профиля извне обязана быть видна сразу)."""
    from gc_core.config_modules import profiles as profiles_mod
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profiles_dir = tmp_path / "base" / "profiles"
    for name in ("home", "work", "corp"):
        _write(
            profiles_dir / f"{name}.yml",
            "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
        )

    calls: list[str] = []
    real_summary = profiles_mod.profile_display_summary

    def _counting_summary(path):
        calls.append(str(path))
        return real_summary(path)

    monkeypatch.setattr(
        profiles_mod, "profile_display_summary", _counting_summary
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)

        calls.clear()
        scr._render_view()
        await pilot.pause()
        assert len(calls) == len(set(calls)) == 3

        calls.clear()
        await pilot.press("down")
        await pilot.pause()
        assert len(calls) == len(set(calls)) == 3


async def test_profiles_cursor_survives_row_disappearing_under_it(
    tmp_path, monkeypatch
):
    """Набор строк перечитывается с диска на КАЖДОЕ нажатие и на каждый рендер, а
    файл профиля мог исчезнуть под ногами (правка/удаление извне) — курсор тогда
    указывает за конец, и голый `rows[idx]` уронил бы обработчик клавиш и хинт."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profiles_dir = tmp_path / "base" / "profiles"
    _write(
        profiles_dir / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )
    _write(
        profiles_dir / "work.yml",
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert len(rows) == 4  # без профиля, home, work, «+ добавить»
        scr._profiles_idx = len(rows) - 1
        scr._render_view()
        await pilot.pause()

        (profiles_dir / "home.yml").unlink()
        scr._profiles_idx = 3  # строк осталось 3 — курсор за концом
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert scr._profiles_idx == 2

        scr._profiles_idx = 3
        assert scr._profiles_hint_text(scr._profiles_rows())
        assert scr._profiles_idx == 2


# --- Клавиша активации: Space (канон базы), Enter — тихий алиас -----------------


async def _cursor_to(scr, kind, name=None):
    """Курсор на строку нужного вида, с перерисовкой."""
    rows = scr._profiles_rows()
    scr._profiles_idx = next(
        i
        for i, r in enumerate(rows)
        if r["kind"] == kind and (name is None or r.get("name") == name)
    )
    scr._render_view()


async def test_profiles_footer_promises_space_not_enter(tmp_path, monkeypatch):
    """Активация профиля — смена состояния, и по канону базы это Space (так же
    у MCP «вкл/выкл» и у отметки в post-update). Enter в базе значит «войти
    внутрь», поэтому глаголом активации футер его больше не называет."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "profile")
        await pilot.pause()
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "Space — активировать" in footer
        assert "Enter — активировать" not in footer


async def test_profiles_footer_on_none_row_names_deactivation(
    tmp_path, monkeypatch
):
    """Строка «— без профиля» при АКТИВНОМ профиле снимает его — футер обязан
    называть это своим глаголом. Раньше он обещал «активировать», а подсказка в
    том же кадре говорила «деактивировать»: курсор при входе в секцию стоит
    ровно здесь, так что первое, что видел пользователь, было противоречие."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text("home")
    )
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    cfg = _base_dict()
    cfg["profile"] = "home"
    app = app_mod.TuiApp(settings=tui_settings(cfg))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=cfg, config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "none")
        await pilot.pause()
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "Space — деактивировать" in footer
        assert "активировать  " not in footer.replace("деактивировать", "")


async def test_profiles_footer_on_none_row_is_silent_with_nothing_to_undo(
    tmp_path, monkeypatch
):
    """Профиль не выбран — на этой строке делать нечего, и футер молчит о
    действии вовсе. Обещать «деактивировать» там, где деактивировать некого, —
    та же ложь, только с другим глаголом."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "none")
        await pilot.pause()
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "активировать" not in footer  # ни с приставкой, ни без
        assert "↑/↓ — выбор" in footer and "Esc — меню" in footer


async def test_space_activates_profile(tmp_path, monkeypatch):
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "profile", "home")
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause(0.1)
        assert 'profile: "home"' in base_cfg.read_text(encoding="utf-8")


async def test_profiles_section_no_longer_carries_the_level_row(
    tmp_path, monkeypatch
):
    """`level` правит ГЛОБАЛЬНЫЙ `level:` конфига, а не ключ внутри файла профиля —
    в секции про профили он был гостем. Его дом — «Режимы работы», рядом с
    равными себе ручками «как работаем» (решение владельца 2026-08-27).

    Секция остаётся списком профилей: строки уровня нет ни в составе, ни в
    тексте, и черта, что стояла над ней, уходит вместе с ней."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n",
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)

        assert "level" not in [r["kind"] for r in scr._profiles_rows()]
        assert not any("уровень модели" in l for l in _profiles_rows_text(scr))


async def test_none_row_hint_does_not_claim_a_clean_config_under_a_session_choice(
    tmp_path, monkeypatch
):
    """Подсказка на «— без профиля» говорит о ДЕЙСТВУЮЩЕМ состоянии, а не о файле,
    и при сессионном выборе врала: «действует чистый конфиг», когда действует
    профиль из сессии — тот самый, который в это же время показывает футер.

    Остальная секция не врёт: маркер `● активен` честно про глобальное состояние
    (её предмет — файл), а строка профиля несёт хвост «выбран в текущей сессии».
    Правится ровно фраза о действии."""
    from gc_tui import app as app_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    # Пара «адрес+токен» задана целиком: с половиной пары стек профиля не собрался
    # бы, и санация сняла бы сессионный выбор — предмет теста исчез бы до проверки.
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        'models:\n  glm:\n    url: http://y\n    token: "sk"\nslots:\n  default: glm\n',
    )

    app = app_mod.TuiApp(settings=tui_settings(_base_dict()))
    app.set_config_path(str(base_cfg))
    app.reload_config()
    async with app.run_test() as pilot:
        # ПОСЛЕ старта: запуск приложения перечитывает набор сессионных ключей и
        # затирает выставленное до него.
        app.set_session_override("profile", "home")
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        scr._profiles_idx = next(
            i for i, r in enumerate(rows) if r["kind"] == "none"
        )

        hint = scr._profiles_hint_text(scr._profiles_rows())

        assert "чистый конфиг" not in hint, hint
        assert "home" in hint  # назван тот профиль, что реально в силе
        assert "сесси" in hint


# --- витрины происхождения судят стек ЗАПУСКА, а не указатель из файла ---------
#
# `gc-ua --profile ""` — путь отхода, который советует `explain_broken_config`:
# профиль в файле назван, но в ЭТОМ запуске не работает. Витрина, спросившая диск
# без действующего оверрайда, называет слой, которого в стеке нет, — и прячет за
# ним точку починки. Сессионный выбор `/profiles` — та же ось: он тоже часть
# «действующего», и витрина обязана видеть его немедленно.


def _app_launched_with(base_cfg, profile_override):
    """Приложение, как его собирает `main()` под флагом `--profile`.

    Определение одно на набор — `test_app_reload_config._app_started_with`:
    вторая копия «как стартует окно» разошлась бы с первой на первой же правке."""
    from test_app_reload_config import _app_started_with

    return _app_started_with(base_cfg, profile_override)


async def test_registry_origin_judges_the_launch_stack_not_the_pointer(
    tmp_path, monkeypatch
):
    """`gc-ua --profile ""` при `profile: work` в файле: реестр живёт только в
    профиле, и в этом запуске уровни НЕ работают. Витрина происхождения, судящая
    по диску, печатала «реестр перекрыт профилем work» и прятала легаси-редактор
    вместе с точкой добавления модели — единственное, чем этот конфиг чинится."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text("work")
    )
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        'models:\n  glm:\n    url: http://y\n    token: "sk"\nslots:\n  default: glm\n',
    )

    app = _app_launched_with(base_cfg, "")
    assert (
        app.base_settings().runtime.active_profile == ""
    )  # стек запуска — без профиля
    async with app.run_test() as pilot:
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        origin = scr._registry_origin()

        assert origin.is_overridden is False, origin
        open_settings_section(scr, "Модели")
        await pilot.pause()
        from gc_tui.editors.models_section import ModelsSectionEditor

        editor = scr.query(ModelsSectionEditor).last()
        assert editor._legacy_default() is True  # редактор модели на месте
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "реестр перекрыт" not in body, body


async def test_registry_origin_sees_the_session_choice(tmp_path, monkeypatch):
    """Обратная сторона того же правила: указатель в файле пуст, а профиль выбран
    в сессии (`/profiles`) — реестр перекрыт ИМ, и витрина обязана сказать это
    сразу, не дожидаясь записи выбора в файл (её не будет)."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(
        tmp_path / "base" / "profiles" / "home.yml",
        'models:\n  glm:\n    url: http://y\n    token: "sk"\nslots:\n  default: glm\n',
    )

    app = _app_launched_with(base_cfg, None)
    async with app.run_test() as pilot:
        # ПОСЛЕ старта: запуск приложения перечитывает набор сессионных ключей и
        # затирает выставленное до него.
        app.set_session_override("profile", "home")
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        origin = scr._registry_origin()

        assert origin.is_overridden is True, origin
        assert origin.overlay_kind == "profile:base"


async def test_field_origins_judge_the_launch_stack_not_the_pointer(
    tmp_path, monkeypatch
):
    """Тот же класс у полей визарда (`_read_origins` → маркеры ⚠ «перекрыто»):
    под `--profile ""` адрес из профиля не действует, и обещание «ваша правка не
    подействует» — ложь ровно там, где человек чинит конфиг."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml", _base_config_text("work")
    )
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        'backend:\n  completions_url: "http://profile/v1"\n',
    )

    app = _app_launched_with(base_cfg, "")
    async with app.run_test() as pilot:
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        assert scr._origin("completions_url").is_overridden is False

        open_settings_section(scr, "Подключение")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "перекрыто профилем" not in body, body


async def test_overriding_layer_name_answers_by_files_like_the_write_gate(
    tmp_path, monkeypatch
):
    """ИСКЛЮЧЕНИЕ среди витрин происхождения экрана — и единственное: эта отвечает
    на вопрос о ПЕРСИСТЕНТНОСТИ записи, а не о том, что действует сейчас.

    Соседние три (маркеры ⚠ у полей, строка честности голоса, происхождение
    реестра) говорят о ТЕКУЩЕМ запуске — «ваша правка не действует», «уровни
    работают», — и обязаны судить стек, на котором окно запущено
    (`_acting_profile`). Эта показывается сразу после сохранения и говорит о судьбе
    записанного ключа: под `gc-ua --profile ""` профиль из файла в этом запуске не
    действует, но записанное значение он перекроет на ЛЮБОМ другом запуске — и
    послать чинить managed-файл значило бы отправить человека не туда.

    Здесь же проходит второе обязательство: гейт перед записью резолвит указатель
    ИЗ ФАЙЛОВ (`_stacked_app_config` судит следующий старт). Сообщение о судьбе
    записи и гейт, её пропустивший, обязаны говорить одно и то же — разойдясь, они
    оставляют человека с двумя противоположными ответами об одном ключе.

    Свипу на заметку: `profile_override` этой витрине НЕ передаётся намеренно."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml",
        _base_config_text("work") + 'tui:\n  finish_gate_mode: "off"\n',
    )
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        'tui:\n  finish_gate_mode: "smart"\n',
    )
    # Managed-слой теста — `tmp_path/config.local.yml` (автофикстура
    # `_isolated_config_layers`): достаточно его записать.
    _write(tmp_path / "config.local.yml", 'tui:\n  finish_gate_mode: "on"\n')

    app = _app_launched_with(base_cfg, "")
    async with app.run_test() as pilot:
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        name = scr._overriding_layer_name({"tui.finish_gate_mode": "off"})

        assert "профилем work" in name, name


async def test_voice_override_text_ignores_a_profile_out_of_the_stack(
    tmp_path, monkeypatch
):
    """Строка честности голосовой секции — та же витрина: под `--profile ""` адрес
    профиля не действует, и ⚠ о нём — ложная тревога о чужом файле."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(
        tmp_path / "base" / "config.yml",
        _base_config_text("work") + 'voice:\n  url: "http://base-voice/v1"\n',
    )
    _write(
        tmp_path / "base" / "profiles" / "work.yml",
        'voice:\n  url: "http://profile-voice/v1"\n',
    )

    app = _app_launched_with(base_cfg, "")
    async with app.run_test() as pilot:
        scr = _mk_screen(config=app.base_settings(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        assert scr._voice_override_text() is None

"""Settings-витрина «Профили» (спека `docs/specs/2026-07-23-profiles-design.md`
§12.3/§12.3a): список обоих корней, тёзки, суффикс секций-оверлеев, единое
правило записи (активация), shadow-hints, value-origin слоя профиля, `/config`
и `/status`, онбординг-предупреждение, cold-start notice (§3.5).
"""
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Static

from tests.tui.conftest import open_settings_section


def _fake_probe(result=None):
    """Дефолт — УСПЕШНЫЙ вердикт, а не `None`: воркер проб моделей эндпоинта
    разыменовывает `result.models` (`tui/screens/settings.py`), поэтому `None`
    роняет не строку пробы, а весь экран через `WorkerFailed`. Эталон вердикта
    один на все settings-тесты — `_probe_ok` из `test_settings_screen`."""
    if result is None:
        from tests.tui.test_settings_screen import _probe_ok
        result = _probe_ok()

    def probe_fn(endpoint, auth, verify, model, **kw):
        return result
    return probe_fn


@pytest.fixture(autouse=True)
def _no_managed_overlay(tmp_path, monkeypatch):
    """Без этого value-origin/активация читают НАСТОЯЩИЙ ~/.gc/config.local.yml
    разработчика (тот же приём, что и tests/test_config_origin.py)."""
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed-overlay-here.yml",
    )


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
        "  completions_url: \"http://host/v1/chat/completions\"\n"
        "agent:\n"
        "  model: \"test-model\"\n"
    )


def _mk_screen(config=None, config_path=None, on_apply=None):
    from tui.screens.settings import SettingsScreen
    return SettingsScreen(
        config=config or {}, config_path=str(config_path) if config_path else None,
        on_apply=on_apply or (lambda: None), probe_fn=_fake_probe(),
    )


async def _open_profiles(pilot, screen):
    open_settings_section(screen, "Профили")
    await pilot.pause()


def _profiles_rows_text(screen) -> list[str]:
    return [str(w.render()) for w in screen.query(".set-profiles-row")]


# --- список профилей обоих корней: имя, источник, тёзка, суффикс «+security» ---

async def test_profiles_section_lists_both_roots_with_source(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")
    _write(tmp_path / "base" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    app = app_mod.TuiApp(config=_base_dict())
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


def _base_dict() -> dict:
    return {"completions_url": "http://host/v1/chat/completions", "model": "test-model"}


async def test_profiles_section_tie_break_one_row_shadow_in_hint(tmp_path, monkeypatch):
    """Тёзка (одинаковое имя в обоих корнях) — ОДНА строка, побеждает проектный;
    перекрытый (глобальный) файл — в hint выделенной строки (спека §3.1)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    proj_profile = _write(project_dir / ".gc" / "profiles" / "work.yml",
                          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")
    base_profile = _write(tmp_path / "base" / "profiles" / "work.yml",
                          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    app = app_mod.TuiApp(config=_base_dict())
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
            i for i, r in enumerate(rows) if r["kind"] == "profile")
        hint = scr._profiles_hint_text(rows)
        assert str(base_profile) in hint


async def test_profiles_section_overlay_suffix_shows_plus_security(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\n"
          "slots:\n  default: flash\n"
          "security:\n  command_moderation:\n    enabled: true\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        lines = _profiles_rows_text(scr)
        assert any("+security" in l for l in lines)


async def test_profiles_section_slot_composition_shown_inline(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\n  glm:\n    url: http://y\n"
          "slots:\n  default: glm\n  light: flash\n")

    app = app_mod.TuiApp(config=_base_dict())
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
    from tui.screens.settings import fit_overlay_suffix
    names = ["security", "summary", "long_memory"]
    full = fit_overlay_suffix(names, width=200)
    assert full == "+security+summary+long_memory"
    tight = fit_overlay_suffix(names, width=15)
    assert tight == "+security+…"
    assert "summary" not in tight            # имя не режется посередине
    tiny = fit_overlay_suffix(names, width=5)
    assert tiny == "+…"                      # не влезло даже первое имя
    assert fit_overlay_suffix([], width=200) == ""


# --- пустое состояние (текст §12.3 дословно + скелет + путь) ---

async def test_profiles_section_empty_state_shows_spec_text_and_level_row(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "Профиль — отдельный файл-конфиг" in body
        assert "переключаемый набор ЛЮБЫХ настроек" in body
        assert "models" in body and "slots" in body
        assert "~/.gc/profiles/<имя>.yml" in body
        # строка уровня живёт и в пустом состоянии
        rows = _profiles_rows_text(scr)
        assert any("уровень модели по умолчанию" in l for l in rows)


# --- активация: единое правило записи, все три случая + создание файла + no-op ---

async def test_activate_project_profile_writes_project_config_creates_file(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")
    project_config = project_dir / ".gc" / "config.yml"
    assert not project_config.exists()

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_activate_base_profile_writes_base_config_without_project_pointer(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_activate_base_profile_redirects_to_project_when_project_sets_pointer(tmp_path, monkeypatch):
    """Проектный `.gc/config.yml` уже несёт указатель (перекрыл бы базу) — запись
    базового профиля тоже уходит в проектный конфиг, иначе была бы no-op (спека §12.3)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")
    project_config = _write(project_dir / ".gc" / "config.yml", 'profile: "other"\n')

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_active_base_profile_selected_by_project_config_shows_marker_and_hint(tmp_path, monkeypatch):
    """Спека §12.3: активный профиль из БАЗОВОГО корня, выбранный проектным
    `.gc/config.yml` (не сам профиль — только указатель), — строка несёт
    «глобальный» (источник ФАЙЛА профиля) + «● активен» + «· выбран проектом»;
    hint выделенной строки — «выбор задан .gc/config.yml проекта» (ревью Task 13,
    Important-находка: settings.py:1094-1097/1129-1131, ветка `_project_sets_
    profile_pointer() and ref.root_kind != "project"` — было без регресс-теста)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")
    # Проектный конфиг сам задаёт указатель — «home» становится действующим
    # ИМЕННО через него (self._config уже несёт "profile": "home" — так же, как
    # его отдал бы реальный load_config_ex после merge проектного слоя).
    _write(project_dir / ".gc" / "config.yml", 'profile: "home"\n')

    app = app_mod.TuiApp(config={**_base_dict(), "profile": "home"})
    async with app.run_test() as pilot:
        scr = _mk_screen(config={**_base_dict(), "profile": "home"}, config_path=base_cfg)
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
            i for i, r in enumerate(rows) if r["kind"] == "profile")
        hint = scr._profiles_hint_text(rows)
        assert "выбор задан .gc/config.yml проекта" in hint


async def test_activate_already_active_profile_is_noop_with_notice(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text(profile="home"))
    _write(tmp_path / "base" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    app = app_mod.TuiApp(config={**_base_dict(), "profile": "home"})
    async with app.run_test() as pilot:
        scr = _mk_screen(config={**_base_dict(), "profile": "home"}, config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        row = next(r for r in rows if r.get("name") == "home")
        before = base_cfg.read_text(encoding="utf-8")
        scr._activate_profile(row["name"], row["ref"])
        assert base_cfg.read_text(encoding="utf-8") == before   # файл не тронут
        title = " ".join(str(w.render()) for w in scr.query("#set-title"))
        assert "уже активен" in title


async def test_profiles_hint_shows_path_and_activation_target(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profile_path = _write(tmp_path / "base" / "profiles" / "home.yml",
                          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        scr._profiles_idx = next(
            i for i, r in enumerate(rows) if r["kind"] == "profile")
        hint = scr._profiles_hint_text(rows)
        assert str(profile_path) in hint
        assert f"Space — активировать (запись: {base_cfg})" in hint


# --- shadow-hint в «Подключение»/«Модель» ---

async def test_shadow_hint_shown_when_profile_active(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text(profile="work"))
    _write(tmp_path / "base" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")

    app = app_mod.TuiApp(config={**_base_dict(), "profile": "work", "models": {"flash": {}}})
    async with app.run_test() as pilot:
        scr = _mk_screen(config={**_base_dict(), "profile": "work"}, config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "действует профиль work" in body


async def test_shadow_hint_shown_for_registry_without_profile(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config={**_base_dict(), "models": {"flash": {"url": "http://x"}}})
    async with app.run_test() as pilot:
        scr = _mk_screen(config={**_base_dict(), "models": {"flash": {"url": "http://x"}}},
                         config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Модель")
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "действует реестр моделей" in body


async def test_shadow_hint_absent_in_legacy_mode(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
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
    from config_origin import Origin, overlay_label
    origin = Origin(raw="", overlay_kind="profile:project",
                    overlay_path=str(tmp_path / "work.yml"), effective="glm")
    assert overlay_label(origin) == "профилем work (проектным)"

    origin_base = Origin(raw="", overlay_kind="profile:base",
                         overlay_path=str(tmp_path / "home.yml"), effective="glm")
    assert overlay_label(origin_base) == "профилем home (глобальным)"


def test_origins_reports_profile_layer(tmp_path, monkeypatch):
    from config_origin import origins
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base = _write(tmp_path / "base" / "config.yml",
                 'profile: "work"\nagent:\n  model: base-model\n')
    _write(project_dir / ".gc" / "profiles" / "work.yml", "agent:\n  model: profile-model\n")

    got = origins(str(base), ["agent.model"])["agent.model"]
    assert got.effective == "profile-model"
    assert got.overlay_kind == "profile:project"
    assert got.is_overridden is True


async def test_settings_screen_shows_profile_overlay_label_no_keyerror(tmp_path, monkeypatch):
    """Интеграционный регресс (бриф): открыть «Подключение», когда действующее
    значение перекрыто ПРОФИЛЕМ — не должно падать KeyError на OVERLAY_LABELS."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      'profile: "work"\nbackend:\n  completions_url: "http://base/v1"\n'
                      'agent:\n  model: "test-model"\n')
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          'backend:\n  completions_url: "http://profile/v1"\n')

    app = app_mod.TuiApp(config={"completions_url": "http://profile/v1", "profile": "work"})
    async with app.run_test() as pilot:
        scr = _mk_screen(config={"completions_url": "http://profile/v1", "profile": "work"},
                         config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")
        await pilot.pause()          # не должно бросить KeyError
        body = " ".join(str(w.render()) for w in scr.query(".set-hint"))
        assert "перекрыто профилем work (проектным)" in body


# --- /config: шапка профиль/уровень/модель + путь файла профиля ---

async def test_config_screen_header_shows_profile_level_model(tmp_path, monkeypatch):
    from tui.screens.config import ConfigScreen
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    profile_path = _write(project_dir / ".gc" / "profiles" / "work.yml",
                          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text(profile="work"))

    app = app_mod.TuiApp(config={"profile": "work", "level": "default",
                                "model": "flash", "models": {"flash": {}}, "slots": {"default": "flash"}})
    app.set_config_path(str(base_cfg))
    async with app.run_test() as pilot:
        app.screen._cmd_config()
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, ConfigScreen)
        text = scr.rendered_text()
        assert "профиль: work (проектный)" in text
        assert "уровень: default" in text
        assert str(profile_path) in text


# --- /status: строка профиля/уровня/модели ---

async def test_status_command_includes_profile_level_model(tmp_path, monkeypatch):
    import tui.app as app_mod
    from tui.screens.viewer import ViewerScreen
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config={"level": "light"})
    app.set_config_path(str(base_cfg))
    async with app.run_test() as pilot:
        app.screen._cmd_status("")
        await pilot.pause()
        scr = app.screen
        assert isinstance(scr, ViewerScreen)
        assert '"level": "light"' in scr._content


# --- онбординг-предупреждение при активном реестре/профиле (спека §11) ---

def test_onboarding_note_for_active_profile():
    from onboarding import active_profile_or_registry_note
    note = active_profile_or_registry_note({"profile": "work"})
    assert note == ('действует профиль "work" — эти поля станут базой наследования, '
                    'а не рабочей моделью')


def test_onboarding_note_for_registry_without_profile():
    from onboarding import active_profile_or_registry_note
    note = active_profile_or_registry_note({"models": {"flash": {}}})
    assert note == ("действует реестр моделей — эти поля станут базой наследования, "
                    "а не рабочей моделью")


def test_onboarding_note_absent_in_legacy_mode():
    from onboarding import active_profile_or_registry_note
    assert active_profile_or_registry_note({"model": "x"}) is None


def test_setup_guide_includes_profile_note():
    from onboarding import setup_guide
    text = setup_guide("/p/config.yml", {"profile": "work", "model": "x",
                                        "completions_url": "http://x", }, program="gc-ua")
    assert 'действует профиль "work"' in text


class _FakeMcp:
    def installable_names(self): return []
    def wired_names(self): return []
    def descriptions(self): return {}
    def install(self, name=None): return 0


async def test_onboarding_screen_shows_warning_when_registry_active(tmp_path):
    from tui.screens.onboarding import OnboardingScreen
    import tui.app as app_mod
    app = app_mod.TuiApp(config={"models": {"flash": {}}})
    async with app.run_test() as pilot:
        scr = OnboardingScreen(config={"models": {"flash": {}}}, config_path=str(tmp_path / "config.yml"),
                               review_all=True, on_commit=lambda: None,
                               fetch=lambda *a, **k: None, mcp=_FakeMcp())
        app.push_screen(scr)
        await pilot.pause()
        warn = scr.query_one("#onb-profile-warn", Static)
        assert "действует реестр моделей" in str(warn.render())


async def test_onboarding_screen_no_warning_in_legacy_mode(tmp_path):
    from tui.screens.onboarding import OnboardingScreen
    from textual.css.query import NoMatches
    import tui.app as app_mod
    app = app_mod.TuiApp(config={"model": "x"})
    async with app.run_test() as pilot:
        scr = OnboardingScreen(config={"model": "x"}, config_path=str(tmp_path / "config.yml"),
                               review_all=True, on_commit=lambda: None,
                               fetch=lambda *a, **k: None, mcp=_FakeMcp())
        app.push_screen(scr)
        await pilot.pause()
        with pytest.raises(NoMatches):
            scr.query_one("#onb-profile-warn", Static)


# --- cold-start notice активации проектного профиля (спека §3.5, residual Task 11) ---

def test_cold_start_notice_for_project_profile_pointer(tmp_path, monkeypatch):
    """Профиль выбран прямо базовым `profile:` (не сессией) на самом первом старте
    (`tui.main()`) — notice тоже обязан всплыть (Concern из отчёта Task 11)."""
    import tui.app as app_mod
    import runtime_notices

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    _write(project_dir / ".gc" / "profiles" / "work.yml",
          "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text(profile="work"))

    from config import load_config_ex
    config_dict, meta = load_config_ex(str(base_cfg))

    runtime_notices.clear_runtime_notices(listeners=True)
    app = app_mod.TuiApp(config=config_dict)
    app._maybe_notice_profile_activation(meta.active_name or "", meta, config_dict)

    notices = runtime_notices.collect_runtime_notices()
    assert any(n.code == "project_profile_activated" for n in notices)


# --- «— без профиля» + черта + хвост (спека §12.3, ревизия 2026-07-28) -------

async def test_none_row_first_active_marker_and_separator(tmp_path, monkeypatch):
    """«— без профиля» — первая строка; при пустом указателе `● активен` на ней;
    черта-разделитель перед строкой уровня."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert rows[0]["kind"] == "none"
        lines = _profiles_rows_text(scr)
        assert "без профиля" in lines[0] and "● активен" in lines[0]
        seps = list(scr.query(".set-group-sep"))
        assert seps, "черта перед строкой уровня отсутствует"
        hint = scr._profiles_hint_text(rows)
        assert hint == "профиль не выбран — действует чистый конфиг"


async def test_none_row_absent_in_empty_state(tmp_path, monkeypatch):
    """Профилей нет — деактивировать нечего: none-строки нет, пустое состояние
    как прежде."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        assert [r["kind"] for r in rows] == ["level"]


async def test_deactivate_writes_empty_pointer_to_base_config(tmp_path, monkeypatch):
    """Enter на «— без профиля» при активном глобальном профиле → `profile: ""`
    в базовый конфиг (единое правило записи)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    cfg = dict(_base_dict(), profile="home")
    app = app_mod.TuiApp(config=cfg)
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
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        before = base_cfg.read_text(encoding="utf-8")
        scr._deactivate_profile()
        assert base_cfg.read_text(encoding="utf-8") == before   # файл не тронут


async def test_session_tail_full_form_on_profile_row(tmp_path, monkeypatch):
    """Хвост сессионного оверрайда — «· выбран в текущей сессии» (полная форма,
    ревизия 2026-07-28)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "smoke.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
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

async def _open_level_row(pilot, scr) -> list[dict]:
    """Открыть «Профили» и поставить курсор на строку уровня (всегда последняя)."""
    await _open_profiles(pilot, scr)
    rows = scr._profiles_rows()
    scr._profiles_idx = len(rows) - 1
    scr._render_view()
    await pilot.pause()
    return rows


async def test_level_row_cycles_through_toggles_order_not_hardcoded(tmp_path, monkeypatch):
    """Крутка идёт по ПОРЯДКУ `TOGGLES["level"].values`, а не по хардкоду
    light→default→max, зашитому где-то в settings.py: переставленный порядок
    значений (тот же приём, что доказывает «источник — TOGGLES», без упора в
    отдельную схема-валидацию значения `level`) меняет следующее значение."""
    import dataclasses
    import tui.app as app_mod
    from tui.toggles import TOGGLES
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    reordered = tuple(reversed(TOGGLES["level"].values))   # max, default, light
    monkeypatch.setitem(TOGGLES, "level", dataclasses.replace(TOGGLES["level"], values=reordered))

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        scr._level_apply(1)              # "default" -> следующий ПО НОВОМУ порядку — "light"
        await pilot.pause()
        assert scr._config["level"] == "light"
        assert 'level: "light"' in base_cfg.read_text(encoding="utf-8")


async def test_level_right_cycles_writes_base_config_and_optimistic_config(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        assert (scr._config.get("level") or "default") == "default"
        await pilot.press("right")
        await pilot.pause(0.1)
        assert scr._config["level"] == "max"          # light -> default -> max
        assert 'level: "max"' in base_cfg.read_text(encoding="utf-8")


async def test_level_left_cycles_backward(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        await pilot.press("left")
        await pilot.pause(0.1)
        assert scr._config["level"] == "light"         # default -> light
        assert 'level: "light"' in base_cfg.read_text(encoding="utf-8")


async def test_level_enter_cycles_forward_same_as_right(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert scr._config["level"] == "max"


async def test_level_flash_names_the_target_file(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        scr._level_apply(1)
        await pilot.pause()
        flash = str(scr.query_one("#set-flash", Static).render())
        assert str(base_cfg) in flash
        assert "max" in flash


async def test_level_write_redirects_to_project_when_project_sets_pointer(tmp_path, monkeypatch):
    """Тот же случай, что и активация профиля рядом: проектный `.gc/config.yml`
    уже несёт указатель `profile:` — запись базового файла была бы перекрыта
    оверлеем проекта, поэтому уходит туда же."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    project_config = _write(project_dir / ".gc" / "config.yml",
                            'profile: "other"\nlevel: "light"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        target = scr._level_write_target()
        assert Path(target) == project_config
        scr._level_apply(1)
        assert 'level: "max"' in project_config.read_text(encoding="utf-8")
        assert 'level: default' in base_cfg.read_text(encoding="utf-8")   # базовый не тронут


async def test_level_write_stays_in_base_when_project_sets_only_profile_pointer(tmp_path, monkeypatch):
    """Указатель `profile:` проекта перекрывает выбор ПРОФИЛЯ, а не уровень —
    уводить из-за него запись `level` в проектный `.gc/config.yml` нельзя: это
    дописало бы проекту (и его репозиторию) ключ, которого он не просил, а сама
    запись в базовый конфиг проектным оверлеем не перекрывается."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    project_config = _write(project_dir / ".gc" / "config.yml", 'profile: "other"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        assert Path(scr._level_write_target()) == base_cfg
        scr._level_apply(1)
        await pilot.pause()
        assert "level" not in project_config.read_text(encoding="utf-8")
        assert 'level: "max"' in base_cfg.read_text(encoding="utf-8")


async def test_level_write_creates_missing_anchor_in_target(tmp_path, monkeypatch):
    """config.yml без строки `level:` (создан до появления ключа) не запирает
    переключение: якорь дописывается из пакетного шаблона — тем же приёмом, что
    у указателя профиля (`_write_profile_pointer`), потому что топ-уровневый
    скаляр сам `write_values` дописать не умеет."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      'profile: ""\n'
                      'backend:\n  completions_url: "http://host/v1/chat/completions"\n'
                      'agent:\n  model: "test-model"\n')

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        scr._level_apply(1)
        await pilot.pause()
        assert 'level: "max"' in base_cfg.read_text(encoding="utf-8")
        flash = str(scr.query_one("#set-flash", Static).render())
        assert "max" in flash


async def test_level_apply_reports_when_written_value_is_shadowed(tmp_path, monkeypatch):
    """Записать значение можно всегда, а подействовать оно может не везде: активный
    профиль задаёт свой `level` и перекрывает базу. Тогда честнее сказать, что
    действует, чем показать «установлен» и нарисовать строку/футер по значению,
    которого нет."""
    import tui.app as app_mod
    from config import load_config_ex
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("pinned"))
    _write(tmp_path / "base" / "profiles" / "pinned.yml", "level: light\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        def _reload() -> None:
            app._config = load_config_ex(str(base_cfg))[0]

        scr = _mk_screen(config=load_config_ex(str(base_cfg))[0],
                         config_path=base_cfg, on_apply=_reload)
        app.push_screen(scr)
        await pilot.pause()
        await _open_level_row(pilot, scr)
        scr._level_apply(1)          # переключаем от действующего light, но профиль держит light
        await pilot.pause()
        assert 'level: "max"' in base_cfg.read_text(encoding="utf-8")   # база из default стала max
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "действует «light»" in notice
        assert scr._config.get("level") == "light"


async def test_profiles_section_refuses_to_write_into_packaged_template(tmp_path, monkeypatch):
    """Запуск без пользовательского конфига (GC_HOME не настроен, `--init-config`
    не запускали): базой стал пакетный config.default.yml — ни активация профиля,
    ни деактивация, ни переключение уровня не смеют его править. Тот же guard, что
    у «Режимов работы» (`_modes_apply`), но по ЦЕЛИ записи: у профилей она бывает
    проектной."""
    import tui.app as app_mod
    import config as config_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    packaged = _write(tmp_path / "packaged" / "config.default.yml", _base_config_text("home"))
    _write(tmp_path / "packaged" / "profiles" / "home.yml",
          "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")
    monkeypatch.setattr(config_mod, "_default_config_path", lambda: str(packaged))
    cfg = dict(_base_dict(), profile="home", level="default")

    app = app_mod.TuiApp(config=dict(cfg))
    async with app.run_test() as pilot:
        scr = _mk_screen(config=cfg, config_path=packaged)
        app.push_screen(scr)
        await pilot.pause()
        rows = await _open_level_row(pilot, scr)

        scr._level_apply(1)
        await pilot.pause()
        home = next(r for r in rows if r.get("name") == "home")
        scr._activate_profile("home", home["ref"])
        scr._deactivate_profile()
        await pilot.pause()

        text = packaged.read_text(encoding="utf-8")
        assert "level: default" in text and 'profile: "home"' in text   # файл не тронут
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "пакетный шаблон" in notice


async def test_profiles_render_reads_each_profile_file_once(tmp_path, monkeypatch):
    """Сводка профиля (чтение + разбор YAML) — самая дорогая часть отрисовки, а
    строки за кадр спрашивают четыре независимых места: футер, монтирование,
    refresh строк и refresh подсказки. Кэш прохода: каждый файл читается ровно
    раз на кадр — и всё равно перечитывается на следующем кадре и на нажатии
    (правка профиля извне обязана быть видна сразу)."""
    import tui.app as app_mod
    import config_modules.profiles as profiles_mod

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profiles_dir = tmp_path / "base" / "profiles"
    for name in ("home", "work", "corp"):
        _write(profiles_dir / f"{name}.yml",
               "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")

    calls: list[str] = []
    real_summary = profiles_mod.profile_display_summary

    def _counting_summary(path):
        calls.append(str(path))
        return real_summary(path)

    monkeypatch.setattr(profiles_mod, "profile_display_summary", _counting_summary)

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_profiles_cursor_survives_row_disappearing_under_it(tmp_path, monkeypatch):
    """Набор строк перечитывается с диска на КАЖДОЕ нажатие и на каждый рендер, а
    файл профиля мог исчезнуть под ногами (правка/удаление извне) — курсор тогда
    указывает за конец, и голый `rows[idx]` уронил бы обработчик клавиш и хинт."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    profiles_dir = tmp_path / "base" / "profiles"
    _write(profiles_dir / "home.yml", "models:\n  glm:\n    url: http://y\nslots:\n  default: glm\n")
    _write(profiles_dir / "work.yml", "models:\n  flash:\n    url: http://x\nslots:\n  default: flash\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        rows = await _open_level_row(pilot, scr)
        assert len(rows) == 4                       # без профиля, home, work, уровень

        (profiles_dir / "home.yml").unlink()
        scr._profiles_idx = 3                       # строк осталось 3 — курсор за концом
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert scr._profiles_idx == 2

        scr._profiles_idx = 3
        assert scr._profiles_hint_text(scr._profiles_rows())
        assert scr._profiles_idx == 2


async def test_level_hint_names_action_and_target_file_keeps_session_tail(tmp_path, monkeypatch):
    """Подсказка больше не «витрина» — называет клавиши, целевой файл и хвост
    про сессионный `/level` (разделение ролей: секция пишет в файл, палитра —
    на сессию)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        rows = await _open_level_row(pilot, scr)
        hint = scr._profiles_hint_text(rows)
        assert "витрина" not in hint
        assert "←/→/Enter — переключить" in hint
        assert str(base_cfg) in hint
        assert "на сессию — /level" in hint


async def test_level_row_dot_color_matches_current_level_and_updates_on_apply(tmp_path, monkeypatch):
    """Task 12: между названием и значением строки уровня — КАНОННЫЙ ТРЕК
    тоггла (тот же приём, что `padded_track` в секции «Режимы работы» и
    палитре), не одиночная точка. Число позиций трека = числу значений
    `TOGGLES["level"].values` (единый источник — будущий авто-режим сделает
    трек четырёхпозиционным сам); горящая позиция окрашена по ТЕКУЩЕМУ
    уровню тем же языком, что футер; трек и цвет меняются вместе со
    значением при переключении."""
    import tui.app as app_mod
    from tui.toggles import TOGGLES, padded_track
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    spec = TOGGLES["level"]
    colors = {v.value: v.color for v in spec.values}
    base_w = 2 * len(spec.values) - 1

    def expected_track(level: str) -> str:
        return padded_track(spec, level, extra_lit=None, base_w=base_w, extra_w=0).plain

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        text = scr._level_row_text(selected=False)
        assert f"level · уровень модели по умолчанию" in text.plain
        assert expected_track("default") in text.plain
        assert text.plain.index(expected_track("default")) > text.plain.index(
            "level · уровень модели по умолчанию")
        assert text.plain.count("●") == 1   # ровно одна горящая позиция в треке
        styles = [str(s.style) for s in text.spans if "bold" in str(s.style).lower()]
        assert any(colors["default"].lower() in st.lower() for st in styles)

        await _open_level_row(pilot, scr)
        scr._level_apply(1)   # default -> max
        await pilot.pause()

        text2 = scr._level_row_text(selected=False)
        assert expected_track("max") in text2.plain
        assert "max" in text2.plain
        assert text2.plain.count("●") == 1
        styles2 = [str(s.style) for s in text2.spans if "bold" in str(s.style).lower()]
        assert any(colors["max"].lower() in st.lower() for st in styles2)
        assert not any(colors["default"].lower() in st.lower() for st in styles2)


async def test_level_row_matches_mode_row_grammar(tmp_path, monkeypatch):
    """Task 13 (решение живого смока 2026-08-10): строка уровня приведена к
    ГРАММАТИКЕ СТРОКИ целиком, как у ручек секции «Режимы работы»
    (`_mode_row_text`) — не по кусочкам (цвет точки/трек по отдельности, как
    было раньше). Порядок: «level · описание» одним блоком INK (выровнено по
    детерминированной ширине из `_level_columns`, тот же приём, что
    `_modes_columns`) -> канонный трек (Task 12) -> значение INK (не DIM,
    как было до этой задачи)."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()

        label_w, base_w, extra_w, value_w = scr._level_columns()
        text = scr._level_row_text(selected=False)
        plain = text.plain
        head = "level · уровень модели по умолчанию"

        head_i = plain.index(head)
        track_i = plain.index("●", head_i)
        value_i = plain.index("default", track_i)
        assert head_i < track_i < value_i   # порядок: имя·описание -> трек -> значение

        # голова выровнена по фиксированной ширине формулой (не на глаз) —
        # тот же контракт, что `label_w` в `_modes_columns`.
        assert plain[head_i:head_i + (label_w - 2)] == head.ljust(label_w - 2)

        # значение выровнено по value_w — тот же приём, что value_w в _mode_row_text
        assert plain[value_i:value_i + value_w] == "default".ljust(value_w)

        # стили ядра строки — как у НЕ-дочерней ручки в `_mode_row_text`:
        # голова и значение простым INK (не bold, не DIM).
        from tui.palette import DIM, INK
        head_span = next(s for s in text.spans if s.start == 2 and s.end == 2 + len(head.ljust(label_w - 2)))
        assert str(head_span.style) == INK
        value_span = next(
            s for s in text.spans if s.start == value_i and s.end == value_i + value_w)
        assert str(value_span.style) == INK
        assert str(value_span.style) != DIM


async def test_profiles_footer_is_positional_toggle_hint_on_level_row(tmp_path, monkeypatch):
    """Футер секции — позиционный (канон `_CONNECTION`): на строке профиля/«без
    профиля» — глагол активации, на строке уровня — вариант с
    «←/→/Enter — переключить»."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        rows = scr._profiles_rows()
        scr._profiles_idx = next(i for i, r in enumerate(rows) if r["kind"] == "profile")
        scr._render_view()
        await pilot.pause()
        footer_on_profile = str(scr.query_one("#set-footer", Static).render())
        assert "Space — активировать" in footer_on_profile
        assert "←/→/Enter — переключить" not in footer_on_profile

        scr._profiles_idx = len(rows) - 1
        scr._render_view()
        await pilot.pause()
        footer_on_level = str(scr.query_one("#set-footer", Static).render())
        assert "←/→/Enter — переключить" in footer_on_level
        assert "Space — активировать" not in footer_on_level


# --- Клавиша активации: Space (канон базы), Enter — тихий алиас -----------------

async def _cursor_to(scr, kind, name=None):
    """Курсор на строку нужного вида, с перерисовкой."""
    rows = scr._profiles_rows()
    scr._profiles_idx = next(
        i for i, r in enumerate(rows)
        if r["kind"] == kind and (name is None or r.get("name") == name))
    scr._render_view()


async def test_profiles_footer_promises_space_not_enter(tmp_path, monkeypatch):
    """Активация профиля — смена состояния, и по канону базы это Space (так же
    у MCP «вкл/выкл» и у отметки в post-update). Enter в базе значит «войти
    внутрь», поэтому глаголом активации футер его больше не называет."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_profiles_footer_on_none_row_names_deactivation(tmp_path, monkeypatch):
    """Строка «— без профиля» при АКТИВНОМ профиле снимает его — футер обязан
    называть это своим глаголом. Раньше он обещал «активировать», а подсказка в
    том же кадре говорила «деактивировать»: курсор при входе в секцию стоит
    ровно здесь, так что первое, что видел пользователь, было противоречие."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text("home"))
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    cfg = _base_dict()
    cfg["profile"] = "home"
    app = app_mod.TuiApp(config=cfg)
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


async def test_profiles_footer_on_none_row_is_silent_with_nothing_to_undo(tmp_path, monkeypatch):
    """Профиль не выбран — на этой строке делать нечего, и футер молчит о
    действии вовсе. Обещать «деактивировать» там, где деактивировать некого, —
    та же ложь, только с другим глаголом."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "none")
        await pilot.pause()
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "активировать" not in footer          # ни с приставкой, ни без
        assert "↑/↓ — выбор" in footer and "Esc — меню" in footer


async def test_space_activates_profile(tmp_path, monkeypatch):
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
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


async def test_enter_still_activates_as_quiet_alias(tmp_path, monkeypatch):
    """Enter продолжает активировать, но футер о нём молчит: пока волна B не
    привезла «вход в профиль», отнимать привычную клавишу не за что, а обещать
    её — значит закреплять расклад, который вот-вот сменится."""
    import tui.app as app_mod
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_cfg = _write(tmp_path / "base" / "config.yml", _base_config_text())
    _write(tmp_path / "base" / "profiles" / "home.yml", "models: {}\n")

    app = app_mod.TuiApp(config=_base_dict())
    async with app.run_test() as pilot:
        scr = _mk_screen(config=_base_dict(), config_path=base_cfg)
        app.push_screen(scr)
        await pilot.pause()
        await _open_profiles(pilot, scr)
        await _cursor_to(scr, "profile", "home")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert 'profile: "home"' in base_cfg.read_text(encoding="utf-8")

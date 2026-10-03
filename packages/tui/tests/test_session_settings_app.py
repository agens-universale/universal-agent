from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {
                "model": "M",
                "tui_finish_gate_mode": "on",
                "tui_text_answer_mode": "judge",
            }
        ),
    )


def test_effective_settings_applies_overrides(tmp_path):
    app = _app(tmp_path)
    assert app.effective_settings().settings.tui.finish_gate_mode == "on"
    app.set_session_override("tui.finish_gate_mode", "off")
    eff = app.effective_settings()
    assert eff.settings.tui.finish_gate_mode == "off"
    from gc_core.settings import is_explicit

    assert is_explicit(eff.settings, "tui.finish_gate_mode")
    # база не мутирована — переопределения отдельно
    assert app.base_settings().settings.tui.finish_gate_mode == "on"


def test_overrides_survive_config_reload(tmp_path, monkeypatch):
    """Сохранение любых настроек перечитывает файл; сессионные значения
    обязаны пережить это (иначе /settings молча сбрасывал бы режим)."""
    app = _app(tmp_path)
    app.set_session_override("tui.text_answer_mode", "allow")

    fresh = {
        "model": "fresh-model",
        "tui_finish_gate_mode": "on",
        "tui_text_answer_mode": "judge",
        "threads_data_path": str(tmp_path / "threads"),
        "profile": "",
    }
    from gc_core.config_modules.profiles import ProfileMeta

    from dataclasses import replace

    from gc_testkit.settings import make_settings

    monkeypatch.setattr(
        "gc_core.config.load_settings",
        lambda *_a, **_k: replace(
            make_settings(fresh),
            profile_meta=ProfileMeta(active_name=None, ref=None, mtime_set={}),
        ),
    )
    monkeypatch.setattr(
        "gc_core.config.apply_cli_overrides", lambda d, **_k: d
    )
    app.reload_config()

    assert (
        app.base_settings().settings.agent.model == "fresh-model"
    )  # база реально перечиталась мокнутым load_settings
    assert app.effective_settings().settings.tui.text_answer_mode == "allow"


def test_overrides_survive_reload_if_changed(tmp_path):
    """Сессионные переопределения обязаны пережить и freshness-обёртку
    (reload_config_if_changed), не только прямой reload_config: она
    делегирует в него же, но контракт («слой сессии остаётся сверху свежей
    базы») фиксируем тестом отдельно от механизма перечитывания."""
    from gc_tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )
    app._config_path = str(cfg_file)
    app.reload_config()  # база + снимок mtime установлены
    app.set_session_override("tui.text_answer_mode", "allow")

    import os
    import time

    cfg_file.write_text('tui:\n  finish_gate_mode: "off"\n', encoding="utf-8")
    t = time.time() + 5.0
    os.utime(cfg_file, (t, t))

    app.reload_config_if_changed()

    eff = app.effective_settings()
    assert (
        eff.settings.tui.finish_gate_mode == "off"
    )  # база реально перечиталась
    assert (
        eff.settings.tui.text_answer_mode == "allow"
    )  # сессия пережила перечитывание


def test_load_session_overrides_replaces_whole_set(tmp_path):
    """Открытие треда со своими значениями заменяет набор целиком; тред без
    значений (пустой dict) возвращает к умолчанию."""
    app = _app(tmp_path)
    app.set_session_override("tui.finish_gate_mode", "off")
    app.load_session_overrides({"tui.text_answer_mode": "deny"})
    eff = app.effective_settings()
    assert eff.settings.tui.text_answer_mode == "deny"
    assert (
        eff.settings.tui.finish_gate_mode == "on"
    )  # прежнее переопределение снято
    app.load_session_overrides({})
    assert app.effective_settings().settings.tui.text_answer_mode == "judge"


def test_apply_toggle_writes_session_not_config_file(tmp_path):
    """Переключение меняет сессию и метаданные треда, но НЕ config.yml."""
    from gc_tui.toggles import apply_toggle, toggle_state

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")

    app = _app(tmp_path)
    app._config_path = str(cfg_file)
    app.tui_state.active_task_id = "t1"

    assert apply_toggle(app, "gate", "off") == "off"
    assert toggle_state(app, "gate") == "off"
    assert (
        cfg_file.read_text(encoding="utf-8")
        == 'tui:\n  finish_gate_mode: "on"\n'
    )

    from gc_tui.services.stores.file_store import FileTaskStore

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    saved = (store.get_task_meta("t1").get("tui") or {}).get(
        "session_settings"
    )
    assert saved == {"tui.finish_gate_mode": "off"}


def test_apply_toggle_rolls_back_when_meta_write_fails(tmp_path, monkeypatch):
    """Запись метаданных упала — переопределение НЕ остаётся применённым.

    Вызывающие показывают «✗ не сохранено» и не перерисовывают строку: если
    значение при этом уже лежит в сессии, пользователю объявлено одно, а
    следующий ход агента идёт в другом режиме. Прежний путь (запись файла →
    reload) при неудачной записи не менял ничего — сохраняем это свойство.

    Отказ мокаем на РЕАЛЬНОЙ записи метаданных, а не на apply_toggle: мок самого
    apply_toggle этот дефект и пропустил."""
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.toggles import apply_toggle, toggle_state

    app = _app(tmp_path)
    app.tui_state.active_task_id = "t1"
    app.set_session_override(
        "tui.text_answer_mode", "allow"
    )  # уже применённое до сбоя

    def boom(self, task_id, patch, system=None):
        raise OSError("disk full")

    monkeypatch.setattr(FileTaskStore, "update_tui_meta", boom)

    with pytest.raises(OSError):
        apply_toggle(app, "gate", "off")

    # объявлено «не сохранено»: действует прежнее — явное "on" базы
    assert toggle_state(app, "gate") == "on"
    assert app.effective_settings().settings.tui.finish_gate_mode == "on"
    assert toggle_state(app, "answers") == "allow"  # чужое значение не снесено


def test_apply_toggle_without_active_thread_keeps_session(tmp_path):
    """Тред ещё не создан — переключение живёт в памяти и не падает."""
    from gc_tui.toggles import apply_toggle, toggle_state

    app = _app(tmp_path)
    app.tui_state.active_task_id = None
    assert apply_toggle(app, "answers", "allow") == "allow"
    assert toggle_state(app, "answers") == "allow"


@pytest.mark.parametrize(
    "global_val,override,expected",
    [
        ("on", None, "on"),
        ("on", "off", "off"),
        ("off", None, "off"),
        ("off", "on", "on"),
    ],
)
def test_effective_gate_matrix_global_x_override(
    tmp_path, global_val, override, expected
):
    """Матрица (глобал × сессионный оверрайд) эффективного /gate (дизайн
    2026-07-21 §2, гейт §9): без оверрайда действует глобал; с оверрайдом —
    оверрайд. Ключ присутствует в файле → поле задано явно (`is_explicit`,
    model_fields_set), поэтому глобальный «off» реально действует."""
    from gc_tui.app import TuiApp
    from gc_tui.toggles import toggle_state

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        f'tui:\n  finish_gate_mode: "{global_val}"\n', encoding="utf-8"
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({}),
    )
    app._config_path = str(cfg_file)
    app.reload_config()
    if override is not None:
        app.set_session_override("tui.finish_gate_mode", override)
    assert toggle_state(app, "gate") == expected


def test_apply_session_overrides_applies_pair(tmp_path):
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.session_settings import read_from_meta

    app = _app(tmp_path)
    app.tui_state.active_task_id = "t1"
    app.apply_session_overrides(
        {
            "tui.finish_gate_mode": "smart",
            "tui.text_answer_mode": "judge",
        }
    )
    eff = app.effective_settings()
    assert eff.settings.tui.finish_gate_mode == "smart"
    assert eff.settings.tui.text_answer_mode == "judge"
    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    saved = read_from_meta(store.get_task_meta("t1"))
    assert saved == {
        "tui.finish_gate_mode": "smart",
        "tui.text_answer_mode": "judge",
    }


def test_clear_session_overrides_removes_pair(tmp_path):
    app = _app(tmp_path)
    app.tui_state.active_task_id = "t1"
    app.apply_session_overrides(
        {
            "tui.finish_gate_mode": "off",
            "tui.text_answer_mode": "allow",
        }
    )
    app.clear_session_overrides(
        ("tui.finish_gate_mode", "tui.text_answer_mode")
    )
    # оверрайдов нет — эффективный конфиг = база (ключи сессии сняты)
    assert app._session_overrides == {}


def test_apply_session_overrides_rolls_back_on_meta_failure(
    tmp_path, monkeypatch
):
    from gc_tui.services.stores.file_store import FileTaskStore

    app = _app(tmp_path)
    app.tui_state.active_task_id = "t1"
    app.set_session_override(
        "tui.text_answer_mode", "deny"
    )  # уже применённое до сбоя

    def boom(self, task_id, patch, system=None):
        raise OSError("disk full")

    monkeypatch.setattr(FileTaskStore, "update_tui_meta", boom)

    with pytest.raises(OSError):
        app.apply_session_overrides(
            {
                "tui.finish_gate_mode": "on",
                "tui.text_answer_mode": "allow",
            }
        )
    # откат целиком: пара не осела, прежний одиночный оверрайд цел
    assert app._session_overrides == {"tui.text_answer_mode": "deny"}


def test_clear_session_overrides_noop_when_nothing_to_clear(
    tmp_path, monkeypatch
):
    from gc_tui.services.stores.file_store import FileTaskStore

    app = _app(tmp_path)
    app.tui_state.active_task_id = "t1"
    calls = {"n": 0}

    def counting(self, task_id, patch, system=None):
        calls["n"] += 1

    monkeypatch.setattr(FileTaskStore, "update_tui_meta", counting)

    app.clear_session_overrides(("tui.finish_gate_mode",))  # нечего снимать
    assert calls["n"] == 0  # persist не звался

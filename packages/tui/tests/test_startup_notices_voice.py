"""voice-уведомления: секция пустая — тишина; заполнена криво/без extra — warning.

Набор переехал в TUI вместе с самими уведомлениями: их собирает `voice_notices` и
передаёт в сборку параметром `extra`. Ядро голосовых уведомлений больше не делает —
это стережёт `test_core_alone_collects_no_voice_notices`.
"""

from gc_core.startup_notices import collect_startup_notices
from gc_tui.services.voice_notices import voice_notices


def _collect(voice):
    cfg = {"voice": voice}
    return collect_startup_notices(cfg, set(), None, is_tui=True, extra=voice_notices(cfg))


def test_empty_voice_section_is_silent(monkeypatch):
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: False)
    assert _collect({"provider": "openai", "url": "", "token": ""}) == []


def test_enabled_voice_without_sounddevice_warns(monkeypatch):
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: False)
    # load_state гасим явно: доп. замечания собираются теперь и без аудио-стека, и без
    # заглушки тест читал бы РЕАЛЬНУЮ установку машины — то есть врал бы через раз.
    monkeypatch.setattr("gc_tui.voice_runtime.state.load_state", lambda: None)
    notices = _collect({"provider": "openai", "url": "https://x", "token": "t"})
    assert [n.code for n in notices] == ["voice_no_sounddevice"]
    assert notices[0].level == "warning"
    assert "libportaudio2" in notices[0].message


def test_missing_sounddevice_does_not_hide_the_other_voice_notices(monkeypatch):
    """Отсутствие аудио-стека не имеет права перекрывать остальные замечания.

    Раньше ветка `voice_no_sounddevice` возвращала свой Notice и не доходила до
    `_extras`. Машина без libportaudio2 — ровно та, где полусломанная локальная
    установка вероятнее всего, а человек о ней не узнавал (ревью 2026-08-07)."""
    from gc_tui.voice_runtime.state import VoiceState

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: False)
    monkeypatch.setattr(
        "gc_tui.voice_runtime.state.load_state",
        lambda: VoiceState(version="v0.8.3", root="C:/nonexistent/speaches", port=8735, model="m"),
    )
    cfg = {
        "voice": {"provider": "openai", "url": "http://127.0.0.1:8735/v1/audio/transcriptions", "token": "local"},
        "voice_overridden_by_managed": "https://api.groq.com/x",
    }
    notices = collect_startup_notices(cfg, set(), None, is_tui=True, extra=voice_notices(cfg))
    codes = [n.code for n in notices]
    assert "voice_no_sounddevice" in codes
    assert "voice_overridden" in codes  # раньше терялось
    assert "voice_install_broken" in codes  # и это тоже


def test_enabled_voice_with_sounddevice_is_silent(monkeypatch):
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    assert _collect({"provider": "openai", "url": "https://x", "token": "t"}) == []


def test_partially_filled_voice_warns(monkeypatch):
    """openai без url (один токен) — секция начата, но работать нечему."""
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    notices = _collect({"provider": "openai", "url": "", "token": "t"})
    assert [n.code for n in notices] == ["voice_incomplete"]
    assert "нужен url" in notices[0].message
    assert "token" not in notices[0].message  # токен для openai необязателен


def test_openai_without_token_is_not_incomplete(monkeypatch):
    """Локальный сервис/открытый endpoint не требуют токена — не замечание."""
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    assert _collect({"provider": "openai", "url": "http://127.0.0.1:8735/v1/audio/transcriptions", "token": ""}) == []


def test_token_without_url_warns_incomplete(monkeypatch):
    """Токен есть, адреса нет — работать не с чем: голос молчит, и человек должен
    узнать почему."""
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    notices = _collect({"provider": "openai", "url": "", "token": "t"})
    assert [n.code for n in notices] == ["voice_incomplete"]
    assert "нужен url" in notices[0].message


def test_core_alone_collects_no_voice_notices(monkeypatch):
    """Ядро само голосовых уведомлений не производит — их приносит фронтенд.

    Раньше это выражалось флагом `is_tui` внутри ядра; теперь — тем, что `gc-cli`
    просто не передаёт `extra`. Сторож на месте прежнего `test_cli_mode_collects_nothing`:
    без него переезд мог бы оставить в ядре забытую голосовую ветку."""
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: False)
    assert (
        collect_startup_notices(
            {"voice": {"provider": "openai", "url": "https://x", "token": "t"}},
            set(),
            None,
            is_tui=True,
        )
        == []
    )


def test_voice_overridden_notice(monkeypatch):
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    monkeypatch.setattr("gc_tui.voice_runtime.state.load_state", lambda: None)
    cfg = {
        "voice": {"provider": "openai", "url": "http://127.0.0.1:8735/v1/audio/transcriptions", "token": "local"},
        "voice_overridden_by_managed": "https://api.groq.com/x",
    }
    notices = collect_startup_notices(cfg, set(), None, is_tui=True, extra=voice_notices(cfg))
    codes = [n.code for n in notices]
    assert "voice_overridden" in codes
    n = next(n for n in notices if n.code == "voice_overridden")
    assert n.level == "info"
    assert "gc-voice remove" in n.message


def test_voice_install_broken_notice(monkeypatch):
    from gc_tui.voice_runtime.state import VoiceState

    monkeypatch.setattr(
        "gc_tui.voice_runtime.state.load_state",
        lambda: VoiceState(version="v0.8.3", root="C:/nonexistent/speaches", port=8735, model="m"),
    )
    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    cfg = {"voice": {"provider": "openai", "url": "http://127.0.0.1:8735/v1/audio/transcriptions", "token": "local"}}
    notices = collect_startup_notices(cfg, set(), None, is_tui=True, extra=voice_notices(cfg))
    assert "voice_install_broken" in [n.code for n in notices]

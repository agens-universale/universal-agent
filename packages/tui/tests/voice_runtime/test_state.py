"""VoiceState: json round-trip, дефолт None, устойчивость к битому файлу."""

from gc_tui.voice_runtime import paths as vpaths
from gc_tui.voice_runtime.state import (
    DEFAULT_MODEL,
    DEFAULT_PORT,
    SPEACHES_PIN,
    VoiceState,
    clear_state,
    current_model_short,
    load_state,
    save_state,
)


def test_defaults_constants():
    assert SPEACHES_PIN == "v0.8.3"


def test_available_update_names_the_pin_only_when_behind():
    """Правило сверки версии — общее для экранов и CLI, поэтому живёт в рантайме."""
    from gc_tui.voice_runtime.state import available_update

    old = VoiceState(version="v0.0.1-old", root="/x", port=8735, model="m")
    same = VoiceState(version=SPEACHES_PIN, root="/x", port=8735, model="m")
    assert available_update(old) == SPEACHES_PIN
    assert available_update(same) is None
    assert available_update(None) is None      # не установлен — обновлять нечего
    assert DEFAULT_PORT == 8735
    assert DEFAULT_MODEL == "Systran/faster-whisper-small"


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path)
    monkeypatch.setattr("gc_tui.voice_runtime.state.voice_state_path",
                        lambda: tmp_path / "state.json")
    assert load_state() is None
    st = VoiceState(version="v0.8.3", root=str(tmp_path / "speaches"),
                    port=8735, model=DEFAULT_MODEL)
    save_state(st)
    assert load_state() == st
    clear_state()
    assert load_state() is None


def test_broken_state_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    monkeypatch.setattr("gc_tui.voice_runtime.state.voice_state_path", lambda: p)
    p.write_text("{broken", encoding="utf-8")
    assert load_state() is None


def test_current_model_short_default_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr("gc_tui.voice_runtime.state.voice_state_path", lambda: tmp_path / "state.json")
    assert current_model_short() == "faster-whisper-small"


def test_current_model_short_uses_installed_state_model(tmp_path, monkeypatch):
    monkeypatch.setattr("gc_tui.voice_runtime.state.voice_state_path", lambda: tmp_path / "state.json")
    save_state(VoiceState(version="v0.8.3", root=str(tmp_path / "speaches"),
                          port=8735, model="Org/custom-model"))
    assert current_model_short() == "custom-model"


def test_paths_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(vpaths, "_gc_data", lambda: tmp_path)
    assert vpaths.voice_data_dir() == tmp_path / "voice"
    assert vpaths.speaches_root() == tmp_path / "voice" / "speaches"
    assert vpaths.voice_pids_dir() == tmp_path / "voice" / "pids"
    assert vpaths.voice_leases_dir() == tmp_path / "voice" / "leases"
    py = vpaths.speaches_python()
    assert str(py).startswith(str(tmp_path / "voice" / "speaches"))
    assert py.name in ("python.exe", "python")

"""Wire voice-секции в managed config.local.yml + детект конфликта с base."""
import yaml

from gc_tui.voice_runtime import wire


def _patch_managed(monkeypatch, tmp_path):
    path = tmp_path / "config.local.yml"
    monkeypatch.setattr("recommended_mcp.runtime.wiring.managed_config_path", lambda: path)
    return path


def test_wire_and_unwire_roundtrip(monkeypatch, tmp_path):
    path = _patch_managed(monkeypatch, tmp_path)
    assert wire.managed_voice() is None
    wire.wire_voice(8735, "Systran/faster-whisper-small")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["voice"]["url"] == "http://127.0.0.1:8735/v1/audio/transcriptions"
    assert data["voice"]["provider"] == "openai"
    assert data["voice"]["token"] == "local"
    assert data["voice"]["model"] == "Systran/faster-whisper-small"
    assert data["voice"]["language"] == "ru"
    assert wire.managed_voice()["url"].endswith(":8735/v1/audio/transcriptions")
    assert wire.unwire_voice() is True
    assert wire.managed_voice() is None
    assert wire.unwire_voice() is False


def test_wire_preserves_other_managed_keys(monkeypatch, tmp_path):
    path = _patch_managed(monkeypatch, tmp_path)
    path.write_text(yaml.safe_dump({"mcp": {"servers": {"x": {"transport": "http"}}}}),
                    encoding="utf-8")
    wire.wire_voice(8735, "m")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["mcp"]["servers"]["x"]["transport"] == "http"   # не потеряли MCP
    assert data["voice"]["model"] == "m"


def test_wire_voice_writes_given_language(monkeypatch, tmp_path):
    """Язык распознавания — часть voice-секции; локальный режим правит его в /settings,
    и wire обязан уметь положить в managed именно выбранный (дефолт схемы — ru)."""
    path = _patch_managed(monkeypatch, tmp_path)
    wire.wire_voice(8735, "m", "en")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["voice"]["language"] == "en"
    wire.wire_voice(8735, "m")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["voice"]["language"] == "ru"


def test_managed_language_reads_back_user_choice(monkeypatch, tmp_path):
    """Язык — единственное в managed-секции, что выбирает человек: повторный wire
    (переустановка, возврат в локальный режим) обязан его переносить, а не терять."""
    _patch_managed(monkeypatch, tmp_path)
    assert wire.managed_language() == "ru"          # managed-секции ещё нет
    wire.wire_voice(8735, "m", "en")
    assert wire.managed_language() == "en"
    wire.wire_voice(8735, "m", wire.managed_language())
    assert wire.managed_voice()["language"] == "en"


def test_user_voice_conflict(monkeypatch, tmp_path):
    _patch_managed(monkeypatch, tmp_path)
    groq = {"voice": {"provider": "openai",
                      "url": "https://api.groq.com/openai/v1/audio/transcriptions",
                      "token": "gsk"}}
    assert wire.user_voice_conflict(groq, 8735) == \
        "https://api.groq.com/openai/v1/audio/transcriptions"
    ours = {"voice": {"url": "http://127.0.0.1:8735/v1/audio/transcriptions",
                      "token": "local"}}
    assert wire.user_voice_conflict(ours, 8735) is None      # это же наш порт
    assert wire.user_voice_conflict({}, 8735) is None          # секции нет
    assert wire.user_voice_conflict({"voice": {"url": "", "token": ""}}, 8735) is None


def test_broken_port_does_not_crash_the_loader(monkeypatch, tmp_path):
    """Опечатка в порту не имеет права ронять запуск агента.

    Сверка зовётся из `config.load_config` на каждом старте, как только прописана
    локальная батарейка. `urlsplit(...).port` на «:87x5» бросает ValueError — и агент
    падал сырым исключением вместо предупреждения о конфиге (ревью 2026-08-07,
    воспроизведено сквозным прогоном load_config)."""
    _patch_managed(monkeypatch, tmp_path)
    typo = {"voice": {"url": "http://localhost:87x5/v1/audio/transcriptions"}}
    # не падает — и считает адрес ЧУЖИМ: перекрытие человеку показать надо
    assert wire.user_voice_conflict(typo, 8735) == \
        "http://localhost:87x5/v1/audio/transcriptions"
    # мусор с обеих сторон тоже не роняет
    from gc_core.utils.url_addr import same_service
    assert same_service("http://localhost:87x5/v1", "http://127.0.0.1:8x/v1") is False


def test_user_voice_conflict_accepts_any_local_form(monkeypatch, tmp_path):
    """Адрес батарейки человек мог вписать как localhost, а сама она пишет 127.0.0.1 —
    сервис один, конфликта нет. Иначе установка и старт скажут разное об одном факте."""
    _patch_managed(monkeypatch, tmp_path)
    same = {"voice": {"url": "http://localhost:8735/v1/audio/transcriptions"}}
    assert wire.user_voice_conflict(same, 8735) is None
    other_port = {"voice": {"url": "http://127.0.0.1:9999/v1/audio/transcriptions"}}
    assert wire.user_voice_conflict(other_port, 8735) ==         "http://127.0.0.1:9999/v1/audio/transcriptions"

# tests/tui/test_voice_setup.py
"""Фасад voice_setup: снапшот статуса, маршрутизация слоёв, probe."""

import pytest

# Захват ДО фикстур. Autouse `_no_network_model_fetch` из conftest (приехала с
# develop, коммит 263eb403) подменяет сам `model_picker.fetch_models` на
# постоянный ответ, и нижний шов транспорта до дела не доходит. Тестовые модули
# импортируются на СБОРЕ, то есть раньше фикстур, — здесь ещё настоящая функция.
from gc_core.model_picker import fetch_models as _REAL_FETCH_MODELS
from gc_tui.services import voice_setup as vs
from gc_tui.voice_runtime.state import SPEACHES_PIN, VoiceState

# Версия — ОТ пина, а не литералом: иначе бамп пина превращал бы «установлен и свеж» в
# «устарел», и падали бы тесты, к обновлению отношения не имеющие.
ST = VoiceState(version=SPEACHES_PIN, root="/x", port=8735, model="Systran/faster-whisper-small")
ST_OLD = VoiceState(version="v0.0.1-old", root="/x", port=8735, model="Systran/faster-whisper-small")


def _cfg(url=""):
    return {"voice": {"url": url, "token": "local", "model": "m"}}


def test_status_local(monkeypatch):
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    monkeypatch.setattr(vs, "_port_alive", lambda st: True)
    s = vs.status_snapshot(_cfg("http://127.0.0.1:8735/v1/audio/transcriptions"))
    assert s == vs.VoiceStatus(
        installed=True, service_alive=True, port=8735, model="Systran/faster-whisper-small", mode="local"
    )


def test_engine_update_only_when_behind(monkeypatch):
    """Правило сверки версии — одно на продукт: его читают и строка в /settings, и
    клавиша обновления, и экран после апгрейда агента."""
    from gc_tui.voice_runtime import state as state_mod

    monkeypatch.setattr(state_mod, "load_state", lambda: ST_OLD)
    assert vs.engine_update() == ("v0.0.1-old", SPEACHES_PIN)
    monkeypatch.setattr(state_mod, "load_state", lambda: ST)
    assert vs.engine_update() is None  # уже на версии сборки
    monkeypatch.setattr(state_mod, "load_state", lambda: None)
    assert vs.engine_update() is None  # не установлен


def test_status_carries_the_available_version(monkeypatch):
    """Снимок несёт факт устаревания сам: строка, футер и гейт клавиши обязаны судить
    по ОДНОМУ значению, а не пересчитывать каждый по-своему."""
    monkeypatch.setattr(vs, "_port_alive", lambda st: True)
    monkeypatch.setattr(vs, "_load_state", lambda: ST_OLD)
    s = vs.status_snapshot(_cfg("http://127.0.0.1:8735/v1/audio/transcriptions"))
    assert s.available_version == SPEACHES_PIN
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    assert vs.status_snapshot(_cfg("http://127.0.0.1:8735/v1/audio/transcriptions")).available_version is None


def test_status_cloud(monkeypatch):
    monkeypatch.setattr(vs, "_load_state", lambda: None)
    s = vs.status_snapshot(_cfg("https://api.example/v1/audio/transcriptions"))
    assert s.mode == "cloud" and not s.installed and s.port is None


def test_status_off(monkeypatch):
    monkeypatch.setattr(vs, "_load_state", lambda: None)
    assert vs.status_snapshot({"voice": {}}).mode == "off"


def test_status_installed_with_empty_url_is_off(monkeypatch):
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    monkeypatch.setattr(vs, "_port_alive", lambda st: True)
    s = vs.status_snapshot(_cfg())
    assert s.mode == "off" and s.installed is True


def test_install_local_feeds_base_config_to_conflict_check(monkeypatch):
    """Без base_raw install видит пустой конфиг, конфликта не находит и МОЛЧА
    перекрывает настроенный человеком voice.url. Предупреждение обязано доходить и
    из мастера, и из /settings — там выбор и делается, а не в CLI."""
    seen = {}
    base = {"voice": {"url": "https://api.groq.com/openai/v1/audio/transcriptions"}}
    monkeypatch.setattr(vs, "base_config_raw", lambda: base)

    def fake_install(base_raw=None, echo=print):
        seen["base_raw"] = base_raw
        return None

    monkeypatch.setattr("gc_tui.voice_runtime.install.install", fake_install)
    assert vs.install_local(echo=lambda *a: None) is True
    assert seen["base_raw"] == base


def test_switch_to_local_installs_only_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(vs, "_load_state", lambda: None)
    monkeypatch.setattr(vs, "install_local", lambda echo=print: calls.append("install") or True)
    monkeypatch.setattr(vs, "_wire_local", lambda config=None: calls.append("wire"))
    assert vs.switch_to_local() is True
    assert calls == ["install"]  # install сам wire'ит

    calls.clear()
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    assert vs.switch_to_local() is True
    assert calls == ["wire"]  # уже установлен — только wire


def _catch_wire(monkeypatch):
    wired = {}
    monkeypatch.setattr(
        vs, "wire_voice", lambda port, model, language="ru": wired.update(port=port, model=model, language=language)
    )
    return wired


def test_wire_local_keeps_language_from_managed(monkeypatch):
    """Возврат в локальный режим переписывает managed-секцию целиком — язык, выбранный
    в /settings, обязан пережить эту перезапись."""
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "en")
    wired = _catch_wire(monkeypatch)
    vs._wire_local()
    assert wired == {"port": 8735, "model": "Systran/faster-whisper-small", "language": "en"}


def test_local_language_falls_back_to_effective_config(monkeypatch):
    """Переезд в облако сносит managed-секцию целиком (wire_cloud делает unwire) —
    язык там больше не лежит. Возврат в локальный режим не имеет права молча
    сбросить его на дефолт: берём из ДЕЙСТВУЮЩЕГО конфига."""
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "")
    assert vs.local_language({"voice": {"language": "en"}}) == "en"
    assert vs.local_language({"voice": {}}) == "ru"  # нигде нет — дефолт схемы
    assert vs.local_language() == "ru"
    # managed жив — он и владелец: конфиг его не перебивает
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "de")
    assert vs.local_language({"voice": {"language": "en"}}) == "de"


def test_switch_to_local_carries_language_through_the_cloud_trip(monkeypatch):
    """Сквозной путь L1: язык был в managed → ушли в облако (unwire) → вернулись —
    managed снова с ним, а не с «ru»."""
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    wired = _catch_wire(monkeypatch)
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "en")
    vs.switch_to_local()
    assert wired["language"] == "en"

    written = {}
    monkeypatch.setattr(vs, "_write_values", lambda path, values, **kw: written.update(values) or [])
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "")  # unwire снёс секцию
    monkeypatch.setattr(vs, "_unwire_managed", lambda: True)
    vs.wire_cloud("cfg.yml", "https://x/v1/audio/transcriptions", "sk", "wh", language="en")
    resolved = {"voice": {"language": written["voice.language"]}}  # так его видит load_config
    vs.switch_to_local(config=resolved)
    assert wired["language"] == "en"


def test_set_local_language_rewires_managed_with_state_values(monkeypatch):
    """Язык локального режима живёт в managed-слое (его пишем мы): пишем тем же wire,
    остальные поля секции берутся из state — они не могут разъехаться с сервисом."""
    monkeypatch.setattr(vs, "_load_state", lambda: ST)
    wired = _catch_wire(monkeypatch)
    assert vs.set_local_language("en") is True
    assert wired == {"port": 8735, "model": "Systran/faster-whisper-small", "language": "en"}
    assert vs.set_local_language("") is True
    assert wired["language"] == "ru"  # пустое поле — дефолт схемы, а не пустой язык


def test_local_language_reads_managed_layer(monkeypatch):
    """Показать язык локального режима — значит показать managed-слой: он его владелец."""
    monkeypatch.setattr(vs, "_managed_language", lambda default="ru": "en")
    assert vs.local_language() == "en"


def test_set_local_language_without_battery_does_nothing(monkeypatch):
    monkeypatch.setattr(vs, "_load_state", lambda: None)
    wired = _catch_wire(monkeypatch)
    assert vs.set_local_language("en") is False
    assert wired == {}


def test_wire_cloud_unwires_managed_and_writes_config(monkeypatch, tmp_path):
    calls = []
    written = {}

    def fake_write(path, values, **kw):
        written.update(values)
        calls.append("write")
        return []

    monkeypatch.setattr(vs, "_write_values", fake_write)
    monkeypatch.setattr(vs, "_unwire_managed", lambda: calls.append("unwire") or True)
    vs.wire_cloud(tmp_path / "config.yml", "https://x/v1/audio/transcriptions", "sk", "wh-1")
    assert calls == ["write", "unwire"]  # запись раньше отключения managed
    assert written == {
        "voice.url": "https://x/v1/audio/transcriptions",
        "voice.token": "sk",
        "voice.model": "wh-1",
        "voice.language": "ru",
    }


def test_wire_cloud_raises_on_missing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(vs, "_write_values", lambda path, values, **kw: ["voice.url"])
    monkeypatch.setattr(vs, "_unwire_managed", lambda: calls.append("unwire") or True)
    with pytest.raises(ValueError):
        vs.wire_cloud(tmp_path / "config.yml", "https://x/v1/audio/transcriptions", "sk", "wh-1")
    assert "unwire" not in calls  # провал записи managed не трогает


def test_probe_ok_and_error(monkeypatch):
    class R:
        status_code = 200

    monkeypatch.setattr(vs, "_post", lambda url, **kw: R())
    r = vs.probe_endpoint("https://x", "t", "m")
    assert r.ok and r.latency_ms is not None and r.error is None

    def boom(url, **kw):
        raise OSError("refused")

    monkeypatch.setattr(vs, "_post", boom)
    r = vs.probe_endpoint("https://x", "t", "m")
    assert not r.ok and "refused" in r.error


def test_probe_respects_verify_ssl(monkeypatch):
    """Самоподписанный корп-сертификат: транскрибация и список моделей идут с
    voice.verify_ssl, проверка обязана идти с ним же — иначе Tab отвечает «ошибка TLS»
    на связке, которая на деле работает."""
    seen = {}

    class R:
        status_code = 200

    monkeypatch.setattr(vs, "_post", lambda url, **kw: seen.update(kw) or R())
    vs.probe_endpoint("https://corp/v1/audio/transcriptions", "t", "m", verify="/ca.pem")
    assert seen["verify"] == "/ca.pem"


def test_remove_local_reports_a_locked_leftover(monkeypatch):
    """Файлы заняты — окружение осталось на диске. Сказать «удалено» нельзя: статус
    уже переключится на «не установлен», и добить остаток человеку будет нечем."""
    monkeypatch.setattr("gc_tui.voice_runtime.cli._cmd_remove", lambda: 1)
    assert vs.remove_local() is False


def test_wire_cloud_writes_language(monkeypatch, tmp_path):
    """Язык распознавания — часть облачной настройки (правится в /settings);
    дефолт схемы `ru`, чтобы не переписывать смысл конфига молча."""
    written = {}
    monkeypatch.setattr(vs, "_write_values", lambda path, values, **kw: written.update(values) or [])
    monkeypatch.setattr(vs, "_unwire_managed", lambda: True)
    vs.wire_cloud(tmp_path / "config.yml", "https://x/v1/audio/transcriptions", "sk", "wh-1", language="en")
    assert written["voice.language"] == "en"

    written.clear()
    vs.wire_cloud(tmp_path / "config.yml", "https://x/v1/audio/transcriptions", "sk", "wh-1")
    assert written["voice.language"] == "ru"


def _stub_model_transport(monkeypatch, handler):
    """Заглушить транспорт пикера моделей ЦЕЛИКОМ.

    Затыкаем оба транспорта сразу — и requests-путь (сейчас дефолтный), и urllib
    (legacy, живёт для тестов): транспорт уже менялся однажды, и тест, прибитый к
    одному из них, не падает, а молча уходит в реальную сеть на несуществующий хост
    (ловилось после мержа: два теста висели по полторы минуты на DNS-таймаутах).
    `handler(url, headers, verify)` возвращает payload либо бросает — как urllib-шов.
    """
    from gc_core import model_picker as mp

    def _requests_get_ex(url, headers, verify, *, config_proxy=None):
        try:
            return mp.HttpReply(status=200, payload=handler(url, headers, verify))
        except OSError as exc:  # сеть недоступна
            return mp.HttpReply(status=None, payload=None, network_error=str(exc))
        except Exception:  # сервер ответил не тем
            return mp.HttpReply(status=404, payload=None)

    # Единственный сетевой шов `fetch_models` — `_requests_get_ex`. Прежде рядом
    # подменялся ещё и `_urllib_get` (старый фолбэк): его удалили как мёртвый код,
    # и подмена несуществующего имени роняла бы тест на AttributeError.
    # Сперва снимаем сетевой намордник набора: без возврата настоящей
    # реализации наша подмена транспорта не исполнялась бы вовсе — оба теста
    # получали `test-model` и там, где ждут имена моделей, и там, где ждут None.
    monkeypatch.setattr(mp, "fetch_models", _REAL_FETCH_MODELS)
    monkeypatch.setattr(mp, "_requests_get_ex", _requests_get_ex)


def test_list_models_sends_bearer_and_returns_ids(monkeypatch):
    """Список моделей endpoint'а — тем же сырым правилом авторизации, что и probe
    (`Bearer <токен из поля>`), и по тому же корню API (суффикс аудио-метода стрижёт
    `api_base`). Пустой токен = без заголовка (открытый/локальный сервис)."""
    seen = []

    def _get(url, headers, verify):
        seen.append((url, headers.get("Authorization"), verify))
        if url.endswith("/config"):
            raise RuntimeError("404")
        return {"data": [{"id": "whisper-1"}, {"id": "gpt-4o"}]}

    _stub_model_transport(monkeypatch, _get)
    got = vs.list_models("https://x/v1/audio/transcriptions", "sk", verify=False)
    assert got == ["whisper-1", "gpt-4o"]  # фильтр релевантности — уже в UI
    assert seen[-1] == ("https://x/v1/models", "Bearer sk", False)

    seen.clear()
    vs.list_models("https://x/v1/audio/transcriptions", "")
    assert seen[-1] == ("https://x/v1/models", None, True)


def test_list_models_tells_failure_from_an_empty_answer(monkeypatch):
    """None (не достучались) и [] (ответил, но моделей нет) — разные сообщения человеку."""

    def _down(url, headers, verify):
        raise OSError("refused")

    _stub_model_transport(monkeypatch, _down)
    assert vs.list_models("https://x/v1/audio/transcriptions", "sk") is None

    _stub_model_transport(monkeypatch, lambda url, headers, verify: {"data": []} if url.endswith("/models") else {})
    assert vs.list_models("https://x/v1/audio/transcriptions", "sk") == []


def test_status_cheap_never_touches_the_network(monkeypatch, tmp_path):
    """Дешёвый снимок обязан обходиться без сокетов: его зовёт UI-поток на каждый
    рендер меню, а connect к мёртвому порту на машинах с молчаливым файрволом сгорает
    по полному таймауту (~0.5 с, замерено) — и каждый ход курсора замерзал."""
    import socket

    from gc_tui.services import voice_setup as vs

    def _boom(*a, **k):
        raise AssertionError("status_cheap полез в сеть")

    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(vs, "_load_state", lambda: None)
    s = vs.status_cheap({"voice": {"url": "https://s.x/v1/audio/transcriptions"}})
    assert s.mode == "cloud" and s.installed is False

    state = type("St", (), {"port": 8735, "model": "m", "version": "v0.8.3"})()
    monkeypatch.setattr(vs, "_load_state", lambda: state)
    monkeypatch.setattr(vs, "_available_update", lambda st: None)
    s = vs.status_cheap({"voice": {"url": "https://s.x/v1/audio/transcriptions"}})
    assert s.installed is True
    assert s.service_alive is None  # живость не мерялась — её приносит воркер


def test_probe_alive_is_the_only_socket_path(monkeypatch):
    """probe_alive меряет порт (для воркера); без установленного сервиса — None."""
    from gc_tui.services import voice_setup as vs

    monkeypatch.setattr(vs, "_load_state", lambda: None)
    assert vs.probe_alive() is None

    state = type("St", (), {"port": 8735})()
    calls = []
    monkeypatch.setattr(vs, "_load_state", lambda: state)
    monkeypatch.setattr(vs, "_port_alive", lambda st: calls.append(st) or True)
    assert vs.probe_alive() is True and calls == [state]


def _update_stubs(monkeypatch, *, install_raises=None):
    """Общая обвязка `update_local`: состояние, гашение, установка, подъём.

    Возвращает журнал — по нему видно, чем кончилось обновление и подняли ли сервис."""
    log = {"stopped": 0, "installed": 0, "ensured": 0}

    def _stop():
        log["stopped"] += 1
        return True  # сервис РАБОТАЛ — его гасят на время обновления

    def _install(**kw):
        log["installed"] += 1
        if install_raises is not None:
            raise install_raises

    def _ensure(_config):
        log["ensured"] += 1
        return True

    monkeypatch.setattr(vs, "_load_state", lambda: ST_OLD)
    monkeypatch.setattr(vs, "base_config_raw", lambda: "")
    monkeypatch.setattr("gc_tui.voice_runtime.cli._stop_service", _stop)
    monkeypatch.setattr("gc_tui.voice_runtime.install.install", _install)
    monkeypatch.setattr("gc_tui.voice_runtime.autostart.ensure_voice_service", _ensure)
    return log


def test_failed_update_puts_the_service_back(monkeypatch):
    """Установка упала — сервис, погашенный ради обновления, обязан вернуться.

    Находка ревью коллеги: `update_local` гасит работающий сервис, а на
    `VoiceInstallError` возвращает False сразу — и голос молчал до перезапуска gc-ua,
    хотя человек всего лишь не смог обновиться."""
    from gc_tui.voice_runtime.install import VoiceInstallError

    said = []
    log = _update_stubs(monkeypatch, install_raises=VoiceInstallError("сеть недоступна"))
    assert vs.update_local(_cfg(), echo=said.append) is False
    assert log == {"stopped": 1, "installed": 1, "ensured": 1}
    assert any("сеть недоступна" in s for s in said), "причину провала обязаны назвать"


def test_successful_update_starts_the_service_once(monkeypatch):
    """Успешный путь не изменился: гашение, установка и ровно один подъём."""
    log = _update_stubs(monkeypatch)
    assert vs.update_local(_cfg(), echo=lambda _s: None) is True
    assert log == {"stopped": 1, "installed": 1, "ensured": 1}

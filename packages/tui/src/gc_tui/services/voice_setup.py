"""Фасад настройки голосового ввода для экранов (мастер, /settings).

Тонкий: данные и действия, БЕЗ UI-строк (рендер — в экранах). Паттерн
«мастер ↔ mcp_runtime.cli»: экраны получают модуль инъекцией и зовут
эти функции; вся политика — в voice_runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass


from gc_core.onboarding_config_writer import write_values as _write_values
from gc_core.voice_runtime.net import with_system_trust_retry
from gc_core.voice_runtime.service import make_silence_wav, port_alive as _port_alive
from gc_core.voice_runtime.state import available_update as _available_update, load_state as _load_state
from gc_core.voice_runtime.wire import (
    base_config_raw, managed_language as _managed_language,
    unwire_voice as _unwire_managed, wire_voice,
)


@dataclass(frozen=True)
class VoiceStatus:
    installed: bool
    # None — живость ещё не мерялась (дешёвый снимок): проверка порта — это сеть,
    # и на UI-потоке ей делать нечего. bool приносит воркер (probe_alive).
    service_alive: bool | None
    port: int | None
    model: str | None
    mode: str            # "local" | "cloud" | "off"
    # Версия движка, до которой можно обновиться, или None (не установлен / уже на ней).
    # Живёт в снимке, а не считается по месту: строка режима, гейт клавиши и футер
    # обязаны судить по ОДНОМУ факту, и статус в секции уже кэшируется на рендер.
    available_version: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    latency_ms: int | None
    error: str | None


def _post(url, **kwargs):
    """Единственный сетевой шов фасада — через voice_runtime.net.post: общая сессия
    агента (без ~/.netrc, иначе Basic оттуда перетирает наш Bearer) и прокси по тем же
    правилам, что у backend. Отдельная функция — шов для тестов."""
    from gc_core.voice_runtime.net import post as voice_post
    return voice_post(url, **kwargs)


def engine_update() -> tuple[str, str] | None:
    """(стоит, доступно) — установленный движок отстал от версии ЭТОЙ сборки агента;
    None — не установлен, уже на пине или рантайма нет.

    Вход для экранов: состояние читает сам (экрану его взять неоткуда), правило берёт
    из рантайма (`voice_runtime.state.available_update`) — там же, где пин. Раньше
    сверка была заперта приватным методом экрана после апгрейда, из-за чего об
    устаревании не знал больше никто: /settings не показывал даже версию, `gc-voice
    status` печатал её без сравнения.

    Импорт внутри функции намеренный: состояние читается на КАЖДЫЙ вызов (переустановка
    меняет его под работающим TUI), и подмена `voice_runtime.state` в тестах должна
    доходить сюда."""
    try:
        from gc_core.voice_runtime.state import available_update, load_state
        state = load_state()
        want = available_update(state)
    except Exception:                       # voice-рантайма нет — сверять нечего
        return None
    return (state.version, want) if want else None


def status_cheap(config_dict: dict) -> VoiceStatus:
    """Снимок без похода в сеть: state.json + конфиг. service_alive=None («не мерялось»).

    Живость порта сюда не входит намеренно: connect к мёртвому порту на машинах, где
    файрвол молча роняет пакеты, сгорает по полному таймауту (~0.5 с, замерено живьём),
    и синхронный вызов с UI-потока замораживал каждый ход курсора в /settings, пока
    сервис лежал. Живость меряет probe_alive — из воркера экрана."""
    st = _load_state()
    url = str((config_dict.get("voice") or {}).get("url") or "")
    if st is not None:
        from gc_core.voice_runtime.autostart import url_is_ours
        mode = "local" if url_is_ours(config_dict, st.port) else ("cloud" if url else "off")
        return VoiceStatus(installed=True,
                           service_alive=None,
                           port=st.port, model=st.model, mode=mode,
                           available_version=_available_update(st))
    return VoiceStatus(installed=False, service_alive=False, port=None, model=None,
                       mode="cloud" if url else "off")


def probe_alive() -> bool | None:
    """Живость порта сервиса — ЕДИНСТВЕННОЕ место фасада с TCP-connect (до 0.5 с).

    Звать только из воркер-потока (канон проб /settings): на машинах с молчаливым
    файрволом мёртвый порт держит соединение весь таймаут. None — сервис не установлен,
    мерять нечего."""
    st = _load_state()
    return None if st is None else _port_alive(st)


def status_snapshot(config_dict: dict) -> VoiceStatus:
    """Полный снимок: дешёвая часть + живость порта. Для воркеров и CLI-подобных путей;
    UI-поток берёт status_cheap и получает живость от probe_alive асинхронно."""
    s = status_cheap(config_dict)
    if not s.installed:
        return s
    from dataclasses import replace
    return replace(s, service_alive=bool(probe_alive()))


def install_local(echo=print) -> bool:
    from gc_core.voice_runtime.install import VoiceInstallError, install
    try:
        # base_raw обязателен: без него install видит пустой конфиг, не находит
        # конфликта и МОЛЧА перекрывает уже настроенный человеком voice.url. Раньше
        # его подавал только CLI, поэтому предупреждение в мастере и /settings —
        # там, где выбор и делается, — не показывалось ни разу.
        install(base_raw=base_config_raw(), echo=echo)
        return True
    except VoiceInstallError as exc:
        echo(f"[voice] ошибка установки: {exc}")
        return False


def update_local(config: dict, echo=print) -> bool:
    """Обновить локальный движок до версии, приколотой в этой сборке агента.

    Порядок обязателен: погасить → переустановить → поднять заново. На Windows
    работающий сервис держит файлы своего каталога, и переклон под ним не удался бы
    (`install` честно скажет «занят», но человеку нужен результат, а не диагноз).
    Порт и модель берём прежние: их выбирал человек, и смена версии движка — не повод
    молча вернуть дефолты."""
    from gc_core.voice_runtime.autostart import ensure_voice_service
    from gc_core.voice_runtime.cli import _stop_service   # приватная, как в remove_local ниже
    from gc_core.voice_runtime.install import VoiceInstallError, install

    state = _load_state()
    if state is None:
        return False
    was_running = _stop_service()
    if was_running:
        echo("[voice] сервис остановлен на время обновления")
    updated = True
    try:
        install(port=state.port, model=state.model,
                base_raw=base_config_raw(), echo=echo)
    except VoiceInstallError as exc:
        echo(f"[voice] ошибка обновления: {exc}")
        updated = False
    finally:
        if was_running:
            # Сервис гасили МЫ и только ради обновления — вернуть его обязаны в ЛЮБОМ
            # исходе (находка ревью коллеги): прежний ранний `return False` оставлял
            # человека без голоса до перезапуска gc-ua, хотя рабочая версия никуда не
            # девалась. Сессия у него уже открыта — поднимаем сами. Про установку и
            # про подъём говорим порознь: это разные новости.
            echo("[voice] поднимаю сервис заново (холодный старт — до полутора минут)")
            if not ensure_voice_service(config):
                echo("[voice] сервис не поднялся — запустится при следующем старте gc-ua")
    return updated


def remove_local() -> bool:
    # _cmd_remove приватная — публичный remove() в voice_runtime кандидат рефакторинга (не делаем сейчас).
    from gc_core.voice_runtime.cli import _cmd_remove
    return _cmd_remove() == 0


def _wire_local(config_dict: dict | None = None) -> None:
    st = _load_state()
    if st is not None:
        wire_voice(st.port, st.model, local_language(config_dict))


def local_language(config_dict: dict | None = None) -> str:
    """Язык, на котором работает локальный режим.

    Владелец — managed-слой, но переезд на свой endpoint сносит его секцию целиком
    (`wire_cloud` делает unwire), и к возврату в локальный режим языка там уже нет.
    Тогда берём его из ДЕЙСТВУЮЩЕГО конфига: иначе выбор человека молча сбрасывался
    бы на дефолт после круга «локальный → облако → локальный»."""
    managed = _managed_language("")
    if managed:
        return managed
    return str(((config_dict or {}).get("voice") or {}).get("language") or "ru")


def set_local_language(language: str) -> bool:
    """Язык локального режима: он лежит в managed-слое (его пишем мы, не юзер), и
    пишется тем же wire — остальные поля секции пересобираются из state и не могут
    разъехаться с сервисом. False — батарейки нет, писать нечего."""
    st = _load_state()
    if st is None:
        return False
    wire_voice(st.port, st.model, language or "ru")
    return True


def switch_to_local(echo=print, config: dict | None = None) -> bool:
    """Install при отсутствии батарейки (он сам wire'ит), иначе — только wire.
    config — действующий конфиг: из него достаётся язык, если managed-секции нет."""
    if _load_state() is None:
        return install_local(echo=echo)
    _wire_local(config)
    return True


def wire_cloud(config_path, url: str, token: str, model: str, language: str = "ru") -> None:
    """Свой endpoint: поля — в юзерский config.yml, managed-слой отключить
    только после успешной записи (провал записи не должен ломать рабочий local)."""
    from gc_core.config import _default_config_path, validate_config_text
    missing = _write_values(config_path,
                            {"voice.url": url, "voice.token": token, "voice.model": model,
                             "voice.language": language},
                            template_path=_default_config_path(), validate=validate_config_text)
    if missing:
        raise ValueError(f"поля не записаны: {', '.join(missing)}")
    _unwire_managed()


def list_models(url: str, token: str, verify=True,
                config_proxy: str | None = None) -> list[str] | None:
    """id моделей сервера по адресу STT-endpoint или None — список не получен.

    Корень API у аудио-методов тот же, что у chat (`api_base` стрижёт
    `/audio/transcriptions`), поэтому переиспользуем общий `fetch_models`
    (/config → /models). Авторизация — тем же сырым правилом, что у `probe_endpoint`
    (`Bearer <токен из поля>`): резолв env-ссылок здесь не делаем, как и там.
    Пустой список ≠ None: сервер ответил, но моделей не отдал — это разные
    сообщения для человека."""
    from gc_core.model_picker import fetch_models
    models = fetch_models(url, f"Bearer {token}" if token else None, verify=verify,
                          config_proxy=config_proxy)
    return None if models is None else [m.id for m in models]


def probe_endpoint(url: str, token: str, model: str, timeout: int = 10,
                   config_proxy: str | None = None, verify=True) -> ProbeResult:
    """POST 0.2 c тишины на endpoint — «жив ли STT» (тот же приём, что прогрев).

    verify — из voice.verify_ssl, как у транскрибации и у списка моделей: проверка,
    строже реальной работы, врёт про рабочую связку (корп-CA → «ошибка TLS»)."""
    def _do() -> ProbeResult:
        t0 = time.monotonic()
        resp = _post(url,
                     headers={"Authorization": f"Bearer {token}"},
                     files={"file": ("probe.wav", make_silence_wav(), "audio/wav")},
                     data={"model": model},
                     timeout=timeout,
                     config_proxy=config_proxy,
                     verify=verify)
        ms = int((time.monotonic() - t0) * 1000)
        if getattr(resp, "status_code", 0) == 200:
            return ProbeResult(ok=True, latency_ms=ms, error=None)
        return ProbeResult(ok=False, latency_ms=ms,
                           error=f"HTTP {getattr(resp, 'status_code', '?')}")
    try:
        return with_system_trust_retry(_do)
    except Exception as exc:
        return ProbeResult(ok=False, latency_ms=None, error=str(exc))

"""Сессионный слой конфига: переопределения, действующие в текущем треде.

Базовые настройки (`TuiApp.base_settings()`) — то, что прочитано из файлов. Этот слой —
то, что пользователь переключил в сессии. Действующие настройки
(`TuiApp.effective_settings()`) = база + слой; их видят и UI, и агент. Владелец состава — белый список `SESSION_KEYS`:
право переопределять ключ выдаёт сама фича, а не «можно всё» (пути к тредам и
снапшот рабочего каталога на лету менять нельзя).

Здесь же — семантика этих ключей: как читается ДЕЙСТВУЮЩЕЕ значение ключа
(правило коллизии /gate со старым глобальным рубильником — спека
2026-07-16-palette-toggles-gate-answers-design §3) и в какой форме набор лежит
в метаданных треда. Один владелец на ключ: витрина (tui/toggles.py) и любой
другой читатель действующего значения обязаны читать одним и тем же правилом
(`SessionKey.effective_value`) — иначе они разойдутся в том, что считают
текущим значением ключа.
Слой не зависит ни от `TuiApp`, ни от презентации — направление только сюда.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict

from gc_core.settings import is_explicit, runtime_of, settings_of

# Ключ режима судьи завершения хода.
_FINISH_GATE_KEY = "tui.finish_gate_mode"


def finish_gate_mode_of(settings: Any) -> str:
    """Эффективный режим /gate: явный tui-ключ > явный глобальный off > дефолт smart.

    `settings` — всё, что принимает `settings_of` (`LoadedSettings`, `AppConfig`,
    контекст). Явность — по `is_explicit` (файл, слой, CLI, сессия), а не по
    значению: умолчание схемы явным выбором не считается."""
    settings = settings_of(settings)
    mode = str(settings.tui.finish_gate_mode or "smart")
    if is_explicit(settings, _FINISH_GATE_KEY):
        return mode
    if (
        is_explicit(settings, "agent.strict_turn_finish_validation")
        and not settings.agent.strict_turn_finish_validation
    ):
        return "off"
    return "smart"


def text_answer_mode_of(settings: Any) -> str:
    """Эффективный режим /answers: коллизии нет — значение или дефолт."""
    return str(settings_of(settings).tui.text_answer_mode or "judge")


def _value_at(settings: Any, dotted: str) -> Any:
    """Значение настройки по пути через точку (`tui.finish_gate_mode`)."""
    current: Any = settings_of(settings)
    for name in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(name)
        else:
            current = getattr(current, name, None)
        if current is None:
            return None
    return current


@dataclass(frozen=True)
class SessionKey:
    """Переопределяемый ключ: путь через точку (конфиг/метаданные) и — если у
    ключа своё правило — читатель действующего значения (`effective`; нет
    своего правила — значение поля настроек по этому пути)."""

    dotted: str
    effective: Callable[[Any], Any] | None = None

    def effective_value(self, settings: Any) -> Any:
        """Значение ключа, действующее для данных настроек (всё, что принимает
        `settings_of`; для `profile` — и `runtime_of`)."""
        if self.effective is not None:
            return self.effective(settings)
        return _value_at(settings, self.dotted)


def _active_profile_of(settings: Any) -> str:
    """Действующий профиль — рантайм загрузки, а не указатель из файла."""
    return runtime_of(settings).active_profile or ""


SESSION_KEYS: Dict[str, SessionKey] = {
    _FINISH_GATE_KEY: SessionKey(_FINISH_GATE_KEY, finish_gate_mode_of),
    "tui.text_answer_mode": SessionKey(
        "tui.text_answer_mode", text_answer_mode_of
    ),
    # Профили и уровни: `level` — обычный ключ, ложится сессионным слоем поверх
    # настроек как любой другой. `profile` тоже регистрируется здесь для
    # персиста/восстановления/санации (белый список), но слоем НЕ накладывается —
    # `TuiApp.effective_settings()` перехватывает его отдельно: смена профиля
    # требует пере-сборки базы под ним, а не подмены одного поля. Действующее
    # значение — профиль, под которым база собрана (`RuntimeEnv.active_profile`).
    "level": SessionKey("level"),
    "profile": SessionKey("profile", _active_profile_of),
}


META_KEY = "session_settings"
BREAKER_META_KEY = "circuit_breaker"


def read_breaker_from_tui(tui: Dict[str, Any]) -> Dict[str, str]:
    """Снимок предохранителя моделей (спека §8) из секции TUI треда."""
    return dict((tui or {}).get(BREAKER_META_KEY) or {})


def to_breaker_meta_patch(snapshot: Dict[str, str]) -> Dict[str, Any]:
    """Снимок предохранителя как патч для meta.tui."""
    return {BREAKER_META_KEY: dict(snapshot or {})}


def read_from_tui(tui: Dict[str, Any]) -> Dict[str, Any]:
    """Переопределения из секции TUI треда (``record_view.tui_section``).

    Форма хранения известна только здесь: читателей у неё больше одного, и
    выражение вида `tui["session_settings"]`, расписанное по экранам,
    третьим читателем породило бы третью копию."""
    return dict((tui or {}).get(META_KEY) or {})


def to_meta_patch(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Набор переопределений как патч для meta.tui (`FileTaskStore.update_tui_meta`)."""
    return {META_KEY: dict(overrides or {})}

"""Сессионный слой конфига: переопределения, действующие в текущем треде.

Базовый конфиг (`TuiApp._config`) — то, что прочитано из файла. Этот слой —
то, что пользователь переключил в сессии. Эффективный конфиг = база + слой;
его видят и UI, и агент. Владелец состава — белый список `SESSION_KEYS`:
право переопределять ключ выдаёт сама фича, а не «можно всё» (пути к тредам и
снапшот рабочего каталога на лету менять нельзя).

Здесь же — семантика этих ключей: как читается ДЕЙСТВУЮЩЕЕ значение ключа
(правило коллизии /gate со старым глобальным рубильником — спека
2026-07-16-palette-toggles-gate-answers-design §3) и в какой форме набор лежит
в метаданных треда. Один владелец на ключ: витрина (tui/toggles.py) и сравнение
с действующим умолчанием (`diff_vs_base`) обязаны читать одним и тем же
правилом — иначе они разойдутся в том, что считают текущим значением ключа.
Слой не зависит ни от `TuiApp`, ни от презентации — направление только сюда.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


def effective_finish_gate_mode(cfg: Dict[str, Any]) -> str:
    """Эффективный режим /gate: явный tui-ключ > явный глобальный off > дефолт smart."""
    mode = str(cfg.get("tui_finish_gate_mode") or "smart")
    if bool(cfg.get("tui_finish_gate_mode_explicit")):
        return mode
    if bool(cfg.get("strict_turn_finish_explicit_false")):
        return "off"
    return "smart"


def effective_text_answer_mode(cfg: Dict[str, Any]) -> str:
    """Эффективный режим /answers: коллизии нет — значение или дефолт."""
    return str(cfg.get("tui_text_answer_mode") or "judge")


@dataclass(frozen=True)
class SessionKey:
    """Переопределяемый ключ: dotted-имя (конфиг/метаданные), плоское имя
    (flatten-конфиг), — если есть — имя флага «задано явно», который читает
    правило коллизии, и читатель действующего значения (`effective`; нет своего
    правила — берётся сырое плоское значение)."""

    dotted: str
    flat: str
    explicit_flag: str | None = None
    effective: Callable[[Dict[str, Any]], Any] | None = None

    def effective_value(self, cfg: Dict[str, Any]) -> Any:
        """Значение ключа, действующее для данного конфига."""
        return self.effective(cfg) if self.effective else cfg.get(self.flat)


SESSION_KEYS: Dict[str, SessionKey] = {
    "tui.finish_gate_mode": SessionKey(
        "tui.finish_gate_mode", "tui_finish_gate_mode", "tui_finish_gate_mode_explicit",
        effective_finish_gate_mode,
    ),
    "tui.text_answer_mode": SessionKey(
        "tui.text_answer_mode", "tui_text_answer_mode", None,
        effective_text_answer_mode,
    ),
    # Профили и уровни: `level` — обычный ключ, накладывается флагом на плоский
    # "level" как любой другой. `profile` тоже регистрируется здесь для
    # персиста/восстановления/санации (белый список), но его
    # ЗНАЧЕНИЕ не читается через apply_overrides — TuiApp.effective_config() перехватывает
    # его отдельно ДО generic-наложения (смена профиля требует пере-мержа base конфига,
    # не просто подмены плоского ключа); apply_overrides всё равно проставляет
    # плоский "profile" поверх пере-смерженной базы — так cfg["profile"] честно отражает
    # сессионный выбор для витрин.
    "level": SessionKey("level", "level"),
    "profile": SessionKey("profile", "profile"),
}


META_KEY = "session_settings"
BREAKER_META_KEY = "circuit_breaker"


def read_breaker_from_meta(meta: Dict[str, Any]) -> Dict[str, str]:
    """Снимок предохранителя моделей (спека §8) из метаданных треда."""
    return dict(((meta or {}).get("tui") or {}).get(BREAKER_META_KEY) or {})


def to_breaker_meta_patch(snapshot: Dict[str, str]) -> Dict[str, Any]:
    """Снимок предохранителя как патч для meta.tui."""
    return {BREAKER_META_KEY: dict(snapshot or {})}


def read_from_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Переопределения из метаданных треда (`FileTaskStore.get_task_meta`).

    Форма хранения известна только здесь: читателей у неё больше одного, и
    выражение вида `meta["tui"]["session_settings"]`, расписанное по экранам,
    третьим читателем породило бы третью копию."""
    return dict(((meta or {}).get("tui") or {}).get(META_KEY) or {})


def to_meta_patch(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Набор переопределений как патч для meta.tui (`FileTaskStore.update_tui_meta`)."""
    return {META_KEY: dict(overrides or {})}


def apply_overrides(cfg: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    """Наложить переопределения на плоский конфиг (мутирует cfg).

    Переключение в сессии — явное действие пользователя, поэтому для ключей с
    `explicit_flag` флаг взводится: иначе правило коллизии сочло бы значение
    неявным и могло вернуть не его."""
    for dotted, value in (overrides or {}).items():
        key = SESSION_KEYS.get(dotted)
        if key is None:
            continue
        cfg[key.flat] = value
        if key.explicit_flag:
            cfg[key.explicit_flag] = True


def diff_vs_base(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Переопределения, отличающиеся от ДЕЙСТВУЮЩЕГО умолчания базового
    конфига — инструмент сравнения сессии с базой (слой API; сейчас без
    потребителя в UI, ближайший — будущая фича /rigor).

    Сравниваем с действующим умолчанием базы, а не с сырым плоским значением:
    у ключа может быть своё правило (коллизия /gate), и по сырому значению
    расхождение видно не всегда — рукописный конфиг с легаси-флагом
    глобального off по сырому значению совпал бы с сессией, включившей режим
    явно, и результат остался бы пустым, хотя действующие значения
    расходятся."""
    changed: Dict[str, Any] = {}
    for dotted, value in (overrides or {}).items():
        key = SESSION_KEYS.get(dotted)
        if key is None:
            continue
        if key.effective_value(base) != value:
            changed[dotted] = value
    return changed

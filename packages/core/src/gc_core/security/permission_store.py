from __future__ import annotations

from typing import Any, Optional

from gc_core.security.call_scope import current_call

#: Префикс ключей решений про исполнение команды или кода. Только ответы под
#: такими ключами уходят судье команд подсказкой («и подобные в сессии»).
COMMAND_KEY_PREFIX = "command:"


def is_command_key(decision_key: Optional[str]) -> bool:
    """Решение про исполнение команды или кода."""
    return bool(decision_key) and str(decision_key).startswith(
        COMMAND_KEY_PREFIX
    )


def remembers_similar(decision_key: Optional[str], mode: Any) -> bool:
    """Покрывает ли ответ «в сессии» и подобные команды — от этого зависит
    подпись вопроса. Подобные решает судья команд, читая ответы человека
    примерами; он работает только в edit. В manual и auto ответ покрывает
    ровно эту команду."""
    value = getattr(mode, "value", mode)
    return is_command_key(decision_key) and value == "edit"


class PermissionStore:
    """Память решений confirm: на один вызов тула (run) + на сессию/тред (session).

    Ключ — "<action>:<value>" (write:<path>, read:<path>, url:<url>, command:<cmd>).
    Матч — точное равенство ключа (Mid; глобы — Full). session приоритетнее run.

    Разовый ответ («разрешить / запретить разово») живёт ровно один вызов тула
    и хранится в привязке этого вызова (``call_scope``), а не здесь: стор у
    родителя и субагентов один, и одновременные вызовы не должны видеть ответы
    друг друга. Внутри вызова ответ помнится — две проверки подряд (защищённый
    путь, затем периметр) не спрашивают об одном действии дважды. Дольше
    вызова его держать нельзя: ключ записи — путь, без содержимого, и одно
    «разово» разрешало бы любые следующие правки того же файла.

    Вне диспетчера (защиту позвали напрямую) привязки нет — разовые ответы
    тогда лежат в самом сторе до конца его жизни.

    session_rules берётся ПО ССЫЛКЕ: в AgentExecutor сюда передаётся
    state.shared.confirm_rules, поэтому record(session=True) мутирует его —
    и существующая персистентность shared сохранит правило в meta.
    """

    def __init__(
        self,
        session_rules: Optional[dict] = None,
        *,
        shared: Optional[Any] = None,
    ) -> None:
        self._session_rules: dict[str, bool] = (
            session_rules if session_rules is not None else {}
        )
        # Разовые ответы для кода вне диспетчера; внутри вызова — в привязке.
        self._run_rules: dict[str, bool] = {}
        # Общее состояние сессии (`SharedStateData`) — носитель режима
        # разрешений. Стор — единственное, что из него видят защиты: у них на
        # руках контекст рана, а не состояние.
        self._shared = shared

    def permission_mode(self) -> Optional[str]:
        """Режим, выбранный в сессии; None — выбора не было."""
        mode = getattr(self._shared, "permission_mode", None)
        return mode if isinstance(mode, str) and mode.strip() else None

    def set_permission_mode(self, mode: Optional[str]) -> None:
        if self._shared is not None:
            self._shared.permission_mode = mode

    def _current_run_rules(self) -> dict[str, bool]:
        scope = current_call()
        return scope.one_time_answers if scope is not None else self._run_rules

    def decision_for(self, key: str) -> Optional[bool]:
        if key in self._session_rules:
            return bool(self._session_rules[key])
        return self._current_run_rules().get(key)

    def record(self, key: str, allow: bool, *, session: bool) -> None:
        self._current_run_rules()[key] = allow
        if session:
            self._session_rules[key] = allow

    def export_run_rules(self) -> dict[str, bool]:
        """Copy transient decisions for an execution-boundary snapshot."""
        return dict(self._current_run_rules())

    def replace_run_rules(self, rules: dict[str, bool]) -> None:
        """Apply a previously merged snapshot without rebinding session rules."""
        current = self._current_run_rules()
        current.clear()
        current.update(rules)

    def session_rules(self) -> dict:
        return self._session_rules

    _INSESSION_PREFIX = COMMAND_KEY_PREFIX
    _MAX_ALLOWED = 12
    _MAX_DENIED = 8
    _MAX_EXAMPLE_CHARS = 280

    def insession_examples(self) -> dict[str, list[str]]:
        """Few-shot примеры для судьи из remembered human Y/N решений.

        Только session_rules с ключами command:* (Y=allow_remember, N=deny_remember).
        run_rules (O/T) никогда не попадают сюда.
        Возвращает {"allowed": [...], "denied": [...]} — последние 12/8,
        усечённые до 280 символов + «…», с убранным префиксом ``command:``.
        """
        allowed: list[str] = []
        denied: list[str] = []
        for key, val in self._session_rules.items():
            if not key.startswith(self._INSESSION_PREFIX):
                continue
            payload = key[len(self._INSESSION_PREFIX) :]
            payload = self._truncate_example(payload)
            if val:
                allowed.append(payload)
            else:
                denied.append(payload)
        return {
            "allowed": allowed[-self._MAX_ALLOWED :],
            "denied": denied[-self._MAX_DENIED :],
        }

    def _truncate_example(self, payload: str) -> str:
        limit = self._MAX_EXAMPLE_CHARS
        if len(payload) <= limit:
            return payload
        return payload[:limit] + "…"

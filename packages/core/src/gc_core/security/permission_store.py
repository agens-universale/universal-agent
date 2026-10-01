from __future__ import annotations

from typing import Optional


class PermissionStore:
    """Память решений confirm за прогон (run) + за сессию/тред (session).

    Ключ — "<action>:<value>" (write:<path>, read:<path>, url:<url>, command:<cmd>).
    Матч — точное равенство ключа (Mid; глобы — Full). session приоритетнее run.

    session_rules берётся ПО ССЫЛКЕ: в AgentExecutor сюда передаётся
    state.shared.confirm_rules, поэтому record(session=True) мутирует его —
    и существующая персистентность shared сохранит правило в meta.
    """

    def __init__(self, session_rules: Optional[dict] = None) -> None:
        self._session_rules: dict[str, bool] = (
            session_rules if session_rules is not None else {}
        )
        self._run_rules: dict[str, bool] = {}

    def decision_for(self, key: str) -> Optional[bool]:
        if key in self._session_rules:
            return bool(self._session_rules[key])
        if key in self._run_rules:
            return self._run_rules[key]
        return None

    def record(self, key: str, allow: bool, *, session: bool) -> None:
        self._run_rules[key] = allow
        if session:
            self._session_rules[key] = allow

    def export_run_rules(self) -> dict[str, bool]:
        """Copy transient decisions for an execution-boundary snapshot."""
        return dict(self._run_rules)

    def replace_run_rules(self, rules: dict[str, bool]) -> None:
        """Apply a previously merged snapshot without rebinding session rules."""
        self._run_rules.clear()
        self._run_rules.update(rules)

    def session_rules(self) -> dict:
        return self._session_rules

    _INSESSION_PREFIX = "command:"
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

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
        self._session: dict[str, bool] = session_rules if session_rules is not None else {}
        self._run: dict[str, bool] = {}

    def decision_for(self, key: str) -> Optional[bool]:
        if key in self._session:
            return bool(self._session[key])
        if key in self._run:
            return self._run[key]
        return None

    def record(self, key: str, allow: bool, *, session: bool) -> None:
        self._run[key] = allow
        if session:
            self._session[key] = allow

    def session_rules(self) -> dict:
        return self._session

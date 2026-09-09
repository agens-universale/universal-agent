"""Проверка политики URL НА КАЖДОМ переходе редиректа.

Гвард ``enforce_url_policy`` проверяет только тот адрес, который назвала
модель. Этого мало: сервер вправе ответить ``302`` куда угодно, и запрет
«не ходить в 169.254.169.254» обходится одним заголовком ``Location``.
Поэтому редиректы разворачиваются вручную, и каждый следующий адрес проходит
ту же ``validate_url_policy``.

Копий этой петли было две — у ``simple_fetch_url`` и у ``download_file``, — а
у ``fetch_url``, который регистрируется по умолчанию, её не было вовсе:
он звал ``follow_redirects=True`` и доверял всей цепочке. Две реализации
одного правила расходятся молча, а третья дырявая — поэтому петля здесь одна.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, Sequence
from urllib.parse import urljoin

from gc_core.security.url_policy import UrlPolicyDecision, validate_url_policy

#: Потолок переходов. Выше — почти наверняка петля, а не навигация.
DEFAULT_MAX_REDIRECTS = 5


class RedirectBlocked(Exception):
    """Переход отвергнут политикой. Несёт решение — его показывают модели."""

    def __init__(self, decision: UrlPolicyDecision) -> None:
        super().__init__(decision.reason or "redirect blocked by policy")
        self.decision = decision


def _next_hop(response: Any, current: str) -> str | None:
    """Абсолютный адрес следующего перехода или ``None``, если его нет."""
    status = getattr(response, "status_code", 0) or 0
    if not (300 <= status < 400):
        return None
    headers = getattr(response, "headers", None)
    location = headers.get("Location") if hasattr(headers, "get") else None
    if not location:
        return None
    return urljoin(current, location)


def _too_many(current: str, limit: int) -> RedirectBlocked:
    return RedirectBlocked(
        UrlPolicyDecision(
            allowed=False,
            url=current,
            error_type="too_many_redirects",
            reason=f"Exceeded {limit} redirects.",
        )
    )


async def fetch_validating_redirects(
    session: Any,
    start_url: str,
    headers: Dict[str, str],
    timeout: Any,
    *,
    allowed_schemes: Sequence[str],
    url_blacklist: Sequence[str],
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> Any:
    """GET с ручным разворотом редиректов; тело читается целиком.

    Для тулов, которым нужен готовый ответ. Кто качает файл — берёт
    :func:`stream_validating_redirects`, чтобы не тянуть тело в память.
    """
    current = start_url
    for _ in range(max_redirects + 1):
        response = await session.get(
            current, headers=headers, timeout=timeout, follow_redirects=False
        )
        next_abs = _next_hop(response, current)
        if next_abs is None:
            return response
        decision = validate_url_policy(next_abs, allowed_schemes, url_blacklist)
        if not decision.allowed:
            raise RedirectBlocked(decision)
        current = next_abs
    raise _too_many(current, max_redirects)


@asynccontextmanager
async def stream_validating_redirects(
    session: Any,
    start_url: str,
    headers: Dict[str, str],
    timeout: Any,
    *,
    allowed_schemes: Sequence[str],
    url_blacklist: Sequence[str],
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
):
    """То же, но тело НЕ читается: отдаёт открытый потоковый ответ.

    Нужен скачиванию: там тело может быть гигабайтным, и решение «слишком
    большой» надо принимать до того, как оно приедет в память.
    """
    current = start_url
    for _ in range(max_redirects + 1):
        request = session.build_request(
            "GET", current, headers=headers, timeout=timeout
        )
        response = await session.send(request, stream=True, follow_redirects=False)
        next_abs = _next_hop(response, current)
        if next_abs is None:
            try:
                yield response
            finally:
                await response.aclose()
            return
        await response.aclose()
        decision = validate_url_policy(next_abs, allowed_schemes, url_blacklist)
        if not decision.allowed:
            raise RedirectBlocked(decision)
        current = next_abs
    raise _too_many(current, max_redirects)


__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "RedirectBlocked",
    "fetch_validating_redirects",
    "stream_validating_redirects",
]

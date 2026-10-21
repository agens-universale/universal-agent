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

Переход на другой хост решает режим разрешений: в manual и auto петля его
не выполняет (``same_host_only``), модель запрашивает адрес отдельно — новый
запрос проходит вопрос или судью. Признак петля получает готовым.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, Sequence
from urllib.parse import urljoin

from gc_core.security.url_policy import (
    UrlPolicyDecision,
    validate_redirect_hop,
)

#: Потолок переходов. Выше — почти наверняка петля, а не навигация.
#:
#: Двадцать, а не пять, и это не запас «на всякий случай»: столько же брал
#: httpx, когда переходы разворачивал он сам (``follow_redirects=True``).
#: Первая редакция общей петли поставила пять, и у ``fetch_url`` — тула,
#: который регистрируется ПО УМОЛЧАНИЮ, — потолок молча упал вчетверо.
#: Длинные цепочки на корпоративных SSO-входах (нормализация домена,
#: языковой редирект) в пять переходов не всегда укладываются, и тул начинал
#: отказывать там, где раньше работал.
DEFAULT_MAX_REDIRECTS = 20


class RedirectBlocked(Exception):
    """Переход отвергнут политикой адресов или режимом (переход на другой
    хост в manual / auto). Несёт решение — его показывают модели."""

    def __init__(self, decision: UrlPolicyDecision) -> None:
        super().__init__(decision.reason or "redirect blocked by policy")
        self.decision = decision


class TooManyRedirects(Exception):
    """Переходов больше потолка. Это НЕ запрет политики.

    Разведено намеренно: превышение отдавалось модели тем же конвертом, что и
    блокировка политикой, то есть как «запрещено правилами». Модель делала из
    этого неверный вывод — что адрес под запретом, — и переставала пробовать
    вместо того, чтобы сообщить о петле редиректов.
    """

    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"Exceeded {limit} redirects starting at {url}")
        self.url = url
        self.limit = limit


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


def checked_next_hop(
    response: Any,
    current: str,
    allowed_schemes: Sequence[str],
    url_blacklist: Sequence[str],
    same_host_only: bool,
) -> str | None:
    """Следующий переход, прошедший проверку, или ``None``, если его нет.

    Запрещённый переход — ``RedirectBlocked``. Единое правило перехода
    для всех обходчиков редиректов.
    """
    next_abs = _next_hop(response, current)
    if next_abs is None:
        return None
    decision = validate_redirect_hop(
        current,
        next_abs,
        allowed_schemes,
        url_blacklist,
        same_host_only=same_host_only,
    )
    if not decision.allowed:
        raise RedirectBlocked(decision)
    return next_abs


def _too_many(current: str, limit: int) -> TooManyRedirects:
    return TooManyRedirects(current, limit)


async def fetch_validating_redirects(
    session: Any,
    start_url: str,
    headers: Dict[str, str],
    timeout: Any,
    *,
    allowed_schemes: Sequence[str],
    url_blacklist: Sequence[str],
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    same_host_only: bool = False,
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
        next_abs = checked_next_hop(
            response, current, allowed_schemes, url_blacklist, same_host_only
        )
        if next_abs is None:
            return response
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
    same_host_only: bool = False,
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
        response = await session.send(
            request, stream=True, follow_redirects=False
        )
        try:
            next_abs = checked_next_hop(
                response,
                current,
                allowed_schemes,
                url_blacklist,
                same_host_only,
            )
            if next_abs is None:
                yield response
                return
        finally:
            await response.aclose()
        current = next_abs
    raise _too_many(current, max_redirects)


__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "RedirectBlocked",
    "TooManyRedirects",
    "checked_next_hop",
    "fetch_validating_redirects",
    "stream_validating_redirects",
]

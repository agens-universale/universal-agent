"""Общая часть двух реализаций ``exec_fetch_url``: ``fetch_url`` и ``simple_fetch_url``.

Оба тула регистрируются под одним именем (``registry`` берёт ``fetch_url``, а
без trafilatura/readability откатывается на ``simple_fetch_url``) и делят
``shared.fetch_url_cache``. Поэтому конверт ответа, кэш, проверка аргументов и
признаки «бинарного» ответа у них обязаны совпадать — и живут здесь, в одном
экземпляре. Различается только извлечение текста из HTML (и мелочи, которые
каждый тул задаёт у себя: см. ``_NON_BINARY_APPLICATION_TYPES`` и хуки
``_meta_title_text`` / ``_find_meta_description``).

Тяжёлых зависимостей (trafilatura, readability) здесь быть не должно: модуль
грузится и тогда, когда их нет, — ради отката на ``simple_fetch_url``.

Сетевой запрос (``_arequest_with_retries``) остаётся в модулях тулов: тесты
подменяют ``get_async_client`` в глобалах самого тула, и запрос обязан брать
клиента оттуда.
"""

from __future__ import annotations

import functools
import html as html_lib
import json
import re
from abc import abstractmethod
from collections import OrderedDict
from typing import Any, Dict, NamedTuple, Optional, Tuple

import httpx

from gc_core.tools.simple_tool_base import AbstractSimpleTool

_INVALID_ARGUMENT = "invalid_argument"
_EMPTY_META: Dict[str, Optional[str]] = {
    "title": None,
    "description": None,
    "lang": None,
}


def _response_url(exc: Exception) -> str | None:
    """URL из ответа-ошибки строкой: httpx.URL в JSON не сериализуется."""
    response = getattr(exc, "response", None)
    url = getattr(response, "url", None)
    return str(url) if url is not None else None


def http_error_failure(
    exc: httpx.HTTPError, attempt: int, attempts: int
) -> Optional[Dict[str, Any]]:
    """kwargs ошибки для ``_result`` — либо None, если попытку стоит повторить.

    Повторяем только то, что лечится повтором: сеть (статуса нет), 429 и 5xx,
    и только пока попытки не кончились.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    retryable = status is None or status in (429,) or status >= 500
    if retryable and attempt != attempts:
        return None
    return dict(
        ok=False,
        final_url=_response_url(exc),
        status_code=status,
        content_type=getattr(
            getattr(exc, "response", None), "headers", {}
        ).get("Content-Type")
        if getattr(exc, "response", None) is not None
        else None,
        result_type="error",
        error={
            "type": "request_failed",
            "message": str(exc),
            "attempt": attempt,
            "attempts_total": attempts,
            "retryable": retryable,
        },
    )


def preview_from_stripped(
    stripped: str, max_preview_chars: int
) -> tuple[str, bool]:
    """Превью из текста без тегов: обрезка до ``max_preview_chars`` и заголовок."""
    truncated = len(stripped) > max_preview_chars
    if truncated:
        stripped = stripped[:max_preview_chars]
    header = (
        f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
        if truncated
        else "Content with removed HTML tags"
    )
    return f"{header}:\n\n{stripped}", truncated


class FetchRequest(NamedTuple):
    """Проверенные аргументы вызова: с ними уже можно идти в сеть."""

    url: str
    proxy: Optional[str]
    timeout: int
    offset: int
    limit: int
    cache_key: str


class FetchUrlBase(AbstractSimpleTool):
    """База ``FetchUrl`` / ``SimpleFetchUrl``: всё, кроме разбора HTML и запроса."""

    timeout_seconds = 20
    max_content_chars = 20_000
    # LRU-лимит кэша извлечённого текста страниц (на тред/сессию).
    # Кэш хранится в SharedStateData (per-thread, персистится в meta),
    # поэтому лимит ограничивает и RAM, и размер JSON-meta треда.
    # Один на оба тула — они делят shared.fetch_url_cache.
    fetch_url_cache_max_size = 10
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    #: ``application/*``, которые НЕ считаются бинарными. У каждого тула свой
    #: набор (``fetch_url`` включает ``x-www-form-urlencoded``, ``simple`` — нет).
    _NON_BINARY_APPLICATION_TYPES: Tuple[str, ...]

    _PROXY_SCHEMES = (
        "http://",
        "https://",
        "socks5://",
        "socks5h://",
        "socks4://",
        "socks4a://",
    )

    def __init__(
        self, ctx: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state

    @classmethod
    def _tool_spec(
        cls,
        *,
        description: str,
        timeout_description: str,
        preview_description: str,
    ) -> Dict[str, Any]:
        """Схема ``exec_fetch_url``: у тулов свои описание и тексты двух полей, остальное общее."""
        return {
            "type": "function",
            "name": "exec_fetch_url",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute URL to fetch (http or https).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": timeout_description,
                    },
                    "max_preview_chars": {
                        "type": "integer",
                        "description": preview_description,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset for paginated reads from cached full content. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Maximum number of characters to return in content. Default {cls.max_content_chars}.",
                    },
                    "proxy": {
                        "type": "string",
                        "description": (
                            "Optional proxy URL for this request "
                            "(http://, https://, socks5://, socks5h://, socks4://). "
                            "Empty string or omit = no proxy."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def _result(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
        cls,
        *,
        url: str,
        ok: bool,
        final_url: Optional[str] = None,
        status_code: Optional[int] = None,
        content_type: Optional[str] = None,
        is_binary: bool = False,
        result_type: Optional[str] = None,
        extracted_source: Optional[str] = None,
        content: str = "",
        preview: str = "",
        truncated: bool = False,
        max_content_chars: Optional[int] = None,
        max_preview_chars: Optional[int] = None,
        error: Optional[Dict[str, Any]] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        returned_chars: Optional[int] = None,
        total_chars: Optional[int] = None,
        has_more: Optional[bool] = None,
        cache_hit: Optional[bool] = None,
        meta: Optional[Dict[str, Optional[str]]] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": cls.registered_tool_name(),
            "ok": ok,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "is_binary": is_binary,
            "result_type": result_type,
            "extracted_source": extracted_source,
            "truncated": truncated,
            "meta": meta if meta is not None else dict(_EMPTY_META),
            "error": error,
        }
        optional = {
            "max_content_chars": max_content_chars,
            "max_preview_chars": max_preview_chars,
            "offset": offset,
            "limit": limit,
            "returned_chars": returned_chars,
            "total_chars": total_chars,
            "has_more": has_more,
        }
        payload.update(
            {
                key: value
                for key, value in optional.items()
                if value is not None
            }
        )
        if has_more is not None and offset is not None and limit is not None:
            payload["next_offset"] = offset + limit if has_more else None
        if cache_hit is not None:
            payload["cache_hit"] = cache_hit
        payload["content"] = content
        payload["preview"] = preview
        return json.dumps(payload, ensure_ascii=False)

    # --- кэш страниц ---------------------------------------------------------

    def _get_cache(self) -> Optional[OrderedDict[str, Any]]:
        shared = getattr(self._state, "shared", None)
        if shared is None:
            return None
        cache = getattr(shared, "fetch_url_cache", None)
        # Нормализация: из meta кэш восстанавливается как обычный dict,
        # а для LRU нужен OrderedDict. Если тип не совпадает — пересобираем.
        if not isinstance(cache, OrderedDict):
            cache = OrderedDict(cache if isinstance(cache, dict) else {})
            shared.fetch_url_cache = cache
        return cache

    @staticmethod
    def _cache_key(url: str, proxy: Optional[str] = None) -> str:
        """Ключ кэша: URL, при наличии proxy — URL+proxy (разные маршруты не смешиваются)."""
        if proxy:
            return f"{url}||proxy={proxy}"
        return url

    def _get_from_cache(
        self, cache_key: str
    ) -> Optional[Tuple[str, Dict[str, Optional[str]]]]:
        """Возвращает (content, meta) для закэшированного ключа или None.

        При cache-hit обновляет LRU-порядок: только что читанный ключ становится
        самым свежим и не вытесняется следующими записями.
        """
        cache = self._get_cache()
        if cache is None or cache_key not in cache:
            return None
        cached = cache[cache_key]
        if isinstance(cached, dict):
            content = cached.get("content", "")
            meta = self._normalize_meta(cached.get("meta"))
        else:
            content = cached if isinstance(cached, str) else ""
            meta = self._normalize_meta(None)
        cache.move_to_end(cache_key)
        return content, meta

    def _put_to_cache(
        self, cache_key: str, content: str, meta: Dict[str, Optional[str]]
    ) -> None:
        """Записывает страницу в кэш и эвиктирует самые старые при превышении лимита."""
        cache = self._get_cache()
        if cache is None:
            return
        cache[cache_key] = {"content": content, "meta": meta}
        cache.move_to_end(cache_key, last=True)
        while len(cache) > self.fetch_url_cache_max_size:
            cache.popitem(last=False)

    # --- прокси --------------------------------------------------------------

    @classmethod
    def _normalize_proxy(
        cls, proxy: Optional[str]
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Возвращает (normalized_proxy | None, error | None)."""
        if proxy is None:
            return None, None
        if not isinstance(proxy, str):
            return None, {"type": _INVALID_ARGUMENT, "field": "proxy"}
        normalized = proxy.strip()
        if not normalized:
            # Пустая / whitespace-only строка = прокси не задан.
            return None, None
        lower = normalized.lower()
        if not lower.startswith(cls._PROXY_SCHEMES):
            return None, {
                "type": _INVALID_ARGUMENT,
                "field": "proxy",
                "message": (
                    "proxy must start with http://, https://, socks5://, socks5h://, socks4://, or socks4a://."
                ),
            }
        return normalized, None

    # --- метаданные страницы -------------------------------------------------

    @staticmethod
    def _normalize_meta(
        meta: Optional[Dict[str, Any]],
    ) -> Dict[str, Optional[str]]:
        normalized: Dict[str, Optional[str]] = dict(_EMPTY_META)
        if not isinstance(meta, dict):
            return normalized
        for key in ("title", "description", "lang"):
            value = meta.get(key)
            if isinstance(value, str):
                value = value.strip()
                normalized[key] = value or None
        return normalized

    @classmethod
    @abstractmethod
    def _meta_title_text(cls, fragment: str) -> str:
        """Текст ``<title>`` без разметки — каждый тул чистит его своим стриппером."""

    @staticmethod
    @abstractmethod
    def _find_meta_description(html: str) -> Optional[str]:
        """Сырое значение ``content`` описания страницы или None."""

    @classmethod
    def _extract_meta(cls, html: str) -> Dict[str, Optional[str]]:
        meta: Dict[str, Optional[str]] = dict(_EMPTY_META)
        if not html:
            return meta

        title_match = re.search(
            r"<title\b[^>]*>(.*?)</title>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if title_match:
            title = cls._meta_title_text(title_match.group(1)).strip()
            meta["title"] = title or None

        html_match = re.search(r"<html\b([^>]*)>", html, flags=re.IGNORECASE)
        if html_match:
            attrs = html_match.group(1)
            lang_match = re.search(
                r"""\blang\s*=\s*(['"])(.*?)\1""",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if lang_match:
                lang = html_lib.unescape(
                    re.sub(r"\s+", " ", lang_match.group(2))
                ).strip()
                meta["lang"] = lang or None

        description_raw = cls._find_meta_description(html)
        if description_raw is not None:
            description = html_lib.unescape(
                re.sub(r"\s+", " ", description_raw)
            ).strip()
            meta["description"] = description or None

        return cls._normalize_meta(meta)

    # --- тип содержимого -----------------------------------------------------

    @classmethod
    def _is_binary_content_type(cls, content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return False
        # Treat these as text-like
        if ct in cls._NON_BINARY_APPLICATION_TYPES:
            return False
        # Common binary types
        if ct.startswith("image/"):
            return True
        if ct in (
            "application/pdf",
            "application/zip",
            "application/gzip",
            "application/x-gzip",
            "application/x-tar",
            "application/octet-stream",
            "application/x-bzip2",
            "application/x-7z-compressed",
        ):
            return True
        # Default: unknown application/* considered binary to be safe
        return bool(ct.startswith("application/"))

    @staticmethod
    def _is_text_like_content_type(content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return True
        return ct in (
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/x-javascript",
            "application/x-www-form-urlencoded",
            "application/rss+xml",
            "application/atom+xml",
        )

    @staticmethod
    def _looks_binary_bytes(data: bytes) -> bool:
        if not data:
            return False
        sample = data[:4096]
        if b"\x00" in sample:
            return True
        printable = 0
        for b in sample:
            if b in (9, 10, 13) or 32 <= b <= 126:
                printable += 1
        return (printable / max(len(sample), 1)) < 0.75

    def _is_binary_response(
        self, content_type: str, raw_content: bytes
    ) -> bool:
        content_type_binary = self._is_binary_content_type(content_type)
        content_type_text = self._is_text_like_content_type(content_type)
        return content_type_binary or (
            not content_type_text and self._looks_binary_bytes(raw_content)
        )

    # --- проверка аргументов -------------------------------------------------

    def _url_policy_blocked_result(
        self,
        error: Dict[str, Any],
        url: str,
        **kwargs: Any,
    ) -> str:
        return self._result(
            url=url,
            ok=False,
            result_type="error",
            error=error,
        )

    def _fetch_arg_error(
        self, timeout, max_preview_chars, offset, limit
    ) -> Optional[Dict[str, Any]]:
        if timeout is not None and not isinstance(timeout, int):
            return {"type": _INVALID_ARGUMENT, "field": "timeout"}
        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return {"type": _INVALID_ARGUMENT, "field": "max_preview_chars"}
        if not isinstance(offset, int) or offset < 0:
            return {"type": _INVALID_ARGUMENT, "field": "offset"}
        if not isinstance(limit, int) or limit <= 0:
            return {"type": _INVALID_ARGUMENT, "field": "limit"}
        return None

    def _url_or_timeout_error(
        self, normalized_url: str, timeout
    ) -> Optional[Dict[str, Any]]:
        if not normalized_url:
            return {
                "type": _INVALID_ARGUMENT,
                "field": "url",
                "message": "URL cannot be empty.",
            }
        if not normalized_url.startswith(("http://", "https://")):
            return {
                "type": _INVALID_ARGUMENT,
                "field": "url",
                "message": "URL must start with http:// or https://.",
            }
        if (timeout or self.timeout_seconds) <= 0:
            return {
                "type": _INVALID_ARGUMENT,
                "field": "timeout",
                "message": "timeout must be greater than 0.",
            }
        return None

    def _begin_fetch(
        self,
        url: Any,
        timeout: Optional[int],
        max_preview_chars: int,
        offset: int,
        limit: Optional[int],
        proxy: Optional[str],
    ) -> Tuple[Optional[str], Optional[FetchRequest]]:
        """Проверка аргументов и кэш — всё, что отвечает без сети.

        ``(готовый ответ, None)`` — ошибка аргумента или попадание в кэш;
        ``(None, запрос)`` — пора идти в сеть.
        """
        if not isinstance(url, str):
            return self._result(
                ok=False,
                url=str(url),
                error={"type": _INVALID_ARGUMENT, "field": "url"},
            ), None

        _result = functools.partial(self._result, url=url)

        if limit is None:
            limit = self.max_content_chars
        arg_error = self._fetch_arg_error(
            timeout, max_preview_chars, offset, limit
        )
        if arg_error is not None:
            return _result(ok=False, error=arg_error), None
        normalized_proxy, proxy_error = self._normalize_proxy(proxy)
        if proxy_error is not None:
            return _result(ok=False, error=proxy_error), None
        normalized_url = url.strip()
        url_error = self._url_or_timeout_error(normalized_url, timeout)
        if url_error is not None:
            return _result(ok=False, error=url_error), None
        effective_timeout = timeout or self.timeout_seconds

        cache_key = self._cache_key(normalized_url, normalized_proxy)
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            cached_content, cached_meta = cached
            payload = self._content_payload(
                cached_content,
                offset,
                limit,
                source="cache",
                cache_hit=True,
                final_url=normalized_url,
                status_code=200,
                content_type="text/plain; charset=utf-8",
                meta=cached_meta,
            )
            return _result(**payload), None
        return None, FetchRequest(
            normalized_url,
            normalized_proxy,
            effective_timeout,
            offset,
            limit,
            cache_key,
        )

    def _browser_headers(self) -> Dict[str, str]:
        """Заголовки «как у браузера»: без них часть сайтов отдаёт заглушку."""
        return {
            "User-Agent": self.default_user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
        }

    # --- конверты ответа -----------------------------------------------------

    def _slice_content(
        self, content: str, offset: int, limit: int
    ) -> Dict[str, Any]:
        total_chars = len(content)
        if offset >= total_chars:
            return {
                "content": "",
                "returned_chars": 0,
                "total_chars": total_chars,
                "has_more": False,
            }
        chunk = content[offset : offset + limit]
        returned_chars = len(chunk)
        has_more = offset + returned_chars < total_chars
        return {
            "content": chunk,
            "returned_chars": returned_chars,
            "total_chars": total_chars,
            "has_more": has_more,
        }

    def _content_payload(
        self,
        content: str,
        offset: int,
        limit: int,
        *,
        source: str,
        cache_hit: bool,
        final_url,
        status_code,
        content_type,
        meta,
    ) -> Dict[str, Any]:
        sliced = self._slice_content(content, offset, limit)
        return dict(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=False,
            result_type="content",
            extracted_source=source,
            content=sliced["content"],
            preview="",
            truncated=sliced["has_more"],
            offset=offset,
            limit=limit,
            returned_chars=sliced["returned_chars"],
            total_chars=sliced["total_chars"],
            has_more=sliced["has_more"],
            cache_hit=cache_hit,
            meta=meta,
            error=None,
        )

    def _binary_payload(
        self, final_url, status_code, content_type, offset: int, limit: int
    ) -> Dict[str, Any]:
        """Бинарный ответ: байты в чат не отдаём, только пометку."""
        return dict(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=True,
            result_type="binary",
            content="binary data",
            preview="",
            offset=offset,
            limit=limit,
            returned_chars=0,
            total_chars=0,
            has_more=False,
            cache_hit=False,
            meta=self._normalize_meta(None),
            error=None,
        )

    def _preview_payload(
        self,
        stripped: str,
        max_preview_chars: int,
        *,
        final_url,
        status_code,
        content_type,
        meta,
    ) -> Dict[str, Any]:
        """Текст извлечь не удалось: отдаём превью без тегов и ошибку extraction_failed."""
        preview, truncated = preview_from_stripped(stripped, max_preview_chars)
        return dict(
            ok=True,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            is_binary=False,
            result_type="preview",
            extracted_source="stripped_html",
            content="",
            preview=preview,
            truncated=truncated,
            max_preview_chars=max_preview_chars if truncated else None,
            meta=meta,
            error={
                "type": "extraction_failed",
                "message": "Could not extract readable text. Returned preview.",
            },
        )

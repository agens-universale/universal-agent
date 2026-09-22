import asyncio
import functools
import html as html_lib
import json
import os
import re
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import httpx

from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.security.url_policy import enforce_url_policy, validate_url_policy, UrlPolicyDecision
from gc_core.utils.async_http import get_async_client
from gc_core.utils.http_proxy import resolve_proxies
import asyncio
from gc_core.tools.exec.redirect_policy import (
    DEFAULT_MAX_REDIRECTS,
    RedirectBlocked as _SharedRedirectBlocked,
    TooManyRedirects as _TooManyRedirects,
    fetch_validating_redirects,
)
from gc_core.security.path_policy import resolve_tool_context


def _response_url(exc: Exception) -> "str | None":
    """URL из ответа-ошибки строкой: httpx.URL в JSON не сериализуется."""
    response = getattr(exc, "response", None)
    url = getattr(response, "url", None)
    return str(url) if url is not None else None


#: Псевдоним общего исключения: петля редиректов теперь одна на три тула
#: (``tools/exec/redirect_policy``), а имя оставлено — на него ссылаются
#: ветки обработки ниже.
_RedirectBlocked = _SharedRedirectBlocked

@loop_warn_on_repeat(**READ_PROFILE)
class SimpleFetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
    max_content_chars = 20_000
    # LRU-лимит кэша извлечённого текста страниц (на тред/сессию).
    # Кэш хранится в SharedStateData (per-thread, персистится в meta),
    # поэтому лимит ограничивает и RAM, и размер JSON-meta треда.
    # Должен совпадать с FetchUrl.fetch_url_cache_max_size — оба делят
    # shared.fetch_url_cache.
    fetch_url_cache_max_size = 10
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return {
            "type": "function",
            "name": "exec_fetch_url",
            "description": (
                "Fetches a URL via HTTP GET and returns readable text extracted from HTML without heavy dependencies. "
                "If the response is binary, returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to fetch (http or https)."},
                    "timeout": {"type": "integer", "description": f"HTTP timeout in seconds. Default {cls.timeout_seconds}."},
                    "max_preview_chars": {"type": "integer", "description": "Maximum number of characters for preview. Default 1200."},
                    "offset": {"type": "integer", "description": "Character offset for paginated reads from cached full content. Default 0."},
                    "limit": {"type": "integer", "description": f"Maximum number of characters to return in content. Default {cls.max_content_chars}."},
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
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return (
            "Tool name: exec_fetch_url\n"
            "Tool description: Downloads a URL and extracts readable text using a lightweight HTML cleaner. "
            "Optional proxy (http/https/socks5/socks5h/socks4); empty string or omit = no proxy."
        )

    @staticmethod
    def _result(
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
            "tool": SimpleFetchUrl.registered_tool_name(),
            "ok": ok,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "is_binary": is_binary,
            "result_type": result_type,
            "extracted_source": extracted_source,
            "truncated": truncated,
            "meta": meta if meta is not None else {"title": None, "description": None, "lang": None},
            "error": error,
        }
        if max_content_chars is not None:
            payload["max_content_chars"] = max_content_chars
        if max_preview_chars is not None:
            payload["max_preview_chars"] = max_preview_chars
        if offset is not None:
            payload["offset"] = offset
        if limit is not None:
            payload["limit"] = limit
        if returned_chars is not None:
            payload["returned_chars"] = returned_chars
        if total_chars is not None:
            payload["total_chars"] = total_chars
        if has_more is not None:
            payload["has_more"] = has_more
        if has_more is not None and offset is not None and limit is not None:
            payload["next_offset"] = offset + limit if has_more else None
        if cache_hit is not None:
            payload["cache_hit"] = cache_hit
        payload["content"] = content
        payload["preview"] = preview
        return json.dumps(payload, ensure_ascii=False)

    def _get_cache(self) -> Optional["OrderedDict[str, Any]"]:
        shared = getattr(self._state, "shared", None)
        if shared is None:
            return None
        cache = getattr(shared, "fetch_url_cache", None)
        # Нормализация: из meta кэш восстанавливается как обычный dict,
        # а для LRU нужен OrderedDict. Если тип не совпадает — пересобираем.
        if not isinstance(cache, OrderedDict):
            cache = OrderedDict(cache if isinstance(cache, dict) else {})
            setattr(shared, "fetch_url_cache", cache)
        return cache

    @staticmethod
    def _cache_key(url: str, proxy: Optional[str] = None) -> str:
        """Ключ кэша: URL, при наличии proxy — URL+proxy (разные маршруты не смешиваются)."""
        if proxy:
            return f"{url}||proxy={proxy}"
        return url

    _PROXY_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")

    @classmethod
    def _normalize_proxy(cls, proxy: Optional[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Возвращает (normalized_proxy | None, error | None)."""
        if proxy is None:
            return None, None
        if not isinstance(proxy, str):
            return None, {"type": "invalid_argument", "field": "proxy"}
        normalized = proxy.strip()
        if not normalized:
            # Пустая / whitespace-only строка = прокси не задан.
            return None, None
        lower = normalized.lower()
        if not lower.startswith(cls._PROXY_SCHEMES):
            return None, {
                "type": "invalid_argument",
                "field": "proxy",
                "message": (
                    "proxy must start with http://, https://, socks5://, "
                    "socks5h://, socks4://, or socks4a://."
                ),
            }
        return normalized, None

    @staticmethod
    def _proxies_dict(proxy: Optional[str]) -> Optional[Dict[str, str]]:
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

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

    @staticmethod
    def _normalize_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
        normalized: Dict[str, Optional[str]] = {"title": None, "description": None, "lang": None}
        if not isinstance(meta, dict):
            return normalized
        for key in ("title", "description", "lang"):
            value = meta.get(key)
            if isinstance(value, str):
                value = value.strip()
                normalized[key] = value or None
        return normalized

    @classmethod
    def _extract_meta(cls, html: str) -> Dict[str, Optional[str]]:
        meta: Dict[str, Optional[str]] = {"title": None, "description": None, "lang": None}
        if not html:
            return meta

        title_match = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = cls._strip_tags(title_match.group(1)).strip()
            meta["title"] = title or None

        html_match = re.search(r"<html\b([^>]*)>", html, flags=re.IGNORECASE)
        if html_match:
            attrs = html_match.group(1)
            lang_match = re.search(r"""\blang\s*=\s*(['"])(.*?)\1""", attrs, flags=re.IGNORECASE | re.DOTALL)
            if lang_match:
                lang = html_lib.unescape(re.sub(r"\s+", " ", lang_match.group(2))).strip()
                meta["lang"] = lang or None

        description_raw = cls._find_meta_description(html)
        if description_raw is not None:
            description = html_lib.unescape(re.sub(r"\s+", " ", description_raw)).strip()
            meta["description"] = description or None

        return cls._normalize_meta(meta)

    @staticmethod
    def _find_meta_description(html: str) -> Optional[str]:
        """Сырое значение ``content`` первого ``<meta name="description">``."""
        for tag_match in re.finditer(r"<meta\b([^>]*)>", html, flags=re.IGNORECASE | re.DOTALL):
            attrs = tag_match.group(1)
            if not re.search(r"""\bname\s*=\s*(['"])description\1""", attrs, flags=re.IGNORECASE):
                continue
            content_match = re.search(
                r"""\bcontent\s*=\s*(['"])(.*?)\1""", attrs, flags=re.IGNORECASE | re.DOTALL
            )
            if content_match:
                return content_match.group(2)
        return None

    @staticmethod
    def _is_binary_content_type(content_type: str) -> bool:
        ct = (content_type or "").split(";")[0].strip().lower()
        if not ct:
            return False
        if ct.startswith("text/"):
            return False
        if ct in (
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/javascript",
            "application/x-javascript",
        ):
            return False
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
        if ct.startswith("application/"):
            return True
        return False

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

    @staticmethod
    def _strip_tags(fragment: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", fragment)
        text = html_lib.unescape(without_tags)
        text = re.sub(r"[ \t]{2,}", "  ", text)
        return text.strip()

    @classmethod
    def _html_block_to_text(cls, match: re.Match[str]) -> str:
        inner = cls._strip_tags(match.group(2))
        if not inner:
            return "\n\n"
        return f"\n\n{inner}\n\n"

    @classmethod
    def _html_list_item_to_text(cls, match: re.Match[str]) -> str:
        inner = cls._strip_tags(match.group(1))
        if not inner:
            return "\n"
        return f"\n- {inner}\n"

    @classmethod
    def _html_heading_to_markdown(cls, match: re.Match[str]) -> str:
        tag_name = (match.group(1) or "").lower()
        inner = cls._strip_tags(match.group(2))
        if not inner:
            return "\n\n"
        level = 1
        if len(tag_name) == 2 and tag_name.startswith("h") and tag_name[1].isdigit():
            level = max(1, min(6, int(tag_name[1])))
        return f"\n\n{'#' * level} {inner}\n\n"

    @staticmethod
    def _html_pre_to_text(match: re.Match[str]) -> str:
        inner = match.group(1) or ""
        inner = re.sub(r"(?i)<br\s*/?>", "\n", inner)
        inner = re.sub(r"<[^>]+>", "", inner)
        inner = html_lib.unescape(inner)
        inner = inner.strip("\n")
        if not inner:
            return "\n\n"
        return f"\n\n{inner}\n\n"

    @classmethod
    def _html_table_cell_to_text(cls, match: re.Match[str]) -> str:
        return f" {cls._strip_tags(match.group(2))} |"

    @classmethod
    def _extract_simple_html_text(cls, html: str) -> str:
        content = html or ""
        content = re.sub(r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</tr\s*>", "\n", content)
        content = re.sub(r"(?is)<(p|div)\b[^>]*>(.*?)</\1>", cls._html_block_to_text, content)
        content = re.sub(r"(?is)<(h[1-6])\b[^>]*>(.*?)</\1>", cls._html_heading_to_markdown, content)
        content = re.sub(r"(?is)<li\b[^>]*>(.*?)</li>", cls._html_list_item_to_text, content)
        content = re.sub(r"(?i)</?(ul|ol)\b[^>]*>", "\n", content)
        content = re.sub(r"(?is)<pre\b[^>]*>(.*?)</pre>", cls._html_pre_to_text, content)
        content = re.sub(r"(?is)<(td|th)\b[^>]*>(.*?)</\1>", cls._html_table_cell_to_text, content)
        content = re.sub(r"(?i)</?(table|thead|tbody|tfoot)\b[^>]*>", "\n", content)
        content = cls._strip_tags(content)
        content = re.sub(r"[ \t]*\|[ \t]*(\n|$)", r"\1", content)
        # Normalize whitespace-only lines to real empty lines before collapsing.
        content = re.sub(r"\n[ \t\n]+\n", "\n\n", content)
        content = re.sub(r"(?:\n[ \t]+\n)+", "\n\n", content)
        content = re.sub(r"\r\n?", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = re.sub(r"[ \t]+([,.;:!?…])", r"\1", content)
        content = re.sub(r"[ \t]{2,}", "  ", content)
        return content.strip()

    def _slice_content(self, content: str, offset: int, limit: int) -> Dict[str, Any]:
        total_chars = len(content)
        if offset >= total_chars:
            return {"content": "", "returned_chars": 0, "total_chars": total_chars, "has_more": False}
        chunk = content[offset : offset + limit]
        returned_chars = len(chunk)
        has_more = offset + returned_chars < total_chars
        return {"content": chunk, "returned_chars": returned_chars, "total_chars": total_chars, "has_more": has_more}

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


    def _fetch_arg_error(self, timeout, max_preview_chars, offset, limit) -> "Optional[Dict[str, Any]]":
        if timeout is not None and not isinstance(timeout, int):
            return {"type": "invalid_argument", "field": "timeout"}
        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return {"type": "invalid_argument", "field": "max_preview_chars"}
        if not isinstance(offset, int) or offset < 0:
            return {"type": "invalid_argument", "field": "offset"}
        if not isinstance(limit, int) or limit <= 0:
            return {"type": "invalid_argument", "field": "limit"}
        return None

    def _url_or_timeout_error(self, normalized_url: str, timeout) -> "Optional[Dict[str, Any]]":
        if not normalized_url:
            return {"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."}
        if not normalized_url.startswith(("http://", "https://")):
            return {"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."}
        if (timeout or self.timeout_seconds) <= 0:
            return {"type": "invalid_argument", "field": "timeout", "message": "timeout must be greater than 0."}
        return None

    def _cached_payload(self, cached, normalized_url: str, offset: int, limit: int) -> Dict[str, Any]:
        cached_content, cached_meta = cached
        sliced = self._slice_content(cached_content, offset, limit)
        return dict(
            ok=True,
            final_url=normalized_url,
            status_code=200,
            content_type="text/plain; charset=utf-8",
            is_binary=False,
            result_type="content",
            extracted_source="cache",
            content=sliced["content"],
            truncated=sliced["has_more"],
            offset=offset,
            limit=limit,
            returned_chars=sliced["returned_chars"],
            total_chars=sliced["total_chars"],
            has_more=sliced["has_more"],
            cache_hit=True,
            meta=cached_meta,
            error=None,
        )

    async def _arequest_with_retries(self, normalized_url: str, headers: Dict[str, str], effective_timeout, normalized_proxy):
        """(response, None) либо (None, kwargs ошибки для _result)."""
        attempts = 1 + self.max_retries
        # Политику берём ТЕМ ЖЕ путём, что и гвард `enforce_url_policy`, —
        # через `resolve_tool_context`. Политика лежит на ВЕРХНЕМ уровне
        # (`path_policy.resolve_tool_context`); чтение config_dict напрямую давало
        # пустой чёрный список и per-hop проверка ничего не проверяла.
        _policy = resolve_tool_context(self, ctx=self._ctx, state=self._state)
        _allowed_schemes = list(_policy.url_allowed_schemes or ["http", "https"])
        _url_blacklist = list(_policy.url_blacklist or [])
        # Потолок ОБЩИЙ: три тула не должны расходиться в том, сколько
        # переходов считается петлёй.
        _max_redirects = DEFAULT_MAX_REDIRECTS

        # Клиент общий и переиспользуемый (пул соединений), закрывать его здесь
        # нельзя — он принадлежит loop, а не одному вызову.
        session = get_async_client(
            # Явный `proxy` тула — первым аргументом, иначе он молча
            # игнорируется (тот же промах, что был в fetch_url).
            proxies=resolve_proxies(normalized_proxy, os.environ, normalized_url)
        )
        for attempt in range(1, attempts + 1):
            try:
                # Общая петля из ``redirect_policy``: одно правило на три тула.
                response = await fetch_validating_redirects(
                    session, normalized_url, headers, effective_timeout,
                    allowed_schemes=_allowed_schemes,
                    url_blacklist=_url_blacklist,
                    max_redirects=_max_redirects,
                )
                response.raise_for_status()
                return response, None
            except _TooManyRedirects as loop_exc:
                # Петля редиректов — сетевая проблема, а не запрет политики.
                return None, dict(
                    ok=False,
                    final_url=loop_exc.url,
                    status_code=None,
                    content_type=None,
                    result_type="error",
                    error={
                        "type": "too_many_redirects",
                        "reason": str(loop_exc),
                        "limit": loop_exc.limit,
                    },
                )
            except _RedirectBlocked as redirect_exc:
                _decision = redirect_exc.decision
                return None, dict(
                    ok=False,
                    final_url=_decision.url,
                    status_code=None,
                    content_type=None,
                    result_type="error",
                    error={
                        "type": _decision.error_type or "url_blocked_by_policy",
                        "reason": _decision.reason,
                        "matched_rule": _decision.matched_rule,
                        "blocked_on_redirect": True,
                    },
                )
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status in (429,) or status >= 500
                if (not retryable) or attempt == attempts:
                    return None, dict(
                        ok=False,
                        final_url=_response_url(exc),
                        status_code=status,
                        content_type=getattr(getattr(exc, "response", None), "headers", {}).get("Content-Type")
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
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        return None, dict(ok=False, result_type="error", error={"type": "request_failed", "message": "Unknown request failure."})

    def _preview_from_html(self, text: str, max_preview_chars: int) -> "tuple[str, bool]":
        preview_content = re.sub(r"\s+", " ", self._strip_tags(text)).strip()
        truncated = len(preview_content) > max_preview_chars
        if truncated:
            preview_content = preview_content[:max_preview_chars]
        header = (
            f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
            if truncated
            else "Content with removed HTML tags"
        )
        return f"{header}:\n\n{preview_content}", truncated

    @enforce_url_policy(url_arg="url")
    async def acall(
        self,
        url: str,
        timeout: Optional[int] = None,
        max_preview_chars: int = 1200,
        offset: int = 0,
        limit: Optional[int] = None,
        proxy: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        if not isinstance(url, str):
            return self._result(ok=False, url=str(url), error={"type": "invalid_argument", "field": "url"})

        _result = functools.partial(self._result, url=url)

        if limit is None:
            limit = self.max_content_chars
        arg_error = self._fetch_arg_error(timeout, max_preview_chars, offset, limit)
        if arg_error is not None:
            return _result(ok=False, error=arg_error)
        normalized_proxy, proxy_error = self._normalize_proxy(proxy)
        if proxy_error is not None:
            return _result(ok=False, error=proxy_error)
        normalized_url = url.strip()
        url_error = self._url_or_timeout_error(normalized_url, timeout)
        if url_error is not None:
            return _result(ok=False, error=url_error)
        effective_timeout = timeout or self.timeout_seconds

        cache_key = self._cache_key(normalized_url, normalized_proxy)
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return _result(**self._cached_payload(cached, normalized_url, offset, limit))

        headers = {
            "User-Agent": self.default_user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
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

        response, request_error = await self._arequest_with_retries(normalized_url, headers, effective_timeout, normalized_proxy)
        if request_error is not None:
            return _result(**request_error)

        content_type = response.headers.get("Content-Type", "")
        final_url = str(response.url)
        status_code = response.status_code
        raw_content = getattr(response, "content", b"")
        content_type_binary = self._is_binary_content_type(content_type)
        content_type_text = self._is_text_like_content_type(content_type)
        is_binary = content_type_binary or (not content_type_text and self._looks_binary_bytes(raw_content))
        if is_binary:
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=True,
                result_type="binary",
                content="binary data",
                offset=offset,
                limit=limit,
                returned_chars=0,
                total_chars=0,
                has_more=False,
                cache_hit=False,
                error=None,
            )

        text = response.text or ""
        page_meta = self._extract_meta(text)
        # Разбор — десяток `re.sub` с бэктрекингом по всему документу, то есть
        # чистый CPU на размере страницы. У `fetch_url` он по этой же причине
        # давно уходит в поток (замер там: 237 мс инлайн против 22 мс), а
        # здесь оставался на loop.
        extracted = await asyncio.to_thread(self._extract_simple_html_text, text)
        if extracted:
            self._put_to_cache(cache_key, extracted, page_meta)
            sliced = self._slice_content(extracted, offset, limit)
            return _result(
                ok=True,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                is_binary=False,
                result_type="content",
                extracted_source="simple_regex",
                content=sliced["content"],
                truncated=sliced["has_more"],
                offset=offset,
                limit=limit,
                returned_chars=sliced["returned_chars"],
                total_chars=sliced["total_chars"],
                has_more=sliced["has_more"],
                cache_hit=False,
                meta=page_meta,
                error=None,
            )

        preview, truncated = self._preview_from_html(text, max_preview_chars)
        return _result(
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
            meta=page_meta,
            error={"type": "extraction_failed", "message": "Could not extract readable text. Returned preview."},
        )


FetchUrl = SimpleFetchUrl

import asyncio
import functools
import html as html_lib
import os
import re
from typing import Any, Dict, Optional

import httpx

from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.security.path_policy import resolve_tool_context
from gc_core.security.url_policy import enforce_url_policy
from gc_core.tools.exec.fetch_url_base import FetchUrlBase, http_error_failure
from gc_core.tools.exec.redirect_policy import (
    DEFAULT_MAX_REDIRECTS,
    fetch_validating_redirects,
)
from gc_core.tools.exec.redirect_policy import (
    RedirectBlocked as _SharedRedirectBlocked,
)
from gc_core.tools.exec.redirect_policy import (
    TooManyRedirects as _TooManyRedirects,
)
from gc_core.utils.async_http import get_async_client
from gc_core.utils.http_proxy import resolve_proxies

#: Псевдоним общего исключения: петля редиректов теперь одна на три тула
#: (``tools/exec/redirect_policy``), а имя оставлено — на него ссылаются
#: ветки обработки ниже.
_RedirectBlocked = _SharedRedirectBlocked


@loop_warn_on_repeat(**READ_PROFILE)
class SimpleFetchUrl(FetchUrlBase):
    #: application/*, которые не считаются бинарными (см. FetchUrlBase).
    #: В отличие от fetch_url, x-www-form-urlencoded сюда не входит.
    _NON_BINARY_APPLICATION_TYPES = (
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
        "application/x-javascript",
    )

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return cls._tool_spec(
            description=(
                "Fetches a URL via HTTP GET and returns readable text extracted from HTML without heavy dependencies. "
                "If the response is binary, returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit."
            ),
            timeout_description=f"HTTP timeout in seconds. Default {cls.timeout_seconds}.",
            preview_description="Maximum number of characters for preview. Default 1200.",
        )

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return (
            "Tool name: exec_fetch_url\n"
            "Tool description: Downloads a URL and extracts readable text using a lightweight HTML cleaner. "
            "Optional proxy (http/https/socks5/socks5h/socks4); empty string or omit = no proxy."
        )

    @classmethod
    def _meta_title_text(cls, fragment: str) -> str:
        return cls._strip_tags(fragment)

    @staticmethod
    def _find_meta_description(html: str) -> Optional[str]:
        """Сырое значение ``content`` первого ``<meta name="description">``."""
        for tag_match in re.finditer(
            r"<meta\b([^>]*)>", html, flags=re.IGNORECASE | re.DOTALL
        ):
            attrs = tag_match.group(1)
            if not re.search(
                r"""\bname\s*=\s*(['"])description\1""",
                attrs,
                flags=re.IGNORECASE,
            ):
                continue
            content_match = re.search(
                r"""\bcontent\s*=\s*(['"])(.*?)\1""",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if content_match:
                return content_match.group(2)
        return None

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
        if (
            len(tag_name) == 2
            and tag_name.startswith("h")
            and tag_name[1].isdigit()
        ):
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
        content = re.sub(
            r"(?is)<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>",
            " ",
            content,
        )
        content = re.sub(r"(?i)<br\s*/?>", "\n", content)
        content = re.sub(r"(?i)</tr\s*>", "\n", content)
        content = re.sub(
            r"(?is)<(p|div)\b[^>]*>(.*?)</\1>",
            cls._html_block_to_text,
            content,
        )
        content = re.sub(
            r"(?is)<(h[1-6])\b[^>]*>(.*?)</\1>",
            cls._html_heading_to_markdown,
            content,
        )
        content = re.sub(
            r"(?is)<li\b[^>]*>(.*?)</li>", cls._html_list_item_to_text, content
        )
        content = re.sub(r"(?i)</?(ul|ol)\b[^>]*>", "\n", content)
        content = re.sub(
            r"(?is)<pre\b[^>]*>(.*?)</pre>", cls._html_pre_to_text, content
        )
        content = re.sub(
            r"(?is)<(td|th)\b[^>]*>(.*?)</\1>",
            cls._html_table_cell_to_text,
            content,
        )
        content = re.sub(
            r"(?i)</?(table|thead|tbody|tfoot)\b[^>]*>", "\n", content
        )
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

    async def _arequest_with_retries(
        self,
        normalized_url: str,
        headers: Dict[str, str],
        effective_timeout,
        normalized_proxy,
    ):
        """(response, None) либо (None, kwargs ошибки для _result)."""
        attempts = 1 + self.max_retries
        # Политику берём ТЕМ ЖЕ путём, что и гвард `enforce_url_policy`, —
        # через `resolve_tool_context`. Политика лежит на ВЕРХНЕМ уровне
        # (`path_policy.resolve_tool_context`); чтение config_dict напрямую давало
        # пустой чёрный список и per-hop проверка ничего не проверяла.
        _policy = resolve_tool_context(self, ctx=self._ctx, state=self._state)
        _allowed_schemes = list(
            _policy.url_allowed_schemes or ["http", "https"]
        )
        _url_blacklist = list(_policy.url_blacklist or [])
        # Потолок ОБЩИЙ: три тула не должны расходиться в том, сколько
        # переходов считается петлёй.
        _max_redirects = DEFAULT_MAX_REDIRECTS

        # Клиент общий и переиспользуемый (пул соединений), закрывать его здесь
        # нельзя — он принадлежит loop, а не одному вызову.
        session = get_async_client(
            # Явный `proxy` тула — первым аргументом, иначе он молча
            # игнорируется (тот же промах, что был в fetch_url).
            proxies=resolve_proxies(
                normalized_proxy, os.environ, normalized_url
            )
        )
        for attempt in range(1, attempts + 1):
            try:
                # Общая петля из ``redirect_policy``: одно правило на три тула.
                response = await fetch_validating_redirects(
                    session,
                    normalized_url,
                    headers,
                    effective_timeout,
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
                        "type": _decision.error_type
                        or "url_blocked_by_policy",
                        "reason": _decision.reason,
                        "matched_rule": _decision.matched_rule,
                        "blocked_on_redirect": True,
                    },
                )
            except httpx.HTTPError as exc:
                failure = http_error_failure(exc, attempt, attempts)
                if failure is not None:
                    return None, failure
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        return None, dict(
            ok=False,
            result_type="error",
            error={
                "type": "request_failed",
                "message": "Unknown request failure.",
            },
        )

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
        # Проверка аргументов и попадание в кэш — общие с fetch_url.
        early, request = self._begin_fetch(
            url, timeout, max_preview_chars, offset, limit, proxy
        )
        if request is None:
            return early
        _result = functools.partial(self._result, url=url)
        headers = self._browser_headers()

        response, request_error = await self._arequest_with_retries(
            request.url, headers, request.timeout, request.proxy
        )
        if request_error is not None:
            return _result(**request_error)

        content_type = response.headers.get("Content-Type", "")
        final_url = str(response.url)
        status_code = response.status_code
        raw_content = getattr(response, "content", b"")
        if self._is_binary_response(content_type, raw_content):
            return _result(
                **self._binary_payload(
                    final_url,
                    status_code,
                    content_type,
                    request.offset,
                    request.limit,
                )
            )

        text = response.text or ""
        page_meta = self._extract_meta(text)
        # Разбор — десяток `re.sub` с бэктрекингом по всему документу, то есть
        # чистый CPU на размере страницы. У `fetch_url` он по этой же причине
        # давно уходит в поток (замер там: 237 мс инлайн против 22 мс), а
        # здесь оставался на loop.
        extracted = await asyncio.to_thread(
            self._extract_simple_html_text, text
        )
        if extracted:
            self._put_to_cache(request.cache_key, extracted, page_meta)
            return _result(
                **self._content_payload(
                    extracted,
                    request.offset,
                    request.limit,
                    source="simple_regex",
                    cache_hit=False,
                    final_url=final_url,
                    status_code=status_code,
                    content_type=content_type,
                    meta=page_meta,
                )
            )

        stripped = re.sub(r"\s+", " ", self._strip_tags(text)).strip()
        return _result(
            **self._preview_payload(
                stripped,
                max_preview_chars,
                final_url=final_url,
                status_code=status_code,
                content_type=content_type,
                meta=page_meta,
            )
        )


FetchUrl = SimpleFetchUrl

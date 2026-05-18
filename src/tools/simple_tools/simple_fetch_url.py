import functools
import html as html_lib
import json
import re
import time
from typing import Any, Dict, Optional

import requests

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
from security.url_policy import enforce_url_policy


class SimpleFetchUrl(AbstractSimpleTool):
    timeout_seconds = 20
    max_content_chars = 20_000
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
    def tool_description(cls):
        return {
            "type": "function",
            "name": "fetch_url",
            "description": (
                "Fetches a URL via HTTP GET and returns readable text extracted from HTML without heavy dependencies. "
                "If the response is binary, returns content='binary data' with is_binary=true. "
                "Stores full extracted text in session in-memory cache by URL and supports paginated reads via offset/limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to fetch (http or https)."},
                    "timeout_seconds": {"type": "integer", "description": f"Optional timeout in seconds. Default {cls.timeout_seconds}."},
                    "max_preview_chars": {"type": "integer", "description": "Maximum number of characters for preview. Default 1200."},
                    "offset": {"type": "integer", "description": "Character offset for paginated reads from cached full content. Default 0."},
                    "limit": {"type": "integer", "description": f"Maximum number of characters to return in content. Default {cls.max_content_chars}."},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls):
        return "Tool name: fetch_url\nTool description: Downloads a URL and extracts readable text using a lightweight HTML cleaner."

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
            "tool": "fetch_url",
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

    def _get_cache(self) -> Optional[Dict[str, Any]]:
        shared = getattr(self._state, "shared", None)
        if shared is None:
            return None
        cache = getattr(shared, "fetch_url_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(shared, "fetch_url_cache", cache)
        return cache

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

        desc_match = re.search(
            r"""<meta\b(?=[^>]*\bname\s*=\s*(['"])description\1)(?=[^>]*\bcontent\s*=\s*(['"])(.*?)\2)[^>]*>""",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if desc_match:
            description = html_lib.unescape(re.sub(r"\s+", " ", desc_match.group(3))).strip()
            meta["description"] = description or None

        return cls._normalize_meta(meta)

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
        content = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?>.*?</\1>", " ", content)
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

    @enforce_url_policy(url_arg="url")
    def __call__(
        self,
        url: str,
        timeout_seconds: Optional[int] = None,
        max_preview_chars: int = 1200,
        offset: int = 0,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> str:
        if not isinstance(url, str):
            return self._result(ok=False, url=str(url), error={"type": "invalid_argument", "field": "url"})

        _result = functools.partial(self._result, url=url)

        if timeout_seconds is not None and not isinstance(timeout_seconds, int):
            return _result(ok=False, error={"type": "invalid_argument", "field": "timeout_seconds"})
        if not isinstance(max_preview_chars, int) or max_preview_chars <= 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "max_preview_chars"})
        if not isinstance(offset, int) or offset < 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "offset"})
        if limit is None:
            limit = self.max_content_chars
        if not isinstance(limit, int) or limit <= 0:
            return _result(ok=False, error={"type": "invalid_argument", "field": "limit"})

        normalized_url = url.strip()
        if not normalized_url:
            return _result(ok=False, error={"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."})
        if not normalized_url.startswith(("http://", "https://")):
            return _result(ok=False, error={"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."})

        effective_timeout = timeout_seconds or self.timeout_seconds
        if effective_timeout <= 0:
            return _result(
                ok=False,
                error={"type": "invalid_argument", "field": "timeout_seconds", "message": "timeout_seconds must be greater than 0."},
            )

        cache = self._get_cache()
        if cache is not None and normalized_url in cache:
            cached = cache[normalized_url]
            if isinstance(cached, dict):
                cached_content = cached.get("content", "")
                cached_meta = self._normalize_meta(cached.get("meta"))
            else:
                cached_content = cached if isinstance(cached, str) else ""
                cached_meta = self._normalize_meta(None)
            sliced = self._slice_content(cached_content, offset, limit)
            return _result(
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

        response = None
        attempts = 1 + self.max_retries
        with requests.Session() as session:
            for attempt in range(1, attempts + 1):
                try:
                    response = session.get(normalized_url, headers=headers, timeout=effective_timeout, allow_redirects=True)
                    response.raise_for_status()
                    break
                except requests.RequestException as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    retryable = status is None or status in (429,) or status >= 500
                    if (not retryable) or attempt == attempts:
                        return _result(
                            ok=False,
                            final_url=getattr(getattr(exc, "response", None), "url", None),
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
                    time.sleep(self.retry_backoff_seconds * attempt)

        if response is None:
            return _result(ok=False, result_type="error", error={"type": "request_failed", "message": "Unknown request failure."})

        content_type = response.headers.get("Content-Type", "")
        final_url = response.url
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
        extracted = self._extract_simple_html_text(text)
        if extracted:
            if cache is not None:
                cache[normalized_url] = {"content": extracted, "meta": page_meta}
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

        preview_content = re.sub(r"\s+", " ", self._strip_tags(text)).strip()
        truncated = len(preview_content) > max_preview_chars
        if truncated:
            preview_content = preview_content[:max_preview_chars]
        header = (
            f"Content with removed HTML tags (truncated to {max_preview_chars} characters)"
            if truncated
            else "Content with removed HTML tags"
        )
        preview = f"{header}:\n\n{preview_content}"
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

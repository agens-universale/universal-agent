import asyncio
import json
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx

from gc_core.constants.tool_names import DOWNLOAD_FILE_NAME
from gc_core.security.path_policy import WriteGuardViolation, audit_policy_decision, check_write_path_allowed, normalize_path_for_policy, resolve_tool_context
from gc_core.security.url_policy import UrlPolicyDecision, enforce_url_policy, validate_url_policy
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.tool_report_paths import tool_report_abspath
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.aio_compat import run_coro_blocking
from gc_core.utils.async_http import get_async_client
from gc_core.utils.http_proxy import resolve_proxies


class _RedirectBlocked(Exception):
    def __init__(self, decision: UrlPolicyDecision) -> None:
        super().__init__(decision.reason or "redirect blocked")
        self.decision = decision


class DownloadFile(AbstractSimpleTool):
    timeout_seconds = 20
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    ALLOWED_EXTENSIONS = frozenset({
        ".pdf", ".docx", ".xls", ".xlsx", ".pptx", ".csv", ".html", ".msg",
        ".txt", ".md", ".json",
    })
    _MAX_REDIRECTS = 10
    _BOM_PREFIXES = (
        b"\xef\xbb\xbf",
        b"\xff\xfe",
        b"\xfe\xff",
    )

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if ctx is not None:  # keep the base-class LocalFsBackend fallback when ctx is absent
            self._ctx = ctx
        self._state = state

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        extensions = ", ".join(sorted(ext.lstrip(".") for ext in cls.ALLOWED_EXTENSIONS))
        return {
            "type": "function",
            "name": DOWNLOAD_FILE_NAME,
            "description": (
                "Downloads a document or plain-text file from an http(s) URL and saves it to the "
                "configured download directory under the project. "
                f"Allowed extensions: {extensions}. "
                "Returns JSON with the absolute saved path for follow-up reading or analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http or https URL.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Basename only, e.g. report.pdf. Must have an allowed extension.",
                    },
                },
                "required": ["url", "filename"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return (
            "Tool name: download_file\n"
            "Tool description: Downloads a document or plain-text file from a URL into the configured download directory.\n\n"
            "Usage:\n"
            "- Use when you need a local copy of a PDF, DOCX, XLSX, TXT, MD, JSON, etc. from a public http(s) URL.\n"
            "- Only the basename is passed as filename; the directory comes from configuration.\n"
            "- On success, use the returned path with a document-reading tool to extract text.\n"
            "- Do not use shell download commands for these formats when this tool is available.\n"
            "- filename must include a supported extension (.pdf, .docx, .xlsx, .txt, .md, .json, ...).\n"
        )

    def _result(self, 
        *,
        ok: bool = False,
        path: str = "",
        filename: str = "",
        bytes_written: int = 0,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps(
            {
                "tool": DownloadFile.registered_tool_name(),
                "ok": ok,
                "path": tool_report_abspath(path, workspace=self._workspace) if path else "",
                "filename": filename,
                "bytes_written": bytes_written,
                "error": error,
            },
            ensure_ascii=False,
        )

    def _get_config_dict(self) -> Dict[str, Any]:
        explicit = self._config_dict
        if isinstance(explicit, dict):
            return explicit
        ctx_config = getattr(self._ctx, "config_dict", None)
        if isinstance(ctx_config, dict):
            return ctx_config
        return {}

    def _resolve_download_settings(self, launch_root: str) -> tuple[str, int] | Dict[str, Any]:
        cfg = self._get_config_dict().get("download") or {}
        raw = os.path.expanduser(os.path.expandvars(str(cfg.get("download_dir") or "downloads").strip()))
        if not raw:
            return {"type": "invalid_download_dir", "message": "download_dir must not be empty."}
        try:
            download_root = str(normalize_path_for_policy(raw, launch_root))
        except ValueError:
            return {"type": "invalid_download_dir", "message": "download_dir resolved to an empty path."}
        max_bytes = int(cfg.get("max_bytes") or 52_428_800)
        return download_root, max_bytes

    @classmethod
    def _validate_filename(cls, filename: Any) -> tuple[str, str] | Dict[str, Any]:
        if not filename or not isinstance(filename, str):
            return {"type": "invalid_argument", "field": "filename"}
        name = filename.strip()
        if not name or name in (".", ".."):
            return {"type": "invalid_filename", "message": "filename must be a non-empty basename."}
        if os.path.basename(name) != name or "/" in name or "\\" in name:
            return {"type": "invalid_filename", "message": "filename must be a basename without path separators."}
        if "\x00" in name or "\r" in name or "\n" in name:
            return {"type": "invalid_filename", "message": "filename contains unsupported characters (null byte or newline)."}
        if len(name) > 255:
            return {"type": "invalid_filename", "message": "filename is too long."}
        ext = os.path.splitext(name)[1].lower()
        if ext not in cls.ALLOWED_EXTENSIONS:
            return {"type": "format_blocked", "reason": "extension not allowed"}
        return name, ext

    @classmethod
    def _validate_magic(cls, body: bytes, ext: str) -> bool:
        if not body:
            return False
        if ext == ".pdf":
            return body.startswith(b"%PDF-")
        if ext in {".docx", ".xlsx", ".pptx"}:
            return body.startswith(b"PK\x03\x04")
        if ext in {".xls", ".msg"}:
            return body.startswith(b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1")
        if ext in {".csv", ".txt", ".md", ".html", ".json"}:
            if b"\x00" in body[:4096]:
                return False
            if ext != ".html":
                return True
            payload = body
            for bom in cls._BOM_PREFIXES:
                if payload.startswith(bom):
                    payload = payload[len(bom):]
                    break
            for byte in payload:
                if byte in (9, 10, 13, 32):
                    continue
                return byte == ord("<")
            return False
        return False

    def _check_write_guard(self, dest: str) -> Optional[Dict[str, Any]]:
        context = resolve_tool_context(self, ctx=self._ctx, state=self._state, config_dict=self._get_config_dict())
        if not context.write_guard.enabled:
            return None
        try:
            check_write_path_allowed(
                dest,
                context.launch_root,
                context.write_guard.extra_paths,
                base_dir=context.launch_root,
                operation="write",
            )
        except WriteGuardViolation as exc:
            wg_mode = (context.write_guard.mode or "deny").strip().lower()
            if wg_mode == "allow":
                audit_policy_decision(
                    context,
                    tool_name=self.registered_tool_name(),
                    decision="executed",
                    reason_code="policy_allow_out_of_perimeter",
                    risk_flags=["out_of_perimeter"],
                )
                return None
            if wg_mode == "ask":
                from gc_core.security.confirm_port import request_confirm
                confirmed = request_confirm(
                    title="Защита записи",
                    question=f"Разрешить запись вне периметра проекта в {dest}?",
                    fields=[
                        ("Tool", self.registered_tool_name()),
                        ("Operation", "write"),
                        ("Path", dest),
                        ("Allowed roots", exc.error.get("allowed_write_roots") or []),
                    ],
                    ctx=context.ctx,
                    decision_key=f"write:{dest}",
                )
                if confirmed:
                    audit_policy_decision(
                        context,
                        tool_name=self.registered_tool_name(),
                        decision="executed",
                        reason_code="policy_ask_confirmed",
                        risk_flags=["out_of_perimeter"],
                    )
                    return None
                exc.error["type"] = "write_guard_cancelled_by_user"
                exc.error["message"] = (
                    f"User declined the write to '{dest}'. Do NOT retry the same "
                    "write, and do NOT silently write somewhere else — ask the user how to "
                    "proceed, or stop and report that it was declined."
                )
            audit_policy_decision(
                context,
                tool_name=self.registered_tool_name(),
                decision="blocked",
                reason_code="policy_deny" if wg_mode == "deny" else "policy_ask_cancelled",
                error_type=exc.error.get("type"),
            )
            return exc.error
        audit_policy_decision(
            context,
            tool_name=self.registered_tool_name(),
            decision="executed",
            reason_code="policy_allow",
        )
        return None

    async def _fetch_with_redirect_validation(
        self,
        session: "httpx.AsyncClient",
        start_url: str,
        headers: Dict[str, str],
        timeout: int,
        *,
        allowed_schemes: list[str],
        url_blacklist: list[str],
    ) -> "httpx.Response":
        current = start_url
        for _ in range(self._MAX_REDIRECTS + 1):
            response = await session.get(
                current, headers=headers, timeout=timeout, follow_redirects=False
            )
            status = getattr(response, "status_code", 0) or 0
            if not (300 <= status < 400):
                return response
            next_url = getattr(response, "headers", {}).get("Location") if hasattr(response, "headers") else None
            if not next_url:
                return response
            next_abs = urljoin(current, next_url)
            decision = validate_url_policy(next_abs, allowed_schemes, url_blacklist)
            if not decision.allowed:
                raise _RedirectBlocked(decision)
            current = next_abs
        raise _RedirectBlocked(
            UrlPolicyDecision(
                allowed=False,
                url=current,
                error_type="too_many_redirects",
                reason=f"Exceeded {self._MAX_REDIRECTS} redirects.",
            )
        )

    async def _http_get_bytes(self, url: str, max_bytes: int) -> tuple[bytes, Optional[Dict[str, Any]]]:
        context = resolve_tool_context(self, ctx=self._ctx, state=self._state, config_dict=self._get_config_dict())
        headers = {
            "User-Agent": self.default_user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
        }
        response = None
        attempts = 1 + self.max_retries
        # Клиент общий и переиспользуемый — закрывать его здесь нельзя.
        session = get_async_client(
            proxies=resolve_proxies(None, os.environ, url)
        )
        if True:
            for attempt in range(1, attempts + 1):
                try:
                    response = await self._fetch_with_redirect_validation(
                        session,
                        url,
                        headers,
                        self.timeout_seconds,
                        allowed_schemes=context.url_allowed_schemes,
                        url_blacklist=context.url_blacklist,
                    )
                    response.raise_for_status()
                    break
                except _RedirectBlocked as redirect_exc:
                    decision = redirect_exc.decision
                    return b"", {
                        "type": decision.error_type or "url_blocked_by_policy",
                        "reason": decision.reason,
                        "matched_rule": decision.matched_rule,
                        "blocked_on_redirect": True,
                    }
                except httpx.HTTPError as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    retryable = status is None or status in (429,) or status >= 500
                    if (not retryable) or attempt == attempts:
                        return b"", {
                            "type": "request_failed",
                            "message": str(exc),
                            "attempt": attempt,
                            "attempts_total": attempts,
                            "retryable": retryable,
                        }
                    await asyncio.sleep(self.retry_backoff_seconds * attempt)

        if response is None:
            return b"", {"type": "request_failed", "message": "Unknown request failure."}

        body = getattr(response, "content", b"") or b""
        if len(body) > max_bytes:
            return b"", {"type": "file_too_large", "message": f"Response body exceeds max_bytes ({max_bytes})."}
        return body, None

    def _atomic_write(self, dest: str, body: bytes) -> Optional[Dict[str, Any]]:
        tmp_path = f"{dest}.tmp"
        try:
            with open(tmp_path, "wb") as fd:
                fd.write(body)
            atomic_replace(tmp_path, dest)
            if hasattr(os, "chmod"):
                try:
                    os.chmod(dest, 0o644)
                except OSError:
                    pass
        except OSError as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return {"type": "write_failed", "message": str(exc)}
        return None

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        """Синхронный вход: прямые вызовы ``apply_tool`` и тесты.

        Форма из docs/async.md: реализация одна — ``acall``; здесь только
        способ её дождаться. Без этого ``apply_tool`` возвращал бы строку
        «tool is async», то есть три встроенных тула молча переставали
        работать через публичную синхронную точку SDK.
        """
        return run_coro_blocking(self.acall(*args, **kwargs))

    @enforce_url_policy(url_arg="url")
    async def acall(self, url: str, filename: str, **kwargs: Any) -> str:
        if not isinstance(url, str):
            return self._result(error={"type": "invalid_argument", "field": "url"})
        normalized_url = url.strip()
        if not normalized_url:
            return self._result(error={"type": "invalid_argument", "field": "url", "message": "URL cannot be empty."})
        if not normalized_url.startswith(("http://", "https://")):
            return self._result(
                error={"type": "invalid_argument", "field": "url", "message": "URL must start with http:// or https://."},
            )

        filename_result = self._validate_filename(filename)
        if isinstance(filename_result, dict):
            return self._result(error=filename_result)
        validated_filename, ext = filename_result

        context = resolve_tool_context(self, ctx=self._ctx, state=self._state, config_dict=self._get_config_dict())
        download_settings = self._resolve_download_settings(context.launch_root)
        if isinstance(download_settings, dict):
            return self._result(error=download_settings)
        download_root, max_bytes = download_settings

        dest = os.path.join(download_root, validated_filename)

        write_guard_error = self._check_write_guard(dest)
        if write_guard_error is not None:
            return self._result(path=dest, filename=validated_filename, error=write_guard_error)

        if os.path.exists(dest):
            return self._result(
                path=dest,
                filename=validated_filename,
                error={"type": "file_exists", "message": "Destination file already exists."},
            )

        body, request_error = await self._http_get_bytes(normalized_url, max_bytes)
        if request_error is not None:
            return self._result(path=dest, filename=validated_filename, error=request_error)

        if not self._validate_magic(body, ext):
            return self._result(
                path=dest,
                filename=validated_filename,
                error={"type": "format_blocked", "reason": "content does not match expected format"},
            )

        try:
            os.makedirs(download_root, exist_ok=True)
        except OSError as exc:
            return self._result(
                path=dest,
                filename=validated_filename,
                error={"type": "write_failed", "message": str(exc)},
            )

        # Инлайн: нативной асинхронной записи в Python нет (все «async file»
        # обёрнуты вокруг того же пула), а поток здесь не окупается — замерено
        # 6.1 мс лага loop против 2.7 мс на записи 32 МиБ при бюджете приёмки
        # 150 мс. Сеть выше остаётся асинхронной, поток убран только с записи.
        write_error = self._atomic_write(dest, body)
        if write_error is not None:
            return self._result(path=dest, filename=validated_filename, error=write_error)

        return self._result(
            ok=True,
            path=dest,
            filename=validated_filename,
            bytes_written=len(body),
        )

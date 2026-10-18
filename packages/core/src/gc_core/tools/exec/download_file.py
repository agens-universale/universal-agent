from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

import httpx

from gc_core.config_modules.security import DownloadConfig
from gc_core.constants.tool_names import DOWNLOAD_FILE_NAME
from gc_core.execution.ports import port
from gc_core.security.path_policy import (
    WRITE_GUARD_TITLE,
    WriteGuardViolation,
    audit_policy_decision,
    check_write_guard_path,
    normalize_path_for_policy,
    resolve_tool_context,
    settle_perimeter_answer,
    write_decision_key,
    write_mode_stage,
)
from gc_core.security.permission_mode import follows_cross_host_redirects
from gc_core.security.url_policy import enforce_url_policy
from gc_core.settings import settings_of
from gc_core.tools.exec.redirect_policy import (
    DEFAULT_MAX_REDIRECTS,
    stream_validating_redirects,
)
from gc_core.tools.exec.redirect_policy import (
    RedirectBlocked as _SharedRedirectBlocked,
)
from gc_core.tools.exec.redirect_policy import (
    TooManyRedirects as _TooManyRedirects,
)
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.async_http import get_async_client
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.http_proxy import resolve_proxies
from gc_core.utils.tool_report_paths import tool_report_abspath

_HTML_EXT = ".html"


#: Псевдоним общего исключения — см. ``tools/exec/redirect_policy``.
_RedirectBlocked = _SharedRedirectBlocked


class DownloadFile(AbstractSimpleTool):
    timeout_seconds = 20
    max_retries = 2
    retry_backoff_seconds = 1.0
    default_user_agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ALLOWED_EXTENSIONS = frozenset(
        {
            ".pdf",
            ".docx",
            ".xls",
            ".xlsx",
            ".pptx",
            ".csv",
            ".html",
            ".msg",
            ".txt",
            ".md",
            ".json",
        }
    )
    _BOM_PREFIXES = (
        b"\xef\xbb\xbf",
        b"\xff\xfe",
        b"\xfe\xff",
    )

    def __init__(
        self, ctx: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        if (
            ctx is not None
        ):  # keep the base-class LocalFsBackend fallback when ctx is absent
            self._ctx = ctx
        self._state = state

    @classmethod
    def tool_description(cls, settings: Any = None) -> Dict[str, Any]:
        extensions = ", ".join(
            sorted(ext.lstrip(".") for ext in cls.ALLOWED_EXTENSIONS)
        )
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
    def prompt_description(cls, settings: Any = None) -> str:
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

    def _result(
        self,
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
                "path": tool_report_abspath(path, workspace=self._workspace)
                if path
                else "",
                "filename": filename,
                "bytes_written": bytes_written,
                "error": error,
            },
            ensure_ascii=False,
        )

    def _download_config(self) -> DownloadConfig:
        """Секция ``security.download`` настроек тула (явные ``settings=``, иначе ctx)."""
        return settings_of(self._settings_source).security.download

    def _resolve_download_settings(
        self, launch_root: str
    ) -> tuple[str, int] | Dict[str, Any]:
        cfg = self._download_config()
        raw = os.path.expanduser(
            os.path.expandvars(str(cfg.download_dir or "downloads").strip())
        )
        if not raw:
            return {
                "type": "invalid_download_dir",
                "message": "download_dir must not be empty.",
            }
        try:
            download_root = str(normalize_path_for_policy(raw, launch_root))
        except ValueError:
            return {
                "type": "invalid_download_dir",
                "message": "download_dir resolved to an empty path.",
            }
        max_bytes = int(cfg.max_bytes or 52_428_800)
        return download_root, max_bytes

    @classmethod
    def _validate_filename(
        cls, filename: Any
    ) -> tuple[str, str] | Dict[str, Any]:
        if not filename or not isinstance(filename, str):
            return {"type": "invalid_argument", "field": "filename"}
        name = filename.strip()
        if not name or name in (".", ".."):
            return {
                "type": "invalid_filename",
                "message": "filename must be a non-empty basename.",
            }
        if os.path.basename(name) != name or "/" in name or "\\" in name:
            return {
                "type": "invalid_filename",
                "message": "filename must be a basename without path separators.",
            }
        if "\x00" in name or "\r" in name or "\n" in name:
            return {
                "type": "invalid_filename",
                "message": "filename contains unsupported characters (null byte or newline).",
            }
        if len(name) > 255:
            return {
                "type": "invalid_filename",
                "message": "filename is too long.",
            }
        ext = os.path.splitext(name)[1].lower()
        if ext not in cls.ALLOWED_EXTENSIONS:
            return {
                "type": "format_blocked",
                "reason": "extension not allowed",
            }
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
            return body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
        if ext in {".csv", ".txt", ".md", _HTML_EXT, ".json"}:
            if b"\x00" in body[:4096]:
                return False
            return ext != _HTML_EXT or cls._looks_like_html(body)
        return False

    @classmethod
    def _looks_like_html(cls, body: bytes) -> bool:
        """Первый значащий байт после BOM/пробелов — '<'."""
        payload = body
        for bom in cls._BOM_PREFIXES:
            if payload.startswith(bom):
                payload = payload[len(bom) :]
                break
        for byte in payload:
            if byte in (9, 10, 13, 32):
                continue
            return byte == ord("<")
        return False

    async def _acheck_write_guard(self, dest: str) -> Optional[Dict[str, Any]]:
        """Гард записи загрузчика; одна точка входа, драйвер — по рантайму.

        С портами рантайма (Temporal native: ``human_request`` / ``guard_probe``)
        тело крутится на loop вызывающего, а его запросы обслуживает
        ``_drive_async``. Локально — как в develop: гард целиком, вместе с
        ожиданием человека в режиме `ask` (в TUI до 600 с), уезжает в поток
        маршрутом гвардов (`dual_wrapper.off_loop`) — только там вызову
        ставится метка «брошен» (см. test_guard_confirm_off_loop).
        """
        from gc_core.security.dual_wrapper import (
            _drive_async,
            _drive_sync,
            off_loop,
        )

        flow = self._write_guard_flow(dest)
        if callable(port(self._ctx, "human_request")) or callable(
            getattr(self._ctx, "guard_probe", None)
        ):
            _, result = await _drive_async(flow)
        else:
            _, result = await off_loop(_drive_sync, flow)
        return result

    def _write_guard_flow(self, dest: str):
        context = resolve_tool_context(
            self,
            ctx=self._ctx,
            state=self._state,
        )
        # Стадия режима разрешений — до `write_guard.enabled`, как у декоратора:
        # выключенная защита режим не отменяет.
        call_now, error = yield from write_mode_stage(
            dest, context, self.registered_tool_name(), "write"
        )
        if error is not None:
            return error
        if call_now:
            return None
        if not context.write_guard.enabled:
            return None
        try:
            yield from check_write_guard_path(
                dest, context, self.registered_tool_name(), "write"
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
                from gc_core.security.confirm_port import confirmation_step

                confirmed = yield confirmation_step(
                    title=WRITE_GUARD_TITLE,
                    question=f"Разрешить запись вне периметра проекта в {dest}?",
                    fields=[
                        ("Tool", self.registered_tool_name()),
                        ("Operation", "write"),
                        ("Path", dest),
                        (
                            "Allowed roots",
                            exc.error.get("allowed_write_roots") or [],
                        ),
                    ],
                    ctx=context.ctx,
                    decision_key=write_decision_key(dest, context),
                )
                if settle_perimeter_answer(
                    context,
                    self.registered_tool_name(),
                    confirmed,
                    exc.error,
                    "write_guard_cancelled_by_user",
                    dest,
                ):
                    return None
            audit_policy_decision(
                context,
                tool_name=self.registered_tool_name(),
                decision="blocked",
                reason_code="policy_deny"
                if wg_mode == "deny"
                else "policy_ask_cancelled",
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

    def _stream_with_redirect_validation(
        self,
        session: httpx.AsyncClient,
        start_url: str,
        headers: Dict[str, str],
        timeout: int,
        *,
        allowed_schemes: list[str],
        url_blacklist: list[str],
        same_host_only: bool = False,
    ):
        """Общая потоковая петля из ``redirect_policy``.

        Тело НЕ читается: файл может быть гигабайтным, и решение «слишком
        большой» надо принимать до того, как он приедет в память.
        """
        return stream_validating_redirects(
            session,
            start_url,
            headers,
            timeout,
            allowed_schemes=allowed_schemes,
            url_blacklist=url_blacklist,
            max_redirects=DEFAULT_MAX_REDIRECTS,
            same_host_only=same_host_only,
        )

    async def _read_bounded(
        self, response: httpx.Response, max_bytes: int
    ) -> tuple[bytes, Optional[Dict[str, Any]]]:
        """Прочитать тело, оборвав его на ``max_bytes``.

        Сначала — заявленный ``Content-Length``: если сервер честно сказал, что
        файл больше лимита, качать нечего вовсе. Он не обязателен и может
        врать, поэтому дальше считаем фактически прочитанное и обрываем.
        """
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return b"", {
                "type": "file_too_large",
                "message": f"Response body exceeds max_bytes ({max_bytes}).",
                "declared_bytes": int(declared),
            }
        chunks: list = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return b"", {
                    "type": "file_too_large",
                    "message": f"Response body exceeds max_bytes ({max_bytes}).",
                }
            chunks.append(chunk)
        return b"".join(chunks), None

    async def _http_get_bytes(
        self, url: str, max_bytes: int
    ) -> tuple[bytes, Optional[Dict[str, Any]]]:
        context = resolve_tool_context(
            self,
            ctx=self._ctx,
            state=self._state,
        )
        # Переход на другой хост — по режиму сессии; решает слой безопасности.
        same_host_only = not follows_cross_host_redirects(context.ctx)
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
        for attempt in range(1, attempts + 1):
            try:
                async with self._stream_with_redirect_validation(
                    session,
                    url,
                    headers,
                    self.timeout_seconds,
                    allowed_schemes=context.url_allowed_schemes,
                    url_blacklist=context.url_blacklist,
                    same_host_only=same_host_only,
                ) as response:
                    if response.status_code >= 400:
                        # Тело ошибки читаем: без него у `raise_for_status`
                        # на стриминговом ответе нет текста.
                        await response.aread()
                    response.raise_for_status()
                    return await self._read_bounded(response, max_bytes)
            except _TooManyRedirects as loop_exc:
                # Петля редиректов — сетевая проблема, а не запрет политики.
                return b"", {
                    "type": "too_many_redirects",
                    "reason": str(loop_exc),
                    "limit": loop_exc.limit,
                }
            except _RedirectBlocked as redirect_exc:
                decision = redirect_exc.decision
                return b"", {
                    "type": decision.error_type or "url_blocked_by_policy",
                    "reason": decision.reason,
                    "matched_rule": decision.matched_rule,
                    "blocked_on_redirect": True,
                }
            except httpx.HTTPError as exc:
                status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
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

        return b"", {
            "type": "request_failed",
            "message": "Unknown request failure.",
        }

    def _atomic_write(
        self, dest: str, body: bytes
    ) -> Optional[Dict[str, Any]]:
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

    @enforce_url_policy(url_arg="url")
    async def acall(self, url: str, filename: str, **kwargs: Any) -> str:
        return await self._prepare_download(url, filename)

    @staticmethod
    def _download_url_error(url):
        if not isinstance(url, str):
            return {"type": "invalid_argument", "field": "url"}
        if not url.strip():
            return {
                "type": "invalid_argument",
                "field": "url",
                "message": "URL cannot be empty.",
            }
        if not url.strip().startswith(("http://", "https://")):
            return {
                "type": "invalid_argument",
                "field": "url",
                "message": "URL must start with http:// or https://.",
            }
        return None

    async def _prepare_download(
        self, url: str, filename: str, *, native_effect=None
    ) -> str:
        url_error = self._download_url_error(url)
        if url_error is not None:
            return self._result(error=url_error)
        normalized_url = url.strip()

        use_probe = callable(port(self._ctx, "guard_probe"))
        if not use_probe:
            filename_result = self._validate_filename(filename)
            if isinstance(filename_result, dict):
                return self._result(error=filename_result)
            validated_filename, ext = filename_result

        context = resolve_tool_context(
            self,
            ctx=self._ctx,
            state=self._state,
        )
        if use_probe:
            from gc_core.security.guard_probe import (
                GuardProbeRequest,
                probe_step,
            )

            facts = await probe_step(
                self._ctx,
                GuardProbeRequest(
                    "download_destination",
                    self.registered_tool_name(),
                    filename,
                    context.launch_root,
                ),
            ).make_coro()
            if "error" in facts.data:
                return self._result(error=facts.data["error"])
            validated_filename = facts.data["filename"]
            ext = facts.data["extension"]
            download_root = facts.data["download_root"]
            max_bytes = facts.data["max_bytes"]
            dest = facts.canonical_path
        else:
            download_settings = self._resolve_download_settings(
                context.launch_root
            )
            if isinstance(download_settings, dict):
                return self._result(error=download_settings)
            download_root, max_bytes = download_settings
            dest = os.path.join(download_root, validated_filename)
        write_guard_error = await self._acheck_write_guard(dest)
        if write_guard_error is not None:
            return self._result(
                path=dest, filename=validated_filename, error=write_guard_error
            )
        effect_data = dict(
            normalized_url=normalized_url,
            validated_filename=validated_filename,
            ext=ext,
            download_root=download_root,
            max_bytes=max_bytes,
            dest=dest,
        )
        if native_effect is not None:
            return await native_effect(effect_data)
        return await self._download_effect(**effect_data)

    async def _download_effect(
        self,
        *,
        normalized_url,
        validated_filename,
        ext,
        download_root,
        max_bytes,
        dest,
    ):
        if os.path.exists(dest):
            return self._result(
                path=dest,
                filename=validated_filename,
                error={
                    "type": "file_exists",
                    "message": "Destination file already exists.",
                },
            )

        body, request_error = await self._http_get_bytes(
            normalized_url, max_bytes
        )
        if request_error is not None:
            return self._result(
                path=dest, filename=validated_filename, error=request_error
            )

        if not self._validate_magic(body, ext):
            return self._result(
                path=dest,
                filename=validated_filename,
                error={
                    "type": "format_blocked",
                    "reason": "content does not match expected format",
                },
            )

        write_error = await self._publish(download_root, dest, body)
        if write_error is not None:
            return self._result(
                path=dest, filename=validated_filename, error=write_error
            )

        # Журнал правок: скачанный файл — правка агента в проекте, и без записи
        # его не было бы ни на экране изменений, ни в сводке хода, ни в откате
        # (пятый раунд ревью, находка 7). Импорт вложенный — цикл с thread_store,
        # как у остальных тулов.
        #
        # Вид правки всегда `create`: существующий путь тул отверг выше
        # (`file_exists`), перезаписи тут не бывает по построению. Поэтому и
        # `old_bytes=None` — «до» у пути не было, снимать нечего, а откату
        # созданного хватает собственного существования.
        from gc_core.agent_edits.recorder import record_edit

        await record_edit(
            self._ctx,
            abs_path=dest,
            old_bytes=None,
            new_bytes=body,
            op="create",
            call_id=self._call_id,
        )

        return self._result(
            ok=True,
            path=dest,
            filename=validated_filename,
            bytes_written=len(body),
        )

    async def _publish(self, root, dest, body):
        publisher = getattr(self._ctx, "download_publish", None)
        if publisher is not None:
            await publisher(dest, body)
            return None
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            return {"type": "write_failed", "message": str(exc)}
        return self._atomic_write(dest, body)

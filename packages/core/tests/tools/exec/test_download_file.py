import asyncio

import httpx
import json
import os

import pytest
import requests

from gc_core.tools.exec.download_file import DownloadFile


from gc_testkit.helpers.tool_ctx import with_local_ctx

PDF_BYTES = b"%PDF-1.4 fake pdf content"
DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 20
EXE_BYTES = b"MZ" + b"\x00" * 50


class _Resp:
    def __init__(self, *, content=b"", status_code=200, url="https://example.com/file", headers=None):
        self.content = content
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _SessionStub:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *_args, **_kwargs):
        return self._response


class _RedirectSessionStub:
    def __init__(self, responses):
        self._responses = list(responses)
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, *_args, **_kwargs):
        response = self._responses[self._index]
        self._index += 1
        return response


def _config(launch_root, *, download_dir="downloads", max_bytes=52_428_800, write_guard=None, url_policy=None):
    cfg = {
        "launch_root": str(launch_root),
        "download": {
            "download_dir": download_dir,
            "max_bytes": max_bytes,
        },
    }
    if write_guard is not None:
        cfg["write_guard"] = write_guard
    if url_policy is not None:
        cfg["url_policy"] = url_policy
    return cfg



def _resp(content=b"", content_type="application/octet-stream", status_code=200, headers=None):
    """Ответ сети как его отдаст httpx."""
    hdrs = {"Content-Type": content_type}
    if headers:
        hdrs.update(headers)
    return httpx.Response(status_code, content=content, headers=hdrs)


def _redirect_chain(responses):
    """Отдавать заготовленные ответы по порядку — для проверки редиректов."""
    queue = list(responses)

    def _handler(request):
        return queue.pop(0) if queue else httpx.Response(200, content=b"")

    return _handler


def _install(monkeypatch, handler):
    """Подменить транспорт httpx у DownloadFile.

    Патчим globals самой функции тула, а не модуль «по имени»: conftest
    каталога tests/tools подменяет gc_core.tools на время импорта, и класс
    оказывается замкнут на другой экземпляр модуля — подмена по sys.modules
    промахнулась бы, и тест ушёл бы в настоящую сеть.
    """
    import inspect

    from gc_core.utils import async_http

    def _dispatch(request):
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(request)
        return handler

    factory = lambda **kw: httpx.AsyncClient(  # noqa: E731
        transport=httpx.MockTransport(_dispatch)
    )
    monkeypatch.setattr(async_http, "get_async_client", factory)
    for target in (DownloadFile.__call__, DownloadFile._http_get_bytes,
                   DownloadFile._stream_with_redirect_validation):
        globals_dict = getattr(inspect.unwrap(target), "__globals__", None)
        if globals_dict is not None and "get_async_client" in globals_dict:
            monkeypatch.setitem(globals_dict, "get_async_client", factory)


def _call(tool, **kwargs) -> str:
    """Позвать тул синхронно — той же дорогой, что ``apply_tool``.

    У тула две точки входа: корутинная ``acall`` для цикла агента и
    синхронная ``__call__`` поверх неё (форма из docs/async.md). Гоняем
    синхронную: она проходит всю цепочку и заодно сторожит, что публичный
    синхронный вход не отвалится снова.
    """
    return tool(**kwargs)


def test_download_file_pdf_happy_path(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    dest = launch_root / "downloads" / "report.pdf"
    assert result["ok"] is True
    assert result["tool"] == "download_file"
    assert result["filename"] == "report.pdf"
    assert result["bytes_written"] == len(PDF_BYTES)
    assert dest.exists()
    assert dest.read_bytes() == PDF_BYTES


def test_download_file_docx_zip_header(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=DOCX_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/doc.docx",
            filename="doc.docx",
        ))

    assert result["ok"] is True
    assert (launch_root / "downloads" / "doc.docx").read_bytes() == DOCX_BYTES


@pytest.mark.parametrize(
    ("filename", "body"),
    [
        ("notes.txt", b"hello plain text\n"),
        ("readme.md", b"# Title\n\nSome markdown.\n"),
        ("data.json", b'{"ok": true, "n": 1}\n'),
    ],
)
def test_download_file_plain_text_extensions(tmp_path, monkeypatch, filename, body):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=body))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url=f"https://example.com/{filename}",
            filename=filename,
        ))

    dest = launch_root / "downloads" / filename
    assert result["ok"] is True
    assert result["filename"] == filename
    assert dest.read_bytes() == body


def test_download_file_plain_text_rejects_null_bytes(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=b"text\x00binary"))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/notes.txt",
            filename="notes.txt",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "format_blocked"
    assert not (launch_root / "downloads" / "notes.txt").exists()


def test_download_file_invalid_filename_path_traversal(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/x.pdf",
            filename="../../x.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_filename"


@pytest.mark.parametrize(
    ("filename", "body"),
    [
        ("my report (final).pdf", PDF_BYTES),
        ("отчёт за июль.pdf", PDF_BYTES),
        ("Q1 (2026) — summary.xlsx", DOCX_BYTES),
        ("file with spaces.txt", b"hello plain text\n"),
    ],
)
def test_download_file_accepts_spaces_parens_unicode(tmp_path, monkeypatch, filename, body):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=body))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/x.pdf",
            filename=filename,
        ))

    assert result["ok"] is True
    assert result["filename"] == filename
    assert (launch_root / "downloads" / filename).read_bytes() == body


@pytest.mark.parametrize("bad_char", ["\x00", "\r", "\n"])
def test_download_file_rejects_null_and_newlines(tmp_path, bad_char):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/x.pdf",
            filename=f"bad{bad_char}name.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_filename"


def test_download_file_format_blocked_extension(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/malware.exe",
            filename="malware.exe",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "format_blocked"


def test_download_file_format_blocked_magic_mismatch(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=EXE_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/fake.pdf",
            filename="fake.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "format_blocked"
    assert not (launch_root / "downloads" / "fake.pdf").exists()


def test_download_file_exists(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    existing = launch_root / "downloads"
    existing.mkdir()
    target = existing / "report.pdf"
    target.write_bytes(b"existing")

    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "file_exists"
    assert target.read_bytes() == b"existing"


def test_download_file_too_large(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, max_bytes=10)),
            url="https://example.com/big.pdf",
            filename="big.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "file_too_large"
    assert not (launch_root / "downloads" / "big.pdf").exists()


def test_download_file_redirect_blocked_by_policy(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    redirect = _resp(
        content=b"",
        status_code=302,
        headers={"Location": "http://169.254.169.254/latest/meta-data"},
    )
    _install(monkeypatch, _redirect_chain([redirect]))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                url_policy={
                    "enabled": True,
                    "allowed_schemes": ["http", "https"],
                    "url_blacklist": ["169.254.*"],
                },
            )
        ),
            url="https://example.com/start",
            filename="meta.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy"
    assert result["error"].get("blocked_on_redirect") is True


def test_download_file_relative_download_dir(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, download_dir="incoming")),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    assert result["ok"] is True
    assert (launch_root / "incoming" / "report.pdf").exists()


def test_download_file_absolute_download_dir_in_extra_paths(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    scratch = tmp_path / "agent-scratch" / "downloads"
    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                download_dir=str(scratch),
                write_guard={"enabled": True, "mode": "deny", "extra_paths": [str(scratch.parent)]},
            )
        ),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    assert result["ok"] is True
    assert (scratch / "report.pdf").exists()


def test_download_file_absolute_download_dir_outside_perimeter(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside-downloads"
    _install(monkeypatch, _resp(content=PDF_BYTES))

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                download_dir=str(outside),
                write_guard={"enabled": True, "mode": "deny", "extra_paths": []},
            )
        ),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "write_guard_violation"
    assert not (outside / "report.pdf").exists()


def test_download_file_descriptions_are_tool_agnostic():
    desc = DownloadFile.tool_description()["description"]
    prompt = DownloadFile.prompt_description()
    combined = f"{desc}\n{prompt}".lower()
    forbidden = (
        "office__",
        "exec_",
        "fs_",
        "read_file",
        "fetch_url",
        "write_to_file",
    )
    for token in forbidden:
        assert token not in combined


def test_download_file_atomic_write_replace_failure(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    _install(monkeypatch, _resp(content=PDF_BYTES))

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    result = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root)),
            url="https://example.com/report.pdf",
            filename="report.pdf",
        ))

    assert result["ok"] is False
    assert result["error"]["type"] == "write_failed"
    dest = launch_root / "downloads" / "report.pdf"
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


# --- M11: тело не приезжает в память целиком --------------------------------


def test_oversized_file_is_rejected_by_content_length_without_downloading(monkeypatch, tmp_path):
    """Сервер честно сказал размер — качать нечего вовсе.

    Раньше файл скачивался в память ЦЕЛИКОМ и только потом сравнивался с
    `max_bytes`: пять гигабайт при лимите в пятьдесят мегабайт полностью
    приезжали в RAM и лишь затем отвергались. Докстрока при этом обещала
    «пишет тело инлайн» — писать к тому моменту было уже нечего.
    """
    launch_root = tmp_path / "root"
    launch_root.mkdir()

    def _handler(request):
        # Тело маленькое, но заявленный размер заведомо больше лимита.
        return httpx.Response(
            200,
            content=b"x" * 64,
            headers={"Content-Length": "999999999",
                     "Content-Type": "application/octet-stream"},
        )

    _install(monkeypatch, _handler)

    out = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, max_bytes=1024)),
        url="https://example.com/big.pdf",
        filename="big.pdf",
    ))

    assert out["ok"] is False, out
    assert out["error"]["type"] == "file_too_large", out
    assert out["error"].get("declared_bytes") == 999999999, out


def test_lying_content_length_is_caught_while_streaming(monkeypatch, tmp_path):
    """Заявленный размер не обязателен и может врать — считаем фактический."""
    launch_root = tmp_path / "root"
    launch_root.mkdir()

    def _handler(request):
        # Заголовка нет вовсе: единственная защита — счёт прочитанного.
        return httpx.Response(200, content=b"y" * 20_000,
                              headers={"Content-Type": "application/octet-stream"})

    _install(monkeypatch, _handler)

    out = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, max_bytes=1024)),
        url="https://example.com/lying.pdf",
        filename="lying.pdf",
    ))

    assert out["ok"] is False, out
    assert out["error"]["type"] == "file_too_large", out


def test_small_file_still_downloads(monkeypatch, tmp_path):
    """Обратная половина: обычный файл по-прежнему скачивается целиком."""
    launch_root = tmp_path / "root"
    launch_root.mkdir()

    # Настоящая сигнатура: у `.pdf` содержимое проверяется по magic-байтам,
    # и произвольные байты тул отвергнет ещё до записи.
    body = PDF_BYTES

    def _handler(request):
        return httpx.Response(200, content=body,
                              headers={"Content-Type": "application/pdf"})

    _install(monkeypatch, _handler)

    out = json.loads(_call(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, max_bytes=4096)),
        url="https://example.com/ok.pdf",
        filename="ok.pdf",
    ))

    assert out["ok"] is True, out
    assert (launch_root / "downloads" / "ok.pdf").read_bytes() == body

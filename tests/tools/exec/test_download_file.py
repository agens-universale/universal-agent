import json
import os

import pytest
import requests

from tools.exec.download_file import DownloadFile


from tests.helpers.tool_ctx import with_local_ctx

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


def test_download_file_pdf_happy_path(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES, url="https://example.com/report.pdf")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

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
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=DOCX_BYTES, url="https://example.com/doc.docx")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/doc.docx",
            filename="doc.docx",
        )
    )

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
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=body, url=f"https://example.com/{filename}")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url=f"https://example.com/{filename}",
            filename=filename,
        )
    )

    dest = launch_root / "downloads" / filename
    assert result["ok"] is True
    assert result["filename"] == filename
    assert dest.read_bytes() == body


def test_download_file_plain_text_rejects_null_bytes(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=b"text\x00binary", url="https://example.com/notes.txt")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/notes.txt",
            filename="notes.txt",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "format_blocked"
    assert not (launch_root / "downloads" / "notes.txt").exists()


def test_download_file_invalid_filename_path_traversal(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/x.pdf",
            filename="../../x.pdf",
        )
    )

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
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=body, url=f"https://example.com/{filename}")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/x.pdf",
            filename=filename,
        )
    )

    assert result["ok"] is True
    assert result["filename"] == filename
    assert (launch_root / "downloads" / filename).read_bytes() == body


@pytest.mark.parametrize("bad_char", ["\x00", "\r", "\n"])
def test_download_file_rejects_null_and_newlines(tmp_path, bad_char):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/x.pdf",
            filename=f"bad{bad_char}name.pdf",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "invalid_filename"


def test_download_file_format_blocked_extension(tmp_path):
    launch_root = tmp_path / "project"
    launch_root.mkdir()

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/malware.exe",
            filename="malware.exe",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "format_blocked"


def test_download_file_format_blocked_magic_mismatch(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=EXE_BYTES, url="https://example.com/fake.pdf")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/fake.pdf",
            filename="fake.pdf",
        )
    )

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

    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES)),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "file_exists"
    assert target.read_bytes() == b"existing"


def test_download_file_too_large(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES, url="https://example.com/big.pdf")),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, max_bytes=10))(
            url="https://example.com/big.pdf",
            filename="big.pdf",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "file_too_large"
    assert not (launch_root / "downloads" / "big.pdf").exists()


def test_download_file_redirect_blocked_by_policy(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    redirect = _Resp(
        content=b"",
        status_code=302,
        url="https://example.com/start",
        headers={"Location": "http://169.254.169.254/latest/meta-data"},
    )
    monkeypatch.setattr(requests, "Session", lambda: _RedirectSessionStub([redirect]))

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                url_policy={
                    "enabled": True,
                    "allowed_schemes": ["http", "https"],
                    "url_blacklist": ["169.254.*"],
                },
            )
        )(
            url="https://example.com/start",
            filename="meta.pdf",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy"
    assert result["error"].get("blocked_on_redirect") is True


def test_download_file_relative_download_dir(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES)),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root, download_dir="incoming"))(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

    assert result["ok"] is True
    assert (launch_root / "incoming" / "report.pdf").exists()


def test_download_file_absolute_download_dir_in_extra_paths(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    scratch = tmp_path / "agent-scratch" / "downloads"
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES)),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                download_dir=str(scratch),
                write_guard={"enabled": True, "mode": "deny", "extra_paths": [str(scratch.parent)]},
            )
        )(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

    assert result["ok"] is True
    assert (scratch / "report.pdf").exists()


def test_download_file_absolute_download_dir_outside_perimeter(tmp_path, monkeypatch):
    launch_root = tmp_path / "project"
    launch_root.mkdir()
    outside = tmp_path / "outside-downloads"
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES)),
    )

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(
                launch_root,
                download_dir=str(outside),
                write_guard={"enabled": True, "mode": "deny", "extra_paths": []},
            )
        )(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

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
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _SessionStub(_Resp(content=PDF_BYTES)),
    )

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    result = json.loads(
        with_local_ctx(DownloadFile, config_dict=_config(launch_root))(
            url="https://example.com/report.pdf",
            filename="report.pdf",
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "write_failed"
    dest = launch_root / "downloads" / "report.pdf"
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()

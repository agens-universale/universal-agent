"""File transfer refusals stay local to a file; control failures propagate."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.runtime_probe import RuntimeUnavailable
from gc_core.tools.fs.file_info import FileInfo
from gc_core.tools.fs.find_in_files import FindInFiles
from gc_core.tools.fs.read_file import ReadFile
from gc_core.user_stop import UserStopInterrupt
from gc_core.utils.file_text import BINARY_SAMPLE_BYTES
from gc_testkit.helpers.native_search import requires_native_search
from gc_testkit.helpers.tool_ctx import local_ctx


def _transfer_message(source, target):
    return (
        f"Engine cp failed ('eb67c6337a5d:{source}' -> '{target}'): "
        f'invalid symlink "{target}" -> "../target.txt"'
    )


class _DownloadEnvironment:
    """Execute the probe locally; fail only the provider's download boundary."""

    def __init__(self, bad_path=None, failure=None):
        self.bad_path = bad_path
        self.failure = failure
        self.downloads = []

    async def exec(self, command, **kwargs):
        return await LocalFsBackend().aexec(command)

    async def download_file(self, source, target):
        self.downloads.append(source)
        if self.failure is not None:
            raise self.failure
        if source == self.bad_path:
            raise RuntimeError(_transfer_message(source, target))
        target.write_bytes(Path(source).read_bytes())


def _backend(environment):
    return HarborBackend(environment, asyncio.get_running_loop())


def _tool(tool_class, root, backend, extra=None):
    config = {"launch_root": str(root)}
    if extra is not None:
        config["fs_tools"] = {"find_in_files": extra}
    return tool_class(ctx=local_ctx(config, os_backend=backend))


def _pair(tmp_path, *, bad_first=False, large=False, token="TOKEN"):
    root = tmp_path / "search"
    root.mkdir()
    good = root / ("z-good.txt" if bad_first else "a-good.txt")
    bad = root / ("a-link.txt" if bad_first else "z-link.txt")
    target = tmp_path / "target.txt"
    target.write_text(
        "before\n"
        + "я" * 700
        + token
        + "z" * 700
        + f"\n{token} again\nafter\n"
        + ("padding\n" * BINARY_SAMPLE_BYTES if large else ""),
        encoding="utf-8",
    )
    for path in (bad, good) if bad_first else (good, bad):
        if path == bad:
            path.symlink_to("../target.txt")
        else:
            path.write_text(f"good before\n{token} good\ngood after\n")
    assert bad.is_symlink() and bad.exists()
    assert bad.read_bytes() == target.read_bytes()
    return root, good, bad


def _local_with_harbor_download(environment, monkeypatch, *, native):
    """Keep real host search/stat/head, crossing Harbor only for body reads."""
    backend = LocalFsBackend()
    harbor = _backend(environment)
    monkeypatch.setattr(backend, "read_bytes", harbor.read_bytes)
    if not native:
        backend._cap_overrides = {"has_find": False, "has_grep": False}
    return backend


async def test_download_normalizes_only_confirmed_symlink_transfer_refusal():
    environment = _DownloadEnvironment(bad_path="/app/link.txt")
    with pytest.raises(OSError, match="invalid symlink") as caught:
        await _backend(environment).read_bytes("/app/link.txt")
    assert type(caught.value.__cause__) is RuntimeError
    assert str(caught.value) == str(caught.value.__cause__)
    assert environment.downloads == ["/app/link.txt"]


@pytest.mark.parametrize(
    "requested, reported, wrong_target, normalizes",
    [
        ("/app/cache:/link.txt", "/app/cache:/link.txt", False, True),
        ("/app/link.txt", "/other:/app/link.txt", False, False),
        ("/app/link.txt", "/app/other.txt", False, False),
        ("/app/link.txt", "/app/link.txt", True, False),
    ],
)
async def test_download_requires_exact_source_and_destination(
    monkeypatch, requested, reported, wrong_target, normalizes
):
    environment = _DownloadEnvironment()
    failures = []

    async def refuse_download(_source, target):
        reported_target = "/tmp/different-download" if wrong_target else target
        failure = RuntimeError(_transfer_message(reported, reported_target))
        failures.append(failure)
        raise failure

    monkeypatch.setattr(environment, "download_file", refuse_download)
    expected_type = OSError if normalizes else RuntimeError
    with pytest.raises(expected_type) as caught:
        await _backend(environment).read_bytes(requested)
    if normalizes:
        assert caught.value.__cause__ is failures[0]
    else:
        assert caught.value is failures[0]


@pytest.mark.parametrize(
    "failure",
    [
        OSError("original read failure"),
        RuntimeError(
            "Engine cp failed ('cid:/app/link' -> '/tmp/out'): denied"
        ),
        RuntimeError("Cannot connect to the Docker daemon"),
        RuntimeError("Command timed out after 30 seconds"),
        RuntimeError("unknown download failure"),
        RuntimeError("Engine cp failed: invalid symlink"),
        ValueError(_transfer_message("/app/link.txt", "/tmp/out")),
        TimeoutError("download timed out"),
        asyncio.CancelledError("cancelled"),
        RuntimeUnavailable(stderr="container unavailable"),
        UserStopInterrupt(kind="exec"),
    ],
)
async def test_download_preserves_other_exception_identity(failure):
    environment = _DownloadEnvironment(failure=failure)
    with pytest.raises(type(failure)) as caught:
        await _backend(environment).read_bytes("/app/link.txt")
    assert caught.value is failure


async def test_probe_failure_does_not_enter_download_conversion(monkeypatch):
    environment = _DownloadEnvironment()
    backend = _backend(environment)
    failure = RuntimeError(_transfer_message("/app/link.txt", "/tmp/out"))

    async def fail_probe(_probe):
        raise failure

    monkeypatch.setattr(backend._runtime_probe, "ensure", fail_probe)
    with pytest.raises(RuntimeError) as caught:
        await backend.read_bytes("/app/link.txt")
    assert caught.value is failure
    assert environment.downloads == []


@requires_native_search
@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("locator_available", [False, True])
@pytest.mark.parametrize("token", ["TOKEN", "École"])
async def test_native_keeps_hits_and_good_context_after_download_refusal(
    tmp_path, monkeypatch, bad_first, locator_available, token
):
    root, good, bad = _pair(tmp_path, bad_first=bad_first, token=token)
    environment = _DownloadEnvironment(bad_path=str(bad))
    backend = _local_with_harbor_download(
        environment, monkeypatch, native=True
    )
    await backend.awarm_capabilities()
    original_argv = backend.aexec_argv
    locators = []

    async def run_argv(argv, **kwargs):
        if "-b" in argv and "-o" in argv:
            locators.append(argv)
            if not locator_available:
                raise OSError("controlled offset lookup failure")
        return await original_argv(argv, **kwargs)

    monkeypatch.setattr(backend, "aexec_argv", run_argv)
    tool = _tool(FindInFiles, root, backend)
    data = json.loads(
        await tool.acall(
            path=str(root),
            pattern=token.lower() + "|OTHER",
            query_mode="python-re",
            case_insensitive=True,
        )
    )

    assert data["ok"] is True and data["search_backend"] == "grep"
    assert data["count"] == 3
    bad_hits = [row for row in data["matches"] if row["file"] == str(bad)]
    assert len(bad_hits) == 2
    assert all(row["context_omitted"] == "read_error" for row in bad_hits)
    assert all("context" not in row for row in bad_hits)
    good_hit = next(row for row in data["matches"] if row["file"] == str(good))
    assert "good before" in good_hit["context"]
    assert Counter(environment.downloads) == {str(good): 1, str(bad): 1}
    assert environment.downloads[0] == str(bad if bad_first else good)
    assert locators
    if locator_available:
        assert all(token in row["line_content"] for row in bad_hits)
        assert data["scan_complete"] is True
        assert data["more_matches_may_exist"] is False
    else:
        assert data["stop_reason"] == "native_match_offset_unavailable"
        assert data["scan_complete"] is False
        assert data["more_matches_may_exist"] is True


@requires_native_search
@pytest.mark.parametrize(
    "kind", ["cancel", "unavailable", "stop", "unexpected"]
)
async def test_native_download_controls_and_unexpected_errors_propagate(
    tmp_path, monkeypatch, kind
):
    root, _, _ = _pair(tmp_path)
    failures = {
        "cancel": asyncio.CancelledError("cancelled"),
        "unavailable": RuntimeUnavailable(stderr="unavailable"),
        "stop": UserStopInterrupt(kind="exec"),
        "unexpected": RuntimeError("unknown download failure"),
    }
    failure = failures[kind]
    backend = _local_with_harbor_download(
        _DownloadEnvironment(failure=failure), monkeypatch, native=True
    )
    with pytest.raises(type(failure)) as caught:
        await _tool(FindInFiles, root, backend).acall(
            path=str(root), pattern="TOKEN|OTHER", query_mode="python-re"
        )
    assert caught.value is failure


@pytest.mark.parametrize("read_phase", ["head", "body"])
async def test_python_failed_mandatory_read_keeps_prior_hits_and_reports_gap(
    tmp_path, monkeypatch, read_phase
):
    root, good, bad = _pair(tmp_path, large=True)
    environment = _DownloadEnvironment(bad_path=str(bad))
    backend = _local_with_harbor_download(
        environment, monkeypatch, native=False
    )
    original_head = backend.read_head_bytes
    heads = []

    async def read_head(path, size):
        heads.append(path)
        if path == str(bad) and read_phase == "head":
            raise OSError("controlled head read failure")
        return await original_head(path, size)

    monkeypatch.setattr(backend, "read_head_bytes", read_head)
    data = json.loads(
        await _tool(FindInFiles, root, backend).acall(
            path=str(root), pattern="TOKEN", query_mode="text"
        )
    )

    assert data["ok"] is True and data["search_backend"] == "python"
    assert data["count"] == 1
    assert data["matches"][0]["file"] == str(good)
    assert "context_omitted" not in data["matches"][0]
    assert heads == [str(good), str(bad)]
    assert environment.downloads == (
        [str(bad)] if read_phase == "body" else []
    )
    assert data["skipped_read_errors"] == 1
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True
    assert "could not be read" in data["guidance"]
    assert "Do not assume the pattern is absent" in data["guidance"]
    assert "stop_reason" not in data


async def test_python_small_files_use_head_without_download(
    tmp_path, monkeypatch
):
    root, _, bad = _pair(tmp_path)
    environment = _DownloadEnvironment(bad_path=str(bad))
    backend = _local_with_harbor_download(
        environment, monkeypatch, native=False
    )
    data = json.loads(
        await _tool(FindInFiles, root, backend).acall(
            path=str(root), pattern="TOKEN", query_mode="text"
        )
    )
    assert data["count"] == 3 and data["scan_complete"] is True
    assert environment.downloads == []
    assert "skipped_read_errors" not in data


@pytest.mark.parametrize(
    "limit, stop_reason",
    [(1, "match_limit"), (1000, "requested_limit_capped")],
)
async def test_python_read_errors_preserve_existing_limit_stop_reason(
    tmp_path, monkeypatch, limit, stop_reason
):
    root, _, bad = _pair(tmp_path, bad_first=True, large=True)
    environment = _DownloadEnvironment(bad_path=str(bad))
    backend = _local_with_harbor_download(
        environment, monkeypatch, native=False
    )
    data = json.loads(
        await _tool(FindInFiles, root, backend).acall(
            path=str(root), pattern="TOKEN", query_mode="text", limit=limit
        )
    )
    assert environment.downloads == [str(bad)]
    assert data["count"] == 1
    assert data["skipped_read_errors"] == 1
    assert data["stop_reason"] == stop_reason
    assert data["scan_complete"] is False
    assert data["more_matches_may_exist"] is True
    assert "could not be read" in data["guidance"]


@pytest.mark.parametrize("phase", ["head", "body", "walk"])
@pytest.mark.parametrize(
    "kind", ["cancel", "unavailable", "stop", "unexpected"]
)
async def test_python_controls_propagate_and_unexpected_failures_stay_errors(
    tmp_path, monkeypatch, phase, kind
):
    root, _, _ = _pair(tmp_path, large=True)
    failures = {
        "cancel": asyncio.CancelledError("cancelled"),
        "unavailable": RuntimeUnavailable(stderr="unavailable"),
        "stop": UserStopInterrupt(kind="exec"),
        "unexpected": ValueError("unexpected failure"),
    }
    failure = failures[kind]
    backend = _local_with_harbor_download(
        _DownloadEnvironment(failure=failure), monkeypatch, native=False
    )

    async def fail_head(_path, _size):
        raise failure

    async def fail_walk(*_args, **_kwargs):
        raise failure
        yield  # Preserve the backend's async-iterator contract.

    if phase == "head":
        monkeypatch.setattr(backend, "read_head_bytes", fail_head)
    elif phase == "walk":
        monkeypatch.setattr(backend, "iter_walk_entries", fail_walk)
    tool = _tool(FindInFiles, root, backend)
    call = tool.acall(path=str(root), pattern="TOKEN", query_mode="text")
    if kind != "unexpected":
        with pytest.raises(type(failure)) as caught:
            await call
        assert caught.value is failure
    else:
        data = json.loads(await call)
        assert data["ok"] is False
        assert data["error"]["type"] == "unexpected_error"
        assert data["matches"] == []


@pytest.mark.parametrize(
    "tool_class, kwargs",
    [(ReadFile, {}), (FileInfo, {"include_line_count": True})],
)
async def test_read_tools_report_structured_failure_from_download_boundary(
    tmp_path, monkeypatch, tool_class, kwargs
):
    root, good, _ = _pair(tmp_path)
    environment = _DownloadEnvironment(bad_path=str(good))
    backend = _local_with_harbor_download(
        environment, monkeypatch, native=False
    )
    data = json.loads(
        await _tool(tool_class, root, backend).acall(path=str(good), **kwargs)
    )
    assert data["ok"] is False
    assert data["error"]["type"] == "read_failed"
    assert environment.downloads == [str(good)]

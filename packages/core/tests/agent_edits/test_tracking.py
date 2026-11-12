"""TrackingFsBackend: единственная роль обёртки — шелл-делта вокруг команд.

Текстовые правки пишет рекордер прямо из fs-тулов, поэтому обёртка про них
ничего не знает: ни снимков, ни журнала «тронутых путей» она больше не ведёт.
Остальная поверхность бэкенда — чистое делегирование внутрь.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core.agent_edits.journal import EditsJournal
from gc_core.agent_edits.tracking import (
    TrackingFsBackend,
    wrap_local_for_tracking,
)
from gc_core.backends.local import LocalFsBackend
from gc_core.config_modules import env_vars
from gc_core.execution_workspace import build_execution_workspace
from gc_core.thread_store import edits_journal_path
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import local_ctx
from gc_testkit.settings import fake_ctx


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def launch_root(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    monkeypatch.chdir(root)
    return root, threads


def _cfg(launch_root: Path) -> dict:
    return {
        "launch_root": str(launch_root),
        "security.write_guard": {
            "enabled": False,
            "mode": "deny",
            "extra_paths": [],
        },
        "agent_edits": {},
    }


def _tool_ctx(launch_root: Path, threads: Path, *, thread_id: str = "t1"):
    cfg = _cfg(launch_root)
    ctx = local_ctx(cfg)
    ctx.is_tui = True
    ctx.threads_dir = str(threads)
    ctx.system_id = "tui"
    ctx.thread_id = thread_id
    ctx.turn_id = "turn-1"
    wrapped = wrap_local_for_tracking(ctx.os_backend, ctx)
    ctx.os_backend = wrapped
    ctx.workspace = build_execution_workspace(wrapped, ctx)
    return ctx


def _records(threads: Path, *, thread_id: str = "t1"):
    return EditsJournal(
        edits_journal_path(str(threads), "tui", thread_id)
    ).read_all()


async def test_tracking_backend_no_longer_writes_touched_journal(launch_root):
    """Правка тулом: журнала «тронутых путей» и старого кэша больше нет."""
    root, threads = launch_root
    target = root / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    ctx = _tool_ctx(root, threads)

    res = json.loads(
        await WriteToFile(ctx=ctx, call_id="c1").acall(
            path=str(target), content="changed\n"
        )
    )
    assert res["ok"] is True
    assert not (threads / "touched_tui_t1.json").exists()
    assert not (root / ".gc" / "diffs").exists()
    # Правка при этом записана — вторым каналом, из самого тула.
    assert [(r.path, r.call_id) for r in _records(threads)] == [
        ("src/foo.py", "c1")
    ]


async def test_tracking_backend_still_records_shell_delta(launch_root):
    """Правка командой видна только обёртке — её канал обязан остаться живым."""
    root, threads = launch_root
    if not _has_git():
        pytest.skip("git недоступен")
    _git(root, "init", "-b", "main")
    ctx = _tool_ctx(root, threads)
    assert isinstance(ctx.os_backend, TrackingFsBackend)

    await ctx.os_backend.arun_python(
        "open('shell_out.txt','wb').write(b'alpha\\n')", cwd=str(root)
    )

    assert (root / "shell_out.txt").read_bytes() == b"alpha\n"
    recs = _records(threads)
    assert [(r.path, r.op, r.call_id) for r in recs] == [
        ("shell_out.txt", "create", None)
    ]


async def test_edits_of_a_raising_command_reach_the_journal(
    launch_root, monkeypatch
):
    """Команда успела написать файл и упала: отмена хода, обрыв транспорта.

    Правка уже на диске — журнал обязан её увидеть, иначе прерванная команда
    уносит свои правки и из счётчиков хода, и с экрана.
    """
    root, threads = launch_root
    if not _has_git():
        pytest.skip("git недоступен")
    _git(root, "init", "-b", "main")
    ctx = _tool_ctx(root, threads)

    async def _writes_then_raises(code, **kwargs):
        (root / "half_done.txt").write_bytes(b"alpha\n")
        raise KeyboardInterrupt("ход отменён")

    monkeypatch.setattr(
        ctx.os_backend._inner, "arun_python", _writes_then_raises
    )

    with pytest.raises(KeyboardInterrupt):
        await ctx.os_backend.arun_python("что угодно", cwd=str(root))

    assert (root / "half_done.txt").is_file()
    assert [(r.path, r.op) for r in _records(threads)] == [
        ("half_done.txt", "create")
    ]


#: Единственное, чего у обёртки нет намеренно: синхронные точки входа запуска
#: команды. На порту они остались ради ЧУЖИХ синхронных бэкендов
#: (`FsBackend.exec` и соседи — не абстрактные, отказывают
#: `SyncExecNotSupportedError`), а своим держать sync- и a-форму рядом
#: запрещает страж `tests/test_sync_async_pairs.py`. Список закрытый: любое
#: другое имя, пропавшее у обёртки, — потеря.
_SYNC_EXEC_ENTRY_POINTS = {"exec", "exec_raw", "exec_argv", "run_python"}


def test_tracking_backend_delegates_full_backend_surface():
    """Поверхность LocalFsBackend не должна усохнуть при переезде обёртки.

    Проверка СТАТИЧЕСКАЯ, по классам, как у соседа ниже: `hasattr` на
    экземпляре ЧИТАЕТ атрибут, а `has_find`/`has_grep`/`grep_*` у бэкенда —
    пробы, и чтение до `awarm_capabilities()` кончается
    `CapabilitiesNotWarmedError` (не `AttributeError`), то есть страж падал бы
    на собственной проверке вместо ответа про поверхность.
    """
    import inspect

    missing = {
        name
        for name in dir(LocalFsBackend)
        if not name.startswith("_")
        and name not in _SYNC_EXEC_ENTRY_POINTS
        and inspect.getattr_static(TrackingFsBackend, name, None) is None
    }
    assert missing == set()


def test_tracking_backend_has_no_sync_exec_forms():
    """Синхронных форм запуска у обёртки нет — и это часть контракта.

    Они не «забыты»: рабочий синхронный `exec` здесь означал цепочку
    `SyncExecAdapter` → поток → `run_coro_blocking(aexec)`, то есть ВТОРОЙ
    event loop в потоке пула. Кооперативный стоп до подпроцесса не доходил, а
    `asyncio.run` хода на выходе ждал `shutdown_default_executor`: после Esc
    поток жил до таймаута команды.
    """
    import inspect

    present = {
        name
        for name in _SYNC_EXEC_ENTRY_POINTS
        if inspect.getattr_static(TrackingFsBackend, name, None) is not None
    }
    assert present == set()


def test_tracking_backend_overrides_keep_the_inner_signature():
    """Обёртка не должна СУЖАТЬ сигнатуры методов, которые переопределяет.

    Проверки по именам мало, и это доказано дважды: `capture_max_chars` исчез
    из трёх методов обёртки при переезде подсистемы и прожил так до следующей
    синхронизации. Отказ молчаливый — зовущий (native-поиск `find`/`grep`,
    MCP-carrier) ловит TypeError своим общим `except` и тихо уходит на
    медленный обходной путь, а страж по именам всё это время зелёный.

    Проверка СТАТИЧЕСКАЯ, по классам: ни экземпляров, ни чтения атрибутов.
    `has_find`/`has_grep` у бэкенда — ленивые пробы, и обычный `getattr` по
    ним запускает настоящие `find`/`grep` в подпроцессе на каждом прогоне.
    """
    import inspect

    narrowed = {}
    for name, own in vars(TrackingFsBackend).items():
        if name.startswith("_") or not inspect.isfunction(own):
            continue  # свойства и пробы сюда не попадают
        inner_fn = inspect.getattr_static(LocalFsBackend, name, None)
        if not inspect.isfunction(inner_fn):
            continue
        own_params = inspect.signature(own).parameters
        if any(p.kind is p.VAR_KEYWORD for p in own_params.values()):
            continue  # **kwargs принимает всё
        lost = set(inspect.signature(inner_fn).parameters) - set(own_params)
        if lost:
            narrowed[name] = sorted(lost)
    assert narrowed == {}


def test_wrap_skips_non_local():
    class HarborLike:
        pass

    ctx = type("C", (), {"is_tui": True})()
    sentinel = HarborLike()
    assert wrap_local_for_tracking(sentinel, ctx) is sentinel


def test_wrap_is_idempotent():
    ctx = fake_ctx(is_tui=True)
    wrapped = wrap_local_for_tracking(LocalFsBackend(), ctx)
    assert isinstance(wrapped, TrackingFsBackend)
    assert wrap_local_for_tracking(wrapped, ctx) is wrapped


def test_fs_tools_do_not_import_tui_or_legacy_packages():
    """Тулы не знают ни про UI-слой, ни про умирающий пакет — только про рекордер."""
    import gc_core.tools.fs.append_to_file as a
    import gc_core.tools.fs.apply_file_edits as e
    import gc_core.tools.fs.search_and_replace as s
    import gc_core.tools.fs.write_to_file_tool as w

    for mod in (w, a, s, e):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "diff_tui" not in src
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert not stripped.split()[1].startswith("gc_tui"), (
                    mod.__name__,
                    line,
                )


async def test_exec_argv_delegates_without_shell(tmp_path):
    """argv уходит внутрь дословно: обёртка не собирает shell-строку."""
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys\nprint('|'.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    hostile = [r"C:\temp\it's report.pdf", '{"a": "b c"}', "--flag='x'"]
    wrapped = TrackingFsBackend(LocalFsBackend(), local_ctx())

    res = await wrapped.aexec_argv([sys.executable, str(script), *hostile])

    assert res.return_code == 0
    assert res.stdout.splitlines()[0] == "|".join(hostile)

"""Данные агента пишутся только через хранилища (спека хранилищ v2, раздел 8).

Тред, память проекта, ответы и сессии фронтендов — это ``ThreadStore`` и
``AgentStore``: клиент подставляет своё хранилище (база, S3), и запись мимо
него молча осталась бы на диске процесса. Сторож обходит ``packages/*/src``
и запрещает файловые записи:

- ``open(...)`` и ``os.fdopen(...)`` с режимами ``w``, ``a``, ``x``, ``+``;
- ``Path.write_text``, ``Path.write_bytes`` (``await backend.write_bytes`` —
  запись workspace через ``os_backend``, не считается);
- ``os.replace``, ``os.rename``, ``atomic_replace``, ``shutil.copy*``,
  ``shutil.move``;
- ``tempfile.mkstemp`` и ``tempfile.NamedTemporaryFile``;
- ``json.dump`` в файловый объект.

Модуль, которому писать файлы можно, — в ``ALLOWED`` с причиной. Причина
«до …» — временное исключение, которое уходит вместе с переводом.
Исключение без единой записи в модуле — устаревшее, сторож его не терпит.
"""

import ast
from pathlib import Path

from gc_testkit.paths import REPO

_WRITE_MODES = frozenset("wax+")
_OS_CALLS = frozenset({"replace", "rename", "fdopen"})
_TEMPFILE_CALLS = frozenset({"mkstemp", "NamedTemporaryFile"})

_CORE = "packages/core/src/gc_core/"
_TUI = "packages/tui/src/gc_tui/"
_MCP = "packages/mcp/src/recommended_mcp/"
_SWARM = "packages/server/src/gc_server/swarm/"
_TESTKIT = "packages/testkit/src/gc_testkit/"

_OS_BACKEND = "запись workspace через os_backend"
_CONFIG = "конфиги пользователя и проекта"
_OLD_DURABLE = (
    "старое durable-хранилище тредов — до перевода Temporal на ThreadStore v2"
)
_EDITS = (
    "журнал и снимки правок workspace — до перевода журнала на append_side"
)
_MCP_RUNTIME = "MCP runtime: установка и состояние серверов"
_VOICE = "голосовой рантайм: свои аренды и состояние"
_UPDATER = "апдейтер"
_TESTKIT_REASON = "тестовый инструментарий, в продукт не входит"

ALLOWED: dict[str, str] = {
    # Реализации хранилищ.
    _CORE + "threads/file_tx.py": "файловое хранилище тредов v2",
    _CORE + "attachments/store.py": "файлы вложений тредов (картинки)",
    _CORE + "thread_store.py": _OLD_DURABLE,
    _CORE + "thread_store_journal.py": _OLD_DURABLE,
    _CORE + "project_memory/storage.py": "память проекта — до AgentStore",
    # Workspace — через os_backend и тулы файловой системы.
    _CORE + "backends/capabilities.py": _OS_BACKEND,
    _CORE + "backends/harbor.py": _OS_BACKEND,
    _CORE + "backends/local.py": _OS_BACKEND,
    _CORE + "tools/exec/download_file.py": "download_file пишет в workspace",
    _CORE + "agent_edits/journal.py": _EDITS,
    _CORE + "agent_edits/snapshots.py": _EDITS,
    _CORE + "agent_edits/revert.py": _EDITS,
    # Конфиги.
    _CORE + "config.py": _CONFIG,
    _CORE + "config_command.py": _CONFIG,
    _CORE + "config_sync.py": _CONFIG,
    _CORE + "config_modules/registry_write.py": _CONFIG,
    _CORE + "onboarding_config_writer.py": _CONFIG,
    _TUI + "screens/settings.py": _CONFIG + " (редактор настроек, профили)",
    # Логи.
    _CORE + "summarize/summary_logging.py": "лог суммаризации",
    # MCP.
    _CORE + "mcp_runtime/state.py": _MCP_RUNTIME,
    _CORE + "tools/connect_mcp/api.py": "медиа MCP вне треда",
    _MCP + "install.py": _MCP_RUNTIME,
    _MCP + "process_supervisor.py": _MCP_RUNTIME,
    _MCP + "runtime/wiring.py": _MCP_RUNTIME,
    # Апдейтер.
    _CORE + "updater/runner.py": _UPDATER,
    _CORE + "upgrade_gate.py": _UPDATER,
    # Служебные примитивы.
    _CORE + "utils/atomic_io.py": "примитив атомарной замены файла",
    _CORE + "utils/directory_write_access.py": "проба права записи в каталог",
    _CORE + "utils/file_lock.py": "файл межпроцессной блокировки",
    # TUI.
    _TUI
    + "screens/main.py": "/export пишет файл в cwd по просьбе пользователя",
    _TUI + "services/input_history.py": "история ввода TUI — файл установки",
    _TUI + "services/session_lock.py": (
        "блокировка сессии между окнами TUI одной машины (аренда v2 — "
        "по имени машины)"
    ),
    _TUI + "voice_runtime/leases.py": _VOICE,
    _TUI + "voice_runtime/state.py": _VOICE,
    # Swarm.
    _SWARM + "lifecycle.py": "swarm: рабочие каталоги участников",
    _SWARM + "runner.py": "swarm: рабочие каталоги участников",
    _SWARM + "storage.py": "swarm: своё хранилище состояния роя",
    # Тестовый инструментарий.
    _TESTKIT + "helpers/crossplatform.py": _TESTKIT_REASON,
    _TESTKIT + "legacy_threads.py": _TESTKIT_REASON,
    _TESTKIT + "tui_fixtures/stub_executor.py": _TESTKIT_REASON,
}


def _awaited(tree: ast.AST) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }


def _mode(call: ast.Call, position: int) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    if len(call.args) > position and isinstance(
        call.args[position], ast.Constant
    ):
        return call.args[position].value
    return None


def _writes_by_mode(mode: object) -> bool:
    return isinstance(mode, str) and bool(set(mode) & _WRITE_MODES)


def _module_call(func: ast.Attribute) -> tuple[str, str] | None:
    if isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return None


def _write_call(call: ast.Call, awaited: set[int]) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "open" and _writes_by_mode(_mode(call, 1)):
            return "open"
        return "atomic_replace" if func.id == "atomic_replace" else None
    if not isinstance(func, ast.Attribute):
        return None
    named = _module_call(func)
    if named is not None:
        module, name = named
        if module == "os" and name in _OS_CALLS:
            if name != "fdopen" or _writes_by_mode(_mode(call, 1)):
                return f"os.{name}"
            return None
        if module == "shutil" and (name.startswith("copy") or name == "move"):
            return f"shutil.{name}"
        if module == "tempfile" and name in _TEMPFILE_CALLS:
            return f"tempfile.{name}"
        if module == "json" and name == "dump":
            return "json.dump"
    if func.attr == "open" and _writes_by_mode(_mode(call, 0)):
        return "Path.open"
    if func.attr in ("write_text", "write_bytes") and id(call) not in awaited:
        return f"Path.{func.attr}"
    return None


def _writes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    awaited = _awaited(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _write_call(node, awaited)
            if kind is not None:
                found.append(f"{kind}:{node.lineno}")
    return found


def _scan() -> dict[str, list[str]]:
    found = {}
    for source in sorted(REPO.glob("packages/*/src")):
        for path in sorted(source.rglob("*.py")):
            writes = _writes(path)
            if writes:
                found[path.relative_to(REPO).as_posix()] = writes
    return found


def test_agent_data_writes_go_through_stores():
    found = _scan()
    assert found, "обход ничего не нашёл — сторож сломан, а не код чист"
    offenders = {
        path: writes for path, writes in found.items() if path not in ALLOWED
    }
    assert offenders == {}, (
        "запись файлов мимо хранилищ: данные агента — через ThreadStore/"
        "AgentStore; законная запись (workspace, конфиги, логи) — в ALLOWED "
        f"с причиной: {offenders}"
    )


def test_every_exception_is_still_needed():
    found = _scan()
    stale = sorted(path for path in ALLOWED if path not in found)
    assert stale == [], (
        f"модуль больше не пишет файлы — убрать из ALLOWED: {stale}"
    )


def test_guard_catches_every_primitive(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import json, os, shutil, tempfile\n"
        "from gc_core.utils.atomic_io import atomic_replace\n"
        "open(p, 'a')\n"
        "os.fdopen(fd, 'w')\n"
        "Path(p).write_text('x')\n"
        "Path(p).open(mode='wb')\n"
        "os.replace(a, b)\n"
        "atomic_replace(a, b)\n"
        "shutil.copyfile(a, b)\n"
        "tempfile.mkstemp()\n"
        "json.dump({}, handle)\n"
        "open(p)\n"
        "async def f(backend):\n"
        "    await backend.write_bytes(p, b'')\n",
        encoding="utf-8",
    )
    kinds = [item.split(":")[0] for item in _writes(sample)]
    assert kinds == [
        "open",
        "os.fdopen",
        "Path.write_text",
        "Path.open",
        "os.replace",
        "atomic_replace",
        "shutil.copyfile",
        "tempfile.mkstemp",
        "json.dump",
    ]

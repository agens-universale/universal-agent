"""Данные агента пишутся только через хранилища (спека хранилищ v2, раздел 8).

Тред, память проекта, ответы и сессии фронтендов — это ``ThreadStore`` и
``AgentStore``: клиент подставляет своё хранилище (база, S3), и запись мимо
него молча осталась бы на диске процесса. Сторож обходит ``packages/*/src``
и запрещает файловые записи:

- ``open``/``io.open``/``codecs.open``/``os.fdopen`` с режимами ``w``,
  ``a``, ``x``, ``+`` (режим не константой — тоже), ``Path.open`` с таким
  режимом, ``os.open`` с флагами записи;
- ``Path.write_text``, ``Path.write_bytes`` — и вызовом, и ссылкой
  (``asyncio.to_thread(path.write_text, …)``); ``await backend.write_bytes``
  — запись workspace через ``os_backend``, не считается;
- ``os.replace``, ``os.rename``, ``Path.rename``, ``atomic_replace``,
  ``shutil.copy*``, ``shutil.move``;
- ``tempfile.mkstemp`` и ``tempfile.NamedTemporaryFile``;
- ``json.dump`` в файловый объект.

Имена сверяются с импортами модуля: ``import os as _os`` и
``from shutil import move`` не прячут вызов.

Модуль, которому писать файлы можно, — в ``ALLOWED`` с причиной. Причина
«до …» — временное исключение, которое уходит вместе с переводом.
Исключение без единой записи в модуле — устаревшее, сторож его не терпит.
"""

import ast
from pathlib import Path

from gc_testkit.paths import REPO

_WRITE_MODES = frozenset("wax+")
_TEMPFILE_CALLS = frozenset({"mkstemp", "NamedTemporaryFile"})
_OPENERS = frozenset({"open", "io.open", "codecs.open", "builtins.open"})
_OS_OPEN_WRITE_FLAGS = frozenset(
    {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}
)
_PATH_WRITES = frozenset({"write_text", "write_bytes"})

_CORE = "packages/core/src/gc_core/"
_TUI = "packages/tui/src/gc_tui/"
_MCP = "packages/mcp/src/recommended_mcp/"
_SWARM = "packages/server/src/gc_server/swarm/"
_TESTKIT = "packages/testkit/src/gc_testkit/"

_OS_BACKEND = "запись workspace через os_backend"
_CONFIG = "конфиги пользователя и проекта"
_OLD_FILE_STORE = (
    "старое файловое хранилище тредов (контракт ThreadStoreBase у "
    "встраивающих): запись через блокировку журнала"
)
_EDITS = (
    "журнал и снимки правок workspace — до перевода журнала на append_side"
)
_MCP_RUNTIME = "MCP runtime: установка и состояние серверов"
_VOICE = "голосовой рантайм: свои аренды и состояние"
_UPDATER = "апдейтер"
_TESTKIT_REASON = "тестовый инструментарий, в продукт не входит"
_ROLLBACK = (
    "файлы develop (acp-sessions, responses-index) рядом с тредом — откат "
    "на develop видит сессии и ответы ветки; до следующего релиза"
)

ALLOWED: dict[str, str] = {
    # Реализации хранилищ.
    _CORE + "threads/file_tx.py": "файловое хранилище тредов v2",
    "packages/stores/src/gc_stores/sqlite/database.py": (
        "хранилища SQLite: файл базы создаётся с правами 0600"
    ),
    _CORE + "attachments/store.py": "файлы вложений тредов (картинки)",
    _CORE + "persistence/plan_file.py": (
        "файл плана для человека — пишут только файловые хранилища тредов "
        "(save_plan_file)"
    ),
    # Совместимость отката: файлы develop пишутся вместе с хранилищем.
    "packages/acp/src/gc_acp/session_index.py": _ROLLBACK,
    "packages/chat/src/gc_chat/responses/index_store.py": _ROLLBACK,
    _CORE + "thread_store.py": _OLD_FILE_STORE,
    _CORE + "thread_store_journal.py": (
        "примитивы атомарной записи и блокировки файлов тредов (file_tx v2, "
        "старое файловое хранилище)"
    ),
    _CORE + "project_memory/storage.py": (
        "файлы памяти проекта у FileAgentStore (атомарная запись, бэкап)"
    ),
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
    _TUI + "services/terminal_guard.py": (
        "вывод в /dev/null, когда терминал умер"
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


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Локальное имя → полное: ``import os as _os`` даёт ``_os → os``,
    ``from shutil import move as mv`` — ``mv → shutil.move``."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                names[alias.asname or alias.name] = full
    return names


def _qualified(func: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{aliases.get(func.value.id, func.value.id)}.{func.attr}"
    return None


def _mode_arg(call: ast.Call, position: int) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return call.args[position] if len(call.args) > position else None


def _writes_by_mode(call: ast.Call, position: int) -> bool:
    """Режим записи; режим не константой — считается записью."""
    mode = _mode_arg(call, position)
    if mode is None:
        return False
    if not isinstance(mode, ast.Constant):
        return True
    return isinstance(mode.value, str) and bool(set(mode.value) & _WRITE_MODES)


def _os_open_writes(call: ast.Call) -> bool:
    flags = call.args[1] if len(call.args) > 1 else None
    if flags is None:
        return False
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(flags)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    return bool(names & _OS_OPEN_WRITE_FLAGS)


def _named_write(name: str, call: ast.Call) -> str | None:
    if name in _OPENERS:
        return name if _writes_by_mode(call, 1) else None
    if name == "os.fdopen":
        return name if _writes_by_mode(call, 1) else None
    if name == "os.open":
        return name if _os_open_writes(call) else None
    module, _, attr = name.rpartition(".")
    if name in ("os.replace", "os.rename") or attr == "atomic_replace":
        return name
    if module == "shutil" and (attr.startswith("copy") or attr == "move"):
        return name
    if module == "tempfile" and attr in _TEMPFILE_CALLS:
        return name
    return name if name == "json.dump" else None


def _method_write(call: ast.Call, awaited: set[int]) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    # ``.open`` бывает у чего угодно (``facade.open(body)``): запись — только
    # явный режим записи константой.
    mode = _mode_arg(call, 0)
    if (
        func.attr == "open"
        and isinstance(mode, ast.Constant)
        and _writes_by_mode(call, 0)
    ):
        return "Path.open"
    if func.attr in _PATH_WRITES and id(call) not in awaited:
        return f"Path.{func.attr}"
    return "Path.rename" if func.attr == "rename" else None


def _write_call(
    call: ast.Call, awaited: set[int], aliases: dict[str, str]
) -> str | None:
    name = _qualified(call.func, aliases)
    if name is not None:
        found = _named_write(name, call)
        if found is not None:
            return found
    found = _method_write(call, awaited)
    if found is not None:
        return found
    # Запись, переданная ссылкой: ``asyncio.to_thread(path.write_text, s)``.
    for arg in call.args:
        if isinstance(arg, ast.Attribute) and arg.attr in _PATH_WRITES:
            return f"Path.{arg.attr} (ссылка)"
    return None


def _writes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    awaited = _awaited(tree)
    aliases = _aliases(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = _write_call(node, awaited, aliases)
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
        "import asyncio, io, json, os, shutil, tempfile\n"
        "import os as _os\n"
        "from shutil import move as mv\n"
        "from gc_core.utils.atomic_io import atomic_replace\n"
        "open(p, 'a')\n"
        "open(p, mode)\n"
        "io.open(p, 'wb')\n"
        "os.fdopen(fd, 'w')\n"
        "os.open(p, os.O_CREAT | os.O_WRONLY)\n"
        "Path(p).write_text('x')\n"
        "Path(p).open(mode='wb')\n"
        "Path(p).rename(q)\n"
        "_os.replace(a, b)\n"
        "atomic_replace(a, b)\n"
        "shutil.copyfile(a, b)\n"
        "mv(a, b)\n"
        "tempfile.mkstemp()\n"
        "json.dump({}, handle)\n"
        "asyncio.to_thread(path.write_bytes, data)\n"
        "open(p)\n"
        "os.open(p, os.O_RDONLY)\n"
        "facade.open(body)\n"
        "async def f(backend):\n"
        "    await backend.write_bytes(p, b'')\n",
        encoding="utf-8",
    )
    kinds = [item.rsplit(":", 1)[0] for item in _writes(sample)]
    assert kinds == [
        "open",
        "open",
        "io.open",
        "os.fdopen",
        "os.open",
        "Path.write_text",
        "Path.open",
        "Path.rename",
        "os.replace",
        "gc_core.utils.atomic_io.atomic_replace",
        "shutil.copyfile",
        "shutil.move",
        "tempfile.mkstemp",
        "json.dump",
        "Path.write_bytes (ссылка)",
    ]

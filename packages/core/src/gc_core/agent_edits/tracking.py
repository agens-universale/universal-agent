"""TrackingFsBackend: шелл-делта вокруг команд поверх LocalFsBackend.

Единственная роль обёртки — правки, которых не видит ни один тул: файлы,
изменённые ``execute_command`` / ``run_python_code``. Вокруг такого вызова
поднимается ``ShellDelta``, и разница уходит в журнал правок треда. Текстовые
правки тулов сюда не попадают: их пишет ``agent_edits.recorder`` прямо из
fs-тулов, поэтому обёртка про них ничего не знает.

Ставится во всех режимах (CLI / server / TUI), Harbor — без неё. Гейт записи
один и живёт внутри канала: без thread-identity не пишется ничего и git не
зовётся. Остальная поверхность бэкенда — чистое делегирование внутрь.

Порт (``backends.protocol.FsBackend``) не наследуется: обёртка повторяет его
поверхность, а не является им. Но форма методов обязана совпадать — файловые
операции корутинные, обходчики асинхронные, команду запускают только
``a``-формы; синхронных двойников запуска здесь нет вовсе (см.
``_atrack_shell_delta``). Полноту поверхности стережёт
``test_tracking_backend_delegates_full_backend_surface``, сигнатуры —
``test_tracking_backend_overrides_keep_the_inner_signature``.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Optional

from gc_core.agent_edits.shell_delta import ShellDelta
from gc_core.backends.async_protocol import arun_backend_exec_raw
from gc_core.backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS


def _capture_kw(capture_max_chars: Optional[int]) -> dict:
    """``capture_max_chars`` уходит внутрь, только если он задан.

    Тот же приём, что у ``FsBackend.exec_argv`` в ``backends.protocol``, и по
    той же причине: бэкенд без этого параметра обязан продолжать работать.
    Безусловный проброс роняет такой бэкенд ``TypeError`` — а вызывающий гасит
    исключение общим ``except`` и молча уходит на обходной путь.
    """
    return (
        {}
        if capture_max_chars is None
        else {"capture_max_chars": capture_max_chars}
    )


def wrap_local_for_tracking(inner: Any, ctx: Any) -> Any:
    """Local → TrackingFsBackend. Уже Tracking / не Local — без изменений."""
    if isinstance(inner, TrackingFsBackend):
        return inner
    # LocalFsBackend по имени класса (с наследниками: SandboxBackend) — без
    # импорта tools.* на module load
    if "LocalFsBackend" not in {c.__name__ for c in type(inner).__mro__}:
        return inner
    return TrackingFsBackend(inner, ctx)


class TrackingFsBackend:
    """Декоратор LocalFsBackend: единственная роль — шелл-делта вокруг команд."""

    def __init__(self, inner: Any, ctx: Any) -> None:
        self._inner = inner
        self._ctx = ctx
        # Карта дедупликации шелл-делты: обёртка одна на ctx и переживает
        # отдельные команды, ShellDelta — нет.
        # путь → (хэш того, чем кончила команда, подпись файла тогда же);
        # подпись нужна, чтобы память не пережила правку мимо канала
        self._edits_seen: dict[str, tuple[str, tuple[int, int] | None]] = {}
        # Кэш путей набора карманного подканала (по подписи файла журнала):
        # та же причина жить на обёртке, что у _edits_seen.
        self._pocket_paths_cache: dict = {}
        # `is_sandbox`, `isolates_exec` и `default_exec_timeout` НЕ присваиваем:
        # все стали property с сеттером (проброс поверхности бэкенда), и
        # присваивание здесь молча писало бы во ВНУТРЕННИЙ backend вместо
        # обёртки — а читать их всё равно надо у него, что property и делает.

    @property
    def exec_output_capture_max_chars(self) -> int:
        return int(
            getattr(
                self._inner,
                "exec_output_capture_max_chars",
                DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
            )
        )

    @exec_output_capture_max_chars.setter
    def exec_output_capture_max_chars(self, value: int) -> None:
        self._inner.exec_output_capture_max_chars = int(value)

    # Capability probes живут на inner (ленивый кэш там же) — обёртка только
    # пробрасывает, иначе native-пути (find/grep) молча считаются недоступными.
    @property
    def has_find(self) -> bool:
        return self._inner.has_find

    @property
    def has_grep(self) -> bool:
        return self._inner.has_grep

    @property
    def grep_exclude_dir(self) -> bool:
        return self._inner.grep_exclude_dir

    @property
    def grep_include(self) -> bool:
        return self._inner.grep_include

    @property
    def supports_background(self) -> bool:
        """Умение отцепить команду в фон — свойство ВНУТРЕННЕГО бэкенда.

        Без проброса тул считал бы обёртку бэкендом без фона и отказывал бы в
        фоновом запуске, хотя под ней обычный LocalFsBackend.
        """
        return bool(getattr(self._inner, "supports_background", False))

    @property
    def inner(self) -> Any:
        return self._inner

    async def awarm_capabilities(self) -> None:
        # Пробы — свойство ВНУТРЕННЕГО бэкенда: шелл-делте тут отслеживать
        # нечего, а кэш обязан лечь именно на него, иначе обёртка прогреет
        # свой, а свойства выше читают чужой — и первый поиск снова заплатит
        # за пробы (а до прогрева получит CapabilitiesNotWarmedError).
        await self._inner.awarm_capabilities()

    async def _atrack_shell_delta(self, run: Callable[[], Any]) -> Any:
        """Правки команды в журнал: снимок «до», вызов, снимок «после».

        ЕДИНСТВЕННАЯ форма. Синхронного близнеца нет намеренно — вместе с
        синхронными ``exec`` / ``exec_argv`` / ``run_python`` самой обёртки:
        цепочка ``SyncExecAdapter`` → ``to_thread(TrackingFsBackend.exec)`` →
        ``LocalFsBackend.exec`` → ``run_coro_blocking(aexec)`` заводила ЕЩЁ
        ОДИН loop в потоке пула. Кооперативный стоп до подпроцесса не доходил,
        а ``asyncio.run`` хода на выходе ждал ``shutdown_default_executor``:
        после Esc поток жил до таймаута команды.

        Обе пробы ``ShellDelta`` уходят через ``asyncio.to_thread``, и это не
        стиль, а необходимость: внутри — запуск git (``git status``, диффы) и
        чтение файлов, то есть блокирующая работа. Пока агента вёл демон-поток,
        loop это не касалось; теперь агент работает НА loop, и те же пробы
        морозили бы его на каждой команде. Код проб не менялся — изменилось то,
        ГДЕ он исполняется.

        Soft-fail целиком внутри канала — ни ``begin``, ни ``after_and_record``
        не бросают: команда пользователя уже выполнена, журнал вторичен.

        Снимок «после» снимается в ``finally``, а не только на успешном
        возврате: к моменту исключения команда уже могла написать файлы —
        отмена хода приходит прерыванием прямо из ожидания процесса, а бэкенд
        по транспорту (sandbox, os_backend) может отвалиться после того, как
        команда отработала на той стороне. Без ``finally`` такие правки уносило
        бы из журнала, счётчиков хода и с экрана — при том что на диске они
        есть.

        Цена выбрана сознательно: отмена хода теперь ждёт съёма делты (обход
        status и диффы), а второе прерывание во время съёма подменит исходное
        исключение своим — того же типа, но из другой точки. Обе цены меньше,
        чем молча потерянные правки: команда уже изменила диск, и экран,
        который об этом не знает, врёт пользователю.
        """
        edits_delta = await asyncio.to_thread(
            ShellDelta.begin,
            self._ctx,
            seen=self._edits_seen,
            pocket_cache=self._pocket_paths_cache,
        )
        try:
            return await run()
        finally:
            if edits_delta is not None:
                await asyncio.to_thread(edits_delta.after_and_record)

    # --- запуск команд ---

    async def aexec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return await self._atrack_shell_delta(
            lambda: self._inner.aexec(
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
                **_capture_kw(capture_max_chars),
            )
        )

    async def aexec_raw(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        """Сырой канал тоже меняет дерево (mkdir/mktemp/rm внутренних
        помощников), поэтому дельта журналируется так же, как у ``aexec``."""
        return await self._atrack_shell_delta(
            lambda: arun_backend_exec_raw(
                self._inner,
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
                **_capture_kw(capture_max_chars),
            )
        )

    async def aexec_argv(
        self,
        argv: list,
        *,
        stdin: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        return await self._atrack_shell_delta(
            lambda: self._inner.aexec_argv(
                argv,
                stdin=stdin,
                cwd=cwd,
                timeout=timeout,
                env=env,
                **_capture_kw(capture_max_chars),
            )
        )

    async def arun_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> Any:
        # Потолок захвата обязан доехать до бэкенда: внутренние каналы
        # (MCP-carrier, native-поиск) заказывают СВОЙ потолок, и обёртка,
        # молча его глотающая, роняет вызов TypeError'ом — а зовущий его
        # гасит общим except и тихо уходит на медленный обходной путь.
        return await self._atrack_shell_delta(
            lambda: self._inner.arun_python(
                code,
                cwd=cwd,
                timeout=timeout,
                env=env,
                **_capture_kw(capture_max_chars),
            )
        )

    async def aspawn_background(
        self, command: str, *, cwd: Any = None, env: Any = None
    ) -> dict:
        """Фоновый запуск проксируем как есть, без шелл-делты.

        Делта снимается ВОКРУГ вызова, а этот возвращается мгновенно: разница
        вокруг него пуста по построению. Файлы, которые фоновый процесс
        напишет позже, журнал не увидит — показать их можно было бы только
        опросом, которого у канала нет. Это осознанная граница, а не пропуск.
        """
        return await self._inner.aspawn_background(command, cwd=cwd, env=env)

    # Синхронных ``exec`` / ``exec_raw`` / ``exec_argv`` / ``run_python`` у
    # обёртки НЕТ — единственное место, где её поверхность у́же поверхности
    # бэкенда, и это намеренно. На порту они остались входом для ЧУЖИХ
    # синхронных реализаций; своим держать обе формы рядом запрещает страж
    # ``tests/test_sync_async_pairs.py``. Здесь запрет ещё и по существу:
    # рабочая синхронная форма — это тот самый второй loop (см.
    # ``_atrack_shell_delta``).

    # --- запись ---

    async def write(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None:
        await self._inner.write(path, content, encoding=encoding)

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._inner.write_bytes(path, data)

    async def append(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None:
        await self._inner.append(path, content, encoding=encoding)

    async def copy(self, src: str, dst: str) -> None:
        await self._inner.copy(src, dst)

    async def makedirs(self, path: str) -> None:
        await self._inner.makedirs(path)

    def default_cwd(self) -> Optional[str]:
        return self._inner.default_cwd()

    # Поверхность бэкенда, доросшая у develop за время ветки: обход каталогов и
    # два свойства. Все — чтение, журнала не касаются; страж
    # `test_tracking_backend_delegates_full_backend_surface` следит, чтобы
    # обёртка не усыхала молча — без проброса native-обход просто исчезает.
    @property
    def default_exec_timeout(self):
        return self._inner.default_exec_timeout

    @default_exec_timeout.setter
    def default_exec_timeout(self, value) -> None:
        self._inner.default_exec_timeout = value

    @property
    def is_sandbox(self) -> bool:
        return self._inner.is_sandbox

    @is_sandbox.setter
    def is_sandbox(self, value: bool) -> None:
        self._inner.is_sandbox = value

    @property
    def isolates_exec(self) -> bool:
        # Песочница (SandboxBackend наследует LocalFsBackend и поэтому
        # оборачивается здесь) изолирует команды. Флаг живёт на внутреннем
        # бэкенде: у LocalFsBackend он False, у SandboxBackend — True.
        # Без проброса обёртка врёт `exec_is_isolated`, и MCP
        # `execute_on: os_backend` поднимает сервер в нашем процессе, хотя
        # команды идут через bwrap / sandbox-exec.
        return bool(getattr(self._inner, "isolates_exec", False))

    @isolates_exec.setter
    def isolates_exec(self, value: bool) -> None:
        self._inner.isolates_exec = value

    def isabs(self, path: str) -> bool:
        return self._inner.isabs(path)

    async def walk_entries(
        self, path: str, *, prune_names=(), prune_dot_dirs: bool = False
    ) -> list:
        return await self._inner.walk_entries(
            path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
        )

    async def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names=(),
        prune_dot_dirs: bool = False,
    ):
        async for entry in self._inner.iter_walk_entries(
            path,
            should_descend=should_descend,
            prune_names=prune_names,
            prune_dot_dirs=prune_dot_dirs,
        ):
            yield entry

    # --- чтение и обход ---

    async def exists(self, path: str) -> bool:
        return await self._inner.exists(path)

    async def lexists(self, path: str) -> bool:
        return await self._inner.lexists(path)

    async def is_file(self, path: str) -> bool:
        return await self._inner.is_file(path)

    async def is_dir(self, path: str) -> bool:
        return await self._inner.is_dir(path)

    async def is_symlink(self, path: str) -> bool:
        return await self._inner.is_symlink(path)

    async def getsize(self, path: str) -> int:
        return await self._inner.getsize(path)

    async def stat_info(
        self, path: str, follow_symlinks: bool = False
    ) -> dict:
        return await self._inner.stat_info(
            path, follow_symlinks=follow_symlinks
        )

    async def readable(self, path: str) -> bool:
        return await self._inner.readable(path)

    async def writable(self, path: str) -> bool:
        return await self._inner.writable(path)

    async def realpath(self, path: str) -> str:
        return await self._inner.realpath(path)

    async def is_hidden(self, path: str) -> bool:
        return await self._inner.is_hidden(path)

    async def read(self, path: str) -> str:
        return await self._inner.read(path)

    async def read_bytes(self, path: str) -> bytes:
        return await self._inner.read_bytes(path)

    async def iter_lines(self, path: str) -> AsyncIterator[str]:
        async for line in self._inner.iter_lines(path):
            yield line

    async def read_head_bytes(self, path: str, n: int) -> bytes:
        return await self._inner.read_head_bytes(path, n)

    async def sha256_bytes(self, path: str) -> str:
        return await self._inner.sha256_bytes(path)

    async def listdir(self, path: str) -> list:
        return await self._inner.listdir(path)

    async def walk(self, path: str) -> list:
        return await self._inner.walk(path)

    async def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> AsyncIterator[tuple[str, bool]]:
        async for item in self._inner.iter_walk(
            path, should_descend=should_descend
        ):
            yield item

    async def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> AsyncIterator[str]:
        async for item in self._inner.iter_files(
            path, should_descend=should_descend
        ):
            yield item

    def normpath(self, path: str) -> str:
        return self._inner.normpath(path)

    def join(self, base: str, name: str) -> str:
        return self._inner.join(base, name)

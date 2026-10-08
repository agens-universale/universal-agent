"""Корень рабочей области: общий запасной путь и громкий отказ вместо «/».

`aprime_backend_cwd` умел ровно одно — позвать `aprime_default_cwd`, если он у
бэкенда есть. У Harbor он есть, у всякого другого async-only бэкенда песочницы
— нет, и тогда `discover_backend_cwd` уходил в СИНХРОННЫЙ `exec`, которого у
таких бэкендов не бывает вовсе, и молча возвращал `/`. Модель начинала работать
от корня файловой системы контейнера, и понять это можно было только по
странным путям в её ответах.

Почему дефект жил при зелёных тестах: двойник `SandboxBackend` в
`test_execution_workspace.py` имеет синхронный `exec`, поэтому там `pwd`
отвечает. Здесь двойник намеренно async-only — как настоящая песочница.
"""

import asyncio
import logging
import time

import pytest
from gc_core import execution_workspace as ew
from gc_core.execution_workspace import (
    WorkspaceRootUnknownError,
    aprime_backend_cwd,
    areconcile_remote_root,
    build_execution_workspace,
    discover_backend_cwd,
)


class _Res:
    def __init__(self, stdout: str, return_code: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = return_code
        self.timed_out = False


class _AsyncOnlySandbox:
    """Песочница без синхронного `exec` и без своего прогрева — как чужой бэкенд."""

    is_sandbox = True

    def __init__(self, *, pwd="/work", raises=None, hangs=False):
        self._pwd = pwd
        self._raises = raises
        self._hangs = hangs
        self.asked = 0

    def default_cwd(self):
        return None

    def normpath(self, path: str) -> str:
        return path.rstrip("/") or "/"

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    async def aexec(self, command, **kwargs):
        self.asked += 1
        if self._hangs:
            await asyncio.sleep(30)
        if self._raises is not None:
            raise self._raises
        return _Res((self._pwd or "") + "\n")

    # Порт признаёт бэкенд своим только при всех трёх формах.
    async def aexec_argv(self, argv, **kwargs):
        return await self.aexec(" ".join(argv), **kwargs)

    async def arun_python(self, code, **kwargs):
        raise NotImplementedError


async def test_generic_pwd_answers_for_an_async_only_sandbox():
    """Главное свойство: корень узнаётся даже без `aprime_default_cwd`.

    На исходном коде здесь `/`: своего прогрева у бэкенда нет, а синхронный
    `exec`, к которому уходил `discover_backend_cwd`, отсутствует.
    """
    backend = _AsyncOnlySandbox(pwd="/work")

    await aprime_backend_cwd(backend, {})

    assert backend.asked == 1, "окружение не спросили вовсе"
    assert discover_backend_cwd(backend) == "/work", (
        "узнанный каталог не виден синхронным сборщикам рабочей области"
    )
    assert build_execution_workspace(backend).root == "/work"


async def test_a_silent_environment_is_a_loud_error():
    """Ничего не вышло — отказ, а не тихий `/`.

    Цена ошибки несимметрична: лишний отказ сборки виден сразу, а корень `/`
    уводит весь прогон работать не там.
    """
    backend = _AsyncOnlySandbox(raises=RuntimeError("контейнер не отвечает"))

    with pytest.raises(WorkspaceRootUnknownError) as excinfo:
        await aprime_backend_cwd(backend, {})

    assert "launch_root" in str(excinfo.value), (
        "в отказе нет подсказки, чем его лечить"
    )


async def test_a_config_root_makes_the_failure_harmless():
    """Корень назван явно — спрашивать окружение незачем и отказывать не за что."""
    backend = _AsyncOnlySandbox(raises=RuntimeError("контейнер не отвечает"))

    await aprime_backend_cwd(backend, {"launch_root": "/testbed"})

    assert backend.asked == 0, "окружение спросили, хотя корень задан конфигом"


async def test_a_hanging_environment_does_not_hang_the_build(monkeypatch):
    """Висящий `pwd` обязан упереться в потолок, а не держать сборку контекста."""
    monkeypatch.setattr(ew, "_CWD_PRIME_CEILING_SEC", 0.05)
    backend = _AsyncOnlySandbox(hangs=True)

    started = time.monotonic()
    with pytest.raises(WorkspaceRootUnknownError):
        await aprime_backend_cwd(backend, {})
    spent = time.monotonic() - started

    assert spent < 1.0, f"сборка ждала молчащее окружение {spent:.2f} с"


async def test_a_declared_cwd_is_not_asked_again():
    """Бэкенд уже знает каталог — второй раз не спрашиваем."""

    class _Knows(_AsyncOnlySandbox):
        def default_cwd(self):
            return "/already"

    backend = _Knows()

    await aprime_backend_cwd(backend, {})

    assert backend.asked == 0


class _SandboxRoot:
    """Песочница с уже известным cwd и управляемым ответом ``is_dir``."""

    is_sandbox = True

    def __init__(self, *, cwd=None, is_dir_result=False, is_dir_error=None):
        self._cwd = cwd
        self._is_dir_result = is_dir_result
        self._is_dir_error = is_dir_error
        self.checked = []

    def default_cwd(self):
        return self._cwd

    async def is_dir(self, path):
        self.checked.append(path)
        if self._is_dir_error is not None:
            raise self._is_dir_error
        return self._is_dir_result


async def test_dead_sandbox_does_not_keep_a_host_launch_root(tmp_path):
    """Оболочка не ответила на проверку корня — ран не стартует с путём хоста."""
    backend = _SandboxRoot(is_dir_error=RuntimeError("docker down"))
    cfg = {"launch_root": str(tmp_path), "project_root": str(tmp_path)}

    with pytest.raises(WorkspaceRootUnknownError) as excinfo:
        await areconcile_remote_root(backend, cfg)

    assert cfg["launch_root"] == str(tmp_path)
    assert "Хостовый путь" in str(excinfo.value)
    assert backend.checked == [str(tmp_path)]


async def test_missing_remote_dir_without_container_cwd_does_not_start():
    """Каталога на той стороне нет и cwd окружения не узнан — хостовый корень не оставляем."""
    backend = _SandboxRoot(cwd=None, is_dir_result=False)
    cfg = {"launch_root": "/home/alex/proj"}

    with pytest.raises(WorkspaceRootUnknownError):
        await areconcile_remote_root(backend, cfg)

    assert cfg["launch_root"] == "/home/alex/proj"


async def test_host_path_is_replaced_when_container_cwd_is_known():
    backend = _SandboxRoot(cwd="/data", is_dir_result=False)
    cfg = {"launch_root": "/home/alex/proj", "project_root": "/home/alex/proj"}

    await areconcile_remote_root(backend, cfg)

    assert cfg["launch_root"] == "/data"
    assert cfg["project_root"] == "/data"


async def test_confirmed_container_launch_root_is_kept():
    backend = _SandboxRoot(cwd="/data", is_dir_result=True)
    cfg = {"launch_root": "/data/sub", "project_root": "/data/sub"}

    await areconcile_remote_root(backend, cfg)

    assert cfg["launch_root"] == "/data/sub"
    assert cfg["project_root"] == "/data/sub"


async def test_a_local_backend_is_never_a_hard_failure(caplog):
    """Не песочница — корень берётся из `os.getcwd()`, отказывать не за что.

    Но отказ собственного прогрева обязан быть слышен: молчание здесь уже
    стоило одного прогона.
    """

    class _Local:
        is_sandbox = False

        def default_cwd(self):
            return None

        async def aprime_default_cwd(self):
            return None

    with caplog.at_level(logging.WARNING):
        await aprime_backend_cwd(_Local(), {})

    assert any(
        "backend_cwd_prime_failed" in r.getMessage() for r in caplog.records
    )


def _acreate_run_context_node():
    import ast
    import importlib.util
    import pathlib

    origin = importlib.util.find_spec("gc_core.run_context").origin
    tree = ast.parse(pathlib.Path(origin).read_text(encoding="utf-8"))
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "acreate_run_context"
    )


def _calls_named(node, name: str) -> list:
    import ast

    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name
    ]


def test_the_root_is_asked_before_mcp_discovery():
    """Спрашивать корень надо ДО дискавери, иначе отказ бросает MCP-сессии.

    Проверка по коду, а не по поведению: поведенческая потребовала бы поднять
    настоящую сборку контекста с дискавери, то есть подменить ровно то, что
    проверяется.
    """
    node = _acreate_run_context_node()
    prime_at = _calls_named(node, "aprime_backend_cwd")
    discovery_at = _calls_named(node, "abuild_tools_data")

    assert prime_at, "сборка больше не спрашивает корень — проверка устарела"
    assert discovery_at, "проверка устарела: дискавери MCP тут больше нет"
    assert min(prime_at) < min(discovery_at), (
        f"прогрев корня (строка {min(prime_at)}) стоит ПОСЛЕ дискавери MCP "
        f"({min(discovery_at)}): отказ оставит открытые сессии"
    )


def test_a_failed_prime_releases_the_run_logger():
    """Отказ прогрева обязан отдать логгер и упасть дальше.

    Логгер прогона создаётся до этого места и держит открытый файл до конца
    процесса: та же утечка, которую уже чинили на отказе сборки.
    """
    import ast

    node = _acreate_run_context_node()
    guarded = [
        t
        for t in ast.walk(node)
        if isinstance(t, ast.Try)
        and any(
            getattr(getattr(c, "func", None), "id", None)
            == "aprime_backend_cwd"
            for stmt in t.body
            for c in ast.walk(stmt)
        )
    ]
    assert guarded, (
        "прогрев не обёрнут в try — его отказ оставит логгер прогона с открытым файлом"
    )

    handlers = [h for t in guarded for h in t.handlers]
    released = any(
        getattr(getattr(c, "func", None), "id", None) == "release_run_logger"
        for h in handlers
        for c in ast.walk(h)
    )
    reraised = any(
        isinstance(n, ast.Raise) and n.exc is None
        for h in handlers
        for n in ast.walk(h)
    )

    assert released, "логгер прогона не освобождается при отказе прогрева"
    assert reraised, "отказ проглочен: сборка обязана упасть дальше"


async def test_the_primed_answer_is_not_kept_forever():
    """Кэш узнанного каталога держит бэкенд слабо — иначе это утечка на прогон."""
    import gc
    import weakref

    backend = _AsyncOnlySandbox(pwd="/work")
    await aprime_backend_cwd(backend, {})
    ref = weakref.ref(backend)

    del backend
    gc.collect()

    assert ref() is None, "бэкенд пережил свой прогон: кэш каталога держит его"

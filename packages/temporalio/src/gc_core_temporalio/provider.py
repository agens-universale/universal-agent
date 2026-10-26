"""Worker-only context preparation for pinned native deployment profiles."""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal

from gc_core.config_modules import AppConfig
from gc_core.config_modules.agent_edits import AgentEditsRecordConfig
from gc_core.config_modules.auth import build_auth_connector
from gc_core.config_modules.env_interpolation import (
    resolve_env_placeholders_strict,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FS_VIEW_IMAGE_NAME,
    PROJECT_MEMORY_TOOL_NAME,
)
from gc_core.context_preparation import settings_from_snapshot
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv, is_explicit
from gc_core.thread_files import thread_files_capability
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gc_core_temporalio.activity_config import ActivitiesConfig
from gc_core_temporalio.public_config import (
    fingerprint_mismatch,
    public_config_snapshot,
    settings_fingerprint,
)

logger = logging.getLogger(__name__)

#: Бэкенды ОС, которые под воркером Temporal не прогонялись.
_UNTESTED_BACKENDS = frozenset({"docker", "ssh", "exec", "sandbox"})


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: str
    store_id: str = "files"
    core: AppConfig = Field(default_factory=AppConfig)
    #: Рантайм агента (как у ``AsyncAgent``: режим текстового ответа и
    #: прочие факты поверхности). ``launch_root`` воркер всегда ставит в
    #: ``workspace``.
    runtime: RuntimeEnv | None = None

    @model_validator(mode="before")
    @classmethod
    def _split_loaded_settings(cls, value):
        """``core`` принимает и ``LoadedSettings`` — те же настройки, что
        получает ``AsyncAgent.create``: настройки и рантайм разом."""
        if isinstance(value, dict) and isinstance(
            value.get("core"), LoadedSettings
        ):
            loaded = value["core"]
            value = {**value, "core": loaded.settings}
            value.setdefault("runtime", loaded.runtime)
        return value


class StoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["file", "sqlite"] = "file"
    path: str


class WorkerVersioningConfig(BaseModel):
    """Worker Versioning Temporal: сборки воркера и закрепление ходов.

    Ход закреплён за сборкой, на которой начался (PINNED), и доигрывается
    ею; следующий ход после Continue-As-New идёт на текущей сборке. При
    деплое новая сборка объявляется текущей
    (``temporal worker deployment set-current-version``), старый воркер
    гасится, когда его версия опустела (простаивающие сессии переходят
    на новую сборку не позже чем через сутки).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    deployment_name: str = Field(default="ua-agent", min_length=1)
    #: Номер сборки; по умолчанию — версия пакета ``gc-core-temporalio`` и
    #: хэш кода (``0.5.0+a1b2c3d4e5f6``): правка без подъёма версии — тоже
    #: новая сборка, иначе Temporal отдал бы закреплённые ходы новому коду.
    build_id: str | None = Field(default=None, min_length=1)
    #: Объявить эту сборку текущей при старте воркера — для одного воркера
    #: и локальной разработки. При раскатке это делает оператор.
    set_current_on_start: bool = False

    def resolved_build_id(self) -> str:
        if self.build_id:
            return self.build_id
        from importlib.metadata import version

        return f"{version('gc-core-temporalio')}+{code_fingerprint()}"


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """sha256 исходников ``gc_core`` и ``gc_core_temporalio`` (12 hex).

    Код workflow и всё, что он исполняет, живёт в этих пакетах. Пути
    относительны пакету и отсортированы — хэш одинаков на всех воркерах
    одной сборки.
    """
    import gc_core

    import gc_core_temporalio

    digest = hashlib.sha256()
    for package in (gc_core, gc_core_temporalio):
        root = Path(package.__file__).parent
        for path in sorted(root.rglob("*.py")):
            digest.update(
                f"{package.__name__}/{path.relative_to(root).as_posix()}\0".encode()
            )
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()[:12]


def _agent_stores(agent_store, profiles) -> dict:
    """Профиль → хранилище агента; профиля нет в словаре — файловое."""
    from collections.abc import Mapping

    from gc_core.agent_store import check_agent_store

    if agent_store is None:
        return {}
    if not isinstance(agent_store, Mapping):
        check_agent_store(agent_store)
        return dict.fromkeys(profiles, agent_store)
    unknown = sorted(set(agent_store) - set(profiles))
    if unknown:
        raise ValueError(f"agent_store: нет таких профилей: {unknown}")
    return {
        name: check_agent_store(store) for name, store in agent_store.items()
    }


def _without_human(settings):
    """Профиль для работы без человека: инструмента вопроса нет, режим
    ``ask`` охраны — ``deny`` (отказ с причиной, модель ищет другой путь),
    иначе ход ждал бы ответа вечно. Явные ``allow``/``deny`` не трогаются."""
    security = settings.security

    def no_ask(section, field="mode"):
        value = getattr(section, field)
        if value != "ask":
            return section
        return section.model_copy(update={field: "deny"})

    security = security.model_copy(
        update={
            "write_guard": no_ask(security.write_guard),
            "read_guard": no_ask(security.read_guard),
            "url_policy": no_ask(security.url_policy),
            "command_moderation": no_ask(security.command_moderation),
        }
    )
    stop = list(settings.agent.stop_tools)
    if ASK_HUMAN_TOOL_NAME not in stop:
        stop.append(ASK_HUMAN_TOOL_NAME)
    return settings.model_copy(
        update={
            "security": security,
            "agent": settings.agent.model_copy(update={"stop_tools": stop}),
        }
    )


class WorkerConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    address: str = "localhost:7233"
    namespace: str = "default"
    workflow_queue: str = "ua-workflow"
    resource_queue: str = "ua-resource-local"
    model_queue: str = "ua-model"
    worker_id: str = "local"
    #: Каталог служебного состояния воркера: от него считается умолчание
    #: ``thread_stores`` (``threads``). ``load(path)`` по умолчанию берёт
    #: каталог файла конфига.
    state_dir: str | None = None
    thread_stores: dict[str, StoreConfig]
    profiles: dict[str, AgentProfile]
    #: Таймауты и повторы activities по уровням (``activity_config``):
    #: сбои провайдера модели по умолчанию повторяются без ограничения.
    activities: ActivitiesConfig = Field(default_factory=ActivitiesConfig)
    #: Отвечает ли человек на вопросы агента (``turn_ask_human``,
    #: подтверждения охраны). По умолчанию нет — агент работает автономно:
    #: инструмента вопроса у него нет, режим ``ask`` охраны — ``deny``.
    human_input: bool = False
    #: Worker Versioning (по умолчанию выключен).
    versioning: WorkerVersioningConfig = Field(
        default_factory=WorkerVersioningConfig
    )

    @model_validator(mode="before")
    @classmethod
    def _state_defaults(cls, data):
        """Служебное состояние по умолчанию — в ``state_dir``.

        Не в cwd: воркер стартует в workspace агента, и треды по умолчанию
        оказались бы в папке, которую агент читает и правит. Без
        ``state_dir`` хранилище тредов задаётся явно (``thread_stores``).
        """
        if not isinstance(data, dict) or not data.get("state_dir"):
            return data
        root = Path(data["state_dir"]).absolute()
        data = dict(data)
        data.setdefault(
            "thread_stores",
            {"files": {"kind": "file", "path": str(root / "threads")}},
        )
        return data

    def state_paths(self):
        """Служебные пути воркера для ``protected_paths``: треды и payload.

        Базы SQLite — вместе с ``-wal``/``-shm``/``-journal`` (корень-файл не
        накрывает соседей). Каждый путь — в двух формах, ``abspath`` и
        ``realpath``: проверка защиты не раскрывает симлинки, и путь через
        симлинк иначе прошёл бы мимо.
        """
        paths = []
        for spec in self.thread_stores.values():
            paths += (
                [spec.path] if spec.kind == "file" else _db_files(spec.path)
            )
        forms = []
        for path in paths:
            for form in (os.path.abspath(path), os.path.realpath(path)):
                if form not in forms:
                    forms.append(form)
        return forms

    @classmethod
    def load(cls, path):
        # `${ENV:…}`/`${FILE:…}` — строго, как у core-конфига на старте:
        # незаданная обязательная переменная роняет запуск воркера списком
        # (файл + ключ), а не подставляет пустую строку в адрес или токен.
        # Необязательное значение — с умолчанием: `${ENV:VAR:-}`.
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            # Служебное состояние по умолчанию — рядом с файлом конфига.
            raw.setdefault("state_dir", str(Path(path).absolute().parent))
        return cls.model_validate(
            resolve_env_placeholders_strict(raw, source=str(path))
        )


def _db_files(path):
    return [
        path,
        *(f"{path}{suffix}" for suffix in ("-wal", "-shm", "-journal")),
    ]


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


_NO_EDITS_RECORDING = AgentEditsRecordConfig(
    tui=False, cli=False, server=False
)


def _checked_workspace(name, profile, cwd):
    """Рабочая папка профиля: существующая абсолютная и равная cwd воркера."""
    workspace = Path(profile.workspace)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError(
            f"Profile {name}: workspace must be an existing absolute directory"
        )
    if workspace.resolve() != cwd:
        raise ValueError(
            f"Profile {name}: start worker in workspace {workspace}; "
            f"current cwd is {cwd}. The worker never calls chdir."
        )
    return workspace


def _with_stop_tool(settings, tool):
    """Настройки с ещё одним выключенным инструментом в ``stop_tools``."""
    return settings.model_copy(
        update={
            "agent": settings.agent.model_copy(
                update={"stop_tools": [*settings.agent.stop_tools, tool]}
            )
        }
    )


def _with_turn_persist(settings, profile):
    if "thread_store" in profile.core.model_fields_set:
        return settings
    # В Temporal по умолчанию тред пишется по ходам: шаги хода
    # защищает история workflow. Явная настройка профиля главнее.
    # Точечно, без пересборки путей: профиль хранит пути как
    # задано, ядро резолвит их от workspace при использовании.
    return settings.model_copy(
        update={
            "thread_store": settings.thread_store.model_copy(
                update={"persist": "turn"}
            )
        }
    )


def _with_protected_state(settings, state_paths):
    # Служебное состояние воркера агент не трогает, где бы оно ни
    # лежало: история всех сессий и payload — не его файлы.
    inside = list(state_paths)
    protected = settings.security.protected_paths
    if not inside and not any(str(p).startswith("~") for p in protected.extra):
        return settings
    # ``~`` — здесь, на воркере: guard команд в коде workflow не
    # может обращаться к os.path.
    expanded = [os.path.expanduser(p) for p in protected.extra]
    return settings.model_copy(
        update={
            "security": settings.security.model_copy(
                update={
                    "protected_paths": protected.model_copy(
                        update={"extra": [*expanded, *inside]}
                    )
                }
            )
        }
    )


def _with_thread_images(settings, name, profile, store):
    # Картинки треда — в хранилище тредов (файлы треда) или рядом с
    # файловыми тредами. Хранилище без того и другого положило бы их
    # на диск воркера мимо треда: инструмент выключается.
    if (
        thread_files_capability(store) is not None
        or isinstance(getattr(store, "threads_dir", None), str)
        or FS_VIEW_IMAGE_NAME in settings.agent.stop_tools
    ):
        return settings
    logger.warning(
        "Profile %s: store %s keeps no thread files; %s disabled",
        name,
        profile.store_id,
        FS_VIEW_IMAGE_NAME,
    )
    return _with_stop_tool(settings, FS_VIEW_IMAGE_NAME)


def _unless_explicit(profile, path, update):
    """``update``, если профиль не задал ``path`` явно (иначе — ничего)."""
    return {} if is_explicit(profile.core, path) else update


def _with_durable_logging(settings, profile):
    # Журнал правок агента (agent_edits) в durable-исполнении не
    # ведётся при любом профиле: его читают только дифф, сводка хода
    # и откат в TUI, а на воркере он стоил бы снимков файлов и
    # `git status` вокруг каждой команды.
    return settings.model_copy(
        update={
            "agent_edits": settings.agent_edits.model_copy(
                update={"record": _NO_EDITS_RECORDING}
            ),
            # Консольная трасса печатается из кода workflow: при
            # replay вывод повторялся бы. Ход виден в истории и логах.
            # Лог прогона по умолчанию — не файлом на диске воркера, а
            # в logging процесса (propagate) и в обработчики
            # приложения; явное значение профиля побеждает.
            "logging": settings.logging.model_copy(
                update={
                    "console_trace": False,
                    **_unless_explicit(
                        profile, "logging.save_logs", {"save_logs": False}
                    ),
                    **_unless_explicit(
                        profile, "logging.propagate", {"propagate": True}
                    ),
                    # Лог прогона уходит в общий logging процесса
                    # (сборщик логов) — секреты в нём маскируются,
                    # если профиль не решил иначе явно.
                    **_unless_explicit(
                        profile,
                        "logging.redact_secrets.enabled",
                        {
                            "redact_secrets": (
                                settings.logging.redact_secrets.model_copy(
                                    update={"enabled": True}
                                )
                            )
                        },
                    ),
                }
            ),
        }
    )


def _worker_runtime(profile, workspace, *, human_input):
    """Рантайм профиля под воркером: каталог старта — workspace; без
    человека (``human_input: false``) канал объявляет «человека нет», и
    вопрос подтверждения — «спросить некого», а не обрыв ожидания."""
    runtime = replace(
        profile.runtime or RuntimeEnv(),
        launch_root=str(workspace.resolve()),
    )
    if human_input:
        return runtime
    return replace(runtime, human_channel=False)


def _worker_settings(name, profile, store, *, state_paths, human_input):
    """Настройки профиля под воркером Temporal (поверх ``profile.core``)."""
    settings = _with_turn_persist(profile.core, profile)
    settings = _with_protected_state(settings, state_paths)
    settings = _with_thread_images(settings, name, profile, store)
    if not human_input:
        settings = _without_human(settings)
    # Project memory — файл в рабочей папке, общий для всех сессий
    # проекта: возможность TUI и CLI. Под Temporal её нет при любом
    # профиле — ни инструмента, ни статуса в промпте, ни activity.
    if PROJECT_MEMORY_TOOL_NAME not in settings.agent.stop_tools:
        settings = _with_stop_tool(settings, PROJECT_MEMORY_TOOL_NAME)
    return _with_durable_logging(settings, profile)


class ContextProvider:
    """Build the actual SDK context, never a reduced adapter-specific agent.

    Local backend relative paths currently follow process cwd. A worker must
    therefore start in its declared workspace; this provider never changes cwd.
    Workflow знает профиль только по имени, как pydantic-ai — модель по ключу
    реестра: activity берёт текущий профиль процесса. Смена настроек или
    ротация токена — перезапуск воркера; живые сессии продолжают работать.
    """

    def __init__(
        self,
        *,
        profiles,
        stores,
        resource_host,
        state_paths=(),
        log_handlers=(),
        human_input=False,
        agent_store=None,
    ):
        self.resource_host: WorkerResourceHost = resource_host
        self.stores = dict(stores)
        #: Хранилище агента (промпты, AGENTS.md, скиллы, субагенты): одно на
        #: все профили или по профилю; ``None`` — файловое по умолчанию.
        self.agent_stores = _agent_stores(agent_store, profiles)
        #: Обработчики логов прогона от приложения (``AgentPlugin``).
        self.log_handlers = list(log_handlers or ())
        #: Человек отвечает на вопросы агента (``WorkerConfig.human_input``).
        self.human_input = bool(human_input)
        self._profiles = {}
        #: Профиль → отпечаток его настроек (``settings_fingerprint``).
        self._fingerprints = {}
        self._contexts = {}
        self._refs = {}
        self._locks = {}
        self._parents = {}
        #: Привязка владельца к профилю и хранилищу (дайджест): владелец не
        #: меняет их посреди жизни контекста на этом воркере.
        self._bound = {}
        #: Последнее обращение к контексту владельца (``time.monotonic``):
        #: по нему обслуживание воркера находит брошенные контексты.
        self._last_used = {}
        cwd = Path.cwd().resolve()
        for name, profile in profiles.items():
            workspace = _checked_workspace(name, profile, cwd)
            if profile.store_id not in stores:
                raise ValueError(
                    f"Profile {name}: unknown store {profile.store_id}"
                )
            settings = _worker_settings(
                name,
                profile,
                stores[profile.store_id],
                state_paths=state_paths,
                human_input=human_input,
            )
            loaded = LoadedSettings(
                settings=settings,
                runtime=_worker_runtime(
                    profile, workspace, human_input=human_input
                ),
            )
            self._profiles[name] = (profile.store_id, loaded)
            # Отпечаток — исходного профиля, до правок воркера выше: их у
            # приложения нет, и они не должны давать расхождение.
            self._fingerprints[name] = settings_fingerprint(profile.core)
            # Снимок для истории проверяется на старте воркера, а не на
            # первом ходе: неизвестный параметр модели (extra_body, …) —
            # отказ сразу, с путём.
            # Полный круг: снимок собирается обратно в настройки так же, как
            # в коде workflow, — иначе сбой проявился бы только там (вечный
            # сбой задачи вместо ошибки старта).
            snapshot = public_config_snapshot(loaded)
            settings_from_snapshot(snapshot.settings, snapshot.runtime)
            kind = settings.os_backend.kind
            if kind in _UNTESTED_BACKENDS:
                logger.warning(
                    "Профиль %s: os_backend.kind=%s под воркером Temporal не "
                    "проверялся (проверены local и harbor)",
                    name,
                    kind,
                )

    def check_settings(self, profile, expected):
        """Сверить отпечаток настроек приложения с профилем воркера.

        Ход идёт по профилю воркера: разошедшиеся настройки — отказ хода с
        перечнем секций, а не агент, который тихо ведёт себя иначе, чем
        приложение ожидает.
        """
        if not expected:
            return
        mismatch = fingerprint_mismatch(expected, self._fingerprints[profile])
        if mismatch:
            raise ExecutionFailure(
                f"Настройки приложения и профиля воркера {profile!r} различаются "
                f"в секциях: {', '.join(mismatch)}. Ход идёт по профилю "
                "воркера: загрузите в приложении те же настройки или "
                "перезапустите воркер с новыми"
            )

    async def _resolve(self, call):
        if call.schema_version != 1 or not call.owner_id:
            raise ValueError("Invalid context request")
        store_id, loaded = self._profiles[call.profile]
        if "store_id" in call.data and call.data["store_id"] != store_id:
            raise ValueError(
                "Requested thread store differs from pinned profile"
            )
        digest = _digest([call.profile, store_id, call.child_context])
        bound = self._bound.setdefault(call.owner_id, digest)
        if bound != digest:
            raise ValueError(
                "Context owner is bound to another profile or store"
            )
        return store_id, loaded

    async def __call__(self, call):
        store_id, loaded = await self._resolve(call)
        self._last_used[call.owner_id] = time.monotonic()
        live = bool(loaded.settings.mcp.servers)
        lock = self._locks.setdefault(call.owner_id, asyncio.Lock())
        async with lock:
            if call.owner_id in self._contexts and (
                not live or call.owner_id in self._refs
            ):
                return self._contexts[call.owner_id]
            if call.child_context is not None:
                return await self._child_context(call)

            async def bootstrap():
                ctx = await acreate_run_context(
                    loaded,
                    auth_connector=build_auth_connector(loaded),
                    run_id=_digest(call.owner_id)[:24],
                    thread_store=self.stores[store_id],
                    log_handlers=self.log_handlers,
                    agent_store=self.agent_stores.get(call.profile),
                )
                self._contexts[call.owner_id] = ctx
                return ctx

            # Only MCP discovery eagerly owns a task. Ordinary FS/model work
            # remains reconstructible; background commands acquire one lazily.
            if live:
                self._refs[call.owner_id] = await self.resource_host.open(
                    call.owner_id,
                    loaded.runtime.launch_root,
                    bootstrap,
                )
            else:
                await bootstrap()
            return self._contexts[call.owner_id]

    def _parent_call(self, call):
        reference = call.child_context
        if reference["parent_owner_id"] == call.owner_id:
            raise ValueError("Child context cannot own itself")
        return replace(
            call,
            owner_id=reference["parent_owner_id"],
            child_context=reference.get("parent_context"),
            data={},
        )

    async def _child_context(self, call):
        from gc_core_temporalio.child_preparation import (
            prepare_child_context,
        )

        parent_call = self._parent_call(call)
        parent_ctx = await self(parent_call)
        ref = call.resource_owner or self._refs.get(parent_call.owner_id)

        async def prepare(_owner_context):
            return await prepare_child_context(call, parent_ctx)

        if ref is not None:
            ctx = await self.resource_host.prepare_context(
                ref, call.owner_id, prepare
            )
            self._refs[call.owner_id] = ref
        else:
            ctx = await prepare(parent_ctx)
        self._contexts[call.owner_id] = ctx
        self._parents[call.owner_id] = parent_call.owner_id
        return ctx

    async def resource_owner(self, call):
        await self(call)
        return self._refs.get(call.owner_id)

    async def ensure_resource_owner(self, call):
        ctx = await self(call)
        if call.child_context is not None:
            parent_call = self._parent_call(call)
            ref = await self.ensure_resource_owner(parent_call)

            async def prepare(_parent_context):
                return ctx

            await self.resource_host.prepare_context(
                ref, call.owner_id, prepare
            )
            self._refs[call.owner_id] = ref
            return ref
        lock = self._locks[call.owner_id]
        async with lock:

            async def bootstrap():
                return ctx

            ref = await self.resource_host.open(
                call.owner_id,
                ctx.workspace.root,
                bootstrap,
            )
            self._refs[call.owner_id] = ref
            return ref

    async def close(self, call):
        await self._resolve(call)
        ref = self._refs.get(call.owner_id)
        if ref is not None and call.child_context is None:
            await self.resource_host.close(ref)
        # Executor accounting and poststeps still use the same context after
        # live-resource teardown. Keep it available without reopening resources.

    def _drop_context(self, owner_id):
        from gc_core.utils.logging_module import release_run_logger

        for child, parent in list(self._parents.items()):
            if parent == owner_id:
                self._drop_context(child)
        ctx = self._contexts.pop(owner_id, None)
        self._last_used.pop(owner_id, None)
        self._bound.pop(owner_id, None)
        self._refs.pop(owner_id, None)
        self._locks.pop(owner_id, None)
        self._parents.pop(owner_id, None)
        if ctx is not None:
            release_run_logger(ctx.logger)

    async def release(self, call):
        """Release worker caches only after the executor's accounting/poststeps."""
        await self._resolve(call)
        ref = self._refs.get(call.owner_id)
        if ref is not None and call.child_context is None:
            await self.resource_host.close(ref)
            self.resource_host.forget(ref)
        self._drop_context(call.owner_id)

    def idle_owners(self, idle_seconds):
        """Владельцы верхнего уровня без обращений дольше ``idle_seconds``."""
        now = time.monotonic()
        return [
            owner
            for owner in self._contexts
            if owner not in self._parents
            and now - self._last_used.get(owner, now) >= idle_seconds
        ]

    async def forget_owner(self, owner_id):
        """Освободить контекст владельца, чей workflow уже не исполняется
        (terminate посреди хода): release из кода workflow не придёт."""
        ref = self._refs.get(owner_id)
        if ref is not None:
            await self.resource_host.close(ref)
            self.resource_host.forget(ref)
        self._drop_context(owner_id)

    async def aclose(self):
        refs = list(dict.fromkeys(self._refs.values()))
        for ref in refs:
            await self.resource_host.close(ref)
            self.resource_host.forget(ref)
        for owner_id in list(self._contexts):
            self._drop_context(owner_id)

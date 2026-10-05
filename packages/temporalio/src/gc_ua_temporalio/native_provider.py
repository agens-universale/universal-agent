"""Worker-only context preparation for pinned native deployment profiles."""

import asyncio
import hashlib
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

from gc_core.config_modules import AppConfig
from gc_core.config_modules.agent_edits import AgentEditsRecordConfig
from gc_core.config_modules.auth import build_auth_connector
from gc_core.config_modules.env_interpolation import (
    resolve_env_placeholders_strict,
)
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gc_ua_temporalio.payload_storage import PayloadStorageConfig


class NativeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: str
    store_id: str = "files"
    preview_stream: bool = False
    core: AppConfig = Field(default_factory=AppConfig)


class NativeStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["file", "sqlite"] = "file"
    path: str


class NativeWorkerConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )
    address: str = "localhost:7233"
    namespace: str = "default"
    workflow_queue: str = "ua-native-workflow"
    resource_queue: str = "ua-native-resource-local"
    model_queue: str = "ua-native-model"
    worker_id: str = "native-local"
    thread_stores: dict[str, NativeStoreConfig] = Field(
        default_factory=lambda: {"files": NativeStoreConfig(path="threads")}
    )
    receipts_database: str
    profiles: dict[str, NativeProfile]
    #: Крупные payload истории Temporal — во внешнее хранилище (см. payload_storage).
    #: По умолчанию — каталог ``payloads`` рядом с ``receipts_database``: без него
    #: большой вывод тула или длинный ход упираются в лимиты Temporal (2 МБ на
    #: payload, 50 МБ на историю). Явный ``null`` выключает хранилище.
    payload_storage: PayloadStorageConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _state_defaults(cls, data):
        """Служебное состояние по умолчанию — рядом с ``receipts_database``.

        Не в cwd: воркер стартует в workspace агента, и треды по умолчанию
        оказались бы в папке, которую агент читает и правит.
        """
        if not isinstance(data, dict) or not data.get("receipts_database"):
            return data
        root = Path(data["receipts_database"]).absolute().parent
        data = dict(data)
        data.setdefault(
            "payload_storage", {"kind": "file", "path": str(root / "payloads")}
        )
        data.setdefault(
            "thread_stores",
            {"files": {"kind": "file", "path": str(root / "threads")}},
        )
        return data

    def state_paths(self):
        """Служебные пути воркера для ``protected_paths``: треды, квитанции,
        payload.

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
        paths += _db_files(self.receipts_database)
        if self.payload_storage is not None and self.payload_storage.path:
            paths.append(self.payload_storage.path)
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


class NativeContextProvider:
    """Build the actual SDK context, never a reduced adapter-specific agent.

    Local backend relative paths currently follow process cwd. A worker must
    therefore start in its declared workspace; this provider never changes cwd.
    Workflow знает профиль только по имени, как pydantic-ai — модель по ключу
    реестра: activity берёт текущий профиль процесса. Смена настроек или
    ротация токена — перезапуск воркера; живые сессии продолжают работать.
    """

    def __init__(
        self, *, profiles, stores, receipts, resource_host, state_paths=()
    ):
        self.resource_host: WorkerResourceHost = resource_host
        self.stores = dict(stores)
        self.receipts = receipts
        self._profiles = {}
        self._contexts = {}
        self._refs = {}
        self._locks = {}
        self._parents = {}
        #: Привязки владельца уже записаны этим процессом: повторная запись —
        #: синхронная SQLite-транзакция на event loop на каждую activity.
        self._bound = set()
        #: Последнее обращение к контексту владельца (``time.monotonic``):
        #: по нему обслуживание воркера находит брошенные контексты.
        self._last_used = {}
        cwd = Path.cwd().resolve()
        for name, profile in profiles.items():
            workspace = Path(profile.workspace)
            if not workspace.is_absolute() or not workspace.is_dir():
                raise ValueError(
                    f"Profile {name}: workspace must be an existing absolute directory"
                )
            if workspace.resolve() != cwd:
                raise ValueError(
                    f"Profile {name}: start worker in workspace {workspace}; "
                    f"current cwd is {cwd}. Native worker never calls chdir."
                )
            if profile.store_id not in stores:
                raise ValueError(
                    f"Profile {name}: unknown store {profile.store_id}"
                )
            settings = profile.core
            if "thread_store" not in profile.core.model_fields_set:
                # В Temporal по умолчанию тред пишется по ходам: шаги хода
                # защищает история workflow. Явная настройка профиля главнее.
                # Точечно, без пересборки путей: профиль хранит пути как
                # задано, ядро резолвит их от workspace при использовании.
                settings = settings.model_copy(
                    update={
                        "thread_store": settings.thread_store.model_copy(
                            update={"persist": "turn"}
                        )
                    }
                )
            # Служебное состояние воркера агент не трогает, где бы оно ни
            # лежало: история всех сессий, квитанции и payload — не его файлы.
            inside = list(state_paths)
            protected = settings.security.protected_paths
            if inside or any(str(p).startswith("~") for p in protected.extra):
                # ``~`` — здесь, на воркере: guard команд в коде workflow не
                # может обращаться к os.path.
                expanded = [os.path.expanduser(p) for p in protected.extra]
                settings = settings.model_copy(
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
            # Журнал правок агента (agent_edits) в durable-исполнении не
            # ведётся при любом профиле: его читают только дифф, сводка хода
            # и откат в TUI, а на воркере он стоил бы снимков файлов и
            # `git status` вокруг каждой команды.
            settings = settings.model_copy(
                update={
                    "agent_edits": settings.agent_edits.model_copy(
                        update={"record": _NO_EDITS_RECORDING}
                    ),
                    # Консольная трасса печатается из кода workflow: при
                    # replay вывод повторялся бы. Ход виден в истории и логах.
                    "logging": settings.logging.model_copy(
                        update={"console_trace": False}
                    ),
                }
            )
            loaded = LoadedSettings(
                settings=settings,
                runtime=RuntimeEnv(launch_root=str(workspace.resolve())),
            )
            self._profiles[name] = (profile.store_id, loaded)

    async def _resolve(self, call):
        if call.schema_version != 1 or not call.owner_id:
            raise ValueError("Invalid native context request")
        store_id, loaded = self._profiles[call.profile]
        if "store_id" in call.data and call.data["store_id"] != store_id:
            raise ValueError(
                "Requested thread store differs from pinned profile"
            )
        digest = _digest([call.profile, store_id, call.child_context])
        if (call.owner_id, digest) not in self._bound:
            await asyncio.to_thread(
                self.receipts.save_operation_result,
                call.owner_id,
                "__native_context_binding__",
                digest,
                {"profile": call.profile, "store_id": store_id},
            )
            self._bound.add((call.owner_id, digest))
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
        from gc_ua_temporalio.native_child_preparation import (
            prepare_native_child_context,
        )

        parent_call = self._parent_call(call)
        parent_ctx = await self(parent_call)
        ref = call.resource_owner or self._refs.get(parent_call.owner_id)

        async def prepare(_owner_context):
            return await prepare_native_child_context(call, parent_ctx)

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
        self._bound = {item for item in self._bound if item[0] != owner_id}
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

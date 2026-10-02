"""Worker-only context preparation for pinned native deployment profiles."""

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

from gc_core.config import flat_config
from gc_core.config_modules import AppConfig
from gc_core.config_modules.auth import build_auth_connector
from gc_core.config_modules.env_interpolation import (
    resolve_env_placeholders_strict,
)
from gc_core.config_modules.field_meta import REVEAL_SECRETS
from gc_core.execution.resource_host import WorkerResourceHost
from gc_core.run_context import acreate_run_context
from gc_core.settings import LoadedSettings, RuntimeEnv
from pydantic import BaseModel, ConfigDict, Field

from gc_ua_temporalio.payload_storage import PayloadStorageConfig


class NativeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = Field(min_length=1)
    workspace: str
    store_id: str = "files"
    preview_stream: bool = False
    core: AppConfig = Field(default_factory=AppConfig)


class NativeStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["file", "sqlite"] = "file"
    path: str


class NativeWorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
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
    payload_storage: PayloadStorageConfig | None = None

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


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class NativeContextProvider:
    """Build the actual SDK context, never a reduced adapter-specific agent.

    Local backend relative paths currently follow process cwd. A worker must
    therefore start in its declared workspace; this provider never changes cwd.
    Profiles are pinned durably by name/revision and their complete config hash.
    """

    def __init__(self, *, profiles, stores, receipts, resource_host):
        self.resource_host: WorkerResourceHost = resource_host
        self.stores = dict(stores)
        self.receipts = receipts
        self._profiles = {}
        self._contexts = {}
        self._refs = {}
        self._locks = {}
        self._parents = {}
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
            loaded = LoadedSettings(
                settings=settings,
                runtime=RuntimeEnv(launch_root=str(workspace.resolve())),
            )
            self._profiles[name] = (profile.revision, profile.store_id, loaded)
            identity = _digest([name, profile.revision])
            receipts.save_operation_result(
                "__native_profiles__",
                identity,
                # Дайджест — по сырым секретам: смена токена при той же
                # ревизии обязана ловиться, а json-дамп SecretStr их маскирует.
                _digest(
                    profile.model_dump(mode="json", context=REVEAL_SECRETS)
                ),
                {"profile": name, "revision": profile.revision},
            )

    def _resolve(self, call):
        if call.schema_version != 1 or not call.owner_id:
            raise ValueError("Invalid native context request")
        revision, store_id, loaded = self._profiles[call.profile]
        if call.revision != revision:
            raise ValueError(f"Profile revision mismatch: {call.profile}")
        if "store_id" in call.data and call.data["store_id"] != store_id:
            raise ValueError(
                "Requested thread store differs from pinned profile"
            )
        self.receipts.save_operation_result(
            call.owner_id,
            "__native_context_binding__",
            _digest([call.profile, revision, store_id, call.child_context]),
            {
                "profile": call.profile,
                "revision": revision,
                "store_id": store_id,
            },
        )
        return store_id, loaded

    async def __call__(self, call):
        store_id, loaded = self._resolve(call)
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
                    # build_auth_connector пока читает плоский вид.
                    auth_connector=build_auth_connector(flat_config(loaded)),
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
        self._resolve(call)
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
        self._refs.pop(owner_id, None)
        self._locks.pop(owner_id, None)
        self._parents.pop(owner_id, None)
        if ctx is not None:
            release_run_logger(ctx.logger)

    async def release(self, call):
        """Release worker caches only after the executor's accounting/poststeps."""
        self._resolve(call)
        ref = self._refs.get(call.owner_id)
        if ref is not None and call.child_context is None:
            await self.resource_host.close(ref)
            self.resource_host.forget(ref)
        self._drop_context(call.owner_id)

    async def aclose(self):
        refs = list(dict.fromkeys(self._refs.values()))
        for ref in refs:
            await self.resource_host.close(ref)
            self.resource_host.forget(ref)
        for owner_id in list(self._contexts):
            self._drop_context(owner_id)

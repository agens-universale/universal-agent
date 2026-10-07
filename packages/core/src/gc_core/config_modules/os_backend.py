"""Секция `os_backend:` — где живут файлы и команды тулов.

``kind`` выбирает бэкенд, подсекция с тем же именем — его параметры. Голая
строка (``os_backend: harbor``) — сокращение для ``{kind: harbor}``: так
пишет Harbor-адаптер и старые конфиги.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gc_core.config_modules.field_meta import gc_meta

OsBackendKind = Literal["local", "harbor", "docker", "ssh", "exec", "sandbox"]


class DockerBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = (
        None  # имя или ID контейнера — обязателен при kind=docker
    )
    user: str | None = None  # docker exec -u
    binary: str = "docker"  # podman — сюда или через kind=exec


class SshBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str | None = None  # обязателен при kind=ssh
    port: int = 22
    user: str | None = None  # None → как в ~/.ssh/config
    key_path: str | None = None  # None → ssh-agent / IdentityFile
    options: list[str] = Field(default_factory=list)  # дополнительные -o
    control_master: bool = True  # один TCP на ран; выключается на Windows


class ExecBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv_prefix: list[str] = Field(
        default_factory=list
    )  # обязателен при kind=exec


class SandboxBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # None → изолируем текущий launch_root; "auto" → каталог на папку запуска.
    workdir: Literal["auto"] | None = None
    network: bool = True
    extra_ro: list[str] = Field(default_factory=list)


class OsBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OsBackendKind = "local"
    cwd: str | None = None  # стартовый каталог «там» (не для sandbox)
    # Переменные окружения команд «там»: могут быть токены — секрет,
    # в публичный снимок (историю Temporal) не идут.
    env: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra=gc_meta(secret=True, worker_only=True),
    )
    # Где качает `exec_fetch_url`: на хосте (agent) или через бэкенд
    # (os_backend) — когда сеть есть только у среды исполнения. Разбор
    # страницы всегда на хосте. Для local/sandbox значение игнорируется.
    fetch_url_execute_on: Literal["agent", "os_backend"] = "agent"
    # Копировать скиллы (system + user + project) в среду исполнения на
    # старте рана: реестр читает их через бэкенд, а лежат они на хосте.
    # Только для remote-kind (docker/ssh/exec); Harbor-адаптер грузит сам.
    provision_skills: bool = True
    # Параметры подключения (хост, пользователь, ключ, префикс команды — в
    # нём бывают токены) нужны только процессу, который исполняет команды:
    # в публичный снимок (историю Temporal) не идут.
    docker: DockerBackendConfig = Field(
        default_factory=DockerBackendConfig,
        json_schema_extra=gc_meta(worker_only=True),
    )
    ssh: SshBackendConfig = Field(
        default_factory=SshBackendConfig,
        json_schema_extra=gc_meta(worker_only=True),
    )
    exec: ExecBackendConfig = Field(
        default_factory=ExecBackendConfig,
        json_schema_extra=gc_meta(worker_only=True),
    )
    sandbox: SandboxBackendConfig = Field(default_factory=SandboxBackendConfig)

    @model_validator(mode="before")
    @classmethod
    def _string_shorthand(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"kind": value.strip().lower()}
        return value

    @model_validator(mode="after")
    def _required_per_kind(self) -> OsBackendConfig:
        if self.kind == "docker" and not self.docker.name:
            raise ValueError(
                "os_backend.docker.name обязателен при kind=docker"
            )
        if self.kind == "ssh" and not self.ssh.host:
            raise ValueError("os_backend.ssh.host обязателен при kind=ssh")
        # Значение с ведущим `-` уходит в argv отдельным словом и читается
        # клиентом как опция (`-oProxyCommand=…`, `--privileged`).
        for label, value in (
            ("ssh.host", self.ssh.host),
            ("ssh.user", self.ssh.user),
            ("docker.name", self.docker.name),
            ("docker.user", self.docker.user),
            ("docker.binary", self.docker.binary),
        ):
            if value and str(value).startswith("-"):
                raise ValueError(
                    f"os_backend.{label} не может начинаться с '-': {value!r}"
                )
        if self.kind == "exec" and not self.exec.argv_prefix:
            raise ValueError(
                "os_backend.exec.argv_prefix обязателен при kind=exec"
            )
        return self

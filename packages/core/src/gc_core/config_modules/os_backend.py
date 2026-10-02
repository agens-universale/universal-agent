"""Секция `os_backend:` — где живут файлы и команды тулов.

``kind`` выбирает бэкенд, подсекция с тем же именем — его параметры. Голая
строка (``os_backend: harbor``) — сокращение для ``{kind: harbor}``: так
пишет Harbor-адаптер и старые конфиги.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # None → изолируем текущий launch_root; "auto" → свежий каталог на ран.
    workdir: Literal["auto"] | None = None
    network: bool = True
    extra_ro: list[str] = Field(default_factory=list)


class OsBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: OsBackendKind = "local"
    cwd: str | None = None  # стартовый каталог «там» (не для sandbox)
    env: dict[str, str] = Field(default_factory=dict)
    docker: DockerBackendConfig = Field(default_factory=DockerBackendConfig)
    ssh: SshBackendConfig = Field(default_factory=SshBackendConfig)
    exec: ExecBackendConfig = Field(default_factory=ExecBackendConfig)
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
        if self.kind == "exec" and not self.exec.argv_prefix:
            raise ValueError(
                "os_backend.exec.argv_prefix обязателен при kind=exec"
            )
        return self

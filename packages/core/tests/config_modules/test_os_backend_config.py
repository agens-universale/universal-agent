# packages/core/tests/config_modules/test_os_backend_config.py
"""Секция `os_backend:` — схема, строковое сокращение, обязательные поля по kind."""

import pytest
from pydantic import ValidationError

from gc_core.config import flatten_app_config
from gc_core.config_modules.os_backend import OsBackendConfig
from gc_core.config_modules.root import AppConfig


def test_defaults_are_local():
    cfg = OsBackendConfig()
    assert cfg.kind == "local"
    assert cfg.cwd is None
    assert cfg.env == {}
    assert cfg.sandbox.network is True
    assert cfg.sandbox.workdir is None


def test_bare_string_is_shorthand_for_kind():
    assert OsBackendConfig.model_validate("harbor").kind == "harbor"
    assert OsBackendConfig.model_validate("  LOCAL ").kind == "local"


def test_rejects_unknown_kind_and_keys():
    with pytest.raises(ValidationError):
        OsBackendConfig(kind="s3")
    with pytest.raises(ValidationError):
        OsBackendConfig(bogus=1)


@pytest.mark.parametrize(
    "payload, missing",
    [
        ({"kind": "docker"}, "docker.name"),
        ({"kind": "ssh"}, "ssh.host"),
        ({"kind": "exec"}, "exec.argv_prefix"),
    ],
)
def test_required_fields_per_kind(payload, missing):
    with pytest.raises(ValidationError, match=missing):
        OsBackendConfig.model_validate(payload)


def test_docker_and_ssh_shapes():
    d = OsBackendConfig.model_validate(
        {"kind": "docker", "docker": {"name": "box"}}
    )
    assert (
        d.docker.name == "box"
        and d.docker.user is None
        and d.docker.binary == "docker"
    )
    s = OsBackendConfig.model_validate(
        {
            "kind": "ssh",
            "ssh": {"host": "h", "user": "u", "key_path": "~/.ssh/id"},
        }
    )
    assert (s.ssh.port, s.ssh.control_master, s.ssh.options) == (22, True, [])


def test_sandbox_workdir_only_auto_or_null():
    assert (
        OsBackendConfig.model_validate(
            {"kind": "sandbox", "sandbox": {"workdir": "auto"}}
        ).sandbox.workdir
        == "auto"
    )
    with pytest.raises(ValidationError):
        OsBackendConfig.model_validate(
            {"kind": "sandbox", "sandbox": {"workdir": "/tmp/x"}}
        )


def test_app_config_has_section_and_flatten_projects_dict():
    app = AppConfig.model_validate(
        {"os_backend": {"kind": "docker", "docker": {"name": "box"}}}
    )
    flat = flatten_app_config(app)
    assert flat["os_backend"]["kind"] == "docker"
    assert flat["os_backend"]["docker"]["name"] == "box"


def test_app_config_default_flattens_to_local():
    assert flatten_app_config(AppConfig())["os_backend"]["kind"] == "local"


def test_fetch_url_locus_and_skills_defaults():
    cfg = OsBackendConfig()
    assert cfg.fetch_url_execute_on == "agent"
    assert cfg.provision_skills is True
    assert (
        OsBackendConfig(fetch_url_execute_on="os_backend").fetch_url_execute_on
        == "os_backend"
    )
    with pytest.raises(ValidationError):
        OsBackendConfig(fetch_url_execute_on="container")

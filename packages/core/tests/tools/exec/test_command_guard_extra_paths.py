"""Какие «дополнительные» пути видит гвард команд.

``security.write_guard.extra_paths`` — каталоги, КУДА агенту можно писать
(demo_config: «writable directories помимо launch_root»); команда с ними идёт
обычной модерацией, а не автоматическим запретом. ``security.protected_paths.extra``
— пути, которые агенту трогать НЕЛЬЗЯ; команда с ними запрещается, как с
системными (до правки в гвард по ошибке попадал первый список, а второй — никуда).
"""

import json

from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_testkit.helpers.tool_ctx import local_ctx, with_local_ctx


def _ctx(tmp_path, **security):
    cfg = {
        "launch_root": str(tmp_path),
        "security.write_guard": {"enabled": False},
    }
    cfg.update(security)
    return local_ctx(cfg, is_tui=False, tui_confirm=None, logger=None)


def _run(tmp_path, monkeypatch, command, ctx):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    tool = with_local_ctx(ExecuteCommand)
    return json.loads(tool(command=command, ctx=ctx))


def test_write_extra_path_is_not_auto_denied(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    shared.mkdir()
    ctx = _ctx(
        tmp_path,
        **{
            "security.write_guard": {
                "enabled": True,
                "extra_paths": [str(shared)],
            }
        },
    )
    result = _run(tmp_path, monkeypatch, f"ls {shared}", ctx)
    # Команда дошла до запуска: безобидный ls по пустому каталогу.
    assert result["ok"] is True, result
    assert result["exit_code"] == 0


def test_protected_extra_path_is_denied(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    ctx = _ctx(
        tmp_path, **{"security.protected_paths": {"extra": [str(secrets)]}}
    )
    result = _run(tmp_path, monkeypatch, f"cat {secrets}/token", ctx)
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"


def test_protected_extra_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".corp").mkdir()
    ctx = _ctx(
        tmp_path, **{"security.protected_paths": {"extra": ["~/.corp"]}}
    )
    result = _run(tmp_path, monkeypatch, f"cat {tmp_path}/.corp/tokens", ctx)
    assert result["error"]["type"] == "protected_path_denied"

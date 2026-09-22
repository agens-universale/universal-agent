"""
Unit-тесты для path-policy слоя read_guard (Контур 1).

Структура аналогична существующим test_root_box_*: проверяем
`check_read_path_allowed` напрямую с разными конфигами.
"""
from __future__ import annotations

import os

import pytest
from gc_core.security.read_guard import (
    ReadGuardConfig,
    ReadGuardViolation,
    check_read_path_allowed,
    expand_path_placeholders,
    parse_read_guard_config,
)


@pytest.fixture
def lab(tmp_path):
    """launch_root для тестов — изолированный tmp."""
    return str(tmp_path)


@pytest.fixture
def cfg_default(lab):
    """
    Дефолтный конфиг с реалистичными denylist'ами как в read-guard-spec.md:
    проектные файлы readable; .gc/ заблокирован; skills/ внутри .gc — exception;
    `.env`, `*.pem` — паттерны.
    """
    return ReadGuardConfig(
        enabled=True,
        mode="deny",
        allowed_read_roots=[],  # = [launch_root]
        allowed_read_paths=["<launch_root>/.gc/skills"],
        denied_read_paths=[
            "~/.ssh",
            "<launch_root>/.gc",
            "/etc/ssl/private",
        ],
        denied_file_patterns=[".env", "*.pem", "*id_rsa*"],
        denied_extensions=[".key", ".pfx"],
    )


def test_inside_launch_root_allowed(lab, cfg_default):
    target = os.path.join(lab, "foo.txt")
    result = check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)
    # realpath на macOS может развернуть /tmp → /private/tmp, поэтому сравниваем
    # через realpath.
    assert result == os.path.realpath(target)


def test_relative_path_resolves_against_launch_root(lab, cfg_default):
    result = check_read_path_allowed("./sub/file.txt", launch_root=lab, cfg=cfg_default)
    assert result.endswith("/sub/file.txt")


def test_outside_launch_root_blocked(lab, cfg_default):
    with pytest.raises(ReadGuardViolation) as exc:
        check_read_path_allowed("/etc/passwd", launch_root=lab, cfg=cfg_default)
    assert exc.value.error["type"] == "read_guard_violation"
    assert "allowed_read_roots" in exc.value.error["matched_rule"]


def test_gc_dir_blocked_in_launch_root(lab, cfg_default):
    target = os.path.join(lab, ".gc", "threads", "x.jsonl")
    with pytest.raises(ReadGuardViolation) as exc:
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)
    assert ".gc" in exc.value.error["matched_rule"]


def test_gc_skills_exception_allowed(lab, cfg_default):
    target = os.path.join(lab, ".gc", "skills", "foo.md")
    result = check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)
    assert result.endswith("/.gc/skills/foo.md")


def test_dotenv_pattern_blocked(lab, cfg_default):
    target = os.path.join(lab, ".env")
    with pytest.raises(ReadGuardViolation) as exc:
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)
    assert exc.value.error["matched_rule"] == ".env"


def test_pem_pattern_blocked(lab, cfg_default):
    target = os.path.join(lab, "secret.pem")
    with pytest.raises(ReadGuardViolation):
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)


def test_id_rsa_pattern_blocked(lab, cfg_default):
    target = os.path.join(lab, "my_id_rsa_old.bak")
    with pytest.raises(ReadGuardViolation):
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)


def test_key_extension_blocked(lab, cfg_default):
    target = os.path.join(lab, "private.key")
    with pytest.raises(ReadGuardViolation) as exc:
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)
    assert exc.value.error["matched_rule"] == ".key"


def test_home_ssh_blocked(lab, cfg_default):
    home = os.path.expanduser("~")
    target = os.path.join(home, ".ssh", "id_rsa")
    with pytest.raises(ReadGuardViolation):
        check_read_path_allowed(target, launch_root=lab, cfg=cfg_default)


def test_wildcard_root_allow_by_default(lab):
    cfg = ReadGuardConfig(
        enabled=True, mode="deny",
        allowed_read_roots=["*"],
        denied_read_paths=["/etc/ssl/private"],
    )
    # Files outside launch_root теперь разрешены.
    result = check_read_path_allowed("/etc/hostname", launch_root=lab, cfg=cfg)
    assert "etc/hostname" in result


def test_wildcard_root_denied_still_blocks(lab):
    cfg = ReadGuardConfig(
        enabled=True, mode="deny",
        allowed_read_roots=["*"],
        denied_read_paths=["/etc/ssl/private"],
    )
    with pytest.raises(ReadGuardViolation):
        check_read_path_allowed("/etc/ssl/private/foo.key",
                                launch_root=lab, cfg=cfg)


def test_extra_allowed_read_root(lab, tmp_path):
    """Дополнительный allowed_read_root разрешает чтение за пределами launch_root."""
    sibling = tmp_path.parent / "other_proj"
    sibling.mkdir(exist_ok=True)
    cfg = ReadGuardConfig(
        enabled=True, mode="deny",
        allowed_read_roots=[str(sibling)],
    )
    result = check_read_path_allowed(
        str(sibling / "file.txt"), launch_root=lab, cfg=cfg,
    )
    assert "other_proj" in result


def test_empty_path_propagates_value_error(lab, cfg_default):
    with pytest.raises(ValueError):
        check_read_path_allowed("", launch_root=lab, cfg=cfg_default)


def test_expand_placeholder_launch_root(lab):
    out = expand_path_placeholders("<launch_root>/sub/dir", lab)
    assert out == os.path.realpath(os.path.join(lab, "sub", "dir"))


def test_expand_placeholder_tilde():
    out = expand_path_placeholders("~/.ssh", "/anything")
    assert out == os.path.realpath(os.path.expanduser("~/.ssh"))


def test_parse_disabled_when_no_command_moderation():
    """
    Если в config_dict отсутствует блок command_moderation, read_guard
    должен быть выключен (semantics same as root_box).
    """
    parsed = parse_read_guard_config({})
    assert parsed.enabled is False


def test_parse_default_disabled_when_read_guard_block_absent():
    """
    Safe-by-default-OFF: даже если command_moderation присутствует,
    read_guard выключен без явного top-level `read_guard.enabled: true`.
    """
    parsed = parse_read_guard_config({"command_moderation": {"mode": "deny"}})
    assert parsed.enabled is False

    parsed = parse_read_guard_config({"read_guard": {"mode": "deny"}})  # без enabled
    assert parsed.enabled is False

    parsed = parse_read_guard_config({"read_guard": {"enabled": True}})
    assert parsed.enabled is True


def test_parse_respects_explicit_config():
    parsed = parse_read_guard_config({
        "read_guard": {
            "enabled": False,
            "mode": "ask",
            "denied_read_paths": ["/secret"],
        },
    })
    assert parsed.enabled is False
    assert parsed.mode == "ask"
    assert parsed.denied_read_paths == ["/secret"]


def test_extra_allowed_paths_bypasses_denied_and_roots(tmp_path):
    """
    extra_allowed_paths (например, skills_root от _resolve_skills_root)
    должен пропускать путь насквозь: ни denied_*, ни allowed_read_roots
    не должны блокировать его.
    """
    lab = tmp_path / "lab"
    skills = tmp_path / "project_outside_lab" / "skills"
    skills.mkdir(parents=True)
    skill_file = skills / "foo.md"
    skill_file.write_text("x")

    cfg = ReadGuardConfig(
        enabled=True,
        mode="deny",
        allowed_read_roots=[str(lab)],  # skills вне lab — без override блокировались бы
        allowed_read_paths=[],
        denied_read_paths=[str(skills.parent)],  # явный deny на родителя
        denied_file_patterns=[],
        denied_extensions=[".md"],  # .md тоже блокировался бы denied_extensions
    )

    # Без extra_allowed — блокируется.
    with pytest.raises(ReadGuardViolation):
        check_read_path_allowed(
            str(skill_file), launch_root=str(lab), cfg=cfg,
        )

    # С extra_allowed — пропускается насквозь.
    resolved = check_read_path_allowed(
        str(skill_file),
        launch_root=str(lab),
        cfg=cfg,
        extra_allowed_paths=[str(skills)],
    )
    assert os.path.realpath(resolved) == os.path.realpath(str(skill_file))


def test_resolve_skills_root_default_and_custom(tmp_path):
    from gc_core.security.read_guard import _resolve_skills_root
    # Без skills_data_path → берёт <project_root>/skills.
    default = _resolve_skills_root({})
    assert default is not None
    assert default.endswith(os.sep + "skills")

    # С явным skills_data_path → возвращается он.
    custom = str(tmp_path / "my_skills")
    assert _resolve_skills_root({"skills_data_path": custom}) == os.path.abspath(custom)

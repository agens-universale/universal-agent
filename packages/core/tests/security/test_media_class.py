"""Класс `media` защищённых путей (спека изображений §11, §20.4).

Медиа — файлы, которые агент получает сам: копии вставленных изображений (корень
`ImageStore`), собственный карман процесса `_staging/<pid>` и картинки из ответов
MCP-тулов. Чтение — без вопроса (пол и периметр `read_guard`), запись тулами —
отказ, команда оболочки с таким путём — подтверждение (без головы — отказ). Чужой
карман `_staging/<другой pid>` в класс не входит — он operational, где бы ни лежал корень.

Раскладка по умолчанию: каталог тредов лежит под `<gc_state>`, то есть внутри
operational — поэтому «media раньше operational» здесь проверяется по-настоящему.
"""

import json
import os
import types
from pathlib import Path

import pytest

from gc_core.attachments.store import (
    IMAGES_DIRNAME,
    STAGING_DIRNAME,
    ImageStore,
)
from gc_core.config_modules import env_vars
from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME
from gc_core.security.command_guard import (
    enforce_command_guard,
    scan_command_for_protected,
)
from gc_core.security.media_roots import media_roots
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.protected_paths import (
    ProtectedSet,
    classify_path,
    protected_decision,
    protected_paths,
)
from gc_core.security.read_guard import enforce_read_guard

SHA16 = "3f9a1c27b0e4d5a6"


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Проект и каталог тредов по умолчанию — под `<gc_state>` (он operational)."""
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir()
    threads = tmp_path / "gc" / "projects" / "proj" / "threads"
    images = threads / IMAGES_DIRNAME
    return types.SimpleNamespace(
        proj=proj,
        threads=threads,
        image=images / f"cli_t1_{SHA16}.png",
        own_staged=images
        / STAGING_DIRNAME
        / str(os.getpid())
        / f"{SHA16}.png",
        foreign_staged=images
        / STAGING_DIRNAME
        / str(os.getpid() + 1)
        / f"{SHA16}.png",
        mcp_shot=tmp_path
        / "gc"
        / "cache"
        / "mcp-media"
        / "browser_take_screenshot_1.png",
        config={"launch_root": str(proj), "threads_data_path": str(threads)},
    )


class _ReadTool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_read_guard(path_arg="path")
    def read(self, path):
        return json.dumps({"ok": True})


class _WriteTool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_write_guard(path_arg="path")
    def write(self, path, content):
        return json.dumps({"ok": True})


class _ExecTool:
    def __init__(self, ctx):
        self._ctx = ctx

    @enforce_command_guard(payload_arg="command", tool_name="exec_command")
    def run(self, command):
        return json.dumps({"ok": True})

    def _command_moderation_blocked_result(self, err, *args, **kwargs):
        return json.dumps({"ok": False, "error": err})


def _tui_ctx(config, answer):
    """TUI-контекст с подставным баннером; `asked` — вопросы, которые он получил."""
    asked = []

    def tui_confirm(**kwargs):
        asked.append(kwargs["question"])
        return answer

    ctx = types.SimpleNamespace(
        config_dict=config, is_tui=True, tui_confirm=tui_confirm, logger=None
    )
    return ctx, asked


# --- корни ------------------------------------------------------------------


def test_media_roots_are_the_image_store_root_own_staging_and_mcp_media(
    layout,
):
    images = layout.threads / IMAGES_DIRNAME
    assert media_roots(layout.config) == (
        images,
        images / STAGING_DIRNAME / str(os.getpid()),
        layout.mcp_shot.parent,
    )


def test_media_roots_without_threads_dir_follow_the_temp_image_store(layout):
    store = ImageStore.for_threads_dir("")
    assert media_roots({})[:2] == (Path(store.root), Path(store.staging_dir))


def test_media_roots_touch_nothing_on_disk(layout):
    """Корни считаются на каждую проверку охраны — каталогов при этом не создаём."""
    media_roots(layout.config)
    assert not layout.threads.exists()


# --- классификация ----------------------------------------------------------


def test_protected_set_keeps_three_argument_construction():
    ps = ProtectedSet(
        instructions=frozenset(),
        operational=frozenset(),
        skills_exceptions=frozenset(),
    )
    assert ps.media == frozenset()


def test_media_is_classified_before_operational(layout):
    ps = protected_paths(
        cwd=str(layout.proj), media=media_roots(layout.config)
    )
    assert classify_path(layout.image, ps) == "media"
    assert classify_path(layout.own_staged, ps) == "media"
    assert classify_path(layout.mcp_shot, ps) == "media"
    assert classify_path(layout.foreign_staged, ps) == "operational"
    assert (
        classify_path(layout.threads / "data_cli_t1.jsonl", ps)
        == "operational"
    )


@pytest.fixture(params=["custom_threads_dir", "sdk_temp_root"])
def outside(request, tmp_path, monkeypatch):
    """Корень хранилища картинок вне `<gc_state>`: свой `threads_data_path` или
    временный корень SDK без каталога тредов. Operational его не накрывает."""
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    config = {"launch_root": str(proj)}
    if request.param == "custom_threads_dir":
        config["threads_data_path"] = str(tmp_path / "custom-threads")
        root = tmp_path / "custom-threads" / IMAGES_DIRNAME
    else:
        root = Path(ImageStore.for_threads_dir("").root)
    return types.SimpleNamespace(
        proj=proj,
        root=root,
        own_pocket=root / STAGING_DIRNAME / str(os.getpid()),
        foreign_staged=root
        / STAGING_DIRNAME
        / str(os.getpid() + 1)
        / f"{SHA16}.png",
        config=config,
    )


def test_foreign_staging_is_operational_wherever_the_root_lives(outside):
    """Чужой карман — вставки другого процесса: operational и вне `<gc_state>`."""
    ps = protected_paths(
        cwd=str(outside.proj), media=media_roots(outside.config)
    )
    assert classify_path(outside.foreign_staged, ps) == "operational"
    assert classify_path(outside.root / STAGING_DIRNAME, ps) == "operational"
    assert classify_path(outside.own_pocket / f"{SHA16}.png", ps) == "media"
    assert classify_path(outside.root / f"cli_t1_{SHA16}.png", ps) == "media"


def test_dot_dot_escapes_are_classified_by_where_they_land(layout, outside):
    """Класс — по нормализованному пути: `..` из своего кармана media не наследует."""
    own = f"{outside.own_pocket}"
    ps = protected_paths(
        cwd=str(outside.proj), media=media_roots(outside.config)
    )
    other = f"{own}{os.sep}..{os.sep}{os.getpid() + 1}{os.sep}x.png"
    assert classify_path(other, ps) == "operational"
    # Из кармана на три уровня вверх — каталог тредов (у временного корня — его
    # родитель). Вне `<gc_state>` он не защищён ничем, но и не media.
    up = f"{own}{os.sep}..{os.sep}..{os.sep}..{os.sep}data_cli_t1.jsonl"
    assert classify_path(up, ps) == "none"

    ps_default = protected_paths(
        cwd=str(layout.proj), media=media_roots(layout.config)
    )
    own_default = os.path.dirname(layout.own_staged)
    up_default = (
        f"{own_default}{os.sep}..{os.sep}..{os.sep}..{os.sep}data_cli_t1.jsonl"
    )
    assert os.path.abspath(up_default) == str(
        layout.threads / "data_cli_t1.jsonl"
    )
    assert classify_path(up_default, ps_default) == "operational"
    other_default = (
        f"{own_default}{os.sep}..{os.sep}{os.getpid() + 1}{os.sep}x.png"
    )
    assert classify_path(other_default, ps_default) == "operational"


def test_foreign_staging_outside_gc_state_is_denied_for_read_write_and_commands(
    outside,
):
    config = {
        **outside.config,
        "read_guard": {"enabled": False},
        "write_guard": {"enabled": False},
    }
    ctx = types.SimpleNamespace(
        config_dict=config, is_tui=False, tui_confirm=None, logger=None
    )
    read = json.loads(_ReadTool(ctx).read(path=str(outside.foreign_staged)))
    assert read["ok"] is False
    assert read["error"]["type"] == "protected_path_denied"
    write = json.loads(
        _WriteTool(ctx).write(path=str(outside.foreign_staged), content="x")
    )
    assert write["ok"] is False
    assert write["error"]["type"] == "protected_path_denied"
    media = media_roots(outside.config)
    assert (
        scan_command_for_protected(
            f"cat {outside.foreign_staged}", cwd=str(outside.proj), media=media
        )
        == "deny"
    )


def test_perimeter_does_not_allow_foreign_staging_through_the_media_root(
    outside,
):
    """Пол выключен (`protected_paths.enabled: false`) — остаётся периметр: корень
    хранилища в нём разрешён, но чужой карман под ним — нет, он спрашивается."""
    config = {
        **outside.config,
        "protected_paths": {"enabled": False},
        "read_guard": {"enabled": True, "mode": "ask"},
    }
    ctx, asked = _tui_ctx(config, "deny")
    assert (
        json.loads(
            _ReadTool(ctx).read(path=str(outside.root / f"cli_t1_{SHA16}.png"))
        )["ok"]
        is True
    )
    assert asked == []
    assert (
        json.loads(_ReadTool(ctx).read(path=str(outside.foreign_staged)))["ok"]
        is False
    )
    assert len(asked) == 1


def test_symlink_into_a_foreign_pocket_is_classified_by_its_own_path(
    tmp_path, monkeypatch
):
    """Фиксирует текущее поведение: класс считается по пути как написан (без
    realpath), поэтому ссылка из проекта в чужой карман для пола — `none`. Периметр
    `read_guard` ссылку разворачивает (realpath) и видит путь вне проекта, а
    разрешение корня хранилища на такой путь не распространяется — спрашивает."""
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir()
    threads = tmp_path / "custom-threads"
    foreign_pocket = (
        threads / IMAGES_DIRNAME / STAGING_DIRNAME / str(os.getpid() + 1)
    )
    foreign_pocket.mkdir(parents=True)
    (foreign_pocket / f"{SHA16}.png").write_bytes(b"png")
    link = proj / "peek"
    try:
        link.symlink_to(foreign_pocket, target_is_directory=True)
    except OSError:
        pytest.skip("symlink недоступен (Windows без прав)")
    config = {
        "launch_root": str(proj),
        "threads_data_path": str(threads),
        "read_guard": {"enabled": True, "mode": "ask"},
    }
    ps = protected_paths(cwd=str(proj), media=media_roots(config))
    assert classify_path(link / f"{SHA16}.png", ps) == "none"
    ctx, asked = _tui_ctx(config, "deny")
    assert (
        json.loads(_ReadTool(ctx).read(path=str(link / f"{SHA16}.png")))["ok"]
        is False
    )
    assert len(asked) == 1


def test_a_planted_temp_root_symlink_opens_nothing(tmp_path, monkeypatch):
    """SDK без каталога тредов: корень картинок — во временном каталоге системы. На
    общем /tmp другой пользователь заранее кладёт под предсказуемым именем ссылку на
    свои цели — охрана чтения не пускает агента по ней без вопроса."""
    if not hasattr(os, "getuid"):
        pytest.skip("на Windows временный каталог у пользователя свой")
    from gc_core.attachments import store

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "id_rsa").write_text("KEY", encoding="utf-8")
    shared_tmp = tmp_path / "tmp"
    shared_tmp.mkdir()
    planted = shared_tmp / f"gc-images-{os.getuid()}"
    planted.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(store.tempfile, "gettempdir", lambda: str(shared_tmp))
    config = {
        "launch_root": str(proj),
        "read_guard": {"enabled": True, "mode": "ask"},
    }
    ctx, asked = _tui_ctx(config, "deny")
    assert (
        json.loads(_ReadTool(ctx).read(path=str(planted / "id_rsa")))["ok"]
        is False
    )
    assert len(asked) == 1


def test_callers_without_config_keep_today_classes(layout):
    """Без корней (судья команд, diff-TUI) раскладка по умолчанию — как до класса."""
    assert (
        classify_path(layout.image, protected_paths(cwd=str(layout.proj)))
        == "operational"
    )
    assert (
        scan_command_for_protected(
            f"cp {layout.image} docs/", cwd=str(layout.proj)
        )
        == "deny"
    )


# --- чтение -----------------------------------------------------------------


def test_media_read_passes_the_floor(layout):
    """Пол `read_guard` пропускает media, хотя корень лежит под operational `<gc_state>`."""
    config = {**layout.config, "read_guard": {"enabled": False}}
    ctx = types.SimpleNamespace(config_dict=config, logger=None)
    for target in (layout.image, layout.own_staged, layout.mcp_shot):
        assert json.loads(_ReadTool(ctx).read(path=str(target)))["ok"] is True


def test_foreign_staging_read_is_denied(layout):
    config = {**layout.config, "read_guard": {"enabled": False}}
    ctx = types.SimpleNamespace(config_dict=config, logger=None)
    result = json.loads(_ReadTool(ctx).read(path=str(layout.foreign_staged)))
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"


def test_media_read_is_inside_the_perimeter(layout):
    """Периметр (`ask`) не спрашивает на каждый просмотр: корни media разрешены, как
    каталог скиллов. Для сравнения файл рядом, вне проекта, спрашивается."""
    config = {**layout.config, "read_guard": {"enabled": True, "mode": "ask"}}
    ctx, asked = _tui_ctx(config, "deny")
    assert (
        json.loads(_ReadTool(ctx).read(path=str(layout.image)))["ok"] is True
    )
    assert (
        json.loads(_ReadTool(ctx).read(path=str(layout.mcp_shot)))["ok"]
        is True
    )
    assert asked == []
    assert (
        json.loads(
            _ReadTool(ctx).read(path=str(layout.proj.parent / "elsewhere.png"))
        )["ok"]
        is False
    )
    assert len(asked) == 1


# --- запись -----------------------------------------------------------------


def test_media_write_decision_is_deny(layout):
    assert (
        protected_decision(
            layout.image,
            cwd=str(layout.proj),
            media=media_roots(layout.config),
        )
        == "deny"
    )


def test_media_write_is_denied(layout):
    config = {**layout.config, "write_guard": {"enabled": False}}
    ctx = types.SimpleNamespace(
        config_dict=config, is_tui=False, tui_confirm=None, logger=None
    )
    for target in (layout.image, layout.own_staged, layout.mcp_shot):
        result = json.loads(
            _WriteTool(ctx).write(path=str(target), content="x")
        )
        assert result["ok"] is False
        assert result["error"]["type"] == "protected_path_denied"


def test_custom_threads_dir_from_config_is_media(tmp_path, monkeypatch):
    """Корень берётся из `threads_data_path`: вне `<gc_state>` (не operational) чтение
    идёт без вопроса, а запись всё равно запрещена — классом media, не operational."""
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir()
    threads = tmp_path / "custom-threads"
    image = threads / IMAGES_DIRNAME / f"cli_t1_{SHA16}.png"
    config = {
        "launch_root": str(proj),
        "threads_data_path": str(threads),
        "read_guard": {"enabled": True, "mode": "ask"},
        "write_guard": {"enabled": False},
    }
    ctx, asked = _tui_ctx(config, "deny")
    assert json.loads(_ReadTool(ctx).read(path=str(image)))["ok"] is True
    assert asked == []
    result = json.loads(_WriteTool(ctx).write(path=str(image), content="x"))
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"


# --- команды ----------------------------------------------------------------


def test_scan_decisions_for_media(layout):
    media = media_roots(layout.config)
    cwd = str(layout.proj)
    assert (
        scan_command_for_protected(
            f"cp {layout.image} docs/", cwd=cwd, media=media
        )
        == "ask"
    )
    assert (
        scan_command_for_protected(f"rm {layout.image}", cwd=cwd, media=media)
        == "ask"
    )
    assert (
        scan_command_for_protected(
            f"cp {layout.mcp_shot} docs/", cwd=cwd, media=media
        )
        == "ask"
    )
    assert (
        scan_command_for_protected(
            f"cat {layout.own_staged}", cwd=cwd, media=media
        )
        == "ask"
    )
    assert (
        scan_command_for_protected(
            f"cat {layout.foreign_staged}", cwd=cwd, media=media
        )
        == "deny"
    )
    assert (
        scan_command_for_protected(
            f"rm -rf {layout.threads}", cwd=cwd, media=media
        )
        == "deny"
    )


def test_command_on_media_runs_when_confirmed(layout):
    ctx, asked = _tui_ctx(layout.config, "once")
    result = json.loads(_ExecTool(ctx).run(command=f"cp {layout.image} docs/"))
    assert result["ok"] is True
    assert len(asked) == 1


def test_command_on_media_declined_is_blocked(layout):
    ctx, asked = _tui_ctx(layout.config, "deny")
    result = json.loads(_ExecTool(ctx).run(command=f"rm {layout.image}"))
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"
    assert len(asked) == 1


def test_command_on_media_without_a_head_is_blocked(layout):
    """Без головы подтверждать некому: существующий путь confirm отдаёт отказ."""
    config = {**layout.config, "stop_tools": [ASK_HUMAN_TOOL_NAME]}
    ctx = types.SimpleNamespace(
        config_dict=config, is_tui=False, tui_confirm=None, logger=None
    )
    result = json.loads(_ExecTool(ctx).run(command=f"cp {layout.image} docs/"))
    assert result["ok"] is False
    assert result["error"]["type"] == "protected_path_denied"

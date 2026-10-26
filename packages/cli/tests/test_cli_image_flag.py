"""`gc-cli --image PATH`: картинки к запросу `-q` (спека §12, CLI).

Флаг объявлен до `parse_known_args`: необъявленный `--image` молча ушёл бы в
переопределения конфига, и запрос ушёл бы без картинки. Файлы проверяются и
ужимаются до старта агента; вложения — к сообщению `-q`, в его тексте — плашки
`[Изображение #N]`.
"""

import sys

# `import cli` парсит sys.argv на импорте (-q обязателен) — фиксируем валидный argv.
sys.argv = ["gc-cli", "-q", "x"]

import errno
import importlib
import io
import logging
import os
import types

import pytest
from gc_cli import cli
from gc_core.attachments.model import (
    MAX_INPUT_BYTES,
    ORIGIN_USER,
    Attachment,
    user_label,
)
from gc_core.attachments.store import (
    IMAGES_DIRNAME,
    STAGING_DIRNAME,
    ImageStore,
)
from gc_core.message_policy.user_message_policy import (
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from gc_core.thread_store import save_thread_meta
from gc_core.utils.format_messages import format_message_responses
from gc_testkit.settings import make_settings
from PIL import Image


def _png(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="PNG")
    return buf.getvalue()


def _image(tmp_path, name: str = "shot.png", color: str = "red") -> str:
    path = tmp_path / name
    path.write_bytes(_png(color))
    return str(path)


def _store(tmp_path) -> ImageStore:
    return ImageStore.for_threads_dir(str(tmp_path / "threads"))


def _att(n: int) -> Attachment:
    return Attachment(
        id=f"{n:016x}",
        kind="image",
        mime="image/png",
        width=64,
        height=48,
        size=100,
        path=f"/pocket/{n:016x}.png",
        origin=ORIGIN_USER,
        label=user_label(n),
        source="shot.png",
        orig_width=64,
        orig_height=48,
    )


# --- флаг --------------------------------------------------------------------


def test_image_flag_is_declared_and_repeatable():
    args = cli.arg_parser.parser.parse_args(
        ["-q", "x", "--image", "a.png", "--image", "b.png"]
    )
    assert args.images == ["a.png", "b.png"]


def test_image_flag_does_not_leak_into_config_overrides(monkeypatch):
    ap = cli.arg_parser
    # Вернуть состояние модуля после reload — его читают соседние тесты.
    monkeypatch.setattr(ap, "run_args", ap.run_args)
    monkeypatch.setattr(
        ap, "extra_config_overrides", ap.extra_config_overrides
    )
    monkeypatch.setattr(sys, "argv", ["gc-cli", "-q", "x", "--image", "a.png"])

    importlib.reload(ap)

    assert ap.run_args.images == ["a.png"]
    assert "image" not in ap.extra_config_overrides


# --- файлы → карман ------------------------------------------------------------


async def test_images_are_staged_in_order_with_numbers_from_first_n(tmp_path):
    store = _store(tmp_path)
    a = _image(tmp_path, "a.png", "red")
    b = _image(tmp_path, "b.png", "blue")

    images = await cli._astage_cli_images(
        [a, b], store, first_n=4, max_per_message=10
    )

    assert [att.label for att in images] == [user_label(4), user_label(5)]
    assert [att.source for att in images] == [a, b]
    assert [att.origin for att in images] == [ORIGIN_USER, ORIGIN_USER]
    assert [os.path.dirname(att.path) for att in images] == [
        store.staging_dir,
        store.staging_dir,
    ]


async def test_same_file_twice_is_one_attachment(tmp_path):
    a = _image(tmp_path)

    images = await cli._astage_cli_images(
        [a, a], _store(tmp_path), first_n=1, max_per_message=10
    )

    assert [att.label for att in images] == [user_label(1)]


async def test_more_images_than_allowed_is_refused_before_reading(tmp_path):
    with pytest.raises(cli.ImageArgError) as exc:
        await cli._astage_cli_images(
            ["x1", "x2", "x3"], _store(tmp_path), first_n=1, max_per_message=2
        )

    assert str(exc.value) == cli.IMAGE_TOO_MANY.format(n=2, count=3)


async def test_missing_file_is_refused(tmp_path):
    missing = str(tmp_path / "нет.png")

    with pytest.raises(cli.ImageArgError) as exc:
        await cli._astage_cli_images(
            [missing], _store(tmp_path), first_n=1, max_per_message=10
        )

    assert str(exc.value) == cli.IMAGE_UNREADABLE.format(path=missing)


async def test_not_an_image_is_refused(tmp_path):
    path = tmp_path / "notes.png"
    path.write_text("просто текст", encoding="utf-8")

    with pytest.raises(cli.ImageArgError) as exc:
        await cli._astage_cli_images(
            [str(path)], _store(tmp_path), first_n=1, max_per_message=10
        )

    assert str(exc.value) == cli.IMAGE_NOT_AN_IMAGE.format(path=str(path))


async def test_too_large_file_is_refused(tmp_path):
    path = tmp_path / "huge.png"
    path.write_bytes(_png() + b"\0" * (MAX_INPUT_BYTES + 1))

    with pytest.raises(cli.ImageArgError) as exc:
        await cli._astage_cli_images(
            [str(path)], _store(tmp_path), first_n=1, max_per_message=10
        )

    assert str(exc.value) == cli.IMAGE_TOO_LARGE.format(path=str(path))


def test_first_number_continues_the_thread(tmp_path):
    threads = str(tmp_path / "threads")
    save_thread_meta(threads, "cli", "t1", {"runtime": {"image_seq": 4}})

    assert cli._first_image_number(threads, "t1", False) == 5
    assert cli._first_image_number(threads, "t1", True) == 1
    assert cli._first_image_number(threads, None, False) == 1
    assert cli._first_image_number(threads, "fresh", False) == 1


# --- сообщения запуска -----------------------------------------------------------


def test_images_ride_on_the_query_message_with_chips_in_front():
    att = _att(1)

    parts, atts = cli._cli_user_parts(
        "почему съехало?",
        [att],
        first_n=1,
        wrap_technical=False,
        forced_skill_id=None,
    )

    assert parts == ["[Изображение #1] почему съехало?"]
    assert atts == [[att]]


def test_technical_wrap_covers_chips_and_query():
    att = _att(2)

    parts, atts = cli._cli_user_parts(
        "почему съехало?",
        [att],
        first_n=2,
        wrap_technical=True,
        forced_skill_id=None,
    )

    assert parts == [
        wrap_technical_user_content("[Изображение #2] почему съехало?")
    ]
    assert atts == [[att]]


def test_skill_instruction_is_a_separate_message_without_images():
    first, second = _att(3), _att(4)

    parts, atts = cli._cli_user_parts(
        "q",
        [first, second],
        first_n=3,
        wrap_technical=False,
        forced_skill_id="pack:skill",
    )

    assert parts == [
        "[Изображение #3] [Изображение #4] q",
        wrap_skill_resolution_technical_instruction("pack:skill"),
    ]
    assert atts == [[first, second], None]


def test_no_images_no_attachments():
    parts, atts = cli._cli_user_parts(
        "q", [], first_n=1, wrap_technical=False, forced_skill_id=None
    )

    assert parts == ["q"]
    assert atts is None


def test_console_trace_prints_content_not_the_attachment_records():
    msg = {
        "role": "user",
        "content": "[Изображение #1] что на снимке?",
        "attachments": [_att(1).to_dict()],
    }

    assert (
        format_message_responses(msg)
        == "\n\n>>>>>>>>>>\nuser: [Изображение #1] что на снимке?"
    )


# --- arun_cli: запуск целиком до исполнителя ----------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """`arun_cli` до исполнителя: конфиг, стамп, контекст и исполнитель подменены."""
    threads = tmp_path / "threads"
    config = make_settings(
        {
            "paths.threads_data_path": str(threads),
            "logging.console_trace": False,
        }
    )
    rec = types.SimpleNamespace(
        arun_calls=[], stamped=False, ctx_built=False, staged_existed=[]
    )

    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path, profile_override=None: config,
    )
    monkeypatch.setattr(
        cli, "apply_cli_overrides", lambda loaded, **_kw: loaded
    )
    monkeypatch.setattr(cli, "cli_forces_legacy_model", lambda cfg: False)
    monkeypatch.setattr(cli, "build_auth_connector", lambda cfg: None)
    from gc_core import startup_notices, upgrade_gate
    from gc_core.tools import registry

    monkeypatch.setattr(upgrade_gate, "detect_upgrade", lambda path=None: None)
    monkeypatch.setattr(
        upgrade_gate,
        "mark_current_run",
        lambda path=None: setattr(rec, "stamped", True),
    )
    monkeypatch.setattr(
        startup_notices, "collect_startup_notices", lambda *a, **k: []
    )
    monkeypatch.setattr(registry, "all_builtin_tool_names", lambda cfg: set())

    async def _ctx(cfg, **kw):
        rec.ctx_built = True
        return types.SimpleNamespace(
            mcp_status=None,
            registered_tool_names=None,
            logger=logging.getLogger("test_cli_image_flag"),
        )

    monkeypatch.setattr(cli, "acreate_run_context", _ctx)

    class _Executor:
        def __init__(self, ctx):
            pass

        async def arun(self, messages, **kw):
            rec.arun_calls.append((messages, kw))
            # Файлы кармана на момент хода: копию в тред делает ядро внутри arun.
            rec.staged_existed.extend(
                os.path.exists(att.path)
                for group in kw.get("attachments") or []
                if group
                for att in group
            )
            return "ok"

    monkeypatch.setattr(cli, "AgentExecutor", _Executor)
    return rec, threads


def _args(tmp_path, *argv):
    return cli.arg_parser.parser.parse_args(
        ["-q", "что на снимке?", "--config", str(tmp_path / "c.yml"), *argv]
    )


async def test_run_attaches_images_to_the_query_and_clears_the_pocket(
    cli_env, monkeypatch, tmp_path
):
    rec, _threads = cli_env
    shot = _image(tmp_path)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        _args(tmp_path, "--image", shot),
    )

    assert await cli.arun_cli() == 0

    [(messages, kw)] = rec.arun_calls
    assert messages == ["[Изображение #1] что на снимке?"]
    [[att]] = kw["attachments"]
    assert att.label == user_label(1)
    assert att.source == shot
    assert kw["thread_id"] is None
    assert rec.staged_existed == [True]  # карман цел, пока идёт ход
    assert not os.path.exists(att.path)  # и чист после него
    assert rec.stamped is True


async def test_with_a_thread_numbers_continue_the_thread(
    cli_env, monkeypatch, tmp_path
):
    rec, threads = cli_env
    save_thread_meta(str(threads), "cli", "t1", {"runtime": {"image_seq": 2}})
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        _args(tmp_path, "-t", "t1", "--image", _image(tmp_path)),
    )

    assert await cli.arun_cli() == 0

    [(messages, kw)] = rec.arun_calls
    assert messages == ["[Изображение #3] что на снимке?"]
    assert kw["thread_id"] == "t1"
    assert [att.label for att in kw["attachments"][0]] == [user_label(3)]


async def test_bad_image_stops_before_the_agent(
    cli_env, monkeypatch, tmp_path, capsys
):
    rec, _threads = cli_env
    missing = str(tmp_path / "нет.png")
    monkeypatch.setattr(
        cli.arg_parser, "run_args", _args(tmp_path, "--image", missing)
    )

    assert await cli.arun_cli() == 1

    assert capsys.readouterr().err.strip() == cli.IMAGE_UNREADABLE.format(
        path=missing
    )
    assert rec.ctx_built is False
    assert rec.stamped is False
    assert rec.arun_calls == []


async def test_start_sweeps_pockets_of_dead_processes(
    cli_env, monkeypatch, tmp_path
):
    _rec, threads = cli_env
    dead = threads / IMAGES_DIRNAME / STAGING_DIRNAME / "999999999"
    dead.mkdir(parents=True)
    (dead / "0123456789abcdef.png").write_bytes(_png())
    monkeypatch.setattr(cli.arg_parser, "run_args", _args(tmp_path))

    assert await cli.arun_cli() == 0

    assert not dead.exists()


async def test_failed_sweep_does_not_stop_the_run(
    cli_env, monkeypatch, tmp_path
):
    """Подметание — уборка, а не условие запуска: как в TUI, ошибка диска его пропускает."""
    rec, _threads = cli_env

    def _denied(self):
        raise PermissionError(errno.EACCES, "Permission denied", self.root)

    monkeypatch.setattr(ImageStore, "sweep_dead_staging", _denied)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        _args(tmp_path, "--image", _image(tmp_path)),
    )

    assert await cli.arun_cli() == 0

    [(messages, _kw)] = rec.arun_calls
    assert messages == ["[Изображение #1] что на снимке?"]


async def test_images_with_structured_output_ride_the_turn_and_the_pocket_is_cleared(
    cli_env, monkeypatch, tmp_path
):
    """`--image` вместе с `--structured-output-format`: картинки уходят с ходом,
    перефраз получает итог хода, а карман чист и после выхода через структурированный
    ответ (он возвращает код раньше обычной печати)."""
    rec, _threads = cli_env
    fmt = tmp_path / "format.md"
    fmt.write_text("json", encoding="utf-8")
    closed, emitted = [], []

    async def _close(self):
        closed.append(True)

    async def _emit(*, format_text, response, settings, ctx, executor):
        emitted.append((format_text, response))
        return 0

    monkeypatch.setattr(
        cli.AgentExecutor, "aclose_run_accounting", _close, raising=False
    )
    monkeypatch.setattr(cli, "_emit_structured_if_requested", _emit)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        _args(
            tmp_path,
            "--image",
            _image(tmp_path),
            "--structured-output-format",
            str(fmt),
        ),
    )

    assert await cli.arun_cli() == 0

    [(messages, kw)] = rec.arun_calls
    assert messages == ["[Изображение #1] что на снимке?"]
    [[att]] = kw["attachments"]
    assert kw["account_run"] is False
    assert emitted == [("json", "ok")]
    assert closed == [True]
    assert rec.staged_existed == [True]
    assert not os.path.exists(att.path)

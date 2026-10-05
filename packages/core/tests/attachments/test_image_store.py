"""Хранилище картинок: карман на pid, копия в тред, чтение со сверкой хеша, уборка."""

import errno
import getpass
import logging
import os
import stat
import tempfile
import threading
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.attachments import store as store_module
from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    user_label,
)
from gc_core.attachments.normalize import (
    MIME_JPEG,
    NormalizedImage,
    NormalizeError,
    normalize_image,
)
from gc_core.attachments.store import (
    IMAGES_DIRNAME,
    STAGING_DIRNAME,
    ImageStore,
    amake_attachment,
    commit_to_run_thread,
)
from gc_core.security.media_roots import media_roots
from gc_testkit.settings import make_settings
from PIL import Image


def _png(size=(60, 40), color=(10, 20, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _image(color=(10, 20, 30)) -> NormalizedImage:
    return normalize_image(_png(color=color))


def _stage(store: ImageStore, color=(10, 20, 30)):
    return store.stage(
        _image(color),
        origin=ORIGIN_USER,
        label=user_label(1),
        source="clipboard",
    )


def test_for_threads_dir_puts_the_root_under_threads(tmp_path):
    store = ImageStore.for_threads_dir(str(tmp_path))
    assert store.root == os.path.join(str(tmp_path), IMAGES_DIRNAME)
    assert store.staging_dir == os.path.join(
        store.root, STAGING_DIRNAME, str(os.getpid())
    )
    assert os.listdir(tmp_path) == []  # объект на диске ничего не создаёт


def test_relative_root_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = ImageStore("images", pid=111)
    assert store.root == os.path.join(os.getcwd(), "images")
    assert store.staging_dir == os.path.join(store.root, "_staging", "111")


def test_for_threads_dir_without_threads_uses_a_per_user_temp_root():
    owner = os.getuid() if hasattr(os, "getuid") else getpass.getuser()
    store = ImageStore.for_threads_dir("")
    assert store.root == os.path.join(
        os.path.abspath(tempfile.gettempdir()), f"gc-images-{owner}"
    )


posix_only = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="на Windows временный каталог у пользователя свой",
)


@posix_only
def test_temp_root_is_created_private(tmp_path):
    root = store_module._private_temp_root(str(tmp_path))
    assert root == str(tmp_path / f"gc-images-{os.getuid()}")
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700


@posix_only
def test_an_own_temp_root_left_open_is_closed(tmp_path):
    """Каталог, созданный раньше с правами по umask: картинки в нём видели бы все."""
    open_root = tmp_path / f"gc-images-{os.getuid()}"
    open_root.mkdir(mode=0o755)
    assert store_module._private_temp_root(str(tmp_path)) == str(open_root)
    assert stat.S_IMODE(os.lstat(open_root).st_mode) == 0o700


@posix_only
def test_a_planted_symlink_is_not_the_temp_root(tmp_path):
    """Общий /tmp: другой пользователь заранее кладёт ссылку под предсказуемым именем.
    Корнем она не становится — берётся свой приватный каталог процесса."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (tmp_path / f"gc-images-{os.getuid()}").symlink_to(
        secret, target_is_directory=True
    )
    root = store_module._private_temp_root(str(tmp_path))
    st = os.lstat(root)
    assert stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert not Path(root).resolve().is_relative_to(secret.resolve())
    assert (
        store_module._private_temp_root(str(tmp_path)) == root
    )  # один на процесс


@posix_only
def test_a_foreign_owned_temp_root_is_not_used(tmp_path, monkeypatch):
    other = os.getuid() + 1
    foreign = tmp_path / f"gc-images-{other}"
    foreign.mkdir(mode=0o700)  # для процесса с uid `other` — каталог чужой
    monkeypatch.setattr(os, "getuid", lambda: other)
    assert store_module._private_temp_root(str(tmp_path)) != str(foreign)


def test_for_config_takes_the_absolute_threads_dir(tmp_path):
    store = ImageStore.for_config(
        make_settings({"paths.threads_data_path": str(tmp_path)})
    )
    assert store.root == os.path.join(str(tmp_path), IMAGES_DIRNAME)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"paths.threads_data_path": ""},
        {"paths.threads_data_path": "rel/threads"},
    ],
)
def test_for_config_without_an_absolute_threads_dir_uses_the_temp_root(config):
    assert (
        ImageStore.for_config(make_settings(config)).root
        == ImageStore.for_threads_dir("").root
    )


@pytest.mark.parametrize("threads", ["custom", "", None])
def test_for_config_root_is_the_media_root(tmp_path, threads):
    """Одно правило корня: охрана путей считает `media` тот же корень и тот же
    карман, куда пишет хранилище."""
    config = (
        {}
        if threads is None
        else {
            "paths.threads_data_path": str(tmp_path / threads)
            if threads
            else ""
        }
    )
    config = make_settings(config)
    store = ImageStore.for_config(config)
    assert media_roots(config)[:2] == (
        Path(store.root),
        Path(store.staging_dir),
    )


def test_stage_writes_a_content_addressed_file_into_the_process_pocket(
    tmp_path,
):
    store = ImageStore(str(tmp_path), pid=111)
    image = _image()
    att = store.stage(
        image, origin=ORIGIN_USER, label=user_label(1), source="shot.png"
    )
    assert att.path == os.path.join(
        str(tmp_path), "_staging", "111", f"{att.id}.png"
    )
    assert len(att.id) == 16
    with open(att.path, "rb") as handle:
        assert handle.read() == image.data
    assert (att.kind, att.mime, att.size) == (
        "image",
        "image/png",
        len(image.data),
    )
    assert (att.width, att.height, att.orig_width, att.orig_height) == (
        60,
        40,
        60,
        40,
    )
    assert (att.origin, att.label, att.source) == (
        ORIGIN_USER,
        "Изображение #1",
        "shot.png",
    )


def test_jpeg_gets_the_jpg_extension(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    image = NormalizedImage(
        b"\xff\xd8\xff-jpeg-bytes", MIME_JPEG, 60, 40, 60, 40
    )
    att = store.stage(
        image, origin=ORIGIN_USER, label=user_label(1), source="a.jpg"
    )
    assert att.path.endswith(f"{att.id}.jpg")


def test_commit_copies_into_the_thread_and_is_idempotent(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    staged = _stage(store)
    committed = store.commit(staged, "CLI", "t1")
    assert committed.path == os.path.join(
        str(tmp_path), f"cli_t1_{staged.id}.png"
    )
    assert committed == replace(staged, path=committed.path)
    assert os.path.exists(staged.path)  # копия, карман не тронут
    assert store.commit(committed, "CLI", "t1") == committed
    assert store.commit(staged, "CLI", "t1") == committed


def test_commit_to_run_thread_copies_into_the_bound_thread(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    staged = _stage(store)
    ctx = SimpleNamespace(image_store=store, system_id="cli", thread_id="t1")
    assert commit_to_run_thread(ctx, staged) == store.commit(
        staged, "cli", "t1"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"system_id": None, "thread_id": None},  # прогон без треда
        {
            "system_id": "cli",
            "thread_id": "t1",
            "image_store": None,
        },  # контекст без хранилища
    ],
)
def test_commit_to_run_thread_keeps_the_pocket_attachment(tmp_path, fields):
    store = ImageStore(str(tmp_path), pid=111)
    staged = _stage(store)
    ctx = SimpleNamespace(**{"image_store": store, **fields})
    assert commit_to_run_thread(ctx, staged) == staged


def test_commit_without_the_pocket_file_keeps_the_attachment(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    staged = _stage(store)
    store.clear_own_staging()
    assert store.commit(staged, "cli", "t1") == staged


def test_commit_on_a_full_disk_keeps_the_attachment_and_warns(
    tmp_path, monkeypatch, caplog
):
    """Решение 1: commit не бросает никогда — иначе сообщение, уже снятое с очереди,
    или ответ человека терялись бы. Проекция отправит записку по файлу кармана."""
    store = ImageStore(str(tmp_path), pid=111)
    staged = _stage(store)

    def _no_space(path, data):
        raise OSError(errno.ENOSPC, "No space left on device", path)

    monkeypatch.setattr(store_module, "_write_atomic", _no_space)

    with caplog.at_level(logging.WARNING, logger=store_module.__name__):
        assert store.commit(staged, "cli", "t1") == staged
    assert "No space left on device" in caplog.text
    assert os.listdir(tmp_path) == [STAGING_DIRNAME]


def test_commit_with_an_unreadable_pocket_file_keeps_the_attachment(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    unreadable = replace(
        _stage(store), path=str(tmp_path)
    )  # каталог вместо файла
    assert store.commit(unreadable, "cli", "t1") == unreadable


def test_snapshot_writes_straight_into_the_thread(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    att = store.snapshot(
        _image(),
        "cli",
        "t1",
        origin=ORIGIN_TOOL,
        label=TOOL_LABEL,
        source="shots/login.png",
    )
    assert att.path == os.path.join(str(tmp_path), f"cli_t1_{att.id}.png")
    assert (att.origin, att.label, att.source) == (
        ORIGIN_TOOL,
        TOOL_LABEL,
        "shots/login.png",
    )
    assert not os.path.exists(store.staging_dir)


def test_thread_ids_are_validated(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    with pytest.raises(ValueError):
        store.commit(_stage(store), "cli", "../escape")


def test_read_for_send_checks_the_hash(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    att = store.commit(_stage(store), "cli", "t1")
    assert store.read_for_send(att) == _image().data
    with open(att.path, "wb") as handle:
        handle.write(_png(color=(200, 0, 0)))
    assert store.read_for_send(att) is None
    os.remove(att.path)
    assert store.read_for_send(att) is None


def test_delete_thread_images_removes_only_that_thread(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    mine = store.commit(_stage(store), "cli", "a")
    neighbour = store.commit(_stage(store, color=(1, 2, 3)), "cli", "a_b")
    other_system = store.commit(_stage(store), "tui", "a")
    store.delete_thread_images("cli", "a")
    assert not os.path.exists(mine.path)
    assert os.path.exists(neighbour.path)
    assert os.path.exists(other_system.path)


def test_delete_thread_images_without_a_root_is_a_no_op(tmp_path):
    ImageStore(str(tmp_path / "missing"), pid=111).delete_thread_images(
        "cli", "a"
    )


def test_clear_own_staging_leaves_other_pockets(tmp_path):
    mine, other = (
        ImageStore(str(tmp_path), pid=111),
        ImageStore(str(tmp_path), pid=222),
    )
    _stage(mine)
    theirs = _stage(other)
    mine.clear_own_staging()
    assert not os.path.exists(mine.staging_dir)
    assert os.path.exists(theirs.path)


def test_sweep_dead_staging_removes_only_dead_pockets(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "is_pid_alive", lambda pid: pid == 222)
    me, alive, dead = (
        ImageStore(str(tmp_path), pid=pid) for pid in (111, 222, 333)
    )
    for store in (me, alive, dead):
        _stage(store)
    stray = os.path.join(str(tmp_path), STAGING_DIRNAME, "notes")
    os.makedirs(stray)
    me.sweep_dead_staging()
    assert os.path.exists(me.staging_dir)  # свой — даже если «мёртв»
    assert os.path.exists(alive.staging_dir)
    assert not os.path.exists(dead.staging_dir)
    assert os.path.exists(stray)  # не pid — не наше


async def test_amake_attachment_from_bytes_normalizes_off_the_loop(
    tmp_path, monkeypatch
):
    threads = []

    def spy(data):
        threads.append(threading.current_thread())
        return normalize_image(data)

    monkeypatch.setattr(store_module, "normalize_image", spy)
    store = ImageStore(str(tmp_path), pid=111)
    shot = BytesIO()
    Image.linear_gradient("L").resize((5120, 2880)).convert("RGB").save(
        shot, format="PNG"
    )
    att = await amake_attachment(
        store, shot.getvalue(), origin=ORIGIN_USER, label=user_label(1)
    )
    assert threads and threads[0] is not threading.main_thread()
    assert (att.width, att.height, att.orig_width, att.orig_height) == (
        1568,
        882,
        5120,
        2880,
    )
    assert att.source == "clipboard"
    assert att.path.startswith(store.staging_dir)


@pytest.mark.parametrize(
    "name",
    [
        "Снимок экрана 2026-09-24 в 10.00.00.png",
        "Screenshot 2026-09-24 at 10.00.00\u202fAM.png",  # узкий неразрывный пробел macOS
    ],
)
async def test_amake_attachment_from_a_path_keeps_the_full_path(
    tmp_path, name
):
    """Источник файла — полный путь: лента показывает, откуда картинка."""
    source = tmp_path / name
    source.write_bytes(_png())
    store = ImageStore(str(tmp_path / "images"), pid=111)
    att = await amake_attachment(
        store, str(source), origin=ORIGIN_USER, label=user_label(2)
    )
    assert att.source == str(source)
    assert att.label == "Изображение #2"


async def test_amake_attachment_resolves_a_relative_path(
    tmp_path, monkeypatch
):
    (tmp_path / "shot.png").write_bytes(_png())
    monkeypatch.chdir(tmp_path)
    store = ImageStore(str(tmp_path / "images"), pid=111)
    att = await amake_attachment(
        store, "shot.png", origin=ORIGIN_USER, label=user_label(1)
    )
    assert att.source == str(tmp_path / "shot.png")


async def test_amake_attachment_errors_pass_through(tmp_path):
    store = ImageStore(str(tmp_path), pid=111)
    with pytest.raises(NormalizeError):
        await amake_attachment(
            store, b"not an image", origin=ORIGIN_USER, label=user_label(1)
        )
    with pytest.raises(FileNotFoundError):
        await amake_attachment(
            store,
            str(tmp_path / "gone.png"),
            origin=ORIGIN_USER,
            label=user_label(1),
        )

"""Фикстуры тредов формата develop годятся в эталон (спека хранилищ v2, 9.4).

Проверяем сами фикстуры и установщик: набор не тривиален, пути подставлены,
замороженные файлы установка не трогает.
"""

import json
from pathlib import Path

from gc_testkit.legacy_threads import (
    FIXTURE_ROOT,
    LEGACY_THREADS,
    install_legacy_threads,
)

PLACEHOLDERS = ("<THREADS_DIR>", "<WORKSPACE>", "<ROOT>")


def test_manifest_is_not_trivial():
    assert len(LEGACY_THREADS) == 7
    assert all(t.messages > 0 for t in LEGACY_THREADS.values())
    assert any(t.archives for t in LEGACY_THREADS.values())
    assert any(t.images for t in LEGACY_THREADS.values())
    assert any(t.subagent_events for t in LEGACY_THREADS.values())
    assert any(t.edits for t in LEGACY_THREADS.values())
    assert LEGACY_THREADS["tui_session"].title


def test_installed_fixture_has_no_placeholders_left(tmp_path):
    threads = install_legacy_threads(tmp_path)
    assert threads == tmp_path / "threads"
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files, "обход обязан что-то найти"
    for path in files:
        if path.suffix in {".json", ".jsonl"}:
            text = path.read_text(encoding="utf-8")
            assert not any(p in text for p in PLACEHOLDERS), path


def test_image_path_points_into_installed_tree(tmp_path):
    threads = install_legacy_threads(tmp_path)
    image = LEGACY_THREADS["sdk_image"]
    data = threads / f"data_{image.system}_{image.thread_id}.jsonl"
    rows = [
        json.loads(line)
        for line in data.read_text(encoding="utf-8").splitlines()
        if line
    ]
    paths = [
        att["path"]
        for row in rows
        for att in row["payload"].get("attachments") or []
    ]
    assert paths, "в треде с картинкой нет вложения"
    assert all(str(threads) in p and Path(p).is_file() for p in paths)


def test_install_does_not_touch_frozen_files(tmp_path):
    before = {
        p: p.read_bytes() for p in FIXTURE_ROOT.rglob("*") if p.is_file()
    }
    assert before, "обход обязан что-то найти"
    install_legacy_threads(tmp_path)
    after = {p: p.read_bytes() for p in FIXTURE_ROOT.rglob("*") if p.is_file()}
    assert before == after

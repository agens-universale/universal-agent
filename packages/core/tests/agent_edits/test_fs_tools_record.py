"""Тулы, меняющие файлы, пишут каждую успешную правку в журнал agent_edits.

Сюда же попадает `download_file`: он не из семейства fs, но файл в проекте
меняет так же, и сквозь журнал проходить обязан. Место общее нарочно — тот,
кто заведёт шестой такой тул, ищет стража здесь.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from gc_core.agent_edits.journal import EditsJournal
from gc_core.agent_edits.snapshots import SnapshotStore
from gc_core.config_modules import env_vars
from gc_core.agent_edits.paths import canonical_path_key
from gc_core.thread_store import edits_journal_path
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.save_base64_file import SaveBase64File
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile

from gc_testkit.helpers.tool_ctx import local_ctx

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    return launch_root, threads_dir


def _tool(
    tool_cls,
    launch_root,
    threads_dir,
    *,
    call_id="call-1",
    identity=True,
    config=None,
):
    """Тул с ctx как в тестах tools/fs (local_ctx) + thread-identity для журнала."""
    ctx = local_ctx({"launch_root": str(launch_root), **(config or {})})
    ctx.is_tui = True  # режим с записью правок по умолчанию
    if identity:
        ctx.threads_dir = str(threads_dir)
        ctx.system_id = "cli"
        ctx.thread_id = "th1"
        ctx.turn_id = "turn-1"
    return tool_cls(ctx=ctx, call_id=call_id)


def _records(threads_dir):
    return EditsJournal(
        edits_journal_path(str(threads_dir), "cli", "th1")
    ).read_all()


def _snapshot_store(launch_root, threads_dir):
    return SnapshotStore(
        launch_root / ".gc" / "agent_edits", "cli_th1", threads_dir
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install_http(monkeypatch, body: bytes) -> None:
    """Сеть download_file — транспортом httpx, а не заглушкой сессии.

    Подменяется фабрика клиента в модуле тула: тул берёт её оттуда по имени,
    и без подмены он уходит в НАСТОЯЩУЮ сеть (проверено 404 с example.com).
    """
    import httpx

    from gc_core.tools.exec import download_file as download_file_module

    def _client(**_kw):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=body,
                    headers={"Content-Type": "application/octet-stream"},
                )
            )
        )

    monkeypatch.setattr(download_file_module, "get_async_client", _client)


async def test_download_file_records_the_file_it_created(roots, monkeypatch):
    """Скачанный файл — правка агента в проекте, и она обязана быть в журнале.

    Тул писал напрямую через `open` и `atomic_replace`, минуя оба канала: ни
    записи, ни отметки создания. Файл появлялся в проекте, но на экране
    изменений его не было и откатить его было нечем (пятый раунд ревью,
    находка 7).

    Перезаписи тут не бывает по построению: существующий путь тул отвергает
    (`file_exists`), поэтому вид правки всегда `create`, а отката такому пути
    хватает собственного существования — базы «до» у него нет и быть не может.
    """
    from gc_core.tools.exec.download_file import DownloadFile

    launch_root, threads_dir = roots
    body = "%PDF-1.4 отчёт".encode("utf-8")
    (launch_root / "downloads").mkdir()
    _install_http(monkeypatch, body)

    tool = _tool(
        DownloadFile,
        launch_root,
        threads_dir,
        config={
            "download": {"download_dir": "downloads", "max_bytes": 1_000_000}
        },
    )
    result = json.loads(
        await tool.acall(
            url="https://example.com/report.pdf", filename="report.pdf"
        )
    )
    assert result["ok"] is True, result.get("error")

    dest = launch_root / "downloads" / "report.pdf"
    assert dest.read_bytes() == body
    key = canonical_path_key(str(dest), str(launch_root))
    recs = [r for r in _records(threads_dir) if r.path == key]
    assert [r.op for r in recs] == ["create"]
    assert recs[0].call_id == "call-1"


async def test_write_to_file_existing_records_write(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "app.py"
    target.parent.mkdir()
    old_bytes = b"line1\nline2\n"
    target.write_bytes(old_bytes)
    new_bytes = b"line1\nline2x\n"

    res = json.loads(
        await _tool(
            WriteToFile, launch_root, threads_dir, call_id="call-write"
        ).acall(path=str(target), content="line1\nline2x\n")
    )
    assert res["ok"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.path == path_key
    assert rec.op == "write"
    assert rec.call_id == "call-write"
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(new_bytes)

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) == old_bytes


async def test_write_to_file_new_file_records_create(roots):
    launch_root, threads_dir = roots
    target = launch_root / "fresh.py"
    new_bytes = b"hello\nworld\n"

    res = json.loads(
        await _tool(
            WriteToFile, launch_root, threads_dir, call_id="call-create"
        ).acall(path=str(target), content="hello\nworld\n")
    )
    assert res["ok"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.op == "create"
    assert rec.call_id == "call-create"
    assert rec.add == 2
    assert rec.del_ == 0
    assert rec.before_sha256 is None
    assert rec.after_sha256 == _sha(new_bytes)


async def test_append_to_file_records_append_with_baseline(roots):
    launch_root, threads_dir = roots
    target = launch_root / "log.txt"
    old_bytes = b"start\n"
    target.write_bytes(old_bytes)

    res = json.loads(
        await _tool(
            AppendToFile, launch_root, threads_dir, call_id="call-append"
        ).acall(path=str(target), content="added\n", separator="")
    )
    assert res["ok"] is True
    assert target.read_bytes() == b"start\nadded\n"

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.op == "append"
    assert rec.call_id == "call-append"
    assert rec.add == 1
    assert rec.del_ == 0
    assert rec.flags == []
    assert rec.before_sha256 == _sha(old_bytes)
    # хэш файла, а не дописанного куска
    assert rec.after_sha256 == _sha(target.read_bytes())

    path_key = canonical_path_key(str(target), str(launch_root))
    assert (
        _snapshot_store(launch_root, threads_dir).session_baseline(path_key)
        == old_bytes
    )


async def test_append_to_crlf_file_records_the_bytes_that_hit_the_disk(roots):
    """CRLF-файл: тул нормализует дописанный кусок к CRLF, и в журнал обязано
    уйти то, что легло на диск, а не исходный LF-кусок — иначе хэш «после»
    не совпадёт с файлом, и следующая правка увидит разрыв цепочки («извне»)."""
    launch_root, threads_dir = roots
    target = launch_root / "win.txt"
    old_bytes = b"one\r\ntwo\r\n"
    target.write_bytes(old_bytes)

    res = json.loads(
        await _tool(
            AppendToFile, launch_root, threads_dir, call_id="call-crlf"
        ).acall(path=str(target), content="three\n", separator="")
    )
    assert res["ok"] is True
    assert target.read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert res["bytes_appended"] == len(b"three\r\n")

    rec = _records(threads_dir)[0]
    assert rec.op == "append"
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(target.read_bytes())
    assert rec.add == 1


async def test_append_over_mutate_cap_records_partial_without_baseline(roots):
    launch_root, threads_dir = roots
    target = launch_root / "big.log"
    target.write_bytes(b"already here\n")

    res = json.loads(
        await _tool(
            AppendToFile,
            launch_root,
            threads_dir,
            call_id="call-partial",
            config={"fs_tools": {"max_mutate_bytes": 4}},
        ).acall(path=str(target), content="added\n", separator="")
    )
    assert res["ok"] is True
    assert target.read_bytes() == b"already here\nadded\n"

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.op == "append"
    assert rec.flags == ["partial"]
    assert rec.before_sha256 is None
    assert rec.after_sha256 is None
    assert rec.add == 1
    assert rec.del_ == 0

    path_key = canonical_path_key(str(target), str(launch_root))
    assert (
        _snapshot_store(launch_root, threads_dir).session_baseline(path_key)
        is None
    )


async def test_apply_file_edits_records_edit(roots):
    launch_root, threads_dir = roots
    target = launch_root / "app.py"
    old_bytes = b"a\nb\nc\n"
    target.write_bytes(old_bytes)
    new_bytes = b"a\nB\nc\n"

    res = json.loads(
        await _tool(
            ApplyFileEdits, launch_root, threads_dir, call_id="call-apply"
        ).acall(path=str(target), edits=[{"search": "b\n", "replace": "B\n"}])
    )
    assert res["applied"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.op == "edit"
    assert rec.call_id == "call-apply"
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(new_bytes)


async def test_search_and_replace_records_edit(roots):
    launch_root, threads_dir = roots
    target = launch_root / "conf.ini"
    old_bytes = b"host=old\nport=1\n"
    target.write_bytes(old_bytes)
    new_bytes = b"host=new\nport=1\n"

    res = json.loads(
        await _tool(
            SearchAndReplace, launch_root, threads_dir, call_id="call-replace"
        ).acall(path=str(target), search="host=old", replace="host=new")
    )
    assert res["applied"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.op == "edit"
    assert rec.call_id == "call-replace"
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(new_bytes)


async def test_created_binary_file_is_revertable_by_deletion(roots):
    """Созданный агентом бинарник виден как «создан», и откат ему доступен.

    Проверка сквозная НАРОЧНО: дефект жил в рекордере, а проявлялся в дельтах.
    Собрав запись руками, страж прошёл бы мимо — `created_here` по записи с
    `op="create"` работает и так. Ловится только цепочка целиком: тул записал →
    журнал сохранил вид правки → набор увидел создание.

    Откат такому пути — удаление, базы «до» у него нет и быть не может.
    """
    launch_root, threads_dir = roots
    target = launch_root / "logo.png"

    res = json.loads(
        await _tool(
            SaveBase64File, launch_root, threads_dir, call_id="call-png"
        ).acall(
            path=str(target), data=base64.b64encode(PNG_BYTES).decode("ascii")
        )
    )
    assert res["ok"] is True

    from gc_core.agent_edits.deltas import file_entries

    [entry] = file_entries(
        _records(threads_dir), lambda key: None, str(launch_root)
    )
    assert entry.kind == "created"
    assert entry.revertable is True


async def test_save_base64_file_records_binary(roots):
    launch_root, threads_dir = roots
    target = launch_root / "shot.png"

    res = json.loads(
        await _tool(
            SaveBase64File, launch_root, threads_dir, call_id="call-b64"
        ).acall(
            path=str(target), data=base64.b64encode(PNG_BYTES).decode("ascii")
        )
    )
    assert res["ok"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    # Бинарность — ФЛАГ, а не вид правки: `op` отвечает на вопрос «что сделал
    # тул». Затирая его родом содержимого, журнал терял создание файла, и
    # созданный бинарник переставал откатываться (пятый раунд ревью, находка 5).
    assert rec.op == "create"
    assert "no_diff" in rec.flags
    assert rec.call_id == "call-b64"
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.add == 0
    assert rec.del_ == 0
    assert rec.before_sha256 is None
    assert rec.after_sha256 == _sha(PNG_BYTES)


async def test_save_base64_file_over_existing_text_keeps_baseline(roots):
    """Перезапись существующего файла: старые байты в снапшот, op — по факту."""
    launch_root, threads_dir = roots
    target = launch_root / "notes.txt"
    old_bytes = b"line1\nline2\n"
    target.write_bytes(old_bytes)
    new_bytes = b"line1\nline2x\n"

    res = json.loads(
        await _tool(
            SaveBase64File, launch_root, threads_dir, call_id="call-b64-over"
        ).acall(
            path=str(target), data=base64.b64encode(new_bytes).decode("ascii")
        )
    )
    assert res["ok"] is True

    recs = _records(threads_dir)
    assert len(recs) == 1
    rec = recs[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.op == "write"
    assert rec.flags == []
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(new_bytes)
    assert (
        _snapshot_store(launch_root, threads_dir).session_baseline(path_key)
        == old_bytes
    )


async def test_save_base64_file_binary_over_existing_keeps_before_hash(roots):
    """Бинарный payload: op/no_diff ставит рекордер, но baseline-хэш не теряется."""
    launch_root, threads_dir = roots
    target = launch_root / "shot.png"
    old_bytes = b"was text here\n"
    target.write_bytes(old_bytes)

    res = json.loads(
        await _tool(
            SaveBase64File, launch_root, threads_dir, call_id="call-b64-bin"
        ).acall(
            path=str(target), data=base64.b64encode(PNG_BYTES).decode("ascii")
        )
    )
    assert res["ok"] is True

    rec = _records(threads_dir)[0]
    assert rec.op == "write"  # существующий файл: вид правки — запись
    assert "no_diff" in rec.flags
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.before_sha256 == _sha(old_bytes)
    assert rec.after_sha256 == _sha(PNG_BYTES)


async def test_agent_edits_config_keys_affect_fs_tool_record(roots):
    """Ключи секции agent_edits доходят до записи, сделанной fs-тулом."""
    launch_root, threads_dir = roots
    target = launch_root / "app.py"
    old_lines = [f"line{i}\n" for i in range(10)]
    target.write_bytes("".join(old_lines).encode("utf-8"))
    new_lines = list(old_lines)
    new_lines[5] = "line5x\n"

    res = json.loads(
        await _tool(
            WriteToFile,
            launch_root,
            threads_dir,
            call_id="call-cfg",
            config={"agent_edits": {"hunk_context_lines": 1}},
        ).acall(path=str(target), content="".join(new_lines))
    )
    assert res["ok"] is True

    rec = _records(threads_dir)[0]
    # контекст 1: вокруг правки ровно по одной строке, line3/line8 в дифф не попали
    assert (
        rec.hunks
        == "--- \n+++ \n@@ -5,3 +5,3 @@\n line4\n-line5\n+line5x\n line6\n"
    )
    assert rec.add == 1
    assert rec.del_ == 1


async def test_max_diff_chars_from_config_truncates_fs_tool_record(roots):
    launch_root, threads_dir = roots
    target = launch_root / "many.py"
    target.write_bytes(
        ("".join(f"old-{i}\n" for i in range(50))).encode("utf-8")
    )

    res = json.loads(
        await _tool(
            WriteToFile,
            launch_root,
            threads_dir,
            call_id="call-cfg-chars",
            config={"agent_edits": {"max_diff_chars": 30}},
        ).acall(
            path=str(target), content="".join(f"new-{i}\n" for i in range(50))
        )
    )
    assert res["ok"] is True

    rec = _records(threads_dir)[0]
    assert rec.flags == ["truncated"]
    assert len(rec.hunks) == 30
    assert rec.add == 50
    assert rec.del_ == 50


async def test_backup_copy_is_not_recorded(roots):
    launch_root, threads_dir = roots
    target = launch_root / "app.py"
    target.write_bytes(b"old\n")

    res = json.loads(
        await _tool(
            WriteToFile, launch_root, threads_dir, call_id="call-backup"
        ).acall(path=str(target), content="new\n", backup=True)
    )
    assert res["ok"] is True
    assert (launch_root / "app.py.bak").read_bytes() == b"old\n"

    recs = _records(threads_dir)
    assert len(recs) == 1
    assert recs[0].path == canonical_path_key(str(target), str(launch_root))


async def test_no_thread_identity_writes_no_journal(roots):
    launch_root, threads_dir = roots
    target = launch_root / "app.py"

    res = json.loads(
        await _tool(
            WriteToFile, launch_root, threads_dir, identity=False
        ).acall(path=str(target), content="hello\n")
    )
    assert res["ok"] is True
    assert target.read_bytes() == b"hello\n"
    assert list(threads_dir.iterdir()) == []


async def test_write_to_file_reads_old_content_only_when_recording(roots):
    launch_root, threads_dir = roots
    target = launch_root / "app.py"
    target.write_bytes(b"old\n")

    def _count_reads(tool) -> list[str]:
        """Счётчик read_bytes на backend этого тула."""
        calls: list[str] = []
        inner = tool._ctx.os_backend.read_bytes

        async def read_bytes(path: str) -> bytes:
            calls.append(path)
            return await inner(path)

        tool._ctx.os_backend.read_bytes = read_bytes
        return calls

    without_identity = _tool(
        WriteToFile, launch_root, threads_dir, identity=False
    )
    reads_off = _count_reads(without_identity)
    raw = await without_identity.acall(path=str(target), content="a\n")
    assert json.loads(raw)["ok"] is True
    assert reads_off == []

    with_identity = _tool(WriteToFile, launch_root, threads_dir)
    reads_on = _count_reads(with_identity)
    raw = await with_identity.acall(path=str(target), content="b\n")
    assert json.loads(raw)["ok"] is True
    assert reads_on == [str(target)]


async def test_append_to_empty_existing_file_is_not_a_creation(roots):
    """Пустой СУЩЕСТВУЮЩИЙ файл дописыванием не «создаётся».

    Флаг ведущего BOM у пустого файла поднят законно, но раньше он же уезжал в
    журнал как `op="create"`, а откат созданного файл УДАЛЯЕТ, базу не
    спрашивая: пустой закоммиченный `__init__.py` после дописывания и отката
    исчезал вместо возврата к пустому. Заодно у такого файла не снималась и
    база «до» — возвращать было бы не к чему.
    """
    launch_root, threads_dir = roots
    target = launch_root / "empty.py"
    target.write_bytes(b"")

    res = json.loads(
        await _tool(
            AppendToFile, launch_root, threads_dir, call_id="call-empty"
        ).acall(path=str(target), content="first\n", separator="")
    )
    assert res["ok"] is True

    rec = _records(threads_dir)[0]
    assert rec.op == "append", "пустой существующий файл помечен созданным"

    path_key = canonical_path_key(str(target), str(launch_root))
    base = _snapshot_store(launch_root, threads_dir).session_baseline(path_key)
    assert base == b"", (
        "база «до» у пустого файла не снята — откату не к чему вернуться"
    )

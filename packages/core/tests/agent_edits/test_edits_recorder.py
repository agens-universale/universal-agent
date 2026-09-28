"""record_edit: единая точка записи о правке файла в журнал треда."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from gc_core.agent_edits.journal import EditsJournal
from gc_core.agent_edits.recorder import is_recording, record_edit
from gc_core.agent_edits.snapshots import SnapshotStore
from gc_core.config_modules import env_vars
from gc_core.agent_edits.paths import canonical_path_key
from gc_core.thread_store import edits_journal_path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    return launch_root, threads_dir


def _ctx(launch_root, threads_dir, *, system_id="cli", thread_id="th1", turn_id="turn-1"):
    return SimpleNamespace(
        config_dict={"launch_root": str(launch_root)},
        threads_dir=str(threads_dir),
        system_id=system_id,
        thread_id=thread_id,
        turn_id=turn_id,
    )


def _journal(threads_dir, *, system_id="cli", thread_id="th1"):
    return EditsJournal(edits_journal_path(str(threads_dir), system_id, thread_id))


def _snapshot_store(launch_root, threads_dir, *, system_id="cli", thread_id="th1"):
    return SnapshotStore(
        launch_root / ".gc" / "agent_edits", f"{system_id}_{thread_id}", threads_dir
    )


def _git_repo_with_seed_commit(launch_root) -> str:
    """`git init` + один коммит в launch_root → его ревизия HEAD.

    Отметка создания и `session_head_rev` пишутся только там, где HEAD
    читается: без репозитория ревизии нет вовсе, и тест про них проходил бы
    при ЛЮБОМ поведении рекордера.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(["git", *args], cwd=str(launch_root), check=True,
                       capture_output=True)
    (launch_root / "seed.txt").write_bytes(b"seed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(launch_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(launch_root), check=True,
                   capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(launch_root),
                          check=True, capture_output=True).stdout.decode().strip()


async def test_normal_edit_records_hunks_hashes_and_both_snapshots(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    old_bytes = b"line1\nline2\n"
    new_bytes = b"line1\nline2x\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-1",
    )

    recs = _journal(threads_dir).read_all()
    assert len(recs) == 1
    rec = recs[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.path == path_key
    assert rec.op == "edit"
    assert rec.call_id == "call-1"
    assert rec.turn_id == "turn-1"
    assert rec.hunks == "--- \n+++ \n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2x\n"
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.before_sha256 == hashlib.sha256(old_bytes).hexdigest()
    assert rec.after_sha256 == hashlib.sha256(new_bytes).hexdigest()
    assert rec.flags == []

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) == old_bytes
    assert store.turn_baseline(path_key, "turn-1") == old_bytes


async def test_create_has_no_before_hash_and_no_session_snapshot(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "new.py"
    target.parent.mkdir()
    new_bytes = b"hello\nworld\n"
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=None,
        new_bytes=new_bytes,
        op="write",
        call_id="call-2",
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.op == "write"  # op передан вызывающим, рекордер его не переопределяет
    assert rec.before_sha256 is None
    assert rec.after_sha256 == hashlib.sha256(new_bytes).hexdigest()
    assert rec.hunks == "--- \n+++ \n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    assert rec.add == 2
    assert rec.del_ == 0

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) is None
    assert store.turn_baseline(path_key, "turn-1") is None


async def test_delete_has_no_after_hash_but_keeps_snapshots(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "gone.py"
    target.parent.mkdir()
    old_bytes = b"line1\nline2\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=None,
        op="delete",
        call_id="call-3",
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.op == "delete"
    assert rec.before_sha256 == hashlib.sha256(old_bytes).hexdigest()
    assert rec.after_sha256 is None
    assert rec.hunks == "--- \n+++ \n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
    assert rec.add == 0
    assert rec.del_ == 2

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) == old_bytes
    assert store.turn_baseline(path_key, "turn-1") == old_bytes


async def test_both_bytes_none_is_noop(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx, abs_path=str(target), old_bytes=None, new_bytes=None, op="edit", call_id="call-x"
    )

    assert _journal(threads_dir).read_all() == []


async def test_binary_content_forces_binary_op_without_snapshots(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "blob.bin"
    target.parent.mkdir()
    old_bytes = b"\x00\x01\x02old-binary"
    new_bytes = b"\x00\x01\x02new-binary"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="write",
        call_id="call-4",
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    # Бинарность — ФЛАГ, а не вид правки: поле `op` отвечает на вопрос
    # «что сделал тул», и затирать его родом содержимого значило терять
    # создание файла (пятый раунд ревью, находка 5).
    assert rec.op == "write"
    assert "no_diff" in rec.flags
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.add == 0
    assert rec.del_ == 0

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) is None
    assert store.turn_baseline(path_key, "turn-1") is None


async def test_oversized_content_skips_diff_and_snapshot(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "big.py"
    target.parent.mkdir()
    old_bytes = b"a" * 100
    new_bytes = b"b" * 100
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-5",
        max_diff_source_bytes=50,
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.op == "edit"  # не binary — op сохранён как передан
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.add == 0
    assert rec.del_ == 0
    assert rec.before_sha256 == hashlib.sha256(old_bytes).hexdigest()
    assert rec.after_sha256 == hashlib.sha256(new_bytes).hexdigest()

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) is None


async def test_large_diff_is_truncated_with_flag_but_counts_stay_accurate(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "many.py"
    target.parent.mkdir()
    old_bytes = ("\n".join(f"old-{i}" for i in range(50)) + "\n").encode("utf-8")
    new_bytes = ("\n".join(f"new-{i}" for i in range(50)) + "\n").encode("utf-8")
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-6",
        max_diff_chars=20,
    )

    rec = _journal(threads_dir).read_all()[0]
    assert rec.flags == ["truncated"]
    assert len(rec.hunks) == 20
    assert rec.add == 50
    assert rec.del_ == 50


async def test_add_count_not_fooled_by_content_line_starting_with_plus_plus(roots):
    """Регрессия: контент '++i;' после '+'-маркера diff даёт '+++i;' — не
    должен приниматься за заголовок '+++' и теряться из счётчика add."""
    launch_root, threads_dir = roots
    target = launch_root / "src" / "counter.py"
    target.parent.mkdir()
    old_bytes = b"x\n"
    new_bytes = b"++i;\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-11",
    )

    rec = _journal(threads_dir).read_all()[0]
    assert rec.add == 1
    assert rec.del_ == 1
    assert "+++i;\n" in rec.hunks


async def test_del_count_not_fooled_by_content_line_starting_with_dash_dash(roots):
    """Регрессия: удалённая строка '--x;' даёт '---x;' после '-'-маркера — не
    должна приниматься за заголовок '---' и теряться из счётчика del."""
    launch_root, threads_dir = roots
    target = launch_root / "src" / "counter2.py"
    target.parent.mkdir()
    old_bytes = b"before\n--x;\nafter\n"
    new_bytes = b"before\nafter\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-12",
    )

    rec = _journal(threads_dir).read_all()[0]
    assert rec.add == 0
    assert rec.del_ == 1
    assert "---x;\n" in rec.hunks


async def test_del_count_not_fooled_by_content_line_that_is_a_dash_separator(roots):
    """Регрессия: удалённая строка-разделитель '---' (markdown/YAML) даёт
    '----' после '-'-маркера — не должна приниматься за заголовок diff'а."""
    launch_root, threads_dir = roots
    target = launch_root / "src" / "readme.md"
    target.parent.mkdir()
    old_bytes = b"before\n---\nafter\n"
    new_bytes = b"before\nafter\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-13",
    )

    rec = _journal(threads_dir).read_all()[0]
    assert rec.add == 0
    assert rec.del_ == 1
    assert "----\n" in rec.hunks


async def test_backup_true_is_noop(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=b"a",
        new_bytes=b"b",
        op="edit",
        call_id="call-7",
        backup=True,
    )

    assert _journal(threads_dir).read_all() == []


async def test_ctx_without_thread_identity_is_noop(roots):
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    ctx = SimpleNamespace(config_dict={"launch_root": str(launch_root)})

    await record_edit(
        ctx, abs_path=str(target), old_bytes=b"a", new_bytes=b"b", op="edit", call_id="call-8"
    )

    assert list(threads_dir.glob("edits_*.jsonl")) == []


async def test_external_file_is_recorded_without_any_baseline(roots):
    """Внешний файл попадает в журнал, но базы не получает.

    Журнальная запись нужна ради блока диффа в ленте — он собирается из хэнков
    по `call_id` и базы не требует. Снимок же означал бы копию чужого
    содержимого в каталоге проекта и право откатить файл за его пределами.
    """
    launch_root, threads_dir = roots
    outside = launch_root.parent / "outside" / "app.yml"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"a: 1\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(outside),
        old_bytes=b"a: 1\n",
        new_bytes=b"a: 2\n",
        op="write",
        call_id="c1",
    )

    records = _journal(threads_dir).read_all()
    key = outside.resolve().as_posix()
    assert [r.path for r in records] == [key]
    assert records[0].call_id == "c1"
    assert records[0].hunks != ""          # блоку в ленте есть что показать

    store = _snapshot_store(launch_root, threads_dir)
    assert store.counting_session_baseline(key) is None
    assert store.turn_baseline(key, "turn-1") is None


async def test_external_file_deleted_here_gets_no_baseline(roots):
    """Удаление внешнего файла — тоже только запись для ленты, без базы.

    У удаления база и есть всё, что остаётся от файла, и внутреннему пути её
    снимают обе (`test_delete_has_no_after_hash_but_keeps_snapshots`). Внешнему
    — ни одной: копия чужого содержимого в каталоге проекта и право вернуть
    файл за его пределами — ровно то, чего правило внешних путей не даёт."""
    launch_root, threads_dir = roots
    outside = launch_root.parent / "outside" / "gone.yml"
    outside.parent.mkdir(parents=True, exist_ok=True)
    old_bytes = b"a: 1\nb: 2\n"
    outside.write_bytes(old_bytes)

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(outside),
        old_bytes=old_bytes,
        new_bytes=None,
        op="delete",
        call_id="c1",
    )

    key = outside.resolve().as_posix()
    records = _journal(threads_dir).read_all()
    assert [(r.path, r.op) for r in records] == [(key, "delete")]
    assert records[0].hunks != ""          # блоку в ленте есть что показать
    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(key) is None
    assert store.counting_session_baseline(key) is None
    assert store.turn_baseline(key, "turn-1") is None


async def test_external_file_created_here_gets_no_created_mark(roots):
    """Отметка создания — тоже ступень отсчёта, и внешнему пути её не дают.

    Отметка создания живёт в ИНДЕКСЕ снапшотов (`SnapshotStore.mark_created`)
    и читается через `created_head_rev`, а не через `.orig`/`.gitorig`
    (`counting_session_baseline` их не видит вовсе) — поэтому без настоящего
    git-репозитория с ревизией `mark_created` выходит первой же проверкой
    (`head_rev is None`) и тест не отличил бы снятый гвард от рабочего.
    `_git_repo_with_seed_commit` даёт HEAD, на который отметке было бы что
    сослаться, если бы гвард `is_external_key` в рекордере не сработал.
    """
    launch_root, threads_dir = roots
    _git_repo_with_seed_commit(launch_root)
    outside = launch_root.parent / "outside" / "new.yml"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"a: 1\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(outside),
        old_bytes=None,
        new_bytes=b"a: 1\n",
        op="create",
        call_id="c1",
    )

    key = outside.resolve().as_posix()
    assert [r.path for r in _journal(threads_dir).read_all()] == [key]
    store = _snapshot_store(launch_root, threads_dir)
    assert store.created_head_rev(key) is None
    assert store.counting_session_baseline(key) is None


async def test_extra_paths_no_longer_buy_a_baseline(roots):
    """Путь из `write_guard.extra_paths` — такой же внешний, как любой другой.

    Регресс ловит не отсутствие `extra_paths` в каноне ключа: верни его туда
    кто-то снова — форма ключа не изменится, `canonical_path_key` и так отдаёт
    абсолютный путь любому файлу вне `launch_root`. Ловит гвард рекордера на
    `is_external_key` (тот же, что у соседних тестов внешних путей) — тест
    закрепляет, что запись в `write_guard.extra_paths` не даёт такому пути
    никакой особой лазейки мимо этого гварда.
    """
    launch_root, threads_dir = roots
    extra = launch_root.parent / "extra_root"
    extra.mkdir(parents=True, exist_ok=True)
    target = extra / "lib.py"
    target.write_bytes(b"x = 1\n")

    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["write_guard"] = {"extra_paths": [str(extra)]}

    await record_edit(
        ctx, abs_path=str(target), old_bytes=b"x = 1\n", new_bytes=b"x = 2\n",
        op="write", call_id="c1",
    )

    key = target.resolve().as_posix()
    assert [r.path for r in _journal(threads_dir).read_all()] == [key]
    assert _snapshot_store(launch_root, threads_dir).counting_session_baseline(key) is None


async def test_global_instruction_file_is_not_recorded_at_all(roots):
    """Глобальный AGENTS.md (L1) не получает НИЧЕГО — ни ключа, ни записи.

    Соблазнительно решить, что он вне проекта и потому закрывается правилом
    внешних файлов. Нет: проверка защиты в `canonical_path_key` стоит РАНЬШЕ
    вопроса о периметре, и L1 остаётся невидимым, как был.

    `gc_home` фикстура `roots` уже подменила на временный — иначе тест писал бы
    в настоящий домашний каталог.
    """
    launch_root, threads_dir = roots

    from gc_core.instructions.layers import instruction_file_paths

    l1 = instruction_file_paths(cwd=str(launch_root))["L1"]
    l1.parent.mkdir(parents=True, exist_ok=True)
    l1.write_bytes("старое\n".encode("utf-8"))

    await record_edit(
        _ctx(launch_root, threads_dir), abs_path=str(l1),
        old_bytes="старое\n".encode("utf-8"), new_bytes="новое\n".encode("utf-8"),
        op="write", call_id="c1",
    )

    assert _journal(threads_dir).read_all() == []


async def test_instruction_file_of_a_neighbour_project_is_recorded(roots):
    """AGENTS.md ЧУЖОГО репозитория — обычный внешний файл, и он виден.

    `instruction_file_paths(cwd=launch_root)` знает инструкции только своего
    проекта, поэтому защита соседа не накрывает. Поведение новое и желаемое —
    это правка файла вне проекта, её и показываем, — но закрепить его надо
    явно, чтобы оно не выглядело случайностью.
    """
    launch_root, threads_dir = roots
    neighbour = launch_root.parent / "neighbour"
    neighbour.mkdir(parents=True, exist_ok=True)
    target = neighbour / "AGENTS.md"
    target.write_bytes("правила\n".encode("utf-8"))

    await record_edit(
        _ctx(launch_root, threads_dir), abs_path=str(target),
        old_bytes="правила\n".encode("utf-8"), new_bytes="другие правила\n".encode("utf-8"),
        op="write", call_id="c1",
    )

    key = target.resolve().as_posix()
    assert [r.path for r in _journal(threads_dir).read_all()] == [key]
    assert _snapshot_store(launch_root, threads_dir).counting_session_baseline(key) is None


async def test_user_skill_outside_the_project_is_recorded(roots):
    """Скилл пользователя — класс `skills`, а он от отслеживания не защищает.

    Правка user-скилла впервые становится видна: `classify_path` проверяет
    `skills_exceptions` ПЕРВЫМИ, поэтому путь под `<data>/skills` не попадает
    ни в `operational`, ни в `instructions`.
    """
    launch_root, threads_dir = roots

    from gc_core.config_modules.env_vars import gc_paths

    skill = gc_paths()["data"] / "skills" / "my" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes("шаг\n".encode("utf-8"))

    await record_edit(
        _ctx(launch_root, threads_dir), abs_path=str(skill),
        old_bytes="шаг\n".encode("utf-8"), new_bytes="другой шаг\n".encode("utf-8"),
        op="write", call_id="c1",
    )

    assert [r.path for r in _journal(threads_dir).read_all()] == [
        skill.resolve().as_posix()
    ]


async def test_cyrillic_path_and_cp1251_content_records_and_snapshots_bytes_exactly(roots):
    launch_root, threads_dir = roots
    target = launch_root / "тест.txt"
    old_text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    new_text = old_text + "Добавлен новый абзац с важной информацией.\n"
    old_bytes = old_text.encode("cp1251")
    new_bytes = new_text.encode("cp1251")
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-10",
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert path_key == "тест.txt"
    assert rec.add == 1
    assert rec.del_ == 0
    assert rec.before_sha256 == hashlib.sha256(old_bytes).hexdigest()
    assert rec.after_sha256 == hashlib.sha256(new_bytes).hexdigest()

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) == old_bytes
    assert store.turn_baseline(path_key, "turn-1") == old_bytes


def test_is_recording_requires_thread_identity(roots):
    launch_root, threads_dir = roots
    assert is_recording(_ctx(launch_root, threads_dir)) is True
    assert is_recording(SimpleNamespace(config_dict={})) is False
    assert (
        is_recording(
            SimpleNamespace(
                config_dict={},
                threads_dir=str(threads_dir),
                system_id="cli",
                thread_id="",
            )
        )
        is False
    )


async def test_snapshots_go_to_agent_edits_dir_not_legacy_diffs(roots):
    launch_root, threads_dir = roots
    target = launch_root / "foo.py"
    old_bytes = b"line1\n"
    target.write_bytes(old_bytes)
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=b"line1x\n",
        op="edit",
        call_id="call-dir",
    )

    # каталог legacy DiffStore не создаётся и не делится с ним index.json
    assert not (launch_root / ".gc" / "diffs").exists()
    snapshots_dir = launch_root / ".gc" / "agent_edits"
    assert len(list(snapshots_dir.glob("*.orig"))) == 1
    assert len(list(snapshots_dir.glob("*.turn"))) == 1
    assert (snapshots_dir / "index.json").is_file()


async def test_partial_skips_snapshots_and_file_hashes(roots):
    launch_root, threads_dir = roots
    target = launch_root / "log.txt"
    target.write_bytes(b"head\n")
    ctx = _ctx(launch_root, threads_dir)

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=None,
        new_bytes=b"tail\n",
        op="append",
        call_id="call-partial",
        partial=True,
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.op == "append"
    assert rec.flags == ["partial"]
    assert rec.before_sha256 is None
    assert rec.after_sha256 is None
    assert rec.add == 1
    assert rec.del_ == 0

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) is None


def _ctx_with_limit(launch_root, threads_dir, max_files: int):
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["agent_edits"] = {"snapshots_max_files": max_files}
    return ctx


async def test_first_edit_evicts_orphans_but_never_live_threads(roots):
    """Эвикция живёт в проде — и не трогает снимки тредов с живым журналом.

    Каталог переполнен так, что кандидатов больше, чем сирот: если эвикцию
    позвать ДО записи журнала, свой тред живым ещё не считается и его свежие
    снимки уходят вместе с сиротами — база отката теряется молча.
    """
    launch_root, threads_dir = roots
    snapshots_dir = launch_root / ".gc" / "agent_edits"
    snapshots_dir.mkdir(parents=True)
    orphans = [snapshots_dir / f"{'0' * 64}_dead_th{i}.orig" for i in (1, 2)]
    for p in orphans:
        p.write_bytes(b"orphan")

    # чужой, но ЖИВОЙ тред: журнал на месте — его снимок неприкосновенен
    edits_journal_path(str(threads_dir), "cli", "other").write_text("", encoding="utf-8")
    alive = SnapshotStore(snapshots_dir, "cli_other", threads_dir)
    alive.capture_session("other.py", b"alive\n")

    target = launch_root / "foo.py"
    target.write_bytes(b"line1\n")

    # 5 снимков при лимите 1: сирот всего 2, остальное защищено живыми журналами
    await record_edit(
        _ctx_with_limit(launch_root, threads_dir, 1),
        abs_path=str(target),
        old_bytes=b"line1\n",
        new_bytes=b"line1x\n",
        op="edit",
        call_id="call-1",
    )

    assert not any(p.exists() for p in orphans)
    assert alive.session_baseline("other.py") == b"alive\n"
    path_key = canonical_path_key(str(target), str(launch_root))
    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_baseline(path_key) == b"line1\n"
    assert store.turn_baseline(path_key, "turn-1") == b"line1\n"


async def test_zero_max_files_switches_eviction_off_instead_of_wiping(roots, monkeypatch):
    """`snapshots_max_files: 0` — «не вытеснять», и ноль обязан доехать нулём.

    В `evict_lru` ноль отсекается первой же строкой (`max_files <= 0 → return`),
    так что снести он ничего не может по построению. Пока санитизация подменяла
    его дефолтом, пользователь терял настройку целиком: сироты вытеснялись,
    хотя вытеснение он выключил.
    """
    launch_root, threads_dir = roots
    snapshots_dir = launch_root / ".gc" / "agent_edits"
    snapshots_dir.mkdir(parents=True)
    orphans = [snapshots_dir / f"{'0' * 64}_dead_th{i}.orig" for i in (1, 2)]
    for p in orphans:
        p.write_bytes(b"orphan")

    seen: list[int] = []
    real_evict = SnapshotStore.evict_lru

    def spying_evict(self, max_files):
        seen.append(max_files)
        return real_evict(self, max_files)

    monkeypatch.setattr(SnapshotStore, "evict_lru", spying_evict)

    target = launch_root / "foo.py"
    target.write_bytes(b"line1\n")
    await record_edit(
        _ctx_with_limit(launch_root, threads_dir, 0),
        abs_path=str(target),
        old_bytes=b"line1\n",
        new_bytes=b"line1x\n",
        op="edit",
        call_id="call-1",
    )

    assert seen == [0]
    assert all(p.exists() for p in orphans)


async def test_eviction_runs_once_per_file_not_on_every_edit(roots, monkeypatch):
    """evict_lru обходит каталог целиком — платить обходом за каждую правку нельзя."""
    launch_root, threads_dir = roots
    target = launch_root / "foo.py"
    target.write_bytes(b"line1\n")
    ctx = _ctx_with_limit(launch_root, threads_dir, 500)

    calls: list[int] = []
    monkeypatch.setattr(
        SnapshotStore, "evict_lru", lambda self, max_files: calls.append(max_files)
    )

    for new in (b"line2\n", b"line3\n"):
        await record_edit(
            ctx,
            abs_path=str(target),
            old_bytes=b"line1\n",
            new_bytes=new,
            op="edit",
            call_id="call-1",
        )

    assert calls == [500]   # только первая правка записала session-снапшот


async def test_session_snapshot_carries_head_rev_in_git_project(roots, monkeypatch):
    """Признак ступени HEAD пишется вместе со снимком (§3.2) и БЕЗ подпроцесса
    git: `rev-parse` на каждом первом контакте с файлом — налог на горячий путь
    fs-тулов (ограничение фикса C1)."""
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    launch_root, threads_dir = roots
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
    ):
        subprocess.run(["git", *args], cwd=str(launch_root), check=True,
                       capture_output=True)
    target = launch_root / "foo.py"
    target.write_bytes(b"line1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(launch_root), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=str(launch_root), check=True,
                   capture_output=True)

    from gc_core.agent_edits import git_ops
    monkeypatch.setattr(
        git_ops, "run_git",
        lambda *a, **kw: pytest.fail("рекордер не должен звать git подпроцессом"),
    )

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=b"line1\n",
        new_bytes=b"line1\nline2\n",
        op="edit",
        call_id="call-1",
    )

    store = _snapshot_store(launch_root, threads_dir)
    path_key = canonical_path_key(str(target), str(launch_root))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(launch_root),
                          check=True, capture_output=True).stdout.decode().strip()
    assert store.session_head_rev(path_key) == head


async def test_session_snapshot_has_no_head_rev_outside_git(roots):
    """Обычная папка — признака нет: ступень HEAD не появится ни у кого."""
    launch_root, threads_dir = roots
    target = launch_root / "foo.py"
    target.write_bytes(b"line1\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=b"line1\n",
        new_bytes=b"line1x\n",
        op="edit",
        call_id="call-1",
    )

    store = _snapshot_store(launch_root, threads_dir)
    assert store.session_head_rev(
        canonical_path_key(str(target), str(launch_root))
    ) is None


async def test_created_file_gets_the_creation_mark_with_head_rev(roots):
    """Д1: снимка у созданного файла нет, ревизию ступени HEAD несёт отметка."""
    launch_root, threads_dir = roots
    head = _git_repo_with_seed_commit(launch_root)
    target = launch_root / "new.py"

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=None,
        new_bytes=b"hello\n",
        op="create",
        call_id="call-1",
    )

    store = _snapshot_store(launch_root, threads_dir)
    path_key = canonical_path_key(str(target), str(launch_root))
    assert store.created_head_rev(path_key) == head
    assert store.session_baseline(path_key) is None   # «пустая база не хранится»


async def test_create_over_a_path_with_a_snapshot_keeps_the_honest_base(roots):
    """Щель `append_to_file` (финальное ревью ветки, I2): существующему файлу
    НУЛЕВОГО размера тул ставит `is_create` (там решается BOM, а не история
    пути) и `old_bytes` не поднимает — сюда приходит `op="create"` по пути,
    который существовал.

    Если путь усекли шелл-командой раньше в этой же сессии, честный `.orig`
    уже снят, и отметка создания перебила бы его: лестница считала бы базу
    пустотой, имея на руках настоящее «до».
    """
    launch_root, threads_dir = roots
    _git_repo_with_seed_commit(launch_root)
    target = launch_root / "notes.md"
    store = _snapshot_store(launch_root, threads_dir)
    path_key = canonical_path_key(str(target), str(launch_root))
    store.capture_session(path_key, b"user work\n", head_rev="a" * 40)
    target.write_bytes(b"")                  # шелл-команда усекла файл

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=None,                      # `size == 0` — тул «до» не поднимает
        new_bytes=b"appended\n",
        op="create",
        call_id="call-1",
    )

    assert store.created_head_rev(path_key) is None
    assert store.session_baseline(path_key) == b"user work\n"
    assert store.session_head_rev(path_key) == "a" * 40


async def test_write_over_existing_file_without_old_bytes_is_not_a_creation(roots):
    """`old_bytes is None` — ещё не «файл создан»: перезапись существующего
    файла сверх капа мутирующих тулов приходит ровно такой же (см.
    `write_to_file_tool`/`save_base64_file`), но с `op="write"`.

    Отметка создания здесь объявила бы базу пути пустой, и файл, живший до
    сессии — возможно, грязный, — получил бы ступень HEAD: ровно тот инвариант,
    который запрещено нарушать.
    """
    launch_root, threads_dir = roots
    _git_repo_with_seed_commit(launch_root)
    target = launch_root / "big.txt"
    target.write_bytes(b"huge existing content\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=None,          # сверх капа: старый контент в память не поднимали
        new_bytes=b"overwritten\n",
        op="write",              # файл СУЩЕСТВОВАЛ — вызывающий это знает
        call_id="call-1",
    )

    store = _snapshot_store(launch_root, threads_dir)
    assert store.created_head_rev(
        canonical_path_key(str(target), str(launch_root))
    ) is None


async def test_partial_append_beyond_cap_is_not_a_creation(roots):
    """`partial` — правка файла, чьё «до» в память не поднимали: старого
    содержимого нет, но файл БЫЛ. Отметка создания тут соврала бы о пустой
    базе — и откат вернул бы пользователю пустоту вместо его файла."""
    launch_root, threads_dir = roots
    target = launch_root / "big.txt"
    target.write_bytes(b"huge existing content\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=None,
        new_bytes=b"appended\n",
        op="append",
        call_id="call-1",
        partial=True,
    )

    store = _snapshot_store(launch_root, threads_dir)
    assert store.created_head_rev(
        canonical_path_key(str(target), str(launch_root))
    ) is None


async def test_snapshot_failure_does_not_swallow_the_journal_record(roots, monkeypatch):
    """Отказ снимка базы стоит ОТКАТА, а не видимости правки.

    Снимок снимается ДО записи в журнал, и исключение там обрывало всю
    функцию: правка исчезала из ленты, с экрана правок и из счётчиков хода
    разом — молча, потому что внешний `except` рекордера глотает всё. Живой
    случай, пойманный смоком: путь снапшота длиннее предела ОС (на Windows
    MAX_PATH 260, а одно имя файла снимка — 110 знаков).
    """
    def _boom(self, *a, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(SnapshotStore, "capture_session", _boom)

    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    old_bytes = b"line1\nline2\n"
    new_bytes = b"line1\nline2x\n"
    target.write_bytes(old_bytes)

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        op="edit",
        call_id="call-1",
    )

    recs = _journal(threads_dir).read_all()
    assert [(r.path, r.op, r.call_id) for r in recs] == [("src/foo.py", "edit", "call-1")]
    assert (recs[0].add, recs[0].del_) == (1, 1)


async def test_snapshot_of_the_turn_failing_does_not_cancel_eviction(roots, monkeypatch):
    """Снимок сессии лёг, а следующий за ним снимок хода упал.

    Обработчик не вправе обнулять признак записи: эвикция цепляется именно к
    нему («снимок реально записан»), и без неё хранилище перерастает свой
    лимит — тихо и навсегда.
    """
    calls = []

    def _boom(self, *a, **kw):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(SnapshotStore, "capture_turn", _boom)
    monkeypatch.setattr(SnapshotStore, "evict_lru", lambda self, limit: calls.append(limit))

    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    target.write_bytes(b"old\n")

    await record_edit(
        _ctx(launch_root, threads_dir),
        abs_path=str(target),
        old_bytes=b"old\n",
        new_bytes=b"new\n",
        op="edit",
        call_id="call-1",
    )

    assert [r.path for r in _journal(threads_dir).read_all()] == ["src/foo.py"]
    assert calls, "эвикция обязана быть вызвана: снимок сессии записан"


async def test_garbage_source_cap_in_config_does_not_kill_the_snapshot(roots):
    """Мусорный `max_diff_source_bytes` не смеет отнимать право на откат.

    Схема пропускает отрицательное значение, а без санитизации оно помечало
    КАЖДУЮ правку `no_diff`: снимка нет, откат мёртв весь сеанс — при том что
    экран изменений читает тот же ключ со своим клампом и показывает строки,
    будто всё в порядке.
    """
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    target.write_bytes(b"old\n")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["agent_edits"] = {"max_diff_source_bytes": -1}

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=b"old\n",
        new_bytes=b"new\n",
        op="edit",
        call_id="call-1",
    )

    rec = _journal(threads_dir).read_all()[0]
    path_key = canonical_path_key(str(target), str(launch_root))
    assert rec.flags == []
    assert rec.hunks.endswith("@@ -1 +1 @@\n-old\n+new\n")
    assert _snapshot_store(launch_root, threads_dir).session_baseline(path_key) == b"old\n"


async def test_non_numeric_limit_in_config_does_not_lose_the_record(roots):
    """Нечисловое значение роняло `int()` внутри записи, а внешний
    `except Exception` глотал исключение — правка исчезала из ленты и счётчиков."""
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    target.write_bytes(b"old\n")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["agent_edits"] = {"hunk_context_lines": "три"}

    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=b"old\n",
        new_bytes=b"new\n",
        op="edit",
        call_id="call-1",
    )

    assert [r.path for r in _journal(threads_dir).read_all()] == ["src/foo.py"]


async def test_tool_edit_after_a_shell_edit_does_not_pass_for_the_session_start(roots):
    """Правка тулом после шелл-команды не выдаёт состояние после команды за начало сессии.

    Команда оставила пути счётную базу (путь был грязен — точного «до» нет).
    Рекордер снимал `.orig` поверх неё — байты уже ПОСЛЕ команды, — и откат «к
    началу сессии» оставлял правки команды на диске."""
    launch_root, threads_dir = roots
    target = launch_root / "src" / "foo.py"
    target.parent.mkdir()
    target.write_bytes(b"after-command\n")
    ctx = _ctx(launch_root, threads_dir)
    store = _snapshot_store(launch_root, threads_dir)
    store.capture_shell_session("src/foo.py", b"index-before\n")

    await record_edit(ctx, abs_path=str(target), old_bytes=b"after-command\n",
                      new_bytes=b"after-tool\n", op="edit", call_id="c1")

    assert _snapshot_store(launch_root, threads_dir).session_baseline("src/foo.py") is None

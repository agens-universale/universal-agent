"""ShellDelta: git-делта вокруг shell-команды → записи в журнал правок.

Тесты идут на настоящем временном git-репозитории (`git init` в tmp_path):
git не мокается — иначе не проверить ни quotepath, ни `--no-index`, ни хуки.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gc_core.agent_edits import git_ops, shell_delta
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.shell_delta import ShellDelta
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.agent_edits.tracking import TrackingFsBackend
from gc_core.config_modules import env_vars
from gc_core.thread_store import edits_journal_path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _marker_script(root: Path, marker: Path) -> str:
    """sh-скрипт, который отмечается в файле: подсовываем git как драйвер."""
    script = root.parent / f"{marker.stem}.sh"
    script.write_text(
        f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\ncat "$1"\n',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(script, 0o755)
    return script.as_posix()


def _run_command(root: Path, code: str, *, returncode: int = 0) -> None:
    """Shell-команда агента: отдельный процесс, реальные правки на диске."""
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(root), capture_output=True)
    assert proc.returncode == returncode, proc.stderr.decode("utf-8", "replace")


def _link_dir_for_platform(link: Path, target: Path) -> bool:
    """Ссылка на каталог, доступная БЕЗ административных прав: junction на
    Windows (`_winapi.CreateJunction`, работает без повышенных прав), symlink
    — на остальных платформах. ``False`` — среда не позволяет ни то ни другое."""
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, ImportError, NotImplementedError, AttributeError):
        return False


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """launch_root = git-репо с одним закоммиченным файлом; threads_dir — рядом."""
    if not __import__("shutil").which("git"):
        pytest.skip("git недоступен")
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    _git(launch_root, "init", "-b", "main")
    _git(launch_root, "config", "user.email", "test@example.com")
    _git(launch_root, "config", "user.name", "test")
    _git(launch_root, "config", "core.autocrlf", "false")
    _git(launch_root, "config", "core.quotepath", "true")  # дефолт git — пиним явно
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "init")
    return launch_root, threads_dir


def _ctx(launch_root, threads_dir, *, shell_tracking=True, turn_id="turn-1"):
    return SimpleNamespace(
        config_dict={
            "launch_root": str(launch_root),
            "diff_tui": {"shell_tracking": shell_tracking},
        },
        threads_dir=str(threads_dir),
        system_id="cli",
        thread_id="th1",
        turn_id=turn_id,
    )


def _records(threads_dir, *, system_id="cli", thread_id="th1"):
    return EditsJournal(edits_journal_path(str(threads_dir), system_id, thread_id)).read_all()


def _snapshot_store(launch_root, *, system_id="cli", thread_id="th1") -> SnapshotStore:
    """Стор того же треда, что пишет канал (thread_key = имя журнала без префикса)."""
    return SnapshotStore(snapshots_root(str(launch_root)), f"{system_id}_{thread_id}")


def _snapshot_names(launch_root, suffix: str) -> list[str]:
    root = snapshots_root(str(launch_root))
    return sorted(p.name for p in root.glob(f"*{suffix}")) if root.is_dir() else []


def _seed_known_file(
    threads_dir,
    launch_root,
    name: str,
    content: bytes,
    *,
    ts: float,
    system_id: str = "cli",
    thread_id: str = "th1",
) -> None:
    """Симулирует «файл уже в наборе»: запись в журнале + совпадающий диск.

    Карманный подканал видит путь как кандидата только если журнал уже про
    него знает (см. докстринг ``shell_delta``) — реальная предыстория (правка
    тулом или более ранней командой) тут не нужна, важен только сам факт
    записи и совпадение содержимого на диске.
    """
    (launch_root / name).write_bytes(content)
    journal_path = edits_journal_path(str(threads_dir), system_id, thread_id)
    EditsJournal(journal_path).append(
        EditRecord(
            path=name,
            turn_id="turn-0",
            ts=ts,
            call_id="seed",
            op="write",
            hunks="",
            add=0,
            del_=0,
            before_sha256=None,
            after_sha256=hashlib.sha256(content).hexdigest(),
            flags=[],
        )
    )


def test_command_creating_untracked_file_records_create_with_hunks(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\nbeta\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["new.txt"]
    rec = recs[0]
    assert rec.op == "create"
    assert rec.turn_id == "turn-1"
    assert rec.call_id is None
    assert rec.before_sha256 is None
    assert rec.after_sha256 == hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    assert "+alpha" in rec.hunks
    assert "+beta" in rec.hunks
    assert rec.add == 2
    assert rec.del_ == 0
    assert rec.flags == []


def test_command_modifying_tracked_file_records_shell_with_hunks(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["tracked.txt"]
    rec = recs[0]
    assert rec.op == "shell"
    assert "-line2\n" in rec.hunks
    assert "+line2x\n" in rec.hunks
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.after_sha256 == hashlib.sha256(b"line1\nline2x\n").hexdigest()


def test_command_staging_its_own_change_is_recorded(repo):
    """`… && git add` двигает и индекс, и дерево: дифф от индекса пуст.

    Путь был чист до команды, HEAD не сдвинулся — «до» читается из HEAD, и
    правка видна независимо от того, попала она в индекс или нет.
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; open('tracked.txt','wb').write(b'line1\\nline2x\\n'); "
        "subprocess.run(['git','add','tracked.txt'], check=True)",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["tracked.txt"]
    rec = recs[0]
    assert rec.op == "shell"
    assert "-line2\n" in rec.hunks
    assert "+line2x\n" in rec.hunks
    assert rec.add == 1
    assert rec.del_ == 1


def test_command_creating_and_staging_a_file_records_create(repo):
    """Созданный и сразу проиндексированный файл в status помечен ``A``, не ``??``.

    В HEAD его нет — это создание: базы у него быть не должно.
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; open('new.txt','wb').write(b'alpha\\n'); "
        "subprocess.run(['git','add','new.txt'], check=True)",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["new.txt"]
    rec = recs[0]
    assert rec.op == "create"
    assert "+alpha" in rec.hunks
    assert rec.add == 1
    assert rec.del_ == 0


def test_command_only_staging_a_foreign_change_is_not_recorded(repo):
    """`git add` без правки содержимого — не правка команды.

    Путь был грязен ДО команды: ручную правку пользователя канал в журнал не
    пишет, как бы команда ни двигала индекс.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nhand\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; subprocess.run(['git','add','tracked.txt'], check=True)",
    )
    delta.after_and_record()

    assert _records(threads_dir) == []


def test_bracketed_path_does_not_pull_a_foreign_file_into_its_diff(repo):
    """Без магии pathspec `git diff -- "f[ab].txt"` матчит ещё и `fa.txt`.

    Чужой файл лёг бы в запись журнала под этим путём — с чужим содержимым на
    экране и раздутыми счётчиками хода.
    """
    launch_root, threads_dir = repo
    (launch_root / "f[ab].txt").write_bytes(b"one\n")
    (launch_root / "fa.txt").write_bytes(b"two\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "brackets")
    # чужая ручная правка ДО команды: без магии она утекла бы в дифф соседа
    (launch_root / "fa.txt").write_bytes(b"two-hand\n")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('f[ab].txt','wb').write(b'one-x\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["f[ab].txt"]
    rec = recs[0]
    assert "fa.txt" not in rec.hunks
    assert rec.add == 1
    assert rec.del_ == 1


def test_conflicted_merge_by_command_is_not_a_creation(repo):
    """Неслитый путь (`AA`/`AU`) в HEAD ЕСТЬ — созданием он быть не может.

    Порода конфликта тоже начинается с `A`, и проверка по первой букве делала
    из конфликтного слияния «создание»: откат такого пути удаляет файл, не
    спрашивая базу, — то есть сносит закоммиченную работу пользователя.
    """
    launch_root, threads_dir = repo
    _git(launch_root, "checkout", "-b", "other")
    (launch_root / "both.txt").write_bytes(b"theirs\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "theirs")
    _git(launch_root, "checkout", "main")
    (launch_root / "both.txt").write_bytes(b"ours\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "ours")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; subprocess.run(['git','merge','other'])",
    )
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.path == "both.txt"]
    assert recs, "конфликтный путь обязан попасть в журнал"
    assert [r.op for r in recs] == ["shell"], "создание — это удаление при откате"


def test_command_deleting_tracked_file_records_delete(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "import os; os.remove('tracked.txt')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["tracked.txt"]
    rec = recs[0]
    assert rec.op == "delete"
    assert rec.hunks == ""
    assert rec.flags == ["no_diff"]
    assert rec.after_sha256 is None
    assert rec.add == 0
    assert rec.del_ == 0


def test_command_deleting_untracked_file_records_delete(repo):
    """Путь исчез из status (обратная сторона делты), а не появился в нём."""
    launch_root, threads_dir = repo
    (launch_root / "scratch.txt").write_bytes(b"tmp\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "import os; os.remove('scratch.txt')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["scratch.txt"]
    assert recs[0].op == "delete"
    assert recs[0].flags == ["no_diff"]


def test_cyrillic_path_from_quoted_porcelain_lands_in_journal(repo):
    """Регресс на core.quotepath: путь приходит как "\\320\\262..." — разэскейпить."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    # имя файла в коде команды — \u-эскейпами: аргумент процесса не зависит от локали
    _run_command(
        launch_root,
        "open('\\u0442\\u0435\\u0441\\u0442 \\u0444\\u0430\\u0439\\u043b.txt','wb')"
        ".write('\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442\\n'.encode('utf-8'))",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["тест файл.txt"]
    assert recs[0].op == "create"
    assert recs[0].add == 1
    assert "привет" in recs[0].hunks


def test_failing_command_still_records_changes(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "open('boom.txt','wb').write(b'partial\\n'); raise SystemExit(3)",
        returncode=3,
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["boom.txt"]
    assert recs[0].op == "create"


def test_repeated_change_of_already_dirty_file_is_recorded(repo):
    """Файл был грязным и до команды: код в status не меняется — ловим по содержимому.

    Числа идут от НАСТОЯЩЕГО «до», снятого байтами перед командой: дописана
    одна строка, значит `+1`. Прежде такой путь считался от пустоты и давал
    `+2` — весь файл как написанный командой.
    """
    launch_root, threads_dir = repo
    (launch_root / "notes.txt").write_bytes(b"first\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('notes.txt','ab').write(b'second\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["notes.txt"]
    assert recs[0].add == 1
    assert "+second" in recs[0].hunks


def test_renamed_path_is_recorded_under_its_new_name(repo):
    """`R old -> new` в porcelain: путь записи — новый, а не вся строка целиком."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; "
        "subprocess.run(['git','mv','tracked.txt','renamed.txt'], check=True); "
        "open('renamed.txt','ab').write(b'line3\\n')",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["renamed.txt"]
    assert recs[0].op == "shell"
    assert "+line3" in recs[0].hunks


def test_command_without_changes_writes_nothing(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "print('nothing to see')")
    delta.after_and_record()

    assert _records(threads_dir) == []


def test_same_content_rewrite_is_not_recorded_twice(repo):
    """Команда переписала untracked-файл теми же байтами: mtime новый, правки нет.

    У untracked дифф с /dev/null никогда не пуст, поэтому дубль ловится только
    по хэшу содержимого — карта живёт у вызывающего, между командами.
    """
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    seen: dict[str, str] = {}

    for _ in range(2):
        delta = ShellDelta.begin(ctx, seen=seen)
        _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
        delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == ["new.txt"]


def test_different_content_rewrite_is_recorded_again(repo):
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    seen: dict[str, str] = {}

    delta = ShellDelta.begin(ctx, seen=seen)
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
    delta.after_and_record()

    delta = ShellDelta.begin(ctx, seen=seen)
    _run_command(launch_root, "open('new.txt','wb').write(b'beta\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["new.txt", "new.txt"]
    assert recs[0].after_sha256 != recs[1].after_sha256


def test_recreated_path_after_delete_is_recorded_again(repo):
    """Удаление снимает путь с дедупликации: повторное создание — снова запись."""
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    seen: dict[str, str] = {}

    for code in (
        "open('new.txt','wb').write(b'alpha\\n')",
        "import os; os.remove('new.txt')",
        "open('new.txt','wb').write(b'alpha\\n')",
    ):
        delta = ShellDelta.begin(ctx, seen=seen)
        _run_command(launch_root, code)
        delta.after_and_record()

    assert [r.op for r in _records(threads_dir)] == ["create", "delete", "create"]


async def test_wrapper_keeps_dedup_map_across_commands(repo):
    """Карта живёт на обёртке backend'а — она одна на ctx и переживает команды."""
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)

    class _Inner:  # минимальный backend: aexec запускает настоящую команду
        is_sandbox = False
        default_exec_timeout = None

        # Без `capture_max_chars` намеренно: бэкенд без этого параметра обязан
        # продолжать работать, и `_capture_kw` обёртки ровно это и обещает.
        async def aexec(self, command, *, cwd=None, timeout=None, env=None):
            _run_command(Path(cwd), command)

    wrapper = TrackingFsBackend(_Inner(), ctx)
    for _ in range(2):
        await wrapper.aexec(
            "open('new.txt','wb').write(b'alpha\\n')", cwd=str(launch_root)
        )

    assert [r.path for r in _records(threads_dir)] == ["new.txt"]


def test_failed_before_snapshot_never_records_foreign_dirt(repo, monkeypatch):
    """Отказ снимка «до» ≠ пустой репозиторий: чужая грязь не наша правка.

    До Р33 отказ git-снимка убивал объект целиком (``begin()`` → ``None``).
    Теперь объект живёт (карманный подканал не зависит от git), а газует
    именно git-подканал: ``_before`` остаётся ``None``, и ниже это явно
    проверяется. "other.txt" при этом не в журнале (свежий тред) — карманному
    подканалу тоже нечего снимать, так что исходная гарантия (чужая грязь не
    попадает в записи как правка команды) сохраняется целиком.
    """
    launch_root, threads_dir = repo
    (launch_root / "other.txt").write_bytes(b"user work in progress\n")
    ctx = _ctx(launch_root, threads_dir)

    # честный отказ: настоящий git с невыполнимым таймаутом, протокол не мокаем
    monkeypatch.setattr(git_ops, "GIT_TIMEOUT_S", 0.000001)
    failed = ShellDelta.begin(ctx)
    assert failed is not None
    assert failed._before is None

    delta = ShellDelta(str(launch_root), ctx)
    delta.before()  # снимок «до» не снялся
    monkeypatch.setattr(git_ops, "GIT_TIMEOUT_S", 15)  # git снова отвечает
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
    delta.after_and_record()

    assert _records(threads_dir) == []


def test_external_diff_driver_is_not_executed(repo):
    """`diff.external` из конфига репо не должен исполняться нашим диффом."""
    launch_root, threads_dir = repo
    marker = launch_root.parent / "ext-diff-ran.txt"
    _git(launch_root, "config", "diff.external", _marker_script(launch_root, marker))

    # контроль: обычный git diff драйвер зовёт — иначе гвард ничего не доказывает
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-control\n")
    subprocess.run(["git", "diff", "--", "tracked.txt"], cwd=str(launch_root), capture_output=True)
    if not marker.exists():
        pytest.skip("git не зовёт diff.external в этой среде — гвард непроверяем")
    marker.unlink()

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2-safe\\n')")
    delta.after_and_record()

    assert not marker.exists()
    assert [r.path for r in _records(threads_dir)] == ["tracked.txt"]
    assert "+line2-safe" in _records(threads_dir)[0].hunks


def test_textconv_driver_is_not_executed(repo):
    """`diff.<driver>.textconv` из конфига репо не должен исполняться."""
    launch_root, threads_dir = repo
    marker = launch_root.parent / "textconv-ran.txt"
    _git(launch_root, "config", "diff.marker.textconv", _marker_script(launch_root, marker))
    (launch_root / ".gitattributes").write_text(
        "*.txt diff=marker\n", encoding="utf-8", newline="\n"
    )

    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-control\n")
    subprocess.run(["git", "diff", "--", "tracked.txt"], cwd=str(launch_root), capture_output=True)
    if not marker.exists():
        pytest.skip("git не зовёт textconv в этой среде — гвард непроверяем")
    marker.unlink()

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2-safe\\n')")
    delta.after_and_record()

    assert not marker.exists()
    assert "tracked.txt" in [r.path for r in _records(threads_dir)]


def test_touch_without_content_change_writes_nothing(repo):
    """Изменился только mtime: git разницы не видит — записи быть не должно."""
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2x\n")
    _git(launch_root, "add", "tracked.txt")  # worktree == index: git diff пуст
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "import os; os.utime('tracked.txt', (1000000, 1000000))")
    delta.after_and_record()

    assert _records(threads_dir) == []


def test_recreated_file_is_recorded_again_after_it_disappeared(repo):
    """Файл исчез между командами — память о нём больше не действует.

    Дедуп помнит хэш того, что команда записала, и живёт всю сессию: у
    неотслеживаемого пути дифф с /dev/null никогда не пуст, и повтор ловится
    только по хэшу. Но откат на экране изменений удаляет файл МИМО этого
    канала, и память оставалась. Повторный запуск той же команды давал байт в
    байт то же содержимое, запись отбрасывалась как дубль, и файла не было ни в
    сводке хода, ни на вкладке «Ход», и откат хода его не трогал (пятый раунд
    ревью, находка 4).
    """
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    seen: dict = {}

    delta = ShellDelta.begin(ctx, seen=seen)
    _run_command(launch_root, "open('gen.py','wb').write(b'print(1)')")
    delta.after_and_record()
    assert [(r.path, r.op) for r in _records(threads_dir)] == [("gen.py", "create")]

    (launch_root / "gen.py").unlink()          # откат на экране: файл удалён мимо канала

    delta = ShellDelta.begin(ctx, seen=seen)
    _run_command(launch_root, "open('gen.py','wb').write(b'print(1)')")
    delta.after_and_record()

    assert [(r.path, r.op) for r in _records(threads_dir)] == [
        ("gen.py", "create"), ("gen.py", "create"),
    ]


def test_binary_file_follows_recorder_contract(repo):
    """Бинарь: хэнков нет, флаг `no_diff` есть, а ВИД правки остаётся своим.

    Команда создала файл — значит `create`, и неважно, что содержимое двоичное.
    Прежде вид затирался на `binary`, `created_here` создания не видел, и
    созданный бинарник показывался как «изменён» без права на откат (пятый
    раунд ревью, находка 5).
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('blob.bin','wb').write(b'\\x00\\x01\\x02binary')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["blob.bin"]
    rec = recs[0]
    assert rec.op == "create"
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.add == 0
    assert rec.del_ == 0
    assert rec.after_sha256 == hashlib.sha256(b"\x00\x01\x02binary").hexdigest()


def test_file_over_source_cap_is_recorded_without_diffing(repo, monkeypatch):
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["max_diff_source_bytes"] = 10
    diff_calls: list[str] = []

    def _spy_diff(self, rel, *, untracked):
        diff_calls.append(rel)
        return ""

    monkeypatch.setattr(shell_delta.ShellDelta, "_diff", _spy_diff)
    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('big.txt','wb').write(b'x' * 100)")
    delta.after_and_record()

    assert diff_calls == []
    rec = _records(threads_dir)[0]
    assert rec.path == "big.txt"
    # Файл сверх капа создан командой — и это тот же случай, что у бинаря:
    # диффа нет, но создание терять нельзя.
    assert rec.op == "create"
    assert rec.flags == ["no_diff"]
    assert rec.hunks == ""
    assert rec.add == 0
    assert rec.del_ == 0


def test_huge_diff_is_truncated_with_flag_but_counts_stay_accurate(repo):
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["max_diff_chars"] = 40
    delta = ShellDelta.begin(ctx)
    _run_command(
        launch_root,
        "open('many.txt','wb').write(b''.join(b'line-%d\\n' % i for i in range(50)))",
    )
    delta.after_and_record()

    rec = _records(threads_dir)[0]
    assert rec.flags == ["truncated"]
    assert len(rec.hunks) == 40
    assert rec.add == 50


def test_too_many_paths_are_capped(repo, monkeypatch):
    launch_root, threads_dir = repo
    monkeypatch.setattr(shell_delta, "MAX_PATHS_PER_COMMAND", 1)
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "open('a.txt','wb').write(b'a\\n'); open('b.txt','wb').write(b'b\\n')",
    )
    delta.after_and_record()

    records = _records(threads_dir)
    assert [r.path for r in records] == ["a.txt"]
    # Экрану изменений — чем признаться в неполноте списка (строка на F3).
    assert [r.command_paths for r in records] == [2]


def test_a_command_under_the_cap_is_not_marked(repo):
    """Команда в пределах потолка своих записей не метит: список полон."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('a.txt','wb').write(b'a\\n')")
    delta.after_and_record()

    assert [r.command_paths for r in _records(threads_dir)] == [None]


def test_shell_tracking_false_writes_nothing(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir, shell_tracking=False))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")

    assert delta is None
    assert _records(threads_dir) == []


def test_channel_off_without_thread_identity(repo):
    launch_root, _threads_dir = repo
    ctx = SimpleNamespace(config_dict={"launch_root": str(launch_root)})

    assert ShellDelta.begin(ctx) is None


def test_git_subchannel_inactive_outside_git_repo(tmp_path, monkeypatch):
    """Вне git-репозитория объект живёт (карманный подканал), git-часть молчит.

    До Р33 канал был чисто git-механизмом, и вне репозитория ``begin()``
    возвращал ``None`` — тест так и назывался. Теперь объект живёт при
    ``is_recording ∧ shell_tracking`` независимо от git (см. докстринг
    модуля): карманный подканал работает и в cache-mode. Смысл прежнего теста
    — «git-делта без ``.git`` не работает» — сохраняется полями ``_before``/
    ``_head_before``: они остаются ``None``, git ни разу не вызывается.
    """
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))

    assert delta is not None
    assert delta._before is None
    assert delta._head_before is None


def test_repo_hooks_are_not_executed(repo):
    """Хуки репозитория не должны исполняться нашими git-вызовами.

    Триггер выбран так, чтобы гвард что-то доказывал: у чистого файла обновляем
    mtime — тогда `git status` обязан переписать закэшированный stat в индексе,
    а запись индекса зовёт post-index-change (контроль ниже это подтверждает).
    """
    launch_root, threads_dir = repo
    marker = launch_root.parent / "hook-ran.txt"  # вне репо: иначе сам попадёт в status
    hook = launch_root / ".git" / "hooks" / "post-index-change"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(hook, 0o755)

    # контроль: обычный git status хук выполняет — иначе гвард ничего не доказывает
    os.utime(launch_root / "tracked.txt", None)
    subprocess.run(
        ["git", "status", "--porcelain", "-u"], cwd=str(launch_root), capture_output=True
    )
    if not marker.exists():
        pytest.skip("git не выполняет post-index-change в этой среде — гвард непроверяем")
    marker.unlink()

    os.utime(launch_root / "tracked.txt", None)  # тот же триггер для наших вызовов
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
    delta.after_and_record()

    assert not marker.exists()
    assert [r.path for r in _records(threads_dir)] == ["new.txt"]


# --- счётная база и признак создания (Р12, Р13) ---


def test_command_created_file_gets_create_record_and_no_snapshot(repo):
    """Пути не было в before И после команды он `??` → создан этой командой.

    Законность пустой базы выражается записью op="create" (правило
    `created_here` в deltas), а не файлом нулевой длины на диске.
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\nbeta\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("new.txt", "create")]
    assert recs[0].add == 2
    assert _snapshot_names(launch_root, ".gitorig") == []
    assert _snapshot_names(launch_root, ".gitturn") == []


def test_preexisting_untracked_file_is_not_treated_as_created(repo):
    """`??` метит ЛЮБОЙ неотслеживаемый файл, а не только созданный командой."""
    launch_root, threads_dir = repo
    (launch_root / "notes.txt").write_bytes(b"first\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('notes.txt','ab').write(b'second\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("notes.txt", "shell")]
    # В индексе пути нет, зато байты сняты ПЕРЕД командой: база точная и
    # честная — «first», а не пустота.
    assert _snapshot_store(launch_root).session_baseline("notes.txt") == b"first\n"


def test_clean_tracked_file_modified_by_command_is_not_created(repo):
    """Чистого tracked-файла в `git status` нет вовсе: «нет в before» ещё не создание."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("tracked.txt", "shell")]


def test_dirty_before_command_gets_a_precise_base_from_real_bytes(repo):
    """Грязный до команды путь получает ТОЧНУЮ базу — снятую байтами, не из индекса.

    Индекс несохранённых правок дерева не содержит: база оттуда врала бы о
    «до», и откат записал бы содержимое индекса поверх работы пользователя.
    Поэтому байты таких путей снимаются ПЕРЕД командой, и база у них честная —
    а значит и откат доступен.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-dirty\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    assert store.session_baseline("tracked.txt") == b"line1\nline2-dirty\n"
    assert store.turn_baseline("tracked.txt", "turn-1") == b"line1\nline2-dirty\n"
    assert store.has_session_baseline("tracked.txt") is True


def test_protected_path_dirty_before_command_stays_out_of_the_pocket(repo):
    """Защищённый от отслеживания путь в карман не попадает, даже будучи грязным.

    Кандидаты кармана приходят из двух источников: из журнала (там ключи уже
    прошли `canonical_path_key`) и из `git status` — сырыми строками porcelain.
    Второй источник фильтра не знал, и вместе с записью карман клал ТОЧНУЮ
    базу, а она даёт право на откат: `r` записал бы байты по пути, который
    экран трогать не вправе.
    """
    launch_root, threads_dir = repo
    (launch_root / ".gc").mkdir()
    (launch_root / ".gc" / "notes.txt").write_bytes(b"secret\n")   # ?? в porcelain

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('.gc/notes.txt','wb').write(b'changed\\n')")
    delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == []
    assert _snapshot_names(launch_root, ".orig") == []


def test_symlink_out_of_the_perimeter_stays_out_of_the_pocket(repo, tmp_path):
    """Ссылка наружу — тот же фильтр: `canonical_path_key` резолвит цель.

    `is_file()` идёт ПО ссылке, и `read_bytes()` прочитал бы чужой файл за
    периметром, а точная база сделала бы его откатываемым — то есть запись
    наружу по нажатию `r`.

    Держится это НЕ периметром (после снятия `extra_paths` ссылка наружу
    получает абсолютный ключ, а не `None`), а фильтром кандидатов кармана и
    гвардом `_build_record`.
    """
    launch_root, threads_dir = repo
    outside = tmp_path / "outside.txt"
    outside.write_text("чужое\n", encoding="utf-8")
    try:
        os.symlink(str(outside), str(launch_root / "link.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("симлинки недоступны в этой среде")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('link.txt','wb').write(b'changed\\n')")
    delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == []
    assert _snapshot_names(launch_root, ".orig") == []


def test_external_path_known_to_the_journal_stays_out_of_the_pocket(repo, tmp_path):
    """У кармана ДВА источника кандидатов, и фильтр нужен обоим.

    Первый — грязные пути `_at_risk_keys` (сама она внешние ключи больше не
    отсекает, см. её докстринг, — фильтрует только слияние в `_pocket_before`),
    второй — пути, ИЗВЕСТНЫЕ ЖУРНАЛУ. У второго источника фильтра не было и не
    требовалось, пока внешний ключ в журнал попасть не мог. Теперь может — его
    кладёт рекордер fs-тула, — и без фильтра СЛЕДУЮЩАЯ команда прочитала бы
    чужой файл целиком и положила его точной базой в каталог проекта, то есть
    сделала бы его откатываемым.

    Одной командой регресс не ловится: первая идёт через `_at_risk_keys` и
    выглядит чистой. Поэтому тест засевает журнал и запускает команду — это и
    есть «вторая».
    """
    launch_root, threads_dir = repo
    outside = tmp_path / "outside" / "app.yml"
    outside.parent.mkdir()
    outside.write_bytes(b"a: 1\n")
    key = outside.resolve().as_posix()

    EditsJournal(edits_journal_path(str(threads_dir), "cli", "th1")).append(
        EditRecord(
            path=key, turn_id="turn-0", ts=1.0, call_id="seed", op="write",
            hunks="", add=0, del_=0, before_sha256=None,
            after_sha256=hashlib.sha256(b"a: 1\n").hexdigest(), flags=[],
        )
    )

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, f"open({str(outside)!r},'wb').write(b'a: 2\\n')")
    delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == [key]   # только засев
    assert _snapshot_names(launch_root, ".orig") == []
    assert _snapshot_names(launch_root, ".turn") == []


def test_external_key_reaching_build_record_stays_out_of_the_journal(repo, tmp_path):
    """Единственная доступная связка, которая доводит внешний ключ до гварда
    git-подканала (`_build_record`), а не до кармана.

    `_build_record` зовут только для путей из `changed` — тех, что видит
    ``git status`` ВНУТРИ `launch_root`. Файл, просто лежащий в `tmp_path` (как
    у соседнего теста), туда не попадает вовсе: до `_build_record` он
    физически не доходит, и гвард `is_external_key` в нём был бы недостижим.
    Ссылка внутри репозитория нужна ровно за этим — довести путь до
    `git status` и до `_build_record`; абсолютным ключ делает уже сам факт
    «вне проекта» (периметр снят в задаче 3, `write_guard.extra_paths` на
    отслеживание больше не влияет).

    Карман эту же связку фильтрует своим фильтром на слиянии кандидатов
    (предыдущий тест), но git-подканал строит `rel` заново из собственного
    `git status` и от кармана не зависит — без гварда в `_build_record` он
    записал бы в журнал точную запись под абсолютным путём, с содержимым
    чужого файла внутри диффа.
    """
    launch_root, threads_dir = repo
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "app.yml").write_bytes(b"a: 1\n")

    link = launch_root / "linkdir"
    if not _link_dir_for_platform(link, outside_dir):
        pytest.skip("ссылка на каталог недоступна в этой среде")

    ctx = _ctx(launch_root, threads_dir)

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('linkdir/app.yml','wb').write(b'a: 2\\n')")
    delta.after_and_record()

    assert _records(threads_dir) == []
    assert _snapshot_names(launch_root, ".orig") == []


def test_file_created_by_command_gets_no_precise_snapshot(repo):
    """У созданного командой пути «до» — пустота, и снимать нечего.

    Байты перед командой снимаются только у путей, КОТОРЫЕ УЖЕ ЕСТЬ: у
    созданного файла до команды содержимого не было вовсе, и точная база ему
    не пишется (откат такому пути — удаление, база не спрашивается).
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
    delta.after_and_record()

    assert [(r.path, r.op) for r in _records(threads_dir)] == [("new.txt", "create")]
    assert _snapshot_names(launch_root, ".orig") == []
    assert _snapshot_names(launch_root, ".turn") == []


def test_counting_base_prefers_the_earlier_of_two_bases(repo):
    """Команда тронула файл первой, следом тот же файл правил fs-тул.

    Регресс Р11: дельта команды не вправе исчезнуть из кумулятива. Держится
    он теперь проще, чем выбором из двух баз: команда кладёт ТОЧНУЮ базу с
    настоящим «до», а поздний снимок инструмента её не перезаписывает —
    `capture_session` пишет только при отсутствии. База одна, и она верная.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-dirty\n")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    store.capture_session("tracked.txt", (launch_root / "tracked.txt").read_bytes())

    assert store.session_baseline("tracked.txt") == b"line1\nline2-dirty\n"


def test_counting_base_is_not_written_when_git_show_fails(repo, monkeypatch):
    """Отказ git — soft-fail: запись в журнале есть, базы нет, исключения нет.

    Файл уже грязен ДО команды — счётный путь гарантирован, и мок реально
    бьёт по вызову, который проверяет этот тест.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-dirty\n")

    def _boom(launch_root_arg, rel):
        raise RuntimeError("git недоступен")

    monkeypatch.setattr(git_ops, "show_index_bytes", _boom)
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    assert [(r.path, r.op) for r in _records(threads_dir)] == [("tracked.txt", "shell")]
    assert _snapshot_names(launch_root, ".gitorig") == []


def test_no_snapshot_for_binary_record(repo):
    launch_root, threads_dir = repo
    (launch_root / "blob.bin").write_bytes(b"\x00\x01old")
    _git(launch_root, "add", "blob.bin")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('blob.bin','wb').write(b'\\x00\\x01\\x02new')")
    delta.after_and_record()

    # Файл СУЩЕСТВОВАЛ до команды, значит вид правки — `shell`, а бинарность
    # сказана флагом.
    recs = _records(threads_dir)
    assert [r.op for r in recs] == ["shell"]
    assert recs[0].flags == ["no_diff"]
    assert _snapshot_names(launch_root, ".gitorig") == []


def test_no_snapshot_for_oversize_record(repo):
    launch_root, threads_dir = repo
    (launch_root / "big.txt").write_bytes(b"y" * 100)
    _git(launch_root, "add", "big.txt")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["max_diff_source_bytes"] = 10
    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('big.txt','wb').write(b'x' * 100)")
    delta.after_and_record()

    assert [r.flags for r in _records(threads_dir)] == [["no_diff"]]
    assert _snapshot_names(launch_root, ".gitorig") == []


def test_no_snapshot_for_delete_record(repo):
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "import os; os.remove('tracked.txt')")
    delta.after_and_record()

    assert [r.op for r in _records(threads_dir)] == ["delete"]
    assert _snapshot_names(launch_root, ".gitorig") == []


def test_shell_channel_evicts_when_it_wrote_a_new_base(repo, monkeypatch):
    """Эвикция — только к факту записи базы: она обходит каталог целиком.

    Файл уже грязен ДО команды — обе правки идут счётным путём в одно и то
    же пространство `.gitorig`, и вторая честно застаёт базу уже записанной.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2-dirty\n")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["snapshots_max_files"] = 7
    calls: list[int] = []
    monkeypatch.setattr(
        SnapshotStore, "evict_lru", lambda self, max_files: calls.append(max_files)
    )

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()
    assert calls == [7]

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2y\\n')")
    delta.after_and_record()
    assert calls == [7]  # база уже есть — второго обхода каталога нет


def _age_file(path: Path, seconds: float = 60.0) -> None:
    """Состарить mtime: файл лежит у пользователя с прошлого, команда его не писала."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_git_rm_cached_is_not_treated_as_created(repo):
    """Команда сняла путь с учёта git: файла в before нет, после — `??`.

    Конъюнкции «нет в before И после `??`» тут мало: файл пользователя не
    менялся, а ложное op="create" отдало бы его откату на удаление.
    """
    launch_root, threads_dir = repo
    _age_file(launch_root / "tracked.txt")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; subprocess.run(['git','rm','--cached','tracked.txt'],"
        " check=True, capture_output=True)",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("tracked.txt", "shell")]
    # путь ушёл из индекса — счётной базы нет (Р32 даёт точную из HEAD вместо неё)
    assert _snapshot_names(launch_root, ".gitorig") == []


def test_an_ignored_file_that_existed_is_not_treated_as_created(repo):
    """Игнорируемый файл, лежавший до команды, — не «созданный», даже если его
    поправили и принудительно проиндексировали.

    Игнорируемых путей в `git status` нет никогда, поэтому «пути не было до
    команды» для них ничего не доказывает, а после `git add -f` путь выглядит
    добавленным. Ложное op="create" отдавало его откату на УДАЛЕНИЕ — вместе с
    содержимым пользователя (ревью PR, находка 3).
    """
    launch_root, threads_dir = repo
    (launch_root / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    _git(launch_root, "add", ".gitignore")
    _git(launch_root, "commit", "-qm", "ignore")
    (launch_root / "secret.txt").write_bytes(b"users data\n")
    _age_file(launch_root / "secret.txt")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess, pathlib; pathlib.Path('secret.txt').write_bytes(b'changed\\n');"
        " subprocess.run(['git','add','-f','secret.txt'], check=True, capture_output=True)",
    )
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.path == "secret.txt"]
    assert recs and all(r.op != "create" for r in recs)


def test_git_reset_mixed_is_not_treated_as_created(repo):
    """`git reset --mixed HEAD~1`: закоммиченный файл стал `??`, но не создан."""
    launch_root, threads_dir = repo
    (launch_root / "second.txt").write_bytes(b"one\ntwo\n")
    _git(launch_root, "add", "second.txt")
    _git(launch_root, "commit", "-m", "second")
    _age_file(launch_root / "second.txt")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; subprocess.run(['git','reset','--mixed','HEAD~1'],"
        " check=True, capture_output=True)",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("second.txt", "shell")]
    assert _snapshot_names(launch_root, ".gitorig") == []


# --- точная база под HEAD-стражем (Р32) ---------------------------------------


def test_clean_tracked_gets_precise_base_and_is_revertable(repo):
    """Чистый tracked-файл правится sed-подобной командой: HEAD за команду не
    сдвинулся, фильтра нет → база ТОЧНАЯ (байты HEAD), а не счётная из индекса —
    такая правка становится откатываемой."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("tracked.txt", "shell")]
    store = _snapshot_store(launch_root)
    assert store.session_baseline("tracked.txt") == b"line1\nline2\n"
    assert store.turn_baseline("tracked.txt", "turn-1") == b"line1\nline2\n"
    assert store.has_session_baseline("tracked.txt") is True
    assert store.has_turn_baseline("tracked.txt", "turn-1") is True
    assert _snapshot_names(launch_root, ".gitorig") == []
    assert _snapshot_names(launch_root, ".gitturn") == []
    # признак ступени HEAD — ревизия НА МОМЕНТ съёма (§3.2, фикс C1)
    assert store.session_head_rev("tracked.txt") == git_ops.rev_parse_head(
        str(launch_root)
    )


def test_git_restore_over_user_work_is_recorded_and_revertable(repo):
    """`git restore` по файлу с несохранённой работой — она обязана уцелеть.

    Находка смока 2026-09-10 на живой команде. Путь грязен до команды, база
    читалась из индекса — а команда возвращает файл РОВНО К ИНДЕКСУ. Дифф
    выходил пустым, записи не появлялось вовсе: работа пользователя исчезала
    молча, без строки на экране и без всякой возможности откатить.
    """
    launch_root, threads_dir = repo
    (launch_root / "tracked.txt").write_bytes("НЕСОХРАН\n".encode("utf-8"))
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; subprocess.run(['git','restore','tracked.txt'], check=True)",
    )
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.path == "tracked.txt"]
    assert recs, "правка обязана попасть в журнал"
    base = _snapshot_store(launch_root).session_baseline("tracked.txt")
    assert base == "НЕСОХРАН\n".encode("utf-8")


def test_command_that_commits_falls_back_to_counting(repo):
    """Команда сама сдвинула HEAD (add+commit) — точная база недоказуема: HEAD,
    прочитанный ПОСЛЕ команды, мог оказаться уже промежуточным результатом её
    же коммита, а не тем, что было на диске ДО неё."""
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "import subprocess; "
        "open('tracked.txt','wb').write(b'line1\\nline2x\\n'); "
        "subprocess.run(['git','add','tracked.txt'], check=True, capture_output=True); "
        "subprocess.run(['git','commit','-m','mid'], check=True, capture_output=True); "
        "open('tracked.txt','wb').write(b'line1\\nline2z\\n')",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("tracked.txt", "shell")]
    store = _snapshot_store(launch_root)
    # счётная база — из ИНДЕКСА (содержимое сразу после коммита команды, не
    # исходное «до» неё): HEAD успел сдвинуться, точность не доказана.
    assert store.counting_session_baseline("tracked.txt") == b"line1\nline2x\n"
    assert _snapshot_names(launch_root, ".orig") == []
    assert _snapshot_names(launch_root, ".turn") == []


def test_custom_filter_blocks_precise_base(repo):
    """`filter=` в атрибутах — конфиг чужого репозитория не должен исполнять
    свой код: точную базу через `cat-file --filters` не читаем вовсе."""
    launch_root, threads_dir = repo
    (launch_root / ".gitattributes").write_bytes(b"secret.txt filter=mangle\n")
    (launch_root / "secret.txt").write_bytes(b"shh\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "add secret")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('secret.txt','wb').write(b'shh2\\n')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("secret.txt", "shell")]
    store = _snapshot_store(launch_root)
    assert store.counting_session_baseline("secret.txt") == b"shh\n"
    assert store.has_session_baseline("secret.txt") is False
    assert _snapshot_names(launch_root, ".orig") == []


def test_precise_base_respects_eol_form(repo):
    """autocrlf-репозиторий: `.orig` в форме рабочего дерева (CRLF), не raw blob —
    иначе откат переписал бы файлу переводы строк."""
    launch_root, threads_dir = repo
    _git(launch_root, "config", "core.autocrlf", "true")
    (launch_root / "eol.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(launch_root, "add", "eol.txt")
    _git(launch_root, "commit", "-m", "add eol.txt")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('eol.txt','wb').write(b'one\\r\\nTWO\\r\\n')")
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    assert store.session_baseline("eol.txt") == b"one\r\ntwo\r\n"


def test_precise_base_follows_lf_disk_under_autocrlf(repo):
    """Тот же autocrlf-репозиторий, но файл лежит на диске с LF (написан агентом
    и закоммичен без выкладки): база — LF, а не форма выкладки (CRLF).

    С формой фильтра дельта команды, дописавшей одну строку, превращалась в
    «весь файл заменён», а откат переписывал файлу переводы строк.
    """
    launch_root, threads_dir = repo
    _git(launch_root, "config", "core.autocrlf", "true")
    (launch_root / "eol.txt").write_bytes(b"one\ntwo\n")
    _git(launch_root, "add", "eol.txt")
    _git(launch_root, "commit", "-m", "add eol.txt")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('eol.txt','ab').write(b'three\\n')")
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    assert store.session_baseline("eol.txt") == b"one\ntwo\n"
    recs = _records(threads_dir)
    assert [(r.path, r.add, r.del_) for r in recs] == [("eol.txt", 1, 0)]


def test_existing_orig_not_clobbered_by_shell_precise(repo):
    """Снимок уже есть (снят инструментом) — точная база шелла его не
    перетирает: `capture_session`/`capture_turn` пишут «только если ещё нет»."""
    launch_root, threads_dir = repo
    store = _snapshot_store(launch_root)
    store.capture_session("tracked.txt", b"earlier session baseline\n")
    store.capture_turn("tracked.txt", b"earlier turn baseline\n", "turn-1")

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    assert store.session_baseline("tracked.txt") == b"earlier session baseline\n"
    assert store.turn_baseline("tracked.txt", "turn-1") == b"earlier turn baseline\n"


# --- карманный канал: снимок набора перед командой (Р33) ----------------------


def test_pocket_records_edit_in_plain_folder(tmp_path, monkeypatch):
    """Обычная папка без git: файл набора правится командой — карманный
    подканал ловит точную правку, хотя git-подканала тут нет вовсе."""
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "known.txt", b"line1\nline2\n", ts=1.0)

    delta = ShellDelta.begin(ctx)
    assert delta is not None
    _run_command(launch_root, "open('known.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.call_id is None]
    assert [r.path for r in recs] == ["known.txt"]
    rec = recs[0]
    assert rec.op == "shell"
    assert rec.hunks.startswith("diff --git a/known.txt b/known.txt")
    assert "-line2\n" in rec.hunks
    assert "+line2x\n" in rec.hunks
    assert rec.add == 1
    assert rec.del_ == 1
    assert rec.flags == []

    store = _snapshot_store(launch_root)
    assert store.session_baseline("known.txt") == b"line1\nline2\n"
    assert store.turn_baseline("known.txt", "turn-1") == b"line1\nline2\n"
    assert store.has_session_baseline("known.txt") is True

    # откат: точная база и правда восстанавливает исходное содержимое
    (launch_root / "known.txt").write_bytes(store.session_baseline("known.txt"))
    assert (launch_root / "known.txt").read_bytes() == b"line1\nline2\n"


def test_pocket_record_carries_the_hash_of_the_bytes_before(tmp_path, monkeypatch):
    """У карманной записи «до» ИЗВЕСТНО — байты сняты перед командой.

    `before_sha256` шелл-канала пуст не по забывчивости: git-подканал видит
    файл только ПОСЛЕ команды. Но карман держит точные байты «до» в памяти —
    из них же строится дифф и пишется `.orig`, — и класть туда `None` значило
    рвать цепочку целостности на ровном месте: одна команда посреди истории
    файла глушила признак «правки не от агента» навсегда (пятый раунд ревью).
    """
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    before_bytes = b"one\ntwo\n"
    _seed_known_file(threads_dir, launch_root, "known.txt", before_bytes, ts=1.0)

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('known.txt','wb').write(b'one\\nTWO\\n')")
    delta.after_and_record()

    [rec] = [r for r in _records(threads_dir) if r.call_id is None]
    assert rec.before_sha256 == hashlib.sha256(before_bytes).hexdigest()
    assert rec.after_sha256 == hashlib.sha256(b"one\nTWO\n").hexdigest()


def test_pocket_takes_priority_over_git_channel(repo, monkeypatch):
    """git-режим, файл набора правится командой второй раз подряд — карманный
    подканал обрабатывает его сам, `git diff` для этого пути не зовётся."""
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)

    # первая правка: журнал ещё пуст, "tracked.txt" не кандидат карманного
    # подканала — её видит git-подканал, как раньше.
    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2x\\n')")
    delta.after_and_record()
    assert [r.path for r in _records(threads_dir)] == ["tracked.txt"]

    diff_calls: list[str] = []
    original_diff = shell_delta.ShellDelta._diff

    def _spy_diff(self, rel, *, untracked):
        diff_calls.append(rel)
        return original_diff(self, rel, untracked=untracked)

    monkeypatch.setattr(shell_delta.ShellDelta, "_diff", _spy_diff)

    # вторая правка: "tracked.txt" теперь известна журналу — карманный
    # подканал обязан отработать её сам, не отдавая git-подканалу.
    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('tracked.txt','wb').write(b'line1\\nline2y\\n')")
    delta.after_and_record()

    assert diff_calls == []
    recs = _records(threads_dir)
    assert [r.path for r in recs] == ["tracked.txt", "tracked.txt"]
    assert recs[1].hunks.startswith("diff --git")
    assert "+line2y" in recs[1].hunks


def test_pocket_skips_oversize_and_respects_total_cap(tmp_path, monkeypatch):
    """Пофайловый кап (`max_diff_source_bytes`) и суммарный кап
    (`POCKET_TOTAL_CAP_BYTES`) отбирают кандидатов ДО чтения байтов, приоритет
    — недавно правленным."""
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["max_diff_source_bytes"] = 20
    monkeypatch.setattr(shell_delta, "POCKET_TOTAL_CAP_BYTES", 8)

    # недавний, но сверх пофайлового капа — не кандидат вовсе, бюджет не тратит
    _seed_known_file(threads_dir, launch_root, "huge.txt", b"x" * 25, ts=3.0)
    # укладывается в оба капа — попадает первым (самый недавний из подходящих)
    _seed_known_file(threads_dir, launch_root, "recent.txt", b"aaaaa", ts=2.0)
    # укладывается в пофайловый кап сам по себе, но вместе с recent.txt рвёт
    # суммарный (5 + 5 = 10 > 8) — вытеснен как менее недавний
    _seed_known_file(threads_dir, launch_root, "old.txt", b"bbbbb", ts=1.0)

    delta = ShellDelta.begin(ctx)

    assert set(delta._pocket) == {"recent.txt"}


def test_pocket_keeps_filling_after_a_candidate_that_does_not_fit(
    tmp_path, monkeypatch
):
    """Не влезший в остаток бюджета кандидат пропускается, а не обрывает набор.

    Прежде тут стоял `break`, и он был оправдан: кандидаты шли из журнала
    «недавние первыми», а утрамбовывать бюджет более старыми файлами помельче
    значило бы предпочесть их недавним. Теперь список смешанный — впереди идут
    грязные пути из `git status`, — и один большой грязный файл убивал весь
    остаток набора, включая известные журналу (пятый раунд ревью, находка 2).
    """
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    ctx.config_dict["diff_tui"]["max_diff_source_bytes"] = 20
    monkeypatch.setattr(shell_delta, "POCKET_TOTAL_CAP_BYTES", 8)

    _seed_known_file(threads_dir, launch_root, "big.txt", b"x" * 7, ts=3.0)
    # 7 + 5 = 12 > 8 — не влезает и пропускается
    _seed_known_file(threads_dir, launch_root, "mid.txt", b"y" * 5, ts=2.0)
    # 7 + 1 = 8 — влезает, и обязан попасть
    _seed_known_file(threads_dir, launch_root, "tiny.txt", b"z", ts=1.0)

    delta = ShellDelta.begin(ctx)

    assert set(delta._pocket) == {"big.txt", "tiny.txt"}


def test_pocket_detects_deletion_of_known_file(tmp_path, monkeypatch):
    """Удалённый файл набора → `op="delete"` с СОХРАНЁННОЙ базой: карманные
    байты «до» уже в памяти, второй (git) путь для этого не нужен."""
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "known.txt", b"line1\nline2\n", ts=1.0)

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "import os; os.remove('known.txt')")
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.call_id is None]
    assert [r.path for r in recs] == ["known.txt"]
    assert recs[0].op == "delete"
    assert recs[0].flags == ["no_diff"]

    store = _snapshot_store(launch_root)
    assert store.session_baseline("known.txt") == b"line1\nline2\n"
    assert store.turn_baseline("known.txt", "turn-1") == b"line1\nline2\n"


def test_pocket_append_failure_on_one_path_does_not_lose_dedup_for_another(
    repo, monkeypatch
):
    """Сбой при записи ВТОРОГО карманного пути в журнал не должен откатывать
    дедупликацию ПЕРВОГО, уже успешно дописанного в этом же проходе цикла —
    иначе исключение пробрасывается из `_record_pocket()` целиком, внешний
    except в `_pocket_after_and_record` теряет уже собранный `recorded`, и
    git-подканал (получив пустой `skip`) пишет ту же правку по первому пути
    повторно.

    Сценарий — `op="delete"`: только он не прикрыт сайд-эффектом `_seen`
    (`_remember` для `op="delete"` путь из `_seen` УБИРАЕТ, а `_build_record`
    git-подканала для отсутствующего файла в `_seen` вообще не смотрит) — на
    `op="shell"` дубликат случайно перехватывается хэш-дедупом `_seen`, и
    регресс был бы незаметен.
    """
    launch_root, threads_dir = repo
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "a.txt", b"alpha\n", ts=2.0)
    _seed_known_file(threads_dir, launch_root, "b.txt", b"beta\n", ts=1.0)

    delta = ShellDelta.begin(ctx)
    # порядок кандидатов — по недавности правки: "a.txt" (ts=2.0) раньше "b.txt".
    assert list(delta._pocket) == ["a.txt", "b.txt"]

    original_append = EditsJournal.append
    failed_once = {"done": False}

    def _flaky_append(self, rec):
        # Только ПЕРВЫЙ такой вызов — это попытка карманного подканала;
        # повторный (если он случится) — уже от git-подканала, ему сбоить не
        # нужно: тест проверяет потерю дедупликации, а не второй сбой подряд.
        if rec.path == "b.txt" and rec.op == "delete" and not failed_once["done"]:
            failed_once["done"] = True
            # ровно тот тип исключения, что кидает file_lock по таймауту.
            raise TimeoutError("file_lock timeout")
        original_append(self, rec)

    monkeypatch.setattr(EditsJournal, "append", _flaky_append)

    _run_command(launch_root, "import os; os.remove('a.txt'); os.remove('b.txt')")
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [r.op for r in recs if r.path == "a.txt"] == ["write", "delete"]
    # "b.txt": карманная попытка упала первой, но git-подканал не пропустил
    # его как чужой skip и подхватил запись сам — итог всё равно ОДНА запись,
    # не ноль.
    assert [r.op for r in recs if r.path == "b.txt"] == ["write", "delete"]


def test_pocket_does_not_clobber_existing_orig(tmp_path, monkeypatch):
    """Снимок уже есть (снят инструментом) — карманные байты «до» его не
    перетирают: `capture_session`/`capture_turn` пишут «только если ещё нет»."""
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "known.txt", b"line1\n", ts=1.0)
    store = _snapshot_store(launch_root)
    store.capture_session("known.txt", b"earlier session baseline\n")
    store.capture_turn("known.txt", b"earlier turn baseline\n", "turn-1")

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('known.txt','wb').write(b'line1x\\n')")
    delta.after_and_record()

    assert store.session_baseline("known.txt") == b"earlier session baseline\n"
    assert store.turn_baseline("known.txt", "turn-1") == b"earlier turn baseline\n"
    # запись про правку всё равно есть — «только если ещё нет» не про журнал
    assert [r.op for r in _records(threads_dir) if r.call_id is None] == ["shell"]


def test_pocket_paths_cache_hits_by_journal_signature(tmp_path, monkeypatch):
    """Два подряд `begin()` без записи в журнал между ними → один парс JSONL:
    кэш путей набора живёт по подписи (размер+mtime) файла журнала."""
    launch_root = tmp_path / "plain"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "known.txt", b"line1\n", ts=1.0)

    calls: list[int] = []
    original_read_all = EditsJournal.read_all

    def _counting_read_all(self):
        calls.append(1)
        return original_read_all(self)

    monkeypatch.setattr(EditsJournal, "read_all", _counting_read_all)
    cache: dict = {}

    delta = ShellDelta.begin(ctx, pocket_cache=cache)
    _run_command(launch_root, "print('noop')")
    delta.after_and_record()

    delta = ShellDelta.begin(ctx, pocket_cache=cache)
    _run_command(launch_root, "print('noop again')")
    delta.after_and_record()

    assert len(calls) == 1


# --- кодировка хэнков канала (Д2) --------------------------------------------


def test_shell_hunks_keep_cp1251_text(repo):
    """Вывод `git diff` по cp1251-файлу читается словами, а не ромбами.

    chardet на почти-ASCII выводе диффа (несколько кириллических байт среди
    заголовков хэнков) уверенности не набирает, и прежний резерв
    `errors="replace"` превращал русский текст в U+FFFD прямо в журнале —
    навсегда, восстановить его потом неоткуда.
    """
    launch_root, threads_dir = repo
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "open('ru.txt','wb').write('привет, мир\\n'.encode('cp1251'))",
    )
    delta.after_and_record()

    recs = _records(threads_dir)
    assert [(r.path, r.op) for r in recs] == [("ru.txt", "create")]
    assert "+привет, мир" in recs[0].hunks
    assert "\ufffd" not in recs[0].hunks


def test_pocket_diff_of_cp1251_file_is_not_no_diff(repo):
    """Карманный подканал считает дифф ПО БАЙТАМ и декодирует их сам: файл,
    который агент читает как cp1251, не имеет права стать `binary`/`no_diff`."""
    launch_root, threads_dir = repo
    _seed_known_file(
        threads_dir, launch_root, "ru.txt", "первая\nвторая\n".encode("cp1251"), ts=1.0
    )

    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "open('ru.txt','wb').write('первая\\nтретья\\n'.encode('cp1251'))",
    )
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.call_id is None]
    assert [(r.path, r.op, r.flags) for r in recs] == [("ru.txt", "shell", [])]
    assert "-вторая" in recs[0].hunks
    assert "+третья" in recs[0].hunks
    assert (recs[0].add, recs[0].del_) == (1, 1)


def test_command_created_file_gets_the_creation_mark(repo):
    """Д1, второй писатель отметки: у файла, созданного командой, базы нет и
    быть не должно — ревизию ступени HEAD несёт отметка создания. Ревизия — та,
    что была ДО команды, даже если команда сама коммитила."""
    launch_root, threads_dir = repo
    head_before = git_ops.rev_parse_head(str(launch_root))
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(launch_root, "open('new.txt','wb').write(b'alpha\\n')")
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    assert [r.op for r in _records(threads_dir)] == ["create"]
    assert store.created_head_rev("new.txt") == head_before
    assert store.session_baseline("new.txt") is None


def test_creation_mark_failure_does_not_drop_the_rest_of_the_command(repo, monkeypatch):
    """Сбой отметки создания на одном пути не уносит из журнала остальные пути
    команды: `file_lock` индекса при конкуренции (сабагенты, swarm в одном
    проекте) отвечает таймаутом, и без своего `try` исключение уходит во
    внешний `except` — записи о путях, оставшихся в наборе, не появятся
    НИКОГДА, а следующая команда увидит их новое состояние уже в своём «до».

    Тот же soft-fail, что у соседей по циклу (`_build_record`, `_capture_base`):
    один плохой путь не убивает набор. Порода исключения — ровно та, что
    кидает `file_lock` по таймауту.
    """
    launch_root, threads_dir = repo

    def _boom(self, path_key, *, head_rev):
        if path_key == "a_new.txt":
            raise TimeoutError("file_lock timeout")

    monkeypatch.setattr(SnapshotStore, "mark_created", _boom)
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    _run_command(
        launch_root,
        "open('a_new.txt','wb').write(b'alpha\\n');"
        "open('b_new.txt','wb').write(b'beta\\n')",
    )
    delta.after_and_record()

    assert [(r.path, r.op) for r in _records(threads_dir)] == [
        ("a_new.txt", "create"),
        ("b_new.txt", "create"),
    ]


def test_ignored_file_in_set_visible_via_pocket(repo):
    """Файл из `.gitignore` не виден `git status` вовсе (дыра git-режима из
    докстринга модуля) — карманный подканал от git status не зависит и
    правку всё равно ловит."""
    launch_root, threads_dir = repo
    (launch_root / ".gitignore").write_text(
        "secret.env\n", encoding="utf-8", newline="\n"
    )
    ctx = _ctx(launch_root, threads_dir)
    _seed_known_file(threads_dir, launch_root, "secret.env", b"TOKEN=old\n", ts=1.0)

    delta = ShellDelta.begin(ctx)
    _run_command(launch_root, "open('secret.env','wb').write(b'TOKEN=new\\n')")
    delta.after_and_record()

    recs = [r for r in _records(threads_dir) if r.call_id is None]
    assert [r.path for r in recs] == ["secret.env"]
    assert "+TOKEN=new" in recs[0].hunks
    assert "-TOKEN=old" in recs[0].hunks


def test_staged_add_codes_match_git_status_documentation():
    """Набор кодов «команда добавила новый путь» — по документации git status.

    `A` в колонке индекса и `[ MTD]` в колонке дерева: `AT` (сменился тип
    файла) — такой же новый путь, как `A ` и `AM`. Неслитые `AA`/`AU` тоже
    начинаются с `A`, но путь под ними в HEAD ЕСТЬ: спутать их с созданием
    значит дать откату право удалить закоммиченный файл.
    """
    from gc_core.agent_edits.shell_delta import _STAGED_ADD_XY

    assert _STAGED_ADD_XY == {"A ", "AM", "AT", "AD"}
    assert not (_STAGED_ADD_XY & {"AA", "AU", "UA", "UU", "DD", "DU", "UD"})


# --- дифф пачкой: паритет с пофайловым ----------------------------------------


def _awkward_changes(repo):
    """Закоммиченные неудобные пути, затем правки всех: `(корень, tracked, untracked)`.

    Пути — те, где пачка могла бы разойтись с пофайловым вызовом: пробел,
    кириллица, glob-скобки, CRLF, файл без перевода строки в конце, пара путей,
    которую git склеил бы в переименование; на POSIX — кавычка, обратный слэш и
    таб в имени (git берёт их в C-кавычки). Часть правок проиндексирована —
    диффы против HEAD и против индекса тогда различаются."""
    launch_root, _threads = repo
    tracked = {
        "plain.py": b"a = 1\n",
        "with space.txt": b"x\n",
        "кириллица.md": "строка\n".encode("utf-8"),
        "br[1].py": b"y = 2\n",
        "crlf.txt": b"one\r\ntwo\r\n",
        "no-eol.txt": b"tail",
        "old_name.py": b"".join(b"line %d\n" % i for i in range(30)),
    }
    if os.name != "nt":
        tracked.update({'quo"te.txt': b"q\n", "back\\slash.txt": b"b\n", "tab\tname.txt": b"t\n"})
    for rel, data in tracked.items():
        (launch_root / rel).write_bytes(data)
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "awkward")
    for rel, data in tracked.items():
        (launch_root / rel).write_bytes(data.replace(b"\n", b"!\n") + b"more\n")
    moved = (launch_root / "old_name.py").read_bytes()
    (launch_root / "old_name.py").unlink()
    (launch_root / "new_name.py").write_bytes(moved)       # содержимое то же — git склеил бы
    _git(launch_root, "add", "--", "plain.py", "crlf.txt")  # индекс ≠ HEAD у части путей
    untracked = {"fresh.py": b"n = 1\n", "свежий.txt": "да\n".encode("utf-8"), "empty.txt": b""}
    for rel, data in untracked.items():
        (launch_root / rel).write_bytes(data)
    return launch_root, [*tracked, "new_name.py"], list(untracked)


@pytest.mark.parametrize("against_head", [True, False])
def test_batched_diff_matches_the_per_path_diff(repo, against_head):
    """Дифф пачкой — байт в байт то, что даёт пофайловый вызов, по каждому пути.

    Хэнки лягут в журнал и дальше в счётчики и экран: разойдись пачка с
    пофайловым вызовом хоть в заголовке — поехали бы и они."""
    launch_root, tracked, _untracked = _awkward_changes(repo)
    delta = ShellDelta(str(launch_root), _ctx(*repo))
    base = git_ops.head_rev_from_files(str(launch_root)) if against_head else None
    expected = {rel: delta._diff(rel, untracked=False, base_rev=base) for rel in tracked}
    assert expected["with space.txt"] and expected["кириллица.md"]   # предмет не пуст
    assert delta._diff_many(tracked, base_rev=base) == expected


def test_batched_diff_survives_splitting_into_chunks(repo, monkeypatch):
    """Длинная пачка режется на куски командной строки — ответ тот же.

    Предел длины командной строки Windows — около 32 тысяч знаков, а путей в
    пачке может быть тысячи."""
    launch_root, tracked, _untracked = _awkward_changes(repo)
    delta = ShellDelta(str(launch_root), _ctx(*repo))
    expected = {rel: delta._diff(rel, untracked=False, base_rev=None) for rel in tracked}
    monkeypatch.setattr(git_ops, "ARGV_CHUNK_CHARS", 40)
    assert delta._diff_many(tracked, base_rev=None) == expected


def test_batched_diff_costs_one_git_process_per_chunk(repo, monkeypatch):
    """Пачка отслеживаемых путей — один запуск git, а не запуск на путь."""
    launch_root, tracked, _untracked = _awkward_changes(repo)
    delta = ShellDelta(str(launch_root), _ctx(*repo))
    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    delta._diff_many(tracked, base_rev=None)
    assert len(calls) == 1


def test_untracked_diffs_match_the_per_path_diff(repo):
    """Дифф новых файлов (параллельно, по файлу) — тот же, что по одному.

    Пачкой его не собрать: `--no-index` сравнивает ровно два пути, а временный
    индекс исполнил бы фильтры репозитория, которые канал нарочно не исполняет."""
    launch_root, _tracked, untracked = _awkward_changes(repo)
    delta = ShellDelta(str(launch_root), _ctx(*repo))
    expected = {rel: delta._diff(rel, untracked=True, base_rev=None) for rel in untracked}
    assert all(expected.values())                     # и пустой файл даёт заголовок
    assert delta._untracked_diffs(untracked) == expected


def test_recording_a_wide_command_costs_a_constant_number_of_git_processes(repo, monkeypatch):
    """Команда, тронувшая много файлов, стоит постоянного числа запусков git.

    Пофайловые запуски (дифф, атрибуты, прежняя версия — три на путь) делали
    запись команды, тронувшей 2000 файлов, делом 35 секунд, пока агент ждёт.
    Здесь 30 отслеживаемых путей правятся и 10 создаются: запусков — по одному
    на вид вопроса, плюс параллельные диффы новых файлов."""
    launch_root, threads_dir = repo
    for i in range(30):
        (launch_root / f"m{i}.py").write_bytes(b"".join(b"x_%d = %d\n" % (j, j) for j in range(10)))
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "wide")
    ctx = _ctx(launch_root, threads_dir)
    delta = ShellDelta.begin(ctx)
    for i in range(30):
        path = launch_root / f"m{i}.py"
        path.write_bytes(path.read_bytes().replace(b"x_3 = 3", b"x_3 = 33"))
    for i in range(10):
        (launch_root / f"new{i}.py").write_bytes(b"n = %d\n" % i)
    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    delta.after_and_record()

    records = _records(threads_dir)
    assert len(records) == 40
    store = _snapshot_store(launch_root)
    assert all(store.session_baseline(f"m{i}.py") is not None for i in range(30))
    assert len(calls) <= 8, [args[:6] for args in calls]


def test_batched_diff_keeps_a_merge_conflict_path(repo):
    """Путь с конфликтом слияния git печатает секцией `diff --cc`, а не `diff --git`.

    Не узнав такой заголовок, разрезатель терял путь целиком (пустой дифф — нет
    записи, нет строки на экране, нет базы) или, если секция стояла между
    обычными, приклеивал её байты к соседней и портил чужой дифф."""
    launch_root, threads_dir = repo
    for rel in ("a.txt", "c.txt", "z.txt"):
        (launch_root / rel).write_bytes(b"base\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "three")
    _git(launch_root, "checkout", "-qb", "other")
    (launch_root / "c.txt").write_bytes(b"theirs\n")
    _git(launch_root, "commit", "-qam", "other")
    _git(launch_root, "checkout", "-q", "main")
    (launch_root / "c.txt").write_bytes(b"ours\n")
    _git(launch_root, "commit", "-qam", "ours")
    subprocess.run(["git", "merge", "other"], cwd=str(launch_root), capture_output=True)
    (launch_root / "a.txt").write_bytes(b"base\nmore\n")
    (launch_root / "z.txt").write_bytes(b"base\nmore\n")
    delta = ShellDelta(str(launch_root), _ctx(launch_root, threads_dir))
    rels = ["a.txt", "c.txt", "z.txt"]

    expected = {rel: delta._diff(rel, untracked=False, base_rev=None) for rel in rels}
    assert expected["c.txt"].startswith("diff --cc")          # предмет теста на месте
    assert delta._diff_many(rels, base_rev=None) == expected


def test_batched_diff_does_not_trust_output_of_a_failed_git(repo, monkeypatch):
    """git упал посреди пачки — ни одной её секции не верим, кусок добирается по одному.

    Секция, напечатанная до отказа, может быть оборвана посреди хэнка: её счётчики
    легли бы в журнал неверными."""
    launch_root, tracked, _untracked = _awkward_changes(repo)
    delta = ShellDelta(str(launch_root), _ctx(*repo))
    expected = {rel: delta._diff(rel, untracked=False, base_rev=None) for rel in tracked}
    real = git_ops.run_git

    def broken_batch(root, args, **kw):
        code, out = real(root, args, **kw)
        if "diff" in args and sum(1 for a in args if a.startswith(":(literal)")) > 1:
            return 128, out[: len(out) // 2]              # оборвано посреди вывода
        return code, out

    monkeypatch.setattr(git_ops, "run_git", broken_batch)
    assert delta._diff_many(tracked, base_rev=None) == expected


# --- запись кусками: цена и отказ ограничены куском -----------------------------


def _wide_command(repo, n: int):
    """`n` закоммиченных файлов, которые «команда» правит; отдаёт `(delta, корень, треды)`."""
    launch_root, threads_dir = repo
    for i in range(n):
        (launch_root / f"w{i}.py").write_bytes(b"a = 1\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "wide")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    for i in range(n):
        (launch_root / f"w{i}.py").write_bytes(b"a = 2\n")
    return delta, launch_root, threads_dir


def test_a_failed_batch_of_base_facts_costs_nothing_but_speed(repo, monkeypatch):
    """Отказ пакетного сбора фактов не лишает баз всю команду — они снимаются по одной.

    Записи в журнал к этому моменту уже легли: без баз пользователь видел бы
    полный список правок и ни одной откатываемой строки."""
    delta, launch_root, threads_dir = _wide_command(repo, 5)

    def broken(*_a, **_k):
        raise OSError("пакетный вызов упал")

    monkeypatch.setattr(git_ops, "custom_filter_paths", broken)
    delta.after_and_record()

    store = _snapshot_store(launch_root)
    assert len(_records(threads_dir)) == 5
    assert all(store.session_baseline(f"w{i}.py") == b"a = 1\n" for i in range(5))


def test_batched_git_calls_never_take_more_than_a_slice_of_paths(repo, monkeypatch):
    """Команда пишется кусками: ни один пакетный вызов git не берёт больше куска путей.

    Иначе и память (все диффы и блобы команды разом), и время одного запуска git
    под общим таймаутом росли бы с числом путей, а отказ стоил бы всей команды."""
    monkeypatch.setattr(shell_delta, "_RECORD_SLICE_PATHS", 3)
    delta, launch_root, threads_dir = _wide_command(repo, 7)
    widths = []
    real = git_ops.run_git

    def spy(root, args, **kw):
        stdin = kw.get("stdin") or b""
        widths.append(max(stdin.count(b"\x00"), stdin.count(b"\n"),
                          sum(1 for a in args if a.startswith(":(literal)"))))
        return real(root, args, **kw)

    monkeypatch.setattr(git_ops, "run_git", spy)
    delta.after_and_record()

    assert len(_records(threads_dir)) == 7
    assert all(_snapshot_store(launch_root).session_baseline(f"w{i}.py") for i in range(7))
    assert max(widths) <= 3


def test_protected_path_never_reaches_git(repo, monkeypatch):
    """Защищённый путь (инструкции агента) не попадает ни в один вызов git.

    Отсев защищённых — первым делом, до диффа: иначе их содержимое уходило бы в
    вывод git и память, а выбрасывалось бы только потом."""
    launch_root, threads_dir = repo
    (launch_root / "AGENTS.md").write_bytes(b"rules\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "agents")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    (launch_root / "AGENTS.md").write_bytes(b"rules\nmore\n")
    (launch_root / "tracked.txt").write_bytes(b"changed\n")
    seen = []
    real_one, real_many = git_ops.run_git, git_ops.run_git_many
    monkeypatch.setattr(git_ops, "run_git", lambda root, args, **kw: (
        seen.append(" ".join(args) + (kw.get("stdin") or b"").decode("utf-8", "replace")),
        real_one(root, args, **kw))[1])
    monkeypatch.setattr(git_ops, "run_git_many", lambda root, argvs: (
        seen.extend(" ".join(a) for a in argvs), real_many(root, argvs))[1])
    delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == ["tracked.txt"]
    assert not [call for call in seen if "AGENTS.md" in call and "status" not in call]


def test_file_facts_are_read_once_per_path(repo, monkeypatch):
    """Бинарность файла проверяется один раз на путь, а не в пачке и ещё раз в записи.

    Проверка — открытие файла и чтение образца: на 10 000 путях второй обход
    стоил бы 10 000 лишних открытий."""
    delta, _launch_root, _threads_dir = _wide_command(repo, 4)
    calls = []
    real = shell_delta._is_binary_file
    monkeypatch.setattr(shell_delta, "_is_binary_file", lambda p: (calls.append(p), real(p))[1])
    delta.after_and_record()

    assert len(calls) == 4


@pytest.mark.skipif(os.name == "nt", reason="симлинки без прав разработчика на Windows")
def test_a_repointed_symlink_never_passes_for_an_edit_of_its_target(repo):
    """Перенаправленный симлинк не выдаётся за правку файла-цели.

    Ключ пути раскрывает ссылку до цели, а дифф и база git читаются по самой
    ссылке: `ln -sfn b.txt link` записывал «правку» `b.txt` с точной базой —
    строкой `a.txt`, и откат затёр бы `b.txt` этой строкой."""
    launch_root, threads_dir = repo
    (launch_root / "a.txt").write_bytes(b"alpha\n")
    (launch_root / "b.txt").write_bytes(b"beta\n")
    os.symlink("a.txt", launch_root / "link")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "link")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    os.remove(launch_root / "link")
    os.symlink("b.txt", launch_root / "link")
    delta.after_and_record()

    assert "b.txt" not in [r.path for r in _records(threads_dir)]
    store = _snapshot_store(launch_root)
    assert store.session_baseline("b.txt") is None
    assert (launch_root / "b.txt").read_bytes() == b"beta\n"


@pytest.mark.skipif(os.name == "nt", reason="симлинки без прав разработчика на Windows")
def test_a_symlink_repointed_at_a_missing_target_is_not_recorded(repo):
    """Висячий симлинк — тоже ссылка: git-подканал его не записывает.

    Цель не существует, ключ ссылку не раскрывает и совпадает с путём из git:
    базой ложился git-блоб ссылки (текст `a.txt`), и откат поставил бы на
    место ссылки обычный файл с этим текстом."""
    launch_root, threads_dir = repo
    (launch_root / "a.txt").write_bytes(b"alpha\n")
    os.symlink("a.txt", launch_root / "link")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "link")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    os.remove(launch_root / "link")
    os.symlink("missing.txt", launch_root / "link")
    delta.after_and_record()

    assert "link" not in [r.path for r in _records(threads_dir)]
    assert _snapshot_store(launch_root).session_baseline("link") is None


def test_a_path_whose_key_differs_only_by_case_is_still_recorded(repo, monkeypatch):
    """Ключ, отличный от пути из git только регистром, — не повод терять правку.

    На Windows `resolve()` отдаёт регистр с диска, а `git status` — из индекса:
    при `Readme.md` в git и `README.md` на диске (обычное дело при
    core.ignorecase) сравнение ключа с путём выкидывало все правки команд по
    файлу. Здесь регистр диска имитируется подменой ключа."""
    launch_root, threads_dir = repo
    (launch_root / "Readme.md").write_bytes(b"old\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "readme")
    real_key = shell_delta.canonical_path_key
    monkeypatch.setattr(
        shell_delta, "canonical_path_key",
        lambda path, root, **kw: (lambda k: "README.md" if k == "Readme.md" else k)(
            real_key(path, root, **kw)),
    )
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    (launch_root / "Readme.md").write_bytes(b"new\n")
    delta.after_and_record()

    assert [r.path for r in _records(threads_dir)] == ["README.md"]


def test_a_path_through_a_junction_is_not_recorded(repo, monkeypatch):
    """Junction на Windows — та же ссылка на папку, но ``is_symlink()`` для него
    отдаёт ``False``; ставится без прав (`mklink /J`), так что это обычный путь
    агента. Здесь junction изображён подменой проверки звена
    (``paths._is_link``): настоящий создаётся только на Windows, а распознавание
    по тегу из ``lstat`` проверяет ``test_paths``."""
    launch_root, threads_dir = repo
    (launch_root / "d").mkdir()
    (launch_root / "d" / "a.txt").write_bytes(b"old\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "dir")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    import shutil
    shutil.rmtree(launch_root / "d")
    junction = launch_root / "d"
    junction.mkdir()
    from gc_core.agent_edits import paths as paths_mod

    real_is_link = paths_mod._is_link
    monkeypatch.setattr(paths_mod, "_is_link", lambda n: Path(n) == junction or real_is_link(n))
    delta.after_and_record()

    assert "d/a.txt" not in [r.path for r in _records(threads_dir)]


@pytest.mark.skipif(os.name == "nt", reason="симлинки без прав разработчика на Windows")
def test_a_path_through_a_directory_turned_symlink_is_not_recorded(repo):
    """Путь через папку, ставшую ссылкой, не выдаётся за правку чужого файла.

    `rm -rf d && ln -s other d`: git по-прежнему показывает отслеживаемый
    `d/a.txt` (удалён), а путь к нему теперь идёт через ссылку и приводит в
    `other/a.txt`. Проверка одного последнего звена ссылку в середине не видит:
    правка `other/a.txt`, которой агент не делал, ложилась в журнал с базой из
    `d/a.txt`, и откат затёр бы ею чужой файл."""
    launch_root, threads_dir = repo
    for rel, text in (("d/a.txt", b"old\n"), ("other/a.txt", b"mine\n")):
        (launch_root / rel).parent.mkdir(exist_ok=True)
        (launch_root / rel).write_bytes(text)
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-qm", "two dirs")
    delta = ShellDelta.begin(_ctx(launch_root, threads_dir))
    import shutil
    shutil.rmtree(launch_root / "d")
    os.symlink("other", launch_root / "d")
    delta.after_and_record()

    assert "other/a.txt" not in [r.path for r in _records(threads_dir)]
    store = _snapshot_store(launch_root)
    assert store.session_baseline("other/a.txt") is None
    assert (launch_root / "other" / "a.txt").read_bytes() == b"mine\n"

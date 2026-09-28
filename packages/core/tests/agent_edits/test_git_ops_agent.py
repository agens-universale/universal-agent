"""git_ops: тонкие git-обёртки слоя agent_edits.

Тесты идут на настоящем временном репозитории: git не мокается — иначе не
проверить ни гигиену вызова (хуки), ни поведение ``git show :<path>`` на
неотслеживаемом пути.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from gc_core.agent_edits import git_ops
from gc_core.agent_edits.git_ops import (
    add_paths,
    blobs_at,
    cat_file_blob,
    cat_file_filters,
    disk_eol,
    custom_filter_paths,
    commit_label,
    has_custom_filter,
    head_bytes_like_disk,
    head_blob_oids,
    head_commit_label,
    head_commit_ts,
    head_rev_from_files,
    last_commits,
    ls_files_set,
    parse_status_line,
    paths_committed_since,
    rev_parse_head,
    run_git,
    show_index_bytes,
    status_entries,
    with_eol,
)


@pytest.fixture
def tmp_git_repo(tmp_path) -> Path:
    """Корень временного git-репозитория с одним закоммиченным файлом."""
    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
        ("config", "core.autocrlf", "false"),
        ("config", "core.quotepath", "true"),  # дефолт git — пиним явно
    ):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)
    (root / "seed.txt").write_bytes(b"seed\n")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True
    )
    return root


def _marker_hook(root: Path, marker: Path) -> None:
    hook = root / ".git" / "hooks" / "post-index-change"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        f'#!/bin/sh\necho ran >> "{marker.as_posix()}"\nexit 0\n',
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(hook, 0o755)


def test_show_index_returns_staged_bytes(tmp_git_repo):
    root = tmp_git_repo
    (root / "a.txt").write_bytes(b"one\n")
    run_git(str(root), ["add", "--", "a.txt"])
    (root / "a.txt").write_bytes(b"two\n")

    assert show_index_bytes(str(root), "a.txt") == b"one\n"


def test_show_index_returns_none_for_untracked(tmp_git_repo):
    (tmp_git_repo / "u.txt").write_bytes(b"x\n")

    assert show_index_bytes(str(tmp_git_repo), "u.txt") is None


def test_show_index_returns_none_outside_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_bytes(b"x\n")

    assert show_index_bytes(str(plain), "a.txt") is None


def test_run_git_reports_failure_code_without_raising(tmp_git_repo):
    code, out = run_git(str(tmp_git_repo), ["show", ":нет-такого.txt"])

    assert code != 0
    assert out == b""


def test_hooks_are_not_executed(tmp_git_repo):
    """Хуки репозитория не должны исполняться ни на одном нашем вызове.

    Триггер — запись индекса (``git add``): она зовёт post-index-change, что
    подтверждает контрольный прогон обычного git.
    """
    root = tmp_git_repo
    marker = root.parent / "hook-ran.txt"  # вне репо: иначе сам попадёт в status
    _marker_hook(root, marker)

    # контроль: обычный git add хук выполняет — иначе гвард ничего не доказывает
    (root / "a.txt").write_bytes(b"control\n")
    subprocess.run(["git", "add", "--", "a.txt"], cwd=str(root), capture_output=True)
    if not marker.exists():
        pytest.skip("git не выполняет post-index-change в этой среде — гвард непроверяем")
    marker.unlink()

    (root / "a.txt").write_bytes(b"ours\n")
    code, _err = add_paths(str(root), ["a.txt"])

    assert code == 0
    assert not marker.exists()
    assert show_index_bytes(str(root), "a.txt") == b"ours\n"


def test_cyrillic_path_round_trip(tmp_git_repo):
    """Не-ASCII имя с пробелом уходит в git и возвращается содержимым как есть."""
    root = tmp_git_repo
    rel = "вой на.txt"
    (root / rel).write_bytes("тише\n".encode("utf-8"))

    assert add_paths(str(root), [rel])[0] == 0
    assert show_index_bytes(str(root), rel) == "тише\n".encode("utf-8")


def test_add_paths_reports_error_text(tmp_git_repo):
    code, err = add_paths(str(tmp_git_repo), ["нет-такого.txt"])

    assert code != 0
    assert err.strip() != ""


def test_add_paths_without_paths_is_noop(tmp_git_repo):
    assert add_paths(str(tmp_git_repo), []) == (0, "")


def _staged(root: Path) -> list[str]:
    _code, out = run_git(str(root), ["diff", "--cached", "--name-only"])
    return out.decode("utf-8").splitlines()


def test_add_paths_matches_literal_names_not_pathspec_magic(tmp_git_repo):
    """Ключ журнала — ИМЯ ФАЙЛА, а не шаблон подстановки.

    Живой случай: в репозитории есть `a1.py`, агент правил `a[1].py`. Без
    `:(literal)` git трактует скобки как класс символов и стейджит ЧУЖОЙ файл
    с кодом 0 — правку пользователя утащило бы в индекс молча.
    """
    root = tmp_git_repo
    (root / "a1.py").write_bytes("чужой\n".encode("utf-8"))
    (root / "a[1].py").write_bytes("наш\n".encode("utf-8"))

    code, err = add_paths(str(root), ["a[1].py"])

    assert code == 0, err
    assert _staged(root) == ["a[1].py"]


# --- status_entries: отказ отличим от пустоты --------------------------------


def test_status_entries_outside_a_repository_is_none(tmp_path):
    """Не репозиторий — не «пустой снимок», а отсутствие самой возможности спросить."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_bytes(b"x\n")

    assert status_entries(str(plain)) is None


def test_parse_status_line_takes_the_new_name_of_a_rename():
    assert parse_status_line("R  old.py -> new.py") == ("new.py", "R ")
    assert parse_status_line(" M a.py") == ("a.py", " M")
    assert parse_status_line("?? a.py") == ("a.py", "??")
    assert parse_status_line("xx") is None


# --- лестница отсчёта: git-факты (Р28/Р32) ------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def test_cat_file_filters_applies_eol(tmp_git_repo):
    """``.gitattributes`` c ``eol=crlf``: blob хранит LF, форма рабочего дерева — CRLF.

    Raw blob (как у ``show_index_bytes``) тут не годится: откат переписал бы
    файлу переводы строк на Windows (§5 спеки).
    """
    root = tmp_git_repo
    (root / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n")
    (root / "a.txt").write_bytes(b"one\ntwo\n")
    _git(root, "add", "--", ".gitattributes", "a.txt")
    _git(root, "commit", "-m", "c1")

    assert cat_file_filters(str(root), "a.txt") == b"one\r\ntwo\r\n"


def test_cat_file_filters_index_form(tmp_git_repo):
    """``rev=\"\"`` читает ИНДЕКС, а не рабочее дерево (как ``show_index_bytes``)."""
    root = tmp_git_repo
    (root / "a.txt").write_bytes(b"one\n")
    run_git(str(root), ["add", "--", "a.txt"])
    (root / "a.txt").write_bytes(b"two\n")

    assert cat_file_filters(str(root), "a.txt", rev="") == b"one\n"


@pytest.fixture
def autocrlf_repo(tmp_git_repo) -> Path:
    """Репозиторий с ``core.autocrlf=true`` и файлом, закоммиченным С LF.

    Конфиг задаётся ЛОКАЛЬНО для репозитория теста — глобальный конфиг машины
    не трогаем, и сцена одинакова на Windows и Linux. Выкладки после коммита
    не было, поэтому на диске файл так и остался с LF: именно этот случай
    ``cat-file --filters`` пересказывает в форме checkout'а (CRLF).
    """
    _git(tmp_git_repo, "config", "core.autocrlf", "true")
    (tmp_git_repo / "a.txt").write_bytes(b"one\ntwo\n")
    _git(tmp_git_repo, "add", "--", "a.txt")
    _git(tmp_git_repo, "commit", "-m", "a.txt")
    return tmp_git_repo


def test_disk_eol_and_with_eol():
    """Стиль концов строк — по преобладанию; ``with_eol`` меняет только их."""
    assert disk_eol(b"a\r\nb\r\n") == b"\r\n"
    assert disk_eol(b"a\nb\n") == b"\n"
    assert disk_eol(b"a\r\nb\n\nc\n") == b"\n"       # LF преобладает
    assert disk_eol(b"no newline") is None
    assert with_eol(b"a\r\nb\n", b"\n") == b"a\nb\n"
    assert with_eol(b"a\nb\r\n", b"\r\n") == b"a\r\nb\r\n"


def test_head_bytes_like_disk_follows_lf_disk_under_autocrlf(autocrlf_repo):
    """Файл написан агентом с LF и закоммичен без выкладки: база — тоже LF.

    Форма фильтра тут CRLF, и по ней каждая строка выходила изменённой, а
    откат переписывал файлу переводы строк.
    """
    root = autocrlf_repo
    assert cat_file_filters(str(root), "a.txt") == b"one\r\ntwo\r\n"
    disk = (root / "a.txt").read_bytes()             # на диске по-прежнему LF
    assert disk == b"one\ntwo\n"
    assert head_bytes_like_disk(str(root), "a.txt", disk) == b"one\ntwo\n"


def test_head_bytes_like_disk_follows_crlf_disk(autocrlf_repo):
    """Диск с CRLF — база тоже CRLF: сырой блоб (LF) сюда не годится."""
    root = autocrlf_repo
    assert cat_file_blob(str(root), "a.txt") == b"one\ntwo\n"
    assert head_bytes_like_disk(str(root), "a.txt", b"x\r\ny\r\n") == b"one\r\ntwo\r\n"


def test_head_bytes_like_disk_without_disk_uses_filter_form(autocrlf_repo):
    """Файла на диске нет — другого свидетельства о «родной» форме тоже нет:
    остаётся форма, которую создал бы checkout."""
    root = autocrlf_repo
    assert head_bytes_like_disk(str(root), "a.txt", None) == cat_file_filters(
        str(root), "a.txt"
    )


def test_head_bytes_like_disk_leaves_binary_alone(autocrlf_repo):
    """Бинарь отдаётся сырым блобом: ``\\r\\n`` внутри него — не концы строк."""
    root = autocrlf_repo
    blob = b"\x00\x01\r\n\x02\n"
    (root / "b.bin").write_bytes(blob)
    _git(root, "add", "--", "b.bin")
    _git(root, "commit", "-m", "b.bin")

    assert head_bytes_like_disk(str(root), "b.bin", b"\x00\n") == blob


def test_head_bytes_like_disk_missing_path_is_none(autocrlf_repo):
    """Пути на ступени нет — ``None``, как у ``cat_file_filters``."""
    assert head_bytes_like_disk(str(autocrlf_repo), "nope.txt", b"x\n") is None


def test_has_custom_filter_detects_and_fails_safe(tmp_git_repo):
    """Кастомный ``filter=`` → True; обычный путь → False; отказ git → True."""
    root = tmp_git_repo
    (root / ".gitattributes").write_bytes(b"secret.txt filter=mangle\n")
    (root / "secret.txt").write_bytes(b"shh\n")
    (root / "plain.txt").write_bytes(b"ok\n")
    _git(root, "add", "--", ".gitattributes", "secret.txt", "plain.txt")
    _git(root, "commit", "-m", "c1")

    assert has_custom_filter(str(root), "secret.txt") is True
    assert has_custom_filter(str(root), "plain.txt") is False

    outside = root.parent / "outside"
    outside.mkdir()
    assert has_custom_filter(str(outside), "whatever.txt") is True


def test_has_custom_filter_covers_worktree_encoding_and_ident(tmp_git_repo):
    """Гвард ловит не только ``filter``: ``working-tree-encoding`` и ``ident``
    тоже меняют содержимое при выкладке, а ``head_bytes_like_disk`` умеет
    воспроизвести только концы строк — базой из сырого блоба такой путь
    получил бы чужую кодировку (и откат записал бы её в файл)."""
    root = tmp_git_repo
    (root / ".gitattributes").write_bytes(
        b"utf16.txt working-tree-encoding=UTF-16\nident.txt ident\n"
    )

    assert has_custom_filter(str(root), "utf16.txt") is True
    assert has_custom_filter(str(root), "ident.txt") is True
    assert has_custom_filter(str(root), "plain.txt") is False


def test_ls_files_set_batch_and_cyrillic(tmp_git_repo):
    root = tmp_git_repo
    rel = "вой на.txt"
    (root / rel).write_bytes("тише\n".encode("utf-8"))
    (root / "b.txt").write_bytes(b"b\n")
    _git(root, "add", "--", rel, "b.txt")
    _git(root, "commit", "-m", "c1")

    assert ls_files_set(str(root), [rel, "b.txt", "missing.txt"]) == {rel, "b.txt"}


def test_ls_files_set_empty_input_returns_empty_set(tmp_git_repo):
    assert ls_files_set(str(tmp_git_repo), []) == set()


def test_head_blob_oids_returns_only_existing(tmp_git_repo):
    root = tmp_git_repo
    (root / "a.txt").write_bytes(b"a\n")
    _git(root, "add", "--", "a.txt")
    _git(root, "commit", "-m", "c1")
    code, out = run_git(str(root), ["rev-parse", "HEAD:a.txt"])
    assert code == 0
    expected_oid = out.decode("utf-8").strip()

    assert head_blob_oids(str(root), ["a.txt", "missing.txt"]) == {"a.txt": expected_oid}


def test_head_blob_oids_empty_input_returns_empty_dict(tmp_git_repo):
    assert head_blob_oids(str(tmp_git_repo), []) == {}


def test_rev_parse_head_returns_full_oid(tmp_git_repo):
    root = tmp_git_repo
    code, out = run_git(str(root), ["rev-parse", "HEAD"])
    assert code == 0

    assert rev_parse_head(str(root)) == out.decode("utf-8").strip()


def test_rev_parse_head_none_on_empty_repo(tmp_path):
    """Репозиторий существует, но ни одного коммита ещё нет."""
    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    root = tmp_path / "empty_repo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(root), check=True, capture_output=True
    )

    assert rev_parse_head(str(root)) is None


def test_head_commit_label_format(tmp_git_repo):
    root = tmp_git_repo
    (root / "a.txt").write_bytes(b"a\n")
    _git(root, "add", "--", "a.txt")
    _git(root, "commit", "-m", "c1")
    code, out = run_git(str(root), ["log", "-1", "--format=%h\t%ct", "--", "a.txt"])
    assert code == 0
    short_hash, _, ts_text = out.decode("utf-8").strip().partition("\t")
    expected_time = datetime.fromtimestamp(int(ts_text)).strftime("%H:%M")

    assert head_commit_label(str(root), "a.txt") == f"{short_hash} · {expected_time}"


def test_head_commit_ts_does_not_glob_onto_a_neighbour(tmp_git_repo):
    """У пути с квадратными скобками время берётся по ЕГО истории, а не соседа.

    Без магии `:(literal)` pathspec `a[1].py` матчит ещё и `a1.py`. Проверяется
    случай, где разница не зависит от часов: сосед закоммичен, а сам путь — нет.
    С магией история пути пуста и ответ `None`; без неё вернулось бы время
    коммита соседа.

    Сравнивать времена двух коммитов тут нельзя: оба ложатся в одну секунду, и
    `%ct` у них совпадает — тест проходил бы по везению (пятый раунд ревью,
    находка 11).
    """
    root = tmp_git_repo
    (root / "a1.py").write_text("neighbour", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "neighbour")

    (root / "a[1].py").write_text("ours", encoding="utf-8")   # НЕ коммитим

    assert head_commit_ts(str(root), "a1.py") is not None
    assert head_commit_ts(str(root), "a[1].py") is None


def test_head_commit_label_none_for_path_without_history(tmp_git_repo):
    """``git log`` без истории пути отвечает кодом 0 и ПУСТЫМ выводом — не ошибка."""
    root = tmp_git_repo
    (root / "new.txt").write_bytes(b"new\n")

    assert head_commit_label(str(root), "new.txt") is None


def test_new_helpers_do_not_run_hooks(tmp_git_repo):
    """Ни один из шести read-only хелперов не должен трогать хуки репозитория.

    По образцу ``test_hooks_are_not_executed``: тот же маркер-хук на
    ``post-index-change``, тот же контрольный прогон обычного git.
    """
    root = tmp_git_repo
    marker = root.parent / "hook-ran-ladder.txt"
    _marker_hook(root, marker)

    # контроль: обычный git add хук выполняет — иначе гвард ничего не доказывает
    (root / "a.txt").write_bytes(b"a\n")
    subprocess.run(["git", "add", "--", "a.txt"], cwd=str(root), capture_output=True)
    if not marker.exists():
        pytest.skip("git не выполняет post-index-change в этой среде — гвард непроверяем")
    marker.unlink()

    rev_parse_head(str(root))
    cat_file_filters(str(root), "seed.txt")
    cat_file_filters(str(root), "a.txt", rev="")
    has_custom_filter(str(root), "seed.txt")
    ls_files_set(str(root), ["seed.txt", "a.txt"])
    head_blob_oids(str(root), ["seed.txt"])
    head_commit_label(str(root), "seed.txt")

    assert not marker.exists()


# --- признак «коммит затронул путь» (C1) -------------------------------------


def test_head_rev_from_files_matches_rev_parse(tmp_git_repo):
    """Читаем ровно то, что резолвит git — но без подпроцесса (горячий путь
    рекордера): loose-ссылка, потом packed, потом ничего."""
    assert head_rev_from_files(str(tmp_git_repo)) == rev_parse_head(str(tmp_git_repo))

    (tmp_git_repo / "seed.txt").write_bytes(b"seed2\n")
    subprocess.run(["git", "commit", "-am", "second"], cwd=str(tmp_git_repo),
                   check=True, capture_output=True)

    assert head_rev_from_files(str(tmp_git_repo)) == rev_parse_head(str(tmp_git_repo))


def test_head_rev_from_files_reads_packed_refs(tmp_git_repo):
    """После `git pack-refs` loose-файла ветки нет — ревизия берётся из packed-refs."""
    subprocess.run(["git", "pack-refs", "--all"], cwd=str(tmp_git_repo), check=True,
                   capture_output=True)
    assert not (tmp_git_repo / ".git" / "refs" / "heads" / "main").exists()

    assert head_rev_from_files(str(tmp_git_repo)) == rev_parse_head(str(tmp_git_repo))


def test_head_rev_from_files_in_linked_worktree(tmp_git_repo, tmp_path):
    """У связанного worktree `.git` — файл, а ссылки лежат в общем каталоге."""
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-b", "side", str(linked)],
                   cwd=str(tmp_git_repo), check=True, capture_output=True)

    assert (linked / ".git").is_file()
    assert head_rev_from_files(str(linked)) == rev_parse_head(str(linked))


def test_head_rev_from_files_without_repo_is_none(tmp_path):
    assert head_rev_from_files(str(tmp_path)) is None


def test_head_rev_from_files_on_unborn_branch_is_none(tmp_path):
    """Ветка без коммитов: HEAD указывает на несуществующую ссылку — признака нет."""
    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    root = tmp_path / "empty"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(root), check=True,
                   capture_output=True)

    assert head_rev_from_files(str(root)) is None


def test_paths_committed_since_lists_only_paths_touched_by_commits(tmp_git_repo):
    before = rev_parse_head(str(tmp_git_repo))
    (tmp_git_repo / "touched.txt").write_bytes(b"a\n")
    (tmp_git_repo / "untouched.txt").write_bytes(b"b\n")
    subprocess.run(["git", "add", "--", "touched.txt", "untouched.txt"],
                   cwd=str(tmp_git_repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "both"], cwd=str(tmp_git_repo), check=True,
                   capture_output=True)
    after_both = rev_parse_head(str(tmp_git_repo))
    (tmp_git_repo / "touched.txt").write_bytes(b"a2\n")
    subprocess.run(["git", "commit", "-am", "touched only"], cwd=str(tmp_git_repo),
                   check=True, capture_output=True)

    assert paths_committed_since(
        str(tmp_git_repo), after_both, ["touched.txt", "untouched.txt"],
    ) == {"touched.txt"}
    assert paths_committed_since(
        str(tmp_git_repo), before, ["touched.txt", "untouched.txt"],
    ) == {"touched.txt", "untouched.txt"}


def test_paths_committed_since_ignores_uncommitted_changes(tmp_git_repo):
    """Правка на диске и даже в индексе коммитом не является — путь не в ответе."""
    rev = rev_parse_head(str(tmp_git_repo))
    (tmp_git_repo / "seed.txt").write_bytes(b"dirty\n")
    subprocess.run(["git", "add", "--", "seed.txt"], cwd=str(tmp_git_repo), check=True,
                   capture_output=True)

    assert paths_committed_since(str(tmp_git_repo), rev, ["seed.txt"]) == set()


def test_paths_committed_since_on_unknown_rev_is_empty(tmp_git_repo):
    """Ревизии больше нет (rebase/gc) — признака нет, а не исключение."""
    assert paths_committed_since(str(tmp_git_repo), "0" * 40, ["seed.txt"]) == set()


def test_paths_committed_since_without_paths_makes_no_call(tmp_git_repo):
    assert paths_committed_since(str(tmp_git_repo), rev_parse_head(str(tmp_git_repo)), []) == set()


# --- пакетные чтения: паритет с пофайловыми ------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


@pytest.fixture
def awkward_repo(tmp_git_repo) -> tuple[Path, list[str]]:
    """Репозиторий с неудобными путями и атрибутами. Отдаёт `(корень, пути)`.

    Пути — те, на которых пакетный разбор мог бы разойтись с пофайловым:
    пробел, кириллица, glob-скобки, бинарь, пустой файл, CRLF; на POSIX — ещё
    кавычка и перевод строки в имени. Атрибуты — все три, что спрашивает
    `has_custom_filter`. В списке путей есть и отсутствующий в HEAD, и каталог."""
    root = tmp_git_repo
    (root / ".gitattributes").write_text(
        "*.enc working-tree-encoding=UTF-16\n*.idt ident\nflt/** filter=foo\n",
        encoding="utf-8")
    files = {
        "plain.py": b"a = 1\n",
        "with space.txt": b"x\n",
        "кириллица.md": "строка\n".encode("utf-8"),
        "br[1].py": b"y = 2\n",
        "bin.dat": b"\x00\x01\x02",
        "empty.txt": b"",
        "crlf.txt": b"one\r\ntwo\r\n",
        "doc.enc": "text\n".encode("utf-16"),       # git перекодирует при add — нужен настоящий UTF-16
        "id.idt": b"$Id$\n",
        "flt/f.txt": b"filtered\n",
    }
    if os.name != "nt":
        files['quo"te.txt'] = b"q\n"
        files["new\nline.txt"] = b"n\n"
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "awkward")
    return root, [*files, "not-in-head.py", "flt"]


def test_custom_filter_paths_matches_the_per_path_answer(awkward_repo):
    """Пакетная проверка атрибутов отвечает ровно то же, что пофайловая."""
    root, rels = awkward_repo
    expected = {rel for rel in rels if has_custom_filter(str(root), rel)}
    assert expected >= {"doc.enc", "id.idt", "flt/f.txt"}      # предмет теста не пуст
    assert custom_filter_paths(str(root), rels) == expected


@pytest.mark.parametrize("rev", ["HEAD", ""])
def test_blobs_at_matches_the_per_path_answer(awkward_repo, rev):
    """Пакетное чтение блобов отдаёт ровно то же, что `cat_file_blob` по одному.

    `rev=""` — индекс, как у `show_index_bytes`. Путь вне ступени и каталог —
    `None`, как у пофайлового чтения."""
    root, rels = awkward_repo
    expected = {rel: cat_file_blob(str(root), rel, rev=rev) for rel in rels}
    assert expected["not-in-head.py"] is None and expected["flt"] is None
    assert blobs_at(str(root), rels, rev=rev) == expected


def test_batched_reads_cost_one_git_process_for_the_whole_batch(awkward_repo, monkeypatch):
    """Пачка путей — один запуск git, а не запуск на путь.

    Ради этого пакетные чтения и заведены: пофайловые запуски на тысячах путей
    стоили десятков секунд (замер: 2000 файлов — 6001 запуск git)."""
    root, rels = awkward_repo
    rels = [rel for rel in rels if "\n" not in rel]   # перевод строки в имени — поштучно
    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    custom_filter_paths(str(root), rels)
    assert len(calls) == 1
    calls.clear()
    blobs_at(str(root), rels)
    assert len(calls) == 1



def test_parallel_git_guards_the_console_once_for_the_whole_batch(tmp_git_repo, monkeypatch):
    """Параллельная пачка сохраняет режим консоли ОДИН раз, а не вокруг каждого запуска.

    У параллельных охранников второй запомнил бы режим, уже испорченный чужим
    дочерним процессом (на Windows он гасит мышь), и под конец вернул бы его."""
    from contextlib import contextmanager

    entered = []

    @contextmanager
    def guard():
        entered.append(1)
        yield

    monkeypatch.setattr(git_ops, "preserve_console_mode", guard)
    answers = git_ops.run_git_many(str(tmp_git_repo), [["rev-parse", "HEAD"]] * 4)
    assert [code for code, _out in answers] == [0, 0, 0, 0]
    assert entered == [1]


def test_index_blobs_in_a_batch_match_show_index_bytes(awkward_repo):
    """Пачка блобов индекса отдаёт то же, что `show_index_bytes` по одному.

    Счётная база шелл-канала читается из индекса; пачка заменяет пофайловый
    `git show :<путь>` и обязана отвечать ровно то же."""
    root, rels = awkward_repo
    expected = {rel: show_index_bytes(str(root), rel) for rel in rels}
    assert blobs_at(str(root), rels, rev="") == expected


def test_add_paths_passes_paths_through_stdin_not_the_command_line(tmp_git_repo, monkeypatch):
    """Пути принятия уходят в git через stdin, а не аргументами.

    Предел командной строки Windows — около 32 тысяч знаков, это порядка тысячи
    путей; принятие команды, тронувшей больше, иначе отказало бы целиком."""
    root = tmp_git_repo
    rels = [f"dir/file_{i}.txt" for i in range(50)]
    for rel in rels:
        (root / rel).parent.mkdir(exist_ok=True)
        (root / rel).write_bytes(b"x\n")
    seen = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (seen.append(a), real(*a, **k))[1])

    assert add_paths(str(root), rels)[0] == 0
    args = seen[0][1]
    assert not any(rel in arg for rel in rels for arg in args)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(root),
                            capture_output=True, text=True, check=True).stdout.split()
    assert sorted(staged) == sorted(rels)


def test_path_lists_split_into_command_line_chunks_give_the_same_answer(awkward_repo, monkeypatch):
    """Списки путей аргументами режутся на куски командной строки — ответ тот же.

    Предел командной строки Windows — около 32 тысяч знаков: экран изменений
    после команды на тысячи файлов иначе получил бы от git отказ, и лестница
    отсчёта молча потеряла бы ступени HEAD и коммиты."""
    root, rels = awkward_repo
    rels = [rel for rel in rels if rel != "flt"]
    since = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(root),
                           capture_output=True, text=True, check=True).stdout.strip()
    whole = (ls_files_set(str(root), rels), head_blob_oids(str(root), rels),
             paths_committed_since(str(root), since, rels))
    assert whole[0] and whole[1] and whole[2]              # предмет не пуст

    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "ARGV_CHUNK_CHARS", 30)
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    chunked = (ls_files_set(str(root), rels), head_blob_oids(str(root), rels),
               paths_committed_since(str(root), since, rels))

    assert chunked == whole
    assert len(calls) > 3


def test_paths_with_non_utf8_bytes_do_not_break_batched_calls(tmp_git_repo):
    """Имя с байтом не из UTF-8 не роняет ни принятие, ни пакетные чтения.

    Такое имя приходит из `git status` суррогатом (`_unquote_c_path` декодирует с
    surrogateescape); строгий `.encode("utf-8")` кидал на нём исключение — в
    принятии оно роняло TUI, в пакетном чтении лишало баз всю пачку."""
    root = str(tmp_git_repo)
    bad = "bad\udcff.txt"

    code, _err = add_paths(root, [bad])
    assert code != 0                                   # пути нет — git отказал, но без исключения
    assert custom_filter_paths(root, [bad, "seed.txt"]) <= {bad}
    assert blobs_at(root, [bad, "seed.txt"]) == {bad: None, "seed.txt": b"seed\n"}


def test_last_commits_in_a_batch_match_the_per_path_answers(awkward_repo, monkeypatch):
    """Последний коммит путей одним `git log` — те же подпись и время, что по одному.

    Экран изменений после коммита правок спрашивал `git log -1` на каждый путь:
    2000 путей — 26 секунд замороженного TUI."""
    root, rels = awkward_repo
    rels = [rel for rel in rels if rel not in ("flt", "not-in-head.py")]
    since = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(root),
                           capture_output=True, text=True, check=True).stdout.strip()
    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    found = last_commits(str(root), since, rels)
    assert len(calls) == 2              # проверка слияний в диапазоне + один git log
    monkeypatch.setattr(git_ops, "_run", real)

    assert set(found) == set(rels)
    for rel in rels:
        short_hash, ts = found[rel]
        assert commit_label(short_hash, ts) == head_commit_label(str(root), rel)
        assert float(ts) == head_commit_ts(str(root), rel)



def test_last_commits_never_disagree_with_the_per_path_answer_across_a_merge(tmp_git_repo):
    """Пачка последних коммитов не отвечает иначе, чем `git log -1 -- путь`, и на слияниях.

    Одиночный `git log` упрощает историю по одному пути и на слиянии идёт за
    родителем, от которого путь пришёл; пачка упрощает её по всем путям разом,
    а `--name-only` у слияния имён не печатает. Разрешённый конфликт давал
    подписью превью отката коммит-родитель вместо самого слияния, и время
    коммита для предупреждения «Хода» — тоже чужое."""
    root = tmp_git_repo
    (root / "c.txt").write_bytes(b"base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    since = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                           capture_output=True, text=True, check=True).stdout.strip()
    _git(root, "checkout", "-qb", "side")
    (root / "c.txt").write_bytes(b"theirs\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", "main")
    (root / "c.txt").write_bytes(b"ours\n")
    _git(root, "commit", "-qam", "ours")
    subprocess.run(["git", "merge", "side"], cwd=str(root), capture_output=True)
    (root / "c.txt").write_bytes(b"resolved\n")
    _git(root, "commit", "-qam", "merge side")

    found = last_commits(str(root), since, ["c.txt", "seed.txt"])

    for rel, (short_hash, ts) in found.items():
        assert commit_label(short_hash, ts) == head_commit_label(str(root), rel), rel


def test_batched_reads_split_long_inputs_into_chunks(awkward_repo, monkeypatch):
    """Пакетные чтения режут вход на куски: память и время одного запуска git — по куску.

    Экран изменений отдавал в одно чтение все закоммиченные пути (до 10 000): один
    запуск под общим таймаутом держал в памяти их блобы разом."""
    root, rels = awkward_repo
    rels = [rel for rel in rels if "\n" not in rel]      # перевод строки в имени — поштучно
    whole = (custom_filter_paths(str(root), rels), blobs_at(str(root), rels))
    calls = []
    real = git_ops._run
    monkeypatch.setattr(git_ops, "_STDIN_CHUNK_PATHS", 3)
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    assert (custom_filter_paths(str(root), rels), blobs_at(str(root), rels)) == whole
    assert len(calls) == 2 * -(-len(rels) // 3)


def test_blobs_of_a_failed_chunk_are_left_unanswered_not_missing(awkward_repo, monkeypatch):
    """Кусок, который git не прочитал, не отвечает `None` за свои пути — он их пропускает.

    `None` значит «пути на ступени нет», и вызывающий его запоминает: отказ одного
    запуска молча стирал бы ступень HEAD у всех путей пачки. Пропущенный путь
    вызывающий спросит по одному."""
    root, rels = awkward_repo
    real = git_ops.run_git
    monkeypatch.setattr(git_ops, "run_git", lambda root_, args, **kw: (
        (-1, b"") if "cat-file" in args and "--batch" in args else real(root_, args, **kw)))

    assert blobs_at(str(root), ["plain.py", "with space.txt"]) == {}


def test_last_commits_across_a_merge_still_answer_in_parallel(tmp_git_repo, monkeypatch):
    """Слияние в диапазоне не откатывает подписи к последовательным запускам.

    На нелинейной истории пачка спрашивает ровно ту же одиночную команду, что и
    по одному, — но параллельно (`run_git_many`): ответ совпадает, а обычный
    `git pull` со слиянием не возвращает экран к 26 с на 2000 путей."""
    root = tmp_git_repo
    for name in ("a.txt", "b.txt"):
        (root / name).write_bytes(b"base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    since = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                           capture_output=True, text=True, check=True).stdout.strip()
    _git(root, "checkout", "-qb", "side")
    (root / "b.txt").write_bytes(b"side\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", "main")
    (root / "a.txt").write_bytes(b"main\n")
    _git(root, "commit", "-qam", "main")
    _git(root, "merge", "-q", "--no-edit", "side")
    many = []
    real_many = git_ops.run_git_many
    monkeypatch.setattr(git_ops, "run_git_many",
                        lambda r, argvs: (many.append(len(argvs)), real_many(r, argvs))[1])

    found = last_commits(str(root), since, ["a.txt", "b.txt"])

    assert many == [2]
    assert {rel: commit_label(*found[rel]) for rel in found} == {
        rel: head_commit_label(str(root), rel) for rel in ("a.txt", "b.txt")
    }


def test_ignored_paths_sees_the_rules_even_for_a_force_added_path(tmp_git_repo):
    """Правила игнорирования — независимо от индекса: `git add -f` путь из них не
    выводит (`--no-index`), а неигнорируемые и несуществующие не отвечаются."""
    from gc_core.agent_edits.git_ops import ignored_paths

    root = tmp_git_repo
    (root / ".gitignore").write_text("*.log\nsecret.txt\n", encoding="utf-8")
    (root / "secret.txt").write_text("s\n", encoding="utf-8")
    (root / "плохо имя.log").write_text("l\n", encoding="utf-8")
    _git(root, "add", "-f", "secret.txt")

    got = ignored_paths(str(root), ["secret.txt", "плохо имя.log", "ok.py", "nope/x.py"])

    assert got == {"secret.txt", "плохо имя.log"}


def test_ignored_paths_treats_a_git_failure_as_ignored(tmp_path):
    """Git отказал (не репозиторий) — весь кусок «игнорируемый»: безопасная сторона."""
    from gc_core.agent_edits.git_ops import ignored_paths

    assert ignored_paths(str(tmp_path), ["a.py", "b.py"]) == {"a.py", "b.py"}


def test_head_tree_tells_a_git_failure_from_an_absent_path(tmp_git_repo, tmp_path):
    """Отсутствие пути в HEAD и отказ git — разные ответы: по отсутствию строится
    ступень «файла нет», откат к которой файл удаляет."""
    from gc_core.agent_edits.git_ops import head_tree

    root = tmp_git_repo
    tracked = next(p.name for p in root.iterdir() if p.is_file())

    tree = head_tree(str(root), [tracked, "never-committed.py"])

    assert set(tree) == {tracked}
    assert head_tree(str(tmp_path / "not-a-repo"), ["a.py"]) is None


def test_status_entries_for_answers_only_about_the_given_paths(tmp_git_repo):
    """Статус по заданным путям: грязные и неотслеживаемые из них — да, остальные
    нет; имя с `[` не разрастается в шаблон; отказ git — `None`."""
    from gc_core.agent_edits.git_ops import status_entries_for

    root = tmp_git_repo
    tracked = next(p.name for p in root.iterdir() if p.is_file())
    (root / tracked).write_text("changed\n", encoding="utf-8")
    (root / "new[1].py").write_text("n\n", encoding="utf-8")
    (root / "new1.py").write_text("n\n", encoding="utf-8")
    (root / "other.txt").write_text("o\n", encoding="utf-8")

    rows = status_entries_for(str(root), [tracked, "new[1].py", "clean-or-missing.py"])

    assert {path for path, _xy in rows} == {tracked, "new[1].py"}
    assert status_entries_for(str(root / "no-such-dir"), [tracked]) is None

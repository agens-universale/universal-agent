"""Capability probes and FakeBackend overrides."""
import os
import re
import shlex
from fnmatch import fnmatch

from backends.capabilities import (
    probe_find_xargs_grep,
    probe_grep_exclude_dir,
    probe_grep_include,
    probe_has_find,
    probe_has_grep,
)
from backends.local import LocalFsBackend
from backends.protocol import ExecResult
from tests.harbor_support.fakes import FakeBackend
from utils.native_fs_search import build_grep_argv_flags


def test_fake_backend_default_caps_false():
    b = FakeBackend({"/work/a.txt": "x"})
    assert b.has_find is False
    assert b.has_grep is False
    assert b.grep_exclude_dir is False
    assert b.grep_include is False


def test_fake_backend_override_caps():
    b = FakeBackend(
        {"/work/a.txt": "x"},
        capabilities={
            "has_find": True,
            "has_grep": True,
            "grep_exclude_dir": True,
            "grep_include": True,
        },
    )
    assert b.has_find is True
    assert b.has_grep is True


def test_local_has_find_grep_smoke_cache():
    b = LocalFsBackend()
    # On a typical Linux CI/dev host GNU find/grep exist.
    f1 = b.has_find
    f2 = b.has_find
    assert f1 is f2
    g1 = b.has_grep
    g2 = b.has_grep
    assert g1 is g2
    # Values are bool (probe may fail in minimal env — still no raise)
    assert isinstance(f1, bool)
    assert isinstance(g1, bool)


# --- probe ≡ production wire -------------------------------------------------


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


class _PosixBox(LocalFsBackend):
    """Simulated POSIX box: a tiny find/grep/xargs over the probe's real tmp tree.

    Probes run with ``is_sandbox`` False, so they build their fixture with host
    ``tempfile``; the sim answers each recipe by walking that tree for real.
    The knobs model exactly the portability holes the probes exist to catch.
    """

    def __init__(
        self,
        *,
        grep_rejects=(),
        find_rejects=(),
        xargs_flags=("-0", "-r"),
        xargs_no_run_if_empty=True,
    ):
        super().__init__()
        self.grep_rejects = set(grep_rejects)
        self.find_rejects = set(find_rejects)
        self.xargs_flags = set(xargs_flags)
        self.xargs_no_run_if_empty = xargs_no_run_if_empty
        self.grep_argvs = []
        self.commands = []

    # --- exec surface ---

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.commands.append(command)
        if command.startswith("command -v "):
            name = shlex.split(command)[2]
            return ExecResult("/usr/bin/" + name + "\n", "", 0)
        if "|" in command:
            return self._pipeline(command)
        return ExecResult("", "", 0)

    def exec_argv(self, argv, *, stdin=None, cwd=None, timeout=None, env=None):
        argv = [str(a) for a in argv]
        self.commands.append(" ".join(argv))
        prog = os.path.basename(argv[0])
        if prog == "find":
            return self._find(argv)
        if prog == "grep":
            return self._grep(argv, stdin=stdin)
        return ExecResult("", prog + ": not found\n", 127)

    # --- simulated tools ---

    def _pipeline(self, command):
        left, _, right = command.partition("|")
        find_argv = shlex.split(left)
        right_argv = shlex.split(right)
        if os.path.basename(right_argv[0]) != "xargs":
            return ExecResult("", "xargs: not found\n", 127)
        rest = right_argv[1:]
        opts = []
        while rest and rest[0].startswith("-") and len(rest[0]) == 2:
            opts.append(rest.pop(0))
        for opt in opts:
            if opt not in self.xargs_flags:
                return ExecResult("", "xargs: invalid option " + opt + "\n", 1)
        found = self._find(find_argv)
        if found.return_code != 0:
            return found
        files = [p for p in found.stdout.split("\0") if p]
        if not files:
            if self.xargs_no_run_if_empty:
                return ExecResult("", "", 0)
            # No -r semantics: the utility still runs once, on empty stdin.
            ran = self._grep(rest, stdin="")
            return ExecResult(ran.stdout, ran.stderr, 123 if ran.return_code else 0)
        return self._grep(rest + files)

    def _find(self, argv):
        args = argv[1:]
        for a in args:
            if a in self.find_rejects:
                return ExecResult("", "find: unknown predicate " + a + "\n", 1)
        if args and args[0] == "-H":
            args = args[1:]
        root, rest = args[0], args[1:]

        prune_names, name_pat, printf_fmt, terminator = [], None, None, None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "(":
                j = rest.index(")", i)
                group = rest[i + 1:j]
                k = 0
                while k < len(group):
                    if group[k] == "-name":
                        prune_names.append(group[k + 1])
                        k += 2
                    else:
                        k += 1
                i = j + 1
                continue
            if tok in ("-mindepth", "-maxdepth"):
                i += 2
                continue
            if tok == "-printf":
                printf_fmt = rest[i + 1]
                i += 2
                continue
            if tok == "-name":
                name_pat = rest[i + 1]
                i += 2
                continue
            if tok in ("-print0", "-print"):
                terminator = tok
                i += 1
                continue
            i += 1

        rows = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if not any(fnmatch(d, p) for p in prune_names)
            ]
            for d in sorted(dirnames):
                rows.append((os.path.join(dirpath, d), True))
            for f in sorted(filenames):
                rows.append((os.path.join(dirpath, f), False))

        if printf_fmt is not None:
            out = "".join(
                os.path.relpath(p, root) + "\t" + ("d" if is_dir else "f") + "\n"
                for p, is_dir in rows
            )
            return ExecResult(out, "", 0)

        files = [p for p, is_dir in rows if not is_dir]
        if name_pat:
            files = [p for p in files if fnmatch(os.path.basename(p), name_pat)]
        sep = "\0" if terminator == "-print0" else "\n"
        return ExecResult("".join(p + sep for p in files), "", 0)

    def _grep(self, argv, *, stdin=None):
        self.grep_argvs.append(list(argv))
        recursive = with_filename = line_number = fixed = ci = False
        include = exclude_dir = None
        pattern, paths = None, []
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok == "--":
                pattern, paths = argv[i + 1], argv[i + 2:]
                break
            if tok in self.grep_rejects:
                return ExecResult("", "grep: unrecognized option " + tok + "\n", 2)
            if tok in ("-R", "-r"):
                recursive = True
            elif tok == "-H":
                with_filename = True
            elif tok == "-n":
                line_number = True
            elif tok == "-F":
                fixed = True
            elif tok == "-i":
                ci = True
            elif tok in ("-E", "--color=never", "--no-messages"):
                pass
            elif tok.startswith("--binary-files="):
                pass
            elif tok.startswith("--include="):
                include = tok.split("=", 1)[1]
            elif tok.startswith("--exclude-dir="):
                exclude_dir = tok.split("=", 1)[1]
            else:
                return ExecResult("", "grep: unrecognized option " + tok + "\n", 2)
            i += 1

        targets = []
        if not paths:
            targets.append(("(standard input)", stdin or ""))
        for p in paths:
            if os.path.isdir(p):
                if not recursive:
                    continue
                for dirpath, dirnames, filenames in os.walk(p):
                    if exclude_dir:
                        dirnames[:] = [
                            d for d in dirnames if not fnmatch(d, exclude_dir)
                        ]
                    for f in sorted(filenames):
                        if include and not fnmatch(f, include):
                            continue
                        fp = os.path.join(dirpath, f)
                        targets.append((fp, _read_text(fp)))
            else:
                if include and not fnmatch(os.path.basename(p), include):
                    continue
                targets.append((p, _read_text(p)))

        show_name = with_filename or len(targets) > 1
        out = []
        for name, content in targets:
            for no, line in enumerate(content.splitlines(), start=1):
                hay = line.lower() if ci else line
                needle = pattern.lower() if ci else pattern
                hit = (needle in hay) if fixed else bool(re.search(needle, hay))
                if not hit:
                    continue
                prefix = (name + ":") if show_name else ""
                prefix += (str(no) + ":") if line_number else ""
                out.append(prefix + line)
        return ExecResult("\n".join(out) + ("\n" if out else ""), "", 0 if out else 1)


def _always_on_grep_flags():
    """Flags the shared builder emits for every production call (no hardcoding)."""
    return set(build_grep_argv_flags(fixed=False)[1:]) & set(
        build_grep_argv_flags(fixed=True)[1:]
    )


def test_grep_probes_run_the_production_flags():
    """Probe argv must carry every always-on production flag, or it proves nothing."""
    always = _always_on_grep_flags()
    for probe in (probe_has_grep, probe_grep_exclude_dir, probe_grep_include):
        box = _PosixBox()
        assert probe(box) is True
        assert box.grep_argvs
        for argv in box.grep_argvs:
            assert always <= set(argv)


def test_grep_probe_smokes_fixed_and_case_insensitive_flags():
    mode_flags = set(build_grep_argv_flags(fixed=True, case_insensitive=True)[1:]) - set(
        build_grep_argv_flags()[1:]
    )
    box = _PosixBox()

    assert probe_has_grep(box) is True

    seen = {flag for argv in box.grep_argvs for flag in argv}
    assert mode_flags <= seen


def test_grep_rejecting_no_messages_is_not_reported_usable():
    """BusyBox case: probe flags pass, a production-only long option does not."""
    box = _PosixBox(grep_rejects=("--no-messages",))

    assert probe_has_grep(box) is False
    assert box.has_grep is False


def test_grep_rejecting_binary_files_is_not_reported_usable():
    box = _PosixBox(grep_rejects=("--binary-files=without-match",))

    assert probe_has_grep(box) is False


def test_backend_without_xargs_r_does_not_report_find():
    """has_find gates the find -print0 | xargs -0 -r grep recipe — smoke it whole."""
    box = _PosixBox(xargs_flags=("-0",))

    assert probe_find_xargs_grep(box) is False
    assert probe_has_find(box) is False
    assert box.has_find is False


def test_backend_that_runs_grep_on_empty_xargs_input_does_not_report_find():
    box = _PosixBox(xargs_no_run_if_empty=False)

    assert probe_find_xargs_grep(box) is False
    assert box.has_find is False


def test_backend_without_find_print0_does_not_report_find():
    box = _PosixBox(find_rejects=("-print0",))

    assert probe_find_xargs_grep(box) is False
    assert box.has_find is False


def test_backend_without_gnu_printf_still_reports_find():
    """has_find gates the prune pipeline, not Harbor's walk -printf listing."""
    box = _PosixBox(find_rejects=("-printf",))

    assert probe_find_xargs_grep(box) is True
    assert probe_has_find(box) is True
    assert box.has_find is True


def test_pipeline_probe_runs_the_production_wire():
    box = _PosixBox()

    assert probe_find_xargs_grep(box) is True

    piped = [c for c in box.commands if "| xargs" in c]
    assert piped
    for cmd in piped:
        assert "-print0" in cmd
        assert "| xargs -0 -r " in cmd


def test_capable_box_reports_all_capabilities():
    """Regression: nothing above narrowed what a fully capable box reports."""
    box = _PosixBox()

    assert box.has_find is True
    assert box.has_grep is True
    assert box.grep_exclude_dir is True
    assert box.grep_include is True


# --- wire format обхода: rel, type, size --------------------------------------

def test_parse_find_printf_entries_reads_sizes_and_tolerates_two_columns():
    from backends.capabilities import parse_find_printf_entries

    rows = parse_find_printf_entries("src\td\t4096\nsrc/a.py\tf\t12\nold.txt\tf\n")
    assert [(e.rel, e.is_dir, e.size) for e in rows] == [
        ("src", True, None),
        ("src/a.py", False, 12),
        ("old.txt", False, None),
    ]


def test_find_printf_wire_format_carries_size():
    from backends.protocol import FIND_PRINTF_FORMAT

    assert FIND_PRINTF_FORMAT == r"%P\t%y\t%s\n"

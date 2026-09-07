"""In-memory stand-in for HarborBackend, for differential tool tests."""
import hashlib
from types import SimpleNamespace

from gc_core.backends.walk_util import (
    WalkEntry,
    iter_files_from_flat,
    iter_walk_entries_from_flat,
    iter_walk_from_flat,
    prune_flat_entries,
)


class FakeBackend:
    """Models a container filesystem as path -> content (str)."""

    is_sandbox = True
    default_exec_timeout = None

    def __init__(self, files=None, *, capabilities=None):
        self.files = dict(files or {})
        # Explicit capability overrides (no real probe). Default: no native CLI.
        self._cap_overrides = dict(
            capabilities
            if capabilities is not None
            else {
                "has_find": False,
                "has_grep": False,
                "grep_exclude_dir": False,
                "grep_include": False,
            }
        )

    @property
    def has_find(self) -> bool:
        return bool(self._cap_overrides.get("has_find", False))

    @property
    def has_grep(self) -> bool:
        return bool(self._cap_overrides.get("has_grep", False))

    @property
    def grep_exclude_dir(self) -> bool:
        return bool(self._cap_overrides.get("grep_exclude_dir", False))

    @property
    def grep_include(self) -> bool:
        return bool(self._cap_overrides.get("grep_include", False))

    # stat primitives
    def exists(self, p):
        # A path "exists" if it is a tracked file OR an implicit directory
        # (has tracked files under it) — mirrors os.path.exists() for dirs,
        # matching LocalFsBackend.exists() semantics for directory-based tools.
        return p in self.files or self.is_dir(p)

    def is_file(self, p):
        return p in self.files

    def is_dir(self, p):
        return any(k != p and k.startswith(p.rstrip("/") + "/") for k in self.files)

    def getsize(self, p):
        return len(self.files[p].encode("utf-8"))

    def sha256(self, p):
        return hashlib.sha256(self.files[p].encode("utf-8")).hexdigest()

    def stat(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        return {
            "size": len(self.files[p].encode("utf-8")), "mtime": 0, "mode": "644",
            "is_file": True, "is_dir": False, "is_symlink": False,
        }

    def walk(self, p):
        """[(relpath, is_dir)] under p, recursively — mirrors `find -mindepth 1`."""
        if not self.is_dir(p):
            raise FileNotFoundError(p)
        base = p.rstrip("/") + "/"
        files, dirs = [], set()
        for k in self.files:
            if not k.startswith(base):
                continue
            rel = k[len(base):]
            parts = rel.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            files.append(rel)
        return [(d, True) for d in sorted(dirs)] + [(f, False) for f in sorted(files)]

    def walk_entries(self, p, *, prune_names=(), prune_dot_dirs=False):
        """Sized rows like `find -printf %s`; prune like `( -type d -name … ) -prune`."""
        base = p.rstrip("/") + "/"
        entries = [
            WalkEntry(
                rel,
                is_dir,
                None if is_dir else len(self.files[base + rel].encode("utf-8")),
            )
            for rel, is_dir in self.walk(p)
        ]
        return prune_flat_entries(entries, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs)

    def iter_walk_entries(self, path, *, should_descend=None, prune_names=(), prune_dot_dirs=False):
        yield from iter_walk_entries_from_flat(
            self.walk_entries(path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

    def iter_walk(self, path, *, should_descend=None):
        yield from iter_walk_from_flat(
            self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

    def iter_files(self, path, *, should_descend=None):
        yield from iter_files_from_flat(
            self.walk(path),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

    # read / write
    def read(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        return self.files[p]

    def read_bytes(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        return self.files[p].encode("utf-8")

    def write(self, p, content, encoding="utf-8"):
        self.files[p] = content

    def write_bytes(self, p, data):
        self.files[p] = data.decode("utf-8")

    def lexists(self, p):
        return self.exists(p)

    def is_symlink(self, p):
        return False

    def realpath(self, p):
        return p

    def readable(self, p):
        return True

    def writable(self, p):
        return True

    def is_hidden(self, p):
        return p.rsplit("/", 1)[-1].startswith(".")

    def stat_info(self, p, follow_symlinks=False):
        st = self.stat(p)
        return {
            "size": st["size"], "mtime": st["mtime"], "mode": int(str(st["mode"]), 8),
            "is_file": st["is_file"], "is_dir": st["is_dir"], "is_symlink": st["is_symlink"],
        }

    def iter_lines(self, p):
        return iter(self.read(p).splitlines(keepends=True))

    def read_head_bytes(self, p, n):
        return self.read_bytes(p)[:n]

    def sha256_bytes(self, p):
        return self.sha256(p)

    def append(self, p, content, encoding="utf-8"):
        self.files[p] = self.files.get(p, "") + content

    def copy(self, src, dst):
        self.files[dst] = self.files[src]

    def makedirs(self, p):
        pass  # dict-modeled fs: dirs are implicit

    def listdir(self, p):
        base = p.rstrip("/") + "/"
        names = {}
        for rel, is_d in self.walk(p):
            if "/" not in rel:
                names[rel] = is_d
        return sorted(names.items())

    def normpath(self, p):
        return p.rstrip("/") or "/"

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    def isabs(self, p):
        return p.startswith("/")

    def default_cwd(self):
        return None

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        raise NotImplementedError("exec parity tests use their own recording fake")

    def run_python(self, code, *, cwd=None, timeout=None, env=None):
        raise NotImplementedError


def bind_os_backend(tool, backend, *, config_dict=None):
    """Attach ``backend`` via minimal RunContext-like ``_ctx`` (unit/diff tests)."""
    from gc_core.execution_workspace import build_execution_workspace

    cfg = config_dict if config_dict is not None else {}
    tool._ctx = SimpleNamespace(
        config_dict=cfg,
        os_backend=backend,
        workspace=build_execution_workspace(backend, cfg),
    )
    # Mirror AbstractSimpleTool.__init__ defaults for attributes accessed by
    # tool methods (e.g. exec tools read self._state / self._call_id).
    # `self._config_dict` is read directly by fs tools (fs_size_limits) and
    # by read_file's char-limit lookup; AbstractSimpleTool.__init__ sets it
    # from kwargs, but tests here build the tool via __new__ (skipping
    # __init__), so expose it explicitly to avoid AttributeError.
    if not hasattr(tool, "_state"):
        tool._state = None
    if not hasattr(tool, "_call_id"):
        tool._call_id = None
    tool._config_dict = cfg
    return tool


def strip_path_fields(env, *keys):
    env = dict(env)
    for k in keys:
        env.pop(k, None)
    return env

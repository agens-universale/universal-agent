import json

from tests.harbor_support.fakes import bind_os_backend
from backends.protocol import FsBackend
from tools.fs.write_to_file_tool import WriteToFile


class RecordingSandboxBackend(FsBackend):
    """In-memory sandbox backend: enough for write_to_file to succeed."""

    is_sandbox = True

    def __init__(self):
        self.files: dict[str, str] = {}

    # --- stat ---
    def exists(self, path):
        return path in self.files or path == "/testbed"

    def lexists(self, path):
        return self.exists(path)

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return path == "/testbed"

    def is_symlink(self, path):
        return False

    def getsize(self, path):
        return len(self.files.get(path, ""))

    def stat_info(self, path, follow_symlinks=False):
        return {
            "size": self.getsize(path),
            "mtime": 0,
            "mode": 0o644,
            "is_file": self.is_file(path),
            "is_dir": self.is_dir(path),
            "is_symlink": False,
        }

    def readable(self, path):
        return True

    def writable(self, path):
        return True

    def realpath(self, path):
        return path

    def is_hidden(self, path):
        return path.rsplit("/", 1)[-1].startswith(".")

    # --- read ---
    def read(self, path):
        return self.files[path]

    def read_bytes(self, path):
        return self.files[path].encode("utf-8")

    def iter_lines(self, path):
        return iter(self.read(path).splitlines(keepends=True))

    def read_head_bytes(self, path, n):
        return self.read(path).encode()[:n]

    def sha256_bytes(self, path):
        return ""

    # --- write ---
    def write(self, path, content, encoding="utf-8"):
        self.files[path] = content

    def write_bytes(self, path, data):
        self.files[path] = data.decode()

    def append(self, path, content, encoding="utf-8"):
        self.files[path] = self.files.get(path, "") + content

    def copy(self, src, dst):
        self.files[dst] = self.files[src]

    def makedirs(self, path):
        return None

    # --- traverse ---
    def listdir(self, path):
        return []

    def walk(self, path):
        return []

    def iter_walk(self, path, *, should_descend=None):
        return iter(())

    def iter_files(self, path, *, should_descend=None):
        return iter(())

    # --- path semantics ---
    def normpath(self, path):
        return path.rstrip("/") or "/"

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    # --- exec ---
    def exec(self, command, *, cwd=None, timeout=None, env=None):
        raise NotImplementedError

    def run_python(self, code, *, cwd=None, timeout=None, env=None):
        raise NotImplementedError

    def default_cwd(self):
        return "/testbed"


def test_write_to_file_envelope_reports_container_path(monkeypatch, tmp_path):
    """In sandbox mode the reported path must not be rewritten against the host cwd."""
    monkeypatch.chdir(tmp_path)                      # host cwd differs from container cwd
    backend = RecordingSandboxBackend()
    tool = bind_os_backend(WriteToFile(), backend)

    out = tool(path="/testbed/mod.py", content="x")   # tools are callables (__call__)
    envelope = json.loads(out) if isinstance(out, str) else out

    assert envelope["ok"] is True
    assert envelope["path"] == "/testbed/mod.py"
    assert not envelope["path"].startswith(str(tmp_path))
    assert backend.files["/testbed/mod.py"] == "x"

import json

from gc_core.backends.protocol import FsBackend
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.harbor_fakes import bind_os_backend
from gc_testkit.helpers.tool_ctx import CallableTool


class RecordingSandboxBackend(FsBackend):
    """In-memory sandbox backend: enough for write_to_file to succeed."""

    is_sandbox = True

    def __init__(self):
        self.files: dict[str, str] = {}

    # --- stat ---
    async def exists(self, path):
        return path in self.files or path == "/testbed"

    async def lexists(self, path):
        return self.exists(path)

    async def is_file(self, path):
        return path in self.files

    async def is_dir(self, path):
        return path == "/testbed"

    async def is_symlink(self, path):
        return False

    async def getsize(self, path):
        return len(self.files.get(path, ""))

    async def stat_info(self, path, follow_symlinks=False):
        return {
            "size": self.getsize(path),
            "mtime": 0,
            "mode": 0o644,
            "is_file": self.is_file(path),
            "is_dir": self.is_dir(path),
            "is_symlink": False,
        }

    async def readable(self, path):
        return True

    async def writable(self, path):
        return True

    async def realpath(self, path):
        return path

    async def is_hidden(self, path):
        return path.rsplit("/", 1)[-1].startswith(".")

    # --- read ---
    async def read(self, path):
        return self.files[path]

    async def read_bytes(self, path):
        return self.files[path].encode("utf-8")

    async def iter_lines(self, path):
        return iter(self.read(path).splitlines(keepends=True))

    async def read_head_bytes(self, path, n):
        return self.read(path).encode()[:n]

    async def sha256_bytes(self, path):
        return ""

    # --- write ---
    async def write(self, path, content, encoding="utf-8"):
        self.files[path] = content

    async def write_bytes(self, path, data):
        self.files[path] = data.decode()

    async def append(self, path, content, encoding="utf-8"):
        self.files[path] = self.files.get(path, "") + content

    async def copy(self, src, dst):
        self.files[dst] = self.files[src]

    async def makedirs(self, path):
        return None

    # --- traverse ---
    async def listdir(self, path):
        return []

    async def walk(self, path):
        return []

    async def iter_walk(self, path, *, should_descend=None):
        return iter(())

    async def iter_files(self, path, *, should_descend=None):
        return iter(())

    # --- path semantics ---
    def normpath(self, path):
        return path.rstrip("/") or "/"

    def join(self, base, name):
        return base.rstrip("/") + "/" + name

    # --- exec ---
    def exec(self, command, *, cwd=None, timeout=None, env=None):
        raise NotImplementedError

    def run_python(
        self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None
    ):
        raise NotImplementedError

    def default_cwd(self):
        return "/testbed"


def test_write_to_file_envelope_reports_container_path(monkeypatch, tmp_path):
    """In sandbox mode the reported path must not be rewritten against the host cwd."""
    monkeypatch.chdir(tmp_path)  # host cwd differs from container cwd
    backend = RecordingSandboxBackend()
    tool = bind_os_backend(CallableTool(WriteToFile()), backend)

    out = tool(
        path="/testbed/mod.py", content="x"
    )  # tools are callables (__call__)
    envelope = json.loads(out) if isinstance(out, str) else out

    assert envelope["ok"] is True
    assert envelope["path"] == "/testbed/mod.py"
    assert not envelope["path"].startswith(str(tmp_path))
    assert backend.files["/testbed/mod.py"] == "x"

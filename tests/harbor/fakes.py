"""In-memory stand-in for HarborBackend, for differential tool tests."""
import hashlib


class FakeBackend:
    """Models a container filesystem as path -> content (str)."""

    def __init__(self, files=None):
        self.files = dict(files or {})

    # stat primitives
    def exists(self, p):
        return p in self.files

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

    # read / write
    def read(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        return self.files[p]

    def read_bytes(self, p):
        if p not in self.files:
            raise FileNotFoundError(p)
        return self.files[p].encode("utf-8")

    def write(self, p, content):
        self.files[p] = content

    def write_bytes(self, p, data):
        self.files[p] = data.decode("utf-8")


def strip_path_fields(env, *keys):
    env = dict(env)
    for k in keys:
        env.pop(k, None)
    return env

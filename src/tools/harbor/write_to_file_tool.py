"""Harbor variant of fs_write_file.

Faithful 1:1 reimplementation of ``WriteToFile.__call__`` for the External adapter:
same validation, same mode/newline/backup/result-check semantics, same ``_result``
envelope. The ONLY difference is physical I/O — existence/read/write/getsize go through
HarborBackend (the task container) instead of the host filesystem. The base
``@enforce_write_guard`` decorator is intentionally dropped: the container is the sandbox.
"""
import os

from tools.fs.write_to_file_tool import WriteToFile


class HarborWriteToFile(WriteToFile):
    def __call__(
        self,
        path: str,
        content: str,
        mode: str = "overwrite",
        make_dirs: bool = False,
        encoding: str = "utf-8",
        newline: str = "preserve",
        backup: bool = False,
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend

        # --- argument validation (identical to base) ---
        if not isinstance(path, str):
            return self._result(ok=False, path=str(path), mode=mode,
                                error={"type": "invalid_argument", "field": "path"})
        if not isinstance(content, str):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "content"})
        if not isinstance(mode, str) or mode not in ("overwrite", "create_only", "fail_if_missing"):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "mode"})
        if not isinstance(make_dirs, bool):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "make_dirs"})
        if not isinstance(encoding, str) or not encoding:
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "encoding"})
        if not isinstance(newline, str) or newline not in ("preserve", "lf", "crlf"):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "newline"})
        if not isinstance(backup, bool):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "backup"})

        # os.path.isabs is a pure string check — no I/O, safe to use for container paths.
        if not os.path.isabs(path):
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "invalid_argument", "field": "path",
                                       "message": "path must be absolute"})

        # --- existence / mode checks (I/O via backend) ---
        exists = backend.exists(path)
        if mode == "create_only" and exists:
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "already_exists"})
        if mode == "fail_if_missing" and not exists:
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "not_found"})

        # --- parent directory check (I/O via backend) ---
        # backend.write handles mkdir -p internally, so only gate when make_dirs=False.
        parent = os.path.dirname(path) or "."
        if not backend.is_dir(parent):
            if make_dirs:
                # backend.write will create parents; nothing extra needed here.
                pass
            else:
                return self._result(ok=False, path=path, mode=mode,
                                    error={"type": "parent_not_found"})

        # --- newline normalization (pure string, no I/O) ---
        normalized = content
        if newline == "lf":
            normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
        elif newline == "crlf":
            normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")

        # --- backup (I/O via backend) ---
        backup_path = None
        if backup and exists:
            backup_path = f"{path}.bak"
            try:
                backend.write(backup_path, backend.read(path))
            except Exception as e:
                return self._result(ok=False, path=path, mode=mode,
                                    error={"type": "backup_failed", "message": str(e)})

        # --- write (I/O via backend) ---
        try:
            backend.write(path, normalized)
        except Exception as e:
            return self._result(ok=False, path=path, mode=mode,
                                error={"type": "write_failed", "message": str(e)})

        # --- post-write size check (I/O via backend) ---
        try:
            bytes_written = backend.getsize(path)
        except Exception:
            bytes_written = 0

        return self._result(
            ok=True,
            path=path,
            mode=mode,
            created=not exists,
            bytes_written=bytes_written,
            backup_path=backup_path,
            error=None,
        )

"""Harbor variant of fs_append_file.

Faithful 1:1 reimplementation of ``AppendToFile.__call__`` for the External adapter:
same validation, same separator logic (including the X_PRESERVE_EMPTY_STRING default
``\\n\\n``), same ``_result`` envelope. The ONLY difference is physical I/O — file
existence/size/read/write go through HarborBackend (the task container) instead of the
host filesystem. The base ``@enforce_write_guard`` decorator is intentionally dropped:
the container is the sandbox.
"""
import os

from tools.fs.append_to_file import AppendToFile


class HarborAppendToFile(AppendToFile):
    def __call__(
        self,
        path: str,
        content: str,
        separator: str = "\n\n",
        create_if_missing: bool = False,
        make_dirs: bool = False,
        encoding: str = "utf-8",
        **kwargs,
    ) -> str:
        backend = self._ctx.harbor_backend

        if not isinstance(path, str):
            return self._result(
                ok=False,
                path=str(path),
                separator=separator,
                error={"type": "invalid_argument", "field": "path"},
            )

        if not isinstance(content, str):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "content"},
            )

        if not isinstance(separator, str):
            return self._result(
                ok=False,
                path=path,
                separator="",
                error={"type": "invalid_argument", "field": "separator"},
            )
        if not isinstance(create_if_missing, bool):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "create_if_missing"},
            )
        if not isinstance(make_dirs, bool):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "make_dirs"},
            )
        if not isinstance(encoding, str) or not encoding:
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "invalid_argument", "field": "encoding"},
            )

        if not os.path.isabs(path):
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={
                    "type": "invalid_argument",
                    "field": "path",
                    "message": "path must be absolute",
                },
            )

        # Existence check (via backend, not host fs)
        exists = backend.exists(path)
        if not exists:
            if not create_if_missing:
                return self._result(
                    ok=False,
                    path=path,
                    separator=separator,
                    error={"type": "not_found"},
                )
            # create_if_missing=True: check parent directory, mirroring base behaviour.
            parent = os.path.dirname(path) or "."
            if not backend.is_dir(parent):
                if not make_dirs:
                    return self._result(
                        ok=False,
                        path=path,
                        separator=separator,
                        error={"type": "parent_not_found"},
                    )
                # make_dirs=True: backend.write will create parents implicitly.

        try:
            before = backend.getsize(path) if exists else 0
            current = backend.read(path) if exists else ""
            backend.write(path, current + separator + content)
            after = backend.getsize(path)
            appended = max(0, after - before)
        except Exception as e:
            return self._result(
                ok=False,
                path=path,
                separator=separator,
                error={"type": "append_failed", "message": str(e)},
            )

        return self._result(
            ok=True,
            path=path,
            separator=separator,
            created=not exists,
            bytes_appended=appended,
            error=None,
        )

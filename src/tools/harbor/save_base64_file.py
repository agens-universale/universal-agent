"""Harbor variant of fs_save_base64_file.

Faithful 1:1 reimplementation of ``SaveBase64File.__call__`` for the External adapter:
same validation, same in-process base64 decoding, same decode_failed handling, same
``_result`` envelope. The ONLY differences from the base are:
- parent check goes through ``backend.is_dir`` (container filesystem, not host).
- bytes are written via ``backend.write_bytes(path, raw)`` (native upload, not open/replace).
- ``bytes_written = len(raw)`` (same formula as base).

No ``@enforce_write_guard`` decorator: the container is the sandbox.
"""
import base64
import os

from tools.fs.save_base64_file import SaveBase64File


class HarborSaveBase64File(SaveBase64File):
    def __call__(self, path: str, data: str, **kwargs) -> str:
        backend = self._ctx.harbor_backend
        _result = self._result

        if not isinstance(path, str) or not path.strip():
            return _result(ok=False, path=str(path), error={"type": "invalid_argument", "field": "path"})

        if not os.path.isabs(path):
            return _result(ok=False, path=path, error={"type": "invalid_argument", "field": "path", "message": "path must be absolute"})

        if not isinstance(data, str) or not data.strip():
            return _result(ok=False, path=path, error={"type": "invalid_argument", "field": "data"})

        try:
            raw = base64.b64decode(data, validate=False)
        except Exception as e:
            return _result(ok=False, path=path, error={"type": "decode_failed", "message": str(e)})

        parent = os.path.dirname(path) or "."
        if not backend.is_dir(parent):
            return _result(ok=False, path=path, error={"type": "parent_not_found"})

        try:
            backend.write_bytes(path, raw)
        except Exception as e:
            return _result(ok=False, path=path, error={"type": "write_failed", "message": str(e)})

        return _result(ok=True, path=path, bytes_written=len(raw))

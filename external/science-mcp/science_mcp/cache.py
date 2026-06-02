from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional


def hash_key(provider: str, url_path: str, params: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"p": provider, "u": url_path, "q": dict(sorted(params.items()))},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DiskCache:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("expires_at", 0) < time.time():
            return None
        return data.get("value")

    def set(self, key: str, value: Any, *, ttl_sec: float) -> None:
        if not self.enabled:
            return
        payload = {"expires_at": time.time() + ttl_sec, "value": value}
        try:
            self._path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

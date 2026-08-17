"""Короткая строка-сводка онбординга: «сервер X · токен Y · модель Z · MCP N».
Самоподписанные поля, короткие формы, усечение — одна строка без переноса."""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

_CAP = 28   # кап на длину хоста и id модели


def _host_port(url: str) -> str:
    if not url:
        return "—"
    p = urlparse(url if "://" in url else "//" + url, scheme="")
    hp = p.netloc or p.path.split("/")[0]
    return _clip(hp) if hp else "—"


def _clip(s: str) -> str:
    return s if len(s) <= _CAP else s[: _CAP - 1] + "…"


def _token(auth: Optional[str]) -> str:
    if auth is None:
        return "—"
    if auth == "":
        return "нет"
    return "Bearer"          # никогда не литерал


def summary_line(*, url: str, auth: Optional[str], model: str, mcp_count: int) -> str:
    server = _host_port(url)
    tok = _token(auth)
    mdl = _clip(model) if model else "—"
    mcp = str(mcp_count) if mcp_count else "—"
    return f"сервер {server}   ·   токен {tok}   ·   модель {mdl}   ·   MCP {mcp}"

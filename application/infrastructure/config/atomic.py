"""配置文件的原子写与回退（不依赖 pydantic/ruamel，供各配置存储共用）。"""
from __future__ import annotations

import os
import tempfile


def atomic_write(path: str, text: str, *, backup: bool = True) -> None:
    """临时写 -> 原子替换，保留可回退备份（02 §10）。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if backup and os.path.exists(path):
            os.replace(path, path + ".bak")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def rollback(path: str) -> None:
    """回退到 .bak（原子写保留的上一版本）。"""
    bak = path + ".bak"
    if os.path.exists(bak):
        os.replace(bak, path)


__all__ = ["atomic_write", "rollback"]

"""圆宿主（``circle38_host`` / ``circle38_host.exe``）定位与自动发现（DEC-053/072）。

**为什么手工启动的 circle38 连不上**：主程序与宿主之间**不是端口/服务**，
而是主程序用 :class:`SubprocessTransport` 自己拉起子进程、经 stdin/stdout
长度前缀 JSON 通信。手工双击出来的进程与主程序没有任何管道相连，主程序也
从不去找它——所以「运行一次找圆」只能报"宿主不可用"。

**现在的做法**：``main.py real`` 启动时按下面的顺序解析宿主命令（DEC-072 起
Windows 找 ``circle38_host.exe``，Linux 找无扩展名的 ``circle38_host``）：

1. 显式 ``--circle-host-cmd``（仍最优先；既有的**整条文件路径**原样当一个参数，
   路径带空格不会被拆断——Windows 上双击 exe 的目录常带空格）；
2. 主程序所在目录的 ``circle38_host[.exe]``；
3. 主程序所在目录下 onedir 文件夹 ``circle38_host\\circle38_host[.exe]``
   （PyInstaller 打包产物整目录拷过来即可）；
4. 源码布局 ``dist_host\\circle38_host[.exe]``（Windows 开发机）或
   ``dist_linux/tower-ops-bundle/circle38_host/``（Linux 开发机）。

找到即由 :func:`application.bootstrap.production.build_production_stack`
**立刻拉起**（不等第一次找圆）；拉起失败只记通知，界面照常开，找圆时如实
报出具体原因。
"""
from __future__ import annotations

import os

from application.bootstrap import app_paths

# 兼容旧调用方：Windows 宿主文件名（Linux 无扩展名，见 _host_names()）
HOST_EXE = "circle38_host.exe"


def _host_names() -> tuple[str, ...]:
    return (HOST_EXE, "circle38_host") if os.name == "nt" else ("circle38_host", HOST_EXE)


def host_candidates(root: str | None = None) -> list[str]:
    """按优先级给出候选宿主路径（只列路径，不判存在）。"""
    base = root or app_paths.app_root()
    out: list[str] = []
    for name in _host_names():
        out += [
            os.path.join(base, name),
            os.path.join(base, "circle38_host", name),
            os.path.join(base, "dist_host", "circle38_host", name),
            os.path.join(base, "dist_linux", "tower-ops-bundle", "circle38_host", name),
        ]
    return out


def find_circle_host(root: str | None = None) -> str | None:
    """在主程序所在目录（或给定 ``root``）找宿主；找到返回路径。"""
    for path in host_candidates(root):
        if os.path.isfile(path):
            return path
    return None


def resolve_circle_host_cmd(
        explicit: str = "", *, root: str | None = None) -> tuple[list[str] | None, str]:
    """解析真实模式下圆宿主的启动命令。

    返回 ``(cmd, note)``：``cmd`` 为 ``None`` 表示没找到（视觉判定/找圆会明确
    报错，不给假结果）；``note`` 是给控制台/日志的说明，区分"显式指定"与
    "自动发现"。
    """
    text = (explicit or "").strip()
    if text:
        if os.path.isfile(text):
            # 整条路径就是一个可执行文件：原样保留（带空格也不拆）
            return [text], f"显式 --circle-host-cmd: {text}"
        # 兼容"解释器 + 参数"的写法（如 python.exe -m ... entry）
        return text.split(), f"显式 --circle-host-cmd: {text}"

    found = find_circle_host(root)
    if found is not None:
        return [found], f"自动发现（主程序同目录）: {found}"
    return None, (
        "未找到圆宿主（主程序同目录的 circle38_host 或 "
        "circle38_host/circle38_host）；视觉判定与找圆会明确报错"
    )


__all__ = ["HOST_EXE", "find_circle_host", "host_candidates", "resolve_circle_host_cmd"]

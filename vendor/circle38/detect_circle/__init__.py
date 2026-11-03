"""原始算法模块 (timesai_tools C 扩展)

将 timesai_tools.pyd + 配套 OpenCV DLL + detect_circle.py 封装在此包内,
UI 通过 `from original_algo import detect_circle_imp` 直接调用, 无需子进程.

依赖:
  - Python 3.8.10 (pyd 编译版本, 不兼容 3.8.20+)
  - timesai_tools.cp38-win_amd64.pyd
  - opencv_*.dll, msvcp140.dll, vcruntime140.dll (本地 OpenCV 4.5.0 运行时)
"""
from .detect_circle import detect_circle_imp

__all__ = ['detect_circle_imp']

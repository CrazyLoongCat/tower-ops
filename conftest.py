"""tower-ops 离线骨架测试入口.

把仓库根目录加入 sys.path, 使 `import application...` 可用 (tests/offline 下的用例).
不导入任何设备/SDK; 离线测试默认不收集 tests/hardware.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

"""边缘侧（S100）服务包。

**不做 eager 导入**：子模块按需导入，避免把某个子模块的依赖（如 pyzmq / 板端 SDK）
带进不需要它的调用方（本项目既有教训：包入口 eager 导入曾把 3.9+ 语法带进 3.8 链路）。

    camera_registry  按序列号绑定通道、启停与健康（E07）
    camera_actor     单通道 Actor：epoch / sequence / 健康 / 有界帧缓存（E07）
    frame_store      有界帧缓存：复制只读持有 + 原始深度单位与对齐内参（E07）
"""
from __future__ import annotations

__all__: list[str] = []

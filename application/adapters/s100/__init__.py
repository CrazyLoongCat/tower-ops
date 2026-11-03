"""S100 边缘相机适配 (E09)。

包入口**不做 eager 导入**：只暴露子模块路径。子模块按需导入，避免把某个子模块的
依赖（如 pyzmq）带进不需要它的调用方 —— 这是本项目踩过的坑（见交接说明 §8.4）。

    framing            V2 多段帧常量/剖面 (draft)
    multipart_decoder  多段解码与严格校验
    session            唯一 REQ owner 会话
    fake_service       离线 Fake V2 服务 (进程内)
    client             S100CameraService 相机契约
"""
from __future__ import annotations

__all__: list[str] = []

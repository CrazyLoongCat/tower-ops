"""application.contracts - DTO、稳定标识、状态枚举、单位与端口.

本层约束 (12 §4.1, 02 §1):
- 稳定标识不依赖 UI 标签/IP/显示顺序;
- 领域长度 mm / 角度 deg / 时长 ms, SDK 边界显式转换;
- JointTarget 与 CartesianPose 严格分型, 深度保留 uint16 + depth_scale_m;
- 无 Qt / 厂家 SDK / 串口 / ZMQ 副作用。

**本包入口故意不做 eager 导入**: 部分子模块 (ids/targets/frames) 依赖 pydantic,
而 `enums`/`results` 位于 **Python 3.8 圆检测宿主**的导入链路上。
若此处 import 全部子模块, 宿主会连带加载 pydantic -> 3.8 报
`cannot import name 'Annotated' from 'typing'`。
请按需显式导入子模块, 例如 `from application.contracts.enums import ImageSpace`。
"""

# tower-ops（工程名 towerops）

杆塔智能作业系统 v0.5 —— **单主应用 + 独立 cp38 圆检测宿主** 的离线骨架。
本轮为**第一阶段离线实现**：P00/P01 台账、A01～A19 离线任务、A21～A24 真实适配器离线实现。

> ⚠️ 本轮**未授权**运行真实设备。`tests/hardware/` 默认跳过；离线测试只使用 Fake/Stub，
> 不打开真实串口/SDK/相机，不建立真实运动通道。离线结果不代表 G1 或现场验收。

## 环境自检（ENV-MAIN）

主环境（Python 3.12）建好后，先自检一次，输出解释器路径/版本/位数与依赖版本：

```powershell
E:\AnacondaEnvs\towerops\python.exe -m pip install -e .      # 装主程序依赖（首次）
E:\AnacondaEnvs\towerops\python.exe scripts\check_env.py      # 打印 env-report JSON
```

主程序依赖含 `pyserial`（串口链路）与 `pyzmq`（S100 连通性探测 / V2 相机通道）；两者在代码里
**惰性导入**，未装也能跑离线套件，但设备配置页对相应协议会**降级**（见下）。
把 `env-report JSON` 的输出贴回，即可回填 `doc/实施记录/环境清单.json` 并把 A02 推进到带证据状态。
脚本只用标准库，装依赖前也能运行；它不联网、不连接设备、不修改环境。

## 运行程序

入口是工程根目录的 `main.py`（在 `tower-ops` 目录下执行）：

```bash
# 图形界面（离线，Fake 硬件；无真实设备时看界面/调试流程）
python main.py gui

# 真实设备模式（连接全部设备、允许运动）
python main.py real

# 查看参数
python main.py --help
```

界面（**七页**：六页按 03 §3 与 V0.2 主稿，另加移植的「运动方案」）：
**作业台 / 手动控制 / 视觉调试 / 配方流程 / 运动方案 / 设备配置 / 日志诊断**。
作业台：选配方 →（离线草稿需勾"允许离线草稿"）→ **启动作业**；执行导航条、步骤时间线、DS86 证据区、任务依赖条、操作栏。
右卡「相机通道」页签 = **设备配置里已启用的相机**（按配置顺序，启用/停用通道即自动增减），每个页签显示该相机本次运行最近的抓拍帧；框/圆调整目前只在 DS86 的本步视觉帧上开放，其余页签只读预览（DEC-051）。
视觉判定出现框/圆后，右卡「定位圆」下拉（或直接点图上的圆）可让操作员**指定用哪个圆做后续引导定位**；首项「不指定」= 引导定位重新找圆。显式选择记 `vision.select_target`（operator 留证），不重算判定、不写检测/配方（DEC-054）。
`停止全部` 在顶部常驻（所有页面可发停止请求）；未验证能力（D405 通道、未核对机构动作）显示**禁用/未绑定**，不伪装 READY。
**设备配置页**：设备节点 / 链路板映射 / 相机通道 三张表均可**新增/修改/删除**，每条可配协议
（serial / Modbus-RTU over TCP / JSON-line over TCP / ZMQ REQ/REP / RTSP / HTTP / 厂商 SDK over TCP / 本地 SDK）
与连接参数（串口 或 IP+端口），并提供**测试连接**（只做连通性握手，不下发动作指令）与**连接/断开**。
**状态只有两个（DEC-086/087）：`未启用`（红） / `已启用`（绿）** —— 配置启用且
（会话打开 / 探针到达，含降级与本地 SDK“无需探针”）即显示 `已启用`；`main.py real`
启动时会先探测一遍已配置设备来初始化状态；具体原因仍看状态列悬停提示。
配置写入 `configs/devices/devices.yaml`（原子写 + `.bak` 备份），仓库内该文件**已导入旧工程的设备连接值**，
每条 `notes` 记有来源文件/字段与被舍弃的冲突候选值；`confirmed: false` 的记录保持 `未启用`，
悬停提示会说明“待核对/未配置”。

**视觉调试页**：通道列表**取自设备配置**（与设备配置页联动），可选中通道、连接/断开、
**单次抓拍**并显示**彩色图与深度伪彩**（`components/image_view.py`）与帧元数据（来源帧/序号/尺寸 dtype/
深度比例/有效深度比/内参），可运行判定并查看状态/指标/单位/来源帧。无适配器的通道抓拍会给出明确原因；
离线合成图在界面标注"离线合成（FakeCamera）"，不冒充真实相机图像。
`python scripts/render_ui.py out/` 可把六页离屏渲染成 PNG（含拼图 `ui_all.png`），供界面验证留证。

**日志**：写 `<runtime>/logs/tower-ops.log`（按天轮转 14 天）并镜像进事件库（诊断页可查）。
页面切换**不记日志**（导航不是需要追溯的操作）；失败/异常会带上底层原因、流水线/结果/帧/相机
与执行期 traceback（DEC-052）。

> 运行数据默认写到 `tower-ops/runtime/`（SQLite 记录库 + 帧文件）。若工程所在盘是**网络映射盘**，
> SQLite 可能因锁机制报 `disk I/O error`，此时用 `--db`/`--runtime` 指到本地磁盘路径即可。

**安全边界**：`main.py gui` **只使用 Fake 硬件 + IPC stub 圆宿主**，不打开真实串口/SDK/相机。
`main.py real` 是**唯一的真实设备入口**：启动即连接全部设备（ds86/s100/eco65），
单台失败不挡界面；**DEC-085 起没有环境变量闸，real 默认允许运动**。
**未确认示教点一律拒绝运动**
（填 `configs/site/joint_targets.yaml`；不填就没有关节目标，运动类配方不放行）。
缺厂家 SDK 时**明确报错而非回退 Fake**；圆宿主会自动发现
主程序同目录的 `circle38_host.exe` / `circle38_host\circle38_host.exe` 并**启动即拉起**
（DEC-053），都找不到时视觉判定**拒绝而非给假结果**。
原生 `circles_for_show` 的空占位（`[]`/`[[]]`/`[[[]]]`）按"没有圆"处理——递归展平后回退
`circle_centers`/`radius`，两者都空才判 `NO_TARGET`，不再误报"原生返回畸形"（DEC-054）。
真实设备运行仍需 A20/D1-SCOPE 的范围与保护审查（见 `doc/实施记录/基线与授权.md` §2）。

**厂家包不复制**：机器人 `Robotic_Arm` 与相机 `scepter_sdk` 保留在旧工程原处，
`main.py real` 启动时由 `bootstrap/vendor_paths.py` 挂到 `sys.path` 并打印可导入状态
（`--robot-sdk-dir` / `--ds86-sdk-dir` 可覆盖）。**必须在 `tower-ops` 目录下运行**
（圆宿主子进程继承 cwd）。

**圆宿主自动启动（DEC-053）**：主程序与 3.8 宿主之间是**子进程 stdin/stdout 管道**，
不是端口服务——手工双击 `circle38_host.exe` 主程序也不会用它。`real` 模式启动时按
「显式 `--circle-host-cmd` > 主程序同目录 `circle38_host.exe` >
`circle38_host\circle38_host.exe` > 源码 `dist_host\circle38_host\circle38_host.exe`」
解析并**立刻拉起**（控制台打印「[real] 圆宿主: 自动发现（主程序同目录）: …」）。
推荐把 PyInstaller 产物 `dist_host\circle38_host\` **整个目录**拷到 `tower-ops.exe` 旁。
拉起失败不挡界面：只记启动通知，找圆时如实报「圆宿主启动失败: …」，不给假结果。

**DS86 已接真实采集**（`adapters/ds86/scepter.py`，移植旧 `ds86_camera.py` 的采集流）：
扫设备 → 按序列号打开 → 应用启动参数（分辨率/帧率/对齐/曝光）→ 启动流 → 读内参 → 取深度/彩色帧。

**唯一例外**是设备配置页的"测试连接"：它按用户点击对已配置的 IP:端口/串口做**连通性探测**
（TCP 握手 / 打开串口 / ZMQ 只读 ping），**不发任何动作指令帧**；未确认地址直接拒绝连接（见 DEC-003）。

**缺依赖时降级（不会死路）**：ZMQ 探测需要 `pyzmq`。未安装时不会只说"无法探测"，而是**降级为 TCP 可达性检查**，
如实报"TCP 端口可达，但未安装 pyzmq，未能验证 ZMQ 应答（安装: `python -m pip install pyzmq`）"，
并以警示色显示——缺依赖是**环境问题不是设备问题**。

**S100 D405 采集（E09）**：`adapters/s100/` 提供唯一 REQ owner 会话（有界队列 / 期限 / 超时重建换 epoch /
`request_id` 去重 / epoch 隔离）、V2 多段解码（严格校验段数/长度/shape/dtype，**明确拒绝 V1/pickle**）、
`S100CameraService` 相机契约与 `services/camera.py` 注册表；离线装配注册**进程内 Fake V2 服务**，
`d405_01/02` 可连接并出图。**真实 ZMQ 传输默认不注册** —— 真实设备连接需 D1 授权
（见 `doc/实施记录/基线与授权.md`）；**V2 编码为 candidate，与真实 S100 的字节级对齐待 E05 ICD 冻结 + E06/E10/E11**。
界面抓拍来源会显示"离线合成（进程内 Fake S100 V2 服务）+ V2 编码状态"，不会显示成真实采集。

**S100 边缘服务（E05/E07/E08）**：协议单一定义在 `application/contracts/camera_v2.py`（服务端与客户端共用）；
`edge_service/` 提供按序列号绑定的通道注册与 Actor、有界帧缓存、采集任务管理（submit 立即受理、
去重/CONFLICT/BUSY、取消/超期/释放、结果 TTL 与容量有界）、V2 服务循环（REP 单所有者、
multipart、访问策略**默认拒绝**）。ICD 与访问边界见 `doc/接口/`、`doc/部署/`。

**运动方案页**（移植自旧工程 `RealMan_Robot/ECO65_NutTighten`，真机实测逻辑）：
页内两个子 tab —— **路径规划**（方案新建/重命名/删除、路点表每行可选 `movel`/`movej` 混用、
读取当前位姿/关节、增删/上下移/应用；`movel` 行按 mm+rad 填、内部换算为 m）与
**路径实施**（运行方案单选、运行/暂停/继续/停止、**引导定位**、**套筒前进/后退**、日志）。
数据在 `configs/motion/motion.yaml`（7 个实测方案 / 6 条路径 / M10·M12·M14 套筒 / Bolt+Ganta 坐标系）。
**引导定位已内嵌**：DS86 抓图 → 深度引导算法 → **cp38 宿主找圆** → 相机系 XYZ → 粗/精偏差 →
人工确认后在 Bolt 工具系按 `x/1000, z/1000` 偏移（粗→精两段）；参数在 `configs/motion/guidance.yaml`。
页面上有**单次视觉检测**（出结果图 + XYZ + 偏差，**不动机械臂、不受运动闸限制**）。
**运动在机械臂连接后可用**（DEC-085 无环境变量闸）；单点到位超时**停下交人处理，绝不自动下发下一点**。

**真实 S100 接入**：`application/adapters/s100/real.py`。构建不自动连接，占位地址一律拒绝；
`python main.py real` 按设备配置建立真实会话。

## 快速开始（离线测试）

```bash
# 主环境（ENV-MAIN, Python 3.12, 待 A02 创建）就绪后：
python -m pytest tests/offline
# 本轮在开发容器 Python 3.10 下运行，结果等级记录为 offline_fake
```

## 目录

```text
application/
  bootstrap/     组合入口、离线栈装配、生命周期与退出顺序
  contracts/     DTO、稳定ID、状态枚举、单位、Port（无 Qt/SDK 副作用）
  domain/        动作定义、资源租约/互锁、遥测时效、几何与标定
  services/      task、actions、stop、recipes、records、devices、device_config、device_probe
  workflow/      py_trees 编译、节点工厂、执行账本、任务状态机
  adapters/      stm32（Codec/会话/板）、realman（机器人）、ds86（相机）、s100（S100 相机服务）
  hosts/circle38/ cp38 圆检测独立宿主 + 主侧 IPC 代理
  algorithms/    circle（圆检测规范化/判定/流水线）
  infrastructure/ 事件、SQLite 持久化
  presentation/  方向1最小作业台（ViewModel + Qt 页面）
edge_service/    S100 边缘服务（E 阶段）：camera_registry / camera_actor / frame_store /
                 capture_jobs / result_store / zmq_server / health / security
configs/         设备/固件/动作/互锁/标定/流水线/配方；边缘侧 configs/edge/cameras.yaml
tests/offline/   离线契约/场景测试（默认收集）
tests/hardware/  获准现场/D1 入口（默认跳过）
```

## 关键约定（摘自设计）

- **单位**：领域层 mm / deg / ms；SDK 边界显式转换；深度保留 uint16 + depth_scale_m。
- **分型**：`JointTarget`（恰 6 关节 deg）与 `CartesianPose` 严格分开。
- **旋转约定**：R = Rz·Ry·Rx（`rpy_zyx_deg`）。
- **命令**：写成功只标 SENT；有效 ACK 才 ACCEPTED；有效 DONE + 终态核验才 COMPLETED。
- **互锁**：默认全部运动互斥（motion_global）；抓拍占静止保护；手动来源不绕过硬条件。
- **未知**：UNKNOWN 资源进入 quarantine，人工核对前不重新分配；非幂等动作不自动重发。
- **一致性**：主程序**不导入** cp38 原生件（ABI 边界），经受控 IPC 调用 3.8 宿主。

## 相关文档

- 设计：[../doc/设计方案/12_AI分步执行总纲.md](../doc/设计方案/12_AI分步执行总纲.md)
- 实绩台账与证据：[../doc/实施记录/README.md](../doc/实施记录/README.md)

"""tower-ops 应用入口。

用法（在 tower-ops 目录下执行）：

    python main.py gui            # 图形界面：离线（Fake 硬件，需 PySide6）
    python main.py real           # **真实设备模式**：连接全部设备并允许运动
    python main.py real --role t  # 真实设备模式：技术员（只显示操作台/日志诊断）
    python main.py real --role g  # 真实设备模式：程序员（全部页面，默认）
    python main.py --help

真实设备模式分两种使用者（DEC-098）：
- `--role g` / `--role programmer`（默认）：显示全部页面；
- `--role t` / `--role technician`：只显示「操作台」「日志诊断」。
离线 ``gui`` 固定显示全部页面，忽略角色。

打包后（PyInstaller，见 `packaging/`）直接运行 `tower-ops.exe [gui|real]`；
`configs/` 与 `runtime/` 落在 **exe 同级目录**（可写、可备份），包内那份只作首次启动的种子，
不会覆盖现场改过的配置（`application/bootstrap/app_paths.py`）。

安全边界：`gui` **只使用 Fake 硬件 + IPC stub 圆宿主**；`real` 是唯一的真实设备入口，
启动即连接全部设备（单台失败不挡界面）并允许运动（DEC-085，无环境变量闸）。
**未确认的示教点仍一律拒绝运动**。真实设备运行仍需 A20/D1-SCOPE 的范围与保护审查。
"""
from __future__ import annotations

import argparse
import os
import sys

# 让 `import application...` 可用（脚本位于工程根目录）
_HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False) and _HERE not in sys.path:
    sys.path.insert(0, _HERE)      # 冻结后由 bootloader 负责模块搜索路径

from application.bootstrap import app_paths  # noqa: E402
from application.bootstrap.circle_host import resolve_circle_host_cmd  # noqa: E402
from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.bootstrap.lifecycle import LifecycleManager, ProfileError  # noqa: E402
from application.presentation.theme import THEME  # noqa: E402

# 相对 <应用根>/configs：源码运行 = tower-ops/configs；打包后 = exe 旁的 configs（见 app_paths）
DEFAULT_RECIPE = os.path.join("recipes", "ds86_two_station_example.yaml")
def _force_utf8_stdout() -> None:
    """Windows 控制台下尽量以 UTF-8 输出中文，避免编码报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


NEEDED_SDK = ("scepter_sdk", "Robotic_Arm")


def _build_stack(args) -> "object":
    return build_offline_stack(
        db_path=args.db,
        runtime_dir=args.runtime,
        recipe_path=args.recipe,
        # 设备与链路配置落在 <应用根>/configs/devices/ (E21); 由界面增删改维护。
        # 源码运行=仓库 configs/；打包后=exe 旁的 configs/（见 app_paths / DEC-016）。
        device_config_path=os.path.join(app_paths.configs_dir(), "devices", "devices.yaml"),
        # 运动方案配置落在 <应用根>/configs/motion/（「运动方案」页维护）。
        motion_config_path=os.path.join(app_paths.configs_dir(), "motion", "motion.yaml"),
        # DEC-059：引导定位配置与 real 模式**同一份**（configs/motion/guidance.yaml）。
        # 此前离线漏传，退回 <runtime>/guidance.yaml（旧默认值：tcp_x=0、找圆/DS86 段为空），
        # 导致离线界面的粗定位与精定位数值完全相同、找圆调试参数也与现场不一致。
        guidance_config_path=os.path.join(app_paths.configs_dir(), "motion",
                                          "guidance.yaml"),
        # 操作台计算配置落在 <应用根>/configs/operations/（DEC-148）。
        operation_config_path=os.path.join(app_paths.configs_dir(), "operations",
                                           "manual_ops.yaml"),
        # 命名视觉检测落在 <应用根>/configs/vision/（「视觉调试」页「另存为检测」维护）。
        vision_detections_path=os.path.join(app_paths.configs_dir(), "vision",
                                            "detections.yaml"),
    )


def _startup_recipe_id(stack, recipe_path: str | None) -> str | None:
    """启动时载入的那份执行方案（按来源文件反查）——作业台默认选中它（DEC-041）。"""
    svc = getattr(stack, "recipes", None)
    if svc is None or not recipe_path:
        return None
    try:
        target = os.path.abspath(recipe_path)
        for rid in svc.drafts():
            src = getattr(svc, "source_of", lambda _r: None)(rid)
            if src and os.path.abspath(src) == target:
                return rid
    except Exception:  # noqa: BLE001 - 反查失败就退回目录里的第一条
        return None
    return None


def _open_gui(stack, *, profile: str, allow_offline_draft: bool,
              default_recipe: str | None = None,
              role: str = "programmer") -> int:
    """打开六页主窗口（作业台默认页）。需 PySide6。"""
    # DEC-044：启动执行方案的问题（旧版已换新/载入失败已跳过）如实打在控制台，
    # 不挡界面 —— 是否处理（重存/换 --recipe）由人决定。
    for msg in (getattr(stack, "startup_notices", None) or []):
        print(f"[{profile}] {msg}")
    try:
        from application.presentation.app import GUI_REQUIRED, MainWindow, UiContext
    except Exception as exc:  # noqa: BLE001
        print(f"[gui] 导入界面失败: {exc}", file=sys.stderr)
        return 2
    if not GUI_REQUIRED:
        print("[gui] 未安装 PySide6, 无法启动图形界面。", file=sys.stderr)
        print("      安装示例:  <ENV-MAIN python> -m pip install PySide6", file=sys.stderr)
        return 2

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from application.presentation.theme import THEME
    from application.presentation.viewmodel import WorkbenchViewModel

    lifecycle = LifecycleManager(stack=stack, profile=profile)
    lifecycle.start()

    app = QApplication(sys.argv[:1])
    font = QFont()
    font.setFamilies(list(THEME.font_stack))
    font.setPointSize(10)
    app.setFont(font)

    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            disabled_devices=[],
                            allow_offline_draft=allow_offline_draft,
                            default_recipe=default_recipe)
    window = MainWindow(UiContext(stack=stack, vm=vm, role=role))
    # DEC-106：启动即最大化铺满当前屏幕（保留标题栏/窗口按钮，便于最小化/关闭）。
    window.showMaximized()
    try:
        return app.exec()
    finally:
        window.shutdown()          # 先停定时器, 再关服务/设备
        lifecycle.shutdown()


def run_gui(args) -> int:
    """离线图形界面（Fake 硬件）。"""
    stack = _build_stack(args)
    return _open_gui(stack, profile="offline", allow_offline_draft=True,
                     default_recipe=_startup_recipe_id(
                         stack, getattr(args, "recipe", None)))


def run_real(args) -> int:
    """**真实设备模式**：连接全部设备并开界面（DEC-085：无环境变量闸、总是允许运动）。"""
    from application.bootstrap.production import (
        ProductionError,
        ProductionRequest,
        build_production_stack,
        initial_probe,
        load_confirmed_targets,
    )

    targets_path = args.targets or os.path.join(app_paths.configs_dir(), "site",
                                                "joint_targets.yaml")

    # 厂家 SDK 路径：**原包不复制不修改**，只把旧工程里的包目录加进 sys.path
    from application.bootstrap.vendor_paths import (
        DEFAULT_DS86_SDK_PARENT,
        DEFAULT_ROBOT_SDK_DIR,
        add_vendor_paths,
        importable,
    )
    added = add_vendor_paths(robot_sdk_dir=args.robot_sdk_dir or None,
                             ds86_sdk_dir=args.ds86_sdk_dir or None)
    for path in added:
        print(f"[real] 厂家包路径已加入: {path}")
    if not added:
        print(f"[real] 厂家包路径未加入（可显式指定）:")
        print(f"[real]   --robot-sdk-dir {DEFAULT_ROBOT_SDK_DIR}   (含 Robotic_Arm)")
        print(f"[real]   --ds86-sdk-dir  {DEFAULT_DS86_SDK_PARENT}   (含 scepter_sdk)")
    for name in NEEDED_SDK:
        print(f"[real] 厂家包 {name}: {'可导入' if importable(name) else '**不可导入**'}")
    # DEC-053/072/073：圆检测后端。opencv=进程内自研后端（Linux 单程序，不起宿主）；
    # auto=有宿主命令用宿主、否则进程内 OpenCV；host=只认宿主。
    if args.circle_backend == "opencv":
        circle_cmd, circle_note = None, "按 --circle-backend opencv：进程内 OpenCV 后端（不启动宿主）"
    elif args.circle_backend == "host":
        circle_cmd, circle_note = resolve_circle_host_cmd(args.circle_host_cmd)
    else:
        # auto：显式 --circle-host-cmd 优先；没给就在**主程序同目录**找
        # circle38_host[.exe] / circle38_host/circle38_host[.exe]；都没有则进程内 OpenCV。
        circle_cmd, circle_note = resolve_circle_host_cmd(args.circle_host_cmd)
        if circle_cmd is None:
            circle_note = ("未找到独立圆宿主；将使用进程内 OpenCV 后端（opencv-linux）")
    print(f"[real] 圆检测后端: {args.circle_backend} · {circle_note}")
    try:
        req = ProductionRequest(
            recipe_path=args.recipe,
            db_path=args.db,
            runtime_dir=args.runtime,
            device_config_path=os.path.join(app_paths.configs_dir(), "devices",
                                            "devices.yaml"),
            circle_host_cmd=circle_cmd,
            circle_backend=args.circle_backend,
            confirmed_targets=load_confirmed_targets(targets_path),
            targets_path=targets_path,
            s100_protocol=args.s100_protocol,
            motion_config_path=os.path.join(app_paths.configs_dir(), "motion",
                                            "motion.yaml"),
            guidance_config_path=os.path.join(app_paths.configs_dir(), "motion",
                                              "guidance.yaml"),
            operation_config_path=os.path.join(app_paths.configs_dir(), "operations",
                                               "manual_ops.yaml"),
            vision_detections_path=os.path.join(app_paths.configs_dir(), "vision",
                                                "detections.yaml"),
        )
    except ProductionError as exc:
        print(f"[real] 参数有误: {exc}", file=sys.stderr)
        return 2

    print("[real] 真实设备模式（profile=production；连接全部设备，允许运动）")
    role_cn = {"t": "技术员", "technician": "技术员",
               "g": "程序员", "programmer": "程序员"}.get(args.role, args.role)
    print(f"[real] 使用者角色: {role_cn}（--role {args.role}）"
          + ("·只显示操作台/日志诊断" if role_cn == "技术员" else "·显示全部页面"))
    print(f"[real] 已确认示教点: {len(req.confirmed_targets)} 个"
          f"{'（关节目标为空 -> 运动类执行方案不会被放行）' if not req.confirmed_targets else ''}")
    print(f"[real] S100 协议: {req.s100_protocol}"
          + ("（现场旧协议 pickle/v1）" if req.s100_protocol == "legacy"
             else "（E05 V2 多段编码，仍 draft；现场服务若还是旧协议会报'缺少 header 段'）"))
    try:
        stack = build_production_stack(req)
    except ProductionError as exc:
        print(f"[real] 装配失败: {exc}", file=sys.stderr)
        print("[real] 这是**整栈**无法建立（授权/存储/配置），不是单台设备连不上。"
              "单台连不上不会走到这里：会照常进界面，在设备页标红。", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 适配器/SDK 异常也要给人话, 不给 traceback
        print(f"[real] 装配失败（{type(exc).__name__}）: {exc}", file=sys.stderr)
        print("[real] 提示：能走到这里的是**整栈**级故障（记录库/存储/装配期异常）。"
              "单台设备不可用是**不致命**的——只记录、照常进界面并在设备页标红，"
              "要逐台看原因请进界面后看设备页。", file=sys.stderr)
        return 2
    # DEC-087：首次进入系统时把已配置设备探测一遍，初始化“未启用/已启用”。
    probed = initial_probe(stack.device_config, stack.device_state)
    enabled_n = sum(1 for _k, status, _d in probed if status == "已启用")
    print(f"[real] 设备状态初始化: 探测 {len(probed)} 项 · "
          f"已启用 {enabled_n} · 未启用 {len(probed) - enabled_n}")
    for key, status, detail in probed:
        if status != "已启用":
            print(f"[real]   - {key}: {status}（{detail}）")
    print(f"[real] 已装配真实相机: {sorted(stack.camera_registry.camera_ids()) or '(无)'}")
    print(f"[real] 机器人: {type(stack.robot).__name__}"
          f"（运动{'允许' if getattr(stack.robot, 'motion_allowed', False) else '被闸拦住'}）")
    session_channels = sorted(stack.camera_registry.camera_ids())
    if session_channels:
        print(f"[real] 本次能建立会话的通道: {', '.join(session_channels)}（界面「连接」可用）")
    else:
        print("[real] 本次没有任何通道能建立会话；界面「连接」为灰，"
              "各通道原因见上面的不可用清单与设备页。")
    issues = list(getattr(stack, "startup_issues", None) or [])
    if issues:
        print(f"[real] 注意: {len(issues)} 台设备装配不可用"
              f"（**不致命**：界面照常启动，设备页对应行会标红、悬停看原因）:")
        for issue in issues:
            keys = f"  影响: {','.join(issue.keys)}" if issue.keys else ""
            print(f"[real]   - {issue.device}: {issue.reason}{keys}")
    return _open_gui(stack, profile="production", allow_offline_draft=False,
                     default_recipe=_startup_recipe_id(stack, args.recipe),
                     role=args.role)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()

    parser = argparse.ArgumentParser(
        prog="main.py", description="杆塔智能作业系统入口")
    parser.add_argument("mode", choices=("gui", "real"),
                        help="gui=离线界面(Fake 硬件); real=真实设备模式")
    parser.add_argument("--role", default="g",
                        choices=("t", "g", "technician", "programmer"),
                        help="real 模式角色（DEC-098）：t=技术员（操作台/日志诊断）；"
                             "g=程序员（全部页面，默认）；也接受完整名；gui 忽略")
    parser.add_argument("--s100-protocol", default="legacy", choices=("legacy", "v2"),
                        help="real 模式 S100 协议: legacy=现场旧协议 pickle/v1（默认，"
                             "与已部署的 s100_d405_service 对齐）；v2=E05 多段编码(仍 draft)")
    parser.add_argument("--circle-host-cmd", default="",
                        help="real 模式真实圆宿主启动命令；不给则自动查找主程序同目录的 "
                             "circle38_host[.exe] / circle38_host/circle38_host[.exe]"
                             "（找到即启动；auto 模式下都没有则改用进程内 OpenCV）")
    parser.add_argument("--circle-backend", default="auto",
                        choices=("auto", "host", "opencv"),
                        help="圆检测后端：auto=有宿主用宿主、否则进程内 OpenCV；"
                             "host=只认宿主（没有则不可用）；"
                             "opencv=进程内 OpenCV 自研后端（Linux 单程序，不起宿主）")
    parser.add_argument("--targets", default="",
                        help="已确认示教点文件（默认 configs/site/joint_targets.yaml）")
    parser.add_argument("--robot-sdk-dir", default="",
                        help="含 Robotic_Arm 的目录（默认旧工程 ECO65_NutTighten）")
    parser.add_argument("--ds86-sdk-dir", default="",
                        help="含 scepter_sdk 的目录（默认旧工程 vision/）")
    parser.add_argument("--recipe", default=os.path.join(app_paths.configs_dir(),
                                                        DEFAULT_RECIPE),
                        help="执行方案 YAML 路径")
    parser.add_argument("--db", default=os.path.join(app_paths.runtime_dir(), "records.db"),
                        help="SQLite 记录库路径")
    parser.add_argument("--runtime", default=app_paths.runtime_dir(),
                        help="运行数据目录 (帧/日志)")
    args = parser.parse_args(argv)

    # 全局日志（DEC-029）：**入口即装**，把启动/配置/各模式运行都记进文本日志文件 +
    # 事件表。控制台仍由下面的 print 负责（故默认 to_console=False，不重复刷屏）。
    # DEC-065：日志级别/保留天数/控制台开关来自「参数配置」页（configs/runtime/settings.yaml）。
    from application.infrastructure import logging_setup
    from application.services.runtime_config import (
        RuntimeSettings, RuntimeSettingsStore, level_value)
    try:
        runtime_settings = RuntimeSettingsStore(os.path.join(
            app_paths.configs_dir(), "runtime", "settings.yaml")).load()
    except Exception:  # noqa: BLE001 - 坏设置文件不挡启动, 用默认
        runtime_settings = RuntimeSettings()
    facility = logging_setup.install(
        args.runtime,
        to_console=bool(runtime_settings.log_to_console),
        file_level=level_value(runtime_settings.log_level),
        backup_days=int(runtime_settings.log_backup_days),
    )
    applog = logging_setup.get_logger("main")
    applog.info("====== 启动 mode=%s argv=%s ======", args.mode,
                " ".join(argv) if argv else " ".join(sys.argv[1:]))
    applog.info("日志文件: %s", facility.log_path)

    # 冻结运行时：把包内 configs/ 里**缺失**的文件释放到 exe 旁的 configs/
    # （只补缺失、不覆盖 —— 现场改过的 devices.yaml / motion.yaml 不会被盖掉）
    seeded = app_paths.seed_user_files()
    print(f"[app] {app_paths.describe()}")
    applog.info("布局: %s", app_paths.describe())
    if seeded:
        print(f"[app] 首次启动：已释放 {len(seeded)} 个种子配置到 {app_paths.configs_dir()}")
        applog.info("首次启动：释放 %d 个种子配置到 %s", len(seeded),
                    app_paths.configs_dir())
        for rel in seeded:
            applog.debug("  释放种子: %s", rel)

    if args.mode == "real":
        return run_real(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())

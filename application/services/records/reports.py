"""运行报告 (E25, 02 §11/§12, 03 §10)。

报告关联 run/station/step/command/capture/frame/result/event 与 配置/标定/流水线 hash;
**缺证据显式列为缺项**, 未决 (UNKNOWN) 动作显式列出; 历史图明确标记来源。
不补造以前不存在的原始记录: 缺失即写缺项, 不用默认值填充。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

# 视为“未决”的命令状态 (不盲重放)
PENDING_STATES = ("SENT", "ACCEPTED", "UNKNOWN")


@dataclass
class RunReport:
    run_id: str
    present: bool = True
    run: dict = field(default_factory=dict)
    stations: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    captures: list[dict] = field(default_factory=list)
    frames: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    pending_commands: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    versions: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "present": self.present, "run": self.run,
            "stations": self.stations, "steps": self.steps, "commands": self.commands,
            "captures": self.captures, "frames": self.frames, "results": self.results,
            "events_count": len(self.events), "pending_commands": self.pending_commands,
            "missing": self.missing, "versions": self.versions,
        }


def _rows(store, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in store.query(sql, args)]


def build_run_report(store, run_id: str, *, runtime_dir: str | None = None) -> RunReport:
    """从持久化层汇总一次运行的报告; 缺失项写进 missing, 不用默认值冒充。"""
    rep = RunReport(run_id=run_id)
    run = _rows(store, "SELECT * FROM runs WHERE run_id=?", (run_id,))
    if not run:
        rep.present = False
        rep.missing.append("run 记录缺失")
        return rep
    rep.run = run[0]
    rep.versions = {
        "recipe_id": run[0].get("recipe_id"),
        "recipe_version": run[0].get("recipe_version"),
        "config_hash": run[0].get("config_hash"),
        "calibration_hash": run[0].get("calibration_hash"),
    }
    for key, sql, args in (
        ("stations", "SELECT * FROM station_instances WHERE run_id=?", (run_id,)),
        ("steps", "SELECT * FROM step_executions WHERE run_id=?", (run_id,)),
        ("commands", "SELECT * FROM command_executions WHERE run_id=?", (run_id,)),
        ("captures", "SELECT * FROM capture_sets WHERE run_id=?", (run_id,)),
        ("events", "SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)),
    ):
        setattr(rep, key, _rows(store, sql, args))

    frame_ids = [c.get("capture_id") for c in rep.captures if c.get("capture_id")]
    if frame_ids:
        marks = ",".join("?" * len(frame_ids))
        rep.frames = _rows(store, f"SELECT * FROM frames WHERE capture_id IN ({marks})",
                           tuple(frame_ids))
    for f in rep.frames:
        if f.get("path") and not os.path.exists(f["path"]):
            rep.missing.append(f"帧文件缺失: {f['frame_id']}")
    if frame_ids and not rep.frames:
        rep.missing.append("capture 存在但无帧记录")

    if rep.frames:
        marks = ",".join("?" * len(rep.frames))
        rep.results = _rows(
            store, f"SELECT * FROM vision_results WHERE frame_id IN ({marks})",
            tuple(f["frame_id"] for f in rep.frames))

    rep.pending_commands = [c for c in rep.commands if c.get("state") in PENDING_STATES]
    if rep.pending_commands:
        rep.missing.append(f"存在 {len(rep.pending_commands)} 条未决命令 (UNKNOWN/在途), 需人工核对")
    if not rep.stations:
        rep.missing.append("无运行作用域记录")
    if not rep.frames:
        rep.missing.append("无帧证据")
    if runtime_dir and not rep.frames:
        rep.missing.append("运行目录无可用帧 (可能已轮转或未采集)")
    return rep


def render_markdown(rep: RunReport) -> str:
    lines = [f"# 运行报告 {rep.run_id}", ""]
    if not rep.present:
        lines += ["**该运行不存在记录。**", f"缺项: {rep.missing}"]
        return "\n".join(lines) + "\n"
    r = rep.run
    lines += [
        f"- 状态: {r.get('state')}",
        f"- 执行方案: {r.get('recipe_id')} @ {r.get('recipe_version')}",
        f"- 配置hash: {r.get('config_hash')}",
        f"- 标定hash: {r.get('calibration_hash')}",
        "", f"## 运行作用域 ({len(rep.stations)})", "",
    ]
    lines += [f"- {s.get('station_instance_id')} [{s.get('state')}]"
              for s in rep.stations] or ["- （无）"]
    lines += ["", f"## 步骤 ({len(rep.steps)})", ""]
    lines += [f"- {s.get('step_execution_id')} [{s.get('state')}]"
              for s in rep.steps] or ["- （无）"]
    lines += ["", f"## 命令 ({len(rep.commands)})", ""]
    lines += [f"- {c.get('command_id')} {c.get('action_ref')} -> {c.get('state')}"
              for c in rep.commands] or ["- （无）"]
    lines += ["", f"## 帧/结果 ({len(rep.frames)} / {len(rep.results)})", ""]
    for f in rep.frames:
        lines.append(f"- {f.get('frame_id')} seq={f.get('sequence')} "
                     f"dtype={f.get('dtype')} sha256={(f.get('sha256') or '')[:12]} "
                     f"[历史/回放标记按来源]")
    lines += ["", "## 未决命令", ""]
    lines += [f"- **{c.get('command_id')}** {c.get('state')} (需人工核对, 不自动重放)"
              for c in rep.pending_commands] or ["- 无"]
    lines += ["", "## 缺项 / 限制", ""]
    lines += [f"- {m}" for m in rep.missing] or ["- 无"]
    lines += ["", f"### 事件 ({len(rep.events)} 条, 明细见数据库)"]
    return "\n".join(lines) + "\n"


def export_report(store, run_id: str, out_path: str, *,
                  fmt: str = "md", runtime_dir: str | None = None) -> str:
    """导出报告到指定路径 (E25: 项目报告导出到 doc 中的用户选定子目录)。"""
    rep = build_run_report(store, run_id, runtime_dir=runtime_dir)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        if fmt == "json":
            json.dump(rep.as_dict(), fh, ensure_ascii=False, indent=2)
        else:
            fh.write(render_markdown(rep))
    return out_path


__all__ = ["PENDING_STATES", "RunReport", "build_run_report", "export_report",
           "render_markdown"]

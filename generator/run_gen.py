#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_gen.py —— 跑步记录生成器 · 交互式命令行
==============================================

用法:
    # 交互模式 (推荐)
    python generator/run_gen.py

    # 一行命令
    python generator/run_gen.py --dist 3.0 --start "2026-09-15 07:30" \
        --lat 30.1234 --lon 104.1234 \
        --cp "一号点:30.1245:104.1255" --cp "二号点:30.1225:104.1215"

    # 配置文件
    python generator/run_gen.py --config myrun.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

# 保证能 import rungen
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rungen import (RunningGenerator, RunnerProfile, RouteMode,
                    generate_record, fmt_pace, fmt_duration, parse_pace,
                    SHAPE_ELLIPSE, SHAPE_TRACK)

C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_CYAN = "\033[36m"
C_RESET = "\033[0m"


def hr(ch="─", n=64):
    print(C_DIM + ch * n + C_RESET)


def title(t):
    print()
    print(C_BOLD + C_CYAN + "  " + t + C_RESET)
    hr()


def ask(prompt, default=None, cast=str):
    """带默认值的输入"""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {prompt}{suffix}: ").strip()
        if not raw:
            if default is not None:
                return default
            print(f"  {C_RED}✗ 该项必填{C_RESET}")
            continue
        try:
            return cast(raw)
        except Exception as e:
            print(f"  {C_RED}✗ 无法解析: {e}{C_RESET}")


def ask_yes_no(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"  {prompt} [{d}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1", "true")


def ask_time(prompt, default=None):
    """
    时间选择: 支持
      · 2026-09-15 07:30
      · 07:30            (今天)
      · now / 现在
      · +30m / -2h       (相对现在)
    """
    print(f"  {C_DIM}格式: 2026-09-15 07:30  或  07:30  或  now  或  +30m/-2h{C_RESET}")
    while True:
        raw = input(f"  {prompt}" + (f" [{default}]" if default else "") + ": ").strip()
        if not raw and default:
            return default
        if not raw:
            continue
        low = raw.lower()
        if low in ("now", "现在"):
            return datetime.now().replace(microsecond=0)
        if low.startswith(("+", "-")) and low[-1] in "mhd":
            try:
                n = int(low[:-1])
                unit = low[-1]
                delta = {"m": timedelta(minutes=n),
                         "h": timedelta(hours=n),
                         "d": timedelta(days=n)}[unit]
                return (datetime.now() + delta).replace(microsecond=0)
            except Exception:
                pass
        fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
                "%H:%M:%S", "%H:%M")
        for f in fmts:
            try:
                dt = datetime.strptime(raw, f)
                if "%Y" not in f:
                    today = datetime.now()
                    dt = dt.replace(year=today.year, month=today.month, day=today.day)
                return dt.replace(microsecond=0)
            except ValueError:
                continue
        print(f"  {C_RED}✗ 无法解析时间, 请重试{C_RESET}")


def ask_checkpoints():
    """录入打卡点"""
    print(f"  {C_DIM}每行: 名称,纬度,经度[,打卡半径米]   直接回车结束{C_RESET}")
    cps = []
    while True:
        raw = input(f"  打卡点 {len(cps)+1} (回车结束): ").strip()
        if not raw:
            break
        parts = [p.strip() for p in raw.replace("，", ",").split(",")]
        if len(parts) < 3:
            print(f"  {C_RED}✗ 至少需要 名称,纬度,经度{C_RESET}")
            continue
        try:
            name = parts[0] or f"打卡点{len(cps)+1}"
            la, lo = float(parts[1]), float(parts[2])
            r = float(parts[3]) if len(parts) > 3 else 30.0
            cps.append((name, la, lo, r))
            print(f"  {C_GREEN}✓ 已添加 {name} ({la}, {lo}) 半径 {r}m{C_RESET}")
        except ValueError as e:
            print(f"  {C_RED}✗ 坐标解析失败: {e}{C_RESET}")
    return cps


# ============================================================
# 交互主流程
# ============================================================
def interactive():
    print()
    print(C_BOLD + "=" * 64 + C_RESET)
    print(C_BOLD + "  跑步记录生成器  ·  交互模式" + C_RESET)
    print(C_BOLD + "=" * 64 + C_RESET)

    title("① 起点位置")
    lat = ask("起点纬度", 30.123400, float)
    lon = ask("起点经度", 104.123400, float)

    title("② 打卡点 (必经)")
    cps = ask_checkpoints()

    title("③ 距离与时间")
    dist = ask("目标距离 (公里)", 3.0, float)
    stime = ask_time("开始时间", datetime.now().replace(microsecond=0))

    title("④ 路线模式")
    print("    1) 环线 / 多圈    (起点=终点, 适合操场/校园)")
    print("    2) 折返 / 多趟    (原路来回)")
    print("    3) 点到点         (需要终点)")
    mc = ask("选择模式", "1", str)
    mode = {"1": RouteMode.LOOP, "2": RouteMode.OUT_AND_BACK,
            "3": RouteMode.POINT2POINT}.get(mc, RouteMode.LOOP)
    end = None
    if mode == RouteMode.POINT2POINT:
        ela = ask("终点纬度", lat + 0.005, float)
        elo = ask("终点经度", lon + 0.005, float)
        end = (ela, elo)

    title("⑤ 跑者参数")
    pace_s = ask("基础配速 (5'30\" 或 330 秒)", "5'30\"", str)
    pace = parse_pace(pace_s)
    cad = ask("基础步频 (步/分)", 172.0, float)
    height = ask("身高 (cm)", 172.0, float)
    weight = ask("体重 (kg)", 65.0, float)
    age = ask("年龄", 22, int)
    fit = ask("体能水平 0~1 (越高越稳)", 0.5, float)

    title("⑥ 高级选项")
    seed_s = ask("随机种子 (回车=按时间)", "", str)
    seed = int(seed_s) if seed_s else None
    interval = ask("采样间隔 (秒)", 5.0, float)
    noise = ask("GPS 噪声强度 (米)", 1.6, float)

    # ---------- 生成 ----------
    title("⑦ 生成中…")
    prof = RunnerProfile(base_pace_s_per_km=pace, base_cadence=cad,
                         height_cm=height, weight_kg=weight, age=age,
                         fitness_level=fit)
    gen = RunningGenerator(
        start=(lat, lon), distance_km=dist, start_time=stime,
        checkpoints=cps, profile=prof, mode=mode, end=end,
        sample_interval_s=interval, seed=seed, noise_sigma_m=noise,
    )
    rec = gen.generate()

    show_result(rec)

    # ---------- 导出 ----------
    title("⑧ 导出")
    if ask_yes_no("导出文件?", True):
        outdir = ask("输出目录", "output", str)
        ts = rec.start_time.strftime("%Y%m%d_%H%M%S")
        prefix = f"run_{ts}_{dist:g}km"
        paths = gen.export_all(rec, outdir, prefix)
        print()
        for k, v in paths.items():
            print(f"  {C_GREEN}✓{C_RESET} {k.upper():>5}  {os.path.abspath(v)}")
    return rec


def show_result(rec):
    """打印结果摘要"""
    title("★ 生成结果")
    s = rec.summary()
    for k, v in s.items():
        if k == "点位校验":
            color = C_GREEN if rec.all_points_valid else C_RED
            print(f"    {k:<10} {color}{v}{C_RESET}")
        else:
            print(f"    {k:<10} {v}")

    if rec.checkpoints:
        hr()
        print(C_BOLD + "  打卡点命中" + C_RESET)
        for c in rec.checkpoints:
            if c["hit"]:
                print(f"    {C_GREEN}✓{C_RESET} {c['name']:<12} "
                      f"距离 {c['hit_dist_m']:>6.1f} m (半径 {c['radius_m']:.0f}m) "
                      f"@ {c['hit_time']}")
            else:
                print(f"    {C_RED}✗{C_RESET} {c['name']:<12} "
                      f"未命中 (最近 {c['hit_dist_m']:.1f} m)")

    if rec.splits:
        hr()
        print(C_BOLD + "  每公里分段" + C_RESET)
        print(f"    {'KM':>4} {'距离':>8} {'用时':>8} {'配速':>8} "
              f"{'步频':>6} {'步幅':>7} {'爬升':>7} {'心率':>5}")
        for sp in rec.splits:
            d = sp.to_dict()
            tail = C_DIM + " (尾段)" + C_RESET if sp.partial else ""
            print(f"    {d['km']:>4} {d['distance_m']:>7.0f}m {d['time']:>8} "
                  f"{d['pace']:>8} {d['avg_cadence']:>6.0f} "
                  f"{d['avg_stride_cm']:>6.0f}cm {d['elev_gain_m']:>6.1f}m "
                  f"{d['avg_hr']:>5}{tail}")


# ============================================================
# 命令行模式
# ============================================================
def parse_cp_arg(s: str):
    parts = s.split(":")
    if len(parts) < 3:
        raise argparse.ArgumentTypeError(f"打卡点格式应为 名称:纬度:经度[:半径], 收到: {s}")
    name, la, lo = parts[0], float(parts[1]), float(parts[2])
    r = float(parts[3]) if len(parts) > 3 else 30.0
    return (name, la, lo, r)


def main():
    ap = argparse.ArgumentParser(
        description="跑步记录生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_gen.py                                             # 交互模式
  python run_gen.py --dist 3.0 --start "2026-09-15 07:30" \\
      --lat 30.1234 --lon 104.1234 \\
      --cp "一号点:30.1245:104.1255" --cp "二号点:30.1225:104.1215"
  python run_gen.py --config myrun.json
""")
    ap.add_argument("--dist", type=float, help="目标距离 (公里)")
    ap.add_argument("--start", type=str, help="开始时间")
    ap.add_argument("--lat", type=float, help="起点纬度")
    ap.add_argument("--lon", type=float, help="起点经度")
    ap.add_argument("--cp", action="append", type=parse_cp_arg, default=[],
                    help="打卡点 名称:纬度:经度[:半径] (可多次)")
    ap.add_argument("--mode", choices=["loop", "outback", "p2p"], default="loop")
    ap.add_argument("--end-lat", type=float)
    ap.add_argument("--end-lon", type=float)
    ap.add_argument("--pace", type=str, default="5'30\"", help="基础配速")
    ap.add_argument("--cadence", type=float, default=172.0)
    ap.add_argument("--height", type=float, default=172.0)
    ap.add_argument("--weight", type=float, default=65.0)
    ap.add_argument("--age", type=int, default=22)
    ap.add_argument("--fitness", type=float, default=0.5)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--interval", type=float, default=5.0, help="采样间隔 (秒)")
    ap.add_argument("--noise", type=float, default=1.6, help="GPS 噪声 (米)")
    # ★★ 轨迹形状（2026-09-29）：ellipse = 圆润闭合环（默认、最稳）；
    #   track = 标准田径场（两个半圆 + 两条直道），按打卡点外接矩形自适应直道长度。
    ap.add_argument("--shape", choices=[SHAPE_ELLIPSE, SHAPE_TRACK],
                    default=SHAPE_ELLIPSE, help="轨迹形状")
    ap.add_argument("--track-straight", type=float, default=0.0,
                    help="跑道形状的直道长度（米）；0 = 按打卡点自适应")
    ap.add_argument("--outdir", type=str, default="output")
    ap.add_argument("--config", type=str, help="JSON 配置文件")
    ap.add_argument("--quiet", action="store_true")

    a = ap.parse_args()

    # ★★ 跑道直道长度覆盖：`plan_route` 读的是 route 模块级常量，
    #   命令行给值时就地改掉（0 / 负值 = 保持"按打卡点自适应"）。
    if getattr(a, "track_straight", 0.0) and a.track_straight > 0:
        from rungen import route as _route_mod
        _route_mod.TRACK_STRAIGHT_M = float(a.track_straight)

    # ---------- 配置文件 ----------
    if a.config:
        with open(a.config, encoding="utf-8") as f:
            cfg = json.load(f)
        a.dist = cfg.get("distance_km", a.dist)
        a.start = cfg.get("start_time", a.start)
        a.lat = cfg.get("start_lat", a.lat)
        a.lon = cfg.get("start_lon", a.lon)
        a.mode = cfg.get("mode", a.mode)
        a.pace = cfg.get("pace", a.pace)
        a.cadence = cfg.get("cadence", a.cadence)
        a.height = cfg.get("height_cm", a.height)
        a.weight = cfg.get("weight_kg", a.weight)
        a.age = cfg.get("age", a.age)
        a.fitness = cfg.get("fitness", a.fitness)
        a.seed = cfg.get("seed", a.seed)
        a.interval = cfg.get("sample_interval_s", a.interval)
        a.noise = cfg.get("noise_sigma_m", a.noise)
        a.outdir = cfg.get("outdir", a.outdir)
        a.shape = cfg.get("shape", a.shape)
        for cp in cfg.get("checkpoints", []):
            if isinstance(cp, dict):
                a.cp.append((cp.get("name", "打卡点"), cp["lat"], cp["lon"],
                             cp.get("radius_m", 30.0)))
            else:
                a.cp.append(tuple(cp))

    # ---------- 无参数 -> 交互模式 ----------
    if not any([a.dist, a.start, a.lat, a.cp, a.config]):
        interactive()
        return

    # ---------- 必需项校验 ----------
    missing = [k for k in ("dist", "start", "lat", "lon") if getattr(a, k) is None]
    if missing:
        ap.error(f"缺少必需参数: {', '.join('--' + m for m in missing)}")

    mode = {"loop": RouteMode.LOOP, "outback": RouteMode.OUT_AND_BACK,
            "p2p": RouteMode.POINT2POINT}[a.mode]
    end = (a.end_lat, a.end_lon) if (a.end_lat and a.end_lon) else None

    prof = RunnerProfile(base_pace_s_per_km=parse_pace(a.pace),
                         base_cadence=a.cadence, height_cm=a.height,
                         weight_kg=a.weight, age=a.age, fitness_level=a.fitness)
    gen = RunningGenerator(
        start=(a.lat, a.lon), distance_km=a.dist, start_time=a.start,
        checkpoints=a.cp, profile=prof, mode=mode, end=end,
        sample_interval_s=a.interval, seed=a.seed, noise_sigma_m=a.noise,
        shape=a.shape,
    )
    rec = gen.generate()

    if not a.quiet:
        show_result(rec)

    ts = rec.start_time.strftime("%Y%m%d_%H%M%S")
    import uuid as _uuid
    uniq = _uuid.uuid4().hex[:6]          # 同名（相同秒+同距离）时保证唯一
    paths = gen.export_all(rec, a.outdir, f"run_{ts}_{a.dist:g}km_{uniq}")
    print()
    for k, v in paths.items():
        print(f"  ✓ {k.upper():>5}  {os.path.abspath(v)}")
    if not rec.all_points_valid:
        print(f"  {C_RED}★ 警告: 存在未通过 isValidPoint 的点位{C_RESET}")
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  已取消")
        sys.exit(130)

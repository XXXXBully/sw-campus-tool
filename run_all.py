#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py — 运动世界校园 · 一键跑步（自由跑 / 计分跑）

把「生成 → 审查 → 提交 → OBS 上传 → 回读校验」串成一条命令。

常用示例
────────
  # ★ 默认：最近 3 天，每天 1 条，时间自动落在有效时段内
  python run_all.py --mode free --dist 2.15

  # 只跑今天
  python run_all.py --mode free --dist 2.15 --today

  # 今天跑两条，显式选时（早 7:30 / 晚 19:00）
  python run_all.py --mode free --per-day 2 --today --times "07:30,19:00"

  # 最近 3 天，每天 2 条
  python run_all.py --mode free --per-day 2

  # 手动指定起跑时间（只提交 1 条）
  python run_all.py --mode free --start "2026-09-15 07:30:00"

  # 计分跑（必须经过服务端打卡点）
  python run_all.py --mode score --dist 2.25 --today

  # 只预览不提交
  python run_all.py --mode score --today --dry-run

时间规则（★ 硬约束）
──────────────────
· 有效时段 06:00 ~ 22:00（22 点后禁止跑步，起跑与结束都不能越界）
· 起跑必须是过去（stopTime 落在未来会触发风控）
· 一天最多 2 条；同一天两条之间至少间隔 跑量时长 + 60 分钟
· 默认只生成最近 3 天（时间太远的记录容易被查）
· 每天固定用「早 7 点 + 晚 19 点」两个自然时段，符合真人作息

设计要点
────────
· 每次运行都重新拉 runModePolicy（policy_ts 必须新鲜，否则 runes 头过期）
· 计分跑自动拉打卡点并做可达性校验；不可达时阻止提交
· 提交后自动做 OBS 回读校验，确认轨迹真的落了对象存储
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ★ 行缓冲：父进程 print 与子进程 stdout（直接写 fd1）交错时，
#   块缓冲会把 print 挤到子进程输出之后，顺序错乱。强制行缓冲修正。
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

PY = sys.executable
CLI = os.path.join(HERE, "swcli.py")

import campus as _campus
DEFAULT_CAMPUS = (_campus.DEFAULT_CAMPUS["lat"], _campus.DEFAULT_CAMPUS["lon"])

# ★ 配速解析必须与生成器共用同一个实现（rungen.core.parse_pace）。
#   曾经这里自带一套弱解析：纯数字按「分钟」算、"5'37\"" 解析失败后静默回退 340 ——
#   于是「估算用时」与「实际生成轨迹」会用两个不同的配速，用户填 337 会得到 11 小时。
#   用 append 而非 insert(0)，避免 generator/ 下的模块名遮蔽工程根模块。
_GEN_DIR = os.path.join(HERE, "generator")
if os.path.isdir(_GEN_DIR) and _GEN_DIR not in sys.path:
    sys.path.append(_GEN_DIR)
try:
    from rungen.core import parse_pace as _parse_pace
except Exception:                      # 生成器缺失时的兜底：保持同一语义
    def _parse_pace(text: str) -> float:
        """配速字符串 -> 秒/公里. 支持 5'37" / 5:37 / 337(秒)"""
        t = str(text).strip().replace('"', '').replace("'", ':')
        if ':' in t:
            mm, ss = t.split(':')[:2]
            return float(mm) * 60 + float(ss)
        return float(t)

# ── 校规硬约束（来自 runwatch 的 campusConfigModel）─────────────────
VALID_START_H = 6            # 有效时段 06:00 起
VALID_END_H = 22             # ★ 22:00 后禁止跑步（stopTime 必须 <= 22:00:59）
DEFAULT_BACKFILL = 3         # ★ 默认只往前补 3 天（时间太远的记录容易被查）
MAX_PER_DAY = 2              # ★ 一天最多 2 条
MIN_GAP_MIN = 60             # 同一天两条之间的最小间隔（分钟）


def _run_cli(args: list, verbose: bool = True) -> int:
    cmd = [PY, CLI] + args
    r = subprocess.run(cmd, capture_output=False, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode


def audit_schedule(runs: list, duration_s: int, verbose: bool = True) -> bool:
    """★ 提交前自检：所有规划的起跑/结束时间必须落在有效时段内，且是过去。

    这是最后一道防线 —— 万一上面的时间逻辑有 bug，这里会拦住。
    """
    now = time.time()
    ok = True
    if verbose:
        print("--- 时间合规自检 ---")
    # 按天分组检查条数
    by_day = {}
    for s in runs:
        by_day.setdefault(s[:10], []).append(s)

    for day in sorted(by_day):
        n = len(by_day[day])
        if n > MAX_PER_DAY:
            print("  [X] %s 共 %d 条，超过每日上限 %d" % (day, n, MAX_PER_DAY))
            ok = False
        elif verbose:
            print("  OK %s  %d 条（上限 %d）" % (day, n, MAX_PER_DAY))

    for s in runs:
        ts = time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
        lt = time.localtime(ts)
        start_min = lt.tm_hour * 60 + lt.tm_min
        end_min = start_min + duration_s / 60.0
        open_min = VALID_START_H * 60
        close_min = VALID_END_H * 60

        if start_min < open_min:
            print("  [X] %s 起跑早于 %02d:00" % (s, VALID_START_H))
            ok = False
        if end_min > close_min:
            print("  [X] %s 结束(%s) 越过 %02d:00"
                  % (s, time.strftime("%H:%M:%S",
                                      time.localtime(ts + duration_s)),
                     VALID_END_H))
            ok = False
        if ts > now:
            print("  [X] %s 起跑在未来" % s)
            ok = False

    # 同日两条间隔检查
    for day in sorted(by_day):
        lst = sorted(by_day[day])
        for i in range(1, len(lst)):
            t0 = time.mktime(time.strptime(lst[i-1], "%Y-%m-%d %H:%M:%S"))
            t1 = time.mktime(time.strptime(lst[i], "%Y-%m-%d %H:%M:%S"))
            need = duration_s + MIN_GAP_MIN * 60
            if t1 - t0 < need:
                print("  [X] %s 与 %s 间隔 %d 分钟 < 需要的 %d 分钟"
                      % (lst[i-1][11:16], lst[i][11:16],
                         (t1 - t0) // 60, need // 60))
                ok = False

    if verbose:
        print("  => %s" % ("全部合规" if ok else "★存在越界，已阻止提交★"))
    return ok


def fetch_existing_records(verbose: bool = True) -> list:
    """查服务端已有跑步记录列表（只读）。

    返回 [{rrid, start_ms, totalDis, totalTime, date, start_min}]
    失败返回 []（只给 warn，不阻塞 —— 排期自检仍会兜底）
    """
    try:
        import swcli
        c = swcli.Client()
        unid = int(c.session.get("unid", 0) or 0)
        body = json.dumps({"pageNum": 1, "pageSize": 100,
                           "selectedUnid": unid, "uid": c.uid},
                          separators=(",", ":"))
        _, biz, err, _ = c.call("POST", "/api/v70230/runnings/records",
                                body, verbose=False)
        if biz is None:
            if verbose:
                print("  [warn] 查询已有记录失败: %s" % (err or "")[:100])
            return []
        d = biz.get("data")
        recs = d if isinstance(d, list) else ((d or {}).get("list") or [])
        out = []
        for r in recs:
            st = int(r.get("startTime") or 0)
            if not st:
                continue
            lt = time.localtime(st / 1000.0)
            out.append({
                "rrid": r.get("rrid"),
                "start_ms": st,
                "totalDis": float(r.get("totalDis") or 0),
                "totalTime": int(r.get("totalTime") or 0),
                "date": time.strftime("%Y-%m-%d", lt),
                "start_min": lt.tm_hour * 60 + lt.tm_min,
            })
        if verbose:
            print("  [对账] 服务端已有 %d 条记录" % len(out))
            byday = {}
            for x in out:
                byday[x["date"]] = byday.get(x["date"], 0) + 1
            for d0 in sorted(byday):
                print("         %s  %d 条" % (d0, byday[d0]))
        return out
    except Exception as e:
        if verbose:
            print("  [warn] 查询已有记录异常: %s" % (str(e)[:100]))
        return []


def reconcile(runs_a: list, dur_s: int, existing: list, per_day: int,
              verbose: bool = True) -> list:
    """★ 排期对账：拿服务端已有记录过滤掉会超限的时间点。

    runs_a    : 计划的时间点（"YYYY-MM-DD HH:MM:SS"）
    existing  : fetch_existing_records() 的返回
    per_day   : 每日目标上限
    返回过滤后的 runs（不修改 service 端任何东西）
    """
    if not existing:
        return runs_a
    byday = {}
    for r in existing:
        byday.setdefault(r["date"], []).append(r["start_min"])

    out = []
    skipped = 0
    for s in runs_a:
        date0, hhmm = s[:10], s[11:16]
        hh, mm = int(hhmm[:2]), int(hhmm[3:5])
        st_min = hh * 60 + mm
        cur = byday.get(date0, [])
        if len(cur) >= per_day:
            skipped += 1
            if verbose:
                print("  [跳过] %s 已有 %d 条记录（达上限 %d）"
                      % (date0, len(cur), per_day))
            continue
        # 同日已有记录，但未满 → 检查时间间隔（新记录不能离已有记录太近）
        gap_ok = True
        for m0 in cur:
            diff = abs(st_min - m0) * 60
            if diff < dur_s + MIN_GAP_MIN * 60:
                gap_ok = False
                break
        if not gap_ok:
            skipped += 1
            if verbose:
                print("  [跳过] %s %s 与已有记录时间冲突（间隔不足）"
                      % (date0, hhmm))
            continue
        out.append(s)
    if verbose and skipped:
        print("  [对账] 剔除 %d 条与已有记录冲突的时间点，保留 %d 条"
              % (skipped, len(out)))
    return out


def _pace_to_sec(pace: str, dist_km: float) -> int:
    """配速 + 距离 → 预计用时（秒）

    ★ 走与生成器相同的解析器（`rungen.core.parse_pace`），保证
      「估算用时」与「生成轨迹」用的是同一个配速值。
      支持 `5:37` / `5'37"` / `337`（秒/公里），与前端 normPace 语义一致。
    """
    try:
        p = _parse_pace(pace)
        if not p or p <= 0:
            raise ValueError("配速必须为正数")
    except Exception as e:
        # ★ 不静默：明确告知用了兜底值，而不是悄悄算出一个离谱的用时
        print("[警告] 配速 %r 无法解析（%s），已按默认 5:40/km 估算用时"
              % (pace, e), file=sys.stderr)
        p = 340.0
    return max(300, int(p * dist_km))


def _clamp_to_valid_window(ts: float) -> float:
    """把时间戳夹进当天的有效时段 [06:00, 22:00)。

    ★ 关键：夹的是【起跑时间】，但必须保证【结束时间】也不越界。
    调用方需在拿到起跑时间后再按 duration 二次校验。
    """
    lt = time.localtime(ts)
    lo = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                      VALID_START_H, 0, 0, 0, 0, -1))
    hi = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                      VALID_END_H, 0, 0, 0, 0, -1))
    return min(max(ts, lo), hi)


def _fmt_start(offset_days: int = 0, at: str = None,
               duration_s: int = 780) -> str:
    """构造一个合理的【过去】起跑时间。

    offset_days : 0=今天，1=昨天，…
    at          : 显式指定时刻 "HH:MM" 或 "HH:MM:SS"（只给分钟时秒位随机）
    duration_s  : 预计跑量（秒），用于保证 start+duration <= 22:00

    约束：
      · 起跑与结束都落在 [06:00, 22:00) 内（★ 22 点后禁跑）
      · 起跑必须是过去（否则 stopTime 落在未来，触发风控）
      · 未指定 at 时，在当天有效窗口内随机
      · 指定 at 但已过去/越界时，会在窗口内就近重排（见返回值前的说明）
    """
    now = time.time()
    base_day = time.localtime(now - offset_days * 86400)
    y, m, d = base_day.tm_year, base_day.tm_mon, base_day.tm_mday

    day_open = time.mktime((y, m, d, VALID_START_H, 0, 0, 0, 0, -1))
    day_close = time.mktime((y, m, d, VALID_END_H, 0, 0, 0, 0, -1))
    # 今天最晚起跑 = 现在 - 60s；其它天 = 22:00 - 跑量
    latest = min(day_close - duration_s,
                 (now - 60) if offset_days <= 0 else day_close - duration_s)
    earliest = day_open
    if latest <= earliest:
        latest = earliest + 60

    if at:
        parts = at.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        # ★ 只给到分钟时，秒位随机（用户说的「07:30」指的是分钟，真实起跑落在
        #   该分钟内的某个秒上）；若显式给了秒则原样尊重。
        ss = int(parts[2]) if len(parts) > 2 else random.randint(0, 59)
        ts = time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))
        if ts > latest:
            # 用户给的时刻在未来或会导致越界 → 就近夹到最晚可行时刻
            ts = latest
        if ts < earliest:
            ts = earliest
    else:
        # 随机：优先在「早 6~9 点」「傍晚 17~21 点」两个真人运动时段里抽，
        # 都不可用（如今天已过）再退化到全天窗口均匀抽。
        slots = [(6, 9), (17, 21)]
        cands = []
        for (h0, h1) in slots:
            lo = time.mktime((y, m, d, h0, 0, 0, 0, 0, -1))
            hi = time.mktime((y, m, d, h1, 0, 0, 0, 0, -1))
            lo, hi = max(lo, earliest), min(hi, latest)
            if hi > lo:
                cands.append((lo, hi))
        if cands:
            lo, hi = random.choice(cands)
        else:
            lo, hi = earliest, latest
        ts = random.uniform(lo, hi)

    # ★ 秒位保留（原为 ":00" 硬写）：ts 本身是带随机秒的浮点时间戳，
    #   抹掉秒会让起跑时间永远是 07:00:00 这种整分整秒，一眼看出是造的。
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def plan_schedule(offset_days: int, per_day: int, duration_s: int,
                  times: list = None) -> list:
    """为一个 offset_day 规划若干条起跑时间。

    per_day : 该天要跑几条（上限 MAX_PER_DAY）
    times   : 用户显式指定的时刻列表 ["07:00","19:30"]，优先级最高
    返回：["YYYY-MM-DD HH:MM:SS", ...]
    """
    per_day = max(0, min(per_day, MAX_PER_DAY))
    if per_day == 0:
        return []
    if times:
        picked = times[:per_day]
    else:
        # 默认：一天两条就「早 + 晚」，一条就随机
        if per_day >= 2:
            picked = ["07:%02d" % random.randint(0, 50),
                      "%d:%02d" % (random.choice([17, 18, 19, 20]),
                                   random.randint(0, 55))]
        else:
            picked = [None]

    out = []
    for t in picked:
        out.append(_fmt_start(offset_days, at=t, duration_s=duration_s))
    # 同一天两条之间必须间隔 >= MIN_GAP_MIN（含跑量），否则重排
    if len(out) >= 2:
        out.sort()
        t0 = time.mktime(time.strptime(out[0], "%Y-%m-%d %H:%M:%S"))
        t1 = time.mktime(time.strptime(out[1], "%Y-%m-%d %H:%M:%S"))
        if t1 - t0 < duration_s + MIN_GAP_MIN * 60:
            # 把第二条往后挪到足够远，仍不超过 22:00
            need = duration_s + MIN_GAP_MIN * 60
            nt = t0 + need
            lt = time.localtime(nt)
            end_limit = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                     VALID_END_H, 0, 0, 0, 0, -1)) - duration_s
            if nt <= end_limit:
                out[1] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(nt))
            else:
                # 排不下就砍掉第二条
                out = out[:1]
    return out


def one_run(args, *, start: str = None, dist: float = None,
            verbose: bool = True) -> int:
    cli = ["submit", "--mode", args.mode,
           "--dist", "%.2f" % (dist or args.dist),
           "--campus-lat", "%.6f" % args.campus_lat,
           "--campus-lon", "%.6f" % args.campus_lon,
           "--pace", args.pace]
    # ★ 轨迹形状（2026-09-29 新增）：ellipse=椭圆（默认最稳）/ track=标准跑道
    if getattr(args, "shape", "ellipse") and args.shape != "ellipse":
        cli += ["--shape", args.shape]
    if getattr(args, "track_straight", 0.0):
        cli += ["--track-straight", "%g" % args.track_straight]
    if start:
        cli += ["--start", start]
    if args.weight:
        cli += ["--weight", "%.1f" % args.weight]
    # ★★ 计分跑默认**每条记录各自重新抽样**打卡点组合（2026-09-29）
    #   服务端每次返回的 5 点是从校区 6 点里随机剔 1 个（离请求坐标最近的必含），
    #   但 `swmode.load_cache` 的 TTL 是 **30 分钟** ⇒ 不加 `--force-points` 时，
    #   短时间内连续提交的几条记录会**复用同一组 5 点**（用户要求「不用一直固定
    #   一个打卡点组合」）。所以 score 模式下逐条强制重拉。
    #   ★ 代价：每条都真实请求一次接口。服务端限流是「5 分钟最多 3 次」——
    #     默认 `--days 3 --per-day 1` 正好 3 条，卡在上限内；更多条请用 --reuse-points。
    if args.force_points or (args.mode == "score" and args.fresh_points):
        cli += ["--force-points"]
    if args.force:
        cli += ["--force"]
    if args.dry_run:
        cli += ["--dry-run"]
    if args.no_obs:
        cli += ["--no-obs"]
    if args.no_verify:
        cli += ["--no-verify"]
    return _run_cli(cli, verbose)


def main():
    ap = argparse.ArgumentParser(
        description="运动世界校园 · 一键跑步（自由跑 / 计分跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--mode", choices=["free", "score"], default="free",
                    help="free=自由跑(校园范围内,无需打卡点) / score=计分跑(必须过打卡点)")
    ap.add_argument("--dist", type=float, default=2.2, help="目标距离 km（默认 2.2）")
    ap.add_argument("--campus-lat", type=float, default=None,
                    help="校区纬度（不传则登录后按学生所属学校自动获取）")
    ap.add_argument("--campus-lon", type=float, default=None,
                    help="校区经度（不传则登录后按学生所属学校自动获取）")
    ap.add_argument("--pace", default="5:40", help="目标配速（默认 5:40）")
    ap.add_argument("--shape", choices=["ellipse", "track"], default="ellipse",
                    help="轨迹形状：ellipse=椭圆（默认，最稳）；"
                         "track=标准跑道（两个半圆 + 两条直道）")
    ap.add_argument("--track-straight", type=float, default=0.0,
                    help="跑道形状的直道长度（米）；0=按打卡点自适应")
    ap.add_argument("--weight", type=float, default=65.0)

    g = ap.add_argument_group("时间安排")
    g.add_argument("--days", type=int, default=DEFAULT_BACKFILL,
                   help="往前补几天（默认 %d，最多 3 天防被查）" % DEFAULT_BACKFILL)
    g.add_argument("--per-day", type=int, default=1,
                   help="每天几条（默认 1，最多 %d）" % MAX_PER_DAY)
    g.add_argument("--times", default=None,
                   help="显式指定时刻，逗号分隔，如 '07:30,19:00'（配 --per-day 用）")
    g.add_argument("--today", action="store_true",
                   help="只跑今天（等价 --days 1 且从今天算起）")
    g.add_argument("--start", default=None,
                   help="完全手动指定开始时间 'YYYY-MM-DD HH:MM:SS'（只提交 1 条）")

    ap.add_argument("--gap", type=int, default=90,
                    help="连续提交之间的间隔秒数（防风控，默认 90）")

    dg = ap.add_argument_group("设备")
    dg.add_argument("--device", default=None,
                    help="设备别名（见 python swcli.py devices list）。默认沿用当前")

    ap.add_argument("--dry-run", action="store_true", help="只预览不提交")
    ap.add_argument("--force-points", action="store_true",
                    help="强制重拉打卡点（**所有**记录都重拉）")
    ap.add_argument("--fresh-points", dest="fresh_points", action="store_true", default=True,
                    help="计分跑时**每条记录**各自重新抽样打卡点组合（默认开）")
    ap.add_argument("--reuse-points", dest="fresh_points", action="store_false",
                    help="所有记录复用同一组打卡点（30 分钟内走缓存，省接口调用）")
    ap.add_argument("--force", action="store_true", help="打卡点不可达仍提交")
    ap.add_argument("--no-obs", action="store_true", help="跳过轨迹上传")
    ap.add_argument("--no-verify", action="store_true", help="跳过回读校验")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    a = ap.parse_args()

    # ── 校区：未显式指定坐标时，按登录态 + 学生所属学校动态取（不硬编码默认）──
    if a.campus_lat is None or a.campus_lon is None:
        if not (a.campus_lat is None and a.campus_lon is None):
            print("[ERR] --campus-lat 与 --campus-lon 必须成对提供")
            return 2
        import swcli as _swcli
        _c = _swcli.Client()
        if not (_c.uid and _c.token):
            print("[ERR] 未指定校区坐标且未登录：请登录后重试，"
                  "或显式传 --campus-lat --campus-lon")
            return 2
        _unid = int(_c.session.get("unid", 0) or 0)
        _camp = _campus.pick_campus(_c, _unid)
        if _camp.get("lat") is None or _camp.get("lon") is None:
            print("[ERR] 校区坐标未收录（%s）：请在 campus.json 手动校准，"
                  "或传 --campus-lat --campus-lon" % _camp.get("name", "?"))
            return 2
        a.campus_lat = _camp["lat"]
        a.campus_lon = _camp["lon"]
        print("校区：%s (%.6f, %.6f) 来源=%s"
              % (_camp["name"], _camp["lat"], _camp["lon"], _camp.get("source", "?")))

    if a.seed is not None:
        random.seed(a.seed)

    # ── 设备选择（必须复用真实 device_id，否则触发 10121 风控）──
    if a.device:
        r = subprocess.run(
            [sys.executable, CLI, "use", a.device],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout.rstrip())
        if r.returncode != 0:
            print("[ERR] 切换设备失败")
            return 2

    # 跑量时长（用于时间窗口自洽）
    dur = _pace_to_sec(a.pace, a.dist)

    # ── 生成时间表 ──────────────────────────────────
    if a.start:
        runs = [a.start]
        plan_desc = "手动指定"
    elif a.today:
        days = 1
        times = [t.strip() for t in a.times.split(",")] if a.times else None
        runs = plan_schedule(0, a.per_day, dur, times)
        plan_desc = "今天"
    else:
        days = a.days
        if days > 3:
            print("[warn] --days %d 超过 3 天，已夹到 3 天（时间太远的记录容易被查）" % days)
            days = 3
        times = [t.strip() for t in a.times.split(",")] if a.times else None
        runs = []
        # 从今天往过去排
        for off in range(0, days):
            runs += plan_schedule(off, a.per_day, dur, times)
        plan_desc = "最近 %d 天" % days

    if not runs:
        print("[ERR] 没有可执行的时间安排")
        return 1

    # 去重 + 排序
    runs = sorted(set(runs))

    # ★ 提交前对账：查服务端已有记录，剔除会超限/冲突的时间点
    print("=" * 62)
    print("-- 服务端记录对账 --")
    existing = fetch_existing_records(verbose=True)
    if existing:
        per_day_cap = min(a.per_day, MAX_PER_DAY)
        runs = reconcile(runs, dur, existing, per_day_cap, verbose=True)
        if not runs:
            print("[stop] 所有计划时间点都与已有记录冲突/超限，本次无提交")
            return 0

    print("=" * 62)
    print("运动世界校园 · 一键跑步")
    print("=" * 62)
    print("模式   : %s" % ("自由跑（校园范围内即可）" if a.mode == "free"
                          else "计分跑（必须经过打卡点）"))
    print("距离   : %.2f km    配速: %s（约 %d 分 %d 秒）"
          % (a.dist, a.pace, dur // 60, dur % 60))
    print("校区   : (%.6f, %.6f)" % (a.campus_lat, a.campus_lon))
    print("时间表 : %s，共 %d 条" % (plan_desc, len(runs)))
    print("限制   : 每天最多 %d 条 / 起跑及结束须在 %02d:00~%02d:00 内"
          % (MAX_PER_DAY, VALID_START_H, VALID_END_H))
    for i, s in enumerate(runs, 1):
        print("         %2d) %s" % (i, s))
    print("=" * 62)

    # ★ 提交前最后一道自检
    if not audit_schedule(runs, dur):
        print("[stop] 时间越界，已阻止提交")
        return 6

    rc_all = 0
    for idx, start in enumerate(runs, 1):
        print()
        print("#" * 62)
        print("# 第 %d/%d 条   开始时间 %s" % (idx, len(runs), start))
        print("#" * 62)
        rc = one_run(a, start=start)
        rc_all |= rc
        if rc == 5:
            print("[stop] 已阻止（打卡点不可达）。改用 --mode free 或加 --force")
            break
        if rc != 0:
            print("[warn] 第 %d 条返回码 %d" % (idx, rc))
        if idx < len(runs) and not a.dry_run:
            print("[wait] 间隔 %ds 后继续…" % a.gap)
            time.sleep(a.gap)

    print()
    print("=" * 62)
    print("全部完成，返回码汇总 = %d" % rc_all)
    return rc_all


if __name__ == "__main__":
    sys.exit(main())

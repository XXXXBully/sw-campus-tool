#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swmode.py — 运动世界校园 · 跑步「双模式」核心

校方有两种跑步模式：

  free  —— 自由跑
      · 只要在【学校范围内】即可
      · 【不需要】经过打卡点
      · 可被计分（服务端按 reasonList 4 条规则自动判定）
      · 轨迹：以校区坐标为圆心的环形绕圈

  score —— 计分跑
      · 【必须】经过服务端下发的打卡点（isFixed=1 为必经点）
      · 轨迹：把打卡点串成闭环，多圈重复直到达到目标距离
      · 若打卡点与用户所在校区距离过远（不可达），警告并建议改用 free

本模块只负责：
  1) 拉取 / 缓存打卡点（规避 5 分钟 3 次限流）
  2) 可达性判定
  3) 调生成器造轨迹
返回轨迹 JSON 路径，供 swcli.py submit 使用。
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

POINTS_CACHE = os.path.join(HERE, "points_cache.json")
POINTS_CACHE_TTL = 30 * 60          # 打卡点缓存 30 分钟（限流 5 分钟 3 次）
REACHABLE_KM = 10.0                 # 超过 10km 判定为"不可达"

EARTH_R = 6371000.0


def haversine(lat1, lon1, lat2, lon2) -> float:
    """两点球面距离（米）"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


# ══════════════════════════════════════════════════════════════════
# 打卡点：拉取 + 缓存 + 可达性
# ══════════════════════════════════════════════════════════════════
def _cache_is_credible(points: list, anchor=None) -> bool:
    """缓存里的点位是否**可信**：非空，且（给定锚点时）每个点位都在可达半径内。

    ★ 为什么必须查「点位离锚点多远」（2026-09-26 审计）：
      旧实现只比对缓存里的 `anchor` 字段，**不看点位本身在哪**。实测
      `points_cache.json` 里那 5 个点位在**北京**（距揭阳校区 **1883 km**），
      而 `anchor` 字段写的是揭阳 —— 按「锚点一致即有效」它们算"有效缓存"。
      一旦被用上就会在 1883 km 外造出一条轨迹（比坐标系那个 1194 m 的
      bug 严重 1500 倍）。
      ★ 判据与 `reachability()` 同源（`REACHABLE_KM`）：**不可达的点位对计分跑
      本来就没用**（上层会拦），所以「离锚点超过可达半径」直接判不可信。
    """
    if not points:
        return False
    if anchor is None:
        return True
    for p in points:
        try:
            d = haversine(float(anchor[0]), float(anchor[1]),
                          float(p["lat"]), float(p["lon"]))
        except (KeyError, TypeError, ValueError):
            return False
        if d / 1000.0 > REACHABLE_KM:
            return False
    return True


def load_cache(force_refresh: bool = False, anchor=None,
               allow_stale: bool = False, verbose: bool = False):
    """读取打卡点缓存。

    ★ 2026-09-26 拆参数：原来只有一个 `force`，却被两处用出了**相反**的意思 ——
      `get_points` 开头传 `force`（用户 `--force-points`）想「强制重拉」，
      而限流兜底传 `force=True` 想「忽略过期、回退到旧缓存」；
      实现里 `force=True → return None`，于是**限流兜底恒拿到 None**，
      「限流时用缓存」这句注释是**死代码**。现在拆成两个明确的参数：

        force_refresh=True —— 强制重拉，**不使用**缓存（`--force-points` 用）
        allow_stale=True   —— 允许使用**已过期**的缓存（限流兜底用）

    两种模式都必须通过 `_cache_is_credible`（锚点一致 + 点位在可达半径内）。
    不可信的缓存一律返回 None —— **宁可不跑，也别跑错地方**。
    """
    if force_refresh or not os.path.exists(POINTS_CACHE):
        return None
    try:
        obj = json.load(open(POINTS_CACHE, encoding="utf-8"))
    except Exception:
        return None
    if time.time() - float(obj.get("_ts", 0)) > POINTS_CACHE_TTL and not allow_stale:
        return None
    if anchor is not None:
        ca = obj.get("anchor")
        if ca is None or (abs(float(ca[0]) - float(anchor[0])) > 1e-5 or
                          abs(float(ca[1]) - float(anchor[1])) > 1e-5):
            # 缓存锚点与当前校区不一致 → 缓存作废（换校区/坐标变了）
            if verbose:
                print("  [cache] 缓存锚点与当前校区不一致，已忽略")
            return None
    pts = obj.get("points") or []
    if not _cache_is_credible(pts, anchor):
        if verbose:
            print("  [cache] 缓存不可信（点位距锚点超出 %.0fkm 或字段异常），已忽略"
                  % REACHABLE_KM)
        return None
    return pts


def save_cache(points: list, anchor=None):
    obj = {"_ts": time.time(), "points": points}
    if anchor is not None:
        obj["anchor"] = [float(anchor[0]), float(anchor[1])]
    json.dump(obj, open(POINTS_CACHE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def is_ratelimit(err: str) -> bool:
    return "10603" in (err or "")


# ============================================================
# ★★ 校区打卡点池（2026-09-29 新增）
# ============================================================
#   服务端每个校区的打卡点是**固定一池**（揭阳校区 = 田径场1~6 共 6 个），
#   每次只随机下发其中 5 个。而"真实跑道"的几何必须靠**完整点池**才能定出来：
#   若本次恰好没下发最北的「田径场2」，只用这 5 个点去拟合，跑道会被压成
#   近乎圆形（实测外接 96.9×95.1，而不是 163.6×75.5）。
#
#   ⇒ 每次拿到点位就并进池子（按 unid），生成"跑道"形状时用**池子**拟合，
#     轨迹于是仍会经过这次没下发、但确实存在于跑道上的那个点
#     —— 这才是真实跑道的走法。
POOL_FILE = os.path.join(HERE, "points_pool.json")


def _pool_load() -> dict:
    if os.path.exists(POOL_FILE):
        try:
            d = json.load(open(POOL_FILE, encoding="utf-8"))
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


def merge_points_pool(points: list, unid: int = 0, max_km: float = 5.0) -> list:
    """把本次下发的打卡点并进「校区池」，返回该池的全部点。

    ★★ 2026-10-08 加「同校区」护栏（踩坑：`points_pool.json` 被写脏）
      本函数是**唯一**会写 `points_pool.json` 的地方，而该文件是**随包发布**的。
      踩到的坑：`_gh_tools/test_points_cache.py` / `test_points_field.py` 直接调
      `get_points()` 喂假点位（`一号点` 23.0/116.0、`二号点` 23.001/116.001），
      两个假点被**真的写进了** `points_pool.json` 的 `3305` 池 ——
      之后所有计分跑都会把这两个 **33 km 外**的点当成"必经点"去绕：
      实测轨迹被撑成 **93.42 km**、起点跑到 (23.0005, 115.9965)、
      离真实打卡点 82~163 m（= 提交上去必然"没经过打卡点"）。
      ⇒ 新点集的质心若离已有池质心 > `max_km`，**判为不同校区：不并入、不写盘**。
      池为空（该校区首次运行）时照旧写入。
    """
    if not points:
        return []
    d = _pool_load()
    key = str(int(unid or 0))
    cur = {}
    for p in d.get(key) or []:
        if isinstance(p, dict) and p.get("pointName"):
            cur[p["pointName"]] = p

    new_pts = [p for p in points
               if isinstance(p, dict) and p.get("pointName")]
    if cur and new_pts:
        c_old = _centroid(list(cur.values()))
        c_new = _centroid(new_pts)
        if c_old is not None and c_new is not None:
            dd = haversine(c_old[0], c_old[1], c_new[0], c_new[1]) / 1000.0
            if dd > max_km:
                return list(cur.values())

    for p in new_pts:
        cur[p["pointName"]] = p
    d[key] = list(cur.values())
    try:
        json.dump(d, open(POOL_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass
    return d[key]


def _centroid(points: list):
    pts = [p for p in points if p.get("lat") is not None]
    if not pts:
        return None
    return (sum(float(p["lat"]) for p in pts) / len(pts),
            sum(float(p["lon"]) for p in pts) / len(pts))


def pool_points(points: list, unid: int = 0, max_km: float = 5.0) -> list:
    """取与本次点集**同一校区**的完整点池（并入本次点）。

    unid 命中就直接用；否则退回"质心 5 km 内最接近的那个池"
    （老版本留下的池可能没记 unid）。取不到就原样返回本次点。
    """
    d = _pool_load()
    pool = list(d.get(str(int(unid or 0))) or [])
    if not pool:
        c = _centroid(points)
        best, best_d = None, float("inf")
        if c is not None:
            for v in d.values():
                if not isinstance(v, list) or not v:
                    continue
                cc = _centroid(v)
                if cc is None:
                    continue
                dd = haversine(c[0], c[1], cc[0], cc[1]) / 1000.0
                if dd < best_d:
                    best, best_d = v, dd
        if best is not None and best_d <= max_km:
            pool = list(best)
    if not pool:
        return list(points)
    # 合并（本次点覆盖同名旧点，保证用最新坐标）
    merged = {}
    for p in pool + list(points):
        if isinstance(p, dict) and p.get("pointName"):
            merged[p["pointName"]] = p
    return list(merged.values())


def get_points(c, lat: float, lon: float, unid: int, *,
               force: bool = False, verbose: bool = True):
    """返回 (points, source)。source ∈ {"cache","remote","rate-limited"}

    ★ 2026-09-26：`force`（用户 `--force-points`）现在明确映射为
      `load_cache(force_refresh=...)`；限流兜底改用 `allow_stale=True` ——
      此前它传的是 `force=True`，而 `force=True` 的语义是「别用缓存」，
      于是「限流时回退到旧缓存」**恒拿到 None**，是一句死代码。
    """
    anchor = (lat, lon) if (lat is not None and lon is not None) else None
    pts = load_cache(force_refresh=force, anchor=anchor, verbose=verbose)
    if pts:
        if verbose:
            print("  [cache] 复用打卡点缓存 %d 个（30 分钟内有效，锚点一致）" % len(pts))
        return pts, "cache"

    import fetch_points as fp
    if verbose:
        print("--- 拉取打卡点 /api/v560/get/1/distance/1 ---")
    biz, raw_pts, err = fp.fetch_points(c, lat, lon, unid, verbose=verbose)
    if biz is None:
        if is_ratelimit(err):
            if verbose:
                print("  [限流] 10603：5 分钟内最多 3 次，请稍后再试")
            # ★ 限流兜底：允许使用**过期但可信**的缓存（锚点一致 + 点位在可达半径内）
            pts = load_cache(anchor=anchor, allow_stale=True, verbose=verbose)
            if not pts and verbose:
                print("  [限流] 且无可用缓存（不存在 / 锚点不符 / 点位离校区过远）"
                      " → 本次无法生成计分跑，请 5 分钟后重试或改用 --mode free")
            return (pts or []), "rate-limited"
        return [], "error"

    # 归一化：服务端把点位放在 pointsResModels
    d = biz.get("data") or {}
    if isinstance(d, dict):
        cand = d.get("pointsResModels") or d.get("list") or []
    elif isinstance(d, list):
        cand = d
    else:
        cand = raw_pts or []

    pts = []
    dropped = 0
    for p in cand:
        try:
            # ★ 经度字段名以服务端/参考工具为准 = lon（不是 lng，lng 只用于 27 键轨迹点）。
            #   lng 仅作防御性兜底：万一服务端改字段名，也不至于整批点位归零。
            _lat = p.get("lat")
            _lon = p.get("lon", p.get("lng"))
            pts.append({
                "pointName": p.get("pointName") or ("点位%d" % (len(pts) + 1)),
                "lat": float(_lat),
                "lon": float(_lon),
                "glat": float(p.get("glat", p.get("gLat", _lat))),
                "glon": float(p.get("glon", p.get("gLng", _lon))),
                "radius": float(p.get("radius") or 15),
                "isFixed": int(p.get("isFixed") or 0),
            })
        except (TypeError, ValueError):
            dropped += 1
            continue

    # ★ 绝不静默丢点位：候选非空却一个都没解析出来 → 字段名漂移了。
    #   若在这里默默返回 []，上层只会报「限流或接口异常」，把排查方向带偏，
    #   而计分跑会退化成「无点位上传」——成绩无效却看不出原因。
    if cand and not pts:
        raise RuntimeError(
            "打卡点解析失败：服务端返回 %d 个候选点，但字段名与预期不符"
            "（需要 lat/lon）。首个候选点原文：%s"
            % (len(cand), json.dumps(cand[0], ensure_ascii=False)[:200]))
    if dropped and verbose:
        print("  [警告] %d 个点位字段异常已跳过（解析成功 %d 个）" % (dropped, len(pts)))
    if pts:
        # ★ 只缓存**可信**的点位（2026-09-26）。服务端确实返回过远在 1883 km 外
        #   的北京点位（09-16/09-17 那批就是），旧代码会把它写进缓存，
        #   之后每次都被 `_cache_is_credible` 拒掉 → 白白多打一次接口（撞限流）。
        #   干脆不写：不可达的点位对计分跑本来也没用（上层会拦）。
        if _cache_is_credible(pts, anchor):
            save_cache(pts, anchor=anchor)
            # ★★ 同时并进「校区打卡点池」：跑道形状要靠完整点池才能拟合出
            #    真实几何（见 `pool_points` 的注释）。
            merged = merge_points_pool(pts, unid)
            if verbose and len(merged) > len(pts):
                print("  [点池] 本校区累计已知打卡点 %d 个（本次下发 %d 个）"
                      % (len(merged), len(pts)))
        elif verbose:
            print("  [警告] 打卡点距校区超出 %.0fkm（不可达），**不写入缓存**"
                  "（避免把错学校的点位存下来）" % REACHABLE_KM)
        if verbose:
            print("  [OK] 打卡点 %d 个（必经 %d 个）"
                  % (len(pts), sum(1 for x in pts if x["isFixed"] == 1)))
    return pts, "remote"


def reachability(points: list, campus_lat: float, campus_lon: float):
    """返回 (可达? , 最近距离km, 最远距离km)。无可达性结论时返回 None

    ★ 传进来的 `points` 必须是 **WGS-84**（先过 `to_wgs_points`）：
      校区坐标（`campus.py` / 生成器自由跑圆心）用的就是 WGS-84，
      拿服务端 BD-09 的 `lat/lon` 直接比会把距离算歪 ~1.2 km。
    """
    if not points:
        return None, None, None
    ds = [haversine(campus_lat, campus_lon, p["lat"], p["lon"]) / 1000.0
          for p in points]
    near, far = min(ds), max(ds)
    return (far <= REACHABLE_KM), near, far


def fixed_points(points: list) -> list:
    """必经点（isFixed=1）。用于【校验】必须命中。"""
    fx = [p for p in points if p["isFixed"] == 1]
    return fx or points


def route_points(points: list) -> list:
    """用于【串路线】的点：优先用全部点位（构成一圈完整跑道）。

    打卡点是校方在同一场地布设的多个点，全部串起来才是一圈完整闭环；
    只串必经点会让闭环退化成一个点。
    """
    return list(points) if len(points) > 1 else fixed_points(points)


def to_wgs_points(points: list) -> list:
    """把服务端打卡点归一化成 **WGS-84**（★ 2026-09-26 修坐标系二次偏移）。

    ── 服务端到底给的是什么坐标？（用 `points_cache.json` 的 5 个点做三选一）──
    服务端下发**两套**坐标，本地实测（残差 ≤ 0.102 m，即 6 位小数的取整误差）：

        lat / lon   = **BD-09**（百度，历史遗留字段）
        glat / glon = **GCJ-02**（高德 / 火星坐标）

    对照另两种假设，残差分别是 **890 m** / **1378 m** —— 都不是巧合能解释的。
    回归测试 `_gh_tools/test_crs_model.py` 把这套模型锁死。

    ── 为什么要转 WGS ──
    轨迹生成器按约定输出 **WGS-84**，`swobs.conv_point` 提交时再转一次
    WGS-84 → GCJ-02 写进 `gLat/gLng`（`coorType="gcj02"`）。
    服务端判定「有没有经过打卡点」用的就是 `gLat/gLng`(GCJ) ↔ `glat/glon`(GCJ)。
    所以喂给生成器的必须是 **GCJ 反解出来的 WGS-84**：

        WGS = gcj02_to_wgs84(glat, glon)

    ★ 真机报障（「没经过 5 个打卡点 / 只是一个在校外的小圈」）的根因：
      旧代码直接把 `lat/lon`(**BD-09**) 当 WGS 喂进去 →
      提交后落点 `wgs84_to_gcj02(BD09)` 距真点位 **1194 m**（App 判定半径 15 m）
      → 地图上整条轨迹落在校墙外，一个点都不经过。
    ★ 半修陷阱：把 `lat/lon` 当 **GCJ** 反解（`gcj02_to_wgs84(lat, lon)`）
      只修一半，提交后落点仍差 **923 m** —— 必须认准 `glat/glon` 才是 GCJ。

    凡是要跟**生成器轨迹**比位置的（生成 / 校验 / 可达性）都先过这里；
    `swsubmit.five_point_payload` 走线上原始字段，**保持原样不动**。
    """
    import swobs
    out = []
    for p in points or []:
        q = dict(p)
        if q.get("_crs") == "wgs84":        # 幂等：已归一化过的原样返回
            out.append(q)
            continue
        try:
            la, lo = float(p["lat"]), float(p["lon"])
        except (KeyError, TypeError, ValueError):
            la = lo = None
        glat, glon = p.get("glat", p.get("gLat")), p.get("glon", p.get("gLng"))
        try:
            gcj = ((float(glat), float(glon))
                   if glat is not None and glon is not None else None)
        except (TypeError, ValueError):
            gcj = None
        if gcj is None:
            if la is None:
                out.append(q)               # 连坐标都没有，原样放行（上层会判错）
                continue
            # 只有 lat/lon(BD-09) 时，先 BD-09 → GCJ-02（swobs 里现成的）
            gcj = swobs.bd09_to_gcj02(la, lo)
        wlat, wlon = swobs.gcj02_to_wgs84(gcj[0], gcj[1])
        q["lat"], q["lon"] = wlat, wlon
        q["gcj_lat"], q["gcj_lon"] = gcj[0], gcj[1]   # 提交判定所用的 GCJ，留痕
        q["bd09_lat"], q["bd09_lon"] = la, lo         # 服务端原值，留痕
        q["_crs"] = "wgs84"
        out.append(q)
    return out


# ══════════════════════════════════════════════════════════════════
# 轨迹生成
# ══════════════════════════════════════════════════════════════════
def _gen_cmd():
    return [sys.executable, os.path.join(HERE, "generator", "run_gen.py")]


def _track_tail_m() -> float:
    """跑道形状下「起点接入直线」（尾巴）的长度，米。

    ★ 唯一实现在 `generator/rungen/route.TRACK_START_TAIL_M` —— 这里只读值，
      用来给 `open_loop_tail` 定**回退下限**。
    ★ 为什么需要（2026-10-08 实测）：尾巴在闭环里走**两趟**（出 + 回），
      留口只按 `LOOP_TAIL_GAP_M`(50m) 回退时只吃掉尾巴 30m 里的一部分，
      剩下的回程与去程**重叠画花**；而且回退量从 50 起算 ⇒ 交付里程
      实测 **2060m**（请求 2000m，偏 +3%）。
      把下限抬到 `50 + 尾巴` ⇒ 交付落在 **[2000, 2030]**。
    """
    try:
        gp = os.path.join(HERE, "generator")
        if gp not in sys.path:
            sys.path.insert(0, gp)
        from rungen import route as _rt
        return max(0.0, float(_rt.TRACK_START_TAIL_M))
    except Exception:
        return 0.0


def _fmt_start(dt=None) -> str:
    """默认开始时间：5 分钟前（留出提交+上传耗时，避免 stopTime 落在未来）"""
    if dt is None:
        t = time.localtime(time.time() - 300)
    else:
        t = dt
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


def gen_free_track(campus_lat: float, campus_lon: float, dist_km: float,
                   *, start: str = None, pace: str = "5:40",
                   cadence: int = 0, seed: int = 0, outdir: str = None,
                   verbose: bool = True) -> str:
    """自由跑：以校区坐标为中心的环形绕圈（不带打卡点）"""
    outdir = outdir or os.path.join(HERE, "generator", "output")
    cmd = _gen_cmd() + [
        "--dist", "%.2f" % dist_km,
        "--start", start or _fmt_start(),
        "--lat", "%.6f" % campus_lat,
        "--lon", "%.6f" % campus_lon,
        "--mode", "loop",
        "--pace", pace,
        "--outdir", outdir,
    ]
    if cadence:
        cmd += ["--cadence", str(cadence)]
    if seed:
        cmd += ["--seed", str(seed)]
    if verbose:
        print("  [生成] 自由跑 环形 %.2fkm @ 校区(%.6f, %.6f)"
              % (dist_km, campus_lat, campus_lon))
    return _run_gen(cmd, outdir, verbose)


ANCHOR_MIN_CLEAR_M = 25.0   # 起点距最近打卡点的最小间距
ANCHOR_ON_RING = True       # True=起点落在闭环上（无引线）；False=落在质心（旧行为）


def _pick_anchor(use: list, verbose: bool = True) -> dict:
    """选一个「不压在打卡点上」的起点。

    ★ 为什么不能直接用 `use[0]`：详情页的「起」图钉会**完全遮住**它下面的
      打卡点标记 ⇒ 数据层 5 个点全命中，肉眼只数得到 4 个。

    ★★ 为什么也不能放在「质心」（2026-09-28 真机复核推翻）：
      质心在闭环**内部**，而生成器是 LOOP 模式（start → 各 cp → start），
      于是轨迹必须从环心**进出一次** ⇒ 地图上多出一根长度≈环半径的**引线**
      （实测：环半径 R≈48m，轨迹点距环心中位 47.9m 但 min 0.0m，
      半径<15m 的点有 10/143）⇒ 整条轨迹不再是一个干净的环/椭圆。
      参考图（真实跑者）里「起」「终」是**压在跑道线上**的，没有引线。

    ⇒ 现在把起点放在**闭环上**：取相邻打卡点之间的**最大方位角空隙**的中点方向，
      半径用空隙两端打卡点半径的均值 ⇒ 起点既落在环上（无引线），
      又离最近打卡点 ≥ `ANCHOR_MIN_CLEAR_M`（不遮标记）。
      实测本校区：最大空隙 85°，落点距最近打卡点 32.3m ≥ 25m ✅

    兵底：点位数 < 3、或算出的落点间距不足时，退回「质心」→ 再退回 `use[0]`。
    """
    n = len(use)
    if n == 0:
        raise ValueError("打卡点为空")

    # 米制等距投影（几十米量级，此处误差可忽略）
    lat0 = sum(p["lat"] for p in use) / n
    lon0 = sum(p["lon"] for p in use) / n
    kx = math.cos(math.radians(lat0))

    def _ll2xy(la, lo):
        return (math.radians(lo - lon0) * EARTH_R * kx,
                math.radians(la - lat0) * EARTH_R)

    def _xy2ll(x, y):
        return (lat0 + math.degrees(y / EARTH_R),
                lon0 + math.degrees(x / (EARTH_R * kx)))

    xy = [_ll2xy(p["lat"], p["lon"]) for p in use]
    cx = sum(q[0] for q in xy) / n
    cy = sum(q[1] for q in xy) / n

    if ANCHOR_ON_RING and n >= 3:
        ang = [math.atan2(q[1] - cy, q[0] - cx) for q in xy]
        rad = [math.hypot(q[0] - cx, q[1] - cy) for q in xy]
        idx = sorted(range(n), key=lambda k: ang[k])
        gaps = [(ang[idx[(k + 1) % n]] - ang[idx[k]]) % (2 * math.pi)
                for k in range(n)]
        gi = max(range(n), key=lambda k: gaps[k])
        mid = ang[idx[gi]] + gaps[gi] / 2.0
        i0, i1 = idx[gi], idx[(gi + 1) % n]
        r_loc = (rad[i0] + rad[i1]) / 2.0
        ax, ay = cx + r_loc * math.cos(mid), cy + r_loc * math.sin(mid)
        alat, alon = _xy2ll(ax, ay)
        d = min(haversine(alat, alon, p["lat"], p["lon"]) for p in use)
        if d >= ANCHOR_MIN_CLEAR_M:
            if verbose:
                print("  [起点] 落在闭环上（最大角空隙 %.0f°，距最近打卡点 %.1fm）"
                      % (math.degrees(gaps[gi]), d))
            return {"pointName": "起点", "lat": alat, "lon": alon,
                    "radius": use[0].get("radius", 15.0)}
        if verbose:
            print("  [起点] ⚠ 环上落点距最近打卡点仅 %.1fm（< %.0fm），退回质心"
                  % (d, ANCHOR_MIN_CLEAR_M))

    # 兵底：质心
    d = min(haversine(lat0, lon0, p["lat"], p["lon"]) for p in use)
    if d < ANCHOR_MIN_CLEAR_M:
        if verbose:
            print("  [起点] ⚠ 质心距最近打卡点仅 %.1fm（< %.0fm），回退到首个打卡点"
                  % (d, ANCHOR_MIN_CLEAR_M))
        return use[0]
    if verbose:
        print("  [起点] 取打卡点质心，距最近打卡点 %.1fm（避开「起」图钉遮挡）" % d)
    return {"pointName": "起点", "lat": lat0, "lon": lon0,
            "radius": use[0].get("radius", 15.0)}


def gen_score_track(points: list, dist_km: float, *,
                    start: str = None, pace: str = "5:40",
                    cadence: int = 0, seed: int = 0, outdir: str = None,
                    shape: str = "track", unid: int = 0,
                    straight_m: float = 0.0,
                    verbose: bool = True) -> str:
    """计分跑：把打卡点串成闭环，多圈重复至目标距离

    ★ 喂给生成器的必须是 **WGS-84**，而服务端给的是 BD-09(`lat/lon`) +
      GCJ-02(`glat/glon`) 两套 —— 见 `to_wgs_points`。少这一步，
      提交后落点距真点位 **1194 m**（BD-09 直喂）或 **923 m**（把 lat/lon
      误当 GCJ 的半修），App 地图上就是「在校外的一个小圈、不经过打卡点」。
      （App 判定半径 **15 m**；正确链实测最差 **5.4 m**。）
    """
    raw = route_points(points)
    if not raw:
        raise ValueError("计分跑需要打卡点，但点位列表为空")
    use = to_wgs_points(raw)

    # ★★ 跑道形状必须用「校区完整点池」拟合（2026-09-29 用户要求）：
    #    服务端每次只下发 5 个，但校区固定一池 6 个。若本次恰好没下发最北的
    #    那个点，只用 5 个点拟合会把跑道压成近乎圆形 —— 而真实跑者跑的是
    #    完整的 400 m 跑道，**那个没下发的点他照样会经过**。
    #    ⇒ 用池子拟合；因为池 ⊇ 本次下发，所以"必经点必中"不受影响。
    fit_raw = raw
    if shape == "track":
        fit_raw = pool_points(raw, unid)
        if verbose and len(fit_raw) > len(raw):
            print("  [跑道] 按完整点池拟合（%d 个，其中 %d 个本次未下发，"
                  "轨迹仍会经过）" % (len(fit_raw), len(fit_raw) - len(raw)))
    use_fit = to_wgs_points(fit_raw) if fit_raw is not raw else use

    outdir = outdir or os.path.join(HERE, "generator", "output")
    anchor = _pick_anchor(use_fit, verbose=verbose)
    cmd = _gen_cmd() + [
        "--dist", "%.2f" % dist_km,
        "--start", start or _fmt_start(),
        "--lat", "%.6f" % anchor["lat"],
        "--lon", "%.6f" % anchor["lon"],
        "--mode", "loop",
        "--pace", pace,
        "--outdir", outdir,
        "--shape", shape,
    ]
    if straight_m and straight_m > 0:
        # ★ 跑道形状的直道长度（米）。0/缺省 = 按打卡点自适应
        #   （实测揭阳点池自适应出 S=88.1、R=37.7，与 IAAF 400m 的 84.39/36.5 几乎一致）
        cmd += ["--track-straight", "%g" % straight_m]
    for p in use_fit:
        cmd += ["--cp", "%s:%.6f:%.6f:%g"
                % (p["pointName"], p["lat"], p["lon"], p["radius"])]
    if cadence:
        cmd += ["--cadence", str(cadence)]
    if seed:
        cmd += ["--seed", str(seed)]
    if verbose:
        print("  [生成] 计分跑 过 %d 个打卡点 环形 %.2fkm" % (len(use), dist_km))
        print("         坐标链：服务端 lat/lon(BD-09) + glat/glon(GCJ)"
              " -> 喂生成器 WGS-84")
        for p in use:
            print("         · %s GCJ(%.6f, %.6f) -> WGS(%.6f, %.6f) r=%gm%s"
                  % (p["pointName"], p.get("gcj_lat", p["lat"]),
                     p.get("gcj_lon", p["lon"]), p["lat"], p["lon"], p["radius"],
                     "  [必经]" if p["isFixed"] == 1 else ""))
    return _run_gen(cmd, outdir, verbose)


def _run_gen(cmd: list, outdir: str, verbose: bool) -> str:
    """跑生成器并返回产出的轨迹 JSON 路径。

    ★ 生成器会打印 "✓ JSON  <abs path>"，从这里解析最新的产出路径，
      不依赖「文件新增」或 mtime（同名文件会被覆盖，mtime 不可靠）。
    """
    os.makedirs(outdir, exist_ok=True)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise RuntimeError("生成器失败(%d):\n%s\n%s"
                           % (r.returncode, r.stdout[-1500:], r.stderr[-1500:]))
    # 从 stdout 抓 "✓ JSON  <abs>"
    import re
    for line in (r.stdout or "").splitlines():
        if "JSON" in line and (".json" in line.lower()):
            m = re.search(r"(\S+\.json)\s*$", line.strip())
            if m:
                p = m.group(1)
                if not os.path.isabs(p):
                    p = os.path.join(outdir, p)
                if os.path.exists(p):
                    if verbose:
                        try:
                            t = json.load(open(p, encoding="utf-8"))
                            mm = t.get("metrics") or {}
                            print("  [OK] %s  距离=%.2fkm 用时=%ds 点=%d"
                                  % (os.path.basename(p),
                                     float(mm.get("distance_m") or 0) / 1000.0,
                                     int(float(mm.get("duration_s") or 0)),
                                     len(t.get("points") or [])))
                        except Exception:
                            print("  [OK] %s" % os.path.basename(p))
                    return p
    raise RuntimeError("生成器未产出 JSON：\n%s" % r.stdout[-1500:])


# ══════════════════════════════════════════════════════════════════
# 轨迹校验：必过打卡点 + 首末闭合
# ══════════════════════════════════════════════════════════════════
def _track_gcj(pts: list) -> list:
    """轨迹点(WGS-84) → 提交链路坐标(GCJ-02)，与 `swobs.conv_point` 同一步。

    只有走这一步，本地算出来的「距打卡点多远」才等于 App 地图上看到的距离。
    """
    import swobs
    return [swobs.wgs84_to_gcj02(float(q["lat"]), float(q["lon"])) for q in pts]


# ★★★ 闭环「留口」—— 让详情页的「起」「终」两个图钉不重叠 ★★★
#
# ⚠ 2026-09-28 更正：起/终 画不画**不是**由「首末间距」决定的（原判断是错的）。
#   真正的开关是轨迹点的 **`type` 标记位**：首点 type=5 → 画「起」，
#   末点 type=6 → 画「终」（见 `swobs.START_PT_TYPE` / `END_PT_TYPE`）。
#   真机 A/B：只改 type ⇒ 两个图钉都出现；只改 state/radius/count/bdA… ⇒ 一个都不出现。
#   间距无关的实证：gap=101.36m 不画、96.40m 不画，而目标图里 gap 仅 ~35m 却有。
#
#   但「留口」这件事仍然要做 —— 生成器产出的闭环是**像素级重合**的
#   （历史 OBS 实测 gap 全部 0.00m），首末落在同一像素 ⇒ 两个图钉**完全重叠**，
#   肉眼只看得见后画的「终」，「起」被盖住。
#   真机记录的自然 gap 是 118.83m / 2159m（≈5.5%）。
#
# ⇒ 把闭环末点沿路径**回退 LOOP_TAIL_GAP_M 米**，把首末拉开到肉眼可分辨：
#     真机实拍 —— gap=110m 时两个图钉清晰分开、与目标图观感一致。
#
# ★★ 与 `verify_track` 的顺序（很重要）：
#   `prepare()` 现在**先校验完整闭环、再留口** —— 闭合判据保持 20m 严阈值，
#   专门用来抓「生成器没绕回起点」这类真 bug；留口是**校验通过之后**的展示层修饰。
#   （旧实现是「先留口、再校验」，逼得 gap 只能 < 20m，两个图钉挤在一起分不开。）
LOOP_TAIL_GAP_M = 50.0

# ★★★ 2026-09-29：留口距离的**上限** —— 用来让「终」也躲开打卡点 ★★★
#   问题：`_pick_anchor` 只保证了「起」距最近打卡点 ≥ `ANCHOR_MIN_CLEAR_M`，
#     没管「终」。而「终」= 闭环末点沿路径回退 `LOOP_TAIL_GAP_M` 米的位置。
#     本校区 5 个打卡点绕一圈 400m ⇒ 相邻两点弧长中位 ≈ 71m、半弧 ≈ 36m，
#     回退 50m **恰好越过**一个打卡点 ⇒ 末点落在它旁边（实测线上记录
#     1327754912 10.79m / 1327712193 8.32m / 1327343591 6.64m）
#     ⇒ 详情页「终」图钉把那个打卡点**完全遮住**，肉眼只数得到 4 个点。
#   ⇒ `open_loop_tail` 增加 `clear_points`：在 [LOOP_TAIL_GAP_M, 本上限]
#     区间里扫一个「末点离最近打卡点最远」的回退距离。
#     ★ 只在**上限以内变长**（不会变短）⇒ 只要 `prepare()` 按上限做留口补偿，
#       最终里程就恒 ≥ 目标值，绝不会掉到服务端 minDistance 之下。
#     ★ 上限取 110m 有真机依据：真机记录的自然 gap 是 118.83m / 2159m（≈5.5%）。
LOOP_TAIL_GAP_MAX_M = 110.0


def _point_at_dist(pts: list, seg: list, dist: float):
    """返回折线上累积里程 = dist 处的 (lat, lon)（线性插值）。"""
    if not pts or not seg:
        return None
    if dist <= 0:
        return (pts[0]["lat"], pts[0]["lon"])
    if dist >= seg[-1]:
        return (pts[-1]["lat"], pts[-1]["lon"])
    k = 1
    while k < len(pts) - 1 and seg[k] < dist:
        k += 1
    span = seg[k] - seg[k - 1]
    f = 0.0 if span <= 0 else (dist - seg[k - 1]) / span
    return (float(pts[k - 1]["lat"]) + (float(pts[k]["lat"]) - float(pts[k - 1]["lat"])) * f,
            float(pts[k - 1]["lon"]) + (float(pts[k]["lon"]) - float(pts[k - 1]["lon"])) * f)


def open_loop_tail(path: str, gap_m: float = LOOP_TAIL_GAP_M,
                   clear_points: list = None, verbose: bool = True) -> dict:
    """**确保**闭环首末相距至少 `gap_m` 米（把末点沿路径回退）。

    语义是 `ensure(gap >= gap_m)` —— **不是**「小于某值才动」：
      · 首末已 ≥ gap_m ⇒ 原样返回（**幂等**：连调两次不会越推越远）
      · 否则沿路径回退到首末 ≈ gap_m（末点线性插值，保留 speed/ele/dist 字段）

    ★ 为什么必须是「至少」而不是「恰好」：**生成器自身的闭合缝就有 5~11m**
      （`generator/rungen/engine.py` 的 LOOP 锚点，实测 8.2m）。旧实现写的是
      「首末 ≥ 5m 就不动」，会把这个 8m 的缝当成「本来就是开口轨迹」而放过
      ⇒ 详情页两个图钉仍然挤在一起分不开（= 白改）。

    返回 {"before_m","after_m","dropped","points"}；轨迹 JSON 就地改写。

    clear_points：**WGS-84** 的打卡点列表（计分跑传 `to_wgs_points(pts)`）。
      给了它 ⇒ 回退距离会在 `[gap_m, LOOP_TAIL_GAP_MAX_M]` 里挑一个让
      **末点（= 详情页「终」）离最近打卡点最远**的值（≥ `ANCHOR_MIN_CLEAR_M`
      才采纳）—— 否则「终」图钉会把某个打卡点完全遮住，肉眼像少打了一个点。
      ★ 只往**长**里挑 ⇒ 配合 `prepare()` 按上限做的留口补偿，最终里程恒 ≥ 目标。
      ★ 自由跑没有打卡点，传 None 即可（行为与旧版完全一致）。
    """
    t = json.load(open(path, encoding="utf-8"))
    pts = t.get("points") or []
    rep = {"before_m": None, "after_m": None, "dropped": 0,
           "points": len(pts)}
    if len(pts) < 3:
        return rep
    rep["before_m"] = haversine(pts[0]["lat"], pts[0]["lon"],
                                pts[-1]["lat"], pts[-1]["lon"])
    if rep["before_m"] >= gap_m:
        rep["after_m"] = rep["before_m"]
        return rep

    # 沿路径累积里程
    seg = [0.0] * len(pts)
    for i in range(1, len(pts)):
        seg[i] = seg[i - 1] + haversine(pts[i - 1]["lat"], pts[i - 1]["lon"],
                                        pts[i]["lat"], pts[i]["lon"])

    # ★★ 让「终」也躲开打卡点（2026-09-29）：在 [gap_m, LOOP_TAIL_GAP_MAX_M]
    #    区间里扫一个「末点离最近打卡点最远」的回退距离。
    #    ★ clear_points 必须是 **WGS-84**（与轨迹同一坐标系）——
    #      服务端下发的 lat/lon 是 BD-09，直接比会偏 ~1194m ⇒ 判据全废。
    if clear_points:
        cand = [c for c in clear_points if "lat" in c and "lon" in c]
        if cand:
            def _clear(pp):
                if pp is None:
                    return -1.0
                return min(haversine(pp[0], pp[1], q["lat"], q["lon"])
                           for q in cand)

            cur_d = _clear(_point_at_dist(pts, seg, seg[-1] - gap_m))
            best_g, best_d = gap_m, cur_d
            # ★★ 候选必须**显式包含上限**（2026-10-08 修）
            #   旧写法从 `gap_m+5` 起步、每 5 m 一档 —— 下限不是 5 的倍数时
            #   （例：跑道尾巴把下限抬到 82）永远扫不到 110，最后一个候选停在
            #   107 ⇒ 差 1 m 就够不着 `ANCHOR_MIN_CLEAR_M`，「终」被静默留在
            #   离打卡点 **2.14 m** 的位置（实测 tail=32m 时）。
            gs = []
            g = gap_m + 5.0
            while g <= LOOP_TAIL_GAP_MAX_M - 1e-9:
                gs.append(g)
                g += 5.0
            gs.append(LOOP_TAIL_GAP_MAX_M)
            for g in gs:
                d = _clear(_point_at_dist(pts, seg, seg[-1] - g))
                if d > best_d + 1e-6:
                    best_d, best_g = d, g
            # ★★ 只要比「下限处」更好就采纳（2026-10-08 修）
            #   旧逻辑是 `best_d >= ANCHOR_MIN_CLEAR_M` 才采纳 —— 扫遍区间
            #   都够不着 25 m 时就**原地不动**，把「终」留在下限那个更差的
            #   位置上（实测 tail=34m：最优候选 23.9 m 被丢弃，实际留 2.15 m）。
            #   正确语义：区间内取**净空最大**的那个，够不着阈值只是「尽力而为」
            #   + 告警，绝不该比最优候选更差。
            if best_g > gap_m and best_d > cur_d + 1e-6:
                if verbose:
                    tag = "" if best_d >= ANCHOR_MIN_CLEAR_M else "  ⚠ 仍 <%.0fm" % ANCHOR_MIN_CLEAR_M
                    print("  [留口] 终距最近打卡点 %.1fm(<%.0fm) ⇒ 回退 %.0fm→%.0fm"
                          "（终距最近打卡点 %.1fm）%s"
                          % (cur_d, ANCHOR_MIN_CLEAR_M, gap_m, best_g, best_d, tag))
                gap_m = best_g
            elif verbose and cur_d < ANCHOR_MIN_CLEAR_M:
                print("  [留口] ⚠ 扫遍 %.0f~%.0fm 仍找不到让「终」离打卡点"
                      "≥%.0fm 的回退距离（当前 %.1fm）"
                      % (LOOP_TAIL_GAP_M, LOOP_TAIL_GAP_MAX_M,
                         ANCHOR_MIN_CLEAR_M, best_d))

    target = seg[-1] - gap_m
    if target <= 0:
        # 轨迹总长比 gap_m 还短：只留首末两点（尽力而为）
        keep = [pts[0], dict(pts[-1])]
        for i, p in enumerate(keep):
            p["i"] = i
        t["points"] = keep
        json.dump(t, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        rep["points"] = len(keep)
        rep["dropped"] = len(pts) - len(keep)
        rep["after_m"] = haversine(keep[0]["lat"], keep[0]["lon"],
                                   keep[-1]["lat"], keep[-1]["lon"])
        if verbose:
            print("  [留口] 轨迹总长 %.0fm < %.0fm，只留首末两点"
                  % (seg[-1], gap_m))
        return rep

    k = 1
    while k < len(pts) - 1 and seg[k] < target:
        k += 1
    span = seg[k] - seg[k - 1]
    f = 0.0 if span <= 0 else (target - seg[k - 1]) / span

    def _lerp(a, b):
        return float(a) + (float(b) - float(a)) * f

    tail = dict(pts[k])                     # 保留 speed/cadence 等原始字段
    tail["lat"] = _lerp(pts[k - 1]["lat"], pts[k]["lat"])
    tail["lon"] = _lerp(pts[k - 1]["lon"], pts[k]["lon"])
    tail["ts"] = int(round(_lerp(pts[k - 1]["ts"], pts[k]["ts"])))
    if "time" in tail:                      # 与 ts 保持一致（生成器会写这个串）
        tail["time"] = time.strftime("%H:%M:%S",
                                     time.localtime(tail["ts"] / 1000.0))
    if "ele" in tail:
        tail["ele"] = _lerp(pts[k - 1].get("ele", 0), pts[k].get("ele", 0))
    if "dist" in tail:
        tail["dist"] = _lerp(pts[k - 1].get("dist", 0), pts[k].get("dist", 0))

    keep = pts[:k] + [tail]                 # 丢掉 k 之后的点，末点即插值点
    rep["dropped"] = len(pts) - len(keep)
    for i, p in enumerate(keep):            # 重新编号（OBS 的 id 取 p["i"]）
        p["i"] = i
    t["points"] = keep
    json.dump(t, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    rep["points"] = len(keep)
    rep["after_m"] = haversine(keep[0]["lat"], keep[0]["lon"],
                               keep[-1]["lat"], keep[-1]["lon"])
    if verbose:
        print("  [留口] 闭环末点沿路径回退 %.0fm：首末 %.2fm → %.2fm"
              % (gap_m, rep["before_m"], rep["after_m"]))
    return rep


def verify_track(path: str, points: list = None, verbose: bool = True) -> dict:
    """校验轨迹是否经过全部【必经点】、是否闭合。

    ★★ 2026-09-26 修「判据与提交链路坐标系不一致」（用户真机报障的第二层根因）：
      轨迹 JSON 里是生成器的 **WGS-84**；提交时 `swobs.conv_point` 会把它转成
      **GCJ-02** 写进 `gLat/gLng`，服务端就是拿这组 GCJ 去比打卡点的
      `glat/glon`(GCJ)。而旧版**在 WGS 空间里直接比 `p["lat"]/p["lon"]`(BD-09)**：
      两边都错、恰好互相抵消，于是本地永远打印「OK 距点 6.0m」——
      而真机上轨迹偏了 **1194 m**。**自洽的空转比没有校验更糟。**
      现在：① 点位先 `to_wgs_points` 归一化到 WGS（用来判闭合/路线是否合理）；
            ② **再按提交链路转成 GCJ 比一次，并以 GCJ 距离作为通过判据**。
      两个数都打印，一旦再漂移立刻看得见。
    """
    t = json.load(open(path, encoding="utf-8"))
    pts = t.get("points") or []
    rep = {"points": len(pts), "hits": [], "closed": False,
           "closed_gap_m": None, "ok": False}
    if pts:
        rep["closed_gap_m"] = haversine(pts[0]["lat"], pts[0]["lon"],
                                        pts[-1]["lat"], pts[-1]["lon"])
        # ★ 阈值 20m（原 5m，2026-09-25 放宽）
        #   ① 同一个函数下面判「有没有命中打卡点」用的就是 max(radius, 20) ——
        #      都是「这个位置算不算到达」，闭合判定没道理比它严 4 倍。
        #   ② 服务端**不校验**首末重合（只按 isValidPoint 判速/步幅），
        #      真实跑步起点终点本就允许几米 GPS 漂移。
        #   ③ 旧阈值 5m 曾把合法的计分跑拦下：`engine._walk` 的 min_gap
        #      外推会把闭环末点推**过**起点 5~11m → warn → swcli.py return 5
        #      →「已阻止提交」（用户手机版报错即此）。engine 侧已修成
        #      精确闭合（200 例实测 0.0000m），这里是第二道保险。
        #   闭合只跟轨迹形状有关，用 WGS 空间量即可（同空间，无坐标系问题）。
        rep["closed"] = rep["closed_gap_m"] < 20.0
    if points:
        # ★★ 2026-09-29：断言范围从「只校验必经点」扩到「校验**全部下发点**」。
        #   旧行为 `need = to_wgs_points(fixed_points(points))` 只强制 `isFixed=1`
        #   的那一个橙点（实测在线恰好 1 个），其余 4 个普通点靠生成器把它们
        #   串进环顶点来保证经过 —— **没有断言兜底**：生成器哪天漏掉某个非必经点，
        #   本地 `verify_track` 照样报 OK，提交后才发现那个点没打上卡。
        #   实测（6 种「6 选 5」组合、揭阳校区）：下发点最大偏差 4.9m，
        #   远小于阈值 max(radius, 20)=20m，所以扩范围不会误触发。
        need = to_wgs_points(list(points))           # 全部下发点都要命中
        gcj_pts = _track_gcj(pts) if pts else []
        allok = True
        for p in need:
            d_wgs = (min(haversine(p["lat"], p["lon"], q["lat"], q["lon"])
                         for q in pts) if pts else 1e9)
            # ★ 判据：提交后 App/服务端看到的那组坐标
            gla, glo = p.get("gcj_lat"), p.get("gcj_lon")
            if gcj_pts and gla is not None:
                d = min(haversine(gla, glo, q[0], q[1]) for q in gcj_pts)
            else:
                d = d_wgs
            hit = d <= max(p["radius"], 20)
            allok &= hit
            rep["hits"].append({"name": p["pointName"], "dist_m": round(d, 2),
                                "dist_wgs_m": round(d_wgs, 2), "hit": hit})
        rep["ok"] = allok and rep["closed"]
    else:
        rep["ok"] = rep["closed"]
    if verbose:
        for h in rep["hits"]:
            print("      %s %s  距点(GCJ提交) %.1fm   本地WGS %.1fm"
                  % ("OK " if h["hit"] else "MISS", h["name"], h["dist_m"],
                     h["dist_wgs_m"]))
        print("      闭合: %s (首末相距 %s m)"
              % ("是" if rep["closed"] else "否",
                 "%.1f" % rep["closed_gap_m"] if rep["closed_gap_m"] is not None else "-"))
    return rep


# ══════════════════════════════════════════════════════════════════
# 统一入口
# ══════════════════════════════════════════════════════════════════
def prepare(c, mode: str, dist_km: float, *, campus_lat: float = None,
            campus_lon: float = None, unid: int = 0, start: str = None,
            pace: str = "5:40", cadence: int = 0, seed: int = 0,
            outdir: str = None, force_points: bool = False,
            shape: str = "track", straight_m: float = 0.0,
            verbose: bool = True) -> dict:
    """按模式准备轨迹。

    返回 {"mode","track","points","tracks_left","warn"}
    """
    mode = (mode or "free").lower()
    if mode not in ("free", "score"):
        raise ValueError("mode 必须是 free 或 score")

    campus_lat = campus_lat if campus_lat is not None else getattr(c, "campus_lat", None)
    campus_lon = campus_lon if campus_lon is not None else getattr(c, "campus_lon", None)
    res = {"mode": mode, "track": None, "points": [], "warn": None}

    # ★★ 留口补偿：`open_loop_tail` 会从轨迹**尾部**去掉 LOOP_TAIL_GAP_M 米
    #    ⇒ 生成时先把这段补回来，保证「请求 2.0km 就真的交 2.0km」。
    #    不补的后果：请求 2.0km 实得 1.95km，可能撞上服务端最小距离（2000m）而提交失败。
    #    距离与时长同源（都从轨迹算），所以补距离后时长自动一致，不需要单独补。
    #  ★ 2026-09-29：计分跑按【上限】补 —— 因为 `open_loop_tail` 现在会在
    #    [LOOP_TAIL_GAP_M, LOOP_TAIL_GAP_MAX_M] 里挑一个「让终躲开打卡点」的
    #    回退距离，实际回退 ≤ 上限 ⇒ 最终里程恒 ≥ 目标值（绝不会短于 minDistance）。
    gap_budget = (LOOP_TAIL_GAP_MAX_M if mode == "score" else LOOP_TAIL_GAP_M)
    gen_km = dist_km + gap_budget / 1000.0
    if verbose and gap_budget:
        print("  [留口补偿] 目标 %.3fkm + 留口 %.0fm ⇒ 生成 %.3fkm"
              % (dist_km, gap_budget, gen_km))

    if mode == "free":
        if campus_lat is None or campus_lon is None:
            raise ValueError("自由跑需要校区坐标 --campus-lat/--campus-lon")
        if verbose:
            print("--- 模式: 自由跑（校园范围内，无需打卡点）---")
        res["track"] = gen_free_track(campus_lat, campus_lon, gen_km,
                                      start=start, pace=pace,
                                      cadence=cadence, seed=seed,
                                      outdir=outdir, verbose=verbose)
        if verbose:
            print("  [校验] 闭合性")
        # ★ 顺序：先校验**完整闭环**（20m 严阈值），再留口（展示层修饰）
        verify_track(res["track"], None, verbose=verbose)
        open_loop_tail(res["track"], verbose=verbose)
        return res

    # ── score ──────────────────────────────────────────────
    if verbose:
        print("--- 模式: 计分跑（必须经过打卡点）---")
    pts, src = get_points(c, campus_lat, campus_lon, unid,
                          force=force_points, verbose=verbose)
    if not pts:
        raise RuntimeError("未能获取打卡点（限流或接口异常），请改用 --mode free")

    if campus_lat is not None and campus_lon is not None:
        # ★ 可达性必须用 WGS-84 点位比 WGS-84 校区（服务端 lat/lon 是 BD-09，
        #   直接比会把距离算歪 ~1.2km → 可能把真实可达的打卡点误判为不可达）
        ok, near, far = reachability(to_wgs_points(pts), campus_lat, campus_lon)
        if verbose:
            print("  [可达性] 最近 %.2fkm 最远 %.2fkm  (阈值 %.0fkm)"
                  % (near, far, REACHABLE_KM))
        if not ok:
            msg = ("打卡点距校区 %.1fkm（最近 %.1fkm），明显不可达 —— "
                   "计分跑无法完成，建议改用 --mode free" % (far, near))
            res["warn"] = msg
            if verbose:
                print("  [!!] %s" % msg)
    if src == "rate-limited":
        res["warn"] = (res["warn"] or "") + " [打卡点接口限流，使用旧缓存]"

    res["points"] = pts
    res["track"] = gen_score_track(pts, gen_km, start=start, pace=pace,
                                   cadence=cadence, seed=seed,
                                   outdir=outdir, shape=shape, unid=unid,
                                   straight_m=straight_m,
                                   verbose=verbose)
    if verbose:
        print("  [校验] 必经点命中 & 闭合性")
    # ★ 顺序：先校验**完整闭环**（20m 严阈值），再留口（展示层修饰）
    rep = verify_track(res["track"], pts, verbose=verbose)
    # ★ 回退下限：跑道形状要把「尾巴的回程那一趟」算进去（见 `_track_tail_m`），
    #   否则尾巴在图上重叠、且交付里程会偏长 3%。
    gap_floor = LOOP_TAIL_GAP_M
    if shape == "track":
        gap_floor = min(LOOP_TAIL_GAP_MAX_M,
                        LOOP_TAIL_GAP_M + _track_tail_m())
    # ★ 传 WGS-84 打卡点（与轨迹同坐标系），让「终」也躲开打卡点
    open_loop_tail(res["track"], gap_m=gap_floor,
                   clear_points=to_wgs_points(pts),
                   verbose=verbose)
    if not rep["ok"]:
        res["warn"] = (res["warn"] or "") + " [轨迹未完全通过必经点/未闭合]"
    return res


def track_start_ms(path: str) -> int:
    t = json.load(open(path, encoding="utf-8"))
    pts = t.get("points") or []
    return int(pts[0]["ts"]) if pts else 0


if __name__ == "__main__":
    import swcli
    ap = __import__("argparse").ArgumentParser(description="双模式轨迹准备")
    ap.add_argument("mode", choices=["free", "score"])
    ap.add_argument("--dist", type=float, default=2.2)
    ap.add_argument("--campus-lat", type=float, default=None)
    ap.add_argument("--campus-lon", type=float, default=None)
    ap.add_argument("--pace", default="5:40")
    ap.add_argument("--force-points", action="store_true")
    a = ap.parse_args()

    cli = swcli.Client()
    unid = int(cli.session.get("unid", 0) or 0)
    import campus
    if a.campus_lat is not None and a.campus_lon is not None:
        lat, lon = float(a.campus_lat), float(a.campus_lon)
    else:
        camp = campus.pick_campus(cli, unid)
        if camp.get("lat") is None or camp.get("lon") is None:
            print("[ERR] 校区坐标未收录（%s）：请在 campus.json 手动校准，"
                  "或传 --campus-lat --campus-lon" % camp.get("name", "?"))
            sys.exit(1)
        lat, lon = camp["lat"], camp["lon"]
        unid = int(camp.get("unid") or unid or 0)
    print("模式=%s 距离=%.2fkm 校区=(%.6f, %.6f) unid=%s"
          % (a.mode, a.dist, lat, lon, unid))
    out = prepare(cli, a.mode, a.dist, campus_lat=lat, campus_lon=lon,
                  unid=unid, pace=a.pace, force_points=a.force_points)
    print("-" * 60)
    print("轨迹文件 : %s" % out["track"])
    if out["warn"]:
        print("警告     : %s" % out["warn"])

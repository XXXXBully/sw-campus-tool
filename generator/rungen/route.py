#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rungen/route.py —— 路径规划 (打卡点必经 + 距离对齐)

★★★ 核心难点
    打卡点通常离起点只有一两百米, 但目标距离可能 2~5 公里。
    单纯画一个大环会把打卡点甩在环内, 根本经过不了。

★★★ 解决方案 (真实校园跑的做法)
    1. 以"起点 + 打卡点"构建一条基础折线 (途经全部打卡点)
    2. 若基础折线长度 < 目标距离, 按**圈**重复 (多圈跑/往返跑)
       —— 这正是校园跑的真实形态: 操场绕圈、或者在教学楼之间来回
    3. 圈与圈之间做**极小幅度**侧偏（`LAP_LATERAL_M`，围绕基准线摆动，
       不做单调外推 —— 单调外推会让多圈变成同心椭圆，见该常量注释）
    4. Catmull-Rom 平滑转弯
    5. 缩放到精确目标长度 + 重采样
    6. 叠加 GPS 漂移噪声

模式:
    LOOP        环线: 起点 -> 打卡点... -> 起点 (可多圈)
    OUT_AND_BACK 折返: 起点 -> 打卡点... -> 折返 (多趟)
    POINT2POINT 点到点: 起点 -> 打卡点... -> 终点
"""
from __future__ import annotations

import itertools
import math
import os
import random
from typing import List, Tuple, Optional, Sequence

from .core import haversine, bearing, dest_point, add_gps_noise, R_EARTH


# ============================================================
# 模式常量
# ============================================================
class RouteMode:
    LOOP = "loop"                 # 环线 / 多圈
    POINT2POINT = "p2p"           # 点到点
    OUT_AND_BACK = "outback"      # 折返 / 多趟


# ★★ 计分跑多圈时的「圈间侧偏」幅度上限（米）—— 2026-09-27
#   真实跑者每圈基本沿同一条线，换道也只是道宽量级（1.22 m）。
#   旧实现按 `amp*(1+0.5*lap)` **单调向外**外推（amp=4 → 6/8/10/12 m），
#   5 圈变 5 个同心椭圆，App 上就是「一团锯齿的小圈」。
#   实测（揭阳 5 打卡点 / 2.12km / seed 1，自邻近带厚 p95）：
#       旧 23.19 m  →  新 3.81 m（其中下游 ~3.5 m 是 14.6 m 采样栅格的弦切伪影）
#   1.2 m 的幅度经下游重采样后对带厚只贡献 ~0.05 m，仅用于避免各圈逐字节相同。
LAP_LATERAL_M = 1.2


# ★★ 打卡点顶点的「微小偏移」上限（米）—— 2026-09-29
#   轨迹顶点原本直接取打卡点原坐标 ⇒ 每次都**精确穿过圆心**（实测 0.18 m），
#   比真实跑步"完美"太多（真实跑者穿过 15 m 判定圆时通常偏几米，不会次次命中圆心）。
#   加 ≤ 该值的随机偏移后，每次穿过位置都不同；幅度必须**远小于**打卡半径（15 m），
#   否则会撞上 App 的命中判定，以及 `swmode.verify_track` 的 max(radius, 20) 阈值。
#   可用环境变量 `RGEN_POINT_JITTER_M` 覆盖（A/B 对照、或临时关成 0）。
POINT_JITTER_M = float(os.environ.get("RGEN_POINT_JITTER_M", "3.5"))


# ============================================================
# ★★ 轨迹形状（2026-09-29 新增，用户可选）
# ============================================================
#   ellipse —— 现状：在打卡点上做**闭合 Catmull-Rom 样条**（圆润的环）。
#              经过全部打卡点，最稳，5 点/6 点都不会畸形。
#   track   —— 标准田径场：**两个半圆 + 两条直道**。
#              先按打卡点的「最小面积外接矩形」定出长轴与宽窄，
#              再把直道长度 / 半圆半径解出来，最后仍由
#              `_scale_about_anchors` 吸附到打卡点上（保证必中）。
SHAPE_ELLIPSE = "ellipse"
SHAPE_TRACK = "track"
SHAPES = (SHAPE_ELLIPSE, SHAPE_TRACK)

# 直道长度（米）。0 / 负值 = **按打卡点自适应**（= 外接矩形长边 − 短边）。
#   真机揭阳校区实测：自适应得到 R≈40 m、直道≈81 m、周长≈417 m，
#   与真实 400 m 跑道（直道 84.39 m / 半径 36.5 m）几乎一致。
#   想强行固定成某个值（例如 50 m）可用 `RGEN_TRACK_STRAIGHT_M` 覆盖。
TRACK_STRAIGHT_M = float(os.environ.get("RGEN_TRACK_STRAIGHT_M", "0"))

# ★★ 起点落在跑道的哪个位置（2026-10-08 用户要求）
#   用户真机反馈：「标准跑道轨迹起点能不能在这个红点位置，就是比较靠左下角」，
#   并给了参考图 —— 真机记录的「起」正好压在跑道**左下角**（直道与半圆的交点），
#   而不是直道中点。
#     True  = 落在**跑道左下角**（地图视角：东+北 最小的那个拐角）
#     False = 落在"离 `start`（= `swmode._pick_anchor` 挑的避让点）最近的曲线点"
#   可用 `RGEN_TRACK_START_CORNER=0` 退回旧行为做 A/B 对照。
TRACK_START_AT_CORNER = os.environ.get("RGEN_TRACK_START_CORNER", "1") != "0"

# ★★ 「左下角起点」距最近打卡点的最小净空（米）。
#   与 `swmode.ANCHOR_MIN_CLEAR_M` 同义 —— 详情页「起」图钉若压在打卡点圆上，
#   肉眼会以为「这个点没打上」（2026-09-29「终遮点」同类问题）。
#   ★ 为什么单独需要它：`_pick_anchor` 的 25 m 避让只管**锚点**，
#     而「左下角」是直接投到跑道拐角上的 —— **绕过了那条规则**。
#   本校区实测：拐角距「田径场4」仅 **8.46 m**（沿弧 +4 m 处更是只有 0.99 m）
#   ⇒ 必须沿曲线让开。设为 0 可关闭让避（`RGEN_TRACK_START_CLEAR_M=0`）。
TRACK_START_CLEAR_M = float(os.environ.get("RGEN_TRACK_START_CLEAR_M", "25"))

# ★★ 起点「接入直线」（尾巴）长度，米（2026-10-08 用户要求）。
#   用户看完第一版渲染后反馈：「这个起点还不够下，可以再下一点，
#   然后一段直线接入这个标准跑道」。
#   语义：起点**不在跑道上**，而在跑道左下角**外侧** `TRACK_START_TAIL_M` 米处；
#        从起点到跑道接入点（左下角）是**一条直线**。
#   ★ 方向 = 接入点处直道的**切向外延**（= 把直道再延长一段）——
#     几何上拐角处直道与半圆本来就是相切的，所以这条尾巴与直道共线，
#     画出来就是「直道向下多延伸了一截」，不是斜插进来的引线。
#   ★ 为什么这样反而**更真**：真机参考图（2026-10-08）里「起」图钉的锚点
#     就落在跑道左下**外侧**，轨迹首点（红点）才在跑道拐角上。
#   ★ 里程影响：尾巴走两趟（出+回），所以环本体只跑到
#     `target_len - 2*TRACK_START_TAIL_M`；而 `open_loop_tail` 的回退
#     （50~110 m）会**吃掉回程那一趟** ⇒ 最终轨迹只走一趟尾巴，
#     「终」落在环上（与参考图一致：起≠终）。
#   ★ 默认 **40 m**（2026-10-08 第三轮改）：接入点从「左下角拐角」挪到
#     「半圆最底点」后，起点会**往东靠 41 m** ⇒ 逼近「田径场3」（它就贴在
#     南侧半圆上，距弧仅 2.15 m）⇒ 尾巴 32 m 时起净空只有 **18.9 m**（< 25）。
#     加长到 40 m ⇒ **26.9 m** ✅（48 m ⇒ 34.9 m）。
#     ★ 拐角模式下 40 m 同样成立（起净空 40.0 m），所以两个模式共用一个默认值。
#   ★ 最初 32 m 的依据：按真机参考图量，图钉锚点离跑道拐角约 **32 m**
#     （参考图 880px 宽、跑道宽 ~260px ≈ 73 m ⇒ 3.56 px/m，图钉锚点偏 115px）。
#   设为 0 可关闭（起点回到跑道曲线上，即上一版行为）。
TRACK_START_TAIL_M = float(os.environ.get("RGEN_TRACK_START_TAIL_M", "40"))

# ★★ 接入直线的**方向**（2026-10-08 用户第二版澄清：「横着接入，由西向东」）
#     False（默认）= **正西→正东**（地图视角的**水平**线）：
#                     起点在跑道正左方，轨迹向东进入跑道。
#     True          = 上一版：沿直道**切向外延**（竖直，方位 186°）。
#   可用 `RGEN_TRACK_TAIL_ALONG_LANE=1` 切回上一版做 A/B。
TRACK_TAIL_ALONG_LANE = os.environ.get("RGEN_TRACK_TAIL_ALONG_LANE", "0") != "0"

# ★★ 接入点落在跑道的**哪个位置**（2026-10-08 用户第三轮追加：
#    「这个横向接入位置能接在标准跑道的**半圆最底点**附近吗」）
#     "corner"        = **左下角拐角**（直道与半圆的交点）—— 上一版；
#     "bottom"（默认）= **南侧半圆的最底点**（长轴南端顶点）。
#   ★ 为什么接在底点反而**更平滑**：正西尾巴的方向 ≈ 短轴方向，而半圆在
#     最底点的**切向**也 ≈ 短轴方向 ⇒ 尾巴与半圆**相切**，转角从拐角处的
#     ~96° 降到 **~6°**（画出来是一条直线"顺"进弧线，没有折角）。
#   ★ 代价：起点会往东挪 ⇒ 靠近「田径场3」⇒ 起净空从 32.2 m 掉到 18.9 m
#     （低于 25 m 避让阈值）。补救 = 把尾巴加长（40 m ⇒ 26.9 m，48 m ⇒ 34.9 m）。
#   ★ `TRACK_START_BOTTOM_DEG`：在最底点所在半圆上**再偏一点**（度）。
#     0 = 正最底点；负值 = 往**西**偏（离「田径场3」更远、净空回升）。
#     实测（尾巴 32 m）：-15° ⇒ 28.4 m、-30° ⇒ 30.4 m（同时保住平滑）。
TRACK_START_AT_BOTTOM = os.environ.get("RGEN_TRACK_START_AT", "bottom").lower() \
    in ("bottom", "1", "true", "yes")
TRACK_START_BOTTOM_DEG = float(os.environ.get("RGEN_TRACK_BOTTOM_DEG", "0"))

# 跑道曲线采样步长（米）。必须足够密，否则 Catmull-Rom 会把直道
#   切出肉眼可见的折角（直道本来就该是直的）。
TRACK_STEP_M = 2.0


# ============================================================
# 平面坐标转换 (等距圆柱, 校园尺度足够精确)
# ============================================================
def _to_xy(lat0: float, lon0: float,
           pts: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    k = math.cos(math.radians(lat0))
    return [((lo - lon0) * k * math.radians(1) * R_EARTH,
             (la - lat0) * math.radians(1) * R_EARTH) for la, lo in pts]


def _to_ll(lat0: float, lon0: float,
           xy: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    k = math.cos(math.radians(lat0))
    return [(lat0 + math.degrees(y / R_EARTH),
             lon0 + math.degrees(x / (R_EARTH * k))) for x, y in xy]


def polyline_length(pts: Sequence[Tuple[float, float]]) -> float:
    return sum(haversine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
               for i in range(len(pts) - 1))


# ============================================================
# Catmull-Rom 样条
# ============================================================
def catmull_rom(points: Sequence[Tuple[float, float]],
                samples_per_seg: int = 12,
                closed: bool = False,
                alpha: float = 0.5) -> List[Tuple[float, float]]:
    """Centripetal Catmull-Rom (alpha=0.5), 曲线通过所有控制点"""
    pts = list(points)
    if len(pts) < 2:
        return pts
    if closed:
        pts = [pts[-1]] + pts + [pts[0], pts[1]]
    else:
        pts = [pts[0]] + pts + [pts[-1]]

    def _tj(ti, pi, pj):
        d = math.hypot(pj[0] - pi[0], pj[1] - pi[1])
        return ti + (d ** alpha if d > 1e-9 else 1e-9)

    out: List[Tuple[float, float]] = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        t0 = 0.0
        t1 = _tj(t0, p0, p1)
        t2 = _tj(t1, p1, p2)
        t3 = _tj(t2, p2, p3)
        if t2 - t1 < 1e-12:
            continue

        def lerp(pa, pb, ta, tb, t):
            if tb - ta < 1e-12:
                return pb
            w = (t - ta) / (tb - ta)
            return (pa[0] + (pb[0] - pa[0]) * w,
                    pa[1] + (pb[1] - pa[1]) * w)

        for s in range(samples_per_seg):
            t = t1 + (t2 - t1) * (s / samples_per_seg)
            a1 = lerp(p0, p1, t0, t1, t)
            a2 = lerp(p1, p2, t1, t2, t)
            a3 = lerp(p2, p3, t2, t3, t)
            b1 = lerp(a1, a2, t0, t2, t)
            b2 = lerp(a2, a3, t1, t3, t)
            out.append(lerp(b1, b2, t1, t2, t))
    out.append(pts[-2])
    return out


# ============================================================
# 缩放
# ============================================================

def scale_xy(path_xy: Sequence[Tuple[float, float]],
             target_len: float) -> List[Tuple[float, float]]:
    """
    在**平面米坐标**下围绕首点缩放, 使总长度 = target_len。
    注意: 输入输出都是 XY 米坐标 (不是经纬度), 避免重复投影导致坍缩。
    """
    if len(path_xy) < 2:
        return list(path_xy)
    import math as _m
    cur = sum(_m.hypot(path_xy[i][0] - path_xy[i - 1][0],
                       path_xy[i][1] - path_xy[i - 1][1])
              for i in range(1, len(path_xy)))
    if cur <= 1e-9:
        return list(path_xy)
    k = target_len / cur
    x0, y0 = path_xy[0]
    return [(x0 + (x - x0) * k, y0 + (y - y0) * k) for x, y in path_xy]


def scale_to_length(path: Sequence[Tuple[float, float]],
                    target_len: float) -> List[Tuple[float, float]]:
    """经纬度版本: 投影到平面 -> 缩放 -> 投回经纬度"""
    if len(path) < 2:
        return list(path)
    la0, lo0 = path[0]
    xy = _to_xy(la0, lo0, path)
    xy = scale_xy(xy, target_len)
    return _to_ll(la0, lo0, xy)


def _jitter_points(ring: Sequence[Tuple[float, float]],
                   rng: random.Random,
                   jitter_m: float = None,
                   ) -> List[Tuple[float, float]]:
    """给环上的**打卡点顶点**加半径内的随机小偏移（首个顶点 = 起点，保持不动）。

    ★ 为什么：顶点原本直接取打卡点原坐标 ⇒ 每次都**精确穿过圆心**（实测 0.18 m），
      真实跑步不会这么准。加偏移后每次穿过位置都不同，观感自然，
      但仍远小于 App 的打卡半径（15 m）。

    偏移半径取 `jitter_m * sqrt(u)` 而不是 `jitter_m * u` —— 后者会让点挤在
      圆心附近（圆内面积 ∝ r²，要面积均匀就得按 sqrt 分布）。
    """
    ring = list(ring)
    if jitter_m is None:                      # 运行时读模块常量，便于 A/B 对照
        jitter_m = POINT_JITTER_M
    if jitter_m <= 0 or len(ring) < 2:
        return ring
    out = [ring[0]]
    for la, lo in ring[1:]:
        brg = rng.uniform(0.0, 360.0)
        r = jitter_m * math.sqrt(rng.random())
        out.append(dest_point(la, lo, brg, r))
    return out


# ============================================================
# ★ 跑道形状：最小面积外接矩形 + 「两个半圆 + 两条直道」
# ============================================================
def _convex_hull(pts: Sequence[Tuple[float, float]]
                 ) -> List[Tuple[float, float]]:
    """Andrew 单调链求凸包（返回逆时针、无重复首点）。"""
    ps = sorted(set((float(x), float(y)) for x, y in pts))
    if len(ps) <= 2:
        return ps

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in ps:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Tuple[float, float]] = []
    for p in reversed(ps):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _min_area_obb(pts: Sequence[Tuple[float, float]]):
    """最小面积外接矩形（旋转卡壳）。

    返回 `(cx, cy, u, w, h)`：中心、长轴单位向量、长边长度、短边长度。

    ★ 为什么用它而不是 PCA / 协方差主轴（2026-09-29 实测教训）：
      打卡点只有 5 个、且分布在环上，**协方差主轴极不稳定** ——
      实测「6 选 5」里最差的一组主轴算成 60.7°（真值 ≈ 90°），
      于是 R/S 全错，拟合出的"跑道"离打卡点最远 **38 m**（阈值 20 m）。
      外接矩形对点集扰动的敏感度低得多，且天然给出"长边/短边"。
    """
    hull = _convex_hull(pts)
    if len(hull) < 3:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0,
                (1.0, 0.0), max(xs) - min(xs), max(ys) - min(ys))

    best = None
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        if seg < 1e-9:
            continue
        ux, uy = dx / seg, dy / seg
        vx, vy = -uy, ux
        us = [p[0] * ux + p[1] * uy for p in hull]
        vs = [p[0] * vx + p[1] * vy for p in hull]
        w = max(us) - min(us)
        h = max(vs) - min(vs)
        area = w * h
        if best is None or area < best[0] - 1e-9:
            cu = (max(us) + min(us)) / 2.0
            cv = (max(vs) + min(vs)) / 2.0
            best = (area, cu * ux + cv * vx, cu * uy + cv * vy, (ux, uy), w, h)
    _area, cx, cy, u, w, h = best
    if w < h:                       # 保证 u 指向长边
        u = (-u[1], u[0])
        w, h = h, w
    return cx, cy, u, w, h


def _stadium_ring(start: Tuple[float, float],
                  waypoints: Sequence[Tuple[float, float]],
                  straight_m: float = None,
                  step: float = TRACK_STEP_M
                  ) -> List[Tuple[float, float]]:
    """把打卡点拟合成标准田径场（两个半圆 + 两条直道），返回经纬度闭环顶点。

    几何：直道长 `S`、半圆半径 `R` ⇒ 周长 = `2S + 2πR`。
      · `R` = 外接矩形短边的一半（跑道的"宽"）
      · `S` = 外接矩形长边 − 短边（自适应）；给了 `straight_m` 就用给定值
    曲线在**打卡点所在平面**里生成，随后仍由 `plan_route` 的
    `_scale_about_anchors` 吸附，保证每个打卡点都必中。
    """
    la0, lo0 = start
    wxy = _to_xy(la0, lo0, list(waypoints))
    cx, cy, u, w, h = _min_area_obb(wxy)

    R = max(h / 2.0, 5.0)
    S = max(w - h, 0.0)
    if straight_m is not None and straight_m > 0:
        S = float(straight_m)

    vx, vy = -u[1], u[0]

    def P(a: float, b: float) -> Tuple[float, float]:
        return (cx + a * u[0] + b * vx, cy + a * u[1] + b * vy)

    half = S / 2.0
    n_str = max(2, int(round(S / step))) if S > 1e-6 else 0
    n_arc = max(6, int(round(math.pi * R / step)))

    pts: List[Tuple[float, float]] = []
    # 直道 1（-half → +half，在 -R 侧）
    for i in range(n_str + 1):
        pts.append(P(-half + S * i / max(1, n_str), -R))
    # 半圆 1（右端，-R → +R）
    for i in range(1, n_arc + 1):
        ang = -math.pi / 2.0 + math.pi * i / n_arc
        pts.append(P(half + R * math.cos(ang), R * math.sin(ang)))
    # 直道 2（+half → -half，在 +R 侧）
    for i in range(1, n_str + 1):
        pts.append(P(half - S * i / max(1, n_str), R))
    # 半圆 2（左端，+R → -R）；末点与首点重合由调用方 `_open_ring` 处理
    for i in range(1, n_arc + 1):
        ang = math.pi / 2.0 + math.pi * i / n_arc
        pts.append(P(-half + R * math.cos(ang), R * math.sin(ang)))

    ll = _to_ll(la0, lo0, pts)
    ll = rotate_ring(ll, start)
    return ll + [ll[0]]


def _nudge_off_points(corner: Tuple[float, float],
                      start: Tuple[float, float],
                      waypoints: Sequence[Tuple[float, float]],
                      straight_m: float = None,
                      clear_m: float = None) -> Tuple[float, float]:
    """把「左下角起点」沿跑道曲线让开打卡点，保证净空 ≥ `clear_m` 米。

    ★★ 为什么必须做（2026-10-08 实测）：`swmode._pick_anchor` 有 25 m 避让规则，
      但「左下角」是**直接投到跑道拐角上**的 —— 绕过了那条规则。
      本校区实测：拐角距「田径场4」仅 **8.46 m**（沿弧 +4 m 处更是只有 0.99 m）
      ⇒ 详情页「起」图钉会压住该打卡点，肉眼以为「这个点没打上」。
      让避方向取「沿曲线走得更近的那一侧」，落点仍在左下角区域、仍在跑道上。
    ★ 找不到满足净空的点就**原样返回拐角**（宁可遮点，也不要跑偏）。
    ★ `clear_m <= 0` 时直接返回拐角（关闭让避，供 A/B）。
    """
    clear_m = TRACK_START_CLEAR_M if clear_m is None else clear_m
    cps = [(float(p[0]), float(p[1])) for p in waypoints]
    if not cps or clear_m <= 0:
        return corner

    def clr(p):
        return min(haversine(p[0], p[1], q[0], q[1]) for q in cps)

    if clr(corner) >= clear_m:
        return corner

    ring = _stadium_ring(start, waypoints, straight_m=straight_m)
    if len(ring) < 3:
        return corner
    n = len(ring)
    ci = min(range(n), key=lambda i: haversine(corner[0], corner[1],
                                               ring[i][0], ring[i][1]))

    def arc_to(k, d):
        """从 ci 沿方向 d 走到第 k 步的累计弧长（米）。"""
        tot = 0.0
        for j in range(k):
            a = ring[(ci + d * j) % n]
            b = ring[(ci + d * (j + 1)) % n]
            tot += haversine(a[0], a[1], b[0], b[1])
        return tot

    best = None
    for d in (+1, -1):
        for k in range(1, n):
            i = (ci + d * k) % n
            if clr(ring[i]) >= clear_m:
                arc = arc_to(k, d)
                if best is None or arc < best[0]:
                    best = (arc, ring[i])
                break
    return best[1] if best else corner


def _stadium_nearest(start: Tuple[float, float],
                     waypoints: Sequence[Tuple[float, float]],
                     straight_m: float = None) -> Tuple[float, float]:
    """返回"起点应该落在跑道曲线的哪一点"（经纬度）。

    ★★ 为什么必须做（2026-09-29 实测）：`swmode._pick_anchor` 是按**打卡点
      构成的环**挑起点的（最大角空隙中点），它并不知道我们要画跑道；
      于是起点可能落在跑道**外侧** 44.6 m 处。LOOP 是 `start → 各 cp → start`，
      起点在环外 ⇒ 轨迹必须"出去一趟再回来" ⇒ 地图上多一根 44 m 的**引线**，
      外接矩形被撑宽（实测 163×75 的跑道变成 167×119），直道也被拉弯。
      ⇒ 跑道形状下，先把起点投到跑道曲线上，再走后面的流程。

    ★★ 2026-10-08 改：投影目标从「离 `start` 最近的曲线点」改成
      **跑道左下角**（见 `TRACK_START_AT_CORNER`）。用户真机反馈 + 参考图：
      真机记录的「起」压在跑道左下角（直道与半圆的交点）上，而不是直道中点。
      实测本校区：旧行为落点在**西直道中点以北**（局部坐标 a=+20.8, b=−R），
      新行为落在**西南角**（a=+half, b=−R）—— 与参考图一致。
      `RGEN_TRACK_START_CORNER=0` 可退回旧行为。
      ★ 拐角落点随后还要过一遍 `_nudge_off_points`（净空让避）——
        因为本校区「田径场4」正压在拐角上（距 8.46 m）。
    """
    la0, lo0 = start
    wxy = _to_xy(la0, lo0, list(waypoints))
    cx, cy, u, w, h = _min_area_obb(wxy)
    R = max(h / 2.0, 5.0)
    S = max(w - h, 0.0)
    if straight_m is not None and straight_m > 0:
        S = float(straight_m)

    vx, vy = -u[1], u[0]
    half = S / 2.0

    if TRACK_START_AT_BOTTOM:
        # ★★ 2026-10-08 第三轮：接入点 = **南侧半圆的最底点**（长轴南端顶点）。
        #   取两个长轴端点里**更靠南**（全局 y 更小）的那个 = 地图视角的"底"。
        #   半圆参数 θ 从该端点起算（0 = 正底点，负 = 往西偏）。
        ends = []
        for sgn in (+1.0, -1.0):
            qx, qy = cx + sgn * (half + R) * u[0], cy + sgn * (half + R) * u[1]
            ends.append((qy, sgn))
        ends.sort()
        esgn = ends[0][1]
        th = math.radians(TRACK_START_BOTTOM_DEG)
        # ★★ 注意 θ 必须相对**朝外方向** `esgn*a` 量：
        #   `ca = esgn*half + R*cos θ` 只有 esgn=+1 时 θ=0 才是顶点；
        #   esgn=-1 时它会落到 OBB **内部**（实测 a=-8.9 而不是 -77.6）。
        #   正确写法 = `esgn*(half + R*cos θ)`（与 `_stadium_ring` 的
        #   半圆参数化一致：`P(±half + R*cos ang, R*sin ang)`，
        #   其中 ang∈(π/2,3π/2) 那一半才是**朝外**的）。
        ca = esgn * (half + R * math.cos(th))
        cb = R * math.sin(th)
        qx = cx + ca * u[0] + cb * vx
        qy = cy + ca * u[1] + cb * vy
        # ★ 与拐角分支同理：开了尾巴就不做「沿曲线让避」（尾巴已在环外）。
        return _to_ll(la0, lo0, [(qx, qy)])[0]

    if TRACK_START_AT_CORNER:
        # 4 个"角" = 两条直道与两个半圆的交点，局部坐标就是 (±half, ±R)。
        # "左下角"取地图视角：x 是东、y 是北 ⇒ 最小化 (x + y)。
        #   （长轴朝南北时 = 西南角；长轴朝东西时 = 同样是屏幕左下那个角。）
        best = None
        for ca, cb in ((half, -R), (half, R), (-half, -R), (-half, R)):
            qx = cx + ca * u[0] + cb * vx
            qy = cy + ca * u[1] + cb * vy
            key = qx + qy
            if best is None or key < best[0]:
                best = (key, qx, qy)
        corner = _to_ll(la0, lo0, [(best[1], best[2])])[0]
        # ★★ 2026-10-08：开了「接入直线」（尾巴）时**不做让避**。
        #   原因：让避是为了别让「起」图钉压住打卡点 —— 而现在「起」落在
        #   环外 `TRACK_START_TAIL_M` 米处，图钉早就不在拐角上了；
        #   反而轨迹从**正拐角**进环时离打卡点最近（本校区距「田径场4」
        #   8.46 m < 判定半径 15 m），更容易"打上卡"。
        #   若此时还沿曲线让避 18 m，尾巴就会先与直道重叠 18 m 再出去 —— 画花了。
        if TRACK_START_TAIL_M > 0:
            return corner
        # ★ 净空让避：拐角可能正压着打卡点（本校区实测距「田径场4」仅 8.46 m）
        return _nudge_off_points(corner, start, waypoints, straight_m)

    px, py = 0.0, 0.0                      # start 在局部 XY 里就是原点
    a = (px - cx) * u[0] + (py - cy) * u[1]
    b = (px - cx) * vx + (py - cy) * vy
    if abs(a) <= half:                     # 投影到直道
        qa, qb = a, (R if b >= 0 else -R)
    else:                                  # 投影到半圆
        ca = half if a > 0 else -half
        da, db = a - ca, b
        L = math.hypot(da, db)
        if L < 1e-9:
            qa, qb = ca, R
        else:
            qa, qb = ca + R * da / L, R * db / L
    qx = cx + qa * u[0] + qb * vx
    qy = cy + qa * u[1] + qb * vy
    return _to_ll(la0, lo0, [(qx, qy)])[0]


def _tail_dir_xy(entry: Tuple[float, float],
                 waypoints: Sequence[Tuple[float, float]],
                 straight_m: float = None) -> Tuple[float, float]:
    """返回接入点 → 起点 S 的单位方向（平面米坐标，x=东 y=北）。

    ★★ 2026-10-08 用户澄清（第二版）：「我说的是**横着**接入，就是**由西向东**」
      ⇒ 尾巴必须是**地图视角的水平线**：起点在跑道**正左方**，轨迹向东进入跑道。
      上一版按「直道切向外延」做成了**竖直**的（方位 186°），方向错了。
      ⇒ 方向恒为**正西** `(-1, 0)`。

    ★ 为什么「正西」对两种跑道朝向都成立：
      · 长轴南北（本校区）：接入点在西直道上，向西 = **垂直**于直道 = 水平线；
      · 长轴东西：接入点是西端拐角，向西 = **沿着**直道 = 同样是水平线。
      两种情形画出来都是"从左边横着接进来"。

    ★ 接入点仍是**左下角拐角**：该拐角是整条跑道**最西**的点，
      水平线向东第一个碰到的就是它 —— 交点唯一，不会切进跑道内部。
    ★ `RGEN_TRACK_TAIL_ALONG_LANE=1` 可退回上一版（沿直道竖直外延）做 A/B。
    """
    if not TRACK_TAIL_ALONG_LANE:
        return (-1.0, 0.0)                     # 正西（水平接入）
    la0, lo0 = entry
    wxy = _to_xy(la0, lo0, list(waypoints))
    cx, cy, u, _w, _h = _min_area_obb(wxy)
    ex, ey = _to_xy(la0, lo0, [entry])[0]
    a = (ex - cx) * u[0] + (ey - cy) * u[1]
    sgn = 1.0 if a >= 0 else -1.0
    return (sgn * u[0], sgn * u[1])


def _attach_start_tail(closed_ll: Sequence[Tuple[float, float]],
                       entry: Tuple[float, float],
                       tail_m: float,
                       waypoints: Sequence[Tuple[float, float]],
                       straight_m: float = None
                       ) -> List[Tuple[float, float]]:
    """在闭环的**接入点**上接一段直线尾巴，起点落到环外 `tail_m` 米处。

    输入 `closed_ll` 必须是**已锚定**的闭环（首点 = 末点 = `entry`，见
    `anchor_ring`）。输出仍是闭环：`[S, entry, p1 ... pn, S]`，
    即「起点 S → 直线接入 entry → 绕环一圈 → 沿尾巴回 S」。

    ★ 回程那一趟会被 `swmode.open_loop_tail`（回退 50~110 m）吃掉
      ⇒ 最终交付的轨迹里尾巴**只走一趟**，「终」落在环上（起 ≠ 终，
      与真机参考图一致）。
    ★ `tail_m <= 0` 原样返回（关闭该特性）。
    """
    ring = _open_ring(closed_ll)
    if tail_m <= 0 or len(ring) < 3:
        return list(closed_ll)
    dx, dy = _tail_dir_xy(entry, waypoints, straight_m)
    la0, lo0 = entry
    ex, ey = _to_xy(la0, lo0, [entry])[0]
    S = _to_ll(la0, lo0, [(ex + dx * tail_m, ey + dy * tail_m)])[0]
    return [S] + ring + [S]


# ============================================================
# ★ 核心: 构建经过所有打卡点的基础环
# ============================================================
def _build_base_loop(start: Tuple[float, float],
                     waypoints: Sequence[Tuple[float, float]],
                     rng: random.Random,
                     bulge: float = 1.35,
                     shape: str = SHAPE_TRACK,
                     straight_m: float = None) -> List[Tuple[float, float]]:
    """
    构建一条"经过起点和所有打卡点"的基础闭合环。

    ★★ 2026-09-25 重写: 从"多边形 + 垂直尖刺凸起"改为"TSP 顺序 + 闭合样条"

    旧实现:
        按**方位角**排序打卡点, 连成多边形, 再在每条边的 30% / 70% 处插两个
        垂直"凸起"控制点 (off = d*(bulge-1)*U(0.6,1) —— 100m 的边就是
        21~35m 的横向尖刺)。
        实测后果:
          · 相邻段转角**中位 53°、60.8% 超过 30°** —— 画出来是带尖刺的乱麻;
          · 环面积只有 **822 m²**, 而真正的 400m 跑道环应有 **49488 m²**;
          · 尖刺让折线弧长虚增, 下游 `_align_geometry` 重采样后又被
            `_correct_length` 整体放大, 打卡点被径向推离路线 (手机端
            反馈「根本没经过打卡点, 轨迹还不是圆」)。
          · 方位角排序在多打卡点时还会产生大量交叉往返 (与 `_tsp_min_legs`
            存在的理由相同)。

    新实现:
        1. 用 `_tsp_min_legs` 求**最短访问顺序** (而不是方位角排序)
        2. 用**闭合 Catmull-Rom** 把顶点串成圆润的环 —— 曲线通过所有打卡点,
           且没有高频尖刺
        3. 旋转到以离起点最近的顶点开头 (`_repeat_to_length` 依赖 base[0]
           在起点附近)

    实测 (2.05km / 5 个揭阳校区打卡点): bbox 94×181m, 环面积 49488 m²,
    5 圈 ≈ 每圈 410m (正好是 400m 跑道), 打卡点最差 5.4m。

    :param bulge: 保留形参以兼容旧调用方; 新实现不再使用 (平滑环的"胖瘦"
        由样条本身决定, 长度交给 `_repeat_to_length` 的圈数去凑)。
    """
    if not waypoints:
        # 无打卡点: 生成一个不规则圆
        n_ctrl = rng.randint(6, 9)
        r = 150.0
        ctrl = []
        for i in range(n_ctrl):
            ang = 360.0 * i / n_ctrl
            rr = r * (1.0 + 0.18 * math.sin(3 * math.radians(ang) + rng.uniform(0, 3))
                      + 0.09 * math.sin(5 * math.radians(ang) + rng.uniform(0, 3)))
            ctrl.append(dest_point(start[0], start[1], ang, rr))
        return ctrl + [ctrl[0]]

    # ★★ 形状分派（2026-09-29）：`track` = 标准田径场（两个半圆 + 两条直道）
    #   注意：跑道形状**不做打卡点抖动**（`_jitter_points`）——
    #   曲线本来就不在打卡点上，抖动只会让拟合整体平移，而下游
    #   `_scale_about_anchors` 又会把它拉回来，等于白抖。
    if shape == SHAPE_TRACK:
        return _stadium_ring(start, waypoints,
                             straight_m=(straight_m if straight_m is not None
                                         else TRACK_STRAIGHT_M))

    # 1. 最短访问顺序 (含起点), 闭合环顶点序列 [start, wp...]
    #
    # ★★ 2026-09-28 修「闭环回程边没被计入代价」：
    #   LOOP 的最后一段是「末个打卡点 → 起点」，可旧代码传 end=None，
    #   求的是**开路**最短路径 —— 回程边完全不参与优化。
    #   实测（揭阳校区 6 选 5 的「缺田径场4」那组）：开路最优顺序
    #   起→3→6→1→5→2 里，5→2 这段横穿内圈，弦离环心只有 **13.8m**
    #   （环半径 ~62m，比值 0.39×），画出来中间塌一块，不像椭圆。
    #   改成 end=start（真正的闭环最短）后该组变成 起→3→6→1→2→5→起，
    #   最近弦 33.3m（0.93×），与其余 5 组（0.68~0.95×）齐平；
    #   且其余 5 组的访问顺序**完全不变** —— 零回归。
    _tour_len, seq = _tsp_min_legs(start, list(waypoints), start)
    ring = list(seq)
    # end=起点 时 seq 末尾是重复的起点：去掉，否则闭合样条会多出一个零长段
    if len(ring) >= 2 and haversine(ring[0][0], ring[0][1],
                                    ring[-1][0], ring[-1][1]) < 1e-6:
        ring.pop()
    if len(ring) < 3:
        return [start] + list(waypoints) + [start]

    # ★★ 2026-09-29 打卡点顶点加「微小偏移」：不再次次精确穿过圆心。
    #   ring[0] 是起点（落在环上，由 `swmode._pick_anchor` 决定），保持不动。
    ring = _jitter_points(ring, rng)

    # 2. 闭合样条: 圆润的环, 通过全部打卡点
    dense = catmull_rom(ring, samples_per_seg=16, closed=True)
    rp = _open_ring(dense)
    if len(rp) < 3:
        return [start] + list(waypoints) + [start]

    # 3. 旋转到起点附近开头
    rp = rotate_ring(rp, start)
    return rp + [rp[0]]


def _open_ring(pts: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    去掉闭合环末尾与首点重合的点, 返回"开路"顶点序列。
    `_build_base_loop` 返回的是 [p0, p1, ..., pn, p0] 形式。
    """
    pts = list(pts)
    if len(pts) >= 2:
        d = haversine(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
        if d < 1e-6:
            pts.pop()
    return pts


def rotate_ring(ring: Sequence[Tuple[float, float]],
                to_point: Tuple[float, float]) -> List[Tuple[float, float]]:
    """旋转闭合环 (开路顶点), 使离 to_point 最近的顶点成为第一个顶点。"""
    ring = list(ring)
    if len(ring) < 2:
        return ring
    k = nearest_index(ring, to_point)
    return ring[k:] + ring[:k]


def translate_xy(xy: Sequence[Tuple[float, float]],
                 dx: float, dy: float) -> List[Tuple[float, float]]:
    """整体平移平面点列"""
    return [(x + dx, y + dy) for x, y in xy]


def anchor_ring(path_ll: Sequence[Tuple[float, float]],
                anchor: Tuple[float, float]) -> List[Tuple[float, float]]:
    """
    ★ 把一条闭合环的"起点"钉到 anchor 上。

    做法:
        1. 找到环上离 anchor 最近的顶点 k
        2. 把环旋转成以 k 开头, 并把 k 平移到 anchor
        3. 闭合: 末尾补回 anchor

    这样几何长度不变 (纯刚体变换), 且首点严格等于 anchor,
    避免"强行改写首末点坐标"导致的 170m 隐形瞬移。
    """
    ring = _open_ring(path_ll)
    if len(ring) < 3:
        return list(path_ll)
    ring = rotate_ring(ring, anchor)
    la0, lo0 = ring[0]
    xy = _to_xy(la0, lo0, ring)
    ax, ay = _to_xy(anchor[0], anchor[1], [anchor])[0]
    # ring[0] 投影后是 (0,0), 需平移 (ax, ay)
    xy = translate_xy(xy, ax, ay)
    out = _to_ll(anchor[0], anchor[1], xy)
    out.append(out[0])
    return out


def _snap_to_targets(path_xy: Sequence[Tuple[float, float]],
                     targets_xy: Sequence[Tuple[float, float]],
                     closed: bool = True,
                     max_iter: int = 8,
                     tol: float = 2.5) -> List[Tuple[float, float]]:
    """
    ★ 迭代把路径"吸"向每个目标点, 直到路径上某点与目标距离 <= tol。

    为什么需要:
        Catmull-Rom 在尖角处会切角。当只有 2 个近乎对称的打卡点时,
        基础环退化成"橄榄形", 样条会把两侧切进去几十米, 打卡点就飘到
        半径之外了。逐个"拉回来"比单纯插控制点稳得多。

    做法:
        1. 找离目标最近的路径点 i
        2. 若距离 > tol, 以 i 为中心叠加一个高斯钟形位移场 (平滑, 不折角)

    注意:
        本操作必然改变弧长 (把曲线往外顶长、往里压短), 所以调用方必须
        在吸附之后重新做长度归一 —— 见 `_scale_about_anchors` 的交替循环。
    """
    path = [tuple(p) for p in path_xy]
    n = len(path)
    if n < 5 or not targets_xy:
        return path

    for _ in range(max_iter):
        worst = 0.0
        for t in targets_xy:
            bi, bd = 0, float("inf")
            for i, (x, y) in enumerate(path):
                d = math.hypot(x - t[0], y - t[1])
                if d < bd:
                    bd, bi = d, i
            worst = max(worst, bd)
            if bd <= tol or bd < 1e-9:
                continue
            need = bd - tol
            ux = (t[0] - path[bi][0]) / bd
            uy = (t[1] - path[bi][1]) / bd
            sig = max(2.0, (n - 1) / 12.0)
            m = n - 1 if closed else n
            newp = []
            for i, (x, y) in enumerate(path):
                if closed:
                    dd = min(abs(i - bi), m - abs(i - bi))
                else:
                    dd = abs(i - bi)
                w = math.exp(-(dd * dd) / (2.0 * sig * sig))
                newp.append((x + ux * need * w, y + uy * need * w))
            path = newp
        if worst <= tol:
            break

    return path



def _scale_about_anchors(path_xy: Sequence[Tuple[float, float]],
                         anchors_xy: Sequence[Tuple[float, float]],
                         target_len: float,
                         max_iter: int = 8,
                         tol: float = 1.5,
                         pin_first: bool = False,
                         ) -> List[Tuple[float, float]]:
    """
    ★ 缩放到目标长度, 但把"锚点"(打卡点) 钉住不动。

    纯 scale_xy 是围绕原点 (起点) 各向同性缩放, 会把打卡点从路径上推开
    (实测 3km 时推开 70+ 米)。本函数做法:
        1. 以所有锚点的质心为中心做缩放 (锚点漂移最小)
        2. 用 _snap_to_targets 把路径"吸"回锚点 —— 但这会缩短路径
        3. 于是再以锚点质心缩放把长度涨回去 —— 锚点纹丝不动
        4. 2/3 交替收敛: 既保长 (target_len) 又命中锚点

    为什么必须交替: 吸附操作本质是把曲线往内/外拉, 必然改变弧长;
    只有在"锚点质心"这个不动点上反复缩放, 才能让两个约束同时满足。

    :param pin_first: 为 True 时把 path[0] (起点) 也当作硬锚点 ——
        每轮缩放后平移回原位。闭合环必须这样, 否则最后只能靠
        `anchor_ring` 做刚体平移把起点拉回去, 而那会把打卡点一起拖走。
    """
    path = [tuple(p) for p in path_xy]
    if len(path) < 3:
        return path
    if not anchors_xy:
        return scale_xy(path, target_len)

    # ★ 把起点并入锚点集合: 让 "起点" 也参与吸附, 这样最后不需要
    #   任何刚体平移 (平移会破坏打卡点命中)。
    all_anchors = list(anchors_xy)
    if pin_first:
        all_anchors = all_anchors + [path[0]]

    ax = sum(a[0] for a in all_anchors) / len(all_anchors)
    ay = sum(a[1] for a in all_anchors) / len(all_anchors)

    def zoom(p, k_):
        return [(ax + (x - ax) * k_, ay + (y - ay) * k_) for x, y in p]

    # 初始: 以锚点质心缩放到目标长
    cur = _xy_len(path)
    if cur <= 1e-9:
        return path
    path = zoom(path, target_len / cur)

    best = path
    best_score = (float("inf"), 0.0)
    for _ in range(max_iter):
        # 吸附锚点
        path = _snap_to_targets(path, all_anchors, closed=True,
                                max_iter=4, tol=tol)
        # 再缩回目标长 (锚点质心不动)
        cur = _xy_len(path)
        if cur > 1e-9:
            path = zoom(path, target_len / cur)

        # 打分: 锚点最大偏差 + 长度偏差
        worst_a = max(min(math.hypot(x - t[0], y - t[1]) for x, y in path)
                      for t in all_anchors)
        len_err = abs(_xy_len(path) - target_len)
        score = (worst_a, len_err)
        if score < best_score:
            best_score = score
            best = path
        if worst_a <= tol and len_err / target_len < 0.005:
            break

    return best


def _scale_about_anchors_open(path_xy: Sequence[Tuple[float, float]],
                              anchors_xy: Sequence[Tuple[float, float]],
                              target_len: float,
                              max_iter: int = 10,
                              tol: float = 1.5,
                              pin_start: bool = False,
                              pin_end: Optional[Tuple[float, float]] = None,
                              ) -> List[Tuple[float, float]]:
    """
    开口路径版本 (折返 / 点到点): 缩放到目标长 + 吸附锚点交替收敛。

    与闭合版的区别:
        · 首点 (起点) 是硬约束 —— `pin_start=True` 时并入锚点集合,
          每轮缩放后平移回原位
        · 末点若是 `pin_end`, 同样是硬约束, 通过把残差按幂曲线摊到
          全程来归位 (末点严格不动, 起点不受影响)

    为什么必须把首末点并入锚点:
        只钉打卡点的话, 缩放会把起点/终点推走; 之后再用刚体平移拉回,
        又会把已经命中的打卡点一起拖偏 —— 两个约束互相打架。
        统一放进同一个"锚点集合"里一起吸附, 才能同时满足。
    """
    path = [tuple(p) for p in path_xy]
    if len(path) < 3:
        return path
    if not anchors_xy:
        return _scale_xy_keep_ends(path, target_len, pin_end)

    # 锚点集合 = 打卡点 (+ 起点 / 终点)
    hard = list(anchors_xy)
    if pin_start:
        hard = hard + [path[0]]
    if pin_end is not None:
        hard = hard + [tuple(pin_end)]

    ax = sum(a[0] for a in hard) / len(hard)
    ay = sum(a[1] for a in hard) / len(hard)

    p0 = path[0]
    pN = tuple(pin_end) if pin_end is not None else None

    def zoom(p, k_):
        return [(ax + (x - ax) * k_, ay + (y - ay) * k_) for x, y in p]

    def reanchor(p):
        """把首点(和末点)平移回原位, 残差用幂曲线摊到全程"""
        if p0 is not None:
            d0x = p0[0] - p[0][0]
            d0y = p0[1] - p[0][1]
            p = [(x + d0x, y + d0y) for x, y in p]
        if pN is not None:
            rx, ry = pN[0] - p[-1][0], pN[1] - p[-1][1]
            if abs(rx) > 1e-12 or abs(ry) > 1e-12:
                k = len(p)
                pw = 2.2
                p = [(x + rx * (i / (k - 1)) ** pw,
                      y + ry * (i / (k - 1)) ** pw)
                     for i, (x, y) in enumerate(p)]
        return p

    cur = _xy_len(path)
    if cur <= 1e-9:
        return path
    path = reanchor(zoom(path, target_len / cur))

    best, best_score = path, (float("inf"), 0.0)
    for _ in range(max_iter):
        path = _snap_to_targets(path, hard, closed=False, max_iter=4, tol=tol)
        cur = _xy_len(path)
        if cur > 1e-9:
            path = reanchor(zoom(path, target_len / cur))
        else:
            path = reanchor(path)

        worst_a = max(min(math.hypot(x - t[0], y - t[1]) for x, y in path)
                      for t in hard)
        len_err = abs(_xy_len(path) - target_len)
        score = (worst_a, len_err)
        if score < best_score:
            best_score, best = score, path
        if worst_a <= tol and len_err / target_len < 0.005:
            break
    return best




def _repeat_to_length(base: Sequence[Tuple[float, float]],
                      target_len: float,
                      rng: random.Random,
                      lateral_m: float = LAP_LATERAL_M,
                      waypoints: Sequence[Tuple[float, float]] = (),
                      anchor: Optional[Tuple[float, float]] = None,
                      rescale: bool = True,
                      ) -> Tuple[List[Tuple[float, float]], int]:
    """
    把基础环重复若干圈, 使总长接近 target_len。

    策略:
        1. 单圈长度 one_len (开路顶点 + 闭合边)
        2. laps = round(target / one_len), 至少 1
        3. ★ 缩放中心取 "打卡点质心 + 起点" 的混合点, 而不是纯打卡点质心:
           纯质心缩放会把**起点**推离 anchor (实测 44m), 之后只能靠
           刚体平移把起点拉回去 —— 而平移会把打卡点一起拖走, 打卡全废。
        4. 逐圈拼接, 圈间轻微侧偏 (更像真人跑), 接缝点不重复

    :param rescale: 是否在拼接前把 `base` 缩放到 `ideal_one`。
        ★ 跑道形状（`track`）传 **False** —— 它要求「每圈精确重放同一条
        跑道曲线」，任何预缩放都会让曲线脱离拟合出的几何；长度改由
        调用方 `_plan_track_loop` 的**纯缩放**一次性解决。

    返回 (闭环顶点列表, 圈数)
    """
    base = _open_ring(base)
    if len(base) < 3:
        return list(base) + [base[0]], 1

    one_len = polyline_length(list(base) + [base[0]])
    if one_len <= 1e-9:
        return list(base) + [base[0]], 1

    # 选圈数: 目标是让"每圈长度"尽量接近原始单圈长 (缩放比 ≈ 1),
    # 这样几何形状和打卡点位置几乎不动, 是数值上最稳的做法。
    # 多个候选时倾向圈数少一点 (轨迹更简洁), 但不能牺牲缩放比。
    def _pick_laps():
        cands = []
        for L in range(1, 101):
            per = target_len / L
            r = per / one_len
            if 0.75 <= r <= 1.35:                # 缩放幅度小, 形状安全
                cands.append((abs(math.log(r)), -L, L))
        if cands:
            cands.sort()
            return cands[0][2]
        # 没有理想圈数: 退而求其次, 选缩放比最接近 1 的
        best, best_cost = 1, float("inf")
        for L in range(1, 101):
            r = (target_len / L) / one_len
            if r < 0.55 or r > 3.0:
                continue
            cost = abs(math.log(r))
            if cost < best_cost:
                best_cost, best = cost, L
        return best

    laps = _pick_laps()
    ideal_one = target_len / laps
    ratio = ideal_one / one_len

    if rescale and 0.55 <= ratio <= 3.0:
        # ★ 缩放中心: 必须同时兼顾 "打卡点不乱跑" 和 "起点不乱跑"。
        #   用起点 + 打卡点质心的加权平均作为缩放中心, 并且缩放后
        #   把整条环平移回 anchor —— 这样两者都能回到原位附近。
        if waypoints:
            gx = sum(w[0] for w in waypoints) / len(waypoints)
            gy = sum(w[1] for w in waypoints) / len(waypoints)
        else:
            gx = sum(p[0] for p in base) / len(base)
            gy = sum(p[1] for p in base) / len(base)
        if anchor is not None:
            # 起点权重与打卡点同权 (几何上这就是"绕起点和打卡点一起缩放")
            k = 1.0 / (len(waypoints) + 1.0) if waypoints else 0.0
            zcx = gx + (anchor[0] - gx) * k
            zcy = gy + (anchor[1] - gy) * k
        else:
            zcx, zcy = gx, gy
        base = [(zcx + (la - zcx) * ratio, zcy + (lo - zcy) * ratio)
                for la, lo in base]
        # ★ 缩放后把环整体平移, 让第一个顶点回到 anchor
        if anchor is not None:
            d_la = anchor[0] - base[0][0]
            d_lo = anchor[1] - base[0][1]
            base = [(la + d_la, lo + d_lo) for la, lo in base]
    elif rescale and anchor is not None:
        base = scale_to_length(base, ideal_one)

    # 环中心 (用于计算"向外偏移"方向)
    #   ★ 命名纠正（2026-09-29）：原为 `cy`/`cx`，实际存的是 lat/lon，
    #     极易与「中心」语义混淆 —— 下文 `bearing(c_lat, c_lon, ...)` 才正确。
    c_lat = sum(p[0] for p in base) / len(base)
    c_lon = sum(p[1] for p in base) / len(base)

    ctrl: List[Tuple[float, float]] = []
    # ★★ 圈间侧偏：必须**小且围绕基准线摆动**（2026-09-27 修）
    #   旧实现 `off = amp*(1+0.5*min(lap,4))` 是**单调向外**的：
    #     amp=4.0 时第 2/3/4/5 圈分别外推 6/8/10/12 m →
    #     5 圈变成 5 个同心椭圆，圈质心最大漂移 **24 m**（实测），
    #     App 上就是「一团锯齿的小圈」而不是干净的跑道椭圆。
    #   ★ 真实跑道：跑者基本沿同一条线，换道也只有道宽量级（1.22 m）。
    #   所以改为**围绕基准线的小幅准随机摆动**，累计漂移有界（≤ 2·amp）。
    #   用黄金角序列而非 rng：确定性、且**不消耗随机数流**
    #   （消耗会改变既有种子的产出，让回归基线整体漂移）。
    amp = min(max(lateral_m, 0.0), LAP_LATERAL_M)
    for lap in range(laps):
        if lap == 0 or amp <= 1e-9:
            off = 0.0
        else:
            off = amp * math.sin(lap * 2.399963229728653)   # 黄金角 → 准均匀
        for i, (la, lo) in enumerate(base):
            if lap > 0 and i == 0:
                continue                    # 接缝点不重复
            if off <= 1e-9:
                ctrl.append((la, lo))
            else:
                # ★ 打卡点附近不偏移（保证每一圈都能打到卡），
                #   并做**平滑过渡**：硬切会让圈在 d_cp≈12m 处出现折角，
                #   那也是「锯齿」的来源之一。
                if waypoints:
                    d_cp = min(haversine(la, lo, w[0], w[1])
                               for w in waypoints)
                else:
                    d_cp = float("inf")
                if d_cp < 12.0:
                    off_i = 0.0
                elif d_cp < 25.0:
                    off_i = off * (d_cp - 12.0) / 13.0
                else:
                    off_i = off
                if off_i <= 1e-9:
                    ctrl.append((la, lo))
                else:
                    brg = bearing(c_lat, c_lon, la, lo)
                    ctrl.append(dest_point(la, lo, brg, off_i))

    ctrl.append(ctrl[0])                    # 闭合
    return ctrl, laps


# ============================================================
# ★★ 跑道形状（两个半圆 + 两条直道）专用闭环流程
# ============================================================
def _track_zoom(path_xy: Sequence[Tuple[float, float]],
                targets_xy: Sequence[Tuple[float, float]],
                target_len: float) -> List[Tuple[float, float]]:
    """跑道专用「纯缩放」：以「打卡点质心 + 起点」加权中心缩放，再把首点钉回原位。

    ★ 为什么不用 `_scale_about_anchors`（2026-09-29 实测定因）：
      它内部的 `_snap_to_targets` 用**半径 ≈ 0.42 圈**的高斯场把曲线往锚点拽
      （`sig = (n-1)/12` 点）。对椭圆无害 —— 椭圆本来就过打卡点，`need ≈ 0`，
      几乎不拉；对跑道却是纯破坏：实测直道残差 **0.04 → 11.06 m**、
      半圆半径离散 **0.05 → 3.74 m**，画出来就是「弯的直道」（用户肉眼可见）。
      而跑道曲线**本来就经过所有打卡点**（拟合误差仅 0.04~2.28 m），
      根本不需要吸附 —— 纯缩放后打卡点最差仅 **4.28 m**（阈值 20 m）。
    """
    if not path_xy or len(path_xy) < 3:
        return list(path_xy)
    cur = _xy_len(path_xy)
    if cur <= 1e-9:
        return list(path_xy)
    k = target_len / cur
    if targets_xy:
        gx = sum(t[0] for t in targets_xy) / len(targets_xy)
        gy = sum(t[1] for t in targets_xy) / len(targets_xy)
        # 起点与打卡点同权 —— 纯质心缩放会把起点推离 anchor（实测 44 m）
        kk = 1.0 / (len(targets_xy) + 1.0)
        zx = gx + (path_xy[0][0] - gx) * kk
        zy = gy + (path_xy[0][1] - gy) * kk
    else:
        zx, zy = path_xy[0]
    out = [(zx + (x - zx) * k, zy + (y - zy) * k) for x, y in path_xy]
    # 缩放会让首点漂移 ⇒ 刚体平移钉回（平移量小，对打卡点影响 ≤ 数米）
    dx = path_xy[0][0] - out[0][0]
    dy = path_xy[0][1] - out[0][1]
    return [(x + dx, y + dy) for x, y in out]


def _plan_track_loop(start: Tuple[float, float],
                     waypoints: Sequence[Tuple[float, float]],
                     target_len: float,
                     rng: random.Random,
                     straight_m: float = None,
                     noise_sigma_m: float = 0.0,
                     lateral_m: float = LAP_LATERAL_M,
                     ) -> Tuple[List[Tuple[float, float]], int]:
    """跑道形状的专用闭环规划：**精确重放跑道环 N 圈 + 纯缩放**。

    与通用管线的区别：不做 Catmull-Rom 样条、不做锚点吸附（见 `_track_zoom`），
    因此全程保持「两个半圆 + 两条直道」的几何，打卡点靠「曲线本身过点」保证。

    流程：起点投到跑道曲线 → 拟合跑道环 → 重放 N 圈（含圈间侧偏）
          → 纯缩放到目标长 → 钉起点。

    ★★ 为什么默认 `noise_sigma_m=0`（2026-09-29 实测定因）：
      跑道环的点间距只有 **0.17 m**（2 m 一个控制点，闭合环 208 点/圈），
      而 `_add_tangential_noise` 的 σ = 1.6 m 比点间距**大一个数量级** ⇒
      相邻点被独立推来推去、曲线锯齿化，**折线长度虚增 31.7%**
      （实测 2000.0 → 2634.1 m，而外接矩形几乎不变）。
      随后「噪声后再归一化」拿这个被污染的长度去缩 ⇒ **整体缩小 24%**
      （实测 OBB 158×74 → 121×57，打卡点偏 27 m，越过 20 m 阈值）。
      椭圆形状之所以没炸，是因为它的锚点吸附会把曲线重新拉回打卡点，
      把长度污染掩盖掉了 —— 跑道没有吸附，于是暴露。
      ⇒ 米级噪声必须加在「点间距 ≳ 噪声」的轨迹上；本流程点太密，不加。
      真机观感验证：跑道轨迹本就沿跑道规整，无抖动不影响真实感。
    """
    sm = straight_m if straight_m is not None else TRACK_STRAIGHT_M

    # ★★ 起点「接入直线」（尾巴）：环本体只跑到 `target_len - 2*tail`，
    #   因为尾巴在闭环里走两趟（出 + 回）。见 `TRACK_START_TAIL_M`。
    #   ★ 回程那趟随后被 `open_loop_tail` 吃掉，所以**不会**在图上重叠。
    tail_m = max(0.0, TRACK_START_TAIL_M)
    ring_target = max(50.0, target_len - 2.0 * tail_m)

    st = _stadium_nearest(start, waypoints, straight_m=sm)
    ring = _stadium_ring(st, waypoints, straight_m=sm)
    ctrl, laps = _repeat_to_length(ring, ring_target, rng, lateral_m=lateral_m,
                                   waypoints=waypoints, anchor=st, rescale=False)

    la0, lo0 = ctrl[0]
    xy = _track_zoom(_to_xy(la0, lo0, ctrl),
                     _to_xy(la0, lo0, list(waypoints)), ring_target)
    ll = _to_ll(la0, lo0, xy)

    if noise_sigma_m > 0:
        # 仅在调用方显式要求时启用；见上文，点间距 0.17 m 下必须用极小值
        step = ring_target / max(1, len(ll))
        sig = min(float(noise_sigma_m), 0.3 * step)
        ll = _add_tangential_noise(ll, sig, rng, closed=True)
        la1, lo1 = ll[0]
        ll = _to_ll(la1, lo1, _track_zoom(_to_xy(la1, lo1, ll),
                                          _to_xy(la1, lo1, list(waypoints)),
                                          ring_target))

    # 起点硬锚定（刚体平移；跑道下首点本就在曲线上，平移量 ≈ 0）
    ll = anchor_ring(ll, st)

    # ★ 接尾巴：起点落到环外，再沿直线接入跑道（2026-10-08 用户要求）
    ll = _attach_start_tail(ll, st, tail_m, waypoints, straight_m=sm)
    return ll, laps


# ============================================================
# 主入口
# ============================================================
def plan_route(start: Tuple[float, float],
               waypoints: Sequence[Tuple[float, float]],
               target_len: float,
               mode: str = RouteMode.LOOP,
               end: Optional[Tuple[float, float]] = None,
               rng: Optional[random.Random] = None,
               noise_sigma_m: float = 1.6,
               bulge: float = 1.35,
               shape: str = SHAPE_TRACK,
               straight_m: float = None,
               ) -> Tuple[List[Tuple[float, float]], int]:
    """
    规划完整路线。

    ★ 关键: 全程在"平面米坐标"下做长度控制, 最后一步才投影回经纬度。
      顺序: 控制点 -> (缩放到目标长) -> 样条 -> (再缩放到目标长) -> 锚定 -> 噪声

    返回: (经纬度点列, 圈数)
    """
    rng = rng or random.Random()
    start = (float(start[0]), float(start[1]))
    waypoints = [(float(a), float(b)) for a, b in waypoints]
    closed = (mode == RouteMode.LOOP)

    # ★★ 跑道形状：起点必须落在跑道曲线上，否则 LOOP 会"从环外进出一次"
    #   ⇒ 多一根引线 + 外接矩形被撑宽（实测起点偏 44.6 m）。见 `_stadium_nearest`。
    if closed and waypoints and shape == SHAPE_TRACK:
        start = _stadium_nearest(
            start, waypoints,
            straight_m=(straight_m if straight_m is not None else TRACK_STRAIGHT_M))

    # ---------- 1. 构建控制点 ----------
    pin_end: Optional[Tuple[float, float]] = None
    if mode == RouteMode.POINT2POINT:
        end_pt = (float(end[0]), float(end[1])) if end else (
            waypoints[-1] if waypoints else start)
        # ★ 退化保护: 没有打卡点, 且终点与起点基本重合 / 未指定
        #   -> 无法构成一条有意义的点到点路线, 退回环线
        degenerate = (haversine(start[0], start[1], end_pt[0], end_pt[1]) < 50.0
                      and not waypoints)
        if degenerate:
            base = _build_base_loop(start, waypoints, rng, bulge=bulge,
                                    shape=shape, straight_m=straight_m)
            ctrl, laps = _repeat_to_length(base, target_len, rng, lateral_m=0.0,
                                           anchor=start)
            mode = RouteMode.LOOP
            closed = True
        else:
            # ★ 用 TSP 最优顺序, 不要用贪心最近邻:
            #   贪心会产生交叉往返, 实测 3 个打卡点就绕出 2045m, 而
            #   最优顺序只要 1448m —— 多出来的长度只能靠"压缩"消掉,
            #   而压缩必然把打卡点推开 (实测 68m)。
            tsp_len, ordered = _tsp_min_legs(start, waypoints, end_pt)
            mid = ordered[1:-1] if len(ordered) > 2 else []
            ctrl = [start] + mid + [end_pt]
            laps = 1
            pin_end = end_pt
            ctrl, laps, pin_end = _repeat_open_to_length(
                ctrl, target_len, pin_end)

    elif mode == RouteMode.OUT_AND_BACK:
        # ★ 折返: 起点 -> 沿途打卡点 -> 最远点, 然后原路返回起点。
        #   多趟时 "去-回" 算一趟, 趟间做轻微侧偏。
        #   同样用 TSP 顺序 (贪心的交叉往返会让单程凭空多出 40%)
        _one_len, ordered = _tsp_min_legs(start, waypoints, None)
        oneway = list(ordered)
        if len(oneway) < 2:
            # 没有打卡点: 沿随机方向往返
            ang = rng.uniform(0, 360)
            half = max(60.0, target_len / 4.0)
            turn = dest_point(start[0], start[1], ang, half)
            oneway = [start, turn]

        one_len = polyline_length(oneway) * 2.0        # 去 + 回
        # ★ 取"不超过 target 的最大趟数", 绝不超额 —— 超额就意味着后面
        #   必须做压缩, 而压缩会把打卡点从路上拽走 (实测 11% 压缩就让
        #   最后两个打卡点漂到 35m, 刚好越过 30m 半径)。
        #   零头用"末段来回"补: 在最后一趟的折返点外再加一小段往返。
        laps = (max(1, int(math.floor(target_len / one_len)))
                if one_len > 1e-9 else 1)
        laps = max(1, laps)

        ctrl_list: List[Tuple[float, float]] = []
        for L in range(laps):
            off = 0.0 if L == 0 else min(5.0, 18.0 / max(1.0, laps))
            going = [start] + oneway[1:]
            back = list(reversed(oneway))              # 回到起点
            if L % 2 == 1:
                # 奇数趟: 反向 (从最远点出发), 让接缝自然
                going, back = back, list(reversed(back))
            part = going + back[1:]
            if ctrl_list:
                part = part[1:]                        # 接缝点不重复
            if off > 1e-9 and waypoints:
                # 侧偏: 离打卡点近的不偏
                adj = []
                for (la, lo) in part:
                    d_cp = min(haversine(la, lo, w[0], w[1]) for w in waypoints)
                    if d_cp < 12.0:
                        adj.append((la, lo))
                    else:
                        brg = bearing(start[0], start[1], la, lo)
                        adj.append(dest_point(la, lo, (brg + 90) % 360, off))
                part = adj
            ctrl_list.extend(part)

        ctrl = ctrl_list
        closed = False
        pin_end = None                                  # 折返自然回到起点
        if ctrl:
            ctrl.append(start)

        # ---------- 零头补偿: 在最后一趟的折返点外再加一段往返 ----------
        #   ctrl 末尾是 start; 其前一段是"回到起点"的收尾腿。真正适合加
        #   零头的位置是**最远折返点** (即一趟的中点), 在那儿向外再走
        #   h 米往返, 可精确补上 delta = target - 当前长。
        cur_len = polyline_length(ctrl)
        delta = target_len - cur_len
        if delta > 1.0 and len(oneway) >= 2 and waypoints:
            far = None
            far_d = -1.0
            for p in oneway:
                d = haversine(start[0], start[1], p[0], p[1])
                if d > far_d:
                    far_d, far = d, p
            if far is not None and far_d > 1.0:
                # 从折返点继续沿原方向外推 h/2 再折回 => 增加约 h
                brg_out = bearing(start[0], start[1], far[0], far[1])
                h_out = delta / 2.0
                tip = dest_point(far[0], far[1], brg_out, h_out)
                # 找到 ctrl 中"最远折返点"对应的下标 (取离 far 最近的)
                bi, bd = 0, float("inf")
                for n, p in enumerate(ctrl):
                    dd = haversine(p[0], p[1], far[0], far[1])
                    if dd < bd:
                        bd, bi = dd, n
                if bd < 25.0:
                    # 在折返点之后插入 tip 再回到折返点 (即"多跑一小段")
                    ctrl = (ctrl[:bi + 1] + [tip, ctrl[bi]]
                            + ctrl[bi + 1:])

    else:
        # ★★ 跑道形状：走专用闭环流程（精确重放 + 纯缩放），
        #   绕开通用管线的 Catmull-Rom 样条与锚点吸附 —— 后者的
        #   「半圈高斯吸附」会把拟合好的跑道拉弯（直道残差 0.04 → 11.06 m）。
        #   见 `_plan_track_loop` / `_track_zoom`。
        if shape == SHAPE_TRACK and waypoints:
            return _plan_track_loop(start, waypoints, target_len, rng,
                                    straight_m=straight_m)
        base = _build_base_loop(start, waypoints, rng, bulge=bulge,
                                shape=shape, straight_m=straight_m)
        ctrl, laps = _repeat_to_length(
            base, target_len, rng,
            lateral_m=(0.0 if len(waypoints) == 0 else LAP_LATERAL_M),
            waypoints=waypoints, anchor=start)

    # ---------- 2. 平面坐标 (以 ctrl[0] 为投影原点) ----------
    la0, lo0 = ctrl[0]
    targets_xy = _to_xy(la0, lo0, list(waypoints))
    cxy = _to_xy(la0, lo0, ctrl)
    if pin_end is not None:
        exy = _to_xy(la0, lo0, [pin_end])[0]

    # ---------- 3. 控制点层面缩放 (锚点不动) ----------
    if len(cxy) >= 2:
        if waypoints and closed:
            cxy = _scale_about_anchors(cxy, targets_xy, target_len,
                                       max_iter=3, tol=2.0, pin_first=True)
        elif waypoints:
            # ★ 开口路径带打卡点: 同样要"锚点不动"地缩放。
            #   早期这里是裸 scale_xy (以起点为不动点), 会把沿途打卡点
            #   推开 45m; 之后 stage5 的末点残差又是 3 次曲线, 在末端
            #   附近拉力极大 —— 最末一个打卡点正好在那儿, 直接被拉废。
            cxy = _scale_about_anchors_open(cxy, targets_xy, target_len,
                                            max_iter=4, tol=2.0,
                                            pin_start=True)
        else:
            cxy = _scale_xy_keep_ends(cxy, target_len,
                                      exy if pin_end is not None else None)

    # ---------- 4. 样条平滑 ----------
    dense = catmull_rom(cxy, samples_per_seg=12, closed=closed)

    # ---------- 5. 开口路径: 在 XY 下把末点对齐到终点 ----------
    if (not closed) and pin_end is not None:
        rem_x = exy[0] - dense[-1][0]
        rem_y = exy[1] - dense[-1][1]
        k = len(dense)
        if abs(rem_x) > 1e-9 or abs(rem_y) > 1e-9:
            # ★ 指数从 3 降到 2.2: 3 次曲线在末端拉力过猛, 会把靠后的
            #   打卡点整段拽偏; 2.2 次仍然"末端修正最强、起点不动",
            #   但影响范围更集中, 对中后段几何更友好。
            dense = [(x + rem_x * (i / (k - 1)) ** 2.2,
                      y + rem_y * (i / (k - 1)) ** 2.2)
                     for i, (x, y) in enumerate(dense)]

    # ---------- 6. 精确长度归一 + 锚点吸附 ----------
    if waypoints and closed:
        # 锚点不动地缩放到目标长, 并交替吸附直到打卡点必中
        dense = _scale_about_anchors(dense, targets_xy, target_len,
                                     max_iter=6, tol=1.5, pin_first=True)
    else:
        # 开口路径 (折返 / 点到点): 同样"缩放到目标长 + 吸附锚点"交替
        if waypoints:
            dense = _scale_about_anchors_open(dense, targets_xy, target_len,
                                              max_iter=10, tol=1.5,
                                              pin_start=True,
                                              pin_end=exy if pin_end is not None else None)
        else:
            # ★ 无打卡点的开口路径: 不能直接 scale_xy —— 那是以 (0,0)
            #   即起点为不动点的缩放, 会把**末点**从终点拽走。
            dense = _scale_open_keep_ends(dense, target_len)

    # ---------- 7. 投影回经纬度 (dense 在 la0/lo0 为原点的平面米坐标下) ----------
    ll = _to_ll(la0, lo0, dense)

    # ---------- 8. 锚定起终点 (刚体平移, 不引入隐形瞬移) ----------
    if closed:
        # ★ 只有当起点确实偏离时才做刚体平移 —— 平移会把打卡点一起
        #   拖走, 所以前面 `pin_first=True` 已经尽量让起点自己回到位。
        off = haversine(ll[0][0], ll[0][1], start[0], start[1])
        if ll[0] != start or off > 0.5:
            ll = anchor_ring(ll, start)
    else:
        # 首点严格等于 start
        d_la = start[0] - ll[0][0]
        d_lo = start[1] - ll[0][1]
        ll = [(a + d_la, b + d_lo) for a, b in ll]

    # ---------- 9. GPS 漂移噪声 ----------
    # ★ 噪声必须"沿路径切向"而不是各向同性:
    #   折返 / 多圈路线的相邻点是反向的, 各向同性噪声会在每次折返处
    #   凭空拉出一个横向偏移, 让弧长暴涨 (实测 3000m 变 3146m, +5%);
    #   而切向噪声只轻微改变点间距, 不破坏几何走向。
    #   同时末点 (以及闭合环的首点) 保持不动。
    noisy = _add_tangential_noise(ll, noise_sigma_m, rng, closed=closed)

    # ---------- 10. 噪声之后再做一次长度归一 (切向噪声会微小改变弧长) ----------
    if len(noisy) > 3:
        la0b, lo0b = noisy[0]
        xy = _to_xy(la0b, lo0b, noisy)
        if closed:
            xy = _scale_about_anchors(xy, targets_xy, target_len,
                                      max_iter=3, tol=2.5, pin_first=True)
        else:
            xy = _scale_xy_keep_ends(xy, target_len,
                                     exy if pin_end is not None else None)
        noisy = _to_ll(la0b, lo0b, xy)
        # 重新钉住首点 (上面以 noisy[0] 为投影原点, 首点应恰好不变)
        if not closed:
            d_la = start[0] - noisy[0][0]
            d_lo = start[1] - noisy[0][1]
            noisy = [(a + d_la, b + d_lo) for a, b in noisy]

    return noisy, laps


def _add_tangential_noise(path: Sequence[Tuple[float, float]],
                          sigma_m: float,
                          rng: random.Random,
                          closed: bool = False,
                          ) -> List[Tuple[float, float]]:
    """
    沿路径切向叠加高斯噪声 (而不是各向同性抖动)。

    为什么:
        各向同性噪声在折返/多圈路线上会在每个折返点制造横向偏移,
        使弧长系统性偏大 (实测 +5%); 切向噪声只改点间距, 不改变
        走向, 既保留了 GPS 抖动的观感, 又不会污染总距离。

        另外叠加少量横向微扰 (sigma 的 25%), 保持轨迹自然。
    """
    pts = [tuple(p) for p in path]
    n = len(pts)
    if n < 3 or sigma_m <= 0:
        return pts

    out: List[Tuple[float, float]] = []
    for i, (la, lo) in enumerate(pts):
        if i == 0 or (not closed and i == n - 1):
            out.append((la, lo))
            continue
        # 切向: 用前后点连线方向 (闭合环首尾相接)
        j = (i + 1) % n
        k = (i - 1) % n
        if closed and i == n - 1:
            j = 0
        d = haversine(pts[k][0], pts[k][1], pts[j][0], pts[j][1])
        if d < 1e-6:
            out.append((la, lo))
            continue
        brg = bearing(pts[k][0], pts[k][1], pts[j][0], pts[j][1])
        tang = rng.gauss(0.0, sigma_m)
        lat = rng.gauss(0.0, sigma_m * 0.25)
        p = dest_point(la, lo, brg, tang)
        p = dest_point(p[0], p[1], (brg + 90.0) % 360.0, lat)
        out.append(p)

    # 闭合环保持首末重合
    if closed and out:
        out.append(out[0])
    return out


def add_sample_noise(path: Sequence[Tuple[float, float]],
                     sigma_m: float,
                     rng: random.Random,
                     ) -> List[Tuple[float, float]]:
    """
    ★ 在**最终采样点**上叠加 GPS 抖动 (2026-09-25)。

    为什么必须在这一层加, 而不是在 `plan_route` 的几何上:

        几何是**每 ~2 米一个点** (catmull_rom samples_per_seg=12 铺出来的),
        而最终记录只有 n 个点 (~15 米一个)。在 2 米间距上叠 1.6 米的抖动,
        折线弧长被**虚增 29.5%** (实测: 弦长 2050m, 平滑后真实路线只有
        1583.6m)。下游 `_align_geometry` 重采样到 136 点后把噪声平均掉,
        长度回到真实的 ~1554m, 于是 `_correct_length` 必须**放大 1.32×**
        才能凑到目标距离 —— 而"整体放大"会把打卡点沿径向推离路线:
        离环心最远的那个点被推出去 13m, 越过 App 自己的打卡半径 (15m),
        手机端地图上就是"根本没经过打卡点"; 同时形状被撑成带尖刺的乱麻。

        把抖动挪到最终采样点上, 同样 1.6m 的幅度只让弧长虚增 ~1%,
        标定的放大倍数从 1.32 降到 ~1.01, 打卡点几乎不动。

    约定:
        · 首点严格不动 (它就是起点, 也是闭环的收口点)
        · 闭合路径 (首末重合) 的末点跟随首点, 保证精确闭合
        · 切向为主 + 25% 横向微扰 —— 与 `_add_tangential_noise` 同口径,
          避免各向同性噪声在折返处凭空拉长弧长
    """
    pts = [tuple(p) for p in path]
    n = len(pts)
    if n < 4 or sigma_m <= 0:
        return pts

    closed = haversine(pts[0][0], pts[0][1],
                       pts[-1][0], pts[-1][1]) < 1e-6
    out = list(pts)
    for i in range(1, n - 1):
        la, lo = out[i]
        a, b = out[i - 1], out[i + 1]
        d = haversine(a[0], a[1], b[0], b[1])
        if d < 1e-9:
            continue
        brg = bearing(a[0], a[1], b[0], b[1])
        p = dest_point(la, lo, brg, rng.gauss(0.0, sigma_m))
        out[i] = dest_point(p[0], p[1], (brg + 90.0) % 360.0,
                            rng.gauss(0.0, sigma_m * 0.25))
    if closed:
        out[-1] = out[0]
    return out


def _xy_len(path_xy: Sequence[Tuple[float, float]]) -> float:
    return sum(math.hypot(path_xy[i][0] - path_xy[i - 1][0],
                          path_xy[i][1] - path_xy[i - 1][1])
               for i in range(1, len(path_xy)))




def _scale_open_keep_ends(path_xy: Sequence[Tuple[float, float]],
                          target_len: float,
                          max_iter: int = 12,
                          tol: float = 1.0) -> List[Tuple[float, float]]:
    """
    开口路径 (无打卡点) 的长度归一, 同时把**首末点**钉住。
    (等价于 `_scale_xy_keep_ends(p, target_len, pin_end=p[-1])`)

    为什么不能简单 `scale_xy`:
        `scale_xy` 的不动点是 path[0] (原点), 缩放会把末点从终点
        拽走上百米 —— 点到点模式就废了。
    """
    return _scale_xy_keep_ends(path_xy, target_len, pin_end=None, max_iter=max_iter)


def _scale_xy_keep_ends(path_xy: Sequence[Tuple[float, float]],
                        target_len: float,
                        pin_end: Optional[Tuple[float, float]] = None,
                        max_iter: int = 12) -> List[Tuple[float, float]]:
    """
    开口路径长度归一的通用实现。

    :param pin_end: 末点应落在的位置; 为 None 时取 `path[-1]` (即末点原地不动)。

    做法:
        1. 以两端点连线的中点为缩放中心 (首末点漂移最小)
        2. 缩放到目标长后, 把首点平移回原位
        3. 末点残差按 2.2 次幂曲线摊到全程 (末点严格不动, 首点不受影响)
        4. 迭代 2/3 直到长度收敛

    注意: 末点用幂曲线归位而不是刚体平移, 是为了避免把中段几何整体
    拖偏 —— 中段可能正好有打卡点。
    """
    path = [tuple(p) for p in path_xy]
    if len(path) < 3:
        return path

    p0 = path[0]
    pN = tuple(pin_end) if pin_end is not None else path[-1]
    cx = (p0[0] + pN[0]) * 0.5
    cy = (p0[1] + pN[1]) * 0.5

    def zoom(p, k_):
        return [(cx + (x - cx) * k_, cy + (y - cy) * k_) for x, y in p]

    for _ in range(max_iter):
        cur = _xy_len(path)
        if cur <= 1e-9:
            break
        need = target_len / cur
        if abs(need - 1.0) < 1e-5:
            break
        path = zoom(path, need)
        # 首点回位
        d0x = p0[0] - path[0][0]
        d0y = p0[1] - path[0][1]
        path = [(x + d0x, y + d0y) for x, y in path]
        # 末点回位: 残差按 2.2 次幂摊到全程 (末点严格不动)
        rx = pN[0] - path[-1][0]
        ry = pN[1] - path[-1][1]
        if abs(rx) > 1e-12 or abs(ry) > 1e-12:
            k = len(path)
            path = [(x + rx * (i / (k - 1)) ** 2.2, y + ry * (i / (k - 1)) ** 2.2)
                    for i, (x, y) in enumerate(path)]

    return path


def _repeat_open_to_length(path: Sequence[Tuple[float, float]],
                           target_len: float,
                           pin_end: Optional[Tuple[float, float]],
                           ) -> Tuple[List[Tuple[float, float]], int, Optional[Tuple[float, float]]]:
    """
    开口路径长度不足时, 用"正向 + 反向交替拼接"来加长 (而不是硬拉伸)。

    硬拉伸会把沿途的打卡点甩出几十米; 来回折返则完全保持几何不变,
    只是把同一条路线走了多趟 —— 这也是真实跑者在短距离打卡点圈里常干的事。

    ★ 终点落位规则 (踩过的坑)
        path = [start ... end_pt], 一趟 "正向" 的末点是 end_pt,
        "反向" 的末点是 start。因此:
            · 奇数趟 -> 末点 = end_pt  (已经正确, 不需要再钉)
            · 偶数趟 -> 末点 = start   (必须补一趟正向, 或以其他方式补到 end_pt)
        早期版本把这两个条件写反了, 于是 46 趟时末点停在起点,
        最终记录末点与目标终点差了 139m。

        这里统一改成: **趟数强制为奇数**, 保证末点一定落在 end_pt。
        若凑奇数会让总长超出太多 (比如 1 趟太短、3 趟太长), 则把
        末点仍然钉在 end_pt, 让调用方的 `pin_end` 逻辑做残差分配。

    返回 (新的控制点列表, 趟数, 是否仍需钉住终点)

    ★★ 为什么"就近取整"是错的 (2026-09 修复)
        早期版本取 `laps = round(ratio)` 再凑奇数, 于是 rep 长度可能
        远超 target (实测 [721] 2000m 目标 -> 3 趟 3989m, 比值 0.50),
        接下来 stage3 只能做 **50% 压缩** —— 压缩是以锚点质心为中心的
        整体收缩, 打卡点被硬生生从路上拽走 118~155m, 全部脱靶。

        正确做法: 取 "不超过 target 的最大趟数" (保证 rep <= target,
        永远不做压缩), 剩下的零头用**末段往返支线**补 —— 即在最后一段
        上插入一个折返 (出去再回来), 几何只在最后一段上膨胀, 前面所有
        趟的打卡点毫发无伤。

    ★★ 为什么必须允许"偶数趟" (2026-09 修复 2)
        如果只允许奇数趟, 某些 ratio 会退化成"少一趟" —— 实测
        ratio=2.28 (floor=2 -> 偶数 -> 退到 1) 只走出 68% 的长度,
        stage3 又得做 1.48× **拉伸**, 打卡点被甩到 339m。

        改法: 奇偶都试, 取"欠长最少"的那个; 偶数趟末点在 start,
        补一段 start->end_pt 的收尾腿即可 (end_pt 通常离沿途很近)。
        这样 rep 永远 <= target 且尽量贴近 target, stage3 只做微调。
    """
    path = [tuple(p) for p in path]
    if len(path) < 2:
        return path, 1, pin_end
    ideal_len = polyline_length(path)
    if ideal_len <= 1e-6:
        return path, 1, pin_end

    # ★ 阈值从 1.6 降到 1.15:
    #   一旦需要拉伸 15% 以上, 沿途打卡点就会被推开 (实测 1.12× 拉伸
    #   推开 68m)。与其拉伸, 不如多跑一趟 —— 几何完全不变, 打卡点
    #   稳如泰山, 这也更像真人在小范围内来回跑。
    ratio = target_len / ideal_len
    if ratio <= 1.15:
        return path, 1, pin_end

    # ★★ 取不超过 target 的最大整数趟 (rep = laps * ideal_len <= target)
    #   奇偶都允许; 偶数趟末点在 start, 后面补收尾腿到 end_pt。
    laps = max(1, int(math.floor(ratio)))

    def build(n_laps):
        seq: List[Tuple[float, float]] = []
        for L in range(n_laps):
            part = list(path) if L % 2 == 0 else list(reversed(path))
            if seq:
                part = part[1:]             # 接缝点不重复
            seq.extend(part)
        return seq

    seq = build(laps)

    # ---------- 偶数趟: 末点停在 start, 补一段收尾腿到 end_pt ----------
    #   偶数趟的末点 = path[0] = start; 而 p2p 要求末点 = end_pt。
    #   补一段 start->end_pt 的正向行程; 若整段装得下就用整段 (顺带
    #   再经过一次沿途打卡点), 装不下就按剩余长度截断。
    if laps % 2 == 0 and pin_end is not None and len(path) >= 2:
        tail = [tuple(p) for p in path]          # start ... end_pt
        tail = tail[1:]                          # 去掉重复的 start
        if tail:
            avail = target_len - polyline_length(seq)
            # 累计走 tail, 直到超出可用长度
            seg_sum = 0.0
            cur = seq[-1]
            cut = []
            for p in tail:
                sl = haversine(cur[0], cur[1], p[0], p[1])
                if seg_sum + sl > avail and cut:
                    # 截断在最后一段中间
                    rem = avail - seg_sum
                    if sl > 1e-9:
                        f = max(0.0, min(1.0, rem / sl))
                        cut.append((cur[0] + (p[0] - cur[0]) * f,
                                    cur[1] + (p[1] - cur[1]) * f))
                    break
                cut.append(p)
                seg_sum += sl
                cur = p
                if seg_sum >= avail:
                    break
            if cut:
                seq = seq + cut
                # 若截断导致末点没到 end_pt, 交给调用方 pin_end 收尾
                last = seq[-1]
                reached = (haversine(last[0], last[1],
                                     pin_end[0], pin_end[1]) < 2.0)
                if not reached:
                    pin_end = tuple(pin_end)
                else:
                    pin_end = None

    # ---------- 零头补偿: 在末段插入往返支线 ----------
    #   rep < target 时, 差额 delta 用"末段折返"吃掉:
    #   在倒数第二个顶点 u 与末点 v 之间插一个点 w, 使得
    #   |u->w| + |w->v| - |u->v| = delta。取 w 在 u->v 的反方向上,
    #   偏移量 sol 满足 2*sol ≈ delta  (|u->w|+|w->v| ≈ |u->v| + 2*sol)
    #   —— 这样只改动最后一段, 前面所有趟 (以及它们的打卡点) 不变。
    delta = target_len - polyline_length(seq)
    if delta > 0.5 and len(seq) >= 2:
        # ★ 必须用"下标"定位末段, 不能用 seq.index(v) ——
        #   多趟重复时同一个顶点会出现多次, index() 返回**第一次**
        #   出现的位置, 于是折返点被插到路径最前面, 长度瞬间爆炸
        #   (实测 7000m 目标被撑到 2100 万米)。
        k = len(seq) - 1
        while k > 0 and haversine(seq[k - 1][0], seq[k - 1][1],
                                  seq[k][0], seq[k][1]) < 5.0:
            k -= 1
        if k <= 0:
            k = len(seq) - 1
        u, v = seq[k - 1], seq[k]
        seg = haversine(u[0], u[1], v[0], v[1])
        brg = bearing(u[0], u[1], v[0], v[1])
        if seg > 1e-9:
            # 侧偏法: w 相对 u->v 连线横向偏移 h, 额外长度 ≈ 2h²/seg
            # 取 h = sqrt(delta * seg / 2), 单次即可, 再做几次牛顿修正
            h = math.sqrt(max(0.0, delta) * seg / 2.0)
            # ★ 上界保护: 横向偏移不该超过 delta 太多, 也不该无界增长
            h_cap = max(0.0, delta) + math.sqrt(2.0 * max(0.0, delta) * seg) + 1.0
            h = min(h, h_cap)
            for _ in range(6):
                w = dest_point(u[0], u[1], (brg + 90.0) % 360, h)
                extra = (haversine(u[0], u[1], w[0], w[1])
                         + haversine(w[0], w[1], v[0], v[1]) - seg)
                err = extra - delta
                if abs(err) < 0.5:
                    break
                deriv = 2.0 * h / max(1e-6, seg) + 1e-6
                h = max(0.0, min(h_cap, h - err / deriv))
            if h > 0.5:
                # 插到末段中间 (按下标, 只动这一段的几何)
                seq = seq[:k] + [w] + seq[k:]

    # 奇数趟: 末点已经就是 end_pt, 无需再钉
    new_pin = None
    return seq, laps, new_pin


def _order_waypoints_greedy(nodes: Sequence[Tuple[float, float]],
                            ) -> List[Tuple[float, float]]:
    """
    贪心最近邻排序: 从 nodes[0] 出发, 每次选最近的未访问节点, 最后到 nodes[-1]。
    用于大到小点到点模式的路径顺序。
    """
    nodes = list(nodes)
    if len(nodes) <= 3:
        return nodes
    start, end = nodes[0], nodes[-1]
    pool = nodes[1:-1]
    route = [start]
    cur = start
    while pool:
        j = min(range(len(pool)),
                key=lambda k: haversine(cur[0], cur[1], pool[k][0], pool[k][1]))
        cur = pool.pop(j)
        route.append(cur)
    route.append(end)
    return route


def nearest_index(path: Sequence[Tuple[float, float]],
                  target: Tuple[float, float]) -> int:
    """path 上离 target 最近的点下标"""
    best_i, best_d = 0, float("inf")
    for i, (la, lo) in enumerate(path):
        d = haversine(la, lo, target[0], target[1])
        if d < best_d:
            best_d, best_i = d, i
    return best_i


# ============================================================
# ★ 最短访问顺序 (打卡点必经性判定 + 路径排序)
# ============================================================
def _tsp_min_legs(start: Tuple[float, float],
                  waypoints: Sequence[Tuple[float, float]],
                  end: Optional[Tuple[float, float]] = None,
                  ) -> Tuple[float, List[Tuple[float, float]]]:
    """
    求 "start 出发访问全部 waypoints, 最后到 end" 的**近似最短**折线长度与顺序。

    为什么需要:
        打卡点散布在起点四周时, 单纯按方位角排序会产生大量交叉往返
        (实测 6 个打卡点会绕出 3.4km, 而最优顺序只要 1.6km)。
        必须真的求一遍访问顺序, 否则 800m 的目标距离在几何上不可能
        经过全部打卡点 —— 生成出来就是"一个都没打到"。

    算法:
        · n <= 8  : 全排列精确求解 (最坏 40320, 毫秒级)
        · n > 8   : 最近邻构造 + 2-opt 局部搜索 (足够好)
    返回 (长度, 顺序列表[含 start 与 end])
    """
    wps = [(float(a), float(b)) for a, b in waypoints]
    s = (float(start[0]), float(start[1]))

    if not wps:
        if end is None:
            return 0.0, [s]
        e = (float(end[0]), float(end[1]))
        return haversine(s[0], s[1], e[0], e[1]), [s, e]

    if len(wps) <= 8:
        best_len, best_seq = float("inf"), None
        # ★ 终点必须是**最后一个**节点, 不能当作普通节点参与排列
        #   (否则 TSP 会把终点放在中间, 生成的"点到点"路线末点就跑偏了)
        for perm in itertools.permutations(wps):
            seq = [s] + list(perm)
            if end is not None:
                seq = seq + [(float(end[0]), float(end[1]))]
            tot = sum(haversine(seq[i][0], seq[i][1], seq[i + 1][0], seq[i + 1][1])
                      for i in range(len(seq) - 1))
            if tot < best_len:
                best_len, best_seq = tot, seq
        return best_len, best_seq

    # --- 最近邻 + 2-opt (终点固定为末尾, 不参与交换) ---
    if end is None:
        pool, tail = list(wps), None
    else:
        tail = (float(end[0]), float(end[1]))
        pool = list(wps)

    seq = [s]
    cur = s
    remaining = list(pool)
    while remaining:
        j = min(range(len(remaining)),
                key=lambda k: haversine(cur[0], cur[1],
                                        remaining[k][0], remaining[k][1]))
        cur = remaining.pop(j)
        seq.append(cur)
    if tail is not None:
        seq.append(tail)

    def total(q):
        return sum(haversine(q[i][0], q[i][1], q[i + 1][0], q[i + 1][1])
                   for i in range(len(q) - 1))

    improved = True
    guard = 0
    # ★ 2-opt 的交换范围排除最后一个节点 (终点必须保持末位)
    n_free = len(seq) - (1 if tail is not None else 0)
    while improved and guard < 200:
        improved = False
        guard += 1
        for i in range(1, n_free - 1):
            for j in range(i + 1, n_free):
                if j - i == 1:
                    continue
                cand = seq[:i] + seq[i:j + 1][::-1] + seq[j + 1:]
                if total(cand) < total(seq) - 1e-9:
                    seq, improved = cand, True
    return total(seq), seq


def min_tour_length(start: Tuple[float, float],
                    waypoints: Sequence[Tuple[float, float]],
                    mode: str = RouteMode.LOOP,
                    end: Optional[Tuple[float, float]] = None,
                    ) -> float:
    """
    在给定模式下 "必须经过全部打卡点" 的**最短可行路程**。

        LOOP         起点 -> 打卡点... -> 回起点
        OUT_AND_BACK 2 × (起点 -> 打卡点...)        (去 + 原路回)
        POINT2POINT  起点 -> 打卡点... -> 终点
    """
    if mode == RouteMode.OUT_AND_BACK:
        one, _ = _tsp_min_legs(start, waypoints, None)
        return one * 2.0
    if mode == RouteMode.POINT2POINT:
        if end is None:
            # 未指定终点: 视作回到起点
            one, _ = _tsp_min_legs(start, waypoints, None)
            return one
        tot, _ = _tsp_min_legs(start, waypoints, end)
        return tot
    # LOOP: 必须回到起点
    tot, seq = _tsp_min_legs(start, waypoints, None)
    if len(seq) > 1:
        tot += haversine(seq[-1][0], seq[-1][1], start[0], start[1])
    return tot

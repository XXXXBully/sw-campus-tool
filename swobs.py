#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swobs.py — 运动世界校园 · OBS 轨迹对象上传

复刻真实 App 的第二阶段上传（第一阶段是 save/record 提交汇总）：
  ① POST /api/obs/temporary/url  换签名 URL
       body: {"bucketName":"iydsj-hbase-hot","objectKey":<key>,
              "method":"Put","contentType":"application/json"}
       → data.signedUrl
  ② PUT <signedUrl>  body = 10 键 OBS 对象（每值 gzip+base64）

两个 objectKey：
    run_data/{YYYYMMDDHH}/{uuid}.json
    run_data/{rrid//1000000}/{rrid}.json

10 键 OBS 对象：
    rrid, uuid, uid, run_data, fixed_point_json, segment_json,
    speed_json, step_freq_json, laps_json, runFaceCheck

为什么必须做：只提交汇总数据（第一步）而不上传轨迹（第二步），
服务端拿不到 GPS 轨迹 → 记录会显示默认位置（如北京）而非真实跑点。
"""
from __future__ import annotations

import gzip
import io
import json
import math
import os
import sys
import time
from base64 import b64encode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swclient as sw

OBS_SIGN_PATH = "/api/obs/temporary/url"
OBS_BUCKET = "iydsj-hbase-hot"

X_PI = math.pi * 3000.0 / 180.0


# ══════════════════════════════════════════════════════════════════
# 坐标转换：BD-09 → GCJ-02
# ══════════════════════════════════════════════════════════════════
def bd09_to_gcj02(bd_lat: float, bd_lng: float):
    """百度 BD-09 → 高德 GCJ-02（与 wire.rs 完全一致）"""
    x = bd_lng - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def gcj02_to_bd09(gcj_lat: float, gcj_lng: float):
    """高德 GCJ-02 → 百度 BD-09（`bd09_to_gcj02` 的数值反函数）。

    用牛顿迭代求根（正变换闭式可导），往返误差 < 1e-9 度（≈0.1 m），
    对「同时填 glat/glon 与 lat/lon 两套坐标」完全够用。
    """
    def resid(blat, blng):
        a, b = bd09_to_gcj02(blat, blng)
        return a - gcj_lat, b - gcj_lng

    blat, blng = gcj_lat, gcj_lng
    h = 1e-8
    for _ in range(30):
        f1, f2 = resid(blat, blng)
        if abs(f1) < 1e-11 and abs(f2) < 1e-11:
            break
        # 数值雅可比（中心差分）
        j11 = (resid(blat + h, blng)[0] - resid(blat - h, blng)[0]) / (2 * h)
        j12 = (resid(blat, blng + h)[0] - resid(blat, blng - h)[0]) / (2 * h)
        j21 = (resid(blat + h, blng)[1] - resid(blat - h, blng)[1]) / (2 * h)
        j22 = (resid(blat, blng + h)[1] - resid(blat, blng - h)[1]) / (2 * h)
        det = j11 * j22 - j12 * j21
        if abs(det) < 1e-18:
            break
        d1 = -(f1 * j22 - j12 * f2) / det
        d2 = -(j11 * f2 - f1 * j21) / det
        blat += d1
        blng += d2
    # 残差自检：没收敛就退回原值（GCJ 当 BD 用，误差数百米但不会更糟）
    r1, r2 = resid(blat, blng)
    if abs(r1) > 1e-6 or abs(r2) > 1e-6:
        return gcj_lat, gcj_lng
    return blat, blng


def wgs84_to_gcj02(lat: float, lng: float):
    """WGS-84 → GCJ-02（中国国测局偏移）。生成器输出的是标准坐标，
    提交时需先转成 GCJ-02 才能在 App 地图上落到正确位置。"""
    a = 6378245.0
    ee = 0.00669342162296594323

    def _transform_lat(x, y):
        ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
               + 0.2 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi)
                + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi)
                + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi)
                + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def _transform_lng(x, y):
        ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
               + 0.1 * math.sqrt(abs(x)))
        ret += (20.0 * math.sin(6.0 * x * math.pi)
                + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi)
                + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi)
                + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat: float, lng: float):
    """GCJ-02 → WGS-84（迭代反解，与 wgs84_to_gcj02 互逆到 ~1e-9 度 ≈ 0.1mm）。

    ★ 为什么需要它（2026-09-26 修）：
      服务端下发的**打卡点**带两套坐标（本地实测，见 `_gh_tools/test_crs_model.py`）：

          lat / lon   = **BD-09**（百度，历史遗留）
          glat / glon = **GCJ-02**（高德）

      而轨迹生成器按约定输出 **WGS-84**，`conv_point` 提交时再做一次
      WGS-84 → GCJ-02。所以喂给生成器之前必须先把 `glat/glon`(GCJ)
      反解成 WGS-84，让提交那一次转换正好还原。
      （揭阳一带 GCJ↔WGS 偏移 **553.8 m**；把 BD-09 的 `lat/lon` 直喂
       会让提交后的落点距真点位 **1194 m**，App 判定半径只有 15 m。）
    """
    wlat, wlng = lat, lng
    for _ in range(6):
        glat, glng = wgs84_to_gcj02(wlat, wlng)
        wlat += lat - glat
        wlng += lng - glng
    return wlat, wlng


# ══════════════════════════════════════════════════════════════════
# gzip + base64
# ══════════════════════════════════════════════════════════════════
def gz(data: bytes) -> str:
    """gzip + base64（Rust flate2 Compression::default() == level 6）"""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as f:
        f.write(data)
    return b64encode(buf.getvalue()).decode("ascii")


def gz_json(v) -> str:
    return gz(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def gz_str(s: str) -> str:
    return gz(s.encode("utf-8"))


# ── ⚠️ 历史遗留：「二次包裹」——**已作废，别再使用** ──────────────
# ★★★ 2026-09-28 晚真机定因，**推翻 09-27 的旧结论** ★★★
#
#   旧结论（错）：App 对列表型载荷还要再解一层 gzip ⇒ 这些键得装「双层」。
#   旧证据：单层时 logcat 报 `java.util.zip.ZipException: Not in GZIP format`
#           （`RunHistoryDetailActivity.parseLapRows`）。**但那条证据的前提是错的**：
#           当时写进去的其实是 `b64(JSON)`——**根本没 gzip**，
#           所以 App gunzip 才炸；并不是「少了一层」。
#
#   新证据（可复现，扫描账号下全部 19 条记录的四键层数）：
#     | 记录                       | speed/step_freq/laps_json | 详情页图表        |
#     |----------------------------|---------------------------|-------------------|
#     | App 自写（09-13~09-18 全部）| gzip×1 → 直接是 JSON      | **配速/步频/步幅/海拔 四张全有** |
#     | 我们 09-27 16:54            | gzip×1 → JSON             | —                 |
#     | 我们 09-28 14:41 / 13:51    | gzip×1 → 还是 base64 文本 | **只剩 配速/海拔 两张** |
#   ⇒ **正确形态 = 单层 `b64(gzip(JSON))`**，与 App 自写记录逐字节同构。
#
#   机理：App 只 gunzip 一次。
#     · 单层 → 解出 JSON ⇒ 步频图（`step_freq_json.stepsNum`）、
#              步幅图（`laps_json.avgStride`）都能画。
#     · 双层 → 解出 base64 文本 ⇒ JSON 解析失败 ⇒ **静默不画**（不抛异常、无 log）。
#     而「配速图 / 海拔图」来自 `run_data.allLocJson`，与这四键无关 ⇒
#     双层时它们照常渲染 —— 这正是「四张图只剩两张」的成因，也是旧结论
#     「双层后全部正常」的错觉来源（当时只看配速/海拔，没数步频/步幅）。
#
#   ★ 教训：**「缺图」要按图逐一数，不能只确认「有图」**；另外，
#     「异常消失」不等于「解析成功」——多包一层会让异常变成**静默失败**。
#
# 下面两个函数保留仅为兼容历史脚本，**不要在新代码里调用**。
def gz2(data: bytes) -> str:
    """⚠️ 已作废：双层包裹。App 只解一层，用它会静默丢图。"""
    return gz(data)


def gz2_json(v) -> str:
    """⚠️ 已作废（见上方长注释）。新代码请用 `gz_json`。"""
    return gz(gz_json(v).encode("ascii"))


def gz2_str(s: str) -> str:
    """⚠️ 已作废（见上方长注释）。新代码请用 `gz_str`。"""
    return gz(gz_str(s).encode("ascii"))


def round_to(v: float, nd: int) -> float:
    f = 10.0 ** nd
    return math.floor(abs(v) * f + 0.5) / f * (1 if v >= 0 else -1)


# ══════════════════════════════════════════════════════════════════
# 27 键协议点集（gen 点 → OBS 点）
# ══════════════════════════════════════════════════════════════════
# ★★ 轨迹点 `type` 标记位 —— 详情页「起」「终」图钉的**唯一开关**
#    2026-09-28 真机 A/B 定因（同一台手机、同一条记录、只翻一个字段）：
#      · 首点 type=5 → 画蓝色「起」；末点 type=6 → 画橙红「终」；其余 type=1
#      · 对照组（只改 state/radius/count/bdA/bdD/bdG/bdS/gainTime/id）→ 一个都不画
#    真机记录 `1321841637` 的 type 序列里 `5`/`6` 也**各只出现一次**，位置就在轨迹两端。
#    ★ 与「首末间距(gap)」无关：gap=101.36m 不画、96.40m 不画，而目标图里 gap 仅 ~35m 却有。
START_PT_TYPE = 5
END_PT_TYPE = 6


def conv_point(p: dict, start_ms: int, pt_type: int = 1) -> dict:
    """生成器点 → OBS 协议点（27 键）。

    生成器给的是 WGS-84 坐标，提交需 GCJ-02。
    若坐标为 (0,0) 视为无效点，gLat/gLng 置 -1。

    pt_type：轨迹点 `type` 标记位，默认 1（普通点）。首/末点由
      `build_obs_object` 传 START_PT_TYPE / END_PT_TYPE（见上方常量注释）。
    """
    lat, lng = float(p.get("lat", 0) or 0), float(p.get("lon", 0) or 0)
    if lat == 0.0 and lng == 0.0:
        glat, glng = -1.0, -1.0
    else:
        glat, glng = wgs84_to_gcj02(lat, lng)

    t_rel = float(p.get("t_rel", 0) or 0)
    dist = float(p.get("dist", 0) or 0)
    steps = int(p.get("steps", 0) or 0)
    cadence = float(p.get("_cadence", p.get("cadence", 0)) or 0)
    speed_ms = float(p.get("speed", 0) or 0)
    ele = float(p.get("ele", 0) or 0)
    # ★★ `speed` / `avgSpeed` 的单位是【十进制分钟/公里】，不是 m/s
    #    真机实测（2026-09-28）：写 m/s（2.95）时详情页配速曲线被画到 Y 轴
    #    3'00" 附近（App 直接把数值当分钟）、「最快配速」显示 1'55"
    #    （= min(speed)）；改成 min/km 后曲线回到 6'00" 一带、
    #    「最快配速」= 4'12"，与「每公里数据」完全自洽。
    pace_min = (1000.0 / (speed_ms * 60.0)) if speed_ms > 0 else 0.0

    return {
        "avgSpeed": round_to(pace_min, 4),
        "bdA": round_to(ele, 2),
        "bdD": 0.0,
        "bdG": 0,
        "bdS": 0.0,
        "coorType": "gcj02",
        "count": 1,
        "dtr": 0.0,
        "flag": start_ms,
        "gLat": round_to(glat, 7),
        "gLng": round_to(glng, 7),
        "gainTime": 0,
        "id": int(p.get("i", 0)),
        "lat": -1.0,
        "lng": -1.0,
        "locType": 1,
        "locationId": "",
        "queueNum": 0,
        "radius": 0.0,
        "speed": round_to(pace_min, 4),
        "state": 0,
        "stepDistance": 0.0,
        "totalDis": round_to(dist, 4),
        "totalTime": int(round(t_rel)),
        "type": int(pt_type),
        "validDis": round_to(dist, 4),
        "validTime": int(round(t_rel)),
    }


# ══════════════════════════════════════════════════════════════════
# 10 秒窗 / 圈 / 五点
# ══════════════════════════════════════════════════════════════════
def build_windows(points: list, start_ms: int, total_time: int, rrid: int):
    """10 秒窗 speed / step_freq（委托 swsubmit.android_tensec，单一实现）。

    ★ id 规则（新版本）：id = (rrid % 100000) * 1000 + 窗口右边界秒数
      注意与旧版「全局序号 60000+n」不同 —— 旧版提交不报错但详情页轨迹异常。

    ★★ 2026-09-24：原先这里自己实现了一遍「就近吸附」取窗，与提交体
      （swsubmit.android_tensec）各写一份、口径还漂移过。现统一委托，
      两处永远是同一份数据；queueNum 仍按各自历史行为（OBS 侧 0）。
    """
    import swsubmit
    sp = swsubmit.android_tensec(points, start_ms, total_time, "speed",
                                 rrid=rrid, queue_num="zero")
    stf = swsubmit.android_tensec(points, start_ms, total_time, "steps",
                                  rrid=rrid, queue_num="zero")
    return sp, stf


def build_laps(points: list, start_ms: int) -> list:
    """每 1000m 一圈，末圈 isFullLap=false；avgStride 单位厘米。

    ★★ 圈界按【精确 1000m 线性插值】(2026-09-24 修复)
      旧实现取「首个跨过 1000m 的采样点」当圈界，圈长会多出最多一个采样
      间隔：实测第 1 圈 1013.4m 而时间只到该采样点 —— 分段配速因此系统性
      偏慢约 1.3%（显示 5'40" 而真值 5'37"），用户看到的「分段/实时配速对不上」。
      真机是 1Hz 采样所以偏差可忽略；采样稀疏时必须插值。

    ★★ 圈时长按【累计取整再差分】(2026-09-24 二次修复)
      旧实现每圈各自 `int(round(lap_t))`，小数秒被独立四舍五入、误差累积：
      150 条样本中 20% 出现「圈用时和 = totalTime ± 1 秒」。
      现保证 Σduration == totalTime，且末圈 cumulativeDuration == totalTime。
    """
    laps = []
    if len(points) < 2:
        return laps
    ds = [float(p.get("dist", 0) or 0) for p in points]
    ts = [float(p.get("t_rel", 0) or 0) for p in points]
    es = [float(p.get("ele", 0) or 0) for p in points]
    ss = [float(p.get("steps", 0) or 0) for p in points]
    alt0 = es[0]
    total_d = ds[-1]
    if total_d <= 0:
        return laps

    def _at(d):
        """在累计距离 d 处线性插值出 (t, ele, steps)"""
        if d <= ds[0]:
            return ts[0], es[0], ss[0]
        if d >= total_d:
            return ts[-1], es[-1], ss[-1]
        lo, hi = 0, len(ds) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if ds[mid] <= d:
                lo = mid
            else:
                hi = mid
        span = ds[hi] - ds[lo]
        f = 0.0 if span <= 1e-9 else (d - ds[lo]) / span
        return (ts[lo] + (ts[hi] - ts[lo]) * f,
                es[lo] + (es[hi] - es[lo]) * f,
                ss[lo] + (ss[hi] - ss[lo]) * f)

    def _gain(d_a, d_b):
        """[d_a, d_b] 区间的正爬升（端点插值 + 单步 0.15m 阈值）。

        ★ 阈值口径委托 `swsubmit.ele_gain`（唯一实现），与生成器界面显示的
          `total_ascent_m`、提交体上传的 `totalAscent` 完全一致。
          历史 bug：这里原先累加**所有**正差、不设阈值，而生成器带 0.15m 阈值，
          于是「各圈爬升之和」比界面上显示的总爬升多 1~3m。
        """
        import swsubmit
        eles = [_at(d_a)[1]]
        for i, d in enumerate(ds):
            if d <= d_a:
                continue
            if d >= d_b:
                break
            eles.append(es[i])
        eles.append(_at(d_b)[1])
        return swsubmit.ele_gain(eles)

    bounds = []
    k = 0
    while (k + 1) * 1000.0 <= total_d + 1e-6:
        bounds.append([k * 1000.0, min((k + 1) * 1000.0, total_d)])
        k += 1
    tail = total_d - k * 1000.0
    if tail > 0 and bounds:
        # ★ 残尾【并入上一圈】而不是单列一圈，两个理由：
        #   ① 距离：闭环标定残差会让 total_d 落在 2000.000031 / 2000.72 这种值上，
        #      单列会造出 3cm 的 0 长度空圈（段时 1s、配速 16'40"），
        #      或让「段距和」比总距少 0.72m（两者都一眼看出是造的）。
        #   ② 时间：残尾太短时下面的累计取整会算出 duration=0 的空圈。
        #   所以要求「距离 ≥1m 且按当前配速至少能走 2 秒」才单列。
        t_tail = _at(total_d)[0] - _at(k * 1000.0)[0]
        if tail >= 1.0 and t_tail >= 2.0:
            bounds.append([k * 1000.0, total_d])
        else:
            bounds[-1][1] = total_d
    if not bounds:
        bounds = [[0.0, total_d]]

    # ★★ 累计取整再差分（2026-09-24 二次修复）
    #   旧实现每圈各自 `int(round(lap_t))`，小数秒被【独立】四舍五入，误差会累积：
    #   实测 150 条样本中 20% 出现「圈用时和 = totalTime ± 1 秒」（3km 三圈各带
    #   0.4~0.6s 残差就会凭空多出/少掉整整 1 秒）—— 用户看到的
    #   「每圈用时加起来跟总时长对不上」。
    #   改成「先对累计时间取整、再相邻相减」，于是 Σduration 恒等于 totalTime，
    #   cumulativeDuration 单调且末值精确等于总时长（与 totalTime 同源同舍入）。
    prev_cum = 0
    for i, (d_a, d_b) in enumerate(bounds):
        t_a, e_a, s_a = _at(d_a)
        t_b, e_b, s_b = _at(d_b)
        lap_d = d_b - d_a
        raw_t = t_b - t_a
        cum = int(round(t_b))
        lap_dur = cum - prev_cum
        prev_cum = cum
        lap_t = max(1.0, raw_t)      # 仅用作配速/步频/步幅的分母，不参与时长累加
        lap_steps = int(max(0.0, s_b - s_a))
        laps.append({
            "avgCadence": round_to(lap_steps / (lap_t / 60.0), 2),
            "avgPace": round_to((lap_t / 60.0) / max(lap_d / 1000.0, 0.001), 2),
            "avgStride": round_to(lap_d / max(1, lap_steps) * 100.0, 2),
            "cumulativeDuration": cum,
            "distance": round_to(lap_d, 4),
            "duration": lap_dur,
            "elevationGain": round_to(_gain(d_a, d_b), 2),
            "endAltAbs": round_to(e_b, 2),
            "endAltRel": round_to(e_b - alt0, 2),
            "flag": start_ms,
            "id": i + 1,
            "isFullLap": lap_d >= 1000.0 - 1e-9,
            "lapIndex": i + 1,
            "step": lap_steps,
        })

    # ★ 吸收「圈界插值残差」：让 Σlaps.elevationGain 精确等于按【整条轨迹】算的总爬升
    #   （即提交体 totalAscent 的口径）。圈界插值会让边界处的上升被相邻两圈各分走
    #   一部分、或两边都不算，实测残差 ≤0.25m —— 量不大，但足以在原始值落在 x.5
    #   附近时让「取整后的整数」差 1m（实测 120 条里 4.2% 出现，如 Σ圈=137.28 而
    #   上传=138）。与「残尾并入上一圈」「圈时长累计取整」同一思路：分项之和 == 总计。
    #   残差并入【距离最大】的那一圈（它的爬升最大，±0.25m 不可能把它压成负数）。
    if laps:
        import swsubmit
        _target = swsubmit.ele_gain(es)
        _cur = sum(float(l["elevationGain"]) for l in laps)
        if abs(_target - _cur) > 1e-9:
            _k = max(range(len(laps)), key=lambda j: float(laps[j]["distance"]))
            laps[_k]["elevationGain"] = round_to(
                float(laps[_k]["elevationGain"]) + (_target - _cur), 2)
    return laps


def five_point_payload(points: list, start_ms: int,
                       real_keys: bool = False) -> list:
    """五点实体（跑完态 isPass=true）。

    ★★ 只接受【服务端下发的打卡点】，绝不接受轨迹点 ★★
      · 自由跑：无围栏、无打卡点 → 传 []，fivePointJson = []（空数组）
      · 计分跑：传学校下发的点位（通常 3~5 个，isFixed=1 为必经点）

    历史 bug（已修）：本函数原先对【轨迹点】逐点生成 isPass=true 的"假打卡点"，
    导致 2km 自由跑（144 个轨迹点）往 fixed_point_json 里塞 144 个点位，
    5km 就是上千个 —— 与真实 App 的「自由跑无点位」完全不符。

    字段实现委托给 swsubmit.five_point_payload，保证提交 body 与 OBS 对象
    里的 fivePointJson 结构完全一致（单一实现，避免两份漂移）。
    """
    if not points:
        return []
    import swsubmit
    return swsubmit.five_point_payload(points, start_ms, real_keys=real_keys)


def five_points_list(points: list, start_ms: int) -> list:
    """记录侧 `fivePointJson` 的**数组值**（list）。

    ★★ 2026-09-27 真机定因：OBS 的 `fixed_point_json` 被
      `Gson.fromJson(text, Bean)` 反序列化，Bean 的 `fivePointJson` 字段
      声明为 **String** ⇒ OBS 里的值必须是**字符串**（内容才是数组文本）。
      真机两轮判别实证（错误 path 从 `$.fivePointJson` 移到 `$.geoFencesJson`）：
        两个都写数组 → `Expected a string but was BEGIN_ARRAY … $.fivePointJson`
        five=str/geo=array → 同一句，但 path = `$.geoFencesJson`
      ⇒ 两个字段都必须是字符串。真机 App 自己写的记录里也全是 str。

    ★ 上一轮推论「path $ ⇒ 值被单独 fromJson(…, Collection) ⇒ 必须是数组」
      **是错的**：`fivePointJson` 在 JSON 里排在 `geoFencesJson` 前面，
      它类型一错解析就中断，`geoFencesJson` 的类型根本没被验证到
      —— 又一起「对照组不成立」（同类第 4 次）。

    ★ OBS 里请用 `five_point_json()`（字符串）；本函数只用于需要 list 的
      场合（例如提交 body 的 JSON 内层结构）。
    """
    if not points:
        return []
    return five_point_payload(points, start_ms)


def five_point_json(points: list, start_ms: int) -> str:
    """记录侧 `fivePointJson` 的**字符串**形态（内容为 JSON 数组文本）。

    ★★ OBS 的 `fixed_point_json.fivePointJson` 就用这个 ——真机定因见
      `five_points_list()` 的 docstring（Bean 字段是 String）。

    ★★★ 2026-09-29：这里**必须**走 `real_keys=True`（真机记录侧键集）★★★
      真机 App 自己写的记录（fixture `obs_backup_1323338948.json`）里：
        · 键 = flag/glat/glon/id/isFixed/isPass/lat/lng/pointName/position/state
          —— **没有 `radius`**
        · **`isFixed` 全 0**、`pointName` 全空
      而详情页「✓ 打卡点」是「必经点画橙黄、普通点画绿底」（见 swcli.py 的
      policy 注释）⇒ 旧版把服务端的 `isFixed=1` 带进 OBS，详情页就多一个**橙点**，
      真机记录永远没有 ⇒ 肉眼可辨。
      ⇒ OBS 一律写真机键集；提交 body 侧（`swsubmit.five_point_wrapper`）不动。
    """
    return json.dumps(five_point_payload(points, start_ms, real_keys=True),
                      separators=(",", ":"), ensure_ascii=False)


def geo_fences_json(geo_fences: list) -> str:
    """记录侧 `geoFencesJson` 字符串（内容为 JSON 数组文本）。

    ★★ OBS 的 `fixed_point_json.geoFencesJson` 就用这个 ——
      Bean 字段是 String（真机定因见 swsubmit.norm_geo_fences）。
      字段实现委托给 swsubmit.geo_fences_json，保证提交 body 与 OBS 对象
      里的 geoFencesJson 结构完全一致（单一实现，避免两份漂移）。
    """
    import swsubmit
    return swsubmit.geo_fences_json(geo_fences)


def geo_fences_list(geo_fences: list) -> list:
    """记录侧 `geoFencesJson` 的**数组值**（list）。

    ★ 注意：**OBS 里不要用这个函数**，要用 `geo_fences_json()`。
      真机实证 Bean 的 `geoFencesJson` 字段是 String，写成数组会抛
      `Expected a string but was BEGIN_ARRAY … path $.geoFencesJson`。
      （上一轮据此推出的「必须是数组」已被推翻，见 five_points_list。）
    """
    import swsubmit
    return swsubmit.norm_geo_fences(geo_fences)


# ══════════════════════════════════════════════════════════════════
# 10 键 OBS 对象 & key 命名
# ══════════════════════════════════════════════════════════════════
def obs_keys(start_ms: int, rrid: int, uuid: str) -> list:
    t0 = time.strftime("%Y%m%d%H", time.localtime(start_ms / 1000.0))
    return ["run_data/{}/{}.json".format(t0, uuid),
            "run_data/{}/{}.json".format(rrid // 1000000, rrid)]


def build_obs_object(points: list, *, rrid: int, uuid: str, uid: int,
                     start_ms: int, total_time: int, with_steps: bool = True,
                     fixed_points: list = None,
                     geo_fences: list = None) -> dict:
    """组装 10 键 OBS 对象（值均 gzip+base64）

    ★★ **10 个键全部是「单层」** `b64(gzip(明文))` —— App 只 gunzip 一次。
      2026-09-28 真机定因（**推翻 09-27 的「列表型四键要双层」结论**）：
        · App 自写记录的 `speed_json`/`step_freq_json`/`laps_json`/`segment_json`
          全是 gzip×1 → 解一层直接是 JSON。
        · 我们写成双层时：App 解一层得到 **base64 文本** ⇒ JSON 解析失败
          ⇒ **静默不画**（不抛异常、无 log）。因为「配速图 / 海拔图」来自
          `run_data.allLocJson`（与这四键无关），双层时它们照常渲染 ⇒
          表现成「四张图只剩两张」。
        · 旧证据 `ZipException: Not in GZIP format` 的真实前提是当时写的是
          `b64(JSON)`（**压根没 gzip**），不是「少了一层」。详见 gz2_json 注释。

    with_steps 参数保留以兼容旧调用，但自由跑与计分跑【都要】完整
    步频/步幅数据（详情页图表数据源），不再清零。

    fixed_points：【服务端下发的打卡点】，只用于 fixed_point_json 里的
      fivePointJson。
        · 自由跑 → 传 [] 或 None → fivePointJson = "[]"（空数组文本，无点位）
        · 计分跑 → 传学校下发的点位
        · ★★ 值是**字符串**（内容才是 JSON 数组文本）—— Bean 字段声明为
          String，写成数组会抛 `Expected a string but was BEGIN_ARRAY
          … path $.fivePointJson`（真机实证），详见 five_points_list()。
      ★ 绝不传轨迹点：轨迹点属于 run_data.allLocJson，两者是不同的东西。

    geo_fences：【服务端下发的围栏】，只用于 fixed_point_json 里的
      geoFencesJson。
        · ★★ 值同样是**字符串**（内容为 JSON 数组文本 `[{…}]`），
          App 会对该文本再 `fromJson(…, Collection)`；写成数组会在 Bean
          解析阶段就抛 `… path $.geoFencesJson`（真机实证）。
        · ★★ 点位键名必须是客户端 Bean 的 **`lng` / `glng`**（不是服务端 DTO 的
          `lon` / `glon`），且 `lat/lng` 恒 0.0、真值全在 `glat/glng`（GCJ-02）
          —— App 画多边形用的是 `glng`；写成 `glon` 会读成 0.0 ⇒ 顶点全塌到
          `(lat, 0)` ⇒ 多边形退化、**解析成功却画不出**（真机 A/B 实证，
          详见 swsubmit.norm_geo_fences）。
    """
    pts = [conv_point(p, start_ms) for p in points]

    # ★★ 步频/步幅图的【数据源保护】（2026-09-28 真机定因）
    #    生成器的 `points` 只有 `cadence`(步/分) / `stride_cm`，**没有 `steps`**
    #    （累计步数）。累计步数由 `swsubmit.prep_points()` 用 cadence 梯形积分补出。
    #    若调用方跳过这一步直接把生成器 points 喂进来：
    #      · `android_tensec`（`_track_stream` 取 `b["steps"]-a["steps"]`）
    #        ⇒ `step_freq_json[].stepsNum` **全 0**
    #      · `build_laps`（`p.get("steps",0)`）⇒ `avgCadence`=0、
    #        `avgStride` 退化成 `lap_d*100`
    #    ⇒ App 画不出【步频图 / 步幅图】，详情页只剩配速图 + 海拔图。
    #    实测：走 `_repair_record.py` 重建的 9/28 记录只有 2 张图；
    #          正常提交（swcli→build_record_body→prep_points）的 9/16 记录有 4 张图。
    #    这里只【告警】不改数据 —— 避免把调用方的缺陷悄悄盖掉。
    _has_cad = any(float(p.get("cadence", 0) or 0) > 0 for p in points)
    _has_steps = any(float(p.get("steps", 0) or 0) > 0 for p in points)
    if _has_cad and not _has_steps:
        import sys as _sys
        _sys.stderr.write(
            "[warn] build_obs_object: points 只有 cadence 没有累计步数 steps "
            "⇒ step_freq_json.stepsNum 全 0、laps.avgStride 失效 "
            "⇒ 详情页会缺【步频图/步幅图】。请先走 swsubmit.prep_points() 补齐。\n")

    # ★★ 详情页「起」「终」图钉：首点 type=5、末点 type=6
    #    （2026-09-28 真机 A/B 定因，见 START_PT_TYPE / END_PT_TYPE 的注释）
    if len(pts) >= 2:
        pts[0]["type"] = START_PT_TYPE
        pts[-1]["type"] = END_PT_TYPE
    run_wrap = {"allLocJson": json.dumps(pts, separators=(",", ":"),
                                        ensure_ascii=False),
                "useZip": False}
    sp, stf = build_windows(points, start_ms, total_time, rrid)
    laps = build_laps(points, start_ms)
    fx = {"fivePointJson": five_point_json(list(fixed_points or []), start_ms),
          "freedomShowFence": False,
          "geoFencesJson": geo_fences_json(geo_fences),
          "runAreaId": -1,
          "useZip": False}
    return {
        "rrid": gz_str(str(rrid)),
        "uuid": gz_str(uuid),
        "uid": gz_str(str(uid)),
        "run_data": gz_json(run_wrap),
        "fixed_point_json": gz_json(fx),
        # ★★★ 列表型四键 = **单层** `b64(gzip(JSON))`（2026-09-28 真机定因，
        #     推翻了此前「要解两层」的结论，详见 gz2_json 上方的长注释）
        "segment_json": gz_str(""),
        "speed_json": gz_json(sp),
        "step_freq_json": gz_json(stf),
        "laps_json": gz_json(laps),
        "runFaceCheck": gz_str(""),
    }


# ══════════════════════════════════════════════════════════════════
# OBS 上传（换签名 URL → PUT）
# ══════════════════════════════════════════════════════════════════
def sign_url(call_fn, key: str, method: str = "Put") -> str:
    body = json.dumps({"bucketName": OBS_BUCKET, "objectKey": key,
                       "method": method, "contentType": "application/json"},
                      separators=(",", ":"))
    _, biz, err, _ = call_fn("POST", OBS_SIGN_PATH, body)
    if biz is None:
        raise RuntimeError("OBS 签名失败: %s" % err)
    d = biz.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            pass
    signed = None
    if isinstance(d, dict):
        signed = d.get("signedUrl") or d.get("signedURL") or d.get("url")
    if not signed:
        signed = biz.get("signedUrl")
    if not signed:
        raise RuntimeError("OBS 签名响应缺 signedUrl: %s"
                           % json.dumps(biz, ensure_ascii=False)[:300])
    return signed


def put_object(signed_url: str, payload: bytes) -> int:
    import urllib.request
    import urllib.error
    req = urllib.request.Request(signed_url, data=payload, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError("OBS PUT HTTP %d: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:200]))


def upload_track(call_fn, points: list, *, rrid: int, uuid: str, uid: int,
                 start_ms: int, total_time: int, with_steps: bool = True,
                 fixed_points: list = None, geo_fences: list = None,
                 verbose: bool = True):
    """完整 OBS 上传：组装 → 换签名 → 双 key PUT。返回成功数。

    fixed_points：服务端下发的打卡点（自由跑传 [] / 不传 → fivePointJson=[]）。
    geo_fences  ：服务端下发的围栏（不传 → geoFencesJson=[] 空数组，
                  详情页无围栏可画；注意是**数组**不是字符串）。
    """
    obj = build_obs_object(points, rrid=rrid, uuid=uuid, uid=uid,
                           start_ms=start_ms, total_time=total_time,
                           with_steps=with_steps,
                           fixed_points=fixed_points,
                           geo_fences=geo_fences)
    payload = json.dumps(obj, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    keys = obs_keys(start_ms, rrid, uuid)
    if verbose:
        print("  OBS 对象 %d 字节  key 数=%d" % (len(payload), len(keys)))
    ok = 0
    for k in keys:
        try:
            url = sign_url(call_fn, k, "Put")
            st = put_object(url, payload)
            if verbose:
                print("  PUT %s -> %d" % (k, st))
            ok += 1
        except Exception as e:
            if verbose:
                print("  [warn] PUT %s 失败: %s" % (k, e))
    return ok, keys


def read_back(call_fn, key: str, timeout: int = 30):
    """用 GET 签名 URL 回读对象原文（bytes）。失败返回 None。

    注意：部分 OBS 桶不允许 GET 临时签名，此时会抛错；调用方应容错。
    """
    import urllib.request
    import urllib.error
    url = sign_url(call_fn, key, "Get")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("OBS GET HTTP %d: %s"
                           % (e.code, e.read().decode("utf-8", "replace")[:200]))


def decode_point_count(obj: dict) -> int:
    """从回读的 OBS 对象里解出轨迹点数量（run_data 的 allLocJson）"""
    import gzip
    import base64
    try:
        rd = json.loads(gzip.decompress(
            base64.b64decode(obj.get("run_data") or "")).decode("utf-8"))
        locs = rd.get("allLocJson") or "[]"
        if isinstance(locs, str):
            locs = json.loads(locs)
        return len(locs)
    except Exception:
        return 0


def selftest() -> bool:
    ok = True
    print("=" * 62)
    print("swobs 自检")
    print("=" * 62)

    import base64

    def _unwrap1(v: str) -> str:
        """信封解一层（gzip+b64 → 明文）。"""
        return gzip.decompress(base64.b64decode(v)).decode("utf-8")

    # 1) 坐标转换
    a, b = wgs84_to_gcj02(22.981367, 116.332141)
    print("  WGS84(22.981367,116.332141) -> GCJ02(%.7f,%.7f)" % (a, b))
    ok &= abs(a - 22.981367) > 1e-5  # 转换后必然有偏移
    ok &= abs(a - 22.981367) < 0.01
    ok &= abs(b - 116.332141) < 0.01
    print("  %s GCJ-02 偏移量合理" % ("OK " if ok else "FAIL"))

    # 2) gzip round-trip
    s = "运动世界校园"
    import base64
    import gzip as _gz
    back = _gz.decompress(base64.b64decode(gz_str(s))).decode("utf-8")
    print("  %s gzip+base64 往返 == 原文" % ("OK " if back == s else "FAIL"))
    ok &= back == s

    # 3) obs_keys
    ks = obs_keys(1789534834000, 1322680573, "TEST-UUID-6BB5-4895-BEB8-E0B7C110EC26")
    exp_rrid = 1322680573 // 1000000
    print("  keys = %s" % ks)
    ok &= ("run_data/%d/1322680573.json" % exp_rrid) in ks
    print("  %s rrid key 命名正确" % ("OK " if ok else "FAIL"))

    # 4) 10 键完整性
    pts = [{"i": 0, "t_rel": 0.0, "lat": 22.981367, "lon": 116.332141,
            "ele": 100.0, "speed": 2.0, "dist": 0.0, "steps": 0, "cadence": 160},
           {"i": 1, "t_rel": 10.0, "lat": 22.981400, "lon": 116.332180,
            "ele": 101.0, "speed": 3.0, "dist": 25.0, "steps": 27, "cadence": 162}]
    obj = build_obs_object(pts, rrid=1322680573, uuid="UUID-T",
                           uid=12345678, start_ms=1789534834000, total_time=10)
    need = ["rrid", "uuid", "uid", "run_data", "fixed_point_json",
            "segment_json", "speed_json", "step_freq_json", "laps_json",
            "runFaceCheck"]
    miss = [k for k in need if k not in obj]
    print("  %s 10 键齐全 %s" % ("OK " if not miss else "FAIL", miss or ""))
    ok &= not miss
    print("  %s 全部值为字符串" % ("OK " if all(isinstance(v, str) for v in obj.values()) else "FAIL"))
    ok &= all(isinstance(v, str) for v in obj.values())

    # 5) run_data 可解回，且含 allLocJson 27 键
    import base64
    rd = json.loads(_gz.decompress(base64.b64decode(obj["run_data"])).decode("utf-8"))
    locs = json.loads(rd["allLocJson"])
    n27 = len(locs[0]) if locs else 0
    print("  run_data 轨迹点数=%d 单点键数=%d" % (len(locs), n27))
    ok &= len(locs) == 2 and n27 == 27
    print("  %s 27 键协议点集" % ("OK " if n27 == 27 else "FAIL"))
    print("  %s coorType=gcj02" % ("OK " if locs[0]["coorType"] == "gcj02" else "FAIL"))
    ok &= locs[0]["coorType"] == "gcj02"

    # 5b) ★★ 「起」「终」图钉开关：首点 type=5 / 末点 type=6 / 中间 type=1
    #     2026-09-28 真机 A/B 定因。反向注入：写成全 1 ⇒ 详情页两个标都不画。
    tseq = [q["type"] for q in locs]
    c_type = (len(locs) >= 2 and locs[0]["type"] == START_PT_TYPE
              and locs[-1]["type"] == END_PT_TYPE
              and all(q["type"] == 1 for q in locs[1:-1]))
    print("  type 序列=%s (期望首 %d / 末 %d / 中间 1)"
          % (tseq, START_PT_TYPE, END_PT_TYPE))
    print("  %s 起终标记位（首 type=5 / 末 type=6）"
          % ("OK " if c_type else "FAIL"))
    ok &= c_type
    # 单点轨迹不得崩（<2 点时不做标记）
    try:
        o1 = build_obs_object(pts[:1], rrid=1322680573, uuid="UUID-T",
                              uid=12345678, start_ms=1789534834000, total_time=10)
        l1 = json.loads(json.loads(_gz.decompress(
            base64.b64decode(o1["run_data"])).decode("utf-8"))["allLocJson"])
        print("  %s 单点轨迹不崩（type=%d）"
              % ("OK " if len(l1) == 1 else "FAIL", l1[0]["type"]))
        ok &= len(l1) == 1
    except Exception as e:                                # pragma: no cover
        print("  FAIL 单点轨迹抛异常: %s" % e)
        ok = False

    # 6) ★★ 五点来源：轨迹点【不得】出现在 fixed_point_json 里
    fx = json.loads(_gz.decompress(
        base64.b64decode(obj["fixed_point_json"])).decode("utf-8"))
    five_free = json.loads(fx["fivePointJson"])          # ★ 内容是数组文本
    print("  [自由跑] fixed_point_json 点位数=%d (期望 0)" % len(five_free))
    c_f5a = isinstance(fx["fivePointJson"], str) and not five_free
    print("  %s 自由跑 fivePointJson == []（字符串包空数组）"
          % ("OK " if c_f5a else "FAIL"))
    ok &= c_f5a

    cps = [{"pointName": "一号点", "lat": 22.98, "lon": 116.33,
            "glat": 22.981, "glon": 116.335, "radius": 15.0, "isFixed": 1},
           {"pointName": "二号点", "lat": 22.99, "lon": 116.34,
            "glat": 22.991, "glon": 116.345, "radius": 15.0, "isFixed": 0}]
    obj2 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10,
                            fixed_points=cps)
    fx2 = json.loads(_gz.decompress(
        base64.b64decode(obj2["fixed_point_json"])).decode("utf-8"))
    five2 = json.loads(fx2["fivePointJson"])             # ★ 值是 str，内容才是数组
    #   ★ 2026-09-29 真机 ground truth 定因：**OBS 侧必须写真机键集** ——
    #     `isFixed` 全 0、`pointName` 全空、**没有 `radius`**
    #     （真机 App 自己写的记录：fixture obs_backup_1323338948/1323338294/1323356363
    #      三条全是 five==allLoc、无 radius、isFixed 全 0 ⇒ 详情页全绿、永无橙点）。
    #     我们旧版把服务端的 `isFixed=1`（必经点）原样写进去 ⇒ 详情页凭空多一个
    #     **橙点**（规则：必经点画橙黄 / 普通点画绿底）⇒ 一眼可辨是伪造。
    #     这里**反向注入**守住它：必须 isFixed 全 0、pointName 全空、radius 不在。
    good2 = (isinstance(fx2["fivePointJson"], str)
             and len(five2) == 2
             and all(p["isFixed"] == 0 for p in five2)
             and all(p["pointName"] == "" for p in five2)
             and "radius" not in five2[0]
             and "lng" in five2[0]
             and "lon" not in five2[0])
    #   ★ 2026-09-28 真机定因：记录侧 `fivePointJson` 的经度字段名是 **`lng`**
    #     （不是服务端打卡点接口的 `lon`）。写成 `lon` ⇒ Bean 读 `lng` 得 0
    #     ⇒ 每个打卡点被画到 (lat, 0)（屏幕外）⇒ 详情页一个 ✓ 都不出现。
    #     这里**反向注入**守住它：必须 `lng` 在、`lon` 不在。
    print("  [计分跑] fixed_point_json 点位数=%d (期望 2)" % len(five2))
    print("  %s 计分跑五点 = 真机键集（字符串 + isFixed 全 0 / pointName 空 / 无 radius / 用 lng）"
          % ("OK " if good2 else "FAIL"))
    ok &= good2

    # ★ 非空转证明：body 侧（real_keys=False）必须**仍保留** isFixed=1 /
    #   pointName / radius —— 若两边都变 0，说明 real_keys 开关压根没生效
    #   （判据空转，等于没测）。body 不能动：服务端那条链路要原样。
    body2 = five_point_payload(cps, 1789534834000)
    c_sw = (body2[0]["isFixed"] == 1 and body2[0]["pointName"] == "一号点"
            and "radius" in body2[0])
    print("  %s 非空转：body 侧仍保留 isFixed=1 / pointName / radius（开关真生效）"
          % ("OK " if c_sw else "FAIL"))
    ok &= c_sw

    # ★ 变异自检：five_points_list → list，five_point_json → str，必须可区分
    c_f5b = (isinstance(five_points_list(cps, 1789534834000), list)
             and isinstance(five_point_json(cps, 1789534834000), str))
    print("  %s 变异自检：five_points_list→list / five_point_json→str"
          % ("OK " if c_f5b else "FAIL"))
    ok &= c_f5b

    # 7) id 规则（speed_json 是**单层**包裹，解一层即明文）
    print("  speed_json[0].id 规则: (rrid%%100000)*1000+hi")
    spj = json.loads(_gz.decompress(
        base64.b64decode(obj["speed_json"])).decode("utf-8"))
    exp_id = (1322680573 % 100000) * 1000 + 10
    print("  %s id=%d (期望 %d)" % ("OK " if spj[0]["id"] == exp_id else "FAIL",
                                    spj[0]["id"], exp_id))
    ok &= spj[0]["id"] == exp_id

    # 8) ★★ 围栏：geoFencesJson 必须是**字符串**（内容才是 JSON 数组文本）
    #    2026-09-27 真机定因：fixed_point_json 被 Gson.fromJson(text, Bean)
    #    反序列化，Bean 的 geoFencesJson 字段声明为 String。
    #      写成数组 → `Expected a string but was BEGIN_ARRAY … path $.geoFencesJson`
    #      （同理 fivePointJson 也一样；曾据 path `$` 误推成"必须是数组"，已推翻）
    gf_raw = [{"id": 2351, "name": "围栏",
               "points": [{"lon": 0.0, "lat": 0.0, "glon": 116.330092,
                           "glat": 22.982321, "pointsNumber": 1}]}]
    obj3 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10,
                            fixed_points=cps, geo_fences=gf_raw)
    fx3 = json.loads(_gz.decompress(
        base64.b64decode(obj3["fixed_point_json"])).decode("utf-8"))
    gj3 = json.loads(fx3["geoFencesJson"])             # 值是 str，内容才是数组
    c_f1 = isinstance(fx3["geoFencesJson"], str)
    p0 = gj3[0]["points"][0]
    #   ★★ 2026-09-28 真机定因（A/B 实证）：围栏顶点键名必须是客户端 Bean 的
    #      **`lng` / `glng`**（不是服务端 `getGeoFenceForRun` DTO 的 `lon`/`glon`），
    #      且 `lat/lng` 恒 0.0、真值全在 `glat/glng`(GCJ-02)。
    #      写 `glon` ⇒ App 读 `glng` 得 0 ⇒ 十顶点全塌到 (lat, 0) ⇒ 多边形退化
    #      ⇒ **Gson 解析成功、不抛异常、却什么也画不出来**。
    #      这里用「服务端 DTO 形态」的输入（lon/glon）反向注入，断言输出必须
    #      被规整成 `glng`（且不得残留 `glon`）。
    c_f2 = (len(gj3) == 1 and gj3[0]["id"] == "2351"
            and p0["glat"] == 22.982321 and p0["glng"] == 116.330092  # GCJ 真值
            and p0["lat"] == 0.0 and p0["lng"] == 0.0                  # 客户端读这两键
            and "glon" not in p0 and "lon" not in p0
            and p0["pointsNumber"] == 1)
    print("  [围栏] geoFencesJson 值类型 = %s (期望 str)"
          % type(fx3["geoFencesJson"]).__name__)
    print("  %s 围栏是字符串（Bean 字段是 String）"
          % ("OK " if c_f1 else "FAIL"))
    print("  %s 顶点键名 = 客户端 Bean 的 glat/glng（lat/lng 恒 0）、id 转字符串"
          % ("OK " if c_f2 else "FAIL"))
    ok &= (c_f1 and c_f2)

    # 客户端形态输入（glng）也必须能规整（两种拼写都要吃）
    gf_cli = [{"id": 2351, "points": [{"lat": 0.0, "lng": 0.0,
                                       "glat": 22.982321,
                                       "glng": 116.330092}]}]
    p1 = geo_fences_list(gf_cli)[0]["points"][0]
    c_f2b = (p1["glat"] == 22.982321 and p1["glng"] == 116.330092
             and p1["lat"] == 0.0 and p1["lng"] == 0.0)
    print("  %s 客户端形态输入（glng）同样被规整"
          % ("OK " if c_f2b else "FAIL"))
    ok &= c_f2b

    # 不传围栏 → "[]"（空数组文本，仍是字符串）
    obj4 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10)
    fx4 = json.loads(_gz.decompress(
        base64.b64decode(obj4["fixed_point_json"])).decode("utf-8"))
    c_f3 = (fx4["geoFencesJson"] == "[]"
            and isinstance(fx4["geoFencesJson"], str))
    print("  %s 不传围栏 → geoFencesJson=\"[]\"（字符串）"
          % ("OK " if c_f3 else "FAIL"))
    ok &= c_f3

    # ★ 变异自检 1：两个函数必须**可区分**（否则断言是空转）
    c_f4 = (isinstance(geo_fences_json(gf_raw), str)
            and isinstance(geo_fences_list(gf_raw), list))
    print("  %s 变异自检：geo_fences_json→str / geo_fences_list→list"
          % ("OK " if c_f4 else "FAIL"))
    ok &= c_f4

    # ★ 变异自检 2：把「空围栏」伪造成对象形状，必须不被当成数组
    c_f5 = isinstance(json.loads(json.dumps({"updateTime": 1, "geoFences": []})),
                      list) is False
    print("  %s 变异自检：对象形状不被当成数组" % ("OK " if c_f5 else "FAIL"))
    ok &= c_f5

    # 9) ★★★ 列表型四键必须是「单层」`b64(gzip(JSON))`（App 只 gunzip 一次）
    #    2026-09-28 真机定因（**推翻「要双层」的旧结论**）：
    #      App 自写记录四键全是 gzip×1；我们写成双层时，App 解一层得到 base64
    #      文本 ⇒ JSON 解析失败 ⇒ **静默不画**（步频图/步幅图消失）。
    list_keys = ("laps_json", "speed_json", "step_freq_json", "segment_json")

    def _is_double(v):
        """解一层后剩下的字节还是 gzip 流（b64 后以 \\x1f\\x8b 开头）= 双层包裹。"""
        try:
            inner = _unwrap1(v)
        except Exception:
            return False
        try:
            return base64.b64decode(inner)[:2] == b"\x1f\x8b"
        except Exception:
            return False

    layer_ok = {k: not _is_double(obj[k]) for k in list_keys}
    print("  %s 列表型四键=单层包裹 %s"
          % ("OK " if all(layer_ok.values()) else "FAIL",
             {k: ("单层" if v else "双层!") for k, v in layer_ok.items()}))
    ok &= all(layer_ok.values())

    # 解一层后必须是合法 JSON / 空串
    laps1 = json.loads(_unwrap1(obj["laps_json"]))
    c_l1 = isinstance(laps1, list) and len(laps1) >= 1
    print("  %s laps_json 解一层 == JSON 数组(%d 圈)"
          % ("OK " if c_l1 else "FAIL", len(laps1) if isinstance(laps1, list) else -1))
    ok &= c_l1
    c_l2 = _unwrap1(obj["segment_json"]) == ""
    print("  %s segment_json 解一层 == 空串" % ("OK " if c_l2 else "FAIL"))
    ok &= c_l2

    # 单层键同样【只】解一层
    single_ok = all(not _is_double(obj[k])
                    for k in ("rrid", "run_data", "fixed_point_json"))
    print("  %s 单层键（rrid/run_data/fixed_point_json）也是单层"
          % ("OK " if single_ok else "FAIL"))
    ok &= single_ok

    # ★ 变异自检：故意把 laps_json 多包一层（双层），断言必须能抓到
    bad = dict(obj)
    bad["laps_json"] = gz2_json(laps1)         # 双层（= 修复前的线上行为）
    caught = _is_double(bad["laps_json"])
    print("  %s 变异自检：双层 laps_json 会被抓出" % ("OK " if caught else "FAIL"))
    ok &= caught

    print("=" * 62)
    print("汇总: %s" % ("全部通过" if ok else "存在失败"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)

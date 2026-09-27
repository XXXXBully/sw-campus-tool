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


# ── 「二次包裹」载荷（列表型字段专用）────────────────────────────
# ★ 真机实测定因（2026-09-27，logcat）：
#     RunHistoryDetailActivity.parseLapRows
#       → mvi.run.oo0o0Oo.OooO0oO
#       → utils.o0OO00O.OooO0oO 抛 java.util.zip.ZipException: Not in GZIP format
#   ⇒ App 对「列表型」载荷（laps / speed / step_freq / segment）**还要再解一层 gzip**。
#   信封解一层后 App 拿到的是明文 → 再 gunzip 就炸。所以这些键里装的**本身就得是
#   gzip+b64**（即解两层才见明文）。
#   后果对照（同一条记录，只改这一处）：
#     单层 → ZipException 中断 setMapCenter，「每公里数据」整块不渲染
#     双层 → 异常计数 0；每公里数据 / 配速图 / 海拔图 / 步频步幅 全部正常
#   （配速图均值 5'40" == 680s÷2km，证明 App 解出的确实是我们的原始数据）
def gz2(data: bytes) -> str:
    """把「已是 gzip+b64 的字符串」再包一层 → 解两层才见明文。"""
    return gz(data)


def gz2_json(v) -> str:
    return gz(gz_json(v).encode("ascii"))


def gz2_str(s: str) -> str:
    return gz(gz_str(s).encode("ascii"))


def round_to(v: float, nd: int) -> float:
    f = 10.0 ** nd
    return math.floor(abs(v) * f + 0.5) / f * (1 if v >= 0 else -1)


# ══════════════════════════════════════════════════════════════════
# 27 键协议点集（gen 点 → OBS 点）
# ══════════════════════════════════════════════════════════════════
def conv_point(p: dict, start_ms: int) -> dict:
    """生成器点 → OBS 协议点（27 键）。

    生成器给的是 WGS-84 坐标，提交需 GCJ-02。
    若坐标为 (0,0) 视为无效点，gLat/gLng 置 -1。
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
    speed = float(p.get("speed", 0) or 0)
    ele = float(p.get("ele", 0) or 0)

    return {
        "avgSpeed": round_to(speed, 4),
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
        "speed": round_to(speed, 4),
        "state": 0,
        "stepDistance": 0.0,
        "totalDis": round_to(dist, 4),
        "totalTime": int(round(t_rel)),
        "type": 1,
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


def five_point_payload(points: list, start_ms: int) -> list:
    """五点实体（跑完态 isPass=true）。

    ★★ 只接受【服务端下发的打卡点】，绝不接受轨迹点 ★★
      · 自由跑：无围栏、无打卡点 → 传 []，fivePointJson 序列化为 "[]"
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
    return swsubmit.five_point_payload(points, start_ms)


def geo_fences_json(geo_fences: list) -> str:
    """记录侧 `geoFencesJson` 字符串（**JSON 数组**）。

    字段实现委托给 swsubmit.geo_fences_json，保证提交 body 与 OBS 对象里
    的 geoFencesJson 结构完全一致（单一实现，避免两份漂移）。
    定因（App 的 Gson 要求 BEGIN_ARRAY）见 swsubmit.norm_geo_fences。
    """
    import swsubmit
    return swsubmit.geo_fences_json(geo_fences)


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

    ★ 两层包裹：`run_data` / `fixed_point_json` / `rrid` / `uuid` / `uid` /
      `runFaceCheck` 是**单层**（信封解一层即明文）；而**列表型**四键
      （`laps_json` / `speed_json` / `step_freq_json` / `segment_json`）
      是**双层**（App 还要再 gunzip 一次）—— 定因见 gz2_json 的注释。
      写错层数的后果不是「数据难看」，而是 `ZipException` 直接中断详情页渲染。

    with_steps 参数保留以兼容旧调用，但自由跑与计分跑【都要】完整
    步频/步幅数据（详情页图表数据源），不再清零。

    fixed_points：【服务端下发的打卡点】，只用于 fixed_point_json 里的
      fivePointJson。
        · 自由跑 → 传 [] 或 None → fivePointJson = "[]"（无点位）
        · 计分跑 → 传学校下发的点位
      ★ 绝不传轨迹点：轨迹点属于 run_data.allLocJson，两者是不同的东西。

    geo_fences：【服务端下发的围栏】，只用于 fixed_point_json 里的
      geoFencesJson。
        · 必须是 JSON **数组** [{"id":"2351","points":[{lat,lon}]}]。
          填成对象 {"updateTime":…,"geoFences":[…]} 会让 App 的 Gson 抛
          BEGIN_ARRAY but was BEGIN_OBJECT，drawGeoFence 中断，**连轨迹都不画**。
        · ★ 但它**不足以**让详情页画出围栏：真机 A/B 试过 5 种变体
          （数组 / 服务端原样 / freedomShowFence=true / runAreaId=2351 /
          计分跑 + 自由跑），围栏与打卡点标记**始终未出现**。
          即「详情页地图的围栏层另有数据源」，尚未定位 —— 详见
          `.workbuddy-ai/memory/MEMORY.md` 的「围栏」段。
          保留该字段是因为它确实是 App 的真实字段、格式已正确，
          且不影响其它任何渲染。
    """
    pts = [conv_point(p, start_ms) for p in points]
    run_wrap = {"allLocJson": json.dumps(pts, separators=(",", ":"),
                                        ensure_ascii=False),
                "useZip": False}
    sp, stf = build_windows(points, start_ms, total_time, rrid)
    laps = build_laps(points, start_ms)
    five = five_point_payload(list(fixed_points or []), start_ms)
    fx = {"fivePointJson": json.dumps(five, separators=(",", ":"),
                                      ensure_ascii=False),
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
        "segment_json": gz2_str(""),
        "speed_json": gz2_json(sp),
        "step_freq_json": gz2_json(stf),
        "laps_json": gz2_json(laps),
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

    fixed_points：服务端下发的打卡点（自由跑传 [] / 不传 → fivePointJson="[]"）。
    geo_fences  ：服务端下发的围栏（不传 → geoFencesJson="[]"，详情页无围栏）。
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

    # 6) ★★ 五点来源：轨迹点【不得】出现在 fixed_point_json 里
    fx = json.loads(_gz.decompress(
        base64.b64decode(obj["fixed_point_json"])).decode("utf-8"))
    five_free = json.loads(fx["fivePointJson"])
    print("  [自由跑] fixed_point_json 点位数=%d (期望 0)" % len(five_free))
    print("  %s 自由跑 fivePointJson == \"[]\"（无点位）"
          % ("OK " if len(five_free) == 0 else "FAIL"))
    ok &= len(five_free) == 0

    cps = [{"pointName": "一号点", "lat": 22.98, "lon": 116.33,
            "glat": 22.981, "glon": 116.335, "radius": 15.0, "isFixed": 1},
           {"pointName": "二号点", "lat": 22.99, "lon": 116.34,
            "glat": 22.991, "glon": 116.345, "radius": 15.0, "isFixed": 0}]
    obj2 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10,
                            fixed_points=cps)
    fx2 = json.loads(_gz.decompress(
        base64.b64decode(obj2["fixed_point_json"])).decode("utf-8"))
    five2 = json.loads(fx2["fivePointJson"])
    good2 = (len(five2) == 2 and five2[0]["pointName"] == "一号点"
             and five2[0]["isFixed"] == 1 and "lon" in five2[0]
             and "lng" not in five2[0])
    print("  [计分跑] fixed_point_json 点位数=%d (期望 2)" % len(five2))
    print("  %s 计分跑五点 = 真实打卡点（pointName/isFixed/lon 正确）"
          % ("OK " if good2 else "FAIL"))
    ok &= good2

    # 7) id 规则（speed_json 是双层包裹，要解两层）
    print("  speed_json[0].id 规则: (rrid%%100000)*1000+hi")
    spj = json.loads(_gz.decompress(base64.b64decode(
        _gz.decompress(base64.b64decode(obj["speed_json"])).decode("utf-8"))).decode("utf-8"))
    exp_id = (1322680573 % 100000) * 1000 + 10
    print("  %s id=%d (期望 %d)" % ("OK " if spj[0]["id"] == exp_id else "FAIL",
                                    spj[0]["id"], exp_id))
    ok &= spj[0]["id"] == exp_id

    # 8) ★★ 围栏：geoFencesJson 必须是 JSON **数组**（2026-09-27 真机定因）
    gf_raw = [{"id": 2351, "name": "围栏",
               "points": [{"lon": 0.0, "lat": 0.0, "glon": 116.330092,
                           "glat": 22.982321, "pointsNumber": 1}]}]
    obj3 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10,
                            fixed_points=cps, geo_fences=gf_raw)
    fx3 = json.loads(_gz.decompress(
        base64.b64decode(obj3["fixed_point_json"])).decode("utf-8"))
    gj3 = json.loads(fx3["geoFencesJson"])
    c_f1 = isinstance(gj3, list)                       # ← 关键：数组不是对象
    c_f2 = (len(gj3) == 1 and gj3[0]["id"] == "2351"
            and gj3[0]["points"][0]["lat"] == 22.982321     # glat 兜底
            and gj3[0]["points"][0]["lon"] == 116.330092)   # glon 兜底
    print("  [围栏] geoFencesJson 顶层类型 = %s (期望 list)"
          % type(gj3).__name__)
    print("  %s 围栏是 JSON 数组（App Gson 要求 BEGIN_ARRAY）"
          % ("OK " if c_f1 else "FAIL"))
    print("  %s 顶点用 glat/glon 兜底、id 转字符串" % ("OK " if c_f2 else "FAIL"))
    ok &= (c_f1 and c_f2)

    # 不传围栏 → 回到 "[]"（旧行为），且仍然是数组
    obj4 = build_obs_object(pts, rrid=1322680573, uuid="UUID-T", uid=12345678,
                            start_ms=1789534834000, total_time=10)
    fx4 = json.loads(_gz.decompress(
        base64.b64decode(obj4["fixed_point_json"])).decode("utf-8"))
    c_f3 = json.loads(fx4["geoFencesJson"]) == []
    print("  %s 不传围栏 → geoFencesJson=\"[]\"" % ("OK " if c_f3 else "FAIL"))
    ok &= c_f3

    # ★ 变异自检：把「空围栏」伪造成对象形状，必须不再通过数组断言
    c_f4 = isinstance(json.loads(json.dumps({"updateTime": 1, "geoFences": []})),
                      list) is False
    print("  %s 变异自检：对象形状不被当成数组" % ("OK " if c_f4 else "FAIL"))
    ok &= c_f4

    # 9) ★★ 列表型四键必须是「双层包裹」（App 会再 gunzip 一次）
    #    真机症状：单层 → ZipException: Not in GZIP format → 详情页「每公里数据」不渲染
    list_keys = ("laps_json", "speed_json", "step_freq_json", "segment_json")
    layer_ok = {}
    for k in list_keys:
        try:
            _unwrap1(_unwrap1(obj[k]))          # 解两层必须成功
            layer_ok[k] = True
        except Exception:
            layer_ok[k] = False
    print("  %s 列表型四键可解两层 %s"
          % ("OK " if all(layer_ok.values()) else "FAIL",
             {k: ("2层" if v else "不足2层") for k, v in layer_ok.items()}))
    ok &= all(layer_ok.values())

    # 解两层后必须是合法 JSON / 空串
    laps2 = json.loads(_unwrap1(_unwrap1(obj["laps_json"])))
    c_l1 = isinstance(laps2, list) and len(laps2) >= 1
    print("  %s laps_json 解两层 == JSON 数组(%d 圈)"
          % ("OK " if c_l1 else "FAIL", len(laps2) if isinstance(laps2, list) else -1))
    ok &= c_l1
    c_l2 = _unwrap1(_unwrap1(obj["segment_json"])) == ""
    print("  %s segment_json 解两层 == 空串" % ("OK " if c_l2 else "FAIL"))
    ok &= c_l2

    # 单层键必须【只】解一层（多解一层要失败）—— 防止把全部键都改成双层
    single_ok = True
    for k in ("rrid", "run_data", "fixed_point_json"):
        try:
            _unwrap1(_unwrap1(obj[k]))           # 再解一层应当炸
            single_ok = False
        except Exception:
            pass
    print("  %s 单层键（rrid/run_data/fixed_point_json）只解一层"
          % ("OK " if single_ok else "FAIL"))
    ok &= single_ok

    # ★ 变异自检：故意把 laps_json 退回单层，断言必须能抓到
    bad = dict(obj)
    bad["laps_json"] = gz_json(laps2)          # 退回单层（即修复前的线上行为）
    try:
        _unwrap1(_unwrap1(bad["laps_json"]))
        caught = False
    except Exception:
        caught = True
    print("  %s 变异自检：单层 laps_json 会被抓出" % ("OK " if caught else "FAIL"))
    ok &= caught

    print("=" * 62)
    print("汇总: %s" % ("全部通过" if ok else "存在失败"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)

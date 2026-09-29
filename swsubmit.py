#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swsubmit.py — 运动世界校园 · 跑步记录提交模块

复刻 Android 端 POST /api/v70260/runnings/save/record 的完整请求：
  · runes 头 = policy_ts + uid
  · runef 头 = run_uuid + start_ms
  · body 31+ 字段 + signature / originalSign
  · speedPerTenSec / stepsPerTenSec 10 秒窗
  · 三层信封加密

用法（被 swcli.py submit 子命令调用）：
    python swsubmit.py --track generator/output/xxx.json --dry-run
    python swsubmit.py --track generator/output/xxx.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swclient as sw

RECORD_PATH = "/api/v70260/runnings/save/record"
POLICY_PATH = "/api/v70103/runModePolicy"
FENCE_PATH = "/api/v1/getGeoFenceForRun"
SIGN_SALT = "2slhe02lsfiwowlcixisla_sls-_slaor"

# UploadSignEntity 声明顺序（31 字段）
UPLOAD_SIGN_FIELD_ORDER = [
    "sportType", "totalTime", "totalDis", "speed", "startTime", "stopTime",
    "complete", "selDistance", "unCompleteReason", "getPrize", "status",
    "uuid", "uid", "avgStepFreq", "totalSteps", "selectedUnid", "calorie",
    "policy", "selRunTime", "validDis", "validTime", "useMobilityTools",
    "errorCode", "geeToken", "unauthorized", "themeId", "faceCheck",
    "goalId", "address", "avgPower", "totalAscent",
]


# ══════════════════════════════════════════════════════════════════
# 签名（Java String.valueOf 语义）
# ══════════════════════════════════════════════════════════════════
def _android_value(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, float):
        # Java String.valueOf(double)：整数显示为 x.0
        if v == int(v) and abs(v) < 1e15:
            return "%.1f" % v
        return repr(v)
    return str(v)


def _build_sign_map(values: dict, has_room_id: bool = False):
    m = []
    for k in UPLOAD_SIGN_FIELD_ORDER:
        if k in values:
            m.append((k, _android_value(values[k])))
    if has_room_id and "roomId" in values:
        m.append(("roomId", _android_value(values["roomId"])))
    return [(k, v) for (k, v) in m if k.lower() != "signature"]


def original_sign(values: dict, has_room_id: bool = False) -> str:
    m = _build_sign_map(values, has_room_id)
    m.sort(key=lambda kv: kv[0])                       # 自然序
    return "&".join("%s=%s" % (k, v) for k, v in m)


def signature(values: dict, has_room_id: bool = False) -> str:
    m = _build_sign_map(values, has_room_id)
    m.sort(key=lambda kv: kv[0].lower())                # compareToIgnoreCase
    q = "&".join("%s=%s" % (k, v) for k, v in m)
    return sw.md5_hex((q + SIGN_SALT).encode("utf-8"))


# ══════════════════════════════════════════════════════════════════
# 数值公式
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
# 五点（服务端打卡点 → 提交 body 的 fivePointJson wrapper 串）
# ══════════════════════════════════════════════════════════════════
def five_point_payload(points: list, start_ms: int) -> list:
    """五点实体（跑完态 isPass=true）。

    ★★★ 2026-09-28 真机 ground truth（5 条真机记录逐一比对，务必读）★★★
      真机 App 自己写的记录里，`fivePointJson` 每个元素的键是：

          flag, glat, glon, id, isFixed, isPass, lat, **lng**, pointName,
          position, state          ← 注意是 `lng`，**没有** radius

      我们旧版写的是 `lat` + **`lon`** ⇒ App 的 Bean 字段 `lng` 取不到值
      ⇒ 经度 = 0 ⇒ 每个打卡点都被画到 **(lat, 0)**（几内亚湾，屏幕外）
      ⇒ 详情页上一个绿底 ✓ 圆标都不出现。
      实测：真机记录 148~152 个点的 `lng` 全有值；我们 5 个点全是 `lon`。

      ★ 别和「服务端打卡点接口」搞混：`getFixedPoint*` 返回的点位用 `lon`，
        记录侧 `fivePointJson` 用 `lng` —— 两套实体、两个字段名（串表里
        `, lon=` 与 `, lng=` 都存在，分别属于不同 Bean）。

    glat/glon 优先用服务端下发的 GCJ 坐标（存在时），否则用 lat/lon 转；
    pointName/isFixed/radius 原样带；position 固定 999（跑完态不在途）。
    """
    out = []
    for i, p in enumerate(points):
        lat = float(p.get("lat", 0) or 0)
        lon = float(p.get("lon", p.get("lng", 0)) or 0)
        glat = float(p.get("glat", p.get("gLat", 0)) or 0)
        glon = float(p.get("glon", p.get("gLng", 0)) or 0)
        if not (glat or glon) and (lat or lon):
            # 无服务端 GCJ 坐标时本地转一次（WGS-84 → GCJ-02）
            try:
                import swobs
                glat, glon = swobs.wgs84_to_gcj02(lat, lon)
            except Exception:
                glat, glon = lat, lon
        out.append({
            "flag": start_ms,
            "glat": round_to(glat, 7),
            "glon": round_to(glon, 7),
            "id": i + 1,
            "isFixed": int(p.get("isFixed", 0) or 0),
            "isPass": True,
            "lat": round_to(lat, 7),
            # ★ 真机字段名是 `lng`（不是 `lon`）—— 写错经度就变 0，打卡点画到 (0,0)
            "lng": round_to(lon, 7),
            "pointName": p.get("pointName", "") or "",
            "position": 999,
            "radius": float(p.get("radius", 0) or 0),
            "state": 0,
        })
    return out


def five_point_wrapper(points: list, start_ms: int,
                       geo_fences: list = None) -> str:
    """提交 body 的 fivePointJson 包装串（计分跑用）。

    仅当 mode=score 且有真实打卡点时调用；自由跑【不传】此字段
    （用户真机确认：自由跑无围栏、无打卡点）。

    geo_fences：服务端 getGeoFenceForRun 下发的围栏，用于填 `geoFencesJson`。
      ★ 不传 → 退回 "[]"（旧行为，详情页无围栏可画）。
    """
    five = five_point_payload(points, start_ms)
    return json.dumps({
        "useZip": False,
        "fivePointJson": json.dumps(five, separators=(",", ":"),
                                    ensure_ascii=False),
        "runAreaId": -1,
        "geoFencesJson": geo_fences_json(geo_fences),
        "freedomShowFence": False,
    }, separators=(",", ":"), ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
# 围栏（geoFencesJson）—— 详情页「围栏范围」的数据源
# ══════════════════════════════════════════════════════════════════
def norm_geo_fences(geo_fences: list) -> list:
    """把服务端 getGeoFenceForRun 的 geoFences 规整成记录侧结构。

    ★★ 2026-09-27 真机定因（两轮判别实验，务必读清楚）★★

    **字段类型**：`fixed_point_json` 被 `Gson.fromJson(text, Bean)` 反序列化，
    而该 Bean 的 `fivePointJson` / `geoFencesJson` 两个字段**都声明为 String**。
    真机 logcat 逐字段实证：
      · 两个都写数组 → `Expected a string but was BEGIN_ARRAY
                       … path $.fivePointJson`（fivePointJson 排在前，先炸）
      · five=str / geo=array → 同样的异常，但 path 变成 `$.geoFencesJson`
    ⇒ **两个字段都必须是「字符串」，字符串内容才是 JSON 数组文本** `[{…}]`。
      （`path $` ≠ 「值要被单独 fromJson」；真实 App 自己写的记录里这两个
        字段也全是 str，见 `_gh_tools/dump_records.py` 的字段类型 dump。）

    ★ 教训（同类错误第 4 次「对照组不成立」）：`fivePointJson` 在 JSON 里
      排在 `geoFencesJson` 前面 ⇒ 只要它类型错，解析就在它那一步中断，
      `geoFencesJson` 的类型**从未被真正验证**。之前据此推出的
      「两个都是数组」是错的；本轮把 five 改回 str 后，错误 path 立刻
      移到 `$.geoFencesJson`，才把它单独验出来。

    ★★★ 点位字段名：**必须是客户端 Bean 的 `lng` / `glng`**（2026-09-28 真机定因）★★★
      真机 App 自己用的那份围栏来自 `runModePolicy(runMode=1)` 的 `data.geoFence`：

          {"lat":0.0,"lng":0.0,"glat":22.982321,"glng":116.330092}

      ⇒ 客户端 `GeoFencesPointBean` 的字段名是 **`lng` / `glng`**，
        且 `lat/lng` 恒 0.0、真值全在 `glat/glng`（GCJ-02）。

      ⚠ 服务端 `getGeoFenceForRun` 返回的却是 `lon`/`glon` —— **那是服务端 DTO**，
        客户端 Bean 读不到 `glon` ⇒ 经度 = 0 ⇒ 十个顶点塌到 `(lat, 0)` ⇒
        多边形退化 ⇒ **Gson 解析成功、不抛异常、却什么也画不出来**。
        这与 `fivePointJson` 的 `lon`→`lng` 是同一类错误（两套 Bean、两套字段名）。

      真机 A/B（同一条 policy=1 记录，只改这一个变量）：
        · `glat`+`glon`（旧写法）→ 无围栏
        · `glat`+`glng`（本写法）→ **围栏多边形正常出现** ✅

      ★ 注意别和 `fivePointJson` 混：那边的点是
        `{flag, glat, glon, id, isFixed, isPass, lat, lng, pointName, position, state}`
        —— 用的是 **`glon`**（已验证能画）。两个 Bean 的字段名不同，别互抄。

    ★ id 转字符串：串表里 `GeoFencesBean{id='` 带引号（String 字段）。
    """
    out = []
    for f in geo_fences or []:
        if not isinstance(f, dict):
            continue
        pts = []
        for i, p in enumerate(f.get("points") or []):
            if not isinstance(p, dict):
                continue
            glat = p.get("glat") or p.get("lat") or 0.0
            glon = p.get("glon") or p.get("glng") or p.get("lon") or p.get("lng") or 0.0
            try:
                glat, glon = float(glat), float(glon)
            except (TypeError, ValueError):
                continue
            if glat == 0.0 and glon == 0.0:
                continue
            pts.append({
                # ★ 照抄 runModePolicy 里 geoFence 的原始键名与取值：
                #   lat/lng 恒 0.0，真值在 glat/glng（GCJ-02）
                "lat": 0.0,
                "lng": 0.0,
                "glat": round(glat, 7),
                "glng": round(glon, 7),
                "pointsNumber": i + 1,
            })
        if not pts:                             # 空围栏不落盘（画不出任何东西）
            continue
        item = {"id": str(f.get("id")), "points": pts,
                "pointsNumber": len(pts)}
        if f.get("name") is not None:
            item["name"] = f.get("name")
        out.append(item)
    return out


def geo_fences_json(geo_fences: list) -> str:
    """记录侧 `geoFencesJson` **字符串**（内容为 JSON 数组文本）。

    ★ OBS 的 `fixed_point_json.geoFencesJson` 就必须是这个字符串形态
      （Bean 字段声明为 String）——真机定因见 `norm_geo_fences`。
    """
    return json.dumps(norm_geo_fences(geo_fences), separators=(",", ":"),
                      ensure_ascii=False)


def geo_fences_from_policy(pd: dict) -> list:
    """从 `runModePolicy` 的响应里取围栏 —— **这是真机 App 自己用的那份**。

    ★★★ 2026-09-28 真机定因（关键）★★★
      `POST /api/v70103/runModePolicy` 的 `data.geoFence` 形如

          {"updateTime": 1790305529784,
           "geoFences": [{"id":2351,"name":"围栏","points":[
                 {"lat":0.0,"lng":0.0,"glat":22.982321,"glng":116.330092}, …]}]}

      这才是 App 开跑前拿到、并用于详情页画围栏的那份（`runMode=1` 时非空）。
      而 `getGeoFenceForRun` 实测**返回空** ⇒ 我们旧代码恒得到 `[]`
      ⇒ 记录里 `geoFencesJson="[]"` ⇒ 详情页没有围栏可画（只能靠手改 OBS 验证）。

      ⇒ 提交时**优先用这里**；`fetch_geo_fences` 降级为兜底。
    """
    if not isinstance(pd, dict):
        return []
    gf = pd.get("geoFence")
    if isinstance(gf, str):
        try:
            gf = json.loads(gf)
        except Exception:
            return []
    if not isinstance(gf, dict):
        return []
    fences = gf.get("geoFences")
    if not isinstance(fences, list) or not fences:
        return []
    return norm_geo_fences(fences)


def fetch_geo_fences(call_fn, verbose: bool = True) -> list:
    """拉学校围栏（**兜底**）：POST /api/v1/getGeoFenceForRun，body "{}"。

    ★ 首选 `geo_fences_from_policy(runModePolicy 的 data)` —— 实测这个接口
      在本校区返回空，只有 runModePolicy 里那份才是 App 真正用的（见上）。

    ★ 任何失败都返回 []（提交照旧进行），只在 verbose 时说明原因 ——
      围栏缺失只影响详情页画不画围栏，绝不该让一次跑步提交失败。
    """
    try:
        _, biz, err, _raw = call_fn("POST", FENCE_PATH, "{}", verbose=False)
    except Exception as e:
        if verbose:
            print("  [围栏] 拉取异常（按无围栏继续）: %s" % str(e)[:120])
        return []
    if not isinstance(biz, dict):
        if verbose:
            print("  [围栏] 无业务体（按无围栏继续）: %s" % str(err)[:120])
        return []
    data = biz.get("data")
    fences = data.get("geoFences") if isinstance(data, dict) else None
    if not isinstance(fences, list) or not fences:
        if verbose:
            print("  [围栏] 服务端未下发围栏（error=%s）→ geoFencesJson=\"[]\""
                  % biz.get("error"))
        return []
    got = norm_geo_fences(fences)
    if verbose:
        print("  [围栏] 下发 %d 个围栏 / %d 个顶点 → geoFencesJson 已填充"
              % (len(got), sum(len(g["points"]) for g in got)))
    return got


def round_to(v: float, nd: int) -> float:
    """Rust/Java 风格四舍五入（half-up on abs）"""
    f = 10.0 ** nd
    return math.floor(abs(v) * f + 0.5) / f * (1 if v >= 0 else -1)


def avg_power(weight: float, total_dis: float, total_time: int) -> int:
    """平均功率（瓦）近似：官方口径 w = 体重 * 距离(m) / 时间(s) 的功率换算"""
    if total_time <= 0:
        return 0
    # 参照 NekoSportsWorldTool/src/track/calorie.rs 口径
    speed = total_dis / total_time
    if speed <= 0:
        return 0
    # MET 近似：功率 = 体重 * v * 1.0（走路/跑步转化）
    return int(round_to(weight * speed * 0.98, 0))


def official_kcal(weight: float, total_time: int, total_dis: float) -> int:
    """官方卡路里口径（kJ→kcal），参照 calorie.rs"""
    if total_time <= 0 or weight <= 0:
        return 0
    speed = total_dis / total_time
    # 简化 MET：跑步 1.05 kcal/kg/km
    km = total_dis / 1000.0
    return int(round_to(weight * km * 1.036, 0))


def _track_stream(points: list) -> list:
    """把轨迹点列压成 [(dt, dd, ds), ...] 的分段流（秒 / 米 / 步）"""
    segs = []
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        t0 = float(a.get("t_rel", a.get("ts", 0)) or 0)
        t1 = float(b.get("t_rel", b.get("ts", 0)) or 0)
        if t1 <= t0:
            continue
        segs.append([t1 - t0,
                     float(b.get("dist", 0) or 0) - float(a.get("dist", 0) or 0),
                     float(b.get("steps", 0) or 0) - float(a.get("steps", 0) or 0)])
    return segs


def _stream_reader(segs: list):
    """按时间从分段流里取量（跨段自动结转）"""
    cur = {"i": 0, "rem": list(segs[0]) if segs else [0.0, 0.0, 0.0]}

    def take(sec: float):
        d = s = 0.0
        left = sec
        while left > 1e-9:
            if cur["i"] >= len(segs):
                break
            rt, rd, rs = cur["rem"]
            if rt <= 1e-9:
                cur["i"] += 1
                if cur["i"] < len(segs):
                    cur["rem"] = list(segs[cur["i"]])
                continue
            k = left if left < rt else rt
            f = k / rt
            d += rd * f
            s += rs * f
            cur["rem"] = [rt - k, rd - rd * f, rs - rs * f]
            left -= k
            if cur["rem"][0] <= 1e-9:
                cur["i"] += 1
                if cur["i"] < len(segs):
                    cur["rem"] = list(segs[cur["i"]])
        return d, s

    return take


def android_tensec(points: list, start_ms: int, total_time: int, kind: str,
                   rrid: int = 0, queue_num: str = "seq") -> list:
    """10 秒窗 speedPerTenSec / stepsPerTenSec。

    ★ id 规则（新版 App）：
        id = (rrid % 100000) * 1000 + 窗口右边界秒数
      旧版用全局序号 60000+n，服务端不报错但详情页轨迹会异常。

    ★★ 窗口量必须按「整 10 秒配额结转」累加（2026-09-24 修复）
      历史 bug：旧实现用**就近吸附** —— 取 `t_rel <= lo` 的最后一个采样点
      当窗起点、`t_rel <= hi` 的最后一个点当窗终点。采样间隔 ~5s 时两端各
      带最多一个采样间隔的偏差，于是"10 秒窗"实际只覆盖 3~15 秒的位移：
      实测窗内配速在 3'34"~15'12" 之间乱跳，**均值比目标慢 22 s/km** ——
      这正是用户看到的「实时配速表对不上」。
      真机是 1Hz 采样，吸附误差 ≤1s，所以看不出问题；正确口径是
      **窗口内的真实位移**：把轨迹按时间切成 10 秒配额，跨窗的部分结转到下一窗。
      （与 NekoSportsWorldTool/src/track/generator.rs 的 ten_d/ten_t 结转一致。）

    queue_num：提交体用窗口序号（历史行为），OBS 侧用 0（真机样本口径）。
    """
    segs = _track_stream(points)
    take = _stream_reader(segs)
    out = []
    seed = ((rrid % 100000) * 1000) if rrid else 60000
    n_win = int(total_time // 10)
    for k in range(n_win):
        lo = k * 10
        hi = min(lo + 10, total_time)
        dist, steps_n = take(float(hi - lo))
        dist = round_to(max(dist, 0.0), 4)
        steps_n = int(max(steps_n, 0.0))
        begin = start_ms + lo * 1000
        end = start_ms + hi * 1000
        qn = k if queue_num == "seq" else 0
        win_id = seed + hi
        if kind == "speed":
            out.append({"beginTime": begin, "distance": dist, "endTime": end,
                        "flag": start_ms, "id": win_id, "queueNum": qn,
                        "state": 0})
        else:
            out.append({"avgDiff": 0.0, "beginTime": begin, "endTime": end,
                        "flag": start_ms, "id": win_id, "maxDiff": 0.0,
                        "minDiff": 1000.0, "queueNum": qn, "state": 0,
                        "stepsNum": steps_n})
    return out


def window_distance_sum(points: list, total_time: int) -> float:
    """10 秒窗距离累计（与 android_tensec 同口径，用于自洽校验）

    ★ 必须与 android_tensec 用同一套「整 10 秒配额结转」口径，否则校验值
      会与真实提交的窗口距离对不上（旧版这里也是就近吸附）。
    """
    take = _stream_reader(_track_stream(points))
    tot = 0.0
    for k in range(int(total_time // 10)):
        lo = k * 10
        hi = min(lo + 10, total_time)
        d, _ = take(float(hi - lo))
        tot += max(d, 0.0)
    return tot


# ══════════════════════════════════════════════════════════════════
# 轨迹 → 提交点（补 ts 相对秒与累计步数）
# ══════════════════════════════════════════════════════════════════
def prep_points(track: dict) -> tuple:
    """把生成器输出转成提交所需的点位。

    返回 (points, total_dis, total_time, total_steps, start_ms)
    生成器 points 字段：i, ts, lat, lon, ele, speed, cadence, stride_cm, hr, dist, seg_m
    """
    pts = track.get("points") or []
    if not pts:
        raise ValueError("轨迹无 points 数据")
    start_ms = int(pts[0]["ts"])
    norm = []
    for p in pts:
        t_rel = (int(p["ts"]) - start_ms) / 1000.0
        cadence = float(p.get("cadence", 0) or 0)
        q = dict(p)
        q["t_rel"] = t_rel
        q["lat"] = float(p.get("lat", 0))
        q["lon"] = float(p.get("lon", 0))
        q["dist"] = float(p.get("dist", 0))
        q["ele"] = float(p.get("ele", 0) or 0)
        # 累计步数：用 cadence(步/分) × 时间步长积分近似
        q["_cadence"] = cadence
        norm.append(q)

    # 累计步数积分（梯形法）
    total_steps = 0.0
    for i, q in enumerate(norm):
        if i == 0:
            q["steps"] = 0.0
            continue
        dt = q["t_rel"] - norm[i - 1]["t_rel"]
        total_steps += (norm[i - 1]["_cadence"] + q["_cadence"]) / 2.0 / 60.0 * dt
        q["steps"] = total_steps

    last = norm[-1]
    total_dis = float(last["dist"])
    total_time = int(round(last["t_rel"]))
    return norm, total_dis, total_time, int(round(total_steps)), start_ms


# ★ 高程噪声阈值（米）：单步上升不超过此值视为 GPS/气压高程抖动，不计入爬升。
#   必须与生成器 `generator/rungen/engine.py` 同口径（`_build_record` 与
#   `_make_split_span` 都用 0.15），否则「界面显示的总爬升」与「实际上传的
#   totalAscent」会对不上 —— 实测前者 41.3 / 后者 42.0，差 1~2%。
ELE_NOISE_M = 0.15


def ele_gain(eles) -> float:
    """按【单步阈值】累计爬升 —— 全项目唯一实现。

    只有单步上升 > ELE_NOISE_M 才计入，且计入的是**整段上升**（不减掉阈值），
    与 engine.py 的 `if d > 0.15: asc += d` 逐字一致。

    ★ 提交体 `totalAscent` 与 OBS `laps[].elevationGain` 都走这里。
      本项目已因「同一语义多份实现、口径悄悄漂移」踩过两次（配速解析、圈时长），
      所以这里刻意只留一份。

    ★ 结果归整到 2 位小数：调用方传入的 `ele` 都是 2 位小数（core.py 序列化时
      `round(p.ele, 2)`），数学真值必然是 2 位小数；但浮点累加会带来 ~1e-14 噪声，
      足以让恰好等于 x.50 的值偏到下方，使 half-up 取整**少 1**
      （实测 raw=15.499999999999993 → 取整 15，而真值 15.5 应取整 16）。
      归整后即为精确值，消除这一处刀锋级不确定。
    """
    g = 0.0
    prev = None
    for e in eles:
        e = float(e or 0.0)
        if prev is not None and e - prev > ELE_NOISE_M:
            g += e - prev
        prev = e
    return round_to(g, 2)


def total_ascent(points: list) -> float:
    """轨迹总爬升（口径见 `ele_gain`）"""
    return ele_gain([p.get("ele", 0) for p in points])


# ══════════════════════════════════════════════════════════════════
# 组装提交体
# ══════════════════════════════════════════════════════════════════
def build_record_body(track: dict, *, uid: int, unid: int, policy: int,
                      policy_ts: int, min_distance: int, weight: float = 65.0,
                      face_check: int = 0, address: str = "",
                      five_point_json: str = "", sport_type: int = 1,
                      with_steps: bool = True, rrid: int = 0) -> dict:
    """组装提交体。

    with_steps：恒为 True（自由跑与计分跑【都要】完整步频步幅图表，
    这是给详情页 步频/步幅 图表用的数据源）。历史 False 分支已废弃。
    """
    points, total_dis, total_time, total_steps_raw, start_ms = prep_points(track)
    stop_ms = start_ms + total_time * 1000
    ascent = total_ascent(points)

    # ★ 先生成 10 秒窗，再让 totalSteps / totalDis 与窗口严格自洽。
    #   服务端常见校验：sum(stepsPerTenSec) == totalSteps。
    sp_ten = android_tensec(points, start_ms, total_time, "speed", rrid=rrid)
    st_ten = android_tensec(points, start_ms, total_time, "steps", rrid=rrid)
    win_dis = sum(x["distance"] for x in sp_ten)
    win_steps = sum(x["stepsNum"] for x in st_ten)

    # 距离：窗口累计与轨迹总距离取整后应对齐；
    # 若窗口插值截断（点稀疏），以轨迹真实距离为准。
    total_dis_i = int(round_to(max(win_dis, total_dis), 0))
    total_steps = int(win_steps) if win_steps > 0 else total_steps_raw

    # 步频步幅：自由跑与计分跑都要上传（详情页图表依赖），恒带
    total_steps = int(win_steps) if win_steps > 0 else total_steps_raw

    power = avg_power(weight, total_dis_i, total_time)
    kcal = official_kcal(weight, total_time, total_dis_i)

    run_uuid = str(uuid.uuid4()).upper()
    dis_ceil = math.ceil(total_dis_i * 100.0) / 100.0
    # ★★★ 2026-09-29 修正：`speed` 字段 = **毫-分/公里**（×1000），不是 ×1024。
    #   现象（用户报告）：「跑步数据总览」的平均配速 与 「配速（分/公里）」图的平均配速 对不上。
    #   真机截图实证（记录 1327343591，11:47 / 2.09km）：
    #       总览 平均配速 = 5'47"   ← 来自本字段
    #       配速图 平均配速 = 5'38"  ← App 自己按 距离/时间 算，是对的
    #   5'47" / 5'38.3" = 1.0240 —— 正是 1024/1000 这个比例。
    #   根因：旧实现写 `round(pace_min, 2) * 1024`，而**服务端把该字段当「毫-分/公里」读**
    #     （÷1000），于是显示值恒为真实配速的 1.024 倍（5'40" 显示成 5'48"，慢 8 秒）。
    #   证据（6 条历史记录，逐条可复现）：服务端返回的 `speed` 恰好 == 我们提交的整数 / 1000，
    #     例：提交 5806 → 返回 5.806；提交 6021 → 返回 6.021。6/6 精确吻合，不是巧合。
    #   ★ 1024 的出处：`NekoSportsWorldTool` 的 Rust 源码（`round(...)*1024`），
    #     我们当初「从源码确认、尚未实测」就照抄了 —— 属于典型的「没实测的二手结论」。
    #   ★ 精度：改成对「毫」取整（等价于把 pace_min 保留 3 位小数），
    #     比旧的 `round(...,2)` 更准，且避免 `int(x*1000)` 的浮点截断（5.667*1000=5666.999…）。
    speed = int(round(total_time / dis_ceil * 50.0 / 3.0 * 1000.0)) if dis_ceil else 0
    avg_step_freq = max(1, int(round_to(total_steps / total_time * 60.0, 0))) if (total_time and total_steps) else 0

    body = {
        "allLocJson": "",
        "sportType": sport_type,
        "policy": policy,
        "totalTime": total_time,
        "startTime": start_ms,
        "stopTime": stop_ms,
        "getPrize": False,
        "status": 0,
        "uuid": run_uuid,
        "uid": uid,
        "selectedUnid": unid,
        "selRunTime": total_time,
        "selDistance": min_distance,
        "totalDis": total_dis_i,
        "speed": speed,
        "validDis": total_dis_i,
        "validTime": total_time,
        "complete": True,
        "unCompleteReason": 0,
        "calorie": kcal,
        "useMobilityTools": 0,
        "faceCheck": face_check,
        "totalAscent": int(round_to(ascent, 0)),
        "avgPower": power,
        "speedPerTenSec": sp_ten,
        "isUpload": False,
        "more": False,
        "latitude": 0.0,
        "longitude": 0.0,
        "maxRunTime": 0,
        "minSteps": 0,
        "errorCode": 0,
        "geeToken": "",
        "unauthorized": 0,
        "themeId": 0,
        "goalId": None,
        "address": address,
    }
    if five_point_json:
        body["fivePointJson"] = five_point_json
    # 步频步幅恒带（自由跑/计分跑都需完整图表数据）
    body["totalSteps"] = total_steps
    body["avgStepFreq"] = avg_step_freq
    body["stepsPerTenSec"] = st_ten

    body["signature"] = signature(body, False)
    body["originalSign"] = original_sign(body, False)
    meta = {"uuid": run_uuid, "start_ms": start_ms, "total_dis": total_dis_i,
            "total_time": total_time, "total_steps": total_steps,
            "avg_step_freq": avg_step_freq, "speed": speed, "calorie": kcal,
            "avg_power": power, "ascent": ascent, "sport_type": sport_type,
            "with_steps": with_steps,
            "win_dis_sum": win_dis, "win_steps_sum": win_steps,
            "points": points}
    return body, meta


# ══════════════════════════════════════════════════════════════════
# 本地自检（签名测试向量）
# ══════════════════════════════════════════════════════════════════
def selftest() -> bool:
    sample = {
        "sportType": 3, "totalTime": 1000, "totalDis": 1200, "speed": 1200,
        "startTime": 1700000000000, "stopTime": 1700000001000,
        "complete": True, "selDistance": 1500, "unCompleteReason": 0,
        "getPrize": False, "status": 1, "uuid": "test-uuid", "uid": 13056447,
        "avgStepFreq": 134, "totalSteps": 1000, "selectedUnid": 57501,
        "calorie": 0, "policy": 0, "selRunTime": 0, "validDis": 1100,
        "validTime": 900, "useMobilityTools": 0, "errorCode": 0,
        "geeToken": "", "unauthorized": 0, "themeId": 0, "faceCheck": 1,
        "goalId": None, "address": "", "avgPower": 0, "totalAscent": 0,
        "roomId": 1001,
    }
    ok = True
    s1 = signature(sample, True)
    exp1 = "f2b958b2b9c8e99c4156076fbc72aabe"
    print("  %s signature(含 roomId) = %s (期望 %s)" % ("OK " if s1 == exp1 else "FAIL", s1, exp1))
    ok &= s1 == exp1

    s2 = signature(sample, False)
    exp2 = "6187185669bbd60d0c9ff4148f33e16d"
    print("  %s signature(不含 roomId) = %s (期望 %s)" % ("OK " if s2 == exp2 else "FAIL", s2, exp2))
    ok &= s2 == exp2

    o1 = original_sign(sample, True)
    pref = "address=&avgPower=0&avgStepFreq=134&calorie=0&complete=true&errorCode=0&faceChec"
    print("  %s originalSign 前缀" % ("OK " if o1.startswith(pref) else "FAIL"))
    ok &= o1.startswith(pref)
    print("  %s originalSign 含 goalId=null" % ("OK " if "goalId=null" in o1 else "FAIL"))
    ok &= "goalId=null" in o1

    # ★★ 围栏：来源 = runModePolicy 的 data.geoFence（App 自己用的那份），
    #    输出键名 = 客户端 Bean 的 glat/glng（不是服务端 DTO 的 glon）。
    #    2026-09-28 真机 A/B 定因；这里正反两面都守。
    pd = {"policy": 1, "geoFence": {
        "updateTime": 1790305529784,
        "geoFences": [{"id": 2351, "name": "围栏", "points": [
            {"lat": 0.0, "lng": 0.0, "glat": 22.982321, "glng": 116.330092},
            {"lat": 0.0, "lng": 0.0, "glat": 22.980760, "glng": 116.330015}]}]}}
    gf = geo_fences_from_policy(pd)
    p0 = gf[0]["points"][0] if gf else {}
    c_fence = (len(gf) == 1 and len(gf[0]["points"]) == 2
               and p0.get("glat") == 22.982321
               and p0.get("glng") == 116.330092
               and p0.get("lat") == 0.0 and p0.get("lng") == 0.0
               and "glon" not in p0 and gf[0]["id"] == "2351")
    print("  %s 围栏取自 runModePolicy.data.geoFence，键名 glat/glng、id 转字符串"
          % ("OK " if c_fence else "FAIL"))
    ok &= c_fence

    pd2 = {"geoFence": {"geoFences": [{"id": 1, "points": [
        {"lat": 0.0, "lon": 0.0, "glat": 22.1, "glon": 116.1}]}]}}
    q0 = geo_fences_from_policy(pd2)[0]["points"][0]
    c_fence2 = (q0["glat"] == 22.1 and q0["glng"] == 116.1
                and "glon" not in q0)
    print("  %s 变异自检：服务端拼写 glon 输入 → 输出必须是 glng"
          % ("OK " if c_fence2 else "FAIL"))
    ok &= c_fence2

    c_fence3 = all(geo_fences_from_policy(b) == [] for b in
                   [None, {}, {"geoFence": None},
                    {"geoFence": {"geoFences": []}}, {"geoFence": "x"}])
    print("  %s 围栏退化输入安全返回 []（不抛异常）"
          % ("OK " if c_fence3 else "FAIL"))
    ok &= c_fence3
    return ok


def main():
    ap = argparse.ArgumentParser(description="跑步记录提交")
    ap.add_argument("--track", help="轨迹 JSON（生成器输出）")
    ap.add_argument("--selftest", action="store_true", help="只跑签名自检")
    ap.add_argument("--dry-run", action="store_true", help="只构造不发送")
    args = ap.parse_args()

    if args.selftest:
        print("=" * 60)
        print("swsubmit 签名自检")
        print("=" * 60)
        ok = selftest()
        print("=" * 60)
        print("汇总: %s" % ("全部通过" if ok else "存在失败"))
        return 0 if ok else 1

    if not args.track:
        ap.print_help()
        return 1
    track = json.load(open(args.track, encoding="utf-8"))
    body, meta = build_record_body(track, uid=12345678, unid=3305, policy=1,
                                   policy_ts=1789535218913, min_distance=2000)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("[dry-run] body 长度 %d 字节" % len(json.dumps(body)))
        print(json.dumps({k: v for k, v in body.items()
                          if not isinstance(v, list)}, ensure_ascii=False, indent=2)[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())

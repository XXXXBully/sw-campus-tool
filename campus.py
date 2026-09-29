#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
campus.py — 按学生所属学校确定真实校区中心

原则（不再硬编码任何默认校区）：
  1) 未登录：不返回任何校区坐标（source="none"），由上层提示「登录后自动获取」。
     绝不 fallback 成某个固定校区。
  2) 登录后：以 /api/v70300/user/info 返回的 unid + campusName 作为学生所属学校。
     - 坐标优先本地覆盖配置 campus.json（用户可手动校准）；
     - 其次服务端围栏 getGeoFenceForRun（仅当围栏 unid 与当前学生 unid 一致才采用，
       防止接口在未指定学校时返回其它校区/北京点位造成「跑错学校」）；
     - 其次内置已知校区表 KNOWN_CAMPUS；
     - 都没有则返回 source="no-coord"（校名准确但无坐标），上层提示手动校准。
"""
from __future__ import annotations

import json
import os

# 内置已知校区表（unid -> {name, lat, lon}）。可继续扩充。
# 注意：这只是「已知坐标」，不是「无条件默认」；学生 school 由 user/info 动态给出。
# ★ 本表坐标必须是 **WGS-84**：它会被直接当成生成器的圆心/自由跑中心，
#   提交时再由 `swobs.conv_point` 做一次 WGS-84 → GCJ-02。
#   （别跟服务端打卡点搞混 —— 打卡点的 `lat/lon` 是 **BD-09**、`glat/glon` 才是
#     GCJ-02，见 `swmode.to_wgs_points` 与 `_gh_tools/test_crs_model.py`。）
KNOWN_CAMPUS = {
    # 广东工业大学 揭阳校区（memory：campusName=广东工业大学 揭阳校区, campusId=3125008344）
    # ★ 坐标已按 OSM 实测边界重标定（WGS-84）：
    #   OSM way 1034082727 边界 lat 22.9796788~22.9854105, lon 116.3158811~116.3267198
    #   中心 = (22.9825447, 116.3213004)。旧值 (22.981367, 116.332141) 经 wgs→gcj 换算后
    #   落在校区以东约 500 m（GCJ 116.3367 > 校区东界 116.3313），导致轨迹出校。
    #
    # ★ `city`：记录 `address` 字段的**权威来源**（见 swcli.py 提交处）。
    #   ⚠ 2026-09-28 定因：此前 `address` 取的是 `identity.city`（设备档案里的城市），
    #     而发布包为隐私脱敏把它统一写成「大连市」（_gh_tools/clean_release.py）⇒
    #     不管在哪个学校跑，记录都显示「大连市」。地址该由**校区**决定，不该由设备决定。
    #   真机 App 的 address 也是**城市级**（高德反地理编码），如「揭阳市」/「大连市」。
    3305: {"name": "广东工业大学 揭阳校区", "lat": 22.9825447, "lon": 116.3213004,
           "city": "揭阳市"},
}

# 兼容旧引用（run_all 等仍引用这些常量），但不再作为「无条件默认」。
DEFAULT_UNID = 3305
DEFAULT_CAMPUS = {
    "name": KNOWN_CAMPUS[3305]["name"],
    "lat": KNOWN_CAMPUS[3305]["lat"],
    "lon": KNOWN_CAMPUS[3305]["lon"],
    "city": KNOWN_CAMPUS[3305].get("city", ""),
    "unid": DEFAULT_UNID,
}

# 本地档案文件（与 swcli.py 同目录）
HERE = os.path.dirname(os.path.abspath(__file__))
CAMPUS_FILE = os.path.join(HERE, "campus.json")


def load_campus_overrides() -> dict:
    """读取本地校区覆盖配置（{unid: {lat, lon, name, city}}），无则空。

    `city` 可选项：记录 `address` 字段用的城市名（如「揭阳市」）。
    未填时回退到 `KNOWN_CAMPUS` 的同 unid 条目，再回退到设备档案的 city。
    """
    if os.path.exists(CAMPUS_FILE):
        try:
            return json.load(open(CAMPUS_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_campus_overrides(d: dict):
    json.dump(d, open(CAMPUS_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def pick_campus(c, unid: int | None = None, *, campus_name: str | None = None,
                verbose: bool = True) -> dict:
    """确定本次跑单的校区中心。

    返回 {"name","lat","lon","city","unid","source"}
      city: 记录 `address` 字段的来源（城市级，如「揭阳市」）。缺失时为 ""，
            由调用方决定回退策略（`swcli.py` 会退到 identity.city）。
      source: "none" | "override" | "server-fence" | "known" | "no-coord"
      - "none"     未登录，无任何校区（lat/lon 为 None）
      - "override" 用户在 campus.json 手动校准坐标
      - "server-fence" 服务端围栏中心（已校验与当前 unid 一致）
      - "known"    内置已知校区表（坐标可靠但不保证 100% 精确）
      - "no-coord" 有校名但无坐标（需上层提示手动校准）
    """
    unid = int(unid or 0)

    # 未登录：绝不返回默认校区
    logged = bool(c is not None and getattr(c, "uid", 0) and getattr(c, "token", ""))
    if not logged:
        return {"name": "未登录", "lat": None, "lon": None, "city": "",
                "unid": 0, "source": "none"}

    overrides = load_campus_overrides()

    # ① 本地覆盖配置（用户手动校正过的学校坐标最优先）
    if unid and str(unid) in overrides:
        o = overrides[str(unid)]
        return {"name": o.get("name") or campus_name or ("校区%d" % unid),
                "lat": float(o["lat"]), "lon": float(o["lon"]),
                "city": str(o.get("city") or ""),
                "unid": unid, "source": "override"}

    # ② 服务端围栏（权威）—— 仅当围栏 unid 与当前学生 unid 一致才采用，防拉错校区
    try:
        fence = fetch_fence_center(c, verbose=verbose)
        if fence and fence.get("lat") is not None and fence.get("lon") is not None:
            f_unid = int(fence.get("unid") or 0)
            if f_unid == 0 or f_unid == unid:
                fence["source"] = "server-fence"
                # 围栏接口不带城市 ⇒ 城市仍从内置表/覆盖配置补
                if not fence.get("city"):
                    fence["city"] = _known_city(unid, overrides)
                return fence
            if verbose:
                print("[campus] 服务端围栏 unid=%s 与当前学生 unid=%s 不符，忽略"
                      % (f_unid, unid))
    except Exception as e:
        if verbose:
            print("[campus] 服务端围栏拉取失败: %s" % (str(e)[:120]))

    # ③ 内置已知校区表
    if unid and unid in KNOWN_CAMPUS:
        k = dict(KNOWN_CAMPUS[unid])
        k["unid"] = unid
        k["source"] = "known"
        k.setdefault("city", "")
        if verbose:
            print("[campus] 内置已知校区: %s (%.6f, %.6f) unid=%s"
                  % (k["name"], k["lat"], k["lon"], unid))
        return k

    # ④ 有校名但无坐标（校名准确，坐标需手动校准）
    name = campus_name or ("校区%d" % unid if unid else "未知校区")
    return {"name": name, "lat": None, "lon": None, "city": _known_city(unid, overrides),
            "unid": unid, "source": "no-coord"}


def _known_city(unid: int, overrides: dict | None = None) -> str:
    """取某 unid 已知的**城市名**（记录 address 的来源）。取不到返回 ""。"""
    overrides = overrides if overrides is not None else load_campus_overrides()
    if unid and str(unid) in overrides:
        c = overrides[str(unid)].get("city")
        if c:
            return str(c)
    k = KNOWN_CAMPUS.get(int(unid or 0)) or {}
    return str(k.get("city") or "")


# ★★ 公开入口（2026-09-29 新增）。
#   起因：`swcli.py` 在「调用方显式传 --campus-lat/--campus-lon」的分支里把
#   `camp["city"]` 置空，于是 `record_address` 退回**设备档案 city**；而发布包
#   为隐私脱敏把它统一写成「大连市」⇒ 从 GUI / run_all 提交的记录全显示大连市。
#   GUI（`gui.py`）与批量（`run_all.py`）**正是**显式传坐标的那条路 ⇒ 修复形同虚设。
#   现在这条分支也用 unid 反查校区城市，保证「地址由校区决定」在**所有**入口成立。
def known_city(unid: int, overrides: dict | None = None) -> str:
    """公开别名：某 unid 的校区城市名（本地覆盖配置 > 内置已知校区），取不到 ""。"""
    return _known_city(unid, overrides)


# ══════════════════════════════════════════════════════════════════
# 记录 address 的**唯一裁决处**
# ══════════════════════════════════════════════════════════════════
def resolve_address(identity_city: str, unid: int, *,
                    explicit: str | None = None,
                    campus_city: str | None = None) -> tuple:
    """裁决记录 `address`。返回 `(address, source)`。

    优先级：`--address` 显式 > 校区城市 > 设备档案 city。

    ★★ 为什么要有这个函数（2026-09-29 定因）：
      地址本该由**校区**决定，但「取校区城市」原先只写在 `swcli.py` 的
      「swcli 自己解析校区」那条分支里；而 **GUI（`gui.py`）与批量（`run_all.py`）
      都是显式传 `--campus-lat/--campus-lon` 的** —— 走的是另一条分支，
      那条分支把 `camp["city"]` 置空 ⇒ 退回**设备档案 city**，
      而发布包为隐私脱敏把它统一写成「大连市」⇒ 从 GUI 提交的记录全显示大连市。
      ⇒ 凡是「同一语义散落在多个分支」的写法，迟早有一条被漏掉。
      现在收敛到这一个函数，`swcli.py` 只调用、不再自己判断。

    `campus_city`：调用方已经解析出的校区城市（可为空，函数内部会按 unid 反查）。
    """
    if explicit:
        return str(explicit), "命令行 --address"
    city = str(campus_city or "").strip() or known_city(unid)
    if city:
        return city, "校区"
    return str(identity_city or ""), "设备档案（该 unid 未收录城市，建议在 campus.json 补 city）"


def selftest() -> bool:
    """回归自检 —— 盯的就是「地址由校区决定」这条规则的所有入口。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print("  %s %s" % ("OK  " if cond else "FAIL", msg))
        ok = ok and bool(cond)

    chk(known_city(3305) == "揭阳市", "known_city(3305) == 揭阳市")

    # ★ 回归：显式传坐标那条路（GUI / run_all）**也必须**拿到校区城市。
    #   曾经的 bug：该分支把 city 置空 ⇒ 退回设备档案（发布包 = 大连市）。
    a, s = resolve_address("大连市", 3305)
    chk(a == "揭阳市" and s == "校区",
        "显式坐标路径 → 揭阳市（不是设备档案的 大连市）  实得 %r/%s" % (a, s))

    # 调用方已解析出校区城市时，直接用
    a, s = resolve_address("大连市", 3305, campus_city="汕头市")
    chk(a == "汕头市", "调用方给的 campus_city 优先于反查  实得 %r" % a)

    # --address 优先级最高
    a, s = resolve_address("大连市", 3305, explicit="北京市", campus_city="汕头市")
    chk(a == "北京市" and "address" in s, "--address 覆盖一切  实得 %r/%s" % (a, s))

    # 未知 unid 才允许退回设备档案（并如实标注来源）
    a, s = resolve_address("大连市", 0)
    chk(a == "大连市" and "设备档案" in s, "未知 unid → 退回设备档案  实得 %r/%s" % (a, s))

    # 全空 → 空串（不编造）
    a, s = resolve_address("", 0)
    chk(a == "", "无任何来源 → 空串  实得 %r" % a)

    print("  %s 汇总: %s" % ("OK  " if ok else "FAIL", "全部通过" if ok else "存在失败"))
    return ok


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(0 if selftest() else 1)



def fetch_fence_center(c, *, verbose: bool = True):
    """从 getGeoFenceForRun 拉取围栏中心坐标。

    接口为 POST /api/v1/getGeoFenceForRun，body "{}"，走信封加密。
    返回 {"name","lat","lon","unid"} 或 None。
    """
    try:
        status, biz, err, raw = c.call("POST", "/api/v1/getGeoFenceForRun",
                                       "{}", verbose=False)
    except Exception as e:
        if verbose:
            print("[campus] getGeoFenceForRun 异常: %s" % str(e)[:120])
        return None

    if biz is None:
        if verbose:
            print("[campus] getGeoFenceForRun 无业务体: %s" % str(err)[:120])
        return None

    # 尝试从 biz 提取围栏中心
    center = _extract_fence_center(biz)
    if center:
        if verbose:
            print("[campus] 服务端围栏中心: (%.6f, %.6f) unid=%s"
                  % (center["lat"], center["lon"], center.get("unid", "?")))
        return center

    if verbose:
        print("[campus] getGeoFenceForRun 返回结构未识别到中心，回退")
    return None


def _extract_fence_center(biz) -> dict | None:
    """从 getGeoFenceForRun 业务体里提取围栏中心（多边形顶点取均值）。"""
    try:
        data = biz.get("data") if isinstance(biz, dict) else None
    except Exception:
        data = None

    # 递归找含经纬度的多边形点，取均值作为中心
    lat_lons = _collect_latlons(data)
    if lat_lons:
        n = len(lat_lons)
        lat = sum(p[0] for p in lat_lons) / n
        lon = sum(p[1] for p in lat_lons) / n
        unid = _find_unid(biz)
        return {"name": _find_name(biz), "lat": lat, "lon": lon, "unid": unid}
    return None


def _collect_latlons(node, depth=0):
    """深度优先收集 (lat, lon) 对。兼容 dict/list 嵌套。"""
    out = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        lat = node.get("lat") or node.get("latitude")
        lon = node.get("lon") or node.get("lng") or node.get("longitude")
        if lat is not None and lon is not None:
            try:
                out.append((float(lat), float(lon)))
            except Exception:
                pass
        for v in node.values():
            out.extend(_collect_latlons(v, depth + 1))
    elif isinstance(node, list):
        for v in node:
            out.extend(_collect_latlons(v, depth + 1))
    return out


def _find_unid(biz) -> int:
    """在业务体里找一个 unid（多为所选校区 id）。"""
    def walk(n, d=0):
        if d > 6:
            return None
        if isinstance(n, dict):
            for k in ("unid", "selectUnid", "campusId", "schoolId", "id"):
                v = n.get(k)
                if v is not None and str(v).isdigit():
                    return int(v)
            for v in n.values():
                r = walk(v, d + 1)
                if r is not None:
                    return r
        elif isinstance(n, list):
            for v in n:
                r = walk(v, d + 1)
                if r is not None:
                    return r
        return None
    return walk(biz) or 0


def _find_name(biz) -> str:
    """在业务体里找个学校/校区名。"""
    def walk(n, d=0):
        if d > 6:
            return ""
        if isinstance(n, dict):
            for k in ("campusName", "schoolName", "name", "fenceName",
                      "geoFenceName"):
                v = n.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in n.values():
                r = walk(v, d + 1)
                if r:
                    return r
        elif isinstance(n, list):
            for v in n:
                r = walk(v, d + 1)
                if r:
                    return r
        return ""
    return walk(biz) or "校区"

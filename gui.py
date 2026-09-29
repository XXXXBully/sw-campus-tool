#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — 运动世界校园 · 本地可视化工具（零依赖，标准库 http.server）

布局（启动 http://127.0.0.1:8765）：
    ① 顶部简洁登录条：手机号 + 密码 + 登录 / 退出 + 状态
    ② 一键跑步表单：模式 / 距离 / 配速 / 平台(安卓·苹果) / 设备 / 开始时间(可随机) / 体重
    ③ 登录后：用户信息 / 跑步统计 / 按日对账 / 最近记录
    ④ 实时运行日志：后端 log_write 缓冲，前端轮询 /api/log 增量渲染

关键点：
  · 设备档案绑定稳定 device_id：选哪个设备就复用哪个（生成一次永久复用），
    平台 android/ios 决定请求头、UA、安装时间惯例。
  · 设备下拉只显示【别名 + 平台徽标】，不暴露真实机型。
  · 时间框「随机」按钮调 /api/random-time，按风控规则(06~22点、过去、当日上限、
    与已有记录间隔)挑一个合适起跑时间填入。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import swcli
import swclient as sw

PORT = 8765
HOST = "127.0.0.1"

PLATFORMS = {"android": "安卓", "ios": "苹果"}


# ══════════════════════════════════════════════════════════════════
# 实时日志缓冲（前端轮询 /api/log 增量拉取；线程安全）
# ══════════════════════════════════════════════════════════════════
_log_lines = []
_log_seq = 0
_log_lock = threading.Lock()

# 提交互斥锁：防止连点/双开导致同一时刻提交多条（配合前端按钮锁）
_RUN_LOCK = threading.Lock()


def log_write(msg: str, level: str = "info"):
    global _log_seq
    with _log_lock:
        line = (_log_seq, time.strftime("%H:%M:%S"), level, str(msg))
        _log_lines.append(line)
        _log_seq += 1
        if len(_log_lines) > 600:
            del _log_lines[:len(_log_lines) - 600]
        print("[gui:%s] %s" % (level, msg))
    return _log_seq


def log_snapshot(since: int):
    with _log_lock:
        out = [(seq, ts, level, msg) for seq, ts, level, msg in _log_lines if seq > since]
        return out, _log_seq


# ══════════════════════════════════════════════════════════════════
# 设备档案（稳定 device_id + 平台）
# ══════════════════════════════════════════════════════════════════
def _fresh_alias(platform: str) -> str:
    """生成一个未占用的设备别名：如 设备1-安卓 / 设备2-苹果"""
    devs = swcli.load_devices()
    tag = PLATFORMS.get(platform, platform)
    i = 1
    while True:
        name = "设备%d-%s" % (i, tag)
        if name not in devs:
            return name
        i += 1


def ensure_device(name: str, platform: str):
    """确保设备档案存在（不存在则生成稳定 device_id 并落盘），然后切为活跃。

    返回 (Identity, 别名)。device_id 一旦生成即持久化，下次选择直接复用。
    """
    devs = swcli.load_devices()
    if name in devs:
        entry = devs[name]
    else:
        ident = sw.Identity(platform=platform)
        entry = ident.to_dict()
        entry["_note"] = "GUI 新建"
        devs[name] = entry
        swcli.save_devices(devs)
        log_write("生成新设备档案 '%s'（%s）：device_id=%s"
                  % (name, PLATFORMS.get(platform, platform), entry["device_id"]))

    swcli.set_active_name(name)
    ident = sw.Identity.from_dict(devs[name])
    swcli.save_identity(ident)
    return ident, name


def do_switch_device(name: str, platform: str) -> dict:
    """切换设备（复用稳定 device_id），并记录日志。"""
    devs = swcli.load_devices()
    if name in devs:
        entry = devs[name]
        if platform and entry.get("platform") != platform:
            # 允许显式换平台（更新档案平台）
            entry["platform"] = platform
            swcli.save_devices(devs)
    ident = sw.Identity.from_dict(devs[name] if name in devs else
                                  {"platform": platform or "android"})
    if name not in devs:
        # 档案不存在，交给 ensure_device 生成
        return ensure_device(name, platform or "android"), True
    swcli.set_active_name(name)
    swcli.save_identity(ident)
    log_write("切换设备 '%s'（%s）：device_id=%s"
              % (name, PLATFORMS.get(ident.platform, ident.platform), ident.device_id))
    return {"ok": True, "msg": "已切换到 %s" % name}, False


def bound_device(username: str) -> dict:
    """某账号在 `device_bind.json` 里绑定的设备（未登录 / 未绑定 → `{}`）。

    ★ 2026-09-29：绑定 = 提交时 `load_identity()` **第 0 条**命中的那台，
      与「界面里选中的」无关 ⇒ 界面必须照它显示，否则就是在骗人。
    """
    alias = swcli.get_bind_alias(username or "")
    if not alias:
        return {}
    d = swcli.load_devices().get(alias)
    if not d:
        return {"alias": alias, "missing": True, "in_pool": False,
                "platform": "", "platform_label": "", "model": ""}
    plat = d.get("platform", "android")
    return {"alias": alias, "missing": False, "in_pool": True,
            "platform": plat, "platform_label": PLATFORMS.get(plat, "安卓"),
            "model": d.get("device_name", "")}


def effective_device(c) -> dict:
    """当前**实际生效**的设备 = 提交时 `load_identity()` 会返回的那台。

    ★★ 2026-09-29 修「界面显示的和实际提交的不是同一台」：
      旧实现 `snap["device"] = get_active_name()` 读的是 `active_device.txt`，
      而 `swcli.Client.__init__` 走的是 `load_identity()` —— 已登录时**第 0 条
      「账号绑定设备」压过 active** ⇒ 界面显示的别名与提交用的 device_id 可以
      完全不同（实测：界面显示安卓「默认」，提交用的是 `guise-iPhone 6S`）。
      这里统一用 `c.identity.device_id` **反查别名**，保证「界面 = 实际」。
    """
    devs = swcli.load_devices()
    did = getattr(c.identity, "device_id", "") or ""
    alias = ""
    for name, d in devs.items():
        if d.get("device_id") == did:
            alias = name
            break
    plat = getattr(c.identity, "platform", "android")
    return {"alias": alias, "device_id": did, "in_pool": bool(alias),
            "model": getattr(c.identity, "device_name", ""),
            "platform": plat, "platform_label": PLATFORMS.get(plat, "安卓")}


def device_list(c=None) -> list:
    """设备下拉的数据源：**只返回实际生效的那一台**（2026-09-29 改）。

    ★ 用户要求：「设备那里只显示出绑定的设备」。
      旧实现返回**全部 99 台**，用户能在界面里切来切去 —— 但切了**根本不影响提交**
      （提交走 `load_identity()` 第 0 条 = 账号绑定设备，见 `effective_device`），
      只制造「我换过设备了」的错觉。现在只暴露唯一一台，界面不再骗人。
      ★ 别名反查不到时返回 `[]`（前端显示「本地默认设备」并把 device 传空串）——
        传空串时 `_do_run_locked` 会跳过 `ensure_device()`，直接用 `load_identity()`
        的结果，**不会**凭空新建一台设备档案。
    """
    if c is None:
        c = swcli.Client()
    swcli.seed_devices_from_identity()
    eff = effective_device(c)
    if not eff["alias"]:
        return []
    uname = c.session.get("username", "")
    return [{"name": eff["alias"],
             "platform": eff["platform"],
             "platform_label": eff["platform_label"],
             "model": eff["model"],
             "active": True,
             "bound": bool(uname) and swcli.get_bind_alias(uname) == eff["alias"]}]


def device_info(name: str) -> dict:
    """查询单个设备档案详情（供选中后回显 device_id；不在下拉列表里暴露）。

    该接口按名字精确查，只返回选中设备的信息，前端仅用于「选中设备后展示设备号」。
    """
    swcli.seed_devices_from_identity()
    devs = swcli.load_devices()
    if name not in devs:
        return {"ok": False, "msg": "设备不存在"}
    d = devs[name]
    return {"ok": True, "name": name,
            "device_id": d.get("device_id", ""),
            "model": d.get("device_name", ""),
            "platform": d.get("platform", "android"),
            "platform_label": PLATFORMS.get(d.get("platform", "android"), "安卓")}


def random_run_time(dist: float, pace: str, prefer_hour: float = None) -> dict:
    """生成一个合适起跑时间：全天有效时段[06:00+, 22:00-跑量]均匀随机 + 过去 + 当日不超限 + 与已有记录间隔足够。

    注意：不再限定在「6~9点/17~21点」两个真人时段 —— 那会导致今天下午随机
    时因必须过去而只能落在早上。这里改为全天窗口均匀抽样，避免"随机全是早上"。
    可选 prefer_hour：偏好起跑时刻（如历史平均 18.5 点），会在其 ±1.5 小时窗口
    内优先抽样，窗口不可用（越界/过期）则回退全天均匀。
    未登录时不做服务端对账（纯本地规则）。
    """
    import run_all
    import random as _rnd
    try:
        dur = run_all._pace_to_sec(pace or "5:40", dist or 2.0)
    except Exception:
        dur = 780

    try:
        c0 = swcli.Client()
        if not (c0.uid and c0.token):
            existing = []          # 未登录：纯本地规则，不发服务端对账请求
        else:
            existing = run_all.fetch_existing_records(verbose=False)
    except Exception:
        existing = []
    byday = {}
    for r in existing:
        byday.setdefault(r["date"], []).append(r["start_min"])

    now = time.time()
    # 在最近 [今天, 昨天, 前天] 里挑一天，均匀随机一个符合规则的起跑时刻
    for off in range(0, 3):
        base = time.localtime(now - off * 86400)
        y, m, d = base.tm_year, base.tm_mon, base.tm_mday
        day_open = time.mktime((y, m, d, 6, 0, 0, 0, 0, -1))          # 06:00
        day_close = time.mktime((y, m, d, 22, 0, 0, 0, 0, -1)) - dur   # 22:00 - 跑量
        # 今天最晚起跑 = now - 60s；其它天 = 22:00 - 跑量
        latest = min(day_close, (now - 60) if off <= 0 else day_close)
        earliest = day_open
        if latest <= earliest:
            continue
        date0 = time.strftime("%Y-%m-%d", base)
        day_hits = byday.get(date0, [])
        if len(day_hits) >= run_all.MAX_PER_DAY:
            continue

        # 采样窗口：默认全天；有偏好时优先用偏好 ±1.5 小时
        lo, hi = earliest, latest
        if prefer_hour is not None:
            plo = time.mktime((y, m, d, int(prefer_hour) - 1, 30, 0, 0, 0, -1))
            phi = time.mktime((y, m, d, int(prefer_hour) + 1, 30, 0, 0, 0, -1))
            lo = max(earliest, plo)
            hi = min(latest, phi)
            if hi <= lo:
                lo, hi = earliest, latest

        # 窗口内均匀抽样若干次，选一个与已有记录间隔足够的时间
        chosen = None
        for _ in range(60):
            cand = _rnd.uniform(lo, hi)
            lt = time.localtime(cand)
            st_min = lt.tm_hour * 60 + lt.tm_min
            ok = True
            for m in day_hits:
                if abs(st_min - m) * 60 < dur + run_all.MIN_GAP_MIN * 60:
                    ok = False
                    break
            if ok:
                chosen = cand
                break
        if chosen is None:
            continue

        # ★ 秒位保留（原为 ":00" 硬写）：chosen 本来就是带随机秒的浮点时间戳，
        #   格式化成 ":00" 会把秒抹掉，于是起跑时间永远是 08:00:00 这种整分整秒。
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(chosen))
        # ★ 硬护栏：返回的起跑时间绝不允许在未来（哪怕边界算错/时钟漂移）
        if chosen > now - 30:
            chosen = now - 60
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(chosen))
        return {"ok": True, "time": ts_str,
                "note": "已避开当日 %d 条记录" % len(day_hits)}
    # 兜底：今天合理过去时间
    fallback = run_all._fmt_start(0, None, dur)
    # ★ 兜底值同样不许在未来
    try:
        fb_ts = time.mktime(time.strptime(fallback, "%Y-%m-%d %H:%M:%S"))
        if fb_ts > now - 30:
            fallback = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(now - 60))
    except Exception:
        fallback = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 60))
    return {"ok": True, "time": fallback, "note": "未对账（未登录或规则宽松）"}


def auto_config() -> dict:
    """一键设置：按历史跑步记录的平均值生成一套表单推荐值。

    依次计算：
      · dist_km  历史平均距离（km）
      · pace     历史平均配速（mm:ss）
      · start    按历史平均起跑时段±1.5h随机一个合规时间
      · platform 取当前活跃设备平台
      · note     基于几条记录
    未登录或无记录时返回 ok=False。
    """
    snap = get_snapshot()
    recs = snap.get("records") or []
    valid = []
    for r in recs:
        dd = float(r.get("dist") or 0)
        tt = float(r.get("time") or 0)
        if dd > 0 and tt > 0:
            valid.append({"dist": dd, "time": tt,
                          "start": r.get("start_ms") or 0})
    if not valid:
        return {"ok": False, "msg": "没有可参考的历史跑步记录（请先登录并跑过至少一次）"}

    n = len(valid)
    # 距离平均（米 → km，保留 2 位）
    avg_dist_m = sum(v["dist"] for v in valid) / n
    dist_km = round(avg_dist_m / 1000.0, 2)

    # 配速平均：秒/公里 = Σtime / Σdist * 1000
    sum_t = sum(v["time"] for v in valid)
    sum_d = sum(v["dist"] for v in valid)
    pace_sec = (sum_t / sum_d) * 1000.0 if sum_d else 340.0
    pace_sec = max(240, min(600, pace_sec))   # 夹在 4:00~10:00（与新下拉一致）
    mm = int(pace_sec // 60)
    ss = int(round(pace_sec % 60))
    if ss == 60:
        mm += 1
        ss = 0
    pace = "%d:%02d" % (mm, ss)

    # 起跑时段偏好：历史平均小时（如 18.4）
    hours = [(v["start"] / 1000.0) for v in valid if v["start"]]
    prefer = None
    if hours:
        import time as _t
        hh = sum(_t.localtime(h).tm_hour + _t.localtime(h).tm_min / 60.0
                 for h in hours) / len(hours)
        prefer = hh

    rt = random_run_time(dist_km, pace, prefer_hour=prefer)
    start = rt.get("time", "")

    # 推荐设备：当前账号绑定的设备优先（其平台即推荐平台），否则退回当前活跃设备
    username = snap.get("username", "")
    devs = swcli.load_devices()
    bind_alias = swcli.get_bind_alias(username) if username else ""
    device = None
    if bind_alias and bind_alias in devs:
        device = bind_alias
        platform = devs[bind_alias].get("platform", "android")
    else:
        # ★ 2026-09-29：回退也要用**实际生效**的设备，不能再用 active_device.txt
        #   （snap["device"] 已是 load_identity() 反查出来的别名）
        device = snap.get("device", "") or ""
        platform = snap.get("platform", "android")
    return {"ok": True, "dist_km": dist_km, "pace": pace, "start": start,
            "platform": platform, "device": device,
            "platform_label": PLATFORMS.get(platform, "安卓"),
            "prefer_hour": round(prefer, 1) if prefer is not None else None,
            "note": "基于 %d 条历史记录平均" % n}


# ══════════════════════════════════════════════════════════════════
# 后端逻辑（复用 swcli / auto_login / run_all）
# ══════════════════════════════════════════════════════════════════
def load_autologin():
    src = open(os.path.join(HERE, "auto_login.py"), encoding="utf-8").read()
    head = src.split("# ============================================================\n# MAIN")[0]
    ns = {"__name__": "autologin"}
    exec(compile(head, "auto_login", "exec"), ns)
    return ns


def do_login(username: str, password: str) -> dict:
    username = (username or "").strip()
    if not username or not password:
        log_write("登录：请先输入手机号和密码", "err")
        return {"ok": False, "msg": "请输入手机号和密码"}

    log_write("登录 %s …（加载加密链 + checkGeeUse）" % username)
    def _dep_msg(e):
        miss = getattr(e, "name", None) or str(e)
        return ("缺少登录依赖 %s —— 请在 Termux 里执行："
                "pkg install -y python-numpy python-pillow"
                "（或重新运行 bash install.sh）" % miss)

    buf = io.StringIO()
    try:
        # ★ load_autologin 必须在 try 内：它 exec 了 auto_login.py 的头部，
        #   而那里有 `from PIL import Image` / `import numpy`。缺依赖时若在
        #   try 外抛出，会冒泡成 HTTP 500，用户只看到"登录不上"却毫无线索。
        ns = load_autologin()
        # 复用/分配该账号绑定的设备（首登固定一台，登录/提交都用它）
        ident = swcli.ensure_account_device(username)
        with contextlib.redirect_stdout(buf):
            result = ns["login"](username, password, identity=ident)
        for line in buf.getvalue().splitlines():
            line = line.rstrip()
            if line:
                log_write(line)
    except ModuleNotFoundError as e:
        msg = _dep_msg(e)
        log_write(msg, "err")
        return {"ok": False, "msg": msg}
    except Exception as e:
        log_write("登录异常：%s" % str(e)[:160], "err")
        return {"ok": False, "msg": "登录异常：%s" % str(e)[:160]}

    if not result:
        log_write("登录失败（账号密码错误或滑块校验未过）", "err")
        return {"ok": False, "msg": "登录失败（账号密码错误或滑块校验未过）"}

    sess = swcli.load_session()
    sess.update({"uid": result["uid"], "token": result["token"],
                 "unid": result.get("unid", ""), "name": result.get("name", ""),
                 "username": username})
    swcli.save_session(sess)
    # 登录成功后把 uid 记进账号绑定记录（该账号后续提交仍用同一设备）
    try:
        binds = swcli.load_binds()
        if username in binds:
            binds[username]["uid"] = int(result.get("uid", 0) or 0)
            swcli.save_binds(binds)
    except Exception:
        pass
    log_write("登录成功：uid=%s name=%s 绑定设备=%s"
              % (result["uid"], result.get("name", ""),
                 bound_device(username).get("alias") or "本地默认设备"), "ok")
    return {"ok": True, "msg": "登录成功", "data": result}


def get_snapshot() -> dict:
    c = swcli.Client()
    uname = c.session.get("username", "")
    eff = effective_device(c)
    snap = {"logged": bool(c.uid and c.token), "uid": c.uid,
            "name": c.session.get("name", ""), "unid": c.session.get("unid", ""),
            "username": uname,
            # ★ 2026-09-29：device 由 `get_active_name()`（active_device.txt）改为
            #   **实际生效别名**。前者在已登录时会被 `load_identity()` 第 0 条
            #   （账号绑定设备）压过 ⇒ 界面显示的与提交用的可以不是同一台。
            "device": eff["alias"],
            "device_model": eff["model"],
            "bound": bound_device(uname),
            "platform": c.identity.platform,
            "platform_label": PLATFORMS.get(c.identity.platform, "安卓"),
            "devices": device_list(c),
            "stats": None, "records": []}

    if not snap["logged"]:
        return snap

    try:
        _, biz, _, _ = c.call("POST", "/api/v70300/user/info", "{}", verbose=False)
        if biz and isinstance(biz, dict):
            d = biz.get("data") or {}
            snap["name"] = d.get("name") or snap["name"]
            snap["campus"] = d.get("campusName", "")
    except Exception:
        pass

    # 校区中心（登录后按学生所属学校动态取，不再硬编码揭阳）——供前端展示与提交参考
    try:
        import campus
        cname = snap.get("campus", "") or ""
        camp = campus.pick_campus(c, int(snap["unid"] or 0),
                                  campus_name=cname, verbose=False)
        if camp.get("lat") is not None and camp.get("lon") is not None:
            snap["campus_lat"] = camp["lat"]
            snap["campus_lon"] = camp["lon"]
        snap["campus_src"] = camp.get("source", "?")
        if not snap.get("campus"):
            snap["campus"] = camp.get("name", snap.get("campus", ""))
    except Exception:
        pass

    try:
        _, biz, _, _ = c.call("GET", "/api/v70100/run/data/index?type=1", "",
                              verbose=False)
        if biz and isinstance(biz, dict):
            d = biz.get("data") or {}
            snap["stats"] = {
                "runCountLength": d.get("runCountLength"),
                "weekRunLength": d.get("weekRunLength"),
                "longestDistance": d.get("longestDistance"),
                "longestTime": d.get("longestTime"),
                "bestSpeed": d.get("bestSpeed"),
            }
    except Exception:
        pass

    try:
        unid = int(c.session.get("unid", 0) or 0)
        body = json.dumps({"pageNum": 1, "pageSize": 20,
                           "selectedUnid": unid, "uid": c.uid}, separators=(",", ":"))
        _, biz, _, _ = c.call("POST", "/api/v70230/runnings/records", body, verbose=False)
        d = biz.get("data")
        recs = d if isinstance(d, list) else ((d or {}).get("list") or [])
        byday = {}
        for r in recs:
            st = int(r.get("startTime") or 0)
            date0 = time.strftime("%Y-%m-%d", time.localtime(st / 1000.0)) if st else "?"
            byday[date0] = byday.get(date0, 0) + 1
            snap["records"].append({
                "rrid": r.get("rrid"),
                "uuid": r.get("uuid"),
                "date": date0,
                "start_ms": st,
                "stop_ms": r.get("stopTime"),
                "dist": r.get("totalDis"),
                "time": r.get("totalTime"),
                "start": time.strftime("%H:%M", time.localtime(st / 1000.0)) if st else "",
                "complete": bool(r.get("complete")),
                "validDis": r.get("validDis"),
                "validTime": r.get("validTime"),
                "avgStepFreq": r.get("avgStepFreq"),
                "sportType": r.get("sportType"),
            })
        snap["records_by_day"] = byday
    except Exception:
        pass
    return snap


def do_run(mode: str, dist: float, start: str, device: str,
           pace: str, weight: float, force: bool, platform: str,
           q_lat: float = None, q_lon: float = None,
           shape: str = "track") -> dict:
    """一键跑步：确认设备(复用稳定device_id) → 排期 → 对账 → 提交。"""
    out = []
    def log(s, lv="info"):
        out.append(s)
        log_write(s, lv)

    c0 = swcli.Client()
    if not (c0.uid and c0.token):
        log_write("提交被拒绝：未登录（请先登录）", "err")
        return {"ok": False, "log": out + ["未登录：请先登录再提交"]}

    # 提交互斥：同一时刻只允许一条提交在跑（防止连点/双开造成重复记录）
    if not _RUN_LOCK.acquire(blocking=False):
        log_write("提交被拒绝：已有提交在进行中，请稍候", "warn")
        return {"ok": False, "log": out + ["已有提交进行中，请等待当前完成再试"]}
    try:
        return _do_run_locked(mode, dist, start, device, pace, weight,
                              force, platform, q_lat, q_lon, out, shape)
    finally:
        _RUN_LOCK.release()


def _do_run_locked(mode, dist, start, device, pace, weight, force, platform,
                   q_lat, q_lon, out, shape="track"):
    """已持有 _RUN_LOCK 的提交体。"""
    c0 = swcli.Client()
    def log(s, lv="info"):
        out.append(s)
        log_write(s, lv)

    # 切设备（复用/生成稳定 device_id，并按需同步平台）
    if device:
        ident, _ = ensure_device(device, platform or "android")
        log("设备：%s（%s）device_id=%s"
            % (device, PLATFORMS.get(ident.platform, ident.platform), ident.device_id))

    import run_all
    import campus
    # 校区中心：优先命令行/档案/服务端围栏/内置库；未登录/无坐标不再回退默认揭阳
    if q_lat is not None and q_lon is not None:
        camp = {"name": "手动指定", "lat": float(q_lat), "lon": float(q_lon),
                "unid": int(c0.session.get("unid", 0) or 0)}
    else:
        camp = campus.pick_campus(c0, int(c0.session.get("unid", 0) or 0))
    if camp.get("lat") is None or camp.get("lon") is None:
        log("校区坐标未收录（%s）：请在 campus.json 手动校准该校区坐标"
            % camp.get("name", "?"), "err")
        return {"ok": False, "log": out + [
            "校区坐标未收录，无法提交。请在 campus.json 为 %s 配置坐标，或改用手动指定。" % camp.get("name", "?")]}
    clat, clon = camp["lat"], camp["lon"]
    log("校区：%s (%.6f, %.6f) 来源=%s"
        % (camp["name"], clat, clon, camp.get("source", "?")))
    cli = ["submit", "--mode", mode, "--dist", "%.2f" % dist,
           "--campus-lat", "%.6f" % clat,
           "--campus-lon", "%.6f" % clon,
           "--pace", pace, "--weight", "%.1f" % weight]
    # ★ 轨迹形状（2026-09-29）：默认 track（标准跑道）；显式传参，避免默认值散落多处
    cli += ["--shape", shape or "track"]
    log("轨迹形状：%s" % ("标准跑道（两个半圆 + 两条直道，按校区完整点池拟合）"
                      if (shape or "track") == "track" else "椭圆（圆润闭合环）"))
    if start:
        cli += ["--start", start]
    if force:
        cli += ["--force"]
    # ★★ 计分跑：每次提交都重新抽样打卡点组合（2026-09-29）
    #   服务端每次返回的 5 点是「6 选 5」随机剔 1 个（离请求坐标最近的必含），
    #   但 `swmode.load_cache` 的 TTL 是 30 分钟 —— 不强制重拉的话，短时间内连点
    #   几次「开始跑步」会**复用同一组 5 点**（用户要求「不用一直固定一个组合」）。
    #   ★ 限流时 swcli 会阻止提交并给出提示（服务端 5 分钟最多 3 次）。
    if mode == "score":
        cli += ["--force-points"]

    try:
        existing = run_all.fetch_existing_records(verbose=False)
        date0 = start[:10] if start else time.strftime("%Y-%m-%d")
        byday = {}
        for x in existing:
            byday[x["date"]] = byday.get(x["date"], 0) + 1
        if byday.get(date0, 0) >= 2 and not force:
            log_write("对账被阻止：%s 已有 %d 条（达上限）。改开始时间或勾强制。"
                      % (date0, byday[date0]), "warn")
            return {"ok": False, "log": out + [
                "对账：%s 已有 %d 条记录（达上限），已阻止。请改时间或勾选强制。"
                % (date0, byday[date0])]}
        log("对账通过：%s 已有 %d 条" % (date0, byday.get(date0, 0)))
    except Exception as e:
        log("对账警告：%s" % str(e)[:120], "warn")

    r = subprocess.run([sys.executable, os.path.join(HERE, "swcli.py")] + cli,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    log("退出码 %d" % r.returncode)
    if r.returncode == 0:
        log(r.stdout[-2000:], "ok")
    else:
        log(r.stdout[-2000:], "err")
    if r.stderr:
        log("stderr: " + r.stderr[-800:], "err")
    log("提交流程结束，结果=%s" % ("成功" if r.returncode == 0 else "失败"),
        "ok" if r.returncode == 0 else "err")
    return {"ok": r.returncode == 0, "log": out}


# ══════════════════════════════════════════════════════════════════
# HTTP 处理
# ══════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _html(self, html: str, code=200):
        self._send(code, html.encode("utf-8"), "text/html")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._html(PAGE)
        elif u.path == "/api/state":
            self._json(get_snapshot())
        elif u.path == "/api/device-info":
            q = parse_qs(u.query)
            self._json(device_info((q.get("name") or [""])[0]))
        elif u.path == "/api/auto-config":
            self._json(auto_config())
        elif u.path == "/api/log":
            q = parse_qs(u.query)
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            lines, nxt = log_snapshot(since)
            self._json({"lines": lines, "next": nxt})
        elif u.path == "/favicon.ico":
            self._send(204, b"")
        else:
            self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(ln) if ln else b""
            q = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            q = {}

        if u.path == "/api/login":
            self._json(do_login(q.get("username", ""), q.get("password", "")))
        elif u.path == "/api/logout":
            # 真·登出：先调服务端注销接口（作废 token），再清空本地会话。
            # 这样服务端不再认为该账号在线，手机等其它端登录不会再报"在别处登录"。
            res = swcli.server_logout(verbose=False)
            log_write("退出登录：%s" % res["msg"], "warn")
            self._json({"ok": res["ok"], "logged": False,
                        "server": res.get("server"), "msg": res["msg"]})
        elif u.path == "/api/device":
            name = q.get("name", "")
            platform = q.get("platform", "android")
            if not name:
                self._json({"ok": False, "msg": "缺少设备名"}, 400)
            else:
                res, _ = do_switch_device(name, platform)
                self._json(res)
        elif u.path == "/api/new-device":
            platform = q.get("platform", "android")
            if platform not in PLATFORMS:
                platform = "android"
            name = _fresh_alias(platform)
            ident, n2 = ensure_device(name, platform)
            self._json({"ok": True, "name": name,
                        "platform": ident.platform,
                        "platform_label": PLATFORMS.get(ident.platform, "安卓"),
                        "device_id": ident.device_id})
        elif u.path == "/api/random-time":
            self._json(random_run_time(float(q.get("dist", 2.0)), q.get("pace", "5:40")))
        elif u.path == "/api/run":
            def _f(v, d):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return d
            self._json(do_run(q.get("mode", "free"), float(q.get("dist", 2.0)),
                              q.get("start", ""), q.get("device", ""),
                              q.get("pace", "5:40"), float(q.get("weight", 65.0)),
                              bool(q.get("force")), q.get("platform", "android"),
                              _f(q.get("lat"), None), _f(q.get("lon"), None),
                              q.get("shape", "track")))
        else:
            self._json({"ok": False, "msg": "not found"}, 404)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>运动世界校园 · 跑步工具</title>
<style>
  /* ── 「跑道」视觉语言 ─────────────────────────────
     签名色：塑胶跑道砖红 · 墨绿草坪 · 暖白纸
     造型：直角细线（非圆角卡片）· 衬线数字（田径计时感）*/
  :root{--bg:#FAF8F4;--card:#FFFDF9;--line:#E4DECF;--txt:#26241F;
        --sub:#8B8376;--ac:#C84B31;--ok:#2F5D50;--warn:#B07D18;--err:#B3392E}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);
       font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       font-size:14px;line-height:1.55;padding:26px 20px 40px}
  .wrap{max-width:920px;margin:0 auto}
  /* 页头签名：砖红「跑道」徽章（三条白线）+ 衬线标题 */
  .mast{display:flex;align-items:center;gap:14px;margin-bottom:16px}
  .mast>div:not(.mark){flex:1;min-width:0}
  .mast .help-btn{margin:0 0 0 auto;width:auto;flex:none;font-size:12px;
       padding:5px 14px;background:transparent;border:1px solid var(--line);
       color:var(--sub);transition:color .15s,border-color .15s}
  .mast .help-btn:hover{color:var(--ac);border-color:var(--ac)}
  .mark{width:42px;height:42px;flex:none;background:var(--ac);
        position:relative;box-shadow:0 2px 0 rgba(0,0,0,.14)}
  .mark i{position:absolute;left:9px;right:9px;height:2px;background:var(--card)}
  .mark i:first-child{top:12px}
  .mark i:nth-child(2){top:20px;background:rgba(255,255,255,.55)}
  .mark i:last-child{top:28px}
  h1{font-family:Georgia,"Songti SC","SimSun",serif;font-size:23px;
     font-weight:700;letter-spacing:.5px;margin-bottom:2px}
  .sub{color:var(--sub);font-size:12px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:2px;
        padding:16px 18px;margin-bottom:14px}
  .card h2{font-size:13px;font-weight:700;letter-spacing:1.2px;margin-bottom:12px;
        color:var(--sub);display:flex;align-items:center}
  .card h2:before{content:"";width:9px;height:9px;background:var(--ac);
        display:inline-block;margin-right:8px;flex:none}
  label{display:block;font-size:11px;color:var(--sub);margin:10px 0 4px;
        letter-spacing:.4px}
  input,select,button{width:100%;padding:9px 11px;border-radius:2px;
        border:1px solid var(--line);background:#FCFAF4;color:var(--txt);
        font-size:14px;outline:none;height:40px}
  select{padding:8px 10px;background-image:none}
  input[type="checkbox"],input[type="radio"]{width:auto;height:auto;padding:0}
  input[type="datetime-local"]{-webkit-appearance:none;appearance:none}
  input:focus,select:focus{border-color:var(--ac);background:#fff}
  button{cursor:pointer;background:var(--ac);border:1px solid var(--ac);color:#fff;
        font-weight:600;margin-top:14px;transition:background .15s;letter-spacing:.5px}
  button:hover{background:#A93D27}
  button.gray{background:#fff;border-color:var(--line);color:var(--txt)}
  button.gray:hover{background:#F1ECE1}
  button.small{width:auto;padding:0 13px;margin:0;height:40px;display:inline-flex;
        align-items:center;justify-content:center;flex:none}
  button:disabled{opacity:.5;cursor:not-allowed}
  .row{display:flex;gap:10px;align-items:flex-end}
  .row>div{flex:1}
  .btn-row{display:flex;gap:10px}
  .btn-row button{flex:1}
  /* 设备自定义下拉：限高滚动 + 搜索，避免一拉到底 */
  .devdd-head{display:flex;align-items:center;justify-content:space-between;
       height:40px;padding:0 11px;border:1px solid var(--line);background:#FCFAF4;
       border-radius:2px;cursor:pointer;font-size:14px;color:var(--txt);
       user-select:none}
  .devdd-list{position:absolute;top:calc(100% + 2px);left:0;right:0;z-index:50;
       display:none;background:#fff;border:1px solid var(--line);border-radius:2px;
       box-shadow:0 8px 24px rgba(0,0,0,.14);overflow:hidden}
  .devdd.open .devdd-list{display:block}
  .devdd-list .devdd-search input{margin:0;border:none;border-bottom:1px solid var(--line);
       height:38px;padding:0 11px;background:transparent;font-size:13px}
  .devdd-list .devdd-search input:focus{border-color:var(--line)}
  /* 自定义配速输入行（固定在列表顶部，不随搜索重建） */
  .devdd-list .devdd-custom{display:flex;gap:6px;padding:8px 9px;
       border-bottom:1px solid var(--line);background:#FCFAF4}
  .devdd-list .devdd-custom input{flex:1;min-width:0;margin:0;height:34px;
       font-size:13px;padding:0 10px}
  .devdd-list .devdd-custom button{flex:none;width:auto;margin:0;height:34px;
       padding:0 12px;font-size:13px;letter-spacing:0}
  .devdd-opts{max-height:210px;overflow-y:auto}
  .devdd-opts .dd-opt{padding:9px 11px;font-size:13px;cursor:pointer;
       border-bottom:1px solid #F0EAE0;white-space:nowrap;overflow:hidden;
       text-overflow:ellipsis;transition:background .12s}
  .devdd-opts .dd-opt:last-child{border-bottom:none}
  .devdd-opts .dd-opt:hover{background:#F3EEE4}
  .devdd-opts .dd-opt.sel{background:var(--ac);color:#fff}
  .devdd-opts .dd-none{padding:12px 11px;font-size:12px;color:var(--sub)}
  .devdd .caret{color:var(--sub);font-size:12px;transition:transform .15s}
  .devdd.open .caret{transform:rotate(180deg)}
  .stat{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
  .stat .s{background:#F6F3EA;border:1px solid var(--line);border-radius:2px;
        padding:10px;text-align:center;position:relative}
  .stat .s b{font-size:19px;font-family:Georgia,"Times New Roman",serif;
        color:var(--txt);display:block;font-weight:700}
  .stat .s span{font-size:11px;color:var(--sub)}
  .stat .s.clickable{cursor:pointer;transition:border-color .15s}
  .stat .s.clickable:hover{border-color:var(--ac);background:#FBEFE9}
  .stat .s.clickable.sel{border-color:var(--ac);background:#FBECE5;
        box-shadow:inset 0 -3px 0 var(--ac)}
  .pill{display:inline-block;padding:2px 9px;border-radius:2px;font-size:11px;
        background:#F4F0E6;color:var(--sub);margin:2px 4px 2px 0;border:1px solid var(--line)}
  .pill.act{background:#FBECE5;color:var(--ac);border-color:#E7BFAF}
  .pill.red{background:#FAE6E2;color:var(--err);border-color:#EDC8C2}
  .pill.ok{background:#E7EFEA;color:var(--ok);border-color:#C9DCD4}
  .log{background:#F7F4EC;border:1px solid var(--line);border-radius:2px;
       padding:10px;font-family:Consolas,Menlo,monospace;font-size:12px;
       height:280px;overflow:auto;white-space:pre-wrap;color:var(--txt)}
  .log .lt{color:#B1A995;margin-right:8px}
  .log .lm{word-break:break-all}
  /* ── 主提交按钮（独立、有仪式感） ── */
  .submit-hero{margin-top:18px;padding:16px 0 2px;border-top:1px dashed #EDE7D8;
       text-align:center}
  .submit-hero .sh-label{font-size:11px;color:var(--sub);letter-spacing:2px;
       margin-bottom:12px;text-transform:uppercase}
  .submit-hero button{width:100%;height:60px;border:none;border-radius:3px;
       margin:0;font-size:18px;font-weight:800;letter-spacing:3px;color:#fff;
       background:linear-gradient(135deg,#C8562E 0%,#A93D27 60%,#8F2F1C 100%);
       box-shadow:0 6px 18px rgba(168,61,39,.28);position:relative;overflow:hidden;
       display:flex;align-items:center;justify-content:center;gap:12px;
       transition:transform .12s,box-shadow .15s}
  .submit-hero button:hover{transform:translateY(-2px);
       box-shadow:0 10px 24px rgba(168,61,39,.34);background:linear-gradient(135deg,#D0623A 0%,#B4432C 60%,#9A3420 100%)}
  .submit-hero button:active{transform:translateY(0);
       box-shadow:0 4px 12px rgba(168,61,39,.28)}
  .submit-hero button:disabled{opacity:.75;cursor:not-allowed}
  .submit-hero .sh-run{width:30px;height:22px;position:relative;flex:none}
  .submit-hero .sh-run i{position:absolute;width:3px;height:14px;border-radius:2px;
       background:#fff;transform:rotate(24deg)}
  .submit-hero .sh-run i:nth-child(1){left:7px;top:2px}
  .submit-hero .sh-run i:nth-child(2){left:13px;top:5px;transform:rotate(-16deg)}
  .submit-hero .sh-run i:nth-child(3){left:19px;top:1px;transform:rotate(30deg)}
  /* 提交中：流光扫描 */
  .submit-hero button.running::after{content:"";position:absolute;top:0;bottom:0;
       left:-40%;width:40%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);
       animation:submitsweep 1.1s linear infinite}
  @keyframes submitsweep{100%{left:110%}}
  .submit-hero button.running .sh-txt::after{content:"…";animation:dots 1.2s steps(4) infinite}
  @keyframes dots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
  .submit-hero .sh-sub{font-size:12px;color:#B1A995;margin-top:10px;
       letter-spacing:.5px}
  .toast{position:fixed;top:16px;right:16px;background:var(--txt);color:#FDFBF5;
       padding:10px 16px;border-radius:2px;font-size:13px;display:none;z-index:99;
       border-left:4px solid var(--ac)}
  /* 记录详情弹窗 */
  .modal-mask{position:fixed;inset:0;background:rgba(38,36,31,.5);z-index:100;
       display:flex;align-items:center;justify-content:center;padding:20px}
  .modal{background:var(--card);border:1px solid var(--line);border-radius:2px;
       max-width:560px;width:100%;max-height:84vh;overflow:auto;box-shadow:0 10px 34px rgba(38,36,31,.3)}
  .modal-head{display:flex;align-items:center;justify-content:space-between;
       padding:14px 20px;border-bottom:1px solid var(--line);font-weight:700;letter-spacing:.5px;
       position:sticky;top:0;background:var(--card);z-index:2}
  .modal-body{padding:16px 18px}
  /* 详情弹窗内容容器：统一水平留白，避免文字贴边 */
  #mBody{padding:18px 20px 20px}
  .modal .kv-row{display:flex;justify-content:space-between;padding:6px 0;
       border-bottom:1px dashed #EDE7D8;font-size:13px}
  .modal .kv-row:last-child{border-bottom:none}
  .modal .kv-row .k{color:var(--sub)}
  .modal .kv-row .v{font-family:Georgia,Consolas,monospace;color:var(--txt);word-break:break-all}
  .modal .ok-big{font-size:22px;font-weight:700;display:block;margin-bottom:10px;
       font-family:Georgia,serif}
  /* ── 详情弹窗 v2：状态条 + 指标卡 + 明细网格 ── */
  .m-hero{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
       padding-bottom:14px;margin-bottom:16px;border-bottom:1px solid var(--line)}
  .m-badge{font-size:13px;font-weight:700;padding:5px 12px;border-radius:2px;letter-spacing:.5px}
  .m-badge.ok{background:#E7EFEA;color:var(--ok);border:1px solid #C9DCD4}
  .m-badge.no{background:#FAE6E2;color:var(--err);border:1px solid #EDC8C2}
  .m-type{font-size:11px;padding:3px 9px;border-radius:2px;border:1px solid var(--line);
       background:#F4F0E6;color:var(--sub);font-weight:600}
  .m-type.score{background:#E7EFEA;color:var(--ok);border-color:#C9DCD4}
  .m-type.free{background:#F4F0E6;color:var(--sub)}
  .m-id{font-size:11px;color:#B1A995;margin-left:auto}
  .m-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
  .mc{background:#F6F3EA;border:1px solid var(--line);border-radius:2px;
       padding:12px 6px;text-align:center}
  .mc b{font-size:16px;font-family:Georgia,"Times New Roman",serif;color:var(--txt);
       display:block;font-weight:700;white-space:nowrap;margin-bottom:3px}
  .mc span{font-size:10px;color:var(--sub)}
  .m-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 22px;margin-bottom:16px}
  .m-grid .mg{padding:8px 0}
  .mg{display:flex;justify-content:space-between;padding:8px 0;
       border-bottom:1px dashed #EDE7D8;font-size:13px;gap:12px}
  .mg .mg-k{color:var(--sub);flex:none}
  .mg .mg-v{font-family:Georgia,Consolas,monospace;color:var(--txt);word-break:break-all;
       text-align:right}
  .m-sec-title{font-size:12px;font-weight:700;color:var(--sub);letter-spacing:1px;
       margin:8px 0 4px;display:flex;align-items:center}
  .m-sec-title:before{content:"";width:7px;height:7px;background:var(--ac);
       display:inline-block;margin-right:7px;flex:none}
  .badge{font-size:11px;padding:2px 8px;border-radius:2px}
  .hint{font-size:12px;color:var(--sub);margin-top:8px}
  .code{color:var(--ac);word-break:break-all;font-family:Consolas,monospace}
  /* 顶部登录条 */
  .loginbar{padding:12px 18px;border-left:4px solid var(--ac)}
  .lb-row{display:flex;gap:10px;align-items:center}
  .lb-row input{flex:3;margin:0}
  .lb-row input:nth-child(2){flex:3}
  .lb-row button{margin:0;flex:1.2}
  .lb-state{flex:1.6;font-size:12px;color:var(--sub);text-align:right;white-space:nowrap}
  .lb-state b{font-size:12px;padding:3px 9px;border-radius:2px;font-weight:600}
  .lb-state b.ok{background:#E7EFEA;color:var(--ok)}
  .lb-state b.no{background:#F4F0E6;color:var(--sub)}
  .lb-remember{flex:1.4;display:flex;align-items:center;gap:5px;font-size:13px;
       color:var(--txt);white-space:nowrap;cursor:pointer;min-width:0}
  .lb-remember input{flex:none!important;width:14px;height:14px;margin:0!important;
       accent-color:var(--ac);cursor:pointer}
  /* 历史账号下拉：只占内容宽度，不抢手机号输入框的空间 */
  .lb-acct{flex:0 0 auto!important;width:auto!important;min-width:132px;max-width:200px;
       margin:0;font-size:13px;cursor:pointer}
  @media(max-width:720px){.row{flex-wrap:wrap}.lb-row{flex-wrap:wrap}.lb-state{flex:100%;text-align:left}}
  /* ── 手机窄屏适配（Termux 手机浏览器访问 127.0.0.1:8765）──────────────── */
  @media(max-width:560px){
    body{padding:16px 12px 36px;font-size:13px}
    .mast{gap:10px}
    .mark{width:34px;height:34px}
    .mark i{left:7px;right:7px}
    .mark i:first-child{top:9px}.mark i:nth-child(2){top:15px;background:rgba(255,255,255,.55)}.mark i:last-child{top:21px}
    h1{font-size:19px}
    .sub{font-size:11px;margin-bottom:10px}
    .card{padding:12px 12px;margin-bottom:10px}
    .stat{grid-template-columns:repeat(2,1fr);gap:8px}
    .m-cards{grid-template-columns:repeat(2,1fr);gap:8px}
    .m-grid{grid-template-columns:1fr}
    /* 登录区在手机端改为纵向堆叠：手机号一行、密码一行、登录按钮一行 */
    .lb-row{flex-direction:column;align-items:stretch;gap:8px}
    .lb-row>input{flex:none;width:100%;margin:0!important}   /* 只作用于账号/密码，避免误伤 checkbox */
    .lb-acct{width:100%!important;max-width:none}
    .lb-row>button{flex:none;width:100%;margin:0}
    .lb-remember{flex:none;width:100%;justify-content:flex-start}
    .lb-remember input{flex:none!important;width:14px!important;height:14px!important;margin:0!important}
    .lb-state{flex:none;width:100%;text-align:left}
    /* ★ 关键：.row>div 默认 flex:1（basis 0）会让 flex-wrap 永不换行，
       导致「模式/距离/配速」「平台/设备/体重」三列被挤扁、文字折行。
       窄屏下让每个字段各占一行。 */
    .row>div{flex:1 1 100%!important}
    .modal-mask{padding:10px;align-items:flex-end}
    .modal{max-height:88vh;border-radius:8px 8px 0 0}
    .log{height:200px;font-size:11px}
    .submit-hero button{height:54px;font-size:16px}
    button.small{height:38px;padding:0 10px}
    input,select{padding:9px 8px;font-size:14px}
    .devdd-opts{max-height:180px}
    .toast{left:12px;right:12px;top:12px;text-align:center}
    .mast .help-btn{font-size:11px;padding:4px 10px}
  }
  @media(prefers-reduced-motion:reduce){*,*:before,*:after{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <!-- 页头签名：跑道徽章 + 衬线标题 -->
  <div class="mast">
    <div class="mark" aria-hidden="true"><i></i><i></i><i></i></div>
    <div>
      <h1>运动世界校园 · 跑步工具</h1>
      <div class="sub" style="margin-bottom:0">保守使用 · 每日最多 2 条 · 起跑 06:00~22:00</div>
    </div>
    <button class="help-btn" onclick="openHelp()">使用说明</button>
  </div>

  <!-- ① 登录条 -->
  <div class="card loginbar">
    <div class="lb-row">
      <select id="acctSel" class="lb-acct" onchange="pickAccount(this.value)"
              style="display:none" title="选择之前登录过的账号（姓名 · 手机号）"></select>
      <input id="username" placeholder="手机号">
      <input id="password" type="password" placeholder="密码">
      <label class="lb-remember" for="remember"><input type="checkbox" id="remember" checked> 记住密码</label>
      <button id="loginBtn" onclick="doLogin()">登 录</button>
      <button id="logoutBtn" class="gray" onclick="doLogout()" style="display:none">退出</button>
      <span class="lb-state" id="loginState"><b class="no">未登录</b></span>
    </div>
  </div>

  <!-- ② 一键跑步（常显） -->
  <div class="card">
    <h2>一键跑步</h2>
    <div class="row">
      <div><label>模式</label>
        <div class="devdd modedd" id="modeDDBox" style="position:relative">
          <div class="devdd-head" id="modeDDHead" role="button" tabindex="0" onclick="toggleModeDD(event)">
            <span id="modeDDTxt">自由跑（校园范围）</span><span class="caret" aria-hidden="true">▾</span>
          </div>
          <div class="devdd-list" id="modeDDList">
            <div class="devdd-opts" id="modeOpts"></div>
          </div>
          <input type="hidden" id="rMode" value="free">
        </div></div>
      <div><label>距离 (km)</label><input id="rDist" type="number" value="2.15" step="0.05" min="0.5"></div>
      <div><label>轨迹形状</label>
        <select id="rShape" title="标准跑道 = 两个半圆 + 两条直道（默认，按校区完整点池拟合）；椭圆 = 更圆润的闭合环">
          <option value="track">标准跑道（默认）</option>
          <option value="ellipse">椭圆</option>
        </select></div>
      <div><label>配速</label>
        <div class="devdd pacedd" id="paceDDBox" style="position:relative">
          <div class="devdd-head" id="paceDDHead" role="button" tabindex="0" onclick="togglePaceDD(event)">
            <span id="paceDDTxt">5:40（常用）</span><span class="caret" aria-hidden="true">▾</span>
          </div>
          <div class="devdd-list" id="paceDDList">
            <div class="devdd-search">
              <input id="paceSearch" placeholder="搜索配速…" oninput="renderPaceList()">
            </div>
            <!-- ★ 自定义配速：自己填 分:秒（或秒数），回车 / 点「添加」即可 -->
            <div class="devdd-custom">
              <input id="paceCustomIn" placeholder="自定义配速，如 5:37" inputmode="decimal"
                     onkeydown="if(event.key==='Enter'){event.preventDefault();applyCustomPace();}">
              <button type="button" onclick="applyCustomPace()">添加</button>
            </div>
            <div class="devdd-opts" id="paceOpts"></div>
          </div>
          <input type="hidden" id="rPace" value="5:40">
        </div></div>
    </div>
    <div class="hint" id="paceTotal" style="margin-top:8px">总用时 ≈ —</div>
    <div class="row">
      <div style="flex:1"><label>校区</label>
        <input id="rCampus" readonly style="background:var(--bg);color:var(--sub)">
      </div>
    </div>
    <div class="row">
      <div><label>平台 <span id="platLbl" style="float:right;font-weight:400;color:var(--sub);font-size:11px">账号绑定</span></label>
        <select id="rPlatform" disabled tabindex="-1"
                style="background:var(--bg);color:var(--sub);cursor:not-allowed">
          <option value="android">安卓</option>
          <option value="ios">苹果</option>
        </select></div>
      <div><label>设备 <span id="devIdLbl" style="float:right;font-weight:400;color:var(--sub);font-size:11px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span></label>
        <input id="rDeviceView" readonly tabindex="-1"
               style="background:var(--bg);color:var(--sub);cursor:default"
               title="由账号绑定决定：首登随机固定一台，之后登录/提交都用它">
        <input type="hidden" id="rDevice">
      </div>
      <div><label>体重 kg</label><input id="rWeight" type="number" value="65" step="1"></div>
    </div>
    <div class="row">
      <div><label>开始时间</label>
        <div class="row" style="gap:6px">
          <input id="rStart" type="datetime-local" step="1" style="flex:1">
          <button class="gray small" style="margin:0" onclick="randomTime()">随机</button>
        </div>
        <div class="hint" id="rStartHint" style="margin-top:4px;color:var(--sub)"></div></div>
      <div style="flex:0.6"><label>&nbsp;</label>
        <div style="display:flex;align-items:center;gap:8px;height:37px">
          <label style="margin:0;font-size:13px;color:var(--txt)"><input type="checkbox" id="rForce" style="width:auto;margin:0"> 强制</label>
        </div></div>
    </div>
    <div class="btn-row">
      <button onclick="autoConfig()" id="autoCfgBtn" class="gray">一键设置（按历史平均）</button>
      <button class="gray" onclick="refresh()">刷新</button>
    </div>
    <div class="submit-hero">
      <div class="sh-label">— 生成并提交 —</div>
      <button onclick="doRun()" id="runBtn">
        <span class="sh-run" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="sh-txt">提交跑步记录</span>
      </button>
      <div class="sh-sub">自动：生成轨迹 → 时间合规 → 对账 → 服务端提交 → OBS 上传</div>
    </div>
    <div class="hint" id="autoCfgHint"></div>
    <div class="hint" id="reconcileHint">当日已满 2 条会自动阻止；随机时间会避开已有记录。</div>
  </div>

  <!-- ③ 登录后用户卡片 -->
  <div id="userCard" style="display:none">
    <div class="card">
      <h2>用户信息</h2>
      <div class="stat" style="grid-template-columns:repeat(3,1fr)">
        <div class="s"><b id="uName">-</b><span>姓名</span></div>
        <div class="s"><b id="uUid">-</b><span>UID</span></div>
        <div class="s"><b id="uUnid">-</b><span>校区</span></div>
      </div>
      <div class="hint">设备: <span class="code" id="uDevice">-</span></div>
    </div>
    <div class="card">
      <h2>跑步统计 <span class="hint" style="font-weight:400">（点击数字查看对应记录）</span></h2>
      <div class="stat">
        <div class="s clickable" data-stat="total"><b id="sTotal">-</b><span>已跑(m)</span></div>
        <div class="s clickable" data-stat="week"><b id="sWeek">-</b><span>本周(m)</span></div>
        <div class="s clickable" data-stat="long"><b id="sLong">-</b><span>最长(km)</span></div>
        <div class="s clickable" data-stat="best"><b id="sBest">-</b><span>配速</span></div>
      </div>
      <div class="hint" id="statDetail" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h2>最近记录（按日对账）</h2>
      <div id="recList"></div>
    </div>
  </div>

  <!-- ④ 日志面板 -->
  <div class="card">
    <h2>运行日志
      <button class="gray small" style="margin-left:auto" onclick="clearLogView()">清空</button>
    </h2>
    <div class="log" id="logPanel"><div class="hint">等待日志…</div></div>
  </div>
</div>

<!-- 使用说明弹窗 -->
<div class="modal-mask" id="helpMask" style="display:none" onclick="if(event.target===this)closeHelp()">
  <div class="modal">
    <div class="modal-head">
      <span>使用说明</span>
      <button class="gray small" style="margin:0;width:auto" onclick="closeHelp()">关闭</button>
    </div>
    <div class="modal-body">
      <div class="m-sec-title">快速上手</div>
      <div class="mg"><span class="mg-k">① 登录</span><span class="mg-v">输入手机号+密码，点「登录」。每个账号首次登录会自动分配一台专属设备（长期固定，避免同设备多账号被风控）。</span></div>
      <div class="mg"><span class="mg-k">② 生成</span><span class="mg-v">选模式（自由跑校园范围 / 计分跑需过打卡点）、距离、配速，点「一键跑步」。</span></div>
      <div class="mg"><span class="mg-k">③ 提交</span><span class="mg-v">核对预览后点「提交」，成功后右侧列表会出现该条记录。</span></div>

      <div class="m-sec-title" style="margin-top:14px">风控红线（务必遵守）</div>
      <div class="mg"><span class="mg-k">每日条数</span><span class="mg-v">最多 2 条（超出会被服务端查重拦截）</span></div>
      <div class="mg"><span class="mg-k">有效时段</span><span class="mg-v">起跑+结束都须在 06:00 ~ 22:00:59</span></div>
      <div class="mg"><span class="mg-k">同日间隔</span><span class="mg-v">两条之间 ≥ 60 分钟</span></div>
      <div class="mg"><span class="mg-k">补跑窗口</span><span class="mg-v">默认往前补 ≤ 3 天</span></div>

      <div class="m-sec-title" style="margin-top:14px">设备与账号</div>
      <div class="mg"><span class="mg-k">每账号一设备</span><span class="mg-v">登录即随机绑定一台，此后固定；请勿手动频繁切换设备否则触发 10121。</span></div>
      <div class="mg"><span class="mg-k">切换账号</span><span class="mg-v">退出后登录新账号，会自动分配另一台设备，互不影响。</span></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<!-- 记录详情弹窗 -->
<div class="modal-mask" id="detailMask" style="display:none" onclick="if(event.target===this)closeDetail()">
  <div class="modal">
    <div class="modal-head">
      <span id="mTitle">跑步详情</span>
      <button class="gray small" style="margin:0;width:auto" onclick="closeDetail()">关闭</button>
    </div>
    <div id="mBody"></div>
  </div>
</div>

<script>
let state = null;
let logSince = -1;

function $(id){return document.getElementById(id)}
function toast(msg){const t=$("toast");t.textContent=msg;t.style.display="block";
  setTimeout(()=>t.style.display="none",3200)}

async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",
    headers:body?{"Content-Type":"application/json"}:{},
    body:body?JSON.stringify(body):undefined});
  return r.json();
}

async function getState(){
  state=await api("/api/state");
  render();
}

function render(){
  const logged=state&&state.logged;
  $("loginBtn").style.display=logged?"none":"block";
  $("logoutBtn").style.display=logged?"block":"none";
  $("loginState").innerHTML=logged
    ? ('<b class="ok">已登录：'+(state.name||state.uid||"")+'</b>')
    : '<b class="no">未登录</b>';
  $("userCard").style.display=logged?"block":"none";
  renderAccounts();   // 历史账号下拉：未登录时显示，已登录时隐藏
  if(state){
    // 设备/平台只读展示：由 fillDevices() 按 state.devices 渲染（= 账号绑定设备）
    fillDevices();
    if(!logged){
      // 未登录：不显示任何默认校区，仅提示登录后自动获取
      $("rCampus").value="（未登录，登录后自动获取校区）";
      return;
    }
  }
  if(!logged)return;
  $("uName").textContent=state.name||"-";
  $("uUid").textContent=state.uid||"-";
  $("uUnid").textContent=(state.campus||"unid "+state.unid)||"-";
  // 校区显示（含坐标来源；未登录/无坐标时显示提示，不再出现默认揭阳）
  let cstr="-";
  if(state.campus){cstr=state.campus;
    if(state.campus_lat!=null&&state.campus_lon!=null){
      const src={default:"默认",override:"自定义","server-fence":"服务端围栏",
                 known:"内置库","no-coord":"无坐标"}[state.campus_src]||"";
      cstr+="  ("+state.campus_lat.toFixed(5)+", "+state.campus_lon.toFixed(5)+" · "+src+")";
    }else{
      cstr+="  (登录后自动获取)";
    }}
  $("rCampus").value=cstr;
  $("uDevice").innerHTML=(state.device||"-")+" · "+(state.platform_label||"");
  // 用户卡片设备名 → 型号（选中即显示认证型号）
  if(state.device){refreshDevId(state.device);}
  if(state.stats){
    $("sTotal").textContent=state.stats.runCountLength??"-";
    $("sWeek").textContent=state.stats.weekRunLength??"-";
    $("sLong").textContent=state.stats.longestDistance? (state.stats.longestDistance/1000).toFixed(2):"-";
    $("sBest").textContent=state.stats.bestSpeed??"-";
  }
  renderStats();
  const byday=state.records_by_day||{};
  let hint="当日已满 2 条会自动阻止；随机时间会避开已有记录。";
  Object.keys(byday).sort().reverse().forEach(d=>{
    hint+="  "+d+":"+byday[d]+"条"+(byday[d]>=2?"(满)":"");
  });
  $("reconcileHint").textContent=hint;
  renderRecords();
}

/* 最近记录：按日对账，可点开查看当天详情 */
let selDay=null;
function renderRecords(){
  const rl=$("recList");
  const byday=state.records_by_day||{};
  const recs=state.records||[];
  if(!recs.length){rl.innerHTML='<div class="hint">暂无记录</div>';return;}
  const days=Object.keys(byday).sort().reverse();
  let html="";
  days.forEach(d=>{
    const n=byday[d];
    const isOpen=selDay===d;
    const dayRecs=recs.filter(r=>r.date===d);
    let inner="";
    if(isOpen){
      inner='<div style="margin:6px 0 0 6px;border-left:2px solid var(--line);padding-left:10px">';
      dayRecs.forEach(r=>{
        const km=(parseFloat(r.dist)||0)/1000;
        const vkm=(parseFloat(r.validDis)||0)/1000;
        const ok=r.complete!==false;
        const stf=r.avgStepFreq?(r.avgStepFreq+" 步/分"):"";
        inner+='<div style="padding:4px 0;font-size:13px;display:flex;align-items:center;gap:6px">'
          +'<span class="pill'+((ok)?" ok":"")+'">'+r.start+' '+((ok)?"达标":"未达")+'</span> '
          +'<b>'+km.toFixed(2)+' km</b> · 用时 '+(r.time||"-")+'s'
          +(r.validDis?' · 有效 '+vkm.toFixed(2)+' km':'')
          +(stf?' · '+stf:'')
          +'<span class="code" style="color:var(--sub)">#'+ (r.rrid||"-") +'</span>'
          +'<button class="gray small" style="margin:0;margin-left:auto;width:auto" onclick="showDetail('+ (r.rrid||0) +')">详情</button>'
          +'</div>';
      });
      inner+='</div>';
    }
    html+='<div style="margin:4px 0">'
      +'<span class="pill'+(n>=2?" red":"")+'" onclick="toggleDay(\''+d+'\')" '
      +'style="cursor:pointer;user-select:none">'
      +'▸ '+d+" · "+n+" 条"+(isOpen?" (收起)":" (展开)")
      +'</span>'+inner+'</div>';
  });
  rl.innerHTML=html;
}

function toggleDay(d){
  selDay=(selDay===d)?null:d;
  renderRecords();
}

/* ── 单次跑步详情弹窗 ── */
function fmtDate(ms){const d=new Date(ms);const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());}
// ★ 真机实测映射：1=计分跑、4=自由跑、6=户外跑
function sportName(t){t=Number(t);return t===1?"计分跑":(t===4?"自由跑":(t===6?"户外跑":"未知"));}

/* ── 使用说明弹窗 ── */
function openHelp(){const m=$("helpMask");m.style.display="flex";}
function closeHelp(){const m=$("helpMask");if(m)m.style.display="none";}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeHelp();});

function showDetail(rrid){
  const r=(state.records||[]).find(x=>String(x.rrid)===String(rrid));
  if(!r){toast("未找到该记录");return;}
  const km=(parseFloat(r.dist)||0)/1000;
  const vkm=(parseFloat(r.validDis)||0)/1000;
  const ok=r.complete!==false;
  const tSec=parseInt(r.time||0,10);
  const mm=tSec?Math.floor(tSec/60):0, ss=tSec?tSec%60:0;
  const durStr=mm? (mm+"分"+String(ss).padStart(2,"0")+"秒") : (ss+"秒");
  const vtm=parseInt(r.validTime||0,10);
  const vdurStr=vtm? (Math.floor(vtm/60)+"分"+String(vtm%60).padStart(2,"0")+"秒") : "-";
  // 配速：秒/公里
  let paceStr="-";
  if(tSec>0&&km>0){
    const p=tSec/km;paceStr=Math.floor(p/60)+":"+String(Math.round(p%60)).padStart(2,"0");
  }
  const stf=r.avgStepFreq? r.avgStepFreq+" 步/分":"-";
  // ★ sportType → 跑法（2026-09-17 真机实测）：1=计分跑(校园跑，需点位核验)、
  //   4=自由跑(无点位、不计成绩)、6=户外跑；其余取值服务端拒绝。
  const ST=Number(r.sportType);
  const isScore=(ST===1);
  const isFree=(ST===4);
  const typeTxt=isScore?"计分跑":(isFree?"自由跑":(ST===6?"户外跑":"未知"));
  const typeCls=isScore?"score":"free";
  const endMs=r.stop_ms||(r.start_ms+(r.time||0)*1000);
  const span=(endMs-(r.start_ms||0))/1000;
  const spStr=span>0? (Math.floor(span/60)+"分"+String(Math.round(span%60)).padStart(2,"0")+"秒") : "-";

  // 状态徽章 + 类型标签
  let html='<div class="m-hero">'
    +'<div class="m-badge '+(ok?"ok":"no")+'">'+(ok?"✓ 已达标":"✗ 未达标")+'</div>'
    +'<div class="m-type '+typeCls+'">'+typeTxt+'</div>'
    +'<div class="m-id">记录 #'+(r.rrid||"-")+'</div>'
    +'</div>';

  // 四象限指标卡：距离 / 用时 / 配速 / 步频
  html+='<div class="m-cards">'
    +'<div class="mc"><b>'+km.toFixed(2)+'</b><span>总距离 km</span></div>'
    +'<div class="mc"><b>'+durStr+'</b><span>总用时</span></div>'
    +'<div class="mc"><b>'+(paceStr==="-"?paceStr:paceStr+'')+'</b><span>平均配速 /km</span></div>'
    +'<div class="mc"><b>'+stf+'</b><span>平均步频</span></div>'
    +'</div>';

  // 明细网格
  const rows=[];
  if(r.start_ms){rows.push(["开始时间",fmtDate(r.start_ms)]);}
  if(r.stop_ms){rows.push(["结束时间",fmtDate(r.stop_ms)]);}
  rows.push(["有效距离",r.validDis? vkm.toFixed(2)+" km":"-"],
            ["有效用时",vdurStr],
            ["设备档案","—"]);
  html+='<div class="m-grid">'
    +rows.map(x=>'<div class="mg"><span class="mg-k">'+x[0]+'</span><span class="mg-v">'+x[1]+'</span></div>').join("")
    +'</div>';

  // 计分跑：额外展示打卡点；自由跑：暂无图表/打卡点提示
  if(isScore){
    html+='<div class="m-sec-title">打卡点</div>'
      +'<div class="hint" style="margin:4px 0 10px;color:var(--sub)">计分跑须经过服务端打卡点，轨迹会覆盖各点。</div>';
  }else{
    html+='<div class="m-sec-title">图表与打卡点</div>'
      +'<div class="hint" style="margin:4px 0 10px;color:var(--sub)">自由跑无打卡点、无实时曲线图表 —— 与真机 App 一致，仅展示上述数据。</div>';
  }

  $("mTitle").textContent="跑步详情 #"+(r.rrid||"");
  $("mBody").innerHTML=html;
  $("detailMask").style.display="flex";
}
function closeDetail(){$("detailMask").style.display="none";}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDetail();});

/* 统计卡点击：展示对应记录详情 */
let selStat=null;
function renderStats(){
  document.querySelectorAll(".stat .s.clickable").forEach(el=>{
    el.classList.toggle("sel", el.dataset.stat===selStat);
  });
  const sd=$("statDetail");
  if(!selStat){sd.innerHTML="";return;}
  const recs=state.records||[];
  if(!recs.length){sd.innerHTML='<div class="hint">暂无记录</div>';return;}
  // 按统计维度排序展示
  let list=[...recs];
  if(selStat==="total")list.sort((a,b)=>(b.dist||0)-(a.dist||0));
  else if(selStat==="week"){
    const wk=7*86400000;const now=Date.now();
    list=list.filter(r=>now-(r.start_ms||0)<wk).sort((a,b)=>(b.dist||0)-(a.dist||0));
    if(!list.length){sd.innerHTML='<div class="hint">本周暂无记录</div>';return;}
    sd.innerHTML='<div class="hint" style="margin-bottom:4px">本周记录 '+list.length+' 条：</div>'+fmtRecList(list);
    return;
  }
  else if(selStat==="long"){list.sort((a,b)=>(b.dist||0)-(a.dist||0));list=list.slice(0,1);}
  else if(selStat==="best"){
    list=list.filter(r=>(r.dist||0)>0&&(r.time||0)>0).sort((a,b)=>
      ((a.time||0)/(a.dist||0))-((b.time||0)/(b.dist||0))).slice(0,1);
  }
  sd.innerHTML='<div class="hint" style="margin-bottom:4px">'+statLabel(selStat)+'：</div>'+fmtRecList(list);
}
function fmtRecList(list){
  return list.map(r=>{
    const km=(parseFloat(r.dist)||0)/1000;
    const ok=r.complete!==false;
    return '<div style="padding:3px 0;font-size:13px">'
      +'<span class="pill'+(ok?" ok":"")+'">'+r.date+' '+r.start+' '+((ok)?"达标":"未达")+'</span> '
      +'<b>'+km.toFixed(2)+' km</b> · '+(r.time||"-")+'s · rrid '+(r.rrid||"-")
      +'</div>';
  }).join("");
}
function statLabel(k){
  return {"total":"全部记录（按距离降序）","long":"最长一次",
          "best":"最快配速一次","week":"本周"}[k]||k;
}
document.addEventListener("click",e=>{
  const s=e.target.closest&&e.target.closest(".stat .s.clickable");
  if(s){selStat=(selStat===s.dataset.stat)?null:s.dataset.stat;renderStats();}
});

/* 用户卡/表单回显设备认证型号 */
async function refreshDevId(name){
  if(!name)return;
  try{
    const res=await api("/api/device-info?name="+encodeURIComponent(name));
    if(res.ok){
      $("uDevice").innerHTML=name+" · "+(res.platform_label||"")
        +' &nbsp;<span class="code" style="font-size:12px">'+(res.model||"未知型号")+'</span>';
    }
  }catch(e){}
}

/* ── 设备展示：**只展示账号绑定的那一台**，不可切换（2026-09-29）──────────
   ★ 为什么不能切换：提交走的是 `swcli.load_identity()` 的**第 0 条** ——
     `device_bind.json` 里该账号绑定的设备，与「界面里选中的」无关。
     以前那个 99 台的下拉切了**根本不影响提交**，只制造「我换过设备了」的错觉。
   ★ 换账号（登录 / 登出）后 `getState()` 会重新拉取 ⇒ 自动换成该账号绑定的设备。 */
function fillDevices(){
  const d=((state&&state.devices)||[])[0];
  const view=$("rDeviceView"),hid=$("rDevice"),lbl=$("devIdLbl");
  if(d&&d.name){
    hid.value=d.name;
    view.value=dispDevName(d.name);
    view.title="账号绑定设备："+d.name+(d.bound?"（已绑定）":"");
    lbl.textContent=d.model?("· "+d.model):"";
    setPlatFromDevice(d.platform);
  }else{
    hid.value="";
    view.value=(state&&state.logged)?"未绑定设备":"本地默认设备";
    view.title="未查到设备档案：提交时直接用本地身份（不会新建档案）";
    lbl.textContent="";
    if(state&&state.platform){setPlatFromDevice(state.platform);}
  }
}

/* 别名去掉内部前缀，只给用户看机型名（guise-iPhone 6S → iPhone 6S） */
function dispDevName(n){return String(n||"").replace(/^guise-/,"");}

/* 平台只读展示：跟随绑定设备，不响应点击（select 本身已 disabled） */
function setPlatFromDevice(plat){
  const sel=$("rPlatform");
  if(sel&&plat){sel.value=plat;}
}

/* ── 历史账号：选择之前登录过的账号（只存本机浏览器）──────────────────────
   ★ 为什么放 localStorage 而不是后端文件：账号 + 姓名属于个人凭据，
     写进工程根会被 `pack_all.py` 的安全检查当成「账号痕迹」拦下，
     而且发布包**绝不能含账号数据**。放浏览器里天然与发布物隔离。
   ★ 姓名从哪来：登录成功后服务端返回的 `data.name`（与 `session.json` 同源）。 */
const ACCT_KEY="sw_accounts";
const ACCT_MAX=8;

function acctLabel(x){return (x.name?x.name+" · ":"")+(x.u||"");}

function loadAccounts(){
  try{
    const raw=localStorage.getItem(ACCT_KEY);
    if(raw){const a=JSON.parse(raw);if(Array.isArray(a))return a;}
    // 迁移旧版单条 sw_cred —— 老用户升级后仍能看到上次的账号
    const old=localStorage.getItem("sw_cred");
    if(old){
      const c=JSON.parse(decodeURIComponent(atob(old)));
      if(c&&c.u)return [{u:c.u,p:c.p||"",name:"",last:Date.now()}];
    }
  }catch(e){}
  return [];
}

function saveAccounts(list){
  try{localStorage.setItem(ACCT_KEY,JSON.stringify(list.slice(0,ACCT_MAX)));}catch(e){}
}

/* 登录成功后 upsert：姓名取服务端返回；密码只在勾选「记住密码」时保存 */
function rememberAccount(u,p,name){
  const list=loadAccounts().filter(x=>x&&x.u!==u);
  list.unshift({u:u,p:($("remember").checked?(p||""):""),name:name||"",last:Date.now()});
  saveAccounts(list);
  renderAccounts();
}

/* 渲染历史账号下拉：选项文字 =「姓名 · 手机号」（姓名在前） */
function renderAccounts(){
  const sel=$("acctSel");
  if(!sel)return;
  const list=loadAccounts().filter(x=>x&&x.u);
  if(!list.length||(state&&state.logged)){sel.style.display="none";return;}
  sel.innerHTML="";
  const ph=document.createElement("option");
  ph.value="";ph.textContent="选择历史账号…";
  sel.appendChild(ph);
  list.forEach(x=>{
    const o=document.createElement("option");
    o.value=x.u;o.textContent=acctLabel(x);
    sel.appendChild(o);
  });
  sel.value="";
  sel.style.display="";
}

/* 选中历史账号：填入手机号 +（若存过）密码 */
function pickAccount(u){
  if(!u)return;
  const x=loadAccounts().find(y=>y&&y.u===u);
  if(!x)return;
  $("username").value=x.u;
  $("password").value=x.p||"";
  $("remember").checked=!!x.p;
  $("password").focus();
  toast(x.p?("已填入 "+acctLabel(x)+" 的账号密码"):("已填入 "+acctLabel(x)+"，请补密码"));
}

async function doLogin(){
  const u=$("username").value,p=$("password").value;
  if(!u||!p){toast("请输入账号密码");return;}
  $("loginBtn").disabled=true;$("loginBtn").textContent="登录中…";
  try{
    const res=await api("/api/login",{username:u,password:p});
    if(res.ok){
      // 历史账号：姓名取服务端返回；密码只在勾选「记住密码」时保存
      try{
        rememberAccount(u,p,(res.data&&res.data.name)||"");
        toast($("remember").checked?"登录成功，已记住账号密码":"登录成功");
      }catch(e){toast("登录成功（但保存凭据失败）");}
      await getState();
    }
    else{toast("登录失败："+res.msg);}
  }catch(e){toast("请求失败:"+e);}
  $("loginBtn").disabled=false;$("loginBtn").textContent="登 录";
}

async function doLogout(){
  try{
    const res=await api("/api/logout",{});
    toast(res&&res.ok?"已退出登录":"退出失败");
  }catch(e){toast("退出请求失败:"+e);}
  // 无论如何都强制重新拉取状态：后端会话已清空 → logged=False → 界面回到登录条
  await getState();
}

async function refresh(){await getState();toast("已刷新");}

async function randomTime(){
  const dist=parseFloat($("rDist").value)||2.15;
  const pace=$("rPace").value||"5:40";
  const res=await api("/api/random-time",{dist,pace});
  if(res.ok&&res.time){
    // ★ 每次随机后刷新输入框 max 为当前时间，确保填入的也是过去/现在
    const d=new Date();const p2=n=>String(n).padStart(2,"0");
    $("rStart").max=d.getFullYear()+"-"+p2(d.getMonth()+1)+"-"+p2(d.getDate())+"T"+p2(d.getHours())+":"+p2(d.getMinutes());
    setStartInput(res.time);toast("随机时间已填入："+res.time);
  }
  else{toast("随机失败");}
}

/* datetime-local 与后端 "YYYY-MM-DD HH:MM:SS" 互转
   ★ 支持到秒：输入框 step=1 后值是 19 字符（含秒），也要兼容旧的 16 字符（到分）。 */
function toLocalInput(s){
  if(!s)return "";
  const t=s.replace(" ","T");
  return t.length>=19? t.slice(0,19) : t.slice(0,16);
}
function fromLocalInput(v){
  if(!v)return "";
  const t=v.replace("T"," ");
  return t.length>=19? t.slice(0,19) : t+":00";
}
/* 生成一个【过去】的起跑时间串（带随机秒）——整分整秒的 08:00:00 一眼看出是造的 */
function pastStartStr(secAgo){
  const d=new Date(Date.now()-(secAgo||90)*1000);
  const p2=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p2(d.getMonth()+1)+"-"+p2(d.getDate())+" "
    +p2(d.getHours())+":"+p2(d.getMinutes())+":"+p2(d.getSeconds());
}
function setStartInput(s){$("rStart").value=toLocalInput(s);}
function getStartInput(){return fromLocalInput($("rStart").value);}

/* 配速：把任意 "m:ss" 设到下拉选择器；不在预设里则选最接近的 */
/* 配速自定义下拉：预设值 + 搜索 + 限高滚动（与设备下拉同风格） */
const PACES=["4:00","4:15","4:30","4:45","5:00","5:15","5:30","5:45",
             "6:00","6:15","6:30","6:45","7:00","7:15","7:30","7:45",
             "8:00","8:15","8:30","8:45","9:00","9:15","9:30","9:45","10:00"];
const PACE_LBL={"4:00":"4:00（快）","5:40":"5:40（常用）","10:00":"10:00（慢）"};
let CUSTOM_PACES=[];   /* 用户自定义配速（本次会话保留，显示在列表里） */
/* 归一化配速："5:37" / "5'37\"" / "337"(秒) -> "5:37"；非法返回 null */
function normPace(s){
  if(s==null)return null;
  s=String(s).trim().replace(/["”″]/g,"").replace(/['’‘′]/g,":")
   .replace(/：/g,":").replace(/\s+/g,"");
  if(!s)return null;
  if(s.indexOf(":")<0){                       /* 纯数字 = 秒/公里 */
    const n=Number(s);
    if(!isFinite(n)||n<120||n>1800)return null;
    return Math.floor(n/60)+":"+String(Math.round(n%60)).padStart(2,"0");
  }
  const p=s.split(":");
  if(p.length!==2)return null;
  const m=Number(p[0]),ss=Number(p[1]);
  if(!isFinite(m)||!isFinite(ss)||m<2||m>30||ss<0||ss>59)return null;
  return m+":"+String(ss).padStart(2,"0");
}
function paceLabel(v){
  if(PACE_LBL[v])return PACE_LBL[v];
  return PACES.indexOf(v)>=0?v:v+"（自定义）";
}
/* ★ 设配速：保留原值（不再吸附到最近的预设 —— 那会把「一键设置」的历史平均配速取整） */
function setPaceSelect(pace){
  const v=normPace(pace);
  if(!v)return;
  if(PACES.indexOf(v)<0&&CUSTOM_PACES.indexOf(v)<0)CUSTOM_PACES.push(v);
  $("rPace").value=v;
  $("paceDDTxt").textContent=paceLabel(v);
  renderPaceList();
  updatePaceTotal();
}
function renderPaceList(){
  const kw=($("paceSearch").value||"").trim().toLowerCase();
  const cur=$("rPace").value;
  const box=$("paceOpts");box.innerHTML="";
  /* 预设 + 已添加的自定义 + 当前值（保证当前值总能出现在列表里） */
  const all=PACES.concat(CUSTOM_PACES.filter(v=>PACES.indexOf(v)<0));
  if(cur&&all.indexOf(cur)<0)all.push(cur);
  let shown=all;
  if(kw)shown=all.filter(v=>v.toLowerCase().indexOf(kw)>=0||(PACE_LBL[v]||"").indexOf(kw)>=0);
  if(!shown.length){
    const d=document.createElement("div");d.className="dd-none";
    d.textContent="（无匹配，可在上方自定义添加）";box.appendChild(d);return;
  }
  shown.forEach(v=>{
    const o=document.createElement("div");
    o.className="dd-opt"+(v===cur?" sel":"");
    o.textContent=paceLabel(v);
    o.onclick=()=>setPaceValue(v);
    box.appendChild(o);
  });
}
function setPaceValue(v){
  $("rPace").value=v;
  $("paceDDTxt").textContent=paceLabel(v);
  closePaceDD();
  renderPaceList();
  updatePaceTotal();
}
/* 自定义配速：读输入框 -> 校验 -> 设为当前配速 */
function applyCustomPace(){
  const el=$("paceCustomIn");if(!el)return;
  const v=normPace(el.value);
  if(!v){toast("配速格式不对：请填 分:秒（如 5:37），或直接填秒数（如 337）");return;}
  if(PACES.indexOf(v)<0&&CUSTOM_PACES.indexOf(v)<0)CUSTOM_PACES.push(v);
  el.value="";
  closePaceDD();
  setPaceValue(v);
  toast("已设为自定义配速 "+v);
}
function togglePaceDD(e){
  e=e||window.event;
  if(e&&e.stopPropagation)e.stopPropagation();
  const box=$("paceDDBox");
  const willOpen=!box.classList.contains("open");
  box.classList.toggle("open",willOpen);
  if(willOpen){$("paceSearch").value="";renderPaceList();$("paceSearch").focus();}
}
function closePaceDD(){const b=$("paceDDBox");if(b)b.classList.remove("open");}
/* 点击页面其他区域收起配速下拉 */
document.addEventListener("click",e=>{
  const box=$("paceDDBox");
  if(box&&!box.contains(e.target))closePaceDD();
});

/* 模式自定义下拉：自由跑/计分跑，与配速/设备同风格 */
const MODES=[{v:"free",t:"自由跑（校园范围）"},{v:"score",t:"计分跑（需打卡点）"}];
function toggleModeDD(e){
  e=e||window.event;
  if(e&&e.stopPropagation)e.stopPropagation();
  const box=$("modeDDBox");
  const willOpen=!box.classList.contains("open");
  box.classList.toggle("open",willOpen);
  if(willOpen)renderModeList();
}
function closeModeDD(){const b=$("modeDDBox");if(b)b.classList.remove("open");}
function renderModeList(){
  const cur=$("rMode").value;
  const box=$("modeOpts");box.innerHTML="";
  MODES.forEach(m=>{
    const o=document.createElement("div");
    o.className="dd-opt"+(m.v===cur?" sel":"");
    o.textContent=m.t;
    o.onclick=()=>setModeValue(m.v);
    box.appendChild(o);
  });
}
function setModeValue(v){
  const m=MODES.find(x=>x.v===v)||MODES[0];
  $("rMode").value=m.v;
  $("modeDDTxt").textContent=m.t;
  closeModeDD();
  renderModeList();
}
/* 点击页面其他区域收起模式下拉 */
document.addEventListener("click",e=>{
  const box=$("modeDDBox");
  if(box&&!box.contains(e.target))closeModeDD();
});
function syncModeDD(){
  const cur=$("rMode").value;
  const m=MODES.find(x=>x.v===cur);
  if(m)$("modeDDTxt").textContent=m.t;
}

/* 总用时 ≈ 距离 × 配速，随距离/配速变化实时刷新 */
function updatePaceTotal(){
  const el=$("paceTotal");
  if(!el)return;
  const dist=parseFloat($("rDist").value);
  const paceStr=$("rPace").value||"5:40";
  if(!dist||dist<=0){
    el.textContent="总用时 ≈ —";
    return;
  }
  const m=paceStr.split(":").map(Number);
  const secPerKm=(m[0]||0)*60+(m[1]||0);
  const total=Math.round(dist*secPerKm);           // 秒
  const h=Math.floor(total/3600),mm=Math.floor((total%3600)/60),ss=total%60;
  const p2=n=>String(n).padStart(2,"0");
  el.textContent="总用时 ≈ "+(h>0?h+":"+p2(mm):mm)+":"+p2(ss);
}

/* 一键设置：按历史跑步记录平均值填好所有字段 */
async function autoConfig(){
  const btn=$("autoCfgBtn");
  const hint=$("autoCfgHint");
  btn.disabled=true;btn.textContent="计算历史平均中…";
  try{
    const res=await api("/api/auto-config");
    if(!res.ok){
      hint.textContent="";
      toast("无法生成："+(res.msg||"请先登录并跑过至少一次"));
      return;
    }
    // 距离 / 配速 / 时间 / 设备，全部按历史平均填入
    if(res.dist_km){$("rDist").value=res.dist_km;}
    if(res.pace){setPaceSelect(res.pace);}
    if(res.start){setStartInput(res.start);}
    // 推荐设备（账号绑定设备优先）→ 平台自动跟随绑定设备
    // ★ 2026-09-29：设备已改为**只读展示绑定设备**，不再「选中」——
    //   这里只做兜底刷新，真正的设备由 fillDevices() 按 state.devices 渲染。
    fillDevices();
    hint.innerHTML='已按历史平均生成：距离 <b>'+res.dist_km+' km</b> · 配速 <b>'+res.pace
      +'</b> · 起跑 <b>'+res.start+'</b> · 平台 '+res.platform_label
      +' &nbsp;<span style="color:var(--sub)">'+res.note+'</span>'
      +(res.prefer_hour!=null?('（历史平均 '+res.prefer_hour+' 点附近）'):'');
    toast("已自动填充（"+res.note+"）");
  }catch(e){hint.textContent="";toast("请求失败:"+e);}
  finally{btn.disabled=false;btn.textContent="一键设置（按历史平均）";}
}

async function doRun(){
  if(!(state&&state.logged)){toast("请先登录再提交");$("username").focus();
    appendLogLine(new Date().toTimeString().slice(0,8),"err","提交被阻止：未登录，请先登录");
    return;}
  const btn=$("runBtn");
  if(btn.dataset.busy==="1"){toast("正在提交中，请等待完成…");return;}
  const mode=$("rMode").value,dist=parseFloat($("rDist").value)||2.15,
        pace=$("rPace").value,start=getStartInput(),
        device=$("rDevice").value,weight=parseFloat($("rWeight").value)||65,
        platform=$("rPlatform").value;
  // ★ 轨迹形状（2026-09-29）：椭圆 = 最稳（默认）；标准跑道 = 两个半圆 + 两条直道
  const shape=($("rShape")&&$("rShape").value)||"track";
  if(!device||device==="__none__"){toast("请先选择/新建设备");return;}
  if(!start){toast("请选择开始时间（可点「随机」）；留空则用当前时间");start=null;}
  btn.dataset.busy="1";btn.disabled=true;btn.classList.add("running");
  const bt=btn.querySelector(".sh-txt");bt.textContent="提交中";
  toast("正在提交…生成轨迹→对账→提交→OBS 上传（看下方日志）");
  try{
    // 把当前校区坐标一并带上（后端优先使用；未登录后端会回退默认揭阳）
    const lat=(state&&state.campus_lat!=null)?state.campus_lat:null;
    const lon=(state&&state.campus_lon!=null)?state.campus_lon:null;
    // ★「强制」复选框此前是**死控件**：后端 do_run/run_all 早就支持 force，
    //   但这里从来不读 rForce、请求体里也没有 force 字段 —— 于是「打卡点不可达」
    //   被拦下时，用户界面里根本没有放行入口（run_all 只提示「加 --force」）。
    const force=!!($("rForce")&&$("rForce").checked);
    const res=await api("/api/run",{mode,dist,pace,start,device,weight,platform,lat,lon,force,shape});
    toast(res.ok?"提交成功":"提交被阻止/失败");
    await getState();
  }catch(e){toast("提交异常:"+e);}
  finally{btn.dataset.busy="0";btn.disabled=false;btn.classList.remove("running");
    bt.textContent="提交跑步记录";
    // ★ 跑完自动把「开始时间」刷新为当前时间：页面不刷新时该输入框的值不会变，
    //   若沿用旧值会让下一次生成出**完全相同**的轨迹（起跑时间相同=种子相同）。
    try{const d=new Date();const p2=n=>String(n).padStart(2,"0");
      const localNow=d.getFullYear()+"-"+p2(d.getMonth()+1)+"-"+p2(d.getDate())+"T"+p2(d.getHours())+":"+p2(d.getMinutes());
      $("rStart").max=localNow;
      setStartInput(pastStartStr(90));   // ★ 带随机秒，避免整分整秒
    }catch(e){}
  }
}

/* 日志 */
const LVCOLOR={"info":"#4b5563","ok":"#2e7d32","warn":"#b26a00","err":"#c62828"};
function appendLogLine(ts,lv,msg){
  const p=$("logPanel");
  const div=document.createElement("div");
  const lt=document.createElement("span");
  lt.className="lt";lt.textContent="["+ts+"]";
  const lm=document.createElement("span");
  lm.className="lm";lm.style.color=LVCOLOR[lv]||"#4b5563";
  lm.textContent=msg;
  div.appendChild(lt);div.appendChild(lm);
  p.appendChild(div);
  while(p.children.length>500)p.removeChild(p.firstChild);
  p.scrollTop=p.scrollHeight;
}

async function pollLog(){
  try{
    const d=await api("/api/log?since="+logSince);
    if(d.lines&&d.lines.length){
      if(logSince===-1){$("logPanel").innerHTML="";}
      d.lines.forEach(l=>appendLogLine(l[1],l[2],l[3]));
      logSince=d.lines[d.lines.length-1][0];
    }
  }catch(e){}
}

function clearLogView(){
  const p=$("logPanel");p.innerHTML="";
  p.appendChild(Object.assign(document.createElement("div"),
    {className:"hint",textContent:"（显示已清空，新日志继续追加）"}));
}

/* 平台只读跟随设备、配速走自定义下拉；仅距离/体重等数值 input 触发总用时刷新 */
document.addEventListener("input",e=>{
  if(e.target.id==="rDist"){updatePaceTotal();}
});

(async function init(){
  // 回填最近一次登录过的账号（有存密码就一起填）
  let restoredPwd=false;
  try{
    const list=loadAccounts();
    if(list.length){
      const x=list[0];
      if(x.u)$("username").value=x.u;
      if(x.p){$("password").value=x.p;$("remember").checked=true;restoredPwd=true;}
    }
  }catch(e){}
  await getState();
  updatePaceTotal();
  syncModeDD();   // 模式下拉显示与默认值同步
  const d=new Date();
  const p2=n=>String(n).padStart(2,"0");
  // ★ 开始时间默认=当前时间（不要写死 18:30 —— 那在早上会是未来时间）
  // 并给 datetime-local 设 max=当前时间，禁止用户选未来时刻。
  const localNow=d.getFullYear()+"-"+p2(d.getMonth()+1)+"-"+p2(d.getDate())+"T"+p2(d.getHours())+":"+p2(d.getMinutes());
  $("rStart").max=localNow;
  setStartInput(pastStartStr(90));   // ★ 带随机秒，避免 08:00:00 这种整分整秒
  setInterval(pollLog,1000);
  if(restoredPwd&&!state.logged){
    setTimeout(()=>toast("已自动填入记住的账号密码，点「登录」即可"),600);
  }
})();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════
# 启动
# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="运动世界校园 可视化工具")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    url = "http://%s:%d" % (HOST, a.port)
    # Windows 上 SO_REUSEADDR 允许重复绑定，先探测已有实例
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/api/state", timeout=4) as r2:
            body = r2.read()
        if b"logged" in body:
            print("检测到控制台已在运行，直接打开浏览器复用…")
            if not a.no_browser:
                webbrowser.open(url)
            return
    except Exception:
        pass

    swcli.seed_devices_from_identity()

    log_write("控制台启动，端口 %d（%s）" % (a.port, time.strftime("%F %T")))
    print("=" * 56)
    print(" 运动世界校园 · 可视化工具")
    print(" 访问: %s" % url)
    print(" 按 Ctrl+C 停止")
    print("=" * 56)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv = ThreadingHTTPServer((HOST, a.port), Handler)
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
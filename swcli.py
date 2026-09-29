#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swcli.py — 运动世界校园 PC 端命令行工具

功能：
  login   使用账号密码登录（含 GT4 滑块破解），保存会话
  whoami  显示当前登录态
  call    调用任意接口（自动加解密）
  probe   探测常用跑步接口，输出明文业务数据

用法：
  python swcli.py whoami
  python swcli.py call POST /api/v70100/run/getHistoryConfig '{}'
  python swcli.py probe

配置/会话文件（与本脚本同目录）：
  identity.json  设备身份（device_id / app_install_time，必须持久稳定）
  session.json   登录态（uid / token / unid / name）
"""
import argparse
import json
import os
import random
import sys
import time

import swclient as sw

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY_FILE = os.path.join(HERE, "identity.json")
DEVICES_FILE = os.path.join(HERE, "devices.json")
# ★ 发布包自带的**只读设备池**（已脱敏）：见 `load_devices()` 的合并逻辑
DEVICES_POOL_FILE = os.path.join(HERE, "devices_pool.json")
SESSION_FILE = os.path.join(HERE, "session.json")
ACTIVE_FILE = os.path.join(HERE, "active_device.txt")
BIND_FILE = os.path.join(HERE, "device_bind.json")

# ══════════════════════════════════════════════════════════════════
# 账号 → 设备 绑定（首登固定：每个账号随机一台设备，登录/提交都用它）
# ══════════════════════════════════════════════════════════════════
def load_binds() -> dict:
    """{ 手机号: {"alias": 设备名, "bind_time": ms, "uid": int} }"""
    if os.path.exists(BIND_FILE):
        try:
            return json.load(open(BIND_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_binds(b: dict):
    json.dump(b, open(BIND_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def get_bind_alias(username: str) -> str:
    """取某账号绑定的设备别名（未绑定返回 ''）。"""
    if not username:
        return ""
    return str(load_binds().get(str(username), {}).get("alias", "") or "")


# ══════════════════════════════════════════════════════════════════
# 设备档案库（多设备选择）
# ══════════════════════════════════════════════════════════════════
# ★ 池子里禁止分发的键（本地语义，不是机型模板）
POOL_SKIP_KEYS = {"默认"}


def load_devices() -> dict:
    """{ 设备别名: {device_id, app_install_time, os_version, device_name, city, ...} }

    ★★ 2026-09-29 修「设备库只有一个」（用户反馈）：
      发布包（尤其**手机包**）此前不带设备库 ⇒ 本函数返回空 ⇒
      `seed_devices_from_identity()` 只播种 **1 台「默认」**，用户看到
      「之前很多设备都不见了」（实测 win 包 99 台 / mobile 包仅 1 台）。
      现在发布包带一份**只读设备池** `devices_pool.json`（已脱敏）：
        · 本地库为空  → 直接用池子初始化（首次运行就有全部设备）
        · 本地库非空  → 把池子里**缺的**设备并进来（老包升级也能补齐）
      ★ 同名设备**不覆盖** —— 用户自己改过的优先。
      ★ 为什么用 `devices_pool.json` 而不是直接放 `devices.json`：
        `devices.json` 是**运行时可变**的本地库（.gitignore 已排除），
        若包内同名，用户覆盖安装时会丢掉自己新增/修改的设备。
    """
    d = {}
    if os.path.exists(DEVICES_FILE):
        try:
            d = json.load(open(DEVICES_FILE, encoding="utf-8"))
        except Exception:
            d = {}
    if os.path.exists(DEVICES_POOL_FILE):
        try:
            pool = json.load(open(DEVICES_POOL_FILE, encoding="utf-8"))
        except Exception:
            pool = {}
        added = 0
        for k, v in pool.items():
            # ★★ 2026-09-29 修「默认设备被池子顶掉」：`默认` 是**本地语义**
            #   （= 本机指纹），不是机型模板，绝不允许从池子分发。
            #   历史池子里误带了一台别人的 `默认`（22081212C / 大连市），
            #   导致手机端 `devs` 永不为空、真机 `默认` 永远播不进来。
            if k in POOL_SKIP_KEYS:
                continue
            if k not in d:
                d[k] = v
                added += 1
        if added:                       # 只在真的补进来时才落盘
            try:
                save_devices(d)
            except Exception:
                pass
    return d


def save_devices(d: dict):
    json.dump(d, open(DEVICES_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def get_active_name() -> str:
    if os.path.exists(ACTIVE_FILE):
        return open(ACTIVE_FILE, encoding="utf-8").read().strip()
    return ""


def set_active_name(name: str):
    open(ACTIVE_FILE, "w", encoding="utf-8").write(name)


def seed_devices_from_identity():
    """把当前 identity.json 作为 '默认' 设备登入档案库（幂等）"""
    devs = load_devices()
    ident = load_identity()
    # ★ 判据必须是「'默认' 在不在」，不能用 `not devs`：
    #   带设备池后 devs 至少 98 台，`not devs` 恒为 False ⇒ 真机永远播不进来。
    if "默认" not in devs:
        nd = ident.to_dict()
        nd["_note"] = "初始导入"
        devs["默认"] = nd
        save_devices(devs)
    if not get_active_name():
        set_active_name("默认")
    return devs


# ── disguise / guise 设备档案导入 ─────────────────────────────────
# 这类档案通常是一批「真实设备指纹」，字段名差异较大，常见别名有：
#   device_id / deviceId / id / uuid
#   device_name / deviceModel / model / phoneModel / brand+model
#   os_version / osVersion / os / android
#   app_install_time / installTime / install_time / appInstallTime
#   city / location / area
#   platform / os_type / osType (android/ios)
_DEV_KEY_ALIASES = {
    "device_id": ["device_id", "deviceid", "device_id_str", "id", "uuid",
                  "device_id_encoded", "rawDeviceId"],
    "device_name": ["device_name", "devicename", "deviceModel", "devicemodel",
                    "model", "phone_model", "phonemodel", "device"],
    "os_version": ["os_version", "osversion", "osVersion", "os", "android",
                   "android_version", "os_ver"],
    "app_install_time": ["app_install_time", "appinstalltime", "installTime",
                         "installtime", "install_time", "appInstallTime",
                         "first_install_time"],
    "city": ["city", "location", "area", "region", "address"],
    "platform": ["platform", "os_type", "ostype", "system"],
}


def _pick_dev(v: dict, field: str, default=None):
    """按别名表取字段值（不区分大小写，兼容 dict/list/嵌套）。"""
    if not isinstance(v, dict):
        return default
    for k in _DEV_KEY_ALIASES.get(field, [field]):
        for kk in list(v.keys()):
            if kk.lower() == k.lower():
                val = v[kk]
                # 嵌套 dict（如 {"data": {...}}）取一层
                while isinstance(val, dict) and val:
                    first = next(iter(val.values()))
                    if isinstance(first, (dict, list)):
                        val = first
                    else:
                        break
                if val not in (None, "", {}):
                    return val
    return default


def _import_devices_json(args, devs: dict) -> int:
    """从 JSON 文件批量导入设备档案。

    支持两种结构：
      A. {"设备名": {...设备字段...}, ...}        直接按名导入
      B. [ {...设备字段..., "name"/"alias": 名字}, ... ]  数组按 name/alias 导入
    每套档案字段用别名表归一化（device_id/model/os/city 等），
    缺省的 os_version/city 用当前身份补齐。
    """
    path = getattr(args, "json_file", None) or (args.name if args.name else "")
    if not path or not os.path.exists(path):
        print("[ERR] 找不到 JSON 文件: %s" % path)
        print("      用法: python swcli.py devices import-json <file.json> [--prefix 张三-] [--force]")
        return 2
    try:
        obj = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print("[ERR] JSON 解析失败: %s" % e)
        return 2

    # 归一化为一组 (别名, 字段dict)
    entries = []  # list of (name, dict)
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                nm = _pick_dev(v, "device_name") or k
                entries.append((k, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if not isinstance(v, dict):
                continue
            nm = (v.get("name") or v.get("alias") or v.get("deviceName")
                  or v.get("device_name") or "档案%d" % (i + 1))
            entries.append((str(nm), v))
    else:
        print("[ERR] 不支持的 JSON 结构（应为 dict 或 list）")
        return 2

    if not entries:
        print("[!] 没有可导入的设备档案")
        return 1

    base = getattr(args, "prefix", "") or ""
    cur = load_identity()
    imported = skipped = 0
    for raw_name, v in entries:
        name = base + str(raw_name)
        if name in devs and not args.force:
            print("  [skip] %s（已存在，--force 覆盖）" % name)
            skipped += 1
            continue
        # 逐字段归一化
        device_id = _pick_dev(v, "device_id")
        if not device_id:
            # 档案缺少 device_id 直接跳过（device_id 是核心身份）
            print("  [skip] %s（缺 device_id，无法导入）" % name)
            skipped += 1
            continue
        platform = str(_pick_dev(v, "platform") or cur.platform).lower()
        platform = "ios" if platform in ("ios", "1") else "android"
        nd = {
            "device_id": str(device_id),
            "device_name": str(_pick_dev(v, "device_name") or cur.device_name),
            "os_version": str(_pick_dev(v, "os_version") or cur.os_version),
            "app_install_time": _pick_dev(v, "app_install_time")
                                or cur.app_install_time,
            "city": str(_pick_dev(v, "city") or cur.city),
            "platform": platform,
        }
        if isinstance(nd["app_install_time"], str):
            try:
                nd["app_install_time"] = int(nd["app_install_time"])
            except ValueError:
                nd["app_install_time"] = cur.app_install_time
        nd["_note"] = "disguise/guise 导入"
        devs[name] = nd
        imported += 1
        print("  [OK] %s  device_id=%s %s platform=%s"
              % (name, nd["device_id"][:24],
                 nd["device_name"], nd["platform"]))
    save_devices(devs)
    print("-" * 56)
    print("导入完成：新增 %d，跳过 %d，档案总数 %d"
          % (imported, skipped, len(devs)))
    print("切换: python swcli.py devices use <别名>")
    return 0 if imported or not skipped else 1


# ══════════════════════════════════════════════════════════════════
# Guise 设备模板池 → 设备档案池（import-guise）
# ══════════════════════════════════════════════════════════════════
def _guise_brand_os(brand: str, name: str) -> str:
    """按品牌推断默认 os_version（Android 14 / iOS 18 系）。"""
    b = (brand or "").lower()
    if b == "apple":
        return "18.0"
    if any(k in (name or "").lower() for k in ("iphone", "ipad")):
        return "18.0"
    # 较新机型给 15/16/17，简单给 14
    return "14"


def _import_guise(args, devs: dict) -> int:
    """导入 Guise 设备模板（json.txt）为设备档案池。

    Guise 模板数组每项形如:
      {"id": "uuid", "name": "K60U",
       "configuration": "{\"brand\":\"Redmi\",\"model\":\"23078RKD5C\",...}"}
    这里把它转成 swcli 设备档案：
      · device_id  = 模板 id（uuid）
      · device_name= brand + " " + model
      · platform   = brand=Apple -> ios，否则 android
      · os_version = 按品牌推断默认
      · app_install_time = 0（绑定时设为账号首登时间）
    """
    path = getattr(args, "json_file", None) or (args.name if args.name else "")
    if not path or not os.path.exists(path):
        print("[ERR] 找不到文件: %s" % path)
        print("      用法: python swcli.py devices import-guise <Guise-Template.json.txt> [--force]")
        return 2
    try:
        arr = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print("[ERR] 解析失败: %s" % e)
        return 2
    if isinstance(arr, dict):
        arr = arr.get("data") or arr.get("list") or []
    if not isinstance(arr, list):
        print("[ERR] Guise 模板应为数组")
        return 2

    imported = skipped = 0
    for it in arr:
        if not isinstance(it, dict):
            continue
        gid = str(it.get("id") or "").strip()
        name = str(it.get("name") or "").strip()
        if not gid:
            skipped += 1
            continue
        # 解析内层 configuration
        cfg = {}
        c = it.get("configuration")
        if isinstance(c, dict):
            cfg = c
        elif isinstance(c, str) and c:
            try:
                cfg = json.loads(c)
            except Exception:
                cfg = {}
        brand = str(cfg.get("brand") or "unknown")
        model = str(cfg.get("model") or name)
        dev_name = ("%s %s" % (brand, model)).strip()
        platform = "ios" if brand.lower() == "apple" else "android"
        osv = _guise_brand_os(brand, name)

        alias = "guise-" + name
        if alias in devs and not args.force:
            print("  [skip] %s（已存在，--force 覆盖）" % alias)
            skipped += 1
            continue

        devs[alias] = {
            "device_id": gid,
            "device_name": dev_name,
            "os_version": osv,
            "app_install_time": 0,
            "city": "",
            "platform": platform,
            "_note": "guise 模板导入",
            "_pool": True,
        }
        imported += 1
        print("  [OK] %-14s %-22s %s  os=%s"
              % (alias, dev_name, platform, osv))

    save_devices(devs)
    print("-" * 56)
    print("Guise 模板导入完成：新增 %d，跳过 %d，设备池 %d 台"
          % (imported, skipped, len(devs)))
    print("绑定: 登录会自动给账号随机分配一台；也可 python swcli.py devices bind <账号>")
    return 0 if imported or not skipped else 1


# ══════════════════════════════════════════════════════════════════
# 账号 → 设备 绑定（首登固定）
# ══════════════════════════════════════════════════════════════════
def ensure_account_device(username: str):
    """确保某账号绑定到一台设备，并返回其 Identity。

    规则：
      · 该账号已绑定 → 复用绑定设备（绝不更换）。
      · 未绑定 → 从设备库随机选一台【未被其他账号占用】的档案，
                 prefer 池子里（_pool=True）的 guise 模板。
                 app_install_time 设为当前时间（该账号首登，即"下载那天"）。
      绑定后：写入 device_bind.json + 设为 active + 同步 identity.json。
    """
    username = str(username or "").strip()
    if not username:
        return load_identity()
    devs = load_devices()
    binds = load_binds()

    # 1) 已绑定 → 复用
    if username in binds:
        alias = binds[username].get("alias")
        if alias in devs:
            if get_active_name() != alias:
                set_active_name(alias)
                save_identity(sw.Identity.from_dict(devs[alias]))
            return sw.Identity.from_dict(devs[alias])

    # 2) 未绑定 → 随机分配
    used = {b.get("alias") for b in binds.values() if b.get("alias")}
    # 优先选未被占用的池模板（guise）
    pool = [k for k, d in devs.items()
            if d.get("_pool") and k not in used]
    if not pool:
        pool = [k for k, d in devs.items()
                if k not in used and not d.get("_pool")]
    if not pool:
        pool = [k for k in devs if k not in used]
    if not pool:
        print("[warn] 设备库为空，无法为账号绑定；请先 devices import-guise")
        return load_identity()

    alias = random.choice(pool)
    nd = dict(devs[alias])
    # app_install_time：账号首登时间（"从下载那天起"）
    if not nd.get("app_install_time"):
        nd["app_install_time"] = int(time.time() * 1000)
        devs[alias] = nd
        save_devices(devs)

    binds[username] = {"alias": alias, "bind_time": int(time.time() * 1000),
                       "uid": 0}
    save_binds(binds)
    set_active_name(alias)
    save_identity(sw.Identity.from_dict(nd))

    print("  [绑定] 账号 %s → 设备 %s（%s，platform=%s）"
          % (username, alias, nd.get("device_name"), nd.get("platform")))
    return sw.Identity.from_dict(nd)


def cmd_devices(args):
    """列出 / 切换 / 新增 / 删除设备档案"""
    devs = seed_devices_from_identity()
    act = get_active_name()

    if args.devices_cmd == "list" or args.devices_cmd is None:
        print("=" * 68)
        print("设备档案库（%d 个）  当前使用: %s" % (len(devs), act or "—"))
        print("=" * 68)
        if not devs:
            print("  （空）")
            return 0
        print("  %-4s %-14s %-34s %-18s %s"
              % ("标记", "别名", "device_id", "机型", "城市"))
        print("  " + "-" * 66)
        for name, d in devs.items():
            mark = "★" if name == act else " "
            print("  %-4s %-16s %-34s %-18s %s"
                  % (mark, name, d.get("device_id", "")[:34],
                     d.get("device_name", "")[:18], d.get("city", "")))
        print("  " + "-" * 66)
        print("  切换: python swcli.py devices use <别名>")
        return 0

    if args.devices_cmd == "use":
        name = args.name
        if name not in devs:
            print("[ERR] 无此设备: %s" % name)
            print("      现有: %s" % ", ".join(devs.keys()))
            return 2
        set_active_name(name)
        # 同时把 identity.json 同步成该设备（旧代码全走 identity.json）
        ident = sw.Identity.from_dict(devs[name])
        save_identity(ident)
        print("[OK] 已切换到设备 '%s'" % name)
        print("     device_id = %s" % ident.device_id)
        print("     机型      = %s  城市 = %s" % (ident.device_name, ident.city))
        print("[!] device_id 变更后，旧 session 可能失效；如遇 10121 请重新登录")
        return 0

    if args.devices_cmd == "import":
        # 从当前 identity.json 再存一份（用于把抓包设备另存）
        name = args.name
        if name in devs and not args.force:
            print("[ERR] 别名已存在: %s（加 --force 覆盖）" % name)
            return 2
        ident = load_identity()
        nd = ident.to_dict()
        if args.note:
            nd["_note"] = args.note
        devs[name] = nd
        save_devices(devs)
        print("[OK] 已导入为 '%s': device_id=%s" % (name, ident.device_id))
        return 0

    if args.devices_cmd == "import-json":
        # 批量导入设备档案（支持 disguise / guise 类多设备档案库）
        # 用法: swcli devices import-json <file.json> [--prefix 前缀] [--force]
        return _import_devices_json(args, devs)

    if args.devices_cmd == "import-guise":
        # Guise 设备模板池 → 设备档案池（每账号随机绑定一台）
        # 用法: swcli devices import-guise <Guise-Template.json.txt> [--force]
        return _import_guise(args, devs)

    if args.devices_cmd == "bind":
        # 查看账号绑定 / 为账号手动分配设备
        # 用法: swcli devices bind <账号>    或  swcli devices bind list
        if args.name == "list" or not args.name:
            binds = load_binds()
            print("=" * 60)
            print("账号 → 设备 绑定（%d 个）" % len(binds))
            print("=" * 60)
            if not binds:
                print("  （空）登录后自动绑定")
            for u, b in binds.items():
                alias = b.get("alias", "")
                d = devs.get(alias, {})
                print("  %-13s -> %-12s %s  (%s)"
                      % (str(u)[:11] + "*" if len(str(u)) > 11 else str(u),
                         alias, d.get("device_name", ""),
                         b.get("bind_time", "")))
            print("  " + "-" * 56)
            print("  手动分配: python swcli.py devices bind <账号>")
            return 0
        # 手动为账号绑定/重绑一台
        if args.force:
            binds = load_binds()
            binds.pop(args.name, None)
            save_binds(binds)
        ident = ensure_account_device(args.name)
        print("[OK] 账号 %s → device_id=%s %s"
              % (args.name, ident.device_id[:24], ident.device_name))
        return 0

    if args.devices_cmd == "unbind":
        # 解绑账号（下次登录重新分配新设备）
        if not args.name:
            print("用法: python swcli.py devices unbind <账号>")
            return 2
        binds = load_binds()
        if args.name not in binds:
            print("[!] 该账号未绑定")
            return 0
        binds.pop(args.name)
        save_binds(binds)
        print("[OK] 已解绑 %s（下次登录会重新分配）" % args.name)
        return 0

    if args.devices_cmd == "del":
        name = args.name
        if name not in devs:
            print("[ERR] 无此设备: %s" % name)
            return 2
        if name == act:
            print("[ERR] '%s' 正在使用中，先切到别的再删" % name)
            return 2
        devs.pop(name)
        save_devices(devs)
        print("[OK] 已删除 '%s'" % name)
        return 0

    print("用法: devices [list|use <别名>|import <别名>|del <别名>]")
    return 1


# ══════════════════════════════════════════════════════════════════
# 持久化
# ══════════════════════════════════════════════════════════════════
def load_identity() -> sw.Identity:
    # 0) 当前已登录账号的绑定设备优先（首登固定，登录/提交都用它）
    try:
        sess = load_session()
        uname = sess.get("username", "")
        if uname:
            b_alias = get_bind_alias(uname)
            if b_alias:
                devs = load_devices()
                if b_alias in devs:
                    return sw.Identity.from_dict(devs[b_alias])
    except Exception:
        pass
    # 1) 若档案库指定了活跃设备，优先用它，保证 device_id 稳定
    act = get_active_name()
    if act:
        devs = load_devices()
        if act in devs:
            try:
                return sw.Identity.from_dict(devs[act])
            except Exception:
                pass
    if os.path.exists(IDENTITY_FILE):
        try:
            d = json.load(open(IDENTITY_FILE, encoding="utf-8"))
            return sw.Identity.from_dict(d)
        except Exception:
            pass
    ident = sw.Identity()
    save_identity(ident)
    print("[identity] 首次生成设备身份并落盘: device_id=%s" % ident.device_id)
    return ident


def save_identity(ident: sw.Identity):
    json.dump(ident.to_dict(), open(IDENTITY_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def load_session() -> dict:
    if os.path.exists(SESSION_FILE):
        try:
            return json.load(open(SESSION_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_session(s: dict):
    json.dump(s, open(SESSION_FILE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════
# 请求核心
# ══════════════════════════════════════════════════════════════════
class Client:
    def __init__(self):
        self.identity = load_identity()
        self.session = load_session()
        self.env = None  # 懒创建：一次进程内 keyDataFour 复用

    @property
    def uid(self) -> int:
        return int(self.session.get("uid", 0) or 0)

    @property
    def token(self) -> str:
        return self.session.get("token", "") or ""

    def _ensure_env(self):
        if self.env is None:
            self.env = sw.EnvelopeSession()
            print("[envelope] keyDataFour=%s pAesKey=%s"
                  % (self.env.key_data_four, self.env.paes_key.hex()))

    def call(self, method: str, path: str, body: str = "{}",
             host: str = sw.HOST, raw_body: bool = False, verbose: bool = True,
             extra_headers: dict = None):
        """发送信封请求 → (http_status, business_or_None, err_or_None, raw_text)"""
        import urllib.request
        import urllib.error

        self._ensure_env()
        header_plain, extra = sw.build_header_for(
            self.identity, self.uid, self.token)
        h_env, h_meta = self.env.build_envelope(header_plain, "observed")
        b_env, b_meta = self.env.build_envelope(
            body, "insert", ts_ms=h_meta["ts"] + 1)

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": sw.UA_FOR.get(self.identity.platform, sw.UA_ANDROID),
            "appVersion": sw.APP_VERSION_FOR.get(self.identity.platform, sw.ANDROID_APP_VERSION),
            "headerSign": h_env,
        }
        headers.update(extra)
        if extra_headers:
            headers.update(extra_headers)

        url = host + path
        data = b_env.encode("utf-8") if method.upper() != "GET" else None
        req = urllib.request.Request(url, data=data, method=method.upper(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                status, raw = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return 0, None, "网络错误: %s" % e, ""

        if verbose:
            print("[http] %s %s -> %d (%d 字节)" % (method.upper(), path, status, len(raw)))

        paes = sw.derive_paes_key(*b_meta["key_data"])
        biz, err = sw.decrypt_response(raw, paes)
        if raw_body and biz is None:
            pass
        return status, biz, err, raw


# ══════════════════════════════════════════════════════════════════
# 子命令
# ══════════════════════════════════════════════════════════════════
def cmd_whoami(args):
    c = Client()
    print("device_id        = %s" % c.identity.device_id)
    print("app_install_time = %d" % c.identity.app_install_time)
    print("device_name      = %s" % c.identity.device_name)
    print("os_version       = %s" % c.identity.os_version)
    print("city             = %s" % c.identity.city)
    print("档案别名         = %s" % (get_active_name() or "（未使用档案库）"))
    # 当前账号的绑定设备
    uname = c.session.get("username", "")
    if uname:
        b_alias = get_bind_alias(uname)
        print("账号绑定         = %s -> %s" % (uname, b_alias or "（未绑定）"))
    print("-" * 50)
    if c.uid and c.token:
        print("uid   = %d" % c.uid)
        print("token = %s" % c.token)
        print("name  = %s" % c.session.get("name", ""))
        print("unid  = %s" % c.session.get("unid", ""))
        print("状态  : 已登录")
    else:
        print("状态  : 未登录（请先执行 login，或手动写入 session.json）")
    return 0


def server_logout(verbose: bool = True) -> dict:
    """真·服务端登出 + 清空本地会话。

    1. 若已登录（uid/token 在），先调服务端登出接口：
       新版 POST /api/v61300/user/userLogout（App 7.3.70 用）
       失败回退 POST /api/v6/user/logout（Neko 参考实测接口）
       body 用 '{}'（Neko 参考实现如此），信封加密自动带 token。
    2. 无论服务端成功/失败，最后都清空本地 session.json，
       保证本地永不残留有效 token。
    返回 {"ok": bool, "server": bool|None, "msg": str}
    """
    c = Client()
    sess = c.session
    logged = bool(c.uid and c.token)
    server_ok = None
    msgs = []

    if logged:
        for path, tag in (("/api/v61300/user/userLogout", "新版"),
                          ("/api/v6/user/logout", "旧版")):
            try:
                status, biz, err, raw = c.call("POST", path, "{}", verbose=False)
                if biz is not None:
                    err_code = biz.get("error")
                    ok = (status == 200 and err_code in (10000, None))
                    server_ok = ok if server_ok is None else (server_ok or ok)
                    msgs.append("%s %s: HTTP %d error=%s" % (tag, path, status, err_code))
                    if ok:
                        break
                else:
                    msgs.append("%s %s: %s" % (tag, path, str(err)[:100]))
            except Exception as e:
                msgs.append("%s %s: 异常 %s" % (tag, path, str(e)[:100]))
    else:
        msgs.append("未登录，跳过服务端注销")

    # 无论服务端如何，清空本地会话
    save_session({})
    msgs.append("本地会话已清空 (session.json={})")
    if verbose:
        print("[logout] " + " | ".join(msgs))
    ok = server_ok is not False  # 未登录/空响应也视为完成
    return {"ok": ok, "server": server_ok, "msg": " | ".join(msgs)}


def cmd_logout(args):
    """登出：调服务端注销接口 + 清空本地会话"""
    res = server_logout(verbose=True)
    print("退出完成：" + res["msg"])
    return 0 if res["ok"] else 1


def cmd_call(args):
    c = Client()
    body = args.body
    if os.path.exists(body):
        body = open(body, encoding="utf-8").read()
    status, biz, err, raw = c.call(args.method, args.path, body,
                                   host=(args.host or sw.HOST))
    if biz is None:
        print("[ERR] %s" % err)
        print("[RAW] %s" % raw[:600])
        return 2
    print(json.dumps(biz, ensure_ascii=False, indent=2))
    return 0


# ══════════════════════════════════════════════════════════════════
# 域名（抓包实测：App 按业务拆分到 4 个 host）
# ══════════════════════════════════════════════════════════════════
HOST_RUN = "https://run.gxapp.iydsj.com"
HOST_DISCOVERY = "https://discovery.gxapp.iydsj.com"
HOST_CONTROL = "https://control.gxapp.iydsj.com"
HOST_PARTNER = "https://partner.iydsj.com"

# 已实测确认的方法（大小写敏感，错方法会返回 "Request method 'X' not supported"）
PROBE_TARGETS = [
    # --- run 域：跑步主链路 ---
    ("GET",  HOST_RUN, "/api/v3/getserver/gmttime", ""),
    ("GET",  HOST_RUN, "/api/v70100/run/getHistoryConfig", ""),
    ("GET",  HOST_RUN, "/api/v70100/run/data/index?type=1", ""),
    ("GET",  HOST_RUN, "/api/v70300/user/info", ""),
    ("POST", HOST_RUN, "/api/v1/getGeoFenceForRun", "{}"),
    ("POST", HOST_RUN, "/api/v41/running/getPersonalSemesterInfo", "{}"),
    ("GET",  HOST_RUN, "/api/v592/home/remind", ""),
    ("GET",  HOST_RUN, "/api/v592/sport/ai/aiTask", ""),
    ("GET",  HOST_RUN, "/api/v70101/physicaltest/physicalResultInform", ""),
    ("GET",  HOST_RUN, "/api/alipay/v70260/getIdentityInfo", ""),
    # --- discovery 域 ---
    ("POST", HOST_DISCOVERY, "/api/v70100/run/calendar/list", "{}"),
    ("GET",  HOST_DISCOVERY, "/api/v70250/home/config", ""),
]


def cmd_probe(args):
    """探测常用跑步接口"""
    c = Client()
    targets = PROBE_TARGETS
    results = []
    for m, host, p, b in targets:
        print("=" * 66)
        status, biz, err, raw = c.call(m, p, b, host=host)
        if biz is None:
            print("[ERR] %s" % err)
            print("[RAW] %s" % raw[:300])
            results.append((p, False))
        else:
            s = json.dumps(biz, ensure_ascii=False)
            print(s[:600])
            errc = biz.get("error") if isinstance(biz, dict) else None
            results.append((p, errc == 10000))
        time.sleep(1.0)
    print("=" * 66)
    print("汇总:")
    for p, ok in results:
        print("  %s %s" % ("OK " if ok else "-- ", p))
    return 0


def cmd_login(args):
    """调用 auto_login.py 的完整登录链（含 GT4 滑块破解），成功后写入 session.json"""
    if not args.username or not args.password:
        print("用法: python swcli.py login <手机号> <密码>")
        return 1

    # 动态加载 auto_login.py（只取函数区，不执行其 __main__）
    src = open(os.path.join(HERE, "auto_login.py"), encoding="utf-8").read()
    head = src.split("# ============================================================\n# MAIN")[0]
    ns = {"__name__": "autologin"}
    exec(compile(head, "auto_login", "exec"), ns)

    print("开始登录: %s" % args.username)
    ident = ensure_account_device(args.username)  # 首登即绑定一台设备，长期固定
    result = ns["login"](args.username, args.password, identity=ident)
    if not result:
        print("[FAIL] 登录失败")
        return 2

    sess = load_session()
    sess.update({"uid": result["uid"], "token": result["token"],
                 "unid": result.get("unid", ""), "name": result.get("name", ""),
                 "username": args.username})
    save_session(sess)
    # 登录成功后把 uid 记进绑定记录（该账号后续提交仍用同一设备）
    try:
        binds = load_binds()
        uname = str(args.username or "")
        if uname in binds:
            binds[uname]["uid"] = int(result.get("uid", 0) or 0)
            save_binds(binds)
    except Exception:
        pass
    print("[OK] 登录成功，会话已写入 session.json")
    print("     uid=%s token=%s" % (result["uid"], result["token"]))
    return 0


def cmd_runwatch(args):
    """检查当前跑步数据概况（只读）"""
    c = Client()
    print("--- 用户信息 ---")
    _, biz, err, _ = c.call("POST", "/api/v70300/user/info", "{}", verbose=False)
    if biz and isinstance(biz, dict):
        d = biz.get("data") or {}
        print("  uid=%s name=%s unid=%s campus=%s"
              % (d.get("uid"), d.get("name"), d.get("unid"), d.get("campusName")))
    else:
        print("  ERR %s" % err)

    print("--- 跑步统计 ---")
    _, biz, err, _ = c.call("GET", "/api/v70100/run/data/index?type=1", "", verbose=False)
    if biz and isinstance(biz, dict):
        d = biz.get("data") or {}
        print("  已跑里程=%s m  周里程=%s m  最长=%s m  最长时长=%ss  最佳配速=%s"
              % (d.get("runCountLength"), d.get("weekRunLength"),
                 d.get("longestDistance"), d.get("longestTime"), d.get("bestSpeed")))
    else:
        print("  ERR %s" % err)

    print("--- 校园跑规则 ---")
    _, biz, err, _ = c.call("GET", "/api/v70100/run/getHistoryConfig", "", verbose=False)
    if biz and isinstance(biz, dict):
        for item in (biz.get("data") or []):
            cfg = item.get("campusConfigModel") or {}
            print("  unid=%s mode=%s 单次下限=%sm 每日上限=%sm 时段=%s"
                  % (cfg.get("unid"), cfg.get("mode"), cfg.get("maleMinSingleDis"),
                     cfg.get("maleDayUpper"),
                     [("%s~%s" % (t.get("startTime"), t.get("endTime")))
                      for t in (cfg.get("randomValidTimes") or [])]))
    else:
        print("  ERR %s" % err)
    return 0


def cmd_policy(args):
    """拉取跑步策略（提交 submit 所需的 policy / timestamp / minDistance）"""
    c = Client()
    unid = args.unid or int(c.session.get("unid", 0) or 0)
    body = json.dumps({"runMode": 1, "ruleUpdateTime": 0,
                       "geoFenceUpdateTime": 0, "selectUnid": unid,
                       "operateType": 0}, separators=(",", ":"))
    _, biz, err, _ = c.call("POST", "/api/v70103/runModePolicy", body)
    if biz is None:
        print("[ERR] %s" % err)
        return 2
    d = biz.get("data") or {}
    rule = d.get("runRuleModel") or {}
    print(json.dumps(biz, ensure_ascii=False, indent=2))
    print("-" * 50)
    print("policy      = %s" % d.get("policy"))
    print("timestamp   = %s   (runes 头用)" % d.get("timestamp"))
    print("minDistance = %s   (selDistance 用)" % rule.get("minDistance"))
    print("faceVerify  = %s" % rule.get("faceVerify"))
    print("速度区间    = %s ~ %s m/s" % (rule.get("speedTop"), rule.get("speedBottom") if "speedBottom" in rule else rule.get("speedTop")))
    print("时长下限    = %s s" % rule.get("minRunTime"))
    return 0


def cmd_submit(args):
    """提交跑步记录（轨迹 JSON → /api/v70260/runnings/save/record → OBS）

    支持双模式：
      --mode free   自由跑：校园范围内环形轨迹，无需打卡点
      --mode score  计分跑：必须经过服务端打卡点（自动拉点 + 可达性校验）
    若给了 --mode 且未给 track，则自动生成轨迹。
    """
    import swsubmit

    mode = (getattr(args, "mode", None) or "").lower()
    track_path = args.track
    prep = {}   # 自动生成轨迹时由 swmode.prepare 填充（含打卡点/警示）

    c = Client()
    if not (c.uid and c.token):
        print("[ERR] 未登录：session.json 缺 uid/token")
        return 2
    unid = args.unid or int(c.session.get("unid", 0) or 0)

    # 0) 双模式自动生成轨迹
    # ★ 记录 address 的来源：`--address` > **校区城市** > 设备档案 city。
    #   ⚠ 2026-09-28 定因：旧实现只取 `c.identity.city`，而发布包为隐私脱敏把它
    #     统一写成「大连市」（_gh_tools/clean_release.py）⇒ 不管在哪跑都显示大连市。
    #     地址该由校区决定（真机 App 也是城市级反地理编码）。
    #   ⚠ 2026-09-29 补漏：修复只写在「swcli 自己解析校区」那条分支里，而
    #     GUI(`gui.py`) / 批量(`run_all.py`) 都显式传 `--campus-lat/--campus-lon`
    #     ⇒ 走的是另一条分支 ⇒ 地址照样退回「大连市」（A/B 实证：不传坐标得
    #     揭阳市、传坐标得空）。现收敛到 `campus.resolve_address()` 单一实现。
    #   ↓ 这里只是「手动轨迹 / 非 free|score」路径的初值，走模式生成时会被上面的
    #     单一实现覆盖。
    record_address = str(getattr(args, "address", None)
                         or getattr(c.identity, "city", "") or "")
    if mode in ("free", "score") and not track_path:
        import swmode
        import campus
        idn = c.identity.to_dict() if hasattr(c.identity, "to_dict") else {}
        # 校区中心：优先命令行 > 本地覆盖/服务端围栏/内置库；无坐标不再回退默认
        if args.campus_lat is not None and args.campus_lon is not None:
            clat, clon = float(args.campus_lat), float(args.campus_lon)
            # ★★ 2026-09-29 修：这条分支也必须给出**校区城市**。
            #   GUI（`gui.py`）与批量（`run_all.py`）都是**显式传坐标**的 ——
            #   原先这里把 city 置空 ⇒ `record_address` 退回设备档案 city，
            #   而发布包为脱敏把它统一写成「大连市」⇒ 记录地址恒为大连市。
            #   （A/B 实证：不传坐标 → 揭阳市 ✅；显式传坐标 → 空 ❌）
            #   ⇒ 用 unid 反查校区城市，让「地址由校区决定」在**所有**入口成立。
            camp = {"name": "命令行指定", "lat": clat, "lon": clon,
                    "unid": unid, "city": campus.known_city(unid)}
        else:
            camp = campus.pick_campus(c, unid)
            if camp.get("lat") is None or camp.get("lon") is None:
                print("[ERR] 校区坐标未收录（%s）：请在 campus.json 手动校准，"
                      "或传 --campus-lat --campus-lon"
                      % camp.get("name", "?"))
                return 2
            clat, clon = camp["lat"], camp["lon"]
            unid = int(camp.get("unid") or unid or 0)
        # 地址裁决**只有一处**（`campus.resolve_address`）—— 见该函数的注释：
        # 优先级 `--address` > 校区城市 > 设备档案 city。别再在这里就地判断。
        record_address, _addr_src = campus.resolve_address(
            getattr(c.identity, "city", "") or "", unid,
            explicit=getattr(args, "address", None),
            campus_city=camp.get("city"))
        print("  [地址] address=%s（来源：%s）" % (record_address or "<空>", _addr_src))
        try:
            prep = swmode.prepare(c, mode, args.dist, campus_lat=clat,
                                  campus_lon=clon, unid=unid,
                                  start=args.start, pace=args.pace,
                                  shape=args.shape,
                                  straight_m=getattr(args, "track_straight", 0.0),
                                  force_points=args.force_points)
        except Exception as e:
            print("[ERR] 轨迹准备失败: %s" % e)
            return 2
        if prep.get("warn"):
            print("[warn] %s" % prep["warn"])
            if not args.force:
                print("[stop] 已阻止提交。确认要跑请加 --force（或改用 --mode free）")
                return 5
        track_path = prep["track"]

    if not track_path:
        print("用法: python swcli.py submit --mode free|score [--dist 2.2]")
        print("      python swcli.py submit <轨迹JSON> [--dry-run]")
        return 1

    # 1) 拉策略
    print("--- 拉取跑步策略 runModePolicy ---")
    # runMode 语义（实测 unid=3305）：1 → policy=1（计分/校园跑）；其余值 → policy=0
    run_mode = getattr(args, "run_mode", None)
    if run_mode is None:
        run_mode = 1
    pbody = json.dumps({"runMode": run_mode, "ruleUpdateTime": 0,
                        "geoFenceUpdateTime": 0, "selectUnid": unid,
                        "operateType": 0}, separators=(",", ":"))
    _, pbiz, perr, _ = c.call("POST", "/api/v70103/runModePolicy", pbody,
                              verbose=False)
    if pbiz is None:
        print("[ERR] 策略获取失败: %s" % perr)
        return 2
    pd = pbiz.get("data") or {}
    rule = pd.get("runRuleModel") or {}
    policy = int(pd.get("policy") or 0)
    # ★★★ 计分跑默认 policy=0（2026-09-28 真机定因）★★★
    #   详情页「✓ 打卡点」画不画，只取决于**记录自身**的 `policy`（服务端原样存下
    #   提交体里这个字段）：
    #     policy=1 → 详情页【不画】✓ 打卡点
    #     policy=0 → 详情页【画】✓ 打卡点（绿底 ✓；必经点画成橙黄）
    #   真机 A/B（同校区 unid=3305）：
    #     · rrid 1327337940（--policy 0）→ 5 个 ✓ 全出现 ✅
    #     · rrid 1327194325（policy=1）  → 0 个 ✓
    #   且 `_gh_tools/fixtures/obs_probe_*.json` 里真机自己写的计分跑记录也是
    #   policy=0（1323338948 policy=0 画 148 个 ✓；1323338294 policy=1 不画）。
    #   ⚠ `runModePolicy(runMode=1)` 返回的 policy=1 是**开跑前界面**的口径，
    #     不是记录侧该落的值 —— 两者语义不同，别直接照抄。
    if mode == "score" and getattr(args, "policy", None) is None:
        policy = 0
    # 允许命令行覆盖 policy（用于判定跑法判别字段）
    if getattr(args, "policy", None) is not None:
        policy = int(args.policy)
    policy_ts = int(pd.get("timestamp") or 0)
    min_distance = args.min_distance or int(rule.get("minDistance") or 2000)
    face_check = 1 if rule.get("faceVerify") else 0
    print("  runMode=%s policy=%d policy_ts=%d minDistance=%d faceCheck=%d"
          % (run_mode, policy, policy_ts, min_distance, face_check))

    # 2) 组装提交体
    print("--- 组装提交体 ---")
    track = json.load(open(track_path, encoding="utf-8"))
    # 时间护栏：startTime 距今不得为负（未来时间），顺延到过去
    first_ms = int((track.get("points") or [{}])[0].get("ts") or 0)
    now_ms = sw.now_ms()
    if first_ms and first_ms > now_ms - 60000:
        shift = first_ms - (now_ms - 60000)
        for p in track.get("points") or []:
            p["ts"] = int(p["ts"]) - shift
        print("  [护栏] 起点时间在未来/过近，整体前移 %.0f 秒" % (shift / 1000.0))

    # ★ 模式 → sportType（2026-09-17 真机实测确定，务必不要再猜）：
    #     1 = 计分跑（校园跑，需点位核验、计入成绩）
    #     4 = 自由跑（无需任何点位、不计入学期成绩，freedomRunCalculate=false）
    #     6 = 户外跑（不计入成绩）
    #   其余取值服务端一律拒绝：7+/9+ → 11517「跑步模式不对」；5 → 10106「uid不存在」。
    #   实测证据：sportType=4 的记录在 App 运动记录列表显示「自由跑」；
    #             =6 显示「户外跑」；=1 显示「计分跑」。
    is_score = (mode == "score")
    sport_type = 1 if is_score else 4
    # 允许命令行覆盖 sportType（用于判定「自由跑」对应的真实取值）
    if getattr(args, "sport_type", None) is not None:
        sport_type = int(args.sport_type)
    # 五点（真实打卡点）：仅计分跑传 fivePointJson；自由跑无围栏无打卡点 → 不传
    # ★★ 围栏来源（2026-09-28 真机定因，务必别再走回头路）：
    #   详情页「围栏范围」画的是**记录自身的 geoFencesJson**，而 App 自己那份围栏
    #   来自 `runModePolicy(runMode=1)` 的 `data.geoFence`（点键名 `lat/lng/glat/glng`）。
    #   旧代码走 `getGeoFenceForRun` —— 实测该接口在本校区**返回空** ⇒ 记录里恒为
    #   `"[]"` ⇒ 详情页没有围栏可画（此前只能靠手改 OBS 验证）。
    #   ⇒ 这里优先取 runModePolicy 里那份（反正已经拉了），空才退回 getGeoFenceForRun。
    #   拉取失败只警告，不阻塞提交。单一实现见 swsubmit.norm_geo_fences。
    geo_fences = []
    if is_score:
        geo_fences = swsubmit.geo_fences_from_policy(pd)
        if geo_fences:
            print("  [围栏] runModePolicy 下发 %d 个围栏 / %d 个顶点"
                  " → geoFencesJson 已填充"
                  % (len(geo_fences),
                     sum(len(g["points"]) for g in geo_fences)))
        else:
            print("  [围栏] runModePolicy 未带围栏 → 退回 getGeoFenceForRun")
            geo_fences = swsubmit.fetch_geo_fences(c.call)
    five_point_json = ""
    if is_score and prep.get("points"):
        try:
            first_ts = int((track.get("points") or [{}])[0].get("ts") or 0)
            five_point_json = swsubmit.five_point_wrapper(prep["points"],
                                                          first_ts,
                                                          geo_fences)
            print("  [五点] 计分跑携带 %d 个真实打卡点" % len(prep["points"]))
        except Exception as e:
            print("  [warn] 五点组装失败（不阻塞提交）: %s" % e)
    body, meta = swsubmit.build_record_body(
        track, uid=c.uid, unid=unid, policy=policy, policy_ts=policy_ts,
        min_distance=min_distance, weight=args.weight,
        face_check=face_check, address=record_address,
        sport_type=sport_type, five_point_json=five_point_json,
        with_steps=True)
    print("  mode=%s sportType=%d uuid=%s 距离=%.0fm 时长=%ds 步数=%d"
          % (mode or "manual", body["sportType"], meta["uuid"],
             meta["total_dis"], meta["total_time"], meta["total_steps"]))
    print("  signature=%s" % body["signature"])

    if args.dry_run:
        print("[dry-run] 不发送。body %d 字节" % len(json.dumps(body)))
        return 0

    # 3) 发送（带 runes / runef 特殊头）
    runes = "%d%d" % (policy_ts, c.uid)
    runef = "%s%d" % (meta["uuid"], meta["start_ms"])
    print("--- 提交 ---")
    print("  runes=%s" % runes)
    print("  runef=%s" % runef)
    status, biz, err, raw = c.call(
        "POST", swsubmit.RECORD_PATH,
        json.dumps(body, separators=(",", ":")),
        extra_headers={"runes": runes, "runef": runef})
    if biz is None:
        print("[ERR] %s" % err)
        print("[RAW] %s" % raw[:800])
        return 2
    print(json.dumps(biz, ensure_ascii=False, indent=2))
    rrid = biz.get("data", {}).get("rrid") if isinstance(biz.get("data"), dict) else None
    if not rrid:
        print("[?] 响应无 rrid，请核对 error 字段")
        return 3
    print("[OK] 提交成功 rrid=%s" % rrid)

    if args.no_obs:
        print("[skip] 已指定 --no-obs，不上传轨迹（记录会显示默认位置）")
        return 0

    # 4) OBS 轨迹上传（关键！否则服务端拿不到 GPS 轨迹）
    import swobs
    print("--- OBS 轨迹上传 ---")
    # ★ 用完整轨迹点（含 lat/lon/ts/dist），并把 10 秒窗 id 与 rrid 对齐
    pts = meta["points"]
    # ★★ 五点（fixed_point_json）只取【学校下发的打卡点】，绝不用轨迹点：
    #    自由跑无围栏无打卡点 → []（fivePointJson="[]"）；计分跑 → 学校点位。
    obs_cps = (prep.get("points") or []) if is_score else []
    print("  [五点] %s → %d 个打卡点"
          % ("计分跑" if is_score else "自由跑（无点位）", len(obs_cps)))
    try:
        obs_ok, keys = swobs.upload_track(
            c.call, pts, rrid=int(rrid), uuid=meta["uuid"], uid=c.uid,
            start_ms=meta["start_ms"], total_time=meta["total_time"],
            with_steps=True, fixed_points=obs_cps, geo_fences=geo_fences)
    except Exception as e:
        print("[ERR] OBS 上传异常: %s" % e)
        return 4
    if obs_ok == 2:
        print("[OK] OBS 双 key 上传成功")
        print("     · %s" % keys[0])
        print("     · %s" % keys[1])
    else:
        print("[warn] OBS 上传成功 %d/2" % obs_ok)
        return 4

    # 5) 回读校验（OBS GET，确认对象真的存在、10 键齐全、轨迹可解压）
    if not args.no_verify:
        print("--- OBS 回读校验 ---")
        try:
            got = swobs.read_back(c.call, keys[0])
            if not got:
                print("  [warn] 回读为空，跳过")
            else:
                obj = json.loads(got.decode("utf-8"))
                n = swobs.decode_point_count(obj)
                print("  [OK] 回读 %d 字节，对象 %d 键，轨迹点 %d 个"
                      % (len(got), len(obj), n))
                print("      rrid=%s  uuid=%s" % (obj.get("rrid"), obj.get("uuid")))
        except Exception as e:
            print("  [warn] 回读校验异常: %s" % e)
    return 0


# ══════════════════════════════════════════════════════════════════
def build_parser():
    p = argparse.ArgumentParser(
        prog="swcli", description="运动世界校园 PC 端命令行工具")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("whoami", help="显示设备身份与登录态")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("devices", help="设备档案：列出/切换/导入/绑定/删除")
    sp.add_argument("devices_cmd", nargs="?", default="list",
                    choices=["list", "use", "import", "import-json",
                             "import-guise", "bind", "unbind", "del"],
                    help="list(默认) / use <别名> / import <别名> / "
                         "import-json <file.json> / import-guise <模板> / "
                         "bind <账号> / unbind <账号> / del <别名>")
    sp.add_argument("name", nargs="?", default=None, help="设备别名 / 文件路径 / 账号")
    sp.add_argument("--note", default=None, help="备注（import 时）")
    sp.add_argument("--prefix", default=None,
                    help="导入前缀（import-json 时给每套档案加别名前缀）")
    sp.add_argument("--force", action="store_true", help="覆盖同名")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("use", help="快捷切换设备：python swcli.py use <别名>")
    sp.add_argument("name", help="设备别名")
    sp.set_defaults(func=lambda a: cmd_devices(
        type("X", (), {"devices_cmd": "use", "name": a.name})()))

    sp = sub.add_parser("logout", help="登出：调服务端注销接口 + 清空本地会话")
    sp.set_defaults(func=cmd_logout)

    sp = sub.add_parser("call", help="调用接口")
    sp.add_argument("method", help="GET / POST")
    sp.add_argument("path", help="接口路径，如 /api/v70100/run/getHistoryConfig")
    sp.add_argument("body", nargs="?", default="{}", help="业务 JSON 或文件路径")
    sp.add_argument("--host", default=None, help="覆盖域名")
    sp.set_defaults(func=cmd_call)

    sp = sub.add_parser("probe", help="探测常用跑步接口")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("login", help="登录（走 GT4 滑块链）")
    sp.add_argument("username", nargs="?", default=None, help="手机号")
    sp.add_argument("password", nargs="?", default=None, help="密码")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("runwatch", help="查看跑步数据概况（只读）")
    sp.set_defaults(func=cmd_runwatch)

    sp = sub.add_parser("policy", help="拉取跑步策略（policy/timestamp/minDistance）")
    sp.add_argument("--unid", type=int, default=0, help="校区 unid，默认取会话")
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("submit", help="提交跑步记录（支持 free/score 双模式）")
    sp.add_argument("track", nargs="?", default=None,
                    help="轨迹 JSON 路径；给了 --mode 时可省略（自动生成）")
    sp.add_argument("--mode", choices=["free", "score"], default=None,
                    help="free=自由跑(校园范围内,无需打卡点) / score=计分跑(必须过打卡点)")
    sp.add_argument("--dist", type=float, default=2.2, help="目标距离 km（--mode 时用）")
    sp.add_argument("--shape", choices=["ellipse", "track"], default="track",
                    help="轨迹形状：track=标准跑道（默认，两个半圆 + 两条直道）；"
                         "ellipse=椭圆")
    sp.add_argument("--track-straight", type=float, default=0.0,
                    help="跑道形状的直道长度（米）；0=按打卡点自适应")
    sp.add_argument("--campus-lat", type=float, default=None, help="校区中心纬度")
    sp.add_argument("--campus-lon", type=float, default=None, help="校区中心经度")
    sp.add_argument("--address", default=None,
                    help="记录 address（城市名，如 揭阳市）；默认取校区城市，"
                         "再退设备档案 city")
    sp.add_argument("--start", default=None, help="开始时间 'YYYY-MM-DD HH:MM:SS'")
    sp.add_argument("--pace", default="5:40", help="目标配速，如 5:40")
    sp.add_argument("--force-points", action="store_true",
                    help="忽略打卡点缓存，强制重新拉取")
    sp.add_argument("--force", action="store_true",
                    help="打卡点不可达时仍然提交（默认阻止）")
    sp.add_argument("--unid", type=int, default=0, help="校区 unid，默认取会话")
    sp.add_argument("--min-distance", type=int, default=0, help="selDistance 覆盖")
    sp.add_argument("--weight", type=float, default=65.0, help="体重 kg")
    sp.add_argument("--dry-run", action="store_true", help="只组装不发送")
    sp.add_argument("--no-obs", action="store_true",
                    help="跳过 OBS 轨迹上传（记录会显示默认位置）")
    sp.add_argument("--no-verify", action="store_true", help="跳过 OBS 回读校验")
    sp.add_argument("--sport-type", type=int, default=None,
                    help="覆盖 sportType（默认 free=1 / score=5）")
    sp.add_argument("--policy", type=int, default=None,
                    help="覆盖提交体 policy（计分跑默认 0=详情页画 ✓ 打卡点；"
                         "1=不画）")
    sp.add_argument("--run-mode", type=int, default=None,
                    help="runModePolicy 的 runMode（1=计分/校园跑；0=自由跑口径）")
    sp.set_defaults(func=cmd_submit)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

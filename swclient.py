#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swclient.py — 运动世界校园 请求客户端（Python 复刻）

来源: NekoSportsWorldTool (Rust, iOS 7.3.40 链路逆向) + 20260916 Android 抓包实测

═══════════════════════════════════════════════════════════════════
加密体系（三层，全部已验证）
═══════════════════════════════════════════════════════════════════
1) 请求信封  {d, h, k, p, t}
     container(inner) = compact_json{
         data: base64(业务明文),
         timeStamp: ms,
         platform: 1,
         keyDataOne/Two/Three/Four
     }
     reqKey    = 16 字节随机（SALT_ALPHABET 抽）
     d         = base64( AES-128-CBC/PKCS7/IV=0(reqKey, container) )
     h         = MD5(container)                     ← ★ 本次会话踩坑点：h 算的是明文
     k         = base64( RSA-1024 PKCS1v15(reqKey) )
     p/t       = 101 / 0
     键序: headerSign 用 [k,p,d,h,t]; body 用 [d,h,k,p,t]

2) 请求头签名
     headerSign = 信封( header明文, observed序 )
     tokenSign  = MD5("timeStamp={ts}&token={token}&uid={uid}"+SALT)
                  ← 已用 20260916 抓包 5/5 实测验证

3) 响应  {r, s, v}
     s → RSA-1024 公钥 raw 运算 → 恢复 00 01 FF..FF 00 <32B 小写hex>
       → 该 hex 应等于 MD5(第一层明文)      ← 验签
     r → AES-128-CBC(IV=0, pAesKey) → 内层信封 JSON
         → 若含 d 再解一层
         → data 字段 base64 解码 → 业务 JSON
     pAesKey = derive_paes_key(keyData1..4)   (fold32 + MBA 混合, LE 16B)

═══════════════════════════════════════════════════════════════════
关键常量
═══════════════════════════════════════════════════════════════════
  keyDataOne   = "nhang.school"        (<- "com.wanhang.school" 末12)
  keyDataTwo   = "5K0E8400-E29"        (<- "5K0E8400-E29B-11D4-A716-4G6RW65G544F" 前12)
  keyDataThree = "597DEA1AFB49"        (<- "DE6AED50-2B3A-5327-ACED-597DEA1AFB49" 末12)
  keyDataFour  = format("%f", first_ms)[-12:]  会话内复用
  SALT         = "2slhe02lsfiwowlcixisla_sls-_slaor"
  RSA 公钥     = 见 RSA_PUB_PEM（已用 72/72 抓包响应 s 验签确认）
"""
import base64
import hashlib
import json
import random
import struct
import time
import uuid

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

# ══════════════════════════════════════════════════════════════════
HOST = "https://run.gxapp.iydsj.com"
DISCOVERY = "https://discovery.gxapp.iydsj.com"

KEYDATA_ONE = "nhang.school"
KEYDATA_TWO = "5K0E8400-E29"
KEYDATA_THREE = "597DEA1AFB49"

SALT = "2slhe02lsfiwowlcixisla_sls-_slaor"
SALT_ALPHABET = "+kot8A*B45jF6CD@a!UVWubcdKLZ{efgMpNOxyz01PQ}Rn)Tvw23XYh(iG7rsEqJHI9+Slm/"

RSA_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC5pqlTzsGNZk1RxhH4O4x3JNTD
V7FbVH66mPfW5v1tnIy4ty7xv8DGMG4Zn/TvstwlJWeYOADHdi8uF21lJaBvzvPt
VEhifHXZq825fI9hGYtDoaVQmCN/Nfs2dKmt89XDrhtl3SZxO6TumOCTQt+5oqjF
2Jo3o1YtkAyGzjaJnwIDAQAB
-----END PUBLIC KEY-----"""

ANDROID_APP_VERSION = "7.3.70"
IOS_APP_VERSION = "7.3.40"
UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; 22081212C Build/UKQ1.231003.002) "
              "AppleWebKit/537.36 SWCampus/7.3.70")
UA_IOS = "SWCampus/7.3.40 (iPhone; iOS 18.1; Scale/3.00)"
UA_FOR = {"android": UA_ANDROID, "ios": UA_IOS}
APP_VERSION_FOR = {"android": ANDROID_APP_VERSION, "ios": IOS_APP_VERSION}

_RSA_KEY = RSA.import_key(RSA_PUB_PEM)


# ══════════════════════════════════════════════════════════════════
# 1. fold32 + derive_paes_key
# ══════════════════════════════════════════════════════════════════
def fold32(value: str) -> int:
    h = 0
    for c in value.encode("utf-8"):
        h = ((((h & 0x00FFFFFF) << 8) | c) ^ (h >> 24)) & 0xFFFFFFFF
    return h


def _ror32(v: int, n: int) -> int:
    return ((v >> n) | (v << (32 - n))) & 0xFFFFFFFF


def derive_paes_key(one: str, two: str, three: str, four: str) -> bytes:
    a, b, c, d = fold32(one), fold32(two), fold32(three), fold32(four)
    w9 = c ^ a
    w10 = w9 ^ _ror32(w9, 24)
    w9 = w10 ^ _ror32(w9, 8)
    w10 = w9 ^ b
    w9 ^= d
    w8 = d ^ b
    w11 = w8 ^ _ror32(w8, 24)
    w8 = w11 ^ _ror32(w8, 8)
    w11 = w8 ^ a
    w8 ^= c
    w12 = w8 & w10
    w11 ^= w12
    w12 = w8 | w9
    w8 ^= w9
    w10 ^= w12
    w8 = w10 ^ ((~w8) & 0xFFFFFFFF)
    w12 = w8 ^ w11
    w8 |= w11
    w8 ^= w10
    w10 = w12 & ((~w10) & 0xFFFFFFFF)
    w9 ^= w10
    return struct.pack("<IIII", w9, w8, w12, w11)


# ══════════════════════════════════════════════════════════════════
# 2. AES-128-CBC / PKCS7 / IV = 0
# ══════════════════════════════════════════════════════════════════
def aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_CBC, b"\x00" * 16).encrypt(pad(plaintext, 16))


def aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    return unpad(AES.new(key, AES.MODE_CBC, b"\x00" * 16).decrypt(ciphertext), 16)


# ══════════════════════════════════════════════════════════════════
# 3. 工具
# ══════════════════════════════════════════════════════════════════
def md5_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s)


def now_ms() -> int:
    return int(time.time() * 1000)


def normalize_key_data(value: str) -> str:
    """≤7B 补随机字母数字到 12；≥13B 取 UTF-8 末 12；8-12 原样。"""
    b = value.encode("utf-8")
    if len(b) <= 7:
        missing = 12 - len(b)
        return value + "".join(random.choices(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
            k=missing))
    if len(b) >= 13:
        return b[-12:].decode("utf-8", "replace")
    return value


def random_req_key() -> str:
    return "".join(random.choice(SALT_ALPHABET) for _ in range(16))


def native_token_sign(uid: int, token: str, ts: int) -> str:
    """tokenSign = MD5('timeStamp={ts}&token={token}&uid={uid}' + SALT)"""
    q = "timeStamp={}&token={}&uid={}".format(ts, token, uid)
    return md5_hex(q + SALT)


# ══════════════════════════════════════════════════════════════════
# 4. 会话（keyDataFour 会话内复用）
# ══════════════════════════════════════════════════════════════════
class EnvelopeSession:
    def __init__(self, first_ms: int | None = None):
        if first_ms is None:
            first_ms = now_ms()
        self.first_ms = first_ms
        # Rust: format!("{:.6}", ms as f64) —— 6 位小数（注意不是 6 位有效数字）
        self.key_data_four = normalize_key_data("{:.6f}".format(float(first_ms)))
        self.key_data = [KEYDATA_ONE, KEYDATA_TWO, KEYDATA_THREE, self.key_data_four]
        self.paes_key = derive_paes_key(*self.key_data)

    def build_envelope(self, plaintext: str, order: str = "insert", ts_ms: int | None = None):
        """构造信封 → (json_str, meta)  meta 含 key_data / req_key / ts / container"""
        ts = ts_ms if ts_ms is not None else now_ms()
        container_data = b64e(plaintext.encode("utf-8"))
        container = json.dumps({
            "data": container_data,
            "timeStamp": ts,
            "platform": 1,
            "keyDataOne": self.key_data[0],
            "keyDataTwo": self.key_data[1],
            "keyDataThree": self.key_data[2],
            "keyDataFour": self.key_data[3],
        }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        req_key = random_req_key()
        d = b64e(aes_encrypt(req_key.encode("utf-8"), container))
        h = md5_hex(container)                                  # ★ h = MD5(container)
        k = b64e(PKCS1_v1_5.new(_RSA_KEY).encrypt(req_key.encode("utf-8")))

        if order == "observed":
            js = '{"k":"%s","p":101,"d":"%s","h":"%s","t":0}' % (k, d, h)
        else:
            js = '{"d":"%s","h":"%s","k":"%s","p":101,"t":0}' % (d, h, k)
        meta = {"container": container, "req_key": req_key, "ts": ts,
                "key_data": list(self.key_data)}
        return js, meta


# ══════════════════════════════════════════════════════════════════
# 5. 请求头（Android）
# ══════════════════════════════════════════════════════════════════
class Identity:
    """设备身份。device_id / app_install_time 必须持久稳定（漂移会触发 10121 风控）。
    platform: "android" 或 "ios"，决定请求头/UA/安装时间惯例。

    ★ 2026-09-29：默认 city 由 "大连市" 改为 "揭阳市" —— 本工具面向的是
      **广东工业大学揭阳校区**（见 campus.py 的 KNOWN_CAMPUS），默认设备城市
      应与校区一致。此前硬编码 "大连市" 会让每台新生成的「默认」设备都带一个
      与用户毫无关系的城市（用户反馈「那个大连的默认设备」）。
      注意：城市不进发布包 —— pack_all.py 的 BAD_IDENTITY 会拦身份文件里的城市名。"""

    def __init__(self, device_id="", app_install_time=0, os_version="14",
                 device_name="22081212C", city="揭阳市", platform="android"):
        self.device_id = device_id or str(uuid.uuid4()).upper()
        # 安装时间惯例：android=90天前；ios=3天前
        self.app_install_time = app_install_time or (now_ms() - (
            90 if platform == "android" else 3) * 86_400_000)
        self.app_update_time = self.app_install_time
        self.os_version = os_version
        self.device_name = device_name
        self.city = city
        self.platform = platform if platform in ("android", "ios") else "android"

    def to_dict(self):
        return {"device_id": self.device_id, "app_install_time": self.app_install_time,
                "os_version": self.os_version, "device_name": self.device_name,
                "city": self.city, "platform": self.platform}

    @classmethod
    def from_dict(cls, d):
        return cls(device_id=d.get("device_id", ""),
                   app_install_time=d.get("app_install_time", 0),
                   os_version=d.get("os_version", "14"),
                   device_name=d.get("device_name", "22081212C"),
                   city=d.get("city", "揭阳市"),
                   platform=d.get("platform", "android"))


def build_android_header(identity: Identity, uid: int, token: str, ts_ms: int | None = None):
    """返回 (header 明文紧凑 JSON, 附加 HTTP 头 dict)"""
    ts = ts_ms if ts_ms is not None else now_ms()
    nonce = str(uuid.uuid4()).upper()
    install = identity.app_install_time
    m = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "appVersion": ANDROID_APP_VERSION,
        "osType": "0",
        "DeviceId": identity.device_id,
        "osVersion": identity.os_version,
        "deviceName": identity.device_name,
        "IMEI": "",
        "logicPixel": "1080x2400",
        "physicPixel": "1080x2400",
        "androidId": "",
        "blMac": "",
        "wifiMac": "",
        "cpuModel": "arm64-v8a",
        "isRoot": False,
        "appUpdateTime": install,
        "appInstallTime": install,
        "nonce": nonce,
        "timeStamp": ts,
        "CustomDeviceId": "{}_android_sportsWorld_campus".format(identity.device_id),
        "uid": uid,
        "token": token,
        "studentId": uid,
        "tokenSign": native_token_sign(uid, token, ts),
    }
    header_plain = json.dumps(m, separators=(",", ":"), ensure_ascii=False)
    extra = {"nonce": nonce, "timeStamp": str(ts),
             "tokenSign": native_token_sign(uid, token, ts)}
    return header_plain, extra


def build_ios_header(identity: Identity, uid: int, token: str, ts_ms: int | None = None):
    """iOS 请求头明文（osType="1"，SWRequestHeaderProvider._fillFullHeaders）。"""
    ts = ts_ms if ts_ms is not None else now_ms()
    nonce = str(uuid.uuid4()).upper()
    install = identity.app_install_time
    m = {
        "osType": "1",
        "DeviceId": identity.device_id,
        "deviceName": identity.device_name,
        "CustomDeviceId": "{}_iOS_sportsWorld_campus".format(identity.device_id),
        "osVersion": identity.os_version,
        "logicPixel": "360x640",
        "physicPixel": "1080x1920",
        "cpuModel": "x86_64",
        "appVersion": IOS_APP_VERSION,
        "isRoot": False,
        "appInstallTime": install,
        "nonce": nonce,
        "timeStamp": ts,
        "studentId": uid if uid >= 1 else 0,
    }
    if uid >= 1 and token:
        m["uid"] = uid
        m["token"] = token
        m["tokenSign"] = native_token_sign(uid, token, ts)
    header_plain = json.dumps(m, separators=(",", ":"), ensure_ascii=False)
    extra = {"nonce": nonce, "timeStamp": str(ts),
             "tokenSign": native_token_sign(uid, token, ts),
             "User-Agent": UA_IOS, "appVersion": IOS_APP_VERSION}
    return header_plain, extra


def build_header_for(identity: Identity, uid: int, token: str,
                     ts_ms: int | None = None):
    """按平台分发 header 构造器。"""
    if identity.platform == "ios":
        return build_ios_header(identity, uid, token, ts_ms)
    return build_android_header(identity, uid, token, ts_ms)


# ══════════════════════════════════════════════════════════════════
# 6. 响应解密
# ══════════════════════════════════════════════════════════════════
def rsa_recovered_digest(response_s: str) -> str:
    """s → RSA 公钥 raw 运算 → 恢复 PKCS#1 type-1 块尾部的 32 字节小写 hex"""
    sig = b64d(response_s)
    size = (_RSA_KEY.n.bit_length() + 7) // 8
    if len(sig) != size:
        raise ValueError("s 长度 %d != %d" % (len(sig), size))
    em = pow(int.from_bytes(sig, "big"), _RSA_KEY.e, _RSA_KEY.n).to_bytes(size, "big")
    if em[0] != 0x00 or em[1] != 0x01:
        raise ValueError("s 不是 PKCS#1 type-1 块: %s" % em[:4].hex())
    i = 2
    while i < len(em) and em[i] == 0xFF:
        i += 1
    if i >= len(em) or em[i] != 0x00:
        raise ValueError("s 缺少 PKCS#1 分隔符")
    tail = em[i + 1:]
    if len(tail) != 32:
        raise ValueError("摘要长度 %d != 32" % len(tail))
    return tail.decode("ascii")


def decrypt_response(raw_text: str, paes_key: bytes):
    """返回 (业务 JSON, 诊断信息)"""
    try:
        obj = json.loads(raw_text)
    except Exception as e:
        return None, "响应不是合法 JSON: %s" % e

    if isinstance(obj, dict) and "resp" in obj:
        r0 = obj["resp"]
        obj = json.loads(r0) if isinstance(r0, str) else (r0 if r0 is not None else obj)

    if not (isinstance(obj, dict) and all(k in obj for k in ("r", "s", "v"))):
        return obj, "明文响应（非加密）"

    v = obj["v"]
    v = int(v) if not isinstance(v, int) else v
    if v != 101:
        return None, "响应版本错误: %s != 101" % v

    expected = rsa_recovered_digest(obj["s"])
    layer1 = aes_decrypt(paes_key, b64d(obj["r"]))
    actual = md5_hex(layer1)
    if actual != expected:
        return None, "响应验签失败: 实际 %s 期望 %s" % (actual, expected)

    plaintext = layer1.decode("utf-8", "replace")
    env = json.loads(plaintext)
    if isinstance(env, dict) and isinstance(env.get("d"), str):
        outer = json.loads(aes_decrypt(paes_key, b64d(env["d"])))
    else:
        outer = env
    if isinstance(outer, dict) and isinstance(outer.get("data"), str):
        business = json.loads(b64d(outer["data"]))
    else:
        business = outer
    return business, None


# ══════════════════════════════════════════════════════════════════
# 7. 自检
# ══════════════════════════════════════════════════════════════════
def selftest():
    ok = True
    print("=" * 72)
    print("自检 1 · fold32")
    for s, exp in [("nhang.school", 0x61297d61), ("5K0E8400-E29", 0x203a324c),
                   ("597DEA1AFB49", 0x363a323c), ("86123.456789", 0x3d2f3d3e)]:
        got = fold32(s)
        flag = got == exp
        ok &= flag
        print("  %s fold32(%-16r) = %#010x" % ("OK " if flag else "!! ", s, got))

    print("自检 2 · derive_paes_key")
    k = derive_paes_key("nhang.school", "5K0E8400-E29", "597DEA1AFB49", "86123.456789")
    flag = k.hex() == "faaed5a4d99af386a7d3f5d109071f13"
    ok &= flag
    print("  %s pAesKey = %s" % ("OK " if flag else "!! ", k.hex()))

    print("自检 3 · AES-128-CBC/IV=0 测试向量")
    ct = aes_encrypt(b"0123456789abcdef", b"hello world, this is a test!!")
    flag = ct.hex() == "c4cf3b785dd429f0d80254ad853d92e1295bc6969a5f029dd83b939da9566c78"
    ok &= flag
    print("  %s AES 向量" % ("OK " if flag else "!! "))

    print("自检 4 · keyDataFour 格式")
    four = normalize_key_data("{:.6f}".format(1788958186123.4568))
    flag = four == "86123.456787"
    ok &= flag
    print("  %s keyDataFour = %r" % ("OK " if flag else "!! ", four))

    print("自检 5 · tokenSign（公开版测试向量）")
    cases = [
        (13056447, "TOKENABC", 1788958186123, "4a8d163186d91ac7b539a7bb07d457ba"),
    ]
    for uid, tok, ts, exp in cases:
        got = native_token_sign(uid, tok, ts)
        flag = got == exp
        ok &= flag
        print("  %s tokenSign(uid=%d, ts=%d)" % ("OK " if flag else "!! ", uid, ts))

    print("自检 6 · 信封构造 + 本地回解闭环")
    sess = EnvelopeSession(first_ms=1788958186123)
    env_js, meta = sess.build_envelope('{"a":1}', "insert")
    env = json.loads(env_js)
    back = aes_decrypt(meta["req_key"].encode(), b64d(env["d"]))
    flag = (back == meta["container"] and md5_hex(back) == env["h"]
            and len(b64d(env["k"])) == 128)
    ok &= flag
    print("  %s d 回解 == container, h == MD5(container), k == 128B" % ("OK " if flag else "!! "))

    print("自检 7 · 响应解密链（自造响应）")
    inner = json.dumps({"error": 10000, "data": {"uid": 12345}},
                       separators=(",", ":"))
    layer2 = json.dumps({"data": b64e(inner.encode()), "timeStamp": 1},
                        separators=(",", ":"))
    inner_env = '{"k":"x","p":101,"d":"%s","h":"y","t":0}' % b64e(
        aes_encrypt(sess.paes_key, layer2.encode()))
    r_b64 = b64e(aes_encrypt(sess.paes_key, inner_env.encode()))

    from Crypto.PublicKey import RSA as _R
    from Crypto.Cipher import PKCS1_v1_5 as _P
    tmp = _R.generate(1024)
    em = bytes([0, 1]) + b"\xff" * (128 - 3 - 32) + b"\x00" + md5_hex(inner_env).encode()
    s_sig = _P.new(tmp).encrypt  # not used
    d_key = tmp.d
    sig = pow(int.from_bytes(em, "big"), d_key, tmp.n).to_bytes(128, "big")
    raw = json.dumps({"r": r_b64, "s": b64e(sig), "v": 101}, separators=(",", ":"))
    # 用同一临时密钥的公钥验签
    import swclient  # noqa
    old = globals()["_RSA_KEY"]
    globals()["_RSA_KEY"] = tmp.publickey()
    biz, err = decrypt_response(raw, sess.paes_key)
    globals()["_RSA_KEY"] = old
    flag = (err is None and biz and biz.get("data", {}).get("uid") == 12345)
    ok &= bool(flag)
    print("  %s 响应解密链闭环 (uid=12345)" % ("OK " if flag else "!! "))

    print("=" * 72)
    print("汇总: %s" % ("全部通过" if ok else "存在失败项"))
    print("=" * 72)
    return ok


if __name__ == "__main__":
    selftest()

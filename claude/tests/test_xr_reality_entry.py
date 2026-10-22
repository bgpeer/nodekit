#!/usr/bin/env python3
"""xray reality 入口（仓库主人 10/22）：sni-split 下 vision² / grpc² / xhttp² 共用 443 上一个 reality 入口。
  vision² 那个入站当入口，不是 vision 的连接解开 reality 后回落到 nginx 本地 h2c（REALITY_FB_PORT），按服务名 / 路径分给
  本地明文的 grpc² / xhttp²。删 vision² 时入口改名留下（零用户照样回落，实测过），后端删光入口才拆；加回 vision² 用户并回入口。
  会话里用真 xray + nginx + mihomo / xray 客户端跑过：增删各种组合每一步都通；入口开着抗量子签名时 grpc² / xhttp²
  带对的 pqv 通、错的不通。这里守逻辑。
"""
import os, sys, json, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
xrcfg = f"{tmp}/xr.json"
m._CORE_META = {"sb": ("sing-box", f"{tmp}/sbbin", f"{tmp}/sb.json", m.SB, "¹"), "xray": ("xray", f"{tmp}/xrbin", xrcfg, m.XRAY, "²")}
m.next_port = lambda: 30001
m._xr_entry_keys = lambda: ("PRIV", "PUB", "abcd", "", "")
m.G.update({"sni": "a.example", "sni_xr": "b.example", "sni_split": "1", "domain": "d.example", "host": "d.example"})

def vision(tag="P·reality-vision²", uid="v1"):
    return {"listen": "127.0.0.1", "port": 2001, "protocol": "vless", "tag": tag,
            "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "reality", "realitySettings": {"serverNames": ["b.example"]}}}
def grpc():
    return {"listen": "127.0.0.1", "port": 2002, "protocol": "vless", "tag": "P·reality-grpc²",
            "settings": {"clients": [{"id": "g1"}], "decryption": "none"},
            "streamSettings": {"network": "grpc", "grpcSettings": {"serviceName": "grpcab", "multiMode": True}}}
def xhttp():
    return {"listen": "127.0.0.1", "port": 2003, "protocol": "vless", "tag": "P·reality-xhttp",
            "settings": {"clients": [{"id": "x1"}], "decryption": "none"},
            "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": "/xp"}}}
tags = lambda ibs: [ib["tag"] for ib in ibs]
fb = lambda ib: (ib.get("settings") or {}).get("fallbacks")

# 1. 新装三个：vision² 当入口、带回落
ibs = m._xr_entry_fixup([vision(), grpc(), xhttp()])
check(f"三个都装：vision² 当入口带回落（{tags(ibs)}）", tags(ibs) == ["P·reality-vision²", "P·reality-grpc²", "P·reality-xhttp"]
      and fb(ibs[0]) == [{"dest": m.REALITY_FB_PORT}])

# 2. 只装 vision²：不带回落
ibs = m._xr_entry_fixup([vision()])
check("只有 vision²：不带回落", not fb(ibs[0]))

# 3. 删 vision²、后面还挂着：入口改名留下、零用户、照样回落
v = vision(); ent0 = m._xr_entry_fixup([v, grpc()])[0]
ibs = m._xr_entry_fixup([grpc()], ent0)
e = [ib for ib in ibs if ib["tag"] == m.XR_ENTRY_TAG]
check(f"删 vision²：入口改名留下、零用户、回落还在（{tags(ibs)}）", len(e) == 1 and e[0]["settings"]["clients"] == [] and fb(e[0])
      and e[0]["streamSettings"]["realitySettings"]["serverNames"] == ["b.example"])
check(f"入口不算协议（{m._ib_proto(e[0])}）", m._ib_proto(e[0]) not in m.XRAY)

# 4. 加回 vision²：用户并回入口，入口改回 vision² 的名字，新建的那个丢掉（密钥用入口的）
ibs = m._xr_entry_fixup(ibs + [vision(uid="v2")])
ents = [ib for ib in ibs if m._xr_is_entry(ib)]
check(f"加回 vision²：并回入口（{tags(ibs)}）", len(ents) == 1 and ents[0]["tag"] == "P·reality-vision²"
      and ents[0]["settings"]["clients"] == [{"id": "v2", "flow": "xtls-rprx-vision"}] and fb(ents[0]))

# 5. 后端删光：零用户入口拆掉；vision² 去掉回落
ibs = m._xr_entry_fixup([ib for ib in m._xr_entry_fixup([grpc()], ent0) if ib["tag"] == m.XR_ENTRY_TAG])
check("后端删光：零用户入口拆掉", ibs == [])
ibs = m._xr_entry_fixup([ent0])
check("后端删光、vision² 还在：只去掉回落", tags(ibs) == ["P·reality-vision²"] and not fb(ibs[0]))

# 6. 只装 grpc²（没有 vision²）：建零用户入口
ibs = m._xr_entry_fixup([grpc()])
e = [ib for ib in ibs if ib["tag"] == m.XR_ENTRY_TAG]
check("只有 grpc²：自动建零用户入口（本地、reality、带回落）", len(e) == 1 and e[0]["listen"] == "127.0.0.1"
      and e[0]["streamSettings"]["security"] == "reality" and fb(e[0]) and e[0]["port"] == 30001)

# 7. 不在分流模式（grpc² 自带 reality、走公网口）：完全不动
pub = dict(grpc(), listen="0.0.0.0", streamSettings={"network": "grpc", "security": "reality", "realitySettings": {}})
check("不在入口后面的：原样不动", m._xr_entry_fixup([pub]) == [pub])

# 8. 链接 UUID：入口 = 自己的用户 + 后面挂的；nginx 两张表不收后端，h2c 回落口收
data = {"inbounds": m._xr_entry_fixup([vision(), grpc(), xhttp()])}
check(f"入口对应的链接：vision + grpc + xhttp（{m._xr_link_uuids(data, data['inbounds'][0])}）",
      m._xr_link_uuids(data, data["inbounds"][0]) == ["v1", "g1", "x1"])
json.dump(data, open(xrcfg, "w"))
ws, stream = m._nginx_backends_from_cfg()
check(f"反代表不收入口后面的（{ws}），分流表只有入口（{stream}）", ws == [] and stream == [{"sni": "b.example", "port": 2001}])
srv = m._nginx_fb_server()
check("h2c 回落口：按服务名 / 路径 grpc_pass，其余 404", f"127.0.0.1:{m.REALITY_FB_PORT}" in srv
      and "location /grpcab/ {" in srv and "grpc_pass grpc://127.0.0.1:2002;" in srv
      and "location /xp/ {" in srv and "grpc_pass grpc://127.0.0.1:2003;" in srv and "return 404;" in srv)
check(f"换伪装域名要改的链接含后端（{sorted(m._reality_uuids('xray'))}）", m._reality_uuids("xray") == {"v1", "g1", "x1"})
json.dump({"inbounds": [vision()]}, open(xrcfg, "w"))
check("没有后端：不写 h2c 回落口", m._nginx_fb_server() == "")

# 9. 建节点：挂入口后面的 grpc² / xhttp² 本地明文、链接走 443 + 入口的公钥 / 伪装域名
m.G["sni_xr"] = "b.example"
ib, lk = m.xr_reality_grpc(2002, "P·reality-grpc²")
check(f"grpc²：本地明文、链接 443 + 入口公钥（{lk[:90]}…）", ib["listen"] == "127.0.0.1" and "security" not in ib["streamSettings"]
      and ":443?" in lk and "pbk=PUB" in lk and "sni=b.example" in lk and "sid=abcd" in lk and "serviceName=" in lk)
ib, lk = m.xr_reality_xhttp(2003, "P·reality-xhttp")
check("xhttp²：本地明文、链接 443 + 入口公钥、host = 伪装域名", ib["listen"] == "127.0.0.1" and "security" not in ib["streamSettings"]
      and ":443?" in lk and "pbk=PUB" in lk and "host=b.example" in lk)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""小火箭写进本机 xray 内核的 vision² —— 仓库主人 10/18：「vision²」。
  xray 26.9.8+ 只收带 X25519MLKEM768 的 ClientHello，小火箭现在连不上，但照写（哪天小火箭带上就能用）；
  本机 xray 内核的其余 reality（grpc / xhttp）照旧跳过，屏上一行说明跳过几条。
"""
import os, sys, io, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.XRAY_DIR = f"{tmp}/xray"; os.makedirs(m.XRAY_DIR)
m.SR_FILE = f"{tmp}/sr.conf"; m.SR_NODES_FILE = f"{tmp}/sr.txt"
m.sr_sub_mode = lambda: False
m.fetch_tpl = lambda u: "[Proxy]\n_NOD_\n[Proxy Group]\n_GRP_\n[Rule]\n"
json.dump({"inbounds": [{"port": p, "streamSettings": {"security": "reality"}} for p in (23456, 8444)]},
          open(f"{m.XRAY_DIR}/config.json", "w"))
m._local_xray_reality = lambda: {("v.example.com", 23456), ("v.example.com", 8444)}
ro = {"public-key": "K", "short-id": "ab"}
nodes = [("a", {"name": "reality-vision²", "type": "vless", "server": "v.example.com", "port": 23456, "uuid": "u",
                "tls": True, "servername": "www.cisco.com", "flow": "xtls-rprx-vision", "reality-opts": ro}),
         ("b", {"name": "reality-grpc²", "type": "vless", "server": "v.example.com", "port": 8444, "uuid": "u", "tls": True,
                "servername": "www.cisco.com", "network": "grpc", "grpc-opts": {"grpc-service-name": "g"}, "reality-opts": ro})]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.build_shadowrocket_sub(nodes, "u")
conf = open(m.SR_FILE).read() if os.path.exists(m.SR_FILE) else ""
v2 = [l for l in conf.splitlines() if l.startswith("reality-vision² = ")]
check(f"vision² 写进配置，带 xtls=2 和 reality 公钥（{v2}）", len(v2) == 1 and ",xtls=2" in v2[0] and "publicKey=K" in v2[0])
check("xray 内核的 grpc reality 照旧不写", "reality-grpc² = " not in conf)
check(f"屏上一行：跳过 1 条（{buf.getvalue().strip()}）", "小火箭跳过 1 条 xray 内核的 reality" in buf.getvalue())
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

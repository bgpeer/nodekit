#!/usr/bin/env python3
"""服务端不做 Host 校验（10/29 真机：开了节点专用域名，vmess-httpupgrade 一直超时，sing-box 日志
  「bad host: 随机.随机.根域名」；xray 的 xhttp-tls 同样超时）。服务端写了 host，sing-box / xray 就只认那一个名字，
  客户端换成节点专用域名就被拒。新装的不写；老机器开节点专用域名时把配置里的去掉、校验过了才重启。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

print("一、新装")
for t in ("httpupgrade", "h2", "ws"):
    tr = x._sb_transport(t, "/p", "node.example")
    check(f"sing-box {t} 入站不写 host（{tr}）", "host" not in tr and tr["path"] == "/p")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("xray xhttp 入站不写 host（两处：nginx 前置、直连）", '"xhttpSettings": {"host": tls_host()' not in src)

print("二、老机器开节点专用域名时去掉")
tmp = tempfile.mkdtemp()
x.SB_DIR, x.XRAY_DIR = f"{tmp}/sb", f"{tmp}/xr"
os.makedirs(x.SB_DIR); os.makedirs(x.XRAY_DIR)
x.SB_BIN, x.XRAY_BIN = f"{tmp}/sing-box", f"{tmp}/xray"
open(x.SB_BIN, "w").write(""); open(x.XRAY_BIN, "w").write("")
SB = {"inbounds": [{"type": "vmess", "tag": "hu", "transport": {"type": "httpupgrade", "path": "/a", "host": "node.example"}},
                   {"type": "vless", "tag": "ws", "transport": {"type": "ws", "path": "/b"}},
                   {"type": "anytls", "tag": "at"}]}
XR = {"inbounds": [{"tag": "xh", "streamSettings": {"network": "xhttp", "xhttpSettings": {"host": "node.example", "path": "/c", "mode": "auto"}}},
                   {"tag": "rl", "streamSettings": {"security": "reality", "xhttpSettings": {"host": "www.cisco.com", "path": "/d"}}}]}
json.dump(SB, open(f"{x.SB_DIR}/config.json", "w")); json.dump(XR, open(f"{x.XRAY_DIR}/config.json", "w"))
CMDS, CHECK = [], {"sing-box": True, "xray": True}
x.sh = lambda c, check=True: CMDS.append(c) or ""
x.core_check = lambda b, c: (CHECK["xray" if "xray" in b else "sing-box"], "")
n = x._core_host_heal()
sb, xr = json.load(open(f"{x.SB_DIR}/config.json")), json.load(open(f"{x.XRAY_DIR}/config.json"))
check(f"去掉了 2 处（{n}）", n == 2)
check("sing-box httpupgrade 的 host 没了、path 还在", sb["inbounds"][0]["transport"] == {"type": "httpupgrade", "path": "/a"})
check("xray xhttp 的 host 没了", xr["inbounds"][0]["streamSettings"]["xhttpSettings"] == {"path": "/c", "mode": "auto"})
check("reality 入站不碰", xr["inbounds"][1]["streamSettings"]["xhttpSettings"]["host"] == "www.cisco.com")
check("改了的两个核心各重启一次", sorted(CMDS) == ["systemctl restart sing-box", "systemctl restart xray"])
CMDS.clear()
check("再跑一次：没得去、不重启", x._core_host_heal() == 0 and not CMDS)
json.dump(SB, open(f"{x.SB_DIR}/config.json", "w"))
CHECK["sing-box"] = False
with contextlib.redirect_stdout(io.StringIO()) as buf:
    n = x._core_host_heal()
check("校验没过：原样放回、不重启、说一行", json.load(open(f"{x.SB_DIR}/config.json")) == SB and not CMDS and n == 0
      and "原样放回" in buf.getvalue())
body = src[src.index("def privdns_domain_apply("):src.index("\ndef ", src.index("def privdns_domain_apply(") + 5)]
check("开节点专用域名（后台那一步）会跑它", "_core_host_heal()" in body)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

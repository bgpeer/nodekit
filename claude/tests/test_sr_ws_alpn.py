#!/usr/bin/env python3
"""小火箭 ws 节点钉 alpn=http/1.1 —— 仓库主人 10/18：朋友苹果小火箭 ws 节点 C 测试超时（T 测试绿），yaml（mihomo）同样的节点通。
  服务器本机：curl --http1.1 走 nginx 443 → 101；curl --http2 → 握手失败。nginx 443 开着 HTTP/2，小火箭不写 alpn 会协商成 h2，
  ws 升级只认 HTTP/1.1。修法：TLS 的 ws（vless / vmess / trojan）在小火箭行里加 alpn=http/1.1；明文 ws、grpc 不加。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
L = m.shadowrocket_line
ws = {"network": "ws", "ws-opts": {"path": "/p", "headers": {"Host": "h.example.com"}}}
vl = L("a", dict(type="vless", server="h.example.com", port=443, uuid="u", tls=True, servername="h.example.com", **ws))
vm = L("b", dict(type="vmess", server="h.example.com", port=443, uuid="u", alterId=0, cipher="auto", tls=True, servername="h.example.com", **ws))
tj = L("c", dict(type="trojan", server="h.example.com", port=443, password="x", sni="h.example.com", **ws))
pl = L("d", dict(type="vmess", server="plain.example.com", port=80, uuid="u", **ws))
gr = L("e", dict(type="vless", server="h.example.com", port=44, uuid="u", tls=True, servername="h.example.com", network="grpc",
                 **{"grpc-opts": {"grpc-service-name": "g"}}))
rv = L("f", dict(type="vless", server="h.example.com", port=443, uuid="u", tls=True, servername="r.example.com", flow="xtls-rprx-vision",
                 **{"reality-opts": {"public-key": "k", "short-id": "s"}}))
for nm, s in (("vless-ws", vl), ("vmess-ws", vm), ("trojan-ws", tj)):
    check(f"{nm}（TLS）带 alpn=http/1.1，且在 obfs=websocket 那组参数里", "obfs=websocket" in s and ",alpn=http/1.1," in s and s.count("alpn=") == 1)
check("明文 ws（没 TLS）不加 alpn", "obfs=websocket" in pl and "alpn=" not in pl)
check("grpc / reality-vision 不加 alpn", "alpn=" not in gr and "alpn=" not in rv)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

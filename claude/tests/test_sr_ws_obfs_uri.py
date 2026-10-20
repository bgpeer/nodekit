#!/usr/bin/env python3
"""小火箭 ws 节点写 obfs-uri / obfs-host —— 仓库主人 10/20 苹果实测：同一组 ws 节点五种写法，
  path + obfsParam（加 alpn=http/1.1、Host 写成 JSON 都一样）全超时，只写 obfs-uri + obfs-host 的全通
  （vless-ws / vmess-ws 经 nginx 443、vless-ws² xray 直连）。10/18 加的 alpn=http/1.1 不是解药，去掉。
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
nh = L("e", dict(type="vless", server="h.example.com", port=443, uuid="u", tls=True, servername="s.example.com", network="ws"))
for nm, s in (("vless-ws", vl), ("vmess-ws", vm), ("trojan-ws", tj), ("明文 vmess-ws", pl)):
    check(f"{nm}：obfs=websocket,obfs-uri=/p,obfs-host=h.example.com（{s}）",
          ",obfs=websocket,obfs-uri=/p,obfs-host=h.example.com," in s)
    check(f"{nm}：不再写 path= / obfsParam= / alpn=", not any(k in s for k in (",path=", "obfsParam=", "alpn=")))
check(f"没写路径 / Host：obfs-uri=/、obfs-host 用 SNI（{nh}）", ",obfs-uri=/,obfs-host=s.example.com," in nh)
# httpupgrade：10/20 实测 obfs=httpupgrade,obfs-uri,obfs-host 通（http-upgrade / websocket+开关 / path+obfsParam 不通）
hu = L("h", dict(type="vmess", server="h.example.com", port=443, uuid="u", alterId=0, cipher="auto", tls=True, servername="h.example.com",
                 network="ws", **{"ws-opts": {"path": "/hu", "headers": {"Host": "h.example.com"}, "v2ray-http-upgrade": True}}))
check(f"vmess-httpupgrade：obfs=httpupgrade,obfs-uri=/hu,obfs-host=h.example.com（{hu}）",
      hu and ",obfs=httpupgrade,obfs-uri=/hu,obfs-host=h.example.com," in hu and "websocket" not in hu)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

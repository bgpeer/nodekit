#!/usr/bin/env python3
"""sing-box 订阅里 vless + httpupgrade 被写成 ws（11/09 全仓检查查出来的）：
  mihomo 里 httpupgrade 也是 network: ws，靠 ws-opts 的 v2ray-http-upgrade 区分。vmess 那支分了，vless 那支一律写 ws，
  服务端是 httpupgrade 的 vless 节点，sing-box 客户端拿订阅连过去：服务端报 real websocket request received、客户端 404。
  本地起真 sing-box 服务端 + 客户端实测：修前只有这一种不通，修后 ws / httpupgrade / grpc / hy2 / tuic / anytls 全通。
  这里逐个对「服务端入站的传输」和「订阅出站的传输」：ws 家族每种、nginx 前置和自己端口两种装法都得一致。
"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
x.ensure_acme = lambda: (f"{tmp}/c.crt", f"{tmp}/c.key", True)
x.G.update(host="node.example.com", domain="", sni="node.example.com")
CASES = [("vless-ws", x.make_sb_vless("ws")), ("vless-httpupgrade", x.make_sb_vless("httpupgrade")),
         ("vmess-ws", x.make_sb_vmess("ws")), ("vmess-httpupgrade", x.make_sb_vmess("httpupgrade"))]

print("一、服务端入站 = 订阅出站")
for front in (False, True):
    x._nginx_front = lambda f=front: f
    where = "nginx 443 前置" if front else "自己端口"
    for name, fn in CASES:
        ib, lk = fn(20000, "t-" + name)
        d = x.link_to_proxy(lk)
        ob = x.mihomo_to_sb_outbound(x.proto_key(d), d) or {}
        st, ct = ib["transport"], ob.get("transport") or {}
        check(f"{where} {name}：传输 服务端 {st['type']} = 订阅 {ct.get('type')}",
              st["type"] == ct.get("type") and st["path"] == ct.get("path"))

print("二、别的没被带偏")
d = x.link_to_proxy("vless://" + x.new_uuid() + "@cdn.example:2053?encryption=none&security=tls&sni=cdn.example"
                    "&fp=chrome&type=ws&host=cdn.example&path=%2Fcdn#CDN")
check("CDN 备用节点（vless + ws）照旧 ws", x.mihomo_to_sb_outbound(x.proto_key(d), d)["transport"]["type"] == "ws")
d = x.link_to_proxy("vless://" + x.new_uuid() + "@node.example:443?encryption=none&security=tls&sni=node.example"
                    "&fp=chrome&type=httpupgrade&host=node.example&path=%2Fhu&smux=1#hu")
ob = x.mihomo_to_sb_outbound(x.proto_key(d), d)
check("httpupgrade 开了 smux：照旧带 multiplex", ob["transport"]["type"] == "httpupgrade" and ob.get("multiplex"))
check("mihomo 那份照旧：ws + v2ray-http-upgrade", d["network"] == "ws" and d["ws-opts"].get("v2ray-http-upgrade") == "true")

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

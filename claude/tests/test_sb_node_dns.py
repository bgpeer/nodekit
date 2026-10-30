#!/usr/bin/env python3
"""sing-box：用公网域名的节点交给境外 DoH 解析（仓库主人 10/30：「就算没建私有DNS也不应该让国内的解析的，
  这就等于直接暴露了IP……不可以1.1.1.1解析吗？」）。以前没指 domain_resolver 的出站走 route.default_domain_resolver，
  作者模板里那是阿里。mihomo 那份本来就走 1.1.1.1 / 8.8.8.8 / 9.9.9.9（proxy-server-nameserver）。
"""
import os, sys, json, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

TPL = json.loads(open(os.path.join(ROOT, "subbox-template.json")).read()
                 .replace('"_GRP_",', "").replace(',\n    "_NOD_"', "").replace('"_NOD_"', ""))
def cfg_with(obs, servers=None):
    c = json.loads(json.dumps(TPL))
    c["outbounds"] = [o for o in c["outbounds"] if isinstance(o, dict)] + obs
    if servers is not None:
        c["dns"]["servers"] = servers
    return c
ip = lambda *o: ".".join(map(str, o))
OB = [{"tag": "a·hy2", "type": "hysteria2", "server": "node.example.com", "server_port": 443},
      {"tag": "b·reality", "type": "vless", "server": "node.example.com", "server_port": 443},
      {"tag": "c·CDN", "type": "vless", "server": ip(104, 16, 1, 1), "server_port": 443},
      {"tag": "d·v6", "type": "vless", "server": "2001:db8::1", "server_port": 443},
      {"tag": "e·被藏", "type": "vless", "server": "k7xq2m4p9z.a8f3kq.example.com", "domain_resolver": {"server": "privdns"}}]

print("一、作者模板")
c = cfg_with(json.loads(json.dumps(OB)))
x._sb_node_dns(c)
srv = {s["tag"]: s for s in c["dns"]["servers"]}
ob = {o["tag"]: o for o in c["outbounds"] if isinstance(o, dict) and o.get("tag")}
check(f"加了一台 nodedns：直连 1.1.1.1 的 DoH（{srv.get('nodedns')}）",
      srv.get("nodedns", {}).get("server") == "1.1.1.1" and srv["nodedns"]["type"] == "https"
      and srv["nodedns"]["tls"]["server_name"] == "cloudflare-dns.com" and srv["nodedns"].get("detour") == "🎯直连")
check("用域名的节点都指它（写对象，SFA 1.15 不认简写）",
      ob["a·hy2"]["domain_resolver"] == {"server": "nodedns"} and ob["b·reality"]["domain_resolver"] == {"server": "nodedns"})
check("地址是 IP（v4 / v6）的不加", "domain_resolver" not in ob["c·CDN"] and "domain_resolver" not in ob["d·v6"])
check("被藏的节点照旧指私有 DNS", ob["e·被藏"]["domain_resolver"] == {"server": "privdns"})
check("route.default_domain_resolver 不动（国内网站照旧用国内 DNS）",
      c["route"]["default_domain_resolver"] == TPL["route"]["default_domain_resolver"])
check("选择器 / 直连这些不带 server 的出站不碰",
      not [o for o in c["outbounds"] if isinstance(o, dict) and "server" not in o and o.get("domain_resolver")])
n = len(c["dns"]["servers"])
x._sb_node_dns(c)
check("再跑一次不重复加", len(c["dns"]["servers"]) == n)

print("二、自定义模板")
mine = {"tag": "nodedns", "type": "https", "server": "9.9.9.9", "path": "/dns-query"}
c = cfg_with(json.loads(json.dumps(OB)), TPL["dns"]["servers"] + [mine])
x._sb_node_dns(c)
check("模板里自己写了 nodedns：用模板的，不另加", [s for s in c["dns"]["servers"] if s.get("tag") == "nodedns"] == [mine])
c = cfg_with([{"tag": "x", "type": "vless", "server": ip(203, 0, 113, 5)}])
before = json.dumps(c["dns"])
x._sb_node_dns(c)
check("没有用域名的节点：什么都不加", json.dumps(c["dns"]) == before)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
body = src[src.index("def build_singbox_sub("):src.index("\ndef ", src.index("def build_singbox_sub(") + 5)]
check("生成 sing-box 配置时在私有解析之后跑", 0 < body.index("_sb_privdns(cfg, nodes)") < body.index("_sb_node_dns(cfg)"))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

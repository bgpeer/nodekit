#!/usr/bin/env python3
"""sing-box：用公网域名的节点不交给国内 DNS 解析（仓库主人 10/30：「就算没建私有DNS也不应该让国内的解析的，
  这就等于直接暴露了IP……」）。以前没指 domain_resolver 的出站走 route.default_domain_resolver，作者模板里那是阿里。
  先试了直连 1.1.1.1 DoH：仓库主人手机 4G 带不带 cloudflare-dns.com 都不通（sing-box 一个出站只能指一台，不通就全挂）；
  指自建 DNS 一试就通。挑法：模板里的 nodedns → 本机自建 DNS → 私有解析那台 → 都没有不动。
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
def cfg_with(obs, extra_servers=()):
    c = json.loads(json.dumps(TPL))
    c["outbounds"] = [o for o in c["outbounds"] if isinstance(o, dict)] + json.loads(json.dumps(obs))
    c["dns"]["servers"] += list(extra_servers)
    return c
ip = lambda *o: ".".join(map(str, o))
DOH = "https" + "://dns.example.com:10443/dns-query/xyabc123"
OB = [{"tag": "a·hy2", "type": "hysteria2", "server": "node.example.com", "server_port": 443},
      {"tag": "b·CDN·ws", "type": "vless", "server": "cdn.example.com", "server_port": 443},
      {"tag": "c·CDN·优选", "type": "vless", "server": ip(104, 16, 1, 1), "server_port": 443},
      {"tag": "d·v6", "type": "vless", "server": "2001:db8::1", "server_port": 443},
      {"tag": "e·被藏", "type": "vless", "server": "k7xq2m4p9z.a8f3kq.example.com", "domain_resolver": {"server": "privdns"}}]
def obmap(c):
    return {o["tag"]: o for o in c["outbounds"] if isinstance(o, dict) and o.get("tag")}
def srvmap(c):
    return {s["tag"]: s for s in c["dns"]["servers"]}

print("一、本机自建 DNS 写进了订阅")
c = cfg_with(OB)
x._sb_node_dns(c, DOH)
s, ob = srvmap(c), obmap(c)
check(f"加了一台 nodedns 指自建 DoH（{s.get('nodedns')}）",
      s.get("nodedns", {}).get("server") == "dns.example.com" and s["nodedns"]["server_port"] == 10443
      and s["nodedns"]["path"] == "/dns-query/xyabc123" and s["nodedns"].get("detour") == "🎯直连"
      and s["nodedns"].get("domain_resolver") == "bootstrap")
check("用域名的节点都指它（写对象，SFA 1.15 不认简写）",
      ob["a·hy2"]["domain_resolver"] == ob["b·CDN·ws"]["domain_resolver"] == {"server": "nodedns"})
check("地址是 IP（v4 / v6）的不加", "domain_resolver" not in ob["c·CDN·优选"] and "domain_resolver" not in ob["d·v6"])
check("被藏的节点照旧指私有 DNS", ob["e·被藏"]["domain_resolver"] == {"server": "privdns"})
check("route.default_domain_resolver 不动（国内网站照旧用国内 DNS）",
      c["route"]["default_domain_resolver"] == TPL["route"]["default_domain_resolver"])
check("不加直连境外公共 DoH（10/30 实测 1.1.1.1 直连不通）", "1.1.1.1" not in json.dumps(s.get("nodedns")))
n = len(c["dns"]["servers"])
x._sb_node_dns(c, DOH)
check("再跑一次不重复加", len(c["dns"]["servers"]) == n)

print("二、模板里自己写了 nodedns")
mine = {"tag": "nodedns", "type": "https", "server": ip(9, 9, 9, 9), "path": "/dns-query"}
c = cfg_with(OB, [mine])
x._sb_node_dns(c, DOH)
check("用模板的，不另加", [v for v in c["dns"]["servers"] if v.get("tag") == "nodedns"] == [mine]
      and obmap(c)["a·hy2"]["domain_resolver"] == {"server": "nodedns"})

print("三、没写自建 DNS 进订阅")
PRIV = {"tag": "privdns", "type": "https", "server": "dns.example.com", "server_port": 10443, "path": "/dns-query/xyabc123"}
c = cfg_with(OB, [PRIV])
x._sb_node_dns(c, "")
check("有私有解析那台：用域名的节点指它", obmap(c)["b·CDN·ws"]["domain_resolver"] == {"server": "privdns"}
      and "nodedns" not in srvmap(c))
c = cfg_with(OB)
before = json.dumps(c)
x._sb_node_dns(c, "")
check("什么都没有：不动（照旧走默认 DNS，能用比藏重要）", json.dumps(c) == before)
c = cfg_with([{"tag": "x", "type": "vless", "server": ip(203, 0, 113, 5)}])
before = json.dumps(c)
x._sb_node_dns(c, DOH)
check("没有用域名的节点：什么都不加", json.dumps(c) == before)

src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
body = src[src.index("def build_singbox_sub("):src.index("\ndef ", src.index("def build_singbox_sub(") + 5)]
check("生成 sing-box 配置时在私有解析之后跑、带着自建 DNS 地址",
      0 < body.index("_sb_privdns(cfg, nodes)") < body.index("_sb_node_dns(cfg, _selfdns_doh())"))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

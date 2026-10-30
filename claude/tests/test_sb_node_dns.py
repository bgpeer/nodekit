#!/usr/bin/env python3
"""sing-box：用公网域名的节点不交给国内 DNS 解析（仓库主人 10/30：「就算没建私有DNS也不应该让国内的解析的，
  这就等于直接暴露了IP……」）。以前没指 domain_resolver 的出站走 route.default_domain_resolver，作者模板里那是阿里。
  先试了直连 1.1.1.1 DoH：仓库主人手机 4G 带不带 cloudflare-dns.com 都不通（sing-box 一个出站只能指一台，不通就全挂）；
  指自建 DNS 一试就通。作者模板第二行留了 nodedns 这个口子（默认跟 local 一样是阿里），跟 final 一个路数：
  有自建 DNS 换成自建的、没有就换成私有解析那台、都没有照模板。模板里没这一行的老模板：有自建 DNS 才加一台。
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

NOSLOT = [v for v in TPL["dns"]["servers"] if v.get("tag") != "nodedns"]
def cfg_noslot(obs, extra=()):
    c = cfg_with(obs, extra)
    c["dns"]["servers"] = [v for v in c["dns"]["servers"] if v.get("tag") != "nodedns"]
    return c

print("零、作者模板留的口子")
slot = srvmap(cfg_with([])).get("nodedns")
check(f"模板第二行就是 nodedns，默认跟 local 一样是阿里（{slot}）",
      TPL["dns"]["servers"][1].get("tag") == "nodedns" and slot["server"] == ip(223, 5, 5, 5) and slot.get("detour") == "🎯直连")

print("一、本机自建 DNS 写进了订阅")
c = cfg_with(OB)
x._sb_node_dns(c, DOH)
s, ob = srvmap(c), obmap(c)
check(f"nodedns 换成自建 DoH，还在原位、tag 不变（{s.get('nodedns')}）",
      c["dns"]["servers"][1]["tag"] == "nodedns" and s["nodedns"]["server"] == "dns.example.com"
      and s["nodedns"]["server_port"] == 10443 and s["nodedns"]["path"] == "/dns-query/xyabc123"
      and s["nodedns"].get("detour") == "🎯直连" and s["nodedns"].get("domain_resolver") == "bootstrap"
      and "tls" not in s["nodedns"])
check("用域名的节点都指它（写对象，SFA 1.15 不认简写）",
      ob["a·hy2"]["domain_resolver"] == ob["b·CDN·ws"]["domain_resolver"] == {"server": "nodedns"})
check("地址是 IP（v4 / v6）的不加", "domain_resolver" not in ob["c·CDN·优选"] and "domain_resolver" not in ob["d·v6"])
check("被藏的节点照旧指私有 DNS", ob["e·被藏"]["domain_resolver"] == {"server": "privdns"})
check("route.default_domain_resolver 不动（国内网站照旧用国内 DNS）",
      c["route"]["default_domain_resolver"] == TPL["route"]["default_domain_resolver"])
n = len(c["dns"]["servers"])
x._sb_node_dns(c, DOH)
check("再跑一次不重复加", len(c["dns"]["servers"]) == n)

print("二、没写自建 DNS 进订阅")
PRIV = {"tag": "privdns", "type": "https", "server": "dns.example.com", "server_port": 10443, "path": "/dns-query/xyabc123",
        "domain_resolver": "bootstrap", "detour": "🎯直连"}
c = cfg_with(OB, [PRIV])
x._sb_node_dns(c, "")
check("有私有解析那台：nodedns 换成跟它一样，节点指 nodedns",
      {k: v for k, v in srvmap(c)["nodedns"].items() if k != "tag"} == {k: v for k, v in PRIV.items() if k != "tag"}
      and obmap(c)["b·CDN·ws"]["domain_resolver"] == {"server": "nodedns"})
c = cfg_with(OB)
x._sb_node_dns(c, "")
check("什么都没有：nodedns 照模板（阿里），节点指它", srvmap(c)["nodedns"] == slot
      and obmap(c)["a·hy2"]["domain_resolver"] == {"server": "nodedns"})

print("三、自定义模板没这一行（老模板）")
c = cfg_noslot(OB)
x._sb_node_dns(c, DOH)
check("有自建 DNS：加一台 nodedns、节点指它", srvmap(c)["nodedns"]["server"] == "dns.example.com"
      and obmap(c)["a·hy2"]["domain_resolver"] == {"server": "nodedns"})
c = cfg_noslot(OB, [PRIV])
x._sb_node_dns(c, "")
check("只有私有解析：直接指 privdns、不另加", "nodedns" not in srvmap(c)
      and obmap(c)["b·CDN·ws"]["domain_resolver"] == {"server": "privdns"})
c = cfg_noslot(OB)
before = json.dumps(c)
x._sb_node_dns(c, "")
check("什么都没有：不动（照旧走默认 DNS）", json.dumps(c) == before)

src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
body = src[src.index("def build_singbox_sub("):src.index("\ndef ", src.index("def build_singbox_sub(") + 5)]
check("生成 sing-box 配置时在私有解析之后跑、带着自建 DNS 地址",
      0 < body.index("_sb_privdns(cfg, nodes)") < body.index("_sb_node_dns(cfg, _selfdns_doh())"))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

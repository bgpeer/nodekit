#!/usr/bin/env python3
"""小火箭：节点域名交给自己的 DNS —— [General] proxy-dns-server（仓库主人 10/31：「要不把这个字段你先写进去呗」）。
  取值跟 sing-box 的 _sb_node_dns 一样：本机自建 DoH 写进了订阅 → 它；否则私有解析的 DNS 机 → 它；都没有 → 不写。
  模板里自己写了 proxy-dns-server：换掉那一行，不重复写。被藏的节点照旧不放（还没在 iPhone 上测通）。"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("x", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

H = "https" + "://"
SELF = H + "dns.example.com:10443/dns-query/xyabc"
PRIV = H + "dns.other.example:10443/dns-query/xydef"
TPL = "[General]\nbypass-system = true\ndns-server = " + H + "doh.pub/dns-query\nipv6 = false\n\n[Proxy]\n"
P = tempfile.mktemp()
def run(tpl, url, nodes=()):
    open(P, "w").write(tpl)
    x._sr_node_dns(P, url, list(nodes))
    return open(P).read()

out = run(TPL, SELF)
check("有自建 DoH：dns-server 下面一行 proxy-dns-server = 它", ("dns-server = " + H + "doh.pub/dns-query\nproxy-dns-server = " + SELF + "\n") in out)
check("dns-server 原样不动", "dns-server = " + H + "doh.pub/dns-query\n" in out)
hid = [("k", {"server": "abc.internal", "_privdns": PRIV})]
check("没自建、有私有解析的 DNS 机：用它", "proxy-dns-server = " + PRIV + "\n" in run(TPL, "", hid))
check("两样都有：自建优先", "proxy-dns-server = " + SELF + "\n" in run(TPL, SELF, hid) and PRIV not in run(TPL, SELF, hid))
check("都没有：一个字不改", run(TPL, "") == TPL)
mine = TPL.replace("ipv6 = false\n", "ipv6 = false\nproxy-dns-server = system\n")
out = run(mine, SELF)
check("模板自己写了：换掉那一行、只一行", out.count("proxy-dns-server") == 1 and "proxy-dns-server = " + SELF in out)
check("再跑一次幂等", run(run(TPL, SELF), SELF) == run(TPL, SELF))
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("gen_shadow 调了它（在 _sr_selfdns 之后）",
      src.find("_sr_node_dns(SR_FILE, _selfdns_doh(), nodes)") > src.find("_sr_selfdns(SR_FILE, _selfdns_doh())") > 0)
check("被藏的节点照旧不放（还没在 iPhone 上测通）", 'if d.get("_privdns"):' in src and "hidden += 1" in src)
os.remove(P)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

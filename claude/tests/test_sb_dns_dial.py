#!/usr/bin/env python3
"""sing-box：我们往 dns.servers 里写的 DoH（自建 DNS 顶掉兜底那台 / 私有解析加的 privdns）不能让 sing-box 起不来。
  对着 sing-box 1.14.4 源码核过两条会让整份配置拒绝启动的规矩（10/28）：
  · DNS 服务器 detour 指向一个什么都没配的 direct 出站 → "detour to an empty direct outbound makes no sense"
    （common/dialer/detour.go；DNS 服务器启动时就 InitializeDetour，不是用到才报）
  · DNS 服务器地址是域名、又没 detour、又没 domain_resolver → "missing domain resolver for domain server address"
    （common/dialer/dialer.go，DNS 服务器的拨号器 DirectResolver=true）
  作者模板（🎯直连 是选择器、有 bootstrap）本来就没事；自定义模板里「直连」是个光秃秃的 direct、或者没有 bootstrap
  的，以前会写出起不来的配置。现在：空 direct 不当 detour（DNS 服务器不写 detour 本来就是直连）；解析器依次找
  bootstrap → 原来那台的 → route.default_domain_resolver；两样都没有就不写、说一行，配置照样能起。
"""
import os, io, sys, json, copy, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)

DOH = "https://dns.example/dns-query/xyabc123"
NAME = "abcdefghij.internal"
NODE = {"tag": "🇺🇸1 Reality", "type": "vless", "server": NAME, "server_port": 443,
        "uuid": "00000000-0000-0000-0000-000000000000"}
NODES = [("🇺🇸1 Reality", {"server": NAME, "_privdns": DOH})]

def would_start(cfg):
    """照 sing-box 1.14.4 的检查走一遍：起不来就返回原因，能起返回 ""。"""
    obs = {o["tag"]: o for o in cfg.get("outbounds", []) if isinstance(o, dict) and o.get("tag")}
    stags = {s.get("tag") for s in cfg["dns"]["servers"]}
    def rtag(r):
        return r.get("server") if isinstance(r, dict) else r
    for s in cfg["dns"]["servers"]:
        dr = rtag(s.get("domain_resolver"))
        if dr and dr not in stags:
            return f"{s['tag']}: domain resolver not found: {dr}"
        d = s.get("detour")
        if d:
            if d not in obs:
                return f"{s['tag']}: outbound detour not found: {d}"
            if obs[d].get("type") == "direct" and set(obs[d]) <= {"type", "tag"}:
                return f"{s['tag']}: detour to an empty direct outbound makes no sense"
        srv = str(s.get("server", ""))
        if s.get("type") in ("udp", "tcp", "tls", "https", "quic", "h3") and any(c.isalpha() for c in srv) \
                and ":" not in srv and not d and not dr:
            return f"{s['tag']}: missing domain resolver for domain server address"
    for o in obs.values():
        dr = rtag(o.get("domain_resolver"))
        if dr and dr not in stags:
            return f"{o['tag']}: domain resolver not found: {dr}"
    return ""

def run(cfg):
    """两样都写一遍，返回 (cfg, 屏上输出)。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        x._sb_selfdns(cfg, DOH)
        x._sb_privdns(cfg, NODES)
    return cfg, out.getvalue()

def dobs(cfg):
    """出站里的 dict（作者模板里还夹着节点占位串）。"""
    return [o for o in cfg["outbounds"] if isinstance(o, dict)]

def srv(cfg, tag):
    return next((s for s in cfg["dns"]["servers"] if s.get("tag") == tag), None)

print("一、作者模板：跟以前一模一样（🎯直连 + bootstrap）")
author = json.load(open(os.path.join(ROOT, "subbox-template.json"), encoding="utf-8"))
author["outbounds"].append(dict(NODE))
c, out = run(copy.deepcopy(author))
check(f"兜底那台换成自建 DoH，bootstrap 解析、走 🎯直连（{srv(c, 'final')}）",
      srv(c, "final") == {"tag": "final", "type": "https", "server": "dns.example", "path": "/dns-query/xyabc123",
                          "domain_resolver": "bootstrap", "detour": "🎯直连"})
check(f"私有解析加的那台同样写法（{srv(c, 'privdns')}）",
      srv(c, "privdns") == {"tag": "privdns", "type": "https", "server": "dns.example", "path": "/dns-query/xyabc123",
                            "domain_resolver": "bootstrap", "detour": "🎯直连"})
check("被藏节点的出站指向它", [o.get("domain_resolver") for o in dobs(c) if o.get("server") == NAME] == [{"server": "privdns"}])
check(f"能起（{would_start(c) or 'ok'}）、屏上没有 ⚠", not would_start(c) and "⚠" not in out)

def custom(direct=None, bootstrap=True, route_dr=None, final_dr=None):
    """自定义模板：直连出站 / bootstrap / route 默认解析器 / 原兜底那台的解析器，按需拼。"""
    obs = [{"tag": "proxy", "type": "selector", "outbounds": ["🇺🇸1 Reality"]}, dict(NODE)]
    if direct is not None:
        obs.insert(0, direct)
    servers = [{"tag": "local", "type": "https", "server": "1.0.0.1"}]
    if bootstrap:
        servers.insert(0, {"tag": "bootstrap", "type": "udp", "server": "1.0.0.1"})
    final = {"tag": "final", "type": "https", "server": "9.9.9.9", "detour": "proxy"}
    if final_dr:
        final["domain_resolver"] = final_dr
    servers.append(final)
    cfg = {"dns": {"servers": servers, "final": "final"}, "outbounds": obs, "route": {"rules": []}}
    if route_dr:
        cfg["route"]["default_domain_resolver"] = route_dr
    return cfg

print("二、自定义模板：「直连」是个什么都没配的 direct 出站")
c, out = run(custom(direct={"tag": "直连", "type": "direct"}))
check(f"能起（{would_start(c) or 'ok'}）", not would_start(c))
check("两台都不写 detour（不写就是直连，写了 sing-box 拒绝启动），bootstrap 解析",
      all("detour" not in srv(c, t) and srv(c, t).get("domain_resolver") == "bootstrap" for t in ("final", "privdns")))
check("被藏节点照样指过去、屏上没有 ⚠",
      [o.get("domain_resolver") for o in dobs(c) if o.get("server") == NAME] == [{"server": "privdns"}] and "⚠" not in out)

print("三、自定义模板：direct 出站配了东西（绑网卡之类）就不算空，照样当 detour")
c, out = run(custom(direct={"tag": "直连", "type": "direct", "bind_interface": "eth0"}))
check(f"能起（{would_start(c) or 'ok'}）、两台都走「直连」",
      not would_start(c) and all(srv(c, t).get("detour") == "直连" for t in ("final", "privdns")))

print("四、自定义模板：没有 bootstrap，用 route.default_domain_resolver 那台")
for rd in ("local", {"server": "local", "strategy": "ipv4_only"}):
    c, out = run(custom(direct={"tag": "直连", "type": "direct"}, bootstrap=False, route_dr=rd))
    check(f"{json.dumps(rd, ensure_ascii=False)}：能起（{would_start(c) or 'ok'}）、两台的解析器都是它",
          not would_start(c) and all(srv(c, t).get("domain_resolver") == rd for t in ("final", "privdns")))

print("五、自定义模板：没有 bootstrap，兜底那台原来的解析器（对象写法）原样留着")
fd = {"server": "local", "strategy": "ipv4_only"}
c, out = run(custom(direct={"tag": "直连", "type": "direct"}, bootstrap=False, final_dr=fd))
check(f"能起（{would_start(c) or 'ok'}）、兜底那台的解析器原样（{srv(c, 'final').get('domain_resolver')}）",
      not would_start(c) and srv(c, "final").get("domain_resolver") == fd)

print("六、自定义模板：没有直连出站、也没有任何解析器 → 不写，说一行，配置照样能起")
base = custom(bootstrap=False)
c, out = run(copy.deepcopy(base))
check(f"能起（{would_start(c) or 'ok'}）", not would_start(c))
check("兜底那台原样没动", srv(c, "final") == srv(base, "final"))
check("没加 privdns、被藏节点也没指过去",
      srv(c, "privdns") is None and not [o for o in dobs(c) if o.get("domain_resolver")])
check(f"屏上两行 ⚠ 说清楚（{out.strip()!r}）", out.count("⚠") == 2 and "自建 DNS" in out and "被藏的节点" in out)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

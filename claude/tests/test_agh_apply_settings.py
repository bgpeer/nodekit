#!/usr/bin/env python3
"""自建 DNS · 安装时调 AdGuard 接口自动设好「2 使用方法」的参数（仓库主人 11/18：
  截图里那几项不用进浏览器手动填）。域名 / 证书 / ClientID 一律从本机配置实时取，
  绝不写死——测试里只用 example.com / 127.0.0.1 / xy 开头的假 ID，不出现任何
  真实用户域名、IP、ClientID。假 AdGuard 按官方接口行为回包。
"""
import os, io, sys, json, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# 假 AdGuard：只认这几个接口，记下收到的 body；dns_info / access/list 先回完整配置。
GOT = {}
DNS_INFO = {"upstream_dns": ["https://dns.example/dns-query"], "bootstrap_dns": ["1.1.1.1"],
            "cache_size": 4096, "ratelimit": 20}
ACCESS = {"allowed_clients": ["127.0.0.1"], "disallowed_clients": [], "blocked_hosts": ["bad.example"]}
def fake_api(path, body=None, port=None, timeout=5, auth=None):
    if body is None:
        if path == "/control/dns_info":
            return 200, json.dumps(DNS_INFO)
        if path == "/control/access/list":
            return 200, json.dumps(ACCESS)
        return 404, ""
    GOT[path] = body
    if path == "/control/filtering/add_url" and body.get("url") == a.AGH_FILTER_EASYLIST_URL:
        return 200, "OK"
    return 200, "OK"
a._agh_api = fake_api
# 用户自己的配置：全部用假值，且每次改动都看函数是不是真的去读它们。
DOM = ["dns.example.com"]
a._domain = lambda: DOM[0]
a._crt = lambda: "/etc/ssl/agh/agh.crt"
a._key = lambda: "/etc/ssl/agh/agh.key"
a._cert_ready = lambda: True
a._cert_wildcard_ok = lambda dom: True
a._selfdns_clientid = lambda: "xy0123456789ab"
a._current_web_port = lambda: 4321

def run():
    GOT.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = a._agh_apply_settings("dns.example.com", "Passw0rd!x", 4321)
    return r, b.getvalue()

r, out = run()
check("全成：返回 True", r is True)
tls = GOT.get("/control/tls/configure", {})
check("加密：服务器名、证书路径都取自用户配置，不是写死的",
      tls.get("server_name") == "dns.example.com"
      and tls.get("certificate_path") == "/etc/ssl/agh/agh.crt"
      and tls.get("private_key_path") == "/etc/ssl/agh/agh.key"
      and tls.get("enabled") is True and tls.get("force_https") is False
      and tls.get("port_https") == 10443 and tls.get("port_dns_over_tls") == 853
      and tls.get("port_dns_over_quic") == 853)
dns = GOT.get("/control/dns_config", {})
check("DNS：上游 3 行、并行请求、缓存 67108864 / TTL 60 / 86400 / 乐观缓存",
      dns.get("upstream_dns") == list(a.AGH_UPSTREAM_DNS)
      and dns.get("upstream_mode") == "parallel" and dns.get("cache_enabled") is True
      and dns.get("cache_size") == 67108864 and dns.get("cache_ttl_min") == 60
      and dns.get("cache_ttl_max") == 86400 and dns.get("cache_optimistic") is True)
check("DNS：先读后写，别的字段没被清掉", dns.get("bootstrap_dns") == ["1.1.1.1"] and dns.get("ratelimit") == 20)
acc = GOT.get("/control/access/set", {})
check("允许的客户端：原有 IP 留着、ClientID 追加进去",
      acc.get("allowed_clients") == ["127.0.0.1", "xy0123456789ab"]
      and acc.get("blocked_hosts") == ["bad.example"])
flt = GOT.get("/control/filtering/add_url", {})
check("拦截列表：EasyList 地址", flt.get("url") == a.AGH_FILTER_EASYLIST_URL and flt.get("whitelist") is False)
check("屏上一行 ✔ 汇总，不打印域名以外的用户参数", "✔ 已自动设好" in out and "Passw0rd" not in out)

# 换一个域名 / 证书 / ClientID：函数必须跟着变，证明不是写死的。
DOM[0] = "dns2.example.net"
a._crt = lambda: "/etc/ssl/agh/other.crt"
a._selfdns_clientid = lambda: "xyfedcba987654"
r, out = run()
check("换域名 / 证书 / ClientID 后：TLS 体跟着变",
      GOT["/control/tls/configure"]["server_name"] == "dns2.example.net"
      and GOT["/control/tls/configure"]["certificate_path"] == "/etc/ssl/agh/other.crt"
      and GOT["/control/access/set"]["allowed_clients"] == ["127.0.0.1", "xyfedcba987654"])

# 没签泛域名证书：允许的客户端先不设（不然安卓专用 DNS 会被挡），其余照常。
a._cert_wildcard_ok = lambda dom: False
r, out = run()
check("没泛域名证书：不调 access/set、其余照常、返回 True",
      "/control/access/set" not in GOT and r is True and "先不设" in out)
a._cert_wildcard_ok = lambda dom: True

# 没域名 / 没证书：加密跳过，DNS 照常，返回 False（不算全成）。
DOM[0] = ""
a._cert_ready = lambda: False
r, out = run()
check("没域名 / 没证书：加密跳过、DNS 照常、返回 False",
      "/control/tls/configure" not in GOT and "/control/dns_config" in GOT and r is False)
DOM[0] = "dns.example.com"
a._cert_ready = lambda: True

# 某一步接口报错：只报一行 ⚠、其余继续，不抛异常。
def flaky(path, body=None, port=None, timeout=5, auth=None):
    if path == "/control/tls/configure":
        return 500, "boom"
    return fake_api(path, body, port, timeout, auth)
a._agh_api = flaky
r, out = run()
check("加密接口 500：报一行 ⚠、DNS / 拦截列表照常、返回 False",
      r is False and "加密没自动设好" in out and "/control/dns_config" in GOT
      and "/control/filtering/add_url" in GOT)
a._agh_api = fake_api

src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
# 注意：这里故意不把真实域名 / IP / ClientID 的任何片段写进断言——写出来本身就是泄漏。
# 只查：自动设置函数里域名、证书、ClientID 是不是真的从配置函数取的，且源码里没有
# 写死的自建 DNS 域名（形如 dnss. 开头的字面量）。
fn = src[src.index("def _agh_apply_settings("):src.index("def _first_setup():")]
check("源码里域名 / 证书 / ClientID 都从配置函数取、没有写死的自建 DNS 域名",
      all(x in fn for x in ("_domain()", "_crt()", "_key()", "_selfdns_clientid()"))
      and "dnss." not in src)
check("安装向导走成后调一次自动设置，失败不拦向导",
      "_agh_apply_settings(user, pw, port)" in src
      and src.index("_agh_apply_settings(user, pw, port)") > src.index('✔ 后台设好了'))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

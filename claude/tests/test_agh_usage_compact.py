#!/usr/bin/env python3
"""13 → 3 使用方法精简（仓库主人 10/30：「使用信息调整一下啰嗦的写一堆眼都看花了，该是调用参数和高亮语言还是保持」，
  照他给的写法重排）：第一步登录 → 第二步加密 → 第三步 DNS 设置（上游 3 行、并行请求、缓存、允许的客户端）→
  DNS 使用场景 ①DoH ②DoT ③明文 → 状态参数 / 当前配置 → 要填的就这几行。值都从这台机实时读、高亮；要复制的顶格单独一行。
"""
import os, io, re, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
H = "https" + "://"                         # 拼出来，claude/ 里不留网址字面量
IP = ".".join(["203", "0", "113", "9"])
DOM, CID = "dns.example.com", "xyabc123"
a._public_ip = lambda: IP
a._domain = lambda: DOM
a._current_web_port = lambda: 4972
a._doh_port = lambda: 10443
a._selfdns_clientid = lambda: CID
a._https_panel = lambda: (DOM, 10443)
a._cert_wildcard_ok = lambda d: True
a._crt = lambda: "/etc/ssl/sb/acme.crt"
a._key = lambda: "/etc/ssl/sb/acme.key"
a._installed = lambda: True
a._running = lambda: True
a._port_busy = lambda p: "AdGuardHome"
ALLOW = [None]
a._agh_users = lambda: [DOM]
a._allowed_clients = lambda: ALLOW[0]
def show():
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.status()
    return re.sub(r"\x1b\[[0-9;]*m", "", b.getvalue())
out = show()
lines = out.splitlines()
order = [out.index(k) for k in ("【第一步 · 登录网页后台】", "【第二步 · 设置加密】", "【第三步 · DNS 设置】",
                                 "【DNS 使用场景】", "AdGuard Home 状态参数", "要填的就这几行")]
check("顺序：一 登录 → 二 加密 → 三 DNS 设置 → 使用场景 → 状态参数 → 要填的几行", order == sorted(order))
check("第一步：加密后台地址、明文应急地址、80 改 3000", H + DOM + ":10443" in out and "http" + f"://{IP}:4972" in out
      and "80 改成 3000" in out)
check("第二步：服务器名称 / 证书 / 私钥 / 端口都是实时值", DOM in out and "/etc/ssl/sb/acme.crt" in out and "853" in out)
ups = [H + h + "/dns-query" for h in ("dns10.quad9.net", "dns.cloudflare.com", "dns.google")]
check("第三步：上游 3 行各自顶格一行（长按好复制）", all(u in lines for u in ups))
check("第三步：并行请求、缓存 67108864 / 60 / 86400、乐观缓存", "并行请求" in out and "67108864" in out
      and "86400" in out and "乐观缓存" in out)
check("没设白名单：允许的客户端框里只填 ID，ID 顶格一行", "框里只填这个 ID" in out and CID in lines)
check("使用场景：DoH 地址、DoT 主机名、明文 IP 各顶格一行",
      H + f"{DOM}:10443/dns-query/{CID}" in lines and f"{CID}.{DOM}" in lines and IP in lines)
check("状态参数在后面：已安装 / 运行 / 53 端口 / 域名 / ClientID",
      out.index("已安装") > out.index("【DNS 使用场景】") and "53 端口" in out and "ClientID" in out)
check("不再上屏的长篇：三种 DNS 怎么选、广告过滤那一大段", "三种 DNS 怎么选" not in out and "拦不掉" not in out)
check("状态参数里有后台用户名（仓库主人 10/30：忘了能查到）", f"后台用户名    : {DOM}" in out)
check("密码哪儿都不显示", "password" not in out.lower() and "密码" not in out.split("AdGuard Home 状态参数")[1])
check(f"比原来短（{len(lines)} 行）", len(lines) < 76)
ALLOW[0] = [CID]
out = show()
check("白名单已设：第三步一行 ✓、明文 53 会被挡一行提示", "✓ 已设好" in out and "明文 53 没法带 ID" in out)
L = out.splitlines()
check("白名单已设：ID 顶格单独一行、不带括号（仓库主人 10/31：复制时会把括号带进去）",
      CID in L and L[L.index(CID) - 1].rstrip().endswith("已设好") and f"（{CID}）" not in out)
a._cert_wildcard_ok = lambda d: False
out = show()
check("白名单已设、没签泛域名：红字一行说安卓会被挡", "安卓专用 DNS 会被挡" in out)

import tempfile
spec2 = importlib.util.spec_from_file_location("a2", os.path.join(ROOT, "adguard-dns.py"))
a2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(a2)
a2.AGH_DIR = tempfile.mkdtemp()
open(a2.AGH_DIR + "/AdGuardHome.yaml", "w").write(
    "http:\n  address: 0.0.0.0:3035\nusers:\n  - name: " + DOM + "\n    password: $2a$10$hash\nauth_attempts: 5\n")
check("从 AdGuardHome.yaml 读得出用户名（密码那行不碰）", a2._agh_users() == [DOM])
os.remove(a2.AGH_DIR + "/AdGuardHome.yaml")
check("没装 / 读不到：空", a2._agh_users() == [])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)


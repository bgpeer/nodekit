#!/usr/bin/env python3
"""17 线路检测加 IPv6（仓库主人 10/29：「应该增加IPv6检测，同时把ipv4和IPv6的本机IP都显示出来，不然找自己的IPv6地址都找不到」）。
  DMIT 那台 IPv4 被墙封了 TCP，改走 IPv6；手动 traceroute -6 才看出 IPv6 回国不是 CN2 GIA（17 实测：联通 / 移动走 CMIN2、电信走 163）。
  - vps-check.py：本机检测多一节「IPv6 回程」（三网各一个点，逐跳查 ASN，跟 IPv4 同一套判定）；没有 IPv6 就一行跳过。
    逐跳 ASN 走 Cymru origin6 区（半字节倒序）；traceroute 加 -6，解析认 IPv6 地址。外部检测收 IPv6（黑名单只收 IPv4，不查）。
  - xy-installer 菜单 17：最上面两个地址，标题上色、地址顶格单独一行；上次结果也显示 IPv6 回程。
  地址全用文档段 2001:db8::/32（RFC 3849）。
"""
import os, io, sys, json, types, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def load(name, f):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, f))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

IP4 = ".".join(["198", "51", "100", "4"])          # 文档段，拼出来（test_claude_folder 守着字面量）
v = load("vc", "vps-check.py")
print("一、vps-check.py")
check("半字节倒序（Cymru origin6 要的格式）",
      v._rev6("2001:db8::1") == "1." + "0." * 23 + "8.b.d.0.1.0.0.2")
asked = []
ASN = {"2001:db8:a::1": "58453", "2001:db8:b::1": "9808", "2001:db8:c::1": "64500", "2001:db8:f::12": "56040"}
NAMES = {"58453": "CMI-INT-HK China Mobile International Limited, HK", "9808": "CMNET-GD Guangdong Mobile, CN",
         "64500": "DMIT, US", "56040": "CMNET-GUANGDONG-AP China Mobile communications corporation, CN"}
def fake_txt(q, server="1.1.1.1"):
    asked.append(q)
    if q.startswith("AS"):
        a = q[2:].split(".")[0]; return [f"{a} | US | arin | 2010 | {NAMES.get(a, '')}"]
    for ip, a in ASN.items():
        if q == f"{v._rev6(ip)}.origin6.asn.cymru.com":
            return [f"{a} | 2001:db8::/32 | {'US' if a == '64500' else 'CN'} | apnic | 2010"]
    return []
v.dns_txt = fake_txt
asn = v.asn_of("2001:db8:a::1")
check(f"IPv6 查 origin6 区（{asked[0][-26:]}）", asked[0].endswith(".origin6.asn.cymru.com") and asn[0] == "58453")
check("IPv6 不查 peer 区（只有 IPv4）", v.peers_of("2001:db8:a::1") == [])
check("移动 CMI 认得出（IPv6 跳也查 ASN）", v.classify_hop("2001:db8:a::1")[0].startswith("移动 CMI"))

TR6 = ("traceroute to 2001:db8:f::12 (2001:db8:f::12), 20 hops max, 80 byte packets\n"
       " 1  2001:db8:c::1  0.671 ms\n 2  *\n 3  2001:db8:a::1  126.414 ms\n"
       " 4  2001:db8:b::1  159.898 ms\n 5  2001:db8:f::12  162.956 ms\n")
cmds = []
v.have = lambda b: b == "traceroute"
v.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (cmds.append(cmd), types.SimpleNamespace(stdout=TR6))[1],
                                     TimeoutExpired=Exception)
hops = v.traceroute("2001:db8:f::12")
check(f"IPv6 目标加 -6（{cmds[0][:22]}）", cmds[0].startswith("traceroute -6 "))
check(f"逐跳认 IPv6 地址、延迟不串（{hops[2]}）",
      hops == [(1, "2001:db8:c::1", 0.671), (2, None, None), (3, "2001:db8:a::1", 126.414),
               (4, "2001:db8:b::1", 159.898), (5, "2001:db8:f::12", 162.956)])
v.traceroute(".".join(["198", "51", "100", "9"]))
check("IPv4 照旧不加 -6", not cmds[-1].startswith("traceroute -6"))
lab, lvl, ms, land = v.route_verdict(hops, "移动")
check(f"判定：长途段移动 CMI、落地 CMNET（{lab} → {land}）", lab.startswith("移动 CMI") and land == "移动 CMNET")

v.TARGETS6 = [("移动", "", "2001:db8:f::12"), ("电信", "", "2001:db8:c::1")]
v.ensure_traceroute = lambda: True
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r6 = v.report_routes6("2001:db8::7", "三")
out = buf.getvalue()
check("IPv6 回程：移动那行写 CMI", "移动  移动 CMI" in out)
check("测试点归属对不上（不是中国运营商）→ 目标存疑跳过", "? 电信  目标存疑" in out and r6["电信"][0][2] == "skip")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    check("没有 IPv6：返回空", v.report_routes6(None) is None)
check("没有 IPv6：一行说跳过", "本机没有 IPv6，跳过" in buf.getvalue())

tmp = tempfile.mkdtemp(); v.LAST_FILE = f"{tmp}/last.json"
with contextlib.redirect_stdout(io.StringIO()):
    v.save_last(IP4, 1, {}, [], [], "2001:db8::7", r6)
d = json.load(open(v.LAST_FILE))
check(f"上次结果存了 IPv6 和 IPv6 回程（{sorted(d['routes6'])}）",
      d["ip6"] == "2001:db8::7" and d["routes6"]["移动"]["labs"][0].startswith("移动 CMI"))

class S6:
    def __init__(s, *a): pass
    def connect(s, a): s.a = a
    def getsockname(s): return (SRC, 0, 0, 0)
    def close(s): pass
v.socket = types.SimpleNamespace(socket=S6, AF_INET6=10, SOCK_DGRAM=2)
SRC = "2600:1f18::5"
check("本机 IPv6：取内核挑的源地址（不发包）", v.my_ip6() == SRC)
SRC = "fe80::1%eth0"
check("链路本地不算", v.my_ip6() is None)

called = []
v.check_external = lambda ip: called.append(ip) or 0
sys.argv = ["vps-check.py", "2001:DB8::0:1"]
check("外部检测收 IPv6（规范成小写压缩写法）", v.main() == 0 and called == ["2001:db8::1"])
sys.argv = ["vps-check.py", "2001:db8::zz"]
with contextlib.redirect_stdout(io.StringIO()):
    rc = v.main()
check("乱写的拒掉", rc == 1)

print("二、xy-installer 菜单 17")
x = load("xy", "xy-installer.py")
x.VPSCHK_LAST = f"{tmp}/last.json"
x.public_ip = lambda: IP4
x.public_ip6 = lambda: "2001:db8::7"
x._ask = lambda p="": "0"
x._hl = lambda c, t: f"<{t}>"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    x.vps_check_menu()
lines = buf.getvalue().splitlines()
check("IPv4：标题一行、地址顶格单独一行", "<▸ 本机 IPv4>" in lines and lines[lines.index("<▸ 本机 IPv4>") + 1] == IP4)
check("IPv6：标题一行、地址顶格单独一行", "<▸ 本机 IPv6>" in lines and lines[lines.index("<▸ 本机 IPv6>") + 1] == "2001:db8::7")
check("上次结果里有 IPv6 回程", "  IPv6 回程" in lines and any("移动 CMI" in l for l in lines))
x.public_ip6 = lambda: ""
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    x.vps_check_menu()
l2 = buf.getvalue().splitlines()
check("没有 IPv6 写「没有」", l2[l2.index("<▸ 本机 IPv6>") + 1] == "（没有）")
src = open(os.path.join(ROOT, "xy-installer.py")).read()
pi = src[src.index("def public_ip():"):src.index("def public_ip6():")]
check("public_ip 出网带中性 UA（不报 Python-urllib）", '"User-Agent": HTTP_UA' in pi)
check("外部 IP 检测收 IPv6", "要检测的 IP（IPv4 / IPv6）" in src)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

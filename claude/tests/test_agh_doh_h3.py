#!/usr/bin/env python3
"""自建 DNS · 11 DoH 走 HTTP/3 开关（仓库主人 11/01：「我的DOH支持H3吗？」→「做个开关吧默认关」）。
  AdGuardHome.yaml 里 dns.serve_http3：开了 DoH 端口多一个 UDP 收 HTTP/3。默认关；开要确认（默认否）；
  加密没开不让开；改完 AdGuard 没按预期起来（UDP 口没开 / 没关掉）就放回原样。
"""
import os, io, sys, re, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
Y = os.path.join(tmp, "AdGuardHome.yaml")
BASE = ("http:\n  address: 0.0.0.0:3000\n"
        "dns:\n  port: 53\n  serve_http3: false\n  use_http3_upstreams: false\n"
        "tls:\n  enabled: {enc}\n  server_name: dns.example.org\n  port_https: 10443\n")
def write(enc=True, h3="false"):
    open(Y, "w").write(BASE.format(enc="true" if enc else "false").replace("serve_http3: false", "serve_http3: " + h3))
a._agh_yaml = lambda: Y
a._installed = lambda: True
a._running = lambda: True
a.os.geteuid = lambda: 0
a.time.sleep = lambda s: None
SH = []
a.sh = lambda c, check=False: SH.append(c) or ""
UDP = {"on": False}
a._udp_listening = lambda p: UDP["on"]
def restart_hook(c):
    SH.append(c)
    if "restart AdGuardHome" in c:
        UDP["on"] = "serve_http3: true" in open(Y).read() and not BROKEN[0]
    return ""
BROKEN = [False]
a.sh = lambda c, check=False: restart_hook(c)

def run(keys):
    q = list(keys); SH.clear()
    a._ask = lambda p="": (print(p) or q.pop(0)) if q else ""
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.doh_h3_toggle()
    return re.sub(r"\033\[[0-9;]*m", "", b.getvalue())

print("一、默认关")
write(); UDP["on"] = False
check("没开过：读出来是关", a._h3_on() is False)

print("二、开")
out = run([""])
check("先一行提示（要放行 UDP 10443），回车 = 不开", "提示：" in out and "UDP 10443" in out and not a._h3_on() and SH == [])
out = run(["y"])
check("y 才开：yaml 改成 true、重启、✔ 一行", a._h3_on() and any("restart" in c for c in SH) and "✔ DoH 已支持 HTTP/3（UDP 10443）" in out)
check("dns 段别的键不动", "use_http3_upstreams: false" in open(Y).read() and "port: 53" in open(Y).read())

print("三、关")
out = run([])
check("再选：直接关、✔ 一行", not a._h3_on() and "✔ DoH 已关掉 HTTP/3" in out)

print("四、起不来放回")
write(); UDP["on"] = False; BROKEN[0] = True
out = run(["y"])
check("开了 UDP 没起来：放回关、⚠ 一行", not a._h3_on() and "已放回原样" in out)
BROKEN[0] = False

print("五、加密没开不让开")
write(enc=False)
out = run(["y"])
check("加密没开：提示先开加密，不改", "先在后台开加密" in out and not a._h3_on() and SH == [])

print("六、读真 /proc")
check("_udp_listening 能跑（不依赖 ss）", a.__dict__["_udp_listening"] is not None)
spec2 = importlib.util.spec_from_file_location("a2", os.path.join(ROOT, "adguard-dns.py"))
a2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(a2)
check("没人听的口返回 False", a2._udp_listening(1) is False)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

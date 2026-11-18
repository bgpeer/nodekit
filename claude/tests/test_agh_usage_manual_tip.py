#!/usr/bin/env python3
"""菜单 2「使用方法」：第二步上方加一行提示（仓库主人 11/18：第二、三步是网页端设置参数，
  如果没有被自动写入，可以自己登入网页端手动写入）。位置在第一步之后、第二步之前，只一行。
"""
import os, io, re, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("a", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
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
a._agh_users = lambda: [DOM]
a._allowed_clients = lambda: None
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a.status()
out = re.sub(r"\x1b\[[0-9;]*m", "", b.getvalue())
TIP = "第二、三步是网页端设置参数，如果没有被自动写入，可以自己登入网页端手动写入"
STEP1 = "【第一步 · 登录网页后台】"
STEP2 = "【第二步 · 设置加密】"
check("提示在屏上", TIP in out)
check("提示只出现一次", out.count(TIP) == 1)
check("位置：第一步之后、第二步之前",
      out.index(STEP1) < out.index(TIP) < out.index(STEP2))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

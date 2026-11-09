#!/usr/bin/env python3
"""vps-check.py --hops <目标>：目标原样拼进 traceroute 命令（shell=True），只收 IP / 普通主机名的字符（11/10 全仓检查）。
  面板 17 → 2 那条进来前验过是合法 IP；--hops 是文档里教人直接敲的（`curl … | python3 - --hops <IP>`），以前不验，
  带 ; $ ` 空格的参数会被 shell 当命令跑。"""
import os, io, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("vc", os.path.join(ROOT, "vps-check.py"))
v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
RAN = []
v.ensure_traceroute = lambda: True
v.traceroute = lambda ip, *a, **k: RAN.append(ip) or []
def hops(arg):
    RAN.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = v.show_hops(arg)
    return r, b.getvalue()
for arg in ("1.2.3.4; touch /tmp/x", "$(id)", "`id`", "1.2.3.4 -g 1", "-h", "a|b", ""):
    r, out = hops(arg)
    check(f"{arg!r}：不跑 traceroute，报不是合法的 IP / 主机名", r == 1 and not RAN and "不是合法的 IP / 主机名" in out)
for arg in ("1.2.3.4", "2001:db8::1", "node.example"):
    r, out = hops(arg)
    check(f"{arg}：照常跑", RAN == [arg])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

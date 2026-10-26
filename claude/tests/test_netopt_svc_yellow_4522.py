#!/usr/bin/env python3
"""4.5.22 —— 仓库主人 10/26：「都好了吗怎么有一排黄字」：自适应切换按设置关着，状况页却把
   net-optimize-adaptive-qos.service disabled/inactive 标成黄字。按设置该关的关着不标黄；设置是开却没跑、设置是关却在跑才标黄。"""
import os, io, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("n", os.path.join(ROOT, "net-optimize.py"))
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
class R:
    def __init__(s, rc, out): s.returncode, s.stdout = rc, out
def screen(act, want_on):
    n.run = lambda cmd, **k: R(0, "") if cmd[1] == "cat" else R(0, "disabled\n" if cmd[1] == "is-enabled" else act + "\n")
    o = io.StringIO()
    with contextlib.redirect_stdout(o):
        n.svc_state("x.service", want_on)
    return o.getvalue()
o = screen("inactive", False)
check(f"设置是关、确实关着：不标黄（{o.strip()}）", "\033[1;33m" not in o and "\033[33m" not in o and "关（按设置" in o)
check("设置是关却在跑：标黄", "设置是关，却在跑" in screen("active", False))
o = screen("inactive", True)
check("设置是开却没跑：照旧标黄", "active=inactive" in o)
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
check("状况页按 config 的 ADAPTIVE_DAEMON 决定该不该开", 'cfg.get("ADAPTIVE_DAEMON", "1") == "1"' in src)
print("ALL PASS" if not bad else f"{bad} FAIL"); sys.exit(1 if bad else 0)

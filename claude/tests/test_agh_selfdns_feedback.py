#!/usr/bin/env python3
"""自建 DNS 菜单 6「写入订阅」不再悄悄没反应 —— 仓库主人 10/20：选 6 屏上一个字没出就回了菜单、也没写进去。
  AdGuard 菜单这边自己再核一遍：主脚本退出码不是 0 → 红字一行；要写却没写进去 → 黄字一行；
  不管成没成，都停一下「按回车返回菜单」，结果不会被马上重印的菜单顶走。
"""
import os, sys, io, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ag", os.path.join(ROOT, "adguard-dns.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.BGP_DIR = tmp; m.SELFDNS_FLAG = f"{tmp}/selfdns.on"
def run(child):
    open(f"{tmp}/xy-installer.py", "w").write(child)
    asked = []; m._ask = lambda q="": asked.append(q) or ""
    if os.path.exists(m.SELFDNS_FLAG): os.remove(m.SELFDNS_FLAG)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m._selfdns_toggle()
    return buf.getvalue(), asked
ok_child = f'open("{m.SELFDNS_FLAG}", "w").write("1"); print("CHILD 已写入")'
o, a = run(ok_child)
check("写进去了：不多打警告，停一下等回车", "⚠" not in o and a == ["  按回车返回菜单…"])
o, a = run('pass')
check(f"退出码 0 却没写进去：黄字一行（{o.strip()}）", "自建 DNS 没写进订阅" in o and a)
o, a = run('import sys; sys.exit(3)')
check(f"主脚本出错：红字一行带退出码和直接跑的命令（{o.strip()}）", "退出码 3" in o and "selfdns-toggle" in o and a)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

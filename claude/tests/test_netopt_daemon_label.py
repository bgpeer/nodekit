#!/usr/bin/env python3
"""菜单 12 第 7 项「当前」写清效果 —— 仓库主人 10/26：「应该在后面写上 开 =阀值生效 / 关 =纯fq模式」；
   固定 cake / 纯暴力发包不跑守护，写明不生效。"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("x", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
L = x._netopt_daemon_label
check("1/2 档开：阈值生效", L("adaptive", True).startswith("开 = 阈值生效"))
check("1/2 档关：纯 fq", L("adaptive", False) == "关 = 纯 fq 模式")
check("固定 cake：写明不生效", "固定 cake 下不生效" in L("fixed_cake", True) and "固定 cake 下不生效" in L("fixed_cake", False))
check("纯暴力发包：写明不生效", "纯暴力发包下不生效" in L("fixed_burst", False))
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("菜单第 7 项用它", '(7, "自适应切换（守护进程）", _netopt_daemon_label(mode, d_on))' in src)
print("ALL PASS" if not bad else f"{bad} FAIL"); sys.exit(1 if bad else 0)

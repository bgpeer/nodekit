#!/usr/bin/env python3
"""Emby 域名那一问：带 *. 填进来的也认，自动去掉（仓库主人 11/11：「把带星号的也改成自动去掉吧」）。
  屏上提示写的是「做好一条 DNS 记录关黄云 *.新名字.根域名（比如 *.ys.根域名）」，紧接着问「新域名」，
  照着填成 *.ys.根域名 很自然；以前直接报「域名格式不对，没有改动」。安装时那一问同理。
"""
import os, re, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
N = m._emby_base_norm
OK = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")          # 菜单里用的那条格式检查
check("*.ys.example.org → ys.example.org，过得了格式检查", N("*.ys.example.org") == "ys.example.org" and OK.match(N("*.ys.example.org")))
check("大写、前后空格、结尾多一个点也归一", N("  *.YS.Example.org. ") == "ys.example.org")
check(".ys.example.org（只带了点）也去掉", N(".ys.example.org") == "ys.example.org")
check("*.emby.ys.example.org：星号去掉后照旧再去掉 emby.", N("*.emby.ys.example.org") == "ys.example.org")
check("以前就认的照旧：emby. / list. / home. 开头的去掉这一段", [N(p + ".ys.example.org") for p in ("emby", "list", "home")] == ["ys.example.org"] * 3)
check("正常填的不变", N("ys.example.org") == "ys.example.org" and N("example.org") == "example.org")
check("空的还是空的（回车取消）", N("") == "" and N(None) == "" and N("*.") == "")
src = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
i = src.index("# 填了 emby.example.com 这种就静默归一")
check("安装时那一问用的是同一个归一（带星号的也认）", "_emby_base_norm(cfg[\"domain\"])" in src[i:i + 400])
check("media-stack 1.5.400 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 400))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

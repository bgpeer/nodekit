#!/usr/bin/env python3
"""屏蔽中国域名 · 放行名单加「不放行」、默认就是不放行（仓库主人 10/30：「这个放行名单我还没点他他为什么就有作者名单，
  不应该是当前：没有放行名单吗？」→「菜单加一项不放行，默认就是不放行」）。
  以前开着、没选过名单的（当时默认作者名单）照旧作者名单——悄悄换成不放行，抖音 / 微信这些走节点就被拦了。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("cb", os.path.join(ROOT, "cn-block.py"))
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
cb.CNBLOCK_FILE = f"{tmp}/cnblock.json"; cb.BGP_DIR = tmp

print("一、默认")
check("没开过：默认不放行", cb.cnblock_load()["wl_mode"] == "none")
check("不放行：白名单一组都没有", cb._whitelist_tags(cb.cnblock_load()) == [])
json.dump({"enabled": True}, open(cb.CNBLOCK_FILE, "w"))
check("以前开着、没选过名单：照旧作者名单（不悄悄换）", cb.cnblock_load()["wl_mode"] == "author"
      and cb._whitelist_tags(cb.cnblock_load()) == list(cb.CN_WHITELIST))
json.dump({"enabled": True, "wl_mode": "custom", "wl_url": ""}, open(cb.CNBLOCK_FILE, "w"))
check("选过的照旧", cb.cnblock_load()["wl_mode"] == "custom")
json.dump({"enabled": False}, open(cb.CNBLOCK_FILE, "w"))
check("关着、没选过：不放行", cb.cnblock_load()["wl_mode"] == "none")

print("二、菜单")
os.remove(cb.CNBLOCK_FILE)
ANS = []
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"
cb.apply_cn_block = lambda cfg=None: True
def run(*a):
    ANS[:] = list(a)
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        cb.menu()
    return b.getvalue()
out = run("0")
check("首页：放行白名单 当前：不放行", "当前：不放行" in out or "不放行" in out.split("放行白名单", 1)[1].split("\n", 1)[0])
out = run("2", "0", "0")
check("2 里多了一项「3. 不放行」", "3. 不放行" in out and "1. 作者名单" in out and "2. 自定义名单" in out)
run("2", "1", "0")
check("选作者名单存下", json.load(open(cb.CNBLOCK_FILE))["wl_mode"] == "author")
out = run("2", "3", "0")
check("再选不放行：存下、屏上一行", json.load(open(cb.CNBLOCK_FILE))["wl_mode"] == "none" and "已设为 不放行" in out)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

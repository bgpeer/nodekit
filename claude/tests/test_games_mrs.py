#!/usr/bin/env python3
"""mihomo 模板 · 国际游戏规则改用 mrs（仓库主人 11/01：「国际游戏改成 mrs 的规则，我看一下是不是进程搞的鬼」）。
  原来的 yaml（classical）带 PROCESS-NAME com.riotgames.*，整个英雄联盟手游都进国际游戏；
  mrs 只有域名，不按进程分，用来查「起步校验要直连」是不是进程规则造成的。
"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
t = open(os.path.join(ROOT, "sub-template.yaml"), encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
line = next((l for l in t.splitlines() if l.strip().startswith("games-!cn:")), "")
check("games-!cn 用 mrs-domain", "*mrs-domain" in line and "*class-yaml" not in line)
check("地址是 category-games-!cn.mrs", line.rstrip().endswith('category-games-!cn.mrs"}'))
check("规则行、DNS 分流照旧引用 games-!cn", "RULE-SET,games-!cn,🎮国际游戏" in t and '"rule-set:games-!cn"' in t)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

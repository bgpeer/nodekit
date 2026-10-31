#!/usr/bin/env python3
"""17 VPS 线路检测「常用服务可达性」加上 Claude（仓库主人 10/31：「这里面加一个Claude啊」），排在 ChatGPT 后面。"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("v", os.path.join(ROOT, "vps-check.py"))
v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
names = [n for n, _ in v.SERVICES]
ok = ("Claude", "claude.ai") in v.SERVICES and names.index("Claude") == names.index("ChatGPT") + 1 \
     and names == ["Google", "YouTube", "ChatGPT", "Claude", "Netflix", "Disney+", "GitHub"]
print(("  ✔ " if ok else "  ✖ ") + f"可达性列表：{'、'.join(names)}")
print("\nALL PASS" if ok else "\nFAIL 1"); sys.exit(0 if ok else 1)

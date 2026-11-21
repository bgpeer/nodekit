#!/usr/bin/env python3
"""README 引用的图片都在、是真图：README 里「执行后的面板效果图」挂的是 assets/panel.jpg，
  图一删 / 一换名，README 上就剩个裂图。管的是引用不断链，不审图里的内容。
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
check("README 挂了管理面板效果图", "![管理面板](assets/panel.jpg)" in readme)
p = os.path.join(ROOT, "assets", "panel.jpg")
check("assets/panel.jpg 在", os.path.exists(p))
data = open(p, "rb").read() if os.path.exists(p) else b""
check("panel.jpg 是真 JPEG（不是空文件 / 错拷的文本）",
      data[:3] == b"\xff\xd8\xff" and len(data) > 10000)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

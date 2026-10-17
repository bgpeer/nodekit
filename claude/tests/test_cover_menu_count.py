#!/usr/bin/env python3
"""「截封面」那一项上次截成几张也写出来（1.5.383）。
仓库主人 10/17：「上次 28 MB……他显示了 28 为什么没有图」—— 那 28 MB 是三部都没截成白拉的流量，
屏上只写流量，看着像截成了。改成「上次：截成 0/3 · 28 MB」，一张没成用黄色。
"""
import importlib.util, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
m.cover_running = lambda mp: None
m.heal_mount_on = lambda mp: True
m.heal_auto_on = lambda: True
m.meta_auto_of = lambda mp: ""
LAST = {"v": {"t": 1, "n": 0, "of": 3, "mb": 28.4}}
m.cover_manual_last = lambda mp: LAST["v"]
def row():
    it = []
    m._drive_tail_items("D", "/quark", lambda *x: it.append(x))
    return m.ANSI_RE.sub("", it[0][1]), it[0][1]
plain, raw = row()
check("一张没成：截成 0/3 · 28 MB", plain == "上次：截成 0/3 · 28 MB")
check("一张没成用黄色", m.YELLOW in raw)
LAST["v"] = {"t": 1, "n": 2, "of": 3, "mb": 9}
plain, raw = row()
check("截成了：2/3、绿色", plain == "上次：截成 2/3 · 9 MB" and m.GREEN in raw)
LAST["v"] = None
check("没截过：照旧", row()[0] == "没图的全部截一次")
check("版本 1.5.383 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 383))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

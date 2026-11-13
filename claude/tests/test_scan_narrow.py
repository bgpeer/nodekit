#!/usr/bin/env python3
"""已经在扫整个盘，再挑它底下的目录 = 缩小范围（仓库主人 11/13：「怎么回事？这是添加上了还是没加上怎么还是整个盘」）。

  扫码挂上后扫描路径自动填成整个盘（1.5.416），屏上提示「要缩小范围选 2 修改扫描路径」。可他进来挑了 /quark/夸克挂载，
  得到一行灰字「已经包含在别的扫描路径里，没有加」，路径还是整个盘 —— 以前这里只会往大里并，没有往小里收的路。
钉住：
  一、挑的目录落在已有的大路径底下：一行提示说清从什么改成什么，确认默认否；确认了就把大的换成挑的
  二、答否：什么都不动
  三、一次挑几个子目录：一起换上；别的盘的路径不受影响
  四、原来的规矩照旧：加大的会吃掉小的；挑的本来就有 → 说没加；挑整个盘还是先警告再问"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SPEC, APPLIED, YN, PICK, ANS, YES = [[]], [], [], [[]], [], [True]
m.explicit_scan_paths = lambda: list(SPEC[0])
m.save_ms_state = lambda *a, **k: SPEC.__setitem__(0, k.get("scan_spec", SPEC[0]))
m._apply_scan_paths = lambda d, why="": APPLIED.append(why)
m.auto_rest_on = lambda: False
m._pick_dirs = lambda d, mp, prompt="要扫哪个": list(PICK[0])
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else "0"
m.ask_yn = lambda q, default=True: (YN.append((q, default)), YES[0])[1]
def run(spec, pick, yes=True):
    SPEC[0], PICK[0], YES[0] = list(spec), list(pick), yes
    APPLIED.clear(); YN.clear(); ANS[:] = ["1", "0"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m._drive_paths_menu("/d", "/quark")
    return plain(buf.getvalue())

print("一、在整个盘的基础上挑一个子目录")
out = run(["/quark"], ["/quark/夸克挂载"])
check("一行提示说清：现在扫什么、改成只扫什么、别的目录不再进 Emby", out.count("提示：") == 1 and "现在扫的是 /quark" in out and "改成只扫 /quark/夸克挂载" in out)
check("确认默认否", YN == [("确认缩小范围？", False)])
check("确认了：整个盘换成挑的那个目录，当场应用", SPEC[0] == ["/quark/夸克挂载"] and len(APPLIED) == 1 and "改成只扫 /quark/夸克挂载" in APPLIED[0])
check("不再出现「已经包含在别的扫描路径里，没有加」", "没有加" not in out)

print("二、答否")
out = run(["/quark"], ["/quark/夸克挂载"], yes=False)
check("什么都不动，屏上说没有改动", SPEC[0] == ["/quark"] and not APPLIED and "没有改动" in out)

print("三、一次挑几个 / 别的盘")
out = run(["/aliyun/电影", "/quark"], ["/quark/夸克挂载/动漫", "/quark/夸克挂载/电影"])
check("一次挑两个子目录：一起换上；阿里盘的路径原样", sorted(SPEC[0]) == ["/aliyun/电影", "/quark/夸克挂载/动漫", "/quark/夸克挂载/电影"])
out = run(["/quark/夸克挂载"], ["/quark/夸克挂载/动漫"])
check("从一个目录再往下收一层也行", SPEC[0] == ["/quark/夸克挂载/动漫"])

print("四、原来的规矩照旧")
out = run(["/quark/夸克挂载/动漫"], ["/quark/夸克挂载"])
check("加大的：小的被吃掉，不问缩小", SPEC[0] == ["/quark/夸克挂载"] and not YN and "去掉 /quark/夸克挂载/动漫" in out)
out = run(["/quark/夸克挂载"], ["/quark/夸克挂载"])
check("挑的本来就有：说没加", SPEC[0] == ["/quark/夸克挂载"] and "没有加" in out and not APPLIED)
out = run(["/quark/夸克挂载"], ["/quark/备份"])
check("挑的是旁边另一个目录：照旧是追加", sorted(SPEC[0]) == ["/quark/夸克挂载", "/quark/备份"] or set(SPEC[0]) == {"/quark/夸克挂载", "/quark/备份"})
out = run(["/quark/夸克挂载"], ["/quark"], yes=False)
check("挑整个盘：照旧先警告再问（默认否），答否不动", "整个盘会把手机备份" in out and YN == [("仍然要整个盘？", False)] and SPEC[0] == ["/quark/夸克挂载"])
out = run(["/quark", "/quark2/x"], ["/quark/a", "/aliyun/b"])
check("挑的里面有一条不在那条大路径底下：不走缩小，照旧合并", not any(q == "确认缩小范围？" for q, _ in YN))
check("media-stack 1.5.421 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 421))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

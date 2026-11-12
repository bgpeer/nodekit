#!/usr/bin/env python3
"""扫码挂盘不再问「挂到哪个路径」，挂上后扫描路径直接填成整个盘（仓库主人 11/12 全新重装：「这个扫马登入直接就扫码登不要
  叫别人填什么路径，当你扫码成功的时候你直接把路径什么的填好，扫完码给个提示：当前是全盘/quark所有路径，如果需要路径缩小
  请选2. 修改扫描路径」）。

  真机上他在「挂到哪个路径」那一问里填的是网盘里的文件夹 —— 「挂到哪」和「扫哪个目录」是两件事，第一次用的人分不清。
钉住：
  一、默认路径没被占：不问，直接用默认的
  二、默认路径已经有盘了（第二个同类的盘）：才问，输错 / 撞了照旧拦
  三、挂上之后：扫描路径自动加上整个盘、应用到 AutoFilm；一行提示说清现在扫的是什么、去哪缩小
  四、115 / 阿里 / 夸克 TV 三条扫码路都这样"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
MOUNTS, ASKED, ANS = [], [], []
m.openlist_storages = lambda d: [(mp, "x", "work", "", "") for mp in MOUNTS]
m.ask = lambda prompt, default="": (ASKED.append(prompt), ANS.pop(0) if ANS else "")[1]
def run(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = fn()
    return r, plain(buf.getvalue())

print("一、默认路径没被占：不问")
r, out = run(lambda: m.ask_mount("/d", "/quark"))
check("直接用默认的 /quark，一句都不问", r == "/quark" and not ASKED)
MOUNTS[:] = ["/aliyun"]
r, out = run(lambda: m.ask_mount("/d", "/quark"))
check("别的盘挂着不相干：照样不问", r == "/quark" and not ASKED)

print("二、默认路径已经有盘了：才问")
MOUNTS[:] = ["/quark"]; ANS[:] = ["/quark2"]
r, out = run(lambda: m.ask_mount("/d", "/quark"))
check("第二个夸克盘：说清 /quark 已经有盘了，问这一个挂到哪", r == "/quark2" and len(ASKED) == 1 and "/quark 已经有盘了" in ASKED[0])
ASKED.clear(); ANS[:] = [""]
r, out = run(lambda: m.ask_mount("/d", "/quark"))
check("回车（还是默认的那个）：撞了，拦住", r == "" and "撞了" in out)
ANS[:] = ["/a b"]
r, out = run(lambda: m.ask_mount("/d", "/quark"))
check("路径带空格：照旧拦", r == "" and "不能" in out)

print("三、挂上之后扫描路径自动填成整个盘")
SPEC, SAVED, APPLIED = [["/aliyun/电影"]], [], []
m.explicit_scan_paths = lambda: list(SPEC[0])
m.save_ms_state = lambda *a, **k: (SAVED.append(k), SPEC.__setitem__(0, k.get("scan_spec", SPEC[0])))[0]
m._apply_scan_paths = lambda d, why="": APPLIED.append(why)
r, out = run(lambda: m.scan_whole_mount("/d", "/quark"))
check("扫描路径加上整个盘，原来别的盘的路径还在", SPEC[0] == ["/aliyun/电影", "/quark"] or sorted(SPEC[0]) == ["/aliyun/电影", "/quark"])
check("当场应用到 AutoFilm（不用再去点一次）", len(APPLIED) == 1 and "/quark" in APPLIED[0])
check("一行提示：现在扫整个盘、会带进什么、去哪缩小", out.count("提示：") == 1 and "当前扫整个盘 /quark" in out and "「2 修改扫描路径」" in out and "手机备份" in out)
n = len(SAVED)
r, out = run(lambda: m.scan_whole_mount("/d", "/quark"))
check("已经在扫整个盘了：不重复加、不重启 AutoFilm，提示照给", len(SAVED) == n and len(APPLIED) == 1 and "当前扫整个盘" in out)
m._apply_scan_paths = lambda d, why="": (_ for _ in ()).throw(RuntimeError("no docker"))
SPEC[0] = []
r, out = run(lambda: m.scan_whole_mount("/d", "/115"))
check("AutoFilm 配置没写成：扫描路径照记、说清「4 生成媒体库」时会再写，不崩", SPEC[0] == ["/115"] and "会再写" in out)

print("四、三条扫码路都接上了")
check("115 / 阿里 / 夸克 TV 挂上后都调 scan_whole_mount", SRC.count("scan_whole_mount(d, mount)") >= 3)
check("不再留「接下来在 2 修改扫描路径里加上」那句（现在自动加了）", "接下来在「2 修改扫描路径」里加上要进 Emby 的目录" not in SRC)
check("三条路的挂载点默认值：/115、/aliyun、夸克的", all(t in SRC for t in ("ask_mount(d, MOUNT_115)", "ask_mount(d, MOUNT_ALI)", "ask_mount(d, MOUNT_QTV)")))
check("media-stack 1.5.416 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 416))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

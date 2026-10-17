#!/usr/bin/env python3
"""撤掉的盘留在 Emby 里的条目（1.5.376）。
真机 10/11：七米蓝「删除所有路径」、本地 strm 也删了，Emby 里还挂着两千七百多个点不开的条目 ——
每小时那道刹车（空壳超过两成不动）一个不清，删的那一屏却写着「每小时的对齐任务也会做」。
钉住：
  一、只认「本地主目录整个没了 + 不在扫描范围」的；还在扫的、主目录还在的不算
  二、删盘那一下当场告诉 Emby（已经确认过，不再问）；确认那句写清 Emby 条目会跟着清
  三、进「3 挂载路径」问一句（醒目一行、默认否）；一次运行只问一回
  四、每小时那道刹车照旧（prune_emby_ghosts 没动）
"""
import contextlib, importlib.util, io, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def quiet(fn, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = fn(*a, **k)
    return r, m.ANSI_RE.sub("", b.getvalue())

D = tempfile.mkdtemp()
m.strm_root = lambda d: os.path.join(D, "strm")
root = os.path.join(D, "strm", m.STRM_SUBDIR)
os.makedirs(os.path.join(root, "quark", "电影"))
open(os.path.join(root, "quark", "电影", "a.strm"), "w").write("/quark/电影/a.mkv")
P = lambda top, n: m.STRM_PATH + f"/{top}/电影/{n}.strm"
SCAN = ["/quark/夸克挂载", "/115/115挂载"]
m.effective_scan_paths = lambda d: list(SCAN)
GH = [("1", "甲", P("七米蓝影视", "甲")), ("2", "乙", P("七米蓝影视", "乙")),
      ("3", "丙", P("115", "丙")),            # 115 还在扫、本地主目录没了：不算（可能还没生成回来）
      ("4", "丁", P("quark", "丁"))]          # 主目录还在：不算
print("一、哪些算撤掉的")
r = m.retired_ghosts("D", GH)
check("只有七米蓝那两条", [g[0] for g in r] == ["1", "2"])
check("没有空壳 → 空", m.retired_ghosts("D", []) == [] and m.retired_ghosts("D", None) == [])

print("三、进「3 挂载路径」问一句")
LIVE = {"g": list(GH)}
m.emby_ghost_items = lambda d, key: list(LIVE["g"])
SENT = []
MODE = {"batch": True}
BYID = {}
def emby(path, key, method="GET", timeout=30, body=None):
    if path.startswith("/Items/Delete?Ids=") and method == "POST":
        if not MODE["batch"]:
            raise OSError("404")
        ids = path.split("=", 1)[1].split(",")
    elif path.startswith("/Items/") and method == "DELETE":
        ids = [path.rsplit("/", 1)[1]]
    else:
        raise AssertionError(path)
    # 跟真机一样：所在文件夹不在就 404「Could not find a part of the path」（1.5.379）
    for g in LIVE["g"]:
        if g[0] in ids and not os.path.isdir(os.path.dirname(m._strm_host_path("D", g[2]))):
            raise OSError("404 Could not find a part of the path")
    SENT.append(ids); LIVE["g"] = [g for g in LIVE["g"] if g[0] not in ids]; return {}
m._emby = emby
m.emby_notify_changes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("别走每小时那条的函数"))
m.scan_log_line = lambda *a, **k: None
YN = []
m.ask_yn = lambda q, dflt=None, *a, **k: YN.append((q, dflt)) or False
n, out = quiet(m.clear_retired_ghosts, "D", "K")
check("醒目一行说清是哪个盘、几个", n == 0 and "七米蓝影视" in out and "2 个" in out)
check("默认否、选否不动", YN == [("从 Emby 里清掉？", False)] and SENT == [])
check("没有 key 不做", m.clear_retired_ghosts("D", "") == 0)
m.ask_yn = lambda *a, **k: True
n, out = quiet(m.clear_retired_ghosts, "D", "K")
check("确认了：只删七米蓝那两条（按 id 成批删，1.5.378）", n == 2 and SENT == [["1", "2"]])
check("115 / 夸克那两条没碰", len(LIVE["g"]) == 2)
check("临时建回的空文件夹删完拿掉", not os.path.exists(os.path.join(root, "七米蓝影视")))
check("边删边报数", "正在从 Emby 删：2/2" in out and "Emby 里 2 个条目已清掉" in out)
LIVE["g"] = [(str(i), "x", P("七米蓝影视", f"x{i}")) for i in range(120)]
SENT.clear()
n, out = quiet(m.clear_retired_ghosts, "D", "K", confirm=False)
check("120 条分三批删完", [len(x) for x in SENT] == [50, 50, 20] and n == 120)
LIVE["g"] = [("a", "x", P("七米蓝影视", "a")), ("b", "x", P("七米蓝影视", "b"))]
SENT.clear(); MODE["batch"] = False
n, out = quiet(m.clear_retired_ghosts, "D", "K", confirm=False)
check("成批删不认：一条条 DELETE", SENT == [["a"], ["b"]] and n == 2)
# 删之前再看一眼：主目录又出现了（重新加了路径生成回来）→ 一个不删
LIVE["g"] = [("c", "x", P("七米蓝影视", "c"))]
m.retired_ghosts = (lambda f: (lambda d, g: list(g or [])))(m.retired_ghosts)
os.makedirs(os.path.join(root, "七米蓝影视", "电影"))
open(os.path.join(root, "七米蓝影视", "电影", "新.strm"), "w").write("/七米蓝影视/电影/新.mkv")
SENT.clear()
n, out = quiet(m.clear_retired_ghosts, "D", "K", confirm=False)
check("主目录本地又有了：一个不删、那棵树不动", SENT == [] and "还有 1 个没删掉" in out
      and os.path.exists(os.path.join(root, "七米蓝影视", "电影", "新.strm")))
src = open(SRC, encoding="utf-8").read()
mm = src[src.index("def mount_paths_menu"):]; mm = mm[:mm.index("\ndef ", 1)]
check("挂载路径一进来问，一次运行只问一回", "_RETIRED_ASKED[0] = True" in mm
      and mm.index("clear_retired_ghosts(d, read_emby_api_key(d))") < mm.index("while True"))

print("二、删盘那一下")
dr = src[src.index("def drop_orphan_strm_dirs"):]; dr = dr[:dr.index("\ndef ", 1)]
check("确认那句写清 Emby 条目会跟着清", "Emby 里这些片（连同观看记录）会跟着清掉" in dr)
check("删完当场清、不再问", "clear_retired_ghosts(d, read_emby_api_key(d), confirm=False)" in dr
      and dr.index("shutil.rmtree") < dr.index("clear_retired_ghosts"))
check("屏上那句「要等它扫一次才会消失」没了", "要等它扫一次才会消失" not in dr)

print("四、每小时那道刹车照旧")
pg = src[src.index("def prune_emby_ghosts"):]; pg = pg[:pg.index("\ndef ", 1)]
check("prune_emby_ghosts 不调 clear_retired_ghosts、刹车还在", "clear_retired_ghosts" not in pg and "GHOST_MAX_PCT" in pg)
check("版本 1.5.379 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 379))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

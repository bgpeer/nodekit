#!/usr/bin/env python3
"""一次加几百上千个：只报新文件夹、不扫整个库（1.5.380）。
仓库主人：「删除可以这么快，添加也可以这么快吗」→「做」。以前「4 生成媒体库」变动超过 300 条就退回
「扫描整个媒体库」—— 慢，还把别的片的音视频轨冲掉。
钉住：
  一、bulk_roots：整个新文件夹只报最上面那层；已有文件夹里新加的照报文件；全新的盘报盘那一层
  二、emby_notify_bulk：一次报完、边等边报收进几个；等齐就停；等不齐说清、不扫整个库
  三、strm_finish：超过 300 条走它，不走 emby_scan_wait；少的照旧 emby_notify_changes；每小时那条函数没动
"""
import contextlib, importlib.util, io, os, sys
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
S = m.STRM_PATH
print("一、bulk_roots")
known = [f"{S}/quark/电影/A/a.strm", f"{S}/quark/剧/B/S01E01.strm"]
created = [f"{S}/quark/电影/新片1/x.strm", f"{S}/quark/电影/新片1/y.strm",
           f"{S}/quark/剧/B/S01E02.strm",
           f"{S}/xiaoya/电影/4K/甲/a.strm", f"{S}/xiaoya/纪录片/乙/b.strm"]
r = m.bulk_roots(created, known)
check("新文件夹只报最上面那层", f"{S}/quark/电影/新片1" in r and not any(x.endswith("x.strm") for x in r))
check("已有文件夹里新加的照报文件", f"{S}/quark/剧/B/S01E02.strm" in r)
check("全新的盘报盘那一层", f"{S}/xiaoya" in r and not any(x.startswith(f"{S}/xiaoya/") for x in r))
check("一共 3 条", len(r) == 3)
check("什么都没有时：全新的盘", m.bulk_roots([f"{S}/a/b/c.strm"], []) == [f"{S}/a"])

print("二、emby_notify_bulk")
POST, HAVE = [], {"n": 0}
def emby(path, key, method="GET", timeout=30, body=None):
    assert path == "/Library/Media/Updated" and method == "POST"
    POST.append(body["Updates"]); return {}
m._emby = emby
m.scan_log_line = lambda *a, **k: None
m.time.sleep = lambda s: None
cre = [f"{S}/xiaoya/电影/{i}/a.strm" for i in range(1000)]
seq = iter([set(cre[:300]), set(cre[:800]), set(cre)])
m.emby_strm_paths = lambda key: next(seq)
r, out = quiet(m.emby_notify_bulk, "K", cre, [f"{S}/old/x.strm"], known)
check("一次报：1 个新文件夹 + 1 个删除", POST == [[{"Path": f"{S}/xiaoya", "UpdateType": "Created"},
                                                {"Path": f"{S}/old/x.strm", "UpdateType": "Deleted"}]])
check("边等边报数、等齐就停", r is True and "Emby 已收进 800/1000" in out and "Emby 已收进 1000 个" in out)
m.emby_strm_paths = lambda key: set(cre[:10])
POST.clear()
r, out = quiet(m.emby_notify_bulk, "K", cre, [], known, wait_s=0.01)
check("等不齐：说清它在后台接着收", r is False and "后台接着收" in out)
m.emby_scan_wait = lambda *a, **k: (_ for _ in ()).throw(AssertionError("扫整个库了"))
check("等不齐也不扫整个库", True)

print("三、strm_finish")
src = open(SRC, encoding="utf-8").read()
fs = src[src.index("def strm_finish"):]; fs = fs[:fs.index("\ndef ", 1)]
check("超过 300 条走 emby_notify_bulk、排在 emby_notify_changes 前面",
      "if len(diff) > EMBY_TARGETED_MAX:" in fs and fs.index("emby_notify_bulk(") < fs.index("emby_notify_changes(key, diff, nudge=True)")
      < fs.index("emby_scan_wait("))
nc = src[src.index("def emby_notify_changes"):]; nc = nc[:nc.index("\ndef ", 1)]
check("每小时那条用的 emby_notify_changes 没动（还是超过 300 就不接）", "len(changes) > EMBY_TARGETED_MAX" in nc
      and "emby_notify_bulk" not in nc)
check("版本 1.5.380 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 380))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

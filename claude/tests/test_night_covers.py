#!/usr/bin/env python3
"""凌晨自动对齐收进来的新片也排「没刮到图的截一帧」（1.5.381）。
仓库主人 10/17：「凌晨自动更新的片没刮削他没有设置自动截图」—— 以前只有手点「4 生成媒体库」才排。
钉住：
  一、do_sync 那条（covers=True）：对上之后把这一轮新加的排进去
  二、头一回（没有上次的记账）不排；一夜几千部（整个盘）不排；没对上不排
  三、每小时那条（scan_if_grown）不传 covers，白天看片的路径不添新动作
"""
import importlib.util, json, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
D = tempfile.mkdtemp()
S = m.STRM_PATH
CUR = {"v": []}
m.strm_inventory = lambda d, only=None: [(p, "/x") for p in CUR["v"]]
m._strm_container_path = lambda d, hp: hp
m.strm_count = lambda d, only=None: len(CUR["v"])
HAVE = {"v": set()}
m.emby_strm_paths = lambda key: set(HAVE["v"])
m.emby_notify_changes = lambda *a, **k: True
m.emby_scan_wait = lambda *a, **k: True
Q = []
m.queue_new_covers = lambda paths: Q.append(list(paths)) or len(paths)
def seen(v):
    json.dump(sorted(v), open(os.path.join(D, m.EMBY_SEEN_FILE), "w"))
old = [f"{S}/115/AV/{i}.strm" for i in range(3)]
new = [f"{S}/115/AV/FC2-{i}.strm" for i in range(4)]
print("一、凌晨那条")
seen(old); CUR["v"] = old + new; HAVE["v"] = set(old)
m.emby_sync_changes(D, "K", covers=True)
check("新加的 4 部排进截图队列", Q == [sorted(new)])
Q.clear(); CUR["v"] = old + new
m.emby_sync_changes(D, "K", covers=True)
check("第二晚没新片：不排", Q == [])
print("二、不该排的")
Q.clear()
os.remove(os.path.join(D, m.EMBY_SEEN_FILE)); HAVE["v"] = set(old)
m.emby_sync_changes(D, "K", covers=True)
check("头一回（没记账）不排", Q == [])
big = [f"{S}/xiaoya/{i}.strm" for i in range(m.COVER_NEW_NIGHT_MAX + 1)]
Q.clear()
seen(old); CUR["v"] = old + big; HAVE["v"] = set(old)
m.emby_sync_changes(D, "K", covers=True)
check("一夜进来一整个盘：不排", Q == [])
Q.clear()
seen(old); CUR["v"] = old + new; HAVE["v"] = set(old)
m.emby_notify_changes = lambda *a, **k: False
m.emby_scan_wait = lambda *a, **k: False
m.emby_sync_changes(D, "K", covers=True)
check("没对上：不排", Q == [])
Q.clear()
seen(old); CUR["v"] = old + new; HAVE["v"] = set(old)
m.emby_notify_changes = lambda *a, **k: True
m.emby_sync_changes(D, "K")
check("不传 covers：不排", Q == [])
print("三、每小时那条")
src = open(SRC, encoding="utf-8").read()
sg = src[src.index("def scan_if_grown"):]; sg = sg[:sg.index("\ndef ", 1)]
ds = src[src.index("def do_sync"):]; ds = ds[:ds.index("\ndef ", 1)]
check("scan_if_grown 不传 covers", "emby_sync_changes(d, key, force=force)" in sg and "covers" not in sg)
check("do_sync 传 covers=True", 'emby_sync_changes(d, key, label="每日对齐的扫描", covers=True)' in ds)
check("版本 1.5.381 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 381))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

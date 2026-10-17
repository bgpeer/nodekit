#!/usr/bin/env python3
"""115 的片截封面：按播放器的脸向 OpenList 要直链（1.5.382）。
真机 10/17：三部 FC2（115）截封面全没成 —— 两部「Input/output error」（拉了十几 MB 就断）、一部「读不出时长」。
/d/ 那条 302 出去的 115 直链是按 OpenList 那张脸签的，ffmpeg 拿播放器的脸去读就被掐。播放那边早就是
pan115_url 按播放器的脸要直链，截封面没跟上。
钉住：
  一、115 盘：先走 pan115_url(播放器的脸) 拿到的直链、只用播放器的脸截（不再换浏览器的脸白等）
  二、那条拿不到：照旧退回 /d/，两张脸都试
  三、不是 115 的盘：不去要 115 直链
"""
import contextlib, importlib.util, io, os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

D = tempfile.mkdtemp()
TP = {"v": "/115/115挂载/AV影片/FC2-1.mp4"}
def strm_host(d, p):
    f = os.path.join(D, "x.strm"); open(f, "w").write(TP["v"]); return f
m._strm_host_path = strm_host
m.cover_candidates = lambda key, d: [("u", "I1", "FC2-1", m.STRM_PATH + "/115/AV影片/FC2-1.strm", 3600)]
m.pan115_mounts = lambda d: ["/115"]
P115 = []
RAW = {"v": "https://cdnfhnfile.115cdn.net/x?t=1"}
m.pan115_url = lambda d, tp, ua: P115.append((tp, ua)) or RAW["v"]
m._ol_token = lambda d: "T"
m._ol_api = lambda *a, **k: {"data": {"sign": "S"}}
m.rebuild_cfg_from_disk = lambda d: {}
m.openlist_public_url = lambda cfg: "https://list.example"
m.hls_probe_url = lambda iid, key: ""
m.range_ok = lambda url, ua: True
m.resolve_once = lambda u, ua, timeout=30: u      # 1.5.384 起 /d/ 先换直链；这里不出网，原样返回
m.emby_ffprobe = lambda url, ua: (3600, 1, "")
m.heal_backlog_hold = lambda *a, **k: ""
m._heal_meter = lambda: ((lambda: 0), "x")
m.emby_set_image = lambda key, iid, jpg: True
ST = {}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **kw: ST.update(kw)
m.heal_log = lambda lines: None
CALLS = []
def frame(url, at, ua=None, watch=None):
    CALLS.append((url, ua))
    ok = url == RAW["v"] and ua == m.PLAYER_UA     # 跟真机一样：115 直链只认签它的那张脸
    return (b"\xff\xd8" + b"x" * 3000, "") if ok else (b"", "Input/output error")
m.cover_frame = frame

print("一、115 盘")
n = m.fill_covers(D, "K", manual=True)
check("截成了", n == 1)
check("按播放器的脸要的 115 直链", P115 == [(TP["v"], m.PLAYER_UA)])
check("先走那条、只用播放器的脸", CALLS == [(RAW["v"], m.PLAYER_UA)])

print("二、那条拿不到：退回 /d/")
RAW["v"] = ""; CALLS.clear(); P115.clear()
n = m.fill_covers(D, "K", manual=True)
check("退回 /d/、两张脸都试", [u.startswith("https://list.example/d/") for u, _ in CALLS] == [True, True]
      and [ua for _, ua in CALLS] == [m.PLAYER_UA, m.BROWSER_UA] and n == 0)

print("三、不是 115 的盘")
TP["v"] = "/quark/夸克挂载/电影/a.mkv"; RAW["v"] = "https://cdnfhnfile.115cdn.net/x?t=1"; P115.clear(); CALLS.clear()
m.fill_covers(D, "K", manual=True)
check("不去要 115 直链", P115 == [])
check("版本 1.5.382 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 382))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

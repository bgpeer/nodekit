#!/usr/bin/env python3
"""截封面每部只向网盘要一次地址（1.5.384）。
真机 10/17–18 夸克三部 FC2：诊断工具 10 秒截到，正式截却五花八门地失败（partial file / Invalid data / I/O error /
读不出时长 / 拿不到地址）。/d/ 每请求一次 OpenList 就去网盘换一次直链，ffmpeg 截一帧要读好几次。
钉住：
  一、resolve_once：302 → 返回跳去的地址；不跳 / 出错 → 原样；不跟着跳过去下载
  二、fill_covers：/d/ 先换成直链，读时长、截帧都用这同一条；m3u8 和 115 那条（已经是直链）不动
"""
import http.server, importlib.util, os, sys, tempfile, threading
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

HIT = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        HIT.append((self.path, self.headers.get("User-Agent"), self.headers.get("Range")))
        if self.path.startswith("/d/"):
            self.send_response(302); self.send_header("Location", "http://127.0.0.1:%d/cdn/x.mp4?sig=1" % PORT); self.end_headers()
        elif self.path == "/plain":
            self.send_response(206); self.send_header("Content-Length", "1"); self.end_headers(); self.wfile.write(b"x")
        else:
            self.send_response(500); self.end_headers()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{PORT}"

print("一、resolve_once")
r = m.resolve_once(B + "/d/quark/a.mp4?sign=S", m.PLAYER_UA)
check("302 → 跳去的那条", r == B + "/cdn/x.mp4?sig=1")
check("只问了一次 /d/、没跟过去下载", [h[0] for h in HIT] == ["/d/quark/a.mp4?sign=S"])
check("带着播放器的脸、只要 1 个字节", HIT[0][1] == m.PLAYER_UA and HIT[0][2] == "bytes=0-0")
check("不跳 → 原样", m.resolve_once(B + "/plain", m.PLAYER_UA) == B + "/plain")
check("出错 → 原样", m.resolve_once(B + "/boom", m.PLAYER_UA) == B + "/boom")
check("连不上 → 原样", m.resolve_once("http://127.0.0.1:1/d/x", m.PLAYER_UA, timeout=3) == "http://127.0.0.1:1/d/x")
srv.shutdown()

print("二、fill_covers")
D = tempfile.mkdtemp()
def strm_host(d, p):
    f = os.path.join(D, "x.strm"); open(f, "w").write("/quark/夸克挂载/AV/FC2-1.mp4"); return f
m._strm_host_path = strm_host
m.cover_candidates = lambda key, d: [("u", "I1", "FC2-1", m.STRM_PATH + "/quark/AV/FC2-1.strm", 0)]
m.pan115_mounts = lambda d: ["/115"]
m._ol_token = lambda d: "T"
m._ol_api = lambda *a, **k: {"data": {"sign": "S"}}
m.rebuild_cfg_from_disk = lambda d: {}
m.openlist_public_url = lambda cfg: "https://list.example"
M3U = {"v": ""}
m.hls_probe_url = lambda iid, key: M3U["v"]
RES = []
m.resolve_once = lambda u, ua, timeout=30: RES.append(u) or "https://cdn.example/final.mp4"
m.range_ok = lambda url, ua: True
PROBE, FRAME = [], []
m.emby_ffprobe = lambda url, ua: PROBE.append(url) or (3600, 1, "")
def frame(url, at, ua=None, watch=None):
    FRAME.append(url); return (b"\xff\xd8" + b"x" * 3000, "")
m.cover_frame = frame
m.heal_backlog_hold = lambda *a, **k: ""
m._heal_meter = lambda: ((lambda: 0), "x")
m.emby_set_image = lambda key, iid, jpg: True
ST = {}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **kw: ST.update(kw)
m.heal_log = lambda lines: None
n = m.fill_covers(D, "K", manual=True)
check("/d/ 先换成直链（只换一次）", len(RES) == 1 and RES[0].startswith("https://list.example/d/"))
check("读时长、截帧都用那同一条直链", PROBE == ["https://cdn.example/final.mp4"] and FRAME == ["https://cdn.example/final.mp4"] and n == 1)
M3U["v"] = "https://mw.example/x.m3u8"; RES.clear(); FRAME.clear()
m.fill_covers(D, "K", manual=True)
check("m3u8 那条不去换", all(".m3u8" not in u for u in RES) and FRAME[0] == "https://mw.example/x.m3u8")
check("版本 1.5.384 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 384))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

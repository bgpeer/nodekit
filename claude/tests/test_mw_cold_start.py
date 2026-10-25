#!/usr/bin/env python3
"""机器重启后 MediaWarp 没登录上 OpenList → 每小时那一轮认出来、重启它（1.5.386）。
真机 10/25：机器重启，容器一起起来，MediaWarp 登录 OpenList 时 OpenList 还没好；之后每次播放都回 Emby 的 404
「Could not find a part of the path」，播放器闪退。日志里没有「令牌作废」，老的检查认不出。
钉住：
  一、MediaWarp 比 OpenList 晚起超过 60 秒：不试（正常的单独重启）
  二、几乎同时起来：拿一个 strm 条目真问一次 —— 404 + 那句「找不到路径」= 坏了；302 = 好的
  三、同一次启动只试一回；没 key / OpenList 没好 / 找不到 strm 条目：不试、不乱重启
  四、heal_mediawarp_token：日志里没「令牌作废」但启动时没登录上，也重启 MediaWarp
"""
import http.server, importlib.util, os, sys, threading
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

MODE = {"v": "broken"}
HIT = []
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        HIT.append(self.path)
        if MODE["v"] == "broken":
            body = b"Could not find a part of the path '/quark/a.mp4'."
            self.send_response(404); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(302); self.send_header("Location", "https://cdn.example/x"); self.end_headers()
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
m.MEDIAWARP_PORT = srv.server_address[1]

START = {"mediawarp": "2026-10-25T12:23:07.1Z", "openlist": "2026-10-25T12:23:06.9Z"}
CMD = []
class R:
    def __init__(self, out="", rc=0): self.stdout, self.stderr, self.returncode = out, "", rc
def sh(cmd, **k):
    CMD.append(cmd)
    for n, v in START.items():
        if cmd.startswith("docker inspect") and cmd.endswith(n):
            return R(v + "\n")
    if cmd.startswith("docker logs"):
        return R("一切正常\n")
    return R()
m.sh = sh
ST = {}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **kw: ST.update(kw)
m.wait_openlist_ready = lambda d, timeout=90: True
m.self_touch = lambda iid: None
ITEMS = {"v": [{"Id": 1, "Path": "/mnt/local/x.mkv"}, {"Id": 239938, "Path": m.STRM_PATH + "/quark/x.strm"}]}
m._emby = lambda path, key, **k: {"Items": ITEMS["v"]}

print("一、晚起很久：不试")
START["mediawarp"] = "2026-10-25T12:30:00Z"
check("比 OpenList 晚起 7 分钟：不试", m.mediawarp_cold_start_broken("D", "K") is False and HIT == [])

print("二、几乎同时起来：真问一次")
START["mediawarp"] = "2026-10-25T12:23:07Z"
check("404 + 找不到路径 → 坏了", m.mediawarp_cold_start_broken("D", "K") is True)
check("问的是 strm 那一条、不跟跳转", len(HIT) == 1 and "/emby/videos/239938/stream" in HIT[0])
HIT.clear()
check("同一次启动不再试", m.mediawarp_cold_start_broken("D", "K") is False and HIT == [])
START["mediawarp"] = "2026-10-25T12:23:05Z"; MODE["v"] = "ok"
check("比 OpenList 还早起、但能 302 → 好的", m.mediawarp_cold_start_broken("D", "K") is False and len(HIT) == 1)

print("三、不该试 / 不该判坏的")
START["mediawarp"] = "2026-10-25T12:23:08Z"; HIT.clear()
check("没 key：不试", m.mediawarp_cold_start_broken("D", "") is False and HIT == [])
START["mediawarp"] = "2026-10-25T12:23:09Z"; ITEMS["v"] = [{"Id": 1, "Path": "/mnt/local/x.mkv"}]
check("找不到 strm 条目：不试", m.mediawarp_cold_start_broken("D", "K") is False and HIT == [])
ITEMS["v"] = [{"Id": 7, "Path": m.STRM_PATH + "/q/y.strm"}]
START["mediawarp"] = "2026-10-25T12:23:10Z"; m.wait_openlist_ready = lambda d, timeout=90: False
check("OpenList 没好：不试", m.mediawarp_cold_start_broken("D", "K") is False and HIT == [])
m.wait_openlist_ready = lambda d, timeout=90: True

print("四、heal_mediawarp_token")
m.ms_install_dir = lambda: "D"
m.read_yaml_scalar = lambda *a, **k: "K"
RUN = []
m.subprocess.run = lambda args, **k: RUN.append(args) or R()
START["mediawarp"] = "2026-10-25T12:23:11Z"; MODE["v"] = "broken"
check("日志没「令牌作废」、但启动时没登录上 → 重启 MediaWarp", m.heal_mediawarp_token() is True and RUN == [["docker", "restart", "mediawarp"]])
RUN.clear(); START["mediawarp"] = "2026-10-25T13:00:00Z"
check("正常的：不重启", m.heal_mediawarp_token() is False and RUN == [])
srv.shutdown()
check("版本 1.5.386 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 386))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

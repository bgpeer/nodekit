#!/usr/bin/env python3
"""claude/tools/progwatch.py（退出后进度什么时候记上）用假 Emby 跑两场：
  一、播放器读详情比 Emby 记上早 → 报 ✖、点名「详情」
  二、Emby 先记上、播放器后读 → 报 ✔
  三、不打 api_key / 用户 id
"""
import os, sys, subprocess, tempfile, textwrap
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.path.join(ROOT, "claude", "tools", "progwatch.py")
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def run(land_after, read_after):
    d = tempfile.mkdtemp(); log = os.path.join(d, "a.log"); open(log, "w").close()
    fake = os.path.join(d, "ms.py")
    open(fake, "w").write(textwrap.dedent(f'''
        import time
        T0 = time.time(); STOP = T0 + 1.0; LOG = {log!r}; wrote = [False]
        def ms_install_dir(): return "/x"
        def read_emby_api_key(d): return "SECRETKEY"
        def bj_fmt(f, ts=None): return "21:15:05"
        import threading
        threading.Timer(1.0 + {read_after}, lambda: open(LOG, "a").write(
            '127.0.0.1 - - [x] "GET /emby/Users/abcdef123/Items/239938?Fields=x HTTP/2.0" 200\\n')).start()
        def _emby(path, key, timeout=0, **k):
            t = time.time()
            if path == "/Sessions":
                if t < STOP:
                    return [{{"Id": "s1", "UserId": "abcdef123", "Client": "Hills",
                             "NowPlayingItem": {{"Id": "239938", "Name": "第164集", "SeriesName": "仙逆", "SeriesId": "112681"}},
                             "PlayState": {{"PositionTicks": 14210000000}}}}]
                return []
            pos = 14210000000 if t > STOP + {land_after} else 11100000000
            return {{"UserData": {{"PlaybackPositionTicks": pos, "Played": False}}}}
    '''))
    env = dict(os.environ, MS_SCRIPT=fake, PW_LOG=log)
    r = subprocess.run([sys.executable, TOOL, "0.3"], env=env, capture_output=True, text=True, timeout=60)
    return r.stdout + r.stderr

out = run(land_after=1.5, read_after=0.4)
print("一、读早了"); check("报 ✖ 读详情比 Emby 记上早", "✖ 有 1 次读页面比 Emby 记上早（详情）" in out and "退出：仙逆 第164集" in out)
check("报出 Emby 是哪一刻记上的", "Emby 记上了：23:41" in out)
out2 = run(land_after=0.2, read_after=1.5)
print("二、先记上后读"); check("报 ✔", "✔ 读页面都在记上之后" in out2)
print("三、不泄漏"); check("不打 api_key / 用户 id", "SECRETKEY" not in out + out2 and "abcdef123" not in out + out2)
if bad:
    print(out); print(out2)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

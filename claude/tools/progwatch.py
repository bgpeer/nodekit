#!/usr/bin/env python3
"""退出播放后进度什么时候记上（只读）：开着它去播、退出几次，每次退出打一段——
Emby 的续播点哪一刻跟上、播放器哪一刻来读页面，读早了就是「退出来没刷新」。

用法（VPS 上，跑着别关，去手机上播放 / 退出；Ctrl+C 结束，默认 20 分钟自己停）：
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/progwatch.py | python3 - [分钟]
只问本机 Emby（/Sessions 每 0.25 秒一次、退出后问那一集的续播点），只读 nginx 日志，什么都不写。
屏上有片名，不打 token / 用户 id。
"""
import importlib.util, os, re, sys, time
S = os.environ.get("MS_SCRIPT") or "/etc/bgpeer/media-stack.py"
spec = importlib.util.spec_from_file_location("ms", S); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
key = m.read_emby_api_key(m.ms_install_dir())
if not key:
    sys.exit("没有 Emby API Key（「7 设置 → 1」）")
LOG = os.environ.get("PW_LOG") or "/var/log/nginx/media-stack.access.log"
MINS = float(sys.argv[1]) if len(sys.argv) > 1 else 20
NEAR = 15 * 10 ** 7            # 续播点跟退出前最后的位置差 15 秒以内算「记上了」


def mmss(t):
    s = int(t or 0) // 10 ** 7
    return f"{s // 60}:{s % 60:02d}"


def sessions():
    out = {}
    for se in m._emby("/Sessions", key, timeout=3) or []:
        it = se.get("NowPlayingItem") or {}
        if it.get("Id"):
            out[se.get("Id")] = (str(it["Id"]), str(se.get("UserId") or ""),
                                 (it.get("SeriesName") + " " if it.get("SeriesName") else "") + str(it.get("Name") or ""),
                                 int((se.get("PlayState") or {}).get("PositionTicks") or 0),
                                 str(se.get("Client") or ""), str(it.get("SeriesId") or ""))
    return out


def userdata(uid, iid):
    ud = (m._emby(f"/Users/{uid}/Items/{iid}?Fields=UserData", key, timeout=3) or {}).get("UserData") or {}
    return int(ud.get("PlaybackPositionTicks") or 0), bool(ud.get("Played"))


try:
    fh = open(LOG, errors="replace"); fh.seek(0, 2)
except OSError:
    fh = None
print(f"开始盯（{MINS:.0f} 分钟）：去手机上播放、退出；每退出一次这里打一段。Ctrl+C 结束。")
seen, n, t_end = {}, 0, time.time() + MINS * 60
try:
    while time.time() < t_end:
        try:
            now = sessions()
        except Exception:
            time.sleep(1); continue
        for sid, (iid, uid, name, pos, cli, ser) in list(seen.items()):
            if now.get(sid, ("",))[0] == iid:
                continue
            n += 1
            t0 = time.time()
            try:
                p0, pl0 = userdata(uid, iid)
            except Exception:
                p0, pl0 = -1, False
            print(f"\n[{n}] {m.bj_fmt('%H:%M:%S')} 退出：{name}（{cli}）")
            print(f"    退出前最后位置 {mmss(pos)}；那一刻 Emby 续播点 {mmss(p0) if p0 >= 0 else '问不到'}"
                  + ("（已看完）" if pl0 else ""))
            landed, reads = None, []
            pat = re.compile(rf"GET /emby/(Users/[^/ ]+/Items/({iid}|{ser or 'x'})[? ]|Users/[^/ ]+/Items/Resume|Shows/NextUp)", re.I)
            while time.time() - t0 < 12:
                if landed is None:
                    try:
                        p, pl = userdata(uid, iid)
                        if pl or abs(p - pos) <= NEAR:
                            landed = time.time() - t0
                            print(f"    +{landed:4.1f}s  Emby 记上了：{'已看完' if pl else mmss(p)}")
                    except Exception:
                        pass
                for ln in (fh.readlines() if fh else []):
                    mm = pat.search(ln)
                    if mm:
                        what = ("详情" if iid in mm.group(1) else "剧集页" if ser and ser in mm.group(1)
                                else "继续观看" if "Resume" in mm.group(1) else "下一集")
                        reads.append((time.time() - t0, what))
                        print(f"    +{reads[-1][0]:4.1f}s  播放器来读：{what}")
                time.sleep(0.2)
            if landed is None:
                print("    12 秒内 Emby 一直没记上 ✖")
            early = [r for r in reads if landed is None or r[0] < landed]
            if early:
                print(f"    ✖ 有 {len(early)} 次读页面比 Emby 记上早（{'、'.join(w for _, w in early)}）—— 这次多半看到旧进度")
            elif reads:
                print("    ✔ 读页面都在记上之后")
            fh and fh.readlines()                       # 这 12 秒之外的行不算这一次
        seen = now
        time.sleep(0.25)
except KeyboardInterrupt:
    pass
print(f"\n结束，共 {n} 次退出。")

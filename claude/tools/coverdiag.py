#!/usr/bin/env python3
"""截封面为什么不成（只读）：照「截封面」的流程把某一部片走一遍 —— 有哪几条路（转码流 m3u8 / 115 直链 / OpenList /d/）、
每条路认不认跳转、读不读得出时长、两种身份（播放器 / 浏览器）各自截不截得到、花了几秒。
只截不存：不改封面、不记账。屏上不印地址（只印主机名）。

用法（VPS 上）：
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/coverdiag.py | python3 - 片名关键字
"""
import importlib.util, os, sys, time, urllib.parse
S = os.environ.get("MS_SCRIPT") or "/etc/bgpeer/media-stack.py"
spec = importlib.util.spec_from_file_location("ms", S); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
if len(sys.argv) < 2:
    sys.exit("要给一个片名关键字")
kw = sys.argv[1]
d = m.ms_install_dir()
key = m.read_emby_api_key(d)
uid = (m._emby("/Users", key) or [{}])[0].get("Id", "")
r = m._emby(f"/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Movie,Episode,Video&SearchTerm={urllib.parse.quote(kw)}"
            f"&Fields=Path,MediaSources", key) or {}
items = [i for i in r.get("Items") or [] if str(i.get("Path") or "").startswith(m.STRM_PATH)]
if not items:
    sys.exit("Emby 里没找到这部（只认 strm 库里的）")
it = items[0]
iid, path = str(it["Id"]), str(it.get("Path") or "")
print(f"== {it.get('Name')}　{path.replace(m.STRM_PATH + '/', '')}")
try:
    tp = m.strm_target_path(open(m._strm_host_path(d, path), encoding="utf-8").read())
except OSError:
    tp = ""
print("   网盘路径：", tp or "（读不到 strm）")
secs = max([(s.get("RunTimeTicks") or 0) / 1e7 for s in it.get("MediaSources") or []] + [0])
print(f"   Emby 里的时长：{secs / 60:.0f} 分" if secs else "   Emby 里还没时长")
host = lambda u: urllib.parse.urlsplit(u).hostname or "?"
routes = []
t = time.time()
try:
    u = m.hls_probe_url(iid, key)
    print(f"   转码流 m3u8：{'有（' + host(u) + '）' if u else '没有'}　{time.time() - t:.1f} 秒")
    if u:
        routes.append(("转码流 m3u8", u))
except Exception as e:
    print("   转码流 m3u8：问的时候出错", str(e)[:60])
if tp and any(tp == mp or tp.startswith(mp.rstrip("/") + "/") for mp in m.pan115_mounts(d)):
    t = time.time()
    u = m.pan115_url(d, tp, m.PLAYER_UA)
    print(f"   115 直链（播放器的脸）：{'有（' + host(u) + '）' if u else '没拿到'}　{time.time() - t:.1f} 秒")
    if u:
        routes.append(("115 直链", u))
tok = m._ol_token(d)
try:
    base = m.openlist_public_url(m.rebuild_cfg_from_disk(d))
except Exception:
    base = ""
if tp and tok and base:
    sign = (m._ol_api("/api/fs/get", {"path": tp, "password": ""}, tok, timeout=60).get("data") or {}).get("sign", "")
    routes.append(("OpenList /d/", base + "/d" + urllib.parse.quote(tp) + (f"?sign={sign}" if sign else "")))
for name, u in routes:
    print(f"== {name}（{host(u)}）")
    for ua_name, ua in (("播放器", m.PLAYER_UA), ("浏览器", m.BROWSER_UA)):
        t = time.time()
        rk = m.range_ok(u, ua) if ".m3u8" not in u.lower() else "（m3u8 不问）"
        s2 = secs
        if not s2:
            s2, _n, e = m.emby_ffprobe(u, ua)
            if not s2:
                print(f"   {ua_name}：认跳转 {rk}　读不出时长（{str(e)[:60]}）　{time.time() - t:.0f} 秒")
                continue
        jpg, why = m.cover_frame(u, s2 * m.COVER_AT, ua=ua)
        print(f"   {ua_name}：认跳转 {rk}　" + (f"截到了（{len(jpg) // 1024} KB）" if jpg else f"没截到（{why}）")
              + f"　{time.time() - t:.0f} 秒")

#!/usr/bin/env python3
"""截封面为什么不成（只读）：照「截封面」的流程把某一部片走一遍 —— 有哪几条路（转码流 m3u8 / 115 直链 / OpenList /d/）、
每条路认不认跳转、读不读得出时长、两种身份（播放器 / 浏览器）各自截不截得到、花了几秒。
另外测原片的下载速度、看时长索引（moov）在文件头还是文件尾（在尾的话截帧前要先跳到尾巴拉几 MB）。
只截不存：不改封面、不记账。屏上不印地址、也不印主机名（OpenList 那条的主机名就是安装人的域名）。

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
def host(u):
    h = urllib.parse.urlsplit(u).hostname or "?"
    return "你的 OpenList" if base and h == (urllib.parse.urlsplit(base).hostname or "") else h.split(".", 1)[-1]
tok = m._ol_token(d)
try:
    base = m.openlist_public_url(m.rebuild_cfg_from_disk(d))
except Exception:
    base = ""
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
if tp and tok and base:
    sign = (m._ol_api("/api/fs/get", {"path": tp, "password": ""}, tok, timeout=60).get("data") or {}).get("sign", "")
    routes.append(("OpenList /d/", base + "/d" + urllib.parse.quote(tp) + (f"?sign={sign}" if sign else "")))
import urllib.request, struct


def _get(u, ua, rng, secs=15):
    """拉 rng 那一段，最多 secs 秒 → (拉到的字节, 用了几秒, 总大小)。"""
    req = urllib.request.Request(u, headers={"User-Agent": ua, "Range": rng})
    t0, buf, total = time.time(), b"", 0
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cr = r.headers.get("Content-Range") or ""
            total = int(cr.rsplit("/", 1)[-1]) if "/" in cr and cr.rsplit("/", 1)[-1].isdigit() else 0
            while time.time() - t0 < secs:
                b = r.read(65536)
                if not b:
                    break
                buf += b
    except Exception as e:
        print("   拉的时候出错：", str(e)[:60])
    return buf, max(time.time() - t0, 0.01), total


def atoms(buf):
    out, i = [], 0
    while i + 8 <= len(buf):
        size, typ = struct.unpack(">I4s", buf[i:i + 8])
        if size == 1 and i + 16 <= len(buf):
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
        out.append((typ.decode("latin-1"), size))
        if size < 8:
            break
        i += size
    return out


for name, u in routes:
    print(f"== {name}（{host(u)}）")
    if ".m3u8" not in u.lower():
        buf, el, total = _get(u, m.PLAYER_UA, "bytes=0-", 15)
        print(f"   下载速度（播放器的脸，拉 15 秒）：{len(buf) / el / 1024:.0f} KB/s"
              + (f"　文件 {total / 2**30:.2f} GB" if total else ""))
        at = atoms(buf)
        if at:
            print("   文件结构：", "　".join(f"{t} {sz / 2**20:.1f}MB" for t, sz in at[:4]))
            if any(t == "mdat" for t, _ in at) and not any(t == "moov" for t, _ in at):
                print("   时长索引（moov）在文件尾：截帧前要先跳到尾巴读完它")
            elif any(t == "moov" for t, _ in at):
                print("   时长索引（moov）在文件头")
    for ua_name, ua in (("播放器", m.PLAYER_UA), ("浏览器", m.BROWSER_UA)):
        t = time.time()
        rk = m.range_ok(u, ua) if ".m3u8" not in u.lower() else "（m3u8 不问）"
        s2 = secs
        if not s2:
            s2, _n, e = m.emby_ffprobe(u, ua)
            if not s2:
                e = " ".join(str(e).split())
                print(f"   {ua_name}：认跳转 {rk}　读不出时长（{e[:60]}）　{time.time() - t:.0f} 秒")
                continue
        jpg, why = m.cover_frame(u, s2 * m.COVER_AT, ua=ua)
        print(f"   {ua_name}：认跳转 {rk}　" + (f"截到了（{len(jpg) // 1024} KB）" if jpg else f"没截到（{why}）")
              + f"　{time.time() - t:.0f} 秒")

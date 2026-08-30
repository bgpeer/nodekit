#!/usr/bin/env bash
# 同一个片子，把【挂载页面走的那条路】和【Emby 走的那条路】各测一遍速度。
#
#     bash compare-speed.sh 龙虎门
#
# 只读，不改任何东西。
#
# 为什么要并排测：这两条路【根本不是同一路流】，而从外面完全看不出来 ——
#   · 挂载页面 → OpenList 的 video_preview 接口 → 网盘的【转码流】(SD/HD…)
#   · Emby     → MediaWarp 302 → 网盘的【原始文件下载链接】
# 网盘对这两条常常给完全不同的速度（阿里云盘尤其明显）。
# 「挂载里随便拖、Emby 卡死」十有八九就是这个差别，而不是脚本或线路的问题。
set -u

Q="${1:-}"
[ -n "$Q" ] || { echo "用法：bash compare-speed.sh <片名的一部分 或 集号>"; exit 1; }
DIR="${MS_DIR:-/opt/media-stack}"
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
# OpenList 的密码在 .secrets，取不到就退回 .env（老版本装的写在那儿）
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key，先跑「3 后补参数」"; exit 1; }

python3 - "$KEY" "$OLPW" "$DATA_ROOT" "$Q" <<'PY'
import json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
KEY, OLPW, DATA_ROOT, Q = sys.argv[1:5]
EMBY, MW, OL = "http://127.0.0.1:8096", "http://127.0.0.1:9000", "http://127.0.0.1:5244"
B, D, R, G, Y, X = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def hr(): print("-" * 60)


def jget(url, timeout=60):
    return json.load(urllib.request.urlopen(url, timeout=timeout))


def ol(path, body, token=None, timeout=60):
    req = urllib.request.Request(
        OL + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": token} if token else {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def pull(url, want=4 * 1024 * 1024, timeout=90):
    """拉一段，返回 (字节数, 秒数, 首字节秒数, 错误)。

    【必须带 Range】播放器永远带 —— 要 seek、要分段。不带的话阿里云盘直接 403，
    量到的就不是播放器走的那条路了（这个坑踩过一次）。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": f"bytes=0-{want - 1}"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        ttfb = time.time() - t
        n = 0
        while n < want:
            b = r.read(1 << 20)
            if not b:
                break
            n += len(b)
        return n, max(time.time() - t, 1e-6), ttfb, ""
    except Exception as e:
        return 0, max(time.time() - t, 1e-6), 0, str(e)


def show(label, n, dt, ttfb, err):
    if err:
        print(f"  {label:22} {R}✖ {err}{X}")
        return 0.0
    mbps = n * 8 / dt / 1e6
    print(f"  {label:22} {B}{mbps:7.1f} Mbps{X}  "
          f"{D}({n/1048576:.1f} MiB / {dt:.1f}s，首字节 {ttfb:.2f}s){X}")
    return mbps


# ---------- 找条目 ----------
items, start = [], 0
while True:
    d = jget(f"{EMBY}/Items?Recursive=true&IncludeItemTypes=Movie,Episode,Video"
             f"&Fields=Path,MediaSources,IndexNumber&StartIndex={start}"
             f"&Limit=500&api_key={KEY}")
    b = d.get("Items") or []
    items += b
    start += len(b)
    if not b or start >= int(d.get("TotalRecordCount") or 0):
        break
cands = []
for i in items:
    p = str(i.get("Path") or "")
    if not p.endswith(".strm"):
        continue
    if (Q.isdigit() and str(i.get("IndexNumber") or "") == Q):
        cands.insert(0, i)
    elif Q in str(i.get("Name") or "") or Q in p.rsplit("/", 1)[-1]:
        cands.append(i)
if not cands:
    print(f"✖ 没找到「{Q}」"); raise SystemExit(1)
if len(cands) > 1:
    print(f"✖ 匹配到 {len(cands)} 个，说具体点："
          + " ／ ".join(str(x.get("Name")) for x in cands[:6])); raise SystemExit(1)
it = cands[0]
iid, name, cpath = it.get("Id"), it.get("Name"), str(it.get("Path"))

# 容器内路径 → 宿主机路径 → 读出 strm 里那个网盘路径
hp = os.path.join(DATA_ROOT, "strm", cpath[len("/data/strm/"):]) \
     if cpath.startswith("/data/strm/") else ""
cloud = ""
if hp and os.path.exists(hp):
    raw = open(hp, encoding="utf-8", errors="replace").read().strip()
    if raw.startswith("/"):
        cloud = raw
    elif "/d/" in raw:                      # URL 形态：https://list.x/d/<路径>?sign=
        cloud = "/" + raw.split("/d/", 1)[1].split("?", 1)[0]
        cloud = urllib.parse.unquote(cloud)

ms = (it.get("MediaSources") or [{}])[0]
size = ms.get("Size") or 0
dur = (it.get("RunTimeTicks") or 0) / 10_000_000
br = ms.get("Bitrate") or (int(size * 8 / dur) if size and dur else 0)

print()
hr(); print(f"  {B}{name}{X}"); hr()
print(f"  网盘路径   {cloud or '(读不出来)'}")
print(f"  原片码率   {B}{br/1e6:.2f} Mbps{X}"
      f"{D}   ← 下面两条路谁能超过它，谁就不卡{X}" if br else "  原片码率   未知")
print()

need = br / 1e6 if br else 0

# ---------- 路 A：Emby 走的（MediaWarp 302 → 原始文件） ----------
print(f"  {B}A. Emby 走的路{X}{D}   MediaWarp 302 → 网盘原始文件{X}")


class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None


loc = ""
t0 = time.time()
try:
    urllib.request.build_opener(NoRedir).open(
        f"{MW}/Videos/{iid}/stream?MediaSourceId=mediasource_{iid}"
        f"&Static=true&api_key={KEY}", timeout=90)
except urllib.error.HTTPError as e:
    if e.code in (301, 302, 303, 307, 308):
        loc = e.headers.get("Location", "")
except Exception as e:
    print(f"    换直链失败：{e}")
a_mbps = 0.0
if loc:
    _host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", loc)
    print(f"    {D}节点 {_host}　换直链 {time.time()-t0:.1f}s{X}")
    a_mbps = show("实测", *pull(loc))
else:
    print(f"    {R}✖ 没拿到 302{X}")
print()

# ---------- 路 B：挂载页面走的（OpenList video_preview → 转码流） ----------
print(f"  {B}B. 挂载页面走的路{X}{D}   OpenList 转码流（video_preview）{X}")
b_mbps = 0.0
best = ""
if not cloud:
    print(f"    {Y}读不出网盘路径，测不了{X}")
elif not OLPW:
    print(f"    {Y}读不到 OpenList 密码，测不了{X}")
else:
    try:
        tok = (ol("/api/auth/login",
                  {"username": "admin", "password": OLPW}).get("data")
               or {}).get("token", "")
        rr = ol("/api/fs/other",
                {"path": cloud, "password": "", "method": "video_preview"},
                tok, timeout=90)
        data = rr.get("data") or {}
        # 各家驱动放法不一样，把所有带 url 的项都抠出来
        lst = []
        for k in ("video_preview_play_info", "live_transcoding_task_list"):
            v = data.get(k)
            if isinstance(v, dict):
                lst += v.get("live_transcoding_task_list") or []
            elif isinstance(v, list):
                lst += v
        if not lst:
            lst = [x for x in (data.get("list") or []) if isinstance(x, dict)]
        got = [(str(x.get("template_id") or x.get("name") or "?"),
                str(x.get("url") or x.get("preview_url") or ""))
               for x in lst if isinstance(x, dict)]
        got = [(q, u) for q, u in got if u]
        if not got:
            print(f"    {Y}这个网盘没给转码流{X}"
                  f"{D}（返回里没有可播的地址 —— 有些盘/有些格式就是没有）{X}")
            print(f"    {D}那挂载页面放的其实也是原文件，和 A 同一条路。{X}")
        else:
            print(f"    {D}可选清晰度：{'、'.join(q for q, _ in got)}{X}")
            # 挑最高的那档测 —— 挂载页面默认给的往往是最低档，
            # 拿最低档去比"不卡"没有意义
            best, burl = got[-1]
            if burl.split("?")[0].endswith(".m3u8"):
                txt = urllib.request.urlopen(urllib.request.Request(
                    burl, headers={"User-Agent": UA}), timeout=60).read().decode(
                    "utf-8", "replace")
                segs = [x.strip() for x in txt.splitlines()
                        if x.strip() and not x.startswith("#")]
                if segs and ".m3u8" in segs[0]:
                    burl2 = (segs[0] if segs[0].startswith("http")
                             else burl.rsplit("/", 1)[0] + "/" + segs[0].lstrip("/"))
                    txt = urllib.request.urlopen(urllib.request.Request(
                        burl2, headers={"User-Agent": UA}), timeout=60).read().decode(
                        "utf-8", "replace")
                    burl = burl2
                    segs = [x.strip() for x in txt.splitlines()
                            if x.strip() and not x.startswith("#")]
                if not segs:
                    print(f"    {R}✖ 播放列表里没有分片{X}")
                else:
                    mid = max(0, len(segs) // 2 - 1)
                    tot, dts, ttfb1 = 0, 0.0, None
                    for u in segs[mid:mid + 3]:
                        au = u if u.startswith("http") else \
                            burl.rsplit("/", 1)[0] + "/" + u.lstrip("/")
                        # 【和 A 拉一样多】不然比的是"谁拉得久"不是"谁快"，
                        # 而且分片动辄几十 MiB，测一次就是几百 MiB 流量
                        n, dt, tf, err = pull(au, want=4 * 1024 * 1024)
                        if ttfb1 is None:
                            ttfb1 = tf
                        tot += n; dts += dt
                    b_mbps = show(f"实测（{best}）", tot, dts, ttfb1 or 0, "")
                    if not tot:
                        print(f"    {R}✖ 分片一个都没拉动{X}")
            else:
                b_mbps = show(f"实测（{best}）", *pull(burl))
    except Exception as e:
        print(f"    {R}✖ 测不了：{e}{X}")

# ---------- 结论 ----------
print()
hr(); print(f"  {B}结论{X}"); hr()
if need:
    print(f"  原片要 {B}{need:.1f} Mbps{X} 才能边放边看。")
# 【只有 A 能拿原片码率当尺子】B 放的是转码流，码率比原片低一个量级
# （SD 通常 1～2 Mbps）。拿 17 Mbps 去衡量它，会把一条明明很流畅的路
# 判成"不够" —— 实测就出过这个错：B 报 12 Mbps「不够 0.7x」，
# 而用户在挂载页面拖进度条根本不卡。尺子用错比不量还糟。
if a_mbps > 0:
    if need:
        print(f"  {'A  Emby 这条':16} {a_mbps:6.1f} Mbps   "
              + (f"{G}够（{a_mbps/need:.1f}x）{X}" if a_mbps >= need * 1.5 else
                 f"{R}不够（{a_mbps/need:.1f}x，要 >1.5x 才稳）{X}"))
    else:
        print(f"  {'A  Emby 这条':16} {a_mbps:6.1f} Mbps")
if b_mbps > 0:
    print(f"  {'B  挂载页面这条':15} {b_mbps:6.1f} Mbps   "
          f"{D}放的是 {best} 转码流，码率比原片低一个量级，"
          f"不能拿原片那个数衡量{X}")
print()
if a_mbps and b_mbps and b_mbps > a_mbps * 1.5:
    print(f"  {Y}两条路差 {b_mbps/a_mbps:.0f} 倍。挂载页面流畅是【两个原因叠加】：{X}")
    print(f"    {D}1. 它放的是转码流（{best}），码率比原片低一个量级{X}")
    print(f"    {D}2. 网盘对这两条是【分开限速】的 —— 下载接口掐得狠，"
          f"播放接口放行{X}")
    print(f"  {D}Emby 要的是原片、走的是下载接口，两头都吃亏。不是脚本的问题。{X}")
    print(f"  {B}想让 Emby 也走转码流{X}{D}：夸克那边是 link_method=streaming；"
          f"阿里要试 alipan_type 改成 alipanTV（TV 接口），或者买第三方权益包"
          f"解掉下载限速。{X}")
elif a_mbps and need and a_mbps < need:
    print(f"  {Y}Emby 这条路的速度撑不住原片码率 —— 卡就是这么来的。{X}")
    print(f"  {D}网盘对第三方限速的话，换个盘放大片、或者放码率低的版本。{X}")
elif a_mbps and need and a_mbps >= need * 1.5:
    print(f"  {G}Emby 这条路够快，卡的原因不在带宽{X}"
          f"{D} —— 去看是不是 Emby 在转码（why-slow.sh 有那一段）。{X}")
print()
PY

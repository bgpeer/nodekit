#!/usr/bin/env bash
# 为什么【这一集】卡，别的不卡 —— 把两集拉出来并排比一遍。
#
# 用法（在装了 media-stack 的机器上）：
#     bash why-slow.sh 238 237
#           ^卡的那集  ^不卡的那集（对照组，可以不填，默认 237）
#
# 只读，不改任何东西。
#
# 一集卡、别的不卡，可能性就那么几种，这个脚本把它们逐条量出来：
#   1. 那一集的码率/分辨率/编码本来就更重   → 看「媒体信息」那段
#   2. Emby 在转码它（2 核的机器转 4K = 必卡）→ 看「Emby 有没有在转码」
#   3. 直链落到了不同的节点，或者那条线本身慢 → 看「直链实测」那段
#   4. 网盘对这个文件单独限速                 → 同上，两集速度差一个量级就是它
set -u

SLOW="${1:-238}"
GOOD="${2:-237}"
DIR="${MS_DIR:-/opt/media-stack}"

# ---- 配置：全部从落盘的文件里读，不写死 ----
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
EMBY="http://127.0.0.1:8096"
MW="http://127.0.0.1:9000"

if [ -z "$KEY" ]; then
  echo "✖ 读不到 Emby API Key（$DIR/mediawarp/config/config.yaml 里的 auth）"
  echo "  先跑 media-stack 的「3 后补参数」把 Key 填上。"
  exit 1
fi

hr() { printf '%s\n' "------------------------------------------------------------"; }

# ---- 找条目 ----
find_item() {   # $1=集号 -> "Id<TAB>Name<TAB>Path"
  python3 - "$EMBY" "$KEY" "$1" <<'PY'
import json,sys,urllib.request,urllib.parse
emby,key,ep=sys.argv[1],sys.argv[2],sys.argv[3]
u=(f"{emby}/Items?Recursive=true&IncludeItemTypes=Episode"
   f"&Fields=Path,MediaSources,IndexNumber&Limit=2000&api_key={key}")
try: d=json.load(urllib.request.urlopen(u,timeout=60))
except Exception as e: print(f"ERR\t{e}\t"); raise SystemExit
for i in d.get("Items") or []:
    p=str(i.get("Path") or "")
    if not p.endswith(".strm"): continue
    # 集号对上，或者网盘文件名里带这个数
    if str(i.get("IndexNumber") or "")==ep or ep in p.rsplit("/",1)[-1]:
        print(f"{i.get('Id')}\t{i.get('Name')}\t{p}"); raise SystemExit
print("NOTFOUND\t\t")
PY
}

media_info() {  # $1=ItemId
  python3 - "$EMBY" "$KEY" "$1" <<'PY'
import json,sys,urllib.request
emby,key,iid=sys.argv[1],sys.argv[2],sys.argv[3]
u=f"{emby}/Items?Ids={iid}&Fields=MediaSources,Path&api_key={key}"
try: d=json.load(urllib.request.urlopen(u,timeout=60))
except Exception as e: print("  拿不到媒体信息:",e); raise SystemExit
it=(d.get("Items") or [{}])[0]
ms=(it.get("MediaSources") or [{}])[0]
def g(k,d2=""): return ms.get(k) or d2
size=g("Size",0) or 0
dur=(it.get("RunTimeTicks") or 0)/10_000_000
br=g("Bitrate",0) or 0
cont=g('Container','?')
hls = str(cont).lower() in ("hls","m3u8")
print(f"  容器      {cont}" + ("   ← 转码流：网盘给的是播放列表，不是整文件，"
                               "所以下面大小/码率是空的（正常）" if hls else ""))
print(f"  文件大小  {size/1024/1024:.0f} MiB" if size
      else ("  文件大小  —（HLS 没有单一文件）" if hls else "  文件大小  未知"))
print(f"  时长      {dur/60:.1f} 分钟" if dur else "  时长      未知（时长为 0 会让续播点存不下来）")
if br: print(f"  总码率    {br/1_000_000:.2f} Mbps   <<< 两集差得多的话，卡就是它")
elif size and dur: print(f"  总码率    {size*8/dur/1_000_000:.2f} Mbps（按大小/时长算）  <<< 两集差得多的话，卡就是它")
elif hls: print("  总码率    —（HLS 的码率看下面「流码率」那行）")
else: print("  总码率    未知")
for s in (ms.get("MediaStreams") or []):
    t=s.get("Type")
    if t=="Video":
        print(f"  视频      {s.get('Codec')} {s.get('Width')}x{s.get('Height')} "
              f"{s.get('AverageFrameRate') or s.get('RealFrameRate') or '?'}fps "
              f"{(s.get('BitRate') or 0)/1_000_000:.2f}Mbps "
              f"profile={s.get('Profile')} {s.get('PixelFormat') or ''} "
              f"{'HDR/'+str(s.get('VideoRange')) if s.get('VideoRange') not in (None,'SDR') else ''}")
    elif t=="Audio":
        print(f"  音频      {s.get('Codec')} {s.get('Channels')}ch "
              f"{(s.get('BitRate') or 0)/1000:.0f}kbps")
PY
}

# ---- 302 + 实测速度（认得出 HLS 和整文件两种）----
probe() {       # $1=ItemId
  python3 - "$MW" "$KEY" "$1" <<'PY'
import re, sys, time, urllib.request, urllib.error
mw, key, iid = sys.argv[1], sys.argv[2], sys.argv[3]

class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

def get(url, rng=None, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "why-slow"})
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-{rng[1]}")
    return urllib.request.urlopen(req, timeout=timeout)

url = (f"{mw}/Videos/{iid}/stream?MediaSourceId=mediasource_{iid}"
       f"&Static=true&api_key={key}")
op = urllib.request.build_opener(NoRedir)
t0 = time.time()
loc = ""
try:
    r = op.open(url, timeout=90)
    print(f"  换直链    {time.time()-t0:.1f} 秒   HTTP {r.status}（不是 302）")
except urllib.error.HTTPError as e:
    dt = time.time() - t0
    if e.code in (301, 302, 303, 307, 308):
        loc = e.headers.get("Location", "")
        print(f"  换直链    {dt:.1f} 秒   HTTP {e.code}")
    else:
        print(f"  换直链    {dt:.1f} 秒   {'✖ HTTP %d' % e.code}")
        print(f"  ✖ 没拿到直链：{dt:.1f} 秒之后回了个 {e.code}。"
              f"这一集【这会儿根本放不了】——")
        print(f"    换直链是每次播放都要走一遍的，它失败就是点开转圈。"
              f"多半是网盘接口在限流（跑「5 体检」看「列目录历史」那张图）。")
        raise SystemExit
except Exception as e:
    print(f"  ✖ 换直链失败：{e}"); raise SystemExit
if not loc:
    print("  ✖ 没拿到 302 —— 视频会经过本机中转，那是必卡的"); raise SystemExit
host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", loc)
print(f"  直链节点  {host}")

# --- 这条直链是 HLS 播放列表，还是一个整文件？ ---
try:
    r = get(loc, timeout=60)
    head = r.read(65536)
    ctype = (r.headers.get("Content-Type") or "").lower()
    clen = r.headers.get("Content-Length")
except Exception as e:
    print(f"  ✖ 直链打不开：{e}"); raise SystemExit

is_hls = head.lstrip().startswith(b"#EXTM3U") or "mpegurl" in ctype
if not is_hls:
    # 整文件：从第 80 MiB 处拉 16 MiB —— 开头那段往往有缓存，测不出持续速度
    size = int(clen or 0)
    off = 80 * 1024 * 1024 if size > 100 * 1024 * 1024 else 0
    t = time.time(); got = 0
    try:
        rr = get(loc, (off, off + 16 * 1024 * 1024 - 1), timeout=120)
        ttfb = time.time() - t
        while got < 16 * 1024 * 1024:
            b = rr.read(1 << 20)
            if not b: break
            got += len(b)
    except Exception as e:
        print(f"  ✖ 拉不动：{e}"); raise SystemExit
    dt = max(time.time() - t, 1e-6)
    print(f"  类型      整文件（原画直链）{'  %.0f MiB' % (size/1048576) if size else ''}")
    print(f"  首字节    {ttfb:.2f} 秒")
    print(f"  实测速度  {got/dt/1048576:.2f} MB/s = {got*8/dt/1e6:.1f} Mbps"
          f"（从第 {off//1048576} MiB 处拉了 {got/1048576:.1f} MiB）")
    print("  ↑ 要大于这一集的总码率才不卡。")
    raise SystemExit

# --- HLS：整个播放列表才几 KB，拿 Range 去测它是【没有意义】的。
#     要测的是里面的分片（.ts），顺便从 BANDWIDTH 读出这条流的码率。 ---
print("  类型      HLS 播放列表（转码流）—— 码率由网盘那边定，不是原片码率")
txt = head.decode("utf-8", "replace")
try:
    txt += r.read().decode("utf-8", "replace")
except Exception:
    pass

def abso(u):
    if u.startswith("http"): return u
    return loc.rsplit("/", 1)[0] + "/" + u.lstrip("/")

bw = 0
m = re.search(r"BANDWIDTH=(\d+)", txt)
if m:
    bw = int(m.group(1))
    print(f"  流码率    {bw/1e6:.2f} Mbps（播放列表里写的 BANDWIDTH）")
# master playlist -> 再进一层拿真正的分片列表
lines = [x.strip() for x in txt.splitlines() if x.strip() and not x.startswith("#")]
if lines and (".m3u8" in lines[0]):
    try:
        loc2 = abso(lines[0])
        txt = get(loc2, timeout=60).read().decode("utf-8", "replace")
        loc = loc2
        lines = [x.strip() for x in txt.splitlines()
                 if x.strip() and not x.startswith("#")]
    except Exception as e:
        print(f"  ✖ 二级播放列表打不开：{e}"); raise SystemExit
if not lines:
    print("  ✖ 播放列表里一个分片都没有 —— 这条流是空的"); raise SystemExit
print(f"  分片数    {len(lines)} 个")
# 拉【中间】那 3 个分片：开头几个网盘那边常是预热好的，测不出真实情况
mid = max(0, len(lines) // 2 - 1)
tot, dt_all, ttfb1, fails = 0, 0.0, None, 0
for u in lines[mid:mid + 3]:
    t = time.time()
    try:
        rr = get(abso(u), timeout=60)
        if ttfb1 is None: ttfb1 = time.time() - t
        n = 0
        while True:
            b = rr.read(1 << 20)
            if not b: break
            n += len(b)
        tot += n; dt_all += time.time() - t
    except Exception:
        fails += 1
if fails:
    print(f"  ✖ {fails}/3 个分片拉失败 —— 播的时候就是卡在这儿")
if tot and dt_all:
    spd = tot / dt_all
    print(f"  首字节    {ttfb1:.2f} 秒")
    print(f"  实测速度  {spd/1048576:.2f} MB/s = {spd*8/1e6:.1f} Mbps"
          f"（拉了中间 {3-fails} 个分片，共 {tot/1048576:.1f} MiB）")
    if bw:
        r_ = spd * 8 / bw
        print(f"  余量      {r_:.1f}x   " + (
            "✔ 够（要 >1.5x 才稳）" if r_ >= 1.5 else
            "⚠ 勉强，遇到抖动就卡" if r_ >= 1.0 else
            "✖ 不够 —— 边放边等，这就是卡的直接原因"))
PY
}

echo
hr; echo "  为什么第 $SLOW 集卡、第 $GOOD 集不卡"; hr

for ep in "$SLOW" "$GOOD"; do
  line="$(find_item "$ep")"
  id="$(printf '%s' "$line" | cut -f1)"
  nm="$(printf '%s' "$line" | cut -f2)"
  pth="$(printf '%s' "$line" | cut -f3)"
  echo
  if [ "$id" = "NOTFOUND" ] || [ -z "$id" ]; then
    echo "【第 $ep 集】在 Emby 里没找到 —— 集号对不上？换个数字试试"
    continue
  fi
  if [ "$id" = "ERR" ]; then echo "【第 $ep 集】问 Emby 失败：$nm"; continue; fi
  echo "【第 $ep 集】$nm"
  echo "  条目 id   $id"
  echo "  strm      $pth"
  # 容器内路径 -> 宿主机路径，把 strm 内容（网盘全路径）打出来
  host="$DATA_ROOT/strm/${pth#/data/strm/}"
  if [ -f "$host" ]; then
    echo "  指向      $(head -c 400 "$host")"
  else
    echo "  指向      （宿主机上找不到 $host）"
  fi
  media_info "$id"
  probe "$id"
done

echo
hr
echo "  Emby 现在有没有在转码"
hr
python3 - "$EMBY" "$KEY" <<'PY'
import json,sys,urllib.request
emby,key=sys.argv[1],sys.argv[2]
try: d=json.load(urllib.request.urlopen(f"{emby}/Sessions?api_key={key}",timeout=30))
except Exception as e: print("  问不到:",e); raise SystemExit
busy=False
for s in d or []:
    ns=s.get("NowPlayingItem") or {}
    if not ns: continue
    busy=True
    ti=s.get("TranscodingInfo") or {}
    play=(s.get("PlayState") or {}).get("PlayMethod")
    print(f"  {s.get('Client')} / {s.get('DeviceName')}  正在放：{ns.get('Name')}")
    print(f"    播放方式  {play}   <<< DirectPlay 才是走 302；"
          f"Transcode/DirectStream 都是【经过本机】，2 核必卡")
    if ti:
        print(f"    转码中    {ti.get('VideoCodec')}  {ti.get('Bitrate',0)/1_000_000:.1f}Mbps "
              f"CPU {ti.get('CompletionPercentage','?')}%  原因：{'、'.join(ti.get('TranscodeReasons') or []) or '?'}")
if not busy:
    print("  当前没有在播的会话 —— 想抓转码的话，让第 238 集放着，再跑一次这个脚本")
PY

echo
hr
echo "  机器负载（转码会把它顶满）"
hr
echo "  $(uptime)"
command -v docker >/dev/null 2>&1 && docker stats --no-stream --format \
  '  {{.Name}}  CPU {{.CPUPerc}}  内存 {{.MemUsage}}' 2>/dev/null | head -8

echo
hr
cat <<'TIP'
  怎么看这份结果
    · 容器是 hls（转码流）                → 大小/总码率是空的，正常。
                                            码率看「流码率」那行 —— 那是网盘
                                            那边定的档位，不是原片码率
    · 「余量」小于 1.5x                   → 边放边等，这就是卡的直接原因。
                                            去「3 后补参数 → 3」把直链方式
                                            从「转码流」换成「原画」试试：
                                            原画是直接拉整文件，不经过网盘的
                                            转码服务器，通常更稳
    · 换直链 ✖（404 / 超时）              → 这一集【当下根本放不了】，点开就转圈。
                                            换直链是每次播放都要走一遍的。
                                            多半是网盘接口在限流 ——
                                            跑「5 体检」看「列目录历史」那张图
    · 有分片拉失败                        → 播的时候就是卡在那几个分片上
    · 播放方式不是 DirectPlay             → Emby 在转码，2 核的机器扛不住。
                                            多半是编码客户端不认
                                            （HEVC 10bit / AV1 / 特殊音轨）
    · 两集各项都差不多、就是卡            → 不是这一集的问题，是网盘那会儿在限流
TIP
echo

#!/usr/bin/env bash
# 为什么【这个片子】卡/放不了，别的正常 —— 把整条链路挨个量一遍。
# 电影和剧集都能查。
#
# 用法（在装了 media-stack 的机器上）：
#     bash why-slow.sh 238 237        两集并排比
#     bash why-slow.sh 龙虎门           只查一个（电影也行）
#           ^集号、片名的一部分、或文件名的一部分都能找
#
# 只读，不改任何东西。
#
# 一集卡、别的不卡，可能性就那么几种，这个脚本把它们逐条量出来：
#   1. 这个片子的码率/分辨率/编码本来就更重 → 看「媒体信息」那段
#   2. Emby 在转码它（2 核的机器转 4K = 必卡）→ 看「Emby 有没有在转码」
#   3. 直链落到了不同的节点，或者那条线本身慢 → 看「直链实测」那段
#   4. 网盘对这个文件单独限速                 → 同上，速度差一个量级就是它
set -u

SLOW="${1:-}"
GOOD="${2:-}"
if [ -z "$SLOW" ]; then
  echo "用法：bash why-slow.sh <卡的那个> [不卡的那个]"
  echo "     可以填集号（238）、片名的一部分（龙虎门）、或文件名的一部分"
  exit 1
fi
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

# ---- 找条目：电影和剧集都要找，按集号 / 片名 / 文件名匹配 ----
find_item() {   # $1=要找的东西 -> "Id<TAB>Name<TAB>Path"
  python3 - "$EMBY" "$KEY" "$1" <<'PY'
import json,sys,urllib.request,urllib.parse
emby,key,q=sys.argv[1],sys.argv[2],sys.argv[3]
out,start=[],0
while True:
    u=(f"{emby}/Items?Recursive=true&IncludeItemTypes=Movie,Episode,Video"
       f"&Fields=Path,IndexNumber&StartIndex={start}&Limit=500&api_key={key}")
    try: d=json.load(urllib.request.urlopen(u,timeout=60))
    except Exception as e: print(f"ERR\t{e}\t"); raise SystemExit
    b=d.get("Items") or []; out+=b; start+=len(b)
    if not b or start>=int(d.get("TotalRecordCount") or 0): break
# 【三种匹配都要，从严到宽】集号最准，其次片名，最后文件名。
# 只按集号找的话，电影一个都找不到 —— 电影没有 IndexNumber。
cands=[]
for i in out:
    p=str(i.get("Path") or "")
    if not p.endswith(".strm"): continue
    base=p.rsplit("/",1)[-1]
    name=str(i.get("Name") or "")
    if q.isdigit() and str(i.get("IndexNumber") or "")==q: cands.insert(0,i)
    elif q in name or q in base: cands.append(i)
if not cands: print("NOTFOUND\t\t"); raise SystemExit
if len(cands)>1:
    print(f"MANY\t{len(cands)}\t"+" ／ ".join(
        str(x.get("Name")) for x in cands[:6])); raise SystemExit
i=cands[0]
print(f"{i.get('Id')}\t{i.get('Name')}\t{i.get('Path')}")
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
if br: print(f"  总码率    {br/1_000_000:.2f} Mbps   <<< 和对照组差得多的话，卡就是它")
elif size and dur: print(f"  总码率    {size*8/dur/1_000_000:.2f} Mbps（按大小/时长算）  <<< 和对照组差得多的话，卡就是它")
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
BOLD, RST = "\033[1m", "\033[0m"
mw, key, iid = sys.argv[1], sys.argv[2], sys.argv[3]

class NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# 【403 不一定是"放不了"，也可能是我这个脚本没带对头】网盘的直链常有防盗链：
# 认 Referer、认 User-Agent。用一个光秃秃的 UA 去拉必然 403，而真正的播放器
# 带着浏览器 UA 就能拉动 —— 这种情况下报"放不了"是误判，会把人带偏。
# 所以挨个换头再试，并且【说清楚是哪一组头才拉得动】：那正是要去 OpenList
# 里补的东西。
HEADSETS = (
    ("脚本默认（无 Referer）", {"User-Agent": "why-slow"}),
    ("浏览器 UA", {"User-Agent": UA_BROWSER}),
    ("浏览器 UA + Referer 阿里云盘",
     {"User-Agent": UA_BROWSER, "Referer": "https://www.alipan.com/"}),
    ("浏览器 UA + Referer 阿里云盘（旧域名）",
     {"User-Agent": UA_BROWSER, "Referer": "https://www.aliyundrive.com/"}),
)
GOOD_HEAD = dict(HEADSETS[0][1])          # 试出来能用的那组，后面测速沿用


def get(url, rng=None, timeout=90, head=None):
    req = urllib.request.Request(url, headers=dict(head or GOOD_HEAD))
    if rng:
        req.add_header("Range", f"bytes={rng[0]}-{rng[1]}")
    return urllib.request.urlopen(req, timeout=timeout)


def open_direct(url):
    """打开直链，必要时换几组请求头再试。返回 (响应, 用的是哪组头) 或 (None, "")。

    【必须带 Range，不然测的不是播放器走的那条路】实测阿里云盘：同一条直链，
    带 Range 拿到 206、2 秒 1 MiB；不带 Range 直接 403。而播放器【永远带
    Range】—— 要 seek、要分段拉。不带 Range 去探，探到的 403 是自己造出来的，
    据此报"这个片子放不了"是彻头彻尾的误判。

    这个坑我踩过一次：先报了防盗链（403），又拿另一个带 -r 的脚本去测，
    五种请求头全部 206 —— 两份结果打架，差别只在这一个头上。
    """
    first = None
    # 播放器开头就是这么拉的：要前 1 MiB
    probe_rng = (0, 1024 * 1024 - 1)
    for label, head in HEADSETS:
        try:
            r = get(url, rng=probe_rng, timeout=60, head=head)
            GOOD_HEAD.clear(); GOOD_HEAD.update(head)
            return r, label
        except Exception as e:
            first = first or f"{label} → {e}"
            continue
    # 带 Range 都不行，再试一次不带的 —— 万一是这个源不认 Range
    try:
        r = get(url, timeout=60, head=HEADSETS[1][1])
        GOOD_HEAD.clear(); GOOD_HEAD.update(HEADSETS[1][1])
        print(f"  ⚠ 这条直链【不认 Range】：带 Range 全被拒，不带反而行")
        print(f"    播放器基本都带 Range，所以这种源在播放器里多半也放不了。")
        return r, HEADSETS[1][0] + "（不带 Range）"
    except Exception:
        pass
    print(f"  ✖ 直链打不开：{first}")
    print(f"    {len(HEADSETS)} 组请求头都试过了（空 UA / 浏览器 UA / 带 Referer），"
          f"带不带 Range 也都试了，全都不行。")
    print(f"    这条直链是网盘刚给的，所以不是过期 —— 是网盘那边拒绝了这次下载。")
    return None, ""

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
              f"这个片子【这会儿根本放不了】——")
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
r, used = open_direct(loc)
if r is None:
    raise SystemExit
try:
    head = r.read(65536)
    ctype = (r.headers.get("Content-Type") or "").lower()
    clen = r.headers.get("Content-Length")
except Exception as e:
    print(f"  ✖ 直链读不动：{e}"); raise SystemExit
if used != HEADSETS[0][0]:
    # 【这一条是结论，不是过程】播放器要是不带这组头，一样 403。
    print(f"  ⚠ 直链要带请求头才拉得动：{BOLD}{used}{RST}")
    print(f"    空手拉是 403。播放器不带这组头也会 403 —— 那就是"
          f"「Emby 里放不了、挂载页面能放」的原因（挂载页面是浏览器，天然带着）。")
    print(f"    修法：OpenList → 存储 → 这个盘 → 打开{BOLD}「Web 代理」{RST}，"
          f"让 OpenList 代拉（代价是流量过一次本机）。")

is_hls = head.lstrip().startswith(b"#EXTM3U") or "mpegurl" in ctype
if not is_hls:
    # 整文件：从第 80 MiB 处拉 16 MiB —— 开头那段往往有缓存，测不出持续速度
    # Content-Range: bytes 0-1048575/11727000000 —— 总大小取斜杠后面那个，
    # Content-Length 在 206 里只是这一段的长度，拿它当总大小会把 off 算成 0
    size = 0
    cr = r.headers.get("Content-Range") or ""
    if "/" in cr:
        try: size = int(cr.rsplit("/", 1)[1])
        except ValueError: size = 0
    size = size or int(clen or 0)
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
    print("  ↑ 要大于上面那个总码率才不卡。")
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
hr
if [ -n "$GOOD" ]; then echo "  「$SLOW」和「$GOOD」并排比"
else echo "  查「$SLOW」"; fi
hr

for ep in "$SLOW" ${GOOD:+"$GOOD"}; do
  line="$(find_item "$ep")"
  id="$(printf '%s' "$line" | cut -f1)"
  nm="$(printf '%s' "$line" | cut -f2)"
  pth="$(printf '%s' "$line" | cut -f3)"
  echo
  if [ "$id" = "NOTFOUND" ] || [ -z "$id" ]; then
    echo "【$ep】在 Emby 里没找到 —— 换个词试试（片名的一部分就行）"
    continue
  fi
  if [ "$id" = "ERR" ]; then echo "【$ep】问 Emby 失败：$nm"; continue; fi
  if [ "$id" = "MANY" ]; then
    echo "【$ep】匹配到 $nm 个，说得再具体一点：$pth"; continue
  fi
  echo "【$ep】$nm"
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
    print("  当前没有在播的会话 —— 想抓转码的话，让那个片子放着，再跑一次这个脚本")
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
    · 换直链 ✖（404 / 超时）              → 这个片子【当下根本放不了】，点开就转圈。
                                            换直链是每次播放都要走一遍的。
                                            多半是网盘接口在限流 ——
                                            跑「5 体检」看「列目录历史」那张图
    · 有分片拉失败                        → 播的时候就是卡在那几个分片上
    · 播放方式不是 DirectPlay             → Emby 在转码，2 核的机器扛不住。
                                            多半是编码客户端不认
                                            （HEVC 10bit / AV1 / 特殊音轨）
    · 各项都正常、就是卡                  → 不是这个片子的问题，是网盘那会儿在限流
TIP
echo

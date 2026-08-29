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
print(f"  容器      {g('Container','?')}")
print(f"  文件大小  {size/1024/1024:.0f} MiB" if size else "  文件大小  未知")
print(f"  时长      {dur/60:.1f} 分钟" if dur else "  时长      未知（时长为 0 会让续播点存不下来）")
if br: print(f"  总码率    {br/1_000_000:.2f} Mbps   <<< 两集差得多的话，卡就是它")
elif size and dur: print(f"  总码率    {size*8/dur/1_000_000:.2f} Mbps（按大小/时长算）  <<< 两集差得多的话，卡就是它")
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

# ---- 302 + 实测下载速度 ----
probe() {       # $1=ItemId
  local iid="$1"
  local url="$MW/Videos/$iid/stream?MediaSourceId=mediasource_${iid}&Static=true&api_key=$KEY"
  local t0 t1 code loc
  t0=$(date +%s.%N)
  # -D - 拿响应头（要的是 302 的 Location），-o /dev/null 扔掉正文
  local hdr; hdr="$(curl -s -m 90 -D - -o /dev/null "$url" 2>/dev/null)"
  t1=$(date +%s.%N)
  code="$(printf '%s' "$hdr" | sed -nE 's#^HTTP/[0-9.]+ ([0-9]+).*#\1#p' | tail -1)"
  loc="$(printf '%s' "$hdr" | sed -nE 's/^[Ll]ocation:[[:space:]]*(.*)$/\1/p' | tr -d '\r' | tail -1)"
  printf "  换直链    %.1f 秒   HTTP %s\n" "$(echo "$t1 - $t0" | bc)" "${code:-?}"
  if [ -z "$loc" ]; then
    echo "  ✖ 没拿到 302 —— 视频会经过本机中转，那是必卡的"
    return
  fi
  echo "  直链节点  $(printf '%s' "$loc" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
  # 从【中间】拉 16MiB：开头那段网盘往往有缓存，测不出真实持续速度
  local off=$((80 * 1024 * 1024))
  local end=$((off + 16 * 1024 * 1024 - 1))
  local out
  out="$(curl -s -m 120 -o /dev/null -r "${off}-${end}" \
         -w '%{speed_download} %{size_download} %{time_starttransfer}' "$loc" 2>/dev/null)"
  local spd sz ttfb
  spd="$(printf '%s' "$out" | awk '{print $1}')"
  sz="$(printf '%s' "$out" | awk '{print $2}')"
  ttfb="$(printf '%s' "$out" | awk '{print $3}')"
  if [ -z "${sz:-}" ] || [ "${sz:-0}" = "0" ]; then
    echo "  ✖ 直链拉不动（拉了 0 字节）—— 这条线就是坏的"
    return
  fi
  printf "  首字节    %.2f 秒\n" "${ttfb:-0}"
  printf "  实测速度  %.2f MB/s  = %.1f Mbps  （从第 80 MiB 处拉了 %s MiB）\n" \
    "$(echo "$spd/1048576" | bc -l)" "$(echo "$spd*8/1000000" | bc -l)" \
    "$(echo "$sz/1048576" | bc -l | cut -c1-4)"
  echo "  ↑ 这个数要【大于上面那个总码率】才不卡。小于就是卡的直接原因。"
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
    · 两集的【总码率】差一倍以上          → 就是片源重，跟脚本无关。
                                            去「3 后补参数 → 3」把直链方式
                                            从「转码流」换成「原画」反而更稳，
                                            或者反过来试
    · 卡的那集【实测速度 < 总码率】       → 边放边等，必卡。看是不是落到了
                                            另一个直链节点（上面那行「直链节点」）
    · 播放方式不是 DirectPlay             → Emby 在转码，2 核的机器扛不住。
                                            多半是那一集的编码客户端不认
                                            （HEVC 10bit / AV1 / 特殊音轨）
    · 两集各项都差不多、就是卡            → 那就不是这一集的问题，是网盘那会儿
                                            在限流。跑「5 链路体检」看
                                            「列目录历史」那张探测图
TIP
echo

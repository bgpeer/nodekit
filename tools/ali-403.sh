#!/usr/bin/env bash
# 阿里云盘直链 403：分清是 IPv6 的事、还是阿里拒绝这台机器下载。只读。
set -u

TOOL_VER="2026-08-30c"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"
DIR="${MS_DIR:-/opt/media-stack}"
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
Q="${1:-龙虎门}"

ID="$(python3 - "$KEY" "$Q" <<'PY'
import json,sys,urllib.request
key,q=sys.argv[1],sys.argv[2]
u=(f"http://127.0.0.1:8096/Items?Recursive=true"
   f"&IncludeItemTypes=Movie,Episode,Video&Fields=Path&Limit=2000&api_key={key}")
for i in (json.load(urllib.request.urlopen(u,timeout=60)).get("Items") or []):
    p=str(i.get("Path") or "")
    if p.endswith(".strm") and (q in str(i.get("Name") or "") or q in p):
        print(i.get("Id")); break
PY
)"
[ -n "$ID" ] || { echo "✖ 没找到「$Q」"; exit 1; }

LOC="$(curl -s -o /dev/null -w '%{redirect_url}' -m 60 \
  "http://127.0.0.1:9000/Videos/$ID/stream?MediaSourceId=mediasource_$ID&Static=true&api_key=$KEY")"
[ -n "$LOC" ] || { echo "✖ 没拿到 302"; exit 1; }
echo "直链主机  $(printf '%s' "$LOC" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
echo

# 【把秒数换算成速度，不要让人自己去除】原来这里只打 "10.041734s"，
# 一屏五行秒数，得自己拿 1 MiB 去除才知道快慢 —— 而这一步恰恰是全场最关键的
# 结论所在，不该留给人心算。
RES="$(mktemp)"
trap 'rm -f "$RES"' EXIT

t() {  # $1=说明  剩下的=curl 参数
  local lbl="$1"; shift
  local out code size tt ip spd
  out="$(curl -s -o /dev/null -m 45 -r 0-1048575 \
         -w '%{http_code} %{size_download} %{time_total} %{remote_ip}' \
         "$@" "$LOC" 2>&1)"
  set -- $out
  code="${1:-?}"; size="${2:-0}"; tt="${3:-0}"; ip="${4:-}"
  spd="$(awk -v s="$size" -v t="$tt" 'BEGIN{
      if (t+0>0 && s+0>0) printf "%6.0f KB/s  %5.2f Mbps", s/t/1024, s*8/t/1000000
      else printf "%-24s", "—" }')"
  printf '  %-22s %s  %s  %s\n' "$lbl" "$code" "$spd" "$ip"
  # 只有真拉下来的才进统计 —— 403 那种 0 字节 0 秒会把平均值带歪
  case "$code" in 200|206) echo "$tt $size" >> "$RES" ;; esac
}
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
echo "  说明：200/206 = 拉得动；403 = 被拒。每条都拉 1 MiB。最后一列是实际连到的 IP"
echo "  ⚠ 测的时候【别同时播放同一部片子】—— 会和它抢同一份带宽，数字不作数"
echo
t "默认（自选 v4/v6）"
t "强制 IPv4"                 -4
t "强制 IPv6"                 -6
t "IPv4 + 浏览器UA"           -4 -A "$UA"
t "IPv4 + UA + Referer"       -4 -A "$UA" -e "https://www.alipan.com/"
echo

# 【几条不同的路速度几乎一样 = 限速，不是线路】线路慢是随机的：换 IP、换协议、
# 换 CDN 节点，快慢一定会散开。而令牌桶限速是按秒发牌的，走哪条路都发一样多，
# 于是几次测下来齐刷刷落在同一个数上 —— 这个"齐"本身就是证据。
awk '{ if ($1+0>0 && $2+0>0) { v=$2/$1/1024; n++; s+=v
         if (n==1||v<mn) mn=v; if (v>mx) mx=v } }
     END {
       if (n < 3) exit
       printf "  五条路里成功 %d 条，%.0f ~ %.0f KB/s（平均 %.0f）\n", n, mn, mx, s/n
       if (mn > 0 && (mx-mn)/mn < 0.30) {
         printf "  ▸ 走哪条路都是这个数（差 %.0f%%）—— 这是【限速】，不是线路问题。\n",
                (mx-mn)/mn*100
         printf "    换机房、换 IP、走 IPv4 还是 IPv6 都改不了它。\n"
       } else {
         printf "  ▸ 各条路差得比较开（%.0f%%）—— 更像线路/节点的事，不像固定限速。\n",
                (mx-mn)/mn*100
       }
     }' "$RES"
echo
echo "  ---- 把下面这条链接复制到【手机浏览器】打开（手机是国内 IP）----"
echo "  这条就是 MediaWarp 302 给播放器的那一条，手机上看两件事："
echo
echo "  ① 能不能下"
echo "     能下   = 阿里按 IP 判，这台机器被地区限制了"
echo "     也 403 = 跟 IP 无关，是这个文件/这次授权的问题"
echo
echo "  ② 有多快（【别拿云盘 App 的下载速度当参照】）"
echo "     App 走的是阿里自己的通道，这条链接是第三方接口发的，两码事。"
echo "     要比就得比同一条链接：手机浏览器拉它有多快、这台机器拉它有多快。"
echo "     手机快、机器慢 = 线路问题（而 302 时流量走播放器，未必碍事）"
echo "     两边都慢     = 这条链接本身被限速，换线路没用"
echo
printf '%s\n' "$LOC"

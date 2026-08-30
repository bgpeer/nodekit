#!/usr/bin/env bash
# 阿里云盘直链 403：分清是 IPv6 的事、还是阿里拒绝这台机器下载。只读。
set -u
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

t() {  # $1=说明  剩下的=curl 参数
  local lbl="$1"; shift
  local out
  out="$(curl -s -o /dev/null -m 45 -r 0-1048575 \
         -w '%{http_code} %{size_download}B %{time_total}s %{remote_ip}' \
         "$@" "$LOC" 2>&1)"
  printf '  %-26s %s\n' "$lbl" "$out"
}
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
echo "  说明：200/206 = 拉得动；403 = 被拒。最后一列是实际连到的 IP"
t "默认（系统自选 v4/v6）"
t "强制 IPv4"                 -4
t "强制 IPv6"                 -6
t "IPv4 + 浏览器UA"           -4 -A "$UA"
t "IPv4 + UA + Referer"       -4 -A "$UA" -e "https://www.alipan.com/"
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

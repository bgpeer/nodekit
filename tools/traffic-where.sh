#!/usr/bin/env bash
# 这台机的流量到底跑在哪儿。只读，不改任何东西。
#
#   bash traffic-where.sh          按来源拆一遍 + 实时采样 30 秒
#   bash traffic-where.sh 120      实时采样改成 120 秒（挂着看更准）
#
# 【为什么要有这个】"流量跑超了"是个结果，不是原因。这台机上同时有两套东西在用网：
# 代理节点（sing-box/xray）和自建 Emby（OpenList 去网盘拉东西）。光看 vnstat 的
# 总数分不出是谁，于是只能猜——猜错了就去限一个本来就很小的东西。
#
# 【累计数定不了案，一定要看实时】docker 给的是「容器启动至今」，vnstat 给的是
# 「今天/本月」——两个时间窗根本对不齐，摆在一起比会得出错误结论。所以这一版
# 把容器的运行时长打出来（好折算成每天多少），并且加了一段【实时采样】：
# 同时量物理网卡和每个容器，谁在跑一目了然。
set -u

TOOL_VER="2026-09-14b"
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
SAMPLE="${1:-30}"
B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; X="\033[0m"

hr()  { printf '%s\n' "------------------------------------------------------------"; }
sec() { echo; printf "${B}%s${X}\n" "$1"; hr; }
# 物理口的收发字节（排除 lo / docker / br- / veth / tun）
phy() { awk '/:/ {split($0,a,":"); n=a[1]; gsub(/ /,"",n);
               if (n=="lo" || n ~ /^(docker|br-|veth|tun|sing)/) next;
               split(a[2],f," "); rx+=f[1]; tx+=f[9]}
             END {print rx" "tx}' /proc/net/dev; }

sec "① 实时采样 ${SAMPLE} 秒：现在是谁在跑"
echo -e "  ${D}这一段是唯一能直接定案的——累计数的时间窗对不齐，比不了。${X}"
read -r RX0 TX0 <<<"$(phy)"
declare -A C0
if command -v docker >/dev/null 2>&1; then
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
    pid=$(docker inspect -f '{{.State.Pid}}' "$c" 2>/dev/null) || continue
    [ -n "${pid:-}" ] && [ "$pid" != 0 ] || continue
    C0[$c]=$(awk '/eth0|ens/ {split($0,a,":"); split(a[2],f," "); print f[1]; exit}' \
             "/proc/$pid/net/dev" 2>/dev/null || echo 0)
  done
fi
printf "  采样中"; for _ in $(seq 1 "$((SAMPLE/5))"); do sleep 5; printf "."; done; echo
read -r RX1 TX1 <<<"$(phy)"
awk -v a="$RX0" -v b="$RX1" -v c="$TX0" -v d="$TX1" -v s="$SAMPLE" '
  BEGIN {rx=(b-a)/s; tx=(d-c)/s;
         printf "  物理网卡      ↓%.2f MB/s  ↑%.2f MB/s   （按这个速度一天 ↓%.0f GB）\n",
                rx/1048576, tx/1048576, rx*86400/1073741824}'
for c in "${!C0[@]}"; do
  pid=$(docker inspect -f '{{.State.Pid}}' "$c" 2>/dev/null) || continue
  now=$(awk '/eth0|ens/ {split($0,a,":"); split(a[2],f," "); print f[1]; exit}' \
        "/proc/$pid/net/dev" 2>/dev/null || echo 0)
  awk -v n="$c" -v a="${C0[$c]}" -v b="$now" -v s="$SAMPLE" '
    BEGIN {r=(b-a)/s; if (r > 10240) printf "  %-14s 收 %.2f MB/s   （一天 %.0f GB）\n",
           n, r/1048576, r*86400/1073741824}'
done
echo -e "  ${D}只列收得动的（>10 KB/s）。没列出来的就是这会儿没在跑。${X}"

sec "② 物理网卡的总账"
if command -v vnstat >/dev/null 2>&1; then
  vnstat --oneline 2>/dev/null | awk -F';' '
    NF>10 {printf "  今天   ↓%-11s ↑%-11s 合计 %s\n", $4, $5, $6;
           printf "  本月   ↓%-11s ↑%-11s 合计 %s\n", $9, $10, $11}'
fi
awk '/:/ {split($0,a,":"); n=a[1]; gsub(/ /,"",n);
          if (n=="lo" || n ~ /^(docker|br-|veth|tun|sing)/) next;
          split(a[2],f," ");
          printf "  %-8s 开机以来 ↓%.1f GB  ↑%.1f GB\n", n, f[1]/1073741824, f[9]/1073741824}' \
  /proc/net/dev
echo
echo -e "  ${D}代理转发是收多少发多少，正常 ↑≈↓。↓ 远大于 ↑ 的那部分 = 拉进来没发出去。${X}"

sec "③ 每个容器（注意：是【容器启动至今】，不是今天）"
if command -v docker >/dev/null 2>&1; then
  docker ps --format '{{.Names}}\t{{.RunningFor}}' 2>/dev/null | sort | while IFS=$'\t' read -r n up; do
    io=$(docker stats --no-stream --format '{{.NetIO}}' "$n" 2>/dev/null)
    printf "  %-14s %-22s 已跑 %s\n" "$n" "${io:-?}" "$up"
  done
  echo
  echo -e "  ${D}openlist 的【收】就是它从网盘拉的量。它和【发】的差 = 拉下来没交付出去的。"
  echo -e "  ${Y}拿这一栏跟②比之前先看「已跑多久」——容器重启过的话计数是清零的。${X}"
fi

sec "④ nginx 日志按 User-Agent 拆（今天）"
for LOG in /var/log/nginx/media-stack.access.log "$DIR"/nginx/logs/access.log; do
  [ -f "$LOG" ] || continue
  echo -e "  ${B}$LOG${X}"
  awk -v d="$(date +%d/%b/%Y)" '
    index($0, d) == 0 {next}
    { b = $10 + 0; ua = tolower($0)
      if (ua ~ /lavf\/|ffmpeg/)                 k = "ffprobe（Emby 探测）"
      else if (ua ~ /infuse|vidhub|senplayer|mpv|vlc|exoplayer|emby|fileball/) k = "播放器"
      else if (ua ~ /mediawarp|openlist|alist/) k = "内部组件"
      else                                       k = "其它"
      s[k] += b; c[k]++ }
    END { if (!length(s)) {print "    今天还没有记录"; exit}
          for (k in s) printf "    %-22s %8.2f GB  %7d 次\n", k, s[k]/1073741824, c[k] }' \
    "$LOG" | sort -k2 -rn
  echo
done
[ -f /var/log/nginx/media-stack.access.log ] || \
  echo -e "  ${Y}找不到 /var/log/nginx/media-stack.access.log —— 媒体那侧的日志就在这个文件，"\
          "\n  别拿 access.log（那是节点订阅那侧的，跟媒体流量无关）。${X}"

sec "⑤ 补时长（heal）自己记的账"
python3 - <<'PY' 2>/dev/null || echo "  （读不到 /etc/bgpeer/media-stack.json）"
import json
st = json.load(open("/etc/bgpeer/media-stack.json"))
day, seen, fail = (st.get("heal_day") or {}), (st.get("heal_seen") or {}), (st.get("heal_fail") or {})
if day:
    print(f"  {day.get('date','?')}  用掉 {float(day.get('mb') or 0):.0f} MB，"
          f"探了 {day.get('probes', 0)} 次")
else:
    print("  今天还没探过（或者脚本还没更新到 1.5.96+，没这个计数器）")
print(f"  放弃名单 {len(fail)} 条（探不出来的，不再每天重复探）")
if seen:
    print(f"  上次基准：{seen.get('date')}  待探 {seen.get('pending')} 个")
PY

sec "⑥ 还有哪些地方会用网（按量从大到小）"
cat <<'TXT'
  补时长(heal)   每条约 6.7 MB（上游算约 18 MB）。有日上限 HEAL_DAY_MB 管着
  首次刮削       海报/剧照，一部几百 KB~几 MB，只在新片进库时，一次性
  0 轨道的条目   每次【播放】前 Emby 要现场探一次，又是几 MB
                 —— 补好时长之后就不再发生，这正是 heal 值得做的理由
  AutoFilm 扫库  只列目录、写 strm，不读视频内容，很小
  预热直链       64 KB/部 × 10 部/轮 ≈ 15 MB/天
  镜像更新       docker pull，一次几百 MB，只在点『7 更新』时
  看片           0 —— MediaWarp 302 把播放器直接指去网盘，视频流不经过这台机
TXT
echo

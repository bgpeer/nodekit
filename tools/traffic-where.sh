#!/usr/bin/env bash
# 这台机的流量到底跑在哪儿。只读，不改任何东西。
#
#   bash traffic-where.sh          按来源拆一遍
#
# 【为什么要有这个】"流量跑超了"是个结果，不是原因。而这台机上同时有两套东西
# 在用网：代理节点（sing-box/xray）和自建 Emby（OpenList 去网盘拉东西）。
# 光看 vnstat 的总数分不出是谁，于是只能猜 —— 猜错了就去限一个本来就很小的东西。
#
# 这个脚本把它拆成四层，每一层都是【实测的计数器】，不是估算：
#   ① 物理网卡      这台机对外一共收发了多少（vnstat / /proc/net/dev）
#   ② 每个容器      openlist 从网盘拉了多少、emby 收了多少（docker 自己的计数）
#   ③ nginx 日志    按 User-Agent 分：ffprobe(探测) / 播放器 / 其它
#   ④ heal 自己     脚本记的当天额度用了多少
#
# 【怎么读这份报告】关键是看①的收发比。代理节点转发是【收多少发多少】，
# 所以正常情况 ↑≈↓。要是 ↓ 远大于 ↑，那多出来的部分就是"拉进来没发出去"的
# —— 那只可能是 OpenList 从网盘拉的（探测/预热），不是代理。
set -u

TOOL_VER="2026-09-14a"
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; C="\033[36m"; X="\033[0m"

hr() { printf '%s\n' "------------------------------------------------------------"; }
sec() { echo; printf "${B}%s${X}\n" "$1"; hr; }

sec "① 物理网卡：这台机一共收发了多少"
if command -v vnstat >/dev/null 2>&1; then
  vnstat --oneline 2>/dev/null | awk -F';' '
    NF>10 {printf "  今天   ↓%-10s ↑%-10s 合计 %s\n", $4, $5, $6;
           printf "  本月   ↓%-10s ↑%-10s 合计 %s\n", $9, $10, $11}'
else
  echo "  （没装 vnstat，用开机以来的累计值）"
fi
awk '/:/ {split($0,a,":"); n=a[1]; gsub(/ /,"",n);
          if (n=="lo" || n ~ /^(docker|br-|veth|tun|sing)/) next;
          split(a[2],f," ");
          printf "  %-8s 开机以来 ↓%.1f GB  ↑%.1f GB\n", n, f[1]/1073741824, f[9]/1073741824}' \
  /proc/net/dev

echo
echo -e "  ${D}怎么读：代理转发是收多少发多少，正常 ↑≈↓。"
echo -e "  ↓ 远大于 ↑ 的那部分 = 拉进来没发出去 = OpenList 去网盘拉的，不是代理。${X}"

sec "② 每个容器各自收发了多少（docker 自己的计数，容器启动至今）"
if command -v docker >/dev/null 2>&1; then
  docker stats --no-stream --format '{{.Name}}\t{{.NetIO}}' 2>/dev/null \
    | awk -F'\t' '{printf "  %-22s %s\n", $1, $2}' | sort
  echo
  echo -e "  ${D}openlist 的【收】就是它从网盘拉下来的量 —— 这台机流量的大头基本都在这一行。"
  echo -e "  它的【发】是给本机的 Emby/MediaWarp，走 docker 内网，不出物理网卡。"
  echo -e "  两者的差 = 拉下来却没交付出去的（探测拉到一半就断，上游那段已经发生了）。${X}"
else
  echo "  （没有 docker）"
fi

sec "③ nginx 日志按 User-Agent 拆（今天）"
LOG=""
for p in "$DIR"/nginx/logs/access.log /var/log/nginx/access.log; do
  [ -f "$p" ] && LOG="$p" && break
done
if [ -n "$LOG" ]; then
  TODAY="$(date +%d/%b/%Y)"
  awk -v d="$TODAY" '
    index($0, d) == 0 {next}
    {
      # 取响应字节数：combined 格式里是 $10
      b = $10 + 0
      ua = tolower($0)
      if (ua ~ /lavf\/|ffmpeg/)                 k = "ffprobe（Emby 探测）"
      else if (ua ~ /infuse|vidhub|senplayer|mpv|vlc|exoplayer|emby|fileball/) k = "播放器"
      else if (ua ~ /mediawarp|openlist|alist/) k = "内部组件"
      else                                       k = "其它"
      s[k] += b; c[k]++
    }
    END {
      for (k in s) printf "  %-22s %8.2f GB   %6d 次\n", k, s[k]/1073741824, c[k]
    }' "$LOG" | sort -k2 -rn
  echo
  echo -e "  ${D}日志：$LOG${X}"
  echo -e "  ${D}注意这是 nginx【发出去】的字节。探测那一行 × 2~3 才是从网盘拉进来的量"
  echo -e "  （拉到一半就断，上游那段已经发生了，但 nginx 没发完）。${X}"
else
  echo "  （找不到 nginx 访问日志）"
fi

sec "④ 补时长（heal）自己记的账"
python3 - "$DIR" <<'PY' 2>/dev/null || echo "  （读不到 state.json）"
import json, os, sys, time
d = sys.argv[1]
try:
    st = json.load(open(os.path.join(d, "state.json")))
except Exception:
    raise SystemExit(1)
day = st.get("heal_day") or {}
seen = st.get("heal_seen") or {}
fail = st.get("heal_fail") or {}
if day:
    print(f"  {day.get('date','?')}  用掉 {float(day.get('mb') or 0):.0f} MB，"
          f"探了 {day.get('probes', 0)} 次")
else:
    print("  今天还没探过（或者还是老版本，没这个计数器）")
print(f"  放弃名单里有 {len(fail)} 条（探不出来的，不再每天重复探）")
if seen:
    print(f"  上次记的基准：{seen.get('date')}  待探 {seen.get('pending')} 个")
PY

sec "⑤ 还有哪些地方会用网（按量从大到小）"
cat <<'TXT'
  补时长(heal)   每条约 6.7 MB（上游算约 18 MB）。有日上限 HEAL_DAY_MB 管着
  首次刮削       海报/剧照，一部几百 KB ~ 几 MB。只在新片进库时发生，一次性
  0 轨道的条目   每次【播放】前 Emby 要现场探一次，又是一次几 MB
                 ——  这些条目补好时长之后就不再发生，是 heal 值得做的理由
  AutoFilm 扫库  只列目录、写 strm，不读视频内容，很小
  预热直链       64 KB/部 × 10 部/轮 ≈ 15 MB/天
  镜像更新       docker pull，一次几百 MB，只在点『7 更新』时
  规则集/图标    节点那侧每天更新，几十 MB 级
  看片           0 —— MediaWarp 302 把播放器直接指去网盘，视频流不经过这台机
TXT
echo

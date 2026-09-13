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

TOOL_VER="2026-09-14f"
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

sec "④b 今天的探测是什么形状：一阵猛扫，还是全天在循环"
echo -e "  ${D}这一栏才分得出「是谁在探」：集中在一两个小时 = 某次扫描/重启触发的；"
echo -e "  每小时都有 = 有东西挂在定时任务上一直探。${X}"
LOGM=/var/log/nginx/media-stack.access.log
if [ -f "$LOGM" ]; then
  awk -v d="$(date +%d/%b/%Y)" '
    index($0, d) == 0 {next}
    tolower($0) ~ /lavf\/|ffmpeg/ {
      # 时间戳形如 [14/Sep/2026:03:21:07 +0000]，取小时
      if (match($0, d":[0-9][0-9]")) { h = substr($0, RSTART+length(d)+1, 2); n[h]++; b[h]+=$10+0 }
    }
    END { if (!length(n)) {print "    今天没有 ffprobe 记录"; exit}
          for (h=0; h<24; h++) { k=sprintf("%02d",h); if (!(k in n)) continue
            bar=""; w=int(n[k]/20); if (w>40) w=40
            for (i=0;i<w;i++) bar=bar"#"
            printf "    %s 时  %5d 次  %6.2f GB  %s\n", k, n[k], b[k]/1073741824, bar }
        }' "$LOGM"
else
  echo "    （找不到 $LOGM）"
fi

sec "④d 这些探测打在哪些文件上：是在循环，还是在走全库"
echo -e "  ${D}同一批文件被反复探 = 有东西在循环（每小时重来一遍）；"
echo -e "  几乎每个文件只出现一两次 = 在走全库（扫描/刷新那一类）。${X}"
if [ -f "$LOGM" ]; then
  awk -v d="$(date +%d/%b/%Y)" '
    index($0, d) == 0 {next}
    tolower($0) ~ /lavf\/|ffmpeg/ {
      p = $7; sub(/\?.*/, "", p)      # 去掉 ?sign=… 那截，不然同一文件算成多个
      n[p]++; tot++
      if (match($0, d":[0-9][0-9]")) { h = substr($0, RSTART+length(d)+1, 2); hs[p" "h]=1 }
    }
    END {
      if (!tot) {print "    今天没有 ffprobe 记录"; exit}
      u = 0; rep = 0
      for (p in n) { u++; if (n[p] > 1) rep++ }
      printf "    探了 %d 次，落在 %d 个不同文件上（平均每个 %.1f 次）\n", tot, u, tot/u
      printf "    被探过不止一次的：%d 个（占 %.0f%%）\n", rep, rep*100/u
      # 跨了几个小时 = 更像循环，而不是一次扫描里的重试
      for (k in hs) { split(k, a2, " "); span[a2[1]]++ }
      m = 0; for (p in span) if (span[p] >= 3) m++
      printf "    跨 3 个以上不同小时被探的：%d 个 %s\n", m,
             (m > u/10 ? "← 像是在循环" : "← 不像循环")
      print "    探得最多的前 8 个："
      c = 0
      for (p in n) if (n[p] > 1) { printf "      %3d 次  %s\n", n[p], substr(p, 1, 64); if (++c >= 8) break }
      if (!c) print "      （没有一个文件被探过两次——说明是在走全库，不是循环）"
    }' "$LOGM"
else
  echo "    （找不到 $LOGM）"
fi

sec "④e strm 文件是不是被改过：Emby 重探老片只有这两个原因"
echo -e "  ${D}Emby 只会重探【它认为变了】的文件。所以 mtime 能把原因一刀切开："
echo -e "  大批 mtime 是今天 → 有东西在重写 strm，Emby 因此重探（该修脚本）；"
echo -e "  mtime 都是很久以前 → strm 没动过，是 Emby 自己在重探（该改 Emby 设置）。${X}"
STRM_ROOT=""
for r in "$DIR"/media/strm "$DIR"/strm; do [ -d "$r" ] && STRM_ROOT="$r" && break; done
export STRM_ROOT
if [ -n "$STRM_ROOT" ]; then
  echo "  strm 根目录：$STRM_ROOT"
  find "$STRM_ROOT" -name '*.strm' -printf '%T@ %p\n' 2>/dev/null | awk -v now="$(date +%s)" '
    { age = (now - $1) / 86400
      if (age < 1)       k = "今天改过"
      else if (age < 2)  k = "昨天改过"
      else if (age < 8)  k = "一周内"
      else if (age < 31) k = "一个月内"
      else               k = "一个月以上"
      n[k]++; tot++
      # 顺带按第一层目录分，看是不是集中在某个盘
      split($2, a2, "/"); for (i=1; i<=length(a2); i++) if (a2[i] != "" && i > 4) { lib[a2[i]]++; break }
    }
    END { if (!tot) {print "    一个 strm 都没找到"; exit}
          printf "    共 %d 个 strm\n", tot
          for (k in n) printf "      %-12s %6d 个 (%.0f%%)\n", k, n[k], n[k]*100/tot
          print "    按盘分（前 6 个）："
          c = 0; for (l in lib) { printf "      %-28s %6d\n", substr(l,1,26), lib[l]; if (++c >= 6) break } }'
else
  echo "    （找不到 strm 目录，在 $DIR/media/strm 或 $DIR/strm 下面）"
fi

sec "④f AutoFilm 实际部署的那份配置：overwrite 是不是 false"
echo -e "  ${D}模板里现在是 overwrite: false，但配置是【装机那天】写下的——"
echo -e "  老版本装的机器可能还是 true，那就每天把全部 strm 重写一遍，"
echo -e "  Emby 看到 mtime 全变就重新探测全库。${X}"
AF=""
for f in "$DIR"/autofilm/config/config.yaml "$DIR"/autofilm/config.yaml; do
  [ -f "$f" ] && AF="$f" && break
done
if [ -n "$AF" ]; then
  echo "  配置：$AF"
  grep -nE "^\s*(id|overwrite|cron|source_dir):" "$AF" 2>/dev/null \
    | sed 's/^/    /' | head -40
  BAD=$(grep -cE "^\s*overwrite:\s*[Tt]rue" "$AF" 2>/dev/null || echo 0)
  OKN=$(grep -cE "^\s*overwrite:\s*[Ff]alse" "$AF" 2>/dev/null || echo 0)
  echo
  if [ "${BAD:-0}" -gt 0 ]; then
    echo -e "  ${R}✗ 有 $BAD 个任务是 overwrite: true —— 就是它每天把 strm 全重写一遍。${X}"
    echo -e "  ${Y}    进菜单 16 点『7 更新』会按新模板重写这份配置（overwrite 改成 false）。${X}"
  elif [ "${OKN:-0}" -gt 0 ]; then
    echo -e "  ${G}✓ $OKN 个任务都是 overwrite: false${X}"
    echo -e "  ${Y}    那 strm 的 mtime 还天天变就是别的东西在写，看 ④e 的分布和下面的时刻。${X}"
  fi
else
  echo "    （找不到 AutoFilm 配置）"
fi
echo
echo "  最近被改过的 strm，改在什么时刻（看是不是都挤在 AutoFilm 那一轮）："
if [ -n "${STRM_ROOT:-}" ]; then
  find "$STRM_ROOT" -name '*.strm' -newermt '-26 hours' -printf '%TH\n' 2>/dev/null \
    | sort | uniq -c | awk '{printf "    %s 时  %d 个\n", $2, $1}'
fi

sec "④c Emby 自己的定时任务最近跑了什么"
python3 - <<'PY' 2>/dev/null || echo "  （问不到 Emby，跳过）"
import json, os, re, urllib.request
cfg = "/opt/media-stack/mediawarp/config/config.yaml"
key = ""
try:
    m = re.search(r"^\s*auth:\s*([^\s#]+)", open(cfg).read(), re.M)
    key = m.group(1) if m else ""
except OSError:
    pass
if not key:
    raise SystemExit(1)
url = f"http://127.0.0.1:8096/ScheduledTasks?api_key={key}"
tasks = json.load(urllib.request.urlopen(url, timeout=20))
rows = []
for t in tasks:
    lr = t.get("LastExecutionResult") or {}
    end = (lr.get("EndTimeUtc") or "")[:16].replace("T", " ")
    rows.append((end, t.get("Name", "?"), lr.get("Status", ""), t.get("State", "")))
rows.sort(reverse=True)
print("  最近跑过的（时间倒序，只列前 8 个）：")
for end, nm, st, state in rows[:8]:
    run = "  ← 正在跑" if state == "Running" else ""
    print(f"    {end or '(没跑过)':17} {nm[:34]:34} {st}{run}")
print()
print("  ⚠ 对 strm 库来说，会去【读视频文件】的任务只有扫描媒体库那一类。")
print("    它一跑就是几千次 ffprobe —— 每次都要从网盘拉一段文件头。")
PY

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

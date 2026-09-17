#!/usr/bin/env bash
# 这台机的 MTU 到底是多少。只读，不改任何东西。
#
#   bash mtu-check.sh              量本机网卡 + 出网方向的路径 MTU
#   bash mtu-check.sh 1.2.3.4      再加量一个指定目标（比如你家宽的出口 IP）
#
# 【为什么要有这个】"VPS 冲不过 1500" 是个结论，但结论后面有三种完全不同的病，
# 处方也完全不同：
#
#   ① 网卡自己就不是 1500（机房套了隧道，常见 1450/1442）
#        → 那是事实，不是故障。往外发的包本来就该按这个数来。
#   ② 网卡是 1500，但路上某一跳更小，而且 ICMP 被挡 = PMTU 黑洞
#        → 小包通、大包卡死（表现：网页开一半、SSH 打字正常 cat 大文件就挂）。
#          治法是在 VPS 上 MSS clamp，【把客户端 TUN 调小治不了这个】。
#   ③ 路径就是 1500，没毛病
#        → 那当初那个 1500 是在治别的病，得重新找。
#
# 【客户端 TUN 的 MTU 和这里量的不是一回事】sing-box 的 TCP/IP stack 把 TCP 连接
# 在客户端本地就终结了，再另开一条连接出去；那条新连接的 MSS 由手机自己的物理网卡
# 和正常 PMTUD 决定，跟 TUN 写多少无关。所以这份报告是用来判断【VPS 这头要不要
# 做 MSS clamp】的，不是用来推算 TUN 该填几的。
#
# 【输出里的公网 IP 会打码】方便你直接截图贴出来问人。
set -u

TOOL_VER="2026-09-17a"
echo "  ${0##*/}  版本 $TOOL_VER"

EXTRA="${1:-}"
B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; X="\033[0m"

hr()  { printf '%s\n' "------------------------------------------------------------"; }
sec() { echo; printf "${B}%s${X}\n" "$1"; hr; }

# 公网 IP 打码：203.0.113.5 -> 203.0.*.*；v6 只留前两段
mask() {
  sed -E -e 's/\b([0-9]{1,3}\.[0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}\b/\1.*.*/g' \
         -e 's/\b([0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}):[0-9a-fA-F:]{4,}/\1:*:*/g'
}

have() { command -v "$1" >/dev/null 2>&1; }

if ! have ping; then
  echo -e "${R}没有 ping，装一下：apt install -y iputils-ping${X}"; exit 1
fi
# -M do（设 DF 位、不许分片）是 iputils 独有的，busybox 的 ping 没有。
# 没有它就只能量网卡、量不了路径 —— 得说清楚，不能默默给个假结果。
PMTU_OK=1
ping -M do -s 64 -c 1 -W 2 -n 127.0.0.1 >/dev/null 2>&1 || PMTU_OK=0

sec "① 本机网卡的 MTU"
echo -e "  ${D}物理口不是 1500 的话，多半是机房套了隧道 —— 那是事实不是故障。${X}"
if have ip; then
  ip -o link show 2>/dev/null | awk '{
      name=$2; sub(/:$/,"",name); sub(/@.*/,"",name);
      for (i=1;i<=NF;i++) if ($i=="mtu") m=$(i+1);
      st="";
      for (i=1;i<=NF;i++) if ($i=="state") st=$(i+1);
      kind = (name=="lo") ? "本地回环" :
             (name ~ /^(docker|br-|veth)/) ? "容器网络" :
             (name ~ /^(tun|wg|sing|nekoray)/) ? "隧道/虚拟" : "物理口";
      printf "  %-14s MTU %-6s %-8s %s\n", name, m, st, kind }'
else
  echo "  没有 ip 命令，跳过"
fi

sec "② 默认出口是哪个网卡、内核记的 PMTU"
if have ip; then
  DEV4=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
  DEV6=$(ip -6 route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')
  echo "  IPv4 默认出口：${DEV4:-无}"
  echo "  IPv6 默认出口：${DEV6:-无}"
  echo
  echo -e "  ${D}下面这几行如果带 'mtu NNNN'，说明内核已经为这条路记下了更小的 PMTU。${X}"
  for t in 1.1.1.1 8.8.8.8; do
    out=$(ip route get "$t" 2>/dev/null | head -2 | tr '\n' ' ')
    # 【目标也走 mask】这里现在填的是公共 DNS、本身不敏感，但规矩一旦写成
    # "看情况"，下次往这个列表里加一个 $EXTRA（用户自己家的出口 IP）就漏了。
    # 统一成"屏上不出现完整 IP"，不用每次再判一遍。
    [ -n "$out" ] && echo "  -> $(echo "$t" | mask) : $(echo "$out" | mask)"
  done
  [ -n "${DEV6:-}" ] && for t in 2606:4700:4700::1111; do
    out=$(ip route get "$t" 2>/dev/null | head -2 | tr '\n' ' ')
    [ -n "$out" ] && echo "  -> $(echo "$t" | mask) : $(echo "$out" | mask)"
  done
fi

# DF 位二分：找出这条路能过的最大整包字节数
# 口径统一成【整个 IP 包】：v4 要减 28（20 IP + 8 ICMP），v6 减 48（40 + 8）
probe() {  # probe <目标> <4|6> <包大小> -> 通了返回 0
  local host=$1 fam=$2 size=$3 ov
  [ "$fam" = 6 ] && ov=48 || ov=28
  ping -"$fam" -M do -s "$((size - ov))" -c 1 -W 2 -n "$host" >/dev/null 2>&1
}
pmtu() {   # pmtu <目标> <4|6> -> 打印能过的最大包，不可达打印空
  local host=$1 fam=$2 lo=1000 hi=9000 mid best=0
  ping -"$fam" -c 1 -W 2 -n "$host" >/dev/null 2>&1 || { echo ""; return; }
  probe "$host" "$fam" "$lo" || { echo "<$lo"; return; }
  best=$lo
  while [ "$lo" -le "$hi" ]; do
    mid=$(( (lo + hi) / 2 ))
    if probe "$host" "$fam" "$mid"; then best=$mid; lo=$((mid + 1));
    else hi=$((mid - 1)); fi
  done
  echo "$best"
}

sec "③ 出网方向的路径 MTU（DF 位二分，每个目标十来个包）"
if [ "$PMTU_OK" = 0 ]; then
  echo -e "  ${Y}这个 ping 不支持 -M do（busybox 版），量不了路径 MTU。${X}"
  echo -e "  ${D}装一下：apt install -y iputils-ping${X}"
else
  echo -e "  ${D}下面的数是【整个 IP 包】的字节数，可以直接跟网卡 MTU 比。${X}"
  echo
  V4="1.1.1.1 8.8.8.8 9.9.9.9"
  [ -n "$EXTRA" ] && V4="$V4 $EXTRA"
  MIN4=""
  for t in $V4; do
    printf "  %-22s " "$(echo "$t" | mask)"
    r=$(pmtu "$t" 4)
    if [ -z "$r" ]; then echo -e "${D}不通（ICMP 被挡，说明不了问题）${X}"
    else
      echo "$r"
      case "$r" in ''|*[!0-9]*) ;; *) { [ -z "$MIN4" ] || [ "$r" -lt "$MIN4" ]; } && MIN4=$r ;; esac
    fi
  done
  if [ -n "${DEV6:-}" ]; then
    echo
    for t in 2606:4700:4700::1111 2001:4860:4860::8888; do
      printf "  %-22s " "$(echo "$t" | mask)"
      r=$(pmtu "$t" 6); [ -z "$r" ] && echo -e "${D}不通${X}" || echo "$r"
    done
  fi
fi

sec "④ 有没有 MSS clamp（治 PMTU 黑洞的正规处方）"
FOUND=0
if have iptables; then
  o=$(iptables -t mangle -S 2>/dev/null | grep -i "TCPMSS" || true)
  [ -n "$o" ] && { FOUND=1; echo "  iptables mangle:"; echo "$o" | sed 's/^/    /' | mask; }
fi
if have nft; then
  o=$(nft list ruleset 2>/dev/null | grep -i "mss" || true)
  [ -n "$o" ] && { FOUND=1; echo "  nftables:"; echo "$o" | sed 's/^/    /' | mask; }
fi
[ "$FOUND" = 0 ] && echo -e "  ${D}没有。路径就是 1500 的话不需要；有黑洞才需要。${X}"

sec "⑤ 怎么读这份报告"
PHY=$(ip -o link show "${DEV4:-}" 2>/dev/null | grep -o 'mtu [0-9]*' | awk '{print $2}')
echo "  物理口 MTU：${PHY:-没读到}    出网实测最小路径 MTU：${MIN4:-没量到}"
echo
# 【两个数各自缺失都要能出结论】没有 ip 命令时 PHY 读不到，但只要 MIN4 量到了
# 就照样能判 —— 拿以太网标称的 1500 当基准比就行。原来写成两个都有才判，
# 于是明明量到了 1420 也只会打一句"没量到"。
case "${MIN4:-x}" in ''|*[!0-9]*) MIN4=""; esac
case "${PHY:-x}"  in ''|*[!0-9]*) PHY="";  esac
BASE="${PHY:-1500}"

if [ -n "$PHY" ] && [ "$PHY" -lt 1500 ]; then
  echo -e "  ${Y}网卡自己就不是 1500（是 $PHY）—— 机房这条线套了隧道。${X}"
  echo -e "  ${D}这是事实不是故障：往外发的包本来就该按 $PHY 来，内核也知道。${X}"
  echo
fi

if [ -z "$MIN4" ]; then
  echo -e "  ${D}路径 MTU 一个都没量到（公网 ICMP 常被整段挡掉）。这【不代表】有问题。"
  echo -e "  换个目标再试：bash ${0##*/} <一个你确定会回 ICMP 的 IP>${X}"
elif [ "$MIN4" -ge "$BASE" ]; then
  echo -e "  ${G}路径 MTU 和网卡一致（都是 $BASE），这条路没有额外缩水，也没有黑洞。${X}"
  echo
  # 【这两种要分开说，不然会自相矛盾】网卡本来就 <1500 的话，"冲不过 1500"
  # 是真的，只是原因在机房隧道、跟故障无关；网卡是 1500 又没缩水，才轮得到
  # "那当初是在治别的病"。上一版两种情况打同一句，在隧道机器上等于自己打自己。
  if [ -n "$PHY" ] && [ "$PHY" -lt 1500 ]; then
    echo -e "  ${D}所以「冲不过 1500」是真的 —— 但原因是机房隧道把网卡压到了 $PHY，"
    echo -e "  不是路上有黑洞。内核和 PMTUD 都已经知道这个数，不用额外做什么。${X}"
  else
    echo -e "  ${D}也就是说「VPS 冲不过 1500」在【这条出网方向】上不成立。"
    echo -e "  当初那个 1500 多半在治别的病。把你当时看到的症状说一下 ——"
    echo -e "  是整条连接卡死？只有某些网站开不了？还是单纯速度上不去？"
    echo -e "  这三种的方向完全不同，值得重新找一次。${X}"
  fi
else
  echo -e "  ${Y}路径 MTU 比网卡小 $((BASE - MIN4)) 字节 —— 路上有一跳更窄。${X}"
  echo
  echo -e "  ${D}不过能量出这个数，本身就说明 ICMP 回来了、PMTUD 是通的 ——"
  echo -e "  真正的黑洞是【量不出来、而且大包直接卡死】。要保险就加 MSS clamp："
  echo -e "  治的是 VPS 这头，跟客户端 TUN 填多少无关。${X}"
  echo "    iptables -t mangle -A POSTROUTING -p tcp --tcp-flags SYN,RST SYN \\"
  echo "      -j TCPMSS --clamp-mss-to-pmtu"
fi
echo
echo -e "  ${D}最后再说一遍：这份报告判的是【VPS 这头要不要 MSS clamp】。"
echo -e "  客户端 TUN 的 MTU 是另一码事 —— sing-box 把 TCP 在客户端本地就终结了，"
echo -e "  再另开一条连接出去，那条的 MSS 由手机物理网卡和 PMTUD 决定。${X}"
echo

#!/usr/bin/env bash
# 这个片子历次播放，MediaWarp 到底把播放器指去了哪 —— 从日志里翻，不猜。
#
#     bash link-history.sh 龙虎门
#     bash link-history.sh 龙虎门 2000     翻更多行（默认 20000 行日志）
#
# 只读，不改任何东西。
#
# 【为什么要有这个】"昨天很流畅、过了一晚上就卡了" —— 这种问题靠当下测一次
# 是答不上来的，当下的状态只有一个。而 MediaWarp 每换一次直链都会把完整地址
# 打进日志，历史全在里面：哪一次指向哪个网盘、是整文件还是 HLS 分片流、
# 中间有没有换过节点。把这些按时间列出来，"什么时候变的"就自己浮出来了。
set -u

Q="${1:-}"
LINES="${2:-20000}"
[ -n "$Q" ] || { echo "用法：bash link-history.sh <片名的一部分> [翻多少行日志]"; exit 1; }

command -v docker >/dev/null 2>&1 || { echo "✖ 没有 docker"; exit 1; }

# 【日志走临时文件，不能用管道】python3 - <<'PY' 是"从 stdin 读程序"，
# 再往 stdin 里管日志进来，两边抢同一个口子：程序读完 heredoc，日志那份就没了。
# 结果是脚本永远报"日志里没有记录"。写完这版第一次跑就撞上了。
LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT
docker logs --tail "$LINES" mediawarp >"$LOG" 2>&1

python3 - "$Q" "$LOG" <<'PY'
import re, sys
from urllib.parse import urlsplit, unquote

Q, LOGFILE = sys.argv[1], sys.argv[2]
B, D, R, G, Y, C, X = ("\033[1m", "\033[2m", "\033[31m", "\033[32m",
                       "\033[33m", "\033[36m", "\033[0m")

# MediaWarp 打的那一行长这样：
#   【INFO】 2026-08-30 05:10:01 | AlistStrm 重定向至：https://dl1-v6.aliyundrive.cloud/...
LINE = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d).*?重定向至：?\s*(\S+)")

# 【失败也要记】这一屏原来只收"重定向至"，也就是只有【成功】的那些。
# 可是"点都点不开"这种症状，证据恰恰是【没有成功记录】和那些 404 ——
# 只看成功记录，屏幕上就是一片空白，跟"这段时间没人播"长得一模一样。
FAIL = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d).*?\|\s*(\d{3})\s*\|"
                  r".*?/Videos/(\d+)/stream")
ANY_TS = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)")

rows, fails, last_ts = [], [], ""
with open(LOGFILE, encoding="utf-8", errors="replace") as f:
    for ln in f:
        m = LINE.search(ln)
        if m:
            rows.append((m.group(1), m.group(2)))
        else:
            fm = FAIL.search(ln)
            if fm and fm.group(2) not in ("200", "204", "206", "302", "304"):
                fails.append((fm.group(1), fm.group(2), fm.group(3)))
        am = ANY_TS.search(ln)
        if am and am.group(1) > last_ts:
            last_ts = am.group(1)

if not rows:
    print(f"{Y}⚠{X} 日志里没有「重定向至」的记录。")
    print(f"  {D}可能是容器最近重启过（日志被清）、或者这段时间根本没人播。{X}")
    print(f"  {D}放一次片子再跑这个脚本，就会有记录。{X}")
    raise SystemExit


def kind(u):
    """这条直链是【整文件】还是【HLS 分片流】—— 两者速度天差地别。"""
    p = unquote(urlsplit(u).path).lower()
    q = unquote(urlsplit(u).query).lower()
    if ".m3u8" in p or ".m3u8" in q or "m3u8" in q:
        return "HLS 分片流"
    for ext in (".mkv", ".mp4", ".ts", ".avi", ".rmvb", ".flv", ".wmv", ".m4v"):
        if ext in p or ext in q:
            return "整文件"
    return "整文件?"


MEDIA_EXT = (".mkv", ".mp4", ".avi", ".rmvb", ".flv", ".wmv", ".m4v", ".mov")


def title(u):
    """从直链里把文件名抠出来。

    两种形态放的地方不一样，都要认：
      · 整文件  文件名在下载头参数里（response-content-disposition）
      · HLS     地址末尾是 media.m3u8，真正的文件名夹在【路径中段】
    只取末段的话，HLS 那些永远显示成 media.m3u8，按片名根本找不到。
    """
    q = unquote(urlsplit(u).query)
    m = re.search(r"filename\*?=(?:UTF-8''|\")?([^&;\"]+)", q)
    if m:
        return unquote(m.group(1))
    segs = [unquote(x) for x in urlsplit(u).path.split("/") if x]
    for seg in reversed(segs):
        if seg.lower().endswith(MEDIA_EXT):
            return seg
    return segs[-1] if segs else u


def haystack(u):
    """拿来和片名比对的那一大坨文本。

    【必须解码两次】网盘的下载头参数是【双重编码】的：地址里写的是
    %25E9%25BE%2599，解一次只得到 %E9%BE%99，解两次才是「龙虎门」。
    只解一次就会出现"说没找到、底下却列着它"这种自相矛盾的输出 ——
    因为列名字那段（title）恰好解了两次。实测被这个坑到过。
    把一次、两次、以及抠出来的名字全拼进去比，宁可宽一点也别漏。
    """
    one = unquote(u)
    return (one + "\n" + unquote(one) + "\n" + title(u)).lower()


hit = [(t, u) for t, u in rows if Q.lower() in haystack(u)]
if not hit:
    names = []
    for _t, u in rows:
        n = title(u)
        if n not in names:
            names.append(n)
    print(f"{Y}⚠{X} 这段日志里没有「{Q}」。出现过的是：")
    for n in names[:15]:
        print(f"  {D}·{X} {n}")
    raise SystemExit

print()
print("=" * 64)
print(f"  {B}{title(hit[-1][1])}{X}   {D}共 {len(hit)} 次换直链{X}")
print("=" * 64)

# 【连着一样的要折起来】实测一部片子 216 次换直链，一行一条刷了满屏，
# 而要找的"什么时候变的"反而淹在里面。连续同一条路折成一行：起止时间 + 次数，
# 变化的那一行自然就凸出来了 —— 这一屏存在的意义就是让变化一眼可见。
runs = []
for t, u in hit:
    key = (urlsplit(u).netloc, kind(u))
    if runs and runs[-1][0] == key:
        runs[-1][2] = t
        runs[-1][3] += 1
    else:
        runs.append([key, t, t, 1])

for i, ((host, k), t0, t1, n) in enumerate(runs):
    col = G if k == "HLS 分片流" else C
    span = t0 if n == 1 else f"{t0} ~ {t1[11:]}"
    mark = f"  {Y}← 从这里开始变了{X}" if i else ""
    print(f"  {col}{host}{X}  {k}")
    print(f"    {D}{span}　{n} 次{X}{mark}")

# 【"之后再没成功过"必须有失败记录撑着】只看"最后一条成功之后日志还有别的行"
# 是不够的：那可能只是【别的片子】在放。拿那个当证据，就会对着一部好好的片子
# 报"当下换不到直链"。所以只认这部片最后一次成功【之后】真的出现过的失败请求。
# 测试里就是被"仙逆播了一次"这条无关记录骗到的。
after = [f for f in fails if hit and f[0] > hit[-1][0]]
stalled = bool(after)
if stalled:
    print(f"  {Y}最后一次成功换直链：{hit[-1][0]}{X}")
    print(f"  {D}之后到 {after[-1][0]} 为止，播放请求只拿到失败 —— "
          f"这段时间点开它只会一直转圈。{X}")
    print()

if after:
    print(f"  {B}这之后失败的播放请求{X}{D}（Emby 要地址、MediaWarp 没给出来）{X}")
    for t, code, iid in after[-8:]:
        print(f"    {R}{t}   HTTP {code}   条目 {iid}{X}")
    if len(after) > 8:
        print(f"    {D}…共 {len(after)} 条，只列最近 8 条{X}")
    print(f"  {D}这些不分片名 —— 换直链没成功，日志里就没有片名可认。{X}")
    print()

hosts = {}
for t, u in hit:
    hosts.setdefault((urlsplit(u).netloc, kind(u)), []).append(t)
# 【结论要看当下，不只看历史】"换不到直链"和"直链变慢"是两回事，修法也完全不同：
# 前者点开根本不动，后者能动但一直缓冲。历史里全是成功、而最近只剩失败的话，
# 当下的问题就是前者 —— 这时再说"速度变了"是把人往错的方向指。
if len(hosts) == 1:
    (h, k), ts = next(iter(hosts.items()))
    print(f"  {D}历史上从头到尾都是同一条路：{h}（{k}）{X}")
    if stalled:
        print(f"  {B}但当下换不到直链{X}{D} —— 这不是快慢的事，是这个盘现在取不到"
              f"地址。跑「6 链路体检」看那个存储的实测结果{X}")
    else:
        print(f"  {D}那「以前流畅现在卡」就不是路变了，是那条路本身的速度变了 —— "
              f"用 tools/ali-403.sh 量一下当下能跑多少{X}")
else:
    print(f"  {B}换过 {len(hosts)} 种走法：{X}")
    for (h, k), ts in hosts.items():
        print(f"    {C}{h}{X}  {k}   {D}{ts[0]} ~ {ts[-1]}，{len(ts)} 次{X}")
    print(f"  {D}形态从「HLS 分片流」变成「整文件」= 从转码流掉回了原画，"
          f"那正是「忽然变卡」最常见的原因{X}")
    if stalled:
        print(f"  {B}另外：当下换不到直链{X}{D} —— 先把这个解决，再谈快慢{X}")
PY

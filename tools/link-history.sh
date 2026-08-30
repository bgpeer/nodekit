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

rows = []
with open(LOGFILE, encoding="utf-8", errors="replace") as f:
    for ln in f:
        m = LINE.search(ln)
        if m:
            rows.append((m.group(1), m.group(2)))

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


# 【按整条地址匹配，不只按抠出来的名字】名字抠得再准也总有抠不到的形态，
# 而片名那几个字一定在地址里的某个地方。宁可宽一点，也别漏掉半段历史。
hit = [(t, u) for t, u in rows if Q.lower() in unquote(u).lower()]
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
print(f"  {D}时间{' ' * 16}指向哪　　　　　　　　　　形态{X}")

prev = None
for t, u in hit:
    host = urlsplit(u).netloc
    k = kind(u)
    # 换了节点或换了形态就标出来 —— "什么时候变的"正是要找的东西
    mark = ""
    if prev and prev != (host, k):
        mark = f"  {Y}← 从这里开始变了{X}"
    prev = (host, k)
    col = G if k == "HLS 分片流" else C
    print(f"  {t}   {col}{host:<34}{X}{k}{mark}")

print()
hosts = {}
for t, u in hit:
    hosts.setdefault((urlsplit(u).netloc, kind(u)), []).append(t)
if len(hosts) == 1:
    (h, k), ts = next(iter(hosts.items()))
    print(f"  {D}从头到尾都是同一条路：{h}（{k}）{X}")
    print(f"  {D}那「以前流畅现在卡」就不是路变了，是那条路本身的速度变了 —— "
          f"用 tools/ali-403.sh 量一下当下能跑多少{X}")
else:
    print(f"  {B}换过 {len(hosts)} 种走法：{X}")
    for (h, k), ts in hosts.items():
        print(f"    {C}{h}{X}  {k}   {D}{ts[0]} ~ {ts[-1]}，{len(ts)} 次{X}")
    print(f"  {D}形态从「HLS 分片流」变成「整文件」= 从转码流掉回了原画，"
          f"那正是「忽然变卡」最常见的原因{X}")
PY

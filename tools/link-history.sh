#!/usr/bin/env bash
# 这个片子历次播放，MediaWarp 到底把播放器指去了哪 —— 从日志里翻，不猜。
#
#     bash link-history.sh 龙虎门
#     bash link-history.sh 龙虎门 2000     翻更多行（默认 20000 行日志）
#
# 只读（实测那一步只是发一次播放请求、拉 1 MiB，和点一次播放走的路一样）。
#
# 两段：先翻历史（去过哪），再【当场实测一次】（现在还行不行）。
# 用户的原话："我现在都播放不了，为什么不直接测" —— 对的。光看日志只能说
# "过去怎样"，而"现在能不能播"必须当场问一次才算数。
#
# 【为什么还留着历史那一段】"昨天很流畅、过了一晚上就卡了" —— 这种问题靠当下测一次
# 是答不上来的，当下的状态只有一个。而 MediaWarp 每换一次直链都会把完整地址
# 打进日志，历史全在里面：哪一次指向哪个网盘、是整文件还是 HLS 分片流、
# 中间有没有换过节点。把这些按时间列出来，"什么时候变的"就自己浮出来了。
set -u

Q="${1:-}"
LINES="${2:-20000}"
[ -n "$Q" ] || { echo "用法：bash link-history.sh <片名的一部分> [翻多少行日志]"; exit 1; }

# 【把版本打出来】这些脚本是 curl 下来跑的，而 raw 有 CDN 缓存：改完立刻拉，
# 拿到的可能还是几分钟前那份。跑出来的结果对不上，人只会以为"改了没用"。
# 屏幕上有个版本号，一眼就能分清是"没改对"还是"拿的是旧的"。
TOOL_VER="2026-08-30e"
echo "  ${0##*/}  版本 $TOOL_VER"

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
# 【路径要认全，还要不分大小写】同一台机器的日志里两种写法都出现过：
#   /Videos/113376/stream      客户端直接播（302 走这条）
#   /videos/113376/original.mkv 另一种客户端的写法，小写
# 转码时走的又是 master.m3u8 / main.m3u8。原来只认大写的 /Videos/.../stream，
# 于是"点不开"的那些请求一条都没收进来，屏幕上干干净净 —— 反倒像是没人点过。
FAIL = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d).*?\|\s*(\d{3})\s*\|"
                  r".*?/videos/(\d+)/", re.I)
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
elif last_ts and hit and last_ts > hit[-1][0]:
    # 【只摆事实，不下结论】这部片最后一次成功之后日志还在动，但没抓到它的失败
    # 请求。可能确实没人点，也可能点了却根本没走到 MediaWarp（客户端直连了
    # Emby 的 8096、或者 Emby 判成转码走了别的路径）。这两种差别很大，
    # 没有证据就别替用户选一个。
    print(f"  {D}最后一次成功换直链：{hit[-1][0]}，日志最新记到 {last_ts}{X}")
    print(f"  {D}中间这段没有它的成功记录，也没抓到它的失败请求 —— "
          f"要么没人点，要么点了没走到 MediaWarp。{X}")
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
        # 【别替测量下结论】"路没变"是日志能证明的；"所以是速度变了"不是 ——
        # 这一屏只看得见去了哪，看不见跑多快。指到能量的那个工具就够了。
        print(f"  {D}路没变过。快慢这一屏看不出来，"
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

# ------------------------------------------------------------------ 当场实测
# 历史只说明"去过哪"，说明不了"现在还行不行"。这一段就发一次真实的播放请求，
# 和用户在客户端点一下播放走的是同一条路。
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "${MS_DIR:-/opt/media-stack}/mediawarp/config/config.yaml" 2>/dev/null | head -1)"

echo
echo "================================================================"
echo "  现在实测一次（和点播放走同一条路）"
echo "================================================================"
if [ -z "$KEY" ]; then
  echo "  读不到 Emby API Key，测不了。先跑「3 后补参数 → 1」"
  exit 0
fi

DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' \
             "${MS_DIR:-/opt/media-stack}/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="${MS_DIR:-/opt/media-stack}/media"

python3 - "$KEY" "$Q" "$DATA_ROOT" <<'PY2'
import json, os, re, sys, time, urllib.request, urllib.error
KEY, Q, DATA_ROOT = sys.argv[1], sys.argv[2], sys.argv[3]
EMBY, MW = "http://127.0.0.1:8096", "http://127.0.0.1:9000"
B, D, R, G, Y, C, X = ("\033[1m", "\033[2m", "\033[31m", "\033[32m",
                       "\033[33m", "\033[36m", "\033[0m")

try:
    u = (f"{EMBY}/Items?Recursive=true&IncludeItemTypes=Movie,Episode,Video"
         f"&Fields=Path&Limit=3000&api_key={KEY}")
    items = json.load(urllib.request.urlopen(u, timeout=60)).get("Items") or []
except Exception as e:
    print(f"  {R}问不到 Emby：{e}{X}")
    raise SystemExit

hit = [i for i in items
       if str(i.get("Path") or "").endswith(".strm")
       and (Q.lower() in str(i.get("Name") or "").lower()
            or Q.lower() in str(i.get("Path") or "").lower())]
if not hit:
    print(f"  {Y}Emby 里没有匹配「{Q}」的 strm 条目{X}")
    raise SystemExit
if len(hit) > 1:
    print(f"  {Y}匹配到 {len(hit)} 个，测第一个：{hit[0].get('Name')}{X}")
it = hit[0]
iid = it.get("Id")
print(f"  {B}{it.get('Name')}{X}  {D}条目 {iid}{X}")

# 【先看 strm 文件本身】MediaWarp 走的是 alist_strm，而它【只认路径形式】。
# strm 里要是留着 URL 形式（老版本生成的、或者补时长那几秒被打断留下的残留），
# MediaWarp 会把整条 URL 当成网盘路径去查，查不到就不 302 —— 播放器一直转圈，
# 而挂载页面照样能播，因为那条路根本不经过 MediaWarp。
# 这个组合极具迷惑性，所以在量速度之前先把它排掉。
cpath = str(it.get("Path") or "")
hpath = (DATA_ROOT.rstrip("/") + "/strm/" + cpath[len("/data/strm/"):]
         if cpath.startswith("/data/strm/") else "")
body = ""
if hpath and os.path.isfile(hpath):
    try:
        body = open(hpath, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        body = ""
    if not body:
        print(f"  {R}✖ strm 文件是空的{X}  {D}{hpath}{X}")
    elif body.lower().startswith(("http://", "https://")):
        print(f"  {R}✖ strm 是 URL 形式{X}  {D}{body[:70]}…{X}")
        print(f"  {D}MediaWarp 的 alist_strm 只认路径形式，拿到 URL 会当成网盘路径"
              f"去查，查不到就不 302 —— 正好是「挂载能播、Emby 转圈」。{X}")
        print(f"  {B}修：点一次「5 生成媒体库」{X}{D}，它开头会把所有 strm "
              f"统一回路径形式{X}")
    else:
        print(f"  {D}strm 路径形式 ✔  {body[:70]}{X}")
elif hpath:
    print(f"  {R}✖ 宿主机上找不到这个 strm{X}  {D}{hpath}{X}")
    print(f"  {D}Emby 库里有条目、磁盘上没文件 —— 播放器点开当然什么都拿不到。{X}")
    # 【是整个盘没了还是就这一个】差别很大：整盘没了多半是那个盘没被扫、
    # 或者被当成孤儿目录清掉了；只少一个文件更像是网盘里那个文件被删/改名了。
    # 两种都靠「5 生成媒体库」重建，但知道是哪种才知道要不要回头看挂载路径。
    parts = cpath[len("/data/strm/"):].split("/")
    drive_dir = (DATA_ROOT.rstrip("/") + "/strm/" + "/".join(parts[:2])
                 if len(parts) >= 2 else "")
    if drive_dir and os.path.isdir(drive_dir):
        n = sum(1 for _r, _d, fs in os.walk(drive_dir)
                for f in fs if f.endswith(".strm"))
        print(f"  {D}这个盘的 strm 目录还在，里面有 {n} 个 strm —— "
              f"少的只是这一个（网盘里那个文件被删了或改名了？）{X}")
    elif drive_dir:
        print(f"  {Y}这个盘的 strm 目录整个都不在{X}"
              f"  {D}{drive_dir}{X}")
        print(f"  {D}整盘的 strm 都没了 —— 回「4 挂载路径」确认这个盘有没有被扫，"
              f"再点「5 生成媒体库」{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向 —— 要的就是那个 302 本身和它的 Location。"""
    def redirect_request(self, *_a, **_k):
        return None


op = urllib.request.build_opener(NoRedirect)
url = (f"{MW}/Videos/{iid}/stream?MediaSourceId=mediasource_{iid}"
       f"&Static=true&api_key={KEY}")
t0 = time.time()
loc, code = "", 0
try:
    r = op.open(url, timeout=90)
    code = r.status
except urllib.error.HTTPError as e:
    code = e.code
    loc = e.headers.get("Location", "") or ""
except Exception as e:
    print(f"  {R}✖ 请求 MediaWarp 失败：{e}{X}")
    raise SystemExit
el = time.time() - t0

if not loc:
    print(f"  {R}✖ 没拿到 302{X}（HTTP {code}，用了 {el:.1f} 秒）")
    print(f"  {D}换不到直链，点开就一直转圈。这不是快慢的问题。{X}")
    # 【strm 本身有问题时就别再指向体检了】体检查的是网盘那头，
    # 而这几种的原因已经在上面写明了 —— 再让人跑一遍体检只会兜圈子，
    # 而且体检那边一切正常，反而把他往错的方向带。
    if not body:
        print(f"  {D}原因上面已经写了 —— 先把 strm 补回来再说。{X}")
    elif body.lower().startswith(("http://", "https://")):
        print(f"  {D}上面已经指出原因了：strm 是 URL 形式。{X}")
    else:
        print(f"  {D}下一步：跑「6 链路体检」看那个存储的实测结果；"
              f"存储是好的就 docker restart mediawarp{X}")
    raise SystemExit

host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", loc)
kind = "HLS 分片流" if ".m3u8" in loc.lower() else "整文件"
print(f"  {G}✔ 302{X}  {D}{el:.1f} 秒{X}  →  {C}{host}{X}  {kind}")

# 【必须带 Range】阿里的直链不带 Range 直接 403，那是自己造出来的假故障
req = urllib.request.Request(loc, headers={
    "Range": "bytes=0-1048575",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=60) as rr:
        n = len(rr.read(1 << 20))
    el = time.time() - t0
    mbps = n * 8 / el / 1e6 if el > 0 else 0
    col = G if mbps >= 8 else (Y if mbps >= 3 else R)
    print(f"  实测速度  {col}{mbps:.2f} Mbps{X}  {D}（{n/1024/el:.0f} KB/s，"
          f"拉了 {n/1024/1024:.1f} MiB）{X}")
    print(f"  {D}够不够看要跟片子的码率比 —— Emby 条目页上写着（比如 16.6 Mbps）。"
          f"拉不到那个数就会边放边等。{X}")
except urllib.error.HTTPError as e:
    print(f"  {R}✖ 这条直链拉不动：HTTP {e.code}{X}")
    print(f"  {D}地址换到了但下不动 —— 用 tools/ali-403.sh 换几种方式再试{X}")
except Exception as e:
    print(f"  {R}✖ 这条直链拉不动：{e}{X}")
PY2

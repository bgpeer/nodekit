#!/usr/bin/env bash
# 「挂载里能播，Emby 里点开就 load fail / 一直转圈」专用。只读，不改任何东西。
#
#   bash cant-play.sh 鹿鼎记          片名的一部分、文件名的一部分都行
#   bash cant-play.sh 鹿鼎记 3        同名的有好几个时，查第 3 个
#
# 【为什么要一条链路一条链路地走】播不了在客户端上永远只有一句 load fail，而这条链
# 有六段，每一段坏了的表现【一模一样】：
#
#   ① Emby 里这个条目本身    ← 原盘目录：大小 0B、容器 Bluray，根本没有可播的文件
#   ② 本地那个 strm 文件      ← 空的、丢了、或者是 URL 形式（MediaWarp 认不出）
#   ③ OpenList 认不认这条路径  ← 网盘里改过名/删了，或者存储掉线
#   ④ MediaWarp 换不换直链    ← 令牌废了（重启过 OpenList），一律 404
#   ⑤ 那条直链拉不拉得动      ← 403、限速、绑 IP
#   ⑥ 拖得动进度条吗          ← 不认 Range 或被限流（429）：网页从头播没事，
#                              一拖就死，Emby 直接播不了
#   (7) 真实播放那一刻        ← 前六段测的都是脚本【自己造的】请求。客户端要转码、
#                              或者压根没走 MediaWarp，前六段照样全绿
#
# 猜是猜不出来的，一段一段问，坏在哪一段就报哪一段。
set -u

TOOL_VER="2026-09-04m"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

Q="${1:-}"
N="${2:-1}"          # 同名的多个条目里查第几个（不填就是第一个）
if [ -z "$Q" ]; then
  echo "用法：bash cant-play.sh <片名的一部分> [第几个]"
  exit 1
fi
DIR="${MS_DIR:-/opt/media-stack}"

KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key（先跑「3 后补参数」）"; exit 1; }
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
# 直链缓存时长是【按最短命的那家网盘算出来的】，不是固定值 —— 这台机器上是 9m，
# 写死"2 小时"会让人以为还要干等两小时。从配置里读。
TTL="$(sed -nE 's/^[[:space:]]*alist_api_ttl:[[:space:]]*"?([^"[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$TTL" ] || TTL="一小会儿"

export MS_KEY="$KEY" MS_OLPW="$OLPW" MS_DATA_ROOT="$DATA_ROOT" MS_Q="$Q" MS_N="$N" MS_TTL="$TTL"
python3 - <<'PY'
import json, os, re, subprocess, time, urllib.error, urllib.parse, urllib.request

EMBY = "http://127.0.0.1:8096"
MW   = "http://127.0.0.1:9000"
OL   = "http://127.0.0.1:5244"
KEY  = os.environ["MS_KEY"]
OLPW = os.environ.get("MS_OLPW") or ""
DATA_ROOT = os.environ["MS_DATA_ROOT"].rstrip("/")
Q = os.environ["MS_Q"]
TTL = os.environ.get("MS_TTL") or "一小会儿"
G="\033[32m"; Y="\033[33m"; R="\033[31m"; D="\033[2m"; B="\033[1m"; C="\033[36m"; X="\033[0m"

# 【脱敏】这一屏是会被截图发出来的。键名不限定前面是 ? 还是 &（裸 token= 也要盖），
# 长串阈值 24 —— 实测 39 位的令牌用 40 做阈值时原样留在了屏幕上。
TOK = re.compile(r"\b((?:access_token|refresh_token|token|auth_key|cookie|sign|"
                 r"password|pwd|api_key)=)[^&\s\"']+", re.I)
LONG = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def safe(s):
    s = TOK.sub(r"\1…", str(s or ""))
    return LONG.sub(lambda m: m.group(0)[:6] + "…", s)


def hr():
    print("  " + "-" * 58)


def emby(path, timeout=60):
    u = f"{EMBY}{path}{'&' if '?' in path else '?'}api_key={KEY}"
    with urllib.request.urlopen(u, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body.strip() else {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """要的就是 302 本身。跟随了就看不见它到底去了哪儿。"""
    def redirect_request(self, *_a, **_k):
        return None


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# ================= ① Emby 里的这个条目 =================
print()
print(f"  {B}① Emby 里的这个条目{X}")
hr()
items, start = [], 0
while True:
    try:
        d = emby(f"/Items?Recursive=true&IncludeItemTypes=Movie,Episode,Video"
                 f"&Fields=Path,MediaSources,MediaStreams,Container"
                 f"&StartIndex={start}&Limit=500")
    except Exception as e:
        print(f"  {R}✖ 问不到 Emby：{safe(e)}{X}")
        raise SystemExit(1)
    batch = d.get("Items") or []
    items += batch
    start += len(batch)
    if not batch or start >= int(d.get("TotalRecordCount") or 0):
        break
hit = [i for i in items
       if Q.lower() in str(i.get("Name") or "").lower()
       or Q.lower() in str(i.get("Path") or "").lower()]
if not hit:
    print(f"  {R}✖ Emby 里没有匹配「{Q}」的条目{X}")
    print(f"  {D}strm 还没进库。点「5 生成媒体库」，完了看它最后那段"
          f"「Emby 媒体库可以指向这些路径」有没有建库。{X}")
    raise SystemExit

def _facts(i):
    """列表那一行要摆的事实：容器、大小 —— 一眼就能看出该查哪一个。"""
    s0 = (i.get("MediaSources") or [{}])[0]
    sz = int(s0.get("Size") or 0)
    return (str(s0.get("Container") or i.get("Container") or "?"),
            f"{sz / 1024 / 1024 / 1024:.2f} GB" if sz else "0B")


if len(hit) > 1:
    # 【七个同名条目，只查第一个还不说是哪一个，等于瞎猜】把它们摆出来，
    # 谁坏了往往一眼就看得出（容器 bluray、大小 0B）。
    print(f"  {Y}匹配到 {len(hit)} 个{X}  {D}第二个参数可以指定查第几个，"
          f"例如：bash cant-play.sh {Q} 3{X}")
    for n, i in enumerate(hit[:12], 1):
        c, z = _facts(i)
        print(f"    {n:>2}. {str(i.get('Name'))[:34]:<34} {D}{c:<8}{z}{X}")
    if len(hit) > 12:
        print(f"    {D}…还有 {len(hit) - 12} 个，把片名写得更具体一点{X}")
    # 【同名条目里混着原盘时必须点破】原盘条目在 Emby 里长得和正常条目一模一样
    # （有海报、有简介），点下去必定 load fail。实测就是这么绕进去的：链路六段
    # 全通了，人却在 Emby 里点了旁边那条原盘，看到的还是 load fail，
    # 于是以为"没修好"。
    _disc = [i for i in hit
             if _facts(i)[0].lower() in ("bluray", "bdmv", "dvd", "iso")]
    if _disc and len(_disc) < len(hit):
        print(f"  {Y}⚠ 这里面有 {len(_disc)} 条是【蓝光原盘】条目"
              f"（容器 bluray、大小 0B），点它必定 load fail{X}")
        print(f"  {D}要播的是容器 mkv/mp4 那几条。原盘那几条点一次"
              f"「5 生成媒体库」会被压成正常条目。{X}")
try:
    pick = int(os.environ.get("MS_N") or "1")
except ValueError:
    pick = 1
pick = pick if 1 <= pick <= len(hit) else 1
it = hit[pick - 1]
if len(hit) > 1:
    print(f"  {D}这次查第 {pick} 个{X}")
iid, cpath = it.get("Id"), str(it.get("Path") or "")
srcs = it.get("MediaSources") or []
size = int((srcs[0].get("Size") if srcs else 0) or 0)
cont = str((srcs[0].get("Container") if srcs else "") or it.get("Container") or "")
secs = (it.get("RunTimeTicks") or 0) / 1e7
print(f"  {B}{it.get('Name')}{X}  {D}条目 {iid}{X}")
print(f"  {D}Path      {cpath}{X}")
print(f"  {D}容器      {cont or '（空）'}　大小 "
      f"{(str(round(size / 1024 / 1024 / 1024, 2)) + ' GB') if size else '0B'}"
      f"　时长 {int(secs // 60)} 分{X}")
# 【编码必须打出来】前六段测的是"服务器给不给得出数据"，而 load fail 还有一个完全
# 不同的死法：Emby 判定客户端解不了这个编码 → 要转码 → 而这套东西【不能转码】
# （文件在网盘上，ffmpeg 手里只有一条 URL）→ 客户端连 /stream 都不去请求就报错。
# 那种情况下 MediaWarp 日志里一条记录都没有 —— 光看日志会误判成"客户端没走 MediaWarp"。
# 所以把编码摆在最前面，让人一眼看出"这文件我的设备本来就放不了"。
# 【MediaStreams 要按 id 单独取】列表查询里 MediaSource 内部的 MediaStreams 是空的，
# 只有按 id 取那一次才填。上一版从列表结果里取，于是这一整段静默跳过 —— 屏上一行
# 编码都没有，而"是不是音轨解不了"恰恰是这次要回答的问题。
_ms = []
try:
    _one = (emby(f"/Items?Ids={iid}&Fields=MediaSources,MediaStreams")
            .get("Items") or [{}])[0]
    _ms = (_one.get("MediaStreams")
           or ((_one.get("MediaSources") or [{}])[0].get("MediaStreams"))
           or [])
except Exception:
    _ms = []
if not _ms and srcs:
    _ms = srcs[0].get("MediaStreams") or []
_v = next((x for x in _ms if x.get("Type") == "Video"), {})
_auds = [x for x in _ms if x.get("Type") == "Audio"]
if not _ms:
    # 【读不到就要说】静默跳过是上一版最坏的地方：屏上没有这两行，看的人根本不知道
    # 这一环压根没测到，而它可能正是原因。
    print(f"  {Y}⚠ 读不到这一条的编码信息（Emby 还没探到媒体流）{X}")
    print(f"  {D}手工查：Emby 网页 → 这部片 → 右上角「…」→ 媒体信息，"
          f"看视频/音频编码。{X}")
    print(f"  {D}最要紧的是音轨：TrueHD / DTS 这类多数客户端解不了，而这套东西"
          f"【连音轨都转不了】—— 那种情况下点开就是 load fail，"
          f"MediaWarp 日志里一条记录都不会有。{X}")
    print(f"  {D}一句话自查：去 OpenList 挂载页面播一次 —— 有画面、没声音，就是它。{X}")
if _v or _auds:
    _vc = str(_v.get("Codec") or "?").lower()
    _prof = str(_v.get("Profile") or "")
    _depth = _v.get("BitDepth") or 0
    _vtxt = f"{_vc}"
    if _prof:
        _vtxt += f" {_prof}"
    if _depth:
        _vtxt += f" {_depth}bit"
    _atxt = "、".join(
        f"{str(a.get('Codec') or '?').lower()}"
        + (f" {a.get('Channels')}声道" if a.get("Channels") else "")
        for a in _auds[:3]) or "（没有音轨信息）"
    print(f"  {D}视频      {_vtxt}{X}")
    print(f"  {D}音频      {_atxt}{X}")
    # 这几种是"很多客户端直接放不了"的重灾区。不下死结论 —— 能不能放取决于具体
    # 设备，但必须点出来，否则人会在服务器这头一直查下去（这次就查了七轮）。
    _hard = []
    if _depth and int(_depth) >= 10 and _vc in ("hevc", "h265", "vp9", "av1"):
        _hard.append(f"{_vc} {_depth}bit")
    if any(str(a.get("Codec") or "").lower() in
           ("truehd", "dts", "dtshd", "eac3", "flac", "mlp") for a in _auds):
        _hard.append("、".join(sorted({str(a.get("Codec")).lower() for a in _auds
                                       if str(a.get("Codec") or "").lower() in
                                       ("truehd", "dts", "dtshd", "eac3", "flac", "mlp")})))
    if _hard:
        print(f"  {Y}⚠ {'、'.join(_hard)} —— 很多客户端【解不了】这个{X}")
        print(f"  {D}解不了时 Emby 会要求转码，而这套东西不能转码（文件在网盘上，"
              f"本机没有它）。表现就是点开直接 load fail，而且【MediaWarp 日志里"
              f"一条请求都没有】—— 客户端连 /stream 都不会去要。{X}")
        print(f"  {B}一句话自查：去 OpenList 挂载页面播这个文件{X}")
        print(f"  {D}【有画面、没声音】= 音轨解不了，那就是它了 —— 挂载页面用的是"
              f"浏览器的解码器，浏览器只认 AAC/MP3/Opus/FLAC，TrueHD / DTS 一概不认。{X}")
        print(f"  {D}Emby 撞的是同一堵墙，只是更早：客户端说解不了这条音轨 → Emby 说"
              f"那我转音轨 → 而这套东西【连音轨都转不了】（文件在网盘上，服务器手里"
              f"没有它）→ 客户端拿不到可播的源 → 瞬间 load fail，连 /stream 都不去要，"
              f"所以 MediaWarp 日志里一条记录都没有。{X}")
        print(f"  {D}画面声音都正常 = 不是编码问题，往后面几段看。{X}")
        print(f"  {D}真解不了的话只有两条路，都不在服务器这头：{X}")
        print(f"  {D}  · 换能解的播放器 —— Infuse / VidHub / Kodi / 电视盒子，"
              f"它们自带 TrueHD、DTS、HEVC 10bit 的解码{X}")
        print(f"  {D}  · 换片源 —— 音轨是 AAC 或 AC3 的压制版，什么都能放{X}")

# 【判"原盘"要有真凭据】容器写着 bluray/iso，或者路径就在 BDMV/CERTIFICATE 里面 ——
# 二者必居其一。上一版还把"大小 0B 且时长 0"也算进来，那是错的：那个组合最常见的
# 意思是【Emby 还没探到媒体信息】，一条普普通通的 strm 刚进库就是这样。
# 结果就是把一条正常条目判成原盘、还让人去做一件毫不相干的事，而真正的原因半个字
# 没提。宁可不下结论，也不能下一个错的。
looks_disc = (cont.lower() in ("bluray", "bdmv", "dvd", "iso")
              or bool(re.search(r"/(BDMV|CERTIFICATE)(/|$)", cpath, re.I)))
if not size and not secs and not looks_disc:
    print()
    print(f"  {Y}⚠ Emby 还没探到这一条的媒体信息（大小 0B、时长 0）{X}")
    print(f"  {D}这【不一定】是播不了的原因，但它自己就是个毛病：续播点是按时长算"
          f"百分比的，分母为 0 那套逻辑整个失效 —— 停止播放直接判「已看完」。{X}")
    print(f"  {D}补时长是「5 生成媒体库」之后在后台跑的（每 3 分钟一轮），"
          f"补到哪儿了看「6 链路体检」的「条目时长」那一行。{X}")
    print(f"  {D}下面接着往下查 —— 探不到时长往往【正是】因为下面某一段不通。{X}")

if looks_disc:
    print()
    print(f"  {R}✖ 这是一个蓝光原盘（或光盘镜像）条目，不是一个视频文件{X}")
    print(f"  {D}原盘是一整棵目录树（BDMV/STREAM 里几十个 .m2ts + 索引），"
          f"播放要按索引在片段之间跳，那需要 Emby 拿得到本地目录；{X}")
    print(f"  {D}而 strm 里只装得下一条指向【单个文件】的地址。所以这类条目必定"
          f"0B / 0bps / load fail —— 跟网盘是哪家、直链怎么设都无关。{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}{D}，它会把原盘压成一个 strm"
          f"（取 BDMV 里最大的那个片段＝正片）。脚本要 v1.5.50 以上。{X}")
    print(f"  {D}压完之后 Emby 里这一条会消失、换成一条正常的电影条目。{X}")
    raise SystemExit

# ================= ② 本地那个 strm 文件 =================
print()
print(f"  {B}② 本地那个 strm 文件{X}")
hr()
hpath = (DATA_ROOT + "/strm/" + cpath[len("/data/strm/"):]
         if cpath.startswith("/data/strm/") else "")
body = ""
if not hpath:
    print(f"  {Y}这个条目不在 strm 目录下（{cpath}），后面几步跳过{X}")
    raise SystemExit
if not os.path.isfile(hpath):
    print(f"  {R}✖ 宿主机上找不到这个 strm{X}  {D}{hpath}{X}")
    print(f"  {D}Emby 库里有条目、磁盘上没文件 —— 点开当然什么都拿不到。{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}")
    raise SystemExit
try:
    body = open(hpath, encoding="utf-8", errors="replace").read().strip()
except OSError as e:
    print(f"  {R}✖ 读不了：{safe(e)}{X}")
    raise SystemExit
if not body:
    print(f"  {R}✖ strm 文件是空的{X}  {D}{hpath}{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}")
    raise SystemExit
if body.lower().startswith(("http://", "https://")):
    print(f"  {R}✖ strm 是 URL 形式{X}  {D}{safe(body)[:70]}…{X}")
    print(f"  {D}MediaWarp 的 alist_strm 【只认路径形式】，拿到 URL 会把整条当成"
          f"网盘路径去查，查不到就不 302 —— 正好是「挂载能播、Emby 转圈」。{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}{D}，它开头会统一改回路径形式{X}")
    raise SystemExit
print(f"  {G}✔{X} 路径形式  {D}{body}{X}")
if re.search(r"/(BDMV|CERTIFICATE)/", body, re.I):
    print(f"  {R}✖ 它指向的是蓝光原盘目录里的一个片段{X}")
    print(f"  {B}修：点一次「5 生成媒体库」{X}{D}（v1.5.50 以上会把原盘压成一条）{X}")

# ================= ③ OpenList 认不认这条网盘路径 =================
print()
print(f"  {B}③ OpenList 认不认这条网盘路径{X}")
hr()
tok = ""
if OLPW:
    try:
        req = urllib.request.Request(
            f"{OL}/api/auth/login",
            data=json.dumps({"username": "admin", "password": OLPW}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        tok = (json.load(urllib.request.urlopen(req, timeout=20)).get("data")
               or {}).get("token", "")
    except Exception as e:
        print(f"  {Y}登不上 OpenList：{safe(e)}{X}")
ol_size = 0        # OpenList 报的文件大小。⑥ 要拿它算文件中点 —— Emby 那边常常是 0
if not tok:
    print(f"  {Y}没有 OpenList 令牌，这一步跳过{X}"
          f"  {D}（读不到 {os.path.dirname(DATA_ROOT)}/.secrets 里的 OPENLIST_PASS）{X}")
    raw = ""
else:
    raw = ""
    try:
        req = urllib.request.Request(
            f"{OL}/api/fs/get",
            data=json.dumps({"path": body, "password": ""}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": tok}, method="POST")
        r = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:
        print(f"  {R}✖ 问 OpenList 失败：{safe(e)}{X}")
        r = {}
    if r.get("code") == 200:
        data = r.get("data") or {}
        raw = str(data.get("raw_url") or "")
        sz = int(data.get("size") or 0)
        ol_size = sz          # Emby 那边的大小常常是 0，定位文件中点要用这个
        print(f"  {G}✔{X} 认得这个文件  {D}{sz / 1024 / 1024 / 1024:.2f} GB{X}")
        if not raw:
            print(f"  {R}✖ 但它没给出直链（raw_url 是空的）{X}")
        else:
            host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", raw)
            # 【本机地址 ≠ 坏了】WebDAV 源、本地目录这类驱动在网盘那侧压根没有 CDN
            # 直链，OpenList 只能回自己的地址。那不是配置错，是那类驱动就这样：
            # 视频会从你的 VPS 过一遍。改配置改不掉，换个盘放才行。
            local = host.split(":")[0] in ("127.0.0.1", "localhost", "openlist")
            print(f"  {D}直链指向  {C}{host}{X}"
                  + (f"  {Y}← OpenList 自己{X}" if local else f"  {G}← 网盘 CDN{X}"))
            if local:
                print(f"  {D}这类驱动（WebDAV 源、本地目录）在网盘侧没有 CDN 直链，"
                      f"OpenList 只能回自己的地址 —— 视频要从你的 VPS 过一遍。"
                      f"不是配置错了，改也改不掉。{X}")
    else:
        msg = str(r.get("message") or "")
        print(f"  {R}✖ OpenList 不认这条路径{X}  {D}{safe(msg)[:120]}{X}")
        if "storage not found" in msg.lower():
            print(f"  {D}这个存储掉线了。去 OpenList 网页看它的状态。{X}")
            raise SystemExit
        if "object not found" not in msg.lower():
            raise SystemExit
        # 【"找不到"有两种，处置相反，不能都说成"文件被删了"】
        #   · 上游真的删了 / 改名了      → 点「5 生成媒体库」重建，本地那条 strm 是废的
        #   · 这一轮列目录没列全 / 撞限流 → 文件好好的，过一会儿自己就好；重建反而会把
        #                                  一批还活着的 strm 当成失效删掉
        # 两者当场就能分开：去列它的父目录（带 refresh，绕开缓存），看那个名字在不在。
        # 实测撞到过：同一个文件，六分钟前挂载页面还播得动，这里却报 object not found。
        want = os.path.basename(body)
        parent = os.path.dirname(body)
        print(f"  {D}再去列一次它的父目录（带 refresh，绕开缓存）看名字在不在...{X}")
        try:
            req = urllib.request.Request(
                f"{OL}/api/fs/list",
                data=json.dumps({"path": parent, "password": "", "page": 1,
                                 "per_page": 0, "refresh": True}).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": tok}, method="POST")
            rl = json.load(urllib.request.urlopen(req, timeout=180))
        except Exception as e:
            rl = {"code": -1, "message": str(e)}
        if rl.get("code") != 200:
            print(f"  {Y}父目录也列不出来{X}  {D}{safe(rl.get('message'))[:100]}{X}")
            print(f"  {D}这不是「文件没了」，是【这个源现在列不动】—— 上游那台 WebDAV "
                  f"在抖或者在限流。别急着点「5 生成媒体库」：那一步会把列不到的"
                  f"当成还在（三态判据），但也修不好这个，等它缓过来再说。{X}")
        else:
            names = [str(x.get("name") or "")
                     for x in ((rl.get("data") or {}).get("content") or [])]
            if want in names:
                print(f"  {Y}父目录里【有】这个名字，可 fs/get 却说找不到{X}")
                print(f"  {D}是刚才那一下的偶发（缓存里存了一份不完整的列表，或者 "
                      f"上游那一瞬抖了）。文件是好的，隔几分钟再跑一次这个脚本。{X}")
                print(f"  {D}它也解释了「挂载里能播、Emby 里不行」—— 两次请求赶上的"
                      f"运气不一样，而不是哪个配置错了。{X}")
            elif not names:
                print(f"  {Y}父目录列出来是空的{X}  {D}0 个条目 —— 上游这一轮什么都没给，"
                      f"多半是限流。不是文件被删了。{X}")
            else:
                print(f"  {R}父目录里确实没有这个名字{X}  {D}上游删了或改名了{X}")
                near = [n for n in names if n[:8] == want[:8]][:3]
                print(f"  {D}那一层现在有 {len(names)} 个条目"
                      + (f"，名字接近的：{'、'.join(near)}" if near else "") + f"{X}")
                print(f"  {B}修：点一次「5 生成媒体库」{X}{D}，本地那条 strm 会被清掉{X}")
        raise SystemExit

# ================= ④ MediaWarp 换不换直链 =================
print()
print(f"  {B}④ MediaWarp 换不换直链（302）{X}")
hr()
op = urllib.request.build_opener(NoRedirect)
url = (f"{MW}/Videos/{iid}/stream?MediaSourceId=mediasource_{iid}"
       f"&Static=true&api_key={KEY}")
loc, code = "", 0
t0 = time.time()
try:
    rr = op.open(url, timeout=90)
    code = rr.status
except urllib.error.HTTPError as e:
    code, loc = e.code, e.headers.get("Location", "") or ""
except Exception as e:
    print(f"  {R}✖ 请求 MediaWarp 失败：{safe(e)}{X}")
    raise SystemExit
el = time.time() - t0
if not loc:
    print(f"  {R}✖ 没拿到 302{X}  {D}HTTP {code}，用了 {el:.1f} 秒{X}")
    print(f"  {D}换不到直链，点开就一直转圈或 load fail。{X}")
    if code in (401, 403, 404):
        # MediaWarp 只在【启动那一刻】登录 OpenList 一次。OpenList 一重启，它手里
        # 那份令牌就作废了，之后每次换直链都被拒 —— 对 Emby 表现成 404。
        # 而已经缓存过直链的片子照样能放，所以看着像"有的能放有的不能放"。
        print(f"  {B}先试：docker restart mediawarp{X}")
        print(f"  {D}它只在启动那一刻登录一次 OpenList，OpenList 一重启旧令牌就废了 ——"
              f"之后每次换直链都被拒。已经缓存过直链的片子照样能放，"
              f"所以看着像「有的能放有的不能放」。{X}")
    else:
        print(f"  {D}下一步：跑「6 链路体检」看这个存储的实测结果。{X}")
    raise SystemExit
host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", loc)
kind = "HLS 分片流" if ".m3u8" in loc.lower() else "整文件"
print(f"  {G}✔ 302{X}  {D}{el:.1f} 秒{X}  →  {C}{host}{X}  {D}{kind}{X}")

# 【302 成功 ≠ 播得了】302 到一个【只有容器里解析得了】的名字，对播放器就是死路：
# 手机、电视报 "Name or service not known"，客户端上只有一句 load fail。
# 而这一整条链每一步都是"成功"的 —— 这就是它极难自己看出来的原因。
bare = host.split(":")[0]
if (bare in ("openlist", "emby", "mediawarp", "autofilm", "localhost")
        or bare.startswith(("127.", "172.1", "172.2", "172.3", "10.", "192.168."))):
    print()
    print(f"  {R}✖ 302 指向的是内网地址，播放器根本连不上{X}  {D}{host}{X}")
    print(f"  {D}这是代理型存储（WebDAV 源、本地目录）特有的：它们在网盘侧没有 CDN"
          f"直链，OpenList 只能回自己的 /d/ 地址 —— 而那个地址的主机名是"
          f"【谁来问就按谁用的主机名拼】。MediaWarp 在容器里用 http://openlist:5244"
          f"去问，拿回来的就是 openlist:5244。{X}")
    print(f"  {B}修：把 OpenList 的「网站 URL」填成对外地址{X}")
    print(f"  {D}跑一次「7 更新」会自动填（脚本要 v1.5.53 以上），"
          f"或者去 OpenList 设置里手填 https://list.<你的域名>。{X}")
    print(f"  {D}填完已经缓存的旧地址最多 {TTL} 后自动换过来（这台机器的直链缓存"
          f"就是这个值）；等不及就 docker restart mediawarp。{X}")
    print(f"  {D}下面那一步是在这台机器上拉的 —— 这里能拉动不代表手机能。{X}")

# ================= ⑤ 那条直链拉不拉得动 =================
print()
print(f"  {B}⑤ 那条直链拉不拉得动{X}")
hr()
# 【必须带 Range】阿里的直链不带 Range 直接 403，那是自己造出来的假故障
req = urllib.request.Request(loc, headers={"Range": "bytes=0-1048575",
                                           "User-Agent": UA})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=90) as rr:
        n = len(rr.read(1 << 20))
        st = rr.status
    el = time.time() - t0
    mbps = n * 8 / el / 1e6 if el > 0 else 0
    print(f"  {G}✔ HTTP {st}{X}  {D}拉了 {n / 1024 / 1024:.1f} MiB，"
          f"{el:.1f} 秒，{mbps:.2f} Mbps{X}")
    if st == 200:
        print(f"  {Y}回的是 200 不是 206 —— 服务器不认 Range{X}")
        print(f"  {D}整文件流这样的话进度条拖不动，只能从头播。"
              f"（HLS 分片流回 200 是正常的，它靠切换分片来跳转）{X}")
    # 【够不够是算出来的，不是拍一个阈值】"低于 5 Mbps 大概率要卡"对 720p 太严、
    # 对 4K 原盘太松。这部片的大小和时长上面都拿到了，直接算它真正需要多少：
    #     码率 = 大小 × 8 ÷ 时长
    # 拿实测速度跟它比，才谈得上"够"或"不够"。
    need = ((ol_size or size) * 8 / secs / 1e6) if secs else 0
    if not need:
        print(f"  {D}读不到这部片的码率，比不了{X}")
    elif mbps >= need * 1.3:
        print(f"  {G}够用{X}  {D}这部片平均码率 {need:.1f} Mbps，实测 {mbps:.1f} —— "
              f"有富余{X}")
    elif mbps >= need:
        print(f"  {Y}刚好够，没有余量{X}  {D}平均码率 {need:.1f} Mbps，实测 {mbps:.1f}。"
              f"高码率的段落（打斗、烟雾）会瞬时超过平均值，那时候要缓冲{X}")
    else:
        print(f"  {R}✖ 不够：这部片要 {need:.1f} Mbps，实测只有 {mbps:.1f}{X}")
        print(f"  {D}能开播，但会边放边等 —— 拖进度条之后尤其明显。{X}")
        print(f"  {D}代理型存储（WebDAV 源）的视频【全程经过你的 VPS】，"
              f"所以这里量到的就是 VPS 到上游那台服务器的速度，"
              f"换播放器、改 Emby 设置都改不了它。{X}")
        print(f"  {D}要么把大码率的片子放夸克/阿里（那些是 302 直连网盘 CDN，"
              f"不吃 VPS 带宽），要么接受这个源只适合放码率低的片子。{X}")

    # ---------- ⑥ 拖进度条（从文件中间要一段）----------
    # 【这一步和上面那步不是一回事】上面拉的是【开头】，服务器就算完全不认 Range、
    # 从头把整个文件发过来，也照样"成功"。而播放器开播时几乎总要先 seek 一下，
    # 拖进度条更是必须从中间要 —— 不支持的话表现正是：网页里从头播能播，
    # 一拖就卡死，Emby / 外部播放器直接播不出来。这是代理型存储（WebDAV 源）
    # 最常见的死因，而且光看"能不能拉到数据"永远发现不了。
    print()
    print(f"  {B}⑥ 拖进度条（从文件中间要一段）{X}")
    hr()
    # 【必须先歇一下】⑤ 刚拉完 1 MiB，紧接着再发一个请求，撞上源的频率限制就是
    # 429 —— 而那是【脚本自己造出来的】。实测栽过一次：同一个文件，单独测时 ⑥ 报
    # "进度条能拖"，跟在 ⑤ 后面测就报 429，两次结论相反。歇三秒，测的才是它本来的样子。
    print(f"  {D}先歇 3 秒再要 —— 紧跟着上一步发请求会撞上源的频率限制，"
          f"那个 429 是脚本自己造的{X}")
    time.sleep(3)
    mid = max(1 << 20, (ol_size or size or (1 << 30)) // 2)
    req2 = urllib.request.Request(loc, headers={
        "Range": f"bytes={mid}-{mid + 65535}", "User-Agent": UA})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req2, timeout=90) as r2:
            st2, cr = r2.status, r2.headers.get("Content-Range", "")
            n2 = len(r2.read(65536))
        el2 = time.time() - t0
        if st2 == 206 and cr:
            start_at = cr.split()[-1].split("-")[0].split("/")[0]
            good = start_at.isdigit() and int(start_at) == mid
            print(f"  {G if good else Y}HTTP 206{X}  {D}{cr}　拿到 {n2} 字节，"
                  f"{el2:.1f} 秒{X}")
            if good:
                print(f"  {G}✔ 服务器认 Range{X}  {D}从哪儿要就从哪儿给{X}")
                # 【能 seek ≠ 拖过去能看】上面只要了 64 KiB，那点数据任何服务器
                # 都给得起。而"拖了之后卡住 / 跳回开头"是【供不上数据】：播放器
                # seek 完要立刻填满缓冲，填不上就放弃重来。所以还得从同一个位置
                # 持续拉几秒，量真实吞吐 —— 这跟 ⑤ 从头拉不是一回事，
                # 很多源是开头快、拖到中间就慢下来。
                print(f"  {D}再从同一个位置连着拉 4 秒，量拖过去之后供不供得上...{X}")
                req3 = urllib.request.Request(loc, headers={
                    "Range": f"bytes={mid}-{mid + (24 << 20)}", "User-Agent": UA})
                try:
                    got3, t3 = 0, time.time()
                    with urllib.request.urlopen(req3, timeout=60) as r3:
                        while time.time() - t3 < 4:
                            chunk = r3.read(1 << 16)
                            if not chunk:
                                break
                            got3 += len(chunk)
                    el3 = max(0.1, time.time() - t3)
                    m3 = got3 * 8 / el3 / 1e6
                    if not need:
                        print(f"  {D}拖过去之后 {m3:.1f} Mbps"
                              f"（读不到码率，比不了）{X}")
                    elif m3 >= need:
                        print(f"  {G}✔ 拖过去也供得上{X}  {D}{m3:.1f} Mbps ≥ "
                              f"这部片要的 {need:.1f} Mbps{X}")
                    else:
                        print(f"  {R}✖ 拖过去就供不上了{X}  {D}只有 {m3:.1f} Mbps，"
                              f"这部片要 {need:.1f} Mbps{X}")
                        print(f"  {D}这正是「一拖就卡住 / 跳回从头播」的原因："
                              f"播放器 seek 完要立刻填满缓冲，填不上就放弃重来。"
                              f"服务器是认 Range 的（上面刚验过），纯粹是慢。{X}")
                except Exception as e3:
                    print(f"  {Y}持续拉的时候断了：{safe(e3)}{X}")
                    print(f"  {D}拖过去之后连不稳 —— 表现就是一拖就卡。{X}")
            else:
                print(f"  {Y}回的 206 起点和要的对不上{X}  "
                      f"{D}要 {mid}，给的是 {start_at}{X}")
        elif st2 == 200:
            print(f"  {R}✖ 回的是 200，不是 206 —— 服务器不认 Range{X}")
            print(f"  {D}它把整个文件从头发过来了。表现就是：网页里从头播能播，"
                  f"一拖进度条就卡死；而 Emby / 外部播放器开播时就要 seek，"
                  f"于是直接播不出来。{X}")
            print(f"  {D}这是代理型存储（WebDAV 源）最常见的死因 —— 上游那台 WebDAV "
                  f"服务器不支持断点续传，OpenList 只是照转，改哪个配置都没用。{X}")
            print(f"  {B}只能换个盘放{X}{D}：夸克 / 阿里 / 115 是 302 到网盘 CDN，"
                  f"那些都认 Range。{X}")
        else:
            print(f"  {R}✖ HTTP {st2}{X}  {D}要中间那一段被拒了{X}")
    except urllib.error.HTTPError as e2:
        if e2.code == 429:
            # 【429 不是"不支持 seek"，是"你请求太频繁"】处置完全不同，说错方向
            # 会让人去折腾 Range、折腾播放器，而真正的限制在源那边。
            ra = e2.headers.get("Retry-After", "")
            print(f"  {R}✖ HTTP 429 —— 源在限流{X}"
                  + (f"  {D}它要求等 {ra} 秒{X}" if ra else ""))
            print(f"  {D}歇 8 秒再要一次，看是一直被限还是刚才那下太密...{X}")
            time.sleep(8)
            try:
                with urllib.request.urlopen(req2, timeout=90) as r4:
                    print(f"  {Y}第二次成功了（HTTP {r4.status}）{X}")
                    print(f"  {D}说明这个源【限的是频率，不是能力】：隔开就给，"
                          f"连着要就拒。{X}")
            except urllib.error.HTTPError as e4:
                print(f"  {R}第二次还是 HTTP {e4.code}{X}")
            except Exception as e4:
                print(f"  {R}第二次也没成：{safe(e4)}{X}")
            print()
            print(f"  {B}这就是「拖进度条跳回开头 / Emby 直接 load fail」的原因{X}")
            print(f"  {D}播放器一 seek 就是一个新请求；开播时更要连发好几个"
                  f"（探测编码、要首帧、再要播放位置）。撞上限流被拒，播放器"
                  f"只好放弃、从头再来 —— 你看到的就是「跳回开头」和「load fail」。{X}")
            print(f"  {D}而挂载网页从头播没事，是因为那是【一个】连续的请求，"
                  f"中途不再要新的。{X}")
            print(f"  {D}限的是源那边，OpenList 只是照转 —— 改直链方式、换播放器、"
                  f"调 Emby 设置都碰不到它。{X}")
            print(f"  {B}这个源不适合放要拖进度条的片子{X}"
                  f"{D}；大码率的放夸克/阿里（302 直连网盘 CDN，不吃这个限制）。{X}")
        else:
            print(f"  {R}✖ HTTP {e2.code}{X}  {D}要中间那一段被拒了 —— 进度条拖不动，"
                  f"播放器多半直接播不出来{X}")
    except Exception as e2:
        print(f"  {R}✖ 要不到中间那一段：{safe(e2)}{X}")
except urllib.error.HTTPError as e:
    print(f"  {R}✖ HTTP {e.code}{X}  {D}直链拿到了，但拉不动{X}")
    if e.code == 429:
        print(f"  {D}429 = 源在限流（请求太频繁）。播放器开播要连发好几个请求，"
              f"撞上就是 load fail。隔一会儿再跑一次这个脚本看是不是一直这样。{X}")
    if e.code == 403:
        print(f"  {D}403 常见两种：直链绑了取它的那台机器的 IP/UA；"
              f"或者签名过期。前者要把这个盘的「回源方式」改成本机代理"
              f"（4 挂载路径 → 选那个盘 → 2 直链方式）。{X}")
except Exception as e:
    print(f"  {R}✖ 拉不动：{safe(e)}{X}")
# ================= (7) 真实播放那一刻发生了什么 =================
# 【前面六段测的都是脚本自己造的请求】它们全通，只证明"这条路走得通"，不证明
# "Emby 客户端走的是这条路"。而最常见的那个死因恰恰不在这六段里：Emby 判定客户端
# 解不了这个编码 → 决定转码 → 而 strm + 302 这套【根本不能转码】（文件在网盘上，
# ffmpeg 手里只有一条 URL 和一台没有那个文件的机器）。
# 转码那条路在日志里长得不一样：master.m3u8 / main.m3u8，而不是 /stream。
print()
print(f"  {B}(7) 真实播放那一刻（读 MediaWarp 日志）{X}")
hr()
print(f"  {D}前面六段测的都是脚本自己造的请求。这一步看【你点播放时】实际发生了什么。{X}")
try:
    _p = subprocess.run(["docker", "logs", "--tail", "1500", "mediawarp"],
                        capture_output=True, text=True, timeout=60)
    mwlog = (_p.stdout or "") + (_p.stderr or "")
    mwerr = ""
except Exception as _e:
    mwlog, mwerr = "", str(_e)
if mwerr:
    print(f"  {Y}读不到 MediaWarp 日志：{safe(mwerr)}{X}")
else:
    HIT = re.compile(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d).*?\|\s*(\d{3})\s*\|"
                     r".*?(/videos/" + str(iid) + r"/(\S*))", re.I)
    rows = [m.groups() for m in (HIT.search(l) for l in mwlog.splitlines()) if m]
    OKC = ("200", "204", "206", "302", "304")
    TRANS = re.compile(r"master\.m3u8|main\.m3u8|hls", re.I)
    if not rows:
        print(f"  {Y}日志里没有这个条目的任何播放请求{X}")
        print(f"  {D}也就是说：你点播放的时候，请求【根本没到 MediaWarp】。{X}")
        print(f"  {D}最常见的原因是客户端连的不是 MediaWarp —— 直连 Emby 的 8096 "
              f"会绕过 302 拦截。客户端里填的服务器地址应该是 https://emby.<你的域名>。{X}")
        print(f"  {D}（也可能只是你还没点过播放。点一次再跑这个脚本。）{X}")
    else:
        print(f"  {D}最近 {min(len(rows), 6)} 条（时间 | 状态 | 走哪条路）：{X}")
        for ts, code, path, tail in rows[-6:]:
            kind = "转码" if TRANS.search(tail) else "直接播放"
            col = G if code in OKC else R
            print(f"    {D}{ts}{X}  {col}{code}{X}  {D}{kind}  {path[:52]}{X}")
        bad = [r for r in rows if r[1] not in OKC]
        trans = [r for r in rows if TRANS.search(r[3])]
        print()
        if trans:
            print(f"  {R}[X] 客户端在要转码流（master.m3u8）{X}")
            print(f"  {D}这套东西【不能转码】：文件在网盘上，ffmpeg 手里只有一条 URL，"
                  f"本机没有那个文件。所以一旦 Emby 判定要转码，必定 load fail —— "
                  f"而前面六段全是好的。{X}")
            print(f"  {B}修：让它直接播放{X}{D} —— 客户端里把画质/播放设置改成"
                  f"「原始质量 / 直接播放」。改完还是转码，就是这个客户端解不了"
                  f"里面的编码（x265 10bit、TrueHD 这类，老设备和部分播放器就是解不了），"
                  f"换 Infuse / VidHub 试试。{X}")
        elif bad:
            print(f"  {R}[X] 有 {len(bad)} 次请求失败，最近一次 HTTP {bad[-1][1]}{X}")
            print(f"  {D}前面六段都通，说明不是链路。看这个状态码：401/403 多半是 "
                  f"MediaWarp 的 OpenList 令牌废了（docker restart mediawarp）；"
                  f"404 是它没认出这条 strm。{X}")
        else:
            print(f"  {G}[v] 请求都成功了（直接播放，302 发出去了）{X}")
            print(f"  {D}MediaWarp 这边是好的。那 load fail 就出在【302 之后】—— "
                  f"播放器自己去连那个地址失败了。最常见的是客户端解不了这个编码"
                  f"（这套东西不能转码），换 Infuse / VidHub 一试就知道。{X}")

print()
PY

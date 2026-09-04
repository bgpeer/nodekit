#!/usr/bin/env bash
# 「挂载里能播，Emby 里点开就 load fail / 一直转圈」专用。只读，不改任何东西。
#
#   bash cant-play.sh 鹿鼎记          片名的一部分、文件名的一部分都行
#   bash cant-play.sh 鹿鼎记 3        同名的有好几个时，查第 3 个
#
# 【为什么要一条链路一条链路地走】播不了在客户端上永远只有一句 load fail，而这条链
# 有五段，每一段坏了的表现【一模一样】：
#
#   ① Emby 里这个条目本身    ← 原盘目录：大小 0B、容器 Bluray，根本没有可播的文件
#   ② 本地那个 strm 文件      ← 空的、丢了、或者是 URL 形式（MediaWarp 认不出）
#   ③ OpenList 认不认这条路径  ← 网盘里改过名/删了，或者存储掉线
#   ④ MediaWarp 换不换直链    ← 令牌废了（重启过 OpenList），一律 404
#   ⑤ 那条直链拉不拉得动      ← 403、限速、绑 IP
#
# 猜是猜不出来的，一段一段问，坏在哪一段就报哪一段。
set -u

TOOL_VER="2026-09-04b"          # 见 link-history.sh 里的说明：CDN 会缓存
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

export MS_KEY="$KEY" MS_OLPW="$OLPW" MS_DATA_ROOT="$DATA_ROOT" MS_Q="$Q" MS_N="$N"
python3 - <<'PY'
import json, os, re, time, urllib.error, urllib.parse, urllib.request

EMBY = "http://127.0.0.1:8096"
MW   = "http://127.0.0.1:9000"
OL   = "http://127.0.0.1:5244"
KEY  = os.environ["MS_KEY"]
OLPW = os.environ.get("MS_OLPW") or ""
DATA_ROOT = os.environ["MS_DATA_ROOT"].rstrip("/")
Q = os.environ["MS_Q"]
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
                 f"&Fields=Path,MediaSources,Container"
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
        if "object not found" in msg.lower():
            print(f"  {D}网盘里这个文件被删了或改名了。点「5 生成媒体库」重建。{X}")
        elif "storage not found" in msg.lower():
            print(f"  {D}这个存储掉线了。去 OpenList 网页看它的状态。{X}")
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
    if mbps < 5:
        print(f"  {Y}这个速度放 1080p 原盘大概率要卡{X}"
              f"  {D}用 tools/ali-403.sh 换几种方式量一遍，"
              f"几条路速度齐平就是网盘限速，不是线路{X}")
    else:
        print()
        print(f"  {G}五段全通{X}  {D}链路本身是好的。还是播不了的话，多半是客户端"
              f"解不了这个编码（这套东西不能转码）—— 换 Infuse / VidHub 试试，"
              f"或者看 tools/playing.sh 那一路是直接播放还是转码。{X}")
except urllib.error.HTTPError as e:
    print(f"  {R}✖ HTTP {e.code}{X}  {D}直链拿到了，但拉不动{X}")
    if e.code == 403:
        print(f"  {D}403 常见两种：直链绑了取它的那台机器的 IP/UA；"
              f"或者签名过期。前者要把这个盘的「回源方式」改成本机代理"
              f"（4 挂载路径 → 选那个盘 → 2 直链方式）。{X}")
except Exception as e:
    print(f"  {R}✖ 拉不动：{safe(e)}{X}")
print()
PY

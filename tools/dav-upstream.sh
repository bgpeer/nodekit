#!/usr/bin/env bash
# WebDAV 盘到底能不能走 302 —— 去问它的【上游】，别问 OpenList。只读，不改任何配置。
#
#   bash dav-upstream.sh              每个 WebDAV 盘各问一遍
#   bash dav-upstream.sh /七米蓝影视   只问这一个
#
# 【这个脚本要回答的就一句话】WebDAV 盘的视频是不是非得【全程走 VPS 流量】不可。
#
# 现在的答案是"是"，理由写在 media-stack.py 的 PROXY_ONLY_DRIVERS 上：WebDAV 传的
# 是文件流，没有"给你一个带签名的公网地址"这回事，所以 OpenList 只能自己代理 ——
# 每一个字节都从网盘流进 VPS、再从 VPS 流给播放器，出口流量按片子大小一比一地烧。
#
# 但那句话是【从协议推出来的，不是量出来的】，而协议留了一个口子：
#
#   WebDAV 的 GET 允许回 302。有些做影视库的上游正是这么干的 —— 鉴权在它那台机器上
#   做完，然后把你丢给一个带签名的 CDN 地址。要是这个盘的上游就是这种，那这台 VPS
#   要做的只是【替播放器去挨那一下 302】，之后的几个 G 一个字节都不用过 VPS。
#   这跟这套东西给转码流做的分片重定向是同一个套路，已经验证过能成。
#
# 两种结果，处置完全不同，所以必须量：
#
#   上游回 302 → 有救。VPS 只出一次几百字节的握手，视频直达播放器。
#   上游回 200 → 没救。凭据在 Authorization 头里，播放器发不出这个头，
#                 换任何配置都一样 —— 这个盘的流量账单就是躲不掉的。
#
# 【不打印任何凭据】上游的账号密码要从 OpenList 库里读出来才能发这个请求，但屏上
# 只出现主机名。302 的目标地址也只留主机名 —— 那种地址里通常带着签名，贴出来等于
# 把一条能直接下片的链交出去。
set -u

TOOL_VER="2026-09-19b"
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

# 【strm 那一份现成的答案在哪】生成媒体库时每部片都写了一个 .strm，
# 里面就是一条 OpenList 路径 —— 比自己去翻目录快得多，也不打那个限流的接口。
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"

export OL_PW="$OLPW" OL_ONLY="${1:-}" OL_DATA_ROOT="$DATA_ROOT"
python3 - <<'PY'
import base64, json, os, time, urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:5244"
# 【中性 UA】见仓库规矩第一条：出网请求不许自报家门。上游是第三方，
# 告诉它"这台机器在跑 media-stack"是白送出去的一条信息。
UA = "curl/8.5.0"
VIDEO = (".mp4", ".mkv", ".ts", ".avi", ".mov", ".flv", ".m4v", ".wmv", ".rmvb")
G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"
D = "\033[2m"; B = "\033[1m"; C = "\033[36m"; X = "\033[0m"
pw = os.environ["OL_PW"]
only = os.environ.get("OL_ONLY") or ""
STRM_ROOT = os.path.join(os.environ.get("OL_DATA_ROOT") or "", "strm")


def api(path, body=None, tok=None, timeout=120, method="POST"):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": tok} if tok else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """要的就是 302 本身 —— 跟随了就看不见它到底给没给 CDN 地址。"""
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def host_of(u):
    """只留主机名。

    【整条地址不能往屏上打】CDN 直链里带着签名，等于一条谁拿到谁都能下片的链；
    上游地址里可能带着路径结构。主机名足够回答"它把我丢到哪儿去了"。
    """
    try:
        h = urllib.parse.urlsplit(u).hostname or "?"
    except ValueError:
        return "?"
    return h


try:
    tok = (api("/api/auth/login", {"username": "admin", "password": pw},
               timeout=20).get("data") or {}).get("token", "")
except Exception as e:
    print(f"{R}✖{X} 连不上 OpenList：{e}")
    raise SystemExit(1)
if not tok:
    print(f"{R}✖{X} OpenList 登录失败（密码对不上？）")
    raise SystemExit(1)

try:
    rows = ((api("/api/admin/storage/list?page=1&per_page=100", tok=tok,
                 timeout=30, method="GET").get("data") or {}).get("content") or [])
except Exception as e:
    print(f"{R}✖{X} 问不到存储列表：{e}")
    raise SystemExit(1)

# 【只挑 WebDAV】别的驱动本来就有 CDN 直链，这个脚本对它们没有意义。
dav = []
for s in rows:
    if "webdav" not in str(s.get("driver") or "").lower():
        continue
    mp = str(s.get("mount_path") or "")
    if only and mp != only:
        continue
    add = s.get("addition")
    if isinstance(add, str):
        try:
            add = json.loads(add or "{}")
        except ValueError:
            add = {}
    dav.append((mp, add or {}))

print()
print(f"  {B}WebDAV 盘的上游给不给直链{X}")
print("=" * 62)
if not dav:
    if only:
        print(f"  {R}✖{X} 没有叫「{only}」的 WebDAV 盘")
    else:
        print(f"  {D}这台机器上一个 WebDAV 盘都没有 —— 别的驱动本来就有 CDN 直链，"
              f"不归这个脚本管。{X}")
    raise SystemExit(0)


def from_strm(root):
    """从已经生成的 .strm 里挑一条属于这个盘的路径。找不到返回空串。

    【这是首选，翻目录只是兜底】理由有三个，每个都够硬：
      · 零次列目录 —— 而列目录接口正是这些源最爱限流的那一个（实测 nginx 那侧
        28 条请求里 7 条 429）。为了做个检测反而去打它，本末倒置。
      · 快 —— 读本地文件，不用等跨境接口
      · 拿到的是【Emby 真的在播的那个文件】，不是随便翻出来的某一个

    翻目录那条路在真实的库上是走不通的：这台机器上的结构是
    /七米蓝影视/mov/电影/R/人在囧途/[人在囧途]….mp4 —— 六层，而「电影」
    那一层按首字母铺开几十个目录，广度优先在那一层就把预算烧光，一个文件都碰不到。
    """
    if not os.path.isdir(STRM_ROOT):
        return ""
    for dirpath, _dirs, files in os.walk(STRM_ROOT):
        for fn in files:
            if not fn.lower().endswith(".strm"):
                continue
            try:
                with open(os.path.join(dirpath, fn), encoding="utf-8",
                          errors="replace") as fh:
                    line = fh.readline().strip()
            except OSError:
                continue
            # strm 里可能是纯路径，也可能是一条 http 地址（挂载页面那种）
            if line.startswith("http://") or line.startswith("https://"):
                line = urllib.parse.unquote(
                    urllib.parse.urlsplit(line).path or "")
                for pre in ("/d", "/p"):
                    if line.startswith(pre + "/"):
                        line = line[len(pre):]
                        break
            if line.startswith(root.rstrip("/") + "/"):
                return line
    return ""


def find_video(root, budget=120):
    """在这个盘里找一个视频来试。返回 (路径, 没找到的原因)。

    【贴着一条路往下钻，别纯广度】纯广度在「按首字母铺开几十个目录」这种库上
    必然翻不到文件（上一版就是这么报的"翻了 25 个目录还没翻完"）。这里把新发现的
    子目录插到队首，等于优先往深处走，同时保留回头路 —— 一头扎进「文档」「图片」
    这种没有视频的岔路也还能退出来接着找别的（dav-check.sh 上栽过一次，
    报"没找到视频"而那个盘里明明有片子）。
    不带 refresh —— 只是要个文件名，读缓存足够，没必要去打那个本来就被限流的接口。
    """
    queue, used, seen, err = [root], 0, 0, ""
    while queue and used < budget:
        path = queue.pop(0)
        used += 1
        try:
            d = api("/api/fs/list", {"path": path, "password": "", "page": 1,
                                     "per_page": 100, "refresh": False},
                    tok).get("data") or {}
        except Exception as e:
            err = err or type(e).__name__
            continue
        items = d.get("content") or []
        seen += len(items)
        for i in items:
            if not i.get("is_dir") and str(i.get("name", "")).lower().endswith(VIDEO):
                return path.rstrip("/") + "/" + i["name"], ""
        # 【插队首 = 优先往深处走】见 docstring：纯广度在真实的影视库上翻不到文件。
        queue[:0] = [path.rstrip("/") + "/" + i["name"]
                     for i in items if i.get("is_dir")]
    if err:
        return "", f"列目录没通（{err}）"
    if seen == 0:
        return "", "这个盘是空的"
    if queue:
        return "", (f"翻了 {used} 个目录都没翻到视频，而且 {STRM_ROOT} 下面也没有"
                    f"这个盘的 strm —— 先跑一次「5 生成媒体库」，再回来跑这个脚本")
    return "", f"{seen} 个条目里没有视频文件"


def quote(p):
    """中文路径必须转义 —— 原样塞进 urllib 直接 UnicodeEncodeError。"""
    return urllib.parse.quote(p, safe="/")


any_302 = 0
asked = 0          # 【真的问到上游的有几个】末尾那句话必须跟着它走，见下面
for mp, add in dav:
    print()
    print(f"  {B}{mp}{X}")
    base = str(add.get("address") or "").rstrip("/")
    root = str(add.get("root_folder_path") or "/").rstrip("/")
    user = str(add.get("username") or "")
    pwd = str(add.get("password") or "")
    if not base:
        print(f"    {R}✖{X} 库里没有这个盘的上游地址 —— 问不了")
        continue
    print(f"    {D}上游 {host_of(base)}{X}")

    # 【先看磁盘上现成的，再谈翻目录】见 from_strm 的说明
    f, why = from_strm(mp), ""
    if not f:
        f, why = find_video(mp)
    if not f:
        print(f"    {(R if '没通' in why else D)}{why}{X}")
        continue
    rel = f[len(mp):] if f.startswith(mp) else f
    url = base + quote(root + "/" + rel.lstrip("/"))

    h = {"User-Agent": UA, "Range": "bytes=0-1"}
    if user or pwd:
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{user}:{pwd}".encode()).decode()
    req = urllib.request.Request(url, headers=h, method="GET")
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=45) as r:
            code, loc = r.getcode(), r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        code, loc = e.code, e.headers.get("Location", "")
    except Exception as e:
        print(f"    {R}✖ 问不到上游{X}  {D}{type(e).__name__}: {str(e)[:80]}{X}")
        continue
    el = time.time() - t0

    asked += 1
    if code in (301, 302, 303, 307, 308) and loc:
        any_302 += 1
        print(f"    {G}✔ 上游回 {code} → {host_of(loc)}{X}  {D}{el:.2f} 秒{X}")
        print(f"    {G}这个盘有救{X}{D}：鉴权在上游做完了，之后是一条公网地址。"
              f"VPS 只要替播放器挨这一下 302，几个 G 的视频一个字节都不用过 VPS。{X}")
    elif code in (200, 206):
        print(f"    {Y}上游回 {code}，直接给字节{X}  {D}{el:.2f} 秒{X}")
        print(f"    {D}没有别的路：凭据在 Authorization 头里，播放器发不出这个头，"
              f"所以只能 OpenList 代读、字节全程过 VPS。"
              f"这个盘的出口流量按片子大小一比一地烧，换配置改不掉。{X}")
    elif code in (401, 403):
        print(f"    {R}✖ 上游回 {code} —— 这个盘的账号密码上游不认{X}")
        print(f"    {D}那它现在能播只能是靠别的（比如上游认 IP）。"
              f"先去「4 挂载路径」把这个盘的凭据核一遍。{X}")
    else:
        print(f"    {Y}上游回 {code}{X}  {D}{el:.2f} 秒 —— 既不是直链也不是字节，"
              f"照实摆在这儿，别替它圆{X}")

print()
print("=" * 62)
# 【一个都没问到的时候，不许说任何关于上游的话】
# 上一版这里只分"有 302"和"没有 302"两档 —— 于是一个视频都没找到、上游一次都没
# 被问到，屏上照样打出「这些 WebDAV 盘的上游都不给直链」。人照着这句话去换源，
# 而那是个基于零数据的决定。「没测成」必须单列一档，这条规矩 why-stall.sh 的
# verdict() 里早就立了（col == C 那一档），这里又犯了一遍。
if asked == 0:
    print(f"  {Y}一个上游都没问到 —— 上面每个盘都卡在「找一个视频来试」这一步{X}")
    print(f"  {D}所以这一轮【什么都没测出来】。这里不会告诉你这些盘有救还是没救，"
          f"因为根本没量。{X}")
    print(f"  {D}按上面每个盘那一行的提示办完，再跑一次。{X}")
elif any_302:
    print(f"  {G}至少有一个 WebDAV 盘的上游是给直链的{X}")
    print(f"  {D}把这一整屏发出去 —— 上游给直链，这套东西就能替它做 302，"
          f"那个盘的 VPS 流量能从「按片子大小一比一」降到几乎为零。{X}")
else:
    print(f"  {D}问到的 {asked} 个 WebDAV 盘，上游都不给直链 —— 它们的流量躲不掉，"
          f"这不是哪次改动造成的，是 WebDAV 这个协议本身的样子。{X}")
    print(f"  {D}真要省这笔流量，只有换个有 CDN 直链的源（夸克 / 阿里 / 115 那一类）。{X}")
PY

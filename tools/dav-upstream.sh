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

TOOL_VER="2026-09-19a"
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

export OL_PW="$OLPW" OL_ONLY="${1:-}"
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


def find_video(root, budget=25):
    """在这个盘里找一个视频来试。返回 (路径, 没找到的原因)。

    广度优先，别一头扎进「文档」「图片」这种没有视频的岔路就到底
    （dav-check.sh 上栽过一次，报"没找到视频"而那个盘里明明有片子）。
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
        queue += [path.rstrip("/") + "/" + i["name"] for i in items if i.get("is_dir")]
    if err:
        return "", f"列目录没通（{err}）"
    if seen == 0:
        return "", "这个盘是空的"
    if queue:
        return "", f"翻了 {used} 个目录还没翻完，都没有视频"
    return "", f"{seen} 个条目里没有视频文件"


def quote(p):
    """中文路径必须转义 —— 原样塞进 urllib 直接 UnicodeEncodeError。"""
    return urllib.parse.quote(p, safe="/")


any_302 = False
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

    if code in (301, 302, 303, 307, 308) and loc:
        any_302 = True
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
if any_302:
    print(f"  {G}至少有一个 WebDAV 盘的上游是给直链的{X}")
    print(f"  {D}把这一整屏发出去 —— 上游给直链，这套东西就能替它做 302，"
          f"那个盘的 VPS 流量能从「按片子大小一比一」降到几乎为零。{X}")
else:
    print(f"  {D}这些 WebDAV 盘的上游都不给直链 —— 它们的流量躲不掉，"
          f"这不是哪次改动造成的，是 WebDAV 这个协议本身的样子。{X}")
    print(f"  {D}真要省这笔流量，只有换个有 CDN 直链的源（夸克 / 阿里 / 115 那一类）。{X}")
PY

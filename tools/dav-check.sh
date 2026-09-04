#!/usr/bin/env bash
# OpenList 自带的 WebDAV：每个盘走 302 直链还是本机代理。只读。
#
#   bash dav-check.sh              每个盘各挑一个视频量一遍
#   bash dav-check.sh /quark       只量这一个盘
#
# 【为什么这件事必须量，不能猜】拿 Infuse / VidHub / Kodi 直接连 OpenList 的 WebDAV，
# 是绕开 Emby 那一整套（strm、刮削、等扫描）最省事的路。但它值不值得走，全看一件事：
#
#   302 到网盘直链  →  视频从播放器直达网盘，不吃 VPS 带宽，进度条随便拖
#   本机代理转发    →  每一个字节都经过你的 VPS，还得看上游认不认 Range
#
# 这两种在客户端里长得一模一样（都是"能播"），差别只在你的流量账单和能不能拖进度条。
# 而它是【每个盘各自】的：同一台 OpenList 上，夸克可能 302、WebDAV 源必然代理。
#
# 路径从接口里取，不用手打中文 —— 裸 curl 打中文路径要自己转义，上一版就是这么 404 的。
set -u

TOOL_VER="2026-09-04b"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

export OL_PW="$OLPW" OL_ONLY="${1:-}"
python3 - <<'PY'
import base64, json, os, time, urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:5244"
VIDEO = (".mp4", ".mkv", ".ts", ".avi", ".mov", ".flv", ".m4v", ".wmv", ".rmvb")
G="\033[32m"; Y="\033[33m"; R="\033[31m"; D="\033[2m"; B="\033[1m"; X="\033[0m"
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
    """要的就是 302 本身。跟随了就看不见它到底 302 到哪儿去了。"""
    def redirect_request(self, *a, **k):
        return None


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
    mounts = [str(s.get("mount_path") or "")
              for s in ((api("/api/admin/storage/list?page=1&per_page=100", tok=tok,
                             timeout=30, method="GET").get("data") or {}).get("content") or [])]
except Exception as e:
    print(f"{R}✖{X} 问不到存储列表：{e}")
    raise SystemExit(1)
if only:
    mounts = [m for m in mounts if m == only]
    if not mounts:
        print(f"{R}✖{X} 没有叫「{only}」的挂载点")
        raise SystemExit(1)


def find_video(root, budget=25):
    """在这个盘里找一个视频文件来试。找不到返回 ""。

    【不能每层只钻第一个文件夹】上一版就是那么写的，一头扎进一条没有视频的岔路
    （「文档」「图片」这种）就到底了，回头也不回，然后报"没找到视频文件"——
    而那个盘里明明有片子。等于那个盘根本没测到，输出还写得像结论。
    改成广度优先，一层层铺开找，用满 budget 次列目录为止（大盘不至于没完没了地钻）。

    【不带 refresh】只是要一个能拿来试的文件名，读缓存足够。带 refresh 等于为了做个
    检测，反而去打那个本来就被限流的列目录接口。
    """
    queue, used = [root], 0
    while queue and used < budget:
        path = queue.pop(0)
        used += 1
        try:
            d = api("/api/fs/list", {"path": path, "password": "", "page": 1,
                                     "per_page": 100, "refresh": False}, tok).get("data") or {}
        except Exception:
            continue
        items = d.get("content") or []
        for i in items:
            if not i.get("is_dir") and str(i.get("name", "")).lower().endswith(VIDEO):
                return path.rstrip("/") + "/" + i["name"]
        queue += [path.rstrip("/") + "/" + i["name"] for i in items if i.get("is_dir")]
    return ""


auth = "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()
print()
print(f"  {B}OpenList 的 WebDAV：每个盘走哪条路{X}")
print("=" * 62)

for mp in mounts:
    f = find_video(mp)
    if not f:
        print(f"  {mp:<16}{Y}没找到视频文件{X}  {D}（找了 25 个目录都没有，或者列目录没通）{X}")
        continue
    # WebDAV 的地址要按 URL 转义 —— 中文原样塞进去 urllib 直接 UnicodeEncodeError
    url = BASE + "/dav" + urllib.parse.quote(f, safe="/")
    req = urllib.request.Request(url, headers={"Authorization": auth,
                                               "User-Agent": "Mozilla/5.0",
                                               "Range": "bytes=0-1023"})
    t0 = time.monotonic()
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=90) as r:
            n = len(r.read(1024))
            code, loc = r.status, ""
    except urllib.error.HTTPError as e:
        code, loc, n = e.code, e.headers.get("Location", ""), 0
    except Exception as e:
        print(f"  {mp:<16}{R}没回话{X}  {D}{type(e).__name__}　{f}{X}")
        continue
    el = time.monotonic() - t0

    if code in (301, 302, 303, 307, 308):
        host = loc.split("/")[2] if "://" in loc else loc[:40]
        print(f"  {mp:<16}{G}302 → 网盘直链{X}  {D}{el:.1f} 秒　{host}{X}")
        print(f"  {' ':<16}{D}视频不经过 VPS，进度条能拖{X}")
    elif code == 206:
        print(f"  {mp:<16}{Y}本机代理（206）{X}  {D}{el:.1f} 秒{X}")
        print(f"  {' ':<16}{D}每个字节都过你的 VPS；Range 认，进度条能拖{X}")
    elif code == 200:
        print(f"  {mp:<16}{R}本机代理，且不认 Range（200）{X}  {D}{el:.1f} 秒{X}")
        print(f"  {' ':<16}{R}进度条拉不动，跳转只能从头下{X}")
    else:
        print(f"  {mp:<16}{R}HTTP {code}{X}  {D}{el:.1f} 秒{X}")
    print(f"  {' ':<16}{D}{f}{X}")

print()
print(f"  {D}客户端连法：https://list.<你的域名>/dav/　账号 admin + OpenList 的密码{X}")
print(f"  {D}（list 这个子域没套 Basic Auth，Infuse / VidHub / Kodi 直接连就行）{X}")
print()
print(f"  {D}这条路的取舍：省掉 Emby 那一整套（strm、刮削、等扫描），新片放进网盘就在；{X}")
print(f"  {D}代价是没有海报简介、没有跨设备的观看进度、也没有分账号的权限隔离 ——{X}")
print(f"  {D}私密库靠的就是 Emby 的账号，WebDAV 这边谁有密码谁全看。两条路可以并存。{X}")
PY

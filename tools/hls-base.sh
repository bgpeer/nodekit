#!/usr/bin/env bash
# 转码流的 m3u8 目录，两次问出来是不是同一个。只读，不改任何东西。
#
#   bash hls-base.sh          自己去找一部转码流的片子
#   bash hls-base.sh 片名     指定一部
#
# 【这个脚本只回答一个问题，因为那个问题决定一个缓存对不对】
#
# 分片重定向（media-stack 的 do_hls_fix）里有这么一段：
#
#     HLS_BASE_TTL = 300
#     def base_of(vid):
#         hit = cache.get(vid)
#         if hit and now - hit[1] < HLS_BASE_TTL:
#             return hit[0]          # 5 分钟内直接用上次那个目录
#
# 它按【条目 id】缓存 m3u8 所在的目录，五分钟内不再问 MediaWarp。
# 这个缓存成不成立，全看一件事：同一部片，MediaWarp 两次给出的 m3u8
# 【是不是同一个目录】。
#
#   两次一样  → 目录稳定，缓存没问题，「第二次播不了」得往别处查
#   两次不同  → 目录按次签发。那么第一次播缓存下来的目录，第二次播就是【死的】——
#               客户端拿到的是新 m3u8，而分片被指回旧目录，于是播不了；
#               等过了五分钟缓存自然过期，又能播了。这个缓存就是按构造错的。
#
# 【不打印地址，只打印指纹】那种地址里带着签名，整条贴出来等于把一条能直接
# 下片的链交出去（仓库规矩第一条）。这里只打 md5 的前 12 位和"一样/不一样"。
set -u

TOOL_VER="2026-09-20a"
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
# 【照抄 why-stall.sh 里那一条，别自己另发明】上一版我写的那个要求"到值就结束"，
# 行尾有注释或尾随空白就匹配不上，结果这台机器上直接读不到 key。
# 下面这条是已经在真机上验过的。
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
       "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1 | tr -d "\"'")"
[ -n "$KEY" ] || { echo "  ✖ 读不到 Emby API Key（$DIR/mediawarp/config/config.yaml 里的 auth）"; exit 1; }

export HB_KEY="$KEY" HB_DATA="$DATA_ROOT" HB_Q="${1:-}" HB_DIR="$DIR"
python3 - <<'PY'
import hashlib, json, os, re, time
import urllib.error, urllib.parse, urllib.request

EMBY = "http://127.0.0.1:8096"
MW = "http://127.0.0.1:9000"
KEY = os.environ["HB_KEY"]
DATA_ROOT = os.environ["HB_DATA"]
Q = os.environ.get("HB_Q") or ""
MS_DIR = os.environ.get("HB_DIR") or "/opt/media-stack"
G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"
D = "\033[2m"; B = "\033[1m"; C = "\033[36m"; X = "\033[0m"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """要的就是 302 本身 —— 跟随了就看不见它指到哪个目录。"""
    def redirect_request(self, *a, **k):
        return None


OP = urllib.request.build_opener(_NoRedirect)


def emby(path, timeout=60):
    u = f"{EMBY}{path}{'&' if '?' in path else '?'}api_key={KEY}"
    with urllib.request.urlopen(u, timeout=timeout) as r:
        b = r.read()
    return json.loads(b) if b.strip() else {}


def fp(s):
    """指纹。【不打印地址本身】那里面带着签名。"""
    return hashlib.md5(s.encode()).hexdigest()[:12] if s else "(空)"


# ---------------------------------------------------------------- 找一部转码流的片子
try:
    items = (emby("/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
                  "&Fields=Path&Limit=4000") or {}).get("Items") or []
except Exception as e:
    print(f"  {R}✖ 问不到 Emby：{e}{X}")
    raise SystemExit(1)
if Q:
    items = [i for i in items if Q in str(i.get("Name") or "")]
    if not items:
        print(f"  {R}✖ 库里没有叫「{Q}」的条目{X}")
        raise SystemExit(1)

# strm 里写的网盘路径 → 挂载点，用来认出哪些片子在转码流那个盘上
STRM_ROOT = os.path.join(DATA_ROOT, "strm")


def mount_of(item):
    """这个条目落在哪个网盘挂载点上。读不出来返回空串。"""
    cp = str(item.get("Path") or "")
    if not cp.startswith("/data/strm/"):
        return ""
    hp = os.path.join(STRM_ROOT, cp[len("/data/strm/"):])
    try:
        line = open(hp, encoding="utf-8", errors="replace").readline().strip()
    except OSError:
        return ""
    return "/" + line.lstrip("/").split("/", 1)[0] if line.startswith("/") else ""


def ask_base(vid):
    """问 MediaWarp 要一次播放地址，返回 (是不是 m3u8, 目录, 整条地址)。"""
    req = urllib.request.Request(
        f"{MW}/Videos/{vid}/stream?MediaSourceId=mediasource_{vid}"
        f"&Static=true&api_key={KEY}", headers={"User-Agent": "curl/8.5.0"})
    loc = ""
    try:
        OP.open(req, timeout=40).close()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
    except Exception as e:
        print(f"    {R}问不到 MediaWarp：{type(e).__name__}{X}")
        return False, "", ""
    if not loc:
        return False, "", ""
    head = loc.split("?", 1)[0]
    return ".m3u8" in head.lower(), head[:head.rfind("/") + 1], loc


print()
print(f"  {B}转码流的 m3u8 目录：两次问出来是不是同一个{X}")
print("=" * 62)

# 【先从库里问出哪些盘是转码流，别挨个去试】挨个问 MediaWarp 意味着挨个换直链，
# 一部片一次，最多几百次 —— 而换直链正是这些源限流最狠的那个动作。为了做个检测
# 反而去打那个接口，本末倒置（dav-upstream.sh 上刚栽过同一个跟头）。
# 读 OpenList 的库是本地、只读、零成本。
def hls_mounts():
    """设成转码流的挂载点。读不出来返回 None —— 【不是空集合】。

    「读不到」和「一个都没有」必须分开：前者该说"没测成"，后者才是"这台机器
    没人用转码流"。把前者说成后者，就是这一轮里犯过三次的那个错。
    """
    db = os.path.join(os.path.dirname(DATA_ROOT.rstrip("/")),
                      "openlist", "config", "data.db")
    if not os.path.exists(db):
        db = os.path.join(MS_DIR, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("select mount_path, addition from x_storages").fetchall()
        con.close()
    except Exception:
        return None
    out = []
    for mp, add in rows:
        try:
            a = json.loads(add or "{}")
        except Exception:
            a = {}
        # 转码流在夸克叫 link_method="streaming"，在 115 叫
        # use_transcoding_address=True —— 同一件事，两个名字，都要认
        if (str(a.get("link_method") or "") == "streaming"
                or str(a.get("use_transcoding_address") or "").lower()
                in ("true", "1")):
            out.append(str(mp))
    return out


want = hls_mounts()
if want is None:
    print(f"  {Y}读不到 OpenList 的库 —— 没法先筛出哪些盘是转码流{X}")
    print(f"  {D}这一轮什么都没测。不会去挨个试 —— 那要挨个换直链，"
          f"正是这些源限流最狠的动作。{X}")
    raise SystemExit(0)
if not want:
    print(f"  {D}这台机器上没有盘设成转码流。{X}")
    print(f"  {D}（这个脚本只对转码流有意义：原画直链是一个完整文件，"
          f"没有「同目录的分片」这回事。）{X}")
    raise SystemExit(0)
print(f"  {D}转码流的盘：{'、'.join(want)}{X}")

cands = [i for i in items if mount_of(i) in want]
if not cands:
    print(f"  {R}✖ 这些盘里一个条目都没找到{X}"
          + (f"  {D}（片名筛过：{Q}）{X}" if Q else ""))
    raise SystemExit(0)

# 【只问一部】筛完之后第一部就够 —— 这个脚本要的是"目录稳不稳"，不是"哪部片"
pick = None
for it in cands[:3]:
    vid = str(it.get("Id") or "")
    if not vid:
        continue
    is_m3u8, base, _ = ask_base(vid)
    if is_m3u8:
        pick = (vid, str(it.get("Name") or "?"), mount_of(it), base)
        break

if not pick:
    print(f"  {Y}这些盘设的是转码流，可 MediaWarp 的 302 不是 m3u8{X}")
    print(f"  {D}那分片重定向这一环根本没被用上 —— 先跑 why-stall.sh 看那条 302"
          f"到底指到哪儿，这个脚本的前提不成立。{X}")
    raise SystemExit(0)

vid, name, mp, base1 = pick
print(f"  片子：{C}{name}{X}  {D}条目 {vid}{X}" + (f"  {D}盘 {mp}{X}" if mp else ""))
# 【先报代价】问一次 = 换一次直链，而换直链正是这些源限流最狠的动作。
# 一共三次，别多；也别让人以为这个脚本是免费的。
print(f"  {D}一共问 MediaWarp 三次（= 换三次直链），中间隔 3 秒和 12 秒。"
      f"不拉任何视频字节。{X}")
print()
print(f"  第 1 次   目录指纹 {C}{fp(base1)}{X}")

# 【要隔一会儿再问】紧挨着问两次，MediaWarp 自己的直链缓存必定命中，
# 量到的是"缓存稳不稳"，不是"目录是不是按次签发"。而分片重定向那个缓存
# 要管 5 分钟，所以真正该比的是【隔开一段时间】的两次。
for wait, label in ((3, "第 2 次"), (12, "第 3 次")):
    time.sleep(wait)
    ok2, base2, _ = ask_base(vid)
    same = (base2 == base1)
    col = G if same else R
    print(f"  {label}   目录指纹 {col}{fp(base2)}{X}"
          f"  {D}（隔了 {wait} 秒）{X}  "
          + (f"{G}一样{X}" if same else f"{R}不一样{X}"))
    if not same:
        base1 = None
        break

print()
print("=" * 62)
if base1 is not None:
    print(f"  {G}✔ 目录是稳定的{X}")
    print(f"  {D}那分片重定向里那个「按条目 id 缓存 5 分钟」的做法站得住 ——"
          f"「第二次播不了」的原因不在这儿，得换个方向查：{X}")
    print(f"  {D}  · 分片本身的签名可能有时效（目录没变，但 .ts 的 query 变了）{X}")
    print(f"  {D}  · Emby 那边换了 MediaSourceId，条目 id 对得上但源对不上{X}")
    print(f"  {D}  · 上游按频率限流（第二次正好撞上）—— 跑 why-stall.sh 的 ⑥{X}")
else:
    print(f"  {R}✖ 目录每次都不一样 —— 那个缓存按构造就是错的{X}")
    print(f"  {D}第一次播时缓存下来的目录，第二次播就是【死的】：客户端从 MediaWarp"
          f"拿到的是新 m3u8，而分片被指回旧目录 → 播不了；等过了 5 分钟缓存自然"
          f"过期，又能播了。{X}")
    print(f"  {B}把这一屏发出去，我按它改。{X}")
PY

#!/usr/bin/env bash
# 挂载页面点开一个文件转圈：到底卡在哪一步。只读。
#
#   bash ol-file.sh "/quark/夸克挂载/动漫/吞噬星空/238 4K.mp4"
#
# 【为什么单独有这个】另外六个脚本都是从 Emby 那头查的（要片名、要条目 id）。可
# "在 OpenList 网页里点开文件转圈"这件事根本没走 Emby —— 网页调的是 OpenList 自己
# 的 fs/get，那一步要去网盘换一条播放地址回来。新加的片子还没进 Emby 的时候，
# 前面那些工具一个都用不上，而这恰恰是最常卡住的时刻。
#
# 分三步量，每步单独计时，卡在哪一目了然：
#   ① 列父目录     慢 = 网盘对列目录接口限流（这个接口是重灾区）
#   ② fs/get       慢/失败 = 换播放地址那一步。转码流没做好就卡在这儿
#   ③ 拉 1 MiB     慢 = 地址拿到了但拉不动，那是限速/线路
set -u

TOOL_VER="2026-08-31a"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

P="${1:-}"
[ -n "$P" ] || { echo "用法：bash ${0##*/} \"/quark/夸克挂载/动漫/某剧/238 4K.mp4\""; exit 1; }

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

export OL_PATH="$P" OL_PW="$OLPW"
python3 - <<'PY'
import json, os, time, urllib.error, urllib.request

BASE = "http://127.0.0.1:5244"
G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"; D="\033[2m"; B="\033[1m"; X="\033[0m"
path = os.environ["OL_PATH"]
parent = path.rsplit("/", 1)[0] or "/"


def api(p, body, tok=None, timeout=180):
    req = urllib.request.Request(
        BASE + p, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": tok} if tok else {})})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace")), time.monotonic() - t0


try:
    r, _ = api("/api/auth/login", {"username": "admin", "password": os.environ["OL_PW"]},
               timeout=20)
    tok = (r.get("data") or {}).get("token", "")
except Exception as e:
    print(f"{R}✖{X} 连不上 OpenList：{e}")
    raise SystemExit(1)
if not tok:
    print(f"{R}✖{X} OpenList 登录失败（密码对不上？）")
    raise SystemExit(1)

print()
print(f"  {B}{path}{X}")
print("=" * 58)

# ---- ① 列父目录。带 refresh，问的是"网盘此刻答不答得动"，不是缓存 ----
try:
    r, t = api("/api/fs/list", {"path": parent, "password": "", "page": 1,
                                "per_page": 1, "refresh": True})
    n = (r.get("data") or {}).get("total", 0) if r.get("code") == 200 else 0
    if r.get("code") == 200:
        c = G if t < 5 else (Y if t < 20 else R)
        print(f"  ① 列父目录    {c}{t:6.1f} 秒{X}  {n} 项")
    else:
        print(f"  ① 列父目录    {R}失败{X}  {str(r.get('message'))[:60]}  ({t:.1f} 秒)")
except Exception as e:
    print(f"  ① 列父目录    {R}没回话{X}  {type(e).__name__}")

# ---- ② fs/get：网页点开文件卡住的就是这一下 ----
raw = ""
try:
    r, t = api("/api/fs/get", {"path": path, "password": ""})
    if r.get("code") == 200:
        raw = str((r.get("data") or {}).get("raw_url") or "")
        c = G if t < 5 else (Y if t < 20 else R)
        print(f"  ② 取播放地址  {c}{t:6.1f} 秒{X}"
              + (f"  → {raw.split('/')[2]}" if raw.startswith("http") else
                 f"  {R}没给地址{X}"))
    else:
        print(f"  ② 取播放地址  {R}失败{X}  {str(r.get('message'))[:70]}  ({t:.1f} 秒)")
except Exception as e:
    print(f"  ② 取播放地址  {R}没回话{X}  {type(e).__name__}"
          f"{D}（网页上就是一直转圈）{X}")

# ---- ③ 真的拉一段，确认这条地址能不能出数据 ----
if raw.startswith("http"):
    N = 1 << 20
    req = urllib.request.Request(raw, headers={"User-Agent": "Mozilla/5.0",
                                               "Range": f"bytes=0-{N - 1}"})
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=60) as resp:
            got = len(resp.read(N))
        t = time.monotonic() - t0
        mbps = got * 8 / t / 1e6 if t > 0 else 0
        c = G if mbps >= 8 else (Y if mbps >= 3 else R)
        print(f"  ③ 拉 1 MiB    {c}{t:6.1f} 秒{X}  {got / 1024:.0f} KB  "
              f"{c}{mbps:.1f} Mbps{X}"
              f"{D}（{got / t / 1024:.0f} KB/s）{X}")
    except urllib.error.HTTPError as e:
        print(f"  ③ 拉 1 MiB    {R}HTTP {e.code}{X}  地址拿到了，网盘拒绝下载")
    except Exception as e:
        print(f"  ③ 拉 1 MiB    {R}拉不动{X}  {type(e).__name__}")

print()
print(f"  {D}怎么读这三行：{X}")
print(f"  {D}① 慢  网盘对【列目录】限流。跟播放无关，但翻目录就是难受{X}")
print(f"  {D}② 慢/没给地址  卡在【换播放地址】。这一步是网页转圈的正主{X}")
print(f"  {D}③ 慢  地址是好的，拉不动 —— 那才是限速或者线路{X}")
print()
print(f"  {Y}② 特别要注意刚上传/刚转存的片子：{X}")
print(f"  {D}夸克/UC 的驱动如果设成【转码流】，取的是网盘转好码的那一路。而转码是网盘{X}")
print(f"  {D}在后台慢慢做的，刚放进去的片子【还没转完】—— 这一步就会一直等或者直接空。{X}")
print(f"  {D}判断方法：拿同一个目录里的【老片子】再跑一次这个脚本。老的秒回、新的卡住，{X}")
print(f"  {D}就是在等网盘转码，不是你的机器有问题。{X}")
print(f"  {D}不想等：media-stack →「4 挂载路径」→ 选这个盘 →「2 直链方式」→ 原画直链。{X}")
PY

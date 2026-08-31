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

TOOL_VER="2026-08-31c"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

P="${1:-}"
[ -n "$P" ] || { echo "用法：bash ${0##*/} \"/quark/夸克挂载/动漫/某剧/238 4K.mp4\""; exit 1; }

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

export OL_PATH="$P" OL_PW="$OLPW"
python3 - <<'PY'
import json, os, re, subprocess, time, urllib.error, urllib.request

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


# ---- OpenList 自己说了什么。上面那几行是"卡在哪"，这一段才是"为什么" ----
#
# 【必须脱敏】OpenList 的报错里带着 access_token（"failed get link: ... token=eyJ..."），
# 而这个输出是会被截图发出来的。整串一律打码，只留够判断的前几位。
#
# 【目录和文件两条路都要能走到】早一版把它写在文件那条路的末尾，而查目录的那条
# 中途就 SystemExit 了 —— "新文件夹刷不出来"恰恰最需要看日志，偏偏一行都看不到。
# 【键名前面【不要】限定 ? 或 &】日志里常常是裸的 "token=xxx"，前面既没有问号也没有
# & —— 实测就是这么漏出去一整串的。用 \b 认词边界，两种写法都盖得住。
TOK = re.compile(r"\b((?:access_token|refresh_token|token|auth_key|cookie|sign|"
                 r"password|pwd)=)[^&\s\"']+", re.I)
JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
# 【24 不是 40】阈值定在 40 时，一串 39 位的令牌原样留在了屏幕上。宁可把长一点的
# 容器 id、哈希也截断 —— 那些截断了不影响判断，令牌漏一次就是漏了。
LONG = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(line):
    line = ANSI.sub("", line)
    line = TOK.sub(lambda m: m.group(1) + "***", line)
    line = JWT.sub("eyJ***", line)
    return LONG.sub(lambda m: m.group(0)[:6] + "***", line)


def show_logs():
    print()
    print(f"  {B}OpenList 最近说了什么{X}  {D}（最近 10 分钟里带报错的行，令牌已打码）{X}")
    print("=" * 58)
    try:
        out = subprocess.run(["docker", "logs", "--since", "10m", "openlist"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            # docker 自己的报错不能混进来当成 OpenList 说的话 —— 那会让人以为
            # 网盘出了问题，其实是这台机器上根本没读到容器
            print(f"  {D}读不到 openlist 的日志：{out.stderr.strip()[:120]}{X}")
            raise SystemExit(0)
        bad = [ln for ln in (out.stdout + out.stderr).splitlines()
               if re.search(r"(?i)error|fail|invalid|expire|denied|refus|timeout|"
                            r"429|401|403|too many|limit", ln)]
        if not bad:
            print(f"  {G}✔{X} 最近 10 分钟没有报错 —— 那就不是 OpenList 这一层的问题")
        for ln in bad[-12:]:
            print(f"  {D}·{X} {clean(ln)[:200]}")
        if len(bad) > 12:
            print(f"  {D}…另外 {len(bad) - 12} 行：docker logs --since 10m openlist{X}")
    except FileNotFoundError:
        print(f"  {D}这台机器上没有 docker 命令，跳过{X}")
    except Exception as e:
        print(f"  {D}读日志失败：{type(e).__name__}{X}")
    print()
    print(f"  {D}这一段里常见的几句，各自是什么意思：{X}")
    print(f"  {D}· too many requests / 429      网盘在限流，等一会儿{X}")
    print(f"  {D}· token / 401 / unauthorized   授权失效了，去 OpenList 重新扫码{X}")
    print(f"  {D}· context deadline exceeded    网盘接口没在时限内回话，限流或线路{X}")
    print(f"  {D}· 一行都没有                    问题不在 OpenList，往浏览器/线路那边看{X}")


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

# ---- ⓪ 这个盘此刻的状态。问【接口】，不是 sqlite 里那份陈年记录 ----
#
# x_storages.status 是【存储初始化那一刻】写进去的，之后恢复了也不会改回 work，
# 拿它当实时状态用会把陈年记录报成当前故障。接口给的这份才是此刻的。
mount = ""
try:
    r, _ = api("/api/admin/storage/list", {"page": 1, "per_page": 100}, tok, timeout=30)
    for st in ((r.get("data") or {}).get("content") or []):
        mp = str(st.get("mount_path") or "")
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            mount = mp
            stat = str(st.get("status") or "")
            dis = st.get("disabled")
            c = G if (stat == "work" and not dis) else R
            print(f"  ⓪ 这个盘      {c}{stat or '?'}{X}"
                  f"{'  ' + R + '已停用' + X if dis else ''}"
                  f"  {D}{mp}  {st.get('driver', '')}"
                  f"  缓存 {st.get('cache_expiration', '?')} 分钟{X}")
            break
    else:
        print(f"  ⓪ 这个盘      {Y}没找到对应的存储{X}  {D}路径打错了？{X}")
except Exception as e:
    print(f"  ⓪ 这个盘      {D}问不到存储状态（{type(e).__name__}）{X}")

# ---- ① 列目录。带 refresh 才是问网盘，不带就是读缓存 ----
#
# 【名字要打出来，不能只报个数】"新加的文件夹刷不出来"这种问题，只有把强制刷新之后
# 网盘【真正返回】的那份名单摆在眼前，才分得清是"缓存旧了"还是"网盘自己就没给"。
# 报一个总数等于什么都没说。
#
# 给的路径是文件夹就列它自己，是文件就列它所在的那一层 —— 不靠扩展名猜，
# 先按文件夹试一次，OpenList 说不是文件夹再退回上一层。
target, is_file = path, False
try:
    r, t = api("/api/fs/list", {"path": target, "password": "", "page": 1,
                                "per_page": 100, "refresh": True})
    if r.get("code") != 200:
        target, is_file = parent, True
        r, t = api("/api/fs/list", {"path": target, "password": "", "page": 1,
                                    "per_page": 100, "refresh": True})
    if r.get("code") == 200:
        data = r.get("data") or {}
        items = data.get("content") or []
        c = G if t < 5 else (Y if t < 20 else R)
        print(f"  ① 列目录      {c}{t:6.1f} 秒{X}  {data.get('total', len(items))} 项"
              f"  {D}{target}{X}")
        want = path.rsplit("/", 1)[-1] if is_file else ""
        for it in items[:40]:
            nm = str(it.get("name") or "")
            mark = f"  {G}← 就是它{X}" if nm == want else ""
            kind = "📁" if it.get("is_dir") else "  "
            print(f"       {kind} {nm}{mark}")
        if len(items) > 40:
            print(f"       {D}…另外 {len(items) - 40} 个{X}")
        if want and not any(str(i.get('name') or '') == want for i in items):
            print(f"       {R}✖ 刷新之后网盘也没给出「{want}」{X}")
    else:
        print(f"  ① 列目录      {R}失败{X}  {str(r.get('message'))[:60]}  ({t:.1f} 秒)")
        is_file = True
except Exception as e:
    print(f"  ① 列目录      {R}没回话{X}  {type(e).__name__}")
    is_file = True

if not is_file:
    print()
    print(f"  {D}上面这份是【强制刷新后网盘真正返回的】名单。{X}")
    print(f"  {D}要找的文件夹不在里面 = 网盘那边就没有（分享目录的自动更新还没同步、{X}")
    print(f"  {D}或者转存进的是另一个目录），跟本机的缓存无关，清缓存也变不出来。{X}")
    show_logs()
    raise SystemExit(0)

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

show_logs()
PY

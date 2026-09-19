#!/usr/bin/env bash
# 「能播，但过几秒卡一下」专用 —— 不用你去点播放，脚本自己当一回播放器把这部片拉一遍。
#
#   bash why-stall.sh                【每个盘都查一遍】，各随机挑一部 —— 不用想片名
#   bash why-stall.sh /quark          只查这个盘，脚本自己挑一部
#   bash why-stall.sh 龙虎门          只查这一部，拉 45 秒
#   bash why-stall.sh 龙虎门 90       拉 90 秒
#   MS_N=2 bash why-stall.sh 龙虎门   同名的有好几个时，查第 2 个
#   MS_SECS=60 bash why-stall.sh      不填参数时每个盘拉多少秒（默认 30）
#
# 【不填参数是主用法】"到底哪个盘有问题"本来就得几个盘并排看才谈得上比较，
# 而逼人先想出一个片名、或者先挑一个盘，都只是在中间多加一道来回。
#
# 【为什么要重写一个，cant-play.sh 不够用】cant-play.sh 回答的是「能不能拉到数据」，
# 它最多连着拉 4 秒、而且是【全速】拉。可是"卡一下"根本不是拉不到数据 ——
# 是【这一秒该来的那一段没按时到】。全速拉 4 秒测不出这件事：
#
#   · 全速拉 = 一直有数据在路上，源那边的限速桶还没空
#   · 真播放 = 缓冲满了就停手，过两秒再要下一段 —— 停手这件事本身，
#     在很多源上就是被降速/被断流的触发点
#
# 所以这个脚本按【播放器的样子】拉：一段一段地要，缓冲满了就歇，播掉了再要，
# 并且每秒记一次缓冲水位。缓冲见底的那一秒，就是你屏幕上卡的那一下。
#
# 屏上会画出一条时间轴，每秒一个格子：
#
#   █ 缓冲充足   ▆ 还剩几秒   ▃ 快见底   ▁ 只剩一点   ✖ 见底了（这一秒在卡）
#
# 一眼就能分出三种完全不同的病，而它们在客户端上长得一模一样（都是"转一下"）：
#
#   ██████▆▃▁✖▁▃▆████████▆▃▁✖▁▃▆████   周期性的，源在限速（令牌桶）
#   ████████▆▆▅▅▄▄▃▃▂▂▁▁✖✖✖✖✖✖✖✖✖✖✖   一路衰减，越拉越慢（被降速/线路劣化）
#   ████████✖✖✖✖✖✖✖✖███████████████   断了一大段（连接被掐，看 ④ 的报错）
#   ▁▁▁✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖✖   从头到尾就没跟上（纯粹是带宽不够）
#
# 同一个文件还会走【两条路】各拉一遍 —— 网盘 CDN 直链、和经过你 VPS 的本机代理 ——
# 好回答那个真正要决定的问题：这部片到底该走哪条。
#
# 【④ 那一段单独量"换一段要多少钱"，它比时间轴更要紧】上面这个模拟播放器缓冲有
# 12 秒，足够把每次换段的等待盖过去 —— 它自己不卡，可真播放器缓冲小得多，而且
# 开播探测、拖进度条、每次续缓冲都要重发请求，每发一次就交一次这个钱。所以④
# 只量【从发出请求到第一个字节回来】用了多久，并且分两个维度看它贵在哪：
#
#   同样 8 MiB，换三个位置要 → 越靠后越慢 = 上游不支持真 Range，在空转到那个位置
#   同样位置，换三种大小要   → 越大越慢   = 上游要把整段备齐才发
#   两个都无关               → 这是【每发一次请求就交一次】的固定开销
#
# 最后那种最常见也最隐蔽：带宽明明够，可播放器每 4 秒要一段、每次停 2 秒，
# 就只有三分之二的时间在传数据；要是客户端一次只要 1 秒的量，直接只剩三分之一。
# 这一段只要首字节就撒手，六次加起来不到 1 MB。
#
# 只读，不改任何东西。
#
# 【要花多少流量】按播放码率拉，不是全速拉，所以大致就是「片子码率 × 秒数」。
# 一部 10 Mbps 的片拉 45 秒 ≈ 56 MB，两条路各一遍 ≈ 112 MB。跑之前屏上会先报数。
set -u

TOOL_VER="2026-09-19l"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

# 【不填就把每个盘都跑一遍】原来这里没填就直接退出，只留一句带尖括号的用法 ——
# 尖括号在 bash 里是重定向，照着敲就是 syntax error。后来改成列个菜单让人再敲
# 一次，还是不对：人要的是结论，不是菜单。现在不填就自己跑完所有盘。
Q="${1:-}"
SECS="${2:-45}"
DIR="${MS_DIR:-/opt/media-stack}"

KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key（先跑「3 后补参数」）"; exit 1; }
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
# 【拿来打码用的，不是拿来拼地址的】这一屏会被截图发出来，而 list.<域名> 出现在
# 直链里 —— 域名是安装人的信息，屏上一律换成 <你的域名>。
DOMAIN="$(sed -nE 's/^DOMAIN=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"

# 【日志走临时文件，不能用管道】python3 - <<'PY' 是从 stdin 读程序，
# 再往 stdin 里灌日志，两边抢同一个口子。link-history.sh 上栽过一次。
MWLOG="$(mktemp)"
# 【程序也落成文件，因为要跑不止一次】不填参数时每个盘都要跑一遍，
# 而 heredoc 只能喂给 stdin 一次。
PYF="$(mktemp)"
trap 'rm -f "$MWLOG" "$PYF"' EXIT
docker logs --tail 4000 mediawarp >"$MWLOG" 2>&1 || : >"$MWLOG"
# MediaWarp 上次是什么时候起来的 —— 用来判断"自愈到底跑没跑过"
MWSTART="$(docker inspect -f '{{.State.StartedAt}}' mediawarp 2>/dev/null || true)"

cat >"$PYF" <<'PY'
import json, os, re, socket, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request

KEY, OLPW, DATA_ROOT, DOMAIN, SECS, Q, MWLOG = sys.argv[1:8]
MWSTART = sys.argv[8] if len(sys.argv) > 8 else ""
EMBY, OL = "http://127.0.0.1:8096", "http://127.0.0.1:5244"
MW = "http://127.0.0.1:9000"          # MediaWarp —— Emby 那条路上唯一算数的那一段
NGXLOG = "/var/log/nginx/media-stack.access.log"
B, D, R, G, Y, C, X = ("\033[1m", "\033[2m", "\033[31m", "\033[32m",
                       "\033[33m", "\033[36m", "\033[0m")
# 【故意伪装浏览器】网盘按 UA 封，不装成浏览器测出来的全是它对陌生 UA 的态度，
# 不是它对播放器的态度。CLAUDE.md 里 BROWSER_UA 这条例外说的就是这个。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

try:
    SECS = max(15, min(300, int(SECS)))
except ValueError:
    SECS = 45

# ---------------------------------------------------------------- 脱敏
# 键名不限定前面是 ? 还是 &（裸 token= 也要盖）；长串阈值 24（39 位的令牌用 40
# 做阈值时原样留在了屏幕上）。域名单独换掉 —— 那是安装人的信息。
TOK = re.compile(r"\b((?:access_token|refresh_token|token|auth_key|cookie|sign|"
                 r"password|pwd|api_key)=)[^&\s\"']+", re.I)
LONG = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def safe(s):
    s = str(s or "")
    if DOMAIN and len(DOMAIN) > 3:
        s = s.replace(DOMAIN, "<你的域名>")
    s = TOK.sub(r"\1…", s)
    return LONG.sub(lambda m: m.group(0)[:6] + "…", s)


def hr():
    print("  " + "-" * 60)


def sec(t):
    print()
    print(f"  {B}{t}{X}")
    hr()


def emby(path, timeout=60):
    u = f"{EMBY}{path}{'&' if '?' in path else '?'}api_key={KEY}"
    with urllib.request.urlopen(u, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body.strip() else {}


def mb(n):
    return f"{n / 1024 / 1024:.0f} MB" if n < 1 << 30 else f"{n / 1024 ** 3:.2f} GB"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """要的就是 302 本身。跟随了就看不见它去了哪儿、每一跳花了多久。"""
    def redirect_request(self, *_a, **_k):
        return None


NOFOLLOW = urllib.request.build_opener(NoRedirect)

# ---------------------------------------------------------------- 后台任务
# 【这一项决定下面所有数字怎么读】预热和补探测打的是同一个网盘。它们在跑的时候
# 量出来的"卡"，测的是排队排不上，不是这条链本身。cant-play.sh 里同一套判法。
BG_SUBS = {"warm": "直链预热", "heal": "补探测", "sync": "每日对齐",
           "keepalive": "链路保活", "precache": "目录预热", "strm": "生成媒体库"}


def bg_tasks():
    out = []
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
        up = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError, AttributeError):
        return out
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = [a.decode("utf-8", "replace") for a in
                    open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
            if len(args) < 3 or not any(a.endswith("media-stack.py") for a in args):
                continue
            if args[-1] not in BG_SUBS or not os.path.basename(args[0]).startswith("python"):
                continue
            st = open(f"/proc/{pid}/stat").read()
            age = up - float(st[st.rindex(")") + 2:].split()[19]) / hz
            out.append((BG_SUBS[args[-1]], max(0, int(age))))
        except (OSError, ValueError, IndexError):
            continue
    return out


# ---------------------------------------------------------------- 挑哪一部
# 【为什么要能"按盘挑"】要查的往往不是某一部片，而是【某个盘】——"夸克到底行不行"。
# 逼人先想出一个片名是多余的一步，而且会出岔子：给出去的例子只要带尖括号，
# 照着敲就是 bash 的重定向、当场 syntax error。所以填挂载点（/quark）也行，
# 什么都不填就把有哪些盘列出来。
STRM_ROOT = os.path.join(DATA_ROOT, "strm")


def walk_strm():
    """本地所有 strm：[(宿主机路径, 里面写的网盘路径), ...]。文件都很小，走一遍很快。"""
    out = []
    for dirpath, _dirs, files in os.walk(STRM_ROOT):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                line = open(p, encoding="utf-8", errors="replace").read().strip()
            except OSError:
                continue
            if line:
                out.append((p, line))
    return out


def drives_here(rows):
    """这些 strm 分布在哪几个盘上：{挂载点: 有几部}。"""
    d = {}
    for _p, line in rows:
        if line.startswith("/"):
            mp = "/" + line.lstrip("/").split("/", 1)[0]
            d[mp] = d.get(mp, 0) + 1
    return d


def item_for(cpath):
    """按【宿主机上的 strm 路径】找回它在 Emby 里的那一条。"""
    rel = os.path.relpath(cpath, STRM_ROOT)
    want = "/data/strm/" + rel.replace(os.sep, "/")
    stem = os.path.splitext(os.path.basename(cpath))[0]
    for term in (stem, stem[:12], os.path.basename(os.path.dirname(cpath))):
        if not term:
            continue
        try:
            res = emby(f"/Items?Recursive=true&SearchTerm={urllib.parse.quote(term)}"
                       f"&IncludeItemTypes=Movie,Episode,Video&Limit=30"
                       f"&Fields=Path,MediaSources,MediaStreams")
        except Exception:
            return None
        for i in res.get("Items") or []:
            if str(i.get("Path") or "") == want:
                return i
    return None


# 【机器读的那一档】外面那层 bash 拿它来决定要跑哪几个盘。一行一个挂载点，
# 片多的排前面，不带颜色也不带别的话。
if Q == "--drives":
    for mp, _n in sorted(drives_here(walk_strm()).items(), key=lambda kv: -kv[1]):
        print(mp)
    raise SystemExit(0)

sec("① 这部片，以及它每秒要多少")
rows = walk_strm()
if not Q:
    # 【不该在这里停下来让人再敲一次】上一版这里打的是"有这几个盘、你去挑一个"，
    # 于是又多一个来回 —— 而人要的从来不是一份菜单，是【结论】。现在外面那层
    # bash 不填参数时会自己把每个盘都跑一遍，这个分支只在它没起作用时兜底。
    have = drives_here(rows)
    print(f"  {Y}没说要查哪一部，也没人替我挑{X}")
    if have:
        print(f"  {D}这台机器上有：{'、'.join(f'{m}（{n} 部）' for m, n in sorted(have.items(), key=lambda kv: -kv[1]))}{X}")
        print(f"  {B}挑一个盘跑：{X}"
              f"  bash why-stall.sh {sorted(have.items(), key=lambda kv: -kv[1])[0][0]}")
    else:
        print(f"  {D}{STRM_ROOT} 下面一个 strm 都没有 —— 先点「5 生成媒体库」。{X}")
    raise SystemExit(0)

it = None
if Q.startswith("/"):
    # 按盘挑：从这个盘里随便挑一部能对上 Emby 条目的
    mp = "/" + Q.strip("/").split("/", 1)[0]
    cand = [p for p, line in rows if line == mp or line.startswith(mp + "/")]
    if not cand:
        have = drives_here(rows)
        print(f"  {R}✖ {mp} 这个盘下面一部片都没有{X}")
        if have:
            print(f"  {D}有的是：{'、'.join(sorted(have))}{X}")
        raise SystemExit(1)
    import random
    random.shuffle(cand)
    print(f"  {D}{mp} 下面有 {len(cand)} 部，随便挑一部来测{X}")
    for p in cand[:6]:
        it = item_for(p)
        if it:
            break
    if not it:
        print(f"  {R}✖ 挑了几部，都在 Emby 库里找不到对应条目{X}")
        print(f"  {D}点一次「5 生成媒体库」再回来。{X}")
        raise SystemExit(1)
else:
    try:
        res = emby(f"/Items?Recursive=true&SearchTerm={urllib.parse.quote(Q)}"
                   f"&IncludeItemTypes=Movie,Episode,Video&Limit=30"
                   f"&Fields=Path,MediaSources,MediaStreams")
    except Exception as e:
        print(f"  {R}✖ 连不上 Emby：{safe(e)}{X}")
        raise SystemExit(1)
    hit = [i for i in (res.get("Items") or [])
           if str(i.get("Path") or "").endswith(".strm")]
    if not hit:
        print(f"  {R}✖ 库里找不到带 strm 的条目：{Q}{X}")
        _have = sorted(drives_here(rows))
        print(f"  {D}换个更短的关键词；或者直接填挂载点"
              f"（比如 {_have[0] if _have else '/某个盘'}），脚本自己挑一部。{X}")
        raise SystemExit(1)
    try:
        pick = int(os.environ.get("MS_N") or "1")
    except ValueError:
        pick = 1
    pick = pick if 1 <= pick <= len(hit) else 1
    it = hit[pick - 1]
    if len(hit) > 1:
        print(f"  {D}找到 {len(hit)} 个，这次查第 {pick} 个（换别的：MS_N=2 ...）{X}")

iid = str(it.get("Id") or "")
cpath = str(it.get("Path") or "")
srcs = it.get("MediaSources") or []
size = int((srcs[0].get("Size") if srcs else 0) or 0)
secs_len = (it.get("RunTimeTicks") or 0) / 1e7
print(f"  {B}{it.get('Name')}{X}  {D}条目 {iid}{X}")

# 【码率是这一屏的分母】没有它，后面"够不够"一句都说不了 —— 快慢是相对于
# 这部片每秒要多少而言的，不存在一个绝对的"多少 Mbps 算快"。
need_bps = 0
if srcs:
    need_bps = int(srcs[0].get("Bitrate") or 0)
if not need_bps and size and secs_len:
    need_bps = int(size * 8 / secs_len)
if secs_len:
    print(f"  {D}大小 {mb(size) if size else '(Emby 还没探到)'}"
          f"　时长 {int(secs_len // 60)} 分{X}")
if not need_bps:
    print(f"  {Y}⚠ 读不到这部片的码率（Emby 还没探到时长或大小）{X}")
    print(f"  {D}没有码率就没法判「够不够」—— 先让它补上时长"
          f"（点开看一眼，几分钟后自动补），再回来跑。{X}")
    print(f"  {D}这次先按 8 Mbps 估着测，结论只当参考。{X}")
    need_bps = 8_000_000
print(f"  {D}平均码率  {X}{C}{need_bps / 1e6:.1f} Mbps{X}"
      f"  {D}（每秒要 {need_bps / 8 / 1024 / 1024:.2f} MB —— 这就是"
      f"后面所有速度的及格线）{X}")

BG = bg_tasks()
if BG:
    print()
    print(f"  {Y}⚠ 此刻有后台任务正在打同一个网盘：{X}"
          f"{B}{'、'.join(f'{n}（已跑 {a // 60} 分{a % 60} 秒）' for n, a in BG)}{X}")
    print(f"  {D}它们和这次测试在抢同一份配额 —— 下面量出来的卡有一部分是它们造成的。"
          f"要测准就先 systemctl stop cron 再跑。{X}")

# ================= ② 网盘上那个文件 =================
sec("② 这条 strm 指到哪儿，OpenList 认不认")
hpath = (DATA_ROOT + "/strm/" + cpath[len("/data/strm/"):]
         if cpath.startswith("/data/strm/") else "")
if not hpath or not os.path.isfile(hpath):
    print(f"  {R}✖ 宿主机上找不到这个 strm{X}  {D}{cpath}{X}")
    print(f"  {D}先跑 cant-play.sh —— 这是「根本播不了」那一类，不是「卡一下」。{X}")
    raise SystemExit(1)
body = open(hpath, encoding="utf-8", errors="replace").read().strip()
if body.lower().startswith(("http://", "https://")):
    print(f"  {R}✖ strm 是 URL 形式，MediaWarp 认不出{X}  {D}先跑 cant-play.sh{X}")
    raise SystemExit(1)
print(f"  {G}✔{X} 网盘路径  {D}{body}{X}")

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
    print(f"  {R}✖ 没有 OpenList 令牌，测不了{X}  {D}读不到 {DATA_ROOT}/../.secrets{X}")
    raise SystemExit(1)

def ol_post(path, payload, timeout=120):
    try:
        req = urllib.request.Request(
            f"{OL}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": tok},
            method="POST")
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except Exception as e:
        return {"code": -1, "message": str(e)}


raw, sign, ol_size = "", "", 0
r = ol_post("/api/fs/get", {"path": body, "password": ""})
if r.get("code") != 200:
    msg = str(r.get("message") or "")
    print(f"  {R}✖ OpenList 这一次没给出直链{X}  {D}{safe(msg)[:120]}{X}")
    # 【"找不到"分两种，而且这件事【本身】就可能是要找的答案】
    #   · 上游真的删了/改名了       → 本地这条 strm 是废的，重建媒体库
    #   · 这一轮撞上限流 / 没列全   → 文件好好的，过一会儿自己就好
    # 后一种最要命：它【不是稳定复现的故障】，而 MediaWarp 在播放那一刻要是撞上
    # 同一下，就是拿不到直链 → 客户端一直转圈。也就是说"偶尔才有"正是症状本身，
    # 不是测试没测准。所以这里不直接退出，先去把它分开。
    if "not found" in msg.lower():
        parent = os.path.dirname(body)
        want = os.path.basename(body)
        print(f"  {D}去列一次它的父目录（带 refresh，绕开缓存）看名字还在不在...{X}")
        rl = ol_post("/api/fs/list", {"path": parent, "password": "", "page": 1,
                                      "per_page": 0, "refresh": True}, timeout=180)
        names = [str(x.get("name") or "")
                 for x in ((rl.get("data") or {}).get("content") or [])]
        if rl.get("code") != 200:
            print(f"  {R}父目录也列不出来{X}  {D}{safe(rl.get('message'))[:100]}{X}")
            print(f"  {D}整个存储这会儿都不通 —— 播放当然也拿不到直链。{X}")
        elif want in names:
            print(f"  {Y}⚠ 文件【在】—— 刚才那下是这个源自己抽了一下{X}")
            print(f"  {B}这就是症状本身，不是测试没测准{X}")
            print(f"  {D}MediaWarp 在你点播放的那一刻要是撞上同一下，就拿不到直链，"
                  f"客户端表现正是一直转圈；隔一会儿再点又好了 —— "
                  f"「有时能播有时不能」就是这么来的。{X}")
            print(f"  {D}这类源按请求频率抽风，所以【少打扰它】是唯一的办法："
                  f"预热已经跳过这类盘（v1.5.109 起），补探测也只补点开过的"
                  f"（v1.5.108 起）。跑测试前 systemctl stop cron 能再少一批。{X}")
            print(f"  {D}再要一次直链...{X}")
            time.sleep(3)
            r = ol_post("/api/fs/get", {"path": body, "password": ""})
            if r.get("code") == 200:
                print(f"  {G}✔ 这次给了 —— 果然是间歇性的{X}")
        else:
            print(f"  {R}✖ 父目录里确实没有这个名字了{X}")
            print(f"  {D}上游把它删了或改名了，本地这条 strm 是废的。"
                  f"点一次「5 生成媒体库」。{X}")
    if r.get("code") != 200:
        raise SystemExit(1)
dat = r.get("data") or {}
raw = str(dat.get("raw_url") or "")
sign = str(dat.get("sign") or "")
ol_size = int(dat.get("size") or 0) or size
host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", raw) if raw else ""
is_local = host.split(":")[0] in ("127.0.0.1", "localhost", "openlist")
print(f"  {G}✔{X} 文件 {mb(ol_size)}　直链指向 {C}{safe(host)}{X}"
      + (f"  {Y}← OpenList 自己（这盘没有 CDN 直链）{X}" if is_local
         else f"  {G}← 网盘 CDN{X}"))

# 【及格线的分子要用网盘报的大小，不是 Emby 记的】码率 = 大小 × 8 ÷ 时长。
# 现场撞到过：同一部片 Emby 记 3.69 GB、网盘上是 17.72 GB，差了近五倍 ——
# 于是整屏的"够不够"全是照着一条低了五倍的及格线判的，"带宽够"这三个字直接作废。
# Emby 那个数是它探测时记下来的，文件换过、或者压根没探全，它就不会自己更新。
if ol_size and secs_len:
    _real = int(ol_size * 8 / secs_len)
    if need_bps and abs(_real - need_bps) > need_bps * 0.3:
        print(f"  {Y}⚠ Emby 记的大小和网盘上的对不上{X}"
              f"  {D}Emby：{mb(size)}　网盘：{mb(ol_size)}{X}")
        print(f"  {B}及格线按网盘那个重算：{need_bps / 1e6:.1f} → "
              f"{_real / 1e6:.1f} Mbps{X}")
        print(f"  {D}Emby 那条媒体信息是旧的（文件换过、或者当初没探全）。"
              f"后面所有「够不够」都按新的这个判。{X}")
        need_bps = _real
    elif not need_bps:
        need_bps = _real

# 【转码流要当场点名】这条直链的主机名/路径里带着它的出身。转码流在 Emby 里
# 播不了（302 过去是 m3u8，分片是相对路径，被播放器拼回 /emby/Videos/<id>/ → 401
# → 一直转圈），而客户端上只有"转圈"两个字，不点名根本想不到是这个开关。
_lowraw = raw.lower()
if ("m3u8" in _lowraw or "video-play" in _lowraw or "transcod" in _lowraw
        or "/hls" in _lowraw):
    print(f"  {R}✖ 这条直链给的是【转码流】，不是原文件{X}")
    print(f"  {D}主机/路径里写着（video-play / m3u8 / hls）。也就是说这个盘的"
          f"「直链方式」还设在转码流上。{X}")
    print(f"  {D}转码流在 Emby 里播不了：302 过去是 m3u8，而 m3u8 里的分片写的是"
          f"相对路径，播放器会把它拼到 /emby/Videos/<条目id>/ 上，于是分片请求"
          f"全打回 Emby → 一路 401 → 一直转圈。{X}")
    print(f"  {B}改法：media-stack → 4 挂载路径 → 选这个盘 → 3 直链方式 → 原画直链{X}")
    print(f"  {D}（脚本 v1.5.110 起不再把新装的机器默认设成转码流，但【已经设过的"
          f"不会自动改回来】—— 得自己动手。）{X}")

# 【要测的是两条路，不是两个地址】
#   /d/ → 存储怎么配就怎么走（有 CDN 直链就 302 出去）= Emby 现在拿到的那条
#   /p/ → 强制经过 OpenList 本机代理             = 「改成本机代理」之后的那条
# 后者不用真去改配置就能先量一遍，这是这个脚本存在的主要理由：
# 那个决定（要不要给这个盘开本机代理）以前只能靠猜。
# ================= ②c MediaWarp 真给客户端的是哪个地址 =================
# 【这一段才是 Emby 走的那条路，前面那些都不是】上面那个 raw_url 是【脚本自己
# 在宿主机上用 127.0.0.1 问出来的】。而代理型存储的直链主机名是「谁来问就按谁
# 用的主机名拼」—— MediaWarp 在容器里用 http://openlist:5244 去问，拿回来的就是
# openlist:5244，然后把【那个地址】302 给你的手机。手机解析不了 openlist 这个
# 名字，于是 load fail / 一直转圈。
#
# 而整条链的每一步都是"成功"的：strm 对、OpenList 认得、直链拉得动（在 VPS 上），
# 只有最后那个地址是废的。这就是它极难自己看出来的原因，也是这个脚本一直没测到
# 七米蓝毛病的原因 —— 我测的是从 VPS 拉 127.0.0.1，那跟客户端拿到的不是一回事。
sec("②c MediaWarp 真给客户端的是哪个地址")
print(f"  {D}上面那条直链是脚本自己问出来的。这一步去问 MediaWarp —— "
      f"它给客户端什么，Emby 那条路上算数的就是什么。{X}")
mw_url, mw_host, mw_inner = "", "", False
_mwq = (f"{MW}/Videos/{iid}/stream?MediaSourceId=mediasource_{iid}"
        f"&Static=true&api_key={KEY}")
_t0 = time.time()
try:
    _rr = NOFOLLOW.open(_mwq, timeout=60)
    _code, _loc = _rr.status, ""
except urllib.error.HTTPError as _e:
    _code, _loc = _e.code, _e.headers.get("Location", "") or ""
except Exception as _e:
    _code, _loc = 0, ""
    print(f"  {R}✖ 问不到 MediaWarp：{safe(_e)}{X}")
_el = time.time() - _t0
if _loc:
    mw_url = _loc
    mw_host = re.sub(r"^[a-z]+://([^/]+).*", r"\1", _loc)
    _bare = mw_host.split(":")[0]
    # 【私网段要按段判，不能按前缀猜】172.x 只有 16-31 那一段是私网，
    # 172.1.x / 172.9.x 是正经公网地址 —— 按 "172." 开头一刀切会把好地址判成坏的。
    _p = _bare.split(".")
    mw_inner = (_bare in ("openlist", "emby", "mediawarp", "autofilm", "localhost")
                or _bare.startswith(("127.", "10.", "192.168."))
                or (len(_p) == 4 and _p[0] == "172" and _p[1].isdigit()
                    and 16 <= int(_p[1]) <= 31))
    _inner = mw_inner
    print(f"  {G}✔ 302{X}  {D}{_el:.1f} 秒{X}  →  {C}{safe(mw_host)}{X}"
          + (f"  {D}HLS 分片流{X}" if ".m3u8" in _loc.lower() else ""))
    if _inner:
        print(f"  {R}✖ 这是个内网地址 —— 你的手机/电视根本连不上{X}")
        print(f"  {D}代理型存储（WebDAV 源、本地目录）在网盘侧没有 CDN 直链，"
              f"OpenList 只能回自己的地址；而那个地址的主机名是【谁来问就按谁用的"
              f"主机名拼】。MediaWarp 在容器里用 http://openlist:5244 去问，"
              f"拿回来的就是 openlist:5244。{X}")
        print(f"  {D}整条链每一步都「成功」—— strm 对、OpenList 认得、直链在这台"
              f"机器上也拉得动 —— 只有最后那个地址是废的。客户端上只有一句 "
              f"load fail 或者一直转圈。{X}")
        print(f"  {B}修：media-stack → 7 更新{X}"
              f"{D}（会把 MediaWarp 问 OpenList 的地址改成对外地址，"
              f"脚本要 v1.5.58 以上）{X}")
        print(f"  {D}改完已经缓存的旧地址要等直链缓存过期才换掉；"
              f"等不及就 docker restart mediawarp。{X}")
    else:
        print(f"  {G}是对外地址 —— 客户端至少解析得了{X}")
        print(f"  {D}下面就拿【这一条】去拉，而不是脚本自己换出来的那条。{X}")
else:
    print(f"  {R}✖ 没拿到 302{X}  {D}HTTP {_code}，{_el:.1f} 秒{X}")
    print(f"  {D}换不到直链，点开就一直转圈。{X}")
    if _code in (401, 403, 404):
        print(f"  {B}先试：docker restart mediawarp{X}")
        print(f"  {D}它只在启动那一刻登录一次 OpenList，OpenList 一重启旧令牌就废了"
              f" —— 之后每次换直链都被拒。已经缓存过直链的片子照样能放，"
              f"所以看着像「有的能放有的不能放」。{X}")

qpath = urllib.parse.quote(body)
proxy_url = f"{OL}/p{qpath}" + (f"?sign={sign}" if sign else "")
# 【/p/ 必须带上登录凭据，不然全是 403 —— 而那个 403 是脚本自己造的】
# OpenList 的 /p/ 要么认 sign、要么认登录。sign_all 关着的时候 fs/get 回的 sign
# 是空串，光靠 sign 进不去。上一版就栽在这儿：夸克和阿里的"本机代理"那条路
# 清一色 403×15，屏上于是写着「这条路根本起不了播」——【测的是我忘了鉴权】，
# 不是那条路不行。结论错得比没有结论更坏，因为它会让人去改一个没坏的东西。
BASE_HDR = {"User-Agent": UA}
PROXY_HDR = dict(BASE_HDR, Authorization=tok)
routes = []
# 【主角是 MediaWarp 真给客户端的那条】拿得到就用它 —— 别的都是旁证。
# 上一版全程拿脚本自己换出来的地址在拉，那条路在 VPS 上当然通，可它跟客户端
# 拿到的根本不是同一个地址。七米蓝的毛病就是这么被整整漏掉几轮的。
if mw_url:
    routes.append(("MediaWarp 真给客户端的那条", mw_url, BASE_HDR))
    if raw and not is_local:
        routes.append(("经过你的 VPS（本机代理）", proxy_url, PROXY_HDR))
elif raw and not is_local:
    routes.append(("网盘 CDN 直链", raw, BASE_HDR))
    routes.append(("经过你的 VPS（本机代理）", proxy_url, PROXY_HDR))
else:
    routes.append(("经过你的 VPS（这盘只有这一条）", proxy_url, PROXY_HDR))

est = need_bps / 8 * SECS * len(routes)
print(f"  {D}这次要拉 {len(routes)} 条路 × {SECS} 秒 ≈ {mb(est)} 流量{X}")


# ================= 播放器模拟 =================
def follow(url, hdr, depth=4):
    """把 302 跟到底，每一跳记一次耗时。返回 (最终地址, [(码, 秒, 主机), ...])。

    【超时收到 15 秒】现场撞到过一跳报了 183.87 秒才吐出 No route to host ——
    一条通不了的路由不该让整屏干等三分钟，而且那个耗时数字本身没有信息量
    （它等于系统的连接超时，不等于这条链有多慢）。
    """
    hops, cur = [], url
    for _ in range(depth):
        req = urllib.request.Request(cur, headers=dict(hdr, Range="bytes=0-1"))
        t0 = time.time()
        try:
            with NOFOLLOW.open(req, timeout=15) as rr:
                hops.append((rr.status, time.time() - t0,
                             re.sub(r"^[a-z]+://([^/]+).*", r"\1", cur)))
                return cur, hops
        except urllib.error.HTTPError as e:
            el = time.time() - t0
            hops.append((e.code, el, re.sub(r"^[a-z]+://([^/]+).*", r"\1", cur)))
            loc = e.headers.get("Location") if e.code in (301, 302, 303, 307, 308) else ""
            if not loc:
                return cur, hops
            cur = urllib.parse.urljoin(cur, loc)
        except Exception as e:
            hops.append((str(e)[:40], time.time() - t0,
                         re.sub(r"^[a-z]+://([^/]+).*", r"\1", cur)))
            return cur, hops
    return cur, hops


class Tank:
    """播放器的缓冲池。下载线程往里灌，主循环按码率往外舀。"""

    def __init__(self, cap_bytes):
        self.lock = threading.Lock()
        self.got = 0          # 累计下载
        self.played = 0       # 累计消费
        self.cap = cap_bytes
        self.stop = threading.Event()
        self.errs = []        # [(第几秒, 说明)]
        self.ttfb = []        # 每个分段请求的首字节耗时
        self.codes = {}       # 分段请求的状态码分布
        self.t0 = time.time()

    def level(self):
        with self.lock:
            return self.got - self.played

    def note(self, msg):
        self.errs.append((time.time() - self.t0, msg))


def puller(url, tank, start, chunk, hdr):
    """一段一段地要，缓冲满了就歇 —— 播放器就是这么干的。

    【"缓冲满了就歇"必须照做，不能全速拉】很多源是按连接算令牌的：你一直拉，
    桶一直空着、它就一直按限速给；你歇两秒再要，它要么给你一个新桶（表现是
    突然很快），要么把你当成新的可疑请求（表现是 403/429/直接断）。
    全速拉测不出这两种反应里的任何一种，而它们正是"播一会儿卡一下"的两个常见成因。
    """
    pos = start
    while not tank.stop.is_set():
        if tank.level() >= tank.cap:
            time.sleep(0.1)
            continue
        end = pos + chunk - 1
        req = urllib.request.Request(url, headers=dict(hdr, Range=f"bytes={pos}-{end}"))
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as rr:
                tank.ttfb.append(time.time() - t0)
                tank.codes[rr.status] = tank.codes.get(rr.status, 0) + 1
                while not tank.stop.is_set():
                    if tank.level() >= tank.cap:
                        time.sleep(0.1)
                        continue
                    piece = rr.read(1 << 16)
                    if not piece:
                        break
                    with tank.lock:
                        tank.got += len(piece)
                        pos += len(piece)
        except urllib.error.HTTPError as e:
            tank.codes[e.code] = tank.codes.get(e.code, 0) + 1
            tank.note(f"HTTP {e.code}")
            if e.code in (403, 429):
                time.sleep(2)      # 被拒了还猛敲，后面量到的全是自己造的
        except Exception as e:
            tank.note(str(e)[:60])
            time.sleep(1)


BARS = ["✖", "▁", "▃", "▆", "█"]


def bar(sec_left):
    if sec_left <= 0:
        return f"{R}{BARS[0]}{X}"
    if sec_left < 1:
        return f"{R}{BARS[1]}{X}"
    if sec_left < 3:
        return f"{Y}{BARS[2]}{X}"
    if sec_left < 6:
        return f"{Y}{BARS[3]}{X}"
    return f"{G}{BARS[4]}{X}"


def play(url, label, need, total_secs, hdr):
    """当一回播放器：先填 2 秒缓冲再"开播"，然后每秒记一次水位。"""
    print()
    print(f"  {B}{label}{X}")
    final, hops = follow(url, hdr)
    for code, el, hh in hops:
        tag = (f"{G}{code}{X}" if code in (200, 206)
               else f"{C}{code}{X}" if code in (301, 302, 303, 307, 308)
               else f"{R}{code}{X}")
        print(f"    {D}跳 {safe(hh)}  {tag}  {el:.2f} 秒{X}")
    bps = need / 8                      # 每秒要多少字节
    cap = int(bps * 12)                 # 缓冲上限 12 秒（够看出限速，又不多花流量）
    chunk = max(2 << 20, int(bps * 4))  # 一段约 4 秒的量
    start = int(ol_size * 0.1) if ol_size else 0
    tank = Tank(cap)
    th = threading.Thread(target=puller, args=(final, tank, start, chunk, hdr),
                          daemon=True)
    th.start()

    prefill = bps * 2
    started, stalls, samples, last_got = False, 0, [], 0
    t_start = time.time()
    open_at = 0.0
    try:
        for tick in range(total_secs * 5):
            time.sleep(0.2)
            with tank.lock:
                got = tank.got
                if not started:
                    if got >= prefill:
                        started = True
                        open_at = time.time() - t_start
                else:
                    tank.played = min(got, tank.played + bps * 0.2)
                lvl = got - tank.played
            if tick % 5 == 4:
                recv = got - last_got
                last_got = got
                left = lvl / bps if bps else 0
                if started and lvl <= 0:
                    stalls += 1
                samples.append((recv, left, started))
    finally:
        tank.stop.set()
        time.sleep(0.2)

    if not started:
        print(f"    {R}✖ 连开播都没开成{X}  {D}{SECS} 秒里没攒够 2 秒的缓冲"
              f"（拿到 {mb(tank.got)}）{X}")
    else:
        print(f"    {D}起播等了 {open_at:.1f} 秒　拉了 {mb(tank.got)}{X}")
    line = "".join(bar(s[1]) if s[2] else f"{D}·{X}" for s in samples)
    print(f"    {line}")
    print(f"    {D}↑ 一格一秒，共 {len(samples)} 秒"
          f"（█ 缓冲充足　▆ 剩几秒　▃ 快见底　▁ 只剩一点　✖ 见底＝这一秒在卡）{X}")
    avg = tank.got * 8 / max(1.0, total_secs) / 1e6
    print(f"    {D}实测均速 {avg:.1f} Mbps　这部片要 {need / 1e6:.1f} Mbps{X}")
    if tank.codes:
        print(f"    {D}分段请求 {'、'.join(f'{k}×{v}' for k, v in sorted(tank.codes.items()))}"
              f"　首字节中位 {sorted(tank.ttfb)[len(tank.ttfb) // 2]:.2f} 秒{X}"
              if tank.ttfb else
              f"    {D}分段请求 {'、'.join(f'{k}×{v}' for k, v in sorted(tank.codes.items()))}{X}")
    for at, msg in tank.errs[:6]:
        print(f"    {R}第 {at:.0f} 秒：{safe(msg)}{X}")
    if len(tank.errs) > 6:
        print(f"    {D}…还有 {len(tank.errs) - 6} 条同类报错{X}")
    return {"label": label, "started": started, "open_at": open_at,
            "stalls": stalls, "samples": samples, "got": tank.got,
            "avg": avg, "errs": tank.errs, "codes": tank.codes,
            "ttfb": tank.ttfb}


def med(xs):
    return sorted(xs)[len(xs) // 2] if xs else 0.0


def verdict(rs, need):
    """把时间轴的形状翻译成一句人话。"""
    codes = set(rs["codes"])
    if codes and codes <= {403}:
        # 【没试过的路不能判死刑】OpenList 的 /p/ 只对开了本机代理的存储开放，
        # 没开就是 0 秒一个 403。上一版把这判成"根本起不了播"，于是夸克和阿里
        # 各被凭空判了一条死路 —— 而那条路压根没被试过。
        return C, ("这条路没测成：OpenList 不让代理这个盘（/p/ 只对开了"
                   "「本机代理」的存储开放），不是它不行")
    if 200 in codes and 206 not in codes:
        # 【回 200 不是"成功"，是"我不认 Range"】要中间那一段，它从头给你一条流。
        # 播放器没法 seek，而 Emby 开播第一件事就是 seek。转码流的典型长相。
        return R, ("服务器不认 Range：要中间那一段，它从头给一条流 —— "
                   "播放器没法 seek，而 Emby 开播第一件事就是 seek。"
                   "转码流就是这样")
    if not rs["started"]:
        return R, "这条路根本起不了播 —— 连 2 秒的缓冲都攒不满"
    dry = [i for i, (recv, left, on) in enumerate(rs["samples"])
           if on and recv == 0]
    t = med(rs["ttfb"])
    # 【"换段要等"这一条必须排在"没卡过"前面，这是上一版最坏的错】上一版把它压在
    # stalls > 0 里面，于是出现过这样一屏：首字节中位 2.06 秒明明白白印在上面，
    # 结论却是"全程没卡过"。原因是这个模拟播放器的缓冲上限有 12 秒 —— 12 秒的缓冲
    # 足够把每次 2 秒的等待盖过去，它自己不卡，可真正的播放器缓冲小得多、而且
    # 开播探测、拖进度条、每次续缓冲都要重发请求，每发一次就交一次这个钱。
    # 换句话说：这一项【量到了】而【没报出来】，比没量还坏。
    if t > 1.0:
        # 一次请求拿到约 4 秒的量，所以每 4 秒就要交一次这个等待。
        waste = t / (t + 4.0)
        return Y, (f"带宽够，但【每换一段就要等 {t:.1f} 秒】—— 光这一项就吃掉 "
                   f"{waste * 100:.0f}% 的时间。播放器缓冲比这个脚本小，"
                   f"还要为开播探测和拖进度条反复重发请求，每发一次交一次")
    if rs["stalls"] == 0:
        return G, "全程没卡过 —— 这条路供得上这部片"
    if rs["avg"] < need / 1e6 * 0.8:
        return R, (f"从头到尾就没跟上：均速 {rs['avg']:.1f} < 需要的 "
                   f"{need / 1e6:.1f} Mbps，纯粹是带宽不够")
    if len(dry) >= 3:
        # 【要看的是"断流这件事"多久来一次，不是每两个空秒差几】一次断流常常连着好
        # 几秒都没数据，raw 的相邻差里于是塞满了 1，周期被这些 1 淹掉。所以先把连
        # 着的空秒并成一段，再看【段与段之间】隔多久 —— 那个数才是"每隔几秒卡一下"
        # 里的"几秒"。
        starts = [i for n, i in enumerate(dry) if n == 0 or dry[n - 1] != i - 1]
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        if len(gaps) >= 2 and max(gaps) - min(gaps) <= 1:
            return Y, (f"周期性断流：每 {sum(gaps) // len(gaps)} 秒断一次，"
                       f"每次断 {len(dry) // len(starts)} 秒左右 —— "
                       f"源在按令牌桶限速，均速够也没用")
        return Y, f"断断续续：{SECS} 秒里有 {len(dry)} 秒完全没有数据进来"
    return Y, f"卡了 {rs['stalls']} 秒，均速勉强够 —— 高码率段落顶不住"


# ================= ②b 这台机器连得上那个主机吗 =================
def conn_probe(host, port):
    """A 记录和 AAAA 记录各去建一次 TCP，分别计时。

    【为什么这一步值得单独做】④ 能量出"每发一次请求就交一次固定开销"，但说不出
    【贵在哪一层】。而有一种成因既常见又完全看不见：这台机器的 IPv6 路由半通不通。
    那样的话每开一条新连接都会【先去试那个到不了的 IPv6 地址】，等超时或等
    unreachable，再退回 IPv4 —— 正好是一笔跟位置无关、跟大小无关、只跟"开了
    几次口"有关的钱，和 ④ 量出来的形状严丝合缝。

    现场的两个主机名把这个嫌疑摆到了台面上：
        video-play-p-zb.drive.quark.cn   → Errno 113 No route to host
        dl1-v6.aliyundrive.cloud         → 名字里就写着 v6
    """
    out = []
    for fam, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
        except OSError:
            out.append((label, "没有这类地址", 0.0, False))
            continue
        if not infos:
            out.append((label, "没有这类地址", 0.0, False))
            continue
        addr = infos[0][4]
        sk = socket.socket(fam, socket.SOCK_STREAM)
        sk.settimeout(8)
        t0 = time.time()
        try:
            sk.connect(addr)
            out.append((label, "通", time.time() - t0, True))
        except Exception as e:
            out.append((label, str(e)[:34], time.time() - t0, False))
        finally:
            try:
                sk.close()
            except OSError:
                pass
    return out


sec("②b 这台机器连得上那些主机吗（IPv4 / IPv6 各试一次）")
print(f"  {D}④ 只能说「每发一次请求交一次钱」，说不出贵在哪一层。而最常见又最"
      f"看不见的一层是：IPv6 路由半通不通 —— 每开一条新连接都先去试那个到不了的"
      f"地址，等超时，再退回 IPv4。{X}")
_hosts = []
for _lb, _u, _h in routes:
    m = re.match(r"^[a-z]+://([^/:]+)(?::(\d+))?", _u)
    if not m:
        continue
    hn, pt = m.group(1), int(m.group(2) or (443 if _u.startswith("https") else 80))
    if hn in ("127.0.0.1", "localhost"):
        continue
    if (hn, pt) not in _hosts:
        _hosts.append((hn, pt))
_v6bad = False
CONN = {}          # 主机 → 最快的那次 TCP 建连耗时。④ 要拿它跟首字节比
if not _hosts:
    print(f"  {D}这条路只连本机（127.0.0.1），没有外部主机要测。{X}")
    print(f"  {D}但 OpenList 自己要去连上游 —— 那一跳这里量不到，"
          f"④ 里那笔固定开销有可能就出在它身上。{X}")
for hn, pt in _hosts:
    print()
    print(f"  {C}{safe(hn)}{X}  {D}:{pt}{X}")
    rows_c = conn_probe(hn, pt)
    for lb, msg, el, okc in rows_c:
        col = G if okc else (D if msg == "没有这类地址" else R)
        print(f"    {lb}　{col}{msg}{X}　{el:.2f} 秒")
    v4 = next((r for r in rows_c if r[0] == "IPv4"), None)
    v6 = next((r for r in rows_c if r[0] == "IPv6"), None)
    _oks = [r[2] for r in rows_c if r[3]]
    if _oks:
        CONN[hn] = min(_oks)
    if v6 and v6[1] != "没有这类地址" and not v6[3] and v4 and v4[3]:
        _v6bad = True
        print(f"    {R}✖ 有 IPv6 地址、但连不上；IPv4 是通的{X}")
        print(f"    {D}每开一条新连接都要先在这个到不了的 IPv6 上耗掉 "
              f"{v6[2]:.1f} 秒，才退回 IPv4 —— 这正是 ④ 量到的那笔固定开销。{X}")
    elif v6 and v6[3] and v4 and v4[3] and v6[2] > v4[2] * 3 + 0.5:
        _v6bad = True
        print(f"    {Y}⚠ IPv6 通，但比 IPv4 慢得多（{v6[2]:.1f} vs {v4[2]:.1f} 秒）{X}")
if _v6bad:
    print()
    print(f"  {B}这台机器的 IPv6 出口有问题 —— 而系统默认【优先走 IPv6】{X}")
    print(f"  {D}先验一句（不改任何东西）：{X}")
    # 【\\n 要留在命令里，不能变成真换行】这一行是给人照抄进终端的，
    # 打印时若把它当成换行，粘出去就断成两行、当场跑不了。
    print(f"      curl -4 -o /dev/null -s -w '%{{time_connect}}\\n' "
          f"https://{safe(_hosts[0][0])}/")
    print(f"      curl -6 -o /dev/null -s -w '%{{time_connect}}\\n' "
          f"https://{safe(_hosts[0][0])}/")
    print(f"  {D}两个数差一个量级就坐实了。真要动的话是让系统优先 IPv4"
          f"（/etc/gai.conf 里的 precedence ::ffff:0:0/96），"
          f"那是全机器的事 —— 先把上面两个数发出来再决定，别急着改。{X}")


sec(f"③ 当一回播放器，每条路各拉 {SECS} 秒")
print(f"  {D}从文件 10% 处开始拉（开头那一段常常被缓存过，测出来偏好看）。"
      f"缓冲上限 12 秒，填满就歇手 —— 播放器就是这么干的。{X}")
done = []
for label, u, hdr in routes:
    done.append(play(u, label, need_bps, SECS, hdr))
    if len(done) < len(routes):
        print(f"  {D}歇 5 秒再测下一条，免得撞上源的频率限制（那个 429 是自己造的）{X}")
        time.sleep(5)

# ================= ④ 换一段要多少钱 =================
def ttfb_probe(url, start, want, hdr):
    """只量【从发出请求到第一个字节回来】用了多久，拿到就撒手。

    整个测试里最便宜也最说明问题的一项：一次几十 KB，六次加起来不到 1 MB。
    """
    req = urllib.request.Request(
        url, headers=dict(hdr, Range=f"bytes={start}-{start + want - 1}"))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as rr:
            el = time.time() - t0
            rr.read(1 << 16)
            return rr.status, el
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0
    except Exception as e:
        return str(e)[:26], time.time() - t0


sec("④ 换一段要多少钱")
print(f"  {D}上面那条时间轴是【一个缓冲 12 秒的播放器】看到的样子，它能把每次换段的"
      f"等待盖过去。可真播放器缓冲小得多，而且开播探测、拖进度条、每次续缓冲都要"
      f"重发请求 —— 每发一次就交一次这个钱。这一段就是去量这笔钱有多大、以及它"
      f"【跟什么有关】。{X}")
print(f"  {D}只要首字节就撒手，六次加起来不到 1 MB。{X}")
SIZES = (("512 KiB", 512 << 10), ("8 MiB", 8 << 20), ("32 MiB", 32 << 20))
for label, u, hdr in routes:
    print()
    print(f"  {B}{label}{X}")
    base = ol_size or (1 << 30)
    print(f"    {D}同样要 8 MiB，从三个位置各要一次：{X}")
    bypos = []
    for pct in (10, 50, 90):
        st, el = ttfb_probe(u, int(base * pct / 100), 8 << 20, hdr)
        bypos.append((pct, st, el))
        col = G if st in (200, 206) else R
        print(f"      {pct:>3}%　{col}{st}{X}　{el:.2f} 秒")
        time.sleep(2)
    print(f"    {D}同样从 50%，要三种大小：{X}")
    bysize = []
    for nm, n in SIZES:
        st, el = ttfb_probe(u, int(base * 0.5), n, hdr)
        bysize.append((nm, st, el))
        col = G if st in (200, 206) else R
        print(f"      {nm:>8}　{col}{st}{X}　{el:.2f} 秒")
        time.sleep(2)

    okp = [e for _p, s, e in bypos if s in (200, 206)]
    oks = [e for _n, s, e in bysize if s in (200, 206)]
    codes_seen = {s for _a, s, _e in bypos} | {s for _a, s, _e in bysize}
    if 416 in codes_seen:
        # 【416 不是"被拒"，是"你要的位置它没有"】偏移是按网盘报的文件大小算的，
        # 而拿到的这条链【比那个文件短】—— 十有八九它根本不是原文件（转码流的
        # 长度和原文件对不上）。说成"被限流"会把人带去查一个不存在的限流。
        print(f"    {Y}→ 回了 416（要的位置超出了它的长度）{X}")
        print(f"    {D}偏移是按网盘报的 {mb(ol_size)} 算的，而这条链比那短 ——"
              f"说明它给的【不是原文件】。转码流就是这样。{X}")
    if len(okp) < 2 or len(oks) < 2:
        if 403 in codes_seen:
            # 【带了登录凭据还是 0.00 秒就回 403 —— 那是策略拒绝，不是鉴权失败】
            # OpenList 的 /p/ 只对【开了「本机代理」的存储】开放。所以这条路是
            # 【没测成】，不是"不行"。上一版把它算成不行，等于凭空给夸克和阿里
            # 各判了一条死路 —— 而那条路压根没被试过。
            print(f"    {Y}→ 403，而且是 0 秒就回的 —— 这不是慢，是 OpenList "
                  f"【不让代理这个盘】{X}")
            print(f"    {D}/p/ 只对开了「本机代理」的存储开放。这条路【没测成】，"
                  f"不是它不行 —— 要真试，先去 4 挂载路径 → 这个盘 → 打开本机代理。{X}")
        else:
            print(f"    {R}→ 请求被拒了好几次，这一段没测成{X}"
                  f"  {D}（拒的那几个码就在上面，429/500 = 源在限流或掐连接）{X}")
        continue
    cost = med(okp + oks)
    if cost < 0.5:
        print(f"    {G}→ 换段几乎不要钱（{cost:.2f} 秒）—— 卡不在这里{X}")
        continue
    # 【三种"贵法"，处置完全不同】
    #   跟位置有关 → 上游不支持真 Range，OpenList 只能从头空转到那个位置
    #   跟大小有关 → 上游要把整段准备好才开始发（打包/解密/转存那一类）
    #   都无关     → 每发一次请求就交一次的固定开销（建连 + TLS + 上游找文件）
    grow_pos = okp[-1] > okp[0] * 2 + 0.5
    grow_size = oks[-1] > oks[0] * 2 + 0.5
    if grow_pos:
        print(f"    {R}→ 位置越靠后等得越久（{okp[0]:.2f} → {okp[-1]:.2f} 秒）{X}")
        print(f"    {D}上游【不支持真正的 Range】：要中间那一段，它得从头把前面的"
              f"字节空转掉。表现就是越往后拖越久，拖到片尾直接超时。{X}")
        print(f"    {D}这个改不掉 —— 是上游那台服务器的事。要拖进度条的片别放这个源。{X}")
    elif grow_size:
        print(f"    {Y}→ 要得越多等得越久（{oks[0]:.2f} → {oks[-1]:.2f} 秒）{X}")
        print(f"    {D}上游要把整段准备好才开始发。那就【要小段】—— 但请求数会变多，"
              f"又会撞上限流，两头为难。{X}")
    else:
        print(f"    {Y}→ 跟位置和大小都无关：这 {cost:.1f} 秒是【每发一次请求就交一次】"
              f"的固定开销{X}")
        # 【这笔钱花在哪一层，决定往哪儿使劲】②b 已经量过 TCP 握手要多久。
        # 握手快而首字节慢，说明钱不在网络上，在上游那台服务器准备数据上 ——
        # 本机怎么调路由、改 MTU、关 IPv6 都碰不到它，只能【少开口】。
        # 反过来握手就很慢，那才是这台机器的网络配置问题。
        _hn = re.sub(r"^[a-z]+://([^/:]+).*", r"\1", u)
        _ct = CONN.get(_hn)
        if _ct is not None:
            if cost > _ct * 5 + 0.3:
                print(f"    {B}钱不在建连上{X}  {D}TCP 握手只要 {_ct:.2f} 秒，"
                      f"第一个字节却要 {cost:.1f} 秒 —— 差 {cost / max(_ct, 0.01):.0f} 倍。"
                      f"慢的是【上游那台服务器准备数据】，不是这条网络。{X}")
                print(f"    {D}所以改路由、关 IPv6、调 MTU 都碰不到它。"
                      f"能动的只有一件事：少开口（每次多要一点）。{X}")
            else:
                print(f"    {R}钱就在建连上{X}  {D}TCP 握手 {_ct:.2f} 秒，"
                      f"首字节 {cost:.1f} 秒 —— 两个数差不多，说明光是"
                      f"把连接建起来就这么久。这是这台机器到那边的网络问题。{X}")
        print(f"    {D}算法很直白：{X}")
        chunk_s = 4.0
        print(f"    {D}  播放器每要一段（约 {chunk_s:.0f} 秒的量）就停 {cost:.1f} 秒 → "
              f"实际只有 {chunk_s / (chunk_s + cost) * 100:.0f}% 的时间在传数据{X}")
        print(f"    {D}  要是播放器一次只要 1 秒的量（很多客户端就是这样，"
              f"Emby 开播探测更是连发好几个小请求）→ 只剩 "
              f"{1 / (1 + cost) * 100:.0f}%，必卡{X}")
        print(f"    {B}这就是「能播、但过几秒卡一下」的来源：不是带宽不够，"
              f"是【要得太频繁】{X}")
        if "本机代理" in label or "VPS" in label:
            print(f"    {D}代理这条路上这笔钱能压：OpenList 设置 → 全局 → "
                  f"代理缓冲区大小（proxy buffer size）调大，它一次向上游多要一点、"
                  f"少要几次。调完回来再跑一遍这个脚本，看这个秒数有没有降。{X}")


# ================= ④b 换个 UA，速度会不会变 =================
# 【症状是"通了但慢一百倍"，不是"被拒"】cant-play.sh 的 ⑧ 按 UA 测过，可它量的是
# 状态码；而这里所有 UA 大概率都回 206，只看状态码一点异常都看不出来。要量速度。
UAS = (("浏览器（脚本一直用的）", UA),
       ("Emby 的 ffprobe", "Lavf/59.27.100"),
       ("Emby 安卓客户端", "Emby/1.4 (Android 13; ExoPlayerLib/2.18.1)"),
       ("VLC / Infuse 那一类", "VLC/3.0.18 LibVLC/3.0.18"),
       # 【这一张的名字要照实写】不设 User-Agent 头，urllib 会自己补一个
       # Python-urllib/3.x 上去 —— 所以它不是"没有脸"，是"另一张脸"。
       # 写成"不带 UA"会让人以为存在一种"什么都不发"的选项，那是假的。
       ("不设 UA（urllib 默认）", ""))


def ua_speed(url, start, ua, secs=6, cap=4 << 20):
    """拿这个 UA 拉一小段，量 Mbps。拿到 cap 或到点就撒手。"""
    hdr = {"Range": f"bytes={start}-{start + cap - 1}"}
    if ua:
        hdr["User-Agent"] = ua
    req = urllib.request.Request(url, headers=hdr)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as rr:
            got = 0
            while time.time() - t0 < secs and got < cap:
                b = rr.read(1 << 16)
                if not b:
                    break
                got += len(b)
            el = max(0.1, time.time() - t0)
            return rr.status, got * 8 / el / 1e6
    except urllib.error.HTTPError as e:
        return e.code, 0.0
    except Exception as e:
        return str(e)[:22], 0.0


_cdn = [(lb, u, h) for lb, u, h in routes
        if not re.match(r"^[a-z]+://(127\.|localhost|openlist)", u)]
if _cdn:
    sec("④b 同一条直链，换几个 UA 各拉一次（比速度，不只看通不通）")
    print(f"  {D}「脚本拉得动、手机拉不动」只剩两个变量：谁去拉（IP）、戴什么脸"
          f"（User-Agent）。UA 这一条当场就能测 —— 五发各拉几秒，一共 20 MB。{X}")
    _lb, _u, _h = _cdn[0]
    _st = int((ol_size or (1 << 30)) * 0.2)
    _res = []
    for who, ua_s in UAS:
        code, mbps = ua_speed(_u, _st, ua_s)
        _res.append((who, code, mbps, ua_s))
        col = G if code in (200, 206) else R
        print(f"    {who:<22}{col}{code}{X}  {C}{mbps:5.1f} Mbps{X}")
        time.sleep(2)
    _ok = [(w, m, u2) for w, c, m, u2 in _res if c in (200, 206) and m > 0]
    if len(_ok) >= 2:
        _fast, _slow = max(_ok, key=lambda x: x[1]), min(_ok, key=lambda x: x[1])
        _brow = next((x for x in _res if x[0].startswith("浏览器")), None)
        if _fast[1] > _slow[1] * 3:
            print(f"  {R}✖ 上游按 User-Agent 区别对待{X}"
                  f"  {D}最快「{_fast[0]}」{_fast[1]:.1f}，"
                  f"最慢「{_slow[0]}」{_slow[1]:.1f} —— 差 "
                  f"{_fast[1] / max(_slow[1], 0.01):.0f} 倍{X}")
            print(f"  {B}这个源上最快的那张脸是「{_fast[0]}」（{_fast[1]:.1f} Mbps）{X}")
            # 【建议必须跟着数据走，不能写死"改成浏览器"】实测撞到过浏览器 UA
            # 【最慢】（超时、0 Mbps）而 VLC 最快（16.1）的源。那种情况下
            # 「伪装成浏览器」这个开关是【反效果】—— 照着做会把能播的弄成播不了。
            # 一条反向的建议比没有建议坏得多。
            _bslow = (_brow is not None
                      and (_brow[1] not in (200, 206) or _brow[2] < _fast[1] / 3))
            if _bslow:
                print(f"  {R}⚠ 注意方向：浏览器那张脸在这个源上【最差】"
                      f"（{_brow[2]:.1f} Mbps）{X}")
                print(f"  {B}所以这个盘【不要】开「伪装成浏览器」—— 开了等于"
                      f"从 {_fast[1]:.1f} 掉到 {_brow[2]:.1f}{X}")
                print(f"  {D}那个开关是给【按 UA 挡 ffmpeg】的源用的（不开就 403）。"
                      f"这个源不挡，它只是对不同的脸给不同的速度，方向正好相反。{X}")
            else:
                print(f"  {D}能动的地方：这个盘的「伪装成浏览器」开关"
                      f"（4 挂载路径 → 这个盘），它会把出去的 UA 换成浏览器那个。{X}")
            # 【前面那些数字是戴哪张脸量的，必须说】不说的话，人会拿一个系统性
            # 偏低的数去做决定 —— 而这正是这次的实际情况：③④ 全程用的是浏览器 UA。
            if _brow is not None and _brow[2] < _fast[1] / 3:
                print(f"  {Y}⚠ 上面 ③④ 的速度和换段费，全是戴着浏览器那张脸量的 ——"
                      f"也就是这个源上最慢的一张。那些数字偏低。{X}")
        else:
            print(f"  {G}✔ 几个 UA 速度差不多{X}"
                  f"  {D}（{_slow[1]:.1f} ～ {_fast[1]:.1f} Mbps）{X}")
            _fast = None
            print(f"  {B}那「脚本拉得动、手机拉不动」就只剩【谁去拉】这一个变量了{X}")
            print(f"  {D}这条直链是 MediaWarp 在【你的 VPS 上】换来的。很多网盘的"
                  f"下载链是认来源的：换链的那个 IP 拉得飞快，别的 IP 拿去就被限速。"
                  f"手机是另一个 IP —— 这正好解释了为什么一改本机代理就流畅："
                  f"那等于让换链的人和用链的人变成同一台机器。{X}")
            print(f"  {D}这个脚本只有 VPS 这一个出口，验证不了 IP 那一头。"
                  f"要自己验：手机浏览器打开挂载页面直接下同一个文件，"
                  f"快 = 不认 IP，慢 = 认。{X}")
    # 【"换张脸能不能救"只有真拉一遍才算答上】上面那五发各拉几秒，量的是瞬时速度；
    # 而"卡不卡"要看一条完整的时间轴 —— 缓冲撑不撑得住、换段费降不降。
    # 【别因为最快的那张脸是"不设 UA"就悄悄不测】上一版的条件里带着 _fast[2]，
    # 而"不设 UA"这一张的 UA 串就是空的 —— 只要它跑赢（实测夸克上它是第二快的
    # 10.3 Mbps，跑赢完全可能），整个复测就被静默跳过：屏上前面刚说完"最快的
    # 那张脸是它"，后面却没有任何复测，人拿不到"换了到底还卡不卡"这个答案。
    # 空 UA 不是"没有选项"，它就是一个选项 —— 照 ua_speed 的做法【把这个头去掉】，
    # 而不是把它设成空串（设成空串发出去的是 `User-Agent:`，跟量的时候不是一回事）。
    if len(_ok) >= 2 and _fast is not None and _fast[1] > _slow[1] * 3:
        _h2 = {k: v for k, v in _h.items() if k.lower() != "user-agent"}
        if _fast[2]:
            _h2["User-Agent"] = _fast[2]
        print()
        print(f"  {B}换成最快的那张脸，再当一回播放器 ——{X}")
        _rs2 = play(_u, f"同一条路，戴「{_fast[0]}」那张脸", need_bps,
                    min(SECS, 30), _h2)
        _rs2["label"] = f"同一条路，戴「{_fast[0]}」那张脸"
        done.append(_rs2)
        _base = next((r for r in done if r is not _rs2), None)
        if _base:
            print(f"  {D}对照：戴浏览器那张脸是 {_base['avg']:.1f} Mbps、"
                  f"卡了 {_base['stalls']} 秒；换这张脸是 {_rs2['avg']:.1f} Mbps、"
                  f"卡了 {_rs2['stalls']} 秒{X}")
            if _rs2["stalls"] == 0 and _base["stalls"] > 0:
                print(f"  {G}✔ 换张脸就不卡了 —— 这个源的问题就是认脸{X}")
            elif _rs2["avg"] > _base["avg"] * 1.5:
                print(f"  {G}✔ 快了 {_rs2['avg'] / max(_base['avg'], 0.01):.1f} 倍{X}")

    print(f"  {D}顺带：以前「转码流 + 302」能跑满，是因为转码流码率低一个量级 ——"
          f"就算被限速也够播。那不代表这条链没被限，只代表限了也看不出来。{X}")


# ================= ⑤ 结论 =================
sec("⑤ 结论")
for rs in done:
    col, why = verdict(rs, need_bps)
    rs["col"] = col
    print(f"  {col}{'✔' if col == G else '✖' if col == R else '⚠'}{X} "
          f"{B}{rs['label']}{X}")
    print(f"    {col}{why}{X}")

# 【"这条路行不行"要跟上面那句结论一致】原来这里另算一遍（只看 stalls），于是
# 出现过结论说"每换一段要等 2 秒"、下面却接着说"几条路都供得上"，自己打自己。
# 判定只留一份，就是 verdict() 给的那个颜色。
ok = [r for r in done if r["col"] == G]
bad = [r for r in done if r["col"] not in (G, C)]
untested = [r for r in done if r["col"] == C]
# 【这一条要摆在所有分支前面，盖过底下任何一句"供得上"】在这台机器上拉得动，
# 不等于手机拉得动。早期版本把它塞进"几条路都供得上"那一支里 —— 而 302 指向
# 内网时那条路根本拉不动，于是压根走不到那一支，这句最要紧的话就再也不出现了。
if mw_inner:
    print()
    print(f"  {R}✖ MediaWarp 给客户端的是内网地址（见 ②c）{X}")
    print(f"  {D}上面这些都是在这台机器上拉的 —— 手机拿到 {safe(mw_host)} "
          f"这个地址，连解析都解析不了。取流这一段量得再好也没用。{X}")
    print(f"  {B}这一条先修：media-stack → 7 更新{X}")
print()
if not ok and not bad:
    # 【一条都没测成的时候别往下判】下面那几支都是在比"哪条行哪条不行"，
    # 而这时候一条都没试过。早期版本会直接掉进最后那支，打出"几条路都不行" ——
    # 一句凭空的死刑。
    print(f"  {Y}几条路都没测成 —— 上面每条都写了为什么{X}")
elif len(done) > 1 and ok and bad:
    print(f"  {B}两条路结果不一样 —— 这就是可以直接动手的地方{X}")
    if "本机代理" in ok[0]["label"]:
        print(f"  {G}走你的 VPS 稳，走网盘 CDN 卡{X}")
        print(f"  {B}改法：media-stack → 4 挂载路径 → 选这个盘 → 本机代理{X}")
        print(f"  {D}代价是视频全程过你的 VPS，吃出口流量；换来的是不卡。{X}")
    else:
        print(f"  {G}走网盘 CDN 稳，走你的 VPS 卡{X}")
        print(f"  {D}说明瓶颈在 VPS 到网盘这一段，别开本机代理。{X}")
elif ok and not bad:
    print(f"  {G}测成的几条都供得上{X}" if untested else f"  {G}几条路都供得上{X}")
    if untested:
        print(f"  {D}（另有 {len(untested)} 条没测成，见上面）{X}")
    print(f"  {D}那卡的原因【不在取流这一段】。剩下的可能性按概率排：{X}")
    print(f"  {D}  1. Emby 在转码 —— 播的时候跑 bash playing.sh，"
          f"看到 Transcode 就是它（转码要先把片子拉下来再转，2 核机器必卡）{X}")
    print(f"  {D}  2. 客户端到你 VPS 这一段不稳 —— 这个脚本量的是 VPS 到网盘，"
          f"量不到你家到 VPS{X}")
    print(f"  {D}  3. 后台任务在抢 —— 上面 ① 那里报了就是{X}")
else:
    print(f"  {R}几条路都不行{X}")
    print(f"  {D}上面每条路的那一句已经分了类；同一类的处置：{X}")
    print(f"  {D}  · 带宽不够 → 这个源只适合放码率低的片，大码率的换个盘放{X}")
    print(f"  {D}  · 周期性断流 → 源在限速，改配置改不掉；错开时间、或换盘{X}")
    print(f"  {D}  · 每换一段要等 → 看上面 ④，它已经把「贵在哪」分好类了{X}")

# ================= ⑥ 真实播放留下的痕迹 =================
sec("⑥ 这部片最近【真被人播】的时候，日志里发生了什么")
print(f"  {D}上面测的都是脚本自己造的请求。这一段看的是真实播放 —— "
      f"客户端可能走了完全不同的路（转码、或者压根没走 MediaWarp）。{X}")
print(f"  {D}要抓现行：在手机上点开让它转着，然后立刻跑这个脚本。{X}")

# ---- 现在有没有人在播：Emby 自己的答案，比任何推断都硬 ----
# 【DirectPlay / DirectStream / Transcode 的区别就是视频走不走这台机器】
#   DirectPlay   客户端直接吃网盘那个文件 —— 302 是真生效的
#   DirectStream 只换容器，视频流仍然【经过本机】转手
#   Transcode    整条视频在本机重编码：先从跨境网盘拉下来、转完再发给客户端。
#                302 等于白设，而 2 核的机器边拉边转供不上，表现正是
#                「转一会儿像连不上一样就断开」
def _fps(ms):
    """这条视频流一秒多少帧。Emby 两个字段都可能缺，也可能写成 "24000/1001"。"""
    for k in ("AverageFrameRate", "RealFrameRate"):
        v = ms.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        if isinstance(v, str) and "/" in v:
            try:
                a, b = v.split("/", 1)
                if float(b):
                    return float(a) / float(b)
            except ValueError:
                pass
    return 0.0


def _ffmpeg_cpu():
    """此刻 ffmpeg 一共吃掉多少 CPU（百分比，100 = 一个核吃满）。

    【在宿主机上 ps 就看得到容器里的 ffmpeg】Emby 跑在 docker 里，但进程表是宿主
    机的，不用 docker exec —— 少一层依赖，Emby 容器名换了也照样能量。
    """
    try:
        out = subprocess.run(["ps", "-eo", "pcpu,args"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception:
        return None, 0
    tot, n = 0.0, 0
    for ln in out.splitlines()[1:]:
        ln = ln.strip()
        if "ffmpeg" not in ln or "ps -eo" in ln:
            continue
        head = ln.split(None, 1)
        try:
            tot += float(head[0])
        except (ValueError, IndexError):
            continue
        n += 1
    return (tot, n) if n else (None, 0)


try:
    _sess = emby("/Sessions") or []
except Exception:
    _sess = []
_live = [x for x in _sess if x.get("NowPlayingItem")]
if not _live:
    print(f"  {Y}此刻没人在播 —— 这一段是整个脚本里唯一能回答"
          f"「点了播放却不出画面」的地方，空着等于没测。{X}")
    print(f"  {D}抓现行：在播放器里点开那部片，【让它转着别退】，"
          f"然后回到这里再跑一次这个脚本。{X}")
else:
    for _s in _live:
        _item = _s.get("NowPlayingItem") or {}
        _n = _item.get("Name") or "?"
        _m = str((_s.get("PlayState") or {}).get("PlayMethod") or "")
        _tr = _s.get("TranscodingInfo") or {}
        print()
        print(f"  {B}正在播：{_n}{X}  {D}{_s.get('Client') or '?'}"
              f"（{_s.get('DeviceName') or '?'}）{X}")

        # ---- 先把"画面到底出没出来"落成事实，别靠截图 ----
        # 【这是"能不能播"和"播得顺不顺"的分界线】进度还在 0，后面所有关于
        # 带宽、卡顿的话都不适用 —— 那些说的是"播着播着"，而这里根本没播起来。
        _pos = (_s.get("PlayState") or {}).get("PositionTicks") or 0
        try:
            _pos = int(_pos) / 1e7
        except (TypeError, ValueError):
            _pos = 0.0
        _stuck = _pos < 1.0
        if _stuck:
            print(f"  {R}进度还在 0:00 —— 画面没出来{X}")
        else:
            print(f"  {D}进度 {int(_pos) // 60}:{int(_pos) % 60:02d}"
                  f"（画面已经出来了，下面说的是顺不顺）{X}")

        if _m == "DirectPlay":
            print(f"  {G}✔ 直接播放{X}  {D}视频没经过这台机器，302 是真生效的{X}")
            if _stuck:
                # 【直接播放 + 不出画面 = 客户端在从头啃整个文件】
                # MP4 的索引（moov）在文件尾，而服务端不认 Range 的时候，播放器
                # 只能从头拉到尾才拿得到它。表现正是"流量巨大、一帧都没有"。
                print(f"  {Y}但画面没出来，而视频压根没过这台机器 —— "
                      f"那卡在客户端和网盘之间，这个脚本量不到那一段。{X}")
                print(f"  {D}最常见的一种：文件的索引（MP4 的 moov）在文件尾，"
                      f"而那一头不认 Range，播放器只能从头啃到尾才拿得到索引 —— "
                      f"流量哗哗跑、一帧画面都没有。上面 ⑤ 里如果有哪条路写了"
                      f"「服务器不认 Range」，就是它。{X}")
        elif _m == "DirectStream":
            print(f"  {Y}⚠ 直接流{X}  {D}只换了容器，视频流还是经过这台机器转手{X}")
        elif _m == "Transcode":
            print(f"  {R}✖ 在转码{X}")
            print(f"  {D}这台机器要先把片子从网盘拉下来、转完再发给客户端 —— "
                  f"302 等于白设。{X}")

            # ---- 【这一段是这一节的重点】转码是"边拉边转"：
            # 拉是满速的，转出来的帧却按 CPU 的速度走。所以完全可能
            # 流量跑到 30 M/s、而进度条一直挂在 0:00 —— 而且直链修得越好，
            # 流量数字越漂亮，越容易被当成网络问题去查。
            # 判据只有一个数：转码帧率 ÷ 源帧率。
            _src = 0.0
            for _ms in (_item.get("MediaStreams") or []):
                if str(_ms.get("Type") or "").lower() == "video":
                    _src = _fps(_ms)
                    break
            if not _src and _item.get("Id"):
                # 【NowPlayingItem 里不一定带 MediaStreams】不同版本的 Emby 给的
                # 字段不一样。少了它，这一节最要紧的那个数（倍速）就算不出来 ——
                # 而算不出来的时候屏上只有一句"帧率 Emby 没报"，人什么也没拿到。
                # 多问一次条目就有了，代价是一次本机请求。
                try:
                    _full = emby(f"/Items?Ids={_item['Id']}&Fields=MediaStreams",
                                 timeout=30) or {}
                    for _it in (_full.get("Items") or []):
                        for _ms in (_it.get("MediaStreams") or []):
                            if str(_ms.get("Type") or "").lower() == "video":
                                _src = _fps(_ms)
                                break
                except Exception:
                    pass
            try:
                _out = float(_tr.get("Framerate") or 0)
            except (TypeError, ValueError):
                _out = 0.0
            if _out > 0 and _src > 0:
                _rt = _out / _src
                _c = G if _rt >= 1.3 else (Y if _rt >= 1.0 else R)
                print(f"  {_c}转码速度 {_rt:.2f} 倍实时{X}  "
                      f"{D}（转出 {_out:.1f} fps，源片 {_src:.1f} fps）{X}")
                if _rt < 1.0:
                    _wait = 1 / _rt if _rt else 0
                    print(f"  {R}→ 转得比播得慢：客户端每等 {_wait:.1f} 秒"
                          f"才拿到 1 秒的画面。{X}")
                    print(f"  {D}这就是「流量很大、画面半天不出来」的机制 —— "
                          f"拉是满速的，转不动而已。跟网盘、跟直链、"
                          f"跟带宽都没有关系，往那边查是白查。{X}")
                elif _rt < 1.3:
                    print(f"  {Y}→ 只比实时快一点点：能起播，但一遇到打斗、"
                          f"字幕多的段落就会追不上。{X}")
            elif _out > 0:
                print(f"  {D}转出 {_out:.1f} fps（源片帧率 Emby 没报，"
                      f"比不出倍速）{X}")

            _pct = _tr.get("CompletionPercentage")
            if isinstance(_pct, (int, float)):
                print(f"  {D}已经转了 {_pct:.1f}%{X}")

            # 硬解/硬编是决定倍速的大头，报出来才知道还有没有救
            _dec = _tr.get("VideoDecoder") or ""
            _enc = _tr.get("VideoEncoder") or ""
            _hw = bool(_tr.get("VideoDecoderIsHardware")) or \
                bool(_tr.get("VideoEncoderIsHardware"))
            if _dec or _enc:
                print(f"  {D}解码 {_dec or '?'} → 编码 {_enc or '?'}"
                      f"（{'硬件加速' if _hw else '纯 CPU'}）{X}")

            # 【"转不动"要分两种，处置完全不同】
            #   CPU 吃满了  → 机器就这个水平，只能别让它转（换播放器 / 换片源）
            #   CPU 没吃满  → 瓶颈不在 CPU，是 ffmpeg 在等输入（源拉不动）
            _cpu, _nproc = _ffmpeg_cpu()
            _cores = os.cpu_count() or 1
            if _cpu is not None:
                _sat = _cpu / (_cores * 100.0)
                print(f"  {D}ffmpeg 此刻吃 {_cpu:.0f}% CPU，这台机器 {_cores} 核"
                      f"（满载算 {_cores * 100}%）{X}")
                if _sat >= 0.7:
                    print(f"  {D}→ CPU 基本吃满了：机器就这个水平，"
                          f"调参数救不回来。{X}")
                else:
                    print(f"  {Y}→ CPU 还有富余，却转不快 —— "
                          f"那 ffmpeg 多半在等输入，瓶颈在取流那一段"
                          f"（看上面 ③ ④）。{X}")
            elif _nproc == 0:
                print(f"  {Y}⚠ Emby 说在转码，进程表里却找不到 ffmpeg —— "
                      f"要么刚起还没 fork，要么它转码用的是别的名字。{X}")

            _why = _tr.get("TranscodeReasons") or []
            if isinstance(_why, str):
                _why = [_why]
            if _why:
                print(f"  {B}Emby 说转码的原因：{'、'.join(_why)}{X}")
            print(f"  {D}这套东西【不该转码】：文件在网盘上，本机手里只有一条 URL。"
                  f"治法在客户端那头 —— 换个能直解的播放器"
                  f"（Infuse / VidHub / Kodi / 电视盒子），或者换个编码普通的片源。"
                  f"浏览器里的网页播放器是最容易触发转码的一种，"
                  f"用它来判断「能不能播」会冤枉整条链路。{X}")
        else:
            print(f"  {Y}⚠ 播放方式：{_m or '(Emby 没报)'}{X}")
print()
try:
    log = open(MWLOG, encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    log = []
# 【颜色码必须先剥掉】MediaWarp 用的是 gin，容器开了 tty 时状态码外面裹着一层
# ANSI（\x1b[97;42m 200 \x1b[0m），"| 200 |" 这个形状就对不上了。实测栽过一次：
# 找到 95 条这个条目的请求，状态码一个都没数出来，屏上只剩一句"最近 95 条请求："
# 后面空空如也 —— 而"那 95 条里有多少是 401/500"恰恰是这一段唯一要回答的问题。
ANSI = re.compile(r"\033\[[0-9;]*m")
log = [ANSI.sub("", ln) for ln in log]

# 【必须按"这次启动"切开】docker logs 不会因为重启就清掉旧的。混着数的后果是拿
# 重启之前的旧账去解释现在的故障 —— media-stack.py 的 mediawarp_token_broken()
# 里早写着这条教训（「修好了还报故障，用户再重启一次，还是报」），这里又踩一遍：
# 屏上那条原因是 09-17 的，而 MediaWarp 09-19 才重启过。
TS_RE = re.compile(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)")


def _ts(ln):
    m = TS_RE.search(ln)
    return f"{m.group(1)} {m.group(2)}" if m else ""


_start = ""
if MWSTART:
    m = TS_RE.search(MWSTART)
    if m:
        _start = f"{m.group(1)} {m.group(2)}"


def _after_start(ln):
    """这一行是不是这次启动【之后】的。没有时间戳的一律算进来（宁可多算）。"""
    if not _start:
        return True
    t = _ts(ln)
    return (not t) or t >= _start


log_all = log
log = [ln for ln in log_all if _after_start(ln)]
mine = [ln for ln in log if f"/videos/{iid}/" in ln.lower()]
mine_old = [ln for ln in log_all
            if f"/videos/{iid}/" in ln.lower() and not _after_start(ln)]
if not mine:
    print(f"  {Y}MediaWarp 日志里没有这个条目的播放记录{X}")
    print(f"  {D}要么最近没人点过它，要么客户端根本没走 MediaWarp"
          f"（转码就是这样 —— Emby 自己去拉，不经过 302）。{X}")
else:
    # 两种写法都认：gin 那行的 "| 200 |"，和有些版本写成 " 200 " 紧跟时间的。
    codes = {}
    for ln in mine:
        m = (re.search(r"\|\s*(\d{3})\s*\|", ln)
             or re.search(r"(?:^|\s)(\d{3})\s*\|", ln)
             or re.search(r"\|\s*(\d{3})(?:\s|$)", ln))
        if m:
            codes[m.group(1)] = codes.get(m.group(1), 0) + 1
    # 【只数状态码丢掉了最要紧的那一维】走 302 和走转码，状态码可以一模一样，
    # 而路径完全不同。上一版 95 条请求摆在屏上，却说不出其中有几条是转码。
    KINDS = (("转码（Emby 自己在拉，302 白设）",
              re.compile(r"master\.m3u8|main\.m3u8|/hls\d*/|\.ts(\?|$|\s)", re.I)),
             ("直接播放（走 302）", re.compile(r"/stream|/original", re.I)),
             ("开播探测", re.compile(r"playbackinfo", re.I)))
    kinds = {}
    for ln in mine:
        for name, rx in KINDS:
            if rx.search(ln):
                kinds[name] = kinds.get(name, 0) + 1
                break
    if kinds:
        print(f"  {D}这些请求要的是什么：{X}")
        for name, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
            col = R if name.startswith("转码") else G if name.startswith("直接") else D
            print(f"    {col}{name}{X}  {D}{n} 条{X}")
        if kinds.get("转码（Emby 自己在拉，302 白设）"):
            print(f"  {R}✖ 客户端走的是【转码】那条路{X}")
            print(f"  {D}那就跟取流快慢没关系了：Emby 要自己把片子从跨境网盘拉下来、"
                  f"边拉边转再喂给客户端。供不上就断 —— 正是「转一会儿像连不上一样"
                  f"就断开」。{X}")
            print(f"  {D}而这套东西【不该转码】：文件在网盘上，本机手里只有一条 URL。{X}")
            print(f"  {B}治法在客户端那头{X}{D}：换个能直解的播放器"
                  f"（Infuse / VidHub / Kodi / 电视盒子），或者换个编码普通的片源。"
                  f"服务器这头怎么调都没用。{X}")
            print(f"  {D}想知道 Emby 为什么要转：点开让它转着，再跑一次这个脚本，"
                  f"上面 PlayMethod 那一段会打出 Emby 自己给的原因。{X}")
    if not codes:
        # 【数不出来就要把原样摆出来，不能只报个总数】
        print(f"  {Y}找到 {len(mine)} 条这个条目的请求，但这份日志的格式认不出状态码{X}")
        print(f"  {D}原样一条（已打码）：{safe(mine[-1])[:150]}{X}")
        print(f"  {D}把这一行发给维护者，下一版就能认了。{X}")
    else:
        print(f"  {D}最近 {len(mine)} 条请求：{X}"
              + "　".join(f"{G if c.startswith('2') or c.startswith('3') else R}"
                          f"{c}×{n}{X}" for c, n in sorted(codes.items())))
    if codes.get("401"):
        print(f"  {R}✖ 有 {codes['401']} 条 401{X}")
        print(f"  {D}401 打在 /emby/Videos/<id>/ 上，几乎只有一个来路：这个盘设的是"
              f"【转码流】。转码流给的是 m3u8，而 m3u8 里的分片写的是相对路径，"
              f"播放器把它拼到 /emby/Videos/<id>/ 上，于是分片请求全打回 Emby → 401"
              f" → 一直转圈。{X}")
        print(f"  {B}改法：4 挂载路径 → 选这个盘 → 3 直链方式 → 原画直链{X}")
    if codes.get("404"):
        _n404 = codes["404"]
        _tot = sum(codes.values())
        print(f"  {R}✖ {_n404}/{_tot} 条换不到直链（404）{X}")
        # 【"现在还在不在发生"是此刻最该回答的问题】人刚把一部片重试着播通了，
        # 那 404 到底是这一刻还在发生、还是早先攒下的，决定了该不该动手。
        _t404 = [t for t in (_ts(ln) for ln in mine
                             if re.search(r"\|\s*404\s*\|", ln)) if t]
        if _t404:
            print(f"  {D}这些 404 的时间：{min(_t404)} ～ {max(_t404)}{X}")
        print(f"  {D}点播放 → MediaWarp 去换直链 → 换不到 → 回 404 → 播放器重试 →"
              f" 又 404 → 放弃。客户端上就是「转一会儿像连不上一样就断开」。{X}")
        # 【别在原因之间替人猜】"404"只说了拒绝，没说为什么拒绝，而两个最常见的
        # 原因处置完全相反：令牌作废重启就好，上游抽风重启完照样抽。
        # 上一版屏上只有一句"敲 docker restart mediawarp" —— 那是把一个猜测写成了
        # 结论。万一原因是上游抽风，人重启一遍没好，就会觉得整套诊断不管用，
        # 而真正的原因一直印在日志里没人去读。
        WHYS = (("令牌作废（OpenList 重启过）",
                 re.compile(r"token is invalidated|响应状态码:\s*401|\bstatus(?: code)?[: ]+401\b",
                            re.I)),
                ("上游/存储这一刻抽风",
                 re.compile(r"object not found|storage not found|failed to get obj", re.I)),
                ("超时", re.compile(r"timeout|timed out|context deadline|i/o timeout", re.I)),
                ("被拒（403/429）", re.compile(r"\b(403|429)\b")))
        hits, samples = {}, []
        for ln in log:
            for name, rx in WHYS:
                if rx.search(ln):
                    hits[name] = hits.get(name, 0) + 1
                    if len(samples) < 3 and name in ("令牌作废（OpenList 重启过）",
                                                     "上游/存储这一刻抽风"):
                        samples.append(ln)
                    break
        if hits:
            print(f"  {D}日志里说的原因：{X}")
            for name, n in sorted(hits.items(), key=lambda kv: -kv[1]):
                print(f"    {Y}{name}{X}  {D}{n} 次{X}")
            # 【原因条数远少于 404 条数，必须说出来】不然人会以为这 39 条都是
            # 那一个原因。实际是日志里就没有那么多原因行 —— 滚掉了，或者
            # MediaWarp 对多数 404 压根没记原因。
            _sum = sum(hits.values())
            if _sum * 3 < _n404:
                print(f"  {Y}⚠ 只找到 {_sum} 条原因，却有 {_n404} 条 404 —— "
                      f"剩下那些日志里没记原因（滚掉了，或者本来就没记）{X}")
                print(f"  {D}所以下面这个判断是【按找到的这几条推的】，不是全部。{X}")
        for ln in samples:
            print(f"    {D}{safe(ln.strip())[:150]}{X}")
        if MWSTART:
            print(f"  {D}MediaWarp 上次启动：{MWSTART[:19].replace('T', ' ')}"
                  f"（UTC）　上面这些都是【这次启动之后】的{X}")
        if mine_old:
            print(f"  {D}（另有 {len(mine_old)} 条是重启【之前】的旧账，没算进来 ——"
                  f"docker logs 不会因为重启就清掉它们）{X}")
        if hits.get("令牌作废（OpenList 重启过）"):
            print(f"  {B}→ 令牌作废了。敲：docker restart mediawarp{X}")
            print(f"  {D}MediaWarp 只在【启动那一刻】登录一次 OpenList，OpenList 一"
                  f"重启（更新、切直链方式、改目录缓存、宿主机重启、被 OOM 杀）旧"
                  f"令牌就作废，而它毫不知情 —— 换直链时拿到 401，整个请求以 404 收场。{X}")
            print(f"  {D}已经缓存过直链的片子照样能放，所以看着像「有的能放有的不能"
                  f"放」；等缓存陆续过期就变成「全都打不开」。重启会清空直链缓存，"
                  f"头几部片第一次点开会慢一点，正常。{X}")
            print(f"  {D}本来有自愈（保活每 20 分钟查一次令牌，发现作废就重启）。"
                  f"上面那个启动时间要是很久以前，说明自愈这条线没跑起来 ——"
                  f"检查 cron 有没有被停掉：systemctl status cron{X}")
        elif hits.get("上游/存储这一刻抽风"):
            print(f"  {B}→ 不是令牌的事，重启也没用{X}")
            print(f"  {D}是 OpenList 向上游要这个文件的时候被拒/找不到。这类源按请求"
                  f"频率抽风：同一条路径这一刻找不到、过一会儿又好了。{X}")
            print(f"  {D}能做的只有【少打扰它】：预热已经跳过这类盘、补探测也只补点"
                  f"开过的；再就是别在扫描/生成媒体库的时候看片。{X}")
        else:
            print(f"  {Y}→ 日志里没有现成的原因字样{X}")
            print(f"  {D}把这几行发出来：{X}")
            print(f"      docker logs --tail 200 mediawarp 2>&1 | grep -iE "
                  f"'404|401|error|失败' | tail -20")

if os.path.isfile(NGXLOG):
    try:
        tail = open(NGXLOG, encoding="utf-8", errors="replace").read().splitlines()[-4000:]
    except OSError:
        tail = []
    pd = [ln for ln in tail if re.search(r'"(?:GET|HEAD) /[pd]/', ln)]
    if pd:
        cc = {}
        for ln in pd:
            m = re.search(r'"\s(\d{3})\s', ln)
            if m:
                cc[m.group(1)] = cc.get(m.group(1), 0) + 1
        print()
        print(f"  {D}nginx 那侧最近 {len(pd)} 条 /p/ /d/ 请求：{X}"
              + "　".join(f"{G if c.startswith('2') else R}{c}×{n}{X}"
                          for c, n in sorted(cc.items())))
        # 【302 不算掉】那是正常的直链跳转，混进"失败"里会把比例算虚高。
        bad_n = sum(n for c, n in cc.items() if c[0] not in ("2", "3"))
        if bad_n and bad_n > len(pd) * 0.05:
            print(f"  {R}✖ {bad_n}/{len(pd)} 条没要到数据 —— 代理这条路在掉{X}")
            print(f"  {D}这些是【客户端真的在要数据】却没要到的那些，"
                  f"比上面任何一个测出来的数字都直接：脚本造的请求一次只有一个，"
                  f"而真播放是好几个并着来。{X}")
            # 【429 和 500 是两件事，处置不同】
            if cc.get("429"):
                print(f"  {Y}  · {cc['429']} 条 429 —— 源按【请求次数】限流{X}")
                print(f"  {D}    要得越频繁越撞。这正好跟 ④ 那一段对得上："
                      f"换段贵 → 播放器只好要得更勤 → 更容易撞限流 → 更卡。{X}")
            if cc.get("500"):
                print(f"  {Y}  · {cc['500']} 条 500 —— 连接被掐在半路{X}")
                print(f"  {D}    OpenList 已经开始往回发了，上游那边断了。"
                      f"播放器表现就是播着播着停住、或者拖过去之后连不稳。{X}")
            if cc.get("403"):
                print(f"  {Y}  · {cc['403']} 条 403 —— 被拒（UA 或签名）{X}")
else:
    print()
    print(f"  {D}（找不到 {NGXLOG}，nginx 那侧跳过）{X}")

print()
print(f"  {D}还想往下查：转码用 playing.sh，历次被指去了哪用 link-history.sh，"
      f"整条链通不通用 cant-play.sh。{X}")
PY

run_one() {   # $1 = 片名或挂载点   $2 = 拉多少秒
  python3 "$PYF" "$KEY" "$OLPW" "$DATA_ROOT" "$DOMAIN" "$2" "$1" "$MWLOG" "$MWSTART"
}

if [ -n "$Q" ]; then
  run_one "$Q" "$SECS"
  exit $?
fi

# 【不填参数 = 把每个盘都跑一遍，一次跑完】
# 上一版这里是列个菜单让人再敲一次 —— 而人要的从来不是菜单，是结论；
# 何况"到底哪个盘有问题"本来就得几个盘并排看才谈得上比较。
EACH="${MS_SECS:-30}"
DRV="$(python3 "$PYF" "$KEY" "$OLPW" "$DATA_ROOT" "$DOMAIN" "$EACH" "--drives" "$MWLOG" "$MWSTART")"
if [ -z "$DRV" ]; then
  echo "  ✖ 一个盘都没找到（$DATA_ROOT/strm 下面没有 strm）—— 先点「5 生成媒体库」"
  exit 1
fi
N="$(printf '%s\n' "$DRV" | grep -c .)"
echo
echo "  没指定查哪个 —— 那就【每个盘都查一遍】，共 $N 个。"
echo "  每个盘随机挑一部、拉 $EACH 秒，加上探测大约 $((N + N / 2 + 1)) 分钟。"
echo "  想只查一个：bash ${0##*/} 那个盘的挂载点"
printf '%s\n' "$DRV" | while IFS= read -r MP; do
  [ -n "$MP" ] || continue
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  $MP"
  echo "════════════════════════════════════════════════════════════"
  run_one "$MP" "$EACH" || echo "  （这个盘没测完，接着下一个）"
done
echo
echo "  全部跑完。把这一整屏发出去就行 —— 几个盘并排看才分得出是"
echo "  某个盘的毛病，还是这台机器/这条线路的毛病。"

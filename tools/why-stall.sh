#!/usr/bin/env bash
# 「能播，但过几秒卡一下」专用 —— 不用你去点播放，脚本自己当一回播放器把这部片拉一遍。
#
#   bash why-stall.sh 龙虎门          拉 45 秒
#   bash why-stall.sh 龙虎门 90       拉 90 秒
#   MS_N=2 bash why-stall.sh 龙虎门   同名的有好几个时，查第 2 个
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
# 只读，不改任何东西。
#
# 【要花多少流量】按播放码率拉，不是全速拉，所以大致就是「片子码率 × 秒数」。
# 一部 10 Mbps 的片拉 45 秒 ≈ 56 MB，两条路各一遍 ≈ 112 MB。跑之前屏上会先报数。
set -u

TOOL_VER="2026-09-18a"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

Q="${1:-}"
SECS="${2:-45}"
if [ -z "$Q" ]; then
  echo "用法：bash why-stall.sh <片名的一部分> [拉多少秒，默认 45]"
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
# 【拿来打码用的，不是拿来拼地址的】这一屏会被截图发出来，而 list.<域名> 出现在
# 直链里 —— 域名是安装人的信息，屏上一律换成 <你的域名>。
DOMAIN="$(sed -nE 's/^DOMAIN=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"

# 【日志走临时文件，不能用管道】python3 - <<'PY' 是从 stdin 读程序，
# 再往 stdin 里灌日志，两边抢同一个口子。link-history.sh 上栽过一次。
MWLOG="$(mktemp)"
trap 'rm -f "$MWLOG"' EXIT
docker logs --tail 4000 mediawarp >"$MWLOG" 2>&1 || : >"$MWLOG"

python3 - "$KEY" "$OLPW" "$DATA_ROOT" "$DOMAIN" "$SECS" "$Q" "$MWLOG" <<'PY'
import json, os, re, sys, threading, time
import urllib.error, urllib.parse, urllib.request

KEY, OLPW, DATA_ROOT, DOMAIN, SECS, Q, MWLOG = sys.argv[1:8]
EMBY, OL = "http://127.0.0.1:8096", "http://127.0.0.1:5244"
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


# ================= ① 这部片，以及它每秒要多少 =================
sec("① 这部片，以及它每秒要多少")
try:
    res = emby(f"/Items?Recursive=true&SearchTerm={urllib.parse.quote(Q)}"
               f"&IncludeItemTypes=Movie,Episode,Video&Limit=30"
               f"&Fields=Path,MediaSources,MediaStreams")
except Exception as e:
    print(f"  {R}✖ 连不上 Emby：{safe(e)}{X}")
    raise SystemExit(1)
hit = [i for i in (res.get("Items") or []) if str(i.get("Path") or "").endswith(".strm")]
if not hit:
    print(f"  {R}✖ 库里找不到带 strm 的条目：{Q}{X}")
    print(f"  {D}换个更短的关键词再试；或者这部片压根没进库。{X}")
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

raw, sign, ol_size = "", "", 0
try:
    req = urllib.request.Request(
        f"{OL}/api/fs/get",
        data=json.dumps({"path": body, "password": ""}).encode(),
        headers={"Content-Type": "application/json", "Authorization": tok},
        method="POST")
    r = json.load(urllib.request.urlopen(req, timeout=120))
except Exception as e:
    print(f"  {R}✖ 问 OpenList 失败：{safe(e)}{X}")
    raise SystemExit(1)
if r.get("code") != 200:
    print(f"  {R}✖ OpenList 不认这条路径{X}  {D}{safe(r.get('message'))[:120]}{X}")
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

# 【要测的是两条路，不是两个地址】
#   /d/ → 存储怎么配就怎么走（有 CDN 直链就 302 出去）= Emby 现在拿到的那条
#   /p/ → 强制经过 OpenList 本机代理             = 「改成本机代理」之后的那条
# 后者不用真去改配置就能先量一遍，这是这个脚本存在的主要理由：
# 那个决定（要不要给这个盘开本机代理）以前只能靠猜。
qpath = urllib.parse.quote(body)
proxy_url = f"{OL}/p{qpath}" + (f"?sign={sign}" if sign else "")
routes = []
if raw and not is_local:
    routes.append(("网盘 CDN 直链", raw))
    routes.append(("经过你的 VPS（本机代理）", proxy_url))
else:
    routes.append(("经过你的 VPS（这盘只有这一条）", proxy_url))

est = need_bps / 8 * SECS * len(routes)
print(f"  {D}这次要拉 {len(routes)} 条路 × {SECS} 秒 ≈ {mb(est)} 流量{X}")


# ================= 播放器模拟 =================
def follow(url, depth=4):
    """把 302 跟到底，每一跳记一次耗时。返回 (最终地址, [(码, 秒, 主机), ...])。"""
    hops, cur = [], url
    for _ in range(depth):
        req = urllib.request.Request(cur, headers={"User-Agent": UA,
                                                   "Range": "bytes=0-1"})
        t0 = time.time()
        try:
            with NOFOLLOW.open(req, timeout=30) as rr:
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


def puller(url, tank, start, chunk):
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
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Range": f"bytes={pos}-{end}"})
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


def play(url, label, need, total_secs):
    """当一回播放器：先填 2 秒缓冲再"开播"，然后每秒记一次水位。"""
    print()
    print(f"  {B}{label}{X}")
    final, hops = follow(url)
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
    th = threading.Thread(target=puller, args=(final, tank, start, chunk), daemon=True)
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


def verdict(rs, need):
    """把时间轴的形状翻译成一句人话。"""
    if not rs["started"]:
        return R, "这条路根本起不了播 —— 连 2 秒的缓冲都攒不满"
    dry = [i for i, (recv, left, on) in enumerate(rs["samples"])
           if on and recv == 0]
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
    if rs["ttfb"] and sorted(rs["ttfb"])[len(rs["ttfb"]) // 2] > 1.5:
        return Y, "每要一段新的都要等一两秒 —— 卡在「换段」上，不是卡在带宽上"
    return Y, f"卡了 {rs['stalls']} 秒，均速勉强够 —— 高码率段落顶不住"


sec(f"③ 当一回播放器，每条路各拉 {SECS} 秒")
print(f"  {D}从文件 10% 处开始拉（开头那一段常常被缓存过，测出来偏好看）。"
      f"缓冲上限 12 秒，填满就歇手 —— 播放器就是这么干的。{X}")
done = []
for label, u in routes:
    done.append(play(u, label, need_bps, SECS))
    if len(done) < len(routes):
        print(f"  {D}歇 5 秒再测下一条，免得撞上源的频率限制（那个 429 是自己造的）{X}")
        time.sleep(5)

# ================= ④ 结论 =================
sec("④ 结论")
for rs in done:
    col, why = verdict(rs, need_bps)
    print(f"  {col}{'✔' if col == G else '✖' if col == R else '⚠'}{X} "
          f"{B}{rs['label']}{X}")
    print(f"    {col}{why}{X}")

ok = [r for r in done if r["started"] and r["stalls"] == 0]
bad = [r for r in done if r not in ok]
print()
if len(done) > 1 and ok and bad:
    print(f"  {B}两条路结果不一样 —— 这就是可以直接动手的地方{X}")
    if "本机代理" in ok[0]["label"]:
        print(f"  {G}走你的 VPS 稳，走网盘 CDN 卡{X}")
        print(f"  {B}改法：media-stack → 4 挂载路径 → 选这个盘 → 本机代理{X}")
        print(f"  {D}代价是视频全程过你的 VPS，吃出口流量；换来的是不卡。{X}")
    else:
        print(f"  {G}走网盘 CDN 稳，走你的 VPS 卡{X}")
        print(f"  {D}说明瓶颈在 VPS 到网盘这一段，别开本机代理。{X}")
elif ok and not bad:
    print(f"  {G}几条路都供得上{X}")
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
    print(f"  {D}  · 换段就要等 → 直链在反复重定向，看上面每一跳的耗时{X}")

# ================= ⑤ 真实播放留下的痕迹 =================
sec("⑤ 这部片最近【真被人播】的时候，日志里发生了什么")
print(f"  {D}上面测的都是脚本自己造的请求。这一段看的是真实播放 —— "
      f"客户端可能走了完全不同的路（转码、或者压根没走 MediaWarp）。{X}")
print()
try:
    log = open(MWLOG, encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    log = []
mine = [ln for ln in log if f"/videos/{iid}/" in ln.lower()]
if not mine:
    print(f"  {Y}MediaWarp 日志里没有这个条目的播放记录{X}")
    print(f"  {D}要么最近没人点过它，要么客户端根本没走 MediaWarp"
          f"（转码就是这样 —— Emby 自己去拉，不经过 302）。{X}")
else:
    codes = {}
    for ln in mine:
        m = re.search(r"\|\s*(\d{3})\s*\|", ln)
        if m:
            codes[m.group(1)] = codes.get(m.group(1), 0) + 1
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
        print(f"  {Y}有 {codes['404']} 条 404 —— MediaWarp 换直链被拒了{X}")
        print(f"  {D}它只在启动那一刻登录一次 OpenList，OpenList 一重启旧令牌就作废。"
              f"敲 docker restart mediawarp。{X}")

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
        bad_n = sum(n for c, n in cc.items() if not c.startswith("2"))
        if bad_n and bad_n > len(pd) * 0.1:
            print(f"  {R}✖ 一成以上不是 2xx —— 代理这条路本身在掉{X}")
            print(f"  {D}这些是【客户端真的在要数据】却没要到的那些，"
                  f"比上面任何一个测出来的数字都直接。{X}")
else:
    print()
    print(f"  {D}（找不到 {NGXLOG}，nginx 那侧跳过）{X}")

print()
print(f"  {D}还想往下查：转码用 playing.sh，历次被指去了哪用 link-history.sh，"
      f"整条链通不通用 cant-play.sh。{X}")
PY

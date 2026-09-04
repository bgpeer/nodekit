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

TOOL_VER="2026-09-04d"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

P="${1:-}"
[ -n "$P" ] || { echo "用法：bash ${0##*/} \"/quark/夸克挂载/动漫/某剧/238 4K.mp4\""; exit 1; }

DIR="${MS_DIR:-/opt/media-stack}"
OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.secrets" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || OLPW="$(sed -nE 's/^OPENLIST_PASS=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$OLPW" ] || { echo "✖ 读不到 OpenList 管理密码（$DIR/.secrets 里的 OPENLIST_PASS）"; exit 1; }

export OL_PATH="$P" OL_PW="$OLPW"
python3 - <<'PY'
import json, os, re, subprocess, time, urllib.error, urllib.parse, urllib.request


def safe_url(u):
    """把地址里的非 ASCII 字符转义掉，不然 urllib 直接 UnicodeEncodeError。

    【为什么以前没暴露】夸克、阿里那种 CDN 直链本来就是转义好的，一路都对。而
    【代理型存储】（WebDAV、本地盘）返回的是 OpenList 自己的 /d/ 地址 —— 路径里
    原样带着中文。于是恰恰是最需要量一量的那类盘，第三步张口就报 UnicodeEncodeError，
    连"能不能拉"都没测到。safe 里留着 % ，避免把已经转义过的再转一遍。
    """
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc,
                                    urllib.parse.quote(p.path, safe="/%"),
                                    p.query, p.fragment))

BASE = "http://127.0.0.1:5244"
G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"; D="\033[2m"; B="\033[1m"; X="\033[0m"
path = os.environ["OL_PATH"]
parent = path.rsplit("/", 1)[0] or "/"


# 【令牌从全局取，不靠调用方记得传】上一版把 tok 做成了可选参数，然后 fs/list 和
# fs/get 两处都忘了传 —— 请求以游客身份发出去，OpenList 当场回
# "Guest user is disabled, login please"，耗时 0.0 秒。于是那几行看着像"网盘拒绝了"，
# 其实一步都没走到网盘。这种"测了个寂寞还打印得像模像样"的输出比不测更坏。
TOKEN = ""


def api(p, body=None, timeout=180, method="POST"):
    """调 OpenList 的接口。GET 的参数拼在 p 里，POST 的放 body。"""
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        BASE + p, data=data, method=method,
        headers={"Content-Type": "application/json",
                 **({"Authorization": TOKEN} if TOKEN else {})})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    el = time.monotonic() - t0
    try:
        return json.loads(raw), el
    except ValueError:
        # 【别把非 JSON 一句 JSONDecodeError 带过】接口路径或方法用错时返回的是一页
        # HTML，而"问不到状态"这四个字完全看不出是哪种错。原样给回前几十个字符。
        return {"code": -1, "message": f"返回的不是 JSON：{raw[:80]}"}, el


def hls_first_segment(text, base):
    """从 m3u8 里取第一个能拉的地址。主列表就先下一层，媒体列表就取第一个分片。

    和 media-stack.py 的 warm_hls 同一个道理：拉播放列表等于只读了一遍目录，
    网盘一点视频数据都没准备 —— 要量真实速度，必须下钻到分片。
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    urls = [l for l in lines if not l.startswith("#")]
    if not urls:
        return ""
    nxt = urllib.parse.urljoin(base, urls[0])
    if "#EXT-X-STREAM-INF" in text:          # 主列表：再下一层才是分片
        try:
            req = urllib.request.Request(safe_url(nxt),
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                sub = r.read(1 << 18).decode("utf-8", "replace")
            subs = [l.strip() for l in sub.splitlines()
                    if l.strip() and not l.startswith("#")]
            if subs:
                return urllib.parse.urljoin(nxt, subs[0])
        except Exception:
            return ""
    return nxt


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
            # 【长行要留头也留尾】RESTY 那种行把整条 URL 铺在中间，而【失败的原因】
            # 在最末尾。上一版直接砍前 200 字，砍掉的正好是唯一有用的那半句 ——
            # 屏幕上剩一堆 access_token=***&app_ver=… 什么都说明不了。
            ln = clean(ln)
            if len(ln) > 190:
                ln = ln[:110] + f" {D}…{X} " + ln[-80:]
            print(f"  {D}·{X} {ln}")
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
    print(f"  {D}· context canceled             【发起方先断了】—— 网页等不下去，"
          f"自己把请求取消了；{X}")
    print(f"  {D}                               OpenList 顺手也取消了上游那一条。"
          f"不是网盘拒绝，是它太慢{X}")
    print(f"  {D}· 一行都没有                    问题不在 OpenList，往浏览器/线路那边看{X}")


try:
    r, _ = api("/api/auth/login", {"username": "admin", "password": os.environ["OL_PW"]},
               timeout=20)
    TOKEN = (r.get("data") or {}).get("token", "")
except Exception as e:
    print(f"{R}✖{X} 连不上 OpenList：{e}")
    raise SystemExit(1)
if not TOKEN:
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
    # 这个接口是 GET，参数拼在 URL 上。上一版 POST 过去拿回一页 HTML，
    # 于是只报了一句没头没脑的 JSONDecodeError。
    r, _ = api("/api/admin/storage/list?page=1&per_page=100", timeout=30, method="GET")
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
    if r.get("code") not in (200, 500):
        # 【认证类的错要当场停】不然后面每一步都拿同一句拒绝当"网盘的回答"，
        # 还配上耗时和结论，越看越像网盘出了问题。
        print(f"  ① 列目录      {R}{r.get('message')}{X}")
        print(f"\n  {Y}这不是网盘的问题 —— 请求没被 OpenList 认下来。{X}")
        show_logs()
        raise SystemExit(1)
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

def pull(url, note="", n=1 << 20):
    """拉一段，报耗时/大小/速度，并说清楚断点续传支不支持。返回读到的前几百字节。"""
    req = urllib.request.Request(safe_url(url),
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Range": f"bytes=0-{n - 1}"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as resp:
        buf = resp.read(n)
        code = resp.status
    t = time.monotonic() - t0
    got = len(buf)
    mbps = got * 8 / t / 1e6 if t > 0 else 0
    c = G if mbps >= 8 else (Y if mbps >= 3 else R)
    print(f"  ③ 拉 1 MiB{note:<4}{c}{t:6.1f} 秒{X}  {got / 1024:.0f} KB  "
          f"{c}{mbps:.1f} Mbps{X}{D}（{got / t / 1024:.0f} KB/s）{X}")
    return buf, code


# ---- ③ 真的拉一段，确认这条地址能不能出数据 ----
if raw.startswith("http"):
    N = 1 << 20
    req = urllib.request.Request(safe_url(raw),
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Range": f"bytes=0-{N - 1}"})
    try:
        body, code = pull(raw)
        # 【206 还是 200，决定了能不能拖进度条】我们发的是 Range 请求：
        #   206 Partial Content = 对方认 Range，播放器想从哪儿开始就从哪儿开始
        #   200 OK              = 不认，整个文件从头发。播放器要跳到 30 分钟处，
        #                         只能把前 30 分钟全下下来 —— 表现就是"进度条拉不动、
        #                         只能从头看"，而且流量白烧一遍
        # 这一条对【代理型存储】（WebDAV、本地盘这些没有 CDN 直链的）尤其要紧：
        # 那条路上 OpenList 要把 Range 透传给上游，上游还得自己支持，缺一环都不行。
        # 【转码流不能拿 Range 来判】m3u8 是一份【完整的小文件】（播放列表），服务器
        # 当然回 200、当然不认 Range —— 那不是"拖不动进度条"，HLS 的 seek 本来就是
        # 靠切换分片做的，跟 Range 没关系。上一版拿同一把尺子去量，把夸克的转码流
        # 判成了"进度条拉不动"，结论正好反了。判据：内容以 #EXTM3U 开头。
        head = body[:200].lstrip()
        if head.startswith(b"#EXTM3U"):
            print(f"  {D}   └ 这是{X}{C}转码流的播放列表{X}{D}（HLS，m3u8）—— "
                  f"上面那个大小是列表本身，不是视频{X}")
            print(f"  {D}      HLS 的进度条靠切换分片，不看 Range，"
                  f"所以 200 在这里是正常的{X}")
            seg = hls_first_segment(body.decode("utf-8", "replace"), raw)
            if seg:
                try:
                    pull(seg, "（分片）")
                except Exception as e:
                    print(f"  {D}      分片拉不动：{type(e).__name__}{X}")
            else:
                print(f"  {D}      列表里没解析出分片地址{X}")
        elif code == 206:
            print(f"  {D}   └ 断点续传  {G}支持{X}{D}（206）—— 进度条能拖{X}")
        else:
            print(f"  {D}   └ 断点续传  {R}不支持{X}{D}（HTTP {code}，我们要的是 206）"
                  f" —— 进度条拉不动，跳转只能从头下{X}")
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

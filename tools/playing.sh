#!/usr/bin/env bash
# 现在正在播的这一路，到底是【直接播放】还是【转码】。只读。
#
#   bash playing.sh          现在正在播什么、怎么播的
#
# 【为什么这件事值得单独查】302 只保证 MediaWarp 把播放器指去了网盘。
# 但 Emby 还有另一条路会把这件事整个作废：转码。一转码，视频就得先由这台
# 机器从网盘拉下来、转完再发给播放器 —— 302 等于白设，而日志里照样打 302，
# 从外面完全看不出来。所以「有没有 302」和「是不是直接播放」是两个问题，
# 得分开问。
#
# 【它只看得见成功的播放】这个脚本读的是 Emby 里「谁正在放什么」，而点开
# 【播不出来】的那一次，这个名单里根本不会出现 —— 播放压根没开始。
# 所以它答的是「能播，但卡 / 糊 / 换几部之后出不来画面」。
# 「点开就转圈 / load fail」那一类归 cant-play.sh，那个不需要有人正在播。
set -u

TOOL_VER="2026-09-20d"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

DIR="${MS_DIR:-/opt/media-stack}"
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key（$DIR/mediawarp/config/config.yaml）"; exit 1; }

DATA_ROOT="$(sed -nE 's/^DATA_ROOT=(.*)$/\1/p' "$DIR/.env" 2>/dev/null | head -1)"
[ -n "$DATA_ROOT" ] || DATA_ROOT="$DIR/media"
python3 - "$KEY" "$DIR" "$DATA_ROOT" <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request


class _NoRedir(urllib.request.HTTPRedirectHandler):
    """要的就是 302 本身 —— 跟随了就看不见它指到哪儿去了。"""
    def redirect_request(self, *a, **k):
        return None

KEY = sys.argv[1]
MS_DIR = sys.argv[2] if len(sys.argv) > 2 else "/opt/media-stack"
DATA_ROOT = sys.argv[3] if len(sys.argv) > 3 else os.path.join(MS_DIR, "media")
BASE = "http://127.0.0.1:8096"
G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; C = "\033[36m"
D = "\033[2m";  B = "\033[1m"; X = "\033[0m"


def api(path):
    sep = "&" if "?" in path else "?"
    u = f"{BASE}{path}{sep}api_key={KEY}"
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def drive_of(item_id):
    """这个条目落在哪个网盘挂载点上，以及那个盘怎么设的。

    【"哪个盘"是这一屏最要紧的一栏】同一台机器上，夸克走 302 直链、七米蓝走本机
    代理、115 可能是转码流 —— 它们的快慢、限流、字节走不走 VPS 全都不一样。
    不说是哪个盘，"有的能播有的不能"这条线索就断在这儿了。

    全是本地只读：条目 → Path → 本地 strm → 网盘路径 → 挂载点 → OpenList 的库。
    读不出来就返回空，绝不猜。
    """
    try:
        det = (api(f"/Items?Ids={item_id}&Fields=Path").get("Items") or [{}])[0]
        cp = str(det.get("Path") or "")
    except Exception:
        return "", ""
    if not cp.startswith("/data/strm/"):
        return "", ""
    hp = os.path.join(DATA_ROOT, "strm", cp[len("/data/strm/"):])
    try:
        line = open(hp, encoding="utf-8", errors="replace").readline().strip()
    except OSError:
        return "", ""
    if not line.startswith("/"):
        return "", ""
    mp = "/" + line.lstrip("/").split("/", 1)[0]

    db = os.path.join(MS_DIR, "openlist", "config", "data.db")
    if not os.path.exists(db):
        return mp, ""
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("select addition, web_proxy from x_storages "
                          "where mount_path = ?", (mp,)).fetchone()
        con.close()
    except Exception:
        return mp, ""
    if not row:
        return mp, ""
    try:
        add = json.loads(row[0] or "{}")
    except Exception:
        add = {}
    # 回源方式：这一项决定字节走不走 VPS，是整屏最要紧的一个字
    proxy = str(row[1] or "").lower() in ("1", "true")
    bits = ["本机代理（字节过 VPS）" if proxy else "302 直链（不过 VPS）"]
    # 画质：转码流在夸克叫 link_method、在 115 叫 use_transcoding_address
    if str(add.get("link_method") or "") == "streaming" or \
            str(add.get("use_transcoding_address") or "").lower() in ("true", "1"):
        bits.append("转码流")
    elif "link_method" in add or "use_transcoding_address" in add:
        bits.append("原画直链")
    return mp, " · ".join(bits)


def served_kind(item_id):
    """MediaWarp 现在【真给】这个条目什么。返回 (是不是 m3u8, 主机名, 说明)。

    【这一问是这个工具最该做却一直没做的】上面那些都是 Emby 自己的说法 ——
    它认为文件是 mp4 还是 hls、它打算 DirectStream 还是 Transcode。可真正送到
    播放器手上的，是 MediaWarp 302 出去的那个地址。两边对不上的时候，播放器
    拿到的东西和它被告知的不是一回事，表现就是"流量在跑、画面出不来"。

    【不打印地址】那里面带着签名，整条贴出来等于把一条能直接下片的链交出去。
    只回它是不是 m3u8、以及主机名。
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:9000/Videos/{item_id}/stream"
        f"?MediaSourceId=mediasource_{item_id}&Static=true&api_key={KEY}",
        headers={"User-Agent": "curl/8.5.0"})
    op = urllib.request.build_opener(_NoRedir)
    loc = ""
    try:
        op.open(req, timeout=40).close()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        if not loc:
            return None, "", f"MediaWarp 回了 {e.code}，没给 302"
    except Exception as e:
        return None, "", f"问不到 MediaWarp（{type(e).__name__}）"
    if not loc:
        return None, "", "MediaWarp 没给 302"
    head = loc.split("?", 1)[0]
    try:
        host = urllib.parse.urlsplit(loc).hostname or "?"
    except ValueError:
        host = "?"
    return (".m3u8" in head.lower()), host, ""


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


try:
    sess = api("/Sessions")
except Exception as e:
    print(f"{R}✖{X} 连不上 Emby：{e}")
    sys.exit(1)

live = [s for s in sess if s.get("NowPlayingItem")]
if not live:
    print(f"{Y}⚠{X} 现在没有人在播。")
    # 【这一支上很容易说错话】上一版写的是"先在播放器里点开一集，再回来跑一次"。
    # 可这个脚本读的是 Emby 的 /Sessions 再筛 NowPlayingItem ——【播放从来没开始
    # 的那一条，这个列表里根本不存在】。于是"点开、转圈、播不出来"的人照做之后
    # 看到的还是这一句，像是在说他没照做。
    # 这个脚本只看得见【成功】的播放；失败的那些它永远看不见，这是它的构造决定的，
    # 不是这次没赶上。所以这里要先把这件事说清楚，再把人交给对的工具。
    print(f"  {D}要说清楚的是：{X}{B}这个脚本只看得见成功的播放{X}"
          f"{D} —— 它读的是 Emby 里"
          f"「谁正在放什么」，而点开【播不出来】的时候，那一条压根就不会出现。"
          f"所以"
          f"「播不出来」＋「这里说没人在播」是配套的，不是你没点。{X}")
    print()
    print(f"  {B}点开就转圈 / load fail —— 用这个：{X}")
    # 【不留尖括号】写成 <片名> 的话，照着敲下去 bash 会把它当重定向，
    # 参数根本传不进来（这个坑这边已经踩过一次）。写成能直接敲的样子，
    # 原样敲下去最多是"没找到这个片名"，不会莫名其妙地失败。
    print(f"  {C}bash cant-play.sh 片名{X}"
          f"{D}   —— 把「片名」换成那部播不出来的片，片名的一部分就行{X}")
    print(f"  {D}它不用有人正在播 —— 自己把「点播放」那一串请求从头走一遍，"
          f"六段（条目 → strm → OpenList → MediaWarp → 直链 → 拖进度条）逐段报，"
          f"坏在哪一段就报哪一段。{X}")
    print()
    print(f"  {D}这个脚本（playing.sh）留着回答另一类问题：能播、但卡 / 糊 / "
          f"换几部之后出不来画面 —— 那些【有人正在播】，跑它才有东西看。{X}")
    sys.exit(0)

# 【先数清楚有几路】"连换几部之后画面出不来"这个症状，唯一要回答的就是这个数。
# Emby 在你切走的时候不会立刻收掉上一路（客户端得先告诉它，而"直接换下一部"
# 往往来不及说）。于是连换几部之后机器上同时挂着好几路 —— 屏上会摆出几段
# 一模一样的输出，而人不会自己去数，也不会意识到"这几段本该只有一段"。
print()
if len(live) == 1:
    print(f"  现在 {B}1 路{X} 在播。")
else:
    print(f"  {Y}⚠ 现在有 {B}{len(live)} 路{X}{Y} 同时在播{X}")
    print(f"  {D}连着换片的时候最容易这样：切走的那一路 Emby 没有立刻收掉。"
          f"要是它们在转码，几个 ffmpeg 分这台机器那点 CPU，"
          f"【谁都出不来帧】—— 而每一路都在拉源文件，所以流量很好看。{X}")
    print(f"  {D}处置：在播放器里退回去把多余的那几路停掉，"
          f"或者等 Emby 自己回收（通常几分钟）。{X}")

for s in live:
    it = s["NowPlayingItem"]
    ps = s.get("PlayState") or {}
    tr = s.get("TranscodingInfo") or {}
    method = str(ps.get("PlayMethod") or "")

    print()
    print("=" * 58)
    print(f"  {B}{it.get('Name') or '?'}{X}"
          f"   {D}{s.get('Client') or '?'} {s.get('ApplicationVersion') or ''}"
          f"  ({s.get('DeviceName') or '?'}){X}")
    print("=" * 58)

    # 【这三种的区别，就是视频走不走这台机器】
    #   DirectPlay   播放器直接吃原文件 —— 302 之后流量在播放器和网盘之间
    #   DirectStream 只换容器，视频流仍然【经过本机】转手一遍
    #   Transcode    整条视频在本机重编码，最吃 CPU、也最慢
    # 【先说在哪个盘】这一栏摆在播放方式前面 —— 因为字节走不走 VPS 由它决定，
    # 而不是由 Emby 报的 PlayMethod 决定。
    _mp, _how = drive_of(it.get("Id"))
    if _mp:
        print(f"  {D}所在网盘   {X}{C}{_mp}{X}"
              + (f"  {D}（{_how}）{X}" if _how else ""))
    else:
        print(f"  {D}所在网盘   读不出来（这个条目不是 strm，或者库里没有那个盘）{X}")

    if method == "DirectPlay":
        print(f"  {G}✔ 直接播放{X}（DirectPlay）"
              f"{D} —— 视频没经过这台机器，302 是真生效的{X}")
    elif method == "DirectStream":
        # 【别说过头】上一版写死"视频流还是经过这台机器"。可 MediaWarp 是在 Emby
        # 前面把 /Videos/{id}/stream 拦下来直接 302 的 —— Emby 报的 PlayMethod 是
        # 【它自己的打算】，不等于字节真的从它身上过。真正决定字节走哪的是上面
        # 那一栏（这个盘的回源方式）：302 直链就不过 VPS，本机代理才过。
        print(f"  {Y}⚠ 直接流{X}（DirectStream）"
              f"{D} —— Emby 打算只换容器、不重编码{X}")
        print(f"  {D}             这是 Emby 的【打算】，不等于字节从它身上过 ——"
              f"MediaWarp 会在它前面把请求 302 走。字节走哪看上面「所在网盘」那一栏。{X}")
    elif method == "Transcode":
        print(f"  {R}✖ 转码{X}（Transcode）"
              f"{D} —— 这台机器要先从网盘把片子拉下来、转完再发给播放器。"
              f"302 等于白设{X}")
    else:
        print(f"  {Y}⚠ 播放方式：{method or '(Emby 没报)'}{X}")

    if tr:
        why = tr.get("TranscodeReasons") or []
        if isinstance(why, str):
            why = [why]
        print(f"  {D}转码原因   {X}{'、'.join(why) or '(没报)'}")
        vd = tr.get("IsVideoDirect")
        ad = tr.get("IsAudioDirect")
        print(f"  {D}视频 {'原样' if vd else '重编码'}"
              f" / 音频 {'原样' if ad else '重编码'}"
              f"   目标 {tr.get('VideoCodec') or '?'}/{tr.get('AudioCodec') or '?'}"
              f"   {(tr.get('Bitrate') or 0) / 1e6:.1f} Mbps{X}")

    # 片子本身要多少码率 —— 这个数决定了"多快才算够"，没有它没法判卡不卡
    src = None
    try:
        det = (api(f"/Items?Ids={it.get('Id')}&Fields=MediaSources")
               .get("Items") or [{}])[0]
        ms = det.get("MediaSources") or []
        src = ms[0] if ms else None
    except Exception:
        pass
    if src:
        bit = src.get("Bitrate") or 0
        size = src.get("Size") or 0
        ticks = src.get("RunTimeTicks") or it.get("RunTimeTicks") or 0
        secs = ticks / 1e7 if ticks else 0
        if not bit and size and secs:
            bit = size * 8 / secs
        line = f"  {D}文件       {src.get('Container') or '?'}"
        if size:
            line += f"  {size / 1024 ** 3:.2f} GB"
        if secs:
            line += f"  {hms(secs)}"
        print(line + X)
        if bit:
            print(f"  {D}平均码率   {X}{C}{bit / 1e6:.1f} Mbps{X}"
                  f"{D}（≈ {bit / 8 / 1024 ** 2:.1f} MB/s，"
                  f"拉不到这个速度就会卡）{X}")
        else:
            # 【0 不是码率，是"没探到"】转码流的 MediaSource 本来就没有 size/bitrate。
            # 印成「0.0 Mbps（拉不到这个速度就会卡）」等于拿一个不存在的数当及格线。
            print(f"  {D}平均码率   {Y}没探到{X}"
                  f"{D} —— 转码流的播放列表本来就没有大小和码率这两项，"
                  f"不是真的 0。这一档判不了「够不够快」。{X}")

    # ---- Emby 认为的 vs MediaWarp 真给的 ----
    # 【这一对才是答案】Emby 的 MediaSources 是探测那一刻记下的，之后不会重探。
    # 盘的画质后来被改过（原画 ↔ 转码流）的话，老条目手里还是旧格式，而 MediaWarp
    # 按【现在】的设置发地址 —— 于是 Emby 说"这是个 0.53 GB 的 mp4，按字节范围拉"，
    # 实际来的却是一份 m3u8 播放列表。播放器拿到的和它被告知的不是一回事。
    _cont = str((src or {}).get("Container") or "").lower()
    print(f"  {D}（下面这一问要换一次直链 —— 换直链正是这些源限流最狠的动作，"
          f"一路只问一次）{X}")
    _is_m3u8, _host, _why = served_kind(it.get("Id"))
    if _is_m3u8 is None:
        print(f"  {Y}MediaWarp 真给的：没问出来{X}  {D}{_why}{X}")
    else:
        _kind = "m3u8（转码流）" if _is_m3u8 else "整个文件（原画）"
        _emby_hls = _cont in ("hls", "m3u8")
        if _emby_hls == _is_m3u8:
            print(f"  {G}✔ 对得上{X}  {D}Emby 认为是 {_cont or '?'}，"
                  f"MediaWarp 真给的也是{_kind}　{_host}{X}")
        else:
            # 【这就是"有的能播有的不能"】而且它只出现在"盘的画质被改过"之后
            # 入库时间早于那次改动的条目上 —— 所以同一个盘里有的好有的坏。
            print(f"  {R}✖ 对不上{X}  {D}Emby 认为是 {X}{C}{_cont or '?'}{X}{D}，"
                  f"而 MediaWarp 真给的是 {X}{C}{_kind}{X}{D}　{_host}{X}")
            print(f"  {D}Emby 的媒体信息是【探测那一刻】记下的，之后不会重探。"
                  f"这个盘的画质后来改过，而这个条目是改之前入库的 —— 于是 Emby "
                  f"按旧格式告诉播放器怎么拿，来的却是新格式。{X}")
            print(f"  {B}这就是「有的能播有的不能」：同一个盘里，改动之前入库的"
                  f"那批全是这个毛病。{X}")
            print(f"  {D}修法：让这些条目重新探一次 —— media-stack heal-reset "
                  f"然后 media-stack heal；heal 补不动的，在 Emby 里对这个媒体库"
                  f"点「刷新元数据 → 覆盖所有元数据」。{X}")

    pos = (ps.get("PositionTicks") or 0) / 1e7
    if pos:
        print(f"  {D}已播到     {hms(pos)}"
              + ("   ⏸ 暂停中" if ps.get("IsPaused") else "") + X)

# ---------------------------------------------------------------- 真的有几个在转
# 【这是"转不动"的硬证据】上面 PlayMethod 说的是 Emby 打算怎么播，这里看的是
# 机器上真的有几个编码器在跑、一共吃掉多少 CPU。几路同时转的时候，帧率上不去
# 不是因为哪一路有问题，是因为它们在分同一份 CPU。
#
# 【只打进程名和数字，不打命令行】argv 里可能带节点的 UUID / 密码 / reality
# 私钥（仓库规矩第一条），而这一屏是要截图发人的。
import subprocess
try:
    _ps = subprocess.run(["ps", "-eo", "pcpu,comm"], capture_output=True,
                         text=True, timeout=15).stdout
except Exception:
    _ps = ""
_cpu, _n = 0.0, 0
for _ln in _ps.splitlines()[1:]:
    _f = _ln.split(None, 1)
    if len(_f) < 2 or "ffmpeg" not in _f[1].lower():
        continue
    try:
        _cpu += float(_f[0])
    except ValueError:
        continue
    _n += 1
_cores = os.cpu_count() or 1
print()
if _n:
    _sat = _cpu / (_cores * 100.0)
    col = R if _n > 1 or _sat >= 0.7 else Y
    print(f"  {col}机器上有 {_n} 个 ffmpeg 在跑{X}"
          f"  {D}一共吃 {_cpu:.0f}% CPU，这台机器 {_cores} 核"
          f"（满载算 {_cores * 100}%）{X}")
    if _n > 1:
        print(f"  {D}几个一起转就是在分同一份 CPU —— 每一路的帧率都会掉到"
              f"出不来画面。先把多余的那几路停掉再看。{X}")
    elif _sat >= 0.7:
        print(f"  {D}CPU 基本吃满了：这台机器转不动这个片源，"
              f"调参数救不回来。治法在客户端那头（换个能直解的播放器）。{X}")
else:
    print(f"  {D}机器上没有 ffmpeg 在跑 —— 没有人在转码。"
          f"那「流量在跑却出不来画面」就不是转码造成的，"
          f"改用 why-stall.sh 查取流那一段。{X}")

print()
print(f"  {D}这里说的是【怎么播】，不是【快不快】。要量速度用 ali-403.sh；"
      f"要看 302 有没有发出去用 media-stack 302。{X}")
PY

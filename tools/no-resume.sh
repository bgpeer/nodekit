#!/usr/bin/env bash
# 「这一集我明明看过，Emby 就是不给进度条 / 不给续播记忆」专用。只读，不改任何东西。
#
#   bash no-resume.sh 仙逆            片名的一部分、文件名的一部分都行
#   bash no-resume.sh 第159集         集号也行
#   bash no-resume.sh 仙逆 3          同名的有好几个时，查第 3 个
#
# 【为什么值得单独写一个】进度条记不住在界面上永远只有"没有进度条"这一种样子，
# 而原因有五个，处置完全不同：
#
#   ① 这个条目没有时长      ← 最常见。Emby 的续播点是按【时长的百分比】存的，
#                             分母为 0 那套逻辑整个失效：停止播放直接判"已看完"，
#                             续播点清零。界面上就是 0B / 0bps 那一行
#   ② 时长有、音视频轨没有  ← 半探到。能播，但每次点开都要现场再探一次
#   ③ 媒体库的续播门槛没设对 ← 默认 120 秒 / 5%，短片永远够不着
#   ④ 在补时长的队列里，还没轮到 ← 补时长跟着每小时那条定时任务跑
#   ⑤ 被"探不出来"放弃了     ← 探够次数就退避，最长 360 天才再试一次
#
# 猜是猜不出来的：①②看条目、③看库、④⑤看脚本自己的账本。一项一项问，
# 哪一项不对就报哪一项，并给出对应的下一步。
set -u

TOOL_VER="2026-09-20a"          # 见 link-history.sh 里的说明：CDN 会缓存
echo "  ${0##*/}  版本 $TOOL_VER"

Q="${1:-}"
N="${2:-1}"
if [ -z "$Q" ]; then
  echo "用法：bash no-resume.sh 片名的一部分 [第几个]"
  echo "     例：bash no-resume.sh 仙逆        bash no-resume.sh 第159集"
  exit 1
fi
DIR="${MS_DIR:-/opt/media-stack}"

KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key（先跑「3 后补参数」）"; exit 1; }

export MS_KEY="$KEY" MS_Q="$Q" MS_N="$N" MS_DIR="$DIR"
python3 - <<'PY'
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

EMBY = "http://127.0.0.1:8096"
KEY  = os.environ["MS_KEY"]
Q    = os.environ["MS_Q"]
DIR  = os.environ["MS_DIR"].rstrip("/")
G="\033[32m"; Y="\033[33m"; R="\033[31m"; D="\033[2m"; B="\033[1m"; C="\033[36m"; X="\033[0m"

# 脚本自己的账本和常量都在这两个地方 —— 【不写死一份】，写死就会和 media-stack.py
# 各走各的，屏上说"还要等 30 天"而实际是 240 天（这个坑这边踩过）。
STATE = "/etc/bgpeer/media-stack.json"
SRC   = "/etc/bgpeer/media-stack.py"


def hr():
    print(f"  {D}{'-' * 58}{X}")


def emby(path, timeout=30):
    u = f"{EMBY}{path}{'&' if '?' in path else '?'}api_key={KEY}"
    with urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": "curl/8.5.0"}),
            timeout=timeout) as r:
        return json.load(r)


def const(name, fallback):
    """从装在机器上的 media-stack.py 里读一个常量。读不到就用兜底值。

    【为什么不直接写死】这些数字（放弃次数、退避天数、每天的流量上限）是
    media-stack.py 说了算的。这里再抄一份，改了那边不改这边，屏上就会说一个
    早就不成立的天数 —— 而人会照着那个天数去等。
    """
    try:
        with open(SRC, encoding="utf-8", errors="replace") as f:
            m = re.search(rf"^{name}\s*=\s*(\d+)", f.read(), re.M)
        return int(m.group(1)) if m else fallback
    except OSError:
        return fallback


GIVEUP      = const("HEAL_GIVEUP", 3)
GIVEUP_DAYS = const("HEAL_GIVEUP_DAYS", 30)
GIVEUP_MAX  = const("HEAL_GIVEUP_MAX_DAYS", 360)
DAY_MB      = const("HEAL_DAY_MB", 2048)
MIN_SEC     = const("RESUME_MIN_SECONDS", 2)


def state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # 【读不到要说出来】这个文件是 root-only 的，非 root 跑就会走到这儿；
        # 静默当成空表的话，④⑤ 两项会一律报"没被放弃"——那是猜的，不是查的。
        return None


# ================================================================ 找条目
print()
print(f"  {B}① Emby 里的这个条目{X}")
hr()
try:
    users = emby("/Users") or []
except Exception as e:
    print(f"  {R}✖ 连不上 Emby：{type(e).__name__}{X}")
    raise SystemExit(1)
uid = (users[0] or {}).get("Id", "") if users else ""
if not uid:
    print(f"  {R}✖ Emby 里一个用户都没有{X}")
    raise SystemExit(1)

try:
    d = emby(f"/Users/{uid}/Items?Recursive=true"
             f"&IncludeItemTypes=Movie,Episode,Video"
             f"&Fields=Path,MediaSources,MediaStreams,UserData,DateCreated"
             f"&Limit=5000", timeout=120)
except Exception as e:
    print(f"  {R}✖ 问不到 Emby：{type(e).__name__}{X}")
    raise SystemExit(1)
items = d.get("Items") or []
hit = [i for i in items
       if Q.lower() in str(i.get("Name") or "").lower()
       or Q.lower() in str(i.get("Path") or "").lower()]
if not hit:
    print(f"  {R}✖ 没有匹配「{Q}」的条目{X}  {D}库里现在有 {len(items)} 个{X}")
    print(f"  {D}填的是 Emby 里显示的那个名字吗？集号也行，比如「第159集」。{X}")
    raise SystemExit(1)
try:
    n = max(1, int(os.environ.get("MS_N") or 1))
except ValueError:
    n = 1
if len(hit) > 1:
    print(f"  {D}匹配到 {len(hit)} 个，看第 {n} 个"
          f"（换一个就在命令后面加个数字）：{X}")
    for k, i in enumerate(hit[:8], 1):
        mark = f"{C}←{X}" if k == n else " "
        print(f"    {mark} {k}. {str(i.get('Name') or '?')[:40]}")
it = hit[min(n, len(hit)) - 1]
iid = str(it.get("Id"))
srcs = it.get("MediaSources") or []
ticks = (min((s.get("RunTimeTicks") or 0) for s in srcs) if srcs
         else (it.get("RunTimeTicks") or 0))
mins = ticks / 6e8
nstream = len(it.get("MediaStreams") or []) or sum(
    len(s.get("MediaStreams") or []) for s in srcs)
size = max([int(s.get("Size") or 0) for s in srcs] or [0])
print(f"  {B}{str(it.get('Name') or '?')[:40]}{X}  {D}条目 {iid}{X}")
print(f"  {D}时长{X}      " + (f"{G}{mins:.0f} 分{X}" if mins else f"{R}没有（0）{X}"))
print(f"  {D}音视频轨{X}  " + (f"{G}{nstream} 条{X}" if nstream else f"{R}0 条{X}"))
print(f"  {D}大小{X}      "
      + (f"{size / 1024 ** 3:.2f} GB" if size else f"{D}0（转码流本来就没有这一项）{X}"))

# 【这一项就是"记不住进度"的直接原因】没有时长 = Emby 存不下续播点。
if not mins:
    print()
    print(f"  {R}✖ 没有时长 —— 这就是记不住进度的直接原因{X}")
    print(f"  {D}Emby 的续播点是按【时长的百分比】存的。分母为 0，那套逻辑整个"
          f"失效：你一停止播放它直接判「已看完」，续播点清零。{X}")
    print(f"  {D}所以这不是播放器的问题，也不是哪个开关没打开 —— 补上时长就好了，"
          f"下面几步查的就是「为什么还没补上」。{X}")
elif not nstream:
    print()
    print(f"  {Y}⚠ 时长有、音视频轨 0 条 —— 只探到一半{X}")
    print(f"  {D}进度条能记住了，但每次点开 Emby 都要【现场再探一次】——"
          f"源那会儿给得出数据就能播，正在限流就是 load fail。{X}")
else:
    print()
    print(f"  {G}✔ 媒体信息是齐的{X}  {D}那「记不住进度」就不是这一项，往下看 ③{X}")

# ================================================================ 播放记录
print()
print(f"  {B}② 你的播放记录（Emby 到底存没存住）{X}")
hr()
# 【必须遍历每个账号】电视一个号、手机一个号是常态。只看第一个号的话，
# 另一个号看过的片子这里会显示"从没点开过"，而那是错的。
seen_any = False
for u in users:
    _uid = (u or {}).get("Id") or ""
    _name = str((u or {}).get("Name") or "?")
    if not _uid:
        continue
    try:
        one = emby(f"/Users/{_uid}/Items?Ids={iid}&Fields=UserData")
        ud = ((one.get("Items") or [{}])[0].get("UserData")) or {}
    except Exception:
        print(f"  {Y}{_name}：问不到{X}")
        continue
    pos = (ud.get("PlaybackPositionTicks") or 0) / 1e7
    played = bool(ud.get("Played"))
    pct = ud.get("PlayedPercentage")
    if not (played or pos or ud.get("PlayCount")):
        print(f"  {D}{_name}：没点开过{X}")
        continue
    seen_any = True
    bits = []
    if played:
        bits.append(f"{Y}已标记看完{X}")
    if pos:
        bits.append(f"{G}续播点 {int(pos // 60)} 分 {int(pos % 60)} 秒{X}")
    else:
        bits.append(f"{R}没有续播点{X}")
    if pct is not None:
        bits.append(f"{D}进度 {float(pct):.0f}%{X}")
    print(f"  {B}{_name}{X}：{'　'.join(bits)}")
if not seen_any:
    print()
    print(f"  {Y}⚠ 所有账号都没有这一条的播放记录{X}")
    # 【这一条决定了 ④】补时长只补"你点开过的"——没点开过就根本不在队列里
    print(f"  {D}补时长【只补点开过的】（库里两千多部，点开过的几十部，"
          f"这一条把每天的流量从 GB 压到 MB）。没点开过 = 不在队列里，"
          f"这是设计如此，不是坏了。{X}")
elif not mins:
    print()
    print(f"  {D}「已标记看完」+「没有续播点」正是分母为 0 的样子 —— 见 ①。{X}")

# ================================================================ 媒体库的续播门槛
print()
print(f"  {B}③ 这个条目所在媒体库的续播门槛{X}")
hr()
path = str(it.get("Path") or "")
try:
    libs = emby("/Library/VirtualFolders") or []
except Exception:
    libs = []
mine = None
for lb in libs:
    for loc in (lb.get("Locations") or []):
        if path and (path == loc or path.startswith(loc.rstrip("/") + "/")):
            mine = lb
            break
    if mine:
        break
if not mine:
    print(f"  {Y}没找到它属于哪个媒体库{X}  {D}（路径 {path[:48] or '空'}）{X}")
else:
    o = mine.get("LibraryOptions") or {}
    _sec = o.get("MinResumeDurationSeconds")
    _pct = o.get("MinResumePct")
    okk = (_sec is not None and int(_sec) <= MIN_SEC
           and _pct is not None and int(_pct) == 0)
    print(f"  {D}最短续播秒数{X}  "
          + (f"{G}{_sec}{X}" if _sec is not None else f"{Y}读不到{X}")
          + f"  {D}（脚本要的是 ≤ {MIN_SEC}）{X}")
    print(f"  {D}最短续播百分比{X}"
          + (f"  {G}{_pct}{X}" if _pct is not None else f"  {Y}读不到{X}")
          + f"  {D}（脚本要的是 0）{X}")
    if okk:
        print(f"  {G}✔ 门槛是对的{X}  {D}这一项不是原因{X}")
    elif _sec is None and _pct is None:
        print(f"  {Y}⚠ 这个 Emby 版本不给这两个字段 —— 判不了{X}")
    else:
        print(f"  {R}✖ 门槛没设对{X}  {D}默认的 120 秒 / 5% 会让短片永远存不下"
              f"续播点 —— 表现是「长的记得住、短的记不住」。{X}")
        print(f"  {B}修：跑一次「7 更新」{X}{D}（它每小时也会自己对一次）{X}")

# ================================================================ 补时长的队列
print()
print(f"  {B}④⑤ 补时长这边（队列 / 放弃名单 / 今天的额度）{X}")
hr()
st = state()
if st is None:
    # 【读不到就说读不到】这是 root-only 的文件
    print(f"  {Y}读不到 {STATE}{X}  {D}—— 用 sudo 跑这个脚本才看得到"
          f"队列和放弃名单这两项{X}")
else:
    tab = st.get("heal_fail") if isinstance(st.get("heal_fail"), dict) else {}
    v = tab.get(iid)
    if isinstance(v, list) and len(v) >= 2:
        cnt, ts = int(v[0]), float(v[1])
        n_over = max(0, cnt - GIVEUP)
        days = min(GIVEUP_DAYS * (2 ** min(n_over, 20)), GIVEUP_MAX)
        left = days * 86400 - (time.time() - ts)
        if cnt >= GIVEUP and left > 0:
            print(f"  {R}✖ 已经放弃了{X}  {D}连续探了 {cnt} 次都没探到音视频轨，"
                  f"还要等 {left / 86400:.0f} 天才自动再试{X}")
            print(f"  {D}退避是翻倍的（{GIVEUP_DAYS} → {GIVEUP_DAYS * 2} → "
                  f"… → {GIVEUP_MAX} 天封顶）—— 一个永远探不出来的条目一年只碰几次，"
                  f"不然就是每月把整库重探一遍。{X}")
            print(f"  {B}真修好过源头就敲：media-stack heal-reset{X}"
                  f"{D}（清空放弃名单，让它们下一轮重新排队）{X}")
        else:
            print(f"  {D}失败记录：{cnt} 次{X}"
                  + (f"  {G}还没到放弃线（{GIVEUP} 次）{X}" if cnt < GIVEUP
                     else f"  {G}退避已经到期，下一轮会再试{X}"))
    else:
        print(f"  {G}✔ 不在放弃名单里{X}  {D}该轮到它的时候就会探{X}")

    day = st.get("heal_day") or {}
    used = float(day.get("mb") or 0) if day.get("date") == time.strftime("%Y-%m-%d") else 0.0
    if used >= DAY_MB:
        print(f"  {R}✖ 今天的补时长额度用完了{X}  {D}已用约 {used:.0f} MB"
              f"（上限 {DAY_MB} MB）—— 今天不再探，明天零点自动清零{X}")
    else:
        print(f"  {D}今天已用 {used:.0f} MB / 上限 {DAY_MB} MB{X}"
              + (f"  {Y}快到了{X}" if used > DAY_MB * 0.8 else ""))

print()
# 【说清它多久跑一次】"等一会儿"是句废话，人要知道等多久、以及能不能现在就来
print(f"  {D}补时长跟着每小时那条定时任务跑（整点触发），也可以现在就手动来一轮：{X}")
print(f"  {C}media-stack heal{X}  {D}（补到完为止，可随时 Ctrl-C）{X}")

# ================================================================ 结论
print()
print(f"  {B}结论{X}")
hr()
if not mins and seen_any:
    print(f"  {Y}这一条没有时长，所以 Emby 存不住续播点。{X}")
    print(f"  {D}你点开过它，所以它【在】补时长的队列里。等下一个整点，"
          f"或者现在敲一次 media-stack heal。{X}")
    print(f"  {D}补上之后进度条就正常了 —— 补之前已经丢掉的那些进度回不来。{X}")
elif not mins and not seen_any:
    print(f"  {Y}这一条没有时长，而且所有账号都没点开过它。{X}")
    print(f"  {D}补时长只补点开过的，所以它现在不在队列里。点开一次再跑"
          f"media-stack heal 就会补。{X}")
elif mins and nstream:
    print(f"  {G}媒体信息是齐的{X}{D} —— 记不住进度的话，看上面 ③ 的门槛那两项，"
          f"以及这个客户端有没有在退出时上报播放位置（有些播放器直接杀进程就不报）。{X}")
else:
    print(f"  {Y}时长有、音视频轨没有 —— 进度条能记住，但每次点开都要现场再探一次。{X}")
    print(f"  {D}跑一次 media-stack heal 把轨道也补上，开播会快一截。{X}")
PY

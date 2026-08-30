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
# 播放的时候跑这个才有东西看 —— 没在播就什么都查不到。
set -u

DIR="${MS_DIR:-/opt/media-stack}"
KEY="$(sed -nE 's/^[[:space:]]*auth:[[:space:]]*([^[:space:]#]+).*/\1/p' \
        "$DIR/mediawarp/config/config.yaml" 2>/dev/null | head -1)"
[ -n "$KEY" ] || { echo "✖ 读不到 Emby API Key（$DIR/mediawarp/config/config.yaml）"; exit 1; }

python3 - "$KEY" <<'PY'
import json, sys, urllib.request

KEY = sys.argv[1]
BASE = "http://127.0.0.1:8096"
G = "\033[32m"; Y = "\033[33m"; R = "\033[31m"; C = "\033[36m"
D = "\033[2m";  B = "\033[1m"; X = "\033[0m"


def api(path):
    sep = "&" if "?" in path else "?"
    u = f"{BASE}{path}{sep}api_key={KEY}"
    with urllib.request.urlopen(u, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


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
    print(f"  {D}这个脚本要在【正在播放】的时候跑才有东西看 —— "
          f"先在播放器里点开一集，再回来跑一次。{X}")
    sys.exit(0)

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
    if method == "DirectPlay":
        print(f"  {G}✔ 直接播放{X}（DirectPlay）"
              f"{D} —— 视频没经过这台机器，302 是真生效的{X}")
    elif method == "DirectStream":
        print(f"  {Y}⚠ 直接流{X}（DirectStream）"
              f"{D} —— 只换了容器，但视频流【还是经过这台机器】转手{X}")
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

    pos = (ps.get("PositionTicks") or 0) / 1e7
    if pos:
        print(f"  {D}已播到     {hms(pos)}"
              + ("   ⏸ 暂停中" if ps.get("IsPaused") else "") + X)

print()
print(f"  {D}这里说的是【怎么播】，不是【快不快】。要量速度用 ali-403.sh；"
      f"要看 302 有没有发出去用 media-stack 302。{X}")
PY

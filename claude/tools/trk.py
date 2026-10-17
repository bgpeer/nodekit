#!/usr/bin/env python3
"""轨道体检（只读）：整个 strm 库有几个条目探到了音视频轨、Emby 自己的计划任务最近一次什么时候跑完。

用法（VPS 上）：
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/trk.py | python3 - [片名关键字]
带关键字时，再列出名字对得上的条目各有几条轨道。看「Scan media library」那行能知道 Emby 有没有扫过整个库
（扫一次整个库，strm 条目的轨道会被清掉一批）。
"""
import importlib.util, os, sys, time, urllib.parse
S = os.environ.get("MS_SCRIPT") or "/etc/bgpeer/media-stack.py"
spec = importlib.util.spec_from_file_location("ms", S); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m.ms_install_dir()
key = m.read_emby_api_key(d)
if not key:
    sys.exit("没有 Emby API Key（「7 设置 → 1」）")
kw = sys.argv[1] if len(sys.argv) > 1 else ""
print("现在", m.bj_fmt("%m-%d %H:%M"), "（北京）")
uid = (m._emby("/Users", key) or [{}])[0].get("Id", "")
tot = has = 0
hit = []
for lb in m._emby("/Library/VirtualFolders", key) or []:
    if not lb.get("ItemId") or not m.is_strm_lib(lb):
        continue
    r = m._emby(f"/Users/{uid}/Items?ParentId={lb['ItemId']}&Recursive=true"
                f"&IncludeItemTypes=Movie,Episode,Video&Fields=MediaStreams,Path", key) or {}
    for i in r.get("Items") or []:
        n = len(i.get("MediaStreams") or [])
        tot += 1
        has += n > 0
        if kw and kw in str(i.get("Name") or "") + str(i.get("SeriesName") or ""):
            hit.append((str(i.get("SeriesName") or "") + " " + str(i.get("Name") or ""), n))
for name, n in hit[:30]:
    print(f"{name.strip()[:40]}  轨道 {n} 条")
print(f"整个库有轨道的：{has} / {tot}")
print("Emby 自己的计划任务（最近一次结束，北京）：")
rows = []
for t in m._emby("/ScheduledTasks", key) or []:
    end = (t.get("LastExecutionResult") or {}).get("EndTimeUtc") or ""
    ts = 0
    if end:
        try:
            ts = time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            ts = 0
    rows.append((ts, t.get("Name") or "?"))
for ts, name in sorted(rows):
    print("  " + (m.bj_fmt("%m-%d %H:%M", ts) if ts else "从没跑过   ") + "  " + name)

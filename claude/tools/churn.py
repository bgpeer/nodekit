#!/usr/bin/env python3
"""某几部片的 strm 和 Emby 条目对一对（只读）：本地文件什么时候改的、Emby 里的路径本地还在不在。
查「每晚被删了又建」「Emby 里两条一样的」这类问题用。

用法（VPS 上）：
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/churn.py | python3 - 片名1 片名2 ...
"""
import importlib.util, os, sys, json, time, urllib.parse
S = os.environ.get("MS_SCRIPT") or "/etc/bgpeer/media-stack.py"
spec = importlib.util.spec_from_file_location("ms", S); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
KW = tuple(sys.argv[1:])
if not KW:
    sys.exit("要给至少一个片名关键字")
d = m.ms_install_dir()
key = m.read_emby_api_key(d)
base = os.path.join(m.strm_root(d), m.STRM_SUBDIR)
try:
    seen = set(json.load(open(os.path.join(d, m.EMBY_SEEN_FILE), encoding="utf-8")))
except Exception:
    seen = set()
print("== 本地 strm（改动时间　记账里有没有　路径 → 网盘）")
for hp, tgt in sorted(m.strm_inventory(d)):
    if any(k in hp for k in KW):
        st = os.stat(hp)
        print(time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime)),
              "记过" if m._strm_container_path(d, hp) in seen else "没记", os.path.relpath(hp, base))
        print("        →", tgt)
print("== Emby 里的条目（入库时间　本地有没有这个文件　路径）")
uid = (m._emby("/Users", key) or [{}])[0].get("Id", "")
shown = set()
for k in KW:
    r = m._emby(f"/Users/{uid}/Items?Recursive=true&IncludeItemTypes=Movie,Episode&SearchTerm={urllib.parse.quote(k)}"
                f"&Fields=Path,MediaSources,DateCreated", key) or {}
    for i in r.get("Items") or []:
        if i.get("Id") in shown:
            continue
        shown.add(i.get("Id"))
        ps = [str(i.get("Path") or "")] + [str(s.get("Path") or "") for s in (i.get("MediaSources") or [])]
        for j, p in enumerate(dict.fromkeys(ps)):
            hp = m._strm_host_path(d, p)
            print(("  " if j else "") + str(i.get("DateCreated") or "")[5:16].replace("T", " "),
                  "有" if hp and os.path.lexists(hp) else "没有",
                  p.replace(m.STRM_PATH + "/", ""), "" if j else f"（{i.get('Name')}）")

#!/usr/bin/env python3
"""盘和 strm 对账（只读）：OpenList 挂着哪些盘、扫描路径、本地每个盘几个 strm、Emby 里收了几个。

用法（VPS 上）：
  curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/claude/tools/orphan.py | python3 -
本地有、OpenList 里已经没了的盘会标出来；Emby 里有、本地整个主目录都没了的也列出来（撤掉的盘留下的空条目）。
"""
import importlib.util, os, sys, collections
S = os.environ.get("MS_SCRIPT") or "/etc/bgpeer/media-stack.py"
spec = importlib.util.spec_from_file_location("ms", S); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m.ms_install_dir()
key = m.read_emby_api_key(d)
mounts = [mp for mp, *_x in m.openlist_storages(d) if mp]
print("== OpenList 里挂着的盘：", "、".join(mounts) or "（读不到）")
print("== 扫描路径（自己加的）：", "、".join(m.explicit_scan_paths()) or "无")
print("== 交给 AutoFilm 的：", "、".join(m.effective_scan_paths(d)) or "无")
root = os.path.join(m.strm_root(d), m.STRM_SUBDIR)
emby = (m.emby_strm_paths(key) if key else None) or set()
by = collections.Counter(p[len(m.STRM_PATH) + 1:].split("/")[0] for p in emby if p.startswith(m.STRM_PATH + "/"))
local = sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x))) if os.path.isdir(root) else []
print("== 本地 strm 主目录（个数　Emby 里收录　OpenList 里还在不在）")
for x in local:
    n = sum(f.endswith(".strm") for _p, _d, fs in os.walk(os.path.join(root, x)) for f in fs)
    alive = any(mp.strip("/").split("/")[0] == x for mp in mounts)
    print(f"  {x:<20} {n:>6} 个　Emby {by.get(x, 0):>6}　" + ("在" if alive else "【OpenList 里已经没了】"))
gone = [(x, n) for x, n in by.items() if x not in local]
for x, n in gone:
    print(f"  {x:<20}      0 个　Emby {n:>6}　【本地整个没了，Emby 里还挂着】")

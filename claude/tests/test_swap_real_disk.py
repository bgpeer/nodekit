#!/usr/bin/env python3
"""ensure_swap 的 swap 文件要落在真磁盘上，别落 /tmp —— 真机踩过（谷歌 1G 机）：
  /tmp 在新版系统（ubuntu resolute）是 tmpfs（内存盘），往它写 512MB swap：要么写不下、
  要么白占内存、swapon 还被拒 → OOM 兜底从没生效，装 nginx 时 OOM 卡死、reboot 都起不来。
  修法：落 /var（根分区），先排除内存盘、再确认磁盘够放，否则宁可不建（继续运行）。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# —— 公共打桩 ——
orig_fstype = getattr(m, "_path_fstype", None)   # 步骤 6 要用真函数，先存下来（下面会拿桩替换它）
logs, calls = [], []
m.echo = lambda msg="": logs.append(msg)
m.run = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": ""})()
m.os.chmod = lambda *a, **k: None          # dd 被打桩，文件不存在，chmod 不能真跑
m.atexit.register = lambda f: None
def low_mem():                             # 1G 无 swap
    m.meminfo_kb = lambda f: {"MemTotal": 975508, "SwapTotal": 0}.get(f, 0)
def reset():
    logs.clear(); calls.clear()
def dd_target():                           # 取 dd 命令里的 of=xxx
    for c in calls:
        if c and c[0] == "dd":
            return next((a[3:] for a in c if a.startswith("of=")), None)
    return None

# 1. 真磁盘：建在 /var，不是 /tmp
low_mem(); reset()
m._path_fstype = lambda p: "ext4"
m.os.statvfs = lambda p: type("S", (), {"f_bavail": 10**9, "f_frsize": 1})()
m.ensure_swap()
check("swap 落在 /var（不是 /tmp）", dd_target() == "/var/.net-optimize-swap")
check("真磁盘够空间：dd + mkswap + swapon 都跑了",
      any(c[0] == "dd" for c in calls) and any(c[0] == "mkswap" for c in calls) and any(c[0] == "swapon" for c in calls))

# 2. 内存盘（tmpfs）：不建，跳过
low_mem(); reset()
m._path_fstype = lambda p: "tmpfs"
m.ensure_swap()
check("tmpfs 内存盘：不 dd、不建 swap", not any(c[0] == "dd" for c in calls))
check("tmpfs：屏上说明是内存盘跳过", any("内存盘" in x for x in logs))

# 3. overlay 同样算内存盘/不可靠：跳过
low_mem(); reset()
m._path_fstype = lambda p: "overlay"
m.ensure_swap()
check("overlay：也跳过不建", not any(c[0] == "dd" for c in calls))

# 4. 真磁盘但空间不足：跳过
low_mem(); reset()
m._path_fstype = lambda p: "ext4"
m.os.statvfs = lambda p: type("S", (), {"f_bavail": 100 * 1024 * 1024, "f_frsize": 1})()  # 100MB
m.ensure_swap()
check("磁盘空间不足：不 dd、屏上说空间不足", not any(c[0] == "dd" for c in calls) and any("空间不足" in x for x in logs))

# 5. 内存够大 / 已有 swap：直接返回，什么都不做
reset(); m.meminfo_kb = lambda f: {"MemTotal": 4194304, "SwapTotal": 0}.get(f, 0)
m.ensure_swap(); check("内存≥2G：不建 swap", not calls and not logs)
reset(); m.meminfo_kb = lambda f: {"MemTotal": 975508, "SwapTotal": 262144}.get(f, 0)
m.ensure_swap(); check("已有 swap：不重复建", not calls and not logs)

# 6. _path_fstype 最长前缀匹配（还原真函数，前面被桩替换过）
if orig_fstype:
    m._path_fstype = orig_fstype
    m.read_text = lambda p, default="": "/dev/sda1 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n" if p == "/proc/mounts" else default
    check("_path_fstype(/var)=ext4（落到根）", m._path_fstype("/var/.net-optimize-swap") == "ext4")
    check("_path_fstype(/tmp)=tmpfs（命中更长前缀）", m._path_fstype("/tmp/.net-optimize-swap") == "tmpfs")

# 7. 源码层：卸载/reset 新旧两个残留路径都清；版本号到 4.5.10
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
check("reset 同时清 /var 和 /tmp 旧残留", '"/var/.net-optimize-swap", "/tmp/.net-optimize-swap"' in src)
check("VERSION 升到 4.5.10", 'VERSION = "4.5.10"' in src)

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

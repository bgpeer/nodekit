#!/usr/bin/env python3
"""ensure_remote_script 只升不降 —— 真机踩过（谷歌机 net-optimize 一进菜单就被降级）：
  进菜单 12 会"再拉一次最新"覆盖 /etc/bgpeer/net-optimize.py，而 raw 被限流(429)回落
  jsDelivr（@main 缓存约 12h、?_t= 破不掉）→ 拿到旧副本，原来不比版本直接覆盖，把刚 curl 的
  4.5.11 又盖回 4.5.9。修法：拉到的带版本号且比本地旧，就别覆盖，保留本地新版。
"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
m.BGP_DIR = tmp
LOCAL = f"{tmp}/net-optimize.py"
URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/net-optimize.py"
def set_remote(body):
    m.fetch_url = lambda u: body
def localver():
    return m._remote_ver(open(LOCAL, encoding="utf-8").read())

# 1. 拉到旧版（4.5.9 < 本地 4.5.11）→ 不覆盖，保留本地新版
open(LOCAL, "w").write('VERSION = "4.5.11"\nprint(1)\n')
set_remote('VERSION = "4.5.9"\nprint(1)\n')
m.ensure_remote_script(URL, LOCAL)
check("拉到旧版：保留本地 4.5.11，不被降级", localver() == "4.5.11")

# 2. 拉到新版（4.5.13 > 本地 4.5.11）→ 覆盖
open(LOCAL, "w").write('VERSION = "4.5.11"\nprint(1)\n')
set_remote('VERSION = "4.5.13"\nprint(1)\n')
m.ensure_remote_script(URL, LOCAL)
check("拉到新版：升到 4.5.13", localver() == "4.5.13")

# 3. 本地不存在（首次）→ 直接写
os.remove(LOCAL)
set_remote('VERSION = "4.5.13"\nprint(1)\n')
m.ensure_remote_script(URL, LOCAL)
check("首次无本地：写入 4.5.13", localver() == "4.5.13")

# 4. SCRIPT_VERSION（主体脚本那种）同样认，旧的不覆盖
open(LOCAL, "w").write('SCRIPT_VERSION = "1.1.138"\nprint(1)\n')
set_remote('SCRIPT_VERSION = "1.1.100"\nprint(1)\n')
m.ensure_remote_script(URL, LOCAL)
check("SCRIPT_VERSION 旧版也不降级", m._remote_ver(open(LOCAL, encoding="utf-8").read()) == "1.1.138")

# 5. 拉到的没版本号（模板/规则那类）→ 照常覆盖（没法比，保持原有行为）
open(LOCAL, "w").write('VERSION = "4.5.11"\nprint(1)\n')
set_remote('print("no version here")\n')
m.ensure_remote_script(URL, LOCAL)
check("拉到的无版本号：照常覆盖", open(LOCAL).read().strip() == 'print("no version here")')

# 6. 拉到的语法坏 → 不覆盖（原有防半截文件行为）
open(LOCAL, "w").write('VERSION = "4.5.11"\nprint(1)\n')
set_remote('VERSION = "4.5.13"\ndef broken(:\n')
m.ensure_remote_script(URL, LOCAL)
check("语法坏：不覆盖，保留本地", localver() == "4.5.11")

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

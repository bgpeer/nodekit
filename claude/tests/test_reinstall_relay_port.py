#!/usr/bin/env python3
"""重装后中转跟着换、菜单显示新端口 —— 仓库主人 10/20：删光节点重装，订阅换了新端口，
  「14 GitHub 中转」还显示旧端口和老 token，让人误会。
  ① 重装是后台进程跑的、新端口写进文件，前台菜单缓存着旧端口 → sub_port() 每次先读文件。
  ② 重装（build_subscription new_token=True）中转 token 一起换，而且在生成配置之前换（配置里规则 / 图标走中转）。
  平时刷新配置（new_token=False）中转 token 和端口都不动。
"""
import os, sys, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.BGP_DIR = tmp; m.SUBPORT_FILE = f"{tmp}/sub.port"; m.GHRELAY_TOKEN_FILE = f"{tmp}/ghrelay.token"; m.HOST_FILE = f"{tmp}/sub.host"
m.harden_perms = lambda: None

# ① 前台缓存着旧端口，后台改了文件 → 读到新的
open(m.SUBPORT_FILE, "w").write("25380"); m._SUB_PORT = None
check("先读到 25380", m.sub_port() == 25380)
open(m.SUBPORT_FILE, "w").write("37235")                 # 模拟后台重装换了端口
check(f"后台换成 37235 后，前台再读就是新的（{m.sub_port()}）", m.sub_port() == 37235)

# ② 重装换中转 token，且生成配置时已经是新 token；平时刷新不换
open(m.GHRELAY_TOKEN_FILE, "w").write("OLDTOK")
seen = []
m.aggregated_links = lambda l: l
m.parse_nodes = lambda l: (["x"], [("k", {})])
m.FMT = {"yaml": {"gen": lambda *a: seen.append((m._ghrelay_token(), m.sub_port())), "label": "mihomo"}}
m.tpl_url_current = lambda e: ""
m.serve_sub = lambda **k: None
m._pick_sub_port = lambda: 41000
m.build_subscription(["l"], new_token=True)
check(f"重装：生成配置时中转 token 已换新、端口已换新（{seen[-1]}）", seen[-1][0] != "OLDTOK" and seen[-1][1] == 41000)
tok = m._ghrelay_token()
m.build_subscription(["l"], new_token=False)
check("平时刷新配置：中转 token、端口都不动", m._ghrelay_token() == tok and m.sub_port() == 41000 and seen[-1] == (tok, 41000))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

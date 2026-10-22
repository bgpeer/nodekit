#!/usr/bin/env python3
"""「只添加新协议」问伪装域名时显示当前在用的 —— 仓库主人 10/20：在「4 更换伪装域名」换过，
  加协议时问的还是第一次安装的老域名。原因：4 只改了节点配置，没回写安装记录 state.json，加协议读的是记录。
  修法：4 换完回写记录；加协议以节点正在用的为准（老机器记录已经是旧的也能对上）；提示里写明可以输入别的域名。
"""
import os, sys, json, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.SB_DIR = f"{tmp}/sb"; os.makedirs(m.SB_DIR); m.SB_BIN = f"{tmp}/no-sing-box"
m.XRAY_DIR = f"{tmp}/xray"; m.STATE_FILE = f"{tmp}/state.json"
def sb(sni):
    json.dump({"inbounds": [{"type": "vless", "tag": "r", "tls": {"server_name": sni, "reality": {"handshake": {"server": sni}}}}]},
              open(f"{m.SB_DIR}/config.json", "w"))
json.dump({"sni": "old.example.com", "prefix": "P"}, open(m.STATE_FILE, "w"))

# 1. 老机器：记录是旧的、节点已换成新的 → 加协议以节点为准
sb("new.example.com")
m._restore_state_to_G(json.load(open(m.STATE_FILE)))
check(f"记录旧、节点新：加协议沿用节点在用的（{m.G['sni']}）", m.G["sni"] == "new.example.com")

# 2. 「4 更换伪装域名」换完回写记录
for f in ("_nginx_split_set_sni", "_links_set_sni", "build_subscription", "restart_services", "_xr_pq_sync"):
    setattr(m, f, lambda *a, **k: False)
m._host = lambda: "h.example.com"
ok = m.change_sni_apply("www.third.example", "sb")
check(f"换伪装域名成功，节点配置改了（{m._current_sni()}）", ok and m._current_sni() == "www.third.example")
check(f"安装记录跟着换（{json.load(open(m.STATE_FILE))}）", json.load(open(m.STATE_FILE)).get("sni") == "www.third.example"
      and json.load(open(m.STATE_FILE)).get("prefix") == "P")

# 3. sing-box 已有 reality → 一个核心一个伪装域名，加协议沿用、不再问
asked = []
m._ask = lambda q: (asked.append(q), "x.example")[1]
m._sni_precheck_one = lambda *a, **k: None
m._asks_reality("add")
check(f"sing-box 已有 reality：沿用它那个、不问（{m.G['sni']}）", not asked and m.G["sni"] == "www.third.example")

# 4. sing-box 还没有 reality → 问；显示当前的、可输入别的；大写 / 末尾点归一
os.remove(f"{m.SB_DIR}/config.json")
m.G["sni"] = "www.third.example"; asked = []
m._ask = lambda q: (asked.append(q), " WWW.Other.Example. ")[1]
m._asks_reality("add")
check(f"提示写明回车沿用当前的（{asked[0].strip()}）", "回车沿用当前的 www.third.example" in asked[0])
check(f"输入的域名归一成小写、去掉末尾点（{m.G['sni']}）", m.G["sni"] == "www.other.example")
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

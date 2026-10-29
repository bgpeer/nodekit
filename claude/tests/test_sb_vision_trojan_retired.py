#!/usr/bin/env python3
"""停用 sing-box 的 vless-vision、trojan（仓库主人 10/29：「我觉得可以把vless-vision删掉…trojan只留在一个核心上」，
  问留哪个核心时选了 xray；看过删除清单后「好的就这么办」）。两台机器上这几个先被单独封端口，DMIT 后来整个 IP 的 TCP 被封。
  做法同 10/23 停用 sing-box reality-grpc：代码不删，挪进 SB 表下面的「备用」注释，builder 还在。
  老机器：节点照常跑、「删除协议」能删、「还没装的」不出负数、「只添加新协议」不再列；命令行 --sb 说清楚改用什么。
"""
import os, sys, io, json, tempfile, contextlib, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("sing-box 表里没有 vless-vision、trojan", "vless-vision" not in m.SB and "trojan" not in m.SB)
check("xray 的 trojan 还在、reality-vision 两边都在", "trojan" in m.XRAY and "reality-vision" in m.SB and "reality-vision" in m.XRAY)
check("builder 还在、备用注释里写着怎么加回",
      callable(m.sb_vless_vision) and callable(m.sb_trojan)
      and '#   "vless-vision":      PROTO(sb_vless_vision' in src and '#   "trojan":            PROTO(sb_trojan' in src)

tmp = tempfile.mkdtemp()
m.STATE_FILE = f"{tmp}/state.json"
json.dump({"sb": list(m.SB) + ["vless-vision", "trojan"], "xray": ["trojan"]}, open(m.STATE_FILE, "w"))
m.read_saved_links = lambda: ["x"]
m.public_ip = lambda: "127.0.0.1"
m._ask = lambda p="": "0"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.install_flow()
check(f"「还没装的」不出负数（{[l for l in buf.getvalue().splitlines() if '还没装的' in l]}）", "sing-box 0 个" in buf.getvalue())
st, have_sb, have_xr = m._installed_state()
seen = {}
m._pick = lambda title, opts, default=None: seen.setdefault(title, list(opts)) and []
m._stale_protos = lambda a, b: ([], [])
have_sb2 = [n for n in have_sb if n != "vless-ws"]
with contextlib.redirect_stdout(io.StringIO()):
    m.add_protocols_flow(st, have_sb2, ["trojan"])
sbopts = next((v for k, v in seen.items() if "sing-box" in k), None)
check(f"「只添加新协议」不列 sing-box 的 vless-vision / trojan（{sbopts}）", sbopts == ["vless-ws"])
check("「删除协议」里照样列出它们（按记录列）", "vless-vision" in have_sb and "trojan" in have_sb)
check("删它们时查注册表不崩", m._proto_fns("teardown", ["vless-vision", "trojan"], []) == [])
for name, want in (("vless-vision", "reality-vision"), ("trojan", "--xray trojan")):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "xy-installer.py"), "--sb", name],
                       capture_output=True, text=True, timeout=60)
    out = (r.stderr + r.stdout).strip()
    check(f"命令行 --sb {name}：说清楚改用什么（{out.splitlines()[-1:]}）", "已停用" in out and want in out)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

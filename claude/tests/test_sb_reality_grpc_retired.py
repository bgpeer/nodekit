#!/usr/bin/env python3
"""停用 sing-box 的 reality-grpc（仓库主人 10/23 点头「删」）：随机端口的 grpc 两台机器都被阻断；grpc 有 grpc-tls（nginx 443）
  和 xray 的 reality-grpc（reality 入口 443）。代码不删，挪进 SB 表下面的「备用」注释，builder 还在，以后改一行就能加回。
  老机器：节点照常跑、「删除协议」能删、「还没装的」不出负数、「只添加新协议」不再列它；命令行 --sb reality-grpc 说清楚改用什么。
  会话里用上一版装一台（带 reality-grpc¹）、换新脚本，真起服务走了一遍：换完全通、删它干净、xray 的还在、删 / 加别的照常。
"""
import os, sys, io, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("sing-box 表里没有 reality-grpc，xray 的还在", "reality-grpc" not in m.SB and "reality-grpc" in m.XRAY)
check("builder 还在、备用注释里写着怎么加回", callable(getattr(m, "sb_reality_grpc", None))
      and '#   "reality-grpc":      PROTO(sb_reality_grpc' in src)
check("grpc 还有两条路：sing-box grpc-tls / xray reality-grpc", "grpc-tls" in m.SB and "reality-grpc" in m.XRAY)

# 老机器：记录里有它
tmp = tempfile.mkdtemp()
m.STATE_FILE = f"{tmp}/state.json"
json.dump({"sb": list(m.SB) + ["reality-grpc"], "xray": ["reality-grpc"]}, open(m.STATE_FILE, "w"))
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
have_sb2 = [n for n in have_sb if n != "vless-ws"]                 # 留一个没装的，让 sing-box 那栏真的列出来
with contextlib.redirect_stdout(io.StringIO()):
    m.add_protocols_flow(st, have_sb2 + ["reality-grpc"], [])
sbopts = next((v for k, v in seen.items() if "sing-box" in k), None)
check(f"「只添加新协议」不列 sing-box 的 reality-grpc（{sbopts}）", sbopts == ["vless-ws"])
check("「删除协议」里照样列出它（按记录列）", "reality-grpc" in have_sb)
fns = m._proto_fns("teardown", ["reality-grpc"], [])
check("删它时查注册表不崩（没有 teardown）", fns == [])
import subprocess
r = subprocess.run([sys.executable, os.path.join(ROOT, "xy-installer.py"), "--sb", "reality-grpc"],
                   capture_output=True, text=True, timeout=60)
buf = io.StringIO(r.stderr + r.stdout)
check(f"命令行 --sb reality-grpc：说清楚改用什么（{buf.getvalue().strip().splitlines()[-1:]}）",
      "已停用" in buf.getvalue() and "grpc-tls" in buf.getvalue())
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

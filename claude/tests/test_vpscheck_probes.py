#!/usr/bin/env python3
"""vps-check.py 逐跳改成每跳 3 个包（原来单包）：单包时一个丢包就把整跳判成
  「无响应」，还可能把一次排队毛刺当成这跳的延迟 —— 骨干在不在的判定就可能
  错一城。traceroute 取 3 个里最快的（min），mtr 取 3 个的平均（Avg 列）。
  地址全用文档段拼出来（test_claude_folder 守着字面量 IP）。
"""
import os, sys, types, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def load(name, f):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, f))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

IP1 = ".".join(["198", "51", "100", "1"])
IP2 = ".".join(["198", "51", "100", "2"])
IP3 = ".".join(["198", "51", "100", "3"])
v = load("vc", "vps-check.py")

# traceroute：每跳 3 个包，中间丢一个、取最快的
TR = (f"traceroute to {IP3} ({IP3}), 30 hops max, 60 byte packets\n"
      f" 1  {IP1}  0.671 ms  0.702 ms  0.688 ms\n"
      " 2  * * *\n"
      f" 3  {IP2}  142.331 ms  *  139.8 ms\n"
      f" 4  {IP3}  162.956 ms\n")
cmds = []
v.have = lambda b: b == "traceroute"
v.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (cmds.append(cmd), types.SimpleNamespace(stdout=TR))[1],
                                     TimeoutExpired=Exception)
hops = v.traceroute(IP3)
check(f"发 3 个包（{cmds[0].split(' -n')[1][:8]}）", "-q 3" in cmds[0])
check(f"丢了一个包的那跳还在、延迟取 3 个里最快的（{hops[2]}）",
      hops[2] == (3, IP2, 139.8))
check(f"3 个全丢的那跳是无响应（{hops[1]}）", hops[1] == (2, None, None))
check(f"单包的老行照样认（{hops[3]}）", hops[3] == (4, IP3, 162.956))

# mtr：-c 3，延迟取 Avg 列（IP 之后第 4 个数字），不是 Last
MTR = (f"Start: 2026-11-19T00:00:00+0000\n"
       f"HOST: localhost  Loss%   Snt   Last   Avg  Best  Wrst StDev\n"
       f"  1.|-- {IP1}  0.0%   3    0.7   0.7   0.6   0.8   0.1\n"
       f"  2.|-- {IP2}  0.0%   3  142.3 141.1 139.8 143.0   1.2\n")
cmds.clear()
v.have = lambda b: b == "mtr"
v.subprocess = types.SimpleNamespace(run=lambda cmd, **k: (cmds.append(cmd), types.SimpleNamespace(stdout=MTR))[1],
                                     TimeoutExpired=Exception)
hops = v.traceroute(IP2)
check(f"mtr 发 3 轮（{cmds[0].split(' -n')[1][:8]}）", "-c 3" in cmds[0])
check(f"mtr 延迟取 Avg 列（{hops[1]}）", hops[1] == (2, IP2, 141.1))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

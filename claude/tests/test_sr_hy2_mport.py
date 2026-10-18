#!/usr/bin/env python3
"""小火箭 hy2 端口跳跃 —— 仓库主人 10/18：「hy2 加上跳跃啊」（原来只写跳跃段起点端口，等于没跳）。
  配置行加 mport=起-止（小火箭分享链接认的字段名）；起点端口照写，小火箭不认 mport 也能连（服务端 DNAT 到真端口）。
  没开跳跃的 hy2 不写 mport。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
hop = m.shadowrocket_line("h", {"type": "hysteria2", "server": "h.example.com", "ports": "30000-31000", "password": "pw",
                                "sni": "h.example.com", "obfs": "salamander", "obfs-password": "op"})
one = m.shadowrocket_line("g", {"type": "hysteria2", "server": "h.example.com", "port": 27000, "password": "pw", "sni": "h.example.com"})
check(f"开了跳跃：起点端口照写 + mport=30000-31000（{hop}）", hop.startswith("h = hysteria2,h.example.com,30000,") and ",mport=30000-31000" in hop)
check("混淆密码照旧在", ",obfsParam=op" in hop)
check(f"没开跳跃：不写 mport（{one}）", "mport" not in one and ",27000," in one)
# 分享链接 mport → 节点 ports → 小火箭行 mport，整条链路
ylines, nodes = m.parse_nodes(["hysteria2://pw@h.example.com:28000?sni=h.example.com&obfs=salamander&obfs-password=op&mport=28000-29000&insecure=0#hy2"])
d = dict(nodes)[next(iter(dict(nodes)))] if isinstance(nodes, list) else None
line = m.shadowrocket_line("x", d) if d else ""
check(f"分享链接带 mport → 小火箭行带同一段（{line}）", ",mport=28000-29000" in line)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

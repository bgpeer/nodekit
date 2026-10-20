#!/usr/bin/env python3
"""mihomo 订阅的 vless 节点带 packet-encoding: xudp —— 对齐 mack-a v3.5.26（仓库主人 10/20：「加」）。
  sing-box 订阅本来就是 xudp、小火箭默认 xudp，只有 mihomo 缺；所有 vless 都加（跟 sing-box 订阅一致），别的协议不加。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
at = "@"
links = ["vless://u" + at + "h.example.com:443?encryption=none&security=reality&sni=www.cisco.com&pbk=K&sid=s&type=tcp&flow=xtls-rprx-vision#rv",
         "vless://u" + at + "h.example.com:443?encryption=none&security=tls&sni=h.example.com&type=ws&host=h.example.com&path=%2Fw#vw",
         "trojan://pw" + at + "h.example.com:443?security=tls&sni=h.example.com&type=tcp#tj"]
ylines, nodes = m.parse_nodes(links)
D = {d["name"]: d for _, d in nodes}
for n in ("rv", "vw"):
    d = next(v for k, v in D.items() if k.endswith(n))
    check(f"vless {n}：packet-encoding: xudp", d.get("packet-encoding") == "xudp")
tj = next(v for k, v in D.items() if k.endswith("tj"))
check("trojan 不加", "packet-encoding" not in tj)
txt = "\n".join(ylines)
check("写进 yaml 的节点行里有 packet-encoding: xudp", "packet-encoding" in txt and "xudp" in txt)
sb = m.mihomo_to_sb_outbound("reality-vision", next(v for k, v in D.items() if k.endswith("rv")))
check("sing-box 订阅照旧 packet_encoding xudp", sb and sb.get("packet_encoding") == "xudp")
sr = m.shadowrocket_line("rv", next(v for k, v in D.items() if k.endswith("rv")))
check("小火箭行不受影响（不多出参数）", "packet" not in sr and "xudp" not in sr)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

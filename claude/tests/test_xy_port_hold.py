#!/usr/bin/env python3
"""端口分配竞态（问题 7）：探一下就放开（TOCTOU）、hy2/tuic 的 UDP 没探、
  _USED_PORTS 只在进程内、CLI --hy2-ports 跳过冲突检查。

  修法：挑到的端口当场绑住不放（hold_port），直到服务起来才松开；
  UDP 协议按 UDP 占位（next_port(proto) / port_free(proto)）；
  CLI 的 --hy2-ports 走跟交互一样的格式 + UDP 冲突检查（见 test_xy_cli_validate.py）。
"""
import os, sys, socket, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def free_tcp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def can_tcp_bind(p):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", p)); s.close(); return True
    except OSError:
        return False

def can_udp_bind(p):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", p)); s.close(); return True
    except OSError:
        return False

# ---- 1. port_free：TCP 占着报占用，UDP 占着 TCP 探不出来、UDP 探才报 ----
p = free_tcp_port()
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(("0.0.0.0", p))
check("TCP 端口被占时 port_free 报占用", not m.port_free(p))
check("TCP 端口被占时 UDP 探照样报空闲（协议不同）", m.port_free(p, "udp"))
s.close()
check("TCP 端口放开后 port_free 报空闲", m.port_free(p))

p = free_udp_port()
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.bind(("0.0.0.0", p))
check("UDP 端口被占时 TCP 探报空闲（以前的坑）", m.port_free(p))
check("UDP 端口被占时 UDP 探报占用", not m.port_free(p, "udp"))
u.close()
check("UDP 端口放开后 UDP 探报空闲", m.port_free(p, "udp"))

# ---- 2. hold_port：占住不放，别的进程绑不上；松开才能绑 ----
p = free_tcp_port()
check("hold_port 占住 TCP 端口", m.hold_port(p, "tcp"))
check("占住期间别人 TCP 绑不上", not can_tcp_bind(p))
check("占住期间 UDP 不受影响（协议不同）", can_udp_bind(p))
m.release_held_ports()
check("松开后 TCP 能绑了", can_tcp_bind(p))

p = free_udp_port()
check("hold_port 占住 UDP 端口", m.hold_port(p, "udp"))
check("占住期间别人 UDP 绑不上", not can_udp_bind(p))
check("占住期间 TCP 不受影响（协议不同）", can_tcp_bind(p))
m.release_held_ports()
check("松开后 UDP 能绑了", can_udp_bind(p))

# ---- 3. next_port：挑到的 UDP 端口一直占着，直到松开 ----
tmp = tempfile.mkdtemp()
m.SUBPORT_FILE = f"{tmp}/sub.port"
open(m.SUBPORT_FILE, "w").write("23456")
m.G["hy2_ports"] = ""
p = m.next_port("udp")
check("next_port(udp) 挑的端口在范围内", 15000 <= p <= 45000)
check("next_port(udp) 挑的端口已登记", p in m._USED_PORTS)
check("next_port(udp) 挑完 UDP 还占着", not can_udp_bind(p))
m.release_held_ports()
check("松开后 UDP 端口回到空闲", can_udp_bind(p))

# ---- 4. build()：UDP 协议按 UDP 占位，TCP 协议按 TCP 占位 ----
def fake_build(port, tag):
    return ({"type": "x", "tag": tag, "listen": "::", "listen_port": port}, "link")
table = {"hy2": {"build": fake_build, "udp": True},
         "vless-ws": {"build": fake_build, "udp": False}}
m.G["prefix"] = ""
ins, _ = m.build(table, ["hy2", "vless-ws"])
hy2_p = ins[0]["listen_port"]; ws_p = ins[1]["listen_port"]
check("hy2 建完 UDP 端口占着", not can_udp_bind(hy2_p))
check("hy2 建完 TCP 不受影响", can_tcp_bind(hy2_p))
check("vless-ws 建完 TCP 端口占着", not can_tcp_bind(ws_p))
check("vless-ws 建完 UDP 不受影响", can_udp_bind(ws_p))
m._release_ports_for(ins)
check("服务起来前松开：hy2 的 UDP 能绑了", can_udp_bind(hy2_p))
check("服务起来前松开：ws 的 TCP 能绑了", can_tcp_bind(ws_p))

# ---- 5. _pick_sub_port：挑到的订阅端口也占住 ----
m._USED_PORTS.clear()
m.SUBPORT_FILE = f"{tmp}/sub2.port"
p = m._pick_sub_port()
check("订阅端口挑完 TCP 占着", not can_tcp_bind(p))
m.release_held_ports()
check("松开后订阅端口回到空闲", can_tcp_bind(p))

# ---- 6. _hop_conflicts：只认 UDP 协议，TCP 协议不算 ----
m.SB_DIR = f"{tmp}/sb"; os.makedirs(m.SB_DIR)
m.XRAY_DIR = f"{tmp}/xray"; os.makedirs(m.XRAY_DIR)
# 注意：_CORE_META 里的配置路径是导入时算死的，只改 SB_DIR/XRAY_DIR 够不着它
m._CORE_META = {"sb": ("sing-box", m.SB_BIN, f"{tmp}/sb/config.json", m.SB, "¹"),
                "xray": ("xray", m.XRAY_BIN, f"{tmp}/xray/config.json", m.XRAY, "²")}
import json
json.dump({"inbounds": [
    {"type": "hysteria2", "tag": "hy2", "listen": "::", "listen_port": 30500},
    {"type": "vless", "tag": "ws", "listen": "::", "listen_port": 30600}]},
    open(f"{m.SB_DIR}/config.json", "w"))
conf = m._hop_conflicts("30000-31000", include_own=True)
ports = [p for p, _ in conf]
check("跳跃段里的 UDP 协议（hy2）被认出来", 30500 in ports)
check("跳跃段里的 TCP 协议不算冲突", 30600 not in ports)
check("跳跃段外不报", 30500 not in [p for p, _ in m._hop_conflicts("40000-41000", True)])

# ---- 7. ask_hy2_range：格式不对重填，不再静默回默认 ----
answers = iter(["bad-format", "40000-41000"])
m._ask = lambda q: next(answers)
check("格式不对重填、合法就收下", m.ask_hy2_range("", include_own=True) == "40000-41000")
answers = iter(["30000-31000", "0"])
m._ask = lambda q: next(answers)
check("撞上 UDP 服务、选 0 就关跳跃", m.ask_hy2_range("", include_own=True) == "n")

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

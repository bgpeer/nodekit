#!/usr/bin/env python3
"""detect_outbound_iface 兜底 —— 真机踩过（谷歌 1G 机）：只靠 `ip route get` / `ip route show default`
  解析出口网卡，GCP 上一个都没出来（OOM 时 ip 起不来，或策略路由把默认路由挪出 main 表），
  结果 offload / RPS-RFS / MSS / 自适应 QoS 全因"无法检测出口网卡"整步跳过。
  修法：再加两道不依赖子进程的兜底——读 /proc/net/route（纯文件、OOM 也能用、拿真默认路由），
  再不行从 /sys/class/net 挑一张 UP 的真网卡；只有彻底没网卡才返回空。
"""
import os, re, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def mkrun(table):     # table: {"ip -4 route get 1.1.1.1": stdout, ...}
    def _run(cmd, **kw):
        return type("R", (), {"stdout": table.get(" ".join(cmd), ""), "returncode": 0})()
    return _run
def mkread(files):    # files: {path: content}
    return lambda p, default="": files.get(p, default)

PROC_ROUTE = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
              "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"   # 非默认（docker 子网）
              "ens4\t00000000\t0138A8C0\t0003\t0\t0\t100\t00000000\n")   # 默认路由

# 1. 正常：ip -4 route get 直接拿到 dev（示例里不写真 IP，避免 claude-folder 误判泄漏）
m.run = mkrun({"ip -4 route get 1.1.1.1":
               "1.1.1.1 via _gw_ dev ens4 src _src_ uid 0\n    cache"})
check("ip route get 正常：拿到 ens4", m.detect_outbound_iface() == "ens4")

# 2. ip 全空（OOM / 策略路由）→ 读 /proc/net/route 的默认路由，且挑 Destination=00000000 那行（跳过 docker0）
m.run = mkrun({})
m.read_text = mkread({"/proc/net/route": PROC_ROUTE})
check("ip 全空：从 /proc/net/route 的默认路由拿到 ens4（不是 docker0）",
      m.detect_outbound_iface() == "ens4")

# 3. ip 空 + /proc/net/route 没有默认路由 → 从 /sys/class/net 挑 UP 的真网卡
m.run = mkrun({})
m.read_text = mkread({"/proc/net/route": "Iface\tDestination\t...\n",   # 只有表头
                      "/sys/class/net/ens4/operstate": "up\n",
                      "/sys/class/net/eth0/operstate": "down\n"})
m.os.listdir = lambda p: ["lo", "eth0", "docker0", "tun0", "ens4"]
check("都没有：挑一张 UP 的真网卡 ens4", m.detect_outbound_iface() == "ens4")

# 4. 最后一招排除 lo / 虚拟 / 隧道网卡
m.run = mkrun({})
m.read_text = mkread({"/proc/net/route": "Iface\tDestination\n",
                      "/sys/class/net/tun0/operstate": "up\n",
                      "/sys/class/net/veth9/operstate": "up\n",
                      "/sys/class/net/lo/operstate": "unknown\n"})
m.os.listdir = lambda p: ["lo", "tun0", "veth9"]
check("只剩 lo/tun/veth：不乱挑，返回空", m.detect_outbound_iface() == "")

# 5. operstate 为 unknown 的真网卡也算 UP（云镜像常见）
m.run = mkrun({})
m.read_text = mkread({"/proc/net/route": "Iface\tDestination\n",
                      "/sys/class/net/ens4/operstate": "unknown\n"})
m.os.listdir = lambda p: ["lo", "ens4"]
check("operstate=unknown 的 ens4 也认", m.detect_outbound_iface() == "ens4")

# 6. 源码层：版本号 ≥ 4.5.11（本次引入这些兜底）
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
mv = tuple(int(x) for x in re.findall(r"\d+", re.search(r'VERSION = "([\d.]+)"', src).group(1)))
check("VERSION ≥ 4.5.11", mv >= (4, 5, 11))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

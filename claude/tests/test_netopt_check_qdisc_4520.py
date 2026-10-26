#!/usr/bin/env python3
"""net-optimize 4.5.20 —— 仓库主人 10/26 贴了一台 1 核 1G 的「网络优化状况」问「现在网络优化做到极致了吗」：
  · 「当前队列（eth0）：mq」看不出 fq 在不在跑：mq 只是多队列网卡的总管，真干活的是每个队列下面挂的那个。
    现在显示「mq（每个队列 fq，共 2 个）」；ingress / clsact 不是它的子，不算。
  · [3] 以前按档位想当然写「出口用 fq 按流公平」，现在看网卡上实际挂的：不是 fq 就照实说，
    内核没编进 fq 的说要换内核，其余的说再跑一次就换上（同一个 _fq_fix）。
  · 老 bash 版的三个遗留脚本（--check 一直提示「可删除」）：跑优化时没人调用就清掉，有人调用的不动。
"""
import os, re, sys, io, contextlib, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "net-optimize.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
tmp = tempfile.mkdtemp()
quiet = lambda: contextlib.redirect_stdout(io.StringIO())

class R:
    def __init__(self, rc=1, out=""): self.returncode, self.stdout = rc, out

MQ_FQ = ("qdisc mq 0: root \n"
         "qdisc fq 0: parent :2 limit 10000p flow_limit 100p buckets 1024 orphan_mask 1023 quantum 3028b\n"
         "qdisc fq 0: parent :1 limit 10000p flow_limit 100p buckets 1024 orphan_mask 1023 quantum 3028b\n"
         "qdisc ingress ffff: parent ffff:fff1 ---------------- \n")
MQ_CODEL = ("qdisc mq 8001: root \n"
            "qdisc fq_codel 0: parent 8001:2 limit 10240p flows 1024 quantum 1514\n"
            "qdisc fq_codel 0: parent 8001:1 limit 10240p flows 1024 quantum 1514\n")
CAKE = "qdisc cake 8001: root refcnt 2 bandwidth unlimited diffserv4 triple-isolate nat nowash\n"
FQ1 = "qdisc fq 8002: root refcnt 2 limit 10000p flow_limit 100p buckets 1024\n"

def with_tc(n, text):
    n.run = lambda cmd, **k: R(0, text) if cmd[:3] == ["tc", "qdisc", "show"] else R(1, "")

# ── 1. 网卡上实际挂的 ───────────────────────────────────────────────────────────
n = load("n1")
for text, want, label in ((MQ_FQ, ("mq", ["fq", "fq"]), "mq（每个队列 fq，共 2 个）"),
                          (MQ_CODEL, ("mq", ["fq_codel", "fq_codel"]), "mq（每个队列 fq_codel，共 2 个）"),
                          (CAKE, ("cake", []), "cake"), (FQ1, ("fq", []), "fq"), ("", ("", []), "")):
    with_tc(n, text)
    got = n.iface_qdiscs("eth0")
    check(f"iface_qdiscs → {got}，屏上显示「{n.iface_qdisc_label('eth0')}」", got == want and n.iface_qdisc_label("eth0") == label)
with_tc(n, MQ_FQ)
check("ingress 不算 mq 的子（样本里有一条 ingress，子还是 2 个 fq）", n.iface_qdiscs("eth0")[1] == ["fq", "fq"])
check("守护进程那边比对用的 iface_root_qdisc 照旧只回根（别把它的判断搞坏）", n.iface_root_qdisc("eth0") == "mq")

# ── 2. --check 整屏跑一遍：[1] 显示 mq 下面挂的；[3] 看实际挂的 ───────────────────────
def check_screen(tc_text, cfg_text="CONFIG_NET_SCH_FQ=m\n", default_qdisc="fq"):
    n = load(f"nc{len(tc_text)}{len(cfg_text)}")
    def run(cmd, **k):
        if cmd[:3] == ["tc", "qdisc", "show"]:
            return R(0, tc_text)
        return R(1, "")
    n.run = run
    n.detect_outbound_iface = lambda: "eth0"
    n.check_ipt_backend = lambda: ""
    n.config_read = lambda: {"ADAPTIVE_QOS_MODE": "adaptive", "ADAPTIVE_DAEMON": "0", "GAME_QOS_SCHEME": "none"}
    n.get_sysctl = lambda k: {"net.ipv4.tcp_congestion_control": "bbr", "net.core.default_qdisc": default_qdisc}.get(k, "")
    n.have_cmd = lambda c: c == "tc"
    real_rt = n.read_text
    n.read_text = lambda p, d="": cfg_text if p.startswith("/boot/config-") else real_rt(p, d)
    n._uname = lambda: type("U", (), {"release": "5.15.0-1109-kvm"})()
    n.container_kind = lambda: ""
    n.NETPROFILE = f"{tmp}/none.json"
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            n.cmd_check()
    except Exception as e:      # 整屏有些项读真机文件，跑不完也要能看到前面那几段
        out.write(f"\n[cmd_check 中途异常：{type(e).__name__}: {e}]")
    return out.getvalue()

o = check_screen(MQ_FQ)
line1 = next((l for l in o.splitlines() if "当前队列（eth0）" in l), "")
check(f"[1] 多队列网卡显示 mq 下面挂的（{line1.strip()}）", "mq（每个队列 fq，共 2 个）" in line1)
check("[3] 实际挂的是 fq：✅ 出口用 fq", "✅ 出口用 fq 按流公平" in o and "出口实际是" not in o)
o = check_screen(MQ_CODEL)
w = next((l for l in o.splitlines() if "出口实际是" in l), "")
check(f"[3] 实际挂的不是 fq：⚠ 照实说、叫重跑（{w.strip()}）",
      "mq（每个队列 fq_codel，共 2 个），不是 fq" in w and "再跑一次「12 网络优化」就换上" in w and "✅ 出口用 fq" not in o)
o = check_screen(MQ_CODEL, cfg_text="# CONFIG_NET_SCH_FQ is not set\n", default_qdisc="fq_codel")
w = next((l for l in o.splitlines() if "出口实际是" in l), "")
check(f"[3] 内核没编进 fq：说要换内核，不叫人重跑（{w.strip()}）", "要用得换内核" in w and "再跑一次" not in w)
hint = next((l for l in o.splitlines() if "提示：默认队列是" in l), "")
check(f"[1] 提示同一个说法（{hint.strip()}）", "要用得换内核" in hint and "再跑一次" not in hint)
nk = load("nct")
nk.container_kind = lambda: "lxc"
check(f"容器小鸡：说队列改不了，不叫人重跑（{nk._fq_fix()}）", "容器" in nk._fq_fix() and "再跑一次" not in nk._fq_fix())
check("[2] 网卡那段的 tc qdisc 也显示 mq 下面挂的", re.search(r'tcq = iface_qdisc_label\(out_iface\)', src) is not None)

# ── 3. 老 bash 版遗留：没人调用就清 ─────────────────────────────────────────────
n = load("nleg")
L = [f"{tmp}/net-optimize-apply", f"{tmp}/net-optimize-adaptive-qos", f"{tmp}/net-optimize-ultimate.sh"]
for f in L:
    open(f, "w").write("#!/bin/bash\n")
os.makedirs(f"{tmp}/units", exist_ok=True)
open(f"{tmp}/units/old-qos.service", "w").write(f"[Service]\nExecStart={L[1]}\n")
n.LEGACY_BASH = tuple(L)
n.LEGACY_REFS = (f"{tmp}/units/*",)
n.run = lambda cmd, **k: R(0, f"*/5 * * * * {L[2]} >/dev/null\n") if cmd[:2] == ["crontab", "-l"] else R(1, "")
with quiet() as o:
    n.clean_legacy_bash()
check("没人调用的删了", not os.path.exists(L[0]))
check("systemd 单元里还写着的不动", os.path.exists(L[1]))
check("crontab 里还写着的不动", os.path.exists(L[2]))
check("屏上一行说删了哪个", "已删除老 bash 版遗留：net-optimize-apply" in o.getvalue() and o.getvalue().count("\n") == 1)
with quiet() as o:
    n.clean_legacy_bash()
check("再跑一次：没有可删的就一声不吭", o.getvalue() == "")
mo = src[src.index("def main_optimize"):src.index("def main_optimize") + 1500]
check("整套优化里跟在 clean_old_config 后面跑", 0 < mo.find("clean_old_config()") < mo.find("clean_legacy_bash()"))
check("卸载照旧把三个遗留一起删（同一份清单）", "+ LEGACY_BASH" in src[src.index("def cmd_reset"):src.index("def cmd_reset") + 9000])
check("--check 不再说「已不被引用」（有人调用的会留着）", "已不被引用" not in src)
check("版本 ≥ 4.5.20", tuple(map(int, re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1).split("."))) >= (4, 5, 20))

print("ALL PASS" if not bad else f"{bad} FAIL")
sys.exit(1 if bad else 0)

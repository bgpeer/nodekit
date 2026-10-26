#!/usr/bin/env python3
"""net-optimize 4.5.17 —— 仓库主人 10/25：「节点和网络优化是不是对接上了……有没有更优秀的方案」，
  看完结论选了「修隐患 + 换 fq + 节点画像架构」，第五条（删 / 关功能）「全部同意」。本测试管第一步：
  · 队列一律优先 fq（BBR 标准搭档）；fq_pie 改成显式开关；「抢带宽」档 pfifo_fast 也换成 fq
  · conntrack 加 tcp_be_liberal=1（开机会无条件加 INVALID→DROP，跨境乱序会误丢合法包）
  · 删 MPTCP（节点不建 MPTCP 套接字，纯空转）、删 DSCP 打标（方向不对、公网清零），并清老残留
  · 自适应守护改成可选、默认关：1/2 档守护关着时出口交还 fq；菜单 12 加「7 自适应切换」开关
"""
import os, re, sys, io, json, logging, tempfile, contextlib, importlib.util
logging.raiseExceptions = False      # 守护进程的 syslog 在容器里没有 /dev/log，别刷噪音
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def load(name, path, env=None):
    old = dict(os.environ)
    if env is not None:
        os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear(); os.environ.update(old)

os.environ.pop("ADAPTIVE_QOS", None); os.environ.pop("ENABLE_FQ_PIE", None)
n = load("netopt", "net-optimize.py")
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
quiet = contextlib.redirect_stdout(io.StringIO)

# ── 1. 配置项 ──────────────────────────────────────────────────────────────
check("fq_pie 默认关（要用得显式 ENABLE_FQ_PIE=1）", n.Cfg.ENABLE_FQ_PIE is False)
check("Cfg 里没有 DSCP / MPTCP 开关了", not hasattr(n.Cfg, "ENABLE_DSCP") and not hasattr(n.Cfg, "ENABLE_MPTCP"))
check("自适应守护默认关（没环境变量、没 config）", n.Cfg.ADAPTIVE_PREF is False and n.Cfg.ADAPTIVE_QOS is False)
n2 = load("netopt2", "net-optimize.py", {"ADAPTIVE_QOS": "1"})
check("环境变量 ADAPTIVE_QOS=1 显式给了以它为准", n2.Cfg.ADAPTIVE_QOS is True)
n3 = load("netopt3", "net-optimize.py", {"ADAPTIVE_QOS": "1", "ADAPTIVE_QOS_MODE": "fixed_cake"})
check("固定 cake 档压成不跑守护，但用户选择（PREF）不丢", n3.Cfg.ADAPTIVE_QOS is False and n3.Cfg.ADAPTIVE_PREF is True)
tmp = tempfile.mkdtemp()
n.CONFIG_FILE = f"{tmp}/config"
open(n.CONFIG_FILE, "w").write("ADAPTIVE_QOS_MODE=adaptive\nADAPTIVE_DAEMON=1\n")
check("_cfg_file_get 读得到菜单开关记的 ADAPTIVE_DAEMON", n._cfg_file_get("ADAPTIVE_DAEMON") == "1")
check("_cfg_file_get 没有的键给默认值", n._cfg_file_get("NOPE", "x") == "x")

# ── 2. 队列一律优先 fq ───────────────────────────────────────────────────────
n.QDISC_MODULES_CONF = f"{tmp}/net-optimize-qdisc.conf"     # 4.5.19 起会写开机加载，别写到真 /etc
def qorder(fq_pie=False, aggressive=False, ok=("fq", "fq_pie", "pie", "pfifo_fast")):
    tried = []
    n.Cfg.ENABLE_FQ_PIE, n.Cfg.AGGRESSIVE_MODE = fq_pie, aggressive
    n.try_set_qdisc = lambda q: (tried.append(q), q in ok)[1]
    n.run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})()
    n.get_sysctl = lambda k: "reno cubic bbr" if "available" in k else "bbr"
    n.has_sysctl_key = lambda k: True
    n.set_sysctl = lambda *a: None
    with contextlib.redirect_stdout(io.StringIO()):
        n.setup_tcp_congestion()
    return tried, n.FINAL_QDISC
t, q = qorder()
check("默认先试 fq，落在 fq", t[0] == "fq" and q == "fq")
t, q = qorder(fq_pie=True)
check("显式 ENABLE_FQ_PIE=1 才先试 fq_pie", t[0] == "fq_pie" and q == "fq_pie")
t, q = qorder(aggressive=True)
check("「抢带宽」激进档也先试 fq（不再是 pfifo_fast）", t[0] == "fq" and q == "fq")
t, q = qorder(ok=("fq_pie", "pie"))
check("内核没有 fq 时退 fq_pie", q == "fq_pie")
n.Cfg.ENABLE_FQ_PIE = n.Cfg.AGGRESSIVE_MODE = False

# ── 3. be_liberal：不跟调优开关走，并进 last-wins 强制列表 ────────────────────────
def sysctl_text(conntrack_tune):
    out = {}
    n.Cfg.ENABLE_CONNTRACK_TUNE = conntrack_tune
    n.meminfo_kb = lambda k: 1048576
    n.container_runtime_present = lambda: False
    n.write_text = lambda p, c, **k: out.__setitem__(p, c)
    n.FINAL_CC, n.FINAL_QDISC = "bbr", "fq"
    with contextlib.redirect_stdout(io.StringIO()):
        n.write_sysctl_conf()
    return out.get(n.SYSCTL_AUTH_FILE, "")
txt = sysctl_text(True)
check("sysctl 里写了 nf_conntrack_tcp_be_liberal = 1", "net.netfilter.nf_conntrack_tcp_be_liberal = 1" in txt)
check("默认队列写的是 fq", "net.core.default_qdisc = fq" in txt)
check("关了 conntrack 调优 be_liberal 照样写（INVALID→DROP 开机无条件加）",
      "nf_conntrack_tcp_be_liberal = 1" in sysctl_text(False))
check("be_liberal 进了 last-wins 强制列表", "net.netfilter.nf_conntrack_tcp_be_liberal" in n.SYSCTL_KEYS)
n.Cfg.ENABLE_CONNTRACK_TUNE = True

# ── 4. MPTCP / DSCP 去掉，老残留会清 ─────────────────────────────────────────
for f in ("setup_mptcp", "setup_dscp_marking", "apply_af41_marking", "ipt_m_length_ok"):
    check(f"{f} 已去掉", not hasattr(n, f))
mo = src[src.index("def main_optimize"):src.index("def main_optimize") + 2500]
check("主流程不再调 MPTCP / DSCP，改成 clear_dscp_residue",
      "setup_mptcp()" not in mo and "setup_dscp_marking()" not in mo and "clear_dscp_residue()" in mo)
cleared = []
n.ipt_clear = lambda cmds, pat, **k: cleared.append((tuple(cmds), pat))
n.clear_dscp_residue()
check("清残留：EF、AF41 都清，v4 v6 所有后端",
      {p for _, p in cleared} == {n.PAT_EF, n.PAT_AF41}
      and all(set(n.V4_CMDS + n.V6_CMDS) <= set(c) for c, _ in cleared))
co = src[src.index("def clean_old_config"):src.index("def clean_old_config") + 1800]
check("整套运行第一步顺手删老 MPTCP 持久化文件", "MPTCP_SYSCTL_OLD" in co)
boot = src[src.index("def cmd_boot_apply"):src.index("# === 状态检查 ===")]
boot_code = "\n".join(l.split("#", 1)[0] for l in boot.splitlines())     # 只看代码，不看注释
check("开机恢复不再写 DSCP EF、不再恢复 AF41", "DSCP" not in boot_code and "AF41" not in boot_code.upper())
check("prio 去掉按 TOS 分档的两条过滤", "match ip tos 0xb8" not in src and "match ip tos 0x88" not in src)

# ── 5. 交还默认队列：先显式挂一个再删 ──────────────────────────────────────────
cmds = []
n.run = lambda c, **k: (cmds.append(c if isinstance(c, str) else " ".join(c)),
                        type("R", (), {"returncode": 0, "stdout": "qdisc fq 0: root"})())[1]
n.reset_root_qdisc("eth0")
check("reset_root_qdisc = replace pfifo_fast 再 del root",
      cmds == ["tc qdisc replace dev eth0 root pfifo_fast", "tc qdisc del dev eth0 root"])

# ── 6. 自适应守护：默认关时出口交还 fq，固定档不动 ──────────────────────────────
def adaptive_off(mode):
    cmds.clear(); cs = {}
    n.Cfg.ADAPTIVE_QOS = n.Cfg.ADAPTIVE_PREF = False
    n.Cfg.ADAPTIVE_QOS_MODE = mode
    n.detect_outbound_iface = lambda: "eth0"
    n.have_cmd = lambda c: True
    n.config_set = lambda k, v, only_if_exists=False: cs.__setitem__(k, str(v))
    with contextlib.redirect_stdout(io.StringIO()):
        n.setup_adaptive_qos()
    return cs
cs = adaptive_off("adaptive")
check("1/2 档守护关：disable 服务（免得开机又起来）", any("systemctl disable net-optimize-adaptive-qos" in c for c in cmds))
check("1/2 档守护关：出口交还 fq", "tc qdisc del dev eth0 root" in cmds)
check("开关和阈值写回 config（整套运行会先删 config）",
      cs.get("ADAPTIVE_DAEMON") == "0" and "ADAPTIVE_QOS_THRESHOLD" in cs and cs.get("GAME_QOS_SCHEME") == "none")
adaptive_off("fixed_cake")
check("固定 cake 档：守护关时不去动出口队列", "tc qdisc del dev eth0 root" not in cmds)
cmds.clear()
n.Cfg.ADAPTIVE_QOS, n.Cfg.ADAPTIVE_QOS_MODE = False, "adaptive"
with contextlib.redirect_stdout(io.StringIO()):
    n.setup_game_qos()
check("1/2 档守护关时 setup_game_qos 不挂 cake（否则把 fq 顶掉）", not any("cake" in c for c in cmds))

# ── 7. 守护进程：抢带宽切 fq，不再补 AF41 ───────────────────────────────────────
cmds.clear()
q = n.AdaptiveQoS({"iface": "eth0", "threshold": 1, "interval": 1, "has_cake": True})
n.tc_apply_cake = lambda i: (cmds.append("cake"), True)[1]
q.switch_to_aggressive(); q.switch_to_game()
check("守护抢带宽 = 交还 fq；低流量 = cake",
      cmds[:2] == ["tc qdisc replace dev eth0 root pfifo_fast", "tc qdisc del dev eth0 root"] and cmds[-1] == "cake")
check("守护里不再有 AF41 / pfifo_fast 硬切", "apply_af41" not in src and "_apply_pfifo" not in src)

# ── 8. 菜单开关 --adaptive：只改这一项 ─────────────────────────────────────────
calls = []
n.self_install = lambda: None
n.setup_adaptive_qos = lambda: calls.append(("setup", n.Cfg.ADAPTIVE_QOS))
n.config_set = lambda k, v, only_if_exists=False: calls.append((k, str(v)))
os.remove(n.CONFIG_FILE)
with contextlib.redirect_stdout(io.StringIO()) as o:
    n.cmd_adaptive_toggle(True)
check("没跑过优化：只提示，不动", calls == [] and "先在菜单 12" in o.getvalue())
open(n.CONFIG_FILE, "w").write("ADAPTIVE_QOS_MODE=adaptive\nADAPTIVE_QOS_THRESHOLD=20971520\n")
calls.clear()
with contextlib.redirect_stdout(io.StringIO()):
    n.cmd_adaptive_toggle(True)
check("1/2 档开：起守护，阈值用 config 里记的",
      ("setup", True) in calls and n.Cfg.ADAPTIVE_QOS_THRESHOLD == 20971520)
calls.clear()
with contextlib.redirect_stdout(io.StringIO()):
    n.cmd_adaptive_toggle(False)
check("1/2 档关：走 setup_adaptive_qos 的关闭分支", ("setup", False) in calls)
open(n.CONFIG_FILE, "w").write("ADAPTIVE_QOS_MODE=fixed_cake\n")
calls.clear()
with contextlib.redirect_stdout(io.StringIO()) as o:
    n.cmd_adaptive_toggle(True)
check("固定档：只记下开关，不起守护", ("ADAPTIVE_DAEMON", "1") in calls
      and not any(c[0] == "setup" for c in calls) and "切回 1 / 2 档后生效" in o.getvalue())
check("CLI 有 --adaptive on|off 并分发到 cmd_adaptive_toggle",
      '"--adaptive", choices=["on", "off"]' in src and "cmd_adaptive_toggle(args.adaptive ==" in src)
mv = tuple(int(x) for x in re.findall(r"\d+", re.search(r'^VERSION = "([\d.]+)"', src, re.M).group(1)))
check("VERSION ≥ 4.5.17", mv >= (4, 5, 17))

# ── 9. 主脚本：菜单 12 第 7 项、阈值读 config、旧版守卫 ──────────────────────────
x = load("xy", "xy-installer.py")
real_adaptive = x._netopt_adaptive          # 菜单测试会把它换成桩，守卫测试要用真的
x.NETOPT_CONFIG = f"{tmp}/xy-config"; x.NETOPT_ADAPTIVE = f"{tmp}/xy-aq.json"
open(x.NETOPT_CONFIG, "w").write("ADAPTIVE_QOS_MODE=adaptive\nADAPTIVE_QOS_THRESHOLD=20971520\nADAPTIVE_DAEMON=0\n")
check("档位阈值先读 config（守护关着时 adaptive-qos.conf 不更新）", x._netopt_state() == ("adaptive", 20.0))
check("开关读 ADAPTIVE_DAEMON=0 → 关", x._netopt_daemon_on() is False)
open(x.NETOPT_CONFIG, "w").write("ADAPTIVE_QOS_MODE=adaptive\n")
x.sh = lambda c, check=True: "active"
check("老机器还没这一项：看服务在不在跑", x._netopt_daemon_on() is True)
os.remove(x.NETOPT_CONFIG)
check("没优化过 = 关", x._netopt_daemon_on() is False)

open(x.NETOPT_CONFIG, "w").write("ADAPTIVE_QOS_MODE=adaptive\nADAPTIVE_QOS_THRESHOLD=10485760\nADAPTIVE_DAEMON=0\n")
items, kvs, ad = [], [], []
x.ui_title = x.ui_line = lambda *a, **k: None
x.ui_kv = lambda k, v: kvs.append((k, v))
x.ui_items = lambda it: items.extend(it)
x._netopt_version = lambda: "4.5.17"
x._bbr_kernel_cur = lambda: ""
x._netopt_adaptive = lambda on: ad.append(on)
it = iter(["7", "0"]); x._ask = lambda q="": next(it)
with contextlib.redirect_stdout(io.StringIO()):
    x.net_optimize_menu()
check("菜单 12 有「7. 自适应切换（守护进程）　当前：关 = 纯 fq 模式」",
      (7, "自适应切换（守护进程）", "关 = 纯 fq 模式") in items)
check("1/2 档守护关时，当前档位后面标「· 切换关」", any("切换关" in str(v) for _, v in kvs))
check("选 7（当前关）→ 打开", ad == [True])
check("菜单 1~6 一项没少", {i[0] for i in items} >= {4, 5, 6, 7, 0})

ran = []
x.ensure_remote_script = lambda u, l: True
x._run_child = lambda c, **k: ran.append(c)
x.NETOPT_LOCAL = f"{tmp}/netopt-cache.py"
open(x.NETOPT_LOCAL, "w").write("# 旧版：不认这个参数\n")
with contextlib.redirect_stdout(io.StringIO()):
    real_adaptive(True)
check("缓存是旧版（不认 --adaptive）就拦住，不让它转头跑整套优化", ran == [])
open(x.NETOPT_LOCAL, "w").write('g.add_argument("--adaptive")\n')
with contextlib.redirect_stdout(io.StringIO()):
    real_adaptive(False)
check("新版：python3 … --adaptive off", ran == [f"python3 {x.NETOPT_LOCAL} --adaptive off"])

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

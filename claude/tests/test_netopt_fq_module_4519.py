#!/usr/bin/env python3
"""net-optimize 4.5.19：fq 模块 —— 仓库主人 10/25：「fq写了安装这个的模块没有啊，以前好像没写了」。
  以前确实没写：只设 default_qdisc=fq、靠内核临场自己加载 sch_fq；模块不在就一声不吭退成 fq_pie / pie / 老默认，
  --check 还一直提示「再跑一次就换上」——跑多少次都一样。现在：
  · 设队列前先 modprobe sch_fq；编成了模块、文件不在（Ubuntu 拆在 linux-modules-extra 里）就装一次再试
  · 内核压根没编进去（Ubuntu 22.04 及以前的精简版 kvm 内核：fq / BBR / cake 都没有）不白跑 apt，屏上照实说
  · 选定的队列 / 拥塞算法模块写进 /etc/modules-load.d 开机就加载（只写内核里真有的），卸载一起删
  · cake 走同一个助手
  10/25 拆包查过：Ubuntu 24.04 通用 / 谷歌 / AWS / Azure / 甲骨文内核，22.04 通用 / 谷歌 / AWS 内核，
  sch_fq、tcp_bbr 都在基础模块包里；22.04 的 kvm 内核配置是「# CONFIG_NET_SCH_FQ is not set」。
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
def body(name):
    m_ = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.S | re.M)
    return m_.group(0) if m_ else ""
tmp = tempfile.mkdtemp()
KVM_CFG = "CONFIG_NET_SCHED=y\n# CONFIG_NET_SCH_FQ is not set\n# CONFIG_NET_SCH_CAKE is not set\n# CONFIG_TCP_CONG_ADVANCED is not set\n"
GEN_CFG = "CONFIG_NET_SCHED=y\nCONFIG_NET_SCH_FQ=m\nCONFIG_NET_SCH_CAKE=m\nCONFIG_TCP_CONG_BBR=m\n"

class R:
    def __init__(self, rc=0, out=""): self.returncode, self.stdout = rc, out

def rig(n, cfg_text, have=(), extra=False, skip_apt=False, extra_has=()):
    """have = 内核里（基础模块包）已经有的模块；extra_has = 装了 linux-modules-extra 才有的；extra = 仓库里有没有那个包。"""
    calls = {"modprobe": [], "apt": [], "policy": []}
    state = {"extra": False}
    n._uname = lambda: type("U", (), {"release": "5.15.0-1109-kvm", "machine": "x86_64"})()
    n.read_text = lambda p, d="": cfg_text if p.startswith("/boot/config-") else d
    n.Cfg.SKIP_APT = skip_apt
    n.have_cmd = lambda c: True
    def run(cmd, **k):
        if cmd[0] == "modprobe":
            calls["modprobe"].append(cmd)
            mod = cmd[-1]
            ok = mod in have or (state["extra"] and mod in extra_has)
            return R(0 if ok else 1)
        return R(0)
    n.run = run
    n._apt_candidate = lambda pkg: (calls["policy"].append(pkg), "1.0" if extra else "")[1]
    def apt(args, **k):
        calls["apt"].append(args); state["extra"] = True
        return R(0)
    n.apt = apt
    return calls

quiet = lambda: contextlib.redirect_stdout(io.StringIO())

# ── 1. 内核编没编进去 ─────────────────────────────────────────────────────────
n = load("n1")
n._uname = lambda: type("U", (), {"release": "x"})()
for txt, sym, want in ((GEN_CFG, "NET_SCH_FQ", "m"), ("CONFIG_NET_SCH_FQ=y\n", "NET_SCH_FQ", "y"),
                       (KVM_CFG, "NET_SCH_FQ", "n"), (KVM_CFG, "TCP_CONG_BBR", "n"), ("", "NET_SCH_FQ", "")):
    n.read_text = lambda p, d="", t=txt: t if p.startswith("/boot/config-") else d
    check(f"kernel_has({sym}) 读 /boot/config：{want or '看不出来'}", n.kernel_has(sym) == want)
check("配置里根本没出现的（依赖没开，kvm 内核的 BBR）也算没编", "TCP_CONG_BBR" not in KVM_CFG)

# ── 2. ensure_kmod ─────────────────────────────────────────────────────────────
n = load("n2"); c = rig(n, GEN_CFG, have=("sch_fq",))
check("模块在：modprobe 一下就成，不碰 apt", n.ensure_kmod("sch_fq", "NET_SCH_FQ") and not c["apt"] and not c["policy"])
n = load("n3"); c = rig(n, KVM_CFG)
check("内核压根没编进去（kvm 内核）：不白跑 apt，直接 False", not n.ensure_kmod("sch_fq", "NET_SCH_FQ") and not c["apt"] and not c["policy"])
n = load("n4"); c = rig(n, GEN_CFG, extra=True, extra_has=("sch_fq",))
with quiet() as o:
    ok = n.ensure_kmod("sch_fq", "NET_SCH_FQ")
check("编成了模块、文件不在：装 linux-modules-extra-<内核> 再试，成了",
      ok and c["apt"] == [["install", "-y", "-qq", "linux-modules-extra-5.15.0-1109-kvm"]] and "linux-modules-extra" in o.getvalue())
n = load("n5"); c = rig(n, GEN_CFG, extra=False)
check("仓库里没这个包：不跑 apt", not n.ensure_kmod("sch_fq", "NET_SCH_FQ") and not c["apt"] and c["policy"])
n = load("n6"); c = rig(n, GEN_CFG, extra=True, skip_apt=True)
check("SKIP_APT=1：不碰 apt", not n.ensure_kmod("sch_fq", "NET_SCH_FQ") and not c["apt"] and not c["policy"])
n = load("n7"); c = rig(n, "", extra=True, extra_has=("sch_fq",))
with quiet():
    ok = n.ensure_kmod("sch_fq", "NET_SCH_FQ")
check("读不到内核配置（容器里常见）：照样试一次 extra", ok and c["apt"])

# ── 3. 设队列：先装 fq 模块，装不上照实说，选完写开机加载 ─────────────────────────────
def congestion(cfg_text, have, qdisc_ok, avail_cc="reno cubic bbr", aggressive=False):
    n = load(f"nq{len(have)}{len(qdisc_ok)}{aggressive}")
    c = rig(n, cfg_text, have=have)
    n.QDISC_MODULES_CONF = f"{tmp}/qdisc-{len(have)}-{len(qdisc_ok)}.conf"
    n.Cfg.ENABLE_FQ_PIE, n.Cfg.AGGRESSIVE_MODE = False, aggressive
    tried = []
    n.try_set_qdisc = lambda q: (tried.append(q), q in qdisc_ok)[1]
    n.has_sysctl_key = lambda k: True
    cur = {"cc": "cubic"}
    n.set_sysctl = lambda k, v: cur.__setitem__("cc", v) if "congestion" in k else None
    n.get_sysctl = lambda k: avail_cc if "available" in k else (cur["cc"] if "congestion" in k else "pfifo_fast")
    rm_ = []
    real_rm = n._rm
    n._rm = lambda p: (rm_.append(p), real_rm(p))
    with quiet() as o:
        n.setup_tcp_congestion()
    conf = open(n.QDISC_MODULES_CONF).read() if os.path.exists(n.QDISC_MODULES_CONF) else None
    return n, c, tried, o.getvalue(), conf, rm_

n, c, tried, out, conf, _ = congestion(GEN_CFG, ("sch_fq", "tcp_bbr"), ("fq", "fq_pie", "pie"))
fq_load = next((i for i, x in enumerate(c["modprobe"]) if x == ["modprobe", "sch_fq"]), None)
check("设队列之前先 modprobe sch_fq", fq_load is not None and tried and tried[0] == "fq")
check(f"选定 fq + bbr：写开机加载 sch_fq、tcp_bbr（{conf!r}）",
      conf is not None and conf.splitlines()[1:] == ["sch_fq", "tcp_bbr"] and conf.startswith("# Net-Optimize"))
check("写之前查过内核里真有（modprobe -n）", ["modprobe", "-n", "-q", "sch_fq"] in c["modprobe"])
check("fq、BBR 好好的不报 ⚠", "⚠" not in out)

n, c, tried, out, conf, rm_ = congestion(KVM_CFG, (), ("pie",) and (), avail_cc="reno cubic")
check(f"kvm 内核：没有 fq、也没有 BBR → 屏上一行 ⚠ 照实说（{[l for l in out.splitlines() if '⚠' in l]}）",
      "没有 fq、BBR，只能用 pfifo_fast / cubic" in out and "5.15.0-1109-kvm" in out and "得换内核" in out
      and len([l for l in out.splitlines() if "⚠" in l]) == 1)
check("kvm 内核：没有能开机加载的，不写那个文件（写了 systemd-modules-load 开机报失败）",
      conf is None and n.QDISC_MODULES_CONF in rm_)
check("kvm 内核：没白跑 apt", not c["apt"])

n, c, tried, out, conf, _ = congestion(GEN_CFG, ("sch_fq", "tcp_bbr"), ("fq", "pfifo_fast"), aggressive=True)
check("「抢带宽」档一样先装 fq、写开机加载", ["modprobe", "sch_fq"] in c["modprobe"] and conf and "sch_fq" in conf)

# ── 4. cake 走同一个助手 ─────────────────────────────────────────────────────
check("ensure_cake_module 走 ensure_kmod（同一套补装逻辑）", 'ensure_kmod("sch_cake", "NET_SCH_CAKE")' in body("ensure_cake_module"))
n = load("ncake"); c = rig(n, KVM_CFG)
with quiet() as o:
    r = n.ensure_cake_module()
check("cake 用不了：照旧退方案 B，不白跑 apt", r is False and "方案 B" in o.getvalue() and not c["apt"])

# ── 5. --check：内核没编进去的别再叫人「再跑一次」 ───────────────────────────────
ck = body("cmd_check")
nf = load("nfix")
nf.container_kind = lambda: ""
nf._uname = lambda: type("U", (), {"release": "5.15.0-1109-kvm"})()
nf.read_text = lambda p, d="": KVM_CFG if p.startswith("/boot/config-") else d
kvm_fix = nf._fq_fix()
nf.read_text = lambda p, d="": GEN_CFG if p.startswith("/boot/config-") else d
gen_fix = nf._fq_fix()
check(f"--check：fq 没编进内核 → 说要换内核（{kvm_fix}），编了才提示「再跑一次就换上」",
      "要用得换内核" in kvm_fix and "再跑一次" not in kvm_fix and "再跑一次「12 网络优化」就换上" in gen_fix)
check("--check 第 [1] 段的提示走 _fq_fix", "_fq_fix()" in ck)
check("--check：BBR 没编进内核也照实说", 'kernel_has("TCP_CONG_BBR") == "n"' in ck and "没编进 BBR" in ck)
check("--check 列出开机加载的队列 / 拥塞算法模块", "QDISC_MODULES_CONF" in ck)

# ── 6. 卸载一起删 ─────────────────────────────────────────────────────────────
check("--reset 删掉队列模块的开机加载", "_rm(QDISC_MODULES_CONF)" in body("cmd_reset"))
check("开机加载文件带 net-optimize 前缀（不跟别人的撞名）", "/etc/modules-load.d/net-optimize-qdisc.conf" in src)
check("版本 ≥ 4.5.19", tuple(map(int, re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1).split("."))) >= (4, 5, 19))

print("ALL PASS" if not bad else f"{bad} FAIL")
sys.exit(1 if bad else 0)

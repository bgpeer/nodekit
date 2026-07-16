#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# 🚀 Net-Optimize-Ultimate v4.2.0 (Python 重构版)
# 功能对齐 bash v3.8.0，所有功能 1:1 保留：
#   自动更新(SHA256SUMS 校验) / 低内存临时 swap / dpkg 自愈 / ulimit /
#   BBRv3→BBRv2→bbrplus→BBR 拥塞算法 / fq_pie 队列 / RAM 自适应缓冲区 /
#   tcp_mem 动态计算 / sysctl 权威收敛(last-wins override) / rp_filter 逐接口 /
#   conntrack 调优+hashsize=max/4 / 网卡 offload(GRO/GSO/TSO/LRO off/
#   UDP GRO 转发/发送分段卸载) / 中断合并自适应 / WireGuard GRO /
#   RPS/RFS/XPS / CPU performance 调频 / MPTCP / MSS 自动探测+双栈 Clamping /
#   iptables 双后端检测 / DSCP EF(QUIC)+AF41(游戏小包) / initcwnd 线路自适应 /
#   激进模式 / 游戏 QoS(cake diffserv4→prio+fq_codel) / 自适应 QoS 守护 /
#   Nginx 官方源+Pin+月度自动升级 / 开机恢复(flock 幂等) /
#   networkd-dispatcher DHCP 续约恢复 initcwnd / netfilter-persistent 兼容
#
# v4.2.0 新增：
#   --reset=完整卸载(原 net-optimize-reset.sh 移植)：清服务/tc/iptables/sysctl/
#   ulimit/conntrack/持久化/initcwnd/cron/脚本本体，恢复被禁用的冲突 sysctl 文件；
#   并修老版 reset 的遗漏——同步剥掉 netfilter-persistent 保存的 mangle 段再
#   save，否则重启后 TCPMSS/DSCP 会被它恢复回来（nat 端口跳跃不受影响）
#
# v4.0.0 重构变化（行为不变，结构变化）：
#   1) 单文件多入口：默认=完整优化；--boot=开机恢复(原 net-optimize-apply)；
#      --daemon=自适应 QoS 守护(原独立 py)；--nginx-upgrade=nginx 装/升(原
#      独立 sh，cron 调用)；--reapply-initcwnd=DHCP 续约钩子；--status=状态报告；
#      --check=完整状态检测(原 net-optimize-check.sh v1.11，v4.1.0 合并)
#   2) 开机恢复与主流程共用同一套函数，不再有两份需要手动同步的逻辑
#   3) iptables 清理/写入/去重收敛为统一助手，行为与 v3.8.0 等价
#   4) 自动更新仅在默认完整优化模式触发（--boot/--daemon 不自更新，更安全）
#
# 用法：
#   python3 <(curl -fsSL https://raw.githubusercontent.com/bgpeer/nodekit/main/net-optimize.py)
#   或落盘后: python3 /usr/local/sbin/net-optimize.py
#   （也可从 bgpeer 管理面板「10 网络优化」一键调用）
# ==============================================================================

import argparse
import atexit
import fcntl
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

VERSION = "4.2.0"

SCRIPT_PATH = "/usr/local/sbin/net-optimize.py"
REMOTE_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/net-optimize.py"
REMOTE_SHA256SUMS_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/SHA256SUMS"

CONFIG_DIR = "/etc/net-optimize"
CONFIG_FILE = f"{CONFIG_DIR}/config"
MODULES_FILE = f"{CONFIG_DIR}/modules.list"
ADAPTIVE_CONF = f"{CONFIG_DIR}/adaptive-qos.conf"
SYSCTL_AUTH_FILE = "/etc/sysctl.d/99-net-optimize.conf"
SYSCTL_OVERRIDE_FILE = "/etc/sysctl.d/zzz-net-optimize-override.conf"
SYSCTL_BACKUP_DIR = f"{CONFIG_DIR}/sysctl-backup"
CONNTRACK_MODULES_CONF = "/etc/modules-load.d/conntrack.conf"
BOOT_SERVICE = "net-optimize"
ADAPTIVE_QOS_SERVICE = "net-optimize-adaptive-qos"
NGINX_LOG = "/var/log/nginx-auto-upgrade.log"
PYTHON_BIN = shutil.which("python3") or "/usr/bin/python3"

SYSCTL_KEYS = [
    "net.core.default_qdisc",
    "net.ipv4.tcp_congestion_control",
    "net.ipv4.tcp_mtu_probing",
    "net.core.rmem_default",
    "net.core.wmem_default",
    "net.core.rmem_max",
    "net.core.wmem_max",
    "net.ipv4.tcp_rmem",
    "net.ipv4.tcp_wmem",
    "net.ipv4.udp_rmem_min",
    "net.ipv4.udp_wmem_min",
    "net.ipv4.udp_mem",
    "net.netfilter.nf_conntrack_max",
    "net.netfilter.nf_conntrack_udp_timeout",
    "net.netfilter.nf_conntrack_udp_timeout_stream",
]

V4_CMDS = ["iptables", "iptables-legacy", "iptables-nft"]
V6_CMDS = ["ip6tables", "ip6tables-legacy", "ip6tables-nft"]


# === 全局配置开关（环境变量，与 bash 版同名同默认值）===
def _env(name, default):
    return os.environ.get(name, default)


def _flag(name, default="1"):
    return _env(name, default) == "1"


class Cfg:
    ENABLE_FQ_PIE = _flag("ENABLE_FQ_PIE")
    ENABLE_MTU_PROBE = _env("ENABLE_MTU_PROBE", "1")
    ENABLE_MSS_CLAMP = _flag("ENABLE_MSS_CLAMP")
    MSS_USER_SET = "MSS_VALUE" in os.environ  # 用户显式指定 MSS 时跳过自动探测
    MSS_VALUE = int(_env("MSS_VALUE", "1452"))
    MSS_AUTO = _flag("MSS_AUTO")
    ENABLE_CONNTRACK_TUNE = _flag("ENABLE_CONNTRACK_TUNE")
    NFCT_MAX = int(_env("NFCT_MAX", "262144"))
    ENABLE_NGINX_REPO = _flag("ENABLE_NGINX_REPO")
    SKIP_APT = _flag("SKIP_APT", "0")
    APPLY_AT_BOOT = _flag("APPLY_AT_BOOT")
    RP_FILTER = _env("RP_FILTER", "2")  # 0=关闭 1=严格 2=松散
    ENABLE_NIC_OFFLOAD = _flag("ENABLE_NIC_OFFLOAD")
    ENABLE_RPS_RFS = _flag("ENABLE_RPS_RFS")
    ENABLE_IPV6_MSS = _flag("ENABLE_IPV6_MSS")
    ENABLE_DSCP = _flag("ENABLE_DSCP")
    ENABLE_INITCWND = _flag("ENABLE_INITCWND")
    TCP_NOTSENT_LOWAT = _env("TCP_NOTSENT_LOWAT", "4096")
    # 代理节点监听口从临时端口池保留（nodekit: 15000-45000 + 20080 + 30000-31000）
    RESERVED_PORTS = _env("RESERVED_PORTS", "15000-45000,20080,30000-31000")
    AGGRESSIVE_MODE = _flag("AGGRESSIVE_MODE", "0")
    ENABLE_GAME_QOS = _flag("ENABLE_GAME_QOS")
    ADAPTIVE_QOS = _flag("ADAPTIVE_QOS")
    ADAPTIVE_QOS_MODE = _env("ADAPTIVE_QOS_MODE", "adaptive")  # adaptive / fixed_cake
    ADAPTIVE_QOS_THRESHOLD = int(_env("ADAPTIVE_QOS_THRESHOLD", "10485760"))
    ADAPTIVE_QOS_INTERVAL = int(_env("ADAPTIVE_QOS_INTERVAL", "2"))
    ADAPTIVE_QOS_COOLDOWN = int(_env("ADAPTIVE_QOS_COOLDOWN", "10"))
    ENABLE_CPU_GOVERNOR = _flag("ENABLE_CPU_GOVERNOR")
    ENABLE_XPS = _flag("ENABLE_XPS")
    ENABLE_IRQ_COALESCING = _flag("ENABLE_IRQ_COALESCING")
    ENABLE_MPTCP = _flag("ENABLE_MPTCP")
    ENABLE_WG_OPT = _flag("ENABLE_WG_OPT")
    RAM_ADAPTIVE_BUFFERS = _flag("RAM_ADAPTIVE_BUFFERS")


# fixed_cake 模式：强制关闭自适应守护，走 setup_game_qos 固定 cake
if Cfg.ADAPTIVE_QOS_MODE == "fixed_cake":
    Cfg.ADAPTIVE_QOS = False

FINAL_CC = ""
FINAL_QDISC = ""


# === 核心工具函数 ===
def run(cmd, timeout=15, env_extra=None, text_input=None):
    """执行命令（列表或字符串，字符串按 shlex 拆分，不走 shell）。永不抛异常。"""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env, input=text_input)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "command not found")
    except Exception as e:  # noqa
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def sh(cmdline, timeout=30, env_extra=None):
    """确需 shell 管道时使用。"""
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    try:
        return subprocess.run(cmdline, shell=True, capture_output=True,
                              text=True, timeout=timeout, env=env)
    except Exception as e:  # noqa
        return subprocess.CompletedProcess(cmdline, 1, "", str(e))


def apt(args, timeout=900):
    return run(["apt-get"] + args, timeout=timeout,
               env_extra={"DEBIAN_FRONTEND": "noninteractive"})


def have_cmd(name):
    return shutil.which(name) is not None


def echo(msg=""):
    print(msg, flush=True)


def logger_msg(msg):
    run(["logger", "-t", "net-optimize", msg], timeout=5)


def read_text(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except Exception:  # noqa
        return default


def write_text(path, content, mode=0o644, exe=False):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o755 if exe else mode)


def write_proc(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except Exception:  # noqa
        return False


def require_root():
    if os.geteuid() != 0:
        echo("❌ 请使用 root 用户运行")
        sys.exit(1)


def has_sysctl_key(key):
    return os.path.exists("/proc/sys/" + key.replace(".", "/"))


def get_sysctl(key):
    r = run(["sysctl", "-n", key], timeout=5)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "N/A"


def set_sysctl(key, value):
    return run(["sysctl", "-w", f"{key}={value}"], timeout=5).returncode == 0


def detect_distro():
    osr, did, codename = read_text("/etc/os-release"), "unknown", "unknown"
    m = re.search(r'^ID=["\']?([^"\'\n]+)', osr, re.M)
    if m:
        did = m.group(1)
    m = re.search(r'^VERSION_CODENAME=["\']?([^"\'\n]+)', osr, re.M)
    if not m:
        m = re.search(r'^UBUNTU_CODENAME=["\']?([^"\'\n]+)', osr, re.M)
    if m:
        codename = m.group(1)
    return did, codename


def meminfo_kb(field):
    m = re.search(rf"^{field}:\s+(\d+)", read_text("/proc/meminfo"), re.M)
    return int(m.group(1)) if m else 0


# === config 键值持久化（/etc/net-optimize/config，KEY=VAL）===
def config_read():
    cfg = {}
    for line in read_text(CONFIG_FILE).splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def config_set(key, value, only_if_exists=False):
    if only_if_exists and not os.path.exists(CONFIG_FILE):
        return
    cfg = config_read()
    cfg[key] = str(value)
    write_text(CONFIG_FILE, "".join(f"{k}={v}\n" for k, v in cfg.items()))


# === iptables 统一助手（清理 / 写入 / 去重 / 计数）===
def ipt_rules(cmd, table="mangle", chain="POSTROUTING"):
    r = run([cmd, "-w", "2", "-t", table, "-S", chain], timeout=5)
    return r.stdout.splitlines() if r.returncode == 0 else []


def ipt_del_rule(cmd, rule, table="mangle"):
    parts = shlex.split(rule.replace("-A ", "-D ", 1))
    return run([cmd, "-w", "2", "-t", table] + parts, timeout=5).returncode == 0


def ipt_clear(cmds, pattern, table="mangle", chain="POSTROUTING", max_rounds=80):
    """把所有指定后端里匹配 pattern 的规则清空（等价 _nopt_clear_all_tcpmss 等）。"""
    rx = re.compile(pattern)
    for cmd in cmds:
        if not have_cmd(cmd):
            continue
        for _ in range(max_rounds):
            hits = [l for l in ipt_rules(cmd, table, chain) if rx.search(l)]
            if not hits:
                break
            for rule in hits:
                ipt_del_rule(cmd, rule, table)


def ipt_dedup(cmd, pattern, keep=1, table="mangle", chain="POSTROUTING"):
    """匹配 pattern 的规则只保留 keep 条（等价 _nopt_dedup_rules）。"""
    if not cmd or not have_cmd(cmd):
        return
    rx = re.compile(pattern)
    for _ in range(20):
        hits = [l for l in ipt_rules(cmd, table, chain) if rx.search(l)]
        if len(hits) <= keep:
            break
        if not ipt_del_rule(cmd, hits[0], table):
            break


def ipt_count(cmd, pattern, table="mangle", chain="POSTROUTING"):
    rx = re.compile(pattern)
    return len([l for l in ipt_rules(cmd, table, chain) if rx.search(l)])


def ipt_add(cmd, args, iface=None, table="mangle", chain="POSTROUTING"):
    base = [cmd, "-w", "2", "-t", table, "-A", chain]
    if iface and iface != "unknown":
        base += ["-o", iface]
    return run(base + args, timeout=5).returncode == 0


PAT_TCPMSS = r"TCPMSS"
PAT_EF = r"0x2e|dscp-class EF|set-dscp 46"
PAT_AF41 = r"0x22|dscp-class AF41|set-dscp 34"
PAT_DSCP = r"DSCP"


def detect_ipt_backend():
    """检测 iptables 实际可用后端（快速路径 legacy 警告 + 试写验证）。"""
    if have_cmd("iptables"):
        r = run(["iptables", "-t", "mangle", "-S", "POSTROUTING"], timeout=5)
        warn = (r.stdout or "") + (r.stderr or "")
        if re.search(r"iptables-legacy", warn, re.I) and have_cmd("iptables-legacy"):
            return "iptables-legacy"

        test_rule = ["-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                     "-j", "TCPMSS", "--set-mss", "9999"]
        run(["iptables", "-t", "mangle", "-A", "POSTROUTING"] + test_rule, timeout=5)
        cnt = ipt_count("iptables", r"set-mss 9999")
        # 清理测试规则（所有后端都清）
        run(["iptables", "-t", "mangle", "-D", "POSTROUTING"] + test_rule, timeout=5)
        if have_cmd("iptables-legacy"):
            run(["iptables-legacy", "-t", "mangle", "-D", "POSTROUTING"] + test_rule, timeout=5)
        if cnt >= 1:
            return "iptables"

        if have_cmd("iptables-legacy"):
            run(["iptables-legacy", "-t", "mangle", "-A", "POSTROUTING"] + test_rule, timeout=5)
            cnt = ipt_count("iptables-legacy", r"set-mss 9999")
            run(["iptables-legacy", "-t", "mangle", "-D", "POSTROUTING"] + test_rule, timeout=5)
            if cnt >= 1:
                return "iptables-legacy"
        return "iptables"

    if have_cmd("iptables-legacy"):
        return "iptables-legacy"
    if have_cmd("iptables-nft"):
        return "iptables-nft"
    return ""


def ip6_cmd_for(ipt_backend):
    if ipt_backend == "iptables-legacy" and have_cmd("ip6tables-legacy"):
        return "ip6tables-legacy"
    if have_cmd("ip6tables"):
        return "ip6tables"
    return ""


def saved_ipt_backend():
    """优先复用 config 里记录的后端，避免重复试写干扰。"""
    b = config_read().get("IPT_BACKEND", "")
    if b and have_cmd(b):
        return b
    return detect_ipt_backend()


def detect_outbound_iface():
    for args in (["ip", "-4", "route", "get", "1.1.1.1"],
                 ["ip", "-6", "route", "get", "2001:4860:4860::8888"]):
        r = run(args, timeout=5)
        m = re.search(r"\bdev\s+(\S+)", r.stdout)
        if m:
            return m.group(1)
    r = run(["ip", "route", "show", "default"], timeout=5)
    m = re.search(r"^default\s.*?\bdev\s+(\S+)", r.stdout, re.M)
    return m.group(1) if m else ""


def strip_route_params(route):
    for pat in (r" initcwnd \d+", r" initrwnd \d+", r" expires \d+sec",
                r" hoplimit \d+", r" pref [a-z]+"):
        route = re.sub(pat, "", route)
    return route


# === 自动更新（SHA256SUMS 校验，仅默认完整模式触发）===
def _mirror_urls(url):
    """raw.githubusercontent 常被限流(429)，补 jsDelivr 镜像兜底（raw 优先，最新鲜）。"""
    urls = [url]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if m:
        o, repo, br, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
        urls.append(f"https://fastly.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
    return urls


def fetch_url(url, timeout=10):
    for u in _mirror_urls(url):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "net-optimize"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:  # noqa
            continue
    return None


def auto_update(argv):
    data = fetch_url(REMOTE_URL)
    if not data:
        return
    remote_hash = hashlib.sha256(data).hexdigest()
    local_hash = ""
    if os.path.isfile(SCRIPT_PATH):
        try:
            with open(SCRIPT_PATH, "rb") as f:
                local_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass
    if remote_hash == local_hash:
        return

    sums = fetch_url(REMOTE_SHA256SUMS_URL)
    if not sums:
        echo("⚠️ 无法获取 SHA256SUMS，跳过自动更新（安全策略）")
        return
    expected = ""
    for line in sums.decode(errors="replace").splitlines():
        if re.search(r"(^|\s)net-optimize\.py$", line.strip()):
            expected = line.split()[0]
            break
    if not expected:
        echo("⚠️ SHA256SUMS 中未找到脚本条目，跳过自动更新")
        return
    if remote_hash != expected:
        echo("❌ 远程脚本 SHA256 校验失败（可能被篡改，或镜像缓存未同步），拒绝更新")
        echo(f"  期望: {expected}")
        echo(f"  实际: {remote_hash}")
        return

    echo("🌀 检测到新版本（SHA256 校验通过），正在更新...")
    tmp = SCRIPT_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, SCRIPT_PATH)
    os.execv(PYTHON_BIN, [PYTHON_BIN, SCRIPT_PATH] + argv)


def self_install():
    """python3 <(curl ...) 运行时 __file__ 是 /dev/fd/*，允许失败。"""
    try:
        src = os.path.abspath(__file__)
        if os.path.isfile(src) and src != SCRIPT_PATH:
            shutil.copyfile(src, SCRIPT_PATH)
            os.chmod(SCRIPT_PATH, 0o755)
    except Exception:  # noqa
        pass


# === 低内存环境保障：<2GB 且无 swap 自动建 512MB 临时 swap ===
def ensure_swap():
    swap_file = "/tmp/.net-optimize-swap"
    total_kb = meminfo_kb("MemTotal") or 4194304
    swap_kb = meminfo_kb("SwapTotal")
    if total_kb >= 2097152 or swap_kb > 0:
        return
    echo(f"⚠️ 内存 {total_kb}KB 且无 swap，自动创建 512MB 临时 swap...")
    ok = (run(["dd", "if=/dev/zero", f"of={swap_file}", "bs=1M", "count=512",
               "status=none"], timeout=120).returncode == 0)
    if ok:
        os.chmod(swap_file, 0o600)
        ok = run(["mkswap", swap_file], timeout=30).returncode == 0 \
            and run(["swapon", swap_file], timeout=30).returncode == 0
    if ok:
        echo("  ✅ 临时 swap 已启用")

        def _cleanup():
            run(["swapoff", swap_file], timeout=30)
            try:
                os.remove(swap_file)
            except Exception:  # noqa
                pass
        atexit.register(_cleanup)
    else:
        try:
            os.remove(swap_file)
        except Exception:  # noqa
            pass
        echo("  ℹ️ swap 创建失败，继续运行")


# === dpkg 状态自愈 ===
def check_dpkg_clean():
    if not have_cmd("dpkg"):
        return
    broken = run(["dpkg", "--audit"], timeout=60).stdout.strip()
    if not broken:
        return
    echo("⚠️ 检测到 dpkg 状态异常，正在自动修复...")

    # 第一轮：常规修复
    run(["dpkg", "--configure", "-a"], timeout=600,
        env_extra={"DEBIAN_FRONTEND": "noninteractive"})
    apt(["--fix-broken", "install", "-y"])
    broken = run(["dpkg", "--audit"], timeout=60).stdout.strip()
    if not broken:
        echo("✅ dpkg 自动修复完成")
        return

    # 第二轮：仅移除 dpkg --audit 报告的异常包（跳过系统关键包）
    echo("⚠️ 常规修复失败，仅移除 dpkg --audit 报告的异常包...")
    critical = {"apt", "bash", "coreutils", "dpkg", "libc6", "systemd",
                "util-linux", "base-files", "base-passwd", "dash"}
    pkgs = set()
    for line in broken.splitlines():
        if line.startswith(" "):
            parts = line.split()
            if parts:
                pkgs.add(parts[0])
    for pkg in sorted(pkgs):
        if pkg in critical:
            echo(f"  ⚠️ 跳过系统关键包: {pkg}")
            continue
        echo(f"  🔧 强制移除: {pkg}")
        run(["dpkg", "--remove", "--force-remove-reinstreq", pkg], timeout=120)

    apt(["--fix-broken", "install", "-y"])
    apt(["autoremove", "-y"])
    if run(["dpkg", "--audit"], timeout=60).stdout.strip():
        echo("❌ dpkg 自动修复失败，请手动处理后重试")
        sys.exit(1)
    echo("✅ dpkg 异常包已清理，环境恢复正常")


# === conntrack / qdisc 可用性探测 ===
def conntrack_available():
    if has_sysctl_key("net.netfilter.nf_conntrack_max"):
        return True
    if os.path.isdir("/proc/sys/net/netfilter") and \
            glob.glob("/proc/sys/net/netfilter/nf_conntrack*"):
        return True
    return os.path.isfile("/proc/net/nf_conntrack")


def try_set_qdisc(q):
    if not has_sysctl_key("net.core.default_qdisc"):
        return False
    return set_sysctl("net.core.default_qdisc", q)


# === Sysctl 权威收敛（避免多脚本互相覆盖）===
def sysctl_file_hits_keys(path):
    content = read_text(path)
    for k in SYSCTL_KEYS:
        if re.search(rf"^\s*{re.escape(k)}\s*=", content, re.M):
            return True
    return False


def backup_and_disable_sysctl_file(path):
    if not os.path.isfile(path) or not sysctl_file_hits_keys(path):
        return
    os.makedirs(SYSCTL_BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    echo(f"🧯 发现冲突 sysctl 文件：{path}")
    shutil.copy2(path, f"{SYSCTL_BACKUP_DIR}/{os.path.basename(path)}.bak-{ts}")
    os.rename(path, f"{path}.disabled-by-net-optimize-{ts}")
    echo(f"  ✅ 已备份并禁用：{path}")


def converge_sysctl_authority():
    echo(f"🧠 收敛 sysctl 权威（以 {SYSCTL_AUTH_FILE} 为准，保证 last-wins）...")
    if not os.path.isfile(SYSCTL_AUTH_FILE):
        echo(f"⚠️ 未发现：{SYSCTL_AUTH_FILE}，跳过")
        return

    main_content = read_text(SYSCTL_AUTH_FILE)
    want = {}
    for k in SYSCTL_KEYS:
        vals = re.findall(rf"^\s*{re.escape(k)}\s*=\s*(.+?)\s*$", main_content, re.M)
        vals = [v for v in vals if not v.startswith("#")]
        if vals:
            want[k] = vals[-1]
    if not want:
        echo(f"⚠️ {SYSCTL_AUTH_FILE} 未解析到关键项，跳过")
        return

    # 1) 生成 override（最后加载，保证 last-wins）
    lines = ["# Net-Optimize: override to guarantee last-wins",
             f"# Generated: {datetime.now(timezone.utc).strftime('%F %T UTC')}"]
    lines += [f"{k} = {want[k]}" for k in SYSCTL_KEYS if k in want]
    write_text(SYSCTL_OVERRIDE_FILE, "\n".join(lines) + "\n")
    echo(f"✅ 写入 override：{SYSCTL_OVERRIDE_FILE}")

    # 2) 禁用 /etc/sysctl.d 里冲突文件（保留 main + override）
    for f in sorted(glob.glob("/etc/sysctl.d/*.conf")):
        if f in (SYSCTL_AUTH_FILE, SYSCTL_OVERRIDE_FILE):
            continue
        backup_and_disable_sysctl_file(f)

    # 3) /etc/sysctl.conf 冲突项注释掉
    if os.path.isfile("/etc/sysctl.conf"):
        content, hit = read_text("/etc/sysctl.conf"), False
        for k in SYSCTL_KEYS:
            new = re.sub(rf"^\s*({re.escape(k)}\s*=.*)$",
                         r"# net-optimize disabled: \1", content, flags=re.M)
            if new != content:
                content, hit = new, True
        if hit:
            write_text("/etc/sysctl.conf", content)
            echo("✅ 已削弱冲突：/etc/sysctl.conf")

    # 4) 立即落地 + 验证（跳过可能由外部内核脚本管控的 qdisc/cc）
    run(["sysctl", "--system"], timeout=60)
    for k, v in want.items():
        set_sysctl(k, v)
        if k in ("net.core.default_qdisc", "net.ipv4.tcp_congestion_control"):
            continue
        actual = re.sub(r"\s+", " ", get_sysctl(k)).strip()
        expected = re.sub(r"\s+", " ", v).strip()
        if actual != expected:
            proc_path = "/proc/sys/" + k.replace(".", "/")
            if write_proc(proc_path, v):
                echo(f"  ⚠️ {k} 被外部覆盖，已强制恢复")
    echo("✅ sysctl 收敛完成（override 已保证 last-wins）")


def force_apply_sysctl_runtime():
    echo("🧷 强制写入 sysctl runtime（防止云镜像/agent 覆盖）")
    run(["sysctl", "--system"], timeout=60)
    if has_sysctl_key("net.ipv4.conf.all.rp_filter"):
        set_sysctl("net.ipv4.conf.all.rp_filter", Cfg.RP_FILTER)
        set_sysctl("net.ipv4.conf.default.rp_filter", Cfg.RP_FILTER)
        for p in glob.glob("/proc/sys/net/ipv4/conf/*/rp_filter"):
            write_proc(p, Cfg.RP_FILTER)
        echo(f"  ✅ rp_filter 已逐接口强制覆盖为 {Cfg.RP_FILTER}")


# === 清理旧配置 ===
def clean_old_config():
    echo("🧹 清理旧配置...")
    need_clean = os.path.isfile("/etc/systemd/system/net-optimize.service") \
        or os.path.isdir(CONFIG_DIR)
    if not need_clean:
        for cmd in ("iptables", "iptables-legacy", "ip6tables", "ip6tables-legacy"):
            if have_cmd(cmd) and any(re.search(r"TCPMSS|DSCP", l)
                                     for l in ipt_rules(cmd)):
                need_clean = True
                break
    if not need_clean:
        echo("✅ 未发现旧配置，跳过清理")
        os.makedirs(CONFIG_DIR, exist_ok=True)
        return

    echo("🔎 发现旧配置，开始清理...")
    run(["systemctl", "stop", "net-optimize.service"], timeout=10)
    run(["systemctl", "disable", "net-optimize.service"], timeout=10)
    try:
        os.remove("/etc/systemd/system/net-optimize.service")
    except Exception:  # noqa
        pass

    # 清理所有后端的 TCPMSS + DSCP 规则（IPv4 + IPv6）
    ipt_clear(V4_CMDS + V6_CMDS, r"TCPMSS|DSCP")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    for f in (CONFIG_FILE, MODULES_FILE):
        try:
            os.remove(f)
        except Exception:  # noqa
            pass
    echo("✅ 旧配置清理完成")


# === nginx 源自愈（Ubuntu/Debian 混源、noble 混淆检测）===
def nginx_sources_selfheal(distro):
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    for f in glob.glob("/etc/apt/sources.list.d/*nginx*.list") + \
            glob.glob("/etc/apt/sources.list.d/*nginx*.sources"):
        content = read_text(f)
        if distro == "ubuntu" and re.search(r"nginx\.org/packages(/mainline)?/debian", content):
            os.rename(f, f"{f}.disabled.{ts}")
            echo(f"🧹 [APT自愈] Ubuntu 检测到 nginx Debian 源，已禁用：{os.path.basename(f)}")
        elif distro == "debian" and re.search(r"nginx\.org/packages(/mainline)?/ubuntu", content):
            os.rename(f, f"{f}.disabled.{ts}")
            echo(f"🧹 [APT自愈] Debian 检测到 nginx Ubuntu 源，已禁用：{os.path.basename(f)}")
        elif re.search(r"nginx\.org/packages(/mainline)?/debian.*\bnoble\b", content):
            os.rename(f, f"{f}.disabled.{ts}")
            echo(f"🧹 [APT自愈] 检测到 debian 路径却使用 noble，已禁用：{os.path.basename(f)}")


# === 工具安装（可选，含 APT 源自愈）===
def maybe_install_tools():
    if Cfg.SKIP_APT:
        echo("⏭️ 跳过工具安装（SKIP_APT=1）")
        return
    if not have_cmd("apt-get"):
        echo("ℹ️ 非APT系统，跳过工具安装")
        return

    distro, _ = detect_distro()
    nginx_sources_selfheal(distro)

    echo("🧰 安装必要工具...")
    check_dpkg_clean()
    if apt(["update", "-y"]).returncode != 0:
        echo("⚠️ apt update 失败（已忽略，不影响主流程）")

    packages = ("ca-certificates curl wget gnupg2 lsb-release "
                "ethtool iproute2 irqbalance chrony "
                "nftables conntrack iptables iptables-persistent "
                "software-properties-common apt-transport-https").split()
    if apt(["install", "-y", "--no-install-recommends"] + packages).returncode != 0:
        echo("⚠️ 部分包安装失败（已忽略）")
    run(["systemctl", "enable", "--now", "irqbalance", "chrony"], timeout=30)


# === Ulimit 优化 ===
def setup_ulimit():
    echo("📂 优化文件描述符限制...")
    write_text("/etc/security/limits.d/99-net-optimize.conf",
               "# Net-Optimize Ultimate - File Descriptor Limits\n"
               "*    soft nofile 1048576\n"
               "*    hard nofile 1048576\n"
               "root soft nofile 1048576\n"
               "root hard nofile 1048576\n")

    sysconf = read_text("/etc/systemd/system.conf")
    if re.search(r"^DefaultLimitNOFILE=", sysconf, re.M):
        sysconf = re.sub(r"^DefaultLimitNOFILE=.*$",
                         "DefaultLimitNOFILE=1048576", sysconf, flags=re.M)
    else:
        sysconf += "\nDefaultLimitNOFILE=1048576\n"
    write_text("/etc/systemd/system.conf", sysconf)

    for pam_file in ("/etc/pam.d/common-session",
                     "/etc/pam.d/common-session-noninteractive"):
        if os.path.isfile(pam_file) and "pam_limits.so" not in read_text(pam_file):
            with open(pam_file, "a") as f:
                f.write("session required pam_limits.so\n")

    run(["systemctl", "daemon-reload"], timeout=30)
    echo("✅ ulimit 配置完成")


# === 拥塞控制与队列算法 ===
def setup_tcp_congestion():
    global FINAL_CC, FINAL_QDISC
    echo("📶 设置TCP拥塞算法和队列...")

    if Cfg.AGGRESSIVE_MODE:
        # 激进模式：pfifo_fast 不限速，不做公平调度
        if try_set_qdisc("pfifo_fast"):
            FINAL_QDISC = "pfifo_fast"
        elif try_set_qdisc("fq"):
            FINAL_QDISC = "fq"
        else:
            FINAL_QDISC = get_sysctl("net.core.default_qdisc")
        echo("  ⚡ 激进模式：队列算法 pfifo_fast（无限速）")
    else:
        if Cfg.ENABLE_FQ_PIE and try_set_qdisc("fq_pie"):
            FINAL_QDISC = "fq_pie"
        elif try_set_qdisc("fq"):
            FINAL_QDISC = "fq"
        elif try_set_qdisc("pie"):
            FINAL_QDISC = "pie"
        else:
            FINAL_QDISC = get_sysctl("net.core.default_qdisc")

    available = get_sysctl("net.ipv4.tcp_available_congestion_control").split()
    target_cc = "cubic"
    # 优先级：bbr3 > bbr2 > bbrplus > bbr > cubic
    for cc in ("bbr3", "bbr2", "bbrplus", "bbr"):
        if cc in available:
            target_cc = cc
            break
    if has_sysctl_key("net.ipv4.tcp_congestion_control"):
        set_sysctl("net.ipv4.tcp_congestion_control", target_cc)
    FINAL_CC = get_sysctl("net.ipv4.tcp_congestion_control")

    echo(f"✅ 最终生效拥塞算法: {FINAL_CC}")
    echo(f"✅ 最终生效队列算法: {FINAL_QDISC}")
    if target_cc.startswith("bbr") and FINAL_CC != target_cc:
        echo(f"⚠️ 提示: 尝试启用 {target_cc} 失败，系统自动回退到了 {FINAL_CC}")


# === Sysctl 深度整合 ===
def write_sysctl_conf():
    echo("📊 写入内核参数配置文件...")

    total_ram_kb = meminfo_kb("MemTotal") or 1048576
    if Cfg.RAM_ADAPTIVE_BUFFERS:
        if total_ram_kb >= 8000000:
            rmem_max = wmem_max = 268435456   # ≥8GB: 256MB
        elif total_ram_kb >= 4000000:
            rmem_max = wmem_max = 134217728   # ≥4GB: 128MB
        elif total_ram_kb >= 2000000:
            rmem_max = wmem_max = 67108864    # ≥2GB: 64MB
        else:
            rmem_max = wmem_max = 33554432    # <2GB: 32MB
    else:
        rmem_max = wmem_max = 67108864
    rmem_default = wmem_default = 262144

    # tcp_mem: min/pressure/max（单位：页，4KB/页），RAM 的 1/32、1/8、1/4 + 下限
    tcp_mem_min = max(total_ram_kb // 4 // 32, 8192)
    tcp_mem_pressure = max(total_ram_kb // 4 // 8, 32768)
    tcp_mem_max = max(total_ram_kb // 4 // 4, 65536)

    cc = FINAL_CC or get_sysctl("net.ipv4.tcp_congestion_control")
    qdisc = FINAL_QDISC or get_sysctl("net.core.default_qdisc")
    L = []
    a = L.append

    a("# =========================================================")
    a(f"# 🚀 Net-Optimize Ultimate v{VERSION} - Kernel Parameters")
    a(f"# Generated: {datetime.now(timezone.utc).strftime('%F %T UTC')}")
    a("# =========================================================")
    a("")
    a("# === 拥塞控制 / 队列 ===")
    a(f"net.core.default_qdisc = {qdisc}")
    a(f"net.ipv4.tcp_congestion_control = {cc}")
    a("")
    a("# === 基础网络设置 ===")
    a("net.core.netdev_max_backlog = 250000")
    a("net.core.somaxconn = 1000000")
    a("net.ipv4.tcp_max_syn_backlog = 819200")
    a("net.ipv4.tcp_syncookies = 1")
    a("")
    a("# === 网卡收包预算 ===")
    a("net.core.netdev_budget = 600")
    a("net.core.netdev_budget_usecs = 3000")
    a("")
    a("# === 连接生命周期 ===")
    a("net.ipv4.tcp_fin_timeout = 15")
    a("net.ipv4.tcp_keepalive_time = 600")
    a("net.ipv4.tcp_keepalive_intvl = 15")
    a("net.ipv4.tcp_keepalive_probes = 2")
    a("net.ipv4.tcp_max_tw_buckets = 32768")
    a("net.ipv4.ip_local_port_range = 1024 65535")
    if Cfg.RESERVED_PORTS:
        # 代理节点监听口从临时端口池保留，避免出站临时端口撞上节点监听口
        a(f"net.ipv4.ip_local_reserved_ports = {Cfg.RESERVED_PORTS}")
    a("")
    a("# === TCP算法优化 ===")
    a(f"net.ipv4.tcp_mtu_probing = {Cfg.ENABLE_MTU_PROBE}")
    a("net.ipv4.tcp_window_scaling = 1")
    a("net.ipv4.tcp_sack = 1")
    a("net.ipv4.tcp_slow_start_after_idle = 0")
    a("net.ipv4.tcp_no_metrics_save = 0")
    a("net.ipv4.tcp_ecn = 1")
    a("net.ipv4.tcp_ecn_fallback = 1")
    a(f"net.ipv4.tcp_notsent_lowat = {Cfg.TCP_NOTSENT_LOWAT}")
    a("net.ipv4.tcp_fastopen = 3")
    a("net.ipv4.tcp_timestamps = 1")
    a("net.ipv4.tcp_autocorking = 0")
    a("net.ipv4.tcp_orphan_retries = 1")
    a("net.ipv4.tcp_retries2 = 15")
    a("net.ipv4.tcp_synack_retries = 1")
    a("net.ipv4.tcp_early_retrans = 3")
    a("net.ipv4.tcp_thin_linear_timeouts = 1")  # 游戏/交互小包流：线性重传替代指数退避
    a("")
    a("# === 低延迟轮询（默认关闭：全局忙等在小核数 VPS 上纯烧 CPU）===")
    a("net.core.busy_poll = 0")    # 专用大核机器可自行改 50 启用忙等轮询
    a("net.core.busy_read = 0")
    a("")
    # 注：tcp_low_latency 在 4.14+ 已移除；tcp_fack / tcp_frto 在 BBR 下无实际作用
    a("# === 内存缓冲区优化（基于物理内存动态计算，default=256KB 让 TCP autotuning 自行扩展）===")
    a(f"# 当前系统内存: {total_ram_kb // 1024} MB → rmem/wmem_max = {rmem_max // 1024 // 1024} MB")
    a(f"net.core.rmem_max = {rmem_max}")
    a(f"net.core.wmem_max = {wmem_max}")
    a(f"net.core.rmem_default = {rmem_default}")
    a(f"net.core.wmem_default = {wmem_default}")
    a("net.core.optmem_max = 65536")
    a(f"net.ipv4.tcp_rmem = 4096 87380 {rmem_max}")
    a(f"net.ipv4.tcp_wmem = 4096 65536 {wmem_max}")
    a("net.ipv4.udp_rmem_min = 16384")
    a("net.ipv4.udp_wmem_min = 16384")
    a(f"net.ipv4.udp_mem = {tcp_mem_min} {tcp_mem_pressure} {tcp_mem_max}")
    a(f"net.ipv4.tcp_mem = {tcp_mem_min} {tcp_mem_pressure} {tcp_mem_max}")
    a("")
    if Cfg.AGGRESSIVE_MODE:
        a("# === 激进模式参数（抢带宽）===")
        a("net.core.netdev_max_backlog = 1000000")
        a("net.ipv4.tcp_retries2 = 15")
        a("net.ipv4.tcp_slow_start_after_idle = 0")
        a("net.ipv4.tcp_no_metrics_save = 1")
        a("net.ipv4.tcp_notsent_lowat = 131072")
        a("net.ipv4.tcp_max_syn_backlog = 2097152")
        a("net.ipv4.udp_rmem_min = 65536")
        a("net.ipv4.udp_wmem_min = 65536")
        a("net.ipv4.udp_mem = 131072 262144 524288")
        a("net.ipv4.tcp_orphan_retries = 3")
        a("")
    a("# === 路由/转发 ===")
    a("net.ipv4.ip_forward = 1")
    a("net.ipv4.conf.all.forwarding = 1")
    a("net.ipv4.conf.default.forwarding = 1")
    a("net.ipv4.conf.all.route_localnet = 1")
    a(f"net.ipv4.conf.all.rp_filter = {Cfg.RP_FILTER}")
    a(f"net.ipv4.conf.default.rp_filter = {Cfg.RP_FILTER}")
    a("")
    a("# === 安全加固 ===")
    a("net.ipv4.conf.all.accept_redirects = 0")
    a("net.ipv4.conf.default.accept_redirects = 0")
    a("net.ipv4.conf.all.secure_redirects = 0")
    a("net.ipv4.conf.default.secure_redirects = 0")
    a("net.ipv4.conf.all.send_redirects = 0")
    a("net.ipv4.conf.default.send_redirects = 0")
    a("net.ipv4.icmp_echo_ignore_broadcasts = 1")
    a("net.ipv4.icmp_ignore_bogus_error_responses = 1")
    a("net.ipv4.icmp_echo_ignore_all = 0")
    a("")
    a("# === IPv6优化 ===")
    a("net.ipv6.conf.all.disable_ipv6 = 0")
    a("net.ipv6.conf.default.disable_ipv6 = 0")
    a("net.ipv6.conf.all.forwarding = 1")
    a("net.ipv6.conf.default.forwarding = 1")
    a("net.ipv6.conf.all.accept_ra = 2")
    a("net.ipv6.conf.default.accept_ra = 2")
    a("net.ipv6.conf.all.use_tempaddr = 2")
    a("net.ipv6.conf.default.use_tempaddr = 2")
    a("net.ipv6.conf.all.accept_redirects = 0")
    a("net.ipv6.conf.default.accept_redirects = 0")
    a("")
    a("# === 邻居表调优 ===")
    a("net.ipv4.neigh.default.gc_thresh1 = 2048")
    a("net.ipv4.neigh.default.gc_thresh2 = 4096")
    a("net.ipv4.neigh.default.gc_thresh3 = 8192")
    a("net.ipv6.neigh.default.gc_thresh1 = 2048")
    a("net.ipv6.neigh.default.gc_thresh2 = 4096")
    a("net.ipv6.neigh.default.gc_thresh3 = 8192")
    a("net.ipv4.neigh.default.unres_qlen = 10000")
    a("")
    a("# === 内核/文件系统安全 ===")
    a("kernel.kptr_restrict = 1")
    a("kernel.yama.ptrace_scope = 1")
    a("kernel.sysrq = 176")
    a("vm.mmap_min_addr = 65536")
    a("vm.max_map_count = 1048576")
    a("vm.swappiness = 1")
    a("vm.overcommit_memory = 2")   # 适度超量，避免 =1 彻底关闭 OOM 保护
    a("vm.overcommit_ratio = 100")  # 无 swap 小内存机 commit 上限修复
    a("kernel.pid_max = 4194304")
    a("")
    a("fs.protected_fifos = 1")
    a("fs.protected_hardlinks = 1")
    a("fs.protected_regular = 2")
    a("fs.protected_symlinks = 1")
    a("")
    if Cfg.ENABLE_CONNTRACK_TUNE:
        a("# === 连接跟踪优化 ===")
        a(f"net.netfilter.nf_conntrack_max = {Cfg.NFCT_MAX}")
        a("net.netfilter.nf_conntrack_udp_timeout = 30")
        a("net.netfilter.nf_conntrack_udp_timeout_stream = 180")
        a("net.netfilter.nf_conntrack_tcp_timeout_established = 432000")
        a("net.netfilter.nf_conntrack_tcp_timeout_time_wait = 120")
        a("net.netfilter.nf_conntrack_tcp_timeout_close_wait = 60")
        a("net.netfilter.nf_conntrack_tcp_timeout_fin_wait = 120")
        a("")

    write_text(SYSCTL_AUTH_FILE, "\n".join(L) + "\n")
    if run(["sysctl", "-e", "--system"], timeout=60).returncode != 0:
        echo("⚠️ 部分参数不支持，但不影响其他项")
    echo(f"✅ sysctl 参数已写入并应用：{SYSCTL_AUTH_FILE}")


# === 连接跟踪模块加载 + 触发 ===
CONNTRACK_MODULES = ["nf_conntrack", "nf_conntrack_netlink",
                     "nf_conntrack_ftp", "nf_nat", "xt_MASQUERADE"]


def conntrack_invalid_drop_rules():
    """INVALID -> DROP 触发规则（主流程与 --boot 共用）。"""
    if not have_cmd("iptables"):
        return False
    for chain in ("INPUT", "OUTPUT"):
        chk = run(["iptables", "-t", "filter", "-C", chain, "-m", "conntrack",
                   "--ctstate", "INVALID", "-j", "DROP"], timeout=5)
        if chk.returncode != 0:
            run(["iptables", "-t", "filter", "-I", chain, "1", "-m", "conntrack",
                 "--ctstate", "INVALID", "-j", "DROP"], timeout=5)
    return True


def setup_conntrack():
    if not Cfg.ENABLE_CONNTRACK_TUNE:
        echo("⏭️ 跳过连接跟踪调优")
        return
    echo("🔗 连接跟踪（conntrack）初始化...")

    for m in CONNTRACK_MODULES:
        run(["modprobe", m], timeout=10)

    write_text(CONNTRACK_MODULES_CONF,
               "# Net-Optimize: conntrack/nat modules\n" +
               "".join(f"{m}\n" for m in CONNTRACK_MODULES))
    echo(f"  ✅ 已写入开机模块加载: {CONNTRACK_MODULES_CONF}")
    write_text(MODULES_FILE, "".join(f"{m}\n" for m in sorted(set(CONNTRACK_MODULES))))
    run(["systemctl", "restart", "systemd-modules-load"], timeout=30)

    # conntrack 哈希桶扩容至 max/4（高并发中转降低查表碰撞）
    hashsize = Cfg.NFCT_MAX // 4
    if write_proc("/sys/module/nf_conntrack/parameters/hashsize", hashsize):
        echo(f"  ✅ conntrack hashsize={hashsize}（max/4，降低哈希碰撞）")
    write_text("/etc/modprobe.d/net-optimize-conntrack.conf",
               f"options nf_conntrack hashsize={hashsize}\n")

    if conntrack_invalid_drop_rules():
        echo("  ✅ 已写入 conntrack 触发规则（INVALID -> DROP）：INPUT/OUTPUT")

    cnt = read_text("/proc/sys/net/netfilter/nf_conntrack_count").strip()
    if cnt:
        echo(f"  🔎 nf_conntrack_count={cnt}")
    echo("✅ 连接跟踪配置完成")


# === 网卡 Offload 优化（GRO/GSO/TSO + UDP GRO 转发 + LRO off）===
def setup_nic_offload():
    if not Cfg.ENABLE_NIC_OFFLOAD:
        echo("⏭️ 跳过网卡 offload 优化")
        return
    echo("🔧 网卡 offload 优化...")
    if not have_cmd("ethtool"):
        echo("  ⚠️ ethtool 未安装，跳过")
        return
    iface = detect_outbound_iface()
    if not iface:
        echo("  ⚠️ 无法检测出口网卡，跳过")
        return

    # 直接逐项尝试开启并统计成功数（v3.8.0 用 grep 短名匹配 ethtool -k 长名
    # 输出永远不命中，实际从未触发开启；此处修正为直接尝试，行为只增不减）
    applied = 0
    for feature in ("gro", "gso", "tso", "sg", "rx", "tx"):
        if run(["ethtool", "-K", iface, feature, "on"], timeout=5).returncode == 0:
            applied += 1
    run(["ethtool", "-K", iface, "tx-nocache-copy", "on"], timeout=5)

    # 关闭 LRO：本机开启了 ip_forward，LRO 合并后的包无法安全转发
    run(["ethtool", "-K", iface, "lro", "off"], timeout=5)

    # UDP GRO 转发 + 发送端分段：QUIC/Hysteria2/TUIC 降 CPU（内核 5.4+/6.x）
    if run(["ethtool", "-K", iface, "rx-udp-gro-forwarding", "on"], timeout=5).returncode == 0:
        echo("  ✅ UDP GRO 转发已开启（QUIC/UDP 代理加速）")
    else:
        echo("  ℹ️ UDP GRO 转发：网卡不支持或内核版本不足，已跳过")
    run(["ethtool", "-K", iface, "tx-udp-segmentation", "on"], timeout=5)

    # 激进模式：加大发送队列 + ring buffer
    if Cfg.AGGRESSIVE_MODE:
        run(["ip", "link", "set", iface, "txqueuelen", "10000"], timeout=5)
        m = re.search(r"qlen (\d+)", run(["ip", "link", "show", iface], timeout=5).stdout)
        actual = int(m.group(1)) if m else 0
        if actual >= 10000:
            echo(f"  ⚡ 激进模式: txqueuelen={actual}")
        else:
            echo(f"  ⚠️ 激进模式: txqueuelen 设置未生效（当前 {actual or 'unknown'}，可能网卡不支持）")

        gout = run(["ethtool", "-g", iface], timeout=5).stdout
        pre = re.search(r"Pre-set maximums:.*?RX:\s*(\d+).*?TX:\s*(\d+)", gout, re.S)
        if pre:
            rx_max, tx_max = pre.group(1), pre.group(2)
            if int(rx_max) > 0:
                run(["ethtool", "-G", iface, "rx", rx_max], timeout=5)
            if int(tx_max) > 0:
                run(["ethtool", "-G", iface, "tx", tx_max], timeout=5)
        echo("  ⚡ 激进模式: ring buffer 已最大化")

    echo(f"  ✅ 出口网卡: {iface}，offload 已检查（成功开启 {applied} 项）")

    # 持久化：udev 规则开机自动应用
    udev = ["# Net-Optimize: NIC offload 持久化",
            f'ACTION=="add", SUBSYSTEM=="net", NAME=="{iface}", '
            f'RUN+="/usr/sbin/ethtool -K {iface} gro on gso on tso on sg on '
            f'tx-nocache-copy on lro off"',
            f'ACTION=="add", SUBSYSTEM=="net", NAME=="{iface}", '
            f"RUN+=\"/bin/sh -c '/usr/sbin/ethtool -K {iface} "
            f"rx-udp-gro-forwarding on tx-udp-segmentation on 2>/dev/null || true'\""]
    if Cfg.AGGRESSIVE_MODE:
        udev.append(f'ACTION=="add", SUBSYSTEM=="net", NAME=="{iface}", '
                    f'RUN+="/usr/sbin/ip link set {iface} txqueuelen 10000"')
    write_text("/etc/udev/rules.d/99-net-optimize-offload.rules", "\n".join(udev) + "\n")
    echo("  ✅ offload 持久化：/etc/udev/rules.d/99-net-optimize-offload.rules")

    # 自适应中断合并（降低延迟，平衡吞吐）
    if Cfg.ENABLE_IRQ_COALESCING:
        if run(["ethtool", "-C", iface, "adaptive-rx", "on", "adaptive-tx", "on"],
               timeout=5).returncode == 0:
            echo("  ✅ 中断合并：自适应模式已开启（adaptive-rx/tx on）")
        elif run(["ethtool", "-C", iface, "rx-usecs", "50", "tx-usecs", "50"],
                 timeout=5).returncode == 0:
            echo("  ✅ 中断合并：固定 50μs (rx/tx-usecs)")
        else:
            echo("  ℹ️ 中断合并：网卡不支持，已跳过")

    # WireGuard 接口 UDP GRO 转发（降低 WG 中转 CPU 占用）
    if Cfg.ENABLE_WG_OPT:
        r = run(["ip", "-o", "link", "show", "type", "wireguard"], timeout=5)
        wg_ifaces = [l.split(": ")[1].split("@")[0] for l in r.stdout.splitlines()
                     if ": " in l]
        if wg_ifaces:
            for wg in wg_ifaces:
                run(["ethtool", "-K", wg, "rx-udp-gro-forwarding", "on"], timeout=5)
            echo("  ✅ WireGuard 接口 UDP GRO 转发已开启")
        else:
            echo("  ℹ️ 未检测到 WireGuard 接口，跳过 WG 接口 GRO")


# === RPS/RFS 多核收包均衡 + XPS 发送端分发 ===
def setup_rps_rfs():
    if not Cfg.ENABLE_RPS_RFS:
        echo("⏭️ 跳过 RPS/RFS 配置")
        return
    echo("🔧 RPS/RFS 多核收包均衡...")
    iface = detect_outbound_iface()
    if not iface:
        echo("  ⚠️ 无法检测出口网卡，跳过")
        return
    ncpu = os.cpu_count() or 1
    if ncpu <= 1:
        echo("  ℹ️ 单核 CPU，RPS/RFS 无需配置")
        return

    cpu_mask = format((1 << ncpu) - 1, "x")
    rps_applied = 0
    for q in glob.glob(f"/sys/class/net/{iface}/queues/rx-*/rps_cpus"):
        if write_proc(q, cpu_mask):
            rps_applied += 1

    rfs_entries = 32768 * ncpu
    write_proc("/proc/sys/net/core/rps_sock_flow_entries", rfs_entries)
    rfs_per_queue = rfs_entries // (rps_applied if rps_applied > 0 else 1)
    for q in glob.glob(f"/sys/class/net/{iface}/queues/rx-*/rps_flow_cnt"):
        write_proc(q, rfs_per_queue)

    echo(f"  ✅ {iface}: RPS 掩码={cpu_mask} ({ncpu}核), "
         f"RFS entries={rfs_entries}, queues={rps_applied}")

    # 持久化：systemd-tmpfiles
    lines = ["# Net-Optimize: RPS/RFS 持久化",
             f"w /proc/sys/net/core/rps_sock_flow_entries - - - - {rfs_entries}"]
    for q in glob.glob(f"/sys/class/net/{iface}/queues/rx-*/rps_cpus"):
        lines.append(f"w {q} - - - - {cpu_mask}")
    for q in glob.glob(f"/sys/class/net/{iface}/queues/rx-*/rps_flow_cnt"):
        lines.append(f"w {q} - - - - {rfs_per_queue}")

    # XPS：发包绑核，与 RPS 配合减少跨核锁竞争
    if Cfg.ENABLE_XPS:
        xps_applied = 0
        for q in glob.glob(f"/sys/class/net/{iface}/queues/tx-*/xps_cpus"):
            if write_proc(q, cpu_mask):
                xps_applied += 1
                lines.append(f"w {q} - - - - {cpu_mask}")
        if xps_applied > 0:
            echo(f"  ✅ XPS 已配置：{xps_applied} 个 TX 队列绑定掩码={cpu_mask}")
        else:
            echo("  ℹ️ XPS：未发现可配置的 TX 队列（单队列网卡或不支持）")

    write_text("/etc/tmpfiles.d/net-optimize-rps.conf", "\n".join(lines) + "\n")
    echo("  ✅ RPS/RFS 持久化：/etc/tmpfiles.d/net-optimize-rps.conf")


# === QUIC/UDP DSCP 优先级标记（EF）===
DSCP_EF_RULE = ["-p", "udp", "--dport", "443", "-j", "DSCP", "--set-dscp-class", "EF"]
DSCP_AF41_RULE = ["-p", "udp", "!", "--dport", "443", "-m", "length",
                  "--length", "0:200", "-j", "DSCP", "--set-dscp-class", "AF41"]
DSCP_AF41_RULE_NOLEN = ["-p", "udp", "!", "--dport", "443",
                        "-j", "DSCP", "--set-dscp-class", "AF41"]


def setup_dscp_marking():
    if not Cfg.ENABLE_DSCP:
        echo("⏭️ 跳过 DSCP 标记")
        return
    echo("🏷️ QUIC/UDP DSCP 优先级标记...")

    ipt = saved_ipt_backend()
    if not ipt:
        echo("  ⚠️ iptables 不可用，跳过")
        return
    iface = detect_outbound_iface()

    # DSCP EF (46=0x2E)：UDP 443 (QUIC) 出口流量加速
    ipt_clear(V4_CMDS, PAT_EF)
    ipt_add(ipt, DSCP_EF_RULE, iface)
    ipt_dedup(ipt, PAT_DSCP)

    ip6 = ip6_cmd_for(ipt)
    if ip6:
        ipt_clear([ip6], PAT_EF)
        ipt_add(ip6, DSCP_EF_RULE, iface)
        ipt_dedup(ip6, PAT_DSCP)

    echo(f"  ✅ UDP 443 (QUIC) DSCP=EF 已标记（{ipt}）")


# === CPU 调频策略优化 ===
def setup_cpu_governor():
    if not Cfg.ENABLE_CPU_GOVERNOR:
        echo("⏭️ 跳过 CPU 调频优化")
        return
    echo("⚡ CPU 调频策略优化（performance 模式）...")

    gov_files = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    if not gov_files:
        echo("  ℹ️ 未发现 cpufreq 接口（容器/虚拟化环境不支持，已跳过）")
        return
    changed = 0
    for g in gov_files:
        if read_text(g).strip() != "performance" and write_proc(g, "performance"):
            changed += 1
    if changed > 0:
        echo(f"  ✅ {changed}/{len(gov_files)} 个 CPU 核心已切换到 performance 模式")
    else:
        echo(f"  ✅ CPU 调频已是 performance 模式（{len(gov_files)} 核心，无需变更）")

    if have_cmd("cpupower"):
        run(["cpupower", "frequency-set", "-g", "performance"], timeout=10)
    lines = ["# Net-Optimize: CPU performance governor"]
    lines += [f"w {g} - - - - performance" for g in gov_files]
    write_text("/etc/tmpfiles.d/net-optimize-cpufreq.conf", "\n".join(lines) + "\n")
    echo("  ✅ CPU 调频持久化：/etc/tmpfiles.d/net-optimize-cpufreq.conf")


# === MPTCP 多路径传输（内核 5.6+）===
def setup_mptcp():
    if not Cfg.ENABLE_MPTCP:
        echo("⏭️ 跳过 MPTCP 配置")
        return
    echo("🔀 MPTCP 多路径传输检测...")
    if not has_sysctl_key("net.mptcp.enabled"):
        echo("  ℹ️ 内核不支持 MPTCP（需要 5.6+），已跳过")
        return
    set_sysctl("net.mptcp.enabled", 1)
    if get_sysctl("net.mptcp.enabled") == "1":
        echo("  ✅ MPTCP 已启用")
        write_text("/etc/sysctl.d/98-net-optimize-mptcp.conf", "net.mptcp.enabled = 1\n")
    else:
        echo("  ⚠️ MPTCP 启用失败（可能被内核编译选项禁用）")


# === 自动检测线路质量 + initcwnd 调整 ===
def ping_avg_rtt(target):
    r = run(["ping", "-c", "3", "-W", "2", target], timeout=20)
    m = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
    return float(m.group(1)) if m else 0.0


def apply_initcwnd_routes(cwnd):
    """v4/v6 默认路由写 initcwnd/initrwnd（主流程、--boot、钩子共用）。"""
    r = run(["ip", "-4", "route", "show", "default"], timeout=5)
    gw = r.stdout.splitlines()[0] if r.stdout.strip() else ""
    if gw:
        parts = shlex.split(strip_route_params(gw))
        run(["ip", "route", "change"] + parts +
            ["initcwnd", str(cwnd), "initrwnd", str(cwnd)], timeout=5)
    r6 = run(["ip", "-6", "route", "show", "default"], timeout=5)
    gw6 = r6.stdout.splitlines()[0] if r6.stdout.strip() else ""
    if gw6:
        parts6 = shlex.split(strip_route_params(gw6))
        run(["ip", "-6", "route", "change"] + parts6 +
            ["initcwnd", str(cwnd), "initrwnd", str(cwnd)], timeout=5)
    return bool(gw), bool(gw6)


def setup_initcwnd():
    if not Cfg.ENABLE_INITCWND:
        echo("⏭️ 跳过 initcwnd 自动调整")
        return
    echo("📡 检测线路质量，自动调整 initcwnd...")

    rtts = [r for r in (ping_avg_rtt(t) for t in ("1.1.1.1", "8.8.8.8", "9.9.9.9"))
            if r > 0]
    avg_rtt = int(sum(rtts) / len(rtts)) if rtts else 0

    # 普通模式: <50ms→20 / 50-150ms→30 / >150ms→50；激进模式一律 64
    if Cfg.AGGRESSIVE_MODE:
        initcwnd = 64
        echo("  ⚡ 激进模式: initcwnd=64（最大初始窗口）")
    elif avg_rtt > 150:
        initcwnd = 50
    elif avg_rtt > 50:
        initcwnd = 30
    else:
        initcwnd = 20
    echo(f"  ℹ️ 平均 RTT: {avg_rtt}ms → initcwnd={initcwnd}")

    got4, got6 = apply_initcwnd_routes(initcwnd)
    if got4:
        echo(f"  ✅ IPv4 默认路由 initcwnd={initcwnd} initrwnd={initcwnd}")
    if got6:
        echo(f"  ✅ IPv6 默认路由 initcwnd={initcwnd} initrwnd={initcwnd}")

    config_set("INITCWND", initcwnd, only_if_exists=True)
    echo("  ✅ initcwnd 配置完成")


# === 路径 MTU 探测（DF 置位 ping 从大到小试探）===
def probe_path_mtu():
    # payload 1472→MTU1500(裸线路) 1452→1480(PPPoE) 1424→1452 1392→1420(WG/隧道)
    for size in (1472, 1452, 1424, 1392):
        for target in ("1.1.1.1", "8.8.8.8"):
            r = run(["ping", "-c1", "-W2", "-M", "do", "-s", str(size), target],
                    timeout=10)
            if r.returncode == 0:
                return size + 28
    return 0


# === MSS Clamping（IPv4 + IPv6，自动探测 + 后端检测 + 去重）===
def apply_one_tcpmss(cmd, iface, mss):
    return ipt_add(cmd, ["-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                         "-j", "TCPMSS", "--set-mss", str(mss)], iface)


def strip_persistent_mangle():
    """从 netfilter-persistent 保存文件里剥掉 mangle 段（保留 nat 端口跳跃）。"""
    for pf in ("/etc/iptables/rules.v4", "/etc/iptables/rules.v6"):
        content = read_text(pf)
        if content and re.search(r"^\*mangle$", content, re.M):
            new = re.sub(r"^\*mangle\n.*?^COMMIT\n", "", content, flags=re.M | re.S)
            write_text(pf, new)
            yield pf


def setup_mss_clamping():
    if not Cfg.ENABLE_MSS_CLAMP:
        echo("⏭️ 跳过MSS Clamping")
        return

    for pf in strip_persistent_mangle():
        echo(f"  ℹ️ 已从 {pf} 中移除 mangle 段")
    # 确保 netfilter-persistent 启用（负责恢复 nat 表端口跳跃规则）
    run(["systemctl", "enable", "netfilter-persistent"], timeout=10)

    # MSS 自动探测：按实际路径 MTU 推导（MTU-40），显式指定或探测失败保持原值
    mss = Cfg.MSS_VALUE
    if Cfg.MSS_AUTO and not Cfg.MSS_USER_SET and have_cmd("ping"):
        mtu = probe_path_mtu()
        if mtu:
            mss = mtu - 40
            echo(f"✅ 路径 MTU 探测: {mtu} → MSS={mss}")
        else:
            echo(f"ℹ️ 路径 MTU 探测失败（ICMP 不通），使用默认 MSS={mss}")
    Cfg.MSS_VALUE = mss

    echo(f"📡 设置MSS Clamping (MSS={mss})...")
    iface = detect_outbound_iface()
    if iface:
        echo(f"✅ 检测到出口接口: {iface}")
    else:
        echo("⚠️ 无法确定出口接口，将使用全局规则")

    # 模块加载必须在后端检测前执行
    for m in ("ip_tables", "iptable_mangle", "ip6_tables", "ip6table_mangle"):
        run(["modprobe", m], timeout=10)

    ipt = detect_ipt_backend()
    if not ipt:
        echo("⚠️ iptables 不可用，跳过")
        return
    echo(f"  ℹ️ iptables 后端: {ipt}")

    write_text(CONFIG_FILE,
               f"ENABLE_MSS_CLAMP=1\nCLAMP_IFACE={iface}\nMSS_VALUE={mss}\n"
               f"RP_FILTER={Cfg.RP_FILTER}\nIPT_BACKEND={ipt}\n")

    # 1) 所有后端强制清理 → 2) 检测后端写 1 条 → 3) 去重验证
    ipt_clear(V4_CMDS, PAT_TCPMSS)
    if apply_one_tcpmss(ipt, iface, mss):
        echo(f"✅ MSS 规则已写入（{ipt}）")
    else:
        echo(f"❌ MSS 写入失败（{ipt}）")
        return
    ipt_dedup(ipt, PAT_TCPMSS)

    cnt = ipt_count(ipt, PAT_TCPMSS)
    if cnt == 1:
        echo("✅ TCPMSS 规则数量：1（正常）")
    elif cnt == 0:
        echo("⚠️ TCPMSS 规则数量：0（写入可能失败）")
    else:
        echo(f"⚠️ TCPMSS 规则数量：{cnt}（仍有重复，可能有其他服务在加）")
    echo("✅ MSS Clamping 设置完成")

    # IPv6 MSS Clamping（IPv6 头比 IPv4 大 20 字节）
    if Cfg.ENABLE_IPV6_MSS:
        echo("📡 设置 IPv6 MSS Clamping...")
        ip6 = ip6_cmd_for(ipt)
        if ip6:
            ipt_clear([ip6], PAT_TCPMSS, max_rounds=40)
            ipv6_mss = mss - 20
            apply_one_tcpmss(ip6, iface, ipv6_mss)
            ipt_dedup(ip6, PAT_TCPMSS)
            echo(f"  ✅ IPv6 MSS={ipv6_mss} ({ip6}), 规则数：{ipt_count(ip6, PAT_TCPMSS)}")
        else:
            echo("  ℹ️ ip6tables 不可用，跳过 IPv6 MSS")

    # 最终完整性校验：确认 IPv4 TCPMSS 仍然存在
    if ipt_count(ipt, PAT_TCPMSS) == 0:
        echo("  ⚠️ IPv4 TCPMSS 在 IPv6 处理后消失，重新写入...")
        apply_one_tcpmss(ipt, iface, mss)
        if ipt_count(ipt, PAT_TCPMSS) >= 1:
            echo("  ✅ IPv4 TCPMSS 已恢复")
        else:
            echo("  ❌ IPv4 TCPMSS 恢复失败")


# === 激进模式：网卡 tc qdisc 覆盖 ===
def setup_aggressive_tc():
    if Cfg.ADAPTIVE_QOS or not Cfg.AGGRESSIVE_MODE:
        return
    echo("⚡ 激进模式：覆盖网卡 tc qdisc...")
    if not have_cmd("tc"):
        echo("  ⚠️ tc 命令不可用，跳过")
        return
    iface = detect_outbound_iface()
    if not iface:
        echo("  ⚠️ 无法检测出口网卡，跳过")
        return
    if run(["tc", "qdisc", "replace", "dev", iface, "root", "pfifo_fast"],
           timeout=5).returncode != 0:
        run(["tc", "qdisc", "replace", "dev", iface, "root", "pfifo",
             "limit", "10000"], timeout=5)
    out = run(["tc", "qdisc", "show", "dev", iface, "root"], timeout=5).stdout.split()
    echo(f"  ✅ {iface} qdisc 已设置为: {out[1] if len(out) > 1 else 'unknown'}")
    echo("  ⚡ 无流量整形，发包不受 AQM 限制")


# === 游戏 QoS：cake / prio 双方案（tc 部分，--boot 与守护共用）===
CAKE_ARGS = ["cake", "diffserv4", "nat", "nowash", "no-split-gso"]
PRIO_MAP = "1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1".split()


def tc_apply_cake(iface):
    return run(["tc", "qdisc", "replace", "dev", iface, "root"] + CAKE_ARGS,
               timeout=5).returncode == 0


def tc_apply_prio(iface):
    if run(["tc", "qdisc", "replace", "dev", iface, "root", "handle", "1:",
            "prio", "bands", "3", "priomap"] + PRIO_MAP, timeout=5).returncode != 0:
        return False
    for parent, handle in (("1:1", "10:"), ("1:2", "20:"), ("1:3", "30:")):
        run(["tc", "qdisc", "replace", "dev", iface, "parent", parent,
             "handle", handle, "fq_codel"], timeout=5)
    run(["tc", "filter", "del", "dev", iface, "parent", "1:"], timeout=5)
    # DSCP EF(TOS 0xb8) / AF41(TOS 0x88) → band0；小 UDP 包(≤128B) → band0
    run("tc filter add dev {i} parent 1: protocol ip prio 1 u32 "
        "match ip tos 0xb8 0xfc flowid 1:1".format(i=iface), timeout=5)
    run("tc filter add dev {i} parent 1: protocol ip prio 2 u32 "
        "match ip tos 0x88 0xfc flowid 1:1".format(i=iface), timeout=5)
    run("tc filter add dev {i} parent 1: protocol ip prio 3 u32 "
        "match ip protocol 17 0xff match u16 0x0000 0xff80 at 2 flowid 1:1"
        .format(i=iface), timeout=5)
    return True


def apply_af41_marking(ipt, ip6, iface, has_length=True, has_length_ip6=True):
    """AF41 游戏小包标记：清旧→写入（主流程/--boot/守护共用）。"""
    ipt_clear([c for c in V4_CMDS + V6_CMDS if c], PAT_AF41)
    for cmd, ok_len in ((ipt, has_length), (ip6, has_length_ip6)):
        if not cmd or not have_cmd(cmd):
            continue
        if ok_len:
            if not ipt_add(cmd, DSCP_AF41_RULE, iface):
                ipt_add(cmd, DSCP_AF41_RULE_NOLEN, iface)  # -m length 不支持时降级
        else:
            ipt_add(cmd, DSCP_AF41_RULE_NOLEN, iface)
        ipt_dedup(cmd, PAT_AF41)


def ensure_cake_module():
    """加载 sch_cake，不可用时尝试装 linux-modules-extra（尊重 SKIP_APT）。"""
    if run(["modprobe", "sch_cake"], timeout=10).returncode == 0:
        return True
    if not Cfg.SKIP_APT and have_cmd("apt-get"):
        kern = run(["uname", "-r"], timeout=5).stdout.strip()
        echo("  ℹ️ sch_cake 模块未加载，尝试安装 linux-modules-extra...")
        if apt(["install", "-y", "-qq", f"linux-modules-extra-{kern}"]).returncode == 0:
            echo(f"  ✅ linux-modules-extra-{kern} 安装成功，重新加载 sch_cake")
        else:
            echo(f"  ℹ️ linux-modules-extra-{kern} 不可用，跳过（将使用方案 B）")
    else:
        echo("  ℹ️ sch_cake 模块未加载且跳过 APT 安装（将使用方案 B）")
    return run(["modprobe", "sch_cake"], timeout=10).returncode == 0


def setup_game_qos():
    if Cfg.ADAPTIVE_QOS:
        return
    if not Cfg.ENABLE_GAME_QOS:
        echo("⏭️ 跳过游戏 QoS 配置")
        return
    if Cfg.AGGRESSIVE_MODE:
        echo("⏭️ 激进模式已开启，跳过游戏 QoS（互斥）")
        return

    echo("🎮 游戏低延迟 QoS 配置...")
    if not have_cmd("tc"):
        echo("  ⚠️ tc 命令不可用，跳过")
        return
    iface = detect_outbound_iface()
    if not iface:
        echo("  ⚠️ 无法检测出口网卡，跳过")
        return

    ipt = saved_ipt_backend()
    ip6 = ip6_cmd_for(ipt) if ipt else ""

    qos_scheme = "none"
    if ensure_cake_module() and tc_apply_cake(iface):
        qos_scheme = "cake"
        echo("  ✅ 方案 A：cake diffserv4 已启用")
        echo("    → 4 档优先级自动分流（Bulk/Best Effort/Video/Voice）")
        echo("    → 游戏小包自动归入高优先级队列")
        echo("    → 视频大流归入 Bulk 队列，不挤压游戏包")
    else:
        if have_cmd("tc"):
            echo("  ⚠️ cake 不可用，回退方案 B")
        if tc_apply_prio(iface):
            qos_scheme = "prio"
            echo("  ✅ 方案 B：prio + fq_codel 已启用")
            echo("    → band 0（高优先）：DSCP EF/AF41 + 小 UDP 包")
            echo("    → band 1（普通）：一般流量")
            echo("    → band 2（低优先）：Bulk 流量")
        else:
            echo("  ⚠️ prio qdisc 设置失败，跳过游戏 QoS")
            return

    # DSCP 标记：游戏流量打 AF41（UDP 小包 ≤200B 非 443）
    if ipt and have_cmd(ipt):
        apply_af41_marking(ipt, ip6, iface)
        echo("  ✅ 游戏 DSCP 标记：UDP 小包(≤200B, 非443) → AF41")

    config_set("GAME_QOS_SCHEME", qos_scheme, only_if_exists=True)
    config_set("ADAPTIVE_QOS_MODE", Cfg.ADAPTIVE_QOS_MODE, only_if_exists=True)
    if Cfg.ADAPTIVE_QOS_MODE == "fixed_cake":
        echo("  ✅ 游戏 QoS 配置完成（固定 cake 模式，不自动切换）")
    else:
        echo(f"  ✅ 游戏 QoS 配置完成（方案: {qos_scheme}）")


# === 自适应 QoS：配置 + systemd 服务（守护逻辑见 AdaptiveQoS / --daemon）===
def ipt_m_length_ok(cmd):
    """检测 -m length 模块可用性（写入测试规则后立即删除）。"""
    if not cmd or not have_cmd(cmd):
        return False
    test = ["-p", "udp", "-m", "length", "--length", "0:200", "-j", "RETURN"]
    if run([cmd, "-t", "mangle", "-A", "POSTROUTING"] + test, timeout=5).returncode == 0:
        run([cmd, "-t", "mangle", "-D", "POSTROUTING"] + test, timeout=5)
        return True
    return False


def setup_adaptive_qos():
    if not Cfg.ADAPTIVE_QOS:
        # 清理：如果之前启用过，现在关闭
        if run(["systemctl", "is-active", ADAPTIVE_QOS_SERVICE], timeout=5).returncode == 0:
            run(["systemctl", "stop", ADAPTIVE_QOS_SERVICE], timeout=15)
            run(["systemctl", "disable", ADAPTIVE_QOS_SERVICE], timeout=15)
            echo("🔄 自适应 QoS 已停止并关闭")
        return

    echo("🔄 自适应 QoS 配置（流量自动切换）...")
    if not have_cmd("tc"):
        echo("  ⚠️ tc 命令不可用，跳过")
        return
    iface = detect_outbound_iface()
    if not iface:
        echo("  ⚠️ 无法检测出口网卡，跳过")
        return

    has_cake = run(["modprobe", "sch_cake"], timeout=10).returncode == 0
    ipt = saved_ipt_backend()
    ip6 = ip6_cmd_for(ipt) if ipt else ""

    has_length = ipt_m_length_ok(ipt)
    if not has_length:
        echo("  ⚠️ iptables -m length 不可用，IPv4 AF41 标记将跳过")
    has_length_ip6 = ipt_m_length_ok(ip6)
    if not has_length_ip6 and ip6:
        echo("  ⚠️ ip6tables -m length 不可用，IPv6 AF41 标记将跳过")

    write_text(ADAPTIVE_CONF, json.dumps({
        "iface": iface,
        "threshold": Cfg.ADAPTIVE_QOS_THRESHOLD,
        "interval": Cfg.ADAPTIVE_QOS_INTERVAL,
        "cooldown": Cfg.ADAPTIVE_QOS_COOLDOWN,
        "has_cake": has_cake,
        "has_length": has_length,
        "has_length_ip6": has_length_ip6,
        "ipt_backend": ipt,
        "ip6_cmd": ip6,
    }, indent=2, ensure_ascii=False) + "\n")

    write_text(f"/etc/systemd/system/{ADAPTIVE_QOS_SERVICE}.service", f"""\
[Unit]
Description=Net-Optimize Adaptive QoS Daemon (Python3)
After=network-online.target net-optimize.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={PYTHON_BIN} {SCRIPT_PATH} --daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")
    run(["systemctl", "daemon-reload"], timeout=30)
    run(["systemctl", "enable", f"{ADAPTIVE_QOS_SERVICE}.service"], timeout=15)
    run(["systemctl", "restart", f"{ADAPTIVE_QOS_SERVICE}.service"], timeout=15)

    config_set("ADAPTIVE_QOS", 1, only_if_exists=True)
    config_set("ADAPTIVE_QOS_MODE", "adaptive", only_if_exists=True)
    config_set("ADAPTIVE_QOS_IFACE", iface, only_if_exists=True)

    echo("  ✅ 自适应 QoS 守护进程已启动")
    echo(f"    → 网卡: {iface}")
    echo(f"    → 阈值: {Cfg.ADAPTIVE_QOS_THRESHOLD // 1024} KB/s")
    echo(f"    → 采样: 每 {Cfg.ADAPTIVE_QOS_INTERVAL}s")
    echo("    → 流量 ≥ 阈值 → pfifo_fast（抢带宽）")
    echo(f"    → 流量 < 阈值 → {'cake/prio' if has_cake else 'prio'}（游戏低延迟）")
    echo(f"    → 服务: systemctl status {ADAPTIVE_QOS_SERVICE}")


# === 自适应 QoS 守护（--daemon 入口）===
class AdaptiveQoS:
    """自动根据出口流量切换 抢带宽(pfifo_fast) ↔ 游戏低延迟(cake/prio)。"""

    def __init__(self, conf):
        self.iface = conf["iface"]
        self.threshold = conf["threshold"]
        self.interval = conf["interval"]
        self.cooldown_secs = conf.get("cooldown", 10)
        self.has_cake = conf.get("has_cake", False)
        self.has_length = conf.get("has_length", False)
        self.has_length_ip6 = conf.get("has_length_ip6", conf.get("has_length", False))
        self.ipt = conf.get("ipt_backend") or ""
        self.ip6 = conf.get("ip6_cmd") or ""
        if not self.ipt:
            self.ipt = next((c for c in V4_CMDS if have_cmd(c)), "")
        if not self.ip6:
            self.ip6 = next((c for c in V6_CMDS if have_cmd(c)), "")
        self.mode = "unknown"  # game / aggressive / unknown

        self.log = logging.getLogger("adaptive-qos")
        self.log.setLevel(logging.INFO)
        try:
            h = logging.handlers.SysLogHandler(address="/dev/log")
            h.ident = "adaptive-qos: "
            self.log.addHandler(h)
        except Exception:  # noqa
            logging.basicConfig(level=logging.INFO)

    @staticmethod
    def _read_stat(iface, name):
        try:
            with open(f"/sys/class/net/{iface}/statistics/{name}") as f:
                return int(f.read().strip())
        except Exception:  # noqa
            return 0

    def _apply_pfifo(self):
        if run(["tc", "qdisc", "replace", "dev", self.iface, "root",
                "pfifo_fast"], timeout=5).returncode != 0:
            run(["tc", "qdisc", "replace", "dev", self.iface, "root",
                 "pfifo", "limit", "10000"], timeout=5)

    def switch_to_game(self):
        if self.mode == "game":
            return
        if not (self.has_cake and tc_apply_cake(self.iface)):
            tc_apply_prio(self.iface)
        apply_af41_marking(self.ipt, self.ip6, self.iface,
                           self.has_length, self.has_length_ip6)
        self.mode = "game"
        self.log.info("切换 → 游戏低延迟 (rate < %d B/s)", self.threshold)

    def switch_to_aggressive(self):
        if self.mode == "aggressive":
            return
        self._apply_pfifo()
        apply_af41_marking(self.ipt, self.ip6, self.iface,
                           self.has_length, self.has_length_ip6)
        self.mode = "aggressive"
        self.log.info("切换 → 抢带宽 (rate >= %d B/s)", self.threshold)

    def run_forever(self):
        cooldown_max = max(1, self.cooldown_secs // self.interval)
        self.log.info("启动: iface=%s threshold=%d interval=%d cooldown=%ds(%d ticks)",
                      self.iface, self.threshold, self.interval,
                      self.cooldown_secs, cooldown_max)
        self.switch_to_game()

        prev_rx = self._read_stat(self.iface, "rx_bytes")
        prev_tx = self._read_stat(self.iface, "tx_bytes")
        time.sleep(self.interval)
        cooldown_ticks = 0

        while True:
            rx = self._read_stat(self.iface, "rx_bytes")
            tx = self._read_stat(self.iface, "tx_bytes")
            # 入站/出站取最大值，任意方向达到阈值即触发抢带宽
            rate = max((rx - prev_rx) // self.interval,
                       (tx - prev_tx) // self.interval)
            prev_rx, prev_tx = rx, tx

            if rate >= self.threshold:
                cooldown_ticks = cooldown_max
                self.switch_to_aggressive()
            elif cooldown_ticks > 0:
                cooldown_ticks -= 1   # 冷却中：保持抢带宽
            else:
                self.switch_to_game()
            time.sleep(self.interval)


def cmd_daemon():
    try:
        with open(ADAPTIVE_CONF) as f:
            conf = json.load(f)
    except Exception as e:  # noqa
        print(f"❌ 读取配置失败: {e}", file=sys.stderr)
        sys.exit(1)
    qos = AdaptiveQoS(conf)
    try:
        qos.run_forever()
    except KeyboardInterrupt:
        qos.log.info("收到停止信号，退出")
    except Exception as e:  # noqa
        qos.log.error("守护进程异常: %s", e)
        sys.exit(1)


# === Nginx 官方源 / 双源共存 / 安装升级 / 自动更新（--nginx-upgrade 由 cron 调）===
NGINX_KEYRING = "/usr/share/keyrings/nginx-archive-keyring.gpg"
NGINX_OFFICIAL_LIST = "/etc/apt/sources.list.d/nginx-official.list"
NGINX_PIN = "/etc/apt/preferences.d/99-nginx-official"
NGINX_CRON = "/etc/cron.d/net-optimize-nginx-update"


def cmd_nginx_upgrade():
    """nginx 安装/升级（原 net-optimize-nginx-upgrade 脚本，输出进日志）。"""
    os.makedirs(os.path.dirname(NGINX_LOG), exist_ok=True)
    logf = open(NGINX_LOG, "a")

    def L(msg=""):
        logf.write(msg + "\n")
        logf.flush()

    def LR(r):
        if r.stdout.strip():
            L(r.stdout.rstrip())
        if r.stderr.strip():
            L(r.stderr.rstrip())

    L(f"========== {datetime.now().strftime('%F %T')} ==========")
    if have_cmd("nginx"):
        L(f"[Before] {run(['nginx', '-v'], timeout=5).stderr.strip()}")
    else:
        L("[Before] Nginx 未安装")

    if not have_cmd("apt-get"):
        L("非 APT 系统，跳过")
        return
    if have_cmd("dpkg"):
        run(["dpkg", "--configure", "-a"], timeout=600,
            env_extra={"DEBIAN_FRONTEND": "noninteractive"})

    if apt(["update", "-y"]).returncode != 0:
        L("⚠️ apt-get update 失败，继续尝试使用现有缓存")

    if have_cmd("nginx"):
        L("检测到 Nginx：执行自动升级")
        pkgs = ["nginx"]
        r = run(["dpkg-query", "-W", "-f=${Status}", "nginx-common"], timeout=10)
        if "install ok installed" in r.stdout:
            pkgs.append("nginx-common")
        r = apt(["install", "--only-upgrade", "-y"] + pkgs)
        LR(r)
        if r.returncode != 0:
            L("⚠️ Nginx 升级失败或当前暂无可升级版本")
    else:
        L("未检测到 Nginx：开始自动安装")
        r = apt(["install", "-y", "nginx"])
        LR(r)
        if r.returncode != 0:
            L("❌ Nginx 安装失败")
            L("")
            return
        run(["systemctl", "enable", "nginx"], timeout=15)
        run(["systemctl", "start", "nginx"], timeout=30)

    if have_cmd("nginx"):
        if run(["nginx", "-t"], timeout=15).returncode == 0:
            if run(["systemctl", "reload", "nginx"], timeout=30).returncode != 0:
                run(["systemctl", "restart", "nginx"], timeout=30)
            L("✅ nginx -t 通过，已 reload/restart")
        else:
            L("⚠️ nginx -t 失败，为避免中断服务，不执行 reload")
            LR(run(["nginx", "-t"], timeout=15))
        L(f"[After] {run(['nginx', '-v'], timeout=5).stderr.strip()}")
        L("----- apt-cache policy nginx -----")
        pol = run(["apt-cache", "policy", "nginx"], timeout=30).stdout.splitlines()
        L("\n".join(pol[:35]))
    L("")
    logf.close()


def fix_nginx_repo():
    if not Cfg.ENABLE_NGINX_REPO:
        echo("⏭️ 跳过 Nginx 管理")
        return
    if not have_cmd("apt-get"):
        echo("ℹ️ 非 APT 系统，跳过 Nginx 管理")
        return
    if Cfg.SKIP_APT:
        echo("⏭️ SKIP_APT=1，跳过 Nginx 安装/升级/源配置")
        return

    echo("🌐 Nginx 官方源 / 双源共存 / 安装升级 / 自动更新配置...")
    distro, codename = detect_distro()
    if codename == "unknown" and have_cmd("lsb_release"):
        codename = run(["lsb_release", "-sc"], timeout=10).stdout.strip() or "unknown"
    if codename == "unknown":
        echo("⚠️ 无法识别系统 codename，跳过 nginx.org 官方源配置，仅尝试系统源安装/升级")

    check_dpkg_clean()
    apt(["update", "-y"])
    apt(["install", "-y", "--no-install-recommends", "ca-certificates", "curl",
         "gnupg", "lsb-release", "apt-transport-https"])

    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    # 1) 清理明显错误的 nginx 源
    nginx_sources_selfheal(distro)

    # 2) 配置 nginx.org 官方 stable 源 + Pin 1001
    base = "https://nginx.org/packages/" + ("ubuntu" if distro == "ubuntu" else "debian")
    if codename != "unknown":
        if not (os.path.isfile(NGINX_KEYRING) and os.path.getsize(NGINX_KEYRING) > 0):
            r = sh("curl -fsSL https://nginx.org/keys/nginx_signing.key | "
                   f"gpg --dearmor -o {NGINX_KEYRING}.tmp", timeout=60)
            if r.returncode == 0:
                os.replace(f"{NGINX_KEYRING}.tmp", NGINX_KEYRING)
                os.chmod(NGINX_KEYRING, 0o644)
                echo("✅ 已写入 nginx.org 官方 keyring")
            else:
                try:
                    os.remove(f"{NGINX_KEYRING}.tmp")
                except Exception:  # noqa
                    pass
                echo("⚠️ nginx.org keyring 获取失败，后续将尝试使用系统已有源")
        else:
            echo("ℹ️ nginx.org keyring 已存在")

        if os.path.isfile(NGINX_KEYRING) and os.path.getsize(NGINX_KEYRING) > 0:
            write_text(NGINX_OFFICIAL_LIST,
                       "# Net-Optimize: nginx.org official stable repo\n"
                       f"deb [signed-by={NGINX_KEYRING}] {base} {codename} nginx\n")
            echo(f"✅ 已配置 nginx.org 官方源：{base} {codename}")
            write_text(NGINX_PIN,
                       "Package: nginx*\nPin: origin nginx.org\nPin-Priority: 1001\n")
            echo("✅ 已设置 nginx.org Pin=1001，官方源优先")

    # 3) ondrej/nginx PPA：可用保留，失效禁用（仅 Ubuntu）
    if distro == "ubuntu":
        has_ondrej = bool(glob.glob("/etc/apt/sources.list.d/*ondrej*nginx*"))
        if not has_ondrej:
            for f in glob.glob("/etc/apt/sources.list.d/*"):
                if "ppa.launchpadcontent.net/ondrej/nginx" in read_text(f):
                    has_ondrej = True
                    break
        if has_ondrej:
            ppa_codename = run(["lsb_release", "-sc"], timeout=10).stdout.strip() or codename
            url = (f"https://ppa.launchpadcontent.net/ondrej/nginx/ubuntu/"
                   f"dists/{ppa_codename}/Release")
            if sh(f"curl -fsSL --max-time 8 {shlex.quote(url)} >/dev/null",
                  timeout=15).returncode == 0:
                echo("ℹ️ 已检测到 ondrej/nginx PPA 源，可用，共存保留")
            else:
                for f in glob.glob("/etc/apt/sources.list.d/*ondrej*nginx*"):
                    os.rename(f, f"{f}.disabled.{ts}")
                echo("ℹ️ ondrej/nginx PPA 已失效，已自动禁用，避免污染 apt update")
        else:
            echo("ℹ️ 未检测到 ondrej/nginx PPA，仅使用 nginx.org 官方源 + 系统源")
    else:
        echo("ℹ️ 非 Ubuntu 系统，跳过 ondrej/nginx PPA 检查")

    # 4) 每次执行主脚本，都立即安装或升级一次
    echo("🔄 执行本次 Nginx 安装/升级检查...")
    cmd_nginx_upgrade()

    # 5) 不执行主脚本时，每月 1 号北京时间 03:10 自动更新
    write_text(NGINX_CRON,
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
               "CRON_TZ=Asia/Shanghai\n"
               "# Net-Optimize: monthly nginx install/upgrade, nginx.org Pin=1001\n"
               f"10 3 1 * * root {PYTHON_BIN} {SCRIPT_PATH} --nginx-upgrade\n")
    echo("✅ 已配置 Nginx 自动更新 cron：北京时间每月 1 号 03:10")

    if have_cmd("nginx"):
        ver = run(["nginx", "-v"], timeout=5).stderr.strip().split("/")[-1]
        echo(f"✅ 当前 Nginx 版本：{ver}")
    else:
        echo(f"⚠️ 当前仍未检测到 Nginx，请查看：{NGINX_LOG}")
    echo(f"🔎 查看日志：tail -n 80 {NGINX_LOG}")


# === DHCP 续约后恢复 initcwnd（--reapply-initcwnd，由 networkd-dispatcher 调）===
def cmd_reapply_initcwnd():
    cfg = config_read()
    if cfg.get("ENABLE_INITCWND", "1") != "1":
        return
    cwnd = cfg.get("INITCWND", "20")
    if cwnd == "0":
        return
    apply_initcwnd_routes(cwnd)
    logger_msg(f"initcwnd={cwnd} re-applied on "
               f"{cfg.get('CLAMP_IFACE', 'unknown')} (networkd-dispatcher)")


# === 开机自启服务（--boot 入口 + systemd 单元 + dispatcher 钩子）===
def install_boot_service():
    if not Cfg.APPLY_AT_BOOT:
        echo("⏭️ 跳过开机自启配置")
        return
    echo("🛠️ 配置开机自启动服务...")

    write_text("/etc/systemd/system/net-optimize.service", f"""\
[Unit]
Description=Net-Optimize Ultimate Boot Optimization
After=network-online.target systemd-sysctl.service cloud-init.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={PYTHON_BIN} {SCRIPT_PATH} --boot
RemainAfterExit=yes
StandardOutput=journal
TimeoutSec=45

[Install]
WantedBy=multi-user.target
""")
    run(["systemctl", "daemon-reload"], timeout=30)
    run(["systemctl", "enable", "net-optimize.service"], timeout=15)

    # networkd-dispatcher hook：DHCP 续约后自动恢复 initcwnd（无需重启）
    hook = "/etc/networkd-dispatcher/routable.d/50-initcwnd"
    write_text(hook,
               "#!/bin/sh\n"
               "# networkd-dispatcher hook: re-apply initcwnd after DHCP renewal\n"
               f"exec {PYTHON_BIN} {SCRIPT_PATH} --reapply-initcwnd\n", exe=True)
    echo("✅ networkd-dispatcher hook 已安装（DHCP 续约自动恢复 initcwnd）")

    # 持久化当前 iptables 规则（保留 nat 表端口跳跃规则，供开机恢复用）
    if have_cmd("netfilter-persistent"):
        run(["netfilter-persistent", "save"], timeout=60)
        echo("✅ iptables 规则已持久化（nat 端口跳跃将在重启后自动恢复）")
    echo("✅ 开机自启服务配置完成")


def cmd_boot_apply():
    """开机恢复（原 net-optimize-apply）：不因单步失败中断。"""
    lock = open("/var/run/net-optimize-apply.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        echo(f"[{datetime.now()}] net-optimize-apply: 另一实例正在运行，跳过")
        return

    cfg = config_read()

    # 模块 + sysctl
    for module in read_text(MODULES_FILE).split():
        run(["modprobe", module], timeout=10)
    run(["sysctl", "-e", "--system"], timeout=60)

    # 强制覆盖 rp_filter（防止 cloud-init/systemd-networkd 按接口覆盖）
    rp = cfg.get("RP_FILTER", "2")
    for p in glob.glob("/proc/sys/net/ipv4/conf/*/rp_filter"):
        write_proc(p, rp)

    # conntrack 触发（INVALID -> DROP + 出站触发计数）
    conntrack_invalid_drop_rules()
    if have_cmd("curl"):
        run(["curl", "-4I", "https://1.1.1.1", "--max-time", "3"], timeout=10)
        run(["curl", "-4I", "https://www.google.com", "--max-time", "3"], timeout=10)

    # 清理 netfilter-persistent 保存的 mangle 规则（保留 nat 表端口跳跃）
    list(strip_persistent_mangle())

    if os.path.isfile(CONFIG_FILE):
        ipt = cfg.get("IPT_BACKEND", "iptables")
        if not have_cmd(ipt):
            ipt = "iptables"
        iface = cfg.get("CLAMP_IFACE", "")
        ip6 = ip6_cmd_for(ipt)

        # 第一步：flush 所有后端 mangle POSTROUTING，彻底避免重复
        for m in ("ip_tables", "iptable_mangle", "ip6_tables", "ip6table_mangle"):
            run(["modprobe", m], timeout=10)
        logger_msg(f"BOOT: flush前 TCPMSS={ipt_count('iptables-legacy', PAT_TCPMSS)}")
        for cmd in V4_CMDS + V6_CMDS:
            if have_cmd(cmd):
                run([cmd, "-w", "2", "-t", "mangle", "-F", "POSTROUTING"], timeout=5)
        logger_msg(f"BOOT: flush后 TCPMSS={ipt_count('iptables-legacy', PAT_TCPMSS)}")

        # 第二步：写入 MSS Clamping（IPv4 + IPv6）
        if cfg.get("ENABLE_MSS_CLAMP", "0") == "1":
            mss = int(cfg.get("MSS_VALUE", "1452"))
            if have_cmd(ipt):
                apply_one_tcpmss(ipt, iface, mss)
            if ip6 and have_cmd(ip6):
                apply_one_tcpmss(ip6, iface, mss - 20)

        # 第三步：写入 DSCP EF（QUIC UDP 443）
        if have_cmd(ipt):
            ipt_add(ipt, DSCP_EF_RULE, iface)
        if ip6 and have_cmd(ip6):
            ipt_add(ip6, DSCP_EF_RULE, iface)

        logger_msg(f"BOOT: add后 TCPMSS={ipt_count('iptables-legacy', PAT_TCPMSS)} "
                   f"DSCP={ipt_count('iptables-legacy', PAT_DSCP)}")

        # initcwnd（IPv6 RA 路由可能开机后几秒才到，等待最多 10 秒）
        cwnd = cfg.get("INITCWND", "20")
        r = run(["ip", "-4", "route", "show", "default"], timeout=5)
        if r.stdout.strip():
            parts = shlex.split(strip_route_params(r.stdout.splitlines()[0]))
            run(["ip", "route", "change"] + parts +
                ["initcwnd", cwnd, "initrwnd", cwnd], timeout=5)
        gw6 = ""
        for _ in range(10):
            r6 = run(["ip", "-6", "route", "show", "default"], timeout=5)
            if r6.stdout.strip():
                gw6 = r6.stdout.splitlines()[0]
                break
            time.sleep(1)
        if gw6:
            parts6 = shlex.split(strip_route_params(gw6))
            run(["ip", "-6", "route", "change"] + parts6 +
                ["initcwnd", cwnd, "initrwnd", cwnd], timeout=5)

        # 游戏 QoS 恢复（cake / prio）+ AF41 标记恢复
        scheme = cfg.get("GAME_QOS_SCHEME", "none")
        if scheme == "cake" and iface and iface != "unknown":
            run(["modprobe", "sch_cake"], timeout=10)
            tc_apply_cake(iface)
        elif scheme == "prio" and iface and iface != "unknown":
            tc_apply_prio(iface)
        if scheme != "none":
            apply_af41_marking(ipt, ip6, iface)

        # 最终去重（兜底：所有后端 TCPMSS / EF / AF41 各保留 1 条）
        for pattern in (PAT_TCPMSS, PAT_EF, PAT_AF41):
            for cmd in V4_CMDS + V6_CMDS:
                ipt_dedup(cmd, pattern)

    logger_msg(f"BOOT: 最终 TCPMSS={ipt_count('iptables-legacy', PAT_TCPMSS)} "
               f"EF={ipt_count('iptables-legacy', PAT_EF)} "
               f"AF41={ipt_count('iptables-legacy', PAT_AF41)}")
    echo(f"[{datetime.now()}] Net-Optimize v{VERSION} 开机优化完成")


# === 状态检查 ===
def print_status():
    cfg = config_read()
    echo("")
    echo("==================== 优 化 状 态 报 告 ====================")

    echo("📊 基 础 状 态 :")
    if Cfg.AGGRESSIVE_MODE:
        echo(f"  ⚡ {'激进模式':<20} : 已开启")
    echo(f"  {'TCP 拥塞算法':<22} : {get_sysctl('net.ipv4.tcp_congestion_control')}")
    echo(f"  {'默认队列':<22} : {get_sysctl('net.core.default_qdisc')}")
    try:
        import resource
        nofile = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:  # noqa
        nofile = "N/A"
    echo(f"  {'文件句柄限制':<22} : {nofile}")
    echo(f"  {'rmem_default':<22} : {get_sysctl('net.core.rmem_default')} bytes")
    echo(f"  {'tcp_window_scaling':<22} : {get_sysctl('net.ipv4.tcp_window_scaling')}")
    echo(f"  {'tcp_sack':<22} : {get_sysctl('net.ipv4.tcp_sack')}")
    echo(f"  {'tcp_notsent_lowat':<22} : {get_sysctl('net.ipv4.tcp_notsent_lowat')}")
    echo("")

    echo("🌐 网 络 状 态 :")
    echo(f"  {'IP 转发':<22} : {get_sysctl('net.ipv4.ip_forward')}")
    echo(f"  {'rp_filter':<22} : {get_sysctl('net.ipv4.conf.all.rp_filter')}")
    echo(f"  {'IPv6 禁用':<22} : {get_sysctl('net.ipv6.conf.all.disable_ipv6')}")
    echo(f"  {'TCP ECN':<22} : {get_sysctl('net.ipv4.tcp_ecn')}")
    echo(f"  {'TCP FastOpen':<22} : {get_sysctl('net.ipv4.tcp_fastopen')}")
    echo("")

    echo("🔗 连 接 跟 踪 (conntrack):")
    if conntrack_available():
        echo("  ✅ conntrack 可用（模块或内建）")
        echo(f"  {'nf_conntrack_max':<30} : {get_sysctl('net.netfilter.nf_conntrack_max')}")
        echo(f"  {'udp_timeout':<30} : {get_sysctl('net.netfilter.nf_conntrack_udp_timeout')}")
        echo(f"  {'udp_timeout_stream':<30} : "
             f"{get_sysctl('net.netfilter.nf_conntrack_udp_timeout_stream')}")
        echo(f"  {'tcp_timeout_established':<30} : "
             f"{get_sysctl('net.netfilter.nf_conntrack_tcp_timeout_established')}")
        if have_cmd("conntrack"):
            r = run(["conntrack", "-C"], timeout=10)
            echo(f"  {'总连接数 (conntrack -C)':<30} : "
                 f"{r.stdout.strip() if r.returncode == 0 else 'N/A'}")
        if os.path.isfile("/proc/net/nf_conntrack"):
            lines = read_text("/proc/net/nf_conntrack").splitlines()
            tcp_c = sum(1 for l in lines if l.startswith("tcp"))
            udp_c = sum(1 for l in lines if l.startswith("udp"))
            total_c = len(lines)
            echo("  /proc 表记录数:")
            echo(f"    TCP entries = {tcp_c}")
            echo(f"    UDP entries = {udp_c}")
            echo(f"    Other       = {max(total_c - tcp_c - udp_c, 0)}")
            echo(f"    Total       = {total_c}")
        else:
            echo("  ℹ️ /proc/net/nf_conntrack 不存在（可能是 nft / 内核暴露差异）")
        if have_cmd("lsmod"):
            if re.search(r"^nf_conntrack", run(["lsmod"], timeout=10).stdout, re.M):
                echo("  ✅ lsmod 可见 nf_conntrack（非内建）")
            else:
                echo("  ℹ️ lsmod 未显示 nf_conntrack（可能是内建，正常）")
    else:
        echo("  ⚠️ conntrack 不可用（内核未启用 netfilter conntrack）")
    echo("")

    echo("🎮 游戏 QoS 状态:")
    scheme = cfg.get("GAME_QOS_SCHEME", "none")
    iface = detect_outbound_iface()
    if scheme == "cake":
        echo("  ✅ 方案: cake diffserv4（4 档自动分流）")
        if iface:
            out = run(["tc", "-s", "qdisc", "show", "dev", iface], timeout=5).stdout
            echo("\n".join(out.splitlines()[:5]))
    elif scheme == "prio":
        echo("  ✅ 方案: prio + fq_codel（3 档手动分流）")
        if iface:
            echo("  tc qdisc:")
            out = run(["tc", "qdisc", "show", "dev", iface], timeout=5).stdout
            echo("\n".join(out.splitlines()[:8]))
    else:
        echo("  ℹ️ 未启用（AGGRESSIVE_MODE=1 或 ENABLE_GAME_QOS=0）")

    aq_mode = cfg.get("ADAPTIVE_QOS_MODE", "adaptive")
    if aq_mode == "fixed_cake":
        echo("\n📌 QoS 模式：固定 cake（不自动切换，始终游戏低延迟）")
    elif run(["systemctl", "is-active", ADAPTIVE_QOS_SERVICE], timeout=5).returncode == 0:
        echo("\n🔄 自适应 QoS：运行中")
        echo(f"  → 阈值: {Cfg.ADAPTIVE_QOS_THRESHOLD // 1024} KB/s  "
             f"采样: {Cfg.ADAPTIVE_QOS_INTERVAL}s")
        echo("  → 高流量→pfifo_fast(抢带宽)  低流量→cake/prio(游戏低延迟)")

    # DSCP 规则概览
    dscp_v4 = dscp_v6 = 0
    for cmd in V4_CMDS:
        if have_cmd(cmd):
            dscp_v4 = ipt_count(cmd, PAT_DSCP)
            if dscp_v4 > 0:
                break
    for cmd in ("ip6tables", "ip6tables-legacy"):
        if have_cmd(cmd):
            dscp_v6 = ipt_count(cmd, PAT_DSCP)
            if dscp_v6 > 0:
                break
    echo(f"  DSCP 规则: IPv4={dscp_v4}条 IPv6={dscp_v6}条 "
         f"(EF=QUIC加速, AF41=游戏小包)")
    echo("")

    echo("📡 MSS Clamping 规则:")
    found = False
    for cmd in V4_CMDS:
        if not have_cmd(cmd):
            continue
        r = run([cmd, "-t", "mangle", "-L", "POSTROUTING", "-n", "-v"], timeout=5)
        if "TCPMSS" in r.stdout:
            echo(f"  ✅ 后端: {cmd}")
            for line in r.stdout.splitlines():
                if re.search(r"Chain|pkts|bytes|TCPMSS", line):
                    echo(line)
            found = True
            break
    if not found:
        echo("  ⚠️ 未找到 MSS 规则（所有后端均未检测到）")
    echo("")

    echo("💻 系 统 信 息 :")
    echo(f"  {'内核版本':<14} : {run(['uname', '-r'], timeout=5).stdout.strip()}")
    did, codename = detect_distro()
    echo(f"  {'发行版':<14} : {did}:{codename}")
    free = run(["free", "-h"], timeout=5).stdout
    m = re.search(r"^Mem:\s+(\S+)(?:\s+\S+){4}\s+(\S+)", free, re.M)
    if m:
        echo(f"  {'内存':<14} : {m.group(1)}")
        echo(f"  {'可用内存':<14} : {m.group(2)}")
    echo("=========================================================")
    echo("")


# === 完整卸载/重置（--reset，原 net-optimize-reset.sh 移植）===
def _rm(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def cmd_reset():
    echo("🧹 开始清除所有 Net-Optimize 配置...")
    echo("=" * 60)

    # [1] systemd 服务
    echo("🔧 [1] 清理 systemd 服务...")
    for svc in (f"{BOOT_SERVICE}.service", f"{ADAPTIVE_QOS_SERVICE}.service"):
        run(["systemctl", "stop", svc], timeout=15)
        run(["systemctl", "disable", svc], timeout=15)
        _rm(f"/etc/systemd/system/{svc}")
        echo(f"  ✅ 已移除 {svc}")
    run(["systemctl", "daemon-reload"], timeout=30)

    # [2] 网卡 tc qdisc
    echo("🔧 [2] 重置网卡 tc qdisc...")
    iface = detect_outbound_iface()
    if iface and have_cmd("tc"):
        run(["tc", "qdisc", "del", "dev", iface, "root"], timeout=5)
        echo(f"  ✅ 已重置 {iface} qdisc（恢复内核默认）")
    else:
        echo("  ℹ️ 无法检测出口网卡，跳过 qdisc 重置")

    # [3] iptables 规则（TCPMSS + DSCP + INVALID DROP，所有后端）
    echo("🔧 [3] 清理 iptables 规则（TCPMSS + DSCP + INVALID DROP）...")
    ipt_clear(V4_CMDS + V6_CMDS, r"TCPMSS|DSCP")
    for cmd in V4_CMDS:
        if not have_cmd(cmd):
            continue
        for chain in ("INPUT", "OUTPUT"):
            run([cmd, "-w", "2", "-t", "filter", "-D", chain, "-m", "conntrack",
                 "--ctstate", "INVALID", "-j", "DROP"], timeout=5)
    echo("  ✅ 已清理所有后端的 TCPMSS/DSCP/INVALID DROP 规则")
    # 同步剥掉 netfilter-persistent 保存文件里的 mangle 段并重新保存，
    # 否则下次开机会把 TCPMSS/DSCP 又恢复回来（nat 表端口跳跃规则不受影响）
    for pf in strip_persistent_mangle():
        echo(f"  ✅ 已从 {pf} 移除 mangle 段")
    if have_cmd("netfilter-persistent"):
        run(["netfilter-persistent", "save"], timeout=60)

    # [4] sysctl 配置（删除本脚本文件 + 恢复被禁用的冲突文件）
    echo("🔧 [4] 清理 sysctl 配置...")
    for f in (SYSCTL_AUTH_FILE, SYSCTL_OVERRIDE_FILE,
              "/etc/sysctl.d/98-net-optimize-mptcp.conf"):
        _rm(f)
    echo("  ✅ 已删除 sysctl 配置文件")
    for f in sorted(glob.glob("/etc/sysctl.d/*.disabled-by-net-optimize-*")):
        orig = re.sub(r"\.disabled-by-net-optimize-.*$", "", f)
        if not os.path.isfile(orig):
            os.rename(f, orig)
            echo(f"  ✅ 已恢复: {orig}")
        else:
            _rm(f)
            echo(f"  🗑 已删除残留: {f}")
    if os.path.isfile("/etc/sysctl.conf"):
        content = read_text("/etc/sysctl.conf")
        restored = content.replace("# net-optimize disabled: ", "")
        if restored != content:
            write_text("/etc/sysctl.conf", restored)
            echo("  ✅ 已恢复 /etc/sysctl.conf 中被注释的行")
    run(["sysctl", "--system"], timeout=60)
    echo("  ✅ sysctl 已重新加载")

    # [5] ulimit
    echo("🔧 [5] 清理 ulimit 配置...")
    _rm("/etc/security/limits.d/99-net-optimize.conf")
    sysconf = read_text("/etc/systemd/system.conf")
    restored = re.sub(r"^DefaultLimitNOFILE.*\n?", "", sysconf, flags=re.M)
    if restored != sysconf:
        write_text("/etc/systemd/system.conf", restored)
    run(["systemctl", "daemon-reload"], timeout=30)
    echo("  ✅ 已清理 ulimit 配置")

    # [6] conntrack
    echo("🔧 [6] 清理 conntrack 配置...")
    _rm(CONNTRACK_MODULES_CONF)
    _rm("/etc/modprobe.d/net-optimize-conntrack.conf")
    echo("  ✅ 已删除 conntrack 开机加载 / hashsize 配置")

    # [7] NIC offload / RPS/RFS / CPU 调频 / initcwnd 钩子持久化
    echo("🔧 [7] 清理网卡/CPU 持久化配置...")
    for f in ("/etc/udev/rules.d/99-net-optimize-offload.rules",
              "/etc/tmpfiles.d/net-optimize-rps.conf",
              "/etc/tmpfiles.d/net-optimize-cpufreq.conf",
              "/etc/networkd-dispatcher/routable.d/50-initcwnd"):
        _rm(f)
    echo("  ✅ 已删除 offload/RPS/RFS/cpufreq/initcwnd-hook 持久化规则")

    # [8] initcwnd 路由参数
    echo("🔧 [8] 清理 initcwnd 路由参数...")
    for fam, args in (("IPv4", ["ip", "-4"]), ("IPv6", ["ip", "-6"])):
        gw = run(args + ["route", "show", "default"], timeout=5).stdout
        line = gw.splitlines()[0] if gw.strip() else ""
        if line and "initcwnd" in line:
            parts = shlex.split(strip_route_params(line))
            run(args + ["route", "change"] + parts, timeout=5)
            echo(f"  ✅ 已清除 {fam} initcwnd")

    # [9] Nginx 自动更新 cron（nginx 本体与源保留，不影响正在用的网站/证书）
    echo("🔧 [9] 清理 Nginx 自动更新 cron...")
    _rm(NGINX_CRON)
    echo("  ✅ 已删除 Nginx 自动更新 cron")

    # [10] 配置目录 + 脚本本体（含旧版 bash 遗留文件）
    echo("🔧 [10] 删除脚本和配置...")
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    shutil.rmtree("/etc/net-optimize-backup", ignore_errors=True)
    for f in (SCRIPT_PATH,
              "/usr/local/sbin/net-optimize-ultimate.sh",
              "/usr/local/sbin/net-optimize-apply",
              "/usr/local/sbin/net-optimize-adaptive-qos",
              "/usr/local/sbin/net-optimize-nginx-upgrade"):
        _rm(f)
    echo("  ✅ 已删除 /etc/net-optimize 与脚本本体（含旧版 bash 遗留）")

    # [11] 临时 swap 残留（正常运行结束会自动清，异常中断可能留下）
    swap_file = "/tmp/.net-optimize-swap"
    if os.path.exists(swap_file):
        run(["swapoff", swap_file], timeout=30)
        _rm(swap_file)
        echo("  ✅ 已清理临时 swap 残留")

    echo("")
    echo("=" * 60)
    echo("🎉 所有 Net-Optimize 配置已清除，系统已恢复默认状态")
    echo("📌 建议重启一次，让内核参数完全回到系统默认：reboot")
    echo("📌 重启后可验证：sysctl net.ipv4.tcp_congestion_control / "
         "iptables -t mangle -L -n -v")


# === 主流程 ===
def main_optimize(argv):
    require_root()
    ensure_swap()

    echo(f"🚀 Net-Optimize-Ultimate v{VERSION} 开始执行...")
    echo("========================================================")

    auto_update(argv)   # 自动更新（SHA256SUMS 校验通过才替换并 exec 新版本）
    self_install()      # python3 <(curl ...) 时 /dev/fd 无法回读，允许失败

    clean_old_config()
    maybe_install_tools()
    setup_ulimit()
    setup_tcp_congestion()
    write_sysctl_conf()
    converge_sysctl_authority()
    force_apply_sysctl_runtime()
    setup_conntrack()
    setup_nic_offload()
    setup_rps_rfs()
    setup_cpu_governor()
    setup_mptcp()
    setup_mss_clamping()
    setup_dscp_marking()
    setup_initcwnd()
    setup_aggressive_tc()
    setup_game_qos()
    setup_adaptive_qos()
    fix_nginx_repo()
    install_boot_service()

    print_status()

    echo("✅ 所有优化配置完成！")
    echo("")
    echo("📌 重要提示：")
    echo("  1. 缓冲区大小已按内存自动计算，重启后完全生效")
    echo("  2. 检查状态: systemctl status net-optimize")
    echo("  3. 查看连接: cat /proc/net/nf_conntrack | head -20")
    echo("  4. 验证MSS: iptables -t mangle -L -n -v")
    echo("  5. 查看 CPU 调频: cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    echo(f"  6. 完整检测: python3 {SCRIPT_PATH} --check")
    echo("")

    if sys.stdin.isatty():
        try:
            answer = input("🔄 是否立即重启以生效所有优化？(y/N): ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.lower() == "y":
            echo("🌀 系统将在3秒后重启...")
            time.sleep(3)
            run(["reboot"], timeout=10)
        else:
            echo("📌 请稍后手动重启以应用所有优化")
    else:
        echo("📌 非交互模式，请手动重启以应用优化")


# === 状态检测（--check，原 net-optimize-check.sh v1.11 完整移植）===
def _color(code, msg):
    print(f"\033[{code}m{msg}\033[0m", flush=True)


def green(msg):
    _color("32", msg)


def yellow(msg):
    _color("33", msg)


def red(msg):
    _color("31", msg)


def c_title():
    echo("=" * 60)


def c_sep():
    echo("-" * 60)


def svc_state(unit):
    if run(["systemctl", "cat", unit], timeout=5).returncode != 0:
        echo(f"  - {unit}: (not installed)")
        return
    en = run(["systemctl", "is-enabled", unit], timeout=5).stdout.strip()
    act = run(["systemctl", "is-active", unit], timeout=5).stdout.strip()
    (green if act == "active" else yellow)(f"  - {unit}: enabled={en}, active={act}")


def check_ipt_backend():
    """检测版后端探测：优先返回已有 TCPMSS 规则的后端（只读，不试写）。"""
    for cmd in V4_CMDS:
        if have_cmd(cmd) and ipt_count(cmd, PAT_TCPMSS) > 0:
            return cmd
    if have_cmd("iptables"):
        r = run(["iptables", "-t", "mangle", "-S", "POSTROUTING"], timeout=5)
        if re.search(r"iptables-legacy", (r.stdout or "") + (r.stderr or ""), re.I) \
                and have_cmd("iptables-legacy"):
            return "iptables-legacy"
        return "iptables"
    if have_cmd("iptables-legacy"):
        return "iptables-legacy"
    if have_cmd("iptables-nft"):
        return "iptables-nft"
    return ""


def ethtool_features(iface):
    out = run(["ethtool", "-k", iface], timeout=5).stdout
    feats = {}
    for line in out.splitlines():
        m = re.match(r"^\s*([\w-]+):\s*(\S+)", line)
        if m:
            feats[m.group(1)] = m.group(2)
    return feats


def iface_root_qdisc(iface):
    out = run(["tc", "qdisc", "show", "dev", iface, "root"], timeout=5).stdout.split()
    return out[1] if len(out) > 1 else ""


def probe_path_mtu_quick():
    """检测版快速探测：-W1 仅打 1.1.1.1（与 check.sh 一致）。"""
    for size in (1472, 1452, 1424, 1392):
        if run(["ping", "-c1", "-W1", "-M", "do", "-s", str(size), "1.1.1.1"],
               timeout=6).returncode == 0:
            return size + 28
    return 0


def cmd_check():
    cfg = config_read()
    ipt_cmd = check_ipt_backend()
    out_iface = detect_outbound_iface()

    echo(f"🔍 开始系统状态检测（Net-Optimize v{VERSION}）...")
    c_title()

    # === [1] 网络优化关键状态 ===
    echo("🌐 [1] 网络优化关键状态")
    c_sep()
    cc = get_sysctl("net.ipv4.tcp_congestion_control")
    qdisc = get_sysctl("net.core.default_qdisc")
    # 优先读取出口网卡实际 qdisc，无法获取时回退 sysctl 默认值
    iface_qdisc = iface_root_qdisc(out_iface) if out_iface and have_cmd("tc") else ""
    display_qdisc = iface_qdisc or qdisc

    aggressive = cfg.get("AGGRESSIVE_MODE", "") == "1" or \
        (display_qdisc == "pfifo_fast" and
         get_sysctl("net.core.netdev_max_backlog") == "1000000")
    if aggressive:
        green("⚡ 激进模式：已开启")
    if cc.startswith("bbr"):
        green(f"✅ 拥塞算法：{cc}")
    else:
        yellow(f"⚠️ 拥塞算法：{cc}（非 BBR 系列）")
    if iface_qdisc:
        green(f"✅ 当前队列（{out_iface}）：{iface_qdisc}")
    else:
        green(f"✅ 默认队列：{qdisc}")
    if has_sysctl_key("net.ipv4.tcp_mtu_probing"):
        green(f"✅ TCP MTU 探测：{get_sysctl('net.ipv4.tcp_mtu_probing')}")

    echo("✅ TCP 参数：")
    for k in ("tcp_window_scaling", "tcp_sack", "tcp_notsent_lowat",
              "tcp_no_metrics_save", "tcp_autocorking", "tcp_thin_linear_timeouts"):
        echo(f"   🔹 {k} = {get_sysctl('net.ipv4.' + k)}")

    echo("✅ 低延迟轮询：")
    bp, br = get_sysctl("net.core.busy_poll"), get_sysctl("net.core.busy_read")
    if bp.isdigit() and int(bp) > 0:
        green(f"   ✅ busy_poll={bp} μs busy_read={br} μs")
    else:
        echo(f"   🔹 busy_poll={bp} busy_read={br}（0=关闭）")

    echo("✅ UDP 缓冲：")
    for k in ("udp_rmem_min", "udp_wmem_min", "udp_mem"):
        echo(f"   🔹 {k} = {get_sysctl('net.ipv4.' + k)}")
    echo("✅ TCP 缓冲：")
    for k in ("tcp_rmem", "tcp_wmem", "tcp_mem"):
        echo(f"   🔹 {k} = {get_sysctl('net.ipv4.' + k)}")

    echo("✅ Core 缓冲（内存自适应）：")
    rmem_max = get_sysctl("net.core.rmem_max")
    echo(f"   🔹 系统内存: {meminfo_kb('MemTotal') // 1024} MB")
    echo(f"   🔹 rmem_default = {get_sysctl('net.core.rmem_default')}")
    echo(f"   🔹 wmem_default = {get_sysctl('net.core.wmem_default')}")
    if rmem_max.isdigit() and int(rmem_max) >= 134217728:
        green(f"   ✅ rmem_max = {rmem_max}（≥128MB）")
    else:
        echo(f"   🔹 rmem_max = {rmem_max}")
    echo(f"   🔹 wmem_max = {get_sysctl('net.core.wmem_max')}")
    echo(f"   🔹 netdev_max_backlog = {get_sysctl('net.core.netdev_max_backlog')}")
    echo("✅ 内存提交策略（ratio=100 防无 swap 小内存机大分配失败）：")
    echo(f"   🔹 vm.overcommit_memory = {get_sysctl('vm.overcommit_memory')}  "
         f"vm.overcommit_ratio = {get_sysctl('vm.overcommit_ratio')}")

    rp = get_sysctl("net.ipv4.conf.all.rp_filter")
    rp_msg = {"0": (yellow, "⚠️ rp_filter = 0（关闭）"),
              "1": (green, "✅ rp_filter = 1（严格）"),
              "2": (green, "✅ rp_filter = 2（松散，推荐）")}
    fn, msg = rp_msg.get(rp, (echo, f"ℹ️ rp_filter = {rp}"))
    fn(msg)

    # === [2] 网卡 Offload / RPS / RFS ===
    c_sep()
    echo("🔧 [2] 网卡 Offload / RPS / RFS")
    c_sep()
    if out_iface:
        echo(f"  出口网卡: {out_iface}")
        if have_cmd("ethtool"):
            feats = ethtool_features(out_iface)
            names = (("generic-receive-offload", "GRO"),
                     ("generic-segmentation-offload", "GSO"),
                     ("tcp-segmentation-offload", "TSO"),
                     ("scatter-gather", "SG"))
            offloads = " ".join(
                ("✅" if feats.get(f) == "on" else "❌") + s for f, s in names)
            echo(f"  Offload: {offloads}")

            lro = feats.get("large-receive-offload", "")
            if lro == "on":
                yellow("  ⚠️ LRO: on（开启转发时应关闭，重跑主脚本可修正）")
            else:
                echo(f"  🔹 LRO: {lro or '不支持'}（off 为正常）")

            ugro = feats.get("rx-udp-gro-forwarding", "")
            useg = feats.get("tx-udp-segmentation", "")
            if ugro == "on":
                green("  ✅ UDP GRO 转发: on（QUIC/Hysteria2/TUIC 加速）")
            else:
                echo(f"  🔹 UDP GRO 转发: {ugro or '不支持'}")
            if useg == "on":
                green("  ✅ UDP 发送分段卸载: on")
            else:
                echo(f"  🔹 UDP 发送分段卸载: {useg or '不支持'}")

        m = re.search(r"qlen (\d+)",
                      run(["ip", "link", "show", out_iface], timeout=5).stdout)
        if m:
            txql = int(m.group(1))
            if txql >= 10000:
                green(f"  ✅ txqueuelen: {txql}（激进）")
            else:
                echo(f"  🔹 txqueuelen: {txql}")

        rps_mask = ""
        for q in glob.glob(f"/sys/class/net/{out_iface}/queues/rx-*/rps_cpus"):
            rps_mask = read_text(q).strip()
            break
        if rps_mask and rps_mask.replace(",", "").strip("0"):
            green(f"  ✅ RPS 掩码: {rps_mask}")
        else:
            echo("  🔹 RPS: 未启用或单核")
        rfs = read_text("/proc/sys/net/core/rps_sock_flow_entries").strip()
        if rfs.isdigit() and int(rfs) > 0:
            green(f"  ✅ RFS: entries={rfs}")
        else:
            echo("  🔹 RFS: 未启用")
        if have_cmd("tc"):
            tcq = iface_root_qdisc(out_iface)
            if tcq:
                echo(f"  🔹 tc qdisc: {tcq}")

        xps_mask = ""
        for q in glob.glob(f"/sys/class/net/{out_iface}/queues/tx-*/xps_cpus"):
            xps_mask = read_text(q).strip()
            break
        if xps_mask and xps_mask.replace(",", "").strip("0"):
            green(f"  ✅ XPS 掩码: {xps_mask}")
        else:
            echo("  🔹 XPS: 未启用或单队列网卡")

        if have_cmd("ethtool"):
            coal = run(["ethtool", "-c", out_iface], timeout=5).stdout
            if coal.strip():
                m1 = re.search(r"Adaptive RX:\s*(\S+)", coal)
                m2 = re.search(r"^rx-usecs:\s*(\S+)", coal, re.M)
                arx = m1.group(1) if m1 else ""
                rxu = m2.group(1) if m2 else "?"
                if arx == "on":
                    green(f"  ✅ 中断合并: adaptive-rx on，rx-usecs={rxu}")
                else:
                    echo(f"  🔹 中断合并: adaptive-rx={arx or 'off'}，rx-usecs={rxu}")
            else:
                echo("  🔹 中断合并: 网卡不支持查询")

        r = run(["ip", "-o", "link", "show", "type", "wireguard"], timeout=5)
        wg_ifaces = [l.split(": ")[1].split("@")[0]
                     for l in r.stdout.splitlines() if ": " in l]
        if wg_ifaces:
            green(f"  ✅ WireGuard 接口: {' '.join(wg_ifaces)}")
            wg_gro = ethtool_features(wg_ifaces[0]).get("rx-udp-gro-forwarding", "")
            if wg_gro == "on":
                green(f"    ✅ WG 接口 UDP GRO 转发: on（{wg_ifaces[0]}）")
            else:
                echo(f"    🔹 WG 接口 UDP GRO 转发: {wg_gro or '不支持'}（{wg_ifaces[0]}）")
        else:
            echo("  🔹 WireGuard: 未检测到接口")

        if os.path.isfile("/etc/udev/rules.d/99-net-optimize-offload.rules"):
            green("  ✅ offload 持久化已配置")
        if os.path.isfile("/etc/tmpfiles.d/net-optimize-rps.conf"):
            green("  ✅ RPS/RFS/XPS 持久化已配置")
    else:
        yellow("  ⚠️ 无法检测出口网卡")

    # === [2b] CPU 调频 ===
    c_sep()
    echo("⚡ [2b] CPU 调频策略")
    c_sep()
    gov_files = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
    if gov_files:
        govs = [read_text(g).strip() for g in gov_files]
        perf = sum(1 for g in govs if g == "performance")
        if perf == len(govs):
            green(f"  ✅ CPU 调频: performance（{len(govs)} 核全部）")
        else:
            yellow(f"  ⚠️ CPU 调频: {govs[-1]}（{perf}/{len(govs)} 核为 performance）")
        if os.path.isfile("/etc/tmpfiles.d/net-optimize-cpufreq.conf"):
            green("  ✅ CPU 调频持久化已配置")
        else:
            echo("  🔹 CPU 调频持久化未配置")
    else:
        echo("  ℹ️ CPU 调频: 不支持（容器/虚拟化环境）")

    # === [2c] MPTCP ===
    c_sep()
    echo("🔀 [2c] MPTCP 多路径传输")
    c_sep()
    if has_sysctl_key("net.mptcp.enabled"):
        if get_sysctl("net.mptcp.enabled") == "1":
            green("  ✅ MPTCP: 已启用")
        else:
            yellow("  ⚠️ MPTCP: 已安装但未启用")
        if os.path.isfile("/etc/sysctl.d/98-net-optimize-mptcp.conf"):
            green("  ✅ MPTCP 持久化已配置")
        else:
            echo("  🔹 MPTCP 持久化未配置")
    else:
        echo("  ℹ️ MPTCP: 内核不支持（需要 5.6+）")

    # === [3] 游戏 QoS 状态 ===
    c_sep()
    echo("🎮 [3] 游戏低延迟 QoS")
    c_sep()
    qos_scheme = cfg.get("GAME_QOS_SCHEME", "none")
    adaptive_active = run(["systemctl", "is-active", ADAPTIVE_QOS_SERVICE],
                          timeout=5).returncode == 0

    if adaptive_active:
        green("  🔄 自适应 QoS：运行中")
        try:
            aq = json.loads(read_text(ADAPTIVE_CONF) or "{}")
        except Exception:  # noqa
            aq = {}
        thr = aq.get("threshold", 1048576)
        itv = aq.get("interval", 2)
        cd = aq.get("cooldown", 10)
        echo(f"    → 阈值: {thr // 1024} KB/s  采样: {itv}s  冷却: {cd}s")
        echo("    → 流量 ≥ 阈值（入站或出站）→ pfifo_fast（抢带宽）")
        echo(f"    → 流量 < 阈值 持续 {cd}s → cake/prio（游戏低延迟）")
        if aq.get("has_length") is False:
            yellow("    → ⚠️ -m length 不可用，AF41 标记已跳过")

        if out_iface and have_cmd("tc"):
            cur = iface_root_qdisc(out_iface)
            if cur in ("pfifo_fast", "pfifo"):
                yellow(f"    → 当前状态: ⚡ 抢带宽模式 (qdisc={cur})")
            elif cur == "cake":
                green("    → 当前状态: 🎮 游戏低延迟模式 (cake)")
            elif cur == "prio":
                green("    → 当前状态: 🎮 游戏低延迟模式 (prio+fq_codel)")
            else:
                echo(f"    → 当前状态: qdisc={cur}")

        logs = run(["journalctl", "-t", "adaptive-qos", "--no-pager", "-n", "3",
                    "--output=short-iso"], timeout=10,
                   env_extra={"TZ": "Asia/Shanghai"}).stdout.strip()
        if logs:
            echo("    → 最近切换日志（北京时间）:")
            for line in logs.splitlines():
                echo(f"      {line}")
    elif qos_scheme == "cake":
        green("  ✅ QoS 方案: cake diffserv4（4 档自动分流）")
        echo("    → Voice（游戏小包）> Video > Best Effort > Bulk（视频大流）")
        if out_iface and have_cmd("tc"):
            out = run(["tc", "qdisc", "show", "dev", out_iface], timeout=5).stdout
            if re.search(r"cake", out, re.I):
                green("    ✅ cake qdisc 已生效")
                s = run(["tc", "-s", "qdisc", "show", "dev", out_iface],
                        timeout=5).stdout
                keep = False
                shown = 0
                for line in s.splitlines():
                    if re.search(r"cake", line, re.I):
                        keep = True
                    if keep and shown < 6:
                        echo(f"    {line}")
                        shown += 1
            else:
                yellow("    ⚠️ cake qdisc 未在网卡上生效（可能被其他服务覆盖）")
    elif qos_scheme == "prio":
        green("  ✅ QoS 方案: prio + fq_codel（3 档手动分流）")
        echo("    → band 0（高优先）: DSCP EF/AF41 + UDP 小包")
        echo("    → band 1（普通）: 一般流量")
        echo("    → band 2（低优先）: Bulk 流量")
        if out_iface and have_cmd("tc"):
            out = run(["tc", "qdisc", "show", "dev", out_iface], timeout=5).stdout
            if re.search(r"prio", out):
                green("    ✅ prio qdisc 已生效")
                echo("    tc qdisc 详情:")
                for line in out.splitlines()[:8]:
                    echo(f"    {line}")
                fcnt = len([l for l in run(
                    ["tc", "filter", "show", "dev", out_iface, "parent", "1:"],
                    timeout=5).stdout.splitlines() if "filter" in l])
                if fcnt > 0:
                    green(f"    ✅ tc filter 规则: {fcnt} 条")
                else:
                    yellow("    ⚠️ tc filter 未发现")
            else:
                yellow("    ⚠️ prio qdisc 未在网卡上生效（可能被其他服务覆盖）")
    else:
        if aggressive:
            echo("  ℹ️ 游戏 QoS 未启用（激进模式下互斥）")
        else:
            echo("  ℹ️ 游戏 QoS 未启用（ENABLE_GAME_QOS=0 或未运行新版主脚本）")

    # DSCP 标记详情（区分 EF 和 AF41）
    echo("")
    echo("  DSCP 标记详情:")
    ef4 = af414 = ef6 = af416 = 0
    for cmd in V4_CMDS:
        if have_cmd(cmd) and ipt_count(cmd, PAT_DSCP) > 0:
            ef4, af414 = ipt_count(cmd, PAT_EF), ipt_count(cmd, PAT_AF41)
            break
    for cmd in V6_CMDS:
        if have_cmd(cmd) and ipt_count(cmd, PAT_DSCP) > 0:
            ef6, af416 = ipt_count(cmd, PAT_EF), ipt_count(cmd, PAT_AF41)
            break
    green(f"    ✅ IPv4 EF (QUIC 加速): {ef4} 条") if ef4 else echo("    🔹 IPv4 EF: 未发现")
    green(f"    ✅ IPv4 AF41 (游戏小包): {af414} 条") if af414 else echo("    🔹 IPv4 AF41: 未发现")
    green(f"    ✅ IPv6 EF (QUIC 加速): {ef6} 条") if ef6 else echo("    🔹 IPv6 EF: 未发现")
    green(f"    ✅ IPv6 AF41 (游戏小包): {af416} 条") if af416 else echo("    🔹 IPv6 AF41: 未发现")

    # === [4] conntrack ===
    c_sep()
    echo("🔗 [4] conntrack / netfilter 状态")
    c_sep()
    if conntrack_available():
        green("✅ nf_conntrack 可用")
        echo(f"  🔸 nf_conntrack_max = {get_sysctl('net.netfilter.nf_conntrack_max')}")
        echo(f"  🔸 udp_timeout = {get_sysctl('net.netfilter.nf_conntrack_udp_timeout')}")
        echo(f"  🔸 udp_timeout_stream = "
             f"{get_sysctl('net.netfilter.nf_conntrack_udp_timeout_stream')}")
        echo(f"  🔸 tcp_timeout_established = "
             f"{get_sysctl('net.netfilter.nf_conntrack_tcp_timeout_established')}")
        # 哈希桶（扩容到 max/4 减少高并发查表碰撞）
        ct_max = get_sysctl("net.netfilter.nf_conntrack_max")
        ct_hash = read_text("/sys/module/nf_conntrack/parameters/hashsize").strip() or "N/A"
        if ct_hash.isdigit() and ct_max.isdigit() and int(ct_hash) >= int(ct_max) // 4:
            green(f"  ✅ hashsize = {ct_hash}（≥ max/4，碰撞优化已生效）")
        else:
            echo(f"  🔸 hashsize = {ct_hash}（建议 ≥ max/4，重跑主脚本可优化）")
        if os.path.isfile("/etc/modprobe.d/net-optimize-conntrack.conf"):
            green("  ✅ hashsize 持久化已配置（modprobe.d）")
    else:
        yellow("ℹ️ nf_conntrack 未启用")

    if os.path.isfile("/proc/net/nf_conntrack"):
        # 行格式：ipv4/ipv6 2 tcp/udp ...（协议在第 3 列）
        lines = read_text("/proc/net/nf_conntrack").splitlines()
        tcp_c = sum(1 for l in lines if len(l.split()) > 2 and l.split()[2] == "tcp")
        udp_c = sum(1 for l in lines if len(l.split()) > 2 and l.split()[2] == "udp")
        total_c = len(lines)
        echo(f"  🔸 TCP={tcp_c} UDP={udp_c} "
             f"Other={max(total_c - tcp_c - udp_c, 0)} Total={total_c}")
    if have_cmd("conntrack"):
        r = run(["conntrack", "-C"], timeout=10)
        echo(f"  🔸 conntrack -C = "
             f"{r.stdout.strip() if r.returncode == 0 else 'N/A'}")

    ct_found = False
    for cmd in V4_CMDS:
        if not have_cmd(cmd):
            continue
        inv_i = len([l for l in ipt_rules(cmd, "filter", "INPUT")
                     if re.search(r"conntrack.*INVALID.*DROP", l)])
        inv_o = len([l for l in ipt_rules(cmd, "filter", "OUTPUT")
                     if re.search(r"conntrack.*INVALID.*DROP", l)])
        if inv_i >= 1 and inv_o >= 1:
            green(f"✅ INVALID DROP（INPUT+OUTPUT）[{cmd}]")
            ct_found = True
            break
    if not ct_found:
        yellow("⚠️ INVALID DROP 规则不完整")

    # === [5] ulimit ===
    c_sep()
    echo("📂 [5] ulimit / fd")
    c_sep()
    try:
        import resource
        green(f"✅ ulimit -n：{resource.getrlimit(resource.RLIMIT_NOFILE)[0]}")
    except Exception:  # noqa
        echo("  ulimit -n：N/A")
    if os.path.isfile("/etc/security/limits.d/99-net-optimize.conf"):
        green("✅ limits.d 已配置")
    m = re.search(r"^DefaultLimitNOFILE.*$",
                  read_text("/etc/systemd/system.conf"), re.M)
    if m:
        green(f"✅ systemd: {m.group(0)}")

    # === [6] MSS Clamping ===
    c_sep()
    echo("📡 [6] MSS Clamping（IPv4 + IPv6）")
    c_sep()
    for label, cmds in (("IPv4", V4_CMDS), ("IPv6", V6_CMDS)):
        found = False
        for cmd in cmds:
            if not have_cmd(cmd):
                continue
            cnt = ipt_count(cmd, PAT_TCPMSS)
            if cnt == 0:
                continue
            found = True
            if cnt == 1:
                green(f"✅ {label} TCPMSS：1 条 [{cmd}]")
            else:
                yellow(f"⚠️ {label} TCPMSS：{cnt} 条 [{cmd}]")
            out = run([cmd, "-t", "mangle", "-L", "POSTROUTING", "-n", "-v"],
                      timeout=5).stdout
            for line in out.splitlines():
                if "TCPMSS" in line:
                    echo(line)
            break
        if not found:
            echo(f"  ℹ️ {label} TCPMSS：未发现")

    # 实测路径 MTU vs 配置 MSS
    if have_cmd("ping"):
        pmtu = probe_path_mtu_quick()
        if pmtu:
            ideal = pmtu - 40
            rule_mss = cfg.get("MSS_VALUE", "")
            echo(f"  🔸 实测路径 MTU = {pmtu} → 理想 MSS = {ideal}")
            if rule_mss.isdigit():
                if int(rule_mss) == ideal:
                    green(f"  ✅ 配置 MSS={rule_mss} 与实际路径匹配")
                else:
                    yellow(f"  ⚠️ 配置 MSS={rule_mss} ≠ 理想值 {ideal}"
                           f"（重跑主脚本可自动校正）")
        else:
            echo("  🔹 路径 MTU 探测失败（ICMP 不通），跳过 MSS 匹配检查")
    if os.path.isfile(CONFIG_FILE):
        green("✅ 配置文件：")
        for line in read_text(CONFIG_FILE).splitlines():
            echo(f"   {line}")

    # === [7] initcwnd ===
    c_sep()
    echo("📡 [7] initcwnd / 路由优化")
    c_sep()
    for fam, args in (("IPv4", ["ip", "-4"]), ("IPv6", ["ip", "-6"])):
        gw = run(args + ["route", "show", "default"], timeout=5).stdout
        m = re.search(r"initcwnd (\d+)", gw)
        if m:
            cw = int(m.group(1))
            if fam == "IPv4" and cw >= 64:
                green(f"  ✅ {fam} initcwnd={cw}（激进）")
            else:
                green(f"  ✅ {fam} initcwnd={cw}")
        else:
            echo(f"  🔹 {fam} initcwnd 未设置")

    # === [8] UDP 监听 ===
    c_sep()
    echo("🧷 [8] UDP 监听 / 活跃连接")
    c_sep()
    if have_cmd("ss"):
        out = run(["ss", "-u", "-l", "-n", "-p"], timeout=10).stdout
        for line in out.splitlines()[:20]:
            echo(line)
    if have_cmd("conntrack"):
        echo("✅ conntrack 活跃：")
        for proto in ("udp", "tcp"):
            r = run(["conntrack", "-L", "-p", proto], timeout=15)
            n = len([l for l in r.stdout.splitlines() if l.strip()])
            echo(f"  🔸 {proto.upper()}：{n}")

    # === [9] sysctl 一致性 ===
    c_sep()
    echo("🗂 [9] sysctl 持久化")
    c_sep()
    if os.path.isfile(SYSCTL_AUTH_FILE):
        green(f"✅ 主配置：{SYSCTL_AUTH_FILE}")
    else:
        yellow(f"⚠️ 未发现 {SYSCTL_AUTH_FILE}")
    if os.path.isfile(SYSCTL_OVERRIDE_FILE):
        green(f"✅ Override：{SYSCTL_OVERRIDE_FILE}")
    else:
        yellow(f"⚠️ 未发现 {SYSCTL_OVERRIDE_FILE}")

    if os.path.isfile(SYSCTL_AUTH_FILE):
        echo("  关键项对比：")
        content = read_text(SYSCTL_AUTH_FILE)
        for k in ("net.core.default_qdisc", "net.ipv4.tcp_congestion_control",
                  "net.ipv4.tcp_window_scaling", "net.ipv4.tcp_sack",
                  "net.core.rmem_max", "net.core.wmem_max",
                  "net.ipv4.conf.all.rp_filter", "net.netfilter.nf_conntrack_max",
                  "net.core.busy_poll", "net.ipv4.tcp_thin_linear_timeouts",
                  "net.ipv4.tcp_max_tw_buckets"):
            rt = get_sysctl(k)
            vals = re.findall(rf"^\s*{re.escape(k)}\s*=\s*(.+?)\s*$", content, re.M)
            vals = [v for v in vals if not v.startswith("#")]
            fv = vals[-1] if vals else "N/A"
            rt_n = re.sub(r"\s+", " ", rt).strip()
            fv_n = re.sub(r"\s+", " ", fv).strip()
            if fv_n == "N/A":
                echo(f"    ℹ️ {k}: runtime={rt}")
            elif rt_n != fv_n:
                if k in ("net.core.default_qdisc",
                         "net.ipv4.tcp_congestion_control"):
                    echo(f"    ℹ️ {k}: runtime={rt_n}（外部设置）, file={fv_n}")
                else:
                    yellow(f"    ⚠️ {k}: runtime={rt} file={fv}")
            else:
                green(f"    ✅ {k}: {rt}")

    disabled = len(glob.glob("/etc/sysctl.d/*.disabled-by-net-optimize-*"))
    if disabled > 0:
        yellow(f"  ℹ️ {disabled} 个被禁用的冲突文件")

    # === [10] 开机自启 ===
    c_sep()
    echo("🛠 [10] 开机自启服务")
    c_sep()
    svc_state("net-optimize.service")
    svc_state(f"{ADAPTIVE_QOS_SERVICE}.service")
    if os.path.isfile(SCRIPT_PATH):
        green(f"✅ 主脚本已安装：{SCRIPT_PATH}（--boot/--daemon 共用）")
    else:
        yellow(f"⚠️ 主脚本缺失：{SCRIPT_PATH}")
    if os.access("/etc/networkd-dispatcher/routable.d/50-initcwnd", os.X_OK):
        green("✅ networkd-dispatcher initcwnd 钩子已安装")
    for old in ("/usr/local/sbin/net-optimize-apply",
                "/usr/local/sbin/net-optimize-adaptive-qos",
                "/usr/local/sbin/net-optimize-ultimate.sh"):
        if os.path.exists(old):
            echo(f"  ℹ️ 旧版 bash 遗留文件仍存在（已不被引用，可删除）：{old}")
    if os.path.isfile(CONNTRACK_MODULES_CONF):
        green("✅ conntrack 模块开机加载")

    # === [11] Nginx ===
    c_sep()
    echo("🔧 [11] Nginx")
    c_sep()
    if have_cmd("apt-cache"):
        if have_cmd("nginx"):
            ver = run(["nginx", "-v"], timeout=5).stderr.strip().split("/")[-1]
            green(f"✅ Nginx {ver}")
            if run(["systemctl", "is-active", "nginx"], timeout=5).returncode == 0:
                green("✅ 运行中")
            else:
                yellow("⚠️ 未运行")
        else:
            echo("  ℹ️ 未安装")
        if os.path.isfile(NGINX_CRON):
            green("✅ 自动更新 cron 已配置")

    # === [12] 系统信息 ===
    c_sep()
    echo("💻 [12] 系统信息")
    c_sep()
    echo(f"  {'内核':<10}: {run(['uname', '-r'], timeout=5).stdout.strip()}")
    echo(f"  {'CPU':<10}: {os.cpu_count() or '?'} 核")
    echo(f"  {'内存':<10}: {meminfo_kb('MemTotal') // 1024} MB")
    echo(f"  {'可用':<10}: {meminfo_kb('MemAvailable') // 1024} MB")
    up = run(["uptime", "-p"], timeout=5).stdout.strip()
    echo(f"  {'运行':<10}: {up or '?'}")
    if os.path.isfile(SCRIPT_PATH):
        m = re.search(r'VERSION = "([^"]+)"', read_text(SCRIPT_PATH))
        if m:
            green(f"✅ 脚本版本：v{m.group(1)}")
    elif os.path.isfile("/usr/local/sbin/net-optimize-ultimate.sh"):
        m = re.search(r"v\d+\.\d+\.\d+",
                      read_text("/usr/local/sbin/net-optimize-ultimate.sh"))
        if m:
            green(f"✅ 脚本版本：{m.group(0)}（bash 旧版）")
    if ipt_cmd:
        echo(f"  ℹ️ iptables 后端：{ipt_cmd}")
    if out_iface:
        echo(f"  ℹ️ 出口网卡：{out_iface}")
    c_title()
    green("🎉 检测完成")


def main():
    parser = argparse.ArgumentParser(
        description=f"Net-Optimize-Ultimate v{VERSION}（Python 重构版，"
                    "默认执行完整优化）")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--boot", action="store_true",
                   help="开机恢复模式（原 net-optimize-apply，由 systemd 调用）")
    g.add_argument("--daemon", action="store_true",
                   help="自适应 QoS 守护进程（由 systemd 服务调用）")
    g.add_argument("--nginx-upgrade", action="store_true",
                   help="Nginx 安装/升级（由 cron 调用，输出到日志）")
    g.add_argument("--reapply-initcwnd", action="store_true",
                   help="重新应用 initcwnd（由 networkd-dispatcher 钩子调用）")
    g.add_argument("--check", action="store_true",
                   help="完整状态检测（原 net-optimize-check.sh，带彩色判定）")
    g.add_argument("--reset", action="store_true",
                   help="卸载网络优化：清除全部优化配置，恢复系统默认"
                        "（原 net-optimize-reset.sh）")
    g.add_argument("--status", action="store_true", help="仅打印优化状态报告")
    g.add_argument("--version", action="store_true", help="打印版本号")
    args, argv = parser.parse_known_args()

    if args.version:
        echo(f"Net-Optimize-Ultimate v{VERSION}")
    elif args.boot:
        require_root()
        cmd_boot_apply()
    elif args.daemon:
        require_root()
        cmd_daemon()
    elif getattr(args, "nginx_upgrade"):
        require_root()
        cmd_nginx_upgrade()
    elif getattr(args, "reapply_initcwnd"):
        require_root()
        cmd_reapply_initcwnd()
    elif args.check:
        cmd_check()
    elif args.reset:
        require_root()
        cmd_reset()
    elif args.status:
        print_status()
    else:
        main_optimize(sys.argv[1:])


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa  等价 bash 的 ERR trap：定位到出错行
        import traceback
        tb = traceback.extract_tb(sys.exc_info()[2])[-1]
        echo(f"❌ 出错：{tb.filename}:{tb.lineno} -> {tb.line} ({e})")
        sys.exit(1)

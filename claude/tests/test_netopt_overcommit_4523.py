#!/usr/bin/env python3
"""net-optimize 4.5.23：vm.overcommit_memory 一律 0（仓库主人 10/29：「网路优化代码好像还有问题啊gcp机器又卡死了」）。
  GCP e2-micro 1G 内存跑「1 档」：以前没装容器就写 overcommit=2（严格提交记账），sing-box / xray / AdGuard 这些 Go 进程
  预留的虚拟内存一算就顶满 CommitLimit，fork 全报 [Errno 12] Cannot allocate memory——MSS 规则写不进、nginx -v 起不来、
  报告全是 N/A、连 reboot 都起不来，重启后临时 swap 没了更起不来。
  现在：写 0、不写 ratio；开机恢复先把老版本写的 2 改掉（文件里改回 0、当场也写 0）；--check 见到 2 就提醒。
"""
import os, io, sys, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
check(f"版本号 ≥ 4.5.23（{n.VERSION}）", tuple(map(int, n.VERSION.split("."))) >= (4, 5, 23))

out = {}
n.meminfo_kb = lambda k: 974092
real_write_text = n.write_text
n.write_text = lambda p, c, **k: out.__setitem__(p, c)
n.FINAL_CC, n.FINAL_QDISC = "bbr", "fq"
with contextlib.redirect_stdout(io.StringIO()):
    n.write_sysctl_conf()
txt = out.get(n.SYSCTL_AUTH_FILE, "")
check("1G 机、没装容器：写的是 vm.overcommit_memory = 0", "vm.overcommit_memory = 0" in txt)
check("不再写 2、也不写 overcommit_ratio", "vm.overcommit_memory = 2" not in txt and "overcommit_ratio" not in txt)
n.write_text = real_write_text

# 开机恢复：老版本写的 2 改回 0
tmp = tempfile.mkdtemp()
n.SYSCTL_AUTH_FILE = f"{tmp}/99-net-optimize.conf"
old = "vm.swappiness = 1\nvm.overcommit_memory = 2\nvm.overcommit_ratio = 100\nkernel.pid_max = 4194304\n"
open(n.SYSCTL_AUTH_FILE, "w").write(old)
procs = {}
real_read = n.read_text
n.read_text = lambda p, d="": "2\n" if p == "/proc/sys/vm/overcommit_memory" else real_read(p, d)
n.write_proc = lambda p, v: procs.__setitem__(p, v) or True
healed = n.heal_overcommit()
new = open(n.SYSCTL_AUTH_FILE).read()
check(f"文件里改回 0、ratio 去掉、别的行不动（{new.splitlines()}）",
      new == "vm.swappiness = 1\nvm.overcommit_memory = 0\nkernel.pid_max = 4194304\n")
check("当场也写 0", healed and procs.get("/proc/sys/vm/overcommit_memory") == 0)
procs.clear()
n.read_text = lambda p, d="": "0\n" if p == "/proc/sys/vm/overcommit_memory" else real_read(p, d)
check("已经是 0：不动", n.heal_overcommit() is False and not procs and open(n.SYSCTL_AUTH_FILE).read() == new)
boot = src[src.index("def cmd_boot_apply"):]
check("开机恢复在 sysctl --system 之前先修", boot.index("heal_overcommit()") < boot.index('"sysctl", "-e", "--system"'))
check("--check 见到 2 就提醒（不再只认有容器的）", 'if _oc == "2":' in src)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""net-optimize 4.5.21 —— 仓库主人 10/26（DMIT）：跑完优化问「是否立即重启」打了 y，却显示「请稍后手动重启」，「怎么没有重启」。
  根因：跑的那几分钟里手机终端碰了 Home 键（屏上那个 ^[[H），ESC[H 留在输入里，跟后面的 y 连成「ESC[Hy」，不认识就当成否。
  反过来更危险：跑的时候早按了 y 回车，问的时候只敲回车，老代码直接重启了（断开所有连接）。
  现在：问之前丢掉还没读走的键（tcflush），回答里的按键码去掉；换内核那几问（_ask_tty）同样处理；
  面板的 _ask 也去掉按键码（编号、y 前面混进 ESC[H 照样认得出）。
  用真的伪终端（pty）跑：键是真按进终端输入队列的，跟手机上一样。
"""
import os, re, sys, pty, time, select, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NETOPT, XY = os.path.join(ROOT, "net-optimize.py"), os.path.join(ROOT, "xy-installer.py")
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def in_pty(child_code, early=b"", answer=b"", wait_for=b"", timeout=10):
    """子进程跑在一个新伪终端里：先往终端塞 early（「跑的时候」误碰的键），看到 wait_for 提示后再打 answer。"""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(sys.executable, [sys.executable, "-c", child_code])
    if early:
        os.write(fd, early)
    out, sent, t0 = b"", False, time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not sent and wait_for and wait_for in out:
            time.sleep(0.2); os.write(fd, answer); sent = True
    os.waitpid(pid, 0)
    return out.decode("utf-8", "replace")

LOAD = ("import importlib.util, sys, time\n"
        "spec = importlib.util.spec_from_file_location('m', %r); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n")

# ── 1. 跑完问重启 ───────────────────────────────────────────────────────────────
REBOOT = (LOAD % NETOPT +
          "m.run = lambda cmd, **k: (print('REBOOT-CALLED', flush=True) if cmd == ['reboot'] else None,"
          " type('R', (), {'returncode': 0, 'stdout': ''})())[1]\n"
          "m.time.sleep = lambda s: None\n"
          "time.sleep(0.6)\n"          # 「跑优化」那几分钟：这时候误碰的键进了终端输入队列
          "m.ask_reboot()\n")
for early, ans, label, want in ((b"\x1b[H", b"y\n", "跑的时候误碰 Home 键（ESC[H），问的时候打 y 回车 → 重启", True),
                                (b"\x1b[A\x1b[D", b"yes\n", "跑的时候碰了方向键，问的时候打 yes → 重启", True),
                                (b"y\n", b"\n", "跑的时候早按了 y 回车，问的时候只敲回车 → 不重启（早按的不替人答）", False),
                                (b"", b"\x1b[Ay\n", "问的时候先碰了 ↑ 再打 y 回车 → 重启", True),
                                (b"", b"\n", "问的时候直接回车 → 默认不重启", False),
                                (b"", b"n\n", "问的时候打 n → 不重启", False)):
    o = in_pty(REBOOT, early, ans, "(y/N)".encode())
    check(label, ("REBOOT-CALLED" in o) == want and ("请稍后手动重启" in o) == (not want))

src = open(NETOPT, encoding="utf-8").read()
mo = src[src.index("def main_optimize"):src.index("def ask_reboot")]
check("整套优化最后走 ask_reboot（不再自己 input）", "ask_reboot()" in mo and "input(" not in mo)

# ── 2. 换内核那几问（_ask_tty 读 /dev/tty） ─────────────────────────────────────────
ASK = LOAD % NETOPT + "time.sleep(0.6)\nprint('GOT=' + repr(m._ask_tty('  现在重启？[y/N]: ')), flush=True)\n"
o = in_pty(ASK, b"y\n", b"\n", b"[y/N]")
check(f"换内核：早按的 y 回车被丢掉，问的时候回车 = 默认（{re.search(r'GOT=.*', o).group(0) if 'GOT=' in o else o[-80:]!r}）", "GOT=''" in o)
o = in_pty(ASK, b"\x1b[H", b"yes\n", b"[y/N]")
check("换内核：混进的按键码去掉，认得出 yes", "GOT='yes'" in o)

# ── 3. 面板的 _ask：编号 / y 前面混进按键码照样认得出 ────────────────────────────────
PANEL = LOAD % XY + "print('GOT=' + repr(m._ask('选择: ')), flush=True)\n"
o = in_pty(PANEL, b"", b"\x1b[H12\n", "选择".encode())
check("面板：ESC[H 后面打 12 → 认成 12", "GOT='12'" in o)
o = in_pty(PANEL, b"", b"\x1bOA\x1b[Dy\n", "选择".encode())
check("面板：方向键（两种写法）后面打 y → 认成 y", "GOT='y'" in o)

# ── 4. 去按键码本身 ─────────────────────────────────────────────────────────────
spec = importlib.util.spec_from_file_location("n", NETOPT); n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
for raw, want in (("\x1b[Hy", "y"), ("\x1b[1;5Cyes\r", "yes"), ("\x1bOBn", "n"), (" y ", "y"), ("\x1b[200~y\x1b[201~", "y"), ("", "")):
    check(f"_clean_answer({raw!r}) → {want!r}", n._clean_answer(raw) == want)
check("版本 ≥ 4.5.21", tuple(map(int, re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1).split("."))) >= (4, 5, 21))

print("ALL PASS" if not bad else f"{bad} FAIL")
sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""网络优化月度自更新：挪到 nginx 升级前 + 换了新版就让自适应 QoS 跟着换（4.5.16）。
仓库主人 10/25：「两个都做吧」——
  一、03:05 自更新，排在 03:10 nginx 升级前（都进排队器，按登记先后跑）
  二、真换了新版（auto_update execv 前打记号）才重启 QoS；已是最新不碰
  三、出口闲着才重启；一直忙就不动（下次开机自然是新版）；守护进程没开就不管
"""
import os, re, sys, io, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "net-optimize.py")
spec = importlib.util.spec_from_file_location("netopt", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

print("一、时刻")
T = {}
m.bj_job_lines = lambda hh, mm, job, cmd, day=None: (T.update(sp=(hh, mm), day=day, job=job) or ([f"x {job}"], [(hh, mm)]))
tmp = tempfile.mkdtemp()
m.SELFUPD_CRON = f"{tmp}/s"; m.NGINX_CRON = f"{tmp}/n"
m.write_selfupd_cron(); s_at = T["sp"]
m.write_nginx_cron(); n_at = T["sp"]
check(f"自更新 {s_at} 在 nginx 升级 {n_at} 之前，同是每月 1 号", s_at < n_at and T["day"] == 1)
check("自更新在 03:00 规则刷新之后（不跟它抢第一个）", s_at > (3, 0))

print("二、换了新版才重启 QoS")
check("auto_update 在 execv 前打记号",
      re.search(r'NETOPT_JUST_UPDATED"\] = "1"\s*#[^\n]*\n\s*os\.execv', src) is not None)
R = []
m.require_root = lambda: None
m.auto_update = lambda argv: None
m.write_selfupd_cron = lambda: None
m.restart_qos_if_idle = lambda *a, **k: R.append(1)
os.environ.pop("NETOPT_JUST_UPDATED", None)
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_self_update()
check("已是最新 → 不碰 QoS", R == [])
os.environ["NETOPT_JUST_UPDATED"] = "1"
with contextlib.redirect_stdout(io.StringIO()):
    m.cmd_self_update()
check("刚换了新版 → 去重启 QoS", R == [1])
os.environ.pop("NETOPT_JUST_UPDATED", None)

print("三、闲着才重启")
spec2 = importlib.util.spec_from_file_location("netopt2", SRC)
n = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(n)
conf = f"{tmp}/aq.conf"; json.dump({"iface": "eth0", "threshold": 1000}, open(conf, "w"))
n.ADAPTIVE_CONF = conf
n.time.sleep = lambda s: None
CMD = []
class Rr:
    def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", ""
ACTIVE = [0]
def fake_run(cmd, timeout=None, **k):
    CMD.append(" ".join(cmd)); return Rr(ACTIVE[0] if "is-active" in cmd else 0)
n.run = fake_run
RATES = []
n._iface_rate = lambda iface, secs=5: RATES.pop(0)
def go(rates, active=0):
    CMD.clear(); RATES[:] = rates; ACTIVE[0] = active
    with contextlib.redirect_stdout(io.StringIO()):
        r = n.restart_qos_if_idle(tries=3, gap=0)
    return r, [c for c in CMD if "restart" in c]
r, rs = go([10])
check("闲着 → 重启一次", r is True and rs == [f"systemctl restart {n.ADAPTIVE_QOS_SERVICE}"])
r, rs = go([5000, 5000, 10])
check("先忙后闲 → 等到闲了再重启", r is True and len(rs) == 1)
r, rs = go([5000, 5000, 5000])
check("一直忙 → 不重启", r is False and rs == [])
r, rs = go([10], active=3)
check("QoS 没开 → 不管", r is False and rs == [])
mv = tuple(int(x) for x in re.search(r'VERSION = "([\d.]+)"', src).group(1).split("."))
check("VERSION ≥ 4.5.16（只动最后一位）", mv >= (4, 5, 16) and mv[:2] == (4, 5))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

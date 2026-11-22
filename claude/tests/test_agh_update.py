#!/usr/bin/env python3
"""12 更新 AdGuard（11/22 加）：1 立即更新（跑官方 --update）/ 2 自动更新（默认开，每天 01:00，可关）。
  立即更新不自己下包：AdGuard 自己的 --update 先校验配置、旧版进 backup 目录，再换二进制、重启服务。
  自动更新每天 01:00 进 bgpeer-jobq 排队（北京时间核对，见 claude/NOTES.md「凌晨定时任务总表」01:00 是空的）；
  排队器那份跟 xy-installer / cn-block / net-optimize / media-stack 里的一模一样，五份要一致。
  版本解析以真二进制核对过：`AdGuardHome --version` 只印一行 `AdGuard Home, version v0.107.79`。
"""
import os, io, re, sys, contextlib, importlib.util, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("agh", os.path.join(ROOT, "adguard-dns.py"))
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# ── 1. 菜单：12 是更新（当前值是版本）、13 是卸载；dispatch 对照 ──
real_agh_version = a._agh_version
real_ensure = a._ensure_autoupd_cron
real_update_menu = a.update_menu
a._installed = lambda: True
a._running = lambda: True
a._privdns_mode = lambda: ""
a._domain = lambda: "dns.example.org"
a._h3_on = lambda: False
a._agh_version = lambda: "v0.107.79"
a._ensure_autoupd_cron = lambda: None
CALLS = []
for f in ("install", "status", "_selfdns_toggle", "_privdns_menu", "dot_clientid", "ios_profile",
          "change_web_port", "change_admin_account", "selfcheck", "uninstall", "phone_probe", "free_port53",
          "change_domain", "doh_h3_toggle", "update_menu"):
    setattr(a, f, (lambda n: lambda *x, **k: CALLS.append(n))(f))

def run_menu(keys):
    q = list(keys); CALLS.clear()
    a._ask = lambda p="": q.pop(0) if q else "0"
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.menu()
    return re.sub(r"\033\[[0-9;]*m", "", b.getvalue())

out = run_menu(["0"])
check("菜单：12 更新 AdGuard（当前：版本）在 13 卸载前面",
      out.find("12. 更新 AdGuard　当前：v0.107.79") >= 0
      and out.find("12. 更新 AdGuard") < out.find("13. 卸载自建DNS"))
run_menu(["12", "0"]); check("12 → update_menu", CALLS == ["update_menu"])
run_menu(["13", "0"]); check("13 → uninstall", CALLS == ["uninstall"])
seen = []
real_update_now = a.update_now
a.update_now = lambda auto=False: seen.append(auto)
old_argv = sys.argv
sys.argv = ["adguard-dns.py", "update"]
try:
    a.main()
finally:
    sys.argv = old_argv
    a.update_now = real_update_now
check("命令行入口：adguard-dns.py update 调 update_now(auto=True)", seen == [True])

# ── 2. 版本解析（真格式以 /tmp 实测为准） ──
a._agh_version = real_agh_version
a._installed = lambda: True
def fake_sh(txt):
    return lambda cmd, check=False: txt
a.sh = fake_sh("AdGuard Home, version v0.107.79\n")
check("版本解析：v0.107.79", a._agh_version() == "v0.107.79")
a.sh = fake_sh("AdGuard Home, version v1.0.0")
check("版本解析：v1.0.0", a._agh_version() == "v1.0.0")
a.sh = fake_sh("garbage\n")
check("版本解析：读不到返回 ''", a._agh_version() == "")
a._installed = lambda: False
check("版本解析：没装返回 ''", a._agh_version() == "")
a._installed = lambda: True

# ── 3. 自动更新开关（cron + off 文件，路径都指到 tmp，不碰系统） ──
a._ensure_autoupd_cron = real_ensure
tmp = tempfile.mkdtemp()
a.AGH_AUTOUPD_OFF = os.path.join(tmp, "agh-autoupdate.off")
a.AGH_AUTOUPD_CRON = os.path.join(tmp, "bgpeer-aghupdate")
a.AGH_AUTOUPD_LOG = os.path.join(tmp, "bgpeer-aghupdate.log")
a.ensure_jobq = lambda: True
check("自动更新默认开（没 off 文件）", a._autoupd_on())
ok = a._autoupd_set(True)
txt = open(a.AGH_AUTOUPD_CRON).read()
check("开：写 cron + 无 off 文件",
      ok and os.path.exists(a.AGH_AUTOUPD_CRON) and not os.path.exists(a.AGH_AUTOUPD_OFF))
check("cron 内容：北京时间 01:00 核对、进排队器 agh-update、调 update、日志",
      '"01"' in txt and "bgpeer-jobq" in txt and "agh-update" in txt
      and "python3 /etc/bgpeer/adguard-dns.py update" in txt and a.AGH_AUTOUPD_LOG in txt)
check("cron 内容：到点核对用的是北京钟点（不是本机时刻）",
      'TZ=CST-8 date +' in txt and '"01"' in txt)
a._autoupd_set(False)
check("关：删 cron + 有 off 文件",
      not os.path.exists(a.AGH_AUTOUPD_CRON) and os.path.exists(a.AGH_AUTOUPD_OFF)
      and not a._autoupd_on())
a._ensure_autoupd_cron()
check("关着时 _ensure 不写 cron", not os.path.exists(a.AGH_AUTOUPD_CRON))
os.remove(a.AGH_AUTOUPD_OFF)
a._ensure_autoupd_cron()
check("开着缺 cron 时 _ensure 补写", os.path.exists(a.AGH_AUTOUPD_CRON))
want = open(a.AGH_AUTOUPD_CRON).read()
a._ensure_autoupd_cron()
check("开着且 cron 内容对时 _ensure 不动", open(a.AGH_AUTOUPD_CRON).read() == want)
open(a.AGH_AUTOUPD_CRON, "w").write("stale\n")
a._ensure_autoupd_cron()
check("开着且 cron 内容不对时 _ensure 重写", open(a.AGH_AUTOUPD_CRON).read() == want)
a._installed = lambda: False
os.remove(a.AGH_AUTOUPD_CRON)
a._ensure_autoupd_cron()
check("没装时 _ensure 不写 cron", not os.path.exists(a.AGH_AUTOUPD_CRON))
a._installed = lambda: True

# ── 4. update_menu 子菜单 ──
a.update_menu = real_update_menu
a._agh_version = lambda: "v0.107.79"
def run_update_menu(keys, off=False):
    if off:
        open(a.AGH_AUTOUPD_OFF, "w").write("1")
    elif os.path.exists(a.AGH_AUTOUPD_OFF):
        os.remove(a.AGH_AUTOUPD_OFF)
    q = list(keys)
    a._ask = lambda p="": q.pop(0) if q else "0"
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        a.update_menu()
    return re.sub(r"\033\[[0-9;]*m", "", b.getvalue())

out = run_update_menu(["0"])
check("子菜单：1 立即更新（当前：版本）/ 2 自动更新（当前：开（每天 01:00））",
      "1. 立即更新　当前：v0.107.79" in out and "2. 自动更新　当前：开（每天 01:00）" in out)
out = run_update_menu(["2", "y", "0"])
check("开着回 y：关掉、cron 撤掉", "每天自动更新已关" in out and not os.path.exists(a.AGH_AUTOUPD_CRON)
      and os.path.exists(a.AGH_AUTOUPD_OFF))
real_set = a._autoupd_set
a._autoupd_set = lambda on: False
out = run_update_menu(["2", "y", "0"])
check("关不掉（写不进）：报 ⚠ 不报 ✔", "⚠ 没关掉" in out and "每天自动更新已关" not in out)
a._autoupd_set = real_set
out = run_update_menu(["2", "n", "0"], off=True)
check("关着回 n：不动", os.path.exists(a.AGH_AUTOUPD_OFF))
out = run_update_menu(["2", "", "0"], off=True)
check("关着回车：打开、cron 写好", "每天自动更新已开" in out and os.path.exists(a.AGH_AUTOUPD_CRON)
      and not os.path.exists(a.AGH_AUTOUPD_OFF))
a._installed = lambda: False
b = io.StringIO()
with contextlib.redirect_stdout(b):
    a.update_menu()
check("没装进更新菜单：先装，不往下走", "还没装 AdGuard Home" in b.getvalue())
a._installed = lambda: True

# ── 5. 更新流程（subprocess 全 stub，不真跑更新） ──
real_run, real_sleep, real_geteuid = a.subprocess.run, a.time.sleep, a.os.geteuid
a.time.sleep = lambda s: None
a.os.geteuid = lambda: 0
class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc; self.stdout = out; self.stderr = err
runs = []
def fake_run(rc=0, out="", err=""):
    def f(cmd, **k):
        runs.append(cmd)
        return _R(rc, out, err)
    return f
def ver_seq(*vs):
    it = iter(vs)
    return lambda: next(it)

a._running = lambda: True
a._agh_version = ver_seq("v0.107.79", "v1.0.0")
a.subprocess.run = fake_run()
a._ask = lambda p="": "y"
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now()
check("立即更新成功：报前后版本、调了 --update",
      ok and "✔ 已更新 v0.107.79 → v1.0.0" in b.getvalue() and runs and "--update" in runs[0])
a._agh_version = ver_seq("v1.0.0", "v1.0.0")
runs.clear()
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now()
check("已经是最新版：明说，不报错", ok and "已经是最新版 v1.0.0" in b.getvalue())
a._agh_version = ver_seq("v0.107.79")
a.subprocess.run = fake_run(rc=1, err="2026/11/22 [error] getting version info err=\"no addresses\"")
runs.clear()
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now()
check("更新失败（rc=1）：明说没成、服务没动、不调后面的",
      not ok and "⚠ 更新没成" in b.getvalue() and "还是 v0.107.79" in b.getvalue())
a._agh_version = ver_seq("v0.107.79", "v1.0.0")
a.subprocess.run = fake_run()
a._running = lambda: False
a.AGH_DIR = tmp
os.makedirs(os.path.join(tmp, "backup-v1.0.0"), exist_ok=True)
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now()
check("更新完服务没起来：报 ⚠ + 旧版备份位置",
      not ok and "没起来" in b.getvalue() and "backup-v1.0.0" in b.getvalue())
a._running = lambda: True
a._agh_version = ver_seq("v0.107.79")
runs.clear()
a._ask = lambda p="": "n"
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now()
check("确认回 n：不跑更新", not ok and runs == [] and "DNS 断一两秒" in b.getvalue())
def _ask_boom(p=""):
    raise AssertionError("cron 调的不该问问题")
a._ask = _ask_boom
a._agh_version = ver_seq("v0.107.79", "v1.0.0")
runs.clear()
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = a.update_now(auto=True)
check("cron 模式：不问问题、直接更新", ok and runs and "DNS 断一两秒" not in b.getvalue())
a.subprocess.run, a.time.sleep, a.os.geteuid = real_run, real_sleep, real_geteuid

# ── 6. 排队器五份一致 ──
files5 = ["xy-installer.py", "cn-block.py", "net-optimize.py", "media-stack.py", "adguard-dns.py"]
def grab(p, name):
    m = re.search(name + r' = r"""(.*?)"""', open(os.path.join(ROOT, p)).read(), re.S)
    return m.group(1) if m else None
def grab_unit(p):
    m = re.search(r'JOBQ_UNIT_TXT = \((.*?)\)\n', open(os.path.join(ROOT, p)).read(), re.S)
    return m.group(1) if m else None
def grab_func(p, name):
    import ast
    lines = open(os.path.join(ROOT, p)).read().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "".join(lines[node.lineno - 1:node.end_lineno])
    return None
check("五份 JOBQ_SH 一模一样", len({grab(f, "JOBQ_SH") for f in files5}) == 1)
check("五份 JOBQ_UNIT_TXT 一模一样", len({grab_unit(f) for f in files5}) == 1)
check("adguard-dns 的 bj_cron_lines / ensure_jobq / jobq_wrap / bj_job_lines 跟主脚本一致",
      all(grab_func("adguard-dns.py", n) == grab_func("xy-installer.py", n)
          for n in ("bj_cron_lines", "ensure_jobq", "jobq_wrap", "bj_job_lines")))
check("五份都写了「五个脚本」，没有「四个脚本」残留",
      all("五个脚本" in open(os.path.join(ROOT, f)).read() for f in files5)
      and all("四个脚本" not in open(os.path.join(ROOT, f)).read() for f in files5))

# ── 7. 总表 + 文档 ──
notes = open(os.path.join(ROOT, "claude", "NOTES.md"), encoding="utf-8").read()
check("NOTES.md 总表：01:00 AdGuard 更新【队】，排在 02:30 前面",
      "每天 01:00 | AdGuard 更新【队】" in notes
      and notes.find("每天 01:00 | AdGuard 更新") < notes.find("每月 3 日起 02:30"))
doc = open(os.path.join(ROOT, "docs", "13-dns.md"), encoding="utf-8").read()
check("文档：菜单里 12 更新、13 卸载，不再有「12 卸载」",
      "12 更新 AdGuard" in doc and "13 卸载自建DNS" in doc and "`12 卸载`" not in doc)
check("文档：12 更新一节写清立即/自动/01:00/排队器/备份/后台 Update Now",
      "每天 **01:00**" in doc and "bgpeer-jobq" in doc and "backup-v*" in doc and "Update Now" in doc)
doc15 = open(os.path.join(ROOT, "docs", "15-cert.md"), encoding="utf-8").read()
check("文档 15-cert：卸载指 13 → 13", "13 → 13 卸载自建 DNS" in doc15 and "13 → 12 卸载" not in doc15)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

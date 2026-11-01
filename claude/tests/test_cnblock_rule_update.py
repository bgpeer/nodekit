#!/usr/bin/env python3
"""屏蔽中国域名 · 5 规则更新（仓库主人 11/01：「可不可以把屏蔽规则和放行规则一起更新……
  5 规则更新 / 1 立即更新 / 2 自动更新 当前：开（每天 2:55）/ 0 返回」）。
  凌晨那一轮以前只比规则集；现在放行名单（作者名单 / 自定义链接 / 单条）变了也一起重注入，
  时间挪到 02:55，紧跟 02:50 节点脚本自更新（同一个排队器，不撞）。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("cb", os.path.join(ROOT, "cn-block.py"))
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
cb.CNBLOCK_FILE = f"{tmp}/cnblock.json"; cb.BGP_DIR = tmp
cb.CRON_FILE = f"{tmp}/cron"; cb.SELF_PATH = os.path.abspath(cb.__file__) + ".notme"
cb.SELFUPD_OFF = f"{tmp}/selfupdate.off"
cb.ensure_jobq = lambda: True
cb.shutil = __import__("shutil")

print("一、时间")
src = open(os.path.join(ROOT, "xy-installer.py")).read()
check("02:55：在 02:50 节点脚本自更新之后、03:00 之前", cb.REFRESH_AT == (2, 55)
      and "SELFUPD_AT   = (2, 50)" in src and cb.REFRESH_AT < (3, 0))
check("xy-installer 修老 cron 也按 02:55", '_heal_bj_cron(CNBLOCK_CRON, 2, 55,' in src)

print("二、cron 跟开关走")
import shutil as _sh
_real_copy = _sh.copy
_sh.copy = lambda a, b: None
cb.cnblock_save({"enabled": True})
cb.setup_cron()
txt = open(cb.CRON_FILE).read() if os.path.exists(cb.CRON_FILE) else ""
check("默认开：装上 cron，注明北京 02:55", "北京时间 02:55" in txt and " refresh " in txt)
cb.cnblock_save({"enabled": True, "auto": False})
cb.setup_cron()
check("关了：cron 撤掉", not os.path.exists(cb.CRON_FILE))
_sh.copy = _real_copy

print("三、菜单")
ANS = []
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"
calls = []
cb.update_now = lambda: calls.append("now") or True
cb.setup_cron = lambda: calls.append("cron")
cb.remove_cron = lambda: calls.append("rm")
def run(f, *a):
    ANS[:] = list(a); b = io.StringIO()
    with contextlib.redirect_stdout(b):
        f()
    return b.getvalue()
cb.cnblock_save({"enabled": True})
out = run(cb.menu, "0")
check("首页：5. 规则更新", "5. 规则更新" in out and "立即更新（拉最新名单" not in out)
out = run(cb.menu, "5", "0", "0")
check("子菜单：1 立即更新 / 2 自动更新 当前：开（每天 02:55）/ 0 返回",
      "1. 立即更新" in out and "2. 自动更新　当前：开（每天 02:55）" in out and "0. 返回" in out)
calls.clear(); run(cb.rule_update_menu, "1", "0")
check("1 = 立即更新", calls == ["now"])
calls.clear(); out = run(cb.rule_update_menu, "2", "", "0")
check("关：先提示、默认否，回车不关", "提示：" in out and cb.auto_on() and calls == [])
calls.clear(); out = run(cb.rule_update_menu, "2", "y", "0")
check("y 才关：存下、撤 cron、屏上 当前：关", not cb.auto_on() and calls == ["rm"] and "自动更新　当前：关" in out)
calls.clear(); out = run(cb.rule_update_menu, "2", "0")
check("再选 2：直接打开、装 cron", cb.auto_on() and calls == ["cron"])

print("四、凌晨那一轮：放行名单一起比")
cb._self_update_copy = lambda: False
cb._cnblk_remote_sets = lambda: {"cnblk-cn-ip": "u"}
cb._rs_hashes = lambda s: {"cnblk-cn-ip": "H"}
restarted = []
cb.sh = lambda c, check=True: restarted.append(c) or "active"
cb.time.sleep = lambda s: None
SIG = ["A"]
cb._wl_sig = lambda cfg: SIG[0]
def night(cfg):
    cb.cnblock_save(cfg); calls.clear(); restarted.clear()
    with contextlib.redirect_stdout(io.StringIO()) as b:
        cb.refresh()
    calls[:] = [c for c in calls if c != "cron"]          # 每轮顺手对齐 cron，不算动作
    return b.getvalue()
out = night({"enabled": True, "wl_sig": "A", "rs_hash": {"cnblk-cn-ip": "H"}})
check("名单、规则集都没变：什么都不动", calls == [] and restarted == [])
out = night({"enabled": True, "wl_sig": "OLD", "rs_hash": {"cnblk-cn-ip": "H"}})
check("名单变了：整套重注入（= 立即更新）", calls == ["now"] and "放行名单有变化" in out)
SIG[0] = None
out = night({"enabled": True, "wl_sig": "OLD", "rs_hash": {"cnblk-cn-ip": "H"}})
check("自定义链接拉不到：名单这次不动", calls == [] and "名单这次不动" in out)
SIG[0] = "A"
out = night({"enabled": True, "wl_sig": "A", "rs_hash": {"cnblk-cn-ip": "OLD"}})
check("只是规则集变了：照旧清缓存重启一次", calls == [] and any("restart sing-box" in c for c in restarted))
out = night({"enabled": True, "auto": False, "wl_sig": "OLD"})
check("关了自动更新还被调到：撤 cron、不动", calls == ["rm"] and restarted == [])
out = night({"enabled": False, "wl_sig": "OLD"})
check("没开屏蔽：不动", calls == [] and restarted == [])

print("五、换本地副本")
spec2 = importlib.util.spec_from_file_location("cb2", os.path.join(ROOT, "cn-block.py"))
cb2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(cb2)
fetched = []
cb2.urllib.request.urlopen = lambda *a, **k: fetched.append(a) or (_ for _ in ()).throw(OSError("x"))
cb2.SELF_PATH = os.path.abspath(cb2.__file__)
cb2.SELFUPD_OFF = f"{tmp}/selfupdate.off"
open(cb2.SELFUPD_OFF, "w").close()
check("菜单 18 关了脚本自动更新：不换、也不发请求", cb2._self_update_copy() is False and fetched == [])
os.remove(cb2.SELFUPD_OFF)
os.environ["CNBLK_FRESH"] = "1"
check("已经是换上的新版在跑：不再换（不会循环）", cb2._self_update_copy() is False and fetched == [])
del os.environ["CNBLK_FRESH"]
check("拉不到：不动", cb2._self_update_copy() is False and len(fetched) == 1)
check("只认 raw（jsDelivr 可能旧，防降级）", cb2.SELF_URL.startswith("https://raw.githubusercontent.com/"))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

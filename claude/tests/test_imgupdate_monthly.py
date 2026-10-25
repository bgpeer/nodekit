#!/usr/bin/env python3
"""「8 更新」加每月自动更新（1.5.388）。仓库主人 10/25：「想让他每个月自动更新一次」，菜单：
    1. 立即更新 / 2. 自动更新　当前：开（每月…）/ 0. 返回
钉住：
  一、菜单：「8」进子菜单，两项都在；默认开；2 来回切
  二、do_imgupdate：没到 3 号 / 这个月做过 / 不在凌晨窗口 / 有人在播 → 不做；该做 → do_update(auto=True)，
      镜像换上了才记这个月做过，没拉全明天再试
  三、do_update(auto=True)：不换脚本、有人在播不问直接返回 None（cron 下不能等输入）
  四、时刻：02:30 排在凌晨所有活前面（02:50 节点自更新、03:00 规则、03:30 元数据…04:15 刷缓存、04:20 AutoFilm）
  五、cron 进排队器、带超时；卸载带走；时区变了重写
"""
import importlib.util, os, sys, tempfile, json
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

D = tempfile.mkdtemp()
ST = {}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **k: ST.update(k)
m.ms_install_dir = lambda: D
m.is_installed = lambda d=None: True
m.read_emby_api_key = lambda d: "K"
m.ok = m.warn = m.info = m.tip = lambda *a, **k: None
m.install_imgup_cron = lambda d: CALLS.append("cron")
CALLS = []

print("一、菜单")
mm = src[src.index("def main_menu("):]; mm = mm[:mm.index("\nif __name__")]
check("主菜单「8」进更新子菜单", 'elif c == "8":\n            update_menu()' in mm)
check("默认开", m.imgup_on() is True)
import io, contextlib
seq = iter(["2", "0"])
m.ask = lambda *a, **k: next(seq)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.update_menu()
out = buf.getvalue()
check("两项都在：1. 立即更新 / 2. 自动更新　当前：开（每月 3 日 02:30）",
      "1. 立即更新" in out and "2. 自动更新" in out and "每月 3 日 02:30" in out and "0. 返回" in out)
check("按 2 → 关", ST.get("imgup_auto") is False and m.imgup_on() is False)
seq = iter(["2", "0"])
with contextlib.redirect_stdout(io.StringIO()):
    m.update_menu()
check("再按 2 → 开，并装上 cron", m.imgup_on() is True and "cron" in CALLS)
seq = iter(["1", "", "0"]); UP = []
m.do_update = lambda from_menu=False, auto=False: UP.append((from_menu, auto))
with contextlib.redirect_stdout(io.StringIO()):
    m.update_menu()
check("按 1 → 立即更新（菜单那条，不是自动）", UP == [(True, False)])

print("二、do_imgupdate")
NOW = {"%Y-%m": "2026-11", "%d": "03", "%H:%M": "02:30"}
m.bj_fmt = lambda fmt, ts=None: NOW.get(fmt, "x")
QUIET = [True]
m.play_quiet_wait = lambda *a, **k: QUIET[0]
RET = [True]
def fake_update(from_menu=False, auto=False):
    UP.append(auto); return RET[0]
m.do_update = fake_update
def run():
    UP.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        m.do_imgupdate()
    return list(UP)
jf = os.path.join(D, "imgupdate.json")
check("3 号 02:30、没做过 → 自动更新一次", run() == [True] and json.load(open(jf))["month"] == "2026-11")
check("这个月做过 → 不再做", run() == [])
os.remove(jf)
NOW["%d"] = "02"; check("还没到 3 号 → 不做", run() == [])
NOW["%d"] = "05"; NOW["%H:%M"] = "14:00"; check("排队器白天才轮到 → 留到明天", run() == [])
NOW["%H:%M"] = "02:31"; QUIET[0] = False; check("有人在播等满了 → 不做", run() == [])
QUIET[0] = True; RET[0] = False
check("镜像没拉全 → 不记这个月，明天再试", run() == [True] and json.load(open(jf))["month"] == "")
RET[0] = True
check("第二天接着试成了", run() == [True] and json.load(open(jf))["month"] == "2026-11")
os.remove(jf); ST["imgup_auto"] = False
check("关着 → 不做", run() == [])
ST["imgup_auto"] = True

print("三、do_update(auto=True)")
up = src[src.index("def do_update("):]; up = up[:up.index("\ndef ", 1)]
check("auto 不换脚本", "if not auto and not os.environ.get(\"MS_SELF_UPDATED\") and self_update():" in up)
i = up.index("someone_playing(_k0)")
check("有人在播：auto 直接 return None，不问", "if auto:\n            return None" in up[i:i + 120]
      and up.index("return None", i) < up.index("ask_yn", i))
check("镜像换上了才返回 True", "_imgs_ok = True" in up and up.rstrip().endswith("return _imgs_ok"))
check("更新时顺手装上每月那条 cron", "install_imgup_cron(d)" in up)

print("四、时刻")
hm = lambda t: tuple(map(int, t.split(":")))
check("02:30，在 02:50 节点脚本自更新 / 03:00 规则刷新之前", hm(m.IMGUP_HOUR_CST) < (2, 50))
_end = hm(m.IMGUP_WINDOW[1]); _end = _end[0] * 60 + _end[1] + 20 + 20   # 最晚起头 + 等没人播 20 分 + 拉镜像约 20 分
check("最晚起头的那一轮也在 04:15 刷目录缓存之前做完",
      _end <= 4 * 60 + 15 and hm(m.IMGUP_WINDOW[0]) <= hm(m.IMGUP_HOUR_CST) < hm(m.IMGUP_WINDOW[1]))

print("五、cron")
spec2 = importlib.util.spec_from_file_location("ms2", SRC)
m2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(m2)
W = {}
m2.write_atomic = lambda p, t, mode=0o644: W.update({p: t})
m2.ensure_jobq = lambda: True
m2.IMGUP_LOG = os.path.join(D, "log", "imgupdate.log")
m2.install_imgup_cron(D)
txt = W.get(m2.IMGUP_CRON, "")
check("进排队器、带超时、调 imgupdate", "ms-imgupdate" in txt and "timeout" in txt and " imgupdate" in txt
      and "imgupdate" in m2.CRON_TIMEOUT)
un = src[src.index("def do_uninstall("):]; un = un[:un.index("\ndef ", 1)]
check("卸载带走", "IMGUP_CRON" in un)
tz = src[src.index("def cron_tz_resync("):]; tz = tz[:tz.index("\ndef ", 1)]
check("时区变了重写", "install_imgup_cron(d)" in tz)
check("子命令 imgupdate 抢锁再跑", 'take_task_lock("imgupdate")' in src)
check("版本 1.5.389 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 389))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

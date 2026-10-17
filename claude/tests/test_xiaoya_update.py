#!/usr/bin/env python3
"""小雅：早先挂上的补全盘、全盘时关掉定时扫描、独立的「更新小雅」（1.5.373）。
仓库主人：「他为什么没有显示全盘，一定要让我挂路径吗」「在小雅里面还要做一个独立的更新按钮吗？可以做」。
钉住：
  一、挂着、没加过路径 → 补全盘；顺手把小雅的定时扫描关掉（自己设过的不动）；加过路径的不动
  二、自己「2 删除所有路径」删掉的不再补回；再挑路径 / 移除小雅后恢复
  三、进小雅那一屏（挂着）就补
  四、更新小雅：没装不做；有人在播先问（默认否）；镜像没变不重建；变了用同一句重建；起不来说清
"""
import contextlib, importlib.util, io, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def quiet(fn, *a, **k):
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = fn(*a, **k)
    return r, m.ANSI_RE.sub("", b.getvalue())

ST = {"scan_spec": []}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **kw: ST.update(kw)
m.explicit_scan_paths = lambda: list(ST.get("scan_spec") or [])
APPLIED = []
m._apply_scan_paths = lambda d, why="": APPLIED.append(why)

print("一、补全盘")
ST.update(scan_spec=["/quark/夸克挂载"])
r, out = quiet(m.xiaoya_default_whole, "D")
check("没加过 → 加上 /xiaoya、立刻应用", r and "/xiaoya" in ST["scan_spec"] and APPLIED)
check("小雅的定时扫描关掉", ST["strm_cron_by_mount"]["/xiaoya"] == m.NEVER_CRON)
check("提示用快速生成", "快速生成" in out)
ST.update(scan_spec=["/quark/夸克挂载"], strm_cron_by_mount={"/xiaoya": "0 0 */6 * * *"})
quiet(m.xiaoya_default_whole, "D")
check("自己设过小雅定时的不动", ST["strm_cron_by_mount"]["/xiaoya"] == "0 0 */6 * * *")
ST.update(scan_spec=["/xiaoya/电影"]); APPLIED.clear()
r, _ = quiet(m.xiaoya_default_whole, "D")
check("加过路径的不动", r is False and ST["scan_spec"] == ["/xiaoya/电影"] and not APPLIED)

print("二、自己删掉的不补回")
ANS = iter(["2", "0"])
m.ask = lambda *a, **k: next(ANS)
m.ask_yn = lambda *a, **k: True
quiet(m._xiaoya_paths_menu, "D")
check("删除所有路径 → 记下不要默认", ST["scan_spec"] == [] and ST.get("xiaoya_no_default") is True)
APPLIED.clear()
r, _ = quiet(m.xiaoya_default_whole, "D")
check("不再补回全盘", r is False and ST["scan_spec"] == [] and not APPLIED)
ANS = iter(["1", "0"])
m._pick_dirs = lambda d, mp: ["/xiaoya/电影"]
quiet(m._xiaoya_paths_menu, "D")
check("再挑路径 → 标记清掉", ST.get("xiaoya_no_default") is False and ST["scan_spec"] == ["/xiaoya/电影"])
src = open(SRC, encoding="utf-8").read()
rm = src[src.index("def xiaoya_remove"):]; rm = rm[:rm.index("\ndef ", 1)]
check("移除小雅也清掉标记（重装又是默认全盘）", "xiaoya_no_default=False" in rm)

print("三、进那一屏就补")
WH = []
m.xiaoya_default_whole = lambda d: WH.append(d)
m.xiaoya_mounted = lambda d: False
m.xiaoya_state = lambda: "在跑"
m.ask = lambda *a, **k: "0"
quiet(m._xiaoya_menu, "D")
check("没挂不补", WH == [])
xm = src[src.index("def _xiaoya_menu"):]; xm = xm[:xm.index("\ndef ", 1)]
check("挂着进来先补（在循环外，只补一次）", xm.index("xiaoya_default_whole(d)") < xm.index("while True"))

print("四、更新小雅")
XS = ["在跑"]
m.xiaoya_state = lambda: XS[0]
CMD = []
IMG = {"old": "sha:A", "new": "sha:A", "pull": 0, "run": 0}
class R:
    def __init__(self, rc=0, out=""): self.returncode, self.stdout, self.stderr = rc, out, ""
def sh(cmd, **k):
    CMD.append(cmd)
    if cmd.startswith("docker inspect"): return R(0, IMG["old"] + "\n")
    if cmd.startswith("docker image inspect"): return R(0, IMG["new"] + "\n")
    if cmd.startswith("docker pull"): return R(IMG["pull"])
    if cmd.startswith("docker run"): return R(IMG["run"])
    return R()
m.sh = sh
m.read_emby_api_key = lambda d=None: "K"
PQ = [""]
m.play_quiet = lambda key: PQ[0]
XS[0] = "未装"
r, out = quiet(m.xiaoya_update, "D")
check("没装：不做", r is False and CMD == [] and "扫码登录" in out)
XS[0] = "在跑"; PQ[0] = "有人在看片"; YN = []
m.ask_yn = lambda q, dflt=None, *a, **k: YN.append(dflt) or False
r, out = quiet(m.xiaoya_update, "D")
check("有人在播：先问、默认否、选否不动", r is False and YN == [False] and CMD == [] and "会断" in out)
PQ[0] = ""
r, out = quiet(m.xiaoya_update, "D")
check("镜像没变：只拉、不重建", r is False and "已是最新" in out
      and not any(c.startswith(("docker rm", "docker run")) for c in CMD))
CMD.clear(); IMG["new"] = "sha:B"
r, out = quiet(m.xiaoya_update, "D")
check("变了：删掉重建，用安装那同一句", r is True and "docker rm -f xiaoya" in CMD and m.xiaoya_run_cmd() in CMD)
check("重建参数：同一个网络、同一个令牌目录、不开端口", "--network mediastack" in m.xiaoya_run_cmd()
      and f"-v {m.XIAOYA_DIR}:/data" in m.xiaoya_run_cmd() and " -p " not in m.xiaoya_run_cmd())
inst = src[src.index("def xiaoya_install"):]; inst = inst[:inst.index("\ndef ", 1)]
check("安装也用这一句", "sh(xiaoya_run_cmd()" in inst)
CMD.clear(); IMG["run"] = 1
r, out = quiet(m.xiaoya_update, "D")
check("起不来：说清怎么补", r is False and "扫码登录" in out)
IMG["pull"] = 1; CMD.clear()
r, out = quiet(m.xiaoya_update, "D")
check("拉不下来：不动容器", r is False and not any(c.startswith("docker rm") for c in CMD))
check("版本 1.5.373 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 373))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

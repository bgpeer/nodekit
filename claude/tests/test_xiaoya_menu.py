#!/usr/bin/env python3
"""小雅那一屏照夸克的排法（1.5.370）。
仓库主人：「安装 / 启动、阿里令牌这两个可以设计在一起，改成 1.扫码登录 → 1 扫码登录 / 2 退出登录 /
0 返回，提示：请用阿里网盘扫码登录，换号直接扫另一个号……扫完码直接带着安装，退出登录也不用卸载安装，
在外面删除小雅可以删除全部安装；3 扫描路径改成 2 修改路径；看一下还有什么功能需要加进去」。
钉住：
  一、主屏：1 扫码登录、2 修改路径，接着是每个盘都有的那几项（生成媒体库……刷新元数据），最后移除小雅
  二、扫码登录那一屏：1 扫码登录 / 2 退出登录 / 3 手动填令牌（没删）/ 0，提示一行，回车 = 返回
  三、退出登录：要确认（默认否）；删令牌、停小雅，容器 / 挂载 / 扫描路径都留着
  四、令牌齐了只是停着 / 没装：点 1 直接起 / 装；扫完码停着的也会起回来
  五、没挂：要挂上才能做的几项拦住、说清先去 2；修改路径不拦
  六、夸克那一屏抽出公共函数后排法没变
"""
import contextlib, importlib.util, io, os, sys, tempfile
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

T = tempfile.mkdtemp(); m.XIAOYA_DIR = os.path.join(T, "xiaoya")
os.makedirs(m.XIAOYA_DIR)
def fill():
    for f, _n in m.XIAOYA_FILES:
        open(os.path.join(m.XIAOYA_DIR, f), "w").write("x\n")
    open(os.path.join(m.XIAOYA_DIR, "opentoken_url.txt"), "w").write("u\n")   # 1.5.374 起没它算「要重扫」

ST = ["在跑"]; MT = [True]; CALLS = []
m.xiaoya_state = lambda: ST[0]
m.xiaoya_mounted = lambda d: MT[0]
m.xiaoya_scan_label = lambda: "扫描全盘"
m.drive_channel = lambda d, mp, drv: ("原画直链", False)
m.drive_links = lambda d, mp, drv: []
m.strm_cron_desc = lambda mp: "跟全局"
m.title_policy_of = lambda mp: "scrape"
m.skip_dirs_of = lambda mp: []
m.rename_policy_of = lambda mp: "off"
m.ms_state = lambda: {}
m.cover_manual_last = lambda mp: None
m.cover_running = lambda mp: None
m.heal_mount_on = lambda mp: True
m.heal_auto_on = lambda: True
m.meta_auto_of = lambda mp: ""
m._scan_menu = lambda d, mp, label: CALLS.append(("scan", mp, label))
m.xiaoya_paths = lambda d: CALLS.append("paths")
m.xiaoya_remove = lambda d: CALLS.append("remove")
m.xiaoya_update = lambda d: CALLS.append("update")
m.xiaoya_default_whole = lambda d: CALLS.append("whole")
m.xiaoya_install = lambda d: CALLS.append("install") or True
m.sh = lambda cmd, **k: CALLS.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

def run_menu(answers):
    it = iter(answers)
    m.ask = lambda *a, **k: next(it, "0")
    return quiet(m._xiaoya_menu, "D")

print("一、主屏")
fill()
_r, out = run_menu(["0"])
import re
rows = [l.strip() for l in out.splitlines() if re.match(r"\s*\d+\. ", l)]
labels = [re.split(r"\s{2,}", r.split(". ", 1)[1])[0].strip() for r in rows]
check("1 扫码登录 · 已登录", rows[0].startswith("1. 扫码登录") and "已登录" in rows[0])
check("2 修改路径 · 扫描全盘", rows[1].startswith("2. 修改路径") and "扫描全盘" in rows[1])
want = ["扫码登录", "修改路径", "生成媒体库", "直链方式", "片名用哪个", "不扫的目录", "名称重定义",
        "截封面", "补时长", "刷新元数据", "更新小雅", "移除小雅", "返回"]
check("跟夸克一样的那几项都在、更新小雅（1.5.373）、移除小雅排最后", labels == want)
check("标题带挂载点", "小雅（WebDAV）" in out and "/xiaoya" in out)
check("没有「安装 / 启动」「阿里令牌」两行了", "安装 / 启动" not in out and "阿里令牌" not in out)
CALLS.clear(); run_menu(["3", "0"])
check("生成媒体库对着 /xiaoya、名字叫小雅", ("scan", "/xiaoya", "小雅 /xiaoya") in CALLS)
CALLS.clear(); run_menu(["2", "0"])
check("2 → 修改路径", CALLS == ["whole", "paths"])
CALLS.clear(); run_menu(["11", "", "0"])
check("11 → 更新小雅", CALLS == ["whole", "update"])
CALLS.clear(); run_menu(["12", "", "0"])
check("移除小雅还在（删全部安装）", CALLS == ["whole", "remove"])
check("挂着进来先补全盘（没加过路径才真补，见 test_xiaoya_paths）", CALLS[0] == "whole")

print("二、扫码登录那一屏")
SEEN = []
def ask(p="", *a, **k):
    SEEN.append(p); return ""
m.ask = ask
r, out = quiet(m.xiaoya_set_tokens, "D")
check("1 阿里 / 2 115 / 3 夸克 / 4 退出登录 / 5 手动填令牌 / 0 返回（1.5.371）",
      all(x in out for x in ("1. 阿里扫码（必需）", "2. 115 扫码（不限速）", "3. 夸克扫码", "4. 退出登录", "5. 手动填令牌", "0. 返回")))
check("提示一行：换号直接扫另一个号", "换号直接扫另一个号" in out)
check("回车 = 返回，不做事", r is False and SEEN[-1] == "请选择（回车 = 返回）")

print("三、退出登录")
YN = [False]
m.ask_yn = lambda *a, **k: YN.append(k.get("default", a[1] if len(a) > 1 else None)) or YN[0]
m.ask = lambda *a, **k: "4"
CALLS.clear()
r, out = quiet(m.xiaoya_set_tokens, "D")
check("要确认、默认否；选否不动", r is False and YN[-1] is False and m.xiaoya_tokens_ok() and CALLS == [])
check("屏上说清后果", "打不开" in out and "重新扫码登录才恢复" in out)
YN[0] = True
r, out = quiet(m.xiaoya_set_tokens, "D")
check("确认了：三样令牌删掉", r is True and not any(os.path.exists(os.path.join(m.XIAOYA_DIR, f)) for f, _ in m.XIAOYA_FILES))
check("小雅停下，不删容器、不删挂载、不删扫描路径",
      CALLS == ["docker stop xiaoya"] and "remove" not in CALLS)
r, out = quiet(m.xiaoya_set_tokens, "D")
check("本来就没登录：不再问", r is False and "没登录" in out)

print("四、停着 / 没装 → 起 / 装")
fill(); ST[0] = "停了"; CALLS.clear()
_r, out = run_menu(["0"])
check("停着：当前写清", "已登录 · 停了" in out)
run_menu(["1", "", "0"])
check("点 1：直接起（不用再扫）", [c for c in CALLS if c != "whole"] == ["install"])
ST[0] = "未装"; CALLS.clear()
_r, out = run_menu(["0"])
check("没装：当前写清", "已登录 · 没装" in out)
m.xiaoya_install = lambda d: CALLS.append("install") or False       # 装的那句确认选了否
SET = []
_set = m.xiaoya_set_tokens
m.xiaoya_set_tokens = lambda d=None: SET.append(d)
run_menu(["1", "", "0"])
check("不装（选了否）：再进登录那一屏", [c for c in CALLS if c != "whole"] == ["install"] and SET == ["D"])
m.xiaoya_set_tokens = _set
for f, _n in m.XIAOYA_FILES:
    os.remove(os.path.join(m.XIAOYA_DIR, f))
ST[0] = "停了"; CALLS.clear()
m._xiaoya_scan = lambda: {f: "y" for f, _n in m.XIAOYA_FILES}
m.ask = lambda *a, **k: "1"
quiet(m.xiaoya_set_tokens, "D")
check("退出登录停下的，重新扫上就起回来", CALLS == ["install"])
ST[0] = "在跑"; CALLS.clear()
quiet(m.xiaoya_set_tokens, "D")
check("在跑：换号只重启", CALLS == ["docker restart xiaoya"])

print("五、没挂")
MT[0] = False; ST[0] = "在跑"; CALLS.clear()
_r, out = run_menu(["3", "0"])
check("生成媒体库拦住、说清先去 2", "先选「2 修改路径」" in out and not any(isinstance(c, tuple) for c in CALLS))
check("没挂：截封面那三项不列，移除小雅照样在", "截封面" not in out and "移除小雅" in out)
CALLS.clear(); run_menu(["2", "0"])
check("修改路径不拦（自己会先挂）", CALLS == ["paths"])

print("六、夸克那一屏排法没变")
src = open(SRC, encoding="utf-8").read()
dm = src[src.index("def _drive_menu"):]; dm = dm[:dm.index("\ndef ", 1)]
check("夸克那一屏用同一套公共项", "_drive_std_items(d, mp, drv, add, mounted, ch, switchable)" in dm
      and "_drive_tail_items(d, mp, add)" in dm)
it = []
quiet(m._drive_std_items, "D", "/q", "QuarkTV", lambda *x: it.append(x), True, "原画直链", False)
check("六项顺序照旧", [x[0] for x in it] == ["修改扫描路径", "生成媒体库", "直链方式", "片名用哪个", "不扫的目录", "名称重定义"])
check("要不要先挂上照旧", [x[3] for x in it] == [True, True, True, False, True, False])
check("版本 1.5.370 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 370))
check("改名「小雅（WebDAV）」、旧名一个不剩（1.5.375）", "小雅（自建）" not in src and "小雅（WebDAV）" in src
      and tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 375))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

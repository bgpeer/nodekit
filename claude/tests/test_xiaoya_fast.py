#!/usr/bin/env python3
"""小雅「快速生成」：读小雅自带的 strm.txt 直接写 strm，不爬网盘（1.5.372）。
仓库主人：「把快速生成 strm 也做了吧」。
钉住：
  一、清单一行 → OpenList 路径：只认 # 后面 /d/ 那段、解码、挡 . / ..、不像样的行跳过
  二、落点和内容跟 AutoFilm 一模一样（strm 树镜像网盘树、内容是路径形式），能被 strm_target_path 读回
  三、只出「修改路径」选的那几类；跳过「不扫的目录」；已有的不动；同名不同后缀留第一个
  四、整套：没加路径 → 不做；问一句再写；问关掉小雅的定时扫描；收尾交给 strm_finish（只管 /xiaoya）
  五、菜单：只有小雅那一屏的「生成媒体库」多一项「3 快速生成」；do_strm 收尾抽成 strm_finish 没丢步骤
"""
import contextlib, importlib.util, io, os, sys, tempfile, urllib.parse
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

def L(path, name="整理过/名字.strm"):
    return f"{name}#http://xiaoya.host:5678/d/{urllib.parse.quote(path)}?sign=SIGN_STR\n"

print("一、一行 → 路径")
check("解码 /d/ 后面那段、挂到 /xiaoya 下", m.xiaoya_list_path(L("🏷️我的115分享/4KRemux/A B/x.mkv"))
      == "/xiaoya/🏷️我的115分享/4KRemux/A B/x.mkv")
check("半编码的也认（清单里真有 🏷%EF%B8%8F 这种）",
      m.xiaoya_list_path("a#http://xiaoya.host:5678/d/🏷%EF%B8%8F我的115分享/x.ts?sign=SIGN_STR")
      == "/xiaoya/🏷️我的115分享/x.ts")
check("前半名字里带 # 也不乱", m.xiaoya_list_path("a#b.strm" + L("电影/y.mp4")[len("整理过/名字.strm"):])
      == "/xiaoya/电影/y.mp4")
check("挡 ..", m.xiaoya_list_path(L("电影/../../etc/x.mkv")) == "")
check("没地址 / 不是 /d/ / 没后缀 → 跳过", m.xiaoya_list_path("就一行字\n") == ""
      and m.xiaoya_list_path("a#http://h/x/y.mkv") == "" and m.xiaoya_list_path(L("电影/目录")) == "")

print("二、落点和内容")
D = tempfile.mkdtemp()
m.strm_root = lambda d: os.path.join(D, "media", "strm")
base = os.path.join(D, "media", "strm", m.STRM_SUBDIR)
lines = [L("电影/流浪地球 (2019)/流浪地球.mkv"), L("电视剧/某剧/S01E01.mp4"), L("音乐/歌.flac"),
         L("电影/合集/原盘/z.mkv"), L("电影/流浪地球 (2019)/流浪地球.mp4"), "坏行\n"]
todo, have, out = m.xiaoya_fast_plan("D", lines, ["/xiaoya/电影", "/xiaoya/电视剧"], ["合集"])
paths = {os.path.relpath(sp, base): op for sp, op in todo}
check("落点 = strm 树里镜像网盘树、后缀换成 .strm",
      paths.get("xiaoya/电影/流浪地球 (2019)/流浪地球.strm") == "/xiaoya/电影/流浪地球 (2019)/流浪地球.mkv")
check("跟 AutoFilm 扫出来的同一个落点（strm_subpath）",
      os.path.join(base, m.strm_subpath("/xiaoya/电视剧/某剧"), "S01E01.strm") in [sp for sp, _ in todo])
check("只出选了的几类（音乐不在里面）", not any("音乐" in k for k in paths) and out >= 1)
check("跳过「不扫的目录」", not any("合集" in k for k in paths))
check("同名不同后缀留第一个", len(paths) == 2)
n, b = quiet(m.xiaoya_fast_write, todo)[0]
sp = os.path.join(base, "xiaoya/电影/流浪地球 (2019)/流浪地球.strm")
check("写出来是路径形式、strm_target_path 读得回", n == 2 and b == 0
      and m.strm_target_path(open(sp, encoding="utf-8").read()) == "/xiaoya/电影/流浪地球 (2019)/流浪地球.mkv")
open(sp, "w").write("/xiaoya/别动我")
todo2, have2, _o = m.xiaoya_fast_plan("D", lines, ["/xiaoya"], [])
check("已有的不动、算跳过", have2 == 2 and all(s != sp for s, _ in todo2) and open(sp).read() == "/xiaoya/别动我")
check("全盘：音乐、合集都出", any("音乐" in s for s, _ in todo2) and any("合集" in s for s, _ in todo2))

print("四、整套")
SC = [[]]
m.xiaoya_scan_paths = lambda: SC[0]
r, out = quiet(m.xiaoya_fast_strm, "D")
check("没加路径 → 不做、说清先去 2", r is False and "2 修改路径" in out)
SC[0] = ["/xiaoya"]
m.XIAOYA_DIR = os.path.join(D, "xiaoya"); os.makedirs(m.XIAOYA_DIR)
open(os.path.join(m.XIAOYA_DIR, "strm.txt"), "w", encoding="utf-8").write("".join(lines + [L("纪录片/新.mkv")]))
m.skip_dirs_of = lambda mp: []
ST = {}
m.ms_state = lambda: dict(ST)
m.save_ms_state = lambda **kw: ST.update(kw)
FIN, CRON, YN = [], [], []
m.strm_finish = lambda d, only, snap0: FIN.append((only, set(snap0)))
m._apply_autofilm_cron = lambda d: CRON.append(d) or True
ANS = [False]
m.ask_yn = lambda q, *a, **k: YN.append(q) or ANS[0]
r, out = quiet(m.xiaoya_fast_strm, "D")
check("先说要生成几个、问一句；选否不写", r is False and "要生成" in out and YN == ["生成？"]
      and not os.path.exists(os.path.join(base, "xiaoya/纪录片/新.strm")))
ANS[0] = True; YN.clear()
r, out = quiet(m.xiaoya_fast_strm, "D")
check("写了：新片出来", r is True and os.path.exists(os.path.join(base, "xiaoya/纪录片/新.strm")))
check("问关掉小雅的定时扫描 → 关、AutoFilm 换配置", any("定时扫描" in q for q in YN)
      and ST["strm_cron_by_mount"]["/xiaoya"] == m.NEVER_CRON and CRON == ["D"])
check("关了以后菜单写「关」", m.strm_cron_desc("/xiaoya") == "关")
check("收尾交给 strm_finish、只管 /xiaoya、带着开工前那份清单",
      FIN and FIN[-1][0] == "/xiaoya" and any(p.endswith("流浪地球.strm") for p in FIN[-1][1]))
YN.clear(); CRON.clear()
r, out = quiet(m.xiaoya_fast_strm, "D")
check("都生成过了：不再问", r is False and "都已经生成过了" in out and YN == [])
open(os.path.join(m.XIAOYA_DIR, "strm.txt"), "a", encoding="utf-8").write(L("纪录片/更新.mkv"))
r, out = quiet(m.xiaoya_fast_strm, "D")
check("定时已经关着：不再问", r is True and not any("定时扫描" in q for q in YN) and CRON == [])
os.remove(os.path.join(m.XIAOYA_DIR, "strm.txt"))
m.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("no docker"))
r, out = quiet(m.xiaoya_fast_strm, "D")
check("读不到清单 → 说清", r is False and "读不到小雅的清单" in out)

print("五、菜单 / 收尾")
src = open(SRC, encoding="utf-8").read()
sm = src[src.index("def _scan_menu"):]; sm = sm[:sm.index("\ndef ", 1)]
check("只有小雅那一屏多「3 快速生成」", 'if mount == XIAOYA_MOUNT:' in sm and "3. 快速生成" in sm
      and 'c == "3" and mount == XIAOYA_MOUNT' in sm)
ds = src[src.index("def do_strm"):]; ds = ds[:ds.index("\ndef ", 1)]
fs = src[src.index("def strm_finish"):]; fs = fs[:fs.index("\ndef ", 1)]
check("do_strm 收尾走 strm_finish", "strm_finish(d, only, snap0)" in ds)
steps = ["apply_skip_dirs(d, only=only)", "apply_drive_defaults(d)", "sync_hls_service(d)",
         "collapse_bluray_folders(d, only=only)", "prune_dead_strm(d, budget=PRUNE_BUDGET, only=only)",
         "migrate_strm_layout(d, key, wait=False)", "emby_notify_changes(key, diff, nudge=True)",
         "align_library(d, key, heal=False, migrate=False)", "auto_libraries_apply(", "report_not_in_emby(",
         "queue_new_covers(", "print_library_targets("]
check("收尾那几步一步没丢", all(x in fs for x in steps))
check("版本 1.5.372 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 372))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

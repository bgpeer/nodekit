#!/usr/bin/env python3
"""小雅那一栏删掉（1.5.385，仓库主人点了头：「把小雅的代码都删掉吧……删代码不要误伤了别的」）。
钉住：
  一、小雅的东西没了：菜单那一栏、扫码 / 快速生成 / 更新 / 移除这些函数、生成媒体库里的「3 快速生成」
  二、别的没被误伤：「4 WebDAV」那一栏（没挂时也在、能＋添加）、115 / 夸克 TV / 阿里那几栏、剩余网盘、调整顺序；
      每个盘那六项 + 截封面那三项的排法照旧；/xiaoya 要是还挂着就当普通盘列出来
  三、卸载照旧带走早先版本装的那份小雅
"""
import importlib.util, os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "media-stack.py")
spec = importlib.util.spec_from_file_location("ms", SRC)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
src = open(SRC, encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
def body(name):
    b = src[src.index(f"def {name}("):]; return b[:b.index("\ndef ", 1)]

print("一、小雅的东西没了")
left = [n for n in dir(m) if re.search(r"xiaoya|aliweb|quark_web_scan|pan115_cookie|ali2115", n, re.I)
        and n not in ("XIAOYA_NAME", "XIAOYA_DIR")]          # 这两个是卸载要用的，故意留着
check("小雅那一套函数一个不剩" + (f"（还剩：{left}）" if left else ""), left == [])
mm = body("mount_paths_menu")
check("挂载路径那一屏没有小雅那一栏", "小雅" not in mm and "xiaoya" not in mm.lower())
sm = body("_scan_menu")
check("生成媒体库没有「3 快速生成」", "快速生成" not in sm and "XIAOYA" not in sm)

print("二、别的没被误伤")
check("「4 WebDAV」没挂时也在（＋添加 WebDAV 的入口）", "lambda: _drive_menu(d, MOUNT_DAV, DRIVER_DAV, mounted=False)" in mm)
check("115 / 夸克 TV / 阿里没挂时那几栏还在", all(x in mm for x in ("MOUNT_115, DRIVER_115", "DRIVER_QTV", "DRIVER_ALI")))
check("剩余网盘、调整顺序还在", "剩余网盘（自动）" in mm and "调整顺序" in mm)
check("/xiaoya 还挂着的话当普通盘列出来（不再藏）", 'if mp and mp != "/"]' in mm)
check("「＋ 添加 WebDAV」那条流程还在", "def _add_webdav_flow(" in src)
for k in ("strm_cron_desc", "title_policy_of", "rename_policy_of"):
    setattr(m, k, lambda mp: "")
m.skip_dirs_of = lambda mp: []
m.ms_state = lambda: {}
m.drive_links = lambda *a: []
m.cover_manual_last = lambda mp: None
m.cover_running = lambda mp: None
m.heal_mount_on = lambda mp: True
m.heal_auto_on = lambda: True
m.meta_auto_of = lambda mp: ""
it = []
m._drive_std_items("D", "/quark", "QuarkTV", lambda *x: it.append(x), True, "原画直链", False)
m._drive_tail_items("D", "/quark", lambda *x: it.append(x))
check("每个盘那九项排法照旧", [x[0] for x in it] == ["修改扫描路径", "生成媒体库", "直链方式", "片名用哪个", "不扫的目录",
                                               "名称重定义", "截封面", "补时长", "刷新元数据"])
check("要不要先挂上照旧", [x[3] for x in it[:6]] == [True, True, True, False, True, False])

print("三、卸载")
un = body("do_uninstall")
check("卸载照旧带走早先装的那份小雅（容器 + 令牌目录）", 'ms_state().get("xiaoya_ours")' in un
      and "docker rm -f {XIAOYA_NAME}" in un and "rmtree(XIAOYA_DIR" in un)
check("版本 1.5.385 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 385))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

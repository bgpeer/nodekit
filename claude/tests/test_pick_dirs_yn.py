#!/usr/bin/env python3
"""挑目录那一屏：编号往下走、y 就选这一层、n 返回上一层（仓库主人 11/13：「输入编号继续往下选，输入y确认就在这一层全盘，
  输入n返回上一层选择，几个盘都是改成一样」）。

  原来屏上一长串：编号 进这一层 / 多个编号…… / . 就选这一层 / * 这一层全部 / .. 上一层 / 或者直接把路径贴进来 / 回车取消，
  手机上折成两行。1.5.419：屏上只说编号 / y / n；原来那几个键都还认（没删）。
钉住：
  一、屏上的提示只有一行：编号 / y / n（最上面一层是「就扫整个盘」「返回」）
  二、编号进下一层；y 选当前这一层；n 回上一层，最上面一层按 n = 不选了
  三、原来的键都还管用：. / .. / * / 贴路径 / 多个编号 / 回车取消
  四、所有盘和「不扫的目录」共用这一个函数"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
TREE = {"/q": ["备份", "挂载"], "/q/挂载": ["动漫", "电影"], "/q/挂载/动漫": [], "/q/挂载/电影": [], "/q/备份": []}
m._ol_token = lambda d: "tok"
m._ol_subdirs = lambda cur, tok: (list(TREE.get(cur, [])), "")
ANS = []
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else ""
def run(*answers):
    ANS[:] = list(answers)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = m._pick_dirs("/d", "/q")
    return r, plain(buf.getvalue())

print("一、屏上的提示")
r, out = run("")
hint = [l for l in out.splitlines() if "编号 进下一层" in l]
check("最上面一层：一行，编号 / y 就扫整个盘 / n 返回 / 回车 放弃", len(hint) == 1 and "y 就扫整个盘" in hint[0] and hint[0].rstrip().endswith("n 返回　回车 放弃"))
check("不再把 . * .. 贴路径 这一串摆在屏上", all(t not in out for t in ("* 这一层全部", ".. 上一层", "或者直接把路径贴进来", ". 就选")))
r, out = run("2", "")
hint = [l for l in out.splitlines() if "编号 进下一层" in l]
check("下面的层：y 就扫这一层 / n 返回上一层 / 回车 放弃（1.5.420）", "y 就扫这一层" in hint[-1] and "n 返回上一层　回车 放弃" in hint[-1])

print("二、编号 / y / n")
r, out = run("2", "y")
check("编号进下一层，y = 就选这一层", r == ["/q/挂载"])
r, out = run("y")
check("最上面一层按 y = 整个盘", r == ["/q"])
r, out = run("2", "1", "n", "2", "y")
check("n = 回上一层，接着还能选别的", r == ["/q/挂载/电影"])
r, out = run("2", "n", "n")
check("回到最上面一层再按 n = 不选了，回去", r == [])
r, out = run("2", "Y")
check("大写 Y / N 也认", r == ["/q/挂载"] and run("N")[0] == [])

print("三、原来的键都还管用（没删，只是不占屏）")
check(". = 选这一层", run("2", ".")[0] == ["/q/挂载"])
check(".. = 上一层", run("2", "..", "1", "y")[0] == ["/q/备份"])
check("最上面一层按 .. 不会退出去", run("..", "y")[0] == ["/q"])
check("* = 这一层的子目录全选", run("2", "*")[0] == ["/q/挂载/动漫", "/q/挂载/电影"])
check("多个编号（逗号隔开）= 一次选好几个", run("1,2")[0] == ["/q/备份", "/q/挂载"])
check("/ 开头 = 直接贴路径", run("/q/挂载/电影")[0] == ["/q/挂载/电影"])
r, out = run("/别的盘/x", "")
check("贴的路径不在这个盘底下：拦住", r == [] and "不在" in out)
r, out = run("9", "")
check("编号越界：说没认出来，不乱选", r == [] and "没认出有效的编号" in out)
check("回车 = 取消", run("")[0] == [])

print("四、所有盘共用")
check("挑目录只有这一个函数，各个盘、「不扫的目录」都调它", SRC.count("def _pick_dirs(") == 1 and SRC.count("_pick_dirs(d, ") >= 2)
check("media-stack 1.5.419 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 420))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

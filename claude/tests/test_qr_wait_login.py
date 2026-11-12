#!/usr/bin/env python3
"""夸克扫码后不用回来按回车，自己等着登上（仓库主人 11/13：「以前……扫码一成功根本就不需要确认他自己就登上去了，以前是有
  几分钟的等待应该是5分钟吧，只要扫码成功VPS这边立刻就有反应了自己就在上去了」）。

  夸克这三处（扫码挂上 / 重新扫码 / 本机自己的一份登录）原来是「扫完并确认后按回车」，回车一次才查一次。
  1.5.417：qr_wait_login 每 4 秒自己查一次，最多等 5 分钟；登上了立刻往下走。
钉住：
  一、登上了立刻返回，不问任何话
  二、还没登上：原因变了才印一行，同一个原因不刷屏
  三、「设备数超限」：指路去夸克 App 解除绑定；人处理完自己接着登上
  四、等满 5 分钟才问一句（回车再等 / q 放弃）
  五、三处都换成自动等；放弃时该撤的撤、该恢复的恢复（跟原来按 q 一样）"""
import importlib.util, os, sys, io, re, types, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
CLOCK, SLEPT, ASKED, ANS = [0.0], [], [], []
m.time = types.SimpleNamespace(monotonic=lambda: CLOCK[0], sleep=lambda s: (SLEPT.append(s), CLOCK.__setitem__(0, CLOCK[0] + s))[0],
                               time=m.time.time, strftime=m.time.strftime, gmtime=m.time.gmtime, localtime=m.time.localtime)
m.ask = lambda prompt, default="": (ASKED.append(prompt), ANS.pop(0) if ANS else "")[1]
def run(answers_from_check, user=()):
    seq = list(answers_from_check); CLOCK[0] = 0.0; SLEPT.clear(); ASKED.clear(); ANS[:] = list(user)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        r = m.qr_wait_login(lambda: seq.pop(0) if len(seq) > 1 else seq[0])
    return r, plain(buf.getvalue())

print("一、登上了立刻走")
r, out = run([""])
check("第一次查就登上了：立刻返回，不问话、不等", r is True and not ASKED and not SLEPT)
check("开头一行说清：手机确认后自己会登上、不用按回车", "这边自己会登上" in out and "不用按回车" in out)

print("二、还没登上")
r, out = run(["need verify", "need verify", "need verify", ""])
check("查了几次才登上：中间自己等，不用人按回车", r is True and not ASKED and SLEPT == [m.QR_POLL_S] * 3)
check("同一个原因只印一行，不刷屏", out.count("还没登上：need verify") == 1)
r, out = run(["need verify", "<body><img src=x/></body> oops", ""])
check("原因变了再印一行；带网页标签的清掉", out.count("还没登上：") == 2 and "<img" not in out and "oops" in out)

print("三、设备数超限")
r, out = run(["failed init storage: 设备数超限", "failed init storage: 设备数超限", ""])
check("指路：夸克 App 登录设备里解除绑定；只说一次", out.count("解除绑定") == 1 and "夸克 App" in out)
check("人在手机上处理完，这边自己接着登上", r is True and not ASKED)

print("四、等满 5 分钟才问")
check("等 5 分钟、每 4 秒查一次", m.QR_WAIT_S == 300 and m.QR_POLL_S == 4)
r, out = run(["need verify"], user=["n"])
check("一直没登上：5 分钟后问一句，n = 放弃（1.5.418：不是 q）", r is False and len(ASKED) == 1 and "回车再等，n 放弃" in ASKED[0]
      and "q" not in ASKED[0] and abs(sum(SLEPT) - 300) < 5)
seq2 = ["need verify"] * 80 + [""]
CLOCK[0] = 0.0; SLEPT.clear(); ASKED.clear(); ANS[:] = [""]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = m.qr_wait_login(lambda: seq2.pop(0) if len(seq2) > 1 else seq2[0])
check("回车 = 再等一轮，之后登上了照样往下走", r is True and len(ASKED) == 1)

print("四之二、115、阿里也是等满了问要不要再等（1.5.418，仓库主人：「都统一成夸克这种……输n放弃」）")
ANS[:] = ["n"]; ASKED.clear()
check("qr_wait_more：n = 放弃", m.qr_wait_more(5) is False and "等了 5 分钟还没登上；回车再等，n 放弃" in ASKED[0])
ANS[:] = [""]
check("回车 = 再等", m.qr_wait_more(5) is True)
ANS[:] = ["N"]
check("大写 N 也算放弃", m.qr_wait_more(5) is False)
f115 = SRC[SRC.index("def _qr115_new"):]; f115 = f115[:f115.index("\ndef ", 10)]
check("115：等满了不再直接结束，问一句；再等就续 5 分钟", "if not qr_wait_more(5):\n                break\n            deadline = time.time() + 300" in f115
      and "while time.time() < deadline:" not in f115)
check("115：码过期 / 手机上取消了，照旧直接结束（不问）", "if state in (2, -1, -2):\n            break" in f115)
i = SRC.index("if not qr_wait_more(max(1, wait_s // 60)):")
fali = SRC[i - 600:i + 3000]
check("阿里：等满了问一句；再等就续一轮", "deadline = time.time() + wait_s" in fali)
check("阿里：码过期照旧直接说「二维码过期了」", 'if st == "QRCodeExpired":' in fali)
check("三种盘问法一样，都是「回车再等，n 放弃」", SRC.count("qr_wait_more(") >= 4 and "回车再等，n 放弃" in SRC and "回车再等，q 放弃" not in SRC)

print("五、三处都换了")
check("不再有「扫完并确认后按回车」（夸克三处）", SRC.count('ask("扫完并确认后按回车（q 放弃）")') == 0)
check("扫码挂上：登上 → 接着建扫描路径；放弃 → 撤掉刚建的存储", re.search(
      r'if qr_wait_login\(lambda: _ol_reload_storage\(d, mount, _ol_token\(d\) or tok\)\):\n\s+ok\(f"夸克 TV 已挂上.*?scan_whole_mount\(d, mount\).*?return True\n\s+_ol_drop_storage\(d, mount, tok\)',
      SRC, re.S) is not None)
check("重新扫码：放弃 → 恢复原来的登录", re.search(
      r'if qr_wait_login\(lambda: _ol_reload_storage\(d, mp, _ol_token\(d\) or otok\)\):\n\s+ok\(f"夸克 TV 已重新登录.*?return True\n\s+_put\(old\)', SRC, re.S) is not None)
check("本机自己的一份登录也自动等", "if not qr_wait_login(_try):\n        return False" in SRC)
check("media-stack 1.5.417 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 418))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

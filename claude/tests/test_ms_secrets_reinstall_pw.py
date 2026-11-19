#!/usr/bin/env python3
"""media-stack：重跑「1 安装」不能把 Emby 密码备份冲掉（11/19 全仓检查）。

  main() 里 .secrets 是整份重写的，只留 4 行账号密码，事后只补回了 API Key；EMBY_PW_* 行
  全被冲掉。重跑安装又不问 Emby 账号密码（已经有 Key 就沿用），密码本身还是原来那个，丢的只是
  备份——菜单 2 和「7 设置 → 12」里就看不到了。修法：重写前读回 EMBY_PW_* 行、重写时带上。
  测试里只用假密码，不出现任何真实用户信息。
"""
import os, sys, stat, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

TMP = tempfile.mkdtemp()
SEC = TMP + "/.secrets"

check("没有文件：返回空", m.emby_pw_kept_lines(SEC) == [])
open(SEC, "w").write("# comment\nOPENLIST_USER=admin\nOPENLIST_PASS=OlPass123\n"
                     "BA_USER=web\nBA_PASS=BaPass123\nEMBY_API_KEY=key123\n"
                     "EMBY_PW_aaa111=Pw11111111\nEMBY_PW_bbb222=Pw22222222\n")
check("只读回 EMBY_PW_* 行，别的行不动", m.emby_pw_kept_lines(SEC) == ["EMBY_PW_aaa111=Pw11111111", "EMBY_PW_bbb222=Pw22222222"])

# 按 main() 里重写的那几行原样走一遍：读回来 → 整份重写（带上）→ 再看
kept = m.emby_pw_kept_lines(SEC)
m.write_atomic(SEC,
               "# 由 media-stack.py 生成，供重跑时沿用已有密码。别手改。\n"
               "OPENLIST_USER=admin2\nOPENLIST_PASS=OlPass456\n"
               "BA_USER=web2\nBA_PASS=BaPass456\n"
               + "".join(ln + "\n" for ln in kept), 0o600)
got = open(SEC).read()
check("重跑安装：4 行新密码写进去了", "OPENLIST_USER=admin2\n" in got and "BA_PASS=BaPass456\n" in got)
check("重跑安装：两份 Emby 密码备份原样留着", "EMBY_PW_aaa111=Pw11111111\n" in got and "EMBY_PW_bbb222=Pw22222222\n" in got)
check("重跑安装：备份只留一份、没重复", got.count("EMBY_PW_aaa111=") == 1 and got.count("EMBY_PW_bbb222=") == 1)
check("重写完还是 0600", stat.S_IMODE(os.stat(SEC).st_mode) == 0o600)

SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
check("main() 里重写前确实读了备份行", "_pw_kept = emby_pw_kept_lines(secret_file)" in SRC)
check("新装（没文件）时行为不变：内容里没有 EMBY_PW_ 行",
      m.emby_pw_kept_lines(TMP + "/nope") == [])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

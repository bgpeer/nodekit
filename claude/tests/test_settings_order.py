#!/usr/bin/env python3
"""「7 设置」里三个账号挨着排（仓库主人 11/12：「把三个帐号移的挨着」）。

  原来浏览器弹框账号在第 2、Emby 账号在第 11、OpenList 账号在第 13，中间隔着域名证书。
  1.5.402：三个账号排到最后 11 / 12 / 13 连着，别的项顺次往前补。
钉住：
  一、屏上 13 项的顺序和编号；一项没少（CLAUDE.md 第四节）；编号从 1 连到 13、对齐
  二、每个编号进的是对的功能（挪了编号最怕的就是屏上写的和实际进的对不上）
  三、屏上 / 文档里指路的「7 设置 → N」跟着改了"""
import importlib.util, os, sys, io, re, contextlib
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)
SRC = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()

WANT = ["Emby API Key", "MetaTube 刮削插件", "自动建库规则", "MetaTube 生效的库", "目录缓存时长", "剧集季集编号", "几%算播放完",
        "自动截封面", "自动补时长", "域名证书", "浏览器弹框账号", "Emby 账号", "OpenList 账号"]
HIT = []
m.ms_install_dir = lambda: "/nonexistent-ms"
m.is_installed = lambda d=None: True
m.read_emby_api_key = lambda d=None: "k"
m.read_env = lambda p, k, fallback="": {"BA_PASS": "x", "DOMAIN": "media.zone.example"}.get(k, "")
m.metatube_on = lambda d=None: True
m.rules_source = lambda: "author"
m.metatube_libraries = lambda key: [("库甲", "1", True, {})]
m.dir_cache_storages = lambda d: [(1, "/a", "x", 30)]
m.ms_state = lambda: {}
m.ep_fix_setting = lambda: True
m.resume_max_pct = lambda: 99
m.cover_auto_on = lambda: False
m.cover_new_on = lambda: True
m.heal_auto_on = lambda: True
m.heal_day_mb = lambda: 2048
m.emby_users = lambda key: [("甲", True)]
m.ol_user = lambda d: "admin"
m.read_yaml_scalar = lambda p, k: "k"
for name, tag in (("set_emby_api_key", "Emby API Key"), ("toggle_metatube", "MetaTube 刮削插件"), ("auto_libraries", "自动建库规则"),
                  ("choose_metatube_libraries", "MetaTube 生效的库"), ("set_dir_cache", "目录缓存时长"), ("set_episode_fix", "剧集季集编号"),
                  ("set_resume_max", "几%算播放完"), ("set_cover_auto", "自动截封面"), ("heal_auto_menu", "自动补时长"),
                  ("emby_domain_menu", "域名证书"), ("set_web_credentials", "浏览器弹框账号"), ("emby_accounts_menu", "Emby 账号"),
                  ("ol_account_menu", "OpenList 账号")):
    assert hasattr(m, name), name
    setattr(m, name, (lambda t: (lambda *a, **k: HIT.append(t)))(tag))
ANS = []
m.ask = lambda prompt, default="": ANS.pop(0) if ANS else "0"
def run(*answers):
    ANS[:] = list(answers); HIT.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.params_menu()
    return plain(buf.getvalue())

print("一、屏上的顺序和编号")
out = run("0")
rows = re.findall(r"(?m)^  (\d+)\. (.+?)\s+当前：", out)
check("13 项，编号 1 到 13 连着", [n for n, _ in rows] == [str(i) for i in range(1, 14)])
check("顺序：" + " / ".join(f"{n} {t}" for n, t in rows), [t for _, t in rows] == WANT)
check("三个账号挨着：11 浏览器弹框账号 / 12 Emby 账号 / 13 OpenList 账号", rows[-3:] == [("11", "浏览器弹框账号"), ("12", "Emby 账号"), ("13", "OpenList 账号")])
check("原来的 13 项一项没少、没多", sorted(t for _, t in rows) == sorted(WANT) and "  0. 返回" in out)
def col(line):                                  # 「当前：」前面占几格（中文占两格）
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in line[:line.index("当前：")])
cols = {col(l) for l in out.splitlines() if re.match(r"  \d+\. ", l) and "当前：" in l}
check(f"「当前：」上下对齐（{sorted(cols)}）", len(cols) == 1)

print("二、每个编号进的是对的功能")
for i, tag in enumerate(WANT, 1):
    run(str(i), "", "0")
    check(f"选 {i} → {tag}", HIT == [tag])
out = run("14", "0")
check("没有的编号：无效选择", "无效选择" in out and not HIT)

print("三、指路的编号跟着改了")
def refs(text, n):
    return re.findall(r"7 设置 → %d(?!\d)[^，。；\n]{0,12}" % n, text)
check("自动截封面的指路是「7 设置 → 8」", "见 7 设置 → 8）" in SRC and "「7 设置 → 8」打开" in SRC and not [r for r in refs(SRC, 9) if "截" in r])
check("自动补时长的指路是「7 设置 → 9」，没有还指着 10 的", len(refs(SRC, 9)) >= 5 and not [r for r in refs(SRC, 10) if "域名" not in r])
check("目录缓存 → 5、季集编号 → 6", "7 设置 → 5。" in SRC and "（调长：7 设置 → 5，" in SRC and "到「7 设置 → 6」开一下" in SRC)
check("域名证书 → 10、Emby 账号 → 12、OpenList 账号 → 13", '"""「7 设置 → 10 域名证书」' in SRC and '"""「7 设置 → 12」：列出 Emby 账号' in SRC
      and '"""「7 设置 → 13」：OpenList 管理员' in SRC)
xy = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("节点面板里指路 Emby 域名证书的那句也改了", "7 设置 → 10 域名证书" in xy and "7 设置 → 12 域名证书" not in xy)
doc = open(os.path.join(ROOT, "docs", "16-emby.md"), encoding="utf-8").read()
check("文档：设置那一节按新编号列", "`10 域名证书`（见下）/ `11 浏览器弹框账号` / `12 Emby 账号` / `13 OpenList 账号`" in doc
      and "`7 设置 → 10 域名证书`" in doc and "| `7 设置 → 11` |" in doc and "| `7 设置 → 12` |" in doc and "| `7 设置 → 13` |" in doc)
c15 = open(os.path.join(ROOT, "docs", "15-cert.md"), encoding="utf-8").read()
check("文档 15-cert 里指路也改了", "7 设置 → 10 域名证书" in c15 and "7 设置 → 12 域名证书" not in c15)

print("四、版本号")
check("media-stack 1.5.402 起", tuple(map(int, m.SCRIPT_VERSION.split("."))) >= (1, 5, 402))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

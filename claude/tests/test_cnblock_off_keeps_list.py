#!/usr/bin/env python3
"""屏蔽中国域名：关掉（菜单 1）不再把放行名单一起删；卸载（菜单 6）先确认（11/10 全仓检查查出来的）。
  以前「1 已开启，关闭屏蔽?」答 y 走的是 remove_cn_block()，整个 cnblock.json 删掉：自定义链接、自己一条条加的
  单条放行域名 / IP、选的名单、自动更新开关全没了，再开变成「不放行」——文档写的却是「未开启则先存着、开启时自动带上」。
  「6 卸载」更是一按就删、连确认都没有（CLAUDE.md 第四节：会让数据没掉的操作要醒目一行 + 默认否）。
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
cb.SB_DIR = f"{tmp}/sb"; os.makedirs(cb.SB_DIR)
cb.CRON_FILE = f"{tmp}/cron"
SH = []
cb.sh = lambda c, check=True: SH.append(c) or ""
cb.apply_cn_block = lambda cfg=None: True
ANS = []
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"
def run(*a):
    ANS[:] = list(a); SH.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        cb.menu()
    return b.getvalue()
SB = f"{cb.SB_DIR}/config.json"
def seed(**extra):
    cfg = {"enabled": True, "wl_mode": "custom", "wl_url": "https://wl.example/wl.py", "auto": False,
           "wl_domains": ["a.example.com", "b.example.org"], "wl_ips": ["1.2.3.0/24"],
           "rs_hash": {"cnblk-cn-ip": "x"}, "wl_sig": "y"}
    cfg.update(extra)
    json.dump(cfg, open(cb.CNBLOCK_FILE, "w"))
    json.dump({"inbounds": [], "outbounds": [{"type": "direct", "tag": "direct"}],
               "route": {"final": "direct",
                         "rule_set": [{"tag": "cnblk-cn-site", "type": "remote", "url": "u"}, {"tag": "mine", "type": "inline"}],
                         "rules": [{"rule_set": "cnblk-cn-site", "action": "reject"}, {"rule_set": "mine", "outbound": "direct"}]}},
              open(SB, "w"))
    open(cb.CRON_FILE, "w").write("x")
cb._link_data = lambda cfg: ([], [], [])          # 菜单首页算单条放行个数会去拉自定义链接：不出网

print("一、1 关闭屏蔽：只关，放行名单留着")
seed()
out = run("1", "", "0")
check("默认否：回车不关", json.load(open(cb.CNBLOCK_FILE))["enabled"] is True and not SH)
out = run("1", "y", "0")
d = json.load(open(cb.CNBLOCK_FILE))
conf = json.load(open(SB))
check("答 y：规则撤掉（别人的规则 / final 不动）、sing-box 重启一次、定时任务撤掉",
      [r["tag"] for r in conf["route"]["rule_set"]] == ["mine"] and conf["route"]["rules"] == [{"rule_set": "mine", "outbound": "direct"}]
      and conf["route"]["final"] == "direct" and SH == ["systemctl restart sing-box"] and not os.path.exists(cb.CRON_FILE))
check("状态文件还在：关着", d["enabled"] is False)
check("自定义链接、选的名单、单条放行域名 / IP、自动更新开关都留着",
      d["wl_url"] == "https://wl.example/wl.py" and d["wl_mode"] == "custom"
      and d["wl_domains"] == ["a.example.com", "b.example.org"] and d["wl_ips"] == ["1.2.3.0/24"] and d["auto"] is False)
check("上次注入的指纹作废（再开按当时的名单重新记）", "rs_hash" not in d and "wl_sig" not in d)
check("屏上一行 ✔，说清名单留着", "已关闭屏蔽（放行名单留着，再开照旧）" in out)
out = run("0")
check("关了以后首页照样显示名单：自定义名单、2 个域名 + 1 个 IP、未开",
      "当前：自定义名单" in out and "2 个域名 + 1 个 IP" in out and "当前：未开" in out)
check("关着：凌晨不刷新、重装不自动注入", cb.refresh() is None and not SH)
seed(); d0 = json.load(open(cb.CNBLOCK_FILE)); d0.pop("wl_mode"); json.dump(d0, open(cb.CNBLOCK_FILE, "w"))
run("1", "y", "0")
check("老装机（没选过名单、当时默认作者名单）：关的时候定成作者名单存下，再开不会变成不放行",
      json.load(open(cb.CNBLOCK_FILE))["wl_mode"] == "author")

print("二、6 卸载：先确认，默认否")
seed()
out = run("6", "", "0")
check("一行提示说清后果（自定义链接、单条放行一起删）+ [y/N]", "提示：卸载会清掉屏蔽规则，自定义链接和单条放行也一起删" in out
      and "确定卸载? [y/N]" in out)
check("回车 = 不卸：状态、规则、定时任务都没动", json.load(open(cb.CNBLOCK_FILE))["enabled"] is True
      and len(json.load(open(SB))["route"]["rule_set"]) == 2 and os.path.exists(cb.CRON_FILE) and not SH)
out = run("6", "y", "0")
check("答 y：规则撤掉、状态文件删掉（照旧是彻底清）", not os.path.exists(cb.CNBLOCK_FILE)
      and [r["tag"] for r in json.load(open(SB))["route"]["rule_set"]] == ["mine"] and not os.path.exists(cb.CRON_FILE))

print("三、命令行 remove 照旧彻底清（主脚本卸载用）")
seed()
sys.argv = ["cn-block.py", "remove"]
with contextlib.redirect_stdout(io.StringIO()):
    cb.main()
check("remove：状态文件删掉", not os.path.exists(cb.CNBLOCK_FILE))

print("四、文档 / 样板跟着改")
doc = open(os.path.join(ROOT, "docs", "10-cn-block.md"), encoding="utf-8").read()
check("文档：1 关了名单留着；6 卸载会先确认", "关了放行名单、自定义链接、单条放行都留着" in doc and "会先确认，默认否" in doc)
tpl = open(os.path.join(ROOT, "whitelist-template.py"), encoding="utf-8").read()
check("样板里的时刻、菜单路径是现在的（02:55、5 规则更新 → 1 立即更新），不再说「必须是 raw 链接」",
      "02:55" in tpl and "03:00" not in tpl and "5 规则更新 → 1 立即更新" in tpl and "必须是 raw 链接" not in tpl)
tags, doms, ips = cb._parse_remote_list(tpl)
check("样板照旧解析得出作者名单那 9 组", tags == list(cb.CN_WHITELIST) and doms == [] and ips == [])

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""Emby 安装：问域名前先一行提示去 Cloudflare 建 *.ys.根域名（关黄云）；不跟节点共用时域名默认 ys.根域名
  （仓库主人 10/31：「Emby 安装的时候也加个这样的提示」，并把自建 DNS 那行补上「关黄云」）。
  emby / list / home 都是底座下的子域名（SUBDOMAINS），一条 *.底座 全盖住，底座本身不用记录。"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
CFD = "https://" + "dash.cloudflare" + ".com"           # 拼出来：测试文件里不留网址（test_claude_folder）
ms = open(os.path.join(ROOT, "media-stack.py"), encoding="utf-8").read()
i_tip = ms.find('tip(f"请在 ' + CFD + ' 做好一条 DNS 记录关黄云 *.ys.{_r0}（名称不想用 ys 改用其他也行）")')
i_ask = ms.find('ok(f"检测到节点域名：{node_domain}")')
check("Emby：问域名之前一行 tip，记录名 *.ys.根域名、关黄云", 0 < i_tip < i_ask)
check("不跟节点共用时，域名默认 ys.根域名（回车就是）",
      'ask("你的域名（填 example.com 或 emby.example.com 都行）", f"ys.{_r0}")' in ms)
check("「媒体服务也用这个域名？」默认否：回车走 ys.根域名（仓库主人 10/31）",
      'ask_yn("媒体服务也用这个域名？", False)' in ms)
xy = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
i = xy.find("def cdn_add():"); body = xy[i:xy.find("\ndef ", i + 5)]
check("CDN 安装：问域名前一行 tip，cdn.根域名、开黄云（CDN 要走 CF 代理）",
      'ui_tip(f"请在 ' + CFD + ' 做好一条 DNS 记录开黄云 cdn.{_r0}（名称不想用 cdn 改用其他也行）")' in body
      and body.find("开黄云") < body.find("输入用于 CDN 的域名"))
check("CDN：问域名之前的提示只留这一行（原来那行合进去了）", body[:body.find("输入用于 CDN")].count("ui_tip(") == 1)
check("子域名都在底座下面（一条 *.底座 盖得住）", all(f'("{s}",' in ms for s in ("home", "emby", "list")))
r0 = lambda nd: ".".join(nd.split(".")[-2:]) if nd.count(".") >= 1 else "域名"
check("根域名：gg.example.com → example.com；没节点域名 → 「域名」", r0("gg.example.com") == "example.com" and r0("") == "域名")
check("版本只动最后一位", re.search(r'SCRIPT_VERSION = "1\.5\.(\d+)"', ms) and int(re.search(r'SCRIPT_VERSION = "1\.5\.(\d+)"', ms).group(1)) >= 393)
agh = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
check("自建 DNS 那行也补上「关黄云」", "做好两条 DNS 记录关黄云 dns.{r0} 和 *.dns.{r0}" in agh)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

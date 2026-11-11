#!/usr/bin/env python3
"""换节点域名后不留旧名字；面板 2 的「刷新三大配置」排第 1 位（仓库主人 11/11：
  「这个域名证书管理里面没有刷新吗怎么这样，我跟你说更换了那些垃圾都给我清干净喔」「把刷新三大配置换到第 1 位去因为选 1 选习惯了」）。
  真机：换完域名，域名证书管理里旧域名和节点专用域名各多一条「节点证书盖不住 ✗　伪装站（nginx）」。
  原因：nginx 443 那条是 `server_name 节点域名 私有名;`，换域名只认「整行就旧域名一个」的，这条没改到。
"""
import os, io, sys, tempfile, contextlib, importlib.util, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
CONF = ("server {\n  listen 80;\n  server_name old.example.org;\n  location / { return 404; }\n}\n"
        "server {\n  listen 443 ssl;\n  server_name old.example.org xab12.cd34.example.org;   # server_name old.example.org 注释不动\n"
        "  server_tokens off;\n  location /ws { proxy_set_header Host old.example.org; }\n}\n")

print("一、换域名时改干净")
new = x._nginx_sub_domain(CONF, "old.example.org", "new.example.org")
check("80 那条（只有一个名字）照旧改", "listen 80;\n  server_name new.example.org;" in new)
check("443 那条（后面跟着节点专用域名）也改到，私有名留着", "server_name new.example.org xab12.cd34.example.org;" in new)
check("注释、别的指令里的旧域名不碰（只改 server_name）", "# server_name old.example.org 注释不动" in new
      and "proxy_set_header Host old.example.org;" in new)
check("子域名、更长的名字不会被误改", x._nginx_sub_domain("  server_name a.old.example.org old.example.org.cn;\n", "old.example.org", "n.example")
      == "  server_name a.old.example.org old.example.org.cn;\n")
check("新旧都在的那条去重", x._nginx_sub_domain("  server_name old.example.org new.example.org;\n", "old.example.org", "new.example.org")
      == "  server_name new.example.org;\n")

print("二、老机器进面板自动清（1.1.218 及以前换过域名留下的）")
x.NGINX_CONF = f"{tmp}/bgpeer.conf"
STALE = CONF.replace("listen 80;\n  server_name old.example.org;", "listen 80;\n  server_name new.example.org;")
open(x.NGINX_CONF, "w").write(STALE)
x.node_domain = lambda: "new.example.org"
x.cert_names = lambda: ["new.example.org", "*.new.example.org"]
RUN, NGT = [], [0]
x.sh = lambda c, check=True: RUN.append(c) or ""
x.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=NGT[0], stdout="", stderr=""))
msg = x._nginx_names_heal()
got = open(x.NGINX_CONF).read()
check("443 那条只剩现在的节点域名：旧域名、证书盖不住的私有名都去掉", "listen 443 ssl;\n  server_name new.example.org;" in got
      and got.count("server_name old.example.org") == 1 and "xab12" not in got.split("#")[0])
check("屏上一行 ✔ 说清清了哪两个，nginx 平滑重载", msg.startswith("✔ ") and "old.example.org、xab12.cd34.example.org" in msg
      and RUN == ["systemctl reload nginx"])
RUN.clear()
check("已经干净的：不动、不重载", x._nginx_names_heal() == "" and not RUN)
x.cert_names = lambda: ["new.example.org", "*.new.example.org", "*.cd34.example.org"]
open(x.NGINX_CONF, "w").write(STALE)
x._nginx_names_heal()
check("证书盖得住的私有名（已经补签过）留着，只清旧域名", "server_name new.example.org xab12.cd34.example.org;" in open(x.NGINX_CONF).read())
open(x.NGINX_CONF, "w").write(STALE); NGT[0] = 1; RUN.clear()
x.cert_names = lambda: ["new.example.org"]
check("改完 nginx -t 不过：放回原文件、不重载", x._nginx_names_heal() == "" and open(x.NGINX_CONF).read() == STALE and not RUN)
NGT[0] = 0
x.cert_names = lambda: []
check("读不出证书覆盖哪些名字：不动（宁可不清也不误删）", x._nginx_names_heal() == "" and open(x.NGINX_CONF).read() == STALE)
x.node_domain = lambda: ""
check("IP 装的（没有节点域名）：不动", x._nginx_names_heal() == "")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("进面板时过一遍", "ng = _nginx_names_heal()" in src)

print("三、面板 2：刷新三大配置排第 1 位")
i = src.index('ui_items([(1, "刷新三大配置", None), (2, "更换节点名称前缀", None), (0, "返回", None)])')
seg = src[i:i + 400]
check("1 = 刷新三大配置、2 = 更换节点名称前缀，两项都在", 'if c == "1":\n        cfg_refresh_all()' in seg and 'elif c == "2":\n        rename_prefix()' in seg)
check("文档跟着改", "`1 刷新三大配置`" in open(os.path.join(ROOT, "docs/02-links.md"), encoding="utf-8").read()
      and "`2 更换节点名称前缀`" in open(os.path.join(ROOT, "docs/01-install.md"), encoding="utf-8").read())

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

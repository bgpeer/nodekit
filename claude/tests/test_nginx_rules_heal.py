#!/usr/bin/env python3
"""nginx 老规则自动重建（仓库主人 10/23）：老机器的 bgpeer.conf 还是老规则（ws 444 断连接、xhttp / grpc 不带斜杠被 301、
  xhttp 不换错误页），打开面板或每天 03:20 那趟按现有节点重建一次；平时只读一下文件就走，重建过就是新规则、以后跳过。
  新装的配置（含 reality 入口回落用的本地 h2c）不能被认成老规则，不然天天重建。平滑重载，不断连接。
  会话里用上一版装一台（SNI 分流 / nginx 前置各一台）换新脚本真跑过：重建一次后摸路径回 404 页、18 个节点全通、再调 no-op。
"""
import os, sys, json, time, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
m.G.update({"domain": "d.example"})
m.NGINX_WS[:] = [{"path": "/wsp", "port": 1}, {"path": "/xhp", "port": 2, "kind": "xhttp"}, {"path": "/grpcab", "port": 3, "kind": "grpc"}]
new = m._nginx_80_server() + m._nginx_https_server("  listen 443 ssl;\n")
fb = ("server {\n  listen 127.0.0.1:%d;\n  location /grpcxy/ {\n    grpc_pass grpc://127.0.0.1:9;\n  }\n}\n" % m.REALITY_FB_PORT)
check("新装的规则：不算老规则", not m._nginx_rules_old(new))
check("新装 + reality 入口的本地 h2c 回落口：也不算（那里外面摸不到）", not m._nginx_rules_old(new + fb))
check("ws 还是 444：老规则", m._nginx_rules_old(new.replace("return 404; }", "return 444; }", 1)))
check("xhttp 没有不带斜杠的 404：老规则", m._nginx_rules_old(new.replace("  location = /xhp {\n    return 404;\n  }\n", "")))
check("xhttp 没换错误页：老规则", m._nginx_rules_old(new.replace("    grpc_intercept_errors on;\n", "")))
check("只有 80 的 acme 小站（reality 占 443）：不算", not m._nginx_rules_old(m._nginx_80_server()))
t0 = time.time()
for _ in range(1000):
    m._nginx_rules_old(new + fb)
check(f"平时只读一下：判断一次 {(time.time() - t0):.3f} 毫秒级", (time.time() - t0) < 1.0)

tmp = tempfile.mkdtemp()
m.NGINX_CONF, m.STATE_FILE = f"{tmp}/bgpeer.conf", f"{tmp}/state.json"
calls = []
m._nginx_sync_from_cfg = lambda: calls.append(dict(m.G)) or "✔ nginx 443 反代表已按现有节点重建（3 个 ws/xhttp）"
json.dump({"domain": "st.example", "nginx": "1"}, open(m.STATE_FILE, "w"))
open(m.NGINX_CONF, "w").write(new)
check("新规则：什么都不做", m._nginx_rules_heal() == "" and not calls)
open(m.NGINX_CONF, "w").write(new.replace("return 404; }", "return 444; }", 1))
m.G["domain"] = "keep.example"; g_before = dict(m.G)
r = m._nginx_rules_heal()
check(f"老规则：按安装记录重建一次（{r}）", r.startswith("✔ nginx 规则已更新") and calls and calls[0]["domain"] == "st.example")
check("用完 G 放回原样", m.G == g_before)
os.remove(m.NGINX_CONF)
check("没有 nginx 前置（没有这个文件）：什么都不做", m._nginx_rules_heal() == "")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
body = lambda n: src[src.index(f"def {n}("):src.index("\ndef ", src.index(f"def {n}(") + 5)]
check("打开面板、每天 03:20 都挂上了", "_nginx_rules_heal()" in body("main_menu") and "_nginx_rules_heal()" in body("cfg_auto_run"))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

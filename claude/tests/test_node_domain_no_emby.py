#!/usr/bin/env python3
"""15 → 2「更换节点域名」不再问 Emby、不动 Emby（仓库主人 10/31：「不要干预emby吧，直接做成不询问，还是在emby设置里面
  做一个更换域名证书的口子吧，自建DNS里面也做一个……这样他们就互相不干预了」）。
  · 流程里没有「Emby 也换成」那一问；Emby 跟节点共用证书（软链）时，新证书接着把 *.Emby底座 签进去，Emby 一个字节不动照样能用
  · 不要求给 Emby / 自建 DNS 自己的域名建记录；Emby 的底座正好是旧节点域名、又是自己一张证书时，撤旧记录别把它那条撤了
  · 自建 DNS 还跟节点共用域名（老装法）：照旧一起换，屏上一行指到 13 → 10 先分开"""
import os, io, re, sys, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("x", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
plain = lambda t: re.sub(r"\033\[[0-9;]*m", "", t)

T = tempfile.mkdtemp()
x.AGH_DOMAIN_FILE = T + "/agh.domain"
x.MS_NGINX_CONF = T + "/media-stack.conf"
open(x.MS_NGINX_CONF, "w").write("server {\n    server_name emby.media.zone2.example;\n}\n"
                                 "server {\n    server_name list.media.zone2.example;\n}\n")
OLD, NEW, EMBY = "node.zone.example", "node2.zone.example", "media.zone2.example"
x.node_domain = lambda: OLD
x.public_ip = lambda: "198.51.100" + ".7"
x._agh_tls = lambda: {}
x.cert_names = lambda: [OLD, "*." + OLD, EMBY, "*." + EMBY]
x._acme_has_cf = lambda: True
x._domain_targets = lambda old: []
x._domain_switch_notice = lambda new: []
RES = []
x._domain_resolves_here = lambda d: (RES.append(d) or True, "ok")
ROWS = [(OLD, "节点证书", "ok", ["订阅地址"]),
        ("*." + OLD, "节点证书", "ok", ["没找到在用的地方"]),
        ("emby.media.zone2.example", "节点证书", "ok", ["Emby"]),
        ("list.media.zone2.example", "节点证书", "ok", ["OpenList"]),
        ("*." + EMBY, "节点证书", "ok", ["Emby 那几个子域"])]
x.domain_uses = lambda i=None: ROWS
PLAN = []
x._node_op_dispatch = lambda plan, fn: PLAN.append(plan)
ASKED = []
def flow(i, answers):
    q = list(answers); PLAN.clear(); RES.clear(); ASKED.clear()
    x._ask = lambda p="": (ASKED.append(p), q.pop(0) if q else "")[1]
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        x.cert_change_domain_flow(i)
    return plain(b.getvalue())

print("流程：不问 Emby")
out = flow({"emby_domain": EMBY, "emby_shared": True}, [NEW, "yes"])
check("没有「Emby 也换成」那一问，只问了新域名和 yes", len(ASKED) == 2 and not any("Emby" in a for a in ASKED))
check("Emby 共用节点证书：计划里带着 emby_keep（新证书接着盖住它），没有 emby_new",
      PLAN and PLAN[0].get("emby_keep") == EMBY and "emby_new" not in PLAN[0])
check("不要求给 Emby 的子域建记录（只查节点的）", RES and all(EMBY not in d for d in RES))
check("屏上说清 Emby 不动、在哪换", "Emby（<子域>.media.zone2.example，换它在「16 自建 Emby → 7 设置 → 12 域名证书」）" in out)
check("「当前关联的域名」里不列 Emby 的", "list.media.zone2.example" not in out.split("▸ 换之前")[0])
out = flow({"emby_domain": EMBY, "emby_shared": False}, [NEW, "yes"])
check("Emby 各用各的：emby_keep 为空", PLAN and PLAN[0].get("emby_keep") == "")
out = flow({}, [NEW, "yes"])
check("没装 Emby：照常换、不提 Emby", PLAN and "Emby（" not in out)
check("菜单叫「更换节点域名」", '(2, "更换节点域名"' in open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read())

print("自建 DNS")
open(x.AGH_DOMAIN_FILE, "w").write("dns.zone3.example\n")
x._agh_domain = lambda: "dns.zone3.example"
ROWS.append(("dns.zone3.example", "自建 DNS 自己的证书", "ok", ["自建 DNS：DoT 853"]))
out = flow({}, [NEW, "yes"])
check("有自己域名的自建 DNS：不列、不要求建记录、屏上说在 13 → 10 换",
      "dns.zone3.example" not in out.split("▸ 换之前")[0] and all("dns.zone3.example" not in d for d in RES)
      and "自建 DNS（dns.zone3.example，换它在「13 自建 DNS → 10 更换域名证书」）" in out)
os.remove(x.AGH_DOMAIN_FILE); ROWS.pop()
x._agh_tls = lambda: {"enabled": "true", "server_name": OLD}
out = flow({}, [NEW, "yes"])
check("老装法（自建 DNS 跟节点共用域名）：照旧一起换，一行指到 13 → 10 先分开",
      "自建 DNS 还跟节点共用这个域名，这次会一起换" in out and PLAN)
x._agh_tls = lambda: {}

print("真换的那一步")
x.STATE_FILE, x.HOST_FILE, x.NODE_FILE = T + "/state.json", T + "/sub.host", T + "/nodes.txt"
x.SB_DIR = x.XRAY_DIR = T + "/none"
x.NGINX_CONF = x.AGH_YAML = T + "/none.conf"
SIGNED = []
def apply(emby_keep, emby_cert, shared, sign_ok=True):
    json.dump({"domain": OLD}, open(x.STATE_FILE, "w")); open(x.HOST_FILE, "w").write(OLD)
    open(x.NODE_FILE, "w").write("")
    SIGNED.clear(); FORGOT.clear()
    x.cert_names = lambda: []
    x.cert_install_apply = lambda dom, wild, tok, names=None, node_dom=None: (SIGNED.append(names), sign_ok)[1]
    x._emby_cert = lambda: (emby_cert, None)
    x._emby_shared = lambda d: shared
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = x.cert_change_domain_apply(NEW, "", emby_keep)
    return r, plain(b.getvalue())
FORGOT = []
x._acme_forget = lambda dom, keep=(): (FORGOT.append((dom, tuple(keep))), True)[1]
x._cert_usable_for = lambda need: (True, "ok")
x.read_saved_links = lambda: []
x.build_subscription = lambda *a, **k: True
x._cert_consumers = lambda: []
x.restart_services = lambda *a: None
r, out = apply(EMBY, EMBY, True)
check("共用：新证书签 新域名 + *.新域名 + Emby 底座 + *.底座", r and SIGNED and SIGNED[0] == [NEW, "*." + NEW, EMBY, "*." + EMBY])
check("Emby 的 .env / nginx 一个字节不动（没有 Emby 换域名那一步了）", "Emby 也换好了" not in out
      and "Emby 没动，照旧是 <子域>.media.zone2.example（新证书接着盖住它）" in out)
r, out = apply("", OLD, False)
check("Emby 底座正好是旧节点域名、自己一张证书：撤旧记录时留着 *.旧域名（它那条）",
      FORGOT and FORGOT[0] == (OLD, ("*." + OLD,)))
r, out = apply("", "", False)
check("没装 Emby：照常两条都撤", FORGOT and FORGOT[0] == (OLD, ()))
r, out = apply(EMBY, EMBY, True, sign_ok=False)
check("证书没签下来：整件事回滚（state 还是旧域名）", r is False and json.load(open(x.STATE_FILE))["domain"] == OLD)

print("撤记录的 keep")
spec2 = importlib.util.spec_from_file_location("x2", os.path.join(ROOT, "xy-installer.py"))
x2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(x2)
CMDS = []
x2.sh = lambda c, check=True: CMDS.append(c) or ""
x2.os.path.exists = (lambda e: lambda p: True if p.endswith("acme.sh/acme.sh") else e(p))(x2.os.path.exists)
x2._acme_forget(OLD, keep=("*." + OLD,))
check("keep 里的那条不撤，别的照撤", any(f"'{OLD}'" in c for c in CMDS) and not any(f"'*.{OLD}'" in c for c in CMDS))
print("selfdns-refresh（自建 DNS 换了自己的域名后叫它）")
x2.SELFDNS_FLAG = T + "/selfdns.on"
x2.privdns_conf = lambda: {"mode": "off", "dns": "", "name": ""}
REGEN = []
x2._privdns_regen = lambda: REGEN.append(1) or 0
b = io.StringIO()
with contextlib.redirect_stdout(b):
    x2.selfdns_refresh()
check("订阅里没写自建 DNS：不刷新、一行说明", not REGEN and "不用刷新" in b.getvalue())
open(x2.SELFDNS_FLAG, "w").write("1")
b = io.StringIO()
with contextlib.redirect_stdout(b):
    x2.selfdns_refresh()
check("写着：三大配置重生成、✔ 一行", REGEN and "✔ 订阅里的自建 DNS 地址已换成新域名" in b.getvalue())
os.remove(x2.SELFDNS_FLAG); REGEN.clear()
x2.privdns_conf = lambda: {"mode": "self", "dns": "", "name": ""}
with contextlib.redirect_stdout(io.StringIO()):
    x2.selfdns_refresh()
check("本机当 DNS 机（被藏节点去问它）：也重生成", REGEN)
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("主脚本认 selfdns-refresh 子命令", 'sys.argv[1] == "selfdns-refresh"' in src)
check("「Emby 跟着节点换」那套删干净了", "_emby_set_domain" not in src and "_emby_default_base" not in src
      and "emby_new" not in src)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

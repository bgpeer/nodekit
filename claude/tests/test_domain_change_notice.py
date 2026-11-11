#!/usr/bin/env python3
"""更换节点域名（15 → 2）真机走了一遍后查出的两处（仓库主人 11/11 的截图）：
  一、换完那一屏「订阅地址」底下印的是 xy-sub 的 systemd unit 正文（[Unit] / ExecStart=…），新地址反而没有。
      现在印三种订阅的新地址；本机节点链接地址（.links）也列出来——聚合了本机的机器存的还是旧域名，要去换。
  二、节点专用域名（13 → 4）要的 *.随机段.根域名 没跟着签进新证书，换完私有名当场失效、要人再去选一次补签。
"""
import os, re, ast, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

print("一、换完那一屏的地址")
UNIT = "[Unit]\nAfter=network.target\n[Service]\nExecStart=/usr/bin/python3 /etc/bgpeer/xy-sub-server.py 1 /etc/bgpeer/sub\n"
x._sub_service_text = lambda: UNIT
x._sub_items = lambda: [("yaml", "https://new.example:1/TokY/VPS.yaml"), ("json", "https://new.example:1/TokJ/VPS.json"),
                        ("conf", "https://new.example:1/TokC/VPS.conf")]
x.links_url = lambda: "https://new.example:1/TokL.links"
x.AGH_DOMAIN_FILE = "/nonexistent/agh.domain"
x._selfdns_doh = lambda: ""
for f in ("_ghrelay_prefix",):
    setattr(x, f, lambda: "")
rows = x._domain_switch_notice("new.example")
txt = "\n".join(f"{a}\n{b}\n{c}" for a, b, c in rows)
check("不再印 systemd unit 正文", "[Unit]" not in txt and "ExecStart" not in txt)
check("三种订阅的新地址各一条，标着是哪种客户端的", [r[1] for r in rows[:3]] == [u for _e, u in x._sub_items()]
      and "mihomo" in rows[0][0] and "sing-box" in rows[1][0] and "小火箭" in rows[2][0])
check("每条都提醒：客户端要重新导入，不是刷新", all("重新导入" in r[2] for r in rows[:3]))
lk = [r for r in rows if r[1] == "https://new.example:1/TokL.links"]
check("本机节点链接地址（.links）也列出来，提醒聚合了本机的机器去换", len(lk) == 1 and "聚合" in lk[0][0] and "3 聚合节点链接" in lk[0][2])
x.links_url = lambda: ""
check("没开过节点链接地址的不列这一条", not [r for r in x._domain_switch_notice("new.example") if "聚合" in r[0]])
x._sub_items = lambda: []
check("订阅还没生成：说还没生成，不报错", ("订阅地址", "(还没生成)", "面板 2") in x._domain_switch_notice("new.example"))

print("二、节点专用域名的泛域名跟着签")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "cert_change_domain_apply")
body = ast.get_source_segment(src, fn)
i = body.index('names = [new, f"*.{new}"]'); j = body.index("cert_install_apply(")
seg = body[i:j]
check("要签的名字里带上 *.<节点专用域名的根>（跟 cert_want_names 同一个口径：同一个根域名才带、已经盖住的不重复）",
      "_privdns_root()" in seg and 'names.append(f"*.{_proot}")' in seg and "_root_domain(new) == _root_domain(_proot)" in seg)
ns = {"_privdns_root": lambda: "ab12cd.example.org", "_root_domain": x._root_domain, "_name_covers": x._name_covers,
      "new": "n2.example.org", "emby_keep": ""}
exec(re.sub(r"(?m)^    ", "", seg[:seg.index("\n", seg.index("names.append(f\"*.{_proot}\")"))]), ns)
check("同一个根域名：新证书签 新域名 + *.新域名 + *.随机段.根域名", ns["names"] == ["n2.example.org", "*.n2.example.org", "*.ab12cd.example.org"])
ns.update(new="n2.other.net", names=None)
exec(re.sub(r"(?m)^    ", "", seg[:seg.index("\n", seg.index("names.append(f\"*.{_proot}\")"))]), ns)
check("换到别的根域名：不带（那个根域名的令牌未必管得到，私有名也不在它底下）", ns["names"] == ["n2.other.net", "*.n2.other.net"])
ns.update(new="n2.example.org", _privdns_root=lambda: "")
exec(re.sub(r"(?m)^    ", "", seg[:seg.index("\n", seg.index("names.append(f\"*.{_proot}\")"))]), ns)
check("没开节点专用域名：照旧只签两个", ns["names"] == ["n2.example.org", "*.n2.example.org"])

print("三、换到别的根域名：私有解析开着就在新根域名下起一个新的私有名、一起签（不用再去 13 → 4 手动选）")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "cert_change_domain_apply")
body = ast.get_source_segment(src, fn)
seg = body[body.index('names = [new, f"*.{new}"]'):body.index("    have = cert_names()")]
def plan(new, mode, name, free=True, proot=None):
    said = []
    ns = {"_privdns_root": lambda: (name.split(".", 1)[1] if mode != "off" and name.count(".") >= 2 and not name.endswith(".internal") else "") if proot is None else proot,
          "_root_domain": x._root_domain, "_name_covers": x._name_covers, "_privdns_is_domain_name": x._privdns_is_domain_name,
          "privdns_conf": lambda: {"mode": mode, "name": name, "dns": ""}, "_privdns_public_free": lambda r: free,
          "_privdns_new_name": lambda r: "newprivabc.seg123." + r, "new": new, "emby_keep": "", "Y": "", "N": "",
          "print": lambda *a: said.append(" ".join(map(str, a)))}
    exec(re.sub(r"(?m)^    ", "", seg), ns)
    return ns["names"], ns["need"], ns["_pname_new"], said
names, need, pn, _ = plan("n2.other.net", "remote", "abcdefghij.seg999.example.org", proot="")
check("外机 DNS 开着、换了根域名：新证书多签 *.随机段.新根域名，新私有名也算进「证书要盖住的」",
      names == ["n2.other.net", "*.n2.other.net", "*.seg123.other.net"] and pn == "newprivabc.seg123.other.net" and pn in need)
names, need, pn, _ = plan("n2.other.net", "self", "abcdefghij.seg999.example.org", proot="")
check("本机 DNS 开着：一样", "*.seg123.other.net" in names and pn)
names, need, pn, _ = plan("n2.other.net", "off", "abcdefghij.seg999.example.org")
check("私有解析关着：不签、照用公网域名", names == ["n2.other.net", "*.n2.other.net"] and not pn)
names, need, pn, _ = plan("n2.other.net", "remote", "abcdefghij.internal")
check("用 IP 装时起的 .internal 私有名：不是节点专用域名，不签", names == ["n2.other.net", "*.n2.other.net"] and not pn)
names, need, pn, said = plan("n2.other.net", "remote", "abcdefghij.seg999.example.org", free=False, proot="")
check("新根域名在 Cloudflare 里有泛解析（藏不住）：不签，屏上一行 ⚠ 说照用公网域名", not pn and len(names) == 2 and "藏不住" in said[0])
names, need, pn, _ = plan("n2.example.org", "remote", "abcdefghij.seg999.example.org")
check("同一个根域名：沿用原来的私有名（多签它那个泛域名），不另起新的", names == ["n2.example.org", "*.n2.example.org", "*.seg999.example.org"]
      and not pn and "x.seg999.example.org" in need)
after = body[body.index('G["host"] = _host()'):body.index("三格式订阅已按新域名重新渲染")]
check("新私有名在重新生成 .links / 订阅之前存下，nginx 里旧私有名换成新的；证书没盖住就不存",
      "_name_covers(cert_names(), _pname_new)" in after and "privdns_save(_pc)" in after
      and "_nginx_sub_domain(_t, _pname_old, _pname_new)" in after
      and after.index("privdns_save(_pc)") < after.index("build_subscription("))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

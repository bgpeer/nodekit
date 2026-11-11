#!/usr/bin/env python3
"""面板 2 和 17：做完一项留在这一屏接着选，回车 / 0 直接回主菜单（仓库主人 11/11：
  「这个不要显示直接回退到主页」「17 VPS 网络检测里面也是检测完了应该也是可以选择的，要回主页回车也是可以回去的」）。
  以前刷新完三大配置 / 检测完一项就印「按回车返回主菜单...」，想接着做还得从主菜单再进一次。
"""
import os, io, sys, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
x = importlib.util.module_from_spec(spec); spec.loader.exec_module(x)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
ANS, DID = [], []
def ask(p=""):
    print(p, end="")
    if not ANS:
        raise AssertionError("还在问：没按预期回主菜单")
    return ANS.pop(0)
x._ask = ask
def run(f, *answers):
    ANS[:] = list(answers); DID.clear()
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        r = f()
    return r, b.getvalue()

print("一、面板 2 节点链接 / 订阅")
x.read_saved_links = lambda: ["link1"]            # 链接正文这里用不到（打印那步换成假的了）
x._sub_service_synced = lambda: True
x.sub_offer_cert_domain = lambda: None
x.print_node_links = lambda links: print("LINKS")
x.print_cloud_ports = lambda: None
x._sub_items = lambda: True
x.print_sub_urls = lambda: print("SUBURLS")
x.cfg_refresh_all = lambda: (DID.append("refresh"), print("  ✔ mihomo 已刷新"))
x.rename_prefix = lambda: (DID.append("rename"), print("  ✔ 前缀已改"))
r, out = run(x.show_links, "1", "")
menu = "  1. 刷新三大配置\n  2. 更换节点名称前缀\n  0. 返回\n"
check("选 1 刷新完：下面再列一遍三项接着选，不回主菜单", DID == ["refresh"] and out.count(menu) == 2
      and out.index("已刷新") < out.rindex(menu))
check("链接和订阅那一大屏只印一次（不重刷）", out.count("LINKS") == 1 and out.count("SUBURLS") == 1)
check("回车：回主菜单，返回 True（告诉主菜单别再停一下）", r is True)
r, out = run(x.show_links, "1", "2", "1", "0")
check("能连着做：刷新 → 改前缀 → 再刷新，0 才回", DID == ["refresh", "rename", "refresh"] and r is True and out.count(menu) == 4)
r, out = run(x.show_links, "9", "0")
check("打了没有的编号：说无效，接着问", "无效选择" in out and r is True and not DID)
x.read_saved_links = lambda: []
r, out = run(x.show_links)
check("还没装节点：照旧一句话，主菜单照旧停一下（返回的不是 True）", "还没有节点" in out and r is not True)

print("二、17 VPS 线路检测")
x.public_ip = lambda: "1.2.3.4"; x.public_ip6 = lambda: ""
x.VPSCHK_LAST = f"{tmp}/none.json"
x.ensure_remote_script = lambda url, local: True
x._run_child = lambda cmd, **k: (DID.append(cmd.split(" ", 2)[-1] if cmd.count(" ") > 1 else ""), print("  …检测输出…"))
items = "  1. 本机 IP 检测（约 2 分钟）\n  2. 外部 IP 检测\n  3. 解锁检测（流媒体 / AI）\n  0. 返回\n"
r, out = run(x.vps_check_menu, "3", "")
check("解锁检测跑完：再列一遍四项接着选；回车回主菜单、返回 True", DID == ["--unlock"] and out.count(items) == 2 and r is True
      and out.index("检测输出") < out.rindex(items))
check("本机 IP / 上次检测那一段只印一次", out.count("▸ 本机 IPv4") == 1)
r, out = run(x.vps_check_menu, "1", "2", "1.2.3.5", "3", "0")
check("能连着做：本机 → 外部 IP → 解锁", DID == ["", "1.2.3.5", "--unlock"] and r is True)
r, out = run(x.vps_check_menu, "2", "not-an-ip", "0")
check("外部 IP 填错：说一句，留在这一屏（以前直接退回主菜单）", "不是合法的 IP 地址" in out and not DID and out.count(items) == 2)
r, out = run(x.vps_check_menu, "")
check("一进来就回车：直接回主菜单", r is True and not DID)

print("三、主菜单")
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("2 和 17 自己选完回来的不再问「按回车返回主菜单」，别的项照旧停一下",
      'if show_links():' in src and 'if vps_check_menu():' in src and src.count('_ask("\\n按回车返回主菜单...")') == 1)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

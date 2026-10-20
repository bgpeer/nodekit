#!/usr/bin/env python3
"""开着 nginx SNI 分流时「删除协议 → 全部删除」—— 仓库主人 10/20 截图：全部删除被挡（「443 的走向依赖 … 请走全部重新安装」）。
  挂在 nginx 后面的一个不剩时不用重建后端：删完把分流收成只剩伪装站（write_nginx_sni_split 两张表空着写，nginx -t 不过它自己回滚）。
  只删一部分、还剩挂在 nginx 后面的：照旧挡住、什么都不动。
"""
import os, sys, io, json, tempfile, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
sbcfg, xrcfg = f"{tmp}/sb.json", f"{tmp}/xr.json"
m._CORE_META = {"sb": ("sing-box", f"{tmp}/sbbin", sbcfg, m.SB, "¹"), "xray": ("xray", f"{tmp}/xrbin", xrcfg, m.XRAY, "²")}
m.NODE_FILE = f"{tmp}/nodes.txt"; m.STATE_FILE = f"{tmp}/state.json"
calls = []
m.sh = lambda c, check=True: calls.append(c) or ""
m.core_check = lambda b, p: (True, "")
m.build_subscription = lambda *a, **k: None
m.write_nginx_sni_split = lambda: calls.append(("split", list(m.NGINX_WS), list(m.NGINX_STREAM))) or True
m.write_nginx_conf = lambda: calls.append("write_nginx_conf(443)")
m._node_op_dispatch = lambda plan, fb: fb()
m._sni_precheck_one = lambda: None
def setup():
    json.dump({"inbounds": [
        {"tag": "P·vless-ws", "listen": "127.0.0.1", "listen_port": 1001, "transport": {"path": "/a"}},
        {"tag": "P·reality-vision", "listen": "127.0.0.1", "listen_port": 1002, "tls": {"server_name": "www.a.com", "reality": {}}},
        {"tag": "P·trojan", "listen": "::", "listen_port": 1003}]}, open(sbcfg, "w"))
    json.dump({"inbounds": [{"tag": "P·trojan²", "port": 2001}]}, open(xrcfg, "w"))
    open(m.NODE_FILE, "w").write("")
    json.dump({"sni_split": "1", "nginx": "1", "sb": ["vless-ws", "reality-vision", "trojan"], "xray": ["trojan"]}, open(m.STATE_FILE, "w"))
    calls.clear(); m.NGINX_WS[:] = [{"path": "/old", "port": 9}]; m.NGINX_STREAM[:] = [{"sni": "www.a.com", "port": 1002}]
def run(answers):
    it = iter(answers); m._ask = lambda q: next(it, "")
    m._pick = lambda title, items, default=None: ["vless-ws"] if "sing-box" in title else []
    st = json.load(open(m.STATE_FILE)); buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.del_protocols_flow(st, list(st["sb"]), list(st["xray"]))
    return buf.getvalue()

# 1. 只删一部分（vless-ws），reality-vision 还挂在 nginx 后面 → 照旧挡住
setup(); o = run(["1"])
check("只删一部分：照旧挡住，提示走全部重新安装", "请走『全部重新安装』" in o and json.load(open(sbcfg))["inbounds"][0]["tag"] == "P·vless-ws")
check("挡住时 nginx 一点没动", not any(isinstance(c, tuple) for c in calls) and "write_nginx_conf(443)" not in calls)

# 2. 全部删除 → 放行；删完分流收成只剩伪装站，不写抢 443 的普通 conf
setup(); o = run(["2", "y"])
check("全部删除：不再挡", "请走『全部重新安装』" not in o and "nginx 443 分流去掉这些后端，只留伪装站" in o)
check(f"两个核心的入站都删光（{json.load(open(sbcfg))['inbounds']} / {json.load(open(xrcfg))['inbounds']}）",
      json.load(open(sbcfg))["inbounds"] == [] and json.load(open(xrcfg))["inbounds"] == [])
sp = [c for c in calls if isinstance(c, tuple)]
check(f"分流重写一次，两张表都是空的（{sp}）", sp == [("split", [], [])])
check("没去写占 443 的普通 nginx conf", "write_nginx_conf(443)" not in calls)
check("屏上一行 ✔ 收成只剩伪装站", "nginx 443 分流已收成只剩伪装站" in o)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

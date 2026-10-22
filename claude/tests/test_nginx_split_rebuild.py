#!/usr/bin/env python3
"""nginx 后面的节点一个一个删 / 一个一个加，删完加完按【剩下的】节点重建 nginx（仓库主人 10/22）。
  以前开着 SNI 分流时只删一部分会被挡（「443 的走向依赖 … 请走全部重新安装」），全部删除才放行（10/20）。
  现在：两个核心挂在 nginx 后面（监听 127.0.0.1）的入站都从配置里读出来，重写反代表 + 分流表；
  nginx -t 不过就把动过的文件原样放回（不是退回装分流之前——那样 443 整个没了）。
  菜单 4 两个核心各换各的伪装域名；手填跟另一个核心重复 → 红字、回到这一步重选。
  真 nginx + sing-box + xray + 客户端的端到端验证在会话里跑过（每一步每个节点都通），这里守逻辑。
"""
import os, sys, io, json, tempfile, contextlib, importlib.util, types, subprocess
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
m.sh = lambda c, check=True: ""
m.core_check = lambda b, p: (True, "")
m.build_subscription = lambda *a, **k: None
m._node_op_dispatch = lambda plan, fb: fb()
syncs = []
real_sync = m._nginx_sync_from_cfg
m._nginx_sync_from_cfg = lambda: syncs.append(m._nginx_backends_from_cfg()) or "✔ nginx 443 分流已按现有节点重建"

def setup():
    json.dump({"inbounds": [
        {"tag": "P·vless-ws¹", "listen": "127.0.0.1", "listen_port": 1001, "transport": {"type": "ws", "path": "/a"}},
        {"tag": "P·reality-vision¹", "listen": "127.0.0.1", "listen_port": 1002, "tls": {"server_name": "www.a.example", "reality": {}}},
        {"tag": "P·trojan¹", "listen": "::", "listen_port": 1003}]}, open(sbcfg, "w"))
    json.dump({"inbounds": [
        {"tag": "P·reality-vision²", "listen": "127.0.0.1", "port": 2001,
         "streamSettings": {"security": "reality", "realitySettings": {"serverNames": ["www.b.example"]}}},
        {"tag": "P·xhttp-tls", "listen": "127.0.0.1", "port": 2002,
         "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": "/x"}}},
        {"tag": "P·trojan²", "port": 2003}]}, open(xrcfg, "w"))
    open(m.NODE_FILE, "w").write("")
    json.dump({"sni_split": "1", "nginx": "1", "domain": "d.example", "sb": ["vless-ws", "reality-vision", "trojan"],
               "xray": ["reality-vision", "xhttp-tls", "trojan"]}, open(m.STATE_FILE, "w"))
    syncs.clear()

def run(answers, pick_sb, pick_xr):
    it = iter(answers); m._ask = lambda q: next(it, "")
    m._pick = lambda title, items, default=None: pick_sb if "sing-box" in title else pick_xr
    st = json.load(open(m.STATE_FILE)); buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.del_protocols_flow(st, list(st["sb"]), list(st["xray"]))
    return buf.getvalue()

# 1. 从配置里认出两个核心挂在 nginx 后面的
setup()
ws, stream = m._nginx_backends_from_cfg()
check(f"反代表：sb ws + xray xhttp（{ws}）", ws == [{"path": "/a", "port": 1001}, {"path": "/x", "port": 2002, "kind": "xhttp"}])
check(f"分流表：两个伪装域名各一条（{stream}）", stream == [{"sni": "www.a.example", "port": 1002}, {"sni": "www.b.example", "port": 2001}])

# 2. 只删一部分（sb vless-ws + xray reality-vision）→ 不再挡，删完按剩下的重建
setup(); o = run(["1", "y"], ["vless-ws"], ["reality-vision"])
check("只删一部分：不再挡，确认框写明 nginx 按剩下的重建", "全部重新安装" not in o and "nginx 443 按剩下的节点重建" in o)
check(f"重建一次，剩下的后端对得上（{syncs}）", syncs == [([{"path": "/x", "port": 2002, "kind": "xhttp"}],
                                                       [{"sni": "www.a.example", "port": 1002}])])
check("没删的入站都还在", [ib["tag"] for ib in json.load(open(sbcfg))["inbounds"]] == ["P·reality-vision¹", "P·trojan¹"]
      and [ib["tag"] for ib in json.load(open(xrcfg))["inbounds"]] == ["P·xhttp-tls", "P·trojan²"])

# 3. 删的都不在 nginx 后面（两个 trojan）→ 不碰 nginx
setup(); o = run(["1", "y"], ["trojan"], ["trojan"])
check("删的不在 nginx 后面：nginx 不动", not syncs and "nginx 443" not in o)

# 4. 全部删除 → 两张表空着重建
setup(); o = run(["2", "y"], [], [])
check(f"全部删除：重建一次、两张表都空（{syncs}）", syncs == [([], [])])

# 5. nginx -t 不过 → 动过的三个文件原样放回
m._nginx_sync_from_cfg = real_sync
m.NGINX_CONF, m.NGINX_STREAM_CONF, m.NGINX_MAIN = f"{tmp}/site.conf", f"{tmp}/stream.conf", f"{tmp}/nginx.conf"
for f, t in ((m.NGINX_CONF, "OLD-SITE"), (m.NGINX_STREAM_CONF, "OLD-STREAM"), (m.NGINX_MAIN, "OLD-MAIN\n# BGPEER-STREAM-BEGIN\n")):
    open(f, "w").write(t)
m.G.update({"domain": "d.example", "sni_split": "1", "nginx": "1"})
m.subprocess = types.SimpleNamespace(run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "bad"))
setup(); r = m._nginx_sync_from_cfg()
check(f"nginx -t 不过：报 ⚠ 且三个文件原样放回（{r}）", r.startswith("⚠") and open(m.NGINX_CONF).read() == "OLD-SITE"
      and open(m.NGINX_STREAM_CONF).read() == "OLD-STREAM" and open(m.NGINX_MAIN).read().startswith("OLD-MAIN"))
m.subprocess = types.SimpleNamespace(run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
m.nginx_reload = lambda: None
r = m._nginx_sync_from_cfg()
sc = open(m.NGINX_STREAM_CONF).read()
check(f"nginx -t 过：分流表写进两个伪装域名（{r}）", r.startswith("✔") and "www.a.example  127.0.0.1:1002" in sc
      and "www.b.example  127.0.0.1:2001" in sc and "grpc_pass grpc://127.0.0.1:2002" in open(m.NGINX_CONF).read())

# 6. 菜单 4：手填跟另一个核心重复 → 红字、回到这一步；随机避开两个
m.REALITY_SNI_POOL = ["www.a.example", "www.b.example", "www.c.example"]
it = iter(["2", "www.a.example", "2", "www.d.example"]); m._ask = lambda q: next(it, "")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    v = m._choose_new_sni("www.b.example", "www.a.example")
check(f"手填重复：红字说重复，重选后拿到新的（{v}）", "重复了" in buf.getvalue() and v == "www.d.example")
m._pick_ok_sni = lambda skip: None
it = iter(["1"]); m._ask = lambda q: next(it, "")
with contextlib.redirect_stdout(io.StringIO()):
    v = m._choose_new_sni("www.b.example", "www.a.example")
check(f"随机：避开当前和另一个核心的（{v}）", v == "www.c.example")

# 7. 装的时候问 xray 那个：跟 sing-box 的重复 → 红字、重填
it = iter(["www.a.example", "www.e.example"]); m._ask = lambda q: next(it, "")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    v = m.ask_reality_sni("xray 的 reality 伪装域名", "", "www.a.example")
check(f"第二个填重复：红字、重填（{v}）", "重复了" in buf.getvalue() and v == ("www.e.example", ""))
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

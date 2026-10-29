#!/usr/bin/env python3
"""「云防火墙要放行的端口」—— 仓库主人 10/23：GCP 防火墙少放行一个端口，trojan² 一直不通，让 GPT 从订阅里摘端口漏了最后一个。
  放在单节点链接和订阅之间；只列本机（10/29 仓库主人定：成员机的端口到那台自己的面板上看，主机不再去拉）；
  hy2 跳跃段带上（落在段里的单端口不重复列）；127.0.0.1 的不列；CDN 节点的端口在 Cloudflare 上不算。
"""
import os, sys, io, json, base64, tempfile, contextlib, threading, http.server, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
m.SB_DIR, m.XRAY_DIR = f"{tmp}/sb", f"{tmp}/xr"; os.makedirs(m.SB_DIR); os.makedirs(m.XRAY_DIR)
m.SUBPORT_FILE, m.NGINX_CONF, m.NGINX_STREAM_CONF = f"{tmp}/sub.port", f"{tmp}/ng.conf", f"{tmp}/stream.conf"
m.NODE_FILE, m.PEERS_FILE = f"{tmp}/nodes.txt", f"{tmp}/peers.json"
m._cdn_load = lambda: [{"cf_port": 2087}]
AT = "@"                                  # 拼出来，免得 test_claude_folder 当成邮箱
json.dump({"inbounds": [{"type": "trojan", "listen": "::", "listen_port": 18047},
                        {"type": "hysteria2", "listen": "::", "listen_port": 31000},
                        {"type": "vless", "listen": "127.0.0.1", "listen_port": 9999}]}, open(f"{m.SB_DIR}/config.json", "w"))
json.dump({"inbounds": [{"listen": "0.0.0.0", "port": 36844, "protocol": "trojan"},
                        {"listen": "127.0.0.1", "port": 9998}]}, open(f"{m.XRAY_DIR}/config.json", "w"))
open(m.SUBPORT_FILE, "w").write("29235")
open(m.NGINX_CONF, "w").write("server { listen 80; }\nserver { listen 443 ssl; }\n")
open(m.NODE_FILE, "w").write("\n".join([
    f"trojan://pw{AT}a.example:18047?security=tls#🇺🇸OR·trojan¹",
    f"hysteria2://pw{AT}a.example:31000?mport=31000-32000#🇺🇸OR·hy2",
    f"vless://u{AT}cf.example:443?security=tls#🇺🇸OR·CDN·优选1"]) + "\n")

# 成员机：本地起一个 .links
vm = base64.b64encode(json.dumps({"add": "b.example", "port": "44489", "ps": "🇯🇵JP·vmess-ws"}).encode()).decode()
peer_links = "\n".join([f"vless://u{AT}b.example:22784?security=reality#🇯🇵JP·reality-vision", "vmess://" + vm,
                        f"vless://u{AT}cf.example:2053?security=tls#🇯🇵JP·CDN·优选2", "# 订阅链接:", "https://b.example:1/x.yaml"])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(peer_links.encode())
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
json.dump([f"http://127.0.0.1:{port}/t.links", "http://127.0.0.1:1/dead.links"], open(m.PEERS_FILE, "w"))

rows = m.cloud_ports()
check(f"本机：节点 + 订阅 + 80/443 + CDN 回源 + hy2 跳跃段，127.0.0.1 的不列（{rows[0]}）",
      rows[0] == ("本机 🇺🇸OR", "80,443,2087,18047,29235,31000-32000,36844"))
check(f"只列本机，聚合了成员机也不列它们（{len(rows)} 行）", len(rows) == 1)
fetched = []
m._fetch_text = lambda u, timeout=15: fetched.append(u) or ""
m.cloud_ports()
check("不再去拉成员机的 .links（进「2 节点链接」不用等它们）", not fetched)

# 屏上：在单节点链接和订阅之间；标题一行、端口顶格一行、台与台空一行
m._sub_items = lambda: True
m.print_sub_urls = lambda: print("SUBURLS")
m._sub_service_synced = lambda: True
m.sub_offer_cert_domain = lambda: None
m._ask = lambda q="": "0"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.show_links()
o = buf.getvalue()
i_link, i_fw, i_sub = o.index("OR·trojan¹"), o.index("云防火墙要放行的端口"), o.index("SUBURLS")
check("「2 节点链接」：单节点链接 → 云防火墙端口 → 订阅", i_link < i_fw < i_sub)
seg = o[i_fw:i_sub].splitlines()
check("标题一行、端口顶格一行", "[本机 🇺🇸OR]" in seg and seg[seg.index("[本机 🇺🇸OR]") + 1] == "80,443,2087,18047,29235,31000-32000,36844")
check("屏上没有成员机", "成员" not in o[i_fw:i_sub])
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
body = lambda n: src[src.index(f"def {n}("):src.index("\ndef ", src.index(f"def {n}(") + 5)]
check("装完、只添加新协议两屏也在链接和订阅之间", body("run").index("print_cloud_ports()") < body("run").index('ui_title("订阅链接')
      and body("_add_apply").index("print_node_links(new_links)") < body("_add_apply").index("print_cloud_ports()")
      < body("_add_apply").index('ui_title("订阅链接'))
srv.shutdown()
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""挂在 nginx 443 后面的路径被人摸到（浏览器 / curl / 扫描器拿普通请求来试）：一律回伪装站自己的 404 页，
  跟访问网站上别的不存在的页面一模一样（仓库主人 10/23：grpc 做到了，其他协议也要这样）。
  以前：ws 直接断连接（444）、xhttp-tls 不带斜杠回 301 跳转、带斜杠回 xray 的 400——都跟别的路径不一样。
  会话里真 nginx + 真伪装站逐个路径比过响应内容（逐字节相同），所有节点照常连通。这里守生成的 nginx 配置。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
m.NGINX_WS[:] = [{"path": "/wsp", "port": 1}, {"path": "/xhp", "port": 2, "kind": "xhttp"}, {"path": "/grpcab", "port": 3, "kind": "grpc"}]
loc = m._nginx_ws_locations()
blk = lambda head: loc[loc.index(head):loc.index("}\n", loc.index(head))]
check("ws：不是 WebSocket 升级回 404，不再 444 断连接", 'if ($http_upgrade !~* "^websocket$") { return 404; }' in loc and "444" not in loc)
check("xhttp / grpc 不带斜杠的路径：精确匹配回 404（不让 nginx 自动 301）",
      "return 404;" in blk("location = /xhp {") and "return 404;" in blk("location = /grpcab {"))
xb = loc[loc.index("location /xhp/ {"):loc.index("location = /grpcab")]
check("xhttp：xray 回的错误码换成伪装站的 404 页", "grpc_intercept_errors on;" in xb and "error_page 400 403 404 405 =404 /404.html;" in xb)
gb = loc[loc.index("location /grpcab/ {"):]
check("grpc：不是 gRPC 请求回 404", 'if ($content_type !~ "application/grpc")' in gb and "grpc_intercept_errors" not in gb)
m.G.update({"domain": "d.example"})
srv = m._nginx_https_server("  listen 443 ssl;\n")
check("server 里 404 走伪装站自己的 404 页", "error_page 404 /404.html;" in srv)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

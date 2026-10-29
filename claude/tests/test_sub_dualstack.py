#!/usr/bin/env python3
"""订阅小服务同时听 IPv4 和 IPv6（仓库主人 10/29：DMIT 的 IPv4 TCP 被墙封了，itdog 测 IPv6 全国通，
  「能同时支持 IPv4 和 IPv6 最好」）。节点域名 A + AAAA 都留着，IPv4 被封时客户端走 IPv6 也拉得到订阅。
  以前只 bind 0.0.0.0，域名走 IPv6 过来订阅 / 规则中转全连不上。没有 IPv6 的机器退回只听 IPv4。
"""
import os, sys, socket, subprocess, tempfile, time, urllib.request, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

tmp = tempfile.mkdtemp()
srv = f"{tmp}/srv.py"; open(srv, "w").write(m._SUB_SERVER_PY)
d = f"{tmp}/sub"; os.makedirs(d)
open(f"{d}/index.html", "w").write("")
open(f"{d}/tok123.yaml", "w").write("mixed-port: 7890\n")

def run(port, extra_env=None):
    p = subprocess.Popen([sys.executable, srv, str(port), d, f"{tmp}/ghtok"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **(extra_env or {})})
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    return p

def get(host, port):
    try:
        with urllib.request.urlopen("http" + f"://{host}:{port}/tok123/VPS.yaml", timeout=3) as r:
            return r.status, r.headers.get("Server"), r.read().decode()
    except Exception as e:
        return str(e), None, ""

port = free_port()
p = run(port)
try:
    st, sv, body = get("127.0.0.1", port)
    check(f"IPv4 照样拉得到（{st}）", st == 200 and "mixed-port" in body)
    check("照旧报 Server: nginx", sv == "nginx")
    try:                                              # 测试机自己有没有 IPv6 回环
        t = socket.socket(socket.AF_INET6); t.bind(("::1", 0)); t.close(); v6 = True
    except OSError:
        v6 = False
    if v6:
        st6, _, body6 = get("[::1]", port)
        check(f"IPv6 也拉得到（{st6}）", st6 == 200 and "mixed-port" in body6)
    else:
        print("  - 这台测试机没有 IPv6，跳过 IPv6 实连")
    check("一个端口两个协议栈都占着：port_free 照样认得出被占", not m.port_free(port))
finally:
    p.terminate(); p.wait()

# 没有 IPv6：建 AF_INET6 套接字就失败 → 退回 0.0.0.0
code = m._SUB_SERVER_PY
check("机器没 IPv6 时退回只听 IPv4", "except OSError:" in code and "S(('0.0.0.0', port), H)" in code
      and code.index("S(('::', port), H)") < code.index("S(('0.0.0.0', port), H)"))
check("双栈：关掉 IPV6_V6ONLY", "IPV6_V6ONLY, 0" in code)
shim = f"{tmp}/nov6.py"
open(shim, "w").write("import socket\n_o = socket.socket.__init__\n"
                      "def _i(self, family=-1, *a, **k):\n"
                      "    if family == socket.AF_INET6: raise OSError(97, 'Address family not supported')\n"
                      "    _o(self, family, *a, **k)\n"
                      "socket.socket.__init__ = _i\n")
port2 = free_port()
p2 = subprocess.Popen([sys.executable, "-c", f"exec(open({shim!r}).read()); import sys; sys.argv={[srv, str(port2), d, tmp + '/ghtok']!r}; "
                       f"exec(compile(open({srv!r}).read(), {srv!r}, 'exec'))"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port2), 0.2).close(); break
        except OSError:
            time.sleep(0.1)
    st, _, _ = get("127.0.0.1", port2)
    check(f"内核关了 IPv6 的机器：照样起得来、IPv4 拉得到（{st}）", st == 200)
finally:
    p2.terminate(); p2.wait()

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

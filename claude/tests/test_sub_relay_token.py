#!/usr/bin/env python3
"""订阅小服务里的 GitHub 中转：没有 token 文件也不能放行（11/09 全仓检查查出来的）。
  以前 `if ftok and tok != ftok` —— token 文件不存在 / 是空的，就谁都能用：从没开过中转的机器（老装机、只有 IP、
  一直关着中转）扫到订阅端口的人拿 /随便/gh/github.com/… 就能让它替自己转 GitHub，也等于认出这儿跑着中转。
  现在：没 token、token 错、目标不是 GitHub，一律回跟普通不存在的地址一样的 404（不回 403，403 就是在说这儿有东西）；
  错误页照 nginx 的样子（标题「404 Not Found」，不是 http.server 的 File not found）。
"""
import os, sys, socket, subprocess, tempfile, time, urllib.request, urllib.error, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
d = f"{tmp}/sub"; os.makedirs(d)
open(f"{d}/index.html", "w").write("")
open(f"{d}/TokA.yaml", "w").write("a: 1\n")
open(f"{tmp}/srv.py", "w").write(m._SUB_SERVER_PY)
TOKF = f"{tmp}/gh.token"

def start():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    p = subprocess.Popen([sys.executable, f"{tmp}/srv.py", str(port), d, TOKF])
    return p, port
def get(port, path):
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)
        except OSError:
            time.sleep(0.1)
    return 0, "", {}

GH = "/gh/raw.githubusercontent.com/bgpeer/rules/main/README.md"
p, port = start()                                   # 没有 token 文件
try:
    code, body, hd = get(port, "/anything" + GH)
    check("没有 token 文件：中转不放行，404", code == 404)
    check("错误页像 nginx（404 Not Found / nginx，不是 File not found）",
          "<title>404 Not Found</title>" in body and "<center>nginx</center>" in body and "File not found" not in body)
    check("Server 头还是 nginx", hd.get("Server") == "nginx")
    open(TOKF, "w").write("")                       # 空文件也一样
    check("token 文件是空的：照样 404", get(port, "/" + GH)[0] == 404)
    open(TOKF, "w").write("goodtok\n")
    check("token 错：404（跟不存在的地址一样，不回 403）", get(port, "/badtok" + GH)[0] == 404)
    check("token 对、目标不是 GitHub：也是 404", get(port, "/goodtok/gh/example.com/x")[0] == 404)
    check("不存在的文件：同一张 404", get(port, "/nope.yaml")[1] == get(port, "/anything" + GH)[1])
    check("订阅照常能拉", get(port, "/TokA.yaml")[:2] == (200, "a: 1\n"))
finally:
    p.terminate()
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

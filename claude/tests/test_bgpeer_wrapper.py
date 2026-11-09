#!/usr/bin/env python3
"""bgpeer 快捷命令拉最新脚本：先验过再换上（11/09 全仓检查查出来的）。
  以前「下到非空就覆盖」：raw 被限流退到 jsDelivr，@main 缓存最长 12 小时，可能拿到旧版 —— 刚「18 更新」的新版
  下次敲 bgpeer 又被盖回旧版；下到一半断了也算非空，换上半截脚本，bgpeer 打不开、02:50 自更新也跑不起来。
  现在跟 ensure_remote_script 一个规矩：编译得过、版本号不比本机旧才换。这里真跑一遍生成出来的 bash：
  假 curl 吐指定内容，看 /etc/bgpeer/xy-installer.py（换成临时目录）最后是哪份、跑的是哪份。
"""
import os, sys, stat, tempfile, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
BIN = os.path.join(tmp, "bin"); os.makedirs(BIN)
LOCAL = os.path.join(tmp, "xy-installer.py")
SRC = os.path.join(tmp, "remote.py")           # 假 curl 吐的内容；不存在 = 下载失败
wrapper = m.BGPEER_WRAPPER.replace("/etc/bgpeer/xy-installer.py", LOCAL)
W = os.path.join(tmp, "bgpeer"); open(W, "w").write(wrapper); os.chmod(W, 0o755)
# 假 curl：把 -o 后面那个文件写成 SRC 的内容；SRC 不在就失败（模拟 429 / 断网）
open(os.path.join(BIN, "curl"), "w").write(
    '#!/usr/bin/env bash\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
    f'[ -f "{SRC}" ] || exit 22\ncat "{SRC}" > "$out"\n')
os.chmod(os.path.join(BIN, "curl"), 0o755)

def body(ver, tag):
    return f'SCRIPT_VERSION = "{ver}"\nprint("RUN {tag}")\n'
def go(local, remote):
    open(LOCAL, "w").write(local)
    if remote is None:
        if os.path.exists(SRC): os.remove(SRC)
    else:
        open(SRC, "w").write(remote)
    env = dict(os.environ, PATH=BIN + ":" + os.environ["PATH"])
    r = subprocess.run(["bash", W, "x"], capture_output=True, text=True, env=env, timeout=60)
    return open(LOCAL).read(), r.stdout.strip()

disk, out = go(body("1.1.200", "old"), body("1.1.211", "new"))
check("远端更新：换上新版、跑的是新版", "1.1.211" in disk and out == "RUN new")
disk, out = go(body("1.1.211", "cur"), body("1.1.200", "stale"))
check("镜像缓存的旧版：不降级，跑本机那份", "1.1.211" in disk and out == "RUN cur")
disk, out = go(body("1.1.211", "cur"), body("1.1.211", "same"))
check("同一个版本：换上也行（内容一样）", out in ("RUN cur", "RUN same"))
disk, out = go(body("1.1.211", "cur"), 'SCRIPT_VERSION = "1.1.212"\nprint("RUN half\n')
check("下到半截（编译不过）：不换，跑本机那份", "1.1.211" in disk and out == "RUN cur")
disk, out = go(body("1.1.211", "cur"), "<html>429 Too Many Requests</html>\n")
check("拿到的是网页：不换", "1.1.211" in disk and out == "RUN cur")
disk, out = go(body("1.1.211", "cur"), None)
check("下载失败：照跑本机那份", out == "RUN cur")
disk, out = go('print("RUN ancient")\n', body("1.1.211", "new"))
check("本机老到没有版本号：换上新版", "1.1.211" in disk and out == "RUN new")
disk, out = go(body("1.1.211", "cur"), 'print("RUN nover")\n')
check("远端没版本号：不换", "1.1.211" in disk and out == "RUN cur")
check("wrapper 里有超时（网络卡住不会一直挂着）", "-m 60" in m.BGPEER_WRAPPER)

# install_shortcut 写本机副本和 wrapper 都是先写临时文件再换名
src = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
seg = src[src.index("def install_shortcut"):src.index("BGPEER_WRAPPER = ")]
check("install_shortcut 两处都原子写", seg.count("_write_atomic_text(") == 2 and 'open(SELF_LOCAL, "w")' not in seg)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""net-optimize 4.5.24：nginx.org 还没出包的新系统借最近长期支持版的主线包（仓库主人 10/29：「主线版源现在没办法安排上吗？」）。
  GCP 上 Ubuntu 26.10（stonking）：以前照写 .../ubuntu stonking，nginx.org 没这个目录，apt update 每次报错，退回装系统的 1.30.4；
  手动换成 noble 实测装上 1.31.8、nginx -t 通过。现在：先探 dists/<代号>/Release，没有就借 noble / jammy，模拟安装能装才留；
  都探不到（连不上 nginx.org）不动现有的源。另外换源时 dpkg 停下来问 nginx.conf 要不要换新版（实测卡住），
  apt 一律 --force-confdef --force-confold 保留现有的（节点脚本改过的 443 分流那段）。
"""
import os, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)
check(f"版本号 4.5.24（{n.VERSION}）", n.VERSION == "4.5.24")
R = type("R", (), {})
def res(rc):
    r = R(); r.returncode = rc; r.stdout = r.stderr = ""; return r

BASE = "https://repo.example/packages/mainline/ubuntu"     # 只看路径拼得对不对，主机名随便
HAVE, ASKED = set(), []
def fake_sh(cmd, timeout=0):
    ASKED.append(cmd)
    return res(0 if any(f"/dists/{c}/Release" in cmd for c in HAVE) else 22)
n.sh = fake_sh
n.echo = lambda m="": None
HAVE.update({"noble", "jammy"})
check("系统自己的代号有包：照用", n.nginx_repo_codename(BASE, "noble", "ubuntu") == "noble")
check("26.10 stonking 没包：借 noble", n.nginx_repo_codename(BASE, "stonking", "ubuntu") == "noble")
HAVE.discard("noble")
check("noble 也没有：退到 jammy", n.nginx_repo_codename(BASE, "stonking", "ubuntu") == "jammy")
HAVE.clear()
check("都探不到（连不上 nginx.org）：返回空、不动现有的源", n.nginx_repo_codename(BASE, "stonking", "ubuntu") == "")
check("探测走 curl 头请求、带超时", all("curl -fsSI --max-time 8" in c for c in ASKED))
HAVE.update({"bookworm"})
check("Debian 同理（借 trixie / bookworm）", n.nginx_repo_codename("https://repo.example/packages/mainline/debian", "forky", "debian") == "bookworm")

CALLS = []
n.run = lambda cmd, timeout=0, env_extra=None: (CALLS.append((cmd, env_extra)), res(0 if "-s" not in cmd else SIM[0]))[1]
SIM = [0]
check("借来的能模拟装上：留", n.nginx_borrow_ok() is True)
check("模拟安装只是 -s、不真装", any(c[0][-3:] == ["install", "-s", "nginx"] for c in CALLS))
SIM[0] = 100
check("依赖对不上（老系统借新包）：不留", n.nginx_borrow_ok() is False)
cmd, env = CALLS[-1]
check(f"apt 一律保留改过的配置文件、不停下来问（{cmd[:5]}）",
      cmd[:5] == ["apt-get", "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
      and env == {"DEBIAN_FRONTEND": "noninteractive"})
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
blk = src[src.index("repo_cn = nginx_repo_codename("):src.index("# 3) ondrej/nginx PPA")]
check("借不上就把源和 Pin 撤掉、重新 update（不留坏源让 apt update 每次报错）",
      "_rm(NGINX_OFFICIAL_LIST); _rm(NGINX_PIN)" in blk and 'apt(["update", "-y"])' in blk)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

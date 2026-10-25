#!/usr/bin/env python3
"""net-optimize 自更新破 CDN 缓存 —— 真机踩过（谷歌机 net-optimize 卡在旧版）：
  auto_update 拉 net-optimize.py 和 SHA256SUMS 都不带 cache-bust，而 raw 常被限流(429)回落
  jsDelivr（@main 缓存约 12h、?_t= 破不掉）→ 自更新拿到旧副本、自以为已是最新，卡在旧版。
  主脚本 ensure_remote_script 一直带 ?_t=，这边漏了。修法：两个 URL 都加 ?_t= 破缓存，口径一致。
"""
import os, re, sys, hashlib, tempfile, io, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("netopt", os.path.join(ROOT, "net-optimize.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# 1. _bust：加时间戳，已有 ? 用 &
b1 = m._bust("https://raw.githubusercontent.com/o/r/main/net-optimize.py")
b2 = m._bust("p/q?a=1")       # 不写真 URL，_bust 只看有没有 ?
check("_bust 加 ?_t=", re.search(r"\?_t=\d+$", b1) is not None)
check("_bust 已有 ? 时用 &_t=", re.search(r"&_t=\d+$", b2) is not None)

# 2. auto_update 两个 URL 都走破缓存
urls = []
data = b"NEW-NET-OPTIMIZE-CONTENT"
def fake_fetch(u, timeout=10):
    urls.append(u)
    return b"deadbeef  net-optimize.py\n" if "SHA256SUMS" in u else data
m.fetch_url = fake_fetch
# 本地脚本内容跟远程不同 → 触发后续 SHA256SUMS 拉取；SHA 对不上 → 停在校验、不会 execv
tmp = tempfile.mkdtemp()
m.SCRIPT_PATH = f"{tmp}/net-optimize.py"
open(m.SCRIPT_PATH, "wb").write(b"OLD-CONTENT")
with contextlib.redirect_stdout(io.StringIO()):
    m.auto_update([])
raw_u = next((u for u in urls if "raw.githubusercontent" in u and "net-optimize.py" in u and "SHA256SUMS" not in u), "")
sums_u = next((u for u in urls if "SHA256SUMS" in u), "")
check("脚本 URL 带 ?_t= 破缓存", "_t=" in raw_u)
check("SHA256SUMS URL 带 ?_t= 破缓存", "_t=" in sums_u)

# 3. 远程与本地一致（都最新）→ 早退，不拉 SHA256SUMS、不写盘
urls.clear()
open(m.SCRIPT_PATH, "wb").write(data)          # 本地已是最新
with contextlib.redirect_stdout(io.StringIO()):
    m.auto_update([])
check("已是最新：只拉脚本一次就早退，不碰 SHA256SUMS", len(urls) == 1 and "SHA256SUMS" not in urls[0])
check("已是最新：本地脚本不动", open(m.SCRIPT_PATH, "rb").read() == data)

# 4. 只升不降：拉到的版本号 ≤ 正在跑的 → 早退，不碰 SHA256SUMS、不写 SCRIPT_PATH
m.VERSION = "4.5.13"
open(m.SCRIPT_PATH, "wb").write(b'VERSION = "4.5.13"\n')     # 本地（正在跑的）是新的
urls.clear()
def fetch_old(u, timeout=10):
    urls.append(u)
    return b'VERSION = "4.5.9"\nprint(1)\n'                  # 镜像拿到的是旧的
m.fetch_url = fetch_old
with contextlib.redirect_stdout(io.StringIO()):
    m.auto_update([])
check("镜像给旧版(4.5.9 ≤ 4.5.13)：早退，不拉 SHA256SUMS", len(urls) == 1 and "SHA256SUMS" not in urls[0])
check("镜像给旧版：SCRIPT_PATH 不被降级", b'4.5.13' in open(m.SCRIPT_PATH, "rb").read())

# 5. 真有新版（> 正在跑的）→ 继续走校验（会去拉 SHA256SUMS）
m.VERSION = "4.5.13"
open(m.SCRIPT_PATH, "wb").write(b'VERSION = "4.5.13"\n')
urls.clear()
def fetch_new(u, timeout=10):
    urls.append(u)
    return b"deadbeef  net-optimize.py\n" if "SHA256SUMS" in u else b'VERSION = "4.5.99"\nprint(1)\n'
m.fetch_url = fetch_new
with contextlib.redirect_stdout(io.StringIO()):
    m.auto_update([])
check("真有新版(4.5.99 > 4.5.13)：继续走到 SHA256SUMS 校验", any("SHA256SUMS" in u for u in urls))

# 6. 版本号 ≥ 4.5.13
src = open(os.path.join(ROOT, "net-optimize.py"), encoding="utf-8").read()
mv = tuple(int(x) for x in re.findall(r"\d+", re.search(r'VERSION = "([\d.]+)"', src).group(1)))
check("VERSION ≥ 4.5.13", mv >= (4, 5, 13))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

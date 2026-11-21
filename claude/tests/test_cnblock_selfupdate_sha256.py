#!/usr/bin/env python3
"""屏蔽中国域名 · 凌晨自更新先验 SHA256SUMS、只升不降（2026-11-19 全仓检查 (6)）。
  以前 _self_update_copy 只 compile() + 含 `def refresh` 就以 root 身份 os.execve：
  拉到被篡改 / 半截的照样执行。照抄 net-optimize.py auto_update：版本号只升不降，
  再拿 SHA256SUMS 校验，拿不到 / 里面没条目 / 对不上都直接跳过（fail-closed，宁可不更新）。
  注意：SHA256SUMS 目前只有 net-optimize.py 一条，发布端要在
  .github/workflows/sha256sums.yml 里改成 `sha256sum net-optimize.py cn-block.py > SHA256SUMS`
  （触发路径也加上 cn-block.py），补上条目之前这里每次都会跳过。
"""
import os, sys, hashlib, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("cb2", os.path.join(ROOT, "cn-block.py"))
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
SELF = f"{tmp}/cn-block.py"
OLD = b'VERSION = "1.0.0"\ndef refresh():\n    pass\n# OLD\n'
NEW = b'VERSION = "1.0.1"\ndef refresh():\n    pass\n# NEW\n'
cb.__file__ = SELF
cb.SELF_PATH = SELF
cb.SELFUPD_OFF = f"{tmp}/selfupdate.off"

def reset():
    open(SELF, "wb").write(OLD)

class Resp:
    def __init__(self, data): self._d = data
    def read(self): return self._d

calls = []
mode = {"script": "ok", "sums": "ok"}
def fake_open(req, timeout=None):
    u = req.full_url if hasattr(req, "full_url") else str(req)
    calls.append(u)
    if "SHA256SUMS" in u:
        if mode["sums"] == "fail":
            raise OSError("no sums")
        if mode["sums"] == "no-entry":
            return Resp(b"deadbeef  net-optimize.py\n")
        return Resp(hashlib.sha256(NEW).hexdigest().encode() + b"  cn-block.py\n")
    if mode["script"] == "fail":
        raise OSError("no net")
    return Resp(NEW if mode["script"] == "ok" else mode["script"])
cb.urllib.request.urlopen = fake_open

def run():
    calls.clear()
    return cb._self_update_copy()

print("一、版本号")
check("VERSION 在，xy-installer 的 _remote_ver 也认它", cb.VERSION == "1.0.0"
      and cb._remote_ver(OLD.decode()) == "1.0.0")
check("只认 raw 的 SHA256SUMS", cb.SELF_SHA256SUMS_URL.startswith("https://raw.githubusercontent.com/"))

print("二、换不换")
reset(); mode.update(script="ok", sums="ok")
check("新版 + 校验通过：换上", run() is True and open(SELF, "rb").read() == NEW)
check("只发两次请求（脚本 + SHA256SUMS）", len(calls) == 2 and "SHA256SUMS" in calls[1])
reset(); mode.update(script="ok", sums="ok")
cb.urllib.request.urlopen = lambda req, timeout=None: Resp(b"") if "SHA256SUMS" in (
    req.full_url if hasattr(req, "full_url") else str(req)) else Resp(NEW)
check("SHA256SUMS 是空的（没条目）：不换", run() is False and open(SELF, "rb").read() == OLD)
cb.urllib.request.urlopen = fake_open
reset()
# 哈希是按新版算的，改一个字节就对不上
tampered = NEW.replace(b"# NEW", b"# TAMPER")
mode["script"] = tampered
check("新版被改了一个字节：SHA256 对不上，不换", run() is False and open(SELF, "rb").read() == OLD)
mode["script"] = "ok"
reset(); mode.update(script="ok", sums="fail")
check("SHA256SUMS 拿不到：不换", run() is False and open(SELF, "rb").read() == OLD)
reset(); mode.update(script="ok", sums="no-entry")
check("SHA256SUMS 里没有 cn-block.py 条目：不换", run() is False and open(SELF, "rb").read() == OLD)
reset(); mode.update(script=OLD, sums="ok")
check("拉到的是本机同一版：不动，也不去拉 SHA256SUMS", run() is False and open(SELF, "rb").read() == OLD
      and not any("SHA256SUMS" in u for u in calls))
reset(); mode.update(script=OLD.replace(b'1.0.0', b'0.9.9'), sums="ok")
check("拉到的是更旧版（镜像缓存）：不降级", run() is False and open(SELF, "rb").read() == OLD
      and not any("SHA256SUMS" in u for u in calls))
reset(); mode.update(script="fail", sums="ok")
check("脚本拉不到：不动", run() is False and open(SELF, "rb").read() == OLD and len(calls) == 1)
reset()
broken = b'VERSION = "1.0.2"\ndef broken(:\n'
mode.update(script=broken, sums="ok")
cb.urllib.request.urlopen = lambda req, timeout=None: Resp(
    hashlib.sha256(broken).hexdigest().encode() + b"  cn-block.py\n") if "SHA256SUMS" in (
    req.full_url if hasattr(req, "full_url") else str(req)) else Resp(broken)
check("新版语法坏（哈希对得上也一样）：不换", run() is False and open(SELF, "rb").read() == OLD)
cb.urllib.request.urlopen = fake_open
reset()
norefresh = b'VERSION = "1.0.2"\n# no refresh here\n'
mode.update(script=norefresh, sums="ok")
cb.urllib.request.urlopen = lambda req, timeout=None: Resp(
    hashlib.sha256(norefresh).hexdigest().encode() + b"  cn-block.py\n") if "SHA256SUMS" in (
    req.full_url if hasattr(req, "full_url") else str(req)) else Resp(norefresh)
check("新版里没有 def refresh：不换", run() is False and open(SELF, "rb").read() == OLD)
cb.urllib.request.urlopen = fake_open

print("三、不换的开关")
reset(); mode.update(script="ok", sums="ok")
open(cb.SELFUPD_OFF, "w").close()
check("菜单 19 关了脚本自动更新：不换、也不发请求", run() is False and calls == [])
os.remove(cb.SELFUPD_OFF)
os.environ["CNBLK_FRESH"] = "1"
check("已经是换上的新版在跑：不再换（不会循环）", run() is False and calls == [])
del os.environ["CNBLK_FRESH"]

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

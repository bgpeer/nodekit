#!/usr/bin/env python3
"""自建 DNS · 「2 使用方法」第二步（设置加密）按后台页面从上到下排（仓库主人 11/05 给的顺序），
  加一行 DNS-over-QUIC 端口 853；端口分行写，每项一行。
"""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "adguard-dns.py"), encoding="utf-8").read()
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
blk = src[src.index("【第二步 · 设置加密】"):src.index("【第三步 · DNS 设置】")]
want = ["勾选「启用加密」", "服务器名称", "「HTTPS 自动重定向」", "HTTPS 端口", "DNS-over-TLS 端口",
        "DNS-over-QUIC 端口", "证书（文件路径）", "私钥（文件路径）", "保存（提示 no IP addresses"]
pos = [blk.find(w) for w in want]
check("九项都在", all(p >= 0 for p in pos))
check("顺序照仓库主人给的", pos == sorted(pos))
check("DoQ 填 853", re.search(r"DNS-over-QUIC 端口：\{G\}853", blk) is not None)
check("HTTPS 和 DoT 端口不再挤一行", not re.search(r"HTTPS 端口.*DNS-over-TLS", blk))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

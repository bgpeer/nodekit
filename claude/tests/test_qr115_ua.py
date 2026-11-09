#!/usr/bin/env python3
"""115 扫码的令牌 / 状态请求报的是 Python-urllib（11/09 全仓检查查出来的）：
  _qr115_get 以前直接 urlopen(网址)，不带 User-Agent。CLAUDE.md：出网一律中性 HTTP_UA。
  同一个域的二维码图片本来就带 HTTP_UA、115 认，令牌 / 状态两条照它补上。
"""
import io, os, sys, urllib.request, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ms", os.path.join(ROOT, "media-stack.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

sent = []
def fake(req, timeout=None):
    sent.append(req)
    return io.BytesIO(b'{"data": {"uid": "u", "time": 1, "sign": "s", "status": 0}}')
m.urllib.request.urlopen = fake

d = m._qr115_get(m.QR115_TOKEN)
check("令牌：照常解析出 data", d.get("data", {}).get("uid") == "u")
st, why = m.qr115_status("u", 1, "s")
check("状态：照常解析", st == 0 and why)
check("两条都发了", len(sent) == 2)
for r in sent:
    host = r.host if isinstance(r, urllib.request.Request) else "?"
    ua = r.get_header("User-agent") if isinstance(r, urllib.request.Request) else None
    check(f"{host}：带中性 UA（{ua}）", ua == m.HTTP_UA)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

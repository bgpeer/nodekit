#!/usr/bin/env python3
"""Claude 专属的两个文件夹（claude/、.claude/）自己守规矩：
  一、诊断工具都能编译、开头写了用法、只读（不出现写文件 / 删文件 / 重启这类动作）
  二、两个文件夹里不许有安装人信息：邮箱、IP（127.0.0.1 除外）、白名单以外的网址
  三、技能 / 智能体文件头齐全（Claude Code 认得出来）
"""
import os, py_compile, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

print("一、诊断工具")
tools = os.path.join(ROOT, "claude", "tools")
for f in sorted(x for x in os.listdir(tools) if x.endswith(".py")):
    p = os.path.join(tools, f); src = open(p, encoding="utf-8").read()
    try:
        py_compile.compile(p, doraise=True); comp = True
    except py_compile.PyCompileError:
        comp = False
    check(f"{f} 能编译、写了用法", comp and "用法" in src)
    check(f"{f} 只读", not re.search(r"os\.remove|rmtree|write_atomic|open\([^)]*['\"][wa]['\"]|docker (restart|rm|stop)|"
                                     r"method=\"(POST|DELETE)\"|save_ms_state", src))
    check(f"{f} 写进了 README", f in open(os.path.join(tools, "README.md"), encoding="utf-8").read())

cd = open(os.path.join(tools, "coverdiag.py"), encoding="utf-8").read()
check("coverdiag 不印 OpenList 的主机名（那就是安装人的域名，10/17 真机印出来过）", "你的 OpenList" in cd)

print("二、没有安装人信息")
OK_HOSTS = ("raw.githubusercontent.com", "github.com", "auth.xiaoya.pro", "openapi.aliyundrive.com", "www.aliyundrive.com",
            "qrcodeapi.115.com", "drive-pc.quark.cn", "uop.quark.cn", "pan.quark.cn", "su.quark.cn", "xiaoya.host",
            "list.example", "cdnfhnfile.115cdn.net", "bj29.api.aliyunpds.com", "127.0.0.1", "h", "tv")
leaks = []
for top in ("claude", ".claude"):
    for dp, _dn, fs in os.walk(os.path.join(ROOT, top)):
        if "__pycache__" in dp:
            continue
        for f in fs:
            p = os.path.join(dp, f)
            try:
                t = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            rel = os.path.relpath(p, ROOT)
            for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", t):
                if not e.endswith(("users.noreply.github.com", "anthropic.com")):
                    leaks.append(f"{rel}: 邮箱 {e}")
            for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", t):
                if ip != "127.0.0.1" and not ip.startswith("0.") and not re.match(r"\d\.\d+\.\d+\.\d+$", ip):
                    leaks.append(f"{rel}: IP {ip}")
            for h in re.findall(r"https?://([^/\s\"'<>`)\\]+)", t):
                h = h.split(":")[0]
                if h and h not in OK_HOSTS and not h.endswith(".example"):
                    leaks.append(f"{rel}: 网址 {h}")
check("两个文件夹里没有邮箱 / IP / 白名单以外的网址" + (f"：{leaks[:5]}" if leaks else ""), not leaks)

print("三、技能 / 智能体")
for p in ([os.path.join(ROOT, ".claude", "skills", s, "SKILL.md") for s in os.listdir(os.path.join(ROOT, ".claude", "skills"))]
          + [os.path.join(ROOT, ".claude", "agents", a) for a in os.listdir(os.path.join(ROOT, ".claude", "agents"))]):
    t = open(p, encoding="utf-8").read()
    check(f"{os.path.relpath(p, ROOT)} 文件头有 name / description",
          t.startswith("---\n") and re.search(r"^name: \S+", t, re.M) and re.search(r"^description: .+", t, re.M))
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""CI 工作流三处（E 组）：
  (a) release.yml 触发路径：五个脚本 + 三个订阅模板（docs/18-update-script.md 说
      「脚本/模板有改动合并进 main 就算发布」——原来只覆盖 4 个 .py，漏了
      media-stack.py 和模板）。library-rules.yaml 是规则文件不是模板、
      whitelist-template.py 只是给人抄走的样板，都不触发。
  (c) sha256sums.yml：不再用 git rebase 回推，改成跟 release.yml 一样每轮站到
      最新 main 上重新生成、重新提交；并且跟 release.yml 共用同一个并发组，
      两个工作流在 net-optimize.py 推上 main 时不再抢 push。
  (d) docs/18-update-script.md 的版本例子：跟上当前版本。
      这里只断「不低于这次修到的版本」，不断相等 —— CI 每次发版都会把
      SCRIPT_VERSION 自动 +1 而不改文档，断相等下次发版就红；断下限只防
      例子又悄悄变回旧版本。
"""
import os, re, sys
import yaml
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

rel_p = os.path.join(ROOT, ".github", "workflows", "release.yml")
sha_p = os.path.join(ROOT, ".github", "workflows", "sha256sums.yml")
rel = yaml.safe_load(open(rel_p, encoding="utf-8"))
sha = yaml.safe_load(open(sha_p, encoding="utf-8"))
check("release.yml / sha256sums.yml 都是合法 YAML", isinstance(rel, dict) and isinstance(sha, dict))

# PyYAML 按 YAML 1.1 把 on: 读成 True，所以两个键都试。
paths = ((rel.get("on") or rel.get(True) or {}).get("push", {}).get("paths", []))
want = {"xy-installer.py", "net-optimize.py", "cn-block.py", "adguard-dns.py",
        "media-stack.py", "sub-template.yaml", "subbox-template.json",
        "shadowrocket-template.conf"}
check(f"release.yml 触发路径含 5 脚本 + 3 模板（{sorted(set(paths))}）", want <= set(paths))

raw_sha = open(sha_p, encoding="utf-8").read()
check("sha256sums.yml 里不用 git rebase 回推（注释里提一句为什么不用不算）", "git rebase" not in raw_sha)
check("sha256sums.yml 每轮站到最新 main 上重算（reset --hard + 重生成）",
      "git reset --hard origin/main" in raw_sha and "sha256sum net-optimize.py cn-block.py > SHA256SUMS" in raw_sha)
g_rel = (rel.get("concurrency") or {}).get("group")
g_sha = (sha.get("concurrency") or {}).get("group")
check(f"两个工作流共用同一个并发组（{g_rel}），串行跑不抢 push",
      g_rel and g_rel == g_sha)
check("sha256sums.yml 不取消进行中的那轮（跟 release.yml 一致）",
      (sha.get("concurrency") or {}).get("cancel-in-progress") is False)

doc = open(os.path.join(ROOT, "docs", "18-update-script.md"), encoding="utf-8").read()
m = re.search(r"当前版本：v(\d+)\.(\d+)\.(\d+)", doc)
ver = tuple(map(int, m.groups())) if m else (0, 0, 0)
check(f"docs/18-update-script.md 的版本例子是 v{'.'.join(map(str, ver))}，不低于 v1.1.229",
      ver >= (1, 1, 229))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

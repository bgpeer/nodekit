#!/usr/bin/env python3
"""屏蔽中国域名 · 单条放行 domain_suffix 的点边界语义（2026-11-19 全仓检查存疑项）。
  结论：不是问题——对过 sing-box 源码才定。
  cn-block 的单条放行是 inline rule_set 里的 `{"domain_suffix": [...]}`
  （apply_cn_block 里那条 headless 规则）。sing-box 1.11 / 1.12 / 1.14 / main
  的 route/rule/rule_item_domain.go 都是 `domain.NewMatcher(domains, suffixes, false)`，
  对应 sing 里 common/domain/matcher.go 的 generateLegacy=false：后缀按「根标签」记，
  命中要求点边界——`example.com` 只命中自身和子域，`badexample.com` 不命中。
  generateLegacy=true 那条路 sing-box 自己全仓库只有一个调用点、传的 false，没调过。
  （也拿 matcher 的逻辑在本地对过一遍：裸域名、子域命中，无点边界、example.com.evil.net、
  example.comm 都不命中；带点后缀 `.example.com` 反而丢了裸域名本身。）
  上游自己的 matcher 测试也没覆盖无点边界用例，所以这里把写法钉死：裸域名进
  domain_suffix，不带点、不用 domain_keyword / domain_regex，免得以后有人顺手改了语义。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("cb", os.path.join(ROOT, "cn-block.py"))
cb = importlib.util.module_from_spec(spec); spec.loader.exec_module(cb)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
tmp = tempfile.mkdtemp()
cb.SB_DIR = f"{tmp}/sb"; os.makedirs(cb.SB_DIR)
cb.CNBLOCK_FILE = f"{tmp}/cnblock.json"; cb.BGP_DIR = tmp
cb.CRON_FILE = f"{tmp}/cron"
SB_CFG = f"{cb.SB_DIR}/config.json"
json.dump({"enabled": True, "wl_mode": "none", "wl_domains": ["example.com", "sub.example.org"], "wl_ips": []},
          open(cb.CNBLOCK_FILE, "w"))
json.dump({"outbounds": [{"type": "direct", "tag": "direct"}],
           "route": {"rules": [], "rule_set": []}}, open(SB_CFG, "w"))
cb._rule_url = lambda rel: "https://r.example/" + rel
cb.subprocess = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))
cb.time.sleep = lambda s: None
SH = []
cb.sh = lambda c, check=True: SH.append(c) or ("active" if "is-active" in c else "")
cb.setup_cron = lambda: None

print("一、生成的 inline 规则集")
with contextlib.redirect_stdout(io.StringIO()):
    ok = cb.apply_cn_block()
conf = json.load(open(SB_CFG))
rs = [r for r in conf["route"]["rule_set"] if r.get("tag") == "cnblk-wl-custom"]
check("注入成功、inline 规则集在", ok is True and len(rs) == 1 and rs[0]["type"] == "inline")
rules = rs[0]["rules"] if rs else []
check("域名进 domain_suffix：裸域名、不带点", rules == [{"domain_suffix": ["example.com", "sub.example.org"]}])
check("不用 domain / domain_keyword / domain_regex（语义不一样）",
      not any(k in r for r in rules for k in ("domain", "domain_keyword", "domain_regex")))
head = [r for r in conf["route"]["rules"] if r.get("rule_set") == "cnblk-wl-custom"]
check("放行规则在拦截规则前面、走 direct",
      len(head) == 1 and head[0]["outbound"] == "direct"
      and conf["route"]["rules"].index(head[0]) < conf["route"]["rules"].index(
          next(r for r in conf["route"]["rules"] if r.get("rule_set") == "cnblk-cn-site")))

print("二、写法钉死在代码注释里")
src = open(os.path.join(ROOT, "cn-block.py"), encoding="utf-8").read()
i = src.index('hr.append({"domain_suffix": wl_doms})')
near = src[max(0, i - 1200):i]
check("注释写明点边界语义、核过的版本、别加「.」别换 domain",
      "点边界" in near and "1.12" in near and "1.14" in near and "generateLegacy" in near
      and "别在前面加" in near and "别换成 domain" in near)

print("三、文档的说法跟写法一致")
doc = open(os.path.join(ROOT, "docs", "10-cn-block.md"), encoding="utf-8").read()
check("文档：填 example.com，子域都放行；notexample.com 不会误命中",
      "和它的所有子域" in doc and "notexample.com" in doc and "不会" in doc)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

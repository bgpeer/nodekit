#!/usr/bin/env python3
"""屏蔽中国域名 · 卸载先过 sing-box check、重启后确认起来、彻底卸载前先明确确认
  （2026-11-19 全仓检查 (11)）。
  以前 remove_cn_block() 写完配置直接重启：写坏了（比如 route 被清空）也不知道，
  节点跟着挂；命令行 remove 一敲就删，连确认都没有。现在写完先 `sing-box check`，
  重启后确认真的起来了，不过就把原配置放回、状态和定时任务都不动；彻底卸载前先问
  一句（默认否，问不到人也当作否），菜单 6 自己问过、命令行 `remove --yes` 不再问。
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
CONF = {"outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct",
                  "rule_set": [{"tag": "cnblk-cn-site", "type": "remote", "url": "u"}, {"tag": "mine", "type": "inline"}],
                  "rules": [{"rule_set": "cnblk-cn-site", "action": "reject"}, {"rule_set": "mine", "outbound": "direct"}]}}
ORIG_CONF = json.dumps(CONF, sort_keys=True).encode()
ANS = []
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"
cb.time.sleep = lambda s: None
SH = []
st = {"restarts": 0, "active_after": 1}
def fake_sh(c, check=True):
    SH.append(c)
    if c.startswith("systemctl restart sing-box"):
        st["restarts"] += 1
        return ""
    if "is-active" in c:
        return "active" if st["restarts"] >= st["active_after"] else ""
    return ""
cb.sh = fake_sh
CHECK = [True]
REAL_CHECK = cb._sb_check                 # 真的 _sb_check：第一节直接测它，后面再换成桩
cb._sb_check = lambda cfg: CHECK[0]

def seed():
    open(SB_CFG, "wb").write(ORIG_CONF)
    json.dump({"enabled": True, "wl_mode": "author"}, open(cb.CNBLOCK_FILE, "w"))
    open(cb.CRON_FILE, "w").write("x")
    SH.clear(); st["restarts"] = 0

print("一、_sb_check 本身")
class FakeRun:
    def __init__(self, rc, err=""): self.returncode = rc; self.stderr = err; self.stdout = ""
real_sub = cb.subprocess
cb.subprocess = SimpleNamespace(run=lambda *a, **k: FakeRun(0))
check("sing-box check 通过", REAL_CHECK(SB_CFG) is True)
cb.subprocess = SimpleNamespace(run=lambda *a, **k: FakeRun(1, "bad config"))
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = REAL_CHECK(SB_CFG)
check("不通过：返回 False、印出原因", ok is False and "bad config" in b.getvalue())
cb.subprocess = real_sub

print("二、check 不过：原配置、状态、定时任务全不动")
seed(); CHECK[0] = False
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = cb.remove_cn_block(confirm=False)
check("返回 False", ok is False)
check("配置内容没动（cnblk 规则还在），也没重启",
      json.load(open(SB_CFG)) == CONF and st["restarts"] == 0)
check("状态、定时任务还在", os.path.exists(cb.CNBLOCK_FILE) and os.path.exists(cb.CRON_FILE))

print("三、check 过、重启起来：正常卸掉")
seed(); CHECK[0] = True; st["active_after"] = 1
ok = cb.remove_cn_block(confirm=False)
conf = json.load(open(SB_CFG))
check("返回 True、cnblk 规则撤掉、别人的规则 / final 不动",
      ok is True and [r["tag"] for r in conf["route"]["rule_set"]] == ["mine"]
      and conf["route"]["rules"] == [{"rule_set": "mine", "outbound": "direct"}]
      and conf["route"]["final"] == "direct")
check("状态文件删掉、定时任务撤掉", not os.path.exists(cb.CNBLOCK_FILE) and not os.path.exists(cb.CRON_FILE))
check("重启一次并确认起来", st["restarts"] == 1 and "systemctl is-active sing-box" in SH)

print("四、check 过、重启起不来：原配置放回，状态、定时任务不动")
seed(); CHECK[0] = True; st["active_after"] = 99
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = cb.remove_cn_block(confirm=False)
check("返回 False、报恢复原配置", ok is False and "已恢复原配置" in b.getvalue())
check("配置按原样放回（cnblk 规则还在）", json.load(open(SB_CFG)) == CONF)
check("状态、定时任务还在", os.path.exists(cb.CNBLOCK_FILE) and os.path.exists(cb.CRON_FILE))
check("又重启了一次（恢复后）", st["restarts"] == 2)

print("五、彻底卸载前先明确确认，默认否")
seed(); CHECK[0] = True; st["active_after"] = 1
ANS[:] = [""]
b = io.StringIO()
with contextlib.redirect_stdout(b):
    ok = cb.remove_cn_block()
check("回车 = 不卸：返回 False、什么都没动",
      ok is False and "已取消" in b.getvalue() and open(SB_CFG, "rb").read() == ORIG_CONF
      and os.path.exists(cb.CNBLOCK_FILE) and st["restarts"] == 0)
ANS[:] = []
check("没人答（_ask 兜底回 0）也一样不卸", cb.remove_cn_block() is False
      and os.path.exists(cb.CNBLOCK_FILE) and st["restarts"] == 0)
cb._ask = lambda p="": (_ for _ in ()).throw(EOFError)
check("问不到人（没终端）也当作否", cb.remove_cn_block() is False and os.path.exists(cb.CNBLOCK_FILE))
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"
ANS[:] = ["y"]
check("答 y：照常卸掉", cb.remove_cn_block() is True and not os.path.exists(cb.CNBLOCK_FILE))

print("六、只关（菜单 1）：不问确认，名单状态留着")
seed(); CHECK[0] = True; st["active_after"] = 1
asked = []
cb._ask = lambda p="": asked.append(p) or "0"
ok = cb.remove_cn_block(keep_cfg=True)
d = json.load(open(cb.CNBLOCK_FILE))
check("返回 True、没问确认", ok is True and asked == [])
check("状态文件还在：关着", d["enabled"] is False)
check("规则撤掉、定时任务撤掉", [r["tag"] for r in json.load(open(SB_CFG))["route"]["rule_set"]] == ["mine"]
      and not os.path.exists(cb.CRON_FILE))
cb._ask = lambda p="": (print(p) or ANS.pop(0)) if ANS else "0"

print("七、没有配置文件：照旧只清状态和定时任务")
seed(); os.remove(SB_CFG)
check("返回 True", cb.remove_cn_block(confirm=False) is True
      and not os.path.exists(cb.CNBLOCK_FILE) and not os.path.exists(cb.CRON_FILE))

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

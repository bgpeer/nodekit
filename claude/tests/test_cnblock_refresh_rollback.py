#!/usr/bin/env python3
"""屏蔽中国域名 · 凌晨刷新回滚：缓存 + 配置一起备、一起放回（2026-11-19 全仓检查 (11)）。
  以前 refresh() 规则集变了那一轮只备 cache.db：重启起不来时只回滚缓存，配置丢了没法放回。
  现在重启前把 config.json 也备下来（放内存，不落 .bak 文件），起不来就两个一起放回、
  再确认 sing-box 真的起来了；回滚后还没起来也要报出来。rs_hash 只有新规则集真生效才记。
"""
import os, io, sys, json, tempfile, contextlib, importlib.util
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
CACHE = f"{tmp}/cache.db"
CONF = {"outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"final": "direct",
                  "rule_set": [{"tag": "cnblk-cn-site", "type": "remote", "url": "u"}, {"tag": "mine", "type": "inline"}],
                  "rules": [{"rule_set": "cnblk-cn-site", "action": "reject"}, {"rule_set": "mine", "outbound": "direct"}]},
        "experimental": {"cache_file": {"enabled": True, "path": CACHE}}}
ORIG_CONF = json.dumps(CONF, sort_keys=True).encode()

def seed(rs_hash="OLD"):
    open(SB_CFG, "wb").write(ORIG_CONF)          # 原样写死，后面按字节比才有意义
    open(CACHE, "wb").write(b"OLD-CACHE")
    json.dump({"enabled": True, "wl_sig": "A", "rs_hash": {"cnblk-cn-ip": rs_hash}}, open(cb.CNBLOCK_FILE, "w"))

cb._self_update_copy = lambda: False
cb.setup_cron = lambda: None
cb._wl_sig = lambda cfg: "A"
cb._cnblk_remote_sets = lambda: {"cnblk-cn-ip": "u"}
cb.time.sleep = lambda s: None
SH = []
st = {"restarts": 0, "active_after": 1, "trash_config": False}
def fake_sh(c, check=True):
    SH.append(c)
    if c.startswith("systemctl restart sing-box"):
        st["restarts"] += 1
        if st["trash_config"] and st["restarts"] == 1:
            open(SB_CFG, "wb").write(b"GARBAGE")     # 模拟：没起来的那次重启把配置也弄丢了
        return ""
    if "is-active" in c:
        return "active" if st["restarts"] >= st["active_after"] else ""
    return ""
cb.sh = fake_sh
NEW_HASH = {"cnblk-cn-ip": "NEW"}
cb._rs_hashes = lambda s: NEW_HASH

def night():
    SH.clear(); st["restarts"] = 0
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        cb.refresh()
    return b.getvalue()

print("一、规则集变了：正常刷新")
seed(); st.update(active_after=1, trash_config=False)
out = night()
check("重启一次就起来：新指纹记下，配置一个字节没动，旧缓存备份清掉",
      st["restarts"] == 1 and json.load(open(cb.CNBLOCK_FILE))["rs_hash"] == NEW_HASH
      and json.dumps(json.load(open(SB_CFG)), sort_keys=True).encode() == ORIG_CONF
      and not os.path.exists(CACHE + ".bak") and "已刷新" in out)

print("二、重启起不来：缓存 + 配置一起回滚")
seed(); st.update(active_after=2, trash_config=True)
out = night()
check("回滚后起来了：报「已回滚缓存 + 配置」",
      st["restarts"] == 2 and "已回滚缓存 + 配置" in out)
check("配置按原样放回（不是垃圾）", open(SB_CFG, "rb").read() == ORIG_CONF)
check("缓存也按原样放回", open(CACHE, "rb").read() == b"OLD-CACHE"
      and not os.path.exists(CACHE + ".bak"))
check("新规则集没生效：指纹还是旧的，下次还会再试",
      json.load(open(cb.CNBLOCK_FILE))["rs_hash"] == {"cnblk-cn-ip": "OLD"})

print("三、回滚后仍起不来：照样报出来，东西还是放回了")
seed(); st.update(active_after=99, trash_config=True)
out = night()
check("报「回滚后仍未启动」", "回滚后仍未启动" in out and st["restarts"] == 2)
check("配置、缓存还是放回了，指纹没记新的",
      open(SB_CFG, "rb").read() == ORIG_CONF and open(CACHE, "rb").read() == b"OLD-CACHE"
      and json.load(open(cb.CNBLOCK_FILE))["rs_hash"] == {"cnblk-cn-ip": "OLD"})

print("四、没变化：什么都不动")
seed(); st.update(active_after=1, trash_config=False)
cb._rs_hashes = lambda s: {"cnblk-cn-ip": "OLD"}
out = night()
check("没重启、配置缓存都没动", st["restarts"] == 0 and open(SB_CFG, "rb").read() == ORIG_CONF
      and open(CACHE, "rb").read() == b"OLD-CACHE" and "没有变化" in out)

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

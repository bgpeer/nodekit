#!/usr/bin/env python3
"""安装回滚（问题 5）：新装 / 重装中途失败，不留半成品节点。

  以前 run() 里 sing-box 配置先被 json.dump 截断、后 core_check 校验，
  xray 阶段一出错就留下 sing-box 跑着新 UUID/新端口、但没有 state.json/订阅。
  修法：配置先写临时文件、校验通过才原子替换（_write_core_cfg）；
  run() 安装前快照两核心配置 + state.json + 链接 + nginx 配置，
  任一步失败整体回滚（_rollback_install）。
"""
import os, sys, json, tempfile, importlib.util
from types import SimpleNamespace
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
SB, XR = f"{tmp}/sb", f"{tmp}/xray"
os.makedirs(SB); os.makedirs(XR)
m.SB_DIR, m.XRAY_DIR = SB, XR
m.STATE_FILE = f"{tmp}/state.json"
m.NODE_FILE = f"{tmp}/nodes.txt"
m.NGINX_CONF = f"{tmp}/bgpeer.conf"
m.NGINX_STREAM_CONF = f"{tmp}/bgpeer-stream.conf"
m.SUBPORT_FILE = f"{tmp}/sub.port"
open(m.SUBPORT_FILE, "w").write("23456")

# ---- 假的 systemctl / subprocess：只记录，不真动服务 ----
calls = []
m.sh = lambda cmd, check=True: (calls.append(cmd), "")[1]
class FakeSP:
    def __init__(self, active):
        self.active = active
    def run(self, argv, **kw):
        return SimpleNamespace(returncode=0 if argv[-1] in self.active else 3)
m._hy2_hop_heal = lambda: False
m.nginx_reload = lambda: calls.append("nginx_reload")
m.core_check = lambda binpath, cfg: (True, "")

# ---- 1. _write_core_cfg：校验不过，旧配置一个字节不动 ----
old_cfg = {"inbounds": [{"tag": "old", "listen_port": 20001}]}
cfg_path = f"{SB}/config.json"
json.dump(old_cfg, open(cfg_path, "w"))
m.core_check = lambda binpath, cfg: (False, "boom")
try:
    m._write_core_cfg("sing-box", "fake-bin", cfg_path, {"inbounds": []})
    raised = False
except RuntimeError:
    raised = True
check("校验不过抛异常", raised)
check("校验不过旧配置没动", json.load(open(cfg_path)) == old_cfg)
check("校验不过临时文件已清掉", not os.path.exists(cfg_path + ".new"))

# ---- 2. _write_core_cfg：校验通过才落盘 ----
m.core_check = lambda binpath, cfg: (True, "")
new_cfg = {"inbounds": [{"tag": "new", "listen_port": 20002}]}
m._write_core_cfg("sing-box", "fake-bin", cfg_path, new_cfg)
check("校验通过新配置落盘", json.load(open(cfg_path)) == new_cfg)
check("校验通过临时文件已换名", not os.path.exists(cfg_path + ".new"))

# ---- 跑 run() 的桩 ----
def stub_run_env(fail_at):
    """fail_at: 在哪个核心的 write_service 上抛异常（"sb" / "xray" / None）。"""
    m.G = {"host": "", "domain": "", "email": "", "sni": "www.example.com", "sni_xr": "",
           "prefix": "", "hy2_ports": "", "nginx": "", "reality443": "", "sni_split": "",
           "smux": "", "force": False}
    m.ensure_deps = lambda: None
    m.takeover_cleanup = lambda: None
    m.public_ip = lambda: "1.2.3.4"
    m.precheck_sni = lambda *a: None
    m.warn_selfsigned = lambda *a: None
    m.install_singbox = lambda: None
    m.install_xray = lambda: None
    m.sb_fix_dns = lambda cfg: None
    m._xr_entry_fixup = lambda ins: ins
    m.cn_block_reapply = lambda: None
    m.bt_reapply = lambda: None
    m.build_subscription = lambda *a, **k: True
    m.sr_set_sub_mode = lambda *a: None
    for f in ("ui_title", "print_node_links", "print_cloud_ports", "print_sub_urls",
              "ui_tip", "ui_line"):
        setattr(m, f, lambda *a, **k: None)
    m.sub_urls_text = lambda: ""
    def build(table, names, pinned=None, dup=None, mark=""):
        port = 20100 if table is m.SB else 20200
        return ([{"type": "vless", "tag": "t", "listen": "::", "listen_port": port}],
                ["vless://fake-link"])
    m.build = build
    def write_service(name, binpath, cfg):
        calls.append(f"write_service {name}")
        if (name == "sing-box" and fail_at == "sb") or (name == "xray" and fail_at == "xray"):
            raise RuntimeError("fake service failure")
    m.write_service = write_service

# ---- 3. 重装回滚：xray 阶段失败，sing-box 恢复旧配置、旧服务重启 ----
old_sb = {"inbounds": [{"tag": "old-sb", "listen_port": 20001}]}
old_xr = {"inbounds": [{"tag": "old-xr", "listen_port": 20002}]}
json.dump(old_sb, open(f"{SB}/config.json", "w"))
json.dump(old_xr, open(f"{XR}/config.json", "w"))
json.dump({"domain": "old.example.com", "sb": ["reality-vision"]}, open(m.STATE_FILE, "w"))
open(m.NODE_FILE, "w").write("vless://old-link\n")
open(m.NGINX_CONF, "w").write("old-nginx")
calls.clear()
m.subprocess = FakeSP({"sing-box", "xray"})
stub_run_env(fail_at="xray")
try:
    m.run(["reality-vision"], ["trojan"])
    raised = False
except RuntimeError:
    raised = True
check("重装 xray 失败时 run() 照样把异常抛出来", raised)
check("重装回滚：sing-box 恢复旧配置",
      json.load(open(f"{SB}/config.json")) == old_sb)
check("重装回滚：xray 恢复旧配置",
      json.load(open(f"{XR}/config.json")) == old_xr)
check("重装回滚：state.json 还是旧的",
      json.load(open(m.STATE_FILE)).get("domain") == "old.example.com")
check("重装回滚：链接文件还是旧的",
      open(m.NODE_FILE).read() == "vless://old-link\n")
check("重装回滚：nginx 配置还是旧的",
      open(m.NGINX_CONF).read() == "old-nginx")
check("重装回滚：旧服务被重启回来",
      "systemctl restart sing-box" in calls and "systemctl restart xray" in calls)
check("重装回滚：nginx 重载了一遍", "nginx_reload" in calls)

# ---- 4. 新装回滚：本来什么都没有，失败后不留半成品服务 ----
for p in (f"{SB}/config.json", f"{XR}/config.json", m.STATE_FILE, m.NODE_FILE, m.NGINX_CONF):
    if os.path.exists(p):
        os.remove(p)
calls.clear()
m.subprocess = FakeSP(set())
stub_run_env(fail_at="sb")
try:
    m.run(["reality-vision"], [])
    raised = False
except RuntimeError:
    raised = True
check("新装 sing-box 失败时 run() 照样把异常抛出来", raised)
check("新装回滚：sing-box 新配置已撤掉", not os.path.exists(f"{SB}/config.json"))
check("新装回滚：没写出 state.json", not os.path.exists(m.STATE_FILE))
check("新装回滚：没写出链接文件", not os.path.exists(m.NODE_FILE))
check("新装回滚：半成品服务被 disable 停掉",
      "systemctl disable --now sing-box" in calls)
check("新装回滚：没动过 nginx 就不重载", "nginx_reload" not in calls)

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""订阅地址末尾换成机器名 —— 仓库主人 10/19：小火箭拿文件名当订阅名，显示的是 token，截图就漏了。
  新地址 /<token>/<名字>.<ext>：名字认机器（谷歌云 Google、AWS…），认不出叫 VPS；不跟节点前缀（前缀会改）。
  老地址 /<token>.<ext> 照样能拉；名字不参与鉴权，token 错照样 404；老装机的小服务脚本自动换新。
"""
import os, sys, socket, subprocess, tempfile, time, urllib.request, urllib.error, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

# 1. 名字：认机器（DMI → 内核后缀 → 厂商自己署的名 → VPS），不跟节点前缀
tmp0 = tempfile.mkdtemp(); m._DMI_DIR = tmp0
def dmi(rel="6.1.0-25-amd64", **kv):
    for n in ("sys_vendor", "product_name", "chassis_asset_tag", "bios_vendor"):
        p = f"{tmp0}/{n}"
        if n in kv: open(p, "w").write(kv[n] + "\n")
        elif os.path.exists(p): os.remove(p)
    m._kernel_release = lambda: rel
    return m.sub_label()
for kv, want in (({"sys_vendor": "Google", "product_name": "Google Compute Engine"}, "Google"),
                 ({"sys_vendor": "Amazon EC2", "product_name": "t3.micro"}, "AWS"),
                 ({"sys_vendor": "Microsoft Corporation", "chassis_asset_tag": "7783-7084-3265-9085-8269-3286-77"}, "Azure"),
                 ({"sys_vendor": "Microsoft Corporation", "product_name": "Virtual Machine"}, "VPS"),
                 ({"sys_vendor": "QEMU", "chassis_asset_tag": "OracleCloud.com"}, "Oracle"),
                 ({"sys_vendor": "Alibaba Cloud", "product_name": "Alibaba Cloud ECS"}, "Aliyun"),
                 ({"sys_vendor": "Vultr", "product_name": "VC2"}, "Vultr"),
                 ({"sys_vendor": "QEMU", "product_name": "Standard PC (i440FX + PIIX, 1996)", "bios_vendor": "SeaBIOS"}, "VPS"),
                 ({"sys_vendor": "Red Hat", "product_name": "KVM"}, "VPS"),
                 ({"sys_vendor": "Contabo GmbH"}, "Contabo"),
                 ({}, "VPS")):
    got = dmi(**kv)
    check(f"{kv or '没有 DMI'} → {got}", got == want)
check("甲骨文 DMI 写 QEMU：认内核 -oracle", dmi("6.8.0-1013-oracle", sys_vendor="QEMU") == "Oracle")
check("谷歌云内核 -gcp", dmi("6.8.0-1015-gcp") == "Google")
m.read_saved_links = lambda: ["x"]; m._split_tag = lambda n: ("🇭🇰", n); m._link_name = lambda u: u
check("节点前缀改成什么都不影响名字", dmi(sys_vendor="QEMU") == "VPS")
dmi(sys_vendor="Google")
m.load_tokens = lambda: {"conf": "TokA_b-1"}; m._sub_https = lambda: True
m._host = lambda: "sub.example"; m.sub_port = lambda: 31000
check(f"小火箭地址：{m.sub_url('conf')}", m.sub_url("conf") == "https://sub.example:31000/TokA_b-1/Google.conf")

# 2. 小服务：新老地址都能拉，token 错 404，不列目录
tmp = tempfile.mkdtemp()
d = f"{tmp}/sub"; os.makedirs(d)
open(f"{tmp}/sr.conf", "w").write("[General]\n"); os.symlink(f"{tmp}/sr.conf", f"{d}/TokA_b-1.conf")
open(f"{d}/index.html", "w").write("")
open(f"{tmp}/gh.token", "w").write("x")
open(f"{tmp}/srv.py", "w").write(m._SUB_SERVER_PY)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
p = subprocess.Popen([sys.executable, f"{tmp}/srv.py", str(port), d, f"{tmp}/gh.token"])
def get(path):
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""
        except OSError:
            time.sleep(0.1)
    return 0, ""
try:
    check("新地址 /<token>/HK.conf 能拉", get("/TokA_b-1/HK.conf") == (200, "[General]\n"))
    check("新地址带 ?参数 也能拉", get("/TokA_b-1/VPS.conf?flag=1")[0] == 200)
    check("老地址 /<token>.conf 照样能拉", get("/TokA_b-1.conf") == (200, "[General]\n"))
    check("token 错：404", get("/TokX/HK.conf")[0] == 404)
    check("名字里塞 ../ 也跳不出去", get("/TokA_b-1/..%2f..%2fsr.conf")[0] in (200, 404) and get("/TokA_b-1/../sr.conf")[0] == 404)
    check("根目录不列文件", get("/")[1] == "")
finally:
    p.terminate()

# 3. 老装机：小服务脚本换新 + 重启一次；一样就不动
srv = f"{tmp}/old-srv.py"; open(srv, "w").write("old")
m.SUB_SERVER = srv; cmds = []; m.sh = lambda c, check=True: cmds.append(c)
check("脚本旧：换新并重启 xy-sub", m._sub_server_heal() and open(srv).read() == m._SUB_SERVER_PY and cmds == ["systemctl restart xy-sub"])
check("脚本已是新的：不动不重启", not m._sub_server_heal() and len(cmds) == 1)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

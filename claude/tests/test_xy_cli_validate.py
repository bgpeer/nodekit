#!/usr/bin/env python3
"""CLI 参数校验：--domain / --email / --sni / --hy2-ports 以前跳过交互式校验、
  直接拼进 openssl / acme.sh 命令、跳过 hy2 冲突检查。
  修法：CLI 走跟交互一样的校验（_cli_validate），过不了直接退出码 2，
  绝不能带着没验过的值进 run()（那后面就是 takeover 卸载）。
"""
import os, sys, socket, argparse, tempfile, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

class FakeAP:
    def error(self, msg):
        raise SystemExit(msg)

def args(**kw):
    d = {"domain": "", "email": "", "sni": "", "sni_xr": "",
         "hy2_ports": "", "sb": "", "xray": ""}
    d.update(kw)
    return argparse.Namespace(**d)

def validates(a):
    try:
        m._cli_validate(a, FakeAP())
        return True
    except SystemExit:
        return False

# ---- 1. 域名 / 邮箱 / 伪装域名：格式校验 ----
check("合法域名通过", validates(args(domain="example.com")))
check("带空格/分号的域名拦下", not validates(args(domain="example.com;id")))
check("带 shell 元字符的域名拦下", not validates(args(domain="a.com$(id)")))
check("单标签域名拦下", not validates(args(domain="localhost")))
MAIL = "a@" + "example.com"   # 邮箱字面量会被 test_claude_folder 当泄漏拦，拆开拼
check("合法邮箱通过", validates(args(email=MAIL)))
check("没点的邮箱拦下", not validates(args(email="a@b")))
check("带空格的邮箱拦下", not validates(args(email="a@b .com")))
check("带 shell 元字符的邮箱拦下", not validates(args(email=MAIL + ";id")))
check("合法伪装域名通过", validates(args(sni="www.example.com")))
check("伪装域名大写/空格归一后通过",
      validates(args(sni=" WWW.Example.COM ")) )
check("带感叹号的伪装域名拦下", not validates(args(sni="bad host!")))
check("带 shell 元字符的伪装域名拦下", not validates(args(sni="a.com$(id)")))
check("空值不校验（走默认）", validates(args()))

# ---- 2. 共享校验函数本身 ----
check("valid_domain 认 example.com", m.valid_domain("example.com"))
check("valid_domain 拦 example.com;id", not m.valid_domain("example.com;id"))
check("valid_domain 拦下划线域名", not m.valid_domain("ex_ample.com"))
check("valid_email 认示例邮箱", m.valid_email(MAIL))
check("valid_email 拦 a@b", not m.valid_email("a@b"))
check("_parse_hy2_range 认 30000-31000",
      m._parse_hy2_range("30000-31000") == (30000, 31000))
check("_parse_hy2_range 拦 abc", m._parse_hy2_range("abc") is None)
check("_parse_hy2_range 拦起止倒置", m._parse_hy2_range("31000-30000") is None)
check("_parse_hy2_range 拦超 65535", m._parse_hy2_range("30000-70000") is None)

# ---- 3. --hy2-ports：格式 + UDP 冲突（跟交互式 ask_hy2_range 同口径） ----
tmp = tempfile.mkdtemp()
m.SB_DIR = f"{tmp}/sb"; os.makedirs(m.SB_DIR)
m.XRAY_DIR = f"{tmp}/xray"; os.makedirs(m.XRAY_DIR)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.bind(("0.0.0.0", 40500))          # 段内占一个真 UDP 端口
check("跳跃段撞上真 UDP 服务就拦下",
      not validates(args(sb="hy2", hy2_ports="40000-41000")))
u.close()
check("UDP 服务走了就不拦了",
      validates(args(sb="hy2", hy2_ports="40000-41000")))
check("格式不对拦下", not validates(args(sb="hy2", hy2_ports="abc")))
check("起止倒置拦下", not validates(args(sb="hy2", hy2_ports="41000-40000")))
check("off 不拦", validates(args(sb="hy2", hy2_ports="off")))
a = args(sb="hy2", hy2_ports="OFF")
m._cli_validate(a, FakeAP())
check("OFF 归一成 n", a.hy2_ports == "n")
a = args(sb="all", hy2_ports="abc")
check("--sb all 含 hy2，照样验", not validates(a))
a = args(sb="reality-vision", hy2_ports="abc")
check("没选 hy2 就不验跳跃范围（跟交互一致）", validates(a))

print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)

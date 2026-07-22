#!/usr/bin/env python3
# adguard-dns.py —— 一键架设 AdGuard Home 去广告 DNS（全设备 DNS 层去广告）
# 独立文件，nodekit 主脚本(xy-installer.py)通过子进程调用：
#   python3 adguard-dns.py            交互菜单（安装 / 卸载 / 查看）
#
# 定位：给「装不了代理的设备」(电视/盒子/IoT/路由器) 和「安卓专用DNS(全系统)」做 DNS 层去广告。
#   - 挂代理的设备本来就靠订阅里的 reject 规则集拦广告，这个是补「没挂代理」的场景。
#   - AdGuard Home 是网页后台管理的软件：这里做「一键装好起服务 + 一键干净卸载」，
#     设管理密码、微调过滤名单在它的网页后台点几下完成（广告过滤默认即开）。
#   - 加密走 DoT(853，复用 acme 证书)：安卓「专用 DNS」填域名即可全系统去广告；
#     DoH 需要 443（被 reality/nginx 占了）故不用。明文 53 给装不了 DoT 的设备（电视/IoT）。
#   - 不动 sing-box/xray/节点：独立服务，卸载彻底、互不影响。
import os, re, sys, time, socket, shutil, secrets, subprocess, urllib.request

BGP_DIR = "/etc/bgpeer"
HOST_FILE = BGP_DIR + "/sub.host"                 # 主脚本存的 host（域名或 IP）
ACME_CRT, ACME_KEY = "/etc/ssl/sb/acme.crt", "/etc/ssl/sb/acme.key"   # 主脚本 acme 证书
AGH_DIR = "/opt/AdGuardHome"
AGH_BIN = AGH_DIR + "/AdGuardHome"
AGH_INSTALL = "https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh"
WEB_PORT = 3000

def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。"""
    try:
        with open("/dev/tty", "r") as t:
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\n").strip()
    except (OSError, EOFError):
        return input(prompt).strip()

def _host():
    try: return open(HOST_FILE).read().strip()
    except OSError: return ""

def _domain():
    """有域名才返回域名（DoT 需要证书 + 域名）；没域名返回 ''。"""
    h = _host()
    return h if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", h or "") else ""

def _public_ip():
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            return urllib.request.urlopen(u, timeout=8).read().decode().strip()
        except Exception:
            pass
    out = sh("hostname -I")
    return out.split()[0] if out else "本机IP"

def _installed():
    return os.path.exists(AGH_BIN)

def _running():
    return sh("systemctl is-active AdGuardHome") == "active"

def _port_busy(port):
    """端口是否被占（TCP bind 探测）。占用返回占用者的粗略名字，空闲返回 ''。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return ""                                    # 绑得上 → 空闲
    except OSError:
        who = sh(f"ss -lntup 'sport = :{port}' 2>/dev/null | tail -n +2")
        m = re.search(r'users:\(\("([^"]+)"', who)
        return m.group(1) if m else "未知进程"
    finally:
        s.close()

def _cert_ready():
    return os.path.exists(ACME_CRT) and os.path.exists(ACME_KEY)

def install():
    if os.geteuid() != 0:
        print("  需要 root 运行。"); return
    if _installed():
        print("  AdGuard Home 已经装过了。要重装请先『2 卸载』再装。")
        _usage(); return
    dom = _domain()
    print("\n  === 安装前检查 ===")
    print(f"  域名: {dom or '（未用域名，装脚本时没填域名）'}")
    if not dom:
        print("  ⚠ 没有域名：安卓「专用 DNS」(加密 DoT) 用不了，只能给设备填本机 IP 走明文 53。")
        print("    想要安卓全系统加密去广告，得先用域名重装主节点。")
        if _ask("  仍然继续安装（只用明文 53）? [y/N]: ").lower() not in ("y", "yes"):
            return
    elif not _cert_ready():
        print(f"  ⚠ 有域名但没找到 acme 证书（{ACME_CRT}）：多半是装节点时用的是自签。")
        print("    没有真证书 DoT 客户端会拒连。可继续装，但建议先给节点配好域名真证书。")
        if _ask("  仍然继续? [y/N]: ").lower() not in ("y", "yes"):
            return
    # 53 常被 systemd-resolved 占用，会让向导里 DNS 端口报红过不去——装前先帮忙腾好
    b53 = _port_busy(53)
    if b53 and ("systemd-resolve" in b53 or "systemd" in b53):
        print(f"\n  ⚠ 53 端口被 {b53} 占用——不腾的话向导里 DNS 端口(53)会报红过不去。")
        if _ask("  现在就腾出 53（关 systemd-resolved 的 53 桩监听，本机解析改公共 DNS，可逆）? [Y/n]: ").lower() not in ("n", "no"):
            _do_free53()
    elif b53:
        print(f"\n  ⚠ 53 端口被 {b53} 占用（不是 systemd-resolved）——请自行确认能否停它，否则向导 DNS 端口会报红。")

    print("\n  正在安装 AdGuard Home（官方安装脚本，装到 /opt/AdGuardHome）…")
    r = subprocess.run(f'curl -sSL "{AGH_INSTALL}" | sh -s -- -v', shell=True)
    if r.returncode or not _installed():
        print("\n  ❌ 安装失败（多半是网络/GitHub 限流）。稍后重试。"); return
    ok = False
    for _ in range(12):
        time.sleep(1)
        if _running(): ok = True; break
    if ok:
        print("\n  ✓ AdGuard Home 已安装并启动。")
    else:
        print("\n  已安装，但服务暂未在运行——稍等或看 `systemctl status AdGuardHome`。")
    _first_setup(_pick_web_port(0) or WEB_PORT)          # 装时就随机挑一个 2000-5000 的后台端口（固定不跳）

def _first_setup(sug):
    ip = _public_ip()
    print("\n  === 下一步：打开网页后台完成初始化（2 分钟）===")
    print(f"  1) 浏览器打开安装向导：\033[1;32mhttp://{ip}:{WEB_PORT}\033[0m（先在防火墙放行 {WEB_PORT}/TCP）")
    print("  2) 向导里两个端口按这样填（\033[1;33m别用默认的 80\033[0m）：")
    print(f"     · 网页管理界面 端口 → 填 \033[1;32m{sug}\033[0m（我随机生成的·防扫描，固定用它不会跳；想省事填 {WEB_PORT} 也行，之后可用菜单 5 改）")
    print("     · DNS 服务器 端口 → 保持 \033[1;32m53\033[0m（装前已帮你腾好；仍报红就回菜单选 4 腾53）")
    print("  3) 设管理员账号密码 → 完成。广告过滤（AdGuard DNS filter）默认就是开的。")
    print(f"  4) 完成后后台地址变成 \033[1;32mhttp://{ip}:{sug}\033[0m（防火墙放行 {sug}、可关掉 {WEB_PORT}）")
    _usage(sug)

def _usage(port=None):
    """一步步的使用说明：登录后台 → 开加密 → 设备指过来。写给不懂的人看。"""
    ip = _public_ip(); dom = _domain()
    port = port or _current_web_port() or WEB_PORT
    G = "\033[1;32m"; Y = "\033[1;33m"; N = "\033[0m"     # 绿=要填的值，黄=提示
    print("\n" + "  " + "=" * 56)
    print("  怎么用它去广告 —— 照着下面三步做")
    print("  " + "=" * 56)

    print("\n  【第一步 · 登录网页后台】")
    print(f"    浏览器打开：  {G}http://{ip}:{port}{N}")
    print("    ↑ 这就是你的管理后台，首次打开设个账号密码；以后看拦截统计、加名单都进这里。")

    print("\n  【第二步 · 开加密】（给手机全系统去广告用；没域名可跳过，只用第三步①明文）")
    if dom:
        print("    后台里点：设置 → 加密设置(Encryption)，然后：")
        print(f"      · 勾选「启用加密」")
        print(f"      · 服务器名称        填：{G}{dom}{N}")
        print(f"      · 证书（选『文件路径』）填：{G}{ACME_CRT}{N}")
        print(f"      · 私钥（选『文件路径』）填：{G}{ACME_KEY}{N}")
        print(f"      · HTTPS 端口         填：{G}10443{N}   （不要填 0，也别填 443）")
        print(f"      · DNS-over-TLS 端口  填：{G}853{N}")
        print(f"      · 点『保存』（若提示 {Y}no IP addresses{N} 的黄字，无害，忽略）")
    else:
        print(f"    {Y}你装节点时没用域名 → 加密(DoT/DoH)用不了，只能用下面①明文 DNS。{N}")
        print("    想要手机全系统加密去广告，得先用域名重装节点。")

    print("\n  【第三步 · 把设备的 DNS 指到这台服务器】下面三种，按设备挑一种：")
    print(f"    ① 明文 DNS —— 电视/盒子/IoT/路由器/电脑，最通用")
    print(f"        把设备的 DNS 填成：  {G}{ip}{N}")
    print(f"        需要：VPS 防火墙放行 {G}53{N}(UDP+TCP)；若 53 被占，回菜单选『4 腾出53端口』")
    if dom:
        print(f"    ② DoT 加密 —— 安卓手机「专用DNS」，全系统生效，{Y}最推荐{N}")
        print(f"        手机：设置 → 网络 → 专用DNS → 选『私人DNS提供商主机名』→ 填：{G}{dom}{N}")
        print(f"        需要：先做完第二步开加密；VPS 防火墙放行 {G}853{N}(TCP)")
        print(f"    ③ DoH 加密 —— 电脑浏览器 / 支持 DoH 的 App")
        print(f"        DoH 地址：  {G}https://{dom}:10443/dns-query{N}")
        print(f"        需要：VPS 防火墙放行 {G}10443{N}(TCP)")

    print("\n  " + "-" * 56)
    print("  三种 DNS 怎么选（一句话）：")
    print(f"    · 明文 53     简单通用、不加密 —— 电视/IoT/内网设备")
    print(f"    · DoT 853     加密、安卓系统原生支持、一次设置全系统去广告 {Y}【首选】{N}")
    print(f"    · DoH 10443   加密、浏览器/App 用；因本机 443 被节点占，所以带端口")
    print("\n  想拦更多广告：")
    print("    · 后台 → 过滤器 → DNS 拦截列表 → 添加名单（推荐 anti-AD：https://anti-ad.net/easylist.txt）")
    print("    · 后台 → 查询日志 → 找到广告域名 → 点『屏蔽』")

def _do_free53():
    """真正腾 53：关掉 systemd-resolved 的桩监听 + 把 resolv.conf 指到公共 DNS（可逆）。
       不含确认/占用者判断，供安装流程与菜单复用。"""
    try:
        d = "/etc/systemd/resolved.conf.d"
        os.makedirs(d, exist_ok=True)
        open(d + "/adguard.conf", "w").write("[Resolve]\nDNSStubListener=no\n")
        # resolv.conf 常是指向 stub(127.0.0.53) 的软链；关桩后要换成真能用的解析
        try: os.remove("/etc/resolv.conf")
        except OSError: pass
        open("/etc/resolv.conf", "w").write("nameserver 1.1.1.1\nnameserver 223.5.5.5\n")
        sh("systemctl restart systemd-resolved")
        time.sleep(2)
        left = _port_busy(53)
        if left and "AdGuardHome" not in left:
            print(f"  53 仍被 {left} 占，请手动检查 `ss -lntup 'sport = :53'`。")
        else:
            print("  ✓ 已腾出 53。")
        print("  （撤销：删 /etc/systemd/resolved.conf.d/adguard.conf 后 systemctl restart systemd-resolved）")
    except OSError as e:
        print("  腾 53 失败:", e)

def free_port53():
    """菜单入口：腾出 53 端口（关 systemd-resolved 桩监听，可逆）。"""
    if os.geteuid() != 0:
        print("  需要 root。"); return
    who = _port_busy(53)
    if not who:
        print("  53 端口现在是空闲的，无需腾。"); return
    if "systemd-resolve" not in who and "systemd" not in who:
        print(f"  53 被 {who} 占用，不是 systemd-resolved——请自行确认那个服务能否停。未改动。"); return
    if _ask("  关闭 systemd-resolved 的 53 桩监听（本机解析改用公共 DNS，可逆）? [y/N]: ").lower() not in ("y", "yes"):
        return
    _do_free53()
    sh("systemctl restart AdGuardHome")           # 已装则让 AGH 立刻接管 53

def _agh_yaml():
    return AGH_DIR + "/AdGuardHome.yaml"

_ADDR_RE = re.compile(r'(?m)^(\s*address:\s+\S+:)(\d+)(\s*)$')   # AdGuardHome.yaml 里 http.address 行

def _current_web_port():
    """从 AdGuardHome.yaml 读当前后台端口；读不到返回 None。"""
    try: txt = open(_agh_yaml()).read()
    except OSError: return None
    m = _ADDR_RE.findall(txt)
    return int(m[0][1]) if len(m) == 1 else None

def _pick_web_port(avoid):
    """在 2000-5000 随机挑一个空闲端口（避开当前端口/被占端口）。"""
    for _ in range(300):
        p = secrets.randbelow(5000 - 2000 + 1) + 2000
        if p == avoid or _port_busy(p):
            continue
        return p
    return None

def change_web_port():
    """改 AdGuard 网页后台端口（随机 2000-5000 / 自定义）：改 yaml + 重启 + 校验，
       改完连不上就回滚，绝不把你锁在后台外面。DNS 端口(53/853)是协议固定的，不动。"""
    if os.geteuid() != 0:
        print("  需要 root。"); return
    if not _installed():
        print("  还没装 AdGuard Home，先选 1 安装。"); return
    yaml_path = _agh_yaml()
    try: txt = open(yaml_path).read()
    except OSError:
        print("  读不到 AdGuard 配置文件。"); return
    hits = _ADDR_RE.findall(txt)
    if len(hits) != 1:                                   # 没能唯一定位就别乱改
        print(f"  配置里没能唯一定位后台端口行（找到 {len(hits)} 处），保险起见不自动改。"); return
    cur = int(hits[0][1])
    print(f"\n  当前后台端口: {cur}")
    print("  1 随机(2000-5000)   2 自定义   0 取消")
    c = _ask("  选择: ").strip()
    if c == "1":
        new = _pick_web_port(cur)
        if not new:
            print("  2000-5000 内没挑到空闲端口，稍后再试。"); return
    elif c == "2":
        s = _ask("  输入端口(1024-65535): ").strip()
        if not s.isdigit() or not (1024 <= int(s) <= 65535):
            print("  端口无效。"); return
        new = int(s)
        if new != cur and _port_busy(new):
            print(f"  {new} 已被占用，换一个。"); return
    else:
        return
    if new == cur:
        print("  端口没变，未改动。"); return
    # 改端口（内存留原文以便回滚）→ 重启 → 校验 AGH 在新端口起来了
    open(yaml_path, "w").write(_ADDR_RE.sub(lambda m: f"{m.group(1)}{new}{m.group(3)}", txt, count=1))
    sh("systemctl restart AdGuardHome")
    ok = False
    for _ in range(12):
        time.sleep(1)
        if _running() and "AdGuardHome" in _port_busy(new):
            ok = True; break
    if ok:
        ip = _public_ip()
        print(f"\n  ✓ 后台端口已改为 {new}。新后台地址：\033[1;32mhttp://{ip}:{new}\033[0m")
        print(f"  ▸ 防火墙：放行 \033[1;32m{new}/TCP\033[0m，关掉旧的 {cur}/TCP。")
    else:
        open(yaml_path, "w").write(txt)                  # 回滚原配置
        sh("systemctl restart AdGuardHome")
        print(f"\n  ❌ 改到 {new} 后 AGH 没在新端口正常起来，已回滚回 {cur}（后台仍可用）。稍后再试或换个端口。")

def status():
    print("\n  === AdGuard Home 状态 ===")
    if not _installed():
        print("  未安装。选『1 安装』先装上。"); return
    print("  已安装:", AGH_BIN)
    print("  运行中 ✓" if _running() else "  未运行 ✗（systemctl status AdGuardHome 看原因）")
    port = _current_web_port()
    if port:
        print(f"  后台端口: {port}    （登录地址见下方第一步）")
    b53 = _port_busy(53)
    print("  53 端口:", "空闲" if not b53 else f"被 {b53} 占用")
    _usage(port)

def uninstall():
    if not _installed():
        print("  没检测到 AdGuard Home，无需卸载。"); return
    if _ask("  确认卸载 AdGuard Home（去广告 DNS）? [y/N]: ").lower() not in ("y", "yes"):
        return
    if os.path.exists(AGH_BIN):
        sh(f"{AGH_BIN} -s uninstall")                # 官方卸载（停服务 + 注销 systemd）
    sh("systemctl stop AdGuardHome")
    sh("systemctl disable AdGuardHome")
    try: shutil.rmtree(AGH_DIR)
    except OSError: pass
    print("  ✓ 已卸载 AdGuard Home（sing-box/xray/节点不受影响）。")
    print("  记得把之前改过 DNS / 专用DNS 的设备改回自动/默认，否则它们会没 DNS 可用。")

def menu():
    while True:
        print("\n" + "=" * 60 + "\n去广告 DNS · AdGuard Home（全设备 DNS 层去广告）\n" + "=" * 60)
        st = "已安装 " + ("运行中 ✓" if _running() else "未运行 ✗") if _installed() else "未安装"
        print("  当前状态:", st)
        print("-" * 60)
        print("  1 安装（装 AdGuard Home + 起服务，之后网页后台点几下完成设置）")
        print("  2 卸载（彻底移除，不动节点）")
        print("  3 查看状态 / 设备怎么设置")
        print("  4 腾出 53 端口（被 systemd-resolved 占用时用）")
        print("  5 改后台端口（随机 2000-5000 / 自定义，防扫描；带回滚）")
        print("  0 退出")
        c = _ask("选择: ").strip()
        if c == "1":   install()
        elif c == "2": uninstall()
        elif c == "3": status()
        elif c == "4": free_port53()
        elif c == "5": change_web_port()
        elif c in ("0", ""):
            return

def main():
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    if act == "remove":                              # 主脚本整体卸载时可调用
        uninstall()
    else:
        menu()

if __name__ == "__main__":
    main()

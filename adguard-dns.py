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
import os, re, sys, time, socket, shutil, secrets, struct, base64, ssl, subprocess, urllib.request

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

def _ask_secret(prompt=""):
    """读密钥类输入且【不回显】。API Token 一旦回显就会留在终端回滚缓冲里，
       随手一张截图、一次录屏、一次贴日志求助就泄露了——而它能改你整个域名的 DNS。"""
    try:
        import termios
        with open("/dev/tty", "r+") as t:
            fd = t.fileno()
            old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO                      # 关掉回显
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, new)
                t.write(prompt); t.flush()
                line = t.readline()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                t.write("\n"); t.flush()
            if line == "":
                raise EOFError
            return line.rstrip("\n").strip()
    except Exception:
        try:
            import getpass
            return getpass.getpass(prompt).strip()        # 兜底：非 tty 环境
        except Exception:
            return _ask(prompt)

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
    _first_setup()          # 初装向导统一走默认 3000（先保证进得去），装好后再用菜单 5 改端口（带回滚，锁不死）

def _first_setup():
    ip = _public_ip()
    print("\n  === 下一步：打开网页后台完成初始化（2 分钟）===")
    print(f"  1) 浏览器打开安装向导：\033[1;32mhttp://{ip}:{WEB_PORT}\033[0m（先在防火墙放行 {WEB_PORT}/TCP）")
    print("  2) 向导里两个端口按这样填（\033[1;33m别用默认的 80\033[0m）：")
    print(f"     · 网页管理界面 端口 → 就填默认的 \033[1;32m{WEB_PORT}\033[0m（\033[1;33m初装先用它保证进得去\033[0m；想换防扫描端口，装好后回菜单选 5 改，改完连不上会自动回滚、锁不死）")
    print("     · DNS 服务器 端口 → 保持 \033[1;32m53\033[0m（装前已帮你腾好；仍报红就回菜单选 4 腾53）")
    print("  3) 设管理员账号密码 → 完成。广告过滤（AdGuard DNS filter）默认就是开的。")
    print(f"  4) 完成后后台地址就是 \033[1;32mhttp://{ip}:{WEB_PORT}\033[0m；想改端口防扫描，回菜单选 \033[1;32m5\033[0m 改（带回滚，安全）")
    _usage(WEB_PORT)

SELFDNS_FLAG     = BGP_DIR + "/selfdns.on"        # 主脚本：自建 DNS 是否已写进订阅
SELFDNS_CID_FILE = BGP_DIR + "/selfdns.clientid"  # 主脚本生成的 AdGuard ClientID

def _selfdns_clientid():
    """读主脚本生成的 ClientID（只读，不生成——它由菜单 6 打开写入订阅时创建）。"""
    try:
        return open(SELFDNS_CID_FILE).read().strip()
    except OSError:
        return ""

def _doh_port():
    """当前 DoH(HTTPS) 端口：从 AdGuardHome.yaml 的 tls.port_https 读，读不到按 10443。"""
    try:
        p = int(_yaml_val(_tls_block(open(_agh_yaml()).read()), "port_https") or 0)
        return p or 10443
    except (OSError, ValueError):
        return 10443

def _cert_wildcard_ok(dom):
    """本机 acme 证书是否覆盖 *.dom —— 这决定 DoT 能不能带 ClientID。
       DoT 的 ClientID 是塞在 SNI 第一段(tls://<ID>.域名)传过去的，证书里没有
       *.域名 这个 SAN，客户端握手就会因证书名不匹配而拒连。"""
    if not (dom and os.path.exists(ACME_CRT)):
        return False
    return f"DNS:*.{dom}" in sh(f"openssl x509 -in {ACME_CRT} -noout -text 2>/dev/null")

def _wildcard_dns_ok(dom):
    """*.dom 的 DNS 通配记录是否已生效：随便取个不存在的子域，能解析出 IP 就算通。
       证书能签下来不代表客户端找得到路——通配 A 记录得单独加。"""
    try:
        socket.getaddrinfo(f"dotcheck{secrets.token_hex(3)}.{dom}", None, socket.AF_INET)
        return True
    except OSError:
        return False

def _allowed_clients():
    """AdGuardHome.yaml 里的访问白名单 allowed_clients；没这个键返回 None，空列表返回 []。"""
    try:
        return _yaml_list(open(_agh_yaml()).read(), "allowed_clients")
    except OSError:
        return None

def _usage(port=None):
    """一步步的使用说明：当前配置一览 → 登录后台 → 开加密 → 设备指过来。写给不懂的人看。"""
    ip = _public_ip(); dom = _domain()
    port = port or _current_web_port() or WEB_PORT
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; C = "\033[1;36m"; N = "\033[0m"
    dohp = _doh_port(); cid = _selfdns_clientid()
    enc = _https_panel() is not None
    allow = _allowed_clients()
    doh_url = f"https://{dom}:{dohp}/dns-query" + (f"/{cid}" if cid else "")
    wild = _cert_wildcard_ok(dom)                     # 证书覆盖 *.域名 → DoT 可带 ClientID
    dot_host = f"{cid}.{dom}" if (wild and cid) else dom

    print("\n" + "  " + "=" * 56)
    print(f"  {C}当前配置信息{N}（下面的值都是从你这台机实时读出来的）")
    print("  " + "=" * 56)
    print(f"    域名          : {G}{dom or '（无，只能用明文 53）'}{N}")
    print(f"    公网 IP       : {G}{ip}{N}")
    print(f"    后台端口      : {G}{port}{N}")
    print(f"    加密(DoT/DoH) : {(G+'已开启'+N) if enc else (Y+'未开启 —— 第二步去开'+N)}")
    print(f"    DoH 端口      : {G}{dohp}{N}        DoT 端口: {G}853{N}       明文: {G}53{N}")
    if cid:
        print(f"    {C}ClientID{N}      : {G}{cid}{N}")
        print(f"                    {Y}这是给「访问设置 → 允许的客户端」用的{N}，填了它就只放行你自己；")
        print(f"                    不填的话你的 DoH 挂在公网上，{R}谁扫到都能当免费解析器用{N}。")
    else:
        print(f"    ClientID      : {Y}还没有 —— 菜单『6 把自建DNS写入订阅配置』打开一次即生成{N}")
    if allow:
        print(f"    访问白名单    : {G}已设置{N} {allow}   （只放行名单内，见第四步）")
    else:
        print(f"    访问白名单    : {R}未设置 —— 对全公网开放{N}"
              + ("（第四步教你关掉）" if cid else ""))
    print(f"    写入订阅      : {(G+'已写入 mihomo/小火箭'+N) if os.path.exists(SELFDNS_FLAG) else (Y+'未写入（菜单 6 开关）'+N)}")

    print("\n  " + "=" * 56)
    print("  怎么用它去广告 —— 照着下面三步做")
    print("  " + "=" * 56)

    print("\n  【第一步 · 登录网页后台】")
    hp = _https_panel()
    if hp:
        print(f"    浏览器打开(推荐·加密)：  {G}https://{hp[0]}:{hp[1]}{N}")
        print(f"    {Y}走域名 HTTPS，登录密码不会明文过网{N}；应急/本机可用明文 http://{ip}:{port}")
    else:
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
        print(f"      · 「HTTPS 自动重定向」{Y}不要勾{N}（勾了进后台会强制跳 HTTPS，用 IP 访问会证书报错、进不去）")
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
        print(f"        手机：设置 → 网络 → 专用DNS → 选『私人DNS提供商主机名』→ 填：{G}{dot_host}{N}")
        if wild and cid:
            print(f"        {Y}↑ 前面那段就是 ClientID{N}（已签泛域名证书，所以 DoT 也能带上它、白名单放行）")
        print(f"        需要：先做完第二步开加密；VPS 防火墙放行 {G}853{N}(TCP)")
        print(f"    ③ DoH 加密 —— 电脑浏览器 / 支持 DoH 的 App")
        print(f"        DoH 地址：  {G}{doh_url}{N}")
        if cid:
            print(f"        {Y}↑ 末段那串就是 ClientID，必须带上{N}（设了白名单之后不带会被拒）")
        print(f"        需要：VPS 防火墙放行 {G}{dohp}{N}(TCP)")

    # 第四步：关开放解析器。写成能照做的步骤（完整路径 + ID 单独一行方便复制），
    # 不能只在菜单 6 打开开关的那一瞬间提示一次——用户回头再看时是进这个页面找的。
    print(f"\n  【第四步 · 关掉「开放解析器」{Y}强烈建议{N}】")
    if not cid:
        print(f"    {Y}还没有 ClientID{N} —— 先去菜单『6 把自建DNS写入订阅配置』打开一次，会自动生成。")
    elif allow:
        print(f"    {G}✓ 已经设好了{N}（当前白名单：{allow}），不用再动。")
    else:
        print(f"    你的 DoH 挂在公网上，{R}不设白名单谁扫到都能拿去当免费解析器用{N}"
              f"（烧你的 CPU 和流量，查询日志里还会混进陌生人的记录）。")
        print(f"    AdGuard 后台 → 设置 → DNS设置 → {C}访问设置{N} → {C}允许的客户端{N}，填入这一行：")
        print(f"\n        {G}{cid}{N}\n")
        print(f"    手机流量 IP 天天变、没法按 IP 做白名单，这个 ClientID 与 IP 无关，换网络也不影响。")
        print(f"    保存后客户端重新拉一次订阅即生效。")
    if cid and wild:
        print(f"    {G}✓ 已签泛域名证书{N}：DoH 和 DoT 都能带 ClientID，白名单都放行——")
        print(f"        · DoH（上面③）  {G}正常{N}，ID 在网址末段")
        print(f"        · DoT（上面②）  {G}正常{N}，ID 在主机名最前面 → {G}{dot_host}{N}")
        print(f"        · 明文53（上面①）  {R}仍会被挡{N} —— 这个协议根本没有 ID 机制，")
        print(f"          电视/IoT 要么把它们的固定 IP 也填进白名单，要么别用这台 DNS。")
    elif cid:
        print(f"    {R}⚠ 代价要先知道{N}：白名单一设，只有能带上 ClientID 的方式进得来——")
        print(f"        · DoH（上面③）  {G}正常{N}，ID 就在网址末段")
        print(f"        · DoT（上面②，安卓专用DNS）/ 明文53（上面①）  {R}会被挡掉{N}")
        print(f"          DoT 传 ID 要靠 tls://<ID>.域名 的 SNI，{R}需要泛域名证书{N}(*.域名)，")
        print(f"          而本机 acme 只签了单域名；明文 53 根本没有 ID 机制。")
        print(f"        {C}→ 想让安卓 DoT 也能用：回菜单选『8 让 DoT 也能带 ClientID』{N}（签泛域名证书）")
        print(f"        {Y}不想折腾就别设白名单{N}——或者把设备固定 IP 填进去（家宽/流量 IP 会变，多半不好使）。")

    print("\n  " + "-" * 56)
    print("  三种 DNS 怎么选（一句话）：")
    print(f"    · 明文 53     简单通用、不加密 —— 电视/IoT/内网设备")
    print(f"    · DoT 853     加密、安卓系统原生支持、一次设置全系统去广告 {Y}【首选】{N}")
    print(f"    · DoH {dohp}   加密、浏览器/App 用；因本机 443 被节点占，所以带端口")
    print("\n  想拦更多广告：")
    print("    · 后台 → 过滤器 → DNS 拦截列表 → 添加名单（推荐 anti-AD：https://anti-ad.net/easylist.txt）")
    print("    · 后台 → 查询日志 → 找到广告域名 → 点『屏蔽』")

def _replace_cf_token():
    """换掉 acme.sh 存的 Cloudflare Token（旧的泄露/被吊销时用）。
       acme.sh 把它存成 account.conf 里的 SAVED_CF_Token='…'，续期时读这里。
       只换这一个值，不重签证书——证书还好好的，没必要浪费一次 LE 签发额度。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; N = "\033[0m"
    conf = os.path.expanduser("~/.acme.sh/account.conf")
    if not os.path.exists(conf):
        print(f"  {R}找不到 {conf}{N}，你的证书可能不是本脚本签的。"); return
    new = _ask_secret("  粘贴新的 Cloudflare API Token（不回显）: ").strip()
    if not new:
        print("  没输入，未改动。"); return
    txt = open(conf).read()
    line = f"SAVED_CF_Token='{new}'"
    if re.search(r"(?m)^SAVED_CF_Token=.*$", txt):
        txt = re.sub(r"(?m)^SAVED_CF_Token=.*$", line, txt)
    else:
        txt = txt.rstrip("\n") + "\n" + line + "\n"
    bak = conf + ".bak"
    shutil.copy(conf, bak)
    open(conf, "w").write(txt)
    os.chmod(conf, 0o600)                                # 里面是密钥，别让别的用户读到
    print(f"  {G}✓ 已更新{N}（旧文件备份在 {bak}）。")
    print(f"  {Y}记得回 Cloudflare 把旧 Token 删掉{N}，否则它仍然能改你的 DNS。")
    print(f"  下次证书续期(约90天后)会用新 Token；想立刻验证可跑：")
    print(f"      ~/.acme.sh/acme.sh --renew -d {_domain()} --ecc --force")

def _revert_wildcard(dom, cid=""):
    """撤销泛域名：把证书重签回只覆盖『域名』，DoT 就不能再带 ClientID 了。

       80 端口空着就用 HTTP-01 重签——这样连 acme.sh 的续期方式也一并回到装机时的
       样子，之后不再依赖 CF Token，可以把它从 account.conf 里清掉。80 被占（nginx
       等）则退回 DNS-01，此时 Token 必须留着，否则 90 天后续期会失败。

       同样是失败安全的：acme.sh 只有签发成功才覆盖证书文件。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; N = "\033[0m"
    print(f"\n  撤销之后：")
    print(f"    · 安卓「专用DNS」要改回填 {G}{dom}{N}（不能再带 ClientID）")
    if _allowed_clients():
        print(f"    · {R}你已经设了访问白名单 —— 撤销后安卓 DoT 会被挡掉、连不上{N}，")
        print(f"      要么同时把白名单清掉，要么就别撤销。")
    print(f"    · DoH 不受影响，照旧带 ClientID")
    if _ask("\n  确认撤销? [y/N]: ").lower() not in ("y", "yes"):
        print("  已取消，没有任何改动。"); return

    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print(f"  {R}找不到 acme.sh{N}，无法重签。"); return
    http01 = not _port_busy(80)
    if http01:
        issue = f"{acme} --issue -d {dom} --standalone --keylength ec-256 --server letsencrypt --force"
        how = "HTTP-01（80 端口空闲，撤销后不再依赖 CF Token）"
    else:
        issue = f"{acme} --issue -d {dom} --dns dns_cf --keylength ec-256 --server letsencrypt --force"
        how = f"DNS-01（80 被占用，{Y}CF Token 仍需保留供续期{N}）"
    print(f"\n  正在重签单域名证书，验证方式：{how} …")
    r = subprocess.run(issue, shell=True, text=True, capture_output=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode and "Cert success" not in out:
        print(f"\n  {R}❌ 重签失败，原证书原封未动（泛域名仍然有效）。{N}报错：")
        for ln in out.splitlines()[-10:]:
            print("     " + ln)
        return
    reload_hook = (" --reloadcmd '"
                   "systemctl reload nginx 2>/dev/null; "
                   "systemctl restart sing-box 2>/dev/null; "
                   "systemctl restart xray 2>/dev/null; "
                   "systemctl restart xy-sub 2>/dev/null; "
                   "systemctl restart AdGuardHome 2>/dev/null; true'")
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
    if _cert_wildcard_ok(dom):
        print(f"  {R}❌ 证书里仍有 *.{dom}，撤销没生效。{N}"
              f"看 `openssl x509 -in {ACME_CRT} -noout -text | grep DNS:` 确认。")
        return
    sh("systemctl restart AdGuardHome", check=False)
    time.sleep(3)
    print(f"  {G}✓ 证书已改回只覆盖 {dom}{N}")
    print(f"  安卓「专用DNS」现在要填：{G}{dom}{N}")
    if http01:
        conf = os.path.expanduser("~/.acme.sh/account.conf")
        if os.path.exists(conf) and re.search(r"(?m)^SAVED_CF_Token=", open(conf).read()):
            if _ask("  续期已不需要 CF Token，把它从 account.conf 清掉? [Y/n]: ").lower() not in ("n", "no"):
                txt = re.sub(r"(?m)^SAVED_CF_Token=.*$\n?", "", open(conf).read())
                shutil.copy(conf, conf + ".bak"); open(conf, "w").write(txt); os.chmod(conf, 0o600)
                print(f"  {G}✓ 已清除{N}（备份 {conf}.bak）。{Y}记得回 CF 把该 Token 删掉。{N}")
    if cid and _allowed_clients():
        print(f"  {R}⚠ 白名单还留着 {cid}{N} —— 现在安卓 DoT 带不了它了，会被挡。"
              f"要用 DoT 就去 AdGuard 把白名单清空。")

def dot_clientid():
    """把 acme 证书重签成同时覆盖『域名』和『*.域名』，让安卓 DoT 也能带 ClientID。

       为什么这么做就够：AdGuard 的证书路径本来就指着 /etc/ssl/sb/acme.crt，把这个文件
       换成覆盖面更大的同一张证书即可 —— AdGuard 配置一个字都不用改，也就没有改坏配置
       再回滚的风险。新证书是旧证书的超集，走域名的节点(ws/trojan)照常握手，不受影响。

       为什么必须 DNS-01：Let's Encrypt 不允许用 HTTP-01 签泛域名，只能靠在 DNS 里放
       TXT 记录验证。域名托管在 Cloudflare，所以用 acme.sh 的 dns_cf 插件 + API token。

       失败不会留下半吊子状态：acme.sh 只有签发成功才会覆盖证书文件，中途失败原证书原封不动。"""
    if os.geteuid() != 0:
        print("  需要 root。"); return
    if not _installed():
        print("  还没装 AdGuard Home，先选 1 安装。"); return
    dom = _domain()
    if not dom:
        print("  没有域名，DoT 本来就用不了，这一步无意义。"); return
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; C = "\033[1;36m"; N = "\033[0m"
    cid = _selfdns_clientid()

    print("\n  " + "=" * 56)
    print(f"  让安卓 DoT 也能带 ClientID（签泛域名证书）")
    print("  " + "=" * 56)
    if _cert_wildcard_ok(dom):
        print(f"  {G}✓ 证书已经覆盖 *.{dom}，这一步做过了。{N}")
        if cid:
            print(f"    安卓「专用DNS」填：{G}{cid}.{dom}{N}")
        if not _wildcard_dns_ok(dom):
            print(f"  {R}⚠ 但 *.{dom} 的 DNS 通配记录没生效{N}——手机会解析不出这个主机名。")
            print(f"    去 Cloudflare 加一条：类型 {G}A{N}  名称 {G}*.{dom.split('.',1)[0] if dom.count('.')>1 else '*'}{N}"
                  f"  内容 {G}{_public_ip()}{N}  代理状态 {Y}仅DNS(灰云){N}")
        print(f"  Token 存在 {G}~/.acme.sh/account.conf{N}，约 90 天后自动续期还要用它。")
        print("  " + "-" * 56)
        print(f"  1 更换 Cloudflare API 令牌（旧的泄露了/被吊销了）")
        print(f"  2 删除 DoT ClientID 支持（证书改回单域名）")
        print(f"  0 返回")
        c = _ask("  选择: ").strip()
        if c == "1":
            _replace_cf_token()
        elif c == "2":
            _revert_wildcard(dom, cid)
        return
    print(f"  会把 acme 证书重签成同时覆盖 {G}{dom}{N} 和 {G}*.{dom}{N}。")
    print(f"  AdGuard 的证书路径不用改（还是 {ACME_CRT}），新证书是旧的超集，节点不受影响。")
    print(f"\n  {Y}需要你先准备两样：{N}")
    print(f"    1) Cloudflare 上加一条通配 A 记录，指向本机 {G}{_public_ip()}{N}，{Y}灰云(仅DNS){N}")
    print(f"       —— 没有它，手机连 {cid or '<ID>'}.{dom} 时根本解析不出地址")
    print(f"    2) Cloudflare API Token（{C}Zone:DNS:Edit{N} + {C}Zone:Zone:Read{N} 权限，限定到这个域名即可）")
    print(f"       在 CF 后台 → 右上头像 → My Profile → API Tokens → Create Token 生成")

    if _ask("\n  两样都准备好了，继续? [y/N]: ").lower() not in ("y", "yes"):
        print("  已取消，没有任何改动。"); return
    if not _wildcard_dns_ok(dom):
        print(f"\n  {R}❌ 检测不到 *.{dom} 的解析{N}——通配 A 记录还没加或还没生效。")
        print(f"     先去 Cloudflare 加好（灰云），等一两分钟再回来。签证书前就查，省得签完才发现没路。")
        return
    print(f"  {G}✓ 通配解析已生效{N}")

    token = _ask_secret("  粘贴 Cloudflare API Token（不回显，粘完直接回车）: ").strip()
    if not token:
        print("  没输入，已取消。"); return

    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print("  找不到 acme.sh —— 你的证书可能不是本脚本签的，不便自动接管。"); return

    print(f"\n  正在签发（DNS-01 验证，需要等 CF 记录传播，约 1-2 分钟）…")
    issue = (f"CF_Token='{token}' {acme} --issue --dns dns_cf "
             f"-d {dom} -d '*.{dom}' --keylength ec-256 --server letsencrypt")
    r = subprocess.run(issue, shell=True, text=True, capture_output=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode and "Cert success" not in out and "Domains not changed" not in out:
        print(f"\n  {R}❌ 签发失败，原证书原封未动，节点不受影响。{N}报错：")
        for ln in out.splitlines()[-12:]:
            print("     " + ln)
        print(f"\n  常见原因：Token 权限不够（要 Zone:DNS:Edit + Zone:Zone:Read）、"
              f"或 Token 没覆盖到 {dom} 所在的区域。")
        return

    # 续期时要连 AdGuardHome 一起重启：它跟 sing-box/xray 一样是启动时把证书读进内存的
    reload_hook = (" --reloadcmd '"
                   "systemctl reload nginx 2>/dev/null; "
                   "systemctl restart sing-box 2>/dev/null; "
                   "systemctl restart xray 2>/dev/null; "
                   "systemctl restart xy-sub 2>/dev/null; "
                   "systemctl restart AdGuardHome 2>/dev/null; true'")
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
    if not _cert_wildcard_ok(dom):
        print(f"\n  {R}❌ 证书导出后仍未覆盖 *.{dom}，没有生效。{N}原证书可能已被替换，"
              f"建议看 `openssl x509 -in {ACME_CRT} -noout -text | grep DNS:`。")
        return
    print(f"  {G}✓ 证书已覆盖 *.{dom}{N}")

    sh("systemctl restart AdGuardHome", check=False)
    time.sleep(3)
    if cid:
        (rc, ips), _, err = _q_dot(853, f"{cid}.{dom}", "example.com")
        if rc == 0 and ips:
            print(f"  {G}✓ 用 {cid}.{dom} 做 SNI 实测 DoT 查询成功{N}")
        else:
            print(f"  {Y}⚠ DoT 实测没通过（{err or 'rcode=' + str(rc)}）{N}——证书是对的，"
                  f"可能是 853 端口没开或 AdGuard 还没起好，稍后用菜单 7 自检再看。")

    print(f"\n  {G}做完了。{N}安卓「专用DNS」现在填：")
    print(f"      {G}{cid + '.' + dom if cid else '<ClientID>.' + dom}{N}")
    if not cid:
        print(f"  {Y}（还没有 ClientID —— 菜单 6 打开一次自建DNS写入订阅即生成）{N}")
    print(f"  之后就可以放心在「允许的客户端」里只填 ClientID：DoH 和 DoT 都带得上它。")
    print(f"  {Y}明文 53 仍然带不了{N}，电视/IoT 要么填固定 IP 进白名单，要么别用这台 DNS。")

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
        print(f"\n  ✓ 后台端口已改为 {new}。新后台地址(明文)：\033[1;32mhttp://{ip}:{new}\033[0m")
        print(f"  ▸ 防火墙：放行 \033[1;32m{new}/TCP\033[0m，关掉旧的 {cur}/TCP。")
        hp = _https_panel()
        if hp:
            print(f"  ▸ 你开了加密：更推荐用域名 HTTPS 进后台 \033[1;32mhttps://{hp[0]}:{hp[1]}\033[0m"
                  f"（走 {hp[1]} 口，不受本次改端口影响、密码加密）。")
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

def _selfdns_remove():
    """卸载 AdGuard 时：若之前用菜单 6 把自建 DNS 写进过订阅，就交给主脚本从订阅移除并刷新
       （没写入则主脚本内部静默跳过）。避免订阅里留着指向已卸载 AGH 的死 DoH。"""
    xy = BGP_DIR + "/xy-installer.py"
    if os.path.exists(xy):
        subprocess.run(f"python3 {xy} selfdns-off", shell=True)

def uninstall(refresh_sub=True):
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
    if refresh_sub:                                  # 单独卸载：把之前写进订阅的自建 DNS 一并撤掉并刷新
        _selfdns_remove()                            # （全量卸载时订阅整个删掉，不必刷新，refresh_sub=False）
    print("  ✓ 已卸载 AdGuard Home（sing-box/xray/节点不受影响）。")
    print("  记得把之前改过 DNS / 专用DNS 的设备改回自动/默认，否则它们会没 DNS 可用。")

# ===================== 自检 =====================
# 为什么要有这个：安卓「私人DNS」是严格模式——填了主机名就只用这一台 DoT，没有任何回落，
# 它一挂全机所有 App 立刻解析失败(errno=7)，且解析不了节点域名 → 连代理自救都做不到。
# 所以要能一键查出到底哪一环断了：服务 / 监听 / 证书 / 真实解析 / 访问控制 / 防火墙。

def _tls_block(txt):
    """从 AdGuardHome.yaml 里切出顶层 tls: 段（下面 enabled 等键才不会跟别处混）。"""
    m = re.search(r'(?m)^tls:[ \t]*$', txt)
    if not m:
        return ""
    out = []
    for line in txt[m.end():].splitlines():
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)

def _yaml_val(block, key):
    m = re.search(rf'(?m)^\s*{re.escape(key)}:[ \t]*(\S.*?)[ \t]*$', block)
    return m.group(1).strip('"\'') if m else ""

def _https_panel():
    """开了加密且有域名时，AGH 后台同时挂在 https://域名:port_https/ 上
       （同一个 HTTPS 端口：根路径 / 是后台、/dns-query 是 DoH）。
       返回 (域名, https端口)；没开加密 / 没域名 / 服务器名对不上 / 端口读不到时返回 None。"""
    dom = _domain()
    if not dom:
        return None
    try: txt = open(_agh_yaml()).read()
    except OSError: return None
    tb = _tls_block(txt)
    if _yaml_val(tb, "enabled") != "true":
        return None
    sname = _yaml_val(tb, "server_name")
    if sname and sname != dom:
        return None
    p = int(_yaml_val(tb, "port_https") or 0)
    return (dom, p) if p else None

def _yaml_list(txt, key):
    """读 YAML 里的一个列表；键不存在返回 None，空列表返回 []。"""
    m = re.search(rf'(?m)^(\s*){re.escape(key)}:[ \t]*(\[[ \t]*\])?[ \t]*$', txt)
    if not m:
        return None
    if m.group(2):
        return []
    indent, items = len(m.group(1)), []
    for line in txt[m.end():].splitlines():
        if not line.strip():
            continue
        cur, st = len(line) - len(line.lstrip()), line.strip()
        if st.startswith("- ") and cur > indent:
            items.append(st[2:].strip())
        elif cur <= indent:
            break
    return items

def _dns_packet(name):
    pkt = struct.pack(">HHHHHH", secrets.randbelow(65536), 0x0100, 1, 0, 0, 0)
    for part in name.split("."):
        pkt += bytes([len(part)]) + part.encode()
    return pkt + b"\x00" + struct.pack(">HH", 1, 1)

def _dns_parse(data):
    """返回 (rcode, [A记录IP])；解析不出来返回 (-1, [])。"""
    try:
        rcode = data[3] & 0x0F
        ancount = struct.unpack(">H", data[6:8])[0]
        i = 12
        while data[i]:                                  # 跳过 question 的 QNAME
            i += data[i] + 1
        i += 5                                          # 0x00 + QTYPE + QCLASS
        ips = []
        for _ in range(ancount):
            while True:                                 # 跳过 answer 的 NAME（可能是压缩指针）
                l = data[i]
                if l & 0xC0 == 0xC0:
                    i += 2; break
                i += 1
                if l == 0:
                    break
                i += l
            rtype = struct.unpack(">H", data[i:i + 2])[0]
            rdlen = struct.unpack(">H", data[i + 8:i + 10])[0]
            i += 10
            if rtype == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in data[i:i + rdlen]))
            i += rdlen
        return rcode, ips
    except Exception:
        return -1, []

def _q_plain(port, name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(6)
    try:
        s.sendto(_dns_packet(name), ("127.0.0.1", port))
        return _dns_parse(s.recvfrom(4096)[0]), ""
    except Exception as e:
        return (-1, []), f"{type(e).__name__}: {e}"
    finally:
        s.close()

def _tls_conn(port, sni):
    """连本机 port，但按 sni 校验证书（校验的是证书内容，跟连 127.0.0.1 不冲突）。"""
    raw = socket.create_connection(("127.0.0.1", port), timeout=8)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=sni)
    except Exception:
        raw.close(); raise

def _q_dot(port, sni, name):
    """DoT 查询：TLS 里发 2 字节长度前缀 + DNS 报文。顺带把服务端证书带回来。"""
    try:
        c = _tls_conn(port, sni)
    except Exception as e:
        return (-1, []), None, f"{type(e).__name__}: {e}"
    cert = c.getpeercert()          # 必须握手后立刻取：连接一关再取会抛 handshake not done
    try:
        q = _dns_packet(name)
        c.sendall(struct.pack(">H", len(q)) + q)
        head = c.recv(2)
        if len(head) < 2:
            return (-1, []), cert, "服务端没回数据"
        need = struct.unpack(">H", head)[0]
        buf = b""
        while len(buf) < need:
            chunk = c.recv(need - len(buf))
            if not chunk:
                break
            buf += chunk
        return _dns_parse(buf), cert, ""
    except Exception as e:
        return (-1, []), cert, f"{type(e).__name__}: {e}"
    finally:
        try: c.close()
        except Exception: pass

def _q_doh(port, sni, name):
    try:
        c = _tls_conn(port, sni)
    except Exception as e:
        return -1, None, f"{type(e).__name__}: {e}"
    cert = c.getpeercert()          # 同上：服务端 Connection: close 后就取不到了
    try:
        d = base64.urlsafe_b64encode(_dns_packet(name)).rstrip(b"=").decode()
        c.sendall((f"GET /dns-query?dns={d} HTTP/1.1\r\nHost: {sni}\r\n"
                   "Accept: application/dns-message\r\nConnection: close\r\n\r\n").encode())
        buf = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        first = buf.split(b"\r\n", 1)[0].decode(errors="replace")
        m = re.search(r"\s(\d{3})\s", first + " ")
        return (int(m.group(1)) if m else -1), cert, ""
    except Exception as e:
        return -1, cert, f"{type(e).__name__}: {e}"
    finally:
        try: c.close()
        except Exception: pass

def _cert_days_left(peercert):
    """优先用握手拿到的证书（那才是真正在服务的），拿不到就读证书文件。"""
    exp = ""
    if peercert and peercert.get("notAfter"):
        exp = peercert["notAfter"]
    elif os.path.exists(ACME_CRT):
        out = sh(f"openssl x509 -enddate -noout -in {ACME_CRT} 2>/dev/null")
        exp = out.split("=", 1)[1].strip() if "=" in out else ""
    if not exp:
        return None, ""
    try:
        return int((ssl.cert_time_to_seconds(exp) - time.time()) // 86400), exp
    except Exception:
        return None, exp

def selfcheck():
    """一键自检：服务 / 监听 / 证书 / 真实解析 / 访问控制 / 本机防火墙。只读不改任何配置。"""
    G, Y, R, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    ok, warn, bad = f"{G}✓{N}", f"{Y}⚠{N}", f"{R}✗{N}"
    print("\n" + "=" * 60 + "\n  自建DNS 自检（只读，不会改任何配置）\n" + "=" * 60)
    if not _installed():
        print(f"  {bad} 还没装 AdGuard Home——先回菜单选『1 安装』。"); return
    dom, ip = _domain(), _public_ip()
    fix = []                                     # 收集结论里要给的处置建议

    # ---- 1 服务 ----
    print("\n  【1/7 服务】")
    if _running():
        print(f"    {ok} AdGuardHome 运行中")
    else:
        print(f"    {bad} AdGuardHome {R}没在运行{N} —— 所有指到它的设备都会没 DNS")
        print("       看原因: journalctl -u AdGuardHome -n 30 --no-pager")
        fix.append("先把服务起来: systemctl restart AdGuardHome")

    try: txt = open(_agh_yaml()).read()
    except OSError:
        txt = ""
        print(f"    {warn} 读不到 {_agh_yaml()}（可能还没走完网页初始化向导）")
    tb = _tls_block(txt)
    enc_on = _yaml_val(tb, "enabled") == "true"
    sname  = _yaml_val(tb, "server_name")
    p_dot  = int(_yaml_val(tb, "port_dns_over_tls") or 0)
    p_doh  = int(_yaml_val(tb, "port_https") or 0)
    web    = _current_web_port()

    # ---- 2 监听 ----
    print("\n  【2/7 端口监听】")
    b53 = _port_busy(53)
    if "AdGuardHome" in b53:
        print(f"    {ok} 53   明文DNS  由 AdGuardHome 监听")
    elif b53:
        print(f"    {bad} 53   被 {R}{b53}{N} 占着，不是 AdGuardHome —— 明文DNS用不了")
        fix.append("腾 53：回菜单选『4 腾出 53 端口』")
    else:
        print(f"    {warn} 53   没人监听 —— 明文DNS用不了（AGH 可能没配 53 或没起来）")
    if web:
        print(f"    {ok} {web:<5}网页后台(明文) http://{ip}:{web}")
    if enc_on and dom and p_doh and (not sname or sname == dom):
        print(f"    {ok} {p_doh:<5}网页后台(加密) {G}https://{dom}:{p_doh}{N}  {Y}← 推荐这样进，密码不明文{N}")
    if not enc_on:
        print(f"    {warn} 加密(DoT/DoH) {Y}没开{N} —— 安卓「私人DNS」用不了，只能用明文 53")
        if dom:
            fix.append("要用安卓私人DNS：后台 → 设置 → 加密设置，按菜单『3 查看状态』里的第二步填")
    else:
        for p, label in ((p_dot, "853  DoT "), (p_doh, "10443 DoH")):
            if not p:
                continue
            who = _port_busy(p)
            tag = ok if "AdGuardHome" in who else (bad if who else warn)
            note = "由 AdGuardHome 监听" if "AdGuardHome" in who else (f"被 {who} 占" if who else "没人监听")
            print(f"    {tag} {label} {note}")

    # ---- 3 证书 ----
    print("\n  【3/7 证书】")
    peer = None
    if not dom:
        print(f"    {warn} 装节点时没用域名 —— 加密用不了，跳过")
    elif not enc_on:
        print(f"    {warn} 加密没开，跳过")
    else:
        if sname and sname != dom:
            print(f"    {bad} 加密设置里的「服务器名称」是 {R}{sname}{N}，和你的域名 {dom} 对不上")
            fix.append(f"把加密设置里的服务器名称改成 {dom}")
        else:
            print(f"    {ok} 服务器名称 {sname or dom}")

    # ---- 4 真实解析 ----
    print("\n  【4/7 真实解析测试】(本机直连，绕开公网)")
    probe = "www.qq.com"
    if not ("AdGuardHome" in b53 or (enc_on and dom and (p_dot or p_doh))):
        print(f"    {warn} 没有可测的服务（53 不在 AGH 手上，加密也没开）—— 先把上面两步弄好")
    if "AdGuardHome" in b53:
        (rc, ips), err = _q_plain(53, probe)
        if rc == 0 and ips:
            print(f"    {ok} 明文 53  解析 {probe} → {ips[0]}")
        else:
            print(f"    {bad} 明文 53  {R}解析失败{N} {err or f'rcode={rc}'}")
            fix.append("53 解析不出来：看后台『设置 → DNS设置 → 上游DNS服务器』是否填了能用的上游")
    if enc_on and dom and p_dot:
        (rc, ips), peer, err = _q_dot(p_dot, dom, probe)
        if rc == 0 and ips:
            print(f"    {ok} DoT {p_dot}  握手+解析都正常 → {ips[0]}   {G}(安卓私人DNS 可用){N}")
        else:
            print(f"    {bad} DoT {p_dot}  {R}失败{N}: {err or f'rcode={rc}'}")
            print(f"       {Y}这一条挂掉 = 填了私人DNS 的安卓机会整机断网{N}")
            if "CERTIFICATE" in err.upper() or "SSLCert" in err:
                fix.append("DoT 证书校验没过：证书过期或填的不是 acme 真证书，去加密设置重填 "
                           f"{ACME_CRT} / {ACME_KEY}")
            else:
                fix.append("DoT 连不上：确认加密设置里 DoT 端口填了、服务已重启")
    if enc_on and dom and p_doh:
        code, pc, err = _q_doh(p_doh, dom, probe)
        peer = peer or pc
        if code == 200:
            print(f"    {ok} DoH {p_doh} 正常 (HTTP 200)")
        else:
            print(f"    {bad} DoH {p_doh} {R}失败{N}: {err or f'HTTP {code}'}")
    days, exp = _cert_days_left(peer)
    if days is not None:
        tag = ok if days > 20 else (warn if days > 7 else bad)
        print(f"    {tag} 证书还有 {days} 天到期（{exp}）")
        if days <= 20:
            fix.append(f"证书 {days} 天后到期——续期后记得 systemctl restart AdGuardHome 让 AGH 重新加载")
    # 顺手验一下广告到底拦没拦
    if "AdGuardHome" in b53:
        (rc, ips), _ = _q_plain(53, "doubleclick.net")
        if rc == 3 or (ips and ips[0] in ("0.0.0.0", "127.0.0.1")) or (rc == 0 and not ips):
            print(f"    {ok} 广告过滤生效（doubleclick.net 已被拦）")
        elif rc == 0 and ips:
            print(f"    {warn} doubleclick.net 没被拦 → {ips[0]}；后台『过滤器』里确认拦截名单是开的")

    # ---- 5 访问控制（这次把你锁在外面的就是它）----
    print("\n  【5/7 访问控制】")
    allow = _yaml_list(txt, "allowed_clients")
    deny  = _yaml_list(txt, "disallowed_clients")
    cid = _selfdns_clientid()
    wild = _cert_wildcard_ok(dom)
    if allow:
        # ClientID 是域名标签（只有字母数字和连字符）；带点或斜杠的是 IP/CIDR。
        # 两者性质完全不同：ClientID 与 IP 无关，移动网络随便切；IP 白名单才怕出口变化。
        ids = [x for x in allow if re.fullmatch(r"[A-Za-z0-9-]+", x)]
        ips = [x for x in allow if x not in ids]
        if ids:
            print(f"    {ok} 用 ClientID 限制: {G}{', '.join(ids)}{N} —— 与 IP 无关，切 WiFi/4G 都不受影响")
            if cid and cid not in ids:
                print(f"    {bad} {R}但当前 ClientID 是 {cid}，不在名单里{N} —— 订阅里的 DoH 会被拒！")
                fix.append(f"把当前 ClientID 加进「允许的客户端」: {cid}")
        if ips:
            print(f"    {warn} 名单里还有 IP/CIDR: {', '.join(ips)}")
            print(f"       {Y}这些是按来源 IP 放行的；移动网络出口 IP 会变，对应设备可能被拒。{N}")
        print(f"    {warn} 白名单一开，{Y}明文 53 一律被拒{N}（协议本身没有 ID 机制）—— 电视/IoT 会没 DNS")
        if not wild:
            print(f"    {bad} {R}安卓 DoT 也会被拒{N}：没签泛域名证书，ID 传不进 SNI")
            fix.append("要用安卓 DoT 就回菜单选『8 让 DoT 也能带 ClientID』签泛域名证书")
        elif cid:
            print(f"    {ok} 安卓 DoT 可用（已签泛域名证书）—— 手机填 {G}{cid}.{dom}{N}")
    else:
        print(f"    {warn} 没设允许列表 —— {R}对全公网开放，谁扫到都能当免费解析器用{N}")
        print(f"       {Y}明文 53 尤其要紧：UDP 可伪造源 IP，是 DNS 放大攻击的经典跳板。{N}")
        if cid:
            fix.append(f"关掉开放解析器：「允许的客户端」填入 ClientID {cid}"
                       + ("" if wild else "（但会挡掉安卓DoT，除非先用菜单 8 签泛域名证书）"))
    if deny:
        print(f"    {warn} 拒绝列表: {', '.join(deny)}")

    # ---- 6 后台暴露面 ----
    # 「允许的客户端」只管 DNS 请求，管不到网页后台。而后台常常挂在【两个】公网入口上，
    # 其中 HTTPS 那个和 DoH 同端口、根路径就是登录页——很容易被忽略，扫到就是撞密码。
    print("\n  【6/7 后台暴露面】")
    addr = re.search(r'(?m)^\s*address:\s+(\S+):(\d+)\s*$', txt)
    bind = addr.group(1) if addr else ""
    entries = []
    if web:
        entries.append(("明文 HTTP", f"http://{ip}:{web}", bind not in ("127.0.0.1", "::1")))
    if enc_on and dom and p_doh and (not sname or sname == dom):
        entries.append(("HTTPS（与 DoH 同端口，根路径即登录页）", f"https://{dom}:{p_doh}/", True))
    pub = [e for e in entries if e[2]]
    if not entries:
        print(f"    {warn} 没读到后台监听地址")
    for label, url, exposed in entries:
        mark = bad if (exposed and label.startswith("明文")) else (warn if exposed else ok)
        where = f"{R}公网可达{N}" if exposed else f"{G}仅本机{N}"
        print(f"    {mark} {label}: {url}  [{where}]")
    if len(pub) > 1:
        print(f"    {warn} {Y}后台有 {len(pub)} 个公网入口{N}——把 {web} 藏起来也没用，"
              f"{p_doh} 的根路径照样是登录页")
    if any(e[2] and e[0].startswith("明文") for e in entries):
        fix.append(f"别再用明文入口登录后台（密码明文过网），改用 https://{dom}:{p_doh}/"
                   if (enc_on and dom and p_doh) else "后台是明文 HTTP，密码会明文过网")
    if pub:
        print(f"    {Y}挡在后台前面的只有你的登录密码{N} —— 弱密码撞出来就全丢了，务必设强密码。")
        print(f"       想彻底不暴露：把后台绑到 127.0.0.1，管理时走 SSH 隧道")
        print(f"       ssh -L {web or 3000}:127.0.0.1:{web or 3000} root@{ip}   然后浏览器开 http://127.0.0.1:{web or 3000}")
        print(f"       {Y}注意 {p_doh or 10443} 那个入口关不掉{N}——它跟着加密走，是 AdGuard 自带的。")

    # ---- 7 本机防火墙 ----
    print("\n  【7/7 本机防火墙】")
    hit = False
    u = sh("ufw status 2>/dev/null")
    if "Status: active" in u:
        hit = True
        print(f"    {warn} ufw 已启用，确认这些端口放行了：53(UDP+TCP)"
              + (f" / {p_dot}(TCP) / {p_doh}(TCP)" if enc_on else "")
              + (f" / {web}(TCP)" if web else ""))
    if sh("firewall-cmd --state 2>/dev/null") == "running":
        hit = True
        print(f"    {warn} firewalld 正在运行，确认上述端口已放行")
    if not hit:
        print(f"    {ok} 本机没启用 ufw/firewalld")
    print(f"    {Y}提醒：本机没拦 ≠ 外面能连。{N}云商（DMIT/甲骨文等）的安全组是另一层，")
    print( "       上面 4/6 全绿但手机连不上，那就一定是安全组没放行。")

    # ---- 结论 ----
    print("\n  " + "-" * 56)
    if fix:
        print(f"  {Y}要处理的事：{N}")
        for i, f in enumerate(fix, 1):
            print(f"    {i}. {f}")
    else:
        print(f"  {G}全部正常。{N}")
    print(f"\n  {Y}万一手机整机没网、又连不上代理自救：{N}")
    print( "    设置 → 网络和互联网 → 私人DNS → 改成「关闭」或「自动」，解析立刻恢复。")

def _selfdns_toggle():
    """把"自建DNS写入订阅"的开关交给主脚本处理（订阅生成/刷新都在主脚本里）。"""
    xy = BGP_DIR + "/xy-installer.py"
    if not os.path.exists(xy):
        print("  没找到主脚本(xy-installer.py)，请从主面板进入本菜单。"); return
    subprocess.run(f"python3 {xy} selfdns-toggle", shell=True)

def menu():
    while True:
        print("\n" + "=" * 60 + "\n自建DNS · AdGuard Home（全设备去广告）\n" + "=" * 60)
        st = "已安装 " + ("运行中 ✓" if _running() else "未运行 ✗") if _installed() else "未安装"
        print("  当前状态:", st)
        print("-" * 60)
        print("  1 安装（装 AdGuard Home + 起服务，之后网页后台点几下完成设置）")
        print("  2 卸载（彻底移除，不动节点；若写过订阅会自动从订阅撤掉自建DNS并刷新）")
        print("  3 使用方法（当前配置信息 + 设备怎么设置，照着填即可）")
        print("  4 腾出 53 端口（被 systemd-resolved 占用时用）")
        print("  5 改后台端口（随机 2000-5000 / 自定义，防扫描；带回滚）")
        print("  6 把自建DNS写入订阅配置（开/关：第一次写入·再点移除，自动刷新）")
        print("  7 自检（服务/端口/证书/解析/访问控制 一次查清，只读不改）")
        print("  8 让 DoT 也能带 ClientID（签泛域名证书；设了白名单后安卓专用DNS仍可用）")
        print("  0 退出")
        c = _ask("选择: ").strip()
        if c == "1":   install()
        elif c == "2": uninstall()
        elif c == "3": status()
        elif c == "4": free_port53()
        elif c == "5": change_web_port()
        elif c == "6": _selfdns_toggle()
        elif c == "7": selfcheck()
        elif c == "8": dot_clientid()
        elif c in ("0", ""):
            return

def main():
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    if act == "remove":                              # 主脚本整体卸载时可调用（订阅整体删，不必刷新）
        uninstall(refresh_sub=False)
    else:
        menu()

if __name__ == "__main__":
    main()

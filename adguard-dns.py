#!/usr/bin/env python3
# adguard-dns.py —— 一键架设 AdGuard Home（自己的 DNS 解析服务器，兼带域名级广告过滤）
# 独立文件，nodekit 主脚本(xy-installer.py)通过子进程调用：
#   python3 adguard-dns.py            交互菜单（安装 / 卸载 / 查看）
#
# 定位：给全部设备一台【自己的解析服务器】—— 解析不再经运营商 DNS，不挂代理的设备
#       (电视/盒子/IoT/路由器) 和安卓「专用DNS」也能用加密 DNS。广告过滤是它【附带】的
#       一层，不是全部：DNS 只能按域名拦，广告跟正文同域名的（视频贴片、信息流推广）
#       拦不掉 —— 别把它当成「装上就没广告」。
#   - 挂代理的设备本来就靠订阅里的 reject 规则集拦广告，这个是补「没挂代理」的场景。
#   - AdGuard Home 是网页后台管理的软件：这里做「一键装好起服务 + 一键干净卸载」，
#     设管理密码、微调过滤名单在它的网页后台点几下完成（广告过滤默认即开）。
#   - 加密走 DoT(853，自建 DNS 自己的域名和证书，10/29 起跟节点分开；老装法复用节点的 acme 证书)：安卓「专用 DNS」填域名即可全系统生效；
#     DoH 需要 443（被 reality/nginx 占了）故不用。明文 53 给装不了 DoT 的设备（电视/IoT）。
#   - 不动 sing-box/xray/节点：独立服务，卸载彻底、互不影响。
#   - 卸载(菜单10)是整套撤干净：服务+数据、订阅里写入的自建 DoH、为 DoT 签的泛域名证书、
#     腾 53 时改的本机解析、ClientID 记录，一次回到没装过的样子。菜单 5 里的「删除 DoT」
#     只撤泛域名证书这一项，AdGuard 本体继续跑。
import os, re, sys, time, json, shlex, socket, shutil, secrets, struct, base64, ssl, subprocess, urllib.request

BGP_DIR = "/etc/bgpeer"
HOST_FILE = BGP_DIR + "/sub.host"                 # 主脚本存的 host（域名或 IP）
ACME_CRT, ACME_KEY = "/etc/ssl/sb/acme.crt", "/etc/ssl/sb/acme.key"   # 主脚本 acme 证书
CERT_META = BGP_DIR + "/cert.json"                # 主脚本记的「这张证书该是什么样」（见 _cert_meta_set）
# 自建 DNS 自己的域名和证书（仓库主人 10/29「各个域名分开互不干预」）：新装起跟节点 / Emby 分开，
# 节点证书不再替它背 *.节点域名。老装法没有 AGH_DOMAIN_FILE，照旧共用 ACME_CRT（_domain / _crt 自动退回）。
AGH_DOMAIN_FILE = BGP_DIR + "/agh.domain"
AGH_CERT_DIR = "/etc/ssl/agh"
AGH_CRT, AGH_KEY = AGH_CERT_DIR + "/agh.crt", AGH_CERT_DIR + "/agh.key"
AGH_DIR = "/opt/AdGuardHome"
AGH_BIN = AGH_DIR + "/AdGuardHome"
RESOLVED_DIR    = "/etc/systemd/resolved.conf.d"
RESOLVED_DROPIN = RESOLVED_DIR + "/adguard.conf"  # 腾 53 时写的 drop-in（卸载时据此还原本机解析）
AGH_INSTALL = "https://raw.githubusercontent.com/AdguardTeam/AdGuardHome/master/scripts/install.sh"
WEB_PORT = 3000

# 跟 xy-installer.py 同一套（菜单 13 是子进程跑本文件，主脚本那份管不到这里）
# 【菜单编号统一高亮】（独占一行的「  3. xx」和并排的「1. a   2. b」都算）仓库主人：「像这种被选项数字 0-20 可以做成高亮的吗，包括里面的选项数字，所有的只要是被选项
# 数字都变成高亮……跟下面的 bgpeer 一样的颜色」。不去改几十处菜单代码：屏幕上每一行「行首空格 + 1~2 位数字 + 点 +
# 空格」的，数字那段换成 bgpeer 同色（粗体绿 1;32）。只在直接印到终端时上色 —— 进日志 / 管道 / 测试的照旧是纯文本。
import builtins as _builtins
import sys as _sys
_MENU_NUM_RE = re.compile(r"(^[ \t]*|(?<=  ))(\d{1,2}\.)(?=\s)", re.M)   # 行首的，或并排的（前面两个空格）


_MENU_SEEN = set()                        # 这一屏印出来的编号；问「请选择」时打的是这里面的，才回头染绿（见 _ask_paint）


def _menu_num_paint(m):
    _MENU_SEEN.add(m.group(2)[:-1])
    return f"{m.group(1)}\033[1;32m{m.group(2)}\033[0m"


def _ask_paint(prompt_shown, v, seen):
    """打完回车之后：打的是菜单上有的编号 → 把这一行重印一遍、数字染成同色；菜单上没有的照旧普通颜色。

    仓库主人：「这个输入只有上面预设好了的才变成高亮，如果你输入的上面是没有的应该就是普通的颜色」。
    打字那一刻是终端自己回显的，还不知道最后打什么，所以只能回车后回头重印。整行放不下一行（会折行）
    就不重印，免得把上一行盖花。"""
    try:
        if not v or v not in seen or not _sys.stdout.isatty():
            return
        cols = shutil.get_terminal_size((60, 20)).columns
        wide = sum(2 if ord(ch) > 0x2E80 else 1 for ch in prompt_shown + v)
        if wide >= cols:
            return
        _builtins.print(f"\033[1A\r\033[2K{prompt_shown}\033[1;32m{v}\033[0m", flush=True)
    except Exception:
        pass


# 【不带点的编号也算】子菜单大多写成「  1 修改配置」「1 作者模板   2 自定义模板   0 返回」，上面那条只认
# 「1.」，于是只有主菜单亮、里面几层全是白的。仓库主人：「我当时是想把所有的需要输入按钮的都要做成高亮」。
# 不带点的数字太容易撞上普通文字（「  80 端口被占」「  5 条候选」），所以收得紧：
#   · 只认 0~29；后面空格隔开紧跟中文（量词「条个台次秒分……」不算），
#     独占一行的单个数字后面跟英文也算（「  1 smux 开关」「  4 Emby 证书」）；
#   · 并排的要前面至少三个空格（菜单项之间都是三四个空格），或紧跟中文冒号（「计费方式：1 双向相加」）。
_MENU_BARE_RE = re.compile(r"(^[ \t]+|(?<=   )|(?<=：))([12]?\d)( +)(?=(\S))", re.M)
_MENU_UNIT = "条个台次秒分小天行项端倍位张份组路周月年号字块元"


def _menu_bare_paint(m):
    head, num, gap, nxt = m.groups()
    cjk = "\u4e00" <= nxt <= "\u9fff" and nxt not in _MENU_UNIT
    word = bool(head) and len(num) == 1 and nxt.isascii() and nxt.isalpha()
    if not (cjk or word):
        return m.group(0)
    _MENU_SEEN.add(num)
    return f"{head}\033[1;32m{num}\033[0m{gap}"


def _menu_paint(s):
    return _MENU_BARE_RE.sub(_menu_bare_paint, _MENU_NUM_RE.sub(_menu_num_paint, s))


def print(*args, **kw):                   # noqa: A001 —— 故意盖住内置 print，见上
    try:
        f = kw.get("file") or _sys.stdout
        if args and isinstance(args[0], str) and f is _sys.stdout and f.isatty():
            args = (_menu_paint(args[0]),) + args[1:]
    except Exception:
        pass
    return _builtins.print(*args, **kw)


# ── 屏上排版小工具（同 xy-installer.py，CLAUDE.md 第五节「排版要整齐干净」）──────────
def _hl(code, text):
    return f"\033[{code}m{text}\033[0m" if _sys.stdout.isatty() else text

def ui_title(text):
    print("\n" + "=" * 60 + f"\n  {text}\n" + "=" * 60)

def ui_line():
    print("-" * 60)

def ui_items(items):
    for num, name, cur in items:
        print(f"  {num}. {name}" + (f"　当前：{cur}" if cur not in (None, "") else ""))

def ui_tip(text):
    print("  " + _hl("1;33", f"提示：{text}"))

def ui_kv(label, value):
    print(f"  {_hl('36', label + '：')}{value}")

def ui_url(label, url, code="1;32"):
    print(_hl("1;33", f"▸ {label}"))
    print(_hl(code, url) if url else _hl("2", "（还没有）"))


def sh(cmd, check=False):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()

def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。"""
    try:
        with open("/dev/tty", "r") as t:
            _seen = set(_MENU_SEEN)
            _MENU_SEEN.clear()
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            v = line.rstrip("\n").strip()
            _ask_paint(prompt, v, _seen)
            return v
    except (OSError, EOFError):
        return input(prompt).strip()

def _host():
    try: return open(HOST_FILE).read().strip()
    except OSError: return ""

def _own_domain():
    """自建 DNS 自己的域名（新装法）；老装法 / 没设返回 ''。"""
    try:
        d = open(AGH_DOMAIN_FILE).read().strip().lower()
    except OSError:
        return ""
    return d if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", d) else ""

def _domain():
    """自建 DNS 对外的域名（DoT 需要证书 + 域名）：有自己的用自己的，老装法跟节点共用；都没有返回 ''。"""
    own = _own_domain()
    if own:
        return own
    h = _host()
    return h if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", h or "") else ""

def _crt():
    """AdGuard 用的证书：有自己的域名就是自己那张，老装法是跟节点共用的 ACME_CRT。"""
    return AGH_CRT if _own_domain() else ACME_CRT

def _key():
    return AGH_KEY if _own_domain() else ACME_KEY

# 出网一律用中性 UA：默认的 Python-urllib/3.x 等于告诉沿途每一跳「这是个脚本」。
# 仓库里别的脚本早就统一过，这一份当时漏了。
HTTP_UA = "curl/8.5.0"


def _fetch(url, timeout=20):
    """GET 一个 URL，返回 bytes；失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _public_ip():
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        b = _fetch(u, timeout=8)
        if b:
            return b.decode(errors="replace").strip()
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
    return os.path.exists(_crt()) and os.path.exists(_key())

def install():
    if os.geteuid() != 0:
        print("  需要 root 运行。"); return
    if _installed():
        if not os.path.exists(_agh_yaml()):           # 装好了、向导还没走完（10/30 GCP：两问被缓冲的回车顶掉了）
            print("  AdGuard Home 装好了，后台账号还没设：现在替你设")
            if not _auto_setup():
                _first_setup()
            return
        print("  AdGuard Home 已经装过了。要重装请先『10 卸载』再装。")
        _usage(); return
    dom = _setup_own_domain()
    if not dom:
        ui_tip("没有域名：安卓专用 DNS（加密 DoT）用不了，设备只能填本机 IP 走明文 53")
        if _ask("  仍然继续安装（只用明文 53）? [y/N]: ").lower() not in ("y", "yes"):
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
    if not _auto_setup():   # 替你走完网页向导：随机后台端口 + 你设的账号密码（仓库主人 10/30）
        _first_setup()      # 没走成：照旧网页向导，统一用默认 3000（先保证进得去），装好后菜单 7 改端口

def _setup_own_domain():
    """装之前给自建 DNS 一个它自己的域名、签一张它自己的证书（域名 + *.域名，DoT 带 ClientID 要泛域名），
       跟节点 / Emby 完全分开（仓库主人 10/29：「各个域名分开互不干预，好操作」）。返回域名；回车 / 没弄成返回 ''。
       换域名 = 卸了重装（仓库主人：「大不了重做」），所以不另设菜单项。
       两条 A 记录先查、签之前就查，省得签完才发现手机找不到这台。令牌：acme.sh 里存着就用它（续期也靠它），
       签不出来多半是那个令牌没管到这个域名的区域——让去 CF 编辑令牌把区域加上，不另贴一个顶掉（acme.sh 只存一个，
       顶掉以后节点 / Emby 那几张的续期就签不出来了）。"""
    R, G_, Y, N = "\033[1;31m", "\033[1;32m", "\033[1;33m", "\033[0m"
    ui_title("自建 DNS 的域名（跟节点分开）")
    nd = (_node_domain() or "").lower()
    # 像 Emby 那样先认出节点域名（仓库主人 10/29）：默认给 dns.节点的根域名——同一个令牌管得到，回车就行
    dft = f"dns.{_root_of(nd)}" if nd else ""
    if nd:
        ui_kv("节点域名", nd)
    raw = _ask(f"  域名（回车 = {dft}，输 n = 不用加密）: " if dft else "  域名（回车 = 不用加密）: ")
    raw = raw.strip().lower().rstrip(".")
    if raw in ("n", "no") or not (raw or dft):
        return ""
    dom = raw or dft
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", dom):
        print(f"  {R}✗ 域名格式不对{N}"); return ""
    # 跟节点同一个名字：acme.sh 按主域名存记录，会把节点那条（续期装到节点证书）顶掉——换个子域名
    if dom == nd or os.path.isdir(os.path.expanduser(f"~/.acme.sh/{dom}_ecc")):
        print(f"  {R}✗ {dom} 已经有一张证书在用（节点 / 别的服务的），换一个只给 DNS 用的子域名{N}"); return ""
    ip = _public_ip()
    print(_hl("1;36", f"[Cloudflare 里两条 A 记录 → {ip}，仅 DNS（灰云）]"))
    print(dom)
    print("*." + dom)
    base = _resolve4(dom)
    if ip not in base:
        print(f"  {R}✗ {dom} 解析到 {', '.join(base) or '（查不到）'}，不是本机{N}")
        ui_tip("建好、等生效（几分钟）再来装"); return ""
    okw, why = _wildcard_dns_check(dom)
    if not okw:
        print(f"  {Y}⚠ {why}：安卓专用 DNS 带不了 ClientID，先装着，建好就能用{N}")
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print(f"  {R}✗ 没有 acme.sh（节点脚本装证书时会装上）{N}"); return ""
    conf = os.path.expanduser("~/.acme.sh/account.conf")
    have_tok = os.path.exists(conf) and re.search(r"(?m)^SAVED_CF_Token=", open(conf).read())
    env = ""
    if not have_tok:
        _cf_token_steps(dom)
        tok = _ask("  粘贴 Cloudflare API Token（粘完回车）: ").strip()
        if not tok:
            print("  没填令牌，这次只用明文 53"); return ""
        env = f"CF_Token={shlex.quote(tok)} "
    print(f"  签 {dom} + *.{dom}（DNS 验证，约 1-2 分钟）…")
    r = subprocess.run(f"{env}{acme} --issue --dns dns_cf -d {dom} -d '*.{dom}' "
                       f"--keylength ec-256 --server letsencrypt", shell=True, text=True, capture_output=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode and "Cert success" not in out and "Domains not changed" not in out:
        print(f"  {R}✗ 证书没签下来{N}")
        for ln in out.splitlines()[-4:]:
            print("     " + ln)
        if have_tok and "invalid domain" in out:
            ui_tip(f"acme.sh 存的令牌管不到 {_root_of(dom)}：CF → API 令牌 → 编辑它 → 区域资源加上这个域名，别新建一个顶掉")
        return ""
    os.makedirs(AGH_CERT_DIR, exist_ok=True)
    os.chmod(AGH_CERT_DIR, 0o700)                        # 私钥：只给 root
    sh(f"{acme} --install-cert -d {dom} --ecc --fullchain-file {AGH_CRT} --key-file {AGH_KEY} "
       f"--reloadcmd 'systemctl try-restart AdGuardHome 2>/dev/null; true'")
    if not os.path.exists(AGH_CRT):
        print(f"  {R}✗ 证书没装上（{AGH_CRT}）{N}"); return ""
    os.makedirs(BGP_DIR, exist_ok=True)
    open(AGH_DOMAIN_FILE, "w").write(dom + "\n")
    print(f"  {G_}✔ 自建 DNS 的证书签好了：{dom} + *.{dom}（自己续期，不碰节点证书）{N}")
    return dom

def _own_cert_remove(dom):
    """卸载时撤掉自建 DNS 自己那张证书：acme.sh 不再续期它，文件和域名记录删掉。节点证书一个字节不动。"""
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if os.path.exists(acme):
        sh(f"{acme} --remove -d {dom} --ecc")
        shutil.rmtree(os.path.expanduser(f"~/.acme.sh/{dom}_ecc"), ignore_errors=True)
    shutil.rmtree(AGH_CERT_DIR, ignore_errors=True)
    try: os.remove(AGH_DOMAIN_FILE)
    except OSError: pass

def _agh_api(path, body=None, port=None, timeout=5, auth=None):
    """本机 AdGuard 的接口（127.0.0.1，不出网）→ (状态码, 回包文字)；连不上返回 (0, 原因)。port 不给 = 向导的 WEB_PORT。
       auth=(用户名, 密码)：带上基本认证（装好以后的接口要登录）。"""
    hdr = {"Content-Type": "application/json"} if body is not None else {}
    if auth:
        hdr["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{port or WEB_PORT}{path}",
                                 data=None if body is None else json.dumps(body).encode(), headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return 0, str(e)

def _flush_typeahead():
    """丢掉装的那一两分钟里误按、还没读走的回车（10/30 GCP：用户名、密码两问被它们当场顶掉，直接退回了网页向导）。"""
    try:
        import termios
        with open("/dev/tty") as t:
            termios.tcflush(t, termios.TCIFLUSH)
    except Exception:
        pass

def _ask_fresh(prompt):
    """问一句、只认人敲的：问之前丢掉缓冲里的键；不到 0.3 秒就回来的空回车不是人按的（10/30 GCP 两次实装，
       用户名一回车、密码那问就被一个凭空多出来的空回车当场顶掉——清过缓冲还有，多半是终端把回车发成 \\r\\n），
       当没发生、再问。人真按的回车照常算。"""
    while True:
        _flush_typeahead()
        t0 = time.monotonic()
        v = _ask(prompt)
        if v or time.monotonic() - t0 >= 0.3:
            return v

def _auto_setup():
    """替你走完网页向导（仓库主人 10/30：「要是直接能改好随机端口最好」）：问用户名、密码，挑一个随机后台端口，
       调向导背后的接口 /control/install/configure 一次交过去——跟在浏览器里点完向导一样（AdGuard 自己 bcrypt 存密码、
       写配置、当场换到新端口）。以前只改配置文件做不成：没配置时它只开向导，账号密码要它自己哈希。
       10/30 用真的 AdGuard Home v0.107.79 跑通过：200 后新端口当场起来、旧的 3000 关掉、账号能登进去（域名当用户名也行）；
       密码少于 8 位它回 422「password must be at least 8 symbols long」。
       没走成（向导没起来、接口报错、新端口打不开）返回 False，调用方照旧给网页向导的步骤。"""
    G, Y, N = "\033[1;32m", "\033[1;33m", "\033[0m"
    for _ in range(15):
        if _agh_api("/control/install/get_addresses")[0] == 200:
            break
        time.sleep(1)
    else:
        return False
    print()
    ui_title("设置后台账号（替你走完网页向导）")
    # 用户名默认用自建 DNS 自己的域名（仓库主人 10/30：「用户名可以直接用自己的域名吗」——真 AdGuard 认带点的用户名，
    # 登得进去）；没域名才默认 admin。密码明着输（「建议显示，不然新手不知道有没有输入成功，DNS而已又不是什么重要网站」），
    # 看得见就不用再输一遍
    dft = _own_domain() or "admin"
    user = _ask_fresh(f"  后台用户名（回车 = {dft}）: ").strip() or dft
    # 空回车不当「不要了」：装的时候误按的回车会把这问顶掉。要退回网页向导得明着输 n
    while True:
        pw = _ask_fresh("  后台密码（至少 8 位；输 n = 改用网页向导）: ").strip()
        if pw.lower() == "n":
            return False
        # 跟用户名 / 域名一样等于没设：域名公网查得到，谁都猜得出（10/31 GCP 实装就设成了一样的）
        if len(pw) >= 8 and pw.lower() not in (user.lower(), (_own_domain() or "").lower()):
            break
        why = "没输" if not pw else ("至少 8 位" if len(pw) < 8 else "别跟用户名 / 域名一样")
        print(f"  {Y}✗ {why}，再输一次{N}")
    port = _pick_web_port(WEB_PORT)
    if not port:
        return False
    code, msg = _agh_api("/control/install/configure",
                         {"web": {"ip": "0.0.0.0", "port": port}, "dns": {"ip": "0.0.0.0", "port": 53},
                          "username": user, "password": pw})
    if code != 200:
        print(f"  {Y}⚠ 自动设置没成（{msg[:80] or code}），改用网页向导{N}")
        return False
    ok = False
    for _ in range(15):
        time.sleep(1)
        if _agh_api("/", port=port)[0] in (200, 302):
            ok = True; break
    if not ok:
        print(f"  {Y}⚠ 设好了，可新端口 {port} 还没起来：等一会儿再打开，或 systemctl status AdGuardHome 看原因{N}")
    ip = _public_ip()
    print(f"\n  {G}✔ 后台设好了：用户名 {user}，后台端口 {port}（随机，以后菜单 7 可改）{N}")
    ui_url("后台地址（云防火墙放行这个端口 TCP）", f"http://{ip}:{port}")
    _usage(port)
    return True

def _first_setup():
    ip = _public_ip()
    print("\n  === 下一步：打开网页后台完成初始化（2 分钟）===")
    print(f"  1) 浏览器打开安装向导：\033[1;32mhttp://{ip}:{WEB_PORT}\033[0m（先在防火墙放行 {WEB_PORT}/TCP）")
    print("  2) 向导里两个端口按这样填（\033[1;33m别用默认的 80\033[0m）：")
    print(f"     · 网页管理界面 端口 → 就填默认的 \033[1;32m{WEB_PORT}\033[0m（\033[1;33m初装先用它保证进得去\033[0m；想换防扫描端口，装好后回菜单选 7 改，改完连不上会自动回滚、锁不死）")
    print("     · DNS 服务器 端口 → 保持 \033[1;32m53\033[0m（装前已帮你腾好；仍报红就回菜单选 9 自检，查到会问腾不腾）")
    print("  3) 设管理员账号密码 → 完成。广告过滤（AdGuard DNS filter）默认就是开的。")
    print(f"  4) 完成后后台地址就是 \033[1;32mhttp://{ip}:{WEB_PORT}\033[0m；想改端口防扫描，回菜单选 \033[1;32m5\033[0m 改（带回滚，安全）")
    _usage(WEB_PORT)

SELFDNS_FLAG     = BGP_DIR + "/selfdns.on"        # 主脚本：自建 DNS 是否已写进订阅
SELFDNS_CID_FILE = BGP_DIR + "/selfdns.clientid"  # 主脚本生成的 AdGuard ClientID
PRIVDNS_FILE     = BGP_DIR + "/privdns.json"      # 主脚本：节点使用私有DNS解析（菜单 4；关 / 本机 / 外机）

def _selfdns_clientid():
    """AdGuard ClientID（DoH 网址末段 / DoT 主机名最前一段 / 「允许的客户端」里填的那串）。没有就生成一个存下来：
       跟主脚本 selfdns_clientid 同一个文件、同一种格式（xy + 12 位十六进制），谁先用谁生成，另一边读到的是同一个。
       以前这边只读、得先去菜单 3 打开一次才有——仓库主人签完泛域名证书，屏上还是「ClientID 还没有」，
       安卓该填的那行主机名也出不来（10/12：「写一大堆，就是不写重要信息」）。
       它等于一把口令（填进白名单后只认它），文件 root-only。写不进去返回 ''。"""
    try:
        t = open(SELFDNS_CID_FILE).read().strip()
        if t:
            return t
    except OSError:
        pass
    t = "xy" + secrets.token_hex(6)
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        fd = os.open(SELFDNS_CID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(t)
    except OSError:
        return ""
    return t

def _doh_port():
    """当前 DoH(HTTPS) 端口：加密开着才读 AdGuardHome.yaml 的 tls.port_https；没开（或读不到）按 10443。
       没开时 yaml 里那个是 AdGuard 的默认 443（向导一走完就写进去了）——拿它教人填，第二步就成了
       「HTTPS 端口：443（别填 0 / 443）」自相矛盾，DoH 地址也写成 :443（10/31 GCP 自动设置后实测）。"""
    try:
        tls = _tls_block(open(_agh_yaml()).read())
        if str(_yaml_val(tls, "enabled")).strip().lower() != "true":
            return 10443
        return int(_yaml_val(tls, "port_https") or 0) or 10443
    except (OSError, ValueError):
        return 10443

# ── 共用证书的底线 ───────────────────────────────────────────────────────
# /etc/ssl/sb/acme.crt 是【全机共用】的：节点(sing-box/xray)、订阅服务、nginx 443
# 和 AdGuard 的 DoT 都指着它。这个文件有三个脚本会写，谁装一张盖不住节点域名的
# 证书进去，所有吃证书的节点就会被客户端当场拒绝（tls: bad certificate），
# 而 reality 照常通——看起来像「随机几个节点坏了」，最难往证书上想。
#
# 这边的 dom 来自 sub.host，节点的真相在 state.json，两者理论上一致但会漂：
# 换过域名、手工改过、订阅还没重生成，都可能对不上。所以不能假设，得装之前验。

def _node_domain():
    """节点当前对外用的域名。以主脚本的 state.json 为准，读不到才退回 sub.host。"""
    try:
        import json
        d = (json.load(open(BGP_DIR + "/state.json")).get("domain") or "").strip()
        if d:
            return d
    except Exception:
        pass
    return _domain()

def _cert_names(path):
    """证书覆盖哪些域名（SAN 列表，含 *.x）。读不出返回 []。"""
    if not os.path.exists(path):
        return []
    out = sh(f"openssl x509 -in {path} -noout -text 2>/dev/null") or ""
    m = re.search(r"Subject Alternative Name:\s*\n\s*(.+)", out)
    return [x.strip()[4:] for x in m.group(1).split(",") if x.strip().startswith("DNS:")] if m else []

def _name_covers(names, domain):
    """SAN 列表覆不覆盖这个域名（泛域名只顶一级，跟客户端的判定一致）。"""
    d = (domain or "").lower().strip(".")
    for n in [x.lower().strip(".") for x in names]:
        if n == d:
            return True
        if n.startswith("*.") and d.endswith(n[1:]) and "." not in d[:-len(n[1:])]:
            return True
    return False

def _install_cert_guarded(acme, dom, reload_hook, what=""):
    """把证书导出到共用路径，但【导完必须还盖得住节点域名】，否则原样还原。

       acme.sh 没法「先导到别处再验」——install-cert 同时也把路径记进域名记录里，
       导到临时路径会让往后的自动续期都装到那个临时路径去。所以这里的做法是
       先备份、导出、验、不合格就还原文件并把记录改回来。"""
    R, G_, N = "\033[1;31m", "\033[1;32m", "\033[0m"
    nd = _node_domain()
    bak = {}
    for p in (ACME_CRT, ACME_KEY):
        if os.path.exists(p):
            bak[p] = p + ".agh.bak"
            shutil.copyfile(p, bak[p])
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
    root = _privdns_root()                             # 节点专用域名（菜单 4）也是节点在用的，一样得盖住
    have = _cert_names(ACME_CRT)
    miss = nd if nd and not _name_covers(have, nd) else \
        (f"*.{root}" if root and not _name_covers(have, "x." + root) else "")
    if miss:
        print(f"\n  {R}❌ 导出的证书盖不住节点正在用的域名 {miss}"
              f"（它覆盖的是 {', '.join(have) or '(读不出)'}）。{N}")
        print(f"  {R}   装上去会让所有吃证书的节点被客户端拒绝（reality 不受影响），"
              f"已还原成原来那张。{N}")
        # 顺序不能反：先把 acme 记录改回节点域名（免得下次自动续期又来一遍），
        # 【最后】才还原文件——install-cert 本身也会写这两个文件，先还原就白还原了。
        if nd and nd != dom:
            sh(f"{acme} --install-cert -d {nd} --ecc "
               f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
        for p, b in bak.items():                       # 文件还原，保证一个字节没动
            shutil.copyfile(b, p)
            os.remove(b)
        if not bak:                                    # 本来就没有证书 → 别留下半张
            for p in (ACME_CRT, ACME_KEY):
                if os.path.exists(p):
                    os.remove(p)
        for svc in ("nginx", "sing-box", "xray", "xy-sub", "AdGuardHome"):
            sh(f"systemctl restart {svc}")
        print(f"     {'' if not what else what + '未生效。'}"
              f"节点域名和本机 sub.host 可能对不上，"
              f"到主面板『15 域名证书管理』看一眼。")
        return False
    for b in bak.values():
        try: os.remove(b)
        except OSError: pass
    return True

def _cert_wildcard_ok(dom):
    """本机 acme 证书是否覆盖 *.dom —— 这决定 DoT 能不能带 ClientID。
       DoT 的 ClientID 是塞在 SNI 第一段(tls://<ID>.域名)传过去的，证书里没有
       *.域名 这个 SAN，客户端握手就会因证书名不匹配而拒连。"""
    if not (dom and os.path.exists(_crt())):
        return False
    return f"DNS:*.{dom}" in sh(f"openssl x509 -in {_crt()} -noout -text 2>/dev/null")

def _resolve4(name):
    """name 的 IPv4 地址（走本机的解析器，查的是公网上的记录）；查不到返回 []。"""
    try:
        return sorted({a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)})
    except OSError:
        return []

def _wildcard_dns_check(dom):
    """*.dom 的通配记录能不能把手机带到这台机器 → (行不行, 一句话说明)。

       安卓私人 DNS 按主机名（ID.域名）找服务器：先用当前网络的 DNS 把它解析成 IP，再去连那个 IP 的 853。
       光「解析得出来」不够——Cloudflare 新加的记录默认开着黄云（代理），解析出来是 CF 的 IP，CF 不转发 853，
       手机就报「无法访问私人 DNS 服务器」；而自检的 DoT 是直连本机测的，照样全绿，查不出来。
       所以拿它跟主域名的解析结果比：主域名是节点在用的，一定是灰云直连本机。
       随便取个不存在的子域去查：证书能签下来不代表客户端找得到路——通配 A 记录得单独加。"""
    wild = _resolve4(f"dotcheck{secrets.token_hex(3)}.{dom}")
    if not wild:
        return False, f"*.{dom} 没有解析记录（通配 A 记录还没加，或还没生效）"
    base = _resolve4(dom)
    if base and not set(wild) & set(base):
        return False, (f"*.{dom} 解析到 {wild[0]}，跟 {dom}（{base[0]}）不一样"
                       f"——多半是 Cloudflare 里通配那条开了黄云（代理）")
    return True, ""

def _wildcard_dns_ok(dom):
    return _wildcard_dns_check(dom)[0]

def _agh_users():
    """AdGuardHome.yaml 里 users 段的用户名（只读名字，密码那行是 bcrypt 哈希，不碰）；读不到返回 []。"""
    try:
        txt = open(_agh_yaml()).read()
    except OSError:
        return []
    m = re.search(r"(?m)^users:\s*\n((?:[ \t]+.*\n?)*)", txt)
    if not m:
        return []
    return [n.strip().strip("'\"") for n in re.findall(r"(?m)^[ \t]*-[ \t]*name:[ \t]*(.+?)[ \t]*$", m.group(1))]

def _allowed_clients():
    """AdGuardHome.yaml 里的访问白名单 allowed_clients；没这个键返回 None，空列表返回 []。"""
    try:
        return _yaml_list(open(_agh_yaml()).read(), "allowed_clients")
    except OSError:
        return None

def _usage(port=None, head=None):
    """使用方法（菜单 2）：三步设置 → 三种用法 → 状态 / 当前配置 / 要填的几行。
       仓库主人 10/30 照着自己的写法重排：「啰嗦的写一堆眼都看花了」——步骤只留要点的那一行，值从这台机实时读、高亮；
       原委（白名单为什么挡安卓、DoH 为什么带端口、广告拦得到哪一步）留在注释和文档里，不上屏。
       head：状态那几行（status() 传进来），放在当前配置前面。"""
    ip = _public_ip(); dom = _domain()
    port = port or _current_web_port() or WEB_PORT
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; C = "\033[1;36m"; N = "\033[0m"
    dohp = _doh_port(); cid = _selfdns_clientid()
    enc = _https_panel() is not None
    allow = _allowed_clients()
    doh_url = f"https://{dom}:{dohp}/dns-query" + (f"/{cid}" if cid else "")
    # 证书覆盖 *.域名 → DoT 才能把 ClientID 放在主机名最前面；没签就带不上，白名单一设安卓专用 DNS 当场断（10/14 真机）
    wild = _cert_wildcard_ok(dom)
    dot_host = f"{cid}.{dom}" if (wild and cid) else dom
    hp = _https_panel()

    print(f"\n  {C}【第一步 · 登录网页后台】{N}")
    if hp:
        print(f"    浏览器打开（推荐·加密）：{G}https://{hp[0]}:{hp[1]}{N}")
        print(f"    应急 / 本机用明文：http://{ip}:{port}")
    else:
        print(f"    浏览器打开：{G}http://{ip}:{port}{N}")
    if not os.path.exists(_agh_yaml()):              # 还没走过向导（自动设置没成）：浏览器里设账号，端口照 3000
        print(f"    首次打开设账号密码；向导里管理端口 {Y}80 改成 3000{N}（以后菜单 7 可改）")

    print(f"\n  {C}【第二步 · 设置加密】{N}")
    if dom:
        print("    后台 → 设置 → 加密设置：")
        print("      · 勾选「启用加密」")
        print(f"      · 服务器名称：{G}{dom}{N}")
        print(f"      · 证书（文件路径）：{G}{_crt()}{N}")
        print(f"      · 私钥（文件路径）：{G}{_key()}{N}")
        print(f"      · HTTPS 端口：{G}{dohp}{N}（别填 0 / 443）　DNS-over-TLS 端口：{G}853{N}")
        print(f"      · 「HTTPS 自动重定向」{Y}不要勾{N}")
        print(f"      · 保存（提示 no IP addresses 的黄字忽略）")
    else:
        print(f"    {Y}没设域名，加密用不了，只能用明文 DNS{N}（要加密：卸了重装，装的时候填域名）")

    print(f"\n  {C}【第三步 · DNS 设置】{N}")
    print("    后台 → 设置 → DNS 设置 → 上游 DNS 服务器填这 3 行：")
    for u in ("https://dns10.quad9.net/dns-query", "https://dns.cloudflare.com/dns-query",
              "https://dns.google/dns-query"):
        print(u)
    print(f"    选 {G}并行请求{N} → 应用")
    print("    下滑到 DNS 缓存配置：")
    print(f"      ✓ 启用缓存")
    print(f"      缓存大小：{G}67108864{N}")
    print(f"      覆盖最小 TTL 值：{G}60{N}")
    print(f"      覆盖最大 TTL 值：{G}86400{N}")
    print(f"      ✓ 乐观缓存 → 保存")
    if not cid:
        print(f"    {Y}ClientID 没生成出来（{BGP_DIR} 写不进去？），允许的客户端先不设{N}")
    elif allow and dom and not wild:
        print(f"    {R}⚠ 允许的客户端已设、没签泛域名证书：安卓专用 DNS 会被挡{N}（菜单 5 签证书，或清空允许的客户端）")
    elif allow:
        print(f"    允许的客户端：{G}✓ 已设好{N}（{', '.join(allow)}）")
    elif dom and not wild:
        print(f"    允许的客户端：{Y}先做菜单 5 签泛域名证书再填{N}（不然安卓专用 DNS 会被挡）")
    else:
        print("    下滑到 允许的客户端，框里只填这个 ID → 保存配置：")
        print(cid)

    print(f"\n  {C}【DNS 使用场景】{N}")
    if dom:
        print(f"    ① DoH 加密 —— 电脑浏览器 / 支持 DoH 的 App（VPS 防火墙放行 {dohp}/TCP）")
        print(doh_url)
        print(f"    ② DoT 加密 —— 安卓「专用DNS」，全系统生效，{Y}最推荐{N}（放行 853/TCP）")
        print("       设置 → 网络 → 专用DNS → 私人DNS提供商主机名：")
        print(dot_host)
    print(f"    ③ 明文 DNS —— 电视 / 盒子 / IoT / 路由器（放行 53/UDP+TCP；53 被占选菜单 9 自检）")
    print(ip)
    if allow:
        ui_tip("设了允许的客户端，明文 53 没法带 ID，会被挡")
    ui_tip("想多拦广告：后台 → 过滤器 → DNS 拦截列表 → 添加 https://anti-ad.net/easylist.txt")

    print("\n  " + "=" * 56)
    print(f"  {C}AdGuard Home 状态参数{N}")
    print("  " + "=" * 56)
    for ln in head or []:
        print(ln)
    print(f"    域名          : {G}{dom or '（无，只能用明文 53）'}{N}")
    print(f"    公网 IP       : {G}{ip}{N}")
    print(f"    后台端口      : {G}{port}{N}")
    users = _agh_users()                           # 仓库主人 10/30：忘了用户名能在这查到；密码不显示（只存了哈希）
    print(f"    后台用户名    : {(G + '、'.join(users) + N) if users else (Y + '还没设（第一步）' + N)}")
    print(f"    加密(DoT/DoH) : {(G+'已开启'+N) if enc else (Y+'未开启（第二步）'+N)}")
    print(f"    DoH 端口      : {G}{dohp}{N}        DoT 端口: {G}853{N}       明文: {G}53{N}")
    if cid:
        print(f"    {C}ClientID{N}      : {G}{cid}{N}")
    else:
        print(f"    ClientID      : {Y}没生成出来{N}")
    if allow:
        print(f"    访问白名单    : {G}已设置{N} {allow}")
    else:
        print(f"    访问白名单    : {R}未设置 —— 对全公网开放（第三步）{N}")
    # 三个订阅都写：mihomo / 小火箭放 DNS 列表最前，sing-box 换掉 dns.final（见主脚本 _*_selfdns）
    print(f"    写入订阅      : {(G+'已写入 mihomo/sing-box/小火箭'+N) if os.path.exists(SELFDNS_FLAG) else (Y+'未写入（菜单 3 开关）'+N)}")

    # 要填的就这几行：标题高亮、值顶格单独一行好复制（仓库主人 10/12：「写一大堆，就是不写重要信息」）。
    # 【列出来的几行不能互相打架】10/14：照着把 ClientID 填进「允许的客户端」，安卓专用 DNS 当场断了——
    # 没签泛域名证书时安卓主机名带不上 ClientID。所以白名单那行只在签了泛域名之后才列。
    print("\n  " + "=" * 56)
    print(f"  {C}要填的就这几行{N}")
    print("  " + "=" * 56)
    items = []
    if dom:
        items.append(("安卓「专用DNS」主机名", dot_host))
    if dom and wild and cid:
        items.append(("AdGuard 后台 → DNS 设置 → 允许的客户端", cid))
    if dom:
        items.append(("DoH 地址（浏览器 / App）", doh_url))
    items.append(("明文 DNS（电视 / 盒子 / 路由器）", ip))
    for i, (label, val) in enumerate(items):         # 条与条之间空一行（CLAUDE.md 第五节）
        ui_url(label, val)
        if i < len(items) - 1:
            print()

def _root_of(dom):
    """可注册的主域名：dns.example.com → example.com（常见 com.cn 这类二级后缀多留一段）。"""
    parts = (dom or "").strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in ("com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
                                                     "com.hk", "com.tw", "co.uk", "co.jp", "com.au"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def _cf_token_steps(dom):
    """要粘 Cloudflare 令牌前，先把在 CF 后台怎么做列出来（仓库主人 10/15 给的步骤，照抄）。"""
    print()
    print(_hl("1;33", "▸ Cloudflare 令牌怎么做"))
    print("  CF 主页右上头像 → 配置文件 → API 令牌 → 创建令牌 → 用「编辑区域 DNS」模板")
    print("  令牌名称：DNS-DOT")
    print("  权限：区域 - DNS - 编辑")
    print("     ＋ 区域 - 区域 - 读取")
    print(f"  区域资源：包括 - 特定区域 - {_root_of(dom)}")
    print("  其他不动 → 继续以显示摘要 → 创建令牌 → 复制令牌")
    print()

def _replace_cf_token():
    """换掉 acme.sh 存的 Cloudflare Token（旧的泄露/被吊销时用）。
       acme.sh 把它存成 account.conf 里的 SAVED_CF_Token='…'，续期时读这里。
       只换这一个值，不重签证书——证书还好好的，没必要浪费一次 LE 签发额度。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; N = "\033[0m"
    conf = os.path.expanduser("~/.acme.sh/account.conf")
    if not os.path.exists(conf):
        print(f"  {R}找不到 {conf}{N}，你的证书可能不是本脚本签的。"); return
    # 明文显示：仓库主人 10/14「令牌是可以显示出来的，没必要隐藏，都是自己装自己的」——盲贴看不见贴没贴上
    _cf_token_steps(_domain())
    new = _ask("  粘贴新的 Cloudflare API Token（粘完回车）: ").strip()
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

def _cert_meta_set(**kw):
    """把证书现在的样子记进主脚本的 cert.json（只改传进来的字段）。

       主脚本重签证书之前靠它判断「这台本来该不该是泛域名」（xy-installer 的 _wildcard_wanted），
       面板也靠它。这边签了泛域名却不记，哪天磁盘上的证书被换掉（重装、换证书），主脚本就以为
       这台本来只要单域名，照着 HTTP-01 签回去 —— 安卓「ID.域名」悄无声息地失效。撤销时同理。"""
    try:
        cur = json.load(open(CERT_META))
    except Exception:
        cur = {}
    cur.update(kw)
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        json.dump(cur, open(CERT_META, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass

def _revert_wildcard(dom, cid="", ask=True, auto_token=False):
    """撤销泛域名：把证书重签回只覆盖『域名』，DoT 就不能再带 ClientID 了。

       80 端口空着就用 HTTP-01 重签——这样连 acme.sh 的续期方式也一并回到装机时的
       样子，之后不再依赖 CF Token，可以把它从 account.conf 里清掉。80 被占（nginx
       等）则退回 DNS-01，此时 Token 必须留着，否则 90 天后续期会失败。

       同样是失败安全的：acme.sh 只有签发成功才覆盖证书文件。

       两个调用方：菜单 5 的「删除 DoT」用默认参数——单独撤这一项，要自己确认，Token
       清不清也再问一次；卸载流程用 ask=False, auto_token=True——那边总确认里已经把这件
       事列出来过了，不该在中途再拦一次，Token 也没有留着的理由，直接清。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; N = "\033[0m"
    if ask:
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
    root = _privdns_root()                        # 节点专用域名（菜单 4）还要 *.根域名：只能 DNS-01，令牌留着
    http01 = not _port_busy(80) and not root
    if http01:
        issue = f"{acme} --issue -d {dom} --standalone --keylength ec-256 --server letsencrypt --force"
        how = "HTTP-01（80 端口空闲，撤销后不再依赖 CF Token）"
    else:
        issue = (f"{acme} --issue -d {dom}" + (f" -d '*.{root}'" if root else "") +
                 " --dns dns_cf --keylength ec-256 --server letsencrypt --force")
        how = (f"DNS-01（节点专用域名还要 *.{root}，{Y}CF Token 仍需保留供续期{N}）" if root else
               f"DNS-01（80 被占用，{Y}CF Token 仍需保留供续期{N}）")
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
    if not _install_cert_guarded(acme, dom, reload_hook, "撤销泛域名"):
        return
    if _cert_wildcard_ok(dom):
        print(f"  {R}❌ 证书里仍有 *.{dom}，撤销没生效。{N}"
              f"看 `openssl x509 -in {ACME_CRT} -noout -text | grep DNS:` 确认。")
        return
    sh("systemctl restart AdGuardHome", check=False)
    time.sleep(3)
    print(f"  {G}✓ 证书已改回只覆盖 {dom}{N}")
    _cert_meta_set(domain=dom, wildcard=False, mode="standalone" if http01 else "dns-cf")
    if ask:
        print(f"  安卓「专用DNS」现在要填：{G}{dom}{N}")
    # DNS-01 重签的话续期还要读 Token，不能清；HTTP-01 才是真的用不着了
    if http01:
        conf = os.path.expanduser("~/.acme.sh/account.conf")
        if os.path.exists(conf) and re.search(r"(?m)^SAVED_CF_Token=", open(conf).read()):
            if auto_token or _ask("  续期已不需要 CF Token，把它从 account.conf 清掉? [Y/n]: ").lower() not in ("n", "no"):
                txt = re.sub(r"(?m)^SAVED_CF_Token=.*$\n?", "", open(conf).read())
                shutil.copy(conf, conf + ".bak"); open(conf, "w").write(txt); os.chmod(conf, 0o600)
                print(f"  {G}✓ 已清除 acme.sh 里的 CF Token{N}（备份 {conf}.bak）。"
                      f"{Y}记得回 CF 把该 Token 删掉。{N}")
    elif auto_token:
        print(f"  {Y}⚠ {'节点专用域名还要 *.' + root if root else '80 端口被占'}，只能走 DNS-01 重签 —— "
              f"acme.sh 里的 CF Token 必须留着{N}（约 90 天后续期还要用它），所以没有清除。")
    if ask and cid and _allowed_clients():
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
        okw, why = _wildcard_dns_check(dom)
        if not okw:
            print(f"  {R}⚠ {why}{N}——手机找不到这台机器，安卓专用 DNS 连不上。")
            print(f"    Cloudflare 里那条：类型 {G}A{N}  名称 {G}*.{dom}{N}"
                  f"  内容 {G}{_public_ip()}{N}  代理状态 {Y}仅DNS(灰云){N}")
        print(f"  Token 存在 {G}~/.acme.sh/account.conf{N}，约 90 天后自动续期还要用它。")
        ui_line()
        ui_items([(1, "更换 Cloudflare API 令牌（泄露了 / 被吊销了）", None),
                  (2, "删除 DoT ClientID 支持（证书改回单域名，AdGuard 照常跑）", None),
                  (0, "返回", None)])
        ui_tip("想把整套自建 DNS 卸掉：回上级菜单选 10")
        c = _ask("选择: ").strip()
        if c == "1":
            _replace_cf_token()
        elif c == "2" and _own_domain():
            # 自己的证书本来就是泛域名、只给 DNS 用，撤了也不省什么；不想要 ClientID，后台清空「允许的客户端」就行
            ui_tip("自建 DNS 用的是自己的证书，不用撤；不想用 ClientID，后台清空「允许的客户端」即可")
        elif c == "2":
            _revert_wildcard(dom, cid)
        return
    if _own_domain():                                     # 自己那张装的时候就带 *.域名；没有说明证书出了岔子
        print(f"  {R}⚠ 自建 DNS 自己的证书里没有 *.{dom}{N}")
        ui_tip("卸了重装自建 DNS（菜单 10 → 1），装的时候会重签"); return
    print(f"  会把 acme 证书重签成同时覆盖 {G}{dom}{N} 和 {G}*.{dom}{N}。")
    print(f"  AdGuard 的证书路径不用改（还是 {ACME_CRT}），新证书是旧的超集，节点不受影响。")
    print(f"\n  {Y}需要你先准备两样：{N}")
    print(f"    1) Cloudflare 上加一条通配 A 记录，指向本机 {G}{_public_ip()}{N}，{Y}灰云(仅DNS){N}")
    print(f"       —— 没有它，手机连 {cid or '<ID>'}.{dom} 时根本解析不出地址")
    print(f"    2) Cloudflare API 令牌（做法见下）")
    _cf_token_steps(dom)

    if _ask("\n  两样都准备好了，继续? [y/N]: ").lower() not in ("y", "yes"):
        print("  已取消，没有任何改动。"); return
    okw, why = _wildcard_dns_check(dom)
    if not okw:
        print(f"\n  {R}❌ {why}{N}")
        print(f"     去 Cloudflare 弄好（名称 *.{dom}、内容本机 IP、灰云「仅 DNS」），等一两分钟再回来。"
              f"签证书前就查，省得签完才发现没路。")
        return
    print(f"  {G}✓ 通配解析已生效{N}")

    token = _ask("  粘贴 Cloudflare API Token（粘完回车）: ").strip()      # 明文显示，同 _replace_cf_token
    if not token:
        print("  没输入，已取消。"); return

    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print("  找不到 acme.sh —— 你的证书可能不是本脚本签的，不便自动接管。"); return

    print(f"\n  正在签发（DNS-01 验证，需要等 CF 记录传播，约 1-2 分钟）…")
    root = _privdns_root()                                # 节点专用域名（菜单 4）要的 *.根域名，一起签上
    issue = (f"CF_Token='{token}' {acme} --issue --dns dns_cf "
             f"-d {dom} -d '*.{dom}'" + (f" -d '*.{root}'" if root else "") +
             " --keylength ec-256 --server letsencrypt")
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
    if not _install_cert_guarded(acme, dom, reload_hook, "泛域名证书"):
        return
    if not _cert_wildcard_ok(dom):
        print(f"\n  {R}❌ 证书导出后仍未覆盖 *.{dom}，没有生效。{N}原证书可能已被替换，"
              f"建议看 `openssl x509 -in {ACME_CRT} -noout -text | grep DNS:`。")
        return
    print(f"  {G}✓ 证书已覆盖 *.{dom}{N}")
    _cert_meta_set(domain=dom, wildcard=True, mode="dns-cf")

    sh("systemctl restart AdGuardHome", check=False)
    time.sleep(3)
    if cid:
        (rc, ips), _, err = _q_dot(853, f"{cid}.{dom}", "example.com")
        if rc == 0 and ips:
            print(f"  {G}✓ 用 {cid}.{dom} 做 SNI 实测 DoT 查询成功{N}")
        else:
            print(f"  {Y}⚠ DoT 实测没通过（{err or 'rcode=' + str(rc)}）{N}——证书是对的，"
                  f"可能是 853 端口没开或 AdGuard 还没起好，稍后用菜单 9 自检再看。")

    print(f"\n  {G}✔ 做完了{N}\n")
    ui_url("安卓「专用DNS」主机名", f"{cid}.{dom}" if cid else "")
    print()
    ui_url("AdGuard 后台 → DNS 设置 → 允许的客户端", cid)
    ui_tip("设了白名单，明文 53（电视 / 盒子）就进不来了，要把它们的固定 IP 也加进去")

def _do_free53():
    """真正腾 53：关掉 systemd-resolved 的桩监听 + 把 resolv.conf 指到公共 DNS（可逆）。
       不含确认/占用者判断，供安装流程与菜单复用。"""
    try:
        os.makedirs(RESOLVED_DIR, exist_ok=True)
        open(RESOLVED_DROPIN, "w").write("[Resolve]\nDNSStubListener=no\n")
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
        print(f"  （撤销：删 {RESOLVED_DROPIN} 后 systemctl restart systemd-resolved；卸载时会自动还原）")
    except OSError as e:
        print("  腾 53 失败:", e)

def _selfcheck_more():
    """自检之后两问（10/31 并进来的「腾出 53 端口」「手机实测」，原来是单独两项）：
       53 被 systemd-resolved 占着才问腾不腾；再问要不要手机实测。都默认否。"""
    if not _installed():
        return
    b53 = _port_busy(53)
    if b53 and "AdGuardHome" not in b53 and "systemd" in b53:
        print()
        ui_tip("53 被 systemd-resolved 占着，明文 DNS 用不了")
        free_port53()
    if _ask("\n  接着做手机实测（开私人 DNS，看它连没连进来）? [y/N]: ").strip().lower() in ("y", "yes"):
        phone_probe()

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
    print()
    ui_kv("当前后台端口", cur)
    ui_items([(1, "随机（2000-5000）", None), (2, "自定义", None), (0, "取消", None)])
    c = _ask("选择: ").strip()
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
    if not _installed():
        print("\n  未安装。选『1 安装』先装上。"); return
    port = _current_web_port()
    b53 = _port_busy(53)
    head = [f"    已安装        : {AGH_BIN}",
            "    运行          : " + ("✓ 运行中" if _running() else "✗ 没在跑（systemctl status AdGuardHome 看原因）"),
            "    53 端口       : " + ("空闲" if not b53 else f"被 {b53} 占用")]
    _usage(port, head)

def _selfdns_remove():
    """卸载 AdGuard 时：若之前用菜单 3 把自建 DNS 写进过订阅，就交给主脚本从订阅移除并刷新
       （没写入则主脚本内部静默跳过）。避免订阅里留着指向已卸载 AGH 的死 DoH。"""
    xy = BGP_DIR + "/xy-installer.py"
    if os.path.exists(xy):
        subprocess.run(f"python3 {xy} selfdns-off", shell=True)

def _sys_dns_ok():
    """本机系统解析当前是否可用。特意用 getent 起新进程测，而不是本进程 socket：
       glibc 的解析器在进程内会把 resolv.conf 读进来，刚改完再自己查未必反映真实状态。"""
    return any(sh(f"getent hosts {h} 2>/dev/null") for h in ("cloudflare.com", "example.com"))

def _restore_resolved():
    """还原装 AdGuard 时「腾 53」对本机解析做的改动：删掉我们写的 drop-in，
       把 /etc/resolv.conf 交回 systemd-resolved（软链回 stub），重启服务。

       为什么每一步都要验证再落地：这台机自己的 DNS 是命脉——apt / curl / acme 续期
       全指着它。所以还原成软链后先实测一次解析，通了才算数；不通就立刻退回「写死公共
       DNS 的普通文件」这个已知能用的状态。宁可留着不完美的配置，也不能把机器搞成没
       DNS 可用——那种情况 SSH 还连得上，但装什么、续什么证书都会失败，很难查。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; N = "\033[0m"
    if not os.path.exists(RESOLVED_DROPIN):
        return                                       # 没腾过 53，本机解析本来就没被动过
    keep = ""
    try: keep = open("/etc/resolv.conf").read()      # 留一份现状，还原失败要退回来
    except OSError: pass
    try:
        os.remove(RESOLVED_DROPIN)
    except OSError as e:
        print(f"  {Y}⚠ 删不掉 {RESOLVED_DROPIN}：{e}，本机解析未还原。{N}"); return
    sh("systemctl restart systemd-resolved")
    time.sleep(1)
    stub = "/run/systemd/resolve/stub-resolv.conf"
    if sh("systemctl is-active systemd-resolved") != "active" or not os.path.exists(stub):
        # 这台机根本没跑 systemd-resolved（有些镜像不装），那就保持现在这份公共 DNS 的
        # resolv.conf —— 它本来就是能用的，没有「原状」可回。
        print(f"  {Y}· 本机没在跑 systemd-resolved，/etc/resolv.conf 保持现状"
              f"（公共 DNS，可用）。{N}")
        return
    try:
        os.remove("/etc/resolv.conf")
        os.symlink(stub, "/etc/resolv.conf")
    except OSError as e:
        print(f"  {Y}⚠ resolv.conf 软链没建成：{e}，保持现状（公共 DNS，可用）。{N}"); return
    time.sleep(1)
    if _sys_dns_ok():
        print(f"  {G}✓ 本机解析已交回 systemd-resolved（腾 53 的改动还原）{N}")
        return
    try: os.remove("/etc/resolv.conf")               # 交回去反而不通 → 退回能用的那份
    except OSError: pass
    open("/etc/resolv.conf", "w").write(keep or "nameserver 1.1.1.1\nnameserver 223.5.5.5\n")
    print(f"  {Y}⚠ 交回 systemd-resolved 后解析不通，已退回写死公共 DNS 的 /etc/resolv.conf"
          f"（本机 DNS 仍然可用）。{N}")

def uninstall(refresh_sub=True):
    """卸载：把这套自建 DNS 留下的东西一次撤干净，回到没装过的样子。

       为什么不只是删服务：这些东西是连在一起的一整套，只删 AdGuard 会留下一地孤儿——
       订阅里还指着一个没人应答的 DoH、证书上挂着没用的 *.域名、acme.sh 里躺着续期用的
       CF Token、本机解析还钉在公共 DNS 上。用户点「卸载」的意思是这套东西整个不要了。
       （菜单 5 的「删除 DoT」是另一回事：只撤泛域名证书这一项，AdGuard 本体照常跑。）

       各项彼此独立、按「先撤依赖它的，再撤它本身」排序，中间某项失败不影响后面：证书
       重签失败原证书原封不动，本机解析还原不通会自动退回能用的状态。"""
    G = "\033[1;32m"; Y = "\033[1;33m"; R = "\033[1;31m"; N = "\033[0m"
    if not _installed():
        print("  没检测到 AdGuard Home，无需卸载。"); return
    dom  = _domain()                                 # 这些状态要趁 AGH 目录还在先读出来
    own  = _own_domain()                             # 新装法：自己的域名、自己的证书，整张撤掉，节点证书不碰
    cid  = _selfdns_clientid()
    wild = refresh_sub and dom and not own and _cert_wildcard_ok(dom)
    subw = os.path.exists(SELFDNS_FLAG)
    prv  = refresh_sub and _privdns_mode() == "self"
    res  = os.path.exists(RESOLVED_DROPIN)
    allow = _allowed_clients()

    print("\n  卸载会一次撤掉下面这些，不用再去别处收尾：")
    print("    · AdGuard Home 服务 + /opt/AdGuardHome")
    print(f"      （{Y}后台设置、允许的客户端、过滤规则、查询日志全部删除，不可恢复{N}）")
    if subw: print("    · 订阅里写入的自建 DoH —— 移除并刷新订阅")
    if prv:  print("    · 节点私有解析（菜单 4 选了「本机DNS解析」）—— 关掉，外机节点改回真实地址")
    if wild: print(f"    · 为 DoT ClientID 签的泛域名证书 —— 重签回只覆盖 {G}{dom}{N}，"
                   f"顺带清掉 acme.sh 存的 CF Token\n"
                   f"      （{Y}重签期间 sing-box/xray 会重启几秒{N}；失败则原证书原封不动）")
    if own:  print(f"    · 自建 DNS 自己的证书（{own} + *.{own}）—— 删掉，acme.sh 不再续期它")
    if res:  print("    · 腾 53 时改的本机解析 —— 交回 systemd-resolved")
    if cid:  print(f"    · ClientID 记录（{cid}）")
    print(f"\n  {G}不会动{N}：sing-box / xray / 节点 / 订阅本身"
          + (f"——{ACME_CRT} 仍是节点在用的那张证书，只是覆盖范围改回单域名。" if wild else "。"))
    if allow:
        print(f"\n  {R}先做这件事再卸载{N}：AdGuard 里设了白名单，卸载后这台 DNS 整个没了，"
              f"而安卓「专用DNS」是严格模式、没有回落——")
        print(f"  先把手机/路由器的 DNS 改回自动/默认，否则那些设备会立刻全部解析失败。")
    if _ask("\n  确认卸载? [y/N]: ").lower() not in ("y", "yes"):
        print("  已取消，没有任何改动。"); return

    if refresh_sub:                                  # 先撤订阅：客户端别再往这台已死的 DoH 打
        _selfdns_remove()                            # （全量卸载时订阅整个删掉，不必刷新，refresh_sub=False）
        _privdns_remove()                            # 当 DNS 机替外机解析的：外机节点改回真实地址
    if os.path.exists(AGH_BIN):
        sh(f"{AGH_BIN} -s uninstall")                # 官方卸载（停服务 + 注销 systemd）
    sh("systemctl stop AdGuardHome")
    sh("systemctl disable AdGuardHome")
    try: shutil.rmtree(AGH_DIR)
    except OSError: pass
    print("  ✓ 已卸载 AdGuard Home（sing-box/xray/节点不受影响）")
    if wild:
        _revert_wildcard(dom, cid, ask=False, auto_token=True)
    if own:
        _own_cert_remove(own)
        print(f"  {G}✓ 已撤掉自建 DNS 自己的证书{N}（节点证书没动）")
    _restore_resolved()
    if cid:
        try:
            os.remove(SELFDNS_CID_FILE)
            print(f"  {G}✓ 已清除 ClientID 记录{N}（重新安装会生成新的）")
        except OSError:
            pass
    print(f"\n  {G}卸载完成。{N}"
          f"记得把之前改过 DNS / 专用DNS 的设备改回自动/默认，否则它们会没 DNS 可用。")

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

def _yaml_unquote(v):
    """脱掉 YAML 值外面的引号；没加引号的原样返回。"""
    v = v.strip()
    for q in ("'", '"'):
        if len(v) >= 2 and v[0] == q and v[-1] == q:
            return v[1:-1].replace(q * 2, q)
    return v


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
            # 【脱引号】写进去的是 '[/cn/]223.5.5.5'，读回来得是 [/cn/]223.5.5.5，
            # 否则跟状态文件里记的对不上、关闭时一条都删不掉
            items.append(_yaml_unquote(st[2:]))
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

def _q_doh(port, sni, name, path="/dns-query"):
    """DoH 查询。返回 (HTTP状态码, DNS rcode, [A记录], 服务端证书, 错误)。

       必须把 DNS 层的 rcode 也解出来：设了访问白名单之后，AdGuard 对不带 ClientID 的
       查询是回【HTTP 200 + DNS REFUSED】——只看 HTTP 状态码会误判成"正常"。
       path 可带 ClientID（/dns-query/<id>），用来测真实客户端走的那条路。"""
    try:
        c = _tls_conn(port, sni)
    except Exception as e:
        return -1, -1, [], None, f"{type(e).__name__}: {e}"
    cert = c.getpeercert()          # 同上：服务端 Connection: close 后就取不到了
    try:
        d = base64.urlsafe_b64encode(_dns_packet(name)).rstrip(b"=").decode()
        c.sendall((f"GET {path}?dns={d} HTTP/1.1\r\nHost: {sni}\r\n"
                   "Accept: application/dns-message\r\nConnection: close\r\n\r\n").encode())
        buf = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode(errors="replace")
        m = re.search(r"\s(\d{3})\s", first + " ")
        code = int(m.group(1)) if m else -1
        rc, ips = _dns_parse(body) if body else (-1, [])
        return code, rc, ips, cert, ""
    except Exception as e:
        return -1, -1, [], cert, f"{type(e).__name__}: {e}"
    finally:
        try: c.close()
        except Exception: pass

def _cert_days_left(peercert):
    """优先用握手拿到的证书（那才是真正在服务的），拿不到就读证书文件。"""
    exp = ""
    if peercert and peercert.get("notAfter"):
        exp = peercert["notAfter"]
    elif os.path.exists(_crt()):
        out = sh(f"openssl x509 -enddate -noout -in {_crt()} 2>/dev/null")
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
    print("\n" + "=" * 60 + "\n  自建DNS 自检（只查不改；要改的查完再问）\n" + "=" * 60)
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
        fix.append("腾 53：自检查完会问（只认 systemd-resolved 占的）")
    else:
        print(f"    {warn} 53   没人监听 —— 明文DNS用不了（AGH 可能没配 53 或没起来）")
    if web:
        print(f"    {ok} {web:<5}网页后台(明文) http://{ip}:{web}")
    if enc_on and dom and p_doh and (not sname or sname == dom):
        print(f"    {ok} {p_doh:<5}网页后台(加密) {G}https://{dom}:{p_doh}{N}  {Y}← 推荐这样进，密码不明文{N}")
    if not enc_on:
        print(f"    {warn} 加密(DoT/DoH) {Y}没开{N} —— 安卓「私人DNS」用不了，只能用明文 53")
        if dom:
            fix.append("要用安卓私人DNS：后台 → 设置 → 加密设置，按菜单『2 使用方法』里的第二步填")
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
    # 设了访问白名单之后，必须【按真实客户端走的那条路】去测，否则全是假失败/假通过：
    # 不带 ClientID 的查询会被 AdGuard 拒（DoT 回 rcode=5，DoH 回 HTTP 200 + rcode=5，
    # 明文 53 直接不应答）。这些"失败"是白名单在正常工作，不是故障。
    cid_p  = _selfdns_clientid()
    wild_p = _cert_wildcard_ok(dom)
    allow_p = _yaml_list(txt, "allowed_clients") or []
    REFUSED = 5
    if allow_p:
        print(f"    {Y}已设访问白名单 → 下面按客户端真实用的地址测（带 ClientID）{N}")
    if "AdGuardHome" in b53:
        (rc, ips), err = _q_plain(53, probe)
        if rc == 0 and ips:
            print(f"    {ok} 明文 53  解析 {probe} → {ips[0]}")
        elif allow_p:
            print(f"    {warn} 明文 53  被白名单拒绝（{Y}预期之内{N}，53 没有 ClientID 机制）"
                  f"—— 电视/IoT 用不了这台 DNS")
        else:
            print(f"    {bad} 明文 53  {R}解析失败{N} {err or f'rcode={rc}'}")
            fix.append("53 解析不出来：看后台『设置 → DNS设置 → 上游DNS服务器』是否填了能用的上游")
    if enc_on and dom and p_dot:
        sni = f"{cid_p}.{dom}" if (cid_p and wild_p) else dom
        (rc, ips), peer, err = _q_dot(p_dot, sni, probe)
        tail = f"（SNI={sni}）" if sni != dom else ""
        if rc == 0 and ips:
            print(f"    {ok} DoT {p_dot}  握手+解析都正常 → {ips[0]}   {G}(安卓私人DNS 可用){N}{tail}")
        elif rc == REFUSED and allow_p:
            if cid_p and cid_p not in allow_p:
                print(f"    {bad} DoT {p_dot}  {R}被白名单拒绝{N}：当前 ClientID {cid_p} 不在名单里")
                fix.append(f"把当前 ClientID 加进「允许的客户端」: {cid_p}")
            elif not wild_p:
                print(f"    {bad} DoT {p_dot}  {R}被白名单拒绝{N}：没签泛域名证书，ID 传不进 SNI")
                fix.append("要用安卓 DoT 就回菜单选『5 DoT 带 ClientID』")
            else:
                print(f"    {bad} DoT {p_dot}  {R}被白名单拒绝{N}{tail} —— 名单里的 ID 与实际不符?")
        else:
            print(f"    {bad} DoT {p_dot}  {R}失败{N}: {err or f'rcode={rc}'}{tail}")
            print(f"       {Y}这一条挂掉 = 填了私人DNS 的安卓机会整机断网{N}")
            if "CERTIFICATE" in err.upper() or "SSLCert" in err:
                fix.append("DoT 证书校验没过：证书过期或填的不是 acme 真证书，去加密设置重填 "
                           f"{_crt()} / {_key()}")
            else:
                fix.append("DoT 连不上：确认加密设置里 DoT 端口填了、服务已重启")
    if enc_on and dom and p_dot and wild_p:
        okw, why = _wildcard_dns_check(dom)
        if okw:
            print(f"    {ok} 安卓主机名 {cid_p or 'ID'}.{dom} 解析到本机")
        else:
            print(f"    {bad} {why}")
            print(f"       {Y}手机按主机名找不到这台机器 →「无法访问私人 DNS 服务器」（上面直连本机测的照样通）{N}")
            fix.append(f"Cloudflare 里 *.{dom} 那条：代理状态改灰云（仅 DNS），内容填本机 IP")
    if enc_on and dom and p_doh:
        path = f"/dns-query/{cid_p}" if cid_p else "/dns-query"
        code, rc, ips, pc, err = _q_doh(p_doh, dom, probe, path)
        peer = peer or pc
        if code == 200 and rc == 0 and ips:
            print(f"    {ok} DoH {p_doh} 解析正常 → {ips[0]}（{path}）")
        elif code == 200 and rc == REFUSED:
            print(f"    {bad} DoH {p_doh} {R}被白名单拒绝{N}（HTTP 200 但 DNS rcode=5）"
                  f"—— 订阅里的 DoH 会失效")
            if cid_p and cid_p not in allow_p:
                fix.append(f"把当前 ClientID 加进「允许的客户端」: {cid_p}")
        else:
            print(f"    {bad} DoH {p_doh} {R}失败{N}: {err or f'HTTP {code} / rcode={rc}'}")
    days, exp = _cert_days_left(peer)
    if days is not None:
        tag = ok if days > 20 else (warn if days > 7 else bad)
        print(f"    {tag} 证书还有 {days} 天到期（{exp}）")
        if days <= 20:
            fix.append(f"证书 {days} 天后到期——续期后记得 systemctl restart AdGuardHome 让 AGH 重新加载")
    # 顺手验一下广告到底拦没拦。设了白名单时明文 53 会被拒、测不出结果，
    # 改走 DoH（带 ClientID）——那本来就是客户端真实用的路。
    ad = "doubleclick.net"
    rc = ips = None
    if allow_p and enc_on and dom and p_doh:
        _, rc, ips, _, _ = _q_doh(p_doh, dom, ad, f"/dns-query/{cid_p}" if cid_p else "/dns-query")
    elif "AdGuardHome" in b53:
        (rc, ips), _ = _q_plain(53, ad)
    if rc is not None:
        if rc == 3 or (ips and ips[0] in ("0.0.0.0", "127.0.0.1")) or (rc == 0 and not ips):
            print(f"    {ok} 广告过滤生效（{ad} 已被拦）")
        elif rc == 0 and ips:
            print(f"    {warn} {ad} 没被拦 → {ips[0]}；后台『过滤器』里确认拦截名单是开的")

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
            fix.append("要用安卓 DoT 就回菜单选『5 DoT 带 ClientID』签泛域名证书")
        elif cid:
            print(f"    {ok} 安卓 DoT 可用（已签泛域名证书）—— 手机填 {G}{cid}.{dom}{N}")
    else:
        print(f"    {warn} 没设允许列表 —— {R}对全公网开放，谁扫到都能当免费解析器用{N}")
        print(f"       {Y}明文 53 尤其要紧：UDP 可伪造源 IP，是 DNS 放大攻击的经典跳板。{N}")
        if cid:
            fix.append(f"关掉开放解析器：「允许的客户端」填入 ClientID {cid}"
                       + ("" if wild else "（但会挡掉安卓DoT，除非先用菜单 5 签泛域名证书）"))
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
    # 【不只是安全组】仓库主人 10/12：自检全绿、订阅里的 DoH(10443) 好好的，安卓私人 DNS 照样报
    # 「无法访问私人 DNS 服务器」——后台查询日志里却有手机的验证查询 xxxx-dnsotls-ds.metric.gstatic.com，
    # 说明服务器这头没毛病，是手机所在的网络时好时坏地拦 853。安卓私人 DNS 只走 853、改不了端口，
    # 服务器这边绕不过去，只能换网络或者私人 DNS 改「自动」交给代理。
    print(f"    {Y}提醒：本机没拦 ≠ 外面能连。{N}上面 4/6 全绿、手机还连不上，就是半路被拦（查完接着做手机实测能看清）：")
    print( "       ① 云商后台的安全组 / 防火墙没放行 853（DMIT/甲骨文等）")
    print( "       ② 手机所在的网络拦了 853（国内有的网络会拦；安卓私人 DNS 只走 853，换不了端口）")
    print( "          WiFi 和流量各试一次，一个行一个不行就是 ②：私人 DNS 改「自动」，开代理时 DNS 由代理接管")

    # ---- 上游分流（只读报状态，不判对错：没开也完全能用）----
    print("\n  【上游】")
    try:
        _ups = _yaml_list(open(_agh_yaml()).read(), "upstream_dns") or []
    except OSError:
        _ups = []
    _plain = [x for x in _ups if not x.startswith("[/")]
    print(f"    {ok if _plain else warn} 默认上游：{'、'.join(_plain) if _plain else '（空——国外域名没人解析）'}")
    # 【为什么单独查这一项】upstream_dns_file 一旦有值，AdGuardHome 就【只读那个文件】，
    # 网页后台『上游DNS服务器』那一栏整个被忽略 —— 后台照样显示着内容，改了也不生效，
    # 是最难自己看出来的一种「改了没反应」。所以这里点名报一句。
    try:
        _m = re.search(r'(?m)^\s*upstream_dns_file:[ \t]*(.*)$', open(_agh_yaml()).read())
    except OSError:
        _m = None
    _updf = _yaml_unquote(_m.group(1)) if _m else ""
    if _updf:
        print(f"    {warn} 配置里设了 upstream_dns_file：{_updf}")
        print(f"      {Y}上游只按这个文件走，后台『上游DNS服务器』那一栏当前被忽略{N}")
        fix.append(f"upstream_dns_file 指向 {_updf}，后台那一栏不生效；"
                   f"不需要的话把 {_agh_yaml()} 里这一行删掉再重启 AdGuardHome")

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

# ===================== 手机实测 =====================
# 仓库主人 10/14：自检全绿（证书、通配记录、白名单、带 ClientID 的 DoT 本机实测都通），手机私人 DNS 照样
# 「无法访问私人 DNS 服务器」。自检是在服务器上测的，看不见「手机 → 服务器」这一段。这里反过来：让手机去连，
# 服务器这边用 tcpdump 盯着 853，看手机的连接到没到、走到哪一步。只在本机看包，不往外发任何东西。
# 同一天第二轮：v1.1.90 看到手机认了证书就报「查询也在走 —— 全通」，其实查询到没到、手机收没收到回答都没看。
# 现在一直看到底：手机的查询到了没、AdGuard 回了没、手机确认收到没（手机回的 ACK 盖没盖过回答）。
# 白名单对不上时 AdGuard 照样回（回 REFUSED），光看包分不出来，所以再从 ClientHello 里读出手机填的主机名，
# 抓完用这个主机名在本机照安卓的检测查询问一遍 AdGuard（真 AdGuard Home 实测：ID 错 / 不带 ID 都是回 REFUSED）。
_TCPDUMP_RE = re.compile(r"IP6? (\S+)\.(\d+) > (\S+)\.(\d+): Flags \[([^\]]*)\]"
                         r"(?:, seq (\d+)(?::(\d+))?)?(?:, ack (\d+))?.*?length (\d+)")
_HEX_RE = re.compile(r"^\s+0x[0-9a-f]+:\s+([0-9a-f ]+?)\s*$")
_TS_RE = re.compile(r"^(\d\d):(\d\d):(\d\d(?:\.\d+)?) ")      # tcpdump 每行开头的时刻
_PROBE_GRACE = 15       # 最后一条新连接来了以后再看几秒就收：等查询、等回答、等手机确认（安卓下一轮验证要隔一分钟）
_PROBE_SETTLE = 5       # 来过的 IP 全通了、又这么久没来新连接，就不用干等到 _PROBE_GRACE

def _tcpdump_argv(port):
    return ["tcpdump", "-nn", "-l", "-x", "-i", "any", f"tcp port {port}"]

def _tls_sni(b):
    """ClientHello 里的 server_name，也就是手机私人 DNS 填的主机名。不是 TLS / 不完整回 ""。"""
    try:
        if b[0] != 0x16 or b[5] != 0x01:
            return ""
        p = 43                                              # 记录头 5 + 握手头 4 + 版本 2 + 随机数 32
        p += 1 + b[p]                                       # session id
        p += 2 + int.from_bytes(b[p:p + 2], "big")          # 加密套件
        p += 1 + b[p]                                       # 压缩方式
        end = p + 2 + int.from_bytes(b[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            t, n = int.from_bytes(b[p:p + 2], "big"), int.from_bytes(b[p + 2:p + 4], "big")
            if t == 0:                                      # server_name：列表长 2 + 类型 1 + 名字长 2 + 名字
                return b[p + 9:p + 9 + int.from_bytes(b[p + 7:p + 9], "big")].decode("ascii", "replace")
            p += 4 + n
    except IndexError:
        pass
    return ""

class _Cap:
    """边读 tcpdump（-x）边记每条连接：按顺序谁发了多少、手机确认收到哪儿、手机填的主机名。
       只记看得到手机 SYN 的新连接——抓包前就连着的看不全，多半也不是这台手机。本机自己的连接不算。"""
    def __init__(self, port, local=()):
        self.port, self.local, self.conns, self._pk = port, set(local), {}, None

    def feed(self, ln):
        m = _TCPDUMP_RE.search(ln)
        if not m:
            h = self._pk and _HEX_RE.match(ln)
            if h:
                self._pk[1].append(h.group(1))
            return
        self.done()
        ts = _TS_RE.match(ln)
        t = int(ts.group(1)) * 3600 + int(ts.group(2)) * 60 + float(ts.group(3)) if ts else None
        a, ap, b, bp, flags, s0, s1, ack, n = m.groups()
        ap, bp, n = int(ap), int(bp), int(n)
        if bp == self.port:
            ip, cport, inbound = a, ap, True
        elif ap == self.port:
            ip, cport, inbound = b, bp, False
        else:
            return
        if ip in self.local or ip.startswith("127.") or ip == "::1":
            return
        if inbound and "S" in flags and "." not in flags:     # 手机的 SYN（带 ECN 是 [SEW]）
            self.conns[(ip, cport)] = {"ip": ip, "seq": [], "ack": 0, "seen": set(), "hello": b"", "sni": None,
                                       "t0": t, "last": t, "oend": 0, "pend": [], "acked": {}, "rtxn": 0}
        c = self.conns.get((ip, cport))
        if not c:
            return
        if inbound and ack and int(ack) > c["ack"]:
            c["ack"] = int(ack)
            while c["pend"] and c["pend"][0] <= c["ack"]:     # 服务器发的这段，手机这一刻确认收到了
                c["acked"][c["pend"].pop(0)] = t
        if t is not None:
            c["last"] = t
        if n:                                           # 段：(手机发的?, 字节, 到第几字节, 时刻, 是不是重发)
            e, key = int(s1 or 0), (inbound, s0, s1)
            rtx = key in c["seen"] or (not inbound and 0 < e <= c["oend"])     # 原样重发 / 服务器合并重发
            c["seen"].add(key)
            if not inbound:
                c["oend"] = max(c["oend"], e)
                if rtx:
                    c["rtxn"] += 1
                elif e > c["ack"]:
                    c["pend"].append(e)
            c["seq"].append((inbound, n, e, t, rtx))
            if inbound and not rtx and c["sni"] is None:
                self._pk = (c, [])

    def done(self):
        """上一个包的十六进制收齐了：拼进 ClientHello，拼够一整条记录就读出主机名。"""
        if not self._pk:
            return
        c, hx = self._pk
        self._pk = None
        try:
            raw = bytes.fromhex("".join(hx).replace(" ", ""))
            if raw[0] >> 4 == 4:
                raw = raw[(raw[0] & 15) * 4:]
            elif raw[0] >> 4 == 6 and raw[6] == 6:
                raw = raw[40:]
            else:
                raise ValueError
            c["hello"] += raw[(raw[12] >> 4) * 4:]
        except (ValueError, IndexError):
            c["sni"] = ""
            return
        h = c["hello"]
        if len(h) >= 5 and (h[0] != 0x16 or len(h) >= 5 + int.from_bytes(h[3:5], "big") or len(h) > 16384):
            c["sni"], c["hello"] = _tls_sni(h), b""

# 一条连接走到了哪一步（好 → 坏）。TLS 里看不见内容，按「谁、按什么顺序、发了多少、手机确认到哪」判断：
#   手机先发 ClientHello，服务器回一串（证书等）；手机再发 ≥50 字节 = Finished（认了证书、握手走完），
#   查询跟在后面（光 Finished 是 64 / 80 字节，带上查询就过 120）；之后服务器单段 ≥40 字节的是回答
#   （AdGuard 先单发 2 字节长度、再发报文，前一段只有 24 字节）；手机的 ACK 盖过回答 = 手机收到了。
# 10/15 第五轮：v1.1.93 只核对了头一个回答，屏上「回了 5 个查询、手机也收到了 ✔」，查询日志里手机却 5 秒后
#   又发了一次检测——后面的回答手机没收到。现在每个回答逐个核对：发出去 1 秒还没确认（服务器在重发）就算丢了；
#   抓包快结束才发、还没来得及确认的不算。前面收到、后面丢了 = partlost（线路先放行、后掐断，最典型的干扰）。
_PROBE_RANK = ["ok", "partlost", "lost", "noanswer", "handshake", "reject", "flightlost", "noreply", "path"]

def _conn_split(c):
    """头一回发的段（重发的不算）切成：服务器头一回回数据的位置 k、Finished 之后那一截 tail、手机在那截里发了多少 cl、
       那截里的回答 ans。还没走到那一步的，返回里对应的是 None。"""
    seq = [x for x in c["seq"] if not x[4]]
    k = next((i for i, x in enumerate(seq) if not x[0]), len(seq))     # 服务器头一回发数据
    if not k or k == len(seq):
        return seq, k, None, 0, None
    rest = seq[k:]
    f = next((i for i, x in enumerate(rest) if x[0]), len(rest))       # 手机接着发的头一段（Finished）
    tail = rest[f:]
    return seq, k, rest[:f], sum(x[1] for x in tail if x[0]), [x for x in tail if not x[0] and x[1] >= 40]

def _answers_fate(c, ans):
    """→ (手机确认收到几个, 丢了几个)：没确认、而且发出去以后这条连接又过了 1 秒还有包（服务器在重发 / 手机还在发别的）就算丢。"""
    got = sum(1 for x in ans if c["ack"] >= x[2])
    lost = sum(1 for x in ans if c["ack"] < x[2] and x[3] is not None and c.get("last") is not None
               and (c["last"] - x[3]) % 86400 >= 1.0)
    return got, lost

def _conn_kind(c):
    seq, k, flight, cl, ans = _conn_split(c)
    if not k:
        return "path"                                   # 只有 TCP 握手，手机一个数据都没发过来
    if k == len(seq):
        return "noreply"                                # 手机发了 ClientHello，服务器没回
    if cl < 50:                                         # 手机没走完握手：收到证书了（确认过）就是不认，没收到就是半路丢了
        return "reject" if c["ack"] >= max(x[2] for x in flight) else "flightlost"
    if not ans:
        return "noanswer" if cl > 120 else "handshake"
    got, lost = _answers_fate(c, ans)
    if lost:
        return "partlost" if got else "lost"
    return "ok" if got else "lost"                      # 一个都还没确认的（刚发出去）也先算 lost

def _conn_stats(c):
    """→ (AdGuard 回了几个查询, 手机收到几个, 丢了几个, 从手机连上到头一个回答几秒, AdGuard 最慢一个回答几秒,
          回答送到手机最慢几秒, 服务器重发了几次)。没时刻的秒数是 None。
       仓库主人 10/15：全局代理能连上、直连不行，问「是不是返回的时候代理和直连不一样」——直连时运营商看得见这是 853 的
       DoT，回程的回答可能被丢了又重发、或者被拖慢，最后是到了，可安卓只等几秒，早判失败了。所以回程走了多久也要量。
       仓库主人 10/15：实测两个网络都 ✔，手机照样报「无法访问」——光说通不够，还得看快不快：
       安卓等回答只等几秒，回得慢（上游慢 / 线路慢）它照样判失败，而包是一个不少都到了。"""
    seq, k, flight, cl, ans = _conn_split(c)
    if ans is None:
        return 0, 0, 0, None, None, None, c["rtxn"]
    t0, first, slow, q = c.get("t0"), None, None, None
    for x in seq[k:][len(flight):]:
        inbound, size, _end, t, _rtx = x
        if inbound:
            q = t                                       # 手机最近发的那段（查询）
        elif size >= 40:
            if first is None and t is not None and t0 is not None:
                first = (t - t0) % 86400               # 跨零点也对
            if t is not None and q is not None:
                slow = max(slow or 0.0, (t - q) % 86400)
    back = [(c["acked"][x[2]] - x[3]) % 86400 for x in ans
            if x[2] in c["acked"] and c["acked"][x[2]] is not None and x[3] is not None]
    return (len(ans), *_answers_fate(c, ans), first, slow, max(back) if back else None, c["rtxn"])

_PROBE_SLOW_AGH, _PROBE_SLOW_ALL = 2.0, 4.0     # AdGuard 回一个查询超过这么久 / 从连上到回答超过这么久，就算慢
_PROBE_SLOW_BACK = 1.5                          # 回答从服务器发出到手机确认超过这么久，就算回程慢（正常是一个来回，零点几秒）

def _probe_verdicts(cap):
    """→ [(结论, 远端IP, 手机填的主机名, 回了几个, 手机收到几个, 连上到回答几秒, AdGuard 最慢几秒, 回程最慢几秒, 重发几次)…]：
       每个 IP 取走得最远的那条连接，按先来后到；个数加总、快慢取那个 IP 所有连接里最快到 / 最慢回的。
       手机的 WiFi 和流量出口不同，各算一个 IP、各验各的（10/15 截图：来了两个 IP，只报了通的那个，
       没通的那个被盖住了）。那个 IP 哪条连接丢过回答，就不算全通（partlost）；通是通、但慢到安卓等不及的，
       结论改成 slowagh（AdGuard 慢）/ slowback（回程慢：回答在路上被丢了重发、或被拖住）/ slowpath（线路慢）。"""
    best, n, got, lost, first, slow, back, rtx = {}, {}, {}, {}, {}, {}, {}, {}
    for c in cap.conns.values():
        ip, kind = c["ip"], _conn_kind(c)
        if ip not in best or _PROBE_RANK.index(kind) < _PROBE_RANK.index(best[ip][0]):
            best[ip] = (kind, c["sni"] or "")
        cn, cg, cl, cf, cs, cb, cr = _conn_stats(c)
        n[ip], got[ip], lost[ip], rtx[ip] = n.get(ip, 0) + cn, got.get(ip, 0) + cg, lost.get(ip, 0) + cl, rtx.get(ip, 0) + cr
        if cb is not None:
            back[ip] = max(back.get(ip, cb), cb)
        if cf is not None:
            first[ip] = min(first.get(ip, cf), cf)
        if cs is not None:
            slow[ip] = max(slow.get(ip, cs), cs)
    out = []
    for ip, (kind, sni) in best.items():
        if kind == "ok" and lost[ip]:
            kind = "partlost"
        elif kind == "ok" and (slow.get(ip) or 0) >= _PROBE_SLOW_AGH:
            kind = "slowagh"
        elif kind == "ok" and (back.get(ip) or 0) >= _PROBE_SLOW_BACK:
            kind = "slowback"
        elif kind == "ok" and (first.get(ip) or 0) >= _PROBE_SLOW_ALL:
            kind = "slowpath"
        out.append((kind, ip, sni, n[ip], got[ip], first.get(ip), slow.get(ip), back.get(ip), rtx[ip]))
    return out

_PROBE_SAY = {
    "ok":         ("✔", "{ip} 连进来了，回了 {n} 个查询、手机都收到了{took}—— 这条线路通"),
    "partlost":   ("✗", "{ip} 回了 {n} 个查询，手机只收到 {got} 个 —— 后面的回答在半路被丢（先放行、后掐断）"),
    "slowagh":    ("⚠", "{ip} 通是通，可 AdGuard 回得慢（最慢 {slow:.1f} 秒）—— 安卓只等几秒，等不到就判「无法访问」"),
    "slowback":   ("⚠", "{ip} 都收到了，可回答送到手机最慢用了 {back:.1f} 秒{rtxs}—— 回程在丢包 / 拖延，安卓只等几秒就判「无法访问」"),
    "slowpath":   ("⚠", "{ip} 通是通，可从连上到回答用了 {first:.1f} 秒 —— 线路太慢，安卓只等几秒就判「无法访问」"),
    "lost":       ("✗", "{ip} 的查询到了、AdGuard 也回了，可手机一直没确认收到 —— 回答在半路被丢"),
    "noanswer":   ("✗", "{ip} 的查询到了，AdGuard 没回 —— systemctl restart AdGuardHome 再测一次"),
    "handshake":  ("✗", "{ip} 握手走完了（证书没问题），可查询一直没到 —— 半路被拦"),
    "reject":     ("⚠", "{ip} 连进来了，拿到证书就断开 —— 手机不认这张证书，或 TLS 握手没过"),
    "flightlost": ("✗", "{ip} 连进来了，可服务器发的证书手机没收到 —— 半路被拦"),
    "noreply":    ("✗", "收到 {ip} 的握手，服务器没回 —— systemctl restart AdGuardHome 再测一次"),
    "path":       ("✗", "收到 {ip} 的连接请求，可握手没走下去 —— 手机的请求半路被拦了（手机所在网络 / 运营商拦 {port}），服务器这边没法绕"),
}

def _probe_host_msg(sni, ask, sname, cid):
    """手机的主机名在本机那一问没过 → (✗ 那行, 提示)。"""
    s = sni.lower()
    pid = s[:-len(sname) - 1] if sname and s.endswith("." + sname) else ""
    if ask[0] == 5:
        why = "没带 ClientID" if s == sname else (f"ClientID「{pid}」不在白名单里" if pid else "跟 AdGuard 的域名对不上")
        tip = (f"AdGuard 后台 → 设置 → DNS 设置 → 允许的客户端，把 {cid} 加回去" if cid and pid == cid else
               f"手机私人 DNS 改填：{cid}.{sname}" if cid and sname else "回菜单 5 把 ClientID 弄好，再按『2 使用方法』填手机")
        return f"手机填的主机名是 {sni} —— {why}，AdGuard 拒了它的查询", tip
    if ask[0] < 0:
        return f"用手机的主机名 {sni} 在本机问 AdGuard 出错：{ask[2]}", "把这屏截图发来再查"
    return (f"AdGuard 答不出安卓的检测域名（rcode={ask[0]}，没有地址）—— 手机收不到地址就判「无法访问」",
            "后台 → 查询日志搜 dnsotls，看是被过滤规则拦了还是上游没回")

def _probe_ask(port, sni):
    """用手机填的主机名，在本机照安卓的检测查询问一遍 AdGuard → (rcode, [IP], 错误)。"""
    (rc, ips), _cert, err = _q_dot(port, sni, f"{secrets.token_hex(4)}-dnsotls-ds.metric.gstatic.com")
    return rc, ips, err

def phone_probe(secs=60):
    """菜单 9 自检之后：手机实测——让手机去连私人 DNS，服务器这边盯着 853，说清卡在哪一段。"""
    G, Y, R, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    if not (_installed() and _running()):
        print("  AdGuardHome 没在运行——先选 1 安装，或选 9 自检看原因。"); return
    try: txt = open(_agh_yaml()).read()
    except OSError: txt = ""
    tb = _tls_block(txt)
    port = int(_yaml_val(tb, "port_dns_over_tls") or 0)
    if _yaml_val(tb, "enabled") != "true" or not port:
        print("  加密（DoT）没开，安卓私人 DNS 本来就用不了——先按『2 使用方法』把加密开上。"); return
    if not shutil.which("tcpdump"):
        ui_tip("要装抓包工具 tcpdump（只在本机看包，不往外发任何东西）")
        if _ask("  装? [Y/n]: ").strip().lower() in ("n", "no"):
            return
        sh("DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y tcpdump")
        if not shutil.which("tcpdump"):
            print("  ⚠ tcpdump 没装上，这项测不了。"); return
    ui_title("手机实测（看手机连没连进来）")
    ui_tip("先把手机上的代理（VPN）关掉")
    print("  现在拿手机：私人 DNS 先选「关闭」，再选回主机名 → 保存")
    print(f"  {secs} 秒内我在这边看着 {port} 端口（Ctrl-C 提前结束）…")
    # 本机自己的 IP 来的连接不再滤掉：手机开着代理、选的又是这台 VPS 上的节点时，私人 DNS 的流量是节点从本机绕回来的
    # （10/15：规则模式连不上，那一路实测里一直看不见）。自检走 127.0.0.1，照旧不算。
    me = set(sh("hostname -I").split())
    who = lambda ip: "本机节点转来的那路" if ip in me else ip
    cap = _Cap(port, {"127.0.0.1", "::1"})
    import select
    seen, rest = set(), b""
    p = subprocess.Popen(_tcpdump_argv(port), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fd = p.stdout.fileno()                      # 直接读 fd：用 readline 的话 Python 先把整段吞进缓冲，select 就再也叫不醒
    try:
        end = time.time() + secs
        stop, fresh, n = end, 0.0, 0            # stop：最晚收工；fresh：最近一条新连接来的时间
        while time.time() < stop:
            if select.select([fd], [], [], 0.5)[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                *done, rest = (rest + chunk).split(b"\n")
                for ln in done:
                    cap.feed(ln.decode(errors="replace"))
                if len(cap.conns) > n:          # 来了新连接（WiFi 和流量各来各的）：从它算起再看一会儿
                    n, fresh = len(cap.conns), time.time()
                    stop = min(end, fresh + _PROBE_GRACE)
                    for c in cap.conns.values():
                        if c["ip"] not in seen:
                            seen.add(c["ip"])
                            print("  · 收到经本机节点转来的连接（手机开着代理）" if c["ip"] in me else f"  · 收到 {c['ip']} 的连接")
            elif p.poll() is not None:
                break
            vs = _probe_verdicts(cap)
            if vs and all(v[0] == "ok" for v in vs) and time.time() - fresh >= _PROBE_SETTLE:
                break                           # 来过的全通了，又静了一阵没新连接：不用干等
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()
        try: p.wait(3)
        except Exception: p.kill()
    cap.done()
    vs = _probe_verdicts(cap)
    # 握手走完了的，用手机填的主机名在本机问一遍（同一个主机名问一次）：白名单对不上 / 检测域名解析不出来，手机都会判「无法访问」
    asks = {}
    for kind, _ip, sni, *_st in vs:
        if sni and kind in ("ok", "slowagh", "slowback", "slowpath", "partlost", "lost", "noanswer", "handshake") and sni not in asks:
            asks[sni] = _probe_ask(port, sni)
    bad = {s: a for s, a in asks.items() if not (a[0] == 0 and a[1])}
    print()
    col = {"✔": G, "✗": R, "⚠": Y}
    for kind, ip, sni, n, got, first, slow, back, rtx in vs:    # 每个 IP 一行
        if kind == "ok" and sni in bad:
            continue                            # 主机名被拒：包是通的也不算通，只报下面主机名那行
        mark, say = _PROBE_SAY[kind]
        rtxs = f"（服务器重发了 {rtx} 次）" if rtx else " "
        took = (f"（连上到回答 {first:.1f} 秒" + (f"，服务器重发了 {rtx} 次" if rtx else "") + "）") if first is not None else " "
        print(f"  {col[mark]}{mark} " + say.format(ip=who(ip), port=port, n=n, got=got, took=took, rtxs=rtxs,
                                                   first=first or 0, slow=slow or 0, back=back or 0) + N)
    if not vs:
        print(f"  {R}✗ {secs} 秒里外面一个连到 {port} 的包都没收到 —— 手机的请求半路被拦了（手机所在网络 / 运营商拦 {port}），服务器这边没法绕{N}")
    sname = (_yaml_val(tb, "server_name") or _domain() or "").lower()
    try: cid = open(SELFDNS_CID_FILE).read().strip()
    except OSError: cid = ""
    for sni in asks:                            # 主机名（带 ID）认没认，每次都直说（10/15：「不会是被 ID 拦截了吧」）
        if sni not in bad:
            has_id = sname and sni.lower().endswith("." + sname)
            print(f"  {G}✔ 手机填的主机名 {sni}：" + ("AdGuard 认这个 ID，没被拦" if has_id else "AdGuard 照常回答") + N)
    host = [_probe_host_msg(s, a, sname, cid) for s, a in bad.items()]
    for line, _tip in host:
        print(f"  {R}✗ {line}{N}")
    kinds = {v[0] for v in vs}                  # 提示只留一行，挑最要紧的那件
    if host:
        ui_tip(host[0][1])
    elif not vs or kinds & {"handshake", "partlost", "lost", "flightlost", "path", "slowpath", "slowback"}:
        ui_tip(f"网络在干扰 {port}：WiFi 和流量各测一次；都不行就私人 DNS 改「自动」，开代理时 DNS 由代理接管")
    elif "slowagh" in kinds:
        ui_tip("AdGuard 后台 → 设置 → DNS 设置 → 上游 DNS 服务器：点「测试上游」，慢的、不通的删掉")
    elif "reject" in kinds:
        ui_tip("把这屏和手机的报错截图发来再查")
    elif kinds == {"ok"}:
        ui_tip("手机还报连不上：AdGuard 后台 → 查询日志搜 dnsotls，截图发来")

def _selfdns_toggle():
    """把"自建DNS写入订阅"的开关交给主脚本处理（订阅生成/刷新都在主脚本里）。"""
    xy = BGP_DIR + "/xy-installer.py"
    if not os.path.exists(xy):
        print("  没找到主脚本(xy-installer.py)，请从主面板进入本菜单。"); return
    was = os.path.exists(SELFDNS_FLAG)
    rc = subprocess.run(f"python3 {xy} selfdns-toggle", shell=True).returncode
    # 结果自己再核一遍、停一下再回菜单：仓库主人 10/20 选 6 屏上一个字没出就回了菜单、也没写进去，
    # 第二次直接跑主脚本又好了，没复现出来。至少别再「悄悄没反应」：退出码不是 0、或者要写却没写进去，都说一行
    if rc:
        print("  " + _hl("1;31", f"⚠ 主脚本出错退出（退出码 {rc}），没改动；直接跑看原因：python3 {xy} selfdns-toggle"))
    elif not was and not os.path.exists(SELFDNS_FLAG):
        print("  " + _hl("1;33", "⚠ 自建 DNS 没写进订阅（原因见上面；没有原因就再选一次 6）"))
    _ask("  按回车返回菜单…")

def _privdns_root():
    """菜单 4 开着「节点专用域名」（用域名装的机器，私有名是 随机.根域名）→ 根域名；没有返回 ''。
       共用证书上多签着 *.根域名 盖住它：这边重签证书（菜单 5、撤销）一定要带上，丢了节点就握手失败。"""
    try:
        import json
        c = json.load(open(PRIVDNS_FILE))
    except Exception:
        return ""
    n = str(c.get("name") or "")
    if c.get("mode") not in ("self", "remote") or not re.match(r"^[a-z0-9]{6,32}\.(?:[a-z0-9-]+\.)+[a-z]{2,}$", n) \
            or n.endswith(".internal"):
        return ""
    return n.split(".", 1)[1]

def _privdns_mode():
    """菜单 4 的「当前」：主脚本存在 privdns.json 里（off / self / remote）。"""
    try:
        import json
        m = json.load(open(PRIVDNS_FILE)).get("mode")
    except Exception:
        m = ""
    return m if m in ("self", "remote") else "off"

def _privdns_menu():
    """菜单 4「节点使用私有DNS解析」：交给主脚本（订阅生成、.links 都在主脚本里），跟 3 一个路子。
       主脚本是老版本就不叫——不认的子命令会掉进 argparse 报错退出，屏上一句话说清先更新。"""
    xy = BGP_DIR + "/xy-installer.py"
    try:
        src = open(xy).read()
    except OSError:
        print("  没找到主脚本(xy-installer.py)，请从主面板进入本菜单。"); return
    if '"privdns"' not in src:
        print("  " + _hl("1;33", "⚠ 主脚本是老版本：先到主面板「更新脚本」→ 1 立即更新，再来"))
        _ask("  按回车返回菜单…"); return
    rc = subprocess.run(f"python3 {xy} privdns", shell=True).returncode
    if rc:
        print("  " + _hl("1;31", f"⚠ 主脚本出错退出（退出码 {rc}）；直接跑看原因：python3 {xy} privdns"))
        _ask("  按回车返回菜单…")

def _privdns_remove():
    """卸载 AdGuard 时：本机在当 DNS 机（菜单 4 选了 1），交给主脚本关掉、外机节点改回真实地址。"""
    xy = BGP_DIR + "/xy-installer.py"
    if os.path.exists(xy) and '"privdns-off"' in open(xy).read():
        subprocess.run(f"python3 {xy} privdns-off", shell=True)

def _ios_fetch(dom, port, path):
    """从本机拉一遍描述文件（SNI 用域名，连本机，不出网）：→ (ServerName 或 '', 出错原因)。
       AdGuard 的 HTTPS 后台跟 http.address 绑同一个地址，0.0.0.0 / 空 就连 127.0.0.1。"""
    import plistlib
    try:
        m = re.search(r'(?m)^\s*address:\s+(\S+):(\d+)\s*$', open(_agh_yaml()).read())
    except OSError:
        m = None
    host = m.group(1).strip("[]") if m else ""
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE   # 本机自检，只看内容
    try:
        with socket.create_connection((host, port), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname=dom) as t:
            t.sendall(f"GET {path} HTTP/1.0\r\nHost: {dom}:{port}\r\nUser-Agent: {HTTP_UA}\r\n\r\n".encode())
            buf = b""
            while True:
                d = t.recv(65536)
                if not d:
                    break
                buf += d
    except OSError as e:
        return "", f"连不上本机 {host}:{port}（{e.__class__.__name__}）"
    head, _, body = buf.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        return "", "AdGuard 没给出描述文件（" + head.split(b"\r\n", 1)[0].decode(errors="replace") + "）"
    try:
        pl = plistlib.loads(body)
        return pl["PayloadContent"][0]["DNSSettings"]["ServerName"], ""
    except Exception:
        return "", "描述文件内容不对"

def ios_profile():
    """苹果手机（iOS 14+）没有安卓那种「专用 DNS」框，要装 .mobileconfig 描述文件。
       用 AdGuard 自带的 /apple/dot.mobileconfig（不用登录就能下）：DoT 走 853，跟安卓专用 DNS 同一条路。
       【不给 DoH 版】实测 v0.107.79：/apple/doh.mobileconfig 的 ServerURL 写死成 https://域名/dns-query/…，
       不带端口；我们的 DoH 在 port_https（默认 10443），443 是节点的，照它装苹果连不上。
       ClientID 规则跟安卓一样：签了泛域名证书才带（主机名 ID.域名），没签就只给域名；
       白名单已设又没签泛域名 → 苹果也会被挡，跟安卓同一句提醒。
       装之前在本机按同一个链接拉一遍，核对里面的主机名，屏上一行 ✔ / ⚠。
       链接里带 ClientID（等于口令），只印到 root 自己的屏上。"""
    ui_title("苹果手机描述文件（iOS 14+）")
    if not _installed():
        print("  ⚠ 还没装 AdGuard Home，先选 1 安装"); return
    hp = _https_panel()
    if not hp:
        print("  ⚠ 没域名或没开加密（DoT），苹果描述文件用不了")
        ui_tip("先按菜单 2「使用方法」第二步开加密"); return
    dom, port = hp
    wild = _cert_wildcard_ok(dom); allow = _allowed_clients()
    cid = _selfdns_clientid() if wild else ""
    want = f"{cid}.{dom}" if cid else dom
    path = f"/apple/dot.mobileconfig?host={dom}" + (f"&client_id={cid}" if cid else "")
    if allow and not wild:
        print(_hl("1;31", "⚠ 白名单已设、没签泛域名证书：描述文件带不上 ClientID，苹果会被挡"))
        ui_tip("二选一：菜单 5 签泛域名证书；或后台清空「允许的客户端」")
    print()
    ui_url("Safari 打开这个链接，下载描述文件", f"https://{dom}:{port}{path}")
    print()
    ui_url("装好后苹果走的 DNS（DoT）", want)
    print()
    got, why = _ios_fetch(dom, port, path)
    if got == want:
        print("  " + _hl("1;32", f"✔ 本机试拉成功，描述文件里的主机名是 {got}"))
    else:
        print("  " + _hl("1;33", "⚠ " + (why or f"描述文件里的主机名是 {got}，应为 {want}")))
    ui_tip("装法：设置 → 已下载描述文件 → 安装；开着小火箭时 DNS 由小火箭接管，订阅里已带自建 DNS")

def _bcrypt(pw):
    """AdGuard 认的密码哈希（bcrypt）；做不出来返回 ''。
       Python 标准库没有 bcrypt，用 apache2-utils 的 htpasswd（AdGuard 官方 FAQ 也是这么教的）；没装就装上。
       密码从标准输入喂进去（-i），不放在命令行上（ps 看得见）。10/31 真 AdGuard Home v0.107.79 实测认 $2y$ 开头的。"""
    if not shutil.which("htpasswd"):
        subprocess.run("apt-get install -y apache2-utils >/dev/null 2>&1", shell=True,
                       env=dict(os.environ, DEBIAN_FRONTEND="noninteractive"))
    if not shutil.which("htpasswd"):
        return ""
    r = subprocess.run(["htpasswd", "-niBC", "10", ""], input=pw, text=True, capture_output=True)
    h = (r.stdout or "").strip().lstrip(":")
    return h if r.returncode == 0 and h.startswith("$2") else ""

def _drop_sessions():
    """改了用户名 / 密码，把网页上已登录的会话全踢掉（仓库主人 10/31：「修改了密码网页端竟然没有断开」）。
       AdGuard 的登录会话存在 data/sessions.db，换密码不碰它，旧 cookie 照样能用；
       服务停着时删掉这个文件，起来后自己重建、所有人都得用新账号重登。10/31 真 v0.107.79 实测：删前旧 cookie 200，删后 401。"""
    try: os.remove(AGH_DIR + "/data/sessions.db")
    except OSError: pass

def change_admin_password():
    """菜单 8 → 2 修改后台密码（仓库主人 10/31：「可不可以用代码修改密码」→「加」）。
       AdGuard 没有改密码的命令也没有接口，密码就是 AdGuardHome.yaml 里 users 那段的 bcrypt 哈希：
       停服务 → 换掉那一行 → 起服务 → 拿新密码登一次接口验证；登不进去就把原配置放回去、再起一次，绝不把人锁在外面。"""
    G, Y, R, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    if os.geteuid() != 0:
        print("  需要 root。"); return
    users = _agh_users()
    if not _installed() or not users:
        ui_tip("后台账号还没设：先选 1"); return
    user = users[0]
    path = _agh_yaml()
    try:
        txt = open(path).read()
    except OSError:
        print("  读不到 AdGuard 配置文件。"); return
    m = re.search(r"(?m)^users:\s*\n(?:[ \t]+.*\n)*?([ \t]+password:[ \t]*)(\S.*)$", txt)
    if not m:
        print("  配置里没找到密码那一行，保险起见不改。"); return
    print()
    ui_kv("后台用户名", user)
    while True:
        pw = _ask_fresh("  新密码（至少 8 位；回车 = 取消）: ").strip()
        if not pw:
            print("  已取消，没改"); return
        if len(pw) >= 8 and pw.lower() not in (user.lower(), (_own_domain() or "").lower()):
            break
        print(f"  {Y}✗ {'至少 8 位' if len(pw) < 8 else '别跟用户名 / 域名一样'}，再输一次{N}")
    h = _bcrypt(pw)
    if not h:
        print(f"  {R}✗ 生成不了密码哈希（htpasswd 装不上），没改{N}"); return
    new = txt[:m.start(2)] + h + txt[m.end(2):]
    sh("systemctl stop AdGuardHome")
    open(path, "w").write(new)
    _drop_sessions()                                 # 网页上已登录的都踢掉，得用新密码重登
    sh("systemctl start AdGuardHome")
    port = _current_web_port()
    ok = False
    for _ in range(15):
        time.sleep(1)
        if _agh_api("/control/status", port=port, auth=(user, pw))[0] == 200:
            ok = True; break
    if ok:
        print(f"  {G}✔ 后台密码改好了，网页已登出，用新密码登录（用户名 {user}）{N}")
        return
    sh("systemctl stop AdGuardHome")
    open(path, "w").write(txt)                       # 放回原配置：旧密码照旧能用
    sh("systemctl start AdGuardHome")
    print(f"  {R}⚠ 新密码登不进去，已改回原来的密码{N}")

def change_admin_name():
    """改后台用户名：换掉 AdGuardHome.yaml 里 users 段第一个 - name:。密码不动（哈希跟用户名无关）。
       停服务 → 写 → 起服务，15 秒内没起来就放回原配置。"""
    G, Y, R, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    if os.geteuid() != 0:
        print("  需要 root。"); return
    users = _agh_users()
    if not _installed() or not users:
        ui_tip("后台账号还没设：先选 1"); return
    path = _agh_yaml()
    try:
        txt = open(path).read()
    except OSError:
        print("  读不到 AdGuard 配置文件。"); return
    m = re.search(r"(?m)^users:\s*\n(?:[ \t]+.*\n)*?([ \t]*-[ \t]*name:[ \t]*)(\S.*)$", txt)
    if not m:
        print("  配置里没找到用户名那一行，保险起见不改。"); return
    while True:
        name = _ask_fresh("  新用户名（回车 = 取消）: ").strip()
        if not name:
            print("  已取消，没改"); return
        if name == users[0]:
            print("  跟现在一样，没改"); return
        # 登录用的是 HTTP 基本认证（用户名:密码），冒号、空白、引号会出岔子
        if re.fullmatch(r"[^\s:'\"#]{1,64}", name):
            break
        print(f"  {Y}✗ 别带空格、冒号、引号、#，再输一次{N}")
    new = txt[:m.start(2)] + name + txt[m.end(2):]
    sh("systemctl stop AdGuardHome")
    open(path, "w").write(new)
    _drop_sessions()
    sh("systemctl start AdGuardHome")
    port = _current_web_port()
    for _ in range(15):
        time.sleep(1)
        if _agh_api("/control/status", port=port)[0] in (401, 403, 200, 302):
            print(f"  {G}✔ 后台用户名改成 {name}，密码不变，网页已登出{N}")
            return
    sh("systemctl stop AdGuardHome")
    open(path, "w").write(txt)
    sh("systemctl start AdGuardHome")
    print(f"  {R}⚠ 改完后台没起来，已改回原来的用户名{N}")

def change_admin_account():
    """菜单 12「修改用户名和密码」（仓库主人 10/31 定的样子：1 修改后台用户名　当前：… / 2 修改后台密码 / 0 返回）。"""
    while True:
        users = _agh_users()
        print()
        ui_items([(1, "修改后台用户名", users[0] if users else "还没设"),
                  (2, "修改后台密码", None),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            change_admin_name()
        elif c == "2":
            change_admin_password()
        else:
            return

def menu():
    while True:
        # 仓库主人 10/31 重排：常用的在前、卸载放最后；「腾出 53 端口」「手机实测」并进「自检」（查到 53 被占才问腾不腾，
        # 查完再问要不要手机实测，都默认否），两项功能都还在。
        # 4 节点使用私有DNS解析：1 本机当 DNS 机 / 2 本机节点交给外机解析 / 3 关（交给主脚本，见 _privdns_menu）。
        # 5 DoT 带 ClientID：签泛域名证书，设了白名单后安卓「专用 DNS」仍可用。7 改后台端口：随机 2000-5000 / 自定义，带回滚。
        # 10 卸载：整套撤干净（服务 + 数据、订阅里的自建 DNS、泛域名证书、腾 53 的改动），不动节点。
        ui_title("自建 DNS · AdGuard Home（兼带广告过滤）")
        if _installed():
            st = "已安装 · " + (_hl("1;32", "运行中") if _running() else _hl("1;31", "未运行"))
        else:
            st = _hl("2", "未安装")
        ui_kv("当前状态", st)
        ui_line()
        ui_items([(1, "安装", None),
                  (2, "使用方法（设备怎么填）", None),
                  (3, "自建 DNS 写入订阅", "已写入" if os.path.exists(SELFDNS_FLAG) else "未写入"),
                  (4, "节点使用私有DNS解析", {"self": "本机", "remote": "外机"}.get(_privdns_mode(), "关")),
                  (5, "DoT 带 ClientID（签泛域名证书）", None),
                  (6, "苹果手机描述文件（iOS 一键装 DNS）", None),
                  (7, "改后台端口", None),
                  (8, "修改用户名和密码", None),
                  (9, "自检（含手机实测）", None),
                  (10, "卸载自建DNS", None),
                  (0, "退出", None)])
        c = _ask("选择: ").strip()
        if c == "1":   install()
        elif c == "2": status()
        elif c == "3": _selfdns_toggle()
        elif c == "4": _privdns_menu()
        elif c == "5": dot_clientid()
        elif c == "6": ios_profile()
        elif c == "7": change_web_port()
        elif c == "8": change_admin_account()
        elif c == "9": selfcheck(); _selfcheck_more()
        elif c == "10": uninstall()
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

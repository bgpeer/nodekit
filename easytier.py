#!/usr/bin/env python3
# easytier.py —— 一键组 EasyTier 虚拟内网（多台 VPS 在一个内网里互访）
# 独立文件，nodekit 主脚本(xy-installer.py)通过子进程调用：
#   python3 easytier.py            交互菜单（安装 / 配置组网 / 状态 / 开机自启 / 卸载）
#   python3 easytier.py remove --yes     菜单 20 那边调：整套撤干净
#
# 定位：把几台 VPS 连成一个虚拟局域网（默认 10.144.0.0/24），互相用内网 IP 访问。
#   - EasyTier 是去中心化 mesh VPN：没有中心服务器，节点之间自己打洞直连，打不通才走中继；
#     加密默认开（aes-gcm）。同名同密钥 = 同一个网，不知道这两样进不来。
#   - 不动 sing-box/xray/节点/订阅：独立服务，卸载彻底、互不影响。
#   - VPS 上不跑 mihomo（mihomo 在 nodekit 里只是生成给客户端的订阅格式），所以这里直接跑
#     easytier-core，不经过 mihomo 的 easytier 出站。手机/电脑上的 mihomo 客户端想进网，
#     用它自己的 easytier 出站配置，跟这台机器上的服务没关系。
#   - 云防火墙的 TCP 11010 脚本够不着：首节点那台要在厂商面板上放行，菜单里会提醒。
#   - 密钥存在 /etc/bgpeer/easytier.conf 和给服务的 /etc/bgpeer/easytier.toml 里
#     （都是 0600，只有 root 能读），跟 AdGuard 的 agh.password 一个做法；进菜单 3 状态
#     能再看到，配另一台机器时照着抄过去。服务只拿 -c 指配置文件，密钥不进服务文件、
#     也不进进程命令行（ps 就能看到）。
import os, re, sys, json, shlex, socket, shutil, secrets, subprocess, urllib.request, ipaddress

BGP_DIR = "/etc/bgpeer"
ET_CONF = BGP_DIR + "/easytier.conf"      # 网络名 / 密钥 / 内网 IP / 角色 / 对端（0600）
ET_TOML = BGP_DIR + "/easytier.toml"      # 给服务的 TOML 配置（0600，密钥只放这里）
ET_BIN = "/usr/local/bin/easytier-core"
ET_CLI = "/usr/local/bin/easytier-cli"
ET_UNIT = "/etc/systemd/system/easytier.service"
ET_PORT = 11010
ET_VER = "2.6.4"                          # 取不到最新版时的兜底（跟 sing-box 的 SB_VER 一个道理）
ET_DEFAULT_NET = "bgpeer-mesh"
ET_DEFAULT_IP1 = "10.144.0.1"             # 首节点默认内网 IP
ET_DEFAULT_IP2 = "10.144.0.2"             # 加入节点默认内网 IP
HTTP_UA = "curl/8.5.0"                    # 出网一律用中性 UA，不报家门

# 跟 xy-installer.py / adguard-dns.py 同一套（菜单 21 是子进程跑本文件，主脚本那份管不到这里）
# 【菜单编号统一高亮】（独占一行的「  3. xx」和并排的「1. a   2. b」都算）仓库主人：「像这种被选项数字 0-20 可以做成高亮的吗，包括里面的选项数字，所有的只要是被选项
# 数字都变成高亮……跟下面的 bgpeer 一样的颜色」。不去改几十处菜单代码：屏幕上每一行「行首空格 + 1~2 位数字 + 点 +
# 空格」的，数字那段换成 bgpeer 同色（粗体绿 1;32）。只在直接印到终端时上色 —— 进日志 / 管道 / 测试的照旧是纯文本。
import builtins as _builtins
import sys as _sys
_MENU_NUM_RE = re.compile(r"(^[ \t]*|(?<=  ))(\d{1,2}\.)(?=\s)", re.M)   # 行首的，或并排的（前面两个空格）


_MENU_SEEN = set() # 这一屏印出来的编号；问「请选择」时打的是这里面的，才回头染绿（见 _ask_paint）


def _menu_num_paint(m):
    _MENU_SEEN.add(m.group(2)[:-1])
    return f"{m.group(1)}\033[1;32m{m.group(2)}\033[0m"


def _ask_paint(prompt_shown, v, seen):
    """打完回车之后：打的是菜单上有的编号 → 把这一行重印一遍、数字染成同色；菜单上没有的照旧普通颜色。

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

def ui_warn(text):
    """会让东西没掉的操作：红色粗体单独一行说清后果（CLAUDE.md 第四节）。"""
    print("  " + _hl("1;31", f"⚠ {text}"))

def ui_kv(label, value):
    print(f"  {_hl('36', label + '：')}{value}")


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

def _ask_default(prompt, default):
    """带默认值的一问：回车就用默认的。"""
    v = _ask(f"{prompt}（回车用 {default}）: ")
    return v or default

def _fetch(url, timeout=20):
    """GET 一个 URL，返回 bytes；失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None

# ── 安装 ──────────────────────────────────────────────────────────────────────
def _latest_ver():
    """取 GitHub 最新正式版 tag（去掉前导 v）。取不到就用 ET_VER 兜底。"""
    try:
        b = _fetch("https://api.github.com/repos/EasyTier/EasyTier/releases/latest")
        tag = json.loads(b.decode())["tag_name"]
        return tag.lstrip("v") or ET_VER
    except Exception:
        return ET_VER

def _arch_asset():
    """EasyTier 发布包的架构名：x86_64 / aarch64。别的架构没有预编译包。"""
    m = os.uname().machine
    t = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(m)
    if not t:
        raise SystemExit(f"不支持的 CPU 架构: {m}（EasyTier 预编译包只有 x86_64 / aarch64）")
    return t

def _et_url(ver, arch):
    return (f"https://github.com/EasyTier/EasyTier/releases/download/"
            f"v{ver}/easytier-linux-{arch}-v{ver}.zip")

def _ensure_unzip():
    """解压要用到 unzip；没有就现装（apt / yum / dnf / apk 哪个有算哪个）。"""
    if shutil.which("unzip"):
        return
    for cmd in ("apt-get install -y unzip", "yum install -y unzip",
                "dnf install -y unzip", "apk add unzip"):
        if shutil.which(cmd.split()[0]):
            print(f"  没装 unzip，现装一个：{cmd}")
            sh(cmd)
            break
    if not shutil.which("unzip"):
        raise RuntimeError("没装 unzip，也装不上：手动装一个再来（apt install -y unzip）")

def _dl_zip(url, dest):
    """下载 zip：curl 先试（-f 让 404 直接失败，不把报错页当文件存下来），
       不行再走 urllib。两个都败就报错。

       curl 必须带 check=True：sh() 只有 check=True 才抛错，不然 curl 失败
       （404、断网、没装 curl）也当没事 return，urllib 兜底跑不到，后面
       unzip 一个不存在的文件只会在版本比对处报一句误导的「版本对不上」，
       更糟的是 /tmp 里残留的旧包还会被当成新版解开。落盘还要验一下大小，
       空文件同样算失败、走兜底。"""
    try:
        sh(f"curl -fsSL --connect-timeout 10 --max-time 300 -o {shlex.quote(dest)} {shlex.quote(url)}", check=True)
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return
    except Exception:
        pass
    b = _fetch(url, timeout=300)
    if b is None:
        raise RuntimeError(f"从 GitHub 下载失败：{url}\n    （机器出不去网？换个能通的线路，或先手动把包放到 {dest}）")
    with open(dest, "wb") as f:
        f.write(b)

def _installed():
    return os.path.exists(ET_BIN) and os.path.exists(ET_CLI)

def _running():
    return sh("systemctl is-active easytier") == "active"

def _autostart_on():
    return sh("systemctl is-enabled easytier") == "enabled"

def install():
    """装 easytier-core + easytier-cli 到 /usr/local/bin。已是最新版就跳过。"""
    ver = _latest_ver()
    if _installed():
        cur = sh(f"{ET_BIN} --version", check=False)
        if ver in cur:
            print(f"  ✔ EasyTier 已是最新版 {ver}")
            return
    arch = _arch_asset()
    url = _et_url(ver, arch)
    print(f"  下载 EasyTier {ver}（{arch}）…")
    _ensure_unzip()
    _dl_zip(url, "/tmp/easytier.zip")
    sh("rm -rf /tmp/easytier && mkdir -p /tmp/easytier")
    sh("unzip -o /tmp/easytier.zip -d /tmp/easytier")
    sh(f"install -m755 /tmp/easytier/easytier-linux-{arch}/easytier-core {ET_BIN}")
    sh(f"install -m755 /tmp/easytier/easytier-linux-{arch}/easytier-cli {ET_CLI}")
    out = sh(f"{ET_BIN} --version", check=False)
    if ver not in out:
        raise RuntimeError(f"EasyTier 装完版本对不上：期望 {ver}，实际输出：\n{out[:200]}\n"
                           "    包可能不完整或来源有问题，已中止。重跑一次。")
    print(f"  ✔ EasyTier {ver} 装好了（{ET_BIN}）")

# ── 配置存盘（0600，跟 agh.password 一个做法）─────────────────────────────────
def _conf_load():
    """读回上次配好的：{} / {"network_name":..,"network_secret":..,"ipv4":..,"role":..,"peer":..}。
       role 是 first（首节点，监听）或 join（加入节点，连首节点）。"""
    cfg = {}
    try:
        for line in open(ET_CONF, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k and v:
                cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg

def _write_0600(path, text):
    """带凭据的文件一律 0600（CLAUDE.md 第一节）：os.open 建出来就是 0600，
       再 chmod 一道防着文件已经存在、权限是旧的。"""
    os.makedirs(BGP_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)

def _conf_save(cfg):
    """配置落盘：0600，只有 root 能读（密钥在里面）。"""
    _write_0600(ET_CONF, "".join(f"{k}={v}\n" for k, v in cfg.items()))

def _toml_str(s):
    """TOML 基本字符串里的两个转义：值已经验过不含空白，只剩 " 和 \\ 要处理。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def _toml_text(cfg):
    """给 easytier-core 的 TOML 配置（-c 指过去）。格式照 EasyTier 自己的
       easytier/src/common/config.rs 里的测试用例核过（v2.6.4 实测也认）：
       顶层 ipv4 / listeners，[network_identity] 里放网络名和密钥，
       加入节点再加一段 [[peer]] 指首节点。"""
    lines = [f"ipv4 = {_toml_str(cfg['ipv4'])}"]
    if cfg.get("role") == "first":
        lines.append(f"listeners = [{_toml_str(f'tcp://0.0.0.0:{ET_PORT}')}, "
                     f"{_toml_str(f'udp://0.0.0.0:{ET_PORT}')}]")
    lines += ["", "[network_identity]",
              f"network_name = {_toml_str(cfg['network_name'])}",
              f"network_secret = {_toml_str(cfg['network_secret'])}"]
    if cfg.get("role") == "join":
        peer_uri = "tcp://" + cfg["peer"] + ":" + str(ET_PORT)
        lines += ["", "[[peer]]", "uri = " + _toml_str(peer_uri)]
    return "\n".join(lines) + "\n"

def _random_secret():
    return secrets.token_hex(16)          # 32 位十六进制，跟 openssl rand -hex 16 一个意思

def _valid_name(s):
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", s or ""))

def _valid_secret(s):
    return bool(s) and len(s) >= 8 and len(s) <= 128 and not re.search(r"\s", s)

def _valid_ipv4(s):
    try:
        ipaddress.IPv4Address(s or "")
        return True
    except ValueError:
        return False

def _valid_peer(s):
    """首节点地址：IPv4 或域名。"""
    return _valid_ipv4(s) or bool(re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", s or ""))

def _unit_text():
    """systemd 服务：前台跑（easytier-core 没有后台参数，-d 是 DHCP 不是 daemon）。
       只传 -c 配置文件路径：密钥不进服务文件（0644 人人可读）、也不进进程命令行
       （ps 就能看到）。密钥只在 0600 的 TOML 里，服务自己去读。"""
    return ("[Unit]\nDescription=EasyTier mesh VPN\n"
            "After=network-online.target\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nExecStart=" + ET_BIN + " -c " + ET_TOML + "\n"
            "Restart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=multi-user.target\n")

def _apply_config(cfg):
    """配置落盘 → 写 TOML（0600）→ 写服务 → 起服务 → 验活。起不来当场报，不静默。"""
    _conf_save(cfg)
    _write_0600(ET_TOML, _toml_text(cfg))
    with open(ET_UNIT, "w", encoding="utf-8") as f:
        f.write(_unit_text())
    sh("systemctl daemon-reload")
    sh("systemctl enable easytier")
    sh("systemctl restart easytier")
    if not _running():
        raise RuntimeError("easytier 服务没起来：看一眼日志再配一次\n"
                           "    journalctl -u easytier -n 50 --no-pager")
    print("  ✔ EasyTier 已启动（开机自启已开）")

def status():
    """这一屏：状态 + 配好的值 + 对端表。密钥印出来是故意的：配另一台机器要照着抄。"""
    ui_title("EasyTier 组网 · 状态")
    if not _installed():
        print("  " + _hl("2", "未安装，先跑 1 安装"))
        return
    cfg = _conf_load()
    ui_kv("当前状态", _hl("1;32", "运行中") if _running() else _hl("1;31", "未运行"))
    if cfg:
        ui_kv("网络名", cfg.get("network_name", ""))
        ui_kv("角色", "首节点（监听）" if cfg.get("role") == "first" else "加入节点")
        ui_kv("本机内网 IP", cfg.get("ipv4", ""))
        if cfg.get("role") == "join":
            ui_kv("首节点", f"{cfg.get('peer', '')}:{ET_PORT}")
        else:
            ui_kv("监听端口", f"TCP {ET_PORT}")
        ui_kv("密钥", cfg.get("network_secret", ""))
    else:
        print("  " + _hl("2", "还没配过，跑 2 配置组网"))
        return
    if _running():
        out = sh(f"{ET_CLI} peer", check=False)
        ui_line()
        print(out if out else "  （对端表是空的：另一台还没连进来）")
    ui_tip("首节点要在云防火墙放行 TCP 11010，不然别人连不进来")

def _autostart_toggle():
    if not _installed():
        print("  先跑 1 安装。")
        return
    if _autostart_on():
        sh("systemctl disable easytier")
        print("  ✔ 开机自启已关（现在跑着的这一份不受影响）")
    else:
        sh("systemctl enable easytier")
        print("  ✔ 开机自启已开")

def uninstall(assume_yes=False):
    """整套撤干净：服务 + 二进制 + 配置和密钥。菜单 20 的 5 全部卸载暂不调它（那边的
       1/5～5/5 排好了，动编号要连测试一起改），先在菜单 21 里自己卸；以后要接再接。"""
    if not _installed() and not os.path.exists(ET_CONF) and not os.path.exists(ET_UNIT) \
            and not os.path.exists(ET_TOML):
        print("  EasyTier 没装，不用卸。")
        return
    ui_warn("EasyTier 组网撤掉：服务停掉，二进制、配置和密钥一起删，内网就断了")
    if not assume_yes and _ask("  确认卸载 EasyTier? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  已取消，什么都没动。")
        return
    sh("systemctl disable --now easytier", check=False)
    sh(f"rm -f {ET_UNIT} {ET_BIN} {ET_CLI} {ET_CONF} {ET_TOML}", check=False)
    sh("systemctl daemon-reload", check=False)
    print("  ✔ EasyTier 已卸载")

# ── 配置组网 ──────────────────────────────────────────────────────────────────
def configure():
    """问一圈配好：角色 → 网络名 → 密钥 → 内网 IP →（加入节点）首节点地址 → 确认 → 起服务。
       上次配过的当默认值，回车就沿用；密钥回车是「保持原样」（没配过才是随机生成）。"""
    if not _installed():
        print("  先跑 1 安装 EasyTier。")
        return
    old = _conf_load()
    ui_title("配置组网")
    ui_items([(1, "首节点（监听，等别人来连）", None),
              (2, "加入节点（连首节点）", None),
              (0, "返回", None)])
    c = _ask("选择: ").strip()
    if c not in ("1", "2"):
        return
    role = "first" if c == "1" else "join"
    default_ip = old.get("ipv4") or (ET_DEFAULT_IP1 if role == "first" else ET_DEFAULT_IP2)
    while True:
        name = _ask_default("网络名", old.get("network_name") or ET_DEFAULT_NET)
        if _valid_name(name):
            break
        print("  网络名只能用字母、数字、-、_，开头得是字母或数字，再填一次。")
    if old.get("network_secret"):
        while True:
            sec = _ask("密钥（回车保持原样 / 打 random 重新随机生成）: ")
            if not sec:
                sec = old["network_secret"]
                break
            if sec == "random":
                sec = _random_secret()
                break
            if _valid_secret(sec):
                break
            print("  密钥至少 8 位、不能带空格，再填一次。")
    else:
        sec = _ask("密钥（回车随机生成）: ")
        while sec and not _valid_secret(sec):
            print("  密钥至少 8 位、不能带空格，再填一次。")
            sec = _ask("密钥（回车随机生成）: ")
        if not sec:
            sec = _random_secret()
    while True:
        ip = _ask_default("本机内网 IP", default_ip)
        if _valid_ipv4(ip):
            break
        print("  不是合法的 IPv4 地址，再填一次。")
    cfg = {"network_name": name, "network_secret": sec, "ipv4": ip, "role": role}
    if role == "join":
        while True:
            peer = _ask("首节点公网 IP（端口固定 11010）: ")
            if _valid_peer(peer):
                break
            print("  填首节点那台的公网 IPv4 或域名，再填一次。")
        cfg["peer"] = peer
    ui_line()
    ui_kv("网络名", name)
    ui_kv("角色", "首节点（监听）" if role == "first" else "加入节点")
    ui_kv("本机内网 IP", ip)
    if role == "join":
        ui_kv("首节点", f"{peer}:{ET_PORT}")
    ui_kv("密钥", sec)
    if _ask("确认这样配？[Y/n]: ").strip().lower() in ("n", "no"):
        print("  已取消，什么都没动。")
        return
    _apply_config(cfg)
    if role == "first":
        ui_tip("去云防火墙放行 TCP 11010，不然别人连不进来")
        ui_tip("另一台机器跑 2 选「加入节点」，网络名和密钥照上面抄")
    else:
        ui_tip("去首节点跑 3 状态，看对端在不在表里")

def menu():
    while True:
        ui_title("EasyTier 组网（多台 VPS 虚拟内网）")
        if _installed():
            cur = sh(f"{ET_BIN} --version", check=False)
            st = "已安装 " + (cur.split()[-1] if cur else "") + " · " + (
                _hl("1;32", "运行中") if _running() else _hl("1;31", "未运行"))
        else:
            st = _hl("2", "未安装")
        ui_kv("当前状态", st)
        cfg = _conf_load()
        ui_tip("首节点要在云防火墙放行 TCP 11010，不然别人连不进来")
        ui_line()
        ui_items([(1, "安装 EasyTier", None),
                  (2, "配置组网", (cfg.get("network_name", "") + " · " + cfg.get("ipv4", ""))
                   if cfg else "未配置"),
                  (3, "状态", None),
                  (4, "开机自启", "开" if _autostart_on() else "关"),
                  (5, "卸载 EasyTier", None),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":   install()
        elif c == "2": configure()
        elif c == "3": status()
        elif c == "4": _autostart_toggle()
        elif c == "5": uninstall()
        elif c in ("0", ""):
            return

def main():
    act = sys.argv[1] if len(sys.argv) > 1 else ""
    if act in ("remove", "uninstall"):               # 菜单 20 那边以后要接就调这个；
        # --yes = 已经输过 yes，不再问
        uninstall(assume_yes="--yes" in sys.argv[2:])
    else:
        menu()

if __name__ == "__main__":
    main()

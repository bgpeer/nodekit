#!/usr/bin/env python3
# ============================================================================
# sb-installer —— sing-box + xray 双核心多协议一键安装器（数据驱动）
# ----------------------------------------------------------------------------
# 设计原则（对应“逻辑和加密要做好”）：
#   1. 密钥一律调用核心自带生成器，绝不在 Python 里手搓 x25519 / UUID
#   2. 内核版本跟随 GitHub：sing-box 取 /releases/latest（只认正式版，避免误跳
#                1.14.0-beta 那条并行的测试线），下限 1.12（anytls inbound 是
#                1.12 才加的）；xray 取全部 release 里版本号最大的（含标了预发行
#                的——XTLS 从 v26.6.1 起每个 release 都标预发行，只认 latest 会
#                永远停在 v26.3.27）。xray reality 传输用 raw；
#                拉不到时回落到 SB_VER / XRAY_VER 兜底常量
#   3. 证书三态：reality 借目标站证书(无需域名) / hy2·tuic·anytls 自签 /
#                ws·trojan 给域名走 acme.sh，不给则自签 + 链接带 insecure
#   4. 加协议 = 往 SB / XRAY 表里加一个 builder，返回 (inbound, share_link)
#
# ⚠️ 已按官方当前文档核对字段，但未做运行时测试。上线前每个协议自测一遍，
#    并对照你 VPS 上实际 sing-box/xray 版本确认 schema（版本会漂）。
# 目标系统：debian / ubuntu（apt）。用法见文件末尾 --help。
# ============================================================================
import os, json, base64, calendar, secrets, uuid, argparse, subprocess, unicodedata, urllib.request, urllib.parse, urllib.error, shutil, socket, re, time, random, ipaddress

# 脚本自身版本号：合并进 main 后 CI 会自动把补丁位 +1 并发布 GitHub Release；
# 想升大/中版本（如 2.0.0）就手动改这里再合并，CI 会直接用你写的这个号发布。
SCRIPT_VERSION = "1.1.14"

# 版本：安装时优先问 GitHub（见 latest_gh_release / newest_gh_release）；下面是问不到时的兜底。
# ⚠ sing-box 必须 ≥1.12（anytls inbound 是 1.12 才加的，1.11 会 FATAL: unknown inbound type: anytls）
# ⚠ 兜底值别落后于线上实际版本：install_xray 用「版本号不匹配就重装」判断，兜底比现装的旧会把它降级。
SB_VER   = "1.12.0"
XRAY_VER = "26.7.28"
SB_BIN, XRAY_BIN = "/usr/local/bin/sing-box", "/usr/local/bin/xray"
SB_DIR,  XRAY_DIR = "/etc/sing-box", "/usr/local/etc/xray"
CERT, KEY = "/etc/ssl/sb/self.crt", "/etc/ssl/sb/self.key"     # 自签
ACME_CRT, ACME_KEY = "/etc/ssl/sb/acme.crt", "/etc/ssl/sb/acme.key"  # acme 签发

# 全局状态：域名/邮箱/SNI 由 CLI 注入；端口每次安装在大区间内随机分配
G = {"host": "", "domain": "", "email": "", "sni": "s0.awsstatic.com", "prefix": ""}
HY2_PORTS = "30000-31000"      # hy2 端口跳跃范围默认值；用户可自定义（--hy2-ports / 菜单）
# 端口随机分配区间：每个协议每次重装都从这里随机挑不同的端口，避免连续端口被批量扫描识别
PORT_LO, PORT_HI = 15000, 45000

def hy2_hop_on():
    """hy2 是否启用端口跳跃：G['hy2_ports'] 设为 off/n/no/none 视为关闭（用户不想跳、固定单端口）。"""
    return (G.get("hy2_ports") or "").strip().lower() not in ("off", "n", "no", "none")

def hy2_range():
    """hy2 端口跳跃范围：用户自定义优先，格式须 起-止（如 30000-31000），否则回落默认；
       关闭跳跃时返回 ''（调用方据此走单端口、不做 DNAT、链接不带 mport）。"""
    if not hy2_hop_on():
        return ""
    r = (G.get("hy2_ports") or HY2_PORTS).strip()
    return r if re.match(r"^\d+-\d+$", r) else HY2_PORTS

# 订阅：把节点注入 Mihomo 模板写成【可编辑配置文件】，HTTP 服务托管，产出订阅链接。
# 换订阅链接只换 token（软链名），不动配置；用户可直接编辑 CFG_FILE 改参数。
BGP_DIR      = "/etc/bgpeer"
CFG_FILE     = BGP_DIR + "/mihomo.yaml"      # mihomo 可编辑成品配置
SBOX_FILE    = BGP_DIR + "/singbox.json"     # sing-box 客户端可编辑成品配置
SR_FILE      = BGP_DIR + "/shadowrocket.conf" # Shadowrocket 可编辑成品配置
SUB_DIR      = BGP_DIR + "/sub"              # 托管目录（<token>.yaml/.json/.conf 软链）
SUB_SERVER   = BGP_DIR + "/xy-sub-server.py" # 订阅托管小服务（支持可选 TLS）
HOST_FILE    = BGP_DIR + "/sub.host"         # 记住订阅用的 host（域名或 IP），换 token 时保持不变
STATE_FILE   = BGP_DIR + "/state.json"       # 记住上次安装（域名/前缀/协议等），重装默认保持节点不变
TOKENS_FILE  = BGP_DIR + "/tokens.json"      # 每格式独立订阅 token
LINKS_FILE   = BGP_DIR + "/nodes.links"      # 本机节点链接（供多机聚合拉取的 .links 端点）
PEERS_FILE   = BGP_DIR + "/peers.json"       # 聚合的成员机 .links 地址列表
CUSTPL_FILE  = BGP_DIR + "/custom_tpl.json"  # 每格式自定义模板链接（gist/GitHub）
TPLSRC_FILE  = BGP_DIR + "/tpl_source.json"  # 每格式当前用的是哪套模板："author" / "custom"
BT_STATE     = BGP_DIR + "/bt.json"          # BT/PT 下载屏蔽开关状态
SUBPORT_FILE = BGP_DIR + "/sub.port"         # 订阅托管端口（首装随机挑一次永久沿用，每台机器不同）
_RAW         = "https://raw.githubusercontent.com/bgpeer/nodekit/main/"
TEMPLATE_URL = _RAW + "sub-template.yaml"           # mihomo 模板
SBOX_TPL_URL = _RAW + "subbox-template.json"        # sing-box 模板
SR_TPL_URL   = _RAW + "shadowrocket-template.conf"  # Shadowrocket 模板

# 模板锚点：短名字，_ + 三个大写字母 + _。
#   _NOD_  建节点        _GRP_  建国家策略组        _NAM_  引用国家组名（可带 :<正则>）
# 老名字 __XY_NODES__ / __XY_GROUPS__ / __XY_NAMES__ 仍然认——自定义模板多半还是老写法，
# 拉下来先在 fetch_tpl 里统一换成短名，后面的代码只认短的。
A_NODES, A_GROUPS, A_NAMES = "_NOD_", "_GRP_", "_NAM_"
_ANCHOR_OLD = {"__XY_NODES__": A_NODES, "__XY_GROUPS__": A_GROUPS, "__XY_NAMES__": A_NAMES}

# 订阅三格式：扩展名 → 客户端
SUB_EXTS = {"yaml": "mihomo/clash", "json": "sing-box", "conf": "Shadowrocket"}

# nginx 前置（可选，需域名）：nginx 在 443 终结 TLS + 伪装站 + 按 path 反代 ws 家族；
# webroot 签证书。Vision/anytls/trojan/reality/hy2/tuic 因协议性质仍走各自端口。
NGINX_CONF = "/etc/nginx/conf.d/bgpeer.conf"
WEBROOT    = "/var/www/bgpeer"
NGINX_WS   = []                 # 运行期收集：ws 家族的 {path, port}，供 nginx location 反代
# SNI 分流模式（--sni-split）：nginx stream + ssl_preread 在 443 按 SNI 不解密分流——
# reality 借用域名的 SNI → 本地 reality 端口；你的真域名/默认 → 本地 https(网站+ws)。
# 对外只有 443，reality 真正上 443，且探测回落到借用真站。hy2 仍走自己的 UDP 端口。
NGINX_MAIN        = "/etc/nginx/nginx.conf"
NGINX_MAIN_BAK    = "/etc/nginx/nginx.conf.bgpeer-bak"
NGINX_STREAM_CONF = "/etc/nginx/bgpeer-stream.conf"   # stream(ssl_preread) 分流配置
NGINX_STREAM      = []          # 运行期收集：reality 后端 [{sni, port}]（监听 127.0.0.1）
SNI_HTTPS_PORT    = 8443        # 本地 https(网站+ws)端口，藏在 stream 443 后面

# 屏蔽中国域名/IP 功能拆到独立文件 cn-block.py，方便单独维护；主脚本只负责拉取+调用。
CNBLOCK_FILE   = BGP_DIR + "/cnblock.json"       # cn-block.py 存的状态（这里只读它判断是否已开启）
CN_BLOCK_LOCAL = BGP_DIR + "/cn-block.py"        # 本地缓存的 cn-block.py
CN_BLOCK_URL   = _RAW + "cn-block.py"            # 仓库里的 cn-block.py（每次尽量拉最新）
ADGUARD_LOCAL  = BGP_DIR + "/adguard-dns.py"     # 本地缓存的 adguard-dns.py
ADGUARD_URL    = _RAW + "adguard-dns.py"         # 仓库里的 adguard-dns.py（去广告 DNS·AdGuard Home）
MEDIA_LOCAL    = BGP_DIR + "/media-stack.py"     # 本地缓存的 media-stack.py
MEDIA_URL      = _RAW + "media-stack.py"         # 仓库里的 media-stack.py（自建 Emby·网盘直链）
SELFDNS_FLAG   = BGP_DIR + "/selfdns.on"         # 开关：把本机自建 DNS(AdGuard DoH) 写进订阅 DNS（存在=开）
SELFDNS_CID_FILE = BGP_DIR + "/selfdns.clientid" # AdGuard ClientID（DoH 地址末段；填进「允许的客户端」即可只放行自己）
GHRELAY_OFF    = BGP_DIR + "/ghrelay.off"        # 开关：规则/图标走本机 GitHub 中转（默认开；存在此文件=用户手动关了）
GHRELAY_TOKEN_FILE = BGP_DIR + "/ghrelay.token"  # 本机中转的 token（防别人蹭；在 BGP_DIR 不在 SUB_DIR，不会被下载）
GHDL_RELAYS  = BGP_DIR + "/gh-relays.json"   # 取件用的【自己人】中转前缀（从别的机器菜单 14 复制来的）

# 网络优化脚本已并入本仓库（net-optimize.py，BBR/QoS 等内核调优，依赖工具自动安装）；
# 主脚本只负责拉取+调用，状态检测走同一脚本的 --check。
NETOPT_LOCAL = BGP_DIR + "/net-optimize.py"      # 本地缓存的网络优化脚本
NETOPT_URL   = _RAW + "net-optimize.py"
# VPS 线路检测（三网回程骨干 + IP 纯净度），同样是拉本仓库脚本来跑
VPSCHK_LOCAL = BGP_DIR + "/vps-check.py"
VPSCHK_LAST  = BGP_DIR + "/vps-check.last.json"   # 上次本机检测结果，菜单里回显
VPSCHK_URL   = _RAW + "vps-check.py"
# 网络优化的落盘状态（net-optimize.py 写的，卸载时 rmtree 整个目录）：
#   config           KEY=VAL，含 ADAPTIVE_QOS_MODE（adaptive / fixed_cake）
#   adaptive-qos.conf JSON，含 threshold（激活阈值，单位字节）
NETOPT_CONFIG   = "/etc/net-optimize/config"
NETOPT_ADAPTIVE = "/etc/net-optimize/adaptive-qos.conf"

# CDN 灾备节点：独立 sing-box 实例(VLESS+WS+TLS)，靠 Cloudflare 橙云中转——
# 客户端连的是 CF 的 IP，VPS 真 IP 被墙时仍能续命。与主节点隔离，重装主节点不受影响。
CDN_DIR   = BGP_DIR + "/cdn"
CDN_STATE = BGP_DIR + "/cdn.json"                # 备用节点状态(域名/uuid/path/端口)
CDN_CRT   = CDN_DIR + "/cert.crt"
CDN_KEY   = CDN_DIR + "/cert.key"
CDN_CONF  = CDN_DIR + "/config.json"
CDN_SVC   = "xy-cdn"
CDN_PORTS = [2053, 2083, 2087, 2096, 8443]       # Cloudflare 免费版可代理的 HTTPS 端口

# 优选：客户端不直连域名解析出的那个 CF 任播 IP，改连一个实测更快的 CF 边缘地址。
# 分享链接里【地址位】填优选地址、【sni/host 仍填真域名】——CF 回源认的是 Host 头，
# 所以换地址不用动服务端任何配置。自动测速在本机(VPS)跑，测的是 VPS→CF 边缘这一段。
CDN_PREF_FILE = BGP_DIR + "/cdn-pref.json"       # 优选状态：测速参数 + 候选地址 + 上次结果
CF_IPS_URL    = "https://www.cloudflare.com/ips-v4"   # CF 官方公布的 IPv4 段（纯文本 CIDR）
CF_SPEED_HOST = "speed.cloudflare.com"                # CF 官方测速端点，任何 CF 边缘 IP 都能服务
# 拉不到官方列表时的兜底段（CF 的 IPv4 段多年稳定，少几段不影响优选质量）
CF_IPV4_FALLBACK = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]
# 测速默认参数（可在优选菜单里改，存 CDN_PREF_FILE）。
# ⚠ 下载测速是真的在下数据、走 VPS 流量：一轮最多 n_top × dl_mb MB（默认 6×10=60MB）。
# 默认值按「小流量包 VPS 也扛得住」定的，流量宽裕可自行调大 n_top / dl_mb 提高准确度。
CDN_PREF_DEFAULTS = {
    "n_cand":     500,   # 候选 IP 采样数（从 CF 各段的 /24 里随机取）
    "n_top":      6,     # 延迟筛出前 N 名才进下载测速
    "n_thread":   100,   # 延迟测试并发数
    "timeout":    2.0,   # 单次 TCP 握手超时(秒)
    "dl_mb":      10.0,  # 单个 IP 下载测速的下载量上限(MB)
    "dl_time":    8,     # 单个 IP 下载测速最长耗时(秒)，到点截断按均速算
    "min_mbps":   0.0,   # 候选速度低于它就不采纳；0=不设限
    "port":       0,     # 测试端口；0=沿用第一条 CDN 节点的 CF 端口
    "n_cand_out": 5,     # 最终写进订阅的候选节点数（交给客户端 URLTest 选）
}

NODE_FILE = "/root/xy-nodes.txt"                 # 本机节点分享链接（订阅由它生成）

# 内核（sing-box/xray）每月自动更新：cron 每月北京时间 2 号 04:00 调 `update-cores`。
SELF_LOCAL     = BGP_DIR + "/xy-installer.py"    # 本地脚本副本（cron 调它，不受网络影响）
CORE_CRON_FILE = "/etc/cron.d/bgpeer-coreupdate" # 每月定点更新内核的 cron
CORE_CRON_LOG  = "/var/log/bgpeer-coreupdate.log"
CERT_FIX_LOG   = "/var/log/bgpeer-certfix.log"   # 证书修复日志（转后台跑，SSH 断了也查得到）
NODE_OP_LOG    = "/var/log/bgpeer-nodeop.log"    # 装/加/删协议日志（同上，转后台跑）
NODE_OP_PLAN   = BGP_DIR + "/nodeop.json"        # 交互里选好的方案，交给后台那半程去执行
CERT_META      = BGP_DIR + "/cert.json"          # 证书的事实来源（见 cert_meta）

# ---------------------------------------------------------------------------- 基础工具
def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode:
        # acme.sh 等工具的报错常写到 stdout，两个都带上才看得到真正原因
        msg = (r.stderr or "").strip() or (r.stdout or "").strip()
        raise RuntimeError(f"cmd failed: {cmd}\n{msg}")
    return r.stdout.strip()

def have(binary):
    return shutil.which(binary) is not None

def ensure_deps():
    """安装脚本依赖：acme.sh --standalone 需要 socat；xray 解压需要 unzip。
       Debian/Ubuntu 最小系统默认不带这些，缺了会导致 --issue / 安装直接失败。"""
    # cron 看着跟代理没关系，但它一缺，两件"自动"的事会【从第一天起就没在跑】且悄无声息：
    #   · acme.sh 装自己那条续期任务时发现没有 crontab，只打一行警告就继续
    #   · 本脚本放进 /etc/cron.d 的内核自动更新，没有 cron 守护进程也不会执行
    # 最小化的 Debian / GCE 镜像确实可能不带它，装完一切看着正常，90 天后证书到期全线挂。
    need = [pkg for pkg, binary in
            (("curl", "curl"), ("socat", "socat"), ("unzip", "unzip"),
             ("openssl", "openssl"), ("tar", "tar"), ("cron", "crontab"),
             ("ca-certificates", None))
            if binary is not None and not have(binary)]
    # ca-certificates 无对应可执行文件，装 acme/真证书时保证 TLS 根证书齐全
    if not have("update-ca-certificates"):
        need.append("ca-certificates")
    if not need:
        return
    print("安装依赖:", ", ".join(need))
    sh("apt-get update -y", check=False)
    sh("DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(need))
    if "cron" in need:
        _ensure_cron_running()

def _ensure_cron_running():
    """把 cron 守护进程拉起来并设为开机自启。装了包但没 enable，等于白装。"""
    for unit in ("cron", "crond"):                 # Debian 系叫 cron，RHEL 系叫 crond
        sh(f"systemctl enable --now {unit}", check=False)

def port_free(port):
    """standalone 验证要独占 80 端口，先探测避免 acme 无谓失败。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()

_USED_PORTS = set()            # 本次安装已分配的端口，防止随机撞车

def next_port():
    """每次安装为每个协议随机挑一个可用端口（PORT_LO~PORT_HI），非连续：
       - 避开 hy2 跳跃段（整段 UDP 被 DNAT 给 hy2，别的协议落进去会被劫走）
       - 避开订阅端口、本次已分配端口、系统已被占用的端口
       连续端口(20001,20002…)一扫一整排是明显的代理指纹，随机分散能显著削弱。"""
    rng = hy2_range()                           # 关了跳跃则为 ''，不占用整段
    hop = tuple(map(int, rng.split("-"))) if rng else None
    for _ in range(500):
        p = secrets.randbelow(PORT_HI - PORT_LO + 1) + PORT_LO
        if p in _USED_PORTS or p == sub_port():
            continue
        if hop and hop[0] <= p <= hop[1]:       # hy2 跳跃段，留给 hy2
            continue
        if not port_free(p):                    # 系统层面已被别的进程占用
            continue
        _USED_PORTS.add(p)
        return p
    raise RuntimeError(f"在 {PORT_LO}-{PORT_HI} 内找不到可用端口，请检查端口占用。")

_SUB_PORT = None               # 进程内缓存，避免反复读文件

def sub_port():
    """订阅/聚合链接托管端口：首装从协议同一大区间随机挑一个并永久记住——
       固定端口所有机器一样，会成为按端口批量扫描识别的指纹；随机后与协议端口混在一起。
       老安装（升级上来的）从正在用的 xy-sub.service 里抠出原端口沿用并落盘，
       已发给客户端的订阅链接一个字不变。挑好后写 SUBPORT_FILE，之后换 token、
       更新脚本、更新配置都用同一个端口，订阅 URL 稳定；只有重装换节点时
       才随 token 一起换新端口（见 renew_sub_port）。"""
    global _SUB_PORT
    if _SUB_PORT:
        return _SUB_PORT
    try:
        p = int(open(SUBPORT_FILE).read().strip())
        if 1024 <= p <= 65535:
            _SUB_PORT = p; return p
    except (OSError, ValueError):
        pass
    try:                                        # 老安装：沿用服务里正在用的端口
        m = re.search(r"xy-sub-server\.py (\d+)", open("/etc/systemd/system/xy-sub.service").read())
        if m:
            _SUB_PORT = int(m.group(1)); _save_sub_port(_SUB_PORT); return _SUB_PORT
    except OSError:
        pass
    _SUB_PORT = _pick_sub_port(); _save_sub_port(_SUB_PORT)   # 新安装：随机挑
    return _SUB_PORT

def _pick_sub_port():
    """从协议同一大区间随机挑订阅端口（避开 hy2 跳跃段/已分配/被占端口）。"""
    rng = hy2_range()
    hop = tuple(map(int, rng.split("-"))) if rng else None
    for _ in range(500):
        p = secrets.randbelow(PORT_HI - PORT_LO + 1) + PORT_LO
        if hop and hop[0] <= p <= hop[1]:
            continue
        if p in _USED_PORTS or not port_free(p):
            continue
        return p
    raise RuntimeError(f"在 {PORT_LO}-{PORT_HI} 内找不到可用的订阅端口，请检查端口占用。")

def renew_sub_port():
    """重装节点时随 token 一起换新端口（旧链接反正已失效，顺带换端口零成本；
       平时换 token/更新配置绝不走这里，端口保持稳定）。直接挑新的落盘，
       绕过「从旧服务文件沿用」的老安装兜底。"""
    global _SUB_PORT
    _SUB_PORT = _pick_sub_port(); _save_sub_port(_SUB_PORT)
    return _SUB_PORT

def set_sub_port(p):
    """把订阅端口设为指定值并落盘（用于用户手动指定一个已在防火墙放行的端口）。"""
    global _SUB_PORT
    _SUB_PORT = p; _save_sub_port(p)
    return p

def _save_sub_port(p):
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        open(SUBPORT_FILE, "w").write(str(p))
    except OSError:
        pass                                    # 写不进（非 root 只读操作等）就靠服务文件兜底

def public_ip():
    """本机公网 IP；外网查不到就退回本地网卡；都拿不到返回 ''（不抛异常）。

       别再 .split()[0] 直接取——出网被墙 + hostname -I 是空的时候那是 IndexError，
       崩在调用方一脸茫然，而真实原因只是「这台机现在查不到自己的 IP」。"""
    try:
        ip = urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip()
        if ip:
            return ip
    except Exception:
        pass
    local = (sh("hostname -I", check=False) or "").split()
    return local[0] if local else ""

def new_uuid():   return str(uuid.uuid4())          # RFC4122 v4，两核心都接受
def new_pw(n=16): return secrets.token_urlsafe(n)
def short_id():   return secrets.token_hex(4)       # 8 位 hex，偶数长度 ≤16

def ss2022_key(method):
    n = 16 if "128" in method else 32               # aes-128→16B, 其余→32B
    return base64.b64encode(secrets.token_bytes(n)).decode()

def vmess_link(d):  # v2 分享链接 = "vmess://" + base64(json)
    return "vmess://" + base64.b64encode(json.dumps(d).encode()).decode()

def ss_userinfo(method, password):
    return base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")

# ---------------------------------------------------------------------------- 证书
def ensure_self_signed():
    if os.path.exists(CERT):
        return
    os.makedirs(os.path.dirname(CERT), exist_ok=True)
    sh(f"openssl ecparam -genkey -name prime256v1 -out {KEY}")
    sh(f'openssl req -new -x509 -days 3650 -key {KEY} -out {CERT} -subj "/CN={G["sni"]}"')

def cert_covers(path, domain):
    """现有证书【真的顶得住】这个域名吗（换域名重装时避免复用旧域名的证书）。

       原来是拿 openssl 的整段文本做子串匹配，两头都会错：
         · 漏判成「盖得住」：一张纯 *.a.com 的证书，文本里含有 a.com 这串字符，
           于是被当成盖得住 a.com —— 可泛域名【不顶自己】，装上去裸域名直接握手失败。
         · 也可能反过来：b.a.com 的证书文本里同样含 a.com。
       改成走 SAN 列表 + 正经的覆盖判定（泛域名只顶一级，跟客户端的判定一致）。"""
    if not domain or not os.path.exists(path):
        return False
    return _name_covers(cert_names(path), domain)

# acme.sh 续期后要执行的重载命令。它会被 acme.sh 记进该域名的 conf、每次续期自动跑。
# 为什么非有不可：sing-box / xray / xy-sub 都是【启动时把证书读进内存】的，不会回头看文件。
# 没有这条 hook，acme 把磁盘上的证书换了新的，三个进程还捏着旧的——90 天一到，客户端撞上
# 过期证书，节点和订阅一起挂，而且日志里看不出所以然（就是「连不上」）。
# 有 nginx 顺带 reload；对应服务不存在则静默跳过，末尾 true 保证 hook 本身永不失败。
_ACME_RELOAD_HOOK = (" --reloadcmd '"
                     "systemctl reload nginx 2>/dev/null; "
                     "systemctl restart sing-box 2>/dev/null; "
                     "systemctl restart xray 2>/dev/null; "
                     "systemctl restart xy-sub 2>/dev/null; true'")

_ACME_HOOK_DONE = False          # 一次运行里只补一次：ensure_acme 每个协议都会调一遍

def _acme_install_cert(acme):
    """把证书导出到 ACME_CRT/ACME_KEY，并把 reloadcmd 记进 acme.sh。"""
    sh(f"{acme} --install-cert -d {G['domain']} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{_ACME_RELOAD_HOOK}")

def _ensure_acme_reload_hook():
    """给老安装补上 reloadcmd（证书没到期、走不到重签分支的那条路）。

       失败不抛：证书本来就是好的，补 hook 只是让【下次续期】能自动重启进程，
       补不上顶多回到原样，不该因此把安装流程打断。"""
    global _ACME_HOOK_DONE
    if _ACME_HOOK_DONE or not G.get("domain"):
        return
    _ACME_HOOK_DONE = True
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        return
    sh(f"{acme} --install-cert -d {G['domain']} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{_ACME_RELOAD_HOOK}", check=False)

def _wildcard_wanted():
    """这台机是不是【本来就该有】一张泛域名证书 → (要不要, 为什么)。

       重签之前必须问一次这个。HTTP-01 签不出泛域名（Let's Encrypt 的硬规定，
       泛域名只能走 DNS-01），而下面那条常规路径就是 HTTP-01 —— 所以什么都不问
       直接重签，等于每重装一次就把泛域名【静默】降级成单域名。

       后果不是「证书差一点」，是 Emby 当场被拆：它对外是 <子域>.<域名>，
       只有 *.<域名> 顶得住。合并好的共用关系会退回两张证书、两条续期链，
       而且全程不吭一声，等你发现时已经是几周之后。"""
    try:
        if cert_meta().get("wildcard"):
            return True, "上次装的就是泛域名证书"
    except Exception:
        pass
    _d = G.get("domain") or ""
    if _d and _name_covers(cert_names(), "x." + _d):
        return True, "磁盘上现有的这张就是泛域名"
    try:
        ed = _emby_cert()[0]
        if ed and _emby_shared(ed):
            return True, f"Emby（{ed}）正跟节点共用这张证书，它服务的是子域名"
    except Exception:
        pass
    return False, ""

def ensure_acme():
    """给了 --domain 就用 acme.sh standalone 签真证书；否则回落自签。"""
    if not G["domain"]:
        ensure_self_signed()
        return CERT, KEY, True                      # (crt, key, insecure)
    # 只有『证书缺失』或『证书不是当前域名的』才重新签——换域名重装必须重签，
    # 否则会拿着旧域名证书导致 8 个走域名证书的节点全部握手失败。
    if not cert_covers(ACME_CRT, G["domain"]):
        # standalone 用 socat 起临时 HTTP 服务占 80 端口做验证，缺 socat 必挂
        if not have("socat"):
            ensure_deps()
        acme = os.path.expanduser("~/.acme.sh/acme.sh")
        if not os.path.exists(acme):
            sh("curl -s https://get.acme.sh | sh -s email=" + (G["email"] or "a@a.com"))
        if not os.path.exists(acme):
            raise RuntimeError("acme.sh 安装失败，检查网络/curl 是否可访问 get.acme.sh")
        sh(f"{acme} --register-account -m {G['email'] or 'a@a.com'} "
           f"--server letsencrypt", check=False)
        sh(f"{acme} --set-default-ca --server letsencrypt", check=False)
        Y_, R_, N_ = "\033[1;33m", "\033[1;31m", "\033[0m"
        wild, why = _wildcard_wanted()
        if wild and _acme_has_cf():
            # 泛域名只能走 DNS-01。acme.sh 里存着 CF 凭据，那就照着原样签回来，
            # 而不是降级——Emby 的共用关系靠软链指向这个文件，泛域名保住就不会断。
            print(f"  这台机该用泛域名证书（{why}），改走 DNS-01 签 "
                  f"{G['domain']} + *.{G['domain']}")
            issue = (f"{acme} --issue --dns dns_cf -d {G['domain']} -d '*.{G['domain']}' "
                     f"--keylength ec-256 --server letsencrypt")
        elif wild:
            # 只能降级了，但绝不静默：说清楚丢了什么、会坏什么、装完去哪儿补。
            print(f"{Y_}  ⚠ 这台机该用泛域名证书（{why}），{N_}")
            print(f"{Y_}    但 acme.sh 里没有 Cloudflare 凭据，泛域名签不出来"
                  f"（HTTP-01 只能签单域名，这是 Let's Encrypt 的硬规定）。{N_}")
            print(f"{R_}    这次会签成单域名，后果：Emby 对外是 <子域>.{G['domain']}，"
                  f"单域名盖不住，共用关系会被拆成两张证书、两条续期链。{N_}")
            print(f"{Y_}    装完回面板『15 证书管理 → 1 安装证书』用 DNS-01 重签一张"
                  f"泛域名的，再『4 Emby 证书』合并回去。{N_}")
            wild = False
        if not wild and G.get("nginx"):
            issue = f"{acme} --issue -d {G['domain']} --webroot {WEBROOT} --keylength ec-256"
        elif not wild:
            if not port_free(80):
                raise RuntimeError(
                    "80 端口被占用，acme.sh --standalone 无法验证。"
                    "先停掉占用 80 的服务(nginx/caddy 等)，或改用自签(回车跳过域名)。")
            issue = f"{acme} --issue -d {G['domain']} --standalone --keylength ec-256"
        # acme.sh 在证书仍有效时会以退出码 2 “跳过续期”，这不是错误；
        # 只要最终能 install-cert 导出证书就算成功，否则才把真实报错抛出来。
        r = subprocess.run(issue, shell=True, text=True, capture_output=True)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        skipped = any(s in out for s in
                      ("Domains not changed", "Skipping", "Next renewal time", "Cert success"))
        if r.returncode and not skipped:
            raise RuntimeError("acme 签发失败(检查域名解析是否指向本机、80 端口是否可达):\n" + out)
        os.makedirs(os.path.dirname(ACME_CRT), exist_ok=True)
        _acme_install_cert(acme)
        # 记下这次签的是什么，下次重装才知道该不该保泛域名
        try:
            save_cert_meta(domain=G["domain"], wildcard=bool(wild),
                           mode="dns-cf" if wild else
                                ("webroot" if G.get("nginx") else "standalone"))
        except Exception:
            pass
    else:
        # 证书已经在、不用重签，但 reloadcmd 可能【压根没装过】——
        # 这条 hook 是后来才加进脚本的，在它之前装的机器走不到上面那一步，
        # acme.sh 续期时什么都不重启。补一次，幂等。
        _ensure_acme_reload_hook()
    return ACME_CRT, ACME_KEY, False

# ---------------------------------------------------------------------------- nginx 前置
def clean_stale_nginx():
    """删掉引用了已不存在证书/目录（如 mack-a 残留 /etc/v2ray-agent）的 nginx 配置文件，
       否则别人的坏块会让 nginx -t 全局失败、我们的 stub 也写不进去。不动 nginx.conf 主文件。"""
    for d in ("/etc/nginx/conf.d", "/etc/nginx/sites-enabled", "/etc/nginx/sites-available"):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            fp = os.path.join(d, f)
            if os.path.abspath(fp) == os.path.abspath(NGINX_CONF):
                continue
            try:
                txt = open(fp).read()
            except (OSError, UnicodeDecodeError):
                continue
            if "/etc/v2ray-agent" in txt:                # mack-a 残留、引用已删证书
                print(f"移除残留 nginx 配置(引用已删证书): {fp}")
                sh(f"rm -f {fp}", check=False)

def ensure_nginx():
    if not have("nginx"):
        sh("apt-get update -y", check=False)
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y nginx", check=False)
    clean_stale_nginx()                                  # 先清掉别人残留的坏块，保证 nginx -t 能过
    os.makedirs(WEBROOT, exist_ok=True)
    if not os.path.exists(WEBROOT + "/index.html"):     # 伪装站首页
        # 别用 Apache/nginx 默认页(一眼假)；放一个像样的通用静态站。
        # 用户可直接覆盖 WEBROOT/index.html 换成自己的真站内容以增强伪装。
        host = G.get("domain") or "this site"
        open(WEBROOT + "/index.html", "w").write(
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"<title>{host}</title>\n"
            "<style>\n"
            "*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"
            "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2933;background:#f5f7fa;"
            "display:flex;min-height:100vh;align-items:center;justify-content:center}\n"
            ".card{max-width:560px;margin:24px;padding:48px 40px;background:#fff;border-radius:14px;"
            "box-shadow:0 8px 30px rgba(0,0,0,.06);text-align:center}\n"
            "h1{margin:0 0 12px;font-size:1.6rem;font-weight:600}\n"
            "p{margin:8px 0;line-height:1.6;color:#616e7c}\n"
            ".dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#3ba55d;"
            "margin-right:8px;vertical-align:middle}\n"
            "footer{margin-top:28px;font-size:.82rem;color:#9aa5b1}\n"
            "</style>\n</head>\n<body>\n<div class=\"card\">\n"
            "<h1><span class=\"dot\"></span>We'll be back soon</h1>\n"
            "<p>This site is currently undergoing scheduled maintenance.</p>\n"
            "<p>Thank you for your patience — please check back a little later.</p>\n"
            "<footer>&copy; 2026 &middot; All rights reserved.</footer>\n"
            "</div>\n</body>\n</html>\n")

def nginx_reload():
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:
        raise RuntimeError("nginx 配置校验未通过:\n" + (chk.stderr or chk.stdout).strip())
    sh("systemctl enable --now nginx", check=False)
    sh("systemctl reload nginx", check=False)

def write_nginx_acme_stub():
    """先放一个 80 server 块，供 acme webroot 验证用（此时还没证书，不写 443）。"""
    conf = (f"server {{\n  listen 80;\n  listen [::]:80;\n  server_name {G['domain']};\n"
            f"  location /.well-known/acme-challenge/ {{ root {WEBROOT}; }}\n"
            f"  location / {{ return 404; }}\n}}\n")
    open(NGINX_CONF, "w").write(conf)
    nginx_reload()

def _nginx_ws_locations():
    """ws 家族的 location 反代块（供 443 或本地 https server 复用）。"""
    locs = ""
    for w in NGINX_WS:
        locs += (f"  location = {w['path']} {{\n"
                 f"    proxy_pass http://127.0.0.1:{w['port']};\n"
                 f"    proxy_http_version 1.1;\n"
                 f"    proxy_set_header Upgrade $http_upgrade;\n"
                 f"    proxy_set_header Connection \"upgrade\";\n"
                 f"    proxy_set_header Host $host;\n"
                 f"    proxy_set_header X-Real-IP $remote_addr;\n  }}\n")
    return locs

def _nginx_80_server():
    """:80——acme webroot 验证 + 跳转到 https。"""
    return (f"server {{\n  listen 80;\n  listen [::]:80;\n  server_name {G['domain']};\n"
            f"  location /.well-known/acme-challenge/ {{ root {WEBROOT}; }}\n"
            f"  location / {{ return 301 https://$host$request_uri; }}\n}}\n")

def _nginx_https_server(listen):
    """https 伪装站 + ws 反代；listen 为监听指令（公网 443 或本地 127.0.0.1:8443）。"""
    return (f"server {{\n{listen}"
            f"  server_name {G['domain']};\n"
            f"  ssl_certificate {ACME_CRT};\n  ssl_certificate_key {ACME_KEY};\n"
            f"  ssl_protocols TLSv1.2 TLSv1.3;\n"
            f"{_nginx_ws_locations()}"
            f"  location / {{ root {WEBROOT}; index index.html; }}\n}}\n")

def write_nginx_conf():
    """签好证书、收集完 ws 家族后，写完整 conf：80 跳转 + 443 伪装站 + ws 按 path 反代。"""
    listen = "  listen 443 ssl http2;\n  listen [::]:443 ssl http2;\n"
    open(NGINX_CONF, "w").write(_nginx_80_server() + _nginx_https_server(listen))
    nginx_reload()

# ---- SNI 分流（--sni-split）：nginx stream + ssl_preread，reality 真正上 443 ----
def ensure_stream_module():
    """确保 nginx 的 stream + ssl_preread 模块可用（Ubuntu/Debian 在 libnginx-mod-stream）。
       是否真能用最终由 preflight 的 nginx -t 判定，这里只尽量把模块装上。"""
    v = subprocess.run("nginx -V", shell=True, text=True, capture_output=True)
    if "with-stream" in (v.stdout + v.stderr):          # 内建 stream（nginx -V 输出在 stderr）
        return True
    if subprocess.run("dpkg -s libnginx-mod-stream", shell=True,
                      capture_output=True).returncode == 0:
        return True                                     # 已装动态模块
    sh("apt-get update -y", check=False)
    sh("DEBIAN_FRONTEND=noninteractive apt-get install -y libnginx-mod-stream", check=False)
    return True

def _nginxconf_has_stream():
    """nginx.conf 顶层是否已有 stream 块（有的话不敢贸然再加，交给用户/我们的标记块）。"""
    try:
        txt = open(NGINX_MAIN).read()
    except OSError:
        return False
    return "BGPEER-STREAM-BEGIN" in txt or re.search(r"(?m)^\s*stream\s*\{", txt) is not None

def _nginxconf_add_stream():
    """在 nginx.conf 顶层追加我们的 stream include（带标记，便于卸载时移除）；幂等。"""
    txt = open(NGINX_MAIN).read()
    if "BGPEER-STREAM-BEGIN" in txt:
        return
    block = ("\n# BGPEER-STREAM-BEGIN\n"
             f"stream {{\n    include {NGINX_STREAM_CONF};\n}}\n"
             "# BGPEER-STREAM-END\n")
    open(NGINX_MAIN, "a").write(block)

def _nginxconf_remove_stream():
    """卸载时移除我们加进 nginx.conf 的 stream 标记块，不动用户其它内容。"""
    try:
        txt = open(NGINX_MAIN).read()
    except OSError:
        return
    new = re.sub(r"\n?# BGPEER-STREAM-BEGIN\n.*?# BGPEER-STREAM-END\n",
                 "\n", txt, flags=re.S)
    if new != txt:
        open(NGINX_MAIN, "w").write(new)

def _stream_conf_text():
    """stream 配置：按 SNI 不解密分流。reality 借用域名 → 本地 reality 端口；
       真域名/默认 → 本地 https(网站+ws)。"""
    m = "map $ssl_preread_server_name $bgpeer_upstream {\n"
    for b in NGINX_STREAM:                              # reality 后端（借用 SNI → 本地端口）
        m += f"    {b['sni']}  127.0.0.1:{b['port']};\n"
    m += f"    {G['domain']}  127.0.0.1:{SNI_HTTPS_PORT};\n"
    m += f"    default  127.0.0.1:{SNI_HTTPS_PORT};\n}}\n"
    srv = ("server {\n  listen 443 reuseport;\n  listen [::]:443 reuseport;\n"
           "  ssl_preread on;\n  proxy_pass $bgpeer_upstream;\n}\n")
    return m + srv

def sni_split_preflight():
    """真正改动前先探测：装 stream 模块，用一份『结构等价』的测试 stream 配置跑 nginx -t。
       通过才敢走 sni-split；不通过返回 False，让调用方退回 reality-443 直连模式。
       全过程可回滚，绝不把用户能用的 nginx 改坏。"""
    if not have("nginx"):
        sh("apt-get update -y", check=False)
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y nginx", check=False)
    if not have("nginx"):
        print("  sni-split 预检：nginx 装不上，退回 reality-443 直连。"); return False
    ensure_stream_module()
    try:
        txt = open(NGINX_MAIN).read()
    except OSError:
        print("  sni-split 预检：读不到 nginx.conf，退回 reality-443 直连。"); return False
    has_ours = "BGPEER-STREAM-BEGIN" in txt
    if not has_ours and re.search(r"(?m)^\s*stream\s*\{", txt):   # 用户自己已有 stream 块
        print("  sni-split 预检：nginx.conf 已有你自己的 stream 块，不便共存，退回 reality-443 直连。")
        return False
    if not os.path.exists(NGINX_MAIN_BAK):               # 首次备份 nginx.conf，供回滚
        sh(f"cp -a {NGINX_MAIN} {NGINX_MAIN_BAK}", check=False)
    test_conf = os.path.join(os.path.dirname(NGINX_MAIN), "bgpeer-stream-test.conf")
    open(test_conf, "w").write(
        "server {\n  listen 65533 reuseport;\n  ssl_preread on;\n"
        "  proxy_pass 127.0.0.1:65534;\n}\n")
    added = not has_ours                                 # 已有我们的正式块就不重复加测试块
    try:
        if added:
            open(NGINX_MAIN, "a").write(
                f"\n# BGPEER-STREAM-TEST\nstream {{\n    include {test_conf};\n}}\n")
        r = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
        ok = r.returncode == 0
    finally:                                             # 无论如何撤掉测试块和测试文件
        if added:
            t = open(NGINX_MAIN).read()
            t = re.sub(r"\n?# BGPEER-STREAM-TEST\nstream \{\n.*?\n\}\n", "\n", t, flags=re.S)
            open(NGINX_MAIN, "w").write(t)
        sh(f"rm -f {test_conf}", check=False)
    if not ok:
        print("  sni-split 预检：nginx stream/ssl_preread 不可用，退回 reality-443 直连。\n   " +
              (r.stderr or r.stdout).strip().replace("\n", "\n   "))
    return ok

def write_nginx_sni_split():
    """写 sni-split 的 http(本地 https 网站+ws) + stream(443 SNI 分流)配置并生效；
       nginx -t 不过则整体回滚（还原 nginx.conf、删 stream 配置），返回 False。"""
    listen = f"  listen 127.0.0.1:{SNI_HTTPS_PORT} ssl http2;\n"
    open(NGINX_CONF, "w").write(_nginx_80_server() + _nginx_https_server(listen))
    open(NGINX_STREAM_CONF, "w").write(_stream_conf_text())
    _nginxconf_add_stream()
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:                                  # 回滚，绝不留下坏配置
        _nginxconf_remove_stream()
        sh(f"rm -f {NGINX_STREAM_CONF}", check=False)
        if os.path.exists(NGINX_MAIN_BAK):
            sh(f"cp -a {NGINX_MAIN_BAK} {NGINX_MAIN}", check=False)
        sh("nginx -t && systemctl reload nginx", check=False)
        print("  sni-split 写入后校验失败，已回滚：\n   " +
              (chk.stderr or chk.stdout).strip().replace("\n", "\n   "))
        return False
    sh("systemctl enable --now nginx", check=False)
    sh("systemctl reload nginx", check=False)
    return True

def free_443_for_reality():
    """reality 要独占 443/TCP：清掉本脚本的 nginx 前置块并 reload，让 nginx 释放 443；
       若 443 仍被别的服务占着，明确警告（否则 sing-box 会绑不上 443、服务起不来）。"""
    if os.path.exists(NGINX_CONF):
        sh(f"rm -f {NGINX_CONF}", check=False)
        sh("nginx -t && systemctl reload nginx", check=False)   # 无 443 server 后 nginx 会释放 443
    if not port_free(443):
        time.sleep(1)
    if not port_free(443):
        Y, N = "\033[1;33m", "\033[0m"
        holder = sh("ss -tlpnH | grep ':443' || true", check=False)
        print(f"{Y}  ⚠ 443 仍被占用，reality 可能绑不上、服务起不来。占用者：\n    {holder}\n"
              f"    先停掉占 443 的服务（nginx/caddy 等）再重装。{N}")

def tls_host():                                     # ws/trojan 的 SNI/Host
    return G["domain"] or G["sni"]

def check_domain_or_die():
    """有域名就先校验它解析到本机公网 IP；不匹配/80 被占 → 爆红并停止，
       且在『任何破坏性动作(接管卸载)之前』执行——绝不在错误域名下删旧装新。
       无域名则整段跳过（自签+IP 安装，无此校验）。"""
    if not G["domain"]:
        return
    R, N = "\033[1;31m", "\033[0m"                   # 红色加粗
    dom = G["domain"]
    try:
        resolved = sorted({info[4][0] for info in socket.getaddrinfo(dom, None)})
    except Exception:
        raise SystemExit(f"{R}\n❌ 域名 {dom} 解析不到（DNS 查询失败）。检查域名拼写/解析是否生效，"
                         f"或重跑时域名留空用『自签证书+IP』安装。{N}")
    myip = public_ip()
    if myip not in resolved:
        raise SystemExit(
            f"{R}\n❌ 域名与服务器 IP 不匹配，无法签发证书，已停止（未改动本机任何配置）：\n"
            f"   域名 {dom} 解析到 → {', '.join(resolved)}\n"
            f"   本机公网 IP    → {myip}\n"
            f"   请把 {dom} 的 A 记录改指向 {myip}，等 DNS 生效后再装；\n"
            f"   或重跑时域名留空，用『自签证书 + IP』安装（无需域名，最省事）。{N}")
    # nginx 前置走 webroot、复用 nginx 的 80，不要求 80 空闲；standalone 才要求
    if not G.get("nginx") and not port_free(80):
        raise SystemExit(f"{R}\n❌ 80 端口被占用，acme standalone 无法验证。先停掉占用 80 的服务"
                         f"（nginx/caddy 等）再装，或域名留空用自签，或用 nginx 前置模式。{N}")

# reality 借用目标池：都是大厂/技术站，实测 TLS1.3+h2+X25519、国内可达、不套乱 CDN。
# 安装时默认从这里随机挑一个（避免所有人都按回车挤在同一个 SNI 上被针对）。
REALITY_SNI_POOL = [
    "www.cisco.com", "www.oracle.com", "www.ibm.com", "www.vmware.com",
    "www.python.org", "www.mysql.com", "www.mongodb.com", "redis.io", "www.swift.com",
    "www.intel.com", "www.amd.com", "www.qualcomm.com", "www.dell.com",
    "www.samsung.com", "academy.nvidia.com",
    "swcdn.apple.com", "updates.cdn-apple.com", "cdn-dynmedia-1.microsoft.com",
    "www.bing.com", "www.tesla.com", "s0.awsstatic.com",
]
SNI_SUGGESTIONS = " / ".join(REALITY_SNI_POOL[:6]) + " 等"

def _reality_sni_ok(sni):
    """探测 reality 借用目标站是否支持 TLS1.3 + HTTP/2。返回 (ok, 说明)。
       reality 要求目标必须 TLS1.3，且最好支持 h2（否则握手特征与真站不符、易被识别）。
       探测本身失败(网络不通等)按『未知』放行，不阻断安装。"""
    if not re.match(r"^[A-Za-z0-9.\-]+$", sni or ""):
        return True, "非常规主机名，跳过校验"
    if not have("openssl"):
        return True, "无 openssl，跳过校验"
    try:
        r = subprocess.run(
            ["openssl", "s_client", "-connect", f"{sni}:443", "-servername", sni,
             "-alpn", "h2", "-tls1_3"],
            input="", text=True, capture_output=True, timeout=15)
        out = r.stdout + r.stderr
    except Exception as e:
        return True, f"探测失败，跳过校验（{e}）"
    if "CONNECTED" not in out:                        # TCP 都没连上：DNS 挂/不可达/被墙
        return False, "从本机连不上该目标:443（reality 握手也需能到达它），换一个可达的大站"
    tls13 = "TLSv1.3" in out and "Cipher is" in out
    h2 = "ALPN protocol: h2" in out
    if tls13 and h2:
        return True, "TLS1.3 + h2 ✓"
    if not tls13:
        return False, "目标不支持 TLS1.3（reality 强制要求），必须换"
    return False, "目标不支持 HTTP/2(h2)，reality 握手特征易露，建议换"

def _sni_precheck_one():
    """探一次当前 G["sni"] 够不够格给 reality 借用；不合格只警告不阻断。"""
    ok, detail = _reality_sni_ok(G["sni"])
    if ok:
        print(f"  reality 借用目标 {G['sni']}: {detail}")
    else:
        Y, N = "\033[1;33m", "\033[0m"                # 黄色警告（不阻断）
        print(f"{Y}  ⚠ reality 借用目标 {G['sni']} 可能不理想：{detail}\n"
              f"    建议换成支持 TLS1.3+h2 的大站：{SNI_SUGGESTIONS}\n"
              f"    （可 --sni 指定或在交互菜单里改；现按你填的继续装）{N}")

def precheck_sni(sb_names, xr_names):
    """选了 reality 类协议时，装前探一次借用目标。选没选据注册表判断，不靠名字前缀。"""
    if proto_flag("reality", sb_names, xr_names):
        _sni_precheck_one()

def warn_selfsigned(sb_names, xr_names):
    """无域名时，依赖证书的 TLS 协议只能自签+insecure，是伪装/加密弱点。
       给出明确引导：优先 reality，或补一个域名走真证书。hy2/tuic 自签是常规，不在此列。"""
    if G["domain"]:
        return
    cert_tls = proto_pick("cert", sb_names, xr_names)
    if not cert_tls:
        return
    Y, N = "\033[1;33m", "\033[0m"
    print(f"{Y}  ⚠ 无域名：{', '.join(cert_tls)} 将用自签证书 + 客户端 allowInsecure。\n"
          f"    这些协议内容仍加密(有各自密码/UUID)，但失去证书校验、且自签是明显特征。\n"
          f"    更稳的伪装：优先选 reality-* 系列（借真站证书，无需域名、无 insecure），\n"
          f"    或补一个域名走 acme 真证书。hy2/tuic 用自签属常规、无需担心。{N}")

# ---------------------------------------------------------------------------- 核心安装
def arch_tag():
    m = os.uname().machine
    t = {"x86_64": "amd64", "aarch64": "arm64"}.get(m)
    if not t:
        raise SystemExit(f"不支持的 CPU 架构: {m}（sing-box/xray 预编译包仅支持 x86_64 / aarch64）")
    return t

# ============================================================================
# 从 GitHub 取件：三个域、各自独立的可达性
#
#   raw.githubusercontent.com          脚本 / 订阅模板 / 规则文件
#   api.github.com                     查最新版本号
#   github.com/<o>/<r>/releases/…      sing-box、xray 的二进制包
#
# 关键点：这三个域**通不通是分开的**。不少线路 raw 通而 releases 不通（IPv6-only、
# 或 ISP 只封了一个），所以不能拿「raw 能拉到」当「二进制也能拉到」。原来这里二进制是
# 一条裸 curl，遇上就直接装不了，报错还只是 curl 的退出码，查不出所以然。
GH_MIRRORS = ("https://gh-proxy.com/", "https://ghfast.top/",
              "https://ghproxy.net/", "https://hub.gitmirror.com/")
_GH_RE = re.compile(r"https://(raw\.githubusercontent\.com|api\.github\.com|github\.com)/")

def _bad_archive(path):
    """收到的到底是不是个压缩包：按扩展名对魔数。不是就返回一句人话，让上层换下一个源。

       为什么需要：反代自己也会出错（限流页、"镜像维护中"的 HTML），那种响应是 200，
       curl -f 拦不住，文件也非空。不看一眼的话，错误会拖到 tar/unzip 才爆出来，
       而且那时已经不会再换源重试了。"""
    try:
        if os.path.getsize(path) == 0:
            return "拿到的是空文件"
        head = open(path, "rb").read(4)
    except Exception as e:
        return f"读不出下载到的文件：{e}"
    want = (b"\x1f\x8b", "gzip") if path.endswith((".tgz", ".tar.gz")) else \
           (b"PK\x03\x04", "zip") if path.endswith(".zip") else (None, "")
    if want[0] and not head.startswith(want[0]):
        return (f"拿到的不是 {want[1]} 包（开头是 {head[:4]!r}），"
                f"多半是反代返回的错误页 / 限流页")
    return ""

def own_relays():
    """自己人的 GitHub 中转前缀列表（从别的机器菜单 14 抄来的，形如
       https://域名:端口/<token>/gh/）。取件时排在公共反代前面。

       为什么不是用【本机自己】那个中转：它就跑在这台机器上，出网走的是同一条路。
       本机连不上 GitHub，本机的中转照样连不上，多绕一跳纯属白费。有用的是**别的
       机器**上的中转——那台能通 GitHub，就能替这台把东西转过来。"""
    try:
        v = json.load(open(GHDL_RELAYS))
    except Exception:
        return []
    out = []
    for x in v if isinstance(v, list) else []:
        x = str(x).strip()
        if x.startswith(("http://", "https://")):
            out.append(x if x.endswith("/") else x + "/")
    return out

def save_own_relays(lst):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(lst, open(GHDL_RELAYS, "w"), ensure_ascii=False, indent=2)

def _relay_kind(u, url):
    """这条候选是谁家的：直连 / jsDelivr / 自己的中转 / 公共反代。只用来把话说清楚。"""
    if u == url:
        return "直连"
    if any(u.startswith(p) for p in own_relays()):
        return "自己的中转"
    if "jsdelivr" in u:
        return "jsDelivr"
    return "公共反代（第三方）"

def _dl_gh(url, dest):
    """下载 GitHub 上的文件（内核二进制包）。直连先试，不行才逐个试公共反代。
       成功返回真正用上的 URL；全都不通则抛出，错误里逐条列出试过谁、败在哪。

       为什么加 -f：原来是 `curl -Lo`，没有 -f 的话 404 页面会被**当成文件存下来**，
       接着 tar/unzip 报一个跟真实原因八竿子打不着的错。-f 让 curl 在 HTTP 错误时直接失败。

       顺序：直连 → jsDelivr(仅 raw) → **自己人的中转**（own_relays，别的机器上那个）
       → 公共反代。自己的排在第三方前面，配了就基本轮不到第三方。

       完整性说明：公共反代是第三方，理论上能掉包。做不到端到端签名校验（上游没发布
       校验和），能做到的三条都做了——① 直连和自己人的排在前面，第三方只是最后兜底；
       ② 装完必校版本号（见 _verify_core，截断/换包都会露馅）；③ 用了谁明着打印出来。"""
    errs = []
    for i, u in enumerate(_mirrors(url)):
        try:
            sh(f"curl -fsSL --connect-timeout 10 --max-time 300 -o '{dest}' '{u}'")
            bad = _bad_archive(dest)
            if bad:
                errs.append(f"{u[:72]}…\n        {bad}")
                continue
            if i:
                print(f"  ⓘ GitHub 直连不通，本次走 {_relay_kind(u, url)}："
                      f"{u[:len(u) - len(url)] or u}")
            return u
        except Exception as e:
            errs.append(f"{u[:72]}…\n        {str(e).strip().splitlines()[-1][:90]}")
    raise RuntimeError("从 GitHub 下载失败，直连和所有中转都不通：\n      "
                       + "\n      ".join(errs)
                       + "\n    （机器出不去网？换个能通的线路，或先手动把包放到 " + dest + "）")

def _verify_core(binpath, ver, what):
    """装完校版本：`<bin> version` 里必须能看到期望的版本号。
       走过反代时这是唯一能自查的一道——包被截断、被换成别的版本都会在这儿露馅。"""
    out = sh(f"{binpath} version", check=False)
    if ver not in out:
        raise RuntimeError(f"{what} 装完版本对不上：期望 {ver}，实际输出：\n{out[:200]}\n"
                           f"    包可能不完整或来源有问题，已中止。重跑一次；"
                           f"持续如此说明这条线路拿到的包不可信。")

def latest_gh_release(repo, fallback):
    """取 GitHub 最新正式版 tag（去掉前导 v）。取不到就用 fallback。
       和 mack-a 一样跟随 latest —— 否则钉死旧版会缺协议（如 anytls 需 1.12）。

       走 fetch_url 而不是裸 urlopen：这样 api 域被墙时也能经反代问到版本号。
       原来问不到就悄悄退回代码里钉死的 SB_VER，内核从此停在 1.12.0 再也推不动。"""
    try:
        tag = json.loads(fetch_url(f"https://api.github.com/repos/{repo}/releases/latest"))["tag_name"]
        return tag.lstrip("v") or fallback
    except Exception:
        return fallback

def _ver_key(v):
    """版本排序键，按 semver 惯例：同一版本号下带预发行后缀的小于正式版。
         "26.7.28"      → ((26,7,28), 1, "")
         "1.14.0-beta.7"→ ((1,14,0),  0, "beta.7")   ← 小于 1.14.0 正式版"""
    core, _, pre = str(v).partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in core.split("."))
    return (nums, 0 if pre else 1, pre)

def newest_gh_release(repo, fallback):
    """取版本号最大的 release tag（**把标了预发行的也算进来**）。去掉前导 v，取不到用 fallback。

       为什么需要它：GitHub 的 /releases/latest 按定义只返回「非预发行」的那个，而 XTLS 从
       v26.6.1 起把每个 Xray release 都标成了预发行(prerelease: true)——于是 /releases/latest
       永远停在 v26.3.27，比实际最新落后 5 个版本、约四个月，且每月的 cron 自动更新每次都拿到
       同一个答案，永远推不动。注意预发行是 release 上的独立标志位，跟 tag 叫什么无关：Xray 的
       tag 一律是 v26.7.28 这种纯版本号，光看 tag 名根本分辨不出来。

       为什么只给 Xray 用、sing-box 仍走 latest_gh_release：Xray 没有并行的测试分支，所有
       release 是一条线性递增的序列(…26.6.27 → 26.7.11 → 26.7.28)，只是习惯性都打预发行标记，
       所以「取最大」就等于「取真正的最新版」。sing-box 相反——它 1.13 正式线和 1.14.0-beta 线
       **并行维护**，而 1.14.0-beta.7 的版本号是大于 1.13.16 的，取最大会把人送上 beta 线，
       跨小版本换配置 schema，节点可能直接起不来。"""
    try:
        rels = json.loads(fetch_url(f"https://api.github.com/repos/{repo}/releases?per_page=20"))
        tags = [str(r["tag_name"]).lstrip("v") for r in rels
                if isinstance(r, dict) and r.get("tag_name") and not r.get("draft")]
        return max(tags, key=_ver_key) if tags else fallback
    except Exception:
        return fallback

def install_singbox():
    ver = latest_gh_release("SagerNet/sing-box", SB_VER)
    if os.path.exists(SB_BIN) and ver in sh(f"{SB_BIN} version", check=False):
        return                                          # 已是目标版本，跳过
    a = arch_tag()
    url = (f"https://github.com/SagerNet/sing-box/releases/download/"
           f"v{ver}/sing-box-{ver}-linux-{a}.tar.gz")
    _dl_gh(url, "/tmp/sb.tgz")
    sh("tar -xzf /tmp/sb.tgz -C /tmp")
    sh(f"install -m755 /tmp/sing-box-{ver}-linux-{a}/sing-box {SB_BIN}")
    _verify_core(SB_BIN, ver, "sing-box")
    os.makedirs(SB_DIR, exist_ok=True)

def install_xray():
    ver = newest_gh_release("XTLS/Xray-core", XRAY_VER)   # 含预发行取最大：XTLS 把每个 release 都标预发行
    if os.path.exists(XRAY_BIN) and ver in sh(f"{XRAY_BIN} version", check=False):
        return
    a = arch_tag()
    zmap = {"amd64": "64", "arm64": "arm64-v8a"}
    url = (f"https://github.com/XTLS/Xray-core/releases/download/"
           f"v{ver}/Xray-linux-{zmap[a]}.zip")
    _dl_gh(url, "/tmp/xray.zip")
    sh("unzip -o /tmp/xray.zip -d /tmp/xray")
    sh(f"install -m755 /tmp/xray/xray {XRAY_BIN}")
    _verify_core(XRAY_BIN, ver, "xray")
    os.makedirs(XRAY_DIR, exist_ok=True)

def reality_keys(binpath, cmd):
    """返回 (private, public)。两核心均是私钥在前、公钥在后。"""
    out = sh(f"{binpath} {cmd}").splitlines()
    priv = out[0].split(":")[-1].strip()
    pub  = out[1].split(":")[-1].strip()
    return priv, pub

def core_check(binpath, cfg):
    """校验核心配置文件。返回 (ok, msg)。
       sing-box 用 `check -c`，xray 用 `run -test -c`（xray 无 check 子命令）；
       内核太旧不认校验命令时按『通过』处理（ok=True），避免误伤。"""
    check_cmd = (f"{binpath} run -test -c {cfg}" if "xray" in os.path.basename(binpath)
                 else f"{binpath} check -c {cfg}")
    r = subprocess.run(check_cmd, shell=True, text=True, capture_output=True)
    msg = (r.stderr or r.stdout).strip()
    if r.returncode and re.search(r"unknown command|unknown flag|Run '.*help'", msg):
        return True, ""
    return (r.returncode == 0), msg

def write_service(name, binpath, cfg):
    # 先校验配置，schema 错就当场报出来（避免像之前 anytls 那样静默起不来）
    ok, msg = core_check(binpath, cfg)
    if not ok:
        raise RuntimeError(f"{name} 配置校验失败（多半是内核版本太旧不认某协议）:\n{msg}")
    unit_path = f"/etc/systemd/system/{name}.service"
    # 不覆盖指向别的程序的同名服务（典型：机器上已装 mack-a 的 sing-box.service）
    if os.path.exists(unit_path) and binpath not in open(unit_path).read():
        raise RuntimeError(
            f"{unit_path} 已存在且指向别的程序（可能是 mack-a 等现有安装）。"
            f"本脚本不覆盖它以免破坏现有服务。请在干净的机器上运行，"
            f"或先卸载现有 {name}（systemctl disable --now {name} 并删除该 unit）。")
    unit = (f"[Unit]\nAfter=network.target nss-lookup.target\n"
            f"[Service]\nExecStart={binpath} run -c {cfg}\n"
            f"{UNIT_RELOAD_LINE}"
            f"Restart=on-failure\nRestartSec=3\nLimitNOFILE=1000000\n"
            f"[Install]\nWantedBy=multi-user.target\n")
    open(unit_path, "w").write(unit)
    sh("systemctl daemon-reload")
    sh(f"systemctl enable {name}", check=False)
    sh(f"systemctl restart {name}")                     # restart 而非 enable --now：重跑能加载新配置

# ============================================================================
# sing-box 协议表 —— 每个 builder 返回 (inbound_dict, share_link)
# ============================================================================
def sb_reality_vision(port, tag):
    uid = new_uuid(); sid = short_id()
    priv, pub = reality_keys(SB_BIN, "generate reality-keypair")
    # sni-split：reality 监听 127.0.0.1，由 nginx stream 按 SNI 转发进来，链接对外报 443；
    # 否则常规监听公网端口。
    split = bool(G.get("sni_split"))
    listen = "127.0.0.1" if split else "::"
    ib = {"type": "vless", "tag": tag, "listen": listen, "listen_port": port,
          "users": [{"uuid": uid, "flow": "xtls-rprx-vision"}],
          "tls": {"enabled": True, "server_name": G["sni"],
                  "reality": {"enabled": True,
                              "handshake": {"server": G["sni"], "server_port": 443},
                              "private_key": priv, "short_id": [sid]}}}
    link_port = 443 if split else port
    if split:
        NGINX_STREAM.append({"sni": G["sni"], "port": port})   # SNI → 本地 reality 端口
    lk = (f"vless://{uid}@{G['host']}:{link_port}?encryption=none&flow=xtls-rprx-vision"
          f"&security=reality&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=tcp#{tag}")
    return ib, lk

def sb_reality_grpc(port, tag):
    uid = new_uuid(); sid = short_id(); svc = "grpc" + secrets.token_hex(2)
    priv, pub = reality_keys(SB_BIN, "generate reality-keypair")
    ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"uuid": uid}],
          "tls": {"enabled": True, "server_name": G["sni"],
                  "reality": {"enabled": True,
                              "handshake": {"server": G["sni"], "server_port": 443},
                              "private_key": priv, "short_id": [sid]}},
          "transport": {"type": "grpc", "service_name": svc}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=grpc"
          f"&serviceName={svc}&mode=gun#{tag}")
    return ib, lk

def setup_port_hopping(target_port, rng):
    """把 rng(如 30000-31000)这段 UDP 用 iptables DNAT 转发到真实端口，实现端口跳跃。
       与 mack-a 同法。带 comment 便于去重/清理；尽量持久化。"""
    lo, hi = rng.split("-")
    tagc = "xy_hy2_portHopping"
    # 先清掉这段 UDP 上所有旧 DNAT 规则——不只本脚本的，还包括 mack-a 等残留的
    # “强制固定”规则（它们指向已死的旧端口，且可能排在前面先匹配，导致 hy2 不通）。
    # inbound 监听 :: 双栈，跳跃段 v4/v6 都要转发，否则 IPv6 客户端走 mport 全挂。
    for ipt in ("iptables", "ip6tables"):
        if not have(ipt):
            continue
        for line in sh(f"{ipt} -t nat -S PREROUTING", check=False).splitlines():
            if not line.startswith("-A"):
                continue
            if "portHopping" in line or f"--dport {lo}:{hi}" in line:
                sh(f"{ipt} -t nat " + line.replace("-A", "-D", 1), check=False)
        sh(f"{ipt} -t nat -A PREROUTING -p udp --dport {lo}:{hi} "
           f"-m comment --comment {tagc} -j DNAT --to-destination :{target_port}", check=False)
    # 尽量持久化（重启后仍生效）；没有 netfilter-persistent 就装一下
    if not have("netfilter-persistent"):
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent", check=False)
    sh("netfilter-persistent save", check=False)

def sb_hysteria2(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    obfs_pw = new_pw()                                   # salamander 混淆：把 QUIC 包头也扰乱，
    #   让流量不再"长得像 QUIC/hysteria"，抗 DPI 识别、也可能绕过针对 QUIC 的运营商 QoS。
    #   开销极小（每包一次 XOR）；服务端/客户端密码由脚本两端自动对齐。
    ib = {"type": "hysteria2", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}],
          "obfs": {"type": "salamander", "password": obfs_pw},
          "tls": {"enabled": True, "alpn": ["h3"],
                  "certificate_path": crt, "key_path": key}}
    rng = hy2_range()                                    # 用户自定义跳跃范围，默认 30000-31000；关了为 ''
    mport = ""
    if rng:
        setup_port_hopping(port, rng)                    # 端口跳跃：UDP 段 DNAT 到本端口
        mport = f"&mport={rng}"
    lk = (f"hysteria2://{pw}@{G['host']}:{port}?sni={tls_host()}"
          f"&obfs=salamander&obfs-password={obfs_pw}"
          f"{mport}&insecure={1 if insec else 0}#{tag}")
    return ib, lk

def sb_tuic(port, tag):
    uid = new_uuid(); pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "tuic", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"uuid": uid, "password": pw}], "congestion_control": "bbr",
          "tls": {"enabled": True, "alpn": ["h3"],
                  "certificate_path": crt, "key_path": key}}
    lk = (f"tuic://{uid}:{pw}@{G['host']}:{port}?congestion_control=bbr&alpn=h3"
          f"&sni={tls_host()}&allow_insecure={1 if insec else 0}#{tag}")
    return ib, lk

def sb_anytls(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "anytls", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}], "padding_scheme": [],
          "tls": {"enabled": True, "certificate_path": crt, "key_path": key}}
    lk = (f"anytls://{pw}@{G['host']}:{port}?sni={tls_host()}"
          f"&insecure={1 if insec else 0}#{tag}")
    return ib, lk

def sb_ss2022(port, tag):
    method = "2022-blake3-aes-128-gcm"; key = ss2022_key(method)
    ib = {"type": "shadowsocks", "tag": tag, "listen": "::", "listen_port": port,
          "method": method, "password": key}
    lk = f"ss://{ss_userinfo(method, key)}@{G['host']}:{port}#{tag}"
    return ib, lk

# --- ws / h2 / httpupgrade 传输参数化：加一种传输 = 加一行映射 ---
def _sb_transport(transport, path, host):
    if transport == "ws":          return {"type": "ws", "path": path}
    if transport == "h2":          return {"type": "http", "path": path, "host": [host]}
    if transport == "httpupgrade": return {"type": "httpupgrade", "path": path, "host": host}
    raise ValueError(transport)

_LINK_NET = {"ws": "ws", "h2": "http", "httpupgrade": "httpupgrade"}      # vless URI
_VMESS_NET = {"ws": "ws", "h2": "h2", "httpupgrade": "httpupgrade"}       # vmess json

def _nginx_front():
    """ws 家族是否走 nginx 443 前置：reality 绑 443 时 443 归 reality，
       此时 nginx 只留 :80 续期、不再前置 ws，ws 改走自己端口的真证书。"""
    return bool(G.get("nginx")) and not G.get("reality443")

def make_sb_vless(transport):
    def b(port, tag):
        uid = new_uuid(); path = "/" + secrets.token_hex(3)
        mux = bool(G.get("smux")) and transport in ("ws", "httpupgrade")  # 仅 ws 家族、且用户选了才开 smux
        smk = "&smux=1" if mux else ""
        if _nginx_front() and transport in ("ws", "httpupgrade"):
            # nginx 前置：本地明文口，TLS 由 nginx 在 443 终结、按 path 反代进来
            ib = {"type": "vless", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
                  "users": [{"uuid": uid}],
                  "transport": _sb_transport(transport, path, tls_host())}
            if mux:
                ib["multiplex"] = {"enabled": True}
            NGINX_WS.append({"path": path, "port": port})
            lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=tls"
                  f"&sni={tls_host()}&type={_LINK_NET[transport]}&host={tls_host()}"
                  f"&path={path}{smk}#{tag}")
            return ib, lk
        crt, key, insec = ensure_acme()
        ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
              "users": [{"uuid": uid}],
              "tls": {"enabled": True, "server_name": tls_host(),
                      "certificate_path": crt, "key_path": key},
              "transport": _sb_transport(transport, path, tls_host())}
        if mux:
            ib["multiplex"] = {"enabled": True}
        lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls"
              f"&sni={tls_host()}&type={_LINK_NET[transport]}&host={tls_host()}"
              f"&path={path}&allowInsecure={1 if insec else 0}{smk}#{tag}")
        return ib, lk
    return b

def make_sb_vmess(transport):
    def b(port, tag):
        uid = new_uuid(); path = "/" + secrets.token_hex(3)
        mux = bool(G.get("smux")) and transport in ("ws", "httpupgrade")  # 仅 ws 家族、且用户选了才开 smux
        smk = {"smux": "1"} if mux else {}
        if _nginx_front() and transport in ("ws", "httpupgrade"):
            ib = {"type": "vmess", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
                  "users": [{"uuid": uid, "alterId": 0}],
                  "transport": _sb_transport(transport, path, tls_host())}
            if mux:
                ib["multiplex"] = {"enabled": True}
            NGINX_WS.append({"path": path, "port": port})
            lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": "443",
                             "id": uid, "aid": "0", "net": _VMESS_NET[transport],
                             "type": "none", "host": tls_host(), "path": path,
                             "tls": "tls", "sni": tls_host(), **smk})
            return ib, lk
        crt, key, insec = ensure_acme()
        ib = {"type": "vmess", "tag": tag, "listen": "::", "listen_port": port,
              "users": [{"uuid": uid, "alterId": 0}],
              "tls": {"enabled": True, "server_name": tls_host(),
                      "certificate_path": crt, "key_path": key},
              "transport": _sb_transport(transport, path, tls_host())}
        if mux:
            ib["multiplex"] = {"enabled": True}
        lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": str(port),
                         "id": uid, "aid": "0", "net": _VMESS_NET[transport],
                         "type": "none", "host": tls_host(), "path": path,
                         "tls": "tls", "sni": tls_host(), **smk})
        return ib, lk
    return b

def sb_trojan(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "trojan", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}],
          "tls": {"enabled": True, "server_name": tls_host(),
                  "certificate_path": crt, "key_path": key}}
    lk = (f"trojan://{pw}@{G['host']}:{port}?security=tls&sni={tls_host()}"
          f"&type=tcp&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

def sb_socks5(port, tag):
    user = "u" + secrets.token_hex(2); pw = new_pw()
    ib = {"type": "socks", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"username": user, "password": pw}]}
    ui = base64.urlsafe_b64encode(f"{user}:{pw}".encode()).decode().rstrip("=")
    lk = f"socks://{ui}@{G['host']}:{port}#{tag}"
    return ib, lk

def sb_naive(port, tag):
    # naive 客户端会校验证书，强烈建议配 --domain 走真证书，自签基本连不上
    user = "u" + secrets.token_hex(2); pw = new_pw()
    crt, key, insec = ensure_acme()
    ib = {"type": "naive", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"username": user, "password": pw}],
          "tls": {"enabled": True, "server_name": tls_host(),
                  "certificate_path": crt, "key_path": key}}
    lk = f"naive+https://{user}:{pw}@{tls_host()}:{port}#{tag}"
    return ib, lk

def sb_shadowtls(port, tag):
    # ShadowTLS v3 = shadowtls inbound + detour 到一个仅本机的 shadowsocks inbound
    # 无通用分享 URI，直接吐 Mihomo YAML 片段（喂 Mihomo-fx 的 PROXIES_YAML）
    pw = new_pw(); method = "2022-blake3-aes-128-gcm"; sskey = ss2022_key(method)
    ss_tag = tag + "-ss"
    st_ib = {"type": "shadowtls", "tag": tag, "listen": "::", "listen_port": port,
             "version": 3, "users": [{"name": "user", "password": pw}],
             "handshake": {"server": G["sni"], "server_port": 443},
             "strict_mode": True, "detour": ss_tag}
    ss_ib = {"type": "shadowsocks", "tag": ss_tag, "listen": "127.0.0.1",
             "method": method, "password": sskey}   # detour 目标，不占公网端口
    yml = (f"  # ShadowTLS(喂 PROXIES_YAML):\n"
           f"  # - {{name: {tag}, type: ss, server: {G['host']}, port: {port}, "
           f"cipher: {method}, password: {sskey}, plugin: shadow-tls, "
           f"plugin-opts: {{host: {G['sni']}, password: {pw}, version: 3}}}}")
    return [st_ib, ss_ib], yml

def sb_vless_vision(port, tag):
    # VLESS + TCP + 真 TLS + XTLS-Vision（对应 mack-a 的 VLESS_TCP/TLS_Vision）
    # 与 reality-vision 区别：这条用服务器自己的证书（给域名走 acme，否则自签+insecure）
    uid = new_uuid(); crt, key, insec = ensure_acme()
    ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"uuid": uid, "flow": "xtls-rprx-vision"}],
          "tls": {"enabled": True, "server_name": tls_host(),
                  "certificate_path": crt, "key_path": key}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&flow=xtls-rprx-vision"
          f"&security=tls&sni={tls_host()}&fp=chrome&type=tcp"
          f"&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

# 当前只装这 10 个协议（对齐 mack-a 的输出，顺序也一致）。
# 想加回其它协议：把下面「备用」块里对应行搬进 SB 即可——builder 都还在，没删。
# ═══════════════════════════════════════════════════════════════════════════════
# 协议注册表
# ═══════════════════════════════════════════════════════════════════════════════
# 一个协议的【全部信息集中在一条记录里】：怎么建、删它要清什么、选中它要补问什么、
# 它有哪些会影响流程的性质。这样加新协议就是加一行，不用再满文件找地方补代码——
# 以前 hy2 的 iptables 规则和 ws 的 nginx 反代都是散在各处建的，只有「建」没有「拆」，
# 删协议时全靠人肉记得，漏一个就是一个不报错的坑。
#
# ── 加一个新协议的清单 ──────────────────────────────────────────────────
#   1. 写 builder：(port, tag) -> (inbound_dict, 分享链接)。
#      builder 里顺手建的系统副作用（如 hy2 的 iptables DNAT），必须在第 2 步配一个
#      teardown 把它收回来——「谁建的谁负责拆」是这张表唯一要守的纪律。
#   2. 在下面的表里加一行 PROTO(...)，据实填这几项（不填=没有这个性质）：
#        udp=True       入站是 UDP（hy2 跳跃段的冲突检测据此判断会不会被劫走）
#        reality=True   reality 类（借用 SNI 的预检、443 优先级要用）
#        ws_front=True  nginx 前置时藏在 443 后面（增量添加/删除的守卫要用）
#        cert=True      吃服务器证书（无域名时退化成自签+insecure，要进自签告警）
#        teardown=fn    删它时要清的副作用。fn(plan=True) 只回答「会清什么」给确认框看，
#                       fn() 才真动手并回报清了什么；不适用时两种模式都返回 ""
#        asks=fn        选中它才问的专属设置，fn(mode)，mode 为 install / add
#        recap=fn       确认框里回显的 (标题, 值)，不需要就别填
#      teardown/asks/recap 允许多个协议共用同一个函数（ws 三兄弟就是），
#      按函数对象去重，所以不会问三遍、也不会把 nginx 反代表重写三遍。
#      字段名打错、忘了包 PROTO(...)、reality 忘了标 —— 都会被 _check_proto_tables()
#      在启动时当场 SystemExit，不会留到用户跑到那一步才发作。
#   3. 客户端侧另有三个按【节点类型】分发的地方，新协议若走了新的传输方式才要动：
#        link_to_proxy()     分享链接 -> mihomo 节点（按 scheme/type 通用解析，通常不用动）
#        proto_key()         mihomo 节点 -> 协议键（决定 sing-box 订阅里映射到哪个 tag）
#        shadowrocket_line() mihomo 节点 -> 小火箭行（不支持的返回 None 自动跳过）
#   其余地方（安装/增量添加/删除/自签告警/UDP 冲突检测/确认框）全部读这张表，
#   不要再往那些流程里写协议名——写死一个名字，就是下一个没人记得的坑。
# ══════════════════════════════════════════════════════════════════════════════

def PROTO(build, *, udp=False, reality=False, ws_front=False, cert=False,
          teardown=None, asks=None, recap=None):
    """一条协议记录。字段含义见上面的清单。"""
    return {"build": build, "udp": udp, "reality": reality, "ws_front": ws_front,
            "cert": cert, "teardown": teardown, "asks": asks, "recap": recap}

def proto_pick(flag, sb_names=(), xr_names=()):
    """选中的协议里带某个性质的那些（据注册表，不靠名字前缀猜）。去重保序。"""
    got = ([n for n in sb_names if SB.get(n, {}).get(flag)]
           + [n for n in xr_names if XRAY.get(n, {}).get(flag)])
    return list(dict.fromkeys(got))

def proto_flag(flag, sb_names=(), xr_names=()):
    """选中的协议里有没有带某个性质的。"""
    return bool(proto_pick(flag, sb_names, xr_names))

def _proto_fns(key, sb_names=(), xr_names=()):
    """收集选中协议的 teardown / asks / recap 函数，按出现顺序去重。

       去重是必须的：ws 三兄弟共用同一个 teardown/asks，不去重就会问三遍、
       把 nginx 反代表重写三遍。用函数对象本身做键，共用即自动合并。"""
    out = []
    for table, names in ((SB, sb_names), (XRAY, xr_names)):
        for n in names:
            fn = table.get(n, {}).get(key)
            if fn and fn not in out:
                out.append(fn)
    return out

def run_asks(mode, sb_names=(), xr_names=()):
    """按注册表补问选中协议各自的专属设置。mode: install(全新安装) / add(增量添加)。"""
    for fn in _proto_fns("asks", sb_names, xr_names):
        fn(mode)

def _pad(text, width=13):
    """按【显示宽度】右补空格（中文/emoji 占两列，str.ljust 按字符数算会对不齐）。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - w)

def recap_lines(sb_names=(), xr_names=()):
    """确认框里要回显的 [(标题, 值)]，同样来自注册表。"""
    out = []
    for fn in _proto_fns("recap", sb_names, xr_names):
        r = fn()
        if r:
            out.append(r)
    return out

# ── teardown：删协议时要清的副作用 ───────────────────────────────────────
# 约定：fn(plan=True) 只回答「会清什么」（给确认框看），fn() 才真动手并回报清了什么；
# 两种模式写在同一个函数里，以后改动只有一处，不会出现「确认框说清 A、实际清了 B」。
# 判断不适用（如 nginx 根本没前置）时两种模式都返回 ""，调用方据此跳过。

def _td_hy2(plan=False):
    """删 hy2：清掉端口跳跃的 iptables DNAT。

       不清的话，整段 UDP 会继续被转给一个已经不存在的端口——不仅 hy2 没了，
       以后装进那一段的任何 UDP 服务也会被一起劫走，而且毫无报错。"""
    if plan:
        return "端口跳跃的 iptables DNAT 规则"
    n = _drop_hy2_dnat()
    return f"已清掉 {n} 条端口跳跃 DNAT 规则" if n else "没有残留的端口跳跃 DNAT 规则"

def _td_ws_front(plan=False):
    """删 nginx 前置的 ws 类：按【剩下的】节点重写 443 的 location 反代表。

       不重写的话，那条 path 还挂在 nginx 上、反代到一个已经没人听的本地端口，
       客户端拿到的是 502 而不是「节点没了」，排查起来更费劲。
       只在 nginx 真的前置着 ws 时才动它（reality 绑 443 的布局下 ws 走自己的端口）。"""
    if not _nginx_front():
        return ""
    if plan:
        return "nginx 443 上对应的 location 反代"
    ws = _rebuild_nginx_ws_from_cfg()
    write_nginx_conf()
    return f"nginx 反代表已按剩下的 {len(ws)} 个 ws 节点重写"

# ── asks / recap：选中该协议才问的设置，以及确认框里的回显 ────────────────

def _asks_hy2(mode):
    """选了 hy2：问端口跳跃范围（带冲突检测）。"""
    # include_own：全新安装时本脚本自己的节点马上要重建，现在监听着的不算冲突
    cur = (G.get("hy2_ports") or "").strip() if mode == "add" else ""
    G["hy2_ports"] = ask_hy2_range(cur, include_own=(mode == "add"))
    if hy2_hop_on():
        print(f"  ⚠ 跳跃段 {hy2_range()} 的 \033[1mUDP 整段\033[0m 要在云厂商防火墙里放行，"
              f"否则跳到的端口连不上（GCE/阿里云这类默认只放行你列过的端口）。")

def _recap_hy2():
    return ("hy2 跳跃", hy2_range() or "关闭（固定单端口）")

def _asks_smux(mode):
    """选了 ws 家族：问要不要开 smux 多路复用。"""
    cur = G.get("smux", "")
    if mode == "add":
        ans = _ask(f"  ws 类开启 smux 多路复用?（{'回车沿用上次的开启' if cur else '回车=不开'}，"
                   f"y 开 / n 不开）: ").strip().lower()
        G["smux"] = "1" if ans in ("y", "yes") else ("" if ans in ("n", "no") else cur)
    else:
        ans = _ask("ws 类开启 smux 多路复用?(网页/小请求更快，大文件下载可能变慢) "
                   "y开启/n不开(回车=不开): ")
        G["smux"] = "1" if ans.lower() in ("y", "yes") else ""

def _recap_smux():
    return ("ws 多路复用", "开启 smux" if G.get("smux") else "不开(默认)")

def _asks_reality(mode):
    """选了 reality 类：确认借用目标 SNI 并做连通性预检。

       只在增量添加时问——全新安装时 SNI 是统一问的（没有域名时 ws/trojan 也拿它
       当伪装 Host，见 tls_host()），不能挪进来按协议问。"""
    if mode != "add":
        return
    ans = _ask(f"\n  reality 借用目标 SNI（回车沿用上次的 {G['sni']}）: ").strip()
    if ans:
        G["sni"] = ans
    _sni_precheck_one()

def _recap_reality():
    return ("借用 SNI", G["sni"])

# ── 表本体 ───────────────────────────────────────────────────────────────
# 当前只装这 10 个协议（对齐 mack-a 的输出，顺序也一致）。
# 想加回其它协议：把下面「备用」块里对应行搬进 SB 即可——builder 都还在，没删。
SB = {
    "vless-vision":      PROTO(sb_vless_vision, cert=True),
    "vless-ws":          PROTO(make_sb_vless("ws"), cert=True, ws_front=True,
                               teardown=_td_ws_front, asks=_asks_smux, recap=_recap_smux),
    "vmess-ws":          PROTO(make_sb_vmess("ws"), cert=True, ws_front=True,
                               teardown=_td_ws_front, asks=_asks_smux, recap=_recap_smux),
    "trojan":            PROTO(sb_trojan, cert=True),
    "hy2":               PROTO(sb_hysteria2, udp=True,
                               teardown=_td_hy2, asks=_asks_hy2, recap=_recap_hy2),
    "reality-vision":    PROTO(sb_reality_vision, reality=True,
                               asks=_asks_reality, recap=_recap_reality),
    "reality-grpc":      PROTO(sb_reality_grpc, reality=True,
                               asks=_asks_reality, recap=_recap_reality),
    "tuic":              PROTO(sb_tuic, udp=True),
    "vmess-httpupgrade": PROTO(make_sb_vmess("httpupgrade"), cert=True, ws_front=True,
                               teardown=_td_ws_front, asks=_asks_smux, recap=_recap_smux),
    "anytls":            PROTO(sb_anytls, cert=True),
}
# 备用（以后想加回，取消注释挪进上面的 SB，按上面的清单据实填标志位）：
#   "ss2022":            PROTO(sb_ss2022),
#   "vless-h2":          PROTO(make_sb_vless("h2"), cert=True),
#   "vless-httpupgrade": PROTO(make_sb_vless("httpupgrade"), cert=True, ws_front=True,
#                              teardown=_td_ws_front, asks=_asks_smux, recap=_recap_smux),
#   "vmess-h2":          PROTO(make_sb_vmess("h2"), cert=True),
#   "socks5":            PROTO(sb_socks5),
#   "naive": sb_naive,
#   "shadowtls": sb_shadowtls,

# ============================================================================
# xray 协议表 —— builder 返回 (inbound_dict, share_link)
# ============================================================================
def _xr_reality_stream(priv, sid, network, extra=None):
    # minClientVer=1.0.0：xray v26.7.11+ 的 reality 服务端默认 minClientVer=26.3.27，会静默
    # 拒掉上报旧版本的客户端（mihomo/Clash 系硬编码 1.8.2、sing-box、旧 xray）→ 连不上。
    # 显式设成 1.0.0（接受所有客户端），兼容优先，避免自动升级 xray 后老客户端集体连不上。
    st = {"network": network, "security": "reality",
          "realitySettings": {"show": False, "dest": f"{G['sni']}:443",
                              "xver": 0, "serverNames": [G["sni"]],
                              "privateKey": priv, "shortIds": [sid],
                              "minClientVer": "1.0.0"}}
    if extra:
        st.update(extra)
    return st

def xr_reality_vision(port, tag):
    uid = new_uuid(); sid = short_id()
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}],
                       "decryption": "none"},
          "streamSettings": _xr_reality_stream(priv, sid, "raw")}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&flow=xtls-rprx-vision"
          f"&security=reality&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=tcp#{tag}")
    return ib, lk

def xr_reality_grpc(port, tag):
    uid = new_uuid(); sid = short_id(); svc = "grpc" + secrets.token_hex(2)
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    st = _xr_reality_stream(priv, sid, "grpc",
                            {"grpcSettings": {"serviceName": svc, "multiMode": True}})
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": st}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=grpc"
          f"&serviceName={svc}&mode=multi#{tag}")
    return ib, lk

def xr_reality_xhttp(port, tag):
    uid = new_uuid(); sid = short_id(); path = "/" + secrets.token_hex(3)
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    st = _xr_reality_stream(priv, sid, "xhttp",
                            {"xhttpSettings": {"path": path}})
    # xhttp 传输不支持 xtls-rprx-vision flow（那是 raw/tcp 专属），客户端也没带 flow，
    # 服务端这里若强设 vision flow 会导致握手对不上 → 连不上，所以留空。
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": st}
    # host 显式带上、并与 sni 保持一致：mihomo 留空时会回退到 servername，结果一样，
    # 但写出来就不依赖客户端的回退实现，换客户端/版本也不会变。
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=xhttp"
          f"&host={G['sni']}&path={path}#{tag}")
    return ib, lk

def _xr_tls(certfile, keyfile):
    return {"certificates": [{"certificateFile": certfile, "keyFile": keyfile}]}

def xr_vless_ws(port, tag):
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": {"network": "ws", "security": "tls",
                             "wsSettings": {"path": path},
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls"
          f"&sni={tls_host()}&type=ws&host={tls_host()}&path={path}"
          f"&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

def xr_vmess_ws(port, tag):
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vmess", "tag": tag,
          "settings": {"clients": [{"id": uid, "alterId": 0}]},
          "streamSettings": {"network": "ws", "security": "tls",
                             "wsSettings": {"path": path},
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": str(port),
                     "id": uid, "aid": "0", "net": "ws", "type": "none",
                     "host": tls_host(), "path": path, "tls": "tls", "sni": tls_host()})
    return ib, lk

def xr_vless_xhttp_tls(port, tag):
    """VLESS + XHTTP + TLS：独立随机端口、自带证书、【不经 nginx】。

       跟 reality-xhttp 的区别只在伪装方式：reality 借别人的站，这个用自己的
       域名证书。好处是不依赖借用目标站的存活/可达性，也不受 reality 那套
       「借用站必须支持 TLS1.3+H2、不能是 CDN」的挑剔；代价是暴露自己的域名。

       为什么不照搬 mack-a 的「任意门」两层结构（dokodemo-door 公网口 →
       127.0.0.1:45988 的真入站）：那是为了在他那套【单配置多入站 + 复杂分流】
       里给这个入站单独挂一条绕过分流的路由（inboundTag → z_direct_outbound）。
       本脚本每个入站各自独立、出站只有 direct/block，中间这一跳不产生任何
       区别，只多一次本地转发和一条路由规则，所以直接监听公网口。"""
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    crt, key, insec = ensure_acme()
    tls = _xr_tls(crt, key)
    tls["serverName"] = tls_host()
    tls["minVersion"] = "1.2"
    tls["alpn"] = ["h2", "http/1.1"]
    if not insec:
        # 真证书才开：自签场景下客户端可能按 IP 连、不发 SNI，开了会被直接拒
        tls["rejectUnknownSni"] = True
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": {"network": "xhttp", "security": "tls",
                             "xhttpSettings": {"host": tls_host(), "path": path,
                                               "mode": "auto"},
                             "tlsSettings": tls}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls"
          f"&sni={tls_host()}&fp=chrome&type=xhttp&host={tls_host()}&path={path}"
          f"&mode=auto&alpn=h2&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

def xr_trojan(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "trojan", "tag": tag,
          "settings": {"clients": [{"password": pw}]},
          "streamSettings": {"network": "raw", "security": "tls",
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = (f"trojan://{pw}@{G['host']}:{port}?security=tls&sni={tls_host()}"
          f"&type=tcp&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

# xray 的 reality 三兄弟原来叫 vless-reality-*，三段名在手机客户端里显示不下、
# 后半截被截掉（vless-reality-x…）。vless- 是冗余的——这几个本来就都是 vless，
# sing-box 那边同样的东西就叫 reality-vision/grpc。统一砍成两段。
XRAY = {
    "reality-vision": PROTO(xr_reality_vision, reality=True,
                            asks=_asks_reality, recap=_recap_reality),
    "reality-grpc":   PROTO(xr_reality_grpc, reality=True,
                            asks=_asks_reality, recap=_recap_reality),
    "reality-xhttp":  PROTO(xr_reality_xhttp, reality=True,
                            asks=_asks_reality, recap=_recap_reality),
    # xray 的 ws/trojan/xhttp-tls 都听自己的端口、自己终结 TLS，不挂 nginx
    # （所以不是 ws_front），无域名时退化成自签+allowInsecure → cert=True。
    "xhttp-tls":      PROTO(xr_vless_xhttp_tls, cert=True),
    "vless-ws":       PROTO(xr_vless_ws, cert=True),
    "vmess-ws":       PROTO(xr_vmess_ws, cert=True),
    "trojan":         PROTO(xr_trojan, cert=True),
}
# 已移除 ss2022：纯全加密无伪装，易被 GFW 全加密流量探测识别；有 reality 完全无需它。

def _check_proto_tables():
    """启动即自检协议表。写错一个字段名（比如 ws_front 打成 wsfront）默认值就会一直是
       False，流程照跑、只是悄悄少做一件事——这种 bug 最难查，所以在这里直接拦下来。"""
    fields = set(PROTO(lambda p, t: None))
    for label, table in (("SB", SB), ("XRAY", XRAY)):
        for n, p in table.items():
            if not isinstance(p, dict) or set(p) != fields:
                raise SystemExit(f"协议表 {label}['{n}'] 不是 PROTO(...) 记录（字段对不上）")
            if not callable(p["build"]):
                raise SystemExit(f"协议表 {label}['{n}'] 的 build 不可调用")
            for k in ("teardown", "asks", "recap"):
                if p[k] is not None and not callable(p[k]):
                    raise SystemExit(f"协议表 {label}['{n}'] 的 {k} 既不是 None 也不可调用")
    for n in REALITY_443_PRIORITY:
        if not (SB.get(n, {}).get("reality") or XRAY.get(n, {}).get("reality")):
            raise SystemExit(f"REALITY_443_PRIORITY 里的 {n} 在协议表里不存在或没标 reality=True")
    if not SB.get(SNI_SPLIT_BACKEND, {}).get("reality"):
        raise SystemExit(f"SNI_SPLIT_BACKEND 指定的 {SNI_SPLIT_BACKEND} "
                         f"不在 sing-box 协议表里、或没标 reality=True")

# 历史旧名 → 现名。老节点的名字烤在分享链接里，命令行也可能还写着旧名，都得认。
# 老节点点一次『更新配置』就会跟着变短（规范化在渲染时做，见 _sep_name）。
_PROTO_ALIASES = {"vless-reality-vision": "reality-vision",
                  "vless-reality-grpc":   "reality-grpc",
                  "vless-reality-xhttp":  "reality-xhttp"}

# ============================================================================ 组装
# reality 绑 443 的优先级：优先 sing-box reality-vision（Vision flow 最稳），依次往下。
# 只能有一个 reality 上 443（443/TCP 独占），其余 reality 留在随机端口。
REALITY_443_PRIORITY = ["reality-vision", "reality-grpc", "reality-xhttp"]

# sni-split 的 443 后端只能是 sing-box 的这一个协议：整个文件里只有它的 builder 会
# 往 NGINX_STREAM 登记「借用 SNI → 本地端口」。挪到常量里，是为了让『为什么偏偏是它』
# 有地方写，也让下面的自检替我们盯着别名/改名。
SNI_SPLIT_BACKEND = "reality-vision"

_check_proto_tables()      # 协议表写错字段 → 这里立刻 SystemExit，不留到运行时才发作

def pick_reality_443(sb_names, xr_names):
    """返回 (要绑 443 的 reality 协议名, 归属核心)；没有 reality 被选则返回 ('', '')。

       必须带上归属核心：reality-vision / reality-grpc 两个核心【同名】，
       而 443/TCP 只能给一个入站。不区分核心的话 pin 会同时命中两边，
       两个入站一起抢 443，起不来。按优先级表 sing-box 先到先得。"""
    for n in REALITY_443_PRIORITY:
        if n in sb_names:
            return n, "sb"
        if n in xr_names:
            return n, "xray"
    return "", ""

def _tag(prefix, name):
    """节点名 = 前缀 + 分隔点 + 协议名（没设前缀就只有协议名）。

       分隔点不能省：没有它，USA/HK 这类【文字前缀】会和协议名连成 `USAvless-ws`，
       国家分组正则里的 `\bUSA\b` 因为右边界不成立而匹配不上，分组直接建不出来
       （emoji 前缀不受影响——它们直接命中 🇺🇸 那一支，不走 \b）。
       顺带也解决了面板上前缀和协议糊成一坨、读不出来的问题。"""
    p = (prefix or "").strip()
    return (p + "·" if p else "") + name

def build(table, names, pinned=None, dup=None, mark=""):
    """pinned: {协议名: 固定端口}，用于把某个 reality 协议钉在 443；其余走随机端口。
       dup/mark: 两核心同名协议(vless-ws/vmess-ws/trojan)集合 dup 里的，名字尾部加个小上标
                 mark 区分（sing-box=¹ / xray=²），避免客户端订阅重名报错；比 -xray 后缀短，
                 手机上也显示得下。"""
    pinned = pinned or {}
    dup = dup or set()
    names = list(dict.fromkeys(names))           # 去重保序：--sb hy2,hy2 不至于生成两个同 tag inbound
    inbounds, links = [], []
    for n in names:
        # 名称 = 用户前缀 + 协议名（默认无前缀，别人部署 US/SG 时自己填 🇺🇸/🇸🇬 等）
        port = pinned.get(n) or next_port()
        tag = _tag(G.get("prefix", ""), n)
        if n in dup:                             # 两核心都有该协议 → 尾部小上标区分（sb ¹ / xray ²）
            tag += mark
        ib, lk = table[n]["build"](port, tag)
        inbounds.append(ib); links.append(lk)
    return inbounds, links

# ============================================================================ 订阅
# YAML 标量：除了真数字和这两个布尔字面量，字符串一律加引号。
#
# 不加引号会出事，而且是在别人的客户端里出事。reality 的 short-id 是 8 位随机 hex，
# 随机到 38e92059 这种「数字 e 数字」的形状时，按 YAML 1.2 的浮点规则它是 38×10^92059
# —— 远超双精度上限，解析出来直接是 Infinity。客户端接着要把配置 JSON 化时就炸了
# （ClashMi: Converting object to an encodable object failed: Infinity）。
#
# 最难查的是各家解析器口径还不一样，同一份配置有的能用有的不能：
#   Go(mihomo)  ParseFloat 溢出报错 → 回退成字符串，没事
#   PyYAML      走 YAML 1.1，浮点必须带小数点 → 当字符串，没事（所以在服务端自测扫不出来）
#   Dart / JS   走 YAML 1.2 → Infinity，炸
#
# 所以这里不做「这个值像不像数字」的判断——正是这种判断在各家 YAML 版本之间不一致。
# 反过来做白名单：只有真 int/float/bool 和下面这两个字面量裸写，其余全部带引号。
# 代价只是配置里多一堆引号，换来的是任何随机值都不会再被谁读成别的类型。
_YAML_BARE = ("true", "false")

def _yq(s):
    """字符串 → YAML 双引号标量（按双引号规则转义，节点名里带引号/反斜杠也不会破格式）。"""
    out = ['"']
    for ch in s:
        if   ch == "\\": out.append("\\\\")
        elif ch == '"':  out.append('\\"')
        elif ch == "\n": out.append("\\n")
        elif ch == "\r": out.append("\\r")
        elif ch == "\t": out.append("\\t")
        elif ord(ch) < 0x20: out.append("\\x%02x" % ord(ch))
        else: out.append(ch)
    out.append('"')
    return "".join(out)

def _yfmt(v):
    if isinstance(v, dict): return "{" + ", ".join(f"{k}: {_yfmt(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list): return "[" + ", ".join(_yfmt(x) for x in v) + "]"
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, (int, float)): return str(v)
    s = str(v)
    return s if s in _YAML_BARE else _yq(s)

# X25519MLKEM768 后量子 KEX 需要新核心：sing-box>=1.12.0、xray>=25.5.16。
# 客户端主动发起该握手，若服务端核心太旧会直接握手失败，故装机核心太旧时不下发此字段。
_MLKEM_MIN = {SB_BIN: (1, 12, 0), XRAY_BIN: (25, 5, 16)}
_MLKEM_CACHE = None

def _core_ver(binpath):
    """读核心版本号 → (a,b,c) 元组；读不到返回 None。"""
    out = sh(f"{binpath} version", check=False)
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    return tuple(int(x or 0) for x in m.groups()) if m else None

def mlkem_ok():
    """已装核心是否都够新以支持 X25519MLKEM768（保守：装了但版本读不出/太旧 → False）。"""
    global _MLKEM_CACHE
    if _MLKEM_CACHE is not None:
        return _MLKEM_CACHE
    ok = True
    for binpath, floor in _MLKEM_MIN.items():
        if os.path.exists(binpath):
            v = _core_ver(binpath)
            if v is None or v < floor:
                ok = False
    _MLKEM_CACHE = ok
    return ok

# ws 家族 smux 多路复用（mihomo 客户端；服务端 sing-box 同步开 multiplex）
# 是否开启由 G["smux"] 决定（安装时询问，默认关：多路复用可能拖慢大文件下载）
_SMUX = {"enabled": "true", "protocol": "h2mux",
         "max-connections": 4, "min-streams": 4, "padding": "true"}
# sing-box 客户端出站的等价多路复用配置
_SB_MUX = {"enabled": True, "protocol": "h2mux",
           "max_connections": 4, "min_streams": 4, "padding": True}

def link_to_proxy(u):
    """分享链接 → Mihomo proxy dict（客户端节点）。解析不了返回 None。"""
    P = urllib.parse.urlparse(u); qs = {k: v[0] for k, v in urllib.parse.parse_qs(P.query).items()}
    sch, host, port = P.scheme, P.hostname, P.port
    uq = urllib.parse.unquote
    def nm(default):
        # 名称直接用链接里的 #备注（已含用户前缀+协议）；不再硬编码国旗。
        # 不在这里拼引号——渲染 YAML 时由 _yfmt 统一加并转义。
        return uq(P.fragment) if P.fragment else default
    insec = qs.get("insecure") == "1" or qs.get("allowInsecure") == "1" or qs.get("allow_insecure") == "1"
    if sch == "vless":
        net = qs.get("type", "tcp"); sec = qs.get("security", "none")
        d = {"name": nm("vless"), "type": "vless", "server": host, "port": port, "uuid": P.username, "udp": "true"}
        if qs.get("flow"): d["flow"] = qs["flow"]
        d["tls"] = "true"; d["client-fingerprint"] = qs.get("fp", "chrome")
        if qs.get("sni"): d["servername"] = qs["sni"]
        if sec == "reality":
            d["reality-opts"] = {"public-key": qs.get("pbk", ""), "short-id": qs.get("sid", "")}
            # X25519MLKEM768 后量子 KEX：仅当本机核心够新才下发，避免旧核心握手失败
            if mlkem_ok():
                d["reality-opts"]["support-x25519mlkem768"] = "true"
            if net == "grpc": d["network"] = "grpc"; d["grpc-opts"] = {"grpc-service-name": qs.get("serviceName") or qs.get("path", "")}
            # xhttp 的 host 显式写出：mihomo 留空会回退到 servername(结果相同)，
            # 但别的客户端未必这么回退，写死更稳。xray 专属传输。
            elif net == "xhttp": d["network"] = "xhttp"; d["xhttp-opts"] = {"path": qs.get("path", "/"), "host": qs.get("host") or qs.get("sni") or host}
            else: d["network"] = "tcp"
        else:
            if insec: d["skip-cert-verify"] = "true"
            if net == "ws": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}}
            elif net == "httpupgrade": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}, "v2ray-http-upgrade": "true"}
            elif net == "grpc": d["network"] = "grpc"; d["grpc-opts"] = {"grpc-service-name": qs.get("serviceName") or qs.get("path", "")}
            elif net == "xhttp": d["network"] = "xhttp"; d["xhttp-opts"] = {"path": qs.get("path", "/"), "host": qs.get("host") or qs.get("sni") or host}
            else: d["network"] = "tcp"
            if qs.get("smux") == "1" and d.get("network") == "ws": d["smux"] = _SMUX
        return d
    if sch in ("hysteria2", "hy2"):
        d = {"name": nm("hy2"), "type": "hysteria2", "server": host, "port": port, "password": P.username, "udp": "true"}
        if qs.get("sni"): d["sni"] = qs["sni"]
        if insec: d["skip-cert-verify"] = "true"
        d["alpn"] = ["h3"]
        if qs.get("obfs") == "salamander" and qs.get("obfs-password"):   # salamander 混淆
            d["obfs"] = "salamander"; d["obfs-password"] = qs["obfs-password"]
        if qs.get("mport"): d["ports"] = qs["mport"]; d.pop("port")   # 端口跳跃：只留跳跃段，不写固定端口
        return d
    if sch == "tuic":
        d = {"name": nm("tuic"), "type": "tuic", "server": host, "port": port,
             "uuid": uq(P.username or ""), "password": uq(P.password or ""), "udp": "true"}
        if qs.get("congestion_control"): d["congestion-controller"] = qs["congestion_control"]
        d["alpn"] = ["h3"]
        if qs.get("sni"): d["sni"] = qs["sni"]
        if insec: d["skip-cert-verify"] = "true"
        return d
    if sch == "anytls":
        d = {"name": nm("anytls"), "type": "anytls", "server": host, "port": port, "password": P.username, "udp": "true"}
        if qs.get("sni"): d["sni"] = qs["sni"]
        if insec: d["skip-cert-verify"] = "true"
        return d
    if sch == "trojan":
        d = {"name": nm("trojan"), "type": "trojan", "server": host, "port": port, "password": P.username, "udp": "true"}
        if qs.get("sni"): d["sni"] = qs["sni"]
        if insec: d["skip-cert-verify"] = "true"
        d["client-fingerprint"] = qs.get("fp", "chrome")
        if qs.get("type") == "ws":                    # trojan+ws（CDN 套 Cloudflare 用）
            d["network"] = "ws"
            d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}}
        return d
    if sch == "vmess":
        b = u[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        name = j.get("ps", "vmess")
        d = {"name": name, "type": "vmess", "server": j["add"], "port": int(j["port"]), "uuid": j["id"],
             "alterId": int(j.get("aid", 0)), "cipher": j.get("scy", "auto"), "udp": "true"}
        if j.get("tls") == "tls": d["tls"] = "true"; d["servername"] = j.get("sni") or j.get("host")
        net = j.get("net", "tcp")
        if net == "ws": d["network"] = "ws"; d["ws-opts"] = {"path": j.get("path", "/"), "headers": {"Host": j.get("host", "")}}
        elif net == "httpupgrade": d["network"] = "ws"; d["ws-opts"] = {"path": j.get("path", "/"), "headers": {"Host": j.get("host", "")}, "v2ray-http-upgrade": "true"}
        if str(j.get("smux")) == "1" and d.get("network") == "ws": d["smux"] = _SMUX
        return d
    if sch == "ss":
        ui = P.username or ""
        dec = ui if ":" in ui else base64.urlsafe_b64decode(ui + "=" * (-len(ui) % 4)).decode()
        method, pw = dec.split(":", 1)
        return {"name": nm("ss"), "type": "ss", "server": host, "port": port, "cipher": method, "password": pw, "udp": "true"}
    return None

def _mirrors(url):
    """GitHub 三个域各自的兜底链，**直连永远排第一**，后面的只在前面失败时才轮到：
         raw.githubusercontent.com   → jsDelivr 两个节点 → 公共反代前缀
         api.github.com / github.com → 公共反代前缀（jsDelivr 只发 raw，不发这两类）
       raw 常被限流(429)，releases 则是整域不通的情况居多，两者都得有退路。"""
    urls = [url]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if m:
        o, repo, br, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
        urls.append(f"https://fastly.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
    if _GH_RE.match(url):
        urls += [pfx + url for pfx in own_relays()]     # 自己人的中转排在第三方前面
        urls += [pfx + url for pfx in GH_MIRRORS]
    return urls

def fetch_url(url):
    """带重试 + 镜像兜底的拉取，缓解 GitHub 429 限流。"""
    last = None
    for rd in range(2):                                 # 两轮，轮间退避
        for u in _mirrors(url):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "xy-installer"})
                return urllib.request.urlopen(req, timeout=15).read().decode()
            except Exception as e:
                last = e
        time.sleep(2 * (rd + 1))
    raise last

def _host():
    return open(HOST_FILE).read().strip() if os.path.exists(HOST_FILE) else (G.get("host") or public_ip())

def load_tokens():
    try: return json.load(open(TOKENS_FILE))
    except Exception: return {}
def save_tokens(t):
    os.makedirs(BGP_DIR, exist_ok=True); json.dump(t, open(TOKENS_FILE, "w"))

def _is_ip(h):
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", h)) or ":" in h   # v4 或 v6 都当 IP

def _sub_https():
    """订阅能否走 HTTPS：host 是域名（非 IP）且 acme 真证书就绪。自签/IP 仍用 HTTP。"""
    h = _host()
    return (not _is_ip(h)) and os.path.exists(ACME_CRT) and os.path.exists(ACME_KEY)

def sub_url(ext):
    t = load_tokens().get(ext)
    if not t:
        return "(未生成)"
    scheme = "https" if _sub_https() else "http"
    return f"{scheme}://{_host()}:{sub_port()}/{t}.{ext}"

def sub_urls_text():
    ff = {"yaml": CFG_FILE, "json": SBOX_FILE, "conf": SR_FILE}; toks = load_tokens()
    return "\n".join(f"  {SUB_EXTS[e]:<12} {sub_url(e)}"
                     for e in ("yaml", "json", "conf") if os.path.exists(ff[e]) and toks.get(e))

def links_url():
    """本机节点链接（.links）地址：粘到别的机器「聚合节点链接」里做多机汇总。"""
    t = load_tokens().get("links")
    if not t:
        return ""
    scheme = "https" if _sub_https() else "http"
    return f"{scheme}://{_host()}:{sub_port()}/{t}.links"

def rotate_token_ext(ext):
    t = load_tokens(); t[ext] = secrets.token_urlsafe(12); save_tokens(t); serve_sub()

def rotate_links_token():
    """换 .links token：旧地址立即失效（防泄露）。聚合了本机的主机需重新复制新地址。"""
    t = load_tokens(); t["links"] = secrets.token_urlsafe(12); save_tokens(t); serve_sub()

# 订阅托管小服务：有 cert/key 参数就起 HTTPS，否则明文 HTTP（用法：port dir [cert key]）
# 订阅托管小服务：静态发订阅文件；另带一个 /gh/ GitHub 中转（规则/图标走本机、不依赖 gh-proxy）。
# 中转和订阅共用同一端口（客户端本就从这端口拉订阅，无需额外放行）。多线程，中转不卡订阅。
# 安全：中转只白名单 GitHub 几个主机——绝不做成"谁都能拿它转发任意网址"的开放代理。
_SUB_SERVER_PY = r'''import http.server, ssl, sys, urllib.request, urllib.parse
port = int(sys.argv[1]); directory = sys.argv[2]
tokenfile = sys.argv[3]
cert = sys.argv[4] if len(sys.argv) > 4 else ''
key  = sys.argv[5] if len(sys.argv) > 5 else ''
ALLOW = ('raw.githubusercontent.com', 'objects.githubusercontent.com', 'github.com', 'codeload.github.com',
         'gist.github.com', 'gist.githubusercontent.com')
class H(http.server.SimpleHTTPRequestHandler):
    timeout = 30                     # 读请求超时：卡住的客户端不会永久占着线程
    def __init__(self, *a, **k):
        super().__init__(*a, directory=directory, **k)
    def log_message(self, *a):
        pass
    def do_GET(self):
        i = self.path.find('/gh/')       # 路径形如 /<token>/gh/<github-url>
        if i >= 0:
            self._relay(self.path[1:i], self.path[i + 4:]); return
        super().do_GET()
    def _relay(self, tok, target):
        ftok = ''
        try:                             # 每次请求读 token 文件：刷新后旧 token 立即失效，无需重启
            with open(tokenfile) as f:
                ftok = f.read().strip()
        except OSError:
            pass
        if ftok and tok != ftok:         # token 不对 → 拒（防别人蹭）
            self.send_error(403); return
        if not target.startswith(('http://', 'https://')):
            target = 'https://' + target.lstrip('/')
        try:
            host = urllib.parse.urlsplit(target).hostname or ''
        except Exception:
            host = ''
        if host not in ALLOW:            # 非 GitHub 主机一律拒，杜绝开放代理滥用
            self.send_error(403); return
        try:
            req = urllib.request.Request(target, headers={'User-Agent': 'xy-sub'})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(); ct = r.headers.get('Content-Type', 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers(); self.wfile.write(data)
        except Exception:
            self.send_error(502)
class S(http.server.ThreadingHTTPServer):
    """TLS 握手必须在工作线程里做。若像以前那样 wrap 监听 socket，accept() 会在主循环
       内完成握手——只要有客户端连上却不握手（mihomo 并发拉几十个规则集时很常见），
       主循环就被堵死、后续订阅/中转全部连不上（表现为 active 但 EOF/超时、队列堆积）。"""
    daemon_threads = True
    request_queue_size = 128          # 默认 5 太小，并发拉规则时瞬间排满
    ctx = None
    def process_request_thread(self, request, client_address):
        if self.ctx is not None:
            try:
                request.settimeout(20)                    # 握手不能无限等
                request = self.ctx.wrap_socket(request, server_side=True)
            except Exception:
                try: self.shutdown_request(request)
                except Exception: pass
                return
        super().process_request_thread(request, client_address)
httpd = S(('0.0.0.0', port), H)
if cert and key:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.ctx = ctx
httpd.serve_forever()
'''

def serve_sub(reset=False):
    """SUB_DIR 放 <token>.<ext> 软链指向各格式配置文件；每格式独立 token（存 TOKENS_FILE）。
       reset=True 换全部 token + 换新随机端口；否则复用已有、只给新格式补 token、端口不动。"""
    os.makedirs(SUB_DIR, exist_ok=True)
    if reset:
        renew_sub_port()                        # 重装换节点：端口随 token 一起换新
    toks = {} if reset else load_tokens()
    for f in os.listdir(SUB_DIR):                       # 清旧软链（含 .links）
        if f.rsplit(".", 1)[-1] in SUB_EXTS or f.endswith(".links"):
            os.remove(os.path.join(SUB_DIR, f))
    for ext, target in (("yaml", CFG_FILE), ("json", SBOX_FILE), ("conf", SR_FILE)):
        if os.path.exists(target):
            toks.setdefault(ext, secrets.token_urlsafe(12))
            os.symlink(target, f"{SUB_DIR}/{toks[ext]}.{ext}")
    # 节点链接端点（.links）：纯本机分享链接，供别的机器聚合拉取；token 保护。
    # 重装换节点(reset)时和订阅一样换 .links token，旧地址失效（防泄露）；平时保持不变。
    local = read_saved_links()
    if local:
        open(LINKS_FILE, "w").write("\n".join(local) + "\n")
        if reset:
            lt = secrets.token_urlsafe(12)
        else:
            lt = toks.get("links") or load_tokens().get("links") or secrets.token_urlsafe(12)
        toks["links"] = lt
        os.symlink(LINKS_FILE, f"{SUB_DIR}/{lt}.links")
    save_tokens(toks)
    open(f"{SUB_DIR}/index.html", "w").write("")        # 有 index 就不列目录，token 不外泄
    # 托管小服务：有域名+真证书就用 TLS（https 订阅），否则明文（自签 host 用 https 客户端会拒）
    open(SUB_SERVER, "w").write(_SUB_SERVER_PY)
    https = _sub_https()
    args = f"{sub_port()} {SUB_DIR} {GHRELAY_TOKEN_FILE}" + (f" {ACME_CRT} {ACME_KEY}" if https else "")
    svc = (f"[Unit]\nAfter=network.target\n[Service]\n"
           f"ExecStart=/usr/bin/python3 {SUB_SERVER} {args}\n"
           f"Restart=on-failure\nRestartSec=3\n[Install]\nWantedBy=multi-user.target\n")
    open("/etc/systemd/system/xy-sub.service", "w").write(svc)
    sh("systemctl daemon-reload")
    sh("systemctl enable xy-sub", check=False)
    sh("systemctl restart xy-sub")

# --- 协议归类：mihomo 节点 dict → 统一协议键（三格式共用）---
def proto_key(d):
    t = d.get("type")
    if t in ("hysteria2", "tuic", "anytls", "trojan"):
        return t if t != "hysteria2" else "hy2"
    if t == "vmess":
        return "vmess-httpupgrade" if d.get("ws-opts", {}).get("v2ray-http-upgrade") else "vmess-ws"
    if t == "vless":
        if d.get("network") == "xhttp":
            return "vless-xhttp"                          # xray 专属，sing-box 不支持 → 不映射
        if d.get("reality-opts"):
            return "reality-grpc" if d.get("network") == "grpc" else "reality-vision"
        if d.get("flow"):
            return "vless-vision"
        return "vless-ws"
    return None

# 协议键 → sing-box 模板里的节点 tag（模板节点名固定，只换连接参数）
PROTO_TO_SBTAG = {
    "vless-vision": "🇺🇲 VLESS_TCP/TLS_Vision", "vless-ws": "🇺🇲 VLESS_WS",
    "vmess-ws": "🇺🇲 VMess_WS", "trojan": "🇺🇲 Trojan_TCP", "hy2": "🇺🇲 Hysteria2_TLS",
    "reality-vision": "🇺🇲 VLESS_Reality_Vision", "reality-grpc": "🇺🇲 VLESS_Reality_gPRC",
    "tuic": "🇺🇲 singbox_tuic", "anytls": "🇺🇲 AnyTLS", "vmess-httpupgrade": "🇺🇲 VMess_HTTPUpgrade_TLS",
}

def mihomo_to_sb_outbound(key, d):
    """mihomo 节点 dict → 完整的 sing-box 出站对象（服务器端现生成，不依赖模板里的固定参数）。
       不支持的类型(如 xhttp)返回 None，由调用方跳过。"""
    if key not in PROTO_TO_SBTAG:
        return None                                      # xhttp 等 → 不写进 sing-box
    tag = d.get("name", "") or key                       # 统一用节点池名称（含服务器端前缀）
    srv = d["server"]; sni = d.get("servername") or d.get("sni") or srv
    insec = bool(d.get("skip-cert-verify"))
    utls = {"enabled": True, "fingerprint": "chrome"}
    t = d.get("type")
    if t == "vless":
        ob = {"tag": tag, "type": "vless", "server": srv, "server_port": int(d["port"]),
              "uuid": d["uuid"], "packet_encoding": "xudp",
              "tls": {"enabled": True, "server_name": sni, "insecure": insec, "utls": utls}}
        if d.get("flow"): ob["flow"] = d["flow"]
        if d.get("reality-opts"):
            ob["tls"]["reality"] = {"enabled": True,
                                    "public_key": d["reality-opts"].get("public-key", ""),
                                    "short_id": d["reality-opts"].get("short-id", "")}
        if d.get("network") == "ws":
            ob["transport"] = {"type": "ws", "path": d["ws-opts"].get("path", "/"),
                               "headers": {"Host": d["ws-opts"].get("headers", {}).get("Host", sni)}}
            if d.get("smux"): ob["multiplex"] = dict(_SB_MUX)
        elif d.get("network") == "grpc":
            ob["transport"] = {"type": "grpc", "service_name": d.get("grpc-opts", {}).get("grpc-service-name", "")}
        return ob
    if t == "vmess":
        net = "httpupgrade" if key == "vmess-httpupgrade" else "ws"
        ob = {"tag": tag, "type": "vmess", "server": srv, "server_port": int(d["port"]),
              "uuid": d["uuid"], "security": "none", "alter_id": 0,
              "tls": {"enabled": True, "server_name": sni, "insecure": insec, "utls": utls},
              "transport": {"type": net, "path": d["ws-opts"].get("path", "/"),
                            "headers": {"Host": d["ws-opts"].get("headers", {}).get("Host", sni)}}}
        if d.get("smux"): ob["multiplex"] = dict(_SB_MUX)
        return ob
    if t == "trojan":
        ob = {"tag": tag, "type": "trojan", "server": srv, "server_port": int(d["port"]),
              "password": d["password"],
              "tls": {"enabled": True, "server_name": sni, "insecure": insec,
                      "alpn": ["http/1.1"], "utls": utls}}
        if d.get("network") == "ws":                  # trojan+ws（CDN）：补 ws 传输
            ob["transport"] = {"type": "ws", "path": d["ws-opts"].get("path", "/"),
                               "headers": {"Host": d["ws-opts"].get("headers", {}).get("Host", sni)}}
        return ob
    if t == "hysteria2":
        ob = {"tag": tag, "type": "hysteria2", "server": srv, "password": d["password"],
              "tls": {"enabled": True, "server_name": sni, "insecure": insec, "alpn": ["h3"]}}
        if d.get("obfs") == "salamander" and d.get("obfs-password"):
            ob["obfs"] = {"type": "salamander", "password": d["obfs-password"]}
        if d.get("ports"):
            ob["server_ports"] = [d["ports"].replace("-", ":")]; ob["hop_interval"] = "30s"
        else:
            ob["server_port"] = int(d["port"])
        return ob
    if t == "tuic":
        return {"tag": tag, "type": "tuic", "server": srv, "server_port": int(d["port"]),
                "uuid": d["uuid"], "password": d["password"], "congestion_control": "bbr",
                "tls": {"enabled": True, "server_name": sni, "insecure": insec, "alpn": ["h3"]}}
    if t == "anytls":
        return {"tag": tag, "type": "anytls", "server": srv, "server_port": int(d["port"]),
                "password": d["password"],
                "tls": {"enabled": True, "server_name": sni, "insecure": insec,
                        "alpn": ["h2", "http/1.1"], "utls": utls}}
    return None

def _sb_has_container(d):
    """dict 里有嵌套 dict、或有『含 dict 的数组』→ 该展开；否则整体压一行。"""
    for v in d.values():
        if isinstance(v, dict):
            return True
        if isinstance(v, list) and any(isinstance(e, dict) for e in v):
            return True
    return False

def sb_dumps(v, ind=0):
    """sing-box 手写风格：容器(root/dns/route 等)展开缩进；数组每个元素各占一行、
       且元素对象整体压成一行（节点/规则/策略组一行一个）。"""
    pad, pad1 = "  " * ind, "  " * (ind + 1)
    if isinstance(v, dict):
        if not _sb_has_container(v):
            return json.dumps(v, ensure_ascii=False)                 # 叶子对象一行
        parts = [f'{pad1}{json.dumps(k, ensure_ascii=False)}: {sb_dumps(val, ind + 1)}'
                 for k, val in v.items()]
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(v, list):
        if not any(isinstance(e, (dict, list)) for e in v):
            return json.dumps(v, ensure_ascii=False)                 # 纯标量数组内联
        parts = [f'{pad1}{json.dumps(e, ensure_ascii=False)}' for e in v]  # 每元素一行
        return "[\n" + ",\n".join(parts) + "\n" + pad + "]"
    return json.dumps(v, ensure_ascii=False)

# ============================================================================ 国家随机分组
# 扫模板注入的节点名，按国家自动建 url-test 随机组（命中≥阈值才建）；搬自 Mihomo-fx 复写脚本。
# 三格式共用同一套检测；各格式生成器按自己语法在 _GRP_ / _NAM_ 锚点渲染。
COUNTRY_THRESHOLD = 2               # 某国节点数 < 该值则不建该组（1=有就建, 2=至少2个）
OTHER_GROUP = "🎲其他随机"          # 未归入任何国家组的漏网节点收进这里（有漏网才建）
# sing-box 出站里"是节点"的类型（用来从模板抽用户手写的静态节点，排除 selector/urltest/direct 等分组）
_SB_NODE_TYPES = {"vless", "vmess", "trojan", "hysteria2", "hysteria", "tuic", "anytls",
                  "shadowsocks", "shadowtls", "socks", "http", "naive", "ssh", "wireguard"}
def _cc(pat):
    """把国家表里的 `\bXX\b` 放宽成「右边也可以直接跟小写字母或数字」。

       为什么要放宽：节点名现在是 `前缀·协议` 拼的，右边界没问题；但【老版本装的】
       和【外部聚合进来的】节点可能是 `USAvless-ws` 这种连在一起的写法，
       `\bUSA\b` 右边界不成立就匹配不上，分组建不出来。放宽后这些老节点
       下次「更新配置」即可自动归组，不用重装。

       只放宽到小写字母/数字，跟大写字母仍然不算（`USAGE` 不会被当成 US 节点）。
       只用 RE2 支持的语法——mihomo 的 filter 是 Go 正则，不支持环视。"""
    return re.sub(r"\\b([A-Z]+)\\b",
                  lambda m: r"\b" + m.group(1) + r"(?:[a-z0-9]|\b)", pat)

COUNTRY_GROUPS = [                  # [组名, 匹配正则]，顺序即面板展示顺序
    ("🇭🇰香港随机",   r"🇭🇰|\bHK\b|Hong|hong|香港|深港|沪港|京港"),
    ("🇹🇼台湾随机",   r"🇹🇼|\bTW\b|\bTWN\b|Taiwan|Taipei|台湾|台灣|台北"),
    ("🇯🇵日本随机",   r"🇯🇵|\bJP\b|Japan|japan|Tokyo|东京|大阪|日本"),
    ("🇸🇬新加坡随机", r"🇸🇬|\bSG\b|Singapore|singapore|新加坡|狮城"),
    ("🇰🇷韩国随机",   r"🇰🇷|\bKR\b|Korea|korea|韩国|首尔"),
    ("🇺🇸美国随机",   r"🇺🇸|🇺🇲|\bUS\b|\bUSA\b|America|美国|洛杉矶|纽约|西雅图|圣何塞|硅谷"),
    ("🇬🇧英国随机",   r"🇬🇧|\bUK\b|\bGB\b|England|Britain|London|英国|伦敦"),
    ("🇩🇪德国随机",   r"🇩🇪|\bDE\b|Germany|German|Frankfurt|德国|法兰克福"),
    ("🇳🇱荷兰随机",   r"🇳🇱|\bNL\b|Netherlands|Holland|Amsterdam|荷兰|阿姆斯特丹"),
    ("🇫🇷法国随机",   r"🇫🇷|\bFR\b|France|Paris|法国|巴黎"),
    ("🇨🇦加拿大随机",  r"🇨🇦|\bCA\b|Canada|加拿大|多伦多"),
    ("🇦🇺澳洲随机",    r"🇦🇺|\bAU\b|Australia|Sydney|澳大利亚|悉尼"),
    ("🇷🇺俄罗斯随机",  r"🇷🇺|\bRU\b|Russia|Moscow|俄罗斯|莫斯科"),
    ("🇮🇳印度随机",    r"🇮🇳|India|india|Mumbai|Delhi|Bangalore|Bengaluru|Chennai|印度|孟买|新德里|班加罗尔"),
    ("🇻🇳越南随机",    r"🇻🇳|Vietnam|vietnam|Hanoi|Saigon|越南|河内|胡志明|西贡"),
    ("🇲🇾马来西亚随机", r"🇲🇾|Malaysia|malaysia|Kuala|马来|吉隆坡"),
    ("🇹🇭泰国随机",    r"🇹🇭|\bTH\b|Thailand|thailand|Bangkok|泰国|曼谷"),
    ("🇮🇩印尼随机",    r"🇮🇩|Indonesia|indonesia|Jakarta|印尼|印度尼西亚|雅加达"),
    ("🇵🇭菲律宾随机",  r"🇵🇭|\bPH\b|Philippines|philippines|Manila|菲律宾|马尼拉"),
    ("🇹🇷土耳其随机",  r"🇹🇷|Turkey|turkey|Türkiye|Istanbul|土耳其|伊斯坦布尔"),
    ("🇦🇪阿联酋随机",  r"🇦🇪|\bUAE\b|Emirates|Dubai|阿联酋|迪拜|阿布扎比"),
    ("🇧🇷巴西随机",    r"🇧🇷|\bBR\b|Brazil|brazil|Brasil|巴西|圣保罗"),
    ("🇦🇷阿根廷随机",  r"🇦🇷|\bAR\b|Argentina|argentina|阿根廷|布宜诺斯艾利斯"),
]
COUNTRY_GROUPS = [(g, _cc(p)) for g, p in COUNTRY_GROUPS]    # 统一放宽右边界（见 _cc）

def _norm_us_flag(s):
    return (s or "").replace("\U0001F1FA\U0001F1F2", "\U0001F1FA\U0001F1F8")   # 🇺🇲→🇺🇸

def detect_countries(names):
    """names: 节点名列表。返回 [(组名, 正则, [命中节点名])]，仅含命中数≥阈值的国家（按表序）。"""
    norm = [_norm_us_flag(n) for n in names]
    out = []
    for gname, pat in COUNTRY_GROUPS:
        rx = re.compile(pat)
        members = [n for n in norm if rx.search(n)]
        if len(members) >= COUNTRY_THRESHOLD:
            out.append((gname, pat, members))
    return out

def other_members(names, present):
    """不属于任何已建国家组的漏网节点名（present 为 detect_countries 的返回）。"""
    norm = [_norm_us_flag(n) for n in names]
    rxs = [re.compile(p) for _, p, _ in present]
    return [n for n in norm if not any(rx.search(n) for rx in rxs)]

def _sb_country_groups(tags, existing=()):
    """sing-box 国家随机组：无 filter，按正则算好每国成员显式列入 urltest。
       返回 (国家组对象列表, 组名列表)。tag 用原始名（保留 🇺🇲 等，匹配用归一名）。

       existing：模板 outbounds 里已有的 tag。**同名的不再生成**，让模板里那个说了算——
       sing-box 遇到重复 tag 是硬失败(FATAL: duplicate outbound/endpoint tag)，整份配置
       解析不了。与 mihomo 侧同一套取舍：同名接管、不同名并存。"""
    present = detect_countries(tags)
    if not present:                                      # 没有任何国家 → 不建组（含"其他随机"），与 mihomo 一致
        return [], []
    existing = set(existing)
    objs, names = [], []
    mk = lambda tag, members: {"tag": tag, "type": "urltest", "outbounds": members,
                               "url": "https://www.gstatic.com/generate_204",
                               "interval": "120s", "tolerance": 30}
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in tags if rx.search(_norm_us_flag(t))]
        if gname not in existing:
            objs.append(mk(gname, members))
        names.append(gname)                              # 名字照样展开（指向模板里的同名出站）
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in tags if not any(r.search(_norm_us_flag(t)) for r in rxs)]
        if omembers:
            if OTHER_GROUP not in existing:
                objs.append(mk(OTHER_GROUP, omembers))
            names.append(OTHER_GROUP)
    return objs, names

def build_singbox_sub(nodes, tpl_url):
    """对象级替换锚点：_NOD_ 换节点对象、_GRP_ 换国家组、
       _NAM_ / <正则> / _NAM_:<正则> 展开策略组成员，再按手写风格序列化。"""
    cfg = json.loads(fetch_tpl(tpl_url))                      # 中转改写 + 老锚点名归一
    objs = []
    for key, d in nodes:
        try:
            ob = mihomo_to_sb_outbound(key, d)
            if ob: objs.append(ob)
        except Exception:
            pass
    if not objs:
        return
    # 国家检测/成员池 = 注入的订阅节点 + 用户手写进模板的静态节点（同为出站节点，按类型识别）
    static_tags = [o["tag"] for o in cfg.get("outbounds", [])
                   if isinstance(o, dict) and o.get("type") in _SB_NODE_TYPES and o.get("tag")]
    tags = [o["tag"] for o in objs] + static_tags
    # 模板里已有的 tag（含手写的策略组和静态节点）：同名的国家组不再生成，避免 duplicate tag
    existing_tags = {o["tag"] for o in cfg.get("outbounds", [])
                     if isinstance(o, dict) and o.get("tag")}
    country_objs, country_names = _sb_country_groups(tags, existing_tags)
    # 已知 tag 全集：模板自己定义的（含 DIRECT、策略组、静态节点）+ 注入的节点 + 自动建的国家组。
    # 用来分辨「字面 tag 名」和「裸正则」——两者都是普通字符串，没有语法上的区别，
    # 只能靠"是不是已经存在这么一个出站"来判。
    known_tags = set(existing_tags) | set(tags) | set(country_names)

    def expand_list(lst):
        """展开策略组成员。三种写法可拆解组合（前缀管建不建国家组，冒号后的正则管带不带节点）：

             _NAM_        只列国家组名，不带节点
             <正则>              只带命中的节点名，不列国家组      例：.*  或  🇺🇸|US
             _NAM_:<正则> 两个都要：国家组名 + 命中的节点名

           裸正则怎么跟字面 tag 名区分：先查 known_tags，是已知出站就当字面量，
           否则才按正则去匹配节点名。所以 "DIRECT"、"🎯直连" 这些照常原样保留，
           而 ".*" 这种不存在的 tag 才会被当成正则。

           正则一条都没匹配上时【保留字面量】而不是丢掉：这样模板里把 tag 名写错一个字，
           sing-box 会照常报 "outbound not found"，一眼能看出问题；静默丢掉反而查不出来。"""
        out = []
        for x in lst:
            if x == A_NAMES:
                out += country_names                                 # 只国家组名
            elif isinstance(x, str) and x.startswith(A_NAMES + ":"):
                out += country_names                                 # 国家组名 + 命中节点名
                out += [t for t in tags if re.search(x[len(A_NAMES) + 1:], t)]
            elif isinstance(x, str) and x not in known_tags:
                try:
                    hit = [t for t in tags if re.search(x, t)]       # 裸正则 → 只匹配节点名
                except re.error:
                    hit = []                                         # 不是合法正则 → 当字面量
                if hit:
                    out += hit
                else:
                    print(f"  ⚠ sing-box 模板里的 {x!r} 既不是已有出站、当正则也匹配不到节点，原样保留")
                    out.append(x)
            else:
                out.append(x)
        return out
    new_ob = []
    for x in cfg.get("outbounds", []):
        if x == A_NODES:
            new_ob += objs                                           # 节点锚点 → 节点对象
        elif x == A_GROUPS:
            new_ob += country_objs                                   # 分组锚点 → 国家 urltest 组
        elif isinstance(x, dict) and isinstance(x.get("outbounds"), list):
            x["outbounds"] = expand_list(x["outbounds"]); new_ob.append(x)
        else:
            new_ob.append(x)
    cfg["outbounds"] = new_ob
    _sb_direct_ip(cfg, _direct_targets(nodes))                    # 各 VPS IP 直连（走紧凑序列化，不破坏格式）
    _sb_selfdns(cfg, _selfdns_doh())                              # 开关开启：dns.final 换成本机自建 DoH
    open(SBOX_FILE, "w").write(sb_dumps(cfg))

# --- Shadowrocket [Proxy] 行：从 mihomo 参数转（名称带国旗前缀让分组正则命中）---
def shadowrocket_line(name, d):
    t = d.get("type"); srv = d["server"]; port = d.get("port")
    sni = d.get("servername") or d.get("sni") or srv
    scv = "1" if d.get("skip-cert-verify") else "0"
    if d.get("network") == "xhttp":
        return None                                   # 小火箭不支持 xhttp → 跳过（不写进小火箭订阅，单链接仍在）
    if t == "vless":
        p = [f"{name} = vless", srv, str(port), f"username={d['uuid']}", "tls=1", f"sni={sni}",
             f"skip-cert-verify={scv}", "tfo=1"]
        if d.get("flow"): p.append(f"flow={d['flow']}")
        if d.get("reality-opts"):
            p += [f"public-key={d['reality-opts'].get('public-key','')}",
                  f"short-id={d['reality-opts'].get('short-id','')}", "fp=chrome"]
        if d.get("network") == "ws":
            p += ["obfs=websocket", f"obfs-uri={d['ws-opts'].get('path','/')}",
                  f"obfs-host={d['ws-opts'].get('headers',{}).get('Host',sni)}"]
        elif d.get("network") == "grpc":
            p += ["transport=grpc", f"grpc-service-name={d.get('grpc-opts',{}).get('grpc-service-name','')}"]
        return ",".join(p)
    if t == "vmess":
        p = [f"{name} = vmess", srv, str(port), f"username={d['uuid']}", "tls=1", f"sni={sni}",
             "alterId=0", f"skip-cert-verify={scv}", "tfo=1",
             "obfs=websocket", f"obfs-uri={d.get('ws-opts',{}).get('path','/')}",
             f"obfs-host={d.get('ws-opts',{}).get('headers',{}).get('Host',sni)}"]
        return ",".join(p)
    if t == "trojan":
        p = [f"{name} = trojan", srv, str(port), f"password={d['password']}",
             "tls=1", f"sni={sni}", f"skip-cert-verify={scv}", "tfo=1"]
        if d.get("network") == "ws":                  # trojan+ws（CDN）
            p += ["obfs=websocket", f"obfs-uri={d.get('ws-opts',{}).get('path','/')}",
                  f"obfs-host={d.get('ws-opts',{}).get('headers',{}).get('Host',sni)}"]
        return ",".join(p)
    if t == "hysteria2":
        pt = port or (d["ports"].split("-")[0] if d.get("ports") else "")   # 跳跃时用起点端口
        p = [f"{name} = hysteria2", srv, str(pt), f"password={d['password']}", f"sni={sni}",
             f"skip-cert-verify={scv}"]
        if d.get("obfs") == "salamander" and d.get("obfs-password"):
            p += ["obfs=salamander", f"obfs-password={d['obfs-password']}"]
        if d.get("ports"): p.append(f"ports={d['ports']}")
        return ",".join(p)
    if t == "tuic":
        return ",".join([f"{name} = tuic", srv, str(port), f"uuid={d['uuid']}",
                         f"password={d['password']}", f"sni={sni}", "alpn=h3", f"skip-cert-verify={scv}"])
    if t == "anytls":
        return ",".join([f"{name} = anytls", srv, str(port), f"password={d['password']}",
                         "tls=1", f"sni={sni}", f"skip-cert-verify={scv}"])
    return None

def _sr_section_keys(tpl, section):
    """抽取 shadowrocket 模板某个段里所有 "名 = ..." 行的等号左边（跳过注释和带锚点的行）。"""
    m = re.search(r"(?ms)^\[" + re.escape(section) + r"\]\s*\n(.*?)(?=^\[|\Z)", tpl)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#") and "__XY" not in ln:
            out.append(ln.split("=", 1)[0].strip())
    return out

def _sr_static_names(tpl):
    """抽取 shadowrocket 模板 [Proxy] 段里用户手写的静态节点名（"名 = 协议,..." 行）。"""
    return _sr_section_keys(tpl, "Proxy")

def _sr_group_names(tpl):
    """抽取 shadowrocket 模板 [Proxy Group] 段里用户手写的策略组名。
       注意带 _NAM_ 的行会被跳过——那种行是模板自带的组、名字里不含国家组名，
       跳过它们不影响判断，反倒避免把锚点当成组名。"""
    return _sr_section_keys(tpl, "Proxy Group")

def _sr_country_groups(names_list, existing=()):
    """shadowrocket 国家随机组：显式列成员（不依赖 shadowrocket 正则引擎，稳）。
       返回 (组定义行文本, 拼进服务组的组名片段[前导逗号, 裸名])。

       existing：模板 [Proxy Group] 段里已有的组名。**同名的不再生成**，让模板里那个说了算。
       与 mihomo / sing-box 侧同一套取舍：同名接管、不同名并存。（mihomo 和 sing-box 的
       重名硬失败已实测确认；小火箭是 iOS 客户端没法在这里跑，但同段里出现两条同名定义
       本身就是歧义的，一并防住。）"""
    present = detect_countries(names_list)
    if not present:                                      # 没有任何国家 → 不建组（含"其他随机"），与 mihomo 一致
        return "", ""
    U = "url=http://www.gstatic.com/generate_204,interval=120,tolerance=30,timeout=5"
    existing = set(existing)
    lines, gnames = [], []
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in names_list if rx.search(_norm_us_flag(t))]
        if gname not in existing:
            lines.append(f"{gname} = url-test,{','.join(members)},{U}")
        gnames.append(gname)                             # 名字照样展开（指向模板里的同名组）
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in names_list if not any(r.search(_norm_us_flag(t)) for r in rxs)]
        if omembers:
            if OTHER_GROUP not in existing:
                lines.append(f"{OTHER_GROUP} = url-test,{','.join(omembers)},{U}")
            gnames.append(OTHER_GROUP)
    return "\n".join(lines), "".join(f",{g}" for g in gnames)

_SR_NAMES_RE = re.compile(r",?" + A_NAMES + r"(,?)")   # 锚点连同前后可有可无的逗号一起吃
def _sr_fill_names(tpl, frag):
    """展开 _NAM_（国家组名），但跳过该行已经写死的名字。

       逗号归模板管：模板写成 `...,♻️全部随机,_NAM_,policy-regex-filter=...`，
       锚点只负责填名字，不再自带前导逗号——这样模板本身就是一份读得通的成员列表。

       两种写法都认：锚点前的逗号可有可无（老模板、以及照老模板改的自定义模板是
       `♻️全部随机_NAM_` 粘在一起写的）。统一"把前面那个逗号吃掉、自己补
       回来"，新旧模板渲染结果一致。没名字可填时（国家组已在模板里写死，或压根没
       检出国家）则连同紧邻的一个逗号一起吃掉，免得留下 ",," 或行尾多一个逗号。

       为什么要按行去重：模板可以把常见国家组直接写进成员列表（现在的小火箭模板
       就是这么写的，配 policy-regex-filter 让客户端自己收拢节点）。这种组
       _sr_country_groups 不会重复生成，但仍会把名字放进 frag——它得指向模板里的
       同名组。这时全局 replace 会让同一行出现两遍同一个组。按行去重之后：
       模板写死的照旧，只有模板里没有的国家（英国、德国、🎲其他随机…）才追加进去。"""
    names = [n for n in frag.split(",") if n]
    def one(line):
        add = ",".join(n for n in names if n not in line)
        def rep(m):
            tail = m.group(1)                                  # 锚点后面原本有没有逗号
            if not add:                                        # 没东西可填：前后只留一个逗号
                return tail                                    # （行尾就一个都不留）
            return ("," if m.start() else "") + add + tail
        return _SR_NAMES_RE.sub(rep, line)
    return re.sub(r"(?m)^.*" + A_NAMES + r".*$", lambda m: one(m.group(0)), tpl)

def build_shadowrocket_sub(nodes, tpl_url):
    lines, names_list = [], []
    for key, d in nodes:
        name = d.get("name", "") or key                  # 统一用节点池名称（含服务器端前缀）
        try:
            s = shadowrocket_line(name, d)
            if s: lines.append(s); names_list.append(name)
        except Exception:
            pass
    if not lines:
        return
    tpl = fetch_tpl(tpl_url)                                 # 中转改写 + 老锚点名归一
    # 国家检测/成员池 = 注入节点 + 用户手写进模板 [Proxy] 段的静态节点（"名 = 协议,..." 行）
    static = _sr_static_names(tpl)
    # 模板 [Proxy Group] 段里已有的组名：同名的国家组不再生成，避免同段两条同名定义
    groups_txt, names_frag = _sr_country_groups(names_list + static, _sr_group_names(tpl))
    out = tpl
    out = _fill_block(out, A_NODES, "\n".join(lines))           # 块锚点整行替换，缩进容错
    out = _fill_block(out, A_GROUPS, groups_txt)
    out = _sr_fill_names(out, names_frag)                       # 行内锚点（按行去重）
    open(SR_FILE, "w").write(out)

# --- 三格式元数据：文件 / 作者模板 / 生成器；自定义模板存 CUSTPL_FILE ---
def _node_names(nodes):
    """从解析后的节点取名字列表，供国家检测用。"""
    return [d.get("name", "") or k for k, d in nodes]

_GNAME_RE = re.compile(r'''name:[ \t]*(?:"([^"]*)"|'([^']*)'|([^,}\n]+))''')

def tpl_group_names(tpl):
    """取模板 proxy-groups: 段里已经写好的组名（三种写法都认：双引号/单引号/不加引号）。
       只扫这一段：pg-anchor 里的 &GLOBAL_PROXIES 之类是组名【引用】不是【定义】，
       扫进来会把自动建组全误判成已存在。"""
    m = re.search(r'(?m)^proxy-groups:[ \t]*$', tpl)
    if not m:
        return set()
    seg = []
    for line in tpl[m.end():].splitlines():
        if line and not line[0].isspace():                     # 碰到下一个顶级键就停
            break
        seg.append(line)
    out = set()
    for a, b, c in _GNAME_RE.findall("\n".join(seg)):
        n = (a or b or c).strip().strip("\"'")
        if n:
            out.add(n)
    return out

def _mihomo_country(names, existing=()):
    """mihomo 国家随机组：返回 (组定义 yaml 行, 拼进🌍全球加速的组名片段)。无国家则空串。
       用 filter+include-all，客户端按正则自动收拢；filter 用单引号 YAML 串避免 \\b 被转义。

       existing：模板 proxy-groups 里已有的组名。**同名的不再生成**，让模板里那个说了算——
       mihomo 遇到重名组是硬失败(ProxyGroup xxx: duplicate group name)，整份配置加载不了。
       名字不同则照常生成，两个组并存互不干扰（想在模板里自定义样式就用同名覆盖，
       想额外多一个组就换个名字）。被跳过的组名仍拼进 🌍全球加速——那个引用会指向模板里
       的同名组，效果不变；即便你自己也把它写进了某个 proxies 列表，重复引用 mihomo 是允许的。"""
    present = detect_countries(names)
    if not present:
        return "", ""
    # hidden: true 让国家组不占面板卡片位（仍可在🌍全球加速里选到）；显式写在组上，
    # 覆盖 <<: *COUNTRY_COMMON，自定义模板不改锚点也生效。
    existing = set(existing)
    lines, gnames = [], []
    for gname, pat, _ in present:
        if gname not in existing:
            lines.append(f"  - {{name: \"{gname}\", <<: *COUNTRY_COMMON, filter: '{pat}', hidden: true}}")
        gnames.append(gname)
    if OTHER_GROUP and other_members(names, present):          # 有漏网节点才建"其他随机"
        if OTHER_GROUP not in existing:
            allpat = "|".join(p for _, p, _ in present)
            lines.append(f"  - {{name: \"{OTHER_GROUP}\", <<: *COUNTRY_COMMON, exclude-filter: '{allpat}', hidden: true}}")
        gnames.append(OTHER_GROUP)
    return "\n".join(lines), "".join(f', "{g}"' for g in gnames)

def _fill_block(tpl, anchor, block):
    """按整行替换独占一行的块锚点：连同该行的前导缩进一起换成 block（block 自带缩进）。
       这样锚点顶格或缩进都行——避免用户给 _NOD_/_GRP_ 缩两格导致 YAML 缩进错乱。

       前面的 "- " 也一起吃掉：mihomo 模板把锚点写成 `  - _NOD_` 这样的列表项，
       模板本身就是一份能解析的 YAML；不写 "- "（老模板、照老模板改的自定义模板）同样认。"""
    return re.sub(r"(?m)^[ \t]*(?:-[ \t]+)?" + re.escape(anchor) + r"[ \t]*$", lambda m: block, tpl)

_SELF_IP_CACHE = None
def _self_ip():
    """本机对外 IPv4：host 是 v4 就用它，否则(域名)取 public_ip()。用于「本机 IP 直连」规则。"""
    global _SELF_IP_CACHE
    if _SELF_IP_CACHE is None:
        h = _host()
        ip = h if re.match(r"^\d+\.\d+\.\d+\.\d+$", h or "") else ""
        if not ip:
            try: ip = public_ip()
            except Exception: ip = ""
        _SELF_IP_CACHE = ip or ""
    return _SELF_IP_CACHE

def _root_domain(host):
    """收敛到可注册域：node2.example.com → example.com。

       多机聚合时同一个注册域下会冒出一堆子域（各节点域名、订阅域名、AdGuard DoT 的
       <ClientID>.域名…），逐条写进规则里又长又重复，还全是同一个域。收敛成一条就够。

       没有引 Public Suffix List（这脚本只用标准库、不为这点事联网），用常见的二级
       公共后缀兜一下：xx.co.uk / xx.com.cn 这类取三段，其余取两段。判断不准的最坏
       结果只是规则比需要的宽一点，而这几条现在插在 MATCH 上一层，前面任何自己写的
       规则都盖得住它。"""
    parts = [p for p in (host or "").strip(".").split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    second = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne"}
    if len(parts[-1]) == 2 and parts[-2] in second:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def _direct_targets(nodes):
    """要直连的目标：本机 + 各节点服务器字面量。**有域名就用域名，没域名才落地 IP**——
       用域名时配置里不出现裸 IP（分享配置也不暴露真实 IP）；多机聚合后自动覆盖各成员机
       地址，挂着聚合代理管理任意一台，SSH/管理流量都走直连、不被重启核心掐断。
       域名一律先收敛到注册域再去重：多机聚合时十几个子域其实就一两个域，写全了没意义。
       返回 [(kind, val)]，kind 为 'ip' 或 'domain'。"""
    out, seen = [], set()
    def add(kind, val):
        val = (val or "").strip()
        if kind == "domain":
            val = _root_domain(val)
        if val and val not in seen:
            seen.add(val); out.append((kind, val))
    h = (_host() or "").strip()                              # 本机：sub.host 存的是域名或 IP
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        add("ip", h)
    elif re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", h):
        add("domain", h)
    else:
        add("ip", _self_ip())                               # 兜底：拿不到合法域名/IP 就用探测的公网 IP
    for _, d in nodes:                                       # 各节点服务器：域名或 IP 字面量都收
        s = _direct_server_of(d)
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", s):
            add("ip", s)
        elif re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", s):
            add("domain", s)
    return out

def _direct_server_of(d):
    """这个节点该写进直连规则的地址——不一定是 server 字段。

       CDN 套用做了优选之后，server 是 **Cloudflare 的共享任播地址**（或第三方优选域名），
       上面跑着海量别人的站点，而且根本不是你的机器：写成 DIRECT 既误伤第三方，
       又毫无用处——这些规则是给「你访问你自己的服务器」用的（SSH/管理流量不走代理），
       而客户端拨号到代理服务器本来就不经过规则引擎，不靠它。
       这种节点真正的归属是 Host 头里那个域名（你的真域名），直连该认它。

       判据：ws/xhttp 类且 Host 与 server 不同且是个域名。reality 排除在外——
       它的 servername/host 是借用的伪装站（如 s0.awsstatic.com），更不能当直连目标。"""
    s = str(d.get("server", "")).strip()
    if d.get("reality-opts"):
        return s
    net = d.get("network")
    if net == "ws":
        host = (d.get("ws-opts") or {}).get("headers", {}).get("Host", "")
    elif net == "xhttp":
        host = (d.get("xhttp-opts") or {}).get("host", "")
    else:
        return s
    host = str(host or "").strip()
    if host and host != s and re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", host):
        return host
    return s

def _direct_rule_text(kind, val):
    """mihomo / 小火箭通用规则文本：IP 走 IP-CIDR(+no-resolve)，域名走 DOMAIN-SUFFIX。

       域名用后缀而不是精确匹配，是因为节点域名底下会长出子域名：AdGuard 的 DoT 要带
       ClientID 时用的是 <ID>.节点域名（见 adguard-dns.py 菜单 8）。精确匹配漏掉它，
       那条查询就会掉进 MATCH 走代理——绕一圈再回到自己的 VPS，多一跳，代理挂了还可能
       连不上。

       后缀取的是注册域（见 _root_domain），所以这一条会罩住整个域名下的全部子域。
       代价是「想让某个子域走代理」不能靠这条规则让路——但这几条现在插在 MATCH 上一层，
       把自己的分流规则写在前面就能盖过它。"""
    return f"IP-CIDR,{val}/32,DIRECT,no-resolve" if kind == "ip" else f"DOMAIN-SUFFIX,{val},DIRECT"

def _ghrelay_token():
    """本机中转的 token（防别人蹭）；没有就生成一个存下来。存 BGP_DIR（不在 SUB_DIR，不会被静态服务下载）。"""
    try:
        t = open(GHRELAY_TOKEN_FILE).read().strip()
        if t:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(12)
    os.makedirs(BGP_DIR, exist_ok=True)
    open(GHRELAY_TOKEN_FILE, "w").write(t)
    return t

def _ghrelay_prefix():
    """本机 GitHub 中转前缀 https://域名:订阅端口/<token>/gh/ ——默认开（有域名+真证书且没被手动关时）。
       带 token 防别人蹭；返回 '' 则用模板里原本的 gh-proxy.com。中转与订阅同端口、只白名单 GitHub、非开放代理。
       没域名/自签时返回 ''：中转走 HTTPS 需要真证书，否则客户端拒连，只能退回 gh-proxy。"""
    if os.path.exists(GHRELAY_OFF):
        return ""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") or not _sub_https():
        return ""
    return f"https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/"

# 能被【自动识别】直接改走中转的 GitHub「原始文件」主机——规则集(.mrs/.yaml)和图标都在这几个上。
# 只认原始文件主机：github.com 的项目页、codeload 的 zip 不自动改（那些多半是说明链接/面板包，
# 误改没意义甚至变慢）；真要转它们，在模板里显式写代理前缀即可，下面第二步会处理。
_GH_RAW_HOSTS = ("raw.githubusercontent.com", "gist.githubusercontent.com",
                 "objects.githubusercontent.com")
_URL_TAIL = r'[^\s"\'<>,}\]\)]+'                     # URL 结尾：碰到引号/空白/YAML·JSON 分隔符就停
# 匹配「可有可无的代理前缀 + GitHub 原始文件链接」。前缀那段会把 https://gh-proxy.com/、
# https://ghproxy.net/ 这类镜像整段吃掉一起替换，避免出现「别人的镜像/自己的中转/…」套娃。
_GH_URL_RE = re.compile(
    r'(?:https?://' + _URL_TAIL + r'?/)?'
    r'(https://(?:' + "|".join(h.replace(".", r"\.") for h in _GH_RAW_HOSTS) + r')/' + _URL_TAIL + r')')

def _ghrelay_rewrite(text):
    """开启时把模板里的 GitHub 链接改走本机中转；关闭/无域名则原样返回。
       三种写法都认——因为很多人写模板不会加 gh-proxy 前缀，靠 raw 主机名识别才最可靠：
         ① https://raw.githubusercontent.com/…            裸链接，自动识别
         ② https://gh-proxy.com/https://raw.github…       老写法，前缀可有可无
         ③ https://随便哪个镜像/https://raw.github…       别人的镜像也整段换掉
       第二步再兜底处理非原始文件主机（github.com/codeload/gist.github.com）上显式写了前缀的。
       对已经是中转链接的文本重复执行不会套娃（前缀段会把旧的中转前缀一并吃掉再补上）。"""
    p = _ghrelay_prefix()
    if not p:
        return text
    text = _GH_URL_RE.sub(lambda m: p + m.group(1), text)
    return text.replace("https://gh-proxy.com/", p)


def fetch_tpl(url):
    """拉模板：GitHub 链接中转改写 + 老锚点名归一。三个格式的生成器都走这里。"""
    t = _ghrelay_rewrite(fetch_url(url))
    for old, new in _ANCHOR_OLD.items():
        t = t.replace(old, new)
    return t

def selfdns_clientid():
    """AdGuard ClientID：DoH 地址的末段（.../dns-query/<id>）。没有就生成一个存下来。
       存 BGP_DIR（不在 SUB_DIR，不会被静态服务下载），同 ghrelay token 的套路。
       用途：把这个 ID 填进 AdGuard「设置→DNS设置→访问设置→允许的客户端」，DoH 就只
       放行自己——不填白名单时它对谁都开放，公网上被扫到就成了别人的免费解析器。
       ID 只用小写字母和数字：AdGuard 要求 ClientID 是合法的域名标签。"""
    try:
        t = open(SELFDNS_CID_FILE).read().strip()
        if t:
            return t
    except OSError:
        pass
    t = "xy" + secrets.token_hex(6)                      # 14 字符，纯小写字母数字
    os.makedirs(BGP_DIR, exist_ok=True)
    open(SELFDNS_CID_FILE, "w").write(t)
    return t

def _selfdns_doh():
    """开关开启且本机是域名时，返回本机 AdGuard 的 DoH 地址
       https://域名:端口/dns-query/<ClientID>，否则返回 ''。
       DoH 端口从 AdGuardHome.yaml 的 port_https 读，读不到默认 10443。

       末段带 ClientID 是为了能在 AdGuard 侧只放行自己（见 selfdns_clientid）。
       手机流量 IP 天天变、没法按 IP 白名单，ClientID 与 IP 无关，正合适。
       向后兼容：AdGuard 未配置「允许的客户端」时对任意 ClientID 都放行，所以带上它
       不会让原本能用的配置失效——它只是把「可以收紧」这个选项交到你手里。"""
    if not os.path.exists(SELFDNS_FLAG):
        return ""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or ""):
        return ""
    port = 10443
    try:
        m = re.search(r'(?m)^\s*port_https:\s*(\d+)', open("/opt/AdGuardHome/AdGuardHome.yaml").read())
        if m and int(m.group(1)) > 0:
            port = int(m.group(1))
    except OSError:
        pass
    return f"https://{dom}:{port}/dns-query/{selfdns_clientid()}"

def _selfdns_prepend_list(tpl, prefix_re, item):
    """在匹配 prefix_re（以 [ 收尾）的单行列表最前插入 item；该列表已含 item 则不动（幂等）。"""
    def repl(m):
        head, rest = m.group(1), m.group(2)                # head=…[，rest=[ 之后到行尾
        return m.group(0) if item in rest else head + item + ", " + rest
    return re.sub(prefix_re, repl, tpl, count=1)

def _mihomo_selfdns(tpl, url):
    """mihomo：把自建 DoH 写进三处，都放列表最前当主用、原有留兜底（DoH 没通自动回落）：
       ① nameserver —— 默认解析用它
       ② global-dns 锚点 —— nameserver-policy 里所有走代理的域名组(*global-dns)共用它，
          改锚点一行即全部生效。cn-dns 不动：国内域名该用国内解析拿就近 CDN，
          用境外自建 DNS 解析反而慢。
       ③ proxy-server-nameserver —— 解析节点 server 域名用。是【加进列表一起竞速】
          而不是替换：这一栏 mihomo 是并发查询、谁先回用谁(dns/util.go 的 picker)，
          所以自建 DoH 通了就用它，挂了只是输掉比赛，剩下几条照常顶上，不会因为
          「DNS 建在自己要解析的节点上」而把整批节点的可用性绑死在一台机上。
          注：这一栏的查询 mihomo 强制直连、不走规则(config.go 里 respectRules 传
          false)，所以不存在「查DNS→走代理→要先解析节点域名」的套娃。

       地址由 _selfdns_doh() 按本机域名动态生成，不写死在模板里——每个人的自建
       域名都不一样。关掉开关时 url 为空、直接原样返回；模板每次生成都是实时重新
       拉取的，所以「不插入」本身就等于「已删除」，不需要单独的移除逻辑。"""
    if not url:
        return tpl
    q = f'"{url}"'
    tpl = _selfdns_prepend_list(tpl, r'(?m)^(\s*nameserver:\s*\[)(.*)$', q)
    tpl = _selfdns_prepend_list(tpl, r'(?m)^(\s*global-dns:\s*&global-dns\s*\[)(.*)$', q)
    # 用 :\s*\[ 收尾，不会误命中 proxy-server-nameserver-policy（那个 key 后面跟的是 -policy）
    tpl = _selfdns_prepend_list(tpl, r'(?m)^(\s*proxy-server-nameserver:\s*\[)(.*)$', q)
    return tpl

def _sr_selfdns(path, url):
    """Shadowrocket：把自建 DoH 加到 dns-server 最前（原有留兜底）。"""
    if not url:
        return
    try: tpl = open(path).read()
    except OSError: return
    if url in tpl:
        return
    new = re.sub(r'(?m)^(dns-server\s*=\s*)', lambda m: m.group(1) + url + ",", tpl, count=1)
    if new != tpl:
        open(path, "w").write(new)

def _mihomo_direct_ip(tpl, targets):
    """mihomo：把本机/各 VPS 直连插到 rules: 段的 MATCH 上一层，避免挂本机代理管理时
       SSH 被路由进代理。

       以前插在 rules: 最顶上，那几条就永远第一个命中：想给自己域名下的某个子域单独
       分流（比如 blog.域名 走代理）根本写不了，写在下面永远够不着。放到 MATCH 上一层
       之后，自己写的规则在前面都能盖过它，而没被任何规则命中的自建域名/IP 仍然被它兜住。

       模板里没写 MATCH 就退回原来的行为（插在 rules: 顶部）——位置不理想，
       总比整段规则丢掉强。"""
    if not targets or "rules:" not in tpl:
        return tpl
    rules = [r for r in (_direct_rule_text(k, v) for k, v in targets) if r not in tpl]
    if not rules:
        return tpl
    m = None
    for m in re.finditer(r"(?m)^([ \t]*)-[ \t]*MATCH\b.*$", tpl):
        pass                                   # 取最后一条 MATCH：兜底规则只可能在最末
    if m:
        indent = m.group(1)                    # 跟着 MATCH 那行的缩进走，别写死两个空格
        block = "".join(f"{indent}- {r}\n" for r in rules)
        return tpl[:m.start()] + block + tpl[m.start():]
    return re.sub(r"(?m)^rules:[ \t]*$",
                  "rules:\n" + "\n".join(f"  - {r}" for r in rules), tpl, count=1)

def _sb_direct_ip(cfg, targets):
    """sing-box：把直连规则插到 route.rules 最前（引用模板里的 🎯直连 出站）；
       就地改 cfg dict，交由 sb_dumps 按模板的紧凑风格序列化——不破坏格式。
       域名用 domain_suffix 而非 domain，理由同 _direct_rule_text：要覆盖节点域名
       底下的子域（AdGuard DoT 的 <ClientID>.节点域名）。"""
    if not targets:
        return
    route = cfg.get("route")
    if not isinstance(route, dict):
        return
    rules = route.get("rules")
    if not isinstance(rules, list):
        return
    tags = {o.get("tag") for o in cfg.get("outbounds", []) if isinstance(o, dict)}
    direct = "🎯直连" if "🎯直连" in tags else next((t for t in tags if t and "直连" in str(t)), "")
    if not direct:
        return
    add = []
    for kind, val in targets:
        rule = {"ip_cidr": [f"{val}/32"], "outbound": direct} if kind == "ip" \
               else {"domain_suffix": [val], "outbound": direct}
        if rule not in rules and rule not in add:
            add.append(rule)
    if add:
        # 追加到最后而不是插到最前：sing-box 的兜底走 route.final、没有 MATCH 这一条，
        # 排在末尾就等价于 mihomo 那边的「MATCH 上一层」——自己写的规则一律优先。
        route["rules"] = rules + add

# 自建 DoH 顶掉原兜底 DNS 时要清掉的【寻址/传输】字段：它们描述的是原来那台
# （8.8.8.8 / dns.google 之类），留一个都会让新地址握错手——尤其 tls.server_name，
# 留着就拿 dns.google 当 SNI 去连你自己的域名。除这些之外的字段一律保留，
# 别人自定义模板里加的 client_subnet、strategy 之类不该被我们抹掉。
_SB_DNS_ADDR_KEYS = ("type", "server", "server_port", "path", "tls", "headers",
                     "detour", "method", "interface_name", "inet4_range", "inet6_range")

def _sb_selfdns(cfg, url):
    """sing-box：把自建 DoH 顶掉【配置自己声明的兜底 DNS】（就地改 cfg dict）。

       认哪一台：读 dns.final 的值——那是配置自己写明"没被规则命中时用谁"的 tag，
       模板里恰好叫 "final"，但自定义模板可以叫别的名。所以按 dns.final 指向的 tag 找，
       不按字面量 "final"，更不按地址（8.8.8.8）——按地址锚定的话，别人把兜底换成
       9.9.9.9 就再也匹配不上，静默失效。dns.final 没写才退回找 tag=="final"。

       为什么改兜底而不是像 mihomo/小火箭那样"插在列表最前留兜底"：sing-box 的 DNS
       【没有列表回落】——一条 dns 规则只指一个 server tag，指到的那台挂了查询就直接
       失败，不会自动换下一台。所以这里不做假的"兜底"，就是把它换掉，并在开关菜单里
       把这个差别讲明白。

       为什么是兜底那台而不是 local/remote：local 是国内解析，换掉就拿不到就近 CDN
       （同 mihomo 那边 cn-dns 不动）；remote 是 clash_mode=全局 专用。兜底那台是
       没被任何规则命中时才用，动它副作用最小。

       detour 走【直连】：DoH 就架在自己节点域名上，走代理等于让解析依赖代理、
       代理又要先解析，套娃。domain_resolver 用模板里的 bootstrap(223.5.5.5 UDP)
       解析这个域名本身，同理。

       地址由 _selfdns_doh() 按本机域名动态生成，不写死在模板里。关掉开关时 url 为空、
       直接返回；模板每次都是实时重拉的，"不写入"本身就等于"已删除"。

       找不到就【说出来】而不是静默跳过：用了自定义模板的人得知道 sing-box 这份没写进去，
       否则会以为三个格式都带上了。"""
    if not url:
        return
    m = re.match(r"^https://([^/:]+)(?::(\d+))?(/.*)$", url)
    if not m:
        return
    host, port, path = m.group(1), m.group(2), m.group(3)
    dns = cfg.get("dns")
    if not isinstance(dns, dict) or not isinstance(dns.get("servers"), list):
        print("  ⚠ sing-box 模板里没有 dns.servers，自建 DNS 未写入这一份。")
        return
    servers = dns["servers"]
    want = dns.get("final") or "final"            # 配置自己声明的兜底 tag
    tags = {o.get("tag") for o in cfg.get("outbounds", []) if isinstance(o, dict)}
    direct = "🎯直连" if "🎯直连" in tags else next((t for t in tags if t and "直连" in str(t)), "")
    have_bootstrap = any(isinstance(o, dict) and o.get("tag") == "bootstrap" for o in servers)
    for i, o in enumerate(servers):
        if not isinstance(o, dict) or o.get("tag") != want:
            continue
        # 按 tag → 寻址 → 其它 → 解析器/出口 的顺序拼，跟模板里手写的排法一致，
        # 别人对着 diff 看时不会觉得整条被搅乱了
        keep = {k: v for k, v in o.items()
                if k not in _SB_DNS_ADDR_KEYS and k not in ("tag", "domain_resolver")}
        new = {"tag": o.get("tag"), "type": "https", "server": host}
        if port and port != "443":
            new["server_port"] = int(port)
        new["path"] = path
        new.update(keep)                       # 别人自定义加的字段原样带过来
        # 解析 DoH 域名本身：优先模板里的 bootstrap；模板没有就沿用原来那台的写法
        dr = "bootstrap" if have_bootstrap else o.get("domain_resolver")
        if dr:
            new["domain_resolver"] = dr
        if direct:
            new["detour"] = direct
        servers[i] = new
        return
    print(f"  ⚠ sing-box 模板的 dns.servers 里没有 tag=\"{want}\" 这台（dns.final 指向它），"
          f"自建 DNS 未写入这一份。")

def _sr_direct_ip(path, targets):
    """Shadowrocket：把本机/各 VPS 直连插到 [Rule] 段的 FINAL 上一层（理由同 mihomo）。
       没有 FINAL 就退回插在 [Rule] 顶部。"""
    if not targets:
        return
    try: tpl = open(path).read()
    except OSError: return
    if "[Rule]" not in tpl:
        return
    new = [r for r in (_direct_rule_text(k, v) for k, v in targets) if r not in tpl]
    if not new:
        return
    m = None
    for m in re.finditer(r"(?m)^[ \t]*FINAL[ \t]*,.*$", tpl):
        pass
    if m:
        open(path, "w").write(tpl[:m.start()] + "\n".join(new) + "\n" + tpl[m.start():])
    else:
        open(path, "w").write(tpl.replace("[Rule]", "[Rule]\n" + "\n".join(new), 1))

# 三种写法都认，按顺序试：新模板的 `,"_NAM_"`、单独成项的 `"_NAM_"`、
# 老模板粘在上一个成员后面的裸 `_NAM_`。注意裸锚点那条不能去吃前面的引号——
# `"🎯直连"_NAM_` 里那个引号是「直连」的收尾，吃掉就把上一个成员拆了。
_MH_NAMES_RE = re.compile(r',[ \t]*"%s"|"%s"|%s' % (A_NAMES, A_NAMES, A_NAMES))
def _mihomo_fill_names(tpl, frag):
    """把 _NAM_ 换成国家组名（frag 形如 ', "🇯🇵日本", "🇺🇸美国"'）。

       模板写成 `["🌍全球加速","♻️全部随机","🎯直连","_NAM_"]`——锚点是个正常的
       带引号列表项，逗号归模板管，这样模板本身就是一份能解析的 YAML。老模板、以及照
       老模板改的自定义模板是 `"🎯直连"_NAM_` 粘在一起写的，逗号藏在 frag 里，
       两种都认：把锚点连同前面的逗号和引号一起吃掉，再由 frag 自己补上逗号。

       没检出国家时 frag 为空，锚点连同那个逗号一起消失，列表不会多出一个空成员。"""
    return _MH_NAMES_RE.sub(lambda m: frag, tpl)

def gen_mihomo(ylines, nodes, tpl_url):
    tpl = fetch_tpl(tpl_url)                                 # 中转改写 + 老锚点名归一
    # 国家检测要看"全部节点"：注入的订阅节点 + 用户手写进模板的静态节点。
    # 静态节点名取 proxy-groups 段之前的 name:（策略组名在 proxy-groups 里，且不含国旗，不会误检）。
    static = re.findall(r'name:\s*"([^"]*)"', tpl.split("proxy-groups:")[0])
    groups_yaml, names_frag = _mihomo_country(_node_names(nodes) + static,
                                              tpl_group_names(tpl))   # 模板里已手写的同名组不再自动生成
    # 块锚点(独占一行)整行替换，缩进容错：_NOD_ 建节点 / _GRP_ 建国家组
    tpl = _fill_block(tpl, A_NODES, "\n".join(ylines))
    tpl = _fill_block(tpl, A_GROUPS, groups_yaml)
    tpl = _mihomo_fill_names(tpl, names_frag)                  # 行内锚点：填国家组名
    tpl = _mihomo_direct_ip(tpl, _direct_targets(nodes))       # 各 VPS IP 直连（防管理时 SSH 走代理）
    tpl = _mihomo_selfdns(tpl, _selfdns_doh())                 # 开关开启：把本机自建 DoH 加进 DNS（带兜底）
    open(CFG_FILE, "w").write(tpl)
def gen_singbox(ylines, nodes, tpl_url):
    build_singbox_sub(nodes, tpl_url)                        # 直连规则已在内部注入并紧凑序列化
def gen_shadow(ylines, nodes, tpl_url):
    build_shadowrocket_sub(nodes, tpl_url)
    _sr_direct_ip(SR_FILE, _direct_targets(nodes))
    _sr_selfdns(SR_FILE, _selfdns_doh())                       # 开关开启：把本机自建 DoH 加进 DNS（带兜底）

FMT = {
    "yaml": {"label": "mihomo",              "file": CFG_FILE,  "author": TEMPLATE_URL, "gen": gen_mihomo},
    "json": {"label": "sing-box",            "file": SBOX_FILE, "author": SBOX_TPL_URL, "gen": gen_singbox},
    "conf": {"label": "小火箭 Shadowrocket", "file": SR_FILE,   "author": SR_TPL_URL,   "gen": gen_shadow},
}

def _load_json(path):
    try: return json.load(open(path))
    except Exception: return {}
def load_custpl():   return _load_json(CUSTPL_FILE)
def set_custpl(ext, url):
    d = load_custpl(); d[ext] = url
    os.makedirs(BGP_DIR, exist_ok=True); json.dump(d, open(CUSTPL_FILE, "w"), ensure_ascii=False, indent=2)
def del_custpl(ext):
    """删掉某格式的自定义模板链接（改回作者模板）。没有则无操作。"""
    d = load_custpl()
    if ext in d:
        del d[ext]
        os.makedirs(BGP_DIR, exist_ok=True); json.dump(d, open(CUSTPL_FILE, "w"), ensure_ascii=False, indent=2)
def tpl_url_for(ext, custom=False):
    return (load_custpl().get(ext) if custom else "") or FMT[ext]["author"]

def load_tplsrc():   return _load_json(TPLSRC_FILE)
def set_tplsrc(ext, src):
    """记住该格式当前用的是哪套模板（"author"/"custom"）。三个格式各记各的。"""
    d = load_tplsrc(); d[ext] = src
    os.makedirs(BGP_DIR, exist_ok=True); json.dump(d, open(TPLSRC_FILE, "w"), ensure_ascii=False, indent=2)

def tpl_src_of(ext):
    """该格式当前该用哪套模板。没有记录时的兜底：有自定义链接就算自定义（沿用老装行为），
       否则作者模板——第一次用的人本来就是作者模板。"""
    src = load_tplsrc().get(ext)
    if src in ("author", "custom"):
        return src if (src == "author" or load_custpl().get(ext)) else "author"
    return "custom" if load_custpl().get(ext) else "author"

def tpl_url_current(ext):
    """按【用户当前的选择】取模板 URL —— 所有会重新生成订阅的功能都该走这里。

       为什么不能一律优先自定义：多路复用开关、GitHub 中转、自建 DNS 这些功能都会顺手
       重生成订阅。原先它们写死 custom=True，于是只要设过自定义链接，哪怕你后来在
       『更新配置』里明确选了作者模板，下次一按开关又被悄悄换回自定义模板——用户看到的
       配置跟他以为的不是同一份，而且没有任何提示。改成跟随最后一次的显式选择。
       三个格式互相独立：可以 mihomo 用自己的、sing-box 用作者的。"""
    if tpl_src_of(ext) == "custom":
        return load_custpl().get(ext) or FMT[ext]["author"]
    return FMT[ext]["author"]

# ============================================================================ 多机聚合
def load_peers():
    try: return [u for u in json.load(open(PEERS_FILE)) if u]
    except Exception: return []

def save_peers(peers):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(peers, open(PEERS_FILE, "w"), ensure_ascii=False, indent=2)

def _fetch_text(url, timeout=15):
    """普通拉取任意 URL 文本（成员机 .links 端点用；不走 github 镜像逻辑）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "xy-installer"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode(errors="ignore")

def peer_status(url):
    """探测成员链接可达性，返回 (是否通, 给人看的说明)。

       原来所有失败都压成一个「不通」，到底是机器没开、端口被墙、证书过期还是
       token 换了，全看不出来，只能一台台上去翻——分清楚这几类，一眼就知道去哪查。"""
    req = urllib.request.Request(url, headers={"User-Agent": "xy-installer"})
    try:
        return True, str(urllib.request.urlopen(req, timeout=8).status)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "404 地址失效（对方换过 token 或重装过，去它那儿重新复制）"
        return False, f"{e.code}"
    except urllib.error.URLError as e:
        r = getattr(e, "reason", e)
        t = str(r)
        if isinstance(r, socket.timeout) or "timed out" in t:
            return False, "超时（对方机器没开，或端口被墙/被防火墙拦）"
        if "certificate" in t.lower() or "SSL" in t or "ssl" in t:
            # 最常见的一种：acme 续期换了证书，但 xy-sub 进程还捏着旧的
            return False, "证书错误（多半是续期后 xy-sub 没重启：去那台机器 systemctl restart xy-sub）"
        if "Name or service not known" in t or "nodename nor servname" in t:
            return False, "域名解析不了"
        if "Connection refused" in t:
            return False, "连接被拒（对方 xy-sub 服务没在跑）"
        if "reset by peer" in t:
            return False, "连接被重置（端口多半被墙了）"
        return False, f"不通（{t[:40]}）"
    except Exception as e:
        return False, f"不通（{str(e)[:40]}）"

_NODE_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://",
                 "hysteria2://", "hy2://", "tuic://", "anytls://")

_SUP = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
        "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}
def _sup(n):
    return "".join(_SUP.get(c, c) for c in str(n))

def _link_name(link):
    """取分享链接的节点名（vmess 在 base64 JSON 的 ps，其余在 #fragment）。"""
    if link.startswith("vmess://"):
        try:
            b = link[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
            return j.get("ps", "")
        except Exception:
            return ""
    return urllib.parse.unquote(link.split("#", 1)[1]) if "#" in link else ""

def _link_rename(link, newname):
    if link.startswith("vmess://"):
        try:
            b = link[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        except Exception:
            return link
        j["ps"] = newname
        return vmess_link(j)
    return link.split("#", 1)[0] + "#" + newname

_TAG_MARKS = "¹²³⁴⁵⁶⁷⁸⁹⁰"          # build() 给双核心同名协议加的尾标，改名时要原样留着

def _match_proto(body):
    """从节点名尾部认出协议段，返回 (前面那截, 规范化后的协议名)；认不出返回 (None, "")。

       长的先试——不然 trojan 会把 vless-ws 抢了、reality-vision 会把
       vless-reality-vision 抢了。历史旧名(vless-reality-*)也在匹配集里，
       认出来之后顺手换成现在的短名：老节点点一次『更新配置』就跟着变短，不用重装。"""
    for proto in sorted(set(SB) | set(XRAY) | set(_PROTO_ALIASES), key=len, reverse=True):
        if body.endswith(proto):
            return body[:-len(proto)], _PROTO_ALIASES.get(proto, proto)
    return None, ""

def _sep_name(nm):
    """给老节点名补上「前缀·协议名」之间的分隔点；已经有了、或认不出协议名就原样返回。

       只动【显示名】：名字烤在分享链接的 #fragment(vmess 在 ps)里，纯粹给人看的，
       uuid / 端口 / 路径 / 服务一律不碰。老节点因此不用重装就能修好——
       重装会重新生成全部 uuid 和端口，为个分隔点不值当。"""
    nm = nm or ""
    i = nm.find("CDN·")
    if i == 0:
        return nm                                   # CDN 节点但没设前缀
    if i > 0:                                       # CDN 节点：CDN· 之前整段都是前缀
        pfx = nm[:i]
        return nm if pfx.endswith("·") else pfx + "·" + nm[i:]
    body, mark = nm, ""
    while body and body[-1] in _TAG_MARKS:          # 尾标先摘下来，改完再贴回去
        mark = body[-1] + mark; body = body[:-1]
    head, proto = _match_proto(body)
    if head is None:
        return nm                                   # 认不出协议名（自定义名字）→ 不动
    return _tag(head.rstrip("·"), proto + mark)

def _split_tag(nm):
    """把节点名拆成 (前缀, 协议段)。协议段含 CDN·/优选N 和双核心尾标(¹²)，
       前缀尾部的分隔点会去掉——重新拼的时候由 _tag() 统一加回去。
       拆不出协议段（用户自己起的名字）返回 (None, 原名)，调用方跳过它。"""
    nm = nm or ""
    i = nm.find("CDN·")
    if i >= 0:                                          # CDN 节点：CDN· 之前整段是前缀
        return nm[:i].rstrip("·"), nm[i:]
    body, mark = nm, ""
    while body and body[-1] in _TAG_MARKS:              # 尾标先摘下来，拼回去时带上
        mark = body[-1] + mark; body = body[:-1]
    head, proto = _match_proto(body)
    if head is None:
        return None, nm
    return head.rstrip("·"), proto + mark

_BAD_PREFIX_CHARS = set('#"\'\\\n\r\t')            # 会把分享链接/YAML/JSON 弄坏的字符

def rename_prefix():
    """更换节点名称前缀。只改【显示名】：uuid / 端口 / 路径 / 服务 / 证书一律不动，
       所以不用重装（重装会重新生成全部 uuid 和端口，为改个名字不值当）。
       只动本机节点——多机聚合时每台机器有自己的前缀，各改各的。"""
    links = read_saved_links()
    if not links:
        print("\n  还没有节点，请先『1.安装』。"); return
    cur = next((p for p, _ in (_split_tag(_link_name(u)) for u in links) if p), "")
    print("\n" + "=" * 60)
    print("  更换节点名称前缀")
    print("=" * 60)
    print(f"  当前前缀：{cur or '(无)'}      节点名 = 前缀·协议名")
    print("  例：🇯🇵 / 🇺🇸2 / 东京 / DMIT / 家宽")
    print("  ⓘ 前缀带国旗或国家代码(JP/US/HK…)，客户端才能自动分出「日本随机」这类分组。")
    new = _ask_free("  新前缀（回车取消；输 - 表示不要前缀）：").strip()
    if not new:
        print("  已取消。"); return
    new = "" if new == "-" else new
    # 明确回显一次：中文/emoji 在终端里删改容易看花，以实际收到的为准
    print(f"  收到前缀：{('「%s」' % new) if new else '(清空前缀)'}")
    if set(new) & _BAD_PREFIX_CHARS:
        print("  ✗ 前缀里不能有引号、# \\ 和换行/制表符（会把分享链接和配置弄坏）。已取消。"); return
    if len(new) > 24:
        print("  ✗ 前缀太长（超过 24 字符），手机上显示不下。已取消。"); return
    if new == cur:
        print("  和当前前缀一样，未改动。"); return
    plan, skipped = [], 0
    for u in links:
        old = _link_name(u)
        pfx, rest = _split_tag(old)
        if pfx is None:
            skipped += 1; continue                      # 认不出协议段的自定义名字，不碰
        plan.append((u, old, _tag(new, rest)))
    if not plan:
        print("  没有可改名的节点（名字都认不出协议段）。"); return
    print("-" * 60)
    for _, old, nn in plan:
        print(f"    {old}  →  {nn}")
    if skipped:
        print(f"  （另有 {skipped} 个名字认不出协议段，保持不动）")
    print("-" * 60)
    print("  只改显示名：uuid / 端口 / 路径 / 服务 / 证书一律不动，不用重装。")
    print("  ⚠ 客户端里手动选中过的节点会回到分组默认；自定义模板里写死了旧名字的要同步改。")
    if (_ask("  确认改名? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。"); return
    newmap = {u: nn for u, _, nn in plan}
    saved, tail = _node_file_parts()
    out = [_link_rename(u, newmap[u]) if u in newmap else u for u in saved]
    with open(NODE_FILE, "w") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
        if tail:
            f.write(tail if tail.startswith("\n") else "\n" + tail)
    nodes = _cdn_load()                                 # CDN 节点的 tag 也在 cdn.json 里存了一份
    if nodes:
        for n in nodes:
            pfx, rest = _split_tag(n.get("tag", ""))
            if pfx is not None:
                n["tag"] = _tag(new, rest)
        _cdn_save(nodes)
    try:                                                # 让重装和新增 CDN 节点沿用新前缀
        st = json.load(open(STATE_FILE)); st["prefix"] = new
        json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)
    except Exception as e:
        print("  ⚠ 订阅刷新失败（名字已改，可到配置菜单点『更新配置』重试）:", e); return
    print(f"  ✓ 已把 {len(plan)} 个节点的前缀改成「{new or '(无)'}」并刷新三格式订阅。")
    print("    客户端重拉一次订阅即生效；多机聚合的话，再去主机点一次『更新配置』重新汇总。")

def _dedup_names(links):
    """多机聚合后可能有同名节点（两台同前缀+同协议）→ mihomo/sing-box 不许重名。
       只给『撞名』的加小上标前缀区分（¹²³…），没撞的保持原样、干净。"""
    names = [_link_name(u) for u in links]
    from collections import Counter
    cnt = Counter(n for n in names if n)
    dup = {n for n, c in cnt.items() if c > 1}
    idx, out = {}, []
    for u, nm in zip(links, names):
        if nm in dup:
            idx[nm] = idx.get(nm, 0) + 1
            out.append(_link_rename(u, _sup(idx[nm]) + nm))       # ¹🇯🇵… ²🇯🇵…（旗子仍在，国家分组照常命中）
        else:
            out.append(u)
    return out

def aggregated_links(local=None):
    """本机链接 + 各成员机 .links（去重；拉不到的成员直接跳过）。
       只认真正的节点分享链接前缀，绝不把订阅 URL/注释误当节点。撞名的自动加 ¹²³ 区分。

       节点名在这里统一补上「前缀·协议」的分隔点（见 _sep_name）：名字是安装时烤进
       分享链接的，老节点仍是 🇺🇸2anytls 这种连写，光靠新装才带分隔点救不了它们。
       放在渲染这一步做，点一次『更新配置』就全好了，而且**聚合的成员机不用一台台去改**
       ——主机渲染时一并规范化。不回写 NODE_FILE：那是原始凭据，能不动就不动。"""
    links = list(local if local is not None else read_saved_links())
    seen = set(links)
    for u in load_peers():
        try:
            text = _fetch_text(u)
        except Exception:
            continue                                    # 不通就忽略这台
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(_NODE_SCHEMES) and s not in seen:
                seen.add(s); links.append(s)
    links = [_link_rename(u, _sep_name(_link_name(u))) for u in links]   # 统一补「前缀·协议」
    return _dedup_names(links)

def parse_nodes(all_links):
    ylines, nodes = [], []
    for u in all_links:
        try:
            d = link_to_proxy(u)
        except Exception:
            d = None
        if not d:
            continue
        ylines.append("  - {" + ", ".join(f"{k}: {_yfmt(v)}" for k, v in d.items()) + "}")
        k = proto_key(d)
        if k:
            nodes.append((k, d))
    return ylines, nodes

def build_subscription(all_links, new_token=False):
    """三格式各生成可编辑配置（有自定义模板就用自定义，否则作者模板），记住 host，托管。
       new_token=True（重装换节点/换域名）换全部 token 刷新订阅；否则保持各格式 token。"""
    all_links = aggregated_links(all_links)               # 合并成员机节点（多机聚合）
    ylines, nodes = parse_nodes(all_links)
    if not ylines:
        return False
    os.makedirs(BGP_DIR, exist_ok=True)
    for ext, meta in FMT.items():
        try:
            meta["gen"](ylines, nodes, tpl_url_current(ext))   # 跟随该格式当前选的模板
        except Exception as e:
            print(f"{meta['label']} 配置生成跳过:", e)
    open(HOST_FILE, "w").write(G["host"])              # 记住 host（域名优先）
    serve_sub(reset=new_token)
    return True

def detect_existing():
    """扫 systemd，找出跑 sing-box/xray 但不是本脚本装的服务（典型：mack-a/v2ray-agent）。
       返回 [(unit名, ExecStart路径)]。只认『别人家』的——本脚本自己的(指向 SB_BIN/XRAY_BIN)不算。"""
    found, d = [], "/etc/systemd/system"
    if not os.path.isdir(d):
        return found
    for f in os.listdir(d):
        if not f.endswith(".service"):
            continue
        try:
            txt = open(os.path.join(d, f)).read()
        except OSError:
            continue
        m = re.search(r"ExecStart=(\S+)", txt)
        if not m:
            continue
        exe = m.group(1)                                 # 只认『可执行文件本身是 sing-box/xray』的
        if not re.search(r"(sing-box|xray)$", exe):      # 避免把 xy-sub(python http.server) 误判
            continue
        if exe in (SB_BIN, XRAY_BIN):                    # 本脚本自己的核心，跳过
            continue
        found.append((f[:-8], exe))
    return found

def takeover_targets():
    """本机有哪些『别人搭建』的代理残留 → (systemd 单元, 目录)。"""
    return detect_existing(), [p for p in ("/etc/v2ray-agent",) if os.path.isdir(p)]

def takeover_confirm():
    """前台问一次要不要卸载接管；同意返回 True。没有残留也返回 True（无事可问）。

       为什么要单独拆出来：安装那半程是转后台跑的（见 _node_op_dispatch），后台没有
       控制终端，这个问题问不出来。所以在前台问掉，同意了就置 G['force']，
       后台跑到 takeover_cleanup 时直接动手、不再问。"""
    units, dirs = takeover_targets()
    if not units and not dirs:
        return True
    _print_takeover_targets(units, dirs)
    ans = _ask("卸载它们、由本脚本接管？删除后不可恢复。同意删除并继续安装[y]，放弃则不安装[N]: ")
    if ans.lower() not in ("y", "yes"):
        print("已放弃：保留现有安装，未做任何改动，退出。")
        return False
    G["force"] = "1"
    return True

def _print_takeover_targets(units, dirs):
    print("\n检测到本机已有『别人搭建』的代理安装：")
    for u, path in units:
        print(f"  - 服务 {u}.service  →  {path}")
    for p in dirs:
        print(f"  - 目录 {p}（疑似 mack-a / v2ray-agent）")

def takeover_cleanup():
    """检测到别人装的节点就卸掉、由本脚本接管。破坏性操作，需确认（--yes 免交互）。"""
    units, dirs = takeover_targets()
    if not units and not dirs:
        return
    _print_takeover_targets(units, dirs)
    if not G.get("force"):
        ans = _ask("卸载它们、由本脚本接管？删除后不可恢复。同意删除并继续安装[y]，放弃则不安装[N]: ")
        if ans.lower() not in ("y", "yes"):
            print("已放弃：保留现有安装，未做任何改动，退出。")
            raise SystemExit(0)
    for u, _ in units:
        sh(f"systemctl disable --now {u}", check=False)
        sh(f"rm -f /etc/systemd/system/{u}.service", check=False)
    sh("systemctl daemon-reload", check=False)
    for p in dirs:
        sh(f"rm -rf {p}", check=False)
    sh("rm -f /usr/bin/vasma /usr/bin/v2ray-agent", check=False)     # mack-a 管理命令软链
    # 清掉别人残留的端口跳跃 iptables 规则（mack-a 的“强制固定”DNAT，指向已死端口会顶掉 hy2）
    for line in sh("iptables -t nat -S PREROUTING", check=False).splitlines():
        if line.startswith("-A") and "portHopping" in line:
            sh("iptables -t nat " + line.replace("-A", "-D", 1), check=False)
    print("已清理，端口/服务名/端口跳跃规则已腾出。\n")

def run(sb_names, xr_names):
    ensure_deps()               # 先补齐 curl/socat/unzip/openssl 等，避免中途才炸
    if G.get("sni_split") and G["domain"]:
        G["nginx"] = "1"        # sni-split 自带 nginx(:80 webroot + stream 443)，提前置位让域名校验按 webroot 放宽
    check_domain_or_die()       # 域名不匹配就此停止——必须在 takeover 卸载别人之前
    takeover_cleanup()          # 有别人装的(mack-a 等)先踢掉再接管
    # 节点地址：有域名用域名，否则用公网 IP（域名需直连 A 记录指向本机）
    G["host"] = G["domain"] or public_ip()
    precheck_sni(sb_names, xr_names)     # reality 借用目标合格性预检（只警告不阻断）
    warn_selfsigned(sb_names, xr_names)  # 无域名自签的伪装弱点引导
    NGINX_WS.clear()
    NGINX_STREAM.clear()
    _USED_PORTS.clear()                  # 本次安装重新随机分配端口
    dup_protos = set(sb_names) & set(xr_names)   # 两核心同名协议 → 各自尾部加 ¹/² 区分

    # --- SNI 分流（--sni-split）：nginx stream+ssl_preread 让 reality 真正上 443，
    #     网站/ws 同在 443（按 SNI 不解密分流）。改 nginx 前先 preflight，
    #     探测不过就退回 reality-443 直连模式，绝不把现有能用的 443 改坏。
    if G.get("sni_split"):
        if not G["domain"]:
            print("  sni-split 需要域名，已忽略。"); G["sni_split"] = ""
        elif SNI_SPLIT_BACKEND not in sb_names:
            print(f"  sni-split 需选 sing-box {SNI_SPLIT_BACKEND}（放到 443 后面），已忽略。")
            G["sni_split"] = ""
        elif not sni_split_preflight():
            G["sni_split"] = ""; G["reality443"] = "1"    # 退回 reality-443 直连
        else:
            G["reality443"] = ""                          # sni-split 下 reality 走本地口，不直绑 443

    # reality 绑 443（直连模式，与 sni-split 互斥）：把主力 reality 协议钉在 443，
    # 主动探测回落到借用的真站，消掉「reality 在非 443 易被 GFW 封 IP」的风险。
    pin = {}
    r443, r443_core = pick_reality_443(sb_names, xr_names) if G.get("reality443") else ("", "")
    if r443:
        pin[r443] = 443
        if G.get("nginx"):
            # 保留 nginx 在 :80（acme webroot 续期照常），把 :443 让给 reality；
            # ws 类不再藏 443，改走自己端口的真证书。这样证书续期不会因为撤掉 nginx 而断。
            print(f"  {r443} → 443（抗封锁）；nginx 仅保留 :80 供证书续期，ws 类改走自己端口。")
        free_443_for_reality()                          # 让出 443（清掉旧 nginx 前置的 443 块）

    if G.get("nginx"):
        if not G["domain"]:
            print("nginx 前置需要域名，已忽略、改用自签+IP。"); G["nginx"] = ""
        else:
            ensure_nginx(); write_nginx_acme_stub()     # 先起 80 供 webroot 签证书
    all_links = []

    if sb_names:
        install_singbox()
        # pin 只给归属核心：同名协议两边都 pin 会一起抢 443
        ins, lks = build(SB, sb_names, pin if r443_core == "sb" else {},
                         dup=dup_protos, mark="¹"); all_links += lks
        if G.get("sni_split"):
            ensure_acme()                               # 确保证书就绪（本地 https server 要用）
            if not write_nginx_sni_split():             # 写 http(本地https)+stream(443分流)，失败已回滚
                print("  ⚠ sni-split 生效失败（nginx 已回滚到安全状态）。此时 reality 监听在本地、"
                      "暂不可达；请用 --no-sni-split 重装，或改用 reality-443 直连模式。")
        elif _nginx_front() and NGINX_WS:
            write_nginx_conf()                          # 收集完 ws 家族，写 443 伪装站+反代
        # reality 绑 443 时 nginx 只留 :80 acme stub（续期用），不写 443 块，443 归 reality
        cfg = f"{SB_DIR}/config.json"
        json.dump({"log": {"level": "info"}, "inbounds": ins,
                   "outbounds": [{"type": "direct"}]},
                  open(cfg, "w"), indent=2)
        write_service("sing-box", SB_BIN, cfg)

    if xr_names:
        install_xray()
        ins, lks = build(XRAY, xr_names, pin if r443_core == "xray" else {},
                         dup=dup_protos, mark="²"); all_links += lks
        cfg = f"{XRAY_DIR}/config.json"
        json.dump({"log": {"loglevel": "warning"}, "inbounds": ins,
                   "outbounds": [{"protocol": "freedom", "tag": "direct"},
                                 {"protocol": "blackhole", "tag": "block"}]},
                  open(cfg, "w"), indent=2)
        write_service("xray", XRAY_BIN, cfg)

    # 之前开过「屏蔽中国域名/IP」的话，重装重写了 config 会丢规则，这里自动重新注入
    if sb_names:
        try:
            cn_block_reapply()
        except Exception as e:
            print("CN 屏蔽重注入跳过（不影响节点）:", e)
    # BT/PT 屏蔽同理：重装重写 config 会丢，之前开过就重注入（cn-block 之后，二者互不覆盖）
    try:
        bt_reapply()
    except Exception as e:
        print("BT 屏蔽重注入跳过（不影响节点）:", e)

    # 落盘保存，避免终端刷屏后找不到；同时打印到屏幕
    out_file = "/root/xy-nodes.txt"
    try:
        with open(out_file, "w") as f:
            f.write("\n".join(all_links) + "\n")
    except OSError:
        out_file = None

    print("\n" + "=" * 60)
    print("分享链接（直接喂给 Mihomo-fx 的 LINKS 解析）:")
    print("=" * 60)
    print("\n".join(all_links))
    if out_file:
        print(f"（已保存到 {out_file}）")

    # 生成三格式订阅（mihomo / sing-box / Shadowrocket），各自一条链接
    ok = False
    try:
        ok = build_subscription(all_links, new_token=True)   # 重装换了节点/域名 → 换 token 刷新订阅
    except Exception as e:
        print("\n订阅生成跳过（不影响节点使用）:", e)
    if ok:
        urls = sub_urls_text()
        if out_file:
            open(out_file, "a").write("\n# 订阅链接:\n" + urls + "\n")
        print("\n" + "=" * 60)
        print("一键订阅链接（按你的客户端选对应一条，含全部节点+分流规则）:")
        print("=" * 60)
        print(urls)
        print("=" * 60)
        proto = "HTTPS(真证书) + 随机 token" if _sub_https() else "明文 HTTP + 随机 token（无域名/自签，客户端拒绝自签 TLS）"
        print(f"※ {proto}，请勿外传；改端口/关闭见 xy-sub.service（端口 {sub_port()}）")

    # 记住这次安装（节点不再随重装丢失：下次进安装默认「保持节点、只更新配置」）
    try:
        json.dump({"host": G["host"], "domain": G["domain"], "sni": G["sni"],
                   "prefix": G.get("prefix", ""), "hy2_ports": G.get("hy2_ports", ""),
                   "nginx": G.get("nginx", ""), "reality443": G.get("reality443", ""),
                   "sni_split": G.get("sni_split", ""), "smux": G.get("smux", ""),
                   "sb": sb_names, "xray": xr_names},
                  open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass

    install_shortcut()
    sched = setup_core_update_cron()                     # 内核每月自动更新（北京每月2号04:00）
    if sched:
        print(f'内核已设为每月自动更新一次（{_core_update_schedule_str()}）；也可随时进菜单 19 手动立即更新。')
    print('\n下次直接输入 \033[1;32mbgpeer\033[0m 即可打开管理面板。')

# ============================================================================ 管理面板 / 快捷命令
def install_shortcut(content=None):
    """安装 bgpeer 快捷命令：本地存一份脚本，wrapper 每次尽量拉最新再运行。
       content 给了就存它（更新脚本时传刚下载的新版，避免又被当前运行的旧版覆盖）。"""
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        open(SELF_LOCAL, "w").write(content if content is not None else open(__file__).read())
        # raw.githubusercontent 常被 GitHub 限流(429)，加 jsDelivr 镜像兜底；
        # 只有真的下到非空内容才覆盖本地，拉不到就继续用本地缓存（不会退回旧版失败）。
        wrapper = ("#!/usr/bin/env bash\n"
                   'u="https://raw.githubusercontent.com/bgpeer/nodekit/main/xy-installer.py"\n'
                   'j="https://cdn.jsdelivr.net/gh/bgpeer/nodekit@main/xy-installer.py"\n'
                   't="$(mktemp)"\n'                     # 随机临时文件，避免固定路径被抢注
                   'curl -fsSL "$u" -o "$t" 2>/dev/null || curl -fsSL "$j" -o "$t" 2>/dev/null || true\n'
                   '[ -s "$t" ] && mv "$t" /etc/bgpeer/xy-installer.py; rm -f "$t"\n'
                   'exec python3 /etc/bgpeer/xy-installer.py "$@"\n')
        open("/usr/local/bin/bgpeer", "w").write(wrapper)
        os.chmod("/usr/local/bin/bgpeer", 0o755)
    except Exception:
        pass

def read_saved_links():
    out = []
    try:
        for l in open(NODE_FILE):        # 用常量，别再写死路径（测试/改路径时两边会对不上）
            s = l.strip()
            if s.startswith("#"):          # 到「# 订阅链接:」注释就停，别把订阅 URL 当节点
                break
            if "://" in s:
                out.append(s)
    except OSError:
        pass
    return out

def _sub_service_synced():
    """正在跑的 xy-sub.service 的 HTTP/HTTPS 状态是否与应有的一致。
       不一致多见于：升级脚本后订阅 URL 变成 https，但托管服务还是旧的明文 HTTP。"""
    try:
        svc = open("/etc/systemd/system/xy-sub.service").read()
    except OSError:
        return True                       # 还没有该服务（没装），不强制
    return (ACME_CRT in svc) == _sub_https()

def show_links():
    links = read_saved_links()
    if not links:
        print("\n还没有节点，请先『1.安装』。"); return
    if not _sub_service_synced():         # HTTP/HTTPS 漂移 → 自动把托管服务同步到当前应有状态
        try:
            serve_sub()                   # 不换 token，仅切换 HTTP/HTTPS 并重启 xy-sub
            print("（已把订阅托管服务同步到 " + ("HTTPS" if _sub_https() else "HTTP") + "，URL 不变）")
        except Exception as e:
            print("（订阅服务同步失败，可稍后『更新配置』重试）:", e)
    print("\n" + "=" * 60 + "\n分享链接:\n" + "=" * 60)
    # 和订阅里保持一致：同样补上「前缀·协议」的分隔点（只影响显示，NODE_FILE 不动）
    print("\n".join(_link_rename(u, _sep_name(_link_name(u))) for u in links))
    urls = sub_urls_text()
    if urls:
        print("=" * 60 + "\n订阅链接（按客户端选一条）:\n" + urls)
    print("-" * 60)
    print("  1 更换节点名称前缀（只改显示名、不用重装，改完自动刷新三格式订阅）")
    if _ask("  选择(回车返回): ").strip() == "1":
        rename_prefix()

def peers_menu():
    """聚合节点链接：顶部显示本机 .links 地址（给别人聚合用），下面加/删成员机链接。
       改完到配置菜单点『更新配置』生效。"""
    # 老安装升级上来还没 .links 端点 → 进来补生成一次，保证本机地址能显示
    if read_saved_links() and not links_url():
        try: serve_sub()
        except Exception: pass
    while True:
        peers = load_peers()
        print("\n" + "=" * 60)
        print("  聚合节点链接（多机汇总）")
        print("=" * 60)
        lu = links_url()
        print("  ▸ 本机 links 链接地址（要被别的主机聚合时，复制这条给它）:")
        print("    " + (lu if lu else "（本机还没节点，先『1.安装』）"))
        print("-" * 60)
        if peers:
            print("  已添加的成员链接（生成时不通的自动忽略）：")
            for i, u in enumerate(peers, 1):
                ok, why = peer_status(u)
                print(f"    {i}. {u}   " +
                      ("\033[1;32m✓\033[0m" if ok else f"\033[1;31m✗ {why}\033[0m"))
        else:
            print("  还没添加成员链接。到别的机器进本菜单，复制它顶部那条 links 链接，粘进来即可。")
        print("-" * 60)
        print("  1 添加链接    2 删除链接    3 刷新本机 links 链接（换 token）    0 返回")
        print("  （加/删后回主菜单进配置菜单点『更新配置』重新汇总生成）")
        c = _ask("选择: ").strip()
        if c == "3":
            if not links_url():
                print("  本机还没节点/links 链接，先『1.安装』。"); continue
            if _ask("  换 token 后旧地址立即失效，聚合了本机的主机要重新复制新地址。确认? y/n: ").strip().lower() in ("y", "yes"):
                try:
                    rotate_links_token()
                    print("  ✓ 已换新地址：\n    " + links_url())
                except Exception as e:
                    print("  刷新失败:", e)
        elif c == "1":
            u = _ask("  粘贴成员机 .links 地址: ").strip()
            if not u:
                continue
            if not re.match(r"^https?://", u):
                print("  ✗ 不是合法的 http(s) 地址，已忽略。"); continue
            if u in peers:
                print("  该链接已存在。"); continue
            peers.append(u); save_peers(peers)
            ok, why = peer_status(u)
            print("  ✓ 已添加。" + ("连通 ✓" if ok else f"（当前 ✗ {why}；之后通了会自动纳入）"))
        elif c == "2":
            if not peers:
                continue
            n = _ask("  删除哪些编号（逗号分隔如 1,3；a=全部）: ").strip().lower()
            if n in ("a", "all"):
                save_peers([]); print(f"  已全部删除（{len(peers)} 条）。"); continue
            try:                                # 手机输入法常打出中文逗号，一并兼容
                idxs = sorted({int(x) for x in n.replace("，", ",").split(",") if x.strip()}, reverse=True)
            except ValueError:
                idxs = []
            if not idxs or not all(1 <= i <= len(peers) for i in idxs):
                print("  编号无效。"); continue
            for i in idxs:                      # 从大到小删，编号不会因前面先删而错位
                print("  已删除:", peers.pop(i - 1))
            save_peers(peers)
        elif c in ("0", ""):
            return

def edit_file(path):
    ed = shutil.which("nano") or shutil.which("vi") or shutil.which("vim")
    if not ed:
        print("未找到编辑器，请手动编辑:", path); return
    try:
        subprocess.call([ed, path])
    except Exception as e:
        print("打开编辑器失败:", e, "—— 手动改:", path)

def _validate_generated(ext, path):
    """校验刚生成的订阅配置，返回 (ok, 错误信息)。主要抓自定义模板改坏导致的语法错误。"""
    try:
        text = open(path).read()
    except OSError as e:
        return False, f"读取失败: {e}"
    if not text.strip():
        return False, "生成内容为空（模板损坏或锚点未命中）"
    if ext == "json":                                           # sing-box：只验 JSON 语法
        # 注意：这是给客户端用的订阅配置，不能用服务器的 sing-box check 做语义校验——
        # 客户端内核版本常与服务器不同，模板里 dns.optimistic 等字段在客户端合法、
        # 却可能不被服务器内核识别，硬校验会误杀（用户模板没动却报失败）。
        try:
            json.loads(text)
        except Exception as e:
            return False, f"JSON 语法错误: {e}"
        return True, ""
    if ext == "yaml":                                           # mihomo：关键段必查 + 有 PyYAML 再验语法
        for sec in ("proxies:", "proxy-groups:", "rules:"):
            if sec not in text:
                return False, f"缺少 {sec} 段（模板损坏）"
        try:
            import yaml
            yaml.safe_load(text)
        except ImportError:
            pass
        except Exception as e:
            return False, f"YAML 语法错误: {e}"
        return True, ""
    if ext == "conf":                                           # Shadowrocket：查关键段
        for sec in ("[Proxy]", "[Proxy Group]", "[Rule]"):
            if sec not in text:
                return False, f"缺少 {sec} 段（模板损坏）"
        return True, ""
    return True, ""

def _regen_config(ext, url, which):
    """用指定模板重生成单格式配置；不动节点、不换 token；失败回滚保留原配置。返回是否成功。
       成功后记住这次用的是哪套模板（which），之后多路复用/中转/自建DNS 等功能重生成
       订阅时会跟着这个选择走，不再被自定义链接单方面劫持。"""
    if not read_saved_links():
        print("  没有已保存节点。"); return False
    G["host"] = _host(); ensure_deps()
    links = aggregated_links()                                  # 本机 + 成员机节点（多机聚合）
    ylines, nodes = parse_nodes(links)
    target = FMT[ext]["file"]
    backup = open(target).read() if os.path.exists(target) else None
    try:
        FMT[ext]["gen"](ylines, nodes, url)
    except Exception as e:
        if backup is not None: open(target, "w").write(backup)      # 回滚，保留原能用配置
        print(f"\n  ❌ 更新失败（生成出错，已保留原配置）：{e}"); return False
    ok, err = _validate_generated(ext, target)
    if not ok:
        if backup is not None: open(target, "w").write(backup)      # 语法/校验不过 → 回滚
        print(f"\n  ❌ 更新失败（{FMT[ext]['label']} 语法/校验错误，已保留原配置）：")
        for ln in str(err).splitlines()[:6]:
            print("     " + ln)
        return False
    serve_sub()                                                     # 保持 token，URL 不变
    set_tplsrc(ext, "custom" if which == "自定义" else "author")    # 记住选择，后续功能跟着走
    print(f"\n  ✅ 更新成功（{which}模板，节点/URL 未变）：\n  {sub_url(ext)}")
    print(f"  ▸ {FMT[ext]['label']} 之后一律按【{which}模板】生成"
          f"（多路复用/GitHub中转/自建DNS 改动时也跟着它）。")
    return True

def update_one_config(ext):
    """更新单个格式的配置：可选作者模板 / 自定义模板；不动节点、不换 token。"""
    print("\n  1 作者模板   2 自定义模板   0 返回")
    c = _ask("  选择: ").strip()
    if c == "1":
        _regen_config(ext, FMT[ext]["author"], "作者")
    elif c == "2":
        url = load_custpl().get(ext)
        if not url:
            print("  还没添加自定义模板链接（先选『4 自定义模板链接』）。"); return
        _regen_config(ext, url, "自定义")

def config_menu(ext):
    """单个格式的配置子菜单：改配置 / 改订阅(换token) / 更新配置(作者·自定义) / 加自定义模板链接。"""
    meta = FMT[ext]
    if not os.path.exists(meta["file"]):
        print(f"\n还没有 {meta['label']} 配置，请先『1.安装』。"); return
    while True:
        cust = load_custpl().get(ext)
        print("\n" + "=" * 60 + f"\n{meta['label']} 配置\n" + "=" * 60)
        src = tpl_src_of(ext)
        print(f"  配置文件: {meta['file']}")
        print(f"  当前订阅: {sub_url(ext)}")
        print(f"  自定义模板: {cust or '(未设置)'}")
        print(f"  ▸ 当前生效: 【{'自定义模板' if src == 'custom' else '作者模板'}】"
              f"  ← 多路复用/GitHub中转/自建DNS 重生成订阅时也用它")
        print("-" * 60)
        print("  1 修改配置（编辑器打开）")
        print("  2 修改订阅（显示当前 / 换 token）")
        print("  3 更新配置（作者模板 / 自定义模板）")
        print("  4 自定义模板链接（添加 / 更换 / 删除）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            edit_file(meta["file"])
        elif c == "2":
            print("  当前订阅:", sub_url(ext))
            if _ask("  换新 token? [y/N]: ").lower() in ("y", "yes"):
                rotate_token_ext(ext); print("  新订阅:", sub_url(ext))
        elif c == "3":
            update_one_config(ext)
        elif c == "4":
            cur = load_custpl().get(ext)
            if cur:                                     # 已有链接：给 更换 / 删除 / 返回
                print(f"\n  当前自定义模板链接：{cur}")
                print("  1 更换   2 删除（改回作者模板）   0 返回")
                s = _ask("  选择: ").strip()
                if s == "2":
                    del_custpl(ext); set_tplsrc(ext, "author")   # 链接没了，当前选择同步切回作者
                    print("  ✓ 已删除自定义模板链接，之后一律用作者模板。")
                    if _ask("  现在就用作者模板重新生成一次配置? [y/N]: ").lower() in ("y", "yes"):
                        _regen_config(ext, FMT[ext]["author"], "作者")   # 立即生效
                    continue
                if s != "1":
                    continue                            # 0/其它 → 返回，不动原链接
            url = _ask("  自定义模板链接(gist/GitHub raw，占位符须与作者模板一致): ").strip()
            if url:
                set_custpl(ext, url); print("  ✓ 已保存。之后『3→2 自定义模板』即用它。")
        elif c == "0" or c == "":
            return

def _script_ver(text):
    """从脚本源码里抠 SCRIPT_VERSION；老版本没有这行则返回 '?'。"""
    m = re.search(r'^SCRIPT_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"

def update_script():
    """只更新脚本本体到最新，不动节点、不改配置；有新版则自动重载新版面板。"""
    try:
        latest = fetch_url(_RAW + "xy-installer.py")
    except Exception as e:
        print("\n更新脚本失败:", e); return
    try:    cur = open(SELF_LOCAL).read()
    except OSError: cur = ""
    if latest == cur:
        # 镜像(jsDelivr)对 main 分支有最长 ~12 小时缓存；刚发布的新版可能要等缓存刷新
        print(f"\n已是最新版本 v{SCRIPT_VERSION}。（若刚发布过新版还没看到，多半是 GitHub/镜像缓存未刷新，稍后再试）")
        return
    install_shortcut(latest)
    print(f"\n脚本已更新 v{SCRIPT_VERSION} → v{_script_ver(latest)}（节点/配置均未改动），正在重新载入新版面板…")
    import sys
    os.execv(sys.executable, [sys.executable, SELF_LOCAL])

def setup_core_update_cron():
    """装每月定点更新内核的 cron：北京时间每月 2 号 04:00。
       Debian/Ubuntu 的 cron 不支持 CRON_TZ，按服务器本地时区把北京时刻换算成本地。
       北京(UTC+8) 2 号 04:00 视本机时区落在本地 1 号或 2 号，天/时/分一并算出。"""
    try:
        import datetime
        if os.path.abspath(__file__) != SELF_LOCAL:      # 确保 cron 调的本地副本存在
            os.makedirs(BGP_DIR, exist_ok=True)
            shutil.copy(os.path.abspath(__file__), SELF_LOCAL)
        bj = datetime.timezone(datetime.timedelta(hours=8))
        local = datetime.datetime(2001, 6, 2, 4, 0, tzinfo=bj).astimezone()  # 每月2号04:00北京→本地
        txt = (f"# bgpeer 内核每月自动更新（北京时间每月2号04:00 = 本机每月{local.day}号 {local:%H:%M}）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               f"{local.minute} {local.hour} {local.day} * * root python3 {SELF_LOCAL} "
               f"update-cores >> {CORE_CRON_LOG} 2>&1\n")
        open(CORE_CRON_FILE, "w").write(txt); os.chmod(CORE_CRON_FILE, 0o644)
        return local
    except OSError as e:
        print("  安装内核自动更新 cron 失败（不影响使用）:", e); return None

def _core_update_schedule_str():
    """返回本机 cron 实际触发时刻的可读描述（北京每月2号04:00 换算后）。"""
    import datetime
    bj = datetime.timezone(datetime.timedelta(hours=8))
    local = datetime.datetime(2001, 6, 2, 4, 0, tzinfo=bj).astimezone()
    return f"每月 {local.day} 号 {local:%H:%M}（本机时区，= 北京每月 2 号 04:00）"

def _xray_heal_minclientver(restart=True):
    """给现有 xray reality 入站补 minClientVer（缺才补）。
       xray v26.7.11+ reality 服务端默认 minClientVer=26.3.27，静默拒掉上报旧版本的客户端
       (mihomo/Clash 系硬编码 1.8.2、sing-box、旧 xray)。补成 1.0.0(接受所有客户端)。
       只在确有缺失时改配置+校验+重启；校验不过则回滚。改了返回 True。"""
    cfg = f"{XRAY_DIR}/config.json"
    if not os.path.exists(cfg):
        return False
    try:
        data = json.load(open(cfg))
    except Exception:
        return False
    changed = False
    for ib in data.get("inbounds", []):
        rs = (ib.get("streamSettings") or {}).get("realitySettings")
        if isinstance(rs, dict) and not rs.get("minClientVer"):
            rs["minClientVer"] = "1.0.0"
            changed = True
    if not changed:
        return False
    old = open(cfg).read()
    json.dump(data, open(cfg, "w"), indent=2)
    if os.path.exists(XRAY_BIN):
        ok, msg = core_check(XRAY_BIN, cfg)
        if not ok:
            open(cfg, "w").write(old)                        # 回滚，绝不留坏配置
            return False
    if restart:
        sh("systemctl restart xray", check=False)
    return True

CORE_DONE_MARK = "本次更新结束"     # 前台跟随日志时用它判断后台已跑完
CERT_DONE_MARK = "本次证书修复结束"  # 同上，证书修复用
NODE_OP_MARK   = "本次节点操作结束"  # 同上，装/加/删协议用

def update_cores_auto(only=None):
    """非交互更新已安装的内核到最新并重启。起不来会记进日志。
       两个入口共用：cron 每月自动更新，以及菜单19 转到后台时。
       only: None 或 "both" → 两个都更；"sing-box" / "xray" → 只更那一个。"""
    ensure_deps()
    ts = time.strftime("%F %T")
    for name, binpath, installer in (("sing-box", SB_BIN, install_singbox),
                                     ("xray", XRAY_BIN, install_xray)):
        if only not in (None, "both") and name != only:
            continue
        if not os.path.exists(binpath):
            continue
        try:
            installer(); sh(f"systemctl restart {name}", check=False)
            time.sleep(2)
            act = sh(f"systemctl is-active {name}", check=False)
            ver = (sh(f"{binpath} version", check=False).splitlines() or ["?"])[0]
            print(f"{ts} {name} 更新完成（{act}）: {ver}")
        except Exception as e:
            print(f"{ts} {name} 更新失败:", e)
    if _xray_heal_minclientver():                            # 升级到 xray 26.7.11+ 后补 minClientVer，兼容旧客户端
        print(f"{ts} xray reality 已补 minClientVer=1.0.0（兼容 mihomo/旧客户端）")
    setup_core_update_cron()                                 # 顺手确保每月自动更新的 cron 在
    print(f"{time.strftime('%F %T')} {CORE_DONE_MARK}")      # 后台跑时用 python3 -u，逐行落盘不缓冲

def update_cores():
    print("\n当前版本:")
    for name, binpath in (("sing-box", SB_BIN), ("xray", XRAY_BIN)):
        if os.path.exists(binpath):
            v = sh(f"{binpath} version", check=False)
            print(f"  {name}: {v.splitlines()[0] if v else '版本读取失败'}")
        else:
            print(f"  {name}: 未安装")
    print("更新核心:  1. sing-box   2. xray   3. 两个   0. 返回")
    print(f"  （每月自动更新已开启：{_core_update_schedule_str()}）")
    c = _ask("选择: ")
    if c == "0" or not c:
        return
    target = {"1": "sing-box", "2": "xray", "3": "both"}.get(c)
    if not target:
        return
    _run_core_update_detached(target)

def _run_core_update_detached(target):
    """把更新派到独立会话里跑，前台只负责跟日志。

       为什么不能在前台直接跑：更新的最后一步是 systemctl restart，而很多人是**挂着本机
       代理来管理这台机**的——重启核心会当场掐断 SSH，前台的 python 进程随即收到 SIGHUP
       死掉。表现就是「先更的那个成功了，后更的那个没动」（两个都选时 sing-box 先重启，
       SSH 一断，xray 就永远轮不到）。start_new_session=True 让它脱离控制终端，SIGHUP
       打不到它，断了照样在服务端跑完——这也是 restart_services() 一直遵循的那条原则。

       前台跟随日志只是为了给你看进度；SSH 断了顶多是看不到后半段，不影响后台那个进程。"""
    try:
        start = os.path.getsize(CORE_CRON_LOG) if os.path.exists(CORE_CRON_LOG) else 0
    except OSError:
        start = 0
    try:
        subprocess.Popen(                                # -u：不缓冲，日志逐行落盘才跟得上
            f"python3 -u {SELF_LOCAL} update-cores {target} >> {CORE_CRON_LOG} 2>&1",
            shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("\n  转后台失败，改在前台直接更新:", e)     # 兜底：宁可前台跑完，也不能不更新
        update_cores_auto(target)
        return
    print(f"\n  已转入后台执行（断开 SSH 也会在服务端跑完）。日志: {CORE_CRON_LOG}")
    print("  下面实时跟随进度，看够了可以直接 Ctrl-C 或断开，不影响后台：\n")
    pos, deadline = start, time.time() + 600
    try:
        while time.time() < deadline:
            time.sleep(1)
            try:
                if os.path.getsize(CORE_CRON_LOG) <= pos:
                    continue
                with open(CORE_CRON_LOG, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                continue
            text = chunk.decode("utf-8", "replace")
            print("  " + text.rstrip("\n").replace("\n", "\n  "))
            if CORE_DONE_MARK in text:
                return
        print(f"\n  等了 10 分钟还没跑完，后台仍在继续。稍后看日志: tail {CORE_CRON_LOG}")
    except KeyboardInterrupt:
        print(f"\n  已退出跟随，后台继续执行。稍后看日志: tail {CORE_CRON_LOG}")

def _uninstall_core():
    """卸载代理主体：sing-box/xray、订阅服务、证书、AdGuard、CDN 节点、命令、cron。
       不含网络优化(BBR/QoS，独立模块)——由调用方决定要不要一起 --reset。"""
    cdn_svcs = [n["svc"] for n in _cdn_load() if n.get("svc")]   # 全部 CDN 套用节点服务
    for svc in ["sing-box", "xray", "xy-sub", CDN_SVC] + cdn_svcs:
        sh(f"systemctl disable --now {svc}", check=False)
        sh(f"rm -f /etc/systemd/system/{svc}.service", check=False)
    sh("systemctl daemon-reload", check=False)
    for ipt in ("iptables", "ip6tables"):
        for line in sh(f"{ipt} -t nat -S PREROUTING", check=False).splitlines():
            if line.startswith("-A") and "xy_hy2_portHopping" in line:
                sh(f"{ipt} -t nat " + line.replace("-A", "-D", 1), check=False)
    sh("netfilter-persistent save", check=False)
    if os.path.exists(NGINX_CONF):                      # 移除本脚本的 nginx 前置块（不动用户其它站点）
        sh(f"rm -f {NGINX_CONF}", check=False)
    _nginxconf_remove_stream()                          # 撤掉 sni-split 加进 nginx.conf 的 stream 块
    sh(f"rm -f {NGINX_STREAM_CONF}", check=False)
    if have("nginx"):
        sh("nginx -t && systemctl reload nginx", check=False)
    if os.path.exists("/opt/AdGuardHome/AdGuardHome"):   # 去广告 DNS（AdGuard Home）一并撤掉——它的 DoT 靠 /etc/ssl/sb 证书，证书这里会删
        sh("/opt/AdGuardHome/AdGuardHome -s uninstall", check=False)
        sh("systemctl stop AdGuardHome", check=False)
        sh("rm -rf /opt/AdGuardHome", check=False)
    for p in (SB_BIN, XRAY_BIN, SB_DIR, XRAY_DIR, "/etc/ssl/sb", SUB_DIR,
              "/root/xy-nodes.txt", "/usr/local/bin/bgpeer", "/etc/bgpeer", WEBROOT,
              # cn-block 的每日刷新 cron、内核每月更新 cron 及日志：不清掉 cron 会调已删脚本报错
              "/etc/cron.d/bgpeer-cnblock", "/var/log/bgpeer-cnblock.log",
              CORE_CRON_FILE, CORE_CRON_LOG):
        sh(f"rm -rf {p}", check=False)

def uninstall_all():
    """卸载子菜单：只卸代理主体 / 全部卸载(再带上网络优化) / 返回。
       AdGuard 自建DNS 本就随代理主体一起卸(它的 DoT 依赖 acme 证书，证书会被删)；
       唯一独立、需单独带上的是网络优化(BBR/QoS，写在 /etc/net-optimize)。"""
    nopt = os.path.exists(NETOPT_CONFIG)
    print("\n" + "=" * 60 + "\n卸载\n" + "=" * 60)
    print("  代理主体：sing-box/xray、订阅服务、证书、AdGuard自建DNS、CDN 节点、bgpeer 命令、定时任务")
    print(f"  网络优化(BBR/QoS)：{'已启用（独立模块）' if nopt else '未启用'}")
    print("-" * 60)
    print("  1 卸载代理主体（网络优化保留）")
    print("  2 全部卸载（代理主体 + 网络优化，一次清干净、恢复系统默认）")
    print("  0 返回")
    c = _ask("选择: ").strip()
    if c == "1":
        if _ask("\n  确认卸载代理主体（AdGuard 一并卸；网络优化保留）? [y/N]: ").lower() in ("y", "yes"):
            _uninstall_core()
            print("\n已卸载代理主体。" + ("网络优化仍在（想卸进『网络优化→卸载』或重跑本项选 2）。" if nopt else ""))
    elif c == "2":
        if _ask("\n  确认全部卸载（含网络优化，恢复系统默认）? [y/N]: ").lower() in ("y", "yes"):
            if nopt:
                print("\n【1/2】卸载网络优化…")
                _run_net_optimize("--reset")                 # 先卸它——它的脚本缓存在 /etc/bgpeer，等下会被主体一起删
            else:
                print("\n【1/2】网络优化未启用，跳过。")
            print("\n【2/2】卸载代理主体…")
            _uninstall_core()
            print("\n✅ 全部卸载完毕，已恢复到装脚本前的干净状态。")
    # 0/其它 → 返回

# ============================================================================ 屏蔽中国域名/IP（独立文件）
def ensure_remote_script(url, local):
    """把仓库里的脚本拉到本地（每次尽量拉最新）；拉不到就用本地缓存。

    两个坑都在这几行里踩过：
      · 原来是 open(local,"w").write(fetch_url(url))，Python 会先把本地文件截成
        0 字节再去发请求，网络一抖缓存就没了；而 os.path.exists 依然为真，于是
        "成功"跑起一个空脚本，菜单点进去毫无反应。改成拉全、校验、再原子替换。
      · URL 上带时间戳绕开 CDN 缓存：raw.githubusercontent 和 jsDelivr 都会缓存
        几分钟到几小时，仓库明明改了、机器上拉到的还是旧版，修复就一直到不了。
    """
    os.makedirs(BGP_DIR, exist_ok=True)
    try:
        sep = "&" if "?" in url else "?"
        body = fetch_url(f"{url}{sep}_t={int(time.time())}")
        if body.strip():
            # 【换上去之前先编译一遍】拉到的可能是半截文件、一页 404、或者限流提示，
            # 而"非空"拦不住这些。四个远程脚本都是 .py，语法层面的坏当场就能查出来 ——
            # 查出来就整个放弃，本地那份一个字节都不动，菜单照常能用旧版进去。
            # media-stack 自己的 selfupdate 早就是这么做的，这条路一直漏着。
            compile(body, local, "exec")
            tmp = local + ".new"
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, local)
    except SyntaxError as e:
        print(f"  \033[33m⚠\033[0m 拉到的 {os.path.basename(local)} 语法不对"
              f"（第 {e.lineno} 行），没有替换本地那份，继续用现有版本。")
    except Exception:
        pass
    return os.path.exists(local) and os.path.getsize(local) > 0

def ensure_cn_block():
    return ensure_remote_script(CN_BLOCK_URL, CN_BLOCK_LOCAL)

def cn_block_menu():
    """打开独立的 cn-block.py 交互菜单（屏蔽 CN 域名/IP + 白名单）。"""
    if not ensure_cn_block():
        print("拉取 cn-block.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {CN_BLOCK_LOCAL}", shell=True)

def adguard_menu():
    """打开独立的 adguard-dns.py 交互菜单（去广告 DNS · AdGuard Home）。"""
    if not ensure_remote_script(ADGUARD_URL, ADGUARD_LOCAL):
        print("拉取 adguard-dns.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {ADGUARD_LOCAL}", shell=True)

def media_stack_menu():
    """打开独立的 media-stack.py（自建 Emby·网盘直链媒体服务器）。

    它是完全独立的一个文件：只【读】本脚本的 state.json（拿域名）和 nginx 的
    内部 https 端口，只【写】/etc/nginx/conf.d/media-stack.conf 和它自己的安装
    目录，绝不碰 nginx.conf / bgpeer.conf / bgpeer-stream.conf。写 nginx 前会先
    nginx -t，不过就自动还原 —— 不会因为装媒体服务把节点搞坏。
    """
    if not ensure_remote_script(MEDIA_URL, MEDIA_LOCAL):
        print("拉取 media-stack.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {MEDIA_LOCAL}", shell=True)

def _ghrelay_regen():
    """重新生成三格式订阅 + 重写托管服务（含中转/新 token）。"""
    G["host"] = _host(); ensure_deps()
    return build_subscription(read_saved_links())

def _relay_probe(prefix):
    """拿这条中转真去拉一个小文件，看通不通。返回 (是否通, 说明)。"""
    probe = "https://raw.githubusercontent.com/bgpeer/nodekit/main/sub-template.yaml"
    try:
        req = urllib.request.Request(prefix + probe, headers={"User-Agent": "xy-installer"})
        body = urllib.request.urlopen(req, timeout=12).read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return False, "403 —— token 不对（对方刷过 token？去那台机器菜单 14 重新复制前缀）"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"连不上：{str(e)[:60]}"
    if b"proxy-groups" not in body:
        return False, f"通了但内容不对（拿到 {len(body)} 字节，不像模板）"
    return True, f"通，拉到 {len(body)} 字节"

def ghdl_relay_menu():
    """备用取件中转：本机拉不到 GitHub 时，借【别的机器】上的中转把东西转过来。

       为什么不用本机自己那个：本机中转就跑在本机上，出网同一条路。本机连不上
       GitHub，它也连不上，多绕一跳没有任何意义。所以这里填的必须是别的机器。"""
    while True:
        lst = own_relays()
        print("\n" + "=" * 60 + "\n备用取件中转（自己人的，优先于公共反代）\n" + "=" * 60)
        print("  用途：这台机器拉不到 GitHub（内核二进制 / 模板 / 版本号）时，")
        print("        借你另一台能通的机器把东西转过来，不用把包交给第三方反代。")
        print("  怎么拿：去那台能通 GitHub 的机器，菜单 14 顶上就印着它的中转地址前缀，整条复制过来。")
        print("-" * 60)
        if lst:
            for i, u in enumerate(lst, 1):
                print(f"  {i}) {u}")
        else:
            print("  （还没配。没配也不影响——直连不通时会退到公共反代，只是那是第三方。）")
        print("-" * 60)
        print("  1 添加一条    2 删除一条    3 逐条测试连通    0 返回")
        c = _ask("选择: ").strip()
        if c in ("0", ""):
            return
        if c == "1":
            u = _ask("  粘贴中转前缀（形如 https://域名:端口/<token>/gh/，回车取消）: ").strip()
            if not u:
                continue
            if not u.startswith(("http://", "https://")):
                print("  \033[1;31m✗ 要一条完整的 http(s) 地址。\033[0m"); continue
            u = u if u.endswith("/") else u + "/"     # 先补斜杠再查 /gh/：
            if "/gh/" not in u:                       # 手抖把末尾那道斜杠删了不该被判成格式错
                print("  \033[1;31m✗ 看着不像中转前缀——正经的那条里有 /gh/。"
                      "去那台机器菜单 14 顶上整条复制。\033[0m"); continue
            try:
                host = urllib.parse.urlsplit(u).hostname or ""
            except Exception:
                host = ""
            if host and host == (_host() or "").strip():
                print("  \033[1;31m⚠ 这是【本机】自己的中转地址。\033[0m")
                print("    它就跑在这台机器上，出网走同一条路——本机连不上 GitHub，它也连不上，")
                print("    绕这一跳不会让你多拉到任何东西。要填的是【另一台】能通 GitHub 的机器。")
                if _ask("    还是要加？(y/N): ").strip().lower() != "y":
                    continue
            ok, why = _relay_probe(u)
            print(("  \033[1;32m✓ " if ok else "  \033[1;31m✗ ") + why + "\033[0m")
            if not ok and _ask("  测不通，仍然加进去？(y/N): ").strip().lower() != "y":
                continue
            if u in lst:
                print("  已经在列表里了。"); continue
            save_own_relays(lst + [u])
            print("  ✓ 已添加。取件时会排在公共反代前面。")
        elif c == "2":
            if not lst:
                print("  列表是空的。"); continue
            n = _ask("  删第几条（回车取消）: ").strip()
            if not n.isdigit() or not (1 <= int(n) <= len(lst)):
                print("  序号不对。"); continue
            gone = lst.pop(int(n) - 1); save_own_relays(lst)
            print(f"  ✓ 已删：{gone}")
        elif c == "3":
            if not lst:
                print("  列表是空的。"); continue
            for u in lst:
                ok, why = _relay_probe(u)
                print(("  \033[1;32m✓ " if ok else "  \033[1;31m✗ ") + f"{u}  —— {why}" + "\033[0m")
        else:
            print("  无效选择。")

def ghrelay_menu():
    """GitHub 中转：规则/图标走【本机中转】还是 gh-proxy.com（别人的）。默认本机中转。
       支持开/关 + 刷新中转 token（防别人蹭，旧地址立即失效，配置随之刷新）。需域名+真证书。"""
    dom = _host()
    if not (re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") and _sub_https()):
        print("\n  需要域名 + acme 真证书才能自建中转（走 HTTPS）；当前无域名/自签，只能用 gh-proxy。"); return
    while True:
        on = not os.path.exists(GHRELAY_OFF)
        print("\n" + "=" * 60 + "\nGitHub 中转（规则/图标走本机·摆脱 gh-proxy 依赖）\n" + "=" * 60)
        print("  当前：" + ("\033[1;32m本机中转\033[0m" if on else "gh-proxy.com（别人的）"))
        if on:
            print(f"  中转地址前缀：https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/")
            print("  （只转发 GitHub、与订阅同端口、带 token 防蹭）")
        print("-" * 60)
        print(f"  1 本机中转 写入配置（开/关）   [当前：{'开' if on else '关（用 gh-proxy）'}]")
        print("  2 刷新中转 token（防别人蹭：旧地址立即失效 + 刷新订阅；订阅端口不变、客户端自动更新即可）")
        print("  3 刷新 token + 换端口（更狠：连订阅端口一起换随机·自动避开节点端口）")
        print(f"  4 备用取件中转（本机拉不到 GitHub 时，借别的机器转）  "
              f"[已配 {len(own_relays())} 条]")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "4":
            ghdl_relay_menu(); continue
        if c in ("1", "2", "3") and not read_saved_links():
            print("  还没有节点，先『1.安装』。"); continue
        if c == "1":
            if on:
                open(GHRELAY_OFF, "w").write("1")
            else:
                try: os.remove(GHRELAY_OFF)
                except OSError: pass
            print("  正在刷新订阅…")
            print("  ✓ 已切换并刷新订阅，客户端重拉即生效。" if _ghrelay_regen() else "  刷新失败（没有可用节点？）。")
        elif c == "2":
            if not on:
                print("  当前用的是 gh-proxy，先『1』开启本机中转再刷 token。"); continue
            open(GHRELAY_TOKEN_FILE, "w").write(secrets.token_urlsafe(12))   # 换新 token，旧的立即失效
            print("  正在换 token 并刷新订阅…")
            if _ghrelay_regen():
                print(f"  ✓ 已换新 token，旧中转地址立即失效。新前缀：https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/")
                print("  客户端重新拉一次订阅即用新 token（订阅地址端口没变，自动更新即可）。")
            else:
                print("  刷新失败（没有可用节点？）。")
        elif c == "3":
            if not on:
                print("  当前用的是 gh-proxy，先『1』开启本机中转再操作。"); continue
            R, N = "\033[1;31m", "\033[0m"
            cur = sub_port()
            print(f"\n  当前订阅端口：{cur}")
            print(f"  ⚠ 换端口后订阅地址会变，客户端要【重新导入订阅】；新端口{R}须在 VPS 防火墙/安全组放行{N}，")
            print("     否则订阅+中转+图标全部打不开（很多机房如 DMIT 默认只开装机时的端口）。")
            newp = None
            while True:                                       # 输错/冲突就退回重输，不用退出菜单重来
                s = _ask("  新订阅端口（回车=随机挑一个 / 输 n 返回）: ").strip().lower()
                if s in ("n", "no"):
                    break
                if not s:                                     # 回车 → 随机（自动避开已占端口/hy2 跳跃段）
                    try:
                        newp = _pick_sub_port()
                    except RuntimeError as e:
                        print(f"  {R}{e}{N}"); continue
                    print(f"  已随机挑到：\033[1;32m{newp}\033[0m（记得防火墙放行它）")
                    break
                if not s.isdigit() or not (1024 <= int(s) <= 65535):
                    print(f"  {R}端口无效{N}：请输入 1024-65535 的数字（或回车=随机 / n=返回）。"); continue
                p = int(s)
                if p != cur and not port_free(p):
                    print(f"  {R}端口冲突{N}：{p} 已被本机其它服务/节点占用，请换一个。"); continue
                newp = p; break
            if newp is None:                                  # 输了 n
                continue
            if _ask(f"  确认把订阅端口改为 {newp}? [y/N]: ").strip().lower() not in ("y", "yes"):
                continue
            set_sub_port(newp)
            open(GHRELAY_TOKEN_FILE, "w").write(secrets.token_urlsafe(12))
            print("  正在换端口 + token 并刷新订阅…")
            if _ghrelay_regen():
                print(f"  ✓ 新订阅端口：\033[1;32m{sub_port()}\033[0m　新中转前缀：https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/")
                print(f"  ▸ 客户端到菜单『2 节点链接/订阅』复制新订阅地址重新导入；确认防火墙已放行 {sub_port()}、可关掉旧端口。")
            else:
                print("  刷新失败（没有可用节点？）。")
        elif c in ("0", ""):
            return

def selfdns_toggle():
    """开关：把本机自建 DNS(AdGuard DoH) 写进订阅配置的 DNS，循环切换、写/删后自动刷新订阅。
       三个格式都写，但机制不同、后果也不同：
         mihomo / 小火箭 —— 列表型 DNS，自建 DoH 放最前当主用、原有留兜底，没通自动回落。
         sing-box —— 【没有列表回落】，一条 dns 规则只指一个 server tag，指到的那台挂了
                     查询就直接失败。所以是把 dns.final 整个换成自建 DoH，
                     DNS 机器挂了 sing-box 的默认解析就断（另两个只是慢一下）。
                     不做假的"兜底"，把差别在菜单里讲明白，让用户自己决定。
       adguard 菜单调用（selfdns-toggle）。"""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or ""):
        print("  需要域名（DoH 走域名+证书）。当前节点不是域名，无法写入自建 DNS。"); return
    if not read_saved_links():
        print("  还没有节点，先『1.安装』。"); return
    on = os.path.exists(SELFDNS_FLAG)
    if on:
        print(f"\n  自建 DNS 已写入订阅。当前 ClientID: {selfdns_clientid()}")
        print("  1 从订阅移除")
        print("  2 更换 ClientID（泄露了就换——截图/贴日志很容易带出去）")
        print("  0 返回")
        c = _ask("  选择: ").strip()
        if c == "2":
            rotate_selfdns_clientid(); return
        if c != "1":
            return
        try: os.remove(SELFDNS_FLAG)
        except OSError: pass
        act = "已移除"
    else:
        if not os.path.exists("/opt/AdGuardHome/AdGuardHome"):
            print("  还没装 AdGuard Home——先装并在后台开好加密(DoH 10443)，否则写进去也用不了。")
            if _ask("  仍然写入? [y/N]: ").strip().lower() not in ("y", "yes"):
                return
        open(SELFDNS_FLAG, "w").write("1")
        act = "已写入"
    G["host"] = dom; ensure_deps()
    if build_subscription(read_saved_links()):               # 重新生成三格式并托管（不换 token）
        print(f"\n  ✓ {act}自建 DNS，订阅已刷新（mihomo / sing-box / 小火箭 三个都写）。")
        if act == "已写入":
            print(f"  写入的 DoH：{_selfdns_doh()}")
            print("  ⚠ 确保 AdGuard 已开加密、防火墙放行 DoH 端口。")
            print("    mihomo / 小火箭：放在列表最前当主用，没通会自动回落到原 DNS，只是慢一下。")
            print("    sing-box：换的是 dns.final，且 sing-box【没有 DNS 回落机制】——"
                  "这台 DNS 挂了\n              它的默认解析就断（多机聚合时尤其要注意："
                  "别的节点还活着，解析却没了）。")
            print(f"\n  ▸ 建议顺手关掉「开放解析器」：DoH 挂在公网上，不设白名单谁扫到都能用。")
            print(f"    AdGuard 后台 → 设置 → DNS设置 → 访问设置 → 允许的客户端，填入这一行：")
            print(f"        {selfdns_clientid()}")
            print(f"    手机流量 IP 会变、没法按 IP 白名单，这个 ClientID 与 IP 无关，换网络也不影响。")
        print("  客户端重新拉一次订阅即生效。")
    else:
        print("  刷新配置失败（没有可用节点？）。")

def rotate_selfdns_clientid():
    """换一个新的 ClientID 并刷新订阅。ClientID 一旦被填进 AdGuard「允许的客户端」，
       它就等价于一把口令——而它会明晃晃出现在订阅配置、使用说明、终端输出里，截个图、
       贴段日志就带出去了。所以得有换的办法，跟 GitHub 中转 token 可以刷新是一个道理。

       换的顺序很重要：先把新 ID【加】进白名单（旧的先留着），再刷新订阅、改安卓 DoT，
       最后才删掉旧 ID —— 反过来做中间会有一段时间连不上。"""
    old = selfdns_clientid()
    dom = _host()
    print(f"\n  当前 ClientID: {old}")
    if _ask("  确认更换? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  已取消。"); return
    new = "xy" + secrets.token_hex(6)
    os.makedirs(BGP_DIR, exist_ok=True)
    open(SELFDNS_CID_FILE, "w").write(new)
    G["host"] = dom; ensure_deps()
    if not build_subscription(read_saved_links()):
        open(SELFDNS_CID_FILE, "w").write(old)          # 订阅没刷成就退回，别让两边对不上
        print("  ✗ 订阅刷新失败，已还原为原 ClientID。"); return
    wild = "DNS:*." + dom in sh(f"openssl x509 -in {ACME_CRT} -noout -text 2>/dev/null", check=False)
    print(f"\n  ✓ 已更换：{old}  →  {new}")
    print(f"  订阅已刷新，新的 DoH：{_selfdns_doh()}")
    print("\n  接下来按这个顺序做，中间不会断：")
    print(f"    1) AdGuard 后台 → 设置 → DNS设置 → 访问设置 → 允许的客户端，")
    print(f"       先【添加】一行 {new}（旧的 {old} 暂时留着）")
    print(f"    2) 客户端重新拉一次订阅")
    if wild:
        print(f"    3) 安卓「专用DNS」改填 {new}.{dom}")
        print(f"    4) 确认都通了，再把白名单里的 {old} 删掉")
    else:
        print(f"    3) 确认都通了，再把白名单里的 {old} 删掉")

def selfdns_off():
    """非交互移除：卸载 AdGuard 时调用。若自建 DNS 已写入订阅则清标记并刷新订阅（不换 token）；
       没写入则静默返回（什么都不打印）。adguard 卸载调用（selfdns-off）。"""
    if not os.path.exists(SELFDNS_FLAG):
        return
    try: os.remove(SELFDNS_FLAG)
    except OSError: pass
    dom = _host()
    links = read_saved_links()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") or not links:
        return                                               # 没域名/没节点：标记已清，订阅无从刷新，跳过
    G["host"] = dom
    try:
        if build_subscription(links):                        # 重新生成三格式（不含自建 DoH）并托管，不换 token
            print("  ✓ 已从订阅移除自建 DNS 并刷新（客户端重拉订阅即恢复原 DNS）。")
    except Exception as e:
        print("  订阅刷新跳过（不影响卸载）:", e)

def cn_block_reapply():
    """重装后调用：若之前开启过屏蔽，用 cn-block.py 重新注入（未开启则内部直接跳过）。"""
    if not cnblock_load().get("enabled"):
        return
    if ensure_cn_block():
        subprocess.run(f"python3 {CN_BLOCK_LOCAL} apply", shell=True)

def cnblock_load():
    try: return json.load(open(CNBLOCK_FILE))
    except Exception: return {}


# ============================================================================ 网络优化（本仓库 net-optimize.py）
def _run_net_optimize(args="", env_extra=None):
    """跑本仓库的 net-optimize.py；模式/阈值用环境变量传入，--check 走 args。
       脚本自带 SHA256 校验的自动更新，本地缓存旧了它会自己换到最新版再执行。"""
    if not ensure_remote_script(NETOPT_URL, NETOPT_LOCAL):
        print("拉取 net-optimize.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {NETOPT_LOCAL} {args}".strip(), shell=True,
                   env=dict(os.environ, **(env_extra or {})))

def _ago(ts):
    """把时间戳说成人话：刚刚 / 3 小时前 / 2 天前。"""
    d = int(time.time()) - int(ts)
    if d < 60:      return "刚刚"
    if d < 3600:    return f"{d // 60} 分钟前"
    if d < 86400:   return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"

def _vpschk_last():
    """打印上次【本机】检测的结果。没测过就说没测过。

       为什么只显示本机：外部 IP 检测查的是别人的 IP，跟这台机器的线路无关，
       混在一起显示会让人以为那是本机的结论。"""
    G, Y, R, D, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[2m", "\033[0m"
    try:
        with open(VPSCHK_LAST, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        print(f"  上次检测：{Y}还未检测过{N}")
        return
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("ts", 0)))
    print(f"  上次检测：{ts}（{_ago(d.get('ts', 0))}）　IP {G}{d.get('ip', '-')}{N}")
    routes = d.get("routes") or {}
    for car in ("电信", "联通", "移动"):
        r = routes.get(car)
        if not r:
            continue
        col, sym = (G, "●") if r.get("prem") else ((Y, "○") if r.get("norm") else (D, "·"))
        tail = f"优质 {r.get('prem', 0)} / 普通 {r.get('norm', 0)}"
        if r.get("unk"):  tail += f" / 未探到 {r['unk']}"
        if r.get("miss"): tail += f" / 没测到 {r['miss']}"
        labs = "、".join(r.get("labs") or []) or "未识别"
        print(f"    {col}{sym} {car}{N}  {col}{labs}{N}　{D}{tail}（共 {r.get('total', 0)} 点）{N}")
    if not routes:
        print(f"    {D}（上次没测到路由）{N}")
    flag = d.get("flag")
    fl = ("画像未取到" if flag is None else
          f"{R}有 proxy 标记{N}" if flag == 2 else
          f"{Y}机房 IP{N}" if flag == 1 else f"{G}无异常标记{N}")
    listed = d.get("bl_listed") or []
    bl = f"{R}命中 {len(listed)}：{'、'.join(listed)}{N}" if listed else f"{G}未命中{N}"
    if d.get("bl_unknown"):
        bl += f"{D}（{d['bl_unknown']} 项未知）{N}"
    print(f"  IP 标记：{fl}　黑名单：{bl}")

def vps_check_menu():
    """VPS 线路检测：三网回程走哪条骨干 + IP 纯净度。脚本在本仓库 vps-check.py。
       纯 stdlib、无第三方依赖；traceroute 缺了它自己 apt/yum/apk 装。"""
    print("\n" + "=" * 60)
    print("  VPS 线路检测（三网回程 + IP 纯净度）")
    print("=" * 60)
    _vpschk_last()
    print("-" * 60)
    print("  1 本机IP检测（约 2 分钟）")
    print("  2 外部IP检测（输入任意 IP，查画像 + 本机到它的链路 + 黑名单）")
    print("  0 返回")
    c = _ask("选择: ").strip()
    if c not in ("1", "2"):
        return
    arg = ""
    if c == "2":
        ip = _ask("要检测的 IP: ").strip()
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip) or any(int(x) > 255 for x in ip.split(".")):
            print("  不是合法的 IPv4 地址。"); return
        arg = " " + ip
    if not ensure_remote_script(VPSCHK_URL, VPSCHK_LOCAL):
        print("  拉取 vps-check.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {VPSCHK_LOCAL}{arg}", shell=True)

def _netopt_state():
    """读网络优化当前档位。返回 (mode, mb)：
       mode = None（未优化）/ 'fixed_cake' / 'fixed_burst' / 'adaptive'；
       mb   = 自适应激活阈值 MB/s（fixed_cake / fixed_burst 或读不到时为 None）。"""
    if not os.path.exists(NETOPT_CONFIG):
        return None, None
    mode = "adaptive"
    try:
        for ln in open(NETOPT_CONFIG):
            if ln.startswith("ADAPTIVE_QOS_MODE="):
                mode = ln.split("=", 1)[1].strip() or "adaptive"
    except OSError:
        return None, None
    if mode in ("fixed_cake", "fixed_burst"):
        return mode, None
    mb = None
    try:
        thr = int(json.load(open(NETOPT_ADAPTIVE)).get("threshold", 0))
        if thr > 0:
            mb = thr / 1048576.0
    except Exception:
        pass
    return "adaptive", mb

def _fmt_mb(mb):
    if mb is None:
        return "?"
    return str(int(round(mb))) if abs(mb - round(mb)) < 0.05 else f"{mb:.1f}"

def net_optimize_menu():
    """网络优化（本仓库 net-optimize.py：BBR/QoS/缓冲区等内核调优，依赖工具自动安装）。"""
    G, N, MARK = "\033[1;32m", "\033[0m", "  \033[1;32m← 当前\033[0m"
    while True:
        mode, mb = _netopt_state()
        is_10 = mode == "adaptive" and mb is not None and abs(mb - 10) < 0.05
        if mode is None:
            cur = "未优化（尚未设置任何档位）"
        elif mode == "fixed_cake":
            cur = "固定 cake 纯智能算法（不切换）"
        elif mode == "fixed_burst":
            cur = "纯暴力发包（固定抢带宽，无智能算法）"
        elif mb is not None:
            cur = f"自适应+抢带宽 · {_fmt_mb(mb)}MB/s 激活"
        else:
            cur = "自适应+抢带宽（阈值未知）"
        m1 = MARK if is_10 else ""
        m2 = MARK if (mode == "adaptive" and mb is not None and not is_10) \
            or mode == "fixed_burst" else ""
        m3 = MARK if mode == "fixed_cake" else ""
        print("\n" + "=" * 60)
        print("  网络优化（BBR / QoS 内核调优，依赖工具自动安装）")
        print("=" * 60)
        print(f"  当前档位: {G}{cur}{N}")
        print("-" * 60)
        print(f"  1 自适应智能算法+抢占带宽（流量 10MB/s 激活，适合内存 <1G 机器）{m1}")
        print(f"  2 自适应智能算法+抢占带宽（默认 20MB/s 激活、阈值可调；输入 0=纯暴力发包无智能算法，适合内存 2G 左右机器）{m2}")
        print(f"  3 固定 cake 纯智能算法（不切换，适合高性能机器）{m3}")
        print("  4 网络优化状况（一键检测当前优化状态）")
        print("  5 卸载网络优化（清除全部优化配置，恢复系统默认）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            _run_net_optimize()
        elif c == "2":
            t = _ask("  激活阈值 MB/s（回车=20；输入 0 = 纯暴力发包，无智能算法）: ").strip() or "20"
            try:    mb = float(t)
            except ValueError: mb = -1
            if mb == 0:
                _run_net_optimize(env_extra={"ADAPTIVE_QOS_MODE": "fixed_burst"})
            elif mb < 0:
                print("  无效数字，请输入 ≥0 的数字（如 20，或 0 = 纯暴力发包）。"); continue
            else:
                _run_net_optimize(env_extra={"ADAPTIVE_QOS_THRESHOLD": str(int(mb * 1024 * 1024))})
        elif c == "3":
            _run_net_optimize(env_extra={"ADAPTIVE_QOS_MODE": "fixed_cake"})
        elif c == "4":
            _run_net_optimize("--check")
        elif c == "5":
            ans = _ask("  确认卸载网络优化？优化写入的内核参数/服务/防火墙标记将全部清除，\n"
                       "  节点本身不受影响（建议卸载后重启一次）。y 确认 / n 返回: ").strip().lower()
            if ans in ("y", "yes"):
                _run_net_optimize("--reset")
        elif c in ("0", ""):
            return


# ============================================================================ smux 多路复用开关
def _load_sb_cfg():
    cfg = f"{SB_DIR}/config.json"
    try:
        return json.load(open(cfg)), cfg
    except Exception:
        return None, cfg

def _sb_ws_inbounds(data):
    """sing-box 配置里可开 smux 的入站：ws/httpupgrade 的 vless/vmess。"""
    return [ib for ib in data.get("inbounds", [])
            if ib.get("type") in ("vless", "vmess")
            and ib.get("transport", {}).get("type") in ("ws", "httpupgrade")]

def _link_set_smux(link, on, tags):
    """按节点名(tags)给 ws 家族链接加/去 smux 标记；名字不在 tags 里的原样返回（如 xray 的 ws）。"""
    if link.startswith("vmess://"):
        try:
            b = link[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        except Exception:
            return link
        if j.get("ps") not in tags:
            return link
        if on: j["smux"] = "1"
        else:  j.pop("smux", None)
        return vmess_link(j)
    if link.startswith("vless://"):
        head, _, frag = link.partition("#")
        if urllib.parse.unquote(frag) not in tags:
            return link
        head = head.replace("&smux=1", "")           # 先去旧标记，避免重复
        if on: head += "&smux=1"
        return head + ("#" + frag if frag else "")
    return link

def _toggle_saved_links_smux(on, tags, path="/root/xy-nodes.txt"):
    """改写保存的分享链接标记；『# 订阅链接:』尾部原样保留。"""
    try:
        lines = open(path).read().split("\n")
    except OSError:
        return
    out, tail = [], False
    for ln in lines:
        if ln.strip().startswith("#"):
            tail = True
        out.append(ln if (tail or "://" not in ln) else _link_set_smux(ln, on, tags))
    open(path, "w").write("\n".join(out))

def smux_current_state():
    """当前是否开启：sing-box ws 入站带 multiplex 即视为开；无 ws 节点返回 None（不适用）。"""
    data, _ = _load_sb_cfg()
    if not data:
        return None
    ws = _sb_ws_inbounds(data)
    if not ws:
        return None
    return any(ib.get("multiplex") for ib in ws)

# ============================================================================
# 应用新配置：sing-box 走 SIGHUP 热重载，xray 只能重启
#
# 为什么区别对待：sing-box 的 cmd/sing-box/cmd_run.go 里 SIGHUP 是这么处理的——
# 先自己 check() 一遍新配置，**不过就只记一行错、继续用旧配置跑**；过了才关掉旧实例
# 重建。而 systemctl restart 遇上坏配置是进程直接退出，再被 Restart=on-failure
# 拖进重启循环，节点全掉。同一份坏配置，热重载顶多是「没生效」，重启则是「全挂」。
#
# xray 没这个待遇：main/run.go 里只 signal.Notify 了 SIGINT 和 SIGTERM，不认 SIGHUP，
# 发过去等于没发（或按默认行为被杀），所以 xray 一律老老实实 restart。
#
# 别误会热重载能保连接：sing-box 收到 SIGHUP 还是会 instance.Close() 再重建，
# **现有连接照样断**。省掉的是进程重建、以及「新配置有错就没得救」这两件事。
RELOADABLE = {"sing-box"}
UNIT_RELOAD_LINE = "ExecReload=/bin/kill -HUP $MAINPID\n"

def _unit_can_reload(name):
    """这个服务现在能不能热重载：只对 sing-box、unit 里得有 ExecReload、且服务正在跑。

       老版本装出来的 unit 没有 ExecReload（那会儿只写了 ExecStart），这里顺手补上——
       只在缺的时候写一次，补完 daemon-reload 让 systemd 认账。补不了就照常返回 False，
       调用方退回 restart，绝不因为补 unit 失败就把「应用配置」这件事搞砸。"""
    if name not in RELOADABLE:
        return False
    try:
        up = f"/etc/systemd/system/{name}.service"
        if not os.path.exists(up):
            return False
        txt = open(up).read()
        if "ExecReload=" not in txt:
            txt = txt.replace("[Service]\n", "[Service]\n" + UNIT_RELOAD_LINE, 1)
            if "ExecReload=" not in txt:            # 没有 [Service] 段？那就不碰它
                return False
            open(up, "w").write(txt)
            sh("systemctl daemon-reload", check=False)
        return sh(f"systemctl is-active {name}", check=False) == "active"
    except Exception:
        return False

def restart_services(*names):
    """后台异步应用新配置：能热重载的热重载，不能的重启，都带 --no-block 立即返回。
       这样即便你挂着本机代理来管理、重启会掐断 SSH，操作也已在服务端完成
       （所有配置/状态必须在调用本函数之前就落盘）。

       为什么先判断能不能 reload、而不是先 reload 失败再退回 restart：--no-block 的
       命令本来就拿不到结果，而且这函数常常跑在「下一秒 SSH 就断」的处境里，没有
       第二次出手的机会。所以是**事前**决定走哪条路，一条命令定生死。"""
    todo = [n for n in names if n]
    hot = [n for n in todo if _unit_can_reload(n)]
    cold = [n for n in todo if n not in hot]
    if hot:
        sh(f"systemctl reload --no-block {' '.join(hot)}", check=False)
    if cold:
        sh(f"systemctl restart --no-block {' '.join(cold)}", check=False)

def smux_apply(on):
    """开/关 smux：改 sing-box 入站 multiplex + 同步链接标记 + 刷新订阅，最后后台重启。"""
    data, cfg = _load_sb_cfg()
    if not data:
        print("  找不到 sing-box 配置，无法切换。"); return
    ws = _sb_ws_inbounds(data)
    if not ws:
        print("  没有 ws/httpupgrade 类节点，smux 不适用。"); return
    tags = set()
    for ib in ws:
        tags.add(ib.get("tag"))
        if on: ib["multiplex"] = {"enabled": True}
        else:  ib.pop("multiplex", None)
    # 安全阀：改完先备份、校验；不过就回滚、绝不重启（单台 VPS 也不会被坏配置锁死）
    old = open(cfg).read() if os.path.exists(cfg) else None
    json.dump(data, open(cfg, "w"), indent=2)
    if os.path.exists(SB_BIN):
        ok, msg = core_check(SB_BIN, cfg)
        if not ok:
            if old is not None: open(cfg, "w").write(old)   # 回滚，核心继续按原配置运行
            print("  ✗ sing-box 配置校验未通过，已回滚、未重启（节点照常）:")
            print("   ", msg.splitlines()[-1] if msg else "校验失败"); return
    _toggle_saved_links_smux(on, tags)                # 校验通过后才动链接/订阅
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)   # 保持 token，刷新三格式订阅
    except Exception as e:
        print("  订阅刷新跳过（不影响节点）:", e)
    restart_services("sing-box")                      # 全部落盘后再后台重启，避免中途掐 SSH 导致没跑完
    print(f"\n  ✓ 已{'开启' if on else '关闭'} smux；订阅已同步，sing-box 正在后台重启（URL 不变）。")
    print("  若你挂着本机代理来管理，重启会让 SSH 瞬断，属正常——操作已在服务端完成。")
    print("  客户端重新拉取订阅、或到各配置菜单点『3 更新配置』即可生效。")

def smux_menu():
    while True:
        st = smux_current_state()
        print("\n" + "=" * 60)
        print("  多路复用开关 smux（只对 ws / httpupgrade 类协议有效）")
        print("=" * 60)
        if st is None:
            print("  本机没有 ws / httpupgrade 类 sing-box 节点，smux 不适用。")
            return
        print(f"  当前状态: {'已开启 ✓' if st else '已关闭'}")
        print("  提示: 开启后网页/小请求更顺，大文件下载/丢包线路可能变慢。")
        print("-" * 60)
        print(f"  1 smux 开关（循环检测，当前{'开' if st else '关'}，选此项切换）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            ans = _ask(f"  确认{'关闭' if st else '开启'} smux? y 确认 / n 返回: ").strip().lower()
            if ans in ("y", "yes"):
                smux_apply(not st)
        elif c in ("0", ""):
            return

# ============================================================================ 更换伪装域名（reality 借用的 SNI）
_SNI_LINK_RE = re.compile(r'([?&]sni=)[^&#]*')

def _sb_reality_inbounds(data):
    return [ib for ib in data.get("inbounds", [])
            if isinstance(ib.get("tls"), dict) and isinstance(ib["tls"].get("reality"), dict)]

def _current_sni():
    """读当前 reality 借用域名(SNI)：sing-box 优先，回落 xray；没有 reality 节点返回 ''。"""
    data, _ = _load_sb_cfg()
    if data:
        for ib in _sb_reality_inbounds(data):
            s = ib["tls"].get("server_name")
            if s:
                return s
    try:
        xd = json.load(open(f"{XRAY_DIR}/config.json"))
        for ib in xd.get("inbounds", []):
            rs = (ib.get("streamSettings") or {}).get("realitySettings") or {}
            names = rs.get("serverNames") or []
            if names:
                return names[0]
    except Exception:
        pass
    return ""

def _set_sni_singbox(data, new):
    """把 sing-box 里所有 reality 入站(+shadowtls 握手)的 SNI 改成 new，返回改动条数。"""
    n = 0
    for ib in data.get("inbounds", []):
        tls = ib.get("tls")
        if isinstance(tls, dict) and isinstance(tls.get("reality"), dict):
            tls["server_name"] = new
            hs = tls["reality"].get("handshake")
            if isinstance(hs, dict):
                hs["server"] = new
            n += 1
        if ib.get("type") == "shadowtls" and isinstance(ib.get("handshake"), dict):
            ib["handshake"]["server"] = new
            n += 1
    return n

def _set_sni_xray(data, new):
    """把 xray 里所有 reality 入站的 dest/serverNames 改成 new，返回改动条数。"""
    n = 0
    for ib in data.get("inbounds", []):
        rs = (ib.get("streamSettings") or {}).get("realitySettings")
        if isinstance(rs, dict):
            rs["dest"] = f"{new}:443"
            rs["serverNames"] = [new]
            n += 1
    return n

def _links_set_sni(new, path=NODE_FILE):
    """改写保存的分享链接里 reality 节点的 sni=（只动带 security=reality 的，域名类节点不误伤）；
       『# 订阅链接:』尾部原样保留。"""
    try:
        lines = open(path).read().split("\n")
    except OSError:
        return
    out, tail = [], False
    for ln in lines:
        if ln.strip().startswith("#"):
            tail = True
        if not tail and "://" in ln and "security=reality" in ln:
            ln = _SNI_LINK_RE.sub(lambda m: m.group(1) + new, ln)
        out.append(ln)
    open(path, "w").write("\n".join(out))

def _nginx_split_set_sni(old, new):
    """sni-split(443 分流)启用时，把 stream map 里旧 SNI 的那条映射改成新 SNI。改了返回 True。"""
    if not old or not os.path.exists(NGINX_STREAM_CONF):
        return False
    try:
        txt = open(NGINX_STREAM_CONF).read()
    except OSError:
        return False
    new_txt = re.sub(rf'(?m)^(\s*){re.escape(old)}(\s+127\.0\.0\.1:)',
                     rf'\g<1>{new}\g<2>', txt)
    if new_txt == txt:
        return False
    open(NGINX_STREAM_CONF, "w").write(new_txt)
    return True

def change_sni_apply(new):
    """把 reality 借用域名换成 new：改两核心配置 + nginx 分流 + 链接 + 订阅，
       任一核心校验不过就整体回滚、不重启（单台 VPS 也不会被坏配置锁死）。"""
    old = _current_sni()
    sbcfg, xrcfg = f"{SB_DIR}/config.json", f"{XRAY_DIR}/config.json"
    items = []                                            # (cfg, bin, svc, old_text)
    if os.path.exists(sbcfg):
        try: data = json.load(open(sbcfg))
        except Exception: data = None
        if data and _set_sni_singbox(data, new):
            old_text = open(sbcfg).read()
            json.dump(data, open(sbcfg, "w"), indent=2)
            items.append((sbcfg, SB_BIN, "sing-box", old_text))
    if os.path.exists(xrcfg):
        try: xd = json.load(open(xrcfg))
        except Exception: xd = None
        if xd and _set_sni_xray(xd, new):
            old_text = open(xrcfg).read()
            json.dump(xd, open(xrcfg, "w"), indent=2)
            items.append((xrcfg, XRAY_BIN, "xray", old_text))
    if not items:
        print("  没找到 reality 节点，无需更换伪装域名。"); return False
    errors = []
    for cfg, binp, svc, _ in items:
        if os.path.exists(binp):
            ok, msg = core_check(binp, cfg)
            if not ok:
                errors.append((svc, msg))
    if errors:
        for cfg, binp, svc, old_text in items:            # 任一不过 → 全回滚
            open(cfg, "w").write(old_text)
        print("  ✗ 配置校验未通过，已回滚、未改动（节点照常）:")
        for svc, msg in errors:
            print(f"    {svc}: {msg.splitlines()[-1] if msg else '校验失败'}")
        return False
    # nginx sni-split（如启用）：改 map → nginx -t，不过则连核心配置一起回滚
    if _nginx_split_set_sni(old, new):
        chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
        if chk.returncode:
            _nginx_split_set_sni(new, old)
            for cfg, binp, svc, old_text in items:
                open(cfg, "w").write(old_text)
            print("  ✗ nginx 分流校验未通过，已整体回滚：\n   "
                  + (chk.stderr or chk.stdout).strip().replace("\n", "\n   ")); return False
        sh("systemctl reload nginx", check=False)
    _links_set_sni(new)                                   # 校验都过了才动链接/订阅
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)
    except Exception as e:
        print("  订阅刷新跳过（不影响节点）:", e)
    restart_services(*[svc for _, _, svc, _ in items])
    return True

def _choose_new_sni(cur):
    print("  1 随机挑一个（内置大站池，自动避开当前）   2 手动输入   0 取消")
    c = _ask("  选择: ").strip()
    if c == "1":
        pool = [s for s in REALITY_SNI_POOL if s != cur]
        return secrets.choice(pool) if pool else None
    if c == "2":
        s = _ask("  输入域名（如 www.microsoft.com）: ").strip().lower().rstrip(".")
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", s):
            print("  域名格式不对。"); return None
        return s
    return None

def change_sni_menu():
    G_, Y_, R_, N_ = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    while True:
        cur = _current_sni()
        print("\n" + "=" * 60)
        print("  更换伪装域名（reality 借用的 SNI 目标站）")
        print("=" * 60)
        if not cur:
            print("  本机没有 reality 类节点（reality-*），没有伪装域名可换。")
            return
        print(f"  当前伪装域名: {G_}{cur}{N_}")
        ok, detail = _reality_sni_ok(cur)                 # 从本机实连一下：连通 + TLS1.3 + h2
        if ok:
            print(f"  连通性检测: {G_}通 · {detail}{N_}")
        else:
            print(f"  连通性检测: {R_}不通 · {detail}{N_}")
            print(f"  {Y_}↑ 你 VPS 连不上/这个站不合格，reality 伪装会打折，建议更换。{N_}")
        print("-" * 60)
        print("  1 更换（随机挑 / 手动输入）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            new = _choose_new_sni(cur)
            if not new or new == cur:
                if new == cur:
                    print("  和当前一样，未改动。")
                continue
            ok2, detail2 = _reality_sni_ok(new)
            if ok2:
                print(f"  新域名 {new}: {G_}通 · {detail2}{N_}")
            else:
                print(f"  新域名 {new}: {R_}不通 · {detail2}{N_}")
                if _ask("  这个站从你 VPS 检测不理想，仍要用? y 继续 / n 重选: ").strip().lower() \
                        not in ("y", "yes"):
                    continue
            if _ask(f"  确认把伪装域名从 {cur} 换成 {new}? y 确认 / n 取消: ").strip().lower() \
                    in ("y", "yes"):
                if change_sni_apply(new):
                    print(f"\n  ✓ 已更换为 {new}，配置 + 订阅已刷新，核心正在后台重启（订阅 URL 不变）。")
                    print("  客户端重新拉取订阅即可生效——无需重装、无需重新导入。")
                    print("  若你挂着本机代理来管理，重启会让 SSH 瞬断，属正常，操作已在服务端完成。")
        elif c in ("0", ""):
            return

# ============================================================================ BT/PT 下载屏蔽
def bt_enabled():
    try: return bool(json.load(open(BT_STATE)).get("enabled"))
    except Exception: return False

def bt_set(on):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump({"enabled": bool(on)}, open(BT_STATE, "w"))

def _is_bt_sb_rule(r):
    """识别本脚本注入的 sing-box BT 规则：裸 sniff 头，或命中 bittorrent 的 reject。
       只认这两类，cn-block 的 rule_set 规则不会误伤（互相保留）。"""
    if r.get("action") == "sniff" and set(r.keys()) == {"action"}:
        return True
    p = r.get("protocol")
    return bool(p and "bittorrent" in (p if isinstance(p, list) else [p]))

def _bt_apply_singbox(on):
    cfg = f"{SB_DIR}/config.json"
    try: conf = json.load(open(cfg))
    except Exception: return False
    route = conf.get("route") or {}
    rules = [r for r in route.get("rules", []) if not _is_bt_sb_rule(r)]   # 先剥旧 BT 规则，保留 cn-block 等
    if on:
        rules = [{"action": "sniff"}, {"protocol": ["bittorrent"], "action": "reject"}] + rules
    if rules: route["rules"] = rules
    else:     route.pop("rules", None)
    if route: conf["route"] = route
    else:     conf.pop("route", None)
    json.dump(conf, open(cfg, "w"), indent=2)
    return True

def _xr_inbound_is_vision(ib):
    s = ib.get("settings")
    if not isinstance(s, dict): return False
    cl = s.get("clients") or [{}]
    return "vision" in str(cl[0].get("flow", "")) if cl else False

def _bt_apply_xray(on):
    cfg = f"{XRAY_DIR}/config.json"
    try: conf = json.load(open(cfg))
    except Exception: return False
    for ib in conf.get("inbounds", []):
        # vision 流上开 sniffing 会干扰它，跳过；其余用 routeOnly 安全嗅探（只影响路由、不改目的地）
        if on and not _xr_inbound_is_vision(ib):
            ib["sniffing"] = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
        else:
            ib.pop("sniffing", None)
    routing = conf.get("routing") or {}
    rules = [r for r in routing.get("rules", [])
             if not (r.get("protocol") and "bittorrent" in r["protocol"])]
    if on:
        rules = [{"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"}] + rules
    if rules: routing["rules"] = rules
    else:     routing.pop("rules", None)
    if routing: conf["routing"] = routing
    else:       conf.pop("routing", None)
    json.dump(conf, open(cfg, "w"), indent=2)
    return True

def bt_apply(on):
    """开/关 BT 屏蔽（两核心 all-or-nothing）：改 config → 各自校验 → 全过才落盘状态 + 后台重启；
       任一不过则两核心全回滚、不重启（单台 VPS 也不会被坏配置锁死）。
       返回 (成功的核心列表, [(核心, 错误信息)])。"""
    items = []   # (cfg, binpath, svc, old_text)
    sbcfg, xrcfg = f"{SB_DIR}/config.json", f"{XRAY_DIR}/config.json"
    if os.path.exists(sbcfg):
        old = open(sbcfg).read(); _bt_apply_singbox(on)
        items.append((sbcfg, SB_BIN, "sing-box", old))
    if os.path.exists(xrcfg):
        old = open(xrcfg).read(); _bt_apply_xray(on)
        items.append((xrcfg, XRAY_BIN, "xray", old))
    errors = []
    for cfg, binp, svc, _ in items:
        if os.path.exists(binp):
            ok, msg = core_check(binp, cfg)
            if not ok: errors.append((svc, msg))
    if errors:
        for cfg, binp, svc, old in items:       # 任一不过 → 全回滚，核心继续按原配置运行
            open(cfg, "w").write(old)
        return [], errors
    bt_set(on)                                  # 全过：状态先落盘（即便随后 SSH 断，状态也已正确）
    restart_services(*[svc for _, _, svc, _ in items])
    return [svc for _, _, svc, _ in items], []

def bt_reapply():
    """重装重写 config 后，若之前开过 BT 屏蔽就重新注入（在 cn-block 之后调，二者互不覆盖）。"""
    if bt_enabled():
        _, errors = bt_apply(True)
        if errors:
            print("BT 屏蔽重注入校验未过、已跳过（不影响节点）:",
                  (errors[0][1].splitlines()[-1] if errors[0][1] else ""))

def bt_menu():
    while True:
        on = bt_enabled()
        if not (os.path.exists(f"{SB_DIR}/config.json") or os.path.exists(f"{XRAY_DIR}/config.json")):
            print("\n还没有节点，请先『1.安装』。"); return
        print("\n" + "=" * 60)
        print("  BT/PT 下载屏蔽（防 VPS 因 BT 流量被投诉封机）")
        print("=" * 60)
        print(f"  当前状态: {'已开启 ✓' if on else '已关闭'}")
        print("  说明: 服务端识别到 BT/PT 流量即拒绝；best-effort，vision 流可能漏一小部分。")
        print("-" * 60)
        print(f"  1 BT/PT 屏蔽开关（循环检测，当前{'开' if on else '关'}，选此项切换）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            ans = _ask(f"  确认{'关闭' if on else '开启'} BT 屏蔽? y 确认 / n 返回: ").strip().lower()
            if ans in ("y", "yes"):
                did, errors = bt_apply(not on)
                if errors:
                    print("  ✗ 配置校验未通过，已回滚、未重启（核心仍按原配置运行，未被锁死）:")
                    for svc, msg in errors:
                        print(f"    {svc}: {msg.splitlines()[-1] if msg else '校验失败'}")
                else:
                    print(f"  ✓ 已{'关闭' if on else '开启'} BT 屏蔽（状态已保存，{('、'.join(did)) or '无核心'} 正在后台重启）。")
                    print("  若你挂着本机代理来管理，重启会让 SSH 瞬断，属正常——设置已生效。")
        elif c in ("0", ""):
            return

# ---------------------------------------------------------------------------- 流量统计（主菜单顶部展示）
def _fmt_traffic(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or u == "PB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024

def _main_iface():
    out = sh("ip -4 route get 1.1.1.1 2>/dev/null || ip route show default", check=False)
    m = re.search(r"\bdev\s+(\S+)", out)
    return m.group(1) if m else ""

def _vnstat_stats(iface):
    """vnstat 2.x：返回 (月rx, 月tx, 日rx, 日tx) 字节；不可用/没数据返回 None。"""
    if not have("vnstat"):
        return None
    sel = f"-i {iface} " if iface else ""
    try:
        mo = json.loads(sh(f"vnstat {sel}--json m 1", check=False))
        mo = mo["interfaces"][0]["traffic"]["month"][-1]
        dy = json.loads(sh(f"vnstat {sel}--json d 1", check=False))
        dy = dy["interfaces"][0]["traffic"]["day"][-1]
        return mo["rx"], mo["tx"], dy["rx"], dy["tx"]
    except Exception:
        return None

def _vnstat_bg_install():
    """后台静默装 vnstat（只试一次），装好后主菜单显示 本月/今日 流量。"""
    marker = BGP_DIR + "/.vnstat_tried"
    if have("vnstat") or os.path.exists(marker) or not have("apt-get"):
        return
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        open(marker, "w").write("")
        subprocess.Popen(
            "DEBIAN_FRONTEND=noninteractive apt-get install -y vnstat >/dev/null 2>&1 && "
            "systemctl enable --now vnstat >/dev/null 2>&1",
            shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

TRAFFIC_FILE = BGP_DIR + "/traffic.json"   # 流量套餐设置：重置日/配额/计费方式/校准

def _traffic_cfg():
    try: return json.load(open(TRAFFIC_FILE))
    except Exception: return {}

def _cycle_start(reset_day, today):
    """机房账单周期的起点（重置日超出当月天数时取月末，如 31 号遇 2 月）。"""
    import calendar, datetime
    d = min(reset_day, calendar.monthrange(today.year, today.month)[1])
    if today.day >= d:
        return datetime.date(today.year, today.month, d)
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return datetime.date(y, m, min(reset_day, calendar.monthrange(y, m)[1]))

def _vnstat_cycle_usage(iface, start, mode):
    """从 vnstat 日表累加账单周期用量（字节）。

       mode: sum=双向相加 / max=单向取大 / out=只计出站 / in=只计入站。

       「只计出站」不是「单向取大」：不少机房（含一部分日本/欧洲小鸡）只按上传计费，
       下载白送。这种机器上 max 会取到下载那一边——媒体库探测、镜像拉取这类活儿
       下载远大于上传，于是面板报出十几倍于真实账单的数字，看着像要爆了，其实离
       配额还差得远。反过来只计入站的也有（少见），一并支持。"""
    import datetime
    sel = f"-i {iface} " if iface else ""
    j = json.loads(sh(f"vnstat {sel}--json d 62", check=False))
    rx = tx = 0
    for e in j["interfaces"][0]["traffic"]["day"]:
        dt = datetime.date(e["date"]["year"], e["date"]["month"], e["date"]["day"])
        if dt >= start:
            rx += e["rx"]; tx += e["tx"]
    return {"sum": rx + tx, "max": max(rx, tx), "out": tx, "in": rx}.get(mode, rx + tx)

def traffic_setup():
    """设置流量套餐：机房重置日 / 月配额 / 计费方式 / 一次性校准到机房当前读数。"""
    cfg = _traffic_cfg()
    print("\n  按机房账单口径显示流量（都可回车跳过）：")
    d = _ask(f"  每月流量重置日 1-31（回车={cfg.get('reset_day', 1)}）: ").strip()
    try: reset_day = min(max(int(d), 1), 31) if d else int(cfg.get("reset_day", 1))
    except ValueError: reset_day = int(cfg.get("reset_day", 1))
    q = _ask(f"  月流量配额 GB（回车={cfg.get('quota_gb') or '不设，只显示用量'}）: ").strip()
    try: quota = float(q) if q else cfg.get("quota_gb")
    except ValueError: quota = cfg.get("quota_gb")
    _MODES = {"1": "sum", "2": "max", "3": "out", "4": "in"}
    _cur = {v: k for k, v in _MODES.items()}.get(cfg.get("mode", "sum"), "1")
    print("  计费方式：1 双向相加   2 单向取大   3 只计出站(上传)   4 只计入站(下载)")
    print("            只按上传计费的机房选 3——那种机器上选 2 会取到下载那一边，")
    print("            面板数字会比真实账单大很多倍。")
    m = _ask(f"  选择 1-4（回车={_cur}）: ").strip()
    mode = _MODES.get(m or _cur, "sum")
    cfg.update({"reset_day": reset_day, "quota_gb": quota, "mode": mode})

    # 校准：vnstat 只统计装机之后的量，本周期装机前的用量抄一次机房面板即可对齐；
    # 差值只在本周期内生效，下个重置日起 vnstat 数据完整、自动归零。
    c = _ask("  校准：机房面板当前显示的已用量 GB（回车不校准）: ").strip()
    if c:
        try:
            import datetime
            start = _cycle_start(reset_day, datetime.date.today())
            now_used = _vnstat_cycle_usage(_main_iface(), start, mode)
            cfg["calib_bytes"] = int(float(c) * 1024 ** 3) - now_used
            cfg["calib_cycle"] = start.isoformat()
            print("  ✓ 已校准到机房读数（下个重置日起自动改用本机完整统计）。")
        except Exception:
            print("  ✗ 校准失败（vnstat 可能还没就绪），套餐设置已保存，可稍后再校准。")
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(cfg, open(TRAFFIC_FILE, "w"))
    print("  ✓ 已保存。面板顶部将按「周期已用/配额 + 重置日」显示。")

def traffic_line():
    """主菜单顶部的流量行。设置过套餐（重置日/配额）→ 按机房账单周期显示；
       否则 vnstat 本月/今日；vnstat 没装 → 内核计数兜底。失败返回 ''，不影响面板。"""
    try:
        iface = _main_iface()
        if not iface:
            return ""
        cfg = _traffic_cfg()
        if have("vnstat") and cfg.get("reset_day"):
            import datetime
            start = _cycle_start(int(cfg["reset_day"]), datetime.date.today())
            used = _vnstat_cycle_usage(iface, start, cfg.get("mode", "sum"))
            if cfg.get("calib_cycle") == start.isoformat():   # 校准只在本周期生效
                used = max(used + int(cfg.get("calib_bytes", 0)), 0)
            tag = {"sum": "双向", "max": "单向取大",
                   "out": "只计出站", "in": "只计入站"}.get(cfg.get("mode", "sum"), "双向")
            quota = cfg.get("quota_gb")
            if quota:
                left = max(quota * 1024 ** 3 - used, 0)
                _t = tag + ("计" if tag in ("双向", "单向取大") else "")
                return (f"  📊 本周期已用: {_fmt_traffic(used)} / {quota:g} GB"
                        f"（剩 {_fmt_traffic(left)}，每月 {cfg['reset_day']} 号重置，{_t}）")
            _t = tag + ("计" if tag in ("双向", "单向取大") else "")
            return (f"  📊 本周期已用: {_fmt_traffic(used)}"
                    f"（每月 {cfg['reset_day']} 号重置，{_t}，{iface}）")
        v = _vnstat_stats(iface)
        if v:
            mrx, mtx, drx, dtx = v
            return (f"  📊 本月流量: ↑{_fmt_traffic(mtx)} ↓{_fmt_traffic(mrx)}"
                    f"   今日: ↑{_fmt_traffic(dtx)} ↓{_fmt_traffic(drx)}（输 t 按机房周期显示）")
        rx = int(open(f"/sys/class/net/{iface}/statistics/rx_bytes").read())
        tx = int(open(f"/sys/class/net/{iface}/statistics/tx_bytes").read())
        tried = os.path.exists(BGP_DIR + "/.vnstat_tried")   # 已试过装 → 不再重复宣称"安装中"
        _vnstat_bg_install()
        hint = "" if tried else "；vnstat 正在后台安装，装好后按 本月/今日 统计"
        return (f"  📊 流量(开机以来): ↑{_fmt_traffic(tx)} ↓{_fmt_traffic(rx)}（{iface}{hint}）")
    except Exception:
        return ""

# ---------------------------------------------------------------------------- CDN 套用（防 IP 被墙）
# 支持多条：state 是节点列表，每条一套独立自签证书 + 配置 + systemd 服务（xy-cdn-<id>）。
def _cdn_load():
    """返回 CDN 节点列表。兼容旧单节点 dict 格式（迁移为列表，服务名沿用旧 xy-cdn）。"""
    try:
        data = json.load(open(CDN_STATE))
    except Exception:
        return []
    if isinstance(data, dict):                            # 旧格式：单节点 → 包成列表
        data.setdefault("id", 1)
        data.setdefault("svc", CDN_SVC)
        data.setdefault("crt", CDN_CRT); data.setdefault("key", CDN_KEY)
        data.setdefault("conf", CDN_CONF)
        return [data]
    return data

def _cdn_save(nodes):
    os.makedirs(CDN_DIR, exist_ok=True)
    json.dump(nodes, open(CDN_STATE, "w"), ensure_ascii=False)

def _cdn_next_id(nodes):
    return max([n.get("id", 0) for n in nodes], default=0) + 1

def _cdn_node_paths(nid):
    return (f"{CDN_DIR}/{nid}.crt", f"{CDN_DIR}/{nid}.key",
            f"{CDN_DIR}/{nid}.json", f"xy-cdn-{nid}")

def _cdn_selfsigned(domain, crt, key):
    """源站自签证书即可：CF 走 Full 模式不校验源站证书，客户端看到的是 CF 的有效证书。"""
    os.makedirs(CDN_DIR, exist_ok=True)
    sh(f"openssl ecparam -genkey -name prime256v1 -out {key}")
    sh(f'openssl req -new -x509 -days 3650 -key {key} -out {crt} -subj "/CN={domain}"')

def _cdn_pick_port(used):
    """从 CF 可代理端口里挑一个未占用的；用尽（>5 条）返回 0。"""
    for p in CDN_PORTS:
        if p not in used and port_free(p):
            return p
    return 0

# CDN 可选协议 → 能写进哪些订阅格式。xhttp 只有 mihomo 认，sing-box/小火箭自动跳过。
CDN_PROTOS = {"1": "vless-ws", "2": "vless-xhttp", "3": "vmess-ws", "4": "trojan-ws"}
CDN_PROTO_SUB = {
    "vless-ws":    "mihomo / sing-box / 小火箭（全支持）",
    "vmess-ws":    "mihomo / sing-box / 小火箭（全支持）",
    "trojan-ws":   "mihomo / sing-box / 小火箭（全支持）",
    "vless-xhttp": "仅 mihomo（sing-box/小火箭不支持 xhttp，写入时自动跳过）",
}

def _cdn_addr(st):
    """分享链接【地址位】：填了优选地址就用它，否则回落到真域名（老节点/未优选即原行为）。"""
    return (st.get("pref") or "").strip() or st["domain"]

def _cdn_link(st):
    """地址位 = 优选地址（或真域名），sni/host 恒为真域名——CF 靠 Host 头回源，
       所以换成任意 CF 边缘 IP/优选域名都能连回同一个源站，服务端无需改动。"""
    proto = st.get("proto", "vless-ws")
    cred = st.get("cred") or st.get("uuid", "")
    dom, port, path, tag = st["domain"], st["cf_port"], st["path"], st["tag"]
    addr = _cdn_addr(st)
    if proto == "vless-xhttp":
        return (f"vless://{cred}@{addr}:{port}?encryption=none&security=tls&sni={dom}"
                f"&host={dom}&type=xhttp&path={path}&fp=chrome#{tag}")
    if proto == "vmess-ws":
        return vmess_link({"v": "2", "ps": tag, "add": addr, "port": str(port), "id": cred,
                           "aid": "0", "net": "ws", "type": "none", "host": dom,
                           "path": path, "tls": "tls", "sni": dom})
    if proto == "trojan-ws":
        return (f"trojan://{cred}@{addr}:{port}?security=tls&sni={dom}"
                f"&type=ws&host={dom}&path={path}&fp=chrome#{tag}")
    return (f"vless://{cred}@{addr}:{port}?encryption=none&security=tls&sni={dom}"
            f"&type=ws&host={dom}&path={path}&fp=chrome#{tag}")

def _cdn_intro():
    """进 CDN 菜单顶部的简短说明（原理 + 执行前三步）。"""
    print("  防 IP 被墙：域名套 Cloudflare 中转，客户端连的是 CF 的 IP，本机真 IP 被墙也能用。")
    print("  嫌慢就用【优选地址】（菜单 2）：换更快的 CF 边缘，或筛一批候选交给客户端自己选。")
    print("  执行前：① 域名解析绑到本机 IP、开【橙色云】代理（必须橙云）；"
          "② VPS 放行端口 2053/2083/2087/2096/8443（商用 VPS 一般全开放）；"
          "③ CF 的 SSL/TLS 模式选【Full 完全】。")

def _state_prefix():
    """读安装时用的名称前缀（state.json），CDN 节点默认沿用它。"""
    try: return json.load(open(STATE_FILE)).get("prefix", "")
    except Exception: return ""

def _cdn_config(proto, core, cred, path, port, domain, crt, key):
    """按协议+核心生成 CDN 节点的 (config_dict, binpath)。cred=uuid(vless/vmess)或password(trojan)。"""
    if core == "xray":
        xr_tls = {"certificates": [{"certificateFile": crt, "keyFile": key}]}
        if proto == "vless-xhttp":
            stream = {"network": "xhttp", "security": "tls",
                      "xhttpSettings": {"path": path}, "tlsSettings": xr_tls}
        else:
            stream = {"network": "ws", "security": "tls",
                      "wsSettings": {"path": path}, "tlsSettings": xr_tls}
        if proto.startswith("vless"):
            ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": "cdn-in",
                  "settings": {"clients": [{"id": cred}], "decryption": "none"},
                  "streamSettings": stream}
        elif proto == "vmess-ws":
            ib = {"listen": "0.0.0.0", "port": port, "protocol": "vmess", "tag": "cdn-in",
                  "settings": {"clients": [{"id": cred}]}, "streamSettings": stream}
        else:  # trojan-ws
            ib = {"listen": "0.0.0.0", "port": port, "protocol": "trojan", "tag": "cdn-in",
                  "settings": {"clients": [{"password": cred}]}, "streamSettings": stream}
        return ({"log": {"loglevel": "warning"}, "inbounds": [ib],
                 "outbounds": [{"protocol": "freedom"}]}, XRAY_BIN)
    # sing-box（不支持 xhttp 入站，xhttp 已在上层强制走 xray）
    sb_tls = {"enabled": True, "server_name": domain,
              "certificate_path": crt, "key_path": key}
    tr = {"type": "ws", "path": path}
    if proto.startswith("vless"):
        ib = {"type": "vless", "tag": "cdn-in", "listen": "::", "listen_port": port,
              "users": [{"uuid": cred}], "tls": sb_tls, "transport": tr}
    elif proto == "vmess-ws":
        ib = {"type": "vmess", "tag": "cdn-in", "listen": "::", "listen_port": port,
              "users": [{"uuid": cred, "alterId": 0}], "tls": sb_tls, "transport": tr}
    else:  # trojan-ws
        ib = {"type": "trojan", "tag": "cdn-in", "listen": "::", "listen_port": port,
              "users": [{"password": cred}], "tls": sb_tls, "transport": tr}
    return ({"log": {"level": "info"}, "inbounds": [ib],
             "outbounds": [{"type": "direct"}]}, SB_BIN)

def _parse_cdn_protos(raw):
    """协议多选解析：回车=vless-ws；0/all=全部 4 种；否则按逗号分隔编号取，去重保序。"""
    raw = raw.strip().replace("，", ",")
    if raw == "":
        return ["vless-ws"]
    if raw.lower() in ("0", "all", "a"):
        return list(CDN_PROTOS.values())
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok in CDN_PROTOS and CDN_PROTOS[tok] not in out:
            out.append(CDN_PROTOS[tok])
    return out

def _cdn_build_one(nodes, proto, core, domain, prefix, pref=""):
    """建一条 CDN 节点（独立证书/配置/服务/端口），成功返回 node、失败返回 None。
       pref=优选地址，只影响分享链接的地址位，服务端配置与它无关。"""
    port = _cdn_pick_port({n["cf_port"] for n in nodes})
    if not port:
        return None
    nid = _cdn_next_id(nodes)
    crt, key, conf, svc = _cdn_node_paths(nid)
    _cdn_selfsigned(domain, crt, key)
    cred = new_pw() if proto == "trojan-ws" else new_uuid()
    path = "/" + secrets.token_hex(4)
    cfg_dict, binpath = _cdn_config(proto, core, cred, path, port, domain, crt, key)
    os.makedirs(CDN_DIR, exist_ok=True)
    json.dump(cfg_dict, open(conf, "w"), indent=2)
    sh(f"systemctl disable --now {svc}", check=False)
    sh(f"rm -f /etc/systemd/system/{svc}.service", check=False)
    sh("systemctl daemon-reload", check=False)
    try:
        write_service(svc, binpath, conf)
    except RuntimeError as e:
        print(f"  ✗ {proto} 服务启动失败（跳过）：", e)
        for p in (crt, key, conf):                        # 起不来就把这条的残留文件清掉
            try: os.remove(p)
            except OSError: pass
        return None
    node = {"id": nid, "proto": proto, "core": core, "domain": domain, "cred": cred,
            "path": path, "cf_port": port, "tag": _tag(prefix, f"CDN·{proto}"), "svc": svc,
            "crt": crt, "key": key, "conf": conf, "in_sub": False, "pref": pref}
    nodes.append(node)                                    # 立即并入，供下一条挑端口/id 避重
    return node

def _cdn_wipe_all(nodes):
    """清空全部 CDN 节点（先撤订阅、停服务删单元、清目录/状态）。"""
    if any(n.get("in_sub") for n in nodes):              # 含候选链接，漏撤会在订阅里留死节点
        try: _cdn_sub_apply(remove_links=_cdn_state_links(nodes, _pref_load()))
        except Exception: pass
    for n in nodes:
        _cdn_drop(n)
    sh("systemctl daemon-reload", check=False)
    shutil.rmtree(CDN_DIR, ignore_errors=True)
    try: os.remove(CDN_STATE)
    except OSError: pass

def cdn_add():
    """CDN 节点安装：协议可多选、一次装多条；已装则问清空重装 / 追加 / 返回。"""
    print("\n" + "=" * 60)
    print("  CDN 节点安装（域名 + Cloudflare 中转，防 IP 被墙时续命）")
    print("=" * 60)
    print("  原理：客户端连 Cloudflare 的 IP、不是你 VPS 的 IP；VPS 真 IP 被墙也能用。")
    print("  前提：一个域名，且能挂到 Cloudflare（免费版就行）。")
    print("-" * 60)
    nodes = _cdn_load()
    if nodes:                                             # 已安装 → 问怎么处理
        print(f"  检测到已安装 {len(nodes)} 条 CDN 节点。")
        ans = _ask("  y 清空重装（先删现有再装新的）/ a 追加新增 / 回车返回: ").strip().lower()
        if ans in ("y", "yes"):
            _cdn_wipe_all(nodes); nodes = []
            print("  ✓ 已清空旧 CDN 节点，开始全新安装。")
        elif ans in ("a", "add"):
            pass                                          # 追加，保留现有
        else:
            print("  已返回。"); return
    free = len(CDN_PORTS) - len(nodes)
    if free <= 0:
        print("  CF 可代理端口已用尽（最多 5 条 CDN 节点）。先卸载一条再加。"); return
    domain = _ask("  输入用于 CDN 的域名（如 node.example.com，回车取消）: ").strip().lower()
    if not domain:
        print("  已取消。"); return
    if "." not in domain or "/" in domain or " " in domain:
        print("  域名格式不对，已取消。"); return

    print("  选协议: 1 VLESS+WS(默认·最稳) / 2 VLESS+XHTTP(最快) / 3 VMess+WS / 4 Trojan+WS")
    protos = _parse_cdn_protos(_ask("  选择(回车=1；可多选，逗号分隔如 1,3,4；a=全部): "))
    if not protos:
        print("  没选到有效协议，已取消。"); return
    if len(protos) > free:
        print(f"  当前只剩 {free} 个可用端口，只装前 {free} 个：{protos[:free]}")
        protos = protos[:free]

    # 核心：只对非 xhttp 的协议问一次（xhttp 强制 xray）；多选里混了 xhttp 会自动分别用对的核心
    core_choice = "sing-box"
    if any(p != "vless-xhttp" for p in protos):
        core_choice = "xray" if _ask("  非 XHTTP 的用哪个核心? 1 sing-box(默认) / 2 xray: ").strip() == "2" else "sing-box"
    if "vless-xhttp" in protos:
        print("  （XHTTP 入站仅 xray 支持，那条自动用 xray）")

    ipfx = _state_prefix()
    if ipfx:
        prefix = _ask_free(f"  节点名称前缀（回车=沿用安装前缀「{ipfx}」，或输入自定义）：").strip() or ipfx
    else:
        prefix = _ask_free("  节点名称前缀（回车=默认 CDN，自定义如 🇯🇵/家宽）：").strip()

    # 追加时新节点沿用已设的优选地址；首装留空——装完统一问要不要筛一批候选
    pref = next((n.get("pref") for n in nodes if n.get("pref")), "")

    # 需要的核心先各下载一次（避免循环里重复打印下载）
    for cr in {("xray" if p == "vless-xhttp" else core_choice) for p in protos}:
        binp = XRAY_BIN if cr == "xray" else SB_BIN
        if not os.path.exists(binp):
            print(f"  正在下载 {cr} 内核（CDN 备用节点用）...")
            try: (install_xray if cr == "xray" else install_singbox)()
            except Exception as e:
                print(f"  ✗ {cr} 下载失败：", e); return

    created = []
    for proto in protos:
        core = "xray" if proto == "vless-xhttp" else core_choice
        node = _cdn_build_one(nodes, proto, core, domain, prefix, pref)
        if node:
            created.append(node)
    if not created:
        print("  ✗ 没有成功新增的节点。"); return
    _cdn_save(nodes)
    ports = "、".join(str(n["cf_port"]) for n in created)
    print(f"\n  ✓ 新增成功 {len(created)} 条（共 {len(nodes)} 条，各自独立服务、与主节点互不影响）。")
    print(f"  记得在 CF 把域名 {created[0]['domain']} 绑到本机 IP、开橙云，VPS 放行端口：{ports}")
    print("  （详细步骤见本菜单顶部说明）。")
    if pref:
        print(f"  优选地址：沿用 {pref}（链接地址位已换成它，SNI/Host 仍是 {created[0]['domain']}）")
    print("\n  ▼ 本次新增的备用链接（导入客户端用；平时留着不用即可）:")
    for i, n in enumerate(created, 1):
        print(f"  {i}. {_cdn_link(n)}")

    # 装完顺手筛一批优选候选：CDN 走 CF 任播，默认解析到哪个边缘全看运气，往往又慢又挤。
    # 先筛一批写进来，让客户端 URLTest 自己挑最快的；以后想换就进菜单 2「优选地址」。
    cfg = _pref_load()
    print("\n" + "-" * 60)
    print("  ▼ 优选候选：现在筛一批更快的 CF 边缘吗？")
    print("    默认解析到的边缘全看运气，实测常比优选后慢好几倍。筛出来的多条候选一起写进")
    print("    订阅，最终由客户端自己挑最快的那条——只有客户端测得到你这边到 CF 的真实延迟。")
    print(f"    代价：下载测速最多耗 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量，约一两分钟。")
    if (_ask("  回车=筛（推荐） / n=跳过: ").strip().lower() or "y") in ("n", "no"):
        print("  已跳过。想筛随时进菜单 2「优选地址」→ 1。")
        return
    cdn_pref_scan(ask=False)

# --- 把 CDN 节点写入/移出订阅（改 /root/xy-nodes.txt 节点段 + 刷新三格式）---
def _node_file_parts():
    """返回 (节点链接list, 尾部注释块str)。read_saved_links 只读到 # 为止，这里保留尾部。"""
    links, tail = [], ""
    try:
        raw = open(NODE_FILE).read().splitlines(keepends=True)
    except OSError:
        return links, tail
    for i, l in enumerate(raw):
        if l.lstrip().startswith("#"):
            tail = "".join(raw[i:]); break
        if "://" in l:
            links.append(l.strip())
    return links, tail

def _cdn_sub_apply(remove_links=(), add_links=()):
    """从节点文件移除 remove_links、加入 add_links（全整条匹配，去重），再刷新订阅。
       没有主节点也能生成——此时订阅仅含 CDN 节点。"""
    links, tail = _node_file_parts()
    remove_set = set(l for l in remove_links if l)
    keep = [l for l in links if l not in remove_set]
    for al in add_links:
        if al and al not in keep:
            keep.append(al)
    os.makedirs(os.path.dirname(NODE_FILE) or ".", exist_ok=True)
    with open(NODE_FILE, "w") as f:
        f.write("\n".join(keep) + ("\n" if keep else ""))
        if tail:
            f.write(tail if tail.startswith("\n") else "\n" + tail)
    G["host"] = _host()
    build_subscription(read_saved_links(), new_token=False)   # 保持 token，刷新三格式

def cdn_write_sub():
    nodes = _cdn_load()
    if not nodes:
        print("  还没配置 CDN 节点，先选 1 新增。"); return
    total = len(nodes); already = sum(1 for n in nodes if n.get("in_sub"))
    writing = already < total                            # 未全部写入 → 写入全部；否则移出全部
    has_main = bool(read_saved_links() and
                    any(l for l in read_saved_links() if not any(l == _cdn_link(n) for n in nodes)))
    print(f"\n  全部 CDN 节点写入订阅（当前 {already}/{total} 条已写入）")
    for n in nodes:
        print(f"    · {n['domain']}:{n['cf_port']} [{n['proto']}] → {CDN_PROTO_SUB[n['proto']]}")
    print("  写入后客户端拉一次订阅即见（不支持某协议的格式自动跳过）；单条备用链接不受影响。")
    if writing and not has_main:
        print("  注意：本机还没装主节点，订阅将只含这些 CDN 节点；且订阅地址走本机 IP，")
        print("       若本机 IP 被墙则订阅地址也拉不到（节点本身经 CF 仍可用，改用单链接导入）。")
    ans = _ask(f"  {'写入全部' if writing else '移出全部'}? y 确认 / n 返回: ").strip().lower()
    if ans not in ("y", "yes"):
        return
    cdn_links = _cdn_state_links(nodes, _pref_load())    # 基础节点 + 候选节点
    try:
        _cdn_sub_apply(remove_links=cdn_links, add_links=(cdn_links if writing else []))
    except Exception as e:
        print("  ✗ 刷新订阅失败：", e); return
    for n in nodes:
        n["in_sub"] = writing
    _cdn_save(nodes)
    print(f"  ✓ 已{'写入' if writing else '移出'}全部 CDN 节点并刷新三格式订阅。客户端重新拉订阅即可生效。")
    if not writing and not read_saved_links():
        # 移出后订阅里一个节点都不剩：build_subscription 对空列表不再刷新，托管的旧订阅内容不会自动清空
        print("  ℹ️ 订阅里已无任何节点，之前托管的订阅内容不会再更新；如需彻底清空可到菜单 2「节点/订阅」重置。")

# ---------------------------------------------------------------------------- 优选地址
# 两种用法：
#   ① 手动填一个优选域名/IP —— 全部 CDN 节点的地址位都换成它；
#   ② 测速筛候选 —— 粗筛出 N 个还不错的 CF 边缘，各生成一条节点写进订阅，
#      最终由【客户端】的 URLTest 挑最快的那条。
#
# 为什么最终选择权必须在客户端：本机测的是「VPS → CF 边缘」，而决定体感的是
# 「你的网络 → CF 边缘」，后者只有客户端测得到。所以 VPS 只做它擅长的粗筛
# （几百个候选 IP 秒级过一遍），把结果作为候选池交给客户端做最终选择。
#
# 也正因如此，这里【没有】定时自动优选：换了地址得等主机重新汇总(多机聚合时)、
# 再等客户端重拉订阅才生效，中间全是人工断点——定时只会造成"它在自动工作"的假象。
# 多候选写进订阅后内容是稳定的，客户端 URLTest 每隔 interval 自己重测，才是真的自动。
def _pref_load():
    """读优选设置；缺项用默认补齐（老版本升上来也能直接用）。"""
    cfg = dict(CDN_PREF_DEFAULTS)
    try:
        cfg.update(json.load(open(CDN_PREF_FILE)))
    except Exception:
        pass
    return cfg

def _pref_save(cfg):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(cfg, open(CDN_PREF_FILE, "w"), ensure_ascii=False, indent=2)

def _pref_port(nodes, cfg):
    """测速端口：设了就用设的；否则沿用第一条 CDN 节点的 CF 端口（没节点则 443）。
       用节点自己的端口测，才能顺带验证这个边缘对该端口确实放行。"""
    if cfg.get("port"):
        return int(cfg["port"])
    return nodes[0]["cf_port"] if nodes else 443

def _cf_cidrs():
    """拉 CF 官方公布的 IPv4 段，返回 (段列表, 是否官方最新)。拉不到用内置兜底。"""
    try:
        req = urllib.request.Request(CF_IPS_URL, headers={"User-Agent": "xy-installer"})
        txt = urllib.request.urlopen(req, timeout=10).read().decode()
        out = [l.strip() for l in txt.splitlines() if re.fullmatch(r"[\d.]+/\d+", l.strip())]
        if out:
            return out, True
    except Exception:
        pass
    return list(CF_IPV4_FALLBACK), False

def _sample_ips(cidrs, n):
    """采样候选 IP：先把各段切成 /24、打散，再在 /24 内随机取一个主机地址。
       按 /24 取而不是整段均分——CF 同一大段内不同 /24 常落在不同机房，这样覆盖面最广。"""
    blocks = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue
        if net.prefixlen >= 24:
            blocks.append(net)
        else:
            blocks.extend(net.subnets(new_prefix=24))
    if not blocks:
        return []
    random.shuffle(blocks)
    seen, ips, guard = set(), [], 0
    while len(ips) < n and guard < n * 5:                 # guard：段数远少于 n 时别死转
        b = blocks[guard % len(blocks)]
        guard += 1
        size = b.num_addresses
        ip = str(b.network_address + (random.randint(1, size - 2) if size > 2 else 0))
        if ip not in seen:
            seen.add(ip); ips.append(ip)
    return ips

def _tcp_rtt(addr, port, timeout):
    """TCP 握手往返(ms)；连不上返回 None。域名也能测（connect 自己解析）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((addr, port))
        return (time.time() - t0) * 1000
    except OSError:
        return None
    finally:
        s.close()

def _latency_round(ips, port, cfg, on_progress=None):
    """并发测全部候选的握手延迟，返回 [(ip, ms)] 按延迟升序（连不上的直接丢掉）。"""
    from concurrent.futures import ThreadPoolExecutor
    out, done = [], [0]
    def work(ip):
        ms = _tcp_rtt(ip, port, float(cfg["timeout"]))
        done[0] += 1                                      # 仅供进度显示，不精确无所谓
        if on_progress and done[0] % 25 == 0:
            on_progress(done[0], len(ips))
        return ip, ms
    with ThreadPoolExecutor(max_workers=max(1, int(cfg["n_thread"]))) as ex:
        for ip, ms in ex.map(work, ips):
            if ms is not None:
                out.append((ip, ms))
    out.sort(key=lambda x: x[1])
    return out

def _dl_mbps(ip, port, cfg):
    """经指定 CF 边缘下载 CF 官方测速端点，返回 Mbps；失败返回 0。
       curl --resolve 把 speed.cloudflare.com 钉到这个 IP：SNI/证书仍是它自己的，
       所以不碰你的域名也能测出这条边缘线路的吞吐。"""
    nbytes = max(1, int(float(cfg["dl_mb"]) * 1_000_000))
    url = f"https://{CF_SPEED_HOST}:{port}/__down?bytes={nbytes}"
    out = sh(f"curl -sS -o /dev/null -w '%{{speed_download}}' "
             f"--resolve {CF_SPEED_HOST}:{port}:{ip} "
             f"--max-time {int(cfg['dl_time'])} '{url}'", check=False)
    try:
        return float(out.strip().replace(",", ".")) * 8 / 1_000_000    # 字节/秒 → Mbps
    except ValueError:
        return 0.0                                        # curl 报错/超时 → 这个 IP 判负

def _dl_probe(top, port, cfg, say):
    """对延迟前几名逐个下载测速，返回全部测通的 [{"ip","ms","mbps"}]，按速度降序。"""
    out = []
    for i, (ip, ms) in enumerate(top, 1):
        mbps = _dl_mbps(ip, port, cfg)
        say(f"    {i:>2}. {ip:<16}{ms:7.1f} ms {mbps:8.2f} Mbps" + ("" if mbps > 0 else "  (失败)"))
        if mbps > 0:
            out.append({"ip": ip, "ms": round(ms, 1), "mbps": round(mbps, 2)})
    out.sort(key=lambda r: -r["mbps"])
    return out

def cdn_speedtest(cfg=None, quiet=False):
    """粗筛一轮：拉 CF 段 → 采样 → 延迟筛 → 下载测速。
       返回按吞吐降序的 [{"ip","ms","mbps"}]；一个可用的都没有则返回 []。"""
    nodes = _cdn_load()
    cfg = cfg or _pref_load()
    port = _pref_port(nodes, cfg)
    say = (lambda *a: None) if quiet else (lambda *a: print(*a))
    cidrs, official = _cf_cidrs()
    say(f"  · CF IP 段 {len(cidrs)} 段（{'官方最新' if official else '拉取失败·用内置兜底段'}）")
    ips = _sample_ips(cidrs, int(cfg["n_cand"]))
    if not ips:
        say("  ✗ 没采到候选 IP（IP 段异常），本轮放弃。"); return []
    say(f"  · 候选 {len(ips)} 个，测握手延迟（端口 {port}，并发 {cfg['n_thread']}）...")
    prog = None if quiet else (lambda a, b: print(f"\r    进度 {a}/{b}", end="", flush=True))
    ranked = _latency_round(ips, port, cfg, prog)
    if not quiet:
        print("\r" + " " * 30 + "\r", end="")
    if not ranked:
        say(f"  ✗ 候选 IP 没有一个连得上 {port} 端口——检查本机出网是否被限，或到「测速参数」换个端口。")
        return []
    top = ranked[:max(1, int(cfg["n_top"]))]
    say(f"  · 通了 {len(ranked)} 个，取延迟最优 {len(top)} 个做下载测速"
        f"（每个最多 {cfg['dl_time']}s / {cfg['dl_mb']}MB）...")
    res = _dl_probe(top, port, cfg, say)
    if not res and port != 443:
        # 少数边缘只对 443 提供测速端点：换 443 再给一次机会（优选地址本身跟端口无关）
        say("  · 该端口下载测速全挂，改用 443 复测一轮...")
        res = _dl_probe(top, 443, cfg, say)
    if not res:
        say("  ✗ 下载测速全部失败，本轮不动现状。")
        return []
    lo = float(cfg["min_mbps"])
    if lo > 0:
        keep = [r for r in res if r["mbps"] >= lo]
        if not keep:
            say(f"  ✗ 最快也只有 {res[0]['mbps']} Mbps，低于下限 {lo} Mbps，本轮不动现状。")
            return []
        res = keep
    return res

# --- 候选节点：从一条基础节点克隆，只换地址位 ---------------------------------
# 它们共用同一个服务端入站（uuid/path/端口全一样），所以【不需要新建任何服务】，
# 区别只在客户端连哪个 CF 边缘。协议挑兼容性最好的，避免订阅里塞一堆客户端不认的。
_CAND_PROTO_PREF = ["vless-ws", "trojan-ws", "vmess-ws", "vless-xhttp"]

def _cdn_tag_prefix(node):
    """从节点 tag 里抠出用户设的名称前缀（tag 形如 「<前缀>CDN·<协议>」）。"""
    t = node.get("tag", "")
    i = t.find("CDN·")
    return t[:i] if i >= 0 else ""

def _cdn_cand_base(nodes):
    """挑一条基础节点当候选模板：优先兼容性最好的协议。"""
    for p in _CAND_PROTO_PREF:
        for n in nodes:
            if n.get("proto") == p:
                return n
    return nodes[0] if nodes else None

def _cdn_cand_nodes(nodes, cfg):
    """当前候选地址对应的节点列表（内存对象，不落 cdn.json——它们不是独立服务）。"""
    cands = [c for c in (cfg.get("cands") or []) if c]
    base = _cdn_cand_base(nodes)
    if not cands or base is None:
        return []
    pfx = _cdn_tag_prefix(base)
    out = []
    for i, addr in enumerate(cands, 1):
        st = dict(base)
        st["pref"] = addr
        st["tag"] = f"{pfx}CDN·优选{i}"
        out.append(st)
    return out

def _cdn_state_links(nodes, cfg):
    """当前状态会产出的全部 CDN 链接（基础节点 + 候选节点）。改动前先算一份当快照。"""
    return [_cdn_link(n) for n in nodes] + [_cdn_link(c) for c in _cdn_cand_nodes(nodes, cfg)]

def _cdn_sub_links(nodes, cfg):
    """其中应当出现在订阅里的：基础节点看自己的 in_sub，候选跟着一起进出。"""
    subn = [n for n in nodes if n.get("in_sub")]
    if not subn:
        return []
    return [_cdn_link(n) for n in subn] + [_cdn_link(c) for c in _cdn_cand_nodes(nodes, cfg)]

def _cdn_resync(old_links, was_in_sub):
    """改完状态后调它：把快照里的旧链接全撤掉，按当前状态重新写回订阅。
       was_in_sub 是改动【前】订阅里有没有 CDN 节点——全删光的场景也得进来做清理。"""
    nodes, cfg = _cdn_load(), _pref_load()
    add = _cdn_sub_links(nodes, cfg)
    if not was_in_sub and not add:
        return                                            # 订阅本来就没它、现在也不该有 → 不动
    try:
        _cdn_sub_apply(remove_links=old_links, add_links=add)
    except Exception as e:
        print("  ⚠ 订阅刷新失败（状态已存下，可回上级菜单 4 重写一次订阅）：", e)

def _cdn_set_pref(addr):
    """手动优选：把地址写进全部 CDN 节点。传空 = 取消优选、回到用域名。
       返回 True 表示确有改动。"""
    nodes = _cdn_load()
    if not nodes:
        return False
    cfg = _pref_load()
    addr = (addr or "").strip()
    if all((n.get("pref") or "") == addr for n in nodes):
        return False
    was = any(n.get("in_sub") for n in nodes)
    old = _cdn_state_links(nodes, cfg)                    # 必须先算：改完就还原不出旧链接了
    for n in nodes:
        n["pref"] = addr
    _cdn_save(nodes)
    _cdn_resync(old, was)
    return True

def cdn_pref_scan(ask=True):
    """测速筛候选：粗筛出若干 CF 边缘各写一条节点进订阅，最终由客户端 URLTest 选。
       ask=False 供安装流程调用——那边已经问过一次，别再问第二遍。"""
    nodes = _cdn_load()
    if not nodes:
        print("  还没配置 CDN 节点，先回上级菜单选 1 装一条。"); return
    cfg = _pref_load()
    n_out = max(1, int(cfg.get("n_cand_out", 5)))
    base = _cdn_cand_base(nodes)
    print(f"\n  测速筛候选：粗筛出最多 {n_out} 个 CF 边缘，各写一条 "
          f"[{base.get('proto')}] 节点进订阅。")
    print(f"  它们共用同一个服务端入站，不新建任何服务；最终由客户端 URLTest 挑最快的那条。")
    print(f"  下载测速最多消耗约 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量。")
    if ask and (_ask("  继续? y 确认 / 回车返回: ") or "n").lower() not in ("y", "yes"):
        return
    res = cdn_speedtest(cfg)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if not res:
        cfg["last"] = {"time": stamp, "ok": False}
        _pref_save(cfg)
        print("  本轮没选出可用地址，候选保持原样。")
        return
    picked = [r["ip"] for r in res[:n_out]]
    was = any(n.get("in_sub") for n in nodes)
    old = _cdn_state_links(nodes, cfg)
    cfg["cands"] = picked
    cfg["last"] = {"time": stamp, "ok": True, "n": len(picked),
                   "best": res[0]["ip"], "mbps": res[0]["mbps"]}
    _pref_save(cfg)
    _cdn_resync(old, was)
    print(f"\n  ✓ 选出 {len(picked)} 个候选，已写成 {len(picked)} 条节点：")
    for i, r in enumerate(res[:n_out], 1):
        print(f"    {_cdn_tag_prefix(base)}CDN·优选{i}   {r['ip']:<16}"
              f"（本机测 {r['ms']}ms / {r['mbps']} Mbps）")
    if was:
        print("  订阅已刷新。")
    else:
        print("  ⚠ 当前 CDN 节点还没写进订阅，候选也不会出现在订阅里——先用上级菜单 4 写入。")
    print("\n  接下来：客户端重拉订阅，让它的 URLTest 从这几条里挑最快的。")
    print("  本机测的是 VPS→CF 这一段，只作粗筛；哪条对你的网络最快，只有客户端说了算。")

def cdn_cand_clear():
    """清空候选（基础节点和手动优选地址不动）。"""
    nodes, cfg = _cdn_load(), _pref_load()
    if not (cfg.get("cands") or []):
        print("  当前没有候选节点。"); return
    was = any(n.get("in_sub") for n in nodes)
    old = _cdn_state_links(nodes, cfg)
    cfg["cands"] = []
    _pref_save(cfg)
    _cdn_resync(old, was)
    print("  ✓ 已清空候选节点（订阅同步刷新）。")

def _pref_valid(addr):
    """优选地址合法性：一个 IPv4 或一个域名。带协议头/端口/路径的一律打回。"""
    if not addr or " " in addr or "/" in addr or ":" in addr:
        return False                                      # 带端口/路径/IPv6 冒号的一律打回
    try:
        ipaddress.ip_address(addr); return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                                 r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+", addr))

# 可调测速参数：键 → (提示, 类型, 下限, 上限, 说明)。范围卡死，免得填出跑不动的组合。
_PREF_FIELDS = [
    ("n_cand",     "候选 IP 数",         int,   10,  5000,  "从 CF 各 /24 里随机采多少个来测延迟"),
    ("n_top",      "进下载测速的名次",     int,   1,   50,    "延迟最优的前几名才下载测速（直接决定流量开销）"),
    ("n_cand_out", "写进订阅的候选数",     int,   1,   20,    "最终写几条候选节点，交给客户端 URLTest 选"),
    ("n_thread",   "延迟测试并发",        int,   1,   500,   "越大越快；小内存机别开太高"),
    ("timeout",    "握手超时(秒)",        float, 0.2, 10,    "超过就当这个 IP 不通"),
    ("dl_mb",      "单个下载量上限(MB)",   float, 0.5, 500,   "流量开销 ≈ 名次 × 这个数"),
    ("dl_time",    "单个下载测速时长(秒)", int,   2,   60,    "到点截断按均速算；越长越准也越费流量"),
    ("min_mbps",   "候选下限(Mbps)",      float, 0,   10000, "低于它的候选不采纳；0=不设限"),
    ("port",       "测试端口",            int,   0,   65535, "0=沿用第一条 CDN 节点的 CF 端口"),
]

def _pref_settings(cfg):
    """逐项改测速参数，回车=保持不变；超范围的那项跳过、不影响其它项。"""
    print("\n  测速参数（回车=不改）：")
    for key, label, cast, lo, hi, note in _PREF_FIELDS:
        raw = _ask(f"    {label} [当前 {cfg.get(key, CDN_PREF_DEFAULTS[key])}]（{note}）: ").strip()
        if not raw:
            continue
        try:
            val = cast(raw)
        except ValueError:
            print(f"    ⚠ {label} 填的不是数字，这项跳过。"); continue
        if not lo <= val <= hi:
            print(f"    ⚠ {label} 需在 {lo}~{hi} 之间，这项跳过。"); continue
        cfg[key] = val
    _pref_save(cfg)
    print(f"  ✓ 已保存。一轮下载测速最多约 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量。")

def _pref_manual(nodes, cfg):
    """手动填优选地址：先连通性探一下，通了才写（不通给你自己拍板）。"""
    print("\n  填第三方优选域名（如各家公开的 CF 优选域名）或一个具体的 CF IP。")
    print("  留空回车 = 取消优选，恢复成直接用你自己的域名。")
    addr = _ask("  优选地址: ").strip().lower()
    if not addr:
        if _cdn_set_pref(""):
            print("  ✓ 已取消优选，全部 CDN 节点恢复用域名（订阅已同步刷新）。")
        else:
            print("  本来就没设优选，未改动。")
        return
    if not _pref_valid(addr):
        print("  ✗ 只填域名或 IPv4 本身，别带 http://、端口和路径；暂不支持 IPv6。已取消。")
        return
    port = _pref_port(nodes, cfg)
    ms = _tcp_rtt(addr, port, 3.0)
    if ms is None:
        print(f"  ⚠ 从本机连 {addr}:{port} 不通（也可能只是本机到它的路由差，客户端未必不通）。")
        if (_ask("  仍然写入? y 确认 / 回车放弃: ") or "n").lower() not in ("y", "yes"):
            print("  已放弃。"); return
    else:
        print(f"  ✓ 连通，握手 {ms:.1f}ms。")
    if _cdn_set_pref(addr):
        print(f"  ✓ 已把全部 CDN 节点的地址位换成 {addr}（订阅已同步刷新，客户端重拉即生效）。")
    else:
        print("  和当前一样，未改动。")

def cdn_pref_menu():
    """优选地址子菜单：手动填一个 / 测速筛一批候选 / 清空候选 / 参数。"""
    while True:
        nodes = _cdn_load()
        cfg = _pref_load()
        print("\n" + "=" * 60)
        print("  优选地址（换客户端连的 CF 边缘，服务端一行都不用改）")
        print("=" * 60)
        print("  原理：分享链接里【地址位】换成更快的 CF 地址，【SNI/Host 仍是你的真域名】；")
        print("        CF 靠 Host 头回源，所以换任意 CF 边缘都能连回同一台 VPS。")
        if not nodes:
            print("-" * 60)
            print("  还没配置 CDN 节点，先回上级菜单选 1 装一条。")
            return
        cur = sorted({(n.get("pref") or "").strip() for n in nodes})
        cands = [c for c in (cfg.get("cands") or []) if c]
        print("-" * 60)
        if cur == [""]:
            print("  基础节点：未优选（客户端直连域名解析到的 CF IP）")
        else:
            print("  基础节点优选地址：" + "、".join(c or "(未优选·用域名)" for c in cur))
        if cands:
            print(f"  候选节点：{len(cands)} 条 —— " + "、".join(cands))
            print("            （客户端 URLTest 从这几条里自己挑最快的）")
        else:
            print("  候选节点：无")
        last = cfg.get("last") or {}
        if last.get("ok"):
            print(f"  上次测速：{last.get('time','')}  选出 {last.get('n','?')} 个"
                  f"，最快 {last.get('best','')} / {last.get('mbps','?')} Mbps")
        elif last:
            print(f"  上次测速：{last.get('time','')}  没选出可用地址（已保持原样）")
        print("-" * 60)
        print(f"  1 测速筛候选（写 {cfg.get('n_cand_out', 5)} 条进订阅，让客户端自己选最快的）")
        print("  2 手动填优选域名/IP（直接留空回车=取消优选、回到用域名）")
        print("  3 清空候选节点")
        print("  4 测速参数")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            cdn_pref_scan()
        elif c == "2":
            _pref_manual(nodes, cfg)
        elif c == "3":
            cdn_cand_clear()
        elif c == "4":
            _pref_settings(cfg)
        elif c in ("0", ""):
            return

def _cdn_drop(node):
    """停服务、删单元、删该条的证书/配置。"""
    sh(f"systemctl disable --now {node['svc']}", check=False)
    sh(f"rm -f /etc/systemd/system/{node['svc']}.service", check=False)
    for p in (node.get("crt"), node.get("key"), node.get("conf")):
        if p:
            try: os.remove(p)
            except OSError: pass

def cdn_remove():
    nodes = _cdn_load()
    if not nodes:
        print("  还没配置 CDN 节点。"); return
    print("\n  卸载哪条 CDN 节点：")
    for i, n in enumerate(nodes, 1):
        print(f"   {i}. {n['domain']}:{n['cf_port']} [{n['proto']}/{n['core']}]"
              f"{'（已写入订阅）' if n.get('in_sub') else ''}")
    print("   a 全部")
    sel = _ask("  选择(编号；可多选，逗号分隔如 1,3；a=全部；回车取消): ").strip().lower()
    if not sel:
        return
    if sel in ("a", "all", "0"):
        targets = list(nodes)
    else:
        idxs, bad = [], False
        for tok in sel.replace("，", ",").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try: i = int(tok)
            except ValueError: bad = True; continue
            if 1 <= i <= len(nodes):
                if i not in idxs: idxs.append(i)
            else:
                bad = True
        if bad:
            print("  含无效/超范围编号，已忽略这些。" if idxs else "  无效选择。")
        if not idxs:
            return
        targets = [nodes[i - 1] for i in idxs]
    if (_ask(f"  确认卸载 {len(targets)} 条? y 确认 / n 返回: ") or "n").lower() not in ("y", "yes"):
        return
    # 已写入订阅的先撤掉，别留死节点。用改动前的全量快照（含候选）撤，漏一条就是死节点
    was_in_sub = any(n.get("in_sub") for n in nodes)
    old_links = _cdn_state_links(nodes, _pref_load())
    for n in targets:
        _cdn_drop(n)
    sh("systemctl daemon-reload", check=False)
    remaining = [n for n in nodes if n not in targets]
    if remaining:
        _cdn_save(remaining)
    else:
        shutil.rmtree(CDN_DIR, ignore_errors=True)
        try: os.remove(CDN_STATE)
        except OSError: pass
        cfg = _pref_load(); cfg["cands"] = []; _pref_save(cfg)   # 节点没了，候选也没意义
    _cdn_resync(old_links, was_in_sub)                  # 撤旧的、把还留着的（含候选）写回
    print(f"  ✓ 已卸载 {len(targets)} 条 CDN 节点（Cloudflare 那边的 DNS 记录请自行删除）。")

def cdn_menu():
    while True:
        nodes = _cdn_load()
        print("\n" + "=" * 60)
        print("  CDN 套用（防 IP 被墙：靠 Cloudflare 中转续命）")
        print("=" * 60)
        _cdn_intro()
        print("-" * 60)
        print(f"  1 CDN节点安装{('（已配置 %d 条）' % len(nodes)) if nodes else ''}")
        pcur = sorted({(n.get("pref") or "").strip() for n in nodes}) if nodes else [""]
        print(f"  2 优选地址（手动填 / 测速筛候选给客户端选）"
              f"{'  当前：' + '、'.join(c for c in pcur if c) if pcur != [''] else ''}")
        print("  3 查看全部备用链接")
        print("  4 全部节点写入/移出订阅（循环开关，执行后订阅自动刷新）")
        print("  5 卸载 CDN 节点（可选某条 / 全部）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            cdn_add()
        elif c == "2":
            cdn_pref_menu()
        elif c == "3":
            if not nodes:
                print("  还没配置，先选 1 CDN节点安装。"); continue
            # 上方：已配置节点列表；下方：全部备用链接
            print("\n  已配置 %d 条：" % len(nodes))
            for i, n in enumerate(nodes, 1):
                act = sh(f"systemctl is-active {n['svc']}", check=False) == "active"
                insub = "已写入订阅" if n.get("in_sub") else "仅备用链接"
                pf = (n.get("pref") or "").strip()
                print(f"   {i}. {n['domain']}:{n['cf_port']}（{n.get('proto','vless-ws')}/"
                      f"{n.get('core','sing-box')}） {'运行中 ✓' if act else '未运行 ✗'}  {insub}"
                      f"{'  优选→' + pf if pf else ''}")
            print("\n  ▼ 全部 CDN 备用节点链接（导入客户端用；平时留着不用即可）:")
            for i, n in enumerate(nodes, 1):
                print(f"  {i}. [{n['proto']}/{n['core']}] {n['domain']}:{n['cf_port']}")
                print(f"     {_cdn_link(n)}")
        elif c == "4":
            cdn_write_sub()
        elif c == "5":
            cdn_remove()
        elif c in ("0", ""):
            return

def _port80_owner():
    """80 端口被谁占着；空闲返回 ""。用来判断 acme 验证该走哪条路。"""
    out = sh("ss -lntp 2>/dev/null", check=False) or ""
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4 or not f[3].endswith(":80"):
            continue
        m = re.search(r'users:\(\("([^"]+)"', line)
        return m.group(1) if m else "未知程序"
    return ""

def _cert_secs_left(path=None):
    """磁盘上 acme 证书还剩多少秒到期；没有证书/读不出返回 None（负数=已过期）。

       用 calendar.timegm 而不是 time.mktime：openssl 打的是 GMT，mktime 会按本机时区
       去解，服务器设了非 UTC 时区就会差出小半天，正好在到期这天给出相反的结论。"""
    path = path or ACME_CRT
    if not os.path.exists(path):
        return None
    out = sh(f"openssl x509 -noout -enddate -in {path}", check=False)
    m = re.search(r"notAfter=(.+)", out or "")
    if not m:
        return None
    try:
        end = time.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return int(calendar.timegm(end) - time.time())

def _cert_left_text(secs):
    """把剩余秒数说成人话。不写「已过期 0 天」这种话。"""
    if secs is None:
        return "读不出来"
    if secs <= 0:
        d = int(-secs // 86400)
        return "刚刚过期" if d == 0 else f"已过期 {d} 天"
    d = secs // 86400
    if d == 0:
        return f"今天就到期（还剩 {max(secs // 3600, 1)} 小时）"
    return f"还剩 {d} 天到期"

# ══════════════════════════════════════════════════════════════════════════════
# 证书信息：把「这张证书到底什么情况」一次问清楚
# ══════════════════════════════════════════════════════════════════════════════
# 证书是全机共用的一块地基：节点(sing-box/xray)、订阅服务(xy-sub)、nginx 443、
# AdGuard 的 DoT 都指着 /etc/ssl/sb/acme.crt。它一过期就是全部一起挂，而且谁都
# 不会报「证书过期」，只会「连不上」。所以这里把每一环都查出来摆到面板上。

def _sub_service_text():
    """xy-sub 的 service 文件内容（读不到返回 ''）。用来判断订阅服务是不是在吃这张证书——
       比调 _sub_https() 便宜得多，后者要查公网 IP，为画一行面板联一次网不值。"""
    for p in ("/etc/systemd/system/xy-sub.service", "/lib/systemd/system/xy-sub.service"):
        try:
            return open(p).read()
        except OSError:
            continue
    return ""

def cert_meta():
    """证书的事实来源：{domain, wildcard, mode}。

       为什么单独存一份，而不是继续读 state.json：state.json 是【装节点时】才写的，
       而证书可以先于节点装（也该允许）。没有 cert.json 时就从磁盘上的证书 + 安装记录
       现推一份出来并落盘，老机器升级上来不用手工补。"""
    try:
        m = json.load(open(CERT_META))
        if m.get("domain"):
            return m
    except Exception:
        pass
    m = {"domain": "", "wildcard": False, "mode": ""}
    names = cert_names(ACME_CRT)
    if names:
        wild = [n for n in names if n.startswith("*.")]
        m["wildcard"] = bool(wild)
        m["domain"] = (wild[0][2:] if wild else names[0])
        m["mode"] = _acme_mode(m["domain"]) or ("dns-cf" if wild else "standalone")
    else:                                        # 没有 acme 证书 → 看看安装记录里有没有域名
        try:
            m["domain"] = json.load(open(STATE_FILE)).get("domain", "")
        except Exception:
            pass
    if m["domain"]:
        save_cert_meta(**m)
    return m

def save_cert_meta(**kw):
    """更新 cert.json（只覆盖传进来的字段）。"""
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
    return cur

def cert_names(path=None):
    """证书覆盖哪些域名（SAN 列表，含 *.x 泛域名）。读不出返回 []。"""
    path = path or ACME_CRT
    if not os.path.exists(path):
        return []
    out = sh(f"openssl x509 -in {path} -noout -text 2>/dev/null", check=False) or ""
    m = re.search(r"Subject Alternative Name:\s*\n\s*(.+)", out)
    if not m:
        return []
    return [x.strip()[4:] for x in m.group(1).split(",") if x.strip().startswith("DNS:")]

_CA_NAMES = [("let's encrypt", "Let's Encrypt"), ("zerossl", "ZeroSSL"),
             ("buypass", "Buypass"), ("google trust", "Google Trust Services"),
             ("cloudflare", "Cloudflare"), ("digicert", "DigiCert"),
             ("sectigo", "Sectigo"), ("amazon", "Amazon"), ("globalsign", "GlobalSign")]

def cert_issuer(path=None):
    """谁签的这张证书 → 人话名字；自签返回「自签」；读不出返回 ''。"""
    path = path or ACME_CRT
    if not os.path.exists(path):
        return ""
    iss = sh(f"openssl x509 -in {path} -noout -issuer 2>/dev/null", check=False) or ""
    sub = sh(f"openssl x509 -in {path} -noout -subject 2>/dev/null", check=False) or ""
    if iss[7:].strip() and iss[7:].strip() == sub[8:].strip():   # issuer == subject
        return "自签"
    low = iss.lower()
    for key, name in _CA_NAMES:
        if key in low:
            return name
    m = re.search(r"\bO\s*=\s*([^,/]+)", iss)              # 认不出就把 O= 原样报出来
    return m.group(1).strip() if m else ""

def cert_key_ok(crt, key):
    """私钥和证书是不是一对（公钥指纹比对）。任一读不出都返回 False。"""
    a = sh(f"openssl x509 -in {crt} -noout -pubkey 2>/dev/null", check=False)
    b = sh(f"openssl pkey -in {key} -pubout 2>/dev/null", check=False)
    return bool(a) and a.strip() == (b or "").strip()

def _acme_conf(dom):
    """acme.sh 里这个域名的记录文件（ecc 优先）；找不到返回 ''。"""
    base = os.path.expanduser("~/.acme.sh")
    for d in (f"{base}/{dom}_ecc/{dom}.conf", f"{base}/{dom}/{dom}.conf"):
        if os.path.exists(d):
            return d
    return ""

def _acme_mode(dom):
    """acme.sh 记着的验证方式：dns-cf / webroot / standalone；没记录返回 ''。"""
    c = _acme_conf(dom)
    if not c:
        return ""
    try:
        txt = open(c).read()
    except OSError:
        return ""
    m = re.search(r"^Le_Webroot=['\"]?([^'\"\n]*)", txt, re.M)
    v = (m.group(1) if m else "").strip()
    if v.startswith("dns_"):
        return "dns-cf" if v == "dns_cf" else v          # 别的 DNS 插件原样报出来
    return "webroot" if v and v != "no" else "standalone"

def _acme_hook_ok(dom):
    """acme.sh 里这个域名记没记 reloadcmd。没记 = 续期后服务不会重读证书。"""
    c = _acme_conf(dom)
    if not c:
        return False
    try:
        return bool(re.search(r"^Le_ReloadCmd=['\"]?.+", open(c).read(), re.M))
    except OSError:
        return False

def acme_hijackers(keep=""):
    """acme.sh 里【除了 keep 之外】还有哪些域名也把证书装到 /etc/ssl/sb/acme.crt。

       为什么这是个坑：acme.sh 的 --install-cert 会把「装到哪个文件、装完跑什么」
       存进该域名自己的记录里，往后每次自动续期都照着做。换过域名的机器上，旧域名
       那条记录【没人删】——它的 cron 续期照跑，续完就把节点正在用的证书覆盖成旧域名
       那张，还顺手按存着的 reloadcmd 重启了核心。
       表现：某天半夜所有吃证书的节点集体「连不上」，reality 照常，而你什么都没做。
       泛解析（*.域名）在的话，旧域名照样验证得过，所以它能一直成功地捣乱。

       返回 [(域名, 记录文件)]。"""
    base = os.path.expanduser("~/.acme.sh")
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base)):
        conf = os.path.join(base, d, d.replace("_ecc", "") + ".conf")
        if not os.path.exists(conf):
            continue
        dom = d[:-4] if d.endswith("_ecc") else d
        if keep and dom == keep:
            continue
        try:
            txt = open(conf).read()
        except OSError:
            continue
        if re.search(r"^Le_RealCertPath=['\"]?" + re.escape(ACME_CRT), txt, re.M):
            out.append((dom, conf))
    return out

def acme_records():
    """acme.sh 里有哪些域名记录 → [{domain, dir, conf, path, secs, status, removable}]。

       status 是「这张还有没有用」的判断，按危害从大到小排：
         抢占中   装到节点证书路径、但不是节点域名 —— 续期时会把节点的证书覆盖掉
         节点在用 / Emby 在用 / 其它服务在用   有主，不许删
         没人用   装到的文件已经不存在，或压根没配过安装路径 —— 留着只是占地方
    """
    base = os.path.expanduser("~/.acme.sh")
    nd, emby = node_domain(), _emby_cert()[0]
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base)):
        dom = d[:-4] if d.endswith("_ecc") else d
        conf = os.path.join(base, d, dom + ".conf")
        if not os.path.exists(conf):
            continue
        try:
            txt = open(conf).read()
        except OSError:
            continue
        m = re.search(r"^Le_RealCertPath=['\"]?([^'\"\n]*)", txt, re.M)
        path = (m.group(1) if m else "").strip()
        bare = dom[2:] if dom.startswith("*.") else dom
        # 判「是不是节点那张」不能把星号去掉再比：*.a.com 并【不】覆盖 a.com 本身，
        # 这种记录多半是 Emby 的（它服务的是 <子域>.a.com）。用真正的覆盖判定。
        if nd and _name_covers([dom], nd):
            status, removable = "节点在用", False
        elif emby and (bare == emby or _name_covers([dom], "x." + emby)):
            status, removable = "Emby 在用", False
        elif path and os.path.abspath(path) == os.path.abspath(ACME_CRT):
            status, removable = "抢占中", True
        elif path and os.path.exists(path):
            status, removable = "别处在用", False
        else:
            status, removable = "没人用", True
        out.append({"domain": dom, "dir": os.path.join(base, d), "conf": conf,
                    "path": path, "secs": _cert_secs_left(os.path.join(base, d, "fullchain.cer")),
                    "status": status, "removable": removable})
    return out

def _acme_cron_ok():
    """acme.sh 的每日续期任务在不在 crontab 里。"""
    return "acme.sh" in (sh("crontab -l 2>/dev/null", check=False) or "")

def _emby_cert():
    """Emby 那张证书 → (基础域名, 剩余秒)。没装 Emby 或没配域名返回 ('', None)。

       Emby 自己签的是泛域名 *.<域名>，装在 /etc/nginx/certs/<域名>.crt，
       它的 nginx 是 server_name <子域>.<域名>（emby./mw. 等好几个子域）。"""
    for envf in ("/opt/emby-stack/.env", "/opt/media-stack/.env"):
        try:
            m = re.search(r"^DOMAIN=(.*)$", open(envf).read(), re.M)
        except OSError:
            continue
        dom = (m.group(1).strip().strip('"\'') if m else "")
        if dom:
            return dom, _cert_secs_left(_emby_crt(dom))
    return "", None

def _emby_crt(dom):  return f"/etc/nginx/certs/{dom}.crt"
def _emby_key(dom):  return f"/etc/nginx/certs/{dom}.key"

def _emby_shared(dom):
    """Emby 的证书路径是不是已经指到节点这张上了（软链）。"""
    p = _emby_crt(dom)
    return os.path.islink(p) and os.path.realpath(p) == os.path.realpath(ACME_CRT)

def node_domain():
    """节点当前对外用的域名（客户端连的就是它）。没有域名（自签+IP）返回 ''。"""
    try:
        return (json.load(open(STATE_FILE)).get("domain") or "").strip()
    except Exception:
        return ""

def cert_info():
    """把证书的方方面面查出来，给面板用。纯读，不改任何东西。"""
    meta = cert_meta()
    dom = meta.get("domain", "")
    names = cert_names()
    secs = _cert_secs_left()
    emby_dom, emby_secs = _emby_cert()
    names = names or []
    nd = node_domain()
    # Emby 对外是 <子域>.<域名>（见 media-stack 的 server_name），所以判断「节点证书能不能
    # 顶替它」要拿一个子域去试，不能拿裸域——裸域证书盖不住任何子域名。
    emby_covered = bool(emby_dom) and _name_covers(names, "x." + emby_dom)
    return {
        "domain": dom,
        "wildcard": any(n.startswith("*.") for n in names),
        "names": names,
        "issuer": cert_issuer(),
        "secs": secs,
        "exists": os.path.exists(ACME_CRT) and os.path.exists(ACME_KEY),
        "key_ok": cert_key_ok(ACME_CRT, ACME_KEY) if os.path.exists(ACME_KEY) else False,
        "mode": meta.get("mode") or (_acme_mode(dom) if dom else ""),
        "cron_ok": _acme_cron_ok(),
        "hook_ok": _acme_hook_ok(dom) if dom else False,
        "emby_domain": emby_dom,
        "emby_secs": emby_secs,
        "emby_covered": emby_covered,
        "emby_shared": bool(emby_dom) and _emby_shared(emby_dom),
        # 节点域名盖没盖住：这是证书的底线。盖不住 = 客户端一律
        # 「tls: bad certificate」，而 reality 照常通——最像「随机几个节点坏了」的一种故障。
        "node_domain": nd,
        "node_covered": (not nd) or _name_covers(names, nd),
        "hijackers": [d for d, _c in acme_hijackers(keep=nd or dom)],
        "selfsigned": os.path.exists(CERT) and not os.path.exists(ACME_CRT),
    }

_MODE_TEXT = {"standalone": "HTTP-01 独占 80 端口", "webroot": "HTTP-01 走 nginx webroot",
              "dns-cf": "DNS-01 Cloudflare（可签泛域名，不占端口）"}

def _left_color(secs):
    """剩余天数的颜色：>30 绿 / 7-30 黄 / <7 或已过期 红。"""
    if secs is None:
        return "\033[1;31m"
    d = secs / 86400
    return "\033[1;32m" if d > 30 else ("\033[1;33m" if d >= 7 else "\033[1;31m")

def cert_panel(info=None):
    """把证书状况摆出来。只读，不改任何东西。"""
    C, Y, R, B, N = "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[1m", "\033[0m"
    i = info or cert_info()
    print("\n" + "=" * 60)
    print("  证书管理")
    print("=" * 60)
    if not i["exists"]:
        if i["selfsigned"]:
            print(f"  {Y}当前：自签证书{N}（没有域名）。自签能用，但客户端要开 allowInsecure，")
            print("        且自签本身就是明显特征。有域名的话建议装一张真证书。")
        else:
            print(f"  {Y}当前：还没有证书。{N}")
        print("-" * 60)
        return i
    lc = _left_color(i["secs"])
    print(f"  域名:      {B}{i['domain'] or '(读不出)'}{N}"
          + (f"   {C}泛域名{N}" if i["wildcard"] else ""))
    print(f"  颁发机构:  {B}{i['issuer'] or '(读不出)'}{N}")
    print(f"  有效期:    {lc}{_cert_left_text(i['secs'])}{N}")
    mode = _MODE_TEXT.get(i["mode"], i["mode"] or "(读不出)")
    auto = i["cron_ok"] and i["hook_ok"]
    print(f"  续期模式:  {'自动' if auto else Y + '不完整' + N}   {mode}")
    if not i["cron_ok"]:
        print(f"    {R}✗ crontab 里没有 acme.sh 的每日续期任务 —— 到期不会自动重签{N}")
    if not i["hook_ok"]:
        print(f"    {R}✗ acme.sh 没记 reloadcmd —— 就算重签了，节点/订阅也不会重读新证书{N}")
    if not i["key_ok"]:
        print(f"    {R}✗ 私钥和证书对不上 —— 握手会直接失败{N}")
    if not i["node_covered"]:
        print(f"    {R}✗ 这张证书盖不住节点正在用的域名 {i['node_domain']}！{N}")
        print(f"    {R}  所有吃证书的节点都会被客户端拒绝（tls: bad certificate），"
              f"只有 reality 还通。{N}")
        print(f"    {R}  点『3 强制重签』重签回 {i['node_domain']} 即可。{N}")
    if i["names"]:
        print(f"  覆盖域名:  {', '.join(i['names'])}")
    if i["hijackers"]:
        print(f"    {R}✗ acme.sh 里还有 {', '.join(i['hijackers'])} 也装到同一个文件！{N}")
        print(f"    {R}  多半是换域名前留下的旧记录。它每次自动续期都会把这张证书覆盖成"
              f"它自己那张，{N}")
        print(f"    {R}  半夜发作、你什么都没做——吃证书的节点集体挂掉，reality 照常。{N}")
        print(f"    {R}  点『3 强制重签』会顺手把它们撤掉。{N}")
    users = [n for n, ok in (("sing-box", os.path.exists(SB_BIN)),
                             ("xray", os.path.exists(XRAY_BIN)),
                             ("订阅服务", ACME_CRT in _sub_service_text()),
                             ("nginx", os.path.exists(NGINX_CONF)),
                             ("AdGuard", os.path.exists("/opt/AdGuardHome/AdGuardHome"))) if ok]
    if users:
        print(f"  正在使用:  {', '.join(users)}")
    if i["emby_domain"]:
        ec = _left_color(i["emby_secs"])
        print(f"  Emby 证书: *.{i['emby_domain']}   {ec}{_cert_left_text(i['emby_secs'])}{N}"
              + (f"   {C}与节点共用一张{N}" if i["emby_shared"] else
                 (f"   {C}本证书已能顶替它{N}" if i["emby_covered"] else "   （另一张，独立续期）")))
    print("-" * 60)
    return i

def cert_validate(crt, key, domain):
    """新签出来的证书能不能用 → (ok, 说明)。装上去之前必须全过，一条不过就别碰现有的。

       domain 可以是一个域名，也可以是一串（升级成泛域名时要同时盖住节点域名和
       Emby 的子域），一串里【每一个】都得盖住才算过。

       四件事都要查，少一件就可能把一台好机器换成连不上：
         ① 文件在不在、解析得开        ② 覆盖不覆盖这个域名（含泛域名）
         ③ 是不是已经过期              ④ 私钥跟证书配不配对
       ④ 尤其阴：签发中断时很容易留下新证书配旧私钥，握手直接失败，
       但文件看着一切正常、日期也没问题。"""
    if not (os.path.exists(crt) and os.path.exists(key)):
        return False, "证书或私钥文件没生成"
    names = cert_names(crt)
    if not names:
        return False, "证书解析不开（文件损坏或不是证书）"
    need = [domain] if isinstance(domain, str) else list(domain)
    missing = [d for d in need if not _name_covers(names, d)]
    if missing:
        return False, (f"这张证书覆盖的是 {', '.join(names)}，"
                       f"盖不住 {', '.join(missing)}")
    secs = _cert_secs_left(crt)
    if secs is None:
        return False, "读不出有效期"
    if secs <= 0:
        return False, "签出来就是过期的"
    if not cert_key_ok(crt, key):
        return False, "私钥和证书对不上（握手会直接失败）"
    return True, f"覆盖 {', '.join(names)}，{_cert_left_text(secs)}"

def _name_covers(names, domain):
    """SAN 列表覆不覆盖这个域名（泛域名只顶一级，跟浏览器/客户端的判定一致）。"""
    d = (domain or "").lower().strip(".")
    for n in [x.lower().strip(".") for x in names]:
        if n == d:
            return True
        if n.startswith("*.") and d.endswith(n[1:]) and "." not in d[:-len(n[1:])]:
            return True
    return False

def _cert_consumers():
    """吃 /etc/ssl/sb/acme.crt 的服务，换完证书要让它们重读。"""
    return [n for n in ("nginx", "sing-box", "xray", "xy-sub", "AdGuardHome")
            if os.path.exists(f"/etc/systemd/system/{n}.service")
            or os.path.exists(f"/lib/systemd/system/{n}.service")]

def cert_install_flow(info):
    """菜单 15 → 1 安装证书。交互问完，真动手那半程转后台（重启核心会掐断 SSH）。"""
    Y, R, N = "\033[1;33m", "\033[1;31m", "\033[0m"
    if info["exists"]:
        cert_upgrade_flow(info)
        return
    dom = _ask("\n  域名（要先把 A 记录解析到本机公网 IP）: ").strip().lower().rstrip(".")
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", dom):
        print("  域名格式不对，已取消。")
        return
    print("\n  验证方式：")
    print("   1 HTTP-01（默认）  用 80 端口验证，只签这一个域名，不用任何密钥")
    print("   2 DNS-01 泛域名    用 Cloudflare API 验证，签 *." + dom + "，不占端口，")
    print("                      Emby / 子域名都能共用同一张")
    w = (_ask("  选择 [1/2]（回车=1）: ") or "1").strip()
    cf_token = ""
    wildcard = (w == "2")
    if wildcard:
        if _acme_has_cf():
            print("  ✓ acme.sh 里已经存着 Cloudflare 凭据，直接用。")
        else:
            print("  需要一个 Cloudflare API Token（后台 → 我的个人资料 → API 令牌 →")
            print("  创建令牌 → 用「编辑区域 DNS」模板，区域选这个域名）")
            cf_token = _ask("  粘贴 Token: ").strip()
            if not cf_token:
                print("  没填 Token，已取消。")
                return
    # 解析检查只对 HTTP-01 有意义：DNS-01 不需要域名指向本机
    if not wildcard:
        try:
            got = sorted({x[4][0] for x in socket.getaddrinfo(dom, None)})
        except Exception:
            got = []
        mine = public_ip()
        if mine not in got:
            print(f"{R}  ✗ {dom} 解析到 {', '.join(got) or '(查不到)'}，不是本机 {mine}。{N}")
            print("    HTTP-01 验证要求域名指向本机，先改好解析再来。")
            return
        print(f"  ✓ 解析检查通过：{dom} → {mine}")
    want = cert_want_names(dom, wildcard, info["emby_domain"])
    print("\n" + "-" * 60)
    print(f"  要签的域名: {', '.join(want)}")
    if wildcard and len(want) > 2:
        print(f"              （顺带把 Emby 的 {info['emby_domain']} 签进去，一张顶两张）")
    print(f"  验证方式:   {_MODE_TEXT['dns-cf' if wildcard else 'standalone']}")
    print(f"  签发机构:   Let's Encrypt")
    print(f"  装到:       {ACME_CRT}")
    print(f"  安全网:     先签到临时文件、验过（覆盖得到每一个域名 / 没过期 / 私钥配对）")
    print(f"              才换上去；不过就丢弃新的，现有证书一个字节不动")
    print(f"  完成后:     {'、'.join(_cert_consumers()) or '(暂无服务)'} 重读证书")
    print(f"{Y}  ⚠ 重启这些服务会掐断代理链路，挂着本机代理连的 SSH 会断——"
          f"不用管，后台会跑完。{N}")
    print("-" * 60)
    if (_ask("确认签发? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return
    plan = {"op": "cert-install", "domain": dom, "wildcard": wildcard,
            "cf_token": cf_token, "names": want, "G": dict(G)}
    _node_op_dispatch(plan, lambda: cert_install_apply(dom, wildcard, cf_token, want))

def cert_upgrade_flow(i):
    """已经有证书时进『1 安装证书』：能升级成泛域名就给这条路，否则说清楚该走哪儿。

       为什么单独有这一步：从单域名升到泛域名【不是重签】——验证方式要从 HTTP-01
       换成 DNS-01，签的名字也多了 *.域名。走『3 强制重签』沿用的是记录里原来那套，
       永远签不出泛域名。没有这条路，装了单域名证书的机器就没法让 Emby 共用。"""
    Y, GRN, C, N = "\033[1;33m", "\033[1;32m", "\033[1;36m", "\033[0m"
    dom = i["domain"]
    emby = i["emby_domain"]
    want = cert_want_names(dom, True, emby)
    missing = [n for n in want if not _name_covers(i["names"], n)]
    print(f"\n  已经装着 {dom} 的证书（{cert_issuer()}，{_cert_left_text(i['secs'])}）")
    print(f"  覆盖：{', '.join(i['names']) or '(读不出)'}")
    if not missing:
        print(f"\n  {GRN}已经是泛域名了，该盖的都盖住了。{N}")
        print("  想重新签一张：选『3 强制重签』。")
        if emby and not i["emby_shared"]:
            print(f"  {C}想让 Emby 共用这一张：选『4 Emby 证书』。{N}")
        _ask("  按回车返回...")
        return
    print(f"\n  可以升级成泛域名，升完会覆盖：{', '.join(want)}")
    print(f"  现在还缺：{Y}{', '.join(missing)}{N}")
    if emby:
        print(f"  升级后 Emby（{emby} 的各个子域）就能跟节点共用这一张，两条续期链并成一条。")
    print("\n  升级要用 DNS-01（Cloudflare API）验证——泛域名只能这么签，HTTP-01 签不出来。")
    print("-" * 60)
    print("  1 升级成泛域名（DNS-01 · Cloudflare）")
    print("  0 返回")
    if (_ask("选择 [1/0]（回车=0 返回）: ") or "0").strip() != "1":
        return
    cf_token = ""
    if _acme_has_cf():
        print("  ✓ acme.sh 里已经存着 Cloudflare 凭据，直接用。")
    else:
        print("  需要一个 Cloudflare API Token（后台 → 我的个人资料 → API 令牌 →")
        print("  创建令牌 → 用「编辑区域 DNS」模板，区域选这个域名）")
        cf_token = _ask("  粘贴 Token: ").strip()
        if not cf_token:
            print("  没填 Token，已取消。")
            return
    print("\n" + "-" * 60)
    print(f"  升级:      {dom}  →  {', '.join(want)}")
    print(f"  验证方式:  {_MODE_TEXT['dns-cf']}")
    print(f"  签发机构:  Let's Encrypt")
    print(f"  安全网:    先签到临时文件、验过（覆盖得到每一个域名 / 没过期 / 私钥配对）")
    print(f"             才换上去；不过就丢弃新的，现有证书一个字节不动")
    print(f"  完成后:    {'、'.join(_cert_consumers()) or '(暂无服务)'} 重读证书")
    print(f"{Y}  ⚠ 重启这些服务会掐断代理链路，挂着本机代理连的 SSH 会断——"
          f"不用管，后台会跑完。{N}")
    print("-" * 60)
    if (_ask("确认升级? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return
    plan = {"op": "cert-install", "domain": dom, "wildcard": True,
            "cf_token": cf_token, "names": want, "G": dict(G)}
    _node_op_dispatch(plan, lambda: cert_install_apply(dom, True, cf_token, want))

def _acme_has_cf():
    """acme.sh 的 account.conf 里存没存过 Cloudflare 凭据。"""
    for c in (os.path.expanduser("~/.acme.sh/account.conf"), "/root/.acme.sh/account.conf"):
        try:
            if re.search(r"^SAVED_CF_(Token|Key)=.+", open(c).read(), re.M):
                return True
        except OSError:
            continue
    return False

def cert_want_names(dom, wildcard, emby_dom=""):
    """这次要签哪些名字。

       带上 Emby 的子域是为了一张顶两张：Emby 对外是 <子域>.<它的域名>，
       所以得有 *.<它的域名>；它跟节点同域时这条跟 *.dom 是同一个，自然去重。

       【一定】把节点正在用的域名也带上：这张证书是全机共用的，签一张不含它的
       证书装上去，等于把所有吃证书的节点一起打死。已经被新名字的泛域名盖住了
       就不用重复列。"""
    names = [dom] + ([f"*.{dom}"] if wildcard else [])
    if wildcard and emby_dom:
        names += [emby_dom, f"*.{emby_dom}"]
    nd = node_domain()
    if nd and not _name_covers(names, nd):
        names.append(nd)
    return list(dict.fromkeys(names))

def cert_install_apply(dom, wildcard, cf_token, names=None, node_dom=None):
    """真正签发+安装（非交互，后台跑）。装上去之前先验，验不过绝不碰现有证书。
       成功返回 True，任一步没成返回 False（换域名流程要靠它决定回不回滚）。

       names：这次要签的完整域名列表，不给就按 dom/wildcard 推。
       node_dom：节点【将要】用的域名，给换域名流程用——见 _cert_swap_in 的说明。"""
    G_OK, R, N = "\033[1;32m", "\033[1;31m", "\033[0m"
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print("  正在安装 acme.sh…")
        sh("curl -s https://get.acme.sh | sh -s email=" + (G.get("email") or "a@a.com"),
           check=False)
    if not os.path.exists(acme):
        print(f"{R}  ✗ acme.sh 装不上（检查能不能访问 get.acme.sh），已放弃，"
              f"现有证书没动。{N}")
        return False
    sh(f"{acme} --register-account -m {G.get('email') or 'a@a.com'} --server letsencrypt",
       check=False)
    sh(f"{acme} --set-default-ca --server letsencrypt", check=False)

    env = dict(os.environ)
    if cf_token:
        env["CF_Token"] = cf_token
    want = names or cert_want_names(dom, wildcard)
    if wildcard:
        ds = " ".join(f"-d '{n}'" for n in want)
        print(f"  要签的域名：{', '.join(want)}")
        issue = f"{acme} --issue --dns dns_cf --keylength ec-256 {ds} --server letsencrypt"
    else:
        hooks = ""
        owner = _port80_owner()
        if owner and "nginx" not in owner:
            print(f"{R}  ✗ 80 端口被 {owner} 占着，HTTP-01 验证进不来。先停掉它。{N}")
            return False
        if owner:
            # 让 acme 自己停一下 nginx，别去改用户的 nginx 配置——那份 conf 可能同时装着
            # 443 伪装站 / ws 反代 / Emby，重写它比证书过期还糟。hook 会被记进域名记录，
            # 以后自动续期也照做。
            hooks = " --pre-hook 'systemctl stop nginx' --post-hook 'systemctl start nginx'"
            print("  80 端口被 nginx 占着 → 验证时自动停一下 nginx（约 10 秒）")
        issue = f"{acme} --issue -d {dom} --standalone --keylength ec-256{hooks}"
    print("  正在签发…（走 acme.sh，可能要十几秒到一分钟）")
    r = subprocess.run(issue, shell=True, text=True, capture_output=True, env=env, timeout=600)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    # acme.sh 在证书仍有效时会以退出码 2「跳过签发」，那不是错误；能导出就算成功。
    skipped = any(k in out for k in ("Domains not changed", "Skipping", "Next renewal time",
                                     "Cert success"))
    if r.returncode and not skipped:
        print(f"{R}  ✗ 签发失败，现有证书没动：{N}\n" + out[-1200:])
        return False

    # 先导到临时路径验一遍，验过了才动真的——这一步是「不可用就回退」的关键
    tmpc, tmpk = ACME_CRT + ".new", ACME_KEY + ".new"
    os.makedirs(os.path.dirname(ACME_CRT), exist_ok=True)
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {tmpc} --key-file {tmpk}", check=False)
    ok, why = cert_validate(tmpc, tmpk, want)
    if not ok:
        for p in (tmpc, tmpk):
            try: os.remove(p)
            except OSError: pass
        print(f"{R}  ✗ 新证书没通过校验（{why}），已丢弃，现有证书没动。{N}")
        return False
    print(f"  ✓ 新证书校验通过：{why}")
    return _cert_swap_in(tmpc, tmpk, dom, wildcard, node_dom)

def _cert_swap_in(tmpc, tmpk, dom, wildcard, node_dom=None):
    """把验过的新证书换上去，顺带记 reloadcmd、补续期任务、让吃证书的服务重读。
       换的过程带备份：任一步失败就把旧的放回去并重启回来。成功返回 True。

       node_dom：下面那道闸要对着「节点【将要】用的域名」校验。平时它就是
       node_domain()；但【换域名】流程里，state.json 这时还写着旧域名，
       照着它校验会把唯一正确的那张新证书拦下来。所以那条路显式传新域名进来。"""
    G_OK, R, N = "\033[1;32m", "\033[1;31m", "\033[0m"
    # 最后一道闸：这张证书是全机共用的地基，节点正在用的域名【一定】要盖得住。
    # 只校验「盖不盖得住你刚才输的域名」是不够的——输错一个字符（llj 打成 ly），
    # 新证书对它自己完全合法，装上去却让所有吃证书的节点被客户端当场拒绝
    # （tls: bad certificate），而 reality 照常通，看起来就像「随机几个节点坏了」。
    nd = node_domain() if node_dom is None else node_dom
    if nd and not _name_covers(cert_names(tmpc), nd):
        for p in (tmpc, tmpk):
            try: os.remove(p)
            except OSError: pass
        print(f"{R}  ✗ 新证书盖不住节点正在用的域名 {nd}"
              f"（它覆盖的是 {', '.join(cert_names(tmpc)) or '(读不出)'}）。{N}")
        print(f"{R}    装上去会让所有吃证书的节点被客户端拒绝，已丢弃，"
              f"现有证书一个字节没动。{N}")
        print(f"    域名是不是打错了？节点在用的是 {nd}。")
        return False
    bak = {}
    for src, dst in ((tmpc, ACME_CRT), (tmpk, ACME_KEY)):
        if os.path.exists(dst):
            bak[dst] = dst + ".bak"
            shutil.copyfile(dst, bak[dst])
        shutil.move(src, dst)
    try:
        # reloadcmd 记进 acme.sh：没有它，以后自动续期换了磁盘上的证书，
        # sing-box/xray/xy-sub 还捏着启动时读进内存的旧证书，90 天一到全挂。
        sh(f"{os.path.expanduser('~/.acme.sh/acme.sh')} --install-cert -d {dom} --ecc "
           f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{_ACME_RELOAD_HOOK}", check=False)
        _ensure_cron_running()
        if not _acme_cron_ok():
            sh(f"{os.path.expanduser('~/.acme.sh/acme.sh')} --install-cronjob", check=False)
        save_cert_meta(domain=dom, wildcard=wildcard,
                       mode="dns-cf" if wildcard else _acme_mode(dom) or "standalone")
        svcs = _cert_consumers()
        if svcs:
            print(f"  让这些服务重读证书：{'、'.join(svcs)}")
            for svc in svcs:
                sh(f"systemctl restart {svc}", check=False)
    except Exception as e:
        for dst, b in bak.items():
            shutil.copyfile(b, dst)
        for svc in _cert_consumers():
            sh(f"systemctl restart {svc}", check=False)
        print(f"{R}  ✗ 换证书过程出错（{e}），已还原成原来那张并重启回来。{N}")
        return False
    for b in bak.values():
        try: os.remove(b)
        except OSError: pass
    i = cert_info()
    print(f"\n{G_OK}  ✓ 证书已装好{N}")
    print(f"    域名 {i['domain']}" + ("（泛域名）" if i["wildcard"] else ""))
    print(f"    签发 {i['issuer']}   {_cert_left_text(i['secs'])}")
    print(f"    自动续期 {'已配好' if i['cron_ok'] and i['hook_ok'] else '仍不完整，回面板看提示'}")
    if not _installed_state()[1] and not _installed_state()[2]:
        print("    还没装节点：去『1 节点安装』，向导会认出这张证书直接用，不再重复申请。")
    return True

def emby_cert_menu(i):
    """菜单 15 → 4：Emby 的证书跟节点【共用一张】还是【各用各的】。

       原来这里是个盲切换——按下去它自己决定合并还是拆开，而合并不了的时候
       只丢一句「顶替不了」就退出来。你看不到当前处在哪一边、能不能改到另一边、
       改不了是因为什么。现在两个方向并排摆在同一屏上，各自标着状态和原因。"""
    Y, R, GRN, C, N = ("\033[1;33m", "\033[1;31m", "\033[1;32m",
                       "\033[1;36m", "\033[0m")
    while True:
        dom = i.get("emby_domain") or ""
        if not dom:
            print("\n  本机没装自建 Emby（或它没配域名），不涉及证书共用。")
            _ask("  按回车返回...")
            return
        shared = i.get("emby_shared")
        print("\n" + "=" * 60)
        print("  Emby 证书")
        print("=" * 60)
        if shared:
            print(f"  当前:      {GRN}共用一张{N}"
                  f"（Emby 的证书路径是指向节点证书的软链）")
            print(f"  两边都用:  {ACME_CRT}   {_cert_left_text(i['secs'])}")
            print(f"             覆盖 {', '.join(i['names']) or '(读不出)'}")
            print(f"  {GRN}续期一条链{N}：节点这张续期时本来就会 reload nginx，"
                  f"Emby 自动吃到新证书。")
        else:
            print(f"  当前:      {Y}各用各的{N}（两张证书、两条续期链）")
            print(f"  节点这张:  {i['domain'] or '(读不出)'}   "
                  f"{_cert_left_text(i['secs'])}")
            print(f"             {ACME_CRT}")
            print(f"  Emby 这张: *.{dom}   {_cert_left_text(i['emby_secs'])}")
            print(f"             {_emby_crt(dom)}")
            print(f"  {Y}⚠ 两条续期链 = 把「证书过期全挂」的风险配了两份，而且 Emby"
                  f"那条断了不会有人发现{N}")
            print(f"    （平时不看，真挂了才知道）。")
        print("-" * 60)
        # ① 共用
        if shared:
            print(f"  1 共用节点这张证书    {GRN}当前就是{N}")
        elif i.get("emby_covered"):
            print(f"  1 共用节点这张证书    {C}可以合并{N}"
                  f"（两张变一张，少一条会悄悄断掉的续期链）")
        else:
            print(f"  1 共用节点这张证书    {R}✗ 不行{N}：节点这张盖不住 "
                  f"<子域>.{dom}")
            print(f"                        Emby 对外是 emby./mw. 等好几个子域，"
                  f"要泛域名 *.{dom} 才顶得住；")
            print(f"                        节点这张覆盖的是 "
                  f"{', '.join(i['names']) or '(读不出)'}")
            print(f"                        {Y}先去『1 安装证书』用 DNS-01 重签一张"
                  f"泛域名的，再回来合并{N}")
        # ② 各用各的
        if shared:
            print(f"  2 各用各的            Emby 用回它自己那张"
                  f"（备份还在就放回去）")
            print(f"                        {Y}⚠ 它在 acme.sh 的续期记录合并时已经撤了，"
                  f"还原后要去『16 自建 Emby』重签{N}")
        else:
            print(f"  2 各用各的            {GRN}当前就是{N}")
        print("  0 返回")
        c = (_ask("选择（回车=0 返回）: ") or "0").strip()
        if c in ("0", ""):
            return
        if c == "1":
            if shared:
                print(f"\n  {GRN}已经是共用的了{N}：{_emby_crt(dom)} → {ACME_CRT}")
                _ask("  按回车继续...")
            elif not i.get("emby_covered"):
                print(f"\n{R}  合并不了——原因上面写着：节点这张盖不住 <子域>.{dom}。{N}")
                print(f"  先『1 安装证书』重签一张泛域名的。")
                _ask("  按回车继续...")
            else:
                emby_share_confirm(i, dom)
                i = cert_info()                      # 状态变了，重读
        elif c == "2":
            if not shared:
                print(f"\n  {GRN}本来就是各用各的{N}，没什么可改。")
                _ask("  按回车继续...")
            else:
                emby_unshare(dom)
                i = cert_info()
        else:
            print("  无效选择。")

def emby_share_confirm(i, dom):
    """把该说的摆出来再动手。能不能合并由 emby_cert_menu 先判过了，这里只管确认。

       为什么值得合并：两张证书签的是同一个域名家族、各自 90 天、各自续期，
       等于把「证书过期全挂」这个风险配了两份，而且续期链断掉的那份不会有人
       发现（Emby 平时不看、真挂了才知道）。合并成一张之后，节点这张的
       reloadcmd 本来就会 reload nginx，Emby 跟着一起吃到新证书。"""
    print("\n" + "-" * 60)
    print(f"  把 Emby 的证书指到节点这张上（两张变一张）")
    print(f"  Emby 现在用:  {_emby_crt(dom)}   {_cert_left_text(i['emby_secs'])}")
    print(f"  改成指向:     {ACME_CRT}   {_cert_left_text(i['secs'])}")
    print(f"  做法:         把 crt/key 换成指向节点证书的软链（Emby 的 nginx 配置一个字不动，")
    print(f"                以后重跑 Emby 安装也不会把它改回去）")
    print(f"  顺带:         让 acme.sh 别再单独续 *.{dom} 那张（避免它续完把软链覆盖掉）")
    print(f"                原证书文件会备份成 .bak，acme.sh 里的记录只是不再跟踪，不删文件")
    print(f"  以后:         节点这张续期时本来就会 reload nginx，Emby 自动吃到新证书")
    print(f"  改回去:       随时回这一屏选『2 各用各的』")
    print("-" * 60)
    if (_ask("确认合并? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return
    emby_share_apply(dom)

def emby_share_apply(dom):
    """真动手：备份 → 换成软链 → nginx -t → reload；不过就整个还原。"""
    R, GRN, N = "\033[1;31m", "\033[1;32m", "\033[0m"
    pairs = [(_emby_crt(dom), ACME_CRT), (_emby_key(dom), ACME_KEY)]
    for tgt, _src in pairs:
        if not os.path.exists(os.path.dirname(tgt)):
            print(f"{R}  ✗ 找不到 {os.path.dirname(tgt)}，Emby 好像没装完，已放弃。{N}")
            return
    baks = []
    try:
        for tgt, src in pairs:
            if os.path.lexists(tgt):
                bak = tgt + ".bak"
                if os.path.islink(tgt):
                    os.remove(tgt)
                else:
                    shutil.move(tgt, bak)
                    baks.append((bak, tgt))
            os.symlink(src, tgt)
        chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
        if chk.returncode:
            raise RuntimeError((chk.stderr or chk.stdout).strip()[-400:])
        sh("systemctl reload nginx", check=False)
    except Exception as e:
        for tgt, _src in pairs:                       # 先把软链摘掉
            if os.path.islink(tgt):
                os.remove(tgt)
        for bak, tgt in baks:                         # 再把原文件放回去
            shutil.move(bak, tgt)
        sh("systemctl reload nginx", check=False)
        print(f"{R}  ✗ 合并失败，已还原成原来那张并 reload 回去：{e}{N}")
        return
    # acme.sh 每天的 cron 会把 *.dom 那张续期并重新 install-cert，那一步会把软链
    # 覆盖成实体文件，悄无声息地退回两张。--remove 只是让它别再跟踪，不删任何文件。
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if os.path.exists(acme):
        sh(f"{acme} --remove -d '*.{dom}' --ecc", check=False)
        print(f"  已让 acme.sh 不再单独续 *.{dom}（证书文件还在，只是不跟踪了）")
    else:
        # 这一步跳过不影响「现在能不能用」，但少了它，acme.sh 的每日 cron 续期
        # *.dom 时会 install-cert 把软链覆盖成实体文件，悄无声息退回两张证书。
        print(f"{R}  ⚠ 没找到 acme.sh，没能撤掉 *.{dom} 的单独续期记录。{N}")
        print(f"    它下次自动续期会把软链覆盖掉、退回两张证书（不影响使用，"
              f"只是又变回两条续期链）。手工撤：acme.sh --remove -d '*.{dom}' --ecc")
    save_cert_meta(emby_shared=True, emby_domain=dom)
    for bak, _t in baks:
        print(f"  原证书已备份：{bak}")
    print(f"\n{GRN}  ✓ 合并完成{N}：Emby 和节点现在共用 {ACME_CRT}")
    print(f"    {_cert_left_text(_cert_secs_left())}；续期时 reload nginx，Emby 自动跟上。")

def emby_unshare(dom):
    """把软链摘掉、还原成 Emby 自己那张（.bak 还在就放回去）。"""
    R, GRN, N = "\033[1;31m", "\033[1;32m", "\033[0m"
    done = False
    for tgt in (_emby_crt(dom), _emby_key(dom)):
        if os.path.islink(tgt):
            os.remove(tgt)
            done = True
        if os.path.exists(tgt + ".bak") and not os.path.exists(tgt):
            shutil.move(tgt + ".bak", tgt)
    if not done:
        print("  本来就不是共用的，没动。")
        return
    save_cert_meta(emby_shared=False)
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:
        print(f"{R}  ⚠ 还原后 nginx -t 不过（多半是备份的旧证书已过期）：{N}")
        print("   " + (chk.stderr or chk.stdout).strip()[-300:])
        print(f"   去『16 自建 Emby』重新签一张它自己的证书即可。")
        return
    sh("systemctl reload nginx", check=False)
    print(f"{GRN}  ✓ 已还原：Emby 用回自己那张证书。{N}")
    print(f"    注意它在 acme.sh 里的续期记录已经撤了，到期前记得去『16 自建 Emby』重签。")

_ACME_STATUS_COLOR = {"抢占中": "\033[1;31m", "没人用": "\033[1;33m",
                      "节点在用": "\033[1;32m", "Emby 在用": "\033[1;32m",
                      "别处在用": "\033[1;36m"}

def cert_clean_flow():
    """菜单 15 → 5：清理 acme.sh 里没用的旧证书记录。

       为什么值得有：acme.sh 是「登记了就一直续」的，换域名、换用途之后旧记录
       没人删。轻则占地方，重则抢占——它存着的安装路径还指着节点的证书文件，
       每次续期都会把节点正在用的那张覆盖掉（半夜发作，最难查）。"""
    R, Y, G_, N = "\033[1;31m", "\033[1;33m", "\033[1;32m", "\033[0m"
    recs = acme_records()
    if not recs:
        print("\n  acme.sh 里没有任何域名记录。")
        _ask("  按回车返回...")
        return
    print("\n" + "=" * 60)
    print("  acme.sh 里的证书记录")
    print("=" * 60)
    for n, r in enumerate(recs, 1):
        c = _ACME_STATUS_COLOR.get(r["status"], "")
        print(f"  {n:>2}. {_pad(r['domain'], 26)}{c}{r['status']}{N}"
              f"   {_cert_left_text(r['secs'])}")
        if r["path"]:
            print(f"      装到 {r['path']}")
    print("-" * 60)
    print(f"  {G_}节点在用 / Emby 在用 / 别处在用{N} = 有主，不列入可删")
    print(f"  {R}抢占中{N} = 装到节点的证书文件、却不是节点域名 —— "
          f"它每次续期都会把节点的证书覆盖掉，强烈建议删")
    print(f"  {Y}没人用{N} = 装到的文件已经不在、或压根没配过安装路径")
    can = [r for r in recs if r["removable"]]
    if not can:
        print(f"\n  {G_}没有需要清理的，都是有主的。{N}")
        _ask("  按回车返回...")
        return
    print(f"\n  可删的：{', '.join(r['domain'] for r in can)}")
    print("  删除 = 让 acme.sh 不再自动续它（--remove）。磁盘上的证书文件默认保留，")
    print("        下一步会单独问要不要一并删掉。")
    print("-" * 60)
    raw = _ask("选哪些（逗号分隔编号，0/all=全部可删的，回车=取消）: ").strip()
    if not raw:
        print("  已取消。")
        return
    if raw == "0" or raw.lower() == "all":
        picked = can
    else:
        picked = []
        for tok in raw.replace("，", ",").split(","):
            tok = tok.strip()
            if tok.isdigit() and 1 <= int(tok) <= len(recs):
                r = recs[int(tok) - 1]
                if not r["removable"]:
                    print(f"  ⚠ 跳过 {r['domain']}：{r['status']}，不能删。")
                    continue
                picked.append(r)
            elif tok:
                print(f"  ⚠ 忽略无效项: {tok}")
    if not picked:
        print("  没选中任何可删的，返回。")
        return
    print(f"\n  将撤销自动续期：{', '.join(r['domain'] for r in picked)}")
    purge = (_ask("  连磁盘上的证书文件也一起删掉? y 删 / 回车=只停续期、文件保留: ")
             or "").strip().lower() in ("y", "yes")
    print(f"  文件：{'一并删除' if purge else '保留（以后想恢复还找得回来）'}")
    if (_ask("确认? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print(f"{R}  ✗ 找不到 acme.sh，无法撤销。{N}")
        return
    for r in picked:
        sh(f"{acme} --remove -d '{r['domain']}' --ecc", check=False)
        sh(f"{acme} --remove -d '{r['domain']}'", check=False)
        msg = f"  ✓ {r['domain']}：已停止自动续期"
        if purge:
            try:
                shutil.rmtree(r["dir"])
                msg += "，证书文件已删除"
            except OSError as e:
                msg += f"（文件删除失败：{e}）"
        print(msg)
    left = [x["domain"] for x in acme_records() if x["removable"]]
    print(f"\n  剩下的可删项：{', '.join(left) if left else '无'}")
    if any(r["status"] == "抢占中" for r in picked):
        print(f"  {Y}刚删掉的里面有『抢占中』的——建议现在点『3 强制重签』，"
              f"把节点的证书重签回来。{N}")
    _ask("  按回车返回...")

# ============================================================ 换域名（菜单 15 → 2）
#
# 域名散落在这台机的下面这些地方，漏掉任何一处的后果都长得一样：
# 某天一半节点连不上，而日志里只有一句「连不上」，没人会往域名上想。
#
#   state.json 的 host / domain     所有读 G["domain"] 的地方、下次进安装向导
#   sub.host                        订阅 URL / GitHub 中转 / 自建 DNS 的 DoH 地址都读它
#   xy-nodes.txt                    每条分享链接的 @域名:端口 和 sni= / host=
#   sing-box config.json            非 reality 入站的 tls.server_name、h2/httpupgrade 的 host
#   xray config.json                tlsSettings.serverName、xhttpSettings.host
#   nginx bgpeer.conf               三处 server_name（80 跳转 / 443 伪装站 / 本地 https）
#   /etc/ssl/sb/acme.crt            证书本身
#   acme.sh 里旧域名那条记录        不撤掉的话它的每日 cron 半夜把证书覆盖回旧域名那张
#   Emby 的 .env + nginx + 证书     Emby 对外是 <子域>.<域名>
#
# ⚠ 最容易写错的一处：reality 的 sni= 是【借用的第三方站】（s0.awsstatic.com 之类），
#   跟你自己的域名没有半点关系。跟着换 = reality 全挂，而且是静默的握手失败，
#   现象还偏偏是「另外几个节点好好的」。所以带 security=reality 的链接只动 @host，
#   query 一个字不碰；两个核心的配置里也整块跳过 reality 入站。

def _relink_domain(link, old, new):
    """把一条分享链接里【属于本机域名】的地方换成 new。认不出来就原样返回。

       只改值等于 old 的字段，其余字节不动——端口/uuid/密码/路径/short-id 全程不变，
       换完客户端刷一次订阅就行，不用重新导入。"""
    if not (old and new) or old == new or not link:
        return link
    if link.startswith("vmess://"):
        b = link[8:]
        try:
            j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        except Exception:
            return link
        hit = False
        for k in ("add", "host", "sni"):                 # ps(节点名)/id/path 不动
            if str(j.get(k, "")).strip() == old:
                j[k] = new; hit = True
        return vmess_link(j) if hit else link
    try:
        P = urllib.parse.urlsplit(link)
    except ValueError:
        return link
    netloc = P.netloc
    if (P.hostname or "").lower() == old.lower():
        # 只换主机名那一段：userinfo（uuid/密码，可能带 @ 和 :）和端口原样保留
        at = netloc.rfind("@")
        ui, hp = (netloc[:at + 1], netloc[at + 1:]) if at >= 0 else ("", netloc)
        i = hp.rfind(":")
        netloc = ui + new + (hp[i:] if i >= 0 else "")
    query = P.query
    if "security=reality" not in query:                  # ← reality 的 sni 是借用站，不能动
        segs = []
        for s in query.split("&"):
            k, eq, v = s.partition("=")
            if eq and urllib.parse.unquote(v) == old:
                v = new                                  # 域名本身不含需要转义的字符
            segs.append(k + eq + v)
        query = "&".join(segs)
    return urllib.parse.urlunsplit((P.scheme, netloc, P.path, query, P.fragment))

def _links_set_domain(text, old, new):
    """整份 xy-nodes.txt 的域名改写。『# 订阅链接:』那段尾注原样保留。"""
    out, tail = [], False
    for ln in text.split("\n"):
        if ln.strip().startswith("#"):
            tail = True
        if not tail and "://" in ln:
            ln = _relink_domain(ln.strip(), old, new)
        out.append(ln)
    return "\n".join(out)

def _sb_set_domain(data, old, new):
    """sing-box 入站里凡是等于 old 的域名字段换成 new，返回改了几处。
       reality 入站整块跳过——它的 server_name / handshake.server 是借用站。"""
    n = 0
    for ib in data.get("inbounds", []):
        tls = ib.get("tls") or {}
        if tls.get("reality"):
            continue
        if tls.get("server_name") == old:
            tls["server_name"] = new; n += 1
        tr = ib.get("transport") or {}
        h = tr.get("host")
        if h == old:                                     # httpupgrade：字符串
            tr["host"] = new; n += 1
        elif isinstance(h, list) and old in h:           # h2：列表
            tr["host"] = [new if x == old else x for x in h]; n += 1
        hd = tr.get("headers") or {}
        if hd.get("Host") == old:
            hd["Host"] = new; n += 1
    return n

def _xr_set_domain(data, old, new):
    """xray 入站的同一件事。同样整块跳过 reality。"""
    n = 0
    for ib in data.get("inbounds", []):
        st = ib.get("streamSettings") or {}
        if st.get("security") == "reality" or st.get("realitySettings"):
            continue
        tl = st.get("tlsSettings") or {}
        if tl.get("serverName") == old:
            tl["serverName"] = new; n += 1
        for key in ("xhttpSettings", "wsSettings", "httpupgradeSettings", "httpSettings"):
            s = st.get(key) or {}
            if s.get("host") == old:
                s["host"] = new; n += 1
            elif isinstance(s.get("host"), list) and old in s["host"]:
                s["host"] = [new if x == old else x for x in s["host"]]; n += 1
            hd = s.get("headers") or {}
            if hd.get("Host") == old:
                hd["Host"] = new; n += 1
    return n

_SRVNAME_RE = r'(?m)^(\s*server_name\s+)%s(\s*;)'

def _nginx_sub_domain(txt, old, new):
    """nginx 配置里的 server_name 改写（只认完全等于 old 的那条）。"""
    return re.sub(_SRVNAME_RE % re.escape(old), lambda m: m.group(1) + new + m.group(2), txt)

def _domain_targets(old):
    """这次换域名会动到哪些文件 → [(路径, 说明)]。只列真的存在、真的含旧域名的。
       给确认框用：让你在按下去之前看到完整清单，而不是事后猜它动了什么。"""
    out = []
    for p, why in ((STATE_FILE, "安装记录（域名/前缀/协议表）"),
                   (HOST_FILE, "订阅 host（订阅 URL / GitHub 中转 / 自建 DNS 都读它）"),
                   (NODE_FILE, "分享链接"),
                   (f"{SB_DIR}/config.json", "sing-box 入站"),
                   (f"{XRAY_DIR}/config.json", "xray 入站"),
                   (NGINX_CONF, "nginx server_name")):
        try:
            if old in open(p, encoding="utf-8", errors="replace").read():
                out.append((p, why))
        except OSError:
            pass
    return out

def _domain_resolves_here(dom):
    """新域名解析到不到本机公网 IP → (ok, 说明)。

       这一步不能省：换完之后客户端连的就是它。解析还没生效就换，等于把所有节点
       一次性打死，而且是在你已经改完一切、服务也重启完之后才发现。"""
    try:
        got = sorted({i[4][0] for i in socket.getaddrinfo(dom, None)})
    except Exception as e:
        return False, f"解析不到（{type(e).__name__}）"
    mine = public_ip()
    if not mine:
        return True, f"解析到 {', '.join(got)}（本机公网 IP 查不到，没法比对，按通过算）"
    if mine in got:
        return True, f"解析到 {mine}，正是本机"
    return False, f"解析到 {', '.join(got)}，而本机是 {mine}"

def _acme_forget(dom):
    """让 acme.sh 不再跟踪这个域名（只停止续期，不删磁盘上的任何文件）。

       换完域名【必须】做这一步。旧域名那条记录里存着「装到哪个文件、装完跑什么」，
       它的每日 cron 照常跑，续完就把节点正在用的证书覆盖成旧域名那张，还顺手按
       记录里的 reloadcmd 重启了核心。发作在半夜、你什么都没做，第二天一半节点
       连不上、reality 好好的。

       真撤掉了才返回 True —— 调用方据此决定要不要打那行「已经撤掉了」，
       别声称做了一件其实没做的事。"""
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        return False
    for d in (dom, f"*.{dom}"):
        sh(f"{acme} --remove -d '{d}' --ecc", check=False)
        sh(f"{acme} --remove -d '{d}'", check=False)
    return True

def _emby_env_files():
    return [p for p in ("/opt/emby-stack/.env", "/opt/media-stack/.env") if os.path.exists(p)]

_EMBY_NGX = "/etc/nginx/conf.d/media-stack.conf"

def _emby_set_domain(old, new, shared):
    """Emby 跟着换域名：.env 的 DOMAIN、nginx 的 server_name/证书路径、证书软链。
       nginx -t 不过就整体还原。返回成功与否。"""
    Y, R, GRN, N = "\033[1;33m", "\033[1;31m", "\033[1;32m", "\033[0m"
    bak, made = {}, []
    def snap(p):
        try: bak[p] = open(p, "rb").read()
        except OSError: pass
    def undo():
        for p, b in bak.items():
            try: open(p, "wb").write(b)
            except OSError: pass
        for p in made:
            try: os.remove(p)
            except OSError: pass
    for envf in _emby_env_files():
        snap(envf)
        txt = open(envf, encoding="utf-8").read()
        open(envf, "w", encoding="utf-8").write(
            re.sub(r"(?m)^(DOMAIN=)['\"]?" + re.escape(old) + r"['\"]?\s*$", r"\g<1>" + new, txt))
    if os.path.exists(_EMBY_NGX):
        snap(_EMBY_NGX)
        txt = open(_EMBY_NGX, encoding="utf-8").read()
        # 这份 conf 是 media-stack 生成的，域名只出现在 server_name 和证书路径两处，
        # 整体替换是安全的；不放心也没关系——下面 nginx -t 不过就原样还原。
        open(_EMBY_NGX, "w", encoding="utf-8").write(txt.replace(old, new))
    # 证书：本来就是跟节点共用（软链）的，就在新名字下重建软链；
    # 各用各的则不擅自动它——那张是旧域名的证书，盖不住新域名，得重签。
    if shared:
        for src, dst in ((ACME_CRT, _emby_crt(new)), (ACME_KEY, _emby_key(new))):
            try:
                if os.path.lexists(dst):
                    os.remove(dst)
                os.symlink(src, dst); made.append(dst)
            except OSError as e:
                undo(); print(f"{R}  ✗ Emby 证书软链建不起来（{e}），Emby 那侧已还原。{N}")
                return False
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:
        undo()
        print(f"{R}  ✗ Emby 的 nginx 校验没过，那一侧已整体还原：{N}\n   "
              + (chk.stderr or chk.stdout).strip().replace("\n", "\n   "))
        return False
    for p in (_emby_crt(old), _emby_key(old)):        # 旧名字的软链留着只会误导
        if os.path.islink(p):
            try: os.remove(p)
            except OSError: pass
    print(f"  {GRN}Emby 也换好了{N}：{old} → {new}"
          + ("（证书继续跟节点共用一张）" if shared else ""))
    if not shared:
        print(f"{Y}    ⚠ Emby 的证书原来是独立一张、签的是 {old}，盖不住 {new}。"
              f"去菜单 15『4 Emby 证书』并成一张，或自己给它重签。{N}")
    return True

def _cert_usable_for(names):
    """磁盘上【正在用的】那张证书，能不能顶住这些域名 → (ok, 说明)。

       跟 cert_validate 是同一套四条判据（解析得开 / 覆盖得到 / 没过期 / 私钥配对），
       只是对象是现役的 ACME_CRT 而不是刚签出来的临时文件。

       为什么要单独有这一道：换域名时如果现有证书【已经】盖得住新域名，流程会
       跳过重签——可「盖得住」不等于「能用」。它可能还有三天到期，也可能是上次
       签发中断留下的「新证书配旧私钥」（文件看着一切正常、日期也没问题，握手
       直接失败）。跳过重签又不验一遍，等于把这两种情况原样带进新域名。"""
    return cert_validate(ACME_CRT, ACME_KEY, names)

def _domain_switch_notice(new, emby_new=""):
    """换完域名之后，哪些【对外地址】跟着变了、去哪儿看新值。

       证书是全机共用的地基，吃它的东西各自对外报一个地址：订阅、节点、
       自建 DNS 的 DoH、GitHub 中转、Emby。换完不逐个说清楚，你只会发现
       「某个东西忽然连不上」，然后挨个去猜。"""
    out = []
    try:
        t = _sub_service_text()
        if t:
            out.append(("订阅地址", t, "面板 2　⚠ host 变了，客户端要【重新导入】，不是刷新"))
    except Exception:
        out.append(("订阅地址", "(读不出)", "面板 2　⚠ host 变了，客户端要重新导入"))
    out.append(("节点链接", f"@{new}:端口", "面板 2　单条分享链接发给别人过的，要重发"))
    try:
        doh = _selfdns_doh()
        if doh:
            out.append(("自建 DNS 的 DoH", doh,
                        "面板 13　手机「专用DNS」/ 路由器里填的那个要改"))
    except Exception:
        pass
    try:
        gh = _ghrelay_prefix()
        if gh:
            out.append(("GitHub 中转", gh, "面板 14　订阅里自动换好了；手工引用过的要改"))
    except Exception:
        pass
    if emby_new:
        out.append(("Emby", f"https://<子域>.{emby_new}", "面板 16　App / 客户端里存的地址要改"))
    return out

def cert_change_domain_apply(new, cf_token="", emby_new=""):
    """换域名（非交互，后台跑）。成功返回 True。

       顺序是有讲究的，反了会出人命：

         1. 先改两个核心的配置 + nginx，各自跑校验。纯本地、可回滚，而且这时候
            服务还没重启，写坏了也没有任何东西是坏的。
         2. 校验全过了才去签证书。反过来的话：证书已经换成新域名、配置却因为
            校验不过回滚回了旧域名 —— 节点用旧域名、证书是新域名，所有吃证书的
            节点当场被客户端拒绝（tls: bad certificate），而 reality 照常通，
            现象就是「随机几个节点坏了」，最难查的那一种。
         3. 证书也拿到了，才动 state.json / sub.host / 分享链接 / 订阅。
         4. 最后撤掉旧域名的 acme 记录再重启。"""
    G_OK, Y, R, N = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    old = node_domain()
    if not old:
        print(f"{R}  ✗ 本机没有域名（自签 + IP 直连），没有域名可换。{N}")
        return False
    if old == new:
        print("  新旧域名一样，没什么可做的。")
        return False
    print(f"\n  {old}  →  {new}\n")

    bak = {}
    def snap(p):
        try: bak[p] = open(p, "rb").read()
        except OSError: pass
    def restore():
        for p, b in bak.items():
            try: open(p, "wb").write(b)
            except OSError: pass

    # ── 1. 两个核心的入站配置 ───────────────────────────────────────────
    cores = []
    for cfg, binp, svc, setter in (
            (f"{SB_DIR}/config.json",   SB_BIN,   "sing-box", _sb_set_domain),
            (f"{XRAY_DIR}/config.json", XRAY_BIN, "xray",     _xr_set_domain)):
        if not os.path.exists(cfg):
            continue
        try:
            data = json.load(open(cfg))
        except Exception as e:
            print(f"{R}  ✗ {svc} 的配置读不出来（{e}），已放弃，一个字节都没改。{N}")
            restore(); return False
        snap(cfg)
        n = setter(data, old, new)
        json.dump(data, open(cfg, "w"), indent=2)
        print(f"  {svc}: 改了 {n} 处（reality 入站整块没动——它指的是借用站）")
        cores.append((cfg, binp, svc))
    errs = []
    for cfg, binp, svc in cores:
        if os.path.exists(binp):
            ok, msg = core_check(binp, cfg)
            if not ok:
                errs.append((svc, msg))
    if errs:
        restore()
        print(f"{R}  ✗ 配置校验没过，已整体回滚、服务一个都没重启（节点照常）：{N}")
        for svc, msg in errs:
            print(f"    {svc}: {(msg or '').splitlines()[-1] if msg else '校验失败'}")
        return False

    # ── 2. nginx ───────────────────────────────────────────────────────
    if os.path.exists(NGINX_CONF):
        snap(NGINX_CONF)
        txt = open(NGINX_CONF, encoding="utf-8").read()
        open(NGINX_CONF, "w", encoding="utf-8").write(_nginx_sub_domain(txt, old, new))
        chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
        if chk.returncode:
            restore()
            print(f"{R}  ✗ nginx 校验没过，已整体回滚（含两个核心的配置）：{N}\n   "
                  + (chk.stderr or chk.stdout).strip().replace("\n", "\n   "))
            return False
        print("  nginx: server_name 已改，nginx -t 通过")

    # ── 3. 证书（到这一步才动，前面全是可回滚的本地改动）─────────────────
    names = [new, f"*.{new}"]
    if emby_new and not _name_covers(names, "x." + emby_new):
        names += [emby_new, f"*.{emby_new}"]
    need = [new, f"*.{new}"] + ([f"x.{emby_new}"] if emby_new else [])
    have = cert_names()
    if _name_covers(have, new) and _name_covers(have, "x." + new) and \
            (not emby_new or _name_covers(have, "x." + emby_new)):
        # 「盖得住」不等于「能用」：可能快到期了，也可能是新证书配旧私钥。
        # 跳过重签就必须在这儿补上这一验，否则等于把坏证书原样带进新域名。
        ok, why = _cert_usable_for(need)
        if not ok:
            restore()
            print(f"{R}  ✗ 证书不可用：{why}{N}")
            print(f"{R}    现有证书虽然名字上盖得住 {new}，但它本身有问题，"
                  f"换过去所有吃证书的节点会当场被客户端拒绝。{N}")
            print(f"{R}    整件事已回滚，两个核心的配置和 nginx 都还是 {old}，"
                  f"节点照常跑着。{N}")
            print(f"    先回面板『3 强制重签』把证书修好，再来换域名。")
            return False
        print(f"  证书: 现有这张已经盖得住 {new} 和 *.{new}，"
              f"且校验通过（{why}），不用重签。")
    elif not cert_install_apply(new, True, cf_token, names=names, node_dom=new):
        restore()
        print(f"{R}  ✗ 新域名的证书没拿到，整件事已回滚 —— 两个核心的配置和 nginx "
              f"都还是 {old}，节点照常跑着。{N}")
        return False
    # 不管走了哪条路，最后都拿【现役的那张】再验一遍。签发那条路里
    # cert_install_apply 验的是临时文件，换上去之后没人再看一眼。
    ok, why = _cert_usable_for(need)
    if not ok:
        restore()
        print(f"{R}  ✗ 换上去的证书通不过最终校验：{why}{N}")
        print(f"{R}    两个核心的配置和 nginx 已回滚到 {old}；但证书文件这时候"
              f"可能已经换过了。{N}")
        print(f"{R}    立刻回面板『3 强制重签』，把 {old} 的证书重签回来。{N}")
        return False
    print(f"  {G_OK}✓ 证书最终校验通过：{why}{N}")

    # ── 4. 安装记录 / 订阅 host / 分享链接 / 三格式订阅 ───────────────────
    for p in (STATE_FILE, HOST_FILE, NODE_FILE):
        snap(p)
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        st = {}
    st["domain"] = new
    if (st.get("host") or "") in (old, ""):
        st["host"] = new
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    try:
        if open(HOST_FILE).read().strip() == old:
            open(HOST_FILE, "w").write(new)
    except OSError:
        pass
    try:
        txt = open(NODE_FILE, encoding="utf-8").read()
        open(NODE_FILE, "w", encoding="utf-8").write(_links_set_domain(txt, old, new))
        print("  分享链接已改写（端口/uuid/密码/路径/short-id 一个字节没动）")
    except OSError as e:
        print(f"{Y}  ⚠ 分享链接没改成（{e}）{N}")
    G["domain"] = new
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)
        print("  三格式订阅已按新域名重新渲染")
    except Exception as e:
        print(f"{Y}  ⚠ 订阅刷新失败（{e}）—— 节点本身已经是新域名了，"
              f"回面板点一次『更新配置』补上即可。{N}")

    # ── 5. Emby ────────────────────────────────────────────────────────
    emby_old = _emby_cert()[0]
    if emby_new and emby_old and emby_new != emby_old:
        _emby_set_domain(emby_old, emby_new, _emby_shared(emby_old))

    # ── 6. 撤掉旧域名的 acme 记录（见 _acme_forget 的说明，这步不能省）──────
    if _acme_forget(old):
        print(f"  已让 acme.sh 不再跟踪 {old}（不撤的话它的每日续期会覆盖掉节点证书）")
    else:
        print(f"{Y}  ⚠ 没找到 acme.sh，{old} 的续期记录没能撤掉。要是它还在，"
              f"半夜会把证书覆盖回旧域名那张 —— 回面板『5 清理旧证书』看一眼。{N}")

    # ── 7. 让吃证书的服务重读，顺带把新配置加载进去 ──────────────────────
    svcs = _cert_consumers() or [s for _, _, s in cores]
    print(f"\n{G_OK}  ✓ 域名已换成 {new}{N}")
    print(f"    重启：{'、'.join(svcs)}")
    print(f"\n{Y}  ⚠ 下面这些对外地址跟着变了，都要去改：{N}")
    print("-" * 60)
    for what, val, where in _domain_switch_notice(new, emby_new):
        print(f"  {Y}{what}{N}")
        print(f"      {val}")
        print(f"      {where}")
    print("-" * 60)
    restart_services(*svcs)
    return True

def _emby_default_base(new):
    """Emby 对外是 <子域>.<域名>，而节点域名是一个完整主机名。节点换到 jp2.example.net
       时 Emby 多半该跟到 example.net（跟现在 llj.679588.xyz / 679588.xyz 的关系一样）。
       只是个默认值，问的时候可以改。"""
    parts = new.split(".")
    return ".".join(parts[1:]) if len(parts) >= 3 else new

def cert_change_domain_flow(i):
    """菜单 15 → 2 更换域名。交互问完 + 把要动的东西全摆出来，确认后转后台。"""
    Y, R, GRN, N = "\033[1;33m", "\033[1;31m", "\033[1;32m", "\033[0m"
    old = node_domain()
    if not old:
        print(f"\n  本机是{Y}自签证书 + IP 直连{N}，没有域名可换。")
        print("  想用域名：先『1 安装证书』装一张，再重装节点。")
        _ask("  按回车返回...")
        return
    print("\n" + "=" * 60)
    print(f"  更换域名　　当前：{GRN}{old}{N}")
    print("=" * 60)
    print("  换的是【你自己的域名】——客户端连的那个、证书签的那个。")
    print(f"  {Y}不是{N} reality 借用的伪装站（那个在菜单 4，现在是 {G.get('sni') or '(未设)'}）。")
    print("-" * 60)
    _ip = public_ip()
    print(f"{Y}  ⚠ 动手之前，新域名这两件事必须先做好，否则证书根本签不出来：{N}")
    print(f"{Y}     ① A 记录指向本机公网 IP　{N}"
          + (f"{GRN}{_ip}{N}" if _ip else f"{R}(本机公网 IP 查不到，自己确认){N}"))
    print(f"       没这条：证书签得出来，但客户端连不上——等于把所有节点一次性打死。")
    print(f"{Y}     ② 这个域名要在 Cloudflare 的区域里（DNS 托管在 CF）{N}")
    print(f"       泛域名证书走 DNS-01，acme 要用 CF 的 API 往区域里写一条 TXT 验证记录。")
    print(f"       域名不在 CF 名下，Token 权限再对也签不出来。")
    print(f"     解析改完【等生效】再来——DNS 有缓存，改完立刻查多半还是旧值。")
    print("-" * 60)
    new = _ask("  新域名（上面两条都做好了再填，回车取消）: ").strip().lower().rstrip(".")
    if not new:
        print("  已取消。"); return
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", new):
        print("  域名格式不对，已取消。"); return
    if new == old:
        print("  跟现在一样，没什么可换的。"); return

    ok, why = _domain_resolves_here(new)
    print(f"  解析检测: {(GRN + '通过') if ok else (R + '不对')} · {why}{N}")
    if not ok:
        print(f"{R}  换完之后客户端连的就是这个域名，解析不到本机 = 所有节点一起断。{N}")
        print(f"     去 DNS 服务商那边把 {new} 的 A 记录指到 "
              f"{_ip or '本机公网 IP'}，等生效（几分钟到几十分钟）再回来。")
        print(f"     本机查一下：dig +short {new}　或　nslookup {new}")
        print(f"     {Y}走 CF 小黄云代理的话这里会查到 CF 的 IP，那是正常的——"
              f"但节点不能套 CF，请先关掉小黄云改成【仅 DNS】。{N}")
        if (_ask("  仍要继续? y 继续 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
            print("  已取消，一个字节都没改。"); return

    # ── 证书：够用就不重签 ──────────────────────────────────────────
    have = cert_names()
    covered = _name_covers(have, new) and _name_covers(have, "x." + new)
    cf_token = ""
    if covered:
        print(f"  证书:     {GRN}现有证书已经盖得住 {new} 和 *.{new}，不用重签{N}")
    elif _acme_has_cf():
        print("  证书:     要重签一张泛域名的（acme.sh 里已存着 Cloudflare 凭据，直接用）")
    else:
        print("  证书:     要重签一张泛域名的，需要 Cloudflare API Token")
        print("            （CF 后台 → 我的个人资料 → API 令牌 → 创建令牌 →")
        print("             用「编辑区域 DNS」模板，区域选新域名那个）")
        cf_token = _ask("  粘贴 Token: ").strip()
        if not cf_token:
            print("  没填 Token，已取消。"); return

    # ── Emby ──────────────────────────────────────────────────────
    emby_old, emby_new = i.get("emby_domain") or _emby_cert()[0], ""
    if emby_old:
        dft = _emby_default_base(new)
        print(f"\n  本机有自建 Emby，它现在的域名是 {GRN}{emby_old}{N}"
              f"（对外是 <子域>.{emby_old}）")
        ans = _ask(f"  Emby 也换成（回车 = {dft}，输 n = 不动它）: ").strip().lower().rstrip(".")
        if ans in ("n", "no"):
            emby_new = ""
            print(f"{Y}    Emby 留在 {emby_old}。那它就得自己一张证书、自己一条续期链，"
                  f"而且旧域名的解析不能撤。{N}")
        else:
            emby_new = ans or dft
            if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", emby_new):
                print("  域名格式不对，已取消。"); return

    # ── 把要动的东西全摆出来再问 ────────────────────────────────────
    targets = _domain_targets(old)
    names = [new, f"*.{new}"]
    if emby_new and not _name_covers(names, "x." + emby_new):
        names += [emby_new, f"*.{emby_new}"]
    print("\n" + "-" * 60)
    print(f"  换域名:    {old}  →  {new}")
    if emby_new:
        print(f"  Emby:      {emby_old}  →  {emby_new}")
    if not covered:
        print(f"  要签的证书: {', '.join(names)}")
    print(f"  会动这些文件（{len(targets)} 个）:")
    for p, whyt in targets:
        print(f"      {p}\n        {whyt}")
    print(f"  不会动:    reality 的借用站（{G.get('sni') or '(未设)'}）、"
          f"端口 / uuid / 密码 / 路径 / short-id")
    print(f"             CDN 套用那几个节点（它们走自己的域名，见菜单 9）")
    print(f"  安全网:    先改配置再跑两个核心自带的校验，任一不过整体回滚、"
          f"服务一个都不重启；")
    print(f"             nginx -t 不过连核心一起回滚；这些全过了才去签证书，"
          f"证书没拿到也整体回滚。")
    print("-" * 60)
    print(f"{Y}  ⚠ 证书是全机共用的地基，吃它的每一样东西对外报的地址都要跟着改：{N}")
    for _what, _v, _where in _domain_switch_notice(new, emby_new):
        print(f"{Y}      · {_what}　→　{_where}{N}")
    print(f"{Y}    换完会把每一条的【新值】打出来，照着去各自的配置里改一遍。{N}")
    print(f"{Y}  ⚠ 订阅地址的 host 会变，客户端要【重新导入】一次订阅，不是刷新。{N}")
    print(f"{Y}  ⚠ 重启核心会掐断代理链路，挂着本机代理连的 SSH 会断——"
          f"不用管，后台会跑完。{N}")
    print("-" * 60)
    # 换域名会动到全机每一样吃证书的东西，故意不接受 y —— 必须完整打出 yes，避免手滑
    print(f"  {R}输入 yes 确认{N}；回车 / n / 其它任何输入都是取消。")
    if _ask("  > ").strip() != "yes":
        print("  已取消，一个字节都没改。")
        return
    plan = {"op": "cert-domain", "domain": new, "cf_token": cf_token,
            "emby_new": emby_new, "G": dict(G)}
    _node_op_dispatch(plan,
                      lambda: cert_change_domain_apply(new, cf_token, emby_new))

def cert_menu():
    """菜单 15：证书管理。"""
    while True:
        i = cert_panel()
        print(f"  1 安装证书      当前：{'已安装' if i['exists'] else '未安装'}")
        _nd = node_domain()
        print("  2 更换域名      " +
              (f"当前：{_nd}（连证书、节点链接、订阅一起换）" if _nd
               else "本机没有域名（自签 + IP）"))
        print("  3 强制重签      重新签一张并让所有服务重读（到期前后、或怀疑证书坏了时用）")
        if i["emby_domain"]:
            print("  4 Emby 证书        " +
                  ("当前：共用一张" if i["emby_shared"] else
                   "当前：各用各的（两张证书、两条续期链）")
                  + "　进去可两边切换")
        _can = [r for r in acme_records() if r["removable"]]
        print("  5 清理旧证书    " +
              (f"\033[1;33m有 {len(_can)} 个没用/抢占的记录\033[0m" if _can else "都是有主的"))
        print("  0 返回")
        c = (_ask("选择（回车=0 返回）: ") or "0").strip()
        if c == "0" or c == "":
            return
        if c == "1":
            cert_install_flow(i)
        elif c == "2":
            cert_change_domain_flow(i)
        elif c == "3":
            cert_fix()
        elif c == "4" and i["emby_domain"]:
            emby_cert_menu(i)
        elif c == "5":
            cert_clean_flow()
        else:
            print("  无效选择。")

def cert_fix_run():
    """真正干活的那部分（非交互）。菜单入口把它派到独立会话里跑，见 cert_fix()。

       强制续期 + 重新导出证书 + 把 reloadcmd 记进 acme.sh + 重启吃证书的几个服务。

       为什么值得单独做个按钮：证书 90 天一换，而 sing-box / xray / xy-sub 都只在启动时
       把它读进内存。续期这条链上任何一环断了（cron 没装、80 被占、reloadcmd 压根没记过），
       表现都是「某天突然全部连不上」——日志里只有一句连不上，没人会往证书上想。"""
    dom = ""
    try:
        dom = json.load(open(STATE_FILE)).get("domain", "")
    except Exception:
        pass
    if not dom:
        print("\n  本机用的是自签证书（没有域名），不涉及 acme 续期。")
        return
    print(f"域名 {dom}    磁盘上的证书：{_cert_left_text(_cert_secs_left())}")
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not os.path.exists(acme):
        print("✗ 找不到 acme.sh，无法续期。")
        return
    # 自动续期这条链有三环，缺一环都是「某天突然全挂」：
    #   ① acme.sh 自己的 cron 每天跑 --cron
    #   ② 到期前 30 天自动重签，写出新证书文件
    #   ③ reloadcmd 通知 sing-box/xray/xy-sub 重新读证书
    # ① 是 acme.sh 装的时候顺带装的，但系统换过 cron、迁移过、或者被清理过就没了，
    # 而且它没了【一点声响都没有】。这里顺手确认一遍，缺了就补。
    if not have("crontab"):
        print("  ⚠ 本机连 cron 都没装——自动续期从第一天起就没在跑，正在安装…")
        sh("apt-get update -y", check=False)
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y cron", check=False)
    _ensure_cron_running()
    cron = sh("crontab -l 2>/dev/null", check=False) or ""
    if "acme.sh" not in cron:
        print("  ⚠ 没有 acme.sh 的续期任务，正在补上…")
        sh(f"{acme} --install-cronjob", check=False)
        if "acme.sh" in (sh("crontab -l 2>/dev/null", check=False) or ""):
            print("  ✓ 续期任务已装上（以后每天自动检查）")
        else:
            print("  ✗ 续期任务仍没装上，检查 cron 服务：systemctl status cron")
    # 续期方式必须跟【现在】的 80 端口状况对上。
    # 装机时没有 nginx，acme.sh 记下的就是 standalone（要独占 80）；可后来别的功能
    # （443 伪装站 / 自建 Emby / AdGuard）把 nginx 拉起来占了 80，standalone 从此永远
    # 验证失败——而且失败只写在 acme 自己的日志里，面板上一个字都看不到。
    # 这次翻车就是这么来的：cron 补上了也没用，续期照样跑不过。
    owner = _port80_owner()
    hooks = ""
    if owner:
        if "nginx" not in owner:
            print(f"  ✗ 80 端口被 {owner} 占着，acme 验证进不来。")
            print("    先停掉它再重试：ss -lntp | grep ':80'")
            return
        # 用 pre/post hook 让 acme 自己停一下 nginx，别去改用户的 nginx 配置——
        # 那份 conf 可能同时装着 443 伪装站/ws 反代/Emby，重写它比证书过期还糟。
        # hook 会被 acme.sh 记进这个域名的记录，以后【自动续期也照做】。
        hooks = (" --pre-hook 'systemctl stop nginx' "
                 "--post-hook 'systemctl start nginx'")
        print("  80 端口被 nginx 占着 → 续期时自动停一下 nginx（约 10 秒），完事自动起回来")
    # 抢占者必须在重签【之前】撤掉：留着它，这次重签出来的证书过几个小时就又被它盖回去，
    # 而且那时你已经不在看了。撤掉只是让 acme.sh 不再跟踪，磁盘上的证书文件一个不删。
    hij = acme_hijackers(keep=dom)
    if hij:
        R, N = "\033[1;31m", "\033[0m"
        print(f"{R}  ⚠ 发现 {len(hij)} 个旧域名也在往 {ACME_CRT} 装证书：{N}")
        for d, _c in hij:
            print(f"{R}      {d}{N}")
        print("    这是换域名时留下的记录，它每次自动续期都会把节点的证书覆盖掉。")
        print("    正在撤掉它们的续期跟踪（证书文件不删，只是不再自动续）…")
        for d, _c in hij:
            sh(f"{acme} --remove -d {d} --ecc", check=False)
            sh(f"{acme} --remove -d {d}", check=False)
        left = acme_hijackers(keep=dom)
        print(f"    {'✓ 已全部撤掉' if not left else R + '✗ 仍剩 ' + ', '.join(d for d, _ in left) + N}")
    print("  正在续期…（走 acme.sh，可能要十几秒）")
    # 用 --issue --force 而不是 --renew：--renew 会沿用记录里那套（可能已经失效的）
    # 验证方式，--issue 则把这次用的方式写回记录，往后自动续期就跟着走对的路。
    r = subprocess.run(f"{acme} --issue -d {dom} --standalone --keylength ec-256 "
                       f"--force{hooks}", shell=True, text=True, capture_output=True)
    if r.returncode:
        # 续期失败要把原文打出来——十次有九次是「80 端口被占」或「域名没解析到本机」，
        # 吞掉报错就只剩一句「修复失败」，等于没说
        print("  ✗ 续期失败，acme.sh 原文如下：")
        print("    " + ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", "\n    ")[-1200:])
        print("  常见原因：80 端口被占（standalone 验证要用）、域名没解析到本机、出网被墙。")
        return
    G["domain"] = dom
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{_ACME_RELOAD_HOOK}", check=False)
    for svc in ("nginx", "sing-box", "xray", "xy-sub"):
        sh(f"systemctl restart {svc}", check=False)
    left = _cert_secs_left()
    if left is not None and left > 0:
        print(f"  ✓ 完成，证书{_cert_left_text(left)}。")
        print("    reloadcmd 已记进 acme.sh，以后自动续期会跟着重启这几个服务，不用再手动来一次。")
    else:
        print("  ⚠ 续期后证书仍不正常。常见原因：域名解析没指向本机、80 端口被占（standalone 验证要用）。")
    print(CERT_DONE_MARK)

# ══════════════════════════════════════════════════════════════════════════════
# 转后台执行：凡是会重启 sing-box / xray 的操作，都得脱离 SSH 的控制终端跑
# ══════════════════════════════════════════════════════════════════════════════
# 你多半是【挂着本机代理在管这台机】的：手机上开着代理，SSH 也走同一条隧道。
# 核心一重启，隧道当场断，SSH 跟着断，前台这个 python 进程收到 SIGHUP 就死在半路。
# 表现最难看的是删除：入站已经从 config.json 里摘掉、服务也重启了，但摘分享链接、
# 刷订阅、写 state.json 这几步还没跑——下次进来一看「协议还在列表里，节点却没了」。
#
# start_new_session=True 起一个新会话（setsid），进程脱离控制终端，SIGHUP 打不到它，
# SSH 断了服务端照样跑完。前台只是【跟日志】，跟丢了无所谓，重连 tail 一下就看得到。

def _log_size(path):
    try:
        return os.path.getsize(path) if os.path.exists(path) else 0
    except OSError:
        return 0

def _self_script():
    """能拿来重新调起本脚本的真实文件路径；没有就返回 ''（调用方据此退回前台）。

       优先当前正在跑的这个文件（bgpeer 快捷命令 exec 的就是 SELF_LOCAL，天然最新），
       其次本地副本。`curl … | python3` 这种管道跑法没有真实文件，两个都落空——
       那是【首次安装】的场景，本机还没有代理，SSH 不会被自己掐断，前台跑正合适。"""
    try:
        p = os.path.abspath(__file__)
        if os.path.isfile(p):
            return p
    except NameError:
        pass
    return SELF_LOCAL if os.path.isfile(SELF_LOCAL) else ""

def _spawn_detached(subcmd, log):
    """把本脚本的某个子命令派到独立会话里跑，输出追加进 log。返回是否派出去了。"""
    script = _self_script()
    if not script:
        print("  本机没有脚本副本（管道直接运行），无法转后台。")
        return False
    try:
        subprocess.Popen(                         # -u：不缓冲，日志逐行落盘前台才跟得上
            f"python3 -u {script} {subcmd} >> {log} 2>&1",
            shell=True, start_new_session=True,
            # stdin 也要掐掉：后台没有终端，万一有哪一步想问点什么，
            # 得当场 EOF 走兜底，而不是挂在一个已经死掉的 SSH 上干等。
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print("  转后台失败:", e)
        return False

def _follow_log(log, start, mark, minutes=5):
    """从 start 处实时跟随日志，看到 mark 就收工。断了/等烦了都不影响后台。"""
    pos, deadline = start, time.time() + minutes * 60
    try:
        while time.time() < deadline:
            time.sleep(1)
            try:
                if os.path.getsize(log) <= pos:
                    continue
                with open(log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                continue
            text = chunk.decode("utf-8", "replace")
            print("  " + text.rstrip("\n").replace("\n", "\n  "))
            if mark in text:
                return True
        print(f"\n  等了 {minutes} 分钟还没跑完，后台仍在继续。稍后看日志: tail {log}")
    except KeyboardInterrupt:
        print(f"\n  已退出跟随，后台继续执行。稍后看日志: tail {log}")
    return False

def _node_op_dispatch(plan, fallback):
    """把交互里选好的方案落盘 → 派到后台跑 → 前台跟日志。

       fallback 是派不出去时的兜底：宁可断线，也不能把活儿做一半。"""
    os.makedirs(BGP_DIR, exist_ok=True)
    try:
        json.dump(plan, open(NODE_OP_PLAN, "w"), ensure_ascii=False, indent=2)
    except OSError as e:
        print("  方案写不进磁盘，改在前台直接执行:", e)
        fallback()
        return
    start = _log_size(NODE_OP_LOG)
    if not _spawn_detached("node-op", NODE_OP_LOG):
        print("  改在前台直接执行（要是断线了，重连进来看一眼结果）：")
        fallback()
        return
    print(f"\n  已转入后台执行（断开 SSH 也会在服务端跑完）。日志: {NODE_OP_LOG}")
    print("  ⚠ 重启核心会掐断代理链路，你挂着本机代理连的 SSH 多半就断在这一步——")
    print("    不用管，后台会把整件事做完。重连进来 tail 上面这个文件就能看到结果。\n")
    _follow_log(NODE_OP_LOG, start, NODE_OP_MARK)

def node_op_run():
    """CLI 子命令 node-op：读出落盘的方案，真正执行装/加/删。见 _node_op_dispatch。"""
    _TITLE = {"install": "全新安装", "add": "添加协议", "del": "删除协议",
              "cert-install": "安装证书", "cert-domain": "更换域名"}
    try:
        plan = json.load(open(NODE_OP_PLAN))
    except Exception as e:
        print("  读不出待执行的方案:", e)
        print(NODE_OP_MARK)
        return
    print("\n" + "=" * 60)
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S')}  {_TITLE.get(plan.get('op'), '?')}")
    print("=" * 60)
    G.update(plan.get("G") or {})
    try:
        if plan["op"] == "cert-install":
            cert_install_apply(plan["domain"], plan["wildcard"],
                               plan.get("cf_token", ""), plan.get("names"))
        elif plan["op"] == "cert-domain":
            cert_change_domain_apply(plan["domain"], plan.get("cf_token", ""),
                                     plan.get("emby_new", ""))
        elif plan["op"] == "install":
            run(plan["sb"], plan["xray"])
        else:
            st, have_sb, have_xr = _installed_state()
            if plan["op"] == "del":
                _del_apply(st, plan["del_sb"], plan["del_xr"], have_sb, have_xr)
            else:
                _add_apply(st, plan["pick_sb"], plan["pick_xr"], have_sb, have_xr)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("  ✗ 执行出错:", e)
    finally:
        try:
            os.remove(NODE_OP_PLAN)               # 别留着，免得下次误跑一遍旧方案
        except OSError:
            pass
        print(NODE_OP_MARK)

def cert_fix():
    """菜单入口：确认后把修复派到独立会话里跑，前台只负责跟日志。

       为什么非得转后台：修复的最后一步是 systemctl restart sing-box / xray，而你多半是
       【挂着本机代理在管这台机】的——核心一重启，代理链路当场断，SSH 跟着断，前台这个
       python 进程收到 SIGHUP 就死了。表现是「证书签下来了，但服务没重启完」「日志断在
       一半」，下次进来还得再来一遍。start_new_session=True 让它脱离控制终端，SIGHUP
       打不到它，SSH 断了照样在服务端跑完。跟菜单 19「更新核心」是同一套处理。"""
    dom = ""
    try:
        dom = json.load(open(STATE_FILE)).get("domain", "")
    except Exception:
        pass
    if not dom:
        print("\n  本机用的是自签证书（没有域名），不涉及 acme 续期。")
        return
    print(f"\n  域名 {dom}    磁盘上的证书：{_cert_left_text(_cert_secs_left())}")
    print("  会依次做：确认 cron 和 acme 续期任务都在 → 看 80 端口被谁占着、据此选验证方式")
    print("            → 强制重签 → 导出到 /etc/ssl/sb/ → 把 reloadcmd 记进 acme.sh")
    print("            → 重启 nginx / sing-box / xray / xy-sub")
    print("  ⚠ 重启核心会掐断代理链路。你要是挂着本机代理连的 SSH，这一步会断线——")
    print("    不用管，任务在后台跑完，重连进来看日志即可。")
    if (_ask("  继续? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return
    start = _log_size(CERT_FIX_LOG)
    if not _spawn_detached("cert-fix", CERT_FIX_LOG):
        print("  改在前台直接修。")                      # 兜底：宁可断线，也不能不修
        cert_fix_run()
        return
    print(f"\n  已转入后台执行（断开 SSH 也会在服务端跑完）。日志: {CERT_FIX_LOG}")
    print("  下面实时跟随进度，断了就断了，重连后 tail 这个文件即可：\n")
    _follow_log(CERT_FIX_LOG, start, CERT_DONE_MARK)

def main_menu():
    # 一次性自愈：xray 26.7.11+ 默认 minClientVer=26.3.27 会拒旧客户端(mihomo 硬编码 1.8.2 等)，
    # 给缺这项的 reality 入站补 1.0.0。只在首次(确有缺失时)改配置+重启 xray，之后即为 no-op。
    if _xray_heal_minclientver():
        print("  ⓘ 已给 xray reality 补 minClientVer=1.0.0：新版 xray(26.7.11+)默认会静默拒掉\n"
              "     mihomo/Clash 等上报旧版本的客户端，补上后它们又能连了（xray 已后台重启一次）。")
    while True:
        print("\n" + "=" * 60)
        print(f"  bgpeer 一键脚本 v{SCRIPT_VERSION}  （sing-box + xray 多协议 / 订阅）")
        print("=" * 60)
        t = traffic_line()
        if t:
            print(t)
            print("-" * 60)
        _left = _cert_secs_left()
        if _left is not None and _left <= 10 * 86400:
            # 证书一过期，节点和订阅会【一起】挂，而且报错只是「连不上」，极难联想到证书
            print(f"  \033[1;31m⚠ TLS 证书{_cert_left_text(_left)}"
                  f"——进 15 修复，否则节点和订阅会一起连不上\033[0m")
            print("-" * 60)
        print("  1. 节点安装（已装则可只加新协议 / 全部重装 / 删除协议）")
        print("  2. 节点链接 / 订阅")
        print("  3. 聚合节点链接（连机VPS合并多台VPS节点）")
        print("  4. 更换伪装域名（reality 借用的 SNI·带连通检测，不用重装）")
        print("  5. 多路复用开关 smux（只针对 ws / httpupgrade 协议）")
        print("  6. mihomo 配置")
        print("  7. sing-box 配置")
        print("  8. 小火箭配置")
        print("  9. CDN套用（利用CF中转，IP被墙时使用，延时比较高）")
        print("  10. 屏蔽中国域名和IP（可做白名单放行）")
        print("  11. BT/PT 下载屏蔽（防 VPS 被投诉封机）")
        print("  12. 网络优化（BBR/QoS 内核调优）")
        print("  13. 自建DNS（AdGuard Home·全设备去广告）")
        print("  14. GitHub中转（规则/图标走本机·默认开，可关）")
        print("  15. 证书管理（状态 / 安装 / 换域名 / 重签）")
        print("  16. 自建Emby（网盘直链媒体服务器·不影响节点）")
        print("  17. VPS线路检测（三网回程骨干 + IP纯净度）")
        print("  18. 更新脚本（不影响节点）")
        print("  19. 更新核心（sing-box / xray）")
        print("  20. 卸载")
        print("  0. 退出")
        print("-" * 60)
        print("  ▸ 退出后输入 \033[1;32mbgpeer\033[0m 可再次唤醒面板管理")
        c = _ask("请选择: ").strip()
        if c == "0" or c == "":
            print("再见。"); return
        if c == "1":     install_flow()
        elif c == "2":   show_links()
        elif c == "3":   peers_menu()
        elif c == "4":   change_sni_menu()
        elif c == "5":   smux_menu()
        elif c == "6":   config_menu("yaml")
        elif c == "7":   config_menu("json")
        elif c == "8":   config_menu("conf")
        elif c == "9":   cdn_menu()
        elif c == "10":  cn_block_menu()
        elif c == "11":  bt_menu()
        elif c == "12":  net_optimize_menu()
        elif c == "13":  adguard_menu()
        elif c == "14":  ghrelay_menu()
        elif c == "15":  cert_menu()
        elif c == "16":  media_stack_menu()
        elif c == "17":  vps_check_menu()
        elif c == "18":  update_script()
        elif c == "19":  update_cores()
        elif c == "20":  uninstall_all()
        elif c in ("t", "T"): traffic_setup()   # 流量套餐设置（顶部流量行按机房周期显示）
        else:
            print("无效选择。"); continue
        _ask("\n按回车返回主菜单...")            # 停一下，别让菜单立刻盖住上面的输出

# ============================================================================ 交互菜单
def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。

       两头都读不到就返回空串（= 回车）。转后台跑的那些活儿（见 _node_op_dispatch）
       没有控制终端、stdin 也被掐成 /dev/null，真有哪一步漏了个问题没在前台问掉，
       也该当场走默认值继续，而不是抛 EOFError 把整件事炸在半路。"""
    try:
        with open("/dev/tty", "r") as t:
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\n").strip()
    except (OSError, EOFError):
        pass
    try:
        return input(prompt).strip()
    except (OSError, EOFError):
        print(prompt + "(无终端，按默认继续)")
        return ""

def _ask_free(prompt):
    """要打中文/emoji 的自由文本输入（前缀之类）：提示语单独占一行，输入从下一行的 > 开始。

       为什么不像别处那样提示和输入挤同一行：终端的行编辑按【字符】发退格，而中文和
       emoji 占【两列】——删一个字只擦掉一列，光标会越删越往前跑，最后啃进提示语里，
       看着像把界面删坏了（其实输入缓冲区是好的，只是屏幕花了）。让输入独占一行，
       删过头也只啃到那个 '> '，提示语毁不掉。"""
    print(prompt)
    return _ask("  > ")

def _pick(title, options, default=None):
    """列出带编号的协议，返回选中的 key 列表。
       回车 = default（缺省=全选）；0/all 永远=全选；也可逗号分隔编号自选。"""
    print("\n" + title)
    for i, name in enumerate(options, 1):
        print(f"  {i:>2}. {name}")
    print("   0. 全部")
    if default is None:
        hint = "回车=全部"
    elif not default:
        hint = "回车=一个都不选，0/all=全部"      # 增量添加时某个核心可以整个跳过
    else:
        hint = "回车=" + "、".join(default) + "，0/all=全部"
    raw = _ask(f"选择(逗号分隔编号, {hint}): ")
    if raw == "":
        return list(default) if default is not None else list(options)
    if raw == "0" or raw.lower() == "all":
        return list(options)
    picked = []
    for tok in raw.replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= len(options):
            picked.append(options[int(tok) - 1])
        elif tok:
            print(f"  ⚠ 忽略无效项: {tok}")
    return picked

# ============================================================================ 增量添加协议
# 已经装好一套节点后，只想再加一个协议（比如新出的 xhttp-tls），不该把现有节点全部
# 重新生成——端口、UUID、密码、订阅 token 全会变，所有客户端都得重新导入一遍。
# 这里走「只添加」：现有 inbound 原样保留，只为新协议生成 inbound 和链接，追加进
# 节点文件后刷新订阅（不换 token），客户端拉一次订阅就多出新节点，老节点纹丝不动。

_CORE_META = {
    # 键: (服务名, 二进制, 配置路径, 协议表, 同名协议的小上标)
    "sb":   ("sing-box", SB_BIN,   f"{SB_DIR}/config.json",   SB,   "¹"),
    "xray": ("xray",     XRAY_BIN, f"{XRAY_DIR}/config.json", XRAY, "²"),
}

def _load_core_cfg(core):
    """读某核心现有 config.json → (cfg, inbounds)；没装或读不出返回 (None, [])。"""
    path = _CORE_META[core][2]
    try:
        cfg = json.load(open(path))
    except Exception:
        return None, []
    return cfg, (cfg.get("inbounds") or [])

def _cfg_port(ib):
    """取 inbound 的端口：sing-box 是 listen_port，xray 是 port。"""
    p = ib.get("listen_port", ib.get("port"))
    return p if isinstance(p, int) else None

def _ports_in_cfgs():
    """两核心现有配置里已占用的端口——增量装新协议时先占位，避免随机撞上。

       光靠 next_port() 里的 port_free() 探测不够：服务正在跑时那些端口确实绑着、探得出来，
       但服务要是恰好挂了或正在重启，探测就会认为端口空闲、把它分给新协议，
       等老服务起来两边抢同一个口。"""
    used = set()
    for core in _CORE_META:
        for ib in _load_core_cfg(core)[1]:
            p = _cfg_port(ib)
            if p:
                used.add(p)
    return used

HY2_HOP_WIDTH = 1000        # 「自动挑一段」时用的宽度

def _udp_ports_in_use(include_own=True):
    """当前被 UDP 占用的端口 → {端口: 谁在用}。

       只查 UDP：端口跳跃的 DNAT 是 `-p udp`（见 setup_port_hopping），
       TCP 服务落在跳跃段里【完全不受影响】，把它们也报出来纯属虚惊一场。

       include_own=False 用于全新安装——本脚本自己的节点马上要重建，
       现在还在监听的那些不算冲突，否则每次重装都会误报一堆。"""
    used = {}
    if include_own:
        for core in _CORE_META:
            label, _b, _p, table, _m = _CORE_META[core]
            for ib in _load_core_cfg(core)[1]:
                # 据注册表认 UDP 协议（按 tag 反查）：以后加 UDP 新协议只改表就行。
                # 改过名认不出来的漏网之鱼，下面的 ss -lnup 还会再兜一次底。
                if table.get(_ib_proto(ib), {}).get("udp"):
                    p = _cfg_port(ib)
                    if p:
                        used[p] = f"{label} 的 {ib.get('tag', '?')}"
    for line in (sh("ss -lnup 2>/dev/null", check=False) or "").splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        m = re.search(r":(\d+)$", f[4])
        if not m:
            continue
        who = re.search(r'users:\(\("([^"]+)"', line)
        name = who.group(1) if who else "未知程序"
        if not include_own and name in ("sing-box", "xray"):
            continue
        used.setdefault(int(m.group(1)), f"{name}（正在监听 UDP）")
    return used

def _hop_conflicts(rng, include_own=True):
    """跳跃段里有哪些 UDP 端口会被 DNAT 劫走 → [(端口, 谁)]，按端口排序。"""
    try:
        lo, hi = (int(x) for x in rng.split("-"))
    except (ValueError, AttributeError):
        return []
    return sorted((p, who) for p, who in _udp_ports_in_use(include_own).items()
                  if lo <= p <= hi)

def _auto_hop_range(include_own=True, width=HY2_HOP_WIDTH):
    """随机挑一段宽 width 的空闲 UDP 区间；实在挑不到返回 ''。
       在 20000-60000 里挑：低端留给系统服务，高端留出余量装得下整段。"""
    for _ in range(300):
        start = secrets.randbelow(60000 - width - 20000) + 20000
        rng = f"{start}-{start + width}"
        if not _hop_conflicts(rng, include_own):
            return rng
    return ""

def ask_hy2_range(cur="", include_own=True):
    """问 hy2 跳跃范围，撞车就当场拦下来。返回要写进 G['hy2_ports'] 的值。

       为什么值得专门做这一环：端口跳跃是把【整段 UDP】DNAT 给 hy2，段内任何别的
       UDP 服务都会被悄悄劫走——tuic、WireGuard、DNS 都算。它的表现是「那个服务
       忽然连不上」，两边日志干干净净，没人会往端口跳跃上想。装之前拦住，
       比事后排查便宜得多。"""
    RED, GRN, OFF = "\033[1;31m", "\033[1;32m", "\033[0m"
    while True:
        tip = f"回车沿用上次的 {cur}" if cur else f"回车=默认 {HY2_PORTS}"
        raw = (_ask(f"  hy2 端口跳跃范围 起-止（{tip}，输 n 不用跳跃）: ").strip() or cur)
        if raw.lower() in ("off", "n", "no", "none"):
            return "n"
        rng = raw if re.match(r"^\d+-\d+$", raw) else HY2_PORTS
        lo, hi = (int(x) for x in rng.split("-"))
        if lo >= hi or hi > 65535:
            print(f"{RED}  ✗ {rng} 不是合法区间（要 起<止 且 ≤65535），重填。{OFF}")
            cur = ""
            continue
        conflicts = _hop_conflicts(rng, include_own)
        if not conflicts:
            return rng
        print(f"{RED}  ✗ 跳跃段 {rng} 和下面这些 UDP 服务重叠。整段 UDP 会被 DNAT 劫给 hy2，"
              f"它们会悄无声息地连不上：{OFF}")
        for p, who in conflicts:
            print(f"{RED}      {p}   {who}{OFF}")
        print(f"  1. 自动挑一段空闲的（宽 {HY2_HOP_WIDTH}）")
        print("  2. 我自己重填")
        print("  0. 不用端口跳跃（hy2 走固定单端口）")
        c = _ask("  选择 [1/2/0]（回车=1）: ").strip() or "1"
        if c == "0":
            return "n"
        if c == "1":
            auto = _auto_hop_range(include_own)
            if auto:
                print(f"{GRN}  ✓ 已挑到空闲段 {auto}{OFF}")
                return auto
            print(f"{RED}  ✗ 20000-60000 里没找到连续 {HY2_HOP_WIDTH} 个都空闲的段，"
                  f"请自己指定或选 0 关掉跳跃。{OFF}")
        cur = ""          # 重填时不再默认沿用旧值，免得一回车又撞回原来那段

def _installed_state():
    """读上次安装记录 → (state dict, sb 协议名 list, xray 协议名 list)。
       顺带把历史旧协议名映射成现名，免得「已装」判断漏掉老节点。"""
    try:
        st = json.load(open(STATE_FILE))
    except Exception:
        return {}, [], []
    fix = lambda lst: [_PROTO_ALIASES.get(n, n) for n in (lst or [])]
    return st, fix(st.get("sb")), fix(st.get("xray"))

def _stale_protos(have_sb, have_xr):
    """安装记录里写着、但核心配置里根本没有的协议 → (sb 残留, xray 残留)。

       怎么来的：删到一半被打断（入站已摘、记录还没写就断了 SSH）、手工改过
       config.json、核心被外面卸过。必须单独认出来，否则这些协议会【卡死】——
       去删它「配置里没找到，跳过」，去加它「记录说已经装了，不在可添加列表里」，
       两条路都走不通，只能整机重装。

       配置文件读不出来（核心压根没装/文件坏了）时保守处理：不当残留。
       那种情况交给删除流程里 cfg is None 那一支去收拾记录。"""
    out = []
    for core, want in (("sb", have_sb), ("xray", have_xr)):
        cfg, ibs = _load_core_cfg(core)
        if cfg is None:
            out.append([])
            continue
        live = {_ib_proto(ib) for ib in ibs}
        out.append([n for n in want if n not in live])
    return out[0], out[1]

def _link_hits(u, sb_protos, xr_protos):
    """这条分享链接属不属于这些协议。

       两核心同名协议靠尾标区分（¹=sing-box ²=xray）；没有尾标说明当时只有一个核心
       装了它，核心位对不上也算命中。CDN 节点的协议段是 CDN·xxx，天然不会命中。"""
    pr, co = _link_core_proto(u)
    if not pr:
        return False
    targets = {("sb", p) for p in sb_protos} | {("xray", p) for p in xr_protos}
    return (co, pr) in targets if co else any(pr == q for _, q in targets)

def _restore_state_to_G(st):
    """把上次安装的参数灌回 G，保证新加的节点跟老节点同域名/同 SNI/同前缀。"""
    G["domain"]     = st.get("domain", "")
    G["sni"]        = st.get("sni") or G["sni"]
    G["prefix"]     = st.get("prefix", "")
    G["hy2_ports"]  = st.get("hy2_ports", "")
    G["nginx"]      = st.get("nginx", "")
    G["reality443"] = st.get("reality443", "")
    G["sni_split"]  = st.get("sni_split", "")
    G["smux"]       = st.get("smux", "")
    G["host"]       = st.get("host") or G["domain"] or public_ip()

def add_protocols_flow(st, have_sb, have_xr):
    """只添加新协议：现有节点一个都不动。"""
    # 记录里写着、配置里却没有的（上次删到一半被打断等），按【没装】算 —— 否则它既
    # 删不掉又加不回来，人就卡死了。见 _stale_protos。
    stale_sb, stale_xr = _stale_protos(have_sb, have_xr)
    avail_sb = [n for n in SB   if n not in have_sb or n in stale_sb]
    avail_xr = [n for n in XRAY if n not in have_xr or n in stale_xr]
    if not avail_sb and not avail_xr:
        print("\n  两个核心的协议都已经装齐了，没有可添加的。")
        return

    _restore_state_to_G(st)
    print("\n" + "=" * 60)
    print("  只添加新协议（现有节点的端口/UUID/密码/订阅地址全部不动）")
    print("=" * 60)
    print(f"  沿用上次安装的参数：域名 {G['domain'] or '(无，自签+IP)'}   "
          f"SNI {G['sni']}   前缀 {G['prefix'] or '(无)'}")
    if stale_sb or stale_xr:
        print(f"  ⓘ {', '.join(dict.fromkeys(stale_sb + stale_xr))} "
              f"安装记录里有、核心配置里却没有它的入站，已按【没装】列进可添加。")

    pick_sb, pick_xr = [], []
    if avail_sb:
        pick_sb = _pick("【sing-box 可添加的协议】", avail_sb, default=[])
    if avail_xr:
        pick_xr = _pick("【xray 可添加的协议】", avail_xr, default=[])
    if not pick_sb and not pick_xr:
        print("  没选任何协议，返回。")
        return

    # ws 家族藏在 nginx 443 后面时，新加一个就得把 nginx 的 location 表整个重写；
    # 而旧节点的 path 只存在于运行中的配置里，重建容易出错（sni-split 还多一层 stream）。
    # 与其冒险改坏正在用的 443，不如在这里挡掉，让用户走全部重装那条路。
    if _nginx_front():
        blocked = proto_pick("ws_front", pick_sb)
        if blocked:
            print(f"\n  ⚠ 跳过 {', '.join(blocked)}：这些协议现在藏在 nginx 443 后面，"
                  f"新增要改动 nginx 反代表，增量模式不碰它。要加请选『全部重新安装』。")
            pick_sb = [n for n in pick_sb if n not in blocked]
    if not pick_sb and not pick_xr:
        print("  没有可增量添加的协议，返回。")
        return

    # 首装时是【按选中的协议】决定问不问那几项的：没装 hy2 就没问过跳跃范围，
    # 没装 reality 也可能没认真挑过借用目标。这次新选了它们，就得补问一遍——
    # 不问等于替用户默默做主，hy2 尤其糟：不问会直接按默认段去配 iptables DNAT。
    # 问哪几项完全由注册表里的 asks 决定，以后加协议不用回来改这里。
    run_asks("add", pick_sb, pick_xr)

    # 新 reality 一律走随机端口：443 已经有主的话抢不得；没有主的话绑 443 还要动
    # nginx/证书布局，那是重装该干的事。
    if any(n in REALITY_443_PRIORITY for n in pick_sb + pick_xr) and not st.get("reality443"):
        print("\n  提示：新加的 reality 走随机端口。想让它独占 443（抗封锁）要走全部重装。")

    print("\n" + "-" * 60)
    if pick_sb:
        print("  新增 sing-box:", ", ".join(pick_sb))
    if pick_xr:
        print("  新增 xray:    ", ", ".join(pick_xr))
    for label, value in recap_lines(pick_sb, pick_xr):
        print("  " + _pad(label + ":", 14) + str(value))
    print("  现有节点:    ", f"sing-box {len(have_sb)} 个 / xray {len(have_xr)} 个（保持不动）")
    print("  订阅地址:    ", "不变，三格式配置会自动重新生成")
    print("               ", "（如果有聚合节点请先到主机更新下配置再到客户端刷新一下即可）")
    print("-" * 60)
    if (_ask("确认添加? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("已取消。")
        return

    # 跟删除同理：建完入站要 systemctl restart 核心，代理一断 SSH 跟着断，
    # 前台进程被 SIGHUP 打死的话，节点建出来了但链接没进订阅文件。转后台跑。
    _node_op_dispatch({"op": "add", "pick_sb": pick_sb, "pick_xr": pick_xr, "G": dict(G)},
                      lambda: _add_apply(st, pick_sb, pick_xr, have_sb, have_xr))

def _add_apply(st, pick_sb, pick_xr, have_sb, have_xr):
    """真正执行增量添加（非交互）。由后台那半程调用，见 _node_op_dispatch / node_op_run。"""
    # 先记下这次要装的里面哪些是【残留】（记录里有、配置里没有）——只有它们在订阅里
    # 留着一条早就连不上的旧链接，待会儿要摘掉。必须在建新入站【之前】算，建完就分不出来了。
    _ssb, _sxr = _stale_protos(have_sb, have_xr)
    redo_sb = [n for n in pick_sb if n in _ssb]
    redo_xr = [n for n in pick_xr if n in _sxr]
    ensure_deps()
    _USED_PORTS.clear()
    _USED_PORTS.update(_ports_in_cfgs())        # 避开现有节点的端口
    NGINX_WS.clear()
    NGINX_STREAM.clear()
    # 同名协议（两核心都有）加小上标区分：跟已装的一起算，免得新旧重名
    dup = (set(have_sb) | set(pick_sb)) & (set(have_xr) | set(pick_xr))

    # 同删除：一个核心失败【不能】把另一个核心已经装好的节点丢在半路——那样节点在
    # 服务器上跑着、订阅里却没有，客户端永远看不到它，而且毫无报错。失败只 break，
    # 下面照样把已经装好的那部分写进链接、刷订阅、记进 state。
    new_links, failed = [], []
    for core, picks in (("sb", pick_sb), ("xray", pick_xr)):
        if not picks:
            continue
        name, binpath, path, table, mark = _CORE_META[core]
        cfg, ibs = _load_core_cfg(core)
        if cfg is None:
            # 这个核心之前没装过：装上它，按全新流程建配置骨架
            (install_singbox if core == "sb" else install_xray)()
            cfg = ({"log": {"level": "info"}, "inbounds": [],
                    "outbounds": [{"type": "direct"}]} if core == "sb" else
                   {"log": {"loglevel": "warning"}, "inbounds": [],
                    "outbounds": [{"protocol": "freedom", "tag": "direct"},
                                  {"protocol": "blackhole", "tag": "block"}]})
            ibs = cfg["inbounds"]
        ins, lks = build(table, picks, dup=dup, mark=mark)
        exist_tags = {ib.get("tag") for ib in ibs}
        add_ins, add_lks = [], []
        for ib, lk in zip(ins, lks):            # tag 撞了就跳过，别把老节点顶掉
            if ib.get("tag") in exist_tags:
                print(f"  ⚠ {name}: 节点名 {ib.get('tag')} 与现有重名，跳过。")
                continue
            add_ins.append(ib)
            add_lks.append(lk)
        if not add_ins:
            continue
        cfg["inbounds"] = ibs + add_ins
        backup = path + ".bak"
        try:
            shutil.copyfile(path, backup)
        except OSError:
            backup = ""
        json.dump(cfg, open(path, "w"), indent=2)
        ok, msg = core_check(binpath, path)
        if not ok:                              # 校验不过就原样退回，绝不拿坏配置去重启
            if backup:
                shutil.copyfile(backup, path)
                os.remove(backup)
            print(f"\n  ✗ {name} 配置校验失败，已回滚，{name} 下面选的协议一个没加：\n{msg}")
            failed.append(name)
            break
        try:
            sh(f"systemctl restart {name}")
        except Exception as e:
            if backup:
                shutil.copyfile(backup, path)
                os.remove(backup)
                sh(f"systemctl restart {name}", check=False)
            print(f"\n  ✗ {name} 重启失败，已回滚，{name} 下面选的协议一个没加：{e}")
            failed.append(name)
            break
        if backup:
            os.remove(backup)
        new_links += add_lks
        if core == "sb":
            # 去重：残留协议本来就还挂在记录里（配置里没有而已），直接相加会写进去两遍
            have_sb = list(dict.fromkeys(have_sb + picks))
        else:
            have_xr = list(dict.fromkeys(have_xr + picks))

    if not new_links:
        print("  没有新增任何节点。")
        return

    # 追加链接 + 刷新订阅（不换 token：客户端里那条订阅地址继续有效）
    links, tail = _node_file_parts()
    # 摘掉残留协议那条早就连不上的旧链接。【只认残留的】：老链接没有 ¹² 尾标时分不出
    # 属于哪个核心，按协议名一刀切会误伤——sb 早装着 reality-vision、这次往 xray 加同名
    # 协议，sb 那条好端端的链接就会被当成旧链接摘掉，节点凭空少一个。
    stale_links = [u for u in links if _link_hits(u, redo_sb, redo_xr)]
    if stale_links:
        print(f"  顺手摘掉 {len(stale_links)} 条同协议的旧链接（残留的死节点）")
        links = [u for u in links if u not in stale_links]
    links += [l for l in new_links if l not in links]
    with open(NODE_FILE, "w") as f:
        f.write("\n".join(links) + ("\n" if links else ""))
        if tail:
            f.write(tail if tail.startswith("\n") else "\n" + tail)
    try:
        build_subscription(read_saved_links(), new_token=False)
    except Exception as e:
        print("  ⚠ 订阅刷新失败（节点已装好，可到配置菜单点『更新配置』重试）:", e)

    st.update({"sb": have_sb, "xray": have_xr, "sni": G["sni"],
               "hy2_ports": G.get("hy2_ports", ""), "smux": G.get("smux", "")})
    try:
        json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f"  已添加 {len(new_links)} 个节点（现有节点未做任何改动）")
    if failed:
        print(f"\033[1;31m  ⚠ {'、'.join(failed)} 那边失败了、已回滚，它下面选的协议一个没加。"
              f"上面这些已经装好并写进订阅了，修好后再单独加那几个即可。\033[0m")
    print("=" * 60)
    print("\n".join(new_links))
    print("\n订阅地址没变，客户端重拉一次订阅即可看到新节点"
          "（有聚合节点的话，先到主机点一次『更新配置』）：")
    print(sub_urls_text())

# ============================================================================ 删除协议
# 删比加难：加只是往配置里塞一条，删要把【散落各处的副作用】一起收回来——
# 端口跳跃的 iptables DNAT、nginx 前置的 location 反代、分享链接、订阅、状态记录。
# 漏掉任何一样都不会报错，只会留下一个安静的坑：
#   · DNAT 不删 → 整段 UDP 继续转给一个已经不存在的端口，连带劫走以后装在那段里的服务
#   · nginx location 不删 → 443 上那条 path 反代到死端口，客户端拿到 502
#   · 链接不删 → 订阅里留着连不上的死节点，客户端每次都去测它

def _link_core_proto(u):
    """分享链接 → (协议名, 归属核心)。两核心同名协议靠尾标区分：¹=sing-box、²=xray；
       没有尾标说明只有一个核心装了它，核心位返回 ''。认不出返回 ('','')。"""
    _, seg = _split_tag(_link_name(u))
    if not seg:
        return "", ""
    core = "sb" if seg.endswith("¹") else ("xray" if seg.endswith("²") else "")
    return seg.rstrip(_TAG_MARKS), core

def _ib_proto(ib):
    """inbound 的 tag → 协议名（去掉前缀和尾标）。"""
    _, seg = _split_tag(ib.get("tag", ""))
    return (seg or "").rstrip(_TAG_MARKS)

def _rebuild_nginx_ws_from_cfg():
    """从现有 sing-box 配置还原 nginx 前置的 ws 反代表。
       前置模式下 ws 家族监听 127.0.0.1 且带 transport.path，据此能完整重建。"""
    NGINX_WS.clear()
    for ib in _load_core_cfg("sb")[1]:
        if ib.get("listen") != "127.0.0.1":
            continue
        path, port = (ib.get("transport") or {}).get("path"), _cfg_port(ib)
        if path and port:
            NGINX_WS.append({"path": path, "port": port})
    return list(NGINX_WS)

def _nginx_backed(names):
    """这些 sb 协议里，哪些的入站是挂在 nginx 后面的（监听 127.0.0.1）。

       从运行中的配置读，不靠名字猜：ws 家族前置、sni-split 的 reality 后端都是
       这个形态，以后再多一种也自动认得。"""
    local = {_ib_proto(ib) for ib in _load_core_cfg("sb")[1] if ib.get("listen") == "127.0.0.1"}
    return [n for n in names if n in local]

def _drop_hy2_dnat():
    """删掉端口跳跃的 DNAT 规则，返回删了几条。"""
    n = 0
    for ipt in ("iptables", "ip6tables"):
        if not have(ipt):
            continue
        for line in (sh(f"{ipt} -t nat -S PREROUTING", check=False) or "").splitlines():
            if line.startswith("-A") and "portHopping" in line:
                sh(f"{ipt} -t nat " + line.replace("-A", "-D", 1), check=False)
                n += 1
    if n:
        sh("netfilter-persistent save", check=False)
    return n

def del_protocols_flow(st, have_sb, have_xr):
    """删除已装协议：只动选中的那几个，其余节点的端口/UUID/订阅地址全不变。"""
    RED, OFF = "\033[1;31m", "\033[0m"
    if not have_sb and not have_xr:
        print("\n  还没装任何协议。")
        return
    _restore_state_to_G(st)
    print("\n" + "=" * 60)
    print("  删除协议")
    print("=" * 60)
    stale_sb, stale_xr = _stale_protos(have_sb, have_xr)
    _mark = lambda lst, stale: ", ".join(n + ("（残留）" if n in stale else "") for n in lst)
    if have_sb:
        print("  已装 sing-box:", _mark(have_sb, stale_sb))
    if have_xr:
        print("  已装 xray:    ", _mark(have_xr, stale_xr))
    if stale_sb or stale_xr:
        print("  ⓘ 标『残留』的：安装记录里有，核心配置里却没有它的入站。\n"
              "     删它会把订阅和记录里剩下的部分清掉；也可以直接从『1 只添加新协议』"
              "把它装回来。")
    print("-" * 60)
    print("  1. 选择删除")
    print("  2. 全部删除")
    print("  0. 返回")
    c = (_ask("选择 [1/2/0]（回车=0 返回）: ") or "0").strip()
    if c == "0":
        return
    if c == "2":
        del_sb, del_xr = list(have_sb), list(have_xr)
    elif c == "1":
        del_sb = _pick("【sing-box 删哪些】", have_sb, default=[]) if have_sb else []
        del_xr = _pick("【xray 删哪些】", have_xr, default=[]) if have_xr else []
    else:
        print("  无效选择，返回。")
        return
    if not del_sb and not del_xr:
        print("  没选任何协议，返回。")
        return

    # sni-split 下 nginx 是 stream 分流 + 本地 https server 两层，443 的走向依赖具体后端；
    # 删了它的后端再去重建这套结构，出错就是整个 443 崩掉。挡住，让走重装。
    if G.get("sni_split"):
        backing = _nginx_backed(del_sb)
        if backing:
            print(f"{RED}  ✗ 本机开着 nginx SNI 分流，443 的走向依赖 "
                  f"{', '.join(backing)} 做后端。{OFF}")
            print("    删它们要重建整套 nginx 结构，增量模式不碰。请走『全部重新安装』。")
            return

    # 要清哪些副作用，全问注册表要（teardown）。加新协议时只要在表里填上 teardown，
    # 这里的确认框和下面的真动手都自动带上它，不会像以前那样漏掉一处。
    teardowns = _proto_fns("teardown", del_sb, del_xr)
    left_sb = [n for n in have_sb if n not in del_sb]
    left_xr = [n for n in have_xr if n not in del_xr]

    print("\n" + "-" * 60)
    if del_sb:
        print("  删除 sing-box:", ", ".join(del_sb))
    if del_xr:
        print("  删除 xray:    ", ", ".join(del_xr))
    print("  删除后剩下:   ", f"sing-box {len(left_sb)} 个 / xray {len(left_xr)} 个")
    for td in teardowns:
        what = td(plan=True)
        if what:
            print("  连带清理:     ", what)
    if not left_sb:
        print(f"{RED}  ⚠ sing-box 将没有任何入站，服务会被停掉并禁用开机自启{OFF}")
    if not left_xr and have_xr:
        print(f"{RED}  ⚠ xray 将没有任何入站，服务会被停掉并禁用开机自启{OFF}")
    # 三格式订阅删完会自动重生成、订阅地址也不换，客户端刷一下就行，不用红字吓人。
    # 分组里万一手动选中过被删的节点，客户端自己会退回该组第一项（模板里第一项就是
    # 自动/随机组），也不用人工干预。真正回不来的只有旧的那条分享链接。
    #
    # 聚合是要提一句的：成员机的节点是【主机渲染订阅时现拉】的(见 aggregated_links)，
    # 所以在成员机上删完，主机那份订阅里那个节点还在，要等主机再渲染一次才消失。
    # 而这台机器无从知道自己有没有被别人当成员聚合，只能无条件提醒一句。
    print("  订阅地址:      不变，三格式配置会自动重新生成")
    print("                 （如果有聚合节点请先到主机更新下配置再到客户端刷新一下即可）")
    print("  协议本身:      随时能从『1 只添加新协议』再装回来")
    print("                 （但会是全新端口/UUID 的新节点，旧的单条分享链接作废）")
    print("-" * 60)
    if (_ask("确认删除? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。")
        return

    # 真动手的那半程转后台：最后一步是 systemctl restart/disable 核心，代理链路一断
    # SSH 跟着断，前台这个进程会被 SIGHUP 打死在半路——入站删了、订阅还没刷，最难收拾。
    _node_op_dispatch({"op": "del", "del_sb": del_sb, "del_xr": del_xr, "G": dict(G)},
                      lambda: _del_apply(st, del_sb, del_xr, have_sb, have_xr))

def _del_apply(st, del_sb, del_xr, have_sb, have_xr):
    """真正执行删除（非交互）。由后台那半程调用，见 _node_op_dispatch / node_op_run。"""
    RED, OFF = "\033[1;31m", "\033[0m"
    left_sb = [n for n in have_sb if n not in del_sb]    # 循环里只用来判断「这个核心删空了没」
    left_xr = [n for n in have_xr if n not in del_xr]
    # 两个核心是分别处理的，其中一个失败【不能】把另一个已经做完的事丢在半路：
    # 那样入站删了、订阅和记录却没跟着改，正是最难收拾的「删了一半」。失败只 break
    # 出循环，下面的收尾照跑，但一律按【真正处理掉的】那部分算，不按用户当初勾的清单。
    removed, stale = [], []          # removed: 真摘掉的入站；stale: 配置里本来就没有的
    done = {"sb": [], "xray": []}    # 每个核心真正处理掉的协议
    failed = []
    for core, dels, left in (("sb", del_sb, left_sb), ("xray", del_xr, left_xr)):
        if not dels:
            continue
        name, binpath, path, _tbl, _mk = _CORE_META[core]
        cfg, ibs = _load_core_cfg(core)
        if cfg is None:
            print(f"  {name}: 读不到配置文件（核心没装或文件坏了），只清记录和订阅。")
            stale += dels
            done[core] += dels
            continue
        keep = [ib for ib in ibs if _ib_proto(ib) not in dels]
        gone = [ib for ib in ibs if _ib_proto(ib) in dels]
        if not gone:
            # 配置里本来就没有 ≠ 没事可做：记录和订阅里多半还留着它（上次删到一半
            # 被打断就是这样）。这里【不能 return】，否则那个协议永远删不掉也加不回来。
            print(f"  {name}: 该协议的入站配置里已经没有了，"
                  f"这次清理订阅和安装记录里剩下的部分。")
            stale += dels
            done[core] += dels
            continue
        cfg["inbounds"] = keep
        backup = path + ".bak"
        try:
            shutil.copyfile(path, backup)
        except OSError:
            backup = ""
        json.dump(cfg, open(path, "w"), indent=2)
        ok, msg = core_check(binpath, path)
        if not ok:                                   # 删出来的配置都过不了校验 → 原样退回
            if backup:
                shutil.copyfile(backup, path)
                os.remove(backup)
            print(f"\n  ✗ {name} 配置校验失败，已回滚，{name} 下面选的协议一个没删：\n{msg}")
            failed.append(name)
            break
        try:
            if keep:
                sh(f"systemctl restart {name}")
            else:
                # 没有入站的核心留着只会空转（甚至起不来反复重启），停掉更干净；
                # 以后再装协议时 write_service 会重新 enable，不用手动恢复。
                sh(f"systemctl disable --now {name}", check=False)
                print(f"  {name} 已无入站 → 服务已停止并禁用开机自启")
        except Exception as e:
            if backup:
                shutil.copyfile(backup, path)
                os.remove(backup)
                sh(f"systemctl restart {name}", check=False)
            print(f"\n  ✗ {name} 重启失败，已回滚，{name} 下面选的协议一个没删：{e}")
            failed.append(name)
            break
        if backup:
            os.remove(backup)
        done[core] += dels
        removed += [ib.get("tag", "?") for ib in gone]

    # 往下一律用【真正处理掉的】那部分：清哪些副作用、摘哪些链接、记录里剩下什么，
    # 都必须跟服务器上的实际情况对齐，不能拿用户当初勾选的清单去算。
    ok_sb, ok_xr = done["sb"], done["xray"]
    teardowns = _proto_fns("teardown", ok_sb, ok_xr)
    left_sb = [n for n in have_sb if n not in ok_sb]
    left_xr = [n for n in have_xr if n not in ok_xr]
    if not removed and not stale:
        print("  没有删掉任何入站。")
        return

    # 入站已经删掉了，副作用清理再失败也不该把整个删除判为失败——逐个 try，
    # 失败只红字报出来，让用户知道具体哪一样要手工收尾。
    for td in teardowns:
        try:
            done = td()
            if done:
                print("  " + done)
        except Exception as e:
            print(f"{RED}  ⚠ 善后清理失败（{getattr(td, '__name__', td)}），"
                  f"可能有残留，需手工确认：{e}{OFF}")

    # 摘掉对应的分享链接（CDN 节点的协议段是 CDN·xxx，天然不会命中，不受影响）
    links, tail = _node_file_parts()
    kept = [u for u in links if not _link_hits(u, ok_sb, ok_xr)]
    dropped = len(links) - len(kept)
    with open(NODE_FILE, "w") as f:
        f.write("\n".join(kept) + ("\n" if kept else ""))
        if tail:
            f.write(tail if tail.startswith("\n") else "\n" + tail)
    try:
        if read_saved_links():
            build_subscription(read_saved_links(), new_token=False)
        else:
            print("  已无任何节点，订阅内容为空（订阅服务和地址保留）。")
    except Exception as e:
        print("  ⚠ 订阅刷新失败（节点已删，可到配置菜单点『更新配置』重试）:", e)

    st.update({"sb": left_sb, "xray": left_xr})
    try:
        json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f"  已删除 {len(removed)} 个节点，摘掉 {dropped} 条分享链接")
    if stale:
        print(f"  （其中 {', '.join(dict.fromkeys(stale))} 的入站配置里本来就没有，"
              f"这次清掉的是订阅和记录）")
    if failed:
        print(f"{RED}  ⚠ {'、'.join(failed)} 那边失败了、已回滚，它下面选的协议一个没删。"
              f"其余部分已按上面的结果收尾完毕，修好后再来一次即可。{OFF}")
    print("=" * 60)
    for t in removed:
        print("   -", t)
    if left_sb or left_xr:
        print("\n  剩下的节点端口/UUID/订阅地址全部没动，客户端重拉一次订阅即可。")
        print("  有聚合节点的话，记得再到主机点一次『更新配置』，主机那份订阅才会同步。")
        print("  想把删掉的协议装回来：主菜单 1 →『1 只添加新协议』，会生成全新节点。")
    else:
        print("\n  本机已无代理节点。想重新装回来：主菜单 1 → 按向导走一遍。")
        print("  （脚本本体、订阅服务、证书、CDN 节点都还在，没有卸载任何东西）")

def install_flow():
    # 已装过：先把装了什么摆出来，再让用户选「只加新的」还是「全部重来」。
    # 分这两条路是因为代价天差地别——全部重装会把每个节点的端口/UUID/密码和订阅
    # token 全部重新生成，所有客户端都得重新导入一遍；只加新协议则一个字节都不动老节点。
    st, have_sb, have_xr = _installed_state()
    if (have_sb or have_xr) and read_saved_links():
        n_sb, n_xr = len(SB) - len(have_sb), len(XRAY) - len(have_xr)
        print("\n" + "=" * 60)
        print("  检测到本机已安装 bgpeer 节点")
        print("=" * 60)
        if have_sb:
            print("  已装 sing-box:", ", ".join(have_sb))
        if have_xr:
            print("  已装 xray:    ", ", ".join(have_xr))
        print(f"  还没装的:      sing-box {n_sb} 个 / xray {n_xr} 个")
        print("-" * 60)
        print("  1. 只添加新协议   老节点的端口/UUID/密码/订阅地址全部不变，推荐")
        print("  2. 全部重新安装   所有节点重新生成，订阅地址也会换，客户端要重新导入")
        print("  3. 删除协议       只删选中的，其余节点不动（含清理 DNAT / nginx 反代）")
        print("  0. 返回")
        # 回车默认 0：这几条都会动正在跑的节点，不该靠误按回车触发
        ans = (_ask("选择 [1/2/3/0] (回车=0 返回): ") or "0").strip()
        if ans == "0":
            print("已取消，返回主菜单。"); return
        if ans == "3":
            del_protocols_flow(st, have_sb, have_xr); return
        if ans == "1":
            add_protocols_flow(st, have_sb, have_xr); return
        if ans != "2":
            print("无效选择，返回主菜单。"); return
        if (_ask("  全部重新安装会让现有客户端全部失效，确认? y 确认 / 回车取消: ")
                or "n").strip().lower() not in ("y", "yes"):
            print("已取消，返回主菜单。"); return
        G["regen"] = "1"
    print("=" * 60)
    print("  sing-box + xray 交互安装")
    print("=" * 60)
    print("选择核心:  1. sing-box   2. xray   3. 两个都装")
    core = _ask("输入 [1/2/3] (回车=1): ") or "1"

    sb_names, xr_names = [], []
    if core in ("1", "3"):
        sb_names = _pick("【sing-box 协议】", list(SB))
    if core in ("2", "3"):
        # 两个都装时，xray 默认只装它独有的 reality-xhttp（其余协议 sing-box 已有，避免重复）；
        # 只装 xray(core=2) 时回车仍全装。想全装 xray 就输 0/all 或点编号。
        xr_default = ["reality-xhttp"] if core == "3" else None
        xr_names = _pick("【xray 协议】", list(XRAY), default=xr_default)
    if not sb_names and not xr_names:
        print("没选任何协议，退出。"); return

    # 证书可能是先在『15 证书管理』里装好的（那边不需要先有节点）。这里认出来，
    # 把域名直接摆上、回车即用，免得手打错一个字符就变成重新申请一张。
    _ci = cert_info()
    if _ci["exists"] and _ci["domain"]:
        print(f"\n  \033[1;32m✓ 检测到已安装证书\033[0m：{_ci['domain']}"
              + ("（泛域名）" if _ci["wildcard"] else "")
              + f"   {_ci['issuer']}   {_cert_left_text(_ci['secs'])}")
        print("    节点直接用它，不会重新申请。回车即用这个域名；想换别的就直接输。")
        domain = _ask(f"域名（回车=用 {_ci['domain']}，输 n = 不用域名走自签）: ").strip()
        domain = _ci["domain"] if not domain else ("" if domain.lower() == "n" else domain)
    else:
        domain = _ask("\n域名(有则走 acme 真证书, 回车=自签): ")
    email = ""   # 证书自动续期、默认占位邮箱即可签发，不再交互问；想指定用命令行 --email
    nginx = ""
    if domain:
        nginx = "1" if (_ask("用 nginx 前置(443伪装站+webroot证书, ws类藏443)? [y/N]: ")
                        .lower() in ("y", "yes")) else ""
    _sni_rand = secrets.choice(REALITY_SNI_POOL)         # 回车就用这个随机挑的（不同机器各不同，不扎堆）
    sni = _ask(f"reality 借用目标站 SNI (回车=随机挑，本次随机到 {_sni_rand}): ") or _sni_rand
    prefix = _ask_free("节点名称前缀（如 🇺🇸/🇯🇵/家宽，回车=无前缀）：")
    # 各协议自己的设置（hy2 跳跃范围、ws 的 smux …）交给注册表的 asks 去问：
    # 它们写进 G，所以得先把已经问到的填进 G，再 run_asks，最后读回来。
    G["domain"], G["email"], G["sni"], G["prefix"] = domain, email, sni, prefix
    G["nginx"], G["hy2_ports"], G["smux"] = nginx, "", ""
    run_asks("install", sb_names, xr_names)
    hy2p, smux = G["hy2_ports"], G["smux"]
    # 抗 GFW 封端口，两档（都让 reality 上 443）：
    #  sni-split（最强，需域名+reality-vision）：nginx SNI 分流，reality+网站/ws 全在 443；
    #  reality-443 直连（次之）：主力 reality 独占 443，nginx 仅留 :80 续期。
    r443 = ""; split = ""
    if domain and SNI_SPLIT_BACKEND in sb_names:
        ans = _ask("用 nginx SNI 分流把 reality+网站全放到 443?(最强抗封锁, 会装 stream 模块) [Y/n]: ")
        split = "" if ans.lower() in ("n", "no") else "1"
    if not split and pick_reality_443(sb_names, xr_names)[0]:
        ans = _ask("把主力 reality 绑到 443 抗封锁?(推荐；会关闭 nginx 前置) [Y/n]: ")
        r443 = "" if ans.lower() in ("n", "no") else "1"
    G["reality443"], G["sni_split"] = r443, split

    reality443_proto = pick_reality_443(sb_names, xr_names)[0] if r443 else ""
    print("\n" + "-" * 60)
    if sb_names: print("  sing-box:", ", ".join(sb_names))
    if xr_names: print("  xray:    ", ", ".join(xr_names))
    print("  证书:    ", f"acme真证书({domain})" if domain else "自签")
    print("  节点地址:", domain if domain else "公网IP")
    if split:
        print("  443方案: ", "SNI分流（reality+网站/ws 全在 443，nginx stream 分流；最强抗封锁）")
    elif reality443_proto:
        print("  443方案: ", f"reality直绑443（{reality443_proto}；nginx 仅 :80 续期）")
    else:
        print("  nginx前置:", "是（443伪装站+webroot，ws类走443）" if nginx else "否")
    print("  名称前缀:", prefix or "(无)")
    print("  SNI:     ", sni)
    for label, value in recap_lines(sb_names, xr_names):
        if label == "借用 SNI":                 # 上面那行已经报过了，别重复
            continue
        print("  " + _pad(label + ":", 10) + str(value))
    print("-" * 60)
    if (_ask("确认开始? [Y/n]: ") or "y").lower() in ("n", "no"):
        print("已取消。"); return
    if not takeover_confirm():        # 唯一还会发问的一步，必须在前台问完
        return
    # 装到最后同样要起/重启核心；全部重装更是会把正在用的节点整个换掉，
    # 挂着本机代理连的 SSH 一定断在那一步。转后台，断线也能装完。
    _node_op_dispatch({"op": "install", "sb": sb_names, "xray": xr_names, "G": dict(G)},
                      lambda: run(sb_names, xr_names))

# ============================================================================ CLI
if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:          # 不带参数 → 管理面板（bgpeer 也走这里）
        main_menu()
        sys.exit(0)
    if sys.argv[1] == "update-cores":   # 非交互：cron 每月自动更新、菜单19 转后台都调这个
        update_cores_auto(sys.argv[2] if len(sys.argv) > 2 else None)   # 可选 sing-box/xray/both
        sys.exit(0)
    if sys.argv[1] == "cert-fix":        # 菜单 15 转后台调这个（脱离 SSH，重启核心也断不掉）
        cert_fix_run()
        sys.exit(0)
    if sys.argv[1] == "node-op":         # 装/加/删协议转后台调这个（同上）
        node_op_run()
        sys.exit(0)
    if sys.argv[1] == "selfdns-toggle":  # adguard 菜单调用：开关"自建DNS写入订阅"
        selfdns_toggle()
        sys.exit(0)
    if sys.argv[1] == "selfdns-off":     # adguard 卸载调用：若已写入则从订阅移除并刷新
        selfdns_off()
        sys.exit(0)
    ap = argparse.ArgumentParser(
        description="sing-box + xray 双核心多协议安装器",
        epilog=("示例:\n"
                "  全装(自签,无域名):  sudo python3 %(prog)s --sb all --xray all\n"
                "  指定协议:           --sb reality-vision,hy2,tuic --xray reality-xhttp\n"
                "  带域名走真证书:     --sb all --xray all --domain a.com --email me@a.com\n"
                f"  sing-box 可选: {','.join(SB)}\n"
                f"  xray 可选:     {','.join(XRAY)}"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sb", default="", help="sing-box 协议，逗号分隔，或 all")
    ap.add_argument("--xray", default="", help="xray 协议，逗号分隔，或 all")
    ap.add_argument("--domain", default="", help="有域名则走 acme 真证书")
    ap.add_argument("--email", default="", help="acme 注册邮箱")
    ap.add_argument("--sni", default="", help="reality 借用的目标站（不填=从内置大厂池随机挑一个）")
    ap.add_argument("--prefix", default="", help="节点名称前缀(如 🇺🇸/🇯🇵)，默认无")
    ap.add_argument("--hy2-ports", default="", help="hy2 端口跳跃范围 起-止，默认 30000-31000；填 off 关闭跳跃走单端口")
    ap.add_argument("--nginx", action="store_true",
                    help="用 nginx 前置(443伪装站+webroot证书, ws类藏443)，需域名")
    ap.add_argument("--no-reality-443", action="store_true",
                    help="不把主力 reality 绑到 443（默认会绑，抗 GFW 封端口；会关闭 nginx 前置）")
    ap.add_argument("--sni-split", action="store_true",
                    help="最强抗封锁：nginx stream+ssl_preread 按 SNI 分流，reality+网站/ws 全在 443（需域名+reality-vision）")
    ap.add_argument("--smux", action="store_true",
                    help="ws 类开启 smux 多路复用（网页/小请求更快，大文件下载可能变慢；默认关）")
    ap.add_argument("--yes", action="store_true",
                    help="检测到别人装的节点(mack-a 等)直接卸载接管，不再询问")
    a = ap.parse_args()

    G["domain"], G["email"], G["sni"], G["prefix"], G["hy2_ports"], G["nginx"], G["force"] = \
        a.domain, a.email, (a.sni or secrets.choice(REALITY_SNI_POOL)), a.prefix, a.hy2_ports, ("1" if a.nginx else ""), a.yes
    G["reality443"] = "" if a.no_reality_443 else "1"   # 默认把 reality 绑 443（抗封端口）
    G["sni_split"] = "1" if a.sni_split else ""         # 最强：nginx SNI 分流，全上 443
    G["smux"] = "1" if a.smux else ""                   # ws 类多路复用，默认关
    sb = list(SB) if a.sb == "all" else [x for x in a.sb.split(",") if x]
    xr = list(XRAY) if a.xray == "all" else [x for x in a.xray.split(",") if x]
    sb = [_PROTO_ALIASES.get(x, x) for x in sb]      # 老写法 vless-reality-* 仍然认
    xr = [_PROTO_ALIASES.get(x, x) for x in xr]
    if not sb and not xr:
        ap.error("至少用 --sb 或 --xray 指定要装的协议")
    # 协议名校验：拼错的名字必须在这里挡下——run() 会先卸载别人的安装(takeover)再 build，
    # 放到 build() 里撞 KeyError 就成了「先把机器上的节点卸了、再崩 traceback」。
    bad_sb = [n for n in sb if n not in SB]
    bad_xr = [n for n in xr if n not in XRAY]
    if bad_sb or bad_xr:
        if bad_sb: ap.error(f"未知的 sing-box 协议: {','.join(bad_sb)}\n  可选: {','.join(SB)}")
        ap.error(f"未知的 xray 协议: {','.join(bad_xr)}\n  可选: {','.join(XRAY)}")
    run(sb, xr)

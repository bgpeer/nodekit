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
import os, json, base64, secrets, uuid, argparse, subprocess, urllib.request, urllib.parse, urllib.error, shutil, socket, re, time, random, ipaddress

# 脚本自身版本号：合并进 main 后 CI 会自动把补丁位 +1 并发布 GitHub Release；
# 想升大/中版本（如 2.0.0）就手动改这里再合并，CI 会直接用你写的这个号发布。
SCRIPT_VERSION = "1.0.65"

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

# 网络优化脚本已并入本仓库（net-optimize.py，BBR/QoS 等内核调优，依赖工具自动安装）；
# 主脚本只负责拉取+调用，状态检测走同一脚本的 --check。
NETOPT_LOCAL = BGP_DIR + "/net-optimize.py"      # 本地缓存的网络优化脚本
NETOPT_URL   = _RAW + "net-optimize.py"
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
    need = [pkg for pkg, binary in
            (("curl", "curl"), ("socat", "socat"), ("unzip", "unzip"),
             ("openssl", "openssl"), ("tar", "tar"), ("ca-certificates", None))
            if binary is not None and not have(binary)]
    # ca-certificates 无对应可执行文件，装 acme/真证书时保证 TLS 根证书齐全
    if not have("update-ca-certificates"):
        need.append("ca-certificates")
    if not need:
        return
    print("安装依赖:", ", ".join(need))
    sh("apt-get update -y", check=False)
    sh("DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(need))

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
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode()
    except Exception:
        return sh("hostname -I").split()[0]

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
    """现有证书是否就是给这个域名签的（换域名重装时避免复用旧域名的证书）。"""
    if not domain or not os.path.exists(path):
        return False
    return domain in sh(f"openssl x509 -in {path} -noout -text 2>/dev/null", check=False)

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
        # nginx 模式走 webroot（复用 nginx 的 80，不用腾端口）；否则 standalone
        if G.get("nginx"):
            issue = f"{acme} --issue -d {G['domain']} --webroot {WEBROOT} --keylength ec-256"
        else:
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
        # reloadcmd 会被 acme.sh 记住，续期时自动执行。sing-box/xray 是启动时把证书读进
        # 内存的、不会自动重载证书文件，所以续期后必须重启它们，否则磁盘证书更新了、进程还用
        # 旧证书，约 90 天后客户端撞上过期证书。有 nginx 顺带 reload；服务不存在则静默跳过。
        reload_hook = (" --reloadcmd '"
                       "systemctl reload nginx 2>/dev/null; "
                       "systemctl restart sing-box 2>/dev/null; "
                       "systemctl restart xray 2>/dev/null; "
                       "systemctl restart xy-sub 2>/dev/null; true'")   # 订阅 HTTPS 证书同步刷新
        sh(f"{acme} --install-cert -d {G['domain']} --ecc "
           f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
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

def precheck_sni(sb_names, xr_names):
    """选了 reality 类协议时，装前探测借用的 SNI 目标是否合格；不合格只警告不阻断。"""
    reality_sel = (any(n in ("reality-vision", "reality-grpc") for n in sb_names)
                   or any(n.startswith("vless-reality") for n in xr_names))
    if not reality_sel:
        return
    ok, detail = _reality_sni_ok(G["sni"])
    if ok:
        print(f"  reality 借用目标 {G['sni']}: {detail}")
    else:
        Y, N = "\033[1;33m", "\033[0m"                # 黄色警告（不阻断）
        print(f"{Y}  ⚠ reality 借用目标 {G['sni']} 可能不理想：{detail}\n"
              f"    建议换成支持 TLS1.3+h2 的大站：{SNI_SUGGESTIONS}\n"
              f"    （可 --sni 指定或在交互菜单里改；现按你填的继续装）{N}")

def warn_selfsigned(sb_names, xr_names):
    """无域名时，依赖证书的 TLS 协议只能自签+insecure，是伪装/加密弱点。
       给出明确引导：优先 reality，或补一个域名走真证书。hy2/tuic 自签是常规，不在此列。"""
    if G["domain"]:
        return
    cert_tls = ([n for n in sb_names if n in
                 ("vless-vision", "trojan", "anytls", "vless-ws", "vmess-ws", "vmess-httpupgrade")]
                + [n for n in xr_names if n in ("vless-ws", "vmess-ws", "trojan")])
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

def latest_gh_release(repo, fallback):
    """取 GitHub 最新正式版 tag（去掉前导 v）。取不到就用 fallback。
       和 mack-a 一样跟随 latest —— 否则钉死旧版会缺协议（如 anytls 需 1.12）。"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "xy-installer", "Accept": "application/vnd.github+json"})
        tag = json.loads(urllib.request.urlopen(req, timeout=15).read())["tag_name"]
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
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases?per_page=20",
            headers={"User-Agent": "xy-installer", "Accept": "application/vnd.github+json"})
        rels = json.loads(urllib.request.urlopen(req, timeout=15).read())
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
    sh(f"curl -Lo /tmp/sb.tgz {url} && tar -xzf /tmp/sb.tgz -C /tmp")
    sh(f"install -m755 /tmp/sing-box-{ver}-linux-{a}/sing-box {SB_BIN}")
    os.makedirs(SB_DIR, exist_ok=True)

def install_xray():
    ver = newest_gh_release("XTLS/Xray-core", XRAY_VER)   # 含预发行取最大：XTLS 把每个 release 都标预发行
    if os.path.exists(XRAY_BIN) and ver in sh(f"{XRAY_BIN} version", check=False):
        return
    a = arch_tag()
    zmap = {"amd64": "64", "arm64": "arm64-v8a"}
    url = (f"https://github.com/XTLS/Xray-core/releases/download/"
           f"v{ver}/Xray-linux-{zmap[a]}.zip")
    sh(f"curl -Lo /tmp/xray.zip {url} && unzip -o /tmp/xray.zip -d /tmp/xray")
    sh(f"install -m755 /tmp/xray/xray {XRAY_BIN}")
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
SB = {"vless-vision": sb_vless_vision,
      "vless-ws": make_sb_vless("ws"),
      "vmess-ws": make_sb_vmess("ws"),
      "trojan": sb_trojan,
      "hy2": sb_hysteria2,
      "reality-vision": sb_reality_vision,
      "reality-grpc": sb_reality_grpc,
      "tuic": sb_tuic,
      "vmess-httpupgrade": make_sb_vmess("httpupgrade"),
      "anytls": sb_anytls}
# 备用（以后想加回，取消注释挪进上面的 SB）：
#   "ss2022": sb_ss2022,
#   "vless-h2": make_sb_vless("h2"),
#   "vless-httpupgrade": make_sb_vless("httpupgrade"),
#   "vmess-h2": make_sb_vmess("h2"),
#   "socks5": sb_socks5,
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

def xr_trojan(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "trojan", "tag": tag,
          "settings": {"clients": [{"password": pw}]},
          "streamSettings": {"network": "raw", "security": "tls",
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = (f"trojan://{pw}@{G['host']}:{port}?security=tls&sni={tls_host()}"
          f"&type=tcp&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

XRAY = {"vless-reality-vision": xr_reality_vision,
        "vless-reality-grpc": xr_reality_grpc,
        "vless-reality-xhttp": xr_reality_xhttp,
        "vless-ws": xr_vless_ws, "vmess-ws": xr_vmess_ws,
        "trojan": xr_trojan}
# 已移除 ss2022：纯全加密无伪装，易被 GFW 全加密流量探测识别；有 reality 完全无需它。

# ============================================================================ 组装
# reality 绑 443 的优先级：优先 sing-box reality-vision（Vision flow 最稳），依次往下。
# 只能有一个 reality 上 443（443/TCP 独占），其余 reality 留在随机端口。
REALITY_443_PRIORITY = ["reality-vision", "reality-grpc",
                        "vless-reality-vision", "vless-reality-xhttp", "vless-reality-grpc"]

def pick_reality_443(sb_names, xr_names):
    """选出要绑到 443 的那个 reality 协议名；没有 reality 被选则返回 ''。"""
    selected = set(sb_names) | set(xr_names)
    for n in REALITY_443_PRIORITY:
        if n in selected:
            return n
    return ""

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
        ib, lk = table[n](port, tag)
        inbounds.append(ib); links.append(lk)
    return inbounds, links

# ============================================================================ 订阅
def _yfmt(v):
    if isinstance(v, dict): return "{" + ", ".join(f"{k}: {_yfmt(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list): return "[" + ", ".join(_yfmt(x) for x in v) + "]"
    return str(v)

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
_WS_FAMILY = {"vless-ws", "vmess-ws", "vmess-httpupgrade"}   # 可开 smux 的 sing-box 节点键
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
        # 名称直接用链接里的 #备注（已含用户前缀+协议）；不再硬编码国旗
        return '"' + (uq(P.fragment) if P.fragment else default) + '"'
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
        name = '"' + j.get("ps", "vmess") + '"'
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
    """raw.githubusercontent 常被限流(429)，补上 jsDelivr 镜像作兜底。"""
    urls = [url]
    m = re.match(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)", url)
    if m:
        o, repo, br, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
        urls.append(f"https://fastly.jsdelivr.net/gh/{o}/{repo}@{br}/{path}")
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
    tag = d.get("name", "").strip('"') or key            # 统一用节点池名称（含服务器端前缀）
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
# 三格式共用同一套检测；各格式生成器按自己语法在 __XY_GROUPS__ / __XY_GROUP_NAMES__ 锚点渲染。
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
    """对象级替换锚点：__XY_NODES__ 换节点对象、__XY_GROUPS__ 换国家组、
       __XY_GROUP_NAMES__ 展开国家组名、__PATTERN__:正则 展开命中节点名，再按手写风格序列化。"""
    cfg = json.loads(_ghrelay_rewrite(fetch_url(tpl_url)))    # 规则/图标链接：开启则改走本机 GitHub 中转
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
    def expand_list(lst):
        out = []
        for x in lst:
            if x == "__XY_NAMES__":
                out += country_names                                 # 裸锚点 → 只国家组名
            elif isinstance(x, str) and x.startswith("__XY_NAMES__:"):
                out += country_names                                 # 带:正则 → 国家组名 + 命中节点名
                out += [t for t in tags if re.search(x[len("__XY_NAMES__:"):], t)]
            elif isinstance(x, str) and x.startswith("__PATTERN__:"):
                sel = [t for t in tags if re.search(x[len("__PATTERN__:"):], t)]
                out += sel or ["DIRECT"]                             # 旧锚点(向后兼容)：只命中的节点名
            else:
                out.append(x)
        return out
    new_ob = []
    for x in cfg.get("outbounds", []):
        if x == "__XY_NODES__":
            new_ob += objs                                           # 节点锚点 → 节点对象
        elif x == "__XY_GROUPS__":
            new_ob += country_objs                                   # 分组锚点 → 国家 urltest 组
        elif isinstance(x, dict) and isinstance(x.get("outbounds"), list):
            x["outbounds"] = expand_list(x["outbounds"]); new_ob.append(x)
        else:
            new_ob.append(x)
    cfg["outbounds"] = new_ob
    _sb_direct_ip(cfg, _direct_targets(nodes))                    # 各 VPS IP 直连（走紧凑序列化，不破坏格式）
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
       注意带 __XY_NAMES__ 的行会被跳过——那种行是模板自带的组、名字里不含国家组名，
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

def build_shadowrocket_sub(nodes, tpl_url):
    lines, names_list = [], []
    for key, d in nodes:
        name = d.get("name", "").strip('"') or key       # 统一用节点池名称（含服务器端前缀）
        try:
            s = shadowrocket_line(name, d)
            if s: lines.append(s); names_list.append(name)
        except Exception:
            pass
    if not lines:
        return
    tpl = _ghrelay_rewrite(fetch_url(tpl_url))               # 规则/图标链接：开启则改走本机 GitHub 中转
    # 国家检测/成员池 = 注入节点 + 用户手写进模板 [Proxy] 段的静态节点（"名 = 协议,..." 行）
    static = _sr_static_names(tpl)
    # 模板 [Proxy Group] 段里已有的组名：同名的国家组不再生成，避免同段两条同名定义
    groups_txt, names_frag = _sr_country_groups(names_list + static, _sr_group_names(tpl))
    out = tpl
    out = _fill_block(out, "__XY_NODES__", "\n".join(lines))    # 块锚点整行替换，缩进容错
    out = _fill_block(out, "__XY_GROUPS__", groups_txt)
    out = out.replace("__XY_NAMES__", names_frag)               # 行内锚点
    open(SR_FILE, "w").write(out)

# --- 三格式元数据：文件 / 作者模板 / 生成器；自定义模板存 CUSTPL_FILE ---
def _node_names(nodes):
    """从解析后的节点取名字列表（去引号），供国家检测用。"""
    return [d.get("name", "").strip('"') or k for k, d in nodes]

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
       这样锚点顶格或缩进都行——避免用户给 __XY_NODES__/__XY_GROUPS__ 缩两格导致 YAML 缩进错乱。"""
    return re.sub(r"(?m)^[ \t]*" + re.escape(anchor) + r"[ \t]*$", lambda m: block, tpl)

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

def gen_mihomo(ylines, nodes, tpl_url):
    tpl = _ghrelay_rewrite(fetch_url(tpl_url))               # 规则/图标链接：开启则改走本机 GitHub 中转
    # 国家检测要看"全部节点"：注入的订阅节点 + 用户手写进模板的静态节点。
    # 静态节点名取 proxy-groups 段之前的 name:（策略组名在 proxy-groups 里，且不含国旗，不会误检）。
    static = re.findall(r'name:\s*"([^"]*)"', tpl.split("proxy-groups:")[0])
    groups_yaml, names_frag = _mihomo_country(_node_names(nodes) + static,
                                              tpl_group_names(tpl))   # 模板里已手写的同名组不再自动生成
    # 块锚点(独占一行)整行替换，缩进容错：__XY_NODES__ 建节点 / __XY_GROUPS__ 建国家组
    tpl = _fill_block(tpl, "__XY_NODES__", "\n".join(ylines))
    tpl = _fill_block(tpl, "__XY_GROUPS__", groups_yaml)
    tpl = tpl.replace("__XY_NAMES__", names_frag)          # 行内锚点：引用组名，原样替换
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
    """探测成员链接可达性，返回 HTTP 状态码字符串；不通返回 '000'。供菜单显示 ✓/红码。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "xy-installer"})
        return str(urllib.request.urlopen(req, timeout=8).status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception:
        return "000"

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
    for proto in sorted(set(SB) | set(XRAY), key=len, reverse=True):   # 长的先试，别让 trojan 抢了 vless-ws
        if body.endswith(proto):
            pfx = body[:-len(proto)]
            if not pfx or pfx.endswith("·"):
                return nm                           # 没前缀 / 已经有分隔点
            return pfx + "·" + proto + mark
    return nm                                       # 认不出协议名（自定义名字）→ 不动

def _names_need_sep(links):
    """返回 [(旧名, 新名)]，只含确实要改的。"""
    out = []
    for u in links:
        old = _link_name(u)
        new = _sep_name(old)
        if old and old != new:
            out.append((old, new))
    return out

def add_name_sep():
    """一键把老节点名补上分隔点（前缀直接连着协议名的那些）。"""
    pending = _names_need_sep(read_saved_links())
    if not pending:
        print("\n  节点名都已经是「前缀·协议」的形式了，无需处理。")
        return
    print("\n" + "=" * 60)
    print("  节点改名：给前缀和协议名之间补分隔点")
    print("=" * 60)
    print("  这些节点名的前缀和协议名连在一起，面板上读不出来；文字前缀(USA/HK 等)还会")
    print("  让国家分组的正则匹配不上、分组建不出来：")
    for old, new in pending:
        print(f"    {old}  →  {new}")
    print("-" * 60)
    print("  只改显示名：uuid / 端口 / 路径 / 服务 / 证书一律不动，不用重装。")
    print("  ⚠ 客户端里手动选中过的节点会因为改名回到分组默认；")
    print("     自定义模板里若写死了旧节点名，需要同步改。")
    if (_ask("  确认改名? y 确认 / 回车取消: ") or "n").strip().lower() not in ("y", "yes"):
        print("  已取消。"); return
    links, tail = _node_file_parts()
    renamed = [_link_rename(u, _sep_name(_link_name(u))) for u in links]
    with open(NODE_FILE, "w") as f:
        f.write("\n".join(renamed) + ("\n" if renamed else ""))
        if tail:
            f.write(tail if tail.startswith("\n") else "\n" + tail)
    nodes = _cdn_load()                             # cdn.json 的 tag 同步改，否则下次生成链接又变回旧名
    if nodes:
        for n in nodes:
            n["tag"] = _sep_name(n.get("tag", ""))
        _cdn_save(nodes)
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)
    except Exception as e:
        print("  ⚠ 订阅刷新失败（名字已改，可到配置菜单点『更新配置』重试）:", e); return
    print(f"  ✓ 已改 {len(pending)} 个节点名并刷新订阅，客户端重拉一次即生效。")
    print("    多机聚合的话，记得再去主机点一次『更新配置』重新汇总。")

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
       只认真正的节点分享链接前缀，绝不把订阅 URL/注释误当节点。撞名的自动加 ¹²³ 区分。"""
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

def takeover_cleanup():
    """检测到别人装的节点就卸掉、由本脚本接管。破坏性操作，需确认（--yes 免交互）。"""
    units = detect_existing()
    dirs  = [p for p in ("/etc/v2ray-agent",) if os.path.isdir(p)]   # mack-a 目录
    if not units and not dirs:
        return
    print("\n检测到本机已有『别人搭建』的代理安装：")
    for u, path in units:
        print(f"  - 服务 {u}.service  →  {path}")
    for p in dirs:
        print(f"  - 目录 {p}（疑似 mack-a / v2ray-agent）")
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
        elif "reality-vision" not in sb_names:
            print("  sni-split 需选 sing-box reality-vision（放到 443 后面），已忽略。")
            G["sni_split"] = ""
        elif not sni_split_preflight():
            G["sni_split"] = ""; G["reality443"] = "1"    # 退回 reality-443 直连
        else:
            G["reality443"] = ""                          # sni-split 下 reality 走本地口，不直绑 443

    # reality 绑 443（直连模式，与 sni-split 互斥）：把主力 reality 协议钉在 443，
    # 主动探测回落到借用的真站，消掉「reality 在非 443 易被 GFW 封 IP」的风险。
    pin = {}
    r443 = pick_reality_443(sb_names, xr_names) if G.get("reality443") else ""
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
        ins, lks = build(SB, sb_names, pin, dup=dup_protos, mark="¹"); all_links += lks
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
        ins, lks = build(XRAY, xr_names, pin, dup=dup_protos, mark="²"); all_links += lks
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
                   "sni_split": G.get("sni_split", ""),
                   "sb": sb_names, "xray": xr_names},
                  open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass

    install_shortcut()
    sched = setup_core_update_cron()                     # 内核每月自动更新（北京每月2号04:00）
    if sched:
        print(f'内核已设为每月自动更新一次（{_core_update_schedule_str()}）；也可随时进菜单 16 手动立即更新。')
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
        for l in open("/root/xy-nodes.txt"):
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
    print("\n".join(links))
    urls = sub_urls_text()
    if urls:
        print("=" * 60 + "\n订阅链接（按客户端选一条）:\n" + urls)
    if _names_need_sep(links):                      # 老装的节点名前缀和协议连在一起 → 就地给个修法
        print("=" * 60)
        print("  ⓘ 检测到节点名的前缀和协议名连在一起（如 🇺🇸2anytls）。文字前缀(USA/HK 等)")
        print("    还会让「美国随机」这类国家分组匹配不上、建不出来。可一键补分隔点——")
        print("    只改显示名，uuid/端口/服务都不动，不用重装。")
        if (_ask("  现在改? y 确认 / 回车跳过: ") or "n").strip().lower() in ("y", "yes"):
            add_name_sep()

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
                code = peer_status(u)
                mark = "\033[1;32m✓\033[0m" if code == "200" else \
                       ("\033[1;31m不通\033[0m" if code == "000" else f"\033[1;31m{code}\033[0m")
                print(f"    {i}. {u}   {mark}")
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
            code = peer_status(u)
            print("  ✓ 已添加。" + ("连通 ✓" if code == "200" else f"（当前不通 {code}，之后通了会自动纳入）"))
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

def update_cores_auto(only=None):
    """非交互更新已安装的内核到最新并重启。起不来会记进日志。
       两个入口共用：cron 每月自动更新，以及菜单16 转到后台时。
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
            tmp = local + ".new"
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, local)
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
        print("  0 返回")
        c = _ask("选择: ").strip()
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
       只写 mihomo / 小火箭（列表型 DNS，把自建 DoH 放最前当主用、原有留兜底，没通自动回落）；
       sing-box 的 DNS 无列表回落机制，强改易断解析，故不写入。adguard 菜单调用（selfdns-toggle）。"""
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
        print(f"\n  ✓ {act}自建 DNS，订阅已刷新（写入 mihomo / 小火箭；sing-box 未动，避免断解析）。")
        if act == "已写入":
            print(f"  写入的 DoH：{_selfdns_doh()}")
            print("  ⚠ 确保 AdGuard 已开加密、防火墙放行 DoH 端口；没通也不影响——会自动回落到原 DNS。")
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

def restart_services(*names):
    """后台异步重启核心：--no-block 交给 systemd 执行，本进程不阻塞、立即返回。
       这样即便你挂着本机代理来管理、重启会掐断 SSH，操作也已在服务端完成
       （所有配置/状态必须在调用本函数之前就落盘）。"""
    svc = " ".join(n for n in names if n)
    if svc:
        sh(f"systemctl restart --no-block {svc}", check=False)

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
            print("  本机没有 reality 类节点（vless-reality-*），没有伪装域名可换。")
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
    """从 vnstat 日表累加账单周期用量（字节）。mode: sum=双向相加 / max=单向取大。"""
    import datetime
    sel = f"-i {iface} " if iface else ""
    j = json.loads(sh(f"vnstat {sel}--json d 62", check=False))
    rx = tx = 0
    for e in j["interfaces"][0]["traffic"]["day"]:
        dt = datetime.date(e["date"]["year"], e["date"]["month"], e["date"]["day"])
        if dt >= start:
            rx += e["rx"]; tx += e["tx"]
    return (rx + tx) if mode == "sum" else max(rx, tx)

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
    m = _ask(f"  计费方式 1 双向相加 / 2 单向取大（回车={'2' if cfg.get('mode') == 'max' else '1'}）: ").strip()
    mode = "max" if m == "2" or (not m and cfg.get("mode") == "max") else "sum"
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
            tag = "双向" if cfg.get("mode", "sum") == "sum" else "单向"
            quota = cfg.get("quota_gb")
            if quota:
                left = max(quota * 1024 ** 3 - used, 0)
                return (f"  📊 本周期已用: {_fmt_traffic(used)} / {quota:g} GB"
                        f"（剩 {_fmt_traffic(left)}，每月 {cfg['reset_day']} 号重置，{tag}计）")
            return (f"  📊 本周期已用: {_fmt_traffic(used)}"
                    f"（每月 {cfg['reset_day']} 号重置，{tag}计，{iface}）")
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
        prefix = _ask(f"  节点名称前缀（回车=沿用安装前缀「{ipfx}」，或输入自定义）: ").strip() or ipfx
    else:
        prefix = _ask("  节点名称前缀（回车=默认 CDN，自定义如 🇯🇵/家宽）: ").strip()

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
        print("  1. 安装（已装则问是否重装节点，y 重装 / n 返回）")
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
        print("  15. 自建Emby（网盘直链媒体服务器·不影响节点）")
        print("  16. 更新脚本（不影响节点）")
        print("  17. 更新核心（sing-box / xray）")
        print("  18. 卸载")
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
        elif c == "15":  media_stack_menu()
        elif c == "16":  update_script()
        elif c == "17":  update_cores()
        elif c == "18":  uninstall_all()
        elif c in ("t", "T"): traffic_setup()   # 流量套餐设置（顶部流量行按机房周期显示）
        else:
            print("无效选择。"); continue
        _ask("\n按回车返回主菜单...")            # 停一下，别让菜单立刻盖住上面的输出

# ============================================================================ 交互菜单
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

def _pick(title, options, default=None):
    """列出带编号的协议，返回选中的 key 列表。
       回车 = default（缺省=全选）；0/all 永远=全选；也可逗号分隔编号自选。"""
    print("\n" + title)
    for i, name in enumerate(options, 1):
        print(f"  {i:>2}. {name}")
    print("   0. 全部")
    if default is None:
        hint = "回车=全部"
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

def install_flow():
    # 已装过就问是否重装节点；不重装就直接返回（更新配置在各配置菜单里做，这里不掺和）
    if os.path.exists(STATE_FILE) and read_saved_links():
        ans = _ask("检测到已安装 bgpeer 节点。重新安装节点? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消，返回主菜单。（更新配置请进对应配置菜单）"); return
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
        # 两个都装时，xray 默认只装它独有的 vless-reality-xhttp（其余协议 sing-box 已有，避免重复）；
        # 只装 xray(core=2) 时回车仍全装。想全装 xray 就输 0/all 或点编号。
        xr_default = ["vless-reality-xhttp"] if core == "3" else None
        xr_names = _pick("【xray 协议】", list(XRAY), default=xr_default)
    if not sb_names and not xr_names:
        print("没选任何协议，退出。"); return

    domain = _ask("\n域名(有则走 acme 真证书, 回车=自签): ")
    email = ""   # 证书自动续期、默认占位邮箱即可签发，不再交互问；想指定用命令行 --email
    nginx = ""
    if domain:
        nginx = "1" if (_ask("用 nginx 前置(443伪装站+webroot证书, ws类藏443)? [y/N]: ")
                        .lower() in ("y", "yes")) else ""
    _sni_rand = secrets.choice(REALITY_SNI_POOL)         # 回车就用这个随机挑的（不同机器各不同，不扎堆）
    sni = _ask(f"reality 借用目标站 SNI (回车=随机挑，本次随机到 {_sni_rand}): ") or _sni_rand
    prefix = _ask("节点名称前缀(如 🇺🇸/🇯🇵/家宽，回车=无前缀): ")
    hy2p = ""
    if "hy2" in sb_names:
        hy2p = _ask("hy2 端口跳跃范围 起-止(回车=30000-31000，自定义直接输数字，输 n 不用端口跳跃): ")
    smux = ""
    if _WS_FAMILY & set(sb_names):     # 只有选了 ws/httpupgrade 节点才问
        ans = _ask("ws 类开启 smux 多路复用?(网页/小请求更快，大文件下载可能变慢) y开启/n不开(回车=不开): ")
        smux = "1" if ans.lower() in ("y", "yes") else ""
    # 抗 GFW 封端口，两档（都让 reality 上 443）：
    #  sni-split（最强，需域名+reality-vision）：nginx SNI 分流，reality+网站/ws 全在 443；
    #  reality-443 直连（次之）：主力 reality 独占 443，nginx 仅留 :80 续期。
    r443 = ""; split = ""
    if domain and "reality-vision" in sb_names:
        ans = _ask("用 nginx SNI 分流把 reality+网站全放到 443?(最强抗封锁, 会装 stream 模块) [Y/n]: ")
        split = "" if ans.lower() in ("n", "no") else "1"
    if not split and pick_reality_443(sb_names, xr_names):
        ans = _ask("把主力 reality 绑到 443 抗封锁?(推荐；会关闭 nginx 前置) [Y/n]: ")
        r443 = "" if ans.lower() in ("n", "no") else "1"
    G["domain"], G["email"], G["sni"], G["prefix"], G["hy2_ports"] = domain, email, sni, prefix, hy2p
    G["nginx"], G["reality443"], G["sni_split"], G["smux"] = nginx, r443, split, smux

    reality443_proto = pick_reality_443(sb_names, xr_names) if r443 else ""
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
    if _WS_FAMILY & set(sb_names):
        print("  ws多路复用:", "开启 smux" if smux else "不开(默认)")
    print("  SNI:     ", sni)
    if "hy2" in sb_names:
        print("  hy2跳跃: ", hy2_range() or "关闭（固定单端口）")
    print("-" * 60)
    if (_ask("确认开始? [Y/n]: ") or "y").lower() in ("n", "no"):
        print("已取消。"); return
    run(sb_names, xr_names)

# ============================================================================ CLI
if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:          # 不带参数 → 管理面板（bgpeer 也走这里）
        main_menu()
        sys.exit(0)
    if sys.argv[1] == "update-cores":   # 非交互：cron 每月自动更新、菜单16 转后台都调这个
        update_cores_auto(sys.argv[2] if len(sys.argv) > 2 else None)   # 可选 sing-box/xray/both
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
                "  指定协议:           --sb reality-vision,hy2,tuic --xray vless-reality-xhttp\n"
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

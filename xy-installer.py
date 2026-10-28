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
import os, json, base64, calendar, secrets, uuid, argparse, subprocess, unicodedata, urllib.request, urllib.parse, urllib.error, shutil, socket, re, time, random, ipaddress, ssl, struct

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

# 脚本自身版本号：合并进 main 后 CI 会自动把补丁位 +1 并发布 GitHub Release；
# 想升大/中版本（如 2.0.0）就手动改这里再合并，CI 会直接用你写的这个号发布。
SCRIPT_VERSION = "1.1.152"

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
G = {"host": "", "domain": "", "email": "", "sni": "www.cisco.com", "prefix": ""}
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
SR_NODES_FILE = BGP_DIR + "/shadowrocket-nodes.txt"  # 小火箭节点订阅（base64 分享链接；跟小火箭配置同一个 token、扩展名 .txt）
SR_MODE_FILE  = BGP_DIR + "/sr_mode"                 # "sub" = 小火箭节点走节点订阅、配置里不带节点；没有 = 写在配置里（一个链接）
SR_ONELINK_MARK = BGP_DIR + "/sr_onelink"            # 订阅模式的老机器已一次性改回一个链接（见 sr_onelink_heal）
HY2_BW_FILE   = BGP_DIR + "/hy2_bw.json"             # hy2 满速（Brutal）：{"up": 上行, "down": 下行}（Mbps）；没有 = 自动（BBR）
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
# 出网请求统一的 User-Agent。原来各脚本各报各的家门（xy-installer / media-stack /
# vps-check / net-optimize / xy-sub），等于主动告诉沿途任何人「这台机器在跑 nodekit」——
# GitHub、jsDelivr、公共反代、ip-api 都看得到。换成最常见的 curl 串：不自报家门，
# 而且脚本里 urllib 和 curl 两条路发出去的请求看起来是一致的，不会一台机器两副面孔。
# 别指望它防指纹：TLS 握手特征、请求头顺序照样能认出是 Python。这一步只是不主动声明身份。
HTTP_UA = "curl/8.5.0"

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
SITE_FB_PORT      = 8445        # 本地明文伪装站：trojan 收到不是 trojan 的连接（扫端口的人）回落到这，看到的是网站
NGINX_FB_CONF     = "/etc/nginx/conf.d/bgpeer-fallback.conf"   # 上面那个小站点单独一份，不跟 bgpeer.conf 的几种写法搅在一起
REALITY_FB_PORT   = 8444        # 本地 h2c：xray reality 入口解开后回落进来，nginx 按路径分给 reality-grpc² / reality-xhttp²
                                # 用 TCP 不用 unix socket：nginx 异常退出留下的 .sock 会让它再也起不来（bind 98），443 跟着全断

# 屏蔽中国域名/IP 功能拆到独立文件 cn-block.py，方便单独维护；主脚本只负责拉取+调用。
CNBLOCK_FILE   = BGP_DIR + "/cnblock.json"       # cn-block.py 存的状态（这里只读它判断是否已开启）
CN_BLOCK_LOCAL = BGP_DIR + "/cn-block.py"        # 本地缓存的 cn-block.py
CN_BLOCK_URL   = _RAW + "cn-block.py"            # 仓库里的 cn-block.py（每次尽量拉最新）
ADGUARD_LOCAL  = BGP_DIR + "/adguard-dns.py"     # 本地缓存的 adguard-dns.py
ADGUARD_URL    = _RAW + "adguard-dns.py"         # 仓库里的 adguard-dns.py（自建 DNS·AdGuard Home）
MEDIA_LOCAL    = BGP_DIR + "/media-stack.py"     # 本地缓存的 media-stack.py
MEDIA_URL      = _RAW + "media-stack.py"         # 仓库里的 media-stack.py（自建 Emby·网盘直链）
SELFDNS_FLAG   = BGP_DIR + "/selfdns.on"         # 开关：把本机自建 DNS(AdGuard DoH) 写进订阅 DNS（存在=开）
SELFDNS_CID_FILE = BGP_DIR + "/selfdns.clientid" # AdGuard ClientID（DoH 地址末段；填进「允许的客户端」即可只放行自己）
PRIVDNS_FILE   = BGP_DIR + "/privdns.json"       # 节点私有解析（13 → 11）：本机选的模式 / 外机 DNS 地址 / 本机私有名
PRIVDNS_MAP_FILE = BGP_DIR + "/privdns-map.json" # 本机当 DNS 机时替哪些外机解析：私有名 → IP（AdGuard 规则照它写）
GHRELAY_OFF    = BGP_DIR + "/ghrelay.off"        # 开关：规则/图标走本机 GitHub 中转（默认开；存在此文件=用户手动关了）
GHRELAY_TOKEN_FILE = BGP_DIR + "/ghrelay.token"  # 本机中转的 token（防别人蹭；在 BGP_DIR 不在 SUB_DIR，不会被下载）
GHDL_RELAYS  = BGP_DIR + "/gh-relays.json"   # 取件用的【自己人】中转前缀（从别的机器菜单 14 复制来的）

# 网络优化脚本已并入本仓库（net-optimize.py，BBR/QoS 等内核调优，依赖工具自动安装）；
# 主脚本只负责拉取+调用，状态检测走同一脚本的 --check。
NETOPT_LOCAL = BGP_DIR + "/net-optimize.py"      # 本地缓存的网络优化脚本
NETOPT_SBIN  = "/usr/local/sbin/net-optimize.py" # 装在机器上的那份（开机恢复、守护进程、月度自更新跑它）
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
# 节点画像：节点在本机占的端口（只有端口），交给网络优化从临时端口池摘掉，见 netprofile_sync
NETPROFILE      = BGP_DIR + "/netprofile.json"

# CDN 灾备节点：独立 sing-box 实例(VLESS+WS+TLS)，靠 Cloudflare 橙云中转——
# 客户端连的是 CF 的 IP，VPS 真 IP 被墙时仍能续命。与主节点隔离，重装主节点不受影响。
CDN_DIR   = BGP_DIR + "/cdn"
CDN_STATE = BGP_DIR + "/cdn.json"                # 备用节点状态(域名/uuid/path/端口)
CDN_CRT   = CDN_DIR + "/cert.crt"
CDN_KEY   = CDN_DIR + "/cert.key"
CDN_CONF  = CDN_DIR + "/config.json"
CDN_SVC   = "xy-cdn"
CDN_PORTS = [2053, 2083, 2087, 2096, 8443]       # Cloudflare 免费版可代理的 HTTPS 端口

# CDN量子加密：给 VLESS 类 CDN 节点开 xray 的 VLESS Encryption（mlkem768x25519plus）。
# 为什么只给 CDN：CF 橙云在它那头把 TLS 解开，能看到 VLESS 里的 UUID 和每次要连的目标；
# 这层加密是客户端 ↔ VPS 端到端的，CF 只看到密文。直连节点外面已经是 REALITY/TLS，再套一层纯属浪费。
# 代价（屏上会提示）：
#   · 服务端只有 xray 支持 → 开了以后 VLESS CDN 节点一律改用 xray 内核（关掉时还原原内核）；
#   · 客户端只有 mihomo 系（≥1.19.13）支持；sing-box 到 1.15 都不支持、小火箭未知 →
#     这两种订阅里自动不写这些节点（含优选候选），单条链接照样能导入别的客户端。
# 认证用 X25519 而不是 ML-KEM-768：每次连接的临时密钥交换两种都是 ML-KEM-768+X25519（抗量子），
# 差别只在「认证」——认证得在握手当时破解才有用，没有「先存后解」的问题；ML-KEM 公钥 1184 字节，
# 塞进分享链接会长到二维码都扫不了。所以取 X25519 认证：保密性同样抗量子，链接短。
# 开关存在 CDN_PREF_FILE 的 "quantum" 键：清空重装后新节点照样按开关来。
CDN_QE_MIN_XRAY = (25, 9, 5)                     # VLESS Encryption 是 xray v25.9.5 起才有的

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
# 三大配置（mihomo / sing-box / 小火箭）每天自动按【当前选的模板】重生成：北京时间 03:20 调 `cfg-auto`。
# 排在 cn-block 03:00 刷新（会重启 sing-box）之后、媒体那套 04:10 起的活之前；rules 仓库 02:10 已同步完。
CFG_AUTO_FILE = BGP_DIR + "/cfg_auto.json"      # {"off": [关掉的格式], "hand": {格式: 手改后的 sha256}}
CFG_AUTO_CRON = "/etc/cron.d/bgpeer-cfgupdate"
CFG_AUTO_LOG  = "/var/log/bgpeer-cfgupdate.log"
CFG_AUTO_AT   = (3, 20)                          # 北京时间
# 脚本本体每天自动更新：北京时间 02:50 调 `self-update`，排在凌晨所有活最前面（03:00 cn-block 刷新、03:10 每月 nginx
# 升级、03:20 三大配置、03:30 Emby 元数据、04:00 每月内核……），后面的活直接用新版。跟「配置自动更新」分开管：
# 关了配置自动更新，脚本照样更新（仓库主人 10/21）。开关在菜单 18。
SELFUPD_CRON = "/etc/cron.d/bgpeer-selfupdate"
SELFUPD_LOG  = "/var/log/bgpeer-selfupdate.log"
SELFUPD_OFF  = BGP_DIR + "/selfupdate.off"       # 有这个文件 = 关
SELFUPD_AT   = (2, 50)                           # 北京时间
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
    # 每次先读文件、不先信缓存：重装是后台进程跑的（_node_op_dispatch），它换了端口写进文件，
    # 前台菜单进程缓存的还是旧端口——10/20 仓库主人重装后「14 GitHub 中转」还显示旧端口。
    try:
        p = int(open(SUBPORT_FILE).read().strip())
        if 1024 <= p <= 65535:
            _SUB_PORT = p; return p
    except (OSError, ValueError):
        pass
    if _SUB_PORT:
        return _SUB_PORT
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
# AdGuardHome（自建 DNS 的 DoT/DoH）同样启动时读证书；它装了菜单 8 写的 hook 里有它，可这里每次 --install-cert
# 都会把 acme.sh 记的 reloadcmd 整条换掉——不带上它，下次续期后 AdGuard 还捏着旧证书，到期那天安卓私人 DNS 整机断网。
# try-restart：没装 / 没在跑就什么都不做。
_ACME_RELOAD_HOOK = (" --reloadcmd '"
                     "systemctl reload nginx 2>/dev/null; "
                     "systemctl restart sing-box 2>/dev/null; "
                     "systemctl restart xray 2>/dev/null; "
                     "systemctl restart xy-sub 2>/dev/null; "
                     "systemctl try-restart AdGuardHome 2>/dev/null; true'")

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
        root = _privdns_root()                   # 节点专用域名（13 → 11）要的 *.根域名，同样只能 DNS-01
        if root and _root_domain(G["domain"]) != root:
            root = ""                            # 这次装的域名换了根域名：旧的不带（令牌多半管不着那个区域）
        if root and not _acme_has_cf():
            print(f"{Y_}  ⚠ 节点专用域名要 *.{root}，acme.sh 里没有 Cloudflare 凭据签不出来，"
                  f"这次节点照用公网域名（13 → 11 再选一次就补签）{N_}")
            root = ""
        if (wild or root) and _acme_has_cf():
            # 泛域名只能走 DNS-01。acme.sh 里存着 CF 凭据，那就照着原样签回来，
            # 而不是降级——Emby 的共用关系靠软链指向这个文件，泛域名保住就不会断。
            names = [G["domain"]] + ([f"*.{G['domain']}"] if wild else []) + ([f"*.{root}"] if root else [])
            print(f"  这台机该用泛域名证书（{why or '节点专用域名'}），改走 DNS-01 签 " + " + ".join(names))
            issue = (f"{acme} --issue --dns dns_cf " + " ".join(f"-d '{n}'" for n in names) +
                     f" --keylength ec-256 --server letsencrypt")
        elif wild:
            # 只能降级了，但绝不静默：说清楚丢了什么、会坏什么、装完去哪儿补。
            print(f"{Y_}  ⚠ 这台机该用泛域名证书（{why}），{N_}")
            print(f"{Y_}    但 acme.sh 里没有 Cloudflare 凭据，泛域名签不出来"
                  f"（HTTP-01 只能签单域名，这是 Let's Encrypt 的硬规定）。{N_}")
            print(f"{R_}    这次会签成单域名，后果：Emby 对外是 <子域>.{G['domain']}，"
                  f"单域名盖不住，共用关系会被拆成两张证书、两条续期链。{N_}")
            print(f"{Y_}    装完回面板『15 域名证书管理 → 1 安装证书』用 DNS-01 重签一张"
                  f"泛域名的，再『4 Emby 证书』合并回去。{N_}")
            wild = False
        dns01 = bool(wild or root)
        if not dns01 and G.get("nginx"):
            issue = f"{acme} --issue -d {G['domain']} --webroot {WEBROOT} --keylength ec-256"
        elif not dns01:
            if not port_free(80):
                raise RuntimeError(
                    "80 端口被占用，acme.sh --standalone 无法验证。"
                    "先停掉占用 80 的服务(nginx/caddy 等)，或改用自签(回车跳过域名)。")
            issue = f"{acme} --issue -d {G['domain']} --standalone --keylength ec-256"
        if not dns01:
            fw_allow({(80, "tcp")})                  # HTTP-01 要从外面连 80：开着防火墙先放行（mack-a 同做法）
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
                           mode="dns-cf" if dns01 else
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

def nginx_install_shared():
    """装 nginx 时走网络优化那一份，让两边共用同一个源、同一条月度升级 cron。

    【为什么不直接 apt install】nginx 在这套东西里是共用件：节点要它做伪装站和
    SNI 分流，Emby 那套要它做 443 反代，而 net-optimize 那边管着 nginx.org 官方源
    （Pin=1001）和每月 1 号的自动升级。各装各的话，谁先装决定了拿到哪个版本
    （系统源 1.22 还是官方源的新版），而且只有点过「12 网络优化」的人才有升级保护
    —— 真正在公网跑 nginx 的反而可能没有。走同一个入口就没这问题。

    拿不到脚本 / 跑不起来返回 False，调用方退回系统源直接装 —— 不能因为这个可选的
    优化路径不通，就把节点装不上。
    """
    if have("nginx"):
        return True
    if not ensure_remote_script(NETOPT_URL, NETOPT_LOCAL):
        return False
    print("  装 nginx（走网络优化的官方源，并挂上每月自动升级）...")
    subprocess.run(f"python3 {NETOPT_LOCAL} --nginx-ensure", shell=True)
    return have("nginx")


# ---------------------------------------------------------------------------- 伪装站
# 以前全网一张「We'll be back soon」维护页（7/09 之前是「It works!」）：扫描的人一看就知道这些机器是同一个
# 脚本装的，一个维护页挂几个月也不像真站。mack-a 是从他仓库 9 套博客模板里随机下一套，用他脚本的人共用这 9 张。
# 这里每台机器在本机现生成一个小站：站点类型、站名、配色、字体、版式、文章标题 / 日期 / 正文全随机，机器之间
# 各不一样（仓库主人 10/11 点头）。全部本地生成：不下载、不引外部字体 / CDN / 统计，页面里没有任何跟仓库或
# 作者沾边的字样。生成一次就不再动（真站不会隔三差五整站换样）；用户自己放的站点一律不碰（只认那两张老默认页）。
_FS_ADJ = ["Quiet", "Slow", "Little", "Northern", "Paper", "Small", "Bright", "Late", "Open", "Gentle", "Hidden",
           "Silver", "Blue", "Green", "Golden", "Distant", "Simple", "Morning", "Evening", "Winter", "Autumn", "Amber"]
_FS_NOUN = ["Harbor", "Lantern", "Garden", "Field", "River", "Window", "Compass", "Orchard", "Meadow", "Valley",
            "Shore", "Path", "Notebook", "Kettle", "Atlas", "Lighthouse", "Workshop", "Canvas", "Hearth", "Pine",
            "Stone", "Cloud", "Island", "Bridge", "Willow", "Harvest"]
_FS_PLACE = ["the coast", "the old town", "the market", "the hills", "the harbor", "the station", "the park",
             "the river", "the bridge", "the lake", "a small village", "the north shore", "the valley"]
_FS_WALK = ["the coast", "the river", "the harbor", "the lake", "the north shore", "the canal", "the old railway line"]
_FS_TRIP = ["the old town", "the hills", "a small village", "the mountains", "the valley", "the countryside",
            "a seaside town"]                            # 散步 / 周末出游各用各的：「沿着公园走」「在车站过周末」读着别扭
_FS_SEASON = ["spring", "summer", "autumn", "winter"]
_FS_MONTH = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
_FS_FIRST = ["Anna", "Lena", "Mira", "Clara", "Nora", "Elena", "Iris", "Maya", "Jonas", "Felix", "Tom", "Leo",
             "Daniel", "Martin", "Owen", "Hugo", "Emil", "Theo", "Sara", "Julia"]
_FS_CITY = ["Lisbon", "Porto", "Lyon", "Utrecht", "Bergen", "Leeds", "Ghent", "Malmo", "Graz", "Turku", "Bristol",
            "Aarhus", "Valencia", "Bologna"]
_FS_KINDS = {
    "blog": {
        "names": ["{A} {N}", "The {N} Journal", "{N} & {N2}", "Notes from the {N}", "{A} {N} Diary"],
        "tag": ["Small notes on everyday things", "Thoughts, walks and the occasional recipe",
                "Writing about books, food and slow weekends", "A personal journal, updated now and then"],
        "nav": ["Home", "About"],
        "topic": ["gardening", "bread baking", "long walks", "journaling", "slow travel", "old maps",
                  "home cooking", "learning a language", "second-hand books", "keeping a sketchbook"],
        "verb": ["bake bread", "draw again", "run in the morning", "keep a notebook", "grow tomatoes",
                 "fix my bicycle", "write by hand"],
        "food": ["soup", "dumplings", "pasta", "curry", "pancakes", "risotto", "lentil stew"],
        "about": ["This is a small personal site where I write about ordinary days.",
                  "Posts are irregular and mostly short.",
                  "I like walking, cooking for friends and reading on slow afternoons.",
                  "Nothing here is sponsored; it is just a notebook that happens to be public."],
    },
    # 插画小站（原来是「摄影」：配图只是渐变色块，一个摄影站全是色块反倒怪——仓库主人 10/23。图在本机按标题画，见 _fs_scene_svg）
    "photo": {
        "names": ["{A} Sketches", "{N} Postcards", "{A} {N} Sketchbook", "{N} & Ink", "{A} {N} Drawings"],
        "tag": ["Small drawings and short notes", "Sketches of places, light and quiet moments",
                "A small sketchbook of everyday scenes", "Drawings from walks near and far"],
        "nav": ["Sketches", "About"],
        "pen": ["fountain pen", "soft pencil", "small watercolour set", "brush pen", "box of pastels"],
        "about": ["I draw mostly on walks, close to home or while travelling.",
                  "This site is a small archive of the sketches I want to keep.",
                  "I use a simple kit and try not to fix things too much afterwards.",
                  "Thanks for stopping by and taking a look."],
    },
    "studio": {
        "names": ["{N} Studio", "{A} {N} Studio", "{N} Works", "Studio {N}", "{N} & {N2} Design"],
        "tag": ["Design and small web projects", "Brand, print and web for small teams",
                "A small independent design studio", "Simple design for local businesses"],
        "nav": ["Journal", "About"],
        "thing": ["website", "menu", "booklet", "logo", "shop page", "poster series"],
        "client": ["bakery", "bookshop", "cafe", "florist", "bike shop", "small library", "music school"],
        "audience": ["slow readers", "small screens", "busy people", "local shops"],
        "topic": ["typography", "color", "small budgets", "accessibility", "print", "white space"],
        "about": ["We are a small design studio based in {city}.",
                  "We work with local shops, cafes and cultural projects on identity, print and simple websites.",
                  "Our process is calm and practical: listen first, sketch a lot, ship something useful.",
                  "We take on only a few projects at a time."],
    },
    "notes": {
        "names": ["{N} Notes", "{A} Bytes", "{N} Log", "Notes on {N}", "The {A} Terminal"],
        "tag": ["Things I learned and do not want to forget", "Small notes on code and tools",
                "A notebook for everyday programming", "Short write-ups of small problems"],
        "nav": ["Notes", "About"],
        "tech": ["text encodings", "time zones", "regular expressions", "SQL indexes", "unit tests",
                 "shell scripts", "image compression", "CSS grid", "date parsing", "plain text files"],
        "tool": ["make", "sqlite", "awk", "git", "vim", "a plain text editor", "spreadsheets"],
        "thing": ["dotfiles", "build script", "backup routine", "photo library", "static site", "notes folder"],
        "about": ["I write software for a living and keep these notes mostly for myself.",
                  "Posts are short and practical: small problems, small fixes.",
                  "If something here helps you too, that makes me happy.",
                  "Opinions are my own and change over time."],
    },
}
# 标题句式 → 跟它配套的句子（仓库主人 10/23：摘要跟标题对不上、秋天的早晨写在一月，认真看会觉得怪）。
# 句子里的 {place}/{topic}/… 用标题里同一个词填；首页摘要就是第一句。
_FS_THEMES = {
"blog": {
  "A walk along {walk}": [
    "The path along {walk} is quieter than I remembered.",
    "I walked for almost two hours and only turned back when the light started to fade.",
    "There is a bench near {walk} where I always stop for a minute.",
    "I took the long way back, past the old houses near the water.",
    "Sometimes the best plan is to leave the phone at home and walk."],
  "What I read this {season}": [
    "This {season} I finally made a dent in the pile of books next to my bed.",
    "Most of what I read this {season} was second-hand, picked up for almost nothing.",
    "One of them stayed with me longer than the others, and I keep coming back to it.",
    "I still have a few pages left of the last one, so maybe more on it later.",
    "A friend recommended one of these years ago and I finally got around to it."],
  "Notes on {topic}": [
    "I have been thinking about {topic} a lot lately, so here are a few notes.",
    "{Topic} looked simple from the outside, and of course it is not.",
    "Most of what I know about {topic} I learned by getting it wrong first.",
    "What I like most about {topic} is that it cannot be rushed.",
    "I will probably add to these notes as I go."],
  "Learning to {verb}": [
    "This year I decided to finally learn to {verb}.",
    "The first attempts were clumsy, but each one was a little better than the last.",
    "A short practice every day works better than a long one on Sundays.",
    "I am nowhere near good yet, and I am enjoying that part more than I expected."],
  "A weekend in {trip}": [
    "We spent two slow days in {trip} and did almost nothing, on purpose.",
    "I did not plan much this time, and it turned out to be the best part.",
    "On the first morning we found a small cafe there and went back every day after.",
    "The weather changed three times before noon, which is normal around there.",
    "Nothing dramatic happened, but I came home in a much better mood."],
  "Small things I liked in {month}": [
    "{Month} went by quickly, so here are a few small things worth remembering.",
    "A good loaf from the bakery on the corner, more than once.",
    "Long evenings with a book and the window open.",
    "I keep a short list of things that made the days better, and in {month} it got long."],
  "On {topic} and patience": [
    "{Topic} has taught me more about patience than anything else this year.",
    "Every time I try to hurry it, it goes wrong in a new way.",
    "It reminded me how much I enjoy doing one thing at a time.",
    "Most of the progress happened on the days I almost gave up."],
  "Cooking {food} again": [
    "I made {food} again this week, the way I first learned it.",
    "The recipe is simple, the trick is to be patient with it.",
    "The first pot was a small disaster, the second one was surprisingly good.",
    "There was far too much {food}, so the neighbours got some too.",
    "If you have a favourite way of making {food}, I would love to hear about it someday."],
  "Three lessons from {topic}": [
    "After a few months of {topic}, three things stand out.",
    "First, start smaller than you think you need to.",
    "Second, the boring days count as much as the good ones.",
    "Third, it is fine to take a week off and come back."],
  "Coffee, rain and {topic}": [
    "It rained all afternoon, so I made coffee and went back to {topic}.",
    "There is something about rain on the window that makes it easier to focus.",
    "Most of the afternoon went by without me noticing.",
    "By the evening the rain had stopped and the street smelled of wet stone."],
  "Starting over with {topic}": [
    "I gave up on {topic} a few years ago, and this month I started over.",
    "This time I am keeping it small and not expecting much.",
    "It feels easier the second time, maybe because I stopped trying to be good at it.",
    "I will write again once it has become a habit."],
  "A slow {season} morning": [
    "{Season} mornings here start slowly, and I have stopped fighting it.",
    "I made coffee, opened the window for a minute and just listened.",
    "By the time the street woke up, I had already written two pages.",
    "It is funny how the smallest routines end up holding the whole week together."],
},
"photo": {
  "{Season} light at {place}": [
    "The {season} light at {place} is low and soft for most of the day.",
    "I sat on a low wall and made three quick sketches before the light went flat.",
    "I like how ordinary places change completely with a bit of low sun."],
  "A morning at {place}": [
    "I got to {place} just after sunrise, before most people were up.",
    "The first sketch was too careful, the second one was quicker and better.",
    "There were a few quiet minutes when nobody walked through the view."],
  "Shadows in {month}": [
    "In {month} the shadows get long early in the afternoon.",
    "I spent a few days just drawing them around the neighbourhood.",
    "Some of the lines are a bit wobbly, but I kept them anyway."],
  "Sketching with a {pen}": [
    "For a few weeks I only took a {pen} with me.",
    "With one tool you stop thinking about materials and start looking.",
    "Walking slowly is still the best way I know to find things to draw."],
  "Colors of {place}": [
    "{Place} has more color than I expected once you look for it.",
    "Painted doors, faded signs and a lot of green.",
    "I mixed most of these colors on the spot and wrote down which ones worked."],
  "Rain over {place}": [
    "It rained over {place} for most of the afternoon.",
    "I drew from under a doorway, which made the view narrow and the lines simple.",
    "A few drops landed on the page, and I left them there."],
  "Pages from a {season} sketchbook": [
    "This sketchbook was in my bag for most of the {season}.",
    "Some pages are quick notes, some took a whole afternoon.",
    "Most pages give you one drawing you like, and that is enough."],
  "Late afternoon at {place}": [
    "Late afternoon is my favourite time at {place}.",
    "The light turns warm for about twenty minutes and then it is gone.",
    "I went back the next day for the same view and it looked completely different."],
  "Small details in {month}": [
    "In {month} I tried to draw only small things.",
    "Door handles, leaves on a windowsill, a cup left on a wall.",
    "I am planning to frame a few of these for the wall."],
},
"studio": {
  "Rebuilding a {thing} for a local {client}": [
    "A local {client} asked us to rebuild their {thing}.",
    "The brief was short and clear, which is always a good start.",
    "Most of the work was removing things rather than adding them.",
    "The new {thing} feels calm, which is exactly what they wanted."],
  "Notes on {topic}": [
    "A few notes on {topic} from the last projects.",
    "{Topic} solved more problems for us than any illustration could.",
    "Small details made the biggest difference."],
  "Why we keep things simple": [
    "Most of our clients are small teams with little time to maintain anything complicated.",
    "Simple things are easier to change later, and they always need to change later.",
    "We would rather ship something plain and useful than something clever and fragile."],
  "A new identity for a {client}": [
    "Recently we worked on a new identity for a {client}.",
    "We started with paper sketches before opening any software.",
    "We kept the palette to two colors and one neutral."],
  "What we learned from {topic}": [
    "Working on {topic} this year changed a few of our habits.",
    "We now test drafts on real phones much earlier.",
    "Printing a draft and pinning it to the wall still helps us see the problems."],
  "Designing for {audience}": [
    "Designing for {audience} means cutting more than you would like.",
    "We tested a few versions with real people before choosing one.",
    "Short lines and generous spacing did most of the work."],
  "A small {thing} for a {client}": [
    "A small {thing} for a {client} in our neighbourhood.",
    "The budget was small, so every decision had to earn its place.",
    "It took three rounds of feedback, which felt just right."],
  "Sketches from a {season} project": [
    "A few sketches from a project we worked on this {season}.",
    "Most of these never made it into the final version.",
    "We keep them because they show how we got there."],
},
"notes": {
  "Notes on {tech}": [
    "A few notes on {tech} that I keep having to look up.",
    "{Tech} is one of those things that looks simple until it is not.",
    "I keep relearning this, which is exactly why it goes here."],
  "A small trick with {tool}": [
    "A small trick with {tool} that saved me some time this week.",
    "The fix turned out to be one line, after an hour of looking in the wrong place.",
    "There is probably a better way, but this one is easy to remember."],
  "Cleaning up my {thing}": [
    "I finally spent an evening cleaning up my {thing}.",
    "Most of it was things I added years ago and never used.",
    "After cleaning things up, the whole thing feels noticeably lighter."],
  "Why I like {tool}": [
    "I have used {tool} for years and it still surprises me sometimes.",
    "Most of the time the simplest tool is good enough.",
    "It does one thing and stays out of the way."],
  "Reading about {tech}": [
    "I spent a weekend reading about {tech}.",
    "The documentation was correct, I just had not read it carefully before.",
    "I wish someone had told me some of this when I started."],
  "Debugging a slow {thing}": [
    "My {thing} had become slow, so I finally looked into it.",
    "Measuring before changing anything saved me from a pointless rewrite.",
    "In the end it was one small change in the wrong place."],
  "Things I got wrong about {tech}": [
    "A short list of things I got wrong about {tech}.",
    "Writing a tiny test first made most of them obvious.",
    "Naming things clearly helped more than any clever trick."],
  "Keeping a tidy {thing}": [
    "Keeping a tidy {thing} is mostly about small habits.",
    "Plain text keeps winning for small personal projects like this.",
    "It is worth writing down the commands you only need twice a year."],
},
}
# 跟哪篇放在一起都不突兀的句子（正文后半段补几句用；不当摘要）
_FS_FILLER = {
  "blog": ["I wrote a few lines in my notebook afterwards.", "More on this another time.",
           "I am trying to write here more often, even if the posts are short.",
           "It was a good reminder to slow down a little.", "Thanks for reading, as always."],
  "photo": ["Thanks for looking.", "More from this series soon.",
            "These were scanned straight from the sketchbook, with only small adjustments.", "I hope to go back there soon."],
  "studio": ["We are always happy to work with neighbours and small teams.",
             "Afterwards we wrote down what we would do differently next time.", "Thanks to everyone involved."],
  "notes": ["Mostly a note for future me.", "If something here helps you too, that makes me happy.",
            "Next time I will start with the boring solution and see if it is enough."],
}

_FS_FONTS = ["Georgia, 'Times New Roman', serif",
             "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
             "'Palatino Linotype', Palatino, 'Book Antiqua', serif",
             "'Trebuchet MS', 'Segoe UI', Tahoma, sans-serif",
             "'Gill Sans', 'Gill Sans MT', Calibri, sans-serif"]
_FS_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# 插画站的配图：按标题在本机画一张矢量风景（仓库主人 10/23：不联网下载——访问图库会暴露这台机器，
# 而且同一批照片在很多台机器上出现本身就是特征）。海边 → 海平线和倒影，山谷 / 山里 → 几层山，城镇 / 车站 / 市场 →
# 一排房子，下雨 → 雨丝，小物件 → 窗台；季节定配色（标题没写就按发布日期的月份），早晨 / 傍晚定太阳高低和天色。
# 每张的山形、房子、云都是随机的，同一个站里也不会两张一样。
_FS_SEASON_COL = {   # 天顶, 天边, 远景, 中景, 近景, 点缀
    "winter": ("#9fb4c8", "#e9eef3", "#c4d0db", "#dde5ec", "#f4f6f8", "#6f8296"),
    "spring": ("#8ec3e6", "#eaf5fb", "#a9c9a0", "#84b77a", "#5f9a5c", "#e6a3b8"),
    "summer": ("#4f9fd8", "#d6ecfa", "#8fb58a", "#5f9a55", "#3f7a3c", "#f2c14e"),
    "autumn": ("#e3a873", "#f8e3cc", "#c99a6b", "#b0703f", "#7d4b2a", "#d8662f"),
}

def _fs_scene_svg(title, date, R):
    t = title.lower()
    season = next((x for x in _FS_SEASON if x in t), None) or \
        ("winter", "winter", "spring", "spring", "spring", "summer", "summer", "summer",
         "autumn", "autumn", "autumn", "winter")[date.month - 1]
    sky0, sky1, far, mid, near, acc = _FS_SEASON_COL[season]
    if "morning" in t:                                     # 早晨 / 傍晚太阳低，但要挂在山和海平线上面，别被挡住
        sky1, sun_y, sun_c = "#fbe3c6", R.randint(170, 205), "#fff1d6"
    elif "late afternoon" in t or "shadows" in t:
        sky1, sun_y, sun_c = "#f6c58f", R.randint(150, 195), "#ffd9a0"
    else:
        sun_y, sun_c = R.randint(80, 170), "#fff6dc"
    rain = "rain" in t
    if rain:
        sky0, sky1 = "#7f8b96", "#c7ced4"
    if any(w in t for w in ("coast", "shore", "harbor", "lake", "river", "canal", "seaside")):
        scene = "sea"
    elif any(w in t for w in ("valley", "hills", "mountains", "countryside")):
        scene = "hills"
    elif any(w in t for w in ("town", "market", "station", "bridge", "park", "village", "railway")):
        scene = "town"
    elif "details" in t or "sketchbook" in t:
        scene = "still"
    else:
        scene = R.choice(["sea", "hills", "town"])
    W, H = 800, 500
    def darker(c, k=0.72):
        return "#%02x%02x%02x" % tuple(int(int(c[i:i + 2], 16) * k) for i in (1, 3, 5))
    def ridge(y0, amp, n, color):
        xs = sorted(R.sample(range(40, W - 40), n - 2)) if n > 2 else []
        pts = [(0, y0 + R.randint(-amp, amp))] + [(x, y0 + R.randint(-amp, amp)) for x in xs] + [(W, y0 + R.randint(-amp, amp))]
        d = f"M0,{H} L{pts[0][0]},{pts[0][1]} "
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            d += f"Q{x1 + (x2 - x1) // 2},{min(y1, y2) - R.randint(0, amp)} {x2},{y2} "
        return f'<path d="{d}L{W},{H} Z" fill="{color}"/>'
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
           f'<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky0}"/>'
           f'<stop offset="1" stop-color="{sky1}"/></linearGradient></defs>',
           f'<rect width="{W}" height="{H}" fill="url(#s)"/>']
    if not rain:
        out.append(f'<circle cx="{R.randint(120, 680)}" cy="{sun_y}" r="{R.randint(26, 46)}" fill="{sun_c}" opacity="0.9"/>')
    for _ in range(R.randint(1, 4)):                       # 云
        cx, cy, k = R.randint(60, 740), R.randint(40, 180), R.uniform(0.7, 1.4)
        out.append(f'<g fill="#ffffff" opacity="{0.5 if rain else 0.75}">'
                   + "".join(f'<ellipse cx="{cx + int(dx * k)}" cy="{cy + int(dy * k)}" rx="{int(rx * k)}" ry="{int(ry * k)}"/>'
                             for dx, dy, rx, ry in ((0, 0, 46, 16), (-34, 6, 30, 12), (36, 5, 34, 13), (8, -10, 28, 14)))
                   + "</g>")
    if scene == "sea":
        hz = R.randint(250, 300)
        out.append(ridge(hz - 8, 10, R.randint(3, 5), far))
        out.append(f'<rect y="{hz}" width="{W}" height="{H - hz}" fill="{sky0}" opacity="0.85"/>')
        for i in range(R.randint(6, 12)):                   # 水面的光
            y = R.randint(hz + 10, H - 10); x = R.randint(0, W - 120)
            out.append(f'<rect x="{x}" y="{y}" width="{R.randint(40, 140)}" height="3" fill="#ffffff" opacity="0.35"/>')
        if R.random() < 0.6:                                # 小船
            bx, by = R.randint(120, 640), hz + R.randint(40, 120)
            out.append(f'<path d="M{bx},{by} l60,0 l-10,14 l-40,0 Z" fill="{acc}"/>'
                       f'<path d="M{bx + 28},{by} l0,-46 l24,40 Z" fill="#fafafa"/>')
        out.append(ridge(H - 40, 12, 3, near))
    elif scene == "hills":
        for y0, amp, col in ((230, 50, far), (300, 40, mid), (380, 30, near)):
            out.append(ridge(y0 + R.randint(-20, 20), amp, R.randint(3, 6), col))
        for _ in range(R.randint(3, 9)):                     # 树
            x, y = R.randint(20, 780), R.randint(400, 470)
            out.append(f'<path d="M{x},{y} l14,-46 l14,46 Z" fill="{darker(near)}"/>'
                       f'<rect x="{x + 12}" y="{y}" width="4" height="8" fill="{darker(near, 0.5)}"/>')
    elif scene == "town":
        out.append(ridge(320, 24, 4, far))
        x = -10
        while x < W:
            w, h = R.randint(60, 120), R.randint(90, 210)
            top = 430 - h
            out.append(f'<rect x="{x}" y="{top}" width="{w}" height="{h + 80}" fill="{R.choice([mid, far, acc, near])}" opacity="0.92"/>')
            if R.random() < 0.6:
                out.append(f'<path d="M{x - 4},{top} L{x + w // 2},{top - R.randint(20, 40)} L{x + w + 4},{top} Z" fill="{near}"/>')
            for wy in range(top + 18, 420, 34):
                for wx in range(x + 12, x + w - 16, 28):
                    if R.random() < 0.7:
                        out.append(f'<rect x="{wx}" y="{wy}" width="12" height="16" fill="#fdf6e3" opacity="0.8"/>')
            x += w + R.randint(2, 14)
        out.append(f'<rect y="430" width="{W}" height="70" fill="{near}" opacity="0.9"/>')
    else:                                                   # 窗台小景
        out.append(f'<rect x="60" y="40" width="680" height="380" fill="none" stroke="{near}" stroke-width="22"/>')
        out.append(f'<line x1="400" y1="40" x2="400" y2="420" stroke="{near}" stroke-width="14"/>')
        out.append(f'<rect y="420" width="{W}" height="80" fill="{mid}"/>')
        px = R.randint(160, 560)
        out.append(f'<path d="M{px},420 l70,0 l-10,-60 l-50,0 Z" fill="{acc}"/>')
        for _ in range(R.randint(4, 7)):
            out.append(f'<ellipse cx="{px + 35 + R.randint(-40, 40)}" cy="{350 - R.randint(0, 70)}" rx="{R.randint(10, 18)}" '
                       f'ry="{R.randint(22, 34)}" fill="{far}" transform="rotate({R.randint(-50, 50)} {px + 35} 360)"/>')
        if R.random() < 0.7:
            cx = R.randint(120, 680)
            out.append(f'<rect x="{cx}" y="392" width="34" height="28" rx="4" fill="#fafafa"/>')
    if rain:
        for _ in range(R.randint(60, 110)):
            x, y = R.randint(0, W), R.randint(-20, H)
            out.append(f'<line x1="{x}" y1="{y}" x2="{x - 6}" y2="{y + 22}" stroke="#ffffff" stroke-width="1.5" opacity="0.45"/>')
    out.append("</svg>")
    return "\n".join(out) + "\n"

def fake_site_write(root, rnd=None):
    """在 root 现生成一个小静态站（首页 / 文章 / 关于 / 404 / robots.txt），返回站名。各项都随机，见上。"""
    import html as _html, datetime, colorsys
    R = rnd or random.SystemRandom()
    kind = R.choice(list(_FS_KINDS)); K = _FS_KINDS[kind]
    n1, n2 = R.sample(_FS_NOUN, 2)
    name = R.choice(K["names"]).format(A=R.choice(_FS_ADJ), N=n1, N2=n2)
    tag, first, city = R.choice(K["tag"]), R.choice(_FS_FIRST), R.choice(_FS_CITY)
    # 配色 / 字体 / 版式
    hue, dark = R.random(), R.random() < 0.3
    def hexc(h, l, s):
        r, g, b = colorsys.hls_to_rgb(h % 1, l, s)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
    accent = hexc(hue, R.uniform(0.62, 0.72) if dark else R.uniform(0.32, 0.42), R.uniform(0.35, 0.65))
    bg = hexc(hue, R.uniform(0.08, 0.12), 0.12) if dark else hexc(hue, R.uniform(0.96, 0.985), R.uniform(0.15, 0.4))
    fg = "#e8e6e1" if dark else hexc(hue, 0.16, 0.15)
    muted = "#a7a29a" if dark else hexc(hue, 0.42, 0.08)
    card = hexc(hue, 0.15, 0.12) if dark else "#ffffff"
    font = _FS_MONO if kind == "notes" and R.random() < 0.5 else R.choice(_FS_FONTS)
    layout = R.choice(["column", "side", "cards"])
    width = R.choice([640, 680, 720, 760, 820])
    radius = R.choice([0, 4, 8, 12])
    datefmt = R.choice(["{d:%Y-%m-%d}", "{d:%B} {d.day}, {d.year}", "{d.day} {d:%b} {d.year}"])
    # 文章：5~10 篇，日期往前随机撒，新的在前；每篇日期不撞，标题里带季节 / 月份的日期就落在那个季节 / 月份里
    today = datetime.date.today()
    themes = _FS_THEMES[kind]
    # 标题里的空位按「洗一遍牌、依次发」取，同一个站里尽量不撞词（别三篇都是「…for a bookshop」）
    src = {"place": _FS_PLACE, "walk": _FS_WALK, "trip": _FS_TRIP, "season": _FS_SEASON, "month": _FS_MONTH,
           "titles": list(themes)}
    src.update({k: K[k] for k in ("topic", "verb", "food", "pen", "thing", "client", "audience", "tech", "tool")
                if k in K})                                     # 标题句式本身也按牌发，不连着几篇都是「Notes on …」
    decks = {k: [] for k in src}
    def deal(k):
        if not decks[k]:
            decks[k] = R.sample(src[k], len(src[k]))
        return decks[k].pop()
    season_months = {"winter": (12, 1, 2), "spring": (3, 4, 5), "summer": (6, 7, 8), "autumn": (9, 10, 11)}
    days = [today - datetime.timedelta(days=n) for n in range(4, 901)]
    used_days = set()
    def sub(text, vals):
        def one(m):
            k = m.group(1)
            v = vals[k.lower()]
            return v[:1].upper() + v[1:] if k[:1].isupper() else v
        return re.sub(r"\{(\w+)\}", one, text)
    posts, seen = [], set()
    for _ in range(R.randint(5, 10)):
        for _try in range(20):
            tpl = deal("titles")
            vals = {k.lower(): deal(k.lower()) for k in re.findall(r"\{(\w+)\}", tpl)}
            t = sub(tpl, vals)
            if t not in seen:
                break
        seen.add(t)
        slug = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60] or "post"
        while any(p["slug"] == slug for p in posts):
            slug += "-" + str(R.randint(2, 9))
        ok = [d for d in days if d not in used_days
              and ("season" not in vals or d.month in season_months[vals["season"]])
              and ("month" not in vals or _FS_MONTH[d.month - 1] == vals["month"])]
        date = R.choice(ok or [d for d in days if d not in used_days])
        used_days.add(date)
        lines = [sub(x, vals) for x in themes[tpl]]
        # 按写好的顺序（第一句是摘要、跟标题直接对上，最后一句收尾）；句子多的随机省掉中间一句，各站不全一样
        body = lines if len(lines) <= 3 else [x for x in lines if x != R.choice(lines[1:-1])]
        body += R.sample(_FS_FILLER[kind], R.randint(1, 2))
        paras, i = [], 0
        while i < len(body):
            k = R.randint(2, 3)
            paras.append(" ".join(body[i:i + k])); i += k
        posts.append({"title": t, "slug": slug, "date": date, "paras": paras})
    posts.sort(key=lambda p: p["date"], reverse=True)
    since = min(p["date"] for p in posts).year - R.randint(0, 3)
    years = f"{since}&ndash;{today.year}" if since < today.year else str(today.year)
    E = _html.escape
    initial = E(name[0])
    icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
            f"%3Crect width='64' height='64' rx='{R.choice([0, 12, 32])}' fill='%23{accent[1:]}'/%3E"
            f"%3Ctext x='32' y='44' font-size='34' text-anchor='middle' fill='white' font-family='sans-serif'%3E{initial}%3C/text%3E%3C/svg%3E")
    css = (f"*{{box-sizing:border-box}}html{{-webkit-text-size-adjust:100%}}"
           f"body{{margin:0;background:{bg};color:{fg};font-family:{font};line-height:1.65;font-size:17px}}"
           f"a{{color:{accent};text-decoration:none}}a:hover{{text-decoration:underline}}"
           f".wrap{{max-width:{width}px;margin:0 auto;padding:28px 20px 48px}}"
           f"header .name{{font-size:1.5rem;font-weight:700;color:{fg}}}header .tag{{color:{muted};margin:.2rem 0 0}}"
           f"nav a{{margin-right:1.1rem}}nav{{margin:.9rem 0 2rem}}"
           f".meta{{color:{muted};font-size:.88rem}}h1{{font-size:1.7rem;line-height:1.3;margin:.2rem 0 .4rem}}"
           f"h2{{font-size:1.15rem;margin:0 0 .2rem}}.post{{margin:0 0 1.6rem}}"
           f"footer{{margin-top:3rem;color:{muted};font-size:.85rem}}")
    if layout == "side":
        css += (f"@media(min-width:900px){{.wrap{{max-width:{width + 260}px;display:grid;"
                f"grid-template-columns:220px 1fr;gap:40px}}header{{position:sticky;top:28px;align-self:start}}"
                f"nav a{{display:block;margin:.3rem 0}}}}")
    if layout == "cards":
        css += (f".list{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}"
                f".post{{background:{card};padding:18px;border-radius:{radius}px;margin:0;"
                f"box-shadow:0 1px 3px rgba(0,0,0,.08)}}")
    if kind == "photo":
        css += (f".ph{{display:block;width:100%;height:auto;aspect-ratio:8/5;object-fit:cover;"
                f"border-radius:{radius}px;margin:0 0 .6rem}}")
    links = [("/", K["nav"][0])] + [("/about/", n) if n == "About" else ("/", n) for n in K["nav"][1:]]
    def page(title, main):
        nav = "".join(f'<a href="{h}">{E(t)}</a>' for h, t in links)
        return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
                f"<title>{E(title)}</title>\n<meta name=\"description\" content=\"{E(tag)}\">\n"
                f"<link rel=\"icon\" href=\"{icon}\">\n<style>{css}</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
                f"<header><a class=\"name\" href=\"/\">{E(name)}</a><p class=\"tag\">{E(tag)}</p>"
                f"<nav>{nav}</nav></header>\n<main>\n{main}\n</main>\n</div>\n"
                f"<div class=\"wrap\" style=\"padding-top:0\"><footer>&copy; {years} {E(name)}</footer></div>\n"
                "</body>\n</html>\n")
    def pic(p):                                             # 插画站：配图是本机画的 /img/<文章>.svg（见 _fs_scene_svg）
        return f'<img class="ph" src="/img/{p["slug"]}.svg" alt="{E(p["title"])}" loading="lazy">'
    items = "\n".join(
        f'<article class="post">{pic(p) if kind == "photo" else ""}'
        f'<h2><a href="/posts/{p["slug"]}.html">{E(p["title"])}</a></h2>'
        f'<div class="meta">{datefmt.format(d=p["date"])}</div>'
        + (f'<p>{E(p["paras"][0].split(". ")[0].rstrip("."))}.</p>' if layout != "cards" else "")
        + "</article>" for p in posts)
    files = {"index.html": page(name, f'<div class="list">\n{items}\n</div>')}
    for j, p in enumerate(posts):
        nxt = posts[j + 1] if j + 1 < len(posts) else None
        main = (f'<article><h1>{E(p["title"])}</h1><div class="meta">{datefmt.format(d=p["date"])}</div>\n'
                + (pic(p) if kind == "photo" else "")
                + "".join(f"<p>{E(x)}</p>\n" for x in p["paras"])
                + "</article>\n<p class=\"meta\"><a href=\"/\">&larr; All posts</a>"
                + (f' &middot; Older: <a href="/posts/{nxt["slug"]}.html">{E(nxt["title"])}</a>' if nxt else "")
                + "</p>")
        files[f"posts/{p['slug']}.html"] = page(f"{p['title']} – {name}", main)
    about = [s.format(city=city) for s in K["about"]]
    hello = "" if kind == "studio" else f"<p>Hi, I&rsquo;m {E(first)}.</p>\n"
    files["about/index.html"] = page(f"About – {name}", "<h1>About</h1>\n" + hello
                                     + "".join(f"<p>{E(s)}</p>\n" for s in about))
    files["404.html"] = page(f"Page not found – {name}", "<h1>Page not found</h1>\n"
                             "<p>The page you are looking for does not exist or has moved.</p>\n"
                             "<p><a href=\"/\">Back to the homepage</a></p>")
    # 归档页：访问 /posts/ 别回 403（nginx 列目录被禁时就是 403，跟别的不存在的路径不一样）
    arch = "\n".join(f'<li><a href="/posts/{p["slug"]}.html">{E(p["title"])}</a> '
                     f'<span class="meta">{datefmt.format(d=p["date"])}</span></li>' for p in posts)
    files["posts/index.html"] = page(f"Archive – {name}", f"<h1>Archive</h1>\n<ul>\n{arch}\n</ul>")
    if kind == "photo":
        for p in posts:
            files[f"img/{p['slug']}.svg"] = _fs_scene_svg(p["title"], p["date"], R)
    files["robots.txt"] = "User-agent: *\nDisallow:\n"
    # 文件时间跟着文章走：nginx 回的 Last-Modified 就是文件时间，全是装机那一刻等于说「这站是某天一口气生成的」。
    # 文章 / 配图 = 发布那天的某个钟点；首页、归档 = 最新一篇；关于 / 404 / robots = 建站那年的某天
    def stamp(d):
        return calendar.timegm(d.timetuple()) + R.randint(7 * 3600, 23 * 3600)
    newest = stamp(posts[0]["date"])
    oldest = stamp(datetime.date(since, R.randint(1, 12), R.randint(1, 28)))
    when = {"index.html": newest, "posts/index.html": newest}
    for p in posts:
        when[f"posts/{p['slug']}.html"] = when[f"img/{p['slug']}.svg"] = stamp(p["date"])
    for rel, txt in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        os.chmod(path, 0o644)                              # nginx 以 www-data 读：别被 umask 收成 0600（403）
        ts = when.get(rel, oldest)
        os.utime(path, (ts, ts))
    for d in (root, os.path.join(root, "posts"), os.path.join(root, "about"), os.path.join(root, "img")):
        if os.path.isdir(d):
            os.chmod(d, 0o755)
    return name

# 老的两张默认页（全网一样）：只认这两张，认出来才换；用户自己放的页面一律不碰
_OLD_ITWORKS_PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>Welcome</title></head>"
                     "<body><h1>It works!</h1><p>The server is running.</p></body></html>\n")
_OLD_MAINT_PAGE = (
    "<!doctype html>\n<html lang=\"en\">\n<head>\n"
    "<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
    "<title>{title}</title>\n"
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
    "<footer>&copy; {year} &middot; All rights reserved.</footer>\n"
    "</div>\n</body>\n</html>\n")

def _is_stock_page(path):
    """首页还是老的全网一样的默认页吗（标题 = 当时的域名、年份不论）。读不到 / 改过一个字都算不是。"""
    try:
        t = open(path, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError):
        return False
    if t == _OLD_ITWORKS_PAGE:
        return True
    m = re.search(r"<title>(.*?)</title>", t)
    y = re.search(r"&copy; (\d{4}) &middot;", t)
    return bool(m and y) and t == _OLD_MAINT_PAGE.replace("{title}", m.group(1)).replace("{year}", y.group(1))

def fake_site_heal():
    """老装机：伪装站首页还是那张全网一样的老默认页 → 换成本机生成的站。返回是否换了。"""
    if not _is_stock_page(WEBROOT + "/index.html"):
        return False
    fake_site_write(WEBROOT)
    return True

def ensure_nginx():
    if not have("nginx") and not nginx_install_shared():
        sh("apt-get update -y", check=False)
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y nginx", check=False)
    clean_stale_nginx()                                  # 先清掉别人残留的坏块，保证 nginx -t 能过
    os.makedirs(WEBROOT, exist_ok=True)
    idx = WEBROOT + "/index.html"
    if not os.path.exists(idx) or _is_stock_page(idx):  # 伪装站：本机现生成一个（见 fake_site_write）
        # 用户可直接覆盖 WEBROOT 里的文件换成自己的真站内容；换过的这里一律不碰。
        fake_site_write(WEBROOT)

def nginx_reload():
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:
        raise RuntimeError("nginx 配置校验未通过:\n" + (chk.stderr or chk.stdout).strip())
    sh("systemctl enable --now nginx", check=False)
    sh("systemctl reload nginx", check=False)

def _site_fallback_ok():
    """这台机器有没有伪装站可回落：有域名、nginx 在（前置 / SNI 分流 / reality 占 443 时 nginx 只留 :80，伪装站文件照样在）。"""
    return bool(G.get("domain")) and bool(G.get("nginx") or G.get("sni_split"))

def ensure_site_fallback():
    """本机 127.0.0.1:SITE_FB_PORT 起一个只服务伪装站的明文站点，给自己端口上的 trojan 当回落（仓库主人 10/23）：
       扫端口的人连上来、发的不是 trojan，看到的是网站首页 / 网站自己的 404 页，跟 443 上一模一样，
       而不是「握手完一句话不回就断」。单独一个文件，bgpeer.conf 怎么重写都不碰它；写过、内容没变就不动。"""
    conf = (f"server {{\n  listen 127.0.0.1:{SITE_FB_PORT};\n  server_name _;\n  server_tokens off;\n"
            f"  error_page 404 /404.html;\n"
            f"  location / {{ root {WEBROOT}; index index.html; try_files $uri $uri/index.html =404; }}\n}}\n")
    try:
        if open(NGINX_FB_CONF).read() == conf:
            return
    except OSError:
        pass
    os.makedirs(os.path.dirname(NGINX_FB_CONF), exist_ok=True)
    open(NGINX_FB_CONF, "w").write(conf)
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:                                   # 万一 nginx -t 不过：撤掉这份，别连累 443
        os.remove(NGINX_FB_CONF)
        return
    sh("systemctl reload nginx", check=False)

def write_nginx_acme_stub():
    """先放一个 80 server 块，供 acme webroot 验证用（此时还没证书，不写 443）。"""
    conf = (f"server {{\n  listen 80;\n{_V6_80()}  server_name {G['domain']};\n"
            f"  location /.well-known/acme-challenge/ {{ root {WEBROOT}; }}\n"
            f"  location / {{ return 404; }}\n}}\n")
    open(NGINX_CONF, "w").write(conf)
    nginx_reload()

def _ipv6_ok():
    """本机内核支不支持 IPv6 套接字。有的 VPS（部分 OpenVZ/LXC、开机参数 ipv6.disable=1）整个没有 IPv6，
       nginx 写了 `listen [::]:80` 就 `Address family not supported`，nginx -t 不过、整个安装失败。"""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.close(); return True
    except OSError:
        return False

def _v6(line):
    """nginx 的 IPv6 监听行：本机没有 IPv6 就不写。"""
    return line if _ipv6_ok() else ""

def _V6_80():
    return _v6("  listen [::]:80;\n")       # 单独拎出来：f-string 里不能写反斜杠（老 Python）

def _nginx_ws_locations():
    """ws 家族 / xhttp / grpc 的 location 反代块（供 443 或本地 https server 复用）。

       路径被人摸到（浏览器 / curl / 扫描器拿普通请求来试）时，回的一律是伪装站自己的 404 页，跟访问网站上
       别的不存在的页面一模一样（server 里的 error_page 404 /404.html）——仓库主人 10/23 让逐个协议对齐。
       以前 ws 是直接断连接（444，mack-a 同做法）、xhttp 是 301 跳转 / xray 回 400，都跟别的路径不一样，
       等于告诉人「这个路径后面有东西」。"""
    locs = ""
    for w in NGINX_WS:
        if w.get("kind") in ("xhttp", "grpc"):
            # grpc-tls（sing-box）同一套：后端明文 h2c，路径是 /服务名/Tun，前缀匹配
            # xray 的 xhttp 挂 nginx 后面：后端是 xray 明文 h2c，nginx 用 grpc_pass 转（XTLS 官方 nginx 示例同做法），
            # stream-one / stream-up / packet-up 三种上行都走得通；proxy_pass(http/1.1) 只有 packet-up 能用。
            # xhttp 的路径后面还会跟 /会话ID/序号，所以这里是前缀匹配（ws 是精确匹配）。
            # 不带斜杠的那个路径单独回 404：不然 nginx 对前缀 location 自动回 301 跳到带斜杠的，等于把路径认了。
            # grpc：不是 gRPC 请求一律 404（mack-a 同做法）。xhttp 的请求不带 grpc 的 content-type，不能这么挡，
            # 改成后端（xray）回的错误码换成伪装站的 404 页——正常的 xhttp 流量 xray 回 200，不受影响。
            guard = ('    if ($content_type !~ "application/grpc") {\n      return 404;\n    }\n'
                     if w.get("kind") == "grpc" else
                     "    grpc_intercept_errors on;\n    error_page 400 403 404 405 =404 /404.html;\n")
            locs += (f"  location = {w['path']} {{\n    return 404;\n  }}\n"
                     f"  location {w['path']}/ {{\n" + guard +
                     f"    client_max_body_size 0;\n"
                     f"    client_body_timeout 1h;\n"
                     f"    grpc_read_timeout 1h;\n"
                     f"    grpc_send_timeout 1h;\n"
                     f"    grpc_set_header Host $host;\n"
                     f"    grpc_set_header X-Real-IP $remote_addr;\n"
                     f"    grpc_pass grpc://127.0.0.1:{w['port']};\n  }}\n")
            continue
        locs += (f"  location = {w['path']} {{\n"
                 # 不是 WebSocket 升级的请求：回伪装站的 404 页。更早是原样转给后端（回 400 Bad Request，一眼看出
                 # 后面挂着代理），后来改成 444 直接断连接——也跟别的路径不一样，10/23 改成 404
                 f"    if ($http_upgrade !~* \"^websocket$\") {{ return 404; }}\n"
                 f"    proxy_pass http://127.0.0.1:{w['port']};\n"
                 f"    proxy_http_version 1.1;\n"
                 f"    proxy_set_header Upgrade $http_upgrade;\n"
                 f"    proxy_set_header Connection \"upgrade\";\n"
                 f"    proxy_set_header Host $host;\n"
                 f"    proxy_set_header X-Real-IP $remote_addr;\n  }}\n")
    return locs

def _nginx_80_server():
    """:80——acme webroot 验证 + 跳转到 https。"""
    return (f"server {{\n  listen 80;\n{_V6_80()}  server_name {G['domain']};\n"
            f"  server_tokens off;\n"                 # 别报 nginx/1.24.0，只回 nginx
            f"  location /.well-known/acme-challenge/ {{ root {WEBROOT}; }}\n"
            f"  location / {{ return 301 https://$host$request_uri; }}\n}}\n")

def _nginx_https_server(listen):
    """https 伪装站 + ws 反代；listen 为监听指令（公网 443 或本地 127.0.0.1:8443）。"""
    pn = _privdns_nginx_name()                        # 节点专用域名（13 → 11）：证书盖得住就一起认
    return (f"server {{\n{listen}"
            f"  server_name {G['domain']}{' ' + pn if pn else ''};\n"
            f"  server_tokens off;\n"                 # 同上：伪装站也别把版本号摆出来
            f"  ssl_certificate {ACME_CRT};\n  ssl_certificate_key {ACME_KEY};\n"
            f"  ssl_protocols TLSv1.2 TLSv1.3;\n"
            f"{_nginx_ws_locations()}"
            f"  error_page 404 /404.html;\n"           # 伪装站自己的 404 页（fake_site_write 生成）
            f"  location / {{ root {WEBROOT}; index index.html; try_files $uri $uri/index.html =404; }}\n}}\n")

def nginx_http2_listen(base):
    """按 nginx 版本给出 listen 行 —— 1.25.1 起 `listen ... http2` 被弃用。

    【必须按版本二选一，不能一刀切】`http2 on;` 在 1.25.1 之前是未知指令，会让
    nginx -t 直接失败；而 `listen ... ssl http2` 在新版上虽然还能跑，nginx -t 会
    每次都吐一条 deprecated 警告，而且官方已经宣布要移除。

    现场撞到的就是后一种：nginx.org 源换上 1.30.5 之后，
      nginx: [warn] the "listen ... http2" directive is deprecated ... bgpeer.conf:9
    media-stack 那边早就有这个判断（nginx_supports_http2_directive），
    只有这里一直写死，两个脚本对着同一个 nginx 各写各的。

    base 形如 "443 ssl" / "[::]:443 ssl" / "127.0.0.1:8443 ssl"，
    返回完整的一行（含缩进和换行）。
    """
    v = subprocess.run("nginx -v", shell=True, text=True, capture_output=True)
    m = re.search(r"/(\d+)\.(\d+)\.(\d+)", (v.stdout or "") + (v.stderr or ""))
    new = bool(m) and tuple(int(x) for x in m.groups()) >= (1, 25, 1)
    return f"  listen {base};\n" if new else f"  listen {base} http2;\n"


def nginx_http2_on():
    """新版才需要单独的 `http2 on;`；旧版返回空串（写了会让 nginx -t 失败）。"""
    v = subprocess.run("nginx -v", shell=True, text=True, capture_output=True)
    m = re.search(r"/(\d+)\.(\d+)\.(\d+)", (v.stdout or "") + (v.stderr or ""))
    new = bool(m) and tuple(int(x) for x in m.groups()) >= (1, 25, 1)
    return "  http2 on;\n" if new else ""


def write_nginx_conf():
    """签好证书、收集完 ws 家族后，写完整 conf：80 跳转 + 443 伪装站 + ws 按 path 反代。"""
    listen = (nginx_http2_listen("443 ssl")
              + _v6(nginx_http2_listen("[::]:443 ssl"))
              + nginx_http2_on())
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
    srv = ("server {\n  listen 443 reuseport;\n" + _v6("  listen [::]:443 reuseport;\n") +
           "  ssl_preread on;\n  proxy_pass $bgpeer_upstream;\n}\n")
    return m + srv

def sni_split_preflight():
    """真正改动前先探测：装 stream 模块，用一份『结构等价』的测试 stream 配置跑 nginx -t。
       通过才敢走 sni-split；不通过返回 False，让调用方退回 reality-443 直连模式。
       全过程可回滚，绝不把用户能用的 nginx 改坏。"""
    if not have("nginx") and not nginx_install_shared():
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
    listen = (nginx_http2_listen(f"127.0.0.1:{SNI_HTTPS_PORT} ssl")
              + nginx_http2_on())
    open(NGINX_CONF, "w").write(_nginx_80_server() + _nginx_https_server(listen) + _nginx_fb_server())
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

# reality 借用目标池：大厂 / 技术站的主站，要 TLS1.3 + h2 + X25519、国内可达、不套乱 CDN。
# CDN 资源域名不收：同一个名字各地由不同边缘节点接，TLS 配置不一，有的机房连 TLS1.3 都握不上
#   （仓库主人 10/11 撞上 updates.cdn-apple.com）。
# 10/18 只留能开 ML-DSA-65 抗量子签名的（目标站支持 X25519MLKEM768、带 SNI 握手的证书链 > 4000 字节，见 PQ_ON_MIN_CHAIN）：
#   仓库主人在洛杉矶一台实测（openssl 看 TLS1.3 / h2，xray tls ping 看 MLKEM 和链长），原池 21 个里只有 9 个够格；
#   不够格的 12 个拿掉（python / mysql / mongodb / redis / intel / amd / dell / bing / tesla / asus 链短，
#   vuejs.org 没 MLKEM，www.swift.com 没 h2），从 40 个大厂主站候选里补了实测全过的 10 个。
#   同日又测 45 个，过了 14 个，挑证书链最长（余量最大、不同行业）的 6 个补上，共 25 个。
#   同一个站换个地区的机房可能不一样，所以装的时候照样在本机探一遍（_sni_precheck_one / _xr_pq_new）。
# 安装时默认从这里随机挑一个（避免所有人都按回车挤在同一个 SNI 上被针对）；随机挑到的本机探不过会自己换（见 _sni_precheck_one）。
REALITY_SNI_POOL = [
    "www.cisco.com", "www.oracle.com", "www.ibm.com", "www.vmware.com", "www.qualcomm.com",
    "www.samsung.com", "academy.nvidia.com", "addons.mozilla.org", "react.dev",
    "www.nvidia.com", "developer.nvidia.com", "www.broadcom.com", "www.nxp.com", "www.analog.com",
    "www.docker.com", "www.atlassian.com", "www.mastercard.com", "www.ebay.com", "www.mozilla.org",
    "www.boeing.com", "www.airbus.com", "www.fedex.com", "www.schneider-electric.com", "www.3m.com", "www.infineon.com",
]

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
        return False, "连不上它的 443"
    tls13 = "TLSv1.3" in out and "Cipher is" in out
    h2 = "ALPN protocol: h2" in out
    if tls13 and h2:
        return True, "TLS1.3 + h2 ✓"
    if not tls13:
        return False, "TLS1.3 握手不成功（reality 要求目标支持 TLS1.3）"
    return False, "不支持 HTTP/2，reality 握手特征容易露"

def _pick_ok_sni(exclude=(), tries=6):
    """从内置池随机挑一个本机探得通的（连通 + TLS1.3 + h2），最多试 tries 个；都不通返回 None。"""
    pool = [x for x in REALITY_SNI_POOL if x not in set(exclude)]
    for cand in random.sample(pool, k=min(tries, len(pool))):
        if _reality_sni_ok(cand)[0]:
            return cand
    return None

def _sni_precheck_one(key="sni", avoid=""):
    """探一次 G[key]（"sni" = sing-box / "sni_xr" = xray 的伪装域名）够不够格给 reality 借用（不阻断安装）。
       回车随机挑的（G[key + "_auto"]）探不过：从池里另挑一个探得通的换上（避开 avoid = 另一个核心的）——
       那是脚本自己挑的，不该拿个不合格的装（仓库主人 10/11 撞上：随机到 updates.cdn-apple.com，从那台机器
       TLS1.3 握手不成功，旧版照装还说「按你填的继续装」）。池里同一个站在不同地区的机房表现不一，探过才算数。
       自己填的不替人换，一行提示；装好后可在「4 更换伪装域名」里换。"""
    if not G.get(key):
        return
    ok, detail = _reality_sni_ok(G[key])
    if ok:
        print(f"  reality 借用目标 {G[key]}: {detail}")
        return
    if G.get(key + "_auto"):
        new = _pick_ok_sni({G[key], avoid} - {""})
        if new:
            print(f"  ✔ reality 借用目标换成 {new}（随机挑到的 {G[key]} 从本机探不过：{detail}）")
            G[key] = new
            return
    ui_tip(f"reality 借用目标 {G[key]} 从本机探不过：{detail}；装好后可在「4 更换伪装域名」里换")

def precheck_sni(sb_names, xr_names):
    """选了 reality 类协议时，装前探一次借用目标（两个核心各一个的话各探各的）。选没选据注册表判断，不靠名字前缀。"""
    if proto_flag("reality", sb_names, xr_names):
        _sni_precheck_one("sni", avoid=G.get("sni_xr", ""))
    if G.get("sni_xr") and proto_flag("reality", (), xr_names):
        _sni_precheck_one("sni_xr", avoid=G["sni"])

def selfsigned_risky(sb_names, xr_names):
    """没域名时只能自签的协议：依赖证书的 TLS 类 + tuic。证书冒充借用的大站、却不是那家签的，
       主动探测握一下手就露馅，是封 IP 的头号特征（mack-a 没域名只给 reality）。
       tuic 没有混淆，握手证书一样探得到；hy2 有 salamander 混淆，探测拿不到握手，不在此列。"""
    return proto_pick("cert", sb_names, xr_names) + (["tuic"] if "tuic" in sb_names else [])

def warn_selfsigned(sb_names, xr_names):
    """无域名还是装了自签协议（安装向导里回了 n、或命令行直接装）：再提醒一行。"""
    if G["domain"]:
        return
    risky = selfsigned_risky(sb_names, xr_names)
    if risky:
        ui_tip(f"无域名：{'、'.join(risky)} 用自签证书，一被探测就露馅、容易封 IP；补个域名走真证书更稳")

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

# 链接里带凭据的形态：GitHub 私有仓库的 raw 链接、以及 release 资产 302 之后那种
# 带签名的对象存储地址，都是靠查询串里的 token 授权的。这类链接**不能交给公共反代**
# ——那等于把凭据原样送给第三方。自己人的中转不受限（那是自己的机器）。
# 典型来源：用户把自定义模板放在自己的私有仓库里，直连又不通。
_SECRET_Q = re.compile(r"[?&](token|access_token|key|sig|signature|x-amz-[a-z-]+)=", re.I)

def _has_secret(url):
    return bool(_SECRET_Q.search(url))

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
    _hy2_hop_persist(target_port, rng)

# 端口跳跃规则的兜底：sing-box 每次启动（含开机）都跑一遍，缺了就补、指错了就换。
# 只靠 netfilter-persistent 不够——实测有机器 save 静默没写进 rules.v4，一重启 DNAT 没了，
# hy2 整段跳跃端口全超时（单端口还通，所以很难看出来）。
HY2_HOP_SH     = "/usr/local/sbin/xy-hy2-hop"
HY2_HOP_DROPIN = "/etc/systemd/system/sing-box.service.d/xy-hy2-hop.conf"

def _hy2_hop_script(port, rng):
    lo, hi = rng.split("-")
    return f"""#!/bin/sh
# sing-box 启动时补回 hy2 端口跳跃 DNAT（UDP {lo}-{hi} -> :{port}）。由安装脚本生成，勿手改。
# hy2 已经不在 sing-box 配置里了（重装没选 hy2 等）就什么都不做，免得把整段 UDP 转给死端口
grep -Eq '"listen_port": *{port}([^0-9]|$)' {SB_DIR}/config.json 2>/dev/null || exit 0
for t in iptables ip6tables; do
  command -v $t >/dev/null 2>&1 || continue
  ok=0
  $t -w -t nat -S PREROUTING 2>/dev/null | grep -- xy_hy2_portHopping | while read -r l; do
    case "$l" in
      *"--dport {lo}:{hi} "*"--to-destination :{port}") ;;
      *) $t -w -t nat $(echo "$l" | sed 's/^-A /-D /') ;;
    esac
  done
  $t -w -t nat -C PREROUTING -p udp --dport {lo}:{hi} -m comment --comment xy_hy2_portHopping -j DNAT --to-destination :{port} 2>/dev/null || {{
    $t -w -t nat -A PREROUTING -p udp --dport {lo}:{hi} -m comment --comment xy_hy2_portHopping -j DNAT --to-destination :{port} && echo "added $t"
  }}
done
exit 0
"""

def _hy2_hop_persist(port, rng):
    """写兜底脚本 + sing-box 的 drop-in（ExecStartPost）。内容没变不动；变了才 daemon-reload。"""
    body = _hy2_hop_script(port, rng)
    unit = f"[Service]\nExecStartPost=-{HY2_HOP_SH}\n"
    changed = False
    try:
        for path, text, mode in ((HY2_HOP_SH, body, 0o700), (HY2_HOP_DROPIN, unit, 0o644)):
            try:
                same = open(path).read() == text
            except Exception:
                same = False
            if not same:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").write(text)
                changed = True
            os.chmod(path, mode)
    except Exception:
        return False
    if changed:
        sh("systemctl daemon-reload", check=False)
    return changed

def _hy2_hop_unpersist():
    """删 hy2 / 卸载时撤掉兜底，免得 sing-box 一启动又把 DNAT 加回来。"""
    gone = False
    for path in (HY2_HOP_DROPIN, HY2_HOP_SH):
        if os.path.exists(path):
            os.remove(path)
            gone = True
    try:
        os.rmdir(os.path.dirname(HY2_HOP_DROPIN))         # 空了才删得掉，别人的 drop-in 不动
    except Exception:
        pass
    if gone:
        sh("systemctl daemon-reload", check=False)

def _vmess_insec_heal():
    """老装机自愈：自签（没有域名）装出来的 vmess 链接缺「跳过证书校验」，补上并刷新订阅（token 不变）。
       只改本机自己的 vmess + TLS 链接（地址 = 本机 host），别的机器聚合过来的、CDN 的不碰。
       补了返回 True；有域名（真证书）或没有要补的，什么都不做。"""
    try:
        st = json.load(open(STATE_FILE))
        if st.get("domain"):
            return False
        # 节点地址看安装时记下的 host：订阅地址可能已换成证书域名（sub.host），节点链接仍是 IP
        host = st.get("host") or open(HOST_FILE).read().strip()
        lines = open(NODE_FILE).read().split("\n")
    except Exception:
        return False
    hit = False
    for i, l in enumerate(lines):
        if l.startswith("#"):
            break                                         # 「# 订阅链接:」往下不是节点
        if not l.startswith("vmess://"):
            continue
        try:
            b = l[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        except Exception:
            continue
        if j.get("tls") != "tls" or j.get("add") != host or j.get("allowInsecure"):
            continue
        j.update(_vmess_insec(True)); lines[i] = vmess_link(j); hit = True
    if not hit:
        return False
    open(NODE_FILE, "w").write("\n".join(lines))
    try:
        G["host"] = _host()               # 订阅地址保持原样（build_subscription 会把它写回 sub.host）
        build_subscription(read_saved_links(), new_token=False)
    except Exception:
        pass
    return True

def _relay_port_heal():
    """老装机自愈：配置里本机中转的地址跟现在的订阅端口 / 中转 token 对不上（重装时先生成、后换端口
       那个老 bug 留下的），就按当前模板重生成三格式（订阅 token、端口不变）。修了返回 True。"""
    try:
        pre = _ghrelay_prefix()
    except Exception:
        return False
    if not pre:
        return False
    host = pre.split("//", 1)[1].split(":", 1)[0]
    stale = False
    for meta in FMT.values():
        try:
            txt = open(meta["file"]).read()
        except OSError:
            continue
        # 配置里出现指向本机域名的中转地址、却不是当前这个前缀 → 端口或 token 是旧的
        for m in re.finditer(r"https://" + re.escape(host) + r":\d+/[^/\s\"']+/gh/", txt):
            if m.group(0) != pre:
                stale = True; break
        if stale:
            break
    if not stale or not read_saved_links():
        return False
    try:
        G["host"] = _host()
        build_subscription(read_saved_links(), new_token=False)
    except Exception:
        return False
    return True

def _hy2_hop_heal():
    """老装机自愈：sing-box 里有 hy2、跳跃没关，就补上兜底并当场跑一次。
       补回了缺的 DNAT 返回 True。没有 hy2 或跳跃关了什么都不做。"""
    try:
        cfg = json.load(open(f"{SB_DIR}/config.json"))
        port = next(ib["listen_port"] for ib in cfg.get("inbounds", [])
                    if ib.get("type") == "hysteria2" and ib.get("listen_port"))
    except StopIteration:
        _hy2_hop_unpersist()                              # 没有 hy2 了，兜底也撤掉
        return False
    except Exception:
        return False
    try:
        raw = json.load(open(STATE_FILE)).get("hy2_ports", "")
    except Exception:
        raw = ""
    if (raw or "").strip().lower() in ("off", "n", "no", "none"):
        _hy2_hop_unpersist()
        return False
    rng = raw.strip() if re.match(r"^\d+-\d+$", (raw or "").strip()) else HY2_PORTS
    _hy2_hop_persist(int(port), rng)
    out = sh(HY2_HOP_SH, check=False) if os.path.exists(HY2_HOP_SH) else ""
    if "added" in (out or ""):
        sh("netfilter-persistent save", check=False)
        return True
    return False

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

def _udp443_ok():
    """UDP 443 能不能给 tuic：没人占，或者占着的就是本机的 sing-box（重装 / 增删时它自己那个 tuic）。
       nginx / reality 都只用 TCP 443，UDP 443 平时是空的。"""
    for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            s = socket.socket(fam, socket.SOCK_DGRAM)
        except OSError:                                  # 机器没开 IPv6：建 socket 这一步就报 97，不算占用
            continue
        try:
            s.bind((addr, 443))
        except OSError as e:
            if fam == socket.AF_INET6 and e.errno in (97, 99):
                continue
            return "sing-box" in (sh("ss -ulpnH 'sport = :443'", check=False) or "")
        finally:
            s.close()
    return True

def sb_tuic(port, tag):
    uid = new_uuid(); pw = new_pw(); crt, key, insec = ensure_acme()
    # 有域名真证书就上 UDP 443（仓库主人 10/23）：同一个域名 TCP 443 是网站、UDP 443 是 h3，跟真网站开 HTTP/3 一模一样；
    # 运营商对 UDP 443 基本放行（YouTube / 谷歌的 QUIC 都走这），随机高端口的 UDP 在 4G 下最容易被限速丢包
    if G.get("domain") and not insec and _udp443_ok():
        port = 443
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
    lk = (f"anytls://{pw}@{G['host']}:{port}?sni={tls_host()}&fp=chrome"
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

def sb_vless_grpc_tls(port, tag):
    """VLESS + gRPC + TLS（自己的域名证书）。nginx 前置时挂 443 后面：本地明文 grpc，nginx 按服务名 grpc_pass 进来，
       跟 ws / xhttp-tls 同一个域名（仓库主人 10/23：随机端口的 grpc 容易被阻断，挂 nginx、不想多填伪装域名）。
       没有 nginx 前置时听自己的端口、自带证书。"""
    uid = new_uuid(); svc = "grpc" + secrets.token_hex(3)
    if _nginx_front():
        ib = {"type": "vless", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
              "users": [{"uuid": uid}], "transport": {"type": "grpc", "service_name": svc}}
        NGINX_WS.append({"path": "/" + svc, "port": port, "kind": "grpc"})
        lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=tls&sni={tls_host()}&fp=chrome"
              f"&alpn=h2&type=grpc&serviceName={svc}&mode=gun#{tag}")
        return ib, lk
    crt, key, insec = ensure_acme()
    ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"uuid": uid}], "transport": {"type": "grpc", "service_name": svc},
          "tls": {"enabled": True, "alpn": ["h2"], "certificate_path": crt, "key_path": key}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls&sni={tls_host()}&fp=chrome"
          f"&alpn=h2&type=grpc&serviceName={svc}&mode=gun&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

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
                  f"&sni={tls_host()}&fp=chrome&type={_LINK_NET[transport]}&host={tls_host()}"
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
              f"&sni={tls_host()}&fp=chrome&type={_LINK_NET[transport]}&host={tls_host()}"
              f"&path={path}&allowInsecure={1 if insec else 0}{smk}#{tag}")
        return ib, lk
    return b

# vmess 的分享链接是 base64 JSON，没有 ?insecure= 那种查询参数，自签时「跳过证书校验」要写进 JSON 里。
# 以前漏了这一项：其它协议自签都带 insecure=1，唯独 vmess 没带，客户端拿自签证书去校验 →
# 「certificate relies on legacy Common Name field」握手失败，三个 vmess 节点在自签机上全部超时。
# 字段名用 v2rayN 系通行的 allowInsecure；link_to_proxy 读它，订阅三格式都跟着走。
def _vmess_insec(insec):
    return {"allowInsecure": "1"} if insec else {}

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
                             "tls": "tls", "sni": tls_host(), "fp": "chrome", **smk})
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
                         "tls": "tls", "sni": tls_host(), "fp": "chrome", **smk, **_vmess_insec(insec)})
        return ib, lk
    return b

def sb_trojan(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "trojan", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}],
          "tls": {"enabled": True, "server_name": tls_host(),
                  "certificate_path": crt, "key_path": key}}
    if not insec and _site_fallback_ok():
        # 不是 trojan 的连接回落到本机伪装站（见 ensure_site_fallback）；TLS 没设 alpn，浏览器走 http/1.1，正好对上明文站
        ensure_site_fallback()
        ib["fallback"] = {"server": "127.0.0.1", "server_port": SITE_FB_PORT}
    lk = (f"trojan://{pw}@{G['host']}:{port}?security=tls&sni={tls_host()}&fp=chrome"
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

# 前 10 个对齐 mack-a 的输出，顺序也一致；grpc-tls 是 10/23 加的（grpc 挂 nginx 443、用自己的域名）。
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
#        ws_front=True  nginx 前置时藏在 443 后面（增删后按配置重建 nginx，见 _nginx_sync_from_cfg）
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
#        shadowrocket_line() mihomo 节点 -> 小火箭行（不支持的返回 None 自动跳过：量子加密）
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

_SNI_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")

def ask_reality_sni(label, default="", avoid=""):
    """问一个 reality 伪装域名 → (域名, 是不是随机挑的)。
       default 给了：回车沿用它；没给：回车从池里随机挑一个（避开 avoid）。
       填的跟 avoid（另一个核心的伪装域名）一样 → 红字说重复、回到这一问重填（仓库主人 10/22：sing-box、xray 各一个，
       sni-split 下 nginx 按伪装域名分流，同一个分不开）；格式不对也回来重填。"""
    rand = secrets.choice([x for x in REALITY_SNI_POOL if x != avoid] or REALITY_SNI_POOL)
    hint = f"回车沿用当前的 {default}" if default else f"回车=随机挑，本次随机到 {rand}"
    while True:
        v = _ask(f"{label}（{hint}）: ").strip().lower().rstrip(".")
        if v and not _SNI_RE.match(v):
            print(_hl("1;31", f"  ✗ {v} 不是域名格式，重填")); continue
        sni = v or default or rand
        if avoid and sni == avoid:
            print(_hl("1;31", f"  ✗ {sni} 跟另一个核心的伪装域名重复了，重填（回车随机挑）"))
            default = ""; hint = f"回车=随机挑，本次随机到 {rand}"
            continue
        return sni, ("1" if not v and not default else "")

def _asks_reality(mode):
    """选了 sing-box 的 reality：增量添加时确认借用域名（跟 xray 的不能重复）并预检。
       全新安装时伪装域名在安装向导里统一问（没有域名时 ws/trojan 也拿它当伪装 Host，见 tls_host()）。"""
    if mode != "add":
        return
    other, have = _current_sni("xray"), _current_sni("sb")
    if have and have != other:                   # 这个核心已有 reality：一个核心一个伪装域名，沿用，不再问
        G["sni"] = have
        print(f"  sing-box 的 reality 伪装域名沿用 {have}")
        return
    cur = G["sni"] if G["sni"] != other else ""
    G["sni"], G["sni_auto"] = ask_reality_sni("\n  sing-box 的 reality 伪装域名", cur, other)
    _sni_precheck_one("sni", avoid=other)

def _asks_reality_xr(mode):
    """选了 xray 的 reality：同上，问 xray 自己那个（跟 sing-box 的不能重复）。"""
    if mode != "add":
        return
    other, have = _current_sni("sb"), _current_sni("xray")
    if have and have != other:                   # 同上：xray 已有 reality 就沿用它那个
        G["sni_xr"] = have
        print(f"  xray 的 reality 伪装域名沿用 {have}")
        return
    cur = have or G.get("sni_xr", "")
    cur = cur if cur != other else ""
    G["sni_xr"], G["sni_xr_auto"] = ask_reality_sni("\n  xray 的 reality 伪装域名", cur, other)
    _sni_precheck_one("sni_xr", avoid=other)

def _recap_reality():
    return ("sing-box 伪装域名", G["sni"])

def _recap_reality_xr():
    return ("xray 伪装域名", _xr_sni())

# ── 表本体 ───────────────────────────────────────────────────────────────
# 前 10 个对齐 mack-a 的输出，顺序也一致；grpc-tls 是 10/23 加的（grpc 挂 nginx 443、用自己的域名）。
# 想加回其它协议：把下面「备用」块里对应行搬进 SB 即可——builder 都还在，没删。
SB = {
    "vless-vision":      PROTO(sb_vless_vision, cert=True),
    "vless-ws":          PROTO(make_sb_vless("ws"), cert=True, ws_front=True,
                               asks=_asks_smux, recap=_recap_smux),
    "vmess-ws":          PROTO(make_sb_vmess("ws"), cert=True, ws_front=True,
                               asks=_asks_smux, recap=_recap_smux),
    "trojan":            PROTO(sb_trojan, cert=True),
    "hy2":               PROTO(sb_hysteria2, udp=True,
                               teardown=_td_hy2, asks=_asks_hy2, recap=_recap_hy2),
    "reality-vision":    PROTO(sb_reality_vision, reality=True,
                               asks=_asks_reality, recap=_recap_reality),
    "tuic":              PROTO(sb_tuic, udp=True),
    "vmess-httpupgrade": PROTO(make_sb_vmess("httpupgrade"), cert=True, ws_front=True,
                               asks=_asks_smux, recap=_recap_smux),
    "anytls":            PROTO(sb_anytls, cert=True),
    "grpc-tls":          PROTO(sb_vless_grpc_tls, cert=True, ws_front=True),
}
# 备用（以后想加回，取消注释挪进上面的 SB，按上面的清单据实填标志位）：
#   reality-grpc：仓库主人 10/23 点头停用——随机端口的 grpc 两台机器都被阻断（Timeout）；grpc 有 grpc-tls（挂 nginx 443）
#   和 xray 的 reality-grpc（挂 reality 入口 443）。已经装了的机器照常跑、「删除协议」里照样能删，只是新装 / 添加不再列。
#   "reality-grpc":      PROTO(sb_reality_grpc, reality=True,
#                              asks=_asks_reality, recap=_recap_reality),
#   "ss2022":            PROTO(sb_ss2022),
#   "vless-h2":          PROTO(make_sb_vless("h2"), cert=True),
#   "vless-httpupgrade": PROTO(make_sb_vless("httpupgrade"), cert=True, ws_front=True,
#                              asks=_asks_smux, recap=_recap_smux),
#   "vmess-h2":          PROTO(make_sb_vmess("h2"), cert=True),
#   "socks5":            PROTO(sb_socks5),
#   "naive": sb_naive,
#   "shadowtls": sb_shadowtls,

# ============================================================================
# xray 协议表 —— builder 返回 (inbound_dict, share_link)
# ============================================================================
# xray reality 跟 mack-a 对齐的两项（仓库主人 10/11：「抗量子签名、最低客户端版本 1.8.2 这个我建议做」）。
# sing-box 内核的 reality 没有这两个选项，不涉及。
#  · minClientVer 1.8.2：上报版本比它低的客户端拒掉。实测（xray 26.9.30 服务端开 show 看上报值）：xray 客户端报自己的
#    版本（26.x），mihomo / Clash 系固定报 1.8.2，正好放行；sing-box 系和不带 X25519MLKEM768 的 mihomo 本来就连不上
#    xray 的 reality（见 _local_xray_reality），跟这项无关。早先写 1.0.0 是防 xray 26.7.11+ 默认的 26.3.27 把 mihomo
#    拒掉（#133），1.8.2 一样防得住。
#  · ML-DSA-65 抗量子签名（mldsa65Seed；链接带 pqv= 验证公钥）：服务端给 reality 的临时证书多签一道抗量子签名，
#    链接带 pqv 的 xray 客户端会验，防以后有量子计算机的人冒充服务端；不验的客户端（mihomo、小火箭配置）照常连。
#    代价是临时证书多出 3309 字节，借用目标站的证书链得比它长才「藏」得住——不是指纹问题，是连通问题。本机实测：
#    开了以后目标站证书链 3457 字节时所有客户端都握不上手、3519 字节能连（叶子证书是 ECDSA 的最吃亏情况）；
#    目标站有没有 X25519MLKEM768 不影响连通。所以：目标站支持 X25519MLKEM768（同 mack-a）、且带 SNI 握手拿到的证书链
#    > 4000 字节才开——mack-a 是 > 3500、量的还是不带 SNI 那张默认证书，太贴边。开了以后每天 03:20 复查，目标站换成
#    短证书链（< 3600）就自动关掉（见 _xray_pq_health），免得 reality 全断。
#    服务端开着、链接里没 pqv 的客户端照常连；反过来链接带 pqv、服务端没开，xray 客户端会连不上——所以开 / 关都要
#    同时改链接（_links_set_pqv）。pqv 有 2600 多字符，带它的链接二维码扫不了，订阅不受影响。
XR_MIN_CLIENT_VER = "1.8.2"
PQ_ON_MIN_CHAIN, PQ_OFF_BELOW = 4000, 3600

def _xr_sni():
    """xray reality 的借用域名。仓库主人 10/22：sing-box、xray 各填一个、不许重复——sni-split 下 nginx 按借用域名
       分流，两个核心同一个域名分不出该给谁。老装机没有第二个，沿用 sing-box 那个。"""
    return G.get("sni_xr") or G["sni"]

def _xr_behind_split():
    """xray reality-vision 挂到 nginx 443 分流后面：开着 sni-split、而且 xray 的借用域名跟 sing-box 不一样才行
       （一样的话 nginx 分不开，留在自己端口，跟以前一样）。"""
    return bool(G.get("sni_split")) and _xr_sni() != G["sni"]

def _xr_reality_stream(priv, sid, network, extra=None, seed=""):
    st = {"network": network, "security": "reality",
          "realitySettings": {"show": False, "dest": f"{_xr_sni()}:443",
                              "xver": 0, "serverNames": [_xr_sni()],
                              "privateKey": priv, "shortIds": [sid],
                              "minClientVer": XR_MIN_CLIENT_VER,
                              # 客户端时间跟服务器差 70 秒以上的握手拒掉：防录下来的握手被重放探测（mack-a 同值）
                              "maxTimeDiff": 70000}}
    if seed:
        st["realitySettings"]["mldsa65Seed"] = seed
    if extra:
        st.update(extra)
    return st

def _pq_ping_parse(out):
    """`xray tls ping` 的输出 → (证书链总长 或 None, 有没有 X25519MLKEM768)。只看「Pinging with SNI」那段：
       reality 客户端握手都带 SNI，不带 SNI 那次拿到的可能是站点的默认证书，长短不一样。握手没成功返回 (None, False)。"""
    s = out.split("Pinging with SNI", 1)
    if len(s) < 2:
        return None, False
    s = s[1].split("-------------------", 1)[0]
    if "Handshake succeeded" not in s:
        return None, False
    m = re.search(r"Certificate chain's total length:\s*(\d+)", s)
    return (int(m.group(1)) if m else None), "X25519MLKEM768" in s

_PQ_PING = {}

def _reality_pq_ping(sni):
    """从本机 xray tls ping 一次借用目标站（同一次运行里同一个站只 ping 一次）。探不了返回 (None, False)。"""
    if sni not in _PQ_PING:
        try:
            r = subprocess.run([XRAY_BIN, "tls", "ping", f"{sni}:443"], capture_output=True, text=True, timeout=25)
            _PQ_PING[sni] = _pq_ping_parse(r.stdout + r.stderr)
        except Exception:
            _PQ_PING[sni] = (None, False)
    return _PQ_PING[sni]

def _reality_pq_ok(sni):
    """借用目标站够不够格开 ML-DSA-65（见 XR_MIN_CLIENT_VER 那段注释）。"""
    n, mlkem = _reality_pq_ping(sni)
    return bool(mlkem and n and n > PQ_ON_MIN_CHAIN)

def _reality_pq_why(sni):
    """目标站不够格开抗量子签名的原因，接在「借用目标站」后面念（屏上用）。"""
    n, mlkem = _reality_pq_ping(sni)
    if n is None:
        return "从本机探不到证书"
    if n <= PQ_ON_MIN_CHAIN:                             # 链太短是会断线的那个原因，先说它
        return f"证书链只有 {n} 字节（要 > {PQ_ON_MIN_CHAIN}）"
    return "不支持 X25519MLKEM768"

def _xr_pq_keys():
    """新生成一对 ML-DSA-65：(种子, 验证公钥)。种子只进服务端配置，验证公钥进链接（pqv=）。"""
    out = sh(f"{XRAY_BIN} mldsa65").splitlines()
    return out[0].split(":")[-1].strip(), out[1].split(":")[-1].strip()

_PQ_SAID = set()

def _xr_pq_new():
    """新建 xray reality 入站时用：借用目标站够格就生成一对 (种子, 验证公钥)，不够格 / 生成失败返回 ("", "")。
       同一个目标站只在屏上说一次开没开。"""
    sni = _xr_sni()
    ok = _reality_pq_ok(sni)
    if sni not in _PQ_SAID:
        _PQ_SAID.add(sni)
        print(f"  ✔ 抗量子签名已开（借用目标站证书链 {_reality_pq_ping(sni)[0]} 字节）" if ok
              else f"  抗量子签名没开：借用目标站{_reality_pq_why(sni)}")
    if not ok:
        return "", ""
    try:
        return _xr_pq_keys()
    except Exception:
        return "", ""

def _pqv(v):
    return f"&pqv={v}" if v else ""

# ── xray reality 入口（sni-split 下）：vision² / grpc² / xhttp² 共用 443 上一个 reality 入口 ──
# nginx stream 只能按伪装域名分流，三个同一个伪装域名分不开（仓库主人 10/22 要都上 443、又不想多填域名）。
# 做法：reality-vision² 那个入站当入口，vision 用户直接在它里面；不是 vision 的连接（grpc / xhttp 都是 h2）
# 解开 reality 后走 fallbacks 回落到 nginx 本地 h2c（REALITY_FB_PORT），按服务名 / 路径分给本地明文的
# grpc² / xhttp² 入站。REALITY 解开后拿不到 ALPN（realAlpn 为空），所以回落不能按 alpn=h2 匹配，只能写默认回落。
# 删掉 vision² 时入口不能跟着没：把它改名成 XR_ENTRY_TAG、用户清空，留着给 grpc² / xhttp²（实测零用户的
# 入口照样回落）；三个都删了入口才拆。再加回 vision² 时用户并回入口。见 _xr_entry_fixup。
XR_ENTRY_TAG = "reality-entry"
_XR_FB_PROTOS = ("reality-grpc", "reality-xhttp")
_XR_ENTRY = {}
_XR_ENTRY_FROM_DISK = [True]        # 全新安装（run）不沿用磁盘上旧配置的密钥

def _xr_is_entry(ib):
    st = ib.get("streamSettings") or {}
    return (st.get("security") == "reality" and str(ib.get("listen", "")) == "127.0.0.1"
            and (ib.get("tag") == XR_ENTRY_TAG or _ib_proto(ib) == "reality-vision"))

def _xr_is_fb_backed(ib):
    """挂在 reality 入口后面的 grpc² / xhttp²：本地、明文、协议名对得上。"""
    st = ib.get("streamSettings") or {}
    return (_ib_proto(ib) in _XR_FB_PROTOS and str(ib.get("listen", "")) == "127.0.0.1"
            and st.get("security") in (None, "", "none"))

def _xr_entry_keys():
    """reality 入口的一套 (私钥, 公钥, shortId, ML-DSA 种子, pqv)。已有入口（增量添加）就从它的配置里取，
       公钥 / pqv 用 xray 从私钥 / 种子算回来；没有就新生成。同一次运行里只算一次。"""
    if _XR_ENTRY.get("k"):
        return _XR_ENTRY["k"]
    k = None
    if _XR_ENTRY_FROM_DISK[0]:
        for ib in _load_core_cfg("xray")[1]:
            if _xr_is_entry(ib):
                rs = ib["streamSettings"].get("realitySettings") or {}
                try:
                    pub = [l for l in sh(f"{XRAY_BIN} x25519 -i {rs['privateKey']}").splitlines()
                           if "PublicKey" in l][0].split(":")[-1].strip()
                    seed = rs.get("mldsa65Seed", "")
                    pqv = ([l for l in sh(f"{XRAY_BIN} mldsa65 -i {seed}").splitlines()
                            if l.startswith("Verify")][0].split(":")[-1].strip() if seed else "")
                    k = (rs["privateKey"], pub, (rs.get("shortIds") or [""])[0], seed, pqv)
                except Exception:
                    k = None
                break
    if not k:
        priv, pub = reality_keys(XRAY_BIN, "x25519")
        seed, pqv = _xr_pq_new()
        k = (priv, pub, short_id(), seed, pqv)
    _XR_ENTRY["k"] = k
    return k

def _xr_entry_fixup(inbounds, removed_vision=None):
    """让 xray 入站表里的 reality 入口跟挂在它后面的 grpc² / xhttp² 对得上，返回新的入站表：
       有后端没入口 → 建一个零用户入口；有入口没后端 → 去掉回落（零用户的入口直接拆）；
       新加的 vision² 碰上已有的零用户入口 → 用户并进入口；removed_vision（这次删掉的 vision² 入站）
       后面还挂着后端 → 改名成零用户入口留下。"""
    ibs = list(inbounds)
    backed = [ib for ib in ibs if _xr_is_fb_backed(ib)]
    if removed_vision is not None and backed and not any(_xr_is_entry(ib) for ib in ibs):
        ent = dict(removed_vision, tag=XR_ENTRY_TAG)
        ent["settings"] = dict(ent.get("settings") or {}, clients=[])
        ibs.append(ent)
    entries = [ib for ib in ibs if _xr_is_entry(ib)]
    hidden = [ib for ib in entries if ib.get("tag") == XR_ENTRY_TAG]
    vision = [ib for ib in entries if ib.get("tag") != XR_ENTRY_TAG]
    if hidden and vision:                               # 加回 vision²：用户并进入口，入口改回 vision² 的名字
        ent, new = hidden[0], vision[0]
        ent["settings"] = dict(ent.get("settings") or {},
                               clients=(new.get("settings") or {}).get("clients") or [])
        ent["tag"] = new.get("tag")
        ibs = [ib for ib in ibs if ib is not new]
        entries = [ent]
    if backed and not entries:
        priv, pub, sid, seed, pqv = _xr_entry_keys()
        ent = {"listen": "127.0.0.1", "port": next_port(), "protocol": "vless", "tag": XR_ENTRY_TAG,
               "settings": {"clients": [], "decryption": "none"},
               "streamSettings": _xr_reality_stream(priv, sid, "raw", seed=seed)}
        ibs.append(ent)
        entries = [ent]
    for ent in entries:
        st = ent.setdefault("settings", {})
        if backed:
            st["fallbacks"] = [{"dest": REALITY_FB_PORT}]
        else:
            st.pop("fallbacks", None)
    if not backed:
        ibs = [ib for ib in ibs if ib.get("tag") != XR_ENTRY_TAG]
    return ibs

def _xr_link_uuids(data, ib):
    """这个 xray 入站对应哪些分享链接（按 UUID）：自己的用户；reality 入口再加上挂在它后面的 grpc² / xhttp²
       ——它们的链接用的是入口的伪装域名 / 公钥 / pqv，换伪装域名、开关抗量子签名要一起改。"""
    out = [str(c.get("id")) for c in (ib.get("settings") or {}).get("clients") or [] if c.get("id")]
    if _xr_is_entry(ib) and (ib.get("settings") or {}).get("fallbacks"):   # 只认 reality 入口：trojan² 也带回落（回伪装站）
        for b in data.get("inbounds", []):
            if _xr_is_fb_backed(b):
                out.append(_xr_ib_uuid(b))
    return [u for u in out if u]

def _nginx_fb_server():
    """reality 入口回落进来的本地 h2c：按 grpc 服务名 / xhttp 路径分给本地明文入站。没有后端返回 ""。"""
    locs = ""
    for ib in _load_core_cfg("xray")[1]:
        if not _xr_is_fb_backed(ib):
            continue
        st = ib.get("streamSettings") or {}
        if st.get("network") == "grpc":
            loc = "/" + (st.get("grpcSettings") or {}).get("serviceName", "").strip("/") + "/"
        else:
            loc = (st.get("xhttpSettings") or {}).get("path", "").rstrip("/") + "/"
        if len(loc) < 3:
            continue
        locs += (f"  location {loc} {{\n    client_max_body_size 0;\n    client_body_timeout 1h;\n"
                 f"    grpc_read_timeout 1h;\n    grpc_send_timeout 1h;\n"
                 f"    grpc_pass grpc://127.0.0.1:{_cfg_port(ib)};\n  }}\n")
    if not locs:
        return ""
    return ("server {\n" + nginx_http2_listen(f"127.0.0.1:{REALITY_FB_PORT}") + nginx_http2_on() +
            locs + "  location / {\n    return 404;\n  }\n}\n")

def xr_reality_vision(port, tag):
    uid = new_uuid()
    # sni-split：跟 sing-box 的 reality-vision 一样挂 nginx 443 后面，nginx 按 xray 自己的借用域名转进来（仓库主人 10/22）；
    # 它同时是 reality-grpc² / reality-xhttp² 的入口，三个共用一套 reality 密钥（见 _xr_entry_keys）
    split = _xr_behind_split()
    if split:
        priv, pub, sid, seed, pqv = _xr_entry_keys()
    else:
        sid = short_id()
        priv, pub = reality_keys(XRAY_BIN, "x25519")
        seed, pqv = _xr_pq_new()
    ib = {"listen": "127.0.0.1" if split else "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}],
                       "decryption": "none"},
          "streamSettings": _xr_reality_stream(priv, sid, "raw", seed=seed)}
    if split:
        NGINX_STREAM.append({"sni": _xr_sni(), "port": port})
    lk = (f"vless://{uid}@{G['host']}:{443 if split else port}?encryption=none&flow=xtls-rprx-vision"
          f"&security=reality&sni={_xr_sni()}&fp=chrome&pbk={pub}&sid={sid}&type=tcp{_pqv(pqv)}#{tag}")
    return ib, lk

def xr_reality_grpc(port, tag):
    uid = new_uuid(); sid = short_id(); svc = "grpc" + secrets.token_hex(2)
    if _xr_behind_split():
        # 挂在 reality 入口后面：本地明文 grpc，入口解开 reality 后回落给 nginx h2c，按服务名转进来（仓库主人 10/22）
        priv, pub, sid, seed, pqv = _xr_entry_keys()
        ib = {"listen": "127.0.0.1", "port": port, "protocol": "vless", "tag": tag,
              "settings": {"clients": [{"id": uid}], "decryption": "none"},
              "streamSettings": {"network": "grpc", "grpcSettings": {"serviceName": svc, "multiMode": True}}}
        lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=reality"
              f"&sni={_xr_sni()}&fp=chrome&pbk={pub}&sid={sid}&type=grpc"
              f"&serviceName={svc}&mode=multi{_pqv(pqv)}#{tag}")
        return ib, lk
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    seed, pqv = _xr_pq_new()
    st = _xr_reality_stream(priv, sid, "grpc",
                            {"grpcSettings": {"serviceName": svc, "multiMode": True}}, seed=seed)
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": st}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={_xr_sni()}&fp=chrome&pbk={pub}&sid={sid}&type=grpc"
          f"&serviceName={svc}&mode=multi{_pqv(pqv)}#{tag}")
    return ib, lk

def xr_reality_xhttp(port, tag):
    uid = new_uuid(); sid = short_id(); path = "/" + secrets.token_hex(3)
    if _xr_behind_split():
        # 同 reality-grpc：挂在 reality 入口后面，本地明文 xhttp（h2c），nginx 按路径转进来
        priv, pub, sid, seed, pqv = _xr_entry_keys()
        ib = {"listen": "127.0.0.1", "port": port, "protocol": "vless", "tag": tag,
              "settings": {"clients": [{"id": uid}], "decryption": "none"},
              "streamSettings": {"network": "xhttp", "xhttpSettings": {"path": path}}}
        lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=reality"
              f"&sni={_xr_sni()}&fp=chrome&pbk={pub}&sid={sid}&type=xhttp"
              f"&host={_xr_sni()}&path={path}{_pqv(pqv)}#{tag}")
        return ib, lk
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    seed, pqv = _xr_pq_new()
    st = _xr_reality_stream(priv, sid, "xhttp",
                            {"xhttpSettings": {"path": path}}, seed=seed)
    # xhttp 传输不支持 xtls-rprx-vision flow（那是 raw/tcp 专属），客户端也没带 flow，
    # 服务端这里若强设 vision flow 会导致握手对不上 → 连不上，所以留空。
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": st}
    # host 显式带上、并与 sni 保持一致：mihomo 留空时会回退到 servername，结果一样，
    # 但写出来就不依赖客户端的回退实现，换客户端/版本也不会变。
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={_xr_sni()}&fp=chrome&pbk={pub}&sid={sid}&type=xhttp"
          f"&host={_xr_sni()}&path={path}{_pqv(pqv)}#{tag}")
    return ib, lk

def _xr_tls(certfile, keyfile):
    """xray TLS 入站的证书段。对齐 mack-a 的 Vision / XHTTP-TLS：TLS 最低 1.2；
       用的是 acme 真证书时开 rejectUnknownSni——拿 IP 扫端口、SNI 不对的一律握手拒绝，
       扫描器拿不到证书，也就看不到域名。自签证书只有 CN、没有 SAN，xray 按 SAN 比对会把
       正常客户端也拒掉，所以自签不开。"""
    tls = {"minVersion": "1.2",
           "certificates": [{"certificateFile": certfile, "keyFile": keyfile}]}
    if certfile == ACME_CRT:
        tls["rejectUnknownSni"] = True
    return tls

def xr_vless_ws(port, tag):
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    if _nginx_front():
        # nginx 前置：跟 sing-box 的 ws 一样藏 443 后面，本地明文口、TLS 由 nginx 终结、按 path 反代（仓库主人 10/22）
        ib = {"listen": "127.0.0.1", "port": port, "protocol": "vless", "tag": tag,
              "settings": {"clients": [{"id": uid}], "decryption": "none"},
              "streamSettings": {"network": "ws", "wsSettings": {"path": path}}}
        NGINX_WS.append({"path": path, "port": port})
        lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=tls"
              f"&sni={tls_host()}&fp=chrome&type=ws&host={tls_host()}&path={path}#{tag}")
        return ib, lk
    crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid}], "decryption": "none"},
          "streamSettings": {"network": "ws", "security": "tls",
                             "wsSettings": {"path": path},
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls"
          f"&sni={tls_host()}&fp=chrome&type=ws&host={tls_host()}&path={path}"
          f"&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

def xr_vmess_ws(port, tag):
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    if _nginx_front():                                  # 同 xr_vless_ws：藏 nginx 443 后面
        ib = {"listen": "127.0.0.1", "port": port, "protocol": "vmess", "tag": tag,
              "settings": {"clients": [{"id": uid, "alterId": 0}]},
              "streamSettings": {"network": "ws", "wsSettings": {"path": path}}}
        NGINX_WS.append({"path": path, "port": port})
        lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": "443",
                         "id": uid, "aid": "0", "net": "ws", "type": "none",
                         "host": tls_host(), "path": path, "tls": "tls", "sni": tls_host(), "fp": "chrome"})
        return ib, lk
    crt, key, insec = ensure_acme()
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vmess", "tag": tag,
          "settings": {"clients": [{"id": uid, "alterId": 0}]},
          "streamSettings": {"network": "ws", "security": "tls",
                             "wsSettings": {"path": path},
                             "tlsSettings": _xr_tls(crt, key)}}
    lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": str(port),
                     "id": uid, "aid": "0", "net": "ws", "type": "none",
                     "host": tls_host(), "path": path, "tls": "tls", "sni": tls_host(),
                     "fp": "chrome", **_vmess_insec(insec)})
    return ib, lk

def xr_vless_xhttp_tls(port, tag):
    """VLESS + XHTTP + TLS：nginx 前置时挂在 443 后面（本地明文 h2c，nginx grpc_pass 进来，仓库主人 10/22），
       否则独立随机端口、自带证书。

       跟 reality-xhttp 的区别只在伪装方式：reality 借别人的站，这个用自己的
       域名证书。好处是不依赖借用目标站的存活/可达性，也不受 reality 那套
       「借用站必须支持 TLS1.3+H2、不能是 CDN」的挑剔；代价是暴露自己的域名。

       为什么不照搬 mack-a 的「任意门」两层结构（dokodemo-door 公网口 →
       127.0.0.1:45988 的真入站）：那是为了在他那套【单配置多入站 + 复杂分流】
       里给这个入站单独挂一条绕过分流的路由（inboundTag → z_direct_outbound）。
       本脚本每个入站各自独立、出站只有 direct/block，中间这一跳不产生任何
       区别，只多一次本地转发和一条路由规则，所以直接监听公网口。"""
    uid = new_uuid(); path = "/" + secrets.token_hex(3)
    if _nginx_front():
        # nginx 前置：藏 443 后面（仓库主人 10/22）。xray 听本地明文 h2c，nginx 终结 TLS、grpc_pass 转进来
        # （见 _nginx_ws_locations 的 xhttp 分支）；链接照样 TLS + h2 连 443
        ib = {"listen": "127.0.0.1", "port": port, "protocol": "vless", "tag": tag,
              "settings": {"clients": [{"id": uid}], "decryption": "none"},
              "streamSettings": {"network": "xhttp",
                                 "xhttpSettings": {"host": tls_host(), "path": path, "mode": "auto"}}}
        NGINX_WS.append({"path": path, "port": port, "kind": "xhttp"})
        lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=tls"
              f"&sni={tls_host()}&fp=chrome&type=xhttp&host={tls_host()}&path={path}"
              f"&mode=auto&alpn=h2#{tag}")
        return ib, lk
    crt, key, insec = ensure_acme()
    tls = _xr_tls(crt, key)                         # 含 minVersion 1.2；真证书时 rejectUnknownSni
    tls["serverName"] = tls_host()
    tls["alpn"] = ["h2", "http/1.1"]
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
    if not insec and _site_fallback_ok():
        # 不是 trojan 的连接回落到本机伪装站（见 ensure_site_fallback）。TLS 只报 http/1.1：xray 默认会跟浏览器谈成 h2，
        # 回落过去的就是 h2 帧，明文站读不懂、回 400，反倒露馅
        ensure_site_fallback()
        ib["settings"]["fallbacks"] = [{"dest": SITE_FB_PORT}]
        ib["streamSettings"]["tlsSettings"]["alpn"] = ["http/1.1"]
    lk = (f"trojan://{pw}@{G['host']}:{port}?security=tls&sni={tls_host()}&fp=chrome"
          f"&type=tcp&allowInsecure={1 if insec else 0}#{tag}")
    return ib, lk

# xray 的 reality 三兄弟原来叫 vless-reality-*，三段名在手机客户端里显示不下、
# 后半截被截掉（vless-reality-x…）。vless- 是冗余的——这几个本来就都是 vless，
# sing-box 那边同样的东西就叫 reality-vision/grpc。统一砍成两段。
XRAY = {
    "reality-vision": PROTO(xr_reality_vision, reality=True,
                            asks=_asks_reality_xr, recap=_recap_reality_xr),
    "reality-grpc":   PROTO(xr_reality_grpc, reality=True,
                            asks=_asks_reality_xr, recap=_recap_reality_xr),
    "reality-xhttp":  PROTO(xr_reality_xhttp, reality=True,
                            asks=_asks_reality_xr, recap=_recap_reality_xr),
    # xray 的 ws / xhttp-tls：nginx 前置时跟 sing-box 的 ws 一样藏 443 后面（仓库主人 10/22），否则听自己的端口、
    # 自己终结 TLS；无域名时退化成自签+allowInsecure → cert=True。trojan 不走 HTTP 路径，留在自己端口。
    "xhttp-tls":      PROTO(xr_vless_xhttp_tls, cert=True, ws_front=True),
    "vless-ws":       PROTO(xr_vless_ws, cert=True, ws_front=True),
    "vmess-ws":       PROTO(xr_vmess_ws, cert=True, ws_front=True),
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
        # UDP 走 xudp（对齐 mack-a v3.5.26「为 Reality Vision 节点添加 xudp 编码」，仓库主人 10/20 要加）：
        # sing-box 订阅早就是 packet_encoding xudp（mihomo_to_sb_outbound），小火箭不写也默认 xudp，只有 mihomo 缺。
        # mack-a 只加 reality-vision，这里跟 sing-box 订阅一样所有 vless 都加——两个核心的 vless 入站都认 xudp
        d["packet-encoding"] = "xudp"
        # VLESS Encryption（CDN量子加密）：mihomo 1.19.13+ 认 encryption 字段；"none" 等于没开，不写
        if qs.get("encryption", "none") not in ("", "none"): d["encryption"] = qs["encryption"]
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
        bw = hy2_bw()                                                 # 满速（Brutal）：开了才写，见 hy2_bw
        if bw:
            d["up"], d["down"] = f"{bw[0]} Mbps", f"{bw[1]} Mbps"
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
        d["client-fingerprint"] = qs.get("fp", "chrome")   # 跟 vless / trojan / vmess 一样，不用 Go 自带指纹
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
        # TLS 指纹：跟 vless 一样默认 chrome。以前 vmess 没带，mihomo 就用 Go 自带的 ClientHello——
        # 这个指纹墙认得，同样走 nginx 443，vless-ws 通、vmess-ws / httpupgrade 被掐成超时。
        # 老链接没有 fp 字段也按 chrome 补，订阅下次生成就带上，不用重装。
        if j.get("tls") == "tls": d["client-fingerprint"] = j.get("fp") or "chrome"
        if j.get("tls") == "tls" and any(str(j.get(k, "")).lower() in ("1", "true")
                                         for k in ("allowInsecure", "insecure", "skip-cert-verify")):
            d["skip-cert-verify"] = "true"
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
        if not _has_secret(url):                        # 带凭据的链接不交给第三方
            urls += [pfx + url for pfx in GH_MIRRORS]
    return urls

def fetch_url(url):
    """带重试 + 镜像兜底的拉取，缓解 GitHub 429 限流。"""
    last = None
    for rd in range(2):                                 # 两轮，轮间退避
        for u in _mirrors(url):
            try:
                req = urllib.request.Request(u, headers={"User-Agent": HTTP_UA})
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
    harden_perms()                # 刚落地是 0644，当场收紧，别留窗口

# 只该 root 看的东西。默认 umask 下 os.makedirs / open 造出来的是 0755 / 0644 ——
# /etc/bgpeer 里存着三个格式的订阅 token、中转 token、安装参数；/etc/bgpeer/sub 里
# 连【文件名】都是 token；/etc/ssl/sb 里是 TLS 私钥。这台机器上任何非 root 进程
# （nginx 的 www-data、Docker 里的 openlist / emby、AdGuard）都读得到。订阅 token
# 一漏，别人就能拉走你全部节点的地址、UUID 和密码。
#
# 收紧是安全的：读这些的全是 root —— sing-box / xray / nginx 主进程 / xy-sub /
# AdGuard 都以 root 跑，units 里没有 User=。
#
# 【不要图省事改成全局 umask 077】试过就知道会炸两处：nginx 要以 www-data 读
# WEBROOT/index.html（伪装站变 403），acme.sh 继承我们的 umask 去写
# webroot 的 .well-known 挑战文件（nginx 读不到 → 签证书直接失败）。
# 所以只收这张白名单上的路径。
_PRIV_DIRS  = lambda: (BGP_DIR, SUB_DIR, "/etc/ssl/sb")
_PRIV_FILES = lambda: (TOKENS_FILE, GHRELAY_TOKEN_FILE, STATE_FILE, PEERS_FILE,
                       GHDL_RELAYS, CUSTPL_FILE, TPLSRC_FILE, SUBPORT_FILE,
                       HOST_FILE, NODE_FILE, CFG_FILE, SBOX_FILE, SR_FILE, SR_NODES_FILE, SR_MODE_FILE,
                       NETPROFILE, PRIVDNS_FILE, PRIVDNS_MAP_FILE, ACME_KEY, KEY)

def harden_perms():
    """把只该 root 看的目录/文件收成 root-only。幂等、极快，每次入口都过一遍。

       为什么是「每次都过」而不是「写的时候设好」：写这些文件的地方有二十多处，
       漏一处就白做；而且老安装是带着 0644 升上来的，光管新写的没用。"""
    for d in _PRIV_DIRS():
        try:
            if os.path.isdir(d):
                os.chmod(d, 0o700)
        except OSError:
            pass
    for f in list(_PRIV_FILES()) + (
            [os.path.join(SUB_DIR, n) for n in os.listdir(SUB_DIR)]
            if os.path.isdir(SUB_DIR) else []):
        try:
            if os.path.exists(f) and not os.path.isdir(f):
                os.chmod(f, 0o600)           # 跟随软链：SUB_DIR 里是指向成品配置的链接
        except OSError:
            pass

def _is_ip(h):
    return bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", h)) or ":" in h   # v4 或 v6 都当 IP

def _sub_https():
    """订阅能否走 HTTPS：host 是域名（非 IP）且 acme 真证书就绪。自签/IP 仍用 HTTP。"""
    h = _host()
    return (not _is_ip(h)) and os.path.exists(ACME_CRT) and os.path.exists(ACME_KEY)

_DMI_DIR = "/sys/class/dmi/id"
# DMI 里认得出的云厂商 → 订阅名；小写子串匹配，按顺序取第一个。
# Azure 认资产标签那串固定号：sys_vendor 写 Microsoft 的也可能是别家用 Hyper-V 开的小鸡
_SUB_VENDORS = (("google", "Google"), ("amazon", "AWS"), ("7783-7084-3265-9085-8269-3286-77", "Azure"), ("oraclecloud", "Oracle"),
                ("alibaba", "Aliyun"), ("tencent", "Tencent"), ("digitalocean", "DigitalOcean"), ("vultr", "Vultr"),
                ("linode", "Linode"), ("akamai", "Linode"), ("hetzner", "Hetzner"), ("huawei", "Huawei"))
# 虚拟化软件 / 主板通用名：写的是这些说明厂商没署名，不拿来当名字
_DMI_GENERIC = ("qemu", "kvm", "red hat", "openstack", "bochs", "xen", "vmware", "innotek", "virtualbox",
                "microsoft", "standard pc", "system manufacturer", "to be filled", "default string", "not specified", "nutanix")

def _kernel_release():
    return os.uname().release

def sub_label():
    """订阅地址末尾的名字：<token>/<名字>.<ext>。小火箭 / Clash 拿文件名当订阅名，原来显示的是 token，
       截个图就把 token 漏了（仓库主人 10/19）。名字认机器、不跟节点前缀（前缀会改）：
       先读 DMI（云厂商虚拟主板自己写的：谷歌云 Google、AWS Amazon EC2、甲骨文资产标签 OracleCloud.com…），
       再看内核名后缀（Ubuntu 云镜像 -gcp / -aws / -azure / -oracle），都认不出（DMIT、搬瓦工这类 KVM 小鸡
       写的是 QEMU）就叫 VPS。厂商自己署了名但不在表里的，照它写的（只留字母数字）。只读本机文件，不出网。
       名字不参与鉴权：哪天认出来的不一样了，客户端里的老地址照样能拉（见 _SUB_SERVER_PY）。"""
    def rd(n):
        try:
            return open(f"{_DMI_DIR}/{n}").read().strip()
        except OSError:
            return ""
    vendor = rd("sys_vendor")
    dmi = " ".join((vendor, rd("product_name"), rd("chassis_asset_tag"), rd("bios_vendor"))).lower()
    for key, name in _SUB_VENDORS:
        if key in dmi:
            return name
    rel = _kernel_release().lower()
    for suf, name in (("-gcp", "Google"), ("-aws", "AWS"), ("-azure", "Azure"), ("-oracle", "Oracle")):
        if rel.endswith(suf):
            return name
    if vendor and not any(g in vendor.lower() for g in _DMI_GENERIC):
        v = re.sub(r"[^A-Za-z0-9]+", "", vendor.split()[0])[:16]
        if re.search(r"[A-Za-z]", v):
            return v
    return "VPS"

def sub_url(ext):
    t = load_tokens().get(ext)
    if not t:
        return "(未生成)"
    scheme = "https" if _sub_https() else "http"
    return f"{scheme}://{_host()}:{sub_port()}/{t}/{sub_label()}.{ext}"   # 老地址 /<token>.<ext> 照样能拉（见 _SUB_SERVER_PY）

SUB_TITLES = {"yaml": "mihomo / Clash", "json": "sing-box", "conf": "小火箭 Shadowrocket",
              "srnodes": "小火箭 节点订阅（首页 ➕ → Subscribe）"}

def _sub_cert_domain():
    """域名证书管理里装的证书，有没有一个能给订阅用的域名 → (域名, 说明)。
       要求：证书盖得住它、而且【解析到本机】（灰色云直连）。橙色云解析到 CF，订阅端口过不了 CF，不能用。
       泛域名证书挑不出具体子域名，就让人填一个（被泛域名盖住、解析到本机才收）。"""
    try:
        i = cert_info()
    except Exception:
        return "", ""
    if not i.get("exists"):
        return "", ""
    names = [n for n in dict.fromkeys([i.get("domain", "")] + i.get("names", [])) if n and not n.startswith("*.")]
    bad = []
    for n in names:
        ok, why = _domain_resolves_here(n)
        if ok:
            return n, why
        bad.append(f"{n}：{why}")
    if i.get("wildcard") and _sys.stdin.isatty():
        base = next((x[2:] for x in i.get("names", []) if x.startswith("*.")), "")
        d = _ask(f"证书是泛域名 *.{base}，订阅用哪个子域名（要灰色云解析到本机，回车跳过）: ").strip().lower().rstrip(".")
        if d and _name_covers(i.get("names", []), d):
            ok, why = _domain_resolves_here(d)
            if ok:
                return d, why
            bad.append(f"{d}：{why}")
    return "", "；".join(bad)

def sub_offer_cert_domain():
    """订阅还在用 IP、域名证书管理里又装了证书 → 问要不要把订阅地址换成证书域名（回车 = 换）。
       仓库主人：「检测到证书域名，询问是否用证书域名来替换订阅，已经是证书域名的就忽略」。

       换的只是订阅地址（sub.host），节点链接不动；换完订阅走 HTTPS + 真证书——明文 HTTP 订阅
       在线路上谁都看得到 token 和全部节点参数，换成 HTTPS 这一条就堵上了；本机 GitHub 中转也跟着能开。
       代价：订阅地址从 http://IP 变成 https://域名，旧地址失效，客户端要重新导入一次。
       换了返回 True。已经是域名、没证书、证书域名没解析到本机，都不问。"""
    if not _sub_items() or not _is_ip(_host()):
        return False
    d, why = _sub_cert_domain()
    if not d:
        if why:
            ui_tip(f"证书域名没解析到本机，订阅暂不能换（要灰色云 A 记录）：{why}")
        return False
    ui_tip(f"检测到证书域名 {d}：订阅可换成 https 域名地址，换完客户端要重新导入")
    if (_ask(f"订阅地址换成 {d}？（回车 = 换 / n = 不换）: ").strip().lower() or "y") in ("n", "no"):
        return False
    old = open(HOST_FILE).read() if os.path.exists(HOST_FILE) else None
    try:
        open(HOST_FILE, "w").write(d)
        G["host"] = d
        build_subscription(read_saved_links(), new_token=False)   # token 不变，xy-sub 换成 HTTPS
    except Exception as e:
        if old is not None:
            open(HOST_FILE, "w").write(old)
        G["host"] = _host()
        try: serve_sub()
        except Exception: pass
        print("  ⚠ 换订阅域名失败，已换回原来的地址：", e)
        return False
    print("  ✔ 订阅地址已换成证书域名（token 不变），客户端重新导入：")
    print_sub_urls()
    return True

def _sub_items():
    ff = {"yaml": CFG_FILE, "json": SBOX_FILE, "conf": SR_FILE}; toks = load_tokens()
    items = [(e, sub_url(e)) for e in ("yaml", "json", "conf") if os.path.exists(ff[e]) and toks.get(e)]
    if sr_nodes_url():                                   # 小火箭节点订阅：紧跟在小火箭配置后面
        items.append(("srnodes", sr_nodes_url()))
    return items

def sub_urls_text():
    """写进文件 / 日志用的纯文本：标题一行、链接一行、空一行。"""
    return "\n\n".join(f"  {SUB_TITLES[e]}\n{u}" for e, u in _sub_items())

def _hl(code, text):
    """只在直接印到终端时上色；进文件 / 管道的照旧纯文本（不然复制出去带一串乱码）。"""
    return f"\033[{code}m{text}\033[0m" if _sys.stdout.isatty() else text

# ── 屏上排版小工具（CLAUDE.md 第五节「排版要整齐干净」）────────────────────────
# 菜单统一长这样：标题框 → 状态 / 链接区 → 分隔线 → 「  N. 名称　当前：值」一项一行 → 一行提示。
# 编号自动染绿（见文件头 print）；颜色只在终端上（_hl）；要复制的链接顶格单独一行。
def ui_title(text):
    print("\n" + "=" * 60 + f"\n  {text}\n" + "=" * 60)

def ui_line():
    print("-" * 60)

def ui_items(items):
    """[(编号, 名称, 当前值或 None), …] → 每项一行「  N. 名称　当前：值」。"""
    for num, name, cur in items:
        print(f"  {num}. {name}" + (f"　当前：{cur}" if cur not in (None, "") else ""))

def ui_tip(text):
    print("  " + _hl("1;33", f"提示：{text}"))

def ui_warn(text):
    """会让东西没掉的操作：红色粗体单独一行说清后果（CLAUDE.md 第四节）。"""
    print("  " + _hl("1;31", f"⚠ {text}"))

def ui_kv(label, value):
    """状态行：「  标签：值」，标签青色。"""
    print(f"  {_hl('36', label + '：')}{value}")

def ui_url(label, url, code="1;32"):
    """一条要复制的链接：黄色标题一行 + 链接顶格单独一行（默认绿色）。"""
    print(_hl("1;33", f"▸ {label}"))
    print(_hl(code, url) if url else _hl("2", "（还没有）"))

def print_sub_urls():
    """屏上的三大订阅：客户端名黄色粗体一行，链接绿色单独一行（顶格，长按复制不带空格），每条之间空一行。
       仓库主人：「下面的三大订阅也是的应该写得清晰明了而且用高亮，写成一坨别人怎么看得清」。"""
    items = _sub_items()
    for i, (e, u) in enumerate(items):
        # 标上这一格式当前用的模板（刷新、每天 03:20 自动更新都按它）；自定义的换个颜色，一眼看得出不是作者模板
        cust = e in FMT and tpl_src_of(e) == "custom"
        mark = "" if e not in FMT else _hl("1;35" if cust else "36", "（自定义模板）" if cust else "（作者模板）")
        print(_hl("1;33", f"▸ {SUB_TITLES[e]}") + mark)
        print(_hl("1;32", u))
        if i < len(items) - 1:
            print()

def print_node_links(links):
    """屏上的分享链接：节点名青色粗体一行，链接单独一行（顶格、不上色，复制最干净），每条之间空一行。
       仓库主人：「每个单条链接应该用什么隔开让别人好看一点或者每条单链接空一行也可以」。"""
    for i, u in enumerate(links, 1):
        print(_hl("1;36", f"[{i}] {_link_name(u) or '节点'}"))
        print(u)
        if i < len(links):
            print()

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
# 订阅托管小服务：静态发订阅文件；另带一个 /gh/ GitHub 中转（规则/图标走本机、不依赖外部 CDN / 反代）。
# 中转和订阅共用同一端口（客户端本就从这端口拉订阅，无需额外放行）。多线程，中转不卡订阅。
# 安全：中转只白名单 GitHub 几个主机——绝不做成"谁都能拿它转发任意网址"的开放代理。
_SUB_SERVER_PY = r'''import http.server, ssl, sys, re, urllib.request, urllib.parse
port = int(sys.argv[1]); directory = sys.argv[2]
tokenfile = sys.argv[3]
cert = sys.argv[4] if len(sys.argv) > 4 else ''
key  = sys.argv[5] if len(sys.argv) > 5 else ''
ALLOW = ('raw.githubusercontent.com', 'objects.githubusercontent.com', 'github.com', 'codeload.github.com',
         'gist.github.com', 'gist.githubusercontent.com')
class H(http.server.SimpleHTTPRequestHandler):
    # 别报家门：Python 默认会回 `Server: SimpleHTTP/0.6 Python/3.11.2`，还有一张一眼
    # 认得出的 http.server 错误页。扫到这个端口的人据此就知道「这是个自建的小服务」，
    # 配上非常规端口，基本等于挂牌说明这里托管着订阅。报 nginx 混进大多数机器里。
    server_version = 'nginx'
    sys_version = ''
    def version_string(self):
        # 不能只靠 server_version + sys_version：父类是 `版本 + ' ' + 系统版本` 拼的，
        # sys_version 置空会留下 `Server: nginx ` 这么个带尾空格的串——真 nginx 不长这样，
        # 尾空格本身就是个破绽。直接把整串定死。
        return 'nginx'
    error_message_format = ('<html><head><title>%(code)d</title></head>'
                            '<body><center><h1>%(code)d %(message)s</h1></center>'
                            '<hr><center>nginx</center></body></html>')
    timeout = 30                     # 读请求超时：卡住的客户端不会永久占着线程
    def __init__(self, *a, **k):
        super().__init__(*a, directory=directory, **k)
    def log_message(self, *a):
        pass
    def do_GET(self):
        i = self.path.find('/gh/')       # 路径形如 /<token>/gh/<github-url>
        if i >= 0:
            self._relay(self.path[1:i], self.path[i + 4:]); return
        # /<token>/<名字>.<ext> → /<token>.<ext>：名字只给客户端当订阅名显示，不参与鉴权；老地址照旧
        m = re.match(r'^/([A-Za-z0-9_-]+)/[^/?#]+\.(yaml|json|conf|txt)(?=$|[?#])', self.path)
        if m:
            self.path = '/%s.%s' % (m.group(1), m.group(2))
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
            # UA 跟主脚本一致（见 HTTP_UA）：这是中转服务端替别人去 GitHub 取件，
            # 报 'xy-sub' 等于替对方向沿途声明「这条链路上跑着 nodekit」。
            req = urllib.request.Request(target, headers={'User-Agent': 'curl/8.5.0'})
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

def _sub_server_heal():
    """xy-sub 小服务的脚本跟本版不一样（老装机）→ 换成本版并重启 xy-sub。只在内容变了时动一次。
       这次是为了认 /<token>/<名字>.<ext> 新地址：不换的话面板给的新地址拉不到。"""
    try:
        if not os.path.exists(SUB_SERVER) or open(SUB_SERVER).read() == _SUB_SERVER_PY:
            return False
        open(SUB_SERVER, "w").write(_SUB_SERVER_PY)
    except OSError:
        return False
    sh("systemctl restart xy-sub", check=False)
    return True

def serve_sub(reset=False, renew_port=True):
    """SUB_DIR 放 <token>.<ext> 软链指向各格式配置文件；每格式独立 token（存 TOKENS_FILE）。
       reset=True 换全部 token + 换新随机端口；否则复用已有、只给新格式补 token、端口不动。
       renew_port=False：端口调用方已经换过了（见 build_subscription），这里别再换第二次。"""
    os.makedirs(SUB_DIR, exist_ok=True)
    # 这个目录里【文件名就是 token】，列一次目录就全曝光；空 index.html 挡住列目录，
    # harden_perms 再把目录本身收成 0700。两道都要，少一道都漏。
    if reset and renew_port:
        renew_sub_port()                        # 重装换节点：端口随 token 一起换新
    toks = {} if reset else load_tokens()
    for f in os.listdir(SUB_DIR):                       # 清旧软链（含 .links）
        if f.rsplit(".", 1)[-1] in SUB_EXTS or f.endswith((".links", ".txt")):
            os.remove(os.path.join(SUB_DIR, f))
    for ext, target in (("yaml", CFG_FILE), ("json", SBOX_FILE), ("conf", SR_FILE)):
        if os.path.exists(target):
            toks.setdefault(ext, secrets.token_urlsafe(12))
            os.symlink(target, f"{SUB_DIR}/{toks[ext]}.{ext}")
    if toks.get("conf") and os.path.exists(SR_NODES_FILE):         # 小火箭节点订阅：跟小火箭配置同一个 token、.txt
        os.symlink(SR_NODES_FILE, f"{SUB_DIR}/{toks['conf']}.txt")
    # 节点链接端点（.links）：纯本机分享链接，供别的机器聚合拉取；token 保护。
    # 重装换节点(reset)时和订阅一样换 .links token，旧地址失效（防泄露）；平时保持不变。
    local = read_saved_links()
    if local:
        _write_links_file(local)                        # 选了外机DNS解析的，末尾多一行 #privdns
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
        if d.get("network") == "grpc":
            return "vless-grpc"
        return "vless-ws"
    return None

# 协议键 → sing-box 模板里的节点 tag（模板节点名固定，只换连接参数）
PROTO_TO_SBTAG = {
    "vless-vision": "🇺🇲 VLESS_TCP/TLS_Vision", "vless-ws": "🇺🇲 VLESS_WS",
    "vmess-ws": "🇺🇲 VMess_WS", "trojan": "🇺🇲 Trojan_TCP", "hy2": "🇺🇲 Hysteria2_TLS",
    "reality-vision": "🇺🇲 VLESS_Reality_Vision", "reality-grpc": "🇺🇲 VLESS_Reality_gPRC",
    "tuic": "🇺🇲 singbox_tuic", "anytls": "🇺🇲 AnyTLS", "vmess-httpupgrade": "🇺🇲 VMess_HTTPUpgrade_TLS",
    "vless-grpc": "🇺🇲 VLESS_gRPC_TLS",
}

def _sb_vmess_security(d):
    """sing-box 出站的 vmess 加密：写具体算法，别写 auto / none / zero。
       xray 的 vmess 服务端只认具体算法——sing-box 把 auto、zero、none 原样报上去，xray 直接拒
       （unknown security type），sing-box 客户端连 xray 内核的 vmess 全挂（新装模拟逐个组合实测：
       aes-128-gcm / chacha20-poly1305 通，auto / zero / none 不通）。sing-box 内核的 vmess 哪种都收。
       链接里写明了具体算法就照用，否则 aes-128-gcm（手机基本都有 AES 硬件加速）。"""
    c = str(d.get("cipher") or "").lower()
    return c if c in ("aes-128-gcm", "chacha20-poly1305", "aes-128-cfb") else "aes-128-gcm"

def mihomo_to_sb_outbound(key, d):
    """mihomo 节点 dict → 完整的 sing-box 出站对象（服务器端现生成，不依赖模板里的固定参数）。
       不支持的类型(如 xhttp)返回 None，由调用方跳过。"""
    if key not in PROTO_TO_SBTAG:
        return None                                      # xhttp 等 → 不写进 sing-box
    if d.get("encryption"):
        return None                                      # VLESS Encryption：sing-box 不支持，写进去只会连不上
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
              "uuid": d["uuid"], "security": _sb_vmess_security(d), "alter_id": 0,
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
        if d.get("up") and d.get("down"):                    # hy2 满速（Brutal），见 hy2_bw
            ob["up_mbps"] = int(str(d["up"]).split()[0]); ob["down_mbps"] = int(str(d["down"]).split()[0])
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
    """不属于任何已建国家组、也不是中国节点的漏网节点名（present 为 detect_countries 的返回）。"""
    norm = [_norm_us_flag(n) for n in names]
    rxs = [re.compile(p) for _, p, _ in present]
    return [n for n in norm if not any(rx.search(n) for rx in rxs) and not is_cn_node(n)]

# ---------------------------------------------------------------------------- 中国节点
# 中国节点（回国 / 国内 BGP / 装在国内机上的节点，或模板里手写的）不该混进出国用的组：
# 被手选或被 url-test 挑中，国外流量就绕到国内机上了。与 Mihomo-fx 复写脚本同一套做法：
#   · 收进 🎯直连（可手选单节点）+ 隐藏的 🇨🇳中国随机（国内地址测速，只挂在 🎯直连 下）；
#   · 从其它出国用途的组（🌍全球加速 / ♻️全部随机 / 🎲其他随机 / 服务组）剔除；
#   · 不受 COUNTRY_THRESHOLD 约束，有 1 个就生效。
# 只认 🇨🇳 国旗、独立单词 CN/cn、"中国大陆"。故意【不】走 _cc 放宽——放宽后 CN2/CNIX
# 这类线路标记会被误收。同时命中其它国家的（🇨🇳台湾、深港中转、CN→JP 中转）不算大陆，
# 仍归原国家组。
# 全是中国节点时不剔除：剔完出国组就空了（sing-box 空组直接 FATAL），保持原样。
CN_GROUP = "🇨🇳中国随机"
CN_PAT = r"🇨🇳|\bCN\b|\bcn\b|中国大陆"
CN_TEST_URL = "http://connect.rom.miui.com/generate_204"   # 国内机连不上 gstatic，不能拿它测
DIRECT_GROUP = "🎯直连"
OTHER_CC_PAT = "|".join(p for _, p in COUNTRY_GROUPS)
# 这些组保留中国节点：直连两件套、国家组（自带精确 filter）、其他随机（自己排过）、
# 链式中转（国内 BGP 机做前置跳板是正经用法）、GLOBAL（全局模式什么都能选）
CN_KEEP = {DIRECT_GROUP, CN_GROUP, OTHER_GROUP, "GLOBAL", "🔗链式中转"} | {g for g, _ in COUNTRY_GROUPS}
_CN_RX, _OTHER_CC_RX = re.compile(CN_PAT), re.compile(OTHER_CC_PAT)

def is_cn_node(name):
    n = _norm_us_flag(name)
    return bool(_CN_RX.search(n)) and not _OTHER_CC_RX.search(n)

def cn_exact_re(names):
    """把一批节点名拼成精确匹配的正则 ^(?:a|b)$，给 mihomo 的 exclude-filter 用。
       为什么不直接用 CN_PAT：Go 正则没有环视，写不出「是 CN 但不带其它国家标记」，
       直接排 CN_PAT 会把「🇨🇳台湾」这类其实不是大陆的节点也踢出去。服务器知道哪些是
       中国节点，列名字最准。只转义正则元字符——Go 不认 `\\ `（反斜杠加空格）这类对非标点的转义。"""
    esc = lambda n: re.sub(r"([\\.+*?()|\[\]{}^$])", r"\\\1", n)
    return "^(?:" + "|".join(esc(n) for n in names) + ")$"

def cn_split(names):
    """返回 (中国节点名列表, 要不要从出国组里剔除)。全是中国节点时不剔除（见上）。"""
    cn = [n for n in names if is_cn_node(n)]
    return cn, bool(cn) and len(cn) < len([n for n in names if n])

def _sb_country_groups(tags, existing=()):
    """sing-box 国家随机组：无 filter，按正则算好每国成员显式列入 urltest。
       返回 (国家组对象列表, 组名列表)。tag 用原始名（保留 🇺🇲 等，匹配用归一名）。

       existing：模板 outbounds 里已有的 tag。**同名的不再生成**，让模板里那个说了算——
       sing-box 遇到重复 tag 是硬失败(FATAL: duplicate outbound/endpoint tag)，整份配置
       解析不了。与 mihomo 侧同一套取舍：同名接管、不同名并存。"""
    present = detect_countries(tags)
    cn, _ = cn_split(tags)
    existing = set(existing)
    objs, names = [], []
    mk = lambda tag, members, url="https://www.gstatic.com/generate_204": {
        "tag": tag, "type": "urltest", "outbounds": members, "url": url,
        "interval": "120s", "tolerance": 30}
    if cn and CN_GROUP not in existing:                  # 🇨🇳中国随机：只挂在 🎯直连 下，不进 names
        objs.append(mk(CN_GROUP, cn, CN_TEST_URL))
    if not present:                                      # 没有任何国家 → 不建国家组（含"其他随机"），与 mihomo 一致
        return objs, []
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in tags if rx.search(_norm_us_flag(t))]
        if gname not in existing:
            objs.append(mk(gname, members))
        names.append(gname)                              # 名字照样展开（指向模板里的同名出站）
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in tags if not any(r.search(_norm_us_flag(t)) for r in rxs)
                    and not is_cn_node(t)]
        if omembers:
            if OTHER_GROUP not in existing:
                objs.append(mk(OTHER_GROUP, omembers))
            names.append(OTHER_GROUP)
    return objs, names

def _cn_into_direct(members, cn):
    """🎯直连 的成员里挂上中国节点：DIRECT 后面插 🇨🇳中国随机，单节点追加在末尾
       （与 mihomo 侧 include-all 的效果同序：DIRECT / 🇨🇳中国随机 / 🌍… / 各中国单节点）。
       已经在里面的不重复加。"""
    out = list(members)
    if CN_GROUP not in out:
        i = out.index("DIRECT") + 1 if "DIRECT" in out else 0
        out.insert(i, CN_GROUP)
    return out + [n for n in cn if n not in out]

def _local_xray_reality():
    """本机 xray 内核开的 reality 入站 → {(对外地址, 链接里的端口, 借用域名)}。
       挂在 nginx 443 分流后面的（监听 127.0.0.1）链接里报的是 443——sing-box 的 reality-vision 也是 443，
       光看端口分不开，所以带上借用域名（两个核心的借用域名不一样才会挂进去，见 _xr_behind_split）。

       sing-box 订阅要跳过它们：xray 新版的 REALITY 只收 ClientHello 里带 X25519MLKEM768 的客户端
       （XTLS/REALITY tls.go：reject outdated/strange Client Hello that doesn't have X25519MLKEM768），
       服务端没有开关能放宽；sing-box 的 reality 客户端不带这一项（新装模拟用 sing-box 1.12 / 1.14 对
       xray 26.9 实测，换哪种 utls 指纹都一样被拒；Hiddify / NekoBox 用的也是 sing-box 内核），
       握手直接被当成探测转去借用站，表现就是证书错 / 超时。
       mihomo 带了，所以 mihomo / 小火箭订阅照常保留这些节点。sing-box 内核开的 reality 不受影响。
       只认本机（地址 = 本机域名 / IP）：多机聚合进来的别人节点看不到人家的内核，不动。"""
    try:
        cfg = json.load(open(f"{XRAY_DIR}/config.json"))
    except Exception:
        return set()
    ports = set()
    for ib in cfg.get("inbounds", []):
        st = ib.get("streamSettings") or {}
        if st.get("security") == "reality" and ib.get("port"):
            sni = ((st.get("realitySettings") or {}).get("serverNames") or [""])[0]
            local = str(ib.get("listen", "")) in ("127.0.0.1", "localhost")
            try:
                ports.add((443 if local else int(ib["port"]), sni))
            except (TypeError, ValueError):
                pass
    if not ports:
        return set()
    hosts = {h for h in (G.get("host"), G.get("domain"), _host()) if h}
    try:
        st = json.load(open(STATE_FILE))
        hosts |= {h for h in (st.get("host"), st.get("domain")) if h}
    except Exception:
        pass
    pn = privdns_conf()["name"]                          # 节点专用域名（13 → 11）：本机节点地址换成它以后也得认出来
    if _privdns_is_domain_name(pn):
        hosts.add(pn)
    return {(h, p, sni) for h in hosts for p, sni in ports}

def build_singbox_sub(nodes, tpl_url):
    """对象级替换锚点：_NOD_ 换节点对象、_GRP_ 换国家组、
       _NAM_ / <正则> / _NAM_:<正则> 展开策略组成员，再按手写风格序列化。"""
    cfg = json.loads(fetch_tpl(tpl_url))                      # 中转改写 + 老锚点名归一
    objs, xr_skip = [], 0
    xr_reality = _local_xray_reality()
    for key, d in nodes:
        if d.get("reality-opts") and (str(d.get("server", "")), int(d.get("port") or 0),
                                      str(d.get("servername", ""))) in xr_reality:
            xr_skip += 1                                     # 见 _local_xray_reality：sing-box 客户端连不上
            continue
        try:
            ob = mihomo_to_sb_outbound(key, d)
            if ob: objs.append(ob)
        except Exception:
            pass
    if xr_skip:
        print(f"  sing-box 订阅跳过 {xr_skip} 条 xray 内核的 reality（sing-box 系客户端连不上，mihomo / 小火箭照常有）")
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
    known_tags = set(existing_tags) | set(tags) | set(country_names) | {CN_GROUP}
    cn_tags, cn_drop = cn_split(tags)
    cn_set = set(cn_tags)

    def expand_list(lst, owner=""):
        """展开策略组成员。三种写法可拆解组合（前缀管建不建国家组，冒号后的正则管带不带节点）：

             _NAM_        只列国家组名，不带节点
             <正则>              只带命中的节点名，不列国家组      例：.*  或  🇺🇸|US
             _NAM_:<正则> 两个都要：国家组名 + 命中的节点名

           裸正则怎么跟字面 tag 名区分：先查 known_tags，是已知出站就当字面量，
           否则才按正则去匹配节点名。所以 "DIRECT"、"🎯直连" 这些照常原样保留，
           而 ".*" 这种不存在的 tag 才会被当成正则。

           正则一条都没匹配上时【保留字面量】而不是丢掉：这样模板里把 tag 名写错一个字，
           sing-box 会照常报 "outbound not found"，一眼能看出问题；静默丢掉反而查不出来。

           owner 是这个组自己的 tag：出国用途的组（不在 CN_KEEP 里）正则展开时剔除中国节点。
           正则本来命中了、只是全被剔掉的，算"命中了零个"，不当字面量保留。"""
        drop = cn_set if (cn_drop and owner not in CN_KEEP) else set()
        pick = lambda rx: [t for t in tags if re.search(rx, t) and t not in drop]
        out = []
        for x in lst:
            if x == A_NAMES:
                out += country_names                                 # 只国家组名
            elif isinstance(x, str) and x.startswith(A_NAMES + ":"):
                out += country_names                                 # 国家组名 + 命中节点名
                out += pick(x[len(A_NAMES) + 1:])
            elif isinstance(x, str) and x not in known_tags:
                try:
                    raw = [t for t in tags if re.search(x, t)]       # 裸正则 → 只匹配节点名
                    hit = pick(x)
                except re.error:
                    raw = hit = []                                   # 不是合法正则 → 当字面量
                if raw and not hit:
                    pass                                             # 命中的全是中国节点，已剔除
                elif hit:
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
            x["outbounds"] = expand_list(x["outbounds"], x.get("tag", ""))
            if cn_tags and x.get("tag") == DIRECT_GROUP:
                x["outbounds"] = _cn_into_direct(x["outbounds"], cn_tags)
            new_ob.append(x)
        else:
            new_ob.append(x)
    cfg["outbounds"] = new_ob
    _sb_direct_ip(cfg, _direct_targets(nodes))                    # 各 VPS IP 直连（走紧凑序列化，不破坏格式）
    _sb_selfdns(cfg, _selfdns_doh())                              # 开关开启：dns.final 换成本机自建 DoH
    _sb_privdns(cfg, nodes)                                       # 被藏的节点：domain_resolver 指向它的 DNS
    open(SBOX_FILE, "w").write(sb_dumps(cfg))

# --- Shadowrocket [Proxy] 行：从 mihomo 参数转（名称带国旗前缀让分组正则命中）---
# 参数名用小火箭自己的那套：仓库主人 10/12 给的官方懒人配置注释里写的就是这些，小火箭导出节点时的内部字段
# 也是这一套（password / tls / peer / allowInsecure / obfs / path / obfsParam / publicKey / shortId / xtls /
# auth / user / udp / alpn / upmbps / downmbps）。以前写的 username / sni / skip-cert-verify / public-key / short-id /
# obfs-uri / obfs-host 是 Surge / mihomo 的叫法——仓库主人 10/11 实测「配置里的节点有的出不来、有的超时，同样的节点
# 单条链接导入能连」，多半栽在这。xtls=2 是 xtls-rprx-vision（1 是早废了的 direct）。
# 这几类小火箭配置里写不出来 / 连不上，跳过（单条链接照样在）：xhttp（小火箭不支持）、VLESS Encryption（查不到支不支持）、
# httpupgrade（小火箭会当成 websocket 连，必断）；xray 内核的 reality 在 build_shadowrocket_sub 里跳过。
# hy2 端口跳跃写 mport=起-止（小火箭分享链接的字段名）；起点端口照写，不认 mport 也能连（服务端 DNAT 到真端口）。
def shadowrocket_line(name, d):
    t = d.get("type"); srv = d["server"]; port = d.get("port")
    sni = d.get("servername") or d.get("sni") or srv
    net, wo = d.get("network"), d.get("ws-opts") or {}
    if d.get("encryption"):
        return None
    tls = [f"peer={sni}"] + (["allowInsecure=1"] if str(d.get("skip-cert-verify")).lower() == "true" else [])
    # ws 的路径 / Host 在小火箭配置行里叫 obfs-uri / obfs-host（跟它的 Snell 行同名），不是分享链接里的 path / obfsParam。
    # 10/20 仓库主人苹果实测同一组节点五种写法：path + obfsParam（加不加 alpn=http/1.1、Host 写成 JSON）全超时，
    # 只写 obfs-uri + obfs-host 的 vless-ws / vmess-ws（经 nginx 443）和 vless-ws²（xray 直连）全通。
    # 10/18 以为是 nginx 开着 h2、加了 alpn=http/1.1，实测不是：小火箭读不到 path，请求打到伪装站首页（nginx 日志 200 不是 101）。
    # httpupgrade（mihomo 里是 ws + v2ray-http-upgrade）写 obfs=httpupgrade，路径 / Host 同 ws：10/20 同一轮实测，
    # http-upgrade、websocket 加 httpupgrade=1 / v2ray-http-upgrade=true、path= + obfsParam= 都不通
    hu = str(wo.get("v2ray-http-upgrade")).lower() == "true"
    ws = ([f"obfs={'httpupgrade' if hu else 'websocket'}", f"obfs-uri={wo.get('path') or '/'}",
           f"obfs-host={(wo.get('headers') or {}).get('Host') or sni}"]
          if net == "ws" else [])
    if t == "vless":
        p = [f"{name} = vless", srv, str(port), f"password={d['uuid']}", "tls=true", *tls]
        if d.get("flow") == "xtls-rprx-vision":
            p.append("xtls=2")
        ro = d.get("reality-opts")
        if ro:
            p += [f"publicKey={ro.get('public-key', '')}", f"shortId={ro.get('short-id', '')}"]
        if net == "grpc":
            # grpc 服务名跟 ws 路径一样写 obfs-uri（不带 /）：10/20 仓库主人苹果实测，path= / obfsParam= / grpc-service-name=
            # 全超时，obfs-uri=服务名 通；写成 obfs-uri=/服务名 不通
            p += ["obfs=grpc", f"obfs-uri={(d.get('grpc-opts') or {}).get('grpc-service-name', '')}"]
        elif net == "xhttp":
            # xhttp 同一套：obfs=xhttp,obfs-uri=路径,obfs-host=Host。10/20 仓库主人苹果实测 xhttp-tls / reality-xhttp 都通；
            # path= + obfsParam= 不通，obfs=splithttp 不通（量子加密的 xhttp 上面已经跳过，配置行写不了 VLESS Encryption）
            xo = d.get("xhttp-opts") or {}
            p += ["obfs=xhttp", f"obfs-uri={xo.get('path') or '/'}", f"obfs-host={xo.get('host') or sni}"]
        else:
            p += ws or ["obfs=none"]
        return ",".join(p + ["udp=1", "tfo=1"])
    if t == "vmess":
        p = [f"{name} = vmess", srv, str(port), f"password={d['uuid']}", f"alterId={d.get('alterId', 0)}",
             f"method={d.get('cipher') or 'auto'}"]
        if str(d.get("tls")).lower() == "true":
            p += ["tls=true", *tls]
        return ",".join(p + (ws or ["obfs=none"]) + ["udp=1", "tfo=1"])
    if t == "trojan":
        return ",".join([f"{name} = trojan", srv, str(port), f"password={d['password']}", "tls=true", *tls]
                        + ws + ["udp=1", "tfo=1"])
    if t == "hysteria2":
        pt = port or (d["ports"].split("-")[0] if d.get("ports") else "")   # 跳跃时用起点端口
        p = [f"{name} = hysteria2", srv, str(pt), f"auth={d['password']}", *tls, "alpn=h3", "udp=1"]
        # 端口跳跃：mport=起-止（小火箭分享链接认的字段名，配置行沿用它自己的内部字段名，同 obfsParam / peer）。
        # 不认也不坏事：照样连起点端口，服务端 DNAT 到真端口。仓库主人 10/18 要加；真机认不认待朋友苹果实测。
        if d.get("ports"):
            p.append(f"mport={d['ports']}")
        if d.get("obfs") == "salamander" and d.get("obfs-password"):
            p.append(f"obfsParam={d['obfs-password']}")
        if d.get("up") and d.get("down"):                                    # 满速（Brutal）：开了才写，见 hy2_bw
            p += [f"upmbps={str(d['up']).split()[0]}", f"downmbps={str(d['down']).split()[0]}"]
        return ",".join(p)
    if t == "tuic":
        return ",".join([f"{name} = tuic", srv, str(port), f"password={d['password']}", f"user={d['uuid']}",
                         *tls, "alpn=h3", "udp=1"])
    if t == "anytls":
        return ",".join([f"{name} = anytls", srv, str(port), f"password={d['password']}", "tls=true", *tls, "udp=1"])
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

# 小火箭节点走订阅时（sr_sub_mode），节点不在配置里，分组没法显式列成员，只能按名字正则收（policy-regex-filter）。
# 模板里各主分组本来就是 policy-regex-filter=.*，小火箭一直认这个写法。只用 (?! 否定前瞻、不用 (?= ：
# 配置是「键=值」逗号分隔，值里再带 = 有被拆坏的风险。判定口径跟 is_cn_node 一致（有中国标记、没有别国标记）。
_SR_NOT_CN = f"^(?!.*(?:{CN_PAT})).*|.*(?:{OTHER_CC_PAT})"        # 不是中国节点：没中国标记，或者带了别国标记
_SR_IS_CN = f"^(?!.*(?:{OTHER_CC_PAT})).*(?:{CN_PAT})"            # 中国节点：没别国标记、有中国标记
def _sr_other_rx(pats):
    """漏网节点（🎲其他随机）：不属于任何已建国家组，也不是中国节点。"""
    return f"^(?!.*(?:{'|'.join(pats)}))(?:(?!.*(?:{CN_PAT})).*|.*(?:{OTHER_CC_PAT}))"

def _sr_country_groups(names_list, existing=(), regex=False):
    """shadowrocket 国家随机组：显式列成员（不依赖 shadowrocket 正则引擎，稳）。
       regex=True（节点走订阅、不在配置里）：改成 policy-regex-filter 按名字收，正则见上。
       返回 (组定义行文本, 拼进服务组的组名片段[前导逗号, 裸名])。

       existing：模板 [Proxy Group] 段里已有的组名。**同名的不再生成**，让模板里那个说了算。
       与 mihomo / sing-box 侧同一套取舍：同名接管、不同名并存。（mihomo 和 sing-box 的
       重名硬失败已实测确认；小火箭是 iOS 客户端没法在这里跑，但同段里出现两条同名定义
       本身就是歧义的，一并防住。）"""
    present = detect_countries(names_list)
    cn, _ = cn_split(names_list)
    U = "url=http://www.gstatic.com/generate_204,interval=120,tolerance=30,timeout=5"
    existing = set(existing)
    lines, gnames = [], []
    if cn and CN_GROUP not in existing:                  # 🇨🇳中国随机：国内地址测速，不进 gnames
        lines.append(f"{CN_GROUP} = url-test,{('policy-regex-filter=' + _SR_IS_CN) if regex else ','.join(cn)},"
                     f"url={CN_TEST_URL},interval=120,tolerance=30,timeout=5")
    if not present:                                      # 没有任何国家 → 不建国家组（含"其他随机"），与 mihomo 一致
        return "\n".join(lines), ""
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in names_list if rx.search(_norm_us_flag(t))]
        if gname not in existing:
            lines.append(f"{gname} = url-test,{('policy-regex-filter=' + pat) if regex else ','.join(members)},{U}")
        gnames.append(gname)                             # 名字照样展开（指向模板里的同名组）
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in names_list if not any(r.search(_norm_us_flag(t)) for r in rxs)
                    and not is_cn_node(t)]
        if omembers:
            if OTHER_GROUP not in existing:
                mem = ("policy-regex-filter=" + _sr_other_rx([p for _, p, _ in present])) if regex else ",".join(omembers)
                lines.append(f"{OTHER_GROUP} = url-test,{mem},{U}")
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

def _sr_cn_isolate(tpl, names, regex=False):
    """小火箭的中国节点隔离（见 CN_PAT 那段）。没有中国节点时原样返回。

       🎯直连：DIRECT 后面插 🇨🇳中国随机，中国单节点插在第一个 key=value 参数前。
       出国组：`policy-regex-filter=.*` 会把中国节点一起收进去。小火箭的正则引擎这里跑
       不了、没法验证负向前瞻，所以不去改正则，而是把这一项换成【显式列出的非中国节点】
       ——跟国家组的"显式列成员"同一个思路。只动 `.*` 这一种写法：别的正则是模板作者
       自己圈的范围，不替他改。全是中国节点时不剔除（cn_split）。"""
    cn, drop = cn_split(names)
    if not cn:
        return tpl
    keep = [n for n in names if n and n not in cn]
    m = re.search(r"(?ms)^\[Proxy Group\][ \t]*\n(.*?)(?=^\[|\Z)", tpl)
    if not m:
        return tpl

    def one(line):
        if "=" not in line or line.lstrip().startswith("#"):
            return line
        gname, body = (x.strip() for x in line.split("=", 1))
        items = body.split(",")
        first_kv = next((i for i, it in enumerate(items) if "=" in it), len(items))
        if gname == DIRECT_GROUP:
            pol = items[1:first_kv]
            if CN_GROUP not in pol:
                pol.insert(pol.index("DIRECT") + 1 if "DIRECT" in pol else 0, CN_GROUP)
            if not regex:                                # 节点走订阅时单个中国节点不在配置里：只挂 🇨🇳中国随机
                pol += [n for n in cn if n not in pol]
            items = items[:1] + pol + items[first_kv:]
        elif drop and gname not in CN_KEEP and "policy-regex-filter=.*" in items and regex:
            items = [("policy-regex-filter=" + _SR_NOT_CN) if it == "policy-regex-filter=.*" else it for it in items]
        elif drop and gname not in CN_KEEP and "policy-regex-filter=.*" in items:
            items = [it for it in items if it != "policy-regex-filter=.*"]
            first_kv = next((i for i, it in enumerate(items) if "=" in it), len(items))
            have = set(items[1:first_kv])
            items = items[:first_kv] + [n for n in keep if n not in have] + items[first_kv:]
        else:
            return line
        return f"{gname} = {','.join(items)}"

    body = "\n".join(one(l) for l in m.group(1).split("\n"))
    return tpl[:m.start(1)] + body + tpl[m.end(1):]

# ---------------------------------------------------------------------------- 小火箭节点订阅
# 小火箭手册原话：配置文件里写本地节点「仅是为了兼容部分其他类型的配置文件，不能当作添加节点的优先选择」。
# 实测也是：配置里的节点有的出不来、有的全超时，同样的节点单条链接导入却能连。所以（仓库主人 10/11 点头）
# 节点改走「节点订阅」——内容就是那些单条分享链接（base64），小火箭用解析单条链接的同一套来解析；
# 配置只管规则和分组，分组按名字正则把订阅里的节点收进来。
# 新装 / 全部重装默认这样；老装机节点原来在配置里，夜里自动更新要是直接把节点拿掉，没加订阅的设备一觉醒来
# 节点全没了（CLAUDE.md 第四节），所以老装机要在「8 小火箭配置」里看清后果、确认了才切。
# ---------------------------------------------------------------------------- hy2 满速（Brutal）
# hy2 不填带宽时用 BBR：探着走、一丢包就收。填了上下行就是 Brutal：按这个速度一直发、丢包不减速——
# 出国线路丢包多，上传（手机 → VPS）尤其吃亏（仓库主人 10/11：「不管是上传还是下载都用最大的口子来」）。
# 只写进客户端配置（mihomo up / down、sing-box up_mbps / down_mbps），服务端不填，跟着客户端报的走。
# 数要填宽带套餐的上下行：填得比线路还大不会更快，多出来的包在半路丢掉——下载方向是 VPS 往外发，丢掉的
# 也算 VPS 流量；Hysteria 官方文档也说设高了丢包严重反而变慢。小火箭的 hy2 不认这两个参数，不受影响。
def hy2_bw():
    """(上行, 下行) Mbps；没开返回 None（hy2 用 BBR 自动）。"""
    try:
        d = json.load(open(HY2_BW_FILE))
        up, down = int(d["up"]), int(d["down"])
        return (up, down) if up > 0 and down > 0 else None
    except Exception:
        return None

def hy2_bw_set(up=None, down=None):
    """填上下行 = 开；都不给 = 关（回到 BBR 自动）。"""
    if up and down:
        os.makedirs(BGP_DIR, exist_ok=True)
        json.dump({"up": int(up), "down": int(down)}, open(HY2_BW_FILE, "w"))
    else:
        try:
            os.remove(HY2_BW_FILE)
        except OSError:
            pass

def hy2_bw_text():
    bw = hy2_bw()
    return _hl("1;32", f"下行 {bw[1]} · 上行 {bw[0]} Mbps") if bw else "关（自动，BBR）"

def sr_sub_mode():
    """小火箭节点走节点订阅（配置里不带节点）吗。"""
    try:
        return open(SR_MODE_FILE).read().strip() == "sub"
    except OSError:
        return False

def sr_set_sub_mode(on):
    """开：之后生成小火箭配置时节点写进节点订阅。关：删掉节点订阅文件和它的 .txt 软链，节点回到配置里。"""
    os.makedirs(BGP_DIR, exist_ok=True)
    if on:
        open(SR_MODE_FILE, "w").write("sub\n")
        return
    for p in (SR_MODE_FILE, SR_NODES_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    _sr_nodes_link()

def _sr_nodes_link():
    """订阅目录里「小火箭配置 token.txt」→ SR_NODES_FILE：有文件就建、没有就删。
       不重启 xy-sub——它是现读目录里的文件，夜里自动更新第一次生成节点订阅时也能直接拉到。"""
    t = load_tokens().get("conf")
    if not t or not os.path.isdir(SUB_DIR):
        return
    ln = f"{SUB_DIR}/{t}.txt"
    try:
        if os.path.lexists(ln):
            os.remove(ln)
        if os.path.exists(SR_NODES_FILE):
            os.symlink(SR_NODES_FILE, ln)
    except OSError:
        pass

def sr_nodes_url():
    """小火箭节点订阅地址：跟小火箭配置同一个 token、扩展名 .txt（换 token 时一起换）。不是订阅模式返回 ''。"""
    t = load_tokens().get("conf")
    if not (t and sr_sub_mode() and os.path.exists(SR_NODES_FILE)):
        return ""
    return f"{'https' if _sub_https() else 'http'}://{_host()}:{sub_port()}/{t}/{sub_label()}.txt"

def sr_onelink_heal():
    """仓库主人 10/12：「订阅应该做成一个链接最好，两个订阅链接人家导入进去说不能用」。#939 起新装默认
       「节点订阅」（配置 + .txt 两个链接）；这些机器一次性改回节点写在配置里，按当前模板重生成小火箭配置。
       只动一次（记标记）：之后在「8 小火箭配置 → 6」自己选回节点订阅的照它的。小火箭配置手改过的不动
       （重生成会盖掉手改的内容）。生成失败切回原样。改了返回 True。"""
    if os.path.exists(SR_ONELINK_MARK):
        return False
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        open(SR_ONELINK_MARK, "w").write("1\n")
    except OSError:
        return False
    if not (sr_sub_mode() and os.path.exists(SR_FILE) and read_saved_links()) or cfg_auto_paused("conf"):
        return False
    keep = open(SR_NODES_FILE).read() if os.path.exists(SR_NODES_FILE) else None
    sr_set_sub_mode(False)
    G["host"] = _host()
    ylines, nodes = parse_nodes(aggregated_links())
    if not ylines or _regen_fmt("conf", ylines, nodes)[0] == "failed":
        sr_set_sub_mode(True)                                    # 小火箭配置 _regen_fmt 已回滚；节点订阅放回去
        if keep is not None:
            open(SR_NODES_FILE, "w").write(keep); _sr_nodes_link()
        return False
    return True

def build_shadowrocket_sub(nodes, tpl_url):
    sub = sr_sub_mode()
    lines, names_list, links = [], [], []
    hidden = 0
    for key, d in nodes:
        name = d.get("name", "") or key                  # 统一用节点池名称（含服务器端前缀）
        if d.get("_privdns"):
            # 被藏的节点（私有解析）：小火箭怎么指定 DNS 解析节点地址还没在手机上测出来，写进去也连不上；
            # 照写真实 IP 又白藏了。仓库主人 10/28：「小火箭的也删掉」——这份先不放，测通了再加回来
            hidden += 1
            continue
        # 本机 xray 内核的 reality 照写：原先以为小火箭不带 X25519MLKEM768、连不上 xray 26.9.8+ 而跳过，
        # 10/20 仓库主人苹果实测小火箭配置里的 vision² 通了（指纹 Safari26_3），不再跳过（xhttp 照旧不写，见 shadowrocket_line）
        if sub:
            # 节点订阅照抄分享链接：小火箭不支持 xhttp，跳过；量子加密（VLESS Encryption）单条导入
            # 仓库主人实测能连，照收（写在配置里的写法查不到支不支持，所以那边还是跳过）
            if d.get("network") != "xhttp" and d.get("_link"):
                links.append(d["_link"]); names_list.append(name)
            continue
        try:
            s = shadowrocket_line(name, d)
            if s: lines.append(s); names_list.append(name)
        except Exception:
            pass
    if hidden:
        print(f"  小火箭这份不放 {hidden} 个被藏的节点（私有解析，小火箭还没测通）")
    if not lines and not links and not hidden:
        return                                           # 全藏了也照样往下生成：不然上一份写着真实 IP 的配置还挂在订阅上
    tpl = fetch_tpl(tpl_url)                                 # 中转改写 + 老锚点名归一
    # 国家检测/成员池 = 注入节点 + 用户手写进模板 [Proxy] 段的静态节点（"名 = 协议,..." 行）
    static = _sr_static_names(tpl)
    # 模板 [Proxy Group] 段里已有的组名：同名的国家组不再生成，避免同段两条同名定义
    groups_txt, names_frag = _sr_country_groups(names_list + static, _sr_group_names(tpl), regex=sub)
    out = tpl
    out = _fill_block(out, A_NODES, "\n".join(lines))           # 块锚点整行替换，缩进容错（节点走订阅时是空的）
    out = _fill_block(out, A_GROUPS, groups_txt)
    out = _sr_fill_names(out, names_frag)                       # 行内锚点（按行去重）
    out = _sr_cn_isolate(out, names_list + static, regex=sub)
    open(SR_FILE, "w").write(out)
    if sub:
        open(SR_NODES_FILE, "w").write(base64.b64encode(("\n".join(links) + "\n").encode()).decode())
        _sr_nodes_link()

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
    cn, _ = cn_split(names)
    # hidden: true 让国家组不占面板卡片位（仍可在🌍全球加速里选到）；显式写在组上，
    # 覆盖 <<: *COUNTRY_COMMON，自定义模板不改锚点也生效。
    existing = set(existing)
    lines, gnames = [], []
    if cn and CN_GROUP not in existing:
        # 🇨🇳中国随机：只挂在 🎯直连 下（_mihomo_cn_isolate），不进 gnames。
        # exclude-filter 踢掉同时带其它国家标记的（🇨🇳台湾 / 深港中转），国内地址测速。
        lines.append(f"  - {{name: \"{CN_GROUP}\", <<: *COUNTRY_COMMON, filter: '{CN_PAT}', "
                     f"exclude-filter: '{OTHER_CC_PAT}', url: \"{CN_TEST_URL}\", hidden: true}}")
    if not present:
        return "\n".join(lines), ""
    for gname, pat, _ in present:
        if gname not in existing:
            lines.append(f"  - {{name: \"{gname}\", <<: *COUNTRY_COMMON, filter: '{pat}', hidden: true}}")
        gnames.append(gname)
    if OTHER_GROUP and other_members(names, present):          # 有漏网节点才建"其他随机"
        if OTHER_GROUP not in existing:
            # 中国节点也排掉（服务器这边 other_members 已经不算它们），按名字精确排（见 cn_exact_re）
            allpat = "|".join([p for _, p, _ in present] + ([cn_exact_re(cn)] if cn else []))
            lines.append(f"  - {{name: \"{OTHER_GROUP}\", <<: *COUNTRY_COMMON, exclude-filter: '{allpat}', hidden: true}}")
        gnames.append(OTHER_GROUP)
    return "\n".join(lines), "".join(f', "{g}"' for g in gnames)

_MH_ANCHOR_DEF_RE = re.compile(r"(?m)^[ \t]*-[ \t]*&([\w-]+)[ \t]*(\{.*\})[ \t]*$")
_MH_MERGE_RE = re.compile(r"<<:[ \t]*\*([\w-]+)")
_MH_INCLUDE_ALL_RE = re.compile(r"(?:^|[{,])[ \t]*include-all:[ \t]*true[ \t]*(?=[,}])")

def _mh_scalar(line, key):
    """行内 flow mapping 里 key 的标量值：返回 (起, 止, 值) 或 None。认单引号/双引号/裸值。"""
    m = re.search(r"(?:^|[{,])[ \t]*" + re.escape(key) +
                  r":[ \t]*('(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\"|[^,}]*)", line)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.startswith("'"):
        val = raw[1:-1].replace("''", "'")
    elif raw.startswith('"'):
        try: val = json.loads(raw)
        except ValueError: val = raw[1:-1]
    else:
        val = raw
    return m.start(1), m.end(1), val

def _mh_sq(v):
    return "'" + v.replace("'", "''") + "'"

def _mihomo_cn_isolate(tpl, names):
    """mihomo 的中国节点隔离（见 CN_PAT 那段）。没有中国节点时原样返回。

       🎯直连：proxies 里 DIRECT 后面插 🇨🇳中国随机；组本身没有 include-all 的，补上
       include-all + filter（中国节点）+ exclude-filter（其它国家标记），把中国单节点扫进来。
       注意这里绝不能加 exclude-type: direct —— 它会把 proxies 里的 DIRECT 一起踢掉。
       出国组：proxy-groups 里凡是 include-all（自己写的，或经 <<: *锚点 继承的）且不在
       CN_KEEP 里的组，exclude-filter 追加中国节点的精确名单（cn_exact_re）；原来有
       exclude-filter 的合并成 (?:原来的)|(?:名单)。全是中国节点时不剔除（cn_split）。

       只处理单行 `- {...}` 写法（作者模板和国家组都是这样）。多行块写法的组不认识，
       原样保留、打一行提示——漏掉隔离不会让配置坏掉，只是回到改动前的行为。"""
    cn, drop = cn_split(names)
    if not cn:
        return tpl
    m = re.search(r"(?m)^proxy-groups:[ \t]*$", tpl)
    if not m:
        return tpl
    anchors = {a: body for a, body in _MH_ANCHOR_DEF_RE.findall(tpl)}
    exact = cn_exact_re(cn)
    lines = tpl[m.end():].split("\n")
    skipped = []
    for i, line in enumerate(lines):
        if line and not line[0].isspace() and i > 0:
            break                                          # 下一个顶级键：proxy-groups 段结束
        st = line.strip()
        gm = _GNAME_RE.search(line)
        if not gm or not st.startswith("-"):
            continue
        gname = (gm.group(1) or gm.group(2) or gm.group(3) or "").strip().strip("\"'")
        if not (st.startswith("- {") and st.endswith("}")):
            skipped.append(gname)
            continue
        merged = [anchors.get(a, "") for a in _MH_MERGE_RE.findall(line)]
        has_ia = bool(_MH_INCLUDE_ALL_RE.search(line)) or any(_MH_INCLUDE_ALL_RE.search(b) for b in merged)
        cut = line.rstrip().rfind("}")
        if gname == DIRECT_GROUP:
            pm = re.search(r"proxies:[ \t]*\[([^\]]*)\]", line)
            if pm and CN_GROUP not in pm.group(1):
                items = [x.strip() for x in pm.group(1).split(",") if x.strip()]
                bare = [x.strip("\"'") for x in items]
                items.insert(bare.index("DIRECT") + 1 if "DIRECT" in bare else 0, f'"{CN_GROUP}"')
                line = line[:pm.start(1)] + ",".join(items) + line[pm.end(1):]
                cut = line.rstrip().rfind("}")
            if not has_ia:
                line = (line[:cut].rstrip() + f", include-all: true, filter: {_mh_sq(CN_PAT)}, "
                        f"exclude-filter: {_mh_sq(OTHER_CC_PAT)}" + line[cut:])
            lines[i] = line
        elif drop and has_ia and gname not in CN_KEEP:
            own = _mh_scalar(line, "exclude-filter")
            if own:
                a, b, v = own
                lines[i] = line[:a] + _mh_sq(f"(?:{v})|(?:{exact})") + line[b:]
            else:
                inh = next((x for x in (_mh_scalar(b, "exclude-filter") for b in merged) if x), None)
                v = f"(?:{inh[2]})|(?:{exact})" if inh else exact
                lines[i] = line[:cut].rstrip() + f", exclude-filter: {_mh_sq(v)}" + line[cut:]
    if skipped:
        print(f"  ⓘ mihomo 模板里这些组不是单行写法，没做中国节点隔离：{'、'.join(skipped)}")
    return tpl[:m.end()] + "\n".join(lines)

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

def _cf_token_steps(dom):
    """要粘 Cloudflare 令牌前，先把在 CF 后台怎么做列出来（仓库主人 10/15 给的步骤，照抄）。"""
    print()
    print(_hl("1;33", "▸ Cloudflare 令牌怎么做"))
    print("  CF 主页右上头像 → 配置文件 → API 令牌 → 创建令牌 → 用「编辑区域 DNS」模板")
    print("  令牌名称：DNS-DOT")
    print("  权限：区域 - DNS - 编辑")
    print("     ＋ 区域 - 区域 - 读取")
    print(f"  区域资源：包括 - 特定区域 - {_root_domain(dom)}")
    print("  其他不动 → 继续以显示摘要 → 创建令牌 → 复制令牌")
    print()

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
        if d.get("_privdns"):
            continue                                         # 被藏的成员机：这条写的就是真实 IP，留着等于白藏（仓库主人 10/28 同意删）
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
       它的 servername/host 是借用的伪装站（如 www.cisco.com），更不能当直连目标。"""
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
    harden_perms()                # 刚落地是 0644，当场收紧，别留窗口
    return t

def _ghrelay_prefix():
    """本机 GitHub 中转前缀 https://域名:订阅端口/<token>/gh/ ——默认开（有域名+真证书且没被手动关时）。
       带 token 防别人蹭；返回 '' 则用模板里原本的链接（作者模板是 jsDelivr）。中转与订阅同端口、只白名单 GitHub、非开放代理。
       没域名/自签时返回 ''：中转走 HTTPS 需要真证书，否则客户端拒连，只能退回模板原链接。"""
    if os.path.exists(GHRELAY_OFF):
        return ""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") or not _sub_https():
        return ""
    return f"https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/"

# 能被【自动识别】直接改走中转的 GitHub 主机——规则集(.mrs/.yaml)、图标、面板包都在这几个上。
# github.com 的项目页不自动改（多半是说明链接，误改没意义）；真要转，在模板里显式写代理前缀。
# codeload 原来也不认，靠模板里写 gh-proxy 前缀才进中转；作者模板改用 jsDelivr 后面板 zip
# （jsDelivr 发不了 zip）只能写裸 codeload 链接，所以现在按主机认它。
_GH_RAW_HOSTS = ("raw.githubusercontent.com", "gist.githubusercontent.com",
                 "objects.githubusercontent.com", "codeload.github.com")
_URL_TAIL = r'[^\s"\'<>,}\]\)]+'                     # URL 结尾：碰到引号/空白/YAML·JSON 分隔符就停
# 匹配「可有可无的代理前缀 + GitHub 原始文件链接」。前缀那段会把 https://gh-proxy.com/、
# https://ghproxy.net/ 这类镜像整段吃掉一起替换，避免出现「别人的镜像/自己的中转/…」套娃。
_GH_URL_RE = re.compile(
    r'(?:https?://' + _URL_TAIL + r'?/)?'
    r'(https://(?:' + "|".join(h.replace(".", r"\.") for h in _GH_RAW_HOSTS) + r')/' + _URL_TAIL + r')')

# 作者模板的远程链接用 jsDelivr（https://cdn.jsdelivr.net/gh/<o>/<r>@<ref>/<path>）——
# 官方 CDN，比第三方反代 gh-proxy 稳，不会说跑路就跑路。中转开着时要把它们也收进本机中转：
# 先还原成 raw.githubusercontent 链接，再交给下面同一套改写。还原成 raw 而不是让中转去拉 jsDelivr：
# VPS 到 GitHub 本来就通，raw 是第一手、没有 jsDelivr 分支缓存那几个小时的滞后，中转的白名单也不用加。
# 四个 jsDelivr 主机都认；版本号是数字范围（@1、@1.2）或 @latest 的不还原——那是 jsDelivr 的
# 版本解析语义，raw 没有对应的东西，原样留着让客户端直接走 jsDelivr。
_JSD_GH_RE = re.compile(
    r'https://(?:cdn|fastly|gcore|testingcf)\.jsdelivr\.net/gh/'
    r'([^/\s"\'<>,}\]\)@]+)/([^/\s"\'<>,}\]\)@]+)@([^/\s"\'<>,}\]\)]+)/(' + _URL_TAIL + r')')

def _jsd_to_raw(text):
    """jsDelivr 的 GitHub 文件链接 → raw.githubusercontent 链接（版本范围 / latest 不动）。"""
    def sub(m):
        o, r, ref, path = m.groups()
        if ref == "latest" or re.fullmatch(r"\d+(\.\d+)?", ref):
            return m.group(0)
        return f"https://raw.githubusercontent.com/{o}/{r}/{ref}/{path}"
    return _JSD_GH_RE.sub(sub, text)

def _ghrelay_rewrite(text):
    """开启时把模板里的 GitHub 链接改走本机中转；关闭/无域名则原样返回。
       四种写法都认——因为很多人写模板不会加 gh-proxy 前缀，靠 raw 主机名识别才最可靠：
         ① https://raw.githubusercontent.com/…            裸链接，自动识别
         ② https://gh-proxy.com/https://raw.github…       老写法，前缀可有可无
         ③ https://随便哪个镜像/https://raw.github…       别人的镜像也整段换掉
         ④ https://cdn.jsdelivr.net/gh/<o>/<r>@<ref>/…     作者模板现在的写法，先还原成 raw
       第二步再兜底处理非原始文件主机（github.com/gist.github.com）上显式写了前缀的。
       对已经是中转链接的文本重复执行不会套娃（前缀段会把旧的中转前缀一并吃掉再补上）。"""
    p = _ghrelay_prefix()
    if not p:
        return text
    text = _GH_URL_RE.sub(lambda m: p + m.group(1), _jsd_to_raw(text))
    return text.replace("https://gh-proxy.com/", p)


# 自定义模板常被贴成「网页」链接——GitHub 文件页 / gist 页面，拉下来是一整页 HTML，
# 不是模板本身。这里把它们换成对应的原始文件链接（前面带了镜像前缀的也认，前缀保留）：
#   https://github.com/<o>/<r>/blob/<ref>/<path>[?plain=1][#L10] → https://raw.githubusercontent.com/<o>/<r>/<ref>/<path>
#   https://gist.github.com/<user>/<id>                         → https://gist.githubusercontent.com/<user>/<id>/raw
#   （gist 的 /raw 不带文件名时给的是第一个文件；多文件 gist 请直接贴那个文件的 Raw 链接）
# github.com/<o>/<r>/raw/... 本来就会跳到原始文件，不用改。
_GH_BLOB_RE   = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/blob/([^?#\s]+)")
_GIST_PAGE_RE = re.compile(r"https://gist\.github\.com/([^/\s]+)/([0-9A-Fa-f]+)/?(?:[?#]\S*)?$")

def tpl_raw_url(url):
    """模板链接若是 GitHub 文件页 / gist 页面，换成原始文件链接；其它原样返回。"""
    u = (url or "").strip()
    m = _GH_BLOB_RE.search(u)
    if m:
        o, r, rest = m.groups()
        return u[:m.start()] + f"https://raw.githubusercontent.com/{o}/{r}/{rest}"
    m = _GIST_PAGE_RE.search(u)
    if m:
        return u[:m.start()] + f"https://gist.githubusercontent.com/{m.group(1)}/{m.group(2)}/raw"
    return u

def fetch_tpl(url):
    """拉模板：GitHub 链接中转改写 + 老锚点名归一。三个格式的生成器都走这里。
       页面链接先换成原始文件链接（老版本存下的自定义链接也照样能用）；
       拉到的若还是网页，直接报错——调用方会回滚、保留原配置，不会把一页 HTML 当模板写进订阅。"""
    raw = fetch_url(tpl_raw_url(url))
    if raw.lstrip()[:64].lower().startswith(("<!doctype html", "<html")):
        raise RuntimeError("模板链接打开是网页、不是模板文件，请改用 Raw（原始文件）链接")
    t = _ghrelay_rewrite(raw)
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

def _mihomo_direct_dns(tpl, targets):
    """mihomo：自己的域名（本机 / 各节点，含 AdGuard DoT 的 <ClientID>.域名）直连时用国内 DoH 解析，不用 system。

       仓库主人 10/15：安卓私人 DNS 填自建 DoT，sing-box 能连、Bettbox（mihomo）规则模式连不上。两边规则一样，
       都把自己的域名判直连（_mihomo_direct_ip / _sb_direct_ip）；差在直连前怎么把域名解析成 IP——
       sing-box 用 default_domain_resolver（阿里 DoH，走直连）；mihomo 用 direct-nameserver: system。
       安卓上开着 VPN 时，「系统 DNS」可能就是代理软件自己那个回假 IP 的 DNS（或又绕回私人 DNS 本身），
       直连拿着假 IP 去连，当然连不上。

       模板开着 direct-nameserver-follow-policy（直连解析也跟 nameserver-policy 走），所以在 nameserver-policy
       最前面给自己的域名指定 *cn-dns 就够了；没有 cn-dns 锚点的自定义模板用写死的阿里 / 腾讯 DoH。
       只动自己的域名，别的直连解析照旧用 system。没有 nameserver-policy 段的模板原样返回。"""
    doms = [v for k, v in targets if k == "domain"]
    m = re.search(r"(?m)^([ \t]*)nameserver-policy:[ \t]*$", tpl)
    if not doms or not m:
        return tpl
    nxt = re.match(r"\n([ \t]+)\S", tpl[m.end():])           # 跟着下一条的缩进走
    ind = nxt.group(1) if nxt else m.group(1) + "  "
    val = "*cn-dns" if re.search(r"&cn-dns\b", tpl) else '["https://223.5.5.5/dns-query", "https://doh.pub/dns-query"]'
    add = "".join(f'\n{ind}"+.{d}": {val}' for d in doms if f'"+.{d}"' not in tpl)
    return tpl[:m.end()] + add + tpl[m.end():] if add else tpl

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

       为什么是把它换掉而不是像 mihomo/小火箭那样"插在列表最前、原有的留着"：sing-box 的
       一条 dns 规则只指一个 server tag，没有列表可插。换掉也不伤日常解析——兜底只接
       没被任何规则分到的查询：作者模板里 A/AAAA 先给 fakeip（直连的再用 local 解真实 IP，
       走代理的交给服务端）、局域网走 local、「全局」走 remote，落到兜底的只剩 MX / TXT 这类
       零头。AdGuard 挂了也就这部分查不到（仓库主人 10/12：「不要瞎说什么 sing-box 解析不了」，
       以前这里和开关菜单都写成了「挂了 sing-box 就解析不了」）。

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

def _privdns_used(nodes):
    """这份配置里真用到的私有名 → 它的 DNS 地址（按名字排好，生成结果稳定）。"""
    return dict(sorted({str(d.get("server")): d["_privdns"] for _, d in nodes if d.get("_privdns")}.items()))

def _yaml_block_end(tpl, m, ind):
    """YAML 里 m 那一行的值到哪结束：后面缩进更深的行、或同缩进的 `- ` 列表项都算它的（空行 / 注释不算终点）。"""
    end, pos = m.end(), m.end()
    for line in tpl[m.end():].split("\n")[1:]:
        pos += 1 + len(line)
        st = line.strip()
        if not st or st.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip())
        if lead > len(ind) or line.startswith(ind + "-"):
            end = pos
            continue
        break
    return end

def _mihomo_privdns(tpl, nodes):
    """mihomo：被藏节点的私有名只去问它的 DNS —— proxy-server-nameserver-policy（mihomo 1.19.20 起才有）。
       模板写了 proxy-server-nameserver 才写这一项（mihomo 规定 policy 不能单独出现，见 config.go）；
       自定义模板没写的，写进 nameserver-policy（没有 proxy-server-nameserver 时，mihomo 解析节点地址走默认那套，
       带 nameserver-policy）。两样都没有就说一行：这份里被藏的节点连不上。"""
    used = _privdns_used(nodes)
    if not used:
        return tpl
    def entries(ind):
        return "".join(f'\n{ind}"{n}": ["{u}"]' for n, u in used.items())
    for key in ("proxy-server-nameserver-policy", None, "nameserver-policy"):
        if key is None:                                   # 模板没有这一段：跟在 proxy-server-nameserver 那一项后面新开
            m = re.search(r"(?m)^([ \t]*)proxy-server-nameserver:.*$", tpl)
            if m:
                ind = m.group(1)
                end = _yaml_block_end(tpl, m, ind)
                return tpl[:end] + f"\n{ind}proxy-server-nameserver-policy:" + entries(ind + "  ") + tpl[end:]
            continue
        m = re.search(rf"(?m)^([ \t]*){key}:[ \t]*$", tpl)
        if m:
            nxt = re.match(r"\n([ \t]+)\S", tpl[m.end():])   # 跟着下一条的缩进走
            ind = nxt.group(1) if nxt and len(nxt.group(1)) > len(m.group(1)) else m.group(1) + "  "
            return tpl[:m.end()] + entries(ind) + tpl[m.end():]
    print("  ⚠ mihomo 模板里没有 proxy-server-nameserver / nameserver-policy，被藏的节点这份里连不上")
    return tpl

def _sb_privdns(cfg, nodes):
    """sing-box：每台 DNS 机加一个 DoH 服务器（写法同 _sb_selfdns：模板里的 bootstrap 解析它的域名、走直连——
       走代理就成了「连节点要先解析、解析又要连节点」的套娃），被藏的节点各自用 domain_resolver 指过去
       （sing-box 1.12 起的出站字段；不指的话走 route.default_domain_resolver 那台国内 DNS，私有名查不到）。"""
    used = _privdns_used(nodes)
    if not used:
        return
    dns = cfg.get("dns")
    if not isinstance(dns, dict) or not isinstance(dns.get("servers"), list):
        print("  ⚠ sing-box 模板里没有 dns.servers，被藏的节点这份里连不上")
        return
    servers = dns["servers"]
    stags = {o.get("tag") for o in servers if isinstance(o, dict)}
    otags = {o.get("tag") for o in cfg.get("outbounds", []) if isinstance(o, dict)}
    direct = "🎯直连" if "🎯直连" in otags else next((t for t in otags if t and "直连" in str(t)), "")
    tag_of = {}
    for url in dict.fromkeys(used.values()):
        m = _PRIVDNS_DNS.match(url)
        n, tag = 1, "privdns"
        while tag in stags:
            n += 1; tag = f"privdns{n}"
        srv = {"tag": tag, "type": "https", "server": m.group(1)}
        if m.group(2) and m.group(2) != "443":
            srv["server_port"] = int(m.group(2))
        srv["path"] = m.group(3)
        if "bootstrap" in stags:
            srv["domain_resolver"] = "bootstrap"
        if direct:
            srv["detour"] = direct
        servers.append(srv)
        stags.add(tag)
        tag_of[url] = tag
    for o in cfg.get("outbounds", []):
        if isinstance(o, dict) and o.get("server") in used:
            o["domain_resolver"] = tag_of[used[o["server"]]]

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
    tpl = _mihomo_cn_isolate(tpl, _node_names(nodes) + static)  # 中国节点：进 🎯直连、出国组剔除
    tpl = _mihomo_direct_ip(tpl, _direct_targets(nodes))       # 各 VPS IP 直连（防管理时 SSH 走代理）
    tpl = _mihomo_direct_dns(tpl, _direct_targets(nodes))      # 自己的域名直连时用国内 DoH 解析（别走 system）
    tpl = _mihomo_selfdns(tpl, _selfdns_doh())                 # 开关开启：把本机自建 DoH 加进 DNS（带兜底）
    tpl = _mihomo_privdns(tpl, nodes)                          # 被藏节点的私有名只去问它的 DNS
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
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode(errors="ignore")

def peer_status(url):
    """探测成员链接可达性，返回 (是否通, 给人看的说明)。

       原来所有失败都压成一个「不通」，到底是机器没开、端口被墙、证书过期还是
       token 换了，全看不出来，只能一台台上去翻——分清楚这几类，一眼就知道去哪查。"""
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
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
    new = _ask_free("  新前缀（回车取消；输 - 表示不要前缀）：", confirm=False).strip()   # 下面有预览 + y 确认
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

def _group_by_prefix(links):
    """名称前缀一样的节点排在一起（只在生成配置时排，节点文件不动）。

       仓库主人：「新加的节点在写进singbox配置的时候这个节点会写到最后面去……能不能在写节点的时候
       名称前缀是一样的写在一起」。节点文件是按装的先后追加的：先装的主节点、后写进订阅的 CDN 节点
       和优选候选、再后来增量加的协议——新加的那条就跟在 CDN 后面，客户端里排到最后。
         · 前缀按第一次出现的先后排（本机的在前、成员机的在后，跟原来一样）
         · 同一前缀里主节点在前，CDN 节点（含优选）在后
         · 别的都保持原来的先后
       认不出协议段的自定义名字自成一组，跟在前面那些组后面。"""
    first, keyed = {}, []
    for i, u in enumerate(links):
        pfx, seg = _split_tag(_link_name(u))
        grp = pfx if pfx is not None else f"\0{i}"
        first.setdefault(grp, i)
        keyed.append((first[grp], 1 if (seg or "").startswith("CDN·") else 0, i, u))
    return [k[-1] for k in sorted(keyed, key=lambda k: k[:3])]

# ---------------------------------------------------------------------------- 节点私有解析（13 → 11）
# 仓库主人 10/28：「有些机场他建的节点必须要用他自己对应的私有dns解析才可以解析」，想让没建 DNS 的机器（1、2）
# 的节点交给建了 DNS 的机器（3）解析。13 → 11 三选一：1 本机DNS解析 / 2 外机DNS解析 / 3 关。
#   · 1、2 选「外机DNS解析」、粘贴 3 的 DoH 地址：本机起一个固定私有名（xxxx.internal），连同 3 的地址写进
#     本机 .links 末尾一行 `#privdns 名字 地址`（主机只认节点链接前缀，老版主机自动忽略这一行）。
#   · 3 选「本机DNS解析」：汇总时拉到 1 的 .links（1 的真实 IP 本来就在里面），把「1 的私有名 → 1 的 IP」写进
#     本机 AdGuard 的自定义规则。「主机把 IP 带过去」走的就是现成的聚合通道，不另开口子。
#   · 谁汇总 1，就把 1 的节点地址换成私有名，配置里再写一句「这个名字只去问 3」。换之前先去 3 问一遍：答出来的
#     IP 跟 1 的链接对得上才换；答不对、问不到，这一轮照写真实地址，保证能连。
# 用 IP 装的机器：私有名是 xxxx.internal（.internal 是 ICANN 2024 年留给内网的后缀：公网 DNS 永远查不到，也没人能注册）；
# 只换别的机器汇总它的那份——它自己的订阅地址就是 http://本机IP:端口/…，自己那份藏了也白藏。
# 用域名装的机器（仓库主人 10/28：「节点用这个令牌的另外DNS来建，主要的是公网查不出节点的信息」）：节点改用
# 「随机.根域名」这个节点专用域名，公网不建任何记录；共用证书多签一个 *.根域名 盖住它（Cloudflare 令牌、DNS 验证，
# 不用公网记录），所有重签证书的地方都带着它（cert_want_names / ensure_acme / cert_fix_run，adguard-dns.py 菜单 8 和撤销）。
# 公网那条记录照留：订阅、面板、伪装站、Emby 照用。它自己的订阅也换（证书盖得住、DNS 答对了才换，见 _privdns_local_meta）。
# 客户端：mihomo 要 1.19.20+（proxy-server-nameserver-policy；老内核不认这一项，名字会去问公共 DNS，公共 DNS 回
# 「查无此名」它也当有效答案——dns/util.go 的 batchExchange 只把 SERVFAIL / REFUSED 当失败——时通时不通）；
# sing-box 1.12+（出站的 domain_resolver）；小火箭还没在手机上测出写法，被藏的节点小火箭那份先不放（仓库主人 10/28 同意）。
PRIVDNS_ZONE  = "internal"
PRIVDNS_GRACE = 7 * 86400        # 外机关掉 / 删掉 / 拉不到以后，映射再留 7 天：没刷新订阅的客户端不会马上断
_PRIVDNS_MODES = {"off": "关", "self": "本机", "remote": "外机"}
_PRIVDNS_NAME = re.compile(r"^[a-z0-9]{6,32}\.(?:internal|(?:[a-z0-9-]+\.)+[a-z]{2,})$")   # xxx.internal 或 随机.根域名
_PRIVDNS_DNS  = re.compile(r"^https://([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::(\d{1,5}))?(/dns-query(?:/[A-Za-z0-9-]+)?)$")
_PRIVDNS_META = re.compile(r"(?m)^#privdns[ \t]+(\S+)[ \t]+(\S+)[ \t]*$")
_PRIVDNS_BEGIN, _PRIVDNS_END = "! xy-privdns begin", "! xy-privdns end"     # AdGuard 规则里圈住我们那一段
_PRIVDNS_LIVE = {}               # 这一轮汇总里真换成私有名的：{私有名: DNS 地址}（aggregated_links 填，parse_nodes 标到节点上）

def privdns_conf():
    """13 → 11 的设置：{"mode": off / self（本机DNS解析）/ remote（外机DNS解析）, "dns": 外机 DoH 地址, "name": 本机私有名}。"""
    try:
        c = json.load(open(PRIVDNS_FILE))
    except Exception:
        c = {}
    if not isinstance(c, dict):
        c = {}
    return {"mode": c.get("mode") if c.get("mode") in ("self", "remote") else "off",
            "dns": str(c.get("dns") or ""), "name": str(c.get("name") or "")}

def privdns_save(c):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump({k: c.get(k, "") for k in ("mode", "dns", "name")}, open(PRIVDNS_FILE, "w"))
    harden_perms()                # 刚落地是 0644，当场收紧

def _privdns_new_name(root=""):
    """本机私有名：10 位小写字母数字 + .internal（用 IP 装的）或 + .根域名（用域名装的，节点专用域名）。
       从关切到开时起一个，之后一直用它——客户端配置里写的就是它，换 IP、换外机地址、本机 / 外机互换都不变
       （关了再开会换新的：整机克隆来的机器私有名会重，这样就分开了）。"""
    abc = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(abc) for _ in range(10)) + "." + (root or PRIVDNS_ZONE)

def _privdns_is_domain_name(n):
    """是不是节点专用域名（随机.根域名，用域名装的机器）——不是 xxx.internal。"""
    return bool(_PRIVDNS_NAME.match(n or "")) and not n.endswith("." + PRIVDNS_ZONE)

def _privdns_root():
    """证书要多签的 *.根域名 的那个根域名：本机开着私有解析、用的是节点专用域名才有；没有返回 ''。
       重签共用证书的地方都靠它带上 *.根域名（漏一处，节点专用域名就握手失败——见 _privdns_local_meta 的兜底）。"""
    c = privdns_conf()
    if c["mode"] == "off" or not _privdns_is_domain_name(c["name"]):
        return ""
    root = c["name"].split(".", 1)[1]
    nd = node_domain()
    return root if not nd or _root_domain(nd) == root else ""      # 换过节点域名（根域名变了）：旧的不再带

def _privdns_nginx_name():
    """nginx 443 那个 server_name 要不要带上节点专用域名：证书盖得住就带（关掉以后也带着——还没刷新订阅的
       客户端照样用它连，带着不碍事）。"""
    n = privdns_conf()["name"]
    return n if _privdns_is_domain_name(n) and _name_covers(cert_names(), n) else ""

def _privdns_local_meta(quiet=False):
    """本机自己的节点要不要换成节点专用域名（用域名装的机器开了私有解析才换）→ (私有名, DNS 地址) 或 None。
       证书没盖住这个名字（重签时 *.根域名 被弄丢了）就不换、说一行：节点照用公网域名，照样能连。"""
    c = privdns_conf()
    n = c["name"]
    if c["mode"] == "off" or not _privdns_is_domain_name(n):
        return None
    if not _name_covers(cert_names(), n):
        if not quiet:
            print(f"  ⚠ 证书里没有 *.{n.split('.', 1)[1]}，本机节点这次照用公网域名（13 → 11 再选一次就补签）")
        return None
    dns = c["dns"] if c["mode"] == "remote" else _agh_doh_url()
    return (n, dns) if _PRIVDNS_DNS.match(dns or "") else None

def _privdns_links_line():
    """本机 .links 末尾那一行（`#privdns 私有名 DNS地址`），汇总本机的主机照它换名：
       用 IP 装的选了外机DNS解析才有；用域名装的开着（本机 / 外机）、证书盖得住节点专用域名才有。"""
    c = privdns_conf()
    if c["mode"] == "remote" and c["name"].endswith("." + PRIVDNS_ZONE) and _PRIVDNS_DNS.match(c["dns"]):
        return f"#privdns {c['name']} {c['dns']}"
    meta = _privdns_local_meta(quiet=True)
    return f"#privdns {meta[0]} {meta[1]}" if meta else ""

def _write_links_file(local):
    """.links 端点的内容：本机分享链接一行一条；选了外机DNS解析的末尾多一行 #privdns（见 _privdns_links_line）。
       链接里照写真实 IP：替本机解析的那台要靠它知道本机的 IP。"""
    if not local:
        return
    extra = _privdns_links_line()
    open(LINKS_FILE, "w").write("\n".join(list(local) + ([extra] if extra else [])) + "\n")

def _privdns_meta(text):
    """成员机 .links 里的 `#privdns` 行 → (私有名, DNS 地址)；没有 / 写错返回 None。"""
    m = _PRIVDNS_META.search(text or "")
    if not m or not _PRIVDNS_NAME.match(m.group(1)) or not _PRIVDNS_DNS.match(m.group(2)):
        return None
    return m.group(1), m.group(2)

def _link_set_server(u, host):
    """把分享链接里的服务器地址换成 host，别的一个字节不动（vmess 改 base64 JSON 里的 add）。"""
    if u.startswith("vmess://"):
        b = u[8:]
        j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        j["add"] = host
        return vmess_link(j)
    m = re.match(r"^([A-Za-z0-9+.-]+://)([^/?#]*)(.*)$", u, re.S)
    ui, at, hp = m.group(2).rpartition("@")
    port = re.match(r"^(?:\[[^\]]*\]|[^:]*)(:\d+)?$", hp)
    return m.group(1) + ui + at + host + ((port.group(1) or "") if port else "") + m.group(3)

def _privdns_ok_link(u):
    """换了地址也不露馅：要加密的都写明了 SNI、ws 类都写明了 Host。没写的话客户端拿地址顶上，私有名就明文
       出现在握手里（链路上一眼就是 xxx.internal），还连不上——这种机器整台不藏。
       本脚本生成的链接都写明了（见 tls_host）；防的是成员机跑着老脚本、或者链接是手抄进去的。"""
    if u.startswith("vmess://"):
        try:
            b = u[8:]
            j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        except Exception:
            return False
        if j.get("tls") == "tls" and not (j.get("sni") or j.get("host")):
            return False
        return not (j.get("net") in ("ws", "httpupgrade") and not j.get("host"))
    P = urllib.parse.urlsplit(u)
    q = {k: v[0] for k, v in urllib.parse.parse_qs(P.query).items()}
    if P.scheme == "ss":
        return "@" in P.netloc                           # SIP002；老式整串 base64 的认不准，不藏
    if P.scheme not in ("vless", "trojan", "hysteria2", "hy2", "tuic", "anytls") or not q.get("sni"):
        return False                                     # 这几种 link_to_proxy 都只认 sni=（vless 一律按 TLS 写）
    return not (q.get("type") in ("ws", "httpupgrade") and not q.get("host"))

def _link_rehost(u, old, new):
    """节点链接换地址：服务器地址换成 new；SNI / Host 原来就是这个地址的（用域名装的机器）一起换成 new，
       借来的伪装域名（reality、自签那个 SNI）不动。节点名（# 后面）不碰。"""
    if u.startswith("vmess://"):
        b = u[8:]
        j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
        j["add"] = new
        for k in ("sni", "host"):
            if old and str(j.get(k) or "").lower() == old.lower():
                j[k] = new
        return vmess_link(j)
    v = _link_set_server(u, new)
    if old and not _is_ip(old):
        head, sep, frag = v.partition("#")
        head = re.sub(r"(?i)([?&](?:sni|host|peer)=)" + re.escape(old) + r"(?=&|$)",
                      lambda m_: m_.group(1) + new, head)
        v = head + sep + frag
    return v

def _privdns_addrs(links, name=""):
    """一台机器的节点能不能藏 → (节点链接里现在写的地址, [IP])；不能藏返回 ("", [])。
       · 用 IP 装的：地址全是 IP，私有名得是 xxx.internal。
       · 用域名装的：地址都是同一个域名，私有名得是这个域名的根域名底下的「随机.根域名」；IP 从公网解析这个域名拿
         （公网那条记录照留）。核对根域名是防成员机报个 xxx.别人家.com 过来，把 AdGuard 变成劫持别人域名的。
       每条都得能放心换名（_privdns_ok_link）；CDN 节点连的是 Cloudflare，本来就不露 IP，照旧不动。"""
    srv = []
    for u in links:
        if "CDN·" in _link_name(u):
            continue
        a = _link_addr(u)
        s = (a[0] if a else "").lower()
        if not s or not _privdns_ok_link(u):
            return "", []
        if s not in srv:
            srv.append(s)
    if not srv:
        return "", []
    if all(_is_ip(s) for s in srv):
        if name and not name.endswith("." + PRIVDNS_ZONE):
            return "", []
        try:
            return srv[0], list(dict.fromkeys(str(ipaddress.ip_address(s)) for s in srv))
        except ValueError:
            return "", []
    dom = srv[0]
    if len(srv) != 1 or (name and (not name.endswith("." + _root_domain(dom)) or name == dom)):
        return "", []
    try:
        ips = sorted({str(ipaddress.ip_address(x[4][0].split("%")[0])) for x in socket.getaddrinfo(dom, None)})
    except Exception:
        return "", []
    return dom, ips

def _privdns_ips(links):
    """_privdns_addrs 只要 IP 那一半。"""
    return _privdns_addrs(links)[1]

def _privdns_label(links):
    """屏上 / 日志里怎么叫这台：节点名前缀（🇺🇸1 这种）；拆不出来就叫「成员机」。"""
    for u in links:
        pre = _split_tag(_link_name(u))[0]
        if pre:
            return pre
    return "成员机"

def _dns_wire(name, qtype):
    """最小的 DNS 查询报文：ID 0、要递归、一个问题（DoH 规定 ID 用 0，方便缓存）。"""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split("."))
    return struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0) + q + b"\0" + struct.pack(">HH", qtype, 1)

def _dns_answer_ips(msg, qtype):
    """DNS 回复报文 → (rcode, [答案里 qtype 那一类的 IP])。A = 1，AAAA = 28。"""
    if len(msg) < 12:
        raise ValueError("不是 DNS 回复")
    rcode = msg[3] & 0x0F
    qd, an = struct.unpack(">HH", msg[4:8])
    i = 12
    def skip(i):
        while True:
            n = msg[i]
            if n == 0:
                return i + 1
            if n & 0xC0 == 0xC0:
                return i + 2
            i += n + 1
    for _ in range(qd):
        i = skip(i) + 4
    ips = []
    for _ in range(an):
        i = skip(i)
        t, _c, _ttl, ln = struct.unpack(">HHIH", msg[i:i + 10])
        i += 10
        rd = msg[i:i + ln]
        i += ln
        if t == qtype == 1 and ln == 4:
            ips.append(socket.inet_ntop(socket.AF_INET, rd))
        elif t == qtype == 28 and ln == 16:
            ips.append(socket.inet_ntop(socket.AF_INET6, rd))
    return rcode, ips

def _doh_ask(url, name, qtype=1, local=None, timeout=6):
    """向 DoH 地址问一次 name → (rcode, [IP])。连不上、证书不对、不回 200 都抛异常。
       local：问的是本机 AdGuard 时直接连本机地址（照样按域名握手、验证书），不绕公网转一圈。
       HTTP/1.0 发出去：对面回完就关连接，不用管分块传输。出网用中性 UA。"""
    m = _PRIVDNS_DNS.match(url)
    if not m:
        raise ValueError("不是 DNS 地址")
    host, port, path = m.group(1), int(m.group(2) or 443), m.group(3)
    q = _dns_wire(name, qtype)
    req = (f"POST {path} HTTP/1.0\r\nHost: {host}:{port}\r\nUser-Agent: {HTTP_UA}\r\n"
           f"Content-Type: application/dns-message\r\nAccept: application/dns-message\r\n"
           f"Content-Length: {len(q)}\r\n\r\n").encode() + q
    ctx = ssl.create_default_context()
    buf = b""
    with socket.create_connection((local or host, port), timeout=timeout) as raw, \
         ctx.wrap_socket(raw, server_hostname=host) as t:
        t.sendall(req)
        while True:
            try:
                d = t.recv(65536)
            except (ssl.SSLError, OSError):
                if buf:
                    break                                # 对面没发 close_notify 就关了：数据已经收全
                raise
            if not d:
                break
            buf += d
    head, _, body = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if not re.match(r"^HTTP/\S+ 200\b", status):
        raise OSError(f"DNS 回 {status[9:12] or '空'}")
    return _dns_answer_ips(body, qtype)

def _agh_doh_url():
    """本机 AdGuard 的 DoH 地址（选「本机DNS解析」后给外机粘贴的那一行）：装了 AdGuard、开了加密、本机是域名才有，
       否则 ''。末段带 ClientID，跟「自建 DNS 写入订阅」同一个（见 selfdns_clientid）。"""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") or not os.path.exists("/opt/AdGuardHome/AdGuardHome"):
        return ""
    t = _agh_tls()
    if t.get("enabled") != "true" or (t.get("server_name") and t["server_name"] != dom):
        return ""
    try:
        port = int(t.get("port_https") or 0)
    except ValueError:
        port = 0
    return f"https://{dom}:{port}/dns-query/{selfdns_clientid()}" if port else ""

def _agh_local_addr():
    """本机 AdGuard 的 HTTPS（DoH）挂在哪个地址：跟 http.address 同一个，0.0.0.0 / :: / 空 → 127.0.0.1。"""
    try:
        m = re.search(r"(?m)^\s*address:\s+(\S+):(\d+)\s*$", open(AGH_YAML).read())
    except OSError:
        m = None
    h = m.group(1).strip("[]\"'") if m else ""
    return "127.0.0.1" if h in ("", "0.0.0.0", "::") else h

def _privdns_is_own(dns):
    """这个 DNS 地址是不是本机的（按域名认；端口、ClientID 对不对交给去问那一步）。"""
    m = _PRIVDNS_DNS.match(dns or "")
    return bool(m) and m.group(1).lower() == (_host() or "").lower()

def _privdns_check(name, dns, ips):
    """去 DNS 问一遍私有名：答出来的 IP 跟 ips 一个不差返回 ""，否则返回原因（一句话）。"""
    if not ips:
        return "节点地址认不准（还没装节点，或者 SNI / Host 没写明）"
    local = _agh_local_addr() if _privdns_is_own(dns) else None
    try:
        for qt in (1, 28):
            want = sorted(i for i in ips if (":" in i) == (qt == 28))
            if not want:
                continue
            rc, got = _doh_ask(dns, name, qt, local=local)
            if rc == 5:
                return "DNS 拒绝了（ClientID 不对？）"
            if rc == 3 or not got:
                return "DNS 里还没有这个名字"
            if sorted(str(ipaddress.ip_address(g)) for g in got) != want:
                return "DNS 答的 IP 对不上（还没重新汇总？）"
    except Exception as e:
        return f"问不到 DNS（{str(e)[:40] or e.__class__.__name__}）"
    return ""

def _privdns_map():
    try:
        m = json.load(open(PRIVDNS_MAP_FILE))
    except Exception:
        return {}
    return {k: v for k, v in m.items() if _PRIVDNS_NAME.match(k) and isinstance(v, dict) and v.get("ips")} \
        if isinstance(m, dict) else {}

def _yaml_scalar(s):
    """YAML 单行标量去引号（AdGuard 写 user_rules 时单引号、双引号、不加引号都可能）。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s

def _agh_user_rules_put(txt, rules):
    """AdGuardHome.yaml 的 user_rules 里，把我们那一段（_PRIVDNS_BEGIN … _PRIVDNS_END）换成 rules；
       用户自己的规则原样一行不动。认不准写法（一行写完的 [a, b]、跨行的长规则）返回 None，不动它。"""
    lines = txt.split("\n")
    k = next((i for i, l in enumerate(lines) if re.match(r"^user_rules:", l)), None)
    if k is None:
        return None
    rest = lines[k][len("user_rules:"):].strip()
    items, j = [], k + 1
    if rest not in ("", "[]"):
        return None
    if rest == "":
        while j < len(lines):
            mm = re.match(r"^([ \t]*)-(?:[ \t]+(.*))?$", lines[j])
            if not mm:
                break
            items.append((lines[j], len(mm.group(1)), _yaml_scalar(mm.group(2) or "")))
            j += 1
        if items and j < len(lines) and lines[j].strip() and \
                len(lines[j]) - len(lines[j].lstrip()) > items[0][1]:
            return None                                  # 跨行的规则：认不准，宁可不动
    keep, ours, inside = [], [], False
    for raw, _ind, v in items:
        if v == _PRIVDNS_BEGIN:
            inside = True
        elif v == _PRIVDNS_END:
            inside = False
        elif inside:
            ours.append(v)
        else:
            keep.append(raw)
    if inside:
        return None                                      # 只有开头没有结尾（被人手改坏了）：往下全算我们的会删掉用户规则
    if ours == list(rules) and (bool(rules) == (len(keep) < len(items))):
        return txt                                       # 规则没变（AdGuard 换过引号也算没变）：不写、不重启
    ind = " " * (items[0][1] if items else 2)
    add = [f"{ind}- '{r}'" for r in [_PRIVDNS_BEGIN] + list(rules) + [_PRIVDNS_END]] if rules else []
    body = keep + add
    return "\n".join(lines[:k] + ["user_rules:" + ("" if body else " []")] + body + lines[j:])

def _agh_rules_sync(m):
    """映射 → AdGuard 自定义规则（||私有名^$dnsrewrite=NOERROR;A;IP）。改了文件返回 True（要重启 AdGuard 才生效）；
       没装 / 没变 / 认不准 user_rules 写法 → False。"""
    try:
        txt = open(AGH_YAML).read()
    except OSError:
        return False
    rules = [f"||{n}^$dnsrewrite=NOERROR;{'AAAA' if ':' in ip else 'A'};{ip}"
             for n in sorted(m) for ip in m[n]["ips"]]
    new = _agh_user_rules_put(txt, rules)
    if new is None:
        if rules:
            print("  ⚠ AdGuard 的自定义规则写法认不准，私有名没写进去")
        return False
    if new == txt:
        return False
    tmp = AGH_YAML + ".xy-new"
    with open(tmp, "w") as f:
        f.write(new)
    os.chmod(tmp, 0o600)          # 里面现在有别的机器的 IP：只给 root 看（AdGuard 新版自己也是 0600）
    os.replace(tmp, AGH_YAML)
    return True

def _agh_wait(m, secs=20):
    """重启 AdGuard 后等它真开始答（要先加载过滤列表，几秒），最多 secs 秒。没有私有名就只等端口开。"""
    url, local = _agh_doh_url(), _agh_local_addr()
    name = next(iter(sorted(m)), "")
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if url and name:
                ip = m[name]["ips"][0]
                rc, got = _doh_ask(url, name, 28 if ":" in ip else 1, local=local, timeout=3)
                if rc == 0 and got:
                    return
            else:
                port = int(_agh_tls().get("port_https") or 0)
                if not port:
                    return
                socket.create_connection((local, port), timeout=2).close()
                return
        except Exception:
            pass
        time.sleep(1)

def _privdns_sync(groups):
    """本机当 DNS 机（13 → 11 选 1）：把指定了本机的机器（外机，还有用节点专用域名的本机自己）写进
       PRIVDNS_MAP_FILE 和 AdGuard 规则。外机关掉 / 删掉 / 拉不到的，过了 PRIVDNS_GRACE 才删。
       映射里的名字或 IP 变了才重启 AdGuard。重启不碍本机看片：装 AdGuard 时本机解析改走公共 DNS
       （adguard-dns.py 的 _do_free53），本机进程都不经过它，断的只是外面来问的那一两秒。
       groups = [(这台的新链接, meta)]。"""
    old = _privdns_map()
    now = int(time.time())
    m = {k: v for k, v in old.items() if now - int(v.get("seen") or 0) <= PRIVDNS_GRACE}
    if privdns_conf()["mode"] == "self" and _agh_doh_url():
        took = set()
        for links, meta in groups:
            if meta and links and _privdns_is_own(meta[1]):
                ips = _privdns_addrs(links, meta[0])[1]
                if ips and meta[0] in took:
                    # 两台报的私有名一样：多半是整机克隆过来的（privdns.json 跟着拷了）。先到先得，后一台不藏
                    print(f"  ⚠ {_privdns_label(links)} 跟别的机器私有名一样（克隆来的？）：到它的 13 → 11 关了再开一次")
                elif ips:
                    m[meta[0]] = {"ips": ips, "seen": now, "label": _privdns_label(links)}
                    took.add(meta[0])
    changed = _agh_rules_sync(m)
    if m:
        os.makedirs(BGP_DIR, exist_ok=True)
        json.dump(m, open(PRIVDNS_MAP_FILE, "w"), ensure_ascii=False)
        harden_perms()
    elif os.path.exists(PRIVDNS_MAP_FILE):
        os.remove(PRIVDNS_MAP_FILE)                      # 全过期了：规则已撤，以后不用再对
    if changed:
        sh("systemctl restart AdGuardHome", check=False)
        _agh_wait(m)
    return m

def _privdns_apply(groups):
    """汇总时把指定了私有解析的机器的节点换成私有名。groups = [(这台的新链接, meta)]，第一组是本机：
       本机用 IP 装的不换（订阅地址就是本机 IP）；用域名装的开着私有解析就换成节点专用域名（_privdns_local_meta）。
       先当 DNS 机把映射对好，再逐台去它的 DNS 问：对上了才换，问不通照写原来的地址（屏上 / 日志一行 ⚠）。"""
    _PRIVDNS_LIVE.clear()
    if not groups:
        return []
    groups = [(groups[0][0], _privdns_local_meta())] + list(groups[1:])
    mode = privdns_conf()["mode"]
    if mode == "self" or os.path.exists(PRIVDNS_MAP_FILE):
        _privdns_sync(groups)
    out = []
    for links, meta in groups:
        if meta and links:
            name, dns = meta
            old, ips = _privdns_addrs(links, name)
            if _privdns_is_own(dns) and mode != "self":
                why = "本机没开「本机DNS解析」"
            elif not ips:
                why = "节点地址认不准（地址不是同一个、SNI / Host 没写明，或者域名解析不出来）"
            else:
                why = _privdns_check(name, dns, ips)
            if not why:
                links = [u if "CDN·" in _link_name(u) else _link_rehost(u, old, name) for u in links]
                _PRIVDNS_LIVE[name] = dns
            else:
                print(f"  ⚠ {_privdns_label(links)} 私有解析没对上：{why}，这次照写原来的地址")
        out += links
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
    groups = [(links, None)]                            # 一台一组：私有解析按台换名（见 _privdns_apply）
    for u in load_peers():
        try:
            text = _fetch_text(u)
        except Exception:
            continue                                    # 不通就忽略这台
        got = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(_NODE_SCHEMES) and s not in seen:
                seen.add(s); got.append(s)
        groups.append((got, _privdns_meta(text)))
    links = _privdns_apply(groups)                      # 指定了私有解析的成员机：问过它的 DNS、对上了才换成私有名
    links = [_link_rename(u, _sep_name(_link_name(u))) for u in links]   # 统一补「前缀·协议」
    return _dedup_names(_group_by_prefix(links))                        # 同前缀排一起；撞名的再加 ¹²

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
        d["_link"] = u                                   # 原始分享链接（名字已规范化），小火箭节点订阅照抄它
        if d.get("server") in _PRIVDNS_LIVE:
            d["_privdns"] = _PRIVDNS_LIVE[d["server"]]   # 被藏的节点：它的私有名该去问哪台 DNS（三个生成器看这个）
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
    # host 也得在【生成配置之前】记下：中转前缀 / 订阅走不走 https / 本机直连规则，生成器都按 sub.host 判断。
    # 以前是生成完才写——重装时换了域名（或 IP ↔ 域名），配置是照着【上一次】的 host 生成的：
    # 中转一条没有、直连规则还是旧地址（重装模拟实测）。
    if G.get("host"):
        open(HOST_FILE, "w").write(G["host"])
    # 重装换端口必须在【生成配置之前】：配置里规则集 / 图标走本机中转的地址带着订阅端口
    # （_ghrelay_prefix 用 sub_port()）。以前是先按旧端口生成、serve_sub 里才换新端口——
    # 订阅地址是新端口，里面的中转地址全是旧端口，规则集一个都拉不下来（仓库主人实测撞上）。
    if new_token:
        renew_sub_port()
        # 中转 token 一起换（仓库主人 10/20：重装订阅全换新了，中转还是老 token 让人误会）。同样要在生成配置之前
        open(GHRELAY_TOKEN_FILE, "w").write(secrets.token_urlsafe(12))
        harden_perms()
    for ext, meta in FMT.items():
        try:
            meta["gen"](ylines, nodes, tpl_url_current(ext))   # 跟随该格式当前选的模板
        except Exception as e:
            print(f"{meta['label']} 配置生成跳过:", e)
    serve_sub(reset=new_token, renew_port=False)          # 端口上面已经换过
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

# ---------------------------------------------------------------------------- 防火墙
# 对齐 mack-a 的 allowPort：机器开着防火墙时，把节点 / 订阅 / 伪装站要用的端口放行。以前完全不管——
# Ubuntu 开了 ufw、甲骨文那种 INPUT 末尾一条 REJECT 兜底的镜像，装完节点全超时、订阅也拉不下来，
# 看起来跟「IP 被墙」一模一样。规则都带 bgpeer 注释，认得出是谁加的。
FW_TAG = "bgpeer"

def _fw_wanted():
    """本机实际在用的公网端口 → {(端口, tcp/udp)}。只收监听公网的，127.0.0.1 的不用开。"""
    want = set()
    def add(p, proto):
        try:
            p = int(p)
        except (TypeError, ValueError):
            return
        if 0 < p < 65536:
            want.add((p, proto))
    try:
        for ib in json.load(open(f"{SB_DIR}/config.json")).get("inbounds", []):
            if str(ib.get("listen", "")) in ("127.0.0.1", "localhost"):
                continue
            t = ib.get("type")
            if t in ("hysteria2", "hysteria", "tuic"):
                add(ib.get("listen_port"), "udp")      # hy2 端口跳跃靠 PREROUTING DNAT，过滤时已是真实端口
            elif t == "shadowsocks":
                add(ib.get("listen_port"), "tcp"); add(ib.get("listen_port"), "udp")
            else:
                add(ib.get("listen_port"), "tcp")
    except Exception:
        pass
    try:
        for ib in json.load(open(f"{XRAY_DIR}/config.json")).get("inbounds", []):
            if str(ib.get("listen", "")) in ("127.0.0.1", "localhost"):
                continue
            add(ib.get("port"), "tcp")
            if (ib.get("streamSettings") or {}).get("network") == "xhttp":
                add(ib.get("port"), "udp")             # xhttp 能走 h3（mack-a 也开 udp）
    except Exception:
        pass
    try:
        for n in _cdn_load():
            add(n.get("cf_port"), "tcp")
    except Exception:
        pass
    try:
        add(open(SUBPORT_FILE).read().strip(), "tcp")  # 直接读文件：sub_port() 没有时会现挑一个
    except OSError:
        pass
    try:
        ng = open(NGINX_CONF).read()
        if re.search(r"listen\s+80\b", ng):
            add(80, "tcp")
        if re.search(r"listen\s+(?:\[::\]:)?443\b", ng):
            add(443, "tcp")
    except OSError:
        pass
    if os.path.exists(NGINX_STREAM_CONF):
        add(443, "tcp")
    return want

def _link_ports(u):
    """分享链接 → 节点在服务器上占的端口（字符串集合）；hy2 端口跳跃的 mport=起-止 原样带上。认不出返回空集。"""
    try:
        if u.startswith("vmess://"):
            b = u[8:]; j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
            return {str(int(j.get("port")))}
        sp = urllib.parse.urlsplit(u)
        out = {str(sp.port)} if sp.port else set()
        mp = urllib.parse.parse_qs(sp.query).get("mport", [""])[0]
        if re.fullmatch(r"\d+-\d+", mp):
            out.add(mp)
        return out
    except (ValueError, TypeError, json.JSONDecodeError, base64.binascii.Error):
        return set()

def _port_csv(ports):
    """端口集合 → 「443,18047,31000-32000」：单端口按大小排，跳跃段按起点插进去；落在跳跃段里的单端口不重复列。"""
    ps = {str(p) for p in ports}
    rngs = [tuple(map(int, p.split("-"))) for p in ps if "-" in p]
    ps = {p for p in ps if "-" in p or not any(a <= int(p) <= b for a, b in rngs)}
    return ",".join(sorted(ps, key=lambda p: int(p.split("-")[0])))

def cloud_ports():
    """云厂商防火墙（GCP / AWS 安全组这类，本机管不到）要放行的端口：[(标题, 「443,…」)]。
       本机：_fw_wanted 收的（节点 / 订阅 / 伪装站 / CDN 回源）+ hy2 跳跃段；
       聚合的成员机：拉它的 .links，按链接收端口（CDN 节点的端口在 Cloudflare 上，不算）+ 它的订阅端口（主机要从这拉）。
       仓库主人 10/23：GCP 少放行一个端口，trojan² 一直不通，自己从订阅里摘端口漏了。"""
    def label(links, fallback):
        for u in links:
            pre = _split_tag(_link_name(u))[0]
            if pre:
                return pre
        return fallback
    local = read_saved_links()
    mine = {str(p) for p, _ in _fw_wanted()}
    for u in local:
        if "CDN·" not in _link_name(u):
            mine |= {p for p in _link_ports(u) if "-" in p}
    out = [(f"本机 {label(local, '')}".strip(), _port_csv(mine))] if mine else []
    for i, url in enumerate(load_peers(), 1):
        try:
            links = [l.strip() for l in _fetch_text(url, timeout=6).splitlines() if l.strip().startswith(_NODE_SCHEMES)]
        except Exception:
            out.append((f"成员 {i}", "拉不到它的节点链接，到那台机器上看"))
            continue
        ports = set()
        for u in links:
            if "CDN·" not in _link_name(u):
                ports |= _link_ports(u)
        sp = urllib.parse.urlsplit(url).port
        if sp:
            ports.add(str(sp))
        out.append((f"成员 {label(links, str(i))}", _port_csv(ports) if ports else "没有节点"))
    return out

def print_cloud_ports():
    """「云防火墙要放行的端口」那一屏：每台一行高亮标题 + 端口顶格一行（长按复制），台与台之间空一行。"""
    rows = cloud_ports()
    if not rows:
        return
    ui_title("云防火墙要放行的端口（tcp、udp 两栏都填）")
    for i, (t, ports) in enumerate(rows):
        print(_hl("1;36", f"[{t}]"))
        print(ports)
        if i < len(rows) - 1:
            print()

def _ipt_catchall():
    """iptables 的 INPUT 会不会挡新连接；挡的话返回放行规则该怎么加（"-I INPUT 7" / "-A INPUT"），不挡返回 ""。
       兜底 = 不带任何条件的 REJECT/DROP（甲骨文镜像：-A INPUT -j REJECT --reject-with icmp-host-prohibited），
       放行规则插在它前面、而不是顶到第 1 条——前面那些封 IP 的（手动 -s x.x.x.x -j DROP、CrowdSec 的 match-set）
       照样先生效。只丢 INVALID 包、封某个来源 IP 的都不算兜底。默认策略 DROP 又没兜底行：接在最后。"""
    out = sh("iptables -S INPUT", check=False) or ""
    rules = [l.strip() for l in out.splitlines() if l.startswith("-A INPUT")]
    for i, l in enumerate(rules, 1):
        if re.fullmatch(r"-A INPUT(?: -m (?:state|conntrack) --(?:state|ctstate) NEW)? -j (?:REJECT|DROP)\b.*", l):
            return f"-I INPUT {i}"                   # 一条条插在同一位置：兜底行跟着往下挪，全在它前面
    return "-A INPUT" if re.search(r"(?m)^-P INPUT DROP", out) else ""

def fw_allow(want, quiet=False):
    """把 want={(端口, tcp/udp)} 在本机防火墙放行（幂等，已放行的不重复加）。返回新放行了几个。
       ufw 开着 → ufw allow；firewalld 在跑 → firewall-cmd；都没有但 iptables 有拦截兜底 → 插 ACCEPT。
       都没开 → 什么都不做（大多数 VPS 默认全放行）。"""
    if not want:
        return 0
    opened, kind = 0, ""
    ufw = sh("ufw status", check=False) if have("ufw") else ""
    if "Status: active" in (ufw or ""):
        kind = "ufw"
        for p, proto in sorted(want):
            if re.search(rf"(?m)^{p}(?:/{proto})?\s+ALLOW\b", ufw):     # 「80  ALLOW」不带协议 = tcp/udp 都放
                continue
            sh(f"ufw allow {p}/{proto} comment {FW_TAG}", check=False); opened += 1
    elif have("firewall-cmd") and (sh("firewall-cmd --state", check=False) or "").strip() == "running":
        kind = "firewalld"
        cur = (sh("firewall-cmd --permanent --list-ports", check=False) or "").split()
        for p, proto in sorted(want):
            if f"{p}/{proto}" in cur:
                continue
            # 运行时 + 永久各加一遍，不 --reload：重载会让旧版 Docker 丢掉自己的转发规则，
            # Emby / OpenList 容器端口当场断（CLAUDE.md 第六节：不许误伤播放）
            sh(f"firewall-cmd --add-port={p}/{proto}", check=False)
            sh(f"firewall-cmd --permanent --add-port={p}/{proto}", check=False); opened += 1
    elif have("iptables") and _ipt_catchall():
        kind, at = "iptables", _ipt_catchall()
        for p, proto in sorted(want):
            rule = f"-p {proto} --dport {p} -m comment --comment {FW_TAG} -j ACCEPT"
            if subprocess.run(f"iptables -C INPUT {rule}", shell=True, capture_output=True).returncode == 0:
                continue
            sh(f"iptables {at} {rule}", check=False); opened += 1
        if opened and have("netfilter-persistent"):
            sh("netfilter-persistent save", check=False)
    if opened and not quiet:
        print(f"  ✔ 本机防火墙（{kind}）已放行 {opened} 个端口")
    return opened

def fw_sync(quiet=False):
    """节点 / 订阅 / 伪装站实际在用的端口全部放行（见 _fw_wanted）。"""
    return fw_allow(_fw_wanted(), quiet)

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
    _XR_ENTRY.clear(); _XR_ENTRY_FROM_DISK[0] = False    # 全新安装：reality 入口新生成密钥
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
        cfg = f"{SB_DIR}/config.json"
        sb_cfg = {"log": {"level": "info"}, "inbounds": ins, "outbounds": [{"type": "direct"}]}
        sb_fix_dns(sb_cfg)                              # 1.13+ 本机 DNS 不走 resolved（见 sb_fix_dns）
        json.dump(sb_cfg, open(cfg, "w"), indent=2)
        write_service("sing-box", SB_BIN, cfg)

    if xr_names:
        install_xray()
        ins, lks = build(XRAY, xr_names, pin if r443_core == "xray" else {},
                         dup=dup_protos, mark="²"); all_links += lks
        ins = _xr_entry_fixup(ins)                      # grpc² / xhttp² 挂 reality 入口后面时补上回落
        cfg = f"{XRAY_DIR}/config.json"
        json.dump({"log": {"loglevel": "warning"}, "inbounds": ins,
                   "outbounds": [{"protocol": "freedom", "tag": "direct"},
                                 {"protocol": "blackhole", "tag": "block"}]},
                  open(cfg, "w"), indent=2)
        write_service("xray", XRAY_BIN, cfg)

    # 两个核心都建完再写 nginx：xray 的 ws / xhttp-tls / reality-vision 也挂在 443 后面（仓库主人 10/22），
    # 以前在 sing-box 那段里写，xray 的还没登记进 NGINX_WS / NGINX_STREAM
    if G.get("sni_split"):
        ensure_acme()                                   # 确保证书就绪（本地 https server 要用）
        NGINX_WS[:], NGINX_STREAM[:] = _nginx_backends_from_cfg()   # 按刚写好的两份配置（含 reality 入口）
        if not write_nginx_sni_split():                 # 写 http(本地https)+stream(443分流)，失败已回滚
            print("  ⚠ sni-split 生效失败（nginx 已回滚到安全状态）。此时 reality 监听在本地、"
                  "暂不可达；请用 --no-sni-split 重装，或改用 reality-443 直连模式。")
    elif _nginx_front() and NGINX_WS:
        write_nginx_conf()                              # 收集完 ws 家族，写 443 伪装站+反代
    # reality 绑 443 时 nginx 只留 :80 acme stub（续期用），不写 443 块，443 归 reality

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
    out_file = NODE_FILE
    try:
        with open(out_file, "w") as f:
            f.write("\n".join(all_links) + "\n")
    except OSError:
        out_file = None

    # 装完的屏和「2 查看链接」一个样：标题框 → 一条一行标题 + 链接顶格、条间空行 → 三大订阅高亮
    ui_title("分享链接")
    print_node_links(all_links)

    # 生成三格式订阅（mihomo / sing-box / Shadowrocket），各自一条链接
    ok = False
    try:
        sr_set_sub_mode(False)                               # 小火箭节点写在配置里，一个链接（仓库主人 10/12，见 sr_onelink_heal）
        ok = build_subscription(all_links, new_token=True)   # 重装换了节点/域名 → 换 token 刷新订阅
    except Exception as e:
        print("\n订阅生成跳过（不影响节点使用）:", e)
    print_cloud_ports()                                      # 订阅端口这时才定下来，放订阅生成之后、订阅链接之前
    if ok:
        urls = sub_urls_text()
        if out_file:
            open(out_file, "a").write("\n# 订阅链接:\n" + urls + "\n")
        ui_title("订阅链接（按客户端选一条）")
        print_sub_urls()
        ui_line()
        # HTTPS 用真证书；无域名 / 自签时是明文 HTTP（客户端拒绝自签 TLS）。改端口 / 关闭见 xy-sub.service
        ui_tip("订阅含随机 token，请勿外传" + ("" if _sub_https() else "（明文 HTTP）"))
    if out_file:
        print(f"  链接已存到 {out_file}")

    # 记住这次安装（节点不再随重装丢失：下次进安装默认「保持节点、只更新配置」）
    try:
        json.dump({"host": G["host"], "domain": G["domain"], "sni": G["sni"], "sni_xr": G.get("sni_xr", ""),
                   "prefix": G.get("prefix", ""), "hy2_ports": G.get("hy2_ports", ""),
                   "nginx": G.get("nginx", ""), "reality443": G.get("reality443", ""),
                   "sni_split": G.get("sni_split", ""), "smux": G.get("smux", ""),
                   "sb": sb_names, "xray": xr_names},
                  open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass
    fw_sync()                                            # 开着防火墙就把节点 / 订阅端口放行（对齐 mack-a）
    netprofile_sync()                                    # 节点端口交给网络优化保留（装着才叫它）

    install_shortcut()
    sched = setup_core_update_cron()                     # 内核每月自动更新（北京每月2号04:00）
    setup_cfg_auto_cron()                                # 三大配置每天北京 03:20 按当前模板自动更新
    setup_selfupd_cron()                                 # 脚本本体每天北京 02:50 自动更新（菜单 18 开关）
    if sched:
        print(f'内核已设为每月自动更新一次（{_core_update_schedule_str()}）；也可随时进菜单 19 手动立即更新。')
    print('\n下次直接输入 \033[1;32mbgpeer\033[0m 即可打开管理面板。')

# ============================================================================ 管理面板 / 快捷命令
def install_shortcut(content=None):
    """安装 bgpeer 快捷命令：本地存一份脚本，wrapper 每次尽量拉最新再运行。
       content 给了就存它（self-update / 菜单18 传刚下载的新版，强制写）。
       不带 content = 装/重装时存「当前跑的这份」，但只升不降：若磁盘已有更新的一份就留磁盘那份。
       （真机踩过：有人用旧脚本跑重装，把 SELF_LOCAL 降级到连 self-update/SCRIPT_VERSION 都没有的老版本，
         每天 02:50 的自动更新 python3 SELF_LOCAL self-update 再也跑不起来、永远卡死，而 bgpeer 每次临时
         下载最新一直显示新版把问题盖住。降级守卫断掉这条路。）"""
    try:
        os.makedirs(BGP_DIR, exist_ok=True)
        if content is None:
            content = open(__file__).read()
            try:    cur = open(SELF_LOCAL).read()
            except OSError: cur = ""
            if cur and _vt(_script_ver(cur)) > _vt(_script_ver(content)):
                content = cur                            # 磁盘那份更新，别覆盖成旧的
        open(SELF_LOCAL, "w").write(content)
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
    sub_offer_cert_domain()               # 订阅还是 IP、域名证书管理里装了证书 → 问要不要换成证书域名
    ui_title("分享链接")
    # 和订阅里保持一致：同样补上「前缀·协议」的分隔点（只影响显示，NODE_FILE 不动）
    print_node_links([_link_rename(u, _sep_name(_link_name(u))) for u in links])
    print_cloud_ports()
    if _sub_items():
        ui_title("订阅链接（按客户端选一条）")
        print_sub_urls()
    ui_line()
    # 1 只改显示名、不用重装，改完自动刷新三格式订阅；2 三格式各按上面标的模板重生成（见 cfg_refresh_all）
    ui_items([(1, "更换节点名称前缀", None), (2, "刷新三大配置", None), (0, "返回", None)])
    c = _ask("选择: ").strip()
    if c == "1":
        rename_prefix()
    elif c == "2":
        cfg_refresh_all()

def peers_menu():
    """聚合节点链接：顶部显示本机 .links 地址（给别人聚合用），下面加/删成员机链接。
       改完到配置菜单点『更新配置』生效。"""
    # 老安装升级上来还没 .links 端点 → 进来补生成一次，保证本机地址能显示
    if read_saved_links() and not links_url():
        try: serve_sub()
        except Exception: pass
    while True:
        peers = load_peers()
        ui_title("聚合节点链接（多机汇总）")
        # 本机这条绿色、成员机那几条淡蓝 —— 仓库主人：「本机的链接应该做成绿色的，然后被添加的换成淡一点的其他的颜色」
        ui_url("本机 links 链接（给别的主机聚合用）", links_url())
        print()
        print(_hl("1;33", f"▸ 已添加的成员链接（{len(peers)} 条，不通的生成时自动跳过）"))
        for i, u in enumerate(peers, 1):
            ok, why = peer_status(u)
            if i > 1:
                print()
            print(_hl("1;36", f"[{i}] ") + (_hl("1;32", "✓ 连通") if ok else _hl("1;31", f"✗ {why}")))
            print(_hl("94", u))
        if not peers:
            print(_hl("2", "（还没有：到别的机器进本菜单，复制它的本机 links 链接粘进来）"))
        ui_line()
        ui_items([(1, "添加链接", None), (2, "删除链接", None),
                  (3, "刷新本机 links 链接（换 token）", None), (0, "返回", None)])
        ui_tip("加 / 删后到配置菜单点「更新配置」重新汇总")
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
    print()
    ui_items([(1, "作者模板", None), (2, "自定义模板", None), (0, "返回", None)])
    c = _ask("选择: ").strip()
    if c == "1":
        _regen_config(ext, FMT[ext]["author"], "作者")
    elif c == "2":
        url = load_custpl().get(ext)
        if not url:
            print("  还没添加自定义模板链接（先选『4 自定义模板链接』）。"); return
        _regen_config(ext, url, "自定义")

def _cfg_auto_load():
    d = _load_json(CFG_AUTO_FILE)
    return {"off": list(d.get("off") or []), "hand": dict(d.get("hand") or {}), "last": d.get("last", "")}

def _cfg_auto_save(d):
    os.makedirs(BGP_DIR, exist_ok=True)
    json.dump(d, open(CFG_AUTO_FILE, "w"), ensure_ascii=False, indent=2)

def _file_sha(path):
    import hashlib
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return ""

def cfg_auto_on(ext):
    """默认开：没被显式关掉就算开。"""
    return ext not in _cfg_auto_load()["off"]

def cfg_auto_paused(ext):
    """手改过、而且手改的那份还原样在 → 暂停自动更新，免得 03:20 一刷把手改的覆盖掉。
       之后不管哪条路重生成过（3 更新配置 / 多路复用 / 中转 / 自建 DNS），文件一变就自动恢复。"""
    h = _cfg_auto_load()["hand"].get(ext)
    return bool(h) and h == _file_sha(FMT[ext]["file"])

def cfg_auto_set(ext, on):
    d = _cfg_auto_load()
    d["off"] = [e for e in d["off"] if e != ext] + ([] if on else [ext])
    if on:
        d["hand"].pop(ext, None)
    _cfg_auto_save(d)

def _cfg_auto_mark_hand(ext):
    d = _cfg_auto_load(); d["hand"][ext] = _file_sha(FMT[ext]["file"]); _cfg_auto_save(d)

def _cfg_auto_state(ext):
    if not cfg_auto_on(ext):
        return "关"
    return "开（手改过，暂停中）" if cfg_auto_paused(ext) else "开（每天 03:20）"

def setup_cfg_auto_cron():
    """装每天北京 03:20 自动更新三大配置的 cron（时区 / 夏令时换算见 bj_cron_lines，进排队器）。幂等。"""
    try:
        if os.path.abspath(__file__) != SELF_LOCAL and not os.path.exists(SELF_LOCAL):
            os.makedirs(BGP_DIR, exist_ok=True)
            shutil.copy(os.path.abspath(__file__), SELF_LOCAL)
        lines, times = bj_job_lines(*CFG_AUTO_AT, "cfgauto", f"python3 {SELF_LOCAL} cfg-auto >> {CFG_AUTO_LOG} 2>&1")
        txt = (f"# bgpeer 三大配置每日自动更新（北京时间 {CFG_AUTO_AT[0]:02d}:{CFG_AUTO_AT[1]:02d}"
               f" = 本机 {' / '.join(f'{h:02d}:{m:02d}' for h, m in times)}，到点核对北京钟点，进排队器）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               + "".join(l + "\n" for l in lines))
        try:
            if open(CFG_AUTO_CRON).read() == txt:
                return True
        except OSError:
            pass
        open(CFG_AUTO_CRON, "w").write(txt); os.chmod(CFG_AUTO_CRON, 0o644)
        return True
    except OSError:
        return False

def _cfg_auto_due(now=None):
    """今天（北京日期）还没跑过就返回今天的日期，跑过了返回 None。

       钟点不在这里核对了：cron 那行到点已经用北京时间核对过（见 bj_cron_lines），之后进排队器，
       可能排在别的任务后面晚几分钟，也可能被重启打断、开机后才接着跑——这些都该照跑，只防同一天跑两遍。"""
    import datetime
    bj = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(
        datetime.timezone(datetime.timedelta(hours=8)))
    day = bj.strftime("%F")
    return None if _cfg_auto_load().get("last") == day else day

def _regen_fmt(e, ylines, nodes):
    """按该格式【当前选的模板】（作者 / 自定义，见 tpl_url_current）重生成一份；生成或校验失败回滚原文件。
       不动节点、不换 token、不重启服务。返回 (状态, 模板名, 错误)：状态 = "updated" / "same" / "failed"。
       每天 03:20 自动更新、「2 节点链接 / 订阅」里的刷新三大配置都走这里，两边结果一致。"""
    target = FMT[e]["file"]
    which = "自定义" if tpl_src_of(e) == "custom" else "作者"
    old = open(target).read()
    try:
        FMT[e]["gen"](ylines, nodes, tpl_url_current(e))
        ok, err = _validate_generated(e, target)
    except Exception as ex:
        ok, err = False, ex
    if not ok:
        open(target, "w").write(old)
        return "failed", which, err
    return ("same" if open(target).read() == old else "updated"), which, ""

def cfg_auto_run():
    """cron 每天 03:20 调：开着自动更新的格式，按【当前选的模板】（作者 / 自定义）重生成一遍。

       不动节点、不换 token、不重启任何服务——订阅目录里是指向配置文件的软链，文件换了客户端
       下次拉就是新的，xy-sub 不用重启（重启那一下正好有客户端在拉就会失败一次）。
       生成或校验失败就回滚原文件，原来能用的那份不会被弄坏。手改过的格式跳过（见 cfg_auto_paused）。"""
    setup_cfg_auto_cron()
    setup_selfupd_cron()                        # 老装机补上脚本每日自动更新（开关在菜单 18）                       # 本机改过时区 / 跨年了，顺手把 cron 对齐
    if os.path.exists(CORE_CRON_FILE):
        setup_core_update_cron()                # 内核每月那条也一起对齐（老装机是按 6 月写死的）
    _heal_other_crons()
    if _relay_port_heal():                      # 中转地址带着旧端口：重生成（cfg-auto 下面本来也会重生成，先修好兜底）
        print(f"{time.strftime('%F %T')} 已修好配置里的中转地址")
    if _vmess_insec_heal():                     # 自签 vmess 缺跳过证书校验：补上（不进菜单的机器也能修好）
        print(f"{time.strftime('%F %T')} 已修好自签 vmess 节点")
    if _cdn_links_heal():                       # CDN 节点旧格式链接没撤掉、叠出同名两条：只留该有的那份
        print(f"{time.strftime('%F %T')} 已清掉重复的 CDN 节点")
    if _acme_cron_heal():                       # crontab 里 acme.sh 的续期任务没了：补上，不然证书到期全断
        print(f"{time.strftime('%F %T')} 已补上 acme.sh 的证书自动续期任务")
    if fake_site_heal():                        # 伪装站还是老的全网一样的默认页：换成本机生成的（只写静态文件）
        print(f"{time.strftime('%F %T')} 伪装站换成了本机生成的站点")
    if sr_onelink_heal():                       # 小火箭订阅改回一个链接（节点写进配置），只动一次
        print(f"{time.strftime('%F %T')} 小火箭节点改回写在配置里（一个链接）")
    if _sub_server_heal():                      # 订阅小服务换成认 /<token>/<名字>.<ext> 的版本
        print(f"{time.strftime('%F %T')} 订阅服务已更新")
    ng = _nginx_rules_heal()                    # nginx 还是老规则（路径被摸到不回 404 页）：重建一次，平滑重载不断连接
    if ng:
        print(f"{time.strftime('%F %T')} {ng}")
    n = _xray_pq_health()                       # 借用目标站换了短证书链：关掉抗量子签名，不然 xray 的 reality 全断
    if n:
        print(f"{time.strftime('%F %T')} 借用目标站证书链变短，{n} 个 xray reality 节点关掉了抗量子签名（订阅已重建）")
    if privdns_conf()["mode"] != "off" and read_saved_links():
        _write_links_file(read_saved_links())   # 私有解析：证书要是被重签丢了 *.根域名，.links 末尾那行跟着撤，别的主机就不换名了
    day = _cfg_auto_due()
    if not day:
        return                                  # 今天已跑过：安静退出
    ts = time.strftime("%F %T")
    todo = [e for e in FMT if os.path.exists(FMT[e]["file"]) and cfg_auto_on(e)]
    for e in [e for e in todo if cfg_auto_paused(e)]:
        print(f"{ts} {FMT[e]['label']} 手改过，跳过（进菜单「自动更新」可恢复）")
    todo = [e for e in todo if not cfg_auto_paused(e)]
    if not todo or not read_saved_links():
        if privdns_conf()["mode"] == "self" or os.path.exists(PRIVDNS_MAP_FILE):
            G["host"] = _host()
            aggregated_links()                  # 三大配置都没在自动更新：当 DNS 机的映射照样每天对一遍（外机换 IP、到期清理）
        return
    G["host"] = _host()
    ylines, nodes = parse_nodes(aggregated_links())
    if not ylines:
        print(f"{ts} 没有可用节点，跳过"); return
    for e in todo:
        st, which, err = _regen_fmt(e, ylines, nodes)
        if st == "failed":
            print(f"{ts} {FMT[e]['label']} 更新失败，已保留原配置（{which}模板）：{str(err).splitlines()[0] if str(err) else err}")
            continue
        print(f"{ts} {FMT[e]['label']} " + ("无变化" if st == "same" else "已更新") + f"（{which}模板）")
    d = _cfg_auto_load(); d["last"] = day; _cfg_auto_save(d)    # 跑完才记：中途被重启打断，开机会接着跑
    harden_perms()

def cfg_refresh_all():
    """「2 节点链接 / 订阅」→ 刷新三大配置：三个格式各按自己当前选的模板（作者 / 自定义）重生成一遍。
       跟每天 03:20 同一套（_regen_fmt）：不动节点、不换 token、不重启服务，订阅地址不变，失败回滚原配置。
       跟 03:20 不同的两处：自动更新关了的格式也刷（人点的就是要刷）；手改过的格式先红字说清会覆盖，默认不刷。"""
    todo = [e for e in FMT if os.path.exists(FMT[e]["file"])]
    if not todo or not read_saved_links():
        print("  还没有配置可刷新，先『1.安装』。"); return
    for e in [e for e in todo if cfg_auto_paused(e)]:
        ui_warn(f"{FMT[e]['label']} 手改过，刷新会把手改的内容覆盖掉")
        if _ask(f"  刷新 {FMT[e]['label']}? [y/N]: ").strip().lower() not in ("y", "yes"):
            todo.remove(e)
            print(f"  ⚠ {FMT[e]['label']} 没刷新（手改的那份留着）")
    if not todo:
        return
    G["host"] = _host()
    ylines, nodes = parse_nodes(aggregated_links())            # 本机 + 成员机节点（多机聚合），跟「更新配置」一样
    if not ylines:
        print("  ⚠ 没有可用节点，没刷新。"); return
    for e in todo:
        st, which, err = _regen_fmt(e, ylines, nodes)
        if st == "failed":
            print(f"  ⚠ {FMT[e]['label']} 刷新失败，已保留原配置（{which}模板）："
                  f"{str(err).splitlines()[0] if str(err) else err}")
        else:
            print(f"  ✔ {FMT[e]['label']} " + ("已刷新" if st == "updated" else "无变化") + f"（{which}模板）")
    harden_perms()
    ui_tip("订阅地址没变，客户端刷新一次订阅就是新的")

def cfg_auto_menu(ext):
    """菜单里的自动更新开关：开 ⇄ 关；手改暂停中的，问一句再恢复（恢复后 03:20 会覆盖手改）。"""
    if cfg_auto_on(ext) and cfg_auto_paused(ext):
        print("  \033[1;33m提示：恢复后每天 03:20 会按模板重生成，手改的内容会被覆盖\033[0m")
        if _ask("  恢复自动更新? [y/N]: ").lower() in ("y", "yes"):
            cfg_auto_set(ext, True); print("  ✔ 已恢复")
        return
    cfg_auto_set(ext, not cfg_auto_on(ext))
    setup_cfg_auto_cron()
    print(f"  ✔ 自动更新已{'开' if cfg_auto_on(ext) else '关'}")

def sr_mode_menu():
    """「8 小火箭配置」→ 6 小火箭节点：写在配置里（一个链接，默认）⇄ 走节点订阅（两个链接）。两个方向都会让小火箭里有东西没掉，
       红字说清、默认否（CLAUDE.md 第四节）。切完按当前模板重生成小火箭配置；生成失败就切回原样。"""
    on = sr_sub_mode()
    if on:
        ui_warn("改回后节点订阅链接作废：小火箭里那条节点订阅要删掉，不删会留着旧节点")
        q = "  改回节点写在配置里? [y/N]: "
    else:
        ui_warn("改完小火箭配置里不再带节点：没在小火箭里加「节点订阅」的设备会看不到节点")
        q = "  改成节点走订阅? [y/N]: "
    if _ask(q).strip().lower() not in ("y", "yes"):
        return
    keep = open(SR_NODES_FILE).read() if os.path.exists(SR_NODES_FILE) else None
    sr_set_sub_mode(not on)
    which = "自定义" if tpl_src_of("conf") == "custom" else "作者"
    if not _regen_config("conf", tpl_url_current("conf"), which):
        sr_set_sub_mode(on)                                     # 原配置 _regen_config 已保留；节点订阅也放回去
        if keep is not None:
            open(SR_NODES_FILE, "w").write(keep); _sr_nodes_link()
        print("  ⚠ 没切成，小火箭节点还是原来的写法"); return
    if not on:
        print()
        ui_url(SUB_TITLES["srnodes"], sr_nodes_url())
        ui_tip("小火箭里先刷新一次配置，再到首页 ➕ 选 Subscribe 加上面这条")

def config_menu(ext):
    """单个格式的配置子菜单：改配置 / 改订阅(换token) / 更新配置(作者·自定义) / 加自定义模板链接。"""
    meta = FMT[ext]
    if not os.path.exists(meta["file"]):
        print(f"\n还没有 {meta['label']} 配置，请先『1.安装』。"); return
    while True:
        cust = load_custpl().get(ext)
        src = tpl_src_of(ext)
        ui_title(f"{meta['label']} 配置")
        # 当前生效的模板，多路复用 / GitHub 中转 / 自建 DNS 重生成订阅时也用它
        ui_kv("配置文件", meta["file"])
        ui_kv("当前生效", _hl("1;32", "自定义模板" if src == "custom" else "作者模板"))
        print()
        ui_url("订阅链接", sub_url(ext))
        if ext == "conf" and sr_nodes_url():
            print()
            ui_url(SUB_TITLES["srnodes"], sr_nodes_url())
        print()
        ui_url("自定义模板链接", cust, "94")
        ui_line()
        items = [(1, "修改配置（编辑器打开）", None),
                 (2, "修改订阅（换 token）", None),
                 (3, "更新配置（作者 / 自定义模板）", None),
                 (4, "自定义模板链接（添加 / 更换 / 删除）", None),
                 (5, "自动更新", _cfg_auto_state(ext))]
        if ext == "conf":
            items.append((6, "小火箭节点", "走节点订阅（两个链接）" if sr_sub_mode() else "写在配置里（一个链接）"))
        ui_items(items + [(0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            before = _file_sha(meta["file"])
            edit_file(meta["file"])
            if _file_sha(meta["file"]) != before and cfg_auto_on(ext):
                _cfg_auto_mark_hand(ext)                # 手改了：自动更新先停，免得 03:20 覆盖
                ui_tip("手改过，自动更新已暂停（重新生成配置后自动恢复）")
        elif c == "5":
            cfg_auto_menu(ext)
        elif c == "6" and ext == "conf":
            sr_mode_menu()
        elif c == "2":
            ui_tip("换 token 后旧订阅链接立即失效，客户端要重新导入")
            if _ask("  换新 token? [y/N]: ").lower() in ("y", "yes"):
                rotate_token_ext(ext); print("  ✔ 已换")
                ui_url("新订阅链接", sub_url(ext))
                if ext == "conf" and sr_nodes_url():             # 节点订阅跟小火箭配置同一个 token，一起换了
                    ui_url("新" + SUB_TITLES["srnodes"], sr_nodes_url())
        elif c == "3":
            update_one_config(ext)
        elif c == "4":
            cur = load_custpl().get(ext)
            if cur:                                     # 已有链接：给 更换 / 删除 / 返回
                print()
                ui_url("当前自定义模板链接", cur, "94")
                ui_items([(1, "更换", None), (2, "删除（改回作者模板）", None), (0, "返回", None)])
                s = _ask("选择: ").strip()
                if s == "2":
                    del_custpl(ext); set_tplsrc(ext, "author")   # 链接没了，当前选择同步切回作者
                    print("  ✓ 已删除自定义模板链接，之后一律用作者模板。")
                    if _ask("  现在就用作者模板重新生成一次配置? [y/N]: ").lower() in ("y", "yes"):
                        _regen_config(ext, FMT[ext]["author"], "作者")   # 立即生效
                    continue
                if s != "1":
                    continue                            # 0/其它 → 返回，不动原链接
            url = _ask("  自定义模板链接(GitHub / gist，页面链接或 raw 都行；占位符须与作者模板一致): ").strip()
            if url:
                raw = tpl_raw_url(url)
                set_custpl(ext, raw)
                if raw != url:
                    print(f"  ✓ 已保存（网页链接已换成原始文件链接）：{raw}")
                else:
                    print("  ✓ 已保存。之后『3→2 自定义模板』即用它。")
        elif c == "0" or c == "":
            return

def _script_ver(text):
    """从脚本源码里抠 SCRIPT_VERSION；老版本没有这行则返回 '?'。"""
    m = re.search(r'^SCRIPT_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "?"

def _vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))

def _remote_ver(text):
    """脚本源码里的版本号：主体用 SCRIPT_VERSION，net-optimize 那种用 VERSION；没有返回 ''。"""
    m = re.search(r'^(?:SCRIPT_VERSION|VERSION)\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""

def _auto_self_update():
    """脚本本体更新到最新（每天 02:50 的 self-update 调；仓库主人 10/21：有的人不上服务器、不开面板，脚本就一直停在
       老版本，修的 bug、加的功能都用不上）。拉不到、版本没变新、新版连语法都过不了 → 什么都不动。换上了返回 True。"""
    try:
        latest = fetch_url(_RAW + "xy-installer.py")
    except Exception as e:
        print(f"{time.strftime('%F %T')} 脚本自动更新：拉不到新版（{e}），照旧用 v{SCRIPT_VERSION}")
        return False
    new = _script_ver(latest)
    if _vt(new) <= _vt(SCRIPT_VERSION):
        return False                                     # 已是最新（或镜像缓存还是旧的）：不动
    try:
        compile(latest, "xy-installer.py", "exec")         # 下了半截 / 坏文件：不换
    except SyntaxError as e:
        print(f"{time.strftime('%F %T')} 脚本自动更新：v{new} 校验没过（{e}），照旧用 v{SCRIPT_VERSION}")
        return False
    install_shortcut(latest)
    print(f"{time.strftime('%F %T')} 脚本自动更新 v{SCRIPT_VERSION} → v{new}")
    return True

def selfupd_on():
    return not os.path.exists(SELFUPD_OFF)

def setup_selfupd_cron():
    """装 / 撤脚本每天 02:50 自动更新的 cron（按开关；时区 / 夏令时换算见 bj_cron_lines，进排队器）。幂等。"""
    try:
        if not selfupd_on():
            if os.path.exists(SELFUPD_CRON):
                os.remove(SELFUPD_CRON)
            return True
        if os.path.abspath(__file__) != SELF_LOCAL and not os.path.exists(SELF_LOCAL):
            os.makedirs(BGP_DIR, exist_ok=True)
            shutil.copy(os.path.abspath(__file__), SELF_LOCAL)
        lines, times = bj_job_lines(*SELFUPD_AT, "selfupdate", f"python3 {SELF_LOCAL} self-update >> {SELFUPD_LOG} 2>&1")
        txt = (f"# bgpeer 脚本每日自动更新（北京时间 {SELFUPD_AT[0]:02d}:{SELFUPD_AT[1]:02d}"
               f" = 本机 {' / '.join(f'{h:02d}:{m:02d}' for h, m in times)}，到点核对北京钟点，进排队器）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               + "".join(l + "\n" for l in lines))
        try:
            if open(SELFUPD_CRON).read() == txt:
                return True
        except OSError:
            pass
        open(SELFUPD_CRON, "w").write(txt); os.chmod(SELFUPD_CRON, 0o644)
        return True
    except OSError:
        return False

def script_update_menu():
    """菜单 18：立即更新 / 每天自动更新开关。"""
    while True:
        ui_title("更新脚本（不影响节点）")
        ui_kv("当前版本", f"v{SCRIPT_VERSION}")
        ui_line()
        ui_items([(1, "立即更新", None),
                  (2, "自动更新", f"开（每天 {SELFUPD_AT[0]:02d}:{SELFUPD_AT[1]:02d}）" if selfupd_on() else "关"),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            update_script(); return
        if c == "2":
            if selfupd_on():
                open(SELFUPD_OFF, "w").write("1")
            else:
                try: os.remove(SELFUPD_OFF)
                except OSError: pass
            ok = setup_selfupd_cron()
            print(f"  ✔ 自动更新已{'开' if selfupd_on() else '关'}" if ok else "  ⚠ 定时任务没写进去（/etc/cron.d 写不了？）")
            continue
        if c in ("0", ""):
            return

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

def bj_cron_lines(hh, mm, cmd, day=None):
    """北京时间 hh:mm（day 给了 = 每月那一号）→ /etc/cron.d 里的行。

    为什么不能只换算一次：Debian/Ubuntu 的 cron 不认 CRON_TZ，只能按本机时区写；而美国、欧洲、
    澳洲这些地方有夏令时，同一个北京时刻在本机冬天和夏天差一小时（洛杉矶冬 11:00 / 夏 12:00），
    只按装的那天算，换季后就整整偏一小时，还可能撞上别的凌晨任务。
    所以按今年 1 月和 7 月各算一次，两个本机时刻都写；每行前面用北京时间再核对一次
    （TZ=CST-8 是 POSIX 写法，不依赖 tzdata），只有对的那行真跑。没有夏令时的时区只有一行。
    每月任务：本机日期可能跟北京差一天，所以日期字段写 *、改由核对里的「几号」把关。"""
    import datetime
    bj = datetime.timezone(datetime.timedelta(hours=8))
    y = datetime.date.today().year
    times = []
    for mon in (1, 7):
        t = datetime.datetime(y, mon, 15, hh, mm, tzinfo=bj).astimezone()
        if (t.hour, t.minute) not in times:
            times.append((t.hour, t.minute))
    fmt, want = ("\\%d\\%H", f"{day:02d}{hh:02d}") if day else ("\\%H", f"{hh:02d}")
    guard = f'[ "$(TZ=CST-8 date +{fmt})" = "{want}" ] && '
    return [f"{m} {h} * * * root {guard}{cmd}" for h, m in times], times


# ── 凌晨任务排队（bgpeer-jobq）────────────────────────────────────────────────
# 仓库主人：「如果有影响就在后面排队，如果要等服务器重启了排队的就自动跟上进行继续做」。
# 凌晨这几条（cn-block 刷新会重启 sing-box、nginx 升级会重启 nginx、内核更新会重启 sing-box/xray、
# 三大配置自动更新）一律先交给 bgpeer-jobq：一个跑完再跑下一个，按登记顺序；记录落硬盘，
# 重启打断的、没轮到的，开机由 bgpeer-jobq.service 接着跑。xy-installer / cn-block / net-optimize /
# media-stack 各带一份【一模一样】的（谁先装谁写，内容相同就不重写；测试会核对四份一致）。
JOBQ_BIN  = "/usr/local/sbin/bgpeer-jobq"
JOBQ_UNIT = "/etc/systemd/system/bgpeer-jobq.service"
JOBQ_SH = r"""#!/bin/bash
# bgpeer-jobq —— 凌晨定时任务排队器（nodekit 生成，四个脚本各带一份同样的，勿手改）
#   bgpeer-jobq run <任务名> '<命令>'   登记 → 排队 → 前面的跑完再跑（同名任务已在排/在跑就不重复登记）
#   bgpeer-jobq resume                  开机时调：上次重启打断的、还没轮到的，按原先顺序接着跑
#   bgpeer-jobq list                    看队列
# 记录落在硬盘（不是 /run），断电重启也丢不了；登记超过 12 小时还没跑上的丢掉，免得白天冒出凌晨的活。
Q=${BGPEER_JOBQ_DIR:-/var/lib/bgpeer-jobq}
LOCK=${BGPEER_JOBQ_LOCK:-/run/bgpeer-jobq.lock}
LOG=${BGPEER_JOBQ_LOG:-/var/log/bgpeer-jobq.log}
MAX_AGE=${BGPEER_JOBQ_MAX_AGE:-43200}
JOB_TIMEOUT=${BGPEER_JOBQ_TIMEOUT:-14400}   # 4 小时：Emby 每日对齐自己就给了 3 小时
BOOT_WAIT=${BGPEER_JOBQ_BOOT_WAIT:-60}
mkdir -p "$Q" && chmod 700 "$Q"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

queued() {   # 同名任务是否已在排 / 在跑
  local f
  for f in "$Q"/*.job "$Q"/*.run; do
    [ -e "$f" ] && [ "$(head -n1 "$f")" = "$1" ] && return 0
  done
  return 1
}

drain() {    # 拿到锁的那个把整条队按登记顺序跑完；没拿到的等着（= 排队）
  exec 9>"$LOCK"
  flock 9
  local f id cmd t0 rc
  while :; do
    f=$(ls -1 "$Q"/*.job 2>/dev/null | sort | head -n1)
    [ -n "$f" ] || break
    id=$(sed -n 1p "$f"); cmd=$(sed -n 2p "$f")
    if [ $(( $(date +%s) - $(stat -c %Y "$f") )) -gt "$MAX_AGE" ]; then
      log "$id 登记超过 $((MAX_AGE / 3600)) 小时没跑上，丢弃"; rm -f "$f"; continue
    fi
    mv "$f" "${f%.job}.run"                 # 正在跑：这时重启，开机会接着跑它
    t0=$(date +%s); log "$id 开始"
    timeout "$JOB_TIMEOUT" bash -c "$cmd" 9>&-   # 9>&-：锁别漏给任务起的进程，不然锁永远放不掉
    rc=$?
    rm -f "${f%.job}.run"; log "$id 结束 rc=$rc 用时 $(( $(date +%s) - t0 ))s"
  done
  exec 9>&-
}

case "$1" in
  run)
    [ -n "$2" ] && [ -n "$3" ] || { echo "用法: $0 run <任务名> '<命令>'" >&2; exit 2; }
    if queued "$2"; then log "$2 已在队列里，不重复登记"; exit 0; fi
    tmp=$(mktemp "$Q/.new.XXXXXX")
    printf '%s\n%s\n' "$2" "$3" > "$tmp"
    mv "$tmp" "$Q/$(date +%s%N)-$2.job"
    if ! flock -n "$LOCK" true 2>/dev/null; then log "$2 排队：前面还有任务在跑"; fi
    drain
    ;;
  resume)
    n=0
    for f in "$Q"/*.run; do [ -e "$f" ] && mv "$f" "${f%.run}.job" && n=$((n + 1)); done
    m=$(ls -1 "$Q"/*.job 2>/dev/null | wc -l)
    [ "$m" -gt 0 ] || exit 0
    log "开机续跑：被重启打断 $n 个，共 $m 个待跑；等 ${BOOT_WAIT}s 让网络和服务先起来"
    sleep "$BOOT_WAIT"
    drain
    ;;
  list)
    for f in "$Q"/*.job "$Q"/*.run; do
      [ -e "$f" ] && echo "$(basename "$f")  $(sed -n 2p "$f")"
    done
    ;;
  *) echo "用法: $0 run <任务名> '<命令>' | resume | list" >&2; exit 2 ;;
esac
"""
JOBQ_UNIT_TXT = ("[Unit]\nDescription=bgpeer 凌晨任务排队：开机接着跑被重启打断 / 还没轮到的\n"
                 "Wants=network-online.target\nAfter=network-online.target sing-box.service xray.service nginx.service\n"
                 "[Service]\nType=simple\nExecStart=" + JOBQ_BIN + " resume\n"
                 "[Install]\nWantedBy=multi-user.target\n")


def ensure_jobq():
    """装 / 更新排队器和开机续跑服务。内容没变什么都不做。装不上返回 False（调用方退回直接跑）。"""
    try:
        changed = False
        for path, txt, mode in ((JOBQ_BIN, JOBQ_SH, 0o755), (JOBQ_UNIT, JOBQ_UNIT_TXT, 0o644)):
            try:
                same = open(path).read() == txt
            except OSError:
                same = False
            if not same:
                # 写临时文件再换名：排队器可能正在跑（bash 是边跑边读脚本的），原地改写会把它读花
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".new", "w") as f:
                    f.write(txt)
                os.chmod(path + ".new", mode)
                os.replace(path + ".new", path)
                changed = True
            os.chmod(path, mode)
        if changed:
            subprocess.run("systemctl daemon-reload; systemctl enable bgpeer-jobq.service",
                           shell=True, capture_output=True)
        return True
    except OSError:
        return False


def jobq_wrap(job, cmd):
    """把一条命令包成「交给排队器」：排队器不在（被别的卸载删了）就直接跑，至少不丢任务。"""
    import shlex
    return f"if [ -x {JOBQ_BIN} ]; then {JOBQ_BIN} run {job} {shlex.quote(cmd)}; else {cmd}; fi"


def bj_job_lines(hh, mm, job, cmd, day=None):
    """北京时间定时任务的 cron 行：时间换算 + 到点核对（见 bj_cron_lines）+ 交给排队器。"""
    ensure_jobq()
    return bj_cron_lines(hh, mm, jobq_wrap(job, cmd), day=day)


def jobq_cleanup_if_unused():
    """卸载时调：/etc/cron.d 里已经没有任务用排队器了，才把它和开机服务、队列一起撤掉。
       （网络优化是独立模块，卸代理主体时它的 nginx 月度升级还在用排队器，那就留着。）"""
    try:
        for n in os.listdir("/etc/cron.d"):
            try:
                if JOBQ_BIN in open(os.path.join("/etc/cron.d", n)).read():
                    return False
            except OSError:
                pass
    except OSError:
        pass
    subprocess.run("systemctl disable bgpeer-jobq.service", shell=True, capture_output=True)
    for p in (JOBQ_BIN, JOBQ_UNIT):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree("/var/lib/bgpeer-jobq", ignore_errors=True)
    try:
        os.remove("/var/log/bgpeer-jobq.log")
    except OSError:
        pass
    subprocess.run("systemctl daemon-reload", shell=True, capture_output=True)
    return True


def setup_core_update_cron():
    """装每月定点更新内核的 cron：北京时间每月 2 号 04:00（时区 / 夏令时换算见 bj_cron_lines）。幂等。"""
    try:
        if os.path.abspath(__file__) != SELF_LOCAL:      # 确保 cron 调的本地副本存在
            os.makedirs(BGP_DIR, exist_ok=True)
            shutil.copy(os.path.abspath(__file__), SELF_LOCAL)
        lines, times = bj_job_lines(4, 0, "coreupdate", f"python3 {SELF_LOCAL} update-cores >> {CORE_CRON_LOG} 2>&1", day=2)
        txt = (f"# bgpeer 内核每月自动更新（北京时间每月 2 号 04:00 = 本机 "
               f"{' / '.join(f'{h:02d}:{m:02d}' for h, m in times)}，到点核对北京日期和钟点，进排队器）\n"
               "SHELL=/bin/bash\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               + "".join(l + "\n" for l in lines))
        try:
            if open(CORE_CRON_FILE).read() == txt:
                return times
        except OSError:
            pass
        open(CORE_CRON_FILE, "w").write(txt); os.chmod(CORE_CRON_FILE, 0o644)
        return times
    except OSError as e:
        print("  安装内核自动更新 cron 失败（不影响使用）:", e); return None

NETOPT_NGINX_CRON = "/etc/cron.d/net-optimize-nginx-update"
CNBLOCK_CRON      = "/etc/cron.d/bgpeer-cnblock"

def _heal_bj_cron(path, hh, mm, title, job, day=None):
    """别的模块装的北京时间定时任务，老写法按本机时区只换算了一次（或写了 Debian 不认的
       CRON_TZ），夏令时换季 / 改时区后会偏，也没进排队器。这里就地换成 bj_job_lines 的写法，
       命令原样保留。已经进了排队器的就不动。改了返回 True。

       · cn-block 每天 03:00 刷新：新版自己每次刷新也会对齐，但老副本要等它更新
       · net-optimize 每月 1 号 03:10 升级 nginx：老写法 CRON_TZ=Asia/Shanghai，UTC 机器上实际是北京 11:10"""
    try:
        txt = open(path).read()
    except OSError:
        return False
    if JOBQ_BIN in txt:
        return False
    m = re.search(r"^\s*\d+\s+\d+\s+\S+\s+\*\s+\*\s+root\s+(.+)$", txt, re.M)
    if not m:
        return False
    cmd = m.group(1).strip()
    if cmd.startswith("[ ") and " ] && " in cmd:          # 上一版带了北京时间核对：剥掉，下面重新加
        cmd = cmd.split(" ] && ", 1)[1]
    if "'" in cmd:
        return False
    lines, times = bj_job_lines(hh, mm, job, cmd, day=day)
    when = f"每月 {day} 号 " if day else "每天 "
    new = (f"# {title}（北京时间{when}{hh:02d}:{mm:02d} = 本机 "
           f"{' / '.join(f'{h:02d}:{mi:02d}' for h, mi in times)}，到点核对北京时间，进排队器）\n"
           "SHELL=/bin/bash\n"
           "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
           + "".join(l + "\n" for l in lines))
    try:
        open(path, "w").write(new); os.chmod(path, 0o644)
        return True
    except OSError:
        return False

def _heal_other_crons():
    _heal_bj_cron(CNBLOCK_CRON, 3, 0, "bgpeer 屏蔽规则集每日刷新", "cnblock")
    _heal_bj_cron(NETOPT_NGINX_CRON, 3, 10, "Net-Optimize: monthly nginx upgrade", "nginx-upgrade", day=1)

def _core_update_schedule_str():
    """可读描述：北京每月 2 号 04:00，附本机实际触发时刻。"""
    _, times = bj_cron_lines(4, 0, "", day=2)
    return f"北京时间每月 2 号 04:00（本机 {' / '.join(f'{h:02d}:{m:02d}' for h, m in times)}）"

def _xr_ib_uuid(ib):
    """xray 入站第一个用户的 UUID（跟分享链接 vless://UUID@ 对得上）；没有返回 ''。"""
    cl = ((ib.get("settings") or {}).get("clients") or [{}])
    return str((cl[0] or {}).get("id", "")) if cl else ""

def _xray_reality_targets(data):
    """xray 配置里的 reality 入站 → [(入站, realitySettings, 借用目标站)]。"""
    out = []
    for ib in data.get("inbounds", []):
        rs = (ib.get("streamSettings") or {}).get("realitySettings")
        if isinstance(rs, dict) and ib.get("port"):
            names = rs.get("serverNames") or []
            out.append((ib, rs, names[0] if names else str(rs.get("dest", "")).rsplit(":", 1)[0]))
    return out

def _xray_reality_commit(cfg, data, old, pq, restart):
    """写回 xray 配置 → 校验（不过回滚、返回 False）→ 链接改 pqv、订阅重建 → 重启 xray。"""
    json.dump(data, open(cfg, "w"), indent=2)
    if os.path.exists(XRAY_BIN):
        ok, _ = core_check(XRAY_BIN, cfg)
        if not ok:
            open(cfg, "w").write(old)                        # 回滚，绝不留坏配置
            return False
    if pq and _links_set_pqv(pq):
        try:
            G["host"] = _host()
            build_subscription(read_saved_links(), new_token=False)
        except Exception:
            pass
    if restart:
        sh("systemctl restart xray", check=False)
    return True

def _xray_heal_minclientver(restart=True):
    """老机器的 xray reality 入站一次性对齐 mack-a（见 XR_MIN_CLIENT_VER 那段注释）：
       minClientVer 缺的、或还是早先兼容用的 1.0.0 → 1.8.2；同一趟里借用目标站够格的顺手开 ML-DSA-65，
       链接补上 pqv=、订阅重建（客户端刷新订阅后才验签；没刷新的照常连，只是不验）。
       改完要校验，不过就整个回滚；对齐过的再调就是 no-op。
       没改返回 None；改了返回这次开了抗量子签名的节点数。"""
    cfg = f"{XRAY_DIR}/config.json"
    if not os.path.exists(cfg):
        return None
    try:
        old = open(cfg).read()
        data = json.loads(old)
    except Exception:
        return None
    todo = [(ib, rs, sni) for ib, rs, sni in _xray_reality_targets(data)
            if rs.get("minClientVer") in (None, "", "1.0.0")]
    if not todo:
        return None
    pq = {}
    for ib, rs, sni in todo:
        rs["minClientVer"] = XR_MIN_CLIENT_VER
        if rs.get("mldsa65Seed") or not sni or not _reality_pq_ok(sni):
            continue
        try:
            seed, v = _xr_pq_keys()
        except Exception:
            continue
        rs["mldsa65Seed"] = seed
        for u in _xr_link_uuids(data, ib):
            pq[u] = v
    return len(pq) if _xray_reality_commit(cfg, data, old, pq, restart) else None

def _xray_pq_health(restart=True):
    """开着抗量子签名的 xray reality：借用目标站换成短证书链（< PQ_OFF_BELOW）就关掉——
       不关的话所有客户端都握不上手（见 XR_MIN_CLIENT_VER 那段注释）。探不到证书（网络抖一下）不算数，不动。
       链接去掉 pqv=、订阅重建、重启 xray（这时 reality 本来就断着，重启只会让它恢复）。返回关掉的节点数。"""
    cfg = f"{XRAY_DIR}/config.json"
    if not os.path.exists(cfg):
        return 0
    try:
        old = open(cfg).read()
        data = json.loads(old)
    except Exception:
        return 0
    off = {}
    for ib, rs, sni in _xray_reality_targets(data):
        if rs.get("mldsa65Seed") and sni:
            n, _ = _reality_pq_ping(sni)
            if n is not None and n < PQ_OFF_BELOW:
                rs.pop("mldsa65Seed")
                for u in _xr_link_uuids(data, ib):
                    off[u] = ""
    if not off:
        return 0
    return len(off) if _xray_reality_commit(cfg, data, old, off, restart) else 0

# ── sing-box 服务端的本机 DNS ──────────────────────────────────────────────────
# 服务端配置原来不写 dns 段，用 sing-box 默认的本机解析。sing-box 1.13 起，Linux 上的本机解析
# 会改走 systemd-resolved 的 DBus 接口——它判断「这台机归 resolved 管」只看 /etc/resolv.conf
# 开头的注释里有没有 systemd-resolved 字样，【不管服务还在不在】。resolved 被停 / 被卸、
# resolv.conf 却还留着那行注释的机器（不少云镜像都这样），每个连接都卡在：
#   lookup www.gstatic.com: (exchange4: The name org.freedesktop.resolve1 was not provided by any .service files)
# 客户端看就是 sing-box 的节点全部超时，xray 的照常（xray 自己读 resolv.conf 里的 nameserver）。
# 内核每月自动更新，1.12 → 1.13 的那天就突然全挂（2026-10 真机）。
# 解法：本机 DNS 写成 {"type":"local","prefer_go":true}——直接拿 resolv.conf 里的 nameserver 自己查，
# 跟 xray 一样，不碰 DBus（源码：local.go 只有 !preferGo 时才走 resolved）。
# prefer_go 是 1.13 才有的字段，1.12 见了会 "unknown field" 整份配置起不来，所以按内核版本加。
SB_PREFER_GO_MIN = (1, 13, 0)

def sb_fix_dns(cfg):
    """给 sing-box 服务端配置补上 prefer_go 的本机 DNS（内核 ≥1.13 才补，1.12 原样不动）。
       没有 dns 段就加一个；已有 local 类型的服务器只补 prefer_go。改了返回 True。"""
    v = _core_ver(SB_BIN) if os.path.exists(SB_BIN) else None
    if v is None or v < SB_PREFER_GO_MIN or not isinstance(cfg, dict):
        return False
    dns = cfg.get("dns")
    if not isinstance(dns, dict) or not dns.get("servers"):
        cfg["dns"] = dict(dns if isinstance(dns, dict) else {},
                          servers=[{"type": "local", "tag": "local", "prefer_go": True}])
        return True
    changed = False
    for srv in dns["servers"]:
        if isinstance(srv, dict) and srv.get("type") == "local" and not srv.get("prefer_go"):
            srv["prefer_go"] = True
            changed = True
    return changed

def _sb_heal_dns(restart=True):
    """现有的 sing-box 配置（主节点 + sing-box 内核的 CDN 节点）缺 prefer_go 就补上；
       校验不过原样回滚，绝不留坏配置；补了的才重启那一个服务。返回补了几份。"""
    targets = [(f"{SB_DIR}/config.json", "sing-box")]
    for n in _cdn_load():
        if n.get("core", "sing-box") != "xray" and n.get("conf") and n.get("svc"):
            targets.append((n["conf"], n["svc"]))
    fixed = 0
    for path, svc in targets:
        try:
            old = open(path).read()
            data = json.loads(old)
        except Exception:
            continue
        if not sb_fix_dns(data):
            continue
        json.dump(data, open(path, "w"), indent=2)
        ok, _ = core_check(SB_BIN, path)
        if not ok:
            open(path, "w").write(old)
            continue
        if restart:
            sh(f"systemctl restart {svc}", check=False)
        fixed += 1
    return fixed

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
    pq = _xray_heal_minclientver()                           # 老机器一次性对齐 mack-a：minClientVer 1.8.2 + 够格开抗量子签名
    if pq is not None:
        print(f"{ts} xray reality 已对齐 mack-a：最低客户端版本 1.8.2" + (f"，{pq} 个节点开了抗量子签名" if pq else ""))
    if _sb_heal_dns():                                       # 升到 sing-box 1.13+ 后补 prefer_go，防 DNS 走 resolved 全挂
        print(f"{ts} sing-box 已补本机 DNS prefer_go（不再依赖 systemd-resolved）")
    if _hy2_hop_heal():                                      # 端口跳跃 DNAT 丢了（持久化没生效）就补回
        print(f"{ts} hy2 端口跳跃规则已补回")
    setup_core_update_cron()                                 # 顺手确保每月自动更新的 cron 在
    setup_cfg_auto_cron()                                    # 同理：三大配置每日自动更新的 cron
    print(f"{time.strftime('%F %T')} {CORE_DONE_MARK}")      # 后台跑时用 python3 -u，逐行落盘不缓冲

def update_cores():
    ui_title("更新核心")
    for name, binpath in (("sing-box", SB_BIN), ("xray", XRAY_BIN)):
        if os.path.exists(binpath):
            v = sh(f"{binpath} version", check=False)
            ui_kv(name, v.splitlines()[0] if v else "版本读取失败")
        else:
            ui_kv(name, _hl("2", "未安装"))
    ui_kv("自动更新", _core_update_schedule_str())
    ui_line()
    ui_items([(1, "sing-box", None), (2, "xray", None), (3, "两个都更新", None), (0, "返回", None)])
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

# 【共用件的规矩：只拿自己的】nginx 这一条三家早就做对了（各删各的 conf，
# 谁都不碰 nginx 本体）。可 /etc/bgpeer 和 /etc/ssl/sb 也是共用的，
# 而这里原来是无条件 rm -rf —— 卸节点会顺手把 Emby 那套弄坏，且不留一句话。
MS_STATE_FILE = "/etc/bgpeer/media-stack.json"      # Emby 那套的状态文件
MS_NGINX_CONF = "/etc/nginx/conf.d/media-stack.conf"  # Emby 那套的站点


def _media_stack_installed():
    """这台机器上的 Emby 那套还在不在。

    判据用【它自己的判据】：状态文件里记着装在哪，那个目录里有 docker-compose.yml
    才算装着。只看状态文件在不在会把"卸干净了但状态文件还留着"也算成在，
    那样节点这边就永远删不掉 /etc/bgpeer 了。
    """
    try:
        with open(MS_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f).get("install_dir") or "/opt/media-stack"
    except Exception:
        return False
    return os.path.exists(os.path.join(d, "docker-compose.yml"))


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
    _hy2_hop_unpersist()
    sh("netfilter-persistent save", check=False)
    if os.path.exists(NGINX_CONF):                      # 移除本脚本的 nginx 前置块（不动用户其它站点）
        sh(f"rm -f {NGINX_CONF}", check=False)
    sh(f"rm -f {NGINX_FB_CONF}", check=False)           # trojan 回落用的本机伪装站
    _nginxconf_remove_stream()                          # 撤掉 sni-split 加进 nginx.conf 的 stream 块
    sh(f"rm -f {NGINX_STREAM_CONF}", check=False)
    if have("nginx"):
        sh("nginx -t && systemctl reload nginx", check=False)
    if os.path.exists("/opt/AdGuardHome/AdGuardHome"):   # 自建 DNS（AdGuard Home）一并撤掉——它的 DoT 靠 /etc/ssl/sb 证书，证书这里会删
        sh("/opt/AdGuardHome/AdGuardHome -s uninstall", check=False)
        sh("systemctl stop AdGuardHome", check=False)
        sh("rm -rf /opt/AdGuardHome", check=False)
    # 【这两样是共用的，得先看有没有别人在用】跟 nginx 一个规矩：只拿自己的。
    _ms = _media_stack_installed()
    _keep_cert = _ms and os.path.exists(MS_NGINX_CONF)
    paths = [SB_BIN, XRAY_BIN, SB_DIR, XRAY_DIR, SUB_DIR,
             "/root/xy-nodes.txt", "/usr/local/bin/bgpeer", WEBROOT,
             # cn-block 的每日刷新 cron、内核每月更新 cron 及日志：不清掉 cron 会调已删脚本报错
             "/etc/cron.d/bgpeer-cnblock", "/var/log/bgpeer-cnblock.log",
             CORE_CRON_FILE, CORE_CRON_LOG, CFG_AUTO_CRON, CFG_AUTO_LOG, SELFUPD_CRON, SELFUPD_LOG]
    if not _keep_cert:
        # 证书是节点签的，没人用就跟着走
        paths.append("/etc/ssl/sb")
    if not _ms:
        paths.append("/etc/bgpeer")          # 没人共用，整个目录一起收
    for p in paths:
        sh(f"rm -rf {p}", check=False)
    jobq_cleanup_if_unused()             # 凌晨任务排队器：没有别的定时任务在用了才撤（网络优化可能还在用）
    if _ms:
        # 【目录留着，但节点自己的文件要清干净】留下的只有别人的东西。
        for p in ("/etc/bgpeer/state.json", CFG_AUTO_FILE, NETPROFILE):
            sh(f"rm -f {p}", check=False)
        print("  ↷ /etc/bgpeer 保留：Emby 那套（media-stack）的状态也存在这里。")
        print("    里面记着它的安装目录、分片重定向端口、每个盘的探测 UA 开关 ——")
        print("    删掉的话它会当场显示「未安装」，而且那些开关会静默关掉。")
    if _keep_cert:
        print("  ↷ /etc/ssl/sb 保留：Emby 那套的 nginx 站点正用着这里的证书。")
        print("    删了它那个站点立刻打不开，而这不是「卸载节点」该有的结果。")
        print("    彻底清掉：先卸 Emby 那套，再 rm -rf /etc/ssl/sb")

def uninstall_all():
    """卸载子菜单：只卸代理主体 / 全部卸载(再带上网络优化) / 返回。
       AdGuard 自建DNS 本就随代理主体一起卸(它的 DoT 依赖 acme 证书，证书会被删)；
       唯一独立、需单独带上的是网络优化(BBR/QoS，写在 /etc/net-optimize)。"""
    nopt = os.path.exists(NETOPT_CONFIG)
    ui_title("卸载")
    ui_kv("代理主体", "sing-box / xray、订阅服务、证书、AdGuard 自建 DNS、CDN 节点、bgpeer 命令、定时任务")
    ui_kv("网络优化", "已启用（独立模块）" if nopt else _hl("2", "未启用"))
    ui_line()
    ui_items([(1, "卸载代理主体（网络优化保留）", None),
              (2, "全部卸载（代理主体 + 网络优化，恢复系统默认）", None),
              (0, "返回", None)])
    c = _ask("选择: ").strip()
    if c in ("1", "2"):
        ui_warn("卸载后节点、订阅、证书全部删除，客户端立即连不上，不可恢复")
    if c == "1":
        if _ask("\n  确认卸载代理主体（AdGuard 一并卸；网络优化保留）? [y/N]: ").lower() in ("y", "yes"):
            _uninstall_core()
            if nopt:
                _netopt_apply_profile()              # 画像没了：保留端口退回老规矩（整段池子 + 本机服务）
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
            # 只升不降：raw 被限流(429)会回落 jsDelivr（@main 缓存约 12h、?_t= 破不掉），
            # 可能拉到旧副本。拉到的带版本号且比本地那份旧，就别覆盖——否则每次进菜单都把
            # 本地新版降级成镜像上的旧版（真机：curl 到 4.5.11，一进菜单又被盖回 4.5.9）。
            newv = _remote_ver(body)
            if newv:
                try:    curv = _remote_ver(open(local, encoding="utf-8").read())
                except OSError: curv = ""
                if curv and _vt(newv) < _vt(curv):
                    return os.path.exists(local) and os.path.getsize(local) > 0
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
    _run_child(f"python3 {CN_BLOCK_LOCAL}")

def _run_child(cmd, **kw):
    """跑一个有自己菜单的子脚本，等它自己退出。

    【Ctrl-C 只该打断子脚本】终端按 Ctrl-C 时整个前台进程组都收到 SIGINT —— 子脚本
    （比如 media-stack「生成媒体库」屏上写着「Ctrl-C 随时走人」）接住了，本脚本却在
    subprocess.run 里跟着抛 KeyboardInterrupt 整个退出：满屏报错，子脚本还留在终端里
    跟 shell 抢输入（真机 9/28：「请选择: 请选择:」）。这里接住，接着等它。
    不用 SIG_IGN：忽略会被子进程继承，那边的 Ctrl-C 就失灵了。
    【不经过 sh】shell=True 时中间隔着一层 sh，Ctrl-C 先把 sh 打死，这里 wait() 立刻返回，
    本脚本以为子脚本退了、接着出自己的菜单 —— 而子脚本还活着，两层菜单抢一个终端。"""
    import shlex
    p = subprocess.Popen(shlex.split(cmd), **kw)
    while True:
        try:
            return p.wait()
        except KeyboardInterrupt:
            continue


def adguard_menu():
    """打开独立的 adguard-dns.py 交互菜单（自建 DNS · AdGuard Home）。"""
    if not ensure_remote_script(ADGUARD_URL, ADGUARD_LOCAL):
        print("拉取 adguard-dns.py 失败，且本地无缓存。请检查网络。"); return
    _run_child(f"python3 {ADGUARD_LOCAL}")

def media_stack_menu():
    """打开独立的 media-stack.py（自建 Emby·网盘直链媒体服务器）。

    它是完全独立的一个文件：只【读】本脚本的 state.json（拿域名）和 nginx 的
    内部 https 端口，只【写】/etc/nginx/conf.d/media-stack.conf 和它自己的安装
    目录，绝不碰 nginx.conf / bgpeer.conf / bgpeer-stream.conf。写 nginx 前会先
    nginx -t，不过就自动还原 —— 不会因为装媒体服务把节点搞坏。
    """
    if not ensure_remote_script(MEDIA_URL, MEDIA_LOCAL):
        print("拉取 media-stack.py 失败，且本地无缓存。请检查网络。"); return
    _run_child(f"python3 {MEDIA_LOCAL}")

def _ghrelay_regen():
    """重新生成三格式订阅 + 重写托管服务（含中转/新 token）。"""
    G["host"] = _host(); ensure_deps()
    return build_subscription(read_saved_links())

def _relay_probe(prefix):
    """拿这条中转真去拉一个小文件，看通不通。返回 (是否通, 说明)。"""
    probe = "https://raw.githubusercontent.com/bgpeer/nodekit/main/sub-template.yaml"
    try:
        req = urllib.request.Request(prefix + probe, headers={"User-Agent": HTTP_UA})
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
        # 用途：这台机器拉不到 GitHub（内核二进制 / 模板 / 版本号）时，借另一台能通的机器转过来，
        # 不用把包交给第三方反代。地址去那台机器菜单 14 顶上整条复制。没配也不影响（退到公共反代）。
        ui_title("备用取件中转（自己人的，优先于公共反代）")
        print(_hl("1;33", f"▸ 已配置（{len(lst)} 条）"))
        for i, u in enumerate(lst, 1):
            if i > 1:
                print()
            print(_hl("1;36", f"[{i}]"))
            print(_hl("94", u))
        if not lst:
            print(_hl("2", "（还没有：去能通 GitHub 的机器，菜单 14 顶上复制它的中转地址前缀）"))
        ui_line()
        ui_items([(1, "添加一条", None), (2, "删除一条", None), (3, "逐条测试连通", None), (0, "返回", None)])
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
            for i, u in enumerate(lst, 1):
                ok, why = _relay_probe(u)
                print(_hl("1;36", f"[{i}] ") + (_hl("1;32", f"✓ {why}") if ok else _hl("1;31", f"✗ {why}")))
        else:
            print("  无效选择。")

def ghrelay_menu():
    """GitHub 中转：规则/图标走【本机中转】还是模板原链接（作者模板为 jsDelivr）。默认本机中转。
       支持开/关 + 刷新中转 token（防别人蹭，旧地址立即失效，配置随之刷新）。需域名+真证书。"""
    dom = _host()
    if not (re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or "") and _sub_https()):
        print("\n  需要域名 + acme 真证书才能自建中转（走 HTTPS）；当前无域名/自签，只能用模板原链接（jsDelivr）。"); return
    while True:
        on = not os.path.exists(GHRELAY_OFF)
        # 中转只转发 GitHub、与订阅同端口、带 token 防蹭。刷新 token：旧地址立即失效 + 刷新订阅，
        # 订阅端口不变、客户端自动更新即可；「换端口」连订阅端口一起换随机（自动避开节点端口）。
        ui_title("GitHub 中转（规则 / 图标走本机）")
        ui_kv("当前", _hl("1;32", "本机中转") if on else "模板原链接（jsDelivr）")
        if on:
            print()
            ui_url("中转地址前缀（给别的机器当备用取件中转）", f"https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/")
        ui_line()
        ui_items([(1, "本机中转写入配置", "开" if on else "关（用 jsDelivr）"),
                  (2, "刷新中转 token（旧地址失效）", None),
                  (3, "刷新 token + 换订阅端口", None),
                  (4, "备用取件中转", f"{len(own_relays())} 条"),
                  (0, "返回", None)])
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
                print("  当前没开本机中转，先『1』开启再刷 token。"); continue
            open(GHRELAY_TOKEN_FILE, "w").write(secrets.token_urlsafe(12))   # 换新 token，旧的立即失效
            harden_perms()                # 刚落地是 0644，当场收紧，别留窗口
            print("  正在换 token 并刷新订阅…")
            if _ghrelay_regen():
                print(f"  ✓ 已换新 token，旧中转地址立即失效。新前缀：https://{dom}:{sub_port()}/{_ghrelay_token()}/gh/")
                print("  客户端重新拉一次订阅即用新 token（订阅地址端口没变，自动更新即可）。")
            else:
                print("  刷新失败（没有可用节点？）。")
        elif c == "3":
            if not on:
                print("  当前没开本机中转，先『1』开启再操作。"); continue
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
            harden_perms()                # 刚落地是 0644，当场收紧，别留窗口
            print("  正在换端口 + token 并刷新订阅…")
            netprofile_sync()                                 # 新订阅端口交给网络优化保留
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
         sing-box —— 把兜底那台（dns.final）换成自建 DoH；国内 local、国外 remote、fakeip 都不动，
                     兜底只接没被规则分到的零头查询，AdGuard 挂了也只影响这部分（见 _sb_selfdns）。
       adguard 菜单调用（selfdns-toggle）。"""
    dom = _host()
    if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom or ""):
        print("  需要域名（DoH 走域名+证书）。当前节点不是域名，无法写入自建 DNS。"); return
    if not read_saved_links():
        print("  还没有节点，先『1.安装』。"); return
    on = os.path.exists(SELFDNS_FLAG)
    if on:
        print()
        ui_kv("自建 DNS", _hl("1;32", "已写入订阅"))
        ui_kv("ClientID", selfdns_clientid())
        ui_line()
        ui_items([(1, "从订阅移除", None), (2, "更换 ClientID（泄露了就换）", None), (0, "返回", None)])
        c = _ask("选择: ").strip()
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
        # mihomo / 小火箭：放列表最前当主用，没通自动回落原 DNS，只是慢一下。
        # sing-box：换的是兜底那台，国内 / 国外 / fakeip 不动，挂了只影响没被规则分到的零头（见 _sb_selfdns）。
        # ClientID 与 IP 无关，手机流量换网也不影响；填进「允许的客户端」就关掉了开放解析器。
        print(f"\n  ✔ {act}自建 DNS，三个订阅已刷新，客户端重拉一次生效")
        if act == "已写入":
            print()
            ui_url("写入的 DoH", _selfdns_doh())
            print()
            ui_url("AdGuard 后台 → DNS 设置 → 访问设置 → 允许的客户端，填这一行", selfdns_clientid())
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

def _privdns_regen():
    """改完私有解析重生成三大配置：跟「刷新三大配置」同一套 _regen_fmt（不动节点、不换 token、不重启服务）；
       汇总、当 DNS 机对映射、去 DNS 问一遍都在 aggregated_links 里。手改过的格式不动，屏上一行说明。"""
    todo = [e for e in FMT if os.path.exists(FMT[e]["file"])]
    G["host"] = _host()
    links = aggregated_links()
    if not todo:
        return
    ylines, nodes = parse_nodes(links)
    if not ylines:
        return
    for e in todo:
        if cfg_auto_paused(e):
            print(f"  ⚠ {FMT[e]['label']} 手改过，没刷新（要刷到「刷新三大配置」里点）")
            continue
        st, _which, err = _regen_fmt(e, ylines, nodes)
        if st == "failed":
            print(f"  ⚠ {FMT[e]['label']} 生成失败，保留原配置：{str(err).splitlines()[0] if str(err) else err}")

def _privdns_served():
    """本机替哪些外机解析 → 「2 台（🇺🇸1、🇯🇵2）」；一台没有返回「还没有」。"""
    m = _privdns_map()
    if not m:
        return "还没有"
    return f"{len(m)} 台（" + "、".join(v.get("label") or "成员机" for _, v in sorted(m.items())) + "）"

def _privdns_own_switched(name):
    """本机订阅已经用上节点专用域名了吗（看三份配置里有没有这个名字）。"""
    for f in (CFG_FILE, SBOX_FILE):
        try:
            if name in open(f).read():
                return True
        except OSError:
            continue
    return False

def _privdns_domain_ready():
    """本机是不是用域名装的、节点链接都写这个域名（CDN 节点不算）→ 域名；不是返回 ''。"""
    dom = node_domain().lower()
    if not dom:
        return ""
    srv = {((_link_addr(u) or ("",))[0] or "").lower() for u in read_saved_links() if "CDN·" not in _link_name(u)}
    return dom if srv == {dom} else ""

def _privdns_public_free(root):
    """根域名上有没有 *.根域名 泛解析：有的话节点专用域名起什么都被公网查得到，藏不住。
       拿一个用完就扔的随机名去问，不拿真要用的那个——真名字一个字都不往公网 DNS 送。"""
    try:
        socket.getaddrinfo(_privdns_new_name(root), None)
        return False
    except OSError:
        return True

def _privdns_domain_enable(c, mode, dns=""):
    """用域名装的机器开私有解析（选 1 / 2）：节点改用公网查不到的节点专用域名「随机.根域名」。
       前台问完（令牌、确认），证书 / nginx / 订阅转后台做——重签证书要重启核心，挂着本机代理连的 SSH 会断。"""
    dom = _privdns_domain_ready()
    root = _root_domain(dom)
    keep = c["mode"] != "off" and c["name"].endswith("." + root) and _privdns_is_domain_name(c["name"])
    name = c["name"] if keep else _privdns_new_name(root)
    if not keep and not _privdns_public_free(root):
        ui_warn(f"公网查得到 *.{root} 底下随便起的名字：Cloudflare 里多半有 *.{root} 泛解析，节点专用域名藏不住，没开")
        return
    covered = _name_covers(cert_names(), name)
    cf = ""
    if not covered and not _acme_has_cf():
        _cf_token_steps(dom)
        cf = _ask("  粘贴 Token: ").strip()
        if not cf:
            print("  没填 Token，已取消，没改动")
            return
    ui_warn("开了以后：节点改用公网查不到的专用域名，小火箭那份不放本机节点"
            + ("" if covered else f"；证书要重签（多签 *.{root}），节点"
               + ("和 nginx 后面的网站 / Emby" if "nginx" in _cert_consumers() else "") + "断几秒"))
    if _ask("  确认开启? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  已取消，没改动")
        return
    plan = {"op": "privdns-domain", "mode": mode, "dns": dns, "name": name, "cf_token": cf, "G": dict(G)}
    _node_op_dispatch(plan, lambda: privdns_domain_apply(plan))

def privdns_domain_apply(plan):
    """后台跑（node-op 的 privdns-domain）：证书盖住节点专用域名 → 存设置 → nginx 认这个名字 → .links、订阅换过去。
       证书签不出来就什么都不改，节点照用公网域名。返回开没开成。"""
    dom, name = node_domain(), plan["name"]
    root = name.split(".", 1)[1]
    if not _name_covers(cert_names(), name):
        # 现有证书上的名字（节点域名、*.域名、Emby 的）一个不丢，再多签一个 *.根域名
        want = list(dict.fromkeys((cert_names() or [dom]) + [f"*.{root}"]))
        print(f"  证书多签一个 *.{root}（Cloudflare DNS 验证，不用公网记录）")
        if not cert_install_apply(dom, f"*.{dom}" in want, plan.get("cf_token", ""), want):
            print("  ✗ 证书没签成，私有解析没开：节点照用公网域名")
            return False
    c = privdns_conf()
    c.update(mode=plan["mode"], name=name, dns=plan.get("dns") or "")
    privdns_save(c)
    saved = dict(G)
    try:
        _restore_state_to_G(json.load(open(STATE_FILE)))
        msg = _nginx_sync_from_cfg()                   # 443 那个 server_name 带上节点专用域名
        if msg:
            print("  " + msg)
    except Exception as e:
        print(f"  ⚠ nginx 没重建成（{e}），挂在 nginx 后面的节点先照用公网域名")
    finally:
        G.clear()
        G.update(saved)
    _write_links_file(read_saved_links())
    _privdns_regen()
    if name in _PRIVDNS_LIVE:
        print("  " + _hl("1;32", f"✔ 已开：本机节点改用 {name}，客户端刷新订阅后生效"))
    elif plan["mode"] == "remote":
        print("  " + _hl("1;33", "⚠ 已开，外机还没对上：外机的「聚合节点链接」里要有本机，在外机点「更新配置」，"
                                 "回来再进本菜单"))
    else:
        print("  " + _hl("1;33", "⚠ 已开，本机 AdGuard 还没答对这个名字（本菜单 7 自检）"))
    return True

def _privdns_set_self(c):
    """选 1：本机当 DNS 机。要装好 AdGuard、开了加密（DoH 靠域名证书）。
       用域名装的（DNS 机都是）：本机节点也换成节点专用域名，由本机 AdGuard 解析。"""
    if not _agh_doh_url():
        ui_tip("本机要先装好 AdGuard、开加密（本菜单 1 装，3 看怎么开）")
        return
    if _privdns_domain_ready():
        _privdns_domain_enable(c, "self")
        return
    was = c["mode"]
    c["mode"] = "self"
    privdns_save(c)
    if was == "remote":
        _write_links_file(read_saved_links())          # 不再请外机替本机解析：.links 末尾那行撤掉
    _privdns_regen()
    print("  " + _hl("1;32", "✔ 已开：把本机 DNS 地址粘到外机的「2 外机DNS解析」"))

def _privdns_set_remote(c):
    """选 2：本机节点交给外机解析。用 IP 装的：私有名 xxx.internal；用域名装的：节点专用域名（_privdns_domain_enable）。"""
    dom = _privdns_domain_ready()
    ips = [] if dom else _privdns_ips(read_saved_links())
    if not dom and not ips:
        ui_tip("本机还没装节点（或节点链接没写明 SNI / Host），没法开")
        return
    url = _ask("  粘贴外机的 DNS 地址: ").strip()
    if not url:
        return
    if not _PRIVDNS_DNS.match(url):
        print("  ✗ 不是外机的 DNS 地址（外机 13 → 11 选 1 后屏上那一行），已忽略")
        return
    if dom:
        _privdns_domain_enable(c, "remote", url)
        return
    ui_warn("开了以后：汇总本机的订阅里，小火箭那份不放本机节点，本机的直连规则去掉")
    if _ask("  确认开启? [y/N]: ").strip().lower() not in ("y", "yes"):
        print("  已取消，没改动")
        return
    if c["mode"] != "remote" or not c["name"].endswith("." + PRIVDNS_ZONE):
        # 每次从关 / 本机切过来都起个新名字：整机克隆来的机器私有名会重，关了再开就分开了。
        # 旧名字在外机那边还留 7 天（PRIVDNS_GRACE），没刷新订阅的客户端照样能解析
        c["name"] = _privdns_new_name()
    c.update(mode="remote", dns=url)
    privdns_save(c)
    _write_links_file(read_saved_links())              # 主机下次来拉 .links 就看到末尾那行
    why = _privdns_check(c["name"], url, ips)
    if not why:
        print("  " + _hl("1;32", "✔ 外机已对上：主机下次汇总（「更新配置」或每天 03:20）就换成私有名"))
    else:
        print("  " + _hl("1;33", f"⚠ 已开，外机还没对上：{why}"))

def _privdns_set_off(c):
    """选 3：关。当 DNS 机时关掉会让外机的节点改回真实地址——醒目一行 + 默认否（CLAUDE.md 第四节）。
       证书上的 *.根域名、nginx 认的节点专用域名都留着：还没刷新订阅的客户端照样连得上。"""
    if c["mode"] == "off":
        return
    if c["mode"] == "self" and _privdns_map():
        ui_warn("关掉后外机的节点改回真实地址（映射再留 7 天，客户端来得及刷新订阅）")
        if _ask("  确认关掉? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("  已取消，没改动")
            return
    was, local = c["mode"], _privdns_is_domain_name(c["name"])
    c["mode"] = "off"
    privdns_save(c)
    _write_links_file(read_saved_links())              # .links 末尾那行撤掉
    if was == "self" or local:
        _privdns_regen()                               # 本机节点换回公网域名 / 外机节点改回原来的地址
    print("  " + _hl("1;32", "✔ 已关" + ("：主机下次汇总改回原来的地址" if was == "remote" else "")))

def privdns_menu():
    """13 → 11「节点使用私有DNS解析」（adguard-dns.py 调 privdns）。仓库主人 10/28 定的菜单：
         1 本机DNS解析 —— 本机当 DNS 机：替指定了本机的外机解析；本机节点也换成节点专用域名
         2 外机DNS解析 —— 本机节点交给外机解析：粘贴外机的 DNS 地址
         3 关          —— 跟原来一样，节点写原来的地址，公网 DNS 都能解析
       来龙去脉见 aggregated_links 上面那段注释。"""
    while True:
        c = privdns_conf()
        ui_title(f"节点使用私有DNS解析　当前：{_PRIVDNS_MODES[c['mode']]}")
        tip = ""
        dom_name = c["mode"] != "off" and _privdns_is_domain_name(c["name"])
        if c["mode"] == "self":
            url = _agh_doh_url()
            ui_url("本机 DNS 地址（给外机粘贴）", url)
            print()
            ui_kv("替外机解析", _privdns_served())
            if not url:
                tip = "本机 AdGuard 没开加密或没装好，外机连不上（本菜单 7 自检）"
        elif c["mode"] == "remote":
            ui_url("外机 DNS 地址", c["dns"], code="36")
            print()
            why = _privdns_check(c["name"], c["dns"], _privdns_addrs(read_saved_links(), c["name"])[1])
            print("  " + (_hl("1;32", "✔ 外机已对上") if not why else _hl("1;33", f"⚠ 外机还没对上：{why}")))
            if why.startswith(("DNS 里还没有", "DNS 答的 IP")):
                tip = "外机的「聚合节点链接」里加上本机 links 链接，再点「更新配置」"
            elif why.startswith("DNS 拒绝"):
                tip = "外机换过 ClientID：到外机 13 → 11 重新复制地址，本机选 2 再粘一次"
            elif not why and dom_name and not _privdns_own_switched(c["name"]):
                _privdns_regen()                       # 外机刚对上：本机自己的订阅也换过去
                print("  " + _hl("1;32", "✔ 本机订阅已换成节点专用域名，客户端刷新订阅后生效"))
        if dom_name:
            ok = _name_covers(cert_names(), c["name"])
            ui_kv("节点专用域名", c["name"] + ("" if ok else _hl("1;31", "（证书没盖住，节点照用公网域名）")))
            if not ok:
                tip = "再选一次 " + ("1" if c["mode"] == "self" else "2") + " 就补签证书"
        ui_line()
        ui_items([(1, "本机DNS解析", None), (2, "外机DNS解析", None), (3, "关", None), (0, "返回", None)])
        if tip:
            ui_tip(tip)
        ch = _ask("选择: ").strip()
        if ch == "1":
            _privdns_set_self(c)
        elif ch == "2":
            _privdns_set_remote(c)
        elif ch == "3":
            _privdns_set_off(c)
        elif ch in ("0", ""):
            return

def privdns_off():
    """卸载 AdGuard 时调用（privdns-off）：本机在当 DNS 机就关掉、清映射，重生成配置（外机节点改回真实地址）。
       别的情况静默返回。"""
    c = privdns_conf()
    if c["mode"] != "self" and not os.path.exists(PRIVDNS_MAP_FILE):
        return
    if c["mode"] == "self":
        c["mode"] = "off"
        privdns_save(c)
    try:
        os.remove(PRIVDNS_MAP_FILE)                     # AdGuard 整个要删，规则跟着没了，映射不用再留
    except OSError:
        pass
    try:
        _privdns_regen()
    except Exception as e:
        print("  订阅刷新跳过（不影响卸载）:", e)
    print("  ✓ 已关掉节点私有解析（外机节点改回真实地址）")

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
    _run_child(f"python3 {NETOPT_LOCAL} {args}".strip(),
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
    ui_title("VPS 线路检测（三网回程 + IP 纯净度）")
    _vpschk_last()
    ui_line()
    # 外部 IP 检测：输入任意 IP，查画像 + 本机到它的链路 + 黑名单
    ui_items([(1, "本机 IP 检测（约 2 分钟）", None), (2, "外部 IP 检测", None), (0, "返回", None)])
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
    _run_child(f"python3 {VPSCHK_LOCAL}{arg}")

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
    thr = 0
    try:            # 4.5.17 起阈值也记在 config（守护关着时 adaptive-qos.conf 不会更新）
        for ln in open(NETOPT_CONFIG):
            if ln.startswith("ADAPTIVE_QOS_THRESHOLD="):
                thr = int(ln.split("=", 1)[1].strip() or 0)
    except (OSError, ValueError):
        thr = 0
    if thr <= 0:
        try:
            thr = int(json.load(open(NETOPT_ADAPTIVE)).get("threshold", 0))
        except Exception:
            thr = 0
    if thr > 0:
        mb = thr / 1048576.0
    return "adaptive", mb

def _netopt_daemon_on():
    """自适应切换（守护进程）开没开：看 config 里菜单开关记的 ADAPTIVE_DAEMON；
       老机器还没这一项（4.5.17 前守护默认开）就看服务在不在跑。没优化过 = 关。"""
    if not os.path.exists(NETOPT_CONFIG):
        return False
    try:
        for ln in open(NETOPT_CONFIG):
            if ln.startswith("ADAPTIVE_DAEMON="):
                return ln.split("=", 1)[1].strip() == "1"
    except OSError:
        return False
    return sh("systemctl is-active net-optimize-adaptive-qos 2>/dev/null", check=False) == "active"

def _fmt_mb(mb):
    if mb is None:
        return "?"
    return str(int(round(mb))) if abs(mb - round(mb)) < 0.05 else f"{mb:.1f}"

def _netopt_version():
    """网络优化脚本的版本号；读不到返回 ""。

    【优先报「装在机器上的那份」】/usr/local/sbin/net-optimize.py 才是开机恢复、
    自适应 QoS 守护、月度 nginx 升级实际执行的那份。/etc/bgpeer 下的只是菜单每次
    拉新的缓存副本 —— 报它的话，屏上写着新版、常驻的东西却还在跑旧代码。
    """
    for f in (NETOPT_SBIN, NETOPT_LOCAL):
        try:
            m = re.search(r'^VERSION = "([^"]+)"', open(f, encoding="utf-8").read(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return ""


def _bbr_kernel_cur(vf="/sys/module/tcp_bbr/version", ccf="/proc/sys/net/ipv4/tcp_congestion_control", boot="/boot"):
    """菜单 12 第 4 项的「当前」：BBRv3 / BBRv3 内核但在用别的 / 装好待重启 / 原厂内核（BBRv1）。
       原厂 v1 没有 version 文件。「装了 v3 却跑着 cubic」仓库主人 10/11 真碰上过，所以连在用的一起看。"""
    def rd(p):
        try:
            return open(p).read().strip()
        except OSError:
            return ""
    v, cc = rd(vf), rd(ccf)
    if v:
        return _hl("1;32", f"BBRv{v}") if cc == "bbr" else _hl("1;33", f"BBRv{v} 内核，在用 {cc or '?'}")
    if "xanmod" in os.uname().release:
        return _hl("1;32", "XanMod")
    try:
        pending = any(f.startswith("vmlinuz-") and "xanmod" in f for f in os.listdir(boot))
    except OSError:
        pending = False
    return _hl("1;33", "已装好，重启后生效") if pending else "原厂内核（BBRv1）"

def _netopt_xanmod():
    """菜单 12 → 4：查条件、确认、安装、问重启都在 net-optimize.py --xanmod 里（也能单独跑）。"""
    if not ensure_remote_script(NETOPT_URL, NETOPT_LOCAL):
        print("拉取 net-optimize.py 失败，且本地无缓存。请检查网络。"); return
    # 旧缓存不认 --xanmod：它用 parse_known_args，不认的参数直接忽略、转头跑整套优化 —— 这里先拦住
    if "--xanmod" not in open(NETOPT_LOCAL, encoding="utf-8", errors="replace").read():
        print("  ⚠ 网络优化脚本没拉到新版，过会儿再试"); return
    _run_child(f"python3 {NETOPT_LOCAL} --xanmod")

def _netopt_daemon_label(mode, on):
    """菜单 12 第 7 项的「当前」：写清开 / 关各是什么效果（仓库主人 10/26：开 = 阈值生效，关 = 纯 fq）。
       固定档（3 固定 cake / 纯暴力发包）不跑守护，开关只是记着，切回 1 / 2 档才生效。"""
    if mode == "fixed_cake":
        return f"{'开' if on else '关'}（固定 cake 下不生效）"
    if mode == "fixed_burst":
        return f"{'开' if on else '关'}（纯暴力发包下不生效）"
    return "开 = 阈值生效（平时 cake，大流量 fq）" if on else "关 = 纯 fq 模式"

def _netopt_adaptive(on):
    """菜单 12 → 7：自适应切换（守护进程）开 / 关。只改这一项，不重跑整套优化。"""
    if not ensure_remote_script(NETOPT_URL, NETOPT_LOCAL):
        print("拉取 net-optimize.py 失败，且本地无缓存。请检查网络。"); return
    # 旧缓存不认 --adaptive：它用 parse_known_args，不认的参数直接忽略、转头跑整套优化 —— 先拦住
    if "--adaptive" not in open(NETOPT_LOCAL, encoding="utf-8", errors="replace").read():
        print("  ⚠ 网络优化脚本没拉到新版，过会儿再试"); return
    _run_child(f"python3 {NETOPT_LOCAL} --adaptive {'on' if on else 'off'}")

# ── 节点画像：节点和网络优化对上号 ────────────────────────────────────────
# 以前网络优化对节点是「盲调」：保留端口写死 15000-45000（跟这边挑端口的区间碰巧一样才罩住），
# CDN 2053 / 8443、nginx 本地 8443-8445、自己指定的订阅端口、池子外的 hy2 跳跃段全漏了。
# 现在这边把节点占的端口写成画像，网络优化照单从临时端口池摘掉（出站连接占不走，节点重启也绑得上）；
# 节点一变（装 / 删协议、CDN 增删、换订阅端口）就叫它 --apply-profile 只刷这一项——一个内核参数，
# 不重启任何服务、不影响在播。画像在 /etc/bgpeer（root-only），只有端口，不出这台机器。

def netprofile():
    """节点画像 → {"v", "pool", "tcp", "udp", "ranges", "listen"}，只有端口（不记 UUID / 密码 / 域名）。
       pool = 挑新端口的整段（新协议从挑端口到绑上有几十秒，整段留着才不会被出站连接抢走）；
       tcp / udp = 两核心的入站（含挂在 nginx 后面、听 127.0.0.1 的）、CDN 节点、订阅端口、nginx 自己听的口；
       ranges = hy2 跳跃段（DNAT 的那段 UDP，别让出站连接的临时端口落进去）；
       listen = 本机别的服务正在听的 TCP 端口（OpenList / Emby / AdGuard / 改过的 SSH…）——记进画像，
       它们一变签名就变，netprofile_sync 才知道要叫网络优化刷新。"""
    tcp, udp, ranges, hy2 = set(), set(), [], False
    for core in _CORE_META:
        for ib in _load_core_cfg(core)[1]:
            p = _cfg_port(ib)
            if not p:
                continue
            t = ib.get("type") or ib.get("protocol") or ""
            if t in ("hysteria2", "hysteria", "tuic"):
                udp.add(p)
                hy2 = hy2 or t == "hysteria2"
                continue
            tcp.add(p)
            if t == "shadowsocks" or (ib.get("streamSettings") or {}).get("network") == "xhttp":
                udp.add(p)                                    # 同 _fw_wanted：ss 两种都听，xhttp 能走 h3
    for n in _cdn_load():
        if isinstance(n.get("cf_port"), int):
            tcp.add(n["cf_port"])
    try:
        tcp.add(int(open(SUBPORT_FILE).read().strip()))       # 直接读文件：sub_port() 没有时会现挑一个
    except (OSError, ValueError):
        pass
    for path in (NGINX_CONF, NGINX_FB_CONF, NGINX_STREAM_CONF):   # 443 / 80 之外还有本地 8443-8445
        try:
            tcp |= {int(m) for m in re.findall(r"(?m)^\s*listen\s+(?:\S*:)?(\d+)\b", open(path).read())}
        except OSError:
            pass
    if hy2:                                                   # 跟 _hy2_hop_heal 一个判法：state 里没关跳跃就是开着
        try:
            raw = (json.load(open(STATE_FILE)).get("hy2_ports") or "").strip()
        except Exception:
            raw = ""
        if raw.lower() not in ("off", "n", "no", "none"):
            ranges.append(raw if re.match(r"^\d+-\d+$", raw) else HY2_PORTS)
    tcp, udp = {p for p in tcp if 0 < p < 65536}, {p for p in udp if 0 < p < 65536}
    return {"v": 1, "pool": f"{PORT_LO}-{PORT_HI}", "ranges": ranges, "tcp": sorted(tcp), "udp": sorted(udp),
            "listen": sorted(p for p in _tcp_listen_ports() if 1024 <= p and p not in tcp | udp)}

def _tcp_listen_ports():
    """本机正在监听的 TCP 端口（/proc/net/tcp、tcp6 里状态 0A），跟 net-optimize 的 tcp_listen_ports 同一个算法。
       UDP 不收：sing-box 替用户发 QUIC / DNS 的客户端套接字在表里跟服务端口长得一样。"""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(f).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            col = line.split()
            if len(col) > 3 and col[3] == "0A":
                try:
                    ports.add(int(col[1].rsplit(":", 1)[1], 16))
                except (IndexError, ValueError):
                    pass
    return ports

def _netopt_apply_profile():
    """叫网络优化按画像刷新保留端口（--apply-profile）。用本机现成的那份、不出网：装在机器上的（每月自更新）
       和面板缓存的，挑认 --apply-profile、版本高的；都是老版就不叫——老版用 parse_known_args，
       不认的参数直接忽略、转头跑整套优化，绝不能喂给它。"""
    best, bestv = "", ()
    for path in (NETOPT_SBIN, NETOPT_LOCAL):
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "--apply-profile" in txt and (not best or _vt(_remote_ver(txt)) > bestv):
            best, bestv = path, _vt(_remote_ver(txt))
    if not best:
        return False
    try:
        return subprocess.run(["python3", best, "--apply-profile"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def netprofile_sync():
    """节点画像写盘（0600），再看网络优化跟上没有：它跑完记下按哪版画像调的（config 的 PROFILE_SIG =
       画像文件 sha256 前 12 位），对不上就叫它刷新——两边对上号，不用每次去比内核参数。
       没跑过网络优化只写画像；手动指定过保留端口（RESERVED_PORTS）的不动。
       装 / 删协议、CDN 增删、换订阅端口之后调；进面板也过一遍（老装机、错过的变动在这补上）。永不抛异常。"""
    import hashlib
    try:
        prof = netprofile()
        body = json.dumps(prof, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            old = open(NETPROFILE).read()
        except OSError:
            old = ""
        if body != old:
            os.makedirs(BGP_DIR, exist_ok=True)
            tmp = NETPROFILE + ".new"
            with open(tmp, "w") as f:
                os.chmod(tmp, 0o600)
                f.write(body)
            os.replace(tmp, NETPROFILE)
        if not os.path.exists(NETOPT_CONFIG):
            return False
        cfg = {}
        for ln in open(NETOPT_CONFIG):
            k, _, v = ln.strip().partition("=")
            cfg[k] = v
        if "RESERVED_PORTS" in cfg:
            return False
        if cfg.get("PROFILE_SIG") == hashlib.sha256(body.encode()).hexdigest()[:12]:
            return False
        return _netopt_apply_profile()
    except Exception:
        return False

def _netopt_confirm():
    """跑优化档位(1/2/3)前确认：会重应用内核参数、期间可能短暂重启服务，默认否，要明确 y 才跑。"""
    print("  " + _hl("1;33", "提示：会重新应用内核参数/队列/防火墙标记，期间可能短暂重启 nginx/sing-box，完全生效需重启一次。"))
    return _ask("  确认执行？y 确认 / n 返回: ").strip().lower() in ("y", "yes")

def net_optimize_menu():
    """网络优化（本仓库 net-optimize.py：BBR/QoS/缓冲区等内核调优，依赖工具自动安装）。"""
    MARK = "  " + _hl("1;32", "← 当前")
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
        d_on = _netopt_daemon_on()
        if mode == "adaptive" and not d_on:
            cur += " · 切换关"          # 4.5.17 起默认关：出口固定 fq，阈值只在开了切换后才用
        m1 = MARK if is_10 else ""
        m2 = MARK if (mode == "adaptive" and mb is not None and not is_10) \
            or mode == "fixed_burst" else ""
        m3 = MARK if mode == "fixed_cake" else ""
        _v = _netopt_version()
        # 档位说明：1 适合内存 <1G；2 默认 20MB/s 激活、阈值可调，输入 0 = 纯暴力发包无智能算法，适合 2G 左右；
        # 3 不切换，适合高性能机器。6 清除全部优化配置、恢复系统默认。
        # 7 自适应切换（守护进程）开关：仓库主人 10/25 同意改成可选、默认关（服务端队列基本不堆，
        #   每隔几秒切 cake/prio 收益小，还多一个常驻进程）。只开关这一项，不重跑整套优化。
        ui_title("网络优化（BBR / QoS 内核调优）")
        ui_kv("版本", _hl("1;32", f"v{_v}") if _v else _hl("2", "未安装"))
        ui_kv("当前档位", _hl("1;32", cur))
        ui_line()
        print(f"  1. 自适应 + 抢带宽（10MB/s 激活，内存 <1G）{m1}")
        print(f"  2. 自适应 + 抢带宽（阈值可调，内存 2G 左右）{m2}")
        print(f"  3. 固定 cake 纯智能算法（高性能机器）{m3}")
        # 换内核排在 4（仓库主人 10/15「移到第 4 位去」），状况 / 卸载顺延到 5 / 6
        ui_items([(4, "换 BBRv3 内核（XanMod）", _bbr_kernel_cur()), (5, "网络优化状况", None),
                  (6, "卸载网络优化（恢复系统默认）", None),
                  (7, "自适应切换（守护进程）", _netopt_daemon_label(mode, d_on)), (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            if _netopt_confirm():
                _run_net_optimize()
        elif c == "2":
            t = _ask("  激活阈值 MB/s（回车=20；输入 0 = 纯暴力发包，无智能算法）: ").strip() or "20"
            try:    mb = float(t)
            except ValueError: mb = -1
            if mb < 0:
                print("  无效数字，请输入 ≥0 的数字（如 20，或 0 = 纯暴力发包）。"); continue
            if not _netopt_confirm(): continue
            if mb == 0:
                _run_net_optimize(env_extra={"ADAPTIVE_QOS_MODE": "fixed_burst"})
            else:
                _run_net_optimize(env_extra={"ADAPTIVE_QOS_THRESHOLD": str(int(mb * 1024 * 1024))})
        elif c == "3":
            if _netopt_confirm():
                _run_net_optimize(env_extra={"ADAPTIVE_QOS_MODE": "fixed_cake"})
        elif c == "4":
            _netopt_xanmod()
        elif c == "5":
            _run_net_optimize("--check")
        elif c == "7":
            _netopt_adaptive(not d_on)
        elif c == "6":
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
    """主菜单 5 提速：ws 类多路复用 smux / hy2 满速（Brutal）。两项互不相干，各自开关。"""
    while True:
        st = smux_current_state()
        ui_title("提速：多路复用 / hy2 满速")
        ui_items([(1, "smux 开关（ws / httpupgrade）",
                   "没有 ws 类节点" if st is None else (_hl("1;32", "开") if st else "关")),
                  (2, "hy2 满速（Brutal）", hy2_bw_text()),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            if st is None:
                print("  ⚠ 本机没有 ws / httpupgrade 类 sing-box 节点，smux 不适用"); continue
            ui_tip("开启后网页 / 小请求更顺，大文件下载 / 丢包线路可能变慢")
            ans = _ask(f"  确认{'关闭' if st else '开启'} smux? y 确认 / n 返回: ").strip().lower()
            if ans in ("y", "yes"):
                smux_apply(not st)
        elif c == "2":
            hy2_bw_menu()
        elif c in ("0", ""):
            return

def hy2_bw_menu():
    """hy2 满速：填宽带套餐的下行 / 上行（Mbps），0 = 关回自动。存好后三大配置按当前模板刷新（地址不变）。"""
    cur = hy2_bw() or (100, 1000)
    ui_tip("填宽带套餐的上下行，hy2 就按这个速度满速发；填得比宽带大不会更快，多发的包还要耗 VPS 流量")
    raw = _ask(f"  下行 Mbps（回车 = {cur[1]}，0 = 关、回到自动）: ").strip()
    if raw == "0":
        if not hy2_bw():
            print("  本来就是自动，没变。"); return
        hy2_bw_set(); print("  ✔ hy2 满速已关，回到自动（BBR）")
    else:
        up_raw = _ask(f"  上行 Mbps（回车 = {cur[0]}）: ").strip()
        try:
            down, up = int(raw or cur[1]), int(up_raw or cur[0])
        except ValueError:
            print("  ⚠ 要填数字（单位 Mbps），没改。"); return
        if not (1 <= down <= 100000 and 1 <= up <= 100000):
            print("  ⚠ 数字不对（1 ~ 100000），没改。"); return
        hy2_bw_set(up, down); print(f"  ✔ hy2 满速：下行 {down} · 上行 {up} Mbps")
    cfg_refresh_all()                                    # 带宽写在客户端配置里：三大配置刷一遍（地址不变）

# ============================================================================ 更换伪装域名（reality 借用的 SNI）
_SNI_LINK_RE = re.compile(r'([?&]sni=)[^&#]*')

def _sb_reality_inbounds(data):
    return [ib for ib in data.get("inbounds", [])
            if isinstance(ib.get("tls"), dict) and isinstance(ib["tls"].get("reality"), dict)]

def _current_sni(core=None):
    """读当前 reality 借用域名(SNI)：core="sb" / "xray" 只看那个核心；不给 = sing-box 优先，回落 xray。
       没有 reality 节点返回 ''。"""
    data, _ = _load_sb_cfg() if core != "xray" else (None, None)
    if data:
        for ib in _sb_reality_inbounds(data):
            s = ib["tls"].get("server_name")
            if s:
                return s
    if core == "sb":
        return ""
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

def _xr_pq_sync(data, sni):
    """换借用目标站时：按新站够不够格，给 xray 的 reality 入站开 / 关 ML-DSA-65（见 XR_MIN_CLIENT_VER 那段注释）。
       返回要改链接的 {UUID: 验证公钥（开）或 ""（关）}；已开着、新站也够格的不动，链接里的 pqv 照旧。
       按 UUID 不按端口：挂在 nginx 443 后面的链接端口是 443，跟 sing-box 的撞（10/22）。"""
    ok = _reality_pq_ok(sni)
    out = {}
    for ib in data.get("inbounds", []):
        rs = (ib.get("streamSettings") or {}).get("realitySettings")
        if not isinstance(rs, dict) or not ib.get("port"):
            continue
        if ok and not rs.get("mldsa65Seed"):
            try:
                seed, v = _xr_pq_keys()
            except Exception:
                continue
            rs["mldsa65Seed"] = seed
            for u in _xr_link_uuids(data, ib):
                out[u] = v
        elif not ok and rs.get("mldsa65Seed"):
            rs.pop("mldsa65Seed")
            for u in _xr_link_uuids(data, ib):
                out[u] = ""
    return out

def _link_set_q(ln, key, val):
    """改分享链接 query 里的一个参数（val 空 = 去掉），# 后面的节点名原样保留。"""
    head, sep, frag = ln.partition("#")
    base, _, query = head.partition("?")
    parts = [x for x in query.split("&") if x and x.split("=", 1)[0] != key]
    if val:
        parts.append(f"{key}={val}")
    return base + ("?" + "&".join(parts) if parts else "") + sep + frag

def _link_uuid(ln):
    """分享链接 vless://UUID@... 的 UUID（其它写法返回 ''）。"""
    try:
        return urllib.parse.unquote(urllib.parse.urlsplit(ln.strip()).username or "")
    except ValueError:
        return ""

def _links_set_pqv(by_uuid, path=None):
    """按 UUID 给保存的 reality 分享链接加上 / 去掉 pqv=（ML-DSA-65 验证公钥；"" = 去掉）。
       『# 订阅链接:』尾部原样保留。返回改了几条。"""
    path = path or NODE_FILE                             # 调用时再取：测试 / 改路径时跟 read_saved_links 对得上
    try:
        lines = open(path).read().split("\n")
    except OSError:
        return 0
    out, tail, n = [], False, 0
    for ln in lines:
        if ln.strip().startswith("#"):
            tail = True
        if not tail and "://" in ln and "security=reality" in ln:
            uid = _link_uuid(ln)
            if uid and uid in by_uuid:
                new = _link_set_q(ln, "pqv", by_uuid[uid])
                n += new != ln
                ln = new
        out.append(ln)
    open(path, "w").write("\n".join(out))
    return n

def _links_set_sni(new, path=None, uuids=None):
    """改写保存的分享链接里 reality 节点的 sni=（只动带 security=reality 的，域名类节点不误伤）；
       uuids 给了就只动这些 UUID 的（只换一个核心的伪装域名时，另一个核心的不能跟着变）；
       reality-xhttp 的 host= 跟 sni 是同一个，一起换。『# 订阅链接:』尾部原样保留。"""
    path = path or NODE_FILE
    try:
        lines = open(path).read().split("\n")
    except OSError:
        return
    out, tail = [], False
    for ln in lines:
        if ln.strip().startswith("#"):
            tail = True
        if (not tail and "://" in ln and "security=reality" in ln
                and (uuids is None or _link_uuid(ln) in uuids)):
            m = _SNI_LINK_RE.search(ln)
            old = m.group(0).split("=", 1)[1] if m else ""
            ln = _SNI_LINK_RE.sub(lambda m: m.group(1) + new, ln)
            if old:
                ln = re.sub(r"([?&]host=)" + re.escape(old) + r"(?=[&#]|$)", lambda m: m.group(1) + new, ln)
        out.append(ln)
    open(path, "w").write("\n".join(out))

def _reality_uuids(core):
    """某个核心 reality 入站的 UUID 集合（换伪装域名时只改这些节点的链接）。"""
    out = set()
    for ib in _load_core_cfg(core)[1]:
        if core == "sb":
            if isinstance((ib.get("tls") or {}).get("reality"), dict):
                out |= {str(u.get("uuid", "")) for u in ib.get("users") or [] if u.get("uuid")}
        elif ((ib.get("streamSettings") or {}).get("security")) == "reality" or _xr_is_fb_backed(ib):
            out |= {str(c.get("id", "")) for c in (ib.get("settings") or {}).get("clients") or [] if c.get("id")}
    return out

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

def change_sni_apply(new, core=None):
    """把 reality 借用域名换成 new：改核心配置 + nginx 分流 + 链接 + 订阅，
       任一核心校验不过就整体回滚、不重启（单台 VPS 也不会被坏配置锁死）。
       core="sb" / "xray" 只换那个核心的（仓库主人 10/22：两个核心各一个伪装域名，菜单 4 分开换）；不给 = 两个都换（老用法）。"""
    cores = ("sb", "xray") if core is None else (core,)
    old = _current_sni(core)
    sbcfg, xrcfg = f"{SB_DIR}/config.json", f"{XRAY_DIR}/config.json"
    items, uuids = [], set()                              # (cfg, bin, svc, old_text)
    if "sb" in cores and os.path.exists(sbcfg):
        try: data = json.load(open(sbcfg))
        except Exception: data = None
        if data and _set_sni_singbox(data, new):
            uuids |= _reality_uuids("sb")
            old_text = open(sbcfg).read()
            json.dump(data, open(sbcfg, "w"), indent=2)
            items.append((sbcfg, SB_BIN, "sing-box", old_text))
    pq = {}                                               # xray reality 的抗量子签名跟着新目标站开 / 关
    if "xray" in cores and os.path.exists(xrcfg):
        try: xd = json.load(open(xrcfg))
        except Exception: xd = None
        if xd and _set_sni_xray(xd, new):
            uuids |= _reality_uuids("xray")
            pq = _xr_pq_sync(xd, new)
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
    # nginx sni-split：按改过的配置重建分流表（nginx -t 不过它自己退回），不过就连核心配置一起回滚
    if G.get("sni_split") and os.path.exists(NGINX_STREAM_CONF):
        r = _nginx_sync_from_cfg()
        if r.startswith("⚠"):
            for cfg, binp, svc, old_text in items:
                open(cfg, "w").write(old_text)
            print("  ✗ nginx 分流校验未通过，已整体回滚（节点照常）"); return False
    elif _nginx_split_set_sni(old, new):                  # 老装机（记录里没 sni_split）：照旧改 map 那一行
        chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
        if chk.returncode:
            _nginx_split_set_sni(new, old)
            for cfg, binp, svc, old_text in items:
                open(cfg, "w").write(old_text)
            print("  ✗ nginx 分流校验未通过，已整体回滚：\n   "
                  + (chk.stderr or chk.stdout).strip().replace("\n", "\n   ")); return False
        sh("systemctl reload nginx", check=False)
    _links_set_sni(new, uuids=uuids if core else None)    # 校验都过了才动链接/订阅
    if pq:
        _links_set_pqv(pq)
        on = sum(1 for v in pq.values() if v)
        print(f"  ✔ {on} 个 xray reality 节点开了抗量子签名" if on else
              f"  ⚠ 新目标站{_reality_pq_why(new)}，{len(pq)} 个 xray reality 节点关掉了抗量子签名")
    try:                                                  # 安装记录跟着换：「只添加新协议」/ 重装默认沿用它（10/20 漏了，加协议还问老域名）
        st = json.load(open(STATE_FILE))
        if core in (None, "sb"):
            st["sni"] = new
        if core == "xray":
            st["sni_xr"] = new
        json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    if core in (None, "sb"):
        G["sni"] = new
    if core == "xray":
        G["sni_xr"] = new
    G["host"] = _host()
    try:
        build_subscription(read_saved_links(), new_token=False)
    except Exception as e:
        print("  订阅刷新跳过（不影响节点）:", e)
    restart_services(*[svc for _, _, svc, _ in items])
    return True

def _choose_new_sni(cur, avoid=""):
    """挑新的伪装域名：随机（池里本机探得通的，避开当前和另一个核心的）/ 手动输入。
       手填跟另一个核心的重复 → 红字说重复、回到这一步重选（仓库主人 10/22）。取消返回 None。"""
    while True:
        ui_items([(1, "随机挑一个（内置大站池，避开当前）", None), (2, "手动输入", None), (0, "取消", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            print("  从内置大站池里挑一个本机探得通的…")
            skip = {cur, avoid} - {""}
            new = _pick_ok_sni(skip)                     # 探不通的跳过；都不通就随便挑一个，下面会提示
            pool = [x for x in REALITY_SNI_POOL if x not in skip]
            return new or (secrets.choice(pool) if pool else None)
        if c == "2":
            v = _ask("  输入域名（如 www.microsoft.com）: ").strip().lower().rstrip(".")
            if not _SNI_RE.match(v):
                print(_hl("1;31", "  ✗ 域名格式不对，重选")); continue
            if avoid and v == avoid:
                print(_hl("1;31", f"  ✗ {v} 跟另一个核心的伪装域名重复了，重选")); continue
            return v
        return None

def change_sni_menu():
    """菜单 4：两个核心的 reality 各一个伪装域名，各换各的（仓库主人 10/22）；只有一个核心有 reality 就只列一项。"""
    G_, Y_, R_, N_ = "\033[1;32m", "\033[1;33m", "\033[1;31m", "\033[0m"
    try:
        _restore_state_to_G(json.load(open(STATE_FILE)))  # nginx 分流要按记录里的布局（sni_split / 域名）重建
    except Exception:
        pass
    while True:
        cur = {"sb": _current_sni("sb"), "xray": _current_sni("xray")}
        cores = [c for c in ("sb", "xray") if cur[c]]
        ui_title("更换伪装域名（reality 借用的 SNI 目标站）")
        if not cores:
            print("  ⚠ 本机没有 reality 类节点，没有伪装域名可换")
            return
        label = {"sb": "sing-box", "xray": "xray"}
        oks = {}
        for c in cores:
            ok, detail = _reality_sni_ok(cur[c])          # 从本机实连一下：连通 + TLS1.3 + h2
            oks[c] = ok
            ui_kv(f"{label[c]} 伪装域名", _hl("1;32", cur[c]) + "　" +
                  (_hl("1;32", f"通 · {detail}") if ok else _hl("1;31", f"不通 · {detail}")))
        ui_line()
        ui_items([(i + 1, f"换 {label[c]} 的（随机挑 / 手动输入）", None) for i, c in enumerate(cores)]
                 + [(0, "返回", None)])
        if not all(oks.values()):
            ui_tip("不通的站 reality 伪装会打折，建议更换")
        c = _ask("选择: ").strip()
        if c in ("0", ""):
            return
        if not c.isdigit() or not 1 <= int(c) <= len(cores):
            continue
        core = cores[int(c) - 1]
        other = cur["xray" if core == "sb" else "sb"]
        new = _choose_new_sni(cur[core], other)
        if not new or new == cur[core]:
            if new == cur[core]:
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
        if _ask(f"  确认把 {label[core]} 的伪装域名从 {cur[core]} 换成 {new}? y 确认 / n 取消: ").strip().lower() \
                in ("y", "yes"):
            if change_sni_apply(new, core):
                print(f"\n  ✓ 已更换为 {new}，配置 + 订阅已刷新，核心正在后台重启（订阅 URL 不变）。")
                print("  客户端重新拉取订阅即可生效——无需重装、无需重新导入。")
                print("  若你挂着本机代理来管理，重启会让 SSH 瞬断，属正常，操作已在服务端完成。")

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
        # 服务端识别到 BT/PT 流量即拒绝；best-effort，vision 流可能漏一小部分。
        ui_title("BT/PT 下载屏蔽（防 VPS 被投诉封机）")
        ui_items([(1, "BT/PT 屏蔽开关", _hl("1;32", "开") if on else "关"), (0, "返回", None)])
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
    print(_hl("1;33", "▸ 计费方式"))
    ui_items([(1, "双向相加", None), (2, "单向取大", None),
              (3, "只计出站（上传）", None), (4, "只计入站（下载）", None)])
    ui_tip("只按上传计费的机房选 3（选 2 会取到下载那边，比账单大很多倍）")
    m = _ask(f"选择（回车 = {_cur}）: ").strip()
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
    enc = st.get("enc") or "none"                         # CDN量子加密：客户端那半把钥匙
    if proto == "vless-xhttp":
        return (f"vless://{cred}@{addr}:{port}?encryption={enc}&security=tls&sni={dom}"
                f"&host={dom}&type=xhttp&path={path}&fp=chrome#{tag}")
    if proto == "vmess-ws":
        return vmess_link({"v": "2", "ps": tag, "add": addr, "port": str(port), "id": cred,
                           "aid": "0", "net": "ws", "type": "none", "host": dom,
                           "path": path, "tls": "tls", "sni": dom, "fp": "chrome"})
    if proto == "trojan-ws":
        return (f"trojan://{cred}@{addr}:{port}?security=tls&sni={dom}"
                f"&type=ws&host={dom}&path={path}&fp=chrome#{tag}")
    return (f"vless://{cred}@{addr}:{port}?encryption={enc}&security=tls&sni={dom}"
            f"&type=ws&host={dom}&path={path}&fp=chrome#{tag}")

def _cdn_intro():
    """进 CDN 菜单顶部的简短说明（原理 + 执行前三步）。"""
    # 原理：域名套 Cloudflare，客户端连 CF 的 IP，本机真 IP 被墙也能用；嫌慢用「2 优选地址」换更快的 CF 边缘。
    print(_hl("1;33", "▸ 装之前在 Cloudflare 做好三件事"))
    print("  ① 域名解析到本机 IP，开" + _hl("1;33", "橙色云"))
    print("  ② VPS 放行 2053 / 2083 / 2087 / 2096 / 8443")
    print("  ③ SSL/TLS 模式选 " + _hl("1;33", "Full（完全）"))

def _state_prefix():
    """读安装时用的名称前缀（state.json），CDN 节点默认沿用它。"""
    try: return json.load(open(STATE_FILE)).get("prefix", "")
    except Exception: return ""

def _cdn_config(proto, core, cred, path, port, domain, crt, key, dec="none"):
    """按协议+核心生成 CDN 节点的 (config_dict, binpath)。cred=uuid(vless/vmess)或password(trojan)。
       dec=VLESS 的 decryption（CDN量子加密开着时是 mlkem768x25519plus...，只有 xray 认）。"""
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
                  "settings": {"clients": [{"id": cred}], "decryption": dec or "none"},
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
    sb = {"log": {"level": "info"}, "inbounds": [ib], "outbounds": [{"type": "direct"}]}
    sb_fix_dns(sb)                                       # 1.13+ 本机 DNS 不走 resolved（见 sb_fix_dns）
    return (sb, SB_BIN)

def _cdn_qe_on():
    """CDN量子加密开关当前是否开着。"""
    return bool(_pref_load().get("quantum"))

def _cdn_qe_xray_ok():
    """xray 已装且够新（VLESS Encryption 要 25.9.5+）。读不出版本按不够新算。"""
    if not os.path.exists(XRAY_BIN):
        return False
    v = _core_ver(XRAY_BIN)
    return v is not None and v >= CDN_QE_MIN_XRAY

def _cdn_qe_keys():
    """生成一对 (服务端 decryption, 客户端 encryption)。
       `xray x25519` 的私钥 / Password 就是 `xray vlessenc` 里 X25519 认证那对钥匙（同一个函数生成），
       借它拿钥匙，免得去解析 vlessenc 那几行说明文字。
       native=不做外观伪装（外面已经是 CF 的 TLS，伪装没意义，native 最快）；
       600s=服务端 0-RTT 票据有效期，0rtt=客户端优先复用票据——都是 vlessenc 的默认值。"""
    priv, pub = reality_keys(XRAY_BIN, "x25519")
    return (f"mlkem768x25519plus.native.600s.{priv}",
            f"mlkem768x25519plus.native.0rtt.{pub}")

def _cdn_is_vless(n):
    return str(n.get("proto", "vless-ws")).startswith("vless")

def _cdn_qe_switch(node, on):
    """给一条 VLESS CDN 节点开/关量子加密：换钥匙、必要时换内核、重写配置、重启它自己的服务。
       端口 / uuid / 路径 / 证书一律不动，客户端那边只是链接里 encryption 那一段变了。
       开：sing-box 内核的改 xray，原内核记进 core_orig；关：有 core_orig 就还原。
       任何一步失败都把配置和服务原样还原，返回 False；成功返回 True。node 原地改。"""
    old = dict(node)
    try:
        old_conf = open(node["conf"]).read()
    except OSError:
        old_conf = None
    if on:
        node["dec"], node["enc"] = _cdn_qe_keys()
        if node.get("core", "sing-box") != "xray":
            node["core_orig"] = node.get("core", "sing-box"); node["core"] = "xray"
    else:
        node.pop("dec", None); node.pop("enc", None)
        if node.get("core_orig"):
            node["core"] = node.pop("core_orig")
    cred = node.get("cred") or node.get("uuid", "")
    try:
        cfg_dict, binpath = _cdn_config(node.get("proto", "vless-ws"), node["core"], cred,
                                        node["path"], node["cf_port"], node["domain"],
                                        node["crt"], node["key"], node.get("dec") or "none")
        json.dump(cfg_dict, open(node["conf"], "w"), indent=2)
        if node["core"] != old.get("core", "sing-box"):
            # 换内核：旧 unit 指向另一个程序，write_service 为防误伤别人的服务不肯覆盖，先删掉
            sh(f"rm -f /etc/systemd/system/{node['svc']}.service", check=False)
        write_service(node["svc"], binpath, node["conf"])
        return True
    except Exception as e:
        node.clear(); node.update(old)
        if old_conf is not None:
            open(node["conf"], "w").write(old_conf)
        old_bin = XRAY_BIN if old.get("core", "sing-box") == "xray" else SB_BIN
        sh(f"rm -f /etc/systemd/system/{node['svc']}.service", check=False)
        try: write_service(node["svc"], old_bin, node["conf"])
        except Exception: pass
        first = (str(e).strip().splitlines() or [""])[0]
        print(f"  ⚠ {node['domain']}:{node['cf_port']} 没改成，已还原：{first}")
        return False

def cdn_quantum():
    """CDN量子加密开关（只作用于 VLESS 类 CDN 节点）。"""
    Y, N = "\033[1;33m", "\033[0m"
    cfg = _pref_load()
    on = bool(cfg.get("quantum"))
    nodes = _cdn_load()
    vl = [n for n in nodes if _cdn_is_vless(n)]
    print(f"\n  CDN量子加密　当前：{'开' if on else '关'}")
    if on:
        print(f"  {Y}提示：关了后 Cloudflare 又能看到 VLESS CDN 节点的 UUID 和访问目标{N}")
    else:
        print(f"  {Y}提示：服务端仅支持 Xray（VLESS CDN 节点会改用 xray）；只有 mihomo 系客户端（1.19.13+）能连，"
              f"sing-box / 小火箭订阅里不再有它们（含优选）{N}")
    if (_ask(f"  {'关闭' if on else '开启'}CDN量子加密? y 确认 / 回车放弃: ") or "n").lower() not in ("y", "yes"):
        print("  已放弃。"); return
    if not on and vl and not _cdn_qe_xray_ok():
        if os.path.exists(XRAY_BIN):
            print("  ⚠ xray 内核太旧（要 25.9.5+），先回主菜单 19 更新内核。"); return
        print("  正在下载 xray 内核（量子加密只有 xray 支持）...")
        try: install_xray()
        except Exception as e:
            print("  ✗ xray 下载失败：", e); return
    was_in_sub = any(n.get("in_sub") for n in nodes)
    old_links = _cdn_state_links(nodes, cfg)             # 必须先算：改完就还原不出旧链接了
    done = bad = 0
    for n in vl:
        if bool(n.get("enc")) != on:                     # 已经是目标状态（比如上次开时没改成的）
            continue
        if _cdn_qe_switch(n, not on): done += 1
        else: bad += 1
    sh("systemctl daemon-reload", check=False)
    if nodes:
        _cdn_save(nodes)
    if not on and bad and done == 0:
        print("  ✗ 一条都没改成，开关保持关闭。"); return
    cfg["quantum"] = not on
    _pref_save(cfg)
    _cdn_resync(old_links, was_in_sub)
    if not vl:
        print(f"  ✓ 已{'关闭' if on else '开启'}（现在没有 VLESS CDN 节点，以后新装的按这个来）。")
    else:
        print(f"  ✓ 已{'关闭' if on else '开启'}，{done} 条 VLESS CDN 节点已改好"
              f"{'' if not was_in_sub else '、订阅已刷新'}，客户端要重拉订阅 / 重新导入链接。")
    if bad:
        print(f"  ⚠ {bad} 条没改成（仍{'加密' if on else '是明文'}），排查后到这里关了再开一次可重试。")

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

def _cdn_build_one(nodes, proto, core, domain, prefix, pref="", qe=False):
    """建一条 CDN 节点（独立证书/配置/服务/端口），成功返回 node、失败返回 None。
       pref=优选地址，只影响分享链接的地址位，服务端配置与它无关。
       qe=CDN量子加密：只对 VLESS 生效，强制 xray 内核，用户选的内核记进 core_orig（关掉时还原）。"""
    port = _cdn_pick_port({n["cf_port"] for n in nodes})
    if not port:
        return None
    nid = _cdn_next_id(nodes)
    crt, key, conf, svc = _cdn_node_paths(nid)
    _cdn_selfsigned(domain, crt, key)
    cred = new_pw() if proto == "trojan-ws" else new_uuid()
    path = "/" + secrets.token_hex(4)
    dec = enc = core_orig = ""
    if qe and proto.startswith("vless"):
        dec, enc = _cdn_qe_keys()
        if core != "xray":
            core_orig, core = core, "xray"
    cfg_dict, binpath = _cdn_config(proto, core, cred, path, port, domain, crt, key, dec or "none")
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
    if enc:
        node["dec"], node["enc"] = dec, enc
    if core_orig:
        node["core_orig"] = core_orig
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
    """CDN 节点安装：协议可多选、一次装多条；已装则问清空重装 / 追加 / 返回。
       原理：客户端连 Cloudflare 的 IP、不是 VPS 的 IP，VPS 真 IP 被墙也能用；前提是一个能挂到
       Cloudflare 的域名（免费版就行）。装完默认写进订阅——IP 被墙时它就是主力，不该再让人去点菜单 4。"""
    ui_title("CDN 节点安装")
    ui_tip("要一个挂在 Cloudflare 的域名；客户端连 CF，VPS 的 IP 被墙也能用")
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

    ui_line()
    ui_items([(1, "VLESS + WS（默认·最稳）", None), (2, "VLESS + XHTTP（最快）", None),
              (3, "VMess + WS", None), (4, "Trojan + WS", None)])
    protos = _parse_cdn_protos(_ask("选协议（回车 = 1；多选用逗号如 1,3,4；a = 全部）: "))
    if not protos:
        print("  没选到有效协议，已取消。"); return
    if len(protos) > free:
        print(f"  当前只剩 {free} 个可用端口，只装前 {free} 个：{protos[:free]}")
        protos = protos[:free]

    # CDN量子加密开着：VLESS 的一律 xray + 加密。xray 太旧就这次先不加密，别让整条装不上。
    qe = _cdn_qe_on() and any(p.startswith("vless") for p in protos)
    if qe and os.path.exists(XRAY_BIN) and not _cdn_qe_xray_ok():
        print("  ⚠ xray 内核太旧（要 25.9.5+），这次 VLESS 节点先不加密；更新内核后到菜单 6 关了再开即可补上。")
        qe = False
    forced_xray = lambda p: p == "vless-xhttp" or (qe and p.startswith("vless"))

    # 核心：只对不强制 xray 的协议问一次（xhttp / 量子加密的 VLESS 强制 xray）。
    # 仓库主人看不懂原来那句「非 XHTTP 的用哪个核心」——先列出哪些固定 xray、这一问管哪几个，再问。
    _pn = {"vless-ws": "VLESS+WS", "vless-xhttp": "VLESS+XHTTP", "vmess-ws": "VMess+WS", "trojan-ws": "Trojan+WS"}
    fixed = [_pn.get(p, p) for p in protos if forced_xray(p)]
    free_ps = [_pn.get(p, p) for p in protos if not forced_xray(p)]
    if fixed:
        # XHTTP 入站只有 xray 支持；CDN量子加密开着时 VLESS 也只能 xray（且只有 mihomo 系客户端能连）
        print(f"  {'、'.join(fixed)}：固定用 xray" + ("（量子加密）" if qe else ""))
    core_choice = "sing-box"
    if free_ps:
        ui_items([(1, "sing-box（默认）", None), (2, "xray", None)])
        core_choice = "xray" if _ask(f"{'、'.join(free_ps)} 用哪个核心（回车 = 1）: ").strip() == "2" else "sing-box"

    ipfx = _state_prefix()
    if ipfx:
        prefix = _ask_free(f"  节点名称前缀（回车=沿用安装前缀「{ipfx}」，或输入自定义）：").strip() or ipfx
    else:
        prefix = _ask_free("  节点名称前缀（回车=默认 CDN，自定义如 🇯🇵/家宽）：").strip()

    # 追加时新节点沿用已设的优选地址；首装留空——装完统一问要不要筛一批候选
    pref = next((n.get("pref") for n in nodes if n.get("pref")), "")

    # 需要的核心先各下载一次（避免循环里重复打印下载）
    for cr in {("xray" if forced_xray(p) else core_choice) for p in protos}:
        binp = XRAY_BIN if cr == "xray" else SB_BIN
        if not os.path.exists(binp):
            print(f"  正在下载 {cr} 内核（CDN 备用节点用）...")
            try: (install_xray if cr == "xray" else install_singbox)()
            except Exception as e:
                print(f"  ✗ {cr} 下载失败：", e); return

    created = []
    for proto in protos:
        core = "xray" if proto == "vless-xhttp" else core_choice
        node = _cdn_build_one(nodes, proto, core, domain, prefix, pref, qe=qe)
        if node:
            created.append(node)
    if not created:
        print("  ✗ 没有成功新增的节点。"); return
    _cdn_save(nodes)
    fw_sync()                                            # CDN 端口（2053…）本机防火墙也得放行
    netprofile_sync()                                    # 网络优化那边也把它摘出临时端口池
    ports = "、".join(str(n["cf_port"]) for n in created)
    # 每条各自独立服务、与主节点互不影响；有优选地址时链接地址位已换成它，SNI/Host 仍是域名
    ui_title(f"已新增 {len(created)} 条 CDN 节点（共 {len(nodes)} 条）")
    print_node_links([_cdn_link(n) for n in created])
    ui_line()
    ui_tip(f"CF 里 {created[0]['domain']} 解析到本机、开橙云、SSL 选 Full；VPS 放行 {ports}")
    if pref:
        ui_kv("优选地址", f"沿用 {pref}")

    # 装完默认写进订阅（仓库主人：「改成装完直接问，默认写入」）。IP 被墙时 CDN 就是主力，
    # 以前装完只留备用链接、要再进菜单 4 写入，容易漏。写入时连同优选候选一起进（见 _cdn_sub_links）。
    if (_ask("写进三大订阅？（回车 = 写入 / n = 只留备用链接）: ").strip().lower() or "y") not in ("n", "no"):
        cfg = _pref_load()
        was = any(n.get("in_sub") for n in nodes)
        old = _cdn_state_links(nodes, cfg)              # 先算快照：改完就还原不出旧链接了
        for n in nodes:
            n["in_sub"] = True                          # 和菜单 4 一致：全部一起写，不留半进半出
        _cdn_save(nodes)
        _cdn_resync(old, was)
        print("  ✔ 已写进三大订阅（订阅地址不变）")
        sub_offer_cert_domain()           # 订阅还是 IP、域名证书管理里装了证书 → 问要不要换成证书域名
    else:
        print("  已只留备用链接，想写随时进菜单 4")

    # 装完顺手筛一批优选候选：CDN 走 CF 任播，默认解析到哪个边缘全看运气，实测常比优选后慢好几倍。
    # 筛出来的多条候选一起写进订阅，由客户端 URLTest 自己挑最快的（只有客户端测得到你那边到 CF 的延迟）；
    # 以后想换就进菜单 2「优选地址」。
    cfg = _pref_load()
    ui_line()
    ui_tip(f"筛一批更快的 CF 边缘，客户端自动挑最快的（约 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量、一两分钟）")
    if (_ask("现在筛？（回车 = 筛 / n = 跳过）: ").strip().lower() or "y") in ("n", "no"):
        print("  已跳过，想筛随时进菜单 2「优选地址」")
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

def _cdn_link_key(u):
    """CDN 链接的「身份」：协议 + 凭据 + path + sni + 端口（不含地址位和名字）。认不出返回 None。

       认「这是哪条 CDN 节点的链接」不能按整串比：链接格式会随版本变（10/10 给 vmess 加了
       "fp":"chrome"，量子加密开关改 encryption=），按整串撤旧的撤不掉、新的又追加进去，
       订阅里就多出一条同名的（客户端显示 ¹🇯🇵·CDN·vmess-ws / ²🇯🇵·CDN·vmess-ws）。
       地址位不算进身份：优选换的就是它，候选节点跟基础节点也只差它。"""
    try:
        if u.startswith("vmess://"):
            b = u[8:]
            j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
            return ("vmess", str(j.get("id", "")), str(j.get("path", "")),
                    str(j.get("sni") or j.get("host") or "").lower(), str(j.get("port", "")))
        P = urllib.parse.urlparse(u)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(P.query).items()}
        return (P.scheme, urllib.parse.unquote(P.username or ""), q.get("path", ""),
                (q.get("sni") or q.get("host") or "").lower(), str(P.port or ""))
    except Exception:
        return None

def _cdn_sub_apply(remove_links=(), add_links=()):
    """从节点文件撤掉 remove_links、加入 add_links（去重），再刷新订阅。
       撤的时候按身份认（见 _cdn_link_key）：格式改过的旧链接也撤得掉，同一条节点不会叠出两份。
       没有主节点也能生成——此时订阅仅含 CDN 节点。"""
    links, tail = _node_file_parts()
    drop = {l for l in list(remove_links) + list(add_links) if l}
    keys = {_cdn_link_key(l) for l in drop} - {None}
    keep = [l for l in links if l not in drop and _cdn_link_key(l) not in keys]
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

def _cdn_links_heal():
    """老装机自愈：节点文件里 CDN 节点的链接跟现在该有的对不上（旧格式没撤掉、同一条叠了两份），
       按身份认出来，只留当前该有的那一份，刷新订阅（token 不变）。修了返回 True。

       来由：10/10 起 vmess 链接带 "fp":"chrome"，CDN·vmess 新旧两串对不上——之后一次优选 / 改地址，
       旧的按整串撤不掉、新的又追加进去，客户端里就成了 ¹🇯🇵·CDN·vmess-ws / ²🇯🇵·CDN·vmess-ws 两条。"""
    try:
        want = _cdn_sub_links(_cdn_load(), _pref_load())
        if not want:
            return False                    # CDN 没写进订阅：不动（菜单 4 写入 / 移出时会按身份一起清）
        # 只认「该在订阅里」的那几条的身份：只把叠出来的、旧格式的收成当前这一份，不会把哪条节点整个撤掉
        keys = {_cdn_link_key(l) for l in want} - {None}
        have = [l for l in _node_file_parts()[0] if _cdn_link_key(l) in keys]
        if sorted(have) == sorted(want):
            return False
        _cdn_sub_apply(remove_links=have, add_links=want)
        return True
    except Exception:
        return False

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
        if not n.get("enc"):
            sub = CDN_PROTO_SUB[n['proto']]
        elif sr_sub_mode() and n["proto"] != "vless-xhttp":     # 小火箭走节点订阅：量子加密单条导入实测能连，照收
            sub = "mihomo / 小火箭（CDN量子加密，sing-box 不支持，写入时自动跳过）"
        else:
            sub = "仅 mihomo（CDN量子加密，sing-box/小火箭写入时自动跳过）"
        print(f"    · {n['domain']}:{n['cf_port']} [{n['proto']}] → {sub}")
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
    if writing:
        sub_offer_cert_domain()           # 订阅还是 IP、域名证书管理里装了证书 → 问要不要换成证书域名
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
        req = urllib.request.Request(CF_IPS_URL, headers={"User-Agent": HTTP_UA})
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

def cdn_speedtest(cfg=None, quiet=False, exclude=(), extra=None):
    """粗筛一轮：拉 CF 段 → 采样 → 延迟筛 → 下载测速。
       返回按吞吐降序的 [{"ip","ms","mbps"}]；一个可用的都没有则返回 []。
       exclude：这轮不要的 IP（现有候选等）；「不通」名单（cfg["bad"]，客户端那边连不上被换下来的）一律不选。
       extra 给个 dict：延迟排得上、但没轮到下载测速的 IP 放进 extra["ranked"]，留着当后备。"""
    nodes = _cdn_load()
    cfg = cfg or _pref_load()
    port = _pref_port(nodes, cfg)
    say = (lambda *a: None) if quiet else (lambda *a: print(*a))
    cidrs, official = _cf_cidrs()
    say(f"  · CF IP 段 {len(cidrs)} 段（{'官方最新' if official else '拉取失败·用内置兜底段'}）")
    skip = set(exclude) | set(cfg.get("bad") or [])
    ips = [ip for ip in _sample_ips(cidrs, int(cfg["n_cand"])) if ip not in skip]
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
    if extra is not None:
        extra["ranked"] = [ip for ip, _ms in ranked[len(top):len(top) + 10]]
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
        return None                                       # 订阅本来就没它、现在也不该有 → 不动
    try:
        _cdn_sub_apply(remove_links=old_links, add_links=add)
        return True
    except Exception as e:
        print("  ⚠ 订阅刷新失败（状态已存下，可回上级菜单 4 重写一次订阅）：", e)
        return False

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
    # 粗筛出最多 n_out 个 CF 边缘，各写一条节点进订阅；它们共用同一个服务端入站、不新建服务，
    # 最终由客户端 URLTest 挑最快的那条。
    ui_title(f"测速筛候选（最多 {n_out} 条 [{base.get('proto')}]）")
    if ask:
        ui_tip(f"下载测速最多耗约 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量")
    if ask and (_ask("  继续? y 确认 / 回车返回: ") or "n").lower() not in ("y", "yes"):
        return
    extra = {}
    res = cdn_speedtest(cfg, extra=extra)
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
    # 后备：测通了没选上的 + 延迟排得上没轮到下载的。「更换优选 IP」先拿它们补，不用再测一轮
    cfg["spare"] = [r["ip"] for r in res[n_out:]] + [ip for ip in extra.get("ranked", []) if ip not in picked]
    cfg["last"] = {"time": stamp, "ok": True, "n": len(picked),
                   "best": res[0]["ip"], "mbps": res[0]["mbps"]}
    _pref_save(cfg)
    ok = _cdn_resync(old, was)
    ui_title(f"选出 {len(picked)} 条优选节点")
    for i, r in enumerate(res[:n_out], 1):
        print(f"  {_hl('1;36', f'{_cdn_tag_prefix(base)}CDN·优选{i}')}  {r['ip']:<16}"
              f"本机测 {r['ms']}ms / {r['mbps']} Mbps")
    ui_line()
    # 本机测的是 VPS→CF 这一段，只作粗筛；哪条对你的网络最快，只有客户端的 URLTest 说了算
    if was and ok:
        print("  ✔ 三大配置已刷新")
        ui_tip("客户端刷新订阅；哪条在客户端里一直不通，用「5 更换优选 IP」换掉")
    elif not was:
        ui_warn("CDN 节点还没写进订阅，优选也不会进——到菜单 4 写入")

def _pick_nums(raw, top):
    """「3,5」「3，5」「3 5」→ [3, 5]（去重、保序）；有一个不在 1..top 就返回 None。"""
    out = []
    for tok in re.split(r"[,，\s]+", raw.strip()):
        if not tok:
            continue
        if not tok.isdigit() or not 1 <= int(tok) <= top:
            return None
        if int(tok) not in out:
            out.append(int(tok))
    return out or None

def cdn_cand_swap():
    """更换优选 IP：客户端里一直不通 / 很慢的候选，按编号换成别的 CF 边缘，换完直接刷新三大配置。
       仓库主人：「优选5 一直连不上……做一个按钮更换优选IP，而且优选完了之后直接刷新三大配置」。

       本机测的是 VPS→CF，你的网络到某个 CF 边缘通不通只有客户端看得见（URLTest 里的 Timeout），
       所以由你点名哪几条不行：先拿上次测速留下的后备补，不够再测一轮；换下来的记进「不通」名单
       （留最近 50 个），以后测速不再选它。编号跟客户端里「优选N」对得上，换完名字不变、只换地址。"""
    nodes, cfg = _cdn_load(), _pref_load()
    if not nodes:
        print("  还没配置 CDN 节点，先回上级菜单选 1 装一条。"); return
    cands = [c for c in (cfg.get("cands") or []) if c]
    if not cands:
        print("  还没有候选节点，先选 1 测速筛候选。"); return
    pfx = _cdn_tag_prefix(_cdn_cand_base(nodes))
    ui_title("更换优选 IP")
    ui_items([(i, f"{pfx}CDN·优选{i}", ip) for i, ip in enumerate(cands, 1)])
    ui_line()
    raw = _ask("换哪几条（客户端里不通的，如 5 或 3,5；回车返回）: ").strip()
    if not raw:
        return
    idxs = _pick_nums(raw, len(cands))
    if not idxs:
        print(f"  ⚠ 编号要在 1~{len(cands)} 之间，没换。"); return
    bad = [ip for ip in (cfg.get("bad") or []) if ip]
    spare = [ip for ip in (cfg.get("spare") or []) if ip not in cands and ip not in bad]
    if len(spare) < len(idxs):
        ui_tip(f"上次测速剩的后备不够，要再测一轮（最多约 {float(cfg['n_top']) * float(cfg['dl_mb']):.0f} MB 流量）")
        if (_ask("  继续? y 确认 / 回车返回: ") or "n").lower() not in ("y", "yes"):
            return
        extra = {}
        res = cdn_speedtest(cfg, exclude=set(cands) | set(spare), extra=extra)
        spare += [ip for ip in [r["ip"] for r in res] + extra.get("ranked", []) if ip not in spare]
    if not spare:
        print("  ⚠ 没找到能换的 IP，候选保持原样。"); return
    was = any(n.get("in_sub") for n in nodes)
    old = _cdn_state_links(nodes, cfg)
    done = []
    for i in idxs:
        if not spare:
            break
        new = spare.pop(0)
        bad.append(cands[i - 1]); done.append((i, cands[i - 1], new))
        cands[i - 1] = new
    cfg["cands"], cfg["spare"], cfg["bad"] = cands, spare, bad[-50:]
    _pref_save(cfg)
    ok = _cdn_resync(old, was)
    for i, a, b in done:
        print(f"  ✔ {pfx}CDN·优选{i}：{a} → {b}")
    if len(done) < len(idxs):
        print(f"  ⚠ 后备不够，还有 {len(idxs) - len(done)} 条没换（选 1 重新测速）")
    if was and ok:
        print("  ✔ 三大配置已刷新")
        ui_tip("客户端刷新一次订阅就换上了")
    elif not was:
        ui_warn("CDN 节点还没写进订阅，优选也不会进——到菜单 4 写入")

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
        # 原理：分享链接里【地址位】换成更快的 CF 地址，SNI / Host 仍是你的真域名；CF 靠 Host 回源，
        # 换任意 CF 边缘都能连回同一台 VPS，服务端一行都不用改。候选节点交给客户端 URLTest 自己挑最快的。
        ui_title("优选地址（换客户端连的 CF 边缘）")
        if not nodes:
            print("  ⚠ 还没配置 CDN 节点，先回上级菜单选 1 装一条")
            return
        cur = sorted({(n.get("pref") or "").strip() for n in nodes})
        cands = [c for c in (cfg.get("cands") or []) if c]
        ui_kv("基础节点", _hl("2", "未优选（用域名）") if cur == [""]
              else _hl("1;32", "、".join(c or "(用域名)" for c in cur)))
        ui_kv("候选节点", f"{len(cands)} 条：" + "、".join(cands) if cands else _hl("2", "无"))
        last = cfg.get("last") or {}
        if last.get("ok"):
            ui_kv("上次测速", f"{last.get('time','')}  选出 {last.get('n','?')} 个，"
                              f"最快 {last.get('best','')} / {last.get('mbps','?')} Mbps")
        elif last:
            ui_kv("上次测速", f"{last.get('time','')}  没选出可用地址（已保持原样）")
        ui_line()
        ui_items([(1, f"测速筛候选（写 {cfg.get('n_cand_out', 5)} 条进订阅）", None),
                  (2, "手动填优选域名 / IP（留空 = 取消优选）", None),
                  (3, "清空候选节点", None),
                  (4, "测速参数", None),
                  (5, "更换优选 IP（换掉不通的候选）", None),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            cdn_pref_scan()
        elif c == "2":
            _pref_manual(nodes, cfg)
        elif c == "3":
            cdn_cand_clear()
        elif c == "4":
            _pref_settings(cfg)
        elif c == "5":
            cdn_cand_swap()
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
    netprofile_sync()                                   # 撤掉的 CDN 端口网络优化那边也放回去
    print(f"  ✓ 已卸载 {len(targets)} 条 CDN 节点（Cloudflare 那边的 DNS 记录请自行删除）。")

def cdn_menu():
    while True:
        nodes = _cdn_load()
        ui_title("CDN 套用（IP 被墙时靠 Cloudflare 中转）")
        _cdn_intro()
        ui_line()
        pcur = sorted({(n.get("pref") or "").strip() for n in nodes}) if nodes else [""]
        insub = any(n.get("in_sub") for n in nodes)
        ui_items([(1, "CDN 节点安装", f"{len(nodes)} 条" if nodes else None),
                  (2, "优选地址", "、".join(c for c in pcur if c) if pcur != [""] else None),
                  (3, "查看全部备用链接", None),
                  (4, "全部节点写入 / 移出订阅", ("已写入" if insub else "未写入") if nodes else None),
                  (5, "卸载 CDN 节点（某条 / 全部）", None),
                  (6, "CDN 量子加密", "开" if _cdn_qe_on() else "关"),
                  (0, "返回", None)])
        c = _ask("选择: ").strip()
        if c == "1":
            cdn_add()
        elif c == "2":
            cdn_pref_menu()
        elif c == "3":
            if not nodes:
                print("  还没配置，先选 1 CDN节点安装。"); continue
            # 每条：标题一行（协议 / 核心 / 地址 + 运行状态 + 是否进订阅），链接顶格一行，条间空一行
            ui_title(f"CDN 备用节点链接（{len(nodes)} 条）")
            for i, n in enumerate(nodes, 1):
                act = sh(f"systemctl is-active {n['svc']}", check=False) == "active"
                pf = (n.get("pref") or "").strip()
                if i > 1:
                    print()
                print(_hl("1;36", f"[{i}] {n.get('proto', 'vless-ws')}/{n.get('core', 'sing-box')}  "
                                  f"{n['domain']}:{n['cf_port']}")
                      + "  " + (_hl("1;32", "运行中") if act else _hl("1;31", "未运行"))
                      + "  " + ("已写入订阅" if n.get("in_sub") else _hl("2", "仅备用"))
                      + ("  量子加密" if n.get("enc") else "") + (f"  优选→{pf}" if pf else ""))
                print(_cdn_link(n))
        elif c == "4":
            cdn_write_sub()
        elif c == "5":
            cdn_remove()
        elif c == "6":
            cdn_quantum()
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
    except Exception:
        m = {}
    if isinstance(m, dict) and m.get("domain"):
        # 旧版自建 DNS 菜单 8 签了泛域名却没记进来：磁盘上这张就是泛域名的，补记上。
        # 不补的话哪天这张被换掉（重装时证书没了之类），重签就不知道这台本来该是泛域名，
        # 照 HTTP-01 签回单域名 —— 安卓「ID.域名」悄无声息地失效。
        try:
            if not m.get("wildcard") and f"*.{m['domain']}" in cert_names(ACME_CRT):
                m = save_cert_meta(wildcard=True, mode=_acme_mode(m["domain"]) or "dns-cf")
        except Exception:
            pass
        return m
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

def _acme_cron_heal():
    """自愈：用着 acme 证书、acme.sh 也在，可 crontab 里没有它的每日续期任务 → 补上。补了返回 True。

       它没了一点声响都没有，等发现时证书已经过期，吃证书的节点 / 订阅 / 自建 DNS 一起断。本脚本从不改
       root 的 crontab，来路通常在外面：别的一键脚本整份重写了 crontab、有人 crontab -r 过。
       只补这一条（acme.sh --install-cronjob 自己会先查有没有），别的定时任务一个不碰。
       连 cron 都没装的不在这里装包——面板会提示，选『3 强制重签』会连 cron 一起装上。"""
    acme = os.path.expanduser("~/.acme.sh/acme.sh")
    if not (os.path.exists(ACME_CRT) and os.path.exists(acme) and have("crontab")) or _acme_cron_ok():
        return False
    _ensure_cron_running()
    sh(f"{acme} --install-cronjob", check=False)
    return _acme_cron_ok()

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
        # 续期模式以 acme.sh 的记录为准（它才是真正拿来续期的那套）：cert.json 是本脚本自己记的，
        # 旧版自建 DNS 菜单 8 改签泛域名（DNS-01）后它还写着 standalone，面板就把 DNS-01 说成「HTTP-01」
        "mode": (_acme_mode(dom) if dom else "") or meta.get("mode") or "",
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

# ── 域名用在哪（菜单 15 面板下半截）────────────────────────────────────────────
# 仓库主人：「把所有域名对应用在什么地方的给他列出来」。第一版把每个节点的协议都列了，太细；
# 第二版缩成一行「域名　哪张证书」，又说「刚刚的意思是说不要帮每个节点都写上去，但是用在什么地方的
# 还是要写出来」。所以现在：一个域名一块，标题「▸ 域名　哪张证书 ✔/✗」，下面一行一个用途。
#   · 节点不逐个列：节点域名的标题已经写着「节点证书」；只有节点在用、别的都没有时才写一行「节点」
#   · reality / 自签节点借来的伪装站名不列（仓库主人：「伪装域名不用写在这里面」）
#   · 自建 DNS 开着、证书里却没有 *.域名：那块黄字「还没签（13 自建DNS → 8）」
# 只读本机文件：不联网、不调核心 —— 每进一次菜单 15 都要画一遍。

# media-stack 的子域 → 用途（跟 media-stack.py 的 SUBDOMAINS 对齐；那边新加的子域认不出，就写「媒体服务」）
_MS_SUB_USE = {"home": "Emby 首页入口", "emby": "Emby", "list": "OpenList（网盘）"}
AGH_YAML = "/opt/AdGuardHome/AdGuardHome.yaml"

def _link_addr(u):
    """分享链接 → (地址, sni, 是不是 reality, 是不是跳过证书校验)。认不出返回 None。"""
    try:
        if u.startswith("vmess://"):
            b = u[8:]
            j = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)))
            return (str(j.get("add") or ""), str(j.get("sni") or j.get("host") or ""), False,
                    str(j.get("allowInsecure", "")) in ("1", "true"))
        P = urllib.parse.urlparse(u)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(P.query).items()}
        insec = "1" in (q.get("insecure"), q.get("allowInsecure"), q.get("allow_insecure"))
        return P.hostname or "", q.get("sni") or q.get("peer") or "", q.get("security") == "reality", insec
    except Exception:
        return None

def _server_names(path):
    """nginx 配置里所有 server_name（一行写几个的也拆开；"_" 是兜底站，不算域名）。"""
    try:
        txt = open(path).read()
    except OSError:
        return []
    out = []
    for line in txt.splitlines():
        for names in re.findall(r"(?:^|[\s{;])server_name\s+([^;]+);", line.split("#", 1)[0]):
            out += [d for d in names.split() if d != "_"]
    return list(dict.fromkeys(out))

def _agh_tls():
    """AdGuardHome.yaml 顶层 tls 段 → {enabled, server_name, port_https, port_dns_over_tls}。没装返回 {}。"""
    try:
        txt = open(AGH_YAML).read()
    except OSError:
        return {}
    m = re.search(r"(?m)^tls:[ \t]*$", txt)
    if not m:
        return {}
    blk = []
    for line in txt[m.end():].splitlines():
        if line and not line[0].isspace():
            break
        blk.append(line)
    blk = "\n".join(blk)
    def val(k):
        v = re.search(rf"(?m)^\s*{k}:[ \t]*(\S.*?)[ \t]*$", blk)
        return v.group(1).strip("\"'") if v else ""
    return {k: val(k) for k in ("enabled", "server_name", "port_https", "port_dns_over_tls")}

_DU_NODE, _DU_WILD, _DU_EMBY, _DU_CDN = range(4)          # 排序：节点证书的在前，CDN 在最后

def domain_uses(i=None):
    """这台机器上的每个域名用在哪 → [(域名, 证书, 级别, [用途…])]，按 节点 → 泛域名 → Emby → CDN 排好。
       级别：'ok' 绿 ✔ / 'bad' 红 ✗（盖不住、过期、没有）/ 'hint' 黄（泛域名还没签）/ '' 不归本机管（CDN）。"""
    i = i or cert_info()
    names = [n.lower() for n in (i.get("names") or [])]
    uses, kind, by_node, agh_wild = {}, {}, set(), set()
    def add(d, use, k):
        d = (d or "").strip().lower().rstrip(".")
        if not d or _is_ip(d):
            return ""
        uses.setdefault(d, [])
        if use and use not in uses[d]:
            uses[d].append(use)
        kind[d] = min(kind.get(d, k), k)
        return d
    by_node.add(add(i.get("node_domain"), "", _DU_NODE))
    for n in names:                                   # 证书里写着的名字都列上，没人用的也看得见
        add(n, "", _DU_WILD if n.startswith("*.") else _DU_NODE)
    # CDN 备用节点：域名挂在 Cloudflare 后面，客户端看到的是 CF 的证书（源站自签）
    cdn = [d for d in dict.fromkeys((c.get("domain") or "").lower() for c in _cdn_load()) if d]
    for d in cdn:
        add(d, "CDN 备用节点", _DU_CDN)
    # 节点链接：只认地址 / sni 落在哪个域名上，不逐个列协议；reality / 自签借来的伪装站名不算
    for u in read_saved_links():
        r = _link_addr(u)
        if not r:
            continue
        host, sni, reality, insec = r
        host = host.lower(); sni = (sni or host).lower()
        if sni in cdn:                                # CDN 节点上面已经算了
            continue
        by_node.add(add(host, "", _DU_NODE))
        if not (reality or insec):
            by_node.add(add(sni, "", _DU_NODE))
    # 订阅地址（+ 本机 GitHub 中转：有域名、真证书、没手动关时规则 / 图标都走它）
    try:
        sub = open(HOST_FILE).read().strip()
    except OSError:
        sub = ""
    if sub and load_tokens():
        relay = not os.path.exists(GHRELAY_OFF) and i.get("exists")
        add(sub, "订阅地址 + GitHub 中转" if relay else "订阅地址", _DU_NODE)
    for d in _server_names(NGINX_CONF):
        add(d, "伪装站（nginx）", _DU_NODE)
    t = _agh_tls()
    if t.get("enabled") == "true" and t.get("server_name"):
        x = t["server_name"].lower()
        ports = [f"{k} {t[f]}" for k, f in (("DoT", "port_dns_over_tls"), ("DoH", "port_https"))
                 if t.get(f) not in ("", "0")]
        add(x, "自建 DNS" + (f"：{' / '.join(ports)}" if ports else ""), _DU_NODE)
        agh_wild.add(add("*." + x, "安卓专用 DNS（主机名带 ClientID）", _DU_WILD))
    # Emby 那套：真在跑的 nginx 站点里写了哪些子域就列哪些
    ed = (i.get("emby_domain") or "").lower()
    for d in _server_names(MS_NGINX_CONF):
        add(d, _MS_SUB_USE.get(d.split(".", 1)[0].lower(), "媒体服务"), _DU_EMBY)
        if i.get("emby_shared"):
            for w in names:
                if w.startswith("*.") and _name_covers([w], d):
                    add(w, "Emby 那几个子域", _DU_WILD)

    secs, es = i.get("secs"), i.get("emby_secs")
    def cert_of(d, k):
        if k == _DU_CDN:
            return "Cloudflare 的证书", ""
        if k == _DU_EMBY and ed and d.endswith("." + ed) and not i.get("emby_shared"):
            if es is None:
                return "没有 Emby 证书", "bad"
            return ("Emby 证书", "ok") if es > 0 else ("Emby 证书已过期", "bad")
        if not i.get("exists"):
            return "没有证书", "bad"
        if secs is not None and secs <= 0:
            return "节点证书已过期", "bad"
        if d.startswith("*."):
            if d in names:
                return "节点证书", "ok"
            return ("还没签（13 自建DNS → 8）", "hint") if d in agh_wild else ("节点证书里没有", "bad")
        return ("节点证书", "ok") if _name_covers(names, d) else ("节点证书盖不住", "bad")
    rows = [(d, *cert_of(d, kind[d]), us or (["节点"] if d in by_node else ["没找到在用的地方"]))
            for d, us in uses.items()]
    return sorted(rows, key=lambda r: kind[r[0]])

def domain_uses_print(i=None):
    """面板下半截：一个域名一块——黄色标题「▸ 域名　哪张证书 ✔/✗」，下面一行一个用途，块之间空一行。"""
    rows = domain_uses(i)
    if not rows:
        return
    print("-" * 60)
    print("  " + _hl("1;36", "域名用在哪"))
    for n, (d, cert, lv, us) in enumerate(rows):
        if n:
            print()
        col = {"ok": "1;32", "bad": "1;31", "hint": "1;33"}.get(lv, "2")
        mark = {"ok": " ✔", "bad": " ✗"}.get(lv, "")
        print(_hl("1;33", f"▸ {d}") + "　" + _hl(col, cert + mark))
        for u in us:
            print(f"  {u}")

def cert_panel(info=None):
    """把证书状况摆出来。只读，不改任何东西。"""
    C, Y, R, B, N = "\033[1;36m", "\033[1;33m", "\033[1;31m", "\033[1m", "\033[0m"
    i = info or cert_info()
    print("\n" + "=" * 60)
    print("  域名证书管理")
    print("=" * 60)
    if not i["exists"]:
        if i["selfsigned"]:
            print(f"  {Y}当前：自签证书{N}（没有域名）。自签能用，但客户端要开 allowInsecure，")
            print("        且自签本身就是明显特征。有域名的话建议装一张真证书。")
        else:
            print(f"  {Y}当前：还没有证书。{N}")
        domain_uses_print(i)
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
        print(f"    {R}✗ " + ("本机没装 cron —— 证书到期不会自动重签，选 3 会连 cron 一起装上" if not have("crontab") else
                               "crontab 里没有 acme.sh 的每日续期任务 —— 到期不会自动重签，选 3 会补上") + N)
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
    domain_uses_print(i)
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
    # HTTP-01：用 80 端口验证，只签这一个域名，不用任何密钥。
    # DNS-01：用 Cloudflare API 验证，签 *.域名，不占端口，Emby / 子域名都能共用同一张。
    print()
    print(_hl("1;33", "▸ 验证方式"))
    ui_items([(1, "HTTP-01（默认，80 端口，只签这一个域名）", None),
              (2, f"DNS-01 泛域名（Cloudflare API，签 *.{dom}）", None)])
    w = (_ask("选择（回车 = 1）: ") or "1").strip()
    cf_token = ""
    wildcard = (w == "2")
    if wildcard:
        if _acme_has_cf():
            print("  ✓ acme.sh 里已经存着 Cloudflare 凭据，直接用。")
        else:
            _cf_token_steps(dom)
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
    sub_offer_cert_domain()               # 证书装好了、订阅还是 IP → 问要不要换成证书域名

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
    # 升级要用 DNS-01（Cloudflare API）——泛域名只能这么签，HTTP-01 签不出来。
    # 升级后 Emby（emby 域的各个子域）就能跟节点共用这一张，两条续期链并成一条。
    ui_title("证书：升级成泛域名")
    ui_kv("当前证书", f"{dom}（{cert_issuer()}，{_cert_left_text(i['secs'])}）")
    ui_kv("覆盖", ", ".join(i["names"]) or "(读不出)")
    if not missing:
        print()
        print("  ✔ " + _hl("1;32", "已经是泛域名了，该盖的都盖住了"))
        ui_tip("想重新签：「3 强制重签」" + ("；想让 Emby 共用：「4 Emby 证书」"
                                          if emby and not i["emby_shared"] else ""))
        _ask("  按回车返回...")
        return
    ui_kv("升级后覆盖", ", ".join(want))
    ui_kv("现在还缺", _hl("1;33", ", ".join(missing)))
    ui_line()
    ui_items([(1, "升级成泛域名（DNS-01 · Cloudflare）", None), (0, "返回", None)])
    if (_ask("选择: ") or "0").strip() != "1":
        return
    cf_token = ""
    if _acme_has_cf():
        print("  ✓ acme.sh 里已经存着 Cloudflare 凭据，直接用。")
    else:
        _cf_token_steps(dom)
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
    root = _privdns_root()                       # 节点专用域名（13 → 11）：*.根域名 要一直带着，丢了它节点握手失败
    if root and _root_domain(dom) == root:
        names.append(f"*.{root}")
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
    if any(n.startswith("*.") for n in want):
        # 要签的里有泛域名（*.域名，或者节点专用域名要的 *.根域名）：HTTP-01 签不出来，只能 DNS-01。
        # wildcard 本身不改——它记的是「要不要 *.域名」，存进 cert.json 给下次重签判断用
        if not wildcard and not cf_token and not _acme_has_cf():
            print(f"{R}  ✗ 要签 {', '.join(n for n in want if n.startswith('*.'))}，得用 Cloudflare 令牌（DNS 验证），"
                  f"acme.sh 里没存过，现有证书没动。{N}")
            return False
        dns01 = True
    else:
        dns01 = False
    if dns01:
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
        fw_allow({(80, "tcp")})                      # HTTP-01 要从外面连 80：开着防火墙先放行
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
    # 只校验「盖不盖得住你刚才输的域名」是不够的——输错一个字符（example 打成 exmaple），
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
        # 共用：Emby 的证书路径是指向节点证书的软链，节点这张续期时本来就会 reload nginx，Emby 自动吃到新证书。
        # 各用各的：两条续期链 = 把「证书过期全挂」的风险配了两份，Emby 那条断了平时没人发现。
        # 合并不了：Emby 对外是 emby. / mw. 等好几个子域，要泛域名 *.域名 才盖得住。
        ui_title("Emby 证书")
        if shared:
            ui_kv("当前", _hl("1;32", "共用一张") + "（一条续期链）")
            ui_kv("证书", f"{ACME_CRT}  {_cert_left_text(i['secs'])}")
            ui_kv("覆盖", ", ".join(i["names"]) or "(读不出)")
        else:
            ui_kv("当前", _hl("1;33", "各用各的") + "（两张证书、两条续期链）")
            ui_kv("节点这张", f"{i['domain'] or '(读不出)'}  {_cert_left_text(i['secs'])}")
            ui_kv("Emby 这张", f"*.{dom}  {_cert_left_text(i['emby_secs'])}")
        ui_line()
        if shared:
            one = _hl("1;32", "当前就是")
        elif i.get("emby_covered"):
            one = _hl("1;36", "可以合并（少一条会悄悄断的续期链）")
        else:
            one = _hl("1;31", f"不行：节点这张盖不住 <子域>.{dom}")
        ui_items([(1, "共用节点这张证书", one),
                  (2, "各用各的", "Emby 用回自己那张" if shared else _hl("1;32", "当前就是")),
                  (0, "返回", None)])
        if not shared and not i.get("emby_covered"):
            ui_tip(f"先去「1 安装证书」用 DNS-01 重签泛域名 *.{dom}，再回来合并")
        elif shared:
            ui_tip("选 2 还原后要去「16 自建 Emby」重签它那张（合并时续期记录已撤）")
        elif not shared:
            ui_tip("各用各的：Emby 那条续期断了平时没人发现，能合并建议合并")
        c = (_ask("选择: ") or "0").strip()
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
# ⚠ 最容易写错的一处：reality 的 sni= 是【借用的第三方站】（www.cisco.com 之类），
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
    """整份 xy-nodes.txt 的域名改写。『# 订阅链接:』那段尾注里的订阅地址也跟着换 host
       （换域名不换 token，换完的地址就是现在真能用的那条；以前原样留着，文件里抄到的还是旧域名）。"""
    out = []
    for ln in text.split("\n"):
        if "://" in ln and not ln.strip().startswith("#"):
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

def _agh_set_domain(txt, old, new):
    """AdGuardHome.yaml 顶层 tls 段里 server_name 等于 old 的那一行换成 new（引号写法也认），别的一个字节不动。
       必须跟着换（10/17 真 AdGuard v0.107.79 实测）：它按 server_name 从 DoT 的 SNI 里拆 ClientID ——
       还是旧域名的话，手机填 <ID>.新域名 拆不出 ID，白名单一设就回 REFUSED，安卓专用 DNS 整机断网；
       adguard-dns.py 也会因为 server_name 对不上域名，把加密当成「未开启」。"""
    m = re.search(r"(?m)^tls:[ \t]*\n", txt)
    if not m:
        return txt
    end = m.end()
    for line in txt[m.end():].splitlines(True):
        if line.strip() and not line[0].isspace():
            break
        end += len(line)
    blk = re.sub(r"(?m)^(\s+server_name:[ \t]*)(['\"]?)" + re.escape(old) + r"\2([ \t]*)$",
                 lambda x: x.group(1) + new + x.group(3), txt[m.end():end])
    return txt[:m.end()] + blk + txt[end:]

def _domain_targets(old):
    """这次换域名会动到哪些文件 → [(路径, 说明)]。只列真的存在、真的含旧域名的。
       给确认框用：让你在按下去之前看到完整清单，而不是事后猜它动了什么。"""
    out = []
    for p, why in ((STATE_FILE, "安装记录（域名/前缀/协议表）"),
                   (HOST_FILE, "订阅 host（订阅 URL / GitHub 中转 / 自建 DNS 都读它）"),
                   (NODE_FILE, "分享链接"),
                   (f"{SB_DIR}/config.json", "sing-box 入站"),
                   (f"{XRAY_DIR}/config.json", "xray 入站"),
                   (NGINX_CONF, "nginx server_name"),
                   (AGH_YAML, "自建 DNS（AdGuard）加密服务器名")):
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
    if _agh_tls().get("enabled") == "true":
        try: cid = open(SELFDNS_CID_FILE).read().strip()      # 只读，没有就不生成
        except OSError: cid = ""
        out.append(("安卓专用 DNS", f"{cid}.{new}" if cid else new,
                    "手机里改成这个；苹果描述文件重装（面板 13 → 10）"))
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

    # ── 2b. 自建 DNS（AdGuard）的加密服务器名：不换的话 <ID>.新域名 拆不出 ClientID，白名单回 REFUSED ──
    if os.path.exists(AGH_YAML):
        txt = open(AGH_YAML, encoding="utf-8").read()
        nt = _agh_set_domain(txt, old, new)
        if nt != txt:
            snap(AGH_YAML)
            open(AGH_YAML, "w", encoding="utf-8").write(nt)
            print("  自建 DNS: AdGuard 加密服务器名已改（重启后生效）")

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
       时 Emby 多半该跟到 example.net（就像 jp.example.com 对应 example.com）。
       只是个默认值，问的时候可以改。"""
    parts = new.split(".")
    return ".".join(parts[1:]) if len(parts) >= 3 else new

def _domain_map(d, old, new, emby_old="", emby_new=""):
    """当前在用的一个域名 → 换完之后对应的新名字（*. 照样带）。跟这次换的无关 → ''。
       先认节点域名（更长、更具体）：jp.a.com 换 jp2.b.net 时，Emby 的 emby.a.com 才不会被它误吃。"""
    w = d.startswith("*.")
    b = d[2:] if w else d
    for o, n in ((old, new), (emby_old, emby_new)):
        if o and n and (b == o or b.endswith("." + o)):
            return ("*." if w else "") + b[:len(b) - len(o)] + n
    return ""

def _domain_record_plan(rows, old, new, emby_old="", emby_new=""):
    """换之前新域名要先建好的 A 记录 → [(要建的名字, 拿来查的主机名)]。
       只要真在用的（证书里写着、却没人用的泛域名不算）；新的节点域名一定在。
       泛域名查一个具体名字：有 ClientID 就查安卓真填的 <ID>.新域名，没有就查 xycheck.新域名。"""
    try: cid = open(SELFDNS_CID_FILE).read().strip()
    except OSError: cid = ""
    want = [new]
    for d, _c, _lv, us in rows:
        if us == ["没找到在用的地方"]:
            continue
        nd = _domain_map(d, old, new, emby_old, emby_new)
        if nd and nd not in want:
            want.append(nd)
    return [(n, f"{cid or 'xycheck'}.{n[2:]}" if n.startswith("*.") else n) for n in want]

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
    if _agh_tls().get("enabled") == "true":
        print(f"{Y}     ③ 装了自建 DNS：再加一条 *.新域名 的 A 记录（灰云），安卓专用 DNS 要用{N}")
    print(f"     解析改完【等生效】再来——DNS 有缓存，改完立刻查多半还是旧值。")
    print("-" * 60)
    new = _ask("  新域名（上面两条都做好了再填，回车取消）: ").strip().lower().rstrip(".")
    if not new:
        print("  已取消。"); return
    if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", new):
        print("  域名格式不对，已取消。"); return
    if new == old:
        print("  跟现在一样，没什么可换的。"); return

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

    # ── 当前关联的域名 + 换之前要建好的 DNS 记录（仓库主人 10/17：全部列出来，没建好就换不成）──
    rows = [r for r in domain_uses(i or None) if r[2] != ""]          # CDN 走自己的域名，不归换域名管
    plan = _domain_record_plan(rows, old, new, emby_old, emby_new)
    print()
    print(_hl("1;33", f"▸ 当前 {old} 关联的域名（{len(rows)} 个）"))
    for d, _c, _lv, us in rows:
        print(f"  {d}")
        print(f"      {' / '.join(us)}")
    print()
    print(_hl("1;33", f"▸ 换之前先在 DNS 建好这些 A 记录（灰云）→ {_ip or '本机公网 IP'}"))
    bad = 0
    for nd, probe in plan:
        ok, why = _domain_resolves_here(probe)
        bad += not ok
        print(f"  {(GRN + '✔') if ok else (R + '✗')} {nd}{N}　{why}")
    if bad:
        print(f"{R}  ⚠ 还有 {bad} 条没建好：现在换，这些地方会连不上，换了也会失败回退{N}")
        ui_tip("建好、等生效（几分钟到几十分钟）再来；走 Cloudflare 的要灰云（仅 DNS）")
        print("  已取消，一个字节都没改。")
        return

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
        _cf_token_steps(new)
        cf_token = _ask("  粘贴 Token: ").strip()
        if not cf_token:
            print("  没填 Token，已取消。"); return

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
    """菜单 15：域名证书管理。"""
    while True:
        i = cert_panel()
        _nd = node_domain()
        _can = [r for r in acme_records() if r["removable"]]
        # 2 更换域名：连证书、节点链接、订阅一起换；3 强制重签：到期前后、或怀疑证书坏了时用
        items = [(1, "安装证书", _hl("1;32", "已安装") if i["exists"] else "未安装"),
                 (2, "更换域名", _nd or _hl("2", "本机没有域名（自签 + IP）")),
                 (3, "强制重签（所有服务重读）", None)]
        if i["emby_domain"]:
            items.append((4, "Emby 证书", "共用一张" if i["emby_shared"] else "各用各的"))
        items += [(5, "清理旧证书", _hl("1;33", f"{len(_can)} 个没用 / 抢占的记录") if _can else "都是有主的"),
                  (0, "返回", None)]
        ui_items(items)
        c = (_ask("选择: ") or "0").strip()
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
    # 【泛域名不能修丢】这里原来一律 HTTP-01 重签，只签得出单域名：AdGuard 菜单 8 签的 *.域名 就这么被换掉，
    # 安卓私人 DNS 填的「ClientID.域名」当场证书对不上、整机断网（Emby 子域名同理）。泛域名只能走 DNS-01：
    # 有 Cloudflare 凭据就照原样签回来；没有就不修——宁可不动，也不静默降级。
    G["domain"] = dom
    wild, why = _wildcard_wanted()
    root = _privdns_root()                       # 节点专用域名（13 → 11）要的 *.根域名：修证书不能把它修丢
    if (wild or root) and not _acme_has_cf():
        R, N = "\033[1;31m", "\033[0m"
        print(f"{R}  ✗ 这张是泛域名证书（{why or '节点专用域名要 *.' + root}），可 acme.sh 里没存 Cloudflare 凭据，"
              f"修复只签得出单域名，没动。{N}")
        print("    要修：『15 域名证书管理 → 1 安装证书』选泛域名、填 Cloudflare 令牌重签")
        return
    # 续期方式必须跟【现在】的 80 端口状况对上。
    # 装机时没有 nginx，acme.sh 记下的就是 standalone（要独占 80）；可后来别的功能
    # （443 伪装站 / 自建 Emby / AdGuard）把 nginx 拉起来占了 80，standalone 从此永远
    # 验证失败——而且失败只写在 acme 自己的日志里，面板上一个字都看不到。
    # 这次翻车就是这么来的：cron 补上了也没用，续期照样跑不过。
    wild = wild or bool(root)                        # 有 *.根域名 也只能 DNS-01
    owner = "" if wild else _port80_owner()          # DNS-01 不走 80，谁占着都无所谓
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
    # 用 --issue --force 而不是 --renew：--renew 会沿用记录里那套（可能已经失效的）
    # 验证方式，--issue 则把这次用的方式写回记录，往后自动续期就跟着走对的路。
    if wild:
        print("  泛域名证书 → 走 Cloudflare DNS 验证重签（不用 80 端口），可能要一两分钟")
        names = [dom] + ([f"*.{dom}"] if _wildcard_wanted()[0] else []) + ([f"*.{root}"] if root else [])
        issue = (f"{acme} --issue --dns dns_cf " + " ".join(f"-d '{n}'" for n in names) +
                 f" --keylength ec-256 --server letsencrypt --force")
    else:
        fw_allow({(80, "tcp")})                      # standalone 验证要从外面连 80：开着防火墙先放行
        print("  正在续期…（走 acme.sh，可能要十几秒）")
        issue = f"{acme} --issue -d {dom} --standalone --keylength ec-256 --force{hooks}"
    r = subprocess.run(issue, shell=True, text=True, capture_output=True)
    if r.returncode:
        # 续期失败要把原文打出来——十次有九次是「80 端口被占」或「域名没解析到本机」，
        # 吞掉报错就只剩一句「修复失败」，等于没说
        print("  ✗ 续期失败，acme.sh 原文如下：")
        print("    " + ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", "\n    ")[-1200:])
        print("  常见原因：80 端口被占（standalone 验证要用）、域名没解析到本机、出网被墙。")
        return
    sh(f"{acme} --install-cert -d {dom} --ecc "
       f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{_ACME_RELOAD_HOOK}", check=False)
    for svc in ("nginx", "sing-box", "xray", "xy-sub"):
        sh(f"systemctl restart {svc}", check=False)
    sh("systemctl try-restart AdGuardHome", check=False)   # 自建 DNS 也换上新证书（没装就不动）
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
              "cert-install": "安装证书", "cert-domain": "更换域名", "privdns-domain": "节点专用域名"}
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
        elif plan["op"] == "privdns-domain":
            privdns_domain_apply(plan)
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
    print("            → 重启 nginx / sing-box / xray / xy-sub / AdGuard")
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
    # 一次性：xray reality 对齐 mack-a（最低客户端版本 1.8.2 + 目标站够格开抗量子签名）。改了才重启 xray，之后是 no-op。
    pq = _xray_heal_minclientver()
    if pq is not None:
        print("  ✔ xray reality 已对齐 mack-a：最低客户端版本 1.8.2"
              + (f"，{pq} 个节点开了抗量子签名（客户端刷新订阅生效）" if pq else ""))
    if _sb_heal_dns():
        print("  ✓ 已修好 sing-box 的本机 DNS（sing-box 已重启）")
    if _hy2_hop_heal():
        print("  ✓ 已补回 hy2 端口跳跃规则（以后 sing-box 每次启动都会自动检查）")
    if _vmess_insec_heal():
        print("  ✓ 已修好自签 vmess 节点（订阅地址不变，客户端刷新一次订阅即可）")
    if _relay_port_heal():
        print("  ✓ 已修好配置里的中转地址（订阅地址不变，客户端刷新一次订阅即可）")
    if _cdn_links_heal():
        print("  ✓ 已清掉重复的 CDN 节点（订阅地址不变，客户端刷新一次订阅即可）")
    if _acme_cron_heal():
        print("  ✓ 已补上证书自动续期任务（crontab 里原来没有 acme.sh，到期不会自动重签）")
    fw_sync()                                    # 老装机：开着防火墙、端口没放行的补上（放行了会说一行）
    if fake_site_heal():
        print("  ✓ 伪装站换成了本机生成的站点（每台机器都不一样）")
    ng = _nginx_rules_heal()                     # nginx 还是老规则：重建一次（平滑重载，连着的不断）
    if ng:
        print("  " + ng)
    if sr_onelink_heal():
        print("  ✔ 小火箭改回一个链接：节点写进配置了，小火箭里那条节点订阅可以删掉")
    if _sub_server_heal():
        print("  ✔ 订阅地址末尾换成机器名（不再显示 token），老地址照样能用")
    if any(os.path.exists(m["file"]) for m in FMT.values()):
        setup_cfg_auto_cron()                    # 老装机补上三大配置每日自动更新（默认开）
    setup_selfupd_cron()                         # 老装机补上脚本每日自动更新（默认开，菜单 18 关）
    if os.path.exists(CORE_CRON_FILE):
        setup_core_update_cron()                 # 老装机那条是按 6 月写死的，换成冬夏两季都对的写法
    _heal_other_crons()                          # cn-block 03:00 / 网络优化 nginx 月度：换成冬夏两季都对的写法
    netprofile_sync()                            # 节点画像：网络优化的保留端口跟节点对上（老装机、错过的变动在这补）
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
        print("  5. 提速：多路复用 smux / hy2 满速")
        print("  6. mihomo 配置")
        print("  7. sing-box 配置")
        print("  8. 小火箭配置")
        print("  9. CDN套用（利用CF中转，IP被墙时使用，延时比较高）")
        print("  10. 屏蔽中国域名和IP（可做白名单放行）")
        print("  11. BT/PT 下载屏蔽（防 VPS 被投诉封机）")
        print("  12. 网络优化（BBR/QoS 内核调优）")
        print("  13. 自建DNS（AdGuard Home·自己的解析服务器，兼带广告过滤）")
        print("  14. GitHub中转（规则/图标走本机·默认开，可关）")
        print("  15. 域名证书管理（状态 / 安装 / 换域名 / 重签）")
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
        elif c == "18":  script_update_menu()
        elif c == "19":  update_cores()
        elif c == "20":  uninstall_all()
        elif c in ("t", "T"): traffic_setup()   # 流量套餐设置（顶部流量行按机房周期显示）
        else:
            print("无效选择。"); continue
        _ask("\n按回车返回主菜单...")            # 停一下，别让菜单立刻盖住上面的输出

# ============================================================================ 交互菜单
# 混进回答里的按键码：手机终端误碰 Home / 方向键会把 ESC[H、ESC[A 这类序列塞进这一行（屏上显示成 ^[[H），
# 跟打的 y、编号连在一起就认不出来了（10/26 DMIT：网络优化跑的时候碰了 Home，最后打 y 没重启）。
_KEYCODES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.|.)|[\x00-\x1f\x7f]")

def _strip_keys(s):
    return _KEYCODES.sub("", s or "").strip()

def _ask(prompt=""):
    """交互输入：优先读 /dev/tty，使 curl|python3 管道下仍可交互。回答里混进的按键码去掉（见 _KEYCODES）。

       两头都读不到就返回空串（= 回车）。转后台跑的那些活儿（见 _node_op_dispatch）
       没有控制终端、stdin 也被掐成 /dev/null，真有哪一步漏了个问题没在前台问掉，
       也该当场走默认值继续，而不是抛 EOFError 把整件事炸在半路。"""
    try:
        with open("/dev/tty", "r") as t:
            # 【打的是菜单上有的编号才染绿】见 _ask_paint
            _seen = set(_MENU_SEEN)
            _MENU_SEEN.clear()
            print(prompt, end="", flush=True)
            line = t.readline()
            if line == "":
                raise EOFError
            v = _strip_keys(line)
            _ask_paint(prompt, v, _seen)
            return v
    except (OSError, EOFError):
        pass
    try:
        return _strip_keys(input(prompt))
    except (OSError, EOFError):
        print(prompt + "(无终端，按默认继续)")
        return ""

def _ask_free(prompt, confirm=True):
    """要打中文/emoji 的自由文本输入（前缀之类）：提示语单独占一行，输入从下一行的 > 开始。

       为什么不像别处那样提示和输入挤同一行：终端的行编辑按【字符】发退格，而中文和
       emoji 占【两列】——删一个字只擦掉一列，光标会越删越往前跑，最后啃进提示语里，
       看着像把界面删坏了（其实输入缓冲区是好的，只是屏幕花了）。让输入独占一行，
       删过头也只啃到那个 '> '，提示语毁不掉。

       输进来的不空，再单独确认一次（confirm）：仓库主人 10/11 输了个国旗、还没按回车就往下走了——
       手机输入法发表情时自己带了个回车，脚本分不出是不是人按的。确认这一步要真按回车才算；
       回别的就重输。后面自带「预览 + y 确认」的（更换前缀）传 confirm=False，不问两遍。"""
    print(prompt)
    while True:
        v = _ask("  > ")
        if not v or not confirm:
            return v
        if _ask(f"  「{v}」回车确认 / n 重输：").strip().lower() in ("", "y", "yes"):
            return v

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
    G["sni"]        = _current_sni("sb") or st.get("sni") or _current_sni() or G["sni"]   # 以节点正在用的为准：老版本「4 更换伪装域名」没回写记录
    G["sni_xr"]     = _current_sni("xray") or st.get("sni_xr") or ""   # xray 自己的借用域名（没有 = 跟 sing-box 共用一个，老装机）
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
    _XR_ENTRY.clear(); _XR_ENTRY_FROM_DISK[0] = True     # 已有 reality 入口就沿用它的密钥（见 _xr_entry_keys）
    # 同名协议（两核心都有）加小上标区分：跟已装的一起算，免得新旧重名
    dup = (set(have_sb) | set(pick_sb)) & (set(have_xr) | set(pick_xr))

    # 同删除：一个核心失败【不能】把另一个核心已经装好的节点丢在半路——那样节点在
    # 服务器上跑着、订阅里却没有，客户端永远看不到它，而且毫无报错。失败只 break，
    # 下面照样把已经装好的那部分写进链接、刷订阅、记进 state。
    new_links, failed = [], []
    nginx_redo = False                   # 新加的有挂在 nginx 后面的 → 装完按全部节点重建 nginx
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
        if core == "sb":
            sb_fix_dns(cfg)                     # 新建的、老的都顺手补上（见 sb_fix_dns）
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
        if core == "xray":
            cfg["inbounds"] = _xr_entry_fixup(cfg["inbounds"])
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
        nginx_redo = nginx_redo or any(ib.get("listen") == "127.0.0.1" for ib in add_ins)
        new_links += add_lks
        if core == "sb":
            # 去重：残留协议本来就还挂在记录里（配置里没有而已），直接相加会写进去两遍
            have_sb = list(dict.fromkeys(have_sb + picks))
        else:
            have_xr = list(dict.fromkeys(have_xr + picks))

    if not new_links:
        print("  没有新增任何节点。")
        return
    if nginx_redo:
        try:
            msg = _nginx_sync_from_cfg()
            if msg:
                print("  " + msg)
        except Exception as e:
            print(f"  ⚠ nginx 重建失败，挂在 443 后面的新节点可能连不上：{e}")

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

    st.update({"sb": have_sb, "xray": have_xr, "sni": G["sni"], "sni_xr": G.get("sni_xr", ""),
               "hy2_ports": G.get("hy2_ports", ""), "smux": G.get("smux", "")})
    try:
        json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=2)
    except OSError:
        pass
    fw_sync()                                            # 新加协议的端口也放行
    netprofile_sync()                                    # 网络优化那边也保留上

    # 和「2 查看链接」一个样：新节点一条一行、条间空行，三大订阅高亮
    ui_title(f"已添加 {len(new_links)} 个节点（现有节点未改动）")
    print_node_links(new_links)
    print_cloud_ports()
    if _sub_items():
        ui_title("订阅链接（地址没变，客户端重拉即可）")
        print_sub_urls()
    ui_line()
    if failed:
        # 失败那边已回滚、它下面选的协议一个没加；上面的已装好并写进订阅，修好后单独加那几个
        ui_warn(f"{'、'.join(failed)} 失败已回滚，那几个协议没加上")
    ui_tip("有聚合节点的话，先到主机点一次「更新配置」")

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

def _nginx_backends_from_cfg():
    """两个核心配置里挂在 nginx 后面（监听 127.0.0.1）的入站 → (ws/xhttp 反代表, reality 分流表)。
       分流表只存在于正在用的配置里，增删一个协议后照着剩下的重建（仓库主人 10/22：要能一个一个加、一个一个删）。
       reality：sing-box tls.server_name / xray realitySettings.serverNames[0] → 本地端口；
       ws：transport.path / wsSettings.path；xray xhttp：xhttpSettings.path（kind=xhttp，nginx 用 grpc_pass）。
       同一个借用域名只认第一个后端：nginx map 里重复的键 nginx -t 过不了。"""
    ws, stream, seen = [], [], set()
    for core in ("sb", "xray"):
        for ib in _load_core_cfg(core)[1]:
            if str(ib.get("listen", "")) not in ("127.0.0.1", "localhost"):
                continue
            port = _cfg_port(ib)
            if not port:
                continue
            if core == "sb":
                tls = ib.get("tls") or {}
                sni = tls.get("server_name") if isinstance(tls.get("reality"), dict) else ""
                tr = ib.get("transport") or {}
                if tr.get("type") == "grpc" and tr.get("service_name"):
                    path, kind = "/" + tr["service_name"], "grpc"
                else:
                    path, kind = tr.get("path"), "ws"
            else:
                if _xr_is_fb_backed(ib):             # 挂在 reality 入口后面的，走 _nginx_fb_server 那条路
                    continue
                st = ib.get("streamSettings") or {}
                rs = st.get("realitySettings") if st.get("security") == "reality" else None
                sni = ((rs or {}).get("serverNames") or [""])[0] if rs else ""
                net = st.get("network")
                path = ((st.get("wsSettings") or {}).get("path") if net == "ws" else
                        (st.get("xhttpSettings") or {}).get("path") if net == "xhttp" else None)
                kind = "xhttp" if net == "xhttp" else "ws"
            if sni:
                if sni not in seen:
                    seen.add(sni); stream.append({"sni": sni, "port": port})
            elif path:
                ws.append({"path": path, "port": port, **({"kind": kind} if kind in ("xhttp", "grpc") else {})})
    return ws, stream

def _nginx_sync_from_cfg():
    """按两个核心【当前的】配置重写 nginx：sni-split 写 stream 分流 + 本地 https；普通前置写 443 伪装站 + 反代。
       nginx -t 不过就把动过的文件原样放回去再 reload —— 退回【改之前那份能用的】，不是退回装分流之前
       （write_nginx_sni_split 自带的回滚是给首装用的，会把 stream 整个拆掉，443 就没了，这里不用它）。
       没开 nginx 前置 / reality 独占 443 的布局：nginx 不在路上，什么都不做。返回 屏上一行 或 ""。"""
    if not G.get("domain") or not (G.get("sni_split") or _nginx_front()):
        return ""
    ws, stream = _nginx_backends_from_cfg()
    NGINX_WS[:] = ws; NGINX_STREAM[:] = stream
    split = bool(G.get("sni_split"))
    files = [NGINX_CONF] + ([NGINX_STREAM_CONF, NGINX_MAIN] if split else [])
    old = {f: (open(f).read() if os.path.exists(f) else None) for f in files}
    if split:
        listen = nginx_http2_listen(f"127.0.0.1:{SNI_HTTPS_PORT} ssl") + nginx_http2_on()
    else:
        listen = (nginx_http2_listen("443 ssl") + _v6(nginx_http2_listen("[::]:443 ssl")) + nginx_http2_on())
    open(NGINX_CONF, "w").write(_nginx_80_server() + _nginx_https_server(listen)
                                + (_nginx_fb_server() if split else ""))
    if split:
        open(NGINX_STREAM_CONF, "w").write(_stream_conf_text())
        _nginxconf_add_stream()
    chk = subprocess.run("nginx -t", shell=True, text=True, capture_output=True)
    if chk.returncode:
        for f, txt in old.items():
            if txt is None:
                sh(f"rm -f {f}", check=False)
            else:
                open(f, "w").write(txt)
        sh("nginx -t && systemctl reload nginx", check=False)
        return "⚠ nginx 重建没通过校验，已退回原配置"
    nginx_reload()
    if split:
        return f"✔ nginx 443 分流已按现有节点重建（{len(stream)} 个 reality、{len(ws)} 个 ws/xhttp）"
    return f"✔ nginx 443 反代表已按现有节点重建（{len(ws)} 个 ws/xhttp）"

def _nginx_rules_old(text):
    """bgpeer.conf 还是老规则吗：挂在 443 后面的路径被摸到不回伪装站的 404 页（ws 是 444 断连接；xhttp / grpc 不带斜杠
       的路径没有单独回 404、会被 nginx 自动 301；xhttp 没把 xray 回的错误码换成 404 页）。只看对外的那部分——
       reality 入口回落用的本地 h2c（REALITY_FB_PORT）在最后，外面摸不到，不算。"""
    pub = text.split(f"127.0.0.1:{REALITY_FB_PORT}")[0]
    if "return 444;" in pub:
        return True
    if "error_page 404 /404.html;" in pub and "try_files" not in pub:   # 伪装站的目录（/posts/ 这种）回 403 而不是 404 页
        return True
    for m_ in re.finditer(r"location (/[^\s{]+)/ \{(.*?)\n  \}", pub, re.S):
        path, body = m_.group(1), m_.group(2)
        if "grpc_pass" not in body:
            continue
        if f"location = {path} {{" not in pub:
            return True
        if "application/grpc" not in body and "grpc_intercept_errors" not in body:
            return True
    return False

def _nginx_rules_heal():
    """老机器：bgpeer.conf 还是老规则（见 _nginx_rules_old）就按两个核心当前的配置重建一遍（仓库主人 10/23）。
       平时只读一下这个文件就走，重建过一次以后就是新规则，以后都跳过。重建用 nginx 平滑重载：连着的节点、
       在播的 Emby 都不断（铁律：不误伤播放）；nginx -t 不过放回原文件。不动节点、不换链接、不重启核心。
       返回屏上一行，没动返回 ""。"""
    if os.path.exists(NGINX_FB_CONF):
        ensure_site_fallback()                           # trojan 回落用的本机伪装站：内容没变就不动，变了重写 + 平滑重载
    try:
        text = open(NGINX_CONF).read()
        st = json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return ""
    if not _nginx_rules_old(text):
        return ""
    saved = dict(G)
    try:
        _restore_state_to_G(st)
        msg = _nginx_sync_from_cfg()
        return ("✔ nginx 规则已更新：443 后面的路径被人摸到，回伪装站的 404 页" if msg.startswith("✔") else msg)
    except Exception as e:
        return f"⚠ nginx 规则没更新成（{e}），原配置照用"
    finally:
        G.clear(); G.update(saved)

def _nginx_backed(names, core="sb"):
    """这个核心的这些协议里，哪些的入站是挂在 nginx 后面的（监听 127.0.0.1）。

       从运行中的配置读，不靠名字猜：ws 家族前置、sni-split 的 reality 后端、xray 的 xhttp 都是
       这个形态，以后再多一种也自动认得。"""
    local = {_ib_proto(ib) for ib in _load_core_cfg(core)[1] if ib.get("listen") == "127.0.0.1"}
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
    _hy2_hop_unpersist()
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
    ui_title("删除协议")
    stale_sb, stale_xr = _stale_protos(have_sb, have_xr)
    _mark = lambda lst, stale: ", ".join(n + (_hl("1;33", "（残留）") if n in stale else "") for n in lst)
    if have_sb:
        ui_kv("已装 sing-box", _mark(have_sb, stale_sb))
    if have_xr:
        ui_kv("已装 xray", _mark(have_xr, stale_xr))
    ui_line()
    ui_items([(1, "选择删除", None), (2, "全部删除", None), (0, "返回", None)])
    if stale_sb or stale_xr:
        # 残留：安装记录里有、核心配置里却没有它的入站。删它会把订阅和记录里剩下的部分清掉
        ui_tip("「残留」= 记录里有、配置里没有；删掉会清干净，也可从「1 只添加新协议」装回来")
    c = (_ask("选择: ") or "0").strip()
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

    # 挂在 nginx 后面的（ws/xhttp 反代、sni-split 的 reality 后端，两个核心都算）删掉后，
    # _del_apply 按【剩下的】节点把 nginx 整个重建一遍；nginx -t 不过退回原样（仓库主人 10/22：要能一个一个删）。
    nginx_redo = bool((G.get("sni_split") or _nginx_front()) and
                      (_nginx_backed(del_sb, "sb") or _nginx_backed(del_xr, "xray")))

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
    if nginx_redo:
        print("  连带清理:      nginx 443 按剩下的节点重建")
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
    # 删之前先记下：要删的里面有没有挂在 nginx 后面的（删完配置里就查不到了）
    nginx_redo = bool((G.get("sni_split") or _nginx_front()) and
                      (_nginx_backed(del_sb, "sb") or _nginx_backed(del_xr, "xray")))
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
        if core == "xray":
            # 删的是 reality 入口（vision²）、后面还挂着 grpc² / xhttp²：入口改名留下；后端删光了入口跟着拆
            vis = [ib for ib in gone if _xr_is_entry(ib)]
            keep = _xr_entry_fixup(keep, vis[0] if vis else None)
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

    if nginx_redo:
        try:
            msg = _nginx_sync_from_cfg()
            if msg:
                print("  " + msg)
        except Exception as e:
            print(f"{RED}  ⚠ nginx 重建失败，可能有残留，需手工确认：{e}{OFF}")

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
    netprofile_sync()                                    # 删掉的端口网络优化那边也放回去

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
        # 按表里还有的数：停用的协议（sing-box 的 reality-grpc）装着也不该把「还没装的」减成负数
        n_sb, n_xr = len([n for n in SB if n not in have_sb]), len([n for n in XRAY if n not in have_xr])
        # 1 只加新协议：老节点端口 / UUID / 密码 / 订阅地址全不变；2 全部重装：订阅地址也换，客户端要重新导入；
        # 3 删除协议：只删选中的，其余不动（含清理 DNAT / nginx 反代）
        ui_title("检测到本机已安装 bgpeer 节点")
        if have_sb:
            ui_kv("已装 sing-box", ", ".join(have_sb))
        if have_xr:
            ui_kv("已装 xray", ", ".join(have_xr))
        ui_kv("还没装的", f"sing-box {n_sb} 个 / xray {n_xr} 个")
        ui_line()
        ui_items([(1, "只添加新协议（老节点不动，推荐）", None),
                  (2, "全部重新安装（订阅地址会换）", None),
                  (3, "删除协议（只删选中的）", None),
                  (0, "返回", None)])
        # 回车默认 0：这几条都会动正在跑的节点，不该靠误按回车触发
        ans = (_ask("选择: ") or "0").strip()
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
    ui_items([(1, "sing-box（默认）", None), (2, "xray", None), (3, "两个都装", None)])
    core = _ask("选择核心（回车 = 1）: ") or "1"

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
    if {"reality-vision", "reality-grpc"} & set(xr_names):
        # xray 新版 reality 只收带 X25519MLKEM768 的客户端，sing-box 系客户端握手被拒（见 _local_xray_reality），
        # sing-box 订阅会自动跳过这几条；sing-box 内核开的 reality 所有客户端都能连
        ui_tip("xray 的 reality-vision / grpc：sing-box App / Hiddify / NekoBox 连不上，给它们用请装在 sing-box 内核")

    # 证书可能是先在『15 域名证书管理』里装好的（那边不需要先有节点）。这里认出来，
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
    if not domain:
        # 对齐 mack-a：没域名只给 reality（为什么见 selfsigned_risky）。默认去掉，回 n 照装（装时 warn_selfsigned 再提醒一行）
        drop = selfsigned_risky(sb_names, xr_names)
        if drop:
            ui_warn(f"没域名：{'、'.join(drop)} 只能自签证书，一被探测就露馅、容易封 IP")
            if (_ask("去掉这几个、只装 reality / hy2？（回车 = 去掉 / n = 照装）: ").strip().lower() or "y") not in ("n", "no"):
                sb_names = [n for n in sb_names if n not in drop]
                xr_names = [n for n in xr_names if n not in drop]
                if not sb_names and not xr_names:
                    print("去掉后没剩协议，退出。没域名请选 reality-vision / reality-grpc / reality-xhttp / hy2。"); return
    nginx = ""
    if domain:
        nginx = "1" if (_ask("用 nginx 前置(443伪装站+webroot证书, ws类藏443)? [y/N]: ")
                        .lower() in ("y", "yes")) else ""
    # reality 伪装域名：两个核心都选了 reality 就各问一个、不许重复（仓库主人 10/22：sni-split 下 nginx 按伪装域名
    # 分流，xray 的 reality-vision 也挂 443 后面，同一个域名分不出该给谁）。回车随机挑（不同机器各不同，不扎堆）；
    # 随机挑的探不过，装的时候自己换一个（见 _sni_precheck_one）。没选 reality 也问第一个：无域名时 ws/trojan 拿它当伪装 Host。
    r_sb, r_xr = proto_flag("reality", sb_names), proto_flag("reality", (), xr_names)
    sni, sni_auto = ask_reality_sni("sing-box 的 reality 伪装域名" if r_sb and r_xr else
                                    "xray 的 reality 伪装域名" if r_xr else "reality 伪装域名（借用目标站 SNI）")
    G["sni_auto"] = "1" if sni_auto else ""
    sni_xr = ""
    if r_sb and r_xr:
        sni_xr, xr_auto = ask_reality_sni("xray 的 reality 伪装域名", avoid=sni)
        G["sni_xr_auto"] = "1" if xr_auto else ""
    G["sni_xr"] = sni_xr
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
    print("  伪装域名:", sni + (f"（sing-box）/ {sni_xr}（xray）" if sni_xr else ""))
    for label, value in recap_lines(sb_names, xr_names):
        if label in ("sing-box 伪装域名", "xray 伪装域名"):   # 上面那行已经报过了，别重复
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
    harden_perms()                  # 每次进来先把 token/私钥收成 root-only（幂等）
    if len(sys.argv) == 1:          # 不带参数 → 管理面板（bgpeer 也走这里）
        main_menu()
        sys.exit(0)
    if sys.argv[1] == "update-cores":   # 非交互：cron 每月自动更新、菜单19 转后台都调这个
        update_cores_auto(sys.argv[2] if len(sys.argv) > 2 else None)   # 可选 sing-box/xray/both
        sys.exit(0)
    if sys.argv[1] == "self-update":     # cron 每天北京 02:50：脚本本体更新到最新（菜单 18 开关）
        _auto_self_update()
        sys.exit(0)
    if sys.argv[1] == "cfg-auto":        # cron 每天北京 03:20：三大配置按当前模板自动更新
        cfg_auto_run()
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
    if sys.argv[1] == "privdns":         # adguard 菜单 11 调用：节点使用私有DNS解析
        privdns_menu()
        sys.exit(0)
    if sys.argv[1] == "privdns-off":     # adguard 卸载调用：本机在当 DNS 机就关掉，外机节点改回真实地址
        privdns_off()
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
    ap.add_argument("--sni-xr", default="", help="xray 的 reality 借用目标站（两个核心都装 reality 时用；不填=随机挑一个跟 --sni 不同的）")
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
    G["sni_auto"] = "" if a.sni else "1"                # 没给 --sni 是随机挑的：探不过自己换（见 _sni_precheck_one）
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
        if "reality-grpc" in bad_sb:
            ap.error("sing-box 的 reality-grpc 已停用（随机端口容易被阻断），grpc 请用 --sb grpc-tls 或 --xray reality-grpc")
        if bad_sb: ap.error(f"未知的 sing-box 协议: {','.join(bad_sb)}\n  可选: {','.join(SB)}")
        ap.error(f"未知的 xray 协议: {','.join(bad_xr)}\n  可选: {','.join(XRAY)}")
    # 两个核心都装 reality：xray 用自己的伪装域名，跟 sing-box 的不能一样（见 ask_reality_sni）
    G["sni_xr"] = G["sni_xr_auto"] = ""
    if proto_flag("reality", sb) and proto_flag("reality", (), xr):
        if a.sni_xr and a.sni_xr == G["sni"]:
            ap.error(f"--sni-xr 跟 --sni 一样（{a.sni_xr}）：两个核心的伪装域名不能重复")
        G["sni_xr"] = a.sni_xr or secrets.choice([x for x in REALITY_SNI_POOL if x != G["sni"]])
        G["sni_xr_auto"] = "" if a.sni_xr else "1"
    run(sb, xr)

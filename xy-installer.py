#!/usr/bin/env python3
# ============================================================================
# sb-installer —— sing-box + xray 双核心多协议一键安装器（数据驱动）
# ----------------------------------------------------------------------------
# 设计原则（对应“逻辑和加密要做好”）：
#   1. 密钥一律调用核心自带生成器，绝不在 Python 里手搓 x25519 / UUID
#   2. 版本钉死：sing-box 1.11.x（避开 1.12 inbound sniff 迁移的破坏性改动）
#                xray 25.x（reality 传输用 raw；ws 已 deprecated 但仍可用）
#   3. 证书三态：reality 借目标站证书(无需域名) / hy2·tuic·anytls 自签 /
#                ws·trojan 给域名走 acme.sh，不给则自签 + 链接带 insecure
#   4. 加协议 = 往 SB / XRAY 表里加一个 builder，返回 (inbound, share_link)
#
# ⚠️ 已按官方当前文档核对字段，但未做运行时测试。上线前每个协议自测一遍，
#    并对照你 VPS 上实际 sing-box/xray 版本确认 schema（版本会漂）。
# 目标系统：debian / ubuntu（apt）。用法见文件末尾 --help。
# ============================================================================
import os, json, base64, secrets, uuid, argparse, subprocess, urllib.request, urllib.parse, urllib.error, shutil, socket, re, time

# 版本：安装时优先取 GitHub 最新正式版；下面是取不到时的兜底。
# ⚠ sing-box 必须 ≥1.12（anytls inbound 是 1.12 才加的，1.11 会 FATAL: unknown inbound type: anytls）
SB_VER   = "1.12.0"
XRAY_VER = "25.3.6"
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
BT_STATE     = BGP_DIR + "/bt.json"          # BT/PT 下载屏蔽开关状态
SUB_PORT     = 20080
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

# 网络优化脚本已并入本仓库（net-optimize.py，BBR/QoS 等内核调优，依赖工具自动安装）；
# 主脚本只负责拉取+调用，状态检测走同一脚本的 --check。
NETOPT_LOCAL = BGP_DIR + "/net-optimize.py"      # 本地缓存的网络优化脚本
NETOPT_URL   = _RAW + "net-optimize.py"

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
        if p in _USED_PORTS or p == SUB_PORT:
            continue
        if hop and hop[0] <= p <= hop[1]:       # hy2 跳跃段，留给 hy2
            continue
        if not port_free(p):                    # 系统层面已被别的进程占用
            continue
        _USED_PORTS.add(p)
        return p
    raise RuntimeError(f"在 {PORT_LO}-{PORT_HI} 内找不到可用端口，请检查端口占用。")

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

# 建议的 reality 借用目标：都是大流量、支持 TLS1.3+h2、不在国内、不套 CDN 的站
SNI_SUGGESTIONS = "www.microsoft.com / addons.mozilla.org / s0.awsstatic.com / dl.google.com"

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
    ver = latest_gh_release("XTLS/Xray-core", XRAY_VER)
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
    st = {"network": network, "security": "reality",
          "realitySettings": {"show": False, "dest": f"{G['sni']}:443",
                              "xver": 0, "serverNames": [G["sni"]],
                              "privateKey": priv, "shortIds": [sid]}}
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
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=reality"
          f"&sni={G['sni']}&fp=chrome&pbk={pub}&sid={sid}&type=xhttp&path={path}#{tag}")
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

def build(table, names, pinned=None, dup=None, mark=""):
    """pinned: {协议名: 固定端口}，用于把某个 reality 协议钉在 443；其余走随机端口。
       dup/mark: 两核心同名协议(vless-ws/vmess-ws/trojan)集合 dup 里的，名字尾部加个小上标
                 mark 区分（sing-box=¹ / xray=²），避免客户端订阅重名报错；比 -xray 后缀短，
                 手机上也显示得下。"""
    pinned = pinned or {}
    dup = dup or set()
    inbounds, links = [], []
    for n in names:
        # 名称 = 用户前缀 + 协议名（默认无前缀，别人部署 US/SG 时自己填 🇺🇸/🇸🇬 等）
        port = pinned.get(n) or next_port()
        tag = G.get("prefix", "") + n
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
            elif net == "xhttp": d["network"] = "xhttp"; d["xhttp-opts"] = {"path": qs.get("path", "/")}  # xray 专属
            else: d["network"] = "tcp"
        else:
            if insec: d["skip-cert-verify"] = "true"
            if net == "ws": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}}
            elif net == "httpupgrade": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}, "v2ray-http-upgrade": "true"}
            elif net == "grpc": d["network"] = "grpc"; d["grpc-opts"] = {"grpc-service-name": qs.get("serviceName") or qs.get("path", "")}
            elif net == "xhttp": d["network"] = "xhttp"; d["xhttp-opts"] = {"path": qs.get("path", "/")}
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
    return f"{scheme}://{_host()}:{SUB_PORT}/{t}.{ext}"

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
    return f"{scheme}://{_host()}:{SUB_PORT}/{t}.links"

def rotate_token_ext(ext):
    t = load_tokens(); t[ext] = secrets.token_urlsafe(12); save_tokens(t); serve_sub()

def rotate_links_token():
    """换 .links token：旧地址立即失效（防泄露）。聚合了本机的主机需重新复制新地址。"""
    t = load_tokens(); t["links"] = secrets.token_urlsafe(12); save_tokens(t); serve_sub()

# 订阅托管小服务：有 cert/key 参数就起 HTTPS，否则明文 HTTP（用法：port dir [cert key]）
_SUB_SERVER_PY = (
    "import http.server, functools, ssl, sys\n"
    "port = int(sys.argv[1]); directory = sys.argv[2]\n"
    "cert = sys.argv[3] if len(sys.argv) > 3 else ''\n"
    "key  = sys.argv[4] if len(sys.argv) > 4 else ''\n"
    "H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)\n"
    "httpd = http.server.ThreadingHTTPServer(('0.0.0.0', port), H)\n"
    "if cert and key:\n"
    "    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)\n"
    "    ctx.load_cert_chain(cert, key)\n"
    "    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)\n"
    "httpd.serve_forever()\n"
)

def serve_sub(reset=False):
    """SUB_DIR 放 <token>.<ext> 软链指向各格式配置文件；每格式独立 token（存 TOKENS_FILE）。
       reset=True 换全部 token；否则复用已有、只给新出现的格式补 token。"""
    os.makedirs(SUB_DIR, exist_ok=True)
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
    args = f"{SUB_PORT} {SUB_DIR}" + (f" {ACME_CRT} {ACME_KEY}" if https else "")
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
        return {"tag": tag, "type": "trojan", "server": srv, "server_port": int(d["port"]),
                "password": d["password"],
                "tls": {"enabled": True, "server_name": sni, "insecure": insec,
                        "alpn": ["http/1.1"], "utls": utls}}
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

def _sb_country_groups(tags):
    """sing-box 国家随机组：无 filter，按正则算好每国成员显式列入 urltest。
       返回 (国家组对象列表, 组名列表)。tag 用原始名（保留 🇺🇲 等，匹配用归一名）。"""
    present = detect_countries(tags)
    if not present:                                      # 没有任何国家 → 不建组（含"其他随机"），与 mihomo 一致
        return [], []
    objs, names = [], []
    mk = lambda tag, members: {"tag": tag, "type": "urltest", "outbounds": members,
                               "url": "https://www.gstatic.com/generate_204",
                               "interval": "120s", "tolerance": 30}
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in tags if rx.search(_norm_us_flag(t))]
        objs.append(mk(gname, members)); names.append(gname)
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in tags if not any(r.search(_norm_us_flag(t)) for r in rxs)]
        if omembers:
            objs.append(mk(OTHER_GROUP, omembers)); names.append(OTHER_GROUP)
    return objs, names

def build_singbox_sub(nodes, tpl_url):
    """对象级替换锚点：__XY_NODES__ 换节点对象、__XY_GROUPS__ 换国家组、
       __XY_GROUP_NAMES__ 展开国家组名、__PATTERN__:正则 展开命中节点名，再按手写风格序列化。"""
    cfg = json.loads(fetch_url(tpl_url))
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
    country_objs, country_names = _sb_country_groups(tags)
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
    _sb_direct_ip(cfg, _direct_ips(nodes))                    # 各 VPS IP 直连（走紧凑序列化，不破坏格式）
    open(SBOX_FILE, "w").write(sb_dumps(cfg))

# --- Shadowrocket [Proxy] 行：从 mihomo 参数转（名称带国旗前缀让分组正则命中）---
def shadowrocket_line(name, d):
    t = d.get("type"); srv = d["server"]; port = d.get("port")
    sni = d.get("servername") or d.get("sni") or srv
    scv = "1" if d.get("skip-cert-verify") else "0"
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
        return ",".join([f"{name} = trojan", srv, str(port), f"password={d['password']}",
                         "tls=1", f"sni={sni}", f"skip-cert-verify={scv}", "tfo=1"])
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

def _sr_static_names(tpl):
    """抽取 shadowrocket 模板 [Proxy] 段里用户手写的静态节点名（"名 = 协议,..." 行）。"""
    m = re.search(r"(?ms)^\[Proxy\]\s*\n(.*?)(?=^\[|\Z)", tpl)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if ln and "=" in ln and not ln.startswith("#") and "__XY" not in ln:
            out.append(ln.split("=", 1)[0].strip())
    return out

def _sr_country_groups(names_list):
    """shadowrocket 国家随机组：显式列成员（不依赖 shadowrocket 正则引擎，稳）。
       返回 (组定义行文本, 拼进服务组的组名片段[前导逗号, 裸名])。"""
    present = detect_countries(names_list)
    if not present:                                      # 没有任何国家 → 不建组（含"其他随机"），与 mihomo 一致
        return "", ""
    U = "url=http://www.gstatic.com/generate_204,interval=120,tolerance=30,timeout=5"
    lines, gnames = [], []
    for gname, pat, _ in present:
        rx = re.compile(pat)
        members = [t for t in names_list if rx.search(_norm_us_flag(t))]
        lines.append(f"{gname} = url-test,{','.join(members)},{U}")
        gnames.append(gname)
    if OTHER_GROUP:
        rxs = [re.compile(p) for _, p, _ in present]
        omembers = [t for t in names_list if not any(r.search(_norm_us_flag(t)) for r in rxs)]
        if omembers:
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
    tpl = fetch_url(tpl_url)
    # 国家检测/成员池 = 注入节点 + 用户手写进模板 [Proxy] 段的静态节点（"名 = 协议,..." 行）
    static = _sr_static_names(tpl)
    groups_txt, names_frag = _sr_country_groups(names_list + static)
    out = tpl
    out = _fill_block(out, "__XY_NODES__", "\n".join(lines))    # 块锚点整行替换，缩进容错
    out = _fill_block(out, "__XY_GROUPS__", groups_txt)
    out = out.replace("__XY_NAMES__", names_frag)               # 行内锚点
    open(SR_FILE, "w").write(out)

# --- 三格式元数据：文件 / 作者模板 / 生成器；自定义模板存 CUSTPL_FILE ---
def _node_names(nodes):
    """从解析后的节点取名字列表（去引号），供国家检测用。"""
    return [d.get("name", "").strip('"') or k for k, d in nodes]

def _mihomo_country(names):
    """mihomo 国家随机组：返回 (组定义 yaml 行, 拼进🌍全球加速的组名片段)。无国家则空串。
       用 filter+include-all，客户端按正则自动收拢；filter 用单引号 YAML 串避免 \\b 被转义。"""
    present = detect_countries(names)
    if not present:
        return "", ""
    # hidden: true 让国家组不占面板卡片位（仍可在🌍全球加速里选到）；显式写在组上，
    # 覆盖 <<: *COUNTRY_COMMON，自定义模板不改锚点也生效。
    lines, gnames = [], []
    for gname, pat, _ in present:
        lines.append(f"  - {{name: \"{gname}\", <<: *COUNTRY_COMMON, filter: '{pat}', hidden: true}}")
        gnames.append(gname)
    if OTHER_GROUP and other_members(names, present):          # 有漏网节点才建"其他随机"
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

def _direct_ips(nodes):
    """要直连的 IP：本机 IP + 所有节点服务器里的 IP 字面量（多机聚合后自动覆盖各成员机 IP，
       这样挂着聚合代理管理任意一台，SSH 都走直连、不被重启核心掐断）。"""
    ips, seen = [], set()
    si = _self_ip()
    if si:
        ips.append(si); seen.add(si)
    for _, d in nodes:
        s = str(d.get("server", "")).strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", s) and s not in seen:
            seen.add(s); ips.append(s)
    return ips

def _mihomo_direct_ip(tpl, ips):
    """mihomo：在 rules: 段顶部插各 VPS IP 直连，避免挂本机代理管理时 SSH 被路由进代理。"""
    if not ips or "rules:" not in tpl:
        return tpl
    new = [f"  - IP-CIDR,{ip}/32,DIRECT,no-resolve" for ip in ips
           if f"IP-CIDR,{ip}/32,DIRECT,no-resolve" not in tpl]
    if not new:
        return tpl
    return re.sub(r"(?m)^rules:[ \t]*$", "rules:\n" + "\n".join(new), tpl, count=1)

def _sb_direct_ip(cfg, ips):
    """sing-box：把各 VPS IP 直连规则插到 route.rules 最前（引用模板里的 🎯直连 出站）；
       就地改 cfg dict，交由 sb_dumps 按模板的紧凑风格序列化——不破坏格式。"""
    if not ips:
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
    for ip in ips:
        rule = {"ip_cidr": [f"{ip}/32"], "outbound": direct}
        if rule not in rules and rule not in add:
            add.append(rule)
    if add:
        route["rules"] = add + rules

def _sr_direct_ip(path, ips):
    """Shadowrocket：在 [Rule] 段顶部插各 VPS IP 直连。"""
    if not ips:
        return
    try: tpl = open(path).read()
    except OSError: return
    if "[Rule]" not in tpl:
        return
    new = [f"IP-CIDR,{ip}/32,DIRECT,no-resolve" for ip in ips
           if f"IP-CIDR,{ip}/32,DIRECT,no-resolve" not in tpl]
    if new:
        open(path, "w").write(tpl.replace("[Rule]", "[Rule]\n" + "\n".join(new), 1))

def gen_mihomo(ylines, nodes, tpl_url):
    tpl = fetch_url(tpl_url)
    # 国家检测要看"全部节点"：注入的订阅节点 + 用户手写进模板的静态节点。
    # 静态节点名取 proxy-groups 段之前的 name:（策略组名在 proxy-groups 里，且不含国旗，不会误检）。
    static = re.findall(r'name:\s*"([^"]*)"', tpl.split("proxy-groups:")[0])
    groups_yaml, names_frag = _mihomo_country(_node_names(nodes) + static)
    # 块锚点(独占一行)整行替换，缩进容错：__XY_NODES__ 建节点 / __XY_GROUPS__ 建国家组
    tpl = _fill_block(tpl, "__XY_NODES__", "\n".join(ylines))
    tpl = _fill_block(tpl, "__XY_GROUPS__", groups_yaml)
    tpl = tpl.replace("__XY_NAMES__", names_frag)          # 行内锚点：引用组名，原样替换
    tpl = _mihomo_direct_ip(tpl, _direct_ips(nodes))       # 各 VPS IP 直连（防管理时 SSH 走代理）
    open(CFG_FILE, "w").write(tpl)
def gen_singbox(ylines, nodes, tpl_url):
    build_singbox_sub(nodes, tpl_url)                        # 直连规则已在内部注入并紧凑序列化
def gen_shadow(ylines, nodes, tpl_url):
    build_shadowrocket_sub(nodes, tpl_url)
    _sr_direct_ip(SR_FILE, _direct_ips(nodes))

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
def tpl_url_for(ext, custom=False):
    return (load_custpl().get(ext) if custom else "") or FMT[ext]["author"]

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
            meta["gen"](ylines, nodes, tpl_url_for(ext, custom=True))
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
        print(f"※ {proto}，请勿外传；改端口/关闭见 xy-sub.service（端口 {SUB_PORT}）")

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
        print(f'内核已设为每月自动更新一次（{_core_update_schedule_str()}）；也可随时进菜单 12 手动立即更新。')
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
            n = _ask("  删除哪个编号: ").strip()
            if n.isdigit() and 1 <= int(n) <= len(peers):
                print("  已删除:", peers.pop(int(n) - 1)); save_peers(peers)
            else:
                print("  编号无效。")
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

def update_one_config(ext):
    """更新单个格式的配置：可选作者模板 / 自定义模板；不动节点、不换 token。"""
    print("\n  1 作者模板   2 自定义模板   0 返回")
    c = _ask("  选择: ").strip()
    if c == "1":
        url = FMT[ext]["author"]; which = "作者"
    elif c == "2":
        url = load_custpl().get(ext)
        if not url:
            print("  还没添加自定义模板链接（先选『4 添加自定义模板链接』）。"); return
        which = "自定义"
    else:
        return
    if not read_saved_links():
        print("  没有已保存节点。"); return
    G["host"] = _host(); ensure_deps()
    links = aggregated_links()                                  # 本机 + 成员机节点（多机聚合）
    ylines, nodes = parse_nodes(links)
    target = FMT[ext]["file"]
    backup = open(target).read() if os.path.exists(target) else None
    try:
        FMT[ext]["gen"](ylines, nodes, url)
    except Exception as e:
        if backup is not None: open(target, "w").write(backup)      # 回滚，保留原能用配置
        print(f"\n  ❌ 更新失败（生成出错，已保留原配置）：{e}"); return
    ok, err = _validate_generated(ext, target)
    if not ok:
        if backup is not None: open(target, "w").write(backup)      # 语法/校验不过 → 回滚
        print(f"\n  ❌ 更新失败（{FMT[ext]['label']} 语法/校验错误，已保留原配置）：")
        for ln in str(err).splitlines()[:6]:
            print("     " + ln)
        return
    serve_sub()                                                     # 保持 token，URL 不变
    print(f"\n  ✅ 更新成功（{which}模板，节点/URL 未变）：\n  {sub_url(ext)}")

def config_menu(ext):
    """单个格式的配置子菜单：改配置 / 改订阅(换token) / 更新配置(作者·自定义) / 加自定义模板链接。"""
    meta = FMT[ext]
    if not os.path.exists(meta["file"]):
        print(f"\n还没有 {meta['label']} 配置，请先『1.安装』。"); return
    while True:
        cust = load_custpl().get(ext)
        print("\n" + "=" * 60 + f"\n{meta['label']} 配置\n" + "=" * 60)
        print(f"  配置文件: {meta['file']}")
        print(f"  当前订阅: {sub_url(ext)}")
        print(f"  自定义模板: {cust or '(未设置)'}")
        print("-" * 60)
        print("  1 修改配置（编辑器打开）")
        print("  2 修改订阅（显示当前 / 换 token）")
        print("  3 更新配置（作者模板 / 自定义模板）")
        print("  4 添加自定义模板链接")
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
            if cur:                                     # 加过了：先显示当前链接，问要不要换
                print(f"  已添加过自定义模板链接：{cur}")
                if _ask("  是否更换? [y/N]: ").lower() not in ("y", "yes"):
                    continue                            # n 返回菜单，不动原链接
            url = _ask("  自定义模板链接(gist/GitHub raw，占位符须与作者模板一致): ").strip()
            if url:
                set_custpl(ext, url); print("  已保存。之后『3→2 自定义模板』即用它。")
        elif c == "0" or c == "":
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
        print("\n已是最新版本。（若刚发布过新版还没看到，多半是 GitHub/镜像缓存未刷新，稍后再试）")
        return
    install_shortcut(latest)
    print("\n脚本已更新（节点/配置均未改动），正在重新载入新版面板…")
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

def update_cores_auto():
    """非交互更新已安装的内核到最新并重启（cron 每月调用）。起不来会记进日志。"""
    ensure_deps()
    ts = time.strftime("%F %T")
    for name, binpath, installer in (("sing-box", SB_BIN, install_singbox),
                                     ("xray", XRAY_BIN, install_xray)):
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

def update_cores():
    print("\n更新核心:  1. sing-box   2. xray   3. 两个   0. 返回")
    print(f"  （每月自动更新已开启：{_core_update_schedule_str()}）")
    c = _ask("选择: ")
    if c == "0" or not c:
        return
    ensure_deps()
    if c in ("1", "3") and os.path.exists(SB_BIN):
        install_singbox(); sh("systemctl restart sing-box", check=False)
        v = sh(f"{SB_BIN} version", check=False)
        print("sing-box 现版本:", v.splitlines()[0] if v else "?")
    if c in ("2", "3") and os.path.exists(XRAY_BIN):
        install_xray(); sh("systemctl restart xray", check=False)
        print("xray 已更新")
    setup_core_update_cron()                             # 顺手确保每月自动更新的 cron 在
    print("更新完成。")

def uninstall_all():
    print("\n将卸载本脚本安装的：sing-box/xray/订阅服务、配置、证书、端口跳跃规则、bgpeer 命令。")
    if (_ask("确认卸载? [y/N]: ") or "n").lower() not in ("y", "yes"):
        print("已取消。"); return
    for svc in ("sing-box", "xray", "xy-sub"):
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
    for p in (SB_BIN, XRAY_BIN, SB_DIR, XRAY_DIR, "/etc/ssl/sb", SUB_DIR,
              "/root/xy-nodes.txt", "/usr/local/bin/bgpeer", "/etc/bgpeer", WEBROOT,
              # cn-block 的每日刷新 cron、内核每月更新 cron 及日志：不清掉 cron 会调已删脚本报错
              "/etc/cron.d/bgpeer-cnblock", "/var/log/bgpeer-cnblock.log",
              CORE_CRON_FILE, CORE_CRON_LOG):
        sh(f"rm -rf {p}", check=False)
    print("已卸载完毕。")

# ============================================================================ 屏蔽中国域名/IP（独立文件）
def ensure_remote_script(url, local):
    """把仓库里的脚本拉到本地（每次尽量拉最新）；拉不到就用本地缓存。"""
    os.makedirs(BGP_DIR, exist_ok=True)
    try:
        open(local, "w").write(fetch_url(url))
    except Exception:
        pass
    return os.path.exists(local)

def ensure_cn_block():
    return ensure_remote_script(CN_BLOCK_URL, CN_BLOCK_LOCAL)

def cn_block_menu():
    """打开独立的 cn-block.py 交互菜单（屏蔽 CN 域名/IP + 白名单）。"""
    if not ensure_cn_block():
        print("拉取 cn-block.py 失败，且本地无缓存。请检查网络。"); return
    subprocess.run(f"python3 {CN_BLOCK_LOCAL}", shell=True)

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

def net_optimize_menu():
    """网络优化（本仓库 net-optimize.py：BBR/QoS/缓冲区等内核调优，依赖工具自动安装）。"""
    while True:
        print("\n" + "=" * 60)
        print("  网络优化（BBR / QoS 内核调优，依赖工具自动安装）")
        print("=" * 60)
        print("  1 自适应智能算法+抢占带宽（流量 10MB/s 激活，适合内存 <1G 机器）")
        print("  2 自适应智能算法+抢占带宽（默认 20MB/s 激活、阈值可调，适合内存 2G 左右机器）")
        print("  3 固定 cake 纯智能算法（不切换，适合高性能机器）")
        print("  4 网络优化状况（一键检测当前优化状态）")
        print("  5 卸载网络优化（清除全部优化配置，恢复系统默认）")
        print("  0 返回")
        c = _ask("选择: ").strip()
        if c == "1":
            _run_net_optimize()
        elif c == "2":
            t = _ask("  激活阈值 MB/s（回车=20）: ").strip() or "20"
            try:    mb = float(t)
            except ValueError: mb = 0
            if mb <= 0:
                print("  无效数字，请输入正数（如 20）。"); continue
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

def main_menu():
    while True:
        print("\n" + "=" * 60)
        print("  bgpeer 一键脚本  （sing-box + xray 多协议 / 订阅）")
        print("=" * 60)
        print("  1. 安装（已装则问是否重装节点，y 重装 / n 返回）")
        print("  2. 节点链接 / 订阅")
        print("  3. 聚合节点链接（多机汇总：把别的 VPS 节点并进来）")
        print("  4. 多路复用开关 smux（只针对 ws / httpupgrade 协议）")
        print("  5. mihomo 配置")
        print("  6. sing-box 配置")
        print("  7. 小火箭配置")
        print("  8. 屏蔽中国域名和IP（CN 域名+IP 拦截 / 白名单放行）")
        print("  9. BT/PT 下载屏蔽（防 VPS 被投诉封机）")
        print("  10. 网络优化（BBR/QoS 内核调优）")
        print("  11. 更新脚本（不影响节点）")
        print("  12. 更新核心（sing-box / xray）")
        print("  13. 卸载")
        print("  0. 退出")
        print("-" * 60)
        c = _ask("请选择: ").strip()
        if c == "0" or c == "":
            print("再见。"); return
        if c == "1":     install_flow()
        elif c == "2":   show_links()
        elif c == "3":   peers_menu()
        elif c == "4":   smux_menu()
        elif c == "5":   config_menu("yaml")
        elif c == "6":   config_menu("json")
        elif c == "7":   config_menu("conf")
        elif c == "8":   cn_block_menu()
        elif c == "9":   bt_menu()
        elif c == "10":  net_optimize_menu()
        elif c == "11":  update_script()
        elif c == "12":  update_cores()
        elif c == "13":  uninstall_all()
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
    email = _ask("acme 注册邮箱(回车=默认): ") if domain else ""
    nginx = ""
    if domain:
        nginx = "1" if (_ask("用 nginx 前置(443伪装站+webroot证书, ws类藏443)? [y/N]: ")
                        .lower() in ("y", "yes")) else ""
    sni = _ask("reality 借用目标站 SNI (回车=s0.awsstatic.com): ") or "s0.awsstatic.com"
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
    if sys.argv[1] == "update-cores":   # 非交互：cron 每月自动更新内核调这个
        update_cores_auto()
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
    ap.add_argument("--sni", default="s0.awsstatic.com", help="reality 借用的目标站")
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
        a.domain, a.email, a.sni, a.prefix, a.hy2_ports, ("1" if a.nginx else ""), a.yes
    G["reality443"] = "" if a.no_reality_443 else "1"   # 默认把 reality 绑 443（抗封端口）
    G["sni_split"] = "1" if a.sni_split else ""         # 最强：nginx SNI 分流，全上 443
    G["smux"] = "1" if a.smux else ""                   # ws 类多路复用，默认关
    sb = list(SB) if a.sb == "all" else [x for x in a.sb.split(",") if x]
    xr = list(XRAY) if a.xray == "all" else [x for x in a.xray.split(",") if x]
    if not sb and not xr:
        ap.error("至少用 --sb 或 --xray 指定要装的协议")
    run(sb, xr)

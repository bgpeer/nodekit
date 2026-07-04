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
import os, json, base64, secrets, uuid, argparse, subprocess, urllib.request, urllib.parse, shutil, socket, re

# 版本：安装时优先取 GitHub 最新正式版；下面是取不到时的兜底。
# ⚠ sing-box 必须 ≥1.12（anytls inbound 是 1.12 才加的，1.11 会 FATAL: unknown inbound type: anytls）
SB_VER   = "1.12.0"
XRAY_VER = "25.3.6"
SB_BIN, XRAY_BIN = "/usr/local/bin/sing-box", "/usr/local/bin/xray"
SB_DIR,  XRAY_DIR = "/etc/sing-box", "/usr/local/etc/xray"
CERT, KEY = "/etc/ssl/sb/self.crt", "/etc/ssl/sb/self.key"     # 自签
ACME_CRT, ACME_KEY = "/etc/ssl/sb/acme.crt", "/etc/ssl/sb/acme.key"  # acme 签发

# 全局状态：域名/邮箱/SNI 由 CLI 注入；端口自增分配
G = {"host": "", "domain": "", "email": "", "sni": "s0.awsstatic.com", "prefix": "", "_port": 20000}
HY2_PORTS = "30000-31000"      # hy2 端口跳跃范围默认值；用户可自定义（--hy2-ports / 菜单）

def hy2_range():
    """取 hy2 端口跳跃范围：用户自定义优先，格式须 起-止（如 30000-31000），否则回落默认。"""
    r = (G.get("hy2_ports") or HY2_PORTS).strip()
    return r if re.match(r"^\d+-\d+$", r) else HY2_PORTS

# 订阅：把节点注入 Mihomo 模板写成【可编辑配置文件】，HTTP 服务托管，产出订阅链接。
# 换订阅链接只换 token（软链名），不动配置；用户可直接编辑 CFG_FILE 改参数。
BGP_DIR      = "/etc/bgpeer"
CFG_FILE     = BGP_DIR + "/mihomo.yaml"      # 可编辑的成品配置（订阅内容就是它）
SUB_DIR      = BGP_DIR + "/sub"              # HTTP 托管目录（内含 <token>.yaml 软链 → CFG_FILE）
HOST_FILE    = BGP_DIR + "/sub.host"         # 记住订阅用的 host（域名或 IP），换 token 时保持不变
SUB_PORT     = 20080
TEMPLATE_URL = "https://raw.githubusercontent.com/bgpeer/nodekit/main/sub-template.yaml"

# nginx 前置（可选，需域名）：nginx 在 443 终结 TLS + 伪装站 + 按 path 反代 ws 家族；
# webroot 签证书。Vision/anytls/trojan/reality/hy2/tuic 因协议性质仍走各自端口。
NGINX_CONF = "/etc/nginx/conf.d/bgpeer.conf"
WEBROOT    = "/var/www/bgpeer"
NGINX_WS   = []                 # 运行期收集：ws 家族的 {path, port}，供 nginx location 反代

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

def next_port():
    G["_port"] += 1
    return G["_port"]

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
        # 装证书后让 acme 自动 reload nginx（续期时也会）
        reload_hook = f" --reloadcmd 'systemctl reload nginx'" if G.get("nginx") else ""
        sh(f"{acme} --install-cert -d {G['domain']} --ecc "
           f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}{reload_hook}")
    return ACME_CRT, ACME_KEY, False

# ---------------------------------------------------------------------------- nginx 前置
def ensure_nginx():
    if not have("nginx"):
        sh("apt-get update -y", check=False)
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y nginx", check=False)
    os.makedirs(WEBROOT, exist_ok=True)
    if not os.path.exists(WEBROOT + "/index.html"):     # 伪装站首页
        open(WEBROOT + "/index.html", "w").write(
            "<!doctype html><html><head><meta charset='utf-8'><title>Welcome</title></head>"
            "<body><h1>It works!</h1><p>The server is running.</p></body></html>\n")

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

def write_nginx_conf():
    """签好证书、收集完 ws 家族后，写完整 conf：80 跳转 + 443 伪装站 + ws 按 path 反代。"""
    locs = ""
    for w in NGINX_WS:
        locs += (f"  location = {w['path']} {{\n"
                 f"    proxy_pass http://127.0.0.1:{w['port']};\n"
                 f"    proxy_http_version 1.1;\n"
                 f"    proxy_set_header Upgrade $http_upgrade;\n"
                 f"    proxy_set_header Connection \"upgrade\";\n"
                 f"    proxy_set_header Host $host;\n"
                 f"    proxy_set_header X-Real-IP $remote_addr;\n  }}\n")
    conf = (
        f"server {{\n  listen 80;\n  listen [::]:80;\n  server_name {G['domain']};\n"
        f"  location /.well-known/acme-challenge/ {{ root {WEBROOT}; }}\n"
        f"  location / {{ return 301 https://$host$request_uri; }}\n}}\n"
        f"server {{\n  listen 443 ssl http2;\n  listen [::]:443 ssl http2;\n"
        f"  server_name {G['domain']};\n"
        f"  ssl_certificate {ACME_CRT};\n  ssl_certificate_key {ACME_KEY};\n"
        f"  ssl_protocols TLSv1.2 TLSv1.3;\n"
        f"{locs}"
        f"  location / {{ root {WEBROOT}; index index.html; }}\n}}\n")
    open(NGINX_CONF, "w").write(conf)
    nginx_reload()

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

# ---------------------------------------------------------------------------- 核心安装
def arch_tag():
    return {"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine]

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

def write_service(name, binpath, cfg):
    # 先校验配置，schema 错就当场报出来（避免像之前 anytls 那样静默起不来）
    r = subprocess.run(f"{binpath} check -c {cfg}", shell=True, text=True, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"{name} 配置校验失败（多半是内核版本太旧不认某协议）:\n"
                           f"{(r.stderr or r.stdout).strip()}")
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
    ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"uuid": uid, "flow": "xtls-rprx-vision"}],
          "tls": {"enabled": True, "server_name": G["sni"],
                  "reality": {"enabled": True,
                              "handshake": {"server": G["sni"], "server_port": 443},
                              "private_key": priv, "short_id": [sid]}}}
    lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&flow=xtls-rprx-vision"
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
    for line in sh("iptables -t nat -S PREROUTING", check=False).splitlines():
        if not line.startswith("-A"):
            continue
        if "portHopping" in line or f"--dport {lo}:{hi}" in line:
            sh("iptables -t nat " + line.replace("-A", "-D", 1), check=False)
    sh(f"iptables -t nat -A PREROUTING -p udp --dport {lo}:{hi} "
       f"-m comment --comment {tagc} -j DNAT --to-destination :{target_port}", check=False)
    # 尽量持久化（重启后仍生效）；没有 netfilter-persistent 就装一下
    if not have("netfilter-persistent"):
        sh("DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent", check=False)
    sh("netfilter-persistent save", check=False)

def sb_hysteria2(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "hysteria2", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}],
          "tls": {"enabled": True, "alpn": ["h3"],
                  "certificate_path": crt, "key_path": key}}
    rng = hy2_range()                                    # 用户自定义跳跃范围，默认 30000-31000
    setup_port_hopping(port, rng)                        # 端口跳跃：UDP 段 DNAT 到本端口
    lk = (f"hysteria2://{pw}@{G['host']}:{port}?sni={tls_host()}"
          f"&mport={rng}&insecure={1 if insec else 0}#{tag}")
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

def make_sb_vless(transport):
    def b(port, tag):
        uid = new_uuid(); path = "/" + secrets.token_hex(3)
        if G.get("nginx") and transport in ("ws", "httpupgrade"):
            # nginx 前置：本地明文口，TLS 由 nginx 在 443 终结、按 path 反代进来
            ib = {"type": "vless", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
                  "users": [{"uuid": uid}],
                  "transport": _sb_transport(transport, path, tls_host())}
            NGINX_WS.append({"path": path, "port": port})
            lk = (f"vless://{uid}@{G['host']}:443?encryption=none&security=tls"
                  f"&sni={tls_host()}&type={_LINK_NET[transport]}&host={tls_host()}"
                  f"&path={path}#{tag}")
            return ib, lk
        crt, key, insec = ensure_acme()
        ib = {"type": "vless", "tag": tag, "listen": "::", "listen_port": port,
              "users": [{"uuid": uid}],
              "tls": {"enabled": True, "server_name": tls_host(),
                      "certificate_path": crt, "key_path": key},
              "transport": _sb_transport(transport, path, tls_host())}
        lk = (f"vless://{uid}@{G['host']}:{port}?encryption=none&security=tls"
              f"&sni={tls_host()}&type={_LINK_NET[transport]}&host={tls_host()}"
              f"&path={path}&allowInsecure={1 if insec else 0}#{tag}")
        return ib, lk
    return b

def make_sb_vmess(transport):
    def b(port, tag):
        uid = new_uuid(); path = "/" + secrets.token_hex(3)
        if G.get("nginx") and transport in ("ws", "httpupgrade"):
            ib = {"type": "vmess", "tag": tag, "listen": "127.0.0.1", "listen_port": port,
                  "users": [{"uuid": uid, "alterId": 0}],
                  "transport": _sb_transport(transport, path, tls_host())}
            NGINX_WS.append({"path": path, "port": port})
            lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": "443",
                             "id": uid, "aid": "0", "net": _VMESS_NET[transport],
                             "type": "none", "host": tls_host(), "path": path,
                             "tls": "tls", "sni": tls_host()})
            return ib, lk
        crt, key, insec = ensure_acme()
        ib = {"type": "vmess", "tag": tag, "listen": "::", "listen_port": port,
              "users": [{"uuid": uid, "alterId": 0}],
              "tls": {"enabled": True, "server_name": tls_host(),
                      "certificate_path": crt, "key_path": key},
              "transport": _sb_transport(transport, path, tls_host())}
        lk = vmess_link({"v": "2", "ps": tag, "add": G["host"], "port": str(port),
                         "id": uid, "aid": "0", "net": _VMESS_NET[transport],
                         "type": "none", "host": tls_host(), "path": path,
                         "tls": "tls", "sni": tls_host()})
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
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "vless", "tag": tag,
          "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}],
                       "decryption": "none"},
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

def xr_ss2022(port, tag):
    method = "2022-blake3-aes-128-gcm"; key = ss2022_key(method)
    ib = {"listen": "0.0.0.0", "port": port, "protocol": "shadowsocks", "tag": tag,
          "settings": {"method": method, "password": key, "network": "tcp,udp"}}
    lk = f"ss://{ss_userinfo(method, key)}@{G['host']}:{port}#{tag}"
    return ib, lk

XRAY = {"vless-reality-vision": xr_reality_vision,
        "vless-reality-grpc": xr_reality_grpc,
        "vless-reality-xhttp": xr_reality_xhttp,
        "vless-ws": xr_vless_ws, "vmess-ws": xr_vmess_ws,
        "trojan": xr_trojan, "ss2022": xr_ss2022}

# ============================================================================ 组装
def build(table, names):
    inbounds, links = [], []
    for n in names:
        # 名称 = 用户前缀 + 协议名（默认无前缀，别人部署 US/SG 时自己填 🇺🇸/🇸🇬 等）
        ib, lk = table[n](next_port(), G.get("prefix", "") + n)
        inbounds.append(ib); links.append(lk)
    return inbounds, links

# ============================================================================ 订阅
def _yfmt(v):
    if isinstance(v, dict): return "{" + ", ".join(f"{k}: {_yfmt(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list): return "[" + ", ".join(_yfmt(x) for x in v) + "]"
    return str(v)

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
            if net == "grpc": d["network"] = "grpc"; d["grpc-opts"] = {"grpc-service-name": qs.get("serviceName") or qs.get("path", "")}
            else: d["network"] = "tcp"
        else:
            if insec: d["skip-cert-verify"] = "true"
            if net == "ws": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}}
            elif net == "httpupgrade": d["network"] = "ws"; d["ws-opts"] = {"path": qs.get("path", "/"), "headers": {"Host": qs.get("host", host)}, "v2ray-http-upgrade": "true"}
            elif net == "grpc": d["network"] = "grpc"; d["grpc-opts"] = {"grpc-service-name": qs.get("serviceName") or qs.get("path", "")}
            else: d["network"] = "tcp"
        return d
    if sch in ("hysteria2", "hy2"):
        d = {"name": nm("hy2"), "type": "hysteria2", "server": host, "port": port, "password": P.username, "udp": "true"}
        if qs.get("sni"): d["sni"] = qs["sni"]
        if insec: d["skip-cert-verify"] = "true"
        d["alpn"] = ["h3"]
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
        return d
    if sch == "ss":
        ui = P.username or ""
        dec = ui if ":" in ui else base64.urlsafe_b64decode(ui + "=" * (-len(ui) % 4)).decode()
        method, pw = dec.split(":", 1)
        return {"name": nm("ss"), "type": "ss", "server": host, "port": port, "cipher": method, "password": pw, "udp": "true"}
    return None

def fetch_template():
    req = urllib.request.Request(TEMPLATE_URL, headers={"User-Agent": "xy-installer"})
    return urllib.request.urlopen(req, timeout=15).read().decode()

def sub_url(token):
    host = open(HOST_FILE).read().strip() if os.path.exists(HOST_FILE) else public_ip()
    return f"http://{host}:{SUB_PORT}/{token}.yaml"

def current_token():
    try:
        for f in os.listdir(SUB_DIR):
            if f.endswith(".yaml"):
                return f[:-5]
    except OSError:
        pass
    return None

def serve_sub(token):
    """在 SUB_DIR 放一个 <token>.yaml 软链指向可编辑的 CFG_FILE，并起/重启 http 服务。
       换 token 只改软链名，CFG_FILE（订阅内容）原样不动。"""
    os.makedirs(SUB_DIR, exist_ok=True)
    for f in os.listdir(SUB_DIR):                       # 清旧 token 软链/文件
        if f.endswith(".yaml"):
            os.remove(os.path.join(SUB_DIR, f))
    os.symlink(CFG_FILE, f"{SUB_DIR}/{token}.yaml")     # 软链 → 编辑 CFG_FILE 即时生效
    open(f"{SUB_DIR}/index.html", "w").write("")        # 有 index 就不列目录，token 不外泄
    svc = (f"[Unit]\nAfter=network.target\n[Service]\n"
           f"ExecStart=/usr/bin/python3 -m http.server {SUB_PORT} --directory {SUB_DIR} --bind 0.0.0.0\n"
           f"Restart=on-failure\nRestartSec=3\n[Install]\nWantedBy=multi-user.target\n")
    open("/etc/systemd/system/xy-sub.service", "w").write(svc)
    sh("systemctl daemon-reload")
    sh("systemctl enable xy-sub", check=False)
    sh("systemctl restart xy-sub")

def build_subscription(all_links):
    """节点注入模板 → 写成可编辑配置 CFG_FILE，记住 host，起服务托管，返回订阅 URL。"""
    lines = []
    for u in all_links:
        try:
            d = link_to_proxy(u)
            if d: lines.append("  - {" + ", ".join(f"{k}: {_yfmt(v)}" for k, v in d.items()) + "}")
        except Exception:
            pass
    if not lines:
        return None
    cfg = fetch_template().replace("__XY_PROXIES__", "\n".join(lines))
    os.makedirs(BGP_DIR, exist_ok=True)
    open(CFG_FILE, "w").write(cfg)                      # 可编辑的成品配置
    open(HOST_FILE, "w").write(G["host"])              # 记住 host（域名优先），换 token 不丢
    token = secrets.token_urlsafe(12)
    serve_sub(token)
    return sub_url(token)

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
        if not m or not re.search(r"sing-box|xray", txt):
            continue
        if SB_BIN in txt or XRAY_BIN in txt:            # 本脚本自己的，跳过
            continue
        found.append((f[:-8], m.group(1)))
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
    check_domain_or_die()       # 域名不匹配就此停止——必须在 takeover 卸载别人之前
    takeover_cleanup()          # 有别人装的(mack-a 等)先踢掉再接管
    # 节点地址：有域名用域名，否则用公网 IP（域名需直连 A 记录指向本机）
    G["host"] = G["domain"] or public_ip()
    NGINX_WS.clear()
    if G.get("nginx"):
        if not G["domain"]:
            print("nginx 前置需要域名，已忽略、改用自签+IP。"); G["nginx"] = ""
        else:
            ensure_nginx(); write_nginx_acme_stub()     # 先起 80 供 webroot 签证书
    all_links = []

    if sb_names:
        install_singbox()
        ins, lks = build(SB, sb_names); all_links += lks
        if G.get("nginx") and NGINX_WS:
            write_nginx_conf()                          # 收集完 ws 家族，写 443 伪装站+反代
        cfg = f"{SB_DIR}/config.json"
        json.dump({"log": {"level": "info"}, "inbounds": ins,
                   "outbounds": [{"type": "direct"}]},
                  open(cfg, "w"), indent=2)
        write_service("sing-box", SB_BIN, cfg)

    if xr_names:
        install_xray()
        ins, lks = build(XRAY, xr_names); all_links += lks
        cfg = f"{XRAY_DIR}/config.json"
        json.dump({"log": {"loglevel": "warning"}, "inbounds": ins,
                   "outbounds": [{"protocol": "freedom", "tag": "direct"},
                                 {"protocol": "blackhole", "tag": "block"}]},
                  open(cfg, "w"), indent=2)
        write_service("xray", XRAY_BIN, cfg)

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

    # 生成一条完整订阅链接：节点注入模板 → 起 http 服务托管
    sub = None
    try:
        sub = build_subscription(all_links)
    except Exception as e:
        print("\n订阅生成跳过（不影响节点使用）:", e)
    if sub:
        if out_file:
            open(out_file, "a").write("\n# 订阅链接:\n" + sub + "\n")
        print("\n" + "=" * 60)
        print("一键订阅链接（导入客户端即用，含全部节点+分流规则）:")
        print("=" * 60)
        print(sub)
        print("=" * 60)
        print(f"※ 明文 HTTP + 随机 token，请勿外传；改端口/关闭见 xy-sub.service（端口 {SUB_PORT}）")

    install_shortcut()
    print('\n下次直接输入 \033[1;32mbgpeer\033[0m 即可打开管理面板。')

# ============================================================================ 管理面板 / 快捷命令
def install_shortcut():
    """安装 bgpeer 快捷命令：本地存一份脚本，wrapper 每次尽量拉最新再运行。"""
    try:
        os.makedirs("/etc/bgpeer", exist_ok=True)
        open("/etc/bgpeer/xy-installer.py", "w").write(open(__file__).read())
        wrapper = ("#!/usr/bin/env bash\n"
                   'u="https://raw.githubusercontent.com/bgpeer/nodekit/main/xy-installer.py"\n'
                   'curl -fsSL "$u" -o /etc/bgpeer/xy-installer.py 2>/dev/null || true\n'
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

def show_links():
    links = read_saved_links()
    if not links:
        print("\n还没有节点，请先『1.安装』。"); return
    print("\n" + "=" * 60 + "\n分享链接:\n" + "=" * 60)
    print("\n".join(links))
    tok = current_token()
    if tok:
        print("=" * 60 + "\n订阅链接:\n" + sub_url(tok))

def mihomo_config_menu():
    """mihomo 配置：配置存成可编辑文件，用户改参数；换订阅只换 token、host 不变。"""
    if not os.path.exists(CFG_FILE):
        print("\n还没有订阅配置，请先『1.安装』。"); return
    tok = current_token()
    print("\n" + "=" * 60 + "\nmihomo 配置\n" + "=" * 60)
    print(f"  配置文件: {CFG_FILE}")
    print( "           用 nano/vi 直接改参数，保存后客户端重新拉取即生效（订阅链接不用换）")
    if tok:
        print(f"  当前订阅: {sub_url(tok)}")
    print("-" * 60)
    print("  1. 只换订阅链接 token（配置原样不动，怀疑外泄/被墙时用）")
    print("  2. 用最新模板重建配置（会覆盖你在配置文件里的手动修改，谨慎）")
    print("  0. 返回")
    c = _ask("选择: ").strip()
    if c == "1":
        serve_sub(secrets.token_urlsafe(12))
        print("\n新订阅链接（配置未改动）:\n" + sub_url(current_token()))
    elif c == "2":
        links = read_saved_links()
        if not links:
            print("没有已保存节点，无法重建。"); return
        G["host"] = open(HOST_FILE).read().strip() if os.path.exists(HOST_FILE) else public_ip()
        try:
            print("\n已用最新模板重建配置：\n" + (build_subscription(links) or "(无节点)"))
        except Exception as e:
            print("重建失败:", e)

def update_cores():
    print("\n更新核心:  1. sing-box   2. xray   3. 两个   0. 返回")
    c = _ask("选择: ")
    if c == "0" or not c:
        return
    ensure_deps()
    if c in ("1", "3") and os.path.exists(SB_BIN):
        install_singbox(); sh("systemctl restart sing-box", check=False)
        print("sing-box 现版本:", sh(f"{SB_BIN} version", check=False).splitlines()[0] if sh(f"{SB_BIN} version", check=False) else "?")
    if c in ("2", "3") and os.path.exists(XRAY_BIN):
        install_xray(); sh("systemctl restart xray", check=False)
        print("xray 已更新")
    print("更新完成。")

def uninstall_all():
    print("\n将卸载本脚本安装的：sing-box/xray/订阅服务、配置、证书、端口跳跃规则、bgpeer 命令。")
    if (_ask("确认卸载? [y/N]: ") or "n").lower() not in ("y", "yes"):
        print("已取消。"); return
    for svc in ("sing-box", "xray", "xy-sub"):
        sh(f"systemctl disable --now {svc}", check=False)
        sh(f"rm -f /etc/systemd/system/{svc}.service", check=False)
    sh("systemctl daemon-reload", check=False)
    for line in sh("iptables -t nat -S PREROUTING", check=False).splitlines():
        if line.startswith("-A") and "xy_hy2_portHopping" in line:
            sh("iptables -t nat " + line.replace("-A", "-D", 1), check=False)
    sh("netfilter-persistent save", check=False)
    if os.path.exists(NGINX_CONF):                      # 移除本脚本的 nginx 前置块（不动用户其它站点）
        sh(f"rm -f {NGINX_CONF}", check=False)
        sh("nginx -t && systemctl reload nginx", check=False)
    for p in (SB_BIN, XRAY_BIN, SB_DIR, XRAY_DIR, "/etc/ssl/sb", SUB_DIR,
              "/root/xy-nodes.txt", "/usr/local/bin/bgpeer", "/etc/bgpeer", WEBROOT):
        sh(f"rm -rf {p}", check=False)
    print("已卸载完毕。")

def main_menu():
    while True:
        print("\n" + "=" * 60)
        print("  bgpeer 一键脚本  （sing-box + xray 多协议 / 订阅）")
        print("=" * 60)
        print("  1. 安装")
        print("  2. 节点链接")
        print("  3. mihomo 配置（修改 / 更新订阅）")
        print("  4. 更新核心（sing-box / xray）")
        print("  5. 卸载")
        print("  0. 退出")
        print("-" * 60)
        c = _ask("请选择: ").strip()
        if c == "0" or c == "":
            print("再见。"); return
        if c == "1":   install_flow()
        elif c == "2": show_links()
        elif c == "3": mihomo_config_menu()
        elif c == "4": update_cores()
        elif c == "5": uninstall_all()
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

def _pick(title, options):
    """列出带编号的协议，返回选中的 key 列表；回车/0/all = 全选。"""
    print("\n" + title)
    for i, name in enumerate(options, 1):
        print(f"  {i:>2}. {name}")
    print("   0. 全部")
    raw = _ask("选择(逗号分隔编号, 回车=全部): ")
    if raw == "" or raw == "0" or raw.lower() == "all":
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
    print("=" * 60)
    print("  sing-box + xray 交互安装")
    print("=" * 60)
    print("选择核心:  1. sing-box   2. xray   3. 两个都装")
    core = _ask("输入 [1/2/3] (回车=1): ") or "1"

    sb_names, xr_names = [], []
    if core in ("1", "3"):
        sb_names = _pick("【sing-box 协议】", list(SB))
    if core in ("2", "3"):
        xr_names = _pick("【xray 协议】", list(XRAY))
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
    if not sb_names or "hy2" in sb_names:
        hy2p = _ask("hy2 端口跳跃范围 起-止(回车=30000-31000): ")
    G["domain"], G["email"], G["sni"], G["prefix"], G["hy2_ports"], G["nginx"] = \
        domain, email, sni, prefix, hy2p, nginx

    print("\n" + "-" * 60)
    if sb_names: print("  sing-box:", ", ".join(sb_names))
    if xr_names: print("  xray:    ", ", ".join(xr_names))
    print("  证书:    ", f"acme真证书({domain})" if domain else "自签")
    print("  节点地址:", domain if domain else "公网IP")
    print("  nginx前置:", "是（443伪装站+webroot，ws类走443）" if nginx else "否")
    print("  名称前缀:", prefix or "(无)")
    print("  SNI:     ", sni)
    if not sb_names or "hy2" in sb_names:
        print("  hy2跳跃: ", hy2_range())
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
    ap.add_argument("--hy2-ports", default="", help="hy2 端口跳跃范围 起-止，默认 30000-31000")
    ap.add_argument("--nginx", action="store_true",
                    help="用 nginx 前置(443伪装站+webroot证书, ws类藏443)，需域名")
    ap.add_argument("--yes", action="store_true",
                    help="检测到别人装的节点(mack-a 等)直接卸载接管，不再询问")
    a = ap.parse_args()

    G["domain"], G["email"], G["sni"], G["prefix"], G["hy2_ports"], G["nginx"], G["force"] = \
        a.domain, a.email, a.sni, a.prefix, a.hy2_ports, ("1" if a.nginx else ""), a.yes
    sb = list(SB) if a.sb == "all" else [x for x in a.sb.split(",") if x]
    xr = list(XRAY) if a.xray == "all" else [x for x in a.xray.split(",") if x]
    if not sb and not xr:
        ap.error("至少用 --sb 或 --xray 指定要装的协议")
    run(sb, xr)

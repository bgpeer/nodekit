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
import os, json, base64, secrets, uuid, argparse, subprocess, urllib.request, shutil, socket

SB_VER   = "1.11.15"
XRAY_VER = "25.3.6"
SB_BIN, XRAY_BIN = "/usr/local/bin/sing-box", "/usr/local/bin/xray"
SB_DIR,  XRAY_DIR = "/etc/sing-box", "/usr/local/etc/xray"
CERT, KEY = "/etc/ssl/sb/self.crt", "/etc/ssl/sb/self.key"     # 自签
ACME_CRT, ACME_KEY = "/etc/ssl/sb/acme.crt", "/etc/ssl/sb/acme.key"  # acme 签发

# 全局状态：域名/邮箱/SNI 由 CLI 注入；端口自增分配
G = {"host": "", "domain": "", "email": "", "sni": "www.microsoft.com", "_port": 20000}

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

def ensure_acme():
    """给了 --domain 就用 acme.sh standalone 签真证书；否则回落自签。"""
    if not G["domain"]:
        ensure_self_signed()
        return CERT, KEY, True                      # (crt, key, insecure)
    if not os.path.exists(ACME_CRT):
        # standalone 用 socat 起临时 HTTP 服务占 80 端口做验证，缺 socat 必挂
        if not have("socat"):
            ensure_deps()
        acme = os.path.expanduser("~/.acme.sh/acme.sh")
        if not os.path.exists(acme):
            sh("curl -s https://get.acme.sh | sh -s email=" + (G["email"] or "a@a.com"))
        if not os.path.exists(acme):
            raise RuntimeError("acme.sh 安装失败，检查网络/curl 是否可访问 get.acme.sh")
        if not port_free(80):
            raise RuntimeError(
                "80 端口被占用，acme.sh --standalone 无法验证。"
                "先停掉占用 80 的服务(nginx/caddy 等)，或改用自签(回车跳过域名)。")
        sh(f"{acme} --register-account -m {G['email'] or 'a@a.com'} "
           f"--server letsencrypt", check=False)
        sh(f"{acme} --set-default-ca --server letsencrypt", check=False)
        sh(f"{acme} --issue -d {G['domain']} --standalone --keylength ec-256")
        os.makedirs(os.path.dirname(ACME_CRT), exist_ok=True)
        sh(f"{acme} --install-cert -d {G['domain']} --ecc "
           f"--fullchain-file {ACME_CRT} --key-file {ACME_KEY}")
    return ACME_CRT, ACME_KEY, False

def tls_host():                                     # ws/trojan 的 SNI/Host
    return G["domain"] or G["sni"]

# ---------------------------------------------------------------------------- 核心安装
def arch_tag():
    return {"x86_64": "amd64", "aarch64": "arm64"}[os.uname().machine]

def install_singbox():
    if os.path.exists(SB_BIN):
        return
    a = arch_tag()
    url = (f"https://github.com/SagerNet/sing-box/releases/download/"
           f"v{SB_VER}/sing-box-{SB_VER}-linux-{a}.tar.gz")
    sh(f"curl -Lo /tmp/sb.tgz {url} && tar -xzf /tmp/sb.tgz -C /tmp")
    sh(f"install -m755 /tmp/sing-box-{SB_VER}-linux-{a}/sing-box {SB_BIN}")
    os.makedirs(SB_DIR, exist_ok=True)

def install_xray():
    if os.path.exists(XRAY_BIN):
        return
    a = arch_tag()
    zmap = {"amd64": "64", "arm64": "arm64-v8a"}
    url = (f"https://github.com/XTLS/Xray-core/releases/download/"
           f"v{XRAY_VER}/Xray-linux-{zmap[a]}.zip")
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
    unit = (f"[Unit]\nAfter=network.target nss-lookup.target\n"
            f"[Service]\nExecStart={binpath} run -c {cfg}\n"
            f"Restart=on-failure\nRestartSec=3\nLimitNOFILE=1000000\n"
            f"[Install]\nWantedBy=multi-user.target\n")
    open(f"/etc/systemd/system/{name}.service", "w").write(unit)
    sh("systemctl daemon-reload")
    sh(f"systemctl enable --now {name}")

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

def sb_hysteria2(port, tag):
    pw = new_pw(); crt, key, insec = ensure_acme()
    ib = {"type": "hysteria2", "tag": tag, "listen": "::", "listen_port": port,
          "users": [{"password": pw}],
          "tls": {"enabled": True, "alpn": ["h3"],
                  "certificate_path": crt, "key_path": key}}
    lk = (f"hysteria2://{pw}@{G['host']}:{port}?sni={tls_host()}"
          f"&insecure={1 if insec else 0}#{tag}")
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

SB = {"reality-vision": sb_reality_vision, "reality-grpc": sb_reality_grpc,
      "hy2": sb_hysteria2, "tuic": sb_tuic, "anytls": sb_anytls, "ss2022": sb_ss2022,
      "vless-ws": make_sb_vless("ws"), "vless-h2": make_sb_vless("h2"),
      "vless-httpupgrade": make_sb_vless("httpupgrade"),
      "vmess-ws": make_sb_vmess("ws"), "vmess-h2": make_sb_vmess("h2"),
      "vmess-httpupgrade": make_sb_vmess("httpupgrade"),
      "trojan": sb_trojan, "socks5": sb_socks5, "naive": sb_naive,
      "shadowtls": sb_shadowtls}

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
        ib, lk = table[n](next_port(), f"{n}-{G['host']}")
        inbounds.append(ib); links.append(lk)
    return inbounds, links

def run(sb_names, xr_names):
    ensure_deps()               # 先补齐 curl/socat/unzip/openssl 等，避免中途才炸
    G["host"] = public_ip()
    all_links = []

    if sb_names:
        install_singbox()
        ins, lks = build(SB, sb_names); all_links += lks
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

    print("\n" + "=" * 60)
    print("分享链接（直接喂给 Mihomo-fx 的 LINKS 解析）:")
    print("=" * 60)
    print("\n".join(all_links))

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

def menu():
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
    sni = _ask("reality 借用目标站 SNI (回车=www.microsoft.com): ") or "www.microsoft.com"
    G["domain"], G["email"], G["sni"] = domain, email, sni

    print("\n" + "-" * 60)
    if sb_names: print("  sing-box:", ", ".join(sb_names))
    if xr_names: print("  xray:    ", ", ".join(xr_names))
    print("  证书:    ", f"acme真证书({domain})" if domain else "自签")
    print("  SNI:     ", sni)
    print("-" * 60)
    if (_ask("确认开始? [Y/n]: ") or "y").lower() in ("n", "no"):
        print("已取消。"); return
    run(sb_names, xr_names)

# ============================================================================ CLI
if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1:          # 不带参数 → 交互菜单
        menu()
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
    ap.add_argument("--sni", default="www.microsoft.com", help="reality 借用的目标站")
    a = ap.parse_args()

    G["domain"], G["email"], G["sni"] = a.domain, a.email, a.sni
    sb = list(SB) if a.sb == "all" else [x for x in a.sb.split(",") if x]
    xr = list(XRAY) if a.xray == "all" else [x for x in a.xray.split(",") if x]
    if not sb and not xr:
        ap.error("至少用 --sb 或 --xray 指定要装的协议")
    run(sb, xr)

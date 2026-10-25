#!/usr/bin/env python3
"""节点画像端到端（net-optimize 4.5.18）：在独立的 mount + net 命名空间里跑【真脚本、真路径、真内核参数】，
  不碰这台机器本身（/etc 下要用的目录都临时盖一层 tmpfs，网络是空的新命名空间）。
  老装机（4.5.17 跑过优化：保留端口写死 15000-45000、sysctl 文件 0644）第一次进面板 → 画像写好 → 叫网络优化
  --apply-profile → 内核里池子外的端口全补上；再进不叫；删 CDN / 关跳跃放回去；手动指定的不动；老版网络优化绝不叫；
  最后让内核 bind(0) 两百次，保留的端口一个都分不到。
  建不了命名空间（没 root / 没权限）就跳过。
"""
import os, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = r'''
REPO="$1"; T="$2"
ok=0; bad=0
chk() { if eval "$2"; then echo "  ✔ $1"; ok=$((ok+1)); else echo "  ✖ $1"; bad=$((bad+1)); fi; }
for d in /etc/bgpeer /etc/net-optimize /etc/sysctl.d /etc/sing-box /usr/local/etc/xray /usr/local/sbin /etc/nginx; do
  mkdir -p "$d" && mount -t tmpfs tmpfs "$d" || { echo "  ✖ 盖不上 tmpfs：$d"; exit 1; }
done
mkdir -p /etc/nginx/conf.d
ip link set lo up

cat > /etc/sysctl.d/99-net-optimize.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.ip_local_reserved_ports = 15000-45000
net.ipv4.tcp_fastopen = 3
EOF
chmod 644 /etc/sysctl.d/99-net-optimize.conf
printf 'ENABLE_MSS_CLAMP=1\nADAPTIVE_DAEMON=0\n' > /etc/net-optimize/config
sysctl -qw net.ipv4.ip_local_port_range="1024 65535"
sysctl -qw net.ipv4.ip_local_reserved_ports=15000-45000
cp "$REPO/net-optimize.py" /usr/local/sbin/net-optimize.py

cat > /etc/sing-box/config.json <<'EOF'
{"inbounds": [{"type": "trojan", "listen": "::", "listen_port": 18047},
              {"type": "hysteria2", "listen": "::", "listen_port": 31000},
              {"type": "vless", "listen": "127.0.0.1", "listen_port": 29999}]}
EOF
cat > /usr/local/etc/xray/config.json <<'EOF'
{"inbounds": [{"listen": "0.0.0.0", "port": 36844, "protocol": "trojan"},
              {"listen": "0.0.0.0", "port": 41234, "protocol": "vless", "streamSettings": {"network": "xhttp"}}]}
EOF
echo '[{"cf_port": 2053, "svc": "xy-cdn-1"}]' > /etc/bgpeer/cdn.json
echo 52001 > /etc/bgpeer/sub.port
echo '{"hy2_ports": "50000-51000"}' > /etc/bgpeer/state.json
printf 'server {\n  listen 80;\n  listen 443 ssl;\n}\nserver {\n  listen 127.0.0.1:8443 ssl;\n}\nserver {\n  listen 127.0.0.1:8444;\n}\n' > /etc/nginx/conf.d/bgpeer.conf
printf 'server {\n  listen 127.0.0.1:8445;\n}\n' > /etc/nginx/conf.d/bgpeer-fallback.conf

python3 -c '
import socket, time
ss = []
for p in (5244, 8096):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", p)); s.listen(); ss.append(s)
time.sleep(60)' >/dev/null 2>&1 &
LP=$!
trap 'kill $LP 2>/dev/null' EXIT
sleep 0.5

sync() { python3 -c "import importlib.util
spec = importlib.util.spec_from_file_location('xy', '$REPO/xy-installer.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.netprofile_sync())"; }
RES() { cat /proc/sys/net/ipv4/ip_local_reserved_ports; }

echo "── 1. 老装机第一次进面板"
S=$(sync); R=$(RES); echo "  叫了网络优化：$S　内核保留端口：$R"
chk "池子外的全补上（CDN、OpenList、Emby、nginx 本地口、跳跃段、订阅），池子整段还在" \
    '[ "$S" = True ] && [ "$R" = "2053,5244,8096,8443-8445,15000-45000,50000-51000,52001" ]'
chk "sysctl 文件那一行跟着改（重启后照样），别的行不动" \
    'grep -qx "net.ipv4.ip_local_reserved_ports = $R" /etc/sysctl.d/99-net-optimize.conf && grep -qx "net.ipv4.tcp_fastopen = 3" /etc/sysctl.d/99-net-optimize.conf && [ $(wc -l < /etc/sysctl.d/99-net-optimize.conf) -eq 4 ]'
chk "sysctl 文件、画像都是 0600" '[ "$(stat -c %a /etc/sysctl.d/99-net-optimize.conf)" = 600 ] && [ "$(stat -c %a /etc/bgpeer/netprofile.json)" = 600 ]'
SIG=$(sha256sum /etc/bgpeer/netprofile.json | cut -c1-12)
chk "config 记下画像签名，原有项不丢" 'grep -qx "PROFILE_SIG=$SIG" /etc/net-optimize/config && grep -qx "ADAPTIVE_DAEMON=0" /etc/net-optimize/config'

echo "── 2. 再进一次面板"
chk "什么都没变：不叫网络优化" '[ "$(sync)" = False ]'

echo "── 3. 删掉 CDN、hy2 关跳跃"
echo '[]' > /etc/bgpeer/cdn.json; echo '{"hy2_ports": "n"}' > /etc/bgpeer/state.json
S=$(sync); R=$(RES); echo "  内核保留端口：$R"
chk "2053 和跳跃段放回临时端口池" '[ "$S" = True ] && [ "$R" = "5244,8096,8443-8445,15000-45000,52001" ]'

echo "── 4. 手动指定过保留端口"
echo RESERVED_PORTS=20000-21000 >> /etc/net-optimize/config
sysctl -qw net.ipv4.ip_local_reserved_ports=20000-21000
echo '[{"cf_port": 2083}]' > /etc/bgpeer/cdn.json
chk "不叫、原样不动" '[ "$(sync)" = False ] && [ "$(RES)" = 20000-21000 ]'
sed -i '/^RESERVED_PORTS=/d' /etc/net-optimize/config

echo "── 5. 机器上只有老版网络优化（不认 --apply-profile）"
printf 'VERSION = "4.5.17"\nprint("full optimize!")\n' > /usr/local/sbin/net-optimize.py
echo '[{"cf_port": 2096}]' > /etc/bgpeer/cdn.json
B=$(RES)
chk "绝不叫它（老版会忽略不认的参数、转头跑整套优化），内核参数不动" '[ "$(sync)" = False ] && [ "$(RES)" = "$B" ]'
cp "$REPO/net-optimize.py" /usr/local/sbin/net-optimize.py

echo "── 6. --check 那一行"
S=$(sync)
python3 /usr/local/sbin/net-optimize.py --check > "$T/check.txt" 2>&1
grep '保留端口' "$T/check.txt" | sed 's/^/  /'
chk "已对上，屏上只有个数、不印端口" 'grep -q "保留端口：已跟节点对上" "$T/check.txt" && ! grep "保留端口" "$T/check.txt" | grep -qE "2096|5244|8096|52001"'

echo "── 7. 内核真照它分临时端口"
sysctl -qw net.ipv4.ip_local_port_range="40000 40009"
sysctl -qw net.ipv4.ip_local_reserved_ports=40000-40005
chk "bind(0) 两百次，保留的 40000-40005 一个都分不到" 'python3 -c "
import socket
got = set()
for _ in range(200):
    s = socket.socket(); s.bind((\"127.0.0.1\", 0)); got.add(s.getsockname()[1]); s.close()
assert got and all(40006 <= p <= 40009 for p in got), got"'
echo "结果：$ok 过 / $bad 不过"
[ $bad -eq 0 ]
'''

if os.geteuid() != 0:
    print("  跳过：要 root 才能建命名空间"); sys.exit(0)
probe = subprocess.run(["unshare", "--mount", "--net", "--propagation", "private", "true"], capture_output=True)
if probe.returncode != 0:
    print("  跳过：这台机器建不了 mount / net 命名空间"); sys.exit(0)
t = tempfile.mkdtemp()
sh = os.path.join(t, "e2e.sh")
open(sh, "w").write(SCRIPT)
r = subprocess.run(["unshare", "--mount", "--net", "--propagation", "private", "bash", sh, ROOT, t],
                   capture_output=True, text=True, timeout=240)
print(r.stdout, end="")
if r.returncode != 0:
    print(r.stderr[-2000:])
print("ALL PASS" if r.returncode == 0 else "✖ FAIL")
sys.exit(r.returncode)

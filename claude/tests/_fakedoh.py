"""测试用的假 AdGuard（test_privdns*.py 共用，本身不是测试）：本地 TLS + DoH（自签 CA），按 ANS 表回答；
   reload(yaml 路径) 照 user_rules 里我们写的 $dnsrewrite 重新装表——跟真 AdGuard「重启后照规则答」一样。
   ANS：{(名字, 1=A / 28=AAAA): (rcode, [IP])}；表里有这个名字但没有这一类 → 空答案（NOERROR）；没有这个名字 → NXDOMAIN。
   STATUS[0] 改成 403 之类 → 不答 DNS、直接回这个 HTTP 状态（模拟白名单挡住）。SEEN 记下每个请求的头。"""
import os, re, ssl, socket, struct, threading, subprocess
try:
    import yaml
except ImportError:
    yaml = None


class FakeAGH:
    def __init__(self, tmp, host):
        self.host, self.ANS, self.STATUS, self.SEEN = host, {}, [200], []
        d = os.path.join(tmp, "pki-" + host)
        os.makedirs(d, exist_ok=True)

        def ossl(*a):
            subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)
        ec = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
        ossl("req", "-x509", *ec, "-keyout", "ca.key", "-out", "ca.crt", "-subj", "/CN=test-ca", "-days", "2")
        ossl("req", *ec, "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=" + host)
        open(os.path.join(d, "ext"), "w").write("subjectAltName=DNS:" + host + "\n")
        ossl("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
             "-out", "srv.crt", "-days", "2", "-extfile", "ext")
        self.sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.sctx.load_cert_chain(os.path.join(d, "srv.crt"), os.path.join(d, "srv.key"))
        self.client_ctx = ssl.create_default_context(cafile=os.path.join(d, "ca.crt"))
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(16)
        self.port = self.lsock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _reply(self, q):
        i, labels = 12, []
        while q[i]:
            labels.append(q[i + 1:i + 1 + q[i]].decode())
            i += q[i] + 1
        name, qt = ".".join(labels), struct.unpack(">H", q[i + 1:i + 3])[0]
        rc, ips = self.ANS.get((name, qt), (0, []) if any(k[0] == name for k in self.ANS) else (3, []))
        ans = b""
        for ip in ips:
            rd = socket.inet_pton(socket.AF_INET if qt == 1 else socket.AF_INET6, ip)
            ans += struct.pack(">HHHIH", 0xC00C, qt, 1, 60, len(rd)) + rd     # 名字用压缩指针指回问题
        return struct.pack(">HHHHHH", 0, 0x8180 | rc, 1, len(ips), 0, 0) + q[12:i + 5] + ans

    def _serve(self):
        while True:
            try:
                c, _ = self.lsock.accept()
            except OSError:
                return
            try:
                t = self.sctx.wrap_socket(c, server_side=True)
                data = b""
                while b"\r\n\r\n" not in data:
                    d = t.recv(4096)
                    if not d:
                        break
                    data += d
                head, _, body = data.partition(b"\r\n\r\n")
                n = int(re.search(rb"(?i)content-length: (\d+)", head).group(1))
                while len(body) < n:
                    body += t.recv(4096)
                self.SEEN.append(head.decode(errors="replace"))
                if self.STATUS[0] != 200:
                    t.sendall(f"HTTP/1.0 {self.STATUS[0]} No\r\n\r\n".encode())
                else:
                    msg = self._reply(body)
                    t.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/dns-message\r\n"
                              b"Content-Length: %d\r\n\r\n" % len(msg) + msg)
                t.close()
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass

    def reload(self, yaml_path):
        """假 AdGuard「重启」：重新读 yaml 里 user_rules 的 $dnsrewrite 规则。"""
        self.ANS.clear()
        txt = open(yaml_path).read()
        rules = (yaml.safe_load(txt).get("user_rules") or []) if yaml else re.findall(r"- '([^']*)'", txt)
        for r in rules:
            m = re.match(r"^\|\|(.+)\^\$dnsrewrite=NOERROR;(A|AAAA);(.+)$", str(r))
            if m:
                self.ANS.setdefault((m.group(1), 1 if m.group(2) == "A" else 28), (0, []))[1].append(m.group(3))

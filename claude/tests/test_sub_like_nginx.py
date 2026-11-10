#!/usr/bin/env python3
"""订阅小服务的回包照 nginx 的样子（仓库主人 11/10：「那就做吧，单独提一个 PR」）。
  它对外报 Server: nginx，可回包还是 Python http.server 的：状态行 HTTP/1.0（真 nginx 一律 HTTP/1.1）、
  Content-type 小写 t、没有 Connection / ETag / Accept-Ranges、别的方法回 501、乱发一行不回头部——自称 nginx 却处处不像。
  这里的期望值都是拿真 nginx（静态站、server_tokens off）同样的请求抓下来的；机器上装了 nginx 的话，最后一节
  当场再起一个真的、同一批请求逐字节对一遍（没装就跳过）。
  长连接是这次改动里最容易出事的：一条连接连发几个请求、带正文的请求之后接着发、服务端断开空闲连接，都在这测。
"""
import os, re, ssl, sys, time, shutil, socket, tempfile, subprocess, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

tmp = tempfile.mkdtemp()
os.chmod(tmp, 0o755)
d = f"{tmp}/sub"; os.makedirs(d)
for name, body in (("index.html", ""), ("TokA.yaml", "x: 1\n"), ("TokA.json", '{"a":1}\n'), ("TokA.conf", "[General]\n"),
                   ("TokA.txt", "vless://x\n"), ("Lt9.links", "vless://y\n")):
    open(f"{d}/{name}", "w").write(body)
srv = f"{tmp}/srv.py"; open(srv, "w").write(m._SUB_SERVER_PY)
subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                "-keyout", f"{tmp}/k.pem", "-out", f"{tmp}/c.pem", "-subj", "/CN=sub.example", "-days", "2",
                "-addext", "subjectAltName=DNS:sub.example"], check=True, capture_output=True)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def wait_port(port):
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 1).close(); return True
        except OSError:
            time.sleep(0.1)
    return False
PORT, TLSPORT = free_port(), free_port()
procs = [subprocess.Popen([sys.executable, srv, str(PORT), d, f"{tmp}/gh.token"]),
         subprocess.Popen([sys.executable, srv, str(TLSPORT), d, f"{tmp}/gh.token", f"{tmp}/c.pem", f"{tmp}/k.pem"])]

def raw(port, data, wait=1.5):
    """发一段原始字节，把对面回的全收下来（对面关了 / 这么久没新东西就算完）。"""
    s = socket.create_connection(("127.0.0.1", port), 3); s.settimeout(wait)
    s.sendall(data); out = b""
    try:
        while True:
            got = s.recv(65536)
            if not got:
                break
            out += got
    except OSError:
        pass
    s.close()
    return out
def req(port, text):
    return raw(port, text.replace("\n", "\r\n").encode())
def parts(resp):
    head, _, body = resp.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return lines[0], [l.split(":", 1)[0] for l in lines[1:]], dict(l.split(": ", 1) for l in lines[1:] if ": " in l), body
def page(code, reason):
    t = f"{code} {reason}"
    return (f"<html>\r\n<head><title>{t}</title></head>\r\n<body>\r\n<center><h1>{t}</h1></center>\r\n"
            f"<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n").encode()
G = "GET %s HTTP/1.1\nHost: h\nConnection: close\n\n"
FILE_HDRS = ["Server", "Date", "Content-Type", "Content-Length", "Last-Modified", "Connection", "ETag", "Accept-Ranges"]
ERR_HDRS = ["Server", "Date", "Content-Type", "Content-Length", "Connection"]

try:
    assert wait_port(PORT) and wait_port(TLSPORT), "小服务没起来"
    print("一、订阅文件：状态行、头的名字 / 顺序 / 大小写")
    st, names, h, body = parts(req(PORT, G % "/TokA.yaml"))
    check("状态行 HTTP/1.1 200 OK（以前是 HTTP/1.0）", st == "HTTP/1.1 200 OK")
    check("头的顺序跟 nginx 一样：Server / Date / Content-Type / Content-Length / Last-Modified / Connection / ETag / Accept-Ranges",
          names == FILE_HDRS)
    mt = int(os.stat(f"{d}/TokA.yaml").st_mtime)
    check("Server: nginx（不带版本号）、ETag 是 nginx 的算法（修改时间-大小，十六进制）、Accept-Ranges: bytes",
          h["Server"] == "nginx" and h["ETag"] == '"%x-%x"' % (mt, 5) and h["Accept-Ranges"] == "bytes" and body == b"x: 1\n")
    check("Date / Last-Modified 是 GMT 的标准写法", all(re.fullmatch(r"\w{3}, \d\d \w{3} \d{4} \d\d:\d\d:\d\d GMT", h[k])
                                                for k in ("Date", "Last-Modified")))
    types = {n: parts(req(PORT, G % ("/" + n)))[2].get("Content-Type") for n in ("TokA.yaml", "TokA.json", "TokA.conf", "TokA.txt", "Lt9.links", "")}
    check("类型照 nginx 的 mime.types：json / txt / html 认得，yaml / conf / links 是 octet-stream",
          types == {"TokA.yaml": "application/octet-stream", "TokA.json": "application/json", "TokA.conf": "application/octet-stream",
                    "TokA.txt": "text/plain", "Lt9.links": "application/octet-stream", "": "text/html"})
    st, names, h, body = parts(req(PORT, G % "/"))
    check("根目录：200、空的（不列目录，文件名就是 token）", st == "HTTP/1.1 200 OK" and body == b"" and h["Content-Length"] == "0")
    check("新地址 /<token>/<名字>.<ext>、带 ?参数、老地址都照旧能拉",
          all(parts(req(PORT, G % p))[3] == b"x: 1\n" for p in ("/TokA/VPS.yaml", "/TokA/VPS.yaml?flag=meta", "/TokA.yaml", "//TokA.yaml", "/TokA%2Eyaml")))
    st, names, h, body = parts(req(PORT, "HEAD /TokA.yaml HTTP/1.1\nHost: h\nConnection: close\n\n"))
    check("HEAD：头一样、没有正文", st == "HTTP/1.1 200 OK" and names == FILE_HDRS and body == b"" and h["Content-Length"] == "5")

    print("二、错误页：跟 nginx 的逐字节一样")
    for label, text, code, reason in (
            ("不存在的地址", G % "/nope", 404, "Not Found"),
            ("token 错", G % "/TokX/VPS.yaml", 404, "Not Found"),
            ("没有 token 文件时的中转地址", G % "/x/gh/raw.githubusercontent.com/a/b/main/c", 404, "Not Found"),
            ("POST 到文件（以前回 501 Unsupported method）", "POST /TokA.yaml HTTP/1.1\nHost: h\nContent-Length: 0\nConnection: close\n\n", 405, "Not Allowed"),
            ("PUT / DELETE / 不认识的方法", "FOO / HTTP/1.1\nHost: h\nConnection: close\n\n", 405, "Not Allowed"),
            ("乱发一行（以前只回正文、没有头）", "hello\n\n", 400, "Bad Request"),
            ("HTTP/1.1 不带 Host", "GET /TokA.yaml HTTP/1.1\nConnection: close\n\n", 400, "Bad Request"),
            ("爬出根目录的 ../", G % "/../../etc/passwd", 400, "Bad Request"),
            ("%2e%2e 写法的 ../", G % "/%2e%2e/%2e%2e/etc/passwd", 400, "Bad Request"),
            ("坏的 %xx / %00", G % "/a%00b", 400, "Bad Request"),
            ("小写方法", "get / HTTP/1.1\nHost: h\n\n", 400, "Bad Request"),
            ("HTTP/2.0 写在请求行里", "GET / HTTP/2.0\nHost: h\n\n", 505, "HTTP Version Not Supported"),
            ("请求行超过 8k", G % ("/" + "a" * 9000), 414, "Request-URI Too Large")):
        st, names, h, body = parts(req(PORT, text))
        check(f"{label} → {code} {reason}", st == f"HTTP/1.1 {code} {reason}" and names == ERR_HDRS and body == page(code, reason)
              and h["Content-Type"] == "text/html" and h["Content-Length"] == str(len(body)))
    st, names, h, body = parts(req(PORT, "GET / HTTP/1.1\nHost: h\nX: " + "a" * 9000 + "\nConnection: close\n\n"))
    check("头太大 → 400 Request Header Or Cookie Too Large 那一张", st == "HTTP/1.1 400 Bad Request"
          and b"<center>Request Header Or Cookie Too Large</center>" in body and len(body) == 226)
    check("错误页里没有 Python / http.server 的痕迹", all(w not in req(PORT, G % "/nope") + req(PORT, "hello\n\n")
                                                   for w in (b"Python", b"SimpleHTTP", b"BaseHTTP", b"File not found", b"Error response")))
    check("HTTP/0.9（不带版本的请求行）：只回正文、没有头", req(PORT, "GET /TokA.yaml\n") == b"x: 1\n" and req(PORT, "GET /nope\n") == page(404, "Not Found"))

    print("三、长连接")
    r = req(PORT, "GET /TokA.yaml HTTP/1.1\nHost: h\n\n" "GET /nope HTTP/1.1\nHost: h\n\n" "GET /TokA.json HTTP/1.1\nHost: h\nConnection: close\n\n")
    check("一条连接连发三个请求：三个都回、顺序对", r.count(b"HTTP/1.1 ") == 3 and r.index(b"x: 1\n") < r.index(b"404 Not Found") < r.index(b'{"a":1}'))
    check("前两个 Connection: keep-alive，最后一个照请求说的 close", re.findall(rb"Connection: ([\w-]+)", r) == [b"keep-alive", b"keep-alive", b"close"])
    r = req(PORT, "POST /TokA.yaml HTTP/1.1\nHost: h\nContent-Length: 11\n\nGET /zzzzzz" "GET /TokA.yaml HTTP/1.1\nHost: h\nConnection: close\n\n")
    check("带正文的请求：正文读掉丢弃，不会被当成下一个请求", r.count(b"HTTP/1.1 ") == 2 and b"405 Not Allowed" in r and r.endswith(b"x: 1\n"))
    check("HTTP/1.0 的请求：默认回完就断；带 Connection: keep-alive 才留（状态行照样是 HTTP/1.1）",
          parts(req(PORT, "GET /TokA.yaml HTTP/1.0\n\n"))[2]["Connection"] == "close"
          and parts(req(PORT, "GET /TokA.yaml HTTP/1.0\nConnection: keep-alive\n\n", ))[2]["Connection"] == "keep-alive"
          and req(PORT, "GET /TokA.yaml HTTP/1.0\n\n").startswith(b"HTTP/1.1 200 OK"))
    check("400 这类回完就断，不留长连接", parts(req(PORT, "GET /a%zz HTTP/1.1\nHost: h\n\n"))[2]["Connection"] == "close")
    t0 = time.time(); r = raw(PORT, b"GET /TokA.yaml HTTP/1.1\r\nHost: h\r\nConnection: close\r\n\r\n", wait=5)
    check("请求说 close：回完马上断（客户端不用干等）", r.endswith(b"x: 1\n") and time.time() - t0 < 1.5)
    src = m._SUB_SERVER_PY
    check("空闲的长连接有时限（65 秒，同 nginx.conf 默认），连着的太多就不再留长连接", "KEEPALIVE = 65" in src and "MAX_KEEP" in src
          and "timeout = KEEPALIVE" in src)

    print("四、没改过回 304；断点续传")
    lm, etag = h and parts(req(PORT, G % "/TokA.yaml"))[2]["Last-Modified"], '"%x-%x"' % (mt, 5)
    def cond(extra):
        return parts(req(PORT, f"GET /TokA.yaml HTTP/1.1\nHost: h\n{extra}\nConnection: close\n\n"))
    st, names, h, body = cond(f"If-None-Match: {etag}")
    check("If-None-Match 对上 → 304，头是 Server / Date / Last-Modified / Connection / ETag，没有正文",
          st == "HTTP/1.1 304 Not Modified" and names == ["Server", "Date", "Last-Modified", "Connection", "ETag"] and body == b"")
    check("If-Modified-Since 一秒不差 → 304；早于 / 晚于修改时间 → 200（nginx 默认要精确相等）",
          cond(f"If-Modified-Since: {lm}")[0].endswith("304 Not Modified")
          and cond("If-Modified-Since: Sat, 01 Jan 2000 00:00:00 GMT")[0].endswith("200 OK")
          and cond("If-Modified-Since: Fri, 01 Jan 2100 00:00:00 GMT")[0].endswith("200 OK"))
    check("If-None-Match 对不上 → 200；W/ 弱标签、* 也算对上", cond('If-None-Match: "zz"')[0].endswith("200 OK")
          and cond(f"If-None-Match: W/{etag}")[0].endswith("304 Not Modified") and cond("If-None-Match: *")[0].endswith("304 Not Modified"))
    st, names, h, body = cond("Range: bytes=1-2")
    check("Range: bytes=1-2 → 206，Content-Range: bytes 1-2/5，没有 Accept-Ranges", st == "HTTP/1.1 206 Partial Content" and body == b": "
          and names == ["Server", "Date", "Content-Type", "Content-Length", "Last-Modified", "Connection", "ETag", "Content-Range"]
          and h["Content-Range"] == "bytes 1-2/5")
    check("bytes=-2 / 3- / 2-99 都按 nginx 的口径截", [cond(f"Range: bytes={r}")[2].get("Content-Range") for r in ("-2", "3-", "2-99")]
          == ["bytes 3-4/5", "bytes 3-4/5", "bytes 2-4/5"])
    st, names, h, body = cond("Range: bytes=99-")
    check("够不着的 Range → 416 + Content-Range: bytes */5", st == "HTTP/1.1 416 Requested Range Not Satisfiable"
          and names == ERR_HDRS + ["Content-Range"] and h["Content-Range"] == "bytes */5" and len(body) == 190
          and cond("Range: bytes=3-1")[0].endswith("416 Requested Range Not Satisfiable"))
    st, names, h, body = cond("Range: bytes=0-0,2-3")
    check("多段 Range → multipart/byteranges，分段格式同 nginx", st == "HTTP/1.1 206 Partial Content"
          and re.fullmatch(r"multipart/byteranges; boundary=\d{20}", h["Content-Type"]) and len(body) == 223
          and b"Content-Range: bytes 0-0/5\r\n\r\nx\r\n" in body and b"Content-Range: bytes 2-3/5\r\n\r\n 1\r\n" in body)
    check("不是 bytes= 的 Range 不理会 → 200", cond("Range: lines=1-2")[0].endswith("200 OK"))

    print("五、HTTPS 口")
    ctx = ssl.create_default_context(cafile=f"{tmp}/c.pem")
    def tls(alpn, text):
        c = ssl.create_default_context(cafile=f"{tmp}/c.pem")
        if alpn:
            c.set_alpn_protocols(alpn)
        with socket.create_connection(("127.0.0.1", TLSPORT), 3) as s0:
            with c.wrap_socket(s0, server_hostname="sub.example") as s:
                s.sendall(text.replace("\n", "\r\n").encode()); out = b""
                try:
                    while True:
                        got = s.recv(65536)
                        if not got:
                            break
                        out += got
                except OSError:
                    pass
                return s.selected_alpn_protocol(), out
    al, r = tls(["h2", "http/1.1"], "GET /TokA/VPS.yaml HTTP/1.1\nHost: sub.example\n\nGET /nope HTTP/1.1\nHost: sub.example\nConnection: close\n\n")
    check("ALPN 应 http/1.1（客户端先报 h2 也退到 1.1，同没开 h2 的 nginx）；一条 TLS 连接连拉两个", al == "http/1.1"
          and r.count(b"HTTP/1.1 ") == 2 and b"x: 1\n" in r and r.endswith(page(404, "Not Found")))
    def hello_reply(port, offer):
        """自己走握手（内存 BIO），把服务端回的第一批原始字节拿出来。"""
        c = ssl.create_default_context(cafile=f"{tmp}/c.pem")
        if offer:
            c.set_alpn_protocols(offer)
        inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
        o = c.wrap_bio(inc, out, server_hostname="sub.example")
        try:
            o.do_handshake()
        except ssl.SSLWantReadError:
            pass
        return raw(port, out.read(), wait=1)
    check("客户端报了 ALPN 却没有 http/1.x（只报 h2）：回 no_application_protocol 警报就断（真 nginx 抓下来的那 7 个字节）",
          hello_reply(TLSPORT, ["h2"]) == bytes.fromhex("15030300020278") and hello_reply(TLSPORT, ["spdy/3", "h2"]) == bytes.fromhex("15030300020278"))
    check("不带 ALPN（Python / 老客户端）、带 http/1.0 的：照常握手", all(hello_reply(TLSPORT, o)[:1] == b"\x16" for o in (None, ["http/1.0"], ["h2", "http/1.1"]))
          and tls(None, G % "/TokA.yaml")[1].endswith(b"x: 1\n"))
    st, names, h, body = parts(req(TLSPORT, "GET / HTTP/1.1\nHost: h\n\n"))
    check("往 HTTPS 口发明文 HTTP → nginx 那张专门的 400（以前直接断开）", st == "HTTP/1.1 400 Bad Request" and names == ERR_HDRS
          and b"<title>400 The plain HTTP request was sent to HTTPS port</title>" in body and len(body) == 248)
    stall = socket.create_connection(("127.0.0.1", TLSPORT), 3)          # 连上不握手的：不能堵住别人
    t0 = time.time(); _, r = tls(None, G % "/TokA.yaml")
    check("有人连上不握手：别的请求照常秒回", r.endswith(b"x: 1\n") and time.time() - t0 < 2)
    stall.close()

    print("六、中转（token 对得上才放行）")
    open(f"{tmp}/gh.token", "w").write("RelayTok\n")
    st, names, h, body = parts(req(PORT, G % "/RelayTok/gh/evil.example/x"))
    check("token 对、不是 GitHub 的主机：404（不做开放代理）", st == "HTTP/1.1 404 Not Found" and body == page(404, "Not Found"))
    st, names, h, body = parts(req(PORT, G % "/Wrong/gh/raw.githubusercontent.com/a/b/main/c"))
    check("token 错：跟不存在的地址同一张 404", st == "HTTP/1.1 404 Not Found" and names == ERR_HDRS and body == page(404, "Not Found"))
    check("中转去 GitHub 取件照旧用中性 UA，不报家门", "'User-Agent': 'curl/8.5.0'" in src and "xy-sub" not in src.replace("报 'xy-sub'", ""))

    print("七、面板 / 定时任务换上新版")
    check("脚本里不再用 http.server（那一套的回包改不干净）", "import http.server" not in src and "SimpleHTTPRequestHandler" not in src)
    m.SUB_SERVER = f"{tmp}/old.py"; open(m.SUB_SERVER, "w").write("# old\n"); cmds = []
    m.sh = lambda c, check=True: cmds.append(c)
    check("老装机：进面板 / 03:20 那轮把小服务换成本版、重启 xy-sub 一次", m._sub_server_heal() and cmds == ["systemctl restart xy-sub"]
          and open(m.SUB_SERVER).read() == src and not m._sub_server_heal())
    xs = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
    check("换上时屏上一行，说清地址、token 不变", "✔ 订阅服务已更新（地址、token 不变）" in xs)

    print("八、跟真 nginx 当场对一遍")
    if not shutil.which("nginx"):
        print("  - 这台没装 nginx，跳过（期望值是 11/10 拿真 nginx 抓的）")
    else:
        NP, NT = free_port(), free_port()
        conf = (f"user root; worker_processes 1; pid {tmp}/ng.pid; error_log {tmp}/ng.err;\n"
                "events { worker_connections 64; }\n"
                "http { include /etc/nginx/mime.types; default_type application/octet-stream; access_log off; server_tokens off;\n"
                + "".join(f"  {k}_temp_path {tmp}/t{k};\n" for k in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
                + f"  server {{ listen 127.0.0.1:{NP}; listen 127.0.0.1:{NT} ssl; ssl_certificate {tmp}/c.pem;"
                  f" ssl_certificate_key {tmp}/k.pem; root {d}; }} }}\n")
        open(f"{tmp}/ng.conf", "w").write(conf)
        ng = subprocess.Popen(["nginx", "-c", f"{tmp}/ng.conf", "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(ng)
        if not wait_port(NP):
            print("  - nginx 没起来（这台不让它跑），跳过")
        else:
            lm = parts(req(NP, G % "/TokA.yaml"))[2]["Last-Modified"]
            battery = [G % p for p in ("/TokA.yaml", "/TokA.json", "/TokA.conf", "/TokA.txt", "/Lt9.links", "/", "/index.html", "/nope",
                                       "/TokA.yaml?x=1", "/TokA.yaml/", "//TokA.yaml", "/TokA%2Eyaml", "/a/../TokA.yaml",
                                       "/../x", "/%2e%2e/x", "/%zz", "/a%00b", "/" + "a" * 9000, "http://h/TokA.yaml")]
            battery += [f"{mth} {p} HTTP/1.1\nHost: h\nContent-Length: 0\nConnection: close\n\n"
                        for mth in ("HEAD", "POST", "PUT", "DELETE", "OPTIONS", "TRACE", "FOO") for p in ("/TokA.yaml", "/nope", "/")]
            battery += [f"GET /TokA.yaml HTTP/1.1\nHost: h\n{x}\nConnection: close\n\n" for x in (
                "Range: bytes=1-2", "Range: bytes=-2", "Range: bytes=3-", "Range: bytes=2-99", "Range: bytes=99-", "Range: bytes=3-1",
                "Range: lines=1-2", "Range: bytes=abc", f"If-Modified-Since: {lm}", "If-Modified-Since: Sat, 01 Jan 2000 00:00:00 GMT",
                "If-Modified-Since: junk", f"If-None-Match: {etag}", f"If-None-Match: W/{etag}", "If-None-Match: *", 'If-None-Match: "zz"',
                f'If-None-Match: "zz", {etag}', f"If-Modified-Since: {lm}\nIf-None-Match: \"zz\"")]
            battery += ["hello\n\n", "GET /TokA.yaml\n", "GET /nope\n", "GET /TokA.yaml HTTP/1.0\n\n", "GET /TokA.yaml HTTP/1.0\nConnection: keep-alive\n\n",
                        "GET /TokA.yaml HTTP/1.1\nConnection: close\n\n", "GET / HTTP/2.0\nHost: h\n\n", "GET /TokA.yaml HTTP/1.2\nHost: h\nConnection: close\n\n",
                        "get / HTTP/1.1\nHost: h\n\n", "GET TokA.yaml HTTP/1.1\nHost: h\n\n", "CONNECT h:443 HTTP/1.1\nHost: h\n\n",
                        "GET / HTTP/1.1\nHost: h\nX: " + "a" * 9000 + "\nConnection: close\n\n",
                        "GET /TokA.yaml HTTP/1.1\nHost: h\n\nGET /nope HTTP/1.1\nHost: h\n\nGET /TokA.json HTTP/1.1\nHost: h\nConnection: close\n\n",
                        "POST /TokA.yaml HTTP/1.1\nHost: h\nContent-Length: 5\n\nabcdeGET /TokA.txt HTTP/1.1\nHost: h\nConnection: close\n\n"]
            norm = lambda b: re.sub(rb"(?m)^Date: [^\r]*", b"Date: -", b)
            diff = [t.split("\n")[0][:60] + (" …" if "\n" in t.strip() else "") for t in battery if norm(req(PORT, t)) != norm(req(NP, t))]
            check(f"{len(battery)} 种请求（文件 / 错误 / 各种方法 / Range / 304 / 长连接 / 带正文）回包跟真 nginx 逐字节一样（只抹掉 Date）"
                  + (f"；不一样的：{diff[:4]}" if diff else ""), not diff)
            a, b = norm(req(TLSPORT, "GET / HTTP/1.1\nHost: h\n\n")), norm(req(NT, "GET / HTTP/1.1\nHost: h\n\n"))
            check("往 HTTPS 口发明文：跟真 nginx 一样", a == b and a)
            def alpn(port, offer):
                c = ssl.create_default_context(cafile=f"{tmp}/c.pem"); c.set_alpn_protocols(offer)
                try:
                    with socket.create_connection(("127.0.0.1", port), 3) as s0:
                        with c.wrap_socket(s0, server_hostname="sub.example") as s:
                            return s.selected_alpn_protocol()
                except ssl.SSLError as e:
                    return e.reason
            offers = (["http/1.1"], ["h2"], ["h2", "http/1.1"], ["http/1.0"], ["http/0.9", "h2"], ["spdy/3", "h2"])
            check("TLS 的 ALPN 选法跟真 nginx 一样（选哪个 / 拒不拒）", all(alpn(TLSPORT, o) == alpn(NT, o) for o in offers))
            check("……拒的时候回的原始字节也一样", all(hello_reply(TLSPORT, o) == hello_reply(NT, o) for o in (["h2"], ["spdy/3", "h2"])))
finally:
    for p in procs:
        p.terminate()

print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)

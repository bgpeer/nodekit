# nodekit · bgpeer 一键脚本

sing-box + xray 双核心、多协议一键部署，自动生成 **mihomo / sing-box / Shadowrocket** 三种订阅，
并可一键屏蔽中国域名/IP（白名单放行）。参考 [mack-a/v2ray-agent](https://github.com/mack-a/v2ray-agent) 的协议组合用 Python 重写，装完直接给客户端一条订阅链接即可。

> ⚠️ 仅供个人学习与合法用途，使用前请阅读文末[免责声明](#免责声明)。

---

## 一键安装

```bash
curl -sL https://raw.githubusercontent.com/bgpeer/nodekit/main/xy-installer.py -o /tmp/xy.py
sudo python3 /tmp/xy.py
```

若 `raw.githubusercontent.com` 被 GitHub 限流（HTTP 429），改用 jsDelivr 镜像（基本不会限流）：

```bash
curl -sL https://cdn.jsdelivr.net/gh/bgpeer/nodekit@main/xy-installer.py -o /tmp/xy.py
sudo python3 /tmp/xy.py
```

装过一次之后，以后直接敲 **`bgpeer`** 就能打开管理面板（内部已带镜像兜底，会尽量拉最新脚本）。

### 环境要求

- Debian / Ubuntu（systemd）
- root 权限
- Python 3
- 有域名走 acme 真证书更稳；无域名则自签证书 + 公网 IP 直连（域名需 A 记录直连指向本机）
- **证书自动续签**：走域名真证书时用 acme.sh 签发，acme.sh 会装每日 cron 自动续期（约 60 天一次），
  续期后自动重启 sing-box / xray（有 nginx 顺带 reload）使新证书生效，无需手动干预
- **内核自动更新**：安装后自动挂 cron，**每月北京时间 2 号凌晨 04:00** 把 sing-box / xray 更新到最新并重启一次
  （无新版则跳过）；也可随时进管理面板 **8 更新核心** 手动立即更新。日志在 `/var/log/bgpeer-coreupdate.log`

---

## 支持的协议

| 核心 | 协议 |
|------|------|
| **sing-box** | vless-vision、vless-ws、vmess-ws、trojan、hy2(端口跳跃+salamander混淆)、reality-vision、reality-grpc、tuic、vmess-httpupgrade、anytls |
| **xray** | vless-reality-xhttp（sing-box 不支持 xhttp，由 xray 承载） |

可以只装 sing-box、只装 xray，或两个一起装（端口/服务互不冲突）。

---

## 管理面板

装完敲 `bgpeer` 进入：

```
  1. 安装（已装则问是否重装节点，y 重装 / n 返回）
  2. 节点链接 / 订阅
  3. mihomo 配置
  4. sing-box 配置
  5. 小火箭配置
  6. 屏蔽中国域名和IP（CN 域名+IP 拦截 / 白名单放行）
  7. 更新脚本（不影响节点）
  8. 更新核心（sing-box / xray）
  9. 卸载
  0. 退出
```

### 订阅（三格式）

装完为每种客户端各生成一条订阅链接（HTTP 托管，默认端口 `20080`）：

| 客户端 | 格式 |
|--------|------|
| mihomo / Clash | `.yaml` |
| sing-box | `.json` |
| Shadowrocket（小火箭）| `.conf` |

每种格式的配置模板都在本仓库里，节点参数由服务端实时注入到模板锚点，重装换节点也会自动更新。
详见下面的 [自定义模板 & 锚点](#自定义模板--锚点)。

### 每个配置菜单（3 / 4 / 5）里可以

- **修改配置**：编辑器直接改成品配置文件
- **修改订阅**：查看当前订阅 / 换 token（换链接不动配置）
- **更新配置**：用作者模板或**自定义模板**重新生成
- **添加自定义模板链接**：把你放在 gist / GitHub 上、占位符一致的模板拉进来映射节点

> 重装节点会刷新三条订阅的 token；只更新脚本或更新配置不换 token。

### 屏蔽中国域名和IP（菜单 6）

在服务端路由里把 **CN 域名（geosite geolocation-cn）+ CN IP（geoip cn）** 走 reject，
白名单内的 CN 服务照常直连。逻辑独立在 `cn-block.py`，方便单独维护。

```
  1 屏蔽中国域名和IP        （已开则再选可关闭）
  2 放行白名单（作者名单 / 自定义名单）
  3 自定义放行名单脚本链接
  4 卸载（不想屏蔽了，直接清掉规则）
  5 退出
```

- 规则集用 sing-box **远程 srs**（`.srs` binary），挂 cron **每天北京时间 03:00 定点刷新**（UTC 19:00）。
- 优先走 **jsDelivr 镜像**、回退 raw；临时拉不到的先注入、交给自动更新重拉；
  确认不存在（404）的才跳过。
- 注入后会确认 sing-box 正常启动，**万一起不来自动回滚**，绝不影响原本能用的节点。
- 自定义白名单：可填纯文本 tag 列表，也可**直接指向一个 `whitelist-inject.sh` 脚本链接**，
  自动抽取其中的 `WHITELIST_TAGS=(...)` 数组。
- 白名单按**域名**（geosite）放行：客户端需走远程 DNS 解析（本项目生成的订阅配置默认即是）；
  若客户端本地解析后以裸 IP 出站，仍会被 CN IP 规则拦截。

规则集来自 [`bgpeer/rules`](https://github.com/bgpeer/rules)，
配套的 mack-a 白名单注入脚本见 [`bgpeer/vps-net`](https://github.com/bgpeer/vps-net)。

---

## 自定义模板 & 锚点

你可以把**自己的配置模板**放到 gist / GitHub raw，脚本按锚点把节点和国家随机组注入进去。
好处：**既能在模板里手写自己的静态节点，又能把成品配置直接托管到服务器保存**；
本仓库的作者模板既是参考，也是首次搭建的人开箱即用的配置。

**三个作者模板（点开直接看）**：

| 客户端 | 模板文件 |
|--------|----------|
| mihomo / Clash | [`sub-template.yaml`](https://github.com/bgpeer/nodekit/blob/main/sub-template.yaml) |
| sing-box | [`subbox-template.json`](https://github.com/bgpeer/nodekit/blob/main/subbox-template.json) |
| Shadowrocket（小火箭）| [`shadowrocket-template.conf`](https://github.com/bgpeer/nodekit/blob/main/shadowrocket-template.conf) |

### 三个锚点（三格式同名，各按自己语法渲染）

| 锚点 | 作用 | 展开成 | 放哪 |
|------|------|--------|------|
| `__XY_NODES__` | **建节点** | 你 VPS 的真实节点 | 独占一行（mihomo `proxies:` 段 / sing-box `outbounds` / 小火箭 `[Proxy]` 段） |
| `__XY_GROUPS__` | **建国家策略组** | 各国 url-test 随机组的**定义** | 独占一行（mihomo `proxy-groups:` 段 / sing-box `outbounds` / 小火箭 `[Proxy Group]` 段） |
| `__XY_NAMES__` | **引用国家组名** | 建好的国家组**名字清单** | **写在列表行内**（主选择组 / 服务组的 proxies·outbounds 里，可放多处） |

> 顺序依赖：必须先有 `__XY_GROUPS__` 把组**造出来**，`__XY_NAMES__` 才有组名可引用；
> 只写 `__XY_NAMES__` 不写 `__XY_GROUPS__` → 引用了不存在的组 → 客户端报错/起不来。

### 国家随机分组是怎么来的

- 脚本扫描**全部节点名**（`__XY_NODES__` 注入的订阅节点 **＋** 你手写进模板的静态节点），
  按**旗子 / 关键词**（如 `🇯🇵`、`JP`、`日本`、`Tokyo`）归国；
- 某国**≥2 个**节点才建该国随机组（`1` 个不建）；剩下没归入任何国家的进「🎲其他随机」；
  `🇺🇲` 自动归一到 `🇺🇸`；一个国家都没有则整段不建。
- **mihomo** 用 `filter`+`include-all`：客户端按正则**自动收拢**匹配节点（连你机场订阅合并进来的同国节点也会进组）；
- **sing-box / 小火箭** 没有 filter：由脚本**算好每国成员显式列进去**。

### 放置规则（照着作者模板抄最稳）

- **块锚点**（`__XY_NODES__` / `__XY_GROUPS__`）：**各占一行**。顶格或缩进都行（生成器会整行替换、自带缩进，缩进不会把 YAML 弄乱）。
- **行内锚点**（`__XY_NAMES__`）：写在 `[...]` 列表里。例：
  - mihomo：`proxies: ["♻️全部随机"__XY_NAMES__]` → `["♻️全部随机","🇯🇵日本随机","🇺🇸美国随机"]`
  - sing-box：`"outbounds": ["♻️随机", "__XY_NAMES__:.*"]`
- **只 sing-box** 的 `__XY_NAMES__` 可带正则后缀：
  - `__XY_NAMES__:.*` = 国家组名 **＋** 全部节点名；
  - 裸 `__XY_NAMES__` = **只**国家组名。
  - 老锚点 `__PATTERN__:.*`（只全部节点名）生成器仍兼容，但作者模板已统一用 `__XY_NAMES__`。

> 机制核心：**检测到锚点才展开，漏写不报错、只是不生成**。就算忘了写 `__XY_NODES__`，也只是不注入节点、模板原样拉上来，不会报错。

### 怎么用自定义模板

1. 进对应配置菜单（**3** mihomo / **4** sing-box / **5** 小火箭）→ **添加自定义模板链接**（填你 gist / GitHub raw 地址）；
2. 再选 **更新配置 → 自定义模板** 重新生成（不动节点、不换订阅 token）。

> 更新配置每次都**实时重新拉取**你模板的最新版；GitHub raw 有约 5 分钟 CDN 缓存，改完模板等一两分钟再点更新。

---

## 命令行用法（非交互）

也可以不进菜单，直接带参数安装：

```bash
# 装全部协议
sudo python3 xy-installer.py --sb all --xray all

# 指定协议 + 域名真证书
sudo python3 xy-installer.py --sb reality-vision,hy2,tuic --xray vless-reality-xhttp \
     --domain a.example.com --email me@example.com

# nginx 前置（443 伪装站 + webroot 证书，ws 类藏 443），需域名
sudo python3 xy-installer.py --sb all --domain a.example.com --nginx
```

常用参数：`--sb` / `--xray`（协议，逗号分隔或 `all`）、`--domain`、`--email`、
`--sni`（reality 借用目标站，默认 `s0.awsstatic.com`）、`--prefix`（节点名前缀）、
`--hy2-ports`（hy2 端口跳跃范围，默认 `30000-31000`）、`--nginx`、
`--no-reality-443`（默认会把主力 reality 绑 443 抗封端口，加此参数则不绑）、
`--yes`（检测到 mack-a 等现有安装直接接管）。

---

## 伪装 / 加密建议

装机时脚本会自动做两项检查，帮你把伪装做扎实（都只提示、不阻断安装）：

- **Reality SNI 预检**：选了 reality 系列协议时，装前会探测你借用的 SNI 目标是否
  **可达 + 支持 TLS1.3 + HTTP/2**。不合格会给黄色警告并建议换站
  （推荐 `www.microsoft.com` / `addons.mozilla.org` / `s0.awsstatic.com` / `dl.google.com`
  这类大流量、支持 h2、不套 CDN、不在国内的站）。借用不合格的站会让 reality 握手特征更容易被识别。
- **无域名自签提示**：不给域名时，依赖证书的 TLS 协议（vless-vision / trojan / ws 家族 / anytls）
  只能走**自签证书 + 客户端 `allowInsecure`**——内容仍加密（各协议有自己的密码/UUID），
  但失去证书校验、且自签本身是明显特征。**想要更强伪装：优先用 `reality-*` 系列**
  （借真站证书，无需域名、无 insecure），或补一个域名走 acme 真证书。
  hy2 / tuic 用自签是行业常规，无需担心。

订阅还带两项客户端增强（服务端已同步支持，均自动生成、无需手动配置）：

- **X25519MLKEM768 后量子密钥交换**（默认开）：`reality-*` 节点在 mihomo 订阅里带
  `reality-opts.support-x25519mlkem768: true`，握手改用抗量子的混合 KEX，也能进一步
  打散 reality 的 ClientHello 指纹。此字段由客户端主动发起，**旧核心会握手失败**，
  故脚本会**先检测本机核心版本**（sing-box ≥ 1.12.0、xray ≥ 25.5.16 才下发；
  版本读不出或过旧则自动省略，保连通性优先）。本脚本每月自动更新核心，正常无需担心。
- **smux 多路复用**（**默认关，安装时询问**）：仅 **ws / httpupgrade** 家族可两头开
  `h2mux`（mihomo `smux`、sing-box `multiplex`）。多条请求复用一条底层连接，
  **网页/小请求延迟更低、连接数更少更隐蔽**；但同一条 TCP 上的**队头阻塞**会让
  **大文件下载 / 测速 / 丢包重的跨境线**变慢，所以默认关——选了 ws 类节点时装机会问一句
  `y开启/n不开(回车=不开)`，命令行用 `--smux` 开启。
  vision / reality / grpc / QUIC(hy2、tuic) / anytls **一律不参与**（它们要么自带更优复用、
  要么与 xray `mux.cool` 不兼容），xray 承载的 ws 也不带该标记，避免两端复用协议不一致。

**伪装站可自替换**：`--nginx` 模式下 443 的伪装首页在 `/var/www/bgpeer/index.html`
（默认是一个「维护中」通用静态页，不是一眼假的 Apache 默认页）。你可以直接覆盖它换成自己的
真站内容，伪装效果更好。

### reality 绑 443（抗 GFW 封端口，默认开启）

xray 内核会警告 `REALITY: Listening on non-443 ports may get your IP blocked by the GFW`——
reality 跑在非 443 高端口，从国内长期用有被封 IP 的风险。为此脚本**默认把主力 reality
协议绑到 443**（优先 sing-box `reality-vision`）：

- 主动探测打你的 443 → sing-box 把握手转发到**借用的真站**（如 awsstatic），
  看到的是真站证书，和真访问该站无法区分——比任何本地伪装站都强。
- 端口随机化削弱"一排代理端口"的扫描指纹；reality 上 443 再消掉"非 443 易被封"的风险，两者互补。

与 nginx 前置的关系：reality 独占 443/TCP 时，**nginx 只保留 :80 用于 acme 证书续期**
（续期不会因此中断），ws 家族不再藏 443、改走各自端口的真证书；443 的"网站伪装"
由 reality 借用的真站接管。其余协议（trojan/tuic/anytls/vless-vision 等）留在随机端口作备用。

加 `--no-reality-443` 可关闭此行为（reality 回到随机端口、保留 nginx 443 前置）。

### SNI 分流（`--sni-split`，最强抗封锁，需域名 + reality-vision）

比 reality 直绑 443 更进一步：用 nginx `stream` + `ssl_preread` 在 443 **按 SNI 不解密分流**，
让 **reality + 网站 + ws 全部只用 443**，对外就是一个 HTTPS 网站：

- SNI = reality 借用域名（如 `s0.awsstatic.com`）→ 转发给本地 reality 端口；
- SNI = 你的真域名 / 默认 → 转发给本地 https（伪装站 + ws 反代）；
- hy2 仍走自己的 UDP 端口 + 跳跃（QUIC 与 nginx 的 443/TCP 不冲突）；
- 证书续期继续走 nginx `:80` webroot，不受影响。

安全兜底：改 `nginx.conf` **前先做 preflight**（装 `libnginx-mod-stream`，用测试配置跑 `nginx -t`），
探测不过就**自动退回 reality-443 直连**；正式写入若 `nginx -t` 不过则**整体回滚**并还原
`nginx.conf`，绝不把现有能用的 443 改坏。交互安装选了域名 + reality-vision 时会询问是否启用。

> v1 只把 sing-box `reality-vision` 放到 443 SNI 分流后面；其余 reality（xray xhttp 等）仍在随机端口。
> 这是较大的架构改动，务必在你自己的机器上实测（`openssl s_client` 打 443 分别用两个 SNI 验证走向 + 客户端逐协议连通）。

---

## 卸载

管理面板选 **9. 卸载**，会移除本脚本安装的 sing-box / xray / 订阅服务、配置、证书、
hy2 端口跳跃规则、nginx 前置块与 `bgpeer` 命令。

---

## 相关仓库

- [`bgpeer/rules`](https://github.com/bgpeer/rules) — geosite / geoip 规则集（srs）
- [`bgpeer/vps-net`](https://github.com/bgpeer/vps-net) — mack-a 白名单注入脚本 `whitelist-inject.sh`

---

## 免责声明

1. 本项目（及 `xy-installer.py`、`cn-block.py` 等脚本）仅供**学习、研究与合法用途**，
   用于搭建你**自己拥有或已获授权**的服务器上的网络代理服务。
2. 请在使用前了解并遵守你**所在国家/地区以及服务器所在地**的相关法律法规。
   因使用本项目产生的一切后果（包括但不限于违反当地法律、服务商封停、数据泄露、财产损失等）
   **由使用者自行承担**，项目作者不承担任何责任。
3. 本项目**不提供**任何代理服务、节点或订阅，也不鼓励、不协助任何违法活动。
4. 脚本会安装并运行第三方软件（sing-box、xray 等），并从第三方来源（GitHub、jsDelivr 等）
   下载核心与规则集；这些第三方内容的可用性、安全性与合规性由其各自提供方负责。
5. 本项目按“**现状**”（AS IS）提供，不作任何明示或暗示的担保。作者不保证其无错误、
   不中断或适用于任何特定用途。你需自行评估风险后使用。
6. 一旦下载、安装或使用本项目，即视为你已阅读、理解并同意以上全部条款。

本项目基于 [MIT License](./LICENSE) 开源。

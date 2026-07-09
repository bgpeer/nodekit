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

---

## 支持的协议

| 核心 | 协议 |
|------|------|
| **sing-box** | vless-vision、vless-ws、vmess-ws、trojan、hy2(端口跳跃)、reality-vision、reality-grpc、tuic、vmess-httpupgrade、anytls |
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

每种格式的配置模板都在本仓库里（`sub-template.yaml` / `subbox-template.json` / `shadowrocket-template.conf`），
节点参数由服务端实时注入到模板锚点，重装换节点也会自动更新。

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

- 规则集用 sing-box **远程 srs**，每 **24h 自动更新**一次，无需额外定时任务。
- 优先走 **jsDelivr 镜像**、回退 raw；临时拉不到的先注入、交给自动更新重拉；
  确认不存在（404）的才跳过。
- 注入后会确认 sing-box 正常启动，**万一起不来自动回滚**，绝不影响原本能用的节点。
- 自定义白名单：可填纯文本 tag 列表，也可**直接指向一个 `whitelist-inject.sh` 脚本链接**，
  自动抽取其中的 `WHITELIST_TAGS=(...)` 数组。

规则集来自 [`bgpeer/rules`](https://github.com/bgpeer/rules)，
配套的 mack-a 白名单注入脚本见 [`bgpeer/vps-net`](https://github.com/bgpeer/vps-net)。

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
`--hy2-ports`（hy2 端口跳跃范围，默认 `30000-31000`）、`--nginx`、`--yes`（检测到 mack-a 等现有安装直接接管）。

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

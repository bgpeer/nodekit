# 这个仓库的硬规矩

## 一、不泄漏

**泄漏作者信息或安装人信息，在代码界是大忌。** 这条排在功能之前。

两类都不许漏：

- **作者信息** —— 真实姓名、邮箱、私人域名、任何能把仓库和具体某个人对上的东西。
- **安装人信息** —— 用它的人的 VPS 真实 IP、域名、节点参数（端口 / UUID / 密码 /
  reality 私钥）、订阅 token、"这台机器在跑代理" 这件事本身，以及 "这台机器在跑
  nodekit" 这件事本身。

### 动手前先问三句

1. **这条数据会离开这台机器吗？** 出网请求、写进客户端配置、打印到屏幕、落进日志、
   落进能被别人读到的文件——都算离开。
2. **谁会看到？** 只有 root？同机的非 root 进程（nginx 的 www-data、Docker 里的
   openlist/emby、AdGuard）？链路上的中间人？第三方服务？扫端口的人？
3. **有没有不漏的做法？** 十有八九有：换中性 User-Agent、换个不报家门的 Server 头、
   把文件收成 0600、把第三方换成自己的中转、干脆不发这个请求。

### 已经立下的规矩（别改回去）

- 出网一律用中性 `HTTP_UA`，不准写 `xy-installer` / `media-stack` 这类自报家门的串。
  （`media-stack` 里的 `BROWSER_UA` 是**故意**伪装浏览器绕网盘 UA 封锁的，例外。）
- 订阅/中转服务对外报 `Server: nginx`，nginx 那边 `server_tokens off`，
  两边口径一致、都不报版本号。
- `SUB_DIR` 里必须有个空 `index.html`，否则 `SimpleHTTPRequestHandler` 会列目录，
  把所有 `<token>.yaml` 文件名——也就是订阅 token——一次性摆出来。
- 带凭据的 URL（`?token=` / `?access_token=` / `?X-Amz-Signature=`）不准喂给公共反代。
- 自己的中转永远排在公共反代前面。
- 存 token / 密钥 / 安装参数的文件和目录一律 root-only（见 `harden_perms`）。
- **不准加任何打点 / telemetry。** 不统计安装量，不回传任何东西。

### 实在躲不掉的时候

有些泄漏是功能本身带来的、去不掉（比如向 Let's Encrypt 申请证书必然要告诉它域名）。
这种**不要自己拍板**：说清楚漏的是什么、谁能看到、有多严重，**拿来跟仓库主人商量**，
他点头了再做。

## 二、版本号

- `xy-installer.py` 的 `SCRIPT_VERSION` 由 CI 自动 +1，**别手动改**。
- `media-stack.py` 的 `SCRIPT_VERSION` **手动改，只动最后一位**（1.5.0 一路加到
  1.5.999，前两位不要自己动）。
- 版本挨着跳，不要跳大版本。

## 三、改完要做的事

- 改了就自己提 PR 并合并，不用等回覆。
- 每次改动都要有回归测试，测试放在 scratchpad 的 `t/` 下，改完把整套跑一遍。
- 模板（`sub-template.yaml` / `subbox-template.json` / `shadowrocket-template.conf`）
  是运行期从 raw 直接拉的，合并即生效；但 VPS 上已生成的成品配置要重新生成订阅才更新。

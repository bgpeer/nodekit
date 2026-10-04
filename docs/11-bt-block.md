[← 返回主页](../README.md#目录)

# 11. BT/PT 下载屏蔽

装好后随时开/关,不用重装、不动节点。菜单里 `1` 循环切换。开启后服务端识别到 BT/PT
流量即 **reject**,防止有人用你的 VPS 挂 BT 下载、招来机房投诉封机:

- **sing-box**:路由加 `sniff` + `protocol: bittorrent → reject`,和「屏蔽中国域名/IP」的规则**互不覆盖**(各自只增删自己那几条)。
- **xray**:入站开安全嗅探(`routeOnly`)+ 路由 `bittorrent → block`;**vision 流入站自动跳过**(在它上面开嗅探会干扰,故不动)。
- best-effort:大部分 BT 会被拦,但 vision 流可能漏一小部分——这是协议特性,mack-a 同款限制。
- 开关状态记在 `bt.json`,重装节点会自动重新注入,不用再点一次。

---

[← 返回主页](../README.md#目录)

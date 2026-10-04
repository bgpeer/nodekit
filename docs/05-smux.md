[← 返回主页](../README.md#目录)

# 5. 多路复用开关 smux

装好后随时开/关 smux，不用重装、不动节点。菜单里 `1` 循环切换（显示当前状态，
`y` 确认 / `n` 返回）。执行后自动:改 sing-box ws 入站的 `multiplex` → 同步分享链接
标记 → 重启 sing-box → 刷新三格式订阅（token/URL 不变）。改完客户端重新拉订阅，
或到各配置菜单点 **3 更新配置** 即可生效。只影响 ws/httpupgrade 类 sing-box 节点，
xray 承载的 ws、reality/vision/QUIC 等一概不动。

---

[← 返回主页](../README.md#目录)

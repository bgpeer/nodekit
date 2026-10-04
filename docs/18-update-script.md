[← 返回主页](../README.md#目录)

# 18. 更新脚本

面板标题栏会显示当前脚本版本（如 `bgpeer 一键脚本 v1.0.0`）；点 **18 更新脚本** 时会显示
`v旧 → v新`，一眼看出更到了什么版本。

发布是全自动的：脚本/模板有改动**合并进 main 就算发布**——GitHub Actions 会自动把
`xy-installer.py` 里的 `SCRIPT_VERSION` 补丁位 +1（如 1.0.0 → 1.0.1）、打 `vX.Y.Z` 标签、
并在仓库 [Releases](https://github.com/bgpeer/nodekit/releases) 页创建发布（更新说明自动生成），
不需要手动操作。想升大版本（如 2.0.0），合并前手动把 `SCRIPT_VERSION` 改成目标版本号即可，
CI 检测到比已发布的高就直接用它，不再自动 +1。

---

[← 返回主页](../README.md#目录)

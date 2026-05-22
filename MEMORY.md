# Minecraft Server Tool - Project Memory

本文档作为项目的个人索引，记录核心架构决策、已完成功能及跨平台（macOS/Windows）开发要点。

## 1. 核心架构：模块化 Python 引擎
*   **入口**: `main.py` (指挥官，负责交互与逻辑分发)
*   **环境管理**: `core/env_manager.py` (处理 Java 路径锁定与系统环境检测)
*   **服务端安装**: `core/server_installer.py` (对接 Paper/Fabric API)
*   **模组同步**: `core/mod_downloader.py` (对接 Modrinth API，基于数据库自动下载)

## 2. 数据库与配置
*   **模组库**: `MOD_DATABASE.md` (记录 Modrinth ID、平台、功能说明)
*   **开发规划**: `WINDOWS_PLAN.md` (记录 Windows GUI 版的功能蓝图)

## 3. 开发关键点 (Memory Notes)
*   **严格开发流程**: 所有开发、调试和测试必须在 `自动脚本` 文件夹中进行。**严禁**主动修改 `我的世界自动开服软件` 目录下的内容。
*   **同步机制**: 只有在 `自动脚本` 中的功能测试通过，且用户明确下达“同步”或“发布”指令后，才能将代码复制到 `我的世界自动开服软件` 准备提交 GitHub。
*   **路径锁定**: 强制使用 `SCRIPT_DIR` 获取脚本物理路径，确保服务器文件夹始终在脚本同级目录生成。
*   **Java 匹配**: macOS 使用 `java_home` 锁定版本；Windows 未来需适配注册表路径或 `where` 命令。
*   **API 逻辑**: 使用 Modrinth V2 API，通过 `game_versions` 和 `loaders` 参数进行过滤。已升级至 v2.1，支持模糊匹配和多加载器回退。

## 4. 产品形态定位（已锁定）
*   **对标**: HMCL（Hello Minecraft! Launcher）的服务端版，故名 HMSL。
*   **技术路线**: Python + CustomTkinter + PyInstaller（macOS 出 `.app`、Windows 出 `.exe`），不走 Java/JavaFX 重写。
*   **理由**: HMCL 早期选 Java 是因当时 Python GUI 不成熟；今天用 Python + PyInstaller 可等价复刻"双击即用"体验，且现有 `core/` 与 `gui_main.py` 可全部保留。
*   详见 `WINDOWS_PLAN.md`。

## 5. 遗留问题与待办
*   **模组同步盲点**: TAB, FerriteCore, Connectivity 在 Modrinth API 中由于标签不规范导致同步率较低 (目前 12/15)。由于当前硬件环境（i7-13700HX/U9-285K）性能过剩，暂不急于死磕这三个模组，待后续优化。
*   [ ] **GUI 按钮断点**: 首页"开启服务器"、版本管理"启动"按钮未绑 command。
*   [ ] **Windows 兼容性**: `.bat` 启动脚本 + 注册表 Java 检测 + 抽离 POSIX-only 调用。
*   [ ] **高级编辑器**: 可视化编辑 `server.properties`。
*   [ ] **模组预设包 UI**: 纯净性能 / 社交增强 / 全家桶 / 自定义勾选。
*   [ ] **打包发布**: PyInstaller 双平台构建 + 图标 + 首次运行向导。
*   [ ] **模组库扩充**: 持续录入经过验证的常用模组 ID。

---
*上次更新日期: 2026年5月21日*

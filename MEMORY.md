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
*   **API 逻辑**: 使用 Modrinth V2 API，通过 `game_versions` 和 `loaders` 参数进行过滤。

## 4. 待办与后续目标
*   [ ] **Windows 兼容性优化**: 增加 `.bat` 启动脚本支持。
*   [ ] **GUI 开发**: 基于 Python (PyQt6/Tkinter) 构建可视化操作界面。
*   [ ] **高级编辑器**: 可视化编辑 `server.properties`。
*   [ ] **模组库扩充**: 持续录入经过验证的常用模组 ID。

---
*上次更新日期: 2026年5月20日*

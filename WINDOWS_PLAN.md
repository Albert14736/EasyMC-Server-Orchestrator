# HMSL - Hello Minecraft! Server Launcher 开发规划

> 目标：做一个**服务端版的 HMCL**。HMCL 解决客户端启动与版本管理，HMSL 解决服务端的一键部署、模组同步与可视化管理。

## 0. 项目定位与形态参照

| 维度 | HMCL（参照对象） | HMSL（本项目） |
| :--- | :--- | :--- |
| 面向 | 玩家客户端 | 服主 / 服务端 |
| 语言 | Java + JavaFX | **Python + CustomTkinter** |
| 分发形态 | `.jar` / `.exe`（exe 是 jar+启动器壳） | **`.app` / `.exe`（PyInstaller 打包）** |
| 体验 | 双击即开，图形化全流程 | 双击即开，图形化全流程 |
| 跨平台 | JVM 一份代码全平台 | Python 一份代码 + PyInstaller 各平台单独构建 |

HMCL 早期选 Java 是因为彼时 Python GUI 生态不成熟；今天用 Python + CustomTkinter + PyInstaller 是更轻、更快的等价路径，能完整复刻 HMCL 的"双击即用"体验。

## 1. 技术选型（已锁定）

* **语言**：Python 3.10+
* **GUI 框架**：CustomTkinter（已在用，深色现代化主题）
* **打包工具**：
  * **Windows** → PyInstaller，输出单文件 `.exe`；后续可选 NSIS / Inno Setup 生成安装包
  * **macOS** → PyInstaller `--windowed`，输出 `.app`；后续可选 `create-dmg` 生成 `.dmg`
* **HTTP / API**：`requests`（Modrinth V2 API）
* **并发**：`threading` + `queue`（GUI 已用此模式喂日志）
* **外部依赖**：仅需用户系统装好 **Java**（脚本会自动检测/引导下载），其余全部内置

## 2. 架构（仿 HMCL 三段式）

| HMCL 模块 | HMSL 对应 | 现状 |
| :--- | :--- | :--- |
| `HMCLCore` | `core/`（env_manager / server_installer / mod_downloader） | ✅ 已成型 |
| `HMCL`（UI） | `gui_main.py` | 🟡 骨架已搭，部分按钮未接线 |
| `HMCLBoot`（启动引导） | PyInstaller 打包产物 + 首次运行的环境检测 | ⬜ 待做 |

## 3. 核心功能模块

### A. 服务器实例管理器（对标 HMCL 的"版本列表"）
* 自动扫描脚本目录下的服务器文件夹（识别 `server.jar` / `eula.txt`）
* **+ 实例注册表（待实装）**：在 `~/.hmsl/instances.json`（或脚本同目录的 `instances.json`）维护一份用户创建过的所有服务器路径清单。每次"新建服务器"成功后追加一条；每次"版本管理"页加载时合并 [当前目录扫描结果 + 注册表已知路径]，并对每条记录做存活检查（路径还在？`server.jar` 还在？）。这让用户把服务器放在任何位置（D 盘、外接硬盘、桌面）都能在同一个 GUI 里管理。
* 卡片式实例列表（已有雏形 [gui_main.py:95](gui_main.py#L95)）
* 单击启动 / 打开文件夹 / 删除 / 重命名
* **当前断点**：启动按钮未绑定 command；首页"当前选中实例"未联动

### B. 新建服务器向导（对标 HMCL 的"安装新游戏版本"）
* 名称 → 版本 → 服务端类型（Paper / Fabric / Forge / NeoForge）→ 进度日志
* 已实装，见 [gui_main.py:117](gui_main.py#L117)

### C. 高级配置编辑器（Properties Editor）
* 把 `server.properties` 的项变成开关 / 滑块 / 下拉
* 每项附中文说明（online-mode、PVP、view-distance、max-players…）
* ⬜ 未开始

### D. 模组管理与预设包
* **预设包**：纯净性能包 / 社交增强包 / 全家桶 / 自定义勾选
* **数据源**：`MOD_DATABASE.md` + Modrinth API
* **权限初始化**：装 LuckPerms 后自动落一套默认权限组文件
* 🟡 后端 `mod_downloader.py` 已能跑（17/x 模组录入，3 个同步盲点搁置），UI 勾选界面未做
* **⭐ 大幅扩展**：详细规划见 [MOD_MANAGEMENT_PLAN.md](MOD_MANAGEMENT_PLAN.md) — 含 Modrinth/CurseForge 搜索、整合包导入、客户端模组扫描清理

### E. 启动脚本生成（跨平台）
* macOS / Linux → `start.sh`（`#!/bin/zsh`，已实装）
* **Windows → `start.bat`**（⬜ 待做，目前 [gui_main.py:208](gui_main.py#L208) 硬编码 sh）
* 用 `sys.platform` 分支判断

### F. 异常与引导
* Java 缺失 → 弹窗 + 跳官网 / 内置下载
* 端口占用、内存溢出 → 明显的弹窗错误提示
* 首次运行向导

## 4. 跨平台差异处理清单

| 项 | macOS | Windows |
| :--- | :--- | :--- |
| Java 定位 | `/usr/libexec/java_home -v N` | 注册表 `HKLM\SOFTWARE\JavaSoft\...` + `where java` 兜底 |
| 启动脚本 | `start.sh` (zsh) | `start.bat` |
| 路径分隔 | 全程用 `os.path.join` / `pathlib` | 同上 |
| 打开文件夹 | `open <path>` | `explorer <path>` |
| 打包产物 | `.app` (+ `.dmg`) | `.exe` (+ NSIS 安装包) |

## 5. 发布构建流程（新增章节）

1. 在对应平台机器上 `pip install -r requirements.txt pyinstaller`
2. macOS：`pyinstaller --windowed --name HMSL --icon assets/icon.icns gui_main.py`
3. Windows：`pyinstaller --onefile --windowed --name HMSL --icon assets/icon.ico gui_main.py`
4. 把 `MOD_DATABASE.md` 用 `--add-data` 一并打包进去
5. 产物放 `dist/`，手动测试无问题后再同步到 [我的世界自动开服软件/](../我的世界自动开服软件/) 发布目录推 GitHub Release

## 6. 路线图（按优先级）

### 第一阶段：补齐 GUI 断点（短期）
- [ ] 首页"🚀 开启服务器"按钮接线 + 选中实例联动 [gui_main.py:70](gui_main.py#L70)
- [ ] 版本管理页"▶ 启动"按钮接线 [gui_main.py:115](gui_main.py#L115)
- [ ] 实例删除 / 重命名

### 第二阶段：Windows 兼容（中期）
- [x] `.bat` 启动脚本生成（[core/server_factory.py](core/server_factory.py)，按 loader 分支：Forge/NeoForge 委托 `run.bat`，Paper/Fabric 直跑 `-jar server.jar`）
- [x] Windows Java 路径检测：JAVA_HOME → 注册表（Oracle/Adoptium/Microsoft/Corretto/Zulu）→ 常见安装目录 glob → `where java`，每个候选项用 `java -version` 验真版本（[core/env_manager.py](core/env_manager.py)）
- [x] 用 `sys.platform` 抽掉所有 `open` / `chmod` 等 POSIX-only 调用（GUI"打开文件夹"、`os.chmod` 已 gated、`start.sh`/`start.bat` 已分支）
- [x] subprocess 加 `encoding="utf-8", errors="replace"` 防 Windows cp936 locale 下中文/emoji 崩溃
- [x] InstanceScanner 也认 `start.bat`，Windows 实例可被识别为 HMSL 管理
- [ ] 首次在 Windows 机器上跑通（待用户手头有 Windows 时验证）

### 第三阶段：功能扩展
- [ ] `server.properties` 可视化编辑器
- [ ] 模组预设包勾选 UI
- [ ] LuckPerms 权限初始化脚本
- [x] **实例注册表**（见 §3.A）：让服务器可以建在任意目录、跨目录被管理
- [ ] **⭐ 模组与整合包管理大模块** — 详见 [MOD_MANAGEMENT_PLAN.md](MOD_MANAGEMENT_PLAN.md)，分 5 期
  - [ ] Phase 1: Modrinth 模组搜索 + 客户端警告
  - [ ] Phase 2: 服务端模组扫描清理（推荐**优先**起手）
  - [ ] Phase 3: `.mrpack` 整合包导入
  - [ ] Phase 4: CurseForge 集成
  - [ ] Phase 5: MultiMC / HMCL 整合包格式

### 第四阶段：发布形态（最终目标）
- [ ] PyInstaller 打包脚本（macOS / Windows 双份）
- [ ] 图标资源（`.icns` / `.ico`）
- [ ] 首次运行向导（Java 检测、目录初始化）
- [ ] GitHub Release 自动化（可选 GitHub Actions 矩阵构建）

---

## 7. 与 HMCL 的差异声明

* **HMCL** 专注客户端：账号、版本、模组、皮肤、启动参数
* **HMSL** 专注服务端：服务端核心、模组同步、`server.properties`、权限、运维启停
* 二者 UI 哲学一致（侧边栏导航 + 卡片化实例 + 向导式创建），用户从 HMCL 切到 HMSL 应该零学习成本

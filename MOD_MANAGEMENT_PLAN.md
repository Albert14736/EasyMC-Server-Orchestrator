# HMSL 模组与整合包管理 - 功能规划

> 这是一个独立的大型功能模块（与 [WINDOWS_PLAN.md](WINDOWS_PLAN.md) 的跨平台关注点不同）。
> 目标：把 HMSL 从"会装服务端"升级到"完整管理服务端的模组生态"，体验对标 HMCL 客户端但聚焦服务端场景。
>
> 用户需求来源：2026-05-22 会话明确确认。

## 0. 设计原则

1. **提示而不阻止**：用户可能有特殊需求（比如想给客户端整合包做开发对照），客户端模组装到服务端要警告 + 二次确认，但允许继续。
2. **API 优先**：能用 hash 查的不用关键词，能用 Modrinth API 的不爬网页（合规且稳定）。
3. **复用 [core/mod_downloader.py](core/mod_downloader.py)**：已对接 Modrinth V2 API，下载/筛选逻辑成熟。
4. **GUI 与逻辑分离**：所有功能先写 `core/` 纯函数 + pytest，再接 GUI（参见 [feedback-hmsl-separate-gui-logic 记忆]）。

---

## 1. 总功能蓝图

| 功能 | 描述 | 类比 HMCL |
|---|---|---|
| **A. 模组搜索面板** | GUI 内直接搜索 Modrinth/CurseForge，按服务器版本+loader 自动过滤，一键安装 | "模组管理 → 搜索安装" |
| **B. 客户端模组警告** | 检测到选中的是仅客户端模组，弹二次确认窗（不阻止） | HMCL 装客户端 mod 无这层 |
| **C. 整合包拖入导入** | 支持拖拽常见整合包文件，自动解析版本/loader/模组清单，落地为 HMSL 实例 | "导入整合包" |
| **D. 服务端模组扫描清理** | 选中服务器 → 扫描 mods/ 目录所有 jar，分类显示客户端专属/未知模组，一键清理客户端模组 | 无（这是服务端版独有需求） |

---

## 2. 涉及的外部 API 与协议

### 2.1 Modrinth V2 API（已对接，免费无 key）

| 端点 | 用途 | 关键参数 |
|---|---|---|
| `GET /v2/search` | 关键词搜索模组 | `query`, `facets=[["versions:1.20.4"],["categories:fabric"]]` |
| `GET /v2/project/{id}` | 查模组元数据 | 包含 `client_side`, `server_side` 字段（required/optional/unsupported） |
| `GET /v2/version_file/{hash}?algorithm=sha1` | **用文件 hash 反查模组** ⭐ | 给定一个 jar 的 sha1，返回它属于哪个项目哪个版本 |
| `GET /v2/project/{id}/version` | 列出模组所有发布版本 | 用来筛选适配当前 MC 版本/loader 的版本 |

**关键洞察**：扫描清理（功能 D）必须用 **hash 反查**而非文件名匹配——文件名经常被改、撞车，hash 是身份证。

### 2.2 CurseForge API（需要 API key）

- 注册地址：https://console.curseforge.com/
- 每个开发者一个 key，限频不严
- **Phase 4 再做**：搞 CF key 分发是工程负担（要么 ship key 有被撤风险，要么让用户自己注册有体验损失）

### 2.3 整合包格式

| 格式 | 标志文件 | 解析方式 |
|---|---|---|
| **Modrinth `.mrpack`** | ZIP 内 `modrinth.index.json` | 解 ZIP → 读 index → 按 hash 下载模组到 overrides 应有的位置 |
| **CurseForge `.zip`** | ZIP 内 `manifest.json` | 解 ZIP → 读 manifest → 调 CF API 按 projectID+fileID 下载 |
| **MultiMC** | ZIP 内 `mmc-pack.json` + `instance.cfg` | 字段较散，需要单独实现一个 reader |
| **HMCL** | ZIP 内 `hmclversion.cfg` 等 | 自家格式，DeepWiki 可查 |

---

## 3. 分期实施路线图

### Phase 1 — Modrinth 模组搜索 + 客户端警告（MVP，最高价值）

**新增文件**：
- `core/modrinth_search.py`：纯函数 `search_mods(query, mc_version, loader, page=1, limit=20) -> List[ModSearchResult]`、`get_project_compat(project_id) -> CompatInfo` 等
- `tests/test_modrinth_search.py`：用 `requests-mock` 或简单 `monkeypatch` mock HTTP

**GUI 改动 [gui_main.py](gui_main.py)**：
- 在"版本管理"页 action bar 新增按钮 `🔍 浏览模组`（选中实例后启用）
- 新窗口 `ModBrowserWindow`（CTkToplevel）：
  ```
  ┌─────────────────────────────────────────────┐
  │ [搜索框..............] [搜索]   过滤: [✓1.20.4] [✓Forge] │
  ├─────────────────────────────────────────────┤
  │ ┌──────────────────────────────────────────┐
  │ │ [icon] Sodium                            │
  │ │        提升 FPS 的优化模组                │
  │ │        🔥 1.2M | server: unsupported     │
  │ │                              [安装]       │
  │ └──────────────────────────────────────────┘
  │ ┌──────────────────────────────────────────┐
  │ │ ... 下一个 ...                            │
  │ └──────────────────────────────────────────┘
  └─────────────────────────────────────────────┘
  ```
- 点"安装"：
  1. 调 `get_project_compat()` 检查 `server_side`
  2. 若 `unsupported`（仅客户端）→ 弹 `ConfirmDialog`："这是纯客户端模组，装到服务端通常没用甚至会崩。继续？" [是] / [否]
  3. 用户确认后下载最匹配版本到 `server_path/mods/`（Paper 服务器则是 `plugins/`）

**测试覆盖**：
- 搜索结果解析、版本过滤、compat 分类（required/optional/unsupported 三态映射）

**预估工作量**：1 个 session

---

### Phase 2 — 服务端模组扫描清理（独立高价值）

**新增文件**：
- `core/mod_scanner.py`：
  - `compute_jar_sha1(path) -> str`
  - `lookup_mod_on_modrinth(sha1) -> Optional[ModInfo]`
  - `scan_server_mods(server_path) -> ScanReport`，返回每个 jar 的分类：`{path, name, status: "client_only" | "server_ok" | "unknown" | "error", project_info?}`
- `tests/test_mod_scanner.py`

**GUI**：
- "版本管理"页 action bar 新增按钮 `🧹 扫描模组`
- 点击 → 弹 `ModScanWindow`，进度条 "正在扫描第 X/Y 个模组..."
- 完成后切换到表格视图：
  ```
  扫描结果（X 个模组）
  ┌──────────────────────────────────────────┐
  │ ☑ 客户端专属（3 个）                       │
  │   ☑ optifine.jar         [删除]           │
  │   ☑ sodium.jar           [删除]           │
  │   ☑ iris.jar             [删除]           │
  │ ☐ 未知模组（2 个，搜索不到）                │
  │   ☐ custom_mod_v2.jar    [删除]           │
  │   ☐ private_mod.jar      [删除]           │
  │ ☐ 服务端兼容（X 个）— 不显示明细           │
  ├──────────────────────────────────────────┤
  │ [一键删除勾选的客户端模组]  [关闭]          │
  └──────────────────────────────────────────┘
  ```
- 一键按钮**默认只勾客户端模组**（未知模组留给用户自行判断），符合用户原话

**关键技术点**：
- Modrinth hash 查询有时返回的 `server_side` 字段是"项目级"而非"版本级"——以项目级为准就好
- 网络失败/超时的模组 → 归入 "unknown"（区分于"搜不到的"，但 UI 不必区分这么细）
- 删除前**移动到 `mods/.disabled/` 子目录**而非直接 `rm` — 给用户后悔药

**预估工作量**：1 个 session

---

### Phase 3 — Modrinth `.mrpack` 整合包导入

**新增文件**：
- `core/modpack_importer.py`：
  - `parse_mrpack(zip_path) -> ModpackManifest`（含版本、loader、mod 列表）
  - `import_modpack(manifest, target_dir, progress_callback) -> CreateServerResult`
- `tests/test_modpack_importer.py`：用预制的小 mrpack 样本测试

**GUI**：
- "创建服务器"向导新增第二种入口："导入整合包"
- 接受文件拖入（Tk 支持 `<<Drop>>` 事件，但 CustomTkinter 上需要小 wrapper）或点"浏览"选文件
- 自动填充版本/loader、显示包含的模组数，确认后调 `import_modpack()`

**预估工作量**：1.5 个 session（拖拽支持有 tk 限制要绕）

---

### Phase 4 — CurseForge 集成（搜索 + 整合包）

**前提决策**：
- 选项 A：用户自己注册 CF key，HMSL 提供"设置"页输入框
- 选项 B：HMSL 内置一个 key，要承担被撤风险
- **推荐 A**：合规、不会突然失效，但增加首次使用门槛

**实施**：
- `core/curseforge_search.py`（结构对应 modrinth_search.py）
- 整合包：`core/modpack_importer.py` 加 `parse_curseforge_zip()` 分支
- GUI 模组搜索面板顶部加 tab：[Modrinth | CurseForge]

**预估工作量**：1.5 个 session

---

### Phase 5 — 其它整合包格式（MultiMC / HMCL）

**优先级最低**——这类整合包通常本来就是给客户端用的，服务端需求小。除非有具体用户提需求，否则暂缓。

---

## 4. 跨期共享的基础设施（先建好不重复造轮子）

### 4.1 通用确认对话框

[gui_main.py](gui_main.py) 已经有 `_show_error`，但只有"知道了"按钮。需要一个 `_confirm_dialog(title, msg, ok_text, cancel_text) -> bool` 给 Phase 1 的客户端模组警告用。

### 4.2 后台任务 + 进度回调

Phase 1-3 都涉及"长任务 + 进度更新"，已有的 `progress_callback(frac, msg)` 模式可继续。但 Phase 2 的"扫描 N 个文件"是迭代式的，可能想要 `(current, total, message)` 三元组而非 `(frac, message)`。考虑给 `core/` 加一个 `ProgressReporter` 类。

### 4.3 网络请求统一封装

目前 [core/mod_downloader.py](core/mod_downloader.py)、`server_installer.py` 都直接调 `requests`。模组功能引入后会有更多 HTTP，建议抽出 `core/http_client.py`：
- 统一超时（10s）
- 统一 User-Agent（"HMSL/0.x"，Modrinth API 礼仪要求标识来源）
- 重试逻辑（最多 2 次，指数退避）
- 离线时优雅降级

---

## 5. 数据流总览（Phase 1 + 2 完成后）

```
[GUI 版本管理页]
    │
    │ 选中服务器
    ▼
[Action Bar]
    ├── ▶ 启动              → core/launcher.py
    ├── 📂 文件夹            → os 跨平台 open
    ├── 🔍 浏览模组(Phase1)   → ModBrowserWindow → core/modrinth_search.py
    │                                            └→ core/mod_downloader.py 下载
    ├── 🧹 扫描模组(Phase2)   → ModScanWindow    → core/mod_scanner.py
    │                                            └→ Modrinth hash API
    ├── ⚙ 编辑配置(Phase 3+) → properties 编辑器（另一个独立大功能）
    └── 🗑 从列表移除         → core/instance_registry.py
```

---

## 6. 优先级建议

| 优先级 | 任务 |
|---|---|
| 🥇 P1 | Phase 1（模组搜索）— 用户最直观需求，且 Modrinth API 已熟 |
| 🥈 P2 | Phase 2（扫描清理）— 用户原话强调，且独立可做 |
| 🥉 P3 | Phase 3（mrpack 导入）— 跟 P1 共享 Modrinth 基础设施 |
| ⭐ P4 | Phase 4（CurseForge）— 等用户提具体需求或 Modrinth 满足不了再做 |
| 🛌 P5 | Phase 5（MultiMC/HMCL 整合包）— 服务端场景需求低 |

**起手建议**：从 **Phase 2（扫描清理）开始**，理由：
- 比 Phase 1 更独立（不需要新 GUI 窗口的复杂搜索体验，只需扫描+表格）
- 价值立刻可见——你现有那个 1.20.4 Forge 实例就有模组可被扫到
- 给后面的 Phase 1 验证了 Modrinth hash API 这条技术线

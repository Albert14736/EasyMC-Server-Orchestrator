# Minecraft 自动化脚本 - 模组与插件数据库

此文件用于记录经过验证的模组（Mods）和插件（Plugins），以便脚本在开服时自动下载。

## 格式规范
| 模组/插件名称 | Modrinth ID | 适用平台 | 功能说明 |
| :--- | :--- | :--- | :--- |
| NeoEssentials | vT6zW0bA | NeoForge | NeoForge 端的全能基础指令与经济系统 |
| Essential Commands | 9s6ENmH9 | Fabric | Fabric 端的全能基础指令 (/home, /tpa等) |
| TAB | 7uU796H9 | Universal | 高度可定制的 TAB 列表和头顶信息 |
| Fabric API | P7dR8mSH | Fabric | 所有 Fabric 模组的基础前置 |
| Lithium | gv9WUmD8 | Fabric | 服务器 TPS 优化 |
| FerriteCore | kl4pbdpt | Fabric | 内存占用优化 |
| Starlight | qm99pY97 | Fabric | 光照引擎优化 (1.20 以前版本必备) |

### 2. 功能类 (Fabric)
*   [待添加]

### 3. 插件类 (Paper/Spigot)
*   [待添加]

---

## 自动化策略记录
1.  **Fabric**: 下载后的 jar 文件应放入 `mods/` 文件夹。
2.  **Paper**: 下载后的 jar 文件应放入 `plugins/` 文件夹。
3.  **版本匹配**: 脚本需要解析此文件，根据用户选定的游戏版本过滤下载链接。

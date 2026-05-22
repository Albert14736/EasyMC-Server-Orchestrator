# Minecraft 自动化脚本 - 模组与插件数据库

此文件用于记录经过验证的模组（Mods）和插件（Plugins），以便脚本在开服时自动下载。

## 格式规范
| 模组/插件名称 | Modrinth ID | 适用平台 | 功能说明 |
| :--- | :--- | :--- | :--- |
| Spark | l6YH9Als | Universal | 实时性能分析与监控 (必备调试工具) |
| Krypton | fQEb0iXm | Fabric | 优化网络协议栈，显著减少掉线 |
| Chunky | fALzjamp | Universal | 离线预生成世界区块，消除跑图卡顿 |
| LuckPerms | Vebnzrzj | Universal | 最强大的权限管理系统 |
| ViaVersion | P1OZGk5p | Paper | 允许更高版本的客户端连接旧版本服务器 |
| Fast Leaf Decay | PcKMtamx | Universal | 砍树后叶子快速消失 |
| No Chat Reports | qQyHxfxd | Universal | 禁用聊天报告系统，保护玩家隐私 |
| TAB | gG7VFbG0 | Universal | 高度可定制的 TAB 列表和头顶信息 |
| Fabric API | P7dR8mSH | Fabric | 所有 Fabric 模组的基础前置 |
| Lithium | gvQqBUqZ | Fabric | 服务器 TPS 优化 |
| FerriteCore | uXXizFIs | Fabric | 内存占用优化 |
| Starlight | H8CaAYZC | Fabric | 光照引擎优化 (1.20 以前版本必备) |
| I'm Fast | im-fast | Universal | 防止因移动过快被踢出服务器 |
| NeoEssentials | yiaK4SZh | NeoForge | NeoForge 端的全能基础指令与经济系统 |
| Essential Commands | 6VdDUivB | Fabric | Fabric 端的全能基础指令 (/home, /tpa等) |

## 已剔除条目（保留记录避免后人重蹈覆辙）
- **EntityCulling** (`NNAgCjsB`): 客户端专属（`client_side=required, server_side=unsupported`），装到服务端无意义。详见 2026-05-23 `audit_mod_database.py` 审计结果。
- **Connectivity** (原标 `8vE9Y066`): 在 Modrinth 上无此项目，疑似仅发布在 CurseForge。等 Phase 4 CF 集成后可重新接入。

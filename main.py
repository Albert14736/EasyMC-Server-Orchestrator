import os
import sys
from core.env_manager import EnvManager
from core.server_installer import ServerInstaller
from core.mod_downloader import ModDownloader

def main():
    env = EnvManager()
    installer = ServerInstaller()
    # 数据库路径锁定在脚本同级目录
    db_path = os.path.join(env.script_dir, "MOD_DATABASE.md")
    downloader = ModDownloader(db_path)

    print("\033[0;34m==========================================")
    print("      Minecraft 一键开服助手 (Python 版)")
    print("==========================================\033[0m")

    # 1. 基础信息交互
    server_name = input("请输入服务器文件夹名称 (默认: mc_server): ").strip() or "mc_server"
    
    print("\n[提示] 本工具仅支持 Minecraft 1.7.10 及以上版本。")
    import re
    while True:
        mc_version = input("请输入 Minecraft 版本 (例如 1.21.1): ").strip() or "1.21.1"
        
        # 基础格式校验: 必须以 1. 开头，且包含至少一个数字段
        if not re.match(r"^1\.\d+(\.\d+)?$", mc_version):
            print("\033[0;31m[错误] 版本格式不正确，请输入类似 1.16.5 或 1.21 的格式。\033[0m")
            continue
            
        # 1.7.10 以下版本判定
        try:
            # 简单的版本号数值比较 (例如 1.7.2 < 1.7.10)
            parts = [int(p) for p in mc_version.split('.')]
            if parts[1] < 7 or (parts[1] == 7 and len(parts) > 2 and parts[2] < 10):
                print("\033[0;31m[错误] 目前仅支持 1.7.10 及以上版本。\033[0m")
                continue
        except:
            print("\033[0;31m[错误] 无法解析该版本号。\033[0m")
            continue
            
        break # 校验通过，跳出循环
    
    # --- 2. 动态生成服务端类型菜单 ---
    def version_ge(v1, v2):
        from distutils.version import LooseVersion
        return LooseVersion(v1) >= LooseVersion(v2)

    import requests
    available_options = []

    # 基础选项 (Forge 始终显示)
    available_options.append(("Forge (传统模组服)", "Forge"))

    # 条件检测：Paper 官方支持列表
    is_paper_supported = False
    if version_ge(mc_version, "1.8.8"):
        try:
            # 静默获取 Paper 支持的版本列表
            paper_versions = requests.get("https://api.papermc.io/v2/projects/paper", timeout=3).json()["versions"]
            if mc_version in paper_versions:
                is_paper_supported = True
        except:
            # 如果网络失败，退回到基础判定
            is_paper_supported = True 

    if is_paper_supported:
        available_options.append(("Paper (高性能插件服)", "Paper"))

    if version_ge(mc_version, "1.14"):
        available_options.append(("Fabric (现代模组服)", "Fabric"))
    
    if version_ge(mc_version, "1.20.2"):
        available_options.append(("NeoForge (新一代模组服)", "NeoForge"))

    print("\n请选择服务端类型:")
    for i, (display_name, _) in enumerate(available_options, 1):
        print(f"{i}) {display_name}")
    
    while True:
        try:
            choice_idx = int(input(f"请选择 (1-{len(available_options)}): ").strip())
            if 1 <= choice_idx <= len(available_options):
                loader_name = available_options[choice_idx - 1][1]
                break
            else:
                print(f"\033[0;31m[错误] 请输入 1 到 {len(available_options)} 之间的数字。\033[0m")
        except ValueError:
            print("\033[0;31m[错误] 请输入有效的数字。\033[0m")

    # 3. 路径处理
    server_path = os.path.join(env.script_dir, server_name)
    if not os.path.exists(server_path):
        os.makedirs(server_path)

    # 提前获取 Java 路径
    java_ver = 21 if mc_version >= "1.20.5" else 17
    java_cmd = env.get_java_cmd(java_ver)

    # 4. 执行服务端安装
    if loader_name == "Paper":
        success = installer.install_paper(server_path, mc_version)
    elif loader_name == "Fabric":
        success = installer.install_fabric(server_path, mc_version)
    elif loader_name == "Forge":
        success = installer.install_forge(server_path, mc_version, java_cmd)
    elif loader_name == "NeoForge":
        success = installer.install_neoforge(server_path, mc_version, java_cmd)
    else:
        print("\033[0;31m[错误] 无效选择。\033[0m")
        return

    if not success:
        print("\033[0;31m[错误] 服务端安装失败，程序终止。\033[0m")
        return

    # 4. 同意 EULA
    with open(os.path.join(server_path, "eula.txt"), "w") as f:
        f.write("eula=true")

    # 5. 同步模组与插件
    # 注意：Forge/NeoForge 的模组同样存放在 mods 文件夹
    target_mod_dir = "plugins" if loader_name == "Paper" else "mods"
    downloader.sync(os.path.join(server_path, target_mod_dir), mc_version, loader_name)

    # 6. 生成启动脚本
    start_script = os.path.join(server_path, "start.sh")
    with open(start_script, "w") as f:
        # Forge 1.17+ 会生成 user_jvm_args.txt 和使用指定的 jar，启动逻辑可能需要微调
        # 这里先保持通用逻辑，对于旧版本 Forge 依然有效
        # 对于新版本 Forge (1.17+)，它通常建议运行 run.sh，我们可以后续做适配
        f.write(f'#!/bin/zsh\n"{java_cmd}" -Xms2G -Xmx4G -jar server.jar nogui')
    
    os.chmod(start_script, 0o755)

    print("\n\033[0;32m==========================================")
    print(f"恭喜！{loader_name} 服务器已准备就绪。")
    print(f"目录: {server_path}")
    print(f"启动: 进入目录运行 ./start.sh")
    print("==========================================\033[0m")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[信息] 用户取消操作。")
        sys.exit(0)

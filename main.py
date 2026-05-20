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
    mc_version = input("请输入 Minecraft 版本 (例如 1.21.1): ").strip() or "1.21.1"
    
    print("\n请选择服务端类型:")
    print("1) Paper (高性能插件服)")
    print("2) Fabric (现代模组服)")
    choice = input("请选择 (1/2): ").strip()

    # 2. 路径处理
    server_path = os.path.join(env.script_dir, server_name)
    if not os.path.exists(server_path):
        os.makedirs(server_path)

    # 3. 执行服务端安装
    if choice == "1":
        loader_name = "Paper"
        success = installer.install_paper(server_path, mc_version)
    else:
        loader_name = "Fabric"
        success = installer.install_fabric(server_path, mc_version)

    if not success:
        print("\033[0;31m[错误] 服务端安装失败，程序终止。\033[0m")
        return

    # 4. 同意 EULA
    with open(os.path.join(server_path, "eula.txt"), "w") as f:
        f.write("eula=true")

    # 5. 同步模组与插件
    target_mod_dir = "plugins" if loader_name == "Paper" else "mods"
    downloader.sync(os.path.join(server_path, target_mod_dir), mc_version, loader_name)

    # 6. 生成启动脚本 (macOS 使用 .sh, Windows 后续支持 .bat)
    java_ver = 21 if mc_version >= "1.20.5" else 17
    java_cmd = env.get_java_cmd(java_ver)
    
    start_script = os.path.join(server_path, "start.sh")
    with open(start_script, "w") as f:
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

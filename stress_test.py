import os
import shutil
from core.env_manager import EnvManager
from core.server_installer import ServerInstaller

def test_install(server_name, version, type_idx):
    env = EnvManager()
    installer = ServerInstaller()
    
    server_path = os.path.join(env.script_dir, server_name)
    if os.path.exists(server_path):
        shutil.rmtree(server_path)
    os.makedirs(server_path)
    
    print(f"\n>>> 测试任务: {version} - {server_name}")
    java_ver = 21 if version >= "1.20.5" else 17
    java_cmd = env.get_java_cmd(java_ver)
    
    success = False
    if type_idx == "Paper":
        success = installer.install_paper(server_path, version)
    elif type_idx == "Fabric":
        success = installer.install_fabric(server_path, version)
    elif type_idx == "Forge":
        success = installer.install_forge(server_path, version, java_cmd)
    elif type_idx == "NeoForge":
        success = installer.install_neoforge(server_path, version, java_cmd)
    
    if success:
        print(f"  [验证] {version} {type_idx} 下载成功！")
        # 检查关键文件
        if os.path.exists(os.path.join(server_path, "server.jar")) or \
           os.path.exists(os.path.join(server_path, "libraries")): # Forge 1.17+ 结构不同
            print("  [验证] 文件结构正确。")
        else:
            print("  [警告] 未找到核心服务端文件。")
    else:
        print(f"  [失败] {version} {type_idx} 下载或安装失败。")
    
    # 立即清理
    shutil.rmtree(server_path)
    return success

# 定义测试矩阵
test_matrix = [
    ("1.7.10", "Forge"),
    ("1.8.8", "Paper"),
    ("1.12.2", "Forge"),
    ("1.16.5", "Fabric"),
    ("1.16.5", "Forge"),
    ("1.20.1", "Fabric"),
    ("1.20.1", "NeoForge"), # 预期报错或需精准版本
    ("1.21.1", "Paper"),
    ("1.21.1", "Fabric")
]

print("=== 开始全自动化压力测试 ===")
results = []
for version, stype in test_matrix:
    res = test_install(f"test_{version}_{stype}", version, stype)
    results.append((version, stype, res))

print("\n=== 测试汇总报告 ===")
for v, t, r in results:
    status = "✅ 成功" if r else "❌ 失败"
    print(f"{v} {t}: {status}")

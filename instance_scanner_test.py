import os
import json

class InstanceScanner:
    def __init__(self, base_dir):
        self.base_dir = base_dir

    def scan(self):
        """扫描基础目录下所有的 Minecraft 服务器实例"""
        instances = []
        exclude = ['.git', '.venv', 'core', '__pycache__', '.vscode']
        
        if not os.path.exists(self.base_dir):
            return []

        for item in os.listdir(self.base_dir):
            item_path = os.path.join(self.base_dir, item)
            
            if os.path.isdir(item_path) and item not in exclude:
                # 更灵活的判定逻辑：
                # 1. 存在 eula.txt (最强信号)
                # 2. 或者 存在任何包含 server/forge/fabric/paper 的 jar 包
                is_server = os.path.exists(os.path.join(item_path, "eula.txt"))
                
                if not is_server:
                    # 扫描所有 .jar 文件
                    for f in os.listdir(item_path):
                        f_lower = f.lower()
                        if f_lower.endswith(".jar") and any(key in f_lower for key in ["server", "forge", "fabric", "paper", "neoforge"]):
                            is_server = True
                            break
                
                if is_server:
                    instance_info = self._get_instance_details(item_path, item)
                    instances.append(instance_info)
        
        return instances

    def _get_instance_details(self, path, name):
        """解析文件夹内的文件，获取版本、类型等细节"""
        details = {
            "name": name,
            "path": path,
            "version": "未知版本",
            "type": "未知类型",
            "eula": False
        }
        
        # 1. 检查 EULA
        eula_path = os.path.join(path, "eula.txt")
        if os.path.exists(eula_path):
            with open(eula_path, 'r') as f:
                if "eula=true" in f.read().lower():
                    details["eula"] = True

        # 2. HMSL 生成的实例会带 start.sh (POSIX) 或 start.bat (Windows)，
        #    用作"由 HMSL 创建"的特征标记
        if (os.path.exists(os.path.join(path, "start.sh"))
                or os.path.exists(os.path.join(path, "start.bat"))):
            details["type"] = "已识别实例"

        return details

# --- 终端测试代码 ---
if __name__ == "__main__":
    # 获取当前脚本所在目录作为基础目录
    base = os.path.dirname(os.path.abspath(__file__))
    scanner = InstanceScanner(base)
    
    print("=== 正在扫描服务器实例 ===")
    found = scanner.scan()
    
    if not found:
        print("未发现任何有效的服务器实例。")
    else:
        print(f"共发现 {len(found)} 个实例：")
        for inst in found:
            print(f"- 名称: {inst['name']}")
            print(f"  路径: {inst['path']}")
            print(f"  EULA: {'✅ 已同意' if inst['eula'] else '❌ 未同意'}")
            print("-" * 20)

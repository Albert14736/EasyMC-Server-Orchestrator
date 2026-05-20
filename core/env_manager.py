import os
import subprocess
import platform

class EnvManager:
    def __init__(self):
        # 锁定项目根目录的绝对路径
        self.script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    def get_java_cmd(self, required_version):
        """获取指定版本的 Java 执行路径"""
        # macOS 专用逻辑
        if platform.system() == "Darwin":
            try:
                cmd = f"/usr/libexec/java_home -v {required_version}"
                path = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
                return os.path.join(path, "bin/java")
            except:
                # 如果 java_home 失败，尝试返回默认 java
                return "java"
        
        # Windows 逻辑 (预留)
        if platform.system() == "Windows":
            # 后续可以增加对注册表或环境变量的检查
            return "java"
            
        return "java"

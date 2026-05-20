import os
import requests
import json

class ModDownloader:
    def __init__(self, db_path):
        self.db_path = db_path
        self.api_base = "https://api.modrinth.com/v2/project"

    def get_download_url(self, project_id, mc_version, loader):
        """调用 Modrinth API 获取符合版本和加载器的最新下载链接"""
        params = {
            "game_versions": json.dumps([mc_version]),
            "loaders": json.dumps([loader.lower()])
        }
        try:
            response = requests.get(f"{self.api_base}/{project_id}/version", params=params)
            if response.status_code == 200:
                data = response.json()
                if data:
                    # 返回第一个符合条件的版本的第一个文件
                    return data[0]['files'][0]['url']
        except Exception as e:
            print(f"  [API 错误] 无法获取 {project_id}: {e}")
        return None

    def sync(self, target_dir, mc_version, selected_loader):
        """解析 MOD_DATABASE.md 并下载匹配的模组/插件"""
        if not os.path.exists(self.db_path):
            print(f"  [警告] 数据库文件不存在: {self.db_path}")
            return

        os.makedirs(target_dir, exist_ok=True)
        print(f"\n--- 正在同步模组与插件 (目标: {target_dir}) ---")

        with open(self.db_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines:
            if '|' not in line or line.startswith('| 名称') or line.startswith('| :---'):
                continue
            
            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 4:
                continue
            
            name = parts[1]
            mod_id = parts[2]
            platform = parts[3]

            # 匹配逻辑: Universal 或者是当前选择的平台
            should_download = False
            if platform == "Universal":
                should_download = True
            elif selected_loader.lower() in platform.lower():
                should_download = True

            if should_download and mod_id:
                print(f"正在检查: {name}...")
                # 为了 API 匹配，将 Paper 映射为 paper 标签
                api_loader = "paper" if selected_loader.lower() == "paper" else "fabric"
                
                url = self.get_download_url(mod_id, mc_version, api_loader)
                if url:
                    print(f"  -> 正在下载 {name}...")
                    try:
                        r = requests.get(url, stream=True)
                        with open(os.path.join(target_dir, f"{name}.jar"), 'wb') as jar:
                            for chunk in r.iter_content(chunk_size=8192):
                                jar.write(chunk)
                        print(f"  [成功] 已安装")
                    except Exception as e:
                        print(f"  [失败] 下载出错: {e}")
                else:
                    print(f"  [跳过] 未找到适用于 {mc_version} 的版本")

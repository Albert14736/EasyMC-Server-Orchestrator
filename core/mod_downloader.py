import os
import requests
import json

class ModDownloader:
    def __init__(self, db_path):
        self.db_path = db_path
        self.api_base = "https://api.modrinth.com/v2/project"

    def get_download_url(self, project_id, mc_version, loader):
        """
        [v2.0 强化版] 
        支持模糊版本匹配 (例如 1.21.1 -> 1.21) 
        支持多加载器兼容 (Fabric -> Quilt)
        """
        # 1. 准备尝试的版本列表
        # 策略：先试原版 (1.21.1)，再试大版本号 (1.21)
        version_list = [mc_version]
        if mc_version.count('.') == 2:
            version_list.append('.'.join(mc_version.split('.')[:2]))

        # 2. 准备尝试的加载器列表
        # 策略：Fabric -> Quilt -> Minecraft (通用型)
        loader_list = [loader.lower()]
        if loader.lower() == "fabric":
            loader_list.append("quilt")
        loader_list.append("minecraft") # 很多 Universal 模组只标记了 minecraft
        
        # 3. 调用 API 获取所有版本数据
        # 尝试多次查询，如果精准查询不到，尝试放宽加载器限制
        try:
            # 第一次：带加载器限制的查询
            params = {
                "game_versions": json.dumps(version_list),
                "loaders": json.dumps(loader_list)
            }
            url = f"{self.api_base}/{project_id}/version"
            response = requests.get(url, params=params, timeout=10)
            
            # 第二次：如果没结果，尝试只按版本查询 (针对某些标签极其不规范的模组)
            if response.status_code != 200 or not response.json():
                params = {"game_versions": json.dumps(version_list)}
                response = requests.get(url, params=params, timeout=10)

            if response.status_code == 200:
                versions_data = response.json()
                if not versions_data:
                    return None
                
                # 4. 寻找最优文件
                # 遍历所有返回的版本 (通常按发布日期排序)
                for version in versions_data:
                    # 过滤掉 alpha/beta (除非没有其他的)
                    if version.get('version_type') not in ['release']:
                        continue
                    
                    # 找到第一个包含文件的 release 版本
                    if version.get('files'):
                        # 优先找带 .jar 后缀且是 primary 的文件
                        for file in version['files']:
                            if file['url'].endswith('.jar'):
                                return file['url']
                
                # 如果没找到 release，退而求其次找第一个可用的
                if versions_data[0].get('files'):
                    return versions_data[0]['files'][0]['url']
                    
        except Exception as e:
            print(f"  [API 错误] {project_id}: {e}")
        return None

    def sync(self, target_dir, mc_version, selected_loader):
        """同步数据库中的模组"""
        if not os.path.exists(self.db_path):
            print(f"  [警告] 数据库文件不存在: {self.db_path}")
            return

        os.makedirs(target_dir, exist_ok=True)
        print(f"\n--- 正在同步模组与插件 (目标: {target_dir}) ---")

        with open(self.db_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        success_count = 0
        total_matched = 0

        for line in lines:
            if '|' not in line or line.startswith('| 名称') or line.startswith('| :---') or line.startswith('#'):
                continue
            
            parts = [p.strip() for p in line.split('|')]
            if len(parts) < 4:
                continue
            
            name = parts[1]
            mod_id = parts[2]
            platform = parts[3]

            if not mod_id: continue

            # 匹配逻辑
            should_download = False
            if platform == "Universal":
                should_download = True
            elif selected_loader.lower() in platform.lower():
                should_download = True

            if should_download:
                total_matched += 1
                print(f"正在同步: {name} ({mod_id})...", end="", flush=True)
                
                # 统一 API 的 loader 标签
                api_loader = "paper" if selected_loader.lower() == "paper" else "fabric"
                if selected_loader.lower() in ["forge", "neoforge"]:
                    api_loader = selected_loader.lower()

                url = self.get_download_url(mod_id, mc_version, api_loader)
                if url:
                    try:
                        r = requests.get(url, stream=True, timeout=15)
                        # 自动纠正文件名，防止空格和非法字符
                        safe_name = name.replace(" ", "_")
                        with open(os.path.join(target_dir, f"{safe_name}.jar"), 'wb') as jar:
                            for chunk in r.iter_content(chunk_size=8192):
                                jar.write(chunk)
                        print(f" [成功]")
                        success_count += 1
                    except Exception as e:
                        print(f" [失败: {e}]")
                else:
                    print(f" [跳过: 未找到兼容版本]")
        
        print(f"\n同步完成: 成功 {success_count} / 匹配 {total_matched}")

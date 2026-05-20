import os
import requests

class ServerInstaller:
    def __init__(self):
        pass

    def install_paper(self, target_path, mc_version):
        """下载 PaperMC 服务端"""
        print(f"正在获取 Paper {mc_version} 构建信息...")
        api_url = f"https://api.papermc.io/v2/projects/paper/versions/{mc_version}"
        try:
            r = requests.get(api_url)
            if r.status_code != 200:
                print("  [错误] 无法找到该版本的 Paper。")
                return False
            
            latest_build = r.json()['builds'][-1]
            jar_name = f"paper-{mc_version}-{latest_build}.jar"
            dl_url = f"{api_url}/builds/{latest_build}/downloads/{jar_name}"
            
            return self._download(dl_url, target_path)
        except Exception as e:
            print(f"  [错误] Paper 下载流程异常: {e}")
            return False

    def install_fabric(self, target_path, mc_version):
        """下载 Fabric 服务端"""
        print(f"正在获取 Fabric {mc_version} 版本信息...")
        try:
            # 获取最新 loader 和 installer
            loader_r = requests.get("https://meta.fabricmc.net/v2/versions/loader")
            inst_r = requests.get("https://meta.fabricmc.net/v2/versions/installer")
            
            loader_ver = loader_r.json()[0]['version']
            inst_ver = inst_r.json()[0]['version']
            
            dl_url = f"https://meta.fabricmc.net/v2/versions/loader/{mc_version}/{loader_ver}/{inst_ver}/server/jar"
            return self._download(dl_url, target_path)
        except Exception as e:
            print(f"  [错误] Fabric 下载流程异常: {e}")
            return False

    def _download(self, url, target_path):
        """通用下载逻辑"""
        print(f"正在从 {url} 下载...")
        try:
            r = requests.get(url, stream=True)
            with open(os.path.join(target_path, "server.jar"), 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            print("  [成功] server.jar 已就绪。")
            return True
        except Exception as e:
            print(f"  [错误] 下载失败: {e}")
            return False

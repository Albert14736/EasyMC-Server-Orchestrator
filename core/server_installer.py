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

    def install_forge(self, target_path, mc_version, java_cmd="java"):
        """下载并自动安装 Forge 服务端"""
        print(f"正在获取 Forge {mc_version} 列表...")
        try:
            # 1. 获取 Forge 版本列表
            promo_url = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
            promos = requests.get(promo_url).json()
            
            # 优先选择推荐版，没有则选最新版
            forge_ver = promos['promos'].get(f"{mc_version}-recommended") or promos['promos'].get(f"{mc_version}-latest")
            if not forge_ver:
                print(f"  [错误] 未找到适用于 {mc_version} 的 Forge 版本。")
                return False
            
            # 尝试下载 Forge 安装器
            # 第一尝试：标准格式 (mc_version-forge_ver)
            full_version_std = f"{mc_version}-{forge_ver}"
            dl_url_std = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full_version_std}/forge-{full_version_std}-installer.jar"
            
            print(f"正在尝试标准下载链接...")
            if self._download(dl_url_std, target_path, "forge-installer.jar"):
                success = True
            else:
                # 第二尝试：旧版格式 (mc_version-forge_ver-mc_version) - 常见于 1.7.10
                print(f"标准链接失效，正在尝试旧版兼容链接...")
                full_version_legacy = f"{mc_version}-{forge_ver}-{mc_version}"
                dl_url_legacy = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{full_version_legacy}/forge-{full_version_legacy}-installer.jar"
                if self._download(dl_url_legacy, target_path, "forge-installer.jar"):
                    success = True
                else:
                    print(f"  [错误] 无法找到有效的 Forge 下载链接。")
                    return False
            
            # 3. 运行安装程序
            if success:
                print("正在执行 Forge 静默安装 (这可能需要几分钟，请耐心等待...)...")
            import subprocess
            result = subprocess.run(
                [java_cmd, "-jar", "forge-installer.jar", "--installServer"],
                cwd=target_path,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print("  [成功] Forge 服务端环境已安装。")
                # 4. 清理垃圾
                self._cleanup(target_path, "forge-installer.jar")
                return True
            else:
                print(f"  [错误] 安装过程出错。")
                # 打印部分错误日志供参考
                if "java.lang.UnsupportedClassVersionError" in result.stderr:
                    print("  -> 提示：Java 版本不匹配，请检查版本选择。")
                return False
                
        except Exception as e:
            print(f"  [错误] Forge 下载/安装异常: {e}")
            return False

    def install_neoforge(self, target_path, mc_version, java_cmd="java"):
        """下载并自动安装 NeoForge 服务端 (针对 1.20.1+)"""
        print(f"正在获取 NeoForge {mc_version} 版本信息...")
        try:
            # NeoForge 使用 Maven 结构，我们可以尝试猜测或从其 API 获取
            # 简化版：直接从其官方 Maven 获取最新构建（NeoForge 版本号通常就是 mc版本.构建号）
            # 例如 1.21.1 对应 21.1.x
            # 这里我们使用一个简单的探测逻辑
            major_ver = mc_version.split('.')[1] if '.' in mc_version else "21"
            # 注意：此逻辑在 NeoForge 官网有更复杂的 API，这里先做一个基础适配
            api_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
            # 实际生产中建议解析 XML 找到最匹配 mc_version 的版本
            # 为简化测试，我们先提示用户此功能需要更精确的 API 接入
            print("  [提示] NeoForge 下载目前需要精准匹配版本号，正在尝试自动匹配...")
            
            # 这里先以 1.21.1 常用版本为例
            if mc_version == "1.21.1":
                full_version = "21.1.53" # 示例版本
            else:
                print(f"  [错误] 目前 NeoForge 自动安装仅适配了特定测试版本，请手动确认。")
                return False

            dl_url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{full_version}/neoforge-{full_version}-installer.jar"
            
            if not self._download(dl_url, target_path, "neoforge-installer.jar"):
                return False
            
            print("正在执行 NeoForge 静默安装...")
            import subprocess
            result = subprocess.run(
                [java_cmd, "-jar", "neoforge-installer.jar", "--installServer"],
                cwd=target_path,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                print("  [成功] NeoForge 服务端已就绪。")
                self._cleanup(target_path, "neoforge-installer.jar")
                return True
            else:
                print(f"  [错误] NeoForge 安装失败。")
                return False

        except Exception as e:
            print(f"  [错误] NeoForge 下载/安装异常: {e}")
            return False

    def _cleanup(self, target_path, installer_name):
        """清理安装器和日志"""
        try:
            os.remove(os.path.join(target_path, installer_name))
            log_name = installer_name + ".log"
            if os.path.exists(os.path.join(target_path, log_name)):
                os.remove(os.path.join(target_path, log_name))
            # 移除 run.bat / run.sh (Forge 会生成，但我们会自己生成 start.sh)
            # 这样保持启动逻辑统一
        except:
            pass

    def _download(self, url, target_path, filename="server.jar"):
        """通用下载逻辑"""
        print(f"正在下载 {filename}...")
        try:
            r = requests.get(url, stream=True)
            if r.status_code != 200:
                print(f"  [错误] 下载请求失败 (HTTP {r.status_code})")
                return False
            
            with open(os.path.join(target_path, filename), 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"  [成功] {filename} 下载完成。")
            return True
        except Exception as e:
            print(f"  [错误] 下载过程中断: {e}")
            return False

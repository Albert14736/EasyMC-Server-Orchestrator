"""
MultiMC / Prism Launcher `.zip` modpack provider.

Format: ZIP containing mmc-pack.json (component list) + instance.cfg
(properties file) at root or inside ONE top-level "instance" folder, and the
actual game/mod content under the instance's game directory next to them:
`.minecraft/` (MultiMC) or `minecraft/` (Prism Launcher's default).

MultiMC packs are SELF-CONTAINED — all mods live inside the zip under
<game dir>/mods/ . There's no separate file download step, so no API key
needed and no network for the install. Just parse + extract.
"""
from __future__ import annotations

import zipfile
from typing import Callable, List, Optional, Tuple

from .base import (
    ImportProgress,
    ImportResult,
    ModpackManifest,
    ModpackProvider,
    archive_fingerprint,
    create_server_for_pack,
    decode_text,
    find_zip_entry,
    load_json_object,
    open_zip,
    read_zip_json,
    zip_entries,
)
from .modrinth import _ModrinthSides, _extract_overrides

_MMC_PACK = "mmc-pack.json"
_INSTANCE_CFG = "instance.cfg"
_GAME_DIRS = (".minecraft", "minecraft")

# MultiMC component uid → server_factory loader name
_UID_LOADER_MAP = {
    "net.minecraftforge":             "Forge",
    "net.neoforged":                  "NeoForge",
    "net.fabricmc.fabric-loader":     "Fabric",
    "org.quiltmc.quilt-loader":       "Fabric",  # Quilt API-compat with Fabric
}
_UID_QUILT = "org.quiltmc.quilt-loader"
_UID_MINECRAFT = "net.minecraft"

_QUILT_WARNING = ("该整合包使用 Quilt 加载器，HMSL 会用 Fabric 服务端运行它；"
                  "只支持 Quilt 的模组可能无法加载。")


class MultiMCProvider(ModpackProvider):
    name = "multimc"

    # ---------- detect ----------

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                # mmc-pack.json may be at root OR inside a single top-level folder
                # (MultiMC's "export instance" puts everything under <InstanceName>/).
                # Deeper copies (e.g. a CF pack's overrides/config/…/mmc-pack.json)
                # don't count.
                return _locate_instance(zf) is not None
        except (zipfile.BadZipFile, OSError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with open_zip(archive_path) as zf:
            inst_dir = _locate_instance(zf)
            if inst_dir is None:
                raise ValueError(f"{_MMC_PACK} 不存在于该 zip 中")
            data = read_zip_json(zf, inst_dir + _MMC_PACK, _MMC_PACK)

            instance_name = ""
            cfg_info = find_zip_entry(zf, inst_dir + _INSTANCE_CFG)
            if cfg_info is not None:
                try:
                    raw = zf.read(cfg_info)
                    try:
                        cfg_text = decode_text(raw)
                    except UnicodeDecodeError:
                        cfg_text = raw.decode("utf-8", errors="replace")
                    instance_name = _read_instance_name(cfg_text)
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
                    pass

            # Validate BEFORE anything gets created on disk: an instance
            # without a game directory has nothing for us to install.
            if _game_dir_prefix(zf, inst_dir) is None:
                raise ValueError("未找到实例的游戏目录（.minecraft/ 或 minecraft/），"
                                 "这个压缩包里没有可导入的内容")

        mc_version, loader, loader_version, is_quilt = _components_to_loader(
            data.get("components", []))
        warnings: List[str] = []
        if is_quilt:
            loader_version = None
            warnings.append(_QUILT_WARNING)

        # MultiMC packs don't list individual files in the manifest; everything
        # is extracted from the game dir. So manifest.files stays empty.
        return ModpackManifest(
            format="multimc",
            name=instance_name or "MultiMC Modpack",
            version="",  # MultiMC has no version field
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary="",
            files=[],
            warnings=warnings,
            source=archive_fingerprint(archive_path),
        )

    # ---------- apply ----------

    def apply(
        self,
        archive_path: str,
        server_name: str,
        parent_dir: str,
        env_manager,
        installer,
        downloader,
        progress_callback: Optional[Callable[[ImportProgress], None]] = None,
        *,
        manifest: Optional[ModpackManifest] = None,
    ) -> ImportResult:
        def report(stage, msg, current=0, total=0):
            if progress_callback:
                progress_callback(ImportProgress(stage=stage, message=msg,
                                                 current=current, total=total))

        report("parsing", "正在读取 mmc-pack.json…")
        try:
            manifest = self.prepared_manifest(archive_path, manifest)
            # MultiMC packs put game content under .minecraft/ (Prism: minecraft/),
            # sometimes inside an instance-name top-level folder, e.g.
            # "MyPack/.minecraft/". Find it before creating anything.
            prefix = _find_minecraft_prefix(archive_path)
        except ValueError as e:
            return ImportResult(False, "", str(e))
        if not prefix:
            return ImportResult(False, "", "未找到实例的游戏目录（.minecraft/ 或 minecraft/）")
        warnings: List[str] = list(manifest.warnings)

        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr = create_server_for_pack(manifest, server_name, parent_dir, env_manager,
                                    installer, downloader, warnings, report)
        if not cr.success:
            return ImportResult(False, cr.server_path or "",
                                f"创建服务端失败：{cr.error}", manifest=manifest,
                                warnings=warnings)
        server_path = cr.server_path

        report("applying_overrides", f"正在解压 {prefix}…")
        sides = _ModrinthSides()
        st = _extract_overrides(archive_path, server_path, prefix, warnings=warnings,
                                progress_callback=progress_callback, sides=sides)
        if sides.offline:
            warnings.append("无法连接 Modrinth，未能检查整合包自带的模组是否为纯客户端模组；"
                            "如果服务器启动报错，可在「模组扫描」里再检查一次。")

        report("done", "整合包导入完成")
        return ImportResult(
            success=True,
            server_path=server_path,
            manifest=manifest,
            files_installed=st.mods_installed,
            files_skipped_client=st.mods_skipped + st.client_skipped,
            files_failed=st.failed,
            warnings=warnings,
        )


# ---------- helpers ----------

def _locate_instance(zf: zipfile.ZipFile) -> Optional[str]:
    """
    Directory prefix ("" or "<Top>/") holding the instance's mmc-pack.json,
    looking only at the zip root and one top-level folder. None if absent.
    """
    candidates = []
    for n, _info in zip_entries(zf):
        if n == _MMC_PACK:
            candidates.append("")
        elif n.endswith("/" + _MMC_PACK) and n.count("/") == 1:
            candidates.append(n[:-len(_MMC_PACK)])
    # Prefer the root, then the first top-level folder
    candidates.sort(key=lambda d: d != "")
    for d in candidates:
        # A real instance also has instance.cfg (or at least a components list)
        if find_zip_entry(zf, d + _INSTANCE_CFG) is not None:
            return d
        try:
            data = load_json_object(zf.read(find_zip_entry(zf, d + _MMC_PACK)), _MMC_PACK)
        except (ValueError, zipfile.BadZipFile, OSError, RuntimeError, EOFError):
            continue
        if isinstance(data.get("components"), list):
            return d
    return None


def _game_dir_prefix(zf: zipfile.ZipFile, inst_dir: str) -> Optional[str]:
    """'<inst>/.minecraft/' or '<inst>/minecraft/' — whichever holds the content."""
    counts = {g: 0 for g in _GAME_DIRS}
    for n, _info in zip_entries(zf):
        for g in _GAME_DIRS:
            p = inst_dir + g + "/"
            if n.startswith(p) and len(n) > len(p):
                counts[g] += 1
    # Both present (rare) → the one with more files; tie → .minecraft (MultiMC)
    best = max(_GAME_DIRS, key=lambda g: (counts[g], g == ".minecraft"))
    return inst_dir + best + "/" if counts[best] else None


def _find_in_zip(zf: zipfile.ZipFile, basename: str) -> Optional[str]:
    """Return the shallowest full zip-entry path whose basename matches, or None."""
    hits = [n for n, _i in zip_entries(zf) if n == basename or n.endswith("/" + basename)]
    return min(hits, key=lambda n: n.count("/")) if hits else None


def _read_instance_name(cfg_text: str) -> str:
    """
    Read `name` from MultiMC's INI-style instance.cfg. Classic MultiMC writes
    no section header; Prism Launcher writes everything under [General].
    """
    found = {}
    section = ""
    for line in cfg_text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if "=" not in s:
            continue
        key, value = s.split("=", 1)
        if key.strip().lower() == "name" and section not in found:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            found[section] = value.strip()
    for sec in ("general", ""):
        if found.get(sec):
            return found[sec]
    return next((v for v in found.values() if v), "")


def _components_to_loader(components: list) -> Tuple[str, str, Optional[str], bool]:
    """
    From MultiMC's components list, return (mc_version, loader, loader_version, is_quilt).
    Falls back to ("", "Paper", None, False) when no Minecraft component is present.
    """
    mc_version = ""
    loader = "Paper"
    loader_version: Optional[str] = None
    is_quilt = False
    if not isinstance(components, list):
        return mc_version, loader, loader_version, is_quilt
    for c in components:
        if not isinstance(c, dict):
            continue
        uid = c.get("uid", "")
        ver = c.get("version", "")
        if not isinstance(uid, str):
            continue
        if uid == _UID_MINECRAFT:
            mc_version = str(ver or "")
        elif uid in _UID_LOADER_MAP:
            loader = _UID_LOADER_MAP[uid]
            loader_version = str(ver) if ver else None
            is_quilt = uid == _UID_QUILT
    return mc_version, loader, loader_version, is_quilt


def _find_minecraft_prefix(archive_path: str) -> Optional[str]:
    """
    Locate the instance's game directory inside the zip. MultiMC's export wraps
    the instance in <InstanceName>/ and uses .minecraft/; Prism Launcher uses
    minecraft/ (no dot). So the game dir might be at:
      .minecraft/   minecraft/
      MyPack/.minecraft/   MyPack/minecraft/
    Return the prefix (ending in '/'), or None.
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            inst_dir = _locate_instance(zf)
            if inst_dir is None:
                return None
            return _game_dir_prefix(zf, inst_dir)
    except (zipfile.BadZipFile, OSError):
        return None

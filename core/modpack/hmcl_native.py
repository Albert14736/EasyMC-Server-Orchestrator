"""
HMCL Native `.zip` modpack provider.

HMCL's own export format: a ZIP containing `modpack.json` (name/author/
gameVersion/etc) and `minecraft/pack.json` — the exported client version
JSON (`jar` = mc_version). Game content lives under the zip's `minecraft/`
subdir, which gets installed as the server root.

The loader is recorded in minecraft/pack.json: HMCL's `patches[]` (ids
fabric / forge / neoforge / quilt with their versions), or failing that the
loader libraries (net.fabricmc:fabric-loader, net.minecraftforge:forge,
net.neoforged:neoforge …), launch arguments and mainClass. Only when none of
those says anything do we look inside the bundled mod jars.

Self-contained: no separate file downloads.
"""
from __future__ import annotations

import io
import zipfile
from typing import Callable, List, Optional, Tuple

from .base import (
    ImportProgress,
    ImportResult,
    ModpackManifest,
    ModpackProvider,
    archive_fingerprint,
    create_server_for_pack,
    find_zip_entry,
    open_zip,
    read_zip_json,
    zip_entries,
)
from .modrinth import _ModrinthSides, _extract_overrides

_MODPACK_JSON = "modpack.json"
_PACK_JSON = "minecraft/pack.json"
_CONTENT_PREFIX = "minecraft/"
# Client-launcher files inside minecraft/ that must not land in the server root.
_CLIENT_LAUNCHER_FILES = ("pack.json", "versions/")

_QUILT_WARNING = ("该整合包使用 Quilt 加载器，HMSL 会用 Fabric 服务端运行它；"
                  "只支持 Quilt 的模组可能无法加载。")

# HMCL patch ids / loader names → (server_factory loader, is_quilt)
_PATCH_LOADERS = {
    "neoforge": ("NeoForge", False),
    "forge":    ("Forge", False),
    "fabric":   ("Fabric", False),
    "quilt":    ("Fabric", True),
}


class HMCLNativeProvider(ModpackProvider):
    name = "hmcl_native"

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                return (find_zip_entry(zf, _MODPACK_JSON) is not None
                        and find_zip_entry(zf, _PACK_JSON) is not None)
        except (zipfile.BadZipFile, OSError):
            return False

    def parse(self, archive_path: str) -> ModpackManifest:
        with open_zip(archive_path) as zf:
            meta = read_zip_json(zf, _MODPACK_JSON)
            pack = read_zip_json(zf, _PACK_JSON)

            mc_version = _mc_version_from(pack, meta)
            loader, loader_version, is_quilt, guessed = _loader_from_pack(pack, meta)
            if loader is None:
                loader, is_quilt, guessed = _guess_loader_from_jars(zf)
                loader_version = None

        warnings: List[str] = []
        if is_quilt:
            loader_version = None
            warnings.append(_QUILT_WARNING)
        if guessed:
            warnings.append(f"整合包没有写明模组加载器，HMSL 按模组文件推测为 {loader}；"
                            f"如果服务器无法启动，请换成正确的加载器重新创建。")

        return ModpackManifest(
            format="hmcl_native",
            name=str(meta.get("name", "HMCL Modpack")),
            version=str(meta.get("version", "")),
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary=str(meta.get("description", meta.get("author", ""))),
            files=[],
            warnings=warnings,
            source=archive_fingerprint(archive_path),
        )

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

        report("parsing", "正在读取 modpack.json…")
        try:
            manifest = self.prepared_manifest(archive_path, manifest)
        except ValueError as e:
            return ImportResult(False, "", str(e))
        warnings: List[str] = list(manifest.warnings)

        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr = create_server_for_pack(manifest, server_name, parent_dir, env_manager,
                                    installer, downloader, warnings, report)
        if not cr.success:
            return ImportResult(False, cr.server_path or "",
                                f"创建服务端失败：{cr.error}", manifest=manifest,
                                warnings=warnings)
        server_path = cr.server_path

        report("applying_overrides", f"正在解压 {_CONTENT_PREFIX}…")
        sides = _ModrinthSides()
        st = _extract_overrides(archive_path, server_path, _CONTENT_PREFIX,
                                warnings=warnings, progress_callback=progress_callback,
                                sides=sides, exclude=_CLIENT_LAUNCHER_FILES)
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

def _mc_version_from(pack: dict, meta: dict) -> str:
    """pack.json jar → inheritsFrom → patches[game] → modpack.json gameVersion."""
    for key in ("jar", "inheritsFrom"):
        v = pack.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    patches = pack.get("patches")
    for p in patches if isinstance(patches, list) else []:
        if isinstance(p, dict) and str(p.get("id", "")).lower() in ("game", "minecraft"):
            v = p.get("version")
            if isinstance(v, str) and v.strip():
                return v.strip()
    v = meta.get("gameVersion")
    return v.strip() if isinstance(v, str) else ""


def _loader_from_pack(pack: dict, meta: dict) -> Tuple[Optional[str], Optional[str], bool, bool]:
    """
    (loader, loader_version, is_quilt, guessed) from HMCL's exported version
    JSON; loader None when the JSON says nothing about a loader.
    """
    # 1. HMCL patches[] — the authoritative record
    patches = pack.get("patches")
    for p in patches if isinstance(patches, list) else []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id", "")).lower()
        if pid in _PATCH_LOADERS:
            loader, quilt = _PATCH_LOADERS[pid]
            ver = p.get("version")
            return loader, (str(ver) if ver else None), quilt, False

    # 2. modpack.json addons[] (same shape as HMCL server manifests), if present
    addons = meta.get("addons")
    for a in addons if isinstance(addons, list) else []:
        if isinstance(a, dict) and str(a.get("id", "")).lower() in _PATCH_LOADERS:
            loader, quilt = _PATCH_LOADERS[str(a["id"]).lower()]
            ver = a.get("version")
            return loader, (str(ver) if ver else None), quilt, False

    # 3. loader libraries
    found = {}
    libs = pack.get("libraries")
    for lib in libs if isinstance(libs, list) else []:
        name = lib.get("name") if isinstance(lib, dict) else None
        if not isinstance(name, str):
            continue
        parts = name.split(":")
        if len(parts) < 3:
            continue
        group, artifact, ver = parts[0], parts[1], parts[2]
        if group == "org.quiltmc" and artifact == "quilt-loader":
            found.setdefault("quilt", ver)
        elif group == "net.fabricmc" and artifact == "fabric-loader":
            found.setdefault("fabric", ver)
        elif group == "net.neoforged" and artifact in ("neoforge", "forge"):
            # NeoForge for 1.20.1 still used the artifact name "forge"
            found.setdefault("neoforge", ver)
        elif group == "net.minecraftforge" and artifact in ("forge", "fmlloader", "minecraftforge"):
            found.setdefault("forge", ver)
    for key in ("quilt", "fabric", "neoforge", "forge"):
        if key in found:
            loader, quilt = _PATCH_LOADERS[key]
            return loader, found[key] or None, quilt, False

    # 4. launch arguments (--fml.neoForgeVersion / --fml.forgeVersion) and mainClass
    args: List[str] = []
    arguments = pack.get("arguments")
    game_args = arguments.get("game") if isinstance(arguments, dict) else None
    if isinstance(game_args, list):
        args = [a for a in game_args if isinstance(a, str)]
    legacy = pack.get("minecraftArguments")
    if isinstance(legacy, str):
        args += legacy.split()
    for flag, key in (("--fml.neoForgeVersion", "neoforge"), ("--fml.forgeVersion", "forge")):
        if flag in args:
            i = args.index(flag)
            ver = args[i + 1] if i + 1 < len(args) else None
            loader, quilt = _PATCH_LOADERS[key]
            return loader, ver, quilt, False
    main = str(pack.get("mainClass", "") or "")
    if main.startswith("org.quiltmc."):
        return "Fabric", None, True, False
    if main.startswith("net.fabricmc."):
        return "Fabric", None, False, False
    if main.startswith(("cpw.mods.", "net.minecraftforge.")) or \
            any("FMLTweaker" in a for a in args):
        return "Forge", None, False, False
    return None, None, False, False


def _guess_loader_from_jars(zf: zipfile.ZipFile, limit: int = 12) -> Tuple[str, bool, bool]:
    """
    Last resort when pack.json says nothing: look at the bundled mod jars'
    metadata files. Returns (loader, is_quilt, guessed). No mods → Paper.
    """
    jars = [info for n, info in zip_entries(zf)
            if n.startswith(_CONTENT_PREFIX + "mods/") and n.lower().endswith(".jar")]
    if not jars:
        return "Paper", False, False
    votes = {"fabric": 0, "quilt": 0, "neoforge": 0, "forge": 0}
    for info in jars[:limit]:
        try:
            with zipfile.ZipFile(io.BytesIO(zf.read(info))) as jar:
                names = set(jar.namelist())
        except Exception:
            continue
        if "fabric.mod.json" in names:
            votes["fabric"] += 1
        elif "quilt.mod.json" in names:
            votes["quilt"] += 1
        elif "META-INF/neoforge.mods.toml" in names:
            votes["neoforge"] += 1
        elif "META-INF/mods.toml" in names or "mcmod.info" in names:
            votes["forge"] += 1
    best = max(votes, key=lambda k: votes[k])
    if not votes[best]:
        return "Forge", False, True   # has mods/ but nothing recognisable: most common choice
    loader, quilt = _PATCH_LOADERS[best]
    return loader, quilt, True


def _guess_loader_from_zip(zf: zipfile.ZipFile, mc_version: str) -> Tuple[str, Optional[str]]:
    """Kept for old callers: (loader, None) guessed from the bundled mod jars."""
    loader, _quilt, _guessed = _guess_loader_from_jars(zf)
    return loader, None

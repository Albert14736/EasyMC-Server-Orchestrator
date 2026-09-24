"""
Provider ABC + shared dataclasses for the modpack import subsystem.

A ModpackProvider does three things:
  detect(path)      -> bool                          # is this my format?
  parse(path)       -> ModpackManifest                # extract metadata
  apply(path, ...)  -> ImportResult                   # create server, install files

GUI/CLI call into the package-level import_modpack(); they don't touch
providers directly. This keeps the surface stable as we add more providers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple


# Anything under these prefixes is 100% client-only by definition, no
# matter what the manifest says (or fails to say).
_CLIENT_ONLY_PATH_PREFIXES = (
    "resourcepacks/",
    "shaderpacks/",
    "texturepacks/",
)


def is_client_only_path(rel_path: str) -> bool:
    """True for resourcepacks/ shaderpacks/ texturepacks/ (case-insensitive, either slash)."""
    p = rel_path.lower().replace("\\", "/")
    return any(p.startswith(prefix) for prefix in _CLIENT_ONLY_PATH_PREFIXES)


# Launch files HMSL generates in the server root (server_factory.write_launch_files).
# A pack that ships its own copies must not overwrite them: start_script_state()
# would then report "manually edited" and HMSL would run the pack's script, which
# usually calls whatever `java` is on PATH instead of the Java HMSL picked.
_HMSL_LAUNCH_FILES = ("start.bat", "start.sh", "hmsl_launch.json")


def hmsl_launch_file(rel_path: str) -> Optional[str]:
    """If rel_path (relative to the server root) is one of HMSL's own launch files
    at the root — start.bat / start.sh / hmsl_launch.json, case-insensitive (Windows
    and macOS file systems are) — return its canonical name, else None."""
    p = (rel_path or "").replace("\\", "/").lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lower()
    return p if p in _HMSL_LAUNCH_FILES else None


def launch_files_skipped_warning(names) -> str:
    """整合包自带的 start.bat 已跳过（HMSL 会生成自己的启动脚本）"""
    names = sorted(set(names), key=_HMSL_LAUNCH_FILES.index)
    what = "启动脚本和启动配置" if "hmsl_launch.json" in names else "启动脚本"
    return f"整合包自带的 {'、'.join(names)} 已跳过（HMSL 会生成自己的{what}）"


_LAUNCH_WARNING_RE = re.compile(r"^整合包自带的 (\S+) 已跳过（HMSL 会生成自己的启动脚本")


def note_launch_files_skipped(warnings: Optional[List[str]], names) -> None:
    """Add (or extend) the single "pack's start.bat was skipped" warning: overrides/,
    server-overrides/ and the download list may each skip some of these files."""
    names = {n for n in names if n}
    if not names or warnings is None:
        return
    for i, w in enumerate(warnings):
        m = _LAUNCH_WARNING_RE.match(w) if isinstance(w, str) else None
        if m:
            earlier = set(m.group(1).split("、")) & set(_HMSL_LAUNCH_FILES)
            warnings[i] = launch_files_skipped_warning(names | earlier)
            return
    warnings.append(launch_files_skipped_warning(names))


def without_launch_files(files: List["ModpackFile"], warnings: List[str]) -> List["ModpackFile"]:
    """Drop manifest download entries that would land on HMSL's launch files
    (see hmsl_launch_file) and say so in `warnings`."""
    hits = [hmsl_launch_file(f.path) for f in files]
    note_launch_files_skipped(warnings, hits)
    return [f for f, h in zip(files, hits) if not h]


@dataclass
class ModpackFile:
    """One file inside the modpack — mod jar, config, resource, etc."""
    path: str                  # relative to server root, e.g. "mods/sodium.jar"
    sha1: Optional[str] = None
    sha512: Optional[str] = None
    download_urls: List[str] = field(default_factory=list)
    file_size: Optional[int] = None
    # Modrinth env semantics: "required" / "optional" / "unsupported" / None.
    # None means the modpack manifest didn't fill this in — we'll try a hash
    # lookup to find the real values during enrich_compat().
    env_client: Optional[str] = None
    env_server: Optional[str] = None

    def is_server_skipped(self) -> bool:
        """
        True if this file should NOT be installed on a server.

        Three layers, in order of confidence:
        1. Path-based: resource/shader/texture packs are *definitively* client.
        2. server=unsupported (Modrinth flagged the project as no-server).
        3. server=optional + client=required: server gracefully skips it but
           client needs it ⇒ shipping to server has no value.

        Aligned with core.mod_scanner.classify_mod so behavior is consistent
        between modpack import and post-install scanning.
        """
        if is_client_only_path(self.path):
            return True
        if self.env_server == "unsupported":
            return True
        if self.env_server == "optional" and self.env_client == "required":
            return True
        return False

    def needs_compat_lookup(self) -> bool:
        """
        True when we should second-guess this file's env via Modrinth project
        metadata. Two cases qualify:

        1. env is unset (older or community packs often omit it).
        2. env says 'required/required' — this is mrpack tooling's lazy default
           and frequently masks a client-only mod (Sodium/Iris/Catalogue etc.)
           when the author didn't bother classifying. Modrinth's project-level
           server_side=unsupported is the authoritative override.

        Restricted to mods/ jars with a sha1 (the only thing we can look up).
        """
        in_mods = self.path.lower().replace("\\", "/").startswith("mods/")
        if not in_mods or not self.sha1:
            return False
        if self.env_server is None:
            return True
        if self.env_server == "required" and self.env_client == "required":
            return True
        return False


@dataclass
class ModpackManifest:
    """Format-agnostic, GUI-friendly view of an integrated modpack."""
    format: str                # "modrinth" / "curseforge" / "multimc" / ...
    name: str
    version: str
    mc_version: str
    loader: str                # "Forge" / "Fabric" / "NeoForge" / "Paper" / "Vanilla"
    loader_version: Optional[str] = None
    summary: str = ""
    files: List[ModpackFile] = field(default_factory=list)
    # Things noticed while parsing that the user should know about (e.g.
    # "Quilt runs on a Fabric server"). apply() copies them into
    # ImportResult.warnings.
    warnings: List[str] = field(default_factory=list)
    # archive_fingerprint() of the file parse() read. Lets apply() reuse a
    # manifest the GUI already parsed (and enriched) for the preview, but only
    # while the archive on disk is still that same file.
    source: str = field(default="", repr=False, compare=False)
    # True once enrich_compat() got answers from Modrinth, so apply() on the
    # same manifest doesn't ask again.
    compat_checked: bool = field(default=False, repr=False, compare=False)

    @property
    def server_files(self) -> List[ModpackFile]:
        return [f for f in self.files if not f.is_server_skipped()]

    @property
    def skipped_client_files(self) -> List[ModpackFile]:
        return [f for f in self.files if f.is_server_skipped()]


@dataclass
class ImportProgress:
    """Streamed during apply() so the GUI can render progress."""
    stage: str                 # "parsing" / "creating_server" / "downloading_files" /
                               # "checking_compat" / "applying_overrides" / "done"
    message: str               # human-readable detail
    current: int = 0           # for downloads: current file index
    total: int = 0


@dataclass
class ImportResult:
    success: bool
    server_path: str
    error: Optional[str] = None
    manifest: Optional[ModpackManifest] = None
    files_installed: int = 0
    files_skipped_client: int = 0
    files_failed: int = 0
    # CurseForge mods whose authors opted out of the public API but which we
    # downloaded via the public forgecdn CDN as a fallback (same approach
    # HMCL/PrismLauncher use). Each entry: {"name": ..., "cf_url": ...}.
    # Surfaced to the GUI so the user knows this happened and can support
    # the author at the listed CF page if they want.
    bypassed_mods: List[dict] = field(default_factory=list)
    # Human-readable (Chinese) notes about a partial import: files that could
    # not be downloaded or written on this OS, missing CF API key, loader
    # version fallback, ... success stays True as long as the server exists.
    warnings: List[str] = field(default_factory=list)


class ModpackProvider(ABC):
    """Subclasses register themselves in core/modpack/__init__.py PROVIDERS."""

    name: str = "unknown"   # short id used in ModpackManifest.format

    @abstractmethod
    def detect(self, archive_path: str) -> bool: ...

    @abstractmethod
    def parse(self, archive_path: str) -> ModpackManifest: ...

    @abstractmethod
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
    ) -> ImportResult: ...

    def prepared_manifest(self, archive_path: str,
                          manifest: Optional[ModpackManifest] = None) -> ModpackManifest:
        """
        The manifest apply() should import with: `manifest` if the caller (the
        GUI preview) already got it from this provider's parse() of this very
        archive — keeping its enrich_compat() results, so the import skips the
        same client-only files the preview counted and doesn't query Modrinth
        again — otherwise a fresh parse(). Raises ValueError like parse().
        """
        if (isinstance(manifest, ModpackManifest) and manifest.format == self.name
                and manifest.source and manifest.source == archive_fingerprint(archive_path)):
            return manifest
        return self.parse(archive_path)


# ---------- shared zip / json / path helpers (used by every provider) ----------

_ZIP_UTF8_FLAG = 0x800
_ZIP_UNICODE_PATH_EXTRA = 0x7075
# Tried (per archive) for entry names stored WITHOUT the UTF-8 flag:
# macOS Finder/ditto write UTF-8 without the flag; 7-Zip / WinRAR / Bandizip /
# Explorer "发送到压缩文件夹" on zh-CN Windows write GBK (CP936).
_ZIP_NAME_FALLBACK_ENCODINGS = ("utf-8", "gbk")


def archive_fingerprint(archive_path: str) -> str:
    """'path|size|mtime' of the archive ('' if it can't be stat'ed); see ModpackManifest.source."""
    try:
        st = os.stat(archive_path)
        return f"{os.path.normcase(os.path.abspath(archive_path))}|{st.st_size}|{st.st_mtime_ns}"
    except (OSError, TypeError, ValueError):
        return ""


def open_zip(archive_path: str) -> zipfile.ZipFile:
    """zipfile.ZipFile(), but a corrupt / truncated / non-zip file becomes ValueError."""
    try:
        return zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as e:
        raise ValueError(f"整合包不是有效的 zip 压缩包（可能已损坏或未下载完整）：{e}") from e
    except OSError as e:
        raise ValueError(f"无法打开整合包文件：{e}") from e


def _unicode_path_extra(info: zipfile.ZipInfo, raw_name: bytes) -> Optional[str]:
    """Info-ZIP Unicode Path extra field (0x7075), written by e.g. WinRAR/Bandizip."""
    extra = info.extra or b""
    i = 0
    while i + 4 <= len(extra):
        tag = int.from_bytes(extra[i:i + 2], "little")
        size = int.from_bytes(extra[i + 2:i + 4], "little")
        body = extra[i + 4:i + 4 + size]
        if tag == _ZIP_UNICODE_PATH_EXTRA and len(body) > 5 and body[0] == 1:
            crc = int.from_bytes(body[1:5], "little")
            if crc == (zipfile.crc32(raw_name) & 0xFFFFFFFF):
                try:
                    return body[5:].decode("utf-8")
                except UnicodeDecodeError:
                    return None
        i += 4 + size
    return None


def _sanitize_entry_name(name: str) -> str:
    """Same normalisation zipfile applies to ZipInfo.filename."""
    nul = name.find("\x00")
    if nul >= 0:
        name = name[:nul]
    if os.sep != "/" and os.sep in name:
        name = name.replace(os.sep, "/")
    if os.altsep and os.altsep != "/" and os.altsep in name:
        name = name.replace(os.altsep, "/")
    return name


def zip_entries(zf: zipfile.ZipFile) -> List[Tuple[str, zipfile.ZipInfo]]:
    """
    Every entry of `zf` as (properly decoded name, ZipInfo), in archive order.

    zipfile decodes names that lack the UTF-8 flag as cp437, which turns
    Chinese names from Windows zippers into mojibake (and a GBK trail byte of
    0x7C / 0x5C into '|' / a path separator). We recover the raw bytes and
    pick one encoding for the whole archive (UTF-8, else GBK) the way HMCL
    does, falling back per entry and finally to zipfile's own cp437 name.

    Always open members with the returned ZipInfo (zf.open(info)), not by name.
    """
    cached = getattr(zf, "_hmsl_entries", None)
    if cached is not None:
        return cached

    infos = zf.infolist()
    names: List[Optional[str]] = [None] * len(infos)
    pending: List[Tuple[int, bytes]] = []
    for idx, info in enumerate(infos):
        if info.flag_bits & _ZIP_UTF8_FLAG:
            names[idx] = info.filename
            continue
        try:
            raw = info.orig_filename.encode("cp437")
        except UnicodeEncodeError:
            names[idx] = info.filename
            continue
        uni = _unicode_path_extra(info, raw)
        if uni is not None:
            names[idx] = _sanitize_entry_name(uni)
        elif raw.isascii():
            names[idx] = info.filename
        else:
            pending.append((idx, raw))

    if pending:
        chosen = None
        for enc in _ZIP_NAME_FALLBACK_ENCODINGS:
            try:
                chosen = [raw.decode(enc) for _idx, raw in pending]
                break
            except UnicodeDecodeError:
                continue
        for n, (idx, raw) in enumerate(pending):
            if chosen is not None:
                names[idx] = _sanitize_entry_name(chosen[n])
                continue
            for enc in _ZIP_NAME_FALLBACK_ENCODINGS:
                try:
                    names[idx] = _sanitize_entry_name(raw.decode(enc))
                    break
                except UnicodeDecodeError:
                    continue
            else:
                names[idx] = infos[idx].filename

    # Windows PowerShell's Compress-Archive (and other broken zippers) write '\'
    # separators; zipfile only converts them on Windows. The ZIP spec only
    # allows '/', so normalise on every platform.
    result = [((names[i] or infos[i].filename).replace("\\", "/"), infos[i])
              for i in range(len(infos))]
    try:
        zf._hmsl_entries = result  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return result


def find_zip_entry(zf: zipfile.ZipFile, name: str) -> Optional[zipfile.ZipInfo]:
    """ZipInfo of the entry whose decoded name is exactly `name` (last one wins, like zipfile)."""
    found = None
    for n, info in zip_entries(zf):
        if n == name:
            found = info
    return found


def decode_text(raw: bytes) -> str:
    """
    Decode a manifest / cfg file. Windows tools often add a UTF-8 BOM
    (PowerShell 5.1 Set-Content/Out-File -Encoding UTF8, old Notepad), write
    UTF-16 (PowerShell 5.1 Out-File default) or ANSI/GBK (zh-CN Notepad "ANSI").
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("gbk")   # raises UnicodeDecodeError if it isn't GBK either


def load_json_object(raw: bytes, what: str) -> dict:
    """Parse a JSON manifest; anything that isn't a JSON object becomes ValueError."""
    try:
        data = json.loads(decode_text(raw))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"无法解析 {what}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"无法解析 {what}: 顶层不是 JSON 对象（格式不正确）")
    return data


def read_zip_json(zf: zipfile.ZipFile, name: str, what: Optional[str] = None) -> dict:
    """Read + parse a JSON object entry; missing / unreadable entry becomes ValueError."""
    what = what or name
    info = find_zip_entry(zf, name)
    if info is None:
        raise ValueError(f"{what} 不存在于该 zip 中")
    try:
        raw = zf.read(info)
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as e:
        raise ValueError(f"无法读取 {what}: {e}") from e
    return load_json_object(raw, what)


# Windows cannot create these names (the Win32 layer rejects them or, for ':',
# silently writes an NTFS alternate data stream instead of the file).
_WIN_ILLEGAL_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)}
_WIN_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in "123456789¹²³"}
    | {f"LPT{i}" for i in "123456789¹²³"}
)
_WIN_MAX_PATH = 260

# Suffixes of our write-then-rename temp files (see temp_path_for()):
# ".hmsl-part" while writing, ".hmsl-new" for a download waiting for its
# client-only check.
_TEMP_SUFFIX = ".hmsl-part"
STAGED_SUFFIX = ".hmsl-new"
_TEMP_SUFFIXES = (_TEMP_SUFFIX, STAGED_SUFFIX)


def windows_name_problem(rel_path: str) -> Optional[str]:
    """Why `rel_path` cannot be written on Windows, or None. '.'/'..' are left to the slip check."""
    for comp in re.split(r"[\\/]", rel_path):
        if comp in ("", ".", ".."):
            continue
        bad = sorted({c for c in comp if c in _WIN_ILLEGAL_CHARS})
        if bad:
            shown = " ".join(c if c >= " " else repr(c)[1:-1] for c in bad)
            return f"文件名含 Windows 不允许的字符 {shown}"
        if comp[-1] in ". ":
            return "文件名以点或空格结尾，Windows 无法保存"
        if comp.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED_NAMES:
            return f"使用了 Windows 保留的设备名 {comp.split('.')[0]}"
    return None


def safe_target(server_root: str, rel_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve `rel_path` (from a manifest or zip entry) under `server_root`.
    Returns (absolute target, None) or (None, Chinese reason) when the path
    escapes the server folder (zip-slip) or cannot exist on this OS.
    """
    if sys.platform == "win32":
        problem = windows_name_problem(rel_path)
        if problem:
            return None, problem
    root_abs = os.path.abspath(server_root)
    target = os.path.abspath(os.path.join(root_abs, rel_path))
    if not target.startswith(root_abs.rstrip("\\/") + os.sep):
        return None, "路径指向服务器目录之外（已拦截，可能是恶意压缩包）"
    return target, None


def write_error_reason(target: str, err: BaseException) -> str:
    """Turn an OSError from makedirs/open/replace into a short Chinese reason."""
    failed_path = getattr(err, "filename", None)
    if isinstance(failed_path, str):
        # report the real file's length, not our temp name's
        if failed_path.startswith("\\\\?\\"):
            failed_path = failed_path[4:]
        while failed_path.endswith(_TEMP_SUFFIXES):
            failed_path = failed_path[:failed_path.rfind(".hmsl-")]
    longest = max(len(target), len(failed_path) if isinstance(failed_path, str) else 0)
    if sys.platform == "win32" and longest >= _WIN_MAX_PATH - 12:
        # (CreateDirectory already fails at 248 characters)
        return (f"完整路径过长（{longest} 个字符，超过 Windows 的 {_WIN_MAX_PATH} 字符限制），"
                f"可把服务器放到更短的目录后重新导入")
    detail = getattr(err, "strerror", None) or str(err)
    return f"写入失败：{detail}"


def temp_path_for(target: str, suffix: str = _TEMP_SUFFIX) -> str:
    """
    Temp file beside `target` for write-then-os.replace(). On Windows it gets
    the \\\\?\\ prefix when only the suffix pushes it past MAX_PATH, so a
    target that itself fits is never refused because of the temp name (a
    target that doesn't fit still fails at once, before any download).
    """
    tmp = target + suffix
    if (sys.platform == "win32" and len(tmp) >= _WIN_MAX_PATH - 12
            and len(os.path.abspath(target)) < _WIN_MAX_PATH and not tmp.startswith("\\\\")):
        tmp = "\\\\?\\" + os.path.abspath(tmp)
    return tmp


def discard_temp(tmp: str) -> None:
    try:
        os.remove(tmp)
    except OSError:
        pass


def replace_from_stream(src, target: str) -> None:
    """
    Copy file object `src` into a temp file beside `target`, then os.replace()
    it into place. On any error (corrupt zip member, disk full, target locked
    or read-only …) the temp file is removed and an existing `target` is left
    exactly as it was — re-importing can never destroy a previously good file.
    """
    tmp = temp_path_for(target)
    try:
        with open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 65536)
        if os.name == "posix" and os.path.isfile(target):
            # overwriting in place used to keep the old mode (e.g. +x on start.sh)
            try:
                shutil.copymode(target, tmp)
            except OSError:
                pass
        os.replace(tmp, target)
    except BaseException:
        discard_temp(tmp)
        raise


def summarize_problems(head: str, problems: List[Tuple[str, str]], limit: int = 5) -> str:
    """One warning line for many files: '<head>：a（原因）；b（原因）；等共 N 个'."""
    shown = "；".join(f"{path}（{reason}）" for path, reason in problems[:limit])
    more = f"；等共 {len(problems)} 个" if len(problems) > limit else ""
    return f"{head}：{shown}{more}"


def server_loader_version(manifest: ModpackManifest) -> Optional[str]:
    """The pack's pinned loader version in the form create_server() expects, or None."""
    lv = (manifest.loader_version or "").strip()
    if not lv or manifest.loader not in ("Fabric", "Forge", "NeoForge"):
        return None
    mc = (manifest.mc_version or "").strip()
    # HMCL / MCBBS sometimes record Forge as "1.20.1-47.2.0"
    if mc and lv.startswith(mc + "-"):
        lv = lv[len(mc) + 1:]
    for prefix in ("neoforge-", "forge-", "fabric-loader-", "fabric-"):
        if lv.lower().startswith(prefix):
            lv = lv[len(prefix):]
            break
    return lv or None


def create_server_for_pack(
    manifest: ModpackManifest,
    server_name: str,
    parent_dir: str,
    env_manager,
    installer,
    downloader,
    warnings: List[str],
    report: Optional[Callable[..., None]] = None,
):
    """
    create_server() for a modpack: never syncs HMSL's MOD_DATABASE (the pack
    brings its own mods, and duplicates would replace its pinned versions) and
    pins the pack's loader version. If that exact loader version can't be
    installed, retry once with the default version and say so in `warnings`.
    """
    from core.server_factory import CreateServerResult, create_server

    if not (manifest.mc_version or "").strip():
        return CreateServerResult(False, "", "整合包没有写明 Minecraft 版本，无法创建服务端")

    def forward(frac, msg):
        if not report:
            return
        try:
            report("creating_server", str(msg), int(float(frac) * 100), 100)
        except (TypeError, ValueError):
            report("creating_server", str(msg))

    def attempt(lv):
        return create_server(
            name=server_name,
            version=manifest.mc_version,
            loader=manifest.loader,
            parent_dir=parent_dir,
            env_manager=env_manager,
            installer=installer,
            downloader=downloader,
            progress_callback=forward,
            sync_mods=False,
            loader_version=lv,
        )

    def merge_warnings(result):
        # newer server_factory reports non-fatal notes (e.g. "installer can't pin a version")
        for w in getattr(result, "warnings", None) or []:
            if isinstance(w, str) and w and w not in warnings:
                warnings.append(w)
        return result

    lv = server_loader_version(manifest)
    cr = attempt(lv)
    if not cr.success and lv:
        retry = attempt(None)
        if retry.success:
            warnings.append(
                f"整合包指定的 {manifest.loader} {lv} 安装失败（{cr.error}），已改用默认版本的 "
                f"{manifest.loader}；如果启动时模组报错，请手动换成 {lv}。")
            return merge_warnings(retry)
    return merge_warnings(cr) if cr.success else cr

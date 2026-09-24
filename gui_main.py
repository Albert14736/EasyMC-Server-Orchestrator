import customtkinter as ctk
import codecs
import locale
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import queue
import time
import traceback
import tkinter as tk
from tkinter import filedialog
from core.env_manager import EnvManager
from core.server_installer import ServerInstaller
from core.mod_downloader import ModDownloader
from core.server_factory import create_server, validate_server_name, dir_is_effectively_empty
from core.launcher import start_server
from core.instance_registry import InstanceRegistry, RegistryEntry, path_key
from core.mod_scanner import scan_server_mods, disable_mods_detailed, ScanReport
from core import modrinth_search as ms
from core import modpack as mp

# Drag-and-drop is provided by tkinterdnd2. It's optional — if the package
# isn't installed, the GUI still works, you just can't drag files in.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False
    DND_FILES = None
    TkinterDnD = None
from core.instance_scanner import InstanceScanner, detect_eula
try:
    from core.instance_scanner import get_instance_details
except ImportError:
    get_instance_details = None
try:
    # 卸载护栏要认得"版本列表里能看到的"每一种服务器文件夹（含只有服务端 jar 的）
    from core.instance_scanner import _looks_like_server as _scanner_looks_like_server
except ImportError:
    _scanner_looks_like_server = None

# 设置外观主题
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 等宽字体：Menlo 只有 macOS 有；Windows 上 Tk 会退回 宋体（非等宽、衬线），日志/配置对不齐。
if sys.platform == "darwin":
    MONO_FONT = "Menlo"
elif sys.platform == "win32":
    MONO_FONT = "Consolas"
else:
    MONO_FONT = "Courier"
# 原生 tk 控件的界面字体：font=("", N) 在中文 Windows 上会解析成 宋体，改用系统 UI 字体。
# macOS 保持 ""（系统默认）不变。
UI_FONT = "Microsoft YaHei UI" if sys.platform == "win32" else ""

# 发出 stop 命令后最多等服务器多久（它一退出 stop() 就立即返回）；超时后 ServerProcess.stop
# 会强制结束整个进程树。大整合包保存世界可能要几十秒，给足余量。
STOP_GRACE_S = 60.0


def _post_ui(widget, fn, *args):
    """从后台线程把 fn(*args) 投递回 Tk 主线程执行。
    窗口已关闭（_closed / 已销毁）或主程序已退出时静默丢弃，
    不会再去碰已销毁的控件（否则 Tk 回调里抛 TclError）。"""
    def run():
        if getattr(widget, "_closed", False):
            return
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        fn(*args)
    try:
        widget.after(0, run)
    except (RuntimeError, tk.TclError):
        pass   # 主循环已结束 / 解释器已销毁


class _WorkerWindowMixin:
    """带后台线程的弹窗：关窗时置 _closed，之后线程投递回来的结果一律丢弃。"""
    _closed = False

    def destroy(self):
        self._closed = True
        super().destroy()

    def _post(self, fn, *args):
        _post_ui(self, fn, *args)


class LogQueue:
    """把 print 输出按【整行】送进文本框。
    按换行缓冲：print(x, end='') + print(' [成功]') 这类分两次写的同一行，
    不会再被拆成两行各带一个时间戳。"""
    def __init__(self, textbox):
        self.queue = queue.Queue()
        self.textbox = textbox
        self._buf = ""
        self._closed = False
        self.textbox.after(100, self.check_queue)

    def write(self, msg):
        self._buf += msg
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.queue.put(line)
        return len(msg)

    def flush(self): pass

    def close(self):
        """安装结束：把最后半行也送出去，队列清空后停止轮询。"""
        if self._buf.strip():
            self.queue.put(self._buf)
        self._buf = ""
        self._closed = True

    def check_queue(self):
        try:
            if not self.textbox.winfo_exists():
                return          # 页面已切走：停止轮询（原来会永远 100ms 空转）
            while not self.queue.empty():
                msg = self.queue.get()
                self.textbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg.strip()}\n")
                self.textbox.see("end")
        except tk.TclError:
            return
        if self._closed and self.queue.empty():
            return
        self.textbox.after(100, self.check_queue)


class _ThreadRoutedStdout:
    """sys.stdout 代理：登记过的线程的 print 输出进各自的 LogQueue，其它线程照旧写原 stdout。
    原实现在安装线程里直接替换全局 sys.stdout 再还原——两个安装/导入同时进行时，
    日志会串到别的文本框，还原时还会互相覆盖。"""

    def __init__(self, fallback):
        self._fallback = fallback
        self._routes = {}

    def route(self, sink):
        self._routes[threading.get_ident()] = sink

    def unroute(self):
        self._routes.pop(threading.get_ident(), None)

    def write(self, s):
        sink = self._routes.get(threading.get_ident())
        if sink is not None:
            return sink.write(s)
        if self._fallback is not None:
            return self._fallback.write(s)
        return len(s)

    def flush(self):
        sink = self._routes.get(threading.get_ident())
        if sink is not None:
            return
        if self._fallback is not None:
            try:
                self._fallback.flush()
            except (OSError, ValueError):
                pass

    def __getattr__(self, name):
        if self._fallback is None:
            raise AttributeError(name)
        return getattr(self._fallback, name)


def _route_stdout_to(sink):
    """让【当前线程】的 print 输出进 sink；配合 _unroute_stdout() 成对使用。"""
    if not isinstance(sys.stdout, _ThreadRoutedStdout):
        sys.stdout = _ThreadRoutedStdout(sys.stdout)
    sys.stdout.route(sink)


def _unroute_stdout():
    if isinstance(sys.stdout, _ThreadRoutedStdout):
        sys.stdout.unroute()


class ConsoleWindow(ctk.CTkToplevel):
    """独立的服务器控制台窗口：实时日志、命令输入、停止按钮。"""
    def __init__(self, master, server_name, server_process):
        super().__init__(master)
        self.title(f"控制台 - {server_name}")
        self.geometry("760x520")
        self.sp = server_process
        self._closed = False

        ctk.CTkLabel(self, text=f"📟 {server_name}", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(10, 4))
        ctk.CTkLabel(self, text=_short_path(server_process.server_path, 70),
                     text_color="gray", font=ctk.CTkFont(size=11)).pack()

        self.log_box = ctk.CTkTextbox(self, width=720, height=360, fg_color="#000000", text_color="#00ff66", font=(MONO_FONT, 12))
        self.log_box.pack(padx=20, pady=10, fill="both", expand=True)

        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.pack(fill="x", padx=20, pady=(0, 10))
        self.cmd_var = ctk.StringVar()
        self.cmd_entry = ctk.CTkEntry(input_row, textvariable=self.cmd_var, placeholder_text="输入服务器命令（如 say hi, list, stop）…")
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cmd_entry.bind("<Return>", lambda e: self._send())
        ctk.CTkButton(input_row, text="发送", width=70, command=self._send).pack(side="left")
        ctk.CTkButton(input_row, text="⏹ 停止", width=80, fg_color="#a13b3b", hover_color="#823030", command=self._stop).pack(side="left", padx=(8, 0))
        ctk.CTkButton(input_row, text="强制结束", width=80, fg_color="#5a2b2b", hover_color="#7a3535",
                      command=self._force_kill).pack(side="left", padx=(8, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_output)

    def _append(self, text):
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")

    def _send(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        self.sp.send_command(cmd)
        self._append(f"> {cmd}")
        self.cmd_var.set("")

    def _stop(self):
        if not self.sp.is_alive():
            self._append("[GUI] 服务器已经不在运行")
            return
        self._append("[GUI] 正在请求服务器停止（保存世界后退出；超过 60 秒仍未退出会被强制结束）...")
        threading.Thread(target=lambda: self.sp.stop(grace=STOP_GRACE_S), daemon=True).start()

    def _force_kill(self):
        """服务器卡死、⏹ 停止 等不到它退出时用：直接结束整个进程树。"""
        if not self.sp.is_alive():
            self._append("[GUI] 服务器已经不在运行")
            return
        dlg = ConfirmDialog(
            self, title="⚠️ 强制结束服务器",
            msg=("强制结束会立即杀掉服务器进程，上次自动保存之后的世界进度会丢失。\n\n"
                 "只在服务器卡住、点「⏹ 停止」后迟迟不退出时使用。"),
            ok_text="强制结束", cancel_text="取消", danger=True)
        self.wait_window(dlg)
        if not dlg.result or self._closed:
            return
        self._append("[GUI] 正在强制结束服务器进程...")
        threading.Thread(target=self.sp.kill, daemon=True).start()

    def _poll_output(self):
        if self._closed:
            return
        try:
            for line in self.sp.drain_lines():
                self._append(line)
            if not self.sp.is_alive():
                for line in self.sp.drain_lines():   # 退出前最后几行别漏
                    self._append(line)
                self._append("[GUI] 服务器进程已退出")
                return
        except tk.TclError:
            return
        self.after(150, self._poll_output)

    def _on_close(self):
        self._closed = True
        if self.sp.is_alive():
            # stop() 在服务器退出后立即返回；60 秒只是给卡住的服务器的上限（大整合包存世界
            # 也够用），超时后 stop() 会强制结束整个进程树。
            # 服务器仍记录在 HMSLApp._servers 里：再点 ▶ 启动 会重新打开它的控制台
            # （可以看停止进度、强制结束）；主窗口关闭时（_on_app_close）也会负责把它停掉。
            threading.Thread(target=lambda: self.sp.stop(grace=STOP_GRACE_S), daemon=True).start()
        self.destroy()


class ModScanWindow(_WorkerWindowMixin, ctk.CTkToplevel):
    """两阶段：扫描中（进度条）→ 完成后展示分类列表 + 一键禁用按钮。"""
    STATUS_LABEL = {
        "client_only": ("🚫 客户端专属", "#e07a5f"),
        "unknown":     ("❓ 未在 Modrinth 找到", "#f4a261"),
        "server_ok":   ("✅ 服务端兼容", "#7eb77f"),
        "error":       ("⚠️ 读取失败", "#b85d5d"),
    }

    def __init__(self, master, server_name, server_path):
        super().__init__(master)
        self.title(f"模组扫描 - {server_name}")
        self.geometry("780x600")
        _raise_toplevel(self)
        self.server_name = server_name
        self.server_path = server_path
        self.report = None
        self._entry_vars = {}  # file_path -> BooleanVar (per-row checkbox)

        ctk.CTkLabel(self, text=f"🧹 模组扫描: {server_name}",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(15, 4))

        # Phase 1: progress UI
        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.progress_frame.pack(fill="both", expand=True, padx=30, pady=20)
        ctk.CTkLabel(self.progress_frame, text="正在通过 Modrinth API 反查每个模组...",
                     text_color="gray").pack(pady=(60, 10))
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, width=520)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)
        self.progress_label = ctk.CTkLabel(self.progress_frame, text="准备开始...", text_color="gray")
        self.progress_label.pack()

        threading.Thread(target=self._run_scan, daemon=True).start()

    def _run_scan(self):
        try:
            report = scan_server_mods(self.server_path, progress_callback=self._on_progress)
        except Exception as e:
            # 先把消息绑定成字符串：Python 3 在 except 块结束时会删掉 e，
            # lambda 里再引用 e 会 NameError，窗口就永远卡在"扫描中"。
            msg = f"扫描失败：{type(e).__name__}: {e}"
            self._post(self._show_fatal, msg)
            return
        self.report = report
        self._post(self._show_results)

    def _on_progress(self, current, total, filename):
        # 后台线程调用：只投递，不在这里碰控件
        self._post(self._set_progress, current, total, filename)

    def _set_progress(self, current, total, filename):
        frac = current / total if total else 1.0
        self.progress_bar.set(frac)
        self.progress_label.configure(text=f"{current}/{total}  {filename}")

    def _show_fatal(self, msg):
        for w in self.progress_frame.winfo_children(): w.destroy()
        ctk.CTkLabel(self.progress_frame, text=msg, text_color="#e07a5f",
                     wraplength=640, justify="left").pack(pady=40)
        ctk.CTkButton(self.progress_frame, text="关闭", width=120, command=self.destroy).pack()

    def _show_results(self):
        self.progress_frame.destroy()

        report = self.report
        if not report.mods_dir:
            ctk.CTkLabel(self, text="该服务器没有 mods/ 或 plugins/ 目录，没什么可扫的。",
                         text_color="gray").pack(pady=80)
            ctk.CTkButton(self, text="关闭", width=120, command=self.destroy).pack()
            return

        n_client = len(report.client_only())
        n_unknown = len(report.unknown())
        n_ok = len(report.server_ok())
        n_err = len(report.errors())
        summary = (f"共 {len(report.entries)} 个模组 — "
                   f"🚫 客户端 {n_client}  ❓ 未知 {n_unknown}  ✅ 兼容 {n_ok}"
                   + (f"  ⚠️ 失败 {n_err}" if n_err else ""))
        ctk.CTkLabel(self, text=summary, text_color="#aaaaaa", font=ctk.CTkFont(size=12)).pack(pady=(0, 8))

        # Scrollable list with checkboxes
        scroll = ctk.CTkScrollableFrame(self, width=720, height=420, fg_color="transparent")
        scroll.pack(padx=20, pady=8, fill="both", expand=True)

        # Group order: client_only first (most actionable), then unknown, then errors, then server_ok
        groups = [
            ("client_only", report.client_only(), True),   # checkbox default-ticked
            ("unknown",     report.unknown(),     False),  # user opt-in only
            ("error",       report.errors(),     False),
            ("server_ok",   report.server_ok(),  False),
        ]
        for status, entries, default_checked in groups:
            if not entries: continue
            label, color = self.STATUS_LABEL[status]
            ctk.CTkLabel(scroll, text=f"{label}（{len(entries)} 个）", text_color=color,
                         font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", pady=(8, 2))
            for entry in entries:
                self._add_entry_row(scroll, entry, default_checked)

        # Bottom action bar
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(4, 14))
        ctk.CTkButton(bottom, text="🗑 禁用所有勾选的模组", width=220, height=38,
                      fg_color="#a13b3b", hover_color="#823030",
                      command=self._on_apply).pack(side="left")
        sub = os.path.basename(report.mods_dir) or "mods"
        ctk.CTkLabel(bottom, text=f"（被禁用的模组会移到 {sub}/.disabled/，可手动恢复）",
                     text_color="gray", font=ctk.CTkFont(size=11)).pack(side="left", padx=10)
        ctk.CTkButton(bottom, text="关闭", width=90, height=38, fg_color="#3d3d3d",
                      hover_color="#4d4d4d", command=self.destroy).pack(side="right")

    def _add_entry_row(self, parent, entry, default_checked):
        row = ctk.CTkFrame(parent, fg_color="#1d1d1d", corner_radius=8)
        row.pack(fill="x", pady=2, padx=4)

        var = ctk.BooleanVar(value=default_checked)
        self._entry_vars[entry.file_path] = (var, entry)
        ctk.CTkCheckBox(row, text="", variable=var, width=20).pack(side="left", padx=(10, 4))

        name_text = entry.file_name
        if entry.mod_info:
            name_text = f"{entry.mod_info.project_title}  ({entry.file_name})"
        ctk.CTkLabel(row, text=name_text, anchor="w",
                     font=ctk.CTkFont(size=12)).pack(side="left", fill="x", expand=True, padx=4, pady=6)

    def _on_apply(self):
        to_disable = [e for (_path, (var, e)) in self._entry_vars.items() if var.get()]
        if not to_disable:
            self._toast("没有勾选任何模组。")
            return
        sub = os.path.basename(self.report.mods_dir) or "mods"
        try:
            moved, failed = disable_mods_detailed(to_disable, self.report.mods_dir)
        except OSError as e:          # 例如 .disabled/ 目录都建不了
            moved, failed = 0, [(f"{sub}/.disabled/", _os_error_text(e))]
        msg = f"已禁用 {moved} 个模组，移至 {sub}/.disabled/。"
        if failed:
            lines = [f"• {name}：{err}" for name, err in failed[:10]]
            if len(failed) > 10:
                lines.append(f"……另有 {len(failed) - 10} 个")
            msg += (f"\n\n以下 {len(failed)} 个未能禁用（若提示文件被占用，请先停止服务器再试）：\n"
                    + "\n".join(lines))
        # 列表已过时：关掉扫描窗，结果提示挂在主窗口上（挂在本窗口上会随 destroy 一起消失，
        # 用户根本看不到）。要看最新状态重新扫描即可。
        app = self.master
        self.destroy()
        app._show_error("禁用结果" if not failed else "禁用结果（部分失败）", msg)

    def _toast(self, msg):
        self.master._show_error("提示", msg, parent=self)


class ConfirmDialog(ctk.CTkToplevel):
    """简单模态确认弹窗：返回 True/False，给 GUI 决定是否继续敏感操作。"""
    def __init__(self, master, title, msg, ok_text="继续", cancel_text="取消", danger=False):
        super().__init__(master)
        # 高度随内容伸缩（路径、任务列表等较长的提示原来会把按钮挤出窗口）
        lines = sum(1 + len(line) // 30 for line in str(msg).split("\n"))
        self.title(title); self.geometry(f"520x{max(320, min(600, 190 + 21 * lines))}")
        self.result = False

        # 底部按钮 FIRST + side="bottom" — 给它预留空间，
        # 否则上面文本一长就会把按钮压成薄片（之前向导踩过同款坑）。
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", pady=20)
        ctk.CTkButton(row, text=cancel_text, width=130, height=38,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self._cancel).pack(side="left", padx=10)
        ctk.CTkButton(row, text=ok_text, width=160, height=38,
                      fg_color=("#a13b3b" if danger else "#2b719e"),
                      hover_color=("#823030" if danger else "#1f538d"),
                      command=self._ok).pack(side="left", padx=10)

        # 内容
        ctk.CTkLabel(self, text=title,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(24, 10))
        ctk.CTkLabel(self, text=msg, wraplength=460, text_color="gray",
                     justify="left").pack(padx=24, pady=(0, 10))
        self.transient(master); self.grab_set()

    def _ok(self):   self.result = True;  self.destroy()
    def _cancel(self): self.result = False; self.destroy()


class RemoveOptionDialog(ctk.CTkToplevel):
    """让用户选「仅从列表移除」还是「彻底卸载」。返回值在 self.choice。"""

    def __init__(self, master, instance_name):
        super().__init__(master)
        self.title(f"移除 {instance_name}")
        self.geometry("500x360")
        self.choice = None  # "remove" / "uninstall" / None(取消)

        ctk.CTkLabel(self, text=f"🗑 处理 {instance_name}",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(18, 4))
        ctk.CTkLabel(self, text="选择一种处理方式：",
                     text_color="gray", font=ctk.CTkFont(size=12)).pack()

        # Option 1: from-list-only (safe)
        opt1 = ctk.CTkFrame(self, fg_color="#2a3540", corner_radius=10,
                            border_width=1, border_color="#3a5570")
        opt1.pack(fill="x", padx=20, pady=(14, 6))
        ctk.CTkButton(opt1, text="📋 仅从列表移除", height=42,
                      fg_color="#2b719e", hover_color="#1f538d",
                      command=lambda: self._pick("remove")).pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(opt1,
                     text="只是不在 HMSL 显示。服务器文件夹和数据全部保留，下次还能找回。",
                     text_color="#bbb", font=ctk.CTkFont(size=11),
                     wraplength=440, justify="left").pack(padx=10, pady=(0, 10))

        # Option 2: full uninstall (destructive)
        opt2 = ctk.CTkFrame(self, fg_color="#3d2020", corner_radius=10,
                            border_width=1, border_color="#883030")
        opt2.pack(fill="x", padx=20, pady=6)
        ctk.CTkButton(opt2, text="⚠️ 彻底卸载", height=42,
                      fg_color="#a13b3b", hover_color="#823030",
                      command=lambda: self._pick("uninstall")).pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(opt2,
                     text="永久删除整个服务器文件夹（世界、模组、配置、玩家存档全部丢失）。",
                     text_color="#e0bbbb", font=ctk.CTkFont(size=11),
                     wraplength=440, justify="left").pack(padx=10, pady=(0, 10))

        ctk.CTkButton(self, text="取消", width=100, fg_color="#3d3d3d",
                      hover_color="#4d4d4d", command=self.destroy).pack(pady=10)

        self.transient(master); self.grab_set()

    def _pick(self, choice):
        self.choice = choice
        self.destroy()


class ModBrowserWindow(_WorkerWindowMixin, ctk.CTkToplevel):
    """HMCL 式模组搜索浏览器：搜 Modrinth → 一键安装到服务器 mods/plugins。"""

    PAGE_SIZE = 15
    # 这些服务端装的是【插件】(plugins/)，不是模组；Modrinth 上要按 project_type=plugin 搜。
    PLUGIN_LOADERS = ("paper", "spigot", "bukkit", "purpur", "folia")

    def __init__(self, master, server_name, server_path, mc_version=None, loader=None):
        super().__init__(master)
        self.app = master
        self.server_name = server_name
        self.server_path = server_path
        self.mc_version = mc_version
        self.loader = loader
        self.offset = 0
        self.current_query = ""
        self._installing_buttons = {}  # button -> hit, so we can re-enable

        # Project type: Paper/Spigot/Bukkit/... -> plugin, others -> mod
        loader_l = (loader or "").lower()
        self.project_type = "plugin" if loader_l in self.PLUGIN_LOADERS else "mod"
        # 原版服务端既不能装模组也不能装插件
        self.vanilla = loader_l == "vanilla"
        kind = "插件" if self.project_type == "plugin" else "模组"
        self.title(f"下载{kind} - {server_name}")
        self.geometry("840x620")
        _raise_toplevel(self)

        ctk.CTkLabel(self, text=f"📥 下载{kind}: {server_name}",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(15, 4))
        filter_text = []
        if mc_version: filter_text.append(f"版本 {mc_version}")
        if loader:     filter_text.append(loader)
        filter_text.append(f"类型 {self.project_type}")
        warn = ""
        if not mc_version:
            warn = "  ⚠️ 未识别出游戏版本，搜索结果不按版本过滤，安装前请自行确认兼容"
        ctk.CTkLabel(self, text="过滤: " + " / ".join(filter_text) + warn,
                     text_color="gray", font=ctk.CTkFont(size=11)).pack()

        # Search row
        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=20, pady=10)
        self.search_var = ctk.StringVar()
        entry = ctk.CTkEntry(search_row, textvariable=self.search_var,
                             placeholder_text="搜索关键字（留空浏览热门）")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda e: self._do_search(0))
        ctk.CTkButton(search_row, text="搜索", width=80, command=lambda: self._do_search(0)).pack(side="left")

        # Result area
        self.results_frame = ctk.CTkScrollableFrame(self, width=780, height=420, fg_color="transparent")
        self.results_frame.pack(padx=20, pady=4, fill="both", expand=True)

        # Pagination bar
        page_row = ctk.CTkFrame(self, fg_color="transparent")
        page_row.pack(fill="x", padx=20, pady=(0, 10))
        self.prev_btn = ctk.CTkButton(page_row, text="◀ 上一页", width=100, state="disabled",
                                       fg_color="#3d3d3d", hover_color="#4d4d4d",
                                       command=lambda: self._do_search(max(0, self.offset - self.PAGE_SIZE)))
        self.prev_btn.pack(side="left", padx=4)
        self.next_btn = ctk.CTkButton(page_row, text="下一页 ▶", width=100, state="disabled",
                                       fg_color="#3d3d3d", hover_color="#4d4d4d",
                                       command=lambda: self._do_search(self.offset + self.PAGE_SIZE))
        self.next_btn.pack(side="left", padx=4)
        self.page_label = ctk.CTkLabel(page_row, text="", text_color="gray")
        self.page_label.pack(side="left", padx=12)
        ctk.CTkButton(page_row, text="关闭", width=80, fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self.destroy).pack(side="right")

        if self.vanilla:
            self._show_msg("原版（Vanilla）服务端不能加载模组或插件。\n\n"
                           "要装模组请新建 Fabric / Forge / NeoForge 服务器；"
                           "要装插件请用 Paper。")
            return
        # Initial fetch (empty query → relevance ranking returns popular mods)
        self._do_search(0)

    def _do_search(self, offset):
        if self.vanilla:
            return
        self.current_query = self.search_var.get().strip()
        self.offset = offset
        # Clear results, show "loading"
        for w in self.results_frame.winfo_children(): w.destroy()
        ctk.CTkLabel(self.results_frame, text="正在搜索…", text_color="gray").pack(pady=80)
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")
        # 序号：连点搜索/翻页时，慢回来的旧结果不能覆盖新结果
        self._search_seq = getattr(self, "_search_seq", 0) + 1
        threading.Thread(target=self._run_search,
                         args=(self._search_seq, self.current_query, self.offset),
                         daemon=True).start()

    def _run_search(self, seq, query, offset):
        try:
            page = ms.search_mods(
                query=query,
                mc_version=self.mc_version,
                loader=self.loader,
                project_type=self.project_type,
                offset=offset,
                limit=self.PAGE_SIZE,
            )
        except Exception as e:
            msg = f"搜索失败：{e}"      # 先绑定：except 结束后 e 会被删除
            self._post(self._search_result, seq, None, msg)
            return
        self._post(self._search_result, seq, page, None)

    def _search_result(self, seq, page, err):
        if seq != self._search_seq:
            return
        if err is not None:
            self._show_msg(err)
        else:
            self._render_page(page)

    def _show_msg(self, msg):
        for w in self.results_frame.winfo_children(): w.destroy()
        ctk.CTkLabel(self.results_frame, text=msg, text_color="#e07a5f",
                     wraplength=700, justify="left").pack(pady=80)

    def _render_page(self, page):
        for w in self.results_frame.winfo_children(): w.destroy()
        if not page.hits:
            ctk.CTkLabel(self.results_frame, text="没有匹配结果。试试别的关键字？",
                         text_color="gray").pack(pady=80)
            self.page_label.configure(text=f"共 0 个结果")
            return
        for hit in page.hits:
            self._add_hit_card(hit)
        # Pagination
        end = page.offset + len(page.hits)
        self.page_label.configure(text=f"共 {page.total_hits} 个结果  ·  显示 {page.offset + 1}-{end}")
        self.prev_btn.configure(state="normal" if page.offset > 0 else "disabled")
        self.next_btn.configure(state="normal" if page.has_next else "disabled")

    def _add_hit_card(self, hit):
        card = ctk.CTkFrame(self.results_frame, fg_color="#1d1d1d", corner_radius=10)
        card.pack(fill="x", pady=4, padx=4)

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(side="left", fill="both", expand=True, padx=14, pady=10)

        title_row = ctk.CTkFrame(body, fg_color="transparent"); title_row.pack(anchor="w", fill="x")
        ctk.CTkLabel(title_row, text=hit.title, font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        if hit.is_client_only():
            ctk.CTkLabel(title_row, text=" 🚫 客户端专属", text_color="#e07a5f",
                         font=ctk.CTkFont(size=11)).pack(side="left", padx=(8, 0))

        desc = hit.description or ""
        if len(desc) > 110: desc = desc[:107] + "…"
        ctk.CTkLabel(body, text=desc, text_color="gray", wraplength=550,
                     justify="left", anchor="w").pack(anchor="w", fill="x", pady=(2, 0))

        ctk.CTkLabel(body, text=f"📥 {hit.downloads:,}   ·   {hit.slug}",
                     text_color="#888", font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(2, 0))

        btn = ctk.CTkButton(card, text="📥 安装", width=90, height=34,
                            fg_color="#2b719e", hover_color="#1f538d")
        btn.configure(command=lambda h=hit, b=btn: self._on_install_clicked(h, b))
        btn.pack(side="right", padx=14, pady=10)

    def _on_install_clicked(self, hit, btn):
        # Step 0: 不知道游戏版本时只能装"最新版"，很可能不兼容——先问一句
        if not self.mc_version:
            dlg = ConfirmDialog(
                self,
                title="⚠️ 未识别出服务器的游戏版本",
                msg=(f"HMSL 没能识别这台服务器的 Minecraft 版本，只能为 \"{hit.title}\" "
                     "安装 Modrinth 上最新的发布版，可能与服务器不兼容、导致启动崩溃。\n\n"
                     "仍要安装吗？"),
                ok_text="仍然安装", cancel_text="取消", danger=True,
            )
            self.wait_window(dlg)
            if not dlg.result:
                return
        # Step 1: client-only check + confirmation
        if hit.is_client_only():
            dlg = ConfirmDialog(
                self,
                title="⚠️ 这是纯客户端模组",
                msg=(f"\"{hit.title}\" 是 Modrinth 上标记为客户端专属的模组"
                     f"（client_side={hit.client_side}, server_side={hit.server_side}）。"
                     "装到服务端通常没用，部分还会让服务端崩溃。\n\n你确定要继续安装吗？"),
                ok_text="仍然安装",
                cancel_text="取消",
                danger=True,
            )
            self.wait_window(dlg)
            if not dlg.result:
                return

        # Step 2: background install
        btn.configure(text="安装中…", state="disabled")
        threading.Thread(target=self._run_install, args=(hit, btn), daemon=True).start()

    def _run_install(self, hit, btn):
        try:
            versions = ms.get_project_versions(hit.project_id,
                                                mc_version=self.mc_version,
                                                loader=self.loader,
                                                project_type=self.project_type)
            best = ms.pick_best_version(versions)
            if not best:
                raise RuntimeError("Modrinth 上没有匹配当前版本/loader 的发布")
            file = ms.pick_primary_file(best)
            if not file:
                raise RuntimeError("该版本没有可下载的 .jar 文件")

            if not os.path.isdir(self.server_path):
                raise RuntimeError(f"服务器文件夹不存在：{self.server_path}")
            # 插件进 plugins/，模组进 mods/（服务器刚建好时可能还没有这个目录，自动创建）。
            # 不用 find_mods_dir：它优先 mods/，Paper 服里若恰好有 mods/ 会装错地方。
            default_sub = "plugins" if self.project_type == "plugin" else "mods"
            dest_dir = os.path.join(self.server_path, default_sub)

            # 带上 Modrinth 给的 sha1：下载不完整/被篡改的 jar 不会落盘
            sha1 = (file.get("hashes") or {}).get("sha1")
            target = ms.download_to(file["url"], dest_dir, file["filename"], sha1=sha1)
            detail = f"已下载到 {os.path.join(default_sub, os.path.basename(target))}"
            success = True
        except Exception as e:
            detail = str(e) or type(e).__name__     # 先绑定：except 结束后 e 会被删除
            success = False
        # 窗口已关也要把结果告诉用户（挂到主窗口上）
        _post_ui(self.app, self._install_done, hit, btn, success, detail)

    def _install_done(self, hit, btn, success, detail):
        try:
            if btn.winfo_exists():
                if success:
                    btn.configure(text="✅ 已安装", state="disabled",
                                  fg_color="#3d6b3d", hover_color="#3d6b3d")
                else:
                    btn.configure(text="❌ 失败", state="normal",
                                  fg_color="#a13b3b", hover_color="#823030")
        except tk.TclError:
            pass
        # Use the app's show_error helper for the detail
        self.app._show_error(
            "安装结果" if success else "安装失败",
            f"{hit.title}\n\n{detail}",
            parent=None if self._closed else self,
        )


class ModpackImportWindow(_WorkerWindowMixin, ctk.CTkToplevel):
    """三阶段：解析 manifest → 用户确认（含目标位置/名字）→ 后台导入 + 进度。"""

    def __init__(self, master, archive_path):
        super().__init__(master)
        self.app = master
        self.title("导入整合包")
        self.geometry("720x540")
        _raise_toplevel(self)
        self.archive_path = archive_path
        self.manifest = None
        self._import_started = False

        ctk.CTkLabel(self, text="📦 导入整合包", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(15, 4))
        ctk.CTkLabel(self, text=os.path.basename(archive_path), text_color="gray",
                     font=ctk.CTkFont(size=11)).pack()

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=20, pady=10)

        # Phase 1: parsing
        ctk.CTkLabel(self.body, text="正在解析整合包…", text_color="gray").pack(pady=80)
        threading.Thread(target=self._parse_in_bg, daemon=True).start()

    # ---------- Phase 1: parse ----------

    def _parse_in_bg(self):
        try:
            provider = mp.detect_provider(self.archive_path)
            if not provider:
                self._post(self._show_fatal,
                           "无法识别此整合包格式（也可能文件已损坏）。\n\n"
                           "目前支持：Modrinth (.mrpack)、CurseForge、MCBBS、"
                           "HMCL（客户端 / 服务端整合包）、MultiMC 整合包（.zip）。")
                return
            manifest = provider.parse(self.archive_path)
            # Many community modpacks omit env metadata. Look it up so the
            # preview's "skip X client-only" number is accurate before the
            # user confirms.
            if hasattr(provider, "enrich_compat"):
                self._post(self._update_parse_status, "正在通过 Modrinth 反查兼容性…")
                provider.enrich_compat(manifest)
        except Exception as e:
            # 先把消息绑定成字符串：Python 3 在 except 块结束时删除 e，
            # 原来 lambda 里引用 e 会 NameError，窗口永远卡在"正在解析"。
            msg = f"解析失败：{type(e).__name__}: {e}"
            self._post(self._show_fatal, msg)
            return
        self.manifest = manifest
        self._post(self._show_preview)

    def _update_parse_status(self, msg):
        for w in self.body.winfo_children():
            if isinstance(w, ctk.CTkLabel):
                w.configure(text=msg)
                return

    def _show_fatal(self, msg):
        for w in self.body.winfo_children(): w.destroy()
        ctk.CTkLabel(self.body, text="❌", font=ctk.CTkFont(size=40)).pack(pady=(40, 8))
        ctk.CTkLabel(self.body, text=msg, text_color="#e07a5f",
                     wraplength=580, justify="left").pack(padx=20)
        ctk.CTkButton(self.body, text="关闭", width=120, command=self.destroy).pack(pady=20)

    # ---------- Phase 2: preview + confirm ----------

    def _show_preview(self):
        m = self.manifest
        for w in self.body.winfo_children(): w.destroy()

        info = ctk.CTkFrame(self.body, fg_color="#1d1d1d", corner_radius=10)
        info.pack(fill="x", pady=(0, 10))
        rows = [
            ("整合包", f"{m.name}  ({m.format})"),
            ("版本", m.version or "—"),
            ("游戏版本", m.mc_version),
            ("加载器", f"{m.loader}" + (f"  ({m.loader_version})" if m.loader_version else "")),
            ("总文件数", f"{len(m.files)} 个 → 将安装 {len(m.server_files)}，跳过 {len(m.skipped_client_files)} 个客户端专属"),
        ]
        if m.summary:
            rows.append(("简介", m.summary))
        for label, value in rows:
            row = ctk.CTkFrame(info, fg_color="transparent"); row.pack(fill="x", padx=14, pady=4)
            ctk.CTkLabel(row, text=f"{label}:", width=80, anchor="w",
                         text_color="gray", font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            ctk.CTkLabel(row, text=value, anchor="w", wraplength=520, justify="left").pack(side="left", fill="x", expand=True)
        # 解析阶段的提示（如"Quilt 按 Fabric 服务端运行""加载器是根据模组 jar 推断的"）
        pre_warnings = [w for w in (getattr(m, "warnings", None) or []) if isinstance(w, str) and w]
        if pre_warnings:
            self.geometry("720x640")
            self._render_warnings(pre_warnings, max_height=90)

        # Target name + parent dir
        cfg = ctk.CTkFrame(self.body, fg_color="transparent"); cfg.pack(fill="x", pady=8)
        ctk.CTkLabel(cfg, text="服务器名称:", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w")
        self.name_var = ctk.StringVar(value=_default_name_from(m.name) or "imported_server")
        ctk.CTkEntry(cfg, textvariable=self.name_var, width=400).pack(anchor="w", pady=(2, 8))

        ctk.CTkLabel(cfg, text="创建位置:", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w")
        dir_row = ctk.CTkFrame(cfg, fg_color="transparent"); dir_row.pack(anchor="w", fill="x", pady=2)
        self.target_dir_var = ctk.StringVar(value=self.master.env.script_dir)
        ctk.CTkEntry(dir_row, textvariable=self.target_dir_var, width=420).pack(side="left")
        ctk.CTkButton(dir_row, text="浏览...", width=70,
                      command=self._pick_dir).pack(side="left", padx=(6, 0))

        # Action buttons
        btn_row = ctk.CTkFrame(self.body, fg_color="transparent"); btn_row.pack(pady=20)
        ctk.CTkButton(btn_row, text="取消", width=120, fg_color="#3d3d3d",
                      hover_color="#4d4d4d", command=self.destroy).pack(side="left", padx=8)
        ctk.CTkButton(btn_row, text="开始导入", width=160, fg_color="#2b719e",
                      hover_color="#1f538d", command=self._begin_import).pack(side="left", padx=8)

    def _pick_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.target_dir_var.get(),
                                         title="选择创建位置")
        if chosen:
            self.target_dir_var.set(os.path.normpath(chosen))

    # ---------- Phase 3: import in background ----------

    def _begin_import(self):
        if self._import_started: return
        name = self.name_var.get().strip()
        parent_dir = self.target_dir_var.get().strip()
        if not name or not os.path.isdir(parent_dir):
            self.app._show_error("无法导入", "请填写服务器名称并选择存在的目录。", parent=self)
            return
        name_err = validate_server_name(name)
        if name_err:
            self.app._show_error("服务器名称不可用", name_err, parent=self)
            return
        target = os.path.join(parent_dir, name)
        if os.path.exists(target) and not os.path.isdir(target):
            self.app._show_error("无法导入", f"已存在同名文件（不是文件夹）：\n{target}", parent=self)
            return
        if self.app._is_server_running(target):
            self.app._show_error("无法导入", f"{name} 正在运行，请先停止它或换一个名称。", parent=self)
            return
        if os.path.isdir(target) and not dir_is_effectively_empty(target):
            # 创建服务端时不会往非空文件夹里装（不合并、不覆盖，也不动里面的任何文件），
            # 所以这里不提供"仍然导入"，只能换个名称。只有 .DS_Store / desktop.ini 这类
            # 系统文件的文件夹算空的（和 create_server 同一判断）。
            new_name = _ask_new_name_for_existing(
                self, self.app, parent_dir, name, action="导入",
                extra=("想更新这台服务器上的整合包？请把新版导入成一台新服务器，"
                       "再把旧服务器的 world 等文件夹复制过去。"))
            if not new_name or self._closed:
                return
            name = new_name
            self.name_var.set(name)
            target = os.path.join(parent_dir, name)
        # 记下目标文件夹导入前的状态：失败时只有【本次新建】的文件夹才提示"可手动删除"
        self._import_target = target
        self._target_state = (("empty" if dir_is_effectively_empty(target) else "nonempty")
                              if os.path.lexists(target) else "absent")
        self._import_started = True
        for w in self.body.winfo_children(): w.destroy()

        ctk.CTkLabel(self.body, text=f"正在导入 {name}…",
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(20, 8))
        self.stage_label = ctk.CTkLabel(self.body, text="准备中…", text_color="gray")
        self.stage_label.pack()
        self.progress_bar = ctk.CTkProgressBar(self.body, width=560)
        self.progress_bar.set(0); self.progress_bar.pack(pady=(15, 6))
        self.detail_label = ctk.CTkLabel(self.body, text="", text_color="gray",
                                          font=ctk.CTkFont(size=11))
        self.detail_label.pack()

        job = f"导入整合包 {name}"
        self.app._busy_jobs.add(job)
        threading.Thread(target=self._run_import,
                         args=(name, parent_dir, job), daemon=True).start()

    def _run_import(self, name, parent_dir, job):
        result, fatal = None, None
        try:
            result = mp.import_modpack(
                archive_path=self.archive_path,
                server_name=name,
                parent_dir=parent_dir,
                env_manager=self.app.env,
                installer=self.app.installer,
                downloader=self.app.downloader,
                # 后台线程回调：只投递，窗口关了就丢弃（不能抛异常打断导入）
                progress_callback=lambda prog: self._post(self._on_progress, prog),
                # 复用预览时已解析+查过兼容性的清单，省掉第二轮 Modrinth 查询
                manifest=self.manifest,
            )
        except Exception as e:
            # import_modpack 按约定不抛；这里兜底，保证窗口一定落到可见的错误状态。
            # 先绑定成字符串：except 结束后 e 会被删除（原 lambda 引用 e → NameError → 永远卡住）。
            traceback.print_exc()
            fatal = f"导入异常：{type(e).__name__}: {e}"
        finally:
            self.app._busy_jobs.discard(job)
        if result is not None and result.success:
            # 在后台线程里直接登记：用户中途关掉导入窗口，服务器也不会从列表里"丢失"
            self._register_imported(name, result)
        if self._closed:
            # 窗口已关：结果挂到主窗口上告诉用户
            if fatal or result is None:
                _post_ui(self.app, self.app._show_error, "整合包导入失败", fatal or "未知错误")
            elif result.success:
                _post_ui(self.app, self.app._show_error, "整合包导入完成",
                         f"{name} 已导入（{result.server_path}），可在「版本管理」中查看。")
            else:
                _post_ui(self.app, self.app._show_error, "整合包导入失败",
                         (result.error or "未知错误") + self._leftover_note(result.server_path))
            return
        if fatal or result is None:
            self._post(self._show_fatal, fatal or "导入异常：未知错误")
        else:
            self._post(self._on_done, name, result)

    def _register_imported(self, name, result):
        """Auto-register the imported server so version-mgmt page picks it up.
        （后台线程调用；注册表有问题时把说明挂到主窗口上，不能只 print 掉。）"""
        reg = self.app.registry
        ok, _res, note = _registry_change(reg, reg.add, RegistryEntry(
            name=name, path=result.server_path,
            loader=result.manifest.loader if result.manifest else "",
            mc_version=result.manifest.mc_version if result.manifest else "",
        ))
        if note:
            print(f"[警告] {note}")
            if not ok:
                note += "\n\n服务器已导入成功，但可能不会出现在「版本管理」列表里。"
            _post_ui(self.app, self.app._show_error, "实例列表提示", note)

    STAGE_TEXT = {
        "parsing": "解析中",
        "creating_server": "创建服务端",
        "downloading_files": "下载模组",
        "checking_compat": "检查兼容性",
        "applying_overrides": "应用 overrides",
        "done": "完成",
    }

    def _on_progress(self, prog):
        stage_text = self.STAGE_TEXT.get(prog.stage, prog.stage)
        self.stage_label.configure(text=f"阶段：{stage_text}")
        self.detail_label.configure(text=prog.message)
        if prog.total > 0:
            self.progress_bar.set(prog.current / prog.total)

    def _on_done(self, name, result):
        warnings = list(getattr(result, "warnings", None) or [])
        if result.success:
            partial = bool(result.files_failed or warnings)
            for w in self.body.winfo_children(): w.destroy()
            ctk.CTkLabel(self.body, text="⚠️" if partial else "🎉",
                         font=ctk.CTkFont(size=40)).pack(pady=(18, 4))
            ctk.CTkLabel(self.body, text="导入完成（部分文件失败）" if result.files_failed
                         else ("导入完成（有提示）" if warnings else "导入完成"),
                         font=ctk.CTkFont(size=18, weight="bold"),
                         text_color="#e0c97a" if partial else None).pack()
            summary = (f"安装文件: {result.files_installed}   "
                       f"跳过客户端: {result.files_skipped_client}   "
                       f"失败: {result.files_failed}")
            ctk.CTkLabel(self.body, text=summary, text_color="gray").pack(pady=6)
            ctk.CTkLabel(self.body, text=_short_path(result.server_path, 60),
                         text_color="#888", font=ctk.CTkFont(size=11)).pack()
            if warnings:
                self._render_warnings(warnings)
            # Transparent bypass notice — explain that we used the same trick
            # HMCL / PrismLauncher / etc use, so users understand and can
            # support authors if they want.
            if result.bypassed_mods:
                self._render_bypass_notice(result.bypassed_mods)
            row = ctk.CTkFrame(self.body, fg_color="transparent"); row.pack(pady=14)
            ctk.CTkButton(row, text="去版本管理查看", width=160,
                          fg_color="#2b719e", hover_color="#1f538d",
                          command=self._go_versions).pack(side="left", padx=8)
            ctk.CTkButton(row, text="关闭", width=100, fg_color="#3d3d3d",
                          hover_color="#4d4d4d", command=self.destroy).pack(side="left", padx=8)
        else:
            msg = (f"导入失败：{result.error or '未知错误'}\n\n"
                   f"已下载 {result.files_installed}，失败 {result.files_failed}。")
            msg += self._leftover_note(result.server_path)
            self._show_fatal(msg)
            if warnings:
                self._render_warnings(warnings, before=self.body.winfo_children()[-1])

    def _leftover_note(self, server_path):
        """导入失败后关于目标文件夹的说明。只有本次导入【新建】的文件夹才说"可手动删除"——
        原本就存在的文件夹（用户已有的服务器）绝不能这样标注。"""
        p = server_path
        target = getattr(self, "_import_target", None)
        if not p or not target or not os.path.isdir(p):
            return ""
        if path_key(p) != path_key(target):
            return ""
        state = getattr(self, "_target_state", "nonempty")
        if state == "absent":
            return f"\n已生成的文件夹（可手动删除）：{p}"
        if state == "empty" and not dir_is_effectively_empty(p):
            return f"\n文件夹原本是空的，导入过程中写入了部分文件，可以手动清理：{p}"
        return ""

    def _go_versions(self):
        app = self.app
        self.destroy()
        app.show_versions()

    def _render_warnings(self, warnings, before=None, max_height=150):
        """把 ImportResult.warnings（失败文件、需要注意的事项）逐条列出来，可滚动、可复制。"""
        box = ctk.CTkTextbox(self.body, height=min(max_height, 22 * len(warnings) + 16),
                             fg_color="#2a2415", text_color="#e0c97a",
                             font=ctk.CTkFont(size=11), wrap="word")
        box.insert("1.0", "\n".join(f"• {w}" for w in warnings))
        box.configure(state="disabled")
        if before is not None:
            box.pack(fill="x", padx=20, pady=(8, 4), before=before)
        else:
            box.pack(fill="x", padx=20, pady=(8, 4))

    def _render_bypass_notice(self, bypassed):
        """Honest disclosure: these mods' authors opted out of third-party API
        but we downloaded via CDN anyway (same as HMCL / PrismLauncher).
        Showing them lets the user choose to support those authors at CF."""
        wrap = ctk.CTkFrame(self.body, fg_color="#2a2415", corner_radius=10,
                            border_width=1, border_color="#7a6520")
        wrap.pack(fill="x", padx=20, pady=(10, 4))
        n = len(bypassed)
        ctk.CTkLabel(
            wrap,
            text=f"ℹ️ 有 {n} 个 mod 是用备用方式下载的",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#e0c97a",
        ).pack(anchor="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(
            wrap,
            text=("这些 mod 的作者在 CurseForge 设置了"
                  "「只允许用官方启动器下载」。HMSL 跟 HMCL 等主流启动器一样，"
                  "通过备用链接帮你装好了。如果你喜欢这些 mod，"
                  "建议去对应页面给作者点支持："),
            text_color="#bbb", font=ctk.CTkFont(size=11),
            wraplength=620, justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 4))
        # Scrollable so 20+ mods don't blow out the window
        listframe = ctk.CTkScrollableFrame(wrap, height=min(140, 24 * n + 10),
                                            fg_color="transparent")
        listframe.pack(fill="x", padx=8, pady=(0, 8))
        for m in bypassed:
            line = ctk.CTkFrame(listframe, fg_color="transparent")
            line.pack(fill="x", anchor="w")
            ctk.CTkLabel(line, text=f"  • {m.get('name', '?')}",
                         text_color="#ddd",
                         font=ctk.CTkFont(size=11), anchor="w").pack(side="left")
            url = m.get("cf_url", "")
            if url:
                ctk.CTkLabel(line, text=url, text_color="#6f9fd0",
                             font=ctk.CTkFont(size=10),
                             anchor="w").pack(side="left", padx=(8, 0))


_WIN_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _default_name_from(modpack_name: str) -> str:
    """Sanitize a modpack title into a filesystem-safe folder name.
    除了 <>:"/\\|?* 还要处理控制字符、结尾的点/空格（Windows 会悄悄去掉，导致注册表路径
    和真实文件夹对不上）和 CON/NUL/COM1 这类保留设备名（"创建"成功但写不进任何文件）。
    结果再过一遍 validate_server_name，仍不合法就返回空串（调用方会用默认名）。
    emoji 等增补平面字符（Java 无法从这样的目录加载 jar）直接去掉，而不是让整个名字作废：
    "🌲 Forest Pack" → "Forest Pack"。"""
    bad = '<>:"/\\|?*'
    # 去掉增补平面字符，以及去掉 emoji 后残留的零宽连接符 / 变体选择符
    text = "".join(c for c in (modpack_name or "")
                   if ord(c) <= 0xFFFF and c not in "‍︎️")
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in text)
    out = re.sub(r" {2,}", " ", out)             # "A 🎮 B" 去掉 emoji 后的双空格
    out = out.strip()[:60].rstrip(" .")
    stem, dot, rest = out.partition(".")
    if stem.rstrip(" ").upper() in _WIN_RESERVED_NAMES:
        out = stem.rstrip(" ") + "_" + dot + rest
    if not out or validate_server_name(out):
        return ""
    return out


def _registry_change(registry, op, *args):
    """registry.add / registry.remove 的包装，可在后台线程调用。返回 (ok, 返回值, 提示或 None)。
    注册表文件读不了时 add() 会抛 RegistryCorruptError（ValueError 子类）而不是覆盖它；
    文件内容损坏时会先备份成 instances.json.corrupt-<时间> 再写，并在 last_warning 里说明。
    这些都要告诉用户（原来只 print，打包成 exe 后用户完全看不到）。"""
    try:
        registry.last_warning = None
    except AttributeError:
        pass
    ok, res, note = True, None, None
    try:
        res = op(*args)
    except Exception as e:
        ok = False
        note = f"更新实例列表（{getattr(registry, 'path', 'instances.json')}）失败：{e}"
    warn = getattr(registry, "last_warning", None)
    if warn and warn not in (note or ""):
        note = f"{note}\n{warn}" if note else warn
    return ok, res, note


def _next_free_name(parent_dir, name):
    """name_2、name_3……中第一个在 parent_dir 下还不存在（或只是个空文件夹）的合法名称；
    实在找不到返回 None。"""
    for i in range(2, 100):
        suffix = f"_{i}"
        cand = name[:100 - len(suffix)].rstrip(" .") + suffix
        if validate_server_name(cand):
            continue
        p = os.path.join(parent_dir, cand)
        if dir_is_effectively_empty(p):      # 不存在，或只有 .DS_Store / desktop.ini 之类
            return cand
    return None


def _ask_new_name_for_existing(master, app, parent_dir, name, action="创建", extra=""):
    """目标文件夹已存在且不为空时调用。

    create_server 不会往非空文件夹里安装（新旧文件混在一起容易出问题），也不会改动或删除
    其中的任何文件——所以这里【不再】提供"仍然创建 / 合并覆盖"（那个选项必然失败），
    只让用户换名称：可以一键改用建议的新名称，或者回去自己改。
    返回要改用的新名称；用户选择返回修改时返回 None。"""
    target = os.path.join(parent_dir, name)
    new_name = _next_free_name(parent_dir, name)
    msg = (f"文件夹已存在且不为空：\n{target}\n\n"
           f"HMSL 不会{action}到已有的文件夹里，也不会改动或删除其中的任何文件。\n")
    if extra:
        msg += f"\n{extra}\n"
    if not new_name:
        app._show_error("⚠️ 目标文件夹已存在", msg + "\n请换一个名称。", parent=master)
        return None
    msg += f"\n可以改用新名称「{new_name}」继续{action}，或返回自己换一个名称。"
    dlg = ConfirmDialog(master, title="⚠️ 目标文件夹已存在", msg=msg,
                        ok_text="改用新名称继续", cancel_text="返回修改")
    master.wait_window(dlg)
    return new_name if dlg.result else None


def _strip_long_prefix(p):
    """把 _long_path 加的 \\\\?\\ 前缀去掉，给用户看普通路径。"""
    p = str(p)
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def _os_error_text(e, replace=()):
    """给用户看的 OSError 文本：不带 \\\\?\\ 前缀、不带 repr 的双反斜杠。
    replace：[(旧串, 新串)]，例如把卸载用的临时文件夹名换回原名。"""
    if isinstance(e, OSError) and e.strerror:
        code = ""
        if getattr(e, "winerror", None):
            code = f"[WinError {e.winerror}] "
        elif e.errno:
            code = f"[Errno {e.errno}] "
        paths = []
        for fn in (e.filename, e.filename2):
            if fn is None or fn == "":
                continue
            if isinstance(fn, bytes):
                fn = os.fsdecode(fn)
            paths.append(_strip_long_prefix(fn))
        text = code + str(e.strerror).strip()
        if paths:
            text += "：" + " → ".join(paths)
    else:
        text = str(e) or type(e).__name__
        text = text.replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")
    for old, new in replace:
        if old:
            text = text.replace(old, new)
    return text


# ===== Config editors (embedded as Frames in the detail page's Tab) =====

# server.properties known fields with Chinese labels + widget specs.
# Anything not listed here goes to the bottom "raw" textbox so we never
# lose values we don't have a visual for.
#
# Spec tuple shapes by type:
#   ("bool", key, label, hint)
#   ("int",  key, label, hint, min, max)
#   ("str",  key, label, hint)
#   ("choice", key, label, hint, [options])
_SERVER_PROP_GROUPS = [
    ("性能 / 网络", [
        ("int",    "max-players", "最大玩家数", "上限玩家数量", 1, 200),
        ("int",    "view-distance", "视距", "区块加载半径，越大越吃 CPU/内存", 3, 32),
        ("int",    "simulation-distance", "模拟距离", "实体/方块模拟范围", 3, 32),
        ("int",    "server-port", "端口", "默认 25565", 1, 65535),
        ("int",    "network-compression-threshold", "网络压缩阈值", "≥此字节的包压缩；-1 关闭", -1, 1500),
    ]),
    ("玩法", [
        ("choice", "difficulty", "难度", "", ["peaceful", "easy", "normal", "hard"]),
        ("choice", "gamemode", "默认游戏模式", "", ["survival", "creative", "adventure", "spectator"]),
        ("bool",   "hardcore", "极限模式", "死亡后变旁观者"),
        ("bool",   "pvp", "PVP", "允许玩家互相攻击"),
        ("bool",   "allow-flight", "允许飞行", "勾上才不会把飞行 mod 踢出"),
        ("bool",   "allow-nether", "允许进入下界", ""),
        ("int",    "spawn-protection", "出生点保护", "半径内方块只能 OP 改", 0, 32),
    ]),
    ("访问 / 安全", [
        ("bool",   "online-mode", "正版验证", "关闭后离线玩家也能进，但安全风险大"),
        ("bool",   "white-list", "启用白名单", ""),
        ("bool",   "enforce-whitelist", "强制白名单", "踢出不在白名单上的在线玩家"),
        ("bool",   "enable-command-block", "启用命令方块", ""),
        ("int",    "op-permission-level", "OP 权限等级", "1-4，4 最高", 1, 4),
    ]),
    ("世界", [
        ("str",    "level-name", "主世界文件夹名", "默认 world"),
        ("str",    "level-seed", "世界种子", "留空 = 随机"),
        ("bool",   "generate-structures", "生成结构", "村庄/神殿/要塞等"),
        ("str",    "motd", "服务器说明", "玩家列表显示的副标题"),
    ]),
]

# 文件里没有该字段（或文件还不存在）时，表单显示 Minecraft 自己的默认值。
# 保存时只写【文件里原有的】或【用户真改过的】字段，没动过的默认值不会被写进文件——
# 原实现会把所有字段都写出去：新服务器点一次保存就变成 online-mode=false、pvp=false、
# allow-nether=false、difficulty=peaceful、各数字字段为空。
_MC_PROP_DEFAULTS = {
    "max-players": "20", "view-distance": "10", "simulation-distance": "10",
    "server-port": "25565", "network-compression-threshold": "256",
    "difficulty": "easy", "gamemode": "survival", "hardcore": False, "pvp": True,
    "allow-flight": False, "allow-nether": True, "spawn-protection": "16",
    "online-mode": True, "white-list": False, "enforce-whitelist": False,
    "enable-command-block": False, "op-permission-level": "4",
    "level-name": "world", "level-seed": "", "generate-structures": True,
    "motd": "A Minecraft Server",
}


def _short_path(path: str, max_chars: int = 55) -> str:
    """
    Left-truncate a path so the rightmost portion (with the filename) stays
    visible, prepending '…' when truncated. Keeps the UI predictable when
    paths are long, instead of letting labels overflow their containers.

        _short_path("/Users/alice/Desktop/.../server.properties", 50)
        # → '…op/projects/my-server/server.properties'
    """
    if not path:
        return ""
    if len(path) <= max_chars:
        return path
    return "…" + path[-(max_chars - 1):]


def _enable_macos_trackpad_scroll(scrollable_frame: ctk.CTkScrollableFrame) -> None:
    """
    Workaround for a CustomTkinter ≤5.2.x quirk: child widgets inside a
    CTkScrollableFrame consume macOS trackpad <MouseWheel> events instead of
    letting them propagate to the inner canvas. Mouse-wheel works (different
    event path) but two-finger scroll on a trackpad doesn't.

    Walk all current descendants of the scrollable frame and forward their
    MouseWheel events to the underlying canvas's yview_scroll.
    """
    if sys.platform != "darwin":
        return
    canvas = getattr(scrollable_frame, "_parent_canvas", None)
    if canvas is None:
        return

    def on_wheel(event):
        # macOS sends event.delta as small ints (e.g. -1/+1); negate to match
        # natural scroll direction.
        canvas.yview_scroll(int(-1 * event.delta), "units")
        return "break"

    def bind_recursive(w):
        try:
            w.bind("<MouseWheel>", on_wheel, add="+")
        except Exception:
            pass
        for child in w.winfo_children():
            bind_recursive(child)

    bind_recursive(scrollable_frame)


def _resolve_data(name, script_dir):
    """找数据文件（如 MOD_DATABASE.md）：优先程序/脚本旁边，其次 PyInstaller 打包内
    (_MEIPASS)。这样单文件 exe 把数据打进包里也能读到，Mac .app 放旁边也能读到。"""
    p = os.path.join(script_dir, name)
    if os.path.isfile(p):
        return p
    base = getattr(sys, "_MEIPASS", None)
    if base:
        pb = os.path.join(base, name)
        if os.path.isfile(pb):
            return pb
    return p


def _rlog(msg):
    """（调试期渲染探针，已停用为空操作；保留函数签名，各调用点无需改动。）"""
    return


def _safe_listdir(path):
    """os.listdir 的兜底版：目录不存在/无权限时返回 []，不抛 OSError。"""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _raise_toplevel(top, master=None):
    """Windows 上新建的 CTkToplevel 常被压在主窗口下面（用户以为没弹窗、点了没反应）。
    给了 master 就设为它的 transient（小提示框），并在显示后抬到最前。macOS 行为不变。"""
    if sys.platform != "win32":
        return
    try:
        if master is not None:
            top.transient(master)
        top.lift()

        def _front():
            try:
                if top.winfo_exists():
                    top.lift()
                    top.focus_force()
            except tk.TclError:
                pass
        top.after(150, _front)
    except tk.TclError:
        pass


# ---- 彻底卸载的护栏 ----

# 文件夹里至少有其中之一，才像一台服务器（或者它在 HMSL 的实例注册表里）
_SERVER_MARKERS = ("server.properties", "eula.txt", "start.bat", "start.sh",
                   "run.bat", "run.sh", "hmsl_launch.json", ".hmsl.json")


def _guard_key(p):
    """比较用的规范形式：绝对路径 + normpath + 不分大小写（NTFS/APFS 默认都不分大小写）。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(p))).casefold()


def _guard_contains(parent_k, child_k):
    """child 等于 parent，或位于 parent 之内（参数都已是 _guard_key 形式）。"""
    if parent_k == child_k:
        return True
    prefix = parent_k if parent_k.endswith(os.sep) else parent_k + os.sep
    return child_k.startswith(prefix)


def _guard_protected_paths(extra=()):
    """返回 (keep, system)：
    keep   —— 这些目录本身及其任何上级都不能删（家目录、AppData、Program Files、HMSL 自己的目录……）；
    system —— 这些目录里面的任何东西都不能删（Windows 目录、Program Files、/System、/usr……）。"""
    keep, system = _guard_protected_raw(extra)
    keys_keep, keys_sys = set(), set()
    for paths, keys in ((keep, keys_keep), (system, keys_sys)):
        for p in paths:
            forms = [p]
            unc = _mapped_drive_unc(p) if sys.platform == "win32" else None
            if unc:
                forms.append(unc)        # 家目录在映射的网络盘上（H:）时，它的 UNC 写法也受保护
            for f in forms:
                try:
                    keys.add(_guard_key(f))
                    keys.add(_guard_key(os.path.realpath(f)))
                except (OSError, ValueError):
                    pass
    return keys_keep, keys_sys


def _file_id(p):
    """目录的 (卷序列号, 文件 ID)。同一个目录无论怎么写路径（\\\\localhost\\C$\\Users、
    映射的网络盘、subst 盘符、8.3 短名、联接点）都得到同一个值；拿不到返回 None。"""
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    if not st.st_ino:
        return None          # 个别文件系统不提供文件 ID：不参与比较，免得误判
    return (st.st_dev, st.st_ino)


def _path_and_parents(p):
    """p 本身及它的每一级上级目录（到盘符根 / 共享根为止）。"""
    out = []
    cur = os.path.normpath(p)
    for _ in range(64):
        out.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return out


def _is_local_host(host):
    """UNC 路径里的主机名是不是本机（localhost / 127.x / ::1 / 本机名 / 本机 IP）。"""
    h = (host or "").strip("[]").lower()
    if not h:
        return False
    if h in ("localhost", "::1", "0:0:0:0:0:0:0:1", ".") or h.startswith("127."):
        return True
    names = {(os.environ.get("COMPUTERNAME") or "").lower()}
    try:
        import socket
        hn = socket.gethostname()
        names.add(hn.lower())
        try:
            names.update(ip.lower() for ip in socket.gethostbyname_ex(hn)[2])
        except OSError:
            pass
    except Exception:
        pass
    names.discard("")
    return h in names or h.split(".")[0] in names


def _local_share_path(share):
    """本机普通共享（非 C$/ADMIN$）对应的本地目录，从 LanmanServer 的共享表里查；查不到返回 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\LanmanServer\Shares") as k:
            i = 0
            while True:
                try:
                    name, val, _t = winreg.EnumValue(k, i)
                except OSError:
                    return None
                i += 1
                if name.lower() == share.lower():
                    for item in (val if isinstance(val, list) else [val]):
                        if isinstance(item, str) and item.lower().startswith("path="):
                            return item[5:]
                    return None
    except Exception:
        return None


def _mapped_drive_unc(p):
    """映射的网络盘（Z:\\x）→ 它背后的 UNC 路径（\\\\server\\share\\x）；不是网络盘返回 None。"""
    drive, tail = os.path.splitdrive(p)
    if len(drive) != 2 or drive[1] != ":":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        if ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 4:      # DRIVE_REMOTE
            return None
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(n)) != 0:
            return None
        return buf.value.rstrip("\\") + tail
    except Exception:
        return None


def _windows_local_aliases(p):
    """Windows：同一目录的本地写法。\\\\本机\\C$\\x → C:\\x，\\\\本机\\ADMIN$\\x → %SystemRoot%\\x，
    本机其它共享按共享表换成本地目录；映射的网络盘先换成 UNC 再照此处理。
    文件身份（_file_id）在某些环境里对 UNC 和本地路径不一致（例如打包应用的 AppData 虚拟化），
    所以护栏还要按这些本地写法再比一遍字符串。非 Windows 返回 []。"""
    if sys.platform != "win32" or not p:
        return []
    out = []
    srcs = [str(p)]
    unc = _mapped_drive_unc(str(p))
    if unc:
        srcs.append(unc)
    for s in srcs:
        s = s.replace("/", "\\")
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\\\" + s[8:]
        if not s.startswith("\\\\") or s.startswith("\\\\?\\") or s.startswith("\\\\.\\"):
            continue
        parts = s[2:].split("\\")
        if len(parts) < 2 or not parts[0] or not parts[1] or not _is_local_host(parts[0]):
            continue
        share, rest = parts[1], [x for x in parts[2:] if x]
        if re.fullmatch(r"[A-Za-z]\$", share):
            base = share[0].upper() + ":\\"
        elif share.upper() == "ADMIN$":
            base = os.environ.get("SystemRoot") or os.environ.get("windir")
        else:
            base = _local_share_path(share)
        if base:
            out.append(os.path.join(base, *rest) if rest else base)
    return out


def _guard_identity_hit(cands, extra=()):
    """按"文件身份"而不是路径字符串再查一遍护栏，返回错误信息或 None。
    realpath 不会把 UNC 别名（\\\\localhost\\C$\\Users、\\\\本机名\\C$\\Windows、
    映射到本机共享的网络盘）还原成本地路径，单靠字符串比较会放过它们。"""
    keep, system = _guard_protected_raw(extra)
    cache = {}

    def fid(p):
        if p not in cache:
            cache[p] = _file_id(p)
        return cache[p]

    keep_ids = set()                    # 受保护目录及其所有上级
    for k in keep:
        for q in _path_and_parents(k):
            i = fid(q)
            if i:
                keep_ids.add(i)
    sys_ids = {i for i in (fid(s) for s in system) if i}
    for c in cands:
        chain = _path_and_parents(c)
        ci = fid(chain[0])
        if ci and ci in keep_ids:
            return "路径过于敏感（系统目录、家目录或其上级），拒绝执行"
        for q in chain:
            qi = fid(q)
            if qi and qi in sys_ids:
                return "路径位于系统目录内，拒绝执行"
    return None


def _guard_protected_raw(extra=()):
    """_guard_protected_paths 的原始路径列表版本：(keep, system)。"""
    home = os.path.expanduser("~")
    keep = [home, os.path.dirname(home)]
    homes = {home}
    if sys.platform == "win32":
        for h in (os.environ.get("USERPROFILE"),
                  (os.environ.get("HOMEDRIVE") or "") + (os.environ.get("HOMEPATH") or "")):
            if h and os.path.isabs(h):
                homes.add(h)
    for h in homes:
        for sub in ("Desktop", "Documents", "Downloads", "桌面", "文档", "下载"):
            keep.append(os.path.join(h, sub))
    system = []
    if sys.platform == "win32":
        env = os.environ
        for var in ("USERPROFILE", "PUBLIC", "APPDATA", "LOCALAPPDATA", "ProgramData",
                    "ALLUSERSPROFILE", "OneDrive", "TEMP", "TMP"):
            if env.get(var):
                keep.append(env[var])
        if env.get("USERPROFILE"):
            keep.append(os.path.dirname(env["USERPROFILE"]))          # C:\Users
        if env.get("PUBLIC"):
            keep.append(os.path.dirname(env["PUBLIC"]))
        if env.get("HOMEDRIVE") and env.get("HOMEPATH"):
            keep.append(env["HOMEDRIVE"] + env["HOMEPATH"])
        for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)",
                    "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)"):
            if env.get(var):
                keep.append(env[var])
                system.append(env[var])
    else:
        keep += ["/", "/Users", "/Applications", "/System", "/Library", "/etc", "/var",
                 "/tmp", "/usr", "/bin", "/sbin", "/opt", "/private", "/Volumes",
                 "/home", "/root", os.path.join(home, "Library")]
        system += ["/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc"]
    keep += [p for p in extra if p]
    return keep, system


def _check_delete_target(path, extra_protected=(), is_registered=False):
    """"彻底卸载"前的护栏（只检查、不删除）。返回 (ok, 错误信息, 规范化的绝对路径)。

    拒绝：空路径、相对/盘符相对路径（"D:" 会解析成 D 盘的当前目录）、\\\\.\\ 设备路径、
    盘符根 / UNC 共享根、家目录及其任何上级、AppData / ProgramData / Program Files /
    Windows 目录等系统位置（比较前统一大小写和分隔符，并解析联接点/符号链接），
    HMSL 自己所在的目录，符号链接/联接点本身，以及看起来不像服务器的文件夹
    （没有 server.properties / eula.txt / 启动脚本等，且不在实例注册表里）。"""
    if not path or not str(path).strip():
        return False, "路径为空", ""
    p = str(path).strip()
    if sys.platform == "win32":
        if p.startswith("\\\\.\\") or p.startswith("//./"):
            return False, f"拒绝删除设备路径：{p}", p
        if p.startswith("\\\\?\\UNC\\"):
            p = "\\\\" + p[8:]
        elif p.startswith("\\\\?\\"):
            p = p[4:]
        drive, tail = os.path.splitdrive(p)
        if not drive:
            return False, f"路径不完整（不是绝对路径），拒绝执行：{p}", p
        if len(drive) == 2 and drive[1] == ":" and (not tail or tail[0] not in "\\/"):
            # "D:" / "D:foo" 是相对 D 盘【当前目录】的路径，可能解析到任意位置
            return False, f"路径不完整（盘符相对路径），拒绝执行：{p}", p
    elif not os.path.isabs(p):
        return False, f"路径不完整（不是绝对路径），拒绝执行：{p}", p

    abs_path = os.path.normpath(os.path.abspath(p))
    drive, tail = os.path.splitdrive(abs_path)
    if tail.strip("\\/") == "":
        return False, f"拒绝删除磁盘或共享的根目录：{abs_path}", abs_path
    if not os.path.isdir(abs_path):
        return False, f"目录不存在：{abs_path}", abs_path
    if os.path.ismount(abs_path):
        return False, f"这是一个磁盘/卷的挂载点，拒绝删除：{abs_path}", abs_path
    if os.path.islink(abs_path) or (hasattr(os.path, "isjunction") and os.path.isjunction(abs_path)):
        return False, (f"这是一个符号链接/联接点，而不是真正的服务器文件夹，拒绝删除：\n{abs_path}"), abs_path

    try:
        real_path = os.path.realpath(abs_path)
    except (OSError, ValueError):
        real_path = abs_path
    # 本机 UNC 共享 / 映射盘的别名（\\localhost\C$\Users → C:\Users）也要一起比对
    cand_paths = [abs_path, real_path]
    for x in (abs_path, real_path):
        for alias in _windows_local_aliases(x):
            if alias not in cand_paths:
                cand_paths.append(alias)
    cand = set()
    for x in cand_paths:
        try:
            cand.add(_guard_key(x))
        except (OSError, ValueError):
            pass
    keep, system = _guard_protected_paths(extra_protected)
    for c in cand:
        if os.path.splitdrive(c)[1].strip("\\/") == "":
            return False, f"拒绝删除磁盘或共享的根目录：{abs_path}", abs_path
        for k in keep:
            if _guard_contains(c, k):          # c 就是受保护目录，或是它的上级
                return False, f"路径过于敏感（系统目录、家目录或其上级），拒绝执行：{abs_path}", abs_path
        for s in system:
            if _guard_contains(s, c):          # c 位于系统目录之内
                return False, f"路径位于系统目录内，拒绝执行：{abs_path}", abs_path
    # 同一目录的别名（UNC 管理共享、映射盘、subst 等）字符串对不上，再按文件身份查一遍
    hit = _guard_identity_hit(cand_paths, extra_protected)
    if hit:
        return False, f"{hit}：{abs_path}", abs_path

    if not is_registered and not _looks_like_server_dir(abs_path):
        return False, ("这个文件夹看起来不像 Minecraft 服务器（没有 server.properties、eula.txt、"
                       f"启动脚本、服务端 jar 等），为防误删，拒绝执行：\n{abs_path}"), abs_path
    return True, None, abs_path


def _looks_like_server_dir(path):
    """卸载护栏的"像不像服务器"判断，与版本列表的扫描规则一致（instance_scanner：
    标志文件、认得出的服务端 jar、libraries/net/minecraft/server），
    否则列表里显示出来的服务器会卸载不掉。"""
    if any(os.path.exists(os.path.join(path, m)) for m in _SERVER_MARKERS):
        return True
    if _scanner_looks_like_server is not None:
        try:
            return bool(_scanner_looks_like_server(path))
        except Exception:
            return False
    return os.path.isdir(os.path.join(path, "libraries", "net", "minecraft", "server"))


_LOG_PATH = None     # 无控制台运行（打包的 exe）时，stdout/stderr 被重定向到这个日志文件


def _setup_windowed_logging():
    """打包成 --windowed exe 后 sys.stdout / sys.stderr 是 None：print、traceback、
    Tk 回调异常全部无声消失，出了问题无从排查。这种情况下改写到 ~/.hmsl/hmsl.log
    （超过 1 MB 轮换成 hmsl.log.1）。有控制台（源码运行）时什么都不做。"""
    global _LOG_PATH
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = os.path.join(os.path.expanduser("~"), ".hmsl")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "hmsl.log")
        try:
            if os.path.getsize(path) > 1024 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass
        f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        f.write(f"\n===== HMSL 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f
    _LOG_PATH = path


def _setup_frozen_stdio():
    """打包的 exe（Windows）即使 stdout/stderr 被管道接走，也按系统 ANSI 编码（中文 Windows =
    GBK）输出，且不认 PYTHONIOENCODING；源码运行则是 UTF-8。统一改成 UTF-8（写不出的字符替换掉），
    --route / --dump-tree 等诊断输出在两种运行方式下一致。源码运行、macOS 不受影响。"""
    if not (getattr(sys, "frozen", False) and sys.platform == "win32"):
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError):
            pass


# 退出兜底里等服务器自己保存退出的秒数（正常点 ✕ 走 _on_app_close，等的是 STOP_GRACE_S）
EXIT_FALLBACK_GRACE_S = 10.0


def _stop_leftover_servers(grace=EXIT_FALLBACK_GRACE_S):
    """退出兜底：HMSL 不是经 _on_app_close 退出的（macOS/Linux 上 Ctrl+C、关终端、kill，
    调试用的 --dump-tree 自动退出……）时，由 HMSL 启动的服务器可能还在跑——它们在独立的
    进程组/会话里，不会跟着 HMSL 一起收到信号。向它们发 stop（保存世界），最多等 grace 秒，
    超时强制结束整个进程树。没有在跑的服务器时什么都不做（可重复调用）。"""
    try:
        from core import launcher
        left = launcher.running_servers()
    except Exception:
        return
    if not left:
        return
    try:
        launcher.stop_all(grace=grace)
    except RuntimeError:
        # 解释器退出阶段（atexit）较新的 Python 可能不许再开线程：逐台停
        for sp in left:
            try:
                sp.stop(grace)
            except Exception:
                pass
    except Exception:
        traceback.print_exc()


def _install_exit_signal_handlers():
    """macOS/Linux：Ctrl+C（SIGINT）、关掉终端（SIGHUP）、kill（SIGTERM）时改走正常退出
    （SystemExit），让 _stop_leftover_servers 把 HMSL 启动的服务器正常停掉。以前服务器和 HMSL
    在同一进程组、会一起收到这些信号；现在服务器单独一个会话，收不到了。
    只接管仍是默认处理方式的信号（nohup 设成忽略的 SIGHUP 保持忽略）。Windows 不动。"""
    if sys.platform == "win32":
        return
    import signal

    def _raise_exit(signum, _frame):
        raise SystemExit(128 + signum)

    for name, default in (("SIGINT", signal.default_int_handler),
                          ("SIGHUP", signal.SIG_DFL),
                          ("SIGTERM", signal.SIG_DFL)):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            if signal.getsignal(sig) is default:
                signal.signal(sig, _raise_exit)
        except (ValueError, OSError, TypeError):
            pass


def _run_app(app):
    """app.mainloop()，结束后兜底停掉仍在运行的服务器。mainloop 因 Ctrl+C / SIGTERM /
    --dump-tree 等结束、服务器却还在跑时：先关窗口（别让界面卡着不响应），再停服务器。
    正常点 ✕（或 macOS 的 Cmd+Q）退出时服务器已经停好，这里什么都不做。"""
    try:
        app.mainloop()
    finally:
        try:
            from core.launcher import running_servers
            if running_servers():
                app.destroy()
        except Exception:
            pass
        _stop_leftover_servers()


def _long_path(path):
    """Windows 上给绝对路径加 \\\\?\\ 前缀，绕过 260 字符 MAX_PATH 限制
    （LongPathsEnabled 默认是 0；整合包的深层 config、世界数据很容易超长）。
    其它平台原样返回。只用于实际的文件 I/O，显示和 relpath 仍用普通路径。"""
    if sys.platform != "win32" or not path:
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        return p
    if p.startswith("\\\\"):                   # UNC: \\server\share\x -> \\?\UNC\server\share\x
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _legacy_encoding():
    """系统的"ANSI"编码（中文 Windows = cp936/GBK）。macOS/Linux 一般就是 utf-8。
    优先 locale.getencoding()（3.11+，不受 Python UTF-8 模式影响）。"""
    try:
        enc = locale.getencoding()
    except AttributeError:
        enc = locale.getpreferredencoding(False)
    try:
        return codecs.lookup(enc).name
    except LookupError:
        return "utf-8"


def _detect_newline(data: bytes) -> str:
    if b"\r\n" in data:
        return "\r\n"
    if b"\n" in data:
        return "\n"
    if b"\r" in data:
        return "\r"
    return "\n"


def _read_text_file(path):
    """读文本配置文件并识别编码 + 换行风格。返回 (text, encoding, newline)。
    text 的换行统一成 '\\n'（给 Tk 文本框用）。

    编码按 带BOM的UTF-8 → UTF-8 → 系统 ANSI 编码（中文 Windows 为 GBK）→ latin-1 依次尝试，
    全部严格解码（不用 errors='replace'）：原来"替换"读入、再按 UTF-8 写回，GBK 文件
    不改一个字点保存也会被永久写坏（锟斤拷）。latin-1 能无损往返任意字节，是最后的保底。
    Raises OSError."""
    with open(_long_path(path), "rb") as f:
        data = f.read()
    if data.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    else:
        candidates = ["utf-8"]
        legacy = _legacy_encoding()
        if legacy not in ("utf-8", "latin-1", "iso8859-1"):
            candidates.append(legacy)
        candidates.append("latin-1")
    for enc in candidates:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:                                    # 不会走到（latin-1 永不失败），留个兜底
        enc, text = "latin-1", data.decode("latin-1")
    nl = _detect_newline(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, enc, nl


def _encoding_label(enc):
    """给文件标签显示用的编码名；UTF-8 不显示。"""
    return {"utf-8": "", "utf-8-sig": "UTF-8 BOM", "gbk": "GBK", "gb18030": "GB18030",
            "latin-1": "Latin-1", "iso8859-1": "Latin-1"}.get(enc, enc.upper())


def _backup_then_write(path, content_str, encoding="utf-8", newline="\n", backup=True):
    """Save with a .bak side-copy of the previous version (safety net).
    content_str 用 '\\n' 分行；按 newline 写回（保持原文件的 CRLF / LF），按 encoding 编码。
    先编码再动文件：编码失败（UnicodeEncodeError）时原文件和 .bak 都不会被碰。"""
    data = content_str.replace("\n", newline).encode(encoding)
    lp = _long_path(path)
    if backup and os.path.isfile(lp):
        try:
            shutil.copyfile(lp, lp + ".bak")
        except OSError:
            pass  # backup is best-effort; don't block the save
    with open(lp, "wb") as f:
        f.write(data)


# ---- server.properties 的 Java Properties 转义 ----
# Java 的 Properties.load(InputStream)（老版本 MC 用它读 server.properties）按 ISO-8859-1 解码，
# 直接写 UTF-8 中文/§ 会变乱码；标准做法是写成 \uXXXX。新版 MC 按 UTF-8 读，也认 \uXXXX。
# 界面上只把 \uXXXX 还原成可读字符；\n、\:、\\ 等其它转义原样显示、原样写回，
# 这样 MOTD 里的 "\n" 换行之类照旧能用。

def _prop_unescape_unicode(s: str) -> str:
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nx = s[i + 1]
            if nx == "u" and i + 6 <= n:
                hx = s[i + 2:i + 6]
                try:
                    out.append(chr(int(hx, 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nx == " ":
                # "\ " 是转义空格（_prop_escape_value 给值开头的空格就这么写）：显示成空格，
                # 否则保存时写的 "motd=\  Welcome" 重新载入后会显示成带反斜杠的 "\  Welcome"
                out.append(" ")
                i += 2
                continue
            out.append(c + nx)          # 其它转义（含 "\\\\"）原样保留、整体跳过
            i += 2
            continue
        out.append(c)
        i += 1
    text = "".join(out)
    try:                                 # 合并 \uD83D\uDE00 这种代理对
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return text


def _prop_escape_value(v: str) -> str:
    out = []
    for idx, ch in enumerate(v):
        o = ord(ch)
        if ch == " " and idx == 0:
            out.append("\\ ")             # Java 读取时会吃掉值开头的空格
        elif ch in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[ch])
        elif o < 0x20 or o > 0x7E:
            if o > 0xFFFF:                # 补充平面字符（emoji）→ 两个 \u 代理
                o -= 0x10000
                out.append("\\u%04X\\u%04X" % (0xD800 + (o >> 10), 0xDC00 + (o & 0x3FF)))
            else:
                out.append("\\u%04X" % o)
        else:
            out.append(ch)
    return "".join(out)


def _prop_parse_line(line: str):
    """返回 (key, raw_value)；注释/空行/无 '=' 的行返回 None。"""
    s = line.strip()
    if not s or s[0] in "#!" or "=" not in line:
        return None
    k, _, v = line.partition("=")
    k = k.strip()
    if not k:
        return None
    return k, v


class ServerPropertiesEditor(ctk.CTkFrame):
    """Visual editor for server.properties with grouped sections + raw fallback."""

    def __init__(self, parent, server_path, app):
        super().__init__(parent, fg_color="transparent")
        self.server_path = server_path
        self.app = app
        self.properties_path = os.path.join(server_path, "server.properties")
        self.vars = {}                # key -> StringVar / BooleanVar
        self.original_lines = []      # preserve comments + ordering（不含行尾）
        self.raw_textbox = None       # for unknown keys
        self._snapshot = {}           # key -> 载入后的显示值（判断"用户改没改"）
        self._unknown_loaded = {}     # 载入时的未知字段 key -> 显示值
        self._file_exists = False
        self._load_failed = False
        self._encoding = "utf-8"
        self._newline = "\n"
        self._trailing_nl = True
        self._backed_up = False       # .bak 每个编辑会话只做一次（保住最初的版本）
        # _build_ui 分批构建字段，建完后由 _finish_form 调 _load 填值（不在此提前调 _load，
        # 否则 raw_textbox 等还没建好会崩）。
        self._build_ui()

    def _build_ui(self):
        # Top action bar: buttons FIRST on the right (predictable spot)
        # then path label fills the remaining width with left-truncation.
        top = self._top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(4, 8), padx=4)
        ctk.CTkButton(top, text="💾 保存", width=110, height=34,
                      fg_color="#2b719e", hover_color="#1f538d",
                      command=self._save).pack(side="right")
        ctk.CTkButton(top, text="↻ 重新加载", width=110, height=34,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self._reload).pack(side="right", padx=6)
        ctk.CTkLabel(top, text=_short_path(self.properties_path, 60),
                     text_color="gray", font=ctk.CTkFont(size=11),
                     anchor="w").pack(side="left", padx=(4, 8), fill="x", expand=True)

        # 文件缺失横幅：改用【原生 tk.Label】（纯色、无圆角、无画布）。原来是带圆角背景
        # 的 CTkLabel——这种画布绘制的圆角控件疑为 macOS 卡渲染的元凶之一（2026-09-23 用户
        # 截图显示它渲染成残缺的棕色块）。它只在 server.properties 缺失时显示，而用户测的
        # 实例恰好都缺，所以每次都触发。
        self.banner = tk.Label(
            self, text="", bg="#5a4a2b", fg="#ffe0a3",
            anchor="w", justify="left", wraplength=880, font=(UI_FONT, 12), padx=10, pady=8)

        # ⚠️ Plan C —— 分页显示，不滚动（2026-09-23 用户提议 + 实测确认）。
        # 根因（computer-use 真鼠标点击 + HMSL_SP 逐层二分实锤）：macOS 的 Cocoa 合成器
        # 在一次性渲染"比窗口高、超出可视区"的一堆控件时，会把整个窗口卡死——CTk / 原生
        # tk 都会，与控件类型无关，是"控件铺得超出窗口"本身。「世界/模组」页不卡，是因为
        # 它们把内容装进【一个能自我虚拟化的控件】(tk.Listbox / CTkTextbox) 里，只画看得
        # 见的部分。这里照此思路：每页只放放得下窗口的少量字段（永不溢出），翻页看下一批。
        # Windows(GDI) 基本无此坑，此方案两平台通吃。
        self._init_vars()
        self._sp_pages = self._paginate(fields_per_page=6)
        self._sp_page_idx = 0
        self._unknown_text = ""
        self.raw_textbox = None

        self._page_frame = tk.Frame(self, bg="#2a2a2a", highlightthickness=0)
        self._page_frame.pack(fill="both", expand=True, padx=4, pady=4)

        nav = tk.Frame(self, bg="#242424", highlightthickness=0)
        nav.pack(fill="x", padx=4, pady=(0, 4))
        self._prev_btn = tk.Button(nav, text="◀ 上一页", command=self._sp_prev,
                                   bg="#3a3a3a", fg="#dddddd", activebackground="#4a4a4a",
                                   activeforeground="white", relief="flat", bd=0,
                                   highlightthickness=0, padx=14, pady=4)
        self._prev_btn.pack(side="left", padx=6, pady=6)
        self._next_btn = tk.Button(nav, text="下一页 ▶", command=self._sp_next,
                                   bg="#3a3a3a", fg="#dddddd", activebackground="#4a4a4a",
                                   activeforeground="white", relief="flat", bd=0,
                                   highlightthickness=0, padx=14, pady=4)
        self._next_btn.pack(side="right", padx=6, pady=6)
        self._page_lbl = tk.Label(nav, text="", bg="#242424", fg="#aaaaaa", font=(UI_FONT, 12))
        self._page_lbl.pack(side="left", expand=True)

        self._load()               # 先把值填进 vars（控件还没建也没关系）
        self._show_sp_page(0)      # 建第一页

    def _init_vars(self):
        """预建所有字段变量，与控件解耦：翻页时控件重建，但值一直存在 self.vars 里，
        所以在哪一页填的值、保存时都在，不会因为没翻到某页而丢。"""
        for _group, fields in _SERVER_PROP_GROUPS:
            for spec in fields:
                kind, key = spec[0], spec[1]
                self.vars[key] = (tk.BooleanVar(value=False) if kind == "bool"
                                  else tk.StringVar(value=""))

    def _paginate(self, fields_per_page=6):
        """把 (组名, 字段) 展平后按每页 fields_per_page 个字段切页。返回 [[(group, spec)...]...]。"""
        flat = [(group, spec) for group, fields in _SERVER_PROP_GROUPS for spec in fields]
        pages = [flat[i:i + fields_per_page]
                 for i in range(0, len(flat), fields_per_page)]
        return pages or [[]]

    def _show_sp_page(self, idx, sync=True):
        """显示第 idx 页：销毁旧页、只建这一页的少量字段（放得下窗口、永不溢出）。
        sync=False：刚从文件重新载入过，别再用旧 raw 框的内容覆盖新载入的未知字段。"""
        n = len(self._sp_pages)
        idx = max(0, min(idx, n - 1))
        if sync:
            self._sync_unknown_from_box()      # 翻走前存回 raw 框内容
        self._sp_page_idx = idx
        for c in self._page_frame.winfo_children():
            c.destroy()
        self.raw_textbox = None
        last_group = None
        for group, spec in self._sp_pages[idx]:
            if group != last_group:
                tk.Label(self._page_frame, text=group, bg="#2a2a2a", fg="#dddddd",
                         font=(UI_FONT, 14, "bold"), anchor="w").pack(
                    anchor="w", pady=(10, 4), padx=6)
                last_group = group
            self._build_field(spec)
        if idx == n - 1:               # 最后一页附"未知字段"raw 编辑区
            self._build_raw_section()
        self._page_lbl.configure(text=f"第 {idx + 1} / {n} 页")
        self._prev_btn.configure(state="normal" if idx > 0 else "disabled")
        self._next_btn.configure(state="normal" if idx < n - 1 else "disabled")

    def _sp_prev(self):
        self._show_sp_page(self._sp_page_idx - 1)

    def _sp_next(self):
        self._show_sp_page(self._sp_page_idx + 1)

    def _reload(self):
        """↻ 重新加载：重读文件填值，再重建当前页。"""
        self._load()
        self._show_sp_page(self._sp_page_idx, sync=False)

    def _build_raw_section(self):
        tk.Label(self._page_frame, text="其他 (高级 / 未知字段)", bg="#2a2a2a",
                 fg="#dddddd", font=(UI_FONT, 14, "bold"), anchor="w").pack(
            anchor="w", pady=(14, 4), padx=6)
        tk.Label(self._page_frame, bg="#2a2a2a", fg="#888888", font=(UI_FONT, 11), anchor="w",
                 text="按 key=value 一行一个；保存时会与上面的可视化字段合并写入。").pack(
            anchor="w", padx=6)
        self.raw_textbox = ctk.CTkTextbox(self._page_frame, height=140,
                                          fg_color="#000000", text_color="#cccccc",
                                          font=(MONO_FONT, 11))
        self.raw_textbox.pack(fill="both", expand=True, padx=4, pady=(4, 8))
        self.raw_textbox.delete("1.0", "end")
        self.raw_textbox.insert("1.0", self._unknown_text)

    def _sync_unknown_from_box(self):
        """若 raw 框当前在屏，把它的内容存回 self._unknown_text（翻页/保存前调用）。"""
        try:
            if self.raw_textbox is not None and self.raw_textbox.winfo_exists():
                self._unknown_text = self.raw_textbox.get("1.0", "end-1c")
        except Exception:
            pass

    def _build_field(self, spec):
        # ⚠️ 全部用【原生 tk 控件】而不是 CTk 控件。控制变量对比（2026-09-23 用户实测）：
        # 「世界/模组」页用原生 tk（不卡），本页原来用 ~77 个 CTk 控件（每个内部带自绘画布、
        # 很重），大半还在窗口外，macOS 一次性合成就卡死。换成轻量原生 tk 后即与那两页同构。
        kind = spec[0]
        key = spec[1]
        var = self.vars[key]        # 变量已在 _init_vars 预建，这里只建控件并绑上去
        BG, FG, GRAY, EBG = "#2a2a2a", "#dddddd", "#888888", "#3a3a3a"
        row = tk.Frame(self._page_frame, bg=BG)
        row.pack(fill="x", padx=6, pady=3)

        def _hint(text):
            if text:
                tk.Label(row, text=text, bg=BG, fg=GRAY, font=(UI_FONT, 11),
                         anchor="w").pack(side="left", padx=(4, 0))

        def _entry(width):
            tk.Entry(row, textvariable=var, width=width, bg=EBG, fg=FG,
                     insertbackground=FG, relief="flat", highlightthickness=1,
                     highlightbackground="#4a4a4a", highlightcolor="#5a8cc0").pack(
                side="left", padx=(0, 8))

        if kind == "bool":
            _k, _key, label, hint = spec
            tk.Checkbutton(row, text=label, variable=var, bg=BG, fg=FG,
                           selectcolor=EBG, activebackground=BG, activeforeground=FG,
                           anchor="w", highlightthickness=0, bd=0).pack(
                side="left", padx=(2, 8))
            _hint(hint)
        elif kind == "int":
            _k, _key, label, hint, lo, hi = spec
            tk.Label(row, text=label, width=16, anchor="w", bg=BG, fg=FG).pack(
                side="left", padx=(2, 4))
            _entry(10)
            _hint(f"({lo}–{hi}) {hint}")
        elif kind == "str":
            _k, _key, label, hint = spec
            tk.Label(row, text=label, width=16, anchor="w", bg=BG, fg=FG).pack(
                side="left", padx=(2, 4))
            _entry(32)
            _hint(hint)
        elif kind == "choice":
            _k, _key, label, hint, options = spec
            tk.Label(row, text=label, width=16, anchor="w", bg=BG, fg=FG).pack(
                side="left", padx=(2, 4))
            # 不再在这里 var.set(options[0])：那会把没动过的字段当成"改成 peaceful"写进文件。
            # 缺省值已由 _load 按 Minecraft 默认值填好。
            om = tk.OptionMenu(row, var, *options)
            om.configure(bg=EBG, fg=FG, activebackground="#4a4a4a", activeforeground=FG,
                         highlightthickness=0, bd=0, relief="flat", width=12,
                         anchor="w", takefocus=0)
            om["menu"].configure(bg=EBG, fg=FG, activebackground="#3a5570",
                                 activeforeground="white", bd=0)
            om.pack(side="left", padx=(0, 8))
            _hint(hint)

    @staticmethod
    def _parse_raw_block(text):
        """解析 raw 框：key=value 一行一个（# 注释、空行忽略）。返回有序 dict。
        值只去掉开头的空白（与 Java Properties 和 _load 一致）：结尾的空格是值的一部分
        （如 rcon.password=p@ss␠），原来 strip() 掉后，没动过的未知字段也会被当成"改过"重写。"""
        out = {}
        for line in text.splitlines():
            kv = _prop_parse_line(line)
            if kv is None:
                continue
            k, v = kv
            out[k] = v.lstrip(" \t\f")
        return out

    def _var_value(self, key):
        """变量当前值的规范字符串形式（布尔为 'true'/'false'）。"""
        var = self.vars[key]
        if isinstance(var, tk.BooleanVar):
            try:
                return "true" if var.get() else "false"
            except tk.TclError:
                return "false"
        return str(var.get())

    def _load(self):
        self._load_failed = False
        self.original_lines = []
        self._file_exists = os.path.isfile(_long_path(self.properties_path))
        parsed = {}
        if self._file_exists:
            # 文件存在 —— 确保横幅隐藏（重新加载时可能从"缺失"切到"存在"）
            self.banner.pack_forget()
            try:
                text, self._encoding, self._newline = _read_text_file(self.properties_path)
            except OSError as e:
                self._load_failed = True
                self.app._show_error("读取失败", f"{_os_error_text(e)}\n\n为防止覆盖原文件，本页暂不能保存。")
                text = ""
            self._trailing_nl = text.endswith("\n") or not text
            self.original_lines = text.split("\n")
            if self.original_lines and self.original_lines[-1] == "":
                self.original_lines.pop()
            for line in self.original_lines:
                kv = _prop_parse_line(line)
                if kv is None:
                    continue
                k, v = kv
                # Java 语义：值开头的空白不算；\uXXXX 还原成可读字符（同名 key 以最后一个为准）
                parsed[k] = _prop_unescape_unicode(v.lstrip(" \t\f"))
        else:
            self.banner.configure(
                text="⚠️ 这台服务器还没有 server.properties —— 先到「概览」页点 "
                     "▶ 启动 让它生成一次，或直接在下面改好字段点 💾 保存来创建"
                     "（只会写入你改过的项，其余由 Minecraft 首次启动时按默认值补全）。")
            self.banner.pack(fill="x", padx=4, pady=(0, 8), after=self._top_bar)
            self._encoding, self._newline, self._trailing_nl = "utf-8", os.linesep, True

        # Populate known vars：文件里有就用文件的，没有就显示 Minecraft 默认值
        known_keys = set(self.vars.keys())
        for key, var in self.vars.items():
            if key in parsed:
                raw = parsed[key]
                if isinstance(var, tk.BooleanVar):
                    var.set(raw.strip().lower() == "true")
                else:
                    var.set(raw)
            else:
                default = _MC_PROP_DEFAULTS.get(key, False if isinstance(var, tk.BooleanVar) else "")
                var.set(default)
        # 快照：保存时只写和快照不同（= 用户改过）的字段
        self._snapshot = {k: self._var_value(k) for k in self.vars}

        # Anything not known → raw 文本（存进 _unknown_text，最后一页的 raw 框会读它）
        self._unknown_loaded = {k: v for k, v in parsed.items() if k not in known_keys}
        if self._file_exists:
            self._unknown_text = "\n".join(f"{k}={v}" for k, v in self._unknown_loaded.items())
        else:
            self._unknown_text = (
                "# server.properties 还不存在 —— 先启动一次服务器就会自动生成；\n"
                "# 或在此输入 key=value 一行一个，按保存直接创建。\n")

    def _collect_changes(self):
        """对比快照，算出 (changes, removed)。changes: key -> 新显示值；removed: 从 raw 框删掉的未知 key。
        有非法输入时抛 ValueError(中文提示)。"""
        kinds = {spec[1]: spec for _g, fields in _SERVER_PROP_GROUPS for spec in fields}
        changes = {}
        for key in self.vars:
            cur = self._var_value(key)
            if cur != self._snapshot.get(key):
                changes[key] = cur
        self._sync_unknown_from_box()
        raw = self._parse_raw_block(self._unknown_text)
        for k, v in raw.items():
            if k in self.vars:
                # raw 框里写了已知字段：以 raw 框为准（和原来的合并规则一致）
                if v != self._snapshot.get(k):
                    changes[k] = v
            elif self._unknown_loaded.get(k) != v:
                changes[k] = v
        removed = {k for k in self._unknown_loaded if k not in raw}

        for k, v in changes.items():
            spec = kinds.get(k)
            if not spec:
                continue
            label = spec[2]
            if spec[0] == "int":
                try:
                    int(v.strip())
                except ValueError:
                    raise ValueError(f"「{label}」({k}) 必须是整数，当前是：{v!r}") from None
                changes[k] = v.strip()
            elif k == "level-name" and not v.strip():
                raise ValueError("「主世界文件夹名」(level-name) 不能为空，否则世界会直接生成在服务器根目录。")
        return changes, removed

    def _save(self):
        if self._load_failed:
            self.app._show_error("无法保存", "server.properties 读取失败，为防止覆盖原文件已禁止保存。\n"
                                             "请点「↻ 重新加载」后再试。")
            return
        try:
            changes, removed = self._collect_changes()
        except ValueError as e:
            self.app._show_error("有字段填写不正确", str(e))
            return

        # Re-emit, preserving original line ordering + comments + 没改动的行（逐字保留）
        out = []
        written = set()
        for line in self.original_lines:
            kv = _prop_parse_line(line)
            if kv is None:
                out.append(line); continue
            k = kv[0]
            if k in removed:
                continue                      # key removed by user — just drop the line
            if k in changes:
                if k in written:
                    continue                  # 同名重复行：只保留一行新值
                out.append(f"{k}={_prop_escape_value(changes[k])}")
                written.add(k)
            else:
                out.append(line)
        # Append any changed keys that weren't in the original
        for k, v in changes.items():
            if k not in written:
                out.append(f"{k}={_prop_escape_value(v)}")

        if out == self.original_lines:
            self.app._show_error("没有改动",
                                 "内容没有变化，未写入文件。" if self._file_exists else
                                 "还没有改动任何字段，未创建 server.properties。")
            return

        appended = len(out) > len(self.original_lines)
        content = "\n".join(out) + ("\n" if (self._trailing_nl or appended) else "")
        # 带 BOM 的 UTF-8 写回时去掉 BOM：Minecraft 会把 BOM 当成第一个 key 的一部分而忽略该行
        enc = "utf-8" if self._encoding == "utf-8-sig" else self._encoding
        try:
            _backup_then_write(self.properties_path, content, encoding=enc,
                               newline=self._newline, backup=not self._backed_up)
        except (OSError, UnicodeError) as e:
            self.app._show_error("保存失败", _os_error_text(e))
            return
        had_file = self._file_exists
        self._backed_up = self._backed_up or had_file
        self._load()                                   # 以新文件为基准，下次保存再比对
        self._show_sp_page(self._sp_page_idx, sync=False)
        self.app._show_error("已保存", "server.properties 已保存" +
                             ("\n旧版备份为 server.properties.bak" if had_file else ""))


class _FilePickerEditor(ctk.CTkFrame):
    """Shared base for World/Mod config editors: left file list, right text editor."""

    EXTS = (".toml", ".cfg", ".json", ".properties", ".yml", ".yaml", ".conf", ".txt")

    def __init__(self, parent, server_path, app):
        super().__init__(parent, fg_color="transparent")
        self.server_path = server_path
        self.app = app
        self.current_file = None
        self._items = []            # 与 Listbox 行一一对应的 (label, full_path)
        self._list_errors = 0       # 列目录时读不了的子目录数（给空态/提示行用）
        # 当前打开文件的原始编码 / 换行 / 载入时的文本（判断有没有改动）
        self._enc = "utf-8"
        self._eol = "\n"
        self._loaded_text = None
        self._backed_up = set()     # 本次编辑会话里已做过 .bak 的文件（.bak 只保存最初版本）
        self._wheel_acc = 0.0
        self._build_ui()
        self._refresh_file_list()

    def _build_ui(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=4, pady=4)

        left = ctk.CTkFrame(body, fg_color="#2a2a2a", corner_radius=8, width=264)
        left.pack(side="left", fill="y", padx=(0, 6))
        left.pack_propagate(False)              # 固定左栏宽度
        self._build_top_controls(left)
        # 文件列表用原生 tk.Listbox（单控件承载 N 行、Tk 内置行虚拟化、只画可见行）。
        # 旧实现给每个文件建一个 ctk.CTkButton —— 数百个 config 文件时，这些重控件首次
        # 变可见会被迫一次性绘制，冻死主线程（大整合包进配置页/点模组子标签卡死的根因）。
        list_holder = ctk.CTkFrame(left, fg_color="transparent")
        list_holder.pack(fill="both", expand=True, padx=4, pady=4)
        sb = tk.Scrollbar(list_holder)
        sb.pack(side="right", fill="y")
        self.file_list = tk.Listbox(
            list_holder, activestyle="none", exportselection=False,
            bg="#2a2a2a", fg="#dddddd",
            selectbackground="#3a5570", selectforeground="white",
            highlightthickness=0, borderwidth=0, relief="flat",
            font=(MONO_FONT, 11), yscrollcommand=sb.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        sb.config(command=self.file_list.yview)
        self.file_list.bind("<<ListboxSelect>>", self._on_select)
        self.file_list.bind("<Double-Button-1>", self._on_select)   # 同一行重选也能打开
        self.file_list.bind("<MouseWheel>", self._on_wheel)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)
        self.file_label = ctk.CTkLabel(right, text="(在左侧选一个文件)",
                                        text_color="gray",
                                        font=ctk.CTkFont(size=11), anchor="w")
        self.file_label.pack(anchor="w", padx=4, pady=(0, 4))
        self.text = ctk.CTkTextbox(right, fg_color="#000000",
                                    text_color="#dddddd",
                                    font=(MONO_FONT, 12))
        self.text.pack(fill="both", expand=True, pady=(0, 6))

        bot = ctk.CTkFrame(right, fg_color="transparent"); bot.pack(fill="x")
        ctk.CTkButton(bot, text="💾 保存", width=110, height=34,
                      fg_color="#2b719e", hover_color="#1f538d",
                      command=self._save).pack(side="right")
        ctk.CTkButton(bot, text="↻ 刷新", width=110, height=34,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self._refresh_file_list).pack(side="right", padx=6)

    def _on_wheel(self, e):
        if sys.platform == "darwin":
            # macOS 双指滚动（Listbox 原生 <MouseWheel>，delta 为小整数）—— 行为不变
            self.file_list.yview_scroll(int(-1 * e.delta), "units")
            return "break"
        if sys.platform == "win32":
            # Windows：每格滚轮 delta=±120，原来直接当行数用 → 一格滚 120 行。
            # 按 120 = 3 行换算；精确触控板会发很小的 delta，累加起来再滚，两个方向对称。
            self._wheel_acc += -e.delta * 3 / 120.0
            steps = int(self._wheel_acc)
            if steps:
                self._wheel_acc -= steps
                self.file_list.yview_scroll(steps, "units")
            return "break"
        return None     # X11 走 Button-4/5，交给 Tk 默认绑定

    def _build_top_controls(self, left_panel):
        """Subclasses may add controls above the file list (e.g. world dropdown)."""
        pass

    # Subclasses override these:
    def _list_files(self):
        return []   # returns list of (display_label, full_path)

    def _refresh_file_list(self):
        self.file_list.delete(0, "end")
        self._list_errors = 0
        self._items = self._list_files()
        if not self._items:
            self.file_list.insert("end", self._empty_hint())
            self.file_list.itemconfig(0, foreground="#888888")
            self._items = []        # 提示行不对应文件；_on_select 按长度跳过
        else:
            for label, _full in self._items:
                self.file_list.insert("end", label)
        if self._list_errors:
            # 提示行放在最后，不对应文件（_on_select 按 _items 长度跳过）
            self.file_list.insert("end", f"（有 {self._list_errors} 个目录读取失败，未列出）")
            self.file_list.itemconfig("end", foreground="#e0a060")

    def _on_select(self, _evt=None):
        sel = self.file_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self._items):   # 空态提示行，无对应文件
            return
        self._open_file(self._items[idx][1])

    def _empty_hint(self):
        """子类可覆盖，按场景给更具体的空态文案。"""
        return "(无可编辑文件)"

    def _rel_label(self, path):
        try:
            return os.path.relpath(path, self.server_path)
        except ValueError:          # 不同盘符等
            return path

    def _open_file(self, path):
        try:
            content, enc, eol = _read_text_file(path)
        except OSError as e:
            self.app._show_error("打开失败", _os_error_text(e)); return
        self.current_file = path
        self._enc, self._eol, self._loaded_text = enc, eol, content
        label = _short_path(self._rel_label(path), 70)
        enc_label = _encoding_label(enc)
        if enc_label:
            label += f"   [{enc_label}]"
        self.file_label.configure(text=label)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def _save(self):
        if not self.current_file:
            self.app._show_error("提示", "还没选文件，请在左侧点一个。"); return
        content = self.text.get("1.0", "end-1c")  # strip the trailing newline Tk inserts
        if content == self._loaded_text:
            self.app._show_error("没有改动", f"{os.path.basename(self.current_file)} 内容没有变化，未写入。")
            return
        enc = self._enc
        try:
            content.replace("\n", self._eol).encode(enc)
        except UnicodeEncodeError:
            # 例如 GBK 文件里输入了 emoji：原编码存不下，问用户是否改存 UTF-8（不静默丢字符）
            dlg = ConfirmDialog(
                self.app, title="编码问题",
                msg=(f"{os.path.basename(self.current_file)} 原来是 {_encoding_label(enc) or enc} 编码，"
                     "存不下你输入的部分字符。\n\n改用 UTF-8 保存吗？（读取该文件的模组若只认原编码，"
                     "中文等字符可能显示异常）"),
                ok_text="改存 UTF-8", cancel_text="取消")
            self.app.wait_window(dlg)
            if not dlg.result:
                return
            enc = "utf-8"
        first = self.current_file not in self._backed_up
        try:
            _backup_then_write(self.current_file, content, encoding=enc, newline=self._eol,
                               backup=first)
        except (OSError, UnicodeError) as e:
            self.app._show_error("保存失败", _os_error_text(e)); return
        self._backed_up.add(self.current_file)
        self._enc, self._loaded_text = enc, content
        self.app._show_error("已保存",
                              f"{os.path.basename(self.current_file)} 已保存（打开前的版本备份为 .bak）")


class WorldConfigEditor(_FilePickerEditor):
    """Per-world config editor: world dropdown + files under serverconfig/."""

    def __init__(self, parent, server_path, app):
        self.worlds = []
        self.world_var = None
        super().__init__(parent, server_path, app)

    def _build_top_controls(self, left_panel):
        self.worlds = self._discover_worlds()
        if not self.worlds:
            ctk.CTkLabel(left_panel,
                         text="(未发现已生成的世界)\n服务器需先启动一次\n才会生成 level.dat",
                         text_color="gray", justify="center",
                         font=ctk.CTkFont(size=11)).pack(pady=12, padx=8)
            return
        ctk.CTkLabel(left_panel, text="世界:", anchor="w",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=(8, 2))
        self.world_var = ctk.StringVar(value=self.worlds[0])
        ctk.CTkOptionMenu(left_panel, variable=self.world_var,
                          values=self.worlds, width=220,
                          command=lambda _v: self._refresh_file_list()).pack(padx=8, pady=(0, 8))

    def _discover_worlds(self):
        out = []
        for name in _safe_listdir(self.server_path):
            d = os.path.join(self.server_path, name)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "level.dat")):
                out.append(name)
        return out

    def _list_files(self):
        if not self.world_var:
            return []
        world_dir = os.path.join(self.server_path, self.world_var.get())
        out = []
        # Forge per-world configs live under serverconfig/
        sc = os.path.join(world_dir, "serverconfig")
        for f in _safe_listdir(sc):
            full = os.path.join(sc, f)
            if os.path.isfile(full) and f.lower().endswith(self.EXTS):
                out.append((f"serverconfig/{f}", full))
        # Editable text files at world root
        for f in _safe_listdir(world_dir):
            full = os.path.join(world_dir, f)
            if os.path.isfile(full) and f.lower().endswith(self.EXTS):
                out.append((f, full))
        return out


class ModConfigEditor(_FilePickerEditor):
    """Global mod config editor: walk <server>/config/ for editable files."""

    def _list_files(self):
        cfg_root = os.path.join(self.server_path, "config")
        if not os.path.isdir(cfg_root):
            return []
        out = []
        errors = []

        def _onerror(e):
            errors.append(e)
        # Windows 上用 \\?\ 长路径遍历：整合包的 config 目录很深，超过 260 字符的子目录
        # 原来会被 os.walk 静默跳过（文件既不列出也打不开）。列表里仍存普通路径，
        # 真正读写时再由 _long_path 加前缀。
        walk_root = _long_path(cfg_root)
        # onerror：个别子目录无权限不该让整页崩，但要计数提示
        for dirpath, _dirs, files in os.walk(walk_root, onerror=_onerror):
            for f in sorted(files):
                if f.lower().endswith(self.EXTS):
                    rel = os.path.relpath(os.path.join(dirpath, f), walk_root)
                    out.append((rel, os.path.join(cfg_root, rel)))
        self._list_errors = len(errors)
        return out

    def _empty_hint(self):
        cfg_root = os.path.join(self.server_path, "config")
        if not os.path.isdir(cfg_root):
            return "（还没有 config/ 目录，装模组或启动一次后才会生成）"
        return "（config/ 下暂无可编辑的文本配置文件）"


# Standard tkinterdnd2-with-CustomTkinter integration: declare a mixin class so
# ctk.CTk inherits TkinterDnD.DnDWrapper without losing CTk's own root logic.
if _DND_AVAILABLE:
    _APP_BASES = (ctk.CTk, TkinterDnD.DnDWrapper)
else:
    _APP_BASES = (ctk.CTk,)


class HMSLApp(*_APP_BASES):
    def __init__(self):
        super().__init__()
        if _DND_AVAILABLE:
            self.TkdndVersion = TkinterDnD._require(self)
        self.title("HMSL - Hello Minecraft! Server Launcher")
        self.geometry(self._fit_geometry(940, 720))
        # 由 HMSL 启动的服务器：path_key -> {"sp": ServerProcess, "name": 名称, "console": ConsoleWindow}
        # 用于：防止重复启动同一台、运行中禁止卸载、关主窗口前先把它们停掉。
        self._servers = {}
        self._busy_jobs = set()       # 正在进行的创建/导入任务（关窗前提醒）
        self._closing = False
        self._install_token = 0
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)
        if sys.platform == "darwin":
            # macOS 的 Cmd+Q / 菜单「退出」/ 程序坞「退出」走 ::tk::mac::Quit，不经过 WM_DELETE_WINDOW。
            # 不接管的话 Tk 直接 exit，HMSL 启动的服务器（单独的会话）会在后台无人看管地继续跑。
            # 定义了这个命令后是否真的退出由 _on_app_close 决定（用户取消则不退出）。
            try:
                self.createcommand("::tk::mac::Quit", self._on_app_close)
            except tk.TclError:
                pass
        self.env = EnvManager()
        self.installer = ServerInstaller()
        self.db_path = _resolve_data("MOD_DATABASE.md", self.env.script_dir)
        self.downloader = ModDownloader(self.db_path)
        self.registry = InstanceRegistry()
        self.full_versions = ["1.21.1", "1.21", "1.20.6", "1.20.4", "1.20.2", "1.20.1", "1.19.4", "1.18.2", "1.16.5", "1.12.2", "1.8.8", "1.7.10"]
        self.selected_ver = None
        self.selected_type = ctk.StringVar(value="")
        self.is_updating_search = False

        # --- 1. 左侧导航栏 ---
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(4, weight=1)
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="HMSL", font=ctk.CTkFont(size=28, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 20))
        self.home_btn = ctk.CTkButton(self.sidebar_frame, text="首页", command=self.show_home, height=45)
        self.home_btn.grid(row=1, column=0, padx=20, pady=10)
        self.ver_btn = ctk.CTkButton(self.sidebar_frame, text="版本管理", command=self.show_versions, height=45)
        self.ver_btn.grid(row=2, column=0, padx=20, pady=10)
        self.dl_btn = ctk.CTkButton(self.sidebar_frame, text="创建服务器", command=self.show_download, height=45)
        self.dl_btn.grid(row=3, column=0, padx=20, pady=10)

        # --- 2. 右侧主内容区 ---
        self.main_frame = ctk.CTkFrame(self, corner_radius=15, fg_color="transparent")
        self.main_frame.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")
        # 让主内容区填满窗口：row0 + 右列(col1) 吃满剩余空间。否则 main_frame 只会
        # 缩到内容自然尺寸，详情页 Tabview 撑不开、6 个操作按钮被挤到可视区外。
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.show_home()

        # --- 3. Global drag-and-drop: drop a .mrpack/.zip anywhere on the window ---
        if _DND_AVAILABLE:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_file_dropped)

    def clear_main_frame(self):
        self._stop_heartbeat()
        for widget in self.main_frame.winfo_children(): widget.destroy()

    def _fit_geometry(self, w, h):
        """Windows：940x720 是逻辑尺寸，CTk 会按 DPI 缩放（150% → 1410x1080 像素），
        1080p 笔记本上窗口底部（创建按钮、翻页栏）会跑出屏幕。按屏幕可用尺寸收一收。
        macOS 保持原尺寸不变。"""
        if sys.platform != "win32":
            return f"{w}x{h}"
        try:
            scale = ctk.ScalingTracker.get_window_scaling(self) or 1.0
            sw = self.winfo_screenwidth() / scale
            sh = self.winfo_screenheight() / scale
            w = max(760, min(w, int(sw) - 40))
            h = max(560, min(h, int(sh) - 110))      # 任务栏 + 标题栏
        except Exception:
            pass
        return f"{w}x{h}"

    def report_callback_exception(self, exc, val, tb):
        """Tk 回调里的未捕获异常：原来只打到 stderr（打包成无控制台 exe 后直接消失，
        用户点了按钮却"什么都没发生"）。现在记录下来并弹个提示。"""
        traceback.print_exception(exc, val, tb)
        if issubclass(exc, tk.TclError):
            return      # 多半是控件已销毁之类的良性竞态，只记日志不打扰用户
        if getattr(self, "_reporting_error", False):
            return
        self._reporting_error = True
        try:
            where = f"\n\n详细信息已写入：{_LOG_PATH}" if _LOG_PATH else ""
            self._show_error("程序内部错误",
                             f"{exc.__name__}: {val}\n\n刚才的操作没有完成，可以重试。{where}")
        except Exception:
            pass
        finally:
            self._reporting_error = False

    def _registry_change(self, op, *args):
        """见模块级 _registry_change。"""
        return _registry_change(self.registry, op, *args)

    # ---- 由 HMSL 启动的服务器 ----

    def _server_record(self, server_path):
        return self._servers.get(path_key(server_path))

    def _is_server_running(self, server_path):
        rec = self._server_record(server_path)
        return bool(rec and rec["sp"].is_alive())

    def _running_servers(self):
        return [rec for rec in self._servers.values() if rec["sp"].is_alive()]

    def _on_app_close(self):
        """主窗口 ✕：还有 HMSL 启动的服务器在跑时，先确认、再把它们正常停掉（存世界）后退出。
        原来直接退出：stdin 管道一断，Minecraft 服务端在后台无头继续运行，
        HMSL 里再也管不到它（端口、world/session.lock 一直被占）。"""
        if self._closing:
            return
        running = self._running_servers()
        jobs = sorted(self._busy_jobs)
        if jobs:
            dlg = ConfirmDialog(
                self, title="⚠️ 还有任务在进行",
                msg=("以下任务还没完成：\n" + "\n".join(f"• {j}" for j in jobs) +
                     "\n\n现在退出会中断它们，留下不完整的服务器文件夹。仍要退出吗？"),
                ok_text="仍然退出", cancel_text="取消", danger=True)
            self.wait_window(dlg)
            if not dlg.result:
                return
        if running:
            names = "、".join(rec["name"] for rec in running)
            dlg = ConfirmDialog(
                self, title="⚠️ 还有服务器在运行",
                msg=(f"以下服务器仍在运行：{names}\n\n"
                     "退出 HMSL 前会先向它们发送 stop 命令（保存世界后关闭），"
                     f"最多等待 {int(STOP_GRACE_S)} 秒，超时则强制结束。"),
                ok_text="停止服务器并退出", cancel_text="取消", danger=True)
            self.wait_window(dlg)
            if not dlg.result:
                return
            running = self._running_servers()     # 等待确认期间可能已有服务器自己退出
        self._closing = True
        if not running:
            self.destroy()
            return
        self._stop_all_and_exit(running)

    def _stop_all_and_exit(self, running):
        for rec in self._servers.values():
            cw = rec.get("console")
            if cw is not None:
                cw._closed = True        # 停掉控制台的日志轮询
        top = ctk.CTkToplevel(self)
        top.title("正在停止服务器")
        top.geometry("420x190")
        top.protocol("WM_DELETE_WINDOW", lambda: None)
        ctk.CTkLabel(top, text="正在停止服务器，请稍候…",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(22, 6))
        status = ctk.CTkLabel(top, text="", text_color="gray", wraplength=380)
        status.pack(padx=20)

        def force():
            for rec in running:
                threading.Thread(target=rec["sp"].kill, daemon=True).start()
        ctk.CTkButton(top, text="立即强制结束", width=140, fg_color="#a13b3b",
                      hover_color="#823030", command=force).pack(pady=16)
        _raise_toplevel(top, self)

        def stop_one(sp):
            try:
                sp.stop(grace=STOP_GRACE_S)
            except Exception:
                traceback.print_exc()
            try:
                if sp.is_alive():
                    sp.kill()
            except Exception:
                traceback.print_exc()

        threads = [threading.Thread(target=stop_one, args=(rec["sp"],), daemon=True)
                   for rec in running]
        for t in threads:
            t.start()
        started = time.monotonic()

        def poll():
            alive = [rec["name"] for rec, t in zip(running, threads) if t.is_alive()]
            if not alive:
                self.destroy()
                return
            try:
                status.configure(text=f"等待 {'、'.join(alive)} 保存并退出…"
                                      f"（{int(time.monotonic() - started)} 秒）")
            except tk.TclError:
                pass
            self.after(200, poll)
        self.after(200, poll)

    @staticmethod
    def _open_in_file_manager(path):
        """用系统文件管理器打开文件夹。Windows 用 os.startfile（ShellExecute）：
        不经过 cmd.exe——没有黑框一闪，路径里的 %VAR%、&、^ 也不会被 cmd 解释。
        Raises OSError."""
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # ---- 渲染卡死诊断：心跳探针 ----
    def _start_heartbeat(self):
        # 调试期心跳探针（卡死即停）已停用；仅确保没有残留计时器。
        self._stop_heartbeat()

    def _hb(self):
        self._hb_n = getattr(self, "_hb_n", 0) + 1
        _rlog(f"  ·hb {self._hb_n}")
        self._hb_after = self.after(500, self._hb)

    def _stop_heartbeat(self):
        a = getattr(self, "_hb_after", None)
        if a is not None:
            try:
                self.after_cancel(a)
            except Exception:
                pass
            self._hb_after = None

    def show_home(self):
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text="欢迎使用 HMSL", font=ctk.CTkFont(size=32, weight="bold")).pack(pady=(60, 10))
        ctk.CTkLabel(self.main_frame, text="专业、极简、高效的一键式开服管理中心", text_color="gray", font=ctk.CTkFont(size=14)).pack(pady=(0, 30))
        self.start_btn = ctk.CTkButton(self.main_frame, text="🚀 开启服务器", width=300, height=90, corner_radius=45, font=ctk.CTkFont(size=26, weight="bold"), command=self._home_start_clicked)
        self.start_btn.pack(pady=20)
        info_card = ctk.CTkFrame(self.main_frame, width=420, height=120, corner_radius=15)
        info_card.pack(pady=30, padx=40); info_card.pack_propagate(False)
        ctk.CTkLabel(info_card, text="当前选中实例", font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 5))
        ctk.CTkLabel(info_card, text="尚未选择服务器", text_color="#3b8ed0", font=ctk.CTkFont(size=16)).pack()
        if _DND_AVAILABLE:
            ctk.CTkLabel(self.main_frame,
                         text="💡 把 .mrpack / .zip 整合包拖到窗口里也能直接导入",
                         text_color="#777", font=ctk.CTkFont(size=12)).pack(pady=(10, 0))

    def _home_start_clicked(self):
        """首页「🚀 开启服务器」：有已选实例就回它的详情页（可点▶启动），
        否则去实例列表让用户挑一台。首页本身没有选择状态，故不直接启动。"""
        inst = getattr(self, "selected_instance", None)
        if inst:
            self.show_instance_detail(inst, initial_tab="概览")
        else:
            self.show_versions()

    def _on_file_dropped(self, event):
        """tkinterdnd2 emits a string like '{/path/with spaces/x.mrpack} /other/y.zip'.
        We parse it with the tcl-aware splitter, take the first matching file."""
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        for p in paths:
            p_clean = p.strip().strip("{}")  # belt-and-suspenders
            lower = p_clean.lower()
            if lower.endswith(".mrpack") or lower.endswith(".zip"):
                if os.path.isfile(p_clean):
                    ModpackImportWindow(self, archive_path=p_clean)
                    return
        # Nothing matched
        self._show_error(
            "无法识别拖入的文件",
            "只支持拖入 .mrpack 或 .zip 整合包文件。",
        )

    def show_versions(self):
        """版本管理页 —— 列表视图。点击卡片跳转到该实例的详情页（HMCL 式）。"""
        self.clear_main_frame()
        self.selected_instance = None
        ctk.CTkLabel(self.main_frame, text="服务器实例管理",
                     font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(10, 5))
        ctk.CTkLabel(self.main_frame, text="点击实例查看详情和管理操作",
                     text_color="gray", font=ctk.CTkFont(size=12)).pack(pady=(0, 10))

        instances = self._collect_instances()
        reg_warn = getattr(self, "_registry_warning", None)
        if reg_warn:
            ctk.CTkLabel(self.main_frame, text=f"⚠️ {reg_warn}", text_color="#e0a060",
                         wraplength=640, justify="left",
                         font=ctk.CTkFont(size=11)).pack(padx=10, pady=(0, 6))
        if not instances:
            ctk.CTkLabel(self.main_frame,
                         text="未发现任何服务器实例，快去创建一个吧！",
                         text_color="gray").pack(pady=100)
            return

        # IMPORTANT: width MUST be set or CTkScrollableFrame's internal canvas
        # doesn't stretch and the cards collapse to ~200px (CTk known quirk).
        # Height is left unset so the frame grows/shrinks with the window.
        scroll_frame = ctk.CTkScrollableFrame(self.main_frame, width=680,
                                               fg_color="transparent")
        scroll_frame.pack(fill="both", expand=True, padx=10, pady=10)
        for inst in instances:
            self.create_server_card(scroll_frame, inst)
        # Macbook 触控板两指滑动需要显式 rebind
        self.after_idle(lambda: _enable_macos_trackpad_scroll(scroll_frame))

    # ===== Instance detail page (HMCL-style) =====

    def show_instance_detail(self, inst, initial_tab="概览", active_sub=None):
        """详情页：← 返回 + 实例信息头 + Tab 容器（概览 / 配置 / ...）。
        active_sub：可选，指定「配置」tab 内默认激活哪个子标签（路由/测试用）。"""
        self.clear_main_frame()
        self.selected_instance = inst  # legacy action methods read this

        # --- Header: back button + instance name + path ---
        header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header.pack(fill="x", pady=(8, 4))
        ctk.CTkButton(header, text="← 返回列表", width=110, height=32,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self.show_versions).pack(side="left", padx=4)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True, padx=12)
        ctk.CTkLabel(title_box, text=f"📦 {inst['name']}",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     anchor="w").pack(anchor="w")
        ctk.CTkLabel(title_box, text=_short_path(inst["path"], 70),
                     font=ctk.CTkFont(size=11), text_color="gray",
                     anchor="w").pack(anchor="w")

        # --- 概览/配置 切换：分段按钮 + 内容【内联渲染】---
        # ⚠️ 每次切换都整页重建（重新调用本方法 → clear_main_frame）。这是 macOS 上
        # 唯一可靠"上屏"的路径：真鼠标点击后，原地"隐藏/显示"甚至"深层销毁+重建"都
        # 不触发窗口重绘（内容建好、事件照收，但画面定格，用户狂点无反应——CTkTabview
        # 的 grid_forget/grid 是同一个坑）；只有 clear_main_frame 整页清空重建才每次都
        # 重绘。所有顶层导航都走它、从不卡，就是明证（2026-09-23 用 computer-use 真实
        # 鼠标点击 + 点击记录器逐步实锤）。全 App 也不再有任何 CTkTabview。
        self._detail_inst = inst
        self._detail_active_sub = active_sub
        active_tab = initial_tab if initial_tab in ("概览", "配置") else "概览"
        # （原来这里每次都往 /tmp/hmsl_render.log 写调试日志：Windows 上落到 <当前盘>:\tmp，
        #   而且按 cp936 写，实例名含 emoji 时抛 UnicodeEncodeError 让详情页打不开——已删除。）

        bar = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        bar.pack(fill="x", padx=10, pady=(6, 0))
        for name in ("概览", "配置"):
            ctk.CTkButton(
                bar, text=name, width=110, height=32,
                fg_color="#2b719e" if name == active_tab else "#3a3a3a",
                hover_color="#1f538d" if name == active_tab else "#4a4a4a",
                command=lambda n=name: self.show_instance_detail(
                    inst, initial_tab=n,
                    active_sub=(self._detail_active_sub if n == "配置" else None)),
            ).pack(side="left", padx=(0, 6))

        content = ctk.CTkFrame(self.main_frame, fg_color="#1d1d1d", corner_radius=8)
        content.pack(fill="both", expand=True, padx=10, pady=10)
        t0 = time.perf_counter()
        if active_tab == "概览":
            self._render_overview_tab(content, inst)
        else:
            self._render_config_tab(content, inst, active_sub=active_sub)
        _rlog(f"[详情] {active_tab}  build+{time.perf_counter()-t0:.3f}s")
        self._start_heartbeat()                    # 诊断：心跳，卡死即停

    # --- Overview tab ---

    def _render_overview_tab(self, parent, inst):
        """实例概览：元数据卡片 + 6 个核心操作按钮（两行）。
        整个内容包在 ScrollableFrame 里，窗口再小按钮也能滚到。"""
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        scroll.after_idle(lambda: _enable_macos_trackpad_scroll(scroll))
        parent = scroll  # everything below packs into the scrollable area

        # Metadata card
        meta = ctk.CTkFrame(parent, fg_color="#2a2a2a", corner_radius=10)
        meta.pack(fill="x", padx=8, pady=8)
        rows = [
            ("加载器", inst.get("type") or "未知"),
            ("游戏版本", inst.get("version") or "未知"),
            ("EULA", "✅ 已同意" if inst.get("eula") else "⚠️ 待同意"),
            ("路径", _short_path(inst["path"], 65)),
        ]
        for label, value in rows:
            row = ctk.CTkFrame(meta, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=3)
            ctk.CTkLabel(row, text=f"{label}:", width=80, anchor="w",
                         text_color="gray",
                         font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
            ctk.CTkLabel(row, text=value, anchor="w").pack(side="left", fill="x", expand=True)

        # Actions — 2 rows of 3 (same grouping as before)
        ctk.CTkLabel(parent, text="操作", font=ctk.CTkFont(size=14, weight="bold"),
                     anchor="w").pack(anchor="w", padx=10, pady=(16, 4))

        row1 = ctk.CTkFrame(parent, fg_color="transparent"); row1.pack(pady=(4, 4))
        row2 = ctk.CTkFrame(parent, fg_color="transparent"); row2.pack(pady=(4, 8))

        ctk.CTkButton(row1, text="▶ 启动", width=140, height=42,
                      fg_color="#2b719e", hover_color="#1f538d",
                      font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._action_launch).pack(side="left", padx=6)
        ctk.CTkButton(row1, text="📂 文件夹", width=140, height=42,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=self._action_open_folder).pack(side="left", padx=6)
        ctk.CTkButton(row1, text="📥 下载模组", width=140, height=42,
                      fg_color="#3d4d6b", hover_color="#4d5d7b",
                      command=self._action_browse_mods).pack(side="left", padx=6)

        ctk.CTkButton(row2, text="🧹 扫描模组", width=140, height=42,
                      fg_color="#3d6b3d", hover_color="#4d7b4d",
                      command=self._action_scan_mods).pack(side="left", padx=6)
        ctk.CTkButton(row2, text="📂 打开 config", width=140, height=42,
                      fg_color="#3d3d3d", hover_color="#4d4d4d",
                      command=lambda: self._open_subfolder(inst["path"], "config")).pack(side="left", padx=6)
        ctk.CTkButton(row2, text="🗑 移除...", width=140, height=42,
                      fg_color="#5a2b2b", hover_color="#7a3535",
                      command=self._action_remove_or_uninstall).pack(side="left", padx=6)

    def _open_subfolder(self, server_path, sub):
        """Open a subdirectory of the server in the OS file manager.
        Creates the subfolder if missing so the link never dead-ends —
        but never recreates a server folder that has been deleted."""
        if not os.path.isdir(server_path):
            self._show_error("服务器文件夹不存在",
                             f"找不到：\n{server_path}\n\n它可能已被移动或删除。")
            return
        target = os.path.join(server_path, sub)
        try:
            os.makedirs(target, exist_ok=True)
            self._open_in_file_manager(target)
        except OSError as e:
            self._show_error("无法打开文件夹", f"{target}\n\n{e}")

    # --- Config tab (with 3 sub-tabs) ---

    def _render_config_tab(self, parent, inst, active_sub=None):
        """配置中心：3 个子页 —— server.properties / 世界 / 模组。

        ⚠️ 不用嵌套 CTkTabview：详情页本身已是一个 CTkTabview，若在其 tab 内再套
        一个 CTkTabview，macOS 上会触发"窗口停止重绘"——子页切换在后台完成、事件
        照收，但画面定格，用户狂点无反应（2026-09-23 用点击记录器实锤）。
        子页切换不在这层原地做，而是回到『详情层』整页重建（见 _show_config_subtab）。
        这里只按传入的 active_sub 一次性把选中的那个子页直接画出来（不切换、不递归）。
        """
        self._cfg_inst = inst
        self._cfg_subtab_factories = {
            "🌐 server.properties": self._render_server_properties_subtab,
            "🌍 世界": self._render_world_config_subtab,
            "🔧 模组": self._render_mod_config_subtab,
        }
        active = (active_sub if active_sub in self._cfg_subtab_factories
                  else "🌐 server.properties")
        _rlog(f"[配置页] 建按钮栏 + 直接渲染子页 {active}")
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(4, 0))
        for name in self._cfg_subtab_factories:
            ctk.CTkButton(
                bar, text=name, width=155, height=30,
                fg_color="#2b719e" if name == active else "#3a3a3a",
                hover_color="#1f538d" if name == active else "#4a4a4a",
                command=lambda n=name: self._show_config_subtab(n),
            ).pack(side="left", padx=(0, 6))
        content = ctk.CTkFrame(parent, fg_color="#2a2a2a", corner_radius=8)
        content.pack(fill="both", expand=True, padx=4, pady=4)
        holder = ctk.CTkFrame(content, fg_color="transparent")
        holder.pack(fill="both", expand=True)
        t0 = time.perf_counter()
        try:
            self._cfg_subtab_factories[active](holder, inst)
        except Exception as e:
            ctk.CTkLabel(
                holder,
                text=f"⚠️ 此配置加载失败：\n{type(e).__name__}: {e}\n\n"
                     f"其它子页和「← 返回列表」仍可正常使用。",
                text_color="#e06c6c", justify="left",
                font=ctk.CTkFont(size=12)).pack(padx=20, pady=20, anchor="w")
            traceback.print_exc()
        _rlog(f"[配置子页] {active}  build+{time.perf_counter()-t0:.3f}s")

    def _show_config_subtab(self, name):
        """点配置子页按钮：整页重建详情页、配置 tab 激活到该子页（clear_main_frame 是
        macOS 上唯一每次都可靠上屏的路径；原地/深层重建都不重绘）。"""
        if name not in self._cfg_subtab_factories:
            return
        self.show_instance_detail(self._detail_inst, initial_tab="配置", active_sub=name)

    def _render_server_properties_subtab(self, parent, inst):
        """server.properties 可视化编辑（分页）+ 未知 key 走 raw 文本框。"""
        editor = ServerPropertiesEditor(parent, inst["path"], self)
        editor.pack(fill="both", expand=True)

    def _render_world_config_subtab(self, parent, inst):
        """世界配置：先列实例下所有世界（含 level.dat 的子目录），选一个后列其下文件。"""
        editor = WorldConfigEditor(parent, inst["path"], self)
        editor.pack(fill="both", expand=True)

    def _render_mod_config_subtab(self, parent, inst):
        """config/ 全局模组配置：文件树 + 原始文本编辑。"""
        editor = ModConfigEditor(parent, inst["path"], self)
        editor.pack(fill="both", expand=True)

    def _collect_instances(self):
        """合并扫描结果和注册表，按 path_key 去重（Windows 路径不分大小写、/ 与 \\ 等价）。
        扫描结果提供 eula 等额外字段；注册表里的 loader / mc_version 来自创建时用户的选择
        或整合包 manifest，比扫描时按文件猜的可靠，有就优先用（原来扫描优先，
        新建的 Fabric 服会显示成 "Vanilla / 未知"，下载模组也就搜错了）。"""
        scanner = InstanceScanner(self.env.script_dir)
        merged = {}
        for s in scanner.scan():
            merged.setdefault(path_key(s["path"]), dict(s))

        self.registry.last_warning = None
        entries = self.registry.live_entries()
        # 注册表文件损坏/读不了时 load() 不抛异常、返回空列表并留下说明，版本页上要提示出来
        self._registry_warning = getattr(self.registry, "last_warning", None)
        for entry in entries:
            key = path_key(entry.path)
            inst = merged.get(key)
            if inst is None:
                abs_path = os.path.abspath(entry.path)
                name = entry.name or os.path.basename(abs_path)
                inst = None
                if get_instance_details is not None:
                    # 不在脚本目录下的服务器也按文件识别加载器/版本/EULA（注册表字段为空时兜底）
                    try:
                        inst = dict(get_instance_details(abs_path, name))
                    except Exception:
                        traceback.print_exc()
                        inst = None
                if inst is None:
                    inst = {
                        "name": name,
                        "path": abs_path,
                        "version": None,
                        "type": None,
                        # 看 eula.txt 的内容（eula=true），不是文件存在就算同意
                        "eula": detect_eula(abs_path),
                    }
                merged[key] = inst
            inst["registered"] = True
            if entry.loader:
                inst["type"] = entry.loader
            if entry.mc_version:
                inst["version"] = entry.mc_version
        return list(merged.values())

    def create_server_card(self, parent, inst):
        """卡片整块可点击。点击 → 跳转到该实例的详情页（HMCL 风格）。"""
        card = ctk.CTkFrame(parent, height=90, corner_radius=15,
                            border_width=2, border_color="#2b2b2b")
        card.pack(fill="x", pady=8, padx=10); card.pack_propagate(False)

        icon = ctk.CTkLabel(card, text="📦", font=ctk.CTkFont(size=30))
        icon.pack(side="left", padx=20)
        info_box = ctk.CTkFrame(card, fg_color="transparent")
        info_box.pack(side="left", fill="both", expand=True, pady=12)

        name_label = ctk.CTkLabel(info_box, text=inst["name"],
                                   font=ctk.CTkFont(size=16, weight="bold"), anchor="w")
        name_label.pack(anchor="w", fill="x")
        status_text = "✅ EULA 已同意" if inst["eula"] else "⚠️ 待同意 EULA"
        meta_label = ctk.CTkLabel(info_box,
                                   text=f"{_short_path(inst['path'], 60)}  |  {status_text}",
                                   font=ctk.CTkFont(size=11), text_color="gray",
                                   anchor="w")
        meta_label.pack(anchor="w", fill="x")

        # Right-side chevron hints "click to enter"
        ctk.CTkLabel(card, text="›", font=ctk.CTkFont(size=28),
                     text_color="#666").pack(side="right", padx=20)

        # Whole card + children → click to enter detail
        clickable = [card, icon, info_box, name_label, meta_label]
        for w in clickable:
            w.bind("<Button-1>", lambda e, i=inst: self.show_instance_detail(i))
        # Hover affordance — change border color on enter/leave
        def _on_enter(e, c=card): c.configure(border_color="#3a5570")
        def _on_leave(e, c=card): c.configure(border_color="#2b2b2b")
        for w in clickable:
            w.bind("<Enter>", _on_enter)
            w.bind("<Leave>", _on_leave)

    def _action_launch(self):
        if not self.selected_instance: return
        self.open_console(self.selected_instance["name"], self.selected_instance["path"])

    def _action_open_folder(self):
        if not self.selected_instance: return
        path = self.selected_instance["path"]
        if not os.path.isdir(path):
            self._show_error("服务器文件夹不存在",
                             f"找不到：\n{path}\n\n它可能已被移动或删除。")
            return
        try:
            self._open_in_file_manager(path)
        except OSError as e:
            self._show_error("无法打开文件夹", f"{path}\n\n{e}")

    def _action_scan_mods(self):
        if not self.selected_instance: return
        ModScanWindow(self, self.selected_instance["name"], self.selected_instance["path"])

    def _action_browse_mods(self):
        if not self.selected_instance: return
        # Pull mc_version + loader from registry where available; the scanner
        # alone doesn't know these for legacy instances created before HMSL.
        inst = self.selected_instance
        mc_version = inst.get("version") or None
        loader = inst.get("type") or None
        ModBrowserWindow(self, inst["name"], inst["path"], mc_version=mc_version, loader=loader)

    def _action_remove_or_uninstall(self):
        """两步流程：先弹选项框，再按选项分发。"""
        if not self.selected_instance: return
        inst = self.selected_instance
        dlg = RemoveOptionDialog(self, inst["name"])
        self.wait_window(dlg)
        if dlg.choice == "remove":
            self._do_remove_from_registry(inst)
        elif dlg.choice == "uninstall":
            self._do_uninstall_with_confirm(inst)

    def _do_remove_from_registry(self, inst):
        """选项 1：仅从注册表移除，不动文件。"""
        ok, removed, note = self._registry_change(self.registry.remove, inst["path"])
        if not ok:
            msg = "没能从列表中移除（服务器文件夹没有被改动）。"
        elif removed:
            msg = ("已从列表中移除。\n\n服务器文件夹和数据全部保留在原位，"
                   "下次扫描或重新注册时还能找回。")
        else:
            msg = ("该实例不在注册表中（可能是脚本目录扫描出的旧实例），"
                   "无法仅「从列表移除」。如要彻底删除，请选「卸载」。")
        if note:
            msg += f"\n\n⚠️ {note}"
        self._show_error("移除结果", msg)
        if removed:
            self.show_versions()

    def _do_uninstall_with_confirm(self, inst):
        """选项 2：弹红色二次确认，确认后 rmtree。"""
        path = inst["path"]
        if self._is_server_running(path):
            self._show_error("无法卸载", f"{inst['name']} 正在运行。\n请先在它的控制台里点「⏹ 停止」，"
                                         "等服务器退出后再卸载。")
            return
        is_registered = bool(inst.get("registered"))
        extra = self._delete_extra_protected()
        # 先过一遍护栏：明显不能删的路径根本不弹"确认删除"
        ok, err, _abs = _check_delete_target(path, extra_protected=extra,
                                             is_registered=is_registered)
        if not ok:
            self._show_error("拒绝卸载", err)
            return
        confirm = ConfirmDialog(
            self,
            title="⚠️ 确认彻底卸载",
            msg=(f"即将永久删除整个服务器文件夹：\n\n"
                 f"{path}\n\n"
                 f"包括世界数据、模组、配置、玩家存档等全部内容。\n"
                 f"此操作不可撤销！"),
            ok_text="确认彻底删除",
            cancel_text="取消",
            danger=True,
        )
        self.wait_window(confirm)
        if not confirm.result:
            return
        if self._is_server_running(path):           # 确认框期间被启动了
            self._show_error("无法卸载", f"{inst['name']} 正在运行，请先停止。")
            return
        ok, err = self._delete_server_folder_safely(path, extra_protected=extra,
                                                    is_registered=is_registered)
        if not ok:
            self._show_error("卸载失败", err)
            if not os.path.isdir(path):
                self.show_versions()
            return
        # Also clean up the registry entry if present
        _ok, _removed, note = self._registry_change(self.registry.remove, path)
        self._servers.pop(path_key(path), None)
        self._show_error("卸载完成", f"已永久删除 {inst['name']}。"
                         + (f"\n\n⚠️ {note}" if note else ""))
        self.show_versions()

    def _delete_extra_protected(self):
        """除系统目录外，HMSL 自己所在的目录（及其上级）也不能被当成服务器删掉。"""
        extra = [self.env.script_dir]
        if getattr(sys, "frozen", False):
            extra.append(os.path.dirname(sys.executable))
        return extra

    @staticmethod
    def _delete_server_folder_safely(path, extra_protected=(), is_registered=False):
        """rmtree 但加几道护栏，拒绝删可能误伤的系统/家目录。返回 (ok, 错误信息)。
        护栏见 _check_delete_target。Windows 上先把整个文件夹改名成临时名：
        只要里面有任何文件被占用（服务器还在跑、资源管理器/编辑器开着某个文件），
        改名就会失败——这时什么都不删，避免删到一半留下个残缺的服务器。"""
        ok, err, abs_path = _check_delete_target(path, extra_protected=extra_protected,
                                                 is_registered=is_registered)
        if not ok:
            return False, err

        target = abs_path
        if sys.platform == "win32":
            trash = f"{abs_path}.hmsl-deleting-{os.getpid()}-{int(time.time())}"
            try:
                os.rename(_long_path(abs_path), _long_path(trash))
            except OSError as e:
                # 只报系统给的原因，不带路径：路径上面已写明，临时改名用的 .hmsl-deleting 名字
                # 和 \\?\ 前缀对用户没有意义
                reason = (f"[WinError {e.winerror}] " if getattr(e, "winerror", None) else "") + \
                         (str(e.strerror or "").strip() or _os_error_text(e))
                return False, ("服务器文件夹正被占用，未删除任何文件。\n\n"
                               "请先停止这台服务器，并关闭打开了其中文件的程序"
                               f"（资源管理器窗口、文本编辑器等）后再试。\n\n{abs_path}\n（{reason}）")
            target = trash

        failures = []
        root_key = os.path.normcase(os.path.abspath(_long_path(target)))

        def _onexc(func, p, exc):
            # 只对"删文件 / 删空文件夹"失败再试一次。os.open / os.scandir / os.lstat 等失败
            # 直接记下：它们的调用方式不同（func(p) 会抛 TypeError），改权限也解决不了；
            # 文件夹非空（ENOTEMPTY，里面有删不掉的东西）同理。
            if func not in (os.unlink, os.remove, os.rmdir):
                failures.append((p, exc))
                return
            try:
                if sys.platform == "win32":
                    # 只读文件/文件夹（从光盘/压缩包/git 拷来的）：去掉只读属性再删一次
                    os.chmod(p, stat.S_IWRITE)
                else:
                    # macOS/Linux：删一个条目要的是【所在文件夹】的写权限（文件自身的权限无关）。
                    # 只在权限不足时、只给服务器文件夹里面的上级文件夹【追加】属主写权限，
                    # 绝不替换原有权限位，也不碰服务器文件夹之外的目录。
                    parent = os.path.dirname(os.path.abspath(p))
                    if (not isinstance(exc, PermissionError)
                            or os.path.normcase(os.path.abspath(p)) == root_key):
                        failures.append((p, exc))
                        return
                    mode = stat.S_IMODE(os.stat(parent).st_mode)
                    if mode & stat.S_IWUSR:
                        failures.append((p, exc))   # 本来就可写：不是权限位的问题
                        return
                    os.chmod(parent, mode | stat.S_IWUSR)
                func(p)
            except Exception as e2:
                failures.append((p, e2))

        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(_long_path(target), onexc=_onexc)
            else:
                shutil.rmtree(_long_path(target),
                              onerror=lambda f, p, ei: _onexc(f, p, ei[1]))
        except OSError as e:
            failures.append((target, e))

        if not os.path.exists(_long_path(target)):
            return True, None
        # 删到一半失败：把剩下的挪回原名（注册表/列表还指向它），并如实告诉用户还剩什么
        remaining = []
        root_lp = _long_path(target)
        try:
            for dp, _dirs, files in os.walk(root_lp):
                for f in files:
                    remaining.append(os.path.relpath(os.path.join(dp, f), root_lp))
        except (OSError, ValueError):
            pass
        if target != abs_path:
            try:
                os.rename(_long_path(target), _long_path(abs_path))
                target = abs_path
            except OSError:
                pass
        sample = [f"• {rel}" for rel in remaining[:8]]
        more = f"\n……共 {len(remaining)} 个文件" if len(remaining) > 8 else ""
        # 错误里的路径去掉 \\?\ 前缀；若已挪回原名，把临时名也换回去
        repl = [(trash, abs_path)] if (sys.platform == "win32" and target == abs_path) else []
        first_err = (f"\n\n首个错误：{_os_error_text(failures[0][1], repl)}" if failures else "")
        return False, (f"删除没有完成，已删除部分文件，仍残留 {len(remaining)} 个文件，位于：\n"
                       f"{target}\n\n" + "\n".join(sample) + more + first_err)

    def open_console(self, name, server_path):
        """打开一个独立控制台窗口，启动服务器并实时显示日志。"""
        rec = self._server_record(server_path)
        if rec and rec["sp"].is_alive():
            cw = rec.get("console")
            try:
                if cw is not None and cw.winfo_exists():
                    cw.deiconify(); cw.lift(); cw.focus_force()
                    return
            except tk.TclError:
                pass
            # 控制台已关，但进程还没退（正在停止中）：别在同一个文件夹上再起一个
            # （会抢 world/session.lock 和端口），而是给它重新打开控制台——
            # 能看到停止进度，卡住时也能在里面点「强制结束」。
            cw = ConsoleWindow(self, rec.get("name") or name, rec["sp"])
            _raise_toplevel(cw, None)
            cw._append("[GUI] 这台服务器仍在运行（或正在停止中），已重新连接到它的控制台。")
            rec["console"] = cw
            return
        try:
            sp = start_server(server_path)
        except (OSError, ValueError, RuntimeError) as e:    # 缺 Java / 缺启动脚本 / 启动配置损坏
            self._show_error(f"无法启动 {name}", str(e))
            return
        cw = ConsoleWindow(self, name, sp)
        _raise_toplevel(cw, None)
        self._servers[path_key(server_path)] = {"sp": sp, "name": name, "console": cw}

    def _show_error(self, title, msg, parent=None):
        """通用提示框（错误和普通结果都用它）。parent：挂在哪个窗口上（默认主窗口）。"""
        master = parent if parent is not None else self
        try:
            if not master.winfo_exists():
                master = self
        except tk.TclError:
            master = self
        msg = str(msg)
        top = ctk.CTkToplevel(master); top.title(title)
        long_msg = len(msg) > 360 or msg.count("\n") > 10
        ctk.CTkLabel(top, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(20, 10))
        if long_msg:
            # 长内容（安装诊断、失败文件清单）用可滚动、可选中复制的文本框
            top.geometry("560x420")
            ctk.CTkButton(top, text="知道了", width=100, command=top.destroy).pack(side="bottom", pady=15)
            box = ctk.CTkTextbox(top, wrap="word", text_color="gray")
            box.pack(fill="both", expand=True, padx=20)
            box.insert("1.0", msg)
            box.configure(state="disabled")
        else:
            lines = sum(1 + len(line) // 26 for line in msg.split("\n"))
            top.geometry(f"420x{min(420, 130 + 20 * lines)}")
            ctk.CTkLabel(top, text=msg, wraplength=380, text_color="gray",
                         justify="left").pack(padx=20)
            ctk.CTkButton(top, text="知道了", width=100, command=top.destroy).pack(pady=15)
        _raise_toplevel(top, master)

    def show_download(self):
        self.clear_main_frame()
        self.selected_ver = None; self.selected_type.set("")
        self._offered_types = []
        header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header.pack(fill="x", pady=(10, 10))
        ctk.CTkLabel(header, text="新建服务器向导", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left", padx=(0, 20))
        ctk.CTkButton(header, text="📦 从整合包导入...", width=170, height=34,
                      fg_color="#3d4d6b", hover_color="#4d5d7b",
                      command=self._open_modpack_import).pack(side="left")

        # Footer is packed FIRST with side="bottom" so tk reserves space for the
        # action button before body_frame expands into the remaining area.
        # Without this ordering, body_frame's expand=True consumes everything
        # and the bottom button gets pushed past the window's edge.
        footer_frame = ctk.CTkFrame(self.main_frame, height=80, fg_color="transparent")
        footer_frame.pack(fill="x", side="bottom", pady=10)
        self.finish_btn = ctk.CTkButton(footer_frame, text="开始创建服务器", state="disabled", width=240, height=50, corner_radius=25, command=self.start_installation)
        self.finish_btn.pack()

        body_frame = ctk.CTkFrame(self.main_frame, corner_radius=15)
        body_frame.pack(fill="both", expand=True, padx=10, pady=10)

        left_box = ctk.CTkFrame(body_frame, fg_color="transparent")
        left_box.pack(side="left", fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(left_box, text="1. 服务器名称", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.name_var = ctk.StringVar(); self.name_var.trace_add("write", lambda *args: self.validate_all())
        self.name_entry = ctk.CTkEntry(left_box, placeholder_text="例如: my_server", textvariable=self.name_var, width=250); self.name_entry.pack(anchor="w")
        # 名称不合法时就地说明原因（Windows 不允许 <>:"/\|?*、结尾的点/空格、CON/NUL 等）
        self.name_hint = ctk.CTkLabel(left_box, text="", text_color="#e07a5f", height=18,
                                      font=ctk.CTkFont(size=11), wraplength=260, justify="left")
        self.name_hint.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(left_box, text="2. 创建位置", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.target_dir_var = ctk.StringVar(value=self.env.script_dir)
        self.target_dir_var.trace_add("write", lambda *args: self.validate_all())
        dir_row = ctk.CTkFrame(left_box, fg_color="transparent")
        dir_row.pack(anchor="w", fill="x")
        self.dir_entry = ctk.CTkEntry(dir_row, textvariable=self.target_dir_var, width=180)
        self.dir_entry.pack(side="left")
        ctk.CTkButton(dir_row, text="浏览...", width=60, command=self.pick_target_dir).pack(side="left", padx=(6, 0))
        self.dir_hint = ctk.CTkLabel(left_box, text="", text_color="#e07a5f", height=18,
                                     font=ctk.CTkFont(size=11))
        self.dir_hint.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(left_box, text="3. 选择游戏版本 (搜索)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.ver_search_var = ctk.StringVar(); self.ver_search_var.trace_add("write", self.update_version_list)
        self.ver_entry = ctk.CTkEntry(left_box, placeholder_text="输入 1.20 等...", textvariable=self.ver_search_var, width=250); self.ver_entry.pack(anchor="w")
        self.ver_listbox = ctk.CTkScrollableFrame(left_box, width=230, height=140); self.ver_listbox.pack(anchor="w", pady=10)

        right_box = ctk.CTkFrame(body_frame, fg_color="transparent")
        right_box.pack(side="right", fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(right_box, text="4. 选择服务端类型", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.type_info_label = ctk.CTkLabel(right_box, text="请先从左侧选择版本", text_color="gray"); self.type_info_label.pack(pady=20)
        self.type_button_frame = ctk.CTkFrame(right_box, fg_color="transparent"); self.type_button_frame.pack(fill="both", expand=True)
        self.update_version_list()

    def update_version_list(self, *args):
        if self.is_updating_search: return
        search_term = self.ver_search_var.get().strip()
        # 用户改了搜索框、内容已不是选中的版本：清掉旧选择，免得"框里写 1.8、实际建 1.20.1"
        if self.selected_ver is not None and search_term != self.selected_ver:
            self._clear_version_selection()
        for widget in self.ver_listbox.winfo_children(): widget.destroy()
        filtered = [v for v in self.full_versions if search_term in v]
        for v in filtered:
            ctk.CTkButton(self.ver_listbox, text=v, fg_color="transparent", text_color="white", hover_color="#2e2e2e", anchor="w", height=32, command=lambda ver=v: self.on_version_selected(ver)).pack(fill="x", padx=5)

    def _clear_version_selection(self):
        self.selected_ver = None
        self.selected_type.set("")
        self._offered_types = []
        try:
            for widget in self.type_button_frame.winfo_children(): widget.destroy()
            self.type_info_label.configure(text="请先从左侧选择版本", text_color="gray")
        except (AttributeError, tk.TclError):
            pass
        self.validate_all()

    def on_version_selected(self, ver):
        self.selected_ver = ver; self.is_updating_search = True; self.ver_search_var.set(ver); self.is_updating_search = False
        self.refresh_type_menu(ver); self.validate_all()

    @staticmethod
    def _loader_options(ver):
        """该游戏版本可选的服务端类型。Vanilla（官方原版服务端，不装模组）任何版本都有。"""
        options = ["Vanilla", "Forge"]
        try:
            parts = [int(p) for p in ver.split('.')]
        except ValueError:
            return options
        parts += [0] * (3 - len(parts))
        v_num = parts[0]*10000 + parts[1]*100 + parts[2]
        if v_num >= 10808: options.append("Paper")
        if v_num >= 11400: options.append("Fabric")
        if v_num >= 12002: options.append("NeoForge")
        return options

    def refresh_type_menu(self, ver):
        self.type_info_label.configure(text=f"适用于 {ver} 的选项：", text_color="white")
        for widget in self.type_button_frame.winfo_children(): widget.destroy()
        options = self._loader_options(ver)
        self._offered_types = options
        # 换了版本后旧选择不再提供（如 NeoForge → 1.7.10）：清掉，否则按钮仍可点
        if self.selected_type.get() not in options:
            self.selected_type.set("")
        labels = {"Vanilla": "Vanilla（原版，不装模组）"}
        for opt in options:
            ctk.CTkRadioButton(self.type_button_frame, text=labels.get(opt, opt), variable=self.selected_type, value=opt, command=self.validate_all).pack(anchor="w", pady=10, padx=10)

    def validate_all(self):
        name = self.name_var.get().strip()
        name_err = validate_server_name(name) if name else None
        target_dir = self.target_dir_var.get().strip()
        dir_ok = os.path.isdir(target_dir)
        try:
            self.name_hint.configure(text=name_err or "")
            self.dir_hint.configure(text="" if (dir_ok or not target_dir) else "该目录不存在")
        except (AttributeError, tk.TclError):
            pass
        ok = (name and not name_err
              and self.selected_ver
              and self.selected_type.get()
              and self.selected_type.get() in getattr(self, "_offered_types", [])
              and dir_ok)
        if ok:
            self.finish_btn.configure(state="normal", fg_color="#2b719e")
        else:
            self.finish_btn.configure(state="disabled", fg_color=["#3B8ED0", "#1F6AA5"])

    def _open_modpack_import(self):
        """让用户选一个 .mrpack/.zip 整合包，并启动 ModpackImportWindow。"""
        path = filedialog.askopenfilename(
            title="选择整合包",
            filetypes=[("整合包文件", "*.mrpack *.zip"), ("所有文件", "*.*")],
        )
        if not path:
            return
        ModpackImportWindow(self, archive_path=os.path.normpath(path))

    def pick_target_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.target_dir_var.get() or self.env.script_dir,
                                         title="选择服务器创建位置")
        if chosen:
            self.target_dir_var.set(os.path.normpath(chosen))

    def start_installation(self):
        server_name = self.name_var.get().strip()
        version = self.selected_ver
        loader = self.selected_type.get()
        target_dir = self.target_dir_var.get().strip()
        name_err = validate_server_name(server_name)
        if name_err:
            self._show_error("服务器名称不可用", name_err)
            return
        server_path = os.path.join(target_dir, server_name)
        if os.path.exists(server_path) and not os.path.isdir(server_path):
            self._show_error("无法创建", f"已存在同名文件（不是文件夹）：\n{server_path}")
            return
        if self._is_server_running(server_path):
            self._show_error("无法创建", f"{server_name} 正在运行，请先停止它或换一个名称。")
            return
        if os.path.isdir(server_path) and not dir_is_effectively_empty(server_path):
            # create_server 不会往非空文件夹里装（也不动里面的文件）：只能换个名称
            #（只有 .DS_Store / desktop.ini 这类系统文件的文件夹算空的，和 create_server 同一判断）
            new_name = _ask_new_name_for_existing(self, self, target_dir, server_name,
                                                  action="创建")
            if not new_name:
                return
            server_name = new_name
            self.name_var.set(server_name)
            server_path = os.path.join(target_dir, server_name)
        self.clear_main_frame()
        self._install_token += 1
        token = self._install_token
        ctk.CTkLabel(self.main_frame, text=f"正在部署：{server_name}", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=20)
        ctk.CTkLabel(self.main_frame, text=f"位置：{target_dir}", text_color="gray", font=ctk.CTkFont(size=12)).pack()
        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=540); self.progress_bar.set(0); self.progress_bar.pack(pady=15)
        self.progress_label = ctk.CTkLabel(self.main_frame, text="准备开始...", text_color="gray",
                                           wraplength=640, justify="left"); self.progress_label.pack()
        self.log_text = ctk.CTkTextbox(self.main_frame, width=650, height=320, fg_color="#000000", text_color="#00ff00")
        self.log_text.pack(pady=20)
        self._install_buttons = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self._install_buttons.pack(pady=(0, 10))
        job = f"创建服务器 {server_name}"
        self._busy_jobs.add(job)
        sink = LogQueue(self.log_text)      # 在主线程建（它会注册 Tk after 轮询）
        thread = threading.Thread(target=self.run_install_logic,
                                  args=(server_name, version, loader, target_dir),
                                  kwargs={"token": token, "job": job, "sink": sink})
        thread.daemon = True; thread.start()

    def run_install_logic(self, name, version, loader, target_dir, token=None, job=None, sink=None):
        """GUI shim: hand off to pure create_server() and render its progress.
        任何异常都要落到"失败 + 返回按钮"，不能让进度条永远停在半路。"""
        if token is None:
            token = self._install_token
        if sink is None:
            sink = LogQueue(self.log_text)
        _route_stdout_to(sink)          # 只接管本线程的 print，不影响别的线程/并发安装
        progress = lambda val, text: self.safe_update_progress(val, text, token=token)
        ok, final_msg = False, ""
        try:
            result = create_server(
                name=name,
                version=version,
                loader=loader,
                parent_dir=target_dir,
                env_manager=self.env,
                installer=self.installer,
                downloader=self.downloader,
                progress_callback=progress,
            )
            if result.success:
                # Register the new instance so it's discoverable on the
                # version-management page even if it lives outside script_dir.
                reg_ok, _res, note = self._registry_change(self.registry.add, RegistryEntry(
                    name=name, path=result.server_path,
                    loader=loader, mc_version=version,
                ))
                if note:
                    print(f"[警告] {note}")
                    if not reg_ok:
                        note += "\n\n服务器已创建成功，但可能不会出现在「版本管理」列表里。"
                    _post_ui(self, self._show_error, "实例列表提示", note)
                # 非致命的提示（例如 MOD_DATABASE 里某些模组没装上）：进日志，并在结果里点一句
                warns = [w for w in (getattr(result, "warnings", None) or []) if w]
                for w in warns:
                    print(f"[提示] {w}")
                ok, final_msg = True, "✨ 服务器部署成功！"
                if warns:
                    final_msg += f"（有 {len(warns)} 条提示，见下方日志）"
            else:
                # CreateServerResult.error 含安装器诊断、缺少的 Java 版本等，原样给用户看
                err = result.error or "未知错误"
                print(f"[错误] {err}")
                if len(err) > 400:
                    err = err[:400] + "…（完整信息见下方日志）"
                final_msg = f"❌ 安装失败：{err}"
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            final_msg = f"❌ 发生意外错误：{type(e).__name__}: {e}"
            print(final_msg)
        finally:
            _unroute_stdout()
            sink.close()
            if job:
                self._busy_jobs.discard(job)
        _post_ui(self, self._install_finished, token, ok, final_msg)

    def _install_page_alive(self, token):
        try:
            return (token == self._install_token
                    and self.progress_bar.winfo_exists())
        except (AttributeError, tk.TclError):
            return False

    def _install_finished(self, token, ok, msg):
        if not self._install_page_alive(token):
            return      # 用户已离开安装页（进度页被别的页面替换）：不往别的页面上加按钮
        self._update_ui_state(1.0 if ok else 0.0, msg)
        if not ok:
            self.progress_label.configure(text_color="#e07a5f")
        row = self._install_buttons
        if ok:
            ctk.CTkButton(row, text="完成并返回", command=self.show_home, width=200).pack(side="left", padx=6)
            ctk.CTkButton(row, text="去版本管理查看", command=self.show_versions, width=160,
                          fg_color="#3d3d3d", hover_color="#4d4d4d").pack(side="left", padx=6)
        else:
            ctk.CTkButton(row, text="← 返回修改", command=self.show_download, width=160).pack(side="left", padx=6)
            ctk.CTkButton(row, text="回首页", command=self.show_home, width=120,
                          fg_color="#3d3d3d", hover_color="#4d4d4d").pack(side="left", padx=6)

    def safe_update_progress(self, val, text, token=None):
        # 后台线程调用：投递回主线程；页面已切走 / 程序已退出则丢弃
        _post_ui(self, self._update_ui_state, val, text, token)

    def _update_ui_state(self, val, text, token=None):
        if token is not None and not self._install_page_alive(token):
            return
        try:
            self.progress_bar.set(val); self.progress_label.configure(text=text)
        except (AttributeError, tk.TclError):
            pass

def _route_to(app: "HMSLApp", route: str) -> None:
    """Drive the GUI to a specific screen via short routes — used by
    tools/snap.py so I can iterate without manual click-through.

    Supported routes (':' delimited):
        home, versions, download
        detail:<instance_name>
        config:<instance_name>          (detail page + 配置 tab active)
        modcfg:<instance_name>          (配置 tab + 内层「🔧 模组」子标签 active)
    """
    if not route:
        return
    parts = route.split(":", 1)
    page = parts[0]
    if page == "home":      app.show_home(); return
    if page == "versions":  app.show_versions(); return
    if page == "download":  app.show_download(); return
    if page in ("detail", "config", "modcfg") and len(parts) == 2:
        name = parts[1]
        for inst in app._collect_instances():
            if inst["name"] == name:
                initial_tab = "概览" if page == "detail" else "配置"
                # modcfg：直接把内层激活子标签设为「🔧 模组」，让模组文件列表真正
                # 变可见（卡死就发生在这一刻——验证 Listbox 改造后不再冻）。
                active_sub = "🔧 模组" if page == "modcfg" else None
                app.show_instance_detail(inst, initial_tab=initial_tab,
                                         active_sub=active_sub)
                return
        print(f"[route_to] 找不到名为 {name!r} 的实例")


def _own_cg_window_id(app):
    """本进程主窗口的 CGWindowID —— 给 screencapture -l 精确抓窗口内容用
    （无视其它 App 遮挡）。拿不到返回 None，调用方回退区域截图。"""
    try:
        import Quartz
    except Exception:
        return None
    pid = os.getpid()
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    except Exception:
        return None
    best_id, best_area = None, 0
    for w in wins:
        if w.get("kCGWindowOwnerPID") != pid:
            continue
        b = w.get("kCGWindowBounds") or {}
        area = (b.get("Width") or 0) * (b.get("Height") or 0)
        if area > best_area:                       # 取本进程最大的窗口
            best_area, best_id = area, w.get("kCGWindowNumber")
    return best_id


def _arm_freeze_watchdog(app, out_path=None, stall_s=2.5):
    """卡死看门狗：主循环每 0.5s 跳一次心跳；后台守护线程发现心跳停跳 >stall_s
    秒，就把此刻所有线程的 Python 调用栈 dump 到 out_path（真前台窗口卡死也能抓）。
    同一次卡死只 dump 一次；恢复后再卡会再 dump。仅诊断用，--freeze-watchdog 开启。
    macOS 仍写 /tmp（开发者的诊断习惯不变）；Windows 没有 /tmp（会落到 <当前盘>:\\tmp），
    改写到 %TEMP%。"""
    diag_dir = tempfile.gettempdir() if sys.platform == "win32" else "/tmp"
    if out_path is None:
        out_path = os.path.join(diag_dir, "hmsl_freeze_stack.txt")
    exc_path = os.path.join(diag_dir, "hmsl_exc.txt")
    import threading
    import faulthandler
    import time as _t
    import traceback as _tb

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"=== freeze watchdog armed {_t.strftime('%H:%M:%S')} "
                    f"(stall>{stall_s}s) ===\n")
    except OSError:
        pass

    # (A) 全局点击记录器：每次左键，记下点中的控件类名+文字（不依赖主循环）。
    def _log_click(e):
        try:
            w = e.widget
            info = w.__class__.__name__
            try:
                t = w.cget("text")
                if t:
                    info += f" text={t!r}"
            except Exception:
                pass
            _rlog(f"[CLICK] @({e.x_root},{e.y_root}) -> {info}")
        except Exception as ex:
            _rlog(f"[CLICK] 记录出错: {ex}")
    try:
        app.bind_all("<Button-1>", _log_click, add="+")
    except Exception:
        pass

    # (B) 回调异常捕获器：Tk 回调里抛的异常默认只打 stderr、易被吞。这里落文件。
    def _report_exc(exc, val, tbk):
        try:
            with open(exc_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== Tk 回调异常 {_t.strftime('%H:%M:%S')} =====\n")
                _tb.print_exception(exc, val, tbk, file=f)
        except Exception:
            pass
        try:
            _tb.print_exception(exc, val, tbk)
        except Exception:
            pass
    try:
        app.report_callback_exception = _report_exc
    except Exception:
        pass

    state = {"beat": 0, "dumped_at": -1}

    def tick():
        state["beat"] += 1
        app.after(500, tick)
    app.after(500, tick)

    def watch():
        last_beat, last_change = -1, _t.time()
        while True:
            _t.sleep(1.0)
            b = state["beat"]
            now = _t.time()
            if b != last_beat:
                last_beat, last_change = b, now
                continue
            if now - last_change >= stall_s and state["dumped_at"] != b:
                state["dumped_at"] = b
                try:
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(f"\n===== 卡死 (心跳停跳 {now-last_change:.1f}s) "
                                f"@beat {b} {_t.strftime('%H:%M:%S')} =====\n")
                        faulthandler.dump_traceback(file=f)
                        f.write("===== end =====\n")
                except Exception:
                    pass

    threading.Thread(target=watch, daemon=True, name="freeze-watchdog").start()


def _widget_text(w) -> str:
    """尽力取控件的显示文字（CTk 存在 ._text，原生 tk 用 cget('text')）。
    输入框取其内容（cget('text') 对 tk.Entry 会被 Tk 缩写匹配成 -textvariable，
    只拿到 PY_VAR1 这种变量名）；复选框附带勾选状态。"""
    try:
        v = getattr(w, "_text", None)
        if isinstance(v, str) and v:
            return v
    except Exception:
        pass
    try:
        if isinstance(w, tk.Entry):
            return w.get()
    except Exception:
        pass
    try:
        if "text" in w.keys():                 # 精确匹配 -text 选项，不走缩写
            t = w.cget("text")
            if isinstance(w, tk.Checkbutton):
                var = str(w.cget("variable"))
                if var:
                    t = f"{t} [{'x' if str(w.getvar(var)) in ('1', 'True', 'true') else ' '}]"
            if isinstance(t, str) and t:
                return t
    except Exception:
        pass
    return ""


def _widget_command_state(w) -> str:
    """'YES'/'NO' 表示这是个带 command 的按钮且是否已绑；'' 表示不是按钮。"""
    try:
        import customtkinter as _ctk
        if isinstance(w, _ctk.CTkButton):
            return "YES" if getattr(w, "_command", None) else "NO"
    except Exception:
        pass
    try:
        import tkinter as _tk
        if isinstance(w, _tk.Button):
            return "YES" if str(w.cget("command")).strip() else "NO"
    except Exception:
        pass
    return ""


def _dump_tree_and_quit(app, out_path: str) -> None:
    """把控件树 + 按钮接线审计写成纯文本到 out_path 后退出 —— 给 tools/dump.py 用。

    读的是控件『配置的文字/回调』而非像素，所以：
      1) 不受 macOS 后台进程推迟文字绘制的影响（滚动表单也能核实内容）；
      2) 能直接查出『按钮没绑 command』这类接线 bug，纯文本 diff，几乎不花 token。
    """
    lines, buttons = [], []

    def walk(w, depth):
        cls = w.__class__.__name__
        text = _widget_text(w)
        cmd = _widget_command_state(w)
        try:
            mapped = 1 if w.winfo_ismapped() else 0
        except Exception:
            mapped = 0
        try:
            geom = f"{w.winfo_width()}x{w.winfo_height()}+{w.winfo_x()}+{w.winfo_y()}"
        except Exception:
            geom = "?"
        row = ["  " * depth + cls]
        if text:
            row.append(f"text={text!r}")
        row.append(f"vis={mapped}")
        row.append(f"geom={geom}")
        if cmd:
            row.append(f"cmd={cmd}")
        lines.append(" ".join(row))
        if cmd:
            buttons.append((cmd, mapped, cls, text or "(无文字)"))
        try:
            for c in w.winfo_children():
                walk(c, depth + 1)
        except Exception:
            pass

    try:
        walk(app, 0)
    except Exception as e:
        lines.append(f"[dump] walk error: {e}")

    out = ["=== BUTTONS (接线审计) ==="]
    unwired = 0
    for cmd, vis, cls, text in buttons:
        flag = ""
        if cmd != "YES":
            flag = "   <== 未绑 command!"
            unwired += 1
        out.append(f"[{cmd:3}] vis={vis} {cls:16} {text}{flag}")
    out.append(f"\n共 {len(buttons)} 个按钮，其中 {unwired} 个未绑 command。")
    out.append("\n=== TREE ===")
    out.extend(lines)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        print(out_path)
    except Exception as e:
        print(f"[dump] write error: {e}")
    app.after(50, app.quit)


def _snap_and_quit(app, out_path: str) -> None:
    """截 HMSL 主窗口到 out_path 后退出。tools/snap.py 用它做无人值守 UI 迭代。

    两段式：先用 AppKit 把本 App 激活成前台 key 窗口（否则 macOS 推迟 label 文字
    绘制，截到空横幅/空字段 + 上一页残影），**延迟一拍**让 run loop 真正 key 化并
    画好文字，再用 CGWindowID 精确抓窗口（无视遮挡）。"""
    import subprocess as _sp

    def _capture():
        try:
            for _ in range(3):
                app.lift(); app.update_idletasks(); app.update()
            wid = _own_cg_window_id(app)
            if wid:
                # -l <id>：抓指定窗口自身内容，无视 z-order/遮挡；-o 去窗口阴影。
                _sp.run(["screencapture", "-x", "-o", "-l", str(wid), out_path],
                        check=False)
            else:
                # 回退：按屏幕区域抓（依赖窗口此刻在最前）。
                x, y = app.winfo_rootx(), app.winfo_rooty()
                w, h = app.winfo_width(), app.winfo_height()
                y_pad = 28                          # 把 macOS 标题栏也带进来
                _sp.run(["screencapture", "-x", "-R",
                         f"{x},{max(0, y - y_pad)},{w},{h + y_pad}", out_path],
                        check=False)
        except Exception as e:
            print(f"[snap_and_quit] capture error: {e}")
        finally:
            app.after(50, app.quit)

    # 激活成前台（应用激活“自己”无需任何权限），然后给 run loop ~0.5s 真正 key 化
    # 并触发文字绘制，再截 —— activateIgnoringOtherApps_ 是异步的，立刻截会太早。
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
    try:
        app.deiconify()
        app.attributes("-topmost", True)
        app.lift()
        app.focus_force()
        app.update()
    except Exception:
        pass
    app.after(500, _capture)


if __name__ == "__main__":
    import argparse
    import atexit
    _setup_frozen_stdio()          # 要在任何 print / argparse 输出之前
    parser = argparse.ArgumentParser(description="HMSL — Hello Minecraft! Server Launcher")
    parser.add_argument("--route", default="",
                        help="Auto-navigate after launch (e.g. 'versions', "
                             "'detail:我的世界服务器', 'config:我的世界服务器').")
    parser.add_argument("--snap", default="",
                        help="After --route, screencap the window to this PATH and exit. "
                             "Used by tools/snap.py for headless UI iteration.")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="Seconds to wait after route before snapping (default 1.5).")
    parser.add_argument("--dump-tree", dest="dump_tree", default="",
                        help="After --route, dump the widget tree + button wiring audit "
                             "to this PATH (text) and exit. Used by tools/dump.py. "
                             "No screenshot / no foreground activation needed.")
    parser.add_argument("--freeze-watchdog", dest="freeze_watchdog",
                        action="store_true",
                        help="Arm a background watchdog that dumps the main-thread "
                             "stack to /tmp/hmsl_freeze_stack.txt (Windows: "
                             "%%TEMP%%\\hmsl_freeze_stack.txt) whenever the UI "
                             "stalls >4s. For diagnosing real-foreground-window hangs.")
    # parse_known_args：双击 .app 启动时 macOS 有时会塞进程参数(-psn_…)，忽略掉不报错。
    args, _ = parser.parse_known_args()
    if args.snap and sys.platform != "darwin":
        # 截图依赖 macOS 的 screencapture / Quartz / AppKit
        print("--snap 仅 macOS 支持（其它平台请用 --dump-tree 核对界面内容）")
        sys.exit(2)
    _setup_windowed_logging()
    # 退出兜底：不经过 _on_app_close 的退出路径也要停掉 HMSL 启动的服务器
    atexit.register(_stop_leftover_servers)
    _install_exit_signal_handlers()

    app = HMSLApp()
    if args.freeze_watchdog:
        _arm_freeze_watchdog(app)
    if args.snap:
        # 截图模式：一启动就把窗口置顶，给 WM 充足时间把它浮到其它 App 之上。
        # screencapture -R 抓的是屏幕区域，HMSL 必须真在最前才拍得到；只在
        # 截图前一刻才 assert topmost 在 macOS 上是抢不过前台 App 的（会拍到
        # 盖在上面的窗口）。
        try:
            app.attributes("-topmost", True)
            app.lift()
        except Exception:
            pass
    if args.route:
        app.after(300, lambda: _route_to(app, args.route))
    if args.snap:
        # Run snap AFTER route + settle so the destination page is fully painted
        app.after(int((args.settle + 0.3) * 1000),
                  lambda: _snap_and_quit(app, args.snap))
    if args.dump_tree:
        # 控件树 dump：不需要抢前台，路由+settle 后直接遍历控件写文本再退出。
        app.after(int((args.settle + 0.3) * 1000),
                  lambda: _dump_tree_and_quit(app, args.dump_tree))
    _run_app(app)

import customtkinter as ctk
import os
import sys
import threading
import queue
import time
from tkinter import filedialog
from core.env_manager import EnvManager
from core.server_installer import ServerInstaller
from core.mod_downloader import ModDownloader
from core.server_factory import create_server
from core.launcher import start_server
from core.instance_registry import InstanceRegistry, RegistryEntry
from core.mod_scanner import scan_server_mods, disable_mods, ScanReport
from instance_scanner_test import InstanceScanner # 导入扫描器

# 设置外观主题
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class LogQueue:
    def __init__(self, textbox):
        self.queue = queue.Queue()
        self.textbox = textbox
        self.textbox.after(100, self.check_queue)
    def write(self, msg):
        if msg.strip(): self.queue.put(msg)
    def flush(self): pass
    def check_queue(self):
        while not self.queue.empty():
            msg = self.queue.get()
            self.textbox.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg.strip()}\n")
            self.textbox.see("end")
        self.textbox.after(100, self.check_queue)

class ConsoleWindow(ctk.CTkToplevel):
    """独立的服务器控制台窗口：实时日志、命令输入、停止按钮。"""
    def __init__(self, master, server_name, server_process):
        super().__init__(master)
        self.title(f"控制台 - {server_name}")
        self.geometry("760x520")
        self.sp = server_process
        self._closed = False

        ctk.CTkLabel(self, text=f"📟 {server_name}", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(10, 4))
        ctk.CTkLabel(self, text=server_process.server_path, text_color="gray", font=ctk.CTkFont(size=11)).pack()

        self.log_box = ctk.CTkTextbox(self, width=720, height=360, fg_color="#000000", text_color="#00ff66", font=("Menlo", 12))
        self.log_box.pack(padx=20, pady=10, fill="both", expand=True)

        input_row = ctk.CTkFrame(self, fg_color="transparent")
        input_row.pack(fill="x", padx=20, pady=(0, 10))
        self.cmd_var = ctk.StringVar()
        self.cmd_entry = ctk.CTkEntry(input_row, textvariable=self.cmd_var, placeholder_text="输入服务器命令（如 say hi, list, stop）…")
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cmd_entry.bind("<Return>", lambda e: self._send())
        ctk.CTkButton(input_row, text="发送", width=70, command=self._send).pack(side="left")
        ctk.CTkButton(input_row, text="⏹ 停止", width=80, fg_color="#a13b3b", hover_color="#823030", command=self._stop).pack(side="left", padx=(8, 0))

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
        self._append("[GUI] 正在请求服务器停止...")
        threading.Thread(target=lambda: self.sp.stop(grace=10.0), daemon=True).start()

    def _poll_output(self):
        if self._closed:
            return
        for line in self.sp.drain_lines():
            self._append(line)
        if not self.sp.is_alive():
            self._append("[GUI] 服务器进程已退出")
            return
        self.after(150, self._poll_output)

    def _on_close(self):
        self._closed = True
        if self.sp.is_alive():
            threading.Thread(target=lambda: self.sp.stop(grace=5.0), daemon=True).start()
        self.destroy()


class ModScanWindow(ctk.CTkToplevel):
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
            self.report = scan_server_mods(self.server_path, progress_callback=self._on_progress)
        except Exception as e:
            self.after(0, lambda: self._show_fatal(f"扫描失败：{e}"))
            return
        self.after(0, self._show_results)

    def _on_progress(self, current, total, filename):
        frac = current / total if total else 1.0
        self.after(0, lambda: (
            self.progress_bar.set(frac),
            self.progress_label.configure(text=f"{current}/{total}  {filename}"),
        ))

    def _show_fatal(self, msg):
        for w in self.progress_frame.winfo_children(): w.destroy()
        ctk.CTkLabel(self.progress_frame, text=msg, text_color="#e07a5f").pack(pady=40)

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
        ctk.CTkLabel(bottom, text="（被禁用的模组会移到 mods/.disabled/，可手动恢复）",
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
        moved = disable_mods(to_disable, self.report.mods_dir)
        self._toast(f"已禁用 {moved} 个模组，移至 mods/.disabled/。")
        # Remove disabled rows from UI by re-rendering
        self.report = scan_server_mods(self.server_path, lookup_fn=lambda s: None)  # quick reclassify
        for w in self.winfo_children():
            if isinstance(w, ctk.CTkScrollableFrame) or (isinstance(w, ctk.CTkFrame) and w.winfo_height() < 60):
                w.destroy()
        # Easiest: just close and let user re-run
        self.destroy()

    def _toast(self, msg):
        top = ctk.CTkToplevel(self); top.title("提示"); top.geometry("360x120")
        ctk.CTkLabel(top, text=msg, wraplength=320).pack(pady=20)
        ctk.CTkButton(top, text="知道了", width=100, command=top.destroy).pack()


class HMSLApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("HMSL - Hello Minecraft! Server Launcher")
        self.geometry("940x720")
        self.env = EnvManager()
        self.installer = ServerInstaller()
        self.db_path = os.path.join(self.env.script_dir, "MOD_DATABASE.md")
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
        self.show_home()

    def clear_main_frame(self):
        for widget in self.main_frame.winfo_children(): widget.destroy()

    def show_home(self):
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text="欢迎使用 HMSL", font=ctk.CTkFont(size=32, weight="bold")).pack(pady=(80, 10))
        ctk.CTkLabel(self.main_frame, text="专业、极简、高效的一键式开服管理中心", text_color="gray", font=ctk.CTkFont(size=14)).pack(pady=(0, 50))
        self.start_btn = ctk.CTkButton(self.main_frame, text="🚀 开启服务器", width=300, height=90, corner_radius=45, font=ctk.CTkFont(size=26, weight="bold"))
        self.start_btn.pack(pady=20)
        info_card = ctk.CTkFrame(self.main_frame, width=420, height=120, corner_radius=15)
        info_card.pack(pady=40, padx=40); info_card.pack_propagate(False)
        ctk.CTkLabel(info_card, text="当前选中实例", font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 5))
        ctk.CTkLabel(info_card, text="尚未选择服务器", text_color="#3b8ed0", font=ctk.CTkFont(size=16)).pack()

    def show_versions(self):
        """实装版本管理页：合并 [脚本目录扫描] + [跨目录注册表] 展示服务器卡片。
        采用 HMCL 式交互：卡片可点选，操作集中在底部 action bar。"""
        self.clear_main_frame()
        self.selected_instance = None
        self.instance_cards = []  # list of (card_frame, inst_dict) so we can update visuals
        ctk.CTkLabel(self.main_frame, text="服务器实例管理", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(10, 5))

        instances = self._collect_instances()

        if not instances:
            ctk.CTkLabel(self.main_frame, text="未发现任何服务器实例，快去创建一个吧！", text_color="gray").pack(pady=100)
            return

        # IMPORTANT: pack action_bar BEFORE the scroll_frame so tk reserves space for it.
        # Otherwise the scroll_frame's expand=True consumes everything and the bar is clipped
        # (same root cause as the wizard footer bug we fixed earlier).
        self._build_action_bar(self.main_frame)

        scroll_frame = ctk.CTkScrollableFrame(self.main_frame, width=680, height=460, fg_color="transparent")
        scroll_frame.pack(fill="both", expand=True, padx=10, pady=10)

        for inst in instances:
            self.create_server_card(scroll_frame, inst)

    def _collect_instances(self):
        """合并扫描结果和注册表，按绝对路径去重；扫描结果优先（包含 eula 等额外字段）。"""
        scanner = InstanceScanner(self.env.script_dir)
        scanned = scanner.scan()
        seen = {os.path.abspath(s["path"]): s for s in scanned}

        for entry in self.registry.live_entries():
            abs_path = os.path.abspath(entry.path)
            if abs_path in seen:
                continue
            seen[abs_path] = {
                "name": entry.name or os.path.basename(abs_path),
                "path": abs_path,
                "version": entry.mc_version or "未知版本",
                "type": entry.loader or "未知类型",
                "eula": os.path.isfile(os.path.join(abs_path, "eula.txt")),
            }
        return list(seen.values())

    def create_server_card(self, parent, inst):
        """卡片本身可点选；操作按钮统一放在底部 action bar，不再挤在卡片内。"""
        card = ctk.CTkFrame(parent, height=90, corner_radius=15, border_width=2, border_color="#2b2b2b")
        card.pack(fill="x", pady=8, padx=10); card.pack_propagate(False)

        icon = ctk.CTkLabel(card, text="📦", font=ctk.CTkFont(size=30))
        icon.pack(side="left", padx=20)
        info_box = ctk.CTkFrame(card, fg_color="transparent")
        info_box.pack(side="left", fill="both", expand=True, pady=12)

        name_label = ctk.CTkLabel(info_box, text=inst["name"], font=ctk.CTkFont(size=16, weight="bold"), anchor="w")
        name_label.pack(anchor="w", fill="x")
        status_text = "✅ EULA 已同意" if inst["eula"] else "⚠️ 待同意 EULA"
        meta_text = f"{inst['path']}  |  {status_text}"
        meta_label = ctk.CTkLabel(info_box, text=meta_text, font=ctk.CTkFont(size=11), text_color="gray", anchor="w", wraplength=520, justify="left")
        meta_label.pack(anchor="w", fill="x")

        # Bind click on every child too — Tk doesn't bubble events to parent automatically.
        clickable = [card, icon, info_box, name_label, meta_label]
        for w in clickable:
            w.bind("<Button-1>", lambda e, i=inst: self._select_instance(i))

        self.instance_cards.append((card, inst))

    def _select_instance(self, inst):
        """高亮选中的卡片，启用 action bar 按钮。"""
        self.selected_instance = inst
        sel_path = os.path.abspath(inst["path"])
        for card, c_inst in self.instance_cards:
            if os.path.abspath(c_inst["path"]) == sel_path:
                card.configure(border_color="#2b719e")  # 主题蓝
            else:
                card.configure(border_color="#2b2b2b")
        # Refresh action bar state
        if hasattr(self, "_action_bar_buttons"):
            for btn in self._action_bar_buttons:
                btn.configure(state="normal")
        if hasattr(self, "_selected_label"):
            self._selected_label.configure(text=f"已选中：{inst['name']}")

    def _build_action_bar(self, parent):
        """页面底部的统一操作栏。无选中时按钮 disabled。"""
        bar = ctk.CTkFrame(parent, height=88, corner_radius=15, fg_color="#1d1d1d")
        bar.pack(fill="x", side="bottom", padx=10, pady=(0, 10))
        bar.pack_propagate(False)

        self._selected_label = ctk.CTkLabel(bar, text="请从上方点选一个实例", text_color="gray", font=ctk.CTkFont(size=12))
        self._selected_label.pack(pady=(8, 0))

        btn_row = ctk.CTkFrame(bar, fg_color="transparent")
        btn_row.pack(pady=(4, 8))

        launch_btn = ctk.CTkButton(btn_row, text="▶ 启动", width=110, height=36, state="disabled",
                                   fg_color="#2b719e", hover_color="#1f538d",
                                   command=self._action_launch)
        launch_btn.pack(side="left", padx=6)

        folder_btn = ctk.CTkButton(btn_row, text="📂 文件夹", width=110, height=36, state="disabled",
                                   fg_color="#3d3d3d", hover_color="#4d4d4d",
                                   command=self._action_open_folder)
        folder_btn.pack(side="left", padx=6)

        scan_btn = ctk.CTkButton(btn_row, text="🧹 扫描模组", width=120, height=36, state="disabled",
                                 fg_color="#3d6b3d", hover_color="#4d7b4d",
                                 command=self._action_scan_mods)
        scan_btn.pack(side="left", padx=6)

        config_btn = ctk.CTkButton(btn_row, text="⚙ 编辑配置", width=110, height=36, state="disabled",
                                   fg_color="#3d3d3d", hover_color="#4d4d4d",
                                   command=self._action_edit_config)
        config_btn.pack(side="left", padx=6)

        remove_btn = ctk.CTkButton(btn_row, text="🗑 从列表移除", width=130, height=36, state="disabled",
                                   fg_color="#5a2b2b", hover_color="#7a3535",
                                   command=self._action_remove_from_registry)
        remove_btn.pack(side="left", padx=6)

        self._action_bar_buttons = [launch_btn, folder_btn, scan_btn, config_btn, remove_btn]

    def _action_launch(self):
        if not self.selected_instance: return
        self.open_console(self.selected_instance["name"], self.selected_instance["path"])

    def _action_open_folder(self):
        if not self.selected_instance: return
        path = self.selected_instance["path"]
        if sys.platform == "darwin":
            os.system(f"open '{path}'")
        elif sys.platform == "win32":
            os.system(f'explorer "{path}"')
        else:
            os.system(f"xdg-open '{path}'")

    def _action_edit_config(self):
        self._show_error("功能开发中", "可视化编辑 server.properties 的功能还未实装，下个版本见。")

    def _action_scan_mods(self):
        if not self.selected_instance: return
        ModScanWindow(self, self.selected_instance["name"], self.selected_instance["path"])

    def _action_remove_from_registry(self):
        if not self.selected_instance: return
        path = self.selected_instance["path"]
        removed = self.registry.remove(path)
        msg = "已从列表中移除（服务器文件并未删除）。" if removed else "该实例不在注册表中（可能是脚本目录下被扫描出的服务器），暂无法从列表移除。"
        self._show_error("移除结果", msg)
        if removed:
            self.show_versions()  # refresh

    def open_console(self, name, server_path):
        """打开一个独立控制台窗口，启动服务器并实时显示日志。"""
        try:
            sp = start_server(server_path)
        except FileNotFoundError as e:
            self._show_error(f"无法启动 {name}", str(e))
            return
        ConsoleWindow(self, name, sp)

    def _show_error(self, title, msg):
        top = ctk.CTkToplevel(self); top.title(title); top.geometry("420x160")
        ctk.CTkLabel(top, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(20, 10))
        ctk.CTkLabel(top, text=msg, wraplength=380, text_color="gray").pack(padx=20)
        ctk.CTkButton(top, text="知道了", width=100, command=top.destroy).pack(pady=15)

    def show_download(self):
        self.clear_main_frame()
        self.selected_ver = None; self.selected_type.set("")
        ctk.CTkLabel(self.main_frame, text="新建服务器向导", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(10, 20))

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
        self.name_entry = ctk.CTkEntry(left_box, placeholder_text="例如: my_server", textvariable=self.name_var, width=250); self.name_entry.pack(anchor="w", pady=(0, 15))

        ctk.CTkLabel(left_box, text="2. 创建位置", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.target_dir_var = ctk.StringVar(value=self.env.script_dir)
        self.target_dir_var.trace_add("write", lambda *args: self.validate_all())
        dir_row = ctk.CTkFrame(left_box, fg_color="transparent")
        dir_row.pack(anchor="w", fill="x", pady=(0, 15))
        self.dir_entry = ctk.CTkEntry(dir_row, textvariable=self.target_dir_var, width=180)
        self.dir_entry.pack(side="left")
        ctk.CTkButton(dir_row, text="浏览...", width=60, command=self.pick_target_dir).pack(side="left", padx=(6, 0))

        ctk.CTkLabel(left_box, text="3. 选择游戏版本 (搜索)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.ver_search_var = ctk.StringVar(); self.ver_search_var.trace_add("write", self.update_version_list)
        self.ver_entry = ctk.CTkEntry(left_box, placeholder_text="输入 1.20 等...", textvariable=self.ver_search_var, width=250); self.ver_entry.pack(anchor="w")
        self.ver_listbox = ctk.CTkScrollableFrame(left_box, width=230, height=140); self.ver_listbox.pack(anchor="w", pady=10)
        self.update_version_list()

        right_box = ctk.CTkFrame(body_frame, fg_color="transparent")
        right_box.pack(side="right", fill="both", expand=True, padx=20, pady=20)
        ctk.CTkLabel(right_box, text="4. 选择服务端类型", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.type_info_label = ctk.CTkLabel(right_box, text="请先从左侧选择版本", text_color="gray"); self.type_info_label.pack(pady=20)
        self.type_button_frame = ctk.CTkFrame(right_box, fg_color="transparent"); self.type_button_frame.pack(fill="both", expand=True)

    def update_version_list(self, *args):
        if self.is_updating_search: return
        search_term = self.ver_search_var.get().strip()
        for widget in self.ver_listbox.winfo_children(): widget.destroy()
        filtered = [v for v in self.full_versions if search_term in v]
        for v in filtered:
            ctk.CTkButton(self.ver_listbox, text=v, fg_color="transparent", text_color="white", hover_color="#2e2e2e", anchor="w", height=32, command=lambda ver=v: self.on_version_selected(ver)).pack(fill="x", padx=5)

    def on_version_selected(self, ver):
        self.selected_ver = ver; self.is_updating_search = True; self.ver_search_var.set(ver); self.is_updating_search = False
        self.refresh_type_menu(ver); self.validate_all()

    def refresh_type_menu(self, ver):
        self.type_info_label.configure(text=f"适用于 {ver} 的选项：", text_color="white")
        for widget in self.type_button_frame.winfo_children(): widget.destroy()
        options = ["Forge"]
        parts = [int(p) for p in ver.split('.')]
        v_num = parts[0]*10000 + parts[1]*100 + (parts[2] if len(parts)>2 else 0)
        if v_num >= 10808: options.append("Paper")
        if v_num >= 11400: options.append("Fabric")
        if v_num >= 12002: options.append("NeoForge")
        for opt in options:
            ctk.CTkRadioButton(self.type_button_frame, text=opt, variable=self.selected_type, value=opt, command=self.validate_all).pack(anchor="w", pady=10, padx=10)

    def validate_all(self):
        ok = (self.name_var.get().strip()
              and self.selected_ver
              and self.selected_type.get()
              and os.path.isdir(self.target_dir_var.get().strip()))
        if ok:
            self.finish_btn.configure(state="normal", fg_color="#2b719e")
        else:
            self.finish_btn.configure(state="disabled", fg_color=["#3B8ED0", "#1F6AA5"])

    def pick_target_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.target_dir_var.get() or self.env.script_dir,
                                         title="选择服务器创建位置")
        if chosen:
            self.target_dir_var.set(chosen)

    def start_installation(self):
        server_name = self.name_var.get().strip()
        version = self.selected_ver
        loader = self.selected_type.get()
        target_dir = self.target_dir_var.get().strip()
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text=f"正在部署：{server_name}", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=20)
        ctk.CTkLabel(self.main_frame, text=f"位置：{target_dir}", text_color="gray", font=ctk.CTkFont(size=12)).pack()
        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=540); self.progress_bar.set(0); self.progress_bar.pack(pady=15)
        self.progress_label = ctk.CTkLabel(self.main_frame, text="准备开始...", text_color="gray"); self.progress_label.pack()
        self.log_text = ctk.CTkTextbox(self.main_frame, width=650, height=320, fg_color="#000000", text_color="#00ff00")
        self.log_text.pack(pady=20)
        thread = threading.Thread(target=self.run_install_logic, args=(server_name, version, loader, target_dir))
        thread.daemon = True; thread.start()

    def run_install_logic(self, name, version, loader, target_dir):
        """GUI shim: hand off to pure create_server() and render its progress."""
        old_stdout = sys.stdout; sys.stdout = LogQueue(self.log_text)
        try:
            result = create_server(
                name=name,
                version=version,
                loader=loader,
                parent_dir=target_dir,
                env_manager=self.env,
                installer=self.installer,
                downloader=self.downloader,
                progress_callback=self.safe_update_progress,
            )
            if result.success:
                # Register the new instance so it's discoverable on the
                # version-management page even if it lives outside script_dir.
                try:
                    self.registry.add(RegistryEntry(
                        name=name, path=result.server_path,
                        loader=loader, mc_version=version,
                    ))
                except Exception as e:
                    print(f"[警告] 写入实例注册表失败：{e}")
                self.safe_update_progress(1.0, "✨ 服务器部署成功！")
                self.after(500, lambda: ctk.CTkButton(self.main_frame, text="完成并返回", command=self.show_home, width=200).pack(pady=10))
            else:
                self.safe_update_progress(0.0, f"❌ 安装失败：{result.error}")
        except Exception as e:
            print(f"致命错误: {e}")
        finally:
            sys.stdout = old_stdout

    def safe_update_progress(self, val, text):
        self.after(0, lambda: self._update_ui_state(val, text))

    def _update_ui_state(self, val, text):
        self.progress_bar.set(val); self.progress_label.configure(text=text)

if __name__ == "__main__":
    app = HMSLApp()
    app.mainloop()

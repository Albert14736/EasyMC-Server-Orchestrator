import customtkinter as ctk
import os
import sys

# 设置外观主题
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class HMSLApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("HMSL - Hello Minecraft! Server Launcher")
        self.geometry("940x650")

        # 配置网格权重
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- 核心数据 ---
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
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def show_home(self):
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text="欢迎使用 HMSL", font=ctk.CTkFont(size=32, weight="bold")).pack(pady=(80, 10))
        ctk.CTkLabel(self.main_frame, text="专业、极简、高效的一键式开服管理中心", text_color="gray", font=ctk.CTkFont(size=14)).pack(pady=(0, 50))
        
        self.start_btn = ctk.CTkButton(self.main_frame, text="🚀 开启服务器", 
                                       width=300, height=90, corner_radius=45,
                                       font=ctk.CTkFont(size=26, weight="bold"))
        self.start_btn.pack(pady=20)
        
        info_card = ctk.CTkFrame(self.main_frame, width=420, height=120, corner_radius=15)
        info_card.pack(pady=40, padx=40)
        info_card.pack_propagate(False)
        ctk.CTkLabel(info_card, text="当前选中实例", font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(20, 5))
        ctk.CTkLabel(info_card, text="尚未选择服务器", text_color="#3b8ed0", font=ctk.CTkFont(size=16)).pack()

    def show_versions(self):
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text="版本实例管理", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=20)
        ctk.CTkLabel(self.main_frame, text="这里将列出您所有的服务器文件夹...", text_color="gray").pack(pady=100)

    def show_download(self):
        self.clear_main_frame()
        # 重置变量
        self.selected_ver = None
        self.selected_type.set("")

        # 标题
        ctk.CTkLabel(self.main_frame, text="新建服务器向导", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=(10, 20))
        
        # 使用 Header-Body-Footer 结构确保布局稳定
        body_frame = ctk.CTkFrame(self.main_frame, corner_radius=15)
        body_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Body: 左侧 (输入) ---
        left_box = ctk.CTkFrame(body_frame, fg_color="transparent")
        left_box.pack(side="left", fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(left_box, text="1. 服务器名称", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.name_var = ctk.StringVar()
        self.name_var.trace_add("write", lambda *args: self.validate_all())
        self.name_entry = ctk.CTkEntry(left_box, placeholder_text="例如: my_server", textvariable=self.name_var, width=250)
        self.name_entry.pack(anchor="w", pady=(0, 20))

        ctk.CTkLabel(left_box, text="2. 选择游戏版本 (搜索)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.ver_search_var = ctk.StringVar()
        self.ver_search_var.trace_add("write", self.update_version_list)
        self.ver_entry = ctk.CTkEntry(left_box, placeholder_text="输入 1.20 等...", textvariable=self.ver_search_var, width=250)
        self.ver_entry.pack(anchor="w")

        self.ver_listbox = ctk.CTkScrollableFrame(left_box, width=230, height=180)
        self.ver_listbox.pack(anchor="w", pady=15)
        self.update_version_list()

        # --- Body: 右侧 (端选择) ---
        self.right_box = ctk.CTkFrame(body_frame, fg_color="transparent")
        self.right_box.pack(side="right", fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(self.right_box, text="3. 选择服务端类型", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 5))
        self.type_info_label = ctk.CTkLabel(self.right_box, text="请先从左侧选择版本", text_color="gray")
        self.type_info_label.pack(pady=20)

        self.type_button_frame = ctk.CTkFrame(self.right_box, fg_color="transparent")
        self.type_button_frame.pack(fill="both", expand=True)

        # --- Footer: 始终可见的创建按钮 ---
        footer_frame = ctk.CTkFrame(self.main_frame, height=80, fg_color="transparent")
        footer_frame.pack(fill="x", side="bottom", pady=10)
        
        self.finish_btn = ctk.CTkButton(footer_frame, text="开始创建服务器", state="disabled", 
                                       width=240, height=50, corner_radius=25,
                                       command=self.start_installation)
        self.finish_btn.pack()

    def update_version_list(self, *args):
        if self.is_updating_search: return
        search_term = self.ver_search_var.get().strip()
        for widget in self.ver_listbox.winfo_children(): widget.destroy()
        filtered = [v for v in self.full_versions if search_term in v]
        for v in filtered:
            btn = ctk.CTkButton(self.ver_listbox, text=v, fg_color="transparent", text_color="white", 
                                hover_color="#2e2e2e", anchor="w", height=32,
                                command=lambda ver=v: self.on_version_selected(ver))
            btn.pack(fill="x", padx=5)

    def on_version_selected(self, ver):
        self.selected_ver = ver
        self.is_updating_search = True
        self.ver_search_var.set(ver)
        self.is_updating_search = False
        self.refresh_type_menu(ver)
        self.validate_all()

    def refresh_type_menu(self, ver):
        self.type_info_label.configure(text=f"适用于 {ver} 的选项：", text_color="white")
        for widget in self.type_button_frame.winfo_children(): widget.destroy()
        
        options = ["Forge"]
        try:
            parts = [int(p) for p in ver.split('.')]
            v_num = parts[0]*10000 + parts[1]*100 + (parts[2] if len(parts)>2 else 0)
            if v_num >= 10808: options.append("Paper")
            if v_num >= 11400: options.append("Fabric")
            if v_num >= 12002: options.append("NeoForge")
        except: options = ["Forge", "Paper", "Fabric", "NeoForge"]

        for opt in options:
            rb = ctk.CTkRadioButton(self.type_button_frame, text=opt, variable=self.selected_type, 
                                     value=opt, command=self.validate_all)
            rb.pack(anchor="w", pady=10, padx=10)

    def validate_all(self):
        """检查所有条件是否满足，以启用创建按钮"""
        name = self.name_var.get().strip()
        version = self.selected_ver
        stype = self.selected_type.get()
        
        if name and version and stype:
            self.finish_btn.configure(state="normal", fg_color="#2b719e")
        else:
            self.finish_btn.configure(state="disabled", fg_color=["#3B8ED0", "#1F6AA5"]) # 恢复默认禁用的颜色

    def start_installation(self):
        server_name = self.name_var.get().strip()
        version = self.selected_ver
        loader = self.selected_type.get()
        
        self.clear_main_frame()
        ctk.CTkLabel(self.main_frame, text=f"正在部署：{server_name}", font=ctk.CTkFont(size=24, weight="bold")).pack(pady=20)
        
        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=540)
        self.progress_bar.set(0); self.progress_bar.pack(pady=15)
        self.progress_label = ctk.CTkLabel(self.main_frame, text="准备开始...", text_color="gray")
        self.progress_label.pack()

        self.log_text = ctk.CTkTextbox(self.main_frame, width=650, height=320, 
                                       fg_color="#000000", text_color="#00ff00", # 黑底绿字，更硬核
                                       font=ctk.CTkFont(family="Courier", size=12))
        self.log_text.pack(pady=20)
        
        self.append_log(f">>> 任务启动: MC {version} / {loader}")
        self.after(800, lambda: self.update_progress(0.2, "正在初始化环境..."))
        self.after(2000, lambda: self.update_progress(0.5, "正在下载服务端 JAR (从镜像站)..."))
        self.after(4000, lambda: self.update_progress(0.8, "同步模组数据库 (Modrinth)..."))
        self.after(6000, lambda: self.update_progress(1.0, "服务器部署成功！"))

    def append_log(self, message):
        self.log_text.insert("end", f"[{os.popen('date +%H:%M:%S').read().strip()}] {message}\n")
        self.log_text.see("end")

    def update_progress(self, val, text):
        self.progress_bar.set(val); self.progress_label.configure(text=text)
        self.append_log(text)
        if val == 1.0:
            ctk.CTkButton(self.main_frame, text="完成并返回首页", command=self.show_home, width=200).pack(pady=10)

if __name__ == "__main__":
    app = HMSLApp()
    app.mainloop()

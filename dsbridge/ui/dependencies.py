"""Non-blocking dependency status and installation controls for the desktop UI."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from dsbridge.dependencies.manager import DependencyManager, LABELS, PACKAGES


class DependencyPanel:
    def __init__(self, parent, base, *, log=lambda message: None, manager=None, profiles=None):
        from dsbridge.core.modes import PROFILES
        self.profiles = PROFILES if profiles is None else profiles
        self.manager = manager or DependencyManager(base)
        self.log = log
        self.rows = None
        self.checking = self.installing = self.converting = False
        self.dialog = None
        self.disposed = False
        self.timer = None
        self.events = queue.Queue()
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill="x", pady=(8, 2))
        self.summary = tk.StringVar(value="依赖：正在检查…")
        ttk.Label(self.frame, textvariable=self.summary, foreground="#43516a").pack(side="left", fill="x", expand=True)
        self.open_button = ttk.Button(self.frame, text="依赖检查与安装", command=self.open)
        self.open_button.pack(side="right")
        self.last_message = "检测不需要连接手柄。安装时请先退出游戏，并确认 Windows 管理员权限提示。"
        self.frame.after(0, self.refresh)
        self.timer = self.frame.after(100, self._pump)
        self.frame.bind("<Destroy>", self._destroyed, add=True)

    def _destroyed(self, event):
        if event.widget == self.frame:
            self.disposed = True
            if self.timer:
                self.frame.after_cancel(self.timer)
                self.timer = None

    def set_conversion_busy(self, busy):
        self.converting = busy
        self._render()

    def guard_start(self, mode):
        # Driver installation and any controller operation must not overlap.
        if self.installing:
            self.open()
            return False
        if mode not in self.profiles:
            return False
        needed = self.profiles[mode].dependencies
        rows = {row.key: row for row in self.rows or []}
        if self.checking or self.rows is None or any(key not in rows or rows[key].state != "ready" for key in needed):
            self.open()
            return False
        return True

    def open(self):
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.lift()
            return
        self.dialog = tk.Toplevel(self.frame)
        self.dialog.title("依赖检查与安装 · 八爪鱼震动桥")
        self.dialog.geometry("780x750")
        self.dialog.minsize(730, 715)
        self.dialog.transient(self.frame.winfo_toplevel())
        box = ttk.Frame(self.dialog, padding=20)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="新电脑准备", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(box, text="USBip 创建 PS5 DualSense 手柄；HidHide 隔离实体输入。",
                  wraplength=670).pack(anchor="w", pady=(5, 12))
        self.row_vars = {}
        for package in self.manager.packages:
            card = ttk.LabelFrame(box, text=package.name, padding=12)
            card.pack(fill="x", pady=(0, 10))
            status, detail, bundle = (tk.StringVar() for _ in range(3))
            self.row_vars[package.key] = (status, detail, bundle)
            ttk.Label(card, textvariable=status, font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
            ttk.Label(card, textvariable=detail, wraplength=640).pack(anchor="w", pady=4)
            ttk.Label(card, textvariable=bundle, wraplength=640, foreground="#58677b").pack(anchor="w")
        self.message = tk.StringVar()
        ttk.Label(box, textvariable=self.message, wraplength=670, foreground="#195d98").pack(anchor="w", pady=5)
        ttk.Label(box, text="安装包随完整发布文件夹提供。安装后会重新检查，并提示是否需要重启。",
                  wraplength=670).pack(anchor="w", pady=(5, 0))
        ttk.Label(box, text="本程序不会自动重启电脑；已安装的依赖不会被重复安装或替换。",
                  wraplength=670).pack(anchor="w", pady=(0, 10))
        actions = ttk.Frame(box)
        actions.pack(side="bottom", fill="x")
        self.refresh_button = ttk.Button(actions, text="重新检查", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.install_button = ttk.Button(actions, text="安装缺失依赖", command=self.install)
        self.install_button.pack(side="left", padx=10)
        self.close_button = ttk.Button(actions, text="关闭", command=self.close)
        self.close_button.pack(side="right")
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self._render()

    def close(self):
        if self.installing:
            self.last_message = "安装仍在进行，请等待结果；Windows 权限提示中可取消本次安装。"
            self._render()
            return
        if self.dialog:
            self.dialog.destroy()
            self.dialog = None

    def refresh(self):
        if self.checking or self.installing or self.converting:
            return
        self.checking = True
        self._render()
        def work():
            try:
                self.events.put(("checked", self.manager.check()))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=work, name="Check driver dependencies", daemon=True).start()

    def install(self):
        if self.checking or self.installing or self.converting or not self.rows:
            return
        if not any(row.can_install for row in self.rows):
            return
        self.installing = True
        self.last_message = "正在校验安装包并重新检查依赖…"
        self._render()
        def work():
            try:
                report = self.manager.install_missing(lambda text: self.events.put(("progress", text)))
                self.events.put(("installed", (report, self.manager.check())))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        # Do not abandon a live installer if the GUI receives an exit request.
        threading.Thread(target=work, name="Install missing drivers", daemon=False).start()

    def _render(self):
        if self.installing:
            self.summary.set("依赖：正在安装，请等待结果…")
        elif self.checking:
            self.summary.set("依赖：正在检查…")
        elif self.rows:
            self.summary.set("依赖：" + " · ".join(row.name + " " + LABELS[row.state] for row in self.rows))
        if not self.dialog or not self.dialog.winfo_exists():
            return
        for package in self.manager.packages:
            status, detail, bundle = self.row_vars[package.key]
            row = next((row for row in self.rows or [] if row.key == package.key), None)
            status.set(LABELS[row.state] + (" · " + row.version if row.version else "") if row else "正在检查…")
            detail.set(row.detail if row else "")
            bundle.set(row.package_detail if row else "")
        busy = self.checking or self.installing or self.converting
        self.refresh_button.configure(state="disabled" if busy else "normal")
        installable = any(row.can_install for row in self.rows or [])
        self.install_button.configure(state="normal" if installable and not busy else "disabled")
        self.close_button.configure(state="disabled" if self.installing else "normal")
        self.message.set("请先停止转换，再检查或安装依赖。" if self.converting else self.last_message)

    def _pump(self):
        if self.disposed:
            return
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "checked":
                    self.rows, self.checking = value, False
                elif kind == "progress":
                    self.last_message = value
                    self.log(value)
                elif kind == "installed":
                    report, self.rows = value
                    self.installing = False
                    outcomes = report["results"]
                    self.last_message = "；".join(item.get("message", "") for item in outcomes if item["outcome"] != "skipped") or "检查完成，没有可自动安装的缺失依赖。"
                    self.log(self.last_message)
                elif kind == "error":
                    self.checking = self.installing = False
                    self.rows = None  # An uncertain check must never reuse stale readiness.
                    self.last_message = "依赖操作失败：" + value
                    self.summary.set(self.last_message)
                    self.log(self.last_message)
                self._render()
        except queue.Empty:
            pass
        self.timer = self.frame.after(100, self._pump)

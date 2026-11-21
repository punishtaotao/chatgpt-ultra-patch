# -*- coding: utf-8 -*-
"""ChatGPT Ultra Patch - one-click GUI (v2.0).

Windows only.  Run with pythonw (no console window):

    pythonw UltraPatch.pyw

Steps automated, in order:
  1. locate the installed ChatGPT app (WindowsApps, auto-detect or browse)
  2. copy the whole `app` folder to a writable location
  3. patch the asar (unlock the ultra effort level)
  4. fix the integrity hash embedded in ChatGPT.exe
  5. verify every file inside the asar
  6. (optional buttons) desktop shortcut / launch the patched app

Zero third-party dependencies (stdlib tkinter only).
"""
import os, sys, glob, shutil, threading, queue, subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import patch_asar as _pa          # noqa: E402
import patch_exe_hash as _peh     # noqa: E402
import check_asar as _ca          # noqa: E402

WINDOWSAPPS = r"C:\Program Files\WindowsApps"
DEFAULT_TARGET = os.path.join(os.environ.get("USERPROFILE", "C:\\Users\\you"), "ChatGPTPatched", "app")


# ---------------------------------------------------------------- helpers
def detect_installed_apps():
    """Return candidate paths of the installed `app` folder(s), best effort."""
    found = []
    # 1) direct listing (works on many setups without admin)
    for pat in (os.path.join(WINDOWSAPPS, "OpenAI.Codex_*", "app"),
                os.path.join(WINDOWSAPPS, "*ChatGPT*", "app")):
        try:
            found.extend(glob.glob(pat))
        except OSError:
            pass
    if found:
        return sorted(set(found), reverse=True)  # newest version first
    return []


def create_desktop_shortcut(target_exe, log):
    """Create Desktop\\ChatGPT.lnk pointing at target_exe (pure ctypes)."""
    import ctypes
    from ctypes import wintypes

    desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    lnk = os.path.join(desktop, "ChatGPT.lnk")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def to_guid(s):
        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g

    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    punk = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(to_guid("{00021401-0000-0000-C000-000000000046}")),
                           None, 1,
                           ctypes.byref(to_guid("{000214F9-0000-0000-C000-000000000046}")),
                           ctypes.byref(punk))
    vtbl = ctypes.cast(ctypes.cast(punk, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                       ctypes.POINTER(ctypes.c_void_p))
    F = ctypes.WINFUNCTYPE
    F(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[9])(punk, target_exe)          # SetPath
    F(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[11])(punk, os.path.dirname(target_exe))  # SetWorkingDirectory
    F(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(vtbl[7])(punk, target_exe, 0)  # SetIconLocation
    F(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[19])(punk, "ChatGPT (patched)")          # SetDescription
    ppf = ctypes.c_void_p()
    F(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
        vtbl[0])(punk, ctypes.byref(to_guid("{0000010b-0000-0000-C000-000000000046}")), ctypes.byref(ppf))
    vpf = ctypes.cast(ctypes.cast(ppf, ctypes.POINTER(ctypes.c_void_p)).contents.value,
                      ctypes.POINTER(ctypes.c_void_p))
    F(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)(vpf[6])(ppf, lnk, 1)  # Save
    ole32.CoUninitialize()
    if not os.path.exists(lnk):
        raise RuntimeError("shortcut was not created: %s" % lnk)
    log("桌面快捷方式已创建: %s" % lnk)
    return lnk


def copy_app(src, dst, log, progress):
    """Copy the app folder with progress feedback. Skips if already copied."""
    if os.path.isfile(os.path.join(dst, "ChatGPT.exe")):
        log("目标目录已存在（含 ChatGPT.exe），跳过复制")
        return
    n = [0]
    def counter(src_path, dst_path):
        n[0] += 1
        if n[0] % 300 == 0:
            progress("复制中... %d 个文件" % n[0])
        return shutil.copy2(src_path, dst_path, follow_symlinks=False)
    log("复制 %s -> %s (首次约 2~5 分钟，请耐心等待)" % (src, dst))
    os.makedirs(dst, exist_ok=True)
    shutil.copytree(src, dst, copy_function=counter, dirs_exist_ok=True)
    progress("复制完成，共 %d 个文件" % n[0])


# ---------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        root.title("ChatGPT 紫色 Ultra 补丁工具 v2.0")
        root.geometry("760x560")
        root.minsize(680, 480)

        self.q = queue.Queue()
        self.busy = False

        pad = dict(padx=10, pady=6)
        frm = ttk.Frame(root)
        frm.pack(fill="both", expand=True, padx=14, pady=10)

        # -- step 1: source
        ttk.Label(frm, text="第 1 步 · 原版应用位置（自动检测，检测不到就手动选 ChatGPT.exe）",
                  font=("", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        self.src_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.src_var, width=70).grid(row=1, column=0, columnspan=2, sticky="we", **pad)
        ttk.Button(frm, text="浏览…", command=self.browse_src).grid(row=1, column=2, **pad)

        # -- step 2: target
        ttk.Label(frm, text="第 2 步 · 副本位置（保持默认就行）",
                  font=("", 10, "bold")).grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        self.dst_var = tk.StringVar(value=DEFAULT_TARGET)
        ttk.Entry(frm, textvariable=self.dst_var, width=70).grid(row=3, column=0, columnspan=2, sticky="we", **pad)
        ttk.Button(frm, text="浏览…", command=self.browse_dst).grid(row=3, column=2, **pad)

        # -- step 3: one click
        ttk.Label(frm, text="第 3 步 · 点下面的按钮，全自动完成（复制 → 补丁 → 修哈希 → 自检）",
                  font=("", 10, "bold")).grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        self.go_btn = tk.Button(frm, text="⚡ 一键补丁", font=("", 13, "bold"),
                                bg="#7c3aed", fg="white", activebackground="#6d28d9",
                                relief="flat", height=2, command=self.run_all)
        self.go_btn.grid(row=5, column=0, columnspan=3, sticky="we", **pad)
        frm.columnconfigure(0, weight=1)

        # -- extras
        extra = ttk.Frame(frm)
        extra.grid(row=6, column=0, columnspan=3, sticky="we", **pad)
        ttk.Button(extra, text="创建桌面快捷方式", command=self.make_shortcut).pack(side="left", padx=(0, 8))
        ttk.Button(extra, text="启动补丁版", command=self.launch_app).pack(side="left", padx=8)
        ttk.Button(extra, text="打开副本目录", command=self.open_dir).pack(side="left", padx=8)

        # -- log
        ttk.Label(frm, text="运行日志").grid(row=7, column=0, sticky="w", **pad)
        self.log = scrolledtext.ScrolledText(frm, height=14, state="disabled", font=("Consolas", 9))
        self.log.grid(row=8, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(8, weight=1)

        # disclaimer
        ttk.Label(frm, foreground="#888",
                  text="仅供学习研究。不包含任何 OpenAI 程序文件；仅修改你自己机器上的副本。风险自负。",
                  font=("", 8)).grid(row=9, column=0, columnspan=3, sticky="w", **pad)

        # auto-detect
        cands = detect_installed_apps()
        if cands:
            self.src_var.set(cands[0])
            self.p("自动检测到原版应用: %s" % cands[0])
            if len(cands) > 1:
                self.p("（另有 %d 个版本候选，可手动改路径）" % (len(cands) - 1))
        else:
            self.p("没自动检测到 WindowsApps 里的应用（无权限时常见）——点「浏览…」选原版 ChatGPT.exe 即可")

        self.root.after(120, self.pump)

    # ------------- logging
    def p(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def pump(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                self.p(text)
                if kind == "error":
                    self.p("❌ " + text)
                elif kind == "done":
                    self.busy = False
                    self.go_btn.configure(state="normal", text="⚡ 一键补丁")
                elif kind == "progress":
                    self.go_btn.configure(text="⏳ " + text)
        except queue.Empty:
            pass
        self.root.after(120, self.pump)

    # ------------- browsing
    def browse_src(self):
        f = filedialog.askopenfilename(
            title="选择原版的 ChatGPT.exe（在 WindowsApps\\OpenAI.Codex_*\\app 里）",
            filetypes=[("ChatGPT.exe", "ChatGPT.exe"), ("所有文件", "*.*")])
        if f:
            self.src_var.set(f)

    def browse_dst(self):
        d = filedialog.askdirectory(title="选择副本目录（就是放补丁版的文件夹）")
        if d:
            self.dst_var.set(d)

    # ------------- paths from UI
    def paths(self):
        src = self.src_var.get().strip().strip('"')
        dst = self.dst_var.get().strip().strip('"')
        if not src:
            raise RuntimeError("请先填写/选择原版应用位置")
        if os.path.isfile(src):          # user picked ChatGPT.exe
            src = os.path.dirname(src)
        if not os.path.isfile(os.path.join(src, "ChatGPT.exe")):
            raise RuntimeError("所选位置没有 ChatGPT.exe：%s" % src)
        if not dst:
            raise RuntimeError("请填写副本位置")
        return src, dst

    # ------------- worker plumbing
    def run_in_thread(self, fn):
        if self.busy:
            messagebox.showinfo("稍等", "上一个任务还在跑")
            return
        try:
            src, dst = self.paths()
        except RuntimeError as e:
            messagebox.showwarning("还差一步", str(e))
            return
        self.busy = True
        self.go_btn.configure(state="disabled", text="⏳ 处理中…")
        threading.Thread(target=fn, args=(src, dst), daemon=True).start()

    def run_all(self):
        def work(src, dst):
            try:
                self.q.put(("log", "== 第 1/4 步：复制副本 =="))
                copy_app(src, dst, self.p2, lambda t: self.q.put(("progress", t)))
                exe = os.path.join(dst, "ChatGPT.exe")
                asar = os.path.join(dst, "resources", "app.asar")
                self.q.put(("log", "== 第 2/4 步：打 asar 补丁（解锁 Ultra 档）=="))
                res = _pa.patch_asar(asar, self.p2)
                self.q.put(("log", "补丁文件数: %d，新头哈希: %s" % (res["patched_files"], res["new_header_sha256"])))
                self.q.put(("log", "== 第 3/4 步：同步 exe 内嵌哈希（跳过必崩）=="))
                _peh.patch_exe_hash(exe, asar, backup=True, log=self.p2)
                self.q.put(("log", "== 第 4/4 步：全量自检 =="))
                chk = _ca.check_asar(asar)
                if not chk["ok"]:
                    raise RuntimeError("自检发现 %d 个不匹配文件，补丁未生效" % len(chk["mismatches"]))
                self.q.put(("log", "自检通过：%d 个文件全部一致 ✅" % chk["checked"]))
                self.q.put(("log", ""))
                self.q.put(("log", "🎉 全部完成！点「启动补丁版」试试，模型列表最右端就是紫色 Ultra 档"))
                self.q.put(("done", "完成"))
            except _pa.PatchError as e:
                if "already" in str(e) or "already patched" in str(e) or "nothing matched" in str(e):
                    self.q.put(("log", "⚠️ %s（如果副本已打过补丁，直接启动即可）" % e))
                    self.q.put(("done", ""))
                else:
                    self.q.put(("error", str(e)))
                    self.q.put(("done", ""))
            except Exception as e:
                self.q.put(("error", "%s: %s" % (type(e).__name__, e)))
                self.q.put(("done", ""))
        self.run_in_thread(work)

    def p2(self, s):
        self.q.put(("log", s))

    # ------------- extra actions (no heavy work)
    def _target_exe(self):
        dst = self.dst_var.get().strip().strip('"')
        exe = os.path.join(dst, "ChatGPT.exe")
        if not os.path.isfile(exe):
            messagebox.showwarning("还没有副本", "副本目录里还没有 ChatGPT.exe，先跑「一键补丁」")
            return None
        return exe

    def make_shortcut(self):
        exe = self._target_exe()
        if not exe:
            return
        try:
            create_desktop_shortcut(exe, self.p)
            messagebox.showinfo("好了", "桌面快捷方式已创建")
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def launch_app(self):
        exe = self._target_exe()
        if not exe:
            return
        # CREATE_NO_WINDOW
        subprocess.Popen([exe], creationflags=0x08000000, close_fds=True)
        self.p("已启动补丁版: %s" % exe)

    def open_dir(self):
        dst = self.dst_var.get().strip().strip('"')
        if os.path.isdir(dst):
            subprocess.Popen(["explorer", dst])


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

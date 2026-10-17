"""WinRemote App —— 图形控制台（tkinter，启停后台 daemon 进程）。

作用：在远程电脑上提供一个简单的小窗口，
  - 显示当前服务（daemon）运行状态
  - 「开启进程」：以 --daemon 后台方式拉起本程序
  - 「关闭进程」：优雅结束正在运行的 daemon
  - 状态自动刷新（2 秒）

与平台无关的核心逻辑（find/start/stop）与 tkinter 界面分离，
便于在无显示环境（CI/Linux headless）下做单元测试。

用法：
    winremote-app --gui          # 打开图形控制台
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

LOG = logging.getLogger("winremote-app.gui_console")

APP_NAME = "winremote-app"


# ---------------------------------------------------------------------------
# 平台无关的启停逻辑（可单测）
# ---------------------------------------------------------------------------

def self_exe() -> str:
    """返回当前可执行文件：PyInstaller 打包后为 exe 路径，源码运行为 python。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return sys.executable


def launch_args(config_path: str) -> list[str]:
    """构造拉起 daemon 的命令行参数。"""
    if getattr(sys, "frozen", False):
        return [self_exe(), "--daemon", "-c", str(config_path)]
    return [
        sys.executable, "-m", "winremote_app",
        "--daemon", "-c", str(config_path),
    ]


def _norm_path(p: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(p))
    except Exception:  # noqa: BLE001
        return ""


def _pid_matches(proc: psutil.Process, exe: str, config_path: str) -> bool:
    """判断进程是否是「本配置」的 daemon。

    规则（防止误认/误杀其他实例）：
    1. 命令行必须含 --daemon，且不是 --gui；
    2. daemon 若带 -c/--config：config 路径必须与本实例一致
       （相对路径按该进程的 cwd 解析后再比较）；
    3. daemon 未带 config：只认 exe 路径与本实例完全相同的
       （同目录部署的默认场景）。
    """
    try:
        if proc.pid == os.getpid():
            return False
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    if "--daemon" not in cmdline or "--gui" in cmdline:
        return False

    self_cfg = _norm_path(config_path) if config_path else ""

    # 提取该进程命令行里的 config 值
    c_vals: list = []
    for i, c in enumerate(cmdline):
        if c in ("-c", "--config") and i + 1 < len(cmdline):
            c_vals.append(cmdline[i + 1])
        elif c.startswith("-c=") or c.startswith("--config="):
            c_vals.append(c.split("=", 1)[1])
    try:
        pcwd = proc.cwd()
    except Exception:  # noqa: BLE001
        pcwd = None
    c_norms = []
    for v in c_vals:
        if v and not os.path.isabs(v) and pcwd:
            v = os.path.join(pcwd, v)
        if v:
            c_norms.append(_norm_path(v))

    if c_norms:
        # 带了 config 的 daemon：路径必须一致
        return bool(self_cfg) and self_cfg in c_norms

    # 未带 config 的 daemon：exe 完全同路径才算
    try:
        p_exe = proc.exe()
    except Exception:  # noqa: BLE001
        p_exe = None
    if p_exe:
        return _norm_path(p_exe) == _norm_path(exe)
    return False


def find_daemon_process(config_path: str | None = None) -> psutil.Process | None:
    """找到正在运行的 daemon 进程；不存在或权限不足时返回 None。"""
    exe = self_exe()
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _pid_matches(proc, exe, config_path or ""):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def start_daemon(config_path: str) -> bool:
    """以 --daemon 后台方式拉起自身，返回是否成功启动。"""
    if find_daemon_process(config_path):
        LOG.info("daemon 已在运行，跳过启动")
        return True
    args = launch_args(config_path)
    LOG.info("启动 daemon: %s", " ".join(args))
    try:
        if os.name == "nt":
            creationflags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception as e:
        LOG.error("启动 daemon 失败: %s", e)
        return False


def stop_daemon(config_path: str, timeout: float = 5.0) -> bool:
    """结束正在运行的 daemon：先优雅 terminate，超时则 kill。"""
    proc = find_daemon_process(config_path)
    if proc is None:
        return True
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return find_daemon_process(config_path) is None
    return find_daemon_process(config_path) is None


def restart_daemon(config_path: str, stop_timeout: float = 5.0) -> bool:
    """重启 daemon：先停掉正在运行的进程，再以 --daemon 拉起。"""
    proc = find_daemon_process(config_path)
    if proc is not None:
        if not stop_daemon(config_path, timeout=stop_timeout):
            LOG.error("重启失败：无法停止旧进程 PID=%s", proc.pid)
            return False
    time.sleep(0.4)
    return start_daemon(config_path)


def daemon_status(config_path: str | None = None) -> dict:
    """返回 daemon 运行状态摘要。"""
    proc = find_daemon_process(config_path)
    if proc is None:
        return {"running": False, "pid": None, "started": None}
    try:
        started = proc.create_time()
    except Exception:
        started = None
    return {"running": True, "pid": proc.pid, "started": started}


# ---------------------------------------------------------------------------
# tkinter 界面
# ---------------------------------------------------------------------------

def run_gui(config: dict) -> int:
    """打开图形控制台（阻塞直到窗口关闭）。"""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("当前环境无 tkinter，无法打开图形控制台。", file=sys.stderr)
        return 2

    config_path = config.get("_config_file", "config.yaml")
    version = config.get("_version", "")

    root = tk.Tk()
    root.title(f"WinRemote 控制台 {version}".strip())
    root.geometry("560x260")
    root.resizable(False, False)

    # --- 顶部状态 ---
    state_var = tk.StringVar(value="检测中…")
    pid_var = tk.StringVar(value="")
    tk.Label(root, text="服务状态", font=("Microsoft YaHei UI", 10), fg="#888").pack(pady=(16, 0))
    tk.Label(root, textvariable=state_var, font=("Microsoft YaHei UI", 22, "bold")).pack()
    tk.Label(root, textvariable=pid_var, font=("Consolas", 9), fg="#666").pack(pady=(2, 8))

    # --- 按钮 ---
    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=8)

    def refresh_status() -> None:
        try:
            st = daemon_status(config_path)
        except Exception as e:
            LOG.warning("刷新状态失败: %s", e)
            state_var.set("状态未知")
            pid_var.set("")
            return
        if st["running"]:
            state_var.set("运行中")
            started = st.get("started")
            if started:
                up = max(0, int(time.time() - started))
                pid_var.set(f"PID {st['pid']}  ·  已运行 {up // 3600}h{(up % 3600) // 60}m")
            else:
                pid_var.set(f"PID {st['pid']}")
            start_btn.config(state=tk.DISABLED)
            restart_btn.config(state=tk.NORMAL)
            stop_btn.config(state=tk.NORMAL)
        else:
            state_var.set("已停止")
            pid_var.set("")
            start_btn.config(state=tk.NORMAL)
            restart_btn.config(state=tk.DISABLED)
            stop_btn.config(state=tk.DISABLED)
        root.after(2000, refresh_status)

    def on_start() -> None:
        start_btn.config(state=tk.DISABLED)
        restart_btn.config(state=tk.DISABLED)
        stop_btn.config(state=tk.DISABLED)
        state_var.set("正在开启…")
        pid_var.set("")
        ok = start_daemon(config_path)
        if not ok:
            state_var.set("开启失败")
        refresh_status()

    def on_restart() -> None:
        start_btn.config(state=tk.DISABLED)
        restart_btn.config(state=tk.DISABLED)
        stop_btn.config(state=tk.DISABLED)
        state_var.set("正在重启…")
        pid_var.set("")
        ok = restart_daemon(config_path)
        if not ok:
            state_var.set("重启失败")
        refresh_status()

    def on_stop() -> None:
        start_btn.config(state=tk.DISABLED)
        restart_btn.config(state=tk.DISABLED)
        stop_btn.config(state=tk.DISABLED)
        state_var.set("正在关闭…")
        pid_var.set("")
        stop_daemon(config_path)
        refresh_status()

    start_btn = tk.Button(
        btn_frame, text="开启进程", command=on_start,
        width=11, height=2, font=("Microsoft YaHei UI", 12),
        bg="#2d6c3a", fg="white", activebackground="#3d8c4a",
        relief="flat", cursor="hand2",
    )
    restart_btn = tk.Button(
        btn_frame, text="重启进程", command=on_restart,
        width=11, height=2, font=("Microsoft YaHei UI", 12),
        bg="#8a6d2f", fg="white", activebackground="#aa8d4f",
        relief="flat", cursor="hand2", state=tk.DISABLED,
    )
    stop_btn = tk.Button(
        btn_frame, text="关闭进程", command=on_stop,
        width=11, height=2, font=("Microsoft YaHei UI", 12),
        bg="#b03a2e", fg="white", activebackground="#d04a3e",
        relief="flat", cursor="hand2", state=tk.DISABLED,
    )
    start_btn.pack(side=tk.LEFT, padx=8)
    restart_btn.pack(side=tk.LEFT, padx=8)
    stop_btn.pack(side=tk.LEFT, padx=8)

    # --- 底部配置信息 ---
    tk.Label(
        root,
        text=f"配置：{config_path}",
        font=("Consolas", 8), fg="#999",
    ).pack(side=tk.BOTTOM, pady=8)
    tk.Label(
        root,
        text="关闭本窗口不影响服务运行",
        font=("Microsoft YaHei UI", 8), fg="#aaa",
    ).pack(side=tk.BOTTOM)

    refresh_status()
    root.mainloop()
    return 0
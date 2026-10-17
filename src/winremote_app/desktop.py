"""WinRemote App —— 桌面控制台（图形界面）。

**两种呈现方式，逻辑层完全共用：**

1. **本地 HTTP + 浏览器应用窗口（默认）**
   - 用标准库 `http.server` 在 127.0.0.1 上起一个仅本机可访问的服务，
     提供界面文件与 `/api/<method>` 接口；
   - 再用系统默认浏览器（Chromium 内核）以 `--app=URL` 打开**无地址栏的独立窗口**，
     观感与原生桌面应用一致，且**零第三方依赖**。

2. **pywebview 原生窗口（可选，`--webview`）**
   - 需要 pythonnet + .NET Framework 正常；部分精简版系统上 CLR 不可用，
     此时请使用默认方式。

界面层 `webui/index.html` 对两种方式都兼容（自动选择 pywebview 桥或 HTTP 接口）；
进程启停逻辑复用 `gui_console` 的 `find/start/stop/restart_daemon`。

用法：
    winremote-app.exe             # 打开桌面控制台（默认方式）
    winremote-app.exe --webview   # 用 pywebview 原生窗口（若环境支持）
    winremote-app.exe --gui       # 旧版 tkinter 简易控制台
"""
from __future__ import annotations

import http.server
import json
import logging
import ntpath
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from . import __version__
from . import gui_console as gc

LOG = logging.getLogger("winremote-app.desktop")

APP_TITLE = "WinRemote 控制台"

# 设置页允许写回的配置键（其余键保持原样，避免误改内部字段）
EDITABLE_KEYS = (
    "server_url",
    "auth_token",
    "device_id",
    "heartbeat_interval",
    "max_reconnect_delay",
    "default_timeout",
    "build_exe_index",
    "emergency_stop",
    "allowed_actions",
)

# 指令白名单全集（界面上的可勾选项）
ALL_ACTIONS = (
    "exec", "sys", "ping", "file", "file_pull", "app",
    "screenshot", "system", "input", "keys", "mouse", "clipboard",
)

# 日志里判断连接状态的关键字（与 ws_client.py 的日志文案对应）
_CONNECT_PAT = re.compile(r"已连接 B 端服务端")
_DISCONNECT_PAT = re.compile(r"重连|连接异常|连接失败|已断开|主动断开")

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "WinRemoteApp"

# 无心跳多久后自动退出（浏览器窗口被关掉的情况；Chromium 后台标签会节流定时器，故放宽）
IDLE_EXIT_SECONDS = 300

# Chromium 内核浏览器可执行文件名（支持 --app 应用窗口）
_CHROMIUM_EXES = (
    "chrome.exe", "msedge.exe", "chromium.exe", "brave.exe",
    "vivaldi.exe", "opera.exe", "centbrowser.exe", "supermium.exe",
)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def app_dir() -> str:
    """应用目录：打包后为 exe 所在目录，源码运行为当前目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(".")


def webui_dir() -> str:
    """前端资源目录（打包后位于 PyInstaller 解包目录下的 webui/）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, "webui")
        if os.path.isdir(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")


# ---------------------------------------------------------------------------
# 纯逻辑（可单测）
# ---------------------------------------------------------------------------

def read_log_tail(path: str, lines: int = 200) -> list[str]:
    """读取日志文件尾部若干行；文件不存在或读取失败时返回空列表。"""
    if not path:
        return []
    try:
        p = Path(path)
        if not p.is_file():
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        n = max(1, int(lines))
        return [ln.rstrip("\r\n") for ln in data[-n:]]
    except Exception as e:  # noqa: BLE001
        LOG.warning("读取日志失败: %s", e)
        return []


def infer_connection(lines: list[str]) -> bool | None:
    """从日志尾部推断当前是否连着 B 端。

    返回 True=最近一次是「已连接」，False=最近一次是断开/重连中，
    None=日志里没有相关记录（例如刚启动还没握手）。
    """
    state: bool | None = None
    for line in lines:
        if _CONNECT_PAT.search(line):
            state = True
        elif _DISCONNECT_PAT.search(line):
            state = False
    return state


def load_config_file(path: str) -> dict:
    """读取 YAML 配置；不存在或格式异常时返回空 dict。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        LOG.warning("读取配置失败: %s", e)
        return {}


def save_config_patch(path: str, patch: dict) -> dict:
    """把 patch 合并进配置文件并原子写回，返回写入后的完整配置。

    只接受 EDITABLE_KEYS 里的键；内部键（下划线开头）一律不落盘。
    """
    cur = load_config_file(path)
    for key in EDITABLE_KEYS:
        if key in patch and patch[key] is not None:
            cur[key] = patch[key]
    data = {k: v for k, v in cur.items() if not str(k).startswith("_")}
    p = Path(path)
    if str(p.parent) not in (".", ""):
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, p)
    return data


def format_uptime(seconds: float) -> str:
    """把秒数格式化成「3 小时 12 分」这类可读文本。"""
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分"
    return "不到 1 分"


def extract_exe_from_command(cmd: str) -> str | None:
    """从注册表的浏览器启动命令行里取出可执行文件路径。"""
    if not cmd:
        return None
    cmd = cmd.strip()
    m = re.match(r'^"([^"]+)"', cmd)
    if m:
        return m.group(1)
    part = cmd.split(" ", 1)[0]
    return part or None


def is_chromium_browser(exe: str | None) -> bool:
    """判断是否为 Chromium 内核浏览器（只有它们支持 --app 应用窗口）。"""
    if not exe:
        return False
    # 用 ntpath：这些是 Windows 路径，即便在类 Unix 上跑测试也要按 "\\" 取文件名
    name = ntpath.basename(exe).lower()
    return name in _CHROMIUM_EXES or "chrom" in name


# ---------------------------------------------------------------------------
# 开机自启（Windows 注册表 Run 项）
# ---------------------------------------------------------------------------

def autostart_get() -> bool | None:
    """查询是否已设置开机自启；非 Windows 返回 None（不支持）。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, _AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:  # noqa: BLE001
        LOG.warning("读取开机自启状态失败: %s", e)
        return None


def autostart_cmd(config_path: str) -> str:
    """开机自启项要写入的命令行。"""
    return f'"{gc.self_exe()}" --daemon -c "{config_path}"'


def autostart_set(enabled: bool, config_path: str) -> bool:
    """设置/取消开机自启；失败返回 False。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            if enabled:
                winreg.SetValueEx(
                    k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                    autostart_cmd(config_path),
                )
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:  # noqa: BLE001
        LOG.error("设置开机自启失败: %s", e)
        return False


# ---------------------------------------------------------------------------
# JS 桥 / HTTP 接口共用的后端 API
# ---------------------------------------------------------------------------

class DesktopApi:
    """桌面控制台的后端 API。

    所有公开方法都返回 dict（{"ok": bool, ...}），前端统一处理；
    既能作为 pywebview 的 `js_api`，也能被 HTTP 层按方法名路由调用。
    """

    def __init__(self, config: dict | None = None, config_path: str | None = None):
        config = config or {}
        self.config_path: str = os.path.abspath(
            config_path or config.get("_config_file") or os.path.join(app_dir(), "config.yaml")
        )
        self.log_file: str = str(
            config.get("_log_file") or os.path.join(app_dir(), "logs", "winremote-app.log")
        )
        self._window = None
        self.quit_callback = None

    def attach(self, window) -> None:
        """绑定 pywebview 窗口对象（用于 quit 时关闭窗口）。"""
        self._window = window

    # ---- 查询 ----

    def get_state(self) -> dict:
        """运行状态 + 连接状态 + 关键配置摘要（前端用它做轮询刷新）。"""
        try:
            st = gc.daemon_status(self.config_path)
        except Exception as e:  # noqa: BLE001
            LOG.warning("查询 daemon 状态失败: %s", e)
            st = {"running": False, "pid": None, "started": None}

        cfg = load_config_file(self.config_path)
        tail = read_log_tail(self.log_file, 400)
        started = st.get("started")
        # 只认行首附近的 [ERROR]（日志格式：时间 [级别] 模块: 内容），
        # 否则把「内容里恰好含 ERROR 字样」的超长记录也当成错误摘要，会把界面撑爆。
        last_err = next((ln for ln in reversed(tail) if "[ERROR]" in ln[:40]), "")
        if len(last_err) > 180:
            last_err = last_err[:180] + "…"
        return {
            "ok": True,
            "version": __version__,
            "running": bool(st.get("running")),
            "pid": st.get("pid"),
            "uptime": round(time.time() - started, 1) if (st.get("running") and started is not None) else 0,
            "connected": infer_connection(tail),
            "config_file": self.config_path,
            "log_file": self.log_file,
            "log_exists": os.path.isfile(self.log_file),
            "autostart": autostart_get(),
            "server_url": str(cfg.get("server_url", "")),
            "device_id": str(cfg.get("device_id", "")),
            "heartbeat_interval": cfg.get("heartbeat_interval", 15),
            "emergency_stop": bool(cfg.get("emergency_stop", False)),
            "allowed_actions": list(cfg.get("allowed_actions") or []),
            "recent_lines": [ln[:160] for ln in tail[-5:]],
            "platform": sys.platform,
            "frozen": bool(getattr(sys, "frozen", False)),
            "last_error": last_err,
        }

    def get_config(self) -> dict:
        """返回设置页需要的可编辑配置。"""
        cfg = load_config_file(self.config_path)
        out = {k: cfg.get(k) for k in EDITABLE_KEYS}
        out["ok"] = True
        out["has_file"] = os.path.isfile(self.config_path)
        return out

    def read_log(self, lines: int = 200) -> dict:
        """读取日志尾部（前端日志页轮询调用）。"""
        n = int(lines) if str(lines).isdigit() else 200
        tail = read_log_tail(self.log_file, max(1, min(n, 2000)))
        return {"ok": True, "lines": tail, "path": self.log_file}

    # ---- 进程控制 ----

    def start(self) -> dict:
        try:
            if gc.daemon_status(self.config_path).get("running"):
                return {"ok": True, "message": "进程已在运行"}
            ok = gc.start_daemon(self.config_path)
            return {"ok": ok, "message": "已启动" if ok else "启动失败，请查看日志"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"启动异常: {e}"}

    def stop(self) -> dict:
        try:
            ok = gc.stop_daemon(self.config_path)
            return {"ok": ok, "message": "已关闭" if ok else "关闭失败，请查看日志"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"关闭异常: {e}"}

    def restart(self) -> dict:
        try:
            ok = gc.restart_daemon(self.config_path)
            return {"ok": ok, "message": "已重启" if ok else "重启失败，请查看日志"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"重启异常: {e}"}

    # ---- 设置 ----

    def save_settings(self, patch: dict) -> dict:
        """保存设置（写回 config.yaml），需重启进程生效。"""
        if not isinstance(patch, dict):
            return {"ok": False, "message": "参数格式错误"}
        try:
            saved = save_config_patch(self.config_path, patch)
        except Exception as e:  # noqa: BLE001
            LOG.error("保存配置失败: %s", e)
            return {"ok": False, "message": f"保存失败: {e}"}
        return {
            "ok": True,
            "message": "已保存，重启进程后生效",
            "need_restart": True,
            "config": {k: saved.get(k) for k in EDITABLE_KEYS},
        }

    def set_autostart(self, enabled: bool) -> dict:
        if os.name != "nt":
            return {"ok": False, "message": "当前系统不支持开机自启设置"}
        ok = autostart_set(bool(enabled), self.config_path)
        return {
            "ok": ok,
            "message": ("已开启开机自启" if enabled else "已关闭开机自启") if ok else "设置失败",
            "autostart": autostart_get(),
        }

    def choose_config_file(self) -> dict:
        """弹出文件选择框，切换配置文件（仅 pywebview 模式可用）。"""
        if self._window is None:
            return {"ok": False, "message": "当前界面方式不支持选择文件，请直接编辑 config.yaml"}
        try:
            picked = self._window.create_file_dialog(
                dialog_type=10,  # OPEN_DIALOG
                allow_multiple=False,
                file_types=("配置文件 (*.yaml;*.yml)", "所有文件 (*.*)"),
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"打开文件选择失败: {e}"}
        if not picked:
            return {"ok": False, "message": "已取消"}
        self.config_path = os.path.abspath(picked[0])
        return {"ok": True, "message": "已切换配置文件，请重启进程", "config_file": self.config_path}

    # ---- 其他 ----

    def open_folder(self, which: str = "config") -> dict:
        """用资源管理器打开配置文件所在目录 / 日志目录。"""
        try:
            if which == "log":
                target = os.path.dirname(self.log_file) or app_dir()
            else:
                target = os.path.dirname(self.config_path) or app_dir()
            if not os.path.isdir(target):
                return {"ok": False, "message": f"目录不存在: {target}"}
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                return {"ok": False, "message": "当前系统不支持"}
            return {"ok": True, "message": "已打开"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"打开失败: {e}"}

    def quit_app(self) -> dict:
        """关闭控制台界面（不停止后台执行器进程）。"""
        try:
            if self._window is not None:
                self._window.destroy()
            elif self.quit_callback is not None:
                threading.Thread(target=self.quit_callback, daemon=True).start()
            return {"ok": True, "message": "已退出"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"退出失败: {e}"}


# ---------------------------------------------------------------------------
# 本地 HTTP 服务（默认界面方式）
# ---------------------------------------------------------------------------

class DesktopServer:
    """把 DesktopApi 暴露成本机 HTTP 接口，供浏览器界面调用。

    - 只监听 127.0.0.1，端口由系统分配（避免冲突）；
    - `GET /` 返回界面文件；`GET /api/ping` 作为前端心跳；
      `POST /api/<method>` 调用 DesktopApi 的同名方法（参数为 JSON 数组）。
    """

    def __init__(self, api: DesktopApi, webui: str | None = None):
        self.api = api
        self.webui = webui or webui_dir()
        self.state = {"last_ping": time.time(), "closed": False}
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    # ---- 内部 ----

    def _handler_class(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "WinRemoteUI/1.0"

            def log_message(self, *args):  # 静音访问日志
                pass

            # --- 工具 ---
            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, path: str, ctype: str) -> None:
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except OSError:
                    self.send_error(404, "not found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            # --- 路由 ---
            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    self._send_file(
                        os.path.join(server.webui, "index.html"),
                        "text/html; charset=utf-8",
                    )
                elif path == "/api/ping":
                    server.state["last_ping"] = time.time()
                    self._send_json({"ok": True, "ts": time.time()})
                elif path.startswith("/static/"):
                    rel = path[len("/static/"):]
                    safe = os.path.normpath(rel).replace("\\", "/")
                    if safe.startswith(".."):
                        self.send_error(403, "forbidden")
                        return
                    full = os.path.join(server.webui, *safe.split("/"))
                    ctype = "application/octet-stream"
                    if safe.endswith(".css"):
                        ctype = "text/css; charset=utf-8"
                    elif safe.endswith(".js"):
                        ctype = "application/javascript; charset=utf-8"
                    elif safe.endswith(".svg"):
                        ctype = "image/svg+xml"
                    self._send_file(full, ctype)
                else:
                    self.send_error(404, "not found")

            def do_POST(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if not path.startswith("/api/"):
                    self.send_error(404, "not found")
                    return
                method = path[len("/api/"):]
                server.state["last_ping"] = time.time()
                if not method or method.startswith("_") or not hasattr(server.api, method):
                    self._send_json({"ok": False, "message": f"未知接口: {method}"}, 404)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"[]"
                    args = json.loads(raw.decode("utf-8")) if raw else []
                    if not isinstance(args, list):
                        args = [args]
                except Exception as e:  # noqa: BLE001
                    self._send_json({"ok": False, "message": f"请求格式错误: {e}"}, 400)
                    return
                fn = getattr(server.api, method)
                if not callable(fn):
                    self._send_json({"ok": False, "message": f"接口不可调用: {method}"}, 400)
                    return
                try:
                    result = fn(*args)
                except Exception as e:  # noqa: BLE001
                    LOG.exception("接口 %s 执行失败", method)
                    self._send_json({"ok": False, "message": f"{type(e).__name__}: {e}"}, 500)
                    return
                if not isinstance(result, dict):
                    result = {"ok": True, "result": result}
                self._send_json(result, 200 if result.get("ok", True) else 400)

        return Handler

    # ---- 生命周期 ----

    def start(self) -> str:
        """启动服务并返回访问地址。"""
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                                        name="cherry-webui")
        self._thread.start()
        url = f"http://127.0.0.1:{self.port}/"
        LOG.info("界面服务已启动: %s", url)
        return url

    def stop(self) -> None:
        self.state["closed"] = True
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None

    def wait_until_idle(self, timeout: float = IDLE_EXIT_SECONDS) -> None:
        """阻塞直到界面长时间没有心跳（窗口被关掉）或收到退出请求。"""
        while not self.state["closed"]:
            time.sleep(1.0)
            if timeout and time.time() - self.state["last_ping"] > timeout:
                LOG.info("界面已 %d 秒无心跳，自动退出。", int(timeout))
                break
        self.stop()


# ---------------------------------------------------------------------------
# 打开浏览器应用窗口
# ---------------------------------------------------------------------------

def detect_default_browser() -> str | None:
    """读取系统默认浏览器的可执行文件路径（仅 Windows）。"""
    if os.name != "nt":
        return os.environ.get("BROWSER")
    try:
        import winreg
        choice = r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, choice) as k:
            progid, _ = winreg.QueryValueEx(k, "ProgId")
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, rf"SOFTWARE\Classes\{progid}\shell\open\command") as k:
                    cmd, _ = winreg.QueryValueEx(k, "")
                exe = extract_exe_from_command(cmd)
                if exe and os.path.isfile(exe):
                    return exe
            except OSError:
                continue
    except Exception as e:  # noqa: BLE001
        LOG.warning("读取默认浏览器失败: %s", e)
    return None


def open_app_window(url: str, title: str = APP_TITLE,
                    profile_dir: str | None = None) -> bool:
    """用默认浏览器打开界面；Chromium 内核则以 `--app` 方式开独立窗口。

    返回是否成功拉起（失败时调用方通常会回退到 tkinter 控制台）。
    """
    exe = detect_default_browser()
    if is_chromium_browser(exe):
        profile = profile_dir or os.path.join(app_dir(), ".webui_profile")
        args = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1020,700",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
        ]
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, creationflags=creationflags,
            )
            LOG.info("已用 %s 打开应用窗口", ntpath.basename(exe))
            return True
        except Exception as e:  # noqa: BLE001
            LOG.warning("打开浏览器应用窗口失败: %s", e)

    # 兜底：交给系统默认方式打开（普通标签页），非 Windows 也走这里
    try:
        import webbrowser
        if webbrowser.open(url):
            LOG.info("已用系统默认方式打开界面: %s", url)
            return True
    except Exception as e:  # noqa: BLE001
        LOG.warning("打开浏览器失败: %s", e)
    return False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def close_console_windows(title: str = APP_TITLE) -> int:
    """向所有标题匹配的控制台窗口发 WM_CLOSE（托盘退出时收尾，避免窗口残留）。"""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        handles: list = []

        def _cb(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    handles.append(hwnd)
            return True

        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc_type(_cb), 0)
        for hwnd in handles:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return len(handles)
    except Exception as e:  # noqa: BLE001
        LOG.debug("关闭控制台窗口失败: %s", e)
        return 0


def run_web_desktop(config: dict) -> int:
    """默认方式：本地 HTTP 服务 + 浏览器应用窗口 + 系统托盘常驻。

    有托盘时：关掉界面窗口程序仍驻留托盘，可从托盘再次唤回；
    无托盘（pystray 不可用）时：退化为「界面长时间无心跳即退出」。
    """
    index = os.path.join(webui_dir(), "index.html")
    if not os.path.isfile(index):
        print(f"未找到界面文件: {index}", file=sys.stderr)
        from . import gui_console
        return gui_console.run_gui(config)

    api = DesktopApi(config, config.get("_config_file"))
    server = DesktopServer(api, webui_dir())
    url = server.start()

    quit_event = threading.Event()

    def _request_quit() -> None:
        quit_event.set()

    api.quit_callback = _request_quit

    def _open_console() -> None:
        """托盘里点「打开控制台」时，重新拉起界面窗口（服务仍在跑，直接复用）。"""
        open_app_window(url, APP_TITLE)

    tray = None
    try:
        from .tray import TrayApp
        tray = TrayApp(api, on_open_console=_open_console, on_quit=_request_quit)
        if not tray.start():
            tray = None
    except Exception as e:  # noqa: BLE001
        LOG.warning("系统托盘不可用，降级为无托盘模式: %s", e)
        tray = None

    if not open_app_window(url, APP_TITLE):
        print(f"未能自动打开浏览器，请手动访问：{url}", file=sys.stderr)

    try:
        if tray is not None:
            while not quit_event.is_set():
                time.sleep(0.4)
        else:
            server.wait_until_idle()
    except KeyboardInterrupt:
        pass
    finally:
        if tray is not None:
            tray.stop()
        server.stop()
        close_console_windows()
    return 0


def run_webview_desktop(config: dict) -> int:
    """可选方式：pywebview 原生窗口（需要可用的 .NET Framework / pythonnet）。"""
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        print(f"pywebview 不可用（{e}），改用默认界面方式。", file=sys.stderr)
        return run_web_desktop(config)

    index = os.path.join(webui_dir(), "index.html")
    if not os.path.isfile(index):
        print(f"未找到界面文件: {index}", file=sys.stderr)
        return run_web_desktop(config)

    api = DesktopApi(config, config.get("_config_file"))
    window = webview.create_window(
        f"{APP_TITLE} {__version__}",
        url=index,
        js_api=api,
        width=1020,
        height=700,
        min_size=(840, 580),
        background_color="#0e1012",
        text_select=False,
    )
    api.attach(window)
    LOG.info("桌面控制台已打开（pywebview）：%s", index)
    try:
        webview.start()
    except Exception as e:  # noqa: BLE001
        print(f"pywebview 启动失败（{e}），改用默认界面方式。", file=sys.stderr)
        return run_web_desktop(config)
    return 0


def run_desktop(config: dict) -> int:
    """打开桌面控制台（阻塞直到界面关闭）。

    默认走「本地 HTTP + 浏览器应用窗口」；`config["_ui_backend"] == "webview"`
    时改用 pywebview 原生窗口。
    """
    if str(config.get("_ui_backend") or "").lower() == "webview":
        return run_webview_desktop(config)
    return run_web_desktop(config)

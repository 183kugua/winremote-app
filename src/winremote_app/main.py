"""winremote-app 入口。

用法：
    winremote-app.exe               # 默认：桌面控制台（本地界面 + 浏览器应用窗口）
    winremote-app.exe --webview     # 桌面控制台（pywebview 原生窗口，需 .NET 支持）
    winremote-app.exe --gui         # 旧版 tkinter 简易控制台
    winremote-app.exe --daemon      # 后台执行器：连接 B 端常驻（服务/开机自启部署用）
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

from . import __version__
from .ws_client import WsClient

LOG = logging.getLogger("winremote-app")


def _app_dir() -> str:
    """返回应用目录：PyInstaller 打包后为 exe 所在目录，源码运行为当前目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(".")


def _resolve_rel(path: str) -> str:
    """相对路径基于应用目录解析，绝对路径原样返回。"""
    if os.path.isabs(path):
        return path
    return os.path.join(_app_dir(), path)


def load_config(path: str) -> dict:
    """读取 YAML 配置并合并默认值。"""
    defaults = default_config()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("配置文件顶层必须是键值对（yaml 映射）")
    defaults.update({k: v for k, v in cfg.items() if v is not None})
    return defaults


def default_config() -> dict:
    """内置默认配置（无配置文件时使用）。"""
    return {
        "server_url": "ws://127.0.0.1:8765/ws",
        "auth_token": "change-me",
        "device_id": "home-pc",
        "heartbeat_interval": 15,
        "max_reconnect_delay": 60,
        "default_timeout": 30,
        "allowed_actions": ["exec", "sys", "ping", "file", "file_pull", "app", "screenshot", "system", "input", "keys", "mouse", "clipboard"],
        "max_pull_size": 200 * 1024 * 1024,
    }


def save_config(path: str, cfg: dict) -> None:
    """把配置写入 YAML 文件（过滤内部下划线键）。"""
    data = {k: v for k, v in cfg.items() if not k.startswith("_")}
    p = Path(path)
    if str(p.parent) not in (".", ""):
        p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_path = Path(log_file)
        if log_path.parent != Path("."):
            log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def prepare_config(args) -> dict:
    """读取配置；不存在时用内置默认值自动生成一份（免手动改名）。"""
    if Path(args.config).is_file():
        cfg = load_config(args.config)
    else:
        cfg = default_config()
        try:
            save_config(args.config, cfg)
            LOG.info("已自动生成配置文件: %s（请编辑 server_url / auth_token / device_id）", args.config)
        except Exception as e:
            LOG.warning("生成默认配置文件失败（将仅用内置默认值）: %s", e)
    cfg["_config_file"] = os.path.abspath(args.config)
    cfg["exe_index_file"] = _resolve_rel(cfg.get("exe_index_file", "exe_index.json"))
    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="winremote-app",
        description="WinRemote 远程操控执行器（纯执行器，无 AI）",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=os.path.join(_app_dir(), "config.yaml"),
        help="配置文件路径（默认应用目录下 config.yaml）",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别（DEBUG/INFO/WARNING/ERROR）")
    parser.add_argument(
        "--log-file",
        default=os.path.join(_app_dir(), "logs", "winremote-app.log"),
        help="日志文件路径（默认应用目录下 logs/winremote-app.log；设空则仅输出控制台）",
    )
    parser.add_argument(
        "--webview",
        action="store_true",
        help="桌面控制台使用 pywebview 原生窗口（默认用浏览器应用窗口，更兼容）",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="旧版 tkinter 简易控制台（默认打开的是桌面控制台）",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="后台执行器：直接连接 B 端 WebSocket 常驻运行（服务/开机自启用）",
    )
    args = parser.parse_args()

    setup_logging(args.log_level, log_file=args.log_file or None)

    cfg = prepare_config(args)

    if args.daemon:
        # daemon 模式：连 B 端常驻运行（服务部署 / 开机自启用）
        client = WsClient(cfg)
        try:
            asyncio.run(client.run())
        except KeyboardInterrupt:
            LOG.info("收到中断信号，退出。")
            sys.exit(0)
        return

    # 图形界面：默认桌面控制台（本地 HTTP + 浏览器应用窗口；--webview 用 pywebview），
    # --gui 用旧版 tkinter 控制台。两种模式下 daemon 都由界面上的按钮拉起。
    cfg["_version"] = __version__
    cfg["_log_file"] = os.path.abspath(args.log_file) if args.log_file else ""
    cfg["_ui_backend"] = "webview" if args.webview else "web"

    if args.gui:
        from . import gui_console
        sys.exit(gui_console.run_gui(cfg))

    from . import desktop
    sys.exit(desktop.run_desktop(cfg))


if __name__ == "__main__":
    main()

"""WinRemote App —— 系统托盘（常驻任务栏）。

作用：程序驻留系统托盘，随时可以
  - 查看执行器状态（运行中 / 已停止 / PID）
  - 开启 / 关闭 / 重启后台执行器
  - 重新打开控制台界面（关掉窗口后也能唤回）
  - 打开配置目录 / 日志目录
  - 退出（可选是否同时停止执行器）

依赖 `pystray`（纯 Python 托盘库）+ `Pillow`（现画图标，不额外带图片资源）；
两者任一不可用时 `start()` 返回 False，调用方会降级为「无托盘」模式。
"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("winremote-app.tray")

ICON_SIZE = 64
APP_NAME = "WinRemote"


def make_icon_image():
    """用 Pillow 现画一个托盘图标：深色圆角底 + 青色鲸鱼剪影。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角底
    d.rounded_rectangle([2, 2, ICON_SIZE - 3, ICON_SIZE - 3], radius=15,
                        fill=(17, 21, 24, 255))
    # 水柱
    d.line([(30, 26), (30, 14)], fill=(110, 190, 212, 255), width=3)
    d.ellipse([(25, 11), (35, 17)], fill=(110, 190, 212, 255))
    # 鲸鱼身体
    d.ellipse([(13, 25), (49, 47)], fill=(59, 167, 196, 255))
    # 尾鳍
    d.polygon([(45, 28), (59, 19), (55, 36)], fill=(59, 167, 196, 255))
    # 眼睛
    d.ellipse([(21, 32), (27, 38)], fill=(11, 13, 15, 255))
    # 腹部高光
    d.arc([(17, 33), (45, 49)], start=10, end=170, fill=(150, 220, 235, 200), width=2)
    return img


class TrayApp:
    """托盘应用：菜单动作全部转发给 DesktopApi / 回调。"""

    def __init__(self, api, on_open_console=None, on_quit=None,
                 refresh_seconds: float = 2.0):
        self.api = api
        self.on_open_console = on_open_console
        self.on_quit = on_quit
        self.refresh_seconds = refresh_seconds
        self._icon = None
        self._thread = None
        self._poll = None
        self._stopped = False
        self._state = {"running": False, "pid": None, "connected": None}

    # ---- 状态 ----

    def _refresh(self) -> None:
        while not self._stopped:
            try:
                st = self.api.get_state()
                self._state = {
                    "running": bool(st.get("running")),
                    "pid": st.get("pid"),
                    "connected": st.get("connected"),
                }
                if self._icon is not None:
                    self._icon.title = self.status_line()
                    self._icon.update_menu()
            except Exception as e:  # noqa: BLE001
                LOG.debug("托盘状态刷新失败: %s", e)
            time.sleep(self.refresh_seconds)

    def status_line(self) -> str:
        st = self._state
        if st["running"]:
            conn = ""
            if st.get("connected") is True:
                conn = " · B 端已连接"
            elif st.get("connected") is False:
                conn = " · 重连中"
            return f"运行中（PID {st['pid']}）{conn}"
        return "已停止"

    def is_running(self, item=None) -> bool:
        return bool(self._state["running"])

    def is_stopped(self, item=None) -> bool:
        return not self._state["running"]

    # ---- 菜单动作 ----

    def _open_console(self, icon=None, item=None) -> None:
        if self.on_open_console is not None:
            try:
                self.on_open_console()
            except Exception as e:  # noqa: BLE001
                LOG.warning("打开控制台失败: %s", e)

    def _start(self, icon=None, item=None) -> None:
        r = self.api.start()
        LOG.info("托盘：开启执行器 -> %s", r.get("message"))

    def _stop(self, icon=None, item=None) -> None:
        r = self.api.stop()
        LOG.info("托盘：关闭执行器 -> %s", r.get("message"))

    def _restart(self, icon=None, item=None) -> None:
        r = self.api.restart()
        LOG.info("托盘：重启执行器 -> %s", r.get("message"))

    def _open_config(self, icon=None, item=None) -> None:
        self.api.open_folder("config")

    def _open_log(self, icon=None, item=None) -> None:
        self.api.open_folder("log")

    def _quit(self, icon=None, item=None) -> None:
        LOG.info("托盘：退出")
        self.stop()
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception:  # noqa: BLE001
                pass

    # ---- 生命周期 ----

    def _build_menu(self):
        from pystray import Menu, MenuItem

        return Menu(
            MenuItem("打开控制台", self._open_console, default=True),
            Menu.SEPARATOR,
            MenuItem(lambda item: "状态：" + self.status_line(), None, enabled=False),
            MenuItem("开启执行器", self._start, enabled=self.is_stopped),
            MenuItem("关闭执行器", self._stop, enabled=self.is_running),
            MenuItem("重启执行器", self._restart, enabled=self.is_running),
            Menu.SEPARATOR,
            MenuItem("打开配置目录", self._open_config),
            MenuItem("打开日志目录", self._open_log),
            Menu.SEPARATOR,
            MenuItem("退出", self._quit),
        )

    def start(self) -> bool:
        """启动托盘（后台线程）；不可用时返回 False。"""
        try:
            import pystray
        except Exception as e:  # noqa: BLE001
            LOG.warning("pystray 不可用，跳过托盘：%s", e)
            return False
        try:
            image = make_icon_image()
        except Exception as e:  # noqa: BLE001
            LOG.warning("生成托盘图标失败: %s", e)
            return False

        try:
            self._icon = pystray.Icon(
                "cherry-remote", image, f"{APP_NAME} · 检测中", self._build_menu()
            )
            self._poll = threading.Thread(target=self._refresh, daemon=True,
                                          name="cherry-tray-poll")
            self._poll.start()
            self._thread = threading.Thread(target=self._icon.run, daemon=True,
                                            name="cherry-tray")
            self._thread.start()
            LOG.info("系统托盘已启动")
            return True
        except Exception as e:  # noqa: BLE001
            LOG.warning("托盘启动失败: %s", e)
            self._icon = None
            return False

    def stop(self) -> None:
        self._stopped = True
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._icon = None

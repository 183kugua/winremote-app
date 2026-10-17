"""Windows GUI 自动化：键盘输入与鼠标控制（纯 ctypes，无第三方依赖）。

实现原理：
- 键盘：SendInput + KEYEVENTF_UNICODE 直输任意字符（含中文），
  按键（Enter/Tab/快捷键组合）用虚拟键码映射 + 修饰键按下/抬起。
- 鼠标：SetCursorPos 移动 + mouse_event 点击/滚动/拖拽。
- 坐标约定：与 PIL ImageGrab.grab(all_screens=True) 的**截图像素坐标**一致。
  屏幕有缩放（125%/150%）或多显示器时，物理像素与逻辑坐标不同，
  因此模块启动时调用 SetProcessDpiAwareness 声明 DPI 感知（1:1 像素），
  并返回虚拟屏幕偏移 offset，使「截图里看到的坐标」==「传给本模块的坐标」。
"""

import ctypes
import logging
import os
import time
from ctypes import wintypes

LOG = logging.getLogger("winremote-app.gui")

# ---- Windows 常量 ----
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77

# 虚拟键码表
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_PAUSE = 0x13
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22  # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91

# 需要 shift 的符号（按键名模式下）
_SHIFT_SYMBOLS = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}

# 常用符号 → 虚拟键码
_SYMBOL_VK = {
    " ": VK_SPACE, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE,
    "/": 0xBF, "`": 0xC0,
}

# 按键名 → 虚拟键码
_NAMED_KEYS = {
    "enter": VK_RETURN, "return": VK_RETURN, "tab": VK_TAB,
    "esc": VK_ESCAPE, "escape": VK_ESCAPE,
    "space": VK_SPACE, "backspace": VK_BACK, "bs": VK_BACK,
    "delete": VK_DELETE, "del": VK_DELETE, "insert": VK_INSERT, "ins": VK_INSERT,
    "home": VK_HOME, "end": VK_END,
    "pageup": VK_PRIOR, "pgup": VK_PRIOR, "pagedown": VK_NEXT, "pgdn": VK_NEXT,
    "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT,
    "printscreen": 0x2C, "prtsc": 0x2C, "scrolllock": VK_SCROLL,
    "pause": VK_PAUSE, "capslock": VK_CAPITAL, "numlock": VK_NUMLOCK,
    "ctrl": VK_CONTROL, "control": VK_CONTROL,
    "shift": VK_SHIFT, "alt": VK_MENU, "menu": VK_MENU,
    "win": VK_LWIN, "windows": VK_LWIN, "meta": VK_LWIN,
}

_NUM_KEYPAD = {"0": 0x60, "1": 0x61, "2": 0x62, "3": 0x63, "4": 0x64,
               "5": 0x65, "6": 0x66, "7": 0x67, "8": 0x68, "9": 0x69,
               "*": 0x6A, "+": 0x6B, "-": 0x6D, ".": 0x6E, "/": 0x6F}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


_dpi_aware = False


def ensure_dpi_aware() -> None:
    """声明进程 DPI 感知，使物理像素 == 逻辑坐标（截图与鼠标坐标 1:1）。"""
    global _dpi_aware
    if _dpi_aware or os.name != "nt":
        return
    try:
        # 尝试按显示器感知（推荐），失败则退化为系统 DPI 感知
        ctx = ctypes.c_int(-4)  # PROCESS_PER_MONITOR_DPI_AWARE
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(ctx.value)):
            _dpi_aware = True
            return
        _dpi_aware = bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception as e:  # noqa: BLE001
        LOG.warning("DPI 感知设置失败（坐标可能受系统缩放影响）: %s", e)


def virtual_screen_offset() -> tuple[int, int]:
    """返回虚拟屏幕左上角偏移 (ox, oy)。

    多显示器时副屏可能位于主屏左/上（负坐标），ImageGrab 的截图像素
    从虚拟屏幕左上角开始，因此截图坐标 (x, y) 对应的屏幕物理坐标为
    (x + ox, y + oy)。单屏时返回 (0, 0)。
    """
    if os.name != "nt":
        return 0, 0
    user32 = ctypes.windll.user32
    ox = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    oy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return int(ox), int(oy)


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("键盘/鼠标控制仅支持 Windows")


# ---------- 键盘 ----------

def _send_keyboard(wVk: int, wScan: int, flags: int) -> None:
    """发送单个键盘事件（SendInput）。"""
    user32 = ctypes.windll.user32
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = wVk
    inp.union.ki.wScan = wScan
    inp.union.ki.dwFlags = flags
    inp.union.ki.time = 0
    inp.union.ki.dwExtraInfo = None
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        LOG.warning("SendInput 键盘事件未完全发送（sent=%s）", sent)


def _vk_from_char(ch: str) -> tuple[int, bool]:
    """把单个字符映射为 (虚拟键码, 是否需要shift)。

    返回 (vk, need_shift)；字符无法用虚拟键码表达时返回 (0, False)，
    此时应使用 Unicode 输入（KEYEVENTF_UNICODE）。
    """
    if ch.isalpha() and ch.isascii():
        return ord(ch.upper()), False
    if ch.isdigit():
        return ord(ch), False
    if ch in _SHIFT_SYMBOLS:
        return _vk_from_char(_SHIFT_SYMBOLS[ch])[0], True
    vk = _SYMBOL_VK.get(ch)
    return (vk, False) if vk else (0, False)


def _parse_key_name(name: str) -> int:
    """解析按键名（大小写不敏感）→ 虚拟键码；失败抛 ValueError。"""
    key = name.strip().lower()
    if not key:
        raise ValueError("按键名为空")
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if key.startswith("f") and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return 0x70 + n - 1
    if key.startswith("num"):
        k = key[3:]
        if k in _NUM_KEYPAD:
            return _NUM_KEYPAD[k]
    if len(key) == 1 and key.isalpha():
        return ord(key.upper())
    if len(key) == 1 and key.isdigit():
        return ord(key)
    if len(key) == 1:
        vk, _ = _vk_from_char(key)
        if vk:
            return vk
    raise ValueError(f"无法识别的按键名: {name}")


def type_text(text: str, interval: float = 0.01) -> None:
    """通过 SendInput Unicode 直输文本（支持中文、任意 Unicode）。"""
    _require_windows()
    ensure_dpi_aware()
    if not text:
        return
    for ch in text:
        _send_keyboard(0, ord(ch), KEYEVENTF_UNICODE)
        _send_keyboard(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        if interval > 0:
            time.sleep(interval)


def press_keys(keys: list[str]) -> None:
    """按下/抬起按键组合（支持修饰键，如 ['ctrl', 'c']）。

    修饰键（ctrl/shift/alt/win）按顺序按下，其余按键逐个点按，最后逆序抬起修饰键。
    """
    _require_windows()
    ensure_dpi_aware()
    if not keys:
        return
    vks: list[int] = [_parse_key_name(k) for k in keys]
    modifiers = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN}
    # 修饰键按下
    for vk in vks:
        if vk in modifiers:
            _send_keyboard(vk, 0, 0)
    # 普通按键点按
    for vk in vks:
        if vk not in modifiers:
            _send_keyboard(vk, 0, 0)
            _send_keyboard(vk, 0, KEYEVENTF_KEYUP)
    # 修饰键逆序抬起
    for vk in reversed(vks):
        if vk in modifiers:
            _send_keyboard(vk, 0, KEYEVENTF_KEYUP)


# ---------- 剪贴板粘贴（兜底方案） ----------

def _set_clipboard_text(text: str) -> None:
    """把文本写入系统剪贴板（CF_UNICODETEXT）。失败抛异常。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard 失败（剪贴板被占用？）")
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError("GlobalAlloc 失败")
        ptr = kernel32.GlobalLock(h)
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            raise OSError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


def paste_text(text: str) -> None:
    """把文本写入剪贴板并发送 Ctrl+V 粘贴（部分程序不接受 Unicode 输入时用）。"""
    _require_windows()
    ensure_dpi_aware()
    _set_clipboard_text(text)
    press_keys(["ctrl", "v"])


def get_clipboard_text() -> str:
    """读取系统剪贴板文本（CF_UNICODETEXT）。

    非 Windows / 剪贴板无文本 / 打开失败时返回空串（不抛异常，便于远程调用）。
    """
    if os.name != "nt":
        return ""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        LOG.warning("OpenClipboard 失败（剪贴板被占用？）")
        return ""
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(h)
    except Exception as e:  # noqa: BLE001
        LOG.warning("读取剪贴板失败: %s", e)
        return ""
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    """把文本写入系统剪贴板（不粘贴）。"""
    _require_windows()
    _set_clipboard_text(text)


# ---------- 窗口激活 ----------

GA_ROOT = 2


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def _window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def activate_window_at(x: int, y: int) -> str | None:
    """激活截图像素坐标 (x, y) 处的顶层窗口（若其不在前台）。

    背景：Windows 会把发送到非前台窗口的首次点击「吃掉」用作激活，导致
    自动化点击落到后台窗口时第一次无效。因此在点击前先把目标窗口提到前台。

    返回被激活窗口的标题；无需激活（已在后台判定失败/已在前台）时返回 None。
    """
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    ox, oy = virtual_screen_offset()
    pt = POINT(int(x + ox), int(y + oy))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    fg = user32.GetForegroundWindow()
    if fg == root:
        return None  # 已在前台，无需处理
    title = _window_title(root)
    try:
        # 先尝试 Alt 轻敲：解除前台锁定限制（Windows 的前台切换规则）
        press_keys(["alt"])
        if user32.SetForegroundWindow(root):
            time.sleep(0.15)
            return title or "(无标题)"
    except Exception as e:  # noqa: BLE001
        LOG.warning("激活窗口失败: %s", e)
    return None


# ---------- 鼠标 ----------

def _move_to(x: int, y: int) -> None:
    """把光标移动到虚拟屏幕物理坐标 (x, y)。"""
    user32 = ctypes.windll.user32
    ok = user32.SetCursorPos(int(x), int(y))
    if not ok:
        LOG.warning("SetCursorPos(%s, %s) 失败", x, y)


def _button_flags(button: str, down: bool) -> int:
    b = (button or "left").lower()
    table = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
    if b not in table:
        raise ValueError(f"不支持的鼠标键: {button}（可选 left/right/middle）")
    return table[b][0 if down else 1]


def _mouse_event(flags: int, data: int = 0) -> None:
    user32 = ctypes.windll.user32
    user32.mouse_event(flags, 0, 0, data, 0)


def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1,
                interval: float = 0.05, activate: bool = True) -> str | None:
    """移动光标到 (x, y) 并点击（支持双击等多次点击）。坐标=截图像素。

    activate=True 时，若目标处窗口不在前台，先激活它——否则 Windows 会把
    首次点击当作「激活窗口」吃掉，导致后台窗口的第一次点击无效。
    返回被激活窗口的标题（无需激活时为 None）。
    """
    _require_windows()
    ensure_dpi_aware()
    activated = activate_window_at(x, y) if activate else None
    ox, oy = virtual_screen_offset()
    _move_to(x + ox, y + oy)
    time.sleep(0.03)  # 等待光标到位/悬停效果
    down = _button_flags(button, True)
    up = _button_flags(button, False)
    for i in range(max(1, int(clicks))):
        _mouse_event(down)
        _mouse_event(up)
        if i + 1 < int(clicks):
            time.sleep(interval)
    return activated


def mouse_move(x: int, y: int) -> None:
    """仅移动光标（悬停），不点击。坐标=截图像素。"""
    _require_windows()
    ensure_dpi_aware()
    ox, oy = virtual_screen_offset()
    _move_to(x + ox, y + oy)


def mouse_scroll(dy: int, x: int | None = None, y: int | None = None) -> None:
    """滚动滚轮。dy>0 向上，dy<0 向下（单位：格）。

    指定 x/y 时先移动光标到该位置再滚动。
    """
    _require_windows()
    ensure_dpi_aware()
    if x is not None and y is not None:
        ox, oy = virtual_screen_offset()
        _move_to(x + ox, y + oy)
        time.sleep(0.03)
    data = int(dy) * WHEEL_DELTA
    _mouse_event(MOUSEEVENTF_WHEEL, data)


def mouse_drag(x1: int, y1: int, x2: int, y2: int,
               button: str = "left", steps: int = 10,
               interval: float = 0.01) -> None:
    """拖拽：从 (x1,y1) 按住移动到 (x2,y2) 释放。坐标=截图像素。"""
    _require_windows()
    ensure_dpi_aware()
    ox, oy = virtual_screen_offset()
    _move_to(x1 + ox, y1 + oy)
    time.sleep(0.03)
    down = _button_flags(button, True)
    up = _button_flags(button, False)
    _mouse_event(down)
    try:
        steps = max(1, int(steps))
        for i in range(1, steps + 1):
            nx = x1 + (x2 - x1) * i / steps
            ny = y1 + (y2 - y1) * i / steps
            _move_to(nx + ox, ny + oy)
            if interval > 0:
                time.sleep(interval)
    finally:
        _mouse_event(up)

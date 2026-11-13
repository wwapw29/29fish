"""Windows 真实输入注入（SendInput）。

为什么要用 SendInput 而不是 pyautogui：
  很多游戏（含 WoW）只认「硬件层」的输入事件，SendInput 走的是系统输入队列，
  带上 SCANCODE 标志后和真实键盘几乎无法区分；而直接 PostMessage 游戏根本不理会。
"""
from __future__ import annotations

import ctypes
import random
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def enable_dpi_awareness() -> None:
    """必须在任何截图/窗口操作之前调用，否则 125% 缩放下坐标全错。"""
    try:
        # PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_VK_NAMES = {
    "space": 0x20, "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "tab": 0x09,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
}


def vk_from_name(name: str) -> int:
    """把配置文件里的按键名转成虚拟键码。支持 '1' / 'a' / 'F5' / '-' / 'f1'。"""
    s = str(name).strip()
    low = s.lower()
    if low in _VK_NAMES:
        return _VK_NAMES[low]
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return 0x6F + int(low[1:])   # F1 = 0x70
    if len(s) == 1:
        return ord(s.upper())
    raise ValueError(f"无法识别的按键名: {name!r}")


class WinInput:
    """鼠标 / 键盘注入。坐标是「虚拟桌面绝对坐标」。"""

    def __init__(self, humanize: bool = True, jitter_px: int = 3, hold_ms: int = 45):
        self.humanize = humanize
        self.jitter_px = jitter_px
        self.hold_ms = hold_ms
        self._multi_monitor = self._has_multi_monitor()

    # ---------------- 内部 ----------------
    @staticmethod
    def _has_multi_monitor() -> bool:
        return user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) > user32.GetSystemMetrics(0)

    @staticmethod
    def _send(*inputs: INPUT) -> None:
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(INPUT))

    def _mouse_input(self, dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    # ---------------- 鼠标 ----------------
    def move_to(self, x: int, y: int) -> None:
        """绝对定位（物理像素，虚拟桌面坐标系）。"""
        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        if self._multi_monitor:
            flags |= MOUSEEVENTF_VIRTUALDESK

        nx = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ny = int(round((y - vy) * 65535 / max(1, vh - 1)))
        self._send(self._mouse_input(nx, ny, flags))

    def click(self, x: int | None = None, y: int | None = None, button: str = "left") -> None:
        """单击；给坐标就先移过去（带随机抖动）。button: left / right"""
        if button == "right":
            down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        else:
            down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP

        if x is not None and y is not None:
            if self.humanize and self.jitter_px > 0:
                x += random.randint(-self.jitter_px, self.jitter_px)
                y += random.randint(-self.jitter_px, self.jitter_px)
            self.move_to(x, y)
            # 停一下再按：游戏要先把鼠标悬停事件处理完（鱼漂会高亮并显示"鱼漂"），
            # 悬停和按下挤在一起容易点空
            time.sleep(random.uniform(0.030, 0.055))

        self._send(self._mouse_input(0, 0, down_flag))
        hold = self.hold_ms
        if self.humanize:
            hold = max(20, int(hold * random.uniform(0.8, 1.35)))
        time.sleep(hold / 1000.0)
        self._send(self._mouse_input(0, 0, up_flag))

    # ---------------- 键盘 ----------------
    def key_tap(self, vk: int, hold_ms: int = 55) -> None:
        sc = user32.MapVirtualKeyW(vk, 0)
        down = INPUT(type=INPUT_KEYBOARD)
        down.u.ki = KEYBDINPUT(wVk=0, wScan=sc, dwFlags=KEYEVENTF_SCANCODE, time=0, dwExtraInfo=0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.u.ki = KEYBDINPUT(wVk=0, wScan=sc, dwFlags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                             time=0, dwExtraInfo=0)
        self._send(down)
        time.sleep(hold_ms / 1000.0)
        self._send(up)

    # ---------------- 窗口 ----------------
    @staticmethod
    def list_windows() -> list[tuple[int, str]]:
        """枚举所有「可见且有标题」的顶层窗口。"""
        found: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    found.append((hwnd, buf.value))
            return True

        user32.EnumWindows(_cb, 0)
        return found

    @classmethod
    def _norm(cls, keywords: str | list[str]) -> list[str]:
        if isinstance(keywords, str):
            keywords = [keywords]
        return [k.strip() for k in keywords if k and k.strip()]

    @classmethod
    def find_windows(cls, keywords: str | list[str]) -> list[tuple[int, str]]:
        """按标题找窗口。**精确匹配优先**，其次才是包含匹配。

        精确优先很重要：项目文件夹也叫 "wow-fishing"，用 "wow" 模糊搜会命中
        资源管理器，结果按键全打给了文件夹窗口。
        """
        kws = cls._norm(keywords)
        if not kws:
            return []
        exact: list[tuple[int, str]] = []
        fuzzy: list[tuple[int, str]] = []
        for hwnd, title in cls.list_windows():
            t = title.strip()
            if t in kws:
                exact.append((hwnd, title))
            elif any(k.lower() in title.lower() for k in kws):
                fuzzy.append((hwnd, title))
        return exact + fuzzy

    @staticmethod
    def get_foreground_title() -> str:
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    @classmethod
    def is_foreground(cls, keywords: str | list[str]) -> bool:
        title = cls.get_foreground_title()
        if not title:
            return False
        kws = cls._norm(keywords)
        if title.strip() in kws:
            return True
        return any(k.lower() in title.lower() for k in kws)

    def activate_window(self, keywords: str | list[str]) -> bool:
        """把匹配标题的窗口拉到前台，返回「最终是否真的在前台」。"""
        wins = self.find_windows(keywords)
        if not wins:
            return False
        hwnd = wins[0][0]
        if user32.GetForegroundWindow() == hwnd:
            return True

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            time.sleep(0.08)

        user32.SetForegroundWindow(hwnd)
        if not self.is_foreground(keywords):
            # 前台锁定时的老办法：先挂到当前前台线程再设置
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(tid_me, tid_fg, True)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(tid_me, tid_fg, False)
            time.sleep(0.06)

        if not self.is_foreground(keywords):
            # 再补一刀：最小化再还原，能破掉大多数拒绝置前的情况
            user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
            time.sleep(0.05)
            user32.ShowWindow(hwnd, 9)
            time.sleep(0.08)

        return self.is_foreground(keywords)

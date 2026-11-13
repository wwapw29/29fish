"""在屏幕上叠加显示「搜索范围」框。

用法:
    python tools/roi_overlay.py       （或双击 roi-overlay.bat）

纯 Win32 API 实现：一个置顶、点击穿透、不抢焦点的透明窗口，只在屏幕上画一个绿框。
框的位置就是 config.toml 里 region 换算出的像素范围 —— 所见即程序所扫。
按 ESC 退出。

为什么不用 tkinter：这台机器的 Python 是精简版，没带 tkinter。
Win32 分层窗口（WS_EX_LAYERED + 颜色键透明）反而更轻，也不会干扰游戏渲染。
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wf.paths import app_root                   # noqa: E402

ROOT = app_root()

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 必须在取屏幕尺寸之前开 DPI 感知，否则 2560x1440 会被缩成 2048x1152
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

COLORKEY = 0x00000001            # COLORREF 0x00BBGGRR，用它做透明色（几乎不会撞色）
GREEN = 0x0066FF00               # #00FF66 —— 鱼漂搜索区域
CYAN = 0x00FFCC00                # #00CCFF —— 结果提示（OCR）区域

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
LWA_COLORKEY = 0x00000001

WM_PAINT = 0x000F
WM_DESTROY = 0x0002
VK_ESCAPE = 0x1B

LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
# WPARAM/LPARAM 必须显式用「指针宽度」的整数，否则 64 位下容易溢出
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


def load_regions() -> dict[str, tuple[int, int, int, int]]:
    """读 config.toml 里的两个区域，换算成像素。

    · search —— 鱼漂搜索区域（程序每帧扫的就是这块）
    · verify —— 屏幕上方那行结果提示的区域（扬竿后 1 秒单独截这块去 OCR）
    """
    import tomllib
    p = ROOT / "config.toml"
    data = {}
    if p.exists():
        with p.open("rb") as fp:
            data = tomllib.load(fp)

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)

    raw = {
        "search": data.get("search", {}).get("region", [0.28, 0.15, 0.72, 0.62]),
        "verify": data.get("verify", {}).get("region", [0.42, 0.07, 0.54, 0.15]),
    }
    out = {}
    for key, (x1, y1, x2, y2) in raw.items():
        x = int(round(min(x1, x2) * sw))
        y = int(round(min(y1, y2) * sh))
        w = max(8, int(round(abs(x2 - x1) * sw)))
        h = max(8, int(round(abs(y2 - y1) * sh)))
        out[key] = (x, y, w, h)
    return out


def main() -> int:
    regions = load_regions()

    # 注意：必须用**主屏**尺寸，不能用 SM_CXVIRTUALSCREEN。
    # 这台机器挂了副屏（虚拟屏 3640x1920 vs 主屏 2560x1440），按虚拟屏建窗口会让
    # 客户区坐标和游戏画面错位，框就画到别处去了。
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    vx = vy = 0

    hinst = kernel32.GetModuleHandleW(None)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            for key, color in (("search", GREEN), ("verify", CYAN)):
                if key not in regions:
                    continue
                rx, ry, rw, rh = regions[key]
                thick = 4 if key == "search" else 3
                # 4 层 1px 矩形叠加当粗线
                for i in range(thick):
                    r = wintypes.RECT(rx - i, ry - i, rx + rw + i, ry + rh + i)
                    hbr = gdi32.CreateSolidBrush(color)
                    user32.FrameRect(hdc, ctypes.byref(r), hbr)
                    gdi32.DeleteObject(hbr)
                # 四角加粗，方便看清边界
                L = 46 if key == "search" else 26
                corner = gdi32.CreateSolidBrush(color)
                for cx, cy, dx, dy in ((rx, ry, 1, 1), (rx + rw, ry, -1, 1),
                                       (rx, ry + rh, 1, -1), (rx + rw, ry + rh, -1, -1)):
                    for tt in range(6):
                        if dx > 0:
                            r = wintypes.RECT(cx, cy + dy * tt, cx + L, cy + dy * tt + 1)
                        else:
                            r = wintypes.RECT(cx - L, cy + dy * tt, cx, cy + dy * tt + 1)
                        user32.FillRect(hdc, ctypes.byref(r), corner)
                gdi32.DeleteObject(corner)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROCTYPE(wndproc)
    # 这几个 API 必须声明签名，否则 64 位指针会被截断
    user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.FrameRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    gdi32.CreateSolidBrush.argtypes = [ctypes.c_uint32]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    user32.BeginPaint.restype = wintypes.HDC
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.style = 0
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.hbrBackground = gdi32.CreateSolidBrush(COLORKEY)
    wc.lpszClassName = "WowFishingRoiOverlay"
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        print("注册窗口类失败:", ctypes.get_last_error())
        return 1

    ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    hwnd = user32.CreateWindowExW(
        ex, "WowFishingRoiOverlay", "ROI", WS_POPUP | WS_VISIBLE,
        vx, vy, sw, sh, None, None, hinst, None)
    if not hwnd:
        print("创建窗口失败:", ctypes.get_last_error())
        return 1

    user32.SetLayeredWindowAttributes(hwnd, COLORKEY, 0, LWA_COLORKEY)
    user32.ShowWindow(hwnd, 8)                    # SW_SHOWNA，显示但不激活
    # 置顶 + 不激活
    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE

    s = regions.get("search")
    v = regions.get("verify")
    print("叠加框已显示：")
    if s:
        print(f"  绿框 鱼漂搜索区域 : ({s[0]},{s[1]}) {s[2]}x{s[3]}  ← [search] region")
    if v:
        print(f"  青框 结果提示区域 : ({v[0]},{v[1]}) {v[2]}x{v[3]}  ← [verify] region")
    print("按 ESC 退出。")

    msg = wintypes.MSG()
    while True:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):   # PM_REMOVE
            if msg.message == 0x0012:                                  # WM_QUIT
                return 0
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000:
            break
        time.sleep(0.05)

    user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

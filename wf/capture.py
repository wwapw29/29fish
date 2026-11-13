"""屏幕捕获。用 mss 抓指定 ROI，尽量少抓像素以拉高帧率。"""
from __future__ import annotations

import numpy as np
import mss


class Capturer:
    """整屏/区域截图器。坐标一律是「相对于目标显示器左上角」的像素坐标。"""

    def __init__(self, monitor: int = 0):
        self.sct = mss.mss()
        mons = self.sct.monitors
        if monitor + 1 >= len(mons):
            monitor = 0
        self.monitor_index = monitor
        mon = mons[monitor + 1]          # monitors[0] 是全部显示器拼接
        self.left = int(mon["left"])
        self.top = int(mon["top"])
        self.width = int(mon["width"])
        self.height = int(mon["height"])

    # ---------------- 基础抓取 ----------------
    def grab_roi(self, x: int, y: int, w: int, h: int) -> np.ndarray:
        """抓取显示器内的一块区域，返回 BGR uint8 数组。"""
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        w = max(1, min(int(w), self.width - x))
        h = max(1, min(int(h), self.height - y))
        box = {
            "left": self.left + x,
            "top": self.top + y,
            "width": w,
            "height": h,
        }
        raw = self.sct.grab(box)
        # mss 返回 BGRA，前三个通道正是 BGR
        return np.asarray(raw)[:, :, :3].copy()

    def grab_screen(self) -> np.ndarray:
        return self.grab_roi(0, 0, self.width, self.height)

    # ---------------- ROI 换算 ----------------
    def region_to_px(self, region: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        """把 0~1 的比例区域换算成像素 (x, y, w, h)。"""
        x1, y1, x2, y2 = region
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        x = int(round(x1 * self.width))
        y = int(round(y1 * self.height))
        w = max(8, int(round((x2 - x1) * self.width)))
        h = max(8, int(round((y2 - y1) * self.height)))
        return x, y, w, h

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass

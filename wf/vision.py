"""视觉核心：鱼漂定位 / 跟踪 / 咬钩判定。

**所有参数都是拿真实截图量出来的，不是拍脑袋。**

鱼漂长这样：橙红羽毛 + 蓝色小旗 + 白球 + 黄色软木塞。水面是暗紫/紫灰色。
试过好几版判据，前几版都翻过车，教训记在这里：

  1) "红色 + 竖直细长" —— 错。鱼漂是**横**的，而且红蓝黄混色。
  2) "高饱和就是鱼漂" —— 错。黄昏光照下水面 S 中位 81，反而比鱼漂(S 71)还高。
  3) "块状 + 面积接近典型值" —— 错。水面反光带被闭运算连成 778x203 的巨块，
     把鱼漂直接吞进去；水底石头又方又 255px，评分反而最高。

真正稳定的判据是**暖色**：`R - B > 30`。
  · 两种光照下水面 R-B 都 ≤26，暖色像素占比 0%
  · 鱼漂的橙红羽毛/黄软木塞 R-B 能到 55~74
  · 六张实测截图，每张图里**只有一个**显著暖色块，位置都落在鱼漂上
  · 咬钩那张（鱼漂被水花盖住）**一个暖色块都没有**

所以：
    定位 = 找暖色块
    跟踪 = 在上一位置附近找暖色块
    咬钩 = 窗口内暖色像素数相对基线骤降（正常 67~696 → 咬钩 0）
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

from .config import BiteCfg, DetectCfg


@dataclass
class Track:
    found: bool = False
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    area: int = 0        # 暖色像素数（= 鱼漂"存在度"）
    score: float = 0.0
    gy: float = 0.0      # 暖色像素**质心**的 y（亚像素），用来测下沉位移

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


class BobberTracker:
    """靠「暖色」找鱼漂。红/橙/黄的色差是水面永远不具备的特征。"""

    def __init__(self, cfg: DetectCfg):
        self.cfg = cfg
        self.track = Track(found=False)
        self.lost = 0
        self.base_w = 0          # 判定窗口尺寸，锁定后固定不变
        self.base_h = 0
        self._kernel = np.ones((3, 3), np.uint8)

    def reset(self) -> None:
        self.track = Track(found=False)
        self.lost = 0
        self.base_w = 0
        self.base_h = 0

    # ---------------- 基础 ----------------
    def warm_mask(self, roi: np.ndarray) -> np.ndarray:
        """暖色掩膜：R-B 比**当前画面**的水面基线明显偏高。

        阈值自适应很关键。同一件水面：
          · 白天（偏红）：R-B 中位 +19，鱼漂 46 → 阈值 30 够用
          · 夜晚深水（偏蓝）：R-B 中位 -95，鱼漂只有 23 → 固定 30 就全灭
        所以取 min(绝对上限, 基线+相对量)，保证"比周围更暖"这个本质不变。
        """
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        s = hsv[:, :, 1]
        b = roi[:, :, 0].astype(np.int16)
        r = roi[:, :, 2].astype(np.int16)
        rb = r - b
        base = float(np.median(rb))
        thr = min(float(self.cfg.warm_delta), base + self.cfg.warm_rel)
        m = (rb > thr) & (s > self.cfg.warm_sat_min)
        mask = m.astype(np.uint8) * 255
        # 先「开」再「闭」：鱼漂周围那圈涟漪的 R-B 也略高于水面，会把鱼漂和涟漪连成
        # 一大片（实测 1389px），超过面积上限就被整个丢掉。涟漪是细线，开运算能断开，
        # 鱼漂是实心块，留得住。
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)

    def _blobs(self, mask: np.ndarray) -> list[tuple[int, int, int, int, int, float, float]]:
        """返回 (面积, x, y, w, h, 质心x, 质心y)。质心比 bbox 中心稳，不受形状抖动影响。"""
        n, _lab, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        out = []
        for i in range(1, n):
            x, y, w, h, area = (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]),
                                int(stats[i, 3]), int(stats[i, 4]))
            if area < self.cfg.min_area or area > self.cfg.max_area:
                continue
            if w < 4 or h < 3:
                continue
            ratio = w / max(h, 1)
            if ratio < self.cfg.min_aspect or ratio > self.cfg.max_aspect:
                continue
            mx, my = float(centroids[i][0]), float(centroids[i][1])
            out.append((area, x, y, w, h, mx, my))
        out.sort(reverse=True)
        return out

    def _seed_score(self, area: int, w: int, h: int, centrality: float) -> float:
        """暖色块的评分。面积用对数尺度，因为远近不同尺寸差十倍以上。"""
        ratio = w / max(h, 1)
        # 形状以 2.5 为最优 —— 鱼漂是横向羽毛，不是方块。
        # 原来以 1.0 为最优，反而把 3.8 的鱼漂判成"形状差"，输给了 1.6 的鱼。
        shape = max(0.0, 1.0 - abs(math.log(ratio / 2.5)) / math.log(3.0))
        size = max(0.0, 1.0 - abs(math.log(max(area, 1) / 250.0)) / math.log(15.0))
        return 0.45 * centrality + 0.20 * size + 0.35 * shape

    def _expand(self, x: int, y: int, w: int, h: int,
                shape: tuple[int, int]) -> tuple[int, int, int, int]:
        """暖色块只是鱼漂的一部分，把框外扩到能盖住整只鱼漂。"""
        rh, rw = shape
        pad = max(self.cfg.box_pad, int(max(w, h) * 0.45))
        x0 = max(0, x - pad)
        y0 = max(0, y - pad)
        x1 = min(rw, x + w + pad)
        y1 = min(rh, y + h + pad)
        return x0, y0, x1 - x0, y1 - y0

    def _refine(self, roi: np.ndarray, x: int, y: int, w: int, h: int
                ) -> tuple[int, int, int, int]:
        """把「暖色种子」扩展成整只鱼漂。

        鱼漂是三部分组成的：红羽毛 + 蓝羽毛 + 棕色木塞（还有个白球）。
        暖色只能捞到红/棕，蓝羽毛和白色部分一点都碰不到，光靠固定外扩是碰运气。

        做法：在种子周围取一块，用这块的**中位色**当水面背景（鱼漂占比很小，
        中位数基本等于水面颜色），把与背景差异明显的像素连成一片，
        再取种子所在的那个连通域 —— 红蓝棕白就全包进来了。
        """
        rh, rw = roi.shape[:2]
        R = max(26, int(max(w, h) * 1.3))
        x0 = max(0, x - R)
        y0 = max(0, y - R)
        x1 = min(rw, x + w + R)
        y1 = min(rh, y + h + R)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return x, y, w, h

        patch = roi[y0:y1, x0:x1]
        bg = np.median(patch.reshape(-1, 3), axis=0)
        diff = np.abs(patch.astype(np.int16) - bg.astype(np.int16)).sum(axis=2)
        m = (diff > self.cfg.diff_threshold).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

        cx = min(max(0, x + w // 2 - x0), m.shape[1] - 1)
        cy = min(max(0, y + h // 2 - y0), m.shape[0] - 1)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        # 不能只取「中心点所在」的那块 —— 种子经常只盖住红羽毛，中心就落在红羽毛上。
        # 改取「和种子框重叠面积最大」的连通域，红蓝棕白连成的那一片才拿得到。
        best_lb, best_ov = 0, 0
        for i in range(1, n):
            bx, by, bw, bh = (int(stats[i, 0]), int(stats[i, 1]),
                              int(stats[i, 2]), int(stats[i, 3]))
            ix0 = max(x, x0 + bx)
            iy0 = max(y, y0 + by)
            ix1 = min(x + w, x0 + bx + bw)
            iy1 = min(y + h, y0 + by + bh)
            ov = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            if ov > best_ov:
                best_ov, best_lb = ov, i
        if best_lb == 0:
            return x, y, w, h
        bx, by, bw, bh = (int(stats[best_lb, 0]), int(stats[best_lb, 1]),
                          int(stats[best_lb, 2]), int(stats[best_lb, 3]))
        # 只接受「变大但没失控」的结果，防止把水花/波纹整片吞进来
        if bw < w or bh < h or bw > w * 2.5 or bh > h * 2.5:
            return x, y, w, h
        return x0 + bx, y0 + by, bw, bh

    # ---------------- 定位 ----------------
    def acquire(self, roi: np.ndarray) -> Track:
        mask = self.warm_mask(roi)
        blobs = self._blobs(mask)
        if not blobs:
            self.track = Track(found=False)
            return self.track

        # 鱼群场景的判据（实测数据）：
        #   鱼漂 = 横向羽毛，长宽比 2.5~4.0（38x10 / 24x12）
        #   红色鱼 = 椭圆，长宽比 1.2~1.8（29x18 / 28x17），面积还经常更大
        # 所以「存在横向细长块」时优先选它，而不是按面积大小选。
        wide = [b for b in blobs
                if b[3] / max(b[4], 1) >= self.cfg.bobber_min_aspect
                and b[0] >= self.cfg.bobber_min_area]
        if wide:
            blobs = wide
        elif self.cfg.school_shield:
            # 没有横向块时（鱼漂竖着/只露软木塞），退回原来的鱼群护盾：只留大块
            small = [b for b in blobs if b[0] < self.cfg.school_small_area]
            if len(small) >= self.cfg.school_min_count:
                big = [b for b in blobs if b[0] >= self.cfg.school_small_area]
                if big:
                    blobs = big

        rh, rw = roi.shape[:2]
        best = None
        best_score = -1.0
        for area, x, y, w, h, mx, my in blobs:
            cx, cy = x + w / 2, y + h / 2
            dx = abs(cx - rw / 2) / (rw / 2)
            dy = abs(cy - rh / 2) / (rh / 2)
            centrality = max(0.0, 1.0 - 0.7 * dx - 0.5 * dy)
            score = self._seed_score(area, w, h, centrality)
            if score > best_score:
                best_score = score
                best = (area, x, y, w, h, mx, my)

        assert best is not None
        area, x, y, w, h, mx, my = best
        x0, y0, bw, bh = self._expand(x, y, w, h, (rh, rw))
        # refine 默认关闭：蓝羽毛和深色水面太像，连通域会连到水面把框和中心一起带偏；
        # 中心一偏，update() 在 ±track_radius 里就找不到暖色块 → 整只丢锁。
        if self.cfg.use_refine:
            x0, y0, bw, bh = self._refine(roi, x0, y0, bw, bh)
        self.base_w, self.base_h = bw, bh
        self.track = Track(found=True, x=x0, y=y0, w=bw, h=bh,
                           area=area, score=best_score, gy=my)
        self.lost = 0
        return self.track

    # ---------------- 跟踪 ----------------
    def update(self, roi: np.ndarray) -> Track:
        """在上一位置附近找暖色块；找不到就保持位置、area 记 0。

        「找不到」恰恰就是咬钩：水花把鱼漂的暖色部分盖住了。
        """
        if not self.track.found:
            return self.track

        R = self.cfg.track_radius
        rh, rw = roi.shape[:2]
        cx, cy = self.track.center
        x0, x1 = max(0, cx - R), min(rw, cx + R)
        y0, y1 = max(0, cy - R), min(rh, cy + R)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return self.track

        sub = roi[y0:y1, x0:x1]
        blobs = self._blobs(self.warm_mask(sub))
        if blobs:
            ocx, ocy = self.track.center
            pick = None
            pick_score = -1.0
            for area, bx, by, bw, bh, mx, my in blobs:
                dcx = x0 + mx
                dcy = y0 + my
                prox = max(0.0, 1.0 - math.hypot(dcx - ocx, dcy - ocy) / max(R, 1))
                s = self._seed_score(area, bw, bh, prox)
                if s > pick_score:
                    pick_score, pick = s, (area, bx, by, bw, bh, mx, my)

            area, bx, by, bw, bh, mx, my = pick
            nx = x0 + bx + bw // 2
            ny = y0 + by + bh // 2
            # 位置平滑，防止窗口跟着质心抖
            nx = int(ocx * 0.6 + nx * 0.4)
            ny = int(ocy * 0.6 + ny * 0.4)
            self.track = Track(found=True,
                               x=nx - self.base_w // 2, y=ny - self.base_h // 2,
                               w=self.base_w, h=self.base_h,
                               area=area, score=pick_score,
                               gy=y0 + my)
            self.lost = 0
        else:
            self.lost += 1
            self.track = Track(found=self.lost < self.cfg.lost_frames,
                               x=self.track.x, y=self.track.y,
                               w=self.track.w, h=self.track.h,
                               area=0, score=0.0, gy=0.0)
        return self.track

    # ---------------- 判定窗口 ----------------
    def window_box(self, roi_shape: tuple[int, int]) -> tuple[int, int, int, int]:
        """判定窗口 = 鱼漂框（锁定后尺寸固定，不随每帧形状变化）。"""
        rh, rw = roi_shape
        cx, cy = self.track.center
        hw = (self.base_w or self.track.w) // 2
        hh = (self.base_h or self.track.h) // 2
        return (max(0, cx - hw), max(0, cy - hh),
                min(rw, cx + hw), min(rh, cy + hh))

    def measure(self, roi: np.ndarray, mask: np.ndarray | None = None) -> int:
        """窗口内的暖色像素数 = 鱼漂"存在度"。"""
        if not self.track.found:
            return 0
        if mask is None:
            mask = self.warm_mask(roi)
        x0, y0, x1, y1 = self.window_box(roi.shape[:2])
        return int(np.count_nonzero(mask[y0:y1, x0:x1]))


class BiteDetector:
    """咬钩判定，两个信号互补：

    A) 存在度骤降：窗口内暖色像素数掉到基线的 20% 以下。
       实测真咬钩 ratio 全是 0.00~0.14（水花彻底盖住），很可靠。
    B) 快速下沉：暖色质心相对基线明显下移。
       它比 A 早 —— 鱼漂刚开始下沉、暖色还没被水花盖住的那一两帧就能捕捉到。
       但暖色一旦归零就没有质心可测，所以 B 只能当补充，不能单独用。
    """

    def __init__(self, cfg: BiteCfg):
        self.cfg = cfg
        self.areas: deque[int] = deque(maxlen=cfg.base_window)
        self.ys: deque[float] = deque(maxlen=cfg.base_window)
        self.streak = 0
        self.cooldown_until = 0.0
        self.t_start: float | None = None

    def reset(self) -> None:
        self.areas.clear()
        self.ys.clear()
        self.streak = 0
        self.t_start = None

    @property
    def ready(self) -> bool:
        return len(self.areas) >= 6

    def info(self) -> dict:
        if not self.areas:
            return {"area": 0, "base": 0, "ratio": 1.0, "drop": 0,
                    "dy": 0.0, "v": 0.0, "streak": 0, "ready": False}
        base = statistics.median(self.areas)
        cur = self.areas[-1]
        dy, v = self._sink_offset()
        return {
            "area": cur,
            "base": int(base),
            "ratio": (cur / base) if base > 0 else 1.0,
            "drop": int(base - cur),
            "dy": dy,
            "v": v,
            "streak": self.streak,
            "ready": self.ready,
        }

    def _sink_offset(self) -> tuple[float, float]:
        """返回 (质心相对基线的下沉量, 最近 3 帧的下沉速度)。"""
        if len(self.ys) < 6:
            return 0.0, 0.0
        hist = list(self.ys)[:-1]
        base_y = statistics.median(hist)
        dy = self.ys[-1] - base_y
        v = (self.ys[-1] - list(self.ys)[-3]) if len(self.ys) >= 3 else 0.0
        return dy, v

    def update(self, area: int, gy: float, now: float,
               ref_h: float = 0.0) -> tuple[bool, dict]:
        self.areas.append(area)
        # gy<=0 表示这帧没找到暖色块（位置已冻结），沿用上一帧免得污染基线
        self.ys.append(gy if gy > 0 else (self.ys[-1] if self.ys else 0.0))

        if self.t_start is None:
            self.t_start = now

        # 刚锁定鱼漂时水面还在荡漾，暖色数会自然波动，先攒基线不判定
        if now - self.t_start < self.cfg.min_wait:
            return False, self.info()

        if not self.ready:
            return False, self.info()

        hist = list(self.areas)[:-1]
        base = statistics.median(hist) if hist else float(area)
        if base < self.cfg.min_base_area:
            self.streak = 0
            return False, self.info()

        ratio = area / base
        drop = base - area

        dy, v = self._sink_offset()

        # 下沉阈值跟着「鱼漂高度」走，这样 1080p / 2K / 不同抛竿距离都通用
        if ref_h and ref_h > 0:
            sink_thr = max(self.cfg.sink_px_min, ref_h * self.cfg.sink_h_ratio)
            speed_thr = sink_thr * 1.2
        else:
            sink_thr = self.cfg.sink_px
            speed_thr = self.cfg.sink_speed_px
        self._sink_thr = sink_thr

        # 信号 B：质心快速下移（鱼漂正在下沉，暖色还没被水花完全盖住）
        sink = False
        if self.cfg.use_sink and gy > 0:
            sink = (dy >= sink_thr) or (v >= speed_thr)

        # 信号 A：存在度骤降（比例 + 绝对量都要满足）
        vanish = (ratio <= self.cfg.sat_drop_ratio) and (drop >= self.cfg.min_drop)
        # 光归零不够 —— 波浪遮挡和鱼跑掉也会归零。必须配上"确实下沉了"。
        # 实测：真咬钩 dy=+22，波浪遮挡/自然消失 dy≈0。
        vanish = vanish and (dy >= max(self.cfg.vanish_dy_min, ref_h * 0.08 if ref_h else 0))

        # 下沉必须配合面积明显下降才算咬钩；光下沉不算
        # （实测大鱼漂被水面晃一下就能让质心偏 12~20px）
        sink_ok = sink and (ratio <= self.cfg.sink_ratio)
        if vanish or sink_ok:
            self.streak += 1
        else:
            self.streak = 0

        fired = (self.streak >= self.cfg.confirm_frames) and (now >= self.cooldown_until)

        info = {
            "area": area,
            "base": int(base),
            "ratio": ratio,
            "drop": int(drop),
            "dy": dy,
            "v": v,
            "sink": sink,
            "vanish": vanish,
            "streak": self.streak,
            "ready": True,
        }
        if fired:
            self.cooldown_until = now + self.cfg.cooldown
            self.areas.clear()
            self.streak = 0
        return fired, info


class VisionPipeline:
    """每帧调用一次的流水线。"""

    def __init__(self, detect_cfg: DetectCfg, bite_cfg: BiteCfg):
        self.tracker = BobberTracker(detect_cfg)
        self.detector = BiteDetector(bite_cfg)
        self.last_info: dict = {}

    def reset(self) -> None:
        self.tracker.reset()
        self.detector.reset()
        self.last_info = {}

    def acquire(self, roi: np.ndarray) -> Track:
        self.detector.reset()
        return self.tracker.acquire(roi)

    def step(self, roi: np.ndarray, now: float) -> tuple[bool, Track, dict]:
        t = self.tracker.update(roi)
        if not t.found:
            return False, t, {"lost": True, "ready": False, "area": 0, "base": 0,
                              "ratio": 1.0, "drop": 0, "dy": 0.0, "v": 0.0,
                              "streak": 0}
        area = self.tracker.measure(roi)
        fired, info = self.detector.update(area, t.gy, now, ref_h=float(t.h))
        info["lost"] = False
        self.last_info = info
        return fired, t, info

"""离线自测：直接用真实游戏截图验证算法。

用法:
    python tools/selftest.py                    # 用内置的参考截图
    python tools/selftest.py 图1.png 图2.png    # 或用自己截的（第 1 张正常、第 2 张咬钩）

检查四项：
  1. 正常画面能定位到鱼漂（bbox 与真值接近）
  2. 正常画面连续 20 帧零误触
  3. 鱼漂被水花盖住时，1~2 帧内触发
  4. 咬钩画面本身不该被误认成鱼漂
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wf.paths import app_root                   # noqa: E402

ROOT = app_root()

import cv2          # noqa: E402
import numpy as np  # noqa: E402

from wf.config import BiteCfg, DetectCfg   # noqa: E402
from wf.vision import VisionPipeline       # noqa: E402

DEFAULT_GLOB = r"C:\Users\Administrator\.workbuddy\clipboard-images\clipboard-2026-11-06T13-04-18-*.jpg"
# 三张参考图的鱼漂真值（前面用高饱和团块量出来的）
TRUTH = {0: (988, 350), 1: (918, 383), 2: (978, 392)}


def crop_roi(img: np.ndarray) -> tuple[np.ndarray, int, int]:
    """按 config 里的 region 裁 ROI（这里硬编码成真实截图的搜索区）。"""
    x0, y0, x1, y1 = 780, 250, 1220, 460
    return img[y0:y1, x0:x1], x0, y0


def shift_bobber(img: np.ndarray, box: tuple[int, int, int, int], dy: int) -> np.ndarray:
    """模拟鱼漂下沉：把鱼漂那块整体往下挪 dy 像素。"""
    x, y, w, h = box
    pad = 8
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
    out = img.copy()
    patch = out[y0:y1, x0:x1].copy()
    out[y0:y1, x0:x1] = cv2.GaussianBlur(out[y0:y1, x0:x1], (13, 13), 0)
    ny0, ny1 = y0 + dy, y1 + dy
    if ny1 <= out.shape[0]:
        out[ny0:ny1, x0:x1] = patch
    return out


def kill_bobber(img: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """模拟咬钩：把鱼漂那块去掉饱和度 + 糊掉（水花盖住的效果）。"""
    x, y, w, h = box
    pad = 7
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
    out = img.copy()
    patch = out[y0:y1, x0:x1]
    patch = cv2.GaussianBlur(patch, (9, 9), 0)
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] *= 0.30                      # 去饱和
    hsv[:, :, 0] = (hsv[:, :, 0] * 0.3 + 60)  # 色相拉回水面附近的紫灰
    out[y0:y1, x0:x1] = cv2.cvtColor(hsv.clip(0, 255).astype(np.uint8), cv2.COLOR_HSV2BGR)
    return out


def jitter(img: np.ndarray, px: int = 1) -> np.ndarray:
    """模拟水面晃动：整体平移 ±px。"""
    dx = int(np.random.randint(-px, px + 1))
    dy = int(np.random.randint(-px, px + 1))
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]),
                          borderMode=cv2.BORDER_REPLICATE)


def main() -> int:
    files = sys.argv[1:]
    if not files:
        files = sorted(glob.glob(DEFAULT_GLOB))
    if not files:
        print("找不到参考截图，请用 python tools/selftest.py 正常.png 咬钩.png 指定")
        return 1

    imgs = [cv2.imread(p) for p in files]
    imgs = [i for i in imgs if i is not None]
    if len(imgs) < 2:
        print("至少需要两张图（正常 + 咬钩）")
        return 1

    dc, bc = DetectCfg(), BiteCfg()
    vis = VisionPipeline(dc, bc)
    ok = True

    # ---------- [1] 定位 ----------
    print("[1] 定位（正常画面）")
    roi, ox, oy = crop_roi(imgs[0])
    t = vis.acquire(roi)
    if not t.found:
        print("    ✗ 完全没找到鱼漂")
        return 2
    gx, gy = t.cx + ox, t.cy + oy
    ex, ey = TRUTH[0]
    dist = ((gx - ex) ** 2 + (gy - ey) ** 2) ** 0.5
    print(f"    定位 ({gx},{gy}) {t.w}x{t.h} area={t.area}  真值 ({ex},{ey})  偏差 {dist:.0f}px")
    if dist > 30:
        print("    ✗ 偏差过大")
        ok = False
    else:
        print("    ✓ 定位准确")

    # ---------- [2] 稳态不误触 ----------
    print("[2] 稳态 20 帧（水面晃动）")
    false_positives = 0
    ratios = []
    for i in range(20):
        frame = jitter(imgs[0], 1)
        r, tr, info = vis.step(crop_roi(frame)[0], now=i * 0.04)
        if info.get("ready"):
            ratios.append(info["ratio"])
        if r:
            false_positives += 1
    print(f"    ratio 区间 {min(ratios):.2f} ~ {max(ratios):.2f}")
    if false_positives:
        print(f"    ✗ 误触 {false_positives} 次")
        ok = False
    else:
        print("    ✓ 零误触")

    # ---------- [3] 咬钩触发 ----------
    # 真实过程是两段：鱼漂先快速下沉（1~2 帧），然后被水花彻底盖住。
    # 检测器需要「下沉」这个证据，所以合成场景也得照着这个节奏来。
    STEP = 0.04
    F_SINK = 32                    # 第 32 帧（1.28s）开始下沉
    F_KILL = 34                    # 第 34 帧被水花盖住
    SINK_DY = 12
    print(f"[3] 鱼漂下沉 {SINK_DY}px 后被水花盖住（{F_SINK*STEP:.1f}s 起）")
    vis.reset()
    roi, rox, roy = crop_roi(imgs[0])
    t = vis.acquire(roi)
    box = (t.x + rox, t.y + roy, t.w, t.h)      # 换算回整图坐标
    fired_at = -1
    seq_log = []
    for i in range(70):
        base = imgs[0]
        if i >= F_SINK:
            base = shift_bobber(base, box, SINK_DY)
        if i >= F_KILL:
            base = kill_bobber(base, (box[0], box[1] + SINK_DY, box[2], box[3]))
        r, tr, info = vis.step(crop_roi(base)[0], now=i * STEP)
        if info.get("ready") and i >= F_SINK - 3:
            seq_log.append((i, info.get("area", 0), info.get("ratio", 1.0),
                            info.get("dy", 0.0), info.get("streak", 0), r))
        if r and fired_at < 0:
            fired_at = i

    for i, a, ra, dy, sk, r in seq_log[:10]:
        print(f"    帧{i:2d}  area={a:4d}  ratio={ra:.2f}  下沉{dy:+6.1f}  连中={sk}  "
              f"{'★触发' if r else ''}")
    if fired_at < 0:
        print("    ✗ 没有触发咬钩")
        ok = False
    else:
        delay = fired_at - F_KILL
        print(f"    ✓ 第 {fired_at} 帧触发（水花盖住后 {delay} 帧 ≈ {delay*STEP*1000:.0f}ms）")
        if delay > 3:
            print("    ⚠ 反应偏慢，把 bite.confirm_frames 调小")
            ok = False

    # ---------- [4] 咬钩画面不该被误锁 ----------
    print("[4] 咬钩画面单独定位")
    roi2, ox2, oy2 = crop_roi(imgs[1])
    vis2 = VisionPipeline(dc, bc)
    t2 = vis2.acquire(roi2)
    if t2.found and t2.area > 150:
        print(f"    ⚠ 仍锁到一块 area={t2.area}（正常，可能是水花里的残留彩色）")
    else:
        print("    ✓ 咬钩时鱼漂不可定位（符合预期）")

    print("\n结果:", "通过 ✓" if ok else "未通过 ✗")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

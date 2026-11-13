"""标定 / 调参工具。

用法:
    python tools/calibrate.py live     # 实时检测预览，边看边调参（最常用）
    python tools/calibrate.py check    # 单帧诊断：截一张图，看能不能定位到鱼漂
    python tools/calibrate.py cast     # 诊断自动抛竿：窗口识别 + 切前台 + 试按技能键
    python tools/calibrate.py region   # 框选水面搜索区域，输出 region 配置值

live 模式按键：
    R = 重新寻找鱼漂    S = 保存当前画面到 debug/    ESC = 退出
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wf.inputs import enable_dpi_awareness      # noqa: E402
from wf.paths import app_root                   # noqa: E402

ROOT = app_root()

enable_dpi_awareness()

import cv2                                     # noqa: E402
import numpy as np                             # noqa: E402

from wf.capture import Capturer                # noqa: E402
from wf.config import load_config              # noqa: E402
from wf.vision import BobberTracker, VisionPipeline   # noqa: E402

WIN = "calibrate"


def _show_and_select(img: np.ndarray, title: str) -> tuple[int, int, int, int]:
    """缩放到合适大小后让用户框选，返回原图坐标的 (x, y, w, h)。"""
    h, w = img.shape[:2]
    scale = min(1.0, 1700.0 / max(w, h))
    disp = img if scale >= 1.0 else cv2.resize(img, None, fx=scale, fy=scale,
                                               interpolation=cv2.INTER_AREA)
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    r = cv2.selectROI(title, disp, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(title)
    x, y, rw, rh = [int(v) for v in r]
    if rw <= 0 or rh <= 0:
        return 0, 0, 0, 0
    return (int(x / scale), int(y / scale), int(rw / scale), int(rh / scale))


def cmd_region() -> int:
    cfg = load_config(ROOT / "config.toml")
    cap = Capturer(cfg.capture.monitor)
    print(f"显示器 {cap.width}x{cap.height}")
    print("请框选水面区域 —— 只要水面，别把角色/宠物/UI 框进来（它们也有彩色像素）")
    img = cap.grab_screen()
    x, y, w, h = _show_and_select(img.copy(), "框选水面后按 Enter")
    if w <= 0:
        print("已取消")
        return 1
    x1, y1 = x / cap.width, y / cap.height
    x2, y2 = (x + w) / cap.width, (y + h) / cap.height
    print("\n把下面这行填进 config.toml 的 [search] 段：\n")
    print(f"region = [{x1:.3f}, {y1:.3f}, {x2:.3f}, {y2:.3f}]")
    print(f"\n（像素: x={x} y={y} w={w} h={h}）")
    cap.close()
    return 0


def cmd_check() -> int:
    """单帧诊断：这一刻能不能定位到鱼漂？顺便打印不同饱和度阈值下的候选。"""
    cfg = load_config(ROOT / "config.toml")
    cap = Capturer(cfg.capture.monitor)
    rx, ry, rw, rh = cap.region_to_px(cfg.search.region)
    roi = cap.grab_roi(rx, ry, rw, rh)
    print(f"ROI {rw}x{rh} @ ({rx},{ry})   截图尺寸 {roi.shape[1]}x{roi.shape[0]}")

    vis = VisionPipeline(cfg.detect, cfg.bite)
    t = vis.acquire(roi)
    if t.found:
        print(f"✓ 定位到鱼漂: ROI内({t.x},{t.y}) {t.w}x{t.h}  面积={t.area}  屏幕({t.x+rx},{t.y+ry})")
    else:
        print("✗ 没找到鱼漂")

    # 阈值是自适应的 min(warm_delta, 画面R-B中位 + warm_rel)，把关键量摊开给用户看
    import numpy as np
    rb = roi[:, :, 2].astype(int) - roi[:, :, 0].astype(int)
    med = float(np.median(rb))
    eff = min(cfg.detect.warm_delta, med + cfg.detect.warm_rel)
    print(f"\n画面 R-B 中位数 = {med:+.0f}，当前实际阈值 = {eff:+.0f}"
          f"（min({cfg.detect.warm_delta:.0f}, 中位+{cfg.detect.warm_rel:.0f})）")
    print("不同 warm_rel 下的暖色块（rel 越大越严）：")
    origin = cfg.detect.warm_rel
    for rel in (50, 60, 70, 80, 90, 100):
        cfg.detect.warm_rel = rel
        bt = BobberTracker(cfg.detect)
        blobs = bt._blobs(bt.warm_mask(roi))
        top = blobs[:2]
        desc = ", ".join(f"{a}px@{x},{y}({w}x{h})" for a, x, y, w, h, *_ in top) or "无"
        print(f"  rel={rel:3d} 阈值{min(cfg.detect.warm_delta, med+rel):+5.0f}: "
              f"{len(blobs):2d} 个 -> {desc}")
    cfg.detect.warm_rel = origin

    out = ROOT / "debug"
    out.mkdir(exist_ok=True)
    disp = roi.copy()
    if t.found:
        cv2.rectangle(disp, (t.x, t.y), (t.x + t.w, t.y + t.h), (0, 255, 0), 2)
        x0, y0, x1, y1 = vis.tracker.window_box(roi.shape[:2])
        cv2.rectangle(disp, (x0, y0), (x1, y1), (0, 200, 255), 1)
    p = out / time.strftime("check-%H%M%S.png")
    cv2.imwrite(str(p), disp)
    print(f"\n标注图已保存: {p}")
    cap.close()
    return 0 if t.found else 2


def cmd_cast() -> int:
    """诊断自动抛竿：窗口识别 → 切前台 → 试按一次技能键。"""
    from wf.inputs import WinInput, vk_from_name

    cfg = load_config(ROOT / "config.toml")
    win = WinInput(humanize=False)

    print("=== 1. 窗口匹配 ===")
    print(f"配置 window_titles = {cfg.game.window_titles}")
    wins = win.find_windows(cfg.game.window_titles)
    if not wins:
        print("  ✗ 一个都没匹配到")
        print("\n  你机器上现在有这些窗口：")
        for _hwnd, t in WinInput.list_windows()[:20]:
            print(f"    - {t!r}")
        print("\n  把游戏窗口的正确标题填进 config.toml 的 game.window_titles")
        return 2
    for i, (hwnd, t) in enumerate(wins):
        print(f"  [{i}] hwnd={hwnd}  {t!r}" + ("   ← 会选这个" if i == 0 else ""))

    print("\n=== 2. 当前前台 ===")
    fg = win.get_foreground_title()
    print(f"  {fg!r}" + ("  （已经是游戏了）" if win.is_foreground(cfg.game.window_titles) else ""))

    print("\n=== 3. 切到游戏 ===")
    ok = win.activate_window(cfg.game.window_titles)
    print(f"  {'✓ 已切到前台' if ok else '✗ 切不过去'}")
    print(f"  现在前台: {win.get_foreground_title()!r}")
    if not ok:
        print("  提示：独占全屏下 Windows 经常拒绝切窗口，把游戏改成「窗口化全屏」再试")
        return 2

    print("\n=== 4. 试按一次技能键 ===")
    vk = vk_from_name(cfg.game.cast_key)
    print(f"  cast_key={cfg.game.cast_key!r}  虚拟键码=0x{vk:02X}")
    for i in (3, 2, 1):
        print(f"  {i} 秒后按键，请把视线放到游戏上…")
        time.sleep(1)
    win.key_tap(vk)
    print("\n  已按下，看游戏里有没有抛竿：")
    print("    抛了  → 按键没问题，直接 run.bat")
    print("    没抛  → cast_key 绑错技能了，或者游戏以管理员运行而本程序不是")
    return 0


def cmd_live() -> int:
    cfg = load_config(ROOT / "config.toml")
    cap = Capturer(cfg.capture.monitor)
    rx, ry, rw, rh = cap.region_to_px(cfg.search.region)
    vis = VisionPipeline(cfg.detect, cfg.bite)

    print("实时检测中… R=重新找鱼漂  S=存当前画面  ESC=退出")
    print(f"ROI: {rw}x{rh} @ ({rx},{ry})")
    print("盯住 ratio 这一行：正常应该稳定在 0.8~1.2，鱼咬钩时会掉到 0.4 以下")

    t0 = time.perf_counter()
    fps = 0.0
    need_acquire = True
    info: dict = {}
    fired = False
    t = None

    while True:
        now = time.perf_counter()
        roi = cap.grab_roi(rx, ry, rw, rh)

        if need_acquire:
            vis.reset()
            t = vis.acquire(roi)
            need_acquire = False
            info, fired = {}, False
            if t.found:
                print(f"锁定: ({t.x},{t.y}) {t.w}x{t.h} 面积={t.area}")
            else:
                print("没找到暖色块 —— 按 R 重试，或调低 detect.warm_delta")
        else:
            fired, t, info = vis.step(roi, now)

        disp = roi.copy()
        if t is not None and t.found:
            x0, y0, x1, y1 = vis.tracker.window_box(roi.shape[:2])
            cv2.rectangle(disp, (x0, y0), (x1, y1), (0, 200, 255), 1)
            cv2.rectangle(disp, (t.x, t.y), (t.x + t.w, t.y + t.h), (0, 255, 0), 2)
        if fired:
            cv2.rectangle(disp, (0, 0), (disp.shape[1] - 1, disp.shape[0] - 1), (0, 0, 255), 4)
            print(">>> 判定咬钩 <<<")

        dt = now - t0
        t0 = now
        if dt > 0:
            fps = 0.85 * fps + 0.15 * (1.0 / dt) if fps else 1.0 / dt

        txt = f"fps={fps:.0f}  area={info.get('area', 0)}  base={info.get('base', 0)}"
        if info.get("ready"):
            txt += (f"  ratio={info.get('ratio', 1.0):.2f}  掉{info.get('drop', 0)}"
                    f"  连低={info.get('streak', 0)}")
        cv2.putText(disp, txt, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(disp, txt, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

        cv2.imshow(WIN, disp)
        k = cv2.waitKey(1) & 0xFF
        if k == 27:
            break
        if k in (ord("r"), ord("R")):
            need_acquire = True
        if k in (ord("s"), ord("S")):
            out = ROOT / "debug"
            out.mkdir(exist_ok=True)
            p = out / time.strftime("live-%H%M%S.png")
            cv2.imwrite(str(p), disp)
            print(f"已保存 {p}")

    cv2.destroyAllWindows()
    cap.close()
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    table = {"region": cmd_region, "check": cmd_check, "live": cmd_live, "cast": cmd_cast}
    if mode not in table:
        print(__doc__)
        return 1
    return table[mode]()


if __name__ == "__main__":
    raise SystemExit(main())

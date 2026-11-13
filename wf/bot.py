"""钓鱼主控：状态机 + 统计 + 调试可视化。"""
from __future__ import annotations

import logging
import random
import time
from collections import deque

import cv2
import numpy as np

from .capture import Capturer
from .config import Config
from .inputs import WinInput, vk_from_name
from .vision import Track, VisionPipeline

# 状态
S_IDLE = "idle"
S_CAST = "cast"
S_SETTLE = "settle"
S_ACQUIRE = "acquire"
S_TRACK = "track"
S_CLICK = "click"
S_RECOVER = "recover"

STATE_LABEL = {
    S_IDLE: "待命",
    S_CAST: "抛竿",
    S_SETTLE: "鱼漂淡入中",
    S_ACQUIRE: "寻找鱼漂",
    S_TRACK: "盯漂中",
    S_CLICK: "扬竿",
    S_RECOVER: "收杆等待",
}


_OCR_ENGINE = None       # RapidOCR 引擎，懒加载后常驻


class FishingBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cap = Capturer(cfg.capture.monitor)
        self.win = WinInput(
            humanize=cfg.humanize.enabled,
            jitter_px=cfg.humanize.click_jitter_px,
            hold_ms=cfg.humanize.press_hold_ms,
        )
        self.vis = VisionPipeline(cfg.detect, cfg.bite)

        self.roi_x, self.roi_y, self.roi_w, self.roi_h = self.cap.region_to_px(cfg.search.region)
        self.cast_vk = vk_from_name(cfg.game.cast_key)

        self.state = S_IDLE
        self.t_state = 0.0
        self.enabled = False
        self.frame: np.ndarray | None = None
        self.track = Track(found=False)
        self.info: dict = {}
        self.message = "按 F8 开始"

        self._pos_hist: deque[tuple[int, int]] = deque(maxlen=9)
        self._last_preview = 0.0
        self._last_idle_grab = 0.0
        self._wait_start = 0.0
        self._cast_time = 0.0
        self._fail_shots = 0
        # 结果核对：扬竿瞬间存一张全屏，等提示文字出来再判定对错
        self.verify_box = self._region_to_screen_px(cfg.verify.region)
        self._pending_shot: np.ndarray | None = None
        self._pending_cast = 0
        self._check_at = 0.0
        self._check_done = True
        self.log = logging.getLogger("bot")

        self.stats = {
            "casts": 0, "bites": 0, "timeouts": 0, "lost": 0, "no_bobber": 0,
            "caught": 0, "wrong": 0,
            "fg_fail": 0, "fg_fail_streak": 0,
            "start": 0.0, "waits": deque(maxlen=50),
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.enabled = True
        self.stats["start"] = time.perf_counter()
        self._goto(S_CAST, time.perf_counter(), 0.0)
        self.message = "准备抛竿"
        self._dump_roi_view()

    def stop(self, msg: str = "已暂停") -> None:
        self.enabled = False
        self.state = S_IDLE
        self.message = msg
        self.vis.reset()

    def close(self) -> None:
        self.cap.close()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def _dump_roi_view(self) -> None:
        """开局存一张全屏图，把搜索区域框出来 —— ROI 框错了是最常见的坑。"""
        try:
            full = self.cap.grab_screen()
            cv2.rectangle(full, (self.roi_x, self.roi_y),
                          (self.roi_x + self.roi_w, self.roi_y + self.roi_h),
                          (0, 255, 0), 3)
            p = self.cfg.root / "debug" / "roi-view.png"
            p.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(p), full)
            self.log.info("搜索区域示意图（绿框）: %s", p)
        except Exception as e:                   # noqa: BLE001
            self.log.debug("存 ROI 示意图失败: %s", e)

    def _ocr_text(self, img: np.ndarray) -> str:
        """识别一小块图里的文字（RapidOCR，纯 ONNX，离线）。

        文字只有十几像素高，直接喂给 OCR 基本认不出，先放大 3 倍。
        首次调用才加载模型（约 1 秒），之后常驻。
        """
        try:
            global _OCR_ENGINE
            if _OCR_ENGINE is None:
                from rapidocr_onnxruntime import RapidOCR
                _OCR_ENGINE = RapidOCR()
                logging.getLogger("bot").info("文字识别模型已加载")
            big = cv2.resize(img, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
            res, _ = _OCR_ENGINE(big)
            if not res:
                return ""
            return "".join(str(x[1]) for x in res)
        except Exception as e:                    # noqa: BLE001
            logging.getLogger("bot").debug("OCR 失败: %s", e)
            return ""

    def _analyze_result_text(self, img: np.ndarray) -> tuple[str, dict]:
        """读屏幕上方那行提示，判断这次扬竿到底对不对。

        三种结果（提示都是黄字，颜色分不开）：
          · 「你的鱼逃走了！」 → 确实有咬钩，判定**正确**，只是这次没钓上来（钓鱼有成功率）
          · 「没有鱼上钩」     → 当时根本没咬钩，判定**错误**
          · 无文字            → 钓上来了

        用 OCR 认字，而不是数笔画/量宽度 —— 后者在不同分辨率、UI 缩放、
        以及出现别的提示语时会全部失效。
        """
        if img is None or img.size == 0:
            return "none", {"text": ""}

        text = self._ocr_text(img)
        info = {"text": text}

        if not text:
            # OCR 没认出来：再用黄字像素兜底判断"到底有没有字"
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            h = hsv[:, :, 0].astype(int)
            s = hsv[:, :, 1].astype(int)
            v = hsv[:, :, 2].astype(int)
            yellow = (h >= 15) & (h <= 45) & (v > 140) & (s > 70)
            info["pixels"] = int(yellow.sum())
            return ("none" if info["pixels"] < self.cfg.verify.min_bright else "unknown"), info

        # 关键词匹配（比整句相等宽容，识别偶尔会漏字或加标点）
        if "逃" in text:
            return "escape", info
        if ("没" in text and "钩" in text) or "没有鱼" in text or "上钩" in text:
            return "nobite", info
        return "unknown", info

    def _do_result_check(self) -> None:
        """扬竿约 1 秒后，单独截屏幕顶部那行提示去做 OCR。

        为什么要单独截：
          · 提示文字出现在画面**最上方**，根本不在「搜索区域」里（那里只有水面）
          · 扬竿瞬间那张图里文字还没出来，拿它识别永远是空的
        只有判定错误（「没有鱼上钩」）才把现场图留下来。
        """
        shot = self._pending_shot
        self._pending_shot = None
        x, y, w, h = self.verify_box
        try:
            reg = self.cap.grab_roi(x, y, w, h)
        except Exception as e:                    # noqa: BLE001
            self.log.debug("抓提示区域失败：%s", e)
            return

        kind, info = self._analyze_result_text(reg)
        text = info.get("text", "")

        if kind == "none":
            self.stats["caught"] += 1
            self.log.info("结果核对：没有提示文字 → 钓上来了 ✓")
            return
        if kind in ("escape", "unknown"):
            # escape = 确实有鱼咬钩（只是没钓上来）；unknown = 认出来了但不是已知那两句，
            # 都当"判定正确"处理，避免误删有用的现场
            self.log.info("结果核对：%s → 判定正确", text or "(未识别)")
            return

        self.stats["wrong"] += 1
        self.log.warning("结果核对：%s → 判定错误！", text or "没有鱼上钩")
        if shot is None:
            return
        try:
            d = self.cfg.root / "debug" / "wrong"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"wrong-{time.strftime('%H%M%S')}-cast{self._pending_cast}.png"
            cv2.imwrite(str(path), shot)
            self.log.warning("  现场已存 debug/wrong/%s", path.name)
        except Exception as e:                    # noqa: BLE001
            self.log.warning("  现场存图失败：%s", e)

    def _save_fail_snapshot(self, roi: np.ndarray) -> None:
        """找鱼漂失败时留一张证据图，方便判断是抛到区域外还是没显形。"""
        if self._fail_shots >= 5:
            return
        self._fail_shots += 1
        try:
            p = self.cfg.root / "debug" / time.strftime("find-fail-%H%M%S.png")
            p.parent.mkdir(exist_ok=True)
            cv2.imwrite(str(p), roi)
            self.log.warning("找漂失败画面已存: %s（看鱼漂在不在这个框里）", p)
        except Exception:
            pass
    def _goto(self, state: str, now: float, wait: float) -> None:
        self.state = state
        self.t_state = now + wait

    def _grab_roi(self) -> np.ndarray:
        self.frame = self.cap.grab_roi(self.roi_x, self.roi_y, self.roi_w, self.roi_h)
        return self.frame

    def _to_screen(self, x: int, y: int) -> tuple[int, int]:
        return self.cap.left + self.roi_x + x, self.cap.top + self.roi_y + y

    def _region_to_screen_px(self, region) -> tuple[int, int, int, int]:
        """把 0~1 的屏幕比例区域换算成 (x, y, w, h) 屏幕像素。"""
        x1, y1, x2, y2 = region
        w, h = self.cap.width, self.cap.height
        x = int(round(min(x1, x2) * w))
        y = int(round(min(y1, y2) * h))
        return x, y, max(8, int(round(abs(x2 - x1) * w))), max(8, int(round(abs(y2 - y1) * h)))

    def _cast(self, now: float) -> None:
        # 关键：按键只能发给游戏窗口。发错窗口不但抛不了竿，还可能误操作别的程序
        if self.cfg.game.activate_window:
            if not self.win.activate_window(self.cfg.game.window_titles):
                self._foreground_fail(now)
                return
        elif not self.win.is_foreground(self.cfg.game.window_titles):
            self._foreground_fail(now)
            return

        self.win.key_tap(self.cast_vk)
        self.stats["casts"] += 1
        self.stats["fg_fail_streak"] = 0
        self._cast_time = now
        self.vis.reset()
        self._pos_hist.clear()
        self.track = Track(found=False)
        self._goto(S_SETTLE, now, self.cfg.timing.cast_settle)
        self.message = "鱼漂淡入中…（等 %.1fs）" % self.cfg.timing.cast_settle
        self.log.info("抛竿 #%d", self.stats["casts"])

    def _foreground_fail(self, now: float) -> None:
        """切不到游戏前台就绝不按键，1 秒后重试；连错 5 次直接停。"""
        self.stats["fg_fail"] += 1
        self.stats["fg_fail_streak"] += 1
        fg = self.win.get_foreground_title()
        self.message = (f"⚠ 切不到游戏窗口（当前前台：{fg or '无标题'}）"
                        f" 第 {self.stats['fg_fail_streak']} 次")
        if self.stats["fg_fail_streak"] >= 5:
            self.stop("连续 5 次切不到游戏窗口 —— 请检查 config.toml 的 "
                      "game.window_titles 和游戏显示模式，然后按 F8 重来")
            return
        self.state = S_CAST
        self.t_state = now + 1.0

    # ------------------------------------------------------------------ #
    # 主循环的一步
    # ------------------------------------------------------------------ #
    def step(self, now: float) -> None:
        if not self.enabled:
            # 待命时低频抓一帧，让预览窗口有画面，不至于看着像程序没跑
            if self.frame is None or now - self._last_idle_grab > 0.5:
                self._last_idle_grab = now
                try:
                    self._grab_roi()
                except Exception:
                    pass
            return

        st = self.state

        if st == S_CAST:
            if now < self.t_state:
                return
            self._cast(now)
            return

        if st == S_SETTLE:
            if now < self.t_state:
                return
            self._goto(S_ACQUIRE, now, self.cfg.timing.acquire_timeout)
            self.message = "寻找鱼漂…"
            return

        if st == S_ACQUIRE:
            roi = self._grab_roi()
            t = self.vis.acquire(roi)
            if t.found:
                self.track = t
                self._pos_hist.clear()
                self._goto(S_TRACK, now, self.cfg.timing.bite_timeout)
                self.message = f"已锁定鱼漂 {t.w}x{t.h}"
                self._wait_start = time.perf_counter()
                self.log.info("锁定鱼漂 %dx%d 面积=%d（抛竿后 %.1fs），开始等咬钩 %.0fs",
                              t.w, t.h, t.area, now - self._cast_time,
                              self.cfg.timing.bite_timeout)
                return
            if now >= self.t_state:
                self.stats["no_bobber"] += 1
                self.log.warning("找鱼漂超时（抛竿后 %.1fs 仍没找到）→ 收杆重抛",
                                 now - self._cast_time)
                self._save_fail_snapshot(roi)
                # 鱼漂很可能还在水里，这时直接按技能键只会「取消钓鱼」，
                # 必须先把这一竿收掉，否则永远在取消/抛竿之间来回
                cx = self.roi_x + self.roi_w // 2
                cy = self.roi_y + self.roi_h // 2
                self.win.click(self.cap.left + cx, self.cap.top + cy, button="right")
                self.message = "没找到鱼漂，收杆重抛"
                self._goto(S_CAST, now, self.cfg.timing.retry_delay)
            return

        if st == S_TRACK:
            roi = self._grab_roi()
            fired, t, info = self.vis.step(roi, now)
            self.track, self.info = t, info

            if t.found and info.get("ready"):
                self._pos_hist.append(t.center)

            if fired:
                # 判定到咬钩后不立刻点，先愣一下 —— 真人反应也要 200~500ms，
                # 而且实测延迟两秒内点都能钓到，不影响成功率
                delay = random.uniform(self.cfg.humanize.react_delay_min,
                                       self.cfg.humanize.react_delay_max)
                self.log.info("判定咬钩 (面积=%s 基线=%s ratio=%.2f 下沉%+.1fpx %s) → %.0fms 后扬竿",
                              info.get("area"), info.get("base"),
                              info.get("ratio", 0), info.get("dy", 0.0),
                              "下沉" if info.get("sink") else "归零",
                              delay * 1000)
                self._goto(S_CLICK, now, delay)
                return

            if self.vis.tracker.lost >= self.cfg.detect.lost_frames:
                self.stats["lost"] += 1
                self.log.warning("鱼漂连续 %d 帧找不到 → 重抛", self.vis.tracker.lost)
                self.message = "鱼漂丢了，重抛"
                self._goto(S_CAST, now, 0.0)
                return

            if now >= self.t_state:
                self.stats["timeouts"] += 1
                # 一般来说一定有鱼咬钩，等满超时说明判定漏了 —— 也算误判
                self.stats["wrong"] += 1
                self.log.info("等咬钩超时 %.1fs（计入误判），收杆重抛",
                              now - (self.t_state - self.cfg.timing.bite_timeout))
                # 先右键点一下鱼漂把这一竿收掉，否则接着按技能键只会「取消钓鱼」，
                # 永远抛不出去
                if self.track.found:
                    sx, sy = self._to_screen(*self.track.center)
                    self.win.click(sx, sy, button="right")
                self.message = f"{self.cfg.timing.bite_timeout:.0f} 秒没咬钩，收杆重抛"
                self._goto(S_CAST, now, 0.0)
            return

        if st == S_CLICK:
            # 反应延迟还没到，先等着（这段期间不抓图、不动鼠标）
            if now < self.t_state:
                return

            # 用咬钩前若干帧的稳定位置（中位数）来点，比用当下位置稳
            if self.track.found and len(self._pos_hist) >= 3:
                xs = sorted(p[0] for p in self._pos_hist)
                ys = sorted(p[1] for p in self._pos_hist)
                cx, cy = xs[len(xs) // 2], ys[len(ys) // 2]
            elif self.track.found:
                cx, cy = self.track.center
            else:
                self._goto(S_CAST, now, 0.0)
                return

            sx, sy = self._to_screen(cx, cy)
            self.win.click(sx, sy, button="right")     # 咬钩要右键点鱼漂
            self.stats["bites"] += 1
            if self._wait_start:
                self.stats["waits"].append(time.perf_counter() - self._wait_start)
                self._wait_start = 0.0

            # 扬竿瞬间留一张全屏 —— 只用来记录"判定错误"的现场。
            # 注意：这会儿提示文字还没出来，不能拿它去 OCR。
            if self.cfg.verify.enabled:
                try:
                    self._pending_shot = self.cap.grab_screen().copy()
                    self._pending_cast = self.stats["casts"]
                except Exception:
                    self._pending_shot = None
            self._check_at = now + self.cfg.verify.delay
            self._check_done = False

            self.message = "咬钩！已右键扬竿"
            wait = random.uniform(self.cfg.timing.after_click_min,
                                  self.cfg.timing.after_click_max)
            # 这段等待里不再抓图盯漂，鼠标也不会乱动
            self._goto(S_RECOVER, now, wait)
            return

        if st == S_RECOVER:
            # 提示文字过 1 秒左右才出现，到点单独截屏幕顶部去 OCR
            if (self.cfg.verify.enabled and not self._check_done
                    and now >= self._check_at):
                self._check_done = True
                self._do_result_check()
            if now >= self.t_state:
                self._goto(S_CAST, now, self.cfg.timing.retry_delay)
            return

    # ------------------------------------------------------------------ #
    # 预览
    # ------------------------------------------------------------------ #
    def render(self) -> np.ndarray | None:
        if self.frame is None:
            return None
        img = self.frame.copy()
        t = self.track

        if t.found:
            # 判定窗口（黄框）= 每帧统计高饱和像素的地盘
            x0, y0, x1, y1 = self.vis.tracker.window_box(img.shape[:2])
            cv2.rectangle(img, (x0, y0), (x1, y1), (0, 200, 255), 1)
            cv2.rectangle(img, (t.x, t.y), (t.x + t.w, t.y + t.h), (0, 255, 0), 2)
            cx, cy = t.center
            cv2.drawMarker(img, (cx, cy), (0, 0, 255), cv2.MARKER_CROSS, 10, 1)

        lines = [f"{STATE_LABEL.get(self.state, self.state)}   {self.message}"]
        if self.state == S_TRACK and self._wait_start:
            waiting = time.perf_counter() - self._wait_start
            lines.append(f"盯漂 {waiting:.1f}s / {self.cfg.timing.bite_timeout:.0f}s"
                         f"   （超时才收杆重抛）")
        if self.info.get("ready"):
            lines.append("彩色像素 area={} base={} ratio={:.2f} 掉{} 连低={}".format(
                self.info.get("area", 0), self.info.get("base", 0),
                self.info.get("ratio", 1.0), self.info.get("drop", 0),
                self.info.get("streak", 0)))

        s = self.stats
        run = time.perf_counter() - s["start"] if s["start"] else 0
        lines.append(f"竿{s['casts']} 咬{s['bites']} 等鱼超时{s['timeouts']} "
                     f"找漂失败{s['no_bobber']} 丢漂{s['lost']} {run/60:.0f}min")

        y = 20
        for i, line in enumerate(lines):
            color = (0, 255, 255) if i == 0 else (215, 215, 215)
            cv2.putText(img, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(img, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)
            y += 22

        return img

    def show_preview(self, now: float) -> bool:
        """返回 False 表示用户关掉了窗口。"""
        if not self.cfg.ui.preview:
            return True
        if now - self._last_preview < 1 / 20:
            return True
        self._last_preview = now
        img = self.render()
        if img is None:
            return True
        sc = max(0.2, min(1.0, self.cfg.ui.preview_scale))
        disp = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        cv2.imshow("WoW Fishing - 调试预览 (F8 开始/暂停, F9 退出)", disp)
        key = cv2.waitKey(1) & 0xFF
        return key != 27

    def fps_sleep(self, t0: float) -> None:
        limit = self.cfg.capture.fps_limit
        if limit and limit > 0:
            dt = 1.0 / limit - (time.perf_counter() - t0)
            if dt > 0:
                time.sleep(dt)
        else:
            time.sleep(0.001)

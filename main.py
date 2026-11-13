"""入口：命令行 + 全局热键 + 主循环。

用法:
    WowFishing.exe                              开始钓鱼（默认开调试预览）
    WowFishing.exe --no-preview                 关掉预览，省 CPU
    WowFishing.exe --duration 300               跑 5 分钟后自动退出
    WowFishing.exe --autostart                  启动即开钓，不用按 F8
    WowFishing.exe calibrate [live|check|cast|region]   调参工具
    WowFishing.exe overlay                      屏幕上叠加显示捕捉范围
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wf.config import load_config                       # noqa: E402
from wf.paths import app_root                           # noqa: E402

# 打包成 exe 后 __file__ 不再指向项目目录，必须用 app_root()
ROOT = app_root()

from wf.inputs import enable_dpi_awareness              # noqa: E402

enable_dpi_awareness()                                   # 必须在任何截图之前

import cv2                                              # noqa: E402
from pynput import keyboard                             # noqa: E402

from wf.bot import FishingBot                           # noqa: E402


# --------------------------------------------------------------------------- #
# 控制台编码
# --------------------------------------------------------------------------- #
def fix_console_encoding() -> None:
    """让输出编码跟随「当前控制台代码页」。

    中文 Windows 的 cmd 是 GBK(936)，Windows Terminal 常是 UTF-8(65001)。
    写死任何一种，另一种环境必乱码。
    """
    if sys.platform != "win32":
        return
    import io
    cp = 0
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        pass
    if not cp:
        return
    enc = {936: "gbk", 54936: "gb18030", 950: "big5", 932: "shift_jis",
           65001: "utf-8"}.get(cp, "gbk")
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding=enc,
                                      errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding=enc,
                                      errors="replace", line_buffering=True)
    except Exception:
        pass
    try:
        print(f"[enc] console cp={cp} -> stdout={getattr(sys.stdout, 'encoding', '?')}",
              flush=True)
    except Exception:
        pass


class _ConsoleHandler(logging.Handler):
    """直接把字节写到文件描述符 1，绕开 Python 的文本层。

    走过的弯路（都是真踩过的）：
      · logging.StreamHandler 创建时把 stream 对象固定住，拿不到后来替换的 stdout
      · logging.basicConfig 在 root 已有 handler 时被静默忽略
      · reconfigure / TextIOWrapper 在 PyInstaller 下行为不一致
    最后结论：别经过任何文本层，自己编码、自己 write(1, bytes)。
    """

    def emit(self, record):
        try:
            line = self.format(record) + "\n"
            enc = getattr(sys.stdout, "encoding", None) or "gbk"
            os.write(1, line.encode(enc, "replace"))
        except Exception:
            pass


def setup_logging(verbose: bool = False) -> None:
    """强行接管 root logger，不用 basicConfig —— 它在 root 已有 handler 时会被忽略。"""
    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s")

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = _ConsoleHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    fh = logging.FileHandler(logdir / f"fishing-{stamp}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    logging.getLogger("PIL").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# 崩溃兜底
# --------------------------------------------------------------------------- #
def _crash_report() -> None:
    """双击运行时闪退是看不到错误的，所以写文件 + 弹窗 + 停住窗口。"""
    import traceback
    tb = traceback.format_exc()
    shown = tb.strip().splitlines()[-6:]
    msg = "程序启动失败：\n\n" + "\n".join(shown) + "\n\n完整信息见 error.log"
    try:
        (ROOT / "error.log").write_text(tb, encoding="utf-8")
    except Exception:
        pass
    try:
        print(tb, flush=True)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "Pixel Sentinel — 启动失败", 0x10)
    except Exception:
        pass
    try:
        input("\n按回车键退出…")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
def main() -> int:
    fix_console_encoding()

    # 打包成单个 exe 后，用子命令区分三个入口，避免重复打包依赖
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ("calibrate", "cal"):
            sys.argv = [sys.argv[0]] + sys.argv[2:]
            from tools.calibrate import main as cal_main
            return cal_main()
        if cmd in ("overlay", "ovl", "roi"):
            from tools.roi_overlay import main as ovl_main
            return ovl_main()

    ap = argparse.ArgumentParser(description="Pixel Sentinel — screen vision trigger")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--no-preview", action="store_true", help="关闭调试预览窗口")
    ap.add_argument("--duration", type=float, default=0, help="运行多少秒后自动停止，0=不限")
    ap.add_argument("--once", action="store_true", help="只跑一轮就停")
    ap.add_argument("--autostart", action="store_true", help="启动后自动开钓，无需按 F8")
    ap.add_argument("--no-gui", action="store_true", help="不开窗口，用原来的控制台模式")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    log = logging.getLogger("main")

    # 默认走图形界面：Qt 内部统一 UTF-8，彻底避开控制台编码这一摊事。
    # 加 --no-gui 或 GUI 起不来时才退回控制台。
    if not args.no_gui:
        try:
            from wf.gui import run_gui
            return run_gui(auto_start=args.autostart)
        except Exception as e:                    # noqa: BLE001
            log.error("图形界面启动失败（%s），改用控制台模式", e)

    cfg = load_config(args.config)
    if args.no_preview:
        cfg.ui.preview = False

    try:
        bot = FishingBot(cfg)
    except Exception as e:                       # noqa: BLE001
        log.error("初始化失败: %s", e)
        return 1

    log.info("显示器 %dx%d @ (%d,%d) | ROI %dx%d @ (%d,%d)",
             bot.cap.width, bot.cap.height, bot.cap.left, bot.cap.top,
             bot.roi_w, bot.roi_h, bot.roi_x, bot.roi_y)

    # 自检：独占全屏下 GDI 截图会拿到全黑
    try:
        probe = bot.cap.grab_screen()
        if float(probe.std()) < 3.0:
            log.warning("截图几乎是纯色（std=%.2f）。多半是游戏用了「独占全屏」，"
                        "请改成「窗口化全屏」。", float(probe.std()))
    except Exception as e:                       # noqa: BLE001
        log.warning("试截图失败: %s", e)

    flags = {"quit": False, "shot": False}

    def toggle():
        if bot.enabled:
            bot.stop("已暂停（F8 继续）")
            log.info("暂停 | 竿%d 咬%d", bot.stats["casts"], bot.stats["bites"])
        else:
            bot.start()
            log.info("开始钓鱼")

    def quit_():
        flags["quit"] = True

    def shot():
        flags["shot"] = True

    hk = keyboard.GlobalHotKeys({
        "<f8>": toggle,
        "<f9>": quit_,
        "<f7>": shot,
    })
    hk.start()

    log.info("F8 开始/暂停 · F7 保存当前画面 · F9 退出")
    if args.autostart:
        bot.start()

    t_begin = time.perf_counter()
    try:
        while not flags["quit"]:
            t0 = time.perf_counter()
            now = t0

            if args.duration and now - t_begin >= args.duration:
                log.info("达到设定时长，退出")
                break
            if args.once and bot.stats["bites"] > 0:
                log.info("已完成一轮，退出")
                break

            bot.step(now)

            if flags["shot"]:
                flags["shot"] = False
                dbg = ROOT / "debug"
                dbg.mkdir(exist_ok=True)
                if bot.frame is not None:
                    p = dbg / time.strftime("shot-%H%M%S.png")
                    cv2.imwrite(str(p), bot.frame)
                    log.info("已保存 %s", p)

            if not bot.show_preview(now):
                log.info("预览窗口已关闭，退出")
                break

            bot.fps_sleep(t0)
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C")
    finally:
        hk.stop()
        s = bot.stats
        run = time.perf_counter() - s["start"] if s["start"] else 0
        avg = (sum(s["waits"]) / len(s["waits"])) if s["waits"] else 0
        total = s["bites"] + s["timeouts"]
        hit = s["caught"]
        acc = (hit / total * 100) if total else 0.0
        log.info("统计 → 抛竿 %d · 判定 %d · 钓到 %d · 误判 %d（超时 %d 已计入）· "
                 "命中率 %.0f%% · 找漂失败 %d · 丢漂 %d · 平均等待 %.1fs · 运行 %.0fs",
                 s["casts"], total, hit, s["wrong"], s["timeouts"],
                 acc, s["no_bobber"], s["lost"], avg, run)
        bot.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        _crash_report()
        raise SystemExit(1)

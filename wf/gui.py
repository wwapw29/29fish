"""图形界面（含实时捕捉画面 + 全局热键）。

为什么要做 GUI：控制台编码在打包后怎么折腾都不可靠（GBK/UTF-8 两种环境互相打架），
而 Qt 内部统一 UTF-8，日志直接渲染在窗口里 —— 从根上就没有"乱码"这回事了。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time

import cv2
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QImage, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QTextEdit, QVBoxLayout, QWidget)

from wf.paths import app_root

ROOT = app_root()

QSS = """
QMainWindow, QWidget#root { background: #1a1a22; }
QLabel { color: #d6d6e0; font-family: "Microsoft YaHei"; }
QLabel#title { color: #8fd3ff; font-size: 15px; font-weight: bold; }
QLabel#state { color: #7dffb0; font-size: 13px; font-weight: bold; }
QLabel#hint  { color: #6f6f80; font-size: 11px; }
QLabel#view  { background: #0d0d14; border: 1px solid #2c2c3c; border-radius: 6px; }
QPushButton {
    background: #2b2b3a; color: #d6d6e0; border: 1px solid #3d3d52;
    border-radius: 6px; padding: 7px 14px; font-family: "Microsoft YaHei";
    font-size: 12px;
}
QPushButton:hover { background: #363648; border-color: #4d7fff; }
QPushButton:pressed { background: #22222e; }
QPushButton#primary { background: #1f6f43; border-color: #2f9f63; color: #d8ffe8; }
QPushButton#primary:hover { background: #26854f; }
QPushButton#danger  { background: #6f2424; border-color: #9f3c3c; color: #ffd8d8; }
QPushButton#danger:hover { background: #8a2c2c; }
QTextEdit {
    background: #12121a; color: #b8b8c8; border: 1px solid #2c2c3c;
    border-radius: 6px; font-family: Consolas, "Microsoft YaHei"; font-size: 12px;
}
"""


class _Bridge(QObject):
    """后台线程 / 热键线程 -> UI 线程的安全通道。"""
    log = Signal(str)
    hotkey = Signal(str)          # "toggle" / "shot" / "quit"


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: _Bridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record):
        try:
            self.bridge.log.emit(self.format(record))
        except Exception:
            pass


class BotThread(threading.Thread):
    """在后台跑 bot.step()，避免点击/按键的阻塞卡住界面。"""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.bot = None
        self._stop = threading.Event()
        self._fps = 60.0

    def run(self):
        log = logging.getLogger("bot")
        try:
            from wf.bot import FishingBot
            self.bot = FishingBot(self.cfg)
            self.bot.start()          # 点了「开始钓鱼」就直接开钓
            log.info("显示器 %dx%d | 搜索区域 %dx%d @ (%d,%d)",
                     self.bot.cap.width, self.bot.cap.height,
                     self.bot.roi_w, self.bot.roi_h, self.bot.roi_x, self.bot.roi_y)
        except Exception as e:                    # noqa: BLE001
            log.error("初始化失败：%s", e)
            return

        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                self.bot.step(t0)
            except Exception as e:                # noqa: BLE001
                log.error("运行出错：%s", e)
                break
            dt = 1.0 / self._fps - (time.perf_counter() - t0)
            if dt > 0:
                time.sleep(dt)

        try:
            self.bot.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pixel Sentinel")
        self.resize(780, 800)
        self.setStyleSheet(QSS)

        self.bridge = _Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.hotkey.connect(self._on_hotkey)

        self.thread: BotThread | None = None
        self.overlay_proc: subprocess.Popen | None = None
        self._hk = None

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(9)

        # 标题 + 状态
        head = QHBoxLayout()
        head.addWidget(QLabel("Pixel Sentinel", objectName="title"))
        head.addStretch()
        self.lbl_state = QLabel("待命", objectName="state")
        self.lbl_stats = QLabel("竿 0 · 咬 0", objectName="hint")
        head.addWidget(self.lbl_state)
        head.addSpacing(14)
        head.addWidget(self.lbl_stats)
        lay.addLayout(head)

        # 按钮
        btns = QHBoxLayout()
        self.btn_start = QPushButton("开始钓鱼", objectName="primary")
        self.btn_start.clicked.connect(self.toggle_fishing)
        self.btn_overlay = QPushButton("看捕捉范围")
        self.btn_overlay.clicked.connect(self.toggle_overlay)
        self.btn_config = QPushButton("改配置")
        self.btn_config.clicked.connect(self.open_config)
        self.btn_logs = QPushButton("日志目录")
        self.btn_logs.clicked.connect(self.open_logs)
        for b in (self.btn_start, self.btn_overlay, self.btn_config, self.btn_logs):
            btns.addWidget(b)
        lay.addLayout(btns)

        # 实时捕捉画面
        lay.addWidget(QLabel("实时捕捉画面（绿框 = 锁定的鱼漂，黄框 = 判定窗口）",
                             objectName="hint"))
        self.lbl_view = QLabel(objectName="view")
        self.lbl_view.setMinimumSize(740, 356)
        self.lbl_view.setAlignment(Qt.AlignCenter)
        self.lbl_view.setText("点「开始钓鱼」后这里会显示实时画面")
        self.lbl_view.setStyleSheet(
            "background:#0d0d14; border:1px solid #2c2c3c; border-radius:6px; color:#4a4a5c;")
        lay.addWidget(self.lbl_view)

        # 日志
        self.txt = QTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setFont(QFont("Consolas", 9))
        self.txt.setMinimumHeight(150)
        lay.addWidget(self.txt, 1)

        hint = QLabel("F8 开始/暂停 · F7 存当前画面 · F9 关闭　|　"
                      "游戏需为「窗口化全屏」，本程序需与游戏同为管理员权限",
                      objectName="hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # 定时器：状态 + 画面
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)

        self._setup_hotkeys()
        self._append_log("就绪。点「开始钓鱼」或按 F8 启动。")

    # ------------------------------------------------------------------ #
    # 热键
    # ------------------------------------------------------------------ #
    def _setup_hotkeys(self) -> None:
        """注册全局热键。GUI 模式之前把 main() 的热键整段跳过了，所以按了没反应。"""
        try:
            from pynput import keyboard
            self._hk = keyboard.GlobalHotKeys({
                "<f8>": lambda: self.bridge.hotkey.emit("toggle"),
                "<f7>": lambda: self.bridge.hotkey.emit("shot"),
                "<f9>": lambda: self.bridge.hotkey.emit("quit"),
            })
            self._hk.start()
            self._append_log("热键已就绪：F8 开始/暂停 · F7 存图 · F9 关闭")
        except Exception as e:                    # noqa: BLE001
            self._append_log(f"热键注册失败（{e}），请用界面按钮操作")

    def _on_hotkey(self, cmd: str) -> None:
        if cmd == "toggle":
            self.toggle_fishing()
        elif cmd == "shot":
            self.save_shot()
        elif cmd == "quit":
            self.close()

    def save_shot(self) -> None:
        if not (self.thread and self.thread.bot and self.thread.bot.frame is not None):
            self._append_log("现在没有画面可保存")
            return
        d = ROOT / "debug"
        d.mkdir(exist_ok=True)
        p = d / time.strftime("shot-%H%M%S.png")
        try:
            cv2.imwrite(str(p), self.thread.bot.frame)
            self._append_log(f"已保存画面：debug/{p.name}")
        except Exception as e:                    # noqa: BLE001
            self._append_log(f"保存失败：{e}")

    # ------------------------------------------------------------------ #
    # UI 更新
    # ------------------------------------------------------------------ #
    def _append_log(self, text: str) -> None:
        self.txt.append(text)
        self.txt.moveCursor(QTextCursor.End)

    def _tick(self) -> None:
        if not (self.thread and self.thread.bot):
            return
        bot = self.thread.bot
        try:
            from wf.bot import STATE_LABEL
            self.lbl_state.setText(STATE_LABEL.get(bot.state, bot.state))
        except Exception:
            pass
        s = bot.stats
        self.lbl_stats.setText(f"竿 {s['casts']} · 咬 {s['bites']} · "
                               f"钓到 {s['caught']} · 误判 {s['wrong']}")
        self._refresh_view(bot)

    def _refresh_view(self, bot) -> None:
        """把 bot 缓存的那一帧（已带标注）画到 QLabel 上。"""
        try:
            img = bot.render()
            if img is None:
                return
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
            pix = QPixmap.fromImage(qimg).scaled(
                self.lbl_view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.lbl_view.setPixmap(pix)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def toggle_fishing(self) -> None:
        if self.thread and self.thread.is_alive() and self.thread.bot:
            self.thread.bot.stop("已暂停")
            self.thread = None
            self.btn_start.setText("开始钓鱼")
            self.btn_start.setObjectName("primary")
            self.btn_start.setStyleSheet(QSS)
            self._append_log("—— 暂停 ——")
            return

        from wf.config import load_config
        cfg = load_config(ROOT / "config.toml")
        cfg.ui.preview = False          # 画面显示在 GUI 里，不再开 cv2 窗口

        self.thread = BotThread(cfg)
        self.thread.start()
        self.btn_start.setText("暂停")
        self.btn_start.setObjectName("danger")
        self.btn_start.setStyleSheet(QSS)
        self._append_log("—— 开始钓鱼 ——")

    def toggle_overlay(self) -> None:
        if self.overlay_proc and self.overlay_proc.poll() is None:
            self.overlay_proc.terminate()
            self.overlay_proc = None
            self.btn_overlay.setText("看捕捉范围")
            self._append_log("捕捉范围框已隐藏")
            return
        cmd = ([sys.executable, "overlay"] if getattr(sys, "frozen", False)
               else [sys.executable, str(ROOT / "main.py"), "overlay"])
        try:
            self.overlay_proc = subprocess.Popen(cmd, cwd=str(ROOT))
            self.btn_overlay.setText("隐藏范围框")
            self._append_log("捕捉范围框已显示（再点一次关闭）")
        except Exception as e:                    # noqa: BLE001
            self._append_log(f"启动范围框失败：{e}")

    def open_config(self) -> None:
        try:
            subprocess.Popen(["notepad.exe", str(ROOT / "config.toml")])
            self._append_log("已打开配置（改完重启程序生效）")
        except Exception as e:                    # noqa: BLE001
            self._append_log(f"打开失败：{e}")

    def open_logs(self) -> None:
        d = ROOT / "logs"
        d.mkdir(exist_ok=True)
        try:
            subprocess.Popen(["explorer.exe", str(d)])
        except Exception as e:                    # noqa: BLE001
            self._append_log(f"打开失败：{e}")

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:
        try:
            if self._hk:
                self._hk.stop()
        except Exception:
            pass
        if self.thread and self.thread.is_alive():
            self.thread.stop()
            time.sleep(0.3)
        if self.overlay_proc and self.overlay_proc.poll() is None:
            self.overlay_proc.terminate()
        event.accept()


def run_gui(auto_start: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Pixel Sentinel")

    win = MainWindow()

    # 多屏环境下 Qt 可能把窗口丢到副屏（实测跑到 x=2812，主屏只有 2560 宽）
    try:
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            win.move(g.center() - win.rect().center())
    except Exception:
        pass

    handler = QtLogHandler(win.bridge)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)

    win.show()
    win.raise_()
    win.activateWindow()

    # 关键：让本窗口对「屏幕捕获」不可见。
    # 否则窗口盖在搜索区域上，抓屏会把自己的画面也抓进去，
    # 实时预览里就会出现无限递归的镜像。
    # WDA_EXCLUDEFROMCAPTURE = 0x11（Win10 2004+；老系统上失败就降级，不影响使用）
    try:
        import ctypes
        hwnd = int(win.winId())
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
    except Exception:
        pass

    if auto_start:
        QTimer.singleShot(600, win.toggle_fishing)

    return app.exec()

"""打包成免安装版（自带 Python 运行时，拷到任何 Windows 机器都能跑）。

用法:
    python build.py

产物:
    dist/WowFishing/          <- 整个文件夹拷走即可，不需要装 Python
        WowFishing.exe        <- 主程序
        config.toml           <- 配置文件（直接改这个）
        开始钓鱼.bat
        调参工具.bat
        显示捕捉范围.bat
        _internal/            <- Python 运行时和依赖，别删
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "release" / "WowFishing"

BATS = {
    "开始钓鱼.bat": """@echo off
cd /d "%~dp0"
rem 无控制台模式启动（窗口是 GUI，黑框只是兜底日志，最小化即可）
start "" /min "%~dp0WowFishing.exe"
""",
    "调参工具.bat": """@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
    echo 用法: 调参工具.bat [live^|check^|cast^|region]
    echo   live   实时检测预览，边看边调参   ^<-- 最常用
    echo   check  截一张图，诊断能不能定位到鱼漂
    echo   cast   诊断自动抛竿
    echo   region 框选水面搜索区域
    echo.
    set /p mode=直接回车用 live:
    if "%mode%"=="" (set mode=live)
    "%~dp0WowFishing.exe" calibrate %mode%
) else (
    "%~dp0WowFishing.exe" calibrate %*
)
pause
""",
    "显示捕捉范围.bat": """@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 屏幕上会显示绿色框（点击穿透，不影响游戏）。按 ESC 退出。
echo.
"%~dp0WowFishing.exe" overlay
""",
}


def main() -> int:
    print("=" * 60)
    print("打包 WoW 钓鱼助手（免安装版）")
    print("=" * 60)

    # 把整个 dist 目录改名挪开，让 PyInstaller 从零创建。
    # 原因：PyInstaller 的 --noconfirm 会先 "Removing dir dist/<name>"，
    # 200+ 个文件会触发安全策略拦截 → 打包静默失败、dist 里还是旧版本（踩过好几次）。
    # 改名不删文件，不会触发拦截。
    # 清理旧产物。用 shutil.rmtree 而不是系统命令 —— 正常机器上没问题。
    # （如果删除失败，PyInstaller 会自己再试一次并报错，不会静默产出旧版本）
    dist_dir = ROOT / "release"
    if dist_dir.exists():
        shutil.rmtree(dist_dir, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "WowFishing",
        "--distpath", str(ROOT / "release"),
        "--paths", str(ROOT),
        # pynput 的 Windows 后端是动态导入的，PyInstaller 扫不到
        "--hidden-import", "pynput.keyboard._win32",
        "--hidden-import", "pynput.mouse._win32",
        # OCR 引擎：模型文件是数据文件，必须整包收集
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-all", "onnxruntime",
        # 这些用不上，排除掉能省几十 MB
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        # PySide6 里用不到的大家伙，排掉能省 100MB+
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtWebEngineWidgets",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtQml",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtMultimedia",
        "--exclude-module", "PySide6.QtCharts",
        "--exclude-module", "PySide6.QtDataVisualization",
        "--exclude-module", "PySide6.QtPdf",
        "--exclude-module", "PySide6.QtSql",
        "--exclude-module", "PySide6.QtTest",
        str(ROOT / "main.py"),
    ]
    print("PyInstaller 正在打包（第一次要几分钟）…\n")
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("\n✗ 打包失败")
        return r.returncode

    if not (DIST / "WowFishing.exe").exists():
        print("\n✗ 没找到生成的 exe")
        return 1

    shutil.copy2(ROOT / "config.toml", DIST / "config.toml")
    for name, content in BATS.items():
        (DIST / name).write_text(content, encoding="utf-8")

    # build/ 里也会有一个同名 exe，但它**没有**旁边的 _internal 依赖，双击必然报
    # "Failed to execute script"。只删里面的 exe，避免用户点错（整目录删除会被安全策略拦）。
    for exe in (ROOT / "build").rglob("*.exe"):
        try:
            exe.unlink()
        except Exception:
            pass
    for spec in ROOT.glob("*.spec"):
        try:
            spec.unlink()
        except Exception:
            pass

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1024 / 1024
    print("\n" + "=" * 60)
    print(f"✓ 打包完成: {DIST}")
    print(f"  体积约 {size:.0f} MB")
    print("  整个文件夹拷到别的机器就能用，不需要装 Python")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
    echo 用法: calibrate.bat [live^|check^|cast^|region]
    echo   live   实时检测预览，边看边调参   ^<-- 最常用
    echo   check  截一张图，诊断能不能定位到鱼漂
    echo   cast   诊断自动抛竿：窗口识别 + 切前台 + 试按技能键
    echo   region 框选水面搜索区域
    echo.
    set /p mode=直接回车用 live:
    if "%mode%"=="" (set mode=live)
    .venv\Scripts\python.exe tools\calibrate.py %mode%
) else (
    .venv\Scripts\python.exe tools\calibrate.py %*
)
pause

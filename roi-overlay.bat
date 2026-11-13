@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 显示捕捉范围叠加框（点击穿透，不影响游戏）。按 ESC 退出。
echo.
.venv\Scripts\python.exe tools\roi_overlay.py
if errorlevel 1 pause

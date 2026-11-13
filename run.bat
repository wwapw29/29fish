@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WoW Fishing Helper

if not exist ".venv\Scripts\python.exe" (
    echo [!] 没有找到虚拟环境 .venv
    echo     请先执行:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem 建议以管理员身份运行本脚本，否则魔兽（管理员权限）可能收不到模拟输入
net session >nul 2>&1
if errorlevel 1 (
    echo [!] 当前不是管理员权限，如果点击无效，请右键「以管理员身份运行」
    echo.
)

.venv\Scripts\python.exe main.py %*
pause

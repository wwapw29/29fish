"""路径工具：统一处理「源码运行」和「打包成 exe 后运行」两种情况。

打包（PyInstaller）后 `__file__` 指向的不是项目目录，硬编码 Path(__file__).parent
会让程序找不到 config.toml、写不了 logs/debug。所有入口都必须走 app_root()。
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """程序根目录。

    · 源码运行  → 项目根目录（wf/ 的上一级）
    · 打包运行  → exe 所在目录（onedir 模式就是那个文件夹）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(rel: str) -> Path:
    """打包进 exe 内部的只读资源（如果将来有图片/模板之类）。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / rel
    return app_root() / rel

"""AutoSI GUI 启动策略：发布版固定 PySide6，旧 GUI 仅供源码开发回退。"""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from pathlib import Path
import traceback


def load_gui_runner(*, packaged: bool, legacy_requested: bool):
    """返回 GUI 入口；发布版不会受旧 GUI 环境变量影响。"""
    if legacy_requested and not packaged:
        return import_module("app.SettingConfig").run_app
    try:
        return import_module("app.PySideSettingConfig").run_app
    except ImportError:
        if packaged:
            raise
        return import_module("app.SettingConfig").run_app


def report_packaged_gui_error(base_dir: str | Path, error: BaseException) -> Path:
    """记录发布版 GUI 启动失败，并显示用户可见错误。"""
    log_dir = Path(base_dir) / "8_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "gui_startup_error.log"
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    message = (
        "AutoSI 正式界面启动失败，程序不会切换到旧界面。\n"
        f"错误日志：{log_path}\n\n{error}"
    )
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(
            f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{detail}"
        )
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("AutoSI 启动失败", message)
        root.destroy()
    except Exception:
        print(message)
    return log_path

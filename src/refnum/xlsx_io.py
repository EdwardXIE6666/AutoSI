"""Excel 落盘：先写临时文件再替换原名，避免原地 Zip 截断已有 xlsx。"""
from __future__ import annotations

import os
import stat
from datetime import datetime


def is_xlsx_write_blocked(exc: BaseException) -> bool:
    """覆盖已有 xlsx 失败：占用、只读、加密拦截。Windows 常给 errno 22。"""
    if isinstance(exc, PermissionError):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "errno", None) in (13, 22):
        return True
    if getattr(exc, "winerror", None) in (5, 32):
        return True
    blob = str(exc).lower()
    return any(
        token in blob
        for token in (
            "permission denied",
            "invalid argument",
            "access is denied",
            "being used by another process",
        )
    )


def _chmod_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def _remove_if_exists(path: str) -> None:
    if not path or not os.path.lexists(path):
        return
    _chmod_writable(path)
    os.remove(path)


def save_workbook(wb, filename: str) -> str:
    """写出 xlsx。不原地打开已有文件（ZipFile 'w' 在占用/加密盘上会 Invalid argument）。

    先写同目录临时文件，再替换到目标名。替换失败则删旧再替；仍失败才落到时间戳名。
    """
    path = os.path.abspath(str(filename or ""))
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    tmp = os.path.join(folder, f".~{name}.{os.getpid()}.tmp")

    try:
        _remove_if_exists(tmp)
        wb.save(tmp)
    except OSError as exc:
        _remove_if_exists(tmp)
        raise

    def _install(dest: str) -> None:
        os.replace(tmp, dest)

    try:
        _install(path)
        return path
    except OSError as exc:
        if not is_xlsx_write_blocked(exc):
            _remove_if_exists(tmp)
            raise
        try:
            _remove_if_exists(path)
            _install(path)
            return path
        except OSError:
            pass

    base, ext = os.path.splitext(path)
    alt = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext or '.xlsx'}"
    try:
        _install(alt)
    except OSError as exc2:
        _remove_if_exists(tmp)
        if not is_xlsx_write_blocked(exc2):
            raise
        raise OSError(f"无法写入: {path}") from exc2
    print(
        f"[功能5] 无法覆盖 {path}，已保存到 {alt}",
        flush=True,
    )
    return alt

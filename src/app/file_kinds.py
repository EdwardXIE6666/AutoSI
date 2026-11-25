"""导入/扫描用的文件类型判定：看内容，不看工程名或位号。"""

from __future__ import annotations

from pathlib import Path

from siwave_extractor.netlist import is_netlist_text, is_schematic_text

_SIW_MAGIC = b"# FVN:"
_GZIP_MAGIC = b"\x1f\x8b"


def _head(path: str | Path, size: int = 80) -> bytes:
    data = Path(path).read_bytes()[:size]
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data


def is_siw_file(path: str | Path) -> bool:
    if Path(path).suffix.lower() != ".siw":
        return False
    try:
        return _head(path).lstrip().startswith(_SIW_MAGIC)
    except OSError:
        return False


def is_odb_file(path: str | Path) -> bool:
    if Path(path).suffix.lower() != ".tgz":
        return False
    try:
        head = _head(path, 4)
    except OSError:
        return False
    return head.startswith(_GZIP_MAGIC)


FORMAT_ERROR_PREFIX = "FORMAT:"


def mismatch_message(kind: str, path: str | Path) -> str | None:
    """文件不能作为该类型导入时返回说明；通过则返回 None。"""
    src = Path(path)
    try:
        readable = src.is_file()
        if readable:
            _head(src, 4)
    except OSError:
        return "无法读取该文件，请确认文件未加密、未损坏。"
    if kind == "schematic":
        if src.suffix.lower() != ".txt":
            return "原理图须为 .txt 文件（PADS 导出，文件头 *PADS-LOGIC*）。"
        if is_schematic_text(src):
            return None
        if is_netlist_text(src):
            return "这是网络频段表，不是原理图。请把原理图放到输入，网络表由功能1生成。"
        return "这不是 PADS 原理图（文件头应为 *PADS-LOGIC*）。"
    if kind == "netlist":
        if src.suffix.lower() != ".txt":
            return "网络表须为 .txt 文件。"
        if is_schematic_text(src):
            return "这是原理图，不是网络频段表。请先运行功能1，或选择功能1生成的 txt。"
        if is_netlist_text(src):
            return None
        return "这不是网络频段表（每行应为：网络名,器件,起始频率,结束频率）。"
    if kind == "siw":
        if is_siw_file(src):
            return None
        if src.suffix.lower() != ".siw":
            return "请选择 .siw 文件。"
        return "这不是有效的 SIwave 工程文件。"
    if kind == "odb":
        if is_odb_file(src):
            return None
        if src.suffix.lower() != ".tgz":
            return "ODB++ 须为 .tgz 文件。"
        return "这不是有效的 ODB++ 压缩包（.tgz）。"
    return f"未知输入类型：{kind}"


def import_error_title(error: str) -> str:
    if str(error or "").startswith(FORMAT_ERROR_PREFIX):
        return "文件格式错误"
    return "导入失败"


def import_error_text(error: str) -> str:
    text = str(error or "")
    if text.startswith(FORMAT_ERROR_PREFIX):
        return text[len(FORMAT_ERROR_PREFIX):]
    return text

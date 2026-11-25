"""功能5 — 预览 / 50R 共用蓝底标题 + 细边框（不含手册着色）。"""
from __future__ import annotations

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

THIN = Side(style="thin", color="FF000000")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
FILL_HDR = PatternFill(fill_type="solid", fgColor="FF4472C4")
FONT_HDR = Font(color="FFFFFFFF", bold=True)
_ALIGN_TITLE = Alignment(horizontal="left", vertical="center", wrap_text=True)


def style_cell(cell, *, fill=None, header=False) -> None:
    cell.border = BORDER
    cell.alignment = CENTER
    if fill is not None:
        cell.fill = fill
    if header:
        cell.font = FONT_HDR
        cell.fill = FILL_HDR


def write_blue_title(ws, row: int, title: str, n_cols: int) -> None:
    """在蓝色表头行写入表标题（合并单元格 + 白字蓝底）。"""
    for col in range(1, n_cols + 1):
        cell = ws.cell(row, col, title if col == 1 else None)
        style_cell(cell, header=True)
        cell.alignment = _ALIGN_TITLE
    try:
        ws.merge_cells(
            start_row=row, start_column=1, end_row=row, end_column=n_cols,
        )
    except Exception:
        pass
    c0 = ws.cell(row, 1)
    c0.value = title
    c0.font = FONT_HDR
    c0.fill = FILL_HDR
    c0.alignment = _ALIGN_TITLE
    c0.border = BORDER

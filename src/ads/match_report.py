"""ADS 天线口匹配验收表 Excel 写出。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import PatternFill

VSWR_LIMIT = 1.3

_FILL_PASS = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
_FILL_FAIL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

_HEADERS = (
    "ANT",
    "频段",
    "最差VSWR",
    "是否<1.3",
    "对应|S11|",
    "顶到边界",
    "跨图同名",
    "各件终值",
)


@dataclass
class BandResult:
    ant: str
    band: str
    worst_vswr: float | None
    s11_mag: float | None
    status: str
    at_bound: list[str]
    cross_ant: str
    values: dict[str, str]


def vswr_pass(worst: float) -> bool:
    return worst < VSWR_LIMIT


def _vswr_cell(row: BandResult) -> tuple[str, PatternFill | None]:
    if row.worst_vswr is None:
        return "未跑成", None
    if vswr_pass(row.worst_vswr):
        return "是", _FILL_PASS
    return "否", _FILL_FAIL


def _format_values(values: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in values.items())


def write_match_report(path: Path, rows: list[BandResult]) -> Path:
    green = sum(1 for r in rows if r.status == "ok")
    red = sum(1 for r in rows if r.status == "fail")
    unrun = sum(1 for r in rows if r.status == "unrun")

    wb = Workbook()
    ws = wb.active
    ws.title = "match"

    ws["A1"] = f"绿={green} 红={red} 未跑成={unrun}"

    for col, header in enumerate(_HEADERS, start=1):
        ws.cell(row=2, column=col, value=header)

    for row_idx, row in enumerate(rows, start=3):
        ws.cell(row=row_idx, column=1, value=row.ant)
        ws.cell(row=row_idx, column=2, value=row.band)
        ws.cell(row=row_idx, column=3, value=row.worst_vswr)
        label, fill = _vswr_cell(row)
        pass_cell = ws.cell(row=row_idx, column=4, value=label)
        if fill is not None:
            pass_cell.fill = fill
        ws.cell(row=row_idx, column=5, value=row.s11_mag)
        ws.cell(row=row_idx, column=6, value=",".join(row.at_bound))
        ws.cell(row=row_idx, column=7, value=row.cross_ant)
        ws.cell(row=row_idx, column=8, value=_format_values(row.values))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path

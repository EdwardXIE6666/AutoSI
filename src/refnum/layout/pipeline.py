"""功能5 — 【阶段 B · 手册式 refnum】编排入口。

流水线：
  L0 分组折叠 → L1 share_map → L2 行序
  → L3 灰格对齐 → 非共享单格 → L6 竖合并
  → L4/L5 Co-pad 整块插入（完整结构）→ 可见唯一 → 链路间空行 → 写表

  share / model / rows / order / place / merge / copad / unique / xlsx
"""
from __future__ import annotations

from typing import Optional

from refnum.layout.order import assert_holes_zero, l2_order_rows
from refnum.layout.rows import (
    build_sheet_plans,
    l0_group_paths,
    l1_share_map,
)
from refnum.layout.xlsx import write_handbook_xlsx
from refnum.path import CoPadStructure, Path

BUILD_ID = "2026-08-09-layout-l2-merge-rehome-loop-v1"
LAYOUT_STAGE = "L2>scoreGather+sideRehomeHostBlockTail+paLnaColBandOnly+mergeKeepPaOutside+copadPerBandRehome+trunkAfterOnly+hostRestore+sandwichReserveCols+L6block→L2rehome≤2+L3>packPrivateToShared+L6>noMergeThruNonOwnerPath+contigOwner+L4+sideTopoFil+grayStop+gap+50R"


def build_handbook_workbook(
    paths: list[Path],
    filename: str,
    copad_structures: Optional[list[CoPadStructure]] = None,
    copad_pairs=None,
    signal_infos=None,
    schematic_res=None,
    pin_parser=None,
    component_values: dict[str, str] | None = None,
) -> str:
    """建 plan 并写 handbook xlsx（Co-pad 块 + 可选 50R 覆盖页）。

    Co-lay 另一支以普通 BAND 进 paths。
    """
    _ = copad_pairs
    paths = list(paths or [])
    plans = build_sheet_plans(paths, copad_structures=copad_structures)
    n_rows = sum(len(p.rows) for p in plans)
    if paths and n_rows == 0:
        raise RuntimeError(
            f"[功能5][最终产物·handbook] L0 结果为空: paths={len(paths)}"
        )
    if n_rows > len(paths):
        raise RuntimeError(
            f"[功能5][最终产物·handbook] L0 行数异常: "
            f"paths={len(paths)} rows={n_rows}"
        )
    return write_handbook_xlsx(
        plans,
        filename,
        build_id=BUILD_ID,
        layout_stage=LAYOUT_STAGE,
        path_count=len(paths),
        paths=paths,
        copad_structures=copad_structures,
        signal_infos=signal_infos,
        schematic_res=schematic_res,
        pin_parser=pin_parser,
        component_values=component_values,
    )


def layout_row_order_ids(paths: list[Path]) -> dict[str, list[str]]:
    """测试：各 Sheet 有序 row_id。"""
    plans = build_sheet_plans(paths)
    return {p.key: [r.row_id for r in p.rows] for p in plans}


__all__ = [
    "BUILD_ID",
    "LAYOUT_STAGE",
    "assert_holes_zero",
    "build_handbook_workbook",
    "build_sheet_plans",
    "l0_group_paths",
    "l1_share_map",
    "l2_order_rows",
    "layout_row_order_ids",
]

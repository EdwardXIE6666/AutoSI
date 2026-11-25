"""功能5 — 【阶段 A · 中间产物】路径预览 Excel 写出（与算法层分离）。

从 refnum_path 取 Path / COPAD 结构，写出总览 + 各 Sheet 核对表。
50R 覆盖审计：refnum_50r_coverage（路径搭建之外的核对页）。
不做路径/COPAD 判定；也不是维修手册最终构图（阶段 B）。
推荐文件名：*_path_preview.xlsx。见 docs/功能5总设计.md §1。
"""
from __future__ import annotations

import os
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from refnum.path import (
    BUILD_ID,
    CoLayStructure,
    CoPadPair,
    CoPadStructure,
    Device,
    Path,
    build_colay_structures,
    build_copad_structures,
    build_paths,
    group_paths_by_ant,
    label_colay_structures_for_sheet,
    label_copad_structures_for_sheet,
    path_display_devices,
    sheet_sort_key,
)
from refnum.xlsx_style import (
    BORDER as _BORDER,
    CENTER as _CENTER,
    style_cell as _style_cell,
    write_blue_title as _write_blue_title,
)

# --- 样式（路径预览专用着色，蓝标题见 refnum_xlsx_style）---
_FILL_IC = PatternFill(fill_type="solid", fgColor="FFD9D9D9")
_FILL_TX = PatternFill(fill_type="solid", fgColor="FFFFC000")
_FILL_RX = PatternFill(fill_type="solid", fgColor="FF92D050")
_FILL_COPAD = PatternFill(fill_type="solid", fgColor="FFFFFF00")
# 并联/对地挂件：浅蓝
_FILL_SHUNT = PatternFill(fill_type="solid", fgColor="FF9DC3E6")
_FILL_BAND = PatternFill(fill_type="solid", fgColor="FFD6DCE4")
_FONT_BOLD = Font(bold=True)



def _cell_fill(dev: Device, direction: str) -> PatternFill:
    # Co-pad/Co-lay/JUMP 网位号 黄；侧支本体与普通器件同色
    if (
        getattr(dev, "is_copad", False)
        or getattr(dev, "is_colay", False)
        or getattr(dev, "is_jump_attach", False)
    ):
        return _FILL_COPAD
    if getattr(dev, "is_shunt", False):
        return _FILL_SHUNT
    if dev.is_sdr or dev.is_ant or dev.is_ic or dev.is_lpf or dev.is_pa_lna:
        return _FILL_IC
    if direction in ("TX", "TRX"):
        return _FILL_TX
    if direction == "RX":
        return _FILL_RX
    return _FILL_TX


def _join_devs(devs: tuple[str, ...]) -> str:
    return " — ".join(devs) if devs else ""


def _format_copad_branch(st: CoPadStructure, devs: tuple[str, ...]) -> str:
    """双 stub（恰 2 器件且分属分叉腿/合并腿）用「|」，否则「 — 」。"""
    if (
        len(devs) == 2
        and st.merge is not None
        and devs[0] in (st.fork.leg_a, st.fork.leg_b)
        and devs[1] in (st.merge.leg_a, st.merge.leg_b)
    ):
        return f"{devs[0]} | {devs[1]}"
    return " — ".join(devs) if devs else ""


def _write_overview(
    ws,
    paths: list[Path],
    by_ant: dict[str, list[Path]],
    copad_pairs: list[CoPadPair],
    structures: list[CoPadStructure],
    coverage_note: str = "",
    colay_structures: Optional[list[CoLayStructure]] = None,
) -> None:
    colay_structures = list(colay_structures or [])
    ws.cell(1, 1, "功能5 【中间产物】路径预览 · 总览（非最终手册 refnum）")
    ws.cell(1, 1).font = _FONT_BOLD
    ws.cell(2, 1, f"build={BUILD_ID}  product=PATH_INTERMEDIATE")
    note = coverage_note or "50R覆盖: 未检查（未传入 schematic_res）"
    ws.cell(
        3, 1,
        "本表仅供路径/COPAD/COLAY/50R 核对，不是维修手册最终图。"
        "ANT 频段链止于 PA/LNA/QPM/LPAMiF；TRX→PA/LNA、GPS/GNSS 单独页。"
        "COPAD=分叉对+两条支路+合并对；COLAY=orphan焊盘双腿正常支路（无合并组）。"
        f" | {note}",
    )

    # —— 表1：路径汇总（蓝底标题行 + 蓝底列名行）——
    r = 5
    _write_blue_title(ws, r, "表1 路径汇总", 6)
    r += 1
    for col, h in enumerate(
        ("Sheet", "类型", "路径数", "最长 hop", "触及结构数", "样例/说明"), 1
    ):
        c = ws.cell(r, col, h)
        _style_cell(c, header=True)
    r += 1
    for ant in sorted(by_ant.keys(), key=sheet_sort_key):
        pl = by_ant[ant]
        kind = pl[0].kind if pl else "BAND"
        if kind == "TRX_PA_LNA":
            kind_label = "TRX→PA/LNA"
        elif kind == "GPS":
            kind_label = "GPS/GNSS"
        else:
            kind_label = "频段BAND"
        max_hop = max((len(p.devices) for p in pl), default=0)
        bands = sorted({p.band for p in pl if p.band})[:6]
        ant_refs = {d.ref for p in pl for d in p.devices}
        n_st = 0
        for st in structures:
            refs = {st.fork.leg_a, st.fork.leg_b}
            if st.merge:
                refs |= {st.merge.leg_a, st.merge.leg_b}
            if refs & ant_refs:
                n_st += 1
        note = ", ".join(bands)
        if kind == "BAND":
            miss = sum(1 for p in pl if not any(d.is_sdr for d in p.devices))
            if miss:
                note = f"缺TRX×{miss}; " + note
        ws.cell(r, 1, ant)
        ws.cell(r, 2, kind_label)
        ws.cell(r, 3, len(pl))
        ws.cell(r, 4, max_hop)
        ws.cell(r, 5, n_st if kind in ("BAND", "GPS") else "-")
        ws.cell(r, 6, note)
        for col in range(1, 7):
            _style_cell(ws.cell(r, col))
        r += 1

    n_band = sum(1 for p in paths if p.kind == "BAND")
    n_trx = sum(1 for p in paths if p.kind == "TRX_PA_LNA")
    n_gps = sum(1 for p in paths if p.kind == "GPS")
    ws.cell(
        r, 1,
        f"合计路径: {len(paths)} "
        f"(频段{n_band} + TRX_PA_LNA{n_trx} + GPS{n_gps})  "
        f"Sheet: {len(by_ant)}  Co-pad对: {len(copad_pairs)}  "
        f"COPAD结构: {len(structures)}  COLAY结构: {len(colay_structures)}",
    )
    r += 2

    # —— 表2：COPAD 结构明细（蓝底标题行 + 蓝底列名行）——
    _write_blue_title(
        ws, r,
        "表2 COPAD 结构明细（干路分叉前 → 分叉对 → 支路一/二 → 合并对 → 干路合并后）",
        7,
    )
    r += 1
    headers = (
        "序号",
        "干路器件（分叉前）",
        "copad对（分叉）",
        "支路一",
        "支路二",
        "copad对（合并）",
        "干路器件（合并后）",
    )
    for col, h in enumerate(headers, 1):
        c = ws.cell(r, col, h)
        _style_cell(c, header=True)
    r += 1

    for i, st in enumerate(structures, 1):
        fork_pair = f"{st.fork.leg_a} + {st.fork.leg_b}"
        if st.merge:
            merge_pair = f"{st.merge.leg_a} + {st.merge.leg_b}"
        else:
            merge_pair = "(缺合并对)"
        ws.cell(r, 1, i)
        ws.cell(r, 2, _join_devs(st.trunk_before))
        ws.cell(r, 3, fork_pair)
        ws.cell(r, 4, _format_copad_branch(st, st.branch1))
        ws.cell(r, 5, _format_copad_branch(st, st.branch2))
        ws.cell(r, 6, merge_pair)
        ws.cell(r, 7, _join_devs(st.trunk_after))
        for col in range(1, 8):
            _style_cell(ws.cell(r, col), fill=_FILL_COPAD)
        r += 1
    if not structures:
        ws.cell(r, 1, "(未识别到 COPAD 结构)")
        r += 1
    r += 2

    # —— 表3：COLAY 结构明细 ——
    _write_blue_title(
        ws, r,
        "表3 COLAY 结构明细（orphan 焊盘双腿 · 正常支路分叉 · 无 copad 合并组）",
        6,
    )
    r += 1
    for col, h in enumerate(
        ("序号", "焊盘网", "colay对", "trunk/第三端", "支路一远侧", "支路二远侧"),
        1,
    ):
        c = ws.cell(r, col, h)
        _style_cell(c, header=True)
    r += 1
    for i, st in enumerate(colay_structures, 1):
        ws.cell(r, 1, i)
        ws.cell(r, 2, st.pad_net or "")
        ws.cell(r, 3, f"{st.pair.leg_a} + {st.pair.leg_b}")
        ws.cell(r, 4, st.pair.trunk_ref or "")
        ws.cell(r, 5, " — ".join(st.branch1) if st.branch1 else "")
        ws.cell(r, 6, " — ".join(st.branch2) if st.branch2 else "")
        for col in range(1, 7):
            _style_cell(ws.cell(r, col), fill=_FILL_COPAD)
        r += 1
    if not colay_structures:
        ws.cell(r, 1, "(未识别到 COLAY 结构)")
        r += 1
    r += 2

    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 36
    ws.column_dimensions["E"].width = 36
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 22


def _write_ant_sheet(
    ws,
    ant_num: str,
    paths: list[Path],
    structures: list[CoPadStructure],
    colay_structures: Optional[list[CoLayStructure]] = None,
    schematic_res=None,
    signal_infos=None,
) -> None:
    """每个 ANT / GPS / TRX 一页：路径 +（非 TRX 页）COPAD/COLAY 结构表。"""
    colay_structures = list(colay_structures or [])
    is_trx_sheet = ant_num == "TRX_PA_LNA" or any(
        p.kind == "TRX_PA_LNA" for p in paths
    )
    is_gps_sheet = ant_num == "GPS" or any(p.kind == "GPS" for p in paths)
    if is_trx_sheet:
        ordered = sorted(
            paths,
            key=lambda p: (
                p.band or "",
                p.devices[0].ref if p.devices else "",
                p.devices[-1].ref if p.devices else "",
            ),
        )
        labeled = []
        colay_labeled = []
    else:
        if is_gps_sheet:
            ordered = sorted(
                paths,
                key=lambda p: (
                    p.band or "",
                    p.devices[0].ref if p.devices else "",
                    p.devices[-1].ref if p.devices else "",
                ),
            )
        else:
            ordered = sorted(
                paths,
                key=lambda p: (
                    p.band or "",
                    0 if p.direction in ("TX", "TRX") else 1,
                    p.direction,
                ),
            )
        labeled = label_copad_structures_for_sheet(
            ordered, structures,
            main_paths=ordered,
        )
        colay_labeled = label_colay_structures_for_sheet(
            ordered, colay_structures,
            schematic_res=schematic_res,
            signal_infos=signal_infos,
        )

    ws.cell(1, 1, f"{ant_num} 路径预览")
    ws.cell(1, 1).font = _FONT_BOLD
    if is_trx_sheet:
        ws.cell(
            2, 1,
            f"Transceiver→PA/LNA 匹配（独立行，不并入频段）· "
            f"共 {len(ordered)} 条 · 左TRX→右PA/LNA · build={BUILD_ID}",
        )
    elif is_gps_sheet:
        ws.cell(
            2, 1,
            f"GPS/GNSS 路径（MAIN 提取；可拼接 TRX→LNA→ANT）· "
            f"共 {len(ordered)} 条 · {len(labeled)} 个COPAD结构 · "
            f"左芯片→右天线 · build={BUILD_ID}",
        )
    else:
        ws.cell(
            2, 1,
            f"频段路径（止于 PA/LNA/QPM/LPAMiF，不含 TRX）· 共 {len(ordered)} 条 · "
            f"{len(labeled)} 个COPAD结构 · 左前端→右ANT · build={BUILD_ID}",
        )

    ws.cell(3, 1, "图例:")
    c = ws.cell(3, 2, "IC/SDR/ANT")
    _style_cell(c, fill=_FILL_IC)
    c = ws.cell(3, 3, "TX被动")
    _style_cell(c, fill=_FILL_TX)
    c = ws.cell(3, 4, "RX被动")
    _style_cell(c, fill=_FILL_RX)
    c = ws.cell(3, 5, "Co-pad/lay/JUMP黄")
    _style_cell(c, fill=_FILL_COPAD)
    c = ws.cell(3, 6, "并联浅蓝")
    _style_cell(c, fill=_FILL_SHUNT)

    row = 5
    if not is_trx_sheet:
        _write_blue_title(
            ws, row,
            "表 COPAD 结构明细（干路分叉前 → 分叉对 → 支路一/二 → 合并对 → 干路合并后）",
            7,
        )
        row += 1
        for col, h in enumerate(
            (
                "序号",
                "干路器件（分叉前）",
                "copad对（分叉）",
                "支路一",
                "支路二",
                "copad对（合并）",
                "干路器件（合并后）",
            ),
            1,
        ):
            c = ws.cell(row, col, h)
            _style_cell(c, header=True)
        row += 1
        if not labeled:
            ws.cell(row, 1, "(本页无 COPAD 结构)")
            row += 1
        else:
            for si, (_fork_l, _merge_l, st) in enumerate(labeled, 1):
                fork_pair = f"{st.fork.leg_a} + {st.fork.leg_b}"
                if st.merge is not None:
                    merge_pair = f"{st.merge.leg_a} + {st.merge.leg_b}"
                else:
                    merge_pair = "(缺)"
                ws.cell(row, 1, si)
                ws.cell(row, 2, _join_devs(st.trunk_before))
                ws.cell(row, 3, fork_pair)
                ws.cell(row, 4, _format_copad_branch(st, st.branch1))
                ws.cell(row, 5, _format_copad_branch(st, st.branch2))
                ws.cell(row, 6, merge_pair)
                ws.cell(row, 7, _join_devs(st.trunk_after))
                for col in range(1, 8):
                    _style_cell(ws.cell(row, col), fill=_FILL_COPAD)
                row += 1
        row += 1
        _write_blue_title(
            ws, row,
            "表 COLAY 结构明细（orphan 焊盘双腿 · 无 copad 合并组）",
            5,
        )
        row += 1
        for col, h in enumerate(
            ("序号", "colay对", "焊盘网", "支路一远侧", "支路二远侧"), 1,
        ):
            c = ws.cell(row, col, h)
            _style_cell(c, header=True)
        row += 1
        if not colay_labeled:
            ws.cell(row, 1, "(本页无 COLAY 结构)")
            row += 1
        else:
            for si, (_lab, st) in enumerate(colay_labeled, 1):
                ws.cell(row, 1, si)
                ws.cell(row, 2, f"{st.pair.leg_a} + {st.pair.leg_b}")
                ws.cell(row, 3, st.pad_net or "")
                ws.cell(row, 4, " — ".join(st.branch1) if st.branch1 else "")
                ws.cell(row, 5, " — ".join(st.branch2) if st.branch2 else "")
                for col in range(1, 6):
                    _style_cell(ws.cell(row, col), fill=_FILL_COPAD)
                row += 1
        row += 1

    max_col = 2
    for i, path in enumerate(ordered, 1):
        n_copad = sum(
            1 for d in path.devices
            if d.is_copad
            or getattr(d, "is_colay", False)
            or getattr(d, "is_jump_attach", False)
        )
        labels = sorted({d.copad_label for d in path.devices if d.copad_label})
        clabels = sorted(
            {getattr(d, "colay_label", "") for d in path.devices if getattr(d, "colay_label", "")}
        )
        if path.kind == "TRX_PA_LNA":
            title = f"#{i}  TRX→PA/LNA  {path.band or '-'}  {path.direction}"
        elif path.kind == "GPS":
            title = f"#{i}  GPS  {path.band or '-'}  {path.direction}"
            if not any(d.is_sdr for d in path.devices):
                title += "  (段:无TRX)"
        else:
            title = f"#{i}  {path.band or '-'}  {path.direction}"
            if labels:
                title += "  含" + ",".join(f"对{x}" for x in labels)
            if clabels:
                title += "  COLAY" + ",".join(clabels)
            if not any(d.is_pa_lna for d in path.devices):
                title += "  ⚠缺PA/LNA"
        cell = ws.cell(row, 1, title)
        cell.fill = _FILL_BAND
        cell.font = _FONT_BOLD
        cell.border = _BORDER
        disp = path_display_devices(path)
        dev_row = row + 1
        for col_i, dev in enumerate(disp, start=2):
            cell = ws.cell(dev_row, col_i, dev.ref)
            _style_cell(cell, fill=_cell_fill(dev, path.direction))
            if dev.is_copad and dev.copad_label:
                role = f"对{dev.copad_label}"
            elif getattr(dev, "is_colay", False) and getattr(dev, "colay_label", ""):
                role = f"COLAY{dev.colay_label}"
            elif getattr(dev, "is_colay", False):
                role = "COLAY"
            elif getattr(dev, "is_jump_attach", False):
                role = "JUMP"
            elif dev.is_shunt:
                role = "并联"
            else:
                role = (dev.part_type or "")[:18]
            tcell = ws.cell(dev_row + 1, col_i, role)
            tcell.alignment = _CENTER
            _is_yellow = (
                dev.is_copad
                or getattr(dev, "is_colay", False)
                or getattr(dev, "is_jump_attach", False)
            )
            tcell.font = Font(
                size=8,
                color=(
                    "FF996600" if _is_yellow
                    else ("FF2F5496" if dev.is_shunt else "FF666666")
                ),
            )
            if _is_yellow:
                tcell.fill = _FILL_COPAD
            elif dev.is_shunt:
                tcell.fill = _FILL_SHUNT
            max_col = max(max_col, col_i)
        ws.cell(dev_row, 1, "器件")
        ws.cell(dev_row + 1, 1, "类型/对")
        _style_cell(ws.cell(dev_row, 1), fill=_FILL_BAND)
        _style_cell(ws.cell(dev_row + 1, 1), fill=_FILL_BAND)

        if disp:
            # 右端点：优先主链末端（非并联），TRX 链勿显示成最右旁路 C
            right_dev = next(
                (d for d in reversed(disp) if not getattr(d, "is_shunt", False)),
                disp[-1],
            )
            left_dev = next(
                (d for d in disp if not getattr(d, "is_shunt", False)),
                disp[0],
            )
            tip = ws.cell(
                row, 2,
                f"左={left_dev.ref}  右={right_dev.ref}  "
                f"hops={len(disp)}  copad腿={n_copad}",
            )
            tip.font = Font(size=9, color="FF666666")

        row = dev_row + 3

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 12
    for c in range(2, max_col + 1):
        # path device columns may overlap letter with table — widen path cols only if larger
        letter = get_column_letter(c)
        cur = ws.column_dimensions[letter].width or 8
        ws.column_dimensions[letter].width = max(cur, 12)


def export_path_preview(
    paths: list[Path],
    filename: str,
    copad_pairs: Optional[list[CoPadPair]] = None,
    copad_structures: Optional[list[CoPadStructure]] = None,
    schematic_res=None,
    signal_infos=None,
    colay_structures: Optional[list[CoLayStructure]] = None,
) -> str:
    """【阶段 A 中间产物】路径 + COPAD/COLAY 预览 + 可选 50R 覆盖审计页。"""
    pairs = list(copad_pairs or getattr(build_paths, "last_copad_pairs", ()) or ())
    structures = list(
        copad_structures
        or getattr(build_paths, "last_copad_structures", ())
        or ()
    )
    if not structures and pairs:
        structures = build_copad_structures(pairs)
    colays = list(
        colay_structures
        or getattr(build_paths, "last_colay_structures", ())
        or ()
    )
    if not colays and schematic_res is not None:
        colays = build_colay_structures(
            signal_infos=signal_infos,
            schematic_res=schematic_res,
            copad_structures=structures,
        )
    by_ant = group_paths_by_ant(paths)

    # 50R 覆盖：独立子能力，不改路径搭建；需 schematic_res 才能审计
    coverage_rows = []
    coverage_note = "50R覆盖: 未检查（未传入 schematic_res）"
    _attach_50r = None
    _summarize_50r = None
    if schematic_res is not None:
        from refnum.coverage_50r import (
            attach_50r_coverage_sheet,
            audit_50r_path_coverage,
            summarize_coverage,
        )
        _attach_50r = attach_50r_coverage_sheet
        _summarize_50r = summarize_coverage
        coverage_rows = audit_50r_path_coverage(
            schematic_res,
            paths,
            signal_infos,
            copad_structures=structures,
            colay_structures=colays,
        )
        coverage_note = _summarize_50r(coverage_rows).get(
            "note", coverage_note,
        )

    wb = Workbook()
    overview = wb.active
    overview.title = "总览"
    _write_overview(
        overview, paths, by_ant, pairs, structures,
        coverage_note=coverage_note,
        colay_structures=colays,
    )

    if _attach_50r is not None:
        _attach_50r(
            wb,
            paths,
            schematic_res=schematic_res,
            signal_infos=signal_infos,
            coverage_rows=coverage_rows,
            sheet_index=1,
            copad_structures=structures,
            colay_structures=colays,
        )

    if not by_ant:
        ws = wb.create_sheet("无路径")
        ws.cell(1, 1, "build_paths 结果为空 — 检查 reverse_edges / ANT 号")
    else:
        for ant in sorted(by_ant.keys(), key=sheet_sort_key):
            title = str(ant)[:31]
            ws = wb.create_sheet(title)
            _write_ant_sheet(
                ws, str(ant), by_ant[ant], structures,
                colay_structures=colays,
                schematic_res=schematic_res,
                signal_infos=signal_infos,
            )

    from refnum.xlsx_io import save_workbook

    return save_workbook(wb, filename)

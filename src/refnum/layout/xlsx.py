"""功能5 阶段 B — 着色 + 写 Excel。

每 Sheet：L3 → 非共享单格 → L6 →（L4/L5 Co-pad 整块插入）→ 可见唯一 → 链路间空行 → 落盘。
"""
from __future__ import annotations

from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from refnum.layout.model import MergeRange, PlacedCell, SheetGrid, SheetPlan
from refnum.layout.place import (
    place_sheet,
    polish_chain_visuals,
    reflow_main_rows_path_order,
)
from refnum.layout.unique import (
    assert_worksheet_unique_device_values,
    collapse_cross_column_copies,
    drop_empty_content_rows,
    enforce_non_shared_single_cell,
    finalize_visible_unique_after_merge,
    restore_fbrx_sample_chain_devices,
)
from refnum.layout.merge import (
    coalesce_adjacent_merge_spans,
    apply_main_chain_merges,
    finalize_merges_after_unique,
    l2_merge_rehome_loop,
)
from refnum.path import CoPadStructure, Device

# 链路与链路之间空行数（boss）
LINK_GAP_ROWS = 2
# GPS 树：支路紧凑（夹心块与邻支之间 1 空行，非 2）
GPS_LINK_GAP_ROWS = 1
# Co-pad 块装配：整块插入双支路（A1/B1/C1/D1/E1/F1），保护主链合并柱
ENABLE_COPAD_BLOCKS = True

_THIN = Side(style="thin", color="FF000000")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_FILL_TX = PatternFill(fill_type="solid", fgColor="FFFFC000")
_FILL_RX = PatternFill(fill_type="solid", fgColor="FF92D050")
_FILL_IC = PatternFill(fill_type="solid", fgColor="FFD9D9D9")
_FILL_COPAD = PatternFill(fill_type="solid", fgColor="FFFFFF00")
_FILL_SHUNT = PatternFill(fill_type="solid", fgColor="FF9DC3E6")
_FILL_HDR = PatternFill(fill_type="solid", fgColor="FF4472C4")
_FILL_LABEL = PatternFill(fill_type="solid", fgColor="FFE7E6E6")
_FILL_GRAY = PatternFill(fill_type="solid", fgColor="FFBFBFBF")  # 对齐顶格连线
_FONT_HDR = Font(color="FFFFFFFF", bold=True)
_FONT_BOLD = Font(bold=True)
_FONT_VALUE = Font(size=11, color="FF1F4E79")


def cell_fill(
    dev: Optional[Device],
    direction: str,
    *,
    kind: str = "device",
) -> PatternFill:
    """颜色：Co-pad / Co-lay / JUMP 网位号 黄；其余按正常链路。

    优先级：Co-pad/Co-lay/JUMP 黄 > 并联浅蓝 > IC 灰 > TX 橙 / RX 绿。
    JUMP 腿走 is_jump_attach。
    """
    if kind in ("copad_tag", "copad_leg"):
        return _FILL_COPAD
    if dev is None:
        return _FILL_LABEL
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
    d = (direction or "").upper()
    if d == "RX":
        return _FILL_RX
    return _FILL_TX


def _style(cell, fill=None, header=False) -> None:
    cell.border = _BORDER
    cell.alignment = _CENTER
    if header:
        cell.font = _FONT_HDR
        cell.fill = _FILL_HDR
    elif fill is not None:
        cell.fill = fill


def _copad_blk_role(rid: str) -> tuple[str | None, str | None]:
    """解析 COPAD 行 → (block_id, role)；role 为 b1/b2；非夹心支路 → (None, None)。"""
    s = str(rid or "")
    if "|COPADBLK" not in s:
        return None, None
    blk: str | None = None
    role: str | None = None
    for p in s.split("|"):
        if p.startswith("COPADBLK"):
            blk = p
        if p in ("b1", "b2"):
            role = p
    return blk, role


def _skip_gap_between(
    ids: list[str],
    i: int,
    grid: SheetGrid | None = None,
) -> bool:
    """是否跳过 ids[i-1] 与 ids[i] 之间的链路空行。

    仅 **同一夹心三行内部**（b1–宿主–b2）不插空行，使夹心贴宿主主链。
    夹心上下支与 **其它链路**（含 merge.trunk 其它 owner）之间仍插正常空行。
    并柱可以跨这些空行。
    """
    if i <= 0 or i >= len(ids):
        return False
    a, b = str(ids[i - 1]), str(ids[i])
    ba, ra = _copad_blk_role(a)
    bb, rb = _copad_blk_role(b)
    branch_homes: set[str] = set()
    hosts: set[str] = set()
    if grid is not None:
        for h, pair in (getattr(grid, "copad_branch_rows_by_host", None) or {}).items():
            hosts.add(h)
            branch_homes.update(pair)
    if a in branch_homes and (b in branch_homes or b in hosts):
        return True
    if a in hosts and b in branch_homes:
        return True

    # b1 → 宿主：下一行须是同块 b2
    if ra == "b1" and rb is None:
        if i + 1 < len(ids):
            bn, rn = _copad_blk_role(ids[i + 1])
            if bn == ba and rn == "b2":
                return True
        return False

    # 宿主 → b2：上一行须是同块 b1
    if rb == "b2" and ra is None:
        if i >= 2:
            bp, rp = _copad_blk_role(ids[i - 2])
            if bp == bb and rp == "b1":
                return True
        return False

    # 同块两支路异常相邻：仍不插
    if ba and bb and ba == bb:
        return True

    return False


def insert_link_gaps(grid: SheetGrid, gap: int = LINK_GAP_ROWS) -> SheetGrid:
    """链路与链路之间插入 gap 条空行（不占器件）。

    竖合并跨链路时包含中间空行。数值行是否再往下盖，见 cover_value_rows。
    Co-pad 夹心三行内部不插空行；夹心外缘与其它链路插正常空行。
    值仍在顶格；空行无器件字。
    """
    if gap <= 0 or len(grid.row_ids) <= 1:
        return grid

    old_ids = list(grid.row_ids)
    old_to_new: dict[int, int] = {}
    new_ids: list[str] = []
    new_cells: dict[str, dict[int, PlacedCell]] = {}
    new_labels: dict[str, str] = {}

    for i, rid in enumerate(old_ids):
        if i > 0 and not _skip_gap_between(old_ids, i, grid):
            for g in range(gap):
                gap_id = f"{grid.key}|__gap__{i}_{g}"
                new_ids.append(gap_id)
                new_labels[gap_id] = ""
                new_cells[gap_id] = {}
        old_to_new[i] = len(new_ids)
        new_ids.append(rid)
        new_labels[rid] = grid.labels.get(rid, "")
        new_cells[rid] = dict(grid.cells.get(rid) or {})

    kept: list[MergeRange] = []
    for m in grid.merges or []:
        if m.row_start not in old_to_new or m.row_end not in old_to_new:
            continue
        ns, ne = old_to_new[m.row_start], old_to_new[m.row_end]
        if ne <= ns:
            continue
        kept.append(
            MergeRange(
                ref=m.ref,
                col=m.col,
                row_start=ns,
                row_end=ne,
                row_ids=tuple(new_ids[r] for r in range(ns, ne + 1)),
            )
        )

    grid.row_ids = new_ids
    grid.labels = new_labels
    grid.cells = new_cells
    grid.merges = kept
    return grid


def _value_cols(grid: SheetGrid, row_i: int, values: dict[str, str]) -> set[int]:
    """这一行上会写出数值的列。"""
    rid = grid.row_ids[row_i]
    cols: set[int] = set()
    for col, placed in (grid.cells.get(rid) or {}).items():
        if placed is None or placed.kind == "gray":
            continue
        ref = str(placed.ref or "").strip()
        if ref and ref in values:
            cols.add(int(col))
    return cols


def _is_rlct_ref(ref: str) -> bool:
    """匹配件 R/L/C/T（位号字母后紧跟数字）。下面没值就空着，不往下盖。

    LPF 是滤波器，不算匹配件。TRIP、LNA 也不是。
    """
    ru = str(ref or "").strip().upper()
    if not ru or ru.startswith("LPF"):
        return False
    return len(ru) >= 2 and ru[0] in "CRLT" and ru[1].isdigit()


def _can_cover_cell(placed, values: dict[str, str]) -> bool:
    """自身没有数值的位号或频段名。灰格保持一格。R/L/C/T 不往下盖。"""
    if placed is None or placed.kind == "gray":
        return False
    if placed.kind not in ("label", "device", "copad_leg", "copad_tag"):
        return False
    ref = str(placed.ref or "").strip()
    if _is_rlct_ref(ref):
        return False
    return not (ref and ref in values)


def _below_occupied(grid: SheetGrid, row_i: int, col: int) -> bool:
    """下一格已有器件或灰格。这种列不能往下盖，避免吞掉 Co-pad 支路上的位号。"""
    nxt = row_i + 1
    if nxt >= len(grid.row_ids):
        return True
    row = grid.cells.get(grid.row_ids[nxt]) or {}
    return row.get(int(col)) is not None


def cover_value_rows(grid: SheetGrid, values: dict[str, str] | None) -> SheetGrid:
    """没有自身数值的格子，只要这一行左边或右边有数值，就往下盖住数值行。

    单个器件和竖合并的底端都算，A 列频段名、B 列器件同样。
    下一行若是 Co-pad 支路，只盖本列下一格是空的；下一格已有位号则停住。
    R/L/C/T 不盖，下面没值就空着；LPF 仍盖。
    灰格保持一格。自身有数值的格子留出下面那一格。分隔空行不进合并。
    """
    if not values or not grid.row_ids:
        return grid

    def beside(row_i: int, col: int) -> bool:
        return any(c != int(col) for c in _value_cols(grid, row_i, values))

    last = len(grid.row_ids) - 1
    if "|__gap__" not in str(grid.row_ids[last]):
        needs = False
        if beside(last, -1):
            for col, placed in (grid.cells.get(grid.row_ids[last]) or {}).items():
                if _can_cover_cell(placed, values) and beside(last, int(col)):
                    needs = True
                    break
            if not needs:
                for merge in grid.merges or []:
                    if (
                        int(merge.row_end) == last
                        and beside(last, int(merge.col))
                        and not _is_rlct_ref(merge.ref)
                    ):
                        needs = True
                        break
        if needs:
            gap_id = f"{grid.key}|__gap__end_0"
            grid.row_ids.append(gap_id)
            grid.labels[gap_id] = ""
            grid.cells[gap_id] = {}

    covered: set[tuple[int, int]] = set()
    rebuilt: list[MergeRange] = []
    for merge in grid.merges or []:
        ne = int(merge.row_end)
        if (
            ne > int(merge.row_start)
            and beside(ne, int(merge.col))
            and not _below_occupied(grid, ne, int(merge.col))
            and not _is_rlct_ref(merge.ref)
        ):
            ne += 1
        for row_i in range(int(merge.row_start), ne + 1):
            covered.add((row_i, int(merge.col)))
        rebuilt.append(
            MergeRange(
                ref=merge.ref,
                col=merge.col,
                row_start=merge.row_start,
                row_end=ne,
                row_ids=tuple(grid.row_ids[r] for r in range(int(merge.row_start), ne + 1)),
            )
        )

    for row_i, rid in enumerate(grid.row_ids):
        if "|__gap__" in str(rid):
            continue
        for col, placed in (grid.cells.get(rid) or {}).items():
            col = int(col)
            if (row_i, col) in covered:
                continue
            if not _can_cover_cell(placed, values) or not beside(row_i, col):
                continue
            if _below_occupied(grid, row_i, col):
                continue
            rebuilt.append(
                MergeRange(
                    ref=str(placed.ref or ""),
                    col=col,
                    row_start=row_i,
                    row_end=row_i + 1,
                    row_ids=(rid, grid.row_ids[row_i + 1]),
                )
            )
            covered.add((row_i, col))
            covered.add((row_i + 1, col))

    grid.merges = rebuilt
    return grid


def _apply_excel_merges(ws, grid, excel_row0: int = 2) -> int:
    """把 grid.merges 落到 openpyxl；返回合并个数。

    写表安全网（原则2）：合并区内若已有**其它器件**字，禁止清空、并截断合并，
    避免合并区误跨其它器件行时抹掉已有字。
    """
    from openpyxl.cell.cell import MergedCell

    # 同列合并去重 / 按跨度从大到小，避免重叠写值
    by_col: dict[int, list] = {}
    for m in grid.merges or []:
        if m.row_end <= m.row_start:
            continue
        by_col.setdefault(m.col, []).append(m)
    n = 0
    for c, ms in by_col.items():
        ms_sorted = sorted(
            ms, key=lambda m: (m.row_end - m.row_start, m.row_start), reverse=True,
        )
        used: list[tuple[int, int]] = []
        for m in ms_sorted:
            r1 = excel_row0 + m.row_start
            r2 = excel_row0 + m.row_end
            if any(not (r2 < a or r1 > b) for a, b in used):
                continue  # 与已合并区重叠则跳过
            ref = str(m.ref or "").strip()
            # 截断：遇其它器件则合并止于其上一行
            end = r1
            for rr in range(r1, r2 + 1):
                cell = ws.cell(rr, c)
                if isinstance(cell, MergedCell):
                    end = rr
                    continue
                val = cell.value
                vs = str(val).strip() if val is not None else ""
                if vs and vs != ref:
                    break
                end = rr
            if end <= r1:
                continue
            r2 = end
            top = ws.cell(r1, c)
            for rr in range(r1 + 1, r2 + 1):
                cell = ws.cell(rr, c)
                if isinstance(cell, MergedCell):
                    continue
                val = cell.value
                vs = str(val).strip() if val is not None else ""
                if vs and vs != ref:
                    continue
                cell.value = None
            try:
                ws.merge_cells(
                    start_row=r1, start_column=c, end_row=r2, end_column=c,
                )
                n += 1
                used.append((r1, r2))
            except Exception as exc:
                raise RuntimeError(
                    f"[handbook.xlsx] merge 失败 ref={m.ref} "
                    f"rows={r1}-{r2} col={c}: {exc}"
                ) from exc
            top.alignment = _CENTER
            top.border = _BORDER
    return n


def _detach_merge(ws, row: int, col: int):
    """把 (row, col) 从竖合并里抠出来，上下两截仍各自合并。"""
    from openpyxl.cell.cell import MergedCell

    cell = ws.cell(row, col)
    if not isinstance(cell, MergedCell):
        return cell
    found = None
    for merged in list(ws.merged_cells.ranges):
        if (
            merged.min_row <= row <= merged.max_row
            and merged.min_col <= col <= merged.max_col
        ):
            found = merged
            break
    if found is None:
        return ws.cell(row, col)
    ws.unmerge_cells(
        start_row=found.min_row,
        start_column=found.min_col,
        end_row=found.max_row,
        end_column=found.max_col,
    )
    if found.min_row < row:
        ws.merge_cells(
            start_row=found.min_row,
            start_column=found.min_col,
            end_row=row - 1,
            end_column=found.max_col,
        )
    if row < found.max_row:
        ws.merge_cells(
            start_row=row + 1,
            start_column=found.min_col,
            end_row=found.max_row,
            end_column=found.max_col,
        )
    return ws.cell(row, col)


def _cell_text(cell) -> str:
    if cell.value is None:
        return ""
    return str(cell.value).strip()


def stamp_component_values(ws, values: dict[str, str] | None) -> list[str]:
    """把器件值写到位号正下方那一格，只给写上值的格子画四边框。

    灰格、标签、以及没有值的器件正下方不画框。下方已有字则不覆盖、不补框。
    竖合并只在这一格确实要写值时才拆开。必须在位号唯一性检查之后调用。
    """
    if not values:
        return []
    from openpyxl.cell.cell import MergedCell

    max_row = ws.max_row or 0
    max_col = ws.max_column or 0
    hits: dict[tuple[int, int], str] = {}
    for row in range(1, max_row + 1):
        for col in range(1, max_col + 1):
            cell = ws.cell(row, col)
            if isinstance(cell, MergedCell):
                continue
            ref = _cell_text(cell)
            if ref in values:
                hits[(row, col)] = ref
    notes: list[str] = []
    touched_rows: set[int] = set()
    for row, col in sorted(hits):
        below = ws.cell(row + 1, col)
        if isinstance(below, MergedCell):
            below = _detach_merge(ws, row + 1, col)
        ref = hits[(row, col)]
        existing = _cell_text(below)
        if existing:
            notes.append(f"{ref} 下方已有「{existing}」，未贴值")
            continue
        below.value = values[ref]
        below.font = _FONT_VALUE
        below.border = _BORDER
        below.alignment = _CENTER
        touched_rows.add(row + 1)
    for row in touched_rows:
        height = ws.row_dimensions[row].height or 0
        if height < 18:
            ws.row_dimensions[row].height = 18
    return notes


def compose_sheet_grid(
    plan: SheetPlan,
    copad_structures: Optional[list[CoPadStructure]] = None,
    signal_infos=None,
) -> tuple[SheetPlan, SheetGrid]:
    """单页手册网格：unique → 回流 → 接柱 → 按段收字 → 填灰。不含链路空行。"""
    plan = l2_merge_rehome_loop(plan)
    grid = place_sheet(plan)
    grid = enforce_non_shared_single_cell(
        plan, grid, defer_public_suffix=True,
    )
    grid = apply_main_chain_merges(plan, grid)
    if ENABLE_COPAD_BLOCKS and copad_structures:
        from refnum.layout.copad import apply_copad_to_grid

        grid = apply_copad_to_grid(
            plan, grid, copad_structures, signal_infos=signal_infos,
        )
    grid = finalize_visible_unique_after_merge(plan, grid)
    grid = reflow_main_rows_path_order(plan, grid)
    grid = restore_fbrx_sample_chain_devices(plan, grid)
    # 先删空壳行、再算柱：**柱必须在最终行序上算**。反过来的话，抽字后只剩标签
    # 的行还在 grid 里，会把一根柱切成两段（留下的单行段写不成合并区，那一行就
    # 被隔在柱外）。
    # `drop_empty_content_rows` 读 merges 只为判「FBRX 取样空壳是否落在柱内」，
    # 用上一轮（`apply_main_chain_merges` / 夹心后 refresh）的柱足够。
    grid = drop_empty_content_rows(grid, plan=plan)
    grid = finalize_merges_after_unique(
        plan, grid, signal_infos=signal_infos,
    )
    # 兜底：同 ref 同列若仍留下紧邻的两段（中间已无任何行），本就是一根柱；
    # 留下单行段那一行写不成合并区、会被隔在柱外。
    grid = coalesce_adjacent_merge_spans(grid)
    # 接柱改段数后必须再收字：一段一钉顶。接柱前那次 unique 按旧段数
    # 留下的下段锚字，填灰会当成 hop 两端画出假平行干。
    # 真切断（中间仍隔行）接不上，仍每段一份。钉顶只搬字，不改柱高。
    grid = finalize_visible_unique_after_merge(plan, grid)
    grid = polish_chain_visuals(grid, plan)
    # 去灰列会平移列号；回流也可能把字丢到第二列。写表前再收一次跨列。
    grid = finalize_visible_unique_after_merge(plan, grid)
    return plan, grid


def write_handbook_xlsx(
    plans: list[SheetPlan],
    filename: str,
    *,
    build_id: str,
    layout_stage: str,
    path_count: int = 0,
    paths=None,
    copad_structures: Optional[list[CoPadStructure]] = None,
    signal_infos=None,
    schematic_res=None,
    pin_parser=None,
    component_values: dict[str, str] | None = None,
) -> str:
    """写出手册式 refnum xlsx（主链 + Co-pad 块 + 可选 50R 覆盖页）。"""
    wb = Workbook()
    blank = wb.active
    blank.title = "_blank"
    visible_refs: set[str] = set()
    from refnum.coverage_50r import collect_visible_grid_refs
    if not plans:
        ws = wb.create_sheet("无路径")
        ws["A1"] = "无 Path 可构图 — 检查阶段 A build_paths"
    else:
        for plan in plans:
            plan, grid = compose_sheet_grid(
                plan,
                copad_structures=copad_structures,
                signal_infos=signal_infos,
            )
            gap_n = (
                GPS_LINK_GAP_ROWS
                if str(plan.key or "").upper() == "GPS"
                else LINK_GAP_ROWS
            )
            grid = insert_link_gaps(grid, gap_n)
            collapse_cross_column_copies(grid, plan)
            cover_value_rows(grid, component_values)
            visible_refs |= collect_visible_grid_refs([grid])

            title = str(plan.key)[:31] or "Sheet"
            ws = wb.create_sheet(title)
            id_to_row = {r.row_id: r for r in plan.rows}
            for r_i, row_id in enumerate(grid.row_ids, start=1):
                if row_id.startswith(f"{plan.key}|__gap__") or "|__gap__" in row_id:
                    # 空行：不写内容，仅占行距
                    continue
                layout_row = id_to_row.get(row_id)
                direction = (
                    layout_row.path.direction if layout_row is not None else "TX"
                ) or "TX"
                for c, placed in (grid.cells.get(row_id) or {}).items():
                    if placed.kind == "gray":
                        cell = ws.cell(r_i, c, None)
                        _style(cell, fill=_FILL_GRAY)
                        continue
                    cell = ws.cell(r_i, c, placed.ref or None)
                    if placed.kind == "label":
                        _style(cell, fill=_FILL_LABEL)
                        cell.font = _FONT_BOLD
                    elif placed.kind in ("copad_tag", "copad_leg"):
                        _style(
                            cell,
                            fill=cell_fill(placed.device, direction, kind=placed.kind),
                        )
                        cell.font = _FONT_BOLD
                    else:
                        _style(
                            cell,
                            fill=cell_fill(placed.device, direction, kind="device"),
                        )
            _apply_excel_merges(ws, grid, excel_row0=1)
            assert_worksheet_unique_device_values(ws, sheet=title)
            ws.column_dimensions["A"].width = 18
            for c in range(2, grid.max_col + 1):
                ws.column_dimensions[get_column_letter(c)].width = 11
            for r_i in range(1, len(grid.row_ids) + 1):
                rid = grid.row_ids[r_i - 1]
                if "|__gap__" in rid:
                    ws.row_dimensions[r_i].height = 10
                else:
                    ws.row_dimensions[r_i].height = 18
            for note in stamp_component_values(ws, component_values):
                print(f"[功能5] {title}: {note}")

    # 50R 覆盖：与 path_preview 同源审计，挂在最终手册内便于交付核对
    if schematic_res is not None:
        try:
            from refnum.coverage_50r import attach_50r_coverage_sheet

            attach_50r_coverage_sheet(
                wb,
                paths or [],
                schematic_res=schematic_res,
                signal_infos=signal_infos,
                sheet_index=1,
                copad_structures=copad_structures,
                covered_refs=visible_refs,
            )
        except Exception as e:
            print(f"[功能5][最终产物·handbook] 50R覆盖页失败（手册仍写出）: {e}")
        # others：50R 未进主图的位号，按有名网（SRS/FBRX…）分类并串链
        try:
            from refnum.others import attach_others_sheet

            attach_others_sheet(
                wb,
                paths or [],
                schematic_res=schematic_res,
                signal_infos=signal_infos,
                pin_parser=pin_parser,
                copad_structures=copad_structures,
                covered_refs=visible_refs,
            )
            if component_values and "others" in wb.sheetnames:
                for note in stamp_component_values(wb["others"], component_values):
                    print(f"[功能5] others: {note}")
        except Exception as e:
            print(f"[功能5][最终产物·handbook] others页失败（手册仍写出）: {e}")

    if blank in wb.worksheets and len(wb.worksheets) > 1:
        wb.remove(blank)

    from refnum.xlsx_io import save_workbook

    return save_workbook(wb, filename)

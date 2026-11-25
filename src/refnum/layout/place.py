"""功能5 阶段 B — L3 主链投放与共享列对齐 + 视觉灰格收口。

规则：
1. 显示序左 TRX→右，行内先后不变
2. 共享件（左邻≥2 且非 RLCT/ANT，折叠后跨≥2 行）同列对齐
3. 不同共享件：owner 行跨度不重叠时可**复用同一列**（压缩横向宽度）
4. 共享列对齐时中间留白（不在 place 阶段铺灰）
5. 视觉填灰见 fill_chain_continuity（真实链路段定义）

前置：L2 已尽量使 owner 相邻。本层之后才 L6 竖合并。
"""
from __future__ import annotations

from typing import Optional

from refnum.layout.copad import sandwich_reserved_bands
from refnum.layout.merge_span import is_main_content_row_id
from refnum.layout.model import (
    UNIQUE_BEARING_KINDS,
    VISIBLE_DEVICE_KINDS,
    LayoutRow,
    MergeRange,
    PlacedCell,
    SheetGrid,
    SheetPlan,
)
from refnum.layout.share import (
    is_pa_lna_device,
    is_non_merge_align_ref,
    is_layout_shared,
    should_vertical_merge,
    display_ref_seq,
)


def _multi_owner_refs(plan: SheetPlan) -> dict[str, frozenset[str]]:
    """L3 对齐锚点 = 实体并柱资格 + 跨≥2 行 + 非 RLCT/ANT。

    与 L6 同一 ``should_vertical_merge``。匹配/公尾 T、C 即使左邻种类≥2
    也不是合并器件（COLAY 短链会让公尾 T 左邻变成 FIL vs C）。
    """
    lnc = plan.left_neighbor_count or {}
    out: dict[str, frozenset[str]] = {}
    for ref, owners in (plan.share_map or {}).items():
        if len(owners) < 2:
            continue
        if not should_vertical_merge(plan, ref, lnc):
            continue
        dev = None
        for row in plan.rows:
            for d in row.devices:
                if str(getattr(d, "ref", "") or "") == ref:
                    dev = d
                    break
            if dev is not None:
                break
        if is_non_merge_align_ref(ref, dev):
            continue
        out[ref] = owners
    return out


def _row_seq(row: LayoutRow) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for dev in row.devices:
        ref = str(getattr(dev, "ref", "") or "").strip()
        if ref:
            out.append((ref, dev))
    return out


def _nat_index(seq: list[tuple[str, object]], ref: str) -> int:
    for i, (r, _) in enumerate(seq):
        if r == ref:
            return i
    return -1


def _cursor_before_ref(
    seq: list[tuple[str, object]],
    ref: str,
    multi_col: dict[str, int],
    start_col: int = 2,
) -> int:
    """已定 multi_col 下，落到 ref 之前时的列游标。"""
    cursor = start_col
    for r, _dev in seq:
        if r == ref:
            break
        if r in multi_col:
            target = multi_col[r]
            if cursor < target:
                cursor = target
            cursor = target + 1
        else:
            cursor += 1
    return cursor


def _owner_row_span(
    order_ids: list[str],
    owners: frozenset[str],
) -> tuple[int, int] | None:
    """owner 在当前行序上的 [min, max] 闭区间下标。"""
    pos = [i for i, rid in enumerate(order_ids) if rid in owners]
    if not pos:
        return None
    return (pos[0], pos[-1])


def _spans_conflict(
    a: tuple[int, int] | None,
    b: tuple[int, int] | None,
    owners_a: frozenset[str],
    owners_b: frozenset[str],
) -> bool:
    """两共享件能否同列：同行共现 或 行跨度重叠 → 冲突。"""
    if owners_a & owners_b:
        return True
    if a is None or b is None:
        return False
    # 闭区间重叠
    return not (a[1] < b[0] or b[1] < a[0])


def assign_shared_columns(
    plan: SheetPlan,
    multi: dict[str, frozenset[str]],
    *,
    fixed: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    """为共享 ref 分配全局列（列从 2 起）。

    列位 = 各 owner 前缀游标的 max，一遍收齐。
    **列可复用**：若两共享件 owner 无交集且行跨度不重叠，可占同一列。

    ``fixed``：已钉死的列（如 PA/LNA 统一列）——不再重算，
    但作为前缀游标参与后续共享件列号。
    """
    if not multi and not fixed:
        return {}

    multi_col: dict[str, int] = {
        str(k): int(v) for k, v in (fixed or {}).items() if str(k).strip()
    }
    if not multi:
        return multi_col

    seqs = {row.row_id: _row_seq(row) for row in plan.rows}
    order_ids = [row.row_id for row in plan.rows]
    spans = {
        ref: _owner_row_span(order_ids, owners) for ref, owners in multi.items()
    }

    def _avg_nat(ref: str) -> float:
        vals = []
        for rid in multi[ref]:
            seq = seqs.get(rid) or []
            i = _nat_index(seq, ref)
            if i >= 0:
                vals.append(float(i))
        return sum(vals) / len(vals) if vals else 0.0

    ordered_refs = sorted(
        (r for r in multi.keys() if r not in multi_col),
        key=lambda r: (_avg_nat(r), r),
    )
    # col → 已占用的 (ref, span)
    col_occ: dict[int, list[tuple[str, tuple[int, int] | None]]] = {}
    for ref, col in multi_col.items():
        owners = multi.get(ref) or frozenset()
        col_occ.setdefault(int(col), []).append((ref, spans.get(ref)))

    for ref in ordered_refs:
        need = 2
        for rid in multi[ref]:
            seq = seqs.get(rid) or []
            if _nat_index(seq, ref) >= 0:
                need = max(
                    need,
                    _cursor_before_ref(seq, ref, multi_col, start_col=2),
                )
        span = spans.get(ref)
        owners = multi[ref]
        col = need
        while True:
            conflict = False
            for other_ref, other_span in col_occ.get(col, ()):
                other_owners = multi.get(other_ref) or frozenset()
                if _spans_conflict(span, other_span, owners, other_owners):
                    conflict = True
                    break
            if not conflict:
                break
            col += 1
        multi_col[ref] = col
        col_occ.setdefault(col, []).append((ref, span))

    return multi_col


def _frontend_pa_lna_ref(row: LayoutRow) -> Optional[str]:
    """本行应进「PA/LNA 列」的前端有源位号。

    - BAND：显示序行首 PA/LNA
    - GPS：行内第一个 PA/LNA（行首常为 TRX）
    - TRX_PA_LNA：显示序最右 PA/LNA
    """
    seq = _row_seq(row)
    if not seq:
        return None
    kind = (getattr(row.path, "kind", None) or "BAND").upper()
    if kind == "TRX_PA_LNA":
        for ref, dev in reversed(seq):
            if is_pa_lna_device(dev):
                return ref
        return None
    # BAND / GPS / 其它：从左往右第一个 PA/LNA
    for ref, dev in seq:
        if is_pa_lna_device(dev):
            return ref
    return None


def _assign_pa_lna_column(
    plan: SheetPlan,
    multi_col: dict[str, int],
) -> dict[str, int]:
    """BAND 前端 PA/LNA 强制同一列（不同位号也同列；合并切断则分块显示）。

    列号 = 各行「第一颗 PA/LNA 前的器件数」max + 2。

    **GPS 排除**：树状 L1/L5 各有不同 LNA，强扭同列会打散共享 FIL/夹心。
    """
    if str(plan.key or "").upper() == "GPS":
        return dict(multi_col)
    out = dict(multi_col)
    max_prefix = 0
    pa_lna_refs: set[str] = set()
    for row in plan.rows or []:
        kind = (getattr(row.path, "kind", None) or "BAND").upper()
        if kind in ("GPS", "TRX_PA_LNA"):
            continue
        seq = _row_seq(row)
        target_ref = _frontend_pa_lna_ref(row)
        if not target_ref:
            continue
        prefix = 0
        for ref, _dev in seq:
            if ref == target_ref:
                break
            prefix += 1
        max_prefix = max(max_prefix, prefix)
        pa_lna_refs.add(target_ref)
    if not pa_lna_refs:
        return out
    need = 2 + max_prefix
    for ref in pa_lna_refs:
        out[ref] = need
    for ref, c in list(out.items()):
        if ref not in pa_lna_refs and int(c) == need:
            out[ref] = need + 1
    return out


def _pack_private_runs_to_following_shared(
    col_map: dict[int, PlacedCell],
    seq: list[tuple[str, object]],
    multi_col: dict[str, int],
    *,
    band: str = "",
) -> dict[int, PlacedCell]:
    """私有段拓扑附着（通用）。

    规则：
      1) **左侧已有共享件**（主链中段）：
         私有段**左贴**左共享列右侧，保持 path 相邻，禁止右甩到远端共享列
         （否则中段匹配会从左共享列被甩到右共享列前 / 更远的 FIL 前）。
      2) **仅右侧有共享**（短链/侧支/贯通只共享右端 FIL）：
         整段右贴该共享列左侧，落在 FIL 旁而非沉到 TRX 列。
    """
    if not multi_col or not seq:
        return col_map
    refs = [r for r, _ in seq]
    # 当前落位
    ref_pl: dict[str, PlacedCell] = {}
    placed_col: dict[str, int] = {}
    for c, pl in col_map.items():
        if pl is None or not str(pl.ref or "").strip():
            continue
        if pl.kind not in VISIBLE_DEVICE_KINDS:
            continue
        ref_pl[str(pl.ref)] = pl
        placed_col[str(pl.ref)] = int(c)

    moves: dict[str, int] = {}
    i = 0
    n = len(refs)
    while i < n:
        if refs[i] in multi_col:
            i += 1
            continue
        run_start = i
        while i < n and refs[i] not in multi_col:
            i += 1
        if i >= n:
            break  # 尾私有：无右侧共享锚，保持原位
        multi_ref = refs[i]
        mcol = int(multi_col[multi_ref])
        # 实际落点优先于 multi_col 名义列
        mcol = int(placed_col.get(multi_ref, mcol))
        run = refs[run_start:i]
        left_shared = (
            refs[run_start - 1]
            if run_start > 0 and refs[run_start - 1] in multi_col
            else None
        )
        if left_shared is not None:
            # 主链中段：紧贴左共享右侧（path 连续）
            left_col = int(
                placed_col.get(left_shared, multi_col.get(left_shared, 2))
            )
            start = left_col + 1
        else:
            # 仅右共享：右贴 multi-1
            start = mcol - len(run)
            if start < 2:
                start = 2
        for j, r in enumerate(run):
            moves[r] = start + j
        # i 指向 multi，下一轮处理 multi
    if not moves:
        return col_map

    label = col_map.get(1)
    new_map: dict[int, PlacedCell] = {}
    if label is not None:
        new_map[1] = label

    def _put(col: int, pl: PlacedCell) -> None:
        c = int(col)
        while c in new_map:
            c += 1
        new_map[c] = pl

    # 先放未移动的共享/其它，再放移动私有（避免占位冲突）
    for c, pl in sorted(col_map.items()):
        if c < 2 or pl is None:
            continue
        ref = str(pl.ref or "").strip()
        if not ref or pl.kind not in VISIBLE_DEVICE_KINDS:
            continue
        if ref in moves:
            continue
        _put(c, pl)
    for ref, nc in sorted(moves.items(), key=lambda t: t[1]):
        pl = ref_pl.get(ref)
        if pl is not None:
            _put(nc, pl)
    return new_map


def place_sheet(plan: SheetPlan) -> SheetGrid:
    """L3 入口：顺序不变 + 共享同列 +（BAND）前端 PA/LNA 统一列。

    PA/LNA 同列后：同一位号连续 owner 竖并；中间被切断（他件占列/非 owner）
    则 L6 按连续段分块合并（见 merge contigOwner）。GPS 不做 PA/LNA 同列强制。

    拓扑附着：连续私有段贴向路径右侧共享列（只共享右端 FIL 的侧支/短链
    落在 FIL 旁，与主链共享 FIL 竖并；不沉到无关 TRX/远端 FIL 列）。

    **列分配顺序（硬）**：
      1) 先钉 PA/LNA 统一列
      2) 再以之为 fixed 前缀算其它共享列
      3) 投放时共享件**钉在 multi_col 目标列**（空则占目标；禁止「cursor 已过
         就顺延」把同 ref 拆到多列，否则 L6 len(cols)!=1 整段不并）
    """
    multi = _multi_owner_refs(plan)
    # 1) 仅算 PA/LNA 目标列
    pa_seed = _assign_pa_lna_column(plan, {})
    # 把 PA/LNA 也纳入 multi，便于 col_occ / 前缀游标
    for ref in pa_seed:
        if ref not in multi:
            owners = set()
            for row in plan.rows or []:
                if any(
                    str(getattr(d, "ref", "") or "").strip() == ref
                    for d in (row.devices or [])
                ):
                    owners.add(row.row_id)
            if len(owners) >= 1:
                multi[ref] = frozenset(owners)
    # 2) 共享列：PA/LNA fixed 后重算，保证长前缀 path 的 LPF/FIL 列够右
    multi_col = assign_shared_columns(plan, multi, fixed=pa_seed)
    multi_col = _assign_pa_lna_column(plan, multi_col)

    grid = SheetGrid(key=plan.key, row_ids=[], labels={})
    max_col = 1

    for row in plan.rows:
        grid.row_ids.append(row.row_id)
        grid.labels[row.row_id] = row.label
        col_map: dict[int, PlacedCell] = {
            1: PlacedCell(ref=row.label, kind="label"),
        }
        seq = _row_seq(row)
        # Pass A：共享件钉目标列
        for ref, dev in seq:
            if ref not in multi_col:
                continue
            target = int(multi_col[ref])
            place_at = target
            occ = col_map.get(place_at)
            if occ is not None and str(occ.ref or "").strip() not in ("", ref):
                # 两共享件同列冲突：顺延（assign 应已避免；兜底）
                while place_at in col_map:
                    place_at += 1
            col_map[place_at] = PlacedCell(ref=ref, device=dev, kind="device")
        # Pass B：私有件按 path 序填空位（贴右侧共享列由 pack 再收）
        cursor = 2
        for ref, dev in seq:
            if ref in multi_col:
                # 游标跳到该共享列右侧，便于后续私有顺排
                c = None
                for col, pl in col_map.items():
                    if (
                        pl is not None
                        and str(pl.ref or "").strip() == ref
                        and pl.kind in VISIBLE_DEVICE_KINDS
                    ):
                        c = col
                        break
                if c is not None and c + 1 > cursor:
                    cursor = c + 1
                continue
            while cursor in col_map:
                cursor += 1
            col_map[cursor] = PlacedCell(ref=ref, device=dev, kind="device")
            cursor += 1

        col_map = _pack_private_runs_to_following_shared(
            col_map,
            seq,
            multi_col,
            band=str(getattr(row.path, "band", "") or ""),
        )
        grid.cells[row.row_id] = col_map
        max_col = max(max_col, max(col_map.keys(), default=1))

    grid.max_col = max(max_col, 1)
    return grid

def _merge_entity_cols_by_row(grid: SheetGrid) -> dict[str, list[int]]:
    """合并器件的实体范围 → 按行索引。

    竖合并表达「同一物理器件跨多行」。实体范围 = 该合并覆盖的 owner 行
   （`MergeRange.row_ids`，与 L6 合并合法性一致：区间内每行必须是该 ref 的 owner）。

    返回：row_id → 本行**够得着**的合并实体列（升序、去重）。
    某行若不在某合并实体范围内，该合并列对本行链路**不是边界**。
    """
    by_row: dict[str, list[int]] = {}
    for m in grid.merges or []:
        if m.row_end <= m.row_start or m.col < 2:
            continue
        rids = list(m.row_ids) if m.row_ids else [
            grid.row_ids[ri]
            for ri in range(m.row_start, m.row_end + 1)
            if 0 <= ri < len(grid.row_ids)
        ]
        for rid in rids:
            by_row.setdefault(rid, []).append(m.col)
    return {rid: sorted(set(cols)) for rid, cols in by_row.items()}


def _copad_sandwich_mids_and_bands(
    grid: SheetGrid,
    plan: Optional[SheetPlan] = None,
) -> tuple[set[str], dict[str, set[int]]]:
    """夹心中层行 id 集合 + 每个中层对应的结构列带。

    结构：…|COPADBLKn|b1 , <host主链>, …|COPADBLKn|b2
    这些 host 的结构列中层不得落字，也不铺灰（夹心馅）。
    带 = 宿主 path 上连续支路腿的列，不是黄行 min..max 一条 mega-band。
    其它主链行照常填灰。
    """
    mid_ids: set[str] = set()
    mid_band: dict[str, set[int]] = {}
    seq_by_host: dict[str, list[str]] = {}
    if plan is not None:
        for row in plan.rows or []:
            if row is None or not getattr(row, "path", None):
                continue
            seq_by_host[str(row.row_id)] = [
                str(x) for x in display_ref_seq(row.path) if x
            ]
    ids = list(grid.row_ids)
    for i, rid in enumerate(ids):
        if "|COPADBLK" not in str(rid) or not str(rid).endswith("|b1"):
            continue
        if i + 2 >= len(ids):
            continue
        host = ids[i + 1]
        lo = ids[i + 2]
        if not str(lo).endswith("|b2"):
            continue
        if "|__gap__" in str(host):
            continue
        # 中层可以是贯通主链，也可以是插入的 COPADBLK mid。
        mid_ids.add(host)
        band: set[int] = set()
        for a, b in sandwich_reserved_bands(
            grid, host, seq_by_host.get(str(host)),
        ):
            band.update(range(int(a), int(b) + 1))
        mid_band[host] = band
    return mid_ids, mid_band


def fill_chain_continuity(
    grid: SheetGrid,
    plan: Optional[SheetPlan] = None,
) -> SheetGrid:
    """按「树状汇合 × 合并实体」填灰。

    领域不变量（boss · 树不是平行假链）：
      - **实体** = 过共享并柱之后的公共干（只在钉顶/宿主行上画）
      - 私有臂：本行**可见器件**之间铺灰，并最多接到**下一跳共享并柱**（汇入）
      - **A 列侧读序**：频段标签(A)→首器件**必须灰接**（树左端，从列 2
        铺到首件）。并柱格跳过不铺；并柱左边的空列仍须灰。
        「从覆盖右缘起」不得做成 A→并柱整段留白。
        「左侧全表空」= 可铺满，不是可空。他行同列有字不挡。
        禁 hop 只管两个可见件之间的中段。
      - 禁止：并柱后把公共干再灰穿到更远并柱（同列多并柱须按行段解析）
      - 合并实体范围：竖并只对覆盖行可见；本行不在实体范围 → 该列不是边界

    填灰步骤：
      1) 最左可见位号左侧 — 从列 2 铺到首件。并柱格 skip；并柱左边空列
         仍须灰。他行同列有字不挡。禁 hop 只管步骤 2/3。
      2) 相邻**可见**位号之间（中段已在本链画出则不铺）
      3) 最右可见 → path 上**下一个**本行够得着的共享并柱（仅一跳）：
         · 私有字 → 汇入第一共享柱
         · 并柱钉顶字 → 实体干上下一柱
         · 非钉顶的并柱覆盖行：不向右假延长公共干
         · 中段已在本链画出、本行既无实体又无并柱 → 禁止 hop
           去更远公共柱。不借其它 path。未画出的 path 件不挡。

    Co-pad：支路行只填本行可见件之间的空隙。夹心列带宿主中层不铺灰
    （馅 = 无位号、无灰），不问 path 中段有没有 FIL、有没有在别行成柱。
    宿主可见件到夹心带左/右缘的空列须灰接；本行这道夹心的腿/hub
    即使在旁主链成柱，也不得把整段当禁 hop 关掉。两块夹心之间
    不铺假公共干。对侧黄腿、其它频段占用同列不是本块中段。
    """
    entity_cols = _merge_entity_cols_by_row(grid)
    mid_ids, mid_band = _copad_sandwich_mids_and_bands(grid, plan)
    id_to_row = {r.row_id: r for r in (plan.rows or [])} if plan else {}
    id_to_idx = {rid: i for i, rid in enumerate(grid.row_ids or [])}

    # 同列可上下叠多个并柱（不同 FIL 段可共列）。
    # 禁止 col→单 ref 覆盖：必须按 (col, 行下标) 解析本行真正盖到的并柱。
    # (col, row_start, row_end, ref, pin_row)
    merge_segs: list[tuple[int, int, int, str, int]] = []
    for m in grid.merges or []:
        c = int(m.col)
        rs, re = int(m.row_start), int(m.row_end)
        if c < 2 or re <= rs:
            continue
        ref = str(m.ref or "").strip()
        if not ref:
            continue
        merge_segs.append((c, rs, re, ref, rs))

    def _merge_at(col: int, row_i: int | None) -> tuple[str, int] | None:
        """本行列上覆盖的并柱 (ref, pin_row)；无则 None。"""
        if row_i is None:
            return None
        for c, rs, re, ref, pin in merge_segs:
            if c == int(col) and rs <= int(row_i) <= re:
                return ref, pin
        return None

    def _paint(cells: dict, c: int, *, skip: set[int]) -> None:
        if c in skip:
            return
        pl = cells.get(c)
        if pl is not None and pl.ref and pl.kind != "gray":
            return
        cells[c] = PlacedCell(ref="", kind="gray")

    for rid in grid.row_ids:
        if "|__gap__" in rid:
            continue
        cells = grid.cells.setdefault(rid, {})
        for c in list(cells.keys()):
            if c >= 2 and cells[c].kind == "gray":
                del cells[c]

        bit_cols = sorted(
            c
            for c, pl in cells.items()
            if c >= 2
            and pl.ref
            and pl.kind in UNIQUE_BEARING_KINDS
        )
        if not bit_cols:
            continue

        reachable = entity_cols.get(rid) or []
        entity_set = set(reachable)
        skip_band = mid_band.get(rid, set()) if rid in mid_ids else set()
        # 夹心列带：中层不铺灰。步骤1/2/3 同一套 skip。
        skip = set(entity_set) | set(skip_band)
        skip_step2 = set(skip)
        row_i = id_to_idx.get(rid)
        is_fbrx_row = "FBRX" in str(rid).upper() or "FBRX" in str(
            (grid.labels or {}).get(rid, "")
        ).upper()
        row = id_to_row.get(rid)
        seq = (
            [str(x) for x in display_ref_seq(row.path) if x]
            if row is not None and row.path is not None
            else []
        )
        _bit_kinds = UNIQUE_BEARING_KINDS

        def _sandwich_host_id() -> str | None:
            if "|COPADBLK" not in str(rid):
                return None
            ids = list(grid.row_ids or [])
            if rid not in ids:
                return None
            i = ids.index(rid)
            if str(rid).endswith("|b1") and i + 1 < len(ids):
                return ids[i + 1]
            if str(rid).endswith("|b2") and i > 0:
                return ids[i - 1]
            if str(rid).endswith("|mid"):
                return rid
            return None

        def _mid_drawn_on_chain(ref: str) -> bool:
            """中段已在本链画出：主链行认其它主链；夹心顶底只认宿主。

            对侧黄腿、其它夹心块不算本链。全表扫字 / 借别人 path 都不问。
            """
            host_only = _sandwich_host_id()
            for orid, ocells in grid.cells.items():
                if orid == rid:
                    continue
                if host_only is not None:
                    if orid != host_only:
                        continue
                elif not is_main_content_row_id(orid):
                    continue
                for pl in ocells.values():
                    if (
                        pl is not None
                        and pl.kind in _bit_kinds
                        and str(pl.ref or "").strip() == ref
                    ):
                        return True
            ids = list(grid.row_ids or [])
            host_i = ids.index(host_only) if host_only in ids else None
            for _c, rs, re, mref, _pin in merge_segs:
                if mref != ref:
                    continue
                if row_i is not None and rs <= int(row_i) <= re:
                    continue
                if host_i is not None:
                    if rs <= int(host_i) <= re:
                        return True
                    continue
                for ri in range(int(rs), int(re) + 1):
                    if 0 <= ri < len(ids) and is_main_content_row_id(ids[ri]):
                        return True
            return False

        def _ref_visible_or_merged_here(ref: str) -> bool:
            for pl in cells.values():
                if (
                    pl is not None
                    and pl.kind in _bit_kinds
                    and str(pl.ref or "").strip() == ref
                ):
                    return True
            if row_i is None:
                return False
            for _c, rs, re, mref, _pin in merge_segs:
                if mref == ref and rs <= int(row_i) <= re:
                    return True
            return False

        def _seq_mids(
            seq_list: list[str], ref_a: str, ref_b: str,
        ) -> list[str] | None:
            if not seq_list or not ref_a or not ref_b:
                return None
            if ref_a not in seq_list or ref_b not in seq_list:
                return None
            ia, ib = seq_list.index(ref_a), seq_list.index(ref_b)
            if ib <= ia:
                return None
            return seq_list[ia + 1 : ib]

        def _foreign_entity_in_span(lo: int, hi: int) -> bool:
            host_only = _sandwich_host_id()
            for orid, ocells in grid.cells.items():
                if orid == rid:
                    continue
                if host_only is not None:
                    if orid != host_only:
                        continue
                elif not is_main_content_row_id(orid):
                    continue
                for c, pl in ocells.items():
                    if not (int(lo) < int(c) < int(hi)):
                        continue
                    if pl is not None and pl.ref and pl.kind in _bit_kinds:
                        rs = str(pl.ref or "").strip()
                        # 本行是该共享件 owner / 并柱盖住 → 本链已接，不是外来中段。
                        if _ref_visible_or_merged_here(rs):
                            continue
                        owners = (getattr(plan, "share_map", None) or {}).get(rs) or ()
                        if rid in owners:
                            continue
                        return True
            return False

        def _gap_blocked(col_a: int, col_b: int) -> bool:
            """两可见件之间：中段已在本链画出、本行没接到 → 不铺假干。"""
            pl_a, pl_b = cells.get(col_a), cells.get(col_b)
            ref_a = str(getattr(pl_a, "ref", "") or "").strip() if pl_a else ""
            ref_b = str(getattr(pl_b, "ref", "") or "").strip() if pl_b else ""
            def _this_row_owns(ref: str) -> bool:
                if _ref_visible_or_merged_here(ref):
                    return True
                owners = (getattr(plan, "share_map", None) or {}).get(ref) or ()
                return rid in owners

            mids = _seq_mids(seq, ref_a, ref_b)
            if mids is not None:
                for mid_ref in mids:
                    if _this_row_owns(mid_ref):
                        continue
                    if _mid_drawn_on_chain(mid_ref):
                        return True
                return False
            # 无两端 path：夹心顶底只看宿主中段列有没有字；
            # 主链行仍须灰（trunk_after extras）。他行旁路匹配不是本链中段。
            if _sandwich_host_id() is not None:
                return _foreign_entity_in_span(col_a, col_b)
            return False

        # Co-pad 支路行：只填相邻可见件之间；规律13 对顶/底行同样成立
        # （禁止 min-max 整行把两块夹心连成假公共干）。不铺行首左灰。
        if "|COPADBLK" in str(rid):
            host_blk = _sandwich_host_id()
            host_seq: list[str] = []
            if host_blk and plan is not None:
                for prow in plan.rows or []:
                    if str(getattr(prow, "row_id", "") or "") == str(host_blk):
                        if prow.path is not None:
                            host_seq = [
                                str(x) for x in display_ref_seq(prow.path) if x
                            ]
                        break
            copad_bands = (
                sandwich_reserved_bands(grid, host_blk, host_seq or None)
                if host_blk
                else []
            )

            def _same_copad_band(c0: int, c1: int) -> bool:
                for lo, hi in copad_bands:
                    if int(lo) <= int(c0) <= int(hi) and int(lo) <= int(c1) <= int(hi):
                        return True
                return not copad_bands

            # 中层被结构滤波并柱盖住：从该列接到本行合并后匹配。
            if row_i is not None and bit_cols:
                hub_cols = [
                    int(c)
                    for c, rs, re, mref, _pin in merge_segs
                    if rs <= int(row_i) <= re
                    and str(mref or "").upper().startswith(
                        ("FIL", "LPF", "BPF", "DUP", "DPX", "TPX")
                    )
                ]
                if hub_cols:
                    left = min(hub_cols)
                    first = bit_cols[0]
                    if left < first and not _gap_blocked(left, first):
                        for c in range(int(left) + 1, int(first)):
                            _paint(cells, c, skip=skip)
            for a, b in zip(bit_cols, bit_cols[1:]):
                if not _same_copad_band(a, b):
                    continue
                if _gap_blocked(a, b):
                    continue
                # 两可见件之间若夹着本行非钉顶并柱，灰接到柱即止，
                # 不得再穿到更远公共件。
                hi = int(b)
                if row_i is not None:
                    for c in range(int(a) + 1, int(b)):
                        hit = _merge_at(int(c), row_i)
                        if hit is not None and int(hit[1]) != int(row_i):
                            hi = int(c)
                            break
                for c in range(int(a) + 1, hi):
                    _paint(cells, c, skip=skip)
            continue

        # 灰格右截断（汇入边界，非器件类型开关）：
        # 本链路从左侧汇入的非钉顶并柱 J：灰格可接到 J，不得穿过 J 继续向右。
        # 判定 J（同时满足）：
        #   1) 本行被该并柱覆盖且非钉顶
        #   2) 本行已有可见器件在 J 左侧（从左臂汇入；排除仅被左侧树柱盖住
        #      的行，不得把 cap 置到树柱列杀光支路灰）
        # 多个 J 取最左：先遇到的柱先截断。
        #
        # **不**要求 J 在 path 上：并柱是物理占用，灰格也不可穿越。
        # 旧 path 过滤会在「path 下一跳更远并柱」时放过中间无关柱
        # （灰穿中间开关去接远端 FIL）。
        gray_right_cap: int | None = None
        if row_i is not None and not is_fbrx_row and bit_cols:
            join_cols: list[int] = []
            last_bit_for_cap = max(bit_cols)
            for c, rs, re, ref, pin in merge_segs:
                if not (rs <= int(row_i) <= re and int(pin) != int(row_i)):
                    continue
                jc = int(c)
                if any(int(bc) < jc for bc in bit_cols):
                    join_cols.append(jc)
            if join_cols:
                # 只截断「最右可见位号以右」的汇入柱。
                # 已越过左侧并柱的臂不得再被左侧柱 cap 掉，
                # 否则更右开关→FIL 之间铺不出灰格横连。
                right_joins = [
                    jc for jc in join_cols if int(jc) >= int(last_bit_for_cap)
                ]
                gray_right_cap = min(right_joins) if right_joins else None

        def _paint_row(c: int) -> None:
            # 穿过汇入并柱：c > J 禁止；c == J 由 skip/entity 处理
            if gray_right_cap is not None and int(c) > int(gray_right_cap):
                return
            _paint(cells, c, skip=skip)

        def _paint_span(lo: int, hi: int) -> None:
            for c in range(int(lo) + 1, int(hi)):
                if gray_right_cap is not None and int(c) > int(gray_right_cap):
                    continue
                _paint(cells, c, skip=skip_step2)

        # 1) A 列标签 → 首器件：从列 2 铺到首件。并柱格由 skip 跳过。
        first_bit = bit_cols[0]
        for c in range(2, first_bit):
            _paint(cells, c, skip=skip)

        # 2) 相邻可见位号之间（私有臂 / 实体干上真器件序）
        # 夹心带在这段里：馅 skip；左右空列须接到夹心缘。
        # 两块夹心之间（min..max 带内的馅外空洞）不铺假干。
        for a, b in zip(bit_cols, bit_cols[1:]):
            in_band = sorted(c for c in skip_band if a < c < b)
            if in_band:
                # 右端是夹心带内、宿主 path 上的结构 IC（hub 柱）→ 灰接到该件。
                # 馅仍是没有本行 hub 的夹心列。禁止把「带内任意可见件」当成 hub。
                pl_b = cells.get(b)
                b_ref = str(getattr(pl_b, "ref", "") or "").strip() if pl_b else ""
                struct = getattr(grid, "copad_structure_refs", None) or set()
                if (
                    skip_band
                    and min(skip_band) <= int(b) <= max(skip_band)
                    and b_ref
                    and b_ref in struct
                    and not is_non_merge_align_ref(
                        b_ref, getattr(pl_b, "device", None),
                    )
                    and not _gap_blocked(a, b)
                ):
                    for c in range(int(a) + 1, int(b)):
                        if gray_right_cap is not None and int(c) > int(gray_right_cap):
                            continue
                        _paint(cells, c, skip=set(entity_set))
                    continue
                # 左右空列须接到夹心缘。他行成柱的夹心腿/hub 不得整段禁 hop。
                # 两块夹心之间（min..max 带内的馅外空洞）不铺。
                _paint_span(a, in_band[0])
                _paint_span(in_band[-1], b)
                continue
            if _gap_blocked(a, b):
                continue
            _paint_span(a, b)

        # 夹心中层/宿主：末件到夹心带左缘。已汇入共享柱的私有臂不得穿柱。
        if skip_band:
            blo = min(skip_band)
            last_vis = bit_cols[-1]
            if (
                last_vis < blo
                and not _gap_blocked(last_vis, blo)
                and (gray_right_cap is None or int(last_vis) < int(gray_right_cap))
            ):
                for c in range(int(last_vis) + 1, int(blo)):
                    if gray_right_cap is not None and int(c) > int(gray_right_cap):
                        break
                    _paint(cells, c, skip=skip_step2)

        # 3) 最右可见 → path 上下一个本行够得着的共享并柱（仅一跳，保树状）
        last_bit = bit_cols[-1]
        last_pl = cells.get(last_bit)
        last_ref = str(getattr(last_pl, "ref", "") or "").strip() if last_pl else ""

        def _ref_merge_col_on_row(ref: str) -> int | None:
            for c in reachable:
                if int(c) <= last_bit:
                    continue
                hit = _merge_at(int(c), row_i)
                if hit is not None and hit[0] == ref:
                    return int(c)
            for c, rs, re, mref, pin in merge_segs:
                if mref != ref or int(c) <= last_bit:
                    continue
                if row_i is not None and rs <= int(row_i) <= re:
                    return int(c)
            return None

        def _next_path_entity_col() -> int | None:
            """path 上 last_ref 之后第一个「本行够得着」的节点列。

            够得着 = 本行可见或并柱覆盖。中段已在本链画出、本行没接到
            → 停止，不得 hop 到更远公共柱。
            """
            if not last_ref or last_ref not in seq:
                # 无 path 序：右侧最近、且本行确有并柱覆盖的列
                for c in reachable:
                    if int(c) > last_bit and _merge_at(int(c), row_i) is not None:
                        return int(c)
                return None
            for ref in seq[seq.index(last_ref) + 1 :]:
                hit_c = _ref_merge_col_on_row(ref)
                if hit_c is not None:
                    return hit_c
                if _mid_drawn_on_chain(ref):
                    return None
            return None

        # 最右已是并柱：仅钉顶行（实体链路宿主）可再接下一公共柱；
        # 覆盖行上的空并柱不向右灌公共干
        # （碰左侧并柱即止，禁止灰穿到 ANT）
        last_is_entity = last_bit in entity_set
        last_merge = _merge_at(last_bit, row_i) if last_is_entity else None
        is_pin_host = (
            last_merge is not None
            and row_i is not None
            and int(last_merge[1]) == int(row_i)
        )

        # 最右可见已落在并柱覆盖列上（含空并柱格）→ 不再向右灰（汇合截断）
        if row_i is not None and _merge_at(last_bit, row_i) is not None:
            if not is_pin_host and not is_fbrx_row:
                continue
        # 最右可见紧邻本行非钉顶并柱 → 已汇入，不向右假延长公共干
        # （夹心中层 L_AFTER 贴 DIP 覆盖格后，不得再灰穿到 TRIP）
        if row_i is not None and not is_fbrx_row:
            adj = _merge_at(int(last_bit) + 1, row_i)
            if adj is not None and int(adj[1]) != int(row_i):
                continue

        allow_entity_hop = (not last_is_entity) or is_pin_host or is_fbrx_row
        if not allow_entity_hop:
            continue

        stop_col = _next_path_entity_col()
        if stop_col is not None and stop_col > last_bit:
            for c in range(last_bit + 1, stop_col):
                _paint_row(c)

    return grid


def drop_pure_gray_columns(grid: SheetGrid) -> SheetGrid:
    """删除「全表无器件、仅灰或空」的列（合并列保留）。"""
    if grid.max_col < 2:
        return grid
    merge_cols = {m.col for m in (grid.merges or [])}
    keep: list[int] = [1]
    for c in range(2, grid.max_col + 1):
        if c in merge_cols:
            keep.append(c)
            continue
        useful = False
        for rid in grid.row_ids:
            if "|__gap__" in rid:
                continue
            pl = (grid.cells.get(rid) or {}).get(c)
            if pl is None or pl.kind == "gray":
                continue
            if pl.ref and pl.kind in (
                "device", "label", "copad_leg", "copad_tag",
            ):
                useful = True
                break
        if useful:
            keep.append(c)

    if keep == list(range(1, grid.max_col + 1)):
        return grid

    old_to_new = {old: i + 1 for i, old in enumerate(keep)}
    new_cells: dict[str, dict[int, PlacedCell]] = {}
    for rid in grid.row_ids:
        src = grid.cells.get(rid) or {}
        new_cells[rid] = {
            old_to_new[old_c]: pl
            for old_c, pl in src.items()
            if old_c in old_to_new
        }

    grid.cells = new_cells
    grid.merges = [
        MergeRange(
            ref=m.ref,
            col=old_to_new[m.col],
            row_start=m.row_start,
            row_end=m.row_end,
            row_ids=m.row_ids,
        )
        for m in (grid.merges or [])
        if m.col in old_to_new
    ]
    grid.max_col = max(old_to_new.values()) if old_to_new else 1
    return grid


def pack_row_by_path(
    seq: list[str],
    by_ref: dict[str, PlacedCell],
    label: object,
    reserved: set[int],
    multi_col: dict[str, int],
    *,
    band: str = "",
) -> dict[int, PlacedCell]:
    """path 序投放；multi_col 为竖并/共享目标列。

    **空柱对齐（硬）**：本行 unique 后无合并件值时，path 前缀私有段须
    **右对齐贴合并列左侧**。左边已经过了共享件（含空柱）则左贴柱右。
    不问游标数字。柱右侧器件仍不得排到柱左。
    later multi 的目标列若已被更早空柱/器件把游标推过，不得再当左贴锚
    （COLAY 腿紧凑列 < FIL 柱 → 私有臂整段翻到柱左）。
    """
    new_map: dict[int, PlacedCell] = {}
    if label is not None:
        new_map[1] = label  # type: ignore[index]

    def _free(c: int) -> int:
        while c in new_map or c in reserved:
            c += 1
        return c

    cursor = 2
    placed: set[str] = set()
    pending: list[tuple[str, PlacedCell]] = []

    def _flush_pending(before_col: int | None) -> None:
        """pending 私有段：绝不占 before_col。

        目标列 ≤ 游标 → 不当左贴锚，从游标往右排。
        左边过了共享件 → 左贴；只有右边共享 → 右贴。
        """
        nonlocal cursor
        if not pending:
            return
        if before_col is not None and int(before_col) <= cursor:
            before_col = None
        if before_col is None:
            for pref, pl in pending:
                c = _free(cursor)
                new_map[c] = pl
                placed.add(pref)
                cursor = c + 1
            pending.clear()
            return
        n = len(pending)
        limit = int(before_col)
        # 在 [cursor, limit) 内找空列（可穿透 reserved，优先不 reserved）
        free_pref: list[int] = []
        free_any: list[int] = []
        c = cursor
        while c < limit:
            if c not in new_map:
                free_any.append(c)
                if c not in reserved:
                    free_pref.append(c)
            c += 1
        pool = free_pref if len(free_pref) >= n else free_any
        if len(pool) >= n:
            # 左边已经过了共享件（含空柱）→ 左贴保 path。
            # 只有右边有共享件 → 右贴到柱左。不问游标数字。
            left_shared = any(
                int(multi_col[r]) < limit
                and (r in placed or int(multi_col[r]) < cursor)
                for r in multi_col
            )
            if left_shared:
                use = pool[:n]
            else:
                use = pool[-n:]
        else:
            # 空档不足：从 limit-1 向左硬塞（仍不占 limit）
            use = []
            c = min(limit, cursor + n) - 1
            while len(use) < n and c >= 2:
                if c < limit and c not in new_map and c not in use:
                    use.append(c)
                c -= 1
            use = sorted(use)
        for (pref, pl), col in zip(pending, use):
            new_map[int(col)] = pl
            placed.add(pref)
            cursor = max(cursor, int(col) + 1)
        for pref, pl in pending[len(use) :]:
            c = _free(cursor)
            while before_col is not None and c == int(before_col):
                c = _free(c + 1)
            new_map[c] = pl
            placed.add(pref)
            cursor = c + 1
        pending.clear()

    for ref in seq:
        if ref in multi_col:
            t = int(multi_col[ref])
            if t < cursor:
                t = cursor
            _flush_pending(t)
            if ref in by_ref:
                c = t
                while c in new_map and (
                    new_map[c] is None
                    or str(getattr(new_map[c], "ref", "") or "") not in ("", ref)
                ):
                    c += 1
                while c in reserved:
                    c += 1
                if c in new_map and str(
                    getattr(new_map.get(c), "ref", "") or ""
                ) not in ("", ref):
                    c = _free(max(c, t) + 1)
                new_map[c] = by_ref[ref]
                placed.add(ref)
                cursor = max(cursor, c + 1)
            else:
                cursor = max(cursor, t + 1)
            continue
        if ref not in by_ref:
            continue
        # 勿落到尚未消费的 multi 目标列上（留给后续锚点）
        later_targets = {
            int(multi_col[r])
            for r in seq[seq.index(ref) + 1 :]
            if r in multi_col
        }
        if later_targets:
            pending.append((ref, by_ref[ref]))
            continue
        c = _free(cursor)
        while c in later_targets or c in new_map or c in reserved:
            c += 1
        new_map[c] = by_ref[ref]
        placed.add(ref)
        cursor = c + 1
    _flush_pending(None)
    pillar_cols = {int(v) for v in multi_col.values()}
    for ref, pl in by_ref.items():
        if ref in placed:
            continue
        c = _free(cursor)
        # 禁止落到空竖并柱列（会吞并）；本行已占该柱的合并件除外
        while c in pillar_cols and (
            c not in new_map
            or str(getattr(new_map.get(c), "ref", "") or "") != ref
        ):
            c += 1
            c = _free(c)
        new_map[c] = pl
        cursor = c + 1
    return new_map


def _snap_host_path_ic_to_sandwich_col(
    grid: SheetGrid,
    new_map: dict[int, PlacedCell],
    *,
    reserved: set[int] | None = None,
) -> None:
    """宿主 path 上的夹心 IC 中段对齐到黄腿同列，禁止紧凑挤到匹配段旁边。

    须：DIP/FIL 等可竖并件与夹心支路同列，匹配灰接到该列。
    禁：对腿/RLCT 不往宿主中层塞。
    """
    struct = getattr(grid, "copad_structure_refs", None) or set()
    if not struct:
        return
    _ = reserved
    yellow_col: dict[str, int] = {}
    branch_homes = set()
    for pair in (getattr(grid, "copad_branch_rows_by_host", None) or {}).values():
        branch_homes.update(pair)
    for rid, cells in (grid.cells or {}).items():
        if "COPADBLK" not in str(rid) and rid not in branch_homes:
            continue
        for c, pl in (cells or {}).items():
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            rs = str(pl.ref or "").strip()
            if rs in struct:
                yellow_col[rs] = int(c)
    merge_col = {
        str(m.ref or "").strip(): int(m.col)
        for m in (grid.merges or [])
        if str(m.ref or "").strip() in struct
    }
    for c, pl in list(new_map.items()):
        if pl is None or int(c) < 2:
            continue
        rs = str(pl.ref or "").strip()
        if not rs or rs not in struct:
            continue
        if is_non_merge_align_ref(rs, pl.device):
            continue
        dest = yellow_col.get(rs) or merge_col.get(rs)
        if dest is None:
            continue
        # 字在黄腿上：宿主这份删掉，不塞进馅。
        if rs in yellow_col:
            del new_map[c]
            continue
        dest = int(dest)
        if dest == int(c):
            continue
        occ = new_map.get(dest)
        occ_ref = str(getattr(occ, "ref", "") or "").strip() if occ else ""
        if occ is not None and occ_ref and occ_ref != rs:
            continue
        del new_map[c]
        new_map[dest] = pl


def reflow_main_rows_path_order(
    plan: SheetPlan,
    grid: SheetGrid,
) -> SheetGrid:
    """主链行回流：剩余器件按 path 显示序；竖并柱全局对齐。

    两阶段：
      1) 各主链行按 path 序紧凑投放（忽略旧列）
      2) 每个竖并 ref 取各行落点 max 列作公共柱，再按 place 规则对齐重投
         — 本行无值的竖并 ref 仍占空柱，其 path 右侧器件不得排到柱左
    不改 COPAD 支路；夹心宿主跳过 sandwich 保留列。
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    merge_refs: set[str] = set()
    for m in grid.merges or []:
        ref = str(m.ref or "").strip()
        if ref and int(m.row_end) > int(m.row_start):
            merge_refs.add(ref)
    # 行序挤走 JUMP 后，焊盘可能暂时没有高柱；仍按 SVM 共享对齐同列，才能走 U。
    lnc = getattr(plan, "left_neighbor_count", None) or {}
    for ref, owners in (getattr(plan, "share_map", None) or {}).items():
        rs = str(ref or "").strip()
        if len(owners or ()) >= 2 and should_vertical_merge(plan, rs, lnc):
            merge_refs.add(rs)

    def _is_host(_g: SheetGrid, _rid: str) -> bool:
        """插入夹心的中层才是馅宿主。

        黄腿覆在已有 merge 支路主链上时，贯通行按主链接 hub，不是馅。
        """
        ids = list(_g.row_ids or [])
        if _rid not in ids:
            return False
        i = ids.index(_rid)
        if i <= 0 or i >= len(ids) - 1:
            return False
        return str(ids[i - 1]).endswith("|b1") and str(ids[i + 1]).endswith("|b2")

    def _host_reserved_cols(_g: SheetGrid, _rid: str, _seq: list[str]) -> set[int]:
        """本宿主夹心带（含带内空洞）。带 = path 连续支路腿，不问列间距。"""
        cols: set[int] = set()
        for a, b in sandwich_reserved_bands(_g, _rid, _seq):
            cols.update(range(int(a), int(b) + 1))
        return cols

    # 收集主链行内容
    row_data: list[tuple[str, list[str], dict[str, PlacedCell], object, set[int], str]] = []
    for rid in list(grid.row_ids or []):
        if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        row = id_to_row.get(rid)
        if row is None or not getattr(row, "path", None):
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        cells = dict(grid.cells.get(rid) or {})
        label = cells.get(1)
        by_ref: dict[str, PlacedCell] = {}
        for c, pl in cells.items():
            if int(c) < 2 or pl is None:
                continue
            if pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            ref = str(pl.ref or "").strip()
            if ref:
                by_ref[ref] = pl
        if not by_ref and not any(r in merge_refs for r in seq):
            continue
        res = (
            _host_reserved_cols(grid, rid, seq) if _is_host(grid, rid) else set()
        )
        band = str(getattr(row.path, "band", "") or "")
        row_data.append((rid, seq, by_ref, label, res, band))

    if not row_data:
        return grid

    def _pack(
        seq: list[str],
        by_ref: dict[str, PlacedCell],
        label: object,
        reserved: set[int],
        multi_col: dict[str, int],
        *,
        band: str = "",
    ) -> dict[int, PlacedCell]:
        return pack_row_by_path(
            seq, by_ref, label, reserved, multi_col, band=band,
        )

    # 夹心宿主：支路行上的结构腿 → 用于把 path 切成「夹心左 / 夹心右」
    # （trunk_after 被 _free 跳过保留列后不得甩到夹心右侧）
    branch_struct_by_host: dict[str, set[str]] = {}
    ids = list(grid.row_ids or [])
    for i, rid in enumerate(ids):
        if not _is_host(grid, rid):
            continue
        legs: set[str] = set()
        mapped = (getattr(grid, "copad_branch_rows_by_host", None) or {}).get(rid)
        neighbors: list[str] = []
        if mapped:
            neighbors = [mapped[0], mapped[1]]
        elif 0 < i < len(ids) - 1:
            neighbors = [ids[i - 1], ids[i + 1]]
        struct_refs = getattr(grid, "copad_structure_refs", None) or set()
        for br in neighbors:
            for pl in (grid.cells.get(br) or {}).values():
                if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                    continue
                ref = str(pl.ref or "").strip()
                if not ref:
                    continue
                # 夹心腿 = 黄格。GPS 的 FIL/LNA hub 在 structure_refs 里
                # 但是干路件，不得当支路腿，否则多块夹心被收成一段 mega-band。
                if pl.kind == "copad_leg":
                    legs.add(ref)
                elif (
                    ref in struct_refs
                    and str(getattr(grid, "key", "") or "") != "GPS"
                ):
                    legs.add(ref)
        if legs:
            branch_struct_by_host[rid] = legs

    def _reserved_runs(reserved: set[int]) -> list[tuple[int, int]]:
        """reserved 列拆成连续段（不相邻即另一条带）。"""
        if not reserved:
            return []
        ordered = sorted(int(c) for c in reserved)
        runs: list[tuple[int, int]] = []
        lo = prev = ordered[0]
        for c in ordered[1:]:
            if c <= prev + 1:
                prev = c
                continue
            runs.append((lo, prev))
            lo = prev = c
        runs.append((lo, prev))
        return runs

    def _host_path_segments(
        seq: list[str], branch_legs: set[str],
    ) -> list[list[str]]:
        """按 path 上支路结构腿分组切宿主段（可含空段）。

        两夹心 path：``[左干] [夹心1腿…] [中段干] [夹心2腿…] [右干]``
        → 3 个宿主段，对应夹心带之间的空档，禁止把中段甩到末夹心右侧。
        """
        segs: list[list[str]] = [[]]
        i = 0
        n = len(seq)
        while i < n:
            if seq[i] in branch_legs:
                segs.append([])
                while i < n and seq[i] in branch_legs:
                    i += 1
                continue
            segs[-1].append(seq[i])
            i += 1
        return segs

    def _pack_host_split(
        seq: list[str],
        by_ref: dict[str, PlacedCell],
        label: object,
        reserved: set[int],
        multi_col: dict[str, int],
        branch_legs: set[str],
        *,
        band: str = "",
    ) -> dict[int, PlacedCell]:
        """夹心宿主：按**多段**夹心保留列投放 path，保持段间干路序。

        旧逻辑用 min/max(reserved) 并成 mega-band，双 COPAD 时把中段干路
        整段甩到末夹心右侧。

        规则：
          1) reserved 拆连续段 → gaps = 段前 / 段间 / 段后
          2) path 按支路腿分组切宿主段
          3) 宿主段与 gaps 对齐左起投放；多段时禁止跨 gap 并带
        单夹心退化为「左硬限 band_lo + 右从 band_hi+1」。
        """
        if not reserved or not branch_legs:
            return _pack(seq, by_ref, label, reserved, multi_col, band=band)
        runs = _reserved_runs(reserved)
        if not runs:
            return _pack(seq, by_ref, label, reserved, multi_col, band=band)

        # gaps: (start_col, hard_limit exclusive | None)
        gaps: list[tuple[int, int | None]] = [(2, runs[0][0])]
        for a, b in zip(runs, runs[1:]):
            gaps.append((a[1] + 1, b[0]))
        gaps.append((runs[-1][1] + 1, None))

        segs = _host_path_segments(seq, branch_legs)
        # 段数与 gap 对齐：多出的并入末段；不足则尾 gap 空置
        while len(segs) > len(gaps):
            segs[-2] = segs[-2] + segs[-1]
            segs.pop()
        while len(segs) < len(gaps):
            segs.append([])

        new_map: dict[int, PlacedCell] = {}
        if label is not None:
            new_map[1] = label  # type: ignore[index]

        def _place_run(
            refs: list[str], start: int, *, hard_limit: int | None,
        ) -> None:
            """段内投放。

            冲突点（GPS 夹心 vs multiport/其它夹心）：
              硬限溢出后向左找位，旧逻辑在已无空位时仍 ``new_map[max(2,c)]=pl``，
              后段公尾会盖掉前段器件。
            解法：
              - 落点 **严格小于本段 start** 且已被其它 ref 占用 → 禁止盖写，改 hard_limit 右侧
              - 同段内（dest≥start）保持历史挤位（含可盖写），以免 multiport 中段回归
              - 空档仍可向左占用
            """
            floor = max(2, int(start))
            cursor = floor

            def _spill_right_of_limit() -> int:
                c = max(2, int(hard_limit) if hard_limit is not None else cursor)
                while c in new_map or c in reserved:
                    c += 1
                return c

            def _commit(c: int, ref: str, pl) -> int:
                dest = max(2, int(c))
                cur = new_map.get(dest)
                other_ref = (
                    str(getattr(cur, "ref", "") or "").strip()
                    if cur is not None
                    else ""
                )
                # 仅禁止「后段落点盖写前段」：dest < 本段 start。
                # 同段内保持历史挤位（multiport 夹心中段依赖），避免回归。
                if other_ref and other_ref != ref and dest < floor:
                    dest = _spill_right_of_limit()
                new_map[dest] = pl
                return dest

            for ref in refs:
                if ref in multi_col:
                    t = int(multi_col[ref])
                    if t < cursor:
                        t = cursor
                    if hard_limit is not None and t >= hard_limit:
                        t = cursor
                    c = t
                    while (
                        c in new_map
                        and str(getattr(new_map.get(c), "ref", "") or "")
                        not in ("", ref)
                    ) or c in reserved or (
                        hard_limit is not None and c >= hard_limit
                    ):
                        c += 1
                        if hard_limit is not None and c >= hard_limit:
                            c = hard_limit - 1
                            while c >= 2 and (
                                c in reserved
                                or (
                                    c in new_map
                                    and str(
                                        getattr(new_map.get(c), "ref", "") or ""
                                    )
                                    not in ("", ref)
                                )
                            ):
                                c -= 1
                            break
                    if hard_limit is not None and c >= hard_limit:
                        c = max(2, hard_limit - 1)
                    if ref in by_ref:
                        dest = _commit(c, ref, by_ref[ref])
                        cursor = max(cursor, dest + 1)
                    else:
                        # 本行无值：空柱占位，后续必须在柱右（与 _pack 同一不变量）。
                        # 不推 cursor → 公尾匹配落到 FIL 柱上，evict 甩到 ANT 右。
                        # 只推本行 cursor，不改其它 owner 对同一柱的灰/并附着。
                        cursor = max(cursor, int(t) + 1)
                    continue
                if ref not in by_ref:
                    continue
                pl = by_ref[ref]
                c = cursor
                while c in new_map or c in reserved:
                    c += 1
                    if hard_limit is not None and c >= hard_limit:
                        break
                if hard_limit is not None and c >= hard_limit:
                    c = hard_limit - 1
                    while c >= 2 and (
                        c in reserved
                        or (
                            c in new_map
                            and str(getattr(new_map.get(c), "ref", "") or "")
                            not in ("", ref)
                        )
                    ):
                        c -= 1
                dest = _commit(c, ref, pl)
                cursor = max(cursor, dest + 1)

        for seg, (g_lo, g_hi) in zip(segs, gaps):
            # 段内仍按 path 序；multi 空柱也推进 cursor
            run_refs = [
                r for r in seg
                if r in by_ref or r in multi_col
            ]
            if not run_refs:
                continue
            _place_run(run_refs, g_lo, hard_limit=g_hi)
        return new_map

    def _compact_col(seq: list[str], by_ref: dict[str, PlacedCell], ref: str) -> int | None:
        """各行紧凑列：path 序上可见前缀 +1。本行无字的空柱仍占一格。"""
        cursor = 2
        for r in seq:
            if r == ref:
                return cursor
            if r in by_ref:
                cursor += 1
        return None

    provisional: dict[str, int] = {}
    for _rid, seq, by_ref, _label, _res, _band in row_data:
        for ref in merge_refs:
            col = _compact_col(seq, by_ref, ref)
            if col is not None:
                provisional[ref] = max(provisional.get(ref, 2), col)

    for m in grid.merges or []:
        ref = str(m.ref or "").strip()
        if ref not in merge_refs:
            continue
        provisional[ref] = max(provisional.get(ref, 2), int(m.col))

    mapped_hosts = getattr(grid, "copad_branch_rows_by_host", None) or {}
    # 阶段2：带 multi 重投
    max_col = 1
    for rid, seq, by_ref, label, res, band in row_data:
        if rid in branch_struct_by_host and res:
            new_map = _pack_host_split(
                seq, by_ref, label, res, multi_col=provisional,
                branch_legs=branch_struct_by_host[rid],
                band=band,
            )
            _snap_host_path_ic_to_sandwich_col(grid, new_map, reserved=res)
        else:
            new_map = _pack(
                seq, by_ref, label, res, multi_col=provisional, band=band,
            )
            if rid in mapped_hosts:
                _snap_host_path_ic_to_sandwich_col(grid, new_map, reserved=res)
        # 夹心 trunk_after extras 不在该行 path，投放会丢掉。
        # 结构干路须接到夹心芯片侧：宿主贴保留列左缘；merge 后干路行贴夹心左缘。
        struct = getattr(grid, "copad_structure_refs", None) or set()
        if struct:
            placed = {
                str(pl.ref or "").strip()
                for pl in new_map.values()
                if pl is not None and str(pl.ref or "").strip()
            }
            extras = [
                (ref, pl) for ref, pl in by_ref.items()
                if ref in struct and ref not in seq
            ]
            guests = getattr(grid, "copad_chip_trunk_row_ids", None) or set()

            def _drop_other_cols(ref: str, keep: int) -> None:
                for c, occ in list(new_map.items()):
                    if (
                        int(c) != int(keep)
                        and occ is not None
                        and str(occ.ref or "").strip() == ref
                    ):
                        del new_map[c]

            if extras and rid in branch_struct_by_host and res:
                lo = min(int(c) for c in res)
                for ref, pl in extras:
                    dest = lo - 1
                    while dest >= 2:
                        occ = new_map.get(dest)
                        occ_ref = (
                            str(getattr(occ, "ref", "") or "").strip() if occ else ""
                        )
                        if occ is None or occ_ref in ("", ref):
                            _drop_other_cols(ref, dest)
                            new_map[dest] = pl
                            break
                        dest -= 1
            elif extras and rid in guests:
                # merge 后干路行：extras 贴夹心芯片侧缘。
                # _pack 会把不在 path 的 leftover 紧凑排掉，这里再挪到缘。
                band_lo = None
                ids = list(grid.row_ids or [])
                if rid in ids:
                    i0 = ids.index(rid)
                    for j in range(i0, len(ids)):
                        if str(ids[j]).endswith("|b1") and j + 1 < len(ids):
                            bands = sandwich_reserved_bands(grid, ids[j + 1])
                            if bands:
                                band_lo = min(lo for lo, _hi in bands)
                            break
                dest0 = max(2, int(band_lo) - 1) if band_lo else None
                if dest0 is None:
                    dest0 = (
                        max((int(c) for c in new_map if int(c) >= 2), default=1) + 1
                    )
                dest = dest0
                for ref, pl in extras:
                    d = dest
                    placed_extra = False
                    while d >= 2:
                        occ = new_map.get(d)
                        occ_ref = (
                            str(getattr(occ, "ref", "") or "").strip() if occ else ""
                        )
                        if occ is None or occ_ref in ("", ref):
                            _drop_other_cols(ref, d)
                            new_map[d] = pl
                            dest = d - 1
                            placed_extra = True
                            break
                        d -= 1
                    if not placed_extra:
                        dest -= 1
            elif extras:
                hub_col = 0
                for c, pl in new_map.items():
                    if pl is None or int(c) < 2:
                        continue
                    ru = str(pl.ref or "").strip().upper()
                    if ru.startswith(("FIL", "LPF", "BPF", "DIP", "U")):
                        hub_col = max(hub_col, int(c))
                dest0 = hub_col + 1 if hub_col else (
                    max((int(c) for c in new_map if int(c) >= 2), default=1) + 1
                )
                dest = dest0
                for ref, pl in extras:
                    while dest in new_map:
                        dest += 1
                    new_map[dest] = pl
                    dest += 1
        grid.cells[rid] = new_map
        if new_map:
            max_col = max(max_col, max(new_map.keys()))
    grid.max_col = max(grid.max_col, max_col)

    # 同步 merge 列到 provisional
    grid.merges = [
        MergeRange(
            ref=m.ref,
            col=int(provisional.get(str(m.ref or "").strip(), m.col)),
            row_start=m.row_start,
            row_end=m.row_end,
            row_ids=m.row_ids,
        )
        for m in (grid.merges or [])
    ]
    # 并柱净空：跨度内禁止其它器件占合并列（夹心/unique 后残留 → 暴力并吞位号）
    _evict_foreign_from_merge_columns(plan, grid)
    return grid


def _evict_foreign_from_merge_columns(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """把误落在竖并柱列上的其它器件挪走（按 path 在合并件左/右决定方向）。"""
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    for m in list(grid.merges or []):
        ref = str(m.ref or "").strip()
        mcol = int(m.col)
        if not ref or m.row_end <= m.row_start:
            continue
        for ri in range(int(m.row_start), int(m.row_end) + 1):
            if not (0 <= ri < len(grid.row_ids)):
                continue
            rid = grid.row_ids[ri]
            if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                continue
            cells = grid.cells.setdefault(rid, {})
            pl = cells.get(mcol)
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            other = str(pl.ref or "").strip()
            if not other or other == ref:
                continue
            # 决定挪到柱左还是柱右
            go_right = True
            row = id_to_row.get(rid)
            if row is not None and getattr(row, "path", None):
                seq = [str(x) for x in display_ref_seq(row.path) if x]
                if other in seq and ref in seq:
                    go_right = seq.index(other) > seq.index(ref)
                elif other in seq and ref not in seq:
                    go_right = True
                elif other in seq:
                    go_right = False
            # 找空列
            if go_right:
                c = mcol + 1
                while c in cells and cells[c].kind in VISIBLE_DEVICE_KINDS and str(
                    cells[c].ref or ""
                ).strip():
                    c += 1
            else:
                c = mcol - 1
                while c >= 2 and c in cells and cells[c].kind in (
                    VISIBLE_DEVICE_KINDS
                ) and str(cells[c].ref or "").strip():
                    c -= 1
                if c < 2:
                    c = mcol + 1
                    while c in cells and cells[
                        c
                    ].kind in VISIBLE_DEVICE_KINDS and str(
                        cells[c].ref or ""
                    ).strip():
                        c += 1
            cells[c] = pl
            del cells[mcol]
            grid.max_col = max(int(grid.max_col or 1), c)


def polish_chain_visuals(
    grid: SheetGrid,
    plan: Optional[SheetPlan] = None,
) -> SheetGrid:
    """视觉收口：按合并柱间真实链路段填灰；去纯灰垫列。"""
    grid = fill_chain_continuity(grid, plan)
    grid = drop_pure_gray_columns(grid)
    grid = fill_chain_continuity(grid, plan)
    return grid

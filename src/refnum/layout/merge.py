"""功能5 阶段 B — 竖并门面（生命周期 + 公开 re-export）。

高度只经 ``compute_vertical_merges``。夹心插行后必须再跑一遍：旧柱行号
已失效，prune 会清掉；unique 钉顶后只剩顶格，finalize 单算拼不回这些柱。
path-owner 是几何附着，不是第二套柱高。钉顶只搬字，不改跨度。

实现按职责拆开，判定只一份：
  merge_span   真连接 / 透明行 / 支路收缩
  merge_emit   发柱
  merge_attach 钉顶 / prune / path-owner 附着

本文件只编排：apply → L2 收拢 → 夹心后 refresh → unique 后 finalize。
调用方继续 ``from refnum.layout.merge import …``。
"""
from __future__ import annotations

from collections import defaultdict

from refnum.layout.merge_attach import (
    _merge_span_crosses_non_owner_main,
    _path_owner_reachable_indices,
    add_path_owner_merges,
    lift_copad_blocks_above_path_owners,
    normalize_merges_pin_top,
    pin_merge_value_to_top,
    prune_merges_missing_cells,
    realign_public_fil_after_path_owner,
    rebind_merge_row_ids,
    union_recomputed_and_prev_merges,
)
from refnum.layout.merge_emit import (
    _provisional_merges,
    align_upstream_merge_span_to_path_successors,
    compute_vertical_merges,
)
from refnum.layout.merge_span import (
    MergeCheckError,
    col_of_ref,
    condense_merge_branch_actives,
    device_ref_visible,
    display_left_neighbor,
    extend_span_to_adjacent_fbrx,
    is_jump_band_row_id,
    is_main_content_row_id,
    is_merge_passthrough_row_id,
    path_left_visible,
    row_ended_at_left_join,
)
from refnum.layout.model import MergeRange, SheetGrid, SheetPlan
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_ref_seq,
    is_non_merge_align_ref,
    should_vertical_merge,
)

# 测试 / 旧调用仍用下划线名
_is_merge_passthrough_row_id = is_merge_passthrough_row_id
_is_jump_band_row_id = is_jump_band_row_id
_col_of_ref = col_of_ref
_row_ended_at_left_join = row_ended_at_left_join
_align_upstream_merge_span_to_path_successors = (
    align_upstream_merge_span_to_path_successors
)


def apply_merges(plan: SheetPlan, grid: SheetGrid) -> SheetGrid:
    """计算合并并挂到 grid.merges。"""
    grid.merges = compute_vertical_merges(plan, grid)
    return grid


def apply_main_chain_merges(plan: SheetPlan, grid: SheetGrid) -> SheetGrid:
    """L6：主链共享列竖并（夹心前）。"""
    return apply_merges(plan, grid)


def seed_provisional_merges(plan: SheetPlan, grid: SheetGrid) -> SheetGrid:
    """夹心前初值柱（不 clip / align）。写表仍走 ``apply_main_chain_merges``。"""
    grid.merges = _provisional_merges(plan, grid, min_owners=2)
    return grid


# L6 阻断 → L2 行序收拢（有界 loop）

MERGE_L2_REHOME_MAX_ROUNDS = 2
MERGE_L2_REHOME_HARD_CAP = 5


def list_merge_rehome_targets(
    plan: SheetPlan,
    *,
    min_owners: int = 2,
) -> list[tuple[str, frozenset[str], int]]:
    """并柱会被非透明主链切断的 ref → (ref, owners, holes)。"""
    from refnum.layout.order import holes_for_ref

    lnc = plan.left_neighbor_count or {}
    order_ids = [r.row_id for r in (plan.rows or [])]
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    out: list[tuple[str, frozenset[str], int]] = []
    for ref, owners in (plan.share_map or {}).items():
        ow = frozenset(str(x) for x in (owners or ()) if x)
        if len(ow) < min_owners:
            continue
        if not should_vertical_merge(plan, ref, lnc):
            continue
        dev = device_from_plan(plan, ref)
        if is_non_merge_align_ref(ref, dev):
            continue
        ru = str(ref).upper()
        if ru.startswith(("U", "SDR", "PA", "LNA")):
            continue
        h = holes_for_ref(order_ids, ow)
        if h <= 0:
            continue
        idxs = [i for i, rid in enumerate(order_ids) if rid in ow]
        if len(idxs) < 2:
            continue
        lo, hi = idxs[0], idxs[-1]
        blocked = False
        for j in range(lo + 1, hi):
            rid = order_ids[j]
            if rid in ow:
                continue
            if is_merge_passthrough_row_id(
                rid, ref=ref, plan=plan, id_to_row=id_to_row,
            ):
                continue
            if is_jump_band_row_id(rid):
                continue
            row_j = id_to_row.get(rid)
            if is_fbrx_sample_row(
                rid=rid,
                devices=getattr(row_j, "devices", None) if row_j is not None else None,
                require_no_ant=True,
                max_devs=None,
                source="rid",
            ):
                seq = set()
                if row_j is not None and row_j.path is not None:
                    seq = {str(x) for x in display_ref_seq(row_j.path) if x}
                if str(ref) not in seq:
                    continue
            blocked = True
            break
        if blocked:
            out.append((str(ref), ow, int(h)))
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def rehome_plan_for_merge_blockers(
    plan: SheetPlan,
    targets: list[tuple[str, frozenset[str], int]],
) -> tuple[SheetPlan, bool]:
    """按阻断 ref 的 owners 做 gather 收拢；有变化则 polish 并刷新 share_map。"""
    from refnum.layout.order import gather_owners, polish_row_order
    from refnum.layout.rows import l1_share_map

    if not targets or not plan.rows:
        return plan, False
    rows = list(plan.rows)
    before = [r.row_id for r in rows]
    for _ref, owners, _h in targets:
        rows = gather_owners(rows, owners)
    if [r.row_id for r in rows] == before:
        return plan, False
    left_n = plan.left_neighbor_count or {}
    share = l1_share_map(rows)
    rows = polish_row_order(
        rows, share, left_neighbor_count=left_n,
    )
    share = l1_share_map(rows)
    new_plan = SheetPlan(
        key=plan.key,
        rows=rows,
        share_map=share,
        left_neighbor_count=left_n,
    )
    return new_plan, True


def l2_merge_rehome_loop(
    plan: SheetPlan,
    *,
    max_rounds: int | None = None,
) -> SheetPlan:
    """L6 语义阻断 → L2 gather 收拢，有界轮询。"""
    cap = MERGE_L2_REHOME_HARD_CAP
    n = MERGE_L2_REHOME_MAX_ROUNDS if max_rounds is None else int(max_rounds)
    n = max(0, min(n, cap))
    if n <= 0 or not plan.rows:
        return plan

    cur = plan
    for rnd in range(1, n + 1):
        targets = list_merge_rehome_targets(cur)
        if not targets:
            return cur
        nxt, ok = rehome_plan_for_merge_blockers(cur, targets)
        if not ok:
            return cur
        cur = nxt

    return cur


def refresh_merges_after_copad(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    signal_infos=None,
) -> SheetGrid:
    """夹心装配后：上提 → 再收第一层前端 → 上下支再平衡 → L6 重算 → path-owner → 钉顶。

    夹心插行后必须重算。旧柱行号已失效，只搬不重算会被 prune 清空；
    unique 钉顶后 finalize 单算拼不回这些柱。
    """
    from refnum.layout.copad import (
        _gather_vertical_merge_owners_after_copad,
        rebalance_copad_branches_toward_path_owners,
    )

    lift_copad_blocks_above_path_owners(plan, grid)
    _gather_vertical_merge_owners_after_copad(plan, grid)
    rebalance_copad_branches_toward_path_owners(plan, grid)
    prev = list(grid.merges or [])
    recomputed = compute_vertical_merges(plan, grid)
    grid.merges = union_recomputed_and_prev_merges(recomputed, prev)
    add_path_owner_merges(plan, grid, signal_infos=signal_infos)
    realign_public_fil_after_path_owner(plan, grid)
    prune_merges_missing_cells(
        grid, plan=plan, signal_infos=signal_infos,
    )
    realign_public_fil_after_path_owner(plan, grid)
    rebind_merge_row_ids(grid)
    grid.merges = normalize_merges_pin_top(grid)
    return grid


def coalesce_adjacent_merge_spans(grid: SheetGrid) -> SheetGrid:
    """把同一根柱被删行分开的**紧邻**两段接上。

    竖并柱在 unique 之前算；抽字后只剩标签的行被 `drop_empty_content_rows` 删掉，
    删行只重映射行号（收缩跨度），不会把因此变紧邻的同 ref 同列两段接上。
    紧邻（``a.row_end + 1 == b.row_start``）意味着中间已无任何行，本就是一根柱；
    尤其留下**单行段**时那行写不成合并区，会被隔在柱外，看着就是「合并器件没有
    合并完这条链路」。

    只接中间无行的段；隔着现存行的一律不动（同列多并柱按行段解析仍成立）。
    """
    merges = list(grid.merges or [])
    if len(merges) < 2:
        return grid
    ids = list(grid.row_ids or [])
    by_key: dict[tuple[str, int], list] = defaultdict(list)
    others: list = []
    for m in merges:
        ref = str(getattr(m, "ref", "") or "")
        col = getattr(m, "col", None)
        if not ref or col is None:
            others.append(m)
            continue
        by_key[(ref, int(col))].append(m)
    out: list = list(others)
    for (ref, col), group in by_key.items():
        group.sort(key=lambda m: (int(m.row_start), int(m.row_end)))
        cur_lo = int(group[0].row_start)
        cur_hi = int(group[0].row_end)
        for m in group[1:]:
            lo, hi = int(m.row_start), int(m.row_end)
            if lo <= cur_hi + 1:
                cur_hi = max(cur_hi, hi)
                continue
            out.append(
                MergeRange(
                    ref=ref,
                    col=col,
                    row_start=cur_lo,
                    row_end=cur_hi,
                    row_ids=tuple(ids[cur_lo : cur_hi + 1]),
                )
            )
            cur_lo, cur_hi = lo, hi
        out.append(
            MergeRange(
                ref=ref,
                col=col,
                row_start=cur_lo,
                row_end=cur_hi,
                row_ids=tuple(ids[cur_lo : cur_hi + 1]),
            )
        )
    grid.merges = out
    return grid


def finalize_merges_after_unique(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    signal_infos=None,
) -> SheetGrid:
    """unique/reflow 后：重算 L6 ∪ 保留 prev 候选 + path-owner + prune + FIL 终对齐。"""
    prev = list(grid.merges or [])
    recomputed = compute_vertical_merges(plan, grid)
    grid.merges = union_recomputed_and_prev_merges(recomputed, prev)
    add_path_owner_merges(plan, grid, signal_infos=signal_infos)
    realign_public_fil_after_path_owner(plan, grid)
    prune_merges_missing_cells(
        grid, plan=plan, signal_infos=signal_infos,
    )
    realign_public_fil_after_path_owner(plan, grid)
    rebind_merge_row_ids(grid)
    grid.merges = normalize_merges_pin_top(grid)
    return grid


__all__ = (
    "MergeCheckError",
    "add_path_owner_merges",
    "apply_main_chain_merges",
    "apply_merges",
    "col_of_ref",
    "compute_vertical_merges",
    "condense_merge_branch_actives",
    "device_ref_visible",
    "display_left_neighbor",
    "extend_span_to_adjacent_fbrx",
    "finalize_merges_after_unique",
    "is_jump_band_row_id",
    "is_main_content_row_id",
    "is_merge_passthrough_row_id",
    "l2_merge_rehome_loop",
    "lift_copad_blocks_above_path_owners",
    "list_merge_rehome_targets",
    "normalize_merges_pin_top",
    "path_left_visible",
    "pin_merge_value_to_top",
    "prune_merges_missing_cells",
    "rebind_merge_row_ids",
    "refresh_merges_after_copad",
    "rehome_plan_for_merge_blockers",
    "seed_provisional_merges",
)

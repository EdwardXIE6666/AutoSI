"""竖并收口：钉顶、prune、path-owner 附着。"""
from __future__ import annotations

from typing import Optional

from refnum.layout.merge_emit import align_upstream_merge_span_to_path_successors
from refnum.layout.merge_span import (
    is_main_content_row_id,
    MergeCheckError,
    col_of_ref,
    condense_active_by_left_neighbor,
    condense_merge_branch_actives,
    display_left_neighbor,
    merge_col_empty,
    non_owner_transparent,
    path_left_visible,
    row_has_left_link_to_ref,
)
from refnum.layout.model import (
    VISIBLE_DEVICE_KINDS,
    MergeRange,
    PlacedCell,
    SheetGrid,
    SheetPlan,
)
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_neighbor,
    display_ref_seq,
    is_non_merge_align_ref,
    is_switch_or_coupler_device,
    should_vertical_merge,
)


def _yellow_branch_rid(rid: str) -> bool:
    s = str(rid or "")
    return s.endswith("|b1") or s.endswith("|b2")


def _structure_ic_yellow_top(
    grid: SheetGrid,
    m: MergeRange,
) -> Optional[int]:
    """结构 IC 字在黄腿时，并柱顶是黄腿，不是夹心宿主。

    须：b1/b2 已有该结构件 → row_start 收到黄腿（宿主不进这段柱）。
    可：黄腿下方其它 owner 仍盖到 row_end。
    禁：公共汇合件（不在 copad_structure_refs）改柱顶；字不在黄腿时不动。
    """
    ref = str(m.ref or "").strip()
    struct = getattr(grid, "copad_structure_refs", None) or set()
    if not ref or ref not in struct:
        return None
    n = len(grid.row_ids or [])
    mcol = int(m.col)
    yellow: list[int] = []
    for ri in range(int(m.row_start), int(m.row_end) + 1):
        if not (0 <= ri < n):
            continue
        rid = grid.row_ids[ri]
        if not _yellow_branch_rid(rid):
            continue
        cells = grid.cells.get(rid) or {}
        cell = cells.get(mcol)
        if cell is not None and str(cell.ref or "").strip() == ref:
            yellow.append(ri)
            continue
        for pl in cells.values():
            if pl is not None and str(pl.ref or "").strip() == ref:
                yellow.append(ri)
                break
    if not yellow:
        return None
    return min(yellow)


def pin_merge_value_to_top(
    grid: SheetGrid,
    m: MergeRange,
) -> Optional[MergeRange]:
    """钉顶只搬字：值写到已定 row_start，不改跨度。

    - 多行：Excel 只显示左上角 → 钉顶并清跨度内其它同 ref
    - 单行：有值则保留（段锚/unique 标记），无值则丢
    - 顶格被其它器件占用 → 这根柱非法，硬失败（禁止下移改柱顶）
    """
    top_i = int(m.row_start)
    end_i = int(m.row_end)
    if end_i < top_i:
        return None
    n = len(grid.row_ids or [])
    if not (0 <= top_i < n):
        return None
    ref = str(m.ref or "").strip()
    if not ref:
        return None
    mcol = int(m.col)
    val_i: Optional[int] = None
    val_cell: Optional[PlacedCell] = None
    for ri in range(top_i, end_i + 1):
        if not (0 <= ri < n):
            continue
        rid = grid.row_ids[ri]
        cell = (grid.cells.get(rid) or {}).get(mcol)
        if cell is not None and str(cell.ref or "").strip() == ref:
            val_i = ri
            val_cell = cell
            break
    if val_i is None or val_cell is None:
        return None
    end_i = max(end_i, val_i)
    if end_i < top_i:
        return None
    top_rid = grid.row_ids[top_i]
    top_cells = grid.cells.setdefault(top_rid, {})
    occ = top_cells.get(mcol)
    occ_ref = str(occ.ref or "").strip() if occ is not None else ""
    if occ_ref and occ_ref != ref:
        raise MergeCheckError(
            f"[handbook.merge] 钉顶顶格被占 sheet={getattr(grid, 'key', '')} "
            f"ref={ref} col={mcol} rows={top_i}:{end_i} occ={occ_ref}"
        )
    if end_i == top_i:
        # 单行：已有值即可
        return MergeRange(
            ref=ref, col=mcol, row_start=top_i, row_end=end_i, row_ids=(),
        )
    if val_i != top_i:
        top_cells[mcol] = PlacedCell(
            ref=val_cell.ref,
            device=val_cell.device,
            kind=val_cell.kind or "device",
        )
        for ri in range(top_i + 1, end_i + 1):
            if not (0 <= ri < n):
                continue
            rid = grid.row_ids[ri]
            cells = grid.cells.get(rid) or {}
            old = cells.get(mcol)
            if old is not None and str(old.ref or "").strip() == ref:
                del cells[mcol]
    return MergeRange(
        ref=ref,
        col=mcol,
        row_start=top_i,
        row_end=end_i,
        row_ids=(),
    )


def normalize_merges_pin_top(grid: SheetGrid) -> list[MergeRange]:
    """对 grid.merges 全部钉顶并丢弃非法段。"""
    kept: list[MergeRange] = []
    for m in grid.merges or []:
        y = _structure_ic_yellow_top(grid, m)
        if y is not None and y != int(m.row_start):
            m = MergeRange(
                ref=m.ref,
                col=m.col,
                row_start=y,
                row_end=max(int(m.row_end), y),
                row_ids=(),
            )
        pinned = pin_merge_value_to_top(grid, m)
        if pinned is not None:
            kept.append(pinned)
    return kept


def rebind_merge_row_ids(grid: SheetGrid) -> None:
    for i, m in enumerate(list(grid.merges or [])):
        ids = tuple(
            grid.row_ids[r]
            for r in range(m.row_start, m.row_end + 1)
            if 0 <= r < len(grid.row_ids)
        )
        grid.merges[i] = MergeRange(
            ref=m.ref,
            col=m.col,
            row_start=m.row_start,
            row_end=m.row_end,
            row_ids=ids,
        )


def prune_merges_missing_cells(
    grid: SheetGrid,
    *,
    plan: Optional[SheetPlan] = None,
    signal_infos=None,
) -> None:
    """竖合并合法性（unique 后收口）。

    - 主链格有值 → 锚点
    - COPAD 支路：跨度锚（钉顶后可无实体值）
    - L6 左邻仍可见 / 行首器件 / path-owner → 锚点
    - **share_map owner** 夹在两锚点之间 → 透明穿过（unique 抽走共享左邻后，
      中段 owner 仍应留在 FIL 并柱内）
    - 跨度止于首末**锚点**（末锚=最后左邻可见行）
    - **禁止**穿过非 owner 主链（原则2；夹心宿主无 FIL 不得被盖）
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])} if plan else {}
    share_map = (plan.share_map if plan is not None else None) or {}
    net_to_devs: dict = {}
    dev_to_nets: dict = {}
    if signal_infos is not None:
        from refnum.graph import build_net_device_maps

        net_to_devs, dev_to_nets = build_net_device_maps(signal_infos)
    kept: list[MergeRange] = []
    n = len(grid.row_ids)
    for m in grid.merges or []:
        # 硬门禁：RLCT / ANT 合并一律丢弃（COLAY 公共腿 nma 已放行）
        if is_non_merge_align_ref(m.ref, device_from_plan(plan, m.ref) if plan else None):
            continue
        ref = str(m.ref or "").strip()
        owners = set(share_map.get(ref) or ())
        mcol = int(m.col)
        # 空列过不过只认「这条 path 含不含这颗」，不跟 SVM。
        # 用 SVM 关 empty_ok → 抄同一前驱的夹心滤波 unique 后接到空壳。
        empty_ok = plan is not None
        present: list[int] = []
        value_idxs: set[int] = set()
        has_value = False
        for ri in range(int(m.row_start), int(m.row_end) + 1):
            if not (0 <= ri < n):
                continue
            rid = grid.row_ids[ri]
            pl = (grid.cells.get(rid) or {}).get(mcol)
            if pl is not None and pl.ref == ref and pl.kind != "gray":
                present.append(ri)
                value_idxs.add(ri)
                has_value = True
                continue
            if "|__gap__" in str(rid):
                continue
            # COPAD：支路有该 ref 是锚；钉顶把字搬到宿主后，原黄格空/灰
            # 仍是本柱覆盖（宿主 path 上的夹心 IC 并回宿主）。
            # 禁止：本格已是别的器件还当本柱锚。
            branch_homes = {
                x
                for p in (getattr(grid, "copad_branch_rows_by_host", None) or {}).values()
                for x in p
            }
            if "COPADBLK" in str(rid) or rid in branch_homes:
                occ_ref = str(getattr(pl, "ref", "") or "").strip() if pl else ""
                if (
                    pl is not None
                    and occ_ref == ref
                    and getattr(pl, "kind", None) != "gray"
                ):
                    present.append(ri)
                    value_idxs.add(ri)
                    has_value = True
                elif not occ_ref or getattr(pl, "kind", None) == "gray":
                    present.append(ri)
                continue
            # 真锚 = 本格有值 / 左邻仍接到本 ref / path-owner。
            # share_map owner 不作锚（只透明，见 _gap_allows）。
            # 空壳行（仅 A 列标签、无器件）不算锚——夹心插入后 L6 跨度仍含
            # unique 抽干的第二 path 时，不得靠 left_link 假阳性把开关灌进空壳行。
            has_device = any(
                pl is not None
                and str(getattr(pl, "ref", "") or "").strip()
                and getattr(pl, "kind", None) not in ("label", "gray", None)
                for pl in (grid.cells.get(rid) or {}).values()
            )
            if (
                has_device
                and plan is not None
                and row_has_left_link_to_ref(
                    plan, grid, rid, ref, mcol, id_to_row,
                )
            ):
                present.append(ri)
                continue
            if (
                has_device
                and plan is not None
                and _path_owner_main_row_active(
                    plan,
                    grid,
                    rid,
                    ref,
                    id_to_row,
                    net_to_devs=net_to_devs,
                    dev_to_nets=dev_to_nets,
                )
            ):
                present.append(ri)
                continue
            # path 含 ref：切断后每段成柱仍可靠同段兄弟成锚。
            # 开关/耦合器：左邻已抽走的主链行不是端锚（已在更左汇合）；
            # 夹在两锚之间仍可经 _gap_allows 透明穿过。取样行仍当锚。
            # 整段有没有实体左邻，在拼 run 之后再卡（抄后缀段整段丢掉）。
            row = id_to_row.get(rid)
            if (
                has_device
                and row is not None
                and row.path is not None
                and ref in {str(x) for x in display_ref_seq(row.path) if x}
            ):
                dev = device_from_plan(plan, ref) if plan is not None else None
                if (not is_switch_or_coupler_device(dev)) or is_fbrx_sample_row(
                    label=str((grid.labels or {}).get(rid, "") or ""),
                    rid=rid,
                    require_no_ant=False,
                    max_devs=None,
                    source="label_or_rid",
                ):
                    present.append(ri)
        if not has_value:
            continue
        if len(present) < 2:
            # 切断后的单行段锚：本列有字则保留，unique 按段留字。
            # 夹心 refresh 在 unique 之前 prune；丢掉 1 行段会让夹缝/下段
            # owner 被当成段外孤儿清掉。
            top_i = int(m.row_start)
            if top_i == int(m.row_end) and top_i in value_idxs:
                kept.append(
                    MergeRange(
                        ref=ref,
                        col=mcol,
                        row_start=top_i,
                        row_end=top_i,
                        row_ids=(grid.row_ids[top_i],) if 0 <= top_i < n else (),
                    )
                )
            continue
        present = sorted(set(present))
        # 同左邻多锚 → 公共干只留代表（与 L6 condense_active_by_left_neighbor 一致）
        # 必须传 plan：共享 multiport 左邻要保留全部锚点，
        # 否则 unique 后 FIL 并柱止于首个 U 支路、盖不到其余频段行。
        # **有字锚不得 condensed 丢掉**：同左邻兄弟行仍显示 FIL/SW 时须留在柱内。
        # 第一层开关/耦合器：真连接段内部的同左邻空壳须留在柱内。
        # unique 钉顶后不得只留 RX 代表。段外左邻已抽走的不是端锚。
        # FIL 仍收缩，防异支空壳超高柱。
        if plan is not None and len(present) >= 2:
            condensed = condense_active_by_left_neighbor(
                list(grid.row_ids), id_to_row, ref, present, plan=plan,
                grid=grid,
            )
            kept_present = sorted(set(condensed) | value_idxs)
            dev = device_from_plan(plan, ref)
            if (
                is_switch_or_coupler_device(dev)
                and not getattr(dev, "is_sdr", False)
            ):
                kept_present = sorted(set(present) | value_idxs)
            # unique 抽空后同左邻可能只剩顶格有字。不得因此丢掉切断后的整段。
            if len(kept_present) >= 2:
                present = kept_present

        def _gap_allows(k: int) -> bool:
            if not (0 <= k < n):
                return False
            rid_k = grid.row_ids[k]
            if rid_k in owners:
                return True
            if non_owner_transparent(
                plan, grid, rid_k, mcol, id_to_row, ref=ref,
            ):
                return True
            if not (empty_ok and merge_col_empty(grid, rid_k, mcol)):
                return False
            row_k = id_to_row.get(rid_k)
            if row_k is None or row_k.path is None:
                return False
            return ref in {str(x) for x in display_ref_seq(row_k.path) if x}

        runs: list[list[int]] = [[present[0]]]
        for a, b in zip(present, present[1:]):
            gap_ok = all(_gap_allows(k) for k in range(a + 1, b))
            if gap_ok:
                runs[-1].append(b)
            else:
                runs.append([b])
        def _run_has_entity_left(idxs: list[int]) -> bool:
            """这段里是否还有实体左邻（不是只剩抄后缀 / 过期钉顶字）。"""
            for i in idxs:
                if not (0 <= i < n):
                    continue
                rid_i = grid.row_ids[i]
                row_i = id_to_row.get(rid_i)
                if path_left_visible(grid, rid_i, row_i, ref):
                    return True
                if plan is not None and row_has_left_link_to_ref(
                    plan, grid, rid_i, ref, mcol, id_to_row,
                ):
                    return True
            return False

        for run in runs:
            if len(run) < 2:
                continue
            lo, hi = run[0], run[-1]
            if hi <= lo:
                continue
            if not _run_has_entity_left(run):
                continue
            # 并柱高度 = 锚点 min..max（值/左邻可见/path-owner）。
            # 锚点之间：share_map owner / 透明行可过（见 _gap_allows）。
            # **禁止**再向下游空壳 tree owner 外扩——unique 后异支空壳
            # 仍在 share_map 时，外扩会把 DIP/FIL 灌成超高并柱（过度合并）。
            # 同支仍显示左邻的兄弟由 L6 `_expand_same_left_col_siblings` 纳入。
            # 允许集 = 锚点 + 跨度内 share owner / 装饰 / 空列放行
            allow = set(run)
            for k in range(lo, hi + 1):
                if _gap_allows(k) or k in run:
                    allow.add(k)
            # 并柱端点不得落在「无本 ref」的 COPAD 行（夹心插入把 L6 跨度撑穿时）
            def _end_ok(idx: int) -> bool:
                if idx in run:
                    return True
                rid_e = grid.row_ids[idx] if 0 <= idx < n else ""
                if "COPADBLK" not in str(rid_e):
                    return True
                pl_e = (grid.cells.get(rid_e) or {}).get(mcol)
                return (
                    pl_e is not None
                    and str(getattr(pl_e, "ref", "") or "").strip() == ref
                )

            while hi > lo and not _end_ok(hi):
                hi -= 1
            while lo < hi and not _end_ok(lo):
                lo += 1
            if hi <= lo:
                continue
            if _merge_span_crosses_non_owner_main(
                grid, lo, hi, allow,
                col=mcol, empty_main_col_ok=empty_ok,
                ref=ref, plan=plan, id_to_row=id_to_row,
            ):
                continue
            kept.append(
                MergeRange(
                    ref=ref,
                    col=mcol,
                    row_start=lo,
                    row_end=hi,
                    row_ids=tuple(grid.row_ids[r] for r in range(lo, hi + 1)),
                )
            )
    grid.merges = kept


def _display_right_neighbor(row, ref: str) -> Optional[str]:
    """显示序（左芯片→右）上 ref 的紧邻右邻。"""
    return display_neighbor(row, ref, side="right")


def _shown_device_refs(grid: SheetGrid, row_id: str) -> set[str]:
    cells = grid.cells.get(row_id) or {}
    return {
        str(pl.ref or "").strip()
        for pl in cells.values()
        if pl.kind in VISIBLE_DEVICE_KINDS and str(pl.ref or "").strip()
    }


def _signal_net_peers(
    ref: str,
    dev_to_nets: dict[str, list[str]],
    net_to_devs: dict[str, list[str]],
) -> set[str]:
    """与 ref 共非地信号网的其它器件（多端口一脚上的 L/R 旁挂等）。"""
    from refnum.graph import is_gnd_net

    peers: set[str] = set()
    for net in dev_to_nets.get(ref, []) or []:
        if is_gnd_net(net):
            continue
        for d in net_to_devs.get(net, []) or []:
            ds = str(d or "").strip()
            if ds and ds != ref:
                peers.add(ds)
    return peers


def _path_owner_main_row_active(
    plan: SheetPlan,
    grid: SheetGrid,
    row_id: str,
    ref: str,
    id_to_row: dict,
    *,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
) -> bool:
    """主链行是否可作为夹心支路 path 归属竖并底边。

    **不在主链再落一份 FIL**：
      path 含 ref，且显示序**紧邻左邻仍可见** → 可并。
      夹心支路保留 FIL 值；主链短行用灰格顶到该列（fill_chain_continuity）。

    禁止：
      - 左邻不可见仍并（空壳泄出）
      - 左邻也是夹心结构件仍并（私有支路误并主链）
    """
    row = id_to_row.get(row_id)
    if row is None:
        return False
    shown = _shown_device_refs(grid, row_id)
    ref_s = str(ref or "").strip()
    if not ref_s:
        return False

    path_refs = {
        str(getattr(d, "ref", "") or "").strip()
        for d in (row.devices or [])
        if str(getattr(d, "ref", "") or "").strip()
    }
    if ref_s not in path_refs:
        # 多端口共网附着：path 不含 ref，但本行可见器件与 ref 共非地网
        # （主 path 不含该件，但与 FIL 共网在另一支路）
        if not net_to_devs or not dev_to_nets:
            return False
        peers = _signal_net_peers(ref_s, dev_to_nets, net_to_devs)
        return bool(peers & shown)

    # 本行仍显示 ref：左右邻或孤立，均可
    if ref_s in shown:
        left = display_left_neighbor(row, ref_s)
        right = _display_right_neighbor(row, ref_s)
        if left and left in shown:
            return True
        if right and right in shown:
            return True
        if not left and not right:
            return bool((path_refs - {ref_s}) & shown)
        return True

    # 夹心已抽走 ref：紧邻左邻 / 连续 RLCT 左段 / 更近 IC 仍可见
    # （FIL 在夹心，主链行左邻或开关仍在 → 并到该归属链）
    left = display_left_neighbor(row, ref_s)
    if left and left in shown:
        return True
    seq = [str(x) for x in display_ref_seq(row.path) if x]
    if ref_s not in seq:
        return False
    idx = seq.index(ref_s)
    # 多 path 非 RLCT 汇合件：左邻 unique 后，更近的非 RLCT 仍可见也可撑 path-owner
    # 但中间 RLCT 段必须仍全可见——否则链路已断
    # （被 unique 抽走后，不得借更远的 FIL 把本 FIL 判 active 并泄并到异支）
    multi_ok = should_vertical_merge(plan, ref_s)
    for j in range(idx - 1, -1, -1):
        cand = seq[j]
        dev = device_from_plan(plan, cand)
        if not is_non_merge_align_ref(cand, dev):
            if multi_ok and cand in shown:
                mid = seq[j + 1 : idx]
                if mid and all(m in shown for m in mid):
                    return True
                if not mid:
                    return True
            break
        # 中间 RLCT 已抽走 → 链路已断，不得借更远仍可见的 RLCT 撑 active
        # （私有 SAW 中段：旁路 path 仍含该件，但夹心抽走了中段 RLCT）
        if cand not in shown:
            break
        return True
    return False


def _max_path_prefix_col_on_owner_rows(
    plan: SheetPlan,
    grid: SheetGrid,
    ref: str,
    owner_main_ids: list[str],
) -> int:
    """主链归属行上，path 序在 ref **之前**且仍显示的器件最大列。

    竖并列必须 **严格大于** 该值，否则 Excel 合并会清空同列左邻
    （严格禁止吞并合并链路上的器件）。
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    max_c = 1
    ref_s = str(ref or "").strip()
    for rid in owner_main_ids:
        row = id_to_row.get(rid)
        if row is None or not getattr(row, "path", None):
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        if ref_s not in seq:
            continue
        for r in seq[: seq.index(ref_s)]:
            c = col_of_ref(grid, rid, r)
            if c is not None:
                max_c = max(max_c, int(c))
    return max_c


def _owner_col_blocked(
    grid: SheetGrid,
    owner_main_ids: list[str],
    col: int,
    *,
    exclude_ref: str,
) -> bool:
    """归属主链行在 col 是否已有其它器件（会吞并则需再右移一格）。"""
    ex = str(exclude_ref or "").strip()
    for rid in owner_main_ids:
        occ = (grid.cells.get(rid) or {}).get(int(col))
        if occ is None or occ.kind not in VISIBLE_DEVICE_KINDS:
            continue
        r = str(occ.ref or "").strip()
        if r and r != ex:
            return True
    return False


def _path_owner_need_col(
    plan: SheetPlan,
    grid: SheetGrid,
    ref: str,
    owner_main_ids: list[str],
) -> int:
    """竖并列下界：path 前缀右侧第一空列。

    **禁止**用「归属行整行最大占用列」——那会把夹心 FIL 顶到链路最右，
    破坏 path 序与夹心落点。
    仅：prefix+1，若该格被占则逐格 +1 直到空（多端口 peer 同理）。
    """
    need = _max_path_prefix_col_on_owner_rows(
        plan, grid, ref, owner_main_ids,
    ) + 1
    guard = 0
    while (
        _owner_col_blocked(
            grid, owner_main_ids, need, exclude_ref=ref,
        )
        and guard < 64
    ):
        need += 1
        guard += 1
    return need




def _snap_lift_target_off_foreign_sandwich(
    ids: list[str],
    grid: SheetGrid,
    target: int,
    *,
    except_prefix: str,
) -> int:
    """上提落点不得插进另一夹心三行中间。

    须：整块仍抬到 path-owner 之上（钉顶不吸字）。
    可：owner 行夹在别人夹心里 → 改到那一块最上行。
    禁：插进 b1 与 b2 之间把另一块撕开。
    """
    from refnum.layout.copad import copad_block_host_id, copad_block_prefix

    if not (0 <= target < len(ids)):
        return target
    by_prefix: dict[str, list[int]] = {}
    for i, rid in enumerate(ids):
        if "COPADBLK" not in str(rid):
            continue
        pref = copad_block_prefix(rid)
        if pref == except_prefix:
            continue
        by_prefix.setdefault(pref, []).append(i)
    for _pref, b_idxs in by_prefix.items():
        host_id = None
        for bi in b_idxs:
            host_id = copad_block_host_id(grid, bi)
            if host_id:
                break
        block_idxs = set(b_idxs)
        if host_id and host_id in ids:
            block_idxs.add(ids.index(host_id))
        if len(block_idxs) < 2:
            continue
        lo, hi = min(block_idxs), max(block_idxs)
        if hi - lo + 1 != len(block_idxs):
            continue
        if lo <= target <= hi:
            return lo
    return target


def lift_copad_blocks_above_path_owners(
    plan: SheetPlan,
    grid: SheetGrid,
) -> int:
    """夹心块上提到 path-owner 主链之上（Excel 钉顶不变量）。

    竖并值只显示在合并顶行。夹心支路上的共享 mid（如 FIL）若 path-owner
    主链行排在夹心**上方**，``pin_merge_value_to_top`` 会把
    FIL 从夹心吸到上方主链 → 夹心中空 → reflow 把右干器件填进夹心带 →
    结构崩坏（FIL / ANT / 公共尾）。

    通用规则（无项目位号）：
      对每个 COPAD 块，收集支路上可参与 path-owner 的 ref；
      若存在非宿主主链 path 含该 ref 且行序在块上方，
      则把 [b1, host, b2] 整块上移到这些 owner 中最高行之上。

    返回上提次数。
    """
    from refnum.layout.copad import (
        copad_block_host_id,
        copad_block_prefix,
    )

    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    n_lift = 0
    # 反复直到稳定（多块互不影响时通常一轮）
    for _ in range(8):
        ids = list(grid.row_ids or [])
        if not ids:
            break
        # prefix → branch indices
        by_prefix: dict[str, list[int]] = {}
        for i, rid in enumerate(ids):
            if "COPADBLK" not in str(rid):
                continue
            by_prefix.setdefault(copad_block_prefix(rid), []).append(i)
        moved = False
        # 自下而上处理，减少上移时下标连锁
        for prefix in sorted(
            by_prefix.keys(),
            key=lambda p: min(by_prefix[p]),
            reverse=True,
        ):
            b_idxs = by_prefix[prefix]
            host_id = None
            for bi in b_idxs:
                host_id = copad_block_host_id(grid, bi)
                if host_id:
                    break
            if not host_id or host_id not in ids:
                continue
            host_i = ids.index(host_id)
            block_idxs = sorted(set(b_idxs + [host_i]))
            # 要求 b1/host/b2 已连续；不连续则跳过（避免撕裂）
            lo, hi = block_idxs[0], block_idxs[-1]
            if hi - lo + 1 != len(block_idxs):
                continue
            if set(range(lo, hi + 1)) != set(block_idxs):
                continue

            branch_refs: set[str] = set()
            for bi in b_idxs:
                for pl in (grid.cells.get(ids[bi]) or {}).values():
                    if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                        continue
                    ref = str(pl.ref or "").strip()
                    dev = pl.device or device_from_plan(plan, ref)
                    if not ref or is_non_merge_align_ref(ref, dev):
                        continue
                    # 黄腿 / 可竖并 mid：与旧逻辑相同。
                    # SVM 为假的夹心 mid（抄后缀 FIL）：仅当非宿主主链左邻仍可见
                    # 才上提，否则钉顶会把字吸走。私有 SAW 中间 RLCT 已抽 → 不上提。
                    if pl.kind == "copad_leg" or should_vertical_merge(plan, ref):
                        branch_refs.add(ref)
                        continue
                    for j, rid in enumerate(ids):
                        if not is_main_content_row_id(rid) or rid == host_id:
                            continue
                        if _path_owner_main_row_active(
                            plan, grid, rid, ref, id_to_row,
                        ):
                            branch_refs.add(ref)
                            break
            if not branch_refs:
                continue

            owner_above: list[int] = []
            for j, rid in enumerate(ids):
                if j >= lo:  # 只关心块上方
                    break
                if not is_main_content_row_id(rid) or rid == host_id:
                    continue
                row = id_to_row.get(rid)
                path_refs = {
                    str(getattr(d, "ref", "") or "").strip()
                    for d in (getattr(row, "devices", None) or [])
                    if str(getattr(d, "ref", "") or "").strip()
                } if row is not None else set()
                shown = {
                    str(pl.ref or "").strip()
                    for pl in (grid.cells.get(rid) or {}).values()
                    if pl is not None
                    and pl.kind in VISIBLE_DEVICE_KINDS
                    and str(pl.ref or "").strip()
                }
                if branch_refs & (path_refs | shown):
                    owner_above.append(j)
            if not owner_above:
                continue
            target = min(owner_above)
            target = _snap_lift_target_off_foreign_sandwich(
                ids, grid, target, except_prefix=prefix,
            )
            # 已在所有 owner 之上
            if lo <= target:
                continue

            block = ids[lo : hi + 1]
            without = ids[:lo] + ids[hi + 1 :]
            # target < lo → 在 without 中下标不变
            new_ids = without[:target] + block + without[target:]
            if new_ids == ids:
                continue
            old_ids = ids
            grid.row_ids = new_ids
            # 重绑 merges 行号
            new_pos = {rid: i for i, rid in enumerate(new_ids)}
            new_merges: list[MergeRange] = []
            for m in grid.merges or []:
                covered = [
                    old_ids[i]
                    for i in range(int(m.row_start), int(m.row_end) + 1)
                    if 0 <= i < len(old_ids) and old_ids[i] in new_pos
                ]
                if not covered:
                    continue
                positions = [new_pos[r] for r in covered]
                new_merges.append(
                    MergeRange(
                        ref=m.ref,
                        col=int(m.col),
                        row_start=min(positions),
                        row_end=max(positions),
                        row_ids=tuple(covered),
                    )
                )
            grid.merges = new_merges
            n_lift += 1
            moved = True
            break  # 下标已变，重新扫描
        if not moved:
            break
    return n_lift


def _merge_span_crosses_non_owner_main(
    grid: SheetGrid,
    lo: int,
    hi: int,
    allowed_idxs: set[int],
    *,
    col: int | None = None,
    empty_main_col_ok: bool = False,
    inactive_co_owner_idxs: set[int] | None = None,
    ref: str = "",
    plan: SheetPlan | None = None,
    id_to_row: dict | None = None,
) -> bool:
    """跨度是否穿过还在走的非 owner 主链。夹心/gap、汇入即止空列、共 path 抽空不切。"""
    inactive = set(inactive_co_owner_idxs or ())
    n = len(grid.row_ids or [])
    for k in range(int(lo), int(hi) + 1):
        if k in allowed_idxs:
            continue
        if not (0 <= k < n):
            return True
        rid = grid.row_ids[k]
        if non_owner_transparent(
            plan, grid, rid, col, id_to_row, ref=ref,
        ):
            continue
        if (
            empty_main_col_ok
            and k in inactive
            and merge_col_empty(grid, rid, col)
        ):
            continue
        return True
    return False


def _path_owner_reachable_indices(
    grid: SheetGrid,
    branch_i: int,
    owner_js: list[int],
    *,
    col: int | None = None,
    empty_main_col_ok: bool = False,
    inactive_co_owner_idxs: set[int] | None = None,
    ref: str = "",
    plan: SheetPlan | None = None,
    id_to_row: dict | None = None,
) -> list[int]:
    """支路锚 + **最近**主链 path-owner（树状贴宿主，禁止灌整页）。

    历史错误：对全部 owner 取 min..max，夹心在一支旁、宿主在另一支时，
    并柱穿过中间行并泄到下方另一 COPAD 支路——并柱「太下」。

    规则：
      1) 只选**距离支路最近**的一条主链 owner（下方优先，否则上方）
      2) 跨度内不得含**其它 COPADBLK**（非本支路）
      3) 仍受非 owner 主链切断约束（``empty_main_col_ok`` 可放宽空列）
    """
    inactive = set(inactive_co_owner_idxs or ())

    def _span_ok(lo: int, hi: int, allow: set[int]) -> bool:
        """夹心→主链：中间合并列为空的 COPAD 可透明；端点不得落在无关 COPAD。"""
        if lo > hi:
            return False
        allow_all = allow | {branch_i}
        for end in (lo, hi):
            if end in allow_all:
                continue
            rid_e = grid.row_ids[end] if 0 <= end < len(grid.row_ids) else ""
            if "COPADBLK" in str(rid_e):
                return False
        for k in range(lo + 1, hi):
            if k in allow_all:
                continue
            rid = grid.row_ids[k] if 0 <= k < len(grid.row_ids) else ""
            if "COPADBLK" not in str(rid):
                continue
            if col is None:
                return False
            pl = (grid.cells.get(rid) or {}).get(int(col))
            if pl is None or getattr(pl, "kind", None) == "gray":
                continue
            if not str(getattr(pl, "ref", "") or "").strip():
                continue
            # 中间夹心合并列有其它器件 → 挡
            return False
        return not _merge_span_crosses_non_owner_main(
            grid, lo, hi, allow_all,
            col=col, empty_main_col_ok=empty_main_col_ok,
            inactive_co_owner_idxs=inactive,
            ref=ref, plan=plan, id_to_row=id_to_row,
        )

    reachable: list[int] = []
    for j in owner_js:
        lo, hi = min(branch_i, j), max(branch_i, j)
        allow = {branch_i, j}
        if _span_ok(lo, hi, allow):
            reachable.append(j)
    if not reachable:
        return [branch_i]
    below = sorted(j for j in reachable if j > branch_i)
    above = sorted((j for j in reachable if j < branch_i), reverse=True)
    # 最近下方，否则最近上方
    pick = below[0] if below else above[0]
    return sorted({branch_i, pick})


def add_path_owner_merges(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    signal_infos=None,
) -> None:
    """夹心支路 → 主链 path 归属竖并。

    硬规则：
    - **RLCT / ANT 永不合并**
    - 夹心中层空馅：黄腿/结构件都不并进宿主中层
    - 贯通宿主匹配灰接到夹心缘，不把 hub 塞进馅
    - 目标主链：path 含 ref 且紧邻左邻仍可见；或共网 peer 可见
    - 合并列 = path 前缀右侧第一空列；不够则一次右移夹心带
    - **值钉在合并顶**（``pin_merge_value_to_top``），中间可空
    - 调用前须 ``lift`` + 上下支再平衡，保证夹心在 owner 之上且 FIL 支路贴 owner
    - **禁止穿越非 owner 主链上已有器件的列**；多 path 非 RLCT 对空列中间主链可透明
      （FIL 夹在宿主旁，仍须并到归属链 — path-owner，非 multiport 后门）
    """
    from refnum.layout.copad import (
        copad_block_host_id,
        copad_block_prefix,
        shift_copad_block_band_right,
    )

    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    net_to_devs: dict = {}
    dev_to_nets: dict = {}
    if signal_infos is not None:
        from refnum.graph import build_net_device_maps

        net_to_devs, dev_to_nets = build_net_device_maps(signal_infos)
    existing = {(m.ref, m.col) for m in (grid.merges or [])}
    added: list[MergeRange] = []
    branch_homes: set[str] = set()
    for pair in (getattr(grid, "copad_branch_rows_by_host", None) or {}).values():
        branch_homes.update(pair)
    for i, rid in enumerate(grid.row_ids):
        if "COPADBLK" not in str(rid) and rid not in branch_homes:
            continue
        host_id = copad_block_host_id(grid, i)
        if not host_id:
            for h, pair in (getattr(grid, "copad_branch_rows_by_host", None) or {}).items():
                if rid in pair:
                    host_id = h
                    break
        branch_items = list((grid.cells.get(rid) or {}).items())
        for col, pl in branch_items:
            ref = str(getattr(pl, "ref", "") or "").strip()
            if not ref:
                continue
            if pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            if is_non_merge_align_ref(
                ref, pl.device or device_from_plan(plan, ref),
            ):
                continue
            col_now = col_of_ref(grid, rid, ref)
            if col_now is None:
                continue
            col = int(col_now)
            owner_js: list[int] = []
            owner_mains: list[str] = []
            # path 含 ref 的全部主链（含未 active）—— empty 穿透时不得跨过
            path_co_owner_js: list[int] = []
            for j, rj in enumerate(grid.row_ids):
                if j == i or not is_main_content_row_id(rj):
                    continue
                if host_id and rj == host_id:
                    # 夹心中层空馅：黄腿/结构件都不并进宿主中层。
                    continue
                row_j = id_to_row.get(rj)
                if row_j is not None:
                    prefs = {
                        str(getattr(d, "ref", "") or "").strip()
                        for d in (row_j.devices or [])
                        if str(getattr(d, "ref", "") or "").strip()
                    }
                    if ref in prefs:
                        path_co_owner_js.append(j)
                if _path_owner_main_row_active(
                    plan,
                    grid,
                    rj,
                    ref,
                    id_to_row,
                    net_to_devs=net_to_devs,
                    dev_to_nets=dev_to_nets,
                ):
                    owner_js.append(j)
                    owner_mains.append(rj)
            # 主链 active 支路与 L6 同一收缩策略（禁止 path-owner 灌满公共干空行）

            owner_js = condense_merge_branch_actives(
                plan,
                list(grid.row_ids),
                id_to_row,
                ref,
                sorted(owner_js),
                grid=grid,
            )
            # path-owner = 几何附着（最近一条左邻仍可见的主链），不是汇合。
            # 空列穿透只认「path 含该 ref」，不跟 SVM：抄同一前驱时 L6 仍不并，
            # 但 unique 抽空的共 path 兄弟必须透明，才能接到左邻仍可见的归属。
            # 用 SVM 关 empty_ok → 回退贴空壳、真链断路。
            # 私有 SAW：中间 RLCT 被抽走则 _path_owner_main_row_active 为假。
            empty_ok = True
            inactive_co = set(path_co_owner_js) - set(owner_js)
            indices = _path_owner_reachable_indices(
                grid,
                i,
                owner_js,
                col=col,
                empty_main_col_ok=empty_ok,
                inactive_co_owner_idxs=inactive_co,
                ref=ref, plan=plan, id_to_row=id_to_row,
            )
            # 远 active 被无关主链挡住：改贴最近可达的共 path 行（含 inactive）。
            # 必须先有 active：无左邻可见的归属时，path 含 ref 仍可能是已断
            # 私有中段（夹心抽走 RLCT），不得回退贴到旁路主链。
            if len(indices) < 2 and owner_js and path_co_owner_js:
                indices = _path_owner_reachable_indices(
                    grid,
                    i,
                    path_co_owner_js,
                    col=col,
                    empty_main_col_ok=empty_ok,
                    inactive_co_owner_idxs=inactive_co,
                    ref=ref, plan=plan, id_to_row=id_to_row,
                )
            if len(indices) < 2:
                continue
            # need_col 只用可达 owner 主链
            reach_mains = [
                grid.row_ids[j]
                for j in indices
                if j != i and is_main_content_row_id(grid.row_ids[j])
            ]
            if not reach_mains:
                continue
            need_col = _path_owner_need_col(plan, grid, ref, reach_mains)
            if col < need_col:
                can_shift = "COPADBLK" in str(rid) or rid in branch_homes
                if can_shift:
                    delta = need_col - col
                    shift_copad_block_band_right(
                        grid,
                        copad_block_prefix(rid),
                        from_col=col,
                        delta=delta,
                    )
                    col_now2 = col_of_ref(grid, rid, ref)
                    if col_now2 is None or int(col_now2) < need_col:
                        continue
                    col = int(col_now2)
            lo, hi = min(indices), max(indices)
            if hi <= lo:
                continue
            if _merge_span_crosses_non_owner_main(
                grid, lo, hi, set(indices),
                col=col, empty_main_col_ok=empty_ok,
                inactive_co_owner_idxs=inactive_co,
                ref=ref, plan=plan, id_to_row=id_to_row,
            ):
                continue
            if (ref, col) in existing:
                for k, m in enumerate(list(grid.merges or [])):
                    if m.ref == ref and m.col == col:
                        if is_non_merge_align_ref(m.ref, pl.device):
                            break
                        ns = min(lo, int(m.row_start))
                        ne = max(hi, int(m.row_end))
                        allow = set(indices)
                        for ri in range(int(m.row_start), int(m.row_end) + 1):
                            allow.add(ri)
                        if ne > ns and not _merge_span_crosses_non_owner_main(
                            grid, ns, ne, allow,
                            col=col, empty_main_col_ok=empty_ok,
                            inactive_co_owner_idxs=inactive_co,
                            ref=ref, plan=plan, id_to_row=id_to_row,
                        ):
                            grid.merges[k] = MergeRange(
                                ref=ref,
                                col=col,
                                row_start=ns,
                                row_end=ne,
                                row_ids=(),
                            )
                        break
                continue
            existing.add((ref, col))
            added.append(
                MergeRange(
                    ref=ref,
                    col=col,
                    row_start=lo,
                    row_end=hi,
                    row_ids=(),
                )
            )
    if added:
        grid.merges = list(grid.merges or []) + added
        grid.merges = normalize_merges_pin_top(grid)


def _extend_fil_merge_to_sandwich_mid_extras(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """夹心中层若有 merge.trunk 的 trunk_after RLCT，滤波并柱须盖到这一行。

    钉顶后只剩顶格时也要建/拉长并柱，不能让匹配单独落在中层把滤波断开。
    """
    ids = list(grid.row_ids or [])
    if not ids:
        return
    for st in getattr(plan, "copad_structures", None) or ():
        hub = str(getattr(getattr(st, "merge", None), "trunk_ref", "") or "").strip()
        if not hub.upper().startswith(("FIL", "LPF", "BPF", "DUP", "DPX", "TPX")):
            continue
        extras = {
            str(t or "").strip()
            for t in (getattr(st, "trunk_after", None) or ())
            if str(t or "").strip()
            and str(t).strip() != hub
            and str(t).strip()[:1].upper() in "CLRT"
        }
        if not extras:
            continue
        hub_col = None
        for rid, cells in (grid.cells or {}).items():
            for c, pl in (cells or {}).items():
                if pl is not None and str(pl.ref or "").strip() == hub:
                    hub_col = int(c)
                    break
            if hub_col is not None:
                break
        if hub_col is None:
            for m in grid.merges or []:
                if str(m.ref or "") == hub:
                    hub_col = int(m.col)
                    break
        if hub_col is None:
            continue
        for i, rid in enumerate(ids):
            if not str(rid).endswith("|mid"):
                continue
            have = {
                str(pl.ref or "").strip()
                for pl in (grid.cells.get(rid) or {}).values()
                if pl is not None
            }
            if not (extras & have):
                continue
            owners = (plan.share_map or {}).get(hub) or ()
            owner_is = [ids.index(o) for o in owners if o in ids]
            if not owner_is:
                continue
            ns = min(owner_is)
            blocked = False
            for ri in range(ns + 1, i):
                rrid = ids[ri]
                if "|__gap__" in str(rrid):
                    continue
                pl = (grid.cells.get(rrid) or {}).get(int(hub_col))
                if (
                    pl is not None
                    and pl.kind in VISIBLE_DEVICE_KINDS
                    and str(pl.ref or "").strip() not in ("", hub)
                ):
                    blocked = True
                    break
            if blocked:
                continue
            updated = False
            for k, m in enumerate(list(grid.merges or [])):
                if str(m.ref or "") != hub or int(m.col) != int(hub_col):
                    continue
                grid.merges[k] = MergeRange(
                    ref=m.ref,
                    col=m.col,
                    row_start=min(int(m.row_start), ns),
                    row_end=max(int(m.row_end), i),
                    row_ids=(),
                )
                updated = True
                break
            if not updated:
                grid.merges = list(grid.merges or []) + [
                    MergeRange(
                        ref=hub,
                        col=int(hub_col),
                        row_start=ns,
                        row_end=i,
                        row_ids=(),
                    )
                ]


def realign_public_fil_after_path_owner(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """path-owner 拉长被动并柱后，FIL 须再包住其行跨度。"""

    grid.merges = align_upstream_merge_span_to_path_successors(
        plan, grid, list(grid.merges or []),
    )
    _extend_fil_merge_to_sandwich_mid_extras(plan, grid)


def union_recomputed_and_prev_merges(
    recomputed: list[MergeRange],
    prev: list[MergeRange],
) -> list[MergeRange]:
    """重算优先；prev 里与重算不重叠的段留下。同 ref+col 允许多段。"""
    kept = list(recomputed or [])
    for m in prev or []:
        rs, cs = str(m.ref or ""), int(m.col)
        ms, me = int(m.row_start), int(m.row_end)
        overlaps = False
        for k in kept:
            if str(k.ref or "") != rs or int(k.col) != cs:
                continue
            if me < int(k.row_start) or int(k.row_end) < ms:
                continue
            overlaps = True
            break
        if not overlaps:
            kept.append(m)
    return kept

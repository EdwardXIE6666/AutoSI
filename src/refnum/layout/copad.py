"""功能5 阶段 B — L4 Co-pad + L5 主链腰部「三行夹心」装配。

目标视觉：

  上支路：          [对腿]     [对腿]     （仅平行对腿，无灰格垫）
  主链行：左干路 …  [  空白  ] … 右干路 → ANT
  下支路：          [对腿] [中段实体] [对腿]

规则：
- 仅 Co-pad **对腿**标黄（copad_leg）；中段/hub 用普通器件色
- 支路只画真实平行实体，空位不铺灰格
- 夹心中层结构列保持空白（无位号、无灰）
- 主链单行贯通；夹心三行内部不插链路空行
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from refnum.layout.model import (
    VISIBLE_DEVICE_KINDS,
    CopadBlockGeom,
    MergeRange,
    PlacedCell,
    SheetGrid,
    SheetPlan,
)
from refnum.layout.merge_span import (
    is_jump_band_row_id,
    is_main_content_row_id,
    is_merge_passthrough_row_id,
)
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_ref_seq,
    is_ant_ref,
    is_frontend_structure_role,
    is_non_merge_align_ref,
    is_pa_lna_device,
    is_rlct_ref,
    is_switch_or_coupler_device,
    should_vertical_merge,
)
from refnum.path import CoPadStructure, Device


def _leg_dev(ref: str, *, is_colay: bool = False) -> Device:
    """Co-pad / Co-lay 对腿：黄标。"""
    d = Device(ref=ref, part_type="CoLay" if is_colay else "CoPad")
    if is_colay:
        d.is_colay = True
        d.is_copad = False
    else:
        d.is_copad = True
        d.is_colay = False
    d.is_passive = False
    return d


def _plain_dev(ref: str, plan: Optional[SheetPlan] = None) -> Device:
    """支路中段/hub：普通着色，不抢黄。

    身份跟 plan（开关/IC 仍灰）。禁止空 Device 丢掉 is_ic 后走 TX/RX。
    """
    src = device_from_plan(plan, ref)
    # GPS absorb 会插空 Device；空位号不是身份，不得挡住前缀补 IC。
    if src is not None and (
        src.is_ic
        or src.is_sdr
        or src.is_ant
        or src.is_lpf
        or src.is_pa_lna
        or src.is_passive
        or src.is_shunt
        or src.is_jump_attach
    ):
        return replace(
            src,
            is_copad=False,
            is_colay=False,
            copad_label="",
            colay_label="",
        )
    d = Device(ref=ref, part_type="")
    d.is_copad = False
    ru = str(ref).upper()
    # 与同文件 hub / 不可 rehome 干路前缀同一类：结构 IC，不是位号名单。
    if ru.startswith((
        "FIL", "LPF", "BPF", "U", "PA", "LNA", "DIP", "DUP",
        "SW", "ESW", "DPX", "TPX",
    )):
        d.is_ic = True
        if ru.startswith(("LPF", "BPF")):
            d.is_lpf = True
    return d


def _leg_cell(ref: str, *, is_colay: bool = False) -> PlacedCell:
    return PlacedCell(
        ref=ref, device=_leg_dev(ref, is_colay=is_colay), kind="copad_leg",
    )


def _plain_cell(ref: str, plan: Optional[SheetPlan] = None) -> PlacedCell:
    return PlacedCell(ref=ref, device=_plain_dev(ref, plan), kind="device")


def _is_complete_structure(st: CoPadStructure) -> bool:
    if str(getattr(st, "link_kind", "") or "") == "single":
        return False
    fa = str(getattr(st.fork, "leg_a", "") or "").strip()
    fb = str(getattr(st.fork, "leg_b", "") or "").strip()
    if not fa or not fb or fa == fb:
        return False
    if st.merge is not None:
        ma = str(getattr(st.merge, "leg_a", "") or "").strip()
        mb = str(getattr(st.merge, "leg_b", "") or "").strip()
        if ma and mb and ma != mb:
            return True
    if tuple(st.branch1 or ()) or tuple(st.branch2 or ()):
        return True
    return False


def structures_for_sheet(
    plan: SheetPlan,
    structures: Optional[list[CoPadStructure]],
) -> list[tuple[str, Optional[str], CoPadStructure]]:
    if not structures:
        return []
    from refnum.copad import copad_structure_touches_refs

    # 侧支管线已清除：本 sheet 位号即主链位号（无「仅侧支挂接」一类）
    sheet_refs: set[str] = set()
    for row in plan.rows:
        for d in row.devices:
            ref = str(getattr(d, "ref", "") or "").strip()
            if ref:
                sheet_refs.add(ref)

    local = [
        st for st in structures
        if _is_complete_structure(st)
        and copad_structure_touches_refs(st, sheet_refs, main_refs=sheet_refs)
    ]
    local.sort(key=lambda st: (st.fork.leg_a, st.fork.leg_b, st.trunk_ref or ""))
    # fork_label/merge_label 仅内部标记，不再画到格子上
    return [("fork", "merge" if st.merge else None, st) for st in local]


def _mids(branch: tuple[str, ...], exclude: set[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in branch or ():
        ref = str(r or "").strip()
        if not ref or ref in exclude or ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    return out


def _structure_leg_refs(st: CoPadStructure) -> set[str]:
    legs = {
        str(st.fork.leg_a or "").strip(),
        str(st.fork.leg_b or "").strip(),
    }
    if st.merge is not None:
        legs |= {
            str(st.merge.leg_a or "").strip(),
            str(st.merge.leg_b or "").strip(),
        }
    return {r for r in legs if r}


def _handbook_copad_leg_sides(
    plan: SheetPlan,
    st: CoPadStructure,
    fork_legs: set[str],
    merge_legs: set[str],
) -> tuple[set[str], set[str]]:
    """手册左芯片 → 右 ANT：返回 (left_legs, right_legs)。

    须：夹心整段与该支路 display（芯片→天线）同向。
    定向真相 = 含 hub 的 path 上，hub 左侧腿 / hub→ANT 之间的腿。
    同一支路叉/合两端腿出现在同一 path、中间无 hub = 该支路串联方向，也要投票。
    「同一支路」以结构支路两端腿为准，不以 A 层 leg_a/leg_b 索引当支路身份。
    结构元组的 fork/merge 名字只用来配对传播，名字反了要跟 path 翻回来。
    可：只沾一条腿的 path 不投票（左合右叉取舍，不得据此整段翻面）。
    禁：支路含 hub 就认 A 层 fork→merge 名字（那会把名字反了的整段翻面）。
    禁：把同一支路两端腿的串联序当取舍丢掉、回退 fork 左。
    回退：没有任何 hub+腿 / 同支路双腿 path 证据时，才用 A 层 fork 左 / merge 右。
    一侧已投票、另一侧空：守已投票侧，禁止 fallback 把已投票侧翻面。
    """
    all_legs = {x for x in (fork_legs | merge_legs) if x}
    structure_hubs: set[str] = set()
    for branch in (
        getattr(st, "branch1", None) or (),
        getattr(st, "branch2", None) or (),
    ):
        for r in branch:
            rs = str(r or "").strip()
            if not rs or rs in all_legs:
                continue
            if is_rlct_ref(rs):
                continue
            structure_hubs.add(rs)

    left: set[str] = set()
    right: set[str] = set()
    for row in plan.rows or []:
        path = getattr(row, "path", None)
        if path is None:
            continue
        seq = display_ref_seq(path)
        if not seq:
            continue
        hubs_here = [h for h in structure_hubs if h in seq]
        if not hubs_here:
            continue
        ant_idxs = [i for i, r in enumerate(seq) if is_ant_ref(r)]
        ant_i = min(ant_idxs) if ant_idxs else len(seq)
        for h in hubs_here:
            hi = seq.index(h)
            if hi >= ant_i:
                continue
            for leg in all_legs:
                if leg not in seq:
                    continue
                li = seq.index(leg)
                if li < hi:
                    left.add(leg)
                elif hi < li < ant_i:
                    right.add(leg)

    def _propagate() -> None:
        for group in (fork_legs, merge_legs):
            if not group:
                continue
            if group & left and not (group & right):
                left.update(group)
            if group & right and not (group & left):
                right.update(group)

    _propagate()
    both = left & right
    if both:
        left -= both
        right -= both
        _propagate()
    if (left or right) and not (left & right):
        return left, right

    left = set()
    right = set()
    pair_ends: list[tuple[str, str]] = []
    if st.merge is not None:
        pair_ends.append(
            (
                str(st.fork.leg_a or "").strip(),
                str(st.merge.leg_a or "").strip(),
            )
        )
        pair_ends.append(
            (
                str(st.fork.leg_b or "").strip(),
                str(st.merge.leg_b or "").strip(),
            )
        )
        # 结构支路两端才是同一支路；A 层索引对交叉时仍要投到
        for branch in (
            getattr(st, "branch1", None) or (),
            getattr(st, "branch2", None) or (),
        ):
            ends = [
                str(r or "").strip()
                for r in branch
                if str(r or "").strip() in all_legs
            ]
            if len(ends) < 2:
                continue
            a, b = ends[0], ends[-1]
            fleg = a if a in fork_legs else (b if b in fork_legs else "")
            mleg = a if a in merge_legs else (b if b in merge_legs else "")
            if fleg and mleg and fleg != mleg:
                pair_ends.append((fleg, mleg))
    for row in plan.rows or []:
        path = getattr(row, "path", None)
        if path is None:
            continue
        seq = display_ref_seq(path)
        if not seq:
            continue
        hubs_here = [h for h in structure_hubs if h in seq]
        for fleg, mleg in pair_ends:
            if not fleg or not mleg or fleg not in seq or mleg not in seq:
                continue
            fi, mi = seq.index(fleg), seq.index(mleg)
            lo, hi = (fi, mi) if fi < mi else (mi, fi)
            if any(h in seq and lo < seq.index(h) < hi for h in hubs_here):
                continue
            if mi < fi:
                left.add(mleg)
                right.add(fleg)
            else:
                left.add(fleg)
                right.add(mleg)
    _propagate()
    both = left & right
    if both:
        left -= both
        right -= both
        _propagate()
    if (left or right) and not (left & right):
        return left, right
    return set(fork_legs), set(merge_legs)


def _branch_series_left_to_right(
    branch: tuple[str, ...],
    *,
    left_legs: set[str],
    right_legs: set[str],
    hub: str = "",
    # 旧调用：merge=左 / fork=右
    merge_legs: Optional[set[str]] = None,
    fork_legs: Optional[set[str]] = None,
) -> list[str]:
    """单条支路 → 左(芯片/前端侧)→右(天线侧)连续序列。

    禁止对角错配：支路内腿与中段保持串联邻接，不得跨支对腿。
    """
    if not left_legs and not right_legs:
        left_legs = set(merge_legs or ())
        right_legs = set(fork_legs or ())
    seq = [
        str(r or "").strip()
        for r in (branch or ())
        if str(r or "").strip() and str(r or "").strip() != hub
    ]
    if not seq:
        return []
    llegs = {x for x in left_legs if x}
    rlegs = {x for x in right_legs if x}
    # 已是左→右
    if seq[0] in llegs and seq[-1] in rlegs:
        return seq
    # 右→左（匹配 BFS / A 层 fork→merge 常如此）→ 反转
    if seq[0] in rlegs and seq[-1] in llegs:
        return list(reversed(seq))
    # 双 stub / 仅两端腿
    if len(seq) == 2 and seq[0] in rlegs and seq[1] in llegs:
        return [seq[1], seq[0]]
    if len(seq) == 2 and seq[0] in llegs and seq[1] in rlegs:
        return seq
    # 含一端腿 + 中段
    if seq[0] in rlegs and seq[-1] not in llegs:
        return list(reversed(seq))
    if seq[-1] in llegs and seq[0] not in rlegs:
        return list(reversed(seq))
    return seq


def _split_branch_ends(
    series: list[str],
    *,
    left_legs: set[str],
    right_legs: set[str],
    merge_legs: Optional[set[str]] = None,
    fork_legs: Optional[set[str]] = None,
) -> tuple[str, list[str], str]:
    """连续序列拆成 (左腿, 中段..., 右腿)；缺端则为空串。"""
    if not left_legs and not right_legs:
        left_legs = set(merge_legs or ())
        right_legs = set(fork_legs or ())
    if not series:
        return "", [], ""
    if len(series) == 1:
        r = series[0]
        if r in left_legs:
            return r, [], ""
        if r in right_legs:
            return "", [], r
        return "", [r], ""
    left, right = series[0], series[-1]
    mid = list(series[1:-1])
    all_legs = left_legs | right_legs
    # 若首尾不是腿（异常），整段当中段
    if left not in all_legs and right not in all_legs:
        return "", list(series), ""
    return left, mid, right


def _structure_fork_merge_legs(
    st: CoPadStructure,
) -> tuple[set[str], set[str]]:
    """返回 (fork_legs, merge_legs)。"""
    fork_legs: set[str] = set()
    merge_legs: set[str] = set()
    if st.fork is not None:
        fork_legs = {
            str(st.fork.leg_a or "").strip(),
            str(st.fork.leg_b or "").strip(),
        } - {""}
    if st.merge is not None:
        merge_legs = {
            str(st.merge.leg_a or "").strip(),
            str(st.merge.leg_b or "").strip(),
        } - {""}
    return fork_legs, merge_legs


def _row_path_refs(plan: SheetPlan, rid: str) -> set[str]:
    for row in plan.rows or []:
        if row.row_id != rid:
            continue
        return {
            str(getattr(d, "ref", "") or "").strip()
            for d in (row.devices or [])
            if str(getattr(d, "ref", "") or "").strip()
        }
    return set()


def _host_post_leg_trunk(
    plan: SheetPlan,
    rid: str,
    st: CoPadStructure,
) -> int:
    """最后一条结构腿往天线，是否还有非匹配、非天线的干路。

    须：夹心挂在汇合后还能接到公尾的贯通主链。
    可：短腿 path 各沾一条叉/合，不算贯通。
    禁：不得用标签 L1、有没有天线格决胜。
    """
    legs = _structure_leg_refs(st)
    if not legs:
        return 0
    row = next((r for r in (plan.rows or []) if r.row_id == rid), None)
    if row is None:
        return 0
    seq = _row_display_refs(row)
    idxs = [i for i, ref in enumerate(seq) if ref in legs]
    if not idxs:
        return 0
    for ref in seq[max(idxs) + 1 :]:
        dev = device_from_plan(plan, ref)
        if is_ant_ref(ref, dev) or is_rlct_ref(ref, dev):
            continue
        return 1
    return 0


def _host_both_sides(
    plan: SheetPlan,
    grid: SheetGrid,
    rid: str,
    st: CoPadStructure,
) -> int:
    """path/已落格是否同时碰到 fork 侧与 merge 侧。无 merge 则碰到任一腿即可。"""
    legs = _structure_leg_refs(st)
    fork_legs, merge_legs = _structure_fork_merge_legs(st)
    cells = grid.cells.get(rid) or {}
    all_refs = _row_path_refs(plan, rid) | {
        str(pl.ref or "").strip()
        for pl in cells.values()
        if pl and pl.kind in VISIBLE_DEVICE_KINDS and pl.ref
    }
    if fork_legs and merge_legs:
        return 1 if (all_refs & fork_legs) and (all_refs & merge_legs) else 0
    return 1 if legs and (all_refs & legs) else 0


def _host_placed_leg_count(
    grid: SheetGrid, rid: str, legs: set[str],
) -> int:
    cells = grid.cells.get(rid) or {}
    return sum(
        1
        for pl in cells.values()
        if pl and pl.ref in legs and pl.kind in VISIBLE_DEVICE_KINDS
    )


def _host_row_id(plan: SheetPlan, grid: SheetGrid, st: CoPadStructure) -> Optional[str]:
    """宿主主链行：已落位对腿最多；同分取汇合后还有干路的贯通。

    须：夹心挂在对腿已经落下、且汇合后还能接到公尾的那一行。
    可：落点更多仍赢；两边都有干路则按行序。
    禁：不得用标签含 L1、有没有天线、腿跨度、行上器件数决胜。
        不得把「各沾一条叉/合」当成贯通。
    """
    legs = _structure_leg_refs(st)

    best_rid: Optional[str] = None
    best_key: tuple[int, int, int] = (-1, -1, -1)
    for rid in grid.row_ids:
        if "|COPADBLK" in rid or "|__gap__" in rid:
            continue
        key = (
            _host_placed_leg_count(grid, rid, legs),
            _host_post_leg_trunk(plan, rid, st),
            _host_both_sides(plan, grid, rid, st),
        )
        if key > best_key:
            best_key = key
            best_rid = rid
    if best_rid is not None and best_key[0] > 0:
        return best_rid

    candidates: list[tuple[int, int, str]] = []
    for row in plan.rows:
        row_refs = {str(getattr(d, "ref", "") or "") for d in row.devices}
        if row_refs & legs:
            candidates.append(
                (
                    _host_post_leg_trunk(plan, row.row_id, st),
                    _host_both_sides(plan, grid, row.row_id, st),
                    row.row_id,
                )
            )
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][2]
    return None


def _rebind_merges_after_row_shuffle(
    grid: SheetGrid,
    old_ids: list[str],
) -> None:
    """row_ids 重排后按 rid 重绑 merges 行号。

    禁止 min-max 灌进未覆盖的无关主链：原先相邻的两行（如两条 FBRX 的
    行首 TRX）被夹心收拢拆开后，中间是耦合器主链，不得合成巨型并。
    跨度内多出来的行只允许夹心支路 / gap。
    """
    from refnum.layout.model import MergeRange

    new_ids = list(grid.row_ids or [])
    new_pos = {rid: i for i, rid in enumerate(new_ids)}
    new_merges: list = []
    for m in grid.merges or []:
        rs, re = int(m.row_start), int(m.row_end)
        covered = [
            old_ids[i]
            for i in range(rs, re + 1)
            if 0 <= i < len(old_ids) and old_ids[i] in new_pos
        ]
        if not covered:
            continue
        covered_set = set(covered)
        positions = [new_pos[r] for r in covered]
        lo, hi = min(positions), max(positions)
        extra_ok = True
        for i in range(lo, hi + 1):
            rid = new_ids[i]
            if rid in covered_set:
                continue
            s = str(rid)
            if "COPADBLK" in s or "|__gap__" in s:
                continue
            extra_ok = False
            break
        if not extra_ok:
            continue
        new_merges.append(
            MergeRange(
                ref=m.ref,
                col=int(m.col),
                row_start=lo,
                row_end=hi,
                row_ids=tuple(new_ids[i] for i in range(lo, hi + 1)),
            )
        )
    grid.merges = new_merges


def _repack_span_keep_passthrough(
    units: list[tuple[str, ...]],
    owned_set: set[int],
    lo: int,
    hi: int,
    passthrough_idx,
) -> list[tuple[str, ...]] | None:
    """跨度内：owner+透明留相对序，只挤非透明无关主链。无真 hole → None。"""
    keep: list[tuple[str, ...]] = []
    holes: list[tuple[str, ...]] = []
    for i in range(lo, hi + 1):
        if i in owned_set or passthrough_idx(i):
            keep.append(units[i])
        else:
            holes.append(units[i])
    if not holes:
        return None
    return units[:lo] + keep + holes + units[hi + 1 :]


def _gather_main_row_ids(grid: SheetGrid, owners: set[str]) -> None:
    """把 owners 主链行收成连续块（保相对序）；块内非 owner 挤到块后。

    夹心插入前调用：含对腿的宿主聚在一起，避免仅共享尾的行夹在 Co-pad
    簇中间。短 FBRX 不是夹心宿主——这一刀可以挤出；夹心后的 FIL 收拢
    再按透明行留下，耦合器 gather 会把取样收进第一层块。
    重排后按 rid 重绑 merges 的 row_start/end。
    """
    if len(owners) < 2:
        return
    ids = list(grid.row_ids or [])
    idxs = [
        i
        for i, rid in enumerate(ids)
        if rid in owners
        and "COPADBLK" not in str(rid)
        and "|__gap__" not in str(rid)
    ]
    if len(idxs) < 2:
        return
    lo, hi = min(idxs), max(idxs)
    if hi - lo + 1 == len(idxs):
        return
    owner_ids = [ids[i] for i in idxs]
    non_owner = [
        ids[i]
        for i in range(lo, hi + 1)
        if ids[i] not in owners
    ]
    old_ids = list(ids)
    grid.row_ids = ids[:lo] + owner_ids + non_owner + ids[hi + 1 :]
    _rebind_merges_after_row_shuffle(grid, old_ids)


def all_sandwich_mid_rows(grid: SheetGrid) -> set[str]:
    """本页**所有**夹心三连的中层（宿主）行 id。

    夹心是原子三连 (b1, host, b2)，任何行重排都不得把 host 从两条黄腿之间抽走
    ——抽走那道夹心就散架（黄腿留在原处、宿主跑到别处，看着就是 copad 组被孤立）。

    同页多块时，A 块的宿主常同时是 B 块的 chip-trunk owner。它是普通 BAND 行：
    既不等于 B 块的 geo_mid，也不含 COPADBLK 字样，所以「只挡当前 unit 的
    geo_mid」拦不住它。判据必须是**任何**夹心的中层，故收在此处作单一真相。
    """
    mids = {u[1] for u in _sandwich_atomic_units(grid) if len(u) == 3}
    mids |= set(getattr(grid, "copad_branch_rows_by_host", None) or {})
    return mids


def _sandwich_atomic_units(grid: SheetGrid) -> list[tuple[str, ...]]:
    """行序列切成原子单元：完整夹心三连 (b1,host,b2) 或单行。"""
    ids = list(grid.row_ids or [])
    units: list[tuple[str, ...]] = []
    i = 0
    n = len(ids)
    while i < n:
        rid = ids[i]
        if (
            str(rid).endswith("|b1")
            and i + 2 < n
            and str(ids[i + 2]).endswith("|b2")
        ):
            units.append((ids[i], ids[i + 1], ids[i + 2]))
            i += 3
            continue
        mapped = getattr(grid, "copad_branch_rows_by_host", None) or {}
        hit = False
        for host, pair in mapped.items():
            if i + 2 >= n or host not in ids:
                continue
            # 须与插入夹心同形：上黄 / 贯通宿主 / 下黄
            if ids[i + 1] == host and ids[i] in pair and ids[i + 2] in pair:
                units.append((ids[i], ids[i + 1], ids[i + 2]))
                i += 3
                hit = True
                break
            # 旧末位宿主（上黄/上黄/宿主），避免半迁移撕裂
            if ids[i + 2] == host and ids[i] in pair and ids[i + 1] in pair:
                units.append((ids[i], ids[i + 1], ids[i + 2]))
                i += 3
                hit = True
                break
        if hit:
            continue
        units.append((rid,))
        i += 1
    return units


def _sandwich_unit_host(grid: SheetGrid, unit: tuple[str, ...]) -> str:
    """夹心三连的贯通宿主。

    插入：中行。覆黄：登记 host（优先中行，旧末位亦可）。
    禁：用支路行当下标 1 的默认宿主。
    """
    if len(unit) != 3:
        return unit[0]
    mapped = getattr(grid, "copad_branch_rows_by_host", None) or {}
    if unit[1] in mapped:
        pair = mapped[unit[1]]
        if unit[0] in pair and unit[2] in pair:
            return unit[1]
    if unit[2] in mapped:
        pair = mapped[unit[2]]
        if unit[0] in pair and unit[1] in pair:
            return unit[2]
    return unit[1]


def _gather_owners_respecting_sandwich(
    grid: SheetGrid,
    owners: set[str],
    *,
    ref: str = "",
    plan: SheetPlan | None = None,
) -> None:
    """夹心后收拢可竖并 owner：夹心三连作原子，禁止拆 b1/host/b2。

    解决冲突：
      - 单腿 gather 收紧后，FIL 宿主夹心把其它 FIL owner 隔开 → 公共链/并柱残缺
      - 若对主链再 gather 又不认夹心原子 → 支路被挤散
      - FIL 公尾 holes 若只夹着 **L6 透明行**（短 FBRX path 不含该 FIL、
        夹心支路、gap）→ 不是 hole。挤出取样会拆掉已贴好的前端耦合器块。

    规则：owner 判定时夹心单元的「代表」= host；整单元随 host 一起移动。
    仅公尾 FIL/LPF：透明行留在块内相对位置，只挤出真正的无关主链。
    耦合器/开关 U*：插入夹心三连 / gap 留相对序（不是无关主链）；
    真无关主链挤出。取样若是该 U 的 owner 已在 owned 里，收到块末。
    U* 取样行放到 owner 块末：相对序若把 FBRX 插在频段支干中间，
    同跨度的 FIL 第一层会被取样链占列切断。
    """
    if len(owners) < 2:
        return
    units = _sandwich_atomic_units(grid)
    if len(units) < 2:
        return

    def _unit_owned(u: tuple[str, ...]) -> bool:
        if len(u) == 3:
            host = _sandwich_unit_host(grid, u)
            if host in owners:
                return True
            return any(
                rid in owners
                for rid in u
                if rid != host
                and is_main_content_row_id(rid)
            )
        rid = u[0]
        if is_merge_passthrough_row_id(rid):
            return False
        return rid in owners

    def _unit_is_fbrx(u: tuple[str, ...]) -> bool:
        rid = _sandwich_unit_host(grid, u) if len(u) == 3 else u[0]
        return is_fbrx_sample_row(
            rid=rid, require_no_ant=True, max_devs=None, source="rid",
        )

    owned_idxs = [i for i, u in enumerate(units) if _unit_owned(u)]
    if len(owned_idxs) < 2:
        return
    lo, hi = min(owned_idxs), max(owned_idxs)
    if hi - lo + 1 == len(owned_idxs):
        return
    owned_set = set(owned_idxs)
    # 两条取样只共享左端 IC 时，不准跨无关主链收成一块（会拆开各自的开关/耦合器）。
    if all(_unit_is_fbrx(units[i]) for i in owned_idxs):
        def _main_hole(u: tuple[str, ...]) -> bool:
            rid = _sandwich_unit_host(grid, u) if len(u) == 3 else u[0]
            s = str(rid)
            if is_merge_passthrough_row_id(s):
                return False
            return not _unit_is_fbrx(u)

        if any(
            _main_hole(units[i])
            for i in range(lo, hi + 1)
            if i not in owned_set
        ):
            return
    rs = str(ref or "").strip()
    ru = rs.upper()
    # 公尾汇合件（滤波 / 分频 / 多工 / SAW）走下方 passthrough 保护：取样行
    # path 不含它 → 是透明行不是 hole，挤出会把取样链拆离它自己的耦合器。
    # 开关 / 耦合器是前端选择器件，走 U* 分支（owner 取样收块末）。
    # **判器件类别，不判位号前缀**：TRIP/DIP/DUP 与 FIL 同类，曾因只认
    # FIL/LPF 前缀而拿不到保护，把 FBRX 取样行挤到表底。
    _ref_dev = device_from_plan(plan, rs) if plan is not None else None
    if _ref_dev is not None:
        public_tail = is_frontend_structure_role(
            _ref_dev
        ) and not is_switch_or_coupler_device(_ref_dev)
    else:
        # 无 plan/器件信息时退回前缀（part_type 缺失的兜底，勿作主判据）
        public_tail = ru.startswith("FIL") or ru.startswith("LPF")
    owner_units = [units[i] for i in owned_idxs]
    if public_tail:
        new_units = _repack_span_keep_passthrough(
            units,
            owned_set,
            lo,
            hi,
            lambda i: all(
                is_merge_passthrough_row_id(
                    rid, ref=rs, plan=plan, allow_foreign_fbrx=True,
                )
                for rid in units[i]
            ),
        )
        if new_units is None:
            return
    else:
        # U*：插入夹心三连 / gap 是结构原子，不是无关主链。
        # 须：汉堡留相对序，不得整块当 hole 沉底。
        # 可：真无关主链挤出；owner FBRX 收到块末。
        # 禁：把 FIL 的 FBRX-path 透明套到 U*（取样粘错簇）。
        def _is_struct_atom(u: tuple[str, ...]) -> bool:
            return all(
                is_merge_passthrough_row_id(rid) for rid in u
            )

        keep: list[tuple[str, ...]] = []
        holes: list[tuple[str, ...]] = []
        for i in range(lo, hi + 1):
            u = units[i]
            if i in owned_set or _is_struct_atom(u):
                keep.append(u)
            else:
                holes.append(u)
        if not holes:
            return
        if any(_unit_is_fbrx(u) for u in keep if _unit_owned(u)):
            band = [
                u for u in keep
                if not (_unit_owned(u) and _unit_is_fbrx(u))
            ]
            fbrx = [
                u for u in keep
                if _unit_owned(u) and _unit_is_fbrx(u)
            ]
            keep = band + fbrx
        new_units = units[:lo] + keep + holes + units[hi + 1 :]
    old_ids = list(grid.row_ids or [])
    grid.row_ids = [rid for u in new_units for rid in u]
    _rebind_merges_after_row_shuffle(grid, old_ids)


def _gather_vertical_merge_owners_after_copad(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """夹心装配后：按可竖并 ref 收拢 owner（夹心原子），小 |owners| 优先。

    同时服务多左邻汇合 FIL 与开关扇出：
    小集合先收，大集合后收时夹心仍整块移动。
    **先收开关/耦合器，再收 FIL/LPF 公尾**：U* 先把取样放到块末并挤出
    无关行，FIL 再收第一层，避免取样占列切断滤波柱。

    **不收行首 LNA/PA/TRX**（显示序下标 0）：允许拆格，把行序让给
    第一层前端/滤波。再 gather 行首会把已连续的开关/FIL 对拆成全 RX|全 TX。

    夹心插入后与钉顶上提后各跑一次：上提只动汉堡，须再收第一层前端。

    夹心 FIL 的 path-owner（含 SVM 为假、share 只有 1 行）也要贴夹心：
    结构/路径含该 FIL → owner；path 不含该 FIL 的 JUMP 是 hole。
    """
    lnc = getattr(plan, "left_neighbor_count", None) or {}
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    items = sorted(
        (
            (str(ref), set(owners or ()))
            for ref, owners in (getattr(plan, "share_map", None) or {}).items()
            if str(ref or "").strip()
            and len(owners or ()) >= 2
            and should_vertical_merge(plan, str(ref), lnc)
        ),
        key=lambda it: (
            1 if str(it[0]).upper().startswith(("FIL", "LPF")) else 0,
            len(it[1]),
            it[0],
        ),
    )
    for _ref, owners in items:
        hint = device_from_plan(plan, _ref)
        if is_pa_lna_device(hint):
            continue
        idxs: list[int] = []
        for rid in owners:
            row = id_to_row.get(rid)
            if row is None or not row.path:
                continue
            seq = display_ref_seq(row.path)
            if _ref in seq:
                idxs.append(seq.index(_ref))
        if idxs and (sum(idxs) / len(idxs)) < 1.0:
            continue
        present = {rid for rid in owners if rid in (grid.row_ids or ())}
        if len(present) >= 2:
            _gather_owners_respecting_sandwich(
                grid, present, ref=_ref, plan=plan,
            )
    for _ref, owners in _sandwich_fil_path_owners(plan, grid):
        present = {rid for rid in owners if rid in (grid.row_ids or ())}
        if len(present) >= 2:
            _gather_owners_respecting_sandwich(
                grid, present, ref=_ref, plan=plan,
            )
    _attach_chip_trunk_rows_before_sandwich(plan, grid)


def _sandwich_fil_path_owners(
    plan: SheetPlan,
    grid: SheetGrid,
) -> list[tuple[str, set[str]]]:
    """夹心块里的 FIL → 该夹心宿主 + path 含该 FIL 的主链。

    汇入即止的单链 FIL（SVM 为假）也要贴夹心，不能只收 share_map≥2。
    """
    items: list[tuple[str, set[str]]] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    for unit in _sandwich_atomic_units(grid):
        if len(unit) != 3:
            continue
        host = _sandwich_unit_host(grid, unit)
        fils: list[str] = []
        for rid in unit:
            for pl in (grid.cells.get(rid) or {}).values():
                rs = str(getattr(pl, "ref", "") or "").strip()
                if rs.upper().startswith("FIL") and rs not in fils:
                    fils.append(rs)
        for fil in fils:
            owners = {host}
            for rid in grid.row_ids or []:
                if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                    continue
                row = id_to_row.get(rid)
                if row is None or row.path is None:
                    continue
                if fil in display_ref_seq(row.path):
                    owners.add(rid)
            key = (fil, frozenset(owners))
            if key in seen or len(owners) < 2:
                continue
            seen.add(key)
            items.append((fil, owners))
    return items


def _attach_chip_trunk_rows_before_sandwich(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """merge.trunk 第一层 owner 贴夹心簇，两臂占并柱最上/最下格。

    须：两条及以上 owner 时，夹心三连夹在第一层两臂中间；并柱最上/最下是这两条频段。
    可：只有一条 owner 仍钉在 b1 上一行；中层 extras 落在两臂之间则被并柱盖到。
    禁：两条都钉在夹心上方，再把并柱底拉到中层 extras，把一条 owner 夹在并区中段。
        禁插进夹心宿主与其它并柱 owner 之间（会变成那根柱的 hole 再被挤走）。
    """
    structures = list(getattr(plan, "copad_structures", None) or [])
    if not structures:
        return
    ids = list(grid.row_ids or [])
    if not ids:
        return
    sandwich_mids = all_sandwich_mid_rows(grid)
    for unit in _sandwich_atomic_units(grid):
        if len(unit) != 3:
            continue
        b1, geo_mid, b2 = unit
        ids = list(grid.row_ids or [])
        below = None
        if b2 in ids:
            bi = ids.index(b2)
            if bi + 1 < len(ids) and is_main_content_row_id(ids[bi + 1]):
                below = ids[bi + 1]
        guests: list[str] = []
        seen: set[str] = set()
        for st in structures:
            st_host = _host_row_id(plan, grid, st)
            if st_host != geo_mid and st_host != below:
                continue
            hubs = _structure_chip_trunk_hubs(st)
            if not hubs:
                continue
            for rid in _owner_rows_for_chip_trunk(
                plan, grid, st_host, frozenset(hubs),
            ):
                if (
                    not rid
                    or rid == st_host
                    or rid in sandwich_mids
                    or rid in unit
                    or rid in seen
                    or rid not in ids
                    or "COPADBLK" in str(rid)
                    or "|__gap__" in str(rid)
                ):
                    continue
                seen.add(rid)
                guests.append(rid)
        if not guests:
            continue
        ids = list(grid.row_ids or [])
        guest_set = set(guests)
        grid.copad_chip_trunk_row_ids.update(guest_set)
        extracted = [rid for rid in ids if rid in guest_set]
        remaining = [rid for rid in ids if rid not in guest_set]
        if b1 not in remaining or not extracted:
            continue
        if len(extracted) >= 2 and b2 in remaining:
            first, last = extracted[0], extracted[-1]
            rest = extracted[1:-1]
            insert_top = remaining.index(b1)
            remaining = remaining[:insert_top] + [first] + rest + remaining[insert_top:]
            insert_bot = remaining.index(b2) + 1
            new_ids = remaining[:insert_bot] + [last] + remaining[insert_bot:]
        else:
            insert_at = remaining.index(b1)
            new_ids = remaining[:insert_at] + extracted + remaining[insert_at:]
        if new_ids == ids:
            continue
        old_ids = list(ids)
        grid.row_ids = new_ids
        _rebind_merges_after_row_shuffle(grid, old_ids)
        ids = list(grid.row_ids or [])


def _main_rows_touching_structure_legs(
    plan: SheetPlan,
    grid: SheetGrid,
    legs: set[str],
) -> set[str]:
    """path 或已落格含结构对腿的主链行。

    历史：任意单腿命中即收 → 含对旁路被拽进夹心宿主簇，
    与同开关扇出的其它频段拆开（holes 升高、并柱塌）。
    现仅作入口；夹心前收拢请用 ``_copad_preinsert_gather_rows``。
    """
    if not legs:
        return set()
    out: set[str] = set()
    for rid in grid.row_ids or []:
        if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        path_refs = _row_path_refs(plan, rid)
        if path_refs & legs:
            out.add(rid)
            continue
        cells = grid.cells.get(rid) or {}
        if any(
            pl and pl.ref in legs and pl.kind in VISIBLE_DEVICE_KINDS
            for pl in cells.values()
        ):
            out.add(rid)
    return out


def _structure_leg_pairs(st: CoPadStructure) -> list[frozenset[str]]:
    """结构上的完整对腿集合（fork / merge 各一对）。"""
    pairs: list[frozenset[str]] = []
    for pair in (getattr(st, "fork", None), getattr(st, "merge", None)):
        if pair is None:
            continue
        a = str(getattr(pair, "leg_a", "") or "").strip()
        b = str(getattr(pair, "leg_b", "") or "").strip()
        if a and b:
            pairs.append(frozenset((a, b)))
    return pairs


def _copad_preinsert_gather_rows(
    plan: SheetPlan,
    grid: SheetGrid,
    structures: list[CoPadStructure],
    geoms: list[CopadBlockGeom],
) -> set[str]:
    """夹心插入前应收拢的主链行（原则1，且不拆同开关扇出簇）。

    只收：
      1) 本结构宿主（``attach_after_row_id`` / ``_host_row_id``）
      2) path 或落格含**完整** fork 对或 merge 对的主链
         （两腿都在 → 真·对腿宿主/对支；单腿路过的含对旁路不收）
      3) path 含本结构 **merge.trunk_ref / trunk_after 芯片侧干**
         （FIL/U/DIP：合并后匹配须贴夹心，不是沾一条黄腿）

    禁止：凡 path 沾一条腿就收（会把含对旁路拽到无关夹心旁）。
    """
    out: set[str] = set()
    for g in geoms or []:
        hid = str(getattr(g, "attach_after_row_id", "") or "").strip()
        if hid and hid in (grid.row_ids or ()):
            out.add(hid)
    for st in structures or []:
        host = _host_row_id(plan, grid, st)
        if host and host in (grid.row_ids or ()):
            out.add(host)
        pairs = _structure_leg_pairs(st)
        for rid in grid.row_ids or []:
            if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                continue
            path_refs = _row_path_refs(plan, rid)
            shown = {
                str(pl.ref or "").strip()
                for pl in (grid.cells.get(rid) or {}).values()
                if pl is not None
                and pl.kind in VISIBLE_DEVICE_KINDS
                and str(pl.ref or "").strip()
            }
            have = path_refs | shown
            if pairs and any(pair <= have for pair in pairs):
                out.add(rid)
        chip_hubs = _structure_chip_trunk_hubs(st)
        if chip_hubs:
            for rid in grid.row_ids or []:
                if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                    continue
                path_refs = _row_path_refs(plan, rid)
                shown = {
                    str(pl.ref or "").strip()
                    for pl in (grid.cells.get(rid) or {}).values()
                    if pl is not None
                    and pl.kind in VISIBLE_DEVICE_KINDS
                    and str(pl.ref or "").strip()
                }
                if chip_hubs & (path_refs | shown):
                    out.add(rid)
    return out


def _gather_hosts_before_insert(
    plan: SheetPlan,
    grid: SheetGrid,
    structures: list[CoPadStructure],
    geoms: list[CopadBlockGeom],
) -> None:
    """夹心插入前按**每一块** Co-pad 收拢宿主，禁止跨结构粘成一块。

    须：同一结构的完整对腿 / 宿主 / merge 干路主链连续，夹心才能贴在自己的树上。
    可：只有一行宿主则不收。
    禁：把页上所有夹心宿主并进一次 gather（分频簇和开关簇会粘在一起，
        汉堡插错簇、本行 hop 过夹心中段、并柱穿越汉堡）。
    """
    sts = list(structures or ())
    if not sts:
        hosts = {
            str(g.attach_after_row_id or "").strip()
            for g in (geoms or [])
            if g.attach_after_row_id
        }
        if len(hosts) >= 2:
            _gather_main_row_ids(grid, hosts)
        return
    for st in sts:
        host = _host_row_id(plan, grid, st)
        st_geoms = [
            g for g in (geoms or [])
            if str(getattr(g, "attach_after_row_id", "") or "").strip()
            == str(host or "")
        ]
        rows = _copad_preinsert_gather_rows(plan, grid, [st], st_geoms)
        if len(rows) >= 2:
            _gather_main_row_ids(grid, rows)


def _structure_chip_trunk_hubs(st: CoPadStructure) -> set[str]:
    """合并盘芯片侧干上的滤波/芯片（FIL/U/DIP），不含任意 RLCT。"""
    out: set[str] = set()
    merge = getattr(st, "merge", None)
    mtr = str(getattr(merge, "trunk_ref", "") or "").strip()
    if mtr:
        out.add(mtr)
    for t in getattr(st, "trunk_after", ()) or ():
        ts = str(t or "").strip()
        if ts:
            out.add(ts)
    hubs: set[str] = set()
    for ref in out:
        ru = ref.upper()
        if ru.startswith(
            ("FIL", "LPF", "BPF", "DIP", "DUP", "DPX", "TPX", "U", "PA", "SW")
        ):
            hubs.add(ref)
    return hubs


def _base_col_for_structure(
    grid: SheetGrid,
    refs: frozenset[str],
    *,
    prefer_legs: Optional[set[str]] = None,
) -> int:
    """结构列带起点：优先对腿列，避免 hub/共享中段把带起点拉偏。"""
    leg_cols: list[int] = []
    any_cols: list[int] = []
    prefer = prefer_legs or set()
    for cells in (grid.cells or {}).values():
        for c, pl in (cells or {}).items():
            if pl is None or not pl.ref:
                continue
            if pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            if pl.ref not in refs:
                continue
            any_cols.append(int(c))
            if pl.ref in prefer:
                leg_cols.append(int(c))
    if leg_cols:
        return max(2, min(leg_cols))
    if any_cols:
        return max(2, min(any_cols))
    return 2


def build_one_block_geom(
    plan: SheetPlan,
    fork_label: str,
    merge_label: Optional[str],
    st: CoPadStructure,
    *,
    block_index: int,
    grid: Optional[SheetGrid] = None,
    foreign_legs: Optional[set[str]] = None,
) -> CopadBlockGeom:
    """局部列带：

      上支路：左端腿 | 中段… | 右端腿   （同支路串联，禁对角错配）
      中层主链：左干 …… 空白 …… 右干
      下支路：左端腿 | 中段… | 右端腿

    左右端与该支路芯片→天线同向。含 hub 的 path 上 hub 两侧腿序是
    定向真相；结构 fork/merge 名字反了要跟 path 翻回来。

    - 仅对腿黄标；中段普通色
    - **不把 trunk_ref/hub 画进支路**（避免抢走主链右干）
    - structure_refs 只含对腿 + 支路中段（删格范围）
    - 默认较长中段支路放下行（FIL 居下）；装配阶段可再按邻接 path 亲和度换位
    """
    _ = fork_label
    is_colay_blk = str(getattr(st, "link_kind", "") or "") == "colay"
    block_id = (
        f"COLAYBLK{block_index + 1}" if is_colay_blk
        else f"COPADBLK{block_index + 1}"
    )
    hub = str(st.trunk_ref or "").strip()
    merge_legs = set()
    if st.merge is not None:
        merge_legs = {
            str(st.merge.leg_a or "").strip(),
            str(st.merge.leg_b or "").strip(),
        } - {""}
    fork_legs = {
        str(st.fork.leg_a or "").strip(),
        str(st.fork.leg_b or "").strip(),
    } - {""}
    legs = merge_legs | fork_legs
    left_legs, right_legs = _handbook_copad_leg_sides(
        plan, st, fork_legs, merge_legs,
    )
    # 左/右端补腿时用的 pair 序（与 A 层 leg_a/leg_b 索引对齐）
    if left_legs == fork_legs and right_legs == merge_legs:
        left_pair = [
            str(st.fork.leg_a or "").strip(),
            str(st.fork.leg_b or "").strip(),
        ]
        right_pair = (
            [
                str(st.merge.leg_a or "").strip(),
                str(st.merge.leg_b or "").strip(),
            ]
            if st.merge is not None
            else ["", ""]
        )
    else:
        left_pair = (
            [
                str(st.merge.leg_a or "").strip(),
                str(st.merge.leg_b or "").strip(),
            ]
            if st.merge is not None
            else ["", ""]
        )
        right_pair = [
            str(st.fork.leg_a or "").strip(),
            str(st.fork.leg_b or "").strip(),
        ]

    s1 = _branch_series_left_to_right(
        tuple(st.branch1 or ()),
        left_legs=left_legs,
        right_legs=right_legs,
        hub=hub,
    )
    s2 = _branch_series_left_to_right(
        tuple(st.branch2 or ()),
        left_legs=left_legs,
        right_legs=right_legs,
        hub=hub,
    )
    # 缺完整支路时：用 leg_a/leg_b 同索引配对补全（非对角）
    def _ensure_ends(series: list[str], idx: int) -> list[str]:
        left, mid, right = _split_branch_ends(
            series, left_legs=left_legs, right_legs=right_legs,
        )
        if not left and any(left_pair):
            left = (
                left_pair[idx]
                if idx < len(left_pair)
                else (left_pair[0] if left_pair else "")
            )
        if not right and any(right_pair):
            right = (
                right_pair[idx]
                if idx < len(right_pair)
                else (right_pair[0] if right_pair else "")
            )
        out: list[str] = []
        if left:
            out.append(left)
        out.extend(mid)
        if right:
            out.append(right)
        # 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for r in out:
            if not r or r in seen:
                continue
            seen.add(r)
            uniq.append(r)
        return uniq

    s1 = _ensure_ends(s1, 0)
    s2 = _ensure_ends(s2, 1)
    # 较长中段放下支（可视）；仅 mid 数，不含腿
    def _n_mid(series: list[str]) -> int:
        return sum(1 for r in series if r not in legs)

    if _n_mid(s1) >= _n_mid(s2):
        series_up, series_lo = s2, s1
    else:
        series_up, series_lo = s1, s2

    left_up, mid_up, right_up = _split_branch_ends(
        series_up, left_legs=left_legs, right_legs=right_legs,
    )
    left_lo, mid_lo, right_lo = _split_branch_ends(
        series_lo, left_legs=left_legs, right_legs=right_legs,
    )
    # 支路中段非 RLCT 留黄腿（与蜂窝同一套）。GPS 不得抽到宿主。
    n_mid = max(len(mid_up), len(mid_lo))

    def _end_cell(ref: str) -> PlacedCell:
        if ref in legs:
            return _leg_cell(ref, is_colay=is_colay_blk)
        return _plain_cell(ref, plan)

    cells: list[tuple[int, int, PlacedCell]] = []
    col = 1
    # 列序：左端腿 → 中段 → 右端腿；上下同行同一支路串联
    # Co-lay 无左端腿：仅中段 + 右端腿
    if left_up or left_lo:
        if left_up:
            cells.append((0, col, _end_cell(left_up)))
        if left_lo:
            cells.append((1, col, _end_cell(left_lo)))
        col += 1
    for i in range(n_mid):
        r_up = mid_up[i] if i < len(mid_up) else ""
        r_lo = mid_lo[i] if i < len(mid_lo) else ""
        if not r_up and not r_lo:
            continue
        if r_up:
            cells.append((0, col, _end_cell(r_up)))
        if r_lo:
            cells.append((1, col, _end_cell(r_lo)))
        col += 1
    if right_up or right_lo:
        if right_up:
            cells.append((0, col, _end_cell(right_up)))
        if right_lo:
            cells.append((1, col, _end_cell(right_lo)))
        col += 1

    refs: set[str] = set(legs)
    refs |= {r for r in series_up + series_lo if r}
    # host_trunk 迁回宿主中层：
    #   - trunk_after（merge 盘后干）
    #   - trunk_before 中**不可竖并**的被动 RLCT 挂件
    # 禁止：FIL/U*、colay 干路等可竖并件（多行并柱会被 rehome 拆掉）
    # 禁止：另一 copad 组的对腿（同网两组时 pad extras 会把它们当干路）
    foreign = {
        str(x or "").strip()
        for x in (foreign_legs or ())
        if str(x or "").strip()
    }
    host_trunk: set[str] = set()
    for t in getattr(st, "trunk_after", ()) or ():
        tr = str(t or "").strip()
        if tr and tr not in foreign:
            host_trunk.add(tr)
    for t in getattr(st, "trunk_before", ()) or ():
        tr = str(t or "").strip()
        if not tr or tr in foreign or not _is_rehomeable_trunk_before_ref(tr):
            continue
        dev = device_from_plan(plan, tr)
        # 可竖并（colay 干路等）不进 host_trunk rehome
        if not is_non_merge_align_ref(tr, dev):
            continue
        host_trunk.add(tr)

    host: Optional[str] = None
    if grid is not None:
        host = _host_row_id(plan, grid, st)

    return CopadBlockGeom(
        block_id=block_id,
        fork_label=fork_label or "fork",
        merge_label=merge_label,
        attach_after_row_id=host,
        cells=cells,
        hub_local_cols=[],
        max_local_col=max(1, col - 1),
        structure_refs=frozenset(r for r in refs if r),
        host_trunk_refs=frozenset(host_trunk),
    )


def _row_display_refs(row: object) -> list[str]:
    path = getattr(row, "path", None)
    if path is not None:
        return [str(x) for x in display_ref_seq(path) if x]
    return [
        str(getattr(d, "ref", "") or "").strip()
        for d in (getattr(row, "devices", None) or [])
        if str(getattr(d, "ref", "") or "").strip()
    ]


def _legs_inline_on_seq(seq: list[str], legs: set[str]) -> bool:
    """两腿都在这一行，且之间只有腿自己或 R/L/C/T。"""
    if len(legs) < 2 or not legs <= set(seq):
        return False
    idxs = [i for i, r in enumerate(seq) if r in legs]
    for r in seq[min(idxs) : max(idxs) + 1]:
        if r in legs or is_rlct_ref(r):
            continue
        return False
    return True


def _is_inline_entry_structure(plan: SheetPlan, st: CoPadStructure) -> bool:
    """两腿已在同一贯通主链行上相邻（或只隔 RLCT）→ 不做三行夹心。

    须：两腿不在同一行，或中间隔了滤波/开关等 → 仍做夹心。
        支路含非 RLCT 中段（FIL/DIP/IC）→ 仍做夹心（匹配网不得摊成链）。
    可：已经画在同一行挨着、且没有非 RLCT 中段 → 内联，不插三行。
    禁：不得用页名、前几格、中段个数决定；不得因 hub 字在别页就取消。
    """
    legs = _structure_leg_refs(st)
    if len(legs) < 2:
        return False
    mid_non_rlct = [
        str(r or "").strip()
        for r in (*(st.branch1 or ()), *(st.branch2 or ()))
        if str(r or "").strip()
        and str(r).strip() not in legs
        and not is_rlct_ref(str(r).strip())
    ]
    if mid_non_rlct:
        return False
    for row in plan.rows or []:
        if _legs_inline_on_seq(_row_display_refs(row), legs):
            return True
    return False


def _is_gps_inline_entry_structure(plan: SheetPlan, st: CoPadStructure) -> bool:
    """GPS 内联 = 通用内联。path 只走一条臂时不得把未进 path 的腿内联掉。"""
    return _is_inline_entry_structure(plan, st)


def build_copad_block_geoms(
    plan: SheetPlan,
    structures: Optional[list[CoPadStructure]] = None,
    *,
    grid: Optional[SheetGrid] = None,
    **_kwargs,
) -> list[CopadBlockGeom]:
    labeled = structures_for_sheet(plan, structures)
    leg_sets = [_structure_leg_refs(st) for _fl, _ml, st in labeled]
    out: list[CopadBlockGeom] = []
    idx = 0
    for i, (fl, ml, st) in enumerate(labeled):
        if _is_inline_entry_structure(plan, st):
            continue
        foreign: set[str] = set()
        for j, legs in enumerate(leg_sets):
            if j != i:
                foreign |= legs
        out.append(
            build_one_block_geom(
                plan, fl, ml, st, block_index=idx, grid=grid,
                foreign_legs=foreign,
            )
        )
        idx += 1
    return out


def _shift_merges(merges: list[MergeRange], insert_at: int, n: int) -> list[MergeRange]:
    """插入行后平移并柱下标。

    若在并柱**内部**插入（rs < insert_at <= re）：只把 re += n，Excel 矩形
    会盖住新夹心行。后续 prune 会砍掉无 ref 的 COPAD 端点；此处仍平移以
    保持主链 owner 行仍在跨度内。
    """
    if n <= 0:
        return list(merges or [])
    out: list[MergeRange] = []
    for m in merges or []:
        rs, re = m.row_start, m.row_end
        if rs >= insert_at:
            rs += n
            re += n
        elif re >= insert_at:
            re += n
        out.append(
            MergeRange(ref=m.ref, col=m.col, row_start=rs, row_end=re, row_ids=())
        )
    return out




def _remove_structure_refs(
    grid: SheetGrid,
    refs: frozenset[str],
    *,
    host_id: str,
    leg_refs: set[str],
) -> None:
    """从网格删结构副本，由 sandwich 支路独占显示。

    - 对腿 + 支路中段：全表主链删（避免 FIL 中段留在多条主链上被 L6 巨型竖并，
      再被 unique 锚回主链导致夹心丢 FIL）
    - COPAD 支路行稍后写入，此处一并清掉旧格无妨
    - path 归属竖并仍可按 path.devices 扩跨度（无字占位）
    """
    _ = host_id
    _ = leg_refs
    for rid in list(grid.row_ids):
        cells = grid.cells.get(rid) or {}
        for c in list(cells.keys()):
            pl = cells[c]
            if pl.kind not in VISIBLE_DEVICE_KINDS or pl.ref not in refs:
                continue
            del cells[c]


def _main_row_ref_counts(grid: SheetGrid) -> dict[str, int]:
    """主链行（非夹心/空行）上各 ref 出现次数。"""
    from collections import Counter

    cnt: Counter[str] = Counter()
    for rid, cells in (grid.cells or {}).items():
        if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        seen: set[str] = set()
        for pl in (cells or {}).values():
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            ref = str(pl.ref or "").strip()
            if ref and ref not in seen:
                seen.add(ref)
                cnt[ref] += 1
    return dict(cnt)


def _trunk_after_band_side(
    plan: Optional[SheetPlan],
    host_id: str,
    trunk_refs: frozenset[str],
    structure_refs: frozenset[str],
) -> str:
    """trunk_after 相对夹心列带应落在哪一侧。

    按宿主 path **显示序**（芯片在左 → ANT 在右）：
    - trunk 出现在结构支路件之前 → ``left``（靠芯片）
    - trunk 出现在结构件之后 → ``right``（靠 ANT）
    - 无 path 信息时默认 ``left``（合并后匹配在夹心芯片侧；不在宿主 path
      的 trunk_after 也须接到夹心左，不得默认甩到天线侧）
    """
    if plan is None or not trunk_refs:
        return "left"
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    row = id_to_row.get(host_id)
    if row is None or not row.path:
        return "left"
    seq = [str(x) for x in display_ref_seq(row.path) if x]
    if not seq:
        return "left"
    pos = {ref: i for i, ref in enumerate(seq)}
    t_idxs = [pos[t] for t in trunk_refs if t in pos]
    if not t_idxs:
        return "left"
    t0 = min(t_idxs)
    s_idxs = [
        pos[s]
        for s in structure_refs
        if s in pos and s not in trunk_refs
    ]
    if not s_idxs:
        return "left"
    return "left" if t0 < min(s_idxs) else "right"


def _place_left_of_band(
    host_cells: dict[int, PlacedCell],
    pl: PlacedCell,
    band_lo: int,
) -> bool:
    """把 pl 放到夹心带左侧最近空列（**禁止覆盖**其它器件）。

    返回是否落下。落下失败不得删 donor。
    """
    dest = max(2, int(band_lo) - 1)
    ref = str(pl.ref or "").strip()
    while dest >= 2:
        occ = host_cells.get(dest)
        if occ is None or str(occ.ref or "").strip() in ("", ref):
            host_cells[dest] = pl
            return True
        dest -= 1
    occ2 = host_cells.get(2)
    if occ2 is None or str(occ2.ref or "").strip() in ("", ref):
        host_cells[2] = pl
        return True
    return False


def _hug_ref_left_of_band(
    host_cells: dict[int, PlacedCell],
    ref: str,
    band_lo: int,
) -> None:
    """已在夹心左、但没贴缘的 RLCT extras 右挪到 sandwich-1。

    须：合并后匹配贴夹心芯片侧缘。
    可：夹心-1 被 FIL hub 占着 → 先把 hub 再左移一格。
    禁：覆盖其它非 hub 器件；不把 extras 推进夹心馅。
    """
    rs = str(ref or "").strip()
    if not rs:
        return
    dest = max(2, int(band_lo) - 1)
    cur: int | None = None
    pl: Optional[PlacedCell] = None
    for c, cell in list(host_cells.items()):
        if cell is not None and str(cell.ref or "").strip() == rs:
            cur = int(c)
            pl = cell
            break
    if cur is None or pl is None or cur >= dest:
        return
    occ = host_cells.get(dest)
    occ_ref = str(getattr(occ, "ref", "") or "").strip() if occ else ""
    if occ is not None and occ_ref and occ_ref != rs:
        if _is_chip_trunk_hub_ref(occ_ref):
            _place_left_of_band(host_cells, occ, dest)
            occ = host_cells.get(dest)
            occ_ref = str(getattr(occ, "ref", "") or "").strip() if occ else ""
        if occ is not None and occ_ref and occ_ref != rs:
            return
    if cur in host_cells:
        del host_cells[cur]
    host_cells[dest] = pl


def _restore_host_path_devices(
    plan: SheetPlan,
    grid: SheetGrid,
    host_id: str,
    structure_refs: frozenset[str],
    *,
    band_hi: int,
) -> None:
    """夹心后：宿主 path 上仍应在主链的器件若缺失，从其它主链行按同列补回。

    只补非结构件；不删 donor（共享 FIL 等须多行共存才能竖并）。
    """
    if host_id not in (grid.row_ids or []):
        return
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    row = id_to_row.get(host_id)
    if row is None or not row.path:
        return
    host_cells = dict(grid.cells.get(host_id) or {})
    present = {
        str(pl.ref or "").strip()
        for pl in host_cells.values()
        if pl is not None and str(pl.ref or "").strip()
    }
    changed = False
    seq = [str(x) for x in display_ref_seq(row.path) if x]
    cut = (getattr(grid, "copad_outside_hub_cut", None) or {}).get(host_id)
    skip_after: set[str] = set()
    if cut and cut in seq:
        skip_after = set(seq[seq.index(cut) + 1 :])
    for ref in seq:
        if not ref or ref in structure_refs or ref in present or ref in skip_after:
            continue
        donor_pl: Optional[PlacedCell] = None
        donor_col: Optional[int] = None
        for rid, cells in (grid.cells or {}).items():
            if rid == host_id or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                continue
            for c, pl in (cells or {}).items():
                if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                    continue
                if str(pl.ref or "").strip() == ref:
                    donor_pl, donor_col = pl, int(c)
                    break
            if donor_pl is not None:
                break
        if donor_pl is None or donor_col is None:
            continue
        dest = int(donor_col)
        occ = host_cells.get(dest)
        if occ is not None and str(occ.ref or "").strip() not in ("", ref):
            dest = max(max(host_cells.keys(), default=band_hi), band_hi) + 1
        host_cells[dest] = PlacedCell(
            ref=ref,
            device=donor_pl.device,
            kind=donor_pl.kind or "device",
        )
        present.add(ref)
        changed = True
    if changed:
        grid.cells[host_id] = host_cells
        if host_cells:
            grid.max_col = max(grid.max_col, max(host_cells.keys()))
        _unify_device_columns(grid)


def _is_chip_trunk_hub_ref(ref: str) -> bool:
    ru = str(ref or "").strip().upper()
    return bool(ru) and ru.startswith(
        ("FIL", "LPF", "BPF", "DIP", "DUP", "DPX", "TPX", "U", "PA", "SW")
    )


def _is_rehomeable_trunk_before_ref(ref: str) -> bool:
    """trunk_before 中可迁宿主的被动挂件（RLCT）；FIL/U* 共享干路不迁。"""
    ru = str(ref or "").strip().upper()
    if not ru:
        return False
    if ru.startswith(
        ("FIL", "LPF", "BPF", "DIP", "DUP", "DPX", "TPX", "U", "PA", "SW", "ESW", "IC")
    ):
        return False
    return ru[0] in "CLRT"


def _emit_pad_extra_cell(ref: str, plan: Optional[SheetPlan]) -> PlacedCell:
    """焊盘并联 extras：浅蓝被动格。禁止复制 FIL/U/DIP 字。"""
    src = device_from_plan(plan, ref) if plan is not None else None
    d = Device(ref=ref, part_type=str(getattr(src, "part_type", "") or ""))
    d.is_copad = False
    d.is_passive = True
    d.is_shunt = True
    return PlacedCell(ref=ref, device=d, kind="device")


def _ensure_rehomeable_trunk_present(
    grid: SheetGrid,
    host_id: str,
    trunk_refs: frozenset[str],
    *,
    band_lo: int,
    band_hi: int,
    plan: Optional[SheetPlan] = None,
    structure_refs: frozenset[str] = frozenset(),
) -> None:
    """夹心该侧必须出现可 rehome 的焊盘并联。格子上还没有就长出来。

    须：trunk_before/after 里的被动 RLCT 落在夹心 ANT 侧或芯片侧宿主行。
    可：已经在宿主该侧 → 不动。
    禁：不复制 hub IC 字（unique 全表一字）；不写进夹心中层馅。
    """
    if not trunk_refs or host_id not in (grid.row_ids or []):
        return
    host_cells = dict(grid.cells.get(host_id) or {})
    present = {
        str(pl.ref or "").strip()
        for pl in host_cells.values()
        if pl is not None and str(pl.ref or "").strip()
    }
    changed = False
    extras: list[str] = []
    hubs: list[str] = []
    for ref in trunk_refs:
        rs = str(ref or "").strip()
        if not rs:
            continue
        if _is_chip_trunk_hub_ref(rs):
            hubs.append(rs)
        elif _is_rehomeable_trunk_before_ref(rs):
            extras.append(rs)
    for rs in extras + hubs:
        if rs in present:
            if (
                _is_rehomeable_trunk_before_ref(rs)
                and _trunk_after_band_side(
                    plan, host_id, frozenset({rs}), structure_refs,
                ) == "left"
            ):
                before_col = next(
                    (
                        int(c)
                        for c, cell in host_cells.items()
                        if cell is not None and str(cell.ref or "").strip() == rs
                    ),
                    None,
                )
                _hug_ref_left_of_band(host_cells, rs, band_lo)
                after_col = next(
                    (
                        int(c)
                        for c, cell in host_cells.items()
                        if cell is not None and str(cell.ref or "").strip() == rs
                    ),
                    None,
                )
                if before_col != after_col:
                    changed = True
            continue
        pl: Optional[PlacedCell] = None
        for rid, cells in (grid.cells or {}).items():
            if rid == host_id or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                continue
            for src in (cells or {}).values():
                if (
                    src is not None
                    and src.kind in VISIBLE_DEVICE_KINDS
                    and str(src.ref or "").strip() == rs
                ):
                    pl = PlacedCell(
                        ref=rs,
                        device=src.device or _emit_pad_extra_cell(rs, plan).device,
                        kind="device",
                    )
                    break
            if pl is not None:
                break
        if pl is None:
            pl = _emit_pad_extra_cell(rs, plan)
        side = _trunk_after_band_side(
            plan, host_id, frozenset({rs}), structure_refs,
        )
        placed = False
        if side == "left":
            dest = max(2, int(band_lo) - 1)
            occ = host_cells.get(dest)
            occ_ref = str(getattr(occ, "ref", "") or "").strip() if occ else ""
            # extras 贴夹心左缘：若 FIL 占着 sandwich-1，先把 FIL 再左移一格
            if (
                occ is not None
                and occ_ref
                and occ_ref != rs
                and _is_chip_trunk_hub_ref(occ_ref)
                and _is_rehomeable_trunk_before_ref(rs)
            ):
                _place_left_of_band(host_cells, occ, dest)
            placed = _place_left_of_band(host_cells, pl, max(2, int(band_lo)))
        else:
            dest = max(2, int(band_hi) + 1)
            while True:
                occ = host_cells.get(dest)
                if occ is None or str(occ.ref or "").strip() in ("", rs):
                    host_cells[dest] = pl
                    placed = True
                    break
                dest += 1
                if dest > int(band_hi) + 40:
                    break
        if not placed:
            continue
        present.add(rs)
        changed = True
    if not changed:
        return
    grid.cells[host_id] = host_cells
    if host_cells:
        grid.max_col = max(grid.max_col, max(host_cells.keys()))
    _unify_device_columns(grid)


def _host_path_refs(plan: Optional[SheetPlan], host_id: str) -> set[str]:
    if plan is None:
        return set()
    return _row_path_refs(plan, host_id)


def _owner_rows_for_chip_trunk(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    host_id: str,
    trunk_refs: frozenset[str],
) -> list[str]:
    """merge.trunk_ref / trunk_after 滤波所在主链行。不含夹心宿主。

    须：合并后干路落在这些行，不得插进宿主通往夹心 hub 的匹配段。
    """
    if plan is None:
        return []
    hubs = {r for r in trunk_refs if _is_chip_trunk_hub_ref(r)}
    if not hubs:
        return []
    out: list[str] = []
    for rid in grid.row_ids or []:
        if rid == host_id or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        have = _row_path_refs(plan, rid) | {
            str(pl.ref or "").strip()
            for pl in (grid.cells.get(rid) or {}).values()
            if pl is not None
            and pl.kind in VISIBLE_DEVICE_KINDS
            and str(pl.ref or "").strip()
        }
        if hubs & have:
            out.append(rid)
    return out


def _clear_refs_from_row(grid: SheetGrid, rid: str, refs: set[str]) -> None:
    if not refs or rid not in (grid.row_ids or []):
        return
    cells = dict(grid.cells.get(rid) or {})
    changed = False
    for c, pl in list(cells.items()):
        if pl is not None and str(pl.ref or "").strip() in refs:
            del cells[c]
            changed = True
    if changed:
        grid.cells[rid] = cells


def _merge_branch_home_rows(
    plan: SheetPlan,
    grid: SheetGrid,
    structures: list,
    host_id: str,
) -> list[str]:
    """merge.trunk_ref 所在主链（不含贯通宿主）。

    须：黄腿画在这些已有支路行上。
    可：不足两行 → 仍在宿主上下插空行。
    禁：支路行已在时再插空黄行，把短臂塞进贯通宿主匹配走廊。
    """
    homes: list[str] = []
    seen: set[str] = set()
    for st in structures or []:
        if _host_row_id(plan, grid, st) != host_id:
            continue
        hubs = _structure_chip_trunk_hubs(st)
        if not hubs:
            continue
        for rid in _owner_rows_for_chip_trunk(
            plan, grid, host_id, frozenset(hubs),
        ):
            if not rid or rid in seen:
                continue
            seen.add(rid)
            homes.append(rid)
    return homes


def _rehome_hub_owner_below_yellow(
    plan: SheetPlan,
    grid: SheetGrid,
    host_id: str,
    upper_id: str,
    lower_id: str,
    map_up: dict[int, PlacedCell],
    map_lo: dict[int, PlacedCell],
    h_idx: int,
    structures: list | None = None,
) -> str:
    """path 含黄腿结构 IC、但不是 merge.trunk 贯通宿主的主链不能当馅中层。

    须：merge.trunk 贯通宿主（path 含 merge.trunk）留汉堡中层。
    可：另有 merge.trunk owner，且这条主链不含 merge.trunk、只走过黄腿 IC
        → 抽到该 IC 黄腿外侧（默认下侧），中层改留空壳，再并柱。
    禁：把贯通宿主当成外侧链抽走（汉堡拆开、本行 hop、并柱穿越黄腿）。
        禁把 IC 塞进馅来凑并。
    """
    host_path = _row_path_refs(plan, host_id)

    def _hubs(m: dict[int, PlacedCell]) -> bool:
        for pl in (m or {}).values():
            if pl is None:
                continue
            rs = str(pl.ref or "").strip()
            if _is_chip_trunk_hub_ref(rs) and rs in host_path:
                return True
        return False

    yellow = None
    yellow_hubs: set[str] = set()
    for pl in list((map_lo or {}).values()) + list((map_up or {}).values()):
        if pl is None:
            continue
        rs = str(pl.ref or "").strip()
        if _is_chip_trunk_hub_ref(rs) and rs in host_path:
            yellow_hubs.add(rs)
    if _hubs(map_lo):
        yellow = lower_id
    elif _hubs(map_up):
        yellow = upper_id
    merge_trunks: set[str] = set()
    for st in structures or []:
        if _host_row_id(plan, grid, st) != host_id:
            continue
        t = str(getattr(getattr(st, "merge", None), "trunk_ref", "") or "").strip()
        if t.upper().startswith(("FIL", "LPF", "BPF", "DUP", "DPX", "TPX")):
            merge_trunks.add(t)
    # 只抽「黄腿 IC ≠ merge.trunk」且本身不是 merge.trunk owner 的外侧链。
    # 黄腿 IC 就是 merge.trunk → 不抽。
    # 贯通宿主 path 含 merge.trunk（即使同时走过黄腿开关）→ 不抽。
    if not yellow_hubs.difference(merge_trunks):
        return host_id
    if merge_trunks & host_path:
        return host_id
    if not yellow or yellow not in (grid.row_ids or []):
        return host_id
    ids = list(grid.row_ids or [])
    if host_id not in ids or upper_id not in ids or lower_id not in ids:
        return host_id
    mid_id = f"{plan.key}|COPADBLK{h_idx}|mid"
    hi = ids.index(host_id)
    ids[hi] = mid_id
    grid.labels[mid_id] = ""
    grid.cells[mid_id] = {}
    if host_id in ids:
        ids = [r for r in ids if r != host_id]
    yi = ids.index(yellow)
    ids = ids[: yi + 1] + [host_id] + ids[yi + 1 :]
    old = list(grid.row_ids)
    grid.row_ids = ids
    _rebind_merges_after_row_shuffle(grid, old)
    _split_outside_hub_owner_suffix(
        plan, grid, host_id, mid_id, yellow_hubs,
    )
    return mid_id


def _split_outside_hub_owner_suffix(
    plan: SheetPlan,
    grid: SheetGrid,
    host_id: str,
    mid_id: str,
    yellow_hubs: set[str],
) -> None:
    """抽到黄腿下的链只留 hub 前匹配；hub 之后的干路留夹心中层天线侧。

    禁：整行 path 跟过去，在并柱右侧画出假平行公干。
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    row = id_to_row.get(host_id)
    if row is None or row.path is None:
        return
    seq = [str(x) for x in display_ref_seq(row.path) if x]
    hub_i = next((i for i, r in enumerate(seq) if r in yellow_hubs), None)
    if hub_i is None:
        return
    hub_ref = seq[hub_i]
    right = seq[hub_i + 1 :]
    if not right:
        grid.copad_outside_hub_cut[host_id] = hub_ref
        return
    host_cells = dict(grid.cells.get(host_id) or {})
    mid_cells = dict(grid.cells.get(mid_id) or {})
    bands = sandwich_reserved_bands(grid, mid_id)
    band_hi = max((int(hi) for _lo, hi in bands), default=2)
    dest = band_hi + 1
    for ref in right:
        pl = None
        for c, cell in list(host_cells.items()):
            if cell is not None and str(cell.ref or "").strip() == ref:
                pl = cell
                del host_cells[c]
                break
        if pl is None:
            continue
        while dest in mid_cells and mid_cells.get(dest) is not None:
            dest += 1
        mid_cells[dest] = pl
        dest += 1
    grid.cells[host_id] = host_cells
    grid.cells[mid_id] = mid_cells
    if mid_cells:
        grid.max_col = max(grid.max_col, max(mid_cells.keys()))
    grid.copad_outside_hub_cut[host_id] = hub_ref


def _place_row_block(grid: SheetGrid, block: list[str]) -> None:
    """把 block 按给定顺序收成连续段，插在原先最早成员的位置。"""
    ids = list(grid.row_ids or [])
    block = [r for r in block if r in ids]
    if len(block) < 2:
        return
    bset = set(block)
    old_first = min(ids.index(r) for r in block)
    insert_at = sum(1 for r in ids[:old_first] if r not in bset)
    rest = [r for r in ids if r not in bset]
    new_ids = rest[:insert_at] + block + rest[insert_at:]
    if new_ids == ids:
        return
    grid.row_ids = new_ids
    _rebind_merges_after_row_shuffle(grid, ids)


def _overlay_branch_cells(
    cells: dict[int, PlacedCell],
    overlay: dict[int, PlacedCell],
) -> dict[int, PlacedCell]:
    """黄腿覆到已有主链：不砸私有匹配字。"""
    out = dict(cells)
    for c, pl in (overlay or {}).items():
        occ = out.get(int(c))
        occ_ref = str(getattr(occ, "ref", "") or "").strip() if occ else ""
        if (
            occ is not None
            and occ_ref
            and occ.kind in VISIBLE_DEVICE_KINDS
            and occ_ref != str(getattr(pl, "ref", "") or "").strip()
        ):
            continue
        out[int(c)] = pl
    return out


def _rehome_host_trunk_to_mid(
    grid: SheetGrid,
    host_id: str,
    trunk_refs: frozenset[str],
    *,
    band_lo: int,
    band_hi: int,
    plan: Optional[SheetPlan] = None,
    structure_refs: frozenset[str] = frozenset(),
    extras_row: str | None = None,
) -> None:
    """夹心装配后：干路件迁回主链。

    宿主 path 上的干仍归宿主。
    宿主 path 没有的 merge 后 RLCT：夹心三连已插入空中层时落中间行芯片侧；
    滤波 hub 仍归 FIL owner。禁止插进宿主通往夹心 hub 的匹配段。
    """
    if not trunk_refs or host_id not in (grid.row_ids or []):
        return
    host_seq = _host_path_refs(plan, host_id)
    foreign = {r for r in trunk_refs if r not in host_seq}
    native = {r for r in trunk_refs if r in host_seq}
    extras_row = extras_row or host_id
    use_mid = (
        extras_row != host_id
        and extras_row in (grid.row_ids or ())
        and "COPADBLK" in str(extras_row)
    )
    if use_mid and foreign:
        rlct = {r for r in foreign if _is_rehomeable_trunk_before_ref(r)}
        hubs = foreign - rlct
        if rlct:
            _rehome_host_trunk_one_side(
                grid, extras_row, frozenset(rlct),
                band_lo=band_lo, band_hi=band_hi, side="left",
            )
            _ensure_rehomeable_trunk_present(
                grid, extras_row, frozenset(rlct),
                band_lo=band_lo, band_hi=band_hi, plan=plan,
                structure_refs=structure_refs,
            )
            _clear_refs_from_row(grid, host_id, rlct)
            for rid in list(grid.row_ids or []):
                if rid in (extras_row,) or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                    continue
                _clear_refs_from_row(grid, rid, rlct)
        foreign = hubs
        trunk_refs = frozenset(native | hubs)
        if not trunk_refs:
            return
    owners = _owner_rows_for_chip_trunk(plan, grid, host_id, frozenset(foreign))
    if owners and foreign:
        for rid in owners:
            _rehome_host_trunk_one_side(
                grid, rid, frozenset(foreign),
                band_lo=band_lo, band_hi=band_hi, side="left",
            )
            _ensure_rehomeable_trunk_present(
                grid, rid, frozenset(foreign),
                band_lo=band_lo, band_hi=band_hi, plan=plan,
                structure_refs=structure_refs,
            )
        _clear_refs_from_row(grid, host_id, foreign)
        trunk_refs = frozenset(native)
        if not trunk_refs:
            return
    # 按 path 侧别拆组，避免左右干混判
    left_refs: set[str] = set()
    right_refs: set[str] = set()
    for ref in trunk_refs:
        rs = str(ref or "").strip()
        if not rs:
            continue
        side = _trunk_after_band_side(
            plan, host_id, frozenset({rs}), structure_refs,
        )
        if side == "left":
            left_refs.add(rs)
        else:
            right_refs.add(rs)
    if left_refs:
        _rehome_host_trunk_one_side(
            grid, host_id, frozenset(left_refs),
            band_lo=band_lo, band_hi=band_hi, side="left",
        )
    if right_refs:
        _rehome_host_trunk_one_side(
            grid, host_id, frozenset(right_refs),
            band_lo=band_lo, band_hi=band_hi, side="right",
        )
    # 焊盘并联 extras：格子上还没有也要落夹心该侧（不复制 U/FIL hub 字）
    _ensure_rehomeable_trunk_present(
        grid, host_id, trunk_refs,
        band_lo=band_lo, band_hi=band_hi, plan=plan,
        structure_refs=structure_refs,
    )
    # 被动 trunk_before 中「不可竖并」的 RLCT：宿主独占。
    # path 含的干路件若还留在汉堡行，unique 会优先夹心把宿主格抽掉。
    # 可竖并件（colay 干路等 is_non_merge=False）禁止抽——须多行并柱。
    # 汉堡对腿（copad_leg）仍留黄腿。
    host_cells = grid.cells.get(host_id) or {}
    host_present = {
        str(pl.ref or "").strip()
        for pl in host_cells.values()
        if pl is not None and str(pl.ref or "").strip()
    }
    for ref in trunk_refs:
        rs = str(ref or "").strip()
        if not rs or not _is_rehomeable_trunk_before_ref(rs):
            continue
        if rs not in host_present:
            continue
        dev = device_from_plan(plan, rs) if plan is not None else None
        # 可进 L6 竖并 → 保留多行
        if not is_non_merge_align_ref(rs, dev):
            continue
        for rid, cells in list((grid.cells or {}).items()):
            if rid == host_id or "|__gap__" in str(rid):
                continue
            for c, pl in list((cells or {}).items()):
                if pl is None or str(pl.ref or "").strip() != rs:
                    continue
                if "COPADBLK" in str(rid) and getattr(pl, "kind", "") == "copad_leg":
                    continue
                del cells[c]


def _rehome_host_trunk_one_side(
    grid: SheetGrid,
    host_id: str,
    trunk_refs: frozenset[str],
    *,
    band_lo: int,
    band_hi: int,
    side: str,
) -> None:
    """单侧干路 rehome（left|right）。"""
    if not trunk_refs or host_id not in (grid.row_ids or []):
        return
    band_lo = max(2, int(band_lo))
    band_hi = max(band_lo, int(band_hi))
    multi = {
        ref
        for ref, n in _main_row_ref_counts(grid).items()
        if n >= 2
    }

    if side == "left":
        # 只保证 trunk 在宿主且列 < band_lo；不搬右干段、不删 donor 共享件
        host_cells = dict(grid.cells.get(host_id) or {})
        for c, pl in list(host_cells.items()):
            if pl is None or pl.ref not in trunk_refs:
                continue
            if int(c) < band_lo:
                continue  # 已在夹心左侧，保持
            # 带内或右侧 → 挪回左侧
            del host_cells[c]
            _place_left_of_band(host_cells, pl, band_lo)
        present = {
            str(pl.ref or "").strip()
            for pl in host_cells.values()
            if pl is not None and str(pl.ref or "").strip()
        }
        for ref in trunk_refs:
            if ref in present:
                continue
            donor_pl: Optional[PlacedCell] = None
            for rid, cells in (grid.cells or {}).items():
                if rid == host_id or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                    continue
                for pl in (cells or {}).values():
                    if pl and pl.kind in VISIBLE_DEVICE_KINDS and pl.ref == ref:
                        donor_pl = pl
                        break
                if donor_pl is not None:
                    break
            if donor_pl is None:
                continue
            if not _place_left_of_band(host_cells, donor_pl, band_lo):
                continue
            present.add(ref)
            # 独占才从 donor 删
            if ref not in multi:
                for rid, cells in list((grid.cells or {}).items()):
                    if rid == host_id or "COPADBLK" in str(rid):
                        continue
                    for cc, pl in list((cells or {}).items()):
                        if pl and pl.ref == ref:
                            del cells[cc]
        grid.cells[host_id] = host_cells
        if host_cells:
            grid.max_col = max(grid.max_col, max(host_cells.keys()))
        _unify_device_columns(grid)
        _nudge_host_trunk_outside_band(
            grid, host_id, trunk_refs, band_lo=band_lo, band_hi=band_hi, side="left",
        )
        return

    # side == right：原右干归位
    donor: Optional[str] = None
    donor_col: Optional[int] = None
    for rid in grid.row_ids:
        if rid == host_id or "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        for c, pl in (grid.cells.get(rid) or {}).items():
            if pl.kind in VISIBLE_DEVICE_KINDS and pl.ref in trunk_refs:
                if donor is None or int(c) < int(donor_col or 10**9):
                    donor, donor_col = rid, int(c)
                break
    if donor is None or donor_col is None:
        # 已在宿主：保证列在夹心带右侧（仅 right 侧）
        _nudge_host_trunk_outside_band(
            grid, host_id, trunk_refs, band_lo=band_lo, band_hi=band_hi, side="right",
        )
        return

    donor_cells = dict(grid.cells.get(donor) or {})
    # 从 trunk 最小列起的右干候选
    move_cols = sorted(
        c
        for c, pl in donor_cells.items()
        if int(c) >= donor_col and pl.kind in ("device", "copad_leg", "gray")
    )
    if not move_cols:
        return
    host_cells = dict(grid.cells.get(host_id) or {})
    src0 = min(move_cols)
    # 若某迁入件已在其它行同列（如共享 FIL），对齐到该列，保证竖并同列
    dest0 = max(band_hi + 1, 2)
    for c in move_cols:
        pl = donor_cells.get(c)
        if pl is None or not pl.ref:
            continue
        for rid, cells in (grid.cells or {}).items():
            if rid == donor:
                continue
            for oc, opl in (cells or {}).items():
                if opl.ref == pl.ref and opl.kind in VISIBLE_DEVICE_KINDS:
                    aligned = int(oc) - (int(c) - src0)
                    dest0 = max(band_hi + 1, aligned)
                    break
    # 仅独占 trunk 段才清宿主目标带；共享件只补格
    exclusive_cols = [
        c
        for c in move_cols
        if (donor_cells.get(c) is None)
        or str(getattr(donor_cells.get(c), "ref", "") or "") not in multi
    ]
    if exclusive_cols:
        span = max(exclusive_cols) - src0
        for c in list(host_cells.keys()):
            if dest0 <= int(c) <= dest0 + span and int(c) > 1:
                # 不覆盖宿主上已有的共享件
                plh = host_cells.get(c)
                if plh and str(plh.ref or "") in multi:
                    continue
                del host_cells[c]
    for c in move_cols:
        pl = donor_cells.get(c)
        if pl is None:
            continue
        ref = str(pl.ref or "").strip()
        dest = dest0 + (int(c) - src0)
        # 宿主已有同 ref 则跳过
        if any(
            str(getattr(hp, "ref", "") or "") == ref
            for hp in host_cells.values()
        ):
            if ref not in multi:
                del donor_cells[c]
            continue
        host_cells[dest] = pl
        # 跨行共享件保留 donor，避免拆掉其它 path 的链路与竖并
        if ref not in multi:
            del donor_cells[c]
    grid.cells[host_id] = host_cells
    grid.cells[donor] = donor_cells
    if host_cells:
        grid.max_col = max(grid.max_col, max(host_cells.keys()))
    _unify_device_columns(grid)
    # 宿主上已有干路件若仍落在夹心带内，挤到带外
    _nudge_host_trunk_outside_band(
        grid, host_id, trunk_refs, band_lo=band_lo, band_hi=band_hi, side=side,
    )


def _nudge_host_trunk_outside_band(
    grid: SheetGrid,
    host_id: str,
    trunk_refs: frozenset[str],
    *,
    band_lo: int,
    band_hi: int,
    side: str,
) -> None:
    """保证宿主干路件列在夹心带外（left: <band_lo；right: >band_hi）。"""
    host_cells = dict(grid.cells.get(host_id) or {})
    if not host_cells:
        return
    changed = False
    for c, pl in list(host_cells.items()):
        if pl is None or str(pl.ref or "").strip() not in trunk_refs:
            continue
        col = int(c)
        if side == "left":
            if col < band_lo:
                continue
            del host_cells[c]
            _place_left_of_band(host_cells, pl, band_lo)
            changed = True
        else:
            if col > band_hi:
                continue
            del host_cells[c]
            dest = band_hi + 1
            while True:
                occ = host_cells.get(dest)
                if occ is None or str(occ.ref or "").strip() in (
                    "", str(pl.ref or "").strip(),
                ):
                    host_cells[dest] = pl
                    break
                dest += 1
            changed = True
    if not changed:
        return
    grid.cells[host_id] = host_cells
    if host_cells:
        grid.max_col = max(grid.max_col, max(host_cells.keys()))
    _unify_device_columns(grid)


def _host_branch_row_ids(grid: SheetGrid, host_id: str) -> tuple[str, str] | None:
    """宿主的上黄 / 下黄行。插空行时为紧邻 b1/b2；复用 merge 支路行时为登记对。"""
    mapped = (getattr(grid, "copad_branch_rows_by_host", None) or {}).get(host_id)
    ids = list(grid.row_ids or [])
    if mapped and mapped[0] in ids and mapped[1] in ids:
        return mapped[0], mapped[1]
    if host_id not in ids:
        return None
    i = ids.index(host_id)
    if i <= 0 or i >= len(ids) - 1:
        return None
    up, lo = str(ids[i - 1]), str(ids[i + 1])
    if up.endswith("|b1") and lo.endswith("|b2"):
        return up, lo
    return None


def sandwich_reserved_bands(
    grid: SheetGrid,
    host_id: str,
    seq: list[str] | None = None,
) -> list[tuple[int, int]]:
    """这一宿主上的夹心带：[lo, hi] 闭区间，含块内空洞。

    须：每块结构的黄腿列（含腿之间空列）是馅，宿主干路不得塞进去。
    可：两块之间隔了宿主干路 → 两条带，中段干路落段间。
    禁：不得用列间距猜同块还是两块。

    一块 = 宿主 path 上连续的支路腿（中间没有本行可见的宿主干路件）。
    黄行上多出来的同块件（对侧腿、hub）归到其左侧那块。
    path 里认不出块时：这一对黄行 min..max 算一条带。
    """
    pair = _host_branch_row_ids(grid, host_id)
    if pair is None:
        return []
    ref_cols: dict[str, list[int]] = {}
    all_cols: list[int] = []
    for br in pair:
        is_inserted = "COPADBLK" in str(br)
        for c, pl in (grid.cells.get(br) or {}).items():
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            ref = str(pl.ref or "").strip()
            if not ref or int(c) < 2:
                continue
            # 插入空黄行：整行结构件都是馅。
            # 黄腿覆在已有主链上：只有黄格是馅，行上原有主链字不是。
            if not is_inserted and pl.kind != "copad_leg":
                continue
            ref_cols.setdefault(ref, []).append(int(c))
            all_cols.append(int(c))
    if not all_cols:
        return []
    yellow = set(ref_cols)
    struct = getattr(grid, "copad_structure_refs", None) or set()
    host_only: set[str] = set()
    for c, pl in (grid.cells.get(host_id) or {}).items():
        if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
            continue
        ref = str(pl.ref or "").strip()
        # 干路（L1801/L1814）切开多块夹心。hub/对腿在 structure_refs 里，
        # 不得当 host_only，否则一块「分开+合并」被枢纽拆成两带。
        if ref and int(c) >= 2 and ref not in yellow and ref not in struct:
            host_only.add(ref)

    groups: list[list[int]] = []
    if seq:
        i = 0
        n = len(seq)
        while i < n:
            if seq[i] not in yellow:
                i += 1
                continue
            cols: list[int] = []
            while i < n and seq[i] not in host_only:
                cols.extend(ref_cols.get(seq[i], []))
                i += 1
            if cols:
                groups.append(cols)
    if not groups:
        return [(min(all_cols), max(all_cols))]

    seeds = [min(g) for g in groups]
    buckets: list[list[int]] = [list(g) for g in groups]
    for c in all_cols:
        idx = 0
        for j, lo in enumerate(seeds):
            if c >= lo:
                idx = j
        buckets[idx].append(c)
    bands = [(min(b), max(b)) for b in buckets if b]
    bands.sort()
    return bands


def sandwich_reserved_cols(
    grid: SheetGrid,
    plan: SheetPlan | None = None,
) -> set[int]:
    """全表夹心带列（各宿主各块 union）。供回流 / 纠序跳过中层馅。"""
    cols: set[int] = set()
    seq_by_host: dict[str, list[str]] = {}
    if plan is not None:
        for row in plan.rows or []:
            if row is None or not getattr(row, "path", None):
                continue
            seq_by_host[str(row.row_id)] = [
                str(x) for x in display_ref_seq(row.path) if x
            ]
    ids = list(grid.row_ids or [])
    seen: set[str] = set()
    for i, rid in enumerate(ids):
        if i <= 0 or i >= len(ids) - 1:
            continue
        if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
            continue
        if not (
            str(ids[i - 1]).endswith("|b1") and str(ids[i + 1]).endswith("|b2")
        ):
            continue
        if rid in seen:
            continue
        seen.add(rid)
        for lo, hi in sandwich_reserved_bands(
            grid, rid, seq_by_host.get(rid),
        ):
            cols.update(range(int(lo), int(hi) + 1))
    if cols:
        return cols
    # 没有 b1/宿主/b2 三连时：按 COPAD 块 min..max（单块）
    by_prefix: dict[str, list[int]] = {}
    for rid, cells in (grid.cells or {}).items():
        if "COPADBLK" not in str(rid):
            continue
        prefix = _copad_block_prefix(rid)
        for c, pl in (cells or {}).items():
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            if str(pl.ref or "").strip() and int(c) >= 2:
                by_prefix.setdefault(prefix, []).append(int(c))
    for clist in by_prefix.values():
        if clist:
            cols.update(range(min(clist), max(clist) + 1))
    return cols


def _sandwich_reserved_cols(
    grid: SheetGrid,
    plan: SheetPlan | None = None,
) -> set[int]:
    return sandwich_reserved_cols(grid, plan)


def _repair_path_order_around_merges(
    plan: SheetPlan,
    grid: SheetGrid,
) -> None:
    """共享竖并列被夹心迁走后，按 path 显示序纠正各主链行左右关系。

    典型：place 时左邻在 FIL 左；夹心把 FIL 收到支路新列后左邻仍停旧列 → 落到 FIL 右侧。
    规则：merge 覆盖的主链行上，path 序在 merge 前的器件整体重排到 merge.col 左侧，
    path 序在后的排到右侧。

    硬边界：夹心支路占用列（``_sandwich_reserved_cols``）禁止被主链重排填满，
    否则双 COPAD 宿主上夹心空档会被压成连续主链。
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    reserved = sandwich_reserved_cols(grid, plan)
    changed = False
    for m in list(grid.merges or []):
        if int(m.row_end) <= int(m.row_start) or not m.ref:
            continue
        mcol = int(m.col)
        mref = str(m.ref).strip()
        if not mref:
            continue
        for ri in range(int(m.row_start), int(m.row_end) + 1):
            if not (0 <= ri < len(grid.row_ids)):
                continue
            rid = grid.row_ids[ri]
            if "COPADBLK" in str(rid) or "|__gap__" in str(rid):
                continue
            prow = id_to_row.get(rid)
            if prow is None or not prow.path:
                continue
            seq = [str(x) for x in display_ref_seq(prow.path) if x]
            cells = dict(grid.cells.get(rid) or {})
            by_ref: dict[str, tuple[int, PlacedCell]] = {}
            for c, pl in cells.items():
                ref = str(pl.ref or "").strip()
                if ref:
                    by_ref[ref] = (int(c), pl)
            # 夹心/path-owner 后：merge 件可能只在支路或合并顶有值，本行空档仍占跨度。
            # path 含 mref → 仍以 mcol 为锚纠正左右；path 不含则跳过（避免误挤并联）。
            if mref not in seq:
                continue
            mi = seq.index(mref)
            before = seq[:mi]
            after = seq[mi + 1 :]
            left_refs = [r for r in before if r in by_ref]
            right_refs = [r for r in after if r in by_ref]
            if not left_refs and not right_refs:
                continue
            # 是否已有错侧或同侧乱序（夹心保留列上的「空洞」不算乱序依据）
            bad = False
            for r in left_refs:
                if by_ref[r][0] >= mcol:
                    bad = True
                    break
            if not bad:
                for r in right_refs:
                    if by_ref[r][0] <= mcol:
                        bad = True
                        break
            if not bad and len(left_refs) >= 2:
                cols = [by_ref[r][0] for r in left_refs]
                if cols != sorted(cols):
                    bad = True
            if not bad and len(right_refs) >= 2:
                cols = [by_ref[r][0] for r in right_refs]
                if cols != sorted(cols):
                    bad = True
            if not bad:
                continue
            # 清掉要重排的
            for r in left_refs + right_refs:
                c0, _pl = by_ref[r]
                cur = cells.get(c0)
                if cur is not None and str(cur.ref or "") == r:
                    del cells[c0]
            # 左侧：path 序，最右贴 mcol-1；跳过夹心保留列
            for k, r in enumerate(reversed(left_refs)):
                dest = mcol - 1 - k
                while dest >= 2 and (
                    dest in reserved
                    or (
                        dest in cells
                        and str(getattr(cells[dest], "ref", "") or "")
                        not in ("", r)
                    )
                ):
                    dest -= 1
                if dest < 2:
                    dest = 2
                    while dest in reserved or (
                        dest in cells
                        and str(getattr(cells[dest], "ref", "") or "")
                        not in ("", r)
                    ):
                        dest += 1
                        if dest >= mcol:
                            break
                if dest >= mcol:
                    # 找不到左侧空位：放回原列（若原列仍空）
                    dest = by_ref[r][0]
                cells[dest] = by_ref[r][1]
            # 右侧：path 序，最左贴 mcol+1；跳过夹心保留列
            for k, r in enumerate(right_refs):
                dest = mcol + 1 + k
                while dest in reserved or (
                    dest in cells
                    and str(getattr(cells[dest], "ref", "") or "")
                    not in ("", r)
                ):
                    dest += 1
                cells[dest] = by_ref[r][1]
            grid.cells[rid] = cells
            if cells:
                grid.max_col = max(grid.max_col, max(cells.keys()))
            changed = True
    if changed:
        _unify_device_columns(grid)


def _rid_is_sandwich_host(grid: SheetGrid, rid: str) -> bool:
    if rid in (getattr(grid, "copad_branch_rows_by_host", None) or {}):
        return True
    ids = list(grid.row_ids or [])
    if rid not in ids:
        return False
    i = ids.index(rid)
    return (
        i > 0
        and i < len(ids) - 1
        and str(ids[i - 1]).endswith("|b1")
        and str(ids[i + 1]).endswith("|b2")
    )


def _unify_device_columns(grid: SheetGrid) -> None:
    """同 ref 多行不同列时收到最小列，保证 L6 同列竖并。

    夹心 host_trunk / 结构件：已在宿主行的那一列优先，避免 min 把
    合并后匹配拽离夹心。
    """
    from collections import defaultdict

    locs: dict[str, list[tuple[str, int, PlacedCell]]] = defaultdict(list)
    for rid, cells in (grid.cells or {}).items():
        for c, pl in (cells or {}).items():
            if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            ref = str(pl.ref or "").strip()
            if ref:
                locs[ref].append((rid, int(c), pl))
    for ref, items in locs.items():
        cols = {c for _r, c, _p in items}
        if len(cols) < 2:
            continue
        struct = getattr(grid, "copad_structure_refs", None) or set()
        host_cols = [
            c
            for rid, c, _p in items
            if rid in (grid.row_ids or ())
            and _rid_is_sandwich_host(grid, rid)
        ]
        # 夹心干路：宿主列优先（贴夹心），禁止 min 把 FIL 拽回远离夹心的孤岛。
        # 不在宿主上的 RLCT extras 取最右列（贴夹心缘），滤波 hub 仍 min 以便竖并。
        if ref in struct and host_cols:
            target = min(host_cols)
        elif ref in struct and not _is_chip_trunk_hub_ref(ref):
            target = max(cols)
        else:
            target = min(cols)
        for rid, c, pl in items:
            if c == target:
                continue
            cells = grid.cells.get(rid) or {}
            occ = cells.get(target)
            if occ is not None and str(occ.ref or "") not in ("", ref):
                continue
            if c in cells:
                del cells[c]
            cells[target] = pl
            grid.cells[rid] = cells


def _drop_merges_for_refs(grid: SheetGrid, refs: frozenset[str]) -> None:
    """丢掉结构位号上的旧 L6 合并，避免夹心中段被主链巨型竖并绑死。"""
    if not refs:
        return
    grid.merges = [
        m for m in (grid.merges or [])
        if str(m.ref or "").strip() not in refs
    ]




def _shift_grid_columns(grid: SheetGrid, after_col: int, delta: int) -> None:
    """全表：col > after_col 的格子与合并列号整体 +delta（为 sandwich 开缝）。"""
    if delta <= 0:
        return
    for rid in list(grid.row_ids):
        cells = grid.cells.get(rid) or {}
        items = sorted(cells.items(), key=lambda it: it[0], reverse=True)
        new_map: dict[int, PlacedCell] = {}
        for c, pl in items:
            new_map[c + delta if c > after_col else c] = pl
        grid.cells[rid] = new_map
    new_merges: list[MergeRange] = []
    for m in grid.merges or []:
        nc = m.col + delta if m.col > after_col else m.col
        new_merges.append(
            MergeRange(
                ref=m.ref,
                col=nc,
                row_start=m.row_start,
                row_end=m.row_end,
                row_ids=m.row_ids,
            )
        )
    grid.merges = new_merges
    grid.max_col = max(grid.max_col + delta, 1)


def _main_neighbor_row_ids(
    grid: SheetGrid, host_i: int
) -> tuple[Optional[str], Optional[str]]:
    """宿主上下最近主链行（跳过已有夹心/空行）。"""
    above = None
    for i in range(host_i - 1, -1, -1):
        rid = grid.row_ids[i]
        if is_main_content_row_id(rid):
            above = rid
            break
    below = None
    for i in range(host_i + 1, len(grid.row_ids)):
        rid = grid.row_ids[i]
        if is_main_content_row_id(rid):
            below = rid
            break
    return above, below


def _branch_affinity_to_path_row(
    plan: SheetPlan,
    neighbor_rid: Optional[str],
    branch_cells: dict[int, PlacedCell],
    *,
    mids_only: bool = False,
) -> int:
    """支路与某主链 path 的共享度：path 上出现支路器件则加分（FIL/IC 更高）。

    mids_only：只计中段/非腿（换位决策用，避免短 stub 仅含对腿把整支换飞）。
    """
    if not neighbor_rid or not branch_cells:
        return 0
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    row = id_to_row.get(neighbor_rid)
    if row is None:
        return 0
    path_refs = {
        str(getattr(d, "ref", "") or "").strip()
        for d in (row.devices or [])
        if str(getattr(d, "ref", "") or "").strip()
    }
    if not path_refs:
        return 0
    score = 0
    for pl in branch_cells.values():
        if mids_only and pl.kind == "copad_leg":
            continue
        ref = str(pl.ref or "").strip()
        if not ref or ref not in path_refs:
            continue
        score += 3 if pl.kind == "copad_leg" else 2
        ru = ref.upper()
        if ru.startswith(("FIL", "U", "LPF", "BPF")):
            score += 5  # 中段 FIL 强亲和
        elif ru.startswith(("R", "L", "C")):
            score += 1
    return score


def _side_path_affinity(
    plan: SheetPlan,
    grid: SheetGrid,
    host_i: int,
    branch_cells: dict[int, PlacedCell],
    *,
    below: bool,
) -> int:
    """宿主一侧**全部**主链 path 的亲和总和（不限最近邻）。

    例：宿主下方可能是 stub 再邻支；FIL 只在邻支，须累加到邻支而非只看 stub。
    """
    if not branch_cells:
        return 0
    total = 0
    n = len(grid.row_ids)
    if below:
        indices = range(host_i + 1, n)
    else:
        indices = range(host_i - 1, -1, -1)
    for j in indices:
        rid = grid.row_ids[j]
        if not is_main_content_row_id(rid):
            continue
        total += _branch_affinity_to_path_row(
            plan, rid, branch_cells, mids_only=True,
        )
    return total


def rebalance_copad_branches_toward_path_owners(
    plan: SheetPlan,
    grid: SheetGrid,
) -> int:
    """lift 后按**新邻域**重换上下支，使含 path-owner mid 的支路贴向 owner。

    装配时 swap 看的是 lift **前**的上下主链；上提后 owner 全在宿主一侧，
    若不重换，FIL 会停在远离 path-owner 的上支，竖并只能从远端跨宿主
    （违反原则2）或并不过去。

    返回实际交换次数。
    """
    ids = list(grid.row_ids or [])
    if not ids:
        return 0
    by_prefix: dict[str, dict[str, int]] = {}
    for i, rid in enumerate(ids):
        s = str(rid)
        if "COPADBLK" not in s:
            continue
        pref = _copad_block_prefix(rid)
        slot = by_prefix.setdefault(pref, {})
        if s.endswith("|b1"):
            slot["b1"] = i
        elif s.endswith("|b2"):
            slot["b2"] = i
    n_swap = 0
    for pref, slots in by_prefix.items():
        i1 = slots.get("b1")
        i2 = slots.get("b2")
        if i1 is None or i2 is None:
            continue
        host_id = _copad_block_host_id(grid, i1)
        if not host_id or host_id not in ids:
            continue
        host_i = ids.index(host_id)
        # 宿主须夹在 b1/b2 之间
        if not (min(i1, i2) < host_i < max(i1, i2)):
            continue
        map_up = dict(grid.cells.get(ids[i1]) or {})
        map_lo = dict(grid.cells.get(ids[i2]) or {})
        new_up, new_lo, swapped = _maybe_swap_copad_branches(
            plan, grid, host_i, map_up, map_lo,
        )
        if not swapped:
            continue
        grid.cells[ids[i1]] = new_up
        grid.cells[ids[i2]] = new_lo
        n_swap += 1
    return n_swap


def _maybe_swap_copad_branches(
    plan: SheetPlan,
    grid: SheetGrid,
    host_i: int,
    map_up: dict[int, PlacedCell],
    map_lo: dict[int, PlacedCell],
) -> tuple[dict[int, PlacedCell], dict[int, PlacedCell], bool]:
    """若换位后与上下主链（整侧）中段亲和度更高，则交换上下支。

    默认：上=br0 / 下=br1。换位条件：
      score(下支↔上侧主链) + score(上支↔下侧主链)
        > score(上支↔上侧) + score(下支↔下侧)
    例：FIL 支路与含左邻的邻支亲和更高 → 放到宿主下方。
    不计对腿-only 命中，避免 stub（仅含对腿）把 FIL 支路换到上侧。
    lift 后由 ``rebalance_copad_branches_toward_path_owners`` 再调一次。
    """
    s_default = _side_path_affinity(
        plan, grid, host_i, map_up, below=False
    ) + _side_path_affinity(plan, grid, host_i, map_lo, below=True)
    s_swap = _side_path_affinity(
        plan, grid, host_i, map_lo, below=False
    ) + _side_path_affinity(plan, grid, host_i, map_up, below=True)
    if s_swap > s_default:
        return map_lo, map_up, True
    return map_up, map_lo, False


def _copad_block_host_id(grid: SheetGrid, branch_i: int) -> Optional[str]:
    """夹心支路行对应的中层 row_id（紧挨 b1 下 / b2 上）。

    中层可能是频段主链，或把主链抽到黄腿外侧后留下的空壳。
    禁止跳过空壳去认更下面那条要并 hub 的主链（那条在夹心外）。
    """
    ids = list(grid.row_ids or [])
    rid = str(ids[branch_i])
    if rid.endswith("|b1"):
        if branch_i + 1 < len(ids) and not str(ids[branch_i + 1]).endswith("|b2"):
            return ids[branch_i + 1]
    if rid.endswith("|b2"):
        if branch_i > 0 and not str(ids[branch_i - 1]).endswith("|b1"):
            return ids[branch_i - 1]
    return None










def _copad_block_prefix(rid: str) -> str:
    """…|COPADBLKn|b1 → …|COPADBLKn（同宿主块上下支共用前缀）。"""
    s = str(rid or "")
    if s.endswith("|b1") or s.endswith("|b2"):
        return s.rsplit("|", 1)[0]
    return s








def _copad_block_band_extent(
    grid: SheetGrid,
    block_prefix: str,
) -> tuple[int, int]:
    """同 COPAD 块上下支路结构件列范围 [lo, hi]；无则 (2, 1)。"""
    cols: list[int] = []
    for rid in grid.row_ids or []:
        if "COPADBLK" not in str(rid):
            continue
        if _copad_block_prefix(rid) != block_prefix:
            continue
        for c, pl in (grid.cells.get(rid) or {}).items():
            if int(c) < 2 or pl is None:
                continue
            if pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            if str(pl.ref or "").strip():
                cols.append(int(c))
    if not cols:
        return 2, 1
    return min(cols), max(cols)


def _host_id_for_copad_block(grid: SheetGrid, block_prefix: str) -> Optional[str]:
    """由 COPAD 块前缀找到夹心宿主主链行。"""
    for i, rid in enumerate(grid.row_ids or []):
        if "COPADBLK" not in str(rid):
            continue
        if _copad_block_prefix(rid) != block_prefix:
            continue
        return _copad_block_host_id(grid, i)
    return None


def _other_row_has_ref_at(
    grid: SheetGrid,
    host_id: str,
    col: int,
    ref: str,
) -> bool:
    rs = str(ref or "").strip()
    if not rs:
        return False
    for rid, rcells in (grid.cells or {}).items():
        if rid == host_id:
            continue
        occ = (rcells or {}).get(int(col))
        if (
            occ is not None
            and occ.kind not in ("gray", "label")
            and str(occ.ref or "").strip() == rs
        ):
            return True
    return False


def _shift_host_cells_right(
    cells: dict[int, PlacedCell],
    from_col: int,
    *,
    delta: int = 1,
) -> None:
    if delta <= 0:
        return
    movers = sorted(
        (
            (int(c), pl)
            for c, pl in list(cells.items())
            if int(c) >= int(from_col)
        ),
        key=lambda t: t[0],
        reverse=True,
    )
    for c, pl in movers:
        nc = c + delta
        if c in cells:
            del cells[c]
        cells[nc] = pl


def _free_host_dest_col(
    grid: SheetGrid,
    host_id: str,
    dest: int,
) -> int:
    """让 dest 在宿主行空出来。其它行仍占该列同 ref → 全表插列。"""
    while True:
        cells = grid.cells.get(host_id) or {}
        occ = cells.get(int(dest))
        if occ is None or occ.kind in ("gray", "label"):
            return int(dest)
        ref = str(occ.ref or "").strip()
        if not ref:
            return int(dest)
        if _other_row_has_ref_at(grid, host_id, dest, ref):
            _shift_grid_columns(grid, after_col=int(dest) - 1, delta=1)
            continue
        _shift_host_cells_right(cells, dest, delta=1)
        grid.cells[host_id] = cells
        grid.max_col = max(
            int(grid.max_col or 1),
            max((int(c) for c in cells), default=int(dest)),
        )
        return int(dest)


def _clear_host_mid_of_sandwich_band(
    grid: SheetGrid,
    host_id: str,
    *,
    band_lo: int,
    band_hi: int,
) -> None:
    """夹心宿主中层 [band_lo, band_hi] 不得残留链路器件。

    规则（通用，非样例位号）：
    - 仅清**带内**残留，接到 band_hi 右侧
    - 带外右侧共享件保持原列（其它 owner 仍对齐）；不得整段左收到带缘
    - dest 被占：其它行没有同 ref → 宿主右侧右移让位；有 → 全表插列
    - 典型：右干与 R 腿同列时必须挤出夹心，不能夹在上下支中间
    """
    if not host_id or host_id not in (grid.cells or {}):
        return
    lo, hi = int(band_lo), int(band_hi)
    if hi < lo:
        return
    cells = dict(grid.cells.get(host_id) or {})
    trapped = sorted(
        (
            (int(c), pl)
            for c, pl in cells.items()
            if lo <= int(c) <= hi
            and pl is not None
            and pl.kind in VISIBLE_DEVICE_KINDS
            and str(pl.ref or "").strip()
        ),
        key=lambda t: t[0],
    )
    if not trapped:
        return
    for c, _ in trapped:
        if c in cells:
            del cells[c]
    grid.cells[host_id] = cells
    dest = hi + 1
    col_map: dict[int, int] = {}
    ref_at_old: dict[int, str] = {}
    for old_c, pl in trapped:
        dest = _free_host_dest_col(grid, host_id, dest)
        cells = dict(grid.cells.get(host_id) or {})
        cells[dest] = pl
        grid.max_col = max(int(grid.max_col or 1), dest)
        grid.cells[host_id] = cells
        col_map[old_c] = dest
        ref_at_old[old_c] = str(pl.ref or "").strip()
        dest += 1
    # 并柱列跟着被挤出的**带内**格子走——仅当旧列已无其它行仍显示该 ref。
    # unique 后公尾 FIL 只剩宿主一格：必须跟，否则 prune 拆柱。
    # 夹心前其它 owner 仍占旧列：禁止跟（否则双列，unique 抽到夹心行，
    # 钉顶行被匹配段占住 → 公尾柱消失）。
    if col_map and grid.merges:
        moved: list[MergeRange] = []
        for m in grid.merges:
            old_c = int(m.col)
            new_c = col_map.get(old_c)
            ref_m = str(m.ref or "").strip()
            if (
                new_c is not None
                and ref_m
                and ref_m == ref_at_old.get(old_c, "")
                and not _other_row_has_ref_at(grid, host_id, old_c, ref_m)
            ):
                moved.append(
                    MergeRange(
                        ref=m.ref,
                        col=new_c,
                        row_start=m.row_start,
                        row_end=m.row_end,
                        row_ids=m.row_ids,
                    )
                )
            else:
                moved.append(m)
        grid.merges = moved


def _shift_copad_block_band_right(
    grid: SheetGrid,
    block_prefix: str,
    *,
    from_col: int,
    delta: int,
) -> None:
    """夹心列带右延（禁止吞左邻，禁止宿主右干掉进夹心中层）。

    步骤：
      1) 本块支路 col≥from_col 右移 delta（FIL 等到位；左腿不动）
      2) 仅清**宿主**中层落入新夹心带的器件（接右干；其它主链不动）
    """
    if delta <= 0:
        return
    fc = int(from_col)

    # 1) 黄腿行：插入的 COPADBLK，或覆在已有 merge 支路主链上的那一对
    targets: list[str] = []
    for rid in list(grid.row_ids or []):
        if "COPADBLK" in str(rid) and _copad_block_prefix(rid) == block_prefix:
            targets.append(rid)
    if not targets:
        for _h, pair in (getattr(grid, "copad_branch_rows_by_host", None) or {}).items():
            if block_prefix in pair:
                targets.extend([p for p in pair if p in (grid.row_ids or [])])
                break
    for rid in targets:
        cells = grid.cells.get(rid) or {}
        label = cells.get(1)
        movers = sorted(
            (
                (int(c), pl)
                for c, pl in cells.items()
                if int(c) >= fc and pl is not None
            ),
            key=lambda t: t[0],
            reverse=True,
        )
        if not movers:
            continue
        for c, pl in movers:
            nc = c + delta
            if c in cells:
                del cells[c]
            cells[nc] = pl
            grid.max_col = max(grid.max_col, nc)
        if label is not None:
            cells[1] = label
        grid.cells[rid] = cells

    # 2) 宿主中层不得与夹心同列（右干不得夹在上下支腿之间）
    _lo2, hi2 = _copad_block_band_extent(grid, block_prefix)
    host = _host_id_for_copad_block(grid, block_prefix)
    if host:
        _clear_host_mid_of_sandwich_band(
            grid, host, band_lo=_lo2, band_hi=hi2,
        )


# 跨模块公开 API（夹心块几何；测试可继续 import `_` 别名）
copad_block_host_id = _copad_block_host_id
copad_block_prefix = _copad_block_prefix
shift_copad_block_band_right = _shift_copad_block_band_right
# sandwich_reserved_bands / sandwich_reserved_cols 见上方定义


def assemble_copad_blocks(
    plan: SheetPlan,
    grid: SheetGrid,
    geoms: list[CopadBlockGeom],
    *,
    structures: Optional[list[CoPadStructure]] = None,
    signal_infos=None,
) -> SheetGrid:
    """L5：三行夹心 — 上支路 / 主链空档 / 下支路。

    列策略：
      在宿主原结构列带处**开缝**插入 sandwich 列宽，**全表同步右移**右侧器件，
      不重排右干，以保持共享列与后续器件的邻接与竖合并。

    行策略：
    - **同一宿主上的多块 Co-pad 共用一对上下支行**（禁止交错插行把
      上下支拆到表顶/表中分离）
    - 每块可按整侧主链中段亲和度换位（FIL 支路贴 path-owner）
    """
    if not geoms:
        return grid

    # 登记结构件：unique 仅对这些 ref 给夹心宿主加分
    for g in geoms:
        for r in g.structure_refs or ():
            rs = str(r or "").strip()
            if rs:
                grid.copad_structure_refs.add(rs)
        for r in g.host_trunk_refs or ():
            rs = str(r or "").strip()
            if not rs:
                continue
            # GPS 干路 RLCT 不得进 structure_refs，否则 reserved 切不开
            # 「干路→分开+合并→干路」多块夹心。
            if str(getattr(plan, "key", "") or "") == "GPS" and is_rlct_ref(rs):
                continue
            grid.copad_structure_refs.add(rs)

    # 夹心前收拢：按结构分别聚宿主，禁止跨 Co-pad 粘成一块
    _gather_hosts_before_insert(
        plan, grid, list(structures or ()), geoms,
    )

    # host_id → geoms（保留原 block 顺序）
    by_host: dict[str, list[CopadBlockGeom]] = {}
    for g in geoms:
        hid = g.attach_after_row_id
        if not hid or hid not in grid.row_ids:
            continue
        by_host.setdefault(hid, []).append(g)

    # 自下而上处理宿主，避免插行打乱尚未处理的 host 下标
    host_order = sorted(
        by_host.keys(),
        key=lambda h: grid.row_ids.index(h),
        reverse=True,
    )
    n_swap = 0
    saved_bands: list[tuple[str, list[tuple[object, int, int]], str]] = []

    for h_idx, host_id in enumerate(host_order):
        host_geoms = by_host[host_id]
        if host_id not in grid.row_ids:
            continue
        host_i = grid.row_ids.index(host_id)

        homes = _merge_branch_home_rows(
            plan, grid, list(structures or ()), host_id,
        )
        # 频段主链不能当黄腿行：否则关行距、hub 并不上贯通、禁 hop 切断、
        # ANT 公尾 unique 到旁路。汉堡必须插入独立支路行；merge 支路只作 owner。
        reuse_homes = False
        upper_id = f"{plan.key}|COPADBLK{h_idx}|b1"
        lower_id = f"{plan.key}|COPADBLK{h_idx}|b2"
        map_up: dict[int, PlacedCell] = {}
        map_lo: dict[int, PlacedCell] = {}
        all_struct_refs: set[str] = set()
        all_leg_refs: set[str] = set()
        # (geom, base_col, band_hi) — 每块独立列带，禁止跨块 mega-band rehome
        geom_bands: list[tuple[object, int, int]] = []

        # 列带：从左到右按 base_col 稳定放置各块（处理中可能右移）
        sorted_geoms = sorted(
            host_geoms,
            key=lambda g: _base_col_for_structure(
                grid,
                g.structure_refs,
                prefer_legs={
                    pl.ref
                    for _br, _lc, pl in g.cells
                    if pl.kind == "copad_leg" and pl.ref
                },
            ),
        )
        for geom in sorted_geoms:
            host_cells = dict(grid.cells.get(host_id) or {})
            leg_refs = {
                pl.ref
                for _br, _lc, pl in geom.cells
                if pl.kind == "copad_leg" and pl.ref
            }
            all_leg_refs |= leg_refs
            all_struct_refs |= set(geom.structure_refs or ())
            base_col = _base_col_for_structure(
                grid, geom.structure_refs, prefer_legs=leg_refs,
            )
            # 每块夹心后留一列给干路，禁止相邻黄带合成 mega-band。
            if geom_bands:
                base_col = max(int(base_col), int(geom_bands[-1][2]) + 2)
            struct_cols = [
                c
                for c, pl in host_cells.items()
                if pl.ref in geom.structure_refs
                and pl.kind in VISIBLE_DEVICE_KINDS
            ]
            orig_hi = max(struct_cols) if struct_cols else base_col
            band_w = max(1, int(geom.max_local_col))
            need_hi = base_col + band_w - 1
            delta = need_hi - orig_hi
            if delta > 0:
                _shift_grid_columns(grid, after_col=orig_hi, delta=delta)
                # 已写入 map 的列若在 orig_hi 右侧，同步右移
                def _shift_map(m: dict[int, PlacedCell]) -> dict[int, PlacedCell]:
                    out: dict[int, PlacedCell] = {}
                    for c, pl in m.items():
                        out[c + delta if c > orig_hi else c] = pl
                    return out

                map_up = _shift_map(map_up)
                map_lo = _shift_map(map_lo)
                geom_bands = [
                    (
                        g,
                        b + delta if b > orig_hi else b,
                        e + delta if e > orig_hi else e,
                    )
                    for g, b, e in geom_bands
                ]

            # 本块局部 → 绝对列；可单独换位
            blk_up: dict[int, PlacedCell] = {}
            blk_lo: dict[int, PlacedCell] = {}
            for br, loc_c, pl in geom.cells:
                abs_c = base_col + int(loc_c) - 1
                if br == 0:
                    blk_up[abs_c] = pl
                else:
                    blk_lo[abs_c] = pl
            # 亲和度按「当前 host 行下标」（插行前）
            hi_now = grid.row_ids.index(host_id)
            blk_up, blk_lo, swapped = _maybe_swap_copad_branches(
                plan, grid, hi_now, blk_up, blk_lo
            )
            if swapped:
                n_swap += 1
            map_up.update(blk_up)
            map_lo.update(blk_lo)
            geom_bands.append((geom, base_col, base_col + band_w - 1))

        # 插行：上 / 主 / 下。merge 支路已有主链时不插空黄行。
        if not reuse_homes:
            host_i = grid.row_ids.index(host_id)
            grid.merges = _shift_merges(grid.merges, host_i + 1, 1)
            grid.row_ids.insert(host_i + 1, lower_id)
            grid.labels[lower_id] = ""

            grid.merges = _shift_merges(grid.merges, host_i, 1)
            grid.row_ids.insert(host_i, upper_id)
            grid.labels[upper_id] = ""

        _remove_structure_refs(
            grid,
            frozenset(all_struct_refs),
            host_id=host_id,
            leg_refs=all_leg_refs,
        )
        _drop_merges_for_refs(grid, frozenset(all_struct_refs))

        mid = dict(grid.cells.get(host_id) or {})
        # 夹心中层只清「已搬到支路的结构件 / 灰格」；
        # 禁止清掉紧邻的共享干路前件（trunk_before），否则宿主丢 FIL、竖并失败
        for _g, base_col, band_hi in geom_bands:
            for c in list(mid.keys()):
                if not (base_col <= c <= band_hi):
                    continue
                pl = mid.get(c)
                if pl is None:
                    del mid[c]
                    continue
                ref = str(pl.ref or "").strip()
                if pl.kind == "gray" or ref in all_struct_refs:
                    del mid[c]
        if 1 not in mid:
            lab = grid.labels.get(host_id, "")
            if lab:
                mid[1] = PlacedCell(ref=lab, kind="label")

        grid.cells[host_id] = mid
        grid.cells[upper_id] = map_up
        grid.cells[lower_id] = map_lo
        sandwich_mid = host_id
        if homes:
            grid.copad_chip_trunk_row_ids.update(homes)
        merge_only = False
        for st in structures or []:
            if _host_row_id(plan, grid, st) != host_id:
                continue
            mtr = str(getattr(getattr(st, "merge", None), "trunk_ref", "") or "").strip()
            ru = mtr.upper()
            if not ru.startswith(("FIL", "LPF", "BPF", "DUP", "DPX", "TPX")):
                continue
            if _owner_rows_for_chip_trunk(
                plan, grid, host_id, frozenset({mtr}),
            ):
                merge_only = True
                break
        if merge_only:
            sandwich_mid = _rehome_hub_owner_below_yellow(
                plan, grid, host_id, upper_id, lower_id,
                map_up, map_lo, h_idx,
                structures=list(structures or ()),
            )
        if map_up or map_lo:
            grid.max_col = max(
                grid.max_col,
                max(list(map_up.keys()) + list(map_lo.keys()), default=1),
            )
        # trunk_after：按**本块**列带独立 rehome。
        # 禁止用 min/max 跨多块 mega-band（会把两夹心之间的公共端
        # 整段拽到第一块左侧，再被 path 重排压成连续主链、夹心空档消失）。
        for geom, blo, bhi in geom_bands:
            tr = {
                str(t or "").strip()
                for t in (geom.host_trunk_refs or ())
                if str(t or "").strip()
            }
            if not tr:
                continue
            _rehome_host_trunk_to_mid(
                grid,
                host_id,
                frozenset(tr),
                band_lo=int(blo),
                band_hi=int(bhi),
                plan=plan,
                structure_refs=frozenset(geom.structure_refs or ()),
                extras_row=sandwich_mid,
            )
        if str(getattr(plan, "key", "") or "") == "GPS":
            present: set[str] = set()
            for rid in grid.row_ids or []:
                for pl in (grid.cells.get(rid) or {}).values():
                    if (
                        pl is not None
                        and pl.kind in VISIBLE_DEVICE_KINDS
                        and str(pl.ref or "").strip()
                    ):
                        present.add(str(pl.ref))
            host_cells = grid.cells.setdefault(host_id, {})
            for _geom, blo, bhi in geom_bands:
                for t in _geom.host_trunk_refs or ():
                    ts = str(t or "").strip()
                    if not ts or is_rlct_ref(ts) or ts in present:
                        continue
                    # 夹心中层会清 band 内宿主格；hub 落在带左，贴着黄腿。
                    col = max(int(blo) - 1, 2)
                    while col in host_cells:
                        occ = host_cells.get(col)
                        if occ is None or not str(getattr(occ, "ref", "") or "").strip():
                            break
                        col += 1
                    host_cells[col] = _plain_cell(ts, plan)
                    present.add(ts)
        band_lo_all = min((b for _g, b, _e in geom_bands), default=2)
        band_hi_all = max((e for _g, _b, e in geom_bands), default=2)
        # 宿主 path 上非结构主链件若被清带/unique 弄丢，按同列从其它主链行补回
        # （保证 COPAD 宿主行在共享 FIL 处仍能接到竖并柱）
        _restore_host_path_devices(
            plan, grid, host_id, frozenset(all_struct_refs), band_hi=band_hi_all,
        )
        saved_bands.append((host_id, list(geom_bands), sandwich_mid))
        # 馅中层只清「插入的夹心三行」。黄腿覆在已有主链上时，
        # 贯通宿主通往 hub 的列要接匹配，不是馅。
        if not reuse_homes:
            for geom, blo, bhi in geom_bands:
                struct = {
                    str(r or "").strip()
                    for r in (geom.structure_refs or ())
                    if str(r or "").strip()
                }
                cols = [
                    int(c)
                    for m in (map_up, map_lo)
                    for c, pl in (m or {}).items()
                    if int(c) >= 2
                    and pl is not None
                    and pl.kind in VISIBLE_DEVICE_KINDS
                    and str(pl.ref or "").strip() in struct
                ]
                lo_i = min(cols) if cols else int(blo)
                hi_i = max(cols) if cols else int(bhi)
                _clear_host_mid_of_sandwich_band(
                    grid, sandwich_mid, band_lo=lo_i, band_hi=hi_i,
                )
    # 夹心后：可竖并 owner 再收拢（夹心三连作原子，不拆 b1/host/b2）
    # 调和「单腿旁路不进宿主簇」与「FIL 多左邻并柱要 owner 连续」。
    _gather_vertical_merge_owners_after_copad(plan, grid)

    # 竖并策略：夹心后必须重算 L6 + path-owner + prune + 钉顶
    from refnum.layout.merge import refresh_merges_after_copad
    from refnum.layout.place import reflow_main_rows_path_order

    grid = refresh_merges_after_copad(
        plan, grid, signal_infos=signal_infos,
    )
    # 夹心列迁移后：先按 merge 锚纠邻域，再全主链 path 序回流
    # （防 FIL 落到夹心左腿左侧等反序）
    _repair_path_order_around_merges(plan, grid)
    grid = reflow_main_rows_path_order(plan, grid)
    # 回流按宿主 path 投放，会丢掉不在 path 上的 trunk_after。
    # 合并后匹配仍须接到夹心芯片侧，回流后再归位一次。
    for host_id, geom_bands, extras_row in saved_bands:
        if host_id not in (grid.row_ids or ()):
            continue
        for geom, blo, bhi in geom_bands:
            tr = {
                str(t or "").strip()
                for t in (getattr(geom, "host_trunk_refs", None) or ())
                if str(t or "").strip()
            }
            if not tr:
                continue
            _rehome_host_trunk_to_mid(
                grid,
                host_id,
                frozenset(tr),
                band_lo=int(blo),
                band_hi=int(bhi),
                plan=plan,
                structure_refs=frozenset(getattr(geom, "structure_refs", None) or ()),
                extras_row=extras_row,
            )
        if str(getattr(plan, "key", "") or "") == "GPS":
            present: set[str] = set()
            for rid in grid.row_ids or []:
                for pl in (grid.cells.get(rid) or {}).values():
                    if (
                        pl is not None
                        and pl.kind in VISIBLE_DEVICE_KINDS
                        and str(pl.ref or "").strip()
                    ):
                        present.add(str(pl.ref))
            host_cells = grid.cells.setdefault(host_id, {})
            for geom, blo, bhi in geom_bands:
                for t in geom.host_trunk_refs or ():
                    ts = str(t or "").strip()
                    if not ts or is_rlct_ref(ts) or ts in present:
                        continue
                    col = max(int(blo) - 1, 2)
                    while col in host_cells:
                        occ = host_cells.get(col)
                        if occ is None or not str(getattr(occ, "ref", "") or "").strip():
                            break
                        col += 1
                    host_cells[col] = _plain_cell(ts, plan)
                    present.add(ts)
    from refnum.layout.merge_attach import realign_public_fil_after_path_owner
    realign_public_fil_after_path_owner(plan, grid)
    return grid


def apply_copad_to_grid(
    plan: SheetPlan,
    grid: SheetGrid,
    structures: Optional[list[CoPadStructure]] = None,
    *,
    signal_infos=None,
) -> SheetGrid:
    geoms = build_copad_block_geoms(plan, structures, grid=grid)
    if not geoms:
        return grid
    return assemble_copad_blocks(
        plan,
        grid,
        geoms,
        structures=list(structures or ()),
        signal_infos=signal_infos,
    )

# 测试 / 调试脚本从 copad 取 path-owner 符号（实现在 merge）
from refnum.layout.merge import (  # noqa: E402
    add_path_owner_merges as _add_copad_branch_path_owner_merges,
    prune_merges_missing_cells as _prune_merges_missing_cells,
    rebind_merge_row_ids as _rebind_merge_row_ids,
)

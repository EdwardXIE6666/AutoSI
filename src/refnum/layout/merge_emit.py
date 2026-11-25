"""竖并发柱：owner 连续段 → 合并区。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from refnum.layout.merge_span import (
    MergeCheckError,
    col_of_ref,
    condense_merge_branch_actives,
    display_left_neighbor,
    extend_span_to_adjacent_fbrx,
    is_merge_passthrough_row_id,
    merge_col_empty,
    non_owner_transparent,
    owner_index_runs_allowing_passthrough,
    path_left_visible,
    row_has_left_link_to_ref,
)
from refnum.layout.model import LayoutRow, MergeRange, SheetGrid, SheetPlan
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_ref_seq,
    is_non_merge_align_ref,
    is_shared_device,
    is_switch_or_coupler_device,
    should_vertical_merge,
)


def _provisional_merges(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    min_owners: int,
) -> list[MergeRange]:
    """竖合并初值：每个**结构连续** owner 段内 active 的 min..max。

    硬原则：
      - 先靠 L2 把 share_map owner 收成 holes≈0
      - 合并只盖 owner（+ 夹心/gap 装饰行），**永不**盖「不含该 ref 的主链」
    """
    order_ids = list(grid.row_ids)
    id_to_idx = {rid: i for i, rid in enumerate(order_ids)}
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    merges: list[MergeRange] = []

    def _merge_priority(ref: str) -> tuple:
        ru = str(ref).upper()
        if ru.startswith(("SDR", "U", "FIL", "LPF", "BPF", "PA", "LNA", "DIP", "DUP")):
            pri = 0
        else:
            pri = 2
        return (pri, -len(plan.share_map.get(ref, ())), ref)

    lnc = plan.left_neighbor_count or {}
    items = sorted(
        (
            (ref, owners)
            for ref, owners in (plan.share_map or {}).items()
            if len(owners) >= min_owners
            and should_vertical_merge(plan, ref, lnc)
        ),
        key=lambda it: _merge_priority(it[0]),
    )

    for ref, owners in items:
        dev = device_from_plan(plan, ref)
        if not should_vertical_merge(plan, ref, lnc):
            continue
        if is_non_merge_align_ref(ref, dev):
            continue
        owner_in_sheet = [rid for rid in order_ids if rid in owners]
        if len(owner_in_sheet) < min_owners:
            continue

        cols: set[int] = set()
        for rid in owner_in_sheet:
            c = col_of_ref(grid, rid, ref)
            if c is not None:
                cols.add(c)
        # unique 可能把字钉在 COPADBLK extras（不在 share owner 行上）。
        # 只扫 plan owner 会 cols 空，整根公共柱不发，开关/贯通行变成孤岛。
        if len(cols) != 1:
            for rid in order_ids:
                c = col_of_ref(grid, rid, ref)
                if c is not None:
                    cols.add(c)
        if len(cols) != 1:
            continue
        col = next(iter(cols))

        owner_idxs = sorted(
            {
                id_to_idx[rid]
                for rid in owner_in_sheet
                if rid in id_to_idx
                and (
                    col_of_ref(grid, rid, ref) == col
                    or (
                        merge_col_empty(grid, rid, col)
                        and row_has_left_link_to_ref(
                            plan, grid, rid, ref, col, id_to_row,
                        )
                    )
                )
            }
        )
        # unique/夹心抽走字后本列可能只剩 1 格或中段空 owner。
        # 空列且左邻仍可见的第一层 owner 仍属本柱，不得当切断。
        if not owner_idxs:
            continue

        raw_active = sorted(
            i
            for i in owner_idxs
            if row_has_left_link_to_ref(
                plan, grid, order_ids[i], ref, col, id_to_row,
            )
        )
        # 支路收缩（与 path-owner 同一策略）
        active_idxs = condense_merge_branch_actives(
            plan, order_ids, id_to_row, ref, raw_active, grid=grid,
        )
        if not owner_idxs:
            continue

        owner_set = set(owner_idxs)
        # 树节点全成员（含 unique 后已无本列落点的 inactive 分支）。
        # 并柱本体仍只用 owner_idxs/active；下沿填透明行时要看见下游
        # inactive，否则 FIL 盖不到 FBRX 取样行（频段行仍属 FIL 树但不在本列）。
        tree_owner_set = {
            id_to_idx[rid]
            for rid in owner_in_sheet
            if rid in id_to_idx
        }

        def _span_blocked(i: int) -> bool:
            if i in owner_set or i in tree_owner_set:
                # 同树 inactive owner（字 unique 走了）透明穿过，不得切断公柱
                return False
            return not non_owner_transparent(
                plan, grid, order_ids[i], col, id_to_row, ref=ref,
            )

        is_sw_cpl = is_switch_or_coupler_device(dev)

        def _is_fbrx_sample_rid(rid: str) -> bool:
            """FBRX 取样行：禁止与主链 active 并成一段（开关跨取样巨型并）。

            主链多左邻 FIL 仍走下方全局/分段逻辑，不受影响。
            """
            row = id_to_row.get(rid)
            return is_fbrx_sample_row(
                rid=rid,
                devices=getattr(row, "devices", None) if row is not None else None,
                require_no_ant=True,
                max_devs=None,
                source="rid",
            )

        main_own = [
            i for i in owner_idxs
            if not _is_fbrx_sample_rid(order_ids[i])
        ]
        main_active = [
            i for i in active_idxs
            if not _is_fbrx_sample_rid(order_ids[i])
        ]
        fbrx_active = [
            i for i in active_idxs if _is_fbrx_sample_rid(order_ids[i])
        ]

        active_set = set(active_idxs)

        def _extend_hi_through_passthrough(lo: int, hi: int) -> int:
            """树状汇合：并柱只服务「本 ref 汇合的分支」。

            构图语义（公共干 ← 多链路在共享器件处合成）：
              - owner_set = 在该节点挂上的全部链路
              - active = 仍有左邻连到本 ref 的分支（柱上有实体连接）
              - inactive owner = 已被 unique 抽走左邻的同树分支（仍属本节点，
                但不进并柱本体）

            下沿规则：仅当 **hi 之后仍存在本 ref 的 inactive owner** 时，
            才把中间装饰行（gap / 夹心支路）填进柱，
            并在首个 inactive owner 前停下。

            JUMP / 短 FBRX 不是装饰行：path 不含本 ref 不得延长盖进去。
            """
            _ = lo
            n = len(order_ids)
            # 下游是否还有本树节点成员（含未落本列的 inactive）？
            has_downstream_inactive = any(
                i > hi and i in tree_owner_set and i not in active_set
                for i in range(hi + 1, n)
            )
            if not has_downstream_inactive:
                return hi
            while hi + 1 < n:
                nxt = hi + 1
                rid = order_ids[nxt]
                # 树成员但非 active → 并柱止于其前
                if nxt in tree_owner_set and nxt not in active_set:
                    break
                if nxt in owner_set:
                    break
                if not is_merge_passthrough_row_id(
                    rid, ref=ref, plan=plan, id_to_row=id_to_row,
                ):
                    break
                if _span_blocked(nxt):
                    break
                hi = nxt
            return hi

        def _extend_lo_to_empty_owners(lo: int) -> int:
            """unique 抽走字的第一层 owner，左邻仍可见 → 空柱仍进柱顶。

            中间只填夹心/gap。上黄腿不是 owner，不得当柱顶。
            """
            while lo - 1 >= 0:
                nxt = lo - 1
                rid = order_ids[nxt]
                empty_owner = (
                    nxt in tree_owner_set
                    and nxt not in owner_set
                    and not _is_fbrx_sample_rid(rid)
                    and row_has_left_link_to_ref(
                        plan, grid, rid, ref, col, id_to_row,
                    )
                    and merge_col_empty(grid, rid, col)
                )
                if empty_owner:
                    lo = nxt
                    continue
                if not is_merge_passthrough_row_id(
                    rid, ref=ref, plan=plan, id_to_row=id_to_row,
                ):
                    break
                if _span_blocked(nxt):
                    break
                look = nxt - 1
                if look < 0:
                    break
                look_rid = order_ids[look]
                if (
                    look in tree_owner_set
                    and look not in owner_set
                    and not _is_fbrx_sample_rid(look_rid)
                    and row_has_left_link_to_ref(
                        plan, grid, look_rid, ref, col, id_to_row,
                    )
                    and merge_col_empty(grid, look_rid, col)
                ):
                    lo = look
                    continue
                break
            return lo

        def _path_left_visible(idx: int) -> bool:
            rid = order_ids[idx]
            return path_left_visible(grid, rid, id_to_row.get(rid), ref)

        def _expand_same_left_col_siblings(
            lo: int, hi: int, act: list[int],
        ) -> tuple[int, int]:
            """active 跨度外扩：同左邻 + 本列仍有 ref 的兄弟行。

            condensed 后只留代表；同左邻兄弟行仍有字时须进柱。
            **不同左邻**且无 left_link 的行不扩。
            """
            active_lefts: set[str] = set()
            for i in act:
                row = id_to_row.get(order_ids[i])
                left = display_left_neighbor(row, ref)
                if left:
                    active_lefts.add(str(left))
            if not active_lefts:
                return lo, hi
            nloc = len(order_ids)

            def _same_branch(idx: int) -> bool:
                if idx not in owner_set:
                    return False
                row = id_to_row.get(order_ids[idx])
                left = display_left_neighbor(row, ref)
                return bool(left) and str(left) in active_lefts

            # 往下/往上：左邻必须仍可见。已抽走则本行挂在更左汇合件上，是另一条链
            while (
                hi + 1 < nloc
                and _same_branch(hi + 1)
                and _path_left_visible(hi + 1)
            ):
                hi += 1
            while (
                lo - 1 >= 0
                and _same_branch(lo - 1)
                and _path_left_visible(lo - 1)
            ):
                lo -= 1
            return lo, hi

        def _emit_unblocked(act_in: list[int], own_in: list[int], *, use_own: bool) -> None:
            src = sorted(own_in if use_own else act_in)
            if not src:
                return
            lo, hi = src[0], src[-1]
            if hi < lo:
                return
            if any(_span_blocked(i) for i in range(src[0], src[-1] + 1)):
                return
            lo = _extend_lo_to_empty_owners(lo)
            if len(src) < min_owners and lo >= src[0]:
                return
            hi = _extend_hi_through_passthrough(lo, hi)
            lo, hi = _expand_same_left_col_siblings(lo, hi, src)
            lo = _extend_lo_to_empty_owners(lo)
            # 收口与 active 同一真相：前驱被 unique / 夹心搬到黄腿或旁行时，
            # `row_has_left_link_to_ref`（active）仍认它连着，收口不得更严——
            # 否则 active 进了 src 又被剔掉，柱缩回去、其后 owner 变孤岛。
            # `is_live` 那层保持 `_path_left_visible` 的严格语义（真连接段边界）。
            live = [
                i for i in range(lo, hi + 1)
                if i in owner_set
                and i in active_set
                and not _is_fbrx_sample_rid(order_ids[i])
            ]
            if live:
                if is_sw_cpl:
                    lo, hi = extend_span_to_adjacent_fbrx(
                        min(live), max(live), len(order_ids),
                        is_owner=lambda i: i in owner_set,
                        is_fbrx=lambda i: _is_fbrx_sample_rid(order_ids[i]),
                        is_live=_path_left_visible,
                        blocked=lambda i: (
                            _span_blocked(i)
                            or (
                                i not in owner_set
                                # 同树 inactive owner（字被 unique 抽走、未认本列）
                                # 仍是这棵树的成员，与 `_span_blocked` 同一语义：
                                # 透明穿过。挡住它会让向外扩找不到取样行
                                # （主链 owner 把取样隔在外面）。
                                and i not in tree_owner_set
                                and not is_merge_passthrough_row_id(
                                    order_ids[i],
                                    ref=ref,
                                    plan=plan,
                                    id_to_row=id_to_row,
                                )
                            )
                        ),
                    )
                else:
                    lo = min(lo, min(live))
            merges.append(
                MergeRange(
                    ref=ref,
                    col=col,
                    row_start=lo,
                    row_end=hi,
                    row_ids=tuple(
                        order_ids[i] for i in range(lo, hi + 1)
                    ),
                )
            )

        def _emit_for_actives(
            act: list[int],
            cover: list[int] | None = None,
        ) -> None:
            """按树成员在非 owner 处切断分段，再对每段发柱/段锚。

            无切断：尊重同左邻收缩（单支不并）。
            有切断：每段按本列有字的 owner 成柱（收缩后 act 可能只剩 1）；
            树段仅 1 个 owner 且本列有字 → 1 行段锚，unique 按段留字。
            unique 钉顶后多行段只剩 1 格：树段仍 ≥2 owner → 不发 1 行。
            """
            act = sorted(act)
            cover = sorted(cover) if cover is not None else list(act)
            focus = set(cover) | set(act)
            if not focus:
                return
            runs = [
                (t_lo, t_hi)
                for t_lo, t_hi in owner_index_runs_allowing_passthrough(
                    sorted(tree_owner_set),
                    order_ids,
                    grid=grid,
                    col=col,
                    ref=ref,
                    plan=plan,
                    id_to_row=id_to_row,
                )
                if any(t_lo <= i <= t_hi for i in focus)
            ]
            if not runs:
                return
            if len(runs) == 1:
                t_lo, t_hi = runs[0]
                act_in = [i for i in act if t_lo <= i <= t_hi]
                own_in = [i for i in owner_idxs if t_lo <= i <= t_hi]
                _emit_unblocked(act_in, own_in, use_own=False)
                return
            for t_lo, t_hi in runs:
                own_in = [i for i in owner_idxs if t_lo <= i <= t_hi]
                act_in = [i for i in act if t_lo <= i <= t_hi]
                tree_in = [i for i in tree_owner_set if t_lo <= i <= t_hi]
                if len(own_in) >= min_owners:
                    _emit_unblocked(act_in, own_in, use_own=True)
                    continue
                if len(own_in) == 1 and len(tree_in) == 1:
                    i = own_in[0]
                    merges.append(
                        MergeRange(
                            ref=ref,
                            col=col,
                            row_start=i,
                            row_end=i,
                            row_ids=(order_ids[i],),
                        )
                    )

        # 取样支干：与主链同一结构连续段（中间仅夹心/gap/不含本 ref 的透明行）
        # → 进同一柱（第一层支干）。被无关主链隔到远端仍分并，禁止巨型并。
        n_before = len(merges)
        near_fbrx: list[int] = []
        far_fbrx: list[int] = []
        if main_active and fbrx_active:
            mlo, mhi = min(main_active), max(main_active)
            for f in fbrx_active:
                lo_f, hi_f = min(f, mlo), max(f, mhi)
                blocked = any(
                    _span_blocked(i)
                    for i in range(lo_f, hi_f + 1)
                    if i not in owner_set
                )
                if blocked:
                    far_fbrx.append(f)
                else:
                    near_fbrx.append(f)
        else:
            far_fbrx = list(fbrx_active)
        if near_fbrx:
            _emit_for_actives(
                sorted(set(main_active) | set(near_fbrx)),
                cover=sorted(set(main_own) | set(near_fbrx)),
            )
        else:
            _emit_for_actives(main_active, cover=main_own)
        _emit_for_actives(far_fbrx)
        # 取样 active 有没有被接进柱，只看「它是否已被已发的柱盖住」，
        # 不看主链侧是否恰好先发出过柱：owner 被 COPADBLK 分段时主链那段会
        # 先满足 min_owners 发柱，若据此跳过本兜底，取样行永远接不进来
        # （主链一段→兜底生效；主链两段→取样成孤岛）。
        _emitted_rows: set[int] = set()
        for m in merges[n_before:]:
            if str(m.ref) == ref:
                _emitted_rows.update(range(int(m.row_start), int(m.row_end) + 1))
        pending_fbrx = [f for f in fbrx_active if f not in _emitted_rows]
        if pending_fbrx and len(main_active) == 1:
            m0 = main_active[0]
            # 左邻收缩后的 active 集合（仅 condensed 支路）
            branch_set = set(active_idxs)

            def _sample_reachable(f: int) -> bool:
                """取样行与唯一主链支路之间：允许公共干 inactive owner / 透明行。"""
                if abs(f - m0) == 1:
                    return True
                lo, hi = min(f, m0), max(f, m0)
                for i in range(lo + 1, hi):
                    # 其它 condensed 主链支路 → 挡（多支不可与远端 FBRX 糊）
                    if i in branch_set and i not in fbrx_active and i != m0:
                        return False
                    if i in owner_set:
                        # 同树已收缩掉的 inactive owner：公共干空行，可透明
                        continue
                    if _span_blocked(i):
                        return False
                return True

            adj_f = [f for f in pending_fbrx if _sample_reachable(f)]
            if adj_f:
                _emit_for_actives(sorted({m0, *adj_f}))
    return merges


def _is_true_terminus_left(
    *,
    left_ref: str,
    left_bottom: int,
    merge: MergeRange,
    order_ids: list[str],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    """左邻共享件 B 是否为本合并 A 的真终点。

    真：A 的合并段在 B 底边之下没有「换了其它左邻仍接到 A」的行
        （即没有 i > left_bottom 且 display_left(A) != B 的行）。
    假：B 底之下 A 仍因其它左邻支路需要继续合并
        （例：A 在 B 底之下还有其它左邻支路）→ 不得用 B 整段裁短 A。
    """
    if left_bottom >= merge.row_end:
        return True
    for i in range(left_bottom + 1, merge.row_end + 1):
        rid = order_ids[i]
        left = display_left_neighbor(id_to_row.get(rid), merge.ref)
        if left is None:
            continue
        if left != left_ref:
            return False
    return True


def _clip_merges_by_shared_display_left(
    plan: SheetPlan,
    grid: SheetGrid,
    merges: list[MergeRange],
    *,
    min_owners: int = 2,
) -> list[MergeRange]:
    """用显示序紧邻左邻共享件底边裁剪 row_end（仅真终点）。

    语义：若 B 已成柱且是 A 的真终点，则 A.row_end 不得大于 B 的合并底边；
    B 还没成柱 → 不拿落位行号当第二套柱高。
    若 A 在 B 底之下仍接其它左邻支路，则跳过用 B 裁 A。
    多轮：左列先稳定。
    """
    if not merges:
        return merges
    order_ids = list(grid.row_ids)
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    lnc = plan.left_neighbor_count or {}

    current = list(merges)
    for _ in range(max(4, len(current) + 2)):
        by_ref: dict[str, list[MergeRange]] = defaultdict(list)
        for m in current:
            by_ref[m.ref].append(m)
        work = sorted(current, key=lambda m: (m.col, m.row_start, m.ref))
        next_list: list[MergeRange] = []
        changed = False
        for m in work:
            if int(m.row_start) == int(m.row_end):
                next_list.append(m)
                continue
            left_shared: set[str] = set()
            for i in range(m.row_start, m.row_end + 1):
                rid = order_ids[i]
                left = display_left_neighbor(id_to_row.get(rid), m.ref)
                if not left:
                    continue
                if is_non_merge_align_ref(left, device_from_plan(plan, left)):
                    continue
                if not is_shared_device(left, lnc):
                    continue
                if len(plan.share_map.get(left, ())) < min_owners:
                    continue
                left_shared.add(left)
            if not left_shared:
                next_list.append(m)
                continue

            clip_hi: int | None = None
            for left in left_shared:
                left_bottom: int | None = None
                for lm in by_ref.get(left, ()):
                    left_bottom = (
                        lm.row_end if left_bottom is None else max(left_bottom, lm.row_end)
                    )
                if left_bottom is None:
                    continue
                if not _is_true_terminus_left(
                    left_ref=left,
                    left_bottom=left_bottom,
                    merge=m,
                    order_ids=order_ids,
                    id_to_row=id_to_row,
                ):
                    continue
                clip_hi = (
                    left_bottom if clip_hi is None else max(clip_hi, left_bottom)
                )

            if clip_hi is None or clip_hi >= m.row_end:
                next_list.append(m)
                continue
            if clip_hi <= m.row_start:
                changed = True
                continue
            new_m = replace(
                m,
                row_end=clip_hi,
                row_ids=tuple(order_ids[i] for i in range(m.row_start, clip_hi + 1)),
            )
            next_list.append(new_m)
            if new_m.row_end != m.row_end:
                changed = True
        current = next_list
        if not changed:
            break
    return current


def align_upstream_merge_span_to_path_successors(
    plan: SheetPlan,
    grid: SheetGrid,
    merges: list[MergeRange],
) -> list[MergeRange]:
    """公共滤波并柱包住 path 邻接的被动并柱行跨度。

    领域：
      - FIL/LPF… 是汇合柱；path 上邻接的被动合并件（C/L/R/T…）
        行跨度须被 FIL 并柱包含（该行 path 含该 FIL）。
      - 禁止把被动柱上 path 不含 FIL 的行（JUMP stub）抄进 FIL。
      - 不拉齐到 U*/其它 FIL*（同列多 FIL 叠段时会互抢行而重叠）。
      - 仅跨列拉齐；同列自由区严格裁剪，禁止两段并柱交错。
    """
    if not merges or not plan.rows:
        return merges
    by_ref: dict[str, list[MergeRange]] = defaultdict(list)
    for m in merges:
        by_ref[str(m.ref)].append(m)

    def _is_public_chain_upstream(ref: str) -> bool:
        """公共滤波/分频类并柱（FIL/LPF…）。"""
        ru = str(ref or "").upper()
        return ru.startswith(
            ("FIL", "LPF", "BPF", "DIP", "DUP", "DPX", "TPX")
        )

    def _is_passive_align_target(ref: str) -> bool:
        """只对齐到被动 RLCT 类，不对齐 U*/另一 FIL（避免同列互吞）。"""
        ru = str(ref or "").strip().upper()
        if not ru or _is_public_chain_upstream(ru):
            return False
        # 多端口/开关/有源：字头 U/PA/SW/ESW 等
        if ru.startswith(("U", "PA", "SW", "ESW", "IC", "TRX", "LNA")):
            return False
        # 被动 R/L/C/T
        return ru[0] in "CLRT"

    # (FIL, passive)：path **显示序严格相邻** 的 FIL↔被动。
    # 禁止隔着其它器件配对（中间有 L/C 干路，不得互拉）。
    pairs: set[tuple[str, str]] = set()
    for row in plan.rows or []:
        if row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            if a not in by_ref or b not in by_ref:
                continue
            if _is_public_chain_upstream(a) and _is_passive_align_target(b):
                pairs.add((a, b))
            if _is_public_chain_upstream(b) and _is_passive_align_target(a):
                pairs.add((b, a))

    if not pairs:
        return merges

    def _free_bounds_on_col(
        col: int,
        lo: int,
        hi: int,
        self_ref: str,
        current: list[MergeRange],
    ) -> tuple[int, int]:
        """同列自由区 [free_lo, free_hi]：含原 [lo,hi]，止于邻段外沿。"""
        free_lo = 0
        free_hi = 10**9
        for o in current:
            if int(o.col) != int(col):
                continue
            # 跳过自身这一段
            if (
                str(o.ref) == self_ref
                and int(o.row_start) == lo
                and int(o.row_end) == hi
            ):
                continue
            olo, ohi = int(o.row_start), int(o.row_end)
            if ohi < lo:
                free_lo = max(free_lo, ohi + 1)
            elif olo > hi:
                free_hi = min(free_hi, olo - 1)
            else:
                # 已与邻段交错：拒绝再扩
                return lo, hi
        return free_lo, free_hi

    out = list(merges)
    order_ids = list(grid.row_ids or [])
    id_to_row = {r.row_id: r for r in (plan.rows or [])}

    def _row_path_has_ref(idx: int, need: str) -> bool:
        if not (0 <= idx < len(order_ids)):
            return False
        rid = order_ids[idx]
        if "|__gap__" in str(rid) or "COPADBLK" in str(rid):
            return True
        row = id_to_row.get(rid)
        if row is None or row.path is None:
            return False
        return str(need) in {str(x) for x in display_ref_seq(row.path) if x}

    for _ in range(8):
        changed = False
        # 每 ref 取各段并集跨度（被动件通常单段）
        ref_spans: dict[str, tuple[int, int]] = {}
        for m in out:
            rs = str(m.ref)
            lo, hi = int(m.row_start), int(m.row_end)
            if rs in ref_spans:
                olo, ohi = ref_spans[rs]
                ref_spans[rs] = (min(olo, lo), max(ohi, hi))
            else:
                ref_spans[rs] = (lo, hi)
        new_out: list[MergeRange] = []
        for m in out:
            up = str(m.ref)
            lo, hi = int(m.row_start), int(m.row_end)
            if not _is_public_chain_upstream(up):
                new_out.append(m)
                continue
            nlo, nhi = lo, hi
            for a, b in pairs:
                if a != up or b not in ref_spans:
                    continue
                # 不同列才拉齐
                b_cols = {int(x.col) for x in by_ref.get(b, ())}
                if int(m.col) in b_cols:
                    continue
                blo, bhi = ref_spans[b]
                nlo, nhi = min(nlo, blo), max(nhi, bhi)
            # 只扩到 path 含该 FIL 的行；被动柱上的 JUMP stub 不跟
            while nhi > hi and not _row_path_has_ref(nhi, up):
                nhi -= 1
            while nlo < lo and not _row_path_has_ref(nlo, up):
                nlo += 1
            if nlo == lo and nhi == hi:
                new_out.append(m)
                continue
            flo, fhi = _free_bounds_on_col(int(m.col), lo, hi, up, out)
            nlo, nhi = max(nlo, flo), min(nhi, fhi)
            nlo = max(0, nlo)
            if order_ids:
                nhi = min(len(order_ids) - 1, nhi)
            if nlo > nhi or (nlo == lo and nhi == hi):
                new_out.append(m)
                continue
            changed = True
            new_out.append(
                MergeRange(
                    ref=m.ref,
                    col=m.col,
                    row_start=nlo,
                    row_end=nhi,
                    row_ids=tuple(
                        order_ids[i] for i in range(nlo, nhi + 1)
                    ) if order_ids else m.row_ids,
                )
            )
        out = new_out
        if not changed:
            break
    return out


def compute_vertical_merges(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    min_owners: int = 2,
) -> list[MergeRange]:
    """对 share_map 中 owner≥min_owners 的 ref 计算竖合并区间。

    夹心插行后必须再跑：旧柱行号失效。unique 钉顶后只剩顶格，单靠
    finalize 算不回这些柱，要靠夹心后这一遍 + union 保住。
    """
    merges = _provisional_merges(plan, grid, min_owners=min_owners)
    merges = _clip_merges_by_shared_display_left(
        plan, grid, merges, min_owners=min_owners,
    )
    merges = align_upstream_merge_span_to_path_successors(plan, grid, merges)

    # 列内合并区间不应重叠（同一列两段可相邻，不可交错占同行）
    by_col: dict[int, list[MergeRange]] = {}
    for m in merges:
        by_col.setdefault(m.col, []).append(m)
    for col, ms in by_col.items():
        ms_sorted = sorted(ms, key=lambda m: m.row_start)
        for a, b in zip(ms_sorted, ms_sorted[1:]):
            if b.row_start <= a.row_end:
                if not (
                    a.row_start == b.row_start
                    and a.row_end == b.row_end
                    and a.ref == b.ref
                ):
                    raise MergeCheckError(
                        f"[handbook.merge] 同列合并重叠 col={col} "
                        f"{a.ref}[{a.row_start},{a.row_end}] vs "
                        f"{b.ref}[{b.row_start},{b.row_end}]"
                    )

    return merges


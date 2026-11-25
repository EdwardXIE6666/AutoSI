"""竖并共享谓词：真连接、透明行、支路收缩。无发柱 / prune / 附着。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from refnum.layout.model import (
    is_decoration_row_id,
    UNIQUE_BEARING_KINDS,
    VISIBLE_DEVICE_KINDS,
    LayoutRow,
    SheetGrid,
    SheetPlan,
)
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_neighbor,
    display_ref_seq,
    is_ant_ref,
    is_layout_shared,
    is_non_merge_align_ref,
    should_vertical_merge,
)


@dataclass(frozen=True)
class MergeCheckError(Exception):
    """合并合法性失败（开发期硬失败，禁止静默盖错行）。"""

    message: str

    def __str__(self) -> str:
        return self.message


def col_of_ref(grid: SheetGrid, row_id: str, ref: str) -> int | None:
    cells = grid.cells.get(row_id) or {}
    for col, placed in cells.items():
        if placed.kind in VISIBLE_DEVICE_KINDS and placed.ref == ref:
            return col
    return None


def row_has_left_link_to_ref(
    plan: SheetPlan,
    grid: SheetGrid,
    row_id: str,
    ref: str,
    col: int,
    id_to_row: dict[str, LayoutRow],
) -> bool:
    """本行路径是否从显示序接到该合并器件（以本行落位为准）。

    active 判定（单一原则：左邻仍接到本行）：
      1. path 含 ref；ref 为显示序起点 → active
      2. 紧邻左邻落在本行 col'<col，或落在 COPAD 支路
      3. 紧邻左邻为 RLCT 时：连续 RLCT 段内任一件仍可见 → active
      4. 否则不 active（**禁止 has_self / multiport 泄并**）

    同左邻汇合滤波跨行并柱：走夹心 path-owner，不在此开后门。
    """
    row = id_to_row.get(row_id)
    if row is None:
        return False
    seq = display_ref_seq(row.path)
    if ref not in seq:
        return False
    idx = seq.index(ref)
    if idx == 0:
        return True
    cells = grid.cells.get(row_id) or {}

    def _visible_left_of_col(need_ref: str) -> bool:
        for c, placed in cells.items():
            if c >= col:
                continue
            if placed.kind not in UNIQUE_BEARING_KINDS:
                continue
            if placed.ref == need_ref:
                return True
        return False

    def _on_copad_branch(need_ref: str) -> bool:
        for rid, rcells in (grid.cells or {}).items():
            if "COPADBLK" not in str(rid):
                continue
            for pl in (rcells or {}).values():
                if pl is not None and str(pl.ref or "") == need_ref:
                    return True
        return False

    left_ref = str(seq[idx - 1] or "").strip()
    if left_ref and _visible_left_of_col(left_ref):
        return True
    if left_ref and _on_copad_branch(left_ref):
        return True

    # 连续 RLCT/ANT 左段：任一件仍可见
    rlct_run: list[str] = []
    for j in range(idx - 1, -1, -1):
        cand = str(seq[j] or "").strip()
        if not cand:
            continue
        dev = device_from_plan(plan, cand)
        if not is_non_merge_align_ref(cand, dev):
            break
        rlct_run.append(cand)
    for rref in rlct_run:
        if _visible_left_of_col(rref) or _on_copad_branch(rref):
            return True

    return False


def display_left_neighbor(row: LayoutRow | None, ref: str) -> str | None:
    """显示序（左芯片→右）上 ref 的紧邻左邻。"""
    return display_neighbor(row, ref, side="left")


def device_ref_visible(
    grid: SheetGrid | None, row_id: str, need_ref: str,
) -> bool:
    """本行格子是否仍显示该位号。"""
    if grid is None or not need_ref:
        return False
    for pl in (grid.cells.get(row_id) or {}).values():
        if (
            pl is not None
            and getattr(pl, "kind", None) in VISIBLE_DEVICE_KINDS
            and str(pl.ref or "").strip() == need_ref
        ):
            return True
    return False


def path_left_visible(
    grid: SheetGrid | None,
    row_id: str,
    row: LayoutRow | None,
    ref: str,
) -> bool:
    """本行是否仍显示 ref 的紧邻左邻（真连接）。"""
    left = display_left_neighbor(row, ref)
    return bool(left) and device_ref_visible(grid, row_id, left)


def extend_span_to_adjacent_fbrx(
    lo: int,
    hi: int,
    n: int,
    *,
    is_owner: Callable[[int], bool],
    is_fbrx: Callable[[int], bool],
    is_live: Callable[[int], bool],
    blocked: Callable[[int], bool],
) -> tuple[int, int]:
    """真连接段向两侧扩到相邻取样（中间仅空壳 / 可过行）。"""

    def _walk(start: int, step: int) -> int | None:
        i = start + step
        found: int | None = None
        while 0 <= i < n:
            if blocked(i):
                break
            if is_owner(i):
                if is_fbrx(i):
                    found = i
                elif is_live(i):
                    break
            i += step
        return found

    up = _walk(lo, -1)
    down = _walk(hi, 1)
    if up is not None:
        lo = up
    if down is not None:
        hi = down
    return lo, hi


def condense_active_by_left_neighbor(
    order_ids: list[str],
    id_to_row: dict[str, LayoutRow],
    ref: str,
    active_idxs: list[int],
    plan: SheetPlan | None = None,
    *,
    grid: "SheetGrid | None" = None,
) -> list[int]:
    """同一显示左邻汇入 = 上游已合成的公共干，只留一支代表。

    树状汇合（设计 §L6）：并柱高度 = **汇合支路数**，不是 path 条数。

    - 左邻各异（私有匹配）→ 各支，并柱盖各行
    - 多路经同一左邻进公共干 → 一支；另路经另一左邻 → 另一支
      → 并柱只盖公共链路行 + 新汇入支路，禁止灌满已汇合频段空行

    代表行（同左邻组）：
      1) 优先 **左邻仍写在本行** 的 active（真连接；避免 unique 后
         无左邻的空壳行抢 min 下标、剪掉真正挂该左邻的频段行）
      2) 否则行序最前（与 unique 靠前保留一致）

    **例外·共享 multiport/IC 左邻**（非 RLCT，share owners≥2）：
      保留该左邻下**全部** active 行。unique 常把「左邻 U」与「FIL 值」
      拆到不同频段行；若只留一支代表，并柱盖不到真正挂着 U 的行。
    """
    if len(active_idxs) <= 1:
        return list(active_idxs)
    from collections import defaultdict

    groups: dict[str, list[int]] = defaultdict(list)
    for i in active_idxs:
        row = id_to_row.get(order_ids[i])
        left = display_left_neighbor(row, ref)
        # 无左邻（行首）各自成支，勿并成一组
        key = left if left else f"__head__{i}"
        groups[key].append(i)
    out: list[int] = []
    share_map = (plan.share_map if plan is not None else None) or {}
    for key, idxs in groups.items():
        if str(key).startswith("__head__"):
            out.append(min(idxs))
            continue
        left = str(key)
        keep_all = False
        if plan is not None and left:
            lo_owners = share_map.get(left) or ()
            dev = device_from_plan(plan, left)
            # 共享且可竖并的非 RLCT 左邻（典型 multiport U）
            if (
                len(lo_owners) >= 2
                and not is_non_merge_align_ref(left, dev)
            ):
                keep_all = True
        if keep_all:
            out.extend(idxs)
            continue
        # 仍显示该左邻的行：多行同左且左邻都在 → 全留（同支并柱高度）
        # 仅一行仍显示左邻 → 只留它（unique 后的真连接代表）
        # 都无左邻字 → min 下标
        if grid is not None and left:
            with_left = [
                i for i in idxs
                if device_ref_visible(grid, order_ids[i], left)
            ]
            if len(with_left) >= 2:
                out.extend(with_left)
                continue
            if with_left:
                out.append(min(with_left))
                continue
        out.append(min(idxs))
    return sorted(set(out))


def condense_merge_branch_actives(
    plan: SheetPlan,
    order_ids: list[str],
    id_to_row: dict[str, LayoutRow],
    ref: str,
    active_idxs: list[int],
    *,
    grid: "SheetGrid | None" = None,
) -> list[int]:
    """并柱支路收缩（L6 与 path-owner **同一策略**）。

    - ``is_layout_shared`` 或 share owners>2：同左邻收成 1 支
      （多路干 / 多频段纯串联 → 收缩后常只剩 1 支 → 不并）
    - 仅 multi_owner 且 owners==2：不收缩（同左邻双路汇合 FIL）
    - 例外：左邻为共享 multiport/IC 时保留该组全部 active（见上）
    - ``grid``：代表行优先「左邻仍可见」（见 ``condense_active_by_left_neighbor``）
    """
    lnc = plan.left_neighbor_count or {}
    owners = (plan.share_map or {}).get(ref) or ()
    if is_layout_shared(plan, ref, lnc) or len(owners) > 2:
        return condense_active_by_left_neighbor(
            order_ids, id_to_row, ref, active_idxs, plan=plan, grid=grid,
        )
    return sorted(active_idxs)


def contiguous_index_runs(idxs: list[int]) -> list[tuple[int, int]]:
    """已排序下标 → 连续段 [(lo, hi), ...]。"""
    if not idxs:
        return []
    runs: list[tuple[int, int]] = []
    lo = prev = idxs[0]
    for i in idxs[1:]:
        if i == prev + 1:
            prev = i
            continue
        runs.append((lo, prev))
        lo = prev = i
    runs.append((lo, prev))
    return runs


def is_jump_band_row_id(rid: str) -> bool:
    """JUMP 取样/旁路行（rid 含 JUMP 标记）。"""
    su = str(rid or "").upper()
    return "|JUMP(" in su or su.startswith("JUMP(") or "|JUMP|" in su


def is_merge_passthrough_row_id(
    rid: str,
    *,
    ref: str = "",
    plan: "SheetPlan | None" = None,
    id_to_row: dict | None = None,
    allow_foreign_fbrx: bool = False,
) -> bool:
    """**唯一**「透明行」判定：柱矩形 / 收拢块里可盖、自身不是 owner 的行。

    基础语义（⑥ 并柱与 ③ 收拢完全相同）：
      - 夹心支路腿（``COPADBLK``）与链路空行（``__gap__``）→ 透明
      - JUMP → **不**透明。JUMP 若 path 含该 ref，它已在 owner 集，不走本判定
      - 其余主链行 → 不透明

    ``allow_foreign_fbrx``（③ 收拢专用，唯一差异）：
      额外把「path **不含**该 ref 的短 FBRX 取样行」也算透明——挤出它会把取样
      链拆离自己的耦合器。⑥ 并柱不开此项：否则异 ref 的取样会被当透明灌进
      无关柱。差异只此一条，用参数表达，**不得再写第二个函数**。

    ``plan`` / ``id_to_row`` 只在开关打开时用于查该行 path。
    """
    s = str(rid or "")
    if is_decoration_row_id(s):
        return True
    if not allow_foreign_fbrx:
        return False
    if is_jump_band_row_id(s):
        return False
    if "FBRX" not in s.upper():
        return False
    rs = str(ref or "").strip()
    if rs:
        rows = id_to_row
        if rows is None and plan is not None:
            rows = {r.row_id: r for r in (getattr(plan, "rows", None) or [])}
        if rows:
            row = rows.get(rid)
            path = getattr(row, "path", None) if row is not None else None
            if path is not None:
                seq = {str(x) for x in display_ref_seq(path) if x}
                return rs not in seq
            if row is not None:
                return True
    return is_fbrx_sample_row(
        rid=rid, require_no_ant=True, max_devs=None, source="rid",
    )


def is_main_content_row_id(rid: str) -> bool:
    """主内容行 = 不是透明装饰行（夹心支路腿 / 链路空行）。

    ``is_merge_passthrough_row_id`` 基础语义的**反面**，同一个定义的两个方向。
    以前 copad / place / unique 各写了一份同义判定（名字还都不一样），
    其中一份多带了冗余的 ``endswith("|b1"/"|b2")``——b1/b2 的 rid 必含
    ``COPADBLK``，那两条永远不会单独命中。
    """
    return not is_merge_passthrough_row_id(rid)


def merge_col_empty(grid: SheetGrid | None, rid: str, col: int | None) -> bool:
    if grid is None or col is None:
        return False
    pl = (grid.cells.get(rid) or {}).get(int(col))
    return (
        pl is None
        or getattr(pl, "kind", None) == "gray"
        or not str(getattr(pl, "ref", "") or "").strip()
    )


def row_ended_at_left_join(
    plan: SheetPlan | None,
    grid: SheetGrid,
    rid: str,
    col: int,
    id_to_row: dict | None,
    *,
    ref: str = "",
) -> bool:
    """这条链是否已在更左的合并件结束（§2）。JUMP 不是。

    join 与目标柱之间的 ANT 是路尽头，不算还在走。
    """
    if plan is None or is_jump_band_row_id(rid):
        return False
    row = (id_to_row or {}).get(rid)
    if row is None or row.path is None:
        return False
    seq = [str(x) for x in display_ref_seq(row.path) if x]
    core = [x for x in seq if not is_ant_ref(x)]
    if not core:
        return False
    last = core[-1]
    if last == str(ref or "").strip():
        return False
    if not (
        last.upper().startswith(("FIL", "LPF"))
        or should_vertical_merge(plan, last)
    ):
        return False
    ids = list(grid.row_ids or [])
    try:
        ri = ids.index(rid)
    except ValueError:
        return False
    join_col = col_of_ref(grid, rid, last)
    if join_col is None:
        for m in grid.merges or []:
            if str(m.ref or "").strip() != last:
                continue
            if int(m.row_start) <= ri <= int(m.row_end):
                join_col = int(m.col)
                break
    if join_col is None:
        for other in ids:
            join_col = col_of_ref(grid, other, last)
            if join_col is not None:
                break
    if join_col is None or int(join_col) >= int(col):
        return False
    cells = grid.cells.get(rid) or {}
    for c, pl in cells.items():
        if not (int(join_col) < int(c) < int(col)):
            continue
        if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
            continue
        rs = str(pl.ref or "").strip()
        if not rs:
            continue
        # 短链 core 已在 FIL 结束；本行还挂着的 ANT 是路尽头，不是还在走。
        if is_ant_ref(rs, getattr(pl, "device", None)):
            continue
        return False
    return True


def non_owner_transparent(
    plan: SheetPlan | None,
    grid: SheetGrid | None,
    rid: str,
    col: int | None,
    id_to_row: dict | None,
    *,
    ref: str = "",
) -> bool:
    """非 owner 这一格能否被柱盖过：夹心/gap，或空列且汇入即止。"""
    if is_merge_passthrough_row_id(
        rid, ref=ref, plan=plan, id_to_row=id_to_row,
    ):
        return True
    if grid is None or col is None:
        return False
    return merge_col_empty(grid, rid, col) and row_ended_at_left_join(
        plan, grid, rid, int(col), id_to_row, ref=ref,
    )


def owner_index_runs_structural_passthrough_only(
    owner_idxs: list[int],
    order_ids: list[str],
) -> list[tuple[int, int]]:
    """owner 下标分段：**仅**夹心支路行 / 链路空行可夹在中间仍算同一段。

    构图硬原则（设计 §L2/L6）：
      1) 行规划先让要合并的 owner 尽量相邻
      2) **禁止**竖并穿越「不含该器件的其它主链」——即便该列为空

    历史错误：曾允许「非 owner 主链空列穿过」以迁就 FBRX 夹在两组 FIL 之间；
    那是用合并穿透掩盖行序失败，会破坏结构。正确做法是 L2 把 owner 收拢。
    """
    if not owner_idxs:
        return []
    owner_set = set(owner_idxs)

    def _gap_ok(j: int) -> bool:
        if j in owner_set:
            return True
        if not (0 <= j < len(order_ids)):
            return False
        return is_merge_passthrough_row_id(order_ids[j])

    runs: list[tuple[int, int]] = []
    lo = prev = owner_idxs[0]
    for i in owner_idxs[1:]:
        if all(_gap_ok(j) for j in range(prev + 1, i)):
            prev = i
            continue
        runs.append((lo, prev))
        lo = prev = i
    runs.append((lo, prev))
    return runs


def owner_index_runs_allowing_passthrough(
    owner_idxs: list[int],
    order_ids: list[str],
    *,
    grid: SheetGrid | None = None,
    col: int | None = None,
    ref: str = "",
    plan: SheetPlan | None = None,
    id_to_row: dict | None = None,
) -> list[tuple[int, int]]:
    """owner 分段；夹心/gap 或汇入即止空列可夹在中间。"""
    if not owner_idxs:
        return []
    owner_set = set(owner_idxs)

    def _gap_ok(j: int) -> bool:
        if j in owner_set:
            return True
        if not (0 <= j < len(order_ids)):
            return False
        return non_owner_transparent(
            plan, grid, order_ids[j], col, id_to_row, ref=ref,
        )

    runs: list[tuple[int, int]] = []
    lo = prev = owner_idxs[0]
    for i in owner_idxs[1:]:
        if all(_gap_ok(j) for j in range(prev + 1, i)):
            prev = i
            continue
        runs.append((lo, prev))
        lo = prev = i
    runs.append((lo, prev))
    return runs


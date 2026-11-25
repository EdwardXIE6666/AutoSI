"""功能5 阶段 B — **可见位号唯一**（精炼边界）。

本文件只做：
  - 非共享 / 无并柱：全表只留 1 格
  - 有并柱：段内钉顶写字、段外清孤儿（占格去重，不改并柱资格/高度算法）
  - FBRX 取样链回写、空壳行删除、写表断言

并柱资格 / 高度 / path-owner / settle → ``refnum_layout_merge``。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional

from refnum.layout.model import (
    UNIQUE_BEARING_KINDS,
    VISIBLE_DEVICE_KINDS,
    MergeRange,
    PlacedCell,
    SheetGrid,
    SheetPlan,
)
from refnum.layout.place import pack_row_by_path
from refnum.layout.rows import is_fbrx_sample_row
from refnum.layout.share import (
    device_from_plan,
    display_ref_seq,
    is_ant_ref,
    is_pa_lna_device,
    is_rlct_ref,
    is_switch_or_coupler_device,
    should_vertical_merge,
)
from refnum.layout.merge import (  # 竖并生命周期在 merge
    extend_span_to_adjacent_fbrx,
    is_jump_band_row_id,
    is_main_content_row_id,
    is_merge_passthrough_row_id,
    normalize_merges_pin_top,
    path_left_visible,
)


class UniqueRefError(RuntimeError):
    """Sheet 内出现重复器件位号。"""


def collect_device_locations(
    grid: SheetGrid,
) -> dict[str, list[tuple[int, str, int, PlacedCell]]]:
    """ref → [(row_index, row_id, col, cell), ...]"""
    loc: dict[str, list[tuple[int, str, int, PlacedCell]]] = defaultdict(list)
    for i, rid in enumerate(grid.row_ids):
        for col, placed in (grid.cells.get(rid) or {}).items():
            # 器件 / Co-pad 腿 / 侧支分叉并位 计入唯一；标签/灰格不计
            if placed.kind not in UNIQUE_BEARING_KINDS:
                continue
            ref = str(placed.ref or "").strip()
            if not ref:
                continue
            loc[ref].append((i, rid, col, placed))
    return loc


def _is_copad_sandwich_host(grid: SheetGrid, rid: str) -> bool:
    """是否夹心中层宿主行（上一行 b1、下一行 b2）。"""
    ids = list(grid.row_ids or [])
    if rid not in ids:
        return False
    i = ids.index(rid)
    if i <= 0 or i >= len(ids) - 1:
        return False
    return str(ids[i - 1]).endswith("|b1") and str(ids[i + 1]).endswith("|b2")


def _through_trunk_rank(
    grid: SheetGrid,
    rid: str,
    plan: Optional[SheetPlan] = None,
    ref: str = "",
) -> int:
    """公尾 unique：主链夹心宿主=2；离宿主最近的其它主链=1。

    FIL 柱顶字常在第一频段 owner 行。同等长公尾时，字行不得把
    C…T→ANT 吸到外面；应落在夹心宿主或紧挨宿主的贯通行。
    COPADBLK 中层 extras 夹在 b1/b2 之间，不是宿主。
    夹心宿主若只是 FIL→ANT stub（无实质公尾），不当贯通=2，避免 COLAY 短链抢走贯通行。
    """
    ids = list(grid.row_ids or [])
    if rid not in ids:
        return 0
    i = ids.index(rid)
    if _is_sandwich_host_row(grid, i):
        stub = False
        if plan is not None and str(ref or "").strip():
            _z, _own, tail = _public_tail_metrics(plan, rid, ref)
            # FIL→T→ANT（tail=3）仍是短链宿主，不是贯通公干
            stub = int(tail) <= 3
        if not stub:
            return 2
    if not is_main_content_row_id(rid):
        return 0
    hosts = [
        j for j, r in enumerate(ids) if _is_sandwich_host_row(grid, j)
    ]
    if not hosts:
        return 0
    mine = min(abs(i - h) for h in hosts)
    others = [
        min(abs(j - h) for h in hosts)
        for j, r in enumerate(ids)
        if is_main_content_row_id(r) and not _is_sandwich_host_row(grid, j)
    ]
    if not others:
        return 0
    return 1 if mine == min(others) else 0


def _path_ends_with_ant(plan: Optional[SheetPlan], rid: str) -> bool:
    """显示序右端是天线（BAND 存贮 ANT-first，不能看 devices[-1]）。"""
    if plan is None:
        return False
    for row in plan.rows or []:
        if row.row_id != rid:
            continue
        if row.path is not None:
            seq = display_ref_seq(row.path)
            return bool(seq and str(seq[-1] or "").upper().startswith("ANT"))
        if row.devices:
            return str(
                getattr(row.devices[-1], "ref", "") or ""
            ).upper().startswith("ANT")
        return False
    return False


def _path_pred_visible(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> int:
    """本行 path 显示序上 ref 的紧邻前驱是否仍落在本行（含夹心支路行不计入主链前驱）。

    用于 unique：私有前缀未断的行优先保留共享后续，避免夹心宿主靠可见 ANT
    吸走其它链路的公共尾。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return 0
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        if rs not in seq:
            return 0
        i = seq.index(rs)
        if i <= 0:
            return 0
        pred = seq[i - 1]
        cells = grid.cells.get(rid) or {}
        if any(
            pl is not None
            and str(pl.ref or "").strip() == pred
            and pl.kind in VISIBLE_DEVICE_KINDS
            for pl in cells.values()
        ):
            return 1
        return 0
    return 0


def _scan_joins_before_ref(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> tuple[list[str], list[int], list[int], int]:
    """一次扫描 path：ref 前多主汇合的 ref 列表、owners、下标，以及 seq 长。

    返回 ``(join_refs, owners, indices, seq_len)``；无 path/无 ref 时全空。
    """
    if plan is None or not ref:
        return [], [], [], 0
    rs = str(ref).strip()
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        if rs not in seq:
            return [], [], [], 0
        idx = seq.index(rs)
        jrefs: list[str] = []
        owns: list[int] = []
        idxs: list[int] = []
        for j, x in enumerate(seq[:idx]):
            # 汇合点 = 合并器件（唯一判定）
            if not should_vertical_merge(plan, x):
                continue
            jrefs.append(x)
            owns.append(len((plan.share_map or {}).get(x) or ()))
            idxs.append(j)
        return jrefs, owns, idxs, len(seq)
    return [], [], [], 0


def _public_tail_metrics(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> tuple[int, int, int]:
    """path 上 ref 的公尾度量，供 unique 比较（元组越大越好）。

    返回 ``(0, join_strength, eff_len)``：
      - 实质公尾（最后汇合尾>2）：``(0, max_owners, last_tail)``
      - stub/无汇合：``(0, 0, eff)``（stub=短尾；无汇合=path 长）
    """
    jrefs, owns, idxs, seq_len = _scan_joins_before_ref(plan, rid, ref)
    if seq_len <= 0:
        return (0, 0, 0)
    if not idxs:
        return (0, 0, seq_len)
    last_tail = seq_len - idxs[-1]
    if last_tail <= 2:
        return (0, 0, last_tail)
    # 公尾强度 = 最后汇合点的 owners。更早开关/芯片 owners 再多
    # 也不是这段公共后缀的汇合（否则 ANT 被吸到私有前级行）。
    return (0, int(owns[-1]), last_tail)


def _public_tail_len_after_last_join(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> int:
    """有效公尾长度（最后汇合尾 / stub 尾 / 无汇合 path 长）。"""
    return int(_public_tail_metrics(plan, rid, ref)[2])


def _strongest_join_before_ref(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> str:
    """path 上 ref 前 owners 最多的多主汇合件（无则空串）。"""
    jrefs, owns, _idxs, _n = _scan_joins_before_ref(plan, rid, ref)
    if not jrefs:
        return ""
    best_i = max(range(len(owns)), key=lambda i: owns[i])
    return jrefs[best_i]


def _join_visible_on_row(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> int:
    """强汇合件在本行的可见强度（公尾 unique 防拆链）。

    2 = 本行有汇合件字；1 = 仅被并柱跨到；0 = 无。
    L4 夹心后部分行假升 host_rank 会把 ANT 吸到无 FIL 字行；
    优先「本行仍写着最强汇合件」的行（钉顶字行）。

    **注意**：对 ANT 不得把 join_vis 放在最高位——中间 FE 多主 FIL
    字行会抢走 ANT，拆掉公共天线尾。
    ANT 见 ``_row_keep_score`` 的 continuity / tail_run 优先。
    """
    join = _strongest_join_before_ref(plan, rid, ref)
    if not join:
        return 0
    cells = grid.cells.get(rid) or {}
    if any(str(pl.ref or "").strip() == join for pl in cells.values() if pl):
        return 2
    try:
        ridx = list(grid.row_ids or []).index(rid)
    except ValueError:
        return 0
    for m in grid.merges or []:
        if str(getattr(m, "ref", "") or "").strip() != join:
            continue
        if int(m.row_start) <= ridx <= int(m.row_end):
            return 1
    return 0


def _path_seq_on_row(
    plan: Optional[SheetPlan],
    rid: str,
) -> list[str]:
    if plan is None:
        return []
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        return [str(x) for x in display_ref_seq(row.path) if x]
    return []


def _row_visible_refs(grid: SheetGrid, rid: str) -> set[str]:
    return {
        str(pl.ref or "").strip()
        for pl in (grid.cells.get(rid) or {}).values()
        if pl is not None
        and getattr(pl, "kind", None) in VISIBLE_DEVICE_KINDS
        and str(pl.ref or "").strip()
    }


def _last_fil_hub_before(
    plan: SheetPlan,
    seq: list[str],
    before_idx: int,
) -> tuple[str, int]:
    """path 上 before_idx 之前最近的 **滤波/分频** 可竖并汇合件。

    仅 FIL/LPF/BPF：DIP/开关虽可竖并，但是频段私有前级，不能当
    「天线公共干」枢纽（否则 DIP|ANT 残行会冒充公尾）。
    """
    for j in range(before_idx - 1, -1, -1):
        x = seq[j]
        xu = str(x).upper()
        if not should_vertical_merge(plan, x):
            continue
        if xu.startswith(("FIL", "LPF", "BPF")):
            return x, j
    return "", -1


def _fil_hub_vis_on_row(
    grid: SheetGrid,
    rid: str,
    fil_ref: str,
) -> int:
    if not fil_ref:
        return 0
    if fil_ref in _row_visible_refs(grid, rid):
        return 2
    try:
        ridx = list(grid.row_ids or []).index(rid)
    except ValueError:
        return 0
    for m in grid.merges or []:
        if str(getattr(m, "ref", "") or "").strip() != fil_ref:
            continue
        if int(m.row_start) <= ridx <= int(m.row_end):
            return 1
    return 0


def _ant_pred_is_matching(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> int:
    """ANT 的 Route 紧邻前驱是否匹配段（RLCT）。

    滤波/前端紧挨 ANT = 残臂；匹配前驱 = 汇合后仍含匹配的公共后缀。
    """
    seq = _path_seq_on_row(plan, rid)
    rs = str(ref or "").strip()
    if not seq or rs not in seq:
        return 0
    idx = seq.index(rs)
    if idx <= 0:
        return 0
    pred = seq[idx - 1]
    return 1 if is_rlct_ref(pred, device_from_plan(plan, pred)) else 0


def _ant_affinity(
    *,
    matching: int = 0,
    through: int = 0,
    public_tail: tuple[int, int, int] = (0, 0, 0),
    pred_vis: int = 0,
    host_len: int = 0,
) -> tuple:
    """ANT affinity 分数的**唯一构造点**（早退与成功路径共用）。

    形状恒定是硬约束：第三位必须是 `_public_tail_metrics` 那种三元组。
    早退若写成 int，与 stub 的元组比就 `tuple > int` TypeError，手册写不出。
    """
    return (
        int(matching),
        int(through),
        tuple(public_tail),
        int(pred_vis),
        int(host_len),
    )


def _ant_common_trunk_affinity(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> tuple:
    """ANT 叶子：落在主链夹心宿主公干，不是 COPADBLK extras / 私有长行。

    最高位：紧邻 ANT 的 Route 前驱是匹配 RLCT（不是滤波/前端残臂）。
    其后主链夹心宿主 through（extras 中层不是宿主）。
    无 SVM 汇合时 path 更长不得压过「本行匹配还在」。
    多个夹心宿主打平时跟更短公干（私有前级更长的行不是贯通）。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return _ant_affinity()
    seq = _path_seq_on_row(plan, rid)
    if not seq or rs not in seq:
        return _ant_affinity()
    idx = seq.index(rs)
    visible = _row_visible_refs(grid, rid)
    pred_vis = 1 if idx > 0 and seq[idx - 1] in visible else 0
    tail_run = 0
    for x in reversed(seq[: idx + 1]):
        if x in visible:
            tail_run += 1
        else:
            break
    through = _through_trunk_rank(grid, rid, plan, rs)
    host_len = -len(seq) if through >= 2 else int(tail_run)
    pt = _public_tail_metrics(plan, rid, rs)
    _jr, _ow, idxs, _n = _scan_joins_before_ref(plan, rid, rs)
    if not idxs:
        # 无汇合：affinity 不拿整段 path 长当公尾（残腿会压过宿主）
        pt = (0, 0, 0)
    return _ant_affinity(
        matching=_ant_pred_is_matching(plan, rid, rs),
        through=through,
        public_tail=pt,
        pred_vis=pred_vis,
        host_len=host_len,
    )


def _common_ant_tail_stickiness(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> int:
    """位于 FIL 公干与 ANT 之间的公尾件：优先与 FIL/ANT 同在的行。

    防止公尾 RLCT 与 ANT 落到不同残行 → 公尾拆段。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return 0
    if is_ant_ref(rs, device_from_plan(plan, rs)):
        return 0
    seq = _path_seq_on_row(plan, rid)
    if not seq or rs not in seq:
        return 0
    # 找 path 上的 ANT
    ant_i = -1
    for j, x in enumerate(seq):
        if str(x).upper().startswith("ANT"):
            ant_i = j
            break
    if ant_i < 0:
        return 0
    ri = seq.index(rs)
    if not (ri < ant_i):
        return 0
    fil_ref, fil_i = _last_fil_hub_before(plan, seq, ant_i)
    if not fil_ref or fil_i < 0:
        # 无滤波枢纽时，最后汇合点（TRIP 等）之后到 ANT 仍是公尾
        jrefs, _owns, idxs, _n = _scan_joins_before_ref(plan, rid, str(seq[ant_i]))
        if idxs:
            fil_i = int(idxs[-1])
            fil_ref = str(jrefs[-1])
        else:
            return 0
    if not fil_ref or fil_i < 0 or not (fil_i < ri < ant_i):
        return 0
    visible = _row_visible_refs(grid, rid)
    score = 1  # 确认为公尾段成员
    if any(str(x).upper().startswith("ANT") for x in visible):
        score += 3
    score += _fil_hub_vis_on_row(grid, rid, fil_ref)
    if ri > 0 and seq[ri - 1] in visible:
        score += 1
    if ri + 1 < len(seq) and seq[ri + 1] in visible:
        score += 1
    return score


def _visible_path_run_left_of_ref(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> int:
    """从 ref 沿 path 向左，连续仍在本行可见的器件数（含 ref）。

    公共链路内聚：贯通行上匹配…T→ANT 整段仍在 → run 长；
    夹心抽走 FIL 后只剩 ANT → run=1。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return 0
    seq: list[str] = []
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        break
    if not seq or rs not in seq:
        return 0
    visible = _row_visible_refs(grid, rid)
    n = 0
    for x in reversed(seq[: seq.index(rs) + 1]):
        if x in visible:
            n += 1
        else:
            break
    return n


def _after_shared_join_on_path(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> bool:
    """ref 是否在某多主汇合件右侧（公共天线侧尾候选）。"""
    if plan is None or not ref:
        return False
    rs = str(ref).strip()
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        if rs not in seq:
            return False
        idx = seq.index(rs)
        return any(should_vertical_merge(plan, x) for x in seq[:idx])
    return False


def _downstream_merge_affinity(
    plan: Optional[SheetPlan],
    grid: SheetGrid,
    rid: str,
    ref: str,
) -> int:
    """path 上 ref 右侧是否仍挂着本行应服务的合并器件（防公共干拆段）。

    unique 把非共享 RLCT 吸到「含对A / 夹心旁路」行时，主链
    FIL 失去左邻 → L6 并柱只剩 1 支；公共匹配段与 FIL 拆成两截。

    通用规则（无位号）：
      2 = 右侧某 ``should_vertical_merge`` 件仍写在本行
      1 = 本行是该合并件 share_map owner（多格尚未钉顶 / 即将并柱）
      0 = 无下游合并件或不归属本行

    主链（…→C→FIL）得分高于旁路（…→C→腿→私有 SAW）。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return 0
    seq: list[str] = []
    for row in plan.rows or []:
        if row.row_id != rid or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        break
    if not seq or rs not in seq:
        return 0
    idx = seq.index(rs)
    if idx + 1 >= len(seq):
        return 0
    visible = _row_visible_refs(grid, rid)
    share = getattr(plan, "share_map", None) or {}
    best = 0
    for x in seq[idx + 1 :]:
        if not should_vertical_merge(plan, x):
            continue
        if x in visible:
            return 2
        if rid in (share.get(x) or ()):
            best = max(best, 1)
    return best


# 旧名（测试/外部）
def _shared_fil_right_span(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> int:
    return _public_tail_len_after_last_join(plan, rid, ref)


def _after_shared_fil_on_path(
    plan: Optional[SheetPlan],
    rid: str,
    ref: str,
) -> bool:
    return _after_shared_join_on_path(plan, rid, ref)


def _row_keep_score(
    grid: SheetGrid,
    row_index: int,
    rid: str,
    plan: Optional[SheetPlan] = None,
    *,
    ref: str = "",
) -> tuple:
    """unique 保留优先级（高位优先）。

    1. 夹心/结构宿主
    2. **公尾秩** ``_public_tail_metrics``：强多主汇合 + 实质中段公尾优先；
       无汇合/FIL→ANT stub 不得拆走 FIL→…→ANT
    3. path 前驱仍在本行 / 可见 ANT / 靠前行 / 器件数

    结构件（``copad_structure_refs``）时宿主分压过公尾，避免干路挂件被假公尾吸走。
    """
    cells = grid.cells.get(rid) or {}
    n_dev = sum(
        1
        for pl in cells.values()
        if pl.kind in VISIBLE_DEVICE_KINDS
        and str(pl.ref or "").strip()
    )
    has_ant_vis = any(
        str(pl.ref or "").upper().startswith("ANT")
        for pl in cells.values()
        if pl.kind in VISIBLE_DEVICE_KINDS
    )
    has_ant = has_ant_vis or _path_ends_with_ant(plan, rid)
    lab = str((grid.labels or {}).get(rid, "") or "")
    if plan is not None:
        for row in plan.rows or []:
            if row.row_id == rid:
                lab = lab or str(row.label or "")
                break
    rs = str(ref or "").strip()
    struct = getattr(grid, "copad_structure_refs", None) or set()
    if struct:
        copad_host = (
            1
            if rs and rs in struct and _is_copad_sandwich_host(grid, rid)
            else 0
        )
    else:
        copad_host = 1 if _is_copad_sandwich_host(grid, rid) else 0
    # 夹心宿主上的公共尾件（不限 structure_refs）
    tail_host = (
        1
        if (
            _is_copad_sandwich_host(grid, rid)
            and _after_shared_join_on_path(plan, rid, rs)
        )
        else 0
    )
    public_tail = _public_tail_metrics(plan, rid, rs)
    fbrx_link = 0
    if plan is not None:
        for row in plan.rows or []:
            if row.row_id != rid:
                continue
            if not is_fbrx_sample_row(
                label=lab or str(row.label or ""),
                rid=rid,
                devices=getattr(row, "devices", None),
                require_no_ant=True,
                max_devs=8,
                source="label",
            ):
                break
            refs = {
                str(getattr(d, "ref", "") or "").strip()
                for d in (row.devices or [])
            }
            if rs not in refs:
                break
            nd = len(getattr(row, "devices", None) or [])
            longer_host = False
            for row2 in plan.rows or []:
                if row2.row_id == rid:
                    continue
                if is_fbrx_sample_row(
                    label=str(getattr(row2, "label", "") or ""),
                    rid=row2.row_id,
                    require_no_ant=False,
                    max_devs=None,
                    source="label",
                ):
                    continue
                refs2 = {
                    str(getattr(d, "ref", "") or "").strip()
                    for d in (row2.devices or [])
                }
                if rs in refs2 and len(row2.devices or []) > nd:
                    longer_host = True
                    break
            if not longer_host:
                fbrx_link = 1
            break
    continuity = _path_pred_visible(plan, grid, rid, rs)
    join_vis = _join_visible_on_row(plan, grid, rid, rs)
    ant_rank = 2 if has_ant_vis else (1 if has_ant else 0)
    host_rank = max(copad_host, tail_host)
    richer = n_dev
    # unique 优先级（高位优先）：
    #
    # **ANT**：ant_trunk（匹配前驱 → 主链夹心宿主 → 可见前驱 →
    #   宿主间更短公干 / 非宿主可见 run）→ continuity → 可见 run → through
    #   → 才是 join_vis。禁止 FIL 柱顶字、extras 中层独占、私有长行抢天线。
    #
    # **非 ANT 公尾中段**：先跟 ANT（ant_tail）和贯通行，再 join_vis。
    #   禁止 FIL 柱顶字行把公尾钉到私有频段行。
    through = _through_trunk_rank(grid, rid, plan, rs)
    if is_ant_ref(rs, device_from_plan(plan, rs) if plan is not None else None):
        tail_run = _visible_path_run_left_of_ref(plan, grid, rid, rs)
        ant_trunk = _ant_common_trunk_affinity(plan, grid, rid, rs)
        return (
            ant_trunk,
            int(continuity),
            int(tail_run),
            int(through),
            join_vis,
            host_rank,
            richer,
            public_tail,
            fbrx_link,
            ant_rank,
            -int(row_index),
            n_dev,
        )
    ds_merge = _downstream_merge_affinity(plan, grid, rid, rs)
    ant_tail = _common_ant_tail_stickiness(plan, grid, rid, rs)
    # 公尾跟 ANT 行；非公尾仍比 join_vis。两套必须同构，否则
    # 一行有 ant_tail、一行没有时 max() TypeError，手册写不出。
    return (
        1 if ant_tail else 0,
        public_tail if ant_tail else (0, 0, 0),
        int(through) if ant_tail else 0,
        int(ant_tail),
        join_vis,
        ds_merge,
        host_rank,
        richer,
        fbrx_link,
        continuity,
        ant_rank,
        -int(row_index),
        n_dev,
    )


def _copad_structure_keep_locs(
    grid: SheetGrid,
    locs: list[tuple[int, str, int, PlacedCell]],
    ref: str,
) -> list[tuple[int, str, int, PlacedCell]]:
    """COPADBLK 独占只给夹心结构件（黄腿 / structure_refs）。

    TRIP 等公共汇合件若落 extras，独占会把并柱缩到汉堡中层，
    天线公干和其它 owner 变成另一棵树。
    """
    struct = getattr(grid, "copad_structure_refs", None) or set()
    rs = str(ref or "").strip()
    out: list[tuple[int, str, int, PlacedCell]] = []
    for x in locs:
        if "COPADBLK" not in str(x[1] or ""):
            continue
        pl = x[3]
        kind = str(getattr(pl, "kind", "") or "")
        pref = str(getattr(pl, "ref", "") or "").strip() or rs
        if kind in ("copad_leg", "copad_tag") or pref in struct:
            out.append(x)
    return out


def _keep_one_location(
    grid: SheetGrid,
    locs: list[tuple[int, str, int, PlacedCell]],
    plan: Optional[SheetPlan] = None,
    *,
    ref: str = "",
) -> None:
    if len(locs) <= 1:
        return
    # 行分优先于列多数（宿主 ANT 列号常与旁路不同，禁止先按列过滤）
    col_cnt = Counter(c for _i, _r, c, _p in locs)
    rs = str(ref or "").strip()
    if not rs and locs:
        rs = str(getattr(locs[0][3], "ref", "") or "").strip()
    _i, keep_rid, keep_col, keep_cell = max(
        locs,
        key=lambda x: (
            _row_keep_score(grid, x[0], x[1], plan, ref=rs),
            col_cnt[x[2]],
            x[2],
        ),
    )
    for _j, rid, col, _p in locs:
        if rid == keep_rid and col == keep_col:
            continue
        cells = grid.cells.get(rid)
        if cells is not None and col in cells:
            del cells[col]
    grid.cells.setdefault(keep_rid, {})[keep_col] = keep_cell


def _locs_all_fbrx_sample(
    grid: SheetGrid,
    locs: list[tuple[int, str, int, PlacedCell]],
) -> bool:
    """落点是否**全部**在 FBRX 取样行（无主链/其它页）。

    多条 FBRX 取样链常共享同一开关/耦合器；若 unique 成只留一行，
    另一 Coupler 下的 FBRX 行会被抽成空壳。
    仅 FBRX↔FBRX 的多份允许各留完整取样链。
    """
    if not locs:
        return False
    return all(_is_fbrx_sample_row(grid, rid) for _i, rid, _c, _p in locs)


def _ref_is_public_ant_suffix(plan: Optional[SheetPlan], ref: str) -> bool:
    """是否落在某条 path 的 FIL 公干与 ANT 之间（含 ANT）。

    夹心插入前不要 unique 这段：当时还没有宿主，FIL 柱顶字行会把
    C…T→ANT 钉在第一频段行。夹心后再 unique，才能跟贯通行。
    """
    rs = str(ref or "").strip()
    if not rs or plan is None:
        return False
    if is_ant_ref(rs, device_from_plan(plan, rs)):
        return True
    for row in plan.rows or []:
        if row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        if rs not in seq:
            continue
        ant_i = next(
            (j for j, x in enumerate(seq) if str(x).upper().startswith("ANT")),
            -1,
        )
        if ant_i < 0:
            continue
        fil_ref, fil_i = _last_fil_hub_before(plan, seq, ant_i)
        ri = seq.index(rs)
        if fil_ref and fil_i >= 0 and fil_i < ri <= ant_i:
            return True
    return False


def enforce_non_shared_single_cell(
    plan: SheetPlan,
    grid: SheetGrid,
    *,
    defer_public_suffix: bool = False,
) -> SheetGrid:
    """非布局共享 / RLCT / ANT：全表只留一格。L3 后立刻执行。

    布局共享件保留多格供 L6 竖并；非共享一律单格。
    **例外**：落点全在 FBRX 取样行 → 各 FBRX 行保留完整取样链（见 _locs_all_fbrx_sample）。
    ``defer_public_suffix``：FIL 后公尾 + ANT 留给夹心后再 unique。
    """
    lnc = plan.left_neighbor_count or {}

    def _pass(*, ant_only: bool) -> None:
        for ref, locs in collect_device_locations(grid).items():
            is_ant = is_ant_ref(
                ref, device_from_plan(plan, ref) if plan is not None else None
            )
            if is_ant != ant_only:
                continue
            if len(locs) <= 1:
                continue
            if _locs_all_fbrx_sample(grid, locs):
                continue
            # 合并器件保留多格供竖并；其余全表单格
            if should_vertical_merge(plan, ref, lnc):
                continue
            if defer_public_suffix and _ref_is_public_ant_suffix(plan, ref):
                continue
            _keep_one_location(grid, locs, plan, ref=ref)

    _pass(ant_only=True)
    _pass(ant_only=False)
    return grid


def _cell_template_from_locs(
    locs: list[tuple[int, str, int, PlacedCell]],
    *,
    prefer_col: int | None = None,
) -> PlacedCell:
    """从已有落点取样式模板。"""
    if prefer_col is not None:
        same = [x for x in locs if x[2] == prefer_col]
        if same:
            src = same[0][3]
            return PlacedCell(
                ref=src.ref,
                device=src.device,
                kind=src.kind or "device",
            )
    src = locs[0][3]
    return PlacedCell(
        ref=src.ref,
        device=src.device,
        kind=src.kind or "device",
    )


def _locs_are_pa_lna(
    locs: list[tuple[int, str, int, PlacedCell]],
) -> bool:
    """是否前端 PA/LNA（不含 TRX/SDR）。段外保留仅对此类生效。"""
    for _i, _rid, _c, pl in locs:
        dev = getattr(pl, "device", None)
        if dev is not None and is_pa_lna_device(dev):
            return True
    return False


def _keep_merge_segments_locations(
    grid: SheetGrid,
    ref: str,
    locs: list[tuple[int, str, int, PlacedCell]],
    segments: list[MergeRange],
    plan: Optional[SheetPlan] = None,
) -> None:
    """竖合并位号：每段保留跨度，值钉在 row_start；清段外孤儿。

    - **PA/LNA**：段外 owner 可保留
    - **其它**：段外一律清（禁孤儿 FIL 被灰格拉假链）
    - path-owner：禁止把合并顶收到 COPAD 行
    """
    _ = plan
    if not locs or not segments:
        return

    keep_keys: set[tuple[str, int]] = set()
    place: list[tuple[str, int, PlacedCell]] = []
    new_segs: list[MergeRange] = []
    covered: set[tuple[int, int]] = set()
    # PA/LNA：段外 owner 可保留；但若段内已含 FBRX 取样行，禁止再在段外留第二份
    # （否则取样行与主链双值触发写表 unique 断言）
    keep_outside = _locs_are_pa_lna(locs)
    seg_has_fbrx = any(
        "FBRX" in str(getattr(m, "row_ids", None) or ())
        or any(
            0 <= i < len(grid.row_ids) and "FBRX" in str(grid.row_ids[i]).upper()
            for i in range(int(m.row_start), int(m.row_end) + 1)
        )
        for m in segments
    )
    if seg_has_fbrx:
        keep_outside = False

    for merge in segments:
        mcol = int(merge.col)
        for ri in range(int(merge.row_start), int(merge.row_end) + 1):
            covered.add((ri, mcol))
        seg_locs = [
            x for x in locs if int(merge.row_start) <= x[0] <= int(merge.row_end)
        ]
        copad_locs = _copad_structure_keep_locs(grid, seg_locs, ref)
        if copad_locs:
            same_col = [x for x in copad_locs if x[2] == mcol]
            pool = same_col or copad_locs
            yellow = [
                x for x in pool
                if str(x[1] or "").endswith("|b1")
                or str(x[1] or "").endswith("|b2")
            ]
            keep_i, keep_rid, keep_col, keep_cell = (yellow or pool)[0]
            # 结构 IC 字留黄腿：并柱顶是黄腿，不得钉到夹心宿主。
            new_start = int(keep_i)
            new_end = max(int(merge.row_end), keep_i)
            dest_col = mcol if yellow else keep_col
            if new_end <= new_start:
                # 单格：值留 COPAD
                keep_keys.add((keep_rid, dest_col))
                place.append((keep_rid, dest_col, keep_cell))
                continue
            keep_keys.add((keep_rid, dest_col))
            place.append((keep_rid, dest_col, keep_cell))
            new_segs.append(
                MergeRange(
                    ref=ref,
                    col=mcol,
                    row_start=new_start,
                    row_end=new_end,
                    row_ids=(),
                )
            )
            continue

        target_i = int(merge.row_start)
        target_col = mcol
        if not (0 <= target_i < len(grid.row_ids)):
            new_segs.append(merge)
            continue
        # 公共汇合件钉顶在主链，不钉 extras 中层
        while (
            target_i <= int(merge.row_end)
            and target_i < len(grid.row_ids)
            and _is_copad_block_row(grid.row_ids[target_i])
        ):
            target_i += 1
        if target_i > int(merge.row_end) or target_i >= len(grid.row_ids):
            target_i = int(merge.row_start)
        target_rid = grid.row_ids[target_i]
        keep_cell: Optional[PlacedCell] = None
        for i, _rid, c, pl in seg_locs or locs:
            if i == target_i and c == target_col:
                keep_cell = pl
                break
        if keep_cell is None:
            pool = seg_locs or locs
            keep_cell = _cell_template_from_locs(pool, prefer_col=target_col)
        keep_keys.add((target_rid, target_col))
        place.append((target_rid, target_col, keep_cell))
        new_segs.append(merge)

    for _i, rid, col, _p in locs:
        if (rid, col) in keep_keys:
            continue
        if keep_outside and (_i, int(col)) not in covered:
            continue
        cells = grid.cells.get(rid)
        if cells is not None and col in cells and cells[col].ref == ref:
            del cells[col]
    for rid, col, cell in place:
        grid.cells.setdefault(rid, {})[col] = cell

    others = [m for m in (grid.merges or []) if str(m.ref or "") != ref]
    grid.merges = others + new_segs
    # 注意：此处不 normalize 全表——会毁掉尚未处理的其它 ref 的段锚 merge


def finalize_visible_unique_after_merge(
    plan: SheetPlan,
    grid: SheetGrid,
) -> SheetGrid:
    """L6 后：有合并按段处理；无合并时结构黄腿优先、否则 keep_score；最后钉顶。"""
    merges_by_ref: dict[str, list[MergeRange]] = defaultdict(list)
    for m in grid.merges or []:
        ref = str(m.ref or "").strip()
        if ref:
            merges_by_ref[ref].append(m)

    # 禁止「FBRX 取样行抢占主链 U*」——会拆掉主链开关并柱，并连带破坏 FIL 柱。
    # 取样短行与主链共享 U* 时走普通 merge 段钉顶（值在段顶=主链宿主）。

    def _pass(*, ant_only: bool) -> None:
        for ref, locs in collect_device_locations(grid).items():
            ant = is_ant_ref(
                ref, device_from_plan(plan, ref) if plan is not None else None
            )
            if ant != ant_only:
                continue
            segs = merges_by_ref.get(ref) or []
            if segs:
                if len(segs) == 1 and len(locs) <= 1:
                    continue
                _keep_merge_segments_locations(grid, ref, locs, segs, plan=plan)
                continue
            if len(locs) <= 1:
                continue
            copad_locs = _copad_structure_keep_locs(grid, locs, ref)
            if copad_locs and not ant:
                # 结构腿/中段：黄腿独占显示（Co-pad 双支路不变量）
                # 不得用宿主 score 抽空 b1/b2（黄腿被吸到贯通行 → 上支路空壳）
                # 天线叶不是结构件：extras 中层若抢走 ANT，宿主公尾变断叶。
                copad_locs.sort(
                    key=lambda x: _row_keep_score(
                        grid, x[0], x[1], plan, ref=ref
                    ),
                    reverse=True,
                )
                _i, keep_rid, keep_col, keep_cell = copad_locs[0]
                for _j, rid, col, _p in locs:
                    if rid == keep_rid and col == keep_col:
                        continue
                    cells = grid.cells.get(rid)
                    if cells is not None and col in cells and cells[col].ref == ref:
                        del cells[col]
                grid.cells.setdefault(keep_rid, {})[keep_col] = keep_cell
            elif _locs_all_fbrx_sample(grid, locs):
                # 仅 FBRX 取样行互抢：各留一份（取样行不得互抽空）
                continue
            else:
                _keep_one_location(grid, locs, plan, ref=ref)

    _pass(ant_only=True)
    _pass(ant_only=False)
    collapse_cross_column_copies(grid, plan)
    # 全表只钉一次：path-owner 值归顶，跨度不缩
    grid.merges = normalize_merges_pin_top(grid)
    return grid


def collapse_cross_column_copies(
    grid: SheetGrid,
    plan: Optional[SheetPlan],
) -> None:
    """不同列的第二份不是合法切断（切断 = 同列两段）。

    写表断言：同位号不得跨列。FBRX 取样行互抢除外。
    赢列按 keep_score，该列上的格（含并柱体）全留。
    """
    for ref, locs in collect_device_locations(grid).items():
        if len(locs) <= 1:
            continue
        if _locs_all_fbrx_sample(grid, locs):
            continue
        cols = {int(c) for _i, _r, c, _p in locs}
        if len(cols) <= 1:
            continue
        rs = str(ref or "").strip()

        def _col_key(col: int) -> tuple:
            clocs = [x for x in locs if int(x[2]) == col]
            best = max(
                clocs,
                key=lambda x: _row_keep_score(grid, x[0], x[1], plan, ref=rs),
            )
            return (
                _row_keep_score(grid, best[0], best[1], plan, ref=rs),
                len(clocs),
                col,
            )

        win = max(cols, key=_col_key)
        for _i, rid, col, _p in locs:
            if int(col) == win:
                continue
            cells = grid.cells.get(rid)
            if (
                cells is not None
                and col in cells
                and str(cells[col].ref or "").strip() == rs
            ):
                del cells[col]
        if grid.merges:
            grid.merges = [
                m
                for m in grid.merges
                if str(m.ref or "").strip() != rs or int(m.col) == win
            ]


def _row_has_visible_device(grid: SheetGrid, rid: str) -> bool:
    """行内是否仍有器件 / Co-pad / 侧支 / 结构标签实体（label/gray 不算）。"""
    for pl in (grid.cells.get(rid) or {}).values():
        if pl.kind in UNIQUE_BEARING_KINDS and str(pl.ref or "").strip():
            return True
    return False


def _is_copad_block_row(rid: str) -> bool:
    return "COPADBLK" in str(rid or "")


def _is_sandwich_host_row(grid: SheetGrid, idx: int) -> bool:
    """夹心宿主：上一行是 b1 或下一行是 b2（结构 ref 抽走后宿主可能暂时无器件）。"""
    ids = list(grid.row_ids or [])
    if idx < 0 or idx >= len(ids):
        return False
    if _is_copad_block_row(ids[idx]):
        return False
    prev_ok = (
        idx > 0
        and _is_copad_block_row(ids[idx - 1])
        and str(ids[idx - 1]).endswith("|b1")
    )
    next_ok = (
        idx + 1 < len(ids)
        and _is_copad_block_row(ids[idx + 1])
        and str(ids[idx + 1]).endswith("|b2")
    )
    return prev_ok or next_ok


def _is_fbrx_sample_row(grid: SheetGrid, rid: str) -> bool:
    """FBRX 取样短行：允许 unique 后无字仍占并柱，保取样链视觉。"""
    return is_fbrx_sample_row(
        label=str((grid.labels or {}).get(rid, "") or ""),
        rid=rid,
        require_no_ant=False,
        max_devs=None,
        source="label_or_rid",
    )


def restore_fbrx_sample_chain_devices(
    plan: SheetPlan,
    grid: SheetGrid,
) -> SheetGrid:
    """取样短行：恢复完整取样链 + 与主链共享的开关/耦合器并柱钉顶。

    1) 取样 path 上**全部**器件对齐到已有列（含匹配 RLCT）
    2) 与**主链**共享的开关/耦合器：合成竖并，钉顶只留一个字
    3) **仅 FBRX↔FBRX** 共享的取样链件：各 FBRX 行保留字，不并抽空
    4) 取样行靠灰格接到并柱列（fill_chain 认 merge entity）
    5) 回写抄它行列若打乱本行 path 序：只按显示序重排这一行
       （行首 IC 在匹配 RLCT 左；已单调的对照短行不重排）
    """
    id_to_row = {r.row_id: r for r in (plan.rows or [])}
    id_to_idx = {rid: i for i, rid in enumerate(grid.row_ids or [])}
    lnc = plan.left_neighbor_count or {}

    ref_col: dict[str, int] = {}
    ref_template: dict[str, PlacedCell] = {}
    for rid in grid.row_ids or []:
        for c, pl in (grid.cells.get(rid) or {}).items():
            if int(c) < 2 or pl is None:
                continue
            if pl.kind not in VISIBLE_DEVICE_KINDS:
                continue
            ref = str(pl.ref or "").strip()
            if not ref:
                continue
            if ref not in ref_col or not _is_fbrx_sample_row(grid, rid):
                ref_col[ref] = int(c)
            ref_template.setdefault(
                ref,
                PlacedCell(ref=pl.ref, device=pl.device, kind=pl.kind or "device"),
            )
    # plan 补模板（本行 unique 抽空时仍能回写）
    for r2 in plan.rows or []:
        for d in r2.devices or []:
            ref = str(getattr(d, "ref", "") or "").strip()
            if ref and ref not in ref_template:
                ref_template[ref] = PlacedCell(ref=ref, device=d, kind="device")

    placed_shared: set[str] = set()
    for rid in list(grid.row_ids or []):
        if not _is_fbrx_sample_row(grid, rid):
            continue
        row = id_to_row.get(rid)
        if row is None or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        # 完整取样链（含匹配 RLCT），不限 U*
        want = list(seq)
        cells = grid.cells.setdefault(rid, {})
        have = {
            str(pl.ref or "").strip()
            for pl in cells.values()
            if pl and str(pl.ref or "").strip()
        }
        for ref in want:
            if ref in have:
                continue
            col = ref_col.get(ref)
            tmpl = ref_template.get(ref)
            if col is None or tmpl is None:
                continue
            occ = cells.get(col)
            if (
                occ is not None
                and str(occ.ref or "").strip()
                and str(occ.ref or "").strip() != ref
            ):
                continue
            cells[col] = PlacedCell(
                ref=tmpl.ref,
                device=tmpl.device,
                kind=tmpl.kind or "device",
            )
            have.add(ref)
            # 仅「与主链共享」的开关/耦合器进并柱钉顶；纯 FBRX 链可双显
            if is_switch_or_coupler_device(
                tmpl.device or device_from_plan(plan, ref)
            ):
                locs_now = [
                    (i, r, c, pl)
                    for i, r in enumerate(grid.row_ids or [])
                    for c, pl in (grid.cells.get(r) or {}).items()
                    if str(getattr(pl, "ref", "") or "") == ref
                ]
                if not _locs_all_fbrx_sample(grid, locs_now):
                    placed_shared.add(ref)
            grid.max_col = max(int(grid.max_col or 1), int(col))
        # 与主链共享的开关/耦合器：取样只留主链列（同列另段），清掉 path 列上的第二份
        for ref in list(have):
            if not is_switch_or_coupler_device(device_from_plan(plan, ref)):
                continue
            if not should_vertical_merge(plan, ref, lnc):
                continue
            want_col = ref_col.get(ref)
            if want_col is None or int(want_col) < 2:
                continue
            off = [
                c
                for c, pl in list(cells.items())
                if pl is not None
                and str(pl.ref or "").strip() == ref
                and int(c) != int(want_col)
            ]
            if not off:
                continue
            tmpl = ref_template.get(ref)
            occ = cells.get(int(want_col))
            if tmpl is not None and (
                occ is None
                or not str(getattr(occ, "ref", "") or "").strip()
                or str(occ.ref or "").strip() == ref
            ):
                cells[int(want_col)] = PlacedCell(
                    ref=tmpl.ref,
                    device=tmpl.device,
                    kind=tmpl.kind or "device",
                )
            for c in off:
                old = cells.get(c)
                if old is not None and str(old.ref or "").strip() == ref:
                    del cells[c]
        # 抄它行列若打乱本行 path 序：只重排这一行。
        # 已单调的对照短行不重排（短链右贴会把行首 IC 拽到远端 FE）。
        last_col = -1
        inverted = False
        for ref in seq:
            hit = next(
                (
                    int(c)
                    for c, pl in cells.items()
                    if int(c) >= 2
                    and pl is not None
                    and str(pl.ref or "").strip() == ref
                ),
                None,
            )
            if hit is None:
                continue
            if hit <= last_col:
                inverted = True
                break
            last_col = hit
        if inverted:
            by_ref: dict[str, PlacedCell] = {}
            label = cells.get(1)
            for c, pl in cells.items():
                if int(c) < 2 or pl is None:
                    continue
                if pl.kind not in VISIBLE_DEVICE_KINDS:
                    continue
                rs = str(pl.ref or "").strip()
                if rs:
                    by_ref[rs] = pl
            fi = id_to_idx.get(rid)
            multi_col: dict[str, int] = {}
            if fi is not None:
                for m in grid.merges or []:
                    if int(m.row_end) <= int(m.row_start):
                        continue
                    if int(m.row_start) <= fi <= int(m.row_end):
                        rs = str(m.ref or "").strip()
                        if rs:
                            multi_col[rs] = int(m.col)
                # 换左邻取样不在主链柱内：共享 U* 仍锁主链列（同列另段）
                for rs, c0 in ref_col.items():
                    if rs in by_ref and should_vertical_merge(plan, rs, lnc):
                        multi_col.setdefault(rs, int(c0))
            new_map = pack_row_by_path(
                seq,
                by_ref,
                label,
                set(),
                multi_col,
                band=str(getattr(row.path, "band", "") or ""),
            )
            grid.cells[rid] = new_map
            if new_map:
                grid.max_col = max(int(grid.max_col or 1), max(new_map.keys()))

    # 同列多行 → 竖并钉顶（只保留一个值）
    # 中间行只要该列无「其它器件」即可并入跨度（unique 后开关空列可夹在主链与取样行之间）
    locs_all = collect_device_locations(grid)
    kept_other = [
        m
        for m in (grid.merges or [])
        if str(m.ref or "").strip() not in placed_shared
    ]
    # 已有并柱已盖住取样行的共享 U*：保留，勿拆后再双值
    covered_ok: set[str] = set()
    for m in grid.merges or []:
        ref = str(m.ref or "").strip()
        if ref not in placed_shared:
            continue
        if int(m.row_end) <= int(m.row_start):
            continue
        for rid in grid.row_ids or []:
            if not _is_fbrx_sample_row(grid, rid):
                continue
            fi = id_to_idx.get(rid)
            if fi is not None and int(m.row_start) <= fi <= int(m.row_end):
                covered_ok.add(ref)
                kept_other.append(m)
                # 清掉取样行双值，只留钉顶
                for ri in range(int(m.row_start) + 1, int(m.row_end) + 1):
                    rid2 = grid.row_ids[ri]
                    cells = grid.cells.get(rid2) or {}
                    old = cells.get(int(m.col))
                    if old is not None and str(old.ref or "").strip() == ref:
                        del cells[int(m.col)]
                break

    new_merges: list[MergeRange] = list(kept_other)
    for ref in sorted(placed_shared):
        if ref in covered_ok:
            continue
        if not should_vertical_merge(plan, ref, lnc):
            locs = locs_all.get(ref) or []
            if len(locs) <= 1:
                continue
            non_f = [x for x in locs if not _is_fbrx_sample_row(grid, x[1])]
            keep = (non_f or locs)[0]
            for _i, rid, c, _p in locs:
                if rid == keep[1] and c == keep[2]:
                    continue
                cells = grid.cells.get(rid)
                if cells is not None and c in cells and cells[c].ref == ref:
                    del cells[c]
            continue
        col = int(ref_col.get(ref, 0) or 0)
        if col < 2:
            continue
        locs = [x for x in (locs_all.get(ref) or []) if int(x[2]) == col]
        # 回写后重新收集
        locs = [
            (i, rid, c, pl)
            for i, rid in enumerate(grid.row_ids or [])
            for c, pl in (grid.cells.get(rid) or {}).items()
            if int(c) == col
            and pl is not None
            and str(pl.ref or "").strip() == ref
        ]
        if len(locs) < 2:
            continue
        idxs = sorted({x[0] for x in locs})
        # 禁止「中间空列就整段并」——会把非 owner 主链夹进开关并柱。
        # 只并 owner 连续段，中间仅允许 gap / JUMP / 条件 FBRX 透明，
        # 或 **share_map 同 ref 的空 owner 行**
        # （unique 后空列仍属该树，须进并柱，否则取样行无法与主链宿主连续
        #  → 回写被删 → 看起来 FBRX 只挂在 FIL 空穿列上）。
        share_owners = set((plan.share_map or {}).get(ref) or ())
        share_owner_idxs = {
            id_to_idx[rid]
            for rid in share_owners
            if rid in id_to_idx
        }
        owner_idx_set = set(idxs) | share_owner_idxs
        tmpl = ref_template[ref]

        def _path_left_visible(idx: int) -> bool:
            if not (0 <= idx < len(grid.row_ids)):
                return False
            rid = grid.row_ids[idx]
            return path_left_visible(grid, rid, id_to_row.get(rid), ref)

        def _gap_ok(j: int) -> bool:
            if j in owner_idx_set:
                return True
            rid_j = grid.row_ids[j]
            if is_merge_passthrough_row_id(
                rid_j, ref=ref, plan=plan, id_to_row=id_to_row,
            ):
                return True
            # JUMP 非本 ref 透明时：即使合并列空也不并进（防公共柱盖私有 stub）
            if is_jump_band_row_id(rid_j):
                return False
            # 空列非透明主链 → 切断
            pl = (grid.cells.get(rid_j) or {}).get(col)
            if pl is None or getattr(pl, "kind", None) == "gray":
                return False
            rref = str(pl.ref or "").strip()
            return not rref or rref == ref

        # 锚点 = 实际落格 + share_map owner（空格也并进柱）
        anchors = sorted(owner_idx_set)
        if len(anchors) < 2:
            anchors = idxs
        runs: list[tuple[int, int]] = []
        live_anchors = [
            i for i in anchors
            if _path_left_visible(i)
            and not _is_fbrx_sample_row(grid, grid.row_ids[i])
        ]
        first_live = live_anchors[0] if live_anchors else None
        last_live = live_anchors[-1] if live_anchors else None
        is_sw_cpl = is_switch_or_coupler_device(device_from_plan(plan, ref))

        span_lo = span_hi = None
        if first_live is not None and last_live is not None:
            span_lo, span_hi = first_live, last_live
            if is_sw_cpl:
                span_lo, span_hi = extend_span_to_adjacent_fbrx(
                    span_lo, span_hi, len(grid.row_ids or []),
                    is_owner=lambda i: i in owner_idx_set,
                    is_fbrx=lambda i: _is_fbrx_sample_row(grid, grid.row_ids[i]),
                    is_live=_path_left_visible,
                    blocked=lambda i: i not in owner_idx_set and not _gap_ok(i),
                )
        lo = prev = anchors[0]
        for i in anchors[1:]:
            # 跨越真连接上沿：所有合并件（v31）。开关/耦合器上沿含相邻取样。
            cut_lo = span_lo if (is_sw_cpl and span_lo is not None) else first_live
            cross_top = cut_lo is not None and prev < cut_lo <= i
            cross_bot = (
                is_sw_cpl
                and span_hi is not None
                and prev <= span_hi < i
            )
            if (not cross_top) and (not cross_bot) and all(_gap_ok(j) for j in range(prev + 1, i)):
                prev = i
                continue
            runs.append((lo, prev))
            lo = prev = i
        runs.append((lo, prev))

        merged_any = False
        for lo, hi in runs:
            if hi <= lo:
                continue
            # 段内至少一端有实际落格（FBRX 回写或主链残留）
            if not any(lo <= i <= hi for i in idxs):
                continue
            # 中间列不得有其它器件
            blocked = False
            for j in range(lo, hi + 1):
                rid_j = grid.row_ids[j]
                pl = (grid.cells.get(rid_j) or {}).get(col)
                if pl is None or getattr(pl, "kind", None) == "gray":
                    continue
                rref = str(pl.ref or "").strip()
                if rref and rref != ref:
                    blocked = True
                    break
            if blocked:
                continue
            for ri in range(lo, hi + 1):
                rid = grid.row_ids[ri]
                cells = grid.cells.setdefault(rid, {})
                if ri == lo:
                    cells[col] = PlacedCell(
                        ref=tmpl.ref,
                        device=tmpl.device,
                        kind=tmpl.kind or "device",
                    )
                else:
                    old = cells.get(col)
                    if old is not None and str(old.ref or "").strip() == ref:
                        del cells[col]
            new_merges.append(
                MergeRange(
                    ref=ref,
                    col=col,
                    row_start=lo,
                    row_end=hi,
                    row_ids=tuple(grid.row_ids[r] for r in range(lo, hi + 1)),
                )
            )
            merged_any = True
        if not merged_any:
            # 无法并：去掉取样行双值，保主链
            for i, rid, c, _p in locs:
                if _is_fbrx_sample_row(grid, rid):
                    cells = grid.cells.get(rid)
                    if cells is not None and c in cells:
                        del cells[c]
            continue
    grid.merges = new_merges
    grid.merges = normalize_merges_pin_top(grid)

    # 取样链最左开关/耦合器：并柱钉顶改到 FBRX，保证取样行看得见 FE。
    # 终点宿主不钉顶：须留在主链行以便与右邻横连；
    # 取样行靠并柱覆盖 + 灰格接到宿主列（见 fill_chain_continuity FBRX 例外）。
    for rid in list(grid.row_ids or []):
        if not _is_fbrx_sample_row(grid, rid):
            continue
        row = id_to_row.get(rid)
        if row is None or row.path is None:
            continue
        seq = [str(x) for x in display_ref_seq(row.path) if x]
        first_fe = next(
            (
                x
                for x in seq
                if is_switch_or_coupler_device(device_from_plan(plan, x))
            ),
            "",
        )
        if not first_fe:
            continue
        fi = id_to_idx.get(rid)
        if fi is None:
            continue
        for mi, m in enumerate(list(grid.merges or [])):
            if str(m.ref or "").strip() != first_fe:
                continue
            if not (int(m.row_start) <= fi <= int(m.row_end)):
                continue
            if int(m.row_start) == fi:
                continue
            # 仅当取样行在并柱上沿附近（之上或即顶）才钉顶；
            # 取样在块底时钉顶会把 Excel 字挪离主链横连。
            if fi > int(m.row_start):
                continue
            col = int(m.col)
            tmpl = ref_template.get(first_fe)
            if tmpl is None:
                continue
            for ri in range(int(m.row_start), int(m.row_end) + 1):
                rid2 = grid.row_ids[ri]
                cells = grid.cells.get(rid2) or {}
                old = cells.get(col)
                if old is not None and str(old.ref or "").strip() == first_fe:
                    del cells[col]
            grid.cells.setdefault(rid, {})[col] = PlacedCell(
                ref=tmpl.ref,
                device=tmpl.device,
                kind=tmpl.kind or "device",
            )
            grid.merges[mi] = MergeRange(
                ref=first_fe,
                col=col,
                row_start=fi,
                row_end=max(int(m.row_end), fi),
                row_ids=(),
            )
    grid.merges = normalize_merges_pin_top(grid)
    return grid


def _row_device_refs_on_grid(grid: SheetGrid, rid: str) -> list[str]:
    out: list[str] = []
    for pl in (grid.cells.get(rid) or {}).values():
        if pl is None or pl.kind not in VISIBLE_DEVICE_KINDS:
            continue
        r = str(pl.ref or "").strip()
        if r:
            out.append(r)
    return out


def _is_shared_residue_shell(
    plan: SheetPlan,
    grid: SheetGrid,
    rid: str,
    idx: int,
) -> bool:
    """主链行 unique 后只剩「别处已有」的布局共享件 → 假链路壳。"""
    if _is_fbrx_sample_row(grid, rid):
        return False
    if _is_copad_block_row(rid):
        return False
    if _is_sandwich_host_row(grid, idx):
        return False
    refs = _row_device_refs_on_grid(grid, rid)
    if not refs:
        return True
    lnc = plan.left_neighbor_count or {}
    locs = collect_device_locations(grid)
    for ref in refs:
        if not should_vertical_merge(plan, ref, lnc):
            return False
        others = [
            x for x in (locs.get(ref) or []) if x[1] != rid
        ]
        if not others:
            return False
    return True


def drop_empty_content_rows(
    grid: SheetGrid,
    plan: Optional[SheetPlan] = None,
) -> SheetGrid:
    """unique 后删除仅剩标签/灰格的空壳行，并收缩竖合并跨度。

    典型场景：GPS 短 stub 路径器件被抽到长链后只剩 A 列标签，
    打断开关树状竖并观感。对所有 Sheet 通用。
    不删 Co-pad 支路行 / 夹心宿主，避免腿行悬空。

    **仅 FBRX 取样行**可在「无器件但落在并柱跨度内」时保留：
      保 SDR—开关取样链视觉。
    **普通主链空壳**（器件全被吸走后只剩 A 列）必须删。
    **共享残留壳**：右侧只剩别处已有的布局共享件也删。
    """
    old_ids = list(grid.row_ids or [])
    if len(old_ids) <= 1:
        return grid

    merge_cover: set[int] = set()
    for m in grid.merges or []:
        if int(m.row_end) <= int(m.row_start):
            continue
        for i in range(int(m.row_start), int(m.row_end) + 1):
            if 0 <= i < len(old_ids):
                merge_cover.add(i)

    keep_old: list[int] = []
    for i, rid in enumerate(old_ids):
        if "|__gap__" in str(rid):
            # 空行占位留给 insert_link_gaps；本阶段不应已有 gap
            keep_old.append(i)
            continue
        if _is_copad_block_row(rid) or _is_sandwich_host_row(grid, i):
            keep_old.append(i)
            continue
        # 共享残留壳删除
        if plan is not None and _is_shared_residue_shell(plan, grid, rid, i):
            continue
        if _row_has_visible_device(grid, rid):
            keep_old.append(i)
            continue
        # 无器件：仅 FBRX 取样空壳可因并柱占位保留
        if i in merge_cover and _is_fbrx_sample_row(grid, rid):
            keep_old.append(i)

    if len(keep_old) == len(old_ids):
        return grid
    if not keep_old:
        # 全空则保留首行，避免 sheet 塌成 0 行
        keep_old = [0]

    old_to_new = {old_i: new_i for new_i, old_i in enumerate(keep_old)}
    new_ids = [old_ids[i] for i in keep_old]
    new_cells = {rid: dict(grid.cells.get(rid) or {}) for rid in new_ids}
    new_labels = {
        rid: (grid.labels or {}).get(rid, "") for rid in new_ids
    }

    new_merges: list[MergeRange] = []
    for m in grid.merges or []:
        present = [
            old_to_new[i]
            for i in range(m.row_start, m.row_end + 1)
            if i in old_to_new
        ]
        if not present:
            continue
        ns, ne = min(present), max(present)
        # 合并值须落在新起点行；若原起点被删，把 cell 挪到 ns
        src_rid = old_ids[m.row_start] if m.row_start < len(old_ids) else ""
        dst_rid = new_ids[ns]
        src_cell = (grid.cells.get(src_rid) or {}).get(m.col)
        if src_cell is not None and src_cell.ref == m.ref:
            new_cells.setdefault(dst_rid, {})[m.col] = src_cell
        # 确认起点有值
        cell = (new_cells.get(dst_rid) or {}).get(m.col)
        if cell is None or cell.ref != m.ref:
            # 从旧区间任意保留行找回
            recovered = None
            for oi in range(m.row_start, m.row_end + 1):
                if oi not in old_to_new:
                    continue
                rid = old_ids[oi]
                pl = (grid.cells.get(rid) or {}).get(m.col)
                if pl is not None and pl.ref == m.ref:
                    recovered = pl
                    break
            if recovered is None:
                continue
            new_cells.setdefault(dst_rid, {})[m.col] = recovered
        if ns == ne:
            # 单行不再保留竖合并
            continue
        new_merges.append(
            MergeRange(
                ref=m.ref,
                col=m.col,
                row_start=ns,
                row_end=ne,
                row_ids=tuple(new_ids[r] for r in range(ns, ne + 1)),
            )
        )

    grid.row_ids = new_ids
    grid.cells = new_cells
    grid.labels = new_labels
    grid.merges = new_merges
    return grid


def assert_worksheet_unique_device_values(ws, *, sheet: str = "") -> None:
    """写表后：第 2 列起位号不得跨列重复；同列多段竖并锚点允许。

    **例外**：A 列均为 FBRX 取样标签的两行可同显同一取样链位号
    （多 Coupler 各挂一条完整 FBRX 链，见 _locs_all_fbrx_sample）。
    """
    seen: dict[str, list[tuple[int, int]]] = {}
    dups: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for r in range(1, (ws.max_row or 0) + 1):
        a_lab = str(ws.cell(r, 1).value or "").upper()
        for c in range(2, (ws.max_column or 0) + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            ref = str(v).strip()
            if not ref:
                continue
            prevs = seen.setdefault(ref, [])
            if not prevs:
                prevs.append((r, c))
                continue
            # 同列竖并锚：允许
            if any(pc == c for _pr, pc in prevs):
                prevs.append((r, c))
                continue
            # 两行皆 FBRX 取样：允许同链双显
            if "FBRX" in a_lab and all(
                "FBRX" in str(ws.cell(pr, 1).value or "").upper()
                for pr, _pc in prevs
            ):
                prevs.append((r, c))
                continue
            dups.append((ref, prevs[0], (r, c)))
            prevs.append((r, c))
    if dups:
        raise UniqueRefError(
            f"[功能5][最终产物·handbook] Sheet={sheet or ws.title} "
            f"写表后仍有重复器件 {len(dups)} 处，拒绝交付。样例: {dups[:8]}"
        )

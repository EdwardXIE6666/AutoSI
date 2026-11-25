"""功能5 阶段 B — L2 行序（简化）。

不变量（与 L6 对偶）：
  1. 可竖并 ref 的 owner 行在序上尽量连续（holes→0）
  2. 非 owner 主链不得夹在 owner 包围盒内（gather 挤到块后）
  3. 前端（FIL / Coupler / 开关 IC）优先于 PA/LNA
  4. 同 Co-pad 结构触及行软聚类（仅行序，不进并柱）

流水线（短）：
  稳定种子 → 高 tier UF 分量 → 分量内加权 Jaccard 聚类
  → compact（按优先级 gather，挤出非 owner）
polish = compact（rehome 后再收一次）

决策面：
  score = Σ hole_cost(ref)；accept 仅当 holes 下降且不破坏更高 tier。
"""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from typing import Optional

from refnum.layout.model import LayoutRow, is_decoration_row_id
from refnum.layout.share import (
    display_ref_seq,
    is_pa_lna_device,
    should_vertical_merge,
)
from refnum.path import CoPadStructure, Device


_COMPACT_ITERS = 64


def _is_mergeable_ref(
    rows: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
    lnc: dict[str, int],
    ref: str,
) -> bool:
    """行序层：与 L6 同一合并器件判定。"""
    plan = SimpleNamespace(
        rows=rows,
        share_map=share_map or {},
        left_neighbor_count=lnc or {},
    )
    return should_vertical_merge(plan, ref, lnc)


def _is_merge_priority_ref(ref: str, device_hint: Optional[Device] = None) -> int:
    """越大越优先保证连续。

    前端（PA/LNA–ANT 区间结构件）> SDR > PA/LNA > 被动。
    **禁止**用位号前缀（U/FIL…）判断；有 device 用类别，无则中性。
    """
    from refnum.layout.share import is_ant_ref, is_rlct_ref

    if device_hint is not None:
        if is_pa_lna_device(device_hint) or getattr(device_hint, "is_pa_lna", False):
            return 2
        if is_rlct_ref(str(ref or ""), device_hint) or is_ant_ref(str(ref or ""), device_hint):
            return 1
        if getattr(device_hint, "is_sdr", False):
            return 3
        # 有 path 时 is_frontend 更准；此处无 path，结构 IC/滤波类抬高
        # （真正「是否 PA–ANT 区间」在 rehome / holes 收口用 path 判定）
        pt = str(getattr(device_hint, "part_type", "") or "").upper()
        if any(
            k in pt
            for k in (
                "COUPLER", "SWITCH", "SAW", "FILTER", "DIVIDER",
                "DIPLEX", "DUPLEX", "MULTIPLEX",
            )
        ):
            return 5
        if getattr(device_hint, "is_ic", False) or getattr(device_hint, "is_lpf", False):
            return 5
        if getattr(device_hint, "is_passive", False):
            return 1
    # 无 Device 提示：不猜前缀，中性
    return 2


def _avg_display_index(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> float:
    """显示序平均下标：越小越靠芯片侧。"""
    vals: list[int] = []
    for rid in owners:
        row = id_to_row.get(rid)
        if row is None or not row.path:
            continue
        seq = display_ref_seq(row.path)
        if ref in seq:
            vals.append(seq.index(ref))
    return sum(vals) / len(vals) if vals else 1e9


class _UF:
    def __init__(self, items: list[str]):
        self.p = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _direction_rank(direction: str) -> int:
    d = (direction or "").upper()
    return {"RX": 0, "TX": 1, "TRX": 2}.get(d, 3)


def _row_stable_key(row: LayoutRow) -> tuple:
    p = row.path
    return (
        (p.band or "").upper(),
        _direction_rank(p.direction),
        (p.kind or "").upper(),
        row.row_id,
    )


def _component_sort_key(comp_rows: list[LayoutRow]) -> tuple:
    bands = sorted({(r.path.band or "").upper() for r in comp_rows})
    ids = sorted(r.row_id for r in comp_rows)
    return (bands[0] if bands else "", len(comp_rows), ids[0] if ids else "")


def holes_for_ref(order_ids: list[str], owners: frozenset[str]) -> int:
    """owner 包围盒内非 owner 行数（硬 holes）。"""
    pos = [i for i, rid in enumerate(order_ids) if rid in owners]
    if len(pos) < 2:
        return 0
    return (pos[-1] - pos[0] + 1) - len(pos)


def holes_for_ref_order(
    order_ids: list[str],
    owners: frozenset[str],
    multi_refs: Optional[dict[str, frozenset[str]]] = None,
) -> int:
    """holes；同 Co-pad 结构软簇内的非 owner 不计入。

    例：COLAY 公共腿 owner=COLAY+JUMP，邻频段同属 ``__copad_struct__*`` 时
    该簇对公共腿有效 holes=0。
    """
    pos = [i for i, rid in enumerate(order_ids) if rid in owners]
    if len(pos) < 2:
        return 0
    soft_cover: set[str] = set()
    for ref, ow in (multi_refs or {}).items():
        if not str(ref).startswith("__copad_struct__"):
            continue
        if owners <= ow:
            soft_cover |= set(ow)
    h = 0
    for i in range(pos[0], pos[-1] + 1):
        rid = order_ids[i]
        if rid in owners or rid in soft_cover:
            continue
        h += 1
    return h


def gather_owners(
    order: list[LayoutRow],
    owners: frozenset[str],
) -> list[LayoutRow]:
    """owners 收成连续块（保相对序）；区间内非 owner 挤到块后。"""
    idxs = [i for i, r in enumerate(order) if r.row_id in owners]
    if len(idxs) < 2:
        return order
    lo, hi = min(idxs), max(idxs)
    if hi - lo + 1 == len(idxs):
        return order
    owner_rows = [order[i] for i in idxs]
    non_owner = [order[i] for i in range(lo, hi + 1) if order[i].row_id not in owners]
    return order[:lo] + owner_rows + non_owner + order[hi + 1 :]


def _is_sample_label_row(row: LayoutRow) -> bool:
    text = f"{getattr(row, 'label', '')} {getattr(row, 'row_id', '')}".upper()
    return "FBRX" in text


def _gather_holes_outside_sample(
    order: list[LayoutRow],
    owners: frozenset[str],
) -> list[list[LayoutRow]]:
    """无关主链挤到「owner 块 + 紧邻短 FBRX」外侧（块前 / 块后各一）。

    只挤非取样 hole：取样仍贴宿主，侧链不得留在芯片侧前端跨度里。
    """
    idxs = [i for i, r in enumerate(order) if r.row_id in owners]
    if len(idxs) < 2:
        return []
    lo, hi = min(idxs), max(idxs)
    while lo > 0 and _is_sample_label_row(order[lo - 1]):
        lo -= 1
    while hi + 1 < len(order) and _is_sample_label_row(order[hi + 1]):
        hi += 1
    block = order[lo:hi + 1]
    holes = [
        r for r in block
        if r.row_id not in owners and not _is_sample_label_row(r)
    ]
    core = [
        r for r in block
        if r.row_id in owners or _is_sample_label_row(r)
    ]
    if not holes:
        return []
    return [
        order[:lo] + holes + core + order[hi + 1:],
        order[:lo] + core + holes + order[hi + 1:],
    ]


def _is_transparent_plan_row(row: LayoutRow) -> bool:
    """取样 / 空行 / 夹心支路：这一层不算「无关主链」。"""
    rid = str(getattr(row, "row_id", "") or "")
    if is_decoration_row_id(rid):
        return True
    return _is_sample_label_row(row)


def _is_page_backbone_ref(
    ref: str,
    owners: frozenset[str],
    rows: list[LayoutRow],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    """不当聚类键、不收拢。

    须：只在一部分主链上的合并件，中间有无关主链 → 要收。
    可：非 owner 只剩取样/空行（或所有主链都是 owner）→ 不收。
    禁：不得用页比例 85% 决定。
    """
    if not owners or not rows:
        return False
    mains = [r.row_id for r in rows if not _is_transparent_plan_row(r)]
    if not mains:
        return len(owners) >= len(rows)
    return all(rid in owners for rid in mains)


def _row_multi_set(
    row_id: str,
    multi_refs: dict[str, frozenset[str]],
    rows: list[LayoutRow],
    id_to_row: dict[str, LayoutRow],
) -> frozenset[str]:
    out: set[str] = set()
    for ref, owners in multi_refs.items():
        if row_id not in owners:
            continue
        if _is_page_backbone_ref(ref, owners, rows, id_to_row):
            continue
        out.add(ref)
    return frozenset(out)


def _hint_for_ref(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> Optional[Device]:
    for rid in owners:
        row = id_to_row.get(rid)
        if not row:
            continue
        for d in row.devices:
            if d.ref == ref:
                return d
    return None


def _ref_tier(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> int:
    """3=前端/结构, 2=SDR, 1=PA·LNA/被动。"""
    if str(ref or "").startswith("__copad_struct__"):
        return 3
    pri = _is_merge_priority_ref(ref, _hint_for_ref(ref, owners, id_to_row))
    if pri >= 4:
        return 3
    if pri >= 3:
        return 2
    return 1


def _fe_priority_key(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> tuple[int, float]:
    """越小越优先：高 tier 先，同 tier 列位更小（芯片侧）先。"""
    return (
        -_ref_tier(ref, owners, id_to_row),
        _avg_display_index(ref, owners, id_to_row),
    )


def _is_protected_fe_ref(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    if str(ref or "").startswith("__copad_struct__"):
        return False
    if _ref_tier(ref, owners, id_to_row) >= 3:
        return True
    return _ref_is_topo_frontend_multi(ref, owners, id_to_row)


def _higher_priority_fe_holes_rose(
    order: list[LayoutRow],
    cand: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
    *,
    except_ref: str,
) -> bool:
    """cand 是否抬高了比 except_ref 更优先的前端 holes。"""
    ex = str(except_ref or "").strip()
    ex_owners = multi_refs.get(ex) if ex else None
    ex_key = (
        _fe_priority_key(ex, ex_owners, id_to_row)
        if ex and ex_owners
        else (1, 1e9)
    )
    ids0 = [r.row_id for r in order]
    ids1 = [r.row_id for r in cand]
    cand_map = {r.row_id: r for r in cand}
    for ref, owners in multi_refs.items():
        if ex and str(ref) == ex:
            continue
        if not _is_protected_fe_ref(ref, owners, id_to_row):
            continue
        if _fe_priority_key(ref, owners, id_to_row) >= ex_key:
            continue
        h0 = holes_for_ref_order(ids0, owners, multi_refs)
        h1 = holes_for_ref_order(ids1, owners, multi_refs)
        if h1 > h0:
            return True
    return False


def copad_structure_soft_multi(
    rows: list[LayoutRow],
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> dict[str, frozenset[str]]:
    """Co-pad 结构 → 触及该结构的行（仅 L2 聚类/打分，不进并柱）。"""
    if not rows or not copad_structures:
        return {}
    out: dict[str, frozenset[str]] = {}
    for i, st in enumerate(copad_structures):
        members: set[str] = set()
        for attr in (
            "fork_pad", "merge_pad", "branch1", "branch2", "internal_chain",
        ):
            for x in getattr(st, attr, ()) or ():
                s = str(x or "").strip()
                if s:
                    members.add(s)
        fork = getattr(st, "fork", None)
        tr = str(getattr(fork, "trunk_ref", "") or "").strip()
        if tr:
            members.add(tr)
        if len(members) < 2:
            continue
        owners: set[str] = set()
        for row in rows:
            for d in row.devices or []:
                ref = str(getattr(d, "ref", "") or "").strip()
                if ref in members:
                    owners.add(row.row_id)
                    break
        if len(owners) >= 2:
            out[f"__copad_struct__{i}"] = frozenset(owners)
    return out


def _merge_soft_multi(
    multi_refs: dict[str, frozenset[str]],
    rows: list[LayoutRow],
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> dict[str, frozenset[str]]:
    merged = dict(multi_refs)
    for k, v in copad_structure_soft_multi(rows, copad_structures).items():
        merged[k] = v
    return merged


def _build_multi_refs(
    rows: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
    lnc: dict[str, int],
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> dict[str, frozenset[str]]:
    multi = {
        ref: owners
        for ref, owners in (share_map or {}).items()
        if len(owners) >= 2 and _is_mergeable_ref(rows, share_map or {}, lnc, ref)
    }
    return _merge_soft_multi(multi, rows, copad_structures)


def _hole_cost(
    ref: str,
    owners: frozenset[str],
    holes: int,
) -> float:
    if holes <= 0:
        return 0.0
    n = max(1, len(owners))
    return float(holes) * float(n * n)


def _score_order(
    order: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
) -> tuple:
    """越小越好：(cost, total_h, 稳定序)。"""
    ids = [r.row_id for r in order]
    cost = 0.0
    total_h = 0
    for ref, owners in multi_refs.items():
        h = holes_for_ref_order(ids, owners, multi_refs)
        cost += _hole_cost(ref, owners, h)
        total_h += h
    return (cost, total_h, tuple(ids))


def _tier_hole_sum(
    order: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
    *,
    min_tier: int,
    except_ref: str = "",
) -> int:
    """≥min_tier 的 holes 总和（except_ref 不计）。"""
    ids = [r.row_id for r in order]
    ex = str(except_ref or "").strip()
    total = 0
    for ref, owners in multi_refs.items():
        if ex and str(ref) == ex:
            continue
        if _ref_tier(ref, owners, id_to_row) < min_tier:
            continue
        total += holes_for_ref_order(ids, owners, multi_refs)
    return total


def _weighted_jaccard(
    a: frozenset[str],
    b: frozenset[str],
    ref_weight: dict[str, float],
) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    num = sum(ref_weight.get(r, 1.0) for r in (a & b))
    den = sum(ref_weight.get(r, 1.0) for r in union)
    return num / den if den else 0.0


def _cluster_order_weighted(
    rows: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
) -> list[LayoutRow]:
    """加权 Jaccard 层次聚类；全主链都经过的件权重 0。"""
    if len(rows) <= 1:
        return list(rows)
    ref_w: dict[str, float] = {}
    for ref, owners in multi_refs.items():
        if _is_page_backbone_ref(ref, owners, rows, id_to_row):
            ref_w[ref] = 0.0
        else:
            ref_w[ref] = float(10 ** _ref_tier(ref, owners, id_to_row))
    clusters: list[list[str]] = [[r.row_id] for r in sorted(rows, key=_row_stable_key)]
    multi_of = {
        r.row_id: _row_multi_set(r.row_id, multi_refs, rows, id_to_row)
        for r in rows
    }

    def _sig(c: list[str]) -> frozenset[str]:
        s: set[str] = set()
        for rid in c:
            s |= multi_of.get(rid, frozenset())
        return frozenset(s)

    while len(clusters) > 1:
        best_i, best_j, best_score, best_tie = 0, 1, -1.0, ""
        for i in range(len(clusters)):
            si = _sig(clusters[i])
            for j in range(i + 1, len(clusters)):
                sc = _weighted_jaccard(si, _sig(clusters[j]), ref_w)
                tie = min(clusters[i][0], clusters[j][0])
                if sc > best_score or (sc == best_score and tie < best_tie):
                    best_i, best_j, best_score, best_tie = i, j, sc, tie
        i, j = (best_i, best_j) if best_i < best_j else (best_j, best_i)
        a, b = clusters[i], clusters[j]
        merged = a + b if a[0] <= b[0] else b + a
        clusters = [c for k, c in enumerate(clusters) if k not in (i, j)]
        clusters.append(merged)
    return [id_to_row[rid] for rid in clusters[0] if rid in id_to_row]


def _seed_order(
    rows: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
) -> list[LayoutRow]:
    """稳定键 + 高 tier UF 分量 + 分量内聚类。"""
    if not rows:
        return []
    id_to_row = {r.row_id: r for r in rows}
    uf = _UF([r.row_id for r in rows])
    for ref, owners in multi_refs.items():
        if _ref_tier(ref, owners, id_to_row) < 2:
            continue
        if _is_page_backbone_ref(ref, owners, rows, id_to_row):
            continue
        ids = list(owners)
        for a, b in zip(ids, ids[1:]):
            uf.union(a, b)

    comps: dict[str, list[LayoutRow]] = defaultdict(list)
    for r in rows:
        comps[uf.find(r.row_id)].append(r)

    order: list[LayoutRow] = []
    for comp in sorted(comps.values(), key=_component_sort_key):
        if multi_refs and len(comp) > 1:
            order.extend(_cluster_order_weighted(comp, multi_refs, id_to_row))
        else:
            order.extend(sorted(comp, key=_row_stable_key))
    got = {r.row_id for r in order}
    for r in sorted(rows, key=_row_stable_key):
        if r.row_id not in got:
            order.append(r)
    return order


# compact（统一 gather）

def _best_place_owners(
    order: list[LayoutRow],
    owners: frozenset[str],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
    *,
    ref: str = "",
) -> list[LayoutRow]:
    """owners 连续块 + 枚举插入位，取 score 最优。

    抬高更优先前端 holes 的候选直接丢掉。
    """
    owner_rows = [r for r in order if r.row_id in owners]
    if len(owner_rows) < 2:
        return order
    rest = [r for r in order if r.row_id not in owners]
    best, best_sc = order, _score_order(order, multi_refs, id_to_row)
    for cand in (
        gather_owners(order, owners),
        *_gather_holes_outside_sample(order, owners),
        *(rest[:i] + owner_rows + rest[i:] for i in range(len(rest) + 1)),
    ):
        if _higher_priority_fe_holes_rose(
            order, cand, multi_refs, id_to_row, except_ref=ref,
        ):
            continue
        sc = _score_order(cand, multi_refs, id_to_row)
        if sc < best_sc:
            best, best_sc = cand, sc
    return best


def _ref_is_topo_frontend_multi(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    """该 multi ref 是否落在任一 owner path 的 PA/LNA–ANT 前端区间。"""
    from refnum.layout.share import frontend_merge_refs_on_path

    rs = str(ref or "").strip()
    if not rs or rs.startswith("__copad_struct__"):
        return False
    for rid in owners:
        row = id_to_row.get(rid)
        if row is None:
            continue
        if rs in frontend_merge_refs_on_path(getattr(row, "path", None)):
            return True
    return False


def _topo_frontend_hole_sum(
    order: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
    *,
    except_ref: str = "",
) -> int:
    """拓扑前端 multi 的 holes 总和（PA/LNA–ANT 区间器件）。"""
    ids = [r.row_id for r in order]
    ex = str(except_ref or "").strip()
    total = 0
    for ref, owners in multi_refs.items():
        if ex and str(ref) == ex:
            continue
        if not _ref_is_topo_frontend_multi(ref, owners, id_to_row):
            continue
        total += holes_for_ref_order(ids, owners, multi_refs)
    return total


def _ref_is_pa_lna_multi(
    ref: str,
    owners: frozenset[str],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    d = _hint_for_ref(ref, owners, id_to_row)
    return bool(d is not None and is_pa_lna_device(d))


def _accept_compact(
    order: list[LayoutRow],
    cand: list[LayoutRow],
    *,
    ref: str,
    owners: frozenset[str],
    h0: int,
    multi_refs: dict[str, frozenset[str]],
    id_to_row: dict[str, LayoutRow],
) -> bool:
    """是否接受一次 gather。

    硬约束：不得为低优先级前端抬高高优先级前端 holes。
    规则：
      - 本 ref holes 必须下降
      - 更高 tier / 同 tier 列位更小的前端 holes 不得升高
      - PA/LNA 收口：拓扑前端 holes 必须不升
      - 前端：score 变好，或本颗 holes→0（不问分数缝）
      - 其它：score 变好 / 低 tier 不抬 tier 前端
    """
    cids = [r.row_id for r in cand]
    h1 = holes_for_ref_order(cids, owners, multi_refs)
    if h1 >= h0:
        return False
    cand_map = {r.row_id: r for r in cand}
    tier_r = _ref_tier(ref, owners, id_to_row)

    # 硬：不得抬高比本 ref 更优先的前端 holes（高 tier / 同 tier 芯片侧）
    if _higher_priority_fe_holes_rose(
        order, cand, multi_refs, id_to_row, except_ref=ref,
    ):
        return False
    # PA/LNA 收口：更严——任何拓扑前端 holes 升高都拒（含 except 外全部）
    if _ref_is_pa_lna_multi(ref, owners, id_to_row):
        t0 = _topo_frontend_hole_sum(order, multi_refs, id_to_row)
        t1 = _topo_frontend_hole_sum(cand, multi_refs, cand_map)
        if t1 > t0:
            return False

    fe0 = _tier_hole_sum(order, multi_refs, id_to_row, min_tier=3, except_ref=ref)
    fe1 = _tier_hole_sum(cand, multi_refs, cand_map, min_tier=3, except_ref=ref)

    sc0 = _score_order(order, multi_refs, id_to_row)
    sc1 = _score_order(cand, multi_refs, cand_map)

    if tier_r >= 3 or _ref_is_topo_frontend_multi(ref, owners, id_to_row):
        if sc1 < sc0:
            return True
        # 本颗前端 holes→0：更优先前端已在上面挡过，不再用分数缝猜
        if h1 == 0:
            return True
        return False

    if fe1 > fe0:
        return False
    return sc1 <= sc0


def compact_row_order(
    order: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
    *,
    force: bool = False,
) -> list[LayoutRow]:
    """统一收口：按优先级 gather，把非 owner 挤出 owner 包围盒。

    force=False：偏 score（l2 主路径）
    force=True：前端 holes 能降就收（polish / rehome 后）
    """
    if not order or not multi_refs:
        return order
    order = list(order)

    for _ in range(_COMPACT_ITERS):
        ids = [r.row_id for r in order]
        id_to_row = {r.row_id: r for r in order}
        bad: list[tuple[int, int, int, str, frozenset[str]]] = []
        for ref, owners in multi_refs.items():
            h = holes_for_ref_order(ids, owners, multi_refs)
            if h <= 0:
                continue
            if _is_page_backbone_ref(ref, owners, order, id_to_row):
                continue
            # 行首 PA/LNA：强制 gather 会拆 PRX|TX 对，留给 score 微调
            if not force and _avg_display_index(ref, owners, id_to_row) < 1.0:
                continue
            tier = _ref_tier(ref, owners, id_to_row)
            # 前端优先；同 tier 小 |owners| 先收（Coupler 2-owner 先于大 FIL）
            bad.append((-tier, len(owners), -h, ref, owners))
        if not bad:
            break
        bad.sort()
        improved = False
        for _nt, _n, _nh, ref, owners in bad:
            h0 = holes_for_ref_order(ids, owners, multi_refs)
            candidates = [
                gather_owners(order, owners),
                *_gather_holes_outside_sample(order, owners),
                _best_place_owners(
                    order, owners, multi_refs, id_to_row, ref=ref,
                ),
            ]
            best = None
            best_h = h0
            best_sc = _score_order(order, multi_refs, id_to_row)
            is_fe_ref = _ref_is_topo_frontend_multi(ref, owners, id_to_row)
            is_pa_ref = _ref_is_pa_lna_multi(ref, owners, id_to_row)
            for cand in candidates:
                h1 = holes_for_ref_order(
                    [r.row_id for r in cand], owners, multi_refs,
                )
                if h1 >= h0:
                    continue
                cand_map = {r.row_id: r for r in cand}
                if _higher_priority_fe_holes_rose(
                    order, cand, multi_refs, id_to_row, except_ref=ref,
                ):
                    continue
                # PA/LNA：不得抬高全部拓扑前端 holes
                if is_pa_ref:
                    if (
                        _topo_frontend_hole_sum(cand, multi_refs, cand_map)
                        > _topo_frontend_hole_sum(order, multi_refs, id_to_row)
                    ):
                        continue
                sc1 = _score_order(cand, multi_refs, cand_map)
                if force and (
                    _ref_tier(ref, owners, id_to_row) >= 3 or is_fe_ref
                ):
                    # 拓扑/类别前端 force：holes↓ 且 score 变好
                    if sc1 < best_sc or (sc1 == best_sc and h1 < best_h):
                        best, best_h, best_sc = cand, h1, sc1
                    continue
                if _accept_compact(
                    order, cand,
                    ref=ref, owners=owners, h0=h0,
                    multi_refs=multi_refs, id_to_row=id_to_row,
                ):
                    if h1 < best_h or (h1 == best_h and sc1 < best_sc):
                        best, best_h, best_sc = cand, h1, sc1
            if best is not None and best_h < h0:
                order = best
                improved = True
                break
        if not improved:
            break
    return order


# 公开 API

def l2_order_rows(
    rows: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
    left_neighbor_count: Optional[dict[str, int]] = None,
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> list[LayoutRow]:
    """L2：共享 owner 尽量相邻；同 Co-pad 结构软聚类。"""
    if not rows:
        return []
    lnc = left_neighbor_count or {}
    multi_refs = _build_multi_refs(rows, share_map, lnc, copad_structures)
    order = _seed_order(rows, multi_refs)
    order = compact_row_order(order, multi_refs, force=False)
    order = compact_row_order(order, multi_refs, force=False)
    order = force_eject_non_owners(order, multi_refs)
    return order


def force_eject_non_owners(
    order: list[LayoutRow],
    multi_refs: dict[str, frozenset[str]],
) -> list[LayoutRow]:
    """硬收口：凡 multi 仍有 **硬 holes**，强制 gather 挤出非 owner。

    典型：开关 owner=多条 FBRX 取样行，贯通行夹在中间 → 并柱盖到贯通行
    （非 owner）。score/前端启发式可能拒收；本函数只认 holes↓。

    例外：非 owner 只剩取样/空行则不强制；PA gather 若抬高拓扑前端 holes 则跳过该步。
    """
    if not order or not multi_refs:
        return order
    order = list(order)
    for _ in range(max(8, len(multi_refs) * 4)):
        ids = [r.row_id for r in order]
        id_to_row = {r.row_id: r for r in order}
        bad: list[tuple[int, int, str, frozenset[str]]] = []
        for ref, owners in multi_refs.items():
            if str(ref).startswith("__copad_struct__"):
                continue
            if len(owners) < 2 or _is_page_backbone_ref(
                ref, owners, order, id_to_row,
            ):
                continue
            # 硬 holes（不算 copad soft 透明）——中间夹非 owner 贯通行必须算 hole
            h = holes_for_ref(ids, owners)
            if h > 0:
                bad.append((len(owners), -h, ref, owners))
        if not bad:
            break
        bad.sort()
        improved = False
        for _n, _nh, ref, owners in bad:
            h0 = holes_for_ref(ids, owners)
            cands = [
                gather_owners(order, owners),
                *_gather_holes_outside_sample(order, owners),
                _best_place_owners(
                    order, owners, multi_refs, id_to_row, ref=ref,
                ),
            ]
            best = None
            best_h = h0
            for cand in cands:
                h1 = holes_for_ref([r.row_id for r in cand], owners)
                if h1 >= best_h:
                    continue
                if _higher_priority_fe_holes_rose(
                    order, cand, multi_refs, id_to_row, except_ref=ref,
                ):
                    continue
                cand_map = {r.row_id: r for r in cand}
                # PA 不得靠挤非 owner 抬高拓扑前端 holes
                if _ref_is_pa_lna_multi(ref, owners, id_to_row):
                    t0 = _topo_frontend_hole_sum(order, multi_refs, id_to_row)
                    t1 = _topo_frontend_hole_sum(cand, multi_refs, cand_map)
                    if t1 > t0:
                        continue
                best, best_h = cand, h1
            if best is not None and best_h < h0:
                order = best
                improved = True
                break
        if not improved:
            break
    return order


def polish_row_order(
    order: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
    left_neighbor_count: Optional[dict[str, int]] = None,
    copad_structures: Optional[list[CoPadStructure]] = None,
) -> list[LayoutRow]:
    """L2 后处理 / rehome 后再收：前端 force + **非 owner 硬挤出**。"""
    if not order:
        return order
    lnc = left_neighbor_count or {}
    multi_refs = _build_multi_refs(order, share_map, lnc, copad_structures)
    if not multi_refs:
        return order
    order = compact_row_order(order, multi_refs, force=False)
    # force：前端（含 |owners|=2 的 Coupler）holes 能降就收
    order = compact_row_order(order, multi_refs, force=True)
    # 硬：任意 multi 夹非 owner 必须挤出
    order = force_eject_non_owners(order, multi_refs)
    return order


def residual_holes(
    order: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
    left_neighbor_count: Optional[dict[str, int]] = None,
) -> list[tuple[str, int, list[str]]]:
    """仍 holes>0 的共享器件。"""
    order_ids = [r.row_id for r in order]
    lnc = left_neighbor_count or {}
    bad: list[tuple[str, int, list[str]]] = []
    for ref, owners in share_map.items():
        if len(owners) < 2:
            continue
        if not _is_mergeable_ref(order, share_map, lnc, ref):
            continue
        h = holes_for_ref(order_ids, owners)
        if h > 0:
            bad.append((ref, h, sorted(owners)))
    bad.sort(key=lambda x: (-x[1], x[0]))
    return bad


def assert_holes_zero(
    order: list[LayoutRow],
    share_map: dict[str, frozenset[str]],
) -> None:
    bad = residual_holes(order, share_map)
    if bad:
        raise RuntimeError(
            f"[功能5][最终产物·handbook] L2 holes!=0: {bad[:5]}"
        )

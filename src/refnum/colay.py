"""功能5 — Co-lay（orphan 焊盘双腿 · 支路补全）。

从 refnum_copad 拆出，降低单文件职责。
焊盘发现/成组原语用 refnum_copad 公开 API；本模块只承载 Co-lay 判定与合成。
兼容：refnum_copad / refnum_path re-export 本模块公开符号。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from refnum.graph import (
    ant_port_number,
    build_net_device_maps,
    is_coupler_ref,
    is_foreign_ant_isolation_net,
    is_gnd_net,
    is_jump_isolation_net,
    is_pa_lna_ref,
    is_plausible_comp_ref,
    is_rlct,
    is_shunt_device,
    is_transceiver_ref,
    make_device,
    signal_infos_of,
)
from refnum.copad import (
    branch_topology_signal_infos,
    build_copad_structures,
    copad_device_adj,
    copad_used_pad_nets,
    dist_to_nearest_ant,
    leg_nearest_ant_port,
    list_pad_tripods,
    pad_discovery_signal_infos,
    pad_rlct_legs,
    pair_from_pad_ends,
    path_follows_ant_to_trx,
)


def _branch_dest_foreign_to_home(
    leg: str,
    pad_net: str,
    home_ant: str,
    d2n: dict,
    n2d: dict,
    schematic_res,
) -> bool:
    """缺失支路远侧最近整口若与宿主页口不同，不得补进本页。"""
    dest = leg_nearest_ant_port(leg, pad_net, d2n, n2d, schematic_res)
    dp = ant_port_number(dest or "")
    hp = ant_port_number(home_ant or "")
    return bool(dp and hp and dp != hp)


def _seed_free_nets(leg, pad_net, d2n):
    """腿器件上的非地、非 JUMP 网 + 焊盘网（COLAY 支路起点）。"""
    from refnum.graph import is_gnd_net, is_jump_isolation_net

    out: set[str] = set()
    for n in d2n.get(leg, []) or []:
        ns = str(n)
        if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
            continue
        out.add(ns)
    if pad_net and not is_gnd_net(str(pad_net)):
        out.add(str(pad_net))
    return out


@dataclass(frozen=True)
class CoLayPair:
    """一个 Co-lay 对 = orphan 焊盘上选出的两只兼容腿（leg_a < leg_b）。

    与 Co-pad 对同形，但不要求远端合并焊盘；两腿是正常链路分叉/汇合语义。
    trunk_ref：焊盘上被排除的第三端（hub IC 或另一 RLCT）。
    """
    leg_a: str
    leg_b: str
    shared_nets: tuple[str, ...] = ()
    trunk_ref: str = ""


@dataclass(frozen=True)
class CoLayStructure:
    """Co-lay 结构 = 单侧共网焊盘 + 两只兼容腿 + 两侧正常支路。

    方案 A：先完整 Co-pad 成组占坑；剩余 orphan 焊盘（≥2 腿、远侧分叉、
    非 copad 合并组收口）→ Co-lay。

    与 Co-pad 类似：**必须有两条支路**；差别是无远端合并焊盘对。
    branch1/2：leg → … → PA/LNA 串联（含起点腿），供预览与缺失支路补 path。
    """
    pair: CoLayPair
    pad_net: str = ""
    pad: tuple[str, ...] = ()
    branch1: tuple[str, ...] = ()  # leg_a → … → FE
    branch2: tuple[str, ...] = ()  # leg_b → … → FE
    link_kind: str = "orphan_pad"



def _leg_far_nets(
    leg: str,
    pad_net: str,
    dev_to_nets: dict[str, list[str]],
) -> set[str]:
    """腿在焊盘网之外的信号网（排除地）。"""
    out: set[str] = set()
    for n in dev_to_nets.get(str(leg), []) or []:
        ns = str(n)
        if not ns or ns == pad_net or is_gnd_net(ns):
            continue
        out.add(ns)
    return out


def _colay_far_neighbors(
    leg: str,
    pad_net: str,
    other_leg: str,
    pad_members: set[str],
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
) -> tuple[str, ...]:
    """腿远侧一跳邻接器件（fallback stub，不含对腿与同焊盘成员）。"""
    far = _leg_far_nets(leg, pad_net, dev_to_nets)
    out: list[str] = []
    seen: set[str] = set()
    for net in sorted(far):
        for d in net_to_devs.get(net, []) or []:
            r = str(d or "").strip()
            if not r or r in seen:
                continue
            if r == leg or r == other_leg or r in pad_members:
                continue
            seen.add(r)
            out.append(r)
    return tuple(out[:8])


def _colay_part_type(ref: str, schematic_res) -> str:
    try:
        return (schematic_res.get_part_type(ref) or "").upper()
    except Exception:
        return ""


def _colay_is_switch_like(ref: str, schematic_res) -> bool:
    """电子开关类：可作支路终点，禁止再穿越（防经开关串进另一条射频链）。"""
    if is_rlct(ref, schematic_res):
        return False
    if is_pa_lna_ref(ref, schematic_res) or is_transceiver_ref(ref, schematic_res):
        return False
    pt = _colay_part_type(ref, schematic_res)
    return "SWITCH" in pt


def _colay_rf_net_count(ref: str, d2n: dict) -> int:
    """器件非地/非 JUMP RF 网口数。"""
    n = 0
    for net in d2n.get(ref, []) or []:
        ns = str(net or "")
        if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
            continue
        n += 1
    return n


def _colay_is_multiport_soft_stop(
    ref: str, schematic_res, d2n: dict,
) -> bool:
    """多 RF 口 / 开关：支路终点，禁止穿口（防 FIL 逆行到另一 TRX 前端）。

    例：腿→FIL 后再穿到对侧腿→另一 TRX 是对 ANT→TRX 主链的逆行。
    2 口 SAW/滤波仍可穿，以保留 leg→FIL→L→PA 真支路。

    **Coupler 不是软终点**：与主路一致，贯通脚可继续走到 PA/LNA；
    若把 3 网 Coupler 当终点会在 Coupler 处假停（COLAY 支路缺 PA）。
    """
    if is_rlct(ref, schematic_res):
        return False
    if is_pa_lna_ref(ref, schematic_res) or is_transceiver_ref(ref, schematic_res):
        return False
    # Coupler：作 mid 贯通，见 _colay_is_branch_mid
    if is_coupler_ref(ref, schematic_res):
        return False
    if _colay_is_switch_like(ref, schematic_res):
        return True
    if _colay_rf_net_count(ref, d2n) >= 3:
        return True
    pt = _colay_part_type(ref, schematic_res)
    if "MULTIPLEX" in pt:
        return True
    return False


def _colay_is_branch_mid(ref: str, schematic_res, d2n: dict | None = None) -> bool:
    """Co-lay 支路可穿越的中间件：RLCT + 双口滤波/分频等（非 TRX、非 FE）。

    多口 FIL/开关不作 mid（见 _colay_is_multiport_soft_stop）。
    """
    if is_rlct(ref, schematic_res):
        return True
    if is_pa_lna_ref(ref, schematic_res) or is_transceiver_ref(ref, schematic_res):
        return False
    if d2n is not None and _colay_is_multiport_soft_stop(ref, schematic_res, d2n):
        return False
    if _colay_is_switch_like(ref, schematic_res):
        return False
    pt = _colay_part_type(ref, schematic_res)
    for kw in (
        "FILTER", "DIVIDER", "COUPLER",
        "SAW", "FBAR", "DIPLEX", "TRIPLEX",
    ):
        if kw in pt:
            return True
    ru = str(ref or "").upper()
    if ru.startswith("FIL") and any(c.isdigit() for c in ru[1:]):
        return True
    return False


def _colay_better_cand(cand: list[str], best: list[str] | None) -> bool:
    if best is None:
        return True
    if len(cand) < len(best):
        return True
    if len(cand) == len(best) and cand < best:
        return True
    return False


def _colay_branch_series(
    leg: str,
    pad_net: str,
    pad_members: set[str],
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    max_depth: int = 14,
    extra_blocked: set[str] | frozenset[str] | None = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> tuple[str, ...]:
    """Co-lay 单腿支路：leg → … → PA/LNA（或开关/多口滤波终点）。

    - 可穿 RLCT/双口滤波；禁止经同焊盘兄弟腿绕行
    - extra_blocked：其它 orphan colay 腿。可**踏上**作为通向 FE 的中继，
      但从其上**只允许再扩到 PA/LNA**（找终点），禁止串其私有 FIL 链
    - 多 RF 口 / 开关只作终点、不穿口（防 FIL 逆行到另一 TRX）
    - **方向门禁：终点优先**（与 ``path_follows_ant_to_trx`` 一致）
      - 收官 PA/LNA → 整段合法，中间不看 ant_dist
      - 中间扩展不因 dist 减小剪枝（否则 L_BR@4→L_FE@3 永远到不了 LNA）
      - 无 FE 的 soft/far-stub 仍用 ant_dist 否决朝天线 stub
    """
    if not leg:
        return ()
    pad_core = set(pad_members) | {leg}
    # 同焊盘兄弟：完全禁入。其它 orphan 腿：可中继，见 foreign_legs
    sibling_block = set(pad_members) - {leg}
    foreign_legs = set(extra_blocked or ()) - sibling_block - {leg}
    free = _seed_free_nets(leg, pad_net, d2n)
    q: deque[tuple[str, list[str]]] = deque([(leg, [leg])])
    visited: set[str] = {leg} | sibling_block
    guard = 0
    best_hard: list[str] | None = None
    best_soft: list[str] | None = None

    def _ok_non_fe(cand: list[str]) -> bool:
        """无 PA/LNA 终点时的朝天线门禁（soft / far-stub）。"""
        return path_follows_ant_to_trx(
            cand, ant_dist, pad_core=pad_core, schematic_res=schematic_res,
        )

    while q and guard < 512:
        guard += 1
        cur, path = q.popleft()
        if len(path) > max_depth:
            continue
        # 其它 colay 腿上：只向 PA/LNA 收官，不扩私有链
        foreign_cur = cur in foreign_legs
        for net in d2n.get(cur, []) or []:
            ns = str(net)
            if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
                continue
            # 蜂窝支路不走 FBRX 取样网（Coupler→开关矩阵为逆向取样）
            if "FBRX" in ns.upper():
                continue
            # 起点禁止走回焊盘网（只从远侧出口扩）
            if cur == leg and ns == pad_net:
                continue
            if (
                is_foreign_ant_isolation_net(ns, "")
                and ns not in free
            ):
                # home 空时 foreign 恒 False；保留接口
                continue
            peers = [
                str(d)
                for d in (n2d.get(ns, []) or [])
                if is_plausible_comp_ref(str(d))
            ]
            if len(peers) > 10:
                continue
            for ds in peers:
                if ds == cur or ds in visited or ds in sibling_block:
                    continue
                if is_pa_lna_ref(ds, schematic_res):
                    cand = path + [ds]
                    # 终点 PA/LNA：直接收（_path_follows 终点优先亦 True）
                    if _colay_better_cand(cand, best_hard):
                        best_hard = cand
                    continue
                if foreign_cur:
                    # 已在其它 colay 腿上：禁止继续扩 RLCT/FIL 私有链
                    continue
                if is_transceiver_ref(ds, schematic_res):
                    continue
                # 多口/开关软终点：禁止穿口到另一 TRX 前端。
                # - 开关：永远软终点，禁止扩进开关矩阵
                #   （旧逻辑 only_rlct_body=False 时仍穿开关/PA，且收官 PA 使
                #    path_follows_ant_to_trx 终点优先直接放行、逆行门禁失效）
                # - 多口滤波 + 前缀全 RLCT：软终点（腿→FIL）
                # - 已过 Coupler 等 RF 躯干的多口滤波：可继续贯通找 PA
                #   （勿在 FIL 假停）
                if _colay_is_multiport_soft_stop(ds, schematic_res, d2n):
                    only_rlct_body = all(
                        is_rlct(x, schematic_res) for x in path[1:]
                    )
                    cand = path + [ds]
                    is_sw = _colay_is_switch_like(ds, schematic_res)
                    if is_sw or only_rlct_body:
                        if _ok_non_fe(cand) and _colay_better_cand(
                            cand, best_soft,
                        ):
                            best_soft = cand
                        # 仅 leg→开关 时可一步收官 PA/LNA；更长前缀禁止再扩
                        if is_sw and len(path) == 1:
                            for net2 in d2n.get(ds, []) or []:
                                ns2 = str(net2 or "")
                                if (
                                    not ns2
                                    or is_gnd_net(ns2)
                                    or is_jump_isolation_net(ns2)
                                ):
                                    continue
                                for d2 in n2d.get(ns2, []) or []:
                                    d2s = str(d2)
                                    if d2s == ds or d2s == leg:
                                        continue
                                    if is_pa_lna_ref(d2s, schematic_res):
                                        hard = path + [ds, d2s]
                                        if _colay_better_cand(hard, best_hard):
                                            best_hard = hard
                        continue
                    # 非开关多口 + 已过 RF 躯干：继续贯通找 PA；同时记 soft
                    if _ok_non_fe(cand) and _colay_better_cand(cand, best_soft):
                        best_soft = cand
                    step = path + [ds]
                    visited.add(ds)
                    q.append((ds, step))
                    continue
                if _colay_is_branch_mid(ds, schematic_res, d2n):
                    step = path + [ds]
                    visited.add(ds)
                    q.append((ds, step))
    best = best_hard if best_hard and len(best_hard) >= 2 else None
    if best is None and best_soft and len(best_soft) >= 2:
        best = best_soft
    if best and len(best) >= 2:
        return tuple(best)
    far = _colay_far_neighbors(
        leg, pad_net, "", pad_members, n2d, d2n,
    )
    if far:
        stub = (leg, *far)
        # 禁止朝天线 far-stub 冒充支路
        if _ok_non_fe(list(stub)):
            return stub
    return (leg,)


def _colay_legs_diverge(
    leg_a: str,
    leg_b: str,
    pad_net: str,
    dev_to_nets: dict[str, list[str]],
) -> bool:
    """两腿远侧分叉：至少一侧有私有远网，且两侧远网集合不完全相同。

    - 两侧都无远网 → 双 stub，证据不足，不标 co-lay
    - 两侧远网集合相同且非空 → 更像双焊盘 same_legs 残片，留给 copad/放弃
    - 其余（一侧有远网，或两侧远网不同）→ 正常并列支路，可标 co-lay
    """
    fa = _leg_far_nets(leg_a, pad_net, dev_to_nets)
    fb = _leg_far_nets(leg_b, pad_net, dev_to_nets)
    if not fa and not fb:
        return False
    if fa and fb and fa == fb:
        return False
    return True


def build_colay_structures(
    signal_infos=None,
    *,
    schematic_res=None,
    copad_structures: list | None = None,
) -> list[CoLayStructure]:
    """方案 A：完整 Co-pad 占坑后，orphan 焊盘双腿 → Co-lay。

    规则（通用，不绑位号/工程）：
      1. 焊盘发现与选腿与 Co-pad 相同（P1–P4 + _pick_copad_legs）
      2. 焊盘网未被任何完整 CoPadStructure 占用
      3. 两腿远侧分叉（正常链路，非 copad 合并组收口）
      4. 腿不与已交付 Co-pad 对重叠
    """
    if schematic_res is None:
        return []

    pad_sig = pad_discovery_signal_infos(schematic_res, signal_infos)
    branch_sig = branch_topology_signal_infos(schematic_res, signal_infos)
    if not pad_sig:
        return []

    if copad_structures is None:
        copad_structures = build_copad_structures(
            None, signal_infos=signal_infos, complete_only=True,
            schematic_res=schematic_res,
        )

    used_pad_nets = copad_used_pad_nets(copad_structures)
    # 完整结构 near/far 网还可能只写在 fork/merge.shared_nets；
    # 另用 pad 成员集合挡住已用腿
    copad_leg_refs: set[str] = set()
    for st in copad_structures:
        for p in (st.fork, st.merge):
            if p is None:
                continue
            copad_leg_refs.add(p.leg_a)
            copad_leg_refs.add(p.leg_b)

    # 选腿/焊盘用 pad 图距离；支路 BFS 与方向门禁必须与 branch 拓扑同图，
    # 否则 pad 图里 Coupler 等多口枢纽被抄近路 → leg@9→coupler@8 被误判逆行，
    # 双腿 COLAY 只剩一侧支路（另一侧 stub）。
    ant_dist = dist_to_nearest_ant(
        copad_device_adj(pad_sig), schematic_res,
    )
    branch_ant_dist = dist_to_nearest_ant(
        copad_device_adj(branch_sig), schematic_res,
    )
    tripods = list_pad_tripods(schematic_res, pad_sig, ant_dist)
    n2d_pad, d2n_pad = build_net_device_maps(pad_sig)
    n2d_br, d2n_br = build_net_device_maps(branch_sig)

    # 用完整结构的 pad 成员反推占用网（shared_nets 偶发为空时兜底）
    if not used_pad_nets:
        for st in copad_structures:
            for pad in (st.fork_pad, st.merge_pad):
                if not pad:
                    continue
                members = set(pad)
                for net, devs in n2d_pad.items():
                    if members <= set(devs) and len(members) >= 2:
                        used_pad_nets.add(str(net))

    structures: list[CoLayStructure] = []
    used_legs: set[str] = set(copad_leg_refs)

    # 两阶段：先收齐 orphan 腿，再算支路。支路 BFS 屏蔽「其它 colay 腿」，
    # 避免经 multiport 串进另一对焊盘私有链（跨拓扑假支路）。
    pending: list[tuple[str, tuple[str, ...], str, str, str]] = []
    for net, ordered in tripods:
        if str(net) in used_pad_nets:
            continue
        legs_set = pad_rlct_legs(ordered, schematic_res)
        if len(legs_set) < 2:
            continue
        if legs_set & used_legs:
            # 半腿已被 copad 占用则整盘跳过，避免拆对
            continue
        pair = pair_from_pad_ends(
            ordered, net, role="fork", schematic_res=schematic_res,
            dev_to_nets=d2n_br, net_to_devs=n2d_br, ant_dist=ant_dist,
        )
        la, lb = pair.leg_a, pair.leg_b
        if not la or not lb or la == lb:
            continue
        if la in used_legs or lb in used_legs:
            continue
        if not _colay_legs_diverge(la, lb, str(net), d2n_br):
            continue
        pending.append(
            (str(net), tuple(ordered), la, lb, pair.trunk_ref or ""),
        )
        used_legs.add(la)
        used_legs.add(lb)

    all_colay_legs = {la for _n, _p, la, lb, _t in pending} | {
        lb for _n, _p, la, lb, _t in pending
    }
    for net, ordered, la, lb, trunk_ref in pending:
        pad_members = set(ordered)
        # 其它 orphan 腿一律屏蔽（含他对；本对兄弟腿已在 pad_members）
        extra_a = all_colay_legs - {la}
        extra_b = all_colay_legs - {lb}
        b1 = _colay_branch_series(
            la, net, pad_members, n2d_br, d2n_br, schematic_res,
            extra_blocked=extra_a, ant_dist=branch_ant_dist,
        )
        b2 = _colay_branch_series(
            lb, net, pad_members, n2d_br, d2n_br, schematic_res,
            extra_blocked=extra_b, ant_dist=branch_ant_dist,
        )
        colay_pair = CoLayPair(
            leg_a=la,
            leg_b=lb,
            shared_nets=(net,),
            trunk_ref=trunk_ref,
        )
        structures.append(
            CoLayStructure(
                pair=colay_pair,
                pad_net=net,
                pad=ordered,
                branch1=b1,
                branch2=b2,
                link_kind="orphan_pad",
            )
        )

    structures.sort(
        key=lambda st: (st.pair.leg_a, st.pair.leg_b, st.pad_net),
    )
    return structures


def pairs_from_colay_structures(
    structures: list[CoLayStructure],
) -> list[CoLayPair]:
    """Co-lay 结构 → 对列表。"""
    out: list[CoLayPair] = []
    seen: set[tuple[str, str]] = set()
    for st in structures:
        key = (st.pair.leg_a, st.pair.leg_b)
        if key in seen:
            continue
        seen.add(key)
        out.append(st.pair)
    return out


def mark_colay_on_paths(
    paths: list,
    pairs: list[CoLayPair] | list[CoLayStructure],
) -> set[str]:
    """给 Path 上的 Co-lay 腿打 is_colay（不覆盖已有 is_copad）。"""
    colay_refs: set[str] = set()
    for item in pairs or []:
        if isinstance(item, CoLayStructure):
            colay_refs.add(item.pair.leg_a)
            colay_refs.add(item.pair.leg_b)
        else:
            colay_refs.add(item.leg_a)
            colay_refs.add(item.leg_b)
    if not colay_refs:
        return set()
    for path in paths:
        for dev in path.devices:
            if dev.ref in colay_refs and not getattr(dev, "is_copad", False):
                dev.is_colay = True
    return colay_refs


def colay_pad_trunk_refs(
    colay_structures: list[CoLayStructure],
    schematic_res,
    signal_infos=None,
) -> set[str]:
    """各 Co-lay 焊盘第三端干路 RLCT（公共串联节点）。"""
    if not colay_structures or schematic_res is None:
        return set()
    sig = signal_infos_of(schematic_res, signal_infos)
    n2d, d2n = build_net_device_maps(sig)
    out: set[str] = set()
    for st in colay_structures or []:
        t = _colay_pad_trunk_rlct(st, schematic_res, n2d=n2d, d2n=d2n)
        if t:
            out.add(str(t))
    return out


def mark_colay_trunk_on_paths(
    paths: list,
    colay_structures: list[CoLayStructure],
    schematic_res,
    signal_infos=None,
) -> set[str]:
    """标记 COLAY 焊盘干路 RLCT：允许手册 L3 同列 + L6 竖并。

    两条 colay 支路在干路处分叉后仍共用该节点；若当普通 RLCT 禁并，
    unique 只留一格 → 副支路在干路处视觉脱节。
    """
    trunks = colay_pad_trunk_refs(
        colay_structures, schematic_res, signal_infos=signal_infos,
    )
    if not trunks or not paths:
        return trunks
    for path in paths:
        for dev in path.devices or []:
            if str(dev.ref or "") in trunks:
                dev.is_colay_trunk = True
    return trunks


def _is_colay_shunt_trunk_skip(
    ref: str,
    schematic_res,
    d2n: Optional[dict],
) -> bool:
    """并地 / 单网对地挂件不得当 COLAY 第三端干路。

    焊盘三端已按 P1/P2 剔除它们；扫 pad_net 全员时不得再抬回来。
    单网串联 stub 不是并地，仍可当干路。
    """
    try:
        if schematic_res is not None and schematic_res.is_parallel(ref):
            return True
    except Exception:
        pass
    if d2n and is_shunt_device(ref, schematic_res, d2n):
        return True
    return False


def _colay_pad_trunk_rlct(
    st: CoLayStructure,
    schematic_res,
    n2d: Optional[dict] = None,
    d2n: Optional[dict] = None,
) -> str:
    """焊盘网上除双腿外的 RLCT 干路件。

    优先扫 pad_net 全员（tripod 的 pad 元组可能不含单网 stub 干路件）；
    再扫 st.pad；最后退 pair.trunk_ref。
    并地挂件跳过，避免把星型网上的对地 C 抬成可竖并干路。
    """
    la = str(st.pair.leg_a or "")
    lb = str(st.pair.leg_b or "")
    pad_net = str(st.pad_net or "")
    candidates: list[str] = []
    if n2d and pad_net:
        candidates.extend(str(d) for d in (n2d.get(pad_net) or []))
    candidates.extend(str(m) for m in (st.pad or ()))
    seen: set[str] = set()
    for ms in candidates:
        if not ms or ms in seen or ms in (la, lb):
            continue
        seen.add(ms)
        if not is_rlct(ms, schematic_res):
            continue
        if _is_colay_shunt_trunk_skip(ms, schematic_res, d2n):
            continue
        return ms
    trunk = str(st.pair.trunk_ref or "")
    if trunk and _is_colay_shunt_trunk_skip(trunk, schematic_res, d2n):
        return ""
    return trunk


def _colay_chip_left_branch(
    leg_to_fe: tuple[str, ...],
    schematic_res,
    signal_infos,
    *,
    exclude_refs: Optional[set[str]] = None,
) -> tuple[str, ...]:
    """leg→…→FE 转为左 FE→右腿，并注入并位。

    exclude_refs：焊盘干路件等不得并进支路（干路归夹心中层）。
    """
    if not leg_to_fe:
        return ()
    leg0 = str(leg_to_fe[0])
    chip_left = list(reversed(leg_to_fe))
    try:
        from refnum.path import inject_parallel_on_path
        sig = signal_infos_of(schematic_res, signal_infos)
        chip_left, _sh = inject_parallel_on_path(
            chip_left, schematic_res, sig, ant_first=False,
        )
    except Exception:
        pass
    block = set(exclude_refs or ()) - {leg0}
    if block:
        chip_left = [r for r in chip_left if r not in block]
    # 保证 fork 腿在最右（chip-left 支路末端）
    if leg0 in chip_left:
        chip_left = [r for r in chip_left if r != leg0] + [leg0]
    return tuple(chip_left)


def colay_to_copad_structures(
    colay_structures: list[CoLayStructure],
    paths: list,
    schematic_res,
    signal_infos=None,
) -> list[CoPadStructure]:
    """Co-lay → 可 L4 夹心装配的 CoPadStructure（无 merge）。

    可视化：上下两条支路 + 中层干路（焊盘第三 RLCT 起，接到 ANT）。
    与完整 Co-pad 同夹心策略，仅缺合并对。
    """
    if not colay_structures or schematic_res is None:
        return []

    sig = signal_infos_of(schematic_res, signal_infos)
    n2d, _d2n = build_net_device_maps(sig)
    out: list[CoPadStructure] = []
    for st in colay_structures:
        la = str(st.pair.leg_a or "")
        lb = str(st.pair.leg_b or "")
        if not la or not lb:
            continue
        br_a = tuple(st.branch1 or ())
        br_b = tuple(st.branch2 or ())
        if br_a and br_a[0] != la:
            br_a = (la, *br_a)
        if br_b and br_b[0] != lb:
            br_b = (lb, *br_b)
        # 至少有一侧可扩到 FE
        if len(br_a) < 2 and len(br_b) < 2:
            continue

        trunk_pad = _colay_pad_trunk_rlct(st, schematic_res, n2d=n2d)
        # 支路不得吞干路件（干路归中层）
        exclude = {trunk_pad} if trunk_pad else set()
        exclude |= {
            str(m) for m in (st.pad or ())
            if m and m not in (la, lb)
        }
        b1 = _colay_chip_left_branch(
            br_a, schematic_res, signal_infos, exclude_refs=exclude,
        )
        b2 = _colay_chip_left_branch(
            br_b, schematic_res, signal_infos, exclude_refs=exclude,
        )
        if len(b1) < 2 and len(b2) < 2:
            continue

        # 从宿主 path 取 trunk_after：干路件起 → ANT
        trunk_after: list[str] = []
        for path in paths or []:
            if getattr(path, "kind", "BAND") != "BAND":
                continue
            if "COLAY" in str(getattr(path, "band", "") or "").upper():
                continue
            refs = [str(d.ref) for d in (path.devices or []) if d.ref]
            if not ({la, lb} & set(refs)):
                continue
            try:
                from refnum.path import path_display_devices
                disp = [
                    str(d.ref) for d in path_display_devices(path) if d.ref
                ]
            except Exception:
                disp = list(reversed(refs))
            start_ref = trunk_pad
            if start_ref and start_ref in disp:
                trunk_after = disp[disp.index(start_ref) :]
            else:
                present = la if la in disp else (lb if lb in disp else "")
                if present and present in disp:
                    trunk_after = disp[disp.index(present) + 1 :]
            if trunk_after:
                break

        if not trunk_after and trunk_pad:
            trunk_after = [trunk_pad]

        fork = CoPadPair(
            leg_a=la if la <= lb else lb,
            leg_b=lb if la <= lb else la,
            shared_nets=(str(st.pad_net or ""),),
            trunk_ref=trunk_pad or str(st.pair.trunk_ref or ""),
        )
        if fork.leg_a != la:
            b1, b2 = b2, b1
        out.append(
            CoPadStructure(
                fork=fork,
                merge=None,
                trunk_ref=fork.trunk_ref,
                fork_pad=tuple(st.pad or ()),
                merge_pad=(),
                internal_chain=(),
                link_kind="colay",
                trunk_before=(),
                branch1=b1,
                branch2=b2,
                trunk_after=tuple(trunk_after),
            )
        )

    return out


def _colay_all_legs(structures: list[CoLayStructure]) -> set[str]:
    out: set[str] = set()
    for st in structures or []:
        la = str(getattr(st.pair, "leg_a", "") or "")
        lb = str(getattr(st.pair, "leg_b", "") or "")
        if la:
            out.add(la)
        if lb:
            out.add(lb)
    return out


def _colay_trim_branch_cross_legs(
    br: tuple[str, ...],
    *,
    own_legs: set[str],
    foreign_legs: set[str],
    schematic_res=None,
) -> tuple[str, ...]:
    """缺失支路遇其它 colay 腿时的截断策略。

    - 支路**收官到 PA/LNA/TRX**：整段保留（foreign 腿可为共享 FE 中继，截断会丢掉真芯片臂）
    - 否则（soft/stub）：遇 foreign 腿截断，防串进另一对私有链
    """
    if not br:
        return ()
    end = str(br[-1] or "")
    if schematic_res is not None and end and (
        is_pa_lna_ref(end, schematic_res)
        or is_transceiver_ref(end, schematic_res)
    ):
        return tuple(str(r) for r in br if r)
    trimmed: list[str] = []
    for r in br:
        rs = str(r or "")
        if not rs:
            continue
        if rs in foreign_legs and rs not in own_legs:
            break
        trimmed.append(rs)
    return tuple(trimmed)


def _colay_reaches_without(
    start: str,
    goals: set[str],
    blocked: set[str],
    n2d: dict,
    d2n: dict,
    schematic_res,
    *,
    max_hop: int = 4,
) -> bool:
    """start 能否不经过 blocked 器件、不经多口/前端绕行，短程到达 goals。

    用于合成正接门禁：缺失腿必须能**局部**接到 shared 首件（同焊盘/短 RLCT）。
    禁止：
      - 经 present 腿（blocked）
      - 经多口 FIL/开关/PA 绕远路再摸到 join（假通路）
    """
    start = str(start or "")
    goals = {str(g) for g in (goals or ()) if g}
    if not start or not goals:
        return False
    if start in goals:
        return True
    block = {str(b) for b in (blocked or ()) if b and b != start}
    q: deque[tuple[str, int]] = deque([(start, 0)])
    seen: set[str] = {start} | block
    guard = 0
    while q and guard < 256:
        guard += 1
        cur, dist = q.popleft()
        if dist >= max_hop:
            continue
        for net in d2n.get(cur, []) or []:
            ns = str(net or "")
            if not ns or is_gnd_net(ns) or is_jump_isolation_net(ns):
                continue
            for d in n2d.get(ns, []) or []:
                ds = str(d or "")
                if not ds or ds == cur or ds in seen:
                    continue
                if not is_plausible_comp_ref(ds):
                    continue
                if ds in goals:
                    return True
                if ds in block:
                    continue
                # 中间只许 RLCT / 双口被动；禁止多口与 FE 当跳板
                if is_pa_lna_ref(ds, schematic_res) or is_transceiver_ref(
                    ds, schematic_res,
                ):
                    continue
                if _colay_is_multiport_soft_stop(ds, schematic_res, d2n):
                    continue
                if not (
                    is_rlct(ds, schematic_res)
                    or _colay_is_branch_mid(ds, schematic_res, d2n)
                ):
                    continue
                seen.add(ds)
                q.append((ds, dist + 1))
    return False


def synthesize_colay_branch_paths(
    paths: list,
    colay_structures: list[CoLayStructure],
    schematic_res,
    signal_infos=None,
) -> list:
    """Co-lay 双支路补全（路径预览用）：主 path 只含一腿时补另一条 BAND Path。

    手册夹心已停用；此处补 path 须注入并位。
    门禁：
      - 缺失支路不得串进其它 colay 对 / 不得远长于宿主
      - **正接**：missing 不经 present 须能到达 shared 首件（禁逆行经对侧腿接共享 FIL）
    存储序 ANT-first。
    """
    if not paths or not colay_structures or schematic_res is None:
        return []

    out: list = []
    seen_keys: set[tuple] = set()
    # (ant, la, lb, missing) → 只保留最短合成分（防宿主爆炸时刷出上百条假 COLAY）
    best_by_intent: dict[tuple, tuple] = {}
    sig = signal_infos_of(schematic_res, signal_infos)
    all_legs = _colay_all_legs(colay_structures)
    # 连通性用分支拓扑（含焊盘网）
    br_sig = branch_topology_signal_infos(schematic_res, sig)
    n2d_br, d2n_br = build_net_device_maps(br_sig)

    for st in colay_structures:
        la = str(st.pair.leg_a or "")
        lb = str(st.pair.leg_b or "")
        if not la or not lb:
            continue
        own_legs = {la, lb}
        foreign_legs = all_legs - own_legs
        br_a = tuple(st.branch1 or ())
        br_b = tuple(st.branch2 or ())
        if br_a and br_a[0] != la:
            br_a = (la, *br_a)
        if br_b and br_b[0] != lb:
            br_b = (lb, *br_b)
        br_a = _colay_trim_branch_cross_legs(
            br_a, own_legs=own_legs, foreign_legs=foreign_legs,
            schematic_res=schematic_res,
        )
        br_b = _colay_trim_branch_cross_legs(
            br_b, own_legs=own_legs, foreign_legs=foreign_legs,
            schematic_res=schematic_res,
        )

        for path in paths:
            if getattr(path, "kind", "BAND") != "BAND":
                continue
            # 已是补出来的 COLAY 行不再当宿主
            if "COLAY" in str(getattr(path, "band", "") or "").upper():
                continue
            refs = [str(d.ref) for d in (path.devices or []) if d.ref]
            if not refs:
                continue
            has_a, has_b = la in refs, lb in refs
            if has_a == has_b:
                continue
            present = la if has_a else lb
            missing = lb if has_a else la
            br_miss = br_b if has_a else br_a
            if not br_miss or br_miss[0] != missing or len(br_miss) < 2:
                continue
            home_now = str(getattr(path, "ant_num", "") or "")
            if _branch_dest_foreign_to_home(
                missing, str(st.pad_net or ""), home_now,
                d2n_br, n2d_br, schematic_res,
            ):
                continue
            br_end = str(br_miss[-1] or "")
            br_ends_fe = bool(
                br_end
                and (
                    is_pa_lna_ref(br_end, schematic_res)
                    or is_transceiver_ref(br_end, schematic_res)
                )
            )
            # 支路私有段（去腿）含其它 colay 腿：无 FE 终点才否决
            # （有 FE 时 foreign 腿可为共享中继，不得截掉芯片臂）
            priv = [r for r in br_miss[1:] if r and r != missing]
            if not br_ends_fe and any(r in all_legs for r in priv):
                continue
            # 宿主不应已含缺失支路的**私有中段**（FIL/L 等）；
            # 公共 FE 终点（两腿都接到同一前端）可同在宿主上，不挡合成
            priv_mids = [
                r for r in priv
                if not is_pa_lna_ref(r, schematic_res)
                and not is_transceiver_ref(r, schematic_res)
            ]
            if any(r in refs for r in priv_mids):
                continue

            try:
                from refnum.path import path_display_devices, inject_parallel_on_path
                disp = [str(d.ref) for d in path_display_devices(path) if d.ref]
            except Exception:
                from refnum.path import inject_parallel_on_path  # type: ignore
                disp = list(reversed(refs))
            if present not in disp:
                continue
            pi = disp.index(present)
            shared = disp[pi + 1 :]
            # 宿主须在 present 左侧有芯片侧内容（避免腿落在路径尾部的垃圾路由）
            if pi < 1:
                continue

            # 宿主芯片侧 FE vs 缺失支路 FE：不同则独立成链（勿嫁接错 FE 宿主）
            # 同焊盘分叉到不同 FE：不得拼成「错 FE + 本口 ANT」假链
            host_fes = [
                str(x)
                for x in disp[:pi]
                if x
                and (
                    is_pa_lna_ref(str(x), schematic_res)
                    or is_transceiver_ref(str(x), schematic_res)
                )
            ]
            divergent_fe = bool(
                br_ends_fe
                and host_fes
                and br_end not in host_fes
            )

            # 正接门禁：同 FE 嫁接时，缺失腿须能接到 shared 首件
            if not divergent_fe and shared:
                join = str(shared[0] or "")
                if join and not _colay_reaches_without(
                    missing, {join}, {present}, n2d_br, d2n_br, schematic_res,
                ):
                    continue

            chip_left = list(reversed(br_miss))
            shunt_all: set[str] = set()
            try:
                chip_left, sh1 = inject_parallel_on_path(
                    chip_left, schematic_res, sig, ant_first=False,
                )
                shunt_all |= set(sh1 or ())
            except Exception:
                pass
            # 干路件不进私有支路段，留给 shared
            n2d_loc, _ = build_net_device_maps(sig)
            trunk_pad = _colay_pad_trunk_rlct(st, schematic_res, n2d=n2d_loc)
            # multiport 干路（FIL）：分叉 FE 臂上挂在腿外侧，便于页内看到滤波锚点
            trunk_mp = str(st.pair.trunk_ref or "") if st.pair else ""
            if not trunk_mp:
                trunk_mp = str(trunk_pad or "")
            block = {trunk_pad} if trunk_pad else set()
            if block and not divergent_fe:
                chip_left = [r for r in chip_left if r not in block]
                if missing in chip_left:
                    chip_left = [r for r in chip_left if r != missing] + [missing]
            if divergent_fe:
                # 独立 FE 臂：腿后接 pad multiport + 本口 ANT（不嫁接宿主 shared）
                if (
                    trunk_mp
                    and trunk_mp not in chip_left
                    and not is_rlct(trunk_mp, schematic_res)
                ):
                    if missing in chip_left:
                        i_m = chip_left.index(missing)
                        chip_left.insert(i_m + 1, trunk_mp)
                    else:
                        chip_left.append(trunk_mp)
                home_ant = str(getattr(path, "ant_num", "") or "").strip().upper()
                if home_ant and home_ant not in chip_left:
                    chip_left.append(home_ant)
            else:
                for x in shared:
                    if x and x not in chip_left:
                        chip_left.append(x)
            if len(chip_left) < 3:
                continue
            # 合成后不得串其它 colay 对：FE 收官时允许 foreign 中继腿
            store_probe = set(chip_left)
            if not br_ends_fe and (foreign_legs & store_probe):
                continue
            if br_ends_fe and (foreign_legs & store_probe):
                # 仅允许 br_miss 上已出现的 foreign 中继；禁额外 foreign 腿
                allowed_foreign = foreign_legs & set(br_miss)
                if (foreign_legs & store_probe) - allowed_foreign:
                    continue
            # 合成 BAND 必须仍含本口 ANT（宿主有收口才能补支路；禁止半截假链进 sheet）
            home_ant = str(getattr(path, "ant_num", "") or "").strip().upper()
            if home_ant:
                from refnum.graph import same_ant_port

                if not any(same_ant_port(x, home_ant) for x in chip_left):
                    continue
            # 不得远长于宿主：私有支路嫁接后总长上限 ≈ 宿主 + 支路净增 + 余量
            host_n = len(disp)
            br_n = len(br_miss)
            if len(chip_left) > host_n + max(br_n, 2) + 6:
                continue
            store = list(reversed(chip_left))
            try:
                store, sh2 = inject_parallel_on_path(
                    store, schematic_res, sig, ant_first=True,
                )
                shunt_all |= set(sh2 or ())
            except Exception:
                pass
            # 干路/colay 腿不得标成并联
            shunt_all -= {la, lb, missing, present, trunk_pad}
            key = (str(path.ant_num), tuple(store))
            if key in seen_keys or store == refs:
                continue
            seen_keys.add(key)

            try:
                from refnum.path import refs_to_devices
                devices = refs_to_devices(store, schematic_res, shunt_all)
            except Exception:
                devices = [make_device(r, schematic_res) for r in store]
                for d in devices:
                    if d.ref in shunt_all:
                        d.is_shunt = True
            for d in devices:
                if d.ref in (la, lb) and not getattr(d, "is_copad", False):
                    d.is_colay = True
            band = getattr(path, "band", "") or str(path.ant_num)
            if "COLAY" not in str(band).upper():
                band = f"{band}_COLAY"
            from refnum.path import Path as _Path
            new_path = _Path(
                ant_num=str(path.ant_num),
                devices=devices,
                band=band,
                direction=getattr(path, "direction", "RX") or "RX",
                kind="BAND",
            )
            intent = (str(path.ant_num), la, lb, missing)
            score = len(devices)
            prev = best_by_intent.get(intent)
            if prev is None or score < prev[0] or (
                score == prev[0] and str(band) < str(prev[1].band)
            ):
                best_by_intent[intent] = (score, new_path)

    out = [p for _s, p in sorted(best_by_intent.values(), key=lambda t: (t[1].ant_num, t[1].band))]
    return out


def emit_uncovered_colay_fe_branch_paths(
    paths: list,
    colay_structures: list[CoLayStructure],
    schematic_res,
    signal_infos=None,
) -> list:
    """COLAY 收官到 PA/LNA 的支路若尚无 path 覆盖 → 独立成 BAND。

    解决：合成依赖「宿主已含一腿」，但该腿可能要到 multiport/JUMP 之后才进页；
    或两腿都未落主扫时，分叉 FE 臂永久缺行。

    规则（通用）：
      - branch 收官 PA/LNA 且 path 集合中尚无「腿 + 该 FE」同现 → 发射
      - 页归属：优先已含对侧腿 / pad multiport / pad 成员 的 ant_num
      - 器件：支路 chip→leg + 并联注入 + pad multiport 锚 + 本口 ANT
    """
    if not colay_structures or schematic_res is None:
        return []

    sig = signal_infos_of(schematic_res, signal_infos)
    br_sig = branch_topology_signal_infos(schematic_res, sig)
    n2d_br, d2n_br = build_net_device_maps(br_sig)
    # path 覆盖：(leg, fe) 已同现
    covered: set[tuple[str, str]] = set()
    ant_of_ref: dict[str, str] = {}
    for path in paths or []:
        refs = [str(d.ref) for d in (path.devices or []) if d.ref]
        rset = set(refs)
        ant = str(getattr(path, "ant_num", "") or "").strip().upper()
        for r in rset:
            if ant and r not in ant_of_ref:
                ant_of_ref[r] = ant
        for st in colay_structures:
            for leg in (st.pair.leg_a, st.pair.leg_b):
                leg = str(leg or "")
                if not leg or leg not in rset:
                    continue
                for x in rset:
                    if is_pa_lna_ref(x, schematic_res) or is_transceiver_ref(
                        x, schematic_res,
                    ):
                        covered.add((leg, x))

    out: list = []
    seen: set[tuple] = set()
    for st in colay_structures:
        la = str(st.pair.leg_a or "")
        lb = str(st.pair.leg_b or "")
        brs = [
            (la, tuple(st.branch1 or ())),
            (lb, tuple(st.branch2 or ())),
        ]
        for leg, br in brs:
            if not leg or not br:
                continue
            if br[0] != leg:
                br = (leg, *br)
            end = str(br[-1] or "")
            if not end or not (
                is_pa_lna_ref(end, schematic_res)
                or is_transceiver_ref(end, schematic_res)
            ):
                continue
            if (leg, end) in covered:
                continue
            if len(br) < 2:
                continue
            # 页归属
            trunk = str(st.pair.trunk_ref or "")
            home = (
                ant_of_ref.get(la if leg == lb else lb)
                or ant_of_ref.get(trunk)
                or next(
                    (
                        ant_of_ref[m]
                        for m in (st.pad or ())
                        if m and str(m) in ant_of_ref
                    ),
                    "",
                )
            )
            if not home:
                continue
            if _branch_dest_foreign_to_home(
                leg, str(st.pad_net or ""), home,
                d2n_br, n2d_br, schematic_res,
            ):
                continue
            chip_left = list(reversed(br))
            shunt_all: set[str] = set()
            try:
                from refnum.path import (
                    inject_parallel_on_path,
                    refs_to_devices,
                    Path as _Path,
                )
                chip_left, sh1 = inject_parallel_on_path(
                    chip_left, schematic_res, sig, ant_first=False,
                )
                shunt_all |= set(sh1 or ())
            except Exception:
                from refnum.path import Path as _Path  # type: ignore
                refs_to_devices = None  # type: ignore
            if trunk and trunk not in chip_left and not is_rlct(
                trunk, schematic_res,
            ):
                if leg in chip_left:
                    chip_left.insert(chip_left.index(leg) + 1, trunk)
                else:
                    chip_left.append(trunk)
            if home not in chip_left:
                chip_left.append(home)
            if len(chip_left) < 3:
                continue
            store = list(reversed(chip_left))
            try:
                from refnum.path import inject_parallel_on_path
                store, sh2 = inject_parallel_on_path(
                    store, schematic_res, sig, ant_first=True,
                )
                shunt_all |= set(sh2 or ())
            except Exception:
                pass
            shunt_all -= {la, lb, leg, trunk, end}
            key = (home, tuple(store))
            if key in seen:
                continue
            seen.add(key)
            try:
                from refnum.path import refs_to_devices
                devices = refs_to_devices(store, schematic_res, shunt_all)
            except Exception:
                devices = [make_device(r, schematic_res) for r in store]
                for d in devices:
                    if d.ref in shunt_all:
                        d.is_shunt = True
            for d in devices:
                if d.ref in (la, lb):
                    d.is_colay = True
            band = f"{leg}_COLAY"
            if trunk and not is_rlct(trunk, schematic_res):
                band = f"{trunk}_{leg}_COLAY"
            out.append(
                _Path(
                    ant_num=home,
                    devices=devices,
                    band=band,
                    direction="RX",
                    kind="BAND",
                )
            )
            covered.add((leg, end))

    return out


def label_colay_structures_for_sheet(
    paths: list,
    structures: list[CoLayStructure],
    schematic_res=None,
    signal_infos=None,
) -> list[tuple[str, CoLayStructure]]:
    """本 ANT 页：每个 Co-lay 结构两腿同标（A/B/…，与 copad 标签独立计数）。

    一腿远侧整口不是本页 → 异口对，不挂本页。
    """
    ant_refs = {d.ref for p in paths for d in p.devices}
    home = ""
    for p in paths:
        home = str(getattr(p, "ant_num", "") or "").strip()
        if home:
            break
    n2d: dict = {}
    d2n: dict = {}
    if schematic_res is not None and home:
        try:
            sig = signal_infos_of(schematic_res, signal_infos)
            n2d, d2n = build_net_device_maps(sig)
        except Exception:
            n2d, d2n = {}, {}

    def _touches(st: CoLayStructure) -> bool:
        if not bool({st.pair.leg_a, st.pair.leg_b} & ant_refs):
            return False
        if not home or not d2n:
            return True
        from refnum.copad import leg_nearest_ant_port

        hp = ant_port_number(home)
        for leg in (st.pair.leg_a, st.pair.leg_b):
            dest = leg_nearest_ant_port(
                str(leg), str(st.pad_net or ""), d2n, n2d, schematic_res,
            )
            dp = ant_port_number(dest or "")
            if hp and dp and dp != hp:
                return False
        return True

    local = [st for st in structures if _touches(st)]
    local.sort(key=lambda st: (st.pair.leg_a, st.pair.leg_b, st.pad_net))

    labeled: list[tuple[str, CoLayStructure]] = []
    ref_to_label: dict[str, str] = {}
    for i, st in enumerate(local):
        if i < 26:
            lab = chr(ord("A") + i)
        else:
            lab = f"L{i + 1}"
        labeled.append((lab, st))
        ref_to_label[st.pair.leg_a] = lab
        ref_to_label[st.pair.leg_b] = lab

    for path in paths:
        for dev in path.devices:
            if dev.ref in ref_to_label and not getattr(dev, "is_copad", False):
                dev.is_colay = True
                dev.colay_label = ref_to_label[dev.ref]
    return labeled



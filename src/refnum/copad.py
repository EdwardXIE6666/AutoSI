"""功能5 — Co-pad 结构判定 + 侧支（算法）。

焊盘发现、Co-pad 成组、支路展开、路径打标、侧支。
Co-lay 实现见 refnum_colay（本模块 re-export 保持兼容）。
图原语见 refnum_graph；路径见 refnum_path；预览见 refnum_path_preview。
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from typing import Optional

from refnum.graph import (
    Device,
    ant_port_number,
    build_net_device_maps,
    canonical_ant_id,
    device_graph_from_signal_infos,
    is_ant_path_node,
    is_connector_ref,
    is_coupler_ref,
    is_cross_ant_isolation_net,
    is_foreign_ant_isolation_net,
    is_gnd_net,
    is_jump_isolation_net,
    is_multiport_anchor,
    is_network,
    is_pa_lna_ref,
    is_plausible_comp_ref,
    is_rlct,
    is_series_rlct,
    is_shunt_device,
    is_side_chain_anchor,
    is_transceiver_ref,
    make_device,
    nongnd_degree,
    pair_key,
    rlc_kind,
    signal_infos_of,
)

@dataclass(frozen=True)
class CoPadPair:
    """一个 Co-pad 对 = 三端焊盘按分叉/合并规则留下的两只（leg_a < leg_b）。

    trunk_ref：被规则排除的第三端（分叉=ANT 向已走；合并=靠干路未走）。
    腿不限定 C/L 类型（禁止样例取向补丁）。
    """
    leg_a: str
    leg_b: str
    shared_nets: tuple[str, ...] = ()
    trunk_ref: str = ""


@dataclass(frozen=True)
class CoPadStructure:
    """一整块 COPAD 结构 = 分叉对 + 两条支路 + 合并对。

    靠近 ANT 的焊盘 = 分叉；远离 ANT = 合并。
    页内：对A = 分叉两器件同标；对B = 合并两器件同标。

    拓扑展开（总览表用）：
      干路（分叉前）→ copad对(分叉) → 支路一 / 支路二 → copad对(合并) → 干路（合并后）
    支路含串联贯通件与挂在支路上的并联件。
    """
    fork: CoPadPair
    merge: Optional[CoPadPair] = None
    trunk_ref: str = ""
    fork_pad: tuple[str, ...] = ()
    merge_pad: tuple[str, ...] = ()
    internal_chain: tuple[str, ...] = ()
    link_kind: str = "bridge"
    trunk_before: tuple[str, ...] = ()   # 分叉前干路器件
    branch1: tuple[str, ...] = ()        # 支路一（含并联）
    branch2: tuple[str, ...] = ()        # 支路二（含并联）
    trunk_after: tuple[str, ...] = ()    # 合并后干路器件


def _pad_discovery_signal_infos(schematic_res, signal_infos=None) -> list:
    """COPAD **焊盘发现/成组**用：合并后的 RF 网表（非全量 808）。

    全量 all_signal_infos_raw 含电源/连接器等，会爆出假焊盘与乱桥；
    焊盘识别必须用 signal_infos_raw / 过滤网表。
    """
    return (
        getattr(schematic_res, "signal_infos_raw", None)
        or signal_infos
        or getattr(schematic_res, "signal_infos", None)
        or []
    )


def _branch_topology_signal_infos(schematic_res, signal_infos=None) -> list:
    """COPAD **支路展开**用：全量拓扑（含非激活/DNP 仍在图上的器件）。

    例：L-stub 支路仅在全量网表中完整。
    仅用于已确认结构的支路 BFS，不用于焊盘发现。
    """
    return (
        getattr(schematic_res, "all_signal_infos_raw", None)
        or _pad_discovery_signal_infos(schematic_res, signal_infos)
    )


def _is_parallel_ground_rlct(ref: str, schematic_res) -> bool:
    """是否「并联接地」RLCT（原理图 gnd/并联集合）。

    这是器件语义，不是「网名是否叫 GND」：
    - 多端口即使有地脚，也 **不是** 并联接地 RLCT，焊盘计数时保留；
    - 并联接地的 R/L/C/T **不计入** 焊盘三端（用户明确排除）。
    - 非并联、仅单端挂着的 stub（未入 gnd 集合）仍可计入三端。
    """
    if not is_rlct(ref, schematic_res):
        return False
    try:
        return bool(schematic_res.is_parallel(ref))
    except Exception:
        return False


def _is_shunt_stem_rlct(
    ref: str,
    schematic_res,
    dev_to_nets: dict[str, list[str]],
    net_to_devs: dict[str, list[str]],
    *,
    pad_net: str = "",
    _visited: Optional[set[str]] = None,
) -> bool:
    """是否「对地支路的根」——一端只接到并联接地件/串联接地链，不算 copad/colay 腿。

    例：
      - L→C(is_parallel)→GND：L 虽 deg=2、自身非 is_parallel，非焊盘侧仅并地 C
      - C→L(is_parallel)→GND：C 为串联接地链根
      - 可经多级纯 RLCT 串联最终只到并地/地

    不得进焊盘三端「贯通」计数（否则 orphan 假三端会误标 co-lay）。
    """
    if not is_rlct(ref, schematic_res):
        return False
    if _is_parallel_ground_rlct(ref, schematic_res):
        return True
    seen = set(_visited or ())
    if str(ref) in seen:
        return False
    seen.add(str(ref))
    for n in dev_to_nets.get(ref, []):
        if not n or is_gnd_net(n):
            # 直连地脚是合法 stub 腿（L-to-GND），不因有地脚排除
            continue
        if pad_net and n == pad_net:
            continue
        others = [
            str(d) for d in net_to_devs.get(n, [])
            if d and str(d) != str(ref)
        ]
        if not others:
            continue
        # 该侧网上「其它器件」全是并地 RLCT → 对地支路侧（L→C_gnd）
        if all(_is_parallel_ground_rlct(o, schematic_res) for o in others):
            return True
        # 串联接地链：该侧仅 RLCT，且每个都是并地或继续为对地支路根
        if all(is_rlct(o, schematic_res) for o in others) and all(
            _is_parallel_ground_rlct(o, schematic_res)
            or _is_shunt_stem_rlct(
                o, schematic_res, dev_to_nets, net_to_devs,
                pad_net=str(n), _visited=seen,
            )
            for o in others
        ):
            return True
    return False


def _is_jump_only_far_rlct(
    ref: str,
    pad_net: str,
    dev_to_nets: dict[str, list[str]],
) -> bool:
    """非焊盘侧其它网**全部**为 JUMP 隔离网 → 非串联 copad/colay 腿。

    JUMP 只做跨侧隔绝，不参与本侧 copad 双支路贯通。
    例：焊盘上仅另一端接 JUMP 的 RLCT，不得与真串联腿抢「同种类对」席位
    （否则真串联腿被挤掉、分叉对误成 JUMP 附着件）。
    有任一非 JUMP 远端网则保留（真 RF 串联/入口）。
    """
    if not pad_net or not dev_to_nets:
        return False
    far = [
        n for n in dev_to_nets.get(ref, [])
        if n and n != pad_net and not is_gnd_net(n)
    ]
    return bool(far) and all(is_jump_isolation_net(n) for n in far)


def _other_net_active_bias(
    ref: str,
    pad_net: str,
    schematic_res,
    dev_to_nets: dict[str, list[str]],
    net_to_devs: dict[str, list[str]],
) -> int:
    """腿的「朝有源侧」倾向：越大越像干线接到 U/TRX/PA，越小越像 copad 腿。

    用于 ≥3 只 RLCT 同网时选 2 腿：优先另一端接被动/FIL 的，排除朝 U* 有源的。
    """
    bias = 0
    for n in dev_to_nets.get(ref, []):
        if not n or n == pad_net or is_gnd_net(n):
            continue
        for d in net_to_devs.get(n, []):
            ds = str(d or "")
            if not ds or ds == str(ref):
                continue
            if _is_parallel_ground_rlct(ds, schematic_res):
                continue
            if is_rlct(ds, schematic_res):
                bias -= 2
                continue
            try:
                pt = (schematic_res.get_part_type(ds) or "").upper()
            except Exception:
                pt = ""
            if any(
                k in pt
                for k in (
                    "TRANSCEIVER", "RF PA", "RF LNA", "LPAMIF", "SWITCH",
                )
            ):
                bias += 12
            elif any(
                k in pt
                for k in (
                    "FILTER", "SAW", "BAW", "MULTIPLEX", "DIVIDER", "COUPLER",
                )
            ):
                bias -= 1
            elif ds.upper().startswith("U"):
                bias += 10
            else:
                bias += 1
    return bias


def _pad_net_members(
    devs: list[str],
    schematic_res,
    *,
    pad_net: str = "",
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    topology_dev_to_nets: Optional[dict[str, list[str]]] = None,
    topology_net_to_devs: Optional[dict[str, list[str]]] = None,
) -> list[str]:
    """焊盘网上参与「三端」计数的器件。

    计入：
      - 多端口 / IC / 滤波器等非 RLCT
      - 非并地、非「对地支路根」、非「仅 JUMP 远端」的 RLCT
    不计入：
      - 并联接地 RLCT（is_parallel）
      - 对地支路根 / 串联接地链根（如 C→L_gnd、L→C_gnd）
      - 非焊盘侧其它网全部为 JUMP 隔离（跨侧隔绝附着，非本侧串联腿）
      - 纯网络名（ANT 端点名除外）

    对地支路判定优先用 topology_*（全量拓扑）：过滤 RF 网表常丢掉并地
    串联链，若只用 pad 网表会把接地链根误算进贯通三端。
    """
    out: list[str] = []
    seen: set[str] = set()
    d2n = dev_to_nets or {}
    n2d = net_to_devs or {}
    # 对地链可见性：全量拓扑 > 焊盘网表
    d2n_sh = topology_dev_to_nets or d2n
    n2d_sh = topology_net_to_devs or n2d
    for d in devs:
        r = str(d or "").strip()
        if not r or r in seen:
            continue
        try:
            if schematic_res is not None and schematic_res.is_network(r):
                if not is_ant_path_node(r):
                    continue
        except Exception:
            pass
        if _is_parallel_ground_rlct(r, schematic_res):
            continue
        if d2n_sh and n2d_sh and _is_shunt_stem_rlct(
            r, schematic_res, d2n_sh, n2d_sh, pad_net=pad_net,
        ):
            continue
        # JUMP 远端用 pad 网表即可（JUMP 名在过滤 RF 网表中仍在）
        if pad_net and d2n and is_rlct(r, schematic_res) and _is_jump_only_far_rlct(
            r, pad_net, d2n,
        ):
            continue
        seen.add(r)
        out.append(r)
    return out


def _copad_device_adj(signal_infos) -> dict[str, set[str]]:
    """共网邻接图（忽略地网本身上的纯连接）；ANT* 网名作为天线端点。"""
    net_to_devs, _ = build_net_device_maps(signal_infos or [])
    adj: dict[str, set[str]] = defaultdict(set)
    for net, devs in net_to_devs.items():
        if is_gnd_net(net):
            continue
        nodes = list(devs)
        if is_ant_path_node(net) and net not in nodes:
            nodes = [net] + nodes
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def _dist_to_nearest_ant(
    adj: dict[str, set[str]],
    schematic_res=None,
) -> dict[str, int]:
    """多源 BFS：节点 → 到最近 ANT 端点的 hop 数。

    **不从 Transceiver 继续外扩**（通用）：否则 LNA/PA 会经 SDR 反抄近路，
    把「靠 TRX 的焊盘」误判成比「靠天线的焊盘」更近 ANT，分叉/合并对调。
    Transceiver 本身仍可被标距，但不作为中转向其它器件灌水。
    """
    seeds = sorted(n for n in adj if is_ant_path_node(n))
    if not seeds:
        return {}
    dist: dict[str, int] = {}
    q: deque[str] = deque()
    for s in seeds:
        dist[s] = 0
        q.append(s)
    while q:
        u = q.popleft()
        # TRX 为天线侧距离洪泛的汇点，禁止经 TRX 脚网抄到对侧
        if schematic_res is not None and is_transceiver_ref(u, schematic_res):
            continue
        for v in sorted(adj.get(u, ())):
            if v in dist:
                continue
            dist[v] = dist[u] + 1
            q.append(v)
    return dist


def _pad_legs_from_arm_roles(
    pool: list[str],
    schematic_res,
    *,
    pad_net: str,
    d2n: dict[str, list[str]],
    n2d: dict[str, list[str]],
    ant: dict[str, int],
    leg_key,
) -> Optional[tuple[list[str], Optional[str]]]:
    """三端+ 按臂角色选主轴。分不出则 None，交给同种类/度数兜底。

    ant / fe / side 见 ``_pad_rlct_arm_role``。
      - ≥2 ant + 1 fe、无 side → 两 ant 为腿、fe 为干路
      - 1 ant + 1 fe + side → 主轴 ant+fe 为腿、side 为干路
    必须在「同种类成对」之前对**全员**调用：异种天线臂（L+C 分频 T）
    不得被两只同种入/出端 L 裁掉。
    """
    if len(pool) < 3 or not d2n or not n2d or not pad_net:
        return None
    roles = {
        r: _pad_rlct_arm_role(r, pad_net, schematic_res, d2n, n2d)
        for r in pool
    }
    ants = [r for r in pool if roles[r] == "ant"]
    fes = [r for r in pool if roles[r] == "fe"]
    sides = [r for r in pool if roles[r] == "side"]
    if len(ants) >= 2 and len(fes) == 1 and not sides:
        def _ak(r: str):
            return _ant_arm_filter_rank(
                r, pad_net, d2n, n2d, schematic_res,
            ) + tuple(leg_key(r))
        return sorted(ants, key=_ak)[:2], fes[0]
    if len(ants) >= 1 and len(fes) >= 1 and sides:
        ant_leg = min(ants, key=lambda r: (ant.get(r, 10**9), str(r)))
        fe_leg = min(
            fes,
            key=lambda r: (
                -_other_net_active_bias(r, pad_net, schematic_res, d2n, n2d),
                str(r),
            ),
        )
        if ant_leg == fe_leg:
            return None
        trunk = min(sides, key=str) if len(sides) == 1 else max(
            sides,
            key=lambda r: (
                _other_net_active_bias(r, pad_net, schematic_res, d2n, n2d),
                str(r),
            ),
        )
        return [ant_leg, fe_leg], trunk
    return None


def _rf_pin_count(ref: str, d2n: dict[str, list[str]]) -> int:
    n = 0
    for net in d2n.get(str(ref) or "") or []:
        if net and not is_gnd_net(str(net)):
            n += 1
    return n


def _ant_arm_filter_rank(
    ref: str,
    pad_net: str,
    d2n: dict[str, list[str]],
    n2d: dict[str, list[str]],
    schematic_res,
    *,
    max_depth: int = 4,
) -> tuple:
    """多条 ant 臂里：越早碰到 ≥3 口公共滤波越优先；2 口 SAW 靠后。

    避免芯片侧串联 SAW 和真天线对抢两条腿名额。
    """
    def _filt_pins(cur: str) -> int:
        try:
            pt = str(
                getattr(schematic_res, "get_part_type", lambda _r: "")(cur)
                or ""
            ).upper()
        except Exception:
            pt = ""
        if not any(
            k in pt
            for k in (
                "FILTER", "SAW", "FBAR", "FREQUENCY DIVIDER",
                "DIPLEX", "DUPLEX", "MULTIPLEX",
            )
        ):
            return 0
        return _rf_pin_count(cur, d2n)

    best3, best2 = 99, 99
    q: deque[tuple[str, int]] = deque()
    visited: set[str] = {str(ref), str(pad_net)}
    for net in d2n.get(str(ref) or "") or []:
        ns = str(net)
        if not ns or is_gnd_net(ns) or ns == str(pad_net):
            continue
        for d in n2d.get(ns) or []:
            ds = str(d or "").strip()
            if not ds or ds in visited:
                continue
            visited.add(ds)
            q.append((ds, 1))
    guard = 0
    while q and guard < 200:
        guard += 1
        cur, depth = q.popleft()
        if depth > max_depth:
            continue
        pins = _filt_pins(cur)
        if pins >= 3:
            best3 = min(best3, depth)
            continue
        if pins == 2:
            best2 = min(best2, depth)
        if schematic_res is not None and (
            is_pa_lna_ref(cur, schematic_res)
            or is_transceiver_ref(cur, schematic_res)
        ):
            continue
        for net in d2n.get(cur) or []:
            ns = str(net)
            if not ns or is_gnd_net(ns) or ns == str(pad_net):
                continue
            for d in n2d.get(ns) or []:
                ds = str(d or "").strip()
                if not ds or ds in visited:
                    continue
                visited.add(ds)
                q.append((ds, depth + 1))
    if best3 < 99:
        return (0, best3)
    if best2 < 99:
        return (1, best2)
    return (2, 99)


def _leg_nearest_ant_port(
    ref: str,
    pad_net: str,
    d2n: dict[str, list[str]],
    n2d: dict[str, list[str]],
    schematic_res,
    *,
    max_depth: int = 8,
) -> Optional[str]:
    """腿离开焊盘网后，远侧最近的天线整口。

    只认天线口网名或器件（``is_ant_path_node``），不认 ``N77_TRX0_ANTx`` 这类中间网。
    碰到整口即停，不经座子抄到别的口。PA/LNA/TRX 是芯片侧终点，不再往外找口。
    走不到整口 → None（朝 FE 的匹配腿，或 GPS 口名不是 ANTn）。
    """
    if not ref or not pad_net or not d2n or not n2d:
        return None

    def _skip_net(ns: str) -> bool:
        if not ns or is_gnd_net(ns) or ns == str(pad_net):
            return True
        return bool(is_jump_isolation_net(ns))

    q: deque[tuple[str, int]] = deque()
    visited: set[str] = {str(ref)}
    best: Optional[tuple[int, str]] = None
    guard = 0

    def _hit(depth: int, name: str) -> None:
        nonlocal best
        port = canonical_ant_id(name)
        if not port:
            return
        if best is None or depth < best[0] or (
            depth == best[0] and port < best[1]
        ):
            best = (depth, port)

    for net in d2n.get(str(ref)) or []:
        ns = str(net)
        if _skip_net(ns):
            continue
        if is_ant_path_node(ns):
            _hit(1, ns)
            continue
        for d in n2d.get(ns) or []:
            ds = str(d or "").strip()
            if not ds or ds in visited:
                continue
            visited.add(ds)
            q.append((ds, 1))

    while q and guard < 500:
        guard += 1
        cur, depth = q.popleft()
        if depth > max_depth:
            continue
        if is_ant_path_node(cur):
            _hit(depth, cur)
            continue
        if is_connector_ref(cur):
            continue
        if schematic_res is not None and (
            is_pa_lna_ref(cur, schematic_res)
            or is_transceiver_ref(cur, schematic_res)
        ):
            continue
        for net in d2n.get(cur) or []:
            ns = str(net)
            if _skip_net(ns):
                continue
            if is_ant_path_node(ns):
                _hit(depth + 1, ns)
                continue
            for d in n2d.get(ns) or []:
                ds = str(d or "").strip()
                if not ds or ds in visited:
                    continue
                visited.add(ds)
                q.append((ds, depth + 1))
    return None if best is None else best[1]


def _pad_rlct_split_ant_ports(
    rlct: list[str],
    pad_net: str,
    d2n: dict[str, list[str]],
    n2d: dict[str, list[str]],
    schematic_res,
    ant_dist: Optional[dict[str, int]] = None,
) -> list[str]:
    """丢掉异口 ANT 向 RLCT，返回剩余可参选腿（不是整盘否决）。

    远侧整口多于一个：丢掉不是最近那一口的 ANT 向 RLCT。
    FE（走不到整口 / port=None）与并地留下。
    剩余若 ≥2 条 port=None 且仍有 ANT 向：选腿只用 FE 贯通臂，
    同口 ANT 向留给干路。但 ant+fe+side 星型主轴除外
    （两只 port=None 是 fe+side，不得收成对）。
    无异口且不满足双 FE+ANT 向则原样。不够两条则返回短列表。
    """
    if not rlct or not pad_net or not d2n or not n2d:
        return list(rlct)
    port_of: dict[str, Optional[str]] = {}
    for r in rlct:
        rs = str(r)
        port_of[rs] = _leg_nearest_ant_port(
            rs, pad_net, d2n, n2d, schematic_res,
        )
    ant_facing = [r for r in rlct if port_of.get(str(r))]
    fe = [r for r in rlct if not port_of.get(str(r))]
    if len({port_of[str(r)] for r in ant_facing}) > 1:
        def _home_key(r: str) -> tuple:
            p = port_of[str(r)] or ""
            d = (ant_dist or {}).get(str(r), 10**9)
            return (d, p, str(r))
        # 本口 = 天线臂（role=ant）远侧最近整口；side 走到另一口不算本口
        home_cands = [
            r for r in ant_facing
            if _pad_rlct_arm_role(str(r), pad_net, schematic_res, d2n, n2d)
            == "ant"
        ] or list(ant_facing)
        home = port_of[str(min(home_cands, key=_home_key))]
        ant_facing = [r for r in ant_facing if port_of[str(r)] == home]
    want = {str(r) for r in fe} | {str(r) for r in ant_facing}
    kept = [r for r in rlct if str(r) in want]
    if len(fe) >= 2 and ant_facing:
        n_ant = n_fe = n_side = 0
        for r in kept:
            role = _pad_rlct_arm_role(str(r), pad_net, schematic_res, d2n, n2d)
            if role == "ant":
                n_ant += 1
            elif role == "fe":
                n_fe += 1
            elif role == "side":
                n_side += 1
        if not (n_ant >= 1 and n_fe >= 1 and n_side >= 1):
            fe_want = {str(r) for r in fe}
            return [r for r in rlct if str(r) in fe_want]
    return kept


def _pick_copad_legs(
    rlct: list[str],
    schematic_res,
    *,
    pad_net: str = "",
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> tuple[list[str], Optional[str]]:
    """从焊盘 RLCT 中选 2 腿 + 可选排除端（唯一选腿入口）。

    优先级：排除仅 JUMP 远端 → **远侧整口过滤（丢掉异口；双 FE 贯通时
    ANT 向不当腿）** → **臂角色（全员）** → 同 RLC 种类成对
    → 非地度数小 → 朝有源倾向低 → 离 ANT 更远。
    排除端：剩余 RLCT 中朝有源高、更近 ANT 者（不含已丢掉的异口）。
    """
    if len(rlct) < 2:
        return list(rlct), None
    d2n = dev_to_nets or {}
    n2d = net_to_devs or {}
    ant = ant_dist or {}

    # JUMP 隔离附着不得进腿池（否则同种类 C 对抢走真串联 L 腿）
    cand = list(rlct)
    if pad_net and d2n:
        no_jump = [
            r for r in cand
            if not _is_jump_only_far_rlct(r, pad_net, d2n)
        ]
        if len(no_jump) >= 2:
            cand = no_jump
    # 异口丢掉，不是整盘否决；同口 + FE 再选腿
    if pad_net and d2n and n2d:
        filtered = _pad_rlct_split_ant_ports(
            cand, pad_net, d2n, n2d, schematic_res, ant_dist=ant,
        )
        if len(filtered) >= 2:
            cand = filtered
        else:
            return list(filtered), None

    def _leg_key(r: str) -> tuple:
        bias = (
            _other_net_active_bias(r, pad_net, schematic_res, d2n, n2d)
            if d2n and n2d
            else 0
        )
        return (
            nongnd_degree(r, d2n) if d2n else 0,
            bias,
            -ant.get(r, 0),
            str(r),
        )

    # 臂角色必须看全员。同种类先裁会把分频 T 的异种天线臂丢掉，
    # 把入端 L 和一条出端 L 凑成假对（并地 C 再被抬成干路）。
    by_role = _pad_legs_from_arm_roles(
        cand, schematic_res,
        pad_net=pad_net, d2n=d2n, n2d=n2d, ant=ant, leg_key=_leg_key,
    )
    if by_role is not None:
        return by_role

    by_kind: dict[str, list[str]] = defaultdict(list)
    for r in cand:
        by_kind[rlc_kind(r, schematic_res) or "?"].append(r)
    pool = list(cand)
    for _k, group in sorted(by_kind.items(), key=lambda x: (-len(x[1]), x[0])):
        if len(group) >= 2:
            pool = group
            break

    by_role = _pad_legs_from_arm_roles(
        pool, schematic_res,
        pad_net=pad_net, d2n=d2n, n2d=n2d, ant=ant, leg_key=_leg_key,
    )
    if by_role is not None:
        return by_role

    legs = sorted(pool, key=_leg_key)[:2]
    # 排除端可来自原 rlct（含 JUMP 附着），但优先非腿 cand
    rest = [r for r in cand if r not in set(legs)]
    if not rest:
        rest = [r for r in rlct if r not in set(legs)]
    if pad_net and d2n and n2d and rest:
        rest_kept = _pad_rlct_split_ant_ports(
            rest, pad_net, d2n, n2d, schematic_res, ant_dist=ant,
        )
        if rest_kept:
            rest = rest_kept
    if not rest:
        return legs, None
    trunk = max(
        rest,
        key=lambda r: (
            (
                _other_net_active_bias(r, pad_net, schematic_res, d2n, n2d)
                if d2n and n2d
                else 0
            ),
            -ant.get(r, 0),
            str(r),
        ),
    )
    return legs, trunk


def _pad_rlct_arm_role(
    ref: str,
    pad_net: str,
    schematic_res,
    d2n: dict[str, list[str]],
    n2d: dict[str, list[str]],
    *,
    max_depth: int = 6,
    home_ant: str = "",
) -> str:
    """焊盘上某 RLCT 臂离开 pad_net 后的角色：ant / fe / side。

    从 ref 的**非 pad 网**向外 BFS（不走回 pad_net 兄弟腿；不走 JUMP 网）：
      - **本口天线臂**（远侧最近整口 = 本页）→ ant。L-L 桥也算本口臂。
      - **异口臂**（远侧整口 ≠ 本页）→ side，禁止从本口 FE/ANT 臂跳过去。
      - 短距（≤2）先碰到 PA/LNA → fe
      - 其它网名含 ANTx 且对端是串联 L → 无本口证据时 side
      - 否则 side
    """
    def _skip_net(ns: str) -> bool:
        if not ns or is_gnd_net(ns) or ns == str(pad_net):
            return True
        if is_jump_isolation_net(ns):
            return True
        return False

    other_nets = [
        str(n)
        for n in (d2n.get(ref) or [])
        if str(n) and not _skip_net(str(n))
    ]
    if not other_nets:
        return "side"

    dest = _leg_nearest_ant_port(ref, pad_net, d2n, n2d, schematic_res)
    hp = ant_port_number(home_ant)
    dp = ant_port_number(dest or "")
    if hp and dp:
        if dp != hp:
            return "side"
        return "ant"

    # 段间桥（旁路汇入另一 L 簇）：网名含 ANTx 且**首跳是串联 RLCT**
    # 非旁路：同名模式但首跳是 C/并地再进 FIL（天线主轴）
    # 旧逻辑凡 *ANTn* 一律 side → 本口天线主轴被误判，门禁盖不到该口。
    for ns in other_nets:
        nu = ns.upper().replace("'", "")
        if not (re.search(r"ANT\d+", nu) and not nu.startswith("ANT")):
            continue
        peers = [
            str(d)
            for d in (n2d.get(ns) or [])
            if str(d) and str(d) != str(ref)
        ]
        if not peers:
            continue
        # 旁路特征：对端是**串联电感 L**（接到另一 L 簇/焊盘），不是 C→FIL 天线主轴
        #   side: 首跳串联 L 桥；ant: 首跳 C 再进 FIL（C 不是 L 桥）
        series_L = [
            p for p in peers
            if (rlc_kind(p, schematic_res) or "").upper() == "L"
            and not is_shunt_device(p, schematic_res, d2n)
        ]
        if series_L and not any(
            is_pa_lna_ref(p, schematic_res) or is_ant_path_node(p)
            for p in peers
        ):
            return "side"

    q: deque[tuple[str, int]] = deque()
    visited: set[str] = {str(ref)}
    for net in other_nets:
        for d in n2d.get(net) or []:
            ds = str(d or "").strip()
            if not ds or ds in visited:
                continue
            visited.add(ds)
            q.append((ds, 1))
    fe_depth: Optional[int] = None
    ant_depth: Optional[int] = None
    guard = 0
    while q and guard < 400:
        guard += 1
        cur, depth = q.popleft()
        if depth > max_depth:
            continue
        if is_ant_path_node(cur):
            ant_depth = depth if ant_depth is None else min(ant_depth, depth)
            continue
        if is_pa_lna_ref(cur, schematic_res) or is_transceiver_ref(
            cur, schematic_res,
        ):
            fe_depth = depth if fe_depth is None else min(fe_depth, depth)
            continue
        try:
            pt = str(
                getattr(schematic_res, "get_part_type", lambda _r: "")(cur)
                or ""
            ).upper()
        except Exception:
            pt = ""
        if any(
            k in pt
            for k in (
                "FILTER", "SAW", "FBAR", "FREQUENCY DIVIDER",
                "DIPLEX", "DUPLEX", "MULTIPLEX",
            )
        ):
            # 公共滤波口 ≈ 天线侧
            ant_depth = depth if ant_depth is None else min(ant_depth, depth)
            continue
        for net in d2n.get(cur) or []:
            ns = str(net)
            if _skip_net(ns):
                continue
            # 旁路 *ANTn*→串联 L 桥不再扩（避免旁路簇串进主轴判定）
            nu = ns.upper().replace("'", "")
            if re.search(r"ANT\d+", nu) and not nu.startswith("ANT"):
                peers_n = [
                    str(d)
                    for d in (n2d.get(ns) or [])
                    if str(d) and str(d) != cur
                ]
                if any(
                    is_rlct(p, schematic_res)
                    and not is_shunt_device(p, schematic_res, d2n)
                    for p in peers_n
                ):
                    continue
            for d in n2d.get(ns) or []:
                ds = str(d or "").strip()
                if not ds or ds in visited:
                    continue
                visited.add(ds)
                q.append((ds, depth + 1))

    # 短距 FE 优先（主 FE 腿）
    if fe_depth is not None and fe_depth <= 2:
        return "fe"
    if ant_depth is not None and (
        fe_depth is None or ant_depth <= fe_depth
    ):
        return "ant"
    if fe_depth is not None:
        return "fe"
    return "side"


def _copad_pad_rlct_extras(
    pad_net: str,
    three: list[str],
    schematic_res,
    *,
    ant_dist: Optional[dict[str, int]] = None,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
) -> tuple[str, ...]:
    """焊盘上未进三端的共网 RLCT（并联挂件）。

    须：共网、未被选成 2 腿+hub 的 RLCT 进入 extras（再并入
    trunk_before / trunk_after），不得蒸发。
    可：焊盘上没有并联则 extras 为空。
    禁：异口 ANT 向、仅 JUMP 远端；不得把并联收成第三只腿。
    """
    n2d = net_to_devs or {}
    d2n = dev_to_nets or {}
    if not pad_net or not n2d:
        return ()
    three_set = {str(x) for x in (three or []) if x}
    pool: list[str] = []
    seen: set[str] = set()
    for d in n2d.get(str(pad_net), []) or []:
        ds = str(d or "").strip()
        if not ds or ds in seen or ds in three_set:
            continue
        seen.add(ds)
        if not is_rlct(ds, schematic_res):
            continue
        if d2n and _is_jump_only_far_rlct(ds, str(pad_net), d2n):
            continue
        pool.append(ds)
    if not pool:
        return ()
    kept = _pad_rlct_split_ant_ports(
        pool, str(pad_net), d2n, n2d, schematic_res, ant_dist=ant_dist,
    )
    home_ports = {
        p
        for r in three
        if (p := _leg_nearest_ant_port(
            str(r), str(pad_net), d2n, n2d, schematic_res,
        ))
    }
    if home_ports:
        kept = [
            r for r in kept
            if (
                _leg_nearest_ant_port(
                    str(r), str(pad_net), d2n, n2d, schematic_res,
                )
                in (None, *home_ports)
            )
        ]
    dist = ant_dist or {}
    kept = sorted(kept, key=lambda r: (int(dist.get(r, 10**9)), str(r)))
    return tuple(kept)


def _canonicalize_pad_ends(
    members: list[str],
    schematic_res,
    ant_dist: dict[str, int],
    dev_to_nets: dict[str, list[str]],
    *,
    pad_net: str = "",
    net_to_devs: Optional[dict[str, list[str]]] = None,
) -> Optional[list[str]]:
    """焊盘有效端 →「2 腿 + 1 排除端」，按 ANT 距排序供展示。

    异口 ANT 向丢掉后在剩余成员上选腿；不够两条同口贯通则不成组。
    三端仍是 2 腿+hub。未进三端的共网 RLCT 由 ``_copad_pad_rlct_extras``
    留下，禁止蒸发（并入结构 pad extras / trunk_before / trunk_after）。
    """
    n2d = net_to_devs or {}
    d2n = dev_to_nets or {}
    rlct = [m for m in members if is_rlct(m, schematic_res)]
    non = [m for m in members if not is_rlct(m, schematic_res)]
    if len(rlct) < 2:
        return None
    if not non and len(rlct) < 3:
        return None

    legs, trunk_rlct = _pick_copad_legs(
        rlct, schematic_res,
        pad_net=pad_net, dev_to_nets=dev_to_nets, net_to_devs=n2d,
        ant_dist=ant_dist,
    )
    if len(legs) < 2:
        return None

    if non:
        hub = min(non, key=lambda r: (ant_dist.get(r, 10**9), str(r)))
        three = [hub, legs[0], legs[1]]
    else:
        if not trunk_rlct:
            return None
        three = [trunk_rlct, legs[0], legs[1]]
    three = sorted(three, key=lambda r: (ant_dist.get(r, 10**9), str(r)))
    # 三端已定。共网未进三端的 RLCT 由 _copad_pad_rlct_extras 留下，
    # _make_structure 并入 pad / trunk，禁止蒸发。
    return three


def _other_rf_nets(ref: str, pad_net: str, d2n: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for n in d2n.get(str(ref) or "") or []:
        ns = str(n)
        if ns and not is_gnd_net(ns) and ns != str(pad_net):
            out.append(ns)
    return out


def _far_meet_node(
    a: str,
    b: str,
    pad_net: str,
    n2d: dict[str, list[str]],
    d2n: dict[str, list[str]],
    *,
    max_depth: int = 6,
) -> tuple[Optional[str], int]:
    """离开焊盘后两臂第一次汇合的器件。禁止走回 pad_net。"""
    def _reach(start: str) -> dict[str, int]:
        dist: dict[str, int] = {}
        q: deque[tuple[str, int]] = deque()
        seen_d = {str(start)}
        seen_n = {str(pad_net)}
        for ns in _other_rf_nets(start, pad_net, d2n):
            seen_n.add(ns)
            for d in n2d.get(ns) or []:
                ds = str(d or "").strip()
                if not ds or ds in seen_d:
                    continue
                seen_d.add(ds)
                dist[ds] = 1
                q.append((ds, 1))
        guard = 0
        while q and guard < 400:
            guard += 1
            cur, depth = q.popleft()
            if depth >= max_depth:
                continue
            for net in d2n.get(cur) or []:
                ns = str(net)
                if not ns or is_gnd_net(ns) or ns in seen_n:
                    continue
                seen_n.add(ns)
                for d in n2d.get(ns) or []:
                    ds = str(d or "").strip()
                    if not ds or ds in seen_d:
                        continue
                    seen_d.add(ds)
                    dist[ds] = depth + 1
                    q.append((ds, depth + 1))
        return dist

    da, db = _reach(a), _reach(b)
    common = (set(da) & set(db)) - {str(a), str(b)}
    if not common:
        return None, 99
    best = min(common, key=lambda x: (max(da[x], db[x]), str(x)))
    return best, max(da[best], db[best])


def _partition_pad_by_far_meet(
    rlct: list[str],
    pad_net: str,
    n2d: dict[str, list[str]],
    d2n: dict[str, list[str]],
) -> Optional[list[list[str]]]:
    """≥4 只贯通 RLCT：按远侧汇合点分成两个对。汇合点必须不同。"""
    items = [str(x) for x in rlct if x]
    if len(items) < 4:
        return None
    items = items[:4]
    w, x, y, z = items[0], items[1], items[2], items[3]
    parts = [
        ((w, x), (y, z)),
        ((w, y), (x, z)),
        ((w, z), (x, y)),
    ]
    best: Optional[tuple] = None
    for p1, p2 in parts:
        m1, d1 = _far_meet_node(p1[0], p1[1], pad_net, n2d, d2n)
        m2, d2 = _far_meet_node(p2[0], p2[1], pad_net, n2d, d2n)
        if not m1 or not m2 or m1 == m2:
            continue
        score = (d1 + d2, str(m1), str(m2))
        if best is None or score < best[0]:
            best = (score, [p1[0], p1[1]], [p2[0], p2[1]])
    if best is None:
        return None
    return [best[1], best[2]]


def _list_pad_tripods(
    schematic_res,
    signal_infos,
    ant_dist: dict[str, int],
) -> list[tuple[str, list[str]]]:
    """候选焊盘网 → 规范三端 (net, ordered)。"""
    net_to_devs, dev_to_nets = build_net_device_maps(signal_infos or [])
    # 对地串联链（并地件）常仅在全量拓扑中完整，焊盘成员过滤须看见它
    top_sig = _branch_topology_signal_infos(schematic_res, signal_infos)
    if top_sig and top_sig is not signal_infos:
        top_n2d, top_d2n = build_net_device_maps(top_sig)
    else:
        top_n2d, top_d2n = net_to_devs, dev_to_nets
    out: list[tuple[str, list[str]]] = []
    for net, devs in net_to_devs.items():
        if is_gnd_net(net):
            continue
        members = _pad_net_members(
            devs, schematic_res,
            pad_net=str(net),
            dev_to_nets=dev_to_nets,
            net_to_devs=net_to_devs,
            topology_dev_to_nets=top_d2n,
            topology_net_to_devs=top_n2d,
        )
        through = [
            m for m in members
            if is_rlct(m, schematic_res)
            and _other_rf_nets(m, str(net), dev_to_nets)
        ]
        groups = (
            _partition_pad_by_far_meet(
                through, str(net), net_to_devs, dev_to_nets,
            )
            if len(through) >= 4
            else None
        )
        if groups:
            for g in groups:
                if len(g) >= 2:
                    out.append((str(net), list(g)))
            continue
        ordered = _canonicalize_pad_ends(
            members, schematic_res, ant_dist, dev_to_nets,
            pad_net=str(net), net_to_devs=net_to_devs,
        )
        if not ordered or len(ordered) < 3:
            continue
        out.append((str(net), list(ordered)))
    out.sort(key=lambda x: (min(ant_dist.get(d, 10**9) for d in x[1]), x[0]))
    return out


def _pair_from_pad_ends(
    ordered: list[str],
    net: str,
    *,
    role: str,
    schematic_res,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> CoPadPair:
    """由规范三端取 copad 对（腿选取统一走 _pick_copad_legs）。"""
    del role
    rlct = [d for d in ordered if is_rlct(d, schematic_res)]
    non = [d for d in ordered if not is_rlct(d, schematic_res)]
    if len(rlct) < 2:
        if len(ordered) >= 2:
            la, lb = pair_key(ordered[0], ordered[-1])
            return CoPadPair(leg_a=la, leg_b=lb, shared_nets=(net,), trunk_ref="")
        la, lb = pair_key(ordered[0], ordered[0]) if ordered else ("", "")
        return CoPadPair(leg_a=la, leg_b=lb, shared_nets=(net,), trunk_ref="")

    legs, trunk_rlct = _pick_copad_legs(
        rlct, schematic_res,
        pad_net=net, dev_to_nets=dev_to_nets, net_to_devs=net_to_devs,
        ant_dist=ant_dist,
    )
    if len(legs) < 2:
        la, lb = pair_key(rlct[0], rlct[1])
        return CoPadPair(leg_a=la, leg_b=lb, shared_nets=(net,), trunk_ref="")
    la, lb = pair_key(legs[0], legs[1])
    if non:
        trunk = min(non, key=str)
    else:
        trunk = trunk_rlct or ""
    return CoPadPair(
        leg_a=la, leg_b=lb, shared_nets=(net,), trunk_ref=trunk,
    )


def _pad_rlct_legs(ordered: list[str], schematic_res) -> set[str]:
    return {d for d in ordered if is_rlct(d, schematic_res)}


def _pad_multiport(ordered: list[str], schematic_res) -> Optional[str]:
    non = [d for d in ordered if not is_rlct(d, schematic_res)]
    return non[0] if len(non) == 1 else None


def _nets_bridge_rlct_legs(
    legs_a: set[str],
    legs_b: set[str],
    net_to_devs: dict[str, list[str]],
    exclude_nets: set[str],
) -> tuple[str, ...]:
    """两焊盘「双腿」桥连证据（不含地、不含焊盘自身网）。

    弱规则（已废）：任一网只要各碰到一侧任意 1 腿 → 假 copad
    （单网只连一侧腿却把滤波盘与旁路盘收成 bridge，抢走侧支）。

    现规则（须双腿证据，与 paths 阶段 n_real≥2 同强度）：
      A) 单网同时覆盖双方**全部**腿，或
      B) 两条边构成完备匹配：legA0—legB_i 与 legA1—legB_j（i≠j）
         各至少有一条共网（两网可不同）
    返回桥相关器件（稳定排序），无则 ()。
    """
    if not legs_a or not legs_b or legs_a & legs_b:
        return ()
    la = {str(x) for x in legs_a if x}
    lb = {str(x) for x in legs_b if x}
    if len(la) < 2 or len(lb) < 2:
        return ()

    # 每网：与两侧腿的交集
    net_hits: list[tuple[str, set[str], set[str], list[str]]] = []
    for net, devs in sorted(net_to_devs.items(), key=lambda x: str(x[0])):
        ns = str(net)
        if ns in exclude_nets or is_gnd_net(ns):
            continue
        s = {str(d) for d in (devs or []) if d}
        ha, hb = s & la, s & lb
        if ha and hb:
            net_hits.append((ns, ha, hb, sorted(s)))

    # A) 单网盖满双方双腿
    for _ns, ha, hb, members in net_hits:
        if la <= ha and lb <= hb:
            return tuple(members)

    # B) 完备双匹配：两只 A 腿分别连到两只不同 B 腿
    las, lbs = sorted(la), sorted(lb)
    # 邻接：a -> 可到达的 b 集合
    adj: dict[str, set[str]] = {a: set() for a in las}
    edge_members: dict[tuple[str, str], set[str]] = {}
    for _ns, ha, hb, members in net_hits:
        for a in ha:
            for b in hb:
                adj.setdefault(a, set()).add(b)
                edge_members.setdefault((a, b), set()).update(members)

    b0, b1 = lbs[0], lbs[1]
    # 两种 B 分配
    for x, y in ((b0, b1), (b1, b0)):
        if x in adj.get(las[0], ()) and y in adj.get(las[1], ()):
            brid = set()
            brid |= edge_members.get((las[0], x), set())
            brid |= edge_members.get((las[1], y), set())
            if brid:
                return tuple(sorted(brid))
    return ()


def _pad_ant_facing_score(
    ordered: list[str],
    schematic_res,
    dev_to_nets: dict[str, list[str]],
) -> int:
    """焊盘「靠天线」分数，越小越靠 ANT（通用类别 + 拓扑，无位号）。

    - 焊盘器件直接挂在 ANT* 网：强天线侧
    - hub 为 Filter/Mux/SAW：天线侧
    - hub 为 PA/LNA：收发侧
    - hub 为 Transceiver：强收发侧
    """
    score = 0
    for d in ordered:
        for net in dev_to_nets.get(d, []):
            if is_ant_path_node(net) or str(net).upper().startswith("ANT"):
                score -= 50
                break
    non = [d for d in ordered if not is_rlct(d, schematic_res)]
    if non:
        hub = min(non, key=str)
        dev = make_device(hub, schematic_res)
        if dev.is_ant:
            score -= 40
        elif dev.is_sdr:
            score += 40
        elif dev.is_pa_lna:
            score += 20
        else:
            pt = (dev.part_type or "").upper()
            if any(
                k in pt
                for k in (
                    "FILTER", "MULTIPLEX", "SAW", "BAW", "DIPLEX",
                    "DUPLEX", "FREQUENCY DIVIDER", "DIVIDER",
                )
            ):
                score -= 20
            elif dev.is_ic or dev.is_lpf:
                score -= 5
    return score


def _order_pads_by_ant(
    n1: str, o1: list[str], n2: str, o2: list[str], ant_dist: dict[str, int],
    schematic_res=None,
    signal_infos=None,
) -> tuple[str, list[str], str, list[str]]:
    """近天线 = 分叉，远天线 = 合并。

    1) **优先全局 ANT hop**（已禁止经 TRX 外扩；拓扑真值）
    2) hop 相同再比朝向分（直连 ANT 网 / Filter vs LNA/TRX 类别）

    禁止：朝向分压过 hop——全 RLCT 焊盘默认分 0 会把芯片侧
    判成「更靠 ANT」，LNA 焊盘 +20 反而更远，导致分叉/合并颠倒、
    双支路 ANT→TRX 门禁全灭、完整 Co-pad 掉成 orphan colay。
    """
    d1 = min(ant_dist.get(d, 10**9) for d in o1)
    d2 = min(ant_dist.get(d, 10**9) for d in o2)
    if d1 < d2:
        return n1, o1, n2, o2
    if d2 < d1:
        return n2, o2, n1, o1
    # hop 相同：朝向分
    if schematic_res is not None and signal_infos is not None:
        _, dev_to_nets = build_net_device_maps(signal_infos)
        s1 = _pad_ant_facing_score(o1, schematic_res, dev_to_nets)
        s2 = _pad_ant_facing_score(o2, schematic_res, dev_to_nets)
        if s1 != s2:
            if s1 < s2:
                return n1, o1, n2, o2
            return n2, o2, n1, o1
    if n1 <= n2:
        return n1, o1, n2, o2
    return n2, o2, n1, o1


def _shortest_device_path(
    adj: dict[str, set[str]],
    start: str,
    ends: set[str],
    *,
    blocked: set[str],
    max_hop: int = 12,
) -> Optional[list[str]]:
    """BFS 最短路径 start→任一 ends；blocked 不可作中间点（起终点可在 ends）。"""
    if start in ends:
        return [start]
    q: deque[tuple[str, list[str]]] = deque([(start, [start])])
    seen = {start} | (blocked - {start} - ends)
    while q:
        u, path = q.popleft()
        if len(path) > max_hop:
            continue
        for v in sorted(adj.get(u, ())):
            if v in seen and v not in ends:
                continue
            np = path + [v]
            if v in ends:
                return np
            if v in blocked:
                continue
            seen.add(v)
            q.append((v, np))
    return None


def _devices_share_signal_net(
    a: str,
    b: str,
    dev_to_nets: dict[str, list[str]],
) -> bool:
    sa = {n for n in dev_to_nets.get(a, []) if n and not is_gnd_net(n)}
    sb = {n for n in dev_to_nets.get(b, []) if n and not is_gnd_net(n)}
    return bool(sa & sb)


def _format_branch_nodes(
    path: list[str],
    schematic_res,
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
    *,
    exclude: Optional[set[str]] = None,
) -> tuple[str, ...]:
    """支路序列：主路径器件 + 仅出现在**相邻两跳共网**上的旁挂 RLCT。

    双 stub（path 恰两端且无共网）：原样返回两端，展示层用「|」连接，不冒充串联。
    exclude：本结构其它 copad 腿等，禁止当旁挂灌入（同焊盘另一腿会共网）。
    旁挂只收 RLCT，不收多端口/IC（避免把壳脚邻域扫成巨串）。
    """
    if not path:
        return ()
    if (
        len(path) == 2
        and not _devices_share_signal_net(path[0], path[1], dev_to_nets)
    ):
        return (path[0], path[1])

    ban = set(exclude or ())
    path_set = set(path)
    out: list[str] = [path[0]]
    used_extra: set[str] = {path[0]}
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        nets_a = set(dev_to_nets.get(a, []))
        nets_b = set(dev_to_nets.get(b, []))
        shared = [
            n for n in nets_a & nets_b
            if n and not is_gnd_net(n)
        ]
        extras: list[str] = []
        for net in shared:
            for other in net_to_devs.get(net, []):
                if other in path_set or other in used_extra or other in ban:
                    continue
                try:
                    if schematic_res.is_network(other) and not is_ant_path_node(other):
                        continue
                except Exception:
                    pass
                # 旁挂只收被动 RLCT；多端口/IC 不进支路展示串
                if not is_rlct(other, schematic_res):
                    continue
                extras.append(other)
                used_extra.add(other)
        out.extend(sorted(extras))
        out.append(b)
        used_extra.add(b)
    return tuple(out)


def _join_branch_devs(
    devs: tuple[str, ...],
    dev_to_nets: Optional[dict[str, list[str]]] = None,
) -> str:
    """支路展示：串联用 — ；双 stub 无共网用 | 。"""
    if not devs:
        return ""
    if (
        len(devs) == 2
        and dev_to_nets is not None
        and not _devices_share_signal_net(devs[0], devs[1], dev_to_nets)
    ):
        return f"{devs[0]} | {devs[1]}"
    return " — ".join(devs)


def _is_real_device(ref: str, schematic_res) -> bool:
    try:
        if schematic_res.is_network(ref) and not is_ant_path_node(ref):
            return False
    except Exception:
        pass
    return bool(ref)


def _copad_local_adj(
    full_adj: dict[str, set[str]],
    core: set[str],
    legs: set[str],
    schematic_res,
    net_to_devs: dict[str, list[str]],
    dev_to_nets: Optional[dict[str, list[str]]] = None,
) -> dict[str, set[str]]:
    """结构局部邻接（通用，两阶段）：

    1) **种子**：焊盘核 + 凡与腿共网的被动/滤波器件（含 FIL/Divider）
       PA/LNA/TRX 不进局部图（焊盘核 hub 也不是夹心馅）
    2) **RLCT 链扩展**：若某网已含 local 中的 RLCT，则把该网上其它 RLCT 纳入
       （支撑支路多级 LC 串联；**不**借此引入新的多端口脚网）

    不从多端口的「非腿网」扫全板。
    实现：worklist 从腿/已纳入 RLCT 的网扩展，禁止对全网表 while 扫描
    （全量 O(n²) 焊盘对时旧实现会整体假死）。
    """
    # device→nets：优先调用方缓存；缺省时从 net_to_devs 反建一次
    if dev_to_nets is None:
        d2n: dict[str, list[str]] = defaultdict(list)
        for net, devs in net_to_devs.items():
            for d in devs or []:
                ds = str(d)
                if ds and net not in d2n[ds]:
                    d2n[ds].append(net)
        dev_to_nets = d2n

    local: set[str] = set()
    for x in core:
        xs = str(x) if x else ""
        if xs and not _is_active_fe_through_ref(xs, schematic_res):
            local.add(xs)
    # 种子：腿所在网的被动/滤波器件（含 FIL/Divider）；JUMP 网不扩。
    # PA/LNA/TRX 即使共网也不进局部图，否则会踩过芯片配到对侧匹配网。
    for leg in legs:
        for net in dev_to_nets.get(str(leg), []) or []:
            if not net or is_gnd_net(net) or is_jump_isolation_net(net):
                continue
            through_n = sum(
                1
                for x in (net_to_devs.get(net, []) or [])
                if x and is_rlct(str(x), schematic_res)
            )
            for d in net_to_devs.get(net, []) or []:
                ds = str(d)
                if not ds or not _is_real_device(ds, schematic_res):
                    continue
                if _is_active_fe_through_ref(ds, schematic_res):
                    continue
                # 同网两组对：只纳入本结构核，禁止抄到另一组腿
                if through_n >= 4 and ds not in core:
                    continue
                local.add(ds)

    # RLCT 链扩展：只从已纳入的 RLCT 的网往外长，不扫全表；不经 JUMP
    work: deque[str] = deque(
        d for d in local if is_rlct(d, schematic_res)
    )
    in_q = set(work)
    while work:
        cur = work.popleft()
        for net in dev_to_nets.get(cur, []) or []:
            if not net or is_gnd_net(net) or is_jump_isolation_net(net):
                continue
            through_n = sum(
                1
                for x in (net_to_devs.get(net, []) or [])
                if x and is_rlct(str(x), schematic_res)
            )
            for d in net_to_devs.get(net, []) or []:
                ds = str(d)
                if not ds or ds in local:
                    continue
                if not _is_real_device(ds, schematic_res):
                    continue
                if not is_rlct(ds, schematic_res):
                    continue
                if through_n >= 4 and ds not in core:
                    continue
                local.add(ds)
                if ds not in in_q:
                    in_q.add(ds)
                    work.append(ds)

    core_s = {str(x) for x in core if x}
    for node, nbrs in full_adj.items():
        ns = str(node or "")
        if not ns or ns in local:
            continue
        if not _is_active_fe_through_ref(ns, schematic_res):
            continue
        if len({str(x) for x in nbrs} & core_s) >= 2:
            local.add(ns)

    sub: dict[str, set[str]] = defaultdict(set)
    # 邻接：仅保留「存在非 JUMP 共网」的边，禁止 JUMP 当桥
    def _share_non_jump(a: str, b: str) -> bool:
        sa = {
            n for n in (dev_to_nets.get(a, []) or [])
            if n and not is_gnd_net(n) and not is_jump_isolation_net(n)
        }
        sb = {
            n for n in (dev_to_nets.get(b, []) or [])
            if n and not is_gnd_net(n) and not is_jump_isolation_net(n)
        }
        return bool(sa & sb)

    for u in local:
        for v in full_adj.get(u, ()):
            if v in local and _share_non_jump(str(u), str(v)):
                sub[u].add(v)
                sub[v].add(u)
    # 同网共现边：只遍历 local 器件触及的非 JUMP 网
    seen_nets: set[str] = set()
    for u in local:
        for net in dev_to_nets.get(u, []) or []:
            if (
                not net or is_gnd_net(net) or net in seen_nets
                or is_jump_isolation_net(net)
            ):
                continue
            seen_nets.add(net)
            pure = [
                str(d) for d in (net_to_devs.get(net, []) or [])
                if d and str(d) in local
            ]
            for i, a in enumerate(pure):
                for b in pure[i + 1 :]:
                    sub[a].add(b)
                    sub[b].add(a)
    return sub


def _is_active_fe_through_ref(ref: str, schematic_res) -> bool:
    """夹心中段禁止穿越的有源前端：PA/LNA/TRX。FIL/耦合器不是。"""
    if not ref or schematic_res is None:
        return False
    s = str(ref)
    return is_pa_lna_ref(s, schematic_res) or is_transceiver_ref(s, schematic_res)


def _path_uses_foreign_legs(
    path: list[str],
    flegs: list[str],
    mlegs: list[str],
    fa: str,
    mb: str,
) -> bool:
    """路径中间是否经过本结构其它 copad 腿（禁止 stub 腿借 through 腿绕行）。"""
    foreign = (set(flegs) | set(mlegs)) - {fa, mb}
    return any(n in foreign for n in path[1:-1])


def _path_cost(
    path: Optional[list[str]],
    fa: str,
    mb: str,
    schematic_res,
    *,
    dual_stub: bool = False,
) -> int:
    """支路代价：贯通路径=长度+异型罚分；双 stub 配对中等罚分；完全无匹配大罚分。"""
    if dual_stub:
        kind_pen = 0
        ka, kb = rlc_kind(fa, schematic_res), rlc_kind(mb, schematic_res)
        if ka and kb and ka != kb:
            kind_pen = 3
        return 100 + kind_pen  # 优于乱绕，劣于真贯通
    if path is None:
        return 10_000
    kind_pen = 0
    ka, kb = rlc_kind(fa, schematic_res), rlc_kind(mb, schematic_res)
    if ka and kb and ka != kb:
        kind_pen = 3
    return len(path) + kind_pen


def _path_follows_ant_to_trx(
    path: list[str],
    ant_dist: Optional[dict[str, int]],
    *,
    pad_core: Optional[set[str]] = None,
    schematic_res=None,
) -> bool:
    """路径是否沿 ANT→TRX 不逆行。

    **终点优先**（抗全局 ant_dist 脆弱性）：
      若路径收官到 **PA/LNA/TRX** → 整段合法。
      星型网/多路径下中继 RLCT 的全局 dist 常非单调
      （例：L_BR@4 → L_FE@3 → U_LNA@2），不得因中间 hop 减小否决真 FE 支路。

    无 FE 终点时，才用 ant_dist 单调门禁防朝天线 stub：
      ant_dist = 到最近 ANT 的 hop，越小越靠天线；
      离开焊盘核后距离应单调不减；出现减小 = 朝天线逆行
      （经 JUMP 回灌或朝天线 far-stub 时距离减小）。

    无 FE 时的例外（禁止「任意最后一跳」豁免——否则 2 节点逆向永远放行）：
      - 同焊盘核内腿↔trunk 可短暂更靠 ANT
      - 最后一跳落到**合并腿/焊盘核**（核内另一支更短）
      - 落到 **Coupler**（多口 RF 枢纽；全局 ant_dist 常经它口被抄近路）
    缺距离的节点跳过。
    """
    if not path or len(path) < 2:
        return True
    # 终点是真 FE → 不看中间 dist（找终点判定）
    if schematic_res is not None:
        end = str(path[-1] or "")
        if end and (
            is_pa_lna_ref(end, schematic_res)
            or is_transceiver_ref(end, schematic_res)
        ):
            return True
    if not ant_dist:
        return True
    core = {str(x) for x in (pad_core or ()) if x}
    for i in range(len(path) - 1):
        a, b = str(path[i]), str(path[i + 1])
        da, db = ant_dist.get(a), ant_dist.get(b)
        if da is None or db is None:
            continue
        if int(db) >= int(da):
            continue
        # 朝天线回退（无 FE 终点）
        if a in core and b in core:
            continue  # 焊盘核内
        if schematic_res is not None and is_coupler_ref(b, schematic_res):
            continue  # Coupler 多口 dist 不可靠，不按单调否决
        if i == len(path) - 2 and b in core:
            continue  # 收官到合并腿/核
        return False
    return True


def _match_branch_paths(
    flegs: list[str],
    mlegs: list[str],
    adj: dict[str, set[str]],
    core_hubs: set[str],
    schematic_res,
    *,
    ant_dist: Optional[dict[str, int]] = None,
    pad_core: Optional[set[str]] = None,
) -> list[list[str]]:
    """分叉腿 ↔ 合并腿：枚举完美匹配，取总代价最小（2 腿即二分图最优）。

    - 局部子图内 BFS（上限=|V|），无魔法 max_hop
    - 中间禁止经过其它 copad 腿（stub 不得借 through 桥乱绕）
    - 中段禁止 PA/LNA/TRX（穿过芯片不是被动夹心；FIL/耦合器仍可）
    - 贯通路径须 ANT→TRX 不逆行（有 ant_dist 时；核内除外）
    - 无合法贯通时：记为双 stub 支路 [fa, mb]（展示为两端开放，非串联）
    - 核内滤波等被动多端口可共用
    """
    from itertools import permutations

    flegs = list(flegs)
    mlegs = list(mlegs)
    n = min(len(flegs), len(mlegs), 2)
    flegs, mlegs = flegs[:n], mlegs[:n]
    hop_cap = max(2, len(adj))
    core = set(pad_core or ()) | set(core_hubs) | set(flegs) | set(mlegs)
    # 夹心中段不得过 PA/LNA/TRX；FIL/耦合器仍可走。核内滤波 hub 仍可共用。
    # 同时邻接两盘腿的 LNA/PA 是夹心 IC 支路，允许 腿—IC—腿。
    active_fe = {
        d for d in adj
        if _is_active_fe_through_ref(str(d), schematic_res)
    }
    sandwich_ic = {
        d for d in active_fe
        if len(set(adj.get(d, ())) & core) >= 2
    }
    active_fe_block = active_fe - sandwich_ic

    best_paths: Optional[list[list[str]]] = None
    best_cost = 10**9

    for perm in permutations(mlegs):
        paths: list[list[str]] = []
        cost = 0
        blocked_mid: set[str] = set()
        for fa, mb in zip(flegs, perm):
            blocked = (
                (blocked_mid | set(flegs) | set(mlegs) | active_fe_block)
                - {fa, mb} - (core_hubs - active_fe_block)
            )
            path = _shortest_device_path(
                adj, fa, {mb}, blocked=blocked, max_hop=hop_cap,
            )
            if path is not None and _path_uses_foreign_legs(
                path, flegs, mlegs, fa, mb,
            ):
                path = None
            # 仅经本结构多端口壳（无中间 RLCT/FIL 贯通）：同多端口双焊盘，
            # 不当作串联支路（否则同多端口两侧腿会冒充贯通）
            if path is not None and len(path) >= 3:
                mid = path[1:-1]
                if mid and all(x in core_hubs for x in mid):
                    path = None
                elif any(
                    _is_active_fe_through_ref(str(x), schematic_res)
                    and str(x) not in sandwich_ic
                    for x in mid
                ):
                    # 中段过 PA/LNA/TRX = 穿过芯片，不是被动匹配夹心
                    path = None
            # ANT→TRX：禁止经 JUMP/公共端朝天线逆行
            if path is not None and not _path_follows_ant_to_trx(
                path, ant_dist, pad_core=core, schematic_res=schematic_res,
            ):
                path = None
            if path is None:
                # 双 stub：两腿同属一对匹配，但不冒充串联通路
                paths.append([fa, mb])
                cost += _path_cost(
                    None, fa, mb, schematic_res, dual_stub=True,
                )
            else:
                paths.append(path)
                cost += _path_cost(path, fa, mb, schematic_res)
                blocked_mid.update(
                    x for x in path[1:-1] if x not in core_hubs
                )
        if cost < best_cost:
            best_cost = cost
            best_paths = paths

    return best_paths or [[fa] for fa in flegs]


def _fork_series_to_gnd_tails(
    fork_shared_nets: set[str],
    schematic_res,
    net_to_devs: dict[str, list[str]],
    dev_to_nets: dict[str, list[str]],
    *,
    exclude: set[str],
) -> list[tuple[str, tuple[str, ...]]]:
    """fork RF 网上的「串联 → 对地/终端」尾巴。

    不变量（无位号分支）：
      1) 存在串联 RLCT A：至少一脚在 fork.shared 非地网
      2) A 另有私有非地网：该网上 peer 全是对地/终端并联（is_shunt），
         或仅 A 自己（开放 stub 仍收 A）
      3) 私有网上的并联终端并入干路；A 为 copad 分叉前最后一颗串联干路
      4) 双 RF 贯通桥（两侧私有网都有非 shunt peer）→ 不收

    返回 [(series_ref, (term_refs…)), …]
    """
    out: list[tuple[str, tuple[str, ...]]] = []
    seen_series: set[str] = set()
    for net in fork_shared_nets:
        for d in net_to_devs.get(str(net), []) or []:
            ds = str(d or "").strip()
            if not ds or ds in exclude or ds in seen_series:
                continue
            if not is_series_rlct(ds, schematic_res, dev_to_nets):
                continue
            nets = [
                str(n)
                for n in (dev_to_nets.get(ds) or [])
                if n and not is_gnd_net(str(n))
            ]
            on_fork = [n for n in nets if n in fork_shared_nets]
            private = [n for n in nets if n not in fork_shared_nets]
            if not on_fork or not private:
                continue
            terms: list[str] = []
            ok = True
            for pn in private:
                peers = [
                    str(p)
                    for p in (net_to_devs.get(pn) or [])
                    if str(p or "").strip() and str(p) != ds
                ]
                for p in peers:
                    if not is_shunt_device(p, schematic_res, dev_to_nets):
                        ok = False
                        break
                    if p not in exclude:
                        terms.append(p)
                if not ok:
                    break
            if not ok:
                continue
            seen_series.add(ds)
            # 终端稳定序
            terms_u = tuple(sorted(set(terms), key=str))
            out.append((ds, terms_u))
    return out


def _resolve_copad_branches(
    fork: CoPadPair,
    merge: CoPadPair,
    fork_pad: tuple[str, ...],
    merge_pad: tuple[str, ...],
    schematic_res,
    signal_infos,
    *,
    full_adj: Optional[dict[str, set[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """展开 copad 结构为：干路前、支路一、支路二、干路后。

    两条支路：分叉腿—合并腿最小费用匹配；局部子图内最短路；
    无路则开放 stub。核内多端口可被两条支路共用。
    """
    flegs = [fork.leg_a, fork.leg_b]
    mlegs = [merge.leg_a, merge.leg_b]
    # 干路 = 焊盘上非腿成员（不因并联排除：并联只不能当腿候选）
    # 另：同 shared 网上的 deg-1 RLCT 挂件（与对腿共网）
    # 仍是干路件，虽不进三端 canonicalize，须并入 trunk_before
    trunk_before_list = [d for d in fork_pad if d not in set(flegs)]
    trunk_after = tuple(d for d in merge_pad if d not in set(mlegs))

    if full_adj is None:
        full_adj = device_graph_from_signal_infos(schematic_res, signal_infos)
    if net_to_devs is None or dev_to_nets is None:
        net_to_devs, dev_to_nets = build_net_device_maps(signal_infos or [])
    # 焊盘网并联挂件 → 干路（通用：共网 + RLCT + shunt，非腿、非 JUMP 串桥）
    seen_tb = set(trunk_before_list) | set(flegs)
    fork_shared = {
        str(n) for n in (fork.shared_nets or ()) if n and not is_gnd_net(str(n))
    }
    for net in fork_shared:
        for d in net_to_devs.get(str(net), []) or []:
            ds = str(d or "")
            if not ds or ds in seen_tb:
                continue
            if not is_rlct(ds, schematic_res):
                continue
            if not is_shunt_device(ds, schematic_res, dev_to_nets):
                continue  # 串联桥（JUMP 侧 deg≥2）不进干路
            trunk_before_list.append(ds)
            seen_tb.add(ds)
    # 串联→对地/终端尾巴（罕见）：fork RF 网上的串联 RLCT，其私有网仅接
    # 对地并联终端。该串联件即「copad 分叉前」最后一颗干路
    # （跨 pad 网↔私有网）；私有网上的并联件一并入干路。
    # 禁止把双 RF 网串联桥（两侧皆有非 shunt 主链 peer）当尾巴。
    for series_ref, term_refs in _fork_series_to_gnd_tails(
        fork_shared,
        schematic_res,
        net_to_devs,
        dev_to_nets,
        exclude=seen_tb | set(mlegs),
    ):
        if series_ref not in seen_tb:
            trunk_before_list.append(series_ref)
            seen_tb.add(series_ref)
        for tr in term_refs:
            if tr not in seen_tb:
                trunk_before_list.append(tr)
                seen_tb.add(tr)
    # 稳定序：靠 ANT 近者在后（与 pad 展示一致）；无 dist 则名字
    if ant_dist:
        trunk_before_list.sort(
            key=lambda r: (int(ant_dist.get(r, 10**9)), str(r)),
        )
    else:
        trunk_before_list.sort(key=str)
    trunk_before = tuple(trunk_before_list)
    core = set(fork_pad) | set(merge_pad) | set(trunk_before)
    legs = set(flegs) | set(mlegs)
    adj = _copad_local_adj(
        full_adj, core, legs, schematic_res, net_to_devs,
        dev_to_nets=dev_to_nets,
    )
    core_hubs = {d for d in core if not is_rlct(d, schematic_res)}

    raw_paths = _match_branch_paths(
        flegs, mlegs, adj, core_hubs, schematic_res,
        ant_dist=ant_dist, pad_core=core,
    )

    # 旁挂禁止塞入本结构其它腿（同焊盘共网的另一腿不是「支路旁挂」）
    exclude_legs = set(flegs) | set(mlegs)
    branches = [
        _format_branch_nodes(
            p, schematic_res, net_to_devs, dev_to_nets, exclude=exclude_legs,
        )
        for p in raw_paths[:2]
    ]
    while len(branches) < 2:
        branches.append(())
    branches = sorted(branches[:2], key=lambda b: (b[0] if b else "", b))
    if trunk_before and trunk_before == trunk_after:
        trunk_after = ()
    return trunk_before, branches[0], branches[1], trunk_after


def _is_real_series_path(
    path: list[str],
    dev_to_nets: dict[str, list[str]],
) -> bool:
    """是否为真实串联通路（非双 stub 开放端）。"""
    if not path or len(path) < 2:
        return False
    if len(path) >= 3:
        return True
    # 恰 2 点：仅当共信号网才算串联（双 stub 无共网）
    return _devices_share_signal_net(path[0], path[1], dev_to_nets)


def _pads_linked_by_passive_paths(
    near_o: list[str],
    far_o: list[str],
    schematic_res,
    signal_infos,
    *,
    full_adj: Optional[dict[str, set[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> tuple[int, tuple[str, ...]]:
    """两焊盘是否经被动局部图可匹配贯通（通用多跳，非单网共腿）。

    与支路展开同一套局部邻接 + 腿匹配：统计真实串联支路条数。
    返回 (真实贯通支路数, 用于 bridge_devs 的中间器件样例)。
    不绑项目位号；中段 PA/LNA/TRX 不当贯通（穿过芯片，不是匹配夹心）。
    不经 TRX 抄近路（局部图不从多端口非腿网扫全板；有源前端不进局部图）。
    可传入缓存 full_adj/maps，避免成组阶段对每对焊盘重复建图。
    ant_dist：有则强制支路 ANT→TRX 不逆行，避免 JUMP 回灌误配。
    """
    if full_adj is None:
        full_adj = device_graph_from_signal_infos(schematic_res, signal_infos)
    if net_to_devs is None or dev_to_nets is None:
        net_to_devs, dev_to_nets = build_net_device_maps(signal_infos or [])
    fork = _pair_from_pad_ends(
        near_o, "_near", role="fork", schematic_res=schematic_res,
        dev_to_nets=dev_to_nets, net_to_devs=net_to_devs, ant_dist=ant_dist,
    )
    merge = _pair_from_pad_ends(
        far_o, "_far", role="merge", schematic_res=schematic_res,
        dev_to_nets=dev_to_nets, net_to_devs=net_to_devs, ant_dist=ant_dist,
    )
    flegs = [fork.leg_a, fork.leg_b]
    mlegs = [merge.leg_a, merge.leg_b]
    core = set(near_o) | set(far_o)
    legs = set(flegs) | set(mlegs)
    adj = _copad_local_adj(
        full_adj, core, legs, schematic_res, net_to_devs,
        dev_to_nets=dev_to_nets,
    )
    core_hubs = {d for d in core if not is_rlct(d, schematic_res)}
    raw = _match_branch_paths(
        flegs, mlegs, adj, core_hubs, schematic_res,
        ant_dist=ant_dist, pad_core=core,
    )
    real: list[list[str]] = []
    mids: list[str] = []
    for p in raw:
        if not _is_real_series_path(p, dev_to_nets):
            continue
        fe_mids = [
            x for x in p[1:-1]
            if _is_active_fe_through_ref(str(x), schematic_res)
        ]
        if fe_mids:
            # 夹心 IC 支路：只认 腿—IC—腿。更长的穿芯片抄道丢掉。
            if not (len(p) == 3 and len(fe_mids) == 1):
                continue
        real.append(p)
        for x in p[1:-1]:
            if x not in mids and x not in legs:
                mids.append(x)
    n_real = len(real)
    return n_real, tuple(mids[:8])


def _make_structure(
    near_net: str,
    near_o: list[str],
    far_net: str,
    far_o: list[str],
    schematic_res,
    signal_infos,
    *,
    link_kind: str,
    bridge_devs: tuple[str, ...] = (),
    full_adj: Optional[dict[str, set[str]]] = None,
    net_to_devs: Optional[dict[str, list[str]]] = None,
    dev_to_nets: Optional[dict[str, list[str]]] = None,
    ant_dist: Optional[dict[str, int]] = None,
) -> CoPadStructure:
    if net_to_devs is None or dev_to_nets is None:
        n2d, d2n = build_net_device_maps(signal_infos or [])
    else:
        n2d, d2n = net_to_devs, dev_to_nets
    fork = _pair_from_pad_ends(
        near_o, near_net, role="fork", schematic_res=schematic_res,
        dev_to_nets=d2n, net_to_devs=n2d, ant_dist=ant_dist,
    )
    merge = _pair_from_pad_ends(
        far_o, far_net, role="merge", schematic_res=schematic_res,
        dev_to_nets=d2n, net_to_devs=n2d, ant_dist=ant_dist,
    )
    # 兼容旧字段：线性链
    chain: list[str] = []
    for d in near_o:
        if d not in chain:
            chain.append(d)
    for d in bridge_devs:
        if d not in chain:
            chain.append(d)
    for d in far_o:
        if d not in chain:
            chain.append(d)

    # 三端配对已完成。未进三端的共网 RLCT 并入 pad，resolve 写入
    # trunk_before（分叉盘 / ANT 侧）与 trunk_after（合并盘 / 芯片侧）。
    extras_near = _copad_pad_rlct_extras(
        near_net, list(near_o), schematic_res,
        ant_dist=ant_dist, dev_to_nets=d2n, net_to_devs=n2d,
    )
    extras_far = _copad_pad_rlct_extras(
        far_net, list(far_o), schematic_res,
        ant_dist=ant_dist, dev_to_nets=d2n, net_to_devs=n2d,
    )
    fork_pad = tuple(
        list(near_o) + [e for e in extras_near if e not in set(near_o)]
    )
    merge_pad = tuple(
        list(far_o) + [e for e in extras_far if e not in set(far_o)]
    )
    trunk_before, b1, b2, trunk_after = _resolve_copad_branches(
        fork, merge, fork_pad, merge_pad,
        schematic_res, signal_infos,
        full_adj=full_adj, net_to_devs=n2d, dev_to_nets=d2n,
        ant_dist=ant_dist,
    )
    return CoPadStructure(
        fork=fork,
        merge=merge,
        trunk_ref=fork.trunk_ref or merge.trunk_ref,
        fork_pad=fork_pad,
        merge_pad=merge_pad,
        internal_chain=tuple(chain),
        link_kind=link_kind,
        trunk_before=trunk_before,
        branch1=b1,
        branch2=b2,
        trunk_after=trunk_after,
    )


def build_copad_structures(
    pairs: list[CoPadPair] | None = None,
    signal_infos=None,
    *,
    complete_only: bool = True,
    schematic_res=None,
) -> list[CoPadStructure]:
    """完整 COPAD 结构：两块焊盘成组。

    成组顺序（证据强→弱，禁止弱 bridge 抢真双支路的合并盘）：
      1) same_legs 同腿双焊盘
      2) paths 多跳被动贯通（≥2 条真实支路；中段多端口优先）
      3) bridge 单网腿桥
    无同 IC 硬配。complete_only=False 时未成组焊盘可标 single。
    """
    del pairs
    if schematic_res is None:
        return []

    # pad_sig：焊盘发现（过滤 RF 网）；branch_sig：桥/支路（全量拓扑）
    pad_sig = _pad_discovery_signal_infos(schematic_res, signal_infos)
    branch_sig = _branch_topology_signal_infos(schematic_res, signal_infos)
    if not pad_sig:
        return []

    ant_dist = _dist_to_nearest_ant(
        _copad_device_adj(pad_sig), schematic_res,
    )
    tripods = _list_pad_tripods(schematic_res, pad_sig, ant_dist)
    # 全量拓扑缓存：paths 阶段会对多对焊盘复用，禁止每对重建
    full_adj_br = device_graph_from_signal_infos(schematic_res, branch_sig)
    net_to_devs_bridge, dev_to_nets_bridge = build_net_device_maps(branch_sig)

    structures: list[CoPadStructure] = []
    used_groups: set[tuple[str, frozenset[str]]] = set()
    used_pair_keys: set[tuple[str, str]] = set()

    def _gid(net: str, ordered: list[str]) -> tuple[str, frozenset[str]]:
        return (
            str(net),
            frozenset(_pad_rlct_legs(ordered, schematic_res)),
        )

    def _try_add(near_net, near_o, far_net, far_o, link_kind, bridge_devs=()) -> bool:
        if near_net == far_net:
            return False
        g1, g2 = _gid(near_net, near_o), _gid(far_net, far_o)
        if g1 in used_groups or g2 in used_groups:
            return False
        st = _make_structure(
            near_net, near_o, far_net, far_o, schematic_res, branch_sig,
            link_kind=link_kind, bridge_devs=bridge_devs,
            full_adj=full_adj_br,
            net_to_devs=net_to_devs_bridge,
            dev_to_nets=dev_to_nets_bridge,
            ant_dist=ant_dist,
        )
        fk = (st.fork.leg_a, st.fork.leg_b)
        mk = (
            (st.merge.leg_a, st.merge.leg_b)
            if st.merge is not None
            else None
        )
        if mk is not None and fk == mk:
            return False
        else:
            if fk in used_pair_keys or (
                mk is not None and mk in used_pair_keys
            ):
                return False
            used_pair_keys.add(fk)
            if mk is not None:
                used_pair_keys.add(mk)
        structures.append(st)
        used_groups.add(g1)
        used_groups.add(g2)
        return True

    def _eligible_pairs(*, allow_same_legs: bool):
        """未占用焊盘对：≥2 腿、不共享多端口壳。同一网两组对可分别成组。"""
        for i, (n1, o1) in enumerate(tripods):
            if _gid(n1, o1) in used_groups:
                continue
            for n2, o2 in tripods[i + 1 :]:
                if n1 == n2:
                    continue
                if _gid(n2, o2) in used_groups:
                    continue
                legs1 = _pad_rlct_legs(o1, schematic_res)
                legs2 = _pad_rlct_legs(o2, schematic_res)
                if len(legs1) < 2 or len(legs2) < 2:
                    continue
                shared = set(o1) & set(o2)
                if any(not is_rlct(d, schematic_res) for d in shared):
                    continue
                same = legs1 == legs2
                if same and not allow_same_legs:
                    continue
                near_net, near_o, far_net, far_o = _order_pads_by_ant(
                    n1, o1, n2, o2, ant_dist, schematic_res, pad_sig,
                )
                yield near_net, near_o, far_net, far_o, legs1, legs2, same

    # 阶段1：同腿（最强证据）
    for near_net, near_o, far_net, far_o, legs1, legs2, same in _eligible_pairs(
        allow_same_legs=True,
    ):
        if not same:
            continue
        hubs = (
            _pad_multiport(near_o, schematic_res),
            _pad_multiport(far_o, schematic_res),
        )
        _try_add(
            near_net, near_o, far_net, far_o,
            "same_legs", bridge_devs=tuple(h for h in hubs if h),
        )

    # 阶段2：双真实支路 paths（先于 bridge）
    # 禁止弱单网 bridge 先占合并盘，把真 copad 双支路挤成 orphan→假 colay
    path_cands: list[tuple] = []
    for near_net, near_o, far_net, far_o, _l1, _l2, same in _eligible_pairs(
        allow_same_legs=False,
    ):
        if same:
            continue
        n_real, mids = _pads_linked_by_passive_paths(
            near_o, far_o, schematic_res, branch_sig,
            full_adj=full_adj_br,
            net_to_devs=net_to_devs_bridge,
            dev_to_nets=dev_to_nets_bridge,
            ant_dist=ant_dist,
        )
        if n_real < 2:
            continue
        n_multi = sum(
            1 for d in mids
            if d and not is_rlct(str(d), schematic_res)
        )
        # 多端口中段优先、中段更短优先
        path_cands.append(
            (
                (n_multi, -len(mids), str(near_net), str(far_net)),
                near_net, near_o, far_net, far_o, mids,
            )
        )
    path_cands.sort(key=lambda x: x[0], reverse=True)
    for _k, near_net, near_o, far_net, far_o, mids in path_cands:
        _try_add(
            near_net, near_o, far_net, far_o,
            "paths", bridge_devs=mids,
        )

    # 阶段3：剩余焊盘单网 bridge
    for near_net, near_o, far_net, far_o, legs1, legs2, same in _eligible_pairs(
        allow_same_legs=False,
    ):
        if same:
            continue
        bridge = _nets_bridge_rlct_legs(
            legs1, legs2, net_to_devs_bridge, {near_net, far_net},
        )
        if bridge:
            _try_add(
                near_net, near_o, far_net, far_o,
                "bridge", bridge_devs=tuple(bridge),
            )

    if not complete_only:
        n2d_s, d2n_s = build_net_device_maps(branch_sig)
        for net, ordered in tripods:
            if _gid(net, ordered) in used_groups:
                continue
            fork = _pair_from_pad_ends(
                ordered, net, role="fork", schematic_res=schematic_res,
                dev_to_nets=d2n_s, net_to_devs=n2d_s, ant_dist=ant_dist,
            )
            structures.append(
                CoPadStructure(
                    fork=fork,
                    merge=None,
                    trunk_ref=ordered[0],
                    fork_pad=tuple(ordered),
                    merge_pad=(),
                    internal_chain=tuple(ordered),
                    link_kind="single",
                )
            )

    return _drop_foreign_group_legs_from_trunks(structures)


def _structure_pair_legs(st: CoPadStructure) -> set[str]:
    """一组 copad 的分叉/合并对腿。"""
    legs: set[str] = set()
    for p in (st.fork, st.merge):
        if p is None:
            continue
        for a in (getattr(p, "leg_a", ""), getattr(p, "leg_b", "")):
            s = str(a or "").strip()
            if s:
                legs.add(s)
    return legs


def _drop_foreign_group_legs_from_trunks(
    structures: list[CoPadStructure],
) -> list[CoPadStructure]:
    """一网两组时，本组干路不得含另一组对腿。

    焊盘上非本组腿会进 trunk_before/after；同网另一组成对的腿不是
    本组干路，否则 layout 会把两块夹心拽成一条 mega-band。
    """
    if len(structures) < 2:
        return structures
    leg_sets = [_structure_pair_legs(st) for st in structures]
    out: list[CoPadStructure] = []
    for i, st in enumerate(structures):
        foreign: set[str] = set()
        for j, legs in enumerate(leg_sets):
            if j != i:
                foreign |= legs
        if not foreign:
            out.append(st)
            continue
        tb = tuple(r for r in (st.trunk_before or ()) if r not in foreign)
        ta = tuple(r for r in (st.trunk_after or ()) if r not in foreign)
        if tb == tuple(st.trunk_before or ()) and ta == tuple(st.trunk_after or ()):
            out.append(st)
        else:
            out.append(replace(st, trunk_before=tb, trunk_after=ta))
    return out


def detect_copad_pairs(schematic_res, signal_infos=None) -> list[CoPadPair]:
    """兼容入口：扫描焊盘 → 完整结构 → 交付对。"""
    pad_sig = _pad_discovery_signal_infos(schematic_res, signal_infos)
    ant_dist = _dist_to_nearest_ant(
        _copad_device_adj(pad_sig), schematic_res,
    )
    tripods = _list_pad_tripods(schematic_res, pad_sig, ant_dist)
    structures = build_copad_structures(
        None, signal_infos=signal_infos, complete_only=True,
        schematic_res=schematic_res,
    )
    pairs = pairs_from_structures(structures)
    detect_copad_pairs.last_structures = tuple(structures)  # type: ignore[attr-defined]
    detect_copad_pairs.last_pad_tripods = tuple(  # type: ignore[attr-defined]
        (n, tuple(o)) for n, o in tripods
    )
    return pairs


def mark_copad_on_paths(paths: list[Path], pairs: list[CoPadPair]) -> set[str]:
    """给 Path 上的 Co-pad 腿打 is_copad。"""
    copad_refs = {ref for pair in pairs for ref in (pair.leg_a, pair.leg_b)}
    if not copad_refs:
        return set()
    for path in paths:
        for dev in path.devices:
            if dev.ref in copad_refs:
                dev.is_copad = True
    return copad_refs


def pairs_from_structures(structures: list[CoPadStructure]) -> list[CoPadPair]:
    """完整结构展开为对列表（打标/预览只认这些）。"""
    out: list[CoPadPair] = []
    seen: set[tuple[str, str]] = set()
    for st in structures:
        for p in (st.fork, st.merge):
            if p is None:
                continue
            key = (p.leg_a, p.leg_b)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def _copad_used_pad_nets(structures: list[CoPadStructure]) -> set[str]:
    """完整 Co-pad 已占用的焊盘网（fork_pad/merge_pad 不直接存网名时靠 pair.shared_nets）。"""
    used: set[str] = set()
    for st in structures:
        for p in (st.fork, st.merge):
            if p is None:
                continue
            for n in p.shared_nets:
                if n:
                    used.add(str(n))
        # 结构构造时 near/far 网写在 pair.shared_nets；单 pad 字段无网名
    return used


def copad_structure_touches_refs(
    st: CoPadStructure,
    sheet_refs: set[str],
    *,
    main_refs: Optional[set[str]] = None,
) -> bool:
    """结构是否挂到本 sheet 位号集合（预览/手册共用）。

    规则：
      1) 分叉腿 / 合并腿 / 支路 与 sheet 有交集
      2) **主链**已碰到任一分叉/合并腿 → 主路夹心（不强制 trunk 进 path）
      3) 否则视为仅侧支挂接：须同时碰到分叉对与合并对，且
         trunk_before 非空时 sheet 须含干路前件
         （防 FIL 焊盘假 bridge 仅凭 fork 腿误挂到无关旁路组）
    """
    sheet = {str(x or "").strip() for x in (sheet_refs or set()) if str(x or "").strip()}
    if not sheet:
        return False
    fork = {
        str(getattr(st.fork, "leg_a", "") or "").strip(),
        str(getattr(st.fork, "leg_b", "") or "").strip(),
    }
    fork.discard("")
    merge: set[str] = set()
    if st.merge is not None:
        merge = {
            str(getattr(st.merge, "leg_a", "") or "").strip(),
            str(getattr(st.merge, "leg_b", "") or "").strip(),
        }
        merge.discard("")
    branches = {
        str(x or "").strip()
        for x in (*(st.branch1 or ()), *(st.branch2 or ()))
        if str(x or "").strip()
    }
    core = fork | merge | branches
    if not (core & sheet):
        return False
    main = (
        {str(x or "").strip() for x in (main_refs or set()) if str(x or "").strip()}
        if main_refs is not None
        else sheet
    )
    # 主链已碰到分叉或合并腿 → 正常主路夹心
    if (fork & main) or (merge & main):
        return True
    # 仅侧支/附属位号：须 fork+merge 双对 + trunk_before（若有）
    before = {
        str(x or "").strip()
        for x in (getattr(st, "trunk_before", ()) or ())
        if str(x or "").strip()
    }
    if before and not (before & sheet):
        return False
    if not (fork & sheet):
        return False
    if merge and not (merge & sheet):
        return False
    return True


def label_copad_structures_for_sheet(
    paths: list[Path],
    structures: list[CoPadStructure],
    *,
    extra_refs: Optional[set[str]] = None,
    main_paths: Optional[list[Path]] = None,
) -> list[tuple[str, Optional[str], CoPadStructure]]:
    """本 ANT 页：结构内 对A=两器件同标A，对B=两器件同标B；下一结构 对C/对D。

    extra_refs：侧支成员等附属位号，可参与挂接（须满足 copad_structure_touches_refs）。
    main_paths：主路（默认 paths）；用于区分「主链夹心」与「仅侧支完整夹心」。

    返回 [(fork_label, merge_label|None, structure), ...]
    """
    path_list = list(paths or [])
    main_list = list(main_paths) if main_paths is not None else path_list
    main_refs = {d.ref for p in main_list for d in p.devices}
    ant_refs = set(main_refs)
    for p in path_list:
        for d in p.devices:
            ant_refs.add(d.ref)
    if extra_refs:
        ant_refs |= {str(x) for x in extra_refs if x}

    local = [
        st for st in structures
        if copad_structure_touches_refs(st, ant_refs, main_refs=main_refs)
    ]
    # 稳定序：按 fork 腿名
    local.sort(key=lambda st: (st.fork.leg_a, st.fork.leg_b, st.trunk_ref))

    labeled: list[tuple[str, Optional[str], CoPadStructure]] = []
    ref_to_label: dict[str, str] = {}
    for i, st in enumerate(local):
        # 结构 i → 对字母 2i, 2i+1
        base = 2 * i
        if base < 26:
            fork_l = chr(ord("A") + base)
        else:
            fork_l = f"P{i + 1}f"
        merge_l: Optional[str] = None
        if st.merge is not None:
            if base + 1 < 26:
                merge_l = chr(ord("A") + base + 1)
            else:
                merge_l = f"P{i + 1}m"
        labeled.append((fork_l, merge_l, st))
        # 对A 的两个器件都标 A
        ref_to_label[st.fork.leg_a] = fork_l
        ref_to_label[st.fork.leg_b] = fork_l
        if st.merge is not None and merge_l is not None:
            ref_to_label[st.merge.leg_a] = merge_l
            ref_to_label[st.merge.leg_b] = merge_l

    for path in paths:
        for dev in path.devices:
            if dev.ref in ref_to_label:
                dev.is_copad = True
                dev.copad_label = ref_to_label[dev.ref]
            else:
                dev.copad_label = ""
    return labeled



# 跨模块公开 API（与 `_` 实现同一函数；测试可继续 import `_` 别名）
pad_discovery_signal_infos = _pad_discovery_signal_infos
branch_topology_signal_infos = _branch_topology_signal_infos
is_jump_only_far_rlct = _is_jump_only_far_rlct
copad_device_adj = _copad_device_adj
dist_to_nearest_ant = _dist_to_nearest_ant
pad_rlct_arm_role = _pad_rlct_arm_role
pick_copad_legs = _pick_copad_legs
list_pad_tripods = _list_pad_tripods
pair_from_pad_ends = _pair_from_pad_ends
pad_rlct_legs = _pad_rlct_legs
path_follows_ant_to_trx = _path_follows_ant_to_trx
copad_used_pad_nets = _copad_used_pad_nets
leg_nearest_ant_port = _leg_nearest_ant_port


# ---------------------------------------------------------------------------
# JUMP / Co-lay：拆到独立模块，避免 path↔copad↔side 循环 import。
# 兼容旧 `from refnum.copad import CoLayPair` 等用 __getattr__ 转发。
# ---------------------------------------------------------------------------
def __getattr__(name: str):
    if name in {
        "device_touches_jump_net",
        "mark_jump_attach_on_devices",
        "mark_jump_attach_on_paths",
    }:
        import refnum.side_chain as _sc

        return getattr(_sc, name)
    if name in {
        "CoLayPair",
        "CoLayStructure",
        "build_colay_structures",
        "colay_pad_trunk_refs",
        "colay_to_copad_structures",
        "label_colay_structures_for_sheet",
        "mark_colay_on_paths",
        "mark_colay_trunk_on_paths",
        "pairs_from_colay_structures",
        "synthesize_colay_branch_paths",
    }:
        import refnum.colay as _cl

        return getattr(_cl, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
